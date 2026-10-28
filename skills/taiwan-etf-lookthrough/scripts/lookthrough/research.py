"""Read dated, reviewed research without network access or invented weights."""

import json
from functools import lru_cache
from pathlib import Path

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
