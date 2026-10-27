"""Multi-company shadow observations: provenance, bounded state and shared capital."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shadow
from test_shadow import START, row, quote


class WatchlistTests(unittest.TestCase):
    def scan(self, capital='1000.01'):
        events = []
        return shadow.ShadowScan(shadow.load_spec(), ['SPY','AAPL','MSFT'], START,
                                 lambda *event:events.append(event), capital, '1000'), events

    def test_watchlist_deduplicates_keeps_primary_and_refuses_unbounded_or_invalid_symbols(self):
        self.assertEqual(shadow.watchlist(' aapl,SPY,AAPL,msft ', 'SPY'), ['SPY','AAPL','MSFT'])
        for value in ['AAPL,MSFT,QQQ,DIA,GOOG,SPY', '../secrets', 'AAPL;sh', 'https://example.org']:
            with self.assertRaises(ValueError): shadow.watchlist(value)

    def test_allocation_sums_to_original_capital_without_cloning_the_account(self):
        scan, _ = self.scan()
        self.assertEqual(sum(m.initial_cash for m in scan.models.values()), Decimal('1000.01'))
        self.assertEqual([m.initial_cash for m in scan.models.values()], [Decimal('333.34'),Decimal('333.34'),Decimal('333.33')])
        self.assertTrue(all(m.entry_budget <= m.initial_cash for m in scan.models.values()))
        self.assertEqual(scan.snapshot(START)['portfolio']['virtual_equity'], '1000.01')
        with self.assertRaises(ValueError): self.scan('0.01')

    def test_only_prospective_signals_create_markers_and_later_quotes_create_virtual_fills(self):
        scan, events = self.scan()
        for symbol, model in scan.models.items():
            model.bars([{**row(i,100+i),'symbol':symbol} for i in range(10)], START)
        self.assertFalse(any(m.decisions or m.fills for m in scan.models.values()))
        now = START.replace(minute=31, second=2)
        for symbol, model in scan.models.items():
            model.bars([{**row(10,110),'symbol':symbol}], now)
            self.assertEqual(model.decisions[-1]['side'], 'buy')
            self.assertEqual(model.decisions[-1]['symbol'], symbol)
            self.assertIn('new completed', model.decisions[-1]['reason'])
            self.assertEqual(model.fills, [])
            later = now + timedelta(seconds=2)
            model.quote(quote(later, symbol=symbol), later)
        value = scan.snapshot(later)
        self.assertEqual(sum(len(c['fills']) for c in value['companies']), 3)
        self.assertEqual(len([e for e in value['events'] if e['kind']=='signal_observed']), 3)
        self.assertEqual({p['symbol'] for k,p in events if k=='virtual_fill_assumed'}, {'SPY','AAPL','MSFT'})
        expected = sum(m.cash + m.quantity*Decimal('110') for m in scan.models.values())
        self.assertEqual(Decimal(value['portfolio']['virtual_equity']), expected)
        self.assertFalse(value['broker_orders']);self.assertFalse(value['live_orders'])

    def test_charts_are_bounded_and_do_not_fabricate_quotes_from_historical_candles(self):
        scan, _ = self.scan();model=scan.models['SPY']
        model.rows=[row(i,100+i) for i in range(600)]
        value=scan.snapshot(START)['companies'][0]
        self.assertEqual(len(value['chart_bars']),120)
        self.assertEqual(value['quote_history'],[])
        self.assertIsNone(value['quote'])
        for i in range(130):
            now=START+timedelta(seconds=i)
            model.quote(quote(now),now)
        self.assertEqual(len(model.quote_history),120)
        self.assertLess(len(shadow.canonical(scan.snapshot(START))),1024*1024)

    def test_a_symbol_revision_pauses_only_its_own_virtual_intent(self):
        scan, _ = self.scan()
        for symbol, model in scan.models.items():model.bars([{**row(0,100),'symbol':symbol}],START)
        scan.models['SPY'].bars([row(0,99)],START)
        self.assertTrue(scan.models['SPY'].revision)
        self.assertFalse(scan.models['AAPL'].revision)

    def test_stopped_scan_never_claims_to_still_be_checking_a_company(self):
        scan, _ = self.scan();scan.active_symbol='MSFT'
        value=scan.snapshot(START,stopped=True)
        self.assertIsNone(value['scanner']['active_symbol'])
        self.assertTrue(all(c['state']=='stopped' for c in value['companies']))

    def test_watch_uses_one_subscription_routes_quotes_and_isolates_a_failed_symbol(self):
        class Stream:
            subscriptions=[]
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            async def subscribe(self,symbols):self.subscriptions.append(symbols)
            async def listen(self,handle):
                for symbol in ['SPY','AAPL','MSFT','WRONG']:
                    await handle(quote(datetime.now(timezone.utc),symbol=symbol))
                await asyncio.Event().wait()
        class Source:
            def bars(self,symbol,timeframe,now):
                if symbol=='MSFT':raise ValueError('fixture source unavailable')
                return [{**row(i,100+i),'symbol':symbol} for i in range(10)]
            def websocket(self):return Stream()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            value=asyncio.run(shadow.watch(shadow.load_spec(),root/'run',root/'activity.json',1,
                             source=Source(),capital='1000',entry_budget='1000',symbols=['SPY','AAPL','MSFT']))
            self.assertEqual(Stream.subscriptions,[['SPY','AAPL','MSFT']])
            self.assertEqual([c['received_messages'] for c in value['companies']],[1,1,1])
            self.assertEqual(value['companies'][2]['bar_status'],'unavailable')
            self.assertEqual(value['companies'][0]['bar_status'],'available')
            journal=[json.loads(line) for line in (root/'run/events.jsonl').read_text().splitlines()]
            self.assertTrue(any(e['event_type']=='bars_unavailable' and e['payload']['symbol']=='MSFT' for e in journal))
            self.assertTrue(all(not c['decisions'] for c in value['companies']))


if __name__ == '__main__':unittest.main()
