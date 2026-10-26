"""Validate disclosed holdings without filling or rescaling missing data."""

from collections import Counter
from dataclasses import dataclass
from datetime import date
import math
from typing import Literal

from .directory import CompanyDirectory
from .models import Snapshot


MAX_STOCK_WEIGHT_SUM = 1.005
MIN_STOCK_WEIGHT_SUM = 0.5
WARN_STOCK_WEIGHT_SUM = 0.9
TOP_N_TRAP_COUNT = 10
MAX_AGE_DAYS = 30
UNRESOLVED_WARN_WEIGHT = 0.01
Status = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    """Record one named rule's severity and human-readable evidence."""

    name: str
    status: Status
    detail: str


@dataclass(frozen=True)
class ValidationResult:
    """Collect ordered checks and their most severe status."""

    status: Status
    checks: tuple[Check, ...]


def _finite_number(value: object) -> bool:
    """Recognize finite built-in numeric weights, excluding booleans."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_snapshot(s: Snapshot, directory: CompanyDirectory,
                      today: date) -> ValidationResult:
    """Check NAV weights, coverage, dates and mandatory source identity.

    Cash/other NAV weights may be signed within [-1, 1]; stock/futures NAV
    weights remain within [0, 1]. Missing nonstock NAV weights stay None.
    Futures notional exposure may be signed and exceed one; only its
    numeric finiteness is checked.
    Directly constructed models can contain invalid stock weights, which
    fail both range and total checks instead of bypassing comparisons.
    No network, input mutation or weight normalization is performed.
    """
    stocks = tuple(h for h in s.holdings if h.kind == "stock")
    invalid_rows = []
    for i, holding in enumerate(s.holdings):
        weight = holding.weight
        lower_bound = -1 if holding.kind in ("cash", "other") else 0
        if ((weight is None and holding.kind == "stock")
                or (weight is not None and (
                    not _finite_number(weight)
                    or not lower_bound <= weight <= 1))
                or (holding.exposure_weight is not None
                    and not _finite_number(holding.exposure_weight))):
            invalid_rows.append(i + 1)
    checks = [Check(
        "weights_in_range", "fail" if invalid_rows else "pass",
        f"Invalid numeric/NAV weights at rows: {invalid_rows}",
    )]

    valid_stock_weights = all(_finite_number(h.weight) for h in stocks)
    stock_total = sum(
        h.weight for h in stocks) if valid_stock_weights else None
    sum_status: Status = "pass"
    if (stock_total is None or not _finite_number(stock_total)
            or stock_total > MAX_STOCK_WEIGHT_SUM
            or stock_total < MIN_STOCK_WEIGHT_SUM):
        sum_status = "fail"
    elif stock_total < WARN_STOCK_WEIGHT_SUM:
        sum_status = "warn"
    checks.append(Check("stock_weight_sum", sum_status,
                        f"Stock NAV weight total: {stock_total}"))
    top_n_fail = (len(stocks) <= TOP_N_TRAP_COUNT
                  and (stock_total is None
                       or not _finite_number(stock_total)
                       or stock_total < WARN_STOCK_WEIGHT_SUM))
    checks.append(Check(
        "not_top_n_only", "fail" if top_n_fail else "pass",
        f"Stock rows: {len(stocks)}; NAV weight total: {stock_total}",
    ))

    counts = Counter(h.code for h in stocks)
    duplicates = sorted(code for code, count in counts.items() if count > 1)
    checks.append(Check(
        "no_duplicate_security", "fail" if duplicates else "pass",
        f"Duplicate stock codes: {duplicates}",
    ))
    age = ((today - s.holdings_date).days
           if type(s.holdings_date) is date else None)
    checks.append(Check(
        "holdings_date_sane",
        "pass" if age is not None and 0 <= age <= MAX_AGE_DAYS else "fail",
        f"Holdings calendar age in days: {age}",
    ))

    unresolved = tuple(
        h for h in stocks
        if directory.resolve(h.code, h.name).issuer_code is None
    )
    unresolved_total = sum(h.weight for h in unresolved
                           if _finite_number(h.weight))
    checks.append(Check(
        "resolvable_securities",
        "warn" if unresolved_total > UNRESOLVED_WARN_WEIGHT else "pass",
        f"Unresolved stock rows: {len(unresolved)}; "
        f"known NAV weight total: {unresolved_total}; "
        "invalid weights fail numeric checks separately",
    ))
    missing = [name for name in ("source_url", "etf_symbol")
               if not isinstance(getattr(s, name), str)
               or not getattr(s, name).strip()]
    checks.append(Check(
        "required_fields", "fail" if missing else "pass",
        f"Missing required fields: {missing}",
    ))
    severity = {"pass": 0, "warn": 1, "fail": 2}
    status = max((check.status for check in checks), key=severity.__getitem__)
    return ValidationResult(status, tuple(checks))
