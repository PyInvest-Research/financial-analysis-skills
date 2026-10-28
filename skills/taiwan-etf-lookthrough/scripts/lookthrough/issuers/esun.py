"""Parse E.SUN 009803 native assets with official fund identity evidence."""

import base64
import binascii
from datetime import date
from decimal import Decimal
import json
import re
from typing import Any, TYPE_CHECKING

from ._assets import check_stock, nav_number, number, read_json
from ..source_quality import discrepancy
from ..models import Holding, Snapshot, snapshot_from_dict, snapshot_to_dict

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = "https://www.esunam.com/ETFAPI/GetFundAssets"
OVERVIEW_URL = "https://www.esunam.com/ETFAPI/GetETFOverview"
_ENVELOPE = "esun-native-assets-v1"
_COLUMNS = {
    "股票": ["股票代號", "股票名稱", "股數", "權重(%)"],
    "期貨": ["期貨代號", "期貨名稱", "口數", "權重(%)", "契約年月"],
    "": [None],
}
_CATEGORIES = {"股票", "期貨", "現金", "保證金"}


def capture(assets: bytes, overview: bytes) -> bytes:
    """Package exact local HTTP bytes without requests or source changes."""
    read_json(assets)
    read_json(overview)
    return json.dumps({
        "capture_kind": _ENVELOPE,
        "assets_base64": base64.b64encode(assets).decode("ascii"),
        "overview_base64": base64.b64encode(overview).decode("ascii"),
    }, separators=(",", ":")).encode("utf-8")


def _unpack(raw: bytes) -> tuple[Any, Any]:
    """Decode a bounded local evidence envelope with exact source bytes."""
    payload = read_json(raw)
    if (not isinstance(payload, dict)
            or set(payload) != {"capture_kind", "assets_base64",
                                "overview_base64"}
            or payload["capture_kind"] != _ENVELOPE):
        raise ValueError("Expected qualified E.SUN local capture")
    try:
        return tuple(read_json(base64.b64decode(payload[key], validate=True))
                     for key in ("assets_base64", "overview_base64"))
    except (TypeError, binascii.Error) as error:
        raise ValueError("Invalid E.SUN captured source bytes") from error


def _response(payload: Any) -> Any:
    """Reject failed API responses or changed outer response structures."""
    if (not isinstance(payload, dict)
            or set(payload) != {"TotalPages", "TotalItems", "Entries",
                                "Message", "StatusCode"}
            or number(payload["StatusCode"], native=True) != 0):
        raise ValueError("Invalid E.SUN API response")
    return payload["Entries"]


def _percent(value: Any) -> Decimal:
    """Read the source's native nonnegative two-decimal percentage points."""
    if not isinstance(value, str) or not re.fullmatch(
            r"\d+(?:\.\d{1,2})?", value):
        raise ValueError("Invalid E.SUN percentage-point unit")
    result = number(value)
    if not 0 <= result <= 100:
        raise ValueError("E.SUN stock percentage outside NAV range")
    return result


def _identity(overview: Any, fund: dict[str, Any]) -> None:
    """Resolve FundNo 50 to 009803 using the same-day official overview."""
    rows = _response(overview)
    if (not isinstance(rows, list) or not rows or len(rows) > 5000
            or number(overview["TotalPages"], native=True) != 1
            or number(overview["TotalItems"], native=True) != len(rows)
            or any(not isinstance(r, dict) for r in rows)):
        raise ValueError("Incomplete E.SUN identity overview")
    candidates = [r for r in rows
                  if r.get("FundNo") == "50" or r.get("StcokNo") == "009803"]
    if len(candidates) != 1:
        raise ValueError("Missing/ambiguous E.SUN FundNo/symbol identity")
    item = candidates[0]
    day = fund["NavDate"]
    if (item.get("FundNo") != "50" or item.get("StcokNo") != "009803"
            or not isinstance(item.get("FundFullName"), str)
            or not item["FundFullName"].startswith("玉山臺灣市值動能50 ETF基金")
            or item.get("ETFTypeStr") != "指數股票型"
            or item.get("DateStr") != day
            or item.get("Date") != f'{day.replace("/", "-")}T00:00:00'
            or nav_number(item.get("Nav"), native=True)
            != nav_number(fund["Nav"])):
        raise ValueError("E.SUN identity, valuation day or NAV mismatch")


def _tables(data: dict[str, Any]) -> dict[str, list[list[str]]]:
    """Read every complete stock, futures and asset table in its NAV scope."""
    if (set(data) != {"FundAsset", "Table"}
            or not isinstance(data["Table"], list)):
        raise ValueError("Unqualified E.SUN asset structure")
    result: dict[str, list[list[str]]] = {}
    for table in data["Table"]:
        if (not isinstance(table, dict)
                or set(table) != {"TableTitle", "Columns", "Rows"}):
            raise ValueError("Changed E.SUN asset table structure")
        title, columns = table["TableTitle"], table["Columns"]
        if (not isinstance(title, str) or title not in _COLUMNS
                or title in result or not isinstance(columns, list)
                or any(not isinstance(c, dict) for c in columns)
                or [c.get("Name") for c in columns] != _COLUMNS[title]):
            raise ValueError("Unknown/duplicate E.SUN asset table or headers")
        rows = table["Rows"]
        width = 5 if title == "期貨" else 4
        if (not isinstance(rows, list) or len(rows) > 5000
                or any(not isinstance(r, list) or len(r) != width
                       or any(not isinstance(v, str) for v in r)
                       for r in rows)):
            raise ValueError("Invalid E.SUN asset rows")
        result[title] = rows
    if set(result) != set(_COLUMNS) or not result["股票"]:
        raise ValueError("Missing E.SUN complete asset table")
    return result


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Keep native NAV percentages with aggregate amount/NAV cross-checks.

    Full official asset tables supply the NAV, all returned stock rows,
    classified amounts and native percentages. The stock market-value/NAV
    ratio must lie within the summed two-decimal display intervals. This
    is a rounding check, not proof against omissions below that bound.
    Cash keeps its disclosed sign. Futures NAV/notional weights and the
    undecomposed asset/liability balance remain unknown. No normalization
    or requests occur, and the local capture retains both original bodies.
    """
    if (etf.symbol != "009803" or etf.issuer != "esun" or etf.is_active
            or etf.source_url != SOURCE_URL):
        raise ValueError("Unqualified E.SUN request identity")
    assets, overview = _unpack(raw)
    entry = _response(assets)
    if (not isinstance(entry, dict) or set(entry) != {"FundID", "Data"}
            or entry["FundID"] != "50"
            or not isinstance(entry["Data"], dict)):
        raise ValueError("E.SUN asset fund identity mismatch")
    data = entry["Data"]
    fund = data.get("FundAsset")
    if (not isinstance(fund, dict)
            or set(fund) != {"Aum", "Units", "Nav", "NavDate"}
            or not isinstance(fund["NavDate"], str)
            or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", fund["NavDate"])):
        raise ValueError("Invalid E.SUN fund NAV/date")
    day = date.fromisoformat(fund["NavDate"].replace("/", "-"))
    nav = nav_number(fund["Aum"])
    units, unit_nav = nav_number(fund["Units"]), nav_number(fund["Nav"])
    if abs(nav / units - unit_nav) > Decimal("0.005"):
        discrepancy("E.SUN AUM/units do not reconcile to rounded NAV")
    _identity(overview, fund)
    tables = _tables(data)
    amounts: dict[str, Decimal] = {}
    for label, display, currency, original in tables[""]:
        if (label not in _CATEGORIES or label in amounts or currency != "TWD"
                or not display.startswith("TWD$")):
            raise ValueError("Unknown E.SUN asset category/currency")
        amount = number(original)
        if (number(display[4:]) != amount or not -nav <= amount <= nav
                or label in {"股票", "期貨"} and amount < 0):
            raise ValueError("Inconsistent E.SUN asset amount/unit")
        amounts[label] = amount
    if set(amounts) != _CATEGORIES:
        raise ValueError("Missing E.SUN qualified asset category")
    holdings: list[Holding] = []
    seen: set[str] = set()
    total = Decimal(0)
    for code, name, quantity, weight in tables["股票"]:
        shares, percent = number(quantity), _percent(weight)
        check_stock(code, name, shares, Decimal(0), Decimal(1))
        if code.startswith("0") or code in seen:
            raise ValueError("Duplicate/ETF E.SUN stock identity")
        seen.add(code)
        total += percent
        holdings.append(Holding(code, name, "stock", float(percent / 100)))
    if abs(amounts["股票"] / nav * 100 - total) > len(seen) * Decimal("0.005"):
        discrepancy("E.SUN stocks fail aggregate NAV rounding bounds")
    holdings.extend(Holding(label, label,
                            "cash" if label == "現金" else "other",
                            float(amount / nav))
                    for label, amount in amounts.items()
                    if label not in {"股票", "期貨"})
    seen.clear()
    futures_percent = Decimal(0)
    for code, name, quantity, weight, month in tables["期貨"]:
        contracts = number(quantity)
        if (not re.fullmatch(r"[A-Z0-9]+", code) or not name.strip()
                or code in seen or contracts < 0
                or contracts != contracts.to_integral()
                or not re.fullmatch(r"\d{4}/(?:0[1-9]|1[0-2])", month)):
            raise ValueError("Invalid E.SUN futures identity/contracts/month")
        futures_percent += _percent(weight)
        seen.add(code)
        holdings.append(Holding(code, f"{name} {month}", "futures", None))
    if abs(amounts["期貨"] / nav * 100 - futures_percent) > (
            len(seen) * Decimal("0.005")):
        raise ValueError("E.SUN futures fail aggregate amount rounding bounds")
    holdings.append(Holding("UNCLASSIFIED", "未分解其他資產及負債", "other", None))
    snapshot = Snapshot("1", etf.symbol, etf.issuer, False, day, fetched_at,
                        SOURCE_URL, "official", tuple(holdings))
    return snapshot_from_dict(snapshot_to_dict(snapshot))
