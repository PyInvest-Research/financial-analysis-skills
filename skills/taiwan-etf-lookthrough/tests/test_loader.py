"""Test synthetic official-format CSV and XLSX holdings ingestion."""

import csv
from datetime import date
import io
from pathlib import Path
import tempfile
from typing import Any
import unittest

from openpyxl import Workbook

from support import Snapshot
from lookthrough.loader import (
    ColumnMap, default_kind, load_official_file, parse_number,
)


FIX = Path(__file__).parent / "fixtures"
COLUMNS = ColumnMap("股票代號", "股票名稱", "持股權重")
META = dict(etf_symbol="SYNTHETIC", issuer="Synthetic Issuer",
            is_active=False, holdings_date=date(2026, 10, 22),
            source_url="https://example.com/synthetic-holdings",
            fetched_at="2026-10-23T00:00:00+00:00")


class LoaderTests(unittest.TestCase):
    """Cover decoding, tabular structure, units and explicit failures."""

    def setUp(self) -> None:
        """Create isolated temporary input files."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def csv_file(self, text: str) -> Path:
        """Write a UTF-8 CSV with BOM."""
        path = self.root / "holdings.csv"
        path.write_text(text, encoding="utf-8-sig")
        return path

    def load(
        self, path: Path, columns: ColumnMap = COLUMNS, **kwargs: Any,
    ) -> Snapshot:
        """Load synthetic input with consistent metadata."""
        return load_official_file(path, columns, **META, **kwargs)

    def test_cp950_percent_strings(self) -> None:
        """Read cp950 and preserve futures NAV-only weights."""
        snapshot = self.load(FIX / "official_cp950.csv")
        holdings = {row.code: row for row in snapshot.holdings}
        self.assertAlmostEqual(holdings["2330"].weight, 0.6012)
        self.assertEqual(holdings["TXFK6"].kind, "futures")
        self.assertAlmostEqual(holdings["TXFK6"].weight, 0.0123)
        self.assertIsNone(holdings["TXFK6"].exposure_weight)
        self.assertEqual(snapshot.source_kind, "agent_official")
        self.assertEqual(snapshot.holdings_date, META["holdings_date"])

    def test_header_row_autodetected_after_preamble(self) -> None:
        """Skip incomplete header-like preamble rows."""
        path = self.csv_file("Synthetic only\n股票代號,股票名稱\n"
                             "股票代號,股票名稱,持股權重\n2330,合成股票,60.12%\n")
        self.assertEqual(self.load(path).holdings[0].weight, 0.6012)

    def test_xlsx_matches_csv(self) -> None:
        """Match a generated XLSX against the synthetic cp950 CSV."""
        workbook = Workbook()
        for row in csv.reader(io.StringIO(
            (FIX / "official_cp950.csv").read_text(encoding="cp950")
        )):
            workbook.active.append(row)
        path = self.root / "official.xlsx"
        workbook.save(path)
        workbook.close()
        self.assertEqual(self.load(path).holdings,
                         self.load(FIX / "official_cp950.csv").holdings)

    def test_fraction_and_explicit_kind(self) -> None:
        """Respect declared fractions and explicit kinds."""
        path = self.csv_file("code,name,weight,kind\n2330,S,0.6012,other\n")
        snapshot = self.load(path, ColumnMap(
            "code", "name", "weight", "kind", weight_unit="fraction"
        ), source_kind="official")
        self.assertEqual(snapshot.holdings[0].weight, 0.6012)
        self.assertEqual(snapshot.holdings[0].kind, "other")
        self.assertEqual(snapshot.source_kind, "official")

    def test_no_unit_guessing(self) -> None:
        """Convert small percent values using the declared unit."""
        path = self.csv_file("code,name,weight\n2330,Synthetic,0.6\n")
        self.assertEqual(self.load(path, ColumnMap(
            "code", "name", "weight"
        )).holdings[0].weight, 0.006)

    def test_header_search_boundary_and_explicit_row(self) -> None:
        """Search exactly 20 rows; explicit header rows are one-based."""
        for preamble in (19, 20):
            path = self.csv_file("Synthetic\n" * preamble
                                 + "code,name,weight\n2330,Synthetic,60\n")
            columns = ColumnMap("code", "name", "weight")
            if preamble == 20:
                with self.assertRaisesRegex(ValueError, "header"):
                    self.load(path, columns)
                columns = ColumnMap("code", "name", "weight", header_row=21)
            self.assertEqual(self.load(path, columns).holdings[0].weight, 0.6)

    def test_missing_weights_and_custom_rules(self) -> None:
        """Keep missing weights and apply custom issuer rules."""
        path = self.csv_file("code,name,weight\n\n2330,Synthetic,\n")
        snapshot = self.load(path, ColumnMap("code", "name", "weight"),
                             kind_rules=lambda code, name: "cash")
        self.assertEqual(len(snapshot.holdings), 1)
        self.assertIsNone(snapshot.holdings[0].weight)
        self.assertEqual(snapshot.holdings[0].kind, "cash")

    def test_rejects_unknown_extension(self) -> None:
        """Reject unsupported formats before opening them."""
        with self.assertRaisesRegex(ValueError, "extension"):
            self.load(self.root / "official.xls")

    def test_rejects_invalid_encoding(self) -> None:
        """Fail when both strict decoders reject the source bytes."""
        path = self.root / "bad.csv"
        path.write_bytes(b"\xff\xff")
        with self.assertRaisesRegex(ValueError, "decode"):
            self.load(path)

    def test_rejects_malformed_csv(self) -> None:
        """Reject unclosed quoted fields."""
        path = self.csv_file('code,name,weight\n2330,"Synthetic,60\n')
        with self.assertRaisesRegex(ValueError, "CSV"):
            self.load(path, ColumnMap("code", "name", "weight"))

    def test_rejects_corrupt_xlsx(self) -> None:
        """Surface workbook errors as descriptive ValueErrors."""
        path = self.root / "bad.xlsx"
        path.write_bytes(b"not a workbook")
        with self.assertRaisesRegex(ValueError, "XLSX"):
            self.load(path)

    def test_rejects_bad_rows_with_line_number(self) -> None:
        """Identify malformed cells, values and unknown kinds."""
        for row in ("2330,S,bad", "2330,S", "2330,S,NaN", "2330,S,Inf"):
            with self.subTest(row=row):
                path = self.csv_file("code,name,weight\n" + row + "\n")
                with self.assertRaisesRegex(ValueError, "row 2"):
                    self.load(path, ColumnMap("code", "name", "weight"))
        path = self.csv_file("code,name,weight,kind\n2330,S,60,unknown\n")
        with self.assertRaisesRegex(ValueError, "row 2.*kind"):
            self.load(path, ColumnMap("code", "name", "weight", "kind"))

    def test_rejects_empty_and_duplicate_headers(self) -> None:
        """Reject absent headers and ambiguous column names."""
        for text in ("", "code,name,weight,weight\n2330,S,60,50\n"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "header"):
                    self.load(self.csv_file(text),
                              ColumnMap("code", "name", "weight"))

    def test_rejects_invalid_configuration(self) -> None:
        """Reject undeclared units and invalid row numbers."""
        for kwargs in ({"weight_unit": "guess"}, {"header_row": 0},
                       {"header_row": -1}, {"header_row": True},
                       {"header_row": 1.5}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.load(FIX / "official_cp950.csv",
                              ColumnMap("code", "name", "weight", **kwargs))

    def test_xlsx_numeric_and_boolean_weights(self) -> None:
        """Accept numeric Excel cells but reject boolean weights."""
        for weight in (60.12, True):
            workbook = Workbook()
            workbook.active.append(["code", "name", "weight"])
            workbook.active.append([2330, "Synthetic", weight])
            path = self.root / "numeric.xlsx"
            workbook.save(path)
            workbook.close()
            columns = ColumnMap("code", "name", "weight")
            if weight is True:
                with self.assertRaisesRegex(ValueError, "row 2"):
                    self.load(path, columns)
            else:
                self.assertEqual(self.load(path, columns).holdings[0].code,
                                 "2330")
                self.assertEqual(self.load(path, columns).holdings[0].weight,
                                 0.6012)


class NumberAndKindTests(unittest.TestCase):
    """Test scalar formatting and specified instrument heuristics."""

    def test_parse_number_formatting(self) -> None:
        """Strip commas, percent markers and fullwidth spaces."""
        self.assertEqual(parse_number("　1,234.5%　"), 1234.5)
        self.assertEqual(parse_number(" -1,234 "), -1234.0)

    def test_parse_number_rejects_blank(self) -> None:
        """Reject blank, nonfinite and non-string inputs."""
        for value in ("", "　 ", "NaN", "inf", "-Infinity", True, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_number(value)

    def test_default_kind(self) -> None:
        """Apply futures and cash precedence before stock codes."""
        for code, name, expected in (
            ("2330", "Synthetic", "stock"),
            ("2881A", "Synthetic", "stock"),
            ("TXFK6", "Synthetic", "futures"),
            ("MTX", "Synthetic", "futures"),
            ("TE", "Synthetic", "futures"),
            ("TF", "Synthetic", "futures"),
            ("X", "臺股期貨", "futures"),
            ("2330", "現金存款", "cash"),
            ("X", "應收帳款", "cash"),
            ("X", "應付帳款", "cash"),
            ("X", "Synthetic", "other"),
        ):
            with self.subTest(code=code, name=name):
                self.assertEqual(default_kind(code, name), expected)
