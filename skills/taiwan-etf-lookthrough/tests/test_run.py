"""Test orchestration using only synthetic files and injected transports."""

from copy import deepcopy
from functools import partial
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

import support  # noqa: F401
from lookthrough.loader import ColumnMap
from lookthrough.run import run_lookthrough as run_pipeline
from lookthrough.sources import (
    SourceUnavailable, load_local_snapshot, load_local_snapshots,
)


# Preserve the original strict acquisition contract for synthetic tickers.
run_lookthrough = partial(run_pipeline, validation_policy="strict")


FIX = Path(__file__).resolve().parent / 'fixtures' / 'cross_path'
CMAP = ColumnMap('code', 'name', 'weight', kind='kind')
META = {
    'etf_symbol': 'ETFA', 'issuer': 'Synthetic Issuer', 'is_active': False,
    'holdings_date': date(2026, 10, 22),
    'source_url': 'https://example.com/synthetic/official',
    'fetched_at': '2026-10-23T00:00:00+00:00',
}
TODAY = date(2026, 10, 23)


def strip_provenance(value: Any) -> Any:
    """Remove only acquisition metadata recursively, retaining all numbers."""
    excluded = {'generated_at', 'source_kind', 'source_url',
                'snapshot_sha256', 'fetched_at'}
    if isinstance(value, dict):
        return {key: strip_provenance(item) for key, item in value.items()
                if key not in excluded}
    if isinstance(value, list):
        return [strip_provenance(item) for item in value]
    return value


class RunTests(unittest.TestCase):
    """Cover source order, usable dates, identity and output integration."""

    def setUp(self) -> None:
        """Create private output paths and reset synthetic transport state."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = json.loads((FIX / 'pyinvest/ETFA.json').read_text())
        self.urls: list[str] = []
        self.remote: dict[str, Any] = {}
        self.index: Any = {'schema_version': '1', 'etfs': []}

    def opener(self, url: str) -> bytes:
        """Serve only explicit synthetic responses, rejecting unknown URLs."""
        self.urls.append(url)
        if url.endswith('/index.json'):
            return json.dumps(self.index).encode()
        key = '/'.join(url.split('/')[-2:])
        if key not in self.remote:
            raise OSError('Synthetic source unavailable')
        value = self.remote[key]
        if isinstance(value, bytes):
            return value
        return json.dumps(value).encode()

    def run_case(self, **kwargs: Any) -> dict[str, Any]:
        """Run and read all outputs, defaulting to an entirely offline run."""
        options = {'today': TODAY, 'use_network': False, **kwargs}
        portfolio_path = options.pop('portfolio_path', FIX / 'portfolio.json')
        paths = run_lookthrough(portfolio_path, self.root / 'out',
                                **options)
        self.assertEqual(set(paths), {'result.json', 'report.md',
                                      'exposures.csv'})
        self.assertTrue(paths['report.md'].read_text().startswith('# ETF'))
        self.assertTrue(paths['exposures.csv'].read_bytes().startswith(
            b'\xef\xbb\xbf'))
        result = json.loads(paths['result.json'].read_text())
        stamp = datetime.fromisoformat(result['generated_at'])
        self.assertEqual(stamp.utcoffset(), timedelta(0))
        return result

    def write_local(self, *payloads: Any) -> Path:
        """Write flat candidate files without modifying shared fixtures."""
        folder = self.root / 'local'
        folder.mkdir(exist_ok=True)
        for number, payload in enumerate(payloads):
            (folder / f'{number}.json').write_text(json.dumps(payload))
        return folder

    def official(self, **metadata: Any) -> dict:
        """Return one explicit synthetic official-file configuration."""
        return {'ETFA': (FIX / 'official.csv', CMAP, {**META, **metadata})}

    def two_etfs(self) -> Path:
        """Create two ETF positions for date-alignment tests."""
        portfolio = json.loads((FIX / 'portfolio.json').read_text())
        portfolio['positions'].append({'symbol': 'ETFB', 'kind': 'etf',
                                       'market_value': 100000})
        path = self.root / 'portfolio.json'
        path.write_text(json.dumps(portfolio))
        return path

    def test_cross_path_results_identical(self) -> None:
        """Compare full recursive results, excluding provenance alone."""
        local = self.run_case(local_snapshot_dir=FIX / 'pyinvest')
        official = self.run_case(official_files=self.official())
        self.assertEqual(strip_provenance(local), strip_provenance(official))
        self.assertEqual(local['totals']['identified_stock_value'], 95000)
        self.assertEqual(local['snapshots'][0]['source_kind'], 'pyinvest')

    def test_signed_cash_other_survive_pipeline_and_reports(self) -> None:
        """Keep signed synthetic buckets and the stock denominator."""
        baseline = self.run_case(local_snapshot_dir=FIX / 'pyinvest')
        payload = deepcopy(self.payload)
        payload['holdings'][-1]['weight'] = -.02
        payload['holdings'].append({
            'code': 'PAYABLE', 'name': 'Synthetic payable', 'kind': 'other',
            'weight': -.03, 'exposure_weight': None,
        })
        folder = self.write_local(payload)
        result = self.run_case(local_snapshot_dir=folder)
        self.assertEqual(result['positions'][0]['status'], 'looked_through')
        self.assertEqual(result['snapshots'][0]['validation_status'], 'pass')
        totals = result['totals']
        self.assertEqual(totals['cash_value'], -2000)
        self.assertEqual(totals['other_value'], -3000)
        self.assertEqual(totals['unclassified_gap_value'], 10000)
        self.assertEqual(totals['identified_stock_value'], 95000)
        self.assertEqual(totals['coverage'], .95)
        self.assertEqual(sum(totals[key] for key in (
            'identified_stock_value', 'unresolved_value', 'cash_value',
            'other_value', 'unclassified_gap_value',
            'not_looked_through_value')), totals['total_value'])
        self.assertEqual(result['companies'], baseline['companies'])
        self.assertEqual(result['industries'], baseline['industries'])
        self.assertEqual(result['metrics'], baseline['metrics'])
        self.assertEqual(result['limitations'], baseline['limitations'])
        report = (self.root / 'out/report.md').read_text()
        self.assertIn('| 現金 | -2,000.00 | -2.00% |', report)
        self.assertIn('| 其他 | -3,000.00 | -3.00% |', report)
        self.assertIn('| 未分類差額 | 10,000.00 | 10.00% |', report)
        for sentence in result['limitations']:
            self.assertIn(sentence, report)
        self.assertEqual(json.loads((folder / '0.json').read_text()), payload)

    def test_falls_back_when_pyinvest_unavailable(self) -> None:
        """Keep transport failures visible after an official fallback."""
        result = self.run_case(use_network=True, opener=self.opener,
                               official_files=self.official())
        self.assertEqual(result['positions'][0]['status'], 'looked_through')
        self.assertEqual(result['snapshots'][0]['source_kind'],
                         'agent_official')
        self.assertIn('PyInvest', ' '.join(result['warnings']))

    def test_offline_mode_never_calls_opener(self) -> None:
        """Neither opener nor index fetch is consulted in offline mode."""
        opener = MagicMock(side_effect=AssertionError('network forbidden'))
        with patch('lookthrough.run.fetch_index') as index:
            result = self.run_case(local_snapshot_dir=FIX / 'pyinvest',
                                   opener=opener)
        opener.assert_not_called()
        index.assert_not_called()
        self.assertEqual(result['totals']['coverage'], .95)

    def test_local_priority_avoids_all_network(self) -> None:
        """A usable local source wins even when network mode is enabled."""
        opener = MagicMock(side_effect=AssertionError('network forbidden'))
        self.run_case(local_snapshot_dir=FIX / 'pyinvest',
                      use_network=True, opener=opener,
                      official_files=self.official(etf_symbol='ETFB'))
        opener.assert_not_called()

    def annotated_portfolio(self) -> Path:
        """Keep an actual ETF identifier in annotated integration tests."""
        self.payload['etf_symbol'] = '0050'
        payload = json.loads((FIX / 'portfolio.json').read_text())
        payload['positions'][0]['symbol'] = '0050'
        path = self.root / 'annotated-portfolio.json'
        path.write_text(json.dumps(payload))
        return path

    def test_partial_local_upgrades_from_published_holdings(self) -> None:
        """Recent partial rows must not suppress fuller published sources."""
        portfolio = self.annotated_portfolio()
        local = deepcopy(self.payload)
        local['holdings'] = local['holdings'][:1]
        folder = self.write_local(local)
        for day in ['2026-10-21', '2026-10-22', '2026-10-23']:
            with self.subTest(day=day):
                self.index['etfs'] = [{'symbol': '0050', 'dates': [day]}]
                self.remote[f'0050/{day}.json'] = {
                    **self.payload, 'holdings_date': day}
                result = self.run_case(
                    validation_policy='annotated', portfolio_path=portfolio,
                    local_snapshot_dir=folder,
                    use_network=True, opener=self.opener)
                self.assertEqual(result['totals']['coverage'], .95)
                self.assertEqual(result['snapshots'][0]['holdings_date'], day)
        self.assertEqual(json.loads((folder / '0.json').read_text()), local)

    def test_partial_local_survives_remote_failure(self) -> None:
        """Failed acquisition retains known rows, denominator and reason."""
        portfolio = self.annotated_portfolio()
        local = deepcopy(self.payload)
        local['holdings'] = local['holdings'][:1]
        folder = self.write_local(local)
        result = self.run_case(
            validation_policy='annotated', portfolio_path=portfolio,
            local_snapshot_dir=folder,
            use_network=True, opener=self.opener)
        self.assertTrue(self.urls)
        self.assertEqual(result['totals']['coverage'], .6)
        self.assertEqual(result['totals']['total_value'], 100000)
        self.assertIn('no available dates', ' '.join(result['warnings']))

    def test_complete_annotated_local_avoids_network(self) -> None:
        """Complete usable local holdings preserve the inexpensive path."""
        portfolio = self.annotated_portfolio()
        folder = self.write_local(self.payload)
        opener = MagicMock(side_effect=AssertionError('network forbidden'))
        self.run_case(validation_policy='annotated', portfolio_path=portfolio,
                      local_snapshot_dir=folder,
                      use_network=True, opener=opener)
        opener.assert_not_called()

    def test_current_partial_precedes_stale_complete_remote(self) -> None:
        """Completeness does not silently reverse the documented date tier."""
        portfolio = self.annotated_portfolio()
        local = deepcopy(self.payload)
        local['holdings'] = local['holdings'][:1]
        folder = self.write_local(local)
        day = '2026-09-01'
        self.index['etfs'] = [{'symbol': '0050', 'dates': [day]}]
        self.remote[f'0050/{day}.json'] = {
            **self.payload, 'holdings_date': day}
        result = self.run_case(
            validation_policy='annotated', portfolio_path=portfolio,
            local_snapshot_dir=folder,
            use_network=True, opener=self.opener)
        self.assertTrue(any(url.endswith(f'/{day}.json') for url in self.urls))
        self.assertEqual(result['totals']['coverage'], .6)

    def test_invalid_local_falls_through(self) -> None:
        """Malformed, invalid-weight and wrong-ETF local files never win."""
        bad_weight = deepcopy(self.payload)
        bad_weight['holdings'][0]['weight'] = 1.1
        wrong_symbol = {**self.payload, 'etf_symbol': 'ETFB'}
        for payload in [{}, bad_weight, wrong_symbol]:
            with self.subTest(payload=payload):
                folder = self.write_local(payload)
                result = self.run_case(local_snapshot_dir=folder,
                                       official_files=self.official())
                self.assertEqual(result['snapshots'][0]['source_kind'],
                                 'agent_official')
                self.assertTrue(result['warnings'])

    def test_good_local_survives_bad_candidate(self) -> None:
        """A malformed companion and invalid newer date cannot hide local."""
        invalid = deepcopy(self.payload)
        invalid['holdings_date'] = '2026-10-23'
        invalid['holdings'][0]['weight'] = 1.1
        folder = self.write_local({}, invalid, self.payload)
        result = self.run_case(local_snapshot_dir=folder,
                               official_files=self.official())
        self.assertEqual(result['snapshots'][0]['source_kind'], 'pyinvest')
        self.assertEqual(result['snapshots'][0]['holdings_date'], '2026-10-22')
        self.assertTrue(result['warnings'])

    def test_invalid_remote_falls_through(self) -> None:
        """Remote schema, financial and requested identity errors fall on."""
        bad_weight = deepcopy(self.payload)
        bad_weight['holdings'][0]['weight'] = 1.1
        values = [{}, bad_weight, {**self.payload, 'etf_symbol': 'ETFB'},
                  {**self.payload, 'holdings_date': '2026-10-21'}]
        self.index['etfs'] = [{'symbol': 'ETFA', 'dates': ['2026-10-22']}]
        for payload in values:
            with self.subTest(payload=payload):
                self.remote = {'ETFA/2026-10-22.json': payload}
                result = self.run_case(use_network=True, opener=self.opener,
                                       official_files=self.official())
                self.assertEqual(result['snapshots'][0]['source_kind'],
                                 'agent_official')
                self.assertIn('PyInvest', ' '.join(result['warnings']))

    def test_invalid_index_entries_are_not_followed(self) -> None:
        """Reject malformed relevant dates without constructing their URLs."""
        for dates in [['../secret'], ['2026-02-30'], '2026-10-22', [None]]:
            with self.subTest(dates=dates):
                self.urls.clear()
                self.index['etfs'] = [{'symbol': 'ETFA', 'dates': dates}]
                result = self.run_case(use_network=True, opener=self.opener,
                                       official_files=self.official())
                self.assertEqual(len(self.urls), 1)
                self.assertEqual(result['totals']['coverage'], .95)
                self.assertIn('index', ' '.join(result['warnings']))

    def test_wrong_official_identity_and_invalid_metadata(self) -> None:
        """Wrong ETF and malformed metadata leave the full ETF unprocessed."""
        for metadata in [{'etf_symbol': 'ETFB'},
                         {'holdings_date': '2026-10-22'},
                         {'fetched_at': 'not a timestamp'}]:
            with self.subTest(metadata=metadata):
                result = self.run_case(
                    official_files=self.official(**metadata))
                self.assertEqual(result['totals']['not_looked_through_value'],
                                 100000)
                self.assertIn('official', result['positions'][0]['reason'])
                self.assertIsNone(result['snapshots'][0]['holdings_date'])

    def test_unavailable_and_direct_only(self) -> None:
        """Empty availability cannot break date selection or direct stocks."""
        result = self.run_case()
        self.assertEqual(result['totals']['not_looked_through_value'], 100000)
        portfolio = self.root / 'direct.json'
        portfolio.write_text(json.dumps({
            'valuation_date': '2026-10-23', 'currency': 'TWD',
            'positions': [{'symbol': '2330', 'kind': 'stock',
                           'market_value': 123}],
        }))
        opener = MagicMock(side_effect=AssertionError('network forbidden'))
        paths = run_lookthrough(portfolio, self.root / 'direct-out',
                                opener=opener, today=TODAY)
        self.assertEqual(json.loads(paths['result.json'].read_text())[
            'totals']['identified_stock_value'], 123)
        opener.assert_not_called()

    def test_latest_common_valid_local_date(self) -> None:
        """Align only validated local candidates to the latest common date."""
        older = {**self.payload, 'holdings_date': '2026-10-21'}
        other = {**older, 'etf_symbol': 'ETFB'}
        folder = self.write_local(self.payload, older, other)
        paths = run_lookthrough(self.two_etfs(), self.root / 'aligned',
                                local_snapshot_dir=folder,
                                use_network=False, today=TODAY)
        result = json.loads(paths['result.json'].read_text())
        self.assertEqual([s['holdings_date'] for s in result['snapshots']],
                         ['2026-10-21', '2026-10-21'])

    def test_remote_dates_align_with_local(self) -> None:
        """A remote candidate can align to local without replacing local."""
        self.index['etfs'] = [{'symbol': 'ETFB',
                               'dates': ['2026-10-21', '2026-10-22']}]
        self.remote = {f'ETFB/{day}.json': {
            **self.payload, 'etf_symbol': 'ETFB', 'holdings_date': day,
        } for day in ['2026-10-21', '2026-10-22']}
        paths = run_lookthrough(self.two_etfs(), self.root / 'aligned',
                                local_snapshot_dir=FIX / 'pyinvest',
                                use_network=True, opener=self.opener,
                                today=TODAY)
        result = json.loads(paths['result.json'].read_text())
        self.assertEqual([s['holdings_date'] for s in result['snapshots']],
                         ['2026-10-22', '2026-10-22'])
        self.assertFalse(any('/ETFA/' in url for url in self.urls))

    def test_failed_candidates_do_not_contaminate_date_status(self) -> None:
        """Latest failed remote holdings never shift successful dates."""
        self.index['etfs'] = [{'symbol': 'ETFB', 'dates': ['2026-10-23']}]
        invalid = deepcopy(self.payload)
        invalid.update(etf_symbol='ETFB', holdings_date='2026-10-23')
        invalid['holdings'][0]['weight'] = 1.1
        self.remote['ETFB/2026-10-23.json'] = invalid
        paths = run_lookthrough(self.two_etfs(), self.root / 'aligned',
                                local_snapshot_dir=FIX / 'pyinvest',
                                opener=self.opener, today=TODAY)
        result = json.loads(paths['result.json'].read_text())
        self.assertEqual(result['snapshots'][0]['date_status'], 'common')
        self.assertIsNone(result['snapshots'][1]['holdings_date'])

    def test_builtin_directory_is_independent_of_cwd(self) -> None:
        """Resolve immutable bundled data using the module file path."""
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            result = self.run_case(local_snapshot_dir=FIX / 'pyinvest')
        finally:
            os.chdir(previous)
        self.assertEqual(result['totals']['coverage'], .95)

    def test_remote_success_precedes_official(self) -> None:
        """Invalid local data falls to remote while preserving source kind."""
        folder = self.write_local({})
        self.index['etfs'] = [{'symbol': 'ETFA', 'dates': ['2026-10-22']}]
        self.remote['ETFA/2026-10-22.json'] = self.payload
        result = self.run_case(local_snapshot_dir=folder,
                               use_network=True, opener=self.opener,
                               official_files=self.official(etf_symbol='ETFB'))
        self.assertEqual(result['snapshots'][0]['source_kind'], 'pyinvest')
        self.assertEqual(result['totals']['coverage'], .95)
        self.assertTrue(result['warnings'])

    def test_remote_index_failure_tries_latest(self) -> None:
        """A failed index transport still permits verified latest.json."""
        self.remote['ETFA/latest.json'] = self.payload

        def opener(url: str) -> bytes:
            """Fail the index alone, serving only the explicit latest file."""
            if url.endswith('index.json'):
                raise OSError('Synthetic index failure')
            return self.opener(url)

        result = self.run_case(use_network=True, opener=opener)
        self.assertEqual(result['totals']['coverage'], .95)
        self.assertIn('index failure', ' '.join(result['warnings']))

    def test_invalid_newer_remote_retains_valid_older(self) -> None:
        """Do not fall past an available valid older published snapshot."""
        self.index['etfs'] = [{'symbol': 'ETFA',
                               'dates': ['2026-10-22', '2026-10-23']}]
        self.remote['ETFA/2026-10-23.json'] = {}
        self.remote['ETFA/2026-10-22.json'] = self.payload
        result = self.run_case(use_network=True, opener=self.opener,
                               official_files=self.official())
        self.assertEqual(result['snapshots'][0]['source_kind'], 'pyinvest')
        self.assertEqual(result['snapshots'][0]['holdings_date'], '2026-10-22')
        self.assertTrue(result['warnings'])

    def test_conflicting_local_date_falls_to_other_valid_date(self) -> None:
        """Exclude all conflicting copies but retain another valid day."""
        changed = {**self.payload, 'issuer': 'Conflicting Synthetic Issuer'}
        older = {**self.payload, 'holdings_date': '2026-10-21'}
        folder = self.write_local(self.payload, changed, self.payload, older)
        result = self.run_case(local_snapshot_dir=folder)
        self.assertEqual(result['snapshots'][0]['holdings_date'], '2026-10-21')
        self.assertIn('Conflicting', ' '.join(result['warnings']))

    def test_no_common_uses_latest_and_ignores_unrelated(self) -> None:
        """Choose each latest valid date and ignore irrelevant ETF metadata."""
        other = {**self.payload, 'etf_symbol': 'ETFB',
                 'holdings_date': '2026-10-20'}
        unrelated = {**self.payload, 'etf_symbol': 'ETFC',
                     'holdings_date': '2025-01-01'}
        folder = self.write_local(self.payload, other, unrelated)
        paths = run_lookthrough(self.two_etfs(), self.root / 'aligned',
                                local_snapshot_dir=folder,
                                use_network=False, today=TODAY)
        result = json.loads(paths['result.json'].read_text())
        self.assertEqual([s['holdings_date'] for s in result['snapshots']],
                         ['2026-10-22', '2026-10-20'])
        self.assertEqual([s['date_status'] for s in result['snapshots']],
                         ['common', 'within_tolerance'])
        self.assertEqual(result['warnings'], [])

    def test_official_dates_keep_explicit_date(self) -> None:
        """Official fallback is independent of local candidate alignment."""
        official = {'ETFB': (FIX / 'official.csv', CMAP, {
            **META, 'etf_symbol': 'ETFB',
            'holdings_date': date(2026, 10, 20), 'is_active': True,
        })}
        paths = run_lookthrough(self.two_etfs(), self.root / 'aligned',
                                local_snapshot_dir=FIX / 'pyinvest',
                                official_files=official,
                                use_network=False, today=TODAY)
        result = json.loads(paths['result.json'].read_text())
        self.assertEqual(result['snapshots'][1]['holdings_date'], '2026-10-20')
        self.assertEqual(result['snapshots'][1]['date_status'],
                         'active_mismatch')

    def test_reasons_accumulate_when_all_sources_fail(self) -> None:
        """Unavailable ETFs retain denominator and each acquisition failure."""
        folder = self.write_local({})
        self.index['etfs'] = [{'symbol': 'ETFA', 'dates': ['2026-10-22']}]
        self.remote['ETFA/2026-10-22.json'] = {}
        result = self.run_case(local_snapshot_dir=folder, use_network=True,
                               opener=self.opener,
                               official_files=self.official(etf_symbol='ETFB'))
        reason = result['positions'][0]['reason']
        for source in ['local', 'PyInvest', 'official']:
            self.assertIn(source, reason)
        self.assertEqual(result['totals']['not_looked_through_value'], 100000)

    def test_single_file_helper_preserves_strict_bulk_api(self) -> None:
        """Share decoding while bulk API still rejects any corrupt file."""
        folder = self.write_local(self.payload)
        self.assertEqual(load_local_snapshot(folder / '0.json').etf_symbol,
                         'ETFA')
        companion = folder / 'portfolio.json'
        companion.write_text('{"positions": []}')
        self.assertIsNone(load_local_snapshot(companion))
        companion.write_text('{"holdings": []}')
        with self.assertRaises(SourceUnavailable):
            load_local_snapshot(companion)
        with self.assertRaises(SourceUnavailable):
            load_local_snapshots(folder)
        result = self.run_case(local_snapshot_dir=folder)
        self.assertEqual(result['totals']['coverage'], .95)

    def test_unrelated_index_rows_do_not_block_requested(self) -> None:
        """Malformed availability for another ETF is irrelevant to this run."""
        self.index['etfs'] = [{'symbol': 'ETFA', 'dates': ['2026-10-22']},
                              {'symbol': 'ETFB', 'dates': ['../bad']}]
        self.remote['ETFA/2026-10-22.json'] = self.payload
        result = self.run_case(use_network=True, opener=self.opener)
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['totals']['coverage'], .95)
