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

from .bundled import load_bundled_with_hash
from .calculate import compute_lookthrough
from .dates import classify_dates, select_dates
from .directory import CompanyDirectory
from .loader import ColumnMap, load_official_file
from .metrics import compute_metrics
from .models import Snapshot, snapshot_from_dict, snapshot_to_dict
from .portfolio import load_portfolio
from .report import build_result_json, write_outputs
from .research import (
    bundled_snapshot, qualifies_research, qualifies_published_candidate,
)
from .sources import SourceUnavailable, load_local_snapshot
from .validate import MAX_AGE_DAYS, ValidationResult, validate_snapshot


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


def run_lookthrough(
    portfolio_path: Path, out_dir: Path, *,
    local_snapshot_dir: Path | None = None,
    official_files: dict[str, tuple[Path, ColumnMap, dict]] | None = None,
    use_network: bool = True, today: date | None = None,
    bundled_dir: Path | None = None,
    base_url: str | None = None,
    opener: Callable[[str], bytes] | None = None,
    validation_policy: str = "annotated",
    ca_verified: bool = False,
) -> dict[str, Path]:
    """Write reports from explicit local inputs and packaged snapshots.

    Explicit local candidates remain first; missing symbols use verified
    bundled content. Official files can replace incomplete/stale candidates,
    followed by stale bundles, fixed research and historical estimates.
    No HTTP request is made. Legacy network/base/opener/CA keywords remain
    accepted for callers migrating from earlier releases and are not used.
    Preserve source provenance; bundle delivery is separate report metadata.
    Per-source errors fall through without shrinking portfolio value.
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
    packaged: dict[str, _Candidate] = {}
    packaged_hashes: dict[str, str] = {}
    for symbol in sorted(symbols):
        if candidates.get(symbol):
            continue
        try:
            record = load_bundled_with_hash(symbol, bundled_dir)
            if record is not None:
                snapshot = record.snapshot
                candidate = _candidate(snapshot, symbol, directory, today,
                                       policy=validation_policy)
                candidates[symbol] = {snapshot.holdings_date: candidate}
                packaged[symbol] = candidate
                packaged_hashes[symbol] = record.content_sha256
        except (SourceUnavailable, ValueError) as error:
            reasons[symbol].append(f'內建快照: {error}')
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
            needs_update = bool(_rank(selected[symbol], today)[1]) or any(
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
                    or (_rank(candidate, today),
                        -candidate.snapshot.holdings_date.toordinal())
                    <= (_rank(selected[symbol], today),
                        -selected[symbol].snapshot.holdings_date.toordinal())):
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
    delivered = {symbol for symbol, candidate in packaged.items()
                 if selected.get(symbol) is candidate}
    for symbol in sorted(delivered):
        snapshot = selected[symbol].snapshot
        age = (today - snapshot.holdings_date).days
        if age > MAX_AGE_DAYS:
            warnings.append(f'{symbol}：內建快照持股日 '
                            f'{snapshot.holdings_date}，距今 {age} 天，已過期；'
                            '請由 agent 另取官方檔；計算函式不會連網，原始日期與未知值保持不變。')
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
    payload = build_result_json(portfolio, result, metrics, generated_at)
    for row in payload['snapshots']:
        if row['symbol'] in delivered:
            row['delivery'] = 'bundled'
            row['archive_content_sha256'] = packaged_hashes[row['symbol']]
            row['source_note'] = (f"內建快照，持股日 {row['holdings_date']}。"
                                  + row.get('source_note', ''))
    by_symbol = {row['symbol']: row for row in payload['snapshots']}
    for position in payload['positions']:
        if position['symbol'] in delivered:
            position['snapshot'] = dict(by_symbol[position['symbol']])
    return write_outputs(payload, out_dir)


def _tier(snapshot: Snapshot, today: date) -> int:
    """Rank current official holdings before stale and fixed research data."""
    if qualifies_research(snapshot) or qualifies_published_candidate(snapshot):
        return 2
    return int((today - snapshot.holdings_date).days > MAX_AGE_DAYS)


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
