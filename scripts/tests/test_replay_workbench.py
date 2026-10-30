"""Saved-evidence replay contracts. Fixtures here are mechanics tests, not edge."""
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))
from desk.backtest.contracts import canonical, strategy_spec


def fixture_report():
    spec = strategy_spec(dict(schema_version=2, family='sma_cross_long', symbol='SPY', timeframe='1m',
        parameters=dict(fast_window=1, slow_window=2),
        position=dict(sizing='fixed_quantity', quantity='2', max_positions=1),
        execution=dict(timing='next_bar_open', order_type='market', limit_offset_bps='10', pending_on_end='cancel', terminal_position='mark_to_market'),
        rationale='Test fixture only, no market edge.'))
    bars = []
    for i, close in enumerate([10, 12, 14, 11, 8, 9, 12, 13]):
        bars.append(dict(symbol='SPY', bar_open_at=f'2026-10-20T13:{30+i}:00.000Z',
            bar_close_at=f'2026-10-20T13:{31+i}:00.000Z', available_at='2026-10-27T04:31:57.041Z',
            open=str(close), high=str(close+1), low=str(close-1), close=str(close), volume='100'))
    data = dict(kind='algo_backtest_report', schema_version=1, strategy=spec, bars=bars,
        classification='historical_unverified', point_in_time_verified=False,
        independent_replay=dict(orders=True, fills=True, equity=True, metrics=True),
        symbol='SPY', timeframe='1m', live_orders=False, promoting=False,
        denominator=dict(included_bars=len(bars)), created_at='2026-10-27T04:44:10.013Z')
    return seal_report(data)


def seal_report(data: dict) -> dict:
    data.pop('report_sha256', None)
    data['report_sha256'] = hashlib.sha256(canonical(data)).hexdigest()
    return data


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('replay_workbench'), 'Saved-data replay module is missing')
        self.api = importlib.import_module('replay_workbench')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve()
        self.patch = patch.object(self.api, 'STATE_ROOTS', (self.state,))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def save_report(self, data=None):
        data = data or fixture_report()
        path = self.state / 'workstation/backtest-report.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def test_library_uses_full_verified_saved_bars_without_fallback_data(self):
        self.assertEqual(self.api.library()['datasets'], [])
        self.assertEqual(self.api.library()['state'], 'unavailable')
        self.save_report()
        result = self.api.library()
        self.assertEqual(result['state'], 'available')
        self.assertEqual(len(result['datasets']), 1)
        self.assertEqual(result['datasets'][0]['bar_count'], 8)
        self.assertEqual(result['datasets'][0]['start'], '2026-10-20T13:30:00.000Z')
        self.assertEqual(result['strategies'][0]['id'], fixture_report()['strategy']['strategy_sha256'])
        self.assertTrue(result['strategies'][0]['available'])
        self.assertFalse(result['live_orders'])

    def test_tampered_unsafe_or_invalid_reports_are_never_datasets(self):
        data = fixture_report()
        mutations = [lambda d: d.update(live_orders=True),
                     lambda d: d.update(promoting=True),
                     lambda d: d['independent_replay'].update(fills=False),
                     lambda d: d.update(classification='synthetic'),
                     lambda d: d['denominator'].update(included_bars=1),
                     lambda d: d['bars'][1].update(bar_open_at=d['bars'][0]['bar_open_at']),
                     lambda d: d['bars'][0].update(low='-1'),
                     lambda d: d['bars'][0].update(close='NaN'),
                     lambda d: d['bars'][0].update(symbol='OTHER'),
                     lambda d: d['bars'][0].update(available_at='2026-10-19T00:00:00.000Z')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad = copy.deepcopy(data)
                mutate(bad)
                self.save_report(seal_report(bad))
                self.assertEqual(self.api.library()['datasets'], [])
        self.save_report(data)
        path = self.state / 'workstation/backtest-report.json'
        path.write_text(path.read_text().replace('"volume": "100"', '"volume": "101"'))
        self.assertEqual(self.api.library()['datasets'], [])
        original = self.state / 'outside.json'
        original.write_text(json.dumps(data))
        path.unlink()
        path.symlink_to(original)
        self.assertEqual(self.api.library()['datasets'], [])

    def test_saved_catalog_deduplicates_and_disables_unsupported_specs(self):
        self.save_report()
        base = fixture_report()['strategy']
        cases = [base]
        for changes in [dict(family='momentum_long', parameters=dict(lookback_bars=3, enter_return_bps='10', exit_return_bps='0', trend_filter_sma=None)),
                        dict(timeframe='1d'), dict(exits=dict(stop_loss_bps='100', take_profit_bps=None, max_hold_bars=None))]:
            spec = {**base, **changes}
            spec.pop('strategy_sha256')
            cases.append(strategy_spec(spec))
        for spec in cases:
            path = self.state / 'library/bundles' / spec['strategy_sha256'] / 'strategy.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(spec))
        result = self.api.library()
        self.assertEqual(len(result['strategies']), 4)
        self.assertEqual(sum(s['available'] for s in result['strategies']), 1)
        for entry in result['strategies']:
            if not entry['available']:
                self.assertTrue(entry['reason'])
        self.assertEqual(len(result['datasets']), 1)

    def test_run_recomputes_causal_fills_equity_and_costs_for_every_bar(self):
        self.save_report()
        self.assertTrue(callable(getattr(self.api, 'run', None)), 'Replay execution is missing')
        settings = self.api.library()['defaults']
        settings.update(capital=100, slippage_bps=100)
        result = self.api.run(settings)
        self.assertEqual(len(result['bars']), 8)
        self.assertEqual(result['bars'][2]['decision'], 'buy')
        self.assertEqual(result['bars'][2]['position'], 0)
        fill = result['fills'][0]
        self.assertEqual(fill['bar_index'], 3)
        self.assertEqual(fill['reference_price'], 11)
        self.assertEqual(fill['price'], 11.11)
        self.assertEqual(fill['quantity'], 2)
        self.assertEqual(fill['slippage_cost'], .22)
        self.assertEqual(result['bars'][3]['cash'], 77.78)
        self.assertEqual(result['bars'][3]['equity'], 99.78)
        self.assertEqual(result['fills'][1]['side'], 'sell')
        self.assertEqual(result['fills'][1]['bar_index'], 5)
        self.assertEqual(result['fills'][1]['price'], 8.91)
        for row in result['bars']:
            self.assertAlmostEqual(row['equity'], row['cash'] + row['position'] * row['close'], places=6)
        self.assertEqual(result['summary']['bar_count'], 8)
        self.assertFalse(result['live_orders'])
        self.assertEqual(result['settings']['commission_bps'], 0)
        self.assertEqual(result['settings']['slippage_bps'], 100)
        cheaper = self.api.run({**settings, 'slippage_bps': 0})
        self.assertGreater(cheaper['summary']['final_equity'], result['summary']['final_equity'])
        poor = self.api.run({**settings, 'capital': 1})
        self.assertEqual(poor['fills'], [])
        self.assertEqual(poor['summary']['final_equity'], 1)
        self.assertGreater(poor['summary']['rejected_orders'], 0)

    def test_run_rejects_unbounded_inputs_and_unavailable_ids(self):
        self.save_report()
        valid = self.api.library()['defaults']
        invalid = [None, [], {}, {**valid, 'capital': 0}, {**valid, 'capital': 100000001},
                   {**valid, 'capital': True}, {**valid, 'capital': '100'}, {**valid, 'capital': float('nan')},
                   {**valid, 'slippage_bps': -1}, {**valid, 'slippage_bps': 1001},
                   {**valid, 'slippage_bps': float('inf')}, {**valid, 'slippage_bps': False},
                   {**valid, 'strategy_id': '../strategy.json'}, {**valid, 'strategy_id': []},
                   {**valid, 'dataset_id': 'a'*64}, {**valid, 'command': 'echo no'}, {**valid, 'fee': 10}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.api.run(payload)
        path = self.state / 'workstation/backtest-report.json'
        path.unlink()
        with self.assertRaises(ValueError):
            self.api.run(valid)
        self.save_report()
        data = fixture_report()
        spec = data['strategy']
        spec['execution']['order_type'] = 'limit'
        spec.pop('strategy_sha256')
        data['strategy'] = strategy_spec(spec)
        self.save_report(seal_report(data))
        with self.assertRaises(ValueError):
            self.api.run({**valid, 'strategy_id': data['strategy']['strategy_sha256']})

    def test_http_routes_enforce_origin_json_and_body_bounds(self):
        from http.server import ThreadingHTTPServer
        from http.client import HTTPConnection
        import threading
        import runner
        self.save_report()
        server = ThreadingHTTPServer(('127.0.0.1', 0), runner.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        port = server.server_port
        conn = HTTPConnection('127.0.0.1', port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/api/backtest/library')
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        library = json.loads(response.read())
        origin = f'http://127.0.0.1:{port}'
        body = json.dumps(library['defaults'])
        headers = {'Origin': origin, 'Content-Type': 'application/json'}
        for content, changes, status in [(body, {'Origin':'https://evil.example'}, 403),
                (body, {'Host':'evil.example'}, 403), (body, {'Content-Type':'text/plain'}, 415),
                (' ' * 4097, {}, 400), ('{}', {}, 400), (body, {}, 200)]:
            conn.request('POST', '/api/backtest/run', content, {**headers, **changes})
            response = conn.getresponse()
            payload = response.read()
            self.assertEqual(response.status, status)
            if status == 200:
                self.assertEqual(len(json.loads(payload)['bars']), 8)

    def test_provenance_identifies_immutable_evidence_and_bar_timing(self):
        path = self.save_report()
        original = path.read_bytes()
        result = self.api.run(self.api.library()['defaults'])
        self.assertEqual(result['provenance']['source_report'], str(path))
        self.assertEqual(result['provenance']['data_hash_format'], 'RFC8785 canonical bars array')
        self.assertEqual(result['bars'][0]['bar_close_at'], fixture_report()['bars'][0]['bar_close_at'])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(result['provenance']['report_sha256'], fixture_report()['report_sha256'])
        self.assertFalse(result['provenance']['point_in_time_verified'])

    def test_malformed_saved_strategy_hash_is_disabled_not_a_server_error(self):
        self.save_report()
        path = self.state / 'library/bundles/bad/strategy.json'
        path.parent.mkdir(parents=True)
        for identity in [123, [], '../not-a-hash']:
            spec = fixture_report()['strategy']
            spec['strategy_sha256'] = identity
            path.write_text(json.dumps(spec))
            result = self.api.library()
            self.assertEqual(len(result['strategies']), 2)
            disabled = [s for s in result['strategies'] if not s['available']]
            self.assertEqual(len(disabled), 1)
            self.assertIn('Invalid', disabled[0]['reason'])
            self.assertRegex(disabled[0]['id'], '^[a-f0-9]{64}$')

    def test_future_prices_cannot_change_earlier_decisions_or_fills(self):
        data = fixture_report()
        self.save_report(data)
        settings = self.api.library()['defaults']
        original = self.api.run(settings)
        for row in data['bars'][5:]:
            row.update(open='200', high='201', low='199', close='200')
        self.save_report(seal_report(data))
        settings.pop('dataset_id')
        changed = self.api.run(settings)
        self.assertEqual(original['bars'][:4], changed['bars'][:4])
        self.assertEqual([f for f in original['fills'] if f['bar_index'] < 5],
                         [f for f in changed['fills'] if f['bar_index'] < 5])

    def test_decision_status_does_not_leak_the_next_bar_execution_outcome(self):
        data = fixture_report()
        self.save_report(data)
        settings = self.api.library()['defaults']
        settings['capital'] = 100
        original = self.api.run(settings)
        data['bars'][3].update(open='200', high='201', low='199', close='200')
        self.save_report(seal_report(data))
        settings.pop('dataset_id')
        changed = self.api.run(settings)
        self.assertEqual(original['bars'][:3], changed['bars'][:3])
        self.assertEqual(original['bars'][2]['decision_status'], 'pending')
        self.assertEqual(original['bars'][3]['execution_events'][0]['status'], 'filled')
        self.assertEqual(changed['bars'][3]['execution_events'][0]['status'], 'rejected_insufficient_cash')

    def test_terminal_signal_is_cancelled_without_adding_a_fake_bar(self):
        data = fixture_report()
        data['bars'] = data['bars'][:3]
        data['denominator']['included_bars'] = 3
        self.save_report(seal_report(data))
        result = self.api.run(self.api.library()['defaults'])
        self.assertEqual(len(result['bars']), 3)
        self.assertEqual(result['fills'], [])
        self.assertEqual(result['bars'][-1]['decision'], 'buy')
        self.assertEqual(result['bars'][-1]['decision_status'], 'cancelled_terminal')

    def test_empty_malformed_oversized_or_linked_parent_evidence_fails_closed(self):
        path = self.save_report()
        for raw in ['{broken', '[]', 'null', '{}']:
            path.write_text(raw)
            self.assertEqual(self.api.library()['datasets'], [])
        self.save_report()
        with patch.object(self.api, 'MAX_BYTES', 10):
            self.assertEqual(self.api.library()['datasets'], [])
        directory = path.parent
        moved = directory.with_name('other')
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.api.library()['datasets'], [])


if __name__ == '__main__':
    unittest.main()
