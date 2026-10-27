"""Wizard persistence, allocation/liquidity enforcement and AI request boundaries."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import onboarding
import paper
from test_paper import Helper


class Market(Helper):
    def api_request(self, method, path, **kwargs):
        if path == '/v2/stocks/quotes/latest':
            return dict(quotes={'SPY':dict(bp='499.99',ap='500.01',t=self.clock['timestamp'])})
        if path == '/v2/stocks/bars':
            end = paper.date(self.clock['timestamp']).replace(second=0) - timedelta(minutes=1)
            return dict(bars={'SPY':[dict(t=(end-timedelta(minutes=25-i)).isoformat(),o=500,h=501,l=499,c=500,v=1000) for i in range(26)]})
        return super().api_request(method,path,**kwargs)


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        p=patch.object(paper,'STATE',self.root);p.start();self.addCleanup(p.stop)
        p=patch.object(onboarding,'PROFILE',self.root/'profile.json');p.start();self.addCleanup(p.stop)
        self.profile=onboarding.defaults();self.profile['family']='upstream_ema'
        self.helper=Market()
        self.spec=paper.strategy(self.profile)

    def session(self): return paper.Session(self.helper,self.spec,self.profile)
    def advance(self): self.helper.clock['timestamp']='2026-10-26T15:02:10Z'
    def posts(self): return [c for c in self.helper.calls if c[0]=='POST']

    def test_wizard_collects_all_requested_settings_and_saves_private(self):
        answers=iter(['1','3','QQQ','2','750','500','100','25','1'])
        with patch('builtins.input',side_effect=lambda _:next(answers)):
            profile=onboarding.wizard()
        self.assertEqual(profile['ai']['provider'],'none');self.assertEqual(profile['family'],'mean_reversion_long')
        self.assertEqual(profile['symbol'],'QQQ');self.assertEqual(profile['timeframe'],'5m')
        self.assertEqual(profile['capital_usd'],'750');self.assertEqual(profile['profit_target_usd'],'100')
        self.assertEqual(profile['liquidity'],'high');self.assertEqual(onboarding.load_profile(),profile)
        self.assertEqual(onboarding.PROFILE.stat().st_mode&0o777,0o600)

    def test_unattended_setup_never_enables_ai_or_overwrites_existing_settings(self):
        onboarding.wizard(False)
        self.assertFalse(onboarding.load_profile()['ai']['consent'])
        p=onboarding.load_profile();p['profit_target_usd']='123';onboarding.save_profile(p)
        onboarding.wizard(False);self.assertEqual(onboarding.load_profile()['profit_target_usd'],'123')

    def test_profile_rejects_invalid_capital_oversizing_and_external_endpoints(self):
        for key,value in [('capital_usd','NaN'),('capital_usd','-5'),('order_budget_usd','1001'),('loss_stop_usd','2000')]:
            p=deepcopy(self.profile);p[key]=value
            with self.assertRaises(ValueError): onboarding.validate(p)
        for endpoint in ['https://example.org/v1','http://user:pass@localhost/v1','http://localhost/v1?key=test','file:///tmp/x']:
            with self.assertRaises(ValueError): onboarding.local_endpoint(endpoint)

    def test_presets_use_canonical_contracts(self):
        for family in onboarding.FAMILIES:
            p=deepcopy(self.profile);p['family']=family
            spec=paper.strategy(p)
            self.assertEqual(spec['family'],family);self.assertEqual(spec['symbol'],'SPY')
            self.assertEqual(len(spec['strategy_sha256']),64)

    def test_capital_and_order_budget_change_actual_submitted_quantity(self):
        self.profile.update(capital_usd='800',order_budget_usd='750')
        s=self.session();s.observe();self.advance();result=s.observe()
        self.assertEqual(self.posts()[0][2]['payload']['qty'],'1')
        self.assertEqual(result['settings']['capital_usd'],'800')
        self.assertEqual(result['settings']['profit_target_usd'],'0')

    def test_budget_below_one_share_blocks_before_ai(self):
        self.profile.update(capital_usd='100',order_budget_usd='100')
        s=self.session();s.observe();self.advance()
        with patch.object(paper,'review_entry') as review:
            result=s.observe();review.assert_not_called()
        self.assertEqual(result['state'],'paused');self.assertEqual(self.posts(),[])

    def test_broker_cash_blocks_before_ai(self):
        self.helper.account['cash']='10'
        s=self.session();s.observe();self.advance()
        with patch.object(paper,'review_entry') as review:
            s.observe();review.assert_not_called()
        self.assertEqual(self.posts(),[])

    def test_liquidity_checks_reject_wide_stale_future_quotes_and_low_volume(self):
        now=paper.date(self.helper.clock['timestamp'])
        rows=[dict(close='500',volume='1000')]*20
        quote=dict(bp='499.99',ap='500.01',t=now.isoformat())
        self.assertTrue(paper.liquidity_check(rows,quote,now,'high')['passed'])
        for changed in [dict(ap='505'),dict(t=(now-timedelta(seconds=11)).isoformat()),dict(t=(now+timedelta(seconds=1)).isoformat())]:
            self.assertFalse(paper.liquidity_check(rows,{**quote,**changed},now,'high')['passed'])
        self.assertFalse(paper.liquidity_check([dict(close='500',volume='1')]*20,quote,now,'high')['passed'])

    def test_ai_veto_cannot_submit_and_is_persisted_as_real_activity(self):
        self.profile['ai'].update(provider='local',model='test-model',consent=True)
        s=self.session();s.observe();self.advance()
        with patch.object(paper,'review_entry',return_value=dict(status='reviewed',decision='hold',reason='Test veto')):
            result=s.observe()
        self.assertEqual(result['state'],'entry_skipped');self.assertEqual(self.posts(),[])
        self.assertTrue(any(e['kind']=='ai' for e in result['events']))
        self.assertEqual(result['ai']['decision'],'hold')

    def test_profit_goal_does_not_increase_quantity(self):
        self.profile['profit_target_usd']='999999'
        s=self.session();s.observe();self.advance();s.observe()
        self.assertEqual(self.posts()[0][2]['payload']['qty'],'1')

    def test_pnl_counts_filled_quantities_and_marks_owned_shares_only(self):
        intents={'buy':dict(side='buy',order=dict(filled_qty='2',filled_avg_price='500')),
                 'sell':dict(side='sell',order=dict(filled_qty='1',filled_avg_price='510'))}
        self.assertEqual(paper.strategy_pnl(intents,Decimal(1),Decimal(505)),Decimal(15))
        intents['buy']['order'].pop('filled_avg_price')
        self.assertIsNone(paper.strategy_pnl(intents,Decimal(1),Decimal(505)))

    def test_loss_stop_latches_and_prevents_new_entries(self):
        s=self.session()
        s.record['intents']={'b':dict(side='buy',order=dict(status='filled',filled_qty='1',filled_avg_price='600')),
                             's':dict(side='sell',order=dict(status='filled',filled_qty='1',filled_avg_price='500'))}
        result=s.observe();self.assertTrue(s.record['entry_halt']);self.assertEqual(result['performance']['strategy_price_pnl_usd'],'-100')
        self.advance();s.observe();self.assertEqual(self.posts(),[])

    def test_ai_call_budget_survives_restart_and_receives_no_broker_keys(self):
        p=self.profile;p['ai'].update(provider='local',model='test',consent=True,daily_call_limit=1)
        row=dict(bar_close_at='2026-10-26T15:00:00Z',close='500',volume='1000')
        def response(args,**kwargs):
            self.assertNotIn('ALPACA_API_KEY',kwargs['env']);self.assertNotIn('ANTHROPIC_API_KEY',kwargs['env'])
            self.assertNotIn('PRIVATE',kwargs['input'])
            return subprocess.CompletedProcess(args,0,stdout=json.dumps(dict(status='reviewed',decision='approve',reason='Test')))
        with patch.dict(os.environ,{'ALPACA_API_KEY':'PRIVATE','ANTHROPIC_API_KEY':'PRIVATE'}),patch.object(subprocess,'run',side_effect=response) as call:
            first=paper.review_entry(p,self.spec,[row]);second=paper.review_entry(p,self.spec,[row])
        self.assertEqual(first['decision'],'approve');self.assertEqual(second['status'],'budget_exhausted');self.assertEqual(call.call_count,1)

    def test_broker_changes_during_ai_review_invalidate_approval(self):
        self.profile['ai'].update(provider='local',model='test',consent=True)
        s=self.session();s.observe();self.advance()
        def approve(*args):
            self.helper.clock['is_open']=False
            return dict(status='reviewed',decision='approve',reason='Test approval')
        with patch.object(paper,'review_entry',side_effect=approve): result=s.observe()
        self.assertEqual(result['state'],'entry_skipped');self.assertEqual(self.posts(),[])

    def test_changing_model_does_not_claim_old_model_review_as_current_connection(self):
        self.profile['ai'].update(provider='local',model='new-model',consent=True)
        s=self.session();s.record['agent']=dict(status='reviewed',decision='approve',reason='Old selection')
        s.record['agent_config_sha256']='old-selection';paper.save(s.journal,s.record)
        restarted=self.session()
        self.assertEqual(restarted.agent['status'],'waiting_for_entry')

    def test_local_http_model_review_flows_through_real_node_wrapper_to_paper_intent(self):
        requests=[]
        class Model(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                requests.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                response=json.dumps(dict(model='offline-test-model',choices=[dict(message=dict(content=json.dumps(dict(decision='approve',reason='Offline adapter test'))))])).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(response)
        server=ThreadingHTTPServer(('127.0.0.1',0),Model)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.profile['ai'].update(provider='local',model='offline-test-model',consent=True,
                                  endpoint=f'http://127.0.0.1:{server.server_port}/v1')
        s=self.session();s.observe();self.advance();result=s.observe()
        self.assertEqual(len(requests),1);self.assertEqual(requests[0][0],'/v1/chat/completions')
        self.assertEqual(result['ai']['reported_model'],'offline-test-model')
        self.assertNotIn('test-account',json.dumps(requests));self.assertEqual(len(self.posts()),1)
        self.assertEqual(result['ai']['decision'],'approve')


if __name__=='__main__': unittest.main()
