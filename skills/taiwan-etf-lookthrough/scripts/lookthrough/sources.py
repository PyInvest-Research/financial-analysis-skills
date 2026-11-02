"""Read local source snapshots without any website transport."""

from datetime import date
import json
from pathlib import Path
import re

from .models import Snapshot, snapshot_from_dict


_COMPANION_NAMES = frozenset({'portfolio.json', 'expected-result.json'})
_SNAPSHOT_FIELDS = frozenset({'etf_symbol', 'holdings_date', 'holdings'})


class SourceUnavailable(RuntimeError):
    """Signal local decoding, schema or snapshot identity failure."""


def _symbol(value: str) -> str:
    """Normalize portfolio-compatible symbols without permitting URL syntax."""
    normalized = value.strip().upper() if isinstance(value, str) else ''
    if not re.fullmatch(r'[0-9A-Z]{4,6}', normalized):
        raise SourceUnavailable('Snapshot symbol must be 4-6 ASCII characters')
    return normalized


def load_local_snapshot(path: Path) -> Snapshot | None:
    """Decode one local snapshot, or skip a recognized demo companion.

    Raise SourceUnavailable with its path for read, schema and identity
    errors. This helper permits callers to isolate per-file failures while
    the bulk loader retains its strict all-files contract.
    """
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
        if (path.name in _COMPANION_NAMES
                and isinstance(payload, dict)
                and not _SNAPSHOT_FIELDS.intersection(payload)):
            return None
        snapshot = snapshot_from_dict(payload)
        if _symbol(snapshot.etf_symbol) != snapshot.etf_symbol:
            raise ValueError('Snapshot symbol must already be normalized')
        return snapshot
    except (OSError, ValueError) as error:
        raise SourceUnavailable(
            f'Cannot load local snapshot {path}: {error}',
        ) from error


def load_local_snapshots(directory: Path) -> dict[str, list[Snapshot]]:
    """Read flat JSON snapshots offline, grouped by symbol in date order.

    Exact same-identity copies coalesce; conflicting copies raise
    SourceUnavailable rather than selecting one arbitrarily. Known demo
    companion objects are skipped only when they contain no snapshot fields.
    Missing directories, damaged holdings and unknown JSON objects fail with
    their path and underlying cause. Empty directories yield an empty mapping.
    """
    snapshots: dict[str, dict[date, Snapshot]] = {}
    current_path = Path(directory)
    try:
        if not current_path.is_dir():
            raise ValueError('Snapshot directory does not exist or is not a '
                             'directory')
        paths = sorted(current_path.glob('*.json'))
        for current_path in paths:
            snapshot = load_local_snapshot(current_path)
            if snapshot is None:
                continue
            by_date = snapshots.setdefault(snapshot.etf_symbol, {})
            previous = by_date.get(snapshot.holdings_date)
            if previous is not None and previous != snapshot:
                raise ValueError('Conflicting snapshots for '
                                 f'{snapshot.etf_symbol} '
                                 f'{snapshot.holdings_date.isoformat()}')
            by_date[snapshot.holdings_date] = snapshot
    except (OSError, ValueError, SourceUnavailable) as error:
        raise SourceUnavailable(
            f'Cannot load local snapshot {current_path}: {error}',
        ) from error
    return {symbol: [by_date[day] for day in sorted(by_date)]
            for symbol, by_date in sorted(snapshots.items())}
