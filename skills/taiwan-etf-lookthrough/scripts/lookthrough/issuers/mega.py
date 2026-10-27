"""Parse dated Mega product-page assets with reconciled native NAV weights."""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import TYPE_CHECKING

from ..models import (
    Holding, Snapshot, snapshot_from_dict, snapshot_to_dict,
)
from ._assets import check_stock, nav_number, number
from ._tables import Node, by_id, has_class, one, read_html

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = "https://www.megafunds.com.tw/MEGA/etf/etf_product.aspx?id=19"
FUND_NAME = "兆豐台灣ESG永續高股息等權重ETF基金"
STOCK_HEADER = ["股票代號", "股票名稱", "股數", "持股權重"]
FUTURES_HEADER = ["期貨代號", "期貨名稱", "契約年月", "口數", "持股權重"]


def _children(node: Node, class_name: str) -> list[Node]:
    """Require complete direct children with an exact expected class token."""
    children = node.children
    if any(child.tag != "div" or not child.closed
           or not has_class(child, class_name) for child in children):
        raise ValueError("Malformed Mega div row or cell")
    return children


def _field(node: Node, class_name: str) -> Node:
    """Require a unique complete descendant with a source class token."""
    return one([child for child in node.find("div")
                if has_class(child, class_name)], f"Mega {class_name}")


def _percentage(text: str) -> Decimal:
    """Read the native two-decimal percentage with its explicit unit."""
    if not re.fullmatch(r"\d+\.\d{2} %", text):
        raise ValueError("Invalid Mega native percentage unit or precision")
    result = number(text[:-2])
    if not 0 <= result <= 100:
        raise ValueError("Mega native percentage outside verified range")
    return result


def _twd(text: str) -> Decimal:
    """Read an explicitly marked, finite signed TWD asset amount."""
    if not text.startswith("TWD$ "):
        raise ValueError("Missing Mega TWD asset currency marker")
    return number(text[5:])


def _desktop(root: Node, suffix: int, header: list[str]) -> list[list[str]]:
    """Read every complete direct div row, including initially hidden rows."""
    panel = by_id(root, f"fund_content_list_{suffix}")
    title = one([child for child in panel.children
                 if has_class(child, "fund-title-box")], "Mega table header")
    if [n.text for n in _children(title, "filed-name")] != header:
        raise ValueError("Unknown Mega div-table header")
    records: list[list[str]] = []
    for node in panel.children:
        if node is title:
            continue
        if (not node.closed or not has_class(node, "fund-info")
                or not has_class(node, f"content-list-{suffix}")):
            raise ValueError("Unknown or malformed Mega div-table record")
        values = [cell.text for cell in _children(node, "fund-content")]
        if len(values) != len(header):
            raise ValueError("Missing Mega div-table cells")
        records.append(values)
    return records


def _mobile(root: Node, suffix: int, header: list[str]) -> list[list[str]]:
    """Transpose complete mobile records into the desktop source order."""
    panel = by_id(root, f"fund_content_list_{suffix}_mobile")
    records: list[list[str]] = []
    for node in _children(panel, "common-mobile-table"):
        fields: dict[str, str] = {}
        for item in _children(node, "common-table-item"):
            if (len(item.children) != 2
                    or not has_class(item.children[0], "item-title")
                    or not has_class(item.children[1], "item-content")
                    or any(not child.closed for child in item.children)):
                raise ValueError("Malformed Mega mobile field")
            label, value = [child.text for child in item.children]
            if label in fields:
                raise ValueError("Duplicate Mega mobile field")
            fields[label] = value
        if set(fields) != set(header):
            raise ValueError("Unknown or missing Mega mobile field")
        records.append([fields[label] for label in header])
    return records


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Qualify 00932 actual assets independently of the PCF announcement.

    Require the actual asset source day, exact product identity and currency,
    same-page stock amount/NAV matching the printed stock category percent,
    and identical complete desktop/mobile holdings. Native rounded stock
    percentages must reconcile to the stock aggregate within the sum of
    each row's disclosed half-unit rounding bound. Preserve those original
    percentages, margins and cash without residual allocation or scaling.
    Futures amount semantics are unverified, so both weights remain unknown.
    """
    if (etf.symbol != "00932" or etf.issuer != "mega" or etf.is_active
            or etf.source_url != SOURCE_URL):
        raise ValueError("Mismatched Mega source descriptor")
    root = read_html(raw)
    selected = one([node for node in by_id(root, "fund_id").find("option")
                    if "selected" in node.attrs], "selected Mega product")
    if selected.attrs.get("value") != "19" or selected.text != FUND_NAME:
        raise ValueError("Mismatched Mega product ID or name")
    basics = [node for node in root.find("div")
              if has_class(node, "etfin-item")
              or has_class(node, "etfin-item-108px")]
    identity: dict[str, str] = {}
    for node in basics:
        label = _field(node, "etfin5-title").text
        if label in identity:
            raise ValueError("Duplicate Mega basic identity field")
        identity[label] = _field(node, "etfin5-info").text
    if (not identity.get("基金名稱", "").startswith(FUND_NAME)
            or identity.get("彭博代碼") != "00932 TT Equity"
            or identity.get("基金類型") != "國內ETF"):
        raise ValueError("Mismatched Mega ETF identity or management type")
    assets = by_id(root, "asset_div")
    heading = _field(assets, "shareholding-title")
    if one(heading.find("h2"), "Mega asset heading").text != "持股比重":
        raise ValueError("Missing Mega actual asset section")
    day_node = one([node for node in heading.find("p") if node.text],
                   "Mega actual asset date")
    match = re.fullmatch(r"資料來源：兆豐投信，(\d{4}/\d{2}/\d{2})", day_node.text)
    if match is None:
        raise ValueError("Missing Mega actual asset source date")
    day = date.fromisoformat(match.group(1).replace("/", "-"))
    nav_fields: dict[str, str] = {}
    for node in _field(assets, "fund-assets").find("div"):
        if has_class(node, "subtilte-item"):
            label = _field(node, "si-title").text
            if label in nav_fields:
                raise ValueError("Duplicate Mega NAV field")
            nav_fields[label] = _field(node, "si-amount").text
    if set(nav_fields) != {"淨資產價值", "在外流通單位數", "每單位淨值"}:
        raise ValueError("Unknown or missing Mega NAV fields")
    nav = nav_number(nav_fields["淨資產價值"])
    units = nav_number(nav_fields["在外流通單位數"])
    if units != units.to_integral():
        raise ValueError("Mega issued units must be integral")
    nav_number(nav_fields["每單位淨值"])
    amounts: dict[str, Decimal] = {}
    stock_category = Decimal(0)
    for node in assets.find("div"):
        if not has_class(node, "fs-item"):
            continue
        label = _field(node, "fund-title").text
        match = re.fullmatch(r"股票 \( (\d+\.\d{2})% \)", label)
        key = "股票" if match else label
        if key not in {"股票", "期貨", "保證金", "現金/存款"} or key in amounts:
            raise ValueError("Unknown or duplicate Mega asset label")
        amount = _twd(_field(node, "fund-amount").text)
        if not 0 <= amount <= nav:
            raise ValueError("Mega asset amount outside verified NAV range")
        amounts[key] = amount
        if match:
            stock_category = number(match.group(1))
    if set(amounts) != {"股票", "期貨", "保證金", "現金/存款"}:
        raise ValueError("Missing Mega asset classification")
    stock_percent = amounts["股票"] / nav * 100
    if stock_percent.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != (
            stock_category):
        raise ValueError("Mega stock category disagrees with NAV denominator")
    allocation = by_id(root, "fund_asset_div")
    tabs = [node for node in allocation.find("div")
            if has_class(node, "fund-name")]
    if [(n.attrs.get("id"), n.text) for n in tabs] != [
            ("fund_tab_1", "股票"), ("fund_tab_2", "期貨")]:
        raise ValueError("Unknown Mega allocation asset types")
    sections = [node for node in allocation.find("div")
                if has_class(node, "fundInfo-content")]
    if [n.attrs.get("id") for n in sections] != [
            f"fund-content-{index}" for index in range(1, 7)]:
        raise ValueError("Unknown or missing Mega allocation section")
    for node in sections[2:]:
        if any(has_class(child, "fund-info")
               or has_class(child, "common-mobile-table")
               for child in node.find("div")):
            raise ValueError("Populated unqualified Mega non-stock type")
    stock_rows = _desktop(allocation, 1, STOCK_HEADER)
    if not stock_rows or stock_rows != _mobile(allocation, 1, STOCK_HEADER):
        raise ValueError("Mega desktop/mobile stocks do not reconcile")
    holdings: list[Holding] = []
    seen: set[str] = set()
    total = Decimal(0)
    for row in stock_rows:
        if row[0] in seen:
            raise ValueError("Duplicate Mega stock")
        check_stock(row[0], row[1], number(row[2]), Decimal(0), nav)
        percentage = _percentage(row[3])
        total += percentage
        seen.add(row[0])
        holdings.append(
            Holding(row[0], row[1], "stock", float(percentage / 100)))
    if abs(total - stock_percent) > len(stock_rows) * Decimal("0.005"):
        raise ValueError("Mega stock rows exceed aggregate rounding bound")
    holdings.extend((
        Holding("保證金", "保證金", "other", float(amounts["保證金"] / nav)),
        Holding("現金/存款", "現金/存款", "cash", float(amounts["現金/存款"] / nav)),
    ))
    futures = _desktop(allocation, 2, FUTURES_HEADER)
    if not futures or futures != _mobile(allocation, 2, FUTURES_HEADER):
        raise ValueError("Mega desktop/mobile futures do not reconcile")
    seen_futures: set[str] = set()
    for row in futures:
        if (not re.fullmatch(r"[A-Z0-9]+", row[0]) or not row[1]
                or row[0] in seen_futures
                or not re.fullmatch(r"\d{4}/(?:0[1-9]|1[0-2])", row[2])):
            raise ValueError("Invalid or duplicate Mega futures identity")
        quantity = number(row[3])
        if quantity != quantity.to_integral():
            raise ValueError("Mega future contracts must be integral")
        _percentage(row[4])
        seen_futures.add(row[0])
        holdings.append(Holding(row[0], row[1], "futures", None, None))
    snapshot = Snapshot("1", etf.symbol, etf.issuer, etf.is_active, day,
                        fetched_at, SOURCE_URL, "official", tuple(holdings))
    return snapshot_from_dict(snapshot_to_dict(snapshot))
