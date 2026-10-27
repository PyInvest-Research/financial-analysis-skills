"""Extract Union asset evidence while blocking unqualified NAV weights."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
from typing import TYPE_CHECKING

from ..models import Snapshot
from ._assets import check_stock, nav_number, number
from ._tables import by_id, one, read_html, rows

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = "https://www.usitc.com.tw/CustCenter/BuyBackList"
STOCK_HEADER = ["股票代號", "股票名稱", "股數", "權重(%)"]
FUTURES_HEADER = ["期貨代碼", "期貨名稱", "口數", "權重(%)", "契約年月"]
NAV_HEADER = ["基金淨資產價值", "基金在外流通單位數", "基金每單位淨值"]
BLOCK_REASON = (
    "Union NAV denominator and complete-stock reconciliation are unqualified; "
    "native percentages, row counts and near-100 totals are insufficient"
)


@dataclass(frozen=True)
class AssetEvidence:
    """Preserve source fields without claiming any percentage is NAV-based."""

    holdings_date: date
    nav_twd: Decimal
    stock_rows: tuple[tuple[str, ...], ...]
    futures_rows: tuple[tuple[str, ...], ...]


def _native_percentage(value: str) -> Decimal:
    """Validate native percent-field syntax without assigning a denominator."""
    if not re.fullmatch(r"\d+\.\d{4}", value):
        raise ValueError("Invalid Union native percentage unit or precision")
    result = number(value)
    if not 0 <= result <= 100:
        raise ValueError("Union native percentage outside disclosed range")
    return result


def extract(raw: bytes, etf: "SupportedEtf") -> AssetEvidence:
    """Validate identity/date and extract raw assets, never a usable snapshot.

    Only the saved public 009804 route is recognized. The full HTML includes
    hidden UI rows and a fund-asset date separate from the PCF announcement.
    Missing cash/payables are not converted into zeros or balancing assets.
    """
    if (etf.symbol != "009804" or etf.issuer != "union" or etf.is_active
            or etf.source_url != SOURCE_URL):
        raise ValueError("Mismatched Union source descriptor")
    root = read_html(raw)
    title = one([node for node in root.find("h2")
                 if "009804" in node.text], "Union fund title")
    if title.text != "聯邦台灣精彩50ETF基金 ( 009804 )":
        raise ValueError("Mismatched Union fund identity")
    panel = by_id(root, "list-pbl")
    headings = [node.text for node in panel.find("h3") if node.text]
    if headings != ["基金資產", "期貨投資比例", "股票投資比例"]:
        raise ValueError("Unknown or missing Union asset section")
    day_node = one([node for node in panel.find("b")
                    if node.text.startswith("資料日期")], "Union asset date")
    match = re.fullmatch(r"資料日期： (\d{4}-\d{2}-\d{2})", day_node.text)
    if match is None:
        raise ValueError("Invalid Union actual asset date")
    day = date.fromisoformat(match.group(1))
    tables: dict[str, list[list[str]]] = {}
    for table in panel.find("table"):
        values = rows(table)
        key = ("nav" if values[0] == NAV_HEADER else
               "stock" if values[0] == STOCK_HEADER else
               "futures" if values[0] == FUTURES_HEADER else None)
        if key is None or key in tables:
            raise ValueError("Unknown or ambiguous Union asset table")
        tables[key] = values
    if set(tables) != {"nav", "stock", "futures"}:
        raise ValueError("Missing Union asset table")
    summary = tables["nav"]
    if (len(summary) != 2 or len(summary[1]) != 3
            or not summary[1][0].startswith("NT$ ")):
        raise ValueError("Malformed Union TWD asset NAV")
    nav = nav_number(summary[1][0][4:])
    stocks: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for row in tables["stock"][1:]:
        if len(row) != 4 or row[0] in seen:
            raise ValueError("Malformed or duplicate Union stock")
        check_stock(row[0], row[1], number(row[2]), Decimal(0), nav)
        _native_percentage(row[3])
        seen.add(row[0])
        stocks.append(tuple(row))
    if not stocks:
        raise ValueError("Missing Union stock rows")
    futures: list[tuple[str, ...]] = []
    seen_futures: set[str] = set()
    for row in tables["futures"][1:]:
        if (len(row) != 5 or not re.fullmatch(r"[A-Z0-9]+", row[0])
                or not row[1] or row[0] in seen_futures
                or not re.fullmatch(r"\d{6}", row[4])):
            raise ValueError("Malformed or duplicate Union futures")
        quantity = number(row[2])
        if quantity != quantity.to_integral():
            raise ValueError("Union future contracts must be integral")
        _native_percentage(row[3])
        seen_futures.add(row[0])
        futures.append(tuple(row))
    return AssetEvidence(day, nav, tuple(stocks), tuple(futures))


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Fail closed after extraction until source semantics are qualified."""
    extract(raw, etf)
    raise ValueError(BLOCK_REASON)
