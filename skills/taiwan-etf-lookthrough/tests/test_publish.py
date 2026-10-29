"""Verify publication, content identity and safe retention offline."""

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
import io
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

from support import make_snapshot
from lookthrough import publish
from lookthrough.directory import CompanyDirectory
from lookthrough.issuers.registry import SupportedEtf
from lookthrough.models import Snapshot, snapshot_to_dict
from lookthrough.sources import fetch_index, fetch_pyinvest_snapshot


STAMP = "2026-10-23T00:00:00+00:00"
LATER = "2026-10-24T00:00:00+00:00"
TODAY = date(2026, 10, 23)
ETF = SupportedEtf(
    "0052", "fubon", False, "http",
    "https://websys.fsit.com.tw/FubonETF/Trade/Assets.aspx?stkId=0052",
)
FIXTURES = Path(__file__).parent / "fixtures" / "issuers" / "fubon"


def snapshot(**changes: Any) -> Snapshot:
    """Build schema-valid synthetic holdings for the requested ETF."""
    return replace(make_snapshot(), etf_symbol=ETF.symbol, issuer=ETF.issuer,
                   source_url=ETF.source_url, **changes)


def write_snapshot(root: Path, value: Snapshot,
                   filename: str | None = None) -> Path:
    """Write a real managed snapshot fixture without publisher helpers."""
    folder = root / value.etf_symbol
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (filename or value.holdings_date.isoformat() + ".json")
    path.write_text(json.dumps(snapshot_to_dict(value)), encoding="utf-8")
    return path


def tree_bytes(root: Path) -> dict[str, tuple[bytes, int]]:
    """Capture bytes and mtimes to detect incidental writes."""
    return {str(path.relative_to(root)): (path.read_bytes(),
                                          path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


class PublishTests(unittest.TestCase):
    """Exercise filesystem products with injected acquisition boundaries."""

    def setUp(self) -> None:
        """Give each test an independent root and offline directory."""
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "output"
        self.directory = CompanyDirectory([], "2026-10-23")
        self.registry = patch.object(publish, "SUPPORTED_ETFS", (ETF,))
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def run_publish(
            self, value: Snapshot | None = None, *,
            fetch: Callable[[SupportedEtf], bytes] | None = None,
            stamp: str = STAMP,
    ) -> dict:
        """Use the real pipeline with synthetic parser results."""
        value = value or snapshot()
        with patch.dict(publish.PARSERS, {"fubon": lambda *args: value}):
            return publish.publish_all(
                self.root, fetch=fetch or (lambda row: b"synthetic"),
                today=TODAY, fetched_at=stamp, directory=self.directory)

    def test_real_parser_writes_three_consumable_products(self) -> None:
        """Catch missing products, wrong index fields and consumer mismatch."""
        raw = (FIXTURES / "0052.2026-10-22.html").read_bytes()
        index = publish.publish_all(
            self.root, fetch=lambda row: raw, today=TODAY,
            fetched_at=STAMP, directory=self.directory)
        self.assertEqual(set(tree_bytes(self.root)), {
            "index.json", "0052/latest.json", "0052/2026-10-22.json"})
        self.assertEqual(index["schema_version"], "1")
        self.assertEqual(index["generated_at"], STAMP)
        row = index["etfs"][0]
        self.assertEqual(set(row), {
            "symbol", "issuer", "is_active", "route", "latest_date",
            "dates", "last_status", "last_error", "last_attempt_at",
            "source_url"})
        self.assertEqual(row["symbol"], "0052")
        self.assertEqual(row["latest_date"], "2026-10-22")
        self.assertEqual(row["dates"], ["2026-10-22"])
        self.assertIn(row["last_status"], ("pass", "warn"))
        self.assertIsNone(row["last_error"])
        self.assertEqual(row["last_attempt_at"], STAMP)

        def local_open(url: str) -> bytes:
            """Resolve only test-local URLs for real snapshot consumers."""
            return (self.root / url.removeprefix("https://test/")).read_bytes()

        self.assertEqual(fetch_index("https://test", local_open), index)
        loaded = fetch_pyinvest_snapshot(
            "0052", base_url="https://test", opener=local_open)
        self.assertEqual(loaded.holdings_date, date(2026, 10, 22))
        self.assertEqual((self.root / "0052/latest.json").read_bytes(),
                         (self.root / "0052/2026-10-22.json").read_bytes())

    def test_invalid_parser_results_keep_good_bytes(self) -> None:
        """Catch promotion of malformed, invalid or wrong-ETF snapshots."""
        self.run_publish()
        good = tree_bytes(self.root)
        bad_values = [
            replace(snapshot(), schema_version="2"),
            replace(snapshot(), holdings=()),
            replace(snapshot(), etf_symbol="0050"),
            replace(snapshot(), issuer="yuanta"),
            replace(snapshot(), is_active=True),
        ]
        for value in bad_values:
            with self.subTest(value=value):
                row = self.run_publish(value, stamp=LATER)["etfs"][0]
                self.assertEqual(row["last_status"], "fail")
                self.assertTrue(row["last_error"])
                self.assertEqual(row["last_attempt_at"], LATER)
                self.assertEqual(row["latest_date"], "2026-10-22")
                for name in ("0052/latest.json", "0052/2026-10-22.json"):
                    self.assertEqual(tree_bytes(self.root)[name], good[name])
        with patch.dict(publish.PARSERS, {
                "fubon": lambda *args: (_ for _ in ()).throw(
                    ValueError("unrecognized table"))}):
            row = publish.publish_all(
                self.root, fetch=lambda row: b"bad", today=TODAY,
                fetched_at=LATER, directory=self.directory)["etfs"][0]
        self.assertEqual(row["last_status"], "fail")
        self.assertIn("unrecognized table", row["last_error"])
        self.assertEqual(tree_bytes(self.root)["0052/latest.json"],
                         good["0052/latest.json"])

    def test_fetch_error_keeps_old_snapshots(self) -> None:
        """Catch transport failures misclassified or overwriting good data."""
        self.run_publish()
        before = tree_bytes(self.root)

        def unavailable(row: SupportedEtf) -> bytes:
            """Simulate a bounded transport failure without network."""
            raise TimeoutError("source timeout")

        row = self.run_publish(fetch=unavailable, stamp=LATER)["etfs"][0]
        self.assertEqual(row["last_status"], "fetch_error")
        self.assertIn("source timeout", row["last_error"])
        self.assertEqual(tree_bytes(self.root)["0052/latest.json"],
                         before["0052/latest.json"])

    def test_failure_changes_only_prior_index_attempt_fields(self) -> None:
        """Catch failed acquisition rewriting previous published metadata."""
        before = self.run_publish()["etfs"][0]
        updated_route = replace(ETF, source_url=ETF.source_url + "&version=2")

        def unavailable(row: SupportedEtf) -> bytes:
            """Raise an error without text to require useful diagnostics."""
            raise TimeoutError()

        with patch.object(publish, "SUPPORTED_ETFS", (updated_route,)):
            for fetch, value, status in (
                    (unavailable, None, "fetch_error"),
                    (None, snapshot(holdings=()), "fail")):
                after = self.run_publish(value, fetch=fetch,
                                         stamp=LATER)["etfs"][0]
                attempt = {"last_status", "last_error", "last_attempt_at"}
                self.assertEqual({key: value for key, value in after.items()
                                  if key not in attempt},
                                 {key: value for key, value in before.items()
                                  if key not in attempt})
                self.assertEqual(after["last_status"], status)
                self.assertTrue(after["last_error"])
                self.assertEqual(after["last_attempt_at"], LATER)

    def test_cathay_five_fallback_rows_never_fetch_or_create_latest(
        self,
    ) -> None:
        """Expose five manual routes without producing public holdings."""
        from lookthrough.issuers.registry import SUPPORTED_ETFS

        cathay = tuple(row for row in SUPPORTED_ETFS
                       if row.issuer == "cathay"
                       and row.route == "official_only")
        self.assertEqual(len(cathay), 4)
        fetch = MagicMock(side_effect=AssertionError("manual-only fetch"))
        with patch.object(publish, "SUPPORTED_ETFS", cathay):
            index = publish.publish_all(
                self.root, fetch=fetch, today=TODAY, fetched_at=STAMP,
                directory=self.directory)
        fetch.assert_not_called()
        self.assertEqual({row["symbol"] for row in index["etfs"]},
                         {"00400A", "00701", "00881", "00922"})
        for row in index["etfs"]:
            self.assertEqual(row["route"], "official_only")
            self.assertEqual(row["last_status"], "official_only")
            self.assertIsNone(row["latest_date"])
            self.assertEqual(row["dates"], [])
        self.assertEqual(set(tree_bytes(self.root)), {"index.json"})

    def test_official_only_never_fetches_and_keeps_history(self) -> None:
        """Catch requests or history deletion after a route change."""
        self.run_publish()
        before = tree_bytes(self.root)

        def forbidden(row: SupportedEtf) -> bytes:
            """Fail immediately if official-only acquisition is attempted."""
            self.fail("official_only must not fetch")

        with patch.object(publish, "SUPPORTED_ETFS", (
                replace(ETF, route="official_only"),)):
            row = self.run_publish(fetch=forbidden)["etfs"][0]
        self.assertEqual(row["route"], "official_only")
        self.assertIsNone(row["latest_date"])
        self.assertEqual(row["dates"], ["2026-10-22"])
        self.assertEqual(tree_bytes(self.root)["0052/latest.json"],
                         before["0052/latest.json"])
        with patch.object(publish, "SUPPORTED_ETFS", ()):
            index = self.run_publish()
        self.assertEqual(index["etfs"][0]["symbol"], "0052")
        self.assertTrue((self.root / "0052/2026-10-22.json").exists())

    def test_fetch_time_churn_preserves_snapshot_bytes_and_mtime(self) -> None:
        """Catch rewrites caused only by collection timestamp changes."""
        self.run_publish()
        before = tree_bytes(self.root)
        row = self.run_publish(snapshot(fetched_at=LATER),
                               stamp=LATER)["etfs"][0]
        self.assertNotIn("revised_at", row)
        for name in ("0052/latest.json", "0052/2026-10-22.json"):
            self.assertEqual(tree_bytes(self.root)[name], before[name])

    def test_financial_and_provenance_changes_record_revision(self) -> None:
        """Catch silently discarded same-date financial/provenance changes."""
        self.run_publish()
        value = snapshot()
        changed = replace(value.holdings[0], weight=0.61)
        value = replace(value, holdings=(changed,) + value.holdings[1:])
        row = self.run_publish(value, stamp=LATER)["etfs"][0]
        self.assertEqual(row["revised_at"], LATER)
        latest = json.loads((self.root / "0052/latest.json").read_text())
        self.assertEqual(latest["holdings"][0]["weight"], 0.61)
        value = replace(value, source_url=ETF.source_url + "&revision=2")
        self.run_publish(value, stamp=LATER)
        latest = json.loads((self.root / "0052/latest.json").read_text())
        self.assertEqual(latest["source_url"], value.source_url)

    def test_older_collection_does_not_regress_latest(self) -> None:
        """Catch selecting latest by collection time instead of source day."""
        self.run_publish()
        before = tree_bytes(self.root)["0052/latest.json"]
        row = self.run_publish(snapshot(holdings_date=date(2026, 10, 21)),
                               stamp=LATER)["etfs"][0]
        self.assertEqual(row["latest_date"], "2026-10-22")
        self.assertEqual(row["dates"], ["2026-10-21", "2026-10-22"])
        self.assertEqual(tree_bytes(self.root)["0052/latest.json"], before)

    def test_corrupt_products_fail_before_any_replacement(self) -> None:
        """Catch treating broken prior data as an empty publication root."""
        for name in ("index.json", "0052/latest.json",
                     "0052/2026-10-22.json"):
            with self.subTest(name=name):
                self.root.mkdir(exist_ok=True)
                self.run_publish()
                path = self.root / name
                original = path.read_bytes()
                path.write_text("{broken", encoding="utf-8")
                before = tree_bytes(self.root)
                with self.assertRaisesRegex(ValueError, name):
                    self.run_publish(stamp=LATER)
                self.assertEqual(tree_bytes(self.root), before)
                path.write_bytes(original)

    def test_index_schema_and_referenced_identity_are_checked(self) -> None:
        """Catch corrupt index shapes or identities surviving preflight."""
        self.run_publish()
        path = self.root / "index.json"
        original = path.read_bytes()
        for payload in ([], {"schema_version": "2", "etfs": []},
                        {"schema_version": "1", "generated_at": STAMP,
                         "etfs": [{"symbol": "../escape"}]}):
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "index.json"):
                self.run_publish()
        path.write_bytes(original)
        write_snapshot(self.root, snapshot(holdings_date=date(2026, 10, 20)),
                       "2026-10-22.json")
        with self.assertRaisesRegex(ValueError, "2026-10-22.json"):
            self.run_publish()

    def test_cli_dry_run_fetches_validates_and_never_writes(self) -> None:
        """Catch dry-run target writes and fake success without parsing."""
        raw = (FIXTURES / "0052.2026-10-22.html").read_bytes()
        for existing in (False, True):
            if existing:
                self.run_publish()
                (self.root / "pinned.json").write_text('{"pinned": []}')
            before = tree_bytes(self.root)
            stdout = io.StringIO()
            with patch.object(publish, "fetch_official", return_value=raw), \
                    patch.object(publish, "_utc_now", return_value=STAMP), \
                    redirect_stdout(stdout):
                result = publish.main(["--out", str(self.root), "--dry-run"])
            self.assertEqual(result, 0)
            self.assertRegex(stdout.getvalue(),
                             r"0052 (pass|warn) 2026-10-22 0\.")
            self.assertEqual(tree_bytes(self.root), before)
            self.assertEqual(self.root.exists(), existing)
        with patch.object(publish, "fetch_official", return_value=b"bad"), \
                patch.object(publish, "_utc_now", return_value=STAMP), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(publish.main([
                "--out", str(self.root), "--dry-run"]), 1)

    def test_cli_fetch_failure_emits_existing_error_as_one_ascii_json(
        self,
    ) -> None:
        """Keep untrusted source controls inside one escaped JSON record."""
        message = "timeout\n::error::injected\r\t\x1b[31m非ASCII\u2028next"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(publish, "fetch_official", side_effect=TimeoutError(
                message)) as fetch, \
                patch.object(publish, "_utc_now", return_value=STAMP), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(publish.main(["--out", str(self.root)]), 1)
        fetch.assert_called_once_with(ETF)
        row = json.loads((self.root / "index.json").read_text())["etfs"][0]
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].isascii())
        self.assertTrue(lines[0].startswith("{"))
        self.assertIn("\\n::error::", lines[0])
        self.assertIn("\\r\\t\\u001b", lines[0])
        self.assertEqual(json.loads(lines[0]), {
            "symbol": "0052", "last_status": "fetch_error",
            "last_error": row["last_error"],
        })
        self.assertEqual(row["last_error"], "TimeoutError: " + message)
        self.assertEqual(stdout.getvalue(), "0052 fetch_error - unknown\n")

    def test_cli_validation_failure_emits_existing_index_reason(
        self,
    ) -> None:
        """Expose validation failures without another fetch or parse."""
        # Future PCF dates now warn; use an actual hard numeric failure.
        stale = replace(snapshot(), holdings=(
            replace(snapshot().holdings[0], weight=2.0),))
        parser = MagicMock(return_value=stale)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(publish, "fetch_official", return_value=b"source") \
                as fetch, patch.dict(publish.PARSERS, {"fubon": parser}), \
                patch.object(publish, "_utc_now", return_value=STAMP), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(publish.main(["--out", str(self.root)]), 1)
        fetch.assert_called_once_with(ETF)
        parser.assert_called_once()
        row = json.loads((self.root / "index.json").read_text())["etfs"][0]
        self.assertEqual(json.loads(stderr.getvalue()), {
            "symbol": "0052", "last_status": "fail",
            "last_error": row["last_error"],
        })
        self.assertIn("weights_in_range", row["last_error"])
        self.assertTrue(stdout.getvalue().startswith("0052 fail 2026-10-22 "))

    def test_cli_success_and_official_only_emit_no_error(self) -> None:
        """Leave successful and manual-only rows on unchanged stdout only."""
        raw = (FIXTURES / "0052.2026-10-22.html").read_bytes()
        manual = SupportedEtf(
            "00878", "cathay", False, "official_only",
            "https://www.cathaysite.com.tw/fund-details/ECN?tab=portfolio")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(publish, "SUPPORTED_ETFS", (ETF, manual)), \
                patch.object(publish, "fetch_official", return_value=raw) \
                as fetch, patch.object(
                    publish, "_utc_now", return_value=STAMP), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(publish.main([
                "--out", str(self.root), "--dry-run"]), 0)
        fetch.assert_called_once_with(ETF)
        self.assertEqual(stderr.getvalue(), "")
        self.assertRegex(stdout.getvalue(), r"0052 (pass|warn) 2026-10-22 0\.")
        self.assertIn("00878 official_only - unknown\n", stdout.getvalue())
        self.assertFalse(self.root.exists())

    def test_cli_failure_preserves_all_good_history(self) -> None:
        """Catch automatic retention deleting prior good data on failure."""
        self.run_publish()
        for offset in range(70):
            write_snapshot(self.root, snapshot(holdings_date=(
                date(2026, 8, 1) + timedelta(days=offset))))
        before = tree_bytes(self.root)
        with patch.object(publish, "fetch_official", side_effect=TimeoutError(
                "source timeout")), \
                patch.object(publish, "_utc_now", return_value=STAMP), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(publish.main(["--out", str(self.root)]), 1)
        after = tree_bytes(self.root)
        self.assertEqual({name: value for name, value in after.items()
                          if name != "index.json"},
                         {name: value for name, value in before.items()
                          if name != "index.json"})

    def test_cli_non_dry_run_prunes_and_refreshes_index_dates(self) -> None:
        """Catch stale index date links after successful automatic pruning."""
        for offset in range(70):
            write_snapshot(self.root, snapshot(holdings_date=(
                date(2026, 8, 1) + timedelta(days=offset))))
        (self.root / "pinned.json").write_text(
            '{"pinned": [["0052", "2026-08-03"]]}')
        raw = (FIXTURES / "0052.2026-10-22.html").read_bytes()
        with patch.object(publish, "fetch_official", return_value=raw), \
                patch.object(publish, "_utc_now", return_value=STAMP), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(publish.main(["--out", str(self.root)]), 0)
        index = json.loads((self.root / "index.json").read_text())
        row = index["etfs"][0]
        self.assertEqual(len(row["dates"]), 32)
        self.assertIn("2026-08-03", row["dates"])
        self.assertIn("2026-08-31", row["dates"])
        self.assertEqual(row["latest_date"], "2026-10-22")
        self.assertFalse((self.root / "0052/2026-08-01.json").exists())
        self.assertTrue(all((self.root / "0052" / f"{day}.json").is_file()
                            for day in row["dates"]))

    def test_cli_invalid_saved_pins_fail_before_any_write(self) -> None:
        """Catch output mutations before discovering malformed pins."""
        self.run_publish()
        (self.root / "pinned.json").write_text('{"pinned": [["../x", "bad"]]}')
        before = tree_bytes(self.root)
        stderr = io.StringIO()
        forbidden = AssertionError("Invalid pins reached acquisition")
        with patch.object(publish, "fetch_official", side_effect=forbidden), \
                redirect_stderr(stderr):
            self.assertEqual(publish.main(["--out", str(self.root)]), 1)
        self.assertIn("pinned.json", stderr.getvalue())
        self.assertEqual(tree_bytes(self.root), before)

    def test_cli_uses_taiwan_calendar_day(self) -> None:
        """Catch false future-date rejection before midnight UTC."""
        raw = (FIXTURES / "0052.2026-10-22.html").read_bytes()
        with patch.object(publish, "fetch_official", return_value=raw), \
                patch.object(publish, "_utc_now",
                             return_value="2026-10-21T17:00:00+00:00"), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(publish.main([
                "--out", str(self.root), "--dry-run"]), 0)


class PruneTests(unittest.TestCase):
    """Verify retention independently of source registry membership."""

    def test_retains_recent_thirty_month_lasts_and_pinned(self) -> None:
        """Catch missing month ends, recent snapshots or pinned dates."""
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as folder:
            root = Path(folder)
            for offset in range(70):
                day = date(2026, 8, 1) + timedelta(days=offset)
                write_snapshot(root, snapshot(holdings_date=day))
            pinned = {("0052", "2026-08-03")}
            deleted = publish.prune(root, pinned)
            retained = {path.name for path in (root / "0052").iterdir()}
            want = {"2026-08-03.json", "2026-08-31.json"}
            for offset in range(30):
                want.add((date(2026, 9, 10) + timedelta(days=offset)
                          ).isoformat() + ".json")
            self.assertEqual(retained, want)
            self.assertEqual(len(deleted), 38)
            self.assertEqual(deleted, sorted(deleted))

    def test_unsafe_malformed_and_symlink_paths_are_untouched(self) -> None:
        """Catch deletion or traversal beyond internally matching snapshots."""
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as folder:
            root = Path(folder) / "out"
            old = write_snapshot(root, snapshot(
                holdings_date=date(2026, 8, 1)))
            write_snapshot(root, snapshot(holdings_date=date(2026, 8, 2)))
            latest = write_snapshot(root, snapshot(), "latest.json")
            invalid = root / "0052/2026-08-03.json"
            invalid.write_text("{}")
            mismatch = write_snapshot(root, snapshot(), "2026-08-04.json")
            unknown = root / "0052/notes.json"
            unknown.write_text("keep")
            unsafe = root / "bad-symbol"
            unsafe.mkdir()
            (unsafe / "2026-08-01.json").write_bytes(old.read_bytes())
            external = Path(folder) / "external"
            external.mkdir()
            target = write_snapshot(external, snapshot(
                holdings_date=date(2026, 7, 1)))
            link = root / "0052/2026-07-01.json"
            link.symlink_to(target)
            (root / "0050").symlink_to(external, target_is_directory=True)
            before = {path: path.read_bytes() for path in (
                latest, invalid, mismatch, unknown, target)}
            publish.prune(root, set(), keep_daily=0)
            for path, raw in before.items():
                self.assertEqual(path.read_bytes(), raw)
            self.assertTrue(link.is_symlink())
            self.assertTrue((root / "0050").is_symlink())
            self.assertFalse(old.exists())

    def test_invalid_pins_fail_before_deleting_anything(self) -> None:
        """Catch partial destructive work before invalid pin validation."""
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as folder:
            root = Path(folder)
            write_snapshot(root, snapshot(holdings_date=date(2026, 8, 1)))
            write_snapshot(root, snapshot(holdings_date=date(2026, 8, 2)))
            before = tree_bytes(root)
            for pins in ({("../x", "2026-08-01")},
                         {("0052", "2026-99-01")}, {"0052"}):
                with self.subTest(pins=pins):
                    with self.assertRaises(ValueError):
                        publish.prune(root, pins, keep_daily=0)
                    self.assertEqual(tree_bytes(root), before)

    def test_bad_pins_and_invalid_snapshots_are_preserved(self) -> None:
        """Catch malformed policy or invalid financial files being deleted."""
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as folder:
            root = Path(folder)
            bad = replace(snapshot(), holdings=(
                replace(snapshot().holdings[0], weight=-0.5),))
            path = write_snapshot(root, replace(
                bad, holdings_date=date(2026, 8, 1)))
            write_snapshot(root, snapshot(holdings_date=date(2026, 8, 2)))
            policy = root / "pinned.json"
            policy.write_text('{"pinned": "invalid"}')
            before = tree_bytes(root)
            with self.assertRaisesRegex(ValueError, "pinned.json"):
                publish.prune(root, set(), keep_daily=0)
            self.assertEqual(tree_bytes(root), before)
            policy.write_text('{"pinned": []}')
            self.assertEqual(publish.prune(root, set(), keep_daily=0), [])
            self.assertTrue(path.exists())


class TransportTests(unittest.TestCase):
    """Exercise request and redirect trust boundaries without real HTTPS."""

    def test_yuanta_api_exact_host_get_uses_existing_transport(self) -> None:
        """Allow the qualified API host through the bounded GET policy."""
        from lookthrough.issuers.registry import get_supported

        etf = get_supported("0050")
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.geturl.return_value = etf.source_url
        response.getheader.return_value = None
        response.getcode.return_value = 200
        response.read.return_value = b"official raw JSON"
        with patch.object(publish, "build_opener", return_value=opener):
            self.assertEqual(publish.fetch_official(etf), b"official raw JSON")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, etf.source_url)
        with self.assertRaises(ValueError):
            publish._check_url(
                "https://etfapi.yuantaetfs.com.evil.test/a")

    def test_untrusted_routes_are_rejected_before_any_request(self) -> None:
        """Catch credentials, nonofficial hosts and insecure transports."""
        urls = [
            "http://websys.fsit.com.tw/a", "https://evil.test/a",
            "https://websys.fsit.com.tw.evil.test/a",
            "https://u:p@websys.fsit.com.tw/a",
            "https://websys.fsit.com.tw:444/a",
            "https://websys.fsit.com.tw:bad/a",
        ]
        with patch.object(publish, "build_opener", side_effect=AssertionError(
                "untrusted URL reached transport")):
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    publish.fetch_official(replace(ETF, source_url=url))
            with self.assertRaisesRegex(ValueError, "playwright"):
                publish.fetch_official(replace(ETF, route="playwright"))

    def test_redirects_keep_original_host_and_never_downgrade(self) -> None:
        """Catch cross-official-host, foreign-host or insecure redirects."""
        request = publish.Request(ETF.source_url)
        redirect = publish._OfficialRedirect("websys.fsit.com.tw")
        for url in ("https://evil.test/a", "http://websys.fsit.com.tw/a",
                    "https://www.yuantaetfs.com/a",
                    "https://user@websys.fsit.com.tw/a",
                    "https://websys.fsit.com.tw:444/a"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                redirect.redirect_request(request, None, 302, "Found", {}, url)
        safe = redirect.redirect_request(
            request, None, 302, "Found", {},
            "https://websys.fsit.com.tw/redirected")
        self.assertEqual(safe.full_url,
                         "https://websys.fsit.com.tw/redirected")

    def test_transport_read_cap_proxy_policy_and_final_host(self) -> None:
        """Catch unbounded reads, ambient proxy state and unsafe final URLs."""
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.geturl.return_value = ETF.source_url
        response.getheader.return_value = None
        response.getcode.return_value = 200
        response.read.return_value = b"source bytes"
        handlers = []

        def make_opener(*values: Any) -> MagicMock:
            """Inspect the actual installed urllib request policy."""
            handlers.extend(values)
            return opener

        with patch.object(publish, "build_opener", side_effect=make_opener):
            self.assertEqual(publish.fetch_official(ETF), b"source bytes")
            self.assertEqual(handlers[0].proxies, {})
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)
            self.assertEqual(response.read.call_args.args, (10485761,))
            response.read.return_value = b"x" * 10485761
            with self.assertRaisesRegex(ValueError, "source_too_large"):
                publish.fetch_official(ETF)
            response.geturl.return_value = "https://evil.test/a"
            with self.assertRaises(ValueError):
                publish.fetch_official(ETF)
