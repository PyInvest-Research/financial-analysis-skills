"""Read KGI actual holdings; keep undated historical PCF unqualified."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import re
from typing import Any, TYPE_CHECKING

from ._assets import number, nav_number
from ._tables import by_id, one, read_html, rows
from ._workbooks import read_sheets
from ..models import EquityEvidence, Holding, Snapshot
from ..refresh_contract import directory

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = "https://www.kgifund.com.tw/Fund/RedemptionList"
ACTUAL_SOURCE_URL = "https://www.kgifund.com.tw/Fund/Detail?fundID=J015"
FUND_NAME = "凱基台灣優選高股息30ETF基金"


@dataclass(frozen=True)
class PcfEvidence:
    """Keep the known NAV day distinct from the unknown holdings day."""

    holdings_date: date | None
    nav_date: date
    stocks: tuple[tuple[Any, ...], ...]


def extract(raw: bytes, etf: "SupportedEtf") -> PcfEvidence:
    """Extract original PCF rows for diagnosis, without creating a snapshot."""
    if (etf.symbol != "00915" or etf.issuer != "kgi" or etf.is_active
            or etf.source_url != SOURCE_URL):
        raise ValueError("Unqualified KGI request identity")
    sheets = read_sheets(raw)
    if set(sheets) != {"申購買回清單公告", "股票", "期貨", "債券", "持債特性"}:
        raise ValueError("Changed KGI PCF workbook structure")
    if (not sheets["持債特性"] or len(sheets["持債特性"]) < 2
            or sheets["持債特性"][1][0] != "凱基台灣優選高股息30ETF基金"):
        raise ValueError("KGI source fund identity mismatch")
    days = []
    for row in sheets["申購買回清單公告"]:
        match = re.fullmatch(
            r"\((\d{4}/\d{2}/\d{2})\)每受益權單位淨資產價值\(元\)",
            str(row[0]))
        if match:
            days.append(date.fromisoformat(match[1].replace("/", "-")))
    if len(days) != 1 or sheets["股票"][0] != [
            "股票代號", "股票名稱", "股數", "權重(%)"]:
        raise ValueError("Missing KGI NAV date or stock header")
    seen: set[str] = set()
    stocks = []
    for row in sheets["股票"][1:]:
        if (len(row) != 4 or not isinstance(row[0], str)
                or not re.fullmatch(r"\d{4}[A-Z]?", row[0].strip())
                or row[0].strip() in seen):
            raise ValueError("Invalid KGI stock row")
        seen.add(row[0].strip())
        number(row[2])
        weight = number(row[3])
        if not 0 <= weight <= 100:
            raise ValueError("Invalid KGI PCF percentage")
        stocks.append(tuple(row))
    return PcfEvidence(None, days[0], tuple(stocks))


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Use the actual dated portfolio, never assign the PCF's NAV day."""
    if raw.startswith(b"PK"):
        extract(raw, etf)
        raise ValueError(
            "KGI actual holdings date/NAV basis remain unqualified; "
            "the dated NAV is not a stock holdings date")
    from ..refresh_inventory import read_kgi

    _descriptor(etf)
    root = read_html(raw)
    one(root.find("html"), "complete KGI document")
    if (by_id(root, "DFundID").attrs.get("value") != "J015"
            or by_id(root, "Name").attrs.get("value") != FUND_NAME
            or by_id(root, "IsETF").attrs.get("value") != "True"):
        raise ValueError("KGI actual product identity mismatch")
    expected_title = (FUND_NAME + " (基金之配息來源可能為收益平準金且並無保證收益及配息) "
                      "(00915 凱基優選高股息30)")
    if [n.text for n in root.find("h2") if n.text] != [expected_title]:
        raise ValueError("KGI actual visible fund identity mismatch")
    inventory = read_kgi(raw, etf.symbol)
    stamp = datetime.fromisoformat(fetched_at)
    if ("T" not in fetched_at or stamp.utcoffset() != timedelta(0)
            or inventory.day > stamp.astimezone(
                timezone(timedelta(hours=8))).date()):
        raise ValueError("KGI actual holdings postdate UTC capture")
    if (by_id(root, "LatestNAVDate").attrs.get("value")
            != inventory.day.strftime("%Y/%m/%d")):
        raise ValueError("KGI NAV and actual stock dates disagree")
    panel = by_id(root, "FundDetail-06")
    tables = [rows(t) for t in panel.find("table")]
    stock_header = ["股票代號", "股票名稱", "股數", "權重(%)"]
    future_header = ["期貨代號", "期貨名稱", "口數", "權重(%)", "契約年月"]
    if ([t[0] for t in tables] != [stock_header, future_header] * 2
            or tables[1] != tables[3] or len(inventory.stocks) != 30
            or panel.find("button") or panel.find("select")
            or panel.find("input")
            or any(a.attrs.get("href") != "javascript:void(0)"
                   for a in panel.find("a"))):
        raise ValueError("KGI complete-stock structure changed")
    # Same-day official stocks/NAV independently bound the common native
    # percentage column. The rounding bound is not an accounting residual.
    native: list[Decimal] = []
    holdings: list[Holding] = []
    for stock, record in zip(inventory.stocks, tables[0][1:], strict=True):
        company = directory().get(stock.code)
        if (company is None or company.market not in ("TWSE", "TPEx")
                or company.name != stock.name):
            raise ValueError("Unqualified KGI security identity/market")
        text = record[3]
        if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
            raise ValueError("Invalid KGI native percentage unit")
        pct = number(text)
        if not 0 <= pct <= 100:
            raise ValueError("KGI native percentage outside range")
        native.append(pct)
        holdings.append(Holding(stock.code, stock.name, "stock",
                                float(pct / 100), market=company.market))
    if (not 0 <= inventory.stock_total <= inventory.nav
            or abs(sum(native) - inventory.stock_total / inventory.nav * 100)
            > len(native) * Decimal("0.005")):
        raise ValueError(
            "KGI native stocks disagree with whole-fund NAV aggregate")
    match = re.search(r"基金在外流通單位數 ([\d,]+) 基金每單位淨值 ([\d.]+)", panel.text)
    if (match is None or abs(inventory.nav / nav_number(match[1])
                             - nav_number(match[2])) > Decimal("0.005")):
        raise ValueError("KGI same-day total NAV/units disagree")
    seen: set[str] = set()
    for future in tables[1][1:]:
        if (len(future) != 5 or not re.fullmatch(r"[A-Z0-9]+", future[0])
                or future[0] in seen or not future[1]
                or not re.fullmatch(r"\d{4}/(?:0[1-9]|1[0-2])", future[4])):
            raise ValueError("Malformed KGI future row")
        contracts = number(future[2])
        pct = number(future[3])
        if contracts != contracts.to_integral() or not 0 <= pct <= 100:
            raise ValueError("Invalid KGI future quantity/percentage")
        seen.add(future[0])
        holdings.append(Holding(future[0], future[1], "futures", None))
    holdings.extend(inventory.assets)
    holdings.append(Holding("UNCLASSIFIED", "未揭露非股票淨額", "other", None))
    equity = EquityEvidence(
        True, float(inventory.nav), float(inventory.stock_total),
        len(native), float(inventory.stock_total),
        (hashlib.sha256(raw).hexdigest(),),
    )
    return Snapshot("2", etf.symbol, etf.issuer, etf.is_active,
                    inventory.day, fetched_at, ACTUAL_SOURCE_URL, "official",
                    tuple(holdings), equity,
                    ("採官方持股頁實際日期及完整桌面／手機一致30列；非申贖籃子。",
                     "保留原生百分點；以同日股票總額／整基金NAV核對合計，未以股價重算或放大。",
                     "來源資格時29上市股票有同日TWSE價格佐證；3293獨立TPEx價格取得未完成，未補價格。",
                     "期貨名目曝險及未揭露非股票淨額保持未知；未將股票合計捨入界限當成餘額解釋。",
                     "合約限已核30列國內股票結構；列數或市場／身分變化須重新核實。"))


def _descriptor(etf: "SupportedEtf") -> None:
    """Keep this actual-portfolio route specific to the qualified product."""
    if (etf.symbol != "00915" or etf.issuer != "kgi" or etf.is_active
            or etf.source_url != ACTUAL_SOURCE_URL):
        raise ValueError("Unqualified KGI actual source descriptor")


def acquire(session, etf: "SupportedEtf") -> bytes:
    """Read one fixed public page within the existing shared HTTP budget."""
    _descriptor(etf)
    return session.get(ACTUAL_SOURCE_URL)
