"""Offline candidate parser for Allianz's complete public fund-assets table.

Not registered for daily acquisition. The source UI labels FundAsset.NavDate
as the assets data date and Aum as whole-fund net assets. PCFDate is not used
as a holdings date. Preserve native stock percentages and unknown residuals.
"""
import base64
import binascii
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re
import unicodedata

from ._assets import check_stock, nav_number, number, read_json
from ..models import EquityEvidence, Holding, Snapshot, snapshot_from_dict, snapshot_to_dict
from ..refresh_contract import directory

SOURCE_URL = "https://etf.allianzgi.com.tw/webapi/api/Fund/GetFundAssets"
_KIND = "allianz-native-assets-candidate-v1"
_FUNDS = {
    "00984A": ("E0001", "安聯台灣高息成長主動式ETF證券投資信託基金",
               "TW00000984A6", "2025/07/14"),
    "00993A": ("E0002", "安聯台灣主動式ETF證券投資信託基金",
               "TW00000993A7", "2026/02/03"),
}
_PARTS = ("overview", "assets", "detail")


def capture(overview: bytes, assets: bytes, detail: bytes) -> bytes:
    """Preserve three exact public responses; never save session cookies."""
    parts = (overview, assets, detail)
    for raw in parts:
        read_json(raw)
    return json.dumps({"capture_kind": _KIND, **{
        key: base64.b64encode(raw).decode("ascii")
        for key, raw in zip(_PARTS, parts)}}).encode()


def _response(raw: bytes, count: int):
    value = read_json(raw)
    if (not isinstance(value, dict) or set(value) != {
            "TotalPages", "TotalItems", "Entries", "Message", "StatusCode", "RedirectUrl"}
            or number(value["StatusCode"], native=True) != 0
            or number(value["TotalPages"], native=True) != -1
            or number(value["TotalItems"], native=True) != count
            or value["RedirectUrl"] is not None or value["Message"] != ""):
        raise ValueError("Invalid Allianz public response/pagination")
    return value["Entries"]


def _day(text):
    if not isinstance(text, str) or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", text):
        raise ValueError("Missing Allianz actual NAV/data date")
    return date.fromisoformat(text.replace("/", "-"))


def _identity(row, symbol="00984A"):
    spec = _FUNDS.get(symbol)
    if (spec is None or not isinstance(row, dict) or row.get("CFundNo") != spec[0]
            or row.get("CSecuritiesCode") != symbol
            or not isinstance(row.get("CFullName"), str)
            or unicodedata.normalize("NFKC", row["CFullName"]) != spec[1]):
        raise ValueError("Wrong Allianz fund identity")


def _futures(summary, table):
    """Retain observed derivative rows without interpreting their percentages."""
    columns = ["序號", "期貨代號", "期貨名稱", "口數", "權重(%)", "契約年月"]
    if (not isinstance(summary, list) or len(summary) != 4
            or summary[0] != "期貨" or summary[2] != "TWD"
            or not isinstance(summary[1], str) or not summary[1].startswith("TWD$")
            or number(summary[1][4:]) != number(summary[3])
            or table["TableTitle"] != "期貨"
            or table["Columns"] != [{"Name": n, "TextAlign": "center"} for n in columns]
            or not isinstance(table["Rows"], list) or not 1 <= len(table["Rows"]) <= 1000):
        raise ValueError("Changed Allianz futures disclosure schema")
    holdings, notes, seen = [], [], set()
    for i, row in enumerate(table["Rows"], 1):
        if (not isinstance(row, list) or len(row) != 6
                or any(not isinstance(v, str) for v in row) or row[0] != str(i)):
            raise ValueError("Changed Allianz futures row")
        _, code, name, quantity, percent, expiry = row
        if (not re.fullmatch(r"[A-Z][A-Z0-9]{0,15}", code) or not name.strip()
                or not re.fullmatch(r"\d{4}/\d{2}", expiry)
                or not re.fullmatch(r"-?\d+\.\d{2}%", percent)
                or number(quantity) != number(quantity).to_integral()):
            raise ValueError("Invalid Allianz futures disclosure")
        _day(expiry + "/01")
        key = code + "-" + expiry.replace("/", "")
        if key in seen:
            raise ValueError("Duplicate Allianz futures contract")
        seen.add(key)
        holdings.append(Holding(key, name, "futures", None, None))
        notes.append(f"官方期貨列 {code} {expiry}：{quantity} 口、原公布 {percent}；"
                     "分母與名目／帳面語意未驗，不作 NAV 權重或衍生曝險。")
    notes.append(f"官方期貨摘要數字 {summary[3]} TWD 保留原檔；未當作已驗帳面價值，也未用來補平非股票差額。")
    return holdings, notes


def parse(raw: bytes, etf, fetched_at: str) -> Snapshot:
    """Qualify disclosed spot-equity exposure, not full balance-sheet audit."""
    if (etf.symbol not in _FUNDS or etf.issuer != "allianz"
            or etf.is_active is not True or etf.source_url != SOURCE_URL):
        raise ValueError("Unqualified Allianz candidate request")
    package = read_json(raw)
    if (not isinstance(package, dict) or set(package) != {"capture_kind", *_PARTS}
            or package["capture_kind"] != _KIND):
        raise ValueError("Expected Allianz three-response capture")
    try:
        parts = tuple(base64.b64decode(package[k], validate=True) for k in _PARTS)
    except (TypeError, binascii.Error) as error:
        raise ValueError("Malformed Allianz capture") from error
    overview, assets, detail = (_response(b, n) for b, n in zip(parts, (1, 0, 0)))
    if not isinstance(overview, list) or len(overview) != 1:
        raise ValueError("Allianz overview is not unique")
    _identity(overview[0], etf.symbol)
    _identity(detail, etf.symbol)
    spec = _FUNDS[etf.symbol]
    if (detail.get("CIsincode") != spec[2]
            or detail.get("CCgResCountryStr") != "台灣"
            or detail.get("CCgCurrencyTypeStr") != "新台幣"
            or detail.get("CCgFundTypeStr") != "主動式交易所交易基金"
            or detail.get("CListingDt") != spec[3]):
        raise ValueError("Changed Allianz domestic product/currency scope")
    if (not isinstance(assets, dict) or set(assets) != {"FundID", "Data"}
            or assets["FundID"] != detail["CFundNo"]):
        raise ValueError("Wrong Allianz assets identity")
    data = assets["Data"]
    if not isinstance(data, dict) or set(data) != {"FundAsset", "Table"}:
        raise ValueError("Changed Allianz assets schema")
    fund = data["FundAsset"]
    if not isinstance(fund, dict) or set(fund) != {"Aum", "Units", "Nav", "NavDate", "PCFDate"}:
        raise ValueError("Changed Allianz NAV schema")
    day = _day(fund["NavDate"])
    if (_day(detail.get("LastNavDate")) != day
            or _day(overview[0].get("CLatestNAVDateStr")) != day
            or overview[0].get("CLatestNAVDate") != f"{day}T00:00:00"):
        raise ValueError("Mixed Allianz actual dates")
    stamp = datetime.fromisoformat(fetched_at)
    if ("T" not in fetched_at or stamp.utcoffset() != timedelta(0)
            or day > stamp.astimezone(timezone(timedelta(hours=8))).date()):
        raise ValueError("Allianz holdings postdate capture or capture lacks UTC")
    # Validate the separate PCF date, but do not promote it to holdings date.
    if _day(fund["PCFDate"]) < day:
        raise ValueError("Invalid Allianz PCF date context")
    nav, units, per_unit = (nav_number(fund[k]) for k in ("Aum", "Units", "Nav"))
    if (abs(nav / units - per_unit) > Decimal("0.005")
            or nav_number(overview[0].get("CLatestNAV"), native=True) != per_unit
            or nav_number(overview[0].get("CLatestNAVStr")) != per_unit):
        raise ValueError("Inconsistent Allianz NAV/units")
    tables = data["Table"]
    allowed_counts = (2, 3) if etf.symbol == "00993A" else (2,)
    if not isinstance(tables, list) or len(tables) not in allowed_counts:
        raise ValueError("Changed Allianz asset categories")
    summary, stocks = tables[:2]
    for table in tables:
        if not isinstance(table, dict) or set(table) != {"TableTitle", "Columns", "Rows"}:
            raise ValueError("Changed Allianz table schema")
    if (summary["TableTitle"] != ""
            or summary["Columns"] != [{"Name": None, "TextAlign": None}]
            or not isinstance(summary["Rows"], list) or len(summary["Rows"]) != len(tables)
            or summary["Rows"][0] != [" ", " ", " ", " "]):
        raise ValueError("Changed Allianz summary table")
    amounts = summary["Rows"][1]
    if (not isinstance(amounts, list) or len(amounts) != 4 or amounts[0] != "股票"
            or amounts[2] != "TWD" or not isinstance(amounts[1], str)
            or not amounts[1].startswith("TWD$")):
        raise ValueError("Unknown Allianz stock amount/currency")
    stock_value = nav_number(amounts[3])
    if stock_value > nav or number(amounts[1][4:]) != stock_value:
        raise ValueError("Inconsistent Allianz stock amount")
    title = stocks["TableTitle"]
    match = re.fullmatch(r"股票 \((\d+\.\d{2})%\)", title) if isinstance(title, str) else None
    expected_columns = ["序號", "股票代號", "股票名稱", "股數", "權重(%)"]
    if (match is None or stocks["Columns"] != [
            {"Name": name, "TextAlign": "center"} for name in expected_columns]):
        raise ValueError("Unknown Allianz stock weight unit/schema")
    category_weight = Decimal(match[1])
    if (stock_value / nav * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != category_weight:
        raise ValueError("Allianz stock percentage is not whole-fund NAV")
    rows = stocks["Rows"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError("Empty/unbounded Allianz stock table")
    roster = directory()
    holdings, seen, differences = [], set(), 0
    name_codes = {}
    # Reject a source name that exactly identifies a different roster company.
    for company in roster._companies.values():
        name_codes.setdefault(company.name.replace("*", ""), set()).add(company.code)
    total = Decimal(0)
    for index, row in enumerate(rows, 1):
        if (not isinstance(row, list) or len(row) != 5
                or any(not isinstance(v, str) for v in row) or row[0] != str(index)):
            raise ValueError("Truncated/changed Allianz stock row sequence")
        _, code, name, quantity, percent = row
        check_stock(code, name, number(quantity), Decimal(0), nav)
        company = roster.get(code)
        if (code in seen or company is None or company.market not in {"TWSE", "TPEx"}
                or re.search(r"\b(?:ADR|ADS|NYSE|NASDAQ|HKEX)\b|存託憑證|美股|港股", name, re.I)
                or name_codes.get(name.replace("*", ""), {code}) != {code}):
            raise ValueError("Unknown/duplicate/contradictory Allianz market identity")
        if not re.fullmatch(r"\d+\.\d{2}%", percent):
            raise ValueError("Invalid Allianz native percentage")
        weight = Decimal(percent[:-1])
        if not 0 <= weight <= 100:
            raise ValueError("Allianz stock weight outside NAV range")
        seen.add(code)
        total += weight
        differences += name.replace("*", "") != company.name.replace("*", "")
        holdings.append(Holding(code, name, "stock", float(weight / 100), market=company.market))
    # No independent row-count API exists. For this reviewed source contract,
    # require the entire sequential list to reconcile to the printed aggregate.
    # A future differently rounded schema needs review, not silent relaxation.
    if total != category_weight:
        raise ValueError("Incomplete Allianz stock percentages/aggregate mismatch")
    future_holdings, future_notes = (
        _futures(summary["Rows"][2], tables[2]) if len(tables) == 3 else ([], []))
    residual = nav - stock_value
    notes = (
        f"官方基金資產資料日 {day}；PCFDate {fund['PCFDate']} 不作持股日。",
        f"逐股原生整基金 NAV 百分點，未重分配；{differences} 列全名／簡稱差異保留原名；名錄 {roster.as_of}。",
        f"NAV 減股票市值 {residual} TWD，非股票／負債組成未知，不補成現金；不是完整資產負債調節。",
        "完整股票列來自官方API；前十筆只是UI顯示截取。未取得逐股市值獨立對帳，三個API可能共用後端。",
        *future_notes,
    )
    holdings.extend(future_holdings)
    holdings.append(Holding("UNCLASSIFIED", "未分解非股票資產及負債", "other", None))
    evidence = EquityEvidence(True, float(nav), float(stock_value), len(rows),
                              float(stock_value), tuple(hashlib.sha256(b).hexdigest() for b in parts))
    return snapshot_from_dict(snapshot_to_dict(Snapshot(
        "2", etf.symbol, etf.issuer, True, day, fetched_at, SOURCE_URL,
        "official", tuple(holdings), evidence, notes)))
