"""Parse qualified Sinopac actual assets and native percent-of-NAV stocks."""

from datetime import date
from decimal import Decimal
import re
from typing import TYPE_CHECKING

from ..models import (
    Holding, Snapshot, snapshot_from_dict, snapshot_to_dict,
)
from ._assets import check_stock, nav_number, number
from ._tables import Node, has_class, one, read_html, rows

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = "https://sitc.sinopac.com/SinopacEtfs/Etfs/SinglePcf/00888"
STOCK_HEADER = ["證券代碼", "證券名稱", "股數", "佔基金淨資產之權重(%)"]
ASSET_LABELS = ["基金淨資產價值(元)", "基金在外流通單位數", "基金每單位淨值(元)"]


def _twd(text: str) -> Decimal:
    """Require the fixture's explicit TWD currency marker."""
    if not text.startswith("NT$ "):
        raise ValueError("Missing TWD NAV currency marker")
    return nav_number(text[4:])


def _summary(table: Node) -> list[list[str]]:
    """Validate a complete native NAV/unit summary."""
    values = rows(table)
    if [row[0] for row in values] != ASSET_LABELS or any(
            len(row) != 2 for row in values):
        raise ValueError("Unknown or malformed Sinopac asset summary")
    _twd(values[0][1])
    units = number(values[1][1])
    if units <= 0 or units != units.to_integral():
        raise ValueError("Invalid issued fund units")
    _twd(values[2][1])
    return values


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Parse the verified 00888 asset section or fail on contract drift.

    The source explicitly labels percent of total fund NAV. Its actual asset
    date and NAV must match the dated NAV summary; desktop and mobile stock
    renderings must agree for every code, quantity and native percentage.
    Empty optional source sections do not establish zero cash or futures.
    """
    if (etf.symbol != "00888" or etf.issuer != "sinopac" or etf.is_active
            or etf.source_url != SOURCE_URL):
        raise ValueError("Mismatched Sinopac source descriptor")
    root = read_html(raw)
    headings = [node for node in root.find("div")
                if has_class(node, "cash_title-s")]
    identity = one([node for node in headings if "證劵代碼" in node.text],
                   "Sinopac fund identity")
    if not re.fullmatch(
            r"永豐台灣ESG永續優質ETF基金（證劵代碼：00888）\d{4}/\d{2}/\d{2}",
            identity.text):
        raise ValueError("Mismatched Sinopac ETF identity")
    section_headings = [node.text for box in root.find("div")
                        if has_class(box, "cash_box")
                        for node in box.children
                        if has_class(node, "cash_title-s")
                        and node is not identity and not node.find("ul")]
    if section_headings != ["基金資產", "股票"]:
        raise ValueError("Unknown or missing Sinopac asset section")
    memo = one([node for node in root.find("div")
                if node.attrs.get("id") == "dvMemo"], "NAV weight meaning")
    if "ETF系列基金之權重(%)為相對基金淨資產價值之比重" not in memo.text:
        raise ValueError("Missing Sinopac native NAV denominator")
    day_node = one([node for node in root.find("p")
                    if node.text.startswith("資料日期")], "actual asset date")
    match = re.fullmatch(r"資料日期：(\d{4}/\d{2}/\d{2})", day_node.text)
    if match is None:
        raise ValueError("Invalid actual Sinopac asset date")
    day_text = match.group(1)
    day = date.fromisoformat(day_text.replace("/", "-"))
    nav_cell = one([node for node in root.find("div")
                    if has_class(node, "fl_cell") and any(
                        child.text.endswith("基金淨資產價值(元)")
                        for child in node.find("h4"))], "dated NAV summary")
    nav_title = one(nav_cell.find("h4"), "dated NAV label")
    if nav_title.text != day_text + " 基金淨資產價值(元)":
        raise ValueError("Sinopac NAV and actual asset dates disagree")
    nav = _twd(one(nav_cell.find("p"), "dated NAV amount").text)
    asset_tables: list[Node] = []
    stock_tables: list[Node] = []
    mobile: list[list[str]] = []
    for table in root.find("table"):
        values = rows(table)
        if len(values[0]) == 2 and values[0][0] == ASSET_LABELS[0]:
            asset_tables.append(table)
        elif values[0] == STOCK_HEADER and has_class(table, "tab_sh-w"):
            stock_tables.append(table)
        elif (has_class(table, "tab_sh-m") and len(values) == 4
              and [row[0] for row in values] == STOCK_HEADER
              and all(len(row) == 2 for row in values)):
            mobile.append([row[1] for row in values])
        else:
            raise ValueError("Unknown or malformed Sinopac asset table")
    if len(asset_tables) != 2:
        raise ValueError("Missing desktop/mobile Sinopac asset summary")
    summaries = [_summary(table) for table in asset_tables]
    if summaries[0] != summaries[1] or _twd(summaries[0][0][1]) != nav:
        raise ValueError("Sinopac NAV summaries disagree")
    values = rows(one(stock_tables, "complete desktop stocks"))[1:]
    if not values or values != mobile:
        raise ValueError("Sinopac desktop/mobile stocks do not reconcile")
    holdings: list[Holding] = []
    seen: set[str] = set()
    for row in values:
        if len(row) != 4 or row[0] in seen:
            raise ValueError("Malformed or duplicate Sinopac stock")
        shares = number(row[2])
        check_stock(row[0], row[1], shares, Decimal(0), nav)
        if not re.fullmatch(r"\d+\.\d{2}", row[3]):
            raise ValueError("Invalid Sinopac percent unit or precision")
        percentage = number(row[3])
        if not 0 <= percentage <= 100:
            raise ValueError("Sinopac stock weight outside NAV range")
        seen.add(row[0])
        holdings.append(Holding(row[0], row[1], "stock",
                                float(percentage / 100)))
    snapshot = Snapshot("1", etf.symbol, etf.issuer, etf.is_active, day,
                        fetched_at, SOURCE_URL, "official", tuple(holdings))
    return snapshot_from_dict(snapshot_to_dict(snapshot))
