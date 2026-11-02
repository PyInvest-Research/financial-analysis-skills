"""Exercise hash-bound offline delivery and real report fallback behavior."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support  # noqa: F401
from bundle_support import write_bundle
from lookthrough.run import run_lookthrough
from lookthrough.sources import SourceUnavailable
try:
    from lookthrough import bundled
except ImportError:
    bundled = None


FIX = Path(__file__).parent / 'fixtures/cross_path'


class BundledTests(unittest.TestCase):
    """Require local authenticated content without any HTTP fallback."""

    def setUp(self) -> None:
        """Keep test files isolated and source networking forbidden."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.payload = json.loads((FIX / 'pyinvest/ETFA.json').read_bytes())
        self.payload['etf_symbol'] = '0050'
        self.bundle = write_bundle(self.root / 'bundle', [self.payload])
        guard = patch('socket.socket.connect',
                      side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_load_keeps_source_and_original_values(self) -> None:
        """Delivery metadata must never rewrite source content or weights."""
        self.assertIsNotNone(bundled)
        snapshot = bundled.load_bundled('0050', self.bundle)
        self.assertEqual(snapshot.source_kind, self.payload['source_kind'])
        self.assertEqual(snapshot.holdings[0].weight,
                         self.payload['holdings'][0]['weight'])
        self.assertEqual(bundled.bundled_index(self.bundle),
                         {'0050': self.payload['holdings_date']})

    def test_payload_hash_or_identity_mismatch_is_rejected(self) -> None:
        """Editing an indexed payload cannot become accepted bundle data."""
        self.assertIsNotNone(bundled)
        for change in ({'issuer': 'changed'}, {'etf_symbol': '0056'},
                       {'holdings_date': '2026-10-21'}):
            with self.subTest(change=change):
                changed = {**self.payload, **change}
                (self.bundle / '0050.json').write_text(json.dumps(changed))
                with self.assertRaises(SourceUnavailable):
                    bundled.load_bundled('0050', self.bundle)

    def test_malformed_unrequested_row_does_not_disable_healthy_etf(
        self,
    ) -> None:
        """A broken unrelated index row cannot remove a usable position."""
        self.assertIsNotNone(bundled)
        path = self.bundle / 'index.json'
        value = json.loads(path.read_bytes())
        value['etfs'].append({'symbol': 'UNUSED', 'holdings_date': 'invalid'})
        path.write_text(json.dumps(value))
        self.assertIsNotNone(bundled.load_bundled('0050', self.bundle))

    def test_unsafe_symbol_duplicate_index_and_symlink_are_rejected(
        self,
    ) -> None:
        """The index cannot select paths outside its managed directory."""
        self.assertIsNotNone(bundled)
        with self.assertRaises(SourceUnavailable):
            bundled.load_bundled('../0050', self.bundle)
        index = json.loads((self.bundle / 'index.json').read_bytes())
        index['etfs'] *= 2
        (self.bundle / 'index.json').write_text(json.dumps(index))
        with self.assertRaises(SourceUnavailable):
            bundled.load_bundled('0050', self.bundle)
        write_bundle(self.bundle, [self.payload])
        (self.bundle / '0050.json').unlink()
        target = self.root / 'outside.json'
        target.write_text(json.dumps(self.payload))
        (self.bundle / '0050.json').symlink_to(target)
        with self.assertRaises(SourceUnavailable):
            bundled.load_bundled('0050', self.bundle)

    def report(self, today: date, **options: object) -> dict:
        """Run the actual portfolio pipeline and inspect its output JSON."""
        self.assertIsNotNone(bundled)
        portfolio = self.root / 'portfolio.json'
        portfolio.write_text(json.dumps({
            'currency': 'TWD', 'valuation_date': today.isoformat(),
            'positions': [{'symbol': '0050', 'kind': 'etf',
                           'market_value': 10000}],
        }))
        paths = run_lookthrough(
            portfolio, self.root / 'out', today=today,
            bundled_dir=self.bundle, **options)
        return json.loads(paths['result.json'].read_bytes())

    def test_current_bundle_works_in_both_network_modes(self) -> None:
        """Network opt-in cannot turn bundle loading into an HTTP request."""
        for mode in (False, True):
            with self.subTest(mode=mode):
                result = self.report(date(2026, 10, 23), use_network=mode)
                row = result['snapshots'][0]
                self.assertEqual(row['delivery'], 'bundled')
                self.assertEqual(row['holdings_date'], '2026-10-22')
                self.assertIsNotNone(row['archive_content_sha256'])
                self.assertEqual(result['positions'][0]['snapshot'], row)
                self.assertNotIn('pyecontech.com', str(result['warnings']))

    def test_stale_bundle_warns_with_original_date_and_age(self) -> None:
        """A stale bundle remains dated and cannot look like a fresh fetch."""
        result = self.report(date(2026, 12, 1))
        warnings = ' '.join(result['warnings'])
        self.assertIn('內建快照', warnings)
        self.assertIn('2026-10-22', warnings)
        self.assertIn('40', warnings)

    def test_corrupt_bundle_falls_through_without_losing_denominator(
        self,
    ) -> None:
        """One broken payload must become unavailable rather than abort."""
        changed = deepcopy(self.payload)
        changed['holdings'][0]['weight'] = 0.01
        (self.bundle / '0050.json').write_text(json.dumps(changed))
        result = self.report(date(2026, 10, 23))
        self.assertIsNone(result['snapshots'][0]['holdings_date'])
        self.assertEqual(result['totals']['total_value'], 10000)

    def test_official_file_precedes_stale_bundle(self) -> None:
        """A fresh explicit official file must replace a stale bundle."""
        from lookthrough.loader import ColumnMap
        result = self.report(date(2026, 12, 1), official_files={'0050': (
            FIX / 'official.csv',
            ColumnMap('code', 'name', 'weight', kind='kind'),
            {'etf_symbol': '0050', 'issuer': 'Synthetic Issuer',
             'is_active': False, 'holdings_date': date(2026, 12, 1),
             'source_url': 'https://example.com/synthetic/official',
             'fetched_at': '2026-12-01T00:00:00+00:00'})})
        self.assertEqual(result['snapshots'][0]['holdings_date'], '2026-12-01')
        self.assertNotEqual(result['snapshots'][0].get('delivery'), 'bundled')

    def test_explicit_local_snapshot_precedes_bundle(self) -> None:
        """A caller's explicit local source remains the first choice."""
        local = self.root / 'local'
        local.mkdir()
        changed = {**self.payload, 'holdings_date': '2026-10-21'}
        (local / '0050.json').write_text(json.dumps(changed))
        result = self.report(date(2026, 10, 23), local_snapshot_dir=local)
        self.assertEqual(result['snapshots'][0]['holdings_date'], '2026-10-21')
        self.assertNotEqual(result['snapshots'][0].get('delivery'), 'bundled')

    def test_older_equivalent_official_file_cannot_downgrade_bundle(self):
        """Within equal freshness and completeness choose the newer date."""
        from lookthrough.loader import ColumnMap
        result = self.report(date(2026, 12, 1), official_files={'0050': (
            FIX / 'official.csv',
            ColumnMap('code', 'name', 'weight', kind='kind'),
            {'etf_symbol': '0050', 'issuer': 'Synthetic Issuer',
             'is_active': False, 'holdings_date': date(2026, 10, 21),
             'source_url': 'https://example.com/synthetic/official',
             'fetched_at': '2026-12-01T00:00:00+00:00'})})
        self.assertEqual(result['snapshots'][0]['holdings_date'], '2026-10-22')
        self.assertEqual(result['snapshots'][0]['delivery'], 'bundled')

    def test_content_digest_uses_verified_raw_optional_field_spelling(self):
        """A valid omitted null must not change the archive identity."""
        for holding in self.payload['holdings']:
            holding.pop('exposure_weight', None)
        write_bundle(self.bundle, [self.payload])
        expected = json.loads((self.bundle / 'index.json').read_bytes())[
            'etfs'][0]['snapshot_sha256']
        result = self.report(date(2026, 10, 23))
        self.assertEqual(result['snapshots'][0]['archive_content_sha256'],
                         expected)

    def test_partial_refresh_inventory_can_upgrade_to_official_file(self):
        """Synthetic route-shaped partial data must read a fuller file."""
        from lookthrough.loader import ColumnMap, load_official_file
        payload = deepcopy(self.payload)
        payload.update(schema_version='2', etf_symbol='00901',
                       issuer='sinopac', source_kind='official',
                       source_url='https://sitc.sinopac.com/SinopacEtfs/'
                       'Etfs/SinglePcf/00901', equity_evidence={
                           'complete': False, 'stock_rows': 2,
                           'source_hashes': ['a' * 64],
                           'total_nav_twd': 100000,
                           'stock_value_twd': 95000,
                           'taiwan_stock_value_twd': 95000})
        for holding in payload['holdings']:
            if holding['kind'] == 'stock':
                holding['market'] = 'TWSE'
        write_bundle(self.bundle, [payload])
        portfolio = self.root / 'partial-portfolio.json'
        portfolio.write_text(json.dumps({
            'currency': 'TWD', 'valuation_date': '2026-10-23',
            'positions': [{'symbol': '00901', 'kind': 'etf',
                           'market_value': 10000}],
        }))
        with patch('lookthrough.run.load_official_file',
                   wraps=load_official_file) as loader:
            paths = run_lookthrough(
                portfolio, self.root / 'partial-out', today=date(2026, 10, 23),
                bundled_dir=self.bundle, official_files={'00901': (
                    FIX / 'official.csv',
                    ColumnMap('code', 'name', 'weight', kind='kind'),
                    {'etf_symbol': '00901', 'issuer': 'Synthetic Issuer',
                     'is_active': False, 'holdings_date': date(2026, 10, 23),
                     'source_url': 'https://example.com/synthetic/official',
                     'fetched_at': '2026-10-23T00:00:00+00:00'})})
            self.assertEqual(loader.call_count, 1)
        result = json.loads(paths['result.json'].read_bytes())
        self.assertNotEqual(result['snapshots'][0].get('delivery'), 'bundled')
        self.assertEqual(result['snapshots'][0]['holdings_date'], '2026-10-23')
