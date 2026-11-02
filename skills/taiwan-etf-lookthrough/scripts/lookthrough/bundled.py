"""Read content-bound packaged snapshots without any network operations."""

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re

from .models import Snapshot, snapshot_from_dict
from .sources import SourceUnavailable, _symbol
from .research import publication_catalog, qualifies_published_candidate


BUNDLED_DIR = Path(__file__).parent / 'data/snapshots'
MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class BundledSnapshot:
    """Keep validated raw content identity beside its normalized model."""

    snapshot: Snapshot
    content_sha256: str


def content_sha256(payload: dict) -> str:
    """Match archive content identity, excluding only acquisition time."""
    fields = {key: value for key, value in payload.items()
              if key != 'fetched_at'}
    return hashlib.sha256(json.dumps(
        fields, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _unique(items: list[tuple]) -> dict:
    """Reject ambiguous duplicate JSON keys."""
    if len(dict(items)) != len(items):
        raise ValueError('Duplicate bundle JSON key')
    return dict(items)


def _read(path: Path) -> dict:
    """Read one bounded nonsymlink JSON object or raise SourceUnavailable."""
    path = Path(os.path.abspath(path))
    try:
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError('Bundle paths must not contain symlinks')
        with path.open('rb') as stream:
            raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError('Bundle JSON exceeds size limit')
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique)
        if not isinstance(value, dict):
            raise ValueError('Bundle JSON must be an object')
        return value
    except (OSError, ValueError, UnicodeError) as error:
        raise SourceUnavailable(
            f'Cannot read bundle {path}: {error}') from error


def _index(root: Path, requested: str | None = None) -> dict[str, dict]:
    """Validate the fixed index contract without following metadata paths."""
    path = root / 'index.json'
    if not path.exists() and not path.is_symlink():
        return {}
    value = _read(path)
    if (value.get('schema_version') != '1'
            or value.get('source') != 'gcs:etf-holdings/v1'
            or not isinstance(value.get('etfs'), list)):
        raise SourceUnavailable('Invalid bundle index schema')
    rows = {}
    try:
        for row in value['etfs']:
            if requested is not None and (
                    not isinstance(row, dict)
                    or row.get('symbol') != requested):
                continue
            code = _symbol(row['symbol'])
            if (code != row['symbol'] or code in rows
                    or date.fromisoformat(row['holdings_date']).isoformat()
                    != row['holdings_date']
                    or not re.fullmatch('[0-9a-f]{64}',
                                        row['snapshot_sha256'])):
                raise ValueError('Invalid or duplicate bundle identity')
            rows[code] = row
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise SourceUnavailable(f'Invalid bundle index: {error}') from error
    return rows


def bundled_index(bundled_dir: Path | None = None) -> dict[str, str]:
    """Return the packaged ETF dates; missing initial bundles are empty."""
    rows = _index(Path(bundled_dir)
                  if bundled_dir is not None else BUNDLED_DIR)
    return {code: row['holdings_date'] for code, row in rows.items()}


def load_bundled_with_hash(symbol: str, bundled_dir: Path | None = None,
                           ) -> BundledSnapshot | None:
    """Verify one indexed snapshot, preserving provenance and original time.

    Missing symbols return None. Invalid JSON, identity, dates, hashes and
    managed paths raise SourceUnavailable for per-ETF fallback handling.
    """
    code = _symbol(symbol)
    root = Path(bundled_dir) if bundled_dir is not None else BUNDLED_DIR
    row = _index(root, code).get(code)
    if row is None:
        return None
    payload = _read(root / f'{code}.json')
    try:
        snapshot = snapshot_from_dict(payload)
        if (snapshot.etf_symbol != code
                or snapshot.holdings_date.isoformat() != row['holdings_date']
                or content_sha256(payload) != row['snapshot_sha256']
                or row.get('source_kind', snapshot.source_kind)
                != snapshot.source_kind):
            raise ValueError('Bundle content hash or identity mismatch')
        reviewed = publication_catalog().get(code)
        if (reviewed and row['holdings_date'] == reviewed['holdings_date']
                and not qualifies_published_candidate(snapshot)):
            raise ValueError('Reviewed fixed snapshot content differs')
        return BundledSnapshot(snapshot, row['snapshot_sha256'])
    except (ValueError, TypeError) as error:
        raise SourceUnavailable(f'Invalid bundled {code}: {error}') from error


def load_bundled(symbol: str, bundled_dir: Path | None = None,
                 ) -> Snapshot | None:
    """Load the verified model for callers that do not need raw identity."""
    record = load_bundled_with_hash(symbol, bundled_dir)
    return record.snapshot if record is not None else None
