"""Read additional official full-portfolio workbooks and native asset JSON."""

from datetime import date
from decimal import Decimal
import re

from .issuers._assets import number, read_json, nav_number, asset_day
from .issuers._workbooks import read_sheets
from .issuers.fhtrust import _workbook_rows
from .issuers._tables import read_html, by_id, rows
from .models import Holding
from .refresh_inventory import Inventory, SecurityRow, _match, _day


def _percentage(value: str) -> Decimal:
    """Read explicit percent points, never decimal/fraction guesses."""
    if not isinstance(value, str) or not value.endswith("%"):
        raise ValueError("Missing explicit percentage unit")
    result = number(value[:-1]) / 100
    if not -1 <= result <= 1:
        raise ValueError("Source percentage out of bounds")
    return result


def read_jpmorgan(raw: bytes) -> Inventory:
    """Read 00989A's complete dated stock and cash sheets."""
    sheets = read_sheets(raw)
    if set(sheets) != {"基金資產 - 股票", "現金與約當現金"}:
        raise ValueError("Changed JPMorgan source asset classes")
    days, stocks, assets = [], [], []
    for label, table in sheets.items():
        if (len(table) < 14
                or table[1] != ["摩根大美國領先科技主動式ETF基金"]):
            raise ValueError("JPMorgan product identity mismatch")
        days.append(_day(_match(re.escape(label) +
                                r" \((\d{4}-\d{2}-\d{2})\)", table[0][0])[1]))
        notes = " ".join(str(r[0]) for r in table[5:11] if r)
        if "相對基金淨資產價值之比重" not in notes:
            raise ValueError("Missing JPMorgan explicit NAV weight meaning")
        if label == "基金資產 - 股票":
            if table[12] != ["股票代碼", "股票名稱", "股數", "金額", "權重(%)"]:
                raise ValueError("Changed JPMorgan equity table header")
            for row in table[13:]:
                if len(row) != 5:
                    raise ValueError("Malformed JPMorgan equity row")
                stocks.append(SecurityRow(row[0], row[1], number(row[2]),
                                          _percentage(row[4])))
        else:
            if table[12] != ["名稱", "金額 (TWD)", "權重 (%)"]:
                raise ValueError("Changed JPMorgan cash header")
            for index, row in enumerate(table[13:]):
                if len(row) != 3:
                    raise ValueError("Malformed JPMorgan cash row")
                assets.append(Holding(f"cash:{index}", row[0], "cash",
                                      float(_percentage(row[2]))))
    if len(set(days)) != 1 or not stocks:
        raise ValueError("Incomplete or mixed-date JPMorgan inventory")
    return Inventory(days[0], None, stocks, assets=assets)


def read_ftft(raw: bytes, symbol: str, fund_id: str) -> Inventory:
    """Read same-response TWD security values and the whole fund NAV."""
    payload = read_json(raw)
    if (payload.get("FundID") != fund_id
            or payload.get("StockCode") != symbol):
        raise ValueError("FTFT product identity mismatch")
    stamp = payload["AssetDate"]
    day, nav = asset_day(stamp), nav_number(payload["FundNetAssetValue"])
    stocks, assets, total = [], [], None
    for row in payload["Secs"]:
        if (row.get("FundID") != fund_id or row.get("StockCode") != symbol
                or row.get("AssetDate") != stamp):
            raise ValueError("FTFT nested identity/date mismatch")
        if row.get("Currency") != "TWD":
            raise ValueError("Unqualified FTFT stock valuation currency")
        stocks.append(SecurityRow(
            row["SecuritiesCode"], row["SecuritiesName"], number(
                row["Shares"]),
            number(row["MarketValue"]) / nav))
    for row in payload["Categories"]:
        if (row.get("FundID") != fund_id or row.get("StockCode") != symbol
                or row.get("AssetDate") != stamp
                or row.get("AssetCurrency") != "TWD"):
            raise ValueError("FTFT asset identity/date/currency mismatch")
        label = row["AssetCategory"]
        amount = number(row["AssetValue"])
        if label == "股票":
            total = amount
        elif label in ("現金", "保證金", "申贖應付款"):
            if label == "申贖應付款":
                amount = -abs(amount)
            assets.append(Holding(label, label,
                                  "cash" if label == "現金" else "other",
                                  float(amount / nav)))
        else:
            raise ValueError("Unqualified FTFT asset category")
    if not stocks or total is None:
        raise ValueError("Missing complete FTFT stock inventory")
    for index, row in enumerate(payload.get("Futs", [])):
        assets.append(Holding(f"future:{index}", "期貨", "futures", None))
    return Inventory(day, nav, stocks, total, assets)


def fh_download(page: bytes, fund_id: str) -> tuple[date, str]:
    """Select the single source-linked workbook for the actual asset date."""
    root = read_html(page)
    panel = by_id(root, "etfPanel3")
    links = {n.attrs.get("href", "") for n in panel.find("a")
             if "assetsExcel" in n.attrs.get("href", "")}
    if len(links) != 1:
        raise ValueError("Ambiguous FH actual inventory workbook")
    link = links.pop()
    stamp = _match(r"^/api/assetsExcel/" + re.escape(fund_id)
                   + r"/(\d{8})$", link)[1]
    day = date.fromisoformat(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}")
    dates = re.findall(r"\(日期：(\d{4}/\d{2}/\d{2})\)", panel.text)
    if dates != [day.strftime("%Y/%m/%d")]:
        raise ValueError("FH page and workbook dates disagree")
    return day, "https://www.fhtrust.com.tw" + link


def read_fhtrust(page: bytes, workbook: bytes, symbol: str,
                 fund_id: str) -> Inventory:
    """Read the linked dated complete workbook with independently shown NAV."""
    day, _ = fh_download(page, fund_id)
    root = read_html(page)
    canonical = [n.attrs.get("content") for n in root.find("meta")
                 if n.attrs.get("property") == "og:url"]
    expected_url = "https://www.fhtrust.com.tw/ETF/etf_detail/" + fund_id
    if canonical != [expected_url]:
        raise ValueError("FH product page identity mismatch")
    panel = by_id(root, "etfPanel3")
    nav = nav_number(_match(r"基金資產淨值 NTD ([\d,]+)", panel.text)[1])
    table = _workbook_rows(workbook)
    if (len(table) < 12 or f"證劵代碼：{symbol}" not in str(table[0][0])
            or table[2][0] != f"日期: {day:%Y/%m/%d}"
            or table[3][0] != "基金資產淨值"
            or nav_number(table[4][0]) != nav):
        raise ValueError("FH workbook identity/date/NAV mismatch")
    stocks, assets, future_section = [], [], False
    for row in table[11:]:
        if not any(v is not None for v in row):
            continue
        if "期貨" in str(row[0]) and "名稱" in str(row[1]):
            future_section = True
            continue
        if len(row) != 5 or not isinstance(row[0], str):
            raise ValueError("Malformed FH actual inventory row")
        if future_section:
            assets.append(Holding(row[0], str(row[1]), "futures", None))
        else:
            stocks.append(SecurityRow(row[0], row[1], number(row[2])))
    total = None
    for element in panel.find("table"):
        records = rows(element)
        if records[0] != ["項目", "金額", "權重 (%)"]:
            continue
        for row in records[1:]:
            if len(row) != 3:
                raise ValueError("Malformed FH asset summary")
            label, amount = row[0], number(row[1])
            if label == "股票":
                total = amount
            elif label != "期貨":
                if "應付" in label:
                    amount = -abs(amount)
                assets.append(Holding(label, label,
                                      "cash" if "現金" in label else "other",
                                      float(amount / nav)))
    if not stocks or total is None:
        raise ValueError("Missing FH complete stock inventory")
    return Inventory(day, nav, stocks, total, assets)
