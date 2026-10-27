"""Synthetic contract data only. Real validation bytes stay under ignored state."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
import json
import tempfile
import unittest

from backtest.contracts import Rejected, canonical, sealed, sha_bytes, strategy_spec
from backtest.maverick_import import STATE, build_snapshot, freeze_capture
from backtest.storage import load_dataset
from backtest.walkforward import CampaignRegistry
from test_maverick_import import capture


def later_capture(offset=84):
    value = deepcopy(capture())
    payload = json.loads(value['raw_response_json'])
    calendar = []
    for index, label in enumerate(payload['index']):
        day = date.fromisoformat(label[:10]) + timedelta(days=offset)
        payload['index'][index] = day.isoformat() + 'T00:00:00.000'
        opened = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30, tzinfo=ZoneInfo('America/New_York'))
        text = lambda t: t.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        calendar.append(dict(date=day.isoformat(), bar_open_at=text(opened), bar_close_at=text(opened.replace(hour=16, minute=0))))
    value['calendar'] = calendar
    value['raw_response_json'] = json.dumps(payload)
    value['metadata'].update(retrieved_at='2024-06-01T00:00:00.000Z', adjustment_as_of='2024-06-01T00:00:00.000Z',
        request=dict(ticker='SPY', start_date=calendar[0]['date'], end_date=calendar[-1]['date']),
        raw_response_sha256=sha_bytes(value['raw_response_json'].encode()))
    return value


class MaverickValidationTests(unittest.TestCase):
    def setUp(self):
        STATE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='maverick-validation-', dir=STATE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dev = freeze_capture(capture(), self.root)

    def freeze(self, value=None, **changes):
        return freeze_capture(value or later_capture(), self.root, 'validation',
            development_registry=changes.get('registry', self.dev['registry_path']),
            development_dataset_id=changes.get('dataset_id', self.dev['manifest']['dataset_id']))

    def campaign(self, store, validation, development=None):
        return store.create_campaign(sealed(dict(schema_version=1, kind='walkforward_campaign', campaign_id='unverified-ladder',
            created_at='2024-06-01T01:00:00.000Z', max_candidates=10,
            datasets=dict(development=(development or self.dev['manifest'])['manifest_sha256'],
                          validation=validation['manifest_sha256'], holdout_id='never-opened'),
            holdout_window=dict(start='2025-01-01T00:00:00.000Z', end_exclusive='2025-04-01T00:00:00.000Z'),
            initial_cash='1000.00', walkforward=dict(initial_train_bars=3, test_bars=2)), 'campaign_sha256'))

    def registration(self, store, campaign):
        spec = strategy_spec(dict(schema_version=2, family='sma_cross_long', symbol='SPY',
            parameters=dict(fast_window=1, slow_window=2), position=dict(quantity='1'), rationale='Synthetic validation mechanics.'))
        prereg = sealed(dict(schema_version=2, kind='preregistration', campaign_id=campaign['campaign_id'],
            created_at='2024-06-01T02:00:00.000Z', author=dict(slot='unit', model='synthetic', role='author'),
            strategy_sha256=spec['strategy_sha256'], datasets=campaign['datasets'],
            cost_model=dict(commission_bps='1', slippage_bps_per_side='5', regulatory_fee_bps_on_sells='0.3', rounding='half_even'),
            baselines=['buy_and_hold', 'cash'], pass_criteria=dict(min_independent_trades=30, max_drawdown_pct='15',
                min_excess_return_vs_buy_and_hold_pct='0', alpha='0.05', test='stationary_block_bootstrap_excess_daily_returns',
                bootstrap=dict(block_bars=2, resamples=1000, seed=0)), paper_criteria=None), 'preregistration_sha256')
        return store.register(campaign['campaign_id'], spec, prereg, '2024-06-02T00:00:00.000Z')['registration_sha256']

    def test_later_validation_manifest_matches_bytes_and_preserves_availability(self):
        result = self.freeze()
        manifest, rows = load_dataset(result['registry_path'], result['manifest']['dataset_id'], 2)
        self.assertEqual(manifest['partition'], 'validation')
        self.assertEqual(manifest['classification'], 'historical_unverified')
        self.assertEqual(manifest['data_sha256'], sha_bytes(Path(result['data_path']).read_bytes()))
        self.assertEqual(manifest['provenance']['development_manifest_sha256'], self.dev['manifest']['manifest_sha256'])
        self.assertLess(self.dev['manifest']['coverage']['last_bar_close'], manifest['coverage']['first_bar_open'])
        self.assertTrue(all(r['available_at'] == manifest['source']['retrieved_at'] for r in rows))
        self.assertFalse(manifest['provenance']['point_in_time_verified'])
        self.assertFalse(manifest['provenance']['historical_eligible'])
        self.assertEqual(result, self.freeze())

    def test_overlapping_or_earlier_validation_rejected_without_publication(self):
        for value in [capture(), later_capture(0), later_capture(-7)]:
            with self.subTest(start=value['calendar'][0]['date']):
                with self.assertRaisesRegex(Rejected, 'partition_overlap'):self.freeze(value)
        self.assertFalse((self.root/'snapshots/validation').exists())

    def test_development_reference_bytes_are_checked(self):
        path = Path(self.dev['data_path']);path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(Rejected, 'hash_mismatch'):self.freeze()

    def test_validation_data_and_raw_checksums_rejected_on_tamper(self):
        value = later_capture();value['raw_response_json'] += ' '
        with self.assertRaisesRegex(Rejected, 'hash_mismatch'):self.freeze(value)
        result = self.freeze();path = Path(result['data_path']);path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(Rejected, 'hash_mismatch'):
            load_dataset(result['registry_path'], result['manifest']['dataset_id'], 2)

    def test_validation_future_availability_rejected(self):
        value = later_capture();payload = json.loads(value['raw_response_json'])
        payload['available_at'] = ['2024-06-02T00:00:00.000Z']*len(payload['data'])
        value['raw_response_json'] = json.dumps(payload);value['metadata']['raw_response_sha256'] = sha_bytes(value['raw_response_json'].encode())
        with self.assertRaisesRegex(Rejected, 'future_data'):self.freeze(value)

    def test_validation_requires_known_development_and_cannot_be_holdout(self):
        with self.assertRaisesRegex(Rejected, 'development_reference_required'):
            freeze_capture(later_capture(), self.root, 'validation')
        with self.assertRaisesRegex(Rejected, 'dataset_access_denied'):
            freeze_capture(later_capture(), self.root, 'holdout')
        result = self.freeze()
        with self.assertRaisesRegex(Rejected, 'strategy_dataset_mismatch'):
            self.freeze(registry=result['registry_path'], dataset_id=result['manifest']['dataset_id'])

    def test_full_research_ladder_and_holdout_refusal_even_with_passing_verdicts(self):
        validation = self.freeze()['manifest'];store = CampaignRegistry(self.dev['campaign_registry_path'])
        self.addCleanup(store.close)
        campaign = self.campaign(store, validation);key = self.registration(store, campaign)
        with self.assertRaisesRegex(Rejected, 'stage_not_evaluated'):
            store.evaluate(key, 'validation', '2024-06-02T00:00:00.000Z')
        development = store.evaluate(key, 'development', '2024-06-02T00:00:00.000Z')
        self.assertFalse(development['verdict']['passed'])
        with self.assertRaisesRegex(Rejected, 'development_gate'):
            store.evaluate(key, 'validation', '2024-06-02T00:00:00.000Z')
        # Analytic unit fixture isolates stage wiring. It is never real qualification evidence.
        passing = sealed(dict(pvalue='0.001', data_classification='historical_unverified',
            metrics=dict(independent_trades=30, max_drawdown='0', excess_return_vs_buy_and_hold_pct='1')), 'result_sha256')
        with store.db:store.db.execute('UPDATE evaluations SET payload=? WHERE registration=? AND partition=?', (canonical(passing), key, 'development'))
        evaluated = store.evaluate(key, 'validation', '2024-06-02T00:00:00.000Z')
        self.assertEqual(evaluated['result']['availability_policy'], 'hindsight_validation_only')
        self.assertEqual(evaluated['result']['warmup_rows'], self.dev['manifest']['row_count'])
        self.assertFalse(evaluated['result']['promoting'])
        with store.db:store.db.execute('UPDATE evaluations SET payload=? WHERE registration=? AND partition=?', (canonical(passing), key, 'validation'))
        with patch('backtest.walkforward.bounded_read', side_effect=AssertionError('holdout must not open')):
            with self.assertRaisesRegex(Rejected, 'holdout_stage_gate'):store.request_holdout(key, self.root/'never-read.json')
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM holdout_requests').fetchone()[0], 0)

    def test_campaign_cannot_swap_the_development_anchor(self):
        validation = self.freeze()['manifest'];other = freeze_capture(later_capture(-7), self.root)['manifest']
        store = CampaignRegistry(self.dev['campaign_registry_path']);self.addCleanup(store.close)
        with self.assertRaisesRegex(Rejected, 'development_reference_mismatch'):self.campaign(store, validation, other)


if __name__ == '__main__':unittest.main()
