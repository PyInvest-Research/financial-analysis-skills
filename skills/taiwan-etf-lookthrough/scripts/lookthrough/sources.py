"""Acquire published or flat local snapshots without trusting metadata URLs."""

from datetime import date
from http.client import HTTPException
import json
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import Snapshot, snapshot_from_dict


PYINVEST_BASE_URL = 'https://pyecontech.com/data/etf-holdings/v1'
_COMPANION_NAMES = frozenset({'portfolio.json', 'expected-result.json'})
_SNAPSHOT_FIELDS = frozenset({'etf_symbol', 'holdings_date', 'holdings'})


class SourceUnavailable(RuntimeError):
    """Signal acquisition, decoding, schema or snapshot identity failure."""


def _check_pyinvest_url(url: str) -> None:
    """Restrict real requests and redirects to first-party snapshot paths."""
    parsed = urlsplit(url)
    root_path = urlsplit(PYINVEST_BASE_URL).path
    if (parsed.scheme != 'https' or parsed.hostname != 'pyecontech.com'
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment
            or not (parsed.path == root_path
                    or parsed.path.startswith(root_path + '/'))
            or not re.fullmatch(r'[A-Za-z0-9/._-]+', parsed.path)
            or any(part in ('.', '..') for part in parsed.path.split('/'))):
        raise ValueError('URL is not an approved PyInvest snapshot endpoint')


class _PyInvestRedirect(HTTPRedirectHandler):
    """Check each redirect before urllib sends its next request."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        """Permit only first-party HTTPS snapshot destinations."""
        _check_pyinvest_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url: str) -> bytes:
    """Read with verified default TLS and a twenty-second timeout.

    Operators must verify their interpreter CA store according to
    references/runtime-safety.md before live acquisition. No custom SSL
    context, credentials or source_url metadata is used here.
    """
    _check_pyinvest_url(url)
    transport = build_opener(_PyInvestRedirect())
    with transport.open(url, timeout=20) as response:
        _check_pyinvest_url(response.geturl())
        return response.read()


def _read_json(url: str, opener: Callable[[str], bytes] | None) -> Any:
    """Decode UTF-8 JSON and wrap transport or serialization errors."""
    try:
        raw = (opener or _open_url)(url)
        return json.loads(raw.decode('utf-8-sig'))
    except (OSError, HTTPException, ValueError, TypeError,
            AttributeError) as error:
        raise SourceUnavailable(f'Cannot read {url}: {error}') from error


def fetch_index(base_url: str = PYINVEST_BASE_URL,
                opener: Callable[[str], bytes] | None = None) -> dict:
    """Fetch an index object, raising SourceUnavailable on read failure.

    Injected openers receive the constructed URL and may support offline fake
    bases. Index contents never authorize additional network destinations.
    """
    payload = _read_json(base_url.rstrip('/') + '/index.json', opener)
    if not isinstance(payload, dict):
        raise SourceUnavailable('Index must be a JSON object')
    return payload


def _symbol(value: str) -> str:
    """Normalize portfolio-compatible symbols without permitting URL syntax."""
    normalized = value.strip().upper() if isinstance(value, str) else ''
    if not re.fullmatch(r'[0-9A-Z]{4,6}', normalized):
        raise SourceUnavailable('Snapshot symbol must be 4-6 ASCII characters')
    return normalized


def fetch_pyinvest_snapshot(
        symbol: str, holdings_date: date | None = None, *,
        base_url: str = PYINVEST_BASE_URL,
        opener: Callable[[str], bytes] | None = None) -> Snapshot:
    """Fetch latest or dated holdings and verify the requested identity.

    Preserve serialized source_kind and provenance for stable content hashes.
    HTTP, timeout, encoding, JSON and schema errors raise SourceUnavailable.
    Financial validation remains the caller's separate responsibility.
    """
    symbol = _symbol(symbol)
    if holdings_date is not None and type(holdings_date) is not date:
        raise SourceUnavailable('Requested holdings date must be a date')
    filename = (holdings_date.isoformat() if holdings_date else 'latest')
    url = f"{base_url.rstrip('/')}/{symbol}/{filename}.json"
    payload = _read_json(url, opener)
    try:
        snapshot = snapshot_from_dict(payload)
    except ValueError as error:
        message = f'Invalid snapshot at {url}: {error}'
        raise SourceUnavailable(message) from error
    if snapshot.etf_symbol != symbol:
        raise SourceUnavailable(f'Snapshot symbol mismatch at {url}')
    if holdings_date is not None and snapshot.holdings_date != holdings_date:
        raise SourceUnavailable(f'Snapshot holdings date mismatch at {url}')
    return snapshot


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
