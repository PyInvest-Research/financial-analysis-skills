"""Protect immutable acquisition receipts across retained publication data."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lookthrough import publish
from lookthrough.directory import CompanyDirectory
from test_publish import ETF, FIXTURES, STAMP, LATER


class ArchiveReceiptTests(unittest.TestCase):
    """Exercise real parsers, file retention and explicit raw export."""

    def setUp(self) -> None:
        """Allocate independent paths and a single reviewed issuer route."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.out = self.base / "published"
        self.raw = (FIXTURES / "0052.2026-10-22.html").read_bytes()
        self.directory = CompanyDirectory([], "2026-10-23")
        registry = patch.object(publish, "SUPPORTED_ETFS", (ETF,))
        registry.start()
        self.addCleanup(registry.stop)

    def collect(self, stamp: str = STAMP, fetch=None, **kwargs) -> list[dict]:
        """Run the publisher and return its immutable per-attempt ledger."""
        attempts: list[dict] = []
        publish._publish_all(
            self.out, fetch=fetch or (lambda _: self.raw),
            today=date(2026, 10, 24), fetched_at=stamp,
            directory=self.directory, write=True, observations=[],
            attempt_sink=attempts.append, **kwargs)
        return attempts

    def test_raw_sink_runs_before_parser_failure(self) -> None:
        """Preserve original bytes even when parsing cannot produce rows."""
        seen: dict[str, bytes] = {}
        attempts = self.collect(fetch=lambda _: b"invalid holdings",
                                raw_sink=lambda s, b: seen.update({s: b}))
        self.assertEqual(seen, {"0052": b"invalid holdings"})
        self.assertEqual(attempts[0]["status"], "fail")
        self.assertIsNone(attempts[0]["content_sha256"])

    def test_fetch_failure_records_attempt_without_raw(self) -> None:
        """Do not promote the previous snapshot after acquisition failure."""
        self.collect()
        seen: list[bytes] = []

        def fail(_):
            raise OSError("synthetic offline failure")

        attempts = self.collect(LATER, fetch=fail,
                                raw_sink=lambda _, raw: seen.append(raw))
        self.assertEqual(seen, [])
        self.assertEqual(attempts[0]["status"], "fetch_error")
        self.assertIsNone(attempts[0]["holdings_date"])
        self.assertIsNone(attempts[0]["raw_sha256"])
        self.assertIn("synthetic offline failure", attempts[0]["last_error"])

    def test_attempt_hash_recorded_when_content_unchanged(self) -> None:
        """Keep the original snapshot timestamp on reconfirmation."""
        first = self.collect()
        path = self.out / "0052/2026-10-22.json"
        original = path.read_bytes()
        second = self.collect(LATER)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(json.loads(original)["fetched_at"], STAMP)
        self.assertEqual(first, second)
        self.assertEqual(second[0]["holdings_date"], "2026-10-22")
        self.assertEqual(len(second[0]["content_sha256"]), 64)

    def test_raw_sink_failure_is_fatal(self) -> None:
        """Never claim archival completeness after failing to save bytes."""
        def fail(*_):
            raise OSError("synthetic disk failure")

        with self.assertRaisesRegex(OSError, "disk failure"):
            self.collect(raw_sink=fail)

    def cli(self, *args: str) -> int:
        """Run the actual CLI with an offline issuer boundary."""
        with patch.object(publish, "fetch_official", lambda _: self.raw), \
                redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()):
            return publish.main(["--out", str(self.out), *args])

    def test_cli_raw_manifest_lists_attempts(self) -> None:
        """Export bytes and attempts through the supported CLI contract."""
        raw_dir = self.base / "raw"
        self.assertEqual(self.cli("--raw-dir", str(raw_dir)), 0)
        receipt = json.loads((raw_dir / "raw-manifest.json").read_bytes())
        self.assertEqual([r["symbol"] for r in receipt["attempts"]], ["0052"])
        self.assertEqual((raw_dir / "0052.raw").read_bytes(), self.raw)
        self.assertEqual(receipt["files"][0]["sha256"],
                         receipt["attempts"][0]["raw_sha256"])

    def test_raw_dir_rejects_existing_or_overlapping_paths(self) -> None:
        """Reject unsafe destinations before writing publication files."""
        for target in (self.out, self.out / "raw", self.base):
            self.assertEqual(self.cli("--raw-dir", str(target)), 1)
            self.assertFalse(self.out.exists())

    def test_dry_run_exports_only_explicit_raw_evidence(self) -> None:
        """Export explicit evidence without publishing data."""
        raw_dir = self.base / "raw"
        self.assertEqual(self.cli("--raw-dir", str(raw_dir), "--dry-run"), 0)
        self.assertFalse(self.out.exists())
        self.assertTrue((raw_dir / "raw-manifest.json").is_file())
