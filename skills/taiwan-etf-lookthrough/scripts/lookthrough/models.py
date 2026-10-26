"""Represent ETF snapshots and their deterministic JSON content identity."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import math
from typing import Any, Literal


class SchemaError(ValueError):
    """Signal unsupported versions or malformed snapshot JSON fields."""


@dataclass(frozen=True)
class Holding:
    """Store a disclosed NAV weight and optional futures notional exposure.

    NAV weights are fractions: cash/other allow [-1, 1], while stock/futures
    allow [0, 1]. Futures exposure_weight is a separate signed notional
    fraction and may exceed those bounds. Missing weights remain None,
    including futures whose notional exposure cannot be established.
    Domain validation is performed separately before snapshots are used.
    """

    code: str
    name: str
    kind: Literal["stock", "futures", "cash", "other"]
    weight: float | None
    exposure_weight: float | None = None


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


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    """Return independent JSON-ready fields, using an ISO date and array."""
    payload = asdict(snapshot)
    payload["holdings_date"] = snapshot.holdings_date.isoformat()
    payload["holdings"] = list(payload["holdings"])
    return payload


def snapshot_from_dict(payload: dict[str, Any]) -> Snapshot:
    """Decode v1 JSON or raise SchemaError for malformed field structures.

    Preserve missing weights without mutating input. Numeric ranges, weight
    totals and source trust still require financial/provenance validation.
    """
    if not isinstance(payload, dict):
        raise SchemaError("Snapshot must be a JSON object")
    if payload.get("schema_version") != "1":
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
    if holding.kind not in ("stock", "futures", "cash", "other"):
        raise SchemaError("Unknown holding kind")
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
