"""Qualified 00731 asset API and same-day export, with bounded date lookup."""

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re
from urllib.parse import urlencode

from .issuers._assets import check_stock, nav_number, number, read_json
from .issuers.fhtrust import _workbook_rows
from .models import Holding
from .refresh_contract import resolve_security
from .refresh_inventory import Inventory, SecurityRow


MAX_LOOKBACK_DAYS = 3
API = "https://www.fhtrust.com.tw/api/assets"
NAME = "復華富時台灣高股息低波動基金"
ASSET_KINDS = {"期貨保證金(NTD)": "other",
               "扣除應付買入證券款後現金餘額(NTD)": "cash"}
FIELDS = {"fundID", "twNameFull", "ec002", "etf002", "ec038", "dDate",
          "pcf_FundNav", "pcf_FundQissue", "pcf_Fundpnav", "result", "diff",
          "detail", "summary", "rate"}


def assets_url(day: date) -> str:
    """Use a query date only; the response must independently name its day."""
    return API + "?" + urlencode({"fundID": "ETF08", "qDate": f"{day:%Y/%m/%d}"})


def payload(raw: bytes) -> dict | None:
    """Recognize only the observed explicit all-null empty response."""
    root = read_json(raw)
    if (not isinstance(root, dict) or not isinstance(root.get("result"), list)
            or len(root["result"]) != 1 or type(root.get("status")) is not Decimal
            or root["status"] != 0):
        raise ValueError("Ambiguous FH assets response")
    data = root["result"][0]
    if not isinstance(data, dict) or data.keys() != FIELDS:
        raise ValueError("Unknown or partial FH assets structure")
    if all(v is None for v in data.values()) and root.get("status") == 0:
        return None
    if (data["fundID"], data["etf002"], data["twNameFull"]) != (
            "ETF08", "00731", NAME):
        raise ValueError("FH assets fund identity mismatch")
    if data["diff"] != []:
        raise ValueError("Unqualified FH assets difference records")
    holding_day(data)
    return data


def holding_day(data: dict) -> date:
    """Require the actual source date; never replace it with the query clock."""
    stamp = data.get("dDate")
    if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", stamp):
        raise ValueError("Missing or invalid actual FH assets date")
    return date.fromisoformat(stamp.replace("/", "-"))


def acquire(session, today: date) -> dict[str, bytes]:
    """Start yesterday for the morning job; stop after three empty days.

    A transport, identity, schema, date or validation error is never retried.
    At most three API calls and one export share the existing session budget.
    """
    for offset in range(1, MAX_LOOKBACK_DAYS + 1):
        query_day = today - timedelta(days=offset)
        raw = session.get(assets_url(query_day))
        data = payload(raw)
        if data is None:
            continue
        if holding_day(data) != query_day:
            raise ValueError("FH actual date disagrees with requested date")
        workbook = session.get("https://www.fhtrust.com.tw/api/assetsExcel/ETF08/"
                               + query_day.strftime("%Y%m%d"))
        read_inventory(raw, workbook)
        return {"assets": raw, "workbook": workbook}
    raise ValueError("FH assets unavailable within three-day lookup budget")


def _percent(value, amount: Decimal, nav: Decimal, *, stock=False) -> Decimal:
    """Validate the disclosed unit and retain native rounded percentages."""
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d{3}%", value):
        raise ValueError("Invalid FH native percentage unit")
    weight = number(value[:-1])
    # Stocks have three-place precision; other rows display two-place values
    # padded to three. Their canonical book weights use disclosed amount/NAV.
    digits = Decimal("0.001") if stock else Decimal("0.01")
    expected = (amount / nav * 100).quantize(digits, rounding=ROUND_HALF_UP)
    if weight != expected or not 0 <= weight <= 100:
        raise ValueError("FH percentage does not reconcile to whole-fund NAV")
    return weight / 100


def read_inventory(api: bytes, workbook: bytes) -> Inventory:
    """Require complete same-day native holdings and qualified stock markets."""
    data = payload(api)
    if data is None:
        raise ValueError("Empty FH assets cannot become a snapshot")
    day, nav = holding_day(data), nav_number(data["pcf_FundNav"])
    table = _workbook_rows(workbook)
    if (len(table) < 12 or not isinstance(table[0][0], str)
            or NAME not in table[0][0] or "證劵代碼：00731" not in table[0][0]
            or table[2][0] != f"日期: {day:%Y/%m/%d}"
            or table[3][0] != "基金資產淨值" or nav_number(table[4][0]) != nav
            or table[10] != ["證券代號", "證券名稱", "股數", "金額", "權重(%)"]):
        raise ValueError("FH workbook identity/date/NAV/header mismatch")
    details, summaries, totals = data["detail"], data["summary"], data["result"]
    if (not isinstance(details, list) or not 1 <= len(details) <= 1000
            or not isinstance(summaries, list) or not isinstance(totals, list)):
        raise ValueError("Missing complete FH asset inventory")
    workbook_rows = {"股票": [], "期貨": []}
    section = "股票"
    for row in table[11:]:
        if not any(v is not None for v in row):
            continue
        if row == ["期貨代號", "期貨名稱", "口數", "金額", "權重(%)"] and section == "股票":
            section = "期貨"
            continue
        if len(row) != 5 or not isinstance(row[0], str):
            raise ValueError("Unknown FH workbook section or row")
        workbook_rows[section].append(row)
    stocks, assets, seen = [], [], set()
    api_rows = {"股票": [], "期貨": []}
    amounts = {"股票": Decimal(0), "期貨": Decimal(0), "其他資產": Decimal(0)}
    other_names = set()
    for row in details:
        if not isinstance(row, dict) or row.get("ftype") not in amounts:
            raise ValueError("Unknown FH asset class")
        kind, code, name = row["ftype"], row["stockid"], row["stockname"]
        if row["qshareCur"] != "NTD" or number(row["accint"]) != 0:
            raise ValueError("Unqualified FH currency or accrued interest")
        amount, shares = number(row["mvalue"]), number(row["qshare"])
        if amount < 0 or shares < 0 or shares != shares.to_integral():
            raise ValueError("Invalid FH amount or quantity")
        weight = _percent(row["prate_addaccint"], amount, nav, stock=kind == "股票")
        amounts[kind] += amount
        if kind in api_rows:
            if (kind, code) in seen:
                raise ValueError("Duplicate FH security")
            seen.add((kind, code))
            api_rows[kind].append([code, name, shares, amount, weight])
        if kind == "股票":
            check_stock(code, name, shares, amount, nav)
            if resolve_security("00731", code, name)[1] not in {"TWSE", "TPEx"}:
                raise ValueError("Unqualified FH stock listing market")
            stocks.append(SecurityRow(code, name, shares, weight))
        elif kind == "期貨":
            if not isinstance(code, str) or not re.fullmatch(r"WTX\d{6}", code):
                raise ValueError("Unqualified FH future identity")
            assets.append(Holding(code, name, "futures", None))
        else:
            if name not in ASSET_KINDS or name in other_names or code != "" or shares != 0:
                raise ValueError("Unqualified or duplicate FH book asset")
            other_names.add(name)
            assets.append(Holding(name, name, ASSET_KINDS[name], float(amount / nav)))
    if not stocks or other_names != ASSET_KINDS.keys():
        raise ValueError("Missing FH stocks or book assets")
    for kind in api_rows:
        normalized = []
        for row in workbook_rows[kind]:
            normalized.append([row[0], row[1], number(row[2]), number(row[3]),
                               _percent(row[4], number(row[3]), nav, stock=kind == "股票")])
        if normalized != api_rows[kind]:
            raise ValueError("FH API and workbook holdings differ")
    reported, notes = {}, []
    for row in summaries:
        kind = row.get("ftype")
        if kind not in amounts or kind in reported:
            raise ValueError("Unknown or duplicate FH summary")
        reported[kind] = number(row["totValue"])
        if kind == "股票":
            ratio = row["totRatio"]
            if not isinstance(ratio, str) or not re.fullmatch(r"\d+\.\d{3}%", ratio):
                raise ValueError("Invalid FH summary percentage unit")
            expected_ratio = reported[kind] / nav * 100
            delta = abs(number(ratio[:-1]) - expected_ratio)
            # The source total is not always the rounded amount/NAV ratio.
            # Bound the discrepancy by the aggregate row rounding envelope,
            # require exact monetary reconciliation, and disclose it verbatim.
            if delta > Decimal("0.0005") * len(stocks):
                raise ValueError("Material FH summary percentage discrepancy")
            if delta > Decimal("0.0005"):
                notes.append(f"FH stock summary {ratio} differs from amount/NAV "
                             f"{expected_ratio:.6f}%; native row weights retained")
        else:
            _percent(row["totRatio"], reported[kind], nav)
    if reported != amounts:
        raise ValueError("Incomplete FH inventory or inconsistent summary")
    total_rows = {}
    for row in totals:
        key = (row.get("ftype"), row.get("itemName"))
        if key in total_rows:
            raise ValueError("Duplicate FH asset total")
        total_rows[key] = number(row["tot_mvalue"])
    expected = {("股票", "股票"): amounts["股票"], ("期貨", "期貨"): amounts["期貨"]}
    expected.update({("其他資產", row["stockname"]): number(row["mvalue"])
                     for row in details if row["ftype"] == "其他資產"})
    residual = nav - amounts["股票"] - amounts["其他資產"]
    if total_rows != expected or not 0 <= residual <= nav * Decimal("0.001"):
        raise ValueError("FH whole-fund book totals disagree")
    return Inventory(day, nav, stocks, amounts["股票"], assets, tuple(notes))
