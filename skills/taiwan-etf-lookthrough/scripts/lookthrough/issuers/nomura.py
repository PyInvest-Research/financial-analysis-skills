"""Parse reviewed Nomura native equity weights with paired PCF checks."""

import base64
import binascii
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from typing import Any, TYPE_CHECKING

from ._assets import check_stock, nav_number, number, read_json
from ..models import (EquityEvidence, Holding, Snapshot, snapshot_from_dict,
                      snapshot_to_dict)
from ..refresh_contract import directory

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
# These are domestic equity source contracts, not a daily-refresh registry.
_PRODUCTS = {
    "00935": (False, "野村臺灣創新科技50 ETF證券投資信託基金"),
    "00944": (False, "野村臺灣趨勢動能高股息ETF證券投資信託基金"),
    "00980A": (True, "野村臺灣智慧優選主動式ETF"),
}
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


def _unpack(raw: bytes) -> tuple[bytes, bytes]:
    """Read both original responses from a bounded local capture."""
    payload = read_json(raw)
    if (not isinstance(payload, dict)
            or set(payload) != {"capture_kind", "assets_base64", "pcf_base64"}
            or payload["capture_kind"] != _ENVELOPE):
        raise ValueError("Expected qualified Nomura local capture")
    try:
        return tuple(base64.b64decode(payload[key], validate=True)
                     for key in ("assets_base64", "pcf_base64"))
    except (TypeError, binascii.Error) as error:
        raise ValueError("Invalid Nomura captured source bytes") from error


def _response(payload: Any) -> dict[str, Any]:
    """Require a successful single-fund API response rather than an error."""
    if (not isinstance(payload, dict)
            or set(payload) != {"TotalPages", "TotalItems", "Entries",
                                "Message", "StatusCode"}
            or number(payload["StatusCode"], native=True) != 0
            or number(payload["TotalPages"], native=True) != -1
            or number(payload["TotalItems"], native=True) != 0
            or payload["Message"] != ""
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
                or any(not isinstance(c, dict)
                       or set(c) != {"Name", "TextAlign"}
                       or c["TextAlign"] != "center" for c in columns)
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
         futures: list[list[str]], symbol: str = "00935") -> dict[str, str]:
    """Cross-check full PCF positions against same-valuation-date assets."""
    if (set(pcf) != _PCF_KEYS or pcf.get("CFundId") != symbol
            or pcf.get("CFullName") != _PRODUCTS[symbol][1]
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


def _domestic_equities(stocks, pcf) -> tuple[list[Holding], list[str]]:
    """Use the reviewed domestic route and unique roster codes, not aliases.

    Literal abbreviations need not equal source full/truncated names. Known
    contradictory identities or overseas listing signals still fail closed.
    No name similarity is used to guess a market or to resolve unknown codes.
    """
    if pcf["CEtfAcrossTime"] != "N":
        raise ValueError("Changed Nomura domestic market context")
    currencies = pcf["TradeInfoCurrencies"]
    if not isinstance(currencies, list) or len(currencies) != 1:
        raise ValueError("Unknown Nomura trading currencies")
    currency = currencies[0]
    if (not isinstance(currency, dict) or set(currency) != {
            "CId", "CtblTradeInfoId", "CFromCurrency", "CToCurrency",
            "CExchange", "CExRateDt", "CCreateDt"}
            or currency["CtblTradeInfoId"] != pcf["CId"]
            or currency["CFromCurrency"] != "TWD"
            or currency["CToCurrency"] != "TWD"
            or number(currency["CExchange"], native=True) != 1):
        raise ValueError("Changed Nomura domestic currency contract")
    roster = directory()
    companies = {code: roster.get(code) for code in stocks}
    if any(c is None or c.market not in {"TWSE", "TPEx"}
           for c in companies.values()):
        raise ValueError("Unknown Nomura listing code/market")
    name_codes: dict[str, set[str]] = {}
    for code, (name, _, _) in stocks.items():
        for label in (name, companies[code].name):
            name_codes.setdefault(label.replace("*", ""), set()).add(code)
    holdings, differences = [], 0
    for code, (name, _, weight) in stocks.items():
        if (re.search(r"\b(?:ADR|ADS|NYSE|NASDAQ|HKEX)\b|存託憑證|美股|港股",
                      name, re.IGNORECASE)
                or name_codes[name.replace("*", "")] != {code}):
            raise ValueError("Conflicting Nomura company/listing identity")
        company = companies[code]
        differences += name.replace("*", "") != company.name.replace("*", "")
        holdings.append(Holding(code, name, "stock", float(weight / 100),
                                market=company.market))
    return holdings, [
        f"國內股票以唯一官方代號／市場辨識；名錄日期 {roster.as_of}；"
        f"{differences} 列來源全名／截短名與名錄簡稱不同，保留原名。",
        "採官方原生 NAV 百分點，不正規化；股票分類值核對僅支持來源契約，"
        "未取得逐股市值；配對 PCF 可能共用後端，非獨立審計。",
    ]


def validate_request(etf: "SupportedEtf") -> None:
    """Share the reviewed identity gate between acquisition and parsing."""
    if (etf.symbol not in _PRODUCTS or etf.issuer != "nomura"
            or type(etf.is_active) is not bool
            or etf.is_active != _PRODUCTS[etf.symbol][0]
            or etf.source_url != SOURCE_URL):
        raise ValueError("Unqualified Nomura request identity")


def pcf_valuation_day(raw: bytes, etf: "SupportedEtf", today: date) -> date:
    """Select a dated assets query from a qualified PCF, never its PCF date.

    This is acquisition context only. The complete paired parser still checks
    every identity, date, NAV, unit, share and weight before publication.
    """
    validate_request(etf)
    pcf = _response(read_json(raw))
    day = _day(pcf.get("CNavDtStr"))
    if (set(pcf) != _PCF_KEYS or pcf.get("CFundId") != etf.symbol
            or pcf.get("CFullName") != _PRODUCTS[etf.symbol][1]
            or pcf.get("CNavDt") != f"{day.isoformat()}T00:00:00"
            or pcf.get("CEtfindexCurrency") != "TWD" or day > today):
        raise ValueError("Invalid Nomura PCF query identity/date")
    for key in ("CAnceTotalAv", "CAnceTotalIssues", "CAnceNav"):
        nav_number(pcf.get(key))
    return day


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Parse full assets and PCF; preserve native stock NAV percentages.

    The complete asset table's stock amount/NAV must lie within the sum
    of per-stock two-decimal display intervals. Paired PCF identity,
    valuation day, NAV, names, shares and weights must agree row-for-row.
    Cash/payables retain signed amount/NAV. Undisclosed asset/liability
    details and futures NAV/notional semantics remain unknown, never zero.
    """
    validate_request(etf)
    assets_raw, pcf_raw = _unpack(raw)
    entry, pcf = _response(read_json(assets_raw)), _response(read_json(pcf_raw))
    if set(entry) != {"FundID", "Data"} or entry["FundID"] != etf.symbol:
        raise ValueError("Nomura response fund identity mismatch")
    data = entry["Data"]
    if (not isinstance(data, dict)
            or not isinstance(data.get("FundAsset"), dict)):
        raise ValueError("Missing Nomura fund NAV")
    fund = data["FundAsset"]
    if set(fund) != {"Aum", "Units", "Nav", "NavDate"}:
        raise ValueError("Changed Nomura NAV structure")
    day, nav = _day(fund["NavDate"]), nav_number(fund["Aum"])
    stamp = datetime.fromisoformat(fetched_at)
    if ("T" not in fetched_at or stamp.utcoffset() != timedelta(0)
            or day > stamp.astimezone(timezone(timedelta(hours=8))).date()):
        raise ValueError("Nomura actual holdings postdate UTC capture")
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
    months = _pcf(pcf, day, nav, units, unit_nav, stocks, tables["期貨"],
                  etf.symbol)
    holdings, notes = _domestic_equities(stocks, pcf)
    residual = nav - sum(v for k, v in amounts.items() if k != "期貨")
    notes.append(f"NAV 減已列非期貨資產為 {residual} TWD；差額原因未知，"
                 "不建立平衡項，不把期貨名目額當資產或股票權重。")
    evidence = EquityEvidence(
        True, float(nav), float(amounts["股票"]), len(stocks),
        float(amounts["股票"]), tuple(hashlib.sha256(b).hexdigest()
                                    for b in (assets_raw, pcf_raw)))
    holdings.extend(Holding(label, label,
                            "cash" if label == "現金" else "other",
                            float(amount / nav))
                    for label, amount in amounts.items()
                    if label not in {"股票", "期貨"})
    holdings.extend(Holding(code, f"{name} {months[code]}", "futures", None)
                    for code, name, _, _ in tables["期貨"])
    holdings.append(Holding("UNCLASSIFIED", "未分解其他資產及負債", "other", None))
    snapshot = Snapshot("2", etf.symbol,
                        etf.issuer, etf.is_active, day, fetched_at,
                        SOURCE_URL, "official", tuple(holdings),
                        evidence, tuple(notes))
    return snapshot_from_dict(snapshot_to_dict(snapshot))
