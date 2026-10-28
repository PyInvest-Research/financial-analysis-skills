"""Compute offline issuer exposures without rescaling disclosed NAV weights."""

from dataclasses import dataclass
from decimal import Decimal
from math import fsum
from typing import Literal

from .dates import DateStatus
from .directory import CompanyDirectory
from .models import Snapshot
from .portfolio import Portfolio
from .validate import ValidationResult


@dataclass(frozen=True)
class SourceShare:
    """Retain a security's TWD contribution from an ETF or direct holding."""

    source: str
    security_code: str
    amount: float


@dataclass(frozen=True)
class CompanyExposure:
    """Consolidate securities by issuer with full-portfolio weight and rows.

    occurrences counts distinct ETF symbols plus the direct source, rather
    than security rows; common and preferred shares in one ETF count once.
    """

    issuer_code: str
    name: str
    industry_code: str
    amount: float
    share: float
    sources: tuple[SourceShare, ...]
    occurrences: int


@dataclass(frozen=True)
class PositionStatus:
    """Retain each position's original value and all calculation evidence."""

    symbol: str
    kind: str
    market_value: float
    status: Literal["looked_through", "not_looked_through", "direct"]
    reason: str | None
    snapshot: Snapshot | None
    validation: ValidationResult | None
    date_status: DateStatus | None


@dataclass(frozen=True)
class LookthroughResult:
    """Separate NAV buckets from additional, potentially unknown futures.

    Tiny per-ETF NAV gaps strictly below 0.0005 are rounded to zero; the
    resulting disclosed rounding residual is not allocated to other buckets.
    """

    total_value: float
    identified_stock_value: float
    coverage: float
    companies: tuple[CompanyExposure, ...]
    unresolved_value: float
    cash_value: float
    other_value: float
    unclassified_gap_value: float
    futures: tuple[tuple[str, float | None], ...]
    not_looked_through_value: float
    positions: tuple[PositionStatus, ...]
    warnings: tuple[str, ...]
    foreign_stock_value: float = 0.0


def _record_security(
    sources: dict[str, list[SourceShare]], directory: CompanyDirectory,
    source: str, code: str, name: str, amount: float,
) -> bool:
    """Record a name-backed issuer contribution, returning False if unknown."""
    resolution = directory.resolve(code, name)
    if resolution.issuer_code is None:
        return False
    sources.setdefault(resolution.issuer_code, []).append(
        SourceShare(source, code, amount))
    return True


def _companies(
    sources: dict[str, list[SourceShare]], directory: CompanyDirectory,
    total: float,
) -> tuple[CompanyExposure, ...]:
    """Build deterministic issuer totals and retain original security rows."""
    companies: list[CompanyExposure] = []
    for code, rows in sources.items():
        company = directory.get(code)
        amount = fsum(row.amount for row in rows)
        companies.append(CompanyExposure(
            code, company.name, company.industry_code, amount,
            amount / total if total else 0.0,
            tuple(sorted(rows, key=lambda r: (r.source, r.security_code,
                                              r.amount))),
            len({row.source for row in rows}),
        ))
    return tuple(sorted(companies, key=lambda c: (-c.amount, c.issuer_code)))


def _gap(snapshot: Snapshot) -> float:
    """Return the signed non-futures NAV gap using an exact decimal boundary.

    Missing nonstock weights remain unallocated in the gap. Stock weights
    must already have passed validation; none are filled or normalized.
    """
    weights = (Decimal(str(h.weight)) for h in snapshot.holdings
               if h.kind != "futures" and h.weight is not None)
    gap = Decimal(1) - sum(weights, Decimal(0))
    return 0.0 if abs(gap) < Decimal("0.0005") else float(gap)


def compute_lookthrough(
    portfolio: Portfolio,
    snapshots: dict[str, Snapshot | None],
    validations: dict[str, ValidationResult],
    date_status: dict[str, DateStatus],
    directory: CompanyDirectory,
    reasons: dict[str, str],
) -> LookthroughResult:
    """Compute TWD exposures from validated snapshots entirely offline.

    Portfolio inputs must be parsed/validated and snapshots must have matching
    identities checked by acquisition. Failed or absent validation/snapshot
    evidence leaves the whole ETF unprocessed, preserving its denominator.
    Date statuses are retained as metadata and never change amounts. Direct
    positions have no verified names, so only exact issuer codes resolve.
    Futures sum signed notionals by contract; any unknown component leaves
    that contract's total unknown rather than reporting a partial amount.
    Inputs are not mutated and no network or file access is performed.
    """
    total = fsum(p.market_value for p in portfolio.positions)
    sources: dict[str, list[SourceShare]] = {}
    buckets: dict[str, list[float]] = {
        "unresolved": [], "cash": [], "other": [], "gap": [],
        "unprocessed": [], "foreign_stock": [],
    }
    futures: dict[str, list[float | None]] = {}
    positions: list[PositionStatus] = []
    warnings = list(portfolio.warnings)

    for position in portfolio.positions:
        symbol, value = position.symbol, position.market_value
        if position.kind == "stock":
            resolved = _record_security(sources, directory, "direct",
                                        symbol, "", value)
            reason = None if resolved else "證券未能對應發行人，保留為未辨識金額"
            if not resolved:
                buckets["unresolved"].append(value)
            positions.append(PositionStatus(
                symbol, position.kind, value, "direct", reason,
                None, None, None))
            continue

        snapshot = snapshots.get(symbol)
        validation = validations.get(symbol)
        unavailable = (snapshot is None or validation is None
                       or validation.status == "fail")
        reason = None
        if unavailable:
            default = ("未取得持股快照" if snapshot is None else
                       "缺少驗證結果" if validation is None else "持股驗證失敗")
            reason = reasons.get(symbol, default)
            buckets["unprocessed"].append(value)
        positions.append(PositionStatus(
            symbol, position.kind, value,
            "not_looked_through" if unavailable else "looked_through",
            reason, snapshot, validation, date_status.get(symbol)))
        if unavailable:
            continue

        for check in validation.checks:
            if check.status == "warn":
                warnings.append(f"{symbol}：{check.name}：{check.detail}")
        missing_weights = sum(h.weight is None and h.kind != "futures"
                              for h in snapshot.holdings)
        if missing_weights:
            sample = ", ".join(h.code for h in snapshot.holdings
                               if h.weight is None and h.kind != "futures")
            if len(sample) > 160:
                sample = sample[:160] + "…"
            warnings.append(f"{symbol}：{missing_weights} 列權重缺漏，"
                            f"金額保留於未分類差額；未知不等於零（{sample}）")
        for holding in snapshot.holdings:
            if holding.kind == "futures":
                exposure = holding.exposure_weight
                futures.setdefault(holding.code, []).append(
                    None if exposure is None else value * exposure)
                continue
            if holding.weight is None:
                continue
            amount = value * holding.weight
            if holding.kind == "derivative_nav":
                buckets["other"].append(amount)
                warnings.append(
                    f"{symbol}：{holding.name}（{holding.code}）已揭露 NAV 比重"
                    f" {holding.weight:.6%} 計入其他資產；此為帳面評價，"
                    "名目曝險未知，未穿透至個股")
            elif holding.kind == "stock":
                if not _record_security(sources, directory, symbol,
                                        holding.code, holding.name, amount):
                    buckets["unresolved"].append(amount)
            else:
                buckets[holding.kind].append(amount)
        gap = _gap(snapshot)
        buckets["gap"].append(value * gap)
        if gap < 0:
            warnings.append(f"{symbol}：未分類差額為負值（{gap:.6%}），"
                            "已保留原始金額")

    companies = _companies(sources, directory, total)
    identified = fsum(c.amount for c in companies)
    futures_totals = tuple(
        (code, None if any(v is None for v in amounts) else fsum(amounts))
        for code, amounts in sorted(futures.items())
    )
    return LookthroughResult(
        total, identified, identified / total if total else 0.0, companies,
        fsum(buckets["unresolved"]), fsum(buckets["cash"]),
        fsum(buckets["other"]), fsum(buckets["gap"]), futures_totals,
        fsum(buckets["unprocessed"]), tuple(positions), tuple(warnings),
        fsum(buckets["foreign_stock"]),
    )
