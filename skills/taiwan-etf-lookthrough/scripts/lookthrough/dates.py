"""Select holdings dates and compare them using weekday approximations.

Weekdays exclude weekends only; public holidays are not deducted.
"""

from datetime import date
from typing import Literal


PASSIVE_TOLERANCE_WEEKDAYS = 5
DateStatus = Literal["common", "within_tolerance", "stale", "active_mismatch"]


def weekday_distance(a: date, b: date) -> int:
    """Count Monday through Friday in (a, b], requiring plain dates a <= b.

    Raises ValueError for invalid date types or a reversed interval.
    """
    if type(a) is not date or type(b) is not date or a > b:
        raise ValueError("Expected plain dates with a <= b")
    weeks, remainder = divmod((b - a).days, 7)
    return weeks * 5 + sum((a.weekday() + offset) % 7 < 5
                           for offset in range(1, remainder + 1))


def _validate_dates(chosen: dict[str, date]) -> None:
    """Reject malformed symbol/date mappings with ValueError."""
    if not isinstance(chosen, dict):
        raise ValueError("Expected a symbol/date dictionary")
    for symbol, value in chosen.items():
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Expected nonempty string symbols")
        if type(value) is not date:
            raise ValueError(f"Expected a plain date for {symbol}")


def select_dates(available: dict[str, list[date]]) -> dict[str, date]:
    """Use the latest common date, or each symbol's latest candidate.

    An empty mapping returns an empty mapping. Raises ValueError for an
    empty candidate list or malformed input; callers must first filter
    unavailable ETFs. Does not mutate the supplied candidates.
    """
    if not isinstance(available, dict):
        raise ValueError("Expected a symbol/candidate-list dictionary")
    candidates: dict[str, set[date]] = {}
    for symbol, values in available.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"No valid date candidates for {symbol}")
        for value in values:
            _validate_dates({symbol: value})
        candidates[symbol] = set(values)
    if not candidates:
        return {}
    common = set.intersection(*candidates.values())
    if common:
        latest = max(common)
        return dict.fromkeys(candidates, latest)
    return {symbol: max(values) for symbol, values in candidates.items()}


def classify_dates(
    chosen: dict[str, date],
    is_active: dict[str, bool],
    tolerance: int = PASSIVE_TOLERANCE_WEEKDAYS,
) -> dict[str, DateStatus]:
    """Classify dates against the latest selected date.

    Exact matches are common. Any active mismatch remains a mismatch even
    across weekends. Passive dates at the tolerance boundary are usable.
    Raises ValueError for invalid dates, tolerance or missing/invalid flags.
    Extra flags are allowed so callers can pass a larger ETF directory.
    """
    _validate_dates(chosen)
    if type(tolerance) is not int or tolerance < 0:
        raise ValueError("Tolerance must be a nonnegative integer")
    if not isinstance(is_active, dict):
        raise ValueError("Expected an active-flag dictionary")
    for symbol in chosen:
        if type(is_active.get(symbol)) is not bool:
            raise ValueError(f"Missing or invalid active flag for {symbol}")
    if not chosen:
        return {}
    reference = max(chosen.values())
    result: dict[str, DateStatus] = {}
    for symbol, selected in chosen.items():
        if selected == reference:
            result[symbol] = "common"
        elif is_active[symbol]:
            result[symbol] = "active_mismatch"
        elif weekday_distance(selected, reference) <= tolerance:
            result[symbol] = "within_tolerance"
        else:
            result[symbol] = "stale"
    return result
