"""Read dated, reviewed research without network access or invented weights."""

import json
from functools import lru_cache
from pathlib import Path
from datetime import date
import math
from urllib.parse import urlsplit

from .models import Snapshot, snapshot_from_dict, snapshot_sha256


DATA_ROOT = Path(__file__).parent / "data"


@lru_cache(maxsize=1)
def research_catalog() -> dict:
    """Return research provenance and dated capabilities, not live status."""
    return json.loads((DATA_ROOT / "research-20261028.json").read_text())


def research_record(symbol: str) -> dict | None:
    """Find a reviewed fund record, preserving unresolved qualifications."""
    return (research_catalog()["funds"].get(symbol)
            or research_catalog().get("fallback_ratios", {}).get(symbol))


def is_research_snapshot(snapshot: Snapshot) -> bool:
    """Recognize a research candidate by symbol and original evidence hash."""
    record = research_record(snapshot.etf_symbol)
    evidence = snapshot.equity_evidence
    known_hashes = {research_catalog()["archive_sha256"],
                    *research_catalog()["source_table_sha256"].values()}
    return bool(record and record.get("snapshot_sha256") and evidence
                and (known_hashes.intersection(evidence.source_hashes)
                     or (snapshot.fetched_at
                         == record.get("snapshot_fetched_at")
                         and snapshot.source_url == record["source_url"])))


def qualifies_research(snapshot: Snapshot) -> bool:
    """Bind a fixed dated snapshot to the exact reviewed content digest."""
    record = research_record(snapshot.etf_symbol)
    return bool(is_research_snapshot(snapshot)
                and snapshot_sha256(snapshot) == record["snapshot_sha256"])


@lru_cache(maxsize=1)
def _bundled_payload() -> dict:
    """Read the small fixed dataset once per process, without network I/O."""
    return json.loads(
        (DATA_ROOT / "research-snapshots-20261028.json").read_text())


def bundled_snapshot(symbol: str) -> Snapshot | None:
    """Load an immutable dated fallback; never retimestamp old holdings."""
    value = _bundled_payload().get(symbol)
    if value is None:
        return None
    snapshot = snapshot_from_dict(value)
    if not qualifies_research(snapshot):
        raise ValueError("Bundled research snapshot digest mismatch")
    return snapshot


def historical_ratio(symbol: str) -> dict | None:
    """Validate a dated reviewed NAV-ratio reference, including rounded zero.

    This is an aggregate estimate, never evidence of complete stock rows.
    No issuer/category average is substituted for a missing fund record.
    """
    record = research_record(symbol)
    if not record or record.get("calculation_support") != "ratio_only":
        return None
    ratio = record.get("taiwan_NAV_ratio_pct")
    if (type(ratio) not in (int, float) or not math.isfinite(ratio)
            or not 0 <= ratio <= 100):
        return None
    try:
        day = record["ratio_observation_date"]
        if date.fromisoformat(day).isoformat() != day:
            return None
        url = record["official_entry_url"]
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in (
                "www.sitca.org.tw", "www.megafunds.com.tw")
                or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            return None
    except (KeyError, ValueError, TypeError):
        return None
    return dict(date=day, share=ratio / 100, source_url=url)
