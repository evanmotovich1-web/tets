from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import runner
import news
import shadow
import paper
import release


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_news_normalizes_real_shape_deduplicates_and_refuses_unsafe_links(self):
        class Ticker:
            def get_news(self,**_):return [dict(content=dict(title='Headline',canonicalUrl=dict(url='https://example.org/a'),pubDate='2026-10-26T12:00:00Z'))]*2+[dict(content=dict(title='Unsafe',canonicalUrl=dict(url='javascript:alert(1)')))]
        result=news.collect('SPY',ticker=Ticker(),rss_sources=())
        self.assertEqual(len(result['sources'][0]['items']),1)
        self.assertEqual(result['sources'][1]['state'],'credentials_required')
        self.assertIn('do not trigger orders',result['use'])

    def test_news_authentication_stays_in_headers_and_errors_never_echo_keys(self):
        creds=dict(api_key='private-test-key',secret_key='private-test-secret')
        requests=[]
        def fail(req,**kwargs):
            requests.append(req)
            raise ValueError('private-test-key private-test-secret')
        ticker=type('Ticker',(),{'get_news':lambda *a,**k:[]})()
        result=news.collect('SPY',creds,ticker=ticker,opener=fail,rss_sources=())
        serialized=json.dumps(result)
        self.assertNotIn(creds['api_key'],serialized);self.assertNotIn(creds['secret_key'],serialized)
        self.assertEqual(result['sources'][1]['state'],'unavailable')
        self.assertNotIn(creds['api_key'],requests[0].full_url)
        self.assertEqual(requests[0].get_header('Apca-api-key-id'),creds['api_key'])

    def test_rss_news_has_normalized_timestamp_and_no_article_body(self):
        xml=b'<rss><channel><item><title>Business headline</title><link>https://example.org/a</link><pubDate>Mon, 26 Oct 2026 12:00:00 GMT</pubDate><description>Article body should not be kept</description></item></channel></rss>'
        result=news.rss_feed('bbc_business_rss','https://feeds.bbci.co.uk/news/business/rss.xml',lambda *a,**k:io.BytesIO(xml))
        self.assertEqual(result['state'],'connected')
        self.assertEqual(result['items'][0]['published_at'],'2026-10-26T12:00:00+00:00')
        self.assertNotIn('Article body',json.dumps(result))

    def test_paper_connect_verifies_paper_endpoint_without_orders_or_key_output(self):
        requests=[]
        class Response(io.BytesIO):pass
        def opener(req,**kwargs):requests.append(req);return Response(b'{"status":"ACTIVE"}')
        result=runner.check_paper(dict(api_key='test-key',secret_key='test-secret'),opener)
        self.assertEqual(result['state'],'connected')
        self.assertEqual(requests[0].full_url,'https://paper-api.alpaca.markets/v2/account')
        self.assertEqual(requests[0].get_method(),'GET')
        self.assertNotIn('test-key',json.dumps(result))

    def test_read_only_http_status_does_not_expose_credentials_or_file_paths(self):
        credentials=self.root/'credentials.json';credentials.write_text('{"secret_key":"private-value"}')
        with patch.object(runner,'STATE',self.root),patch.object(runner,'CONFIG',self.root/'config.json'),patch.object(runner,'ACTIVITY',self.root/'activity.json'):
            server=ThreadingHTTPServer(('127.0.0.1',0),runner.Handler)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                with urlopen(base+'/api/status')as response:data=json.load(response)
                self.assertEqual(data['runner']['state'],'not_started')
                self.assertNotIn('private-value',json.dumps(data))
                for path in ('/credentials.json','/../../credentials.json','/api/status?path=credentials.json'):
                    with self.assertRaises(HTTPError)as e:urlopen(base+path)
                    self.assertEqual(e.exception.code,404)
            finally:server.shutdown();server.server_close();worker.join()

    def test_profiles_have_different_limits_and_restore_from_disk(self):
        envfile=runner.ROOT/'scripts/env'
        for profile,expected in [('normal','3072'),('mac2017','1536')]:
            root=self.root/profile;state=root/'desk/runtime/.state/tets';state.mkdir(parents=True)
            (state/'install-profile.txt').write_text(profile+'\n')
            result=subprocess.run(['/bin/sh','-c','. "$ENV_FILE"; printf "%s" "$NODE_OPTIONS"'],
                env={'PATH':'/usr/bin:/bin','ALGO_ROOT':str(root),'ENV_FILE':str(envfile)},capture_output=True,text=True,check=True)
            self.assertIn(expected,result.stdout)

    def test_shadow_entry_budget_is_enforced_before_any_virtual_fill(self):
        spec=shadow.load_spec();start=datetime(2026,10,26,14,30,tzinfo=timezone.utc)
        model=shadow.Shadow(spec,start,initial_cash='1000',entry_budget='10')
        model.baselined=True;model.bar_status='available'
        model.pending=dict(side='buy',created_at=shadow.iso(start),limit_price=None,reference_price=None)
        later=start+timedelta(seconds=2)
        model.quote(dict(id='SPY',price=100,time=str(int(later.timestamp()*1000)),market_hours=1),later)
        self.assertEqual(model.fills,[]);self.assertEqual(model.cash,Decimal(1000))

    def test_no_real_money_mode(self):
        with self.assertRaises(ValueError):paper.configure('real',False)
        with patch.object(runner,'CONFIG',self.root/'config.json'):
            runner.CONFIG.write_text('{"mode":"real"}')
            with self.assertRaises(ValueError):runner.config()

    def test_source_inventory_excludes_all_runtime_keys_and_no_openterminal(self):
        files=release.source_files(runner.ROOT)
        for name in ('install.sh','install-2017-mac.sh','scripts/runner.py','scripts/news.py'):self.assertIn(name,files)
        self.assertFalse(any('.state/'in p or 'openterminal'in p.lower()for p in files))
        self.assertFalse(release.allowed('desk/runtime/.state/paper/credentials.json'))


if __name__=='__main__':unittest.main()
