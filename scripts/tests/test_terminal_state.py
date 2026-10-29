"""The terminal reads canonical CLI artifacts and never substitutes model execution."""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import options_short_engine as engine
import runner
import terminal_state as terminal


class TerminalTests(unittest.TestCase):
    def test_missing_and_corrupt_runs_have_no_sample_returns_or_passes(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(terminal,'STATE',Path(temp)):
            self.assertEqual(terminal.research()['accounts'],[])
            path=Path(temp)/'tets/adw_latest_run.json';path.parent.mkdir();path.write_text('{broken')
            self.assertEqual(terminal.research()['state'],'unavailable')

    def test_saved_run_keeps_negative_returns_and_explicit_execution_limits(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(terminal,'STATE',Path(temp)):
            path=Path(temp)/'tets/adw_latest_run.json';path.parent.mkdir()
            path.write_text(json.dumps(dict(kind='algo_harness2_adw_pipeline_run', live_orders=False,broker_orders=False,
                shadow_fleet=dict(total_fleet_net_pnl='-12.34',accounts=[dict(net_pnl='-12.34')]),phases=[])))
            result=terminal.research();self.assertEqual(result['net_pnl'],'-12.34')
            self.assertIn('No model inference',result['model_execution']);self.assertIn('provenance unavailable',result['data_basis'])
            self.assertIsNone(result['all_phases_passed'])

    def test_ui_edits_are_read_by_cli_and_preserve_other_assignments(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(terminal,'STATE',Path(temp)):
            terminal.save_models({'assignments':{'trader_price_structure':'codex:custom-one'}})
            terminal.save_models({'assignments':{'trader_theta_mean_reversion':'openrouter:custom-two'}})
            roster=engine.load_sticky_model_roster(Path(temp)/'harness2/model_roster.json')
            models={r['agent_id']:r['primary_model'] for r in roster}
            self.assertEqual(models['trader_price_structure'],'codex:custom-one')
            self.assertEqual(models['trader_theta_mean_reversion'],'openrouter:custom-two')
            self.assertEqual(terminal.models()['state'],'saved')
            self.assertEqual(list((Path(temp)/'harness2').glob('.models-*')),[])

    def test_invalid_models_and_arbitrary_fields_cannot_mutate_settings(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(terminal,'STATE',Path(temp)):
            for value in [{'command':'shell'},{'assignments':{'all':'codex:gpt'}},
                          {'assignments':{'trader_price_structure':'claude:test'}},
                          {'assignments':{'trader_price_structure':None}}]:
                with self.assertRaises(ValueError):terminal.save_models(value)
            self.assertFalse((Path(temp)/'harness2/model_roster.json').exists())

    def test_library_is_read_only_and_reports_full_catalog_denominator(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'algo/desk/runtime/.state/library/catalog.sqlite';path.parent.mkdir(parents=True)
            conn=sqlite3.connect(path);conn.executescript('CREATE TABLE attempts(payload TEXT,verified INTEGER);CREATE TABLE strategies(hash TEXT);')
            conn.executemany('INSERT INTO attempts VALUES(?,0)',[(json.dumps(dict(id=str(i),status='failed')), )for i in range(35)])
            conn.execute("INSERT INTO strategies VALUES('one')");conn.commit();conn.close()
            with patch.object(terminal,'ROOT',root/'tets'):
                result=terminal.library();self.assertEqual(result['counts'],dict(attempts=35,strategies=1))
                self.assertEqual(len(result['attempts']),30);self.assertEqual(result['attempts'][0]['status'],'failed')
                self.assertFalse(result['attempts'][0]['catalog_verified'])

    def test_http_model_save_requires_same_origin_json_and_uses_fixed_action(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(terminal,'STATE',Path(temp)):
            server=ThreadingHTTPServer(('127.0.0.1',0),runner.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:'+str(server.server_port)
            payload=json.dumps({'assignments':{'trader_price_structure':'codex:test'}}).encode()
            def send(origin,content='application/json'):
                return urlopen(Request(base+'/api/models',data=payload,headers={'Origin':origin,'Content-Type':content}),timeout=5)
            try:
                for origin in ['https://attacker.example','null','http://localhost:'+str(server.server_port)]:
                    with self.assertRaises(HTTPError)as error:send(origin)
                    self.assertEqual(error.exception.code,403)
                with self.assertRaises(HTTPError)as error:send(base,'text/plain')
                self.assertEqual(error.exception.code,415)
                with send(base)as response:self.assertEqual(json.load(response)['state'],'saved')
                with urlopen(base+'/api/terminal')as response:
                    result=json.load(response);self.assertEqual(result['models']['state'],'saved')
                    self.assertEqual(result['research']['state'],'unavailable')
                    self.assertEqual(result['broker']['broker'],'robinhood')
                broker_payload=json.dumps({
                    'broker':'robinhood',
                    'execution_mode':'robinhood_paper_mirror',
                    'connected':True,
                    'total_account_equity_usd':40000.0,
                    'agent_tradable_pct':30.0,
                    'daily_loss_stop_pct':10.0,
                }).encode()
                with urlopen(Request(base+'/api/broker',data=broker_payload,headers={'Origin':base,'Content-Type':'application/json'}),timeout=5)as response:
                    b_res=json.load(response)
                    self.assertEqual(b_res['state'],'saved')
                    self.assertEqual(b_res['total_account_equity_usd'],40000.0)
                    self.assertEqual(b_res['agent_tradable_pct'],30.0)
                    self.assertEqual(b_res['agent_tradable_usd'],12000.0)
                    self.assertEqual(b_res['protected_reserve_usd'],28000.0)
                    self.assertEqual(b_res['daily_loss_stop_usd'],1200.0)
                    self.assertFalse(b_res['connected'])
                    self.assertEqual(b_res['status_message'],'Robinhood: not connected — live orders unavailable')
                    self.assertFalse(b_res['live_orders'])
                class NoRedirect(HTTPRedirectHandler):
                    def redirect_request(self, req, fp, code, msg, headers, newurl):
                        return None
                no_redirect_opener = build_opener(NoRedirect)
                with self.assertRaises(HTTPError) as redir_err:
                    no_redirect_opener.open(base + '/api/broker/robinhood-redirect', timeout=5)
                self.assertEqual(redir_err.exception.code, 302)
                self.assertEqual(redir_err.exception.headers.get('Location'), 'https://robinhood.com/login')
                redir_err.exception.close()
                with urlopen(base + '/api/broker') as response:
                    rh_after = json.load(response)
                    self.assertEqual(rh_after['broker'], 'robinhood')
                    self.assertFalse(rh_after['connected'])
                    self.assertEqual(rh_after['connection_status'], 'pending_login')
                    self.assertEqual(rh_after['status_message'], 'Robinhood: not connected — live orders unavailable')
                    self.assertFalse(rh_after['live_orders'])
                self.assertIn('Robinhood: not connected — live orders unavailable', (runner.ROOT / 'scripts/status.html').read_text())
                with urlopen(base+'/api/alpaca-paper')as response:
                    ap_res=json.load(response)
                    self.assertEqual(ap_res['state'],'not_connected')
                    self.assertFalse(ap_res['has_api_keys'])
                    self.assertIsNone(ap_res['equity_usd'])
                    self.assertIsNone(ap_res['today_pnl_usd'])
                    self.assertFalse(ap_res['live_orders'])
            finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
