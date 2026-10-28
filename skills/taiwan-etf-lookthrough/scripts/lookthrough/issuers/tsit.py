"""Parse qualified TSIT investment stocks using native same-date NAV shares."""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import TYPE_CHECKING

from ..source_quality import discrepancy
from ..models import (
    Holding, Snapshot, snapshot_from_dict, snapshot_to_dict,
)
from ._assets import check_stock, nav_number, number
from ._tables import by_id, has_class, one, read_html, rows

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = "https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/00936"
STOCK_HEADER = ["代號", "名稱", "股數", "持股權重"]
FUTURES_HEADER = ["期貨名稱", "契約年月", "口數", "持股權重"]
CURRENT_HEADER = ["流動資產總額(計價幣)", "流動比率(%)"]
CARD_HEADERS = {
    "申購買回清單公告", "期貨", "現金", "國家配置", "ETF成分股異動差異資訊", "股票",
}


def _percentage(text: str) -> Decimal:
    """Require the native four-decimal percentage and explicit percent sign."""
    if not re.fullmatch(r"\d+\.\d{4}%", text):
        raise ValueError("Invalid TSIT percentage unit or precision")
    value = number(text[:-1])
    if not 0 <= value <= 100:
        raise ValueError("TSIT percentage outside verified range")
    return value


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Require complete stock totals and NAV/current-assets reconciliation.

    The investment panel's native stock percentages reconcile to its stock
    total within the summed four-decimal display rounding bound. That total
    equals the complement of disclosed current
    assets over same-date NAV at four decimals; the separately disclosed
    current-asset ratio must also match NAV at its two-decimal precision.
    Current assets remain a broad other bucket; futures NAV and notional
    semantics are unverified and therefore both remain unknown.
    """
    if (etf.symbol != "00936" or etf.issuer != "tsit" or etf.is_active
            or etf.source_url != SOURCE_URL):
        raise ValueError("Mismatched TSIT source descriptor")
    root = read_html(raw)
    title = one([node for node in root.find("h3")
                 if has_class(node, "item-title")], "TSIT fund title")
    if not title.text.startswith("台新臺灣永續高息中小型ETF基金 (00936)"):
        raise ValueError("Mismatched TSIT fund identity")
    ids = [node for node in root.find("input")
           if node.attrs.get("id") == "ETF_ID"]
    if not ids or any(node.attrs.get("value") != etf.symbol for node in ids):
        raise ValueError("Mismatched TSIT hidden fund identity")
    panel = by_id(root, "paneFive1")
    hidden = by_id(root, "NAV_DATE")
    match = re.fullmatch(r"(\d{4}/\d{2}/\d{2}) 上午 12:00:00",
                         hidden.attrs.get("value") or "")
    if hidden.attrs.get("type") != "hidden" or match is None:
        raise ValueError("Missing TSIT investment NAV date")
    day_text = match.group(1)
    day = date.fromisoformat(day_text.replace("/", "-"))
    cards = [node for node in panel.find("div")
             if has_class(node, "card-header")]
    if (len(cards) != len(CARD_HEADERS)
            or {n.text for n in cards} != CARD_HEADERS):
        raise ValueError("Unknown or missing TSIT investment asset section")
    tables: dict[str, list[list[str]]] = {}
    for table in panel.find("table"):
        values = rows(table)
        header = values[0]
        key = ("stock" if header == STOCK_HEADER else
               "futures" if header == FUTURES_HEADER else
               "current" if header == CURRENT_HEADER else
               "nav" if len(header) == 2
               and header[0] == "基金淨資產價值(元)" else
               "country" if header == ["名稱", "比重(%)"] else
               "review" if len(header) == 2
               and header[0] == "定審頻率(月數)" else None)
        if key is None or key in tables:
            raise ValueError("Unknown or ambiguous TSIT investment table")
        tables[key] = values
    if set(tables) != {"stock", "futures", "current", "nav", "country",
                       "review"}:
        raise ValueError("Missing TSIT investment tables")
    nav_rows = tables["nav"]
    if any(len(row) != 2 for row in nav_rows):
        raise ValueError("Malformed TSIT dated NAV summary")
    if not nav_rows[0][1].startswith("TWD "):
        raise ValueError("Missing TSIT TWD NAV unit")
    nav = nav_number(nav_rows[0][1][4:])
    dates = [re.fullmatch(r"(\d{4}/\d{2}/\d{2})每基數(?:實際申購總價金|申購總價金差額)\(元\)",
                          row[0]) for row in nav_rows
             if re.match(r"\d{4}/", row[0])]
    if (len(dates) != 2
            or any(m is None or m.group(1) != day_text for m in dates)):
        raise ValueError("TSIT actual asset and dated NAV fields disagree")
    current = tables["current"]
    if len(current) != 2 or len(current[1]) != 2:
        raise ValueError("Malformed TSIT current asset disclosure")
    current_amount = number(current[1][0])
    if not 0 <= current_amount <= nav:
        raise ValueError("TSIT current asset amount outside NAV range")
    if not re.fullmatch(r"\d+\.\d{2}", current[1][1]):
        raise ValueError("Invalid TSIT current asset percentage unit")
    current_pct = current_amount / nav * 100
    if number(current[1][1]) != current_pct.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP):
        discrepancy("TSIT current assets disagree with NAV denominator")
    stock_rows = tables["stock"]
    if (len(stock_rows) < 3 or len(stock_rows[-1]) != 2
            or stock_rows[-1][0] != "股票合計"):
        raise ValueError("Missing TSIT complete stock total")
    stock_total = _percentage(stock_rows[-1][1])
    if stock_total != (100 - current_pct).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP):
        discrepancy("TSIT stock/current assets do not reconcile to NAV")
    holdings: list[Holding] = []
    seen: set[str] = set()
    total = Decimal(0)
    for row in stock_rows[1:-1]:
        if len(row) != 4 or not re.fullmatch(r"\d{4}[A-Z]? TT", row[0]):
            raise ValueError("Malformed TSIT Taiwan stock identity")
        code = row[0][:-3]
        if code in seen:
            raise ValueError("Duplicate TSIT stock")
        check_stock(code, row[1], number(row[2]), Decimal(0), nav)
        percentage = _percentage(row[3])
        total += percentage
        holdings.append(
            Holding(code, row[1], "stock", float(percentage / 100)))
        seen.add(code)
    # Each stock and the independent aggregate can round by half a unit.
    tolerance = Decimal(len(holdings) + 1) * Decimal("0.00005")
    if abs(total - stock_total) > tolerance:
        discrepancy("TSIT stock rows do not reconcile to stock total")
    holdings.append(Holding("current_assets", CURRENT_HEADER[0], "other",
                            float(current_amount / nav)))
    futures = tables["futures"]
    if (len(futures) < 3 or len(futures[-1]) != 2
            or futures[-1][0] != "期貨合計"):
        raise ValueError("Missing TSIT futures total")
    future_total = Decimal(0)
    seen_futures: set[str] = set()
    for row in futures[1:-1]:
        if (len(row) != 4 or not re.fullmatch(r"\d{6}", row[1])
                or not row[0] or row[0] in seen_futures):
            raise ValueError("Malformed or duplicate TSIT futures")
        contracts = number(row[2])
        if contracts != contracts.to_integral():
            raise ValueError("TSIT futures contracts must be integral")
        future_total += _percentage(row[3])
        seen_futures.add(row[0])
        holdings.append(Holding(row[0], row[0], "futures", None, None))
    if future_total != _percentage(futures[-1][1]):
        discrepancy("TSIT futures do not reconcile to source total")
    snapshot = Snapshot("1", etf.symbol, etf.issuer, etf.is_active, day,
                        fetched_at, SOURCE_URL, "official", tuple(holdings))
    return snapshot_from_dict(snapshot_to_dict(snapshot))
