"""Parse verified Fubon HTML amounts over their same-page TWD NAV."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
import re
from typing import TYPE_CHECKING, Literal

from ..loader import parse_number
from ..source_quality import discrepancy
from ..models import (
    Holding, Snapshot, snapshot_from_dict, snapshot_to_dict,
)

if TYPE_CHECKING:
    from .registry import SupportedEtf


STOCK_HEADER = ["股票代碼", "股票名稱", "股數", "金額", "權重(%)"]
FUTURES_HEADER = ["期貨代碼", "期貨名稱", "口數", "金額", "權重(%)"]
REPO_HEADER = ["代號", "名稱", "金額"]
ASSET_RULES: dict[str, Literal["cash", "other"]] = {
    "現金 (TWD)": "cash",
    "保證金 (TWD)": "other",
    "應收(付)證券款 (TWD)": "other",
    "應付受益權單位買回款 (TWD)": "other",
    "附買回債券": "other",
}
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


@dataclass
class _Node:
    """Keep source elements and closure evidence for relevant fields."""

    tag: str
    attrs: dict[str, str | None]
    children: list["_Node"] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    closed: bool = False

    @property
    def text(self) -> str:
        """Collapse HTML whitespace after entity decoding."""
        return " ".join("".join(self.parts).split())

    def find(self, tag: str) -> list["_Node"]:
        """Return matching descendants in source order."""
        return [node for child in self.children
                for node in ([child] if child.tag == tag else [])
                + child.find(tag)]


class _Document(HTMLParser):
    """Extract HTML structure without executing scripts or fixing tables."""

    def __init__(self, text: str) -> None:
        """Parse an already strictly decoded source document."""
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]
        self.feed(text)
        self.close()

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        """Store elements, retaining invalid duplicate attributes as errors."""
        if len(dict(attrs)) != len(attrs):
            raise ValueError("Ambiguous duplicate HTML attributes")
        node = _Node(tag, dict(attrs), closed=tag in VOID_TAGS)
        self.stack[-1].children.append(node)
        if not node.closed:
            self.stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        """Close self-closing elements without absorbing later source rows."""
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        """Close the matching node but retain malformed child evidence."""
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack[index].closed = True
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        """Retain each ancestor's text, including nested span labels."""
        for node in self.stack:
            node.parts.append(data)


def _decode(raw: bytes) -> str:
    """Honor the single declared HTML charset with strict decoding."""
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("Fubon HTML must contain source bytes")
    declarations = {
        match.group(1).lower()
        for meta in re.findall(rb"<meta\b[^>]*>", raw, re.I)
        for match in re.finditer(rb"charset\s*=\s*['\"]?([\w-]+)", meta,
                                 re.I)
    }
    if len(declarations) != 1:
        raise ValueError("Missing or ambiguous declared HTML charset")
    try:
        return raw.decode(declarations.pop().decode("ascii"))
    except (LookupError, UnicodeError) as error:
        raise ValueError("Cannot decode declared HTML charset") from error


def _one(nodes: list[_Node], label: str) -> _Node:
    """Require exactly one complete source element for a required field."""
    if len(nodes) != 1 or not nodes[0].closed:
        raise ValueError(f"Missing, malformed or ambiguous {label}")
    return nodes[0]


def _number(text: str) -> Decimal:
    """Read a finite amount without accepting a percent as currency."""
    if "%" in text:
        raise ValueError("Currency or quantity contains percent marker")
    parse_number(text)
    return Decimal(text.replace(",", "").replace("\u3000", "").strip())


def _rows(table: _Node) -> list[list[str]]:
    """Reject malformed or nested table cells rather than losing rows."""
    if not table.closed or table.find("table"):
        raise ValueError("Malformed or nested holdings table")
    rows: list[list[str]] = []
    for row in table.find("tr"):
        cells = row.find("td")
        if not row.closed or not cells or any(not c.closed for c in cells):
            raise ValueError("Malformed holdings row")
        rows.append([cell.text for cell in cells])
    if not rows:
        raise ValueError("Empty holdings table")
    return rows


def _securities(
    rows: list[list[str]], nav: Decimal, *, futures: bool = False,
) -> list[Holding]:
    """Validate complete rows and totals; keep futures semantics unknown."""
    kind: Literal["stock", "futures"] = "futures" if futures else "stock"
    total_label = "期貨合計" if futures else "股票合計"
    if (len(rows) < 3 or len(rows[-1]) != 5
            or rows[-1][0] != total_label
            or any(rows[-1][index] for index in (1, 2))):
        raise ValueError(f"Missing or malformed {kind} total")
    total = _number(rows[-1][3])
    parse_number(rows[-1][4])
    holdings: list[Holding] = []
    amounts: list[Decimal] = []
    codes: set[str] = set()
    for row in rows[1:-1]:
        if len(row) != 5 or not row[0] or not row[1] or row[0] in codes:
            raise ValueError(f"Malformed or duplicate {kind} identity")
        if not futures and not re.fullmatch(r"\d{4}[A-Z]?", row[0]):
            raise ValueError("Invalid stock code")
        quantity, amount = _number(row[2]), _number(row[3])
        percentage = parse_number(row[4])
        if not futures and (quantity < 0 or not 0 <= amount <= nav
                            or not 0 <= percentage <= 100):
            raise ValueError("Stock numeric values outside valid range")
        amounts.append(amount)
        codes.add(row[0])
        holdings.append(Holding(
            row[0], row[1], kind,
            None if futures else float(amount / nav),
            exposure_weight=None,
        ))
    if sum(amounts, Decimal(0)) != total:
        discrepancy(f"{kind} amount total does not reconcile")
    return holdings


def _assets(rows: list[list[str]], nav: Decimal) -> list[Holding]:
    """Map only known raw labels to signed economic NAV contributions."""
    if len(rows) < 2:
        raise ValueError("Missing asset rows")
    holdings: list[Holding] = []
    seen: set[str] = set()
    for row in rows[1:]:
        if len(row) != 2 or row[0] not in ASSET_RULES or row[0] in seen:
            raise ValueError("Unknown, malformed or duplicate asset label")
        label = row[0]
        amount = _number(row[1])
        if label == "應付受益權單位買回款 (TWD)":
            amount = -abs(amount)
        if not -nav <= amount <= nav:
            raise ValueError("Asset NAV contribution outside valid range")
        holdings.append(Holding(label, label, ASSET_RULES[label],
                                float(amount / nav)))
        seen.add(label)
    return holdings


def _repo_details(
    rows: list[list[str]], assets: list[list[str]], nav: Decimal,
) -> None:
    """Reconcile repo details to the one summary contribution, not stocks.

    The verified Fubon section has no total row. Preserve its raw amounts
    in source evidence and require their exact sum to match the asset
    summary; only that summary is emitted as other to avoid double count.
    """
    summaries = [row for row in assets[1:] if row[0] == "附買回債券"]
    if len(rows) < 2 or len(summaries) != 1 or len(summaries[0]) != 2:
        raise ValueError("Missing or ambiguous repo detail or summary")
    total = Decimal(0)
    seen: set[str] = set()
    for row in rows[1:]:
        if (len(row) != 3 or not re.fullmatch(r"[A-Z0-9]+", row[0])
                or not row[1] or row[0] in seen):
            raise ValueError("Malformed or duplicate repo identity")
        amount = _number(row[2])
        if not 0 <= amount <= nav:
            raise ValueError("Repo amount outside verified range")
        total += amount
        seen.add(row[0])
    if total != _number(summaries[0][1]):
        discrepancy("Repo detail does not reconcile with asset summary")


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Parse one verified Fubon disclosure, or raise ValueError.

    Require independent body title and hidden-ID identity, actual holdings
    date, positive finite same-page NAV, complete stocks and mapped assets.
    Native rounded percentages never replace amount/NAV weights. Futures
    amounts, direction and NAV meaning remain unverified: preserve actual
    codes/names with both NAV weight and notional exposure left unknown.
    No network, normalization, residual allocation or source mutation occurs.
    """
    root = _Document(_decode(raw)).root
    title = _one([node for node in root.find("h6")
                  if "top" in (node.attrs.get("class") or "").split()],
                 "fund title")
    hidden = _one([node for node in root.find("input")
                   if node.attrs.get("id") ==
                   "mainContent_subMainContent_hidStkId"], "hidden fund ID")
    identity = re.fullmatch(r"([0-9]{4,6}[A-Z]?)\s+(.+)", title.text)
    if (etf.issuer != "fubon" or identity is None
            or identity.group(1) != etf.symbol
            or hidden.attrs.get("type") != "hidden"
            or hidden.attrs.get("value") != etf.symbol
            or identity.group(2).startswith("主動") != etf.is_active):
        raise ValueError("Mismatched Fubon ETF identity or management type")
    panel = _one([node for node in root.find("div")
                  if node.attrs.get("id") ==
                  "mainContent_subMainContent_pnlData"], "holdings panel")
    nav_row = _one([node for node in panel.find("li")
                    if node.find("p")
                    and node.find("p")[0].text == "基金淨資產(新台幣)"],
                   "same-page NAV")
    nav_cells = nav_row.find("p")
    if len(nav_cells) != 2 or any(not cell.closed for cell in nav_cells):
        raise ValueError("Malformed same-page NAV")
    nav = _number(nav_cells[1].text)
    if nav <= 0:
        raise ValueError("NAV must be positive")
    day = _one([node for node in panel.find("p")
                if node.text.startswith("資料日期")], "holdings date")
    match = re.fullmatch(r"資料日期\s*：\s*(\d{4})/(\d{2})/(\d{2})", day.text)
    if match is None:
        raise ValueError("Invalid actual holdings date")
    holdings_date = date(*(int(part) for part in match.groups()))
    tables: dict[str, list[list[str]]] = {}
    for table in panel.find("table"):
        rows = _rows(table)
        header = rows[0]
        key = ("stock" if header == STOCK_HEADER else
               "futures" if header == FUTURES_HEADER else
               "repos" if header == REPO_HEADER else
               "assets" if header == ["項目", "金額"] else None)
        if key is None or key in tables:
            raise ValueError("Unknown or ambiguous holdings table")
        if key == "repos":
            repo_panel = _one([node for node in panel.find("div")
                               if node.attrs.get("id") ==
                               "mainContent_subMainContent_pnlBds"],
                              "repo panel")
            if repo_panel.find("table") != [table]:
                raise ValueError("Unknown or ambiguous repo table location")
        tables[key] = rows
    if not {"stock", "assets"} <= tables.keys():
        raise ValueError("Missing stock or asset section")
    sections = panel.find("h6")
    expected_sections = {"股票"} | ({"期貨"} if "futures" in tables else set())
    if "repos" in tables:
        expected_sections.add("附買回債券")
    if (len(sections) != len(expected_sections)
            or {node.text for node in sections} != expected_sections
            or any(not node.closed for node in sections)):
        raise ValueError("Missing or unknown security section")
    holdings = _securities(tables["stock"], nav)
    holdings.extend(_assets(tables["assets"], nav))
    has_repo_summary = any(row[0] == "附買回債券"
                           for row in tables["assets"][1:])
    if has_repo_summary != ("repos" in tables):
        raise ValueError("Missing repo summary or detail section")
    if has_repo_summary:
        _repo_details(tables["repos"], tables["assets"], nav)
    if "futures" in tables:
        holdings.extend(_securities(tables["futures"], nav, futures=True))
    snapshot = Snapshot(
        "1", etf.symbol, "fubon", etf.is_active, holdings_date, fetched_at,
        etf.source_url, "official", tuple(holdings),
    )
    return snapshot_from_dict(snapshot_to_dict(snapshot))
