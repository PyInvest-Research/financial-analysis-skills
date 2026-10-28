"""Validate disclosed holdings without filling or rescaling missing data."""

from collections import Counter
from dataclasses import dataclass
from datetime import date
import math
from typing import Literal
from urllib.parse import urlsplit

from .directory import CompanyDirectory
from .mixed_contract import qualifies_mixed
from .models import Snapshot
from .research import is_research_snapshot, qualifies_research


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
                      today: date, *,
                      policy: str = "strict") -> ValidationResult:
    """Check NAV weights, coverage, dates and mandatory source identity.

    Cash/other/derivative_nav NAV weights may be signed within [-1, 1];
    stock/futures NAV weights remain within [0, 1]. derivative_nav requires
    an explicit weight and cannot carry notional exposure. Missing other
    nonstock NAV weights stay None.
    Futures notional exposure may be signed and exceed one; only its
    numeric finiteness is checked.
    Directly constructed models can contain invalid stock weights, which
    fail both range and total checks instead of bypassing comparisons.
    No network, input mutation or weight normalization is performed.
    """
    if policy not in ("strict", "annotated"):
        raise ValueError("policy must be strict or annotated")
    annotated = policy == "annotated"
    research = qualifies_research(s)
    stocks = tuple(h for h in s.holdings if h.kind == "stock")
    mixed = qualifies_mixed(s) or research
    equities = tuple(h for h in s.holdings
                     if h.kind in ("stock", "foreign_stock"))
    invalid_rows = []
    for i, holding in enumerate(s.holdings):
        weight = holding.weight
        lower_bound = (-1 if holding.kind in (
            "cash", "other", "derivative_nav") else 0)
        missing_allowed = (annotated or research
                           and holding.kind == "foreign_stock")
        if ((weight is None and holding.kind in (
                "stock", "foreign_stock", "derivative_nav")
             and not missing_allowed)
                or (weight is not None and (
                    not _finite_number(weight)
                    or not lower_bound <= weight <= 1))
                or (holding.kind == "derivative_nav"
                    and holding.exposure_weight is not None)
                or (holding.exposure_weight is not None
                    and not _finite_number(holding.exposure_weight))):
            invalid_rows.append(i + 1)
    checks = [Check(
        "weights_in_range", "fail" if invalid_rows else "pass",
        f"Invalid numeric/NAV weights at rows: {invalid_rows}",
    )]

    checked_stocks = equities if mixed or annotated else stocks
    valid_stock_weights = all(
        _finite_number(h.weight) or h.weight is None and (
            annotated or research)
        for h in checked_stocks)
    stock_total = sum(
        h.weight for h in checked_stocks
        if h.weight is not None) if valid_stock_weights else None
    sum_status: Status = "pass"
    if (stock_total is None or not _finite_number(stock_total)
            or stock_total > MAX_STOCK_WEIGHT_SUM
            or stock_total < (0 if mixed or annotated
                              else MIN_STOCK_WEIGHT_SUM)):
        sum_status = "fail"
    elif not mixed and stock_total < WARN_STOCK_WEIGHT_SUM:
        sum_status = "warn"
    checks.append(Check("stock_weight_sum", sum_status,
                        f"Stock NAV weight total: {stock_total}"))
    top_n_fail = (not mixed and len(stocks) <= TOP_N_TRAP_COUNT
                  and (stock_total is None
                       or not _finite_number(stock_total)
                       or stock_total < WARN_STOCK_WEIGHT_SUM))
    checks.append(Check(
        "not_top_n_only", "fail" if top_n_fail else "pass",
        f"Stock rows: {len(stocks)}; NAV weight total: {stock_total}",
    ))

    if annotated and top_n_fail:
        checks[-1] = Check("not_top_n_only", "warn",
                           "部分持股／前十大名單：只計算已知列，不能稱為完整曝險")
    counts = Counter((h.market, h.code) if mixed or annotated else h.code
                     for h in checked_stocks)
    duplicates = sorted((code for code, count in counts.items() if count > 1),
                        key=str)
    checks.append(Check(
        "no_duplicate_security", "fail" if duplicates else "pass",
        f"Duplicate stock codes: {duplicates}",
    ))
    if s.schema_version == "2" or s.equity_evidence is not None or any(
            h.kind == "foreign_stock" or h.market is not None
            for h in s.holdings):
        if research:
            checks.append(Check(
                "research_equity_evidence", "warn",
                "採用已審核的固定研究快照；保留原持股日，非每日更新。"
                "台股逐股比例可用，海外權重或全基金 NAV 未知時仍保留缺漏。"))
        elif annotated and not qualifies_mixed(s):
            checks.append(_annotated_equity(s, directory))
        else:
            checks.append(_mixed_evidence(s, directory))
    if is_research_snapshot(s) and not research:
        checks.append(Check("research_content_hash", "fail",
                            "Research snapshot differs from reviewed content"))
    age = ((today - s.holdings_date).days
           if type(s.holdings_date) is date else None)
    checks.append(Check(
        "holdings_date_sane",
        ("pass" if age is not None and 0 <= age <= MAX_AGE_DAYS else
         "warn" if annotated and age is not None and age > MAX_AGE_DAYS
         else "fail"),
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
    if annotated:
        try:
            url = urlsplit(s.source_url)
            safe_url = (url.scheme == "https" and bool(url.hostname)
                        and not url.username and not url.password
                        and url.port in (None, 443))
        except ValueError:
            safe_url = False
        checks.append(Check("source_https", "pass" if safe_url else "fail",
                            "來源須為 HTTPS；官網身分由取得資料的 agent 核對"))
        unknown = sum(h.weight is None for h in equities)
        if unknown:
            checks.append(Check("partial_equity_weights", "warn",
                                f"{unknown} 列股票權重未知；僅計算已知權重"))
        if not research and not mixed and not any(
                h.kind == "stock" and _finite_number(h.weight)
                for h in s.holdings):
            checks.append(Check("usable_equity_rows", "fail",
                                "沒有可計算台股列，亦無已審核的零台股證據"))
    if s.issuer == "yuanta" and s.etf_symbol in ("0055", "0056"):
        checks.append(Check(
            "NAV_denominator_qualification", "warn",
            "官方 FundWeights.Summary.fundsize 與 PCF.totalav 曾有差異；"
            "沿用官方公布權重暫估，不任選分母或重新放大。"))
    severity = {"pass": 0, "warn": 1, "fail": 2}
    status = max((check.status for check in checks), key=severity.__getitem__)
    return ValidationResult(status, tuple(checks))


def _annotated_equity(snapshot: Snapshot,
                      directory: CompanyDirectory) -> Check:
    """Allow agent-provided partial equities with explicit market identity.

    Unknown aggregates stay unused. Reported amounts use disclosed row
    weights only; this check never certifies a complete zero allocation.
    """
    evidence = snapshot.equity_evidence
    equities = [h for h in snapshot.holdings
                if h.kind in ("stock", "foreign_stock")]
    valid = evidence is not None and bool(equities)
    if valid:
        valid = (type(evidence.complete) is bool
                 and type(evidence.stock_rows) is int
                 and evidence.stock_rows == len(equities)
                 and bool(evidence.source_hashes)
                 and all(value is None or _finite_number(value) and value >= 0
                         for value in (evidence.total_nav_twd,
                                       evidence.stock_value_twd,
                                       evidence.taiwan_stock_value_twd))
                 and evidence.total_nav_twd != 0
                 and all(h.market in ("TWSE", "TPEx")
                         if h.kind == "stock" else
                         bool(h.market) and h.market not in ("TWSE", "TPEx")
                         for h in equities))
    if valid:
        valid = all(directory.get(h.code) is None
                    or directory.get(h.code).market == h.market
                    for h in equities if h.kind == "stock")
    return Check("annotated_equity_evidence", "warn" if valid else "fail",
                 "依 agent 核對之官方逐股權重暫估；完整性與總額口徑未獨立驗證，"
                 "不以聚合比率覆蓋逐股結果，不推定零台股")


def _mixed_evidence(snapshot: Snapshot, directory: CompanyDirectory) -> Check:
    """Require explicit markets, complete rows and same-NAV reconciliation."""
    evidence = snapshot.equity_evidence
    equities = [h for h in snapshot.holdings
                if h.kind in ("stock", "foreign_stock")]
    valid = qualifies_mixed(snapshot) and evidence is not None
    if valid:
        valid = (evidence.complete is True
                 and type(evidence.stock_rows) is int
                 and evidence.stock_rows == len(equities) > 0
                 and _finite_number(evidence.total_nav_twd)
                 and evidence.total_nav_twd > 0
                 and _finite_number(evidence.stock_value_twd)
                 and 0 <= evidence.stock_value_twd
                 <= evidence.total_nav_twd * MAX_STOCK_WEIGHT_SUM
                 and bool(evidence.source_hashes)
                 and all(_finite_number(h.weight) for h in equities))
    if valid:
        valid = all(
            (h.market in ("TWSE", "TPEx") if h.kind == "stock"
             else h.market in ("KOSPI", "KOSDAQ", "NASDAQ"))
            for h in equities)
        valid = valid and all(
            directory.get(h.code) is None
            or directory.get(h.code).market == h.market
            for h in equities if h.kind == "stock")
    if valid:
        # Two printed percentage decimals allow half .01 percentage point.
        tolerance = len(equities) * .00005 + 1e-12
        total = sum(h.weight for h in equities)
        valid = abs(total - evidence.stock_value_twd
                    / evidence.total_nav_twd) <= tolerance
        taiwan = evidence.taiwan_stock_value_twd
        if taiwan is not None:
            local = [h for h in equities if h.kind == "stock"]
            valid = (valid and _finite_number(taiwan)
                     and 0 <= taiwan <= evidence.stock_value_twd
                     and abs(sum(h.weight for h in local)
                             - taiwan / evidence.total_nav_twd)
                     <= len(local) * .00005 + 1e-12)
    return Check("mixed_equity_evidence", "pass" if valid else "fail",
                 "Complete actual equity rows, explicit listing markets and "
                 "whole-fund NAV aggregates must reconcile; no rescaling")
