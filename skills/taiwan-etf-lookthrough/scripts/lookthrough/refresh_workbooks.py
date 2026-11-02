"""Read additional official full-portfolio workbooks and native asset JSON."""

from datetime import date
from dataclasses import replace
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
    total, notes = None, []
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
                # After-payables cash is already net; keep the source sign.
                # Only these exact labels establish a TWD valuation amount.
                # Foreign/missing currency cannot be divided by the TWD NAV,
                # even when its native amount is zero or a percent is printed.
                known_ntd = label in {
                    "扣除應付買入證券款後現金餘額(NTD)",
                    "期貨保證金(NTD)", "應付申購預收款(NTD)",
                }
                cash = re.fullmatch(
                    r"扣除應付買入證券款後現金餘額\([A-Z]{3}\)", label)
                weight = float(amount / nav) if known_ntd else None
                if not known_ntd:
                    notes.append(f"FH {label}: unqualified book currency or "
                                 "category; NAV weight unknown")
                assets.append(Holding(
                    label, label, "cash" if cash else "other", weight))
    if not stocks or total is None:
        raise ValueError("Missing FH complete stock inventory")
    return Inventory(day, nav, stocks, total, assets, tuple(notes))


def read_fhtrust_00924_native(
    page: bytes, workbook: bytes, assets: bytes,
) -> Inventory:
    """Read the reviewed 2026-10-29 inventory without qualifying its markets.

    This explicit offline entry is not a product registration or collector.
    The 146-row, three-place percentage contract is source/date limited.
    Native weights are retained; independent summary discrepancies are notes,
    not a reason to normalize weights or demand complete fund accounting.
    """
    inventory = read_fhtrust(page, workbook, "00924", "ETF20")
    root = read_json(assets)
    if (not isinstance(root, dict) or type(root.get("status")) is not Decimal
            or root["status"] != 0
            or not isinstance(root.get("result"), list)
            or len(root["result"]) != 1):
        raise ValueError("Invalid 00924 asset response")
    data = root["result"][0]
    if (not isinstance(data, dict)
            or data.get("fundID") != "ETF20"
            or data.get("etf002") != "00924"
            or data.get("twNameFull") != "復華美國標普500成長ETF基金"
            or data.get("dDate") != "2026/10/29"
            or inventory.day != date(2026, 10, 29)
            or nav_number(data.get("pcf_FundNav")) != inventory.nav
            or data.get("diff") != []):
        raise ValueError("00924 identity/date/NAV contract mismatch")
    details, summaries = data.get("detail"), data.get("summary")
    if (not isinstance(details, list) or not isinstance(summaries, list)
            or any(not isinstance(row, dict)
                   or row.get("ftype") not in {"股票", "期貨", "其他資產"}
                   for row in details)):
        raise ValueError("Invalid 00924 asset classes")
    stocks = [row for row in details if row["ftype"] == "股票"]
    totals = [row for row in summaries
              if isinstance(row, dict) and row.get("ftype") == "股票"]
    if len(stocks) != 146 or len(inventory.stocks) != 146 or len(totals) != 1:
        raise ValueError("Incomplete 00924 stock inventory")
    table = _workbook_rows(workbook)
    if table[10] != ["證券代號", "證券名稱", "股數", "金額", "權重(%)"]:
        raise ValueError("00924 workbook stock header mismatch")
    book = []
    for row in table[11:]:
        if not any(value is not None for value in row):
            continue
        if row == ["期貨代號", "期貨名稱", "口數", "金額", "權重(%)"]:
            break
        book.append(row)
    if len(book) != 146:
        raise ValueError("Incomplete 00924 workbook stocks")

    def native_percent(value: str) -> Decimal:
        """Read an explicit native three-decimal percentage."""
        if (not isinstance(value, str)
                or not re.fullmatch(r"\d+\.\d{3}%", value)):
            raise ValueError(
                "00924 requires explicit three-place percent units")
        return _percentage(value)

    seen, valued, amounts = set(), [], []
    for row, exported, prior in zip(stocks, book, inventory.stocks):
        code, name = row.get("stockid"), row.get("stockname")
        shares, amount = number(row.get("qshare")), number(row.get("mvalue"))
        weight = native_percent(row.get("prate_addaccint"))
        if (not isinstance(code, str) or not code.strip() or code in seen
                or not isinstance(name, str) or not name.strip()
                or row.get("qshareCur") != "USD"
                or number(row.get("accint")) != 0
                or shares < 0 or shares != shares.to_integral()
                or not 0 <= amount <= inventory.nav or weight < 0):
            raise ValueError("Invalid 00924 stock identity/quantity/value")
        if (len(exported) != 5 or exported[:2] != [code, name]
                or number(exported[2]) != shares
                or number(exported[3]) != amount
                or native_percent(exported[4]) != weight
                or (prior.code, prior.name, prior.shares)
                != (code, name, shares)):
            raise ValueError("00924 API/workbook stock disagreement")
        if abs(amount / inventory.nav - weight) > Decimal("0.000005"):
            raise ValueError(
                "00924 row exceeds half-unit percentage tolerance")
        seen.add(code)
        amounts.append(amount)
        valued.append(replace(prior, nav_weight=weight))
    total = number(totals[0].get("totValue"))
    if sum(amounts) != total or total != inventory.stock_total:
        raise ValueError("00924 stock book total mismatch")
    aggregate = native_percent(totals[0].get("totRatio"))
    notes = list(inventory.quality_notes)
    if abs(aggregate - total / inventory.nav) > Decimal("0.000005"):
        notes.append("00924 independent stock summary "
                     f"{totals[0]['totRatio']} "
                     "differs from amount/NAV "
                     f"{total / inventory.nav * 100}%; "
                     "cause unknown; native row weights retained")
    notes.append("00924 stock market identities require independent review; "
                 "this inventory does not qualify a canonical candidate")
    return replace(inventory, stocks=valued, quality_notes=tuple(notes))
