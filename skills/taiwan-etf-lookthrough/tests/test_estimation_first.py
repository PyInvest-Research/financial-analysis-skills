"""Exercise estimation-first behavior with synthetic, private-free inputs."""

from dataclasses import replace
from datetime import date
from datetime import datetime
import json
from pathlib import Path
import sys
import struct
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lookthrough.dates import select_dates  # noqa: E402
from lookthrough.directory import CompanyDirectory  # noqa: E402
from lookthrough.loader import ColumnMap, load_official_file  # noqa: E402
from lookthrough.models import (  # noqa: E402
    EquityEvidence, Holding, Snapshot, snapshot_to_dict,
)
from lookthrough.portfolio import PortfolioError, parse_portfolio  # noqa: E402
from lookthrough.run import run_lookthrough  # noqa: E402
from lookthrough.validate import validate_snapshot  # noqa: E402
from lookthrough.sources import _NoRedirect, PYINVEST_BASE_URL  # noqa: E402


TODAY = date(2026, 10, 28)
ROOT = Path(__file__).resolve().parents[1]


def snapshot(**changes: object) -> Snapshot:
    """Create a synthetic dated fund with real directory security codes."""
    result = Snapshot(
        "1", "0050", "synthetic", False, TODAY,
        "2026-10-28T00:00:00+00:00", "https://example.com/holdings",
        "agent_official", (Holding("2330", "台積電", "stock", .95),
                           Holding("TWD", "現金", "cash", .05)))
    return replace(result, **changes)


class EstimationTests(unittest.TestCase):
    """Catch global rejection, silent omission and accidental rescaling."""

    def setUp(self) -> None:
        """Create isolated inputs and load the bundled directory offline."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = CompanyDirectory.from_file(
            ROOT / "scripts/lookthrough/data/company-directory.json")

    def run_case(self, positions: list[dict],
                 snapshots: tuple[Snapshot, ...] = (),
                 **options: object) -> dict:
        """Run acquisition, validation, calculation and serialization."""
        portfolio = self.root / "portfolio.json"
        portfolio.write_text(json.dumps(dict(
            currency="TWD", valuation_date=TODAY.isoformat(),
            positions=positions)))
        folder = self.root / "snapshots"
        folder.mkdir(exist_ok=True)
        for index, value in enumerate(snapshots):
            (folder / f"{index}.json").write_text(json.dumps(
                snapshot_to_dict(value)))
        paths = run_lookthrough(
            portfolio, self.root / "out", local_snapshot_dir=folder,
            today=TODAY, use_network=False, **options)
        self.report = paths["report.md"].read_text()
        return json.loads(paths["result.json"].read_text())

    def official(self, text: str, **options: object) -> Snapshot:
        """Load a synthetic official CSV through the public loader."""
        path = self.root / "official.csv"
        path.write_text(text)
        return load_official_file(
            path, ColumnMap("code", "name", "weight"),
            etf_symbol="0050", issuer="synthetic", is_active=False,
            holdings_date=TODAY, fetched_at="2026-10-28T00:00:00Z",
            source_url="https://example.com/holdings", **options)

    def test_future_date_is_annotated_without_limit(self) -> None:
        """Future observations stay dated and warned even beyond 3 weekdays."""
        value = snapshot(holdings_date=date(2027, 1, 4))
        result = self.run_case([
            dict(symbol="0050", kind="etf", market_value=100000)], (value,))
        self.assertEqual(result["totals"]["identified_stock_value"], 95000)
        self.assertEqual(result["positions"][0]["result_quality"], "annotated")
        self.assertEqual(result["snapshots"][0]["date_kind"], "forward_pcf")
        self.assertIn("非已實現持股", self.report)
        self.assertEqual(validate_snapshot(
            value, self.directory, TODAY).status, "fail")

    def test_identical_fetches_choose_latest_without_conflict(self) -> None:
        """Fetch metadata alone must not discard usable same-day holdings."""
        result = self.run_case([
            dict(symbol="0050", kind="etf", market_value=100000)], (
                snapshot(), snapshot(fetched_at="2026-10-28T03:00:00Z")))
        self.assertEqual(result["totals"]["identified_stock_value"], 95000)
        self.assertEqual(result["snapshots"][0]["fetched_at"],
                         "2026-10-28T03:00:00Z")
        self.assertFalse(any("Conflicting" in w for w in result["warnings"]))

    def test_conflicting_fetches_choose_latest_and_name_discard(self) -> None:
        """Changed same-day content remains usable with an audit warning."""
        result = self.run_case([
            dict(symbol="0050", kind="etf", market_value=100000)], (
                snapshot(), snapshot(fetched_at="2026-10-28T03:00:00Z",
                                     holdings=(Holding(
                                         "2330", "台積電", "stock", .8),))))
        self.assertEqual(result["totals"]["identified_stock_value"], 80000)
        self.assertIn("0.json", " ".join(result["warnings"]))

    def test_complete_denominator_with_strings_zero_and_foreign(self) -> None:
        """Normalization cannot discard a foreign position's market value."""
        result = self.run_case([
            dict(symbol="2330", kind="STOCK", market_value="100,000"),
            dict(symbol="VOO", kind="etf", market_value=100000),
            dict(symbol="2317", kind="stock", market_value=0)])
        self.assertEqual(result["totals"]["total_value"], 200000)
        self.assertEqual(result["totals"]["coverage"], .5)
        self.assertEqual(result["positions"][1]["kind"], "other_asset")
        self.assertEqual(result["skipped_rows"][0]["row"], 3)
        self.assertIn("零市值", self.report)

    def test_negative_and_nonfinite_portfolio_values_still_fail(self) -> None:
        """Annotated mode must not conceal corrupt financial input."""
        for value in (-1, True, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(PortfolioError):
                parse_portfolio(dict(
                    currency="TWD", valuation_date="2026-10-28",
                    positions=[dict(symbol="2330", kind="stock",
                                    market_value=value)]),
                    b"synthetic", policy="annotated")

    def test_loader_skips_table_artifacts_and_missing_marker(self) -> None:
        """Totals cannot inflate other NAV; dashes cannot become zero."""
        value = self.official(
            "code,name,weight\n2330,台積電,90%\ncode,name,weight\n"
            "股票,,\n2317,鴻海,-\n合計,,100%\n註：資料說明,,abc\n",
            policy="annotated")
        self.assertEqual([h.code for h in value.holdings], ["2330", "2317"])
        self.assertIsNone(value.holdings[1].weight)
        self.assertTrue(value.quality_notes)

    def test_bad_numeric_weight_invalidates_source(self) -> None:
        """NaN, Inf and out-of-range numbers cannot be skipped as footers."""
        for text in ("NaN", "Inf", "101%"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.official("code,name,weight\n2330,台積電,90%\n"
                              f"2317,鴻海,{text}\n", policy="annotated")

    def test_bad_data_row_limit(self) -> None:
        """More than 5% suspicious rows must fail a damaged table."""
        with self.assertRaises(ValueError):
            self.official("code,name,weight\n2330,台積電,90%\n"
                          "2317,鴻海,invalid\n", policy="annotated")

    def test_second_sheet_and_sixtieth_header_with_aliases(self) -> None:
        """Search actual holdings sheets rather than stopping at a cover."""
        workbook = Workbook()
        workbook.active.append(["cover"])
        sheet = workbook.create_sheet("holdings")
        for _ in range(59):
            sheet.append(["title"])
        sheet.append(["股票代號", "股票名稱", "比重"])
        sheet.append(["2330", "台積電", 95])
        path = self.root / "official.xlsx"
        workbook.save(path)
        value = load_official_file(
            path, ColumnMap(("code", "股票代號"), ("name", "股票名稱"),
                            ("weight", "比重")),
            etf_symbol="0050", issuer="synthetic", is_active=False,
            holdings_date=TODAY, fetched_at="2026-10-28T00:00:00Z",
            source_url="https://example.com/holdings", policy="annotated")
        self.assertEqual(value.holdings[0].weight, .95)
        self.assertIn("holdings", " ".join(value.quality_notes))

    def test_duplicate_missing_weight_does_not_use_partial_sum(self) -> None:
        """An unknown duplicate makes the merged security weight unknown."""
        value = snapshot(holdings=(
            Holding("2330", "台積電", "stock", .4),
            Holding("2330", "台積電", "stock", None),
            Holding("2317", "鴻海", "stock", .2)))
        result = self.run_case([
            dict(symbol="0050", kind="etf", market_value=100000)], (value,))
        self.assertEqual(result["totals"]["identified_stock_value"], 20000)
        self.assertEqual(result["totals"]["unclassified_gap_value"], 80000)
        self.assertEqual(validate_snapshot(
            value, self.directory, TODAY).status, "fail")

    def test_non_taiwan_source_keeps_buckets(self) -> None:
        """Bond-only inputs report known other NAV and unknown completeness."""
        result = self.run_case([
            dict(symbol="00679B", kind="etf", market_value=100000)], (
                snapshot(etf_symbol="00679B", holdings=(
                    Holding("BOND", "US Treasury", "other", .9),)),))
        self.assertEqual(result["totals"]["other_value"], 90000)
        self.assertEqual(result["positions"][0]["status"], "looked_through")
        self.assertIn("不代表確認零台股", self.report)

    def test_new_listing_is_separate_from_identified(self) -> None:
        """A missing directory code cannot boost verified coverage."""
        result = self.run_case([
            dict(symbol="0050", kind="etf", market_value=100000)], (
                snapshot(holdings=(Holding("9998", "Synthetic IPO", "stock",
                                           .8),)),))
        self.assertEqual(result["totals"]["identified_stock_value"], 0)
        self.assertEqual(result["totals"]["provisional_stock_value"], 80000)
        self.assertEqual(
            result["provisional_companies"][0]["directory_status"],
            "not_in_bundled_directory")

    def test_ratio_fallback_is_separate_and_in_headline(self) -> None:
        """A 58.29% ratio estimates 58,290 without company exposure."""
        result = self.run_case([
            dict(symbol="009828", kind="etf", market_value=100000)])
        self.assertEqual(result["positions"][0]["result_quality"],
                         "estimated_ratio")
        self.assertEqual(result["totals"]["estimated"][
            "estimated_taiwan_equity_value"], 58290)
        self.assertEqual(result["totals"]["identified_stock_value"], 0)
        self.assertEqual(
            result["totals"]["coverage_including_estimates"], .5829)
        self.assertEqual(result["totals"]["not_looked_through_value"], 100000)
        self.assertEqual(result["companies"], [])
        self.assertIn("58.29%", self.report.split("## 資料涵蓋")[0])

    def test_common_date_rollback_is_limited(self) -> None:
        """A laggard cannot force fresh funds onto much older holdings."""
        values = {"A": [date(2026, 10, 1), TODAY],
                  "B": [date(2026, 10, 1)]}
        selected = select_dates(values, max_rollback_weekdays=5)
        self.assertEqual(selected["A"], TODAY)
        self.assertEqual(select_dates(values)["A"], date(2026, 10, 1))

    def test_empty_official_source_is_unavailable(self) -> None:
        """An empty snapshot cannot count as successfully processed money."""
        result = self.run_case([
            dict(symbol="0050", kind="etf", market_value=100000)], (
                snapshot(holdings=()),))
        self.assertEqual(result["positions"][0]
                         ["status"], "not_looked_through")

    def test_bad_symbol_keeps_value_in_denominator(self) -> None:
        """A bad symbol or kind affects only its own positive-value row."""
        result = self.run_case([
            dict(symbol="2330", kind="stock", market_value=100000),
            dict(symbol=None, kind="invalid", market_value=100000)])
        self.assertEqual(result["totals"]["coverage"], .5)
        self.assertEqual(result["totals"]["not_looked_through_value"], 100000)

    def test_numeric_overflow_cannot_hide_among_valid_rows(self) -> None:
        """Numeric overflow must reject a source even below skip thresholds."""
        with self.assertRaises(ValueError):
            self.official("code,name,weight\n" + "2330,TSMC,1%\n" * 25
                          + "2317,Hon Hai,1e309\n", policy="annotated")

    def test_strict_loader_preserves_numbered_error(self) -> None:
        """Strict callers retain row-numbered nonfinite diagnostics."""
        with self.assertRaisesRegex(ValueError, "Invalid row 2"):
            self.official("code,name,weight\n2330,TSMC,NaN\n")

    def test_current_remote_precedes_stale_local(self) -> None:
        """Stale local availability must not prevent trying current JSON."""
        def public(url: str) -> bytes:
            """Serve the exact index and requested dated payload only."""
            if url.endswith("/index.json"):
                return json.dumps(dict(schema_version="1", etfs=[
                    dict(symbol="0050", dates=["2026-10-28"])])).encode()
            self.assertTrue(url.endswith("/0050/2026-10-28.json"))
            return json.dumps(snapshot_to_dict(snapshot())).encode()

        portfolio = self.root / "portfolio.json"
        portfolio.write_text(json.dumps(dict(
            currency="TWD", valuation_date=TODAY.isoformat(), positions=[
                dict(symbol="0050", kind="etf", market_value=100000)])))
        folder = self.root / "local"
        folder.mkdir()
        (folder / "old.json").write_text(json.dumps(snapshot_to_dict(snapshot(
            holdings_date=date(2026, 9, 1), holdings=(
                Holding("2330", "台積電", "stock", .5),)))))
        paths = run_lookthrough(portfolio, self.root / "remote-out",
                                local_snapshot_dir=folder, today=TODAY,
                                opener=public)
        result = json.loads(paths["result.json"].read_text())
        self.assertEqual(result["totals"]["identified_stock_value"], 95000)
        self.assertIn("TLS 信任庫未核對", " ".join(result["warnings"]))

    def test_default_clock_uses_taipei_date(self) -> None:
        """UTC evening must use the next calendar day in Taipei."""
        class Clock(datetime):
            """Freeze an instant that is the next day only in Taipei."""

            @classmethod
            def now(cls, tz=None) -> datetime:
                """Convert the frozen instant into the requested timezone."""
                from datetime import timezone
                instant = datetime(2026, 10, 27, 18, tzinfo=timezone.utc)
                return instant.astimezone(tz)

        portfolio = self.root / "portfolio.json"
        portfolio.write_text(json.dumps(dict(
            currency="TWD", valuation_date=TODAY.isoformat(), positions=[
                dict(symbol="0050", kind="etf", market_value=100000)])))
        folder = self.root / "local"
        folder.mkdir()
        (folder / "current.json").write_text(json.dumps(
            snapshot_to_dict(snapshot())))
        with patch("lookthrough.run.datetime", Clock):
            paths = run_lookthrough(portfolio, self.root / "clock-out",
                                    local_snapshot_dir=folder,
                                    use_network=False)
        result = json.loads(paths["result.json"].read_text())
        self.assertEqual(result["snapshots"][0]["date_kind"], "observed")

    def test_html_xls_is_data_only(self) -> None:
        """A table in a disguised XLS is readable without a browser."""
        path = self.root / "official.xls"
        path.write_text("<html><table><tr><th>code</th><th>name</th>"
                        "<th>weight</th></tr><tr><td>2330</td>"
                        "<td>TSMC</td><td>95%</td></tr></table></html>")
        value = load_official_file(
            path, ColumnMap("code", "name", "weight"), etf_symbol="0050",
            issuer="synthetic", is_active=False, holdings_date=TODAY,
            fetched_at="2026-10-28T00:00:00Z",
            source_url="https://example.com/holdings", policy="annotated")
        self.assertEqual(value.holdings[0].weight, .95)

    def test_public_json_rejects_same_host_redirects(self) -> None:
        """The relaxed CA-audit route still sends no redirected request."""
        with self.assertRaises(ValueError):
            _NoRedirect().redirect_request(
                Request(PYINVEST_BASE_URL + "/index.json"), None,
                302, "Found", {}, PYINVEST_BASE_URL + "/0050/latest.json")

    def test_biff_xls_reads_real_binary_records(self) -> None:
        """Read a minimal BIFF8 workbook, not an HTML stand-in."""
        def record(kind: int, data: bytes = b"") -> bytes:
            """Encode one documented BIFF record with little-endian length."""
            return struct.pack("<HH", kind, len(data)) + data

        def bof(kind: int) -> bytes:
            """Start a workbook or worksheet substream."""
            return record(0x0809, struct.pack(
                "<HHHHII", 0x600, kind, 0xDBB, 1996, 0, 6))

        name = b"Holdings"
        offset = len(bof(5)) + 12 + len(name) + 4
        bound = record(0x0085, struct.pack(
            "<IBBBB", offset, 0, 0, len(name), 0) + name)
        raw = bof(5) + bound + record(0x000A) + bof(0x10)
        raw += record(0x0200, struct.pack("<IIHHH", 0, 2, 0, 3, 0))
        for r, row in enumerate((("code", "name", "weight"),
                                 ("2330", "TSMC", "95%"))):
            for c, value in enumerate(row):
                raw += record(0x0204, struct.pack(
                    "<HHHHB", r, c, 0, len(value), 0) + value.encode())
        path = self.root / "binary.xls"
        path.write_bytes(raw + record(0x000A))
        value = load_official_file(
            path, ColumnMap("code", "name", "weight"), etf_symbol="0050",
            issuer="synthetic", is_active=False, holdings_date=TODAY,
            fetched_at="2026-10-28T00:00:00Z",
            source_url="https://example.com/holdings", policy="annotated")
        self.assertEqual(value.holdings[0].code, "2330")
        self.assertEqual(value.holdings[0].weight, .95)

    def test_skipping_exactly_five_percent_is_allowed(self) -> None:
        """One skipped row among twenty remains annotated, not discarded."""
        value = self.official("code,name,weight\n" + "2330,TSMC,1%\n" * 19
                              + "2317,Hon Hai,unknown\n", policy="annotated")
        self.assertEqual(len(value.holdings), 19)
        self.assertIn("已略過", " ".join(value.quality_notes))

    def test_duplicate_known_weights_merge_without_rescaling(self) -> None:
        """Equal names merge; different names keep separate rows."""
        result = self.run_case([
            dict(symbol="0050", kind="etf", market_value=100000)], (
                snapshot(holdings=(Holding("2330", "台積電", "stock", .4),
                                   Holding("2330", "台積電", "stock", .2),
                                   Holding("2330", "Wrong", "stock", .1))),))
        self.assertAlmostEqual(
            result["totals"]["identified_stock_value"], 70000)
        self.assertEqual(result["totals"]["unresolved_value"], 0)
        self.assertAlmostEqual(
            result["totals"]["unclassified_gap_value"], 30000)
        self.assertEqual(len(result["companies"][0]["sources"]), 2)
        self.assertIn("同代號不同名稱", self.report)

    def test_zero_ratio_does_not_confirm_zero_taiwan(self) -> None:
        """Rounded monthly 0% cannot establish complete zero Taiwan stock."""
        result = self.run_case([
            dict(symbol="00402A", kind="etf", market_value=100000)])
        self.assertEqual(result["positions"][0]["result_quality"],
                         "estimated_ratio")
        self.assertEqual(result["totals"]["estimated"][
            "estimated_taiwan_equity_value"], 0)
        self.assertIn("不代表確認零台股", self.report)

    def test_strict_ratio_is_still_reference_only(self) -> None:
        """Strict mode keeps its prior fields and full unavailable value."""
        result = self.run_case([
            dict(symbol="009828", kind="etf", market_value=100000)],
            validation_policy="strict")
        self.assertNotIn("estimated", result["totals"])
        self.assertNotIn("result_quality", result["positions"][0])
        self.assertEqual(result["totals"]["not_looked_through_value"], 100000)

    def test_strict_duplicate_fetches_remain_conflicts(self) -> None:
        """Strict mode does not apply annotated same-day deduplication."""
        result = self.run_case([
            dict(symbol="0050", kind="etf", market_value=100000)], (
                snapshot(), snapshot(fetched_at="2026-10-28T03:00:00Z")),
            validation_policy="strict")
        self.assertEqual(result["positions"][0]
                         ["status"], "not_looked_through")
        self.assertIn("Conflicting", result["positions"][0]["reason"])

    def test_malformed_snapshot_falls_back_to_monthly_ratio(self) -> None:
        """A failed source must still permit the final estimation tier."""
        result = self.run_case([
            dict(symbol="009828", kind="etf", market_value=100000)], (
                snapshot(etf_symbol="009828", holdings=(
                    Holding("2330", "台積電", "stock", 10),)),))
        self.assertEqual(result["positions"][0]["result_quality"],
                         "estimated_ratio")
        self.assertEqual(result["totals"]["estimated"][
            "estimated_taiwan_equity_value"], 58290)

    def test_cash_value_is_in_denominator_and_own_bucket(self) -> None:
        """Explicit cash is neither missing money nor equity exposure."""
        result = self.run_case([
            dict(symbol="TWD", kind="cash", market_value=100000),
            dict(symbol="2330", kind="stock", market_value=100000)])
        self.assertEqual(result["totals"]["cash_value"], 100000)
        self.assertEqual(result["totals"]["coverage"], .5)

    def test_bad_kind_does_not_hide_out_of_range_weight(self) -> None:
        """An invalid kind must not conceal an out-of-range number."""
        path = self.root / "bad-kind.csv"
        path.write_text("code,name,weight,kind\n" +
                        "2330,TSMC,1%,stock\n" * 20 +
                        "2317,Hon Hai,101%,typo\n")
        with self.assertRaises(ValueError):
            load_official_file(
                path, ColumnMap("code", "name", "weight", "kind"),
                etf_symbol="0050", issuer="synthetic", is_active=False,
                holdings_date=TODAY, fetched_at="2026-10-28T00:00:00Z",
                source_url="https://example.com/holdings", policy="annotated")

    def test_incomplete_cash_source_keeps_taiwan_unknown(self) -> None:
        """No Taiwan equity rows do not establish a zero allocation."""
        value = snapshot(
            schema_version="2", holdings=(Holding(
                "TWD", "現金", "cash", .2),),
            equity_evidence=EquityEvidence(False, None, None, 0,
                                           None, ("a" * 64,)))
        result = self.run_case([
            dict(symbol="0050", kind="etf", market_value=100000)], (value,))
        self.assertIsNone(result["taiwan_equity"]["per_etf"][0]["share"])
        self.assertIsNone(result["taiwan_equity"]
                          ["confirmed_allocation_share"])
        self.assertEqual(result["totals"]["cash_value"], 20000)
        self.assertIn("未知", self.report)

    def test_partial_zero_row_keeps_allocation_unknown(self) -> None:
        """A rounded zero row is still not a complete market inventory."""
        value = snapshot(
            schema_version="2", holdings=(Holding(
                "2330", "台積電", "stock", 0, market="TWSE"),),
            equity_evidence=EquityEvidence(False, 100, 0, 1,
                                           None, ("a" * 64,)))
        result = self.run_case([
            dict(symbol="0050", kind="etf", market_value=100000)], (value,))
        self.assertIsNone(result["taiwan_equity"]["per_etf"][0]["share"])

    def test_reviewed_issuer_ratio_is_not_limited_to_sitca(self) -> None:
        """The existing historical references include official issuer data."""
        result = self.run_case([
            dict(symbol="00690", kind="etf", market_value=100000)])
        self.assertEqual(result["positions"][0]["result_quality"],
                         "estimated_ratio")
        self.assertAlmostEqual(result["totals"]["estimated"][
            "estimated_taiwan_equity_value"], 98193.69248229572)
