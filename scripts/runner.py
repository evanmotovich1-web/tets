#!/usr/bin/env python3
"""Standalone foreground algo runner, source checks and read-only local status page."""
import argparse
import errno
from datetime import datetime, timezone
import fcntl
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
    if path.is_symlink() or path.stat().st_size > 262144: raise ValueError('Invalid status file')
    return json.loads(path.read_text())


def status():
    runner = read(STATE / 'runner.json') or {'state':'not_started','mode':config()['mode']}
    if runner.get('state') == 'running':
        try: os.kill(runner['pid'], 0)
        except (ProcessLookupError, PermissionError): runner = {**runner,'state':'stopped'}
    return dict(observed_at=stamp(), runner=runner, activity=read(ACTIVITY), settings=public_profile(),
                news=read(STATE/'news.json'), connections=read(STATE/'connections.json'))


def setup():
    mode = os.environ.get('TETS_MODE') or config()['mode']
    if sys.stdin.isatty() and 'TETS_MODE' not in os.environ:
        print('1) Yahoo shadow — no account or keys; virtual fills\n2) Alpaca paper — simulated broker execution')
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
    shadow.save(CONFIG,dict(mode=mode,news_sources=['yahoo_finance','yahoo_finance_rss','bbc_business_rss','alpaca_news_when_connected']))
    if mode == 'paper': connect()
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
        return dict(source='alpaca_paper',state='connected',live_orders=False,checked_at=stamp())
    except Exception as error:
        return dict(source='alpaca_paper',state='unavailable',error_type=type(error).__name__,live_orders=False)


def connect():
    # Reuse the exact private storage and hidden input flow from the reviewed paper runner.
    paper.configure('paper',interactive=sys.stdin.isatty())
    result=check_paper()
    shadow.save(STATE/'paper-connection.json',result)
    print(json.dumps(result))
    return result


def collect_news():
    profile=onboarding.load_profile() or onboarding.defaults()
    result=news.collect(profile['symbol'],paper.credentials())
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
        else: self.send_error(404); return
        self.send_response(200); self.send_header('Content-Type',content); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self,*_): pass


def open_server(port, allow_fallback=False):
    for candidate in range(port, min(port+32,65536)) if allow_fallback else [port]:
        try: return ThreadingHTTPServer(('127.0.0.1',candidate),Handler)
        except OSError as error:
            if error.errno != errno.EADDRINUSE or not allow_fallback: raise
    raise ValueError('No free local status port; set TETS_PORT to an available port')


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
        def refresh_news():
            while not stop.is_set():
                try: collect_news()
                except Exception: pass
                stop.wait(300)
        worker=threading.Thread(target=refresh_news,daemon=True);worker.start()
        activity=str(ACTIVITY); run_dir=STATE/'runs'/uuid.uuid4().hex
        if mode=='shadow':
            command=[sys.executable,str(ROOT/'scripts/shadow.py'),'watch','--spec',str(STATE/'strategy.json'),
                     '--seconds',str(seconds),'--state-dir',str(run_dir),'--activity',activity,
                     '--capital',profile['capital_usd'],'--entry-budget',profile['order_budget_usd']]
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
                print(f'Tets {mode}: {url}\nLogs: {STATE}/algo.log\nCtrl-C stops this test run.',flush=True)
                if open_browser: webbrowser.open(url)
                deadline=time.monotonic()+seconds
                while time.monotonic()<deadline:
                    code=child.poll()
                    if code is not None:
                        if code: raise ValueError('Algo process exited; inspect local algo.log')
                        break
                    time.sleep(.3)
        except KeyboardInterrupt: pass
        finally:
            signal.signal(signal.SIGTERM,previous)
            if child and child.poll()is None:
                os.killpg(child.pid,signal.SIGTERM)
                try: child.wait(timeout=20)
                except subprocess.TimeoutExpired: os.killpg(child.pid,signal.SIGKILL);child.wait()
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
    p.add_argument('action',choices=['setup','connect','doctor','news','start','status','self-test'])
    p.add_argument('--seconds',type=int,default=28800)
    p.add_argument('--no-open',action='store_true')
    p.add_argument('--offline',action='store_true')
    args=p.parse_args()
    if not 1<=args.seconds<=28800: p.error('seconds must be between 1 and 28800')
    if args.action=='setup':setup()
    elif args.action=='connect':connect()
    elif args.action=='doctor':doctor(not args.offline)
    elif args.action=='news':print(json.dumps(collect_news(),indent=2))
    elif args.action=='start':start(args.seconds,not args.no_open and os.environ.get('TETS_NO_OPEN')!='1')
    elif args.action=='status':print(json.dumps(status(),indent=2))
    else:self_test()


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.SubprocessError)as error:
        print(f'Tets: {type(error).__name__}: {error}',file=sys.stderr);sys.exit(1)
