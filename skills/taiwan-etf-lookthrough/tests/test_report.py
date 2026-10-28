"""Check reproducible offline reports retain calculations and provenance."""

import csv
from dataclasses import replace
from datetime import date
import io
import json
from math import fsum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest

import support  # noqa: F401
from lookthrough import __version__
from lookthrough.calculate import compute_lookthrough
from lookthrough.dates import classify_dates
from lookthrough.directory import CompanyDirectory
from lookthrough.metrics import compute_metrics
from lookthrough.models import snapshot_from_dict, snapshot_sha256
from lookthrough.portfolio import load_portfolio
from lookthrough.report import (
    ACTIVE_NOTE, EFFECTIVE_N_NOTE, ESTIMATE_NOTE, FUTURES_NOTE, NOT_ADVICE,
    OVERLAP_DISCLAIMER, build_result_json, render_report_md, write_outputs,
)
from lookthrough.validate import validate_snapshot


FIXTURES = Path(__file__).parent / "fixtures/handcalc"
STAMP = "2026-10-25T00:00:00Z"
LIMITS = (OVERLAP_DISCLAIMER, EFFECTIVE_N_NOTE, ESTIMATE_NOTE,
          ACTIVE_NOTE, FUTURES_NOTE, NOT_ADVICE)


class ReportTests(unittest.TestCase):
    """Catch omitted gaps, provenance, date warnings and unstable output."""

    def setUp(self) -> None:
        """Load the saved synthetic C1 case without using the network."""
        self.directory = CompanyDirectory.from_file(
            FIXTURES / "directory.json")
        self.load_case(1)

    def load_case(self, number: int) -> None:
        """Load one immutable hand-calculation case for real computation."""
        folder = FIXTURES / f"c{number}"
        self.portfolio = load_portfolio(folder / "portfolio.json")
        payload = json.loads((folder / "snapshots.json").read_text())
        self.snapshots = {
            symbol: snapshot_from_dict(row) if row else None
            for symbol, row in payload.items()
        }

    def build(self, stamp: str = STAMP) -> dict[str, Any]:
        """Run actual validation, dates, arithmetic and metrics offline."""
        snapshots = {k: v for k, v in self.snapshots.items() if v is not None}
        checks = {k: validate_snapshot(v, self.directory, date(2026, 10, 25))
                  for k, v in snapshots.items()}
        statuses = classify_dates(
            {k: v.holdings_date for k, v in snapshots.items()},
            {k: v.is_active for k, v in snapshots.items()})
        result = compute_lookthrough(
            self.portfolio, self.snapshots, checks, statuses, self.directory,
            {"ETFX": "範圍外，未取得官方檔"})
        return build_result_json(
            self.portfolio, result, compute_metrics(result, self.directory),
            stamp)

    def test_fixed_section_order_dates_and_all_six_limits(self) -> None:
        """Keep mandatory disclosures even without active holdings."""
        payload = self.build()
        md = render_report_md(payload)
        self.assertEqual([line for line in md.splitlines()
                          if line.startswith("#")], [
            "# ETF 持股穿透報告", "## 摘要", "## 資料涵蓋", "## 各檔持股日與來源",
            "## 公司曝險前 10 大", "## 產業分布", "## 集中度與重疊", "## 限制與說明"])
        self.assertEqual(payload["limitations"], list(LIMITS))
        for sentence in LIMITS:
            self.assertIn(sentence, md)
        self.assertIn("2026-10-22", md)
        self.assertIn("國定假日", md)
        for label in ("總市值", "已辨識股票", "未辨識", "現金", "其他", "未分類差額", "未穿透"):
            self.assertIn(f"| {label} |", md)
        self.assertIn("980,000.00 | 98.00%", md)

    def test_modern_cathay_note_follows_selected_source_only(self) -> None:
        """Disclose derived codes and unknown NAV only for modern sources."""
        original = self.snapshots["ETFA"]
        self.snapshots["ETFA"] = replace(
            original, issuer="cathay", source_kind="agent_official",
            source_url=("https://www.cathaysite.com.tw/"
                        "fund-details/ECN?tab=portfolio"))
        payload = self.build()
        note = payload["snapshots"][0]["source_note"]
        self.assertIn("不是現代頁原生代碼", note)
        self.assertIn("未知", note)
        self.assertIn(note, render_report_md(payload))
        self.snapshots["ETFA"] = replace(
            self.snapshots["ETFA"], source_url=(
                "https://www.cathaysite.com.tw/ETF/detail/ECN?tab=etf3"))
        self.assertNotIn("source_note", self.build()["snapshots"][0])
        self.snapshots["ETFA"] = original

    def test_json_identity_metrics_and_source_contributions(self) -> None:
        """Expose real model metadata and never substitute invented hashes."""
        payload = self.build()
        self.assertEqual(payload["schema_version"], "1")
        self.assertEqual(payload["code_version"], __version__)
        self.assertEqual(payload["input_sha256"], self.portfolio.input_sha256)
        self.assertEqual(payload["valuation_date"], "2026-10-22")
        self.assertEqual(payload["generated_at"], STAMP)
        snapshot = payload["snapshots"][0]
        self.assertEqual(snapshot["symbol"], "ETFA")
        self.assertEqual(snapshot["snapshot_sha256"],
                         snapshot_sha256(self.snapshots["ETFA"]))
        self.assertEqual(snapshot["validation_status"], "pass")
        self.assertEqual(snapshot["date_status"], "common")
        self.assertEqual(snapshot["source_kind"], "official")
        self.assertEqual(snapshot["source_url"],
                         "https://example.com/synthetic/ETFA")
        self.assertEqual(snapshot["fetched_at"],
                         "2026-10-23T00:00:00+00:00")
        self.assertAlmostEqual(payload["metrics"]["effective_holdings"],
                               960400 / 440400)
        self.assertEqual(payload["companies"][0]["amount_twd"], 640000)
        self.assertEqual(len(payload["companies"][0]["sources"]), 3)
        self.assertTrue(payload["positions"][0]["validation"]["checks"])

    def test_stale_and_active_dates_keep_values(self) -> None:
        """Report passive stale and active mismatch as different statuses."""
        self.load_case(4)
        snapshot = self.snapshots["ETFB"]
        for day, active, status in ((14, False, "stale"),
                                    (21, True, "active_mismatch")):
            with self.subTest(status=status):
                self.snapshots["ETFB"] = replace(
                    snapshot, holdings_date=date(2026, 10, day),
                    is_active=active)
                payload = self.build()
                md = render_report_md(payload)
                self.assertEqual(payload["snapshots"][1]["date_status"],
                                 status)
                self.assertEqual(payload["totals"]["identified_stock_value"],
                                 980000)
                self.assertIn(
                    f"以 2026-10-22 的部位市值，套用 2026-10-{day} 揭露的持股權重估計",
                    md)
                self.assertIn(status, md)
                if active:
                    self.assertIn("主動式", md)
                    self.assertIn(ACTIVE_NOTE, md)
                else:
                    summary = md.split("## 摘要\n\n", 1)[1]
                    self.assertTrue(summary.startswith("⚠ 持股資料過期：ETFB"))

    def test_agent_official_label_and_warning_survive(self) -> None:
        """Show nonbuiltin origin in JSON and human-readable source rows."""
        self.snapshots["ETFA"] = replace(
            self.snapshots["ETFA"], source_kind="agent_official")
        payload = self.build()
        self.assertEqual(payload["snapshots"][0]["source_kind"],
                         "agent_official")
        self.assertIn("非內建來源，由 agent 取得", render_report_md(payload))
        self.load_case(5)
        self.assertTrue(any("合併" in text
                            for text in self.build()["warnings"]))

    def test_unavailable_position_keeps_denominator_and_reason(self) -> None:
        """Keep a missing ETF visibly unavailable with no fabricated date."""
        self.load_case(3)
        payload = self.build()
        totals = payload["totals"]
        self.assertEqual(totals["total_value"], 1100000)
        self.assertEqual(totals["not_looked_through_value"], 100000)
        self.assertAlmostEqual(totals["coverage"], 980000 / 1100000)
        position = next(p for p in payload["positions"]
                        if p["symbol"] == "ETFX")
        self.assertEqual(position["reason"], "範圍外，未取得官方檔")
        self.assertIsNone(position["snapshot"])
        missing = next(s for s in payload["snapshots"]
                       if s["symbol"] == "ETFX")
        self.assertIsNone(missing["holdings_date"])
        self.assertIsNone(missing["snapshot_sha256"])
        md = render_report_md(payload)
        self.assertIn("範圍外，未取得官方檔", md)
        self.assertIn("100,000.00 | 9.09%", md)

    def test_unknown_futures_and_negative_gap_remain_distinct(self) -> None:
        """Never print unknown futures as zero or add notionals to NAV."""
        self.load_case(6)
        payload = self.build()
        self.assertEqual(payload["totals"]["futures"], [
            {"security_code": "TXFK6", "amount_twd": None}])
        self.assertEqual(payload["totals"]["unclassified_gap_value"], -4000)
        md = render_report_md(payload)
        self.assertIn("| TXFK6 | 未知 |", md)
        self.assertIn("-4,000.00 | -0.40%", md)
        self.assertIn("期貨額外曝險（不計入總市值分桶）", md)

    def test_all_six_limits_also_appear_without_futures(self) -> None:
        """Keep universal fixed notes even if no future/active row exists."""
        for symbol, snapshot in self.snapshots.items():
            self.snapshots[symbol] = replace(snapshot, holdings=tuple(
                row for row in snapshot.holdings if row.kind != "futures"))
        payload = self.build()
        self.assertEqual(payload["totals"]["futures"], [])
        for sentence in LIMITS:
            self.assertIn(sentence, render_report_md(payload))

    def test_csv_reconciles_company_amounts_with_preferred(self) -> None:
        """Retain every source security and BOM without losing precision."""
        self.load_case(2)
        payload = self.build()
        with TemporaryDirectory() as folder:
            paths = write_outputs(payload, Path(folder))
            raw = paths["exposures.csv"].read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
            self.assertEqual(reader.fieldnames, [
                "issuer_code", "name", "industry_code", "amount_twd",
                "share", "source", "security_code", "source_amount_twd"])
            rows = list(reader)
        for company in payload["companies"]:
            matches = [r for r in rows
                       if r["issuer_code"] == company["issuer_code"]]
            self.assertAlmostEqual(fsum(float(r["source_amount_twd"])
                                        for r in matches),
                                   company["amount_twd"])
            self.assertTrue(all(float(r["amount_twd"]) ==
                                company["amount_twd"] for r in matches))
        preferred = [r for r in rows if r["issuer_code"] == "2881"]
        self.assertEqual([(r["security_code"], r["source_amount_twd"])
                          for r in preferred],
                         [("2881", "80000.0"), ("2881A", "40000.0")])

    def test_output_bytes_are_stable_except_json_generated_at(self) -> None:
        """Keep timestamps confined to the injected JSON field."""
        with TemporaryDirectory() as folder:
            first = write_outputs(self.build(), Path(folder) / "a")
            again = write_outputs(self.build(), Path(folder) / "b")
            changed = write_outputs(
                self.build("2026-10-26T00:00:00Z"), Path(folder) / "c")
            for filename, path in first.items():
                self.assertEqual(path.read_bytes(),
                                 again[filename].read_bytes())
                if filename != "result.json":
                    self.assertEqual(path.read_bytes(),
                                     changed[filename].read_bytes())
            first_json = first["result.json"].read_text()
            self.assertTrue(first_json.endswith("\n"))
            self.assertIn("台積電", first_json)
            self.assertEqual(first_json, json.dumps(
                self.build(), sort_keys=True, ensure_ascii=False, indent=2)
                + "\n")
            original = json.loads(first_json)
            later = json.loads(changed["result.json"].read_text())
            original.pop("generated_at")
            later.pop("generated_at")
            self.assertEqual(original, later)

    def test_render_does_not_mutate_and_escapes_table_cells(self) -> None:
        """Preserve payload and stop source text from breaking table rows."""
        payload = self.build()
        payload["companies"][0]["name"] = "Name|extra\n<script>"
        before = json.dumps(payload, ensure_ascii=False)
        md = render_report_md(payload)
        self.assertEqual(json.dumps(payload, ensure_ascii=False), before)
        self.assertIn("Name\\|extra &lt;script&gt;", md)

    def test_empty_results_keep_headers_limits_without_division(self) -> None:
        """Support no identified companies and a zero total defensively."""
        payload = self.build()
        payload["companies"] = []
        payload["industries"] = []
        payload["snapshots"] = []
        payload["positions"] = []
        payload["totals"] = {k: ([] if k == "futures" else 0)
                             for k in payload["totals"]}
        payload["metrics"]["effective_holdings"] = None
        md = render_report_md(payload)
        self.assertIn("無可計算資料", md)
        self.assertIn("0.00%", md)
        with TemporaryDirectory() as folder:
            paths = write_outputs(payload, Path(folder))
            rows = paths["exposures.csv"].read_text(encoding="utf-8-sig")
            self.assertEqual(len(rows.splitlines()), 1)
