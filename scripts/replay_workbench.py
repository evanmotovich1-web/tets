"""Read-only saved-bar research replay. Never connects to brokers or providers.

The existing independent SMA engine is deliberately the only execution adapter.
Unsupported saved strategies remain visible, never mapped to a different strategy.
"""
import hashlib
import json
import math
import re
from pathlib import Path
import sys
from decimal import Decimal, InvalidOperation
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from desk.backtest.contracts import canonical, strategy_spec

STATE_ROOTS = (ROOT / 'desk/runtime/.state', ROOT.parent / 'algo/desk/runtime/.state',
               Path.home() / 'code/algo/desk/runtime/.state')
MAX_BYTES = 16 * 1024 * 1024


def _read(path):
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Unsafe or oversized saved evidence')
    return json.loads(path.read_bytes())


def _validate_report(report):
    if (report.get('live_orders') is not False or report.get('promoting') is not False or
            report.get('classification') != 'historical_unverified' or
            not all(report.get('independent_replay', {}).get(k) is True for k in ('orders', 'fills', 'equity', 'metrics'))):
        raise ValueError('Unverified report')
    rows = report['bars']
    if not isinstance(rows, list) or not 2 <= len(rows) <= 10000 or report['denominator']['included_bars'] != len(rows):
        raise ValueError('Invalid bar denominator')
    previous = None
    for row in rows:
        opened, closed, available = (datetime.fromisoformat(row[k].replace('Z', '+00:00')) for k in ('bar_open_at', 'bar_close_at', 'available_at'))
        if not all(t.tzinfo for t in (opened, closed, available)) or not opened < closed <= available or (previous is not None and opened < previous):
            raise ValueError('Invalid or unordered timestamps')
        previous = closed
        if row['symbol'] != report['symbol']:
            raise ValueError('Mixed symbols')
        op, hi, lo, cl, volume = (Decimal(str(row[k])) for k in ('open', 'high', 'low', 'close', 'volume'))
        if not all(n.is_finite() and 0 <= n <= 1000000000 for n in (op, hi, lo, cl, volume)) or not 0 < lo <= min(op, cl) <= max(op, cl) <= hi:
            raise ValueError('Invalid OHLCV')
        if any(n != n.quantize(Decimal('.000001')) for n in (op, hi, lo, cl)):
            raise ValueError('Price precision exceeds replay adapter')


def _unsupported(spec):
    if spec.get('schema_version') != 2 or spec.get('family') != 'sma_cross_long':
        return 'Replay adapter supports saved v2 SMA long strategies only; no substitution.'
    if spec['execution']['order_type'] != 'market' or spec['position']['sizing'] != 'fixed_quantity':
        return 'Replay adapter requires market orders and saved fixed share quantity.'
    if any(v is not None for v in spec['exits'].values()):
        return 'Replay adapter does not implement this strategy’s additional exits.'
    return None


def _saved_id(spec):
    identity = spec.get('strategy_sha256')
    if type(identity) is str and re.fullmatch('[a-f0-9]{64}', identity):
        return identity
    # Preserve the invalid supplied hash in spec; only this catalog key is derived.
    # Validation below must still disable it, not silently repair the evidence.
    return hashlib.sha256(canonical(spec)).hexdigest()


def _catalog():
    datasets, strategies = {}, {}
    for root in STATE_ROOTS:
        paths = [root / 'tets/replay/backtest-report.json', root / 'workstation/backtest-report.json']
        paths += sorted((root / 'backtest-views').glob('*/report.json'))
        for path in paths:
            try:
                report = _read(path)
                if report.get('kind') != 'algo_backtest_report' or report.get('schema_version') != 1:
                    continue
                if report.get('report_sha256') != hashlib.sha256(canonical({k:v for k,v in report.items() if k != 'report_sha256'})).hexdigest():
                    continue
                _validate_report(report)
                rows = report['bars']
                if not rows:
                    continue
                identity = hashlib.sha256(canonical(rows)).hexdigest()
                provenance = dict(classification=report['classification'], point_in_time_verified=False,
                    report_sha256=report['report_sha256'], data_sha256=identity,
                    source_report=str(path), data_hash_format='RFC8785 canonical bars array',
                    note='Historical research, simulated fills; no edge claim.')
                datasets.setdefault(identity, dict(id=identity, symbol=report['symbol'], timeframe=report['timeframe'],
                    bar_count=len(rows), start=rows[0]['bar_open_at'], end=rows[-1]['bar_close_at'],
                    provenance=provenance, _rows=rows))
                spec = report['strategy']
                strategies.setdefault(_saved_id(spec), spec)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
                continue
        strategy_paths = sorted((root / 'library/bundles').glob('*/strategy.json'))
        strategy_paths += [root / 'tets/strategy.json']
        strategy_paths += sorted((root / 'tets/runs').glob('*/strategy.json'))
        strategy_paths += sorted((root / 'backtest-views').glob('*/strategy.json'))
        for path in strategy_paths:
            try:
                spec = _read(path)
                if isinstance(spec, dict):
                    identity = _saved_id(spec)
                    strategies.setdefault(identity, spec)
            except (OSError, ValueError, TypeError):
                continue
    entries = []
    for identity, spec in strategies.items():
        try:
            spec = strategy_spec(spec)
            reason = _unsupported(spec)
        except (ValueError, KeyError, TypeError, InvalidOperation):
            reason = 'Invalid or unsupported saved strategy specification/hash.'
        compatible = [d['id'] for d in datasets.values() if d['symbol'] == spec.get('symbol') and d['timeframe'] == spec.get('timeframe')]
        reason = reason or (None if compatible else 'No compatible saved dataset for this symbol/timeframe.')
        family = spec.get('family', 'unknown')
        entries.append(dict(id=identity, name=f"{family} {spec.get('parameters', {})} · {identity[:8]}",
            family=family, symbol=spec.get('symbol'), timeframe=spec.get('timeframe'),
            available=reason is None, reason=reason, dataset_ids=compatible, _spec=spec))
    return entries, list(datasets.values())


def library():
    strategies, datasets = _catalog()
    available = next((s for s in strategies if s['available']), None)
    return dict(state='available' if available else 'unavailable',
        strategies=[{k:v for k,v in s.items() if not k.startswith('_')} for s in strategies],
        datasets=[{k:v for k,v in d.items() if not k.startswith('_')} for d in datasets],
        defaults=dict(strategy_id=available['id'] if available else None,
            dataset_id=available['dataset_ids'][0] if available else None, capital=10000, slippage_bps=10),
        live_orders=False)


def run(payload):
    """Recompute, never scale or replay previously saved fills/P&L."""
    from desk.backtest.independent_replay import replay
    required = {'strategy_id', 'capital', 'slippage_bps'}
    if type(payload) is not dict or not required <= payload.keys() or payload.keys() - required - {'dataset_id'}:
        raise ValueError('Expected strategy_id, capital, slippage_bps and optional dataset_id')
    for field in ('strategy_id', 'dataset_id'):
        if field in payload and (type(payload[field]) is not str or not re.fullmatch('[a-f0-9]{64}', payload[field])):
            raise ValueError('Invalid saved identifier')
    for field, low, high in [('capital', 1, 100000000), ('slippage_bps', 0, 1000)]:
        value = payload[field]
        if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
            raise ValueError('Invalid ' + field)
    strategies, datasets = _catalog()
    strategy = next((s for s in strategies if s['id'] == payload['strategy_id']), None)
    if strategy is None or not strategy['available']:
        raise ValueError('Saved strategy unavailable')
    dataset_id = payload.get('dataset_id', strategy['dataset_ids'][0])
    if dataset_id not in strategy['dataset_ids']:
        raise ValueError('Saved dataset unavailable or incompatible')
    dataset = next(d for d in datasets if d['id'] == dataset_id)
    capital, slippage = payload['capital'], payload['slippage_bps']
    rows = dataset['_rows']
    result = replay(strategy['_spec'], rows, str(capital),
        dict(commission_bps='0', slippage_bps_per_side=str(slippage)), 6, 6)
    orders = {o['decision_at']: o for o in result['orders']}
    indices = {row['bar_open_at']: i for i, row in enumerate(rows)}
    bars = []
    for index, (row, point) in enumerate(zip(rows, result['equity'])):
        order = orders.get(row['bar_close_at'], {})
        previous_order = orders.get(rows[index-1]['bar_close_at'], {}) if index else {}
        if not isinstance(order, dict) or not isinstance(previous_order, dict):
            raise ValueError('Invalid replay order')
        execution_events = []
        if previous_order and previous_order['status'] != 'cancelled_terminal':
            execution_events.append(dict(side=previous_order['side'], status=previous_order['status'],
                                         timestamp=row['bar_open_at']))
        # The engine mutates orders to final outcomes. A close-time snapshot must
        # not reveal a fill/rejection until the next bar's execution has occurred.
        decision_status = ('cancelled_terminal' if index == len(rows)-1 else 'pending') if order else 'no_order'
        bars.append(dict(timestamp=row['bar_open_at'], bar_close_at=row['bar_close_at'],
            **{k:float(row[k]) for k in ('open', 'high', 'low', 'close', 'volume')},
            equity=float(point['equity']), cash=float(point['cash']), position=int(point['quantity']),
            decision=order.get('side', 'hold'), decision_status=decision_status, execution_events=execution_events))
    fills = []
    for fill in result['fills']:
        index = indices[fill['executed_at']]
        fills.append(dict(bar_index=index, timestamp=fill['executed_at'], side=fill['side'],
            quantity=int(fill['quantity']), reference_price=float(rows[index]['open']), price=float(fill['price']),
            slippage_cost=float(fill['slippage_cost']), fee=float(fill['commission'])))
    summary = {k:float(v) if isinstance(v, str) else v for k,v in result['metrics'].items()}
    summary.update(bar_count=len(rows), fill_count=len(fills),
        net_pnl=float(Decimal(result['metrics']['final_equity']) - Decimal(str(capital))))
    return dict(bars=bars, fills=fills, summary=summary,
        settings=dict(strategy_id=strategy['id'], dataset_id=dataset_id, capital=capital, slippage_bps=slippage,
            commission_bps=0, regulatory_fee_bps_on_sells=0, execution='next_bar_open',
            sizing='saved strategy unchanged; no auto scaling'),
        provenance=dict(dataset['provenance'], engine='desk.backtest.independent_replay',
            signal_basis='SMA uses prior closed bars, excludes current bar; fill at next available bar open.',
            fee_policy='Zero commission and regulatory fees; slippage applies per side.',
            terminal_policy='Mark open positions to final close; cancel final pending order.',
            price_scale=6, cash_scale=6),
        live_orders=False, promoting=False)


def ensure_seed():
    try:
        from desk.backtest.replay_seed import ensure_seeded
        ensure_seeded(ROOT / 'desk/runtime/.state')
    except Exception:
        pass


if __name__ == '__main__':
    ensure_seed()
