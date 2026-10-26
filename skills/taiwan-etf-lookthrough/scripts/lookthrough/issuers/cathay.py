"""Adapt qualified manual Cathay public DOM captures without acquisition."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
import json
import re
from typing import TYPE_CHECKING

from ..models import Holding, Snapshot, snapshot_from_dict, snapshot_to_dict

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_IDS = {"00878": "ECN", "00400A": "EEA"}
HEADERS = {
    "股票": ["證券代碼", "股票名稱", "股數", "持股權重"],
    "期貨": ["期貨代號", "期貨名稱", "持股權重", "口數", "契約年月"],
    "其他資產": ["項目", "金額"],
}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}
REPRESENTATION = ("official rendered DOM plus visible date-control value; "
                  "not a downloaded XLSX or HTTP response")


@dataclass
class _Node:
    """Retain source text and child structure without executing markup."""

    tag: str
    attrs: dict[str, str | None]
    children: list["_Node"] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Return entity-decoded text with collapsed display whitespace."""
        return " ".join("".join(self.parts).split())

    def find(self, tag: str) -> list["_Node"]:
        """Find all matching descendants in original document order."""
        return [n for child in self.children
                for n in ([child] if child.tag == tag else [])
                + child.find(tag)]

    def has(self, name: str) -> bool:
        """Check one exact CSS class, never substring-match class names."""
        return name in (self.attrs.get("class") or "").split()

    def css(self, name: str) -> list["_Node"]:
        """Find descendants carrying an exact observed class."""
        return [n for child in self.children
                for n in ([child] if child.has(name) else [])
                + child.css(name)]


class _Document(HTMLParser):
    """Require balanced captured DOM and reject scripts or duplicate attrs."""

    def __init__(self, html: str) -> None:
        """Parse local DOM or raise ValueError for malformed HTML."""
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]
        self.feed(html)
        self.close()
        if len(self.stack) != 1:
            raise ValueError("Unclosed capture HTML")

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        """Store source structure, closing standard void elements."""
        if tag == "script" or len(dict(attrs)) != len(attrs):
            raise ValueError("Script or ambiguous HTML attributes")
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        """Close XML-style elements without swallowing following rows."""
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        """Reject mismatched closures instead of silently repairing HTML."""
        if len(self.stack) == 1 or self.stack[-1].tag != tag:
            raise ValueError("Mismatched capture HTML closing tag")
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        """Preserve each ancestor's full textual content for exact labels."""
        for node in self.stack:
            node.parts.append(data)


def _one(nodes: list[_Node]) -> _Node:
    """Require exactly one field instead of accepting ambiguous captures."""
    if len(nodes) != 1:
        raise ValueError("Missing or ambiguous capture field")
    return nodes[0]


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON fields before metadata can be overwritten."""
    if len(dict(pairs)) != len(pairs):
        raise ValueError("Duplicate envelope field")
    return dict(pairs)


def _number(text: str) -> Decimal:
    """Read a finite signed native number with strict grouping syntax."""
    if not re.fullmatch(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text):
        raise ValueError("Invalid native numeric field")
    value = Decimal(text.replace(",", ""))
    if not value.is_finite():
        raise ValueError("Nonfinite native numeric field")
    return value


def _percentage(text: str) -> Decimal:
    """Keep the observed two-decimal native percentage without rescaling."""
    if not re.fullmatch(r"\d+\.\d{2}%", text):
        raise ValueError("Invalid native percentage precision")
    value = _number(text[:-1])
    if not 0 <= value <= 100:
        raise ValueError("Native percentage outside supported range")
    return value


def _quantity(text: str) -> None:
    """Validate disclosed units without converting them to exposure."""
    value = _number(text)
    if value < 0 or value != value.to_integral():
        raise ValueError("Invalid native quantity")


def _fields(row: _Node, labels: list[str]) -> list[str]:
    """Read alternating exact native row labels and values."""
    box = _one(row.css("other_value"))
    cells = box.children
    if (len(cells) != 2 * len(labels)
            or any(c.tag != "span" or c.children for c in cells)):
        raise ValueError("Unknown value row structure")
    if ([c.text for c in cells[::2]] != labels
            or not all(c.has("values_name") for c in cells[::2])
            or any(c.has("values_name") for c in cells[1::2])):
        raise ValueError("Unknown value row labels")
    return [c.text for c in cells[1::2]]


def _identity(row: _Node, *, asset: bool = False) -> tuple[str, str]:
    """Read one exact code/name structure, or a single known asset label."""
    if (len(row.children) != 2 or not row.children[0].has("code_and_name")
            or not row.children[1].has("other_value")):
        raise ValueError("Unknown holdings row structure")
    box = row.children[0]
    expected = ["name"] if asset else ["code", "name"]
    if (len(box.children) != len(expected)
            or any(c.tag != "span" or c.children or not c.has(label)
                   or not c.text for c, label in zip(box.children, expected))):
        raise ValueError("Invalid holding identity")
    return ((box.children[0].text, box.children[0].text) if asset else
            (box.children[0].text, box.children[1].text))


def _rows(section: _Node, name: str) -> list[_Node]:
    """Require the expanded known section and its exact native header."""
    if not section.has("bar_table") or not section.has("s_show_all_cnt"):
        raise ValueError("Collapsed or unsupported holdings section")
    title = _one(section.css("bar_table_title"))
    if title.text != f"詳細持股成分 - {name}":
        raise ValueError("Unknown section title")
    area = _one(section.css("bar_table_are"))
    expected = ["bar_table_subtitle", "bar_table_head", "bar_table_body"]
    if (len(area.children) not in (3, 4)
            or any(not child.has(label) for child, label
                   in zip(area.children[:3], expected))
            or (len(area.children) == 4 and (
                name != "股票" or not area.children[3].has("button-box")))):
        raise ValueError("Unknown section structure")
    header = area.children[1]
    if [n.text for n in header.find("span")] != HEADERS[name]:
        raise ValueError("Unknown holdings header")
    body = area.children[2]
    if any(n.tag != "div" or not n.has("bar_table_line")
           for n in body.children):
        raise ValueError("Unknown holdings body row")
    if not body.children:
        raise ValueError("Empty holdings body")
    return body.children


def _overview(holdings: _Node) -> tuple[Decimal, dict[str, Decimal]]:
    """Require same-overview TWD NAV and supported aggregate asset labels."""
    overview = _one(holdings.css("weights_block"))
    if [n.text for n in overview.find("h3")] != ["持股權重概覽"]:
        raise ValueError("Missing holdings overview")
    nav_box = _one([n for n in overview.css("ov_num_box")
                    if [h.text for h in n.find("h4")] == ["基金資產總淨值"]])
    if _one(nav_box.css("ov_currency")).text != "新台幣":
        raise ValueError("Unsupported NAV currency")
    nav = _number(_one(nav_box.css("ov_num_money")).text)
    if nav <= 0:
        raise ValueError("NAV must be positive")
    asset_box = _one(overview.css("ov_box_body"))
    if _one(asset_box.css("ov_chart_head")).text != "資產成分幣別皆為新台幣":
        raise ValueError("Unsupported aggregate currency or labels")
    table = _one(asset_box.find("table"))
    aggregates: dict[str, Decimal] = {}
    for row in table.find("tr"):
        label = _one(row.css("bar-chart-item")).text
        if label not in ("股票", "期貨（名目本金）") or label in aggregates:
            raise ValueError("Unknown or duplicate aggregate asset")
        amount = _one(row.css("bar-chart-amount")).text
        if not amount.startswith("$ "):
            raise ValueError("Unsupported aggregate amount")
        aggregates[label] = _number(amount[2:])
    if "股票" not in aggregates or not 0 <= aggregates["股票"] <= nav:
        raise ValueError("Missing or invalid aggregate stocks")
    return nav, aggregates


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Parse qualified manual capture bytes, or raise ValueError.

    The producer verifies visible holdings date and expanded completeness
    when capturing the official page. This adapter checks metadata syntax,
    raw identity and known structure, not arbitrary-envelope authenticity.
    Use native stock percent / 100, signed TWD assets / NAV, and unknown
    futures weights. No network, inferred dates, sign flips or rescaling.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > 2 * 1024 * 1024:
        raise ValueError("Invalid capture bytes or size")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Malformed UTF-8 capture JSON") from error
    if (isinstance(payload, dict)
            and payload.get("representation") == (
                "Rendered public DOM after the visible show-more control was "
                "expanded; not byte-exact HTTP or downloaded XLSX")):
        from .cathay_modern import parse as parse_modern
        return parse_modern(raw, etf, fetched_at)
    keys = {"source_url", "captured_at", "representation", "fund_heading",
            "rendered_html", "root_tag", "selected_holdings_date"}
    if (not isinstance(payload, dict) or set(payload) != keys
            or any(not isinstance(v, str) for v in payload.values())
            or payload["root_tag"] != "APP-ETF-DETAIL"
            or payload["representation"] != REPRESENTATION):
        raise ValueError("Unsupported manual capture envelope")
    day = date.fromisoformat(payload["selected_holdings_date"])
    stamp = datetime.fromisoformat(payload["captured_at"])
    if (day.isoformat() != payload["selected_holdings_date"]
            or "T" not in payload["captured_at"]
            or stamp.utcoffset() != timedelta(0)
            or fetched_at != payload["captured_at"]):
        raise ValueError("Invalid manual date or UTC capture timestamp")
    root = _Document(payload["rendered_html"]).root
    app = _one(root.children)
    if app.tag != "app-etf-detail" or root.text != app.text:
        raise ValueError("Unsupported capture root")
    heading = _one(app.find("h1")).text
    identity = re.fullmatch(r"(\d{4,6}[A-Z]?) (國泰.+ETF基金)", heading)
    if (identity is None or identity.group(1) not in SOURCE_IDS
            or etf.issuer != "cathay" or identity.group(1) != etf.symbol
            or ("主動式" in identity.group(2)) != etf.is_active
            or heading != payload["fund_heading"]):
        raise ValueError("Mismatched raw Cathay identity or management type")
    expected_url = ("https://www.cathaysite.com.tw/ETF/detail/"
                    f"{SOURCE_IDS[identity.group(1)]}?tab=etf3")
    if payload["source_url"] != expected_url or etf.source_url != expected_url:
        raise ValueError("Mismatched official Cathay source URL")
    holdings = _one(app.find("app-holdings"))
    control = _one(holdings.find("input"))
    if (control.attrs.get("name") != "dp"
            or control.attrs.get("type") != "text"
            or "readonly" not in control.attrs
            or (control.attrs.get("value") is not None
                and control.attrs["value"] != day.isoformat())):
        raise ValueError("Missing or contradictory holdings date control")
    nav, aggregates = _overview(holdings)
    sections: dict[str, _Node] = {}
    for node in holdings.find("div"):
        if node.has("bar_table") or "data-anchor" in node.attrs:
            label = node.attrs.get("data-anchor")
            if label not in HEADERS or label in sections:
                raise ValueError("Unknown or duplicate holdings section")
            sections[label] = node
    required = {"股票", "其他資產"}
    if "期貨（名目本金）" in aggregates:
        required.add("期貨")
    if set(sections) != required:
        raise ValueError("Missing or inconsistent holdings sections")
    titles = [n.text for n in holdings.find("h3")]
    expected_titles = {"持股權重概覽", "各類資產佔比"} | {
        f"詳細持股成分 - {name}" for name in required}
    if len(titles) != len(expected_titles) or set(titles) != expected_titles:
        raise ValueError("Unknown or incomplete holdings titles")
    result: list[Holding] = []
    stocks: list[Decimal] = []
    seen: set[str] = set()
    for row in _rows(sections["股票"], "股票"):
        code, name = _identity(row)
        if not re.fullmatch(r"\d{4}[A-Z]?", code) or code in seen:
            raise ValueError("Invalid or duplicate stock code")
        quantity, percent = _fields(row, ["股數", "持股權重"])
        _quantity(quantity)
        weight = _percentage(percent) / 100
        stocks.append(weight)
        seen.add(code)
        result.append(Holding(code, name, "stock", float(weight)))
    rounding = Decimal(len(stocks)) * Decimal("0.00005")
    if abs(sum(stocks) - aggregates["股票"] / nav) > rounding:
        raise ValueError("Stock aggregate exceeds native rounding tolerance")
    asset_labels: set[str] = set()
    empty = 0
    for row in _rows(sections["其他資產"], "其他資產"):
        if not row.children and not row.text:
            empty += 1
            if empty > 1:
                raise ValueError("Ambiguous empty asset containers")
            continue
        label, _ = _identity(row, asset=True)
        if label not in ("現金", "保證金", "申贖應付款") or label in asset_labels:
            raise ValueError("Unknown or duplicate other asset")
        text = _fields(row, ["金額"])[0]
        match = re.fullmatch(r"(?:\(TWD\) \$ |NT\$)(-?[\d,]+)", text)
        if match is None:
            raise ValueError("Unsupported asset amount currency or format")
        amount = _number(match.group(1))
        if not -nav <= amount <= nav:
            raise ValueError("Asset amount outside signed NAV range")
        result.append(Holding(label, label,
                              "cash" if label == "現金" else "other",
                              float(amount / nav)))
        asset_labels.add(label)
    if asset_labels != {"現金", "保證金", "申贖應付款"}:
        raise ValueError("Missing required explicit other assets")
    if "期貨" in sections:
        codes: set[str] = set()
        for row in _rows(sections["期貨"], "期貨"):
            code, name = _identity(row)
            if not re.fullmatch(r"[A-Z0-9]+", code) or code in codes:
                raise ValueError("Invalid or duplicate futures identity")
            percent, quantity, expiry = _fields(
                row, ["持股權重", "口數", "契約年月"])
            _percentage(percent)
            _quantity(quantity)
            if not re.fullmatch(r"\d{4}/\d{2}", expiry):
                raise ValueError("Invalid futures expiry")
            date(int(expiry[:4]), int(expiry[5:]), 1)
            result.append(Holding(code, name, "futures", None, None))
            codes.add(code)
    snapshot = Snapshot(
        "1", etf.symbol, "cathay", etf.is_active, day, fetched_at,
        expected_url, "agent_official", tuple(result),
    )
    return snapshot_from_dict(snapshot_to_dict(snapshot))
