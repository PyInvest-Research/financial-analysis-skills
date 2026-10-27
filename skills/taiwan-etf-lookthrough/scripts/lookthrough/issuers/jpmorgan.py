"""Parse the official JPMorgan full portfolio and signed derivative NAV."""

from datetime import date
from decimal import Decimal
import re
from typing import Any, TYPE_CHECKING

from ._assets import number, read_json
from ._workbooks import read_sheets
from ..models import Holding, Snapshot, snapshot_from_dict, snapshot_to_dict

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = (
    "https://am.jpmorgan.com/FundsMarketingHandler/historicalData?"
    "cusip=TW00000401A1&country=tw&role=twetf&userLoggedIn=false&language=zh"
    "&version=9.18_1791879504")
DOWNLOAD_URL = (
    "https://am.jpmorgan.com/FundsMarketingHandler/excel?type=holding_pcf&"
    "cusip=TW00000401A1&country=tw&role=twetf&locale=zh-TW&date=")
_TITLE = "摩根台灣鑫收益主動式ETF基金"
_STOCKS = "基金資產 - 股票"
_FUTURES = "基金資產 - 期貨"
_OPTIONS = "基金資產 - 選擇權"
_CASH = "現金與約當現金"
_SHEETS = {_STOCKS, _FUTURES, _OPTIONS, _CASH}


def latest_day(raw: bytes) -> date:
    """Select a published holdings day, independent of NAV/publication day."""
    payload = read_json(raw)
    days = payload.get("holdingAvailableDates") if isinstance(
        payload, dict) else None
    if not isinstance(days, list) or not 1 <= len(days) <= 10000:
        raise ValueError("Missing JPMorgan available holdings dates")
    parsed = []
    for value in days:
        if not isinstance(value, str):
            raise ValueError("Invalid JPMorgan holdings date")
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError("Noncanonical JPMorgan holdings date")
        parsed.append(day)
    return max(parsed)


def _percentage(value: Any, *, signed: bool = False) -> float:
    """Use the explicitly documented NAV percentage without rescaling."""
    if not isinstance(value, str) or not re.fullmatch(r"-?\d+\.\d{2}%", value):
        raise ValueError("Invalid JPMorgan NAV percentage")
    result = Decimal(value[:-1]) / 100
    if not (-1 if signed else 0) <= result <= 1:
        raise ValueError("JPMorgan NAV percentage outside range")
    return float(result)


def _rows(name: str, rows: list[list[Any]]) -> tuple[date, list[list[Any]]]:
    """Require identical dated titles and source NAV explanatory notes."""
    if len(rows) < 13 or len(rows[0]) != 1 or rows[1] != [_TITLE]:
        raise ValueError("Invalid JPMorgan fund identity/layout")
    match = re.fullmatch(re.escape(name) + r" \((\d{4}-\d{2}-\d{2})\)",
                         str(rows[0][0]))
    if match is None:
        raise ValueError("Missing JPMorgan sheet holdings date")
    notes = " ".join(str(row[0]) for row in rows[5:11] if row)
    if ("相對基金淨資產價值之比重" not in notes
            or "每日結算損益" not in notes or "選擇權權重" not in notes):
        raise ValueError("Missing qualified JPMorgan NAV convention")
    expected = (["股票代碼", "股票名稱", "股數", "金額", "權重(%)"]
                if name == _STOCKS else ["名稱", "金額 (TWD)", "權重 (%)"]
                if name == _CASH else
                ["商品代碼", "商品名稱", "商品數量 (口數)", "權重 (%)"])
    if rows[12] != expected:
        raise ValueError("Changed JPMorgan source header")
    return date.fromisoformat(match[1]), [r for r in rows[13:] if r]


def _nav_bounds(amount: Decimal, weight: float) -> tuple[Decimal, Decimal]:
    """Bound a common NAV using amount and two-decimal printed percentage.

    This is only a consistency check, not an inferred replacement for NAV
    or a recalculation of the disclosed canonical weight.
    """
    fraction = Decimal(str(weight))
    if amount * fraction < 0 or (amount == 0 and fraction != 0):
        raise ValueError("JPMorgan amount/percentage NAV signs disagree")
    if amount == 0:
        return Decimal(0), Decimal("Infinity")
    amount, fraction = abs(amount), abs(fraction)
    half = Decimal("0.00005")
    low = amount / (fraction + half)
    high = (amount / (fraction - half) if fraction > half
            else Decimal("Infinity"))
    return low, high


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Preserve all portfolio sheets and keep book NAV distinct from risk.

    Native weights have an explicit fund-NAV definition in each sheet.
    Futures settlement and option market values use derivative_nav; the
    separately retained futures notional exposure remains unknown.
    """
    if (etf.symbol != "00401A" or etf.issuer != "jpmorgan"
            or not etf.is_active or etf.source_url != SOURCE_URL):
        raise ValueError("Unqualified JPMorgan request identity")
    sheets = read_sheets(raw)
    if set(sheets) != _SHEETS:
        raise ValueError("Missing/unknown JPMorgan portfolio asset sheet")
    day = None
    holdings: list[Holding] = []
    nav_low, nav_high = Decimal(0), Decimal("Infinity")
    for name in (_STOCKS, _FUTURES, _OPTIONS, _CASH):
        actual, rows = _rows(name, sheets[name])
        if day is not None and day != actual:
            raise ValueError("JPMorgan portfolio sheet dates disagree")
        day = actual
        seen: set[str] = set()
        for row in rows:
            expected = 5 if name == _STOCKS else 3 if name == _CASH else 4
            if len(row) != expected:
                raise ValueError("Malformed JPMorgan portfolio row")
            code, label = row[:2] if name != _CASH else ("TWD", row[0])
            if (not isinstance(code, str) or not code or code in seen
                    or not isinstance(label, str) or not label):
                raise ValueError("Duplicate/invalid JPMorgan security")
            seen.add(code)
            if name == _STOCKS:
                shares, amount = number(row[2]), number(row[3])
                if (not re.fullmatch(r"\d{4}[A-Z]?", code)
                        or shares < 0 or shares != shares.to_integral()
                        or amount < 0):
                    raise ValueError("Invalid JPMorgan stock values")
                weight = _percentage(row[4])
                low, high = _nav_bounds(amount, weight)
                nav_low, nav_high = max(nav_low, low), min(nav_high, high)
                holdings.append(Holding(code, label, "stock", weight))
            elif name == _CASH:
                if row[0] != "NEW TAIWAN DOLLAR":
                    raise ValueError("Unqualified JPMorgan cash currency")
                weight = _percentage(row[2], signed=True)
                low, high = _nav_bounds(number(row[1]), weight)
                nav_low, nav_high = max(nav_low, low), min(nav_high, high)
                holdings.append(Holding("TWD", row[0], "cash", weight))
            else:
                quantity = number(row[2])
                if quantity != quantity.to_integral():
                    raise ValueError("Invalid JPMorgan derivative quantity")
                if name == _FUTURES:
                    holdings.append(Holding(code, label, "futures", None))
                holdings.append(Holding(
                    code, label, "derivative_nav",
                    _percentage(row[3], signed=True)))
        if name == _STOCKS and not rows:
            raise ValueError("Empty JPMorgan stock disclosure")
    if nav_low >= nav_high:
        raise ValueError("JPMorgan amount/weight NAV denominators disagree")
    snapshot = Snapshot(
        "1", etf.symbol, etf.issuer, True, day, fetched_at,
        DOWNLOAD_URL + day.isoformat(), "official", tuple(holdings))
    return snapshot_from_dict(snapshot_to_dict(snapshot))
