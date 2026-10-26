"""Parse qualified Capital holdings from explicit PCF-day API queries."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import re
from typing import Any, TYPE_CHECKING

from ..models import Holding, Snapshot, snapshot_from_dict, snapshot_to_dict

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = "https://www.capitalfund.com.tw/CFWeb/api/etf/buyback"
IDENTITIES = {
    '群益台灣核心50主動式ETF基金': ('00415A', '504', True),
    '群益台灣ESG低碳50ETF基金': ('00923', '365', False),
    '群益台灣半導體收益ETF基金': ('00927', '366', False),
    '群益台灣科技高息成長ETF基金': ('00946', '388', False),
    '群益台灣科技創新主動式ETF基金': ('00992A', '500', True),
    "群益台灣精選高息ETF基金": ("00919", "195", False),
    "群益台灣精選強棒主動式ETF基金": ("00982A", "399", True),
}
DATA_KEYS = {"pcf", "stocks", "bonds", "futures", "assets", "rps",
             "characteristics"}
STOCK_KEYS = {"date1", "stocNo", "stocName", "weight", "weightRound", "share",
              "shareFormat", "stocEname", "replace", "smallStatus"}
FUTURE_KEYS = {"txDesc", "weight", "weightRound", "lot", "lotRound", "txDate",
               "txEname"}
ASSET_KINDS = {"現金": "cash", "保證金": "other", "應收付證券款": "other",
               "應付贖回款": "other"}
AGGREGATE_EPSILON = Decimal("0.000001")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys before source fields can be overwritten."""
    if len(dict(pairs)) != len(pairs):
        raise ValueError("Duplicate Capital API key")
    return dict(pairs)


def _nonfinite(text: str) -> Decimal:
    """Reject nonstandard NaN/Infinity JSON constants."""
    raise ValueError(f"Nonfinite Capital API constant: {text}")


def _mapping(
    value: Any, keys: set[str], *, exact: bool = True,
) -> dict[str, Any]:
    """Require known source object shape or explicit required PCF fields."""
    if not isinstance(value, dict) or not keys <= value.keys():
        raise ValueError("Missing Capital API fields")
    if exact and value.keys() != keys:
        raise ValueError("Unknown Capital API object structure")
    return value


def _number(value: Any) -> Decimal:
    """Accept finite native JSON numbers, excluding bools and numeric text."""
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("Invalid native Capital number")
    return value


def _percentage(value: Any) -> Decimal:
    """Keep native percentage precision within the stock NAV-weight range."""
    number = _number(value)
    if not 0 <= number <= 100:
        raise ValueError("Stock percentage outside NAV range")
    return number


def _quantity(value: Any) -> None:
    """Check a native share/lot count without inferring future direction."""
    number = _number(value)
    if number < 0 or number != number.to_integral():
        raise ValueError("Invalid native Capital quantity")


def _day(value: Any) -> date:
    """Require an actual source ISO calendar date without fallbacks."""
    if not isinstance(value, str):
        raise ValueError("Missing actual Capital date")
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError("Invalid Capital ISO date format")
    return day


def _publication_day(value: Any) -> date:
    """Check the source's localized stock publication date and clock."""
    if not isinstance(value, str):
        raise ValueError("Missing stock publication date")
    match = re.fullmatch(
        r"(\d{4})/(\d{1,2})/(\d{1,2}) (?:上午|下午) "
        r"(\d{1,2}):(\d{2}):(\d{2})", value,
    )
    if match is None:
        raise ValueError("Invalid stock publication date format")
    year, month, day, hour, minute, second = map(int, match.groups())
    if not 1 <= hour <= 12 or not 0 <= minute <= 59 or not 0 <= second <= 59:
        raise ValueError("Invalid stock publication clock")
    return date(year, month, day)


def _aggregate(value: Any) -> Decimal:
    """Read the six-decimal aggregate without treating zero as null."""
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d{6}", value):
        raise ValueError("Invalid source aggregate format")
    return Decimal(value)


def _label(value: Any) -> str:
    """Require an exact nonblank, trimmed source identity label."""
    if (not isinstance(value, str) or not value.strip()
            or value != value.strip()):
        raise ValueError("Invalid source identity label")
    return value


def _stock(row: Any, publication: date) -> tuple[str, str, Decimal]:
    """Read native actual stock weight, not display weightRound or a basket."""
    row = _mapping(row, STOCK_KEYS)
    code, name = _label(row["stocNo"]), _label(row["stocName"])
    if not re.fullmatch(r"\d{4}[A-Z]?", code):
        raise ValueError("Invalid Capital stock code")
    if _publication_day(row["date1"]) != publication:
        raise ValueError("Stock publication date disagrees with PCF date1")
    if (not isinstance(row["stocEname"], str)
            or not isinstance(row["shareFormat"], str)
            or row["replace"] is not None or row["smallStatus"] is not None):
        raise ValueError("Unsupported Capital stock row fields")
    _quantity(row["share"])
    _percentage(row["weightRound"])
    return code, name, _percentage(row["weight"])


def _future(row: Any) -> tuple[str, str, Decimal]:
    """Validate raw future fields while leaving canonical exposure unknown."""
    row = _mapping(row, FUTURE_KEYS)
    code, name = _label(row["txEname"]), _label(row["txDesc"])
    month = row["txDate"]
    if (not re.fullmatch(r"[A-Z0-9]+", code) or not isinstance(month, str)
            or not re.fullmatch(r"\d{6}", month)):
        raise ValueError("Invalid Capital future identity or month")
    date(int(month[:4]), int(month[4:]), 1)
    _quantity(row["lot"])
    _number(row["lotRound"])
    _number(row["weightRound"])
    return code, name, _number(row["weight"])


def _request_identity(etf: "SupportedEtf") -> tuple[str, str]:
    """Bind an approved symbol/type/endpoint to the official product ID."""
    for name, (symbol, fund_id, active) in IDENTITIES.items():
        if etf.symbol == symbol:
            if (etf.issuer != "capital" or type(etf.is_active) is not bool
                    or etf.is_active != active
                    or etf.source_url != SOURCE_URL):
                break
            return name, fund_id
    raise ValueError("Mismatched Capital identity, type or source URL")


def _context(
    raw: bytes, etf: "SupportedEtf",
) -> tuple[dict[str, Any], dict[str, Any], date, date]:
    """Validate raw business/identity/date context, including discovery.

    Discovery may have zero aggregates and is never a complete Snapshot.
    This common context does not replace full explicit-response parsing.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > 2 * 1024 * 1024:
        raise ValueError("Invalid Capital API bytes or size")
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object,
            parse_float=Decimal, parse_int=Decimal, parse_constant=_nonfinite,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Malformed UTF-8 Capital JSON") from error
    payload = _mapping(payload, {"code", "data", "message"})
    if _number(payload["code"]) != 200:
        raise ValueError("Capital API returned a non-success business code")
    if payload["message"] is not None and not isinstance(
            payload["message"], str):
        raise ValueError("Invalid Capital business message")
    data = _mapping(payload["data"], DATA_KEYS)
    pcf = _mapping(
        data["pcf"], {"fundName", "date1", "date2", "nav", "dollar",
                      "totStock", "totFuture", "totETF"}, exact=False,
    )
    name, _ = _request_identity(etf)
    if _label(pcf["fundName"]) != name:
        raise ValueError("Mismatched raw Capital fund identity")
    holdings_date, publication = _day(pcf["date2"]), _day(pcf["date1"])
    if holdings_date > publication:
        raise ValueError("Actual date2 follows its PCF publication date1")
    return data, pcf, holdings_date, publication


def _money(value: Any, nav: Decimal) -> Decimal:
    """Require explicit signed TWD amount text within the source NAV range."""
    if not isinstance(value, str):
        raise ValueError("Invalid Capital asset fields")
    match = re.fullmatch(
        r"TWD (-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})", value)
    if match is None:
        raise ValueError("Invalid Capital asset currency or amount")
    amount = Decimal(match.group(1).replace(",", ""))
    if not -nav <= amount <= nav:
        raise ValueError("Capital asset outside signed NAV range")
    return amount


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Parse qualified current API fund contracts or raise ValueError.

    Derive identity from known raw fundName/product contracts. Use actual
    date2, native stock weight / 100 and signed explicit TWD assets / NAV.
    Map qualified 00415A RP once; reject other rps/bonds. Never silently
    omit them or balance gaps.
    Require a positive source stock aggregate matching native stock rows.
    Default/null-date aggregate0 responses are rejected, not repaired.
    No I/O, network, date/stamp or row-count allowlist, publication-date
    fallback, global sign flip or weight rescaling occurs.
    """
    data, pcf, holdings_date, publication = _context(raw, etf)
    symbol, active = etf.symbol, etf.is_active
    if (data["bonds"] != [] or not isinstance(data["rps"], list)
            or (data["rps"] and symbol != "00415A")):
        raise ValueError("Unqualified rps/bonds relationships require review")
    if data["characteristics"] != [] or _aggregate(pcf["totETF"]) != 0:
        raise ValueError("Unsupported additional Capital holdings structure")
    if pcf["dollar"] != "TWD":
        raise ValueError("Unsupported Capital NAV currency")
    nav = _number(pcf["nav"])
    if nav <= 0:
        raise ValueError("Capital NAV must be positive")
    if (not isinstance(fetched_at, str) or "T" not in fetched_at
            or datetime.fromisoformat(fetched_at).utcoffset() != timedelta(0)):
        raise ValueError("Invalid Capital UTC capture timestamp")
    stocks, assets, futures = data["stocks"], data["assets"], data["futures"]
    if (not isinstance(stocks, list) or not stocks
            or not isinstance(assets, list) or not assets
            or not isinstance(futures, list)):
        raise ValueError("Missing actual Capital holdings arrays")
    result: list[Holding] = []
    seen: set[str] = set()
    total = Decimal(0)
    for row in stocks:
        code, name, percentage = _stock(row, publication)
        if code in seen:
            raise ValueError("Duplicate Capital stock code")
        total += percentage
        seen.add(code)
        result.append(Holding(code, name, "stock", float(percentage / 100)))
    if total <= 0:
        raise ValueError("No positive native stock allocation")
    aggregate = _aggregate(pcf["totStock"])
    if aggregate <= 0:
        raise ValueError("A positive source stock aggregate is required")
    if abs(aggregate - total) > AGGREGATE_EPSILON:
        raise ValueError("Source stock aggregate disagrees with native stocks")
    seen = set()
    for row in assets:
        row = _mapping(row, {"asDesc", "asMoney", "asEname"})
        label = _label(row["asDesc"])
        if label not in ASSET_KINDS or label in seen:
            raise ValueError("Unknown or duplicate Capital asset label")
        if (not isinstance(row["asEname"], str)
                or (label == "應付贖回款"
                    and row["asEname"] != "Accrued Redemption")):
            raise ValueError("Invalid Capital asset fields")
        amount = _money(row["asMoney"], nav)
        result.append(Holding(label, label, ASSET_KINDS[label],
                              float(amount / nav)))
        seen.add(label)
    required = {"現金", "保證金"} if symbol in ("00919", "00982A") else {"現金"}
    if not required <= seen:
        raise ValueError("Missing required explicit Capital assets")
    rp_names: set[str] = set()
    for row in data["rps"]:
        row = _mapping(row, {"bondsName", "bondsMoney", "bondsEname"})
        name = _label(row["bondsName"])
        _label(row["bondsEname"])
        if name in rp_names or name in seen:
            raise ValueError("Duplicate Capital RP identity")
        rp_names.add(name)
        amount = _money(row["bondsMoney"], nav)
        result.append(Holding(name, name, "other", float(amount / nav)))
    seen = set()
    future_total = Decimal(0)
    for row in futures:
        code, name, percentage = _future(row)
        if code in seen:
            raise ValueError("Duplicate Capital future code")
        future_total += percentage
        result.append(Holding(code, name, "futures", None, None))
        seen.add(code)
    if abs(_aggregate(pcf["totFuture"]) - future_total) > AGGREGATE_EPSILON:
        raise ValueError("Source future aggregate disagrees with rows")
    snapshot = Snapshot(
        "1", symbol, "capital", active, holdings_date, fetched_at,
        SOURCE_URL, "agent_official", tuple(result),
    )
    return snapshot_from_dict(snapshot_to_dict(snapshot))
