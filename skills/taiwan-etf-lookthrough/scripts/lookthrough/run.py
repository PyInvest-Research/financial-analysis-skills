"""Orchestrate validated acquisitions and offline exposure/report functions.

Import with PYTHONPATH=scripts from the skill directory (package=false):
from lookthrough.run import run_lookthrough
Portfolio data stays local; network calls request public ETF snapshots only.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .calculate import compute_lookthrough
from .dates import classify_dates, select_dates
from .directory import CompanyDirectory
from .loader import ColumnMap, load_official_file
from .metrics import compute_metrics
from .models import Snapshot, snapshot_from_dict, snapshot_to_dict
from .portfolio import load_portfolio
from .report import build_result_json, write_outputs
from .research import bundled_snapshot, qualifies_research
from .sources import (
    PYINVEST_BASE_URL, SourceUnavailable, fetch_index,
    fetch_pyinvest_snapshot, load_local_snapshot, open_public_json,
)
from .validate import ValidationResult, validate_snapshot


@dataclass(frozen=True)
class _Candidate:
    """Keep a usable immutable snapshot with its validation evidence."""

    snapshot: Snapshot
    validation: ValidationResult


def _candidate(snapshot: Snapshot, symbol: str,
               directory: CompanyDirectory, today: date,
               expected_date: date | None = None, *,
               policy: str = "strict") -> _Candidate:
    """Check schema, requested identity and financial usability at entry."""
    snapshot = snapshot_from_dict(snapshot_to_dict(snapshot))
    if snapshot.etf_symbol != symbol:
        raise ValueError(f'Snapshot symbol mismatch for {symbol}')
    if expected_date is not None and snapshot.holdings_date != expected_date:
        raise ValueError(f'Snapshot holdings date mismatch for {symbol}')
    validation = validate_snapshot(snapshot, directory, today, policy=policy)
    if validation.status == 'fail':
        details = '; '.join(f'{check.name}: {check.detail}'
                            for check in validation.checks
                            if check.status == 'fail')
        raise ValueError(f'Snapshot validation failed: {details}')
    return _Candidate(snapshot, validation)


def _local_candidates(
    folder: Path, symbols: set[str], directory: CompanyDirectory, today: date,
    reasons: dict[str, list[str]], warnings: list[str],
    policy: str = "annotated",
) -> dict[str, dict[date, _Candidate]]:
    """Isolate damaged files and reject ambiguous same-identity snapshots."""
    candidates: dict[str, dict[date, _Candidate]] = {}
    conflicted: set[tuple[str, date]] = set()
    kept_paths: dict[tuple[str, date], str] = {}
    try:
        if not folder.is_dir():
            raise OSError('Snapshot directory does not exist')
        paths = sorted(folder.glob('*.json'))
    except OSError as error:
        for symbol in symbols:
            reasons[symbol].append(f'local: {error}')
        return candidates
    for path in paths:
        try:
            snapshot = load_local_snapshot(path)
        except SourceUnavailable as error:
            warnings.append(f'local: {error}')
            continue
        if snapshot is None or snapshot.etf_symbol not in symbols:
            continue
        symbol, day = snapshot.etf_symbol, snapshot.holdings_date
        by_date = candidates.setdefault(symbol, {})
        try:
            candidate = _candidate(snapshot, symbol, directory, today, day,
                                   policy=policy)
            previous = by_date.get(day)
            if (symbol, day) in conflicted:
                continue
            if previous is not None and previous != candidate:
                if policy == 'strict':
                    del by_date[day]
                    conflicted.add((symbol, day))
                    raise ValueError(f'Conflicting snapshots for {day}')
                old = snapshot_to_dict(previous.snapshot)
                new = snapshot_to_dict(candidate.snapshot)
                old.pop('fetched_at')
                new.pop('fetched_at')
                newer = (datetime.fromisoformat(snapshot.fetched_at)
                         > datetime.fromisoformat(
                             previous.snapshot.fetched_at))
                if old != new:
                    discarded = (kept_paths[(symbol, day)]
                                 if newer else path.name)
                    warnings.append(f'{symbol}：同日內容不同，採最新抓取；'
                                    f'已捨棄 {discarded}')
                if not newer:
                    continue
            by_date[day] = candidate
            kept_paths[(symbol, day)] = path.name
        except ValueError as error:
            reasons[symbol].append(f'local {path.name}: {error}')
    for symbol in sorted(symbols):
        if not candidates.get(symbol):
            reasons[symbol].append('local: no usable requested ETF snapshot')
    return candidates


def _index_dates(index: dict, symbol: str) -> list[date]:
    """Validate only requested availability rows; never follow index URLs."""
    if index.get('schema_version') != '1' or not isinstance(
            index.get('etfs'), list):
        raise ValueError('index must contain schema_version 1 and etfs array')
    rows = [row for row in index['etfs']
            if isinstance(row, dict) and row.get('symbol') == symbol]
    if not rows:
        return []
    if len(rows) != 1:
        raise ValueError(f'index contains duplicate entries for {symbol}')
    values = rows[0].get('dates')
    if not isinstance(values, list):
        raise ValueError(f'index dates must be an array for {symbol}')
    days = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f'index dates must be ISO strings for {symbol}')
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError(f'index date must use YYYY-MM-DD for {symbol}')
        days.append(day)
    return sorted(set(days), reverse=True)


def _remote_candidates(
    symbols: set[str], directory: CompanyDirectory, today: date,
    reasons: dict[str, list[str]], base_url: str,
    opener: Callable[[str], bytes] | None,
    policy: str = "annotated",
) -> dict[str, dict[date, _Candidate]]:
    """Validate published candidates using an injectable transport."""
    candidates: dict[str, dict[date, _Candidate]] = {}
    if opener is None and policy == 'annotated':
        opener = open_public_json
    try:
        index = fetch_index(base_url=base_url, opener=opener)
    except SourceUnavailable as error:
        index = None
        for symbol in symbols:
            reasons[symbol].append(f'PyInvest index: {error}')
    for symbol in sorted(symbols):
        try:
            days = (_index_dates(index, symbol)
                    if index is not None else [None])
        except ValueError as error:
            reasons[symbol].append(f'PyInvest index: {error}')
            continue
        if not days:
            reasons[symbol].append('PyInvest index: no available dates')
        for day in days:
            try:
                snapshot = fetch_pyinvest_snapshot(
                    symbol, day, base_url=base_url, opener=opener)
                research_route = index is not None and any(
                    row.get('symbol') == symbol
                    and row.get('route') == 'research'
                    for row in index['etfs'] if isinstance(row, dict))
                if research_route and not qualifies_research(snapshot):
                    raise ValueError('Published research hash mismatch')
                candidate = _candidate(snapshot, symbol, directory, today, day,
                                       policy=policy)
                candidates.setdefault(symbol, {})[
                    snapshot.holdings_date] = candidate
            except (SourceUnavailable, ValueError) as error:
                reasons[symbol].append(f'PyInvest {day or "latest"}: {error}')
    return candidates


def run_lookthrough(
    portfolio_path: Path, out_dir: Path, *,
    local_snapshot_dir: Path | None = None,
    official_files: dict[str, tuple[Path, ColumnMap, dict]] | None = None,
    use_network: bool = True, today: date | None = None,
    base_url: str = PYINVEST_BASE_URL,
    opener: Callable[[str], bytes] | None = None,
    validation_policy: str = "annotated",
    ca_verified: bool = False,
) -> dict[str, Path]:
    """Write three report artifacts after local, PyInvest, official fallback.

    Strict uses its first usable source. Annotated prefers current official
    rows, then stale, fixed research and historical ratios. Local
    and published candidates align to their latest common usable date, or
    each latest date when none is common. Explicit official files keep their
    stated dates. Failed candidates never participate in date classification.
    All acquisition failures remain in unavailable-position reasons or, on
    successful fallback, report warnings. Offline mode never fetches an index
    or invokes an opener. Inputs and bundled directory files remain unchanged.

    Portfolio/schema/output errors propagate; per-source failures fall through
    without removing any portfolio value from the denominator. Live default
    strict transport requires the runtime-safety.md CA preflight. Annotated
    public JSON retains default TLS, no redirects and a CA-audit warning.
    Output I/O errors
    may leave partial artifacts; accept the mapping only on successful return.
    """
    if today is None:
        today = datetime.now(ZoneInfo('Asia/Taipei')).date()
    if type(today) is not date:
        raise ValueError('today must be a plain calendar date')
    if validation_policy not in ('strict', 'annotated'):
        raise ValueError('validation_policy must be strict or annotated')
    if type(ca_verified) is not bool:
        raise ValueError('ca_verified must be a boolean')
    portfolio = load_portfolio(portfolio_path, policy=validation_policy)
    directory = CompanyDirectory.from_file(
        Path(__file__).resolve().parent / 'data' / 'company-directory.json')
    symbols = {p.symbol for p in portfolio.positions if p.kind == 'etf'}
    reasons: dict[str, list[str]] = {symbol: [] for symbol in symbols}
    warnings: list[str] = []
    candidates = (_local_candidates(
        local_snapshot_dir, symbols, directory, today, reasons, warnings,
        validation_policy)
        if local_snapshot_dir is not None else {})
    missing = {symbol for symbol in symbols if not candidates.get(symbol)}
    if validation_policy == 'annotated':
        missing |= {symbol for symbol in symbols
                    if candidates.get(symbol) and all(
                        _rank(c, today) > (0, 0)
                        for c in candidates[symbol].values())}
    if use_network and missing:
        if validation_policy == 'annotated' and not ca_verified:
            warnings.append('TLS 信任庫未核對；僅讀 PyInvest 公開 JSON，'
                            '仍使用預設憑證驗證、exact host、禁止轉址')
        remote = _remote_candidates(
            missing, directory, today, reasons, base_url, opener,
            validation_policy)
        for symbol, values in remote.items():
            for day, candidate in values.items():
                existing = candidates.setdefault(symbol, {}).get(day)
                if (existing is None or validation_policy == 'annotated'
                        and _rank(candidate, today) < _rank(existing, today)):
                    candidates[symbol][day] = candidate
    if validation_policy == 'annotated':
        candidates = {
            symbol: {day: c for day, c in values.items()
                     if _rank(c, today) == min(
                         _rank(v, today) for v in values.values())}
            for symbol, values in candidates.items() if values}
    available = {
        symbol: list(by_date) for symbol, by_date in candidates.items()
        if by_date
    }
    selected = {
        symbol: candidates[symbol][day]
        for symbol, day in select_dates(
            available, max_rollback_weekdays=(
                5 if validation_policy == 'annotated' else None)).items()
    }
    for symbol in sorted(symbols):
        if symbol in selected:
            needs_update = any(
                c.status == 'warn' and c.name in (
                    'holdings_date_sane', 'stock_weight_sum', 'not_top_n_only',
                    'partial_equity_weights', 'NAV_denominator_qualification',
                    'research_equity_evidence', 'annotated_equity_evidence')
                for c in selected[symbol].validation.checks)
            if (not needs_update or not official_files
                    or symbol not in official_files):
                continue
        if not official_files or symbol not in official_files:
            continue
        try:
            path, columns, metadata = official_files[symbol]
            if type(metadata.get('holdings_date')) is not date:
                raise ValueError('official holdings_date must be a plain date')
            snapshot = load_official_file(path, columns,
                                          policy=validation_policy, **metadata)
            candidate = _candidate(
                snapshot, symbol, directory, today, metadata['holdings_date'],
                policy=validation_policy)
            if (validation_policy == 'strict' or symbol not in selected
                    or _rank(candidate, today)
                    <= _rank(selected[symbol], today)):
                selected[symbol] = candidate
        except (OSError, ValueError, TypeError, AttributeError) as error:
            reasons[symbol].append(f'official: {error}')
    for symbol in sorted(symbols - selected.keys()):
        try:
            snapshot = bundled_snapshot(symbol)
            if snapshot is not None:
                selected[symbol] = _candidate(
                    snapshot, symbol, directory, today,
                    policy=validation_policy)
                continue
        except ValueError as error:
            reasons[symbol].append(f'fixed research: {error}')
        reasons[symbol].append(
            'official: no usable file; agent should obtain dated holdings '
            'from verified official HTTPS sources, calculate available rows '
            'with annotations, and retain missing amounts')
    snapshots = {symbol: candidate.snapshot
                 for symbol, candidate in selected.items()}
    validations = {symbol: candidate.validation
                   for symbol, candidate in selected.items()}
    statuses = classify_dates(
        {symbol: snapshot.holdings_date
         for symbol, snapshot in snapshots.items()},
        {symbol: snapshot.is_active for symbol, snapshot in snapshots.items()},
    )
    joined_reasons = {symbol: '; '.join(details)
                      for symbol, details in reasons.items() if details}
    result = compute_lookthrough(portfolio, snapshots, validations, statuses,
                                 directory, joined_reasons,
                                 policy=validation_policy)
    warnings.extend(f'{symbol}: {joined_reasons[symbol]}'
                    for symbol in sorted(selected) if symbol in joined_reasons)
    result = replace(result, warnings=(*result.warnings, *warnings))
    metrics = compute_metrics(result, directory)
    generated_at = datetime.now(timezone.utc).isoformat()
    return write_outputs(build_result_json(portfolio, result, metrics,
                                           generated_at), out_dir)


def _tier(snapshot: Snapshot, today: date) -> int:
    """Rank current official holdings before stale and fixed research data."""
    if qualifies_research(snapshot):
        return 2
    return int((today - snapshot.holdings_date).days > 30)


def _rank(candidate: _Candidate, today: date) -> tuple[int, int]:
    """Prefer fuller evidence within a date tier, never by exposure size.

    Incomplete local sources still trigger acquisition. Explicit incomplete
    inventories and missing weights also matter when schema-2 validation
    permits a low Taiwan allocation without a stock-total warning.
    """
    snapshot = candidate.snapshot
    evidence = snapshot.equity_evidence
    incomplete = (
        evidence is not None and not evidence.complete
        or any(h.weight is None for h in snapshot.holdings
               if h.kind in ('stock', 'foreign_stock'))
        or any(c.status == 'warn' and c.name in (
            'stock_weight_sum', 'not_top_n_only', 'partial_equity_weights',
            'refresh_partial_inventory', 'no_taiwan_rows_in_source')
               for c in candidate.validation.checks))
    return _tier(snapshot, today), int(incomplete)
