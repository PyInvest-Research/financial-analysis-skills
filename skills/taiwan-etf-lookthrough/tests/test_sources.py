"""Verify snapshot acquisition using injected offline transports."""

from dataclasses import replace
from datetime import date, datetime
from http.client import BadStatusLine
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

from support import make_snapshot
from lookthrough.models import snapshot_sha256, snapshot_to_dict
from lookthrough.sources import (
    PYINVEST_BASE_URL, SourceUnavailable, _PyInvestRedirect,
    fetch_index, fetch_pyinvest_snapshot, load_local_snapshots,
)


class SourcesTests(unittest.TestCase):
    """Exercise decoding, identities, conflicts and network boundaries."""

    def setUp(self) -> None:
        """Create deterministic synthetic serialized holdings."""
        self.snapshot = make_snapshot()
        self.raw = json.dumps(snapshot_to_dict(self.snapshot)).encode()
        self.urls: list[str] = []

    def opener(self, url: str) -> bytes:
        """Record the requested URL and return synthetic bytes."""
        self.urls.append(url)
        return self.raw

    def test_latest_and_dated_preserve_provenance(self) -> None:
        """Fetch known paths and never follow the source_url metadata."""
        actual = fetch_pyinvest_snapshot(' etfa ', opener=self.opener)
        self.assertEqual(snapshot_sha256(actual),
                         snapshot_sha256(self.snapshot))
        self.assertEqual(self.urls, [f'{PYINVEST_BASE_URL}/ETFA/latest.json'])
        actual = fetch_pyinvest_snapshot(
            'ETFA', date(2026, 10, 22), base_url='https://fake.test/data/',
            opener=self.opener,
        )
        self.assertEqual(actual, self.snapshot)
        self.assertEqual(self.urls[-1],
                         'https://fake.test/data/ETFA/2026-10-22.json')

    def test_index(self) -> None:
        """Accept an index object without following its URLs."""
        self.assertEqual(fetch_index(opener=lambda url: b'{"etfs": []}'),
                         {'etfs': []})
        with self.assertRaisesRegex(SourceUnavailable, 'object'):
            fetch_index(opener=lambda url: b'[]')

    def test_failures_preserve_cause(self) -> None:
        """Wrap HTTP, timeout, decoding, JSON and schema failures."""
        for error in [HTTPError('https://fake', 404, 'Not Found', {}, None),
                      TimeoutError('request timed out'),
                      BadStatusLine('Malformed HTTP status')]:
            with self.subTest(error=error), self.assertRaisesRegex(
                    SourceUnavailable, str(error)):
                fetch_pyinvest_snapshot(
                    'ETFA', opener=MagicMock(side_effect=error),
                )
        for raw, message in [(b'bad', 'Expecting value'),
                             (b'\xff', 'codec'),
                             (b'{}', 'schema_version'),
                             (b'[]', 'object')]:
            with self.subTest(raw=raw), self.assertRaisesRegex(
                    SourceUnavailable, message):
                fetch_pyinvest_snapshot('ETFA', opener=lambda url: raw)

    def test_mismatched_identity(self) -> None:
        """Reject wrong symbols and wrong requested dates."""
        with self.assertRaisesRegex(SourceUnavailable, 'symbol'):
            fetch_pyinvest_snapshot('ETFB', opener=self.opener)
        with self.assertRaisesRegex(SourceUnavailable, 'date'):
            fetch_pyinvest_snapshot('ETFA', date(2026, 10, 21),
                                    opener=self.opener)

    def test_invalid_paths_never_call_opener(self) -> None:
        """Reject path injection, credentials and non-calendar dates."""
        fake = MagicMock(return_value=self.raw)
        for symbol in ['../ETFA', 'ETFA/x', 'ETFA?x', 'a@host', '50',
                       'https://evil.test', 'ETFA%2f', 'ETFA\\x', None]:
            with self.subTest(symbol=symbol), self.assertRaises(
                    SourceUnavailable):
                fetch_pyinvest_snapshot(symbol, opener=fake)
        for invalid in ['2026-10-22', datetime(2026, 10, 22)]:
            with self.assertRaises(SourceUnavailable):
                fetch_pyinvest_snapshot('ETFA', invalid, opener=fake)
        fake.assert_not_called()

    def test_default_base_restriction(self) -> None:
        """Reject untrusted bases before constructing transport."""
        for base in ['https://evil.test/data',
                     'http://pyecontech.com/data/etf-holdings/v1',
                     'https://user@pyecontech.com/data/etf-holdings/v1',
                     PYINVEST_BASE_URL + '/../v2',
                     PYINVEST_BASE_URL + '?x=1']:
            with self.subTest(base=base), patch(
                    'lookthrough.sources.build_opener') as builder:
                with self.assertRaises(SourceUnavailable):
                    fetch_index(base_url=base)
                builder.assert_not_called()

    def test_default_timeout(self) -> None:
        """Keep default verified TLS and a twenty-second timeout."""
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{}'
        response.geturl.return_value = PYINVEST_BASE_URL + '/index.json'
        with patch('lookthrough.sources.build_opener') as builder:
            builder.return_value.open.return_value = response
            self.assertEqual(fetch_index(), {})
            self.assertEqual(builder.return_value.open.call_args.kwargs,
                             {'timeout': 20})
            self.assertIsInstance(builder.call_args.args[0],
                                  _PyInvestRedirect)

    def test_final_response_url_is_checked(self) -> None:
        """Check final destinations even when a transport hides redirects."""
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = 'https://evil.test/index.json'
        with patch('lookthrough.sources.build_opener') as builder:
            builder.return_value.open.return_value = response
            with self.assertRaisesRegex(SourceUnavailable, 'approved'):
                fetch_index()
            response.read.assert_not_called()

    def test_published_source_kind_preserves_hash(self) -> None:
        """Do not rewrite the publisher's serialized source kind."""
        published = replace(self.snapshot, source_kind='pyinvest')
        self.raw = json.dumps(snapshot_to_dict(published)).encode()
        actual = fetch_pyinvest_snapshot('ETFA', opener=self.opener)
        self.assertEqual(snapshot_sha256(actual), snapshot_sha256(published))

    def test_redirect_guard(self) -> None:
        """Block hostile destinations before the redirected request."""
        handler = _PyInvestRedirect()
        req = Request(PYINVEST_BASE_URL + '/index.json')
        for url in ['https://evil.test/data',
                    'http://pyecontech.com/data/etf-holdings/v1/index.json',
                    PYINVEST_BASE_URL + '/../secret',
                    'https://u:p@pyecontech.com/data/etf-holdings/v1/x']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(req, None, 302, 'Found', {}, url)
        allowed = handler.redirect_request(
            req, None, 302, 'Found', {},
            PYINVEST_BASE_URL + '/0050/latest.json',
        )
        self.assertEqual(allowed.full_url,
                         PYINVEST_BASE_URL + '/0050/latest.json')

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
