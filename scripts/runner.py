#!/usr/bin/env python3
"""Standalone foreground algo runner, source checks and read-only local status page."""
import argparse
import errno
from datetime import datetime, timezone
try: import fcntl
except ImportError:
    class fcntl:
        LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8
        @staticmethod
        def flock(*_): pass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen
import uuid
import webbrowser

import onboarding
import paper
import shadow
import news
import company_data
import replay_bridge
import terminal_state

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'desk/runtime/.state/tets'
CONFIG = STATE / 'config.json'
ACTIVITY = STATE / 'activity.json'


def stamp(): return datetime.now(timezone.utc).isoformat()


def config():
    data = json.loads(CONFIG.read_text()) if CONFIG.exists() else {'mode':'shadow'}
    if data.get('mode') not in ('shadow','paper'): raise ValueError('Test mode must be shadow or paper')
    return data


def read(path):
    if not path.exists(): return None
    if path.is_symlink() or path.stat().st_size > 16*1024*1024: raise ValueError('Invalid status file')
    return json.loads(path.read_text())


def status():
    runner = read(STATE / 'runner.json') or {'state':'not_started','mode':config()['mode']}
    if runner.get('state') == 'running':
        try: os.kill(runner['pid'], 0)
        except (ProcessLookupError, PermissionError): runner = {**runner,'state':'stopped'}
    observer='not_running'
    lock=ACTIVITY.with_name(ACTIVITY.name+'.lock')
    if lock.is_file() and not lock.is_symlink():
        with lock.open('r') as handle:
            try: fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(handle,fcntl.LOCK_UN)
            except BlockingIOError: observer='active_writer'
    return dict(observed_at=stamp(), runner=runner, observer=observer, activity=read(ACTIVITY), settings=public_profile(),
                news=read(STATE/'news.json'), market=read(STATE/'market.json'), connections=read(STATE/'connections.json'),
                broker=terminal_state.broker(), alpaca_paper=terminal_state.alpaca_paper(),
                adw_fleet=read(STATE/'adw_latest_run.json') or read(ROOT/'desk/runtime/.state/harness2/adw_latest_run.json'))


def setup():
    mode = os.environ.get('TETS_MODE') or config()['mode']
    if sys.stdin.isatty() and 'TETS_MODE' not in os.environ:
        print('1) Yahoo shadow + local backtest engine — no API keys; virtual fills\n2) Alpaca paper account — optional; requires your paper-account keys')
        choice = input('Test mode [1/2, default 1]: ').strip() or '1'
        if choice not in ('1','2'): raise ValueError('Choose 1 or 2')
        mode = {'1':'shadow','2':'paper'}[choice]
    if mode not in ('shadow','paper'): raise ValueError('TETS_MODE must be shadow or paper')
    onboarding.wizard(sys.stdin.isatty())
    profile = onboarding.load_profile()
    if mode == 'shadow' and (profile['timeframe'] == '1d' or profile['family'] == 'upstream_ema'):
        raise ValueError('Yahoo shadow supports intraday OHLCV families. Choose paper for daily bars or upstream EMA.')
    spec = paper.strategy(profile)
    if mode == 'shadow':
        from_spec = {k:v for k,v in spec.items() if k not in ('quantity','label','sizing_budget')}
        shadow.save(STATE/'strategy.json', from_spec)
    symbols = config().get('watchlist', [profile['symbol'], 'AAPL', 'MSFT'])
    if mode == 'shadow':
        if 'TETS_SYMBOLS' in os.environ: symbols = os.environ['TETS_SYMBOLS']
        elif sys.stdin.isatty():
            symbols = onboarding.ask('Shadow watchlist (1–5 symbols, comma-separated; primary always included)',
                                     ','.join(shadow.watchlist(symbols, profile['symbol'])),
                                     lambda value:shadow.watchlist(value, profile['symbol']))
        symbols = shadow.watchlist(symbols, profile['symbol'])
    shadow.save(CONFIG,dict(mode=mode,watchlist=symbols,news_sources=['yahoo_finance','yahoo_finance_rss','bbc_business_rss','alpaca_news_when_connected']))
    if mode == 'paper': connect()
    elif sys.stdin.isatty() and os.environ.get('TETS_NO_ALPACA_WIZARD') != '1' and not paper.credentials():
        connect()
    print(f'Saved {mode} mode. Yahoo prices/news need no API key. Alpaca news reuses local paper keys when available.')


def check_paper(creds=None, opener=urlopen):
    creds = creds or paper.credentials()
    if not creds: return dict(source='alpaca_paper',state='credentials_required',live_orders=False)
    try:
        req=Request('https://paper-api.alpaca.markets/v2/account',headers={
            'APCA-API-KEY-ID':creds['api_key'],'APCA-API-SECRET-KEY':creds['secret_key']})
        with opener(req,timeout=15) as response:
            raw=response.read(65537)
            if len(raw)>65536: raise ValueError('oversized_account')
            account=json.loads(raw)
        if account.get('status')!='ACTIVE': return dict(source='alpaca_paper',state='account_not_active',live_orders=False)
        res = dict(source='alpaca_paper',state='connected',live_orders=False,checked_at=stamp())
        for k in ('equity','cash','buying_power'):
            if account.get(k) is not None:
                res[k] = str(account.get(k))
        return res
    except Exception as error:
        return dict(source='alpaca_paper',state='unavailable',error_type=type(error).__name__,live_orders=False)


def connect():
    # Reuse the exact private storage and hidden input flow from the reviewed paper runner.
    paper.configure('paper',interactive=sys.stdin.isatty())
    result=check_paper()
    shadow.save(STATE/'paper-connection.json',result)
    print(json.dumps(result))
    return result


def watch_symbols(profile=None):
    profile=profile or onboarding.load_profile() or onboarding.defaults()
    return shadow.watchlist(os.environ.get('TETS_SYMBOLS',config().get('watchlist',[profile['symbol']])),profile['symbol']) if config()['mode']=='shadow' else [profile['symbol']]


def collect_news():
    symbols=watch_symbols()
    result=news.collect_watchlist(symbols,paper.credentials())
    shadow.save(STATE/'news.json',result)
    return result


def doctor(network=True):
    profile=onboarding.load_profile() or onboarding.defaults()
    result=dict(checked_at=stamp(),mode=config()['mode'],profile=public_profile(),live_orders=False)
    if network:
        try:
            bars=shadow.YahooSource().bars(profile['symbol'],profile['timeframe'] if profile['timeframe']!='1d'else'1h',datetime.now(timezone.utc))
            result['yahoo_market_data']=dict(state='connected',symbol=profile['symbol'],completed_bars=len(bars),
                                           last_bar_at=bars[-1]['bar_close_at'],last_close=bars[-1]['close'])
        except Exception as error: result['yahoo_market_data']=dict(state='unavailable',error_type=type(error).__name__)
        result['alpaca_paper']=check_paper()
        result['news']=collect_news()
    else: result['network']='not_checked'
    result['installation']=dict(python=sys.version.split()[0],yfinance=shadow.importlib.metadata.version('yfinance'),
                                paper_environment_installed=(paper.STATE/'installed.json').exists(),openterminal_installed=False)
    shadow.save(STATE/'connections.json',result)
    print(json.dumps(result,indent=2))
    return result


def public_profile():
    return onboarding.public_settings(onboarding.load_profile() or onboarding.defaults())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/': body=(ROOT/'scripts/status.html').read_bytes(); content='text/html; charset=utf-8'
        elif self.path == '/api/status': body=json.dumps(status()).encode(); content='application/json'
        elif self.path == '/api/replay': body=json.dumps(replay_bridge.status()).encode(); content='application/json'
        elif self.path == '/api/terminal': body=json.dumps(terminal_state.snapshot()).encode(); content='application/json'
        elif self.path == '/api/broker': body=json.dumps(terminal_state.broker()).encode(); content='application/json'
        elif self.path == '/api/alpaca-paper': body=json.dumps(terminal_state.alpaca_paper()).encode(); content='application/json'
        elif self.path == '/api/broker/robinhood-redirect':
            terminal_state.save_broker({'broker':'robinhood','connected':True})
            self.send_response(302)
            self.send_header('Location','https://robinhood.com/login')
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            return
        elif self.path == '/replay':
            replay=replay_bridge.read()
            if not replay:self.send_error(404,'No verified historical replay on this Mac');return
            body=replay['html'];content='text/html; charset=utf-8'
        else: self.send_error(404); return
        self.send_response(200); self.send_header('Content-Type',content); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Content-Length',str(len(body)))
        if self.path=='/replay':
            self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
        self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        if self.path not in ('/api/models', '/api/broker', '/api/alpaca-paper'): self.send_error(404); return
        # Same-origin JSON only: no cross-site form submissions or arbitrary command execution.
        expected=f'http://127.0.0.1:{self.server.server_port}'
        if self.headers.get('Origin') != expected or self.headers.get('Host') != expected.removeprefix('http://'):
            self.send_error(403); return
        if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            self.send_error(415); return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 4096: raise ValueError('Invalid request size')
            payload=json.loads(self.rfile.read(length))
            result=(terminal_state.save_models(payload) if self.path == '/api/models'
                    else terminal_state.save_alpaca_paper(payload) if self.path == '/api/alpaca-paper'
                    else terminal_state.save_broker(payload))
        except (ValueError,TypeError):
            self.send_error(400,'Invalid request payload'); return
        body=json.dumps(result).encode()
        self.send_response(200); self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body))); self.end_headers();self.wfile.write(body)
    def log_message(self,*_): pass


def open_server(port, allow_fallback=False):
    for candidate in range(port, min(port+32,65536)) if allow_fallback else [port]:
        try: return ThreadingHTTPServer(('127.0.0.1',candidate),Handler)
        except OSError as error:
            if error.errno != errno.EADDRINUSE or not allow_fallback: raise
    raise ValueError('No free local status port; set TETS_PORT to an available port')


def refresh_sources(stop, profile):
    def headlines():
        while not stop.is_set():
            try: collect_news()
            except Exception: pass
            stop.wait(60)
    def companies():
        symbols=watch_symbols(profile)
        while not stop.is_set():
            values=[]
            for symbol in symbols:
                if stop.is_set():return
                values.append(company_data.collect(symbol))
                shadow.save(STATE/'market.json',dict(observed_at=stamp(),companies=values,refresh_seconds=3600))
            stop.wait(3600)
    for target in (headlines,companies): threading.Thread(target=target,daemon=True).start()


def dashboard(seconds, open_browser=True):
    """Reopen the UI over saved or running observations without starting another trader."""
    port=int(os.environ.get('TETS_PORT','8765'))
    if not 1024<=port<=65535: raise ValueError('Choose a port from 1024 to 65535')
    server=open_server(port,allow_fallback='TETS_PORT' not in os.environ)
    server.daemon_threads=True
    stop=threading.Event()
    refresh_sources(stop,onboarding.load_profile() or onboarding.defaults())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    print(f'Tets dashboard: {url}\nViewing existing observations; no additional trading worker started.',flush=True)
    if open_browser:webbrowser.open(url)
    try:stop.wait(seconds)
    except KeyboardInterrupt:pass
    finally:stop.set();server.shutdown();server.server_close()


def start(seconds, open_browser=True):
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (STATE/'runner.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('Another Tets runner already owns this checkout') from None
        mode=config()['mode']; profile=onboarding.load_profile() or onboarding.defaults()
        if read(STATE/'runner.json') and ACTIVITY.exists(): ACTIVITY.unlink()
        port=int(os.environ.get('TETS_PORT','8765'))
        if not 1024<=port<=65535: raise ValueError('Choose a port from 1024 to 65535')
        server=open_server(port, allow_fallback='TETS_PORT' not in os.environ)
        port=server.server_address[1]
        server.daemon_threads=True
        web=threading.Thread(target=server.serve_forever,daemon=True);web.start()
        stop=threading.Event()
        refresh_sources(stop,profile)
        activity=str(ACTIVITY); run_dir=STATE/'runs'/uuid.uuid4().hex
        if mode=='shadow':
            symbols = shadow.watchlist(os.environ.get('TETS_SYMBOLS',config().get('watchlist',[profile['symbol']])),profile['symbol'])
            spec_args = ['--spec',str(STATE/'strategy.json')] if (STATE/'strategy.json').is_file() else []
            command=[sys.executable,str(ROOT/'scripts/shadow.py'),'watch',*spec_args,
                     '--seconds',str(seconds),'--state-dir',str(run_dir),'--activity',activity,
                     '--capital',profile['capital_usd'],'--entry-budget',profile['order_budget_usd'],
                     '--symbols',','.join(symbols)]
        else:
            paper.check_install()
            command=[str(paper.STATE/'.venv/bin/python'),str(ROOT/'scripts/paper.py'),'watch','--activity',activity]
        child=None
        previous=signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            with (STATE/'algo.log').open('w') as log:
                child=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                shadow.save(STATE/'runner.json',dict(state='running',mode=mode,pid=os.getpid(),child_pid=child.pid,
                            started_at=stamp(),run_dir=str(run_dir),url=f'http://127.0.0.1:{port}',live_orders=False))
                url=f'http://127.0.0.1:{port}'
                ap=terminal_state.alpaca_paper()
                ap_state=ap.get('state','not_connected')
                eq_str=f"${ap['equity_usd']:,.2f}" if ap.get('equity_usd') is not None else 'not connected'
                cash_str=f"${ap['cash_usd']:,.2f}" if ap.get('cash_usd') is not None else 'not connected'
                print(f'Tets {mode}: {url}\nLogs: {STATE}/algo.log\nCtrl-C stops this test run.',flush=True)
                print(f'Alpaca Paper ({ap.get("endpoint","https://paper-api.alpaca.markets")}): state={ap_state} · equity={eq_str} · cash={cash_str}',flush=True)
                if open_browser: webbrowser.open(url)
                deadline=time.monotonic()+seconds
                seen_events=set()
                while time.monotonic()<deadline:
                    code=child.poll()
                    if code is not None:
                        if code: raise ValueError('Algo process exited; inspect local algo.log')
                        break
                    try:
                        act=read(ACTIVITY) or {}
                        for ev in (act.get('events') or [])[-10:]:
                            ekey=(ev.get('at'),ev.get('symbol'),ev.get('side'),ev.get('message'))
                            if ekey not in seen_events:
                                seen_events.add(ekey)
                                print(f"  [{ev.get('at','LIVE')}] [{str(ev.get('side') or ev.get('kind') or 'SIGNAL').upper()}] {ev.get('symbol','')} {ev.get('message','')}",flush=True)
                    except Exception:
                        pass
                    time.sleep(.3)
        except KeyboardInterrupt: pass
        finally:
            signal.signal(signal.SIGTERM,previous)
            if child and child.poll()is None:
                if hasattr(os,'killpg'): os.killpg(child.pid,signal.SIGTERM)
                else: child.terminate()
                try: child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    if hasattr(os,'killpg'): os.killpg(child.pid,signal.SIGKILL)
                    else: child.kill()
                    child.wait()
            stop.set();server.shutdown();server.server_close()
            shadow.save(STATE/'runner.json',dict(state='stopped',mode=mode,pid=os.getpid(),stopped_at=stamp(),live_orders=False))


def self_test():
    directory=STATE/'self-tests'/uuid.uuid4().hex
    result=subprocess.run(['node','runtime/tick.mjs','--event-id','fixture:tets','--provider','fixture','--state-dir',str(directory)],
        cwd=ROOT/'desk',capture_output=True,text=True,timeout=180,env={**os.environ,'TRADING_DESK_ALLOW_CLI_AUTHOR':'0',
        'TRADING_DESK_ALLOW_PAID_INFERENCE':'0','ALGO_DESK_ALLOW_LIVE_ORDERS':'0'})
    lines=[json.loads(line)for line in result.stdout.splitlines()if line.startswith('{')]
    value=lines[-1]if lines else {}
    shadow.save(STATE/'self-test.json',value)
    if result.returncode or value.get('state')!='completed': raise ValueError('Synthetic self-test did not complete')
    print(json.dumps(value,indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['setup','connect','alpaca','doctor','news','start','dashboard','status','self-test','verify-hardware','evolve','adw','visual','model','models','keys','tests','test'])
    p.add_argument('positional',nargs='*')
    p.add_argument('--seconds',type=int,default=28800)
    p.add_argument('--no-open',action='store_true')
    p.add_argument('--offline',action='store_true')
    p.add_argument('--provider',default=None)
    p.add_argument('--model',default=None)
    p.add_argument('--agent',default='traders')
    p.add_argument('--set',dest='assignments',action='append')
    p.add_argument('--reset',action='store_true')
    p.add_argument('--json',action='store_true')
    p.add_argument('--full',action='store_true')
    args=p.parse_args()
    if not 1<=args.seconds<=28800: p.error('seconds must be between 1 and 28800')
    if args.action=='setup':setup()
    elif args.action in ('connect','alpaca'):connect()
    elif args.action=='doctor':doctor(not args.offline)
    elif args.action=='news':print(json.dumps(collect_news(),indent=2))
    elif args.action=='start':start(args.seconds,not args.no_open and os.environ.get('TETS_NO_OPEN')!='1')
    elif args.action=='dashboard':dashboard(args.seconds,not args.no_open and os.environ.get('TETS_NO_OPEN')!='1')
    elif args.action=='status':print(json.dumps(status(),indent=2))
    elif args.action=='verify-hardware':
        import hardware
        hardware.verify(sys.modules[__name__])
    elif args.action=='evolve':
        import harness2_evolve
        harness2_evolve.main(['run','--k','2','--mode','parallel'])
    elif args.action in ('adw','visual','model','models','keys'):
        import harness2_evolve
        fwd=[args.action,*list(args.positional or []),'--agent',args.agent]
        if args.provider: fwd.extend(['--provider',args.provider])
        if args.model: fwd.extend(['--model',args.model])
        for item in args.assignments or []: fwd.extend(['--set',item])
        if args.reset: fwd.append('--reset')
        if args.json: fwd.append('--json')
        harness2_evolve.main(fwd)
    elif args.action in ('tests','test'):
        import harness2_evolve
        fwd=['tests']
        if args.full: fwd.append('--full')
        harness2_evolve.main(fwd)
    else:self_test()


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.SubprocessError)as error:
        print(f'Tets: {type(error).__name__}: {error}',file=sys.stderr);sys.exit(1)
