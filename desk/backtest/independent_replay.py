"""Independent SMA/market-order reconstruction. Stdlib only, no desk engine imports."""
from decimal import Decimal as D, ROUND_HALF_EVEN


def replay(spec, rows, initial, costs, price_scale, cash_scale, decision_start_at=None):
    assert spec['family'] == 'sma_cross_long'
    assert spec['execution']['order_type'] == 'market'
    assert spec['position']['sizing'] == 'fixed_quantity'
    assert all(v is None for v in spec['exits'].values())
    quant = lambda value, scale: D(value).quantize(D(10) ** -scale, rounding=ROUND_HALF_EVEN)
    fmt = lambda value, scale: format(quant(value, scale), f'.{scale}f')
    quantity = int(spec['position']['quantity'])
    slip = D(costs['slippage_bps_per_side']) / 10000
    comm = D(costs['commission_bps']) / 10000
    reg = D(costs.get('regulatory_fee_bps_on_sells', '0')) / 10000
    cash, held, pending = D(initial), 0, None
    entry, entry_fee = D(0), D(0)
    fees = regulatory = slippage = realized = D(0)
    completed = 0
    orders, fills, equity = [], [], []
    for index, row in enumerate(rows):
        if pending:
            side = pending['side']
            raw = D(row['open'])
            price = quant(raw * (1 + slip if side == 'buy' else 1 - slip), price_scale)
            notional = price * quantity
            fee = quant(notional * comm, cash_scale)
            sell_fee = quant(notional * reg, cash_scale) if side == 'sell' else D(0)
            if side == 'buy' and notional + fee > cash:
                pending['status'] = 'rejected_insufficient_cash'
            else:
                pending.update(status='filled', executed_at=row['bar_open_at'])
                fill = dict(order_id=pending['order_id'], side=side, executed_at=row['bar_open_at'],
                    quantity=str(quantity), price=fmt(price, price_scale), notional=fmt(notional, cash_scale),
                    commission=fmt(fee, cash_scale), slippage_cost=fmt(abs(price-raw)*quantity, cash_scale))
                if reg: fill['regulatory_fee'] = fmt(sell_fee, cash_scale)
                fills.append(fill)
                if side == 'buy':
                    assert held == 0
                    cash -= notional + fee; held = quantity; entry = price; entry_fee = fee
                else:
                    assert held == quantity
                    cash += notional - fee - sell_fee
                    realized += (price-entry)*quantity - entry_fee - fee - sell_fee
                    held = 0; completed += 1
                fees += fee; regulatory += sell_fee; slippage += abs(price-raw)*quantity
            pending = None
        equity.append(dict(at=row['bar_close_at'], cash=fmt(cash,cash_scale), quantity=str(held),
            equity=fmt(cash+held*D(row['close']),cash_scale)))
        fast, slow = spec['parameters']['fast_window'], spec['parameters']['slow_window']
        if index < max(1, slow) or decision_start_at is not None and row['bar_close_at'] < decision_start_at: continue
        prior = [D(r['close']) for r in rows[:index]]
        f, s = sum(prior[-fast:])/fast, sum(prior[-slow:])/slow
        side = 'buy' if held == 0 and f > s else 'sell' if held and f <= s else None
        if side:
            pending = dict(order_id=len(orders)+1, side=side, quantity=str(quantity),
                decision_at=row['bar_close_at'], status='pending')
            orders.append(pending)
    if pending: pending['status'] = 'cancelled_terminal'
    metrics = dict(initial_cash=fmt(initial,cash_scale),final_cash=fmt(cash,cash_scale),
        final_equity=equity[-1]['equity'], realized_net_pnl=fmt(realized,cash_scale),
        unrealized_pnl=fmt(held*(D(rows[-1]['close'])-entry),cash_scale),
        commission_total=fmt(fees,cash_scale),regulatory_fee_total=fmt(regulatory,cash_scale),
        slippage_cost_total=fmt(slippage,cash_scale),completed_trades=completed,open_positions=int(held>0),
        rejected_orders=sum(o['status'].startswith('rejected_') for o in orders),
        cancelled_pending_orders=sum(o['status'].startswith('cancelled_') for o in orders))
    peak, drawdown = D(initial), D(0)
    for point in equity:
        value = D(point['equity']); peak = max(peak,value)
        drawdown = max(drawdown,(peak-value)/peak)
    normalized = lambda value: '0' if value == 0 else format(value.normalize(),'f')
    metrics['max_drawdown'] = normalized(quant(drawdown,12))
    metrics['total_return'] = normalized(quant((D(equity[-1]['equity'])-D(initial))/D(initial),12))
    return dict(orders=orders,fills=fills,equity=equity,metrics=metrics)
