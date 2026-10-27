"""Parse First Securities 00728 native full-holdings groups conservatively."""

from datetime import date
from decimal import Decimal
import re
from typing import Any, TYPE_CHECKING

from ._assets import check_stock, number, read_json
from ..models import Holding, Snapshot, snapshot_from_dict, snapshot_to_dict

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = "https://www.fsitc.com.tw/WebAPI.aspx/Get_hd"
_ROW_KEYS = {"fundid", "sdate", "group", "A", "B", "C", "D", "E"}
_TOTAL_CODES = {"股票": "1", "期貨": "3", "其他+負債": "5"}
_ASSET_KINDS = {"保證金": "other", "現金/存款": "cash"}


def _percent(value: Any, *, signed: bool = False,
             suffix: bool = False) -> Decimal:
    """Require native two-decimal percentage units without guessing scales."""
    pattern = r"-?\d+\.\d{2}" if signed else r"\d+\.\d{2}"
    if suffix:
        pattern += "%"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Invalid First Securities percentage unit")
    percent = number(value[:-1] if suffix else value)
    if not (-100 if signed else 0) <= percent <= 100:
        raise ValueError("First Securities percentage outside NAV range")
    return percent


def _rows(raw: bytes) -> tuple[list[dict[str, str]], date]:
    """Decode d and require every fund ID and actual asset day to agree."""
    outer = read_json(raw)
    if (not isinstance(outer, dict) or set(outer) != {"d"}
            or not isinstance(outer["d"], str)):
        raise ValueError("Expected First Securities JSON-string wrapper")
    rows = read_json(outer["d"].encode("utf-8"))
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError("Missing/oversized First Securities full holdings")
    day_text = None
    for row in rows:
        if (not isinstance(row, dict) or set(row) != _ROW_KEYS
                or any(not isinstance(v, str) for v in row.values())
                or row["fundid"] != "D90" or row["group"] not in {"1", "2",
                                                                  "4", "5"}
                or row["E"] != ""):
            raise ValueError("Unknown First Securities row/fund/asset type")
        if day_text is None:
            day_text = row["sdate"]
        if row["sdate"] != day_text:
            raise ValueError("Mixed First Securities asset dates")
    if not isinstance(day_text, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", day_text):
        raise ValueError("Missing actual First Securities asset date")
    return rows, date.fromisoformat(day_text)


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Keep full native stock percentages, reconciling classified groups.

    D90 is the source-page-qualified identity for 00728. The source's group
    1 stock rows reconcile to its group 5 stock percentage within summed
    two-decimal rounding bounds. Group 4 cash/margin percentages reconcile
    separately to the other-assets category. Their disclosed TWD amounts
    must admit a common positive denominator at that display precision.
    This does not date or validate the separate 10/27 PCF NAV: it is never
    used. Unknown liabilities and both futures weight meanings remain None.
    All returned rows are processed; no top-N or zero-weight filtering occurs.
    """
    if (etf.symbol != "00728" or etf.issuer != "fsitc" or etf.is_active
            or etf.source_url != SOURCE_URL):
        raise ValueError("Unqualified First Securities request identity")
    rows, day = _rows(raw)
    stocks: dict[str, tuple[str, Decimal]] = {}
    futures: dict[str, tuple[str, Decimal]] = {}
    assets: dict[str, tuple[Decimal, Decimal]] = {}
    totals: dict[str, Decimal] = {}
    for row in rows:
        group, label = row["group"], row["A"]
        if group == "1":
            percent, shares = _percent(row["C"]), number(row["D"])
            check_stock(label, row["B"], shares, Decimal(0), Decimal(1))
            if label.startswith("0") or label in stocks:
                raise ValueError("Duplicate/ETF First Securities stock")
            stocks[label] = (row["B"], percent)
        elif group == "2":
            percent, contracts = _percent(row["B"]), number(row["C"])
            month = row["D"]
            identity = f"{label} {month}"
            if (not label.strip() or identity in futures or contracts < 0
                    or contracts != contracts.to_integral()
                    or not re.fullmatch(r"\d{4}(?:0[1-9]|1[0-2])", month)):
                raise ValueError("Invalid First Securities futures contract")
            futures[identity] = (identity, percent)
        elif group == "4":
            if (label not in _ASSET_KINDS or label in assets or row["D"] != ""
                    or not row["B"].startswith("TWD ")):
                raise ValueError("Unknown First Securities asset/currency")
            amount = number(row["B"][4:])
            percent = _percent(row["C"], signed=True)
            if amount * percent < 0 or amount == 0 and percent != 0:
                raise ValueError("First Securities asset sign mismatch")
            assets[label] = (amount, percent)
        else:
            if (label not in _TOTAL_CODES or label in totals
                    or row["C"] != _TOTAL_CODES[label] or row["D"] != ""):
                raise ValueError("Unknown/duplicate First Securities category")
            totals[label] = _percent(row["B"], signed=True, suffix=True)
    if (not stocks or set(totals) != set(_TOTAL_CODES)
            or set(assets) != set(_ASSET_KINDS)):
        raise ValueError("Missing First Securities asset category/positions")
    for label, values in (("股票", [p for _, p in stocks.values()]),
                          ("期貨", [p for _, p in futures.values()]),
                          ("其他+負債", [p for _, p in assets.values()])):
        if abs(sum(values, Decimal(0)) - totals[label]) > (
                len(values) + 1) * Decimal("0.005"):
            raise ValueError("First Securities full rows fail category bounds")
    lower, upper = Decimal(0), Decimal("Infinity")
    for amount, percent in assets.values():
        if amount == 0:
            continue
        absolute, points = abs(amount), abs(percent)
        lower = max(lower, absolute * 100 / (points + Decimal("0.005")))
        if points > Decimal("0.005"):
            upper = min(upper, absolute * 100 / (points - Decimal("0.005")))
    if lower > upper:
        raise ValueError(
            "First Securities asset percentage denominators differ")
    holdings = [Holding(code, name, "stock", float(percent / 100))
                for code, (name, percent) in stocks.items()]
    holdings.extend(Holding(label, label, _ASSET_KINDS[label],
                            float(percent / 100))
                    for label, (_, percent) in assets.items())
    holdings.extend(Holding(code, name, "futures", None)
                    for code, (name, _) in futures.items())
    holdings.append(Holding("UNCLASSIFIED", "未分解其他資產及負債", "other", None))
    snapshot = Snapshot("1", etf.symbol, etf.issuer, False, day, fetched_at,
                        SOURCE_URL, "official", tuple(holdings))
    return snapshot_from_dict(snapshot_to_dict(snapshot))
