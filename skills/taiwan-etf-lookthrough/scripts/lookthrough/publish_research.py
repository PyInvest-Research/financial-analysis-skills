"""Publish explicitly reviewed fixed snapshots offline, never daily routes."""

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import snapshot_from_dict, snapshot_to_dict
from .publish import (
    _atomic_write, _encoded, _existing, _read_index, _read_json, _safe_root,
    _timestamp,
)
from .refresh_contract import directory
from .research import publication_catalog, qualifies_published_candidate
from .validate import validate_snapshot


def publish_reviewed(out_root: Path, snapshots: list[Path], *,
                     published_at: str, today: date) -> dict:
    """Preflight the whole batch before adding dated/latest/index artifacts.

    Input digests must match the explicit publication catalog AND existing
    candidate identity checks. Financial validation is unchanged. A collision
    with any other route/content fails; no existing snapshot is replaced.
    Each new file is atomic, with the index written last. No network, pruning,
    retimestamping, normalization or automatic-acquisition registration occurs.
    """
    root = _safe_root(out_root)
    _timestamp(published_at)
    rows = _read_index(root)
    dated, latest = _existing(root, rows, directory())
    if not snapshots:
        raise ValueError("At least one reviewed snapshot is required")
    pending, seen = [], set()
    for path in snapshots:
        value = snapshot_from_dict(_read_json(path))
        symbol, day = value.etf_symbol, str(value.holdings_date)
        if symbol in seen or not qualifies_published_candidate(value):
            raise ValueError(
                "Duplicate or unqualified fixed research snapshot")
        seen.add(symbol)
        result = validate_snapshot(
            value, directory(), today, policy="annotated")
        if result.status == "fail":
            raise ValueError(
                f"Fixed research financial validation failed: {symbol}")
        raw = _encoded(snapshot_to_dict(value))
        previous = rows.get(symbol)
        expected = dict(symbol=symbol, issuer=value.issuer,
                        is_active=value.is_active, route="research",
                        source_url=value.source_url, last_status=result.status,
                        last_attempt_at=published_at, last_error=None,
                        latest_date=day, dates=[day])
        if previous is not None:
            # Only an explicitly reviewed, exact empty official-only entry may
            # be promoted. Never replace a published, daily or changed entry.
            reviewed_previous = publication_catalog()[symbol].get(
                'previous_official_only_entry')
            if (previous == reviewed_previous
                    and previous.get('route') == 'official_only'
                    and previous.get('last_status') == 'official_only'
                    and previous.get('dates') == []
                    and previous.get('latest_date') is None
                    and previous.get('issuer') == value.issuer
                    and previous.get('is_active') is value.is_active
                    and not dated.get(symbol) and symbol not in latest
                    and not (root / symbol).exists()):
                pending.append((symbol, day, raw, expected))
                continue
            # A repeat is a no-op only for exactly the same published batch.
            comparable = {k: v for k, v in expected.items()
                          if k != "last_attempt_at"}
            if (any(previous.get(k) != v for k, v in comparable.items())
                    or set(dated.get(symbol, {})) != {day}
                    or dated[symbol][day][1] != raw
                    or latest.get(symbol, (None, None))[1] != raw):
                raise ValueError(f"Existing publication conflicts: {symbol}")
            continue
        if (dated.get(symbol) or symbol in latest
                or (root / symbol).exists()):
            raise ValueError(
                f"Unindexed snapshot directory conflicts: {symbol}")
        pending.append((symbol, day, raw, expected))
    if not pending:
        return _read_json(root / "index.json")
    for symbol, day, raw, row in pending:
        _atomic_write(root / symbol / f"{day}.json", raw)
        _atomic_write(root / symbol / "latest.json", raw)
        rows[symbol] = row
    index = dict(schema_version="1", generated_at=published_at,
                 etfs=[rows[symbol] for symbol in sorted(rows)])
    _atomic_write(root / "index.json", _encoded(index))
    return index


def main() -> None:
    """Explicit offline CLI; not called by the daily publisher."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("snapshots", nargs="+", type=Path)
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    publish_reviewed(args.out, args.snapshots,
                     published_at=now.isoformat(),
                     today=now.astimezone(ZoneInfo("Asia/Taipei")).date())


if __name__ == "__main__":
    main()
