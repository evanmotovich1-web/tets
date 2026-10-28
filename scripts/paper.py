#!/usr/bin/env python3
"""Pinned AlpacaTrading installation and foreground prospective paper runner."""
import argparse
from datetime import datetime, timedelta, timezone, time as day_time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
try: import fcntl
except ImportError:
    class fcntl:
        LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8
        @staticmethod
        def flock(*_): pass
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import webbrowser
from zoneinfo import ZoneInfo
from onboarding import load_profile, public_settings, LIQUIDITY
from mac_compat import wheel_args

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'desk/runtime/.state/paper'
MODE = ROOT / 'desk/runtime/.state/install-mode.json'
UPSTREAM = 'https://github.com/mickpletcher/AlpacaTrading.git'
REVISION = '2a415e13bbc3fd48c48279edbe74e02c642d5ecf'
PAPER_URL = 'https://paper-api.alpaca.markets'
DATA_URL = 'https://data.alpaca.markets'
NY = ZoneInfo('America/New_York')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2); stream.write('\n')
    temporary.replace(path)


def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=300, **kwargs)


def git(*args):
    return run(['git', '-C', str(STATE / 'AlpacaTrading'), *args], capture_output=True, text=True).stdout.strip()


def check_source():
    target = STATE / 'AlpacaTrading'
    if target.is_symlink() or git('rev-parse', 'HEAD') != REVISION:
        raise ValueError('AlpacaTrading revision mismatch; files preserved')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise ValueError('AlpacaTrading source changed; files preserved')
    return target


def prepare():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = STATE / 'AlpacaTrading'
    if not target.exists():
        # A failed fetch remains visible and is never treated as an installed copy.
        run(['git', 'init', '-q', str(target)])
        run(['git', '-C', str(target), 'fetch', '--depth=1', UPSTREAM, REVISION])
        run(['git', '-C', str(target), 'checkout', '-q', '--detach', 'FETCH_HEAD'])
    check_source()
    venv = STATE / '.venv'
    if venv.is_symlink(): raise ValueError('Paper environment symlink refused')
    if not venv.exists(): run(['uv', 'venv', '--python', '3.13', str(venv)])
    # Only the upstream helper runtime is needed; no scheduler, webhook, or RSI bot.
    run(['uv', 'pip', 'sync', '--python', str(venv / 'bin/python'), *wheel_args(), '--require-hashes',
         str(ROOT / 'scripts/paper-requirements.lock')])
    run([str(venv / 'bin/python'), '-c', 'import alpaca, requests, dotenv, rfc8785, backtesting'])
    save(STATE / 'installed.json', dict(upstream=UPSTREAM, revision=REVISION, live_orders=False,
         dependency_sha256=hashlib.sha256((ROOT / 'scripts/paper-requirements.lock').read_bytes()).hexdigest()))
    print('Installed pinned AlpacaTrading paper helpers and isolated Python environment.', flush=True)


def check_install():
    check_source()
    installed = json.loads((STATE / 'installed.json').read_text())
    expected = hashlib.sha256((ROOT / 'scripts/paper-requirements.lock').read_bytes()).hexdigest()
    if installed.get('revision') != REVISION or installed.get('dependency_sha256') != expected:
        raise ValueError('Paper installation changed; rerun ALGO_INSTALL_MODE=paper make install')
    python = STATE / '.venv/bin/python'
    if not python.is_file(): raise ValueError('Paper Python environment is missing')
    return python


def configure(mode, interactive=True):
    if mode != 'paper': raise ValueError('Tets accepts paper mode only')
    if mode == 'paper' and interactive and not credentials():
        print('Create a FREE Alpaca Paper Only account, then generate PAPER API keys.')
        print('https://app.alpaca.markets/ — keys stay on this machine; never paste them in chat.')
        if os.environ.get('ALGO_INSTALL_NO_OPEN') != '1' and os.environ.get('TETS_NO_OPEN') != '1': webbrowser.open('https://app.alpaca.markets/')
        key = getpass.getpass('Paper API key (Enter to connect later): ').strip()
        if key:
            secret = getpass.getpass('Paper secret key: ').strip()
            if not secret: raise ValueError('Paper secret is required')
            save(STATE / 'credentials.json', dict(api_key=key, secret_key=secret))
    save(MODE, dict(mode=mode, broker='alpaca' if mode == 'paper' else 'robinhood' if mode == 'real' else None))
    print(f'Saved mode: {mode}. Live orders remain disabled.', flush=True)


def credentials():
    key, secret = os.environ.get('ALPACA_API_KEY'), os.environ.get('ALPACA_SECRET_KEY')
    if key and secret: return dict(api_key=key, secret_key=secret)
    path = STATE / 'credentials.json'
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError('Paper credentials must be a regular private file (chmod 600)')
        data = json.loads(path.read_text())
        if data.get('api_key') and data.get('secret_key'): return data
    return None


def load_helpers():
    target = check_source()
    # Verify tracked bytes, including the safety module, before importing upstream.
    sys.path.insert(0, str(target / 'Alpaca'))
    import alpaca_paper
    alpaca_paper.require_paper_mode(PAPER_URL, paper=True)
    if alpaca_paper.TRADING_BASE_URL != PAPER_URL or alpaca_paper.DATA_BASE_URL != DATA_URL:
        raise ValueError('Paper endpoints changed')
    return alpaca_paper


def strategy(profile=None):
    path = os.environ.get('ALGO_PAPER_STRATEGY')
    if not path:
        if profile:
            family = profile['family']
            if family == 'upstream_ema':
                return dict(label='Configured EMA 9/21 · unqualified', family=family, symbol=profile['symbol'],
                    timeframe=profile['timeframe'], quantity='1', sizing_budget=True,
                    strategy_sha256=hashlib.sha256(f"ema:9:21:{profile['symbol']}:{profile['timeframe']}".encode()).hexdigest())
            sys.path.insert(0, str(ROOT / 'desk'))
            from backtest.contracts import strategy_spec
            parameters = {'sma_cross_long':dict(fast_window=9,slow_window=21),
                          'momentum_long':dict(lookback_bars=10,enter_return_bps='20',exit_return_bps='-10'),
                          'mean_reversion_long':dict(lookback_bars=20,enter_z='-2',exit_z='0'),
                          'breakout_long':dict(entry_channel_bars=20,exit_channel_bars=10)}[family]
            spec = strategy_spec(dict(schema_version=2, family=family, symbol=profile['symbol'], timeframe=profile['timeframe'],
                                     parameters=parameters, position=dict(quantity='1'), rationale='User-selected paper preset; unqualified.'))
            return {**spec, 'quantity':'1', 'sizing_budget':True, 'label':f'Configured {family} · unqualified'}
        return dict(label='DEMO · upstream EMA 9/21 · unqualified', family='upstream_ema',
                    symbol='SPY', timeframe='1m', quantity='1', strategy_sha256='demo-ema-9-21')
    sys.path.insert(0, str(ROOT / 'desk'))
    from backtest.contracts import strategy_spec
    spec = strategy_spec(json.loads(Path(path).read_text()))
    if spec['schema_version'] != 2 or spec['family'] == 'post_earnings_drift_long':
        raise ValueError('Paper accepts v2 OHLCV strategies; earnings data is unavailable')
    if spec['position']['sizing'] != 'fixed_quantity' or Decimal(spec['position']['quantity']) > 100:
        raise ValueError('Paper requires fixed quantity of at most 100 shares')
    return {**spec, 'quantity': spec['position']['quantity'], 'label': f"Algo candidate · {spec['family']} · unqualified"}


def date(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def closed_bars(raw, symbol, timeframe, now):
    durations = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '1d': 23400}
    rows = []
    for bar in sorted(raw, key=lambda b: b['t']):
        opened = date(bar['t']); local = opened.astimezone(NY)
        if timeframe == '1d':
            opened = datetime.combine(local.date(), day_time(9, 30), NY).astimezone(timezone.utc)
        closed = min(opened + timedelta(seconds=durations[timeframe]),
                     datetime.combine(local.date(), day_time(16), NY).astimezone(timezone.utc))
        if local.weekday() >= 5 or (timeframe != '1d' and not day_time(9, 30) <= local.time() < day_time(16)):
            continue
        if closed > now - timedelta(seconds=2): continue
        if rows and rows[-1]['bar_open_at'] == opened.isoformat(): raise ValueError('Duplicate market bar')
        rows.append(dict(symbol=symbol, bar_open_at=opened.isoformat(), bar_close_at=closed.isoformat(),
                         available_at=now.isoformat(), **{k: str(bar[v]) for k, v in
                         [('open', 'o'), ('high', 'h'), ('low', 'l'), ('close', 'c'), ('volume', 'v')]}))
    return rows


def signal_for(helper, spec, rows, position, orders):
    if spec['family'] == 'upstream_ema':
        closes = [float(r['close']) for r in rows]
        fast, slow = helper.ema(closes, 9), helper.ema(closes, 21)
        if fast is None or slow is None: return None
        return 'sell' if position and fast < slow else 'buy' if not position and fast > slow else None
    from backtest.engine import family_signal
    filled_buys = [o for o in orders if o['side'] == 'buy' and o.get('filled_at')]
    entry = max((o['filled_at'] for o in filled_buys), default=None)
    entry_index = next((i for i, row in enumerate(rows) if entry and date(row['bar_close_at']) >= date(entry)), 0)
    return family_signal(spec, rows, len(rows) - 1, bool(position), entry_index,
                         Decimal(position['avg_entry_price']) if position else None)


def liquidity_check(rows, quote, now, level):
    result = dict(level=level, passed=False, spread_bps=None, average_bar_dollars=None, reason='Quote or warm-up data unavailable')
    if len(rows) < 20 or not quote: return result
    bid, ask = Decimal(str(quote.get('bp', 0))), Decimal(str(quote.get('ap', 0)))
    if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask: return result
    age = (now - date(quote['t'])).total_seconds()
    if not 0 <= age <= 10: return {**result, 'reason':'Quote stale or in the future'}
    spread = (ask - bid) / ((ask + bid) / 2) * 10000
    dollars = sum(Decimal(r['close']) * Decimal(r['volume']) for r in rows[-20:]) / 20
    limits = LIQUIDITY[level]
    passed = spread <= Decimal(limits['max_spread_bps']) and dollars >= Decimal(limits['min_bar_dollars'])
    return dict(level=level, passed=passed, spread_bps=str(spread.quantize(Decimal('.01'))),
                average_bar_dollars=str(dollars.quantize(Decimal('.01'))),
                reason='Liquidity checks passed' if passed else 'Spread or traded-dollar requirement not met')


def strategy_pnl(intents, quantity, mark):
    cash = Decimal(0)
    for intent in intents.values():
        order = intent.get('order', {})
        filled = Decimal(order.get('filled_qty') or '0')
        if filled:
            if not order.get('filled_avg_price'): return None
            cash += filled * Decimal(order['filled_avg_price']) * (-1 if intent['side'] == 'buy' else 1)
    return cash + quantity * mark


def review_entry(profile, spec, rows):
    config = profile['ai']
    if config['provider'] == 'none' or not config['consent']:
        return dict(status='disabled', decision='approve', reason='Rule-based entry; AI reviews off')
    budget_file = STATE / 'ai-budget.json'
    today = datetime.now(timezone.utc).date().isoformat()
    budget = json.loads(budget_file.read_text()) if budget_file.exists() else {}
    if budget.get('date') != today: budget = dict(date=today, calls=0)
    if budget['calls'] >= config['daily_call_limit']:
        return dict(status='budget_exhausted', decision=None, reason='Daily AI request limit reached; entry skipped')
    budget['calls'] += 1
    save(budget_file, budget)  # Reserve before invoking a possibly billable CLI.
    context = dict(mode='paper', symbol=spec['symbol'], family=spec['family'], technical_signal='buy',
                   capital_usd=profile['capital_usd'], profit_goal_usd=profile['profit_target_usd'],
                   goal_is_not_forecast=True, bars=[{k:r[k] for k in ('bar_close_at','close','volume')} for r in rows[-30:]])
    # No broker keys, account data, API keys or writable agent tools enter this process.
    env = {k:v for k,v in os.environ.items() if not any(part in k.upper() for part in
           ('ALPACA','APCA_','API_KEY','SECRET','TOKEN','ANTHROPIC_BASE_URL'))}
    try:
        response = subprocess.run(['node',str(ROOT/'desk/runtime/paper-agent.mjs')], cwd=ROOT/'desk', env=env,
            input=json.dumps(dict(config=config,context=context)), text=True, capture_output=True, timeout=40)
        if response.returncode or len(response.stdout) > 8192: raise ValueError('AI response invalid')
        result = json.loads(response.stdout)
        if result.get('decision') not in ('approve','hold',None) or result.get('status') not in ('reviewed','unavailable','disabled'):
            raise ValueError('AI response invalid')
        return {**result, 'calls_today':budget['calls'], 'daily_call_limit':config['daily_call_limit']}
    except Exception:
        return dict(status='unavailable', decision=None, reason='AI unavailable; entry skipped', calls_today=budget['calls'])


class Session:
    def __init__(self, helper, spec, profile=None, publish=None):
        self.helper, self.spec = helper, spec
        self.profile, self.publish = profile, publish
        self.liquidity, self.performance = None, None
        self.journal = STATE / ('journal-' + hashlib.sha256(spec['strategy_sha256'].encode()).hexdigest()[:16] + '.json')
        self.record = json.loads(self.journal.read_text()) if self.journal.exists() else dict(last_bar=None, intents={})
        self.events = self.record.setdefault('events', [])
        self.ai_key = hashlib.sha256(json.dumps(profile['ai'],sort_keys=True).encode()).hexdigest() if profile else None
        self.agent = self.record.get('agent') if self.record.get('agent_config_sha256') == self.ai_key else None
        self.agent = self.agent or dict(status='waiting_for_entry', decision=None, reason='This AI selection has not reviewed an entry yet')
        if profile and (profile['ai']['provider'] == 'none' or not profile['ai']['consent']):
            self.agent = dict(status='disabled', decision=None, reason='Rule-based run; AI reviews off')
        self.prefix = 'algo-' + hashlib.sha256(spec['strategy_sha256'].encode()).hexdigest()[:12] + '-'

    def request(self, method, path, **kwargs):
        self.helper.require_paper_mode(self.helper.TRADING_BASE_URL, paper=True)
        return self.helper.api_request(method, path, **kwargs)

    def event(self, kind, message):
        self.events.append(dict(at=datetime.now(timezone.utc).isoformat(), kind=kind, message=message))
        del self.events[:-40]

    def observe(self):
        spec, symbol = self.spec, self.spec['symbol']
        account = self.request('GET', '/v2/account')
        account_binding = hashlib.sha256(account['id'].encode()).hexdigest()
        if self.record.get('account_binding', account_binding) != account_binding:
            raise ValueError('Paper account changed; inspect strategy ledger')
        self.record['account_binding'] = account_binding
        clock = self.request('GET', '/v2/clock')
        positions = self.request('GET', '/v2/positions')
        # Open orders are fetched independently so a full history page cannot hide one.
        open_orders = self.request('GET', '/v2/orders', params={'status': 'open', 'limit': '500'})
        recent = self.request('GET', '/v2/orders', params={'status': 'all', 'limit': '100', 'direction': 'desc'})
        if len(open_orders) >= 500: raise ValueError('Open order inventory exceeds runner bound')
        if any(o['symbol'] == symbol and not o.get('client_order_id', '').startswith(self.prefix) for o in open_orders):
            blocked = 'External order on strategy symbol; monitoring only'
        else: blocked = None
        ours = [o for o in recent if o.get('client_order_id', '').startswith(self.prefix)]
        # An intent is persisted BEFORE submitting. Unknown outcomes never cause a retry.
        for client_id, intent in self.record['intents'].items():
            if intent.get('order', {}).get('status') in ('filled', 'canceled', 'expired', 'rejected'):
                continue
            order = self.request('GET', '/v2/orders:by_client_order_id', params={'client_order_id': client_id}, allow_not_found=True)
            if order:
                intent['order'] = {k: order.get(k) for k in ('side', 'qty', 'filled_qty', 'filled_at', 'status', 'filled_avg_price')}
            else: blocked = 'Order outcome unknown; inspect Alpaca before resuming'
        save(self.journal, self.record)
        owned = sum((Decimal(i.get('order', {}).get('filled_qty') or '0') *
                     (1 if i['side'] == 'buy' else -1) for i in self.record['intents'].values()), Decimal(0))
        position = next((p for p in positions if p['symbol'] == symbol), None)
        actual = Decimal(position['qty']) if position else Decimal(0)
        if owned != actual or actual < 0: blocked = 'Position differs from this strategy ledger; monitoring only'
        now = date(clock['timestamp'])
        params = dict(symbols=symbol, timeframe={'1m':'1Min','5m':'5Min','15m':'15Min','1h':'1Hour','1d':'1Day'}[spec['timeframe']],
                      start=(now - timedelta(days=1000 if spec['timeframe'] == '1d' else 30)).isoformat(),
                      end=now.isoformat(), limit='1000', sort='desc', feed='iex', adjustment='raw')
        data = self.request('GET', '/v2/stocks/bars', params=params, use_data_api=True)
        rows = closed_bars(data.get('bars', {}).get(symbol, []), symbol, spec['timeframe'], now)
        status, message = ('watching', 'Waiting for a new completed bar') if clock['is_open'] else ('market_closed', 'Market closed; waiting for the next session')
        last = rows[-1] if rows else None
        entry_reason = None
        if self.profile:
            if clock['is_open']:
                quotes = self.request('GET', '/v2/stocks/quotes/latest', params={'symbols':symbol,'feed':'iex'}, use_data_api=True)
                self.liquidity = liquidity_check(rows, quotes.get('quotes', {}).get(symbol), now, self.profile['liquidity'])
            mark = Decimal(position.get('current_price', last['close'] if last else '0')) if position else Decimal(0)
            pnl = strategy_pnl(self.record['intents'], owned, mark) if actual == owned else None
            target = Decimal(self.profile['profit_target_usd'])
            self.performance = dict(strategy_price_pnl_usd=str(pnl) if pnl is not None else None,
                                    profit_target_usd=str(target), target_remaining_usd=str(target-pnl) if pnl is not None and target else None,
                                    accounting='Broker fills plus marked owned shares; excludes fees')
            if pnl is not None and pnl <= -Decimal(self.profile['loss_stop_usd']): self.record['entry_halt'] = True
            if self.record.get('entry_halt'): entry_reason = 'Configured loss stop reached; new entries halted'
            elif pnl is None: entry_reason = 'Fill accounting unavailable; new entries paused'
            elif clock['is_open'] and not self.liquidity['passed']: entry_reason = self.liquidity['reason']
        if not rows: status, message = 'waiting_for_data', 'No completed regular-session IEX bars available'
        if last and not self.record['last_bar']:
            self.record['last_bar'] = last['bar_close_at']
            save(self.journal, self.record)  # Also baseline a new connection outside market hours.
            return self.projection(account, clock, positions, ours, last, 'paused' if blocked else 'entries_paused' if entry_reason else status, blocked or entry_reason or message)
        if rows and clock['is_open']:
            freshness = {'1m':180,'5m':420,'15m':1020,'1h':3720,'1d':4*86400}[spec['timeframe']]
            if (now - date(last['bar_close_at'])).total_seconds() > freshness:
                blocked = 'Market data stale; orders paused'
            bar_id = last['bar_close_at']
            if date(bar_id) > date(self.record['last_bar']):
                self.record['last_bar'] = bar_id
                side = signal_for(self.helper, spec, rows, position, ours)
                self.event('rule', f"{symbol} {spec['family']}: {(side or 'hold').upper()}")
                pending = any(o['symbol'] == symbol for o in open_orders)
                if side == 'buy' and entry_reason: blocked = entry_reason
                if side and not blocked and not pending:
                    if account.get('status') != 'ACTIVE' or any(account.get(k) for k in ('trading_blocked','account_blocked','trade_suspended_by_user')):
                        blocked = 'Paper account does not permit trading'
                    else:
                        quantity = Decimal(spec['quantity']) if side == 'buy' else actual
                        price = (Decimal(last['close']) * (Decimal('1.002') if side == 'buy' else Decimal('.998'))).quantize(
                            Decimal('.01'), rounding=ROUND_UP if side == 'buy' else ROUND_DOWN)
                        cap = min(Decimal('1000'), Decimal(self.profile['order_budget_usd']), Decimal(self.profile['capital_usd'])) if self.profile else Decimal('1000')
                        if side == 'buy' and self.profile and spec.get('sizing_budget'):
                            quantity = min(Decimal(100), (cap/price).to_integral_value(rounding=ROUND_DOWN)) if price > 0 else Decimal(0)
                        if price <= 0 or quantity <= 0 or (side == 'buy' and (price * quantity > cap or price * quantity > Decimal(account.get('cash','1000000000')))):
                            blocked = 'Order exceeds configured capital/entry budget ($1,000 hard cap), cash, or cannot buy one whole share'
                        else:
                            if side == 'buy' and self.profile:
                                active_ai = self.profile['ai']['consent'] and self.profile['ai']['provider'] != 'none'
                                self.event('ai' if active_ai else 'risk', 'Reviewing proposed paper entry' if active_ai else 'Rule-based entry within configured limits')
                                save(self.journal, self.record)
                                if active_ai and self.publish: self.publish(self.projection(account,clock,positions,ours,last,'ai_reviewing','Reviewing the proposed paper entry'))
                                self.agent = review_entry(self.profile, spec, rows)
                                self.record['agent'] = self.agent
                                self.record['agent_config_sha256'] = self.ai_key
                                self.event('ai', f"{self.agent['status']}: {self.agent.get('reason','')}")
                                if self.agent.get('decision') != 'approve':
                                    save(self.journal, self.record)
                                    return self.projection(account,clock,positions,ours,last,'entry_skipped','AI did not approve this entry; skipped')
                                if active_ai:
                                    clock = self.request('GET','/v2/clock')
                                    quotes = self.request('GET','/v2/stocks/quotes/latest',params={'symbols':symbol,'feed':'iex'},use_data_api=True)
                                    self.liquidity = liquidity_check(rows,quotes.get('quotes',{}).get(symbol),date(clock['timestamp']),self.profile['liquidity'])
                                    account = self.request('GET','/v2/account')
                                    refreshed_positions = self.request('GET','/v2/positions')
                                    refreshed_orders = self.request('GET','/v2/orders',params={'status':'open','limit':'500'})
                                    changed = any(p['symbol']==symbol and Decimal(p['qty'])!=0 for p in refreshed_positions) or any(o['symbol']==symbol for o in refreshed_orders)
                                    allowed = clock['is_open'] and self.liquidity['passed'] and not changed and \
                                              account.get('status')=='ACTIVE' and not any(account.get(k) for k in ('trading_blocked','account_blocked','trade_suspended_by_user')) and \
                                              Decimal(account.get('cash','0')) >= price*quantity
                                    if not allowed:
                                        self.event('risk','Market/broker checks changed during AI review; entry skipped')
                                        save(self.journal,self.record)
                                        return self.projection(account,clock,refreshed_positions,ours,last,'entry_skipped','Market/broker checks changed during AI review; entry skipped')
                                if self.publish and load_profile() != self.profile:
                                    return self.projection(account,clock,positions,ours,last,'settings_changed','Settings changed during review; entry skipped. Restart to apply.')
                            client_id = self.prefix + hashlib.sha256((bar_id + side).encode()).hexdigest()[:24]
                            self.record['intents'][client_id] = dict(side=side, bar=bar_id)
                            save(self.journal, self.record)
                            self.request('POST', '/v2/orders', payload=dict(symbol=symbol, qty=str(quantity), side=side,
                                         type='limit', limit_price=str(price), time_in_force='day', extended_hours=False,
                                         client_order_id=client_id))
                            self.event('order', f'{side.upper()} {quantity} {symbol}: submitted, fill unconfirmed')
                            message = f'{side.upper()} paper order submitted; awaiting broker confirmation'
            save(self.journal, self.record)
        if blocked: status, message = 'paused', blocked
        elif entry_reason: status, message = 'entries_paused', entry_reason
        save(self.journal, self.record)
        return self.projection(account, clock, positions, ours, last, status, message)

    def projection(self, account, clock, positions, ours, last, status, message):
        spec, symbol = self.spec, self.spec['symbol']
        return dict(kind='algo_paper_activity', schema_version=1, live_orders=False, mode='paper',
                    observed_at=datetime.now(timezone.utc).isoformat(), state=status, message=message,
                    strategy=spec['label'], strategy_sha256=spec['strategy_sha256'], symbol=symbol,
                    feed='Alpaca IEX · completed bars', market_open=clock['is_open'], next_open=clock.get('next_open'),
                    last_bar_at=last['bar_close_at'] if last else None, price=last['close'] if last else None,
                    account={k: account.get(k) for k in ('equity','cash','buying_power','last_equity')},
                    positions=[{k: p.get(k) for k in ('symbol','qty','avg_entry_price','current_price','unrealized_pl')} for p in positions],
                    orders=[{k: o.get(k) for k in ('symbol','side','qty','filled_qty','filled_avg_price','status','submitted_at','filled_at')} for o in ours],
                    upstream_revision=REVISION, promoting=False,
                    settings=public_settings(self.profile) if self.profile else None, ai=self.agent,
                    liquidity_check=self.liquidity, performance=self.performance, events=list(self.events))


def watch(activity, once=False):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / 'runner.lock').open('w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('Another paper runner already owns this checkout') from None
        session = None
        while True:
            try:
                profile = load_profile()
                # Allow the connection screen to recover as soon as the operator
                # enters keys with make paper-connect; no port-taking restart.
                if session is None:
                    creds = credentials()
                    if creds:
                        check_install()
                        os.environ.update(ALPACA_API_KEY=creds['api_key'], ALPACA_SECRET_KEY=creds['secret_key'])
                        session = Session(load_helpers(), strategy(profile), profile, lambda data:save(activity,data))
                if not session:
                    data = dict(kind='algo_paper_activity', schema_version=1, live_orders=False, mode='paper',
                                state='credentials_required', message='Run sh tets connect to enter your Alpaca paper API keys.',
                                observed_at=datetime.now(timezone.utc).isoformat())
                elif profile != session.profile:
                    data = dict(kind='algo_paper_activity',schema_version=1,live_orders=False,mode='paper',
                        state='settings_changed',message='Settings saved. Restart this workstation to apply them. Existing paper orders/positions remain at Alpaca.',
                        observed_at=datetime.now(timezone.utc).isoformat(), settings=public_settings(profile) if profile else None)
                else: data = session.observe()
            except Exception:
                # HTTP exceptions may contain private response data. Publish only this fixed message.
                data = dict(kind='algo_paper_activity', schema_version=1, live_orders=False, mode='paper',
                            state='connection_error', message='Paper API unavailable or rejected. Check paper keys, network and endpoint settings.',
                            observed_at=datetime.now(timezone.utc).isoformat())
            try:
                profile = profile if data.get('state') == 'settings_changed' else session.profile if session else load_profile()
                if profile and not data.get('settings'): data['settings'] = public_settings(profile)
                if profile and not data.get('ai'):
                    data['ai'] = dict(status='not_started',decision=None,reason='Waiting for broker connection and a valid entry')
            except ValueError: pass
            save(activity, data)
            if once: return data
            time.sleep(15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare','configure','watch'])
    parser.add_argument('--mode', choices=['real','paper','research'], default='paper')
    parser.add_argument('--non-interactive', action='store_true')
    parser.add_argument('--activity', type=Path, default=STATE / 'activity.json')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'prepare': prepare()
        elif args.action == 'configure': configure(args.mode, not args.non_interactive and sys.stdin.isatty())
        else: watch(args.activity, args.once)
    except KeyboardInterrupt: return 0
    except Exception as error:
        print(f'Paper setup failed ({type(error).__name__}); source and local credentials preserved.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__': sys.exit(main())
