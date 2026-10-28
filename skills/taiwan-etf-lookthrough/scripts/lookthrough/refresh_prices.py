"""Read date-matched official closing prices without filling absent quotes."""

from datetime import date
from decimal import Decimal
from urllib.parse import urlencode

from .issuers._assets import MAX_RAW_BYTES, number, read_json


def price_url(market: str, day: date) -> str:
    """Return the exact official route evidenced by the research package."""
    if market == "TWSE":
        return "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?" + (
            urlencode(dict(date=day.strftime("%Y%m%d"),
                           type="ALLBUT0999", response="json")))
    if market == "TPEx":
        return ("https://www.tpex.org.tw/www/zh-tw/afterTrading/"
                "dailyQuotes?") + (
            urlencode(dict(date=day.strftime("%Y/%m/%d"),
                           type="EW", response="json")))
    raise ValueError("Unsupported official quote market")


def read_prices(raw: bytes, market: str, day: date) -> dict[str, Decimal]:
    """Require the returned date and labeled closing-price column.

    Suspensions and unavailable closing prices stay absent. They must not
    become zero or be replaced by another day's quote. ETF names and other
    symbols in the exchange-wide response are not company classifications.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RAW_BYTES:
        raise ValueError("Invalid official quote response size")
    payload = read_json(raw)
    if (not isinstance(payload, dict)
            or payload.get("date") != day.strftime("%Y%m%d")
            or str(payload.get("stat", "")).lower() != "ok"):
        raise ValueError("Official quote status/date mismatch")
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise ValueError("Missing official quote tables")
    code_header = "證券代號" if market == "TWSE" else "代號"
    close_header = "收盤價" if market == "TWSE" else "收盤"
    selected = [t for t in tables if isinstance(t, dict)
                and code_header in t.get("fields", [])
                and close_header in t.get("fields", [])
                and (market != "TPEx"
                     or t.get("title") == "上櫃股票行情")]
    if len(selected) != 1:
        raise ValueError("Ambiguous official closing-price table")
    table = selected[0]
    fields = table["fields"]
    code_index, close_index = (fields.index(code_header),
                               fields.index(close_header))
    records = table.get("data")
    if not isinstance(records, list) or not 1 <= len(records) <= 20000:
        raise ValueError("Invalid official quote row count")
    result: dict[str, Decimal] = {}
    seen: set[str] = set()
    for row in records:
        if not isinstance(row, list) or len(row) != len(fields):
            raise ValueError("Malformed official quote row")
        code, text = row[code_index], row[close_index]
        text = text.strip() if isinstance(text, str) else text
        if not isinstance(code, str) or not code or code in seen:
            raise ValueError("Duplicate/invalid official quote symbol")
        seen.add(code)
        if text in ("", "--", "---", "----", "-----", None):
            continue
        value = number(text)
        if value <= 0:
            raise ValueError("Nonpositive official closing price")
        result[code] = value
    return result
