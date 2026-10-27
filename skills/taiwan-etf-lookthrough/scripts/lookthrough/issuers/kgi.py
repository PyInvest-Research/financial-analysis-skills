"""Preserve KGI PCF evidence while refusing an unproved stock as-of date."""

from dataclasses import dataclass
from datetime import date
import re
from typing import Any, TYPE_CHECKING

from ._assets import number
from ._workbooks import read_sheets
from ..models import Snapshot

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = "https://www.kgifund.com.tw/Fund/RedemptionList"


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
    """Fail closed until actual holdings day and NAV basis are qualified."""
    extract(raw, etf)
    raise ValueError("KGI actual holdings date/NAV basis remain unqualified; "
                     "the dated NAV is not a stock holdings date")
