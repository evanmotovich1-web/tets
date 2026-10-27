"""Live company snapshot semantics, per-company news and a read-only replay bridge."""
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import rfc8785
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import company_data
import news
import replay_bridge
import runner


class MarketTerminalTests(unittest.TestCase):
    def ticker(self,info):return type('Ticker',(),{'get_info':lambda _:info})()

    def test_equity_snapshot_records_share_basis_without_claiming_a_live_market_cap(self):
        result=company_data.collect('AAPL',self.ticker(dict(quoteType='EQUITY',longName='Apple',sharesOutstanding=1000,
            marketCap=200000,previousClose=200,regularMarketVolume=10000)))
        self.assertEqual(result['state'],'connected');self.assertEqual(result['shares_outstanding'],'1000')
        self.assertEqual(result['reported_market_cap'],'200000')
        self.assertNotIn('live_market_cap',result);self.assertIn('snapshot',result['note'])

    def test_etf_fund_assets_are_not_presented_as_equity_market_cap(self):
        result=company_data.collect('SPY',self.ticker(dict(quoteType='ETF',sharesOutstanding=999999,marketCap=999999,totalAssets=1000000)))
        self.assertIsNone(result['shares_outstanding']);self.assertIsNone(result['reported_market_cap'])
        self.assertEqual(result['fund_assets'],'1000000')

    def test_nonfinite_missing_or_failed_company_values_stay_unavailable(self):
        result=company_data.collect('AAPL',self.ticker(dict(quoteType='EQUITY',sharesOutstanding=float('nan'),marketCap=-1,volume=True)))
        self.assertIsNone(result['shares_outstanding']);self.assertIsNone(result['reported_market_cap']);self.assertIsNone(result['day_volume'])
        bad=type('Ticker',(),{'get_info':lambda _:(_ for _ in ()).throw(ValueError('sensitive provider internals'))})()
        result=company_data.collect('AAPL',bad)
        self.assertEqual(result['state'],'unavailable');self.assertNotIn('sensitive',json.dumps(result))

    def test_company_news_is_separate_and_broad_news_is_fetched_only_once(self):
        def collect(symbol,*_,**kwargs):return dict(symbol=symbol,sources=[dict(source='fixture',state='connected',items=[dict(title=symbol,url='https://example.org/'+symbol)])])
        with patch.object(news,'collect',side_effect=collect) as fetch,patch.object(news,'rss_feed',return_value=dict(source='bbc',state='empty',items=[])) as broad:
            result=news.collect_watchlist(['SPY','AAPL'])
        self.assertEqual([c['symbol']for c in result['companies']],['SPY','AAPL'])
        self.assertEqual(fetch.call_count,2);broad.assert_called_once();self.assertEqual(result['refresh_seconds'],60)

    def make_replay(self,root):
        data=dict(kind='algo_backtest_report',schema_version=1,live_orders=False,promoting=False,symbol='SPY',
                  window=dict(start='a',end='b'),denominator=dict(included_bars=2),created_at='fixture',
                  independent_replay=dict(orders=True,fills=True,equity=True,metrics=True))
        data['report_sha256']=hashlib.sha256(rfc8785.dumps(data)).hexdigest()
        folder=root/'desk/runtime/.state/tets/replay';folder.mkdir(parents=True)
        (folder/'backtest-report.json').write_text(json.dumps(data))
        (folder/'backtest-report.html').write_text('<script id="report-data" type="application/json">'+json.dumps(data)+'</script><p>Historical fixture</p>')
        return folder,data

    def test_replay_links_only_matching_digest_verified_historical_evidence(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(replay_bridge,'ROOT',Path(temp)):
            folder,data=self.make_replay(Path(temp));self.assertEqual(replay_bridge.status()['state'],'available')
            data['symbol']='AAPL';(folder/'backtest-report.json').write_text(json.dumps(data))
            self.assertIsNone(replay_bridge.read())

    def test_replay_refuses_mismatched_html_or_live_order_claims(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(replay_bridge,'ROOT',Path(temp)):
            folder,data=self.make_replay(Path(temp));(folder/'backtest-report.html').write_text('<p>Wrong report</p>')
            self.assertIsNone(replay_bridge.read())
        with tempfile.TemporaryDirectory() as temp,patch.object(replay_bridge,'ROOT',Path(temp)):
            folder,data=self.make_replay(Path(temp));data['live_orders']=True;(folder/'backtest-report.json').write_text(json.dumps(data))
            self.assertIsNone(replay_bridge.read())

    def test_replay_http_is_read_only_and_sandboxed_with_no_arbitrary_path(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(replay_bridge,'ROOT',Path(temp)):
            self.make_replay(Path(temp));server=ThreadingHTTPServer(('127.0.0.1',0),runner.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                with urlopen(base+'/api/replay')as response:self.assertEqual(json.load(response)['mode'],'historical_replay')
                with urlopen(base+'/replay')as response:
                    self.assertIn(b'Historical fixture',response.read());self.assertIn("connect-src 'none'",response.headers['Content-Security-Policy'])
            finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
