"""Exercise snapshot validation rules with synthetic holdings only."""

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import unittest

from support import make_snapshot
from lookthrough.directory import CompanyDirectory
from lookthrough.models import Holding, Snapshot
from lookthrough.validate import ValidationResult, validate_snapshot


TODAY = date(2026, 10, 25)


def snapshot_with_stock_weights(weights: list[float]) -> Snapshot:
    """Replace fixture stocks with distinct synthetic security codes."""
    base = make_snapshot()
    codes = ["2330", "2317", "2454"]
    return replace(base, holdings=tuple(
        Holding(codes[i] if i < 3 else str(8000 + i), "Stock", "stock", w)
        for i, w in enumerate(weights)
    ))


def failed_names(result: ValidationResult) -> set[str]:
    """Return checks that failed for concise assertions."""
    return {c.name for c in result.checks if c.status == "fail"}


class ValidateTests(unittest.TestCase):
    """Check thresholds, invalid inputs and instrument distinctions."""

    @classmethod
    def setUpClass(cls) -> None:
        """Load the bundled directory without a network request."""
        cls.d = CompanyDirectory.from_file(
            Path(__file__).resolve().parents[1]
            / "scripts/lookthrough/data/company-directory.json"
        )

    def check_status(self, snapshot: Snapshot, name: str) -> str:
        """Return one named validation check's status."""
        result = validate_snapshot(snapshot, self.d, TODAY)
        return next(c.status for c in result.checks if c.name == name)

    def test_weights_in_range_fail(self) -> None:
        """Reject invalid NAV values across instrument kinds."""
        for kind in ("stock", "futures", "cash", "other"):
            for weight in (-1.01, 1.01, float("nan"), float("inf"),
                           -float("inf"), "0.5", True, False):
                with self.subTest(kind=kind, weight=weight):
                    s = replace(make_snapshot(), holdings=(
                        Holding("2330", "Stock", kind, weight),))
                    self.assertEqual(
                        self.check_status(s, "weights_in_range"), "fail")

    def test_cash_other_signed_nav_pass(self) -> None:
        """Accept signed NAV endpoints without filling missing weights."""
        for kind in ("cash", "other"):
            for weight in (-1, -0.01, 0, 1, None):
                with self.subTest(kind=kind, weight=weight):
                    base = make_snapshot()
                    s = replace(base, holdings=base.holdings + (
                        Holding("NONSTOCK", "Nonstock", kind, weight),))
                    self.assertEqual(
                        validate_snapshot(s, self.d, TODAY).status, "pass")
                    self.assertEqual(s.holdings[-1].weight, weight)

    def test_stock_futures_nav_remains_unsigned(self) -> None:
        """Reject negative NAV while keeping zero and one valid in range."""
        for kind in ("stock", "futures"):
            for weight, expected in ((-0.01, "fail"), (0, "pass"),
                                     (1, "pass")):
                with self.subTest(kind=kind, weight=weight):
                    s = replace(make_snapshot(), holdings=(
                        Holding("2330", "Stock", kind, weight),))
                    self.assertEqual(
                        self.check_status(s, "weights_in_range"), expected)

    def test_stock_weight_sum_fail(self) -> None:
        """Reject too small or too large totals, including empty stocks."""
        for weights in ([], [0.49], [0.6, 0.406]):
            with self.subTest(weights=weights):
                s = snapshot_with_stock_weights(weights)
                self.assertEqual(
                    self.check_status(s, "stock_weight_sum"), "fail")

    def test_weight_sum_over_one_fails(self) -> None:
        """Detect percentages incorrectly stored as fractions."""
        s = snapshot_with_stock_weights([60.0, 20.0, 15.0])
        r = validate_snapshot(s, self.d, TODAY)
        self.assertEqual(r.status, "fail")
        self.assertIn("stock_weight_sum", failed_names(r))

    def test_stock_weight_sum_invalid_fail(self) -> None:
        """Missing, malformed and nonfinite stock weights cannot pass."""
        for weight in (None, "0.6", True, float("nan"), float("inf"),
                       -float("inf")):
            with self.subTest(weight=weight):
                s = snapshot_with_stock_weights([0.95, weight])
                self.assertEqual(
                    self.check_status(s, "stock_weight_sum"), "fail")
                self.assertEqual(
                    self.check_status(s, "weights_in_range"), "fail")

    def test_stock_weight_sum_warn(self) -> None:
        """Warn below 0.9, while the minimum bound itself is allowed."""
        for weight in (0.5, 0.8999):
            self.assertEqual(self.check_status(
                snapshot_with_stock_weights([weight]), "stock_weight_sum"),
                "warn")

    def test_stock_weight_sum_pass(self) -> None:
        """Accept both exact inclusive upper and warning bounds."""
        for weights in ([0.9], [0.6, 0.405]):
            self.assertEqual(self.check_status(
                snapshot_with_stock_weights(weights), "stock_weight_sum"),
                "pass")

    def test_not_top_n_only_fail(self) -> None:
        """Detect ten disclosed stocks totaling only 0.55."""
        self.assertEqual(self.check_status(
            snapshot_with_stock_weights([0.055] * 10), "not_top_n_only"),
            "fail")

    def test_not_top_n_only_pass(self) -> None:
        """Eleven stocks or a 0.9 total clear the top-ten rule."""
        for weights in ([0.05] * 11, [0.9]):
            self.assertEqual(self.check_status(
                snapshot_with_stock_weights(weights), "not_top_n_only"),
                "pass")

    def test_no_duplicate_security_fail(self) -> None:
        """Reject repeated stock codes regardless of their names."""
        s = make_snapshot()
        s = replace(s, holdings=s.holdings + (
            Holding("2330", "Other Name", "stock", 0.01),))
        self.assertEqual(
            self.check_status(s, "no_duplicate_security"), "fail")

    def test_holdings_date_sane_fail(self) -> None:
        """Reject tomorrow and dates older than thirty calendar days."""
        for offset in (1, -31):
            s = replace(make_snapshot(),
                        holdings_date=TODAY + timedelta(days=offset))
            self.assertEqual(
                self.check_status(s, "holdings_date_sane"), "fail")

    def test_holdings_date_sane_pass(self) -> None:
        """Accept today and exactly thirty days ago."""
        for offset in (0, -30):
            s = replace(make_snapshot(),
                        holdings_date=TODAY + timedelta(days=offset))
            self.assertEqual(
                self.check_status(s, "holdings_date_sane"), "pass")

    def test_resolvable_securities_warn(self) -> None:
        """An ETF holding and unknown company contribute unresolved weight."""
        for code in ("0050", "999999"):
            s = make_snapshot()
            s = replace(s, holdings=s.holdings + (
                Holding(code, "Unknown", "stock", 0.011),))
            self.assertEqual(
                self.check_status(s, "resolvable_securities"), "warn")
            self.assertEqual(validate_snapshot(
                s, self.d, TODAY).status, "warn")

    def test_resolvable_securities_pass(self) -> None:
        """Unresolved weights aggregate, with exactly 0.01 allowed."""
        for weights, expected in (([0.01], "pass"), ([0.006] * 2, "warn")):
            s = make_snapshot()
            unknown = tuple(Holding(str(900000 + i), "Unknown", "stock", w)
                            for i, w in enumerate(weights))
            s = replace(s, holdings=s.holdings + unknown)
            self.assertEqual(
                self.check_status(s, "resolvable_securities"), expected)

    def test_required_fields_fail(self) -> None:
        """Reject empty and whitespace-only identifiers and URLs."""
        for field in ("source_url", "etf_symbol"):
            for value in ("", "   "):
                s = replace(make_snapshot(), **{field: value})
                self.assertEqual(
                    self.check_status(s, "required_fields"), "fail")

    def test_clean_snapshot_passes(self) -> None:
        """The clean fixture returns seven passing checks."""
        r = validate_snapshot(make_snapshot(), self.d, TODAY)
        self.assertEqual(r.status, "pass")
        self.assertEqual(len(r.checks), 7)
        self.assertTrue(all(c.detail and c.status == "pass" for c in r.checks))

    def test_futures_notional_pass(self) -> None:
        """Signed futures notional is not constrained by NAV weight bounds."""
        for exposure in (1.5, -1.5, None):
            s = make_snapshot()
            s = replace(s, holdings=s.holdings + (
                Holding("TXF", "Futures", "futures", None, exposure),))
            self.assertEqual(validate_snapshot(
                s, self.d, TODAY).status, "pass")
