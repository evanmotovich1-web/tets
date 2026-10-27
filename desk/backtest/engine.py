"""Pinned backtesting.py adapter. Narrow private broker hooks implement v1 costs.

The upstream broker owns order processing, execution timing and trade lifecycle.
Decimal below normalizes costs and independently reconciles observed engine fills.
It is not an alternative strategy/order simulator.
"""
from decimal import Decimal
from bisect import bisect_right
from functools import partial
import warnings
from zoneinfo import ZoneInfo

import backtesting
from backtesting import Backtest, Strategy
from backtesting.backtesting import _Broker
import pandas as pd

from .contracts import dec, norm, require, rounded, scaled, stamp, strategy_spec, obj

ENGINE_VERSION = "0.6.6"


def validate_v2_rows(rows, timeframe, *, observed_at=None):
    """Independent execution-boundary checks, including DST-aware regular hours."""
    from datetime import time
    durations = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 23400}
    previous = None
    for row in rows:
        opened, closed, available = (stamp(row[k]) for k in ("bar_open_at", "bar_close_at", "available_at"))
        require(opened < closed and available >= closed, "future_data")
        if observed_at is None:
            require(available == closed, "future_data")
        else:
            require(available <= stamp(observed_at), "future_data")
        local_open, local_close = (dt.astimezone(ZoneInfo("America/New_York")) for dt in (opened, closed))
        require(local_open.weekday() < 5 and local_open.date() == local_close.date() and
                time(9, 30) <= local_open.time() < local_close.time() <= time(16), "extended_hours_not_allowed")
        length = (closed - opened).total_seconds()
        require(length == durations[timeframe] or (timeframe == "1h" and length == 1800 and local_close.time() == time(16)), "timeframe_too_fast")
        require(previous is None or opened >= previous, "future_data")
        previous = closed


def validate_earnings(events, symbol):
    require(events is not None, "missing_dataset")
    require(type(events) is list and len(events) <= 10000, "parameter_out_of_range")
    previous = None
    for event in events:
        obj(event, ["available_at", "eps_actual", "eps_estimate", "session"], ["symbol"])
        available = stamp(event["available_at"])
        require(previous is None or available > previous, "future_data")
        previous = available
        require(event.get("symbol", symbol) == symbol, "symbol_out_of_scope")
        require(event["session"] in ("bmo", "amc"), "parameter_out_of_range")
        dec(event["eps_actual"], low=Decimal("-1000000"), high=Decimal("1000000"))
        dec(event["eps_estimate"], low=Decimal("-1000000"), high=Decimal("1000000"))
    return events


def family_signal(spec, rows, index, held, entry_index=None, entry_price=None, earnings_schedule=None):
    """Only this closed bar and earlier observations can affect a decision.

    Indicator windows exclude this bar. Earnings delay counts completed post-event
    bars: delay=1 decides at the first post-event close and fills at the next open.
    """
    p, family = spec["parameters"], spec["family"]
    row = rows[index]
    close = Decimal(row["close"])
    prior = rows[:index]
    enter = exit_signal = False
    trend = p.get("trend_filter_sma")
    trend_ok = trend is None or (len(prior) >= trend and close > sum(Decimal(r["close"]) for r in prior[-trend:]) / trend)
    if family == "sma_cross_long":
        if len(prior) >= p["slow_window"]:
            fast = sum(Decimal(r["close"]) for r in prior[-p["fast_window"]:]) / p["fast_window"]
            slow = sum(Decimal(r["close"]) for r in prior[-p["slow_window"]:]) / p["slow_window"]
            enter, exit_signal = fast > slow, fast <= slow
    elif family == "momentum_long":
        if len(prior) >= p["lookback_bars"]:
            change = (close / Decimal(prior[-p["lookback_bars"]]["close"]) - 1) * 10000
            enter, exit_signal = change > Decimal(p["enter_return_bps"]) and trend_ok, change < Decimal(p["exit_return_bps"])
    elif family == "mean_reversion_long":
        if len(prior) >= p["lookback_bars"]:
            values = [Decimal(r["close"]) for r in prior[-p["lookback_bars"]:]]
            mean = sum(values) / len(values)
            deviation = (sum((v - mean) ** 2 for v in values) / len(values)).sqrt()
            if deviation:
                z = (close - mean) / deviation
                enter, exit_signal = z <= Decimal(p["enter_z"]) and trend_ok, z >= Decimal(p["exit_z"])
    elif family == "breakout_long":
        if len(prior) >= max(p["entry_channel_bars"], p["exit_channel_bars"]):
            channel = prior[-p["entry_channel_bars"]:]
            volume_ok = p["min_volume_ratio"] is None or Decimal(row["volume"]) >= Decimal(p["min_volume_ratio"]) * sum(Decimal(r["volume"]) for r in channel) / len(channel)
            enter = close > max(Decimal(r["high"]) for r in channel) and volume_ok
            exit_signal = close < min(Decimal(r["low"]) for r in prior[-p["exit_channel_bars"]:])
    else:
        for first, event in earnings_schedule.get(index, []):
            # Do not inspect future event values to produce today's signal.
            if stamp(event["available_at"]) > stamp(row["bar_close_at"]):
                break
            estimate = Decimal(event["eps_estimate"])
            if estimate == 0 or first == 0:
                continue
            gap = (Decimal(rows[first]["open"]) / Decimal(rows[first - 1]["close"]) - 1) * 10000
            surprise = (Decimal(event["eps_actual"]) - estimate) / abs(estimate) * 100
            enter = surprise >= Decimal(p["min_surprise_pct"]) and (p["max_gap_bps"] is None or gap <= Decimal(p["max_gap_bps"]))
            if enter:
                break
        exit_signal = held and index - entry_index + 1 >= p["hold_bars"]
    if held:
        exits = spec["exits"]
        change = (close / entry_price - 1) * 10000
        exit_signal = exit_signal or (exits["stop_loss_bps"] is not None and change <= -Decimal(exits["stop_loss_bps"]))
        exit_signal = exit_signal or (exits["take_profit_bps"] is not None and change >= Decimal(exits["take_profit_bps"]))
        exit_signal = exit_signal or (exits["max_hold_bars"] is not None and index - entry_index + 1 >= exits["max_hold_bars"])
    return "sell" if held and exit_signal else "buy" if not held and enter else None


def simulate(spec, manifest, rows, initial_cash, cost_model, earnings_events=None, decision_start_at=None):
    require(backtesting.__version__ == ENGINE_VERSION, "engine_version_mismatch")
    v2 = spec.get("schema_version") == 2
    earnings_schedule = {}
    if v2:
        spec = strategy_spec(spec)
        require(manifest["symbols"] == [spec["symbol"]] and manifest["timeframe"] == spec["timeframe"], "strategy_dataset_mismatch")
        require(rows and all(row["symbol"] == spec["symbol"] for row in rows), "symbol_out_of_scope")
        observed_at = None
        if manifest.get("classification") == "historical_unverified":
            # Hindsight research replay only. Stored availability stays at
            # fetch time; this does not claim the rows were known at signal time.
            from .maverick_import import validate_manifest
            observed_at = validate_manifest(manifest)
        validate_v2_rows(rows, spec["timeframe"], observed_at=observed_at)
        if spec["family"] == "post_earnings_drift_long":
            earnings_events = validate_earnings(earnings_events, spec["symbol"])
            opens = [stamp(row["bar_open_at"]) for row in rows]
            for event in earnings_events:
                first = bisect_right(opens, stamp(event["available_at"]))
                decision = first + spec["parameters"]["entry_delay_bars"] - 1
                if decision < len(rows):
                    earnings_schedule.setdefault(decision, []).append((first, event))
    price_scale, cash_scale = manifest["price_scale"], manifest["cash_scale"]
    quantity = int(Decimal(spec["position"].get("quantity", "1")))
    commission = Decimal(cost_model["commission_bps"]) / 10000
    slippage = Decimal(cost_model["slippage_bps_per_side"]) / 10000
    regulatory = Decimal(cost_model.get("regulatory_fee_bps_on_sells", "0")) / 10000
    cash = Decimal(initial_cash)
    orders, fills = [], []
    admission_cash = cash
    broker_instances = []
    entry_index = entry_price = None

    def execution_price(raw, side):
        return rounded(Decimal(str(raw)) * (1 + slippage if side == "buy" else 1 - slippage), price_scale)

    def fee(price, side="buy"):
        return rounded(price * quantity * commission, cash_scale) + (
            rounded(price * quantity * regulatory, cash_scale) if side == "sell" else Decimal(0))

    def pending():
        require(orders and orders[-1]["status"] == "pending", "engine_order_invariant")
        return orders[-1]

    def record_fill(price, side, index):
        nonlocal admission_cash
        admission_cash += (-price * quantity if side == "buy" else price * quantity) - fee(price, side)
        order = pending()
        require(order["side"] == side, "engine_side_invariant")
        order["status"] = "filled"
        order["executed_at"] = rows[index]["bar_open_at"]
        fills.append({"order_id": order["order_id"], "side": side,
                      "executed_at": rows[index]["bar_open_at"], "quantity": str(quantity),
                      "price": scaled(price, price_scale), "notional": scaled(price * quantity, cash_scale),
                      "commission": scaled(rounded(price * quantity * commission, cash_scale), cash_scale),
                      **({"regulatory_fee": scaled(rounded(price * quantity * regulatory, cash_scale) if side == "sell" else 0, cash_scale)} if regulatory else {}),
                      "slippage_cost": scaled(abs(price - Decimal(rows[index]["open"])) * quantity, cash_scale)})

    class CostBroker(_Broker):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            broker_instances.append(self)

        def _adjusted_price(self, size=None, price=None):
            require(size is None or size > 0, "shorts_unsupported")
            return float(execution_price(self.last_price if price is None else price, "buy"))

        def _process_orders(self):
            nonlocal quantity
            # Upstream uses margin cash and floating admission. Check exact rounded
            # cost before delegation, with commission=0 in the upstream broker.
            for order in list(self.orders):
                require(order.limit is None and order.stop is None, "nonmarket_order_unsupported")
                if v2:
                    raw = Decimal(str(self._data.Open[-1]))
                    side = "buy" if order.parent_trade is None else "sell"
                    price = execution_price(raw, side)
                    if spec["execution"]["order_type"] == "limit":
                        offset = Decimal(spec["execution"]["limit_offset_bps"]) / 10000
                        limit = rounded(raw * (1 + offset if side == "buy" else 1 - offset), price_scale)
                        # Bounded open-only fill model: no optimistic intrabar fills.
                        if (side == "buy" and price > limit) or (side == "sell" and price < limit):
                            pending()["status"] = "cancelled_limit"
                            self.orders.remove(order)
                            continue
                    if side == "buy" and spec["position"]["sizing"] == "fixed_notional":
                        quantity = int(Decimal(spec["position"]["notional_usd"]) // price)
                        pending()["quantity"] = str(quantity)
                        if quantity == 0:
                            pending()["status"] = "rejected_insufficient_notional"
                            self.orders.remove(order)
                            continue
                        order._replace(size=quantity)
                if order.parent_trade is None:
                    require(not self.trades and order.size == quantity, "engine_position_invariant")
                    price = execution_price(self._data.Open[-1], "buy")
                    if rounded(price * quantity, cash_scale) + fee(price) > admission_cash:
                        pending()["status"] = "rejected_insufficient_cash"
                        self.orders.remove(order)
            super()._process_orders()

        def _open_trade(self, price, size, sl, tp, time_index, tag):
            nonlocal entry_index, entry_price
            require(size == quantity and sl is None and tp is None, "engine_position_invariant")
            super()._open_trade(price, size, sl, tp, time_index, tag)
            charge = fee(Decimal(str(price)))
            self._cash -= float(charge)
            record_fill(Decimal(str(price)), "buy", time_index)
            entry_index, entry_price = time_index, Decimal(str(price))

        def _close_trade(self, trade, price, time_index):
            adjusted = execution_price(price, "sell")
            entry_fee = fee(Decimal(str(trade.entry_price)))
            super()._close_trade(trade, float(adjusted), time_index)
            self._cash -= float(fee(adjusted, "sell"))
            self.closed_trades[-1]._commissions = float(entry_fee + fee(adjusted, "sell"))
            record_fill(adjusted, "sell", time_index)

    class SmaLong(Strategy):
        def init(self):
            pass

        def next(self):
            index = len(self.data) - 1
            if decision_start_at is not None and rows[index]["bar_close_at"] < decision_start_at:
                return
            require(not self.orders, "engine_unsettled_order")
            if v2:
                side = family_signal(spec, rows, index, bool(self.position), entry_index, entry_price, earnings_schedule)
            else:
                slow, fast = spec["parameters"]["slow_window"], spec["parameters"]["fast_window"]
                if len(self.data) < slow:
                    return
                # Preserve v1's current-bar SMA semantics and exact output.
                fast_mean = self.data.Close[-fast:].mean()
                slow_mean = self.data.Close[-slow:].mean()
                side = "buy" if not self.position and fast_mean > slow_mean else (
                    "sell" if self.position and fast_mean <= slow_mean else None)
            if side is None:
                return
            order_id = len(orders) + 1
            orders.append({"order_id": order_id, "side": side, "quantity": str(quantity),
                           "decision_at": rows[index]["bar_close_at"], "status": "pending"})
            if side == "buy":
                self.buy(size=1 if v2 and spec["position"]["sizing"] == "fixed_notional" else quantity, tag=order_id)
            else:
                self.position.close()

    frame = pd.DataFrame({key.title(): [float(r[key]) for r in rows]
                          for key in ["open", "high", "low", "close", "volume"]},
                         index=pd.to_datetime([r["bar_open_at"] for r in rows]))
    backtest = Backtest(frame, SmaLong, cash=float(cash), commission=0, spread=0,
                        margin=1, trade_on_close=False, hedging=False,
                        exclusive_orders=False, finalize_trades=False)
    # Preserve all engine settings. Do not replace its run or order-processing loop.
    backtest._broker = partial(CostBroker, **backtest._broker.keywords)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Some trades remain open.*")
        stats = backtest.run()
    require(len(broker_instances) == 1, "engine_instance_invariant")
    broker = broker_instances[0]
    # Any unexpected upstream cancellation is not silently accepted.
    for order in orders:
        if order["status"] == "pending":
            require(order["decision_at"] == rows[-1]["bar_close_at"], "unexpected_engine_rejection")
            order["status"] = "cancelled_terminal"
    for order in list(broker.orders):
        order.cancel()

    # Reconcile fills, not signals, to an exact rounded ledger for persistence.
    equity, held, entry, entry_commission = [], 0, Decimal(0), Decimal(0)
    total_fees = total_regulatory = total_slippage = realized = Decimal(0)
    closed_trades = 0
    peak, max_drawdown = cash, Decimal(0)
    fills_by_time = {}
    for fill in fills:
        fills_by_time.setdefault(fill["executed_at"], []).append(fill)
    for index, row in enumerate(rows):
        for fill in fills_by_time.get(row["bar_open_at"], []):
            price, charge, notional = map(Decimal, (fill["price"], fill["commission"], fill["notional"]))
            reg_charge = Decimal(fill.get("regulatory_fee", "0"))
            charge += reg_charge
            if fill["side"] == "buy":
                require(held == 0, "ledger_position_invariant")
                cash -= notional + charge
                held, entry, entry_commission = int(fill["quantity"]), price, charge
            else:
                require(held == int(fill["quantity"]), "ledger_position_invariant")
                cash += notional - charge
                realized += (price - entry) * held - entry_commission - charge
                held = 0
                closed_trades += 1
            total_fees += Decimal(fill["commission"])
            total_regulatory += reg_charge
            total_slippage += Decimal(fill["slippage_cost"])
        marked = rounded(cash + held * Decimal(row["close"]), cash_scale)
        peak = max(peak, marked)
        max_drawdown = max(max_drawdown, (peak - marked) / peak)
        equity.append({"at": row["bar_close_at"], "cash": scaled(cash, cash_scale),
                       "quantity": str(held), "equity": scaled(marked, cash_scale)})
        engine_equity = Decimal(str(stats["_equity_curve"]["Equity"].iloc[index]))
        require(abs(engine_equity - marked) <= max(Decimal("0.000001"), Decimal(1).scaleb(-cash_scale) / 100), "engine_ledger_disagreement")
    require(len(broker.closed_trades) == closed_trades and len(broker.trades) == (1 if held else 0), "engine_ledger_disagreement")
    final_equity = Decimal(equity[-1]["equity"])
    metrics = {"initial_cash": scaled(initial_cash, cash_scale), "final_cash": scaled(cash, cash_scale),
               "final_equity": scaled(final_equity, cash_scale), "realized_net_pnl": scaled(realized, cash_scale),
               "unrealized_pnl": scaled(held * (Decimal(rows[-1]["close"]) - entry), cash_scale),
               "commission_total": scaled(total_fees, cash_scale), "slippage_cost_total": scaled(total_slippage, cash_scale),
               "completed_trades": closed_trades, "open_positions": 1 if held else 0,
               "rejected_orders": sum(o["status"].startswith("rejected_") for o in orders),
               "cancelled_pending_orders": sum(o["status"].startswith("cancelled_") for o in orders),
               "total_return": norm(rounded((final_equity - Decimal(initial_cash)) / Decimal(initial_cash), 12)),
               "max_drawdown": norm(rounded(max_drawdown, 12))}
    if regulatory:
        metrics["regulatory_fee_total"] = scaled(total_regulatory, cash_scale)
    return orders, fills, equity, metrics
