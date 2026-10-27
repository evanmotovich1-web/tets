"""Forward observations use canonical strategy rules and never a broker."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shadow
from market_data import iso, normalize_quote, YahooSource

START = datetime(2026, 10, 26, 14, 30, 30, tzinfo=timezone.utc)


def row(index, price):
    opened = datetime(2026, 10, 26, 14, 20, tzinfo=timezone.utc) + timedelta(minutes=index)
    return dict(symbol='SPY', bar_open_at=iso(opened), bar_close_at=iso(opened + timedelta(minutes=1)),
        available_at=iso(opened + timedelta(minutes=1, seconds=1)),
        open=str(price), high=str(price + 1), low=str(price - 1), close=str(price), volume='1000')


def quote(at, price=110, symbol='SPY'):
    return dict(id=symbol, price=price, time=str(int(at.timestamp() * 1000)), market_hours=1)


class ShadowTests(unittest.TestCase):
    def model(self, **changes):
        spec = shadow.load_spec()
        if changes:
            from backtest.contracts import strategy_spec
            spec.pop('strategy_sha256'); spec.update(changes); spec = strategy_spec(spec)
        events = []
        model = shadow.Shadow(spec, START, lambda kind, value: events.append((kind, value)))
        model.bars([row(i, 100 + i) for i in range(10)], START)
        return model, events

    def test_history_warms_indicators_without_past_decisions_or_fills(self):
        model, events = self.model()
        self.assertEqual(model.decisions, []); self.assertEqual(model.fills, []); self.assertIsNone(model.pending)
        self.assertEqual(model.sequence, 10)
        self.assertEqual(events[-1][0], 'warmup_loaded')

    def test_reconnect_catchup_evaluates_latest_bar_once_without_replaying_old_intents(self):
        model, _ = self.model()
        now = START.replace(minute=32, second=2)
        model.bars([row(10, 110), row(11, 111)], now)
        self.assertEqual(len(model.decisions), 1)
        self.assertEqual(model.decisions[0]['bar_close_at'], row(11, 111)['bar_close_at'])

    def test_canonical_signal_and_later_quote_fill_have_explicit_cost_and_provenance(self):
        model, events = self.model()
        now = START.replace(minute=31, second=2)
        model.bars([row(10, 110)], now)
        from backtest.engine import family_signal
        self.assertEqual(model.pending['side'], family_signal(model.spec, model.rows, 10, False))
        # An old quote received after the decision must never fill that decision.
        model.quote(quote(now - timedelta(seconds=1)), now + timedelta(seconds=1))
        self.assertEqual(model.fills, [])
        later = now + timedelta(seconds=2)
        model.quote(quote(later), later)
        self.assertEqual(len(model.fills), 1)
        self.assertEqual(model.fills[0]['price'], '110.11')
        self.assertEqual(model.fills[0]['fill_kind'], 'assumed_quote_fill_not_broker_execution')
        self.assertEqual(model.quantity, Decimal(1))
        self.assertLess(model.fills[0]['decision_at'], model.fills[0]['quote_at'])
        self.assertEqual(model.snapshot(later)['virtual_equity'], '9999.89')

    def test_stale_future_wrong_symbol_and_nonfinite_quotes_are_rejected(self):
        model, _ = self.model()
        for value in [quote(START - timedelta(seconds=11)), quote(START + timedelta(seconds=3)),
                      quote(START, symbol='AAPL'), quote(START, price=float('nan')), quote(START, price=-1)]:
            model.quote(value, START)
        self.assertIsNone(model.latest)
        self.assertEqual(sum(model.drops.values()), 5)

    def test_round_trip_realized_pnl_reconciles_cash_and_both_sides_slippage(self):
        model, _ = self.model(parameters=dict(fast_window=1, slow_window=2))
        now = START.replace(minute=31, second=2)
        model.bars([row(10, 110)], now)
        model.quote(quote(now + timedelta(seconds=2), price=110), now + timedelta(seconds=2))
        model.bars([row(11, 108)], now + timedelta(minutes=1))
        # Cross the 2/10 SMA down; the subsequent quote supplies the fill price.
        model.bars([row(12, 104)], now + timedelta(minutes=2))
        later = now + timedelta(minutes=2, seconds=2)
        model.quote(quote(later, price=106), later)
        self.assertEqual([f['side'] for f in model.fills], ['buy', 'sell'])
        self.assertEqual(model.cash, Decimal('9995.78'))
        self.assertEqual(model.realized, Decimal('-4.22'))
        self.assertEqual(model.quantity, Decimal(0))

    def test_fresh_quote_cannot_fill_when_data_bars_are_unavailable(self):
        model, _ = self.model()
        now = START.replace(minute=31, second=2)
        model.bars([row(10, 110)], now); model.bar_status = 'unavailable'
        model.quote(quote(now + timedelta(seconds=1)), now + timedelta(seconds=1))
        self.assertEqual(model.fills, [])
        self.assertEqual(model.snapshot(now + timedelta(seconds=1))['state'], 'bars_unavailable')

    def test_duplicate_and_out_of_order_quotes_are_not_counted_as_new_price_evidence(self):
        model, events = self.model()
        model.quote(quote(START), START)
        model.quote(quote(START), START)
        model.quote(quote(START - timedelta(seconds=1)), START)
        self.assertEqual(model.drops['duplicate_or_out_of_order'], 2)
        self.assertEqual(sum(k == 'quote_observed' for k, _ in events), 1)

    def test_overnight_quotes_are_visible_but_never_execute_virtual_intents(self):
        model, _ = self.model()
        now = START.replace(hour=22)
        model.pending = dict(side='buy', created_at=iso(now), reference_price=None, limit_price=None)
        model.quote(quote(now + timedelta(seconds=1)), now + timedelta(seconds=1))
        self.assertIsNotNone(model.latest); self.assertEqual(model.fills, [])
        self.assertEqual(model.snapshot(now + timedelta(seconds=1))['state'], 'market_closed')

    def test_a_revised_previously_observed_bar_pauses_instead_of_rewriting_history(self):
        model, _ = self.model()
        changed = row(9, 999)
        model.bars([changed], START)
        self.assertTrue(model.revision)
        self.assertEqual(model.rows[-1]['close'], '109')
        model.bars([row(10, 110)], START.replace(minute=31, second=2))
        self.assertIsNone(model.pending)
        self.assertEqual(model.snapshot(START)['state'], 'bar_revision_paused')

    def test_pending_virtual_limit_needs_a_later_observation_within_price(self):
        model, _ = self.model(execution=dict(order_type='limit', limit_offset_bps='10'))
        now = START.replace(minute=31, second=2)
        model.bars([row(10, 110)], now)
        model.quote(quote(now + timedelta(seconds=1)), now + timedelta(seconds=1))
        self.assertEqual(model.fills, [])
        model.quote(quote(now + timedelta(seconds=2), price=112), now + timedelta(seconds=2))
        self.assertEqual(model.fills, [])
        model.quote(quote(now + timedelta(seconds=3), price=109), now + timedelta(seconds=3))
        self.assertEqual(len(model.fills), 1)

    def test_expired_intent_and_bars_unavailable_do_not_fill(self):
        model, _ = self.model()
        now = START.replace(minute=31, second=2)
        model.bars([row(10, 110)], now)
        model.bar_status = 'unavailable'
        model.quote(quote(now + timedelta(seconds=2)), now + timedelta(seconds=2))
        self.assertEqual(model.fills, [])
        model.bar_status = 'available'
        model.quote(quote(now + timedelta(seconds=121)), now + timedelta(seconds=121))
        self.assertEqual(model.fills, []); self.assertIsNone(model.pending)

    def test_future_bars_are_refused_and_earnings_specs_require_missing_data(self):
        model, _ = self.model()
        with self.assertRaisesRegex(ValueError, 'future_bar'): model.bars([row(11, 112)], START)
        spec = shadow.load_spec(); spec.pop('strategy_sha256')
        spec.update(family='post_earnings_drift_long', timeframe='1h', parameters=dict(min_surprise_pct='1', entry_delay_bars=1, hold_bars=2), data_requirements=['ohlcv_pti_v1', 'earnings_events_pti_v1'])
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'spec.json'; p.write_text(json.dumps(spec))
            with self.assertRaisesRegex(ValueError, 'earnings_data_unavailable'): shadow.load_spec(p)

    def test_status_is_explicitly_unqualified_and_has_no_broker_client(self):
        model, _ = self.model()
        status = model.snapshot(START)
        self.assertFalse(status['live_orders']); self.assertFalse(status['broker_orders']); self.assertFalse(status['promoting'])
        self.assertEqual(status['qualification'], 'unqualified_forward_observation')
        self.assertFalse(any(hasattr(model, method) for method in ('place_order', 'submit_order', 'request')))

    def test_yahoo_bars_keep_receipt_time_and_exclude_incomplete_and_extended_hours(self):
        import pandas as pd
        frame = pd.DataFrame({'Open': [100, 101, 102], 'High': [101, 102, 103], 'Low': [99, 100, 101],
                              'Close': [100, 101, 102], 'Volume': [1000, 2000, 3000]},
            index=pd.to_datetime(['2026-10-26T12:00:00Z', '2026-10-26T14:20:00Z', '2026-10-26T14:30:00Z']))
        fake = SimpleNamespace(Ticker=lambda symbol: SimpleNamespace(history=lambda **kwargs: frame))
        with patch.dict(sys.modules, {'yfinance': fake}): result = YahooSource().bars('SPY', '1m', START)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['bar_close_at'], '2026-10-26T14:21:00.000Z')
        self.assertGreater(result[0]['available_at'], result[0]['bar_close_at'])

    def test_shadow_files_are_packaged_but_runtime_prices_and_state_are_excluded(self):
        import release
        files = release.source_files(shadow.ROOT)
        for name in ['scripts/shadow.py', 'scripts/market_data.py', 'tets', 'scripts/shadow-requirements.lock',
                     'scripts/runner.py', 'scripts/news.py', 'scripts/status.html']:
            self.assertTrue(name in files, name)
        self.assertFalse(release.allowed('desk/runtime/.state/shadow/runs/test/events.jsonl'))


if __name__ == '__main__': unittest.main()
