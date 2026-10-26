"""Adapt manual Cathay cards using explicit official identity joins."""

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING

from .cathay import (
    _Document, _Node, _fields, _identity, _number, _object, _one,
    _percentage, _quantity,
)
from ..models import Holding, Snapshot, snapshot_from_dict, snapshot_to_dict

if TYPE_CHECKING:
    from .registry import SupportedEtf


REPRESENTATION = (
    "Rendered public DOM after the visible show-more control was expanded; "
    "not byte-exact HTTP or downloaded XLSX"
)
IDENTITIES = {
    "00701": ("EAD", "國泰臺灣低波動股利精選30基金", "國泰股利精選30"),
    "00881": ("ECR", "國泰台灣科技龍頭ETF基金", "國泰台灣科技龍頭"),
    "00922": ("EDO", "國泰台灣領袖50 ETF基金", "國泰台灣領袖50"),
    "00878": ("ECN", "國泰台灣ESG永續高股息ETF基金", "國泰永續高股息"),
    "00400A": ("EEA", "國泰台股動能高息主動式ETF基金", "主動國泰動能高息"),
}
SOURCE_URLS = frozenset(
    f"https://www.cathaysite.com.tw/fund-details/{row[0]}?tab=portfolio"
    for row in IDENTITIES.values()
)
SOURCE_NOTE = (
    "國泰現代卡股票代碼由官方名錄精確唯一名稱查表及已核對官方原生對照衍生，"
    "不是現代頁原生代碼；只移除排版空白，不作模糊或別名猜測。"
    "本來源無基金總NAV，非股票權重未知；現金／其他合計0僅是已知部分，"
    "不代表實際現金或其他資產為0。"
)
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
HEADERS = {"期貨": ["期貨代號", "期貨名稱", "持股權重", "口數", "契約年月"],
           "其他資產": ["項目", "金額"], "附買回債券": ["名稱", "金額"]}


def _key(name: str) -> str:
    """Remove Unicode layout whitespace only, preserving case/punctuation."""
    return "".join(name.split())


def _lookup() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read immutable bundled official names and three native source traces.

    The resource hash anchors the bundled directory version, not capture
    dates or arbitrary HTML authenticity. File/format errors fail closed.
    """
    provenance = json.loads(
        (DATA_DIR / "cathay-modern-lookup.json").read_text())
    raw = (DATA_DIR / "company-directory.json").read_bytes()
    if (provenance["version"] != "1"
            or hashlib.sha256(raw).hexdigest()
            != provenance["directory_sha256"]):
        raise ValueError("Unqualified Cathay lookup directory version")
    directory = json.loads(raw)
    if directory["as_of"] != provenance["directory_as_of"]:
        raise ValueError("Contradictory Cathay lookup directory date")
    supplemental = {}
    for row in provenance["supplemental_mappings"]:
        key = _key(row["original_name"])
        if (key != row["lookup_key"] or key in supplemental
                or row["match_count"] != 1):
            raise ValueError("Ambiguous Cathay supplemental lookup")
        supplemental[key] = row
    return directory["companies"], supplemental


def _resolve(name: str, rows: list[dict[str, Any]],
             supplemental: dict[str, dict[str, Any]]) -> str:
    """Require a unique exact official name or qualified native supplement."""
    key = _key(name)
    if not key:
        raise ValueError("Missing Cathay stock name")
    matches = [row for row in rows if _key(row["name"]) == key]
    if len(matches) > 1:
        raise ValueError("Ambiguous exact Cathay stock name")
    if matches:
        code = matches[0]["code"]
    elif key in supplemental:
        code = supplemental[key]["code"]
    else:
        raise ValueError(f"No exact official code match: {name}")
    if not isinstance(code, str) or not re.fullmatch(r"\d{4}[A-Z]?", code):
        raise ValueError("Invalid derived Cathay stock code")
    return code


def _classes(nodes: list[_Node], classes: list[str]) -> None:
    """Require exact structural slots without guessing at extra children."""
    if len(nodes) != len(classes) or any(
            node.tag != "div" or not node.has(label)
            for node, label in zip(nodes, classes)):
        raise ValueError("Unknown modern Cathay DOM structure")


def _stock_attrs(node: _Node) -> None:
    """Reject new stock identity/data attributes instead of ignoring them."""
    for name in node.attrs:
        if name not in ("class", "style") and not re.fullmatch(
                r"_ngcontent-[a-zA-Z0-9-]+", name):
            raise ValueError("Unqualified modern stock attribute")
    for child in node.children:
        _stock_attrs(child)


def _stocks(
    box: _Node, rows: list[dict[str, Any]],
    supplemental: dict[str, dict[str, Any]],
) -> tuple[list[Holding], list[str]]:
    """Read every expanded name/percentage row and exact derived code."""
    if not box.has("full") or len(box.children) != 2:
        raise ValueError("Incomplete modern stock list")
    listing, mask = box.children
    if (listing.tag != "ul" or not listing.has("bar-chart-ul")
            or not mask.has("mask") or mask.text or mask.children
            or not listing.children):
        raise ValueError("Unsupported modern stock list")
    result: list[Holding] = []
    text: list[str] = []
    seen: set[str] = set()
    for row in listing.children:
        _stock_attrs(row)
        if row.tag != "li" or len(row.children) != 2:
            raise ValueError("Unsupported modern stock row")
        background, content = row.children
        if (not background.has("background") or background.children
                or background.text or content.tag != "div"
                or len(content.children) != 2):
            raise ValueError("Unsupported modern stock value")
        name_box, value = content.children
        title = _one(name_box.children)
        if (title.tag != "div" or not title.has("title") or title.children
                or not title.text or not value.has("number")
                or value.children):
            raise ValueError("Missing modern stock identity/percentage")
        percent = _percentage(value.text)
        width = background.attrs.get("style", "")
        match = re.fullmatch(r"width: (\d+(?:\.\d+)?)%;", width or "")
        if match is None or _number(match.group(1)) != percent:
            raise ValueError("Contradictory native stock percentage")
        code = _resolve(title.text, rows, supplemental)
        if code in seen:
            raise ValueError("Duplicate derived Cathay stock code")
        seen.add(code)
        result.append(Holding(code, title.text, "stock", float(percent / 100)))
        text.extend([title.text, value.text])
    return result, text


def _section_rows(section: _Node, label: str) -> list[_Node]:
    """Check known modern nonstock title, header and expanded row slots."""
    if not section.has("s_show_all_cnt") or len(section.children) != 3:
        raise ValueError("Incomplete modern asset section")
    heading, title, area = section.children
    if (heading.tag != "h4" or heading.text != label
            or not title.has("bar_table_title") or title.text != label
            or not area.has("bar_table_are")):
        raise ValueError("Unknown modern asset section")
    _classes(area.children, ["bar_table_head", "bar_table_body"])
    header, body = area.children
    if [n.text for n in header.find("span")] != HEADERS[label]:
        raise ValueError("Unknown modern asset header")
    if not body.children or any(
            row.tag != "div" or not row.has("bar_table_line")
            for row in body.children):
        raise ValueError("Missing modern asset rows")
    return body.children


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Parse complete manual DOM, deriving codes and leaving nonstock None.

    Producer-saved heading/title/URL bind the official manual page identity;
    the actual date is visible in the holdings card. HTML and visible text
    must agree row by row. This cannot authenticate coherently forged manual
    envelopes. No network, acquisition, raw-hash/date/count allowlist, total
    NAV inference, rescaling, fuzzy names or silent unknown assets occur.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > 2 * 1024 * 1024:
        raise ValueError("Invalid modern capture size")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Malformed modern capture JSON") from error
    keys = {"heading", "portfolio_html", "portfolio_text", "source_url",
            "title", "captured_at", "representation"}
    if (not isinstance(payload, dict) or not keys <= payload.keys()
            or payload.keys() - keys not in (set(), {"official_fund_links"})
            or any(not isinstance(payload[key], str) for key in keys)
            or payload.get("official_fund_links", []) != []
            or payload["representation"] != REPRESENTATION
            or etf.symbol not in IDENTITIES or etf.issuer != "cathay"
            or type(etf.is_active) is not bool
            or etf.is_active != (etf.symbol == "00400A")):
        raise ValueError("Unsupported modern capture envelope/identity")
    route_id, heading, short_name = IDENTITIES[etf.symbol]
    url = ("https://www.cathaysite.com.tw/fund-details/"
           f"{route_id}?tab=portfolio")
    if (payload["source_url"] != url or etf.source_url != url
            or payload["heading"] != f"{etf.symbol} {heading}"
            or payload["title"] != f"{etf.symbol} {short_name}－投資組合｜國泰投信"):
        raise ValueError("Mismatched modern official fund identity")
    stamp = payload["captured_at"]
    if ("T" not in stamp or datetime.fromisoformat(stamp).utcoffset()
            != timedelta(0) or fetched_at != stamp):
        raise ValueError("Invalid modern capture UTC timestamp")
    document = _Document(payload["portfolio_html"]).root
    card = _one(document.children)
    if (not card.has("card-section") or len(card.children) != 2
            or card.find("input")):
        raise ValueError("Unsupported modern holdings card")
    header, body = card.children
    if (not header.has("card-title-block")
            or [n.text for n in header.find("h3")] != ["投資標的"]):
        raise ValueError("Missing modern holdings heading")
    date_text = _one(header.css("date")).text
    match = re.fullmatch(r"資料日期｜(\d{4})/(\d{2})/(\d{2})", date_text)
    if match is None:
        raise ValueError("Missing visible modern holdings date")
    day = date(*map(int, match.groups()))
    if len(body.children) != 3:
        raise ValueError("Unknown modern holdings layout")
    stocks, control, groups = body.children
    if (not stocks.has("bar-chart-ul-box") or not control.has("text-center")
            or not groups.has("other-table-group")):
        raise ValueError("Unknown modern holdings layout")
    button = _one(control.children)
    if (not button.has("view-all-bar-chart") or not button.has("open")
            or [n.text for n in button.children] != ["查看更多", "收合"]):
        raise ValueError("Modern list is not fully expanded")
    rows, supplemental = _lookup()
    result, stock_text = _stocks(stocks, rows, supplemental)
    expected_text = ["投資標的", date_text, *stock_text, "收合"]
    seen: set[str] = set()
    for section in groups.children:
        if section.tag != "div" or not section.has("bar_table"):
            raise ValueError("Unknown modern asset structure")
        label = _one(section.css("bar_table_title")).text
        if label not in HEADERS or label in seen:
            raise ValueError("Unqualified or duplicate modern asset section")
        if label == "附買回債券" and etf.symbol != "00922":
            raise ValueError("Unqualified modern repo source")
        seen.add(label)
        expected_text.extend([label, *HEADERS[label]])
        identities: set[str] = set()
        for row in _section_rows(section, label):
            code, name = _identity(row, asset=label != "期貨")
            if code in identities:
                raise ValueError("Duplicate modern nonstock identity")
            identities.add(code)
            expected_text.extend([code, name] if label == "期貨" else [name])
            if label == "期貨":
                percent, quantity, expiry = _fields(
                    row, ["持股權重", "口數", "契約年月"])
                _percentage(percent)
                _quantity(quantity)
                if (not re.fullmatch(r"[A-Z0-9]+", code)
                        or not re.fullmatch(r"\d{4}/\d{2}", expiry)):
                    raise ValueError("Invalid modern futures identity")
                date(int(expiry[:4]), int(expiry[5:]), 1)
                expected_text.extend([percent, quantity, expiry])
                kind = "futures"
            else:
                amount, = _fields(row, ["金額"])
                if label == "其他資產":
                    if name not in ("現金", "保證金", "申贖應付款"):
                        raise ValueError("Unqualified modern asset label")
                    match = re.fullmatch(r"\(TWD\) \$ (.+)", amount)
                    if match is None and amount != "NT$0":
                        raise ValueError("Unqualified modern asset currency")
                    _number(match.group(1) if match else "0")
                else:
                    _quantity(amount)
                expected_text.append(amount)
                kind = "cash" if label == "其他資產" and name == "現金" else "other"
            result.append(Holding(code, name, kind, None, None))
        if label == "其他資產" and identities != {"現金", "保證金", "申贖應付款"}:
            raise ValueError("Missing modern disclosed asset")
    if "其他資產" not in seen:
        raise ValueError("Missing modern assets section")
    actual_text = [" ".join(line.split())
                   for line in payload["portfolio_text"].splitlines()
                   if line.strip()]
    if actual_text != expected_text:
        raise ValueError("Modern HTML and visible text disagree on rows/date")
    snapshot = Snapshot("1", etf.symbol, "cathay", etf.is_active, day,
                        fetched_at, url, "agent_official", tuple(result))
    return snapshot_from_dict(snapshot_to_dict(snapshot))
