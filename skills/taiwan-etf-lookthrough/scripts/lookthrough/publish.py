"""Publish validated holdings and safely retain dated snapshot history."""

import argparse
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from time import sleep
from typing import Any
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler, ProxyHandler, Request, build_opener,
)
from zoneinfo import ZoneInfo

from .directory import CompanyDirectory
from .issuers import capital, fhtrust, ftft
from .issuers import (
    ab, esun, fsitc, jpmorgan, mega, mixed, nomura, sinopac, tsit,
)
from .issuers._assets import asset_day, read_json
from .issuers.registry import PARSERS, SUPPORTED_ETFS, SupportedEtf
from .models import Snapshot, snapshot_from_dict, snapshot_to_dict
from .validate import validate_snapshot
from .refresh import RefreshSession, acquire as acquire_refresh
from .transport_evidence import TransportEvidence
from .refresh_contract import products as refresh_products
from .source_quality import parse_annotated


RETENTION_DAILY = 30
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_HOSTS = frozenset({
    "websys.fsit.com.tw", "www.yuantaetfs.com", "www.cathaysite.com.tw",
    "www.capitalfund.com.tw", "etfapi.yuantaetfs.com",
    "www.fhtrust.com.tw", "www.ftft.com.tw",
    "sitc.sinopac.com", "www.tsit.com.tw", "www.megafunds.com.tw",
    "www.nomurafunds.com.tw", "www.esunam.com", "www.fsitc.com.tw",
    "am.jpmorgan.com", "webapi.alliancebernstein.com",
    "www.kgifund.com.tw", "cwapi.cathaysite.com.tw",
})
_SYMBOL = re.compile(r"[0-9A-Z]{4,6}")
_DATED_FILE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})\.json")
_ROUTES = frozenset({"http", "playwright", "official_only", "research"})
_STATUSES = frozenset({"pass", "warn", "fail", "fetch_error", "official_only"})
Saved = tuple[Snapshot, bytes]
_refresh_session: RefreshSession | None = None
_source_transport: TransportEvidence | None = None


def _symbol(value: Any) -> str:
    """Reject unsafe or nonnormalized managed directory identifiers."""
    if not isinstance(value, str) or not _SYMBOL.fullmatch(value):
        raise ValueError("Symbol must be 4-6 uppercase ASCII letters/digits")
    return value


def _day(value: Any) -> str:
    """Return a canonical ISO calendar day or raise ValueError."""
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != (
            value):
        raise ValueError("Date must use YYYY-MM-DD")
    return value


def _timestamp(value: Any) -> str:
    """Require an ISO timestamp with an explicit UTC offset."""
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Timestamp must be an ISO UTC string")
    if datetime.fromisoformat(value).utcoffset() != timedelta(0):
        raise ValueError("Timestamp must use UTC")
    return value


def _safe_root(root: Path) -> Path:
    """Reject symlink traversal and nondirectory publication roots."""
    root = Path(os.path.abspath(root))
    for path in (root, *root.parents):
        if path.is_symlink():
            raise ValueError(f"Symlink paths are not managed: {path}")
    if root.exists() and not root.is_dir():
        raise ValueError(f"Output root is not a directory: {root}")
    return root


def _read_json(path: Path) -> Any:
    """Read a regular UTF-8 JSON file with path-specific diagnostics."""
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Expected a regular nonsymlink file")
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read {path}: {error}") from error


def _read_index(root: Path) -> dict[str, dict[str, Any]]:
    """Validate an existing index rather than silently restarting it."""
    path = root / "index.json"
    if not path.exists() and not path.is_symlink():
        return {}
    payload = _read_json(path)
    try:
        if (not isinstance(payload, dict)
                or payload.get("schema_version") != "1"
                or not isinstance(payload.get("etfs"), list)):
            raise ValueError("Unsupported index schema")
        _timestamp(payload["generated_at"])
        rows: dict[str, dict[str, Any]] = {}
        for row in payload["etfs"]:
            if not isinstance(row, dict):
                raise ValueError("Index entry must be an object")
            symbol = _symbol(row["symbol"])
            if symbol in rows:
                raise ValueError(f"Duplicate index symbol: {symbol}")
            if (not isinstance(row["issuer"], str) or not row["issuer"]
                    or type(row["is_active"]) is not bool
                    or row["route"] not in _ROUTES
                    or not isinstance(row["dates"], list)):
                raise ValueError(f"Invalid index metadata: {symbol}")
            days = [_day(day) for day in row["dates"]]
            if len(days) != len(set(days)):
                raise ValueError(f"Duplicate index dates: {symbol}")
            latest = row["latest_date"]
            if latest is not None:
                if _day(latest) not in days:
                    raise ValueError(
                        f"Latest date missing from dates: {symbol}")
            if row["route"] == "official_only" and latest is not None:
                raise ValueError("official_only latest_date must be null")
            for key in ("last_error", "source_url"):
                if row[key] is not None and not isinstance(row[key], str):
                    raise ValueError(f"Invalid {key}: {symbol}")
            if (row["last_status"] is not None
                    and row["last_status"] not in _STATUSES):
                raise ValueError(f"Invalid status: {symbol}")
            for key in ("last_attempt_at", "revised_at"):
                if row.get(key) is not None:
                    _timestamp(row[key])
            if "last_attempt_at" not in row:
                raise ValueError("Missing last_attempt_at")
            rows[symbol] = dict(row, dates=sorted(days))
        return rows
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid {path}: {error}") from error


def _read_snapshot(path: Path, symbol: str,
                   day: str | None = None) -> Saved:
    """Decode existing bytes and verify their managed path identity."""
    try:
        payload = _read_json(path)
        value = snapshot_from_dict(payload)
        day_mismatch = (day is not None
                        and value.holdings_date.isoformat() != day)
        if value.etf_symbol != symbol or day_mismatch:
            raise ValueError("Snapshot symbol/date does not match path")
        return value, path.read_bytes()
    except (OSError, ValueError) as error:
        raise ValueError(f"Invalid snapshot {path}: {error}") from error


def _content_hash(value: Snapshot) -> str:
    """Hash financial/provenance content independently of collection time."""
    payload = snapshot_to_dict(value)
    payload.pop("fetched_at")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _existing(
        root: Path, rows: dict[str, dict[str, Any]],
        directory: CompanyDirectory,
) -> tuple[dict[str, dict[str, Saved]], dict[str, Saved]]:
    """Preflight all managed files and index references before any writes."""
    dated: dict[str, dict[str, Saved]] = {}
    latest: dict[str, Saved] = {}
    if root.exists():
        for folder in sorted(root.iterdir()):
            if not _SYMBOL.fullmatch(folder.name):
                continue
            if folder.is_symlink():
                raise ValueError(f"Symlink directory is not managed: {folder}")
            if not folder.is_dir():
                raise ValueError(f"Expected a symbol directory: {folder}")
            symbol = folder.name
            dates = dated.setdefault(symbol, {})
            for path in sorted(folder.iterdir()):
                match = _DATED_FILE.fullmatch(path.name)
                if match:
                    try:
                        day = _day(match[1])
                    except ValueError as error:
                        raise ValueError(
                            f"Invalid date path: {path}") from error
                    dates[day] = _read_snapshot(path, symbol, day)
                elif path.name == "latest.json":
                    latest[symbol] = _read_snapshot(path, symbol)
            values = list(dates.values())
            if symbol in latest:
                current = latest[symbol][0]
                day = current.holdings_date.isoformat()
                if (day not in dates or _content_hash(current)
                        != _content_hash(dates[day][0])):
                    path = folder / "latest.json"
                    raise ValueError(f"{path} has no matching dated snapshot")
                values.append(latest[symbol])
            for value, _ in values:
                # Validate financial data as-of day, allowing historical age.
                result = validate_snapshot(value, directory,
                                           value.holdings_date,
                                           policy="annotated")
                if result.status == "fail":
                    raise ValueError(
                        f"Invalid historical snapshot in {folder}")
                row = rows.get(symbol)
                if row and (row["issuer"] != value.issuer
                            or row["is_active"] != value.is_active):
                    raise ValueError(
                        f"Index/snapshot identity mismatch: {folder}")
    for symbol, row in rows.items():
        if any(day not in dated.get(symbol, {}) for day in row["dates"]):
            raise ValueError(f"index.json references missing dates: {symbol}")
        if row["latest_date"] is not None and (
                symbol not in latest or row["latest_date"]
                != latest[symbol][0].holdings_date.isoformat()):
            raise ValueError(f"index.json/latest.json date mismatch: {symbol}")
    return dated, latest


def _encoded(payload: dict[str, Any]) -> bytes:
    """Encode deterministic readable JSON without nonfinite values."""
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode("utf-8")


def _atomic_write(path: Path, raw: bytes) -> None:
    """Replace one file atomically, preserving old bytes on staging failure."""
    _safe_root(path.parent)
    if path.is_symlink():
        raise ValueError(f"Refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
                delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _save_parse_failure(folder: Path, etf: SupportedEtf, raw: bytes,
                        stamp: str, error: Exception) -> dict:
    """Retain exact collector bytes outside public data for bounded repair.

    The receipt binds bytes, registry identity, acquisition time and error.
    Capture failures are recorded without discarding other valid updates.
    The 2 MiB cap matches the agent's existing per-source budget.
    """
    record = {"status": "saved", "symbol": etf.symbol,
              "source_url": etf.source_url, "fetched_at": stamp,
              "error_type": type(error).__name__,
              "untrusted_error": str(error)[:2000],
              "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if len(raw) > 2 * 1024 * 1024:
        record["status"] = "source_too_large"
        return record
    try:
        _atomic_write(folder / etf.symbol / f'{record["sha256"]}.raw', raw)
    except (OSError, ValueError) as capture_error:
        record["status"] = "capture_failed"
        record["capture_error_type"] = type(capture_error).__name__
    return record


def publish_all(out_root: Path, *, fetch: Callable[[SupportedEtf], bytes],
                today: date, fetched_at: str,
                directory: CompanyDirectory,
                symbols: set[str] | None = None) -> dict:
    """Publish validated dated/latest/index JSON, preserving good failures.

    Fetch errors and parser/schema/identity/validation failures update only
    the index attempt fields for that ETF. Warn snapshots are publishable.
    Existing corrupt data and I/O errors raise with no false success. Each
    replacement is atomic; the entire multi-file publication is not a single
    filesystem transaction. No retention or network policy is imposed on an
    explicitly injected fetch callable.
    """
    return _publish_all(out_root, fetch=fetch, today=today,
                        fetched_at=fetched_at, directory=directory,
                        write=True, observations=[], selected=symbols)


def _publish_all(
        out_root: Path, *, fetch: Callable[[SupportedEtf], bytes], today: date,
        fetched_at: str, directory: CompanyDirectory, write: bool,
        observations: list[tuple[str, str, str | None, float | None]],
        selected: set[str] | None = None,
        failure_root: Path | None = None,
        failure_records: dict | None = None,
) -> dict:
    """Share real acquisition/validation between writing and read-only runs."""
    global _refresh_session
    if fetch is fetch_official:
        _refresh_session = None
    root = _safe_root(out_root)
    _timestamp(fetched_at)
    rows = _read_index(root)
    dated, latest = _existing(root, rows, directory)
    registry = sorted(SUPPORTED_ETFS, key=lambda row: row.symbol)
    if selected is not None:
        if (not isinstance(selected, set) or not selected
                or not selected <= {row.symbol for row in registry}):
            raise ValueError("Selected publication requires known symbols")
        registry = [row for row in registry if row.symbol in selected]
    symbols: set[str] = set()
    for etf in registry:
        symbol = _symbol(etf.symbol)
        if symbol in symbols or etf.route not in _ROUTES:
            raise ValueError(f"Duplicate symbol or invalid route: {symbol}")
        symbols.add(symbol)
    for etf in registry:
        symbol = etf.symbol
        dates = dated.setdefault(symbol, {})
        old_latest = latest.get(symbol)
        previous_row = rows.get(symbol)
        row = dict(previous_row or {}, symbol=symbol, issuer=etf.issuer,
                   is_active=etf.is_active, route=etf.route,
                   latest_date=(old_latest[0].holdings_date.isoformat()
                                if old_latest else None),
                   dates=sorted(dates), last_status="official_only",
                   last_error=None, last_attempt_at=fetched_at,
                   source_url=etf.source_url)
        rows[symbol] = row
        if etf.route == "official_only":
            row["latest_date"] = None
            observations.append((symbol, "official_only", None, None))
            continue
        try:
            raw = fetch(etf)
        except Exception as error:
            row.update(previous_row or {}, last_status="fetch_error",
                       last_error=f"{type(error).__name__}: {error}",
                       last_attempt_at=fetched_at)
            observations.append((symbol, "fetch_error", None, None))
            continue
        observed_day: str | None = None
        stock_sum: float | None = None
        try:
            parsed = parse_annotated(PARSERS[etf.issuer], raw, etf, fetched_at)
            value = snapshot_from_dict(snapshot_to_dict(parsed))
            if (value.etf_symbol != symbol or value.issuer != etf.issuer
                    or value.is_active != etf.is_active):
                raise ValueError(
                    "Parsed snapshot identity differs from registry")
            observed_day = value.holdings_date.isoformat()
            weights = [h.weight for h in value.holdings if h.kind == "stock"]
            stock_sum = (sum(weights) if all(w is not None for w in weights)
                         else None)
            result = validate_snapshot(value, directory, today,
                                       policy="annotated")
            if result.status == "fail":
                detail = "; ".join(f"{check.name}: {check.detail}"
                                   for check in result.checks
                                   if check.status == "fail")
                raise ValueError(detail)
        except Exception as error:
            row.update(previous_row or {}, last_status="fail",
                       last_error=f"{type(error).__name__}: {error}",
                       last_attempt_at=fetched_at)
            if failure_root is not None and failure_records is not None:
                failure_records[symbol] = _save_parse_failure(
                    failure_root, etf, raw, fetched_at, error)
            observations.append((symbol, "fail", observed_day, stock_sum))
            continue
        row["last_status"] = result.status
        previous = dates.get(observed_day)
        same = (previous is not None and _content_hash(previous[0])
                == _content_hash(value))
        if same:
            saved = previous
        else:
            saved = (value, _encoded(snapshot_to_dict(value)))
            if previous is not None:
                row["revised_at"] = fetched_at
            if write:
                _atomic_write(root / symbol / f"{observed_day}.json", saved[1])
        dates[observed_day] = saved
        newest = dates[max(dates)]
        if (old_latest is None or newest[0].holdings_date
                > old_latest[0].holdings_date
                or (newest[0].holdings_date == old_latest[0].holdings_date
                    and _content_hash(newest[0])
                    != _content_hash(old_latest[0]))):
            if write:
                _atomic_write(root / symbol / "latest.json", newest[1])
        row.update(latest_date=newest[0].holdings_date.isoformat(),
                   dates=sorted(dates))
        observations.append((symbol, result.status, observed_day, stock_sum))
    index = {"schema_version": "1", "generated_at": fetched_at,
             "etfs": [rows[symbol] for symbol in sorted(rows)]}
    if write:
        _atomic_write(root / "index.json", _encoded(index))
    return index


def _validate_pins(pinned: set[tuple[str, str]]) -> None:
    """Validate all pins before any destructive filesystem operation."""
    if not isinstance(pinned, set):
        raise ValueError("Pinned pairs must be a set")
    for pair in pinned:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("Each pin must be a (symbol, YYYY-MM-DD) pair")
        _symbol(pair[0])
        _day(pair[1])


def _read_pins(root: Path) -> set[tuple[str, str]]:
    """Read pinned.json as explicit [symbol, YYYY-MM-DD] JSON pairs."""
    path = root / "pinned.json"
    if not path.exists() and not path.is_symlink():
        return set()
    payload = _read_json(path)
    try:
        if (not isinstance(payload, dict) or set(payload) != {"pinned"}
                or not isinstance(payload["pinned"], list)):
            raise ValueError("Expected an object containing a pinned array")
        pairs = payload["pinned"]
        if any(not isinstance(pair, list) or len(pair) != 2 for pair in pairs):
            raise ValueError("Each pin must be a two-element JSON array")
        pinned = {tuple(pair) for pair in pairs}
        _validate_pins(pinned)
        return pinned
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid {path}: {error}") from error


def prune(out_root: Path, pinned: set[tuple[str, str]],
          keep_daily: int = RETENTION_DAILY) -> list[Path]:
    """Delete only matching managed dated snapshots outside retention.

    Retain the newest keep_daily snapshots, each calendar month's last
    available snapshot and every explicit pin, independently per symbol.
    Unknown, malformed, mismatched or symlink files/directories are ignored.
    Never modify latest/index/pinned. I/O errors propagate, and deletion is
    not transactional. Callers must serialize concurrent publication/pruning.
    """
    _validate_pins(pinned)
    if type(keep_daily) is not int or keep_daily < 0:
        raise ValueError("keep_daily must be a nonnegative integer")
    root = _safe_root(out_root)
    if not root.exists():
        return []
    # Validate the saved policy even when the caller supplies explicit pins.
    pinned = pinned | _read_pins(root)
    directory = CompanyDirectory([], date.today().isoformat())
    candidates: list[Path] = []
    for folder in sorted(root.iterdir()):
        if (not _SYMBOL.fullmatch(folder.name) or folder.is_symlink()
                or not folder.is_dir()):
            continue
        dates: dict[str, Path] = {}
        for path in sorted(folder.iterdir()):
            match = _DATED_FILE.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            try:
                day = _day(match[1])
                value, _ = _read_snapshot(path, folder.name, day)
                if validate_snapshot(value, directory,
                                     value.holdings_date,
                                     policy="annotated").status == "fail":
                    continue
            except ValueError:
                continue
            dates[day] = path
        ordered = sorted(dates)
        months = {day[:7]: day for day in ordered}
        keep = set(ordered[-keep_daily:]) if keep_daily else set()
        keep.update(months.values())
        keep.update(day for symbol, day in pinned if symbol == folder.name)
        candidates.extend(dates[day] for day in ordered if day not in keep)
    deleted: list[Path] = []
    for path in sorted(candidates):
        # Recheck the path immediately before unlink; never follow symlinks.
        _safe_root(path.parent)
        if path.is_symlink() or not path.is_file():
            continue
        value, _ = _read_snapshot(path, path.parent.name, path.stem)
        if validate_snapshot(value, directory,
                             value.holdings_date,
                             policy="annotated").status == "fail":
            continue
        path.unlink()
        deleted.append(path)
    return deleted


def _check_url(url: str, host: str | None = None) -> str:
    """Restrict requests and redirects to verified official HTTPS hosts."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in _HOSTS
            or (host is not None and parsed.hostname != host)
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.fragment
            or any(ord(character) < 32 for character in url)):
        raise ValueError("URL is not an approved official HTTPS endpoint")
    return parsed.hostname


class _OfficialRedirect(HTTPRedirectHandler):
    """Validate every redirect before urllib issues a subsequent request."""

    def __init__(self, host: str) -> None:
        """Bind redirects to the original official request host."""
        super().__init__()
        self.host = host

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        """Reject cross-host destinations and insecure redirect downgrades."""
        if req.get_method() == "POST":
            raise ValueError("Official POST redirects are forbidden")
        _check_url(newurl, self.host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _capital_post(opener: Any, etf: SupportedEtf,
                  fund_id: str, query_day: str | None) -> bytes:
    """Read one bounded POST without cookies, redirects, retry or fallback."""
    request = Request(
        etf.source_url,
        data=json.dumps({"fundId": fund_id, "date": query_day}).encode(),
        headers={"User-Agent": "PyInvest-ETF-Lookthrough/1.5.4",
                 "Content-Type": "application/json"}, method="POST",
    )
    def check(response):
        _check_url(response.geturl(), "www.capitalfund.com.tw")
        if (response.geturl() != capital.SOURCE_URL
                or response.getcode() != 200):
            raise ValueError("Capital POST changed endpoint or HTTP status")
    return (_source_transport or TransportEvidence()).read(
        opener, request, timeout=30, limit=MAX_RESPONSE_BYTES, check_response=check)


def _fetch_capital(etf: SupportedEtf) -> bytes:
    """Discover date1 then request that explicit PCF day, two seconds later.

    Only the second raw response reaches the existing full parser. Dates
    and identity must agree across requests. date2 never becomes a query.
    No today inference, date rollback, retries or alternate endpoint occurs.
    """
    _, fund_id = capital._request_identity(etf)
    opener = build_opener(ProxyHandler({}),
                          _OfficialRedirect("www.capitalfund.com.tw"))
    discovery = _capital_post(opener, etf, fund_id, None)
    _, _, holdings_day, publication = capital._context(discovery, etf)
    sleep(2)
    raw = _capital_post(opener, etf, fund_id, publication.strftime("%Y/%m/%d"))
    _, _, actual_day, actual_publication = capital._context(raw, etf)
    if (actual_day, actual_publication) != (holdings_day, publication):
        raise ValueError("Capital POST responses disagree on source dates")
    return raw


class _NoSourceRedirect(_OfficialRedirect):
    """Stop new source collectors before following any HTTP redirect."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        """Require explicit route review rather than following metadata."""
        raise ValueError("Source discovery redirects are forbidden")


def _source_request(opener: Any, url: str, host: str,
                    payload: dict | None = None) -> bytes:
    """Read one exact qualified URL with verified TLS and fixed limits."""
    _check_url(url, host)
    headers = {"User-Agent": "PyInvest-ETF-Lookthrough/1.5.4"}
    data = None
    if payload is not None:
        data = json.dumps(payload, allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(url, data=data, headers=headers)
    def check(response):
        if response.geturl() != url:
            raise ValueError("Source response URL changed")
    return (_source_transport or TransportEvidence()).read(
        opener, request, timeout=30, limit=MAX_RESPONSE_BYTES, check_response=check)


def _source_get(opener: Any, url: str, host: str) -> bytes:
    """Read one exact qualified GET URL with fixed transport limits."""
    return _source_request(opener, url, host)


def _fetch_new_source(etf: SupportedEtf) -> bytes:
    """Acquire two bounded responses with a source-derived current date."""
    expected = (fhtrust.SOURCE_URL if etf.issuer == "fhtrust"
                else ftft.SOURCE_URL)
    symbol = "00929" if etf.issuer == "fhtrust" else "00905"
    if etf.source_url != expected or etf.symbol != symbol or etf.is_active:
        raise ValueError("Unqualified source request identity/URL")
    host = _check_url(expected)
    opener = build_opener(ProxyHandler({}), _NoSourceRedirect(host))
    first = _source_get(opener, expected, host)
    if etf.issuer == "fhtrust":
        info = fhtrust.inspect_page(first)
        workbook = _source_get(opener, info.download_url, host)
        return fhtrust.capture(first, workbook)
    day = ftft.latest_day(first)
    raw = _source_get(opener, f"{ftft.SHARES_URL}?date={day:%Y%m%d}", host)
    payload = read_json(raw)
    if (not isinstance(payload, dict)
            or asset_day(payload.get("AssetDate")) != day):
        raise ValueError("FTFT requested date differs from returned assets")
    return raw


_EXPANSION = {
    "sinopac": (sinopac, "00888", False),
    "tsit": (tsit, "00936", False),
    "mega": (mega, "00932", False),
    "nomura": (nomura, "00935", False),
    "esun": (esun, "009803", False),
    "fsitc": (fsitc, "00728", False),
    "jpmorgan": (jpmorgan, "00401A", True),
    "ab": (ab, "00404A", True),
}


def _fetch_expansion(etf: SupportedEtf) -> bytes:
    """Collect reviewed public routes without cookies, tokens or retries.

    Every request has a fixed source host/path and a ten-MiB cap. All new
    GET/POST redirects fail before following them. Response dates, never
    the request clock, determine the snapshot holdings day.
    """
    module, symbol, active = _EXPANSION[etf.issuer]
    if etf.issuer == "nomura":
        nomura.validate_request(etf)
    elif (etf.symbol != symbol or etf.is_active != active
          or etf.source_url != module.SOURCE_URL):
        raise ValueError("Unqualified expansion source identity/URL")
    host = _check_url(etf.source_url)
    opener = build_opener(ProxyHandler({}), _NoSourceRedirect(host))
    if etf.issuer in ("sinopac", "tsit", "mega"):
        return _source_get(opener, etf.source_url, host)
    if etf.issuer == "fsitc":
        return _source_request(opener, etf.source_url, host,
                               {"pStrFundID": "D90", "pStrDate": ""})
    if etf.issuer == "nomura":
        # The source UI falls back to the local day when selecting latest PCF.
        # This is only a query parameter; CNavDt remains the holdings date.
        today = datetime.now(ZoneInfo("Asia/Taipei")).date()
        publication = today.isoformat()
        pcf = _source_request(opener, nomura.PCF_URL, host,
                              {"Type": 1, "Keyword": "", "FundNo": etf.symbol,
                               "Date": publication})
        day = nomura.pcf_valuation_day(pcf, etf, today)
        assets = _source_request(opener, etf.source_url, host,
                                 {"FundID": etf.symbol, "SearchDate": day.isoformat()})
        return nomura.capture(assets, pcf)
    if etf.issuer == "esun":
        overview = _source_request(opener, esun.OVERVIEW_URL, host, {
            "PageSize": 999, "PageIndex": 1, "SortColName": "",
            "ETFInvestmentIDs": [], "ETFFundTypeIDs": [],
            "ETFFundDividendIDs": [], "ETFInvestAreaIDs": [],
            "IsDesc": False,
        })
        assets = _source_request(opener, etf.source_url, host,
                                 {"FundID": "50"})
        return esun.capture(assets, overview)
    first = _source_get(opener, etf.source_url, host)
    if etf.issuer == "jpmorgan":
        day = jpmorgan.latest_day(first)
        raw = _source_get(opener, jpmorgan.DOWNLOAD_URL +
                          day.isoformat(), host)
        if jpmorgan.parse(raw, etf, _utc_now()).holdings_date != day:
            raise ValueError(
                "JPMorgan requested/returned holdings day differs")
        return raw
    day = ab.holdings_day(first)
    # A dated basket is the creation basket, with previous-day NAV.
    # Only the latest basket was evidenced to disclose actual same-day NAV.
    basket = _source_get(opener, ab.NAV_URL, host)
    payload = read_json(basket)
    if (not isinstance(payload, dict)
            or payload.get("navAsOfDate") != day.isoformat()):
        raise ValueError("AB requested/returned NAV day differs")
    return ab.capture(first, basket)


def fetch_official(etf: SupportedEtf) -> bytes:
    """Fetch an HTTP route with verified TLS, 30s timeout and 10MiB cap.

    Before live use, audit the actual runtime CA according to
    references/runtime-safety.md. No proxy environment, cookies, credentials,
    browser state, script execution or TLS bypass is used. Browser routes
    fail explicitly until a verified transport exists.
    """
    global _refresh_session
    if etf.route != "http":
        raise ValueError(f"Unsupported acquisition route: {etf.route}")
    if etf.symbol in refresh_products():
        if etf.source_url != refresh_products()[etf.symbol]["source_url"]:
            raise ValueError("Unqualified refresh source URL")
        if _refresh_session is None:
            _refresh_session = RefreshSession(transport=_source_transport)
        return acquire_refresh(etf.symbol, _refresh_session)
    if etf.symbol in mixed.PRODUCTS:
        return _fetch_mixed(etf)
    if etf.issuer in ("fhtrust", "ftft"):
        return _fetch_new_source(etf)
    if etf.issuer in _EXPANSION:
        return _fetch_expansion(etf)
    host = _check_url(etf.source_url)
    if etf.issuer == "capital":
        return _fetch_capital(etf)
    opener = build_opener(ProxyHandler({}), _OfficialRedirect(host))
    request = Request(etf.source_url, headers={
        "User-Agent": "PyInvest-ETF-Lookthrough/1.5.4",
    })
    def check(response):
        _check_url(response.geturl(), host)
    return (_source_transport or TransportEvidence()).read(
        opener, request, timeout=30, limit=MAX_RESPONSE_BYTES, check_response=check)


def _fetch_mixed(etf: SupportedEtf) -> bytes:
    """Read one HTML or four Cathay responses, with no retry/redirect."""
    if (etf.is_active or (etf.issuer, etf.source_url)
            != mixed.PRODUCTS[etf.symbol]):
        raise ValueError("Unqualified mixed source descriptor")
    host = _check_url(etf.source_url)
    opener = build_opener(ProxyHandler({}), _NoSourceRedirect(host))
    page = _source_get(opener, etf.source_url, host)
    if etf.symbol != "00735":
        return page
    host = _check_url(mixed.API)
    opener = build_opener(ProxyHandler({}), _NoSourceRedirect(host))
    assets = _source_get(opener, mixed.API + 'GetETFAssets?FundCode=AW', host)
    day = mixed.assets_day(assets)
    stocks = _source_get(opener, mixed.API +
                         f'GetETFDetailStockList?FundCode=AW&SearchDate={day}',
                         host)
    balance = _source_get(opener, mixed.API +
                          f'GetETFDetailBalList?FundCode=AW&SearchDate={day}',
                          host)
    return mixed.capture_cathay(page, assets, stocks, balance,
                                requested_date=day)


def _utc_now() -> str:
    """Return the collection timestamp in UTC."""
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    """Run publication/retention or a no-write fetch/parse/validation dry-run.

    Run with scripts on PYTHONPATH, or from scripts/, for this package=false
    skill. Return 1 on required route failure, corrupt existing data or I/O
    errors. Explicit partial mode permits per-ETF failures only after all
    writes finish and a fresh external completion report is saved. Fatal
    errors still return 1. A dry-run computes proposed changes in
    memory; it creates no output directories, temporary snapshots or index.
    """
    global _refresh_session, _source_transport
    _refresh_session = None
    _source_transport = None
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--symbols", nargs="+", help="Only reviewed symbols")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Save valid updates despite per-ETF failures")
    parser.add_argument("--run-report", type=Path,
                        help="Fresh external JSON completion report path")
    args = parser.parse_args(argv)
    try:
        root = _safe_root(args.out)
        report_path = None
        failure_root = None
        failure_records: dict = {}
        if args.allow_partial and args.run_report is None:
            raise ValueError("Partial publication requires a run report")
        if args.run_report is not None:
            if args.dry_run:
                raise ValueError("A dry-run cannot emit a completion report")
            report_path = Path(os.path.abspath(args.run_report))
            _safe_root(report_path.parent)
            if (report_path == root or root in report_path.parents
                    or report_path.exists() or report_path.is_symlink()):
                raise ValueError("Run report must be fresh and outside data")
            failure_root = report_path.with_suffix(".parse-failures")
            _safe_root(failure_root)
            if (failure_root == root or root in failure_root.parents
                    or failure_root.exists()):
                raise ValueError("Parse evidence must be fresh outside data")
            _source_transport = TransportEvidence(report_path.with_suffix(".transport"))
        pins = _read_pins(root)
        directory = CompanyDirectory.from_file(
            Path(__file__).parent / "data" / "company-directory.json")
        stamp = _utc_now()
        today = datetime.fromisoformat(stamp).astimezone(
            ZoneInfo("Asia/Taipei")).date()
        observations: list[tuple[str, str, str | None, float | None]] = []
        index = _publish_all(
            root, fetch=fetch_official, today=today, fetched_at=stamp,
            directory=directory, write=not args.dry_run,
            observations=observations,
            failure_root=failure_root, failure_records=failure_records,
            selected=set(args.symbols) if args.symbols else None)
        for symbol, status, day, stock_sum in observations:
            total = "unknown" if stock_sum is None else f"{stock_sum:.8f}"
            print(f"{symbol} {status} {day or '-'} {total}")
            if status in ("fail", "fetch_error"):
                row = next(row for row in index["etfs"]
                           if row["symbol"] == symbol)
                print(json.dumps({key: row[key] for key in (
                    "symbol", "last_status", "last_error")},
                    ensure_ascii=True), file=sys.stderr)
        failed = any(status in ("fail", "fetch_error")
                     for _, status, _, _ in observations)
        if not args.dry_run and not failed:
            deleted = prune(root, pins)
            if deleted:
                for row in index["etfs"]:
                    row["dates"] = [day for day in row["dates"]
                                    if (root / row["symbol"]
                                        / f"{day}.json").is_file()]
                _atomic_write(root / "index.json", _encoded(index))
        if report_path is not None:
            counts = dict.fromkeys(sorted(_STATUSES), 0)
            for _, status, _, _ in observations:
                counts[status] += 1
            _atomic_write(report_path, _encoded({
                "completed": True, "partial": failed,
                "generated_at": stamp, "counts": counts,
                "parse_failures": failure_records,
                "source_denials": (_source_transport.denials.records
                                   if _source_transport else []),
                "failed_symbols": [symbol for symbol, status, _, _
                                   in observations
                                   if status in ("fail", "fetch_error")],
                "index_sha256": hashlib.sha256(
                    (root / "index.json").read_bytes()).hexdigest(),
            }))
        return int(failed and not args.allow_partial)
    except (OSError, ValueError) as error:
        print(f"Publication failed: {error}", file=sys.stderr)
        return 1
    finally:
        _source_transport = None
        _refresh_session = None


if __name__ == "__main__":
    raise SystemExit(main())
