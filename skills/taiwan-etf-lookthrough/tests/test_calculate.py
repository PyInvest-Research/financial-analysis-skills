"""Check offline exposure arithmetic against six saved hand calculations."""

from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import unittest

import support  # noqa: F401
from lookthrough.calculate import LookthroughResult, compute_lookthrough
from lookthrough.dates import classify_dates
from lookthrough.directory import CompanyDirectory
from lookthrough.models import Holding, snapshot_from_dict
from lookthrough.portfolio import Portfolio, Position, load_portfolio
from lookthrough.validate import ValidationResult, validate_snapshot


FIXTURES = Path(__file__).parent / "fixtures/handcalc"
TODAY = date(2026, 10, 25)


class CalculateTests(unittest.TestCase):
    """Preserve NAV denominators, unresolved values and signed futures."""

    def setUp(self) -> None:
        """Load only the immutable saved directory subset and C1 inputs."""
        self.directory = CompanyDirectory.from_file(
            FIXTURES / "directory.json")
        self.load_case(1)

    def load_case(self, number: int) -> None:
        """Load synthetic holdings and a portfolio from JSON fixtures."""
        folder = FIXTURES / f"c{number}"
        self.portfolio = load_portfolio(folder / "portfolio.json")
        payload = json.loads((folder / "snapshots.json").read_text())
        self.snapshots = {
            symbol: snapshot_from_dict(row) if row else None
            for symbol, row in payload.items()
        }

    def compute(
        self, validations: dict[str, ValidationResult] | None = None,
    ) -> LookthroughResult:
        """Validate fixtures and derive date statuses before computing."""
        snapshots = {k: v for k, v in self.snapshots.items() if v is not None}
        if validations is None:
            validations = {k: validate_snapshot(v, self.directory, TODAY)
                           for k, v in snapshots.items()}
        statuses = classify_dates(
            {k: v.holdings_date for k, v in snapshots.items()},
            {k: v.is_active for k, v in snapshots.items()},
        )
        return compute_lookthrough(
            self.portfolio, self.snapshots, validations, statuses,
            self.directory, {"ETFX": "範圍外，未取得官方檔"},
        )

    def assert_c1_amounts(self, result: LookthroughResult) -> None:
        """Assert every C1 stock, bucket, share and futures amount."""
        amounts = {"2330": 640000, "2881": 120000, "2317": 80000,
                   "2882": 80000, "2454": 60000}
        self.assertEqual([c.issuer_code for c in result.companies],
                         list(amounts))
        for company in result.companies:
            self.assertAlmostEqual(company.amount,
                                   amounts[company.issuer_code], places=6)
            self.assertAlmostEqual(company.share,
                                   company.amount / result.total_value,
                                   places=6)
        self.assertAlmostEqual(result.identified_stock_value, 980000, places=6)
        self.assertAlmostEqual(result.cash_value, 16000, places=6)
        self.assertAlmostEqual(result.other_value, 0, places=6)
        self.assertAlmostEqual(result.unresolved_value, 0, places=6)
        self.assertAlmostEqual(result.unclassified_gap_value, 4000, places=6)
        self.assertEqual(result.futures, (("TXFK6", 12000),))

    def test_c1_multiple_etfs_and_direct(self) -> None:
        """Add independent source amounts without renormalizing stocks."""
        result = self.compute()
        self.assert_c1_amounts(result)
        self.assertAlmostEqual(result.total_value, 1000000, places=6)
        self.assertAlmostEqual(result.coverage, .98, places=6)
        company = result.companies[0]
        self.assertEqual(company.occurrences, 3)
        self.assertEqual([(s.source, s.security_code, s.amount)
                          for s in company.sources],
                         [("ETFA", "2330", 240000),
                          ("ETFB", "2330", 200000),
                          ("direct", "2330", 200000)])

    def test_c2_preferred_consolidates_issuer(self) -> None:
        """Retain common/preferred rows from one ETF occurrence."""
        self.load_case(2)
        result = self.compute()
        self.assert_c1_amounts(result)
        company = next(c for c in result.companies if c.issuer_code == "2881")
        self.assertEqual(company.occurrences, 1)
        self.assertEqual(
            [(s.security_code, s.amount) for s in company.sources],
            [("2881", 80000), ("2881A", 40000)])

    def test_c3_unavailable_etf_stays_in_denominator(self) -> None:
        """Retain unavailable market values and the supplied reason."""
        self.load_case(3)
        result = self.compute()
        self.assert_c1_amounts(result)
        self.assertAlmostEqual(result.total_value, 1100000, places=6)
        self.assertAlmostEqual(result.companies[0].share,
                               640000 / 1100000, places=6)
        self.assertAlmostEqual(result.not_looked_through_value, 100000,
                               places=6)
        position = next(p for p in result.positions if p.symbol == "ETFX")
        self.assertEqual(position.status, "not_looked_through")
        self.assertEqual(position.reason, "範圍外，未取得官方檔")

    def test_c4_dates_change_metadata_only(self) -> None:
        """Keep amounts through passive stale and active mismatch dates."""
        self.load_case(4)
        original = self.snapshots["ETFB"]
        for day, active, status in ((15, False, "within_tolerance"),
                                    (14, False, "stale"),
                                    (21, True, "active_mismatch")):
            with self.subTest(day=day, active=active):
                self.snapshots["ETFB"] = replace(
                    original, holdings_date=date(2026, 10, day),
                    is_active=active)
                result = self.compute()
                self.assert_c1_amounts(result)
                self.assertAlmostEqual(result.coverage, .98, places=6)
                position = next(p for p in result.positions
                                if p.symbol == "ETFB")
                self.assertEqual(position.date_status, status)

    def test_c5_duplicate_input_merges(self) -> None:
        """Expose the portfolio's duplicate-merge warning and C1 amounts."""
        self.load_case(5)
        result = self.compute()
        self.assert_c1_amounts(result)
        self.assertEqual(len(result.positions), 3)
        self.assertTrue(any("合併" in w for w in result.warnings))

    def test_c6_unknown_futures_and_negative_gap(self) -> None:
        """Retain unconverted futures and negative reconciliation balances."""
        self.load_case(6)
        result = self.compute()
        self.assertEqual(result.futures, (("TXFK6", None),))
        self.assertAlmostEqual(result.unclassified_gap_value, -4000, places=6)
        self.assertAlmostEqual(result.cash_value, 24000, places=6)
        self.assertAlmostEqual(result.identified_stock_value, 980000, places=6)
        self.assertTrue(any("負" in w and "ETFA" in w
                            for w in result.warnings))

    def test_unresolved_security_counts_as_gap(self) -> None:
        """Treat ETF securities as unresolved, without inventing issuers."""
        original = self.snapshots["ETFA"]
        self.snapshots["ETFA"] = replace(original, holdings=original.holdings
                                         + (Holding("0056", "ETF", "stock",
                                                    .01),))
        result = self.compute()
        self.assertAlmostEqual(result.unresolved_value, 4000, places=6)
        self.assertAlmostEqual(result.unclassified_gap_value, 0, places=6)
        self.assertNotIn("0056", [c.issuer_code for c in result.companies])

    def test_failed_validation_not_looked_through(self) -> None:
        """Do not use any holdings of a failed snapshot."""
        result = self.compute({"ETFA": ValidationResult("fail", ()),
                               "ETFB": ValidationResult("pass", ())})
        self.assertAlmostEqual(result.not_looked_through_value, 400000,
                               places=6)
        self.assertAlmostEqual(result.identified_stock_value, 600000, places=6)
        self.assertEqual(result.futures, ())
        self.assertEqual(result.positions[0].status, "not_looked_through")

    def test_missing_validation_not_looked_through(self) -> None:
        """Require validation evidence instead of treating absence as pass."""
        result = self.compute({})
        self.assertAlmostEqual(result.not_looked_through_value, 800000,
                               places=6)
        self.assertAlmostEqual(result.identified_stock_value, 200000, places=6)

    def test_missing_stock_weight_fails_without_zero_fill(self) -> None:
        """Reject an ETF containing a missing required stock NAV weight."""
        original = self.snapshots["ETFA"]
        self.snapshots["ETFA"] = replace(original, holdings=(
            replace(original.holdings[0], weight=None),
        ) + original.holdings[1:])
        result = self.compute()
        self.assertAlmostEqual(result.not_looked_through_value, 400000,
                               places=6)
        self.assertEqual(result.positions[0].validation.status, "fail")

    def test_amounts_reconcile_to_total(self) -> None:
        """Reconcile all six cases excluding additional futures notionals."""
        for number in range(1, 7):
            with self.subTest(case=number):
                self.load_case(number)
                result = self.compute()
                self.assertAlmostEqual(
                    result.identified_stock_value + result.unresolved_value
                    + result.cash_value + result.other_value
                    + result.unclassified_gap_value
                    + result.not_looked_through_value,
                    result.total_value, places=6)

    def test_unknown_and_direct_preferred_stay_unresolved(self) -> None:
        """Do not guess security names for unknown direct holdings."""
        self.portfolio = replace(self.portfolio, positions=(
            Position("9999", "stock", 400),
            Position("2881A", "stock", 600),
        ))
        result = self.compute()
        self.assertEqual(result.companies, ())
        self.assertAlmostEqual(result.unresolved_value, 1000, places=6)
        self.assertAlmostEqual(result.coverage, 0, places=6)
        self.assertEqual([p.status for p in result.positions],
                         ["direct", "direct"])
        self.assertTrue(all(p.reason for p in result.positions))

    def test_unknown_snapshot_stock_stays_unresolved(self) -> None:
        """Preserve the entire weight of an unknown stock security."""
        original = self.snapshots["ETFA"]
        self.snapshots["ETFA"] = replace(original, holdings=(
            replace(original.holdings[0], code="9999", name="Unknown"),
        ) + original.holdings[1:])
        self.assertAlmostEqual(self.compute().unresolved_value, 240000,
                               places=6)

    def test_preferred_without_matching_name_stays_unresolved(self) -> None:
        """Require disclosed issuer-name linkage for preferred securities."""
        self.load_case(2)
        original = self.snapshots["ETFB"]
        self.snapshots["ETFB"] = replace(original, holdings=tuple(
            replace(h, name="Unverified") if h.code == "2881A" else h
            for h in original.holdings))
        result = self.compute()
        self.assertAlmostEqual(result.unresolved_value, 40000, places=6)
        company = next(c for c in result.companies if c.issuer_code == "2881")
        self.assertAlmostEqual(company.amount, 80000, places=6)
        self.assertTrue(result.warnings)

    def test_missing_nonstock_weight_remains_in_gap(self) -> None:
        """Report absent nonstock weights without inferring cash amounts."""
        original = self.snapshots["ETFA"]
        self.snapshots["ETFA"] = replace(original, holdings=tuple(
            replace(h, weight=None) if h.kind == "cash" else h
            for h in original.holdings))
        result = self.compute()
        self.assertAlmostEqual(result.unclassified_gap_value, 20000, places=6)
        self.assertTrue(any("TWD" in w and "缺漏" in w
                            for w in result.warnings))

    def test_signed_and_aggregated_futures(self) -> None:
        """Add signed notionals by contract without altering NAV buckets."""
        original = self.snapshots["ETFB"]
        self.snapshots["ETFB"] = replace(original, holdings=original.holdings
                                         + (Holding("TXFK6", "Future",
                                                    "futures", .1, -.04),))
        result = self.compute()
        self.assertEqual(result.futures, (("TXFK6", -4000),))
        self.assertAlmostEqual(result.unclassified_gap_value, 4000, places=6)
        self.snapshots["ETFB"] = replace(original, holdings=original.holdings
                                         + (Holding("TXFK6", "Future",
                                                    "futures", None),))
        self.assertEqual(self.compute().futures, (("TXFK6", None),))

    def test_gap_tolerance_strict_boundary_and_other(self) -> None:
        """Keep exact boundary gaps and count other disclosed NAV assets."""
        original = self.snapshots["ETFA"]
        for gap in (.00049, .0005, -.00049, -.0005):
            with self.subTest(gap=gap):
                holdings = original.holdings[:4] + (
                    Holding("TWD", "Cash", "cash", .04),
                    Holding("OTHER", "Other", "other",
                            float(str(.01 - gap))),)
                self.snapshots["ETFA"] = replace(original, holdings=holdings)
                result = self.compute()
                expected = 0 if abs(gap) < .0005 else 400000 * gap
                self.assertAlmostEqual(result.unclassified_gap_value,
                                       expected, places=6)
                self.assertAlmostEqual(result.other_value,
                                       400000 * (.01 - gap), places=6)

    def test_empty_portfolio_has_zero_coverage(self) -> None:
        """Handle an empty portfolio without division by zero."""
        self.portfolio = Portfolio(TODAY, (), (), "")
        result = self.compute()
        self.assertEqual(result.total_value, 0)
        self.assertEqual(result.coverage, 0)
        self.assertEqual(result.companies, ())

    def test_output_order_independent_of_holding_order(self) -> None:
        """Produce stable company, source and contract ordering."""
        first = self.compute()
        self.snapshots = {
            key: replace(value, holdings=tuple(reversed(value.holdings)))
            for key, value in reversed(self.snapshots.items())
        }
        second = self.compute()
        self.assertEqual(first.companies, second.companies)
        self.assertEqual(first.futures, second.futures)
