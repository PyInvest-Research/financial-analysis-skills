"""Qualify Union's actual stock panel against same-day official closes."""

import base64
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from typing import TYPE_CHECKING

from ..models import EquityEvidence, Holding, Snapshot
from ..refresh_contract import directory
from ..refresh_prices import read_prices
from ._assets import MAX_RAW_BYTES, check_stock, nav_number, number, read_json
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
    """Preserve source fields before independent denominator verification."""

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


def capture(page: bytes, quotes: bytes) -> bytes:
    """Bind original HTML and TWSE JSON bytes; never execute either input."""
    if any(not isinstance(b, bytes) or not 0 < len(b) <= MAX_RAW_BYTES
           for b in (page, quotes)):
        raise ValueError("Invalid Union capture body length")
    return json.dumps(dict(
        union_version=1, page=base64.b64encode(page).decode(),
        quotes=base64.b64encode(quotes).decode()),
        separators=(",", ":")).encode()


def acquire(session, etf: "SupportedEtf") -> bytes:
    """Use the fixed public page then cached TWSE closes on its asset day.

    The default page must identify 009804 before any quote request. No PCF
    query date, retry, new budget, cookies or browser state are introduced.
    """
    if etf.symbol != "009804" or etf.source_url != SOURCE_URL:
        raise ValueError("Unqualified Union acquisition descriptor")
    page = session.get(SOURCE_URL)
    evidence = extract(page, etf)
    quotes = session.price("TWSE", evidence.holdings_date)
    if quotes is None:
        raise ValueError("Missing same-day Union NAV denominator evidence")
    return capture(page, quotes)


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Retain native weights only after each row matches whole-fund NAV.

    Qualification is limited to the observed complete 50-stock TWSE panel.
    A changed count, market or UI contract requires review, not truncation.
    Unpaired historical HTML remains unqualified. Nonstock NAV is unknown.
    """
    if not raw.lstrip().startswith(b"{"):
        extract(raw, etf)
        raise ValueError(BLOCK_REASON)
    payload = read_json(raw, limit=28 * 1024**2)
    keys = {"union_version", "page", "quotes"}
    if (not isinstance(payload, dict) or set(payload) != keys
            or payload["union_version"] != 1):
        raise ValueError("Invalid Union capture schema")
    try:
        page, quotes = (base64.b64decode(payload[k], validate=True)
                        for k in ("page", "quotes"))
    except (ValueError, TypeError) as error:
        raise ValueError("Malformed Union capture encoding") from error
    if any(not 0 < len(b) <= MAX_RAW_BYTES for b in (page, quotes)):
        raise ValueError("Invalid Union original body length")
    evidence = extract(page, etf)
    stamp = datetime.fromisoformat(fetched_at)
    if ("T" not in fetched_at or stamp.utcoffset() != timedelta(0)
            or evidence.holdings_date > stamp.astimezone(
                timezone(timedelta(hours=8))).date()):
        raise ValueError("Union actual holdings postdate UTC capture")
    root = read_html(page)
    one(root.find("html"), "complete Union document")
    if (by_id(root, "list-pbl-list").text != "基金成分股"
            or by_id(root, "list-fid-list").text != "申購買回清單"
            or "現金申購買回清單" not in by_id(root, "list-fid").text):
        raise ValueError("Union actual-stock/PCF distinction changed")
    panel = by_id(root, "list-pbl")
    controls = panel.find("button")
    if (len(evidence.stock_rows) != 50 or panel.find("a")
            or panel.find("select") or panel.find("input")
            or len(controls) != 1 or controls[0].attrs.get("id") != "foldtable"
            or controls[0].text != "展開↓"):
        raise ValueError("Union complete-stock panel contract changed")
    prices = read_prices(quotes, "TWSE", evidence.holdings_date)
    holdings: list[Holding] = []
    for code, name, shares, native in evidence.stock_rows:
        company = directory().get(code)
        if (company is None or company.market != "TWSE"
                or name != company.name and (code, name, company.name)
                != ("4904", "遠傳電信", "遠傳")):
            raise ValueError(
                f"Unqualified Union stock identity/market: {code}")
        if code not in prices:
            raise ValueError(f"Missing Union same-day closing price: {code}")
        calculated = number(shares) * prices[code] / evidence.nav_twd * 100
        if abs(calculated - _native_percentage(native)) > Decimal("0.00005"):
            raise ValueError(
                f"Union native weight disagrees with same-day NAV: {code}")
        holdings.append(Holding(
            code, name, "stock", float(_native_percentage(native) / 100),
            market="TWSE"))
    holdings.extend(Holding(r[0], r[1], "futures", None)
                    for r in evidence.futures_rows)
    holdings.append(Holding("UNCLASSIFIED", "未揭露非股票淨資產", "other", None))
    equity = EquityEvidence(
        True, float(evidence.nav_twd), None, len(evidence.stock_rows), None,
        tuple(hashlib.sha256(b).hexdigest() for b in (page, quotes)),
    )
    return Snapshot("2", etf.symbol, etf.issuer, etf.is_active,
                    evidence.holdings_date, fetched_at, SOURCE_URL, "official",
                    tuple(holdings), equity,
                    ("官方基金成分股完整 50 列；含 HTML 已載入的隱藏列，非申贖籃子或前十大。",
                     "原生四位百分點逐列以同日 TWSE 收盤價 × 股數 ÷ 全基金 NAV 核對；保留原權重。",
                     "非股票 NAV、期貨名目曝險及股票市值總額未獨立揭露，保持未知，不補平差額。",
                     "4904 原名遠傳電信與官方名錄簡稱遠傳依唯一代號及 TWSE 市場核對。",
                     "本合約限已核的 50 列 TWSE 結構；列數、市場或介面改變需重新資格驗證。"))
