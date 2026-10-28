"""Test holdings date selection and weekday tolerance boundaries."""

from datetime import date, datetime
import unittest

import support  # noqa: F401
from lookthrough.dates import classify_dates, select_dates, weekday_distance


class DateTests(unittest.TestCase):
    """Keep common-date selection and active mismatches explicit."""

    def test_weekday_distance_boundaries(self) -> None:
        """Count only weekdays in the half-open interval (a, b]."""
        cases = [(15, 22, 5), (16, 19, 1), (17, 18, 0),
                 (18, 19, 1), (19, 19, 0), (19, 20, 1)]
        for start, end, expected in cases:
            with self.subTest(start=start, end=end):
                self.assertEqual(weekday_distance(date(2026, 10, start),
                                                  date(2026, 10, end)),
                                 expected)
        self.assertEqual(weekday_distance(date(2025, 12, 31),
                                          date(2026, 1, 2)), 2)
        self.assertEqual(weekday_distance(date.min, date.max), 2608614)

    def test_invalid_distance_inputs(self) -> None:
        """Reject reversed intervals and dates with time components."""
        with self.assertRaises(ValueError):
            weekday_distance(date(2026, 10, 22), date(2026, 10, 15))
        for bad in ("2026-10-22", None, datetime(2026, 10, 22)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                weekday_distance(date(2026, 10, 15), bad)

    def test_latest_common(self) -> None:
        """Use the newest intersection, ignoring order and duplicates."""
        first = date(2026, 10, 20)
        common = date(2026, 10, 21)
        latest = date(2026, 10, 22)
        available = {"A": [latest, common, first, common],
                     "B": [common, first]}
        self.assertEqual(select_dates(available), {"A": common, "B": common})
        self.assertEqual(available["A"], [latest, common, first, common])

    def test_no_common(self) -> None:
        """Fall back to each symbol's latest date."""
        a, b = date(2026, 10, 22), date(2026, 10, 21)
        self.assertEqual(select_dates({"A": [a], "B": [b]}),
                         {"A": a, "B": b})
        self.assertEqual(select_dates({"A": [b, a]}), {"A": a})
        self.assertEqual(select_dates({}), {})

    def test_unavailable_or_malformed_candidates(self) -> None:
        """Never fabricate dates or silently drop an unavailable ETF."""
        for bad in ({"A": []}, {"A": [date.today()], "B": []},
                    {"A": ["2026-10-22"]}, {"A": None}, [],
                    {"A": (date.today(),)}, {1: [date.today()]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                select_dates(bad)

    def test_classify_default_boundary(self) -> None:
        """Five weekdays are usable; six are stale; active lags mismatch."""
        chosen = {"A": date(2026, 10, 22), "B": date(2026, 10, 15),
                  "C": date(2026, 10, 14), "ACT": date(2026, 10, 21)}
        self.assertEqual(classify_dates(chosen, dict.fromkeys(chosen, False)
                                        | {"ACT": True}),
                         {"A": "common", "B": "within_tolerance",
                          "C": "stale", "ACT": "active_mismatch"})

    def test_active_weekend_mismatch(self) -> None:
        """Active mismatches remain mismatches at zero weekday distance."""
        chosen = {"A": date(2026, 10, 18), "B": date(2026, 10, 17),
                  "C": date(2026, 10, 16)}
        self.assertEqual(classify_dates(chosen, {"A": True, "B": True,
                                                 "C": False}, tolerance=0),
                         {"A": "common", "B": "active_mismatch",
                          "C": "within_tolerance"})
        self.assertEqual(classify_dates({}, {}), {})

    def test_custom_tolerance(self) -> None:
        """Honor explicit passive tolerance, including zero."""
        chosen = {"A": date(2026, 10, 22), "B": date(2026, 10, 21)}
        flags = {"A": False, "B": False}
        self.assertEqual(classify_dates(chosen, flags, 0)["B"], "stale")
        self.assertEqual(classify_dates(chosen, flags, 1)["B"],
                         "within_tolerance")

    def test_invalid_classification_inputs(self) -> None:
        """Reject bad tolerances, missing flags and non-boolean flags."""
        chosen = {"A": date(2026, 10, 22)}
        for tolerance in (-1, True, 1.5, "5", None):
            with self.subTest(tolerance=tolerance):
                with self.assertRaises(ValueError):
                    classify_dates(chosen, {"A": False}, tolerance)
        for flags in ({}, {"A": 1}, None):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                classify_dates(chosen, flags)
        for bad in ({"A": "2026-10-22"}, [], {1: date.today()}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                classify_dates(bad, {"A": False})
