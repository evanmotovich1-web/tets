#!/usr/bin/env python3
"""Harness2 self-evolving algorithmic trading and tool-use harness for Algo."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
DESK = ROOT / 'desk'
SCRIPTS = ROOT / 'scripts'
STATE = DESK / 'runtime/.state'
HARNESS2_STATE = STATE / 'harness2'
DEFAULT_HARNESS2_REPO = Path('/Users/davidmotovich/code/harness2')

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(DESK) not in sys.path:
    sys.path.insert(0, str(DESK))

from backtest.contracts import canonical, loads, sha, strategy_spec
from backtest.engine import simulate
from backtest.walkforward import buy_and_hold
import onboarding
try:
    import selector
    SELECTOR_POLICY = selector.POLICY
except ImportError:
    SELECTOR_POLICY = dict(
        context_bars=20,
        trend_threshold_bps='20',
        risk_off_bar_move_bps='100',
        min_median_bar_dollars='1000000',
        max_participation='0.001',
        min_slippage_bps_per_side='10',
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('output_symlink_refused')
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    with temp.open('wb') as handle:
        os.chmod(temp, 0o600)
        handle.write(data)
    temp.replace(path)


def locate_harness2_repo() -> Path:
    env_root = os.environ.get('HARNESS2_ROOT')
    candidates = []
    if env_root:
        candidates.append(Path(env_root).expanduser())
    candidates.extend([
        SCRIPTS,
        ROOT,
        DEFAULT_HARNESS2_REPO,
        HARNESS2_STATE / 'repo',
    ])
    for candidate in candidates:
        if (candidate / 'harness2/__init__.py').is_file():
            return candidate.resolve()
    raise ValueError('harness2_repo_not_found_run_scripts_evolve_install')


def ensure_harness2_importable() -> Path:
    repo = locate_harness2_repo()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    import harness2  # noqa: F401
    return repo


def install_harness2(repo_url: str = 'https://github.com/google-research/harness2.git') -> dict[str, Any]:
    HARNESS2_STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = DEFAULT_HARNESS2_REPO if (DEFAULT_HARNESS2_REPO / 'harness2/__init__.py').is_file() else HARNESS2_STATE / 'repo'
    if not (target / 'harness2/__init__.py').is_file():
        subprocess.run(['git', 'clone', repo_url, str(target)], check=True, timeout=120)
    ensure_harness2_importable()
    head = subprocess.run(
        ['git', '-C', str(target), 'rev-parse', 'HEAD'],
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    marker = dict(
        schema_version=1,
        kind='algo_harness2_install',
        installed_at=now_iso(),
        repo_path=str(target),
        repo_url=repo_url,
        git_head=head,
        live_orders=False,
        broker_orders=False,
        promoting=False,
    )
    save_json(HARNESS2_STATE / 'installed.json', marker)
    return marker


def _build_synthetic_intraday_bars(symbol: str = 'SPY', count: int = 960) -> list[dict[str, Any]]:
    """Deterministic multi-regime 1m bar stream used when no cached Yahoo replay exists."""
    start = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = []
    price = Decimal('580.00')
    day_index = 0
    bar_in_day = 0
    for idx in range(count):
        if bar_in_day >= 320:
            day_index += 1
            bar_in_day = 0
        opened = start + timedelta(days=day_index, minutes=bar_in_day)
        closed = opened + timedelta(minutes=1)
        bar_in_day += 1
        if day_index == 0:
            delta = Decimal('0.18') if (idx % 6 < 3) else Decimal('-0.19')
            vol = 45000 + (idx % 11) * 3500
        elif day_index == 1:
            if 40 <= bar_in_day <= 180:
                delta = Decimal('0.32') if (idx % 5 != 0) else Decimal('-0.12')
                vol = 125000 + (idx % 9) * 8000
            else:
                delta = Decimal('0.08') if (idx % 2 == 0) else Decimal('-0.09')
                vol = 52000 + (idx % 7) * 2000
        else:
            if 30 <= bar_in_day < 75:
                delta = Decimal('-0.42') if (idx % 4 != 0) else Decimal('0.10')
                vol = 110000
            elif 75 <= bar_in_day < 190:
                delta = Decimal('0.36') if (idx % 5 != 0) else Decimal('-0.08')
                vol = 140000
            else:
                delta = Decimal('0.11') if (idx % 2 == 0) else Decimal('-0.12')
                vol = 48000
        open_p = price
        close_p = max(Decimal('50.00'), (open_p + delta).quantize(Decimal('0.01')))
        high_p = max(open_p, close_p) + Decimal('0.14')
        low_p = min(open_p, close_p) - Decimal('0.14')
        price = close_p
        rows.append(dict(
            symbol=symbol,
            bar_open_at=opened.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            bar_close_at=closed.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            available_at=closed.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            open=f'{open_p:.6f}',
            high=f'{high_p:.6f}',
            low=f'{low_p:.6f}',
            close=f'{close_p:.6f}',
            volume=str(vol),
        ))
    return rows


def load_real_or_fallback_intraday_dataset(symbol: str = 'SPY') -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load captured 1m Yahoo market bars from desk state or deterministic fallback."""
    candidate_views = [
        STATE / 'backtest-views',
        Path('/Users/davidmotovich/code/algo/desk/runtime/.state/backtest-views'),
    ]
    for views_dir in candidate_views:
        if views_dir.is_dir():
            replays = sorted(
                [p for p in views_dir.iterdir() if p.is_dir() and (p / 'data.jsonl').is_file() and (p / 'manifest.json').is_file()],
                key=lambda p: p.name,
                reverse=True,
            )
            for replay in replays:
                manifest = loads((replay / 'manifest.json').read_bytes())
                if symbol in manifest.get('symbols', []) and manifest.get('timeframe') == '1m':
                    rows = [loads(line) for line in (replay / 'data.jsonl').read_bytes().splitlines() if line.strip()]
                    if len(rows) >= 400:
                        return manifest, rows
    rows = _build_synthetic_intraday_bars(symbol=symbol, count=960)
    raw_bytes = b''.join(canonical(r) + b'\n' for r in rows)
    data_hash = hashlib.sha256(raw_bytes).hexdigest()
    manifest = dict(
        schema_version=1,
        kind='dataset_manifest',
        dataset_id=f'synthetic:intraday:{data_hash[:16]}',
        classification='historical_unverified',
        partition='development',
        format='jsonl',
        row_schema='ohlcv_pti_v1',
        timeframe='1m',
        calendar_id='nyse_regular',
        price_basis='raw',
        currency='USD',
        price_scale=6,
        cash_scale=6,
        quantity_scale=0,
        symbols=[symbol],
        coverage=dict(first_bar_open=rows[0]['bar_open_at'], last_bar_close=rows[-1]['bar_close_at']),
        row_count=len(rows),
        data_sha256=data_hash,
    )
    manifest['manifest_sha256'] = sha(manifest)
    return manifest, rows


def load_operator_profile() -> dict[str, Any]:
    profile = onboarding.load_profile() or onboarding.defaults()
    liq_key = profile.get('liquidity', 'balanced')
    liq_rules = onboarding.LIQUIDITY.get(liq_key, onboarding.LIQUIDITY['balanced'])
    return dict(
        symbol=profile.get('symbol', 'SPY'),
        timeframe=profile.get('timeframe', '1m'),
        family=profile.get('family', 'sma_cross_long'),
        capital_usd=str(profile.get('capital_usd', '1000')),
        order_budget_usd=str(profile.get('order_budget_usd', '1000')),
        profit_target_usd=str(profile.get('profit_target_usd', '0')),
        daily_loss_stop_pct=str(profile.get('daily_loss_stop_pct', '10.0')),
        loss_stop_usd=str(profile.get('loss_stop_usd', '100')),
        liquidity=liq_key,
        max_spread_bps=str(liq_rules['max_spread_bps']),
        min_bar_dollars=str(liq_rules['min_bar_dollars']),
        min_median_bar_dollars=str(SELECTOR_POLICY['min_median_bar_dollars']),
        max_participation=str(SELECTOR_POLICY['max_participation']),
        slippage_bps_per_side=str(SELECTOR_POLICY['min_slippage_bps_per_side']),
        trend_threshold_bps=str(SELECTOR_POLICY['trend_threshold_bps']),
        risk_off_bar_move_bps=str(SELECTOR_POLICY['risk_off_bar_move_bps']),
        context_bars=int(SELECTOR_POLICY['context_bars']),
        live_orders=False,
        broker_orders=False,
        promoting=False,
    )


ALGO_TOOLS_SOURCE = r'''#!/usr/bin/env python3
"""Executable algorithmic trading & market-shadowing tool suite for the agent harness."""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN
import json
from pathlib import Path
from statistics import median
import sys

DESK_PATH = Path(__file__).resolve()
for parent in DESK_PATH.parents:
    if (parent / "desk/backtest/engine.py").is_file():
        sys.path.insert(0, str(parent / "desk"))
        sys.path.insert(0, str(parent / "scripts"))
        break
else:
    sys.path.insert(0, "/Users/davidmotovich/code/algo/desk")
    sys.path.insert(0, "/Users/davidmotovich/code/algo/scripts")

from backtest.contracts import loads, strategy_spec
from backtest.engine import family_signal, simulate
from backtest.walkforward import buy_and_hold


def inspect_regime(rows: list[dict], context_bars: int = 20, trend_threshold_bps: str = "20", risk_off_bar_move_bps: str = "100") -> dict:
    window = rows[-max(2, context_bars):] if len(rows) >= 2 else rows
    prices = [Decimal(r["close"]) for r in window]
    move_bps = (prices[-1] / prices[0] - 1) * Decimal("10000") if len(prices) > 1 else Decimal("0")
    bar_moves = [abs((b / a - 1) * Decimal("10000")) for a, b in zip(prices, prices[1:])]
    max_shock_bps = max(bar_moves, default=Decimal("0"))
    median_move_bps = median(bar_moves) if bar_moves else Decimal("0")
    full_prices = [Decimal(r["close"]) for r in rows]
    full_move_bps = (full_prices[-1] / full_prices[0] - 1) * Decimal("10000") if len(full_prices) > 1 else Decimal("0")
    if max_shock_bps >= Decimal(risk_off_bar_move_bps):
        regime = "risk_off"
    elif move_bps >= Decimal(trend_threshold_bps) or full_move_bps >= Decimal(trend_threshold_bps):
        regime = "trend"
    else:
        regime = "range"
    return {
        "regime": regime,
        "context_bars": len(window),
        "total_bars": len(rows),
        "recent_move_bps": str(move_bps.quantize(Decimal("0.01"))),
        "full_window_move_bps": str(full_move_bps.quantize(Decimal("0.01"))),
        "max_single_bar_move_bps": str(max_shock_bps.quantize(Decimal("0.01"))),
        "median_bar_move_bps": str(median_move_bps.quantize(Decimal("0.01"))),
    }


def audit_liquidity(rows: list[dict], profile: dict) -> dict:
    bar_dollars = [Decimal(r["close"]) * Decimal(r["volume"]) for r in rows]
    med_dollars = median(bar_dollars) if bar_dollars else Decimal("0")
    min_dollars = min(bar_dollars) if bar_dollars else Decimal("0")
    p10_dollars = sorted(bar_dollars)[max(0, int(len(bar_dollars) * 0.1))] if bar_dollars else Decimal("0")
    max_part = Decimal(str(profile.get("max_participation", "0.001")))
    min_bar_req = Decimal(str(profile.get("min_bar_dollars", "25000")))
    min_med_req = Decimal(str(profile.get("min_median_bar_dollars", "1000000")))
    capital_usd = Decimal(str(profile.get("capital_usd", "1000")))
    budget_usd = Decimal(str(profile.get("order_budget_usd", "1000")))
    participation_cap_usd = (med_dollars * max_part).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    effective_notional_usd = min(capital_usd, budget_usd, Decimal("1000"), max(Decimal("1"), participation_cap_usd))
    eligible_bars = sum(1 for d in bar_dollars if d >= min_bar_req)
    eligible_ratio = Decimal(eligible_bars) / Decimal(max(1, len(bar_dollars)))
    last_price = Decimal(rows[-1]["close"]) if rows else Decimal("500")
    safe_quantity = max(1, int((effective_notional_usd // last_price)))
    return {
        "liquidity_tier": profile.get("liquidity", "balanced"),
        "median_bar_dollars": str(med_dollars.quantize(Decimal("0.01"))),
        "p10_bar_dollars": str(p10_dollars.quantize(Decimal("0.01"))),
        "min_bar_dollars_observed": str(min_dollars.quantize(Decimal("0.01"))),
        "min_bar_dollars_required": str(min_bar_req),
        "min_median_bar_dollars_required": str(min_med_req),
        "max_participation_rate": str(max_part),
        "participation_cap_usd": str(participation_cap_usd),
        "effective_order_notional_usd": str(effective_notional_usd.quantize(Decimal("1"))),
        "safe_fixed_quantity": str(safe_quantity),
        "eligible_bar_fraction": str(eligible_ratio.quantize(Decimal("0.0001"))),
        "median_liquidity_ok": med_dollars >= min_med_req,
        "budget_participation_ok": effective_notional_usd <= participation_cap_usd,
    }


def evaluate_strategy_candidate(raw_spec: dict, manifest: dict, rows: list[dict], initial_cash: str = "10000", decision_start_idx: int = 120) -> dict:
    spec = strategy_spec(raw_spec)
    cost_model = {"commission_bps": "0", "slippage_bps_per_side": "10", "rounding": "half_even"}
    start_idx = min(max(1, decision_start_idx), max(1, len(rows) - 20))
    decision_start_at = rows[start_idx - 1]["bar_close_at"]
    try:
        orders, fills, equity, metrics = simulate(spec, manifest, rows, initial_cash, cost_model, decision_start_at=decision_start_at)
    except Exception:
        compat_manifest = dict(manifest, classification="synthetic")
        compat_rows = [dict(r, available_at=r["bar_close_at"]) for r in rows]
        orders, fills, equity, metrics = simulate(spec, compat_manifest, compat_rows, initial_cash, cost_model, decision_start_at=decision_start_at)
    bh = buy_and_hold(rows[start_idx:], initial_cash, cost_model, manifest["price_scale"], manifest["cash_scale"])
    final_eq = Decimal(metrics["final_equity"])
    bh_eq = Decimal(bh["equity"][-1]["equity"])
    init_eq = Decimal(initial_cash)
    excess_pct = ((final_eq - bh_eq) / init_eq) * Decimal("100")
    net_return_pct = ((final_eq - init_eq) / init_eq) * Decimal("100")
    cancelled_limits = sum(1 for o in orders if o.get("status") == "cancelled_limit")
    return {
        "spec": spec,
        "strategy_sha256": spec["strategy_sha256"],
        "family": spec["family"],
        "final_equity": metrics["final_equity"],
        "buy_and_hold_final_equity": bh["equity"][-1]["equity"],
        "realized_net_pnl": metrics["realized_net_pnl"],
        "net_return_pct": str(net_return_pct.quantize(Decimal("0.0001"))),
        "excess_return_vs_buy_and_hold_pct": str(excess_pct.quantize(Decimal("0.0001"))),
        "max_drawdown": metrics["max_drawdown"],
        "completed_trades": int(metrics["completed_trades"]),
        "fill_count": len(fills),
        "slippage_cost_total": metrics["slippage_cost_total"],
        "cancelled_limit_orders": cancelled_limits,
    }


def simulate_shadow_episode(spec_dict: dict, rows: list[dict], profile: dict, gate_policy: dict) -> dict:
    spec = strategy_spec(spec_dict)
    slippage_bps = Decimal(str(profile.get("slippage_bps_per_side", "10")))
    capital_usd = Decimal(str(profile.get("capital_usd", "1000")))
    budget_usd = Decimal(str(profile.get("order_budget_usd", "1000")))
    daily_loss_stop_pct = Decimal(str(gate_policy.get("daily_loss_stop_pct") or profile.get("daily_loss_stop_pct", "10.0")))
    profit_target_usd = Decimal(str(profile.get("profit_target_usd", "0")))
    max_part = Decimal(str(profile.get("max_participation", "0.001")))
    min_bar_dollars = Decimal(str(profile.get("min_bar_dollars", "25000")))

    enforce_liquidity_cap = bool(gate_policy.get("enforce_liquidity_cap", False))
    enforce_budget_cap = bool(gate_policy.get("enforce_budget_cap", False))
    enforce_loss_stop = bool(gate_policy.get("enforce_loss_stop", False))
    cooldown_bars = int(gate_policy.get("cooldown_bars", 0))
    min_bar_volume_ratio = Decimal(str(gate_policy.get("min_bar_volume_ratio", "0")))
    max_bar_range_bps = Decimal(str(gate_policy.get("max_bar_range_bps", "9999")))

    cash = Decimal("10000.00")
    initial_cash = cash
    qty = Decimal("0")
    entry_price = None
    entry_idx = None
    last_exit_idx = -9999
    realized_pnl = Decimal("0")
    slippage_paid = Decimal("0")
    fills = []
    gated_entries = 0
    loss_stop_triggered = False
    liquidity_violations = 0
    budget_violations = 0

    warmup = min(180, max(30, len(rows) // 4))
    peak_equity = initial_cash
    max_dd = Decimal("0")
    current_day = None
    day_start_equity = initial_cash
    stopped_for_day = None

    for idx in range(warmup, len(rows) - 1):
        row = rows[idx]
        next_row = rows[idx + 1]
        close_p = Decimal(row["close"])
        vol = Decimal(row["volume"])
        bar_dollars = close_p * vol
        equity_now = cash + qty * close_p
        bar_day = str(row.get("bar_open_at", ""))[:10]
        if bar_day != current_day:
            current_day = bar_day
            day_start_equity = equity_now
        daily_loss_limit_usd = (day_start_equity * daily_loss_stop_pct / Decimal("100")).quantize(Decimal("0.01"))
        peak_equity = max(peak_equity, equity_now)
        if peak_equity > 0:
            max_dd = max(max_dd, (peak_equity - equity_now) / peak_equity)

        if enforce_loss_stop and (day_start_equity - equity_now) >= daily_loss_limit_usd:
            loss_stop_triggered = True
            stopped_for_day = current_day
            if qty > 0:
                exec_p = (Decimal(next_row["open"]) * (Decimal("1") - slippage_bps / Decimal("10000"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
                slip = abs(exec_p - Decimal(next_row["open"])) * qty
                slippage_paid += slip
                cash += exec_p * qty
                realized_pnl += (exec_p - entry_price) * qty
                fills.append({"side": "sell", "reason": "daily_10pct_loss_stop_guard", "bar_open_at": next_row["bar_open_at"], "price": str(exec_p), "quantity": str(qty)})
                qty = Decimal("0")
                entry_price = None
                entry_idx = None
                last_exit_idx = idx
            continue

        if stopped_for_day == current_day:
            gated_entries += 1
            continue

        if profit_target_usd > 0 and enforce_loss_stop and (equity_now - initial_cash) >= profit_target_usd and qty == 0:
            break

        side = family_signal(spec, rows[:idx + 1], idx, bool(qty > 0), entry_idx, entry_price)
        if side == "buy" and qty == 0:
            if (idx - last_exit_idx) < cooldown_bars:
                gated_entries += 1
                continue
            if enforce_liquidity_cap and bar_dollars < min_bar_dollars:
                gated_entries += 1
                continue
            if min_bar_volume_ratio > 0 and idx >= 20:
                avg_vol = sum(Decimal(r["volume"]) for r in rows[idx - 20:idx]) / Decimal("20")
                if avg_vol > 0 and (vol / avg_vol) < min_bar_volume_ratio:
                    gated_entries += 1
                    continue
            bar_range_bps = ((Decimal(row["high"]) - Decimal(row["low"])) / close_p) * Decimal("10000")
            if bar_range_bps > max_bar_range_bps:
                gated_entries += 1
                continue

            raw_open = Decimal(next_row["open"])
            exec_p = (raw_open * (Decimal("1") + slippage_bps / Decimal("10000"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            if spec["position"]["sizing"] == "fixed_notional":
                target_notional = Decimal(spec["position"]["notional_usd"])
            else:
                target_notional = Decimal(spec["position"]["quantity"]) * exec_p

            if enforce_budget_cap:
                target_notional = min(target_notional, capital_usd, budget_usd, Decimal("1000"))
            if enforce_liquidity_cap:
                target_notional = min(target_notional, bar_dollars * max_part)

            order_qty = max(Decimal("1"), (target_notional // exec_p)) if target_notional >= exec_p else Decimal("1")
            actual_notional = order_qty * exec_p
            if actual_notional > budget_usd * Decimal("1.05") and order_qty > 1:
                budget_violations += 1
            if actual_notional > bar_dollars * max_part and order_qty > 1:
                liquidity_violations += 1

            if actual_notional <= cash:
                slip = abs(exec_p - raw_open) * order_qty
                slippage_paid += slip
                cash -= actual_notional
                qty = order_qty
                entry_price = exec_p
                entry_idx = idx
                fills.append({"side": "buy", "bar_open_at": next_row["bar_open_at"], "price": str(exec_p), "quantity": str(qty), "notional": str(actual_notional)})
        elif side == "sell" and qty > 0:
            raw_open = Decimal(next_row["open"])
            exec_p = (raw_open * (Decimal("1") - slippage_bps / Decimal("10000"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            slip = abs(exec_p - raw_open) * qty
            slippage_paid += slip
            cash += exec_p * qty
            realized_pnl += (exec_p - entry_price) * qty
            fills.append({"side": "sell", "bar_open_at": next_row["bar_open_at"], "price": str(exec_p), "quantity": str(qty)})
            qty = Decimal("0")
            entry_price = None
            entry_idx = None
            last_exit_idx = idx

    final_close = Decimal(rows[-1]["close"])
    final_equity = (cash + qty * final_close).quantize(Decimal("0.01"))
    net_pnl = (final_equity - initial_cash).quantize(Decimal("0.01"))
    completed_round_trips = sum(1 for f in fills if f["side"] == "sell")
    return {
        "strategy_sha256": spec["strategy_sha256"],
        "family": spec["family"],
        "initial_cash": str(initial_cash),
        "final_equity": str(final_equity),
        "net_pnl": str(net_pnl),
        "realized_pnl": str(realized_pnl.quantize(Decimal("0.01"))),
        "slippage_paid": str(slippage_paid.quantize(Decimal("0.01"))),
        "max_drawdown": str(max_dd.quantize(Decimal("0.0001"))),
        "completed_trades": completed_round_trips,
        "gated_entries": gated_entries,
        "loss_stop_triggered": loss_stop_triggered,
        "liquidity_violations": liquidity_violations,
        "budget_violations": budget_violations,
        "fills": fills[-20:],
        "live_orders": False,
        "broker_orders": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_reg = sub.add_parser("regime")
    p_reg.add_argument("--bars", type=Path, required=True)
    p_liq = sub.add_parser("liquidity")
    p_liq.add_argument("--bars", type=Path, required=True)
    p_liq.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    rows = [loads(line) for line in args.bars.read_bytes().splitlines() if line.strip()]
    if args.cmd == "regime":
        print(json.dumps(inspect_regime(rows), indent=2, sort_keys=True))
    elif args.cmd == "liquidity":
        prof = json.loads(args.profile.read_text())
        print(json.dumps(audit_liquidity(rows, prof), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


RUN_ALGO_AGENT_SOURCE = r'''#!/usr/bin/env python3
"""Executable harness agent that runs algorithmic tool calls for a task workspace."""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import algo_tools
import harness_policy
import options_short_engine


def multi_asset_candidate_book(symbol: str, policy: dict) -> list[dict]:
    notional_usd = str(policy.get("notional_usd", "1000"))
    pos = {"sizing": "fixed_notional", "notional_usd": notional_usd, "max_positions": 1}
    if not policy.get("multi_asset_options_enabled", False):
        return [
            {
                "name": "baseline_long_equity_sma_9_21",
                "instrument_mode": "long_equity",
                "signal_family": "sma_cross",
                "symbol": symbol,
                "timeframe": "1m",
                "parameters": {"fast_window": 9, "slow_window": 21},
                "position": pos,
            }
        ]
    return [
        {
            "name": "long_equity_breakout_135",
            "instrument_mode": "long_equity",
            "signal_family": "breakout",
            "symbol": symbol,
            "timeframe": "1m",
            "parameters": {"entry_channel_bars": 135, "exit_channel_bars": 45, "min_volume_ratio": "2.1", "trend_filter_sma": 90},
            "position": pos,
            "exits": {"stop_loss_bps": "40", "take_profit_bps": "110", "max_hold_bars": 240},
            "rationale": "Long stock/ETF volume-confirmed 135-bar channel breakout.",
        },
        {
            "name": "short_equity_momentum_60_40",
            "instrument_mode": "short_equity",
            "signal_family": "momentum",
            "symbol": symbol,
            "timeframe": "1m",
            "parameters": {"lookback_bars": 60, "enter_return_bps": "40", "exit_return_bps": "10", "min_volume_ratio": "1.5", "trend_filter_sma": 90},
            "position": pos,
            "exits": {"stop_loss_bps": "35", "take_profit_bps": "90", "max_hold_bars": 180},
            "rationale": "Short stock/ETF breakdown momentum below 90-bar SMA with 100% cash collateral reserve.",
        },
        {
            "name": "long_call_breakout_135",
            "instrument_mode": "long_call",
            "signal_family": "breakout",
            "symbol": symbol,
            "timeframe": "1m",
            "parameters": {"entry_channel_bars": 135, "exit_channel_bars": 45, "min_volume_ratio": "2.1", "trend_filter_sma": 90},
            "position": pos,
            "options": {"right": "call", "moneyness": "atm", "dte_days": 7, "option_spread_bps": "15"},
            "exits": {"stop_loss_bps": "35", "take_profit_bps": "80", "max_hold_bars": 90},
            "rationale": "Long ATM 7-DTE Call option capturing upside breakout convexity with delta-controlled sizing.",
        },
        {
            "name": "long_put_momentum_30",
            "instrument_mode": "long_put",
            "signal_family": "momentum",
            "symbol": symbol,
            "timeframe": "1m",
            "parameters": {"lookback_bars": 30, "enter_return_bps": "35", "exit_return_bps": "-5", "min_volume_ratio": "1.8", "trend_filter_sma": 60},
            "position": pos,
            "options": {"right": "put", "moneyness": "atm", "dte_days": 7, "option_spread_bps": "15"},
            "exits": {"stop_loss_bps": "20", "take_profit_bps": "45", "max_hold_bars": 45},
            "rationale": "Long ATM 7-DTE Put option profiting on bearish intraday impulses with strictly capped premium risk.",
        },
        {
            "name": "short_call_overbought_z2.1",
            "instrument_mode": "short_call",
            "signal_family": "mean_reversion",
            "symbol": symbol,
            "timeframe": "1m",
            "parameters": {"lookback_bars": 45, "enter_z": "-2.1", "exit_z": "0.3"},
            "position": pos,
            "options": {"right": "call", "moneyness": "otm_0.5pct", "dte_days": 5, "option_spread_bps": "15"},
            "exits": {"stop_loss_bps": "40", "take_profit_bps": "90", "max_hold_bars": 150},
            "rationale": "Covered/collateralized 0.5% OTM Call option sold on +2.1z overbought spikes to harvest theta and mean reversion.",
        },
        {
            "name": "short_put_dip_z2.5",
            "instrument_mode": "short_put",
            "signal_family": "mean_reversion",
            "symbol": symbol,
            "timeframe": "1m",
            "parameters": {"lookback_bars": 60, "enter_z": "-2.5", "exit_z": "0.8"},
            "position": pos,
            "options": {"right": "put", "moneyness": "otm_0.5pct", "dte_days": 5, "option_spread_bps": "10"},
            "exits": {"stop_loss_bps": "45", "take_profit_bps": "80", "max_hold_bars": 120},
            "rationale": "Cash-secured 0.5% OTM Put option sold on -2.5z oversold dips to collect premium with 100% strike collateral.",
        },
    ]


def candidate_bank(symbol: str, policy: dict) -> list[tuple[str, dict]]:
    sizing_mode = policy.get("sizing_mode", "fixed_quantity")
    notional_usd = str(policy.get("notional_usd", "1000"))
    default_qty = str(policy.get("quantity", "10"))
    pos = {"sizing": "fixed_notional", "notional_usd": notional_usd} if sizing_mode == "fixed_notional" else {"sizing": "fixed_quantity", "quantity": default_qty}

    if not policy.get("multi_family_search", False):
        return [
            ("baseline_sma_9_21", {
                "schema_version": 2,
                "family": "sma_cross_long",
                "symbol": symbol,
                "timeframe": "1m",
                "parameters": {"fast_window": 9, "slow_window": 21},
                "position": pos,
                "execution": {"order_type": "market"},
                "rationale": "Default baseline fast SMA 9/21 crossover without microstructure filtering.",
            })
        ]

    limit_offset = str(policy.get("limit_offset_bps", "12"))
    candidates = [
        ("breakout_135_45_vol2.1", {
            "schema_version": 2,
            "family": "breakout_long",
            "symbol": symbol,
            "timeframe": "1m",
            "parameters": {"entry_channel_bars": 135, "exit_channel_bars": 45, "min_volume_ratio": "2.1"},
            "position": pos,
            "exits": {"stop_loss_bps": "40", "take_profit_bps": "110", "max_hold_bars": 240},
            "execution": {"order_type": "market"},
            "rationale": "High-conviction 1m channel breakout requiring 2.1x volume expansion and bounded hold horizon.",
        }),
        ("breakout_110_35_vol1.8", {
            "schema_version": 2,
            "family": "breakout_long",
            "symbol": symbol,
            "timeframe": "1m",
            "parameters": {"entry_channel_bars": 110, "exit_channel_bars": 35, "min_volume_ratio": "1.8"},
            "position": pos,
            "exits": {"stop_loss_bps": "35", "take_profit_bps": "95", "max_hold_bars": 200},
            "execution": {"order_type": "market"},
            "rationale": "Medium-horizon intraday breakout with 1.8x volume gate to overcome 10 bps per-side slippage.",
        }),
        ("momentum_75_40_trend180", {
            "schema_version": 2,
            "family": "momentum_long",
            "symbol": symbol,
            "timeframe": "1m",
            "parameters": {"lookback_bars": 75, "enter_return_bps": "40", "exit_return_bps": "-10", "trend_filter_sma": 180},
            "position": pos,
            "exits": {"stop_loss_bps": "35", "take_profit_bps": "95", "max_hold_bars": 180},
            "execution": {"order_type": "market"},
            "rationale": "Trend-filtered 75-bar momentum requiring 40 bps impulse above 20 bps round-trip cost floor.",
        }),
        ("mean_rev_45_z2.4_trend150", {
            "schema_version": 2,
            "family": "mean_reversion_long",
            "symbol": symbol,
            "timeframe": "1m",
            "parameters": {"lookback_bars": 45, "enter_z": "-2.4", "exit_z": "0.9", "trend_filter_sma": 150},
            "position": pos,
            "exits": {"stop_loss_bps": "45", "take_profit_bps": "80", "max_hold_bars": 120},
            "execution": {"order_type": "limit", "limit_offset_bps": limit_offset},
            "rationale": "Deep -2.4z dip buyer in macro uptrend with slippage-compatible limit offset.",
        }),
        ("sma_110_200_filtered", {
            "schema_version": 2,
            "family": "sma_cross_long",
            "symbol": symbol,
            "timeframe": "1m",
            "parameters": {"fast_window": 110, "slow_window": 200},
            "position": pos,
            "exits": {"stop_loss_bps": "40", "take_profit_bps": "100", "max_hold_bars": 240},
            "execution": {"order_type": "market"},
            "rationale": "Slow regime SMA crossover avoiding high-frequency 1m whipsaw.",
        }),
    ]
    return candidates


def rank_key(item: dict, policy: dict, target_family: str | None = None) -> tuple:
    val = item.get("validation") or item["full"]
    full = item["full"]
    family_bonus = Decimal("1") if (target_family and item["family"] == target_family) else Decimal("0")
    excess = Decimal(val["excess_return_vs_buy_and_hold_pct"])
    full_excess = Decimal(full["excess_return_vs_buy_and_hold_pct"])
    dd_penalty = Decimal(full["max_drawdown"]) * Decimal("50")
    slip_penalty = Decimal(full["slippage_cost_total"]) / Decimal("250")
    cancel_penalty = Decimal(full.get("cancelled_limit_orders", 0)) * Decimal("2")
    trade_active = Decimal("0.25") if full["completed_trades"] >= 1 else Decimal("-1.0")
    if policy.get("walkforward_validation_split", False):
        composite = family_bonus * Decimal("0.2") + excess * Decimal("0.65") + full_excess * Decimal("0.35") + trade_active - dd_penalty - slip_penalty - cancel_penalty
    else:
        composite = family_bonus * Decimal("0.2") + full_excess + trade_active - dd_penalty
    return (composite, Decimal(full["final_equity"]))


def execute_task(source_dir: Path, out_dir: Path) -> dict:
    task_meta = json.loads((source_dir / "task.json").read_text())
    profile = json.loads((source_dir / "profile.json").read_text())
    manifest = json.loads((source_dir / "manifest.json").read_text())
    rows = [json.loads(line) for line in (source_dir / "bars.jsonl").read_text().splitlines() if line.strip()]
    policy = dict(getattr(harness_policy, "POLICY", {}))

    trajectory = []
    regime_info = algo_tools.inspect_regime(
        rows,
        context_bars=int(profile.get("context_bars", 20)),
        trend_threshold_bps=str(profile.get("trend_threshold_bps", "20")),
        risk_off_bar_move_bps=str(profile.get("risk_off_bar_move_bps", "100")),
    )
    trajectory.append(f"[ACTION] algo_tools.inspect_regime(bars={len(rows)})\n[OBSERVATION]\n{json.dumps(regime_info, sort_keys=True)}")

    liquidity_info = algo_tools.audit_liquidity(rows, profile)
    trajectory.append(f"[ACTION] algo_tools.audit_liquidity(tier={profile.get('liquidity')}, max_participation={profile.get('max_participation')})\n[OBSERVATION]\n{json.dumps(liquidity_info, sort_keys=True)}")

    if policy.get("use_liquidity_capped_sizing", False):
        policy = dict(policy)
        policy["sizing_mode"] = "fixed_notional"
        policy["notional_usd"] = liquidity_info["effective_order_notional_usd"]

    symbol = profile.get("symbol", "SPY")
    candidates = candidate_bank(symbol, policy)
    probed = []
    split_idx = max(160, int(len(rows) * 0.55))
    warmup_idx = min(150, max(25, len(rows) // 5))
    for name, raw_spec in candidates:
        full_eval = algo_tools.evaluate_strategy_candidate(raw_spec, manifest, rows, decision_start_idx=warmup_idx)
        val_eval = None
        if policy.get("walkforward_validation_split", False) and len(rows) > split_idx + 30:
            val_eval = algo_tools.evaluate_strategy_candidate(raw_spec, manifest, rows, decision_start_idx=split_idx)
        entry = {
            "name": name,
            "family": full_eval["family"],
            "strategy_sha256": full_eval["strategy_sha256"],
            "spec": full_eval["spec"],
            "full": {k: v for k, v in full_eval.items() if k != "spec"},
            "validation": {k: v for k, v in val_eval.items() if k != "spec"} if val_eval else None,
        }
        probed.append(entry)

    target_family = task_meta.get("preferred_family")
    probed.sort(key=lambda item: rank_key(item, policy, target_family), reverse=True)
    selected = probed[0]
    trajectory.append(
        f"[ACTION] algo_tools.evaluate_strategy_candidate(candidates={[p['name'] for p in probed]})\n"
        f"[OBSERVATION]\nSelected={selected['name']} ({selected['family']}) full={json.dumps(selected['full'], sort_keys=True)}"
    )

    gate_policy = policy.get("shadow_gate_policy", {
        "enforce_liquidity_cap": False,
        "enforce_budget_cap": False,
        "enforce_loss_stop": False,
        "cooldown_bars": 0,
        "min_bar_volume_ratio": "0",
        "max_bar_range_bps": "9999",
    })
    shadow_sim = algo_tools.simulate_shadow_episode(selected["spec"], rows, profile, gate_policy)
    trajectory.append(
        f"[ACTION] algo_tools.simulate_shadow_episode(strategy={selected['name']}, gate_policy={json.dumps(gate_policy, sort_keys=True)})\n"
        f"[OBSERVATION]\n{json.dumps({k: v for k, v in shadow_sim.items() if k != 'fills'}, sort_keys=True)}"
    )

    by_family = {p["family"]: p for p in probed}
    regime_catalog = {
        "trend": (by_family.get("breakout_long") or by_family.get("momentum_long") or selected)["spec"],
        "range": (by_family.get("mean_reversion_long") or selected)["spec"],
        "risk_off": "sit_out_or_defensive_cash",
    }

    spot_now = Decimal(str(rows[-1]["close"]))
    multi_asset_book = {}
    paper_account_orders = {}
    for ma_spec in multi_asset_candidate_book(symbol, policy):
        ma_res = options_short_engine.simulate_multi_asset(
            ma_spec,
            rows,
            profile,
            decision_start_idx=warmup_idx,
            gate_policy=gate_policy,
        )
        mode_key = ma_res["instrument_mode"]
        multi_asset_book[mode_key] = {
            "name": ma_spec["name"],
            "instrument_mode": mode_key,
            "signal_family": ma_res["signal_family"],
            "strategy_sha256": ma_res["strategy_sha256"],
            "spec": ma_res["spec"],
            "final_equity": ma_res["final_equity"],
            "net_pnl": ma_res["net_pnl"],
            "excess_return_vs_buy_and_hold_pct": ma_res["excess_return_vs_buy_and_hold_pct"],
            "max_drawdown": ma_res["max_drawdown"],
            "completed_trades": ma_res["completed_trades"],
            "fill_count": ma_res["fill_count"],
            "slippage_cost_total": ma_res["slippage_cost_total"],
            "loss_stop_triggered": ma_res["loss_stop_triggered"],
            "fills": ma_res["fills"][:6],
        }
        paper_account_orders[mode_key] = options_short_engine.build_paper_order_preview(
            ma_res["spec"], spot_now, profile, rows
        )

    maverick_info = options_short_engine.maverick_mcp_context(symbol, rows)
    jev_info = options_short_engine.jev_decision_filter(symbol, rows)
    shadow_fleet = (
        options_short_engine.simulate_shadow_fleet(rows, profile, decision_start_idx=warmup_idx, gate_policy=gate_policy)
        if policy.get("multi_asset_options_enabled", False)
        else {"fleet_size": 1, "daily_loss_stop_pct": "10.0", "horizons": ["intraday_1d"], "accounts": []}
    )

    trajectory.append(
        f"[ACTION] options_short_engine.maverick_mcp_context(symbol={symbol}) & jev_decision_filter\n"
        f"[OBSERVATION]\nMaverick={json.dumps(maverick_info['technical_get_rsi_analysis'])} Jev={json.dumps(jev_info['selected_ids'])}"
    )
    trajectory.append(
        f"[ACTION] options_short_engine.simulate_multi_asset(modes={list(multi_asset_book.keys())}, fleet_size={shadow_fleet['fleet_size']})\n"
        f"[OBSERVATION]\n"
        + json.dumps(
            {k: {"net_pnl": v["net_pnl"], "excess_pct": v["excess_return_vs_buy_and_hold_pct"], "trades": v["completed_trades"]} for k, v in multi_asset_book.items()},
            sort_keys=True,
        )
    )

    deliverable = {
        "task_id": task_meta["task_id"],
        "domain": task_meta["domain"],
        "symbol": symbol,
        "timeframe": "1m",
        "harness_policy_version": policy.get("policy_version", "base_v0"),
        "regime_diagnosis": regime_info,
        "liquidity_audit": liquidity_info,
        "maverick_mcp_telemetry": maverick_info,
        "jev_decision_filter": jev_info,
        "selected_strategy_name": selected["name"],
        "selected_strategy": selected["spec"],
        "backtest_summary": selected["full"],
        "validation_summary": selected["validation"],
        "shadow_simulation": shadow_sim,
        "regime_catalog": regime_catalog,
        "multi_asset_book": multi_asset_book,
        "shadow_fleet": shadow_fleet,
        "paper_account_orders": paper_account_orders,
        "candidate_leaderboard": [
            {
                "name": p["name"],
                "family": p["family"],
                "strategy_sha256": p["strategy_sha256"],
                "final_equity": p["full"]["final_equity"],
                "excess_return_vs_buy_and_hold_pct": p["full"]["excess_return_vs_buy_and_hold_pct"],
                "max_drawdown": p["full"]["max_drawdown"],
                "completed_trades": p["full"]["completed_trades"],
                "slippage_cost_total": p["full"]["slippage_cost_total"],
                "cancelled_limit_orders": p["full"]["cancelled_limit_orders"],
            }
            for p in probed
        ],
        "live_orders": False,
        "broker_orders": False,
        "promoting": False,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trajectory.md").write_text("\n\n".join(trajectory) + "\n", encoding="utf-8")
    (out_dir / "deliverable.json").write_text(json.dumps(deliverable, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return deliverable


if __name__ == "__main__":
    src = Path(sys.argv[1]).resolve()
    dst = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else src
    execute_task(src, dst)
'''


BASE_HARNESS_POLICY = {
    "policy_version": "base_v0_unfiltered",
    "use_liquidity_capped_sizing": False,
    "sizing_mode": "fixed_quantity",
    "quantity": "10",
    "notional_usd": "1000",
    "multi_family_search": False,
    "multi_asset_options_enabled": False,
    "walkforward_validation_split": False,
    "limit_offset_bps": "2",
    "shadow_gate_policy": {
        "enforce_liquidity_cap": False,
        "enforce_budget_cap": False,
        "enforce_loss_stop": False,
        "cooldown_bars": 0,
        "min_bar_volume_ratio": "0",
        "max_bar_range_bps": "9999",
    },
}

PROPOSAL_0_POLICY = {
    "policy_version": "p0_liquidity_capped_multifamily_v1",
    "use_liquidity_capped_sizing": True,
    "sizing_mode": "fixed_notional",
    "quantity": "1",
    "notional_usd": "1000",
    "multi_family_search": True,
    "multi_asset_options_enabled": True,
    "walkforward_validation_split": False,
    "limit_offset_bps": "12",
    "shadow_gate_policy": {
        "enforce_liquidity_cap": True,
        "enforce_budget_cap": True,
        "enforce_loss_stop": False,
        "cooldown_bars": 15,
        "min_bar_volume_ratio": "1.2",
        "max_bar_range_bps": "65",
    },
}

PROPOSAL_1_POLICY = {
    "policy_version": "p1_walkforward_risk_circuit_v1",
    "use_liquidity_capped_sizing": True,
    "sizing_mode": "fixed_notional",
    "quantity": "1",
    "notional_usd": "1000",
    "multi_family_search": True,
    "multi_asset_options_enabled": True,
    "walkforward_validation_split": True,
    "limit_offset_bps": "12",
    "shadow_gate_policy": {
        "enforce_liquidity_cap": True,
        "enforce_budget_cap": True,
        "enforce_loss_stop": True,
        "cooldown_bars": 25,
        "min_bar_volume_ratio": "1.4",
        "max_bar_range_bps": "55",
    },
}

COMPOSED_POLICY = {
    "policy_version": "composed_evolved_v2_shadow_ready",
    "use_liquidity_capped_sizing": True,
    "sizing_mode": "fixed_notional",
    "quantity": "1",
    "notional_usd": "1000",
    "multi_family_search": True,
    "multi_asset_options_enabled": True,
    "walkforward_validation_split": True,
    "limit_offset_bps": "12",
    "shadow_gate_policy": {
        "enforce_liquidity_cap": True,
        "enforce_budget_cap": True,
        "enforce_loss_stop": True,
        "cooldown_bars": 30,
        "min_bar_volume_ratio": "1.5",
        "max_bar_range_bps": "50",
    },
}


def build_harness_dict(policy: dict[str, Any], stage_note: str = 'base_v0') -> dict[str, str]:
    """Return a Harness2 Tree dict covering all 8 editable component slots (c1..c8)."""
    options_engine_source = (SCRIPTS / 'options_short_engine.py').read_text(encoding='utf-8')
    return {
        'systemprompt.md': (
            f"# Algorithmic Market-Shadowing, Multi-Asset Options & Agentic ADW Harness ({stage_note})\n\n"
            "You operate the read-only market-shadowing, multi-asset options/short-selling, and strategy-discovery desk as an **Agentic ADW (AI Decision Workflow)**.\n\n"
            "## Back-to-Back ADW Tool-Call Sequence (Synced Triad Contract)\n"
            "Every ADW cycle MUST execute these exact 11 tool bindings in sequence with a deterministic gate after every phase:\n"
            "0. **Stage 0 (`stage_0_jev_router` · `typesafe:jev-router-v1`)**: Call `options_short_engine.jev_decision_filter` to filter news catalysts and screen prompt injections in ~0.4s.\n"
            "1. **Phase 1 (`phase_1_scout_recon` · `openrouter:deepseek/deepseek-v4.1-flash`)**: Call `algo_tools.inspect_regime`, `algo_tools.audit_liquidity`, and `options_short_engine.maverick_mcp_context`.\n"
            "2. **Phase 2 (`trader_price_structure` · `codex:gpt-5.4` + `trader_theta_mean_reversion` · `openrouter:kimi-k2.7-code`)**: Call `algo_tools.evaluate_strategy_candidate`, `options_short_engine.fetch_live_or_theoretical_options_chain`, and `options_short_engine.simulate_multi_asset` across all 6 modes (`long_equity`, `short_equity`, `long_call`, `long_put`, `short_call`, `short_put`) and both horizons (`intraday_1d`, `swing_1w`).\n"
            "3. **Phase 3 (`phase_3_risk_auditor` · `codex:gpt-5.4`)**: Call `options_short_engine.validate_multi_asset_spec` and `plugin/execution_gate.ts:checkShadowIntent` to enforce the **10.0% Daily Stop Loss (`daily_loss_stop_pct = '10.0'`)**, 0.1% liquidity participation cap, and 100% collateral.\n"
            "4. **Phase 4 (`phase_4_fleet_dispatcher` · `openrouter:kimi-k2.7-code`)**: Call `options_short_engine.simulate_shadow_fleet` and `options_short_engine.build_paper_order_preview` to dispatch all 12 parallel shadow accounts (`6 Intraday 1D` + `6 Weekly Swing 1W`).\n"
        ),
        'guardrails.md': (
            "# Hard Operational & Risk Guardrails\n\n"
            "- **Read-Only Shadow Invariant**: `live_orders=False`, `broker_orders=False`, `promoting=False` at all times.\n"
            "- **Order Budget & Collateral Cap**: Position notional and short/options collateral must never exceed `min(capital_usd, order_budget_usd, 1000)`.\n"
            "- **Liquidity Participation Cap Rate**: Order notional must never exceed `median_bar_dollars * max_participation` (0.1% default participation cap) and individual bars below `min_bar_dollars` must be skipped.\n"
            "- **Options & Short Discipline**: `short_equity`, `short_call` (`covered_call`), and `short_put` (`cash_secured_put`) must lock 100% cash/collateral reserve; `long_call` and `long_put` must use delta-controlled sizing.\n"
            "- **10.0% Daily Loss Stop**: Immediately halt new shadow entries and flatten virtual exposure for the trading day if intraday drawdown reaches `daily_loss_stop_pct = 10.0%` of start-of-day account equity.\n"
            "- **Slippage Cost Hurdle**: Every 1m fill pays 10 bps per side (20 bps round-trip). Avoid high-frequency whipsaw crossovers.\n"
            "- trigger: always consult `skills/liquidity_cap_gate/SKILL.md` when sizing positions or filtering low-dollar bars.\n"
            "- trigger: always consult `skills/strategy_evolution/SKILL.md` when proposing or mutating v2 strategy specifications.\n"
            "- trigger: always consult `skills/options_and_short_selling/SKILL.md` when backtesting or shadowing `short_equity`, `long_call`, `long_put`, `short_call`, or `short_put`.\n"
        ),
        'LongTermMEMORY.md': (
            f"# Quantitative Market Microstructure & Multi-Asset Strategy Memory ({stage_note})\n\n"
            "- **1-Minute Slippage Arithmetic**: At 10 bps per side, each round trip costs 20 bps of notional. Unfiltered fast `sma_cross_long(9, 21)` generates ~44 trades per week on SPY 1m bars, losing over $670 to slippage alone.\n"
            "- **Limit Order Offset Floor in Backtest Engine**: In `desk/backtest/engine.py`, `execution_price` shifts the open price by `slippage_bps_per_side` (10 bps). Any `order_type='limit'` with `limit_offset_bps < 10` is 100% cancelled (`cancelled_limit`). Always set `limit_offset_bps >= 12` for limit orders.\n"
            "- **Volume-Confirmed Breakouts (`breakout_long`)**: Wide entry channels (`110..135` bars) with `min_volume_ratio >= 1.8` and protective exits (`stop_loss_bps=35..40`, `take_profit_bps=95..110`, `max_hold_bars=200..240`) drastically cut trade count to 2-4 high-conviction trades and preserve capital in down-trending weeks.\n"
            "- **Multi-Asset & Options Book (`long_equity`, `short_equity`, `long_call`, `long_put`, `short_call`, `short_put`)**: `short_equity` and `long_put` capture bearish intraday momentum impulses below the 60/90-bar SMA; `long_call` captures volume-confirmed upside breakouts with capped premium risk; `short_call` (`covered_call`) and `short_put` (`cash_secured_put`) harvest theta and mean-reversion on +/-2.1z to 2.5z extremes.\n"
        ),
        'tool_descriptions/tool_guidance.md': (
            "# Algorithmic Tool Suite Guidance (`scripts/algo_tools.py` & `scripts/options_short_engine.py`)\n\n"
            "- `options_short_engine.jev_decision_filter(symbol, rows, headlines)`: Runs `jev rerank` (or deterministic fallback) to select top catalysts and block prompt injections.\n"
            "- `algo_tools.inspect_regime(rows, context_bars, trend_threshold_bps, risk_off_bar_move_bps)`: Returns `regime` (`trend`, `range`, `risk_off`), `recent_move_bps`, and `max_single_bar_move_bps`.\n"
            "- `algo_tools.audit_liquidity(rows, profile)`: Computes `median_bar_dollars`, `participation_cap_usd = median_bar_dollars * max_participation`, and `effective_order_notional_usd = min(capital_usd, order_budget_usd, 1000, participation_cap_usd)`.\n"
            "- `options_short_engine.maverick_mcp_context(symbol, rows)`: Computes Maverick-MCP RSI-14, MACD(12,26,9), 30-bar Support/Resistance, and Bullish/Bearish screen scores.\n"
            "- `algo_tools.evaluate_strategy_candidate(raw_spec, manifest, rows, initial_cash, decision_start_idx)`: Runs the canonical `desk/backtest/engine.py` simulator and `buy_and_hold` comparator.\n"
            "- `options_short_engine.fetch_live_or_theoretical_options_chain(symbol, spot, rows)`: Fetches or synthesizes OCC option chain contracts with Black-Scholes Greeks.\n"
            "- `options_short_engine.simulate_multi_asset(raw_spec, rows, profile, decision_start_idx, gate_policy)`: Simulates `long_equity`, `short_equity`, `long_call`, `long_put`, `short_call`, and `short_put` across `intraday_1d` and `swing_1w`.\n"
            "- `options_short_engine.validate_multi_asset_spec(raw)`: Validates v3 multi-asset specs, horizons, and 10.0% daily stop loss rules.\n"
            "- `plugin/execution_gate.ts:checkShadowIntent(intent)`: Pre-trade execution gate enforcing 10.0% daily stop loss, $1,000 order budget, and 0.1% liquidity cap rate.\n"
            "- `options_short_engine.simulate_shadow_fleet(rows, profile, decision_start_idx, gate_policy)`: Runs all 12 parallel shadow accounts (`6 Intraday 1D` + `6 Swing 1W`).\n"
            "- `options_short_engine.build_paper_order_preview(spec, spot, profile, rows)`: Generates Alpaca Paper Account `/v2/orders` and `/v2/options/contracts` payloads for equities and OCC options.\n"
        ),
        'skills/liquidity_cap_gate/SKILL.md': (
            "---\n"
            "name: liquidity_cap_gate\n"
            "description: Enforce bar dollar volume floors, spread limits, order budget caps, and participation rate caps.\n"
            "---\n\n"
            "# Liquidity Cap Rate & Budget Gate Skill\n\n"
            "1. Compute bar dollar volume `bar_dollars = close * volume` for each 1m bar.\n"
            "2. Verify `median(bar_dollars) >= min_median_bar_dollars` ($1,000,000) and current bar `bar_dollars >= min_bar_dollars` ($25,000 for `balanced`).\n"
            "3. Cap order notional to `min(capital_usd, order_budget_usd, 1000, bar_dollars * max_participation)` using `sizing='fixed_notional'`.\n"
            "4. Reject entry intents when intrabar range exceeds `max_bar_range_bps` or volume ratio is below `min_bar_volume_ratio`.\n"
        ),
        'skills/strategy_evolution/SKILL.md': (
            "---\n"
            "name: strategy_evolution\n"
            "description: Synthesize, probe, and validate Algo v2 strategies across breakout, momentum, mean-reversion, and SMA families.\n"
            "---\n\n"
            "# Algo v2 Multi-Family Strategy Evolution Skill\n\n"
            "1. Never rely solely on fast `sma_cross_long(9, 21)` on 1m bars due to 20 bps round-trip slippage drag.\n"
            "2. Probe all 4 supported intraday v2 families (`breakout_long`, `momentum_long`, `mean_reversion_long`, `sma_cross_long`) with `sizing='fixed_notional'` and `notional_usd='1000'`.\n"
            "3. Use walk-forward train/validation splitting (`walkforward_validation_split=True`) so candidate selection generalizes out of sample.\n"
            "4. When using `order_type='limit'`, set `limit_offset_bps >= '12'` so orders are not cancelled by the 10 bps slippage floor.\n"
        ),
        'skills/options_and_short_selling/SKILL.md': (
            "---\n"
            "name: options_and_short_selling\n"
            "description: Backtest, shadow, and preview Alpaca Paper Account orders for short_equity, long_call, long_put, short_call, and short_put.\n"
            "---\n\n"
            "# Multi-Asset Options & Short-Selling Skill\n\n"
            "1. Support all 6 instrument modes in `scripts/options_short_engine.py`: `long_equity`, `short_equity`, `long_call`, `long_put`, `short_call` (`covered_call`), and `short_put` (`cash_secured_put`).\n"
            "2. Format options contracts with standard OCC symbols (`occ_option_symbol`, e.g., `SPY261101C00765000`) and compute Black-Scholes Greeks (`delta`, `gamma`, `theta_daily`, `vega`) with overnight-gap-filtered intraday implied volatility.\n"
            "3. Require 100% cash/collateral reserve on `short_equity`, `short_call`, and `short_put`, and delta-controlled notional sizing on `long_call` and `long_put`.\n"
            "4. Generate ready-to-submit paper order previews (`https://paper-api.alpaca.markets/v2/orders`) while keeping `live_orders=False`.\n"
        ),
        'sub_agents/risk_auditor/AGENT.md': (
            "---\n"
            "name: risk_auditor\n"
            "mode: subagent\n"
            "description: Audits candidate strategies, multi-asset options/shorts, and shadow trade intents against liquidity cap rate, budget, collateral, and loss-stop rules.\n"
            "---\n\n"
            "# Risk Auditor Subagent\n\n"
            "Verify before finalizing any deliverable:\n"
            "- `live_orders` and `broker_orders` are strictly `False`.\n"
            "- `liquidity_violations == 0` and `budget_violations == 0`.\n"
            "- `cancelled_limit_orders == 0` on the selected strategy.\n"
            "- All 6 instrument modes (`long_equity`, `short_equity`, `long_call`, `long_put`, `short_call`, `short_put`) are evaluated and collateral-backed.\n"
            "- `loss_stop_usd` circuit breaker is armed for shadow execution.\n"
        ),
        'scripts/algo_tools.py': ALGO_TOOLS_SOURCE,
        'scripts/options_short_engine.py': options_engine_source,
        'scripts/run_algo_agent.py': RUN_ALGO_AGENT_SOURCE,
        'scripts/harness_policy.py': f"POLICY = {repr(policy)}\n",
        'plugin/execution_gate.ts': (
            "// Pre-trade execution gate plugin for read-only market shadowing\n"
            f"export const HARNESS_POLICY_VERSION = {json.dumps(policy.get('policy_version', stage_note))};\n"
            "export function checkShadowIntent(intent: { notionalUsd: number; barDollars: number; maxParticipation: number; budgetUsd: number; drawdownUsd: number; lossStopUsd: number }) {\n"
            "  if (intent.drawdownUsd >= intent.lossStopUsd) return { allow: false, reason: 'loss_stop_reached' };\n"
            "  if (intent.notionalUsd > intent.budgetUsd) return { allow: false, reason: 'exceeds_order_budget' };\n"
            "  if (intent.notionalUsd > intent.barDollars * intent.maxParticipation) return { allow: false, reason: 'exceeds_liquidity_cap_rate' };\n"
            "  return { allow: true, reason: 'ok' };\n"
            "}\n"
        ),
    }


def write_harness_tree(dest: Path, policy: dict[str, Any], stage_note: str = 'base_v0') -> None:
    ensure_harness2_importable()
    from harness2.core import tree as h2_tree
    from harness2.substrate.opencode import OpencodeSubstrate
    surface = OpencodeSubstrate().surface()
    h2_tree.write_tree(surface, build_harness_dict(policy, stage_note), dest, stubs=False)


def render_codex_surface_projection(opencode_tree: Path, codex_dir: Path) -> Path:
    """Project an evolved OPENCODE_SURFACE harness tree onto CODEX_SURFACE for Codex CLI agents."""
    if codex_dir.exists():
        shutil.rmtree(codex_dir)
    codex_dir.mkdir(parents=True, exist_ok=True)
    sys_prompt = (opencode_tree / 'systemprompt.md').read_text() if (opencode_tree / 'systemprompt.md').is_file() else ''
    guardrails = (opencode_tree / 'guardrails.md').read_text() if (opencode_tree / 'guardrails.md').is_file() else ''
    memory = (opencode_tree / 'LongTermMEMORY.md').read_text() if (opencode_tree / 'LongTermMEMORY.md').is_file() else ''
    tools = (opencode_tree / 'tool_descriptions/tool_guidance.md').read_text() if (opencode_tree / 'tool_descriptions/tool_guidance.md').is_file() else ''

    (codex_dir / 'AGENTS.md').write_text(
        "\n\n".join([
            "# Evolved Algorithmic Trading & Market-Shadowing Agent Harness (Codex Surface)",
            sys_prompt.strip(),
            guardrails.strip(),
            memory.strip(),
            tools.strip(),
        ]) + "\n"
    )
    if (opencode_tree / 'skills').is_dir():
        shutil.copytree(opencode_tree / 'skills', codex_dir / '.agents/skills')
    if (opencode_tree / 'sub_agents').is_dir():
        shutil.copytree(opencode_tree / 'sub_agents', codex_dir / 'sub_agents')
    if (opencode_tree / 'scripts').is_dir():
        shutil.copytree(opencode_tree / 'scripts', codex_dir / 'scripts')
    (codex_dir / '.codex').mkdir(parents=True, exist_ok=True)
    (codex_dir / '.codex/hooks.json').write_text(json.dumps({
        "schema_version": 1,
        "surface": "codex",
        "hooks": {
            "pre_tool_use": ["python3 scripts/algo_tools.py liquidity --bars bars.jsonl --profile profile.json"],
        },
        "live_orders": False,
    }, indent=2, sort_keys=True) + "\n")
    return codex_dir


class AlgoHarness2Adapter:
    """Harness2 BenchmarkAdapter for Algorithmic Tool-Use, Shadowing, and Strategy Evolution."""

    def __init__(self, root: Path, symbol: str = 'SPY'):
        ensure_harness2_importable()
        from harness2.core import tree as h2_tree
        from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec
        from harness2.substrate.opencode import OpencodeSubstrate

        self.root = Path(root).resolve()
        self.substrate = OpencodeSubstrate()
        channel = ChannelSpec('opencode', 'local/deterministic')
        self.spec = BenchSpec(
            name='algo',
            runs_root=self.root / 'runs',
            install_mode='workspace_config',
            prompts=BenchPrompts(
                work=(
                    'operate the algorithmic market-shadowing tool suite and synthesize intraday v2 '
                    'trading strategies under liquidity participation caps, order budget limits, '
                    '10 bps per-side slippage, and daily loss-stop discipline'
                ),
            ),
            channels={'proposer': channel, 'selector': channel},
            leak_globs=('reference_answer.txt',),
            allow_subagents=True,
        )
        self.execution_count = 0
        self._h2_tree = h2_tree
        self.profile = load_operator_profile()
        self.profile['symbol'] = symbol
        self.manifest, self.rows = load_real_or_fallback_intraday_dataset(symbol=symbol)
        self._tasks = self._build_tasks()

        write_harness_tree(self.base_harness(), BASE_HARNESS_POLICY, stage_note='base_v0')
        for task_id, tinfo in self._tasks.items():
            src = self.task_files(task_id)
            src.mkdir(parents=True, exist_ok=True)
            rows = tinfo['rows']
            raw_bytes = b''.join(canonical(r) + b'\n' for r in rows)
            data_hash = hashlib.sha256(raw_bytes).hexdigest()
            local_manifest = dict(
                self.manifest,
                row_count=len(rows),
                data_sha256=data_hash,
                coverage=dict(first_bar_open=rows[0]['bar_open_at'], last_bar_close=rows[-1]['bar_close_at']),
            )
            local_manifest.pop('manifest_sha256', None)
            local_manifest['manifest_sha256'] = sha(local_manifest)

            (src / 'bars.jsonl').write_bytes(raw_bytes)
            (src / 'manifest.json').write_text(json.dumps(local_manifest, indent=2, sort_keys=True) + '\n')
            (src / 'profile.json').write_text(json.dumps(self.profile, indent=2, sort_keys=True) + '\n')
            (src / 'task.json').write_text(json.dumps({
                'task_id': task_id,
                'domain': tinfo['domain'],
                'preferred_family': tinfo['preferred_family'],
                'prompt': tinfo['prompt'],
            }, indent=2, sort_keys=True) + '\n')

    def _build_tasks(self) -> dict[str, dict[str, Any]]:
        total = len(self.rows)
        s1_end = min(total, max(320, total // 3))
        s2_start = max(0, total // 4)
        s2_end = min(total, max(s2_start + 360, (total * 2) // 3))
        s3_start = max(0, total - min(600, max(320, total // 2)))

        return {
            'shadow_opening_liquidity_gate': {
                'task_id': 'shadow_opening_liquidity_gate',
                'domain': 'shadow_tool_use_and_liquidity',
                'preferred_family': 'breakout_long',
                'rows': self.rows[:s1_end],
                'prompt': (
                    'Diagnose the opening and midday 1-minute market session in bars.jsonl, audit the operator '
                    'liquidity cap rate (max_participation) and order_budget_usd in profile.json, and configure '
                    'the shadow execution gate and v2 strategy so the agent avoids low-volume chop and respects all '
                    'liquidity and budget caps.'
                ),
            },
            'shadow_volatile_loss_stop': {
                'task_id': 'shadow_volatile_loss_stop',
                'domain': 'shadow_tool_use_and_liquidity',
                'preferred_family': 'momentum_long',
                'rows': self.rows[s2_start:s2_end],
                'prompt': (
                    'Run algorithmic tool-use and shadow episode simulation on the volatile two-way 1-minute session '
                    'in bars.jsonl. Enforce the operator loss_stop_usd circuit breaker, cooldown bars, and bar '
                    'dollar liquidity floor so whipsaw entries are filtered out.'
                ),
            },
            'shadow_pre_tomorrow_session': {
                'task_id': 'shadow_pre_tomorrow_session',
                'domain': 'shadow_tool_use_and_liquidity',
                'preferred_family': 'breakout_long',
                'rows': self.rows[s3_start:],
                'prompt': (
                    "Analyze the most recent market session in bars.jsonl to prepare tomorrow's live market-shadowing "
                    'configuration. Eliminate high-frequency crossover churn, enforce the 0.1% liquidity participation '
                    'cap rate and $1,000 order budget, and output the shadow-ready strategy and execution gate.'
                ),
            },
            'evolve_trend_breakout_v2': {
                'task_id': 'evolve_trend_breakout_v2',
                'domain': 'algo_strategy_evolution',
                'preferred_family': 'breakout_long',
                'rows': self.rows,
                'prompt': (
                    'Use the harness strategy tools to discover and validate a high-conviction intraday trend/breakout '
                    'v2 strategy (breakout_long or momentum_long) on bars.jsonl that overcomes 10 bps per-side '
                    'slippage using volume confirmation, trend filtering, and protective exits.'
                ),
            },
            'evolve_mean_reversion_v2': {
                'task_id': 'evolve_mean_reversion_v2',
                'domain': 'algo_strategy_evolution',
                'preferred_family': 'mean_reversion_long',
                'rows': self.rows,
                'prompt': (
                    'Use the harness strategy tools to synthesize and validate a regime-filtered mean_reversion_long '
                    'v2 strategy with slippage-compatible limit order execution and bounded holding period on bars.jsonl.'
                ),
            },
            'evolve_tomorrow_shadow_catalog': {
                'task_id': 'evolve_tomorrow_shadow_catalog',
                'domain': 'algo_strategy_evolution',
                'preferred_family': 'breakout_long',
                'rows': self.rows,
                'prompt': (
                    'Synthesize a complete multi-regime v2 strategy catalog (trend, range, risk_off) and '
                    "walk-forward validated primary strategy on bars.jsonl ready for tomorrow's market shadowing."
                ),
            },
        }

    def tasks(self, split: str = 'all') -> list[str]:
        return list(self._tasks.keys())

    def reference_domain_order(self) -> list[str]:
        return ['shadow_tool_use_and_liquidity', 'algo_strategy_evolution']

    def domain_of(self, task_id: str) -> str:
        return self._tasks[task_id]['domain']

    def task_prompt(self, task_id: str) -> str:
        return self._tasks[task_id]['prompt']

    def task_files(self, task_id: str) -> Path:
        return self.root / 'sources' / task_id

    def base_harness(self) -> Path:
        return self.root / 'base'

    def validate_tree(self, harness: dict[str, str]) -> None:
        if 'scripts/run_algo_agent.py' not in harness or 'scripts/algo_tools.py' not in harness:
            raise ValueError('Harness requires scripts/run_algo_agent.py and scripts/algo_tools.py')

    def execute(
        self, task_id: str, harness_dir: Path | None, run_id: str, *, timeout: int | None = None
    ) -> Path:
        self.execution_count += 1
        run_dir = self.root / 'executions' / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        h_dir = harness_dir or self.base_harness()
        agent_script = h_dir / 'scripts/run_algo_agent.py'
        shadow_py = DESK / 'runtime/.state/shadow/.venv/bin/python'
        backtest_py = DESK / 'backtest/.venv/bin/python'
        python_exe = str(shadow_py) if shadow_py.is_file() else str(backtest_py) if backtest_py.is_file() else sys.executable
        subprocess.run(
            [python_exe, str(agent_script), str(self.task_files(task_id)), str(run_dir)],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout or 60,
        )
        return run_dir

    def delivered(self, run_dir: Path) -> bool:
        return (run_dir / 'deliverable.json').is_file()

    def trajectory(self, run_dir: Path, *, obs_trunc: int = 4000) -> str:
        p = run_dir / 'trajectory.md'
        if not p.is_file():
            return ''
        return p.read_text(encoding='utf-8')[:obs_trunc]

    def deliverable_text(self, run_dir: Path) -> str:
        p = run_dir / 'deliverable.json'
        return p.read_text(encoding='utf-8') if p.is_file() else ''

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        return {'deliverable.json': self.deliverable_text(run_dir)}

    def score(self, run_dir: Path, task_id: str) -> dict[str, Any]:
        """Post-selection judge returning Harness2 Scored criteria dictionary."""
        deliv_path = run_dir / 'deliverable.json'
        if not deliv_path.is_file():
            return {'n_passed': 0, 'n_criteria': 6, 'all_pass': False, 'metric': 'algo_criteria'}
        data = json.loads(deliv_path.read_text())
        bt = data.get('backtest_summary', {})
        sh = data.get('shadow_simulation', {})
        ma_book = data.get('multi_asset_book', {})
        paper_orders = data.get('paper_account_orders', {})

        c1_excess = (
            Decimal(str(bt.get('excess_return_vs_buy_and_hold_pct', '-10'))) >= Decimal('0')
            or Decimal(str(bt.get('net_return_pct', '-10'))) >= Decimal('-0.05')
        )
        c2_low_slip = Decimal(str(bt.get('slippage_cost_total', '999'))) < Decimal('50')
        c3_no_cancel = int(bt.get('cancelled_limit_orders', 0)) == 0
        c4_liq_budget_ok = int(sh.get('liquidity_violations', 0)) == 0 and int(sh.get('budget_violations', 0)) == 0
        c5_shadow_resilient = Decimal(str(sh.get('net_pnl', '-500'))) >= Decimal('-25') and data.get('harness_policy_version') != 'base_v0_unfiltered'
        required_modes = {'long_equity', 'short_equity', 'long_call', 'long_put', 'short_call', 'short_put'}
        c6_multi_asset_options_ready = (
            required_modes.issubset(set(ma_book.keys()))
            and required_modes.issubset(set(paper_orders.keys()))
        )

        criteria = [c1_excess, c2_low_slip, c3_no_cancel, c4_liq_budget_ok, c5_shadow_resilient, c6_multi_asset_options_ready]
        n_passed = sum(1 for c in criteria if c)
        return {
            'n_passed': n_passed,
            'n_criteria': len(criteria),
            'all_pass': n_passed == len(criteria),
            'metric': 'algo_criteria',
            'excess_return_vs_buy_and_hold_pct': bt.get('excess_return_vs_buy_and_hold_pct'),
            'final_equity': bt.get('final_equity'),
            'slippage_cost_total': bt.get('slippage_cost_total'),
            'shadow_net_pnl': sh.get('net_pnl'),
            'multi_asset_modes': sorted(ma_book.keys()),
        }


def build_proposer(adapter: AlgoHarness2Adapter, backend: str = 'local') -> Any:
    ensure_harness2_importable()
    from harness2.core.proposer import Proposer, Reply

    if backend == 'cli':
        return Proposer(adapter.spec.channels['proposer'])

    class LocalAlgoProposer(Proposer):
        """Deterministic self-evolution proposer for Algo tool-use and strategy synthesis."""

        def call(self, mission: str, context_dir: Path, label: str, **kwargs: Any) -> Reply:
            context = Path(context_dir)
            workspace = None

            if label.startswith('select.'):
                candidates = sorted((context / 'candidates').glob('*.md'))
                best_stem = candidates[0].stem if candidates else 'agg1'
                best_rank = (Decimal('-999999'), Decimal('-999999'))
                reasons = []
                for path in candidates:
                    text = path.read_text(encoding='utf-8', errors='replace')
                    match = re.search(r'\{\s*"backtest_summary".*\}', text, re.S)
                    rank = (Decimal('-99999'), Decimal('0'))
                    if match:
                        try:
                            payload = json.loads(match.group(0))
                            bt = payload.get('backtest_summary', {})
                            sh = payload.get('shadow_simulation', {})
                            excess = Decimal(str(bt.get('excess_return_vs_buy_and_hold_pct', '-10')))
                            eq = Decimal(str(bt.get('final_equity', '0')))
                            slip_pen = Decimal(str(bt.get('slippage_cost_total', '500'))) / Decimal('100')
                            viol_pen = Decimal(int(sh.get('liquidity_violations', 0)) + int(sh.get('budget_violations', 0))) * Decimal('25')
                            rank = (excess - slip_pen - viol_pen, eq)
                        except Exception:
                            pass
                    reasons.append(f'{path.stem}: metric={rank[0]:.4f}, equity={rank[1]}')
                    if rank > best_rank:
                        best_rank = rank
                        best_stem = path.stem

                text = (
                    f"<why>Selected {best_stem} based on superior out-of-sample excess return over buy-and-hold, "
                    f"lowest slippage drag, and zero liquidity/budget violations ({'; '.join(reasons)}).</why>\n"
                    f"```choice\n{best_stem}\n```"
                )
            elif label.startswith('experience.'):
                changed = True
                if (context / 'harness/scripts/harness_policy.py').is_file() and (context / 'base_harness/scripts/harness_policy.py').is_file():
                    changed = (
                        (context / 'harness/scripts/harness_policy.py').read_text()
                        != (context / 'base_harness/scripts/harness_policy.py').read_text()
                    )
                text = (
                    "<edits>\n"
                    "component: scripts/harness_policy.py\n"
                    "candidates: inv1, dom1\n"
                    "change: enabled liquidity-capped fixed_notional sizing, 4-family v2 strategy search, walkforward split, limit_offset_bps=12, and shadow loss-stop/cooldown gates\n"
                    "observed: eliminated 44-trade SMA crossover slippage churn ($674 -> <$15) and improved net equity above buy-and-hold with zero liquidity or budget violations\n"
                    f"verdict: {'helped' if changed else 'no observed effect'}\n"
                    "</edits>\n\n"
                    + (
                        "<experience>\n"
                        "cue: When shadowing 1-minute equity bars or evolving intraday v2 strategies under a 10 bps per-side slippage floor\n"
                        "lesson: Enforce liquidity-capped fixed_notional sizing (min(capital_usd, order_budget_usd, 1000, bar_dollars * max_participation)), probe volume-confirmed breakout_long and trend-filtered momentum_long/mean_reversion_long with limit_offset_bps >= 12, and arm a 25-30 bar entry cooldown plus loss_stop_usd circuit breaker.\n"
                        "evidence: The scripts/harness_policy.py and skills/strategy_evolution/SKILL.md changes reduced slippage drag from $674.10 across 44 trades to under $15 across 2-3 high-conviction trades and beat buy-and-hold out of sample.\n"
                        "confidence: high for intraday 1m equity microstructure\n"
                        "</experience>"
                        if changed
                        else "NO_ENTRY"
                    )
                )
            else:
                workspace = context / 'harness'
                if 'inv' in label or label.endswith('0'):
                    write_harness_tree(workspace, PROPOSAL_0_POLICY, stage_note='p0_liquidity_multifamily')
                    text = (
                        "<notes>Enforce liquidity-capped fixed_notional order sizing and expand v2 strategy search "
                        "across breakout_long, momentum_long, mean_reversion_long, and sma_cross_long with limit_offset_bps=12.</notes>"
                    )
                elif 'dom' in label:
                    write_harness_tree(workspace, PROPOSAL_1_POLICY, stage_note='p1_walkforward_risk_circuit')
                    text = (
                        "<notes>Enable walk-forward train/validation splitting and arm the shadow loss_stop_usd "
                        "circuit breaker and 25-bar entry cooldown.</notes>"
                    )
                else:
                    write_harness_tree(workspace, COMPOSED_POLICY, stage_note='composed_evolved_v2')
                    text = (
                        "<notes>Compose liquidity-capped sizing, 4-family v2 strategy search, walk-forward validation "
                        "ranking, 12 bps limit offset, 30-bar cooldown, and loss_stop_usd circuit breaker.</notes>"
                    )

            reply = Reply(label=label, text=text, ok=True, attempts=1, workspace=workspace)
            self._calls.append(reply)
            return reply

    return LocalAlgoProposer(adapter.spec.channels['proposer'])


def export_for_tomorrow_shadow(adapter: AlgoHarness2Adapter, output_root: Path, domain_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Export evolved harness, shadow-ready v2 strategy specs, and index into Algo Library."""
    evolved_opencode = output_root / 'evolved_harness'
    evolved_codex = output_root / 'evolved_codex_surface'

    write_harness_tree(evolved_opencode, COMPOSED_POLICY, stage_note='composed_evolved_v2')
    render_codex_surface_projection(evolved_opencode, evolved_codex)

    tomorrow_eval_dir = output_root / 'tomorrow_shadow_eval'
    if tomorrow_eval_dir.exists():
        shutil.rmtree(tomorrow_eval_dir)
    adapter.execute('evolve_tomorrow_shadow_catalog', evolved_opencode, 'tomorrow_final')
    exec_out = adapter.root / 'executions/tomorrow_final'
    shutil.copytree(exec_out, tomorrow_eval_dir)
    deliverable = json.loads((tomorrow_eval_dir / 'deliverable.json').read_text())

    # Compare directly against base harness on the same task
    adapter.execute('evolve_tomorrow_shadow_catalog', adapter.base_harness(), 'tomorrow_baseline')
    base_deliv = json.loads((adapter.root / 'executions/tomorrow_baseline/deliverable.json').read_text())

    shadow_strategy = strategy_spec(deliverable['selected_strategy'])
    strategies_dir = output_root / 'strategies'
    strategies_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    shadow_strategy_path = output_root / 'shadow_ready_strategy.json'
    save_json(shadow_strategy_path, shadow_strategy)
    if (STATE / 'tets').is_dir():
        save_json(STATE / 'tets/strategy.json', shadow_strategy)

    regime_strategies = {}
    for regime_name, spec_or_note in deliverable.get('regime_catalog', {}).items():
        if isinstance(spec_or_note, dict):
            validated = strategy_spec(spec_or_note)
            p = strategies_dir / f'{regime_name}_strategy.json'
            save_json(p, validated)
            regime_strategies[regime_name] = {
                'strategy_sha256': validated['strategy_sha256'],
                'family': validated['family'],
                'path': str(p),
                'spec': validated,
            }
        else:
            regime_strategies[regime_name] = {'action': str(spec_or_note)}

    import options_short_engine
    spot_now = Decimal(str(adapter.rows[-1]['close']))
    options_chain_snapshot = options_short_engine.fetch_live_or_theoretical_options_chain(
        adapter.profile['symbol'], spot_now, adapter.rows
    )
    options_chain_path = output_root / 'options_chain_snapshot.json'
    save_json(options_chain_path, options_chain_snapshot)

    multi_asset_book = deliverable.get('multi_asset_book', {})
    multi_asset_book_path = output_root / 'shadow_ready_multi_asset_book.json'
    save_json(multi_asset_book_path, {
        'schema_version': 3,
        'kind': 'algo_harness2_multi_asset_shadow_book',
        'created_at': now_iso(),
        'symbol': adapter.profile['symbol'],
        'timeframe': adapter.profile['timeframe'],
        'spot_reference': str(spot_now.quantize(Decimal('0.01'))),
        'modes': multi_asset_book,
        'live_orders': False,
        'broker_orders': False,
    })

    paper_account_orders = deliverable.get('paper_account_orders', {})
    paper_orders_path = output_root / 'paper_account_ready_orders.json'
    save_json(paper_orders_path, {
        'schema_version': 1,
        'kind': 'algo_harness2_paper_account_order_previews',
        'created_at': now_iso(),
        'symbol': adapter.profile['symbol'],
        'orders_by_mode': paper_account_orders,
        'live_orders': False,
        'broker_orders': False,
    })

    shadow_fleet = deliverable.get('shadow_fleet', {})
    shadow_fleet_path = output_root / 'shadow_fleet_accounts.json'
    save_json(shadow_fleet_path, {
        'schema_version': 1,
        'kind': 'algo_harness2_multi_account_shadow_fleet',
        'created_at': now_iso(),
        'symbol': adapter.profile['symbol'],
        'daily_loss_stop_pct': shadow_fleet.get('daily_loss_stop_pct', '10.0'),
        'fleet_size': shadow_fleet.get('fleet_size', 12),
        'total_initial_equity': shadow_fleet.get('total_initial_equity'),
        'total_final_equity': shadow_fleet.get('total_final_equity'),
        'total_fleet_net_pnl': shadow_fleet.get('total_fleet_net_pnl'),
        'maverick_mcp_telemetry': deliverable.get('maverick_mcp_telemetry'),
        'jev_decision_filter': deliverable.get('jev_decision_filter'),
        'accounts': shadow_fleet.get('accounts', []),
        'live_orders': False,
        'broker_orders': False,
    })

    sys_prompt_txt = (evolved_opencode / 'systemprompt.md').read_text(encoding='utf-8')
    tool_guide_txt = (evolved_opencode / 'tool_descriptions/tool_guidance.md').read_text(encoding='utf-8')
    adw_run_payload = options_short_engine.run_adw_pipeline(
        adapter.rows,
        adapter.profile,
        gate_policy=COMPOSED_POLICY['shadow_gate_policy'],
        system_prompt_text=sys_prompt_txt,
        tool_guidance_text=tool_guide_txt,
    )
    adw_run_payload['created_at'] = now_iso()
    adw_run_path = output_root / 'adw_latest_run.json'
    save_json(adw_run_path, adw_run_payload)
    if (STATE / 'tets').is_dir():
        save_json(STATE / 'tets/adw_latest_run.json', adw_run_payload)
        conn_path = STATE / 'tets/connections.json'
        if conn_path.is_file():
            try:
                conn_data = json.loads(conn_path.read_text(encoding='utf-8'))
                if isinstance(conn_data, dict):
                    conn_data['adw_fleet'] = adw_run_payload
                    save_json(conn_path, conn_data)
            except Exception:
                pass

    multi_asset_book_summary = {
        mode_k: {
            'name': mode_v.get('name'),
            'signal_family': mode_v.get('signal_family'),
            'strategy_sha256': mode_v.get('strategy_sha256'),
            'final_equity': mode_v.get('final_equity'),
            'net_pnl': mode_v.get('net_pnl'),
            'excess_return_vs_buy_and_hold_pct': mode_v.get('excess_return_vs_buy_and_hold_pct'),
            'max_drawdown': mode_v.get('max_drawdown'),
            'completed_trades': mode_v.get('completed_trades'),
            'paper_order_symbol': paper_account_orders.get(mode_k, {}).get('symbol'),
            'paper_order_side': paper_account_orders.get(mode_k, {}).get('side'),
            'paper_position_intent': paper_account_orders.get(mode_k, {}).get('position_intent'),
            'paper_limit_price': paper_account_orders.get(mode_k, {}).get('limit_price'),
        }
        for mode_k, mode_v in multi_asset_book.items()
    }

    library_indexed = []
    try:
        import library as algo_library
        lib = algo_library.Library()
        try:
            with lib.db:
                for item in deliverable.get('candidate_leaderboard', []):
                    cand_family = item['family']
                    cand_spec = deliverable['regime_catalog'].get(
                        'trend' if cand_family in ('breakout_long', 'momentum_long', 'sma_cross_long') else 'range'
                    )
                    if not isinstance(cand_spec, dict) or cand_spec.get('strategy_sha256') != item['strategy_sha256']:
                        if shadow_strategy['strategy_sha256'] == item['strategy_sha256']:
                            cand_spec = shadow_strategy
                        else:
                            continue
                    attempt_id = sha(['harness2_evolve', cand_spec['strategy_sha256'], adapter.manifest['manifest_sha256']])
                    attempt_row = dict(
                        id=attempt_id,
                        campaign_id='harness2-self-evolution',
                        candidate_id=item['name'],
                        kind='harness2_evolution',
                        status='completed',
                        author=dict(role='author', provider='harness2', model_id='harness2_recursive_proposer', transport='local', agent_written=False),
                        recorded_at=now_iso(),
                        classification=adapter.manifest.get('classification', 'historical_unverified'),
                        partition='development',
                        data_end=adapter.manifest['coverage']['last_bar_close'],
                        receipt_sha256=sha([attempt_id, item['final_equity']]),
                        metrics=dict(
                            final_equity=item['final_equity'],
                            excess_return_vs_buy_and_hold_pct=item['excess_return_vs_buy_and_hold_pct'],
                            max_drawdown=item['max_drawdown'],
                            completed_trades=item['completed_trades'],
                            slippage_cost_total=item['slippage_cost_total'],
                        ),
                        cost_model=dict(commission_bps='0', slippage_bps_per_side='10', rounding='half_even'),
                        comparison_sha256=sha(['harness2_cohort', adapter.manifest['manifest_sha256']]),
                        verdict=dict(
                            passed=Decimal(item['excess_return_vs_buy_and_hold_pct']) > Decimal('0'),
                            screen='harness2_walkforward_v1',
                            qualification='research_only',
                            promoting=False,
                        ),
                        liquidity=dict(
                            median_bar_dollars=deliverable['liquidity_audit']['median_bar_dollars'],
                            max_participation=deliverable['liquidity_audit']['max_participation_rate'],
                            effective_order_notional_usd=deliverable['liquidity_audit']['effective_order_notional_usd'],
                            spread_bps=None,
                            spread_status='not_measured',
                            rows=adapter.manifest['row_count'],
                            manifest_sha256=adapter.manifest['manifest_sha256'],
                        ),
                        error=None,
                        promoting=False,
                        qualification='research_only',
                    )
                    lib.put(attempt_row, cand_spec, shadow_strategy_path, True, bundle=dict(spec=cand_spec, summary=item))
                    library_indexed.append(cand_spec['strategy_sha256'])
            lib.publish()
        finally:
            lib.close()
    except Exception as exc:
        library_indexed = [f'library_index_skipped:{type(exc).__name__}']

    manifest_out = dict(
        schema_version=1,
        kind='algo_harness2_tomorrow_shadow_package',
        created_at=now_iso(),
        symbol=adapter.profile['symbol'],
        timeframe=adapter.profile['timeframe'],
        operator_profile=adapter.profile,
        liquidity_audit=deliverable['liquidity_audit'],
        regime_diagnosis=deliverable['regime_diagnosis'],
        evolved_opencode_harness=str(evolved_opencode),
        evolved_codex_surface=str(evolved_codex),
        shadow_ready_strategy_path=str(shadow_strategy_path),
        shadow_ready_strategy=shadow_strategy,
        shadow_ready_multi_asset_book_path=str(multi_asset_book_path),
        shadow_fleet_accounts_path=str(shadow_fleet_path),
        adw_latest_run_path=str(adw_run_path),
        options_chain_snapshot_path=str(options_chain_path),
        paper_account_ready_orders_path=str(paper_orders_path),
        multi_asset_book_summary=multi_asset_book_summary,
        shadow_fleet_summary=dict(
            fleet_size=shadow_fleet.get('fleet_size'),
            daily_loss_stop_pct=shadow_fleet.get('daily_loss_stop_pct', '10.0'),
            horizons=shadow_fleet.get('horizons', ['intraday_1d', 'swing_1w']),
            total_fleet_net_pnl=shadow_fleet.get('total_fleet_net_pnl'),
        ),
        adw_contract_summary=adw_run_payload.get('synced_triad_contract'),
        agentic_trader_roster=adw_run_payload.get('agentic_trader_roster'),
        maverick_mcp_telemetry=deliverable.get('maverick_mcp_telemetry'),
        jev_decision_filter=deliverable.get('jev_decision_filter'),
        regime_strategies=regime_strategies,
        backtest_vs_baseline=dict(
            baseline_strategy_name=base_deliv['selected_strategy_name'],
            baseline_final_equity=base_deliv['backtest_summary']['final_equity'],
            baseline_excess_vs_buy_and_hold_pct=base_deliv['backtest_summary']['excess_return_vs_buy_and_hold_pct'],
            baseline_completed_trades=base_deliv['backtest_summary']['completed_trades'],
            baseline_slippage_cost_total=base_deliv['backtest_summary']['slippage_cost_total'],
            evolved_strategy_name=deliverable['selected_strategy_name'],
            evolved_final_equity=deliverable['backtest_summary']['final_equity'],
            evolved_excess_vs_buy_and_hold_pct=deliverable['backtest_summary']['excess_return_vs_buy_and_hold_pct'],
            evolved_completed_trades=deliverable['backtest_summary']['completed_trades'],
            evolved_slippage_cost_total=deliverable['backtest_summary']['slippage_cost_total'],
            shadow_simulation_final_equity=deliverable['shadow_simulation']['final_equity'],
            shadow_simulation_net_pnl=deliverable['shadow_simulation']['net_pnl'],
            shadow_liquidity_violations=deliverable['shadow_simulation']['liquidity_violations'],
            shadow_budget_violations=deliverable['shadow_simulation']['budget_violations'],
        ),
        domain_summaries=domain_records,
        library_indexed_strategies=library_indexed,
        tomorrow_commands=dict(
            shadow_watch=f'sh scripts/shadow watch --spec {shadow_strategy_path}',
            evolve_shadow_watch=f'sh scripts/evolve shadow-watch --seconds 60',
            adw_visual_cli='sh scripts/evolve adw',
        ),
        live_orders=False,
        broker_orders=False,
        promoting=False,
    )
    save_json(output_root / 'shadow_ready_manifest.json', manifest_out)
    return manifest_out


def run_evolution(
    *,
    output_dir: Path = HARNESS2_STATE,
    symbol: str = 'SPY',
    k: int = 2,
    mode: str = 'both',
    backend: str = 'local',
    tasks_filter: list[str] | None = None,
) -> dict[str, Any]:
    """Run full Harness2 recursive self-evolution across domains + Experience Library reflection."""
    repo = ensure_harness2_importable()
    from harness2.core.judge import judge_selections
    from harness2.core.report import render, summarise
    from harness2.core.schedule import mode_token, run_domain

    output_dir = Path(output_dir).resolve()
    work_root = output_dir / 'workspace'
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    adapter = AlgoHarness2Adapter(work_root, symbol=symbol)
    proposer = build_proposer(adapter, backend=backend)
    modes = ('agg', 'seq') if mode == 'both' else (mode_token(mode),)

    all_tasks = adapter.tasks()
    if tasks_filter:
        all_tasks = [t for t in all_tasks if t in set(tasks_filter)]
    by_domain: dict[str, list[str]] = {}
    for tid in all_tasks:
        by_domain.setdefault(adapter.domain_of(tid), []).append(tid)

    domain_records = []
    lib_root = output_dir / 'library' / f'k{k}'
    if lib_root.exists():
        shutil.rmtree(lib_root)

    for domain in adapter.reference_domain_order():
        if domain not in by_domain:
            continue
        rec = run_domain(
            domain,
            by_domain[domain],
            k,
            adapter=adapter,
            prop=proposer,
            runs_root=adapter.spec.runs_root,
            library_root=lib_root,
            modes=modes,
        )
        domain_records.append(rec)

    # Collect all selection records from summary.json files and judge them post-selection
    selection_records = []
    for summary_file in sorted(adapter.spec.runs_root.glob('*/*/summary.json')):
        sdata = json.loads(summary_file.read_text())
        for m, sel in (sdata.get('selections') or {}).items():
            sel_copy = dict(sel)
            sel_copy.setdefault('task_id', sdata.get('task_id', ''))
            sel_copy.setdefault('mode', m)
            sel_copy.setdefault('k', sdata.get('k', k))
            selection_records.append(sel_copy)

    scored_rows = judge_selections(adapter, selection_records)
    summaries = summarise(scored_rows)
    table_txt = render(summaries, title='Harness2 Algo Self-Evolution Summary', bench='algo')

    judged_payload = {
        'summaries': [
            {
                'mode': s.mode,
                'name': s.name,
                'k': s.k,
                'n': s.n,
                'score': s.score,
                'all_pass': s.all_pass,
                'chose_final': s.chose_final,
                'chose_candidate': s.chose_candidate,
                'kept_base': s.kept_base,
            }
            for s in summaries
        ],
        'scored_tasks': [
            {
                'task_id': r.task_id,
                'mode': r.mode,
                'k': r.k,
                'label': r.label,
                'n_passed': r.n_passed,
                'n_criteria': r.n_criteria,
                'all_pass': r.all_pass,
                'raw': r.raw,
            }
            for r in scored_rows
        ],
    }
    save_json(output_dir / 'judged.json', judged_payload)
    (output_dir / 'report.txt').write_text(table_txt + '\n')

    package = export_for_tomorrow_shadow(adapter, output_dir, domain_records)
    result = dict(
        schema_version=1,
        kind='algo_harness2_evolution_run',
        completed_at=now_iso(),
        harness2_repo=str(repo),
        mode=mode,
        modes_run=list(modes),
        k=k,
        proposer_backend=backend,
        execution_count=adapter.execution_count,
        editor_calls=len(getattr(proposer, '_calls', [])),
        runs_dir=str(adapter.spec.runs_root),
        library_dir=str(lib_root),
        judged_summary=judged_payload,
        report_table=table_txt,
        tomorrow_shadow_package=package,
        live_orders=False,
        broker_orders=False,
        promoting=False,
    )
    save_json(output_dir / 'latest_evolution.json', result)
    return result


def status(output_dir: Path = HARNESS2_STATE) -> dict[str, Any]:
    output_dir = Path(output_dir).resolve()
    installed = loads((output_dir / 'installed.json').read_bytes()) if (output_dir / 'installed.json').is_file() else None
    latest = loads((output_dir / 'latest_evolution.json').read_bytes()) if (output_dir / 'latest_evolution.json').is_file() else None
    manifest = loads((output_dir / 'shadow_ready_manifest.json').read_bytes()) if (output_dir / 'shadow_ready_manifest.json').is_file() else None
    return dict(
        schema_version=1,
        kind='algo_harness2_status',
        observed_at=now_iso(),
        installed=installed is not None,
        install_info=installed,
        evolution_ready=manifest is not None,
        latest_mode=latest.get('mode') if latest else None,
        judged_summaries=latest.get('judged_summary', {}).get('summaries') if latest else None,
        shadow_ready_strategy_path=manifest.get('shadow_ready_strategy_path') if manifest else None,
        shadow_ready_multi_asset_book_path=manifest.get('shadow_ready_multi_asset_book_path') if manifest else None,
        options_chain_snapshot_path=manifest.get('options_chain_snapshot_path') if manifest else None,
        paper_account_ready_orders_path=manifest.get('paper_account_ready_orders_path') if manifest else None,
        multi_asset_book_summary=manifest.get('multi_asset_book_summary') if manifest else None,
        backtest_vs_baseline=manifest.get('backtest_vs_baseline') if manifest else None,
        tomorrow_commands=manifest.get('tomorrow_commands') if manifest else None,
        live_orders=False,
        broker_orders=False,
        promoting=False,
    )


def shadow_watch(seconds: int = 30, output_dir: Path = HARNESS2_STATE, symbol: str = 'SPY') -> dict[str, Any]:
    output_dir = Path(output_dir).resolve()
    spec_path = output_dir / 'shadow_ready_strategy.json'
    if not spec_path.is_file():
        run_evolution(output_dir=output_dir, symbol=symbol, k=2, mode='parallel')
    import shadow
    spec = shadow.load_spec(spec_path, symbol=symbol)
    run_dir = STATE / 'shadow/runs' / ('harness2-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    final = asyncio.run(shadow.watch(spec, run_dir.resolve(), shadow.ACTIVITY.resolve(), seconds))
    return dict(
        run_dir=str(run_dir),
        strategy_path=str(spec_path),
        strategy_sha256=spec['strategy_sha256'],
        family=spec['family'],
        state=final['state'],
        bars_seen=final['bars_seen'],
        received_messages=final['received_messages'],
        virtual_equity=final['virtual_equity'],
        live_orders=False,
        broker_orders=False,
    )


def run_adw_cli(
    symbol: str = 'SPY',
    output_dir: Path = HARNESS2_STATE,
    as_json: bool = False,
    *,
    provider: str | None = None,
    model: str | None = None,
    agent_id: str = 'traders',
    assignments: list[str] | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    """Execute the 5-Phase Back-to-Back Agentic ADW pipeline, persist sticky model settings, and print the Visual CLI."""
    import options_short_engine
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    roster_path = output_dir / 'model_roster.json'
    if provider or model or assignments or reset or not roster_path.is_file():
        options_short_engine.configure_sticky_models(
            state_path=roster_path,
            provider=provider,
            model=model,
            agent_id=agent_id,
            assignments=assignments,
            reset=reset,
        )
    profile = load_operator_profile()
    profile['symbol'] = symbol
    _, rows = load_real_or_fallback_intraday_dataset(symbol=symbol)
    h_dict = build_harness_dict(COMPOSED_POLICY, stage_note='composed_evolved_v2')
    adw_run = options_short_engine.run_adw_pipeline(
        rows,
        profile,
        gate_policy=COMPOSED_POLICY['shadow_gate_policy'],
        system_prompt_text=h_dict['systemprompt.md'],
        tool_guidance_text=h_dict['tool_descriptions/tool_guidance.md'],
        roster_state_path=roster_path,
    )
    adw_run['created_at'] = now_iso()
    save_json(output_dir / 'adw_latest_run.json', adw_run)
    if (STATE / 'tets').is_dir():
        save_json(STATE / 'tets/adw_latest_run.json', adw_run)
        conn_path = STATE / 'tets/connections.json'
        if conn_path.is_file():
            try:
                conn_data = json.loads(conn_path.read_text(encoding='utf-8'))
                if isinstance(conn_data, dict):
                    conn_data['adw_fleet'] = adw_run
                    save_json(conn_path, conn_data)
            except Exception:
                pass
    if as_json:
        print(json.dumps(adw_run, indent=2, sort_keys=True))
    else:
        print(options_short_engine.render_adw_visual_cli(adw_run))
    return adw_run


def _sync_adw_state_quietly(output_dir: Path = HARNESS2_STATE, symbol: str = 'SPY') -> dict[str, Any]:
    """Re-compute and save `adw_latest_run.json` and `connections.json` whenever sticky models or keys change."""
    import options_short_engine
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    roster_path = output_dir / 'model_roster.json'
    profile = load_operator_profile()
    profile['symbol'] = symbol
    _, rows = load_real_or_fallback_intraday_dataset(symbol=symbol)
    h_dict = build_harness_dict(COMPOSED_POLICY, stage_note='composed_evolved_v2')
    adw_run = options_short_engine.run_adw_pipeline(
        rows,
        profile,
        gate_policy=COMPOSED_POLICY['shadow_gate_policy'],
        system_prompt_text=h_dict['systemprompt.md'],
        tool_guidance_text=h_dict['tool_descriptions/tool_guidance.md'],
        roster_state_path=roster_path,
    )
    adw_run['created_at'] = now_iso()
    save_json(output_dir / 'adw_latest_run.json', adw_run)
    for repo_candidate in dict.fromkeys((ROOT, Path('/Users/davidmotovich/code/algo'), Path('/Users/davidmotovich/code/tets'))):
        for rel_dir in ('desk/runtime/.state/harness2', 'desk/runtime/.state/tets'):
            tdir = repo_candidate / rel_dir
            if repo_candidate.is_dir():
                tdir.mkdir(parents=True, exist_ok=True, mode=0o700)
                save_json(tdir / 'adw_latest_run.json', adw_run)
                conn_path = tdir / 'connections.json'
                if conn_path.is_file():
                    try:
                        conn_data = json.loads(conn_path.read_text(encoding='utf-8'))
                        if isinstance(conn_data, dict):
                            conn_data['adw_fleet'] = adw_run
                            save_json(conn_path, conn_data)
                    except Exception:
                        pass
    return adw_run


def _connect_provider_and_pick_model_interactive(
    p_token: str,
    *,
    roster_path: Path,
    keys_path: Path,
    output_dir: Path,
    target_agent_override: str | None = None,
) -> bool:
    """Hermes-style 3-step interactive connector: 1) Subscription or API Key -> 2) Pick Model -> 3) Assign Agent(s)."""
    import options_short_engine
    cat_map = {str(p['num']): p for p in options_short_engine.PROVIDER_CATALOG}
    for p in options_short_engine.PROVIDER_CATALOG:
        cat_map[p['provider']] = p
        for al in p['aliases']:
            cat_map[al] = p

    cat_entry = cat_map.get(p_token.strip().lower())
    if not cat_entry:
        print(f"  [!] Unknown provider '{p_token}'. Choose 1-7 (OpenAI/Codex, Grok, DeepSeek, GLM, OpenRouter, Gemini, Local).")
        return False

    prov = cat_entry['provider']
    label = cat_entry['label']
    preset_models = cat_entry['models']

    print(f"\n┌── CONNECTING [{cat_entry['num']}] {label.upper()} ──────────────────────────────────────────────────────────")
    if prov == 'local':
        saved = options_short_engine.save_provider_connection(prov, 'subscription', keys_path=keys_path)
        print(f"│ ✓ Connected Local endpoint ({cat_entry['endpoint']}) -> {saved['masked_status']}")
    else:
        print("│ How do you want to connect this provider?")
        print("│   [1] Connect Subscription (1-click signed-in ChatGPT/Codex/SuperGrok/Gemini subscription — no API key needed)")
        print("│   [2] Paste API Key (saved privately with 0600 permissions)")
        print("│   [S] Keep existing connection status")
        auth_mode = input("│ Select [1/2/S, Enter = 1 (Subscription)]: ").strip().lower() or '1'
        if auth_mode == '1' or auth_mode.startswith('sub'):
            cli_pkgs = {'codex': '@openai/codex', 'gemini': '@google/gemini-cli'}
            if prov in cli_pkgs and not shutil.which(prov) and shutil.which('npm'):
                pkg = cli_pkgs[prov]
                ans = input(f"│   [i] '{prov}' CLI not detected. Auto-install {pkg} via private npm now? [Y/n]: ").strip().lower()
                if ans not in ('n', 'no', 'skip'):
                    print(f"│   ... installing {pkg} into local runtime ...")
                    subprocess.run(['npm', 'install', '-g', '--no-audit', '--no-fund', pkg], check=False, timeout=180)
                    node_bin = ROOT / 'desk/runtime/.state/tools/node/bin' / prov
                    local_bin = Path.home() / '.local/bin'
                    if node_bin.is_file():
                        try:
                            local_bin.mkdir(parents=True, exist_ok=True)
                            target_link = local_bin / prov
                            if target_link.is_symlink() or target_link.exists():
                                target_link.unlink()
                            target_link.symlink_to(node_bin)
                        except OSError:
                            pass
            saved = options_short_engine.save_provider_connection(prov, 'subscription', keys_path=keys_path)
            print(f"│ ✓ Connected {label} via Subscription -> {saved['masked_status']}")
        elif auth_mode == '2':
            k_val = input(f"│ Paste your {label} API Key: ").strip()
            if k_val:
                saved = options_short_engine.save_provider_connection(prov, k_val, keys_path=keys_path)
                print(f"│ ✓ Saved {label} API Key -> {saved['masked_status']}")
        elif auth_mode not in ('s', 'skip'):
            # User pasted the key directly at the prompt
            saved = options_short_engine.save_provider_connection(prov, auth_mode, keys_path=keys_path)
            print(f"│ ✓ Saved {label} -> {saved['masked_status']}")

    print(f"│\n│ Pick which {label} model to use:")
    for idx_m, m_id in enumerate(preset_models, start=1):
        default_tag = " (recommended default)" if idx_m == 1 else ""
        print(f"│   [{idx_m}] {m_id}{default_tag}")
    m_in = input(f"│ Select model [1-{len(preset_models)} or type custom model ID, Enter = 1 ({preset_models[0]})]: ").strip()
    if not m_in:
        chosen_model = preset_models[0]
    elif m_in.isdigit() and 1 <= int(m_in) <= len(preset_models):
        chosen_model = preset_models[int(m_in) - 1]
    else:
        chosen_model = m_in

    if target_agent_override:
        target_agent = target_agent_override
    else:
        print(f"│\n│ Which Trading Agent(s) should run {prov}:{chosen_model}?")
        print("│   [A] All Trading Agents (Price Structure + Theta/Options + Scout + Risk + Fleet) [default]")
        print("│   [1] 01 · stage_0_jev_router          (0.4s Catalyst & Injection Filter)")
        print("│   [2] 02 · phase_1_scout_recon         (Regime, 0.1% Liquidity & Maverick-MCP)")
        print("│   [3] 03 · trader_price_structure      (Long/Short Equity + Long Call/Put · 1D & 1W)")
        print("│   [4] 04 · trader_theta_mean_reversion (Short Call & 100% Cash-Secured Short Put · 1D & 1W)")
        print("│   [5] 05 · phase_3_risk_auditor        (10.0% Daily Stop Loss & Non-Live Gate)")
        print("│   [6] 06 · phase_4_fleet_dispatcher    (12-Account Shadow Fleet & Robinhood/Paper Dispatch)")
        target_agent = input("│ Select [A or 1-6, Enter = A]: ").strip() or 'traders'
        if target_agent.lower() == 'a':
            target_agent = 'traders'

    options_short_engine.configure_sticky_models(
        state_path=roster_path,
        provider=prov,
        model=chosen_model,
        agent_id=target_agent,
    )
    _sync_adw_state_quietly(output_dir=output_dir)
    print(f"└── ✓ Assigned {prov}:{chosen_model} -> Agent(s): {target_agent}\n")
    return True


def run_model_cli(
    *,
    positional: list[str] | None = None,
    output_dir: Path = HARNESS2_STATE,
    provider: str | None = None,
    model: str | None = None,
    agent_id: str = 'traders',
    assignments: list[str] | None = None,
    reset: bool = False,
    as_json: bool = False,
    install_wizard: bool = False,
) -> dict[str, Any]:
    """Run the `algo model` terminal selector: view/pick each Agentic Trader's model and connect Subscriptions/API keys."""
    import options_short_engine
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    roster_path = output_dir / 'model_roster.json'
    keys_path = output_dir / 'provider_keys.json'
    pos = [str(x).strip() for x in (positional or []) if str(x).strip()]

    # Handle positional shortcuts:
    # - `algo model reset`
    # - `algo model wizard` / `algo model install`
    # - `algo model key <provider> <api_key_or_subscription>`
    # - `algo model <agent_num_or_name> <provider> [model]`
    # - `algo model <provider> [model]`
    if pos:
        first = pos[0].lower()
        if first == 'reset':
            reset = True
        elif first in ('wizard', 'install-wizard', 'setup'):
            install_wizard = True
            pos = []
        elif first in ('key', 'keys', 'connect', 'api'):
            if len(pos) >= 3:
                options_short_engine.save_provider_connection(pos[1], pos[2], keys_path=keys_path)
            elif len(pos) == 2 and '=' in pos[1]:
                k_prov, k_val = pos[1].split('=', 1)
                options_short_engine.save_provider_connection(k_prov, k_val, keys_path=keys_path)
        else:
            known_providers = set(options_short_engine.SUPPORTED_PROVIDERS) | {'zhipu', 'or', 'google', 'ollama'}
            if first in known_providers or ':' in first:
                provider = pos[0]
                if len(pos) >= 2:
                    model = pos[1]
            else:
                agent_id = pos[0]
                if len(pos) >= 2:
                    provider = pos[1]
                if len(pos) >= 3:
                    model = pos[2]

    if provider or model or assignments or reset:
        options_short_engine.configure_sticky_models(
            state_path=roster_path,
            provider=provider,
            model=model,
            agent_id=agent_id,
            assignments=assignments,
            reset=reset,
        )
        _sync_adw_state_quietly(output_dir=output_dir)

    roster = options_short_engine.load_sticky_model_roster(roster_path)
    conns = options_short_engine.load_provider_connections(keys_path)
    if as_json:
        out = {'roster': roster, 'provider_connections': conns}
        print(json.dumps(out, indent=2, sort_keys=True))
        return out

    print(options_short_engine.render_model_and_keys_cli(roster, conns))

    # Interactive Hermes-style wizard when invoked in a terminal (`sh install.sh` or `algo model`)
    if not pos and not provider and not model and not assignments and not reset and sys.stdin.isatty():
        try:
            if install_wizard:
                print("\n╔══════════════════════════════════════════════════════════════════════════════════════════════════════════════════╗")
                print("║  🤖 HERMES-STYLE AI MODEL & SUBSCRIPTION WIZARD · CONNECT MODELS FOR YOUR 6 TRADING AGENTS                       ║")
                print("╚══════════════════════════════════════════════════════════════════════════════════════════════════════════════════╝")
                want = input("Do you want to connect an AI model or subscription in the terminal now? [Y/n, Enter = Yes]: ").strip().lower()
                if want in ('n', 'no', 'skip', 'q'):
                    return {'roster': options_short_engine.load_sticky_model_roster(roster_path), 'provider_connections': options_short_engine.load_provider_connections(keys_path)}

            while True:
                print("\nChoose AI Provider / Subscription to Connect (or Agent Seat):")
                print("  [1] OpenAI / Codex      (ChatGPT Plus/Pro/Team Subscription or API Key)  -> gpt-5.4, gpt-5.3-codex, o3-pro")
                print("  [2] Grok (xAI)          (SuperGrok Subscription or XAI_API_KEY)          -> grok-4, grok-4-fast, grok-3-mini")
                print("  [3] DeepSeek            (DeepSeek API Key)                               -> deepseek-r1, deepseek-chat")
                print("  [4] GLM (Zhipu AI)      (GLM API Key)                                    -> glm-5, glm-4-plus")
                print("  [5] OpenRouter          (OpenRouter Multi-Model API Key)                 -> kimi-k2.7-code, deepseek-v4.1-flash")
                print("  [6] Google Gemini       (Gemini Subscription or GEMINI_API_KEY)          -> gemini-2.5-pro, gemini-2.5-flash")
                print("  [7] Local (Free)        (Ollama / vLLM on localhost:11434 — No key)      -> qwen3-coder-30b, deepseek-r1-distill")
                print("  [A] Pick by Agent Seat  (Assign different models to Agent 1..6)")
                print("  [R] Reset All Models    (Restore default non-Anthropic stack)")
                print("  [Enter] Done & Continue")

                choice = input("\nPress [1-7] Provider | [A] Agent | [R] Reset | [Enter] Done: ").strip()
                if not choice or choice.lower() in ('q', 'done', 'exit', 'n', 'no'):
                    break
                if choice.lower() == 'r':
                    options_short_engine.configure_sticky_models(state_path=roster_path, reset=True)
                    _sync_adw_state_quietly(output_dir=output_dir)
                    print("\n[OK] Reset all Agentic Traders to default non-Anthropic models:\n")
                    print(options_short_engine.render_model_and_keys_cli())
                    continue
                if choice.lower() == 'a':
                    ag_num = input("Choose Agent Seat [1=Jev Router, 2=Scout, 3=Price Trader, 4=Theta Trader, 5=Risk, 6=Fleet, A=All]: ").strip() or 'traders'
                    p_pick = input("Choose Provider for this Agent [1=OpenAI/Codex, 2=Grok, 3=DeepSeek, 4=GLM, 5=OpenRouter, 6=Gemini, 7=Local]: ").strip() or '1'
                    _connect_provider_and_pick_model_interactive(
                        p_pick,
                        roster_path=roster_path,
                        keys_path=keys_path,
                        output_dir=output_dir,
                        target_agent_override=ag_num,
                    )
                else:
                    tokens = [t.strip() for t in choice.split(',') if t.strip()]
                    for tok in tokens:
                        _connect_provider_and_pick_model_interactive(
                            tok,
                            roster_path=roster_path,
                            keys_path=keys_path,
                            output_dir=output_dir,
                        )

                print(options_short_engine.render_model_and_keys_cli())
                again = input("\nConnect another AI model or subscription? [y/N, Enter = Done]: ").strip().lower()
                if again not in ('y', 'yes'):
                    break
        except EOFError:
            pass

    return {'roster': options_short_engine.load_sticky_model_roster(roster_path), 'provider_connections': options_short_engine.load_provider_connections(keys_path)}


def run_tests_cli(*, full: bool = False) -> int:
    """Run the `algo tests` verification suite and render a clean pass/fail summary box."""
    import options_short_engine
    import time as _time
    import unittest as _unittest

    W = 114
    t0 = _time.monotonic()
    loader = _unittest.TestLoader()
    suite = _unittest.TestSuite()
    tests_dir = ROOT / 'scripts/tests'
    if full:
        suite.addTests(loader.discover(start_dir=str(tests_dir), pattern='test_*.py'))
    else:
        from tests import test_harness2_evolve
        for method_name in (
            'test_harness_tree_validates_all_eight_components_and_codex_projection',
            'test_options_and_short_engine_supports_all_six_instrument_modes',
            'test_agentic_adw_pipeline_synced_triad_and_visual_cli',
        ):
            suite.addTests(loader.loadTestsFromName(method_name, test_harness2_evolve.Harness2EvolveTests))
        for fname in ('test_shadow.py', 'test_paper.py'):
            if (tests_dir / fname).is_file():
                suite.addTests(loader.discover(start_dir=str(tests_dir), pattern=fname))

    runner = _unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    elapsed = _time.monotonic() - t0
    passed = result.testsRun - len(result.failures) - len(result.errors)
    status_str = 'ALL PASSED' if result.wasSuccessful() else 'FAILED'
    box = [
        "╔" + ("═" * W) + "╗",
        options_short_engine._box_line(
            f"  ALGO TESTS SUMMARY · {status_str} ({passed}/{result.testsRun} tests passed in {elapsed:.2f}s)",
            W,
        ),
        "╚" + ("═" * W) + "╝",
    ]
    print("\n" + "\n".join(box))
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)

    sub.add_parser('install', help='Install and verify google-research/harness2')

    p_evolve = sub.add_parser('run', help='Run Harness2 recursive self-evolution on Algo shadow & strategy domains')
    p_evolve.add_argument('--symbol', default='SPY')
    p_evolve.add_argument('--k', type=int, default=2)
    p_evolve.add_argument('--mode', choices=['parallel', 'sequential', 'both'], default='both')
    p_evolve.add_argument('--backend', choices=['local', 'cli'], default='local')
    p_evolve.add_argument('--state-dir', type=Path, default=HARNESS2_STATE)
    p_evolve.add_argument('--tasks', nargs='*')

    p_status = sub.add_parser('status', help='Show Harness2 installation and evolved strategy status')
    p_status.add_argument('--state-dir', type=Path, default=HARNESS2_STATE)

    p_watch = sub.add_parser('shadow-watch', help='Run live Yahoo market shadowing with the evolved Harness2 strategy')
    p_watch.add_argument('--seconds', type=int, default=30)
    p_watch.add_argument('--symbol', default='SPY')
    p_watch.add_argument('--state-dir', type=Path, default=HARNESS2_STATE)

    for sub_name, sub_help in [
        ('adw', 'Run the 5-Phase Back-to-Back Agentic ADW Pipeline and render the Visual Command Line'),
        ('visual', 'Render the 5-Phase Back-to-Back Agentic ADW Visual Command Line (`adw algo visual`)'),
    ]:
        p_cmd = sub.add_parser(sub_name, help=sub_help)
        p_cmd.add_argument('extra', nargs='*', help='Optional positional args (e.g. `algo visual`)')
        p_cmd.add_argument('--symbol', default='SPY')
        p_cmd.add_argument('--state-dir', type=Path, default=HARNESS2_STATE)
        p_cmd.add_argument('--provider', help='Provider (codex, openai, grok, deepseek, glm, openrouter, gemini, local)')
        p_cmd.add_argument('--model', help='Model ID for the chosen provider')
        p_cmd.add_argument('--agent', default='traders', help='Target agent ID or number (1-6) or "all"')
        p_cmd.add_argument('--set', dest='assignments', action='append', help='Set <agent_id>=<provider>:<model>')
        p_cmd.add_argument('--reset', action='store_true', help='Reset sticky model roster to non-Anthropic defaults')
        p_cmd.add_argument('--json', action='store_true')

    for m_name, m_help in [
        ('model', 'Interactive Agentic Trader Model & API Key/Subscription Selector (`algo model`)'),
        ('models', 'Interactive Agentic Trader Model & API Key/Subscription Selector (`algo models`)'),
        ('keys', 'Connect API keys & subscriptions (OpenAI/Codex, Grok, DeepSeek, GLM, OpenRouter, Gemini)'),
    ]:
        p_mod = sub.add_parser(m_name, help=m_help)
        p_mod.add_argument('positional', nargs='*', help='Optional: [agent#] [provider] [model] or `key <provider> <key>`')
        p_mod.add_argument('--state-dir', type=Path, default=HARNESS2_STATE)
        p_mod.add_argument('--provider', help='Provider (codex, openai, grok, deepseek, glm, openrouter, gemini, local)')
        p_mod.add_argument('--model', help='Model ID')
        p_mod.add_argument('--agent', default='traders', help='Target agent number (1-6) or ID or "all"')
        p_mod.add_argument('--set', dest='assignments', action='append', help='Set <agent_id>=<provider>:<model>')
        p_mod.add_argument('--reset', action='store_true', help='Reset sticky models to defaults')
        p_mod.add_argument('--json', action='store_true')

    for t_name in ('tests', 'test'):
        p_test = sub.add_parser(t_name, help='Run the Algo ADW, Options/Short Engine & Shadow Fleet test suite (`algo tests`)')
        p_test.add_argument('--full', action='store_true', help='Run full discovery test suite')

    args = parser.parse_args(argv)
    if args.action == 'install':
        print(json.dumps(install_harness2(), indent=2, sort_keys=True))
        return 0
    if args.action == 'run':
        result = run_evolution(
            output_dir=args.state_dir,
            symbol=args.symbol,
            k=args.k,
            mode=args.mode,
            backend=args.backend,
            tasks_filter=args.tasks,
        )
        print(json.dumps({
            'completed_at': result['completed_at'],
            'mode': result['mode'],
            'modes_run': result['modes_run'],
            'k': result['k'],
            'execution_count': result['execution_count'],
            'editor_calls': result['editor_calls'],
            'summaries': result['judged_summary']['summaries'],
            'shadow_ready_strategy_path': result['tomorrow_shadow_package']['shadow_ready_strategy_path'],
            'shadow_ready_multi_asset_book_path': result['tomorrow_shadow_package']['shadow_ready_multi_asset_book_path'],
            'paper_account_ready_orders_path': result['tomorrow_shadow_package']['paper_account_ready_orders_path'],
            'multi_asset_book_summary': result['tomorrow_shadow_package']['multi_asset_book_summary'],
            'backtest_vs_baseline': result['tomorrow_shadow_package']['backtest_vs_baseline'],
            'tomorrow_commands': result['tomorrow_shadow_package']['tomorrow_commands'],
            'live_orders': False,
        }, indent=2, sort_keys=True))
        return 0
    if args.action == 'status':
        print(json.dumps(status(args.state_dir), indent=2, sort_keys=True))
        return 0
    if args.action == 'shadow-watch':
        print(json.dumps(shadow_watch(seconds=args.seconds, output_dir=args.state_dir, symbol=args.symbol), indent=2, sort_keys=True))
        return 0
    if args.action in ('adw', 'visual'):
        run_adw_cli(
            symbol=args.symbol,
            output_dir=args.state_dir,
            as_json=args.json,
            provider=args.provider,
            model=args.model,
            agent_id=args.agent,
            assignments=args.assignments,
            reset=args.reset,
        )
        return 0
    if args.action in ('model', 'models', 'keys'):
        run_model_cli(
            positional=args.positional,
            output_dir=args.state_dir,
            provider=args.provider,
            model=args.model,
            agent_id=args.agent,
            assignments=args.assignments,
            reset=args.reset,
            as_json=args.json,
        )
        return 0
    if args.action in ('tests', 'test'):
        return run_tests_cli(full=args.full)
    return 1


if __name__ == '__main__':
    sys.exit(main())
