"""Validate bounded source JSON and exact disclosed NAV arithmetic."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import re
from typing import Any


MAX_RAW_BYTES = 10 * 1024 * 1024


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate keys rather than overwriting source evidence."""
    if len(dict(items)) != len(items):
        raise ValueError("Duplicate source JSON key")
    return dict(items)


def _constant(value: str) -> None:
    """Reject nonstandard JSON NaN and Infinity values."""
    raise ValueError(f"Nonfinite source JSON constant: {value}")


def read_json(raw: bytes, limit: int = MAX_RAW_BYTES) -> Any:
    """Decode bounded UTF-8 JSON, keeping numeric values as Decimal."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise ValueError("Source bytes are empty or exceed the read cap")
    try:
        return json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=_pairs,
            parse_int=Decimal, parse_float=Decimal, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Malformed source JSON") from error


def number(value: Any, *, native: bool = False) -> Decimal:
    """Read finite source amounts; native JSON numbers exclude text/bools."""
    if native and type(value) is not Decimal:
        raise ValueError("Expected a native numeric source amount")
    if type(value) not in (str, int, float, Decimal):
        raise ValueError("Invalid source numeric type")
    text = str(value).strip()
    if not re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
        raise ValueError("Invalid source number")
    result = Decimal(text.replace(",", ""))
    if not result.is_finite():
        raise ValueError("Nonfinite source amount")
    return result


def nav_number(value: Any, *, native: bool = False) -> Decimal:
    """Require a strictly positive finite disclosed total NAV."""
    result = number(value, native=native)
    if result <= 0:
        raise ValueError("NAV must be positive")
    return result


def check_stock(code: Any, name: Any, shares: Decimal,
                amount: Decimal, nav: Decimal) -> None:
    """Check Taiwan security identity and nonnegative disclosed amounts."""
    if (not isinstance(code, str)
            or not re.fullmatch(r"\d{4}[A-Z]?", code)
            or not isinstance(name, str) or not name.strip()
            or shares < 0 or shares != shares.to_integral()
            or not 0 <= amount <= nav):
        raise ValueError("Invalid stock identity or amount")


def check_percentage(value: Any, amount: Decimal,
                     nav: Decimal, digits: int) -> None:
    """Match printed percentage to amount/NAV at its disclosed precision."""
    if (not isinstance(value, str)
            or not re.fullmatch(rf"\d+\.\d{{{digits}}}%", value)):
        raise ValueError("Invalid disclosed stock percentage unit")
    percent = Decimal(value[:-1])
    rounded = (amount / nav * 100).quantize(
        Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    if percent != rounded:
        raise ValueError("Stock percentage disagrees with same-date NAV")


def asset_day(value: Any) -> date:
    """Convert an explicit UTC asset timestamp to its Taipei calendar day."""
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Missing source asset timestamp")
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("Asset timestamp must explicitly use UTC")
    return stamp.astimezone(timezone(timedelta(hours=8))).date()
