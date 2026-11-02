"""Verify local snapshot decoding, deduplication and per-file failures."""

from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from support import make_snapshot
from lookthrough.models import snapshot_to_dict
from lookthrough.sources import SourceUnavailable, load_local_snapshots


class SourcesTests(unittest.TestCase):
    """Preserve the local-file contract after website transport removal."""

    def setUp(self) -> None:
        """Create source-shaped synthetic local bytes."""
        self.snapshot = make_snapshot()
        self.raw = json.dumps(snapshot_to_dict(self.snapshot)).encode()

    def test_local_dates_and_companions(self) -> None:
        """Read flat snapshots, skipping known demo companion objects."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'a.json').write_bytes(self.raw)
            older = replace(self.snapshot, holdings_date=date(2026, 10, 20))
            (root / 'z.json').write_text(json.dumps(snapshot_to_dict(older)))
            (root / 'portfolio.json').write_text('{"positions": []}')
            (root / 'expected-result.json').write_text('{"metrics": {}}')
            self.assertEqual(load_local_snapshots(root),
                             {'ETFA': [older, self.snapshot]})

    def test_local_duplicates(self) -> None:
        """Coalesce exact copies; reject contradictory same-date data."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'a.json').write_bytes(self.raw)
            (root / 'b.json').write_bytes(self.raw)
            self.assertEqual(load_local_snapshots(root),
                             {'ETFA': [self.snapshot]})
            changed = replace(self.snapshot, issuer='Conflicting Issuer')
            (root / 'b.json').write_text(json.dumps(snapshot_to_dict(changed)))
            with self.assertRaisesRegex(SourceUnavailable, 'Conflicting'):
                load_local_snapshots(root)

    def test_local_corruption_and_missing_directory(self) -> None:
        """Never swallow damaged holdings, even under companion filenames."""
        for name, raw in [('holdings.json', b'{'),
                          ('holdings.json', b'{}'),
                          ('portfolio.json', b'{"holdings": []}')]:
            with self.subTest(name=name, raw=raw), \
                    tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / name).write_bytes(raw)
                with self.assertRaisesRegex(SourceUnavailable, name):
                    load_local_snapshots(root)
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(load_local_snapshots(Path(folder)), {})
            with self.assertRaises(SourceUnavailable):
                load_local_snapshots(Path(folder) / 'missing')
