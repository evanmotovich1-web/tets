#!/usr/bin/env python3
"""Bounded read-only forward testing. No broker client, authentication or order API."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid

from market_data import YahooSource, INTERVALS, date, iso, normalize_quote, regular

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'desk/runtime/.state/shadow'
ACTIVITY = ROOT / 'desk/runtime/.state/tets/activity.json'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink(): raise ValueError('output_symlink_refused')
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with temp.open('wb') as stream:
        os.chmod(temp, 0o600); stream.write(canonical(value) + b'\n')
    temp.replace(path)


def install():
    venv = STATE / '.venv'
    if venv.is_symlink(): raise ValueError('shadow_environment_symlink_refused')
    if not venv.exists(): subprocess.run(['uv', 'venv', '--python', '3.14', str(venv)], check=True, timeout=120)
    lock = ROOT / 'scripts/shadow-requirements.lock'
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    marker = STATE / 'installed.json'
    if not marker.exists() or json.loads(marker.read_text()).get('dependency_sha256') != digest:
        subprocess.run(['uv', 'pip', 'sync', '--python', str(venv / 'bin/python'), '--require-hashes', str(lock)], check=True, timeout=180)
        save(marker, dict(dependency_sha256=digest, provider='yahoo_yfinance', live_orders=False))
    print('Installed yfinance 1.7.0 with pinned strategy-engine dependencies. No account or key required.')


def load_spec(path=None, symbol='SPY'):
    sys.path.insert(0, str(ROOT / 'desk'))
    from backtest.contracts import loads, strategy_spec
    if path:
        raw = Path(path).read_bytes()
        if len(raw) > 65536: raise ValueError('strategy_too_large')
        spec = strategy_spec(loads(raw))
    else:
        spec = strategy_spec(dict(schema_version=2, family='sma_cross_long', symbol=symbol, timeframe='1m',
            parameters=dict(fast_window=3, slow_window=8), position=dict(quantity='1'),
            execution=dict(order_type='market'), rationale='Unqualified demonstration candidate for prospective shadow observation.'))
    if spec['schema_version'] != 2 or spec['timeframe'] not in INTERVALS:
        raise ValueError('shadow_requires_v2_intraday_strategy')
    if spec['family'] == 'post_earnings_drift_long': raise ValueError('shadow_earnings_data_unavailable')
    if spec['position']['sizing'] == 'fixed_quantity' and Decimal(spec['position']['quantity']) > 100:
        raise ValueError('shadow_quantity_limit')
    return spec


class Shadow:
    """Canonical signal rules plus explicitly assumed, prospective quote fills."""
    def __init__(self, spec, started_at, emit=lambda *_: None, initial_cash='10000.00', entry_budget='1000'):
        sys.path.insert(0, str(ROOT / 'desk'))
        from backtest.engine import family_signal
        self.signal = family_signal
        self.spec, self.started_at, self.emit = spec, started_at, emit
        self.rows = []; self.known = {}; self.baselined = False; self.sequence = 0
        self.latest = None; self.last_decision = None; self.pending = None
        self.cash = Decimal(initial_cash); self.initial_cash = self.cash
        self.entry_budget = Decimal(entry_budget)
        if not self.cash.is_finite() or self.cash <= 0 or not self.entry_budget.is_finite() or not 0 < self.entry_budget <= 1000:
            raise ValueError('invalid_shadow_budget')
        self.quantity = Decimal(0); self.entry_price = None; self.entry_sequence = None
        self.realized = Decimal(0); self.fees = Decimal(0); self.fills = []; self.decisions = []
        self.received = 0; self.drops = Counter(); self.last_error = None
        self.bar_status = 'waiting_for_bars'; self.transport = 'connecting'
        self.slippage_bps = Decimal(10); self.commission_bps = Decimal(0)
        self.revision = False
        try: self.yfinance_version = importlib.metadata.version('yfinance')
        except importlib.metadata.PackageNotFoundError: self.yfinance_version = None

    def bars(self, rows, now):
        fields = ['symbol', 'bar_open_at', 'bar_close_at', 'open', 'high', 'low', 'close', 'volume']
        incoming = []
        for row in sorted(rows, key=lambda r: r['bar_close_at']):
            if row['symbol'] != self.spec['symbol']: raise ValueError('unexpected_symbol')
            if date(row['bar_close_at']) > now or date(row['available_at']) > now: raise ValueError('future_bar')
            if not regular(date(row['bar_open_at'])): raise ValueError('extended_hours_bar')
            key = row['bar_close_at']; content = {k: row[k] for k in fields}
            if key in self.known:
                if content != self.known[key]:
                    self.revision = True; self.pending = None
                    self.emit('bar_revision_detected', dict(bar_close_at=key))
                continue
            if self.rows and key <= self.rows[-1]['bar_close_at']: continue
            self.known[key] = content; incoming.append(row)
        self.bar_status = 'available'
        for row in incoming:
            self.rows.append(row); self.sequence += 1
            self.emit('bar_first_observed', row)
            if len(self.rows) > 600:
                old = self.rows.pop(0); self.known.pop(old['bar_close_at'], None)
        # A reconnect may return several missed bars at once. All are already
        # known now: evaluate the latest only, never replay missed trade intents.
        if self.baselined and incoming and date(incoming[-1]['bar_close_at']) > self.started_at:
            self.decide(incoming[-1], now)
        if not self.baselined:
            self.baselined = True
            self.emit('warmup_loaded', dict(rows=len(self.rows), last_bar=self.rows[-1]['bar_close_at'] if self.rows else None))

    def decide(self, row, now):
        if self.revision or self.pending or not regular(now): return
        if (now - date(row['bar_close_at'])).total_seconds() > max(120, INTERVALS[self.spec['timeframe']] * 2): return
        index = len(self.rows) - 1
        entry_index = self.entry_sequence - (self.sequence - len(self.rows)) if self.entry_sequence is not None else None
        side = self.signal(self.spec, self.rows, index, bool(self.quantity), entry_index, self.entry_price)
        decision = dict(decision_at=iso(now), bar_close_at=row['bar_close_at'], side=side or 'hold',
                        strategy_sha256=self.spec['strategy_sha256'])
        self.last_decision = iso(now); self.decisions.append(decision); self.decisions = self.decisions[-50:]
        self.emit('signal_observed', decision)
        if side:
            self.pending = dict(side=side, created_at=iso(now), reference_price=None, limit_price=None)

    def quote(self, message, now):
        self.received += 1
        try: quote = normalize_quote(message, self.spec['symbol'], now)
        except ValueError as error:
            self.drops[str(error)] += 1; return
        if self.latest and quote['quote_at'] <= self.latest['quote_at']:
            self.drops['duplicate_or_out_of_order'] += 1; return
        self.latest = quote; self.transport = 'connected'
        self.emit('quote_observed', quote)
        if not self.pending: return
        pending = self.pending
        age = (now - date(pending['created_at'])).total_seconds()
        if age > INTERVALS[self.spec['timeframe']] * 2:
            self.emit('virtual_intent_expired', pending); self.pending = None; return
        if self.revision or self.bar_status != 'available' or not regular(now) or not regular(date(quote['quote_at'])): return
        # A quote observed before the decision cannot be retroactively used as a fill.
        if quote['quote_at'] <= pending['created_at'] or quote['received_at'] <= pending['created_at']: return
        price = Decimal(quote['price']); buy = pending['side'] == 'buy'
        multiplier = 1 + self.slippage_bps / 10000 if buy else 1 - self.slippage_bps / 10000
        assumed = (price * multiplier).quantize(Decimal('.01'), rounding=ROUND_HALF_EVEN)
        if self.spec['execution']['order_type'] == 'limit':
            if pending['limit_price'] is None:
                offset = Decimal(self.spec['execution']['limit_offset_bps']) / 10000
                pending['limit_price'] = str(price * (1 + offset if buy else 1 - offset))
                return  # Need another later observation; touching the reference is not a fill.
            limit = Decimal(pending['limit_price'])
            if (buy and assumed > limit) or (not buy and assumed < limit): return
        quantity = (Decimal(self.spec['position']['notional_usd']) / assumed).to_integral_value(rounding=ROUND_DOWN) if buy and self.spec['position']['sizing'] == 'fixed_notional' else Decimal(self.spec['position'].get('quantity', '1')) if buy else self.quantity
        commission = (assumed * quantity * self.commission_bps / 10000).quantize(Decimal('.01'))
        if quantity <= 0 or (buy and assumed * quantity + commission > min(self.cash, self.entry_budget)):
            self.emit('virtual_intent_rejected', dict(reason='insufficient_virtual_cash_or_notional')); self.pending = None; return
        fill = dict(side=pending['side'], quantity=str(quantity), price=str(assumed), quote_price=quote['price'],
                    quote_at=quote['quote_at'], observed_at=quote['received_at'], decision_at=pending['created_at'],
                    commission=str(commission), fill_kind='assumed_quote_fill_not_broker_execution')
        self.cash += (-assumed * quantity if buy else assumed * quantity) - commission
        self.fees += commission
        if buy:
            self.quantity = quantity; self.entry_price = assumed; self.entry_sequence = self.sequence
        else:
            self.realized += (assumed - self.entry_price) * quantity - commission
            self.quantity = Decimal(0); self.entry_price = None; self.entry_sequence = None
        self.fills.append(fill); self.fills = self.fills[-50:]; self.pending = None
        self.emit('virtual_fill_assumed', fill)

    def snapshot(self, now, stopped=False):
        age = (now - date(self.latest['quote_at'])).total_seconds() if self.latest else None
        fresh = age is not None and 0 <= age <= 10
        state = 'bar_revision_paused' if self.revision else 'market_closed' if not regular(now) else 'waiting_for_bars' if not self.baselined else 'bars_unavailable' if self.bar_status != 'available' else 'shadow_watching' if fresh else 'stale_feed' if self.latest else 'waiting_for_quotes'
        price = Decimal(self.latest['price']) if self.latest else None
        equity = self.cash + self.quantity * price if price is not None else self.cash if not self.quantity else None
        return dict(schema_version=1, kind='algo_shadow_activity', mode='shadow', state='stopped' if stopped else state,
            observed_at=iso(now), started_at=iso(self.started_at), live_orders=False, broker_orders=False,
            qualification='unqualified_forward_observation', promoting=False, provider='yahoo_yfinance',
            source_repo='https://github.com/ranaroussi/yfinance', yfinance_version=self.yfinance_version,
            feed_status='fresh_indicative' if fresh else 'stale' if self.latest else self.transport,
            bar_status=self.bar_status, last_error=self.last_error, symbol=self.spec['symbol'],
            family=self.spec['family'], strategy_sha256=self.spec['strategy_sha256'], timeframe=self.spec['timeframe'],
            bars_seen=self.sequence, buffered_bars=len(self.rows), last_bar_at=self.rows[-1]['bar_close_at'] if self.rows else None,
            quote=self.latest, quote_age_seconds=age, received_messages=self.received, dropped=dict(self.drops),
            initial_cash=str(self.initial_cash), entry_budget=str(self.entry_budget), cash=str(self.cash), virtual_quantity=str(self.quantity),
            virtual_equity=str(equity.quantize(Decimal('.01'))) if equity is not None else None,
            virtual_realized_pnl=str(self.realized), pending=self.pending, fills=self.fills, decisions=self.decisions,
            cost_model=dict(slippage_bps_per_side=str(self.slippage_bps), commission_bps=str(self.commission_bps)),
            execution_model='assumed later-quote fills; not next-open backtest fills; no broker execution',
            feed_note='Unofficial indicative Yahoo data for personal research. Stream prices are not consolidated bid/ask.',
            bar_note='Closed regular-session OHLCV from Yahoo polling. Availability is first receipt time; warmup never trades.')


async def watch(spec, directory, activity, seconds, source=None, capital='10000.00', entry_budget='1000'):
    source = source or YahooSource()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (directory / 'writer.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (directory / 'run.json').exists(): raise ValueError('existing_run_refused_start_a_new_directory')
    activity.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = activity.with_name(activity.name + '.lock')
    if lock_path.is_symlink(): raise ValueError('activity_lock_symlink_refused')
    activity_lock = lock_path.open('a'); os.chmod(lock_path, 0o600)
    try: fcntl.flock(activity_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        activity_lock.close(); lock.close()
        raise ValueError('shadow_activity_writer_already_running') from None
    started = datetime.now(timezone.utc); run_id = uuid.uuid4().hex
    source_files = ['scripts/shadow.py', 'scripts/market_data.py', 'scripts/shadow-requirements.lock',
                    'desk/backtest/engine.py', 'desk/backtest/contracts.py']
    save(directory / 'run.json', dict(run_id=run_id, pid=os.getpid(), started_at=iso(started), seconds=seconds,
         strategy=spec, provider='yahoo_yfinance', live_orders=False,
         source_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_files},
         engine_sha256=hashlib.sha256((ROOT / 'desk/backtest/engine.py').read_bytes()).hexdigest()))
    save(directory / 'strategy.json', spec)
    journal = (directory / 'events.jsonl').open('xb'); os.chmod(journal.name, 0o600)
    previous = '0' * 64; count = 0
    def emit(kind, payload):
        nonlocal previous, count
        if count >= 100000 or journal.tell() > 64 * 1024 * 1024: raise RuntimeError('shadow_journal_budget')
        count += 1
        event = dict(run_id=run_id, sequence=count, previous_sha256=previous,
                     observed_at=iso(datetime.now(timezone.utc)), event_type=kind, payload=payload)
        previous = hashlib.sha256(canonical(event)).hexdigest()
        journal.write(canonical(dict(**event, event_sha256=previous)) + b'\n'); journal.flush()
    model = Shadow(spec, started, emit, initial_cash=capital, entry_budget=entry_budget); stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop.set)
    async def bars():
        while not stop.is_set():
            try:
                rows = await asyncio.to_thread(source.bars, spec['symbol'], spec['timeframe'], datetime.now(timezone.utc))
                model.bars(rows, datetime.now(timezone.utc)); model.last_error = None
            except Exception as error:
                model.bar_status = 'unavailable'; model.last_error = 'bars_' + type(error).__name__
                emit('bars_unavailable', dict(error_type=type(error).__name__))
            try: await asyncio.wait_for(stop.wait(), timeout=15)
            except asyncio.TimeoutError: pass
    async def quotes():
        while not stop.is_set():
            try:
                async with source.websocket() as ws:
                    await ws.subscribe([spec['symbol']]); model.transport = 'connected_waiting_for_quote'
                    async def handle(message): model.quote(message, datetime.now(timezone.utc))
                    await ws.listen(handle)
            except asyncio.CancelledError: raise
            except Exception as error:
                model.transport = 'disconnected'; model.last_error = 'stream_' + type(error).__name__
                emit('stream_unavailable', dict(error_type=type(error).__name__))
            try: await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError: pass
    tasks = [asyncio.create_task(bars()), asyncio.create_task(quotes())]
    finish = loop.time() + seconds
    try:
        while not stop.is_set() and loop.time() < finish:
            if model.received > 100000 or count >= 100000: raise RuntimeError('shadow_message_budget')
            if any(t.done() and t.exception() for t in tasks): raise RuntimeError('shadow_worker_failed')
            snapshot = model.snapshot(datetime.now(timezone.utc))
            save(activity, snapshot); save(directory / 'activity.json', snapshot)
            try: await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError: pass
    finally:
        stop.set()
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        final = model.snapshot(datetime.now(timezone.utc), stopped=True)
        save(activity, final); save(directory / 'activity.json', final)
        journal.flush(); os.fsync(journal.fileno()); journal.close(); activity_lock.close(); lock.close()
    print(json.dumps(dict(run_dir=str(directory), activity=str(activity), state=final['state'],
                         received_messages=final['received_messages'], bars_seen=final['bars_seen'], broker_orders=False)))
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'watch', 'probe'])
    parser.add_argument('--spec', type=Path)
    parser.add_argument('--symbol', default='SPY')
    parser.add_argument('--seconds', type=int)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--activity', type=Path, default=ACTIVITY)
    parser.add_argument('--capital', default='10000.00')
    parser.add_argument('--entry-budget', default='1000')
    args = parser.parse_args()
    if args.action == 'install': install(); return
    seconds = args.seconds if args.seconds is not None else 40 if args.action == 'probe' else 3600
    if not 1 <= seconds <= 8 * 3600: raise ValueError('shadow_duration_out_of_bounds')
    marker = STATE / 'installed.json'
    expected = hashlib.sha256((ROOT / 'scripts/shadow-requirements.lock').read_bytes()).hexdigest()
    if not marker.exists() or json.loads(marker.read_text()).get('dependency_sha256') != expected:
        raise ValueError('shadow_install_out_of_date_run_make_shadow_install')
    if importlib.metadata.version('yfinance') != '1.7.0': raise ValueError('shadow_dependency_version_mismatch')
    spec = load_spec(args.spec, args.symbol)
    directory = args.state_dir or STATE / 'runs' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6])
    final = asyncio.run(watch(spec, directory.resolve(), args.activity.resolve(), seconds, capital=args.capital, entry_budget=args.entry_budget))
    if args.action == 'probe' and (final['quote'] is None or not final['bars_seen']):
        raise ValueError('live_probe_not_proven_check_activity_status')


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'Shadow: {error}', file=sys.stderr); sys.exit(1)
