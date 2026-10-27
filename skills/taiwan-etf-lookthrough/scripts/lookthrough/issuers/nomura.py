"""Parse Nomura 00935 native asset percentages with independent PCF checks."""

import base64
import binascii
from datetime import date
from decimal import Decimal
import json
import re
from typing import Any, TYPE_CHECKING

from ._assets import check_stock, nav_number, number, read_json
from ..models import Holding, Snapshot, snapshot_from_dict, snapshot_to_dict

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = (
    "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets")
PCF_URL = (
    "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundTradeInfo")
_COLUMNS = {
    "股票": ["股票代號", "股票名稱", "股數", "權重(%)"],
    "期貨": ["期貨代碼", "期貨名稱", "口數", "權重(%)"],
    "": ["項目", "金額", "幣別", "金額(原幣)"],
}
_CATEGORIES = {"股票", "期貨", "現金", "保證金", "應收(付)證券款"}
_ENVELOPE = "nomura-native-assets-v1"
_PCF_KEYS = {
    "CId", "CFundId", "CFullName", "CFundShortName", "CPcfdate", "CNavDt",
    "CNavDtStr", "CAnceTotalAv", "CAnceTotalIssues", "CAnceIssuesDiff",
    "CAnceNav", "CAnceEstcValue", "CCtrlBasketValue", "CCtrlDiffBasketValue",
    "CCtrlBasketValueP", "CCloseDt", "CAnceTotalIssuesT1", "CEtfindexCurrency",
    "CExRate", "CExRateDt", "CEtfAcrossTime", "CAnceBaseValue", "Stocks",
    "Futures", "Bonds", "BondProperty", "TradeInfoCurrencies", "Etfs",
    "Options", "CWarning", "CCalendarDt", "CBeneficiariesCount",
    "CIndexOrProductType", "CAnceTotalIssuesT1Date", "CBeneficiariesCountDate",
}


def capture(assets: bytes, pcf: bytes) -> bytes:
    """Package exact local HTTP bytes; perform no requests or source edits."""
    read_json(assets)
    read_json(pcf)
    return json.dumps({
        "capture_kind": _ENVELOPE,
        "assets_base64": base64.b64encode(assets).decode("ascii"),
        "pcf_base64": base64.b64encode(pcf).decode("ascii"),
    }, separators=(",", ":")).encode("utf-8")


def _unpack(raw: bytes) -> tuple[Any, Any]:
    """Read both original responses from a bounded local capture."""
    payload = read_json(raw)
    if (not isinstance(payload, dict)
            or set(payload) != {"capture_kind", "assets_base64", "pcf_base64"}
            or payload["capture_kind"] != _ENVELOPE):
        raise ValueError("Expected qualified Nomura local capture")
    try:
        return tuple(read_json(base64.b64decode(payload[key], validate=True))
                     for key in ("assets_base64", "pcf_base64"))
    except (TypeError, binascii.Error) as error:
        raise ValueError("Invalid Nomura captured source bytes") from error


def _response(payload: Any) -> dict[str, Any]:
    """Require a successful single-fund API response rather than an error."""
    if (not isinstance(payload, dict)
            or set(payload) != {"TotalPages", "TotalItems", "Entries",
                                "Message", "StatusCode"}
            or number(payload["StatusCode"], native=True) != 0
            or not isinstance(payload["Entries"], dict)):
        raise ValueError("Invalid Nomura API response")
    return payload["Entries"]


def _day(value: Any) -> date:
    """Accept an explicit slash-separated source calendar date."""
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}/\d{2}/\d{2}", value):
        raise ValueError("Invalid Nomura NAV date")
    return date.fromisoformat(value.replace("/", "-"))


def _percent(value: Any) -> Decimal:
    """Read source percentage points at its two-decimal display precision."""
    if not isinstance(value, str) or not re.fullmatch(
            r"\d+(?:\.\d{1,2})?", value):
        raise ValueError("Invalid Nomura percentage-point unit")
    result = number(value)
    if not 0 <= result <= 100:
        raise ValueError("Nomura percentage outside NAV range")
    return result


def _tables(data: Any, day_text: str) -> dict[str, list[list[str]]]:
    """Reject unknown tables, changed headers, units and mixed asset dates."""
    if (not isinstance(data, dict)
            or set(data) != {"FundAsset", "Table", "BondProperty"}
            or data["BondProperty"] is not None
            or not isinstance(data["Table"], list)):
        raise ValueError("Unqualified Nomura asset structure")
    result: dict[str, list[list[str]]] = {}
    for table in data["Table"]:
        if (not isinstance(table, dict)
                or set(table) != {"TableTitle", "Columns", "Rows", "NavDate"}
                or table["NavDate"] != day_text):
            raise ValueError("Invalid Nomura table/date")
        title, columns = table["TableTitle"], table["Columns"]
        if (not isinstance(title, str) or title not in _COLUMNS
                or title in result or not isinstance(columns, list)
                or any(not isinstance(c, dict) for c in columns)
                or [c.get("Name") for c in columns] != _COLUMNS[title]):
            raise ValueError("Unknown/duplicate Nomura table or headers")
        rows = table["Rows"]
        if (not isinstance(rows, list) or len(rows) > 5000
                or any(not isinstance(r, list) or len(r) != 4
                       or any(not isinstance(v, str) for v in r)
                       for r in rows)):
            raise ValueError("Invalid Nomura table rows")
        result[title] = rows
    if set(result) != set(_COLUMNS) or not result["股票"]:
        raise ValueError("Missing Nomura complete asset table")
    return result


def _amounts(rows: list[list[str]], nav: Decimal) -> dict[str, Decimal]:
    """Read every qualified signed TWD asset; never drop new categories."""
    result: dict[str, Decimal] = {}
    for label, display, currency, original in rows:
        if (label not in _CATEGORIES or label in result or currency != "TWD"
                or not display.startswith("TWD$")):
            raise ValueError("Unknown Nomura category/currency")
        amount = number(original)
        if (number(display[4:]) != amount or not -nav <= amount <= nav
                or label in {"股票", "期貨"} and amount < 0):
            raise ValueError("Inconsistent Nomura asset amounts")
        result[label] = amount
    if set(result) != _CATEGORIES:
        raise ValueError("Missing Nomura asset classification")
    return result


def _stocks(rows: list[list[str]]) -> dict[str, tuple[str, Decimal, Decimal]]:
    """Keep every native stock row, including zero rounded percentages."""
    result: dict[str, tuple[str, Decimal, Decimal]] = {}
    for code, name, quantity, weight in rows:
        shares, percent = number(quantity), _percent(weight)
        check_stock(code, name, shares, Decimal(0), Decimal(1))
        if code.startswith("0") or code in result:
            raise ValueError("Duplicate/ETF Nomura stock identity")
        result[code] = (name, shares, percent)
    return result


def _pcf(pcf: dict[str, Any], day: date, nav: Decimal,
         units: Decimal, unit_nav: Decimal,
         stocks: dict[str, tuple[str, Decimal, Decimal]],
         futures: list[list[str]]) -> dict[str, str]:
    """Cross-check full PCF positions against same-valuation-date assets."""
    if (set(pcf) != _PCF_KEYS or pcf.get("CFundId") != "00935"
            or pcf.get("CNavDt") != f"{day.isoformat()}T00:00:00"
            or pcf.get("CNavDtStr") != day.strftime("%Y/%m/%d")
            or pcf.get("CEtfindexCurrency") != "TWD"
            or number(pcf.get("CAnceTotalAv"), native=True) != nav
            or number(pcf.get("CAnceTotalIssues"), native=True) != units
            or nav_number(pcf.get("CAnceNav")) != unit_nav
            or any(pcf.get(k) != [] for k in ("Bonds", "Etfs", "Options"))
            or pcf.get("BondProperty") is not None):
        raise ValueError("Nomura PCF identity/date/NAV or asset mismatch")
    raw_stocks, raw_futures = pcf.get("Stocks"), pcf.get("Futures")
    if not isinstance(raw_stocks, list) or not isinstance(raw_futures, list):
        raise ValueError("Missing Nomura PCF positions")
    reference: dict[str, tuple[str, Decimal, Decimal]] = {}
    for row in raw_stocks:
        if (not isinstance(row, dict)
                or set(row) != {"CtblTradeInfoId", "CStockCode", "CStockName",
                                "CQuantity", "CWeightsPct"}
                or row["CtblTradeInfoId"] != pcf.get("CId")):
            raise ValueError("Invalid Nomura PCF stock structure")
        code = row["CStockCode"]
        if not isinstance(code, str) or code in reference:
            raise ValueError("Duplicate Nomura PCF stock")
        reference[code] = (row["CStockName"],
                           number(row["CQuantity"], native=True),
                           number(row["CWeightsPct"], native=True))
    if stocks != reference:
        raise ValueError("Nomura complete PCF/asset stocks disagree")
    reference_futures: dict[str, tuple[str, Decimal, Decimal]] = {}
    months: dict[str, str] = {}
    for row in raw_futures:
        if (not isinstance(row, dict)
                or set(row) != {"CtblTradeInfoId", "CFuturesCode",
                                "CFuturesName", "CQuantity", "CWeightsPct",
                                "CContractYm"}
                or row["CtblTradeInfoId"] != pcf.get("CId")):
            raise ValueError("Invalid Nomura PCF futures structure")
        code, month = row["CFuturesCode"], row["CContractYm"]
        if (not isinstance(code, str) or code in reference_futures
                or not isinstance(month, str)
                or not re.fullmatch(r"\d{4}/(?:0[1-9]|1[0-2])", month)):
            raise ValueError("Invalid Nomura PCF futures identity/month")
        reference_futures[code] = (row["CFuturesName"],
                                   number(row["CQuantity"], native=True),
                                   number(row["CWeightsPct"], native=True))
        months[code] = month
    actual: dict[str, tuple[str, Decimal, Decimal]] = {}
    for code, name, quantity, weight in futures:
        if (not re.fullmatch(r"[A-Z0-9]+", code) or code in actual
                or not name.strip()):
            raise ValueError("Invalid Nomura futures identity")
        shares = number(quantity)
        if shares < 0 or shares != shares.to_integral():
            raise ValueError("Invalid Nomura futures contracts")
        actual[code] = (name, shares, _percent(weight))
    if actual != reference_futures:
        raise ValueError("Nomura complete PCF/asset futures disagree")
    return months


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Parse full assets and PCF; preserve native stock NAV percentages.

    The complete asset table's stock amount/NAV must lie within the sum
    of per-stock two-decimal display intervals. Independent PCF identity,
    valuation day, NAV, names, shares and weights must agree row-for-row.
    Cash/payables retain signed amount/NAV. Undisclosed asset/liability
    details and futures NAV/notional semantics remain unknown, never zero.
    """
    if (etf.symbol != "00935" or etf.issuer != "nomura" or etf.is_active
            or etf.source_url != SOURCE_URL):
        raise ValueError("Unqualified Nomura request identity")
    assets, pcf = _unpack(raw)
    entry, pcf = _response(assets), _response(pcf)
    if set(entry) != {"FundID", "Data"} or entry["FundID"] != "00935":
        raise ValueError("Nomura response fund identity mismatch")
    data = entry["Data"]
    if (not isinstance(data, dict)
            or not isinstance(data.get("FundAsset"), dict)):
        raise ValueError("Missing Nomura fund NAV")
    fund = data["FundAsset"]
    if set(fund) != {"Aum", "Units", "Nav", "NavDate"}:
        raise ValueError("Changed Nomura NAV structure")
    day, nav = _day(fund["NavDate"]), nav_number(fund["Aum"])
    units, unit_nav = nav_number(fund["Units"]), nav_number(fund["Nav"])
    if abs(nav / units - unit_nav) > Decimal("0.005"):
        raise ValueError("Nomura AUM/units do not reconcile to rounded NAV")
    tables = _tables(data, fund["NavDate"])
    amounts = _amounts(tables[""], nav)
    stocks = _stocks(tables["股票"])
    total = sum((v[2] for v in stocks.values()), Decimal(0))
    if abs(amounts["股票"] / nav * 100 - total) > (
            len(stocks) * Decimal("0.005")):
        raise ValueError("Nomura stocks fail aggregate NAV rounding bounds")
    months = _pcf(pcf, day, nav, units, unit_nav, stocks, tables["期貨"])
    holdings = [Holding(code, name, "stock", float(weight / 100))
                for code, (name, _, weight) in stocks.items()]
    holdings.extend(Holding(label, label,
                            "cash" if label == "現金" else "other",
                            float(amount / nav))
                    for label, amount in amounts.items()
                    if label not in {"股票", "期貨"})
    holdings.extend(Holding(code, f"{name} {months[code]}", "futures", None)
                    for code, name, _, _ in tables["期貨"])
    holdings.append(Holding("UNCLASSIFIED", "未分解其他資產及負債", "other", None))
    snapshot = Snapshot("1", etf.symbol, etf.issuer, False, day, fetched_at,
                        SOURCE_URL, "official", tuple(holdings))
    return snapshot_from_dict(snapshot_to_dict(snapshot))
