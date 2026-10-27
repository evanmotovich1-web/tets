"""Prospective paper execution boundaries; no network or credentials required."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paper
import release


class Helper:
    TRADING_BASE_URL = paper.PAPER_URL
    DATA_BASE_URL = paper.DATA_URL

    def __init__(self):
        self.calls = []
        self.account = dict(id='test-account', status='ACTIVE', equity='100000', last_equity='99900',
                            cash='100000', buying_power='100000', account_number='PRIVATE-DO-NOT-PUBLISH')
        self.clock = dict(timestamp='2026-10-26T15:01:10Z', is_open=True, next_open='2026-10-27T13:30:00Z')
        self.positions, self.open_orders, self.orders = [], [], []
        self.reconciled, self.fail_submit = {}, False

    def require_paper_mode(self, base_url, paper=True):
        if base_url != self.DATA_BASE_URL and base_url != 'https://paper-api.alpaca.markets':
            raise RuntimeError('Live disabled')

    def ema(self, values, period): return 2 if period == 9 else 1

    def api_request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == 'POST':
            if self.fail_submit: raise TimeoutError('private HTTP response must not be published')
            return dict(id='private-order', status='accepted', filled_qty='0')
        if path == '/v2/account': return self.account
        if path == '/v2/clock': return self.clock
        if path == '/v2/positions': return self.positions
        if path == '/v2/orders:by_client_order_id': return self.reconciled
        if path == '/v2/orders': return self.open_orders if kwargs['params']['status'] == 'open' else self.orders
        if path == '/v2/stocks/bars':
            minute = '00' if '15:01' in self.clock['timestamp'] else '01'
            return dict(bars={'SPY': [dict(t=f'2026-10-26T15:{minute}:00Z', o=500, h=501, l=499, c=500, v=10)]})
        raise AssertionError(path)


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.object(paper, 'STATE', self.root); patcher.start(); self.addCleanup(patcher.stop)
        self.helper = Helper()
        self.spec = dict(label='DEMO', family='upstream_ema', symbol='SPY', timeframe='1m',
                         quantity='1', strategy_sha256='test-demo')
        self.session = paper.Session(self.helper, self.spec)

    def advance(self): self.helper.clock['timestamp'] = '2026-10-26T15:02:10Z'
    def posts(self): return [c for c in self.helper.calls if c[0] == 'POST']

    def test_initial_observation_never_submits_a_historical_signal(self):
        result = self.session.observe()
        self.assertEqual(result['state'], 'watching'); self.assertEqual(self.posts(), [])
        self.assertEqual(result['account']['equity'], '100000')
        self.assertNotIn('PRIVATE', json.dumps(result)); self.assertNotIn('account_number', result['account'])

    def test_new_bar_submits_once_and_acceptance_is_not_a_fill(self):
        self.session.observe(); self.advance()
        result = self.session.observe(); self.session.observe()
        self.assertEqual(len(self.posts()), 1)
        payload = self.posts()[0][2]['payload']
        self.assertEqual(payload['type'], 'limit'); self.assertEqual(payload['limit_price'], '501.00')
        self.assertFalse(payload['extended_hours']); self.assertEqual(result['orders'], [])
        self.assertIn(payload['client_order_id'], json.loads(self.session.journal.read_text())['intents'])

    def test_crash_after_submission_never_retries_unknown_outcome(self):
        self.session.observe(); self.advance(); self.helper.fail_submit = True
        with self.assertRaises(TimeoutError): self.session.observe()
        self.helper.fail_submit = False
        restarted = paper.Session(self.helper, self.spec)
        result = restarted.observe()
        self.assertEqual(result['state'], 'paused'); self.assertIn('unknown', result['message'])
        self.assertEqual(len(self.posts()), 1)

    def test_fill_is_reconciled_from_broker_and_position_ownership_survives_restart(self):
        self.session.observe(); self.advance(); self.session.observe()
        client_id = self.posts()[0][2]['payload']['client_order_id']
        self.helper.reconciled = dict(side='buy', qty='1', filled_qty='1', filled_at='2026-10-26T15:02:12Z', status='filled')
        self.helper.orders = [{**self.helper.reconciled, 'client_order_id': client_id, 'symbol':'SPY', 'filled_avg_price':'500', 'submitted_at':'2026-10-26T15:02:10Z'}]
        self.helper.positions = [dict(symbol='SPY', qty='1', avg_entry_price='500', current_price='501', unrealized_pl='1')]
        result = paper.Session(self.helper, self.spec).observe()
        self.assertEqual(result['state'], 'watching'); self.assertEqual(result['orders'][0]['filled_qty'], '1')
        self.assertNotIn('client_order_id', result['orders'][0])

    def test_market_closed_never_orders_and_reports_next_open(self):
        self.helper.clock['is_open'] = False
        result = self.session.observe(); self.advance(); self.session.observe()
        self.assertEqual(result['state'], 'market_closed'); self.assertTrue(result['next_open'])
        self.assertEqual(self.posts(), [])

    def test_stale_data_prevents_orders(self):
        self.session.observe()
        self.helper.clock['timestamp'] = '2026-10-26T15:20:10Z'
        result = self.session.observe()
        self.assertEqual(result['state'], 'paused'); self.assertIn('stale', result['message'])
        self.assertEqual(self.posts(), [])

    def test_external_position_prevents_strategy_from_selling_it(self):
        self.session.observe(); self.advance()
        self.helper.positions = [dict(symbol='SPY', qty='1', avg_entry_price='500')]
        self.assertEqual(self.session.observe()['state'], 'paused'); self.assertEqual(self.posts(), [])

    def test_pending_order_prevents_second_submission(self):
        self.session.observe(); self.advance()
        self.helper.open_orders = [dict(symbol='SPY', client_order_id=self.session.prefix + 'pending')]
        self.session.observe(); self.assertEqual(self.posts(), [])

    def test_blocked_account_and_oversized_buy_never_submit(self):
        self.session.observe(); self.advance(); self.helper.account['trading_blocked'] = True
        self.assertEqual(self.session.observe()['state'], 'paused'); self.assertEqual(self.posts(), [])
        self.helper.account['trading_blocked'] = False
        self.session.record['last_bar'] = '2026-10-26T15:01:00+00:00'; self.spec['quantity'] = '3'
        self.assertIn('$1,000', self.session.observe()['message']); self.assertEqual(self.posts(), [])

    def test_different_account_refuses_old_strategy_journal(self):
        self.session.observe(); self.helper.account['id'] = 'a-different-account'
        with self.assertRaisesRegex(ValueError, 'account changed'): self.session.observe()

    def test_live_endpoint_is_rejected_before_any_api_call(self):
        self.helper.TRADING_BASE_URL = 'https://api.alpaca.markets'
        with self.assertRaises(RuntimeError): self.session.observe()
        self.assertEqual(self.helper.calls, [])

    def test_future_incomplete_and_extended_hours_bars_are_excluded(self):
        raw = [dict(t=t, o=1, h=2, l=1, c=2, v=1) for t in
               ['2026-10-26T12:00:00Z','2026-10-26T15:00:00Z','2026-10-26T15:01:00Z','2026-10-26T15:02:00Z']]
        rows = paper.closed_bars(raw, 'SPY', '1m', datetime(2026,10,26,15,1,30,tzinfo=timezone.utc))
        self.assertEqual(len(rows), 1); self.assertIn('15:01:00', rows[0]['bar_close_at'])

    def test_missing_credentials_is_truthful_and_never_loads_broker(self):
        with patch.object(paper, 'credentials', return_value=None), patch.object(paper, 'load_helpers') as load:
            result = paper.watch(self.root / 'activity.json', once=True)
        self.assertEqual(result['state'], 'credentials_required'); load.assert_not_called()
        self.assertNotIn('account', result)

    def test_connection_failure_does_not_publish_exception_or_secret(self):
        with patch.object(paper, 'credentials', return_value=dict(api_key='test', secret_key='test')), \
             patch.object(paper, 'check_install'), patch.object(paper, 'load_helpers', return_value=self.helper), \
             patch.object(paper, 'strategy', return_value=self.spec), \
             patch.object(paper.Session, 'observe', side_effect=ValueError('PRIVATE-SECRET')), patch.dict(os.environ, {}):
            result = paper.watch(self.root / 'activity.json', once=True)
        self.assertEqual(result['state'], 'connection_error'); self.assertNotIn('PRIVATE', json.dumps(result))

    def test_paper_runtime_is_included_but_credentials_are_excluded_from_release(self):
        files = release.source_files(paper.ROOT)
        for name in ('scripts/paper.py','scripts/paper-requirements.in','scripts/paper-requirements.lock','scripts/runner.py'):
            self.assertIn(name, files)
        self.assertFalse(release.allowed('desk/runtime/.state/paper/credentials.json'))

    def test_keys_are_written_private_and_noninteractive_config_does_not_prompt(self):
        with patch.object(paper, 'MODE', self.root/'mode.json'), patch.object(paper, 'credentials', return_value=None), \
             patch.object(paper.getpass, 'getpass') as ask:
            paper.configure('paper', interactive=False)
            ask.assert_not_called(); self.assertEqual(json.loads((self.root/'mode.json').read_text())['mode'], 'paper')
        paper.save(self.root/'private.json', dict(secret_key='test-only'))
        self.assertEqual((self.root/'private.json').stat().st_mode & 0o777, 0o600)

    def test_algo_candidate_uses_existing_contract_and_signal_function(self):
        candidate = self.root/'candidate.json'
        candidate.write_text(json.dumps(dict(schema_version=2, family='sma_cross_long', symbol='SPY', timeframe='1m',
            parameters=dict(fast_window=2, slow_window=3), position=dict(quantity='1'), rationale='Test candidate')))
        with patch.dict(os.environ, {'ALGO_PAPER_STRATEGY':str(candidate)}):
            spec = paper.strategy()
        rows = [dict(close=str(v)) for v in range(1,6)]
        self.assertEqual(len(spec['strategy_sha256']), 64)
        self.assertEqual(paper.signal_for(self.helper, spec, rows, None, []), 'buy')

    def test_running_connection_screen_detects_new_keys_without_restart(self):
        result = dict(kind='algo_paper_activity', state='watching', live_orders=False)
        with patch.object(paper, 'credentials', side_effect=[None, dict(api_key='test', secret_key='test')]), \
             patch.object(paper, 'check_install') as check, \
             patch.object(paper, 'load_helpers', return_value=self.helper), \
             patch.object(paper, 'strategy', return_value=self.spec), \
             patch.object(paper.Session, 'observe', return_value=result) as observe, \
             patch.object(paper.time, 'sleep', side_effect=[None, KeyboardInterrupt]), patch.dict(os.environ, {}):
            with self.assertRaises(KeyboardInterrupt): paper.watch(self.root/'activity.json')
        self.assertEqual(json.loads((self.root/'activity.json').read_text())['state'], 'watching')
        check.assert_called_once(); observe.assert_called_once()


if __name__ == '__main__': unittest.main()
