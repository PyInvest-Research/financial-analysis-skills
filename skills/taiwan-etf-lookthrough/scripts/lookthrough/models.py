"""Represent ETF snapshots and their deterministic JSON content identity."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import math
import re
from typing import Any, Literal


class SchemaError(ValueError):
    """Signal unsupported versions or malformed snapshot JSON fields."""


@dataclass(frozen=True)
class Holding:
    """Store a disclosed NAV weight and optional futures notional exposure.

    NAV weights are fractions: cash/other/derivative_nav allow [-1, 1], while
    stock/futures allow [0, 1]. derivative_nav is a disclosed derivative book
    valuation, never a notional/delta exposure; it enters other NAV once.
    Futures exposure_weight is a separate signed notional
    fraction and may exceed those bounds. Missing weights remain None,
    including futures whose notional exposure cannot be established.
    Domain validation is performed separately before snapshots are used.
    """

    code: str
    name: str
    kind: Literal["stock", "foreign_stock", "futures", "cash", "other",
                  "derivative_nav"]
    weight: float | None
    exposure_weight: float | None = None
    market: str | None = None


@dataclass(frozen=True)
class EquityEvidence:
    """Bind complete actual equity rows to whole-fund TWD NAV evidence.

    Source aggregates and printed per-row percentages remain independent.
    Unknown Taiwan aggregate value stays None; it is never residual-filled.
    """

    complete: bool
    total_nav_twd: float | None
    stock_value_twd: float | None
    stock_rows: int
    taiwan_stock_value_twd: float | None
    source_hashes: tuple[str, ...]


@dataclass(frozen=True)
class Snapshot:
    """Store ordered holdings with their source and observation dates.

    holdings_date is the source's ISO calendar date; fetched_at is a UTC
    ISO 8601 timestamp. A tuple keeps the holding sequence immutable.
    """

    schema_version: str
    etf_symbol: str
    issuer: str
    is_active: bool
    holdings_date: date
    fetched_at: str
    source_url: str
    source_kind: Literal["pyinvest", "official", "agent_official"]
    holdings: tuple[Holding, ...]
    equity_evidence: EquityEvidence | None = None


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    """Return independent JSON-ready fields, using an ISO date and array."""
    if snapshot.schema_version == "1" and (
            snapshot.equity_evidence is not None or any(
                h.market is not None or h.kind == "foreign_stock"
                for h in snapshot.holdings)):
        raise SchemaError("Mixed fields require snapshot schema 2")
    payload = asdict(snapshot)
    payload["holdings_date"] = snapshot.holdings_date.isoformat()
    payload["holdings"] = list(payload["holdings"])
    if snapshot.schema_version == "1":
        payload.pop("equity_evidence")
        for row in payload["holdings"]:
            row.pop("market")
    elif snapshot.equity_evidence is not None:
        payload["equity_evidence"]["source_hashes"] = list(
            snapshot.equity_evidence.source_hashes)
    return payload


def snapshot_from_dict(payload: dict[str, Any]) -> Snapshot:
    """Decode v1 JSON or raise SchemaError for malformed field structures.

    Preserve missing weights without mutating input. Numeric ranges, weight
    totals and source trust still require financial/provenance validation.
    """
    if not isinstance(payload, dict):
        raise SchemaError("Snapshot must be a JSON object")
    if payload.get("schema_version") not in ("1", "2"):
        raise SchemaError("Unsupported snapshot schema_version")
    try:
        for key in ("etf_symbol", "issuer", "holdings_date", "fetched_at",
                    "source_url", "source_kind"):
            if not isinstance(payload[key], str):
                raise SchemaError(f"{key} must be a string")
        if type(payload["is_active"]) is not bool:
            raise SchemaError("is_active must be a boolean")
        if payload["source_kind"] not in (
                "pyinvest", "official", "agent_official"):
            raise SchemaError("Unknown source_kind")
        if not isinstance(payload["holdings"], list):
            raise SchemaError("holdings must be a JSON array")
        fields = dict(payload)
        holdings_date = date.fromisoformat(payload["holdings_date"])
        if holdings_date.isoformat() != payload["holdings_date"]:
            raise SchemaError("holdings_date must use YYYY-MM-DD")
        stamp = datetime.fromisoformat(payload["fetched_at"])
        if ("T" not in payload["fetched_at"]
                or stamp.utcoffset() != timedelta(0)):
            raise SchemaError("fetched_at must be an ISO 8601 UTC timestamp")
        fields["holdings_date"] = holdings_date
        fields["holdings"] = tuple(
            _holding_from_dict(row) for row in payload["holdings"]
        )
        if payload["schema_version"] == "1":
            if "equity_evidence" in payload or any(
                    h.market is not None or h.kind == "foreign_stock"
                    for h in fields["holdings"]):
                raise SchemaError("Mixed fields require snapshot schema 2")
        else:
            evidence = payload.get("equity_evidence")
            if not isinstance(evidence, dict):
                raise SchemaError("Schema 2 requires equity_evidence")
            evidence = dict(evidence)
            hashes = evidence.get("source_hashes")
            if (not isinstance(hashes, list) or not 1 <= len(hashes) <= 10
                    or any(not isinstance(h, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", h) for h in hashes)):
                raise SchemaError("Missing or invalid original source hashes")
            evidence["source_hashes"] = tuple(hashes)
            fields["equity_evidence"] = EquityEvidence(**evidence)
        return Snapshot(**fields)
    except SchemaError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise SchemaError("Malformed snapshot fields") from error


def _holding_from_dict(payload: dict[str, Any]) -> Holding:
    """Decode one row, rejecting invalid enums, types and nonfinite values."""
    if not isinstance(payload, dict):
        raise SchemaError("Each holding must be a JSON object")
    holding = Holding(**payload)
    if not isinstance(holding.code, str) or not isinstance(holding.name, str):
        raise SchemaError("Holding code and name must be strings")
    if holding.kind not in (
            "stock", "foreign_stock", "futures", "cash", "other",
            "derivative_nav"):
        raise SchemaError("Unknown holding kind")
    if holding.market is not None and (
            not isinstance(holding.market, str)
            or not 1 <= len(holding.market) <= 80
            or any(ord(char) < 32 for char in holding.market)):
        raise SchemaError("Invalid listing market label")
    for key in ("weight", "exposure_weight"):
        value = getattr(holding, key)
        if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value)):
            raise SchemaError(f"{key} must be a finite number or null")
    return holding


def snapshot_sha256(snapshot: Snapshot) -> str:
    """Hash compact sorted UTF-8 JSON, including source fetch metadata."""
    canonical = json.dumps(
        snapshot_to_dict(snapshot), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
