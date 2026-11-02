"""Replay the failed official capture without network or private data paths."""

from datetime import date
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support  # noqa: F401
from lookthrough import native_daily, publish
from lookthrough.models import snapshot_from_dict
from lookthrough.refresh_contract import directory


class AllianzDailyIdentityTests(unittest.TestCase):
    """Keep valid official full names intact through daily qualification."""

    def test_actual_capture_passes_the_real_publisher(self) -> None:
        """Replay the exact failed official bytes without changing holdings."""
        path = Path(__file__).parent / (
            'fixtures/issuers/allianz/00984A.2026-11-02.raw.gz')
        raw = gzip.decompress(path.read_bytes())
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            'da38d4b01332c22ab5cd09d175667a8cf94e596e3a0718a3260f307545512692')
        with (
            tempfile.TemporaryDirectory() as folder,
            patch('socket.socket.connect',
                  side_effect=AssertionError('offline only')),
        ):
            root = Path(folder) / 'holdings'
            result = publish.publish_all(
                root, fetch=lambda etf: raw, today=date(2026, 11, 2),
                fetched_at='2026-11-02T10:31:31.837897+00:00',
                directory=directory(), symbols={'00984A'})
            row = next(r for r in result['etfs'] if r['symbol'] == '00984A')
            self.assertEqual(row['last_status'], 'warn')
            self.assertEqual(row['latest_date'], '2026-11-02')
            snapshot = snapshot_from_dict(json.loads(
                (root / '00984A/latest.json').read_bytes()))
            self.assertEqual(len([h for h in snapshot.holdings
                                  if h.kind == 'stock']), 81)
            self.assertEqual(
                [(h.code, h.name, h.weight, h.market)
                 for h in snapshot.holdings if h.code == '3044'],
                [('3044', '健鼎科技', 0.0065, 'TWSE')])
            self.assertTrue(native_daily.qualifies(snapshot))
