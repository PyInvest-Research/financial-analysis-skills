"""Parse qualified Yuanta Daily actual holdings, excluding creation baskets."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import re
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from ..models import Holding, Snapshot, snapshot_from_dict, snapshot_to_dict

if TYPE_CHECKING:
    from .registry import SupportedEtf


IDENTITIES = {
    "0051": ('1078', '元大台灣中型100證券投資信託基金'),
    "0053": ('1081', '元大台灣ETF傘型證券投資信託基金之電子科技證券投資信託基金'),
    "0055": ('1083', '元大台灣ETF傘型證券投資信託基金之金融證券投資信託基金'),
    "006201": ('1099', '元大櫃買ETF傘型證券投資信託基金之富櫃50證券投資信託基金'),
    "006203": ('1103', '元大摩臺證券投資信託基金'),
    "00713": ('1164', '元大台灣高股息低波動ETF證券投資信託基金'),
    "00850": ('1198', '元大臺灣ESG永續ETF證券投資信託基金'),
    "00940": ('1245', '元大臺灣價值高息ETF證券投資信託基金'),
    "0050": ("1066", "元大台灣卓越50證券投資信託基金"),
    "0056": ("1084", "元大台灣高股息證券投資信託基金"),
}
WEIGHT_KEYS = {"Summary", "StockWeights", "FutureWeights", "ETFWeights",
               "BondWeights"}
ROW_KEYS = {"code", "ym", "name", "ename", "weights", "qty"}
SUMMARY_KEYS = {"code", "name", "stkvalues", "futvalues", "etfvalues",
                "bndvalues", "fundsize"}


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys rather than selecting an arbitrary value."""
    if len(dict(pairs)) != len(pairs):
        raise ValueError("Duplicate Daily API field")
    return dict(pairs)


def _nonfinite(text: str) -> Decimal:
    """Reject nonstandard NaN/Infinity JSON constants throughout the body."""
    raise ValueError(f"Nonfinite Daily API constant: {text}")


def _mapping(
    value: Any, keys: set[str], *, exact: bool = True,
) -> dict[str, Any]:
    """Require known actual-holdings structure or explicit required fields."""
    if not isinstance(value, dict) or not keys <= value.keys():
        raise ValueError("Missing or invalid Daily API object")
    if exact and value.keys() != keys:
        raise ValueError("Unsupported Daily API object fields")
    return value


def _number(value: Any) -> Decimal:
    """Accept finite native JSON numbers, excluding strings and booleans."""
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("Invalid native numeric field")
    return value


def _percentage(value: Any) -> Decimal:
    """Validate native percentage points at their disclosed precision."""
    number = _number(value)
    if (not 0 <= number <= 100
            or number * 100 != (number * 100).to_integral()):
        raise ValueError("Invalid native percentage range or precision")
    return number


def _source_day(text: Any) -> date:
    """Read YYYYMMDD without a fetch/announcement fallback."""
    if not isinstance(text, str) or not re.fullmatch(r"\d{8}", text):
        raise ValueError("Invalid source date format")
    return datetime.strptime(text, "%Y%m%d").date()


def _memo_day(value: Any) -> date:
    """Read the Chinese supplied-date statement as corroboration."""
    if not isinstance(value, list):
        raise ValueError("Missing Daily API Memo")
    locales: dict[str, str] = {}
    for item in value:
        item = _mapping(item, {"locale", "content"})
        locale = item["locale"]
        if locale not in ("en", "zh-tw") or locale in locales:
            raise ValueError("Unknown or duplicate Memo locale")
        if not isinstance(item["content"], list):
            raise ValueError("Invalid Memo content")
        texts = [_mapping(row, {"text"})["text"] for row in item["content"]]
        if any(not isinstance(text, str) for text in texts):
            raise ValueError("Invalid Memo text")
        locales[locale] = "".join(texts)
    matches = re.findall(
        r"資料內容由元大投信\s*(\d{4}/\d{2}/\d{2})\s*提供",
        locales.get("zh-tw", ""),
    )
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous supplied-date statement")
    return _source_day(matches[0].replace("/", ""))


def _source_url(url: str, symbol: str) -> None:
    """Validate the known official Daily endpoint while preserving its URL."""
    if not isinstance(url, str):
        raise ValueError("Missing official source URL")
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != "etfapi.yuantaetfs.com"
            or parts.path != "/ectranslation/api/bridge" or parts.fragment
            or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443)):
        raise ValueError("Unsupported official Daily source URL")
    expected = {
        "APIType": "ETFAPI", "CompanyName": "YUANTAFUNDS",
        "PageName": f"/product/detail/{symbol}/ratio", "DeviceId": "null",
        "FuncId": "PCF/Daily", "AppName": "ETF", "Device": "3",
        "Platform": "ETF", "ticker": symbol,
    }
    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    if query != {key: [value] for key, value in expected.items()}:
        raise ValueError("Mismatched or ambiguous Daily query identity")


def _row(value: Any, *, futures: bool = False) -> tuple[str, str, Decimal]:
    """Check actual identity, quantity, percentage and optional expiry."""
    row = _mapping(value, ROW_KEYS)
    for field in ("code", "name", "ename"):
        if (not isinstance(row[field], str) or not row[field].strip()
                or row[field] != row[field].strip()):
            raise ValueError("Invalid actual holding identity")
    pattern = r"[A-Z0-9]+" if futures else r"\d{4}[A-Z]?"
    if not re.fullmatch(pattern, row["code"]):
        raise ValueError("Invalid actual holding code")
    quantity = _number(row["qty"])
    if quantity < 0 or quantity != quantity.to_integral():
        raise ValueError("Invalid actual holding quantity")
    if futures:
        expiry = row["ym"]
        if not isinstance(expiry, str) or not re.fullmatch(r"\d{6}", expiry):
            raise ValueError("Invalid future contract month")
        date(int(expiry[:4]), int(expiry[4:]), 1)
    elif row["ym"] is not None:
        raise ValueError("Unsupported actual stock month field")
    return row["code"], row["name"], _percentage(row["weights"])


CASH_KEYS = {"TranDate", "CashPosition", "MMF", "Margin", "RP",
             "ForwardExchange", "Currency", "CouponRate", "InvestArea",
             "CreditRating"}
ASSET_KEYS = {"code", "name", "ename", "crncy", "exrate", "rto", "amt",
              "trandate"}
CASH_LABELS = {
    ("1500", "現金", "CASH"): "cash",
    ("1200", "附買回債券", "RP"): "other",
    ("3300", "應付受益權單位買回款", "PAYABLES FOR UNITS REDEEMED"): "other",
    ("3301", "應付申購預收款", "PAYABLES"): "other",
}


def _text(value: Any) -> str:
    """Require a nonblank native label, preserving original source text."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing Cash label")
    return value


def _cash_rows(cash: dict[str, Any], key: str) -> list[Any]:
    """Require an explicit source array without assuming missing means zero."""
    rows = cash[key]
    if not isinstance(rows, list):
        raise ValueError("Invalid Cash array")
    return rows


def _cash_holdings(
    value: Any, day: date, fund_id: str, nav: Decimal,
) -> list[Holding]:
    """Map qualified NTD amounts; retain unanchored RP as unknown other.

    Date and fund metadata corroborate Memo=null. RP summaries reconcile
    exactly and count once. Currency/InvestArea are metadata, not assets.
    Unknown categories, identities or nonfinite numeric fields raise.
    """
    cash = _mapping(value, CASH_KEYS)
    if _source_day(cash["TranDate"]) != day:
        raise ValueError("Contradictory Cash date")
    for key in ("MMF", "ForwardExchange", "CouponRate", "CreditRating"):
        if cash[key] != []:
            raise ValueError("Unqualified Cash category")
    for key, keys, id_key, day_key in (
        ("Currency", {"fundId", "cDate", "crncy", "endRate"},
         "fundId", "cDate"),
        ("InvestArea", {"TRAN_DATE", "FUND_ID", "name", "ename",
                        "HOLD_RTO", "CNT"}, "FUND_ID", "TRAN_DATE"),
    ):
        rows = _cash_rows(cash, key)
        if not rows:
            raise ValueError("Missing corroborating Cash metadata")
        seen = set()
        for row in rows:
            row = _mapping(row, keys)
            if row[id_key] != fund_id or _source_day(row[day_key]) != day:
                raise ValueError("Contradictory Cash metadata identity/date")
            label = _text(row["crncy"] if key == "Currency" else row["name"])
            if label in seen:
                raise ValueError("Duplicate Cash metadata")
            seen.add(label)
            if key == "Currency":
                if _number(row["endRate"]) <= 0:
                    raise ValueError("Invalid reference exchange rate")
            else:
                _text(row["ename"])
                _percentage(row["HOLD_RTO"])
                count = _number(row["CNT"])
                if count < 0 or count != count.to_integral():
                    raise ValueError("Invalid InvestArea count")
    holdings: list[Holding] = []
    rp_summary: Decimal | None = None
    rp_rows: list[tuple[str, str, Decimal]] = []
    for key in ("CashPosition", "Margin", "RP"):
        seen = set()
        for row in _cash_rows(cash, key):
            row = _mapping(row, ASSET_KEYS)
            if _source_day(row["trandate"]) != day:
                raise ValueError("Contradictory Cash asset date")
            name, ename = _text(row["name"]), _text(row["ename"])
            amount = _number(row["amt"])
            _number(row["rto"])
            _number(row["exrate"])
            code = row["code"]
            if code in seen:
                raise ValueError("Duplicate Cash asset identity")
            seen.add(code)
            if key == "RP":
                if (not isinstance(code, str)
                        or not re.fullmatch(r"[A-Z0-9]+", code)
                        or row["crncy"] is not None
                        or _number(row["rto"]) != 0
                        or _number(row["exrate"]) != 0 or amount < 0):
                    raise ValueError("Unqualified RP shape")
                rp_rows.append((code, name, amount))
                continue
            if row["crncy"] != "NTD":
                raise ValueError("Unqualified Cash asset currency")
            if key == "Margin":
                if (code, name, ename) != (None, "保證金", "Margin"):
                    raise ValueError("Unqualified margin identity")
                code, kind = "Margin", "other"
            else:
                kind = CASH_LABELS.get((code, name, ename))
                if kind is None:
                    raise ValueError("Unqualified CashPosition identity")
                if code == "1200":
                    rp_summary = amount
            holdings.append(Holding(code, name, kind, float(amount / nav)))
    if rp_summary is not None:
        if not rp_rows or sum(row[2] for row in rp_rows) != rp_summary:
            raise ValueError("RP details disagree with NTD summary")
    else:
        holdings.extend(Holding(code, name, "other", None, None)
                        for code, name, _ in rp_rows)
    return holdings


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Return a manual official Snapshot from qualified actual-holdings JSON.

    Stock weights are native FundWeights.StockWeights percentages / 100.
    InKind and PCF creation-unit cash never become whole-fund holdings.
    Futures NAV/exposure remain unknown and Cash=null remains unknown.
    Validate raw identity, source dates and supported actual-holdings shape.
    No I/O, network, price splice, date/stamp allowlist or rescaling occurs.
    Invalid or unsupported input raises ValueError, preserving source bytes.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > 2 * 1024 * 1024:
        raise ValueError("Invalid Daily API bytes or size")
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object,
            parse_float=Decimal, parse_int=Decimal, parse_constant=_nonfinite,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Malformed UTF-8 Daily API JSON") from error
    payload = _mapping(
        payload, {"PCF", "InKind", "Cash", "FundWeights", "Memo"})
    pcf = _mapping(payload["PCF"], {"markcd", "fundid", "fullname", "trandate",
                                    "anndate", "upddate"}, exact=False)
    symbol = pcf["markcd"]
    if (not isinstance(symbol, str) or symbol not in IDENTITIES
            or (pcf["fundid"], pcf["fullname"]) != IDENTITIES[symbol]
            or etf.issuer != "yuanta" or etf.symbol != symbol
            or etf.is_active is not False):
        raise ValueError("Mismatched raw Yuanta identity or management type")
    _source_url(etf.source_url, symbol)
    holdings_date = _source_day(pcf["trandate"])
    announcement_date = _source_day(pcf["anndate"])
    update = pcf["upddate"]
    if (not isinstance(update, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", update)):
        raise ValueError("Invalid PCF update timestamp")
    if (datetime.fromisoformat(update).date() != holdings_date
            or announcement_date < holdings_date):
        raise ValueError("Contradictory actual holdings date evidence")
    if not isinstance(fetched_at, str) or "T" not in fetched_at:
        raise ValueError("Invalid capture UTC timestamp")
    if datetime.fromisoformat(fetched_at).utcoffset() != timedelta(0):
        raise ValueError("Capture timestamp must be UTC")
    weights = _mapping(payload["FundWeights"], WEIGHT_KEYS)
    summary = _mapping(weights["Summary"], SUMMARY_KEYS)
    if summary["code"] != pcf["fundid"]:
        raise ValueError("Summary code disagrees with raw fund identity")
    if (weights["ETFWeights"] != []
            or weights["BondWeights"] != []
            or _number(summary["etfvalues"]) != 0
            or _number(summary["bndvalues"]) != 0):
        raise ValueError("Unsupported actual cash, ETF or bond asset mapping")
    fundsize = _number(summary["fundsize"])
    stock_amount = _number(summary["stkvalues"])
    future_amount = _number(summary["futvalues"])
    if fundsize <= 0 or not 0 <= stock_amount <= fundsize or future_amount < 0:
        raise ValueError("Invalid actual Summary amounts")
    cash_holdings: list[Holding] = []
    if payload["Cash"] is not None:
        cash_holdings = _cash_holdings(
            payload["Cash"], holdings_date, pcf["fundid"], fundsize)
    if payload["Memo"] is not None or payload["Cash"] is None:
        if _memo_day(payload["Memo"]) != holdings_date:
            raise ValueError("Contradictory Memo date")
    stocks, futures = weights["StockWeights"], weights["FutureWeights"]
    if (not isinstance(stocks, list) or not stocks
            or not isinstance(futures, list)
            or bool(futures) != (future_amount != 0)):
        raise ValueError("Missing or inconsistent actual holding arrays")
    holdings: list[Holding] = []
    seen: set[str] = set()
    percentages: list[Decimal] = []
    for value in stocks:
        code, name, percent = _row(value)
        if code in seen:
            raise ValueError("Duplicate actual stock code")
        seen.add(code)
        percentages.append(percent)
        holdings.append(Holding(code, name, "stock", float(percent / 100)))
    tolerance = Decimal(len(stocks)) * Decimal("0.00005")
    if abs(sum(percentages) / 100 - stock_amount / fundsize) > tolerance:
        raise ValueError(
            "Actual stock ratio exceeds native rounding tolerance")
    seen = set()
    for value in futures:
        code, name, _ = _row(value, futures=True)
        if code in seen:
            raise ValueError("Duplicate actual future code")
        seen.add(code)
        holdings.append(Holding(code, name, "futures", None, None))
    holdings.extend(cash_holdings)
    snapshot = Snapshot(
        "1", symbol, "yuanta", False, holdings_date, fetched_at,
        etf.source_url, "agent_official", tuple(holdings),
    )
    return snapshot_from_dict(snapshot_to_dict(snapshot))
