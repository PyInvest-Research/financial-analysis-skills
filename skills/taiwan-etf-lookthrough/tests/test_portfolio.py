"""Test local portfolio input validation and byte-level provenance."""

from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest

import support  # noqa: F401
from lookthrough.portfolio import (
    Portfolio, PortfolioError, Position, load_portfolio, parse_portfolio,
)


def pos(symbol: Any, kind: Any, value: Any) -> dict[str, Any]:
    """Build one input position without hiding invalid test values."""
    return dict(symbol=symbol, kind=kind, market_value=value)


def parse(**kwargs: Any) -> Portfolio:
    """Parse an ordinary portfolio with selected fields replaced."""
    data = dict(valuation_date="2026-10-23", currency="TWD",
                positions=[pos("0050", "etf", 400000)])
    data.update(kwargs)
    return parse_portfolio(data, b"original input bytes")


class PortfolioTests(unittest.TestCase):
    """Exercise strict field checks, merging and file loading."""

    def test_valid_portfolio(self) -> None:
        """Keep date and typed immutable positions for consumers."""
        portfolio = parse()
        self.assertEqual(portfolio.valuation_date, date(2026, 10, 23))
        self.assertEqual(portfolio.positions,
                         (Position("0050", "etf", 400000.0),))
        self.assertIsInstance(portfolio.positions[0].market_value, float)
        self.assertEqual(portfolio.warnings, ())

    def test_symbol_normalization(self) -> None:
        """Normalize case and whitespace without inventing leading zeros."""
        portfolio = parse(positions=[pos(" 00982a ", "etf", 1)])
        self.assertEqual(portfolio.positions[0].symbol, "00982A")
        for bad in ("50", "", "0050.TW", "ABCDEFG", "ＡＢＣＤ", 50):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(PortfolioError, "第 1 列"):
                    parse(positions=[pos(bad, "etf", 1)])

    def test_rejects_bad_market_values(self) -> None:
        """Reject strings, bools, invalid ranges and unrepresentable ints."""
        for bad in ["400,000", 0, -1, float("nan"), float("inf"),
                    -float("inf"), True, False, None, 10 ** 1000]:
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(PortfolioError, "第 1 列"):
                    parse(positions=[pos("0050", "etf", bad)])

    def test_duplicates_merged_with_warning(self) -> None:
        """Merge normalized identities and retain first-appearance order."""
        portfolio = parse(positions=[pos("etfa", "etf", 200000),
                                     pos("2330", "stock", 3),
                                     pos(" ETFA ", "etf", 200000),
                                     pos("ETFA", "etf", 100000)])
        self.assertEqual(portfolio.positions,
                         (Position("ETFA", "etf", 500000.0),
                          Position("2330", "stock", 3.0)))
        self.assertEqual(portfolio.warnings,
                         ("重複輸入 ETFA：已合併 3 列",))

    def test_two_duplicates_exact_warning(self) -> None:
        """Preserve the specified two-row warning contract verbatim."""
        portfolio = parse(positions=[pos("ETFA", "etf", 200000),
                                     pos("ETFA", "etf", 200000)])
        self.assertEqual(portfolio.positions,
                         (Position("ETFA", "etf", 400000.0),))
        self.assertIn("重複輸入 ETFA：已合併 2 列", portfolio.warnings)

    def test_rejects_conflicting_kinds(self) -> None:
        """Prevent normalized symbols from colliding across kinds."""
        with self.assertRaisesRegex(PortfolioError, "第 2 列"):
            parse(positions=[pos("etfa", "etf", 1),
                             pos(" ETFA ", "stock", 2)])

    def test_rejects_sum_overflow(self) -> None:
        """Reject infinite duplicate or whole-portfolio totals."""
        for symbol in ("0050", "2330"):
            with self.subTest(symbol=symbol):
                with self.assertRaisesRegex(PortfolioError, "第 2 列"):
                    parse(positions=[pos("0050", "etf", 1e308),
                                     pos(symbol, "etf", 1e308)])

    def test_rejects_malformed_rows_with_row_number(self) -> None:
        """Report the actual one-based input row on malformed positions."""
        for bad in (None, [], {}, pos("2330", "bond", 1),
                    pos("2330", None, 1), pos("2330", "STOCK", 1)):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(PortfolioError, "第 2 列"):
                    parse(positions=[pos("0050", "etf", 1), bad])

    def test_rejects_empty_or_nonarray_positions(self) -> None:
        """Require at least one position in a JSON array."""
        for bad in ([], None, {}, "0050", ()):
            with self.subTest(bad=bad), self.assertRaises(PortfolioError):
                parse(positions=bad)

    def test_rejects_non_twd_currency(self) -> None:
        """Require the exact supported currency rather than coercing it."""
        for bad in ("USD", "twd", " TWD ", None, 1):
            with self.subTest(bad=bad), self.assertRaises(PortfolioError):
                parse(currency=bad)

    def test_rejects_non_iso_or_invalid_dates(self) -> None:
        """Reject compact dates, weeks, timestamps and invalid days."""
        for bad in ("20261023", "2026-W43-5", "2026-02-30", None,
                    "2026-10-23T00:00:00", date(2026, 10, 23)):
            with self.subTest(bad=bad), self.assertRaises(PortfolioError):
                parse(valuation_date=bad)

    def test_rejects_missing_fields_and_wrong_root(self) -> None:
        """Reject invalid structures through the public error type."""
        for bad in ({}, [], None):
            with self.subTest(bad=bad), self.assertRaises(PortfolioError):
                parse_portfolio(bad, b"raw")
        data = dict(valuation_date="2026-10-23", currency="TWD",
                    positions=[pos("0050", "etf", 1)])
        for key in data:
            with self.subTest(key=key), self.assertRaises(PortfolioError):
                parse_portfolio({k: v for k, v in data.items() if k != key},
                                b"raw")

    def test_hashes_raw_bytes_without_reencoding(self) -> None:
        """Whitespace differences in actual input change provenance hash."""
        data = dict(valuation_date="2026-10-23", currency="TWD",
                    positions=[pos("0050", "etf", 1)])
        raw = json.dumps(data).encode("utf-8")
        first = parse_portfolio(data, raw)
        second = parse_portfolio(data, raw + b"\n")
        self.assertEqual(first.input_sha256, hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(first.input_sha256, second.input_sha256)

    def test_loads_local_json_and_wraps_invalid_input(self) -> None:
        """Read bytes locally and expose file/JSON failures consistently."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "portfolio.json"
            raw = (b'{"valuation_date":"2026-10-23","currency":"TWD",'
                   b'"positions":[{"symbol":"2330","kind":"stock",'
                   b'"market_value":1.5}]}\n')
            path.write_bytes(raw)
            result = load_portfolio(path)
            self.assertEqual(result.positions,
                             (Position("2330", "stock", 1.5),))
            self.assertEqual(result.input_sha256,
                             hashlib.sha256(raw).hexdigest())
            for invalid in (b"{", b"\xff", b"[]"):
                path.write_bytes(invalid)
                with self.assertRaises(PortfolioError):
                    load_portfolio(path)
            with self.assertRaises(PortfolioError):
                load_portfolio(Path(temp) / "missing.json")
