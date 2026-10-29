"""Extract actual inventories from the reviewed additional official routes."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import re
from typing import TypeVar

from .issuers._assets import nav_number, number, read_json
from .issuers._tables import by_id, has_class, one, read_html, rows
from .issuers.mega import _desktop, _mobile, STOCK_HEADER
from .models import Holding


T = TypeVar("T")


def _single(values: list[T], label: str) -> T:
    """Require one extracted value rather than an HTML node."""
    if len(values) != 1:
        raise ValueError(f"Missing or ambiguous {label}")
    return values[0]


@dataclass(frozen=True)
class SecurityRow:
    """Keep security identity, actual shares and optional NAV weight."""

    code: str
    name: str
    shares: Decimal
    nav_weight: Decimal | None = None


@dataclass
class Inventory:
    """Keep dated actual equities separate from non-equity book assets."""

    day: date
    nav: Decimal | None
    stocks: list[SecurityRow]
    stock_total: Decimal | None = None
    assets: list[Holding] = field(default_factory=list)
    quality_notes: tuple[str, ...] = ()


def _match(pattern: str, text: str) -> re.Match:
    """Require an unambiguous disclosure instead of guessing absent fields."""
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous actual inventory field")
    return matches[0]


def _day(text: str) -> date:
    """Parse a printed ISO-style date without consulting the request clock."""
    return date.fromisoformat(text.replace("/", "-"))


def _stock_rows(records: list[list[str]]) -> list[SecurityRow]:
    """Require every source stock row and preserve its original symbol."""
    result = []
    seen: set[str] = set()
    for row in records:
        if len(row) != 4 or not row[0] or not row[1] or row[0] in seen:
            raise ValueError("Invalid or duplicate inventory security")
        shares = number(row[2])
        if shares < 0 or shares != shares.to_integral():
            raise ValueError("Invalid actual stock quantity")
        # Native percentages are not assumed to have whole-NAV denominators.
        percentage = number(row[3].removesuffix("%"))
        if not 0 <= percentage <= 100:
            raise ValueError("Invalid disclosed stock percentage unit")
        result.append(SecurityRow(row[0], row[1], shares))
        seen.add(row[0])
    if not result:
        raise ValueError("Empty actual stock inventory")
    return result


def read_tsit(raw: bytes, symbol: str) -> Inventory:
    """Read actual Taishin shares; do not treat futures weights as stocks."""
    root = read_html(raw)
    ids = [n.attrs.get("value") for n in root.find("input")
           if n.attrs.get("id") == "ETF_ID"]
    if not ids or any(value != symbol for value in ids):
        raise ValueError("Taishin product identity mismatch")
    title = one([n for n in root.find("h3")
                 if has_class(n, "item-title")], "Taishin fund identity")
    if symbol not in title.text:
        raise ValueError("Missing Taishin visible fund identity")
    stamp = by_id(root, "NAV_DATE").attrs.get("value", "")
    day = _day(_match(r"^(\d{4}/\d{2}/\d{2}) 上午 12:00:00$", stamp)[1])
    panel = by_id(root, "paneFive1")
    tables = [rows(t) for t in panel.find("table")]
    nav_rows = _single([t for t in tables if t[0][0] == "基金淨資產價值(元)"],
                       "Taishin fund NAV")
    if not nav_rows[0][1].startswith("TWD "):
        raise ValueError("Taishin NAV currency is not TWD")
    nav = nav_number(nav_rows[0][1][4:])
    stamps = re.findall(r"(\d{4}/\d{2}/\d{2})每基數", panel.text)
    if not stamps or any(_day(s) != day for s in stamps):
        raise ValueError("Taishin holdings and NAV dates disagree")
    table = _single([t for t in tables if t[0] ==
                     ["代號", "名稱", "股數", "持股權重"]], "Taishin stocks")
    if table[-1][0] != "股票合計":
        raise ValueError("Missing Taishin complete stock total")
    stocks = _stock_rows(table[1:-1])
    result = Inventory(day, nav, stocks)
    current = [t for t in tables if t[0] ==
               ["流動資產總額(計價幣)", "流動比率(%)"]]
    if len(current) != 1 or len(current[0]) != 2:
        raise ValueError("Missing Taishin current asset disclosure")
    amount = number(current[0][1][0])
    result.assets.append(Holding("current_assets", "流動資產總額", "other",
                                 float(amount / nav)))
    for table in tables:
        if table[0] == ["期貨名稱", "契約年月", "口數", "持股權重"]:
            for row in table[1:-1]:
                if len(row) != 4:
                    raise ValueError("Malformed Taishin futures row")
                result.assets.append(Holding(
                    row[0] + ":" + row[1], row[0], "futures", None))
    return result


def read_sinopac(raw: bytes, symbol: str) -> Inventory:
    """Read the actual asset date and matching desktop/mobile inventories."""
    root = read_html(raw)
    identities = [n.text for n in root.find("div")
                  if has_class(n, "cash_title-s") and symbol in n.text]
    if len(identities) != 1 or f"證劵代碼：{symbol}" not in identities[0]:
        raise ValueError("Sinopac visible product identity mismatch")
    stamp = _single([n.text for n in root.find("p")
                     if n.text.startswith("資料日期")], "Sinopac asset date")
    day = _day(_match(r"^資料日期：(\d{4}/\d{2}/\d{2})$", stamp)[1])
    nav_cell = one([n for n in root.find("div")
                    if has_class(n, "fl_cell") and any(
                        c.text.endswith("基金淨資產價值(元)")
                        for c in n.find("h4"))], "Sinopac dated NAV")
    label = one(nav_cell.find("h4"), "Sinopac NAV label").text
    if label != f"{day:%Y/%m/%d} 基金淨資產價值(元)":
        raise ValueError("Sinopac actual asset and NAV dates disagree")
    amount = one(nav_cell.find("p"), "Sinopac NAV amount").text
    if not amount.startswith("NT$ "):
        raise ValueError("Missing Sinopac TWD NAV")
    header = ["證券代碼", "證券名稱", "股數", "佔基金淨資產之權重(%)"]
    desktop, mobile = [], []
    for table in root.find("table"):
        values = rows(table)
        if values[0] == header and has_class(table, "tab_sh-w"):
            desktop.append(values[1:])
        elif (has_class(table, "tab_sh-m") and len(values) == 4
              and [r[0] for r in values] == header):
            if any(len(r) != 2 for r in values):
                raise ValueError("Malformed Sinopac mobile equity")
            mobile.append([r[1] for r in values])
    records = _single(desktop, "Sinopac complete desktop inventory")
    if records != mobile:
        raise ValueError("Sinopac desktop/mobile inventories disagree")
    if "ETF系列基金之權重(%)為相對基金淨資產價值之比重" not in (
            by_id(root, "dvMemo").text):
        raise ValueError("Missing Sinopac explicit whole-NAV convention")
    stocks = [SecurityRow(r.code, r.name, r.shares, number(source[3]) / 100)
              for r, source in zip(_stock_rows(records), records,
                                   strict=True)]
    return Inventory(day, nav_number(amount[4:]), stocks)


def read_kgi(raw: bytes, symbol: str) -> Inventory:
    """Use the full holdings tab, never the next-day creation basket."""
    root = read_html(raw)
    titles = [n.text for n in root.find("h2") if n.text]
    if not any(f"({symbol} " in title for title in titles):
        raise ValueError("KGI visible fund identity mismatch")
    panel = by_id(root, "FundDetail-06")
    day = _day(_match(r"持股比重 \((\d{4}/\d{2}/\d{2})\)", panel.text)[1])
    nav = nav_number(_match(
        r"基金淨值資產價值 TWD\$([\d,]+)(?: |$)", panel.text)[1])
    header = ["股票代號", "股票名稱", "股數", "權重(%)"]
    stocks = [rows(t) for t in panel.find("table") if rows(t)[0] == header]
    if len(stocks) != 2 or stocks[0] != stocks[1]:
        raise ValueError("KGI desktop/mobile stocks disagree")
    total = _match(r"股票 TWD\$([\d,]+)(?: |$)", panel.text)[1]
    result = Inventory(day, nav, _stock_rows(stocks[0][1:]), number(total))
    for label, kind in [("現金", "cash"), ("保證金", "other")]:
        value = re.search(label + r" TWD\$([\d,]+)(?: |$)", panel.text)
        if value:
            result.assets.append(Holding(label, label, kind,
                                         float(number(value[1]) / nav)))
    return result


def read_mega(raw: bytes, symbol: str, fund_id: str) -> Inventory:
    """Read Mega's actual assets and duplicate full stock views."""
    root = read_html(raw)
    selected = one([n for n in by_id(root, "fund_id").find("option")
                    if "selected" in n.attrs], "Mega selected fund")
    if selected.attrs.get("value") != fund_id:
        raise ValueError("Mega fund identity mismatch")
    panel = by_id(root, "asset_div")
    text = panel.text.split("指數分配權重")[0]
    day = _day(_match(r"資料來源：兆豐投信，(\d{4}/\d{2}/\d{2})", text)[1])
    nav = nav_number(_match(r"淨資產價值 ([\d,]+) ", text)[1])
    raw_section = raw.decode("utf-8-sig").split('id="asset_div"', 1)[1]
    if not re.search(r'淨資產幣別</div>\s*<div class="st-unit">\s*TWD\s*</div>',
                     raw_section[:1600]):
        raise ValueError("Missing Mega NAV currency")
    records = _desktop(root, 1, STOCK_HEADER)
    if records != _mobile(root, 1, STOCK_HEADER):
        raise ValueError("Mega desktop/mobile stocks disagree")
    total = number(_match(r"股票 \( [\d.]+% \) TWD\$ ([\d,]+)", text)[1])
    result = Inventory(day, nav, _stock_rows(records), total)
    for label, kind in [("現金/存款", "cash"), ("保證金", "other")]:
        found = re.search(re.escape(label) + r" TWD\$ ([\d,]+)", text)
        if found:
            result.assets.append(Holding(label, label, kind,
                                         float(number(found[1]) / nav)))
    return result


def read_union(raw: bytes, symbol: str) -> Inventory:
    """Read Union's dated actual list without inferring unknown NAV weights."""
    root = read_html(raw)
    if not any(f"( {symbol} )" in n.text for n in root.find("h2")):
        raise ValueError("Union visible fund identity mismatch")
    panel = by_id(root, "list-pbl")
    day = _day(_match(r"資料日期： (\d{4}-\d{2}-\d{2})", panel.text)[1])
    tables = [rows(t) for t in panel.find("table")]
    stocks = _single([t for t in tables if t[0] ==
                      ["股票代號", "股票名稱", "股數", "權重(%)"]], "Union stocks")
    # The research qualifies the complete foreign inventory, not its weights.
    return Inventory(day, None, _stock_rows(stocks[1:]))


def read_cathay(assets: bytes, stocks: bytes, balance: bytes,
                requested_day: date, *, native_nav: bool = False) -> Inventory:
    """Read complete dated Cathay API records using the requested NAV day."""
    payloads = [read_json(raw) for raw in (assets, stocks, balance)]
    if any(p.get("success") is not True or p.get("returnCode") != "2000"
           for p in payloads):
        raise ValueError("Cathay business response failed")
    meta, equities, balances = [p["result"] for p in payloads]
    day = _day(meta["preDate"])
    if day != requested_day:
        raise ValueError("Cathay requested/returned actual date mismatch")
    nav = nav_number(meta["fundNav"])
    records = [[r["stockCode"], r["stockName"], r["volumn"], r["weights"]]
               for r in equities]
    result = Inventory(day, nav, _stock_rows(records))
    if native_nav:
        result.stocks = [SecurityRow(r.code, r.name, r.shares,
                                     number(source[3].removesuffix("%")) / 100)
                         for r, source in zip(result.stocks, records,
                                              strict=True)]
    for index, row in enumerate(balances):
        label, amount = row["item"], row["amount"]
        if label == "股票":
            result.stock_total = number(_match(r"^NT\$([\d,.]+)$", amount)[1])
        elif "期貨" in label:
            result.assets.append(Holding(
                f"future:{index}", label, "futures", None))
        else:
            matched = re.fullmatch(r"(?:NT\$|\(TWD\) \$ )(-?[\d,.]+)", amount)
            weight = float(number(matched[1]) / nav) if matched else None
            # A payable's economic sign is independent of its display sign.
            if weight is not None and "應付款" in label:
                weight = -abs(weight)
            result.assets.append(Holding(
                f"balance:{index}", label + " " + amount,
                "cash" if label == "現金" else "other", weight))
    return result
