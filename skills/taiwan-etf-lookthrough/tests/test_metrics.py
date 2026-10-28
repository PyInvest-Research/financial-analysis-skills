"""Check concentration and overlap with saved synthetic hand calculations."""

from dataclasses import replace
import json
from pathlib import Path
import unittest

import support  # noqa: F401
from lookthrough.calculate import compute_lookthrough
from lookthrough.directory import Company, CompanyDirectory
from lookthrough.metrics import compute_metrics, pairwise_overlap
from lookthrough.models import Holding, snapshot_from_dict
from lookthrough.portfolio import load_portfolio
from lookthrough.validate import ValidationResult


FIXTURES = Path(__file__).parent / "fixtures/handcalc"


class MetricsTests(unittest.TestCase):
    """Keep full portfolio denominators and merge issuer weights first."""

    def setUp(self) -> None:
        """Load C1 without network access or invented holding values."""
        self.directory = CompanyDirectory.from_file(
            FIXTURES / "directory.json")
        self.portfolio = load_portfolio(FIXTURES / "c1/portfolio.json")
        payload = json.loads((FIXTURES / "c1/snapshots.json").read_text())
        self.snapshots = {key: snapshot_from_dict(value)
                          for key, value in payload.items()}
        self.result = compute_lookthrough(
            self.portfolio, self.snapshots,
            {key: ValidationResult("pass", ()) for key in self.snapshots},
            {}, self.directory, {})

    def test_c1_metrics(self) -> None:
        """Reproduce C1 industry amounts, top shares and effective count."""
        metrics = compute_metrics(self.result, self.directory)
        self.assertAlmostEqual(metrics.top5_share, .98)
        self.assertAlmostEqual(metrics.top10_share, .98)
        self.assertAlmostEqual(metrics.effective_holdings, 960400 / 440400)
        self.assertEqual(metrics.overlaps, (("ETFA", "ETFB", .5),))
        self.assertEqual([(row.industry_code, row.amount)
                          for row in metrics.industries],
                         [("24", 700000), ("17", 200000), ("31", 80000)])
        self.assertEqual([row.share for row in metrics.industries],
                         [.7, .2, .08])

    def test_effective_holdings_none_when_nothing_identified(self) -> None:
        """Return no effective count when all capital is unrecognized."""
        empty = replace(self.result, companies=(), identified_stock_value=0)
        metrics = compute_metrics(empty, self.directory)
        self.assertIsNone(metrics.effective_holdings)
        self.assertEqual(metrics.top5_share, 0)
        self.assertEqual(metrics.industries, ())

    def test_empty_zero_total(self) -> None:
        """Avoid division by zero for an empty result."""
        empty = replace(self.result, total_value=0, companies=(), positions=())
        metrics = compute_metrics(empty, self.directory)
        self.assertEqual(metrics.top10_share, 0)
        self.assertIsNone(metrics.effective_holdings)
        self.assertEqual(metrics.overlaps, ())

    def test_preferred_merge_before_minimum(self) -> None:
        """Consolidate preferred and common weights before pairwise min."""
        first = replace(self.snapshots["ETFA"], holdings=(
            Holding("2881", "富邦金", "stock", .2),
            Holding("2881A", "富邦金特", "stock", .1),
            Holding("9999", "Unknown", "stock", .7)))
        second = replace(first, etf_symbol="ETFB", holdings=(
            Holding("2881", "富邦金", "stock", .25),
            Holding("9999", "Unknown", "stock", .75)))
        self.assertAlmostEqual(
            pairwise_overlap(first, second, self.directory), .25)
        self.assertAlmostEqual(
            pairwise_overlap(first, first, self.directory), .3)

    def test_overlap_ignores_nonstock_and_unverified_preferred(self) -> None:
        """Exclude unknowns, cash, futures and unverified preferred names."""
        snapshot = replace(self.snapshots["ETFA"], holdings=(
            Holding("2881A", "Unverified", "stock", .1),
            Holding("0056", "ETF", "stock", .2),
            Holding("2330", "Cash", "cash", .3),
            Holding("2330", "Future", "futures", None, .4)))
        self.assertEqual(pairwise_overlap(snapshot, snapshot, self.directory),
                         0)

    def test_only_processed_etfs_sorted_lexicographically(self) -> None:
        """Skip direct and failed positions even with snapshot metadata."""
        positions = self.result.positions
        ignored = replace(positions[0], symbol="ETFX",
                          status="not_looked_through")
        direct = replace(positions[0], symbol="2330", kind="stock",
                         status="direct")
        result = replace(self.result,
                         positions=(positions[1], ignored, direct,
                                    positions[0]))
        self.assertEqual(compute_metrics(result, self.directory).overlaps,
                         (("ETFA", "ETFB", .5),))

    def test_missing_industry_names_group_as_unknown(self) -> None:
        """Preserve source code 91 in directory but report unknown sector."""
        directory = CompanyDirectory((
            Company("2330", "台積電", "TWSE", "91", ""),
            Company("2881", "富邦金", "TWSE", "17", "   "),
        ), "2026-10-25")
        metrics = compute_metrics(self.result, directory)
        self.assertEqual(len(metrics.industries), 1)
        industry = metrics.industries[0]
        self.assertEqual((industry.industry_code, industry.industry_name),
                         ("unknown", "未對應"))
        self.assertAlmostEqual(industry.amount, 980000)
        self.assertAlmostEqual(industry.share, .98)

    def test_top_counts_sort_amounts_without_rescaling(self) -> None:
        """Select the largest five/ten regardless of input ordering."""
        company = self.result.companies[0]
        companies = tuple(replace(company, issuer_code=str(1000 + i),
                                  amount=i, share=i / 100)
                          for i in range(1, 13))
        result = replace(self.result, companies=companies, total_value=100)
        metrics = compute_metrics(result, self.directory)
        self.assertAlmostEqual(metrics.top5_share, .5)
        self.assertAlmostEqual(metrics.top10_share, .75)

    def test_effective_count_handles_extreme_scales(self) -> None:
        """Avoid squaring overflow and underflow through internal scaling."""
        for amount in (1e200, 1e-200):
            with self.subTest(amount=amount):
                companies = tuple(replace(row, amount=amount)
                                  for row in self.result.companies[:2])
                result = replace(self.result, companies=companies,
                                 total_value=amount * 2)
                self.assertAlmostEqual(
                    compute_metrics(result, self.directory).effective_holdings,
                    2)

    def test_zero_identified_amounts_have_no_effective_count(self) -> None:
        """Handle zero-weight issuer rows without a division by zero."""
        result = replace(self.result, companies=tuple(
            replace(row, amount=0) for row in self.result.companies))
        self.assertIsNone(compute_metrics(result, self.directory)
                          .effective_holdings)

    def test_missing_stock_weight_is_rejected(self) -> None:
        """Reject missing stock weights rather than silently zero-filling."""
        snapshot = replace(self.snapshots["ETFA"], holdings=(
            Holding("2330", "台積電", "stock", None),))
        with self.assertRaisesRegex(ValueError, "Missing stock weight"):
            pairwise_overlap(snapshot, snapshot, self.directory)
