"""Synthetic contract and accounting checks, not evidence of strategy edge."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from backtest.contracts import (Rejected, canonical, sealed, sha_bytes, strategy_spec)
from backtest.engine import simulate
from backtest.storage import load_dataset
from backtest.worker import Worker

APP = Path(__file__).resolve().parents[2]
PARAMETERS = {
    "momentum_long": {"lookback_bars": 2, "enter_return_bps": "500", "exit_return_bps": "0"},
    "mean_reversion_long": {"lookback_bars": 5, "enter_z": "-2", "exit_z": "0"},
    "breakout_long": {"entry_channel_bars": 2, "exit_channel_bars": 2},
    "post_earnings_drift_long": {"min_surprise_pct": "5", "entry_delay_bars": 1, "hold_bars": 2},
    "sma_cross_long": {"fast_window": 1, "slow_window": 2},
}


def spec(family="momentum_long", **overrides):
    value = {"schema_version": 2, "family": family, "symbol": "SPY", "parameters": deepcopy(PARAMETERS[family]),
             "position": {"quantity": "1"}, "rationale": "Synthetic mechanics test."}
    if family == "post_earnings_drift_long":
        value["data_requirements"] = ["ohlcv_pti_v1", "earnings_events_pti_v1"]
    value.update(overrides)
    return value


def bars(closes):
    day = datetime(2024, 1, 8, tzinfo=timezone.utc)
    rows = []
    for close in closes:
        close = Decimal(str(close))
        rows.append({"symbol": "SPY", "bar_open_at": day.strftime("%Y-%m-%dT14:30:00.000Z"),
                     "bar_close_at": day.strftime("%Y-%m-%dT21:00:00.000Z"),
                     "available_at": day.strftime("%Y-%m-%dT21:00:00.000Z"),
                     "open": str(close), "high": str(close + 1), "low": str(close - 1),
                     "close": str(close), "volume": "1000"})
        day += timedelta(days=1)
        while day.weekday() > 4:
            day += timedelta(days=1)
    return rows


def event(rows, actual="2", estimate="1"):
    return {"available_at": rows[1]["bar_close_at"].replace("21:00", "22:00"),
            "eps_actual": actual, "eps_estimate": estimate, "session": "amc"}


def run(value, rows, events=None, commission="0", slippage="0", cash="1000.00"):
    manifest = {"symbols": ["SPY"], "timeframe": value.get("timeframe", "1d"), "price_scale": 2, "cash_scale": 2}
    return simulate(strategy_spec(value), manifest, rows, cash,
                    {"commission_bps": commission, "slippage_bps_per_side": slippage}, events)


class StrategyFamilyTests(unittest.TestCase):
    def reject(self, value, code):
        with self.assertRaises(Rejected) as caught:
            strategy_spec(value)
        self.assertEqual(caught.exception.code, code)

    def test_legacy_sma_spec_and_simulation_are_byte_identical(self):
        request = json.loads((APP / "backtest/fixtures/request.json").read_text())
        value = strategy_spec(request["strategy"])
        manifest, rows = load_dataset(APP / "backtest/fixtures/registry.json", request["dataset_id"])
        output = {"strategy": value, "simulation": simulate(value, manifest, rows, request["initial_cash"], request["cost_model"])}
        self.assertEqual(sha_bytes(canonical(output)), "5721429d22ebb2a892aeebecec085cc5eda0578dc87c7d805f0efc03088d8272")

    def test_momentum_known_next_open_fills_and_net_loss(self):
        rows = bars([100, 100, 100, 110, 112, 90, 95, 100])
        orders, fills, _, metrics = run(spec(), rows)
        self.assertEqual([o["decision_at"] for o in orders[:2]], [rows[3]["bar_close_at"], rows[5]["bar_close_at"]])
        self.assertEqual([f["executed_at"] for f in fills], [rows[4]["bar_open_at"], rows[6]["bar_open_at"]])
        self.assertEqual([f["price"] for f in fills], ["112.00", "95.00"])
        self.assertEqual(metrics["realized_net_pnl"], "-17.00")

    def test_mean_reversion_uses_prior_population_deviation(self):
        rows = bars([98, 99, 100, 101, 102, 90, 101, 102])
        orders, fills, _, metrics = run(spec("mean_reversion_long"), rows)
        self.assertEqual(orders[0]["decision_at"], rows[5]["bar_close_at"])
        self.assertEqual([f["price"] for f in fills], ["101.00", "102.00"])
        self.assertEqual(metrics["realized_net_pnl"], "1.00")

    def test_zero_deviation_is_no_signal(self):
        _, fills, _, _ = run(spec("mean_reversion_long"), bars([100, 100, 100, 100, 100, 90, 100]))
        self.assertEqual(fills, [])

    def test_breakout_uses_prior_channels_and_volume(self):
        rows = bars([100, 101, 102, 110, 112, 90, 91])
        value = spec("breakout_long")
        value["parameters"]["min_volume_ratio"] = "2"
        self.assertEqual(run(value, rows)[1], [])
        rows[3]["volume"] = "2000"
        orders, fills, _, metrics = run(value, rows)
        self.assertEqual(orders[0]["decision_at"], rows[3]["bar_close_at"])
        self.assertEqual([f["executed_at"] for f in fills], [rows[4]["bar_open_at"], rows[6]["bar_open_at"]])
        self.assertEqual(metrics["realized_net_pnl"], "-21.00")

    def test_post_earnings_delay_and_hold(self):
        rows = bars([100, 100, 120, 121, 122, 125])
        orders, fills, _, metrics = run(spec("post_earnings_drift_long"), rows, [event(rows)])
        self.assertEqual(orders[0]["decision_at"], rows[2]["bar_close_at"])
        self.assertEqual([f["executed_at"] for f in fills], [rows[3]["bar_open_at"], rows[5]["bar_open_at"]])
        self.assertEqual(metrics["realized_net_pnl"], "4.00")

    def test_earnings_gap_cap_zero_estimate_and_no_reentry(self):
        rows = bars([100, 100, 120, 121, 122, 125, 126, 127])
        value = spec("post_earnings_drift_long")
        value["parameters"]["max_gap_bps"] = "1000"
        self.assertEqual(run(value, rows, [event(rows)])[1], [])
        value["parameters"]["max_gap_bps"] = None
        self.assertEqual(run(value, rows, [event(rows, estimate="0")])[1], [])
        self.assertEqual(len(run(value, rows, [event(rows)])[1]), 2)

    def test_future_earnings_values_cannot_change_past_orders(self):
        rows = bars([100, 100, 120, 121, 122, 125])
        future = {**event(rows), "available_at": rows[4]["bar_close_at"], "eps_actual": "99"}
        before = run(spec("post_earnings_drift_long"), rows, [event(rows), future])
        future["eps_actual"] = "-99"
        after = run(spec("post_earnings_drift_long"), rows, [event(rows), future])
        cutoff = future["available_at"]
        self.assertEqual([o for o in before[0] if o["decision_at"] < cutoff],
                         [o for o in after[0] if o["decision_at"] < cutoff])
        self.assertEqual([f for f in before[1] if f["executed_at"] < cutoff],
                         [f for f in after[1] if f["executed_at"] < cutoff])
        self.assertEqual(before[2][:5], after[2][:5])

    def test_bmo_event_is_not_visible_at_prior_close(self):
        rows = bars([100, 100, 120, 121, 122, 125])
        announcement = {**event(rows), "available_at": rows[2]["bar_open_at"].replace("14:30", "13:00"), "session": "bmo"}
        orders, fills, _, _ = run(spec("post_earnings_drift_long"), rows, [announcement])
        self.assertEqual(orders[0]["decision_at"], rows[2]["bar_close_at"])
        self.assertEqual(fills[0]["executed_at"], rows[3]["bar_open_at"])

    def test_v2_sma_excludes_current_bar(self):
        rows = bars([100, 110, 90, 120, 100])
        orders, fills, _, _ = run(spec("sma_cross_long"), rows)
        self.assertEqual(orders[0]["decision_at"], rows[2]["bar_close_at"])
        self.assertEqual(fills[0]["executed_at"], rows[3]["bar_open_at"])

    def test_fixed_notional_resizes_whole_shares_and_reconciles(self):
        rows = bars([100, 100, 100, 110, 112, 90, 95, 110, 110, 80, 80])
        rows[8].update(open="80", low="79")
        value = spec(position={"sizing": "fixed_notional", "notional_usd": "250"})
        _, fills, _, metrics = run(value, rows, commission="100")
        self.assertEqual([f["quantity"] for f in fills], ["2", "2", "3", "3"])
        balance = Decimal("1000")
        for fill in fills:
            self.assertLessEqual(Decimal(fill["notional"]), Decimal("250") if fill["side"] == "buy" else Decimal("1000"))
            balance += (-1 if fill["side"] == "buy" else 1) * Decimal(fill["notional"]) - Decimal(fill["commission"])
        self.assertEqual(Decimal(metrics["final_equity"]), balance)

    def test_notional_too_small_does_not_become_fractional_or_leveraged(self):
        value = spec(position={"sizing": "fixed_notional", "notional_usd": "25"})
        _, fills, _, metrics = run(value, bars([100, 100, 100, 110, 112, 120]))
        self.assertEqual(fills, [])
        self.assertGreater(metrics["rejected_orders"], 0)

    def test_limit_never_fills_beyond_price_bound(self):
        value = spec(execution={"order_type": "limit", "limit_offset_bps": "10"})
        _, fills, _, metrics = run(value, bars([100, 100, 100, 110, 112, 120]), slippage="100")
        self.assertEqual(fills, [])
        self.assertGreater(metrics["cancelled_pending_orders"], 0)

    def test_stop_loss_is_next_open_and_can_gap(self):
        rows = bars([100, 100, 100, 110, 112, 100, 80])
        value = spec(exits={"stop_loss_bps": "300"})
        value["parameters"]["exit_return_bps"] = "-5000"
        orders, fills, _, _ = run(value, rows)
        self.assertEqual(orders[1]["decision_at"], rows[5]["bar_close_at"])
        self.assertEqual(fills[1]["price"], "80.00")

    def test_take_profit_and_max_hold_each_exit_at_following_open(self):
        rows = bars([100, 100, 100, 110, 112, 120, 125])
        for exits in ({"take_profit_bps": "500"}, {"max_hold_bars": 2}):
            with self.subTest(exits=exits):
                _, fills, _, _ = run(spec(exits=exits), rows)
                self.assertEqual(fills[1]["executed_at"], rows[6]["bar_open_at"])

    def test_trend_filter_blocks_entry(self):
        value = spec("mean_reversion_long")
        value["parameters"]["trend_filter_sma"] = 5
        self.assertEqual(run(value, bars([98, 99, 100, 101, 102, 90, 101, 102]))[1], [])

    def test_future_bar_mutation_preserves_earlier_fills_and_decisions(self):
        rows = bars([100, 100, 100, 110, 112, 90, 95, 100])
        before = run(spec(), rows)
        rows[-1].update(open="200", close="200", high="201", low="199")
        after = run(spec(), rows)
        self.assertEqual(before[1], after[1])
        self.assertEqual(before[0][:2], after[0][:2])
        self.assertEqual(before[2][:-1], after[2][:-1])

    def test_future_available_at_rejects(self):
        rows = bars([100, 100, 100, 110, 112])
        rows[2]["available_at"] = rows[3]["bar_close_at"]
        with self.assertRaisesRegex(Rejected, "future_data"):
            run(spec(), rows)

    def test_session_is_new_york_and_dst_aware(self):
        rows = bars([100, 100, 100, 110, 112])
        rows[0]["bar_open_at"] = rows[0]["bar_open_at"].replace("14:30", "13:30")
        with self.assertRaisesRegex(Rejected, "extended_hours_not_allowed"):
            run(spec(), rows)

    def test_summer_session_and_intraday_timeframes(self):
        rows = bars([100, 100, 100, 110, 112])
        for i, row in enumerate(rows):
            day = datetime(2024, 7, 8, tzinfo=timezone.utc) + timedelta(days=i)
            row.update(bar_open_at=day.strftime("%Y-%m-%dT13:30:00.000Z"),
                       bar_close_at=day.strftime("%Y-%m-%dT20:00:00.000Z"),
                       available_at=day.strftime("%Y-%m-%dT20:00:00.000Z"))
        self.assertEqual(len(run(spec(), rows)[1]), 1)
        for timeframe, minutes in (("1m", 1), ("5m", 5), ("15m", 15), ("1h", 60)):
            with self.subTest(timeframe=timeframe):
                sample = bars([100, 100, 100, 110, 112])
                opened = datetime(2024, 1, 8, 14, 30, tzinfo=timezone.utc)
                for i, row in enumerate(sample):
                    start = opened + timedelta(minutes=i * minutes)
                    end = start + timedelta(minutes=minutes)
                    row.update(bar_open_at=start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                               bar_close_at=end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                               available_at=end.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
                self.assertEqual(run(spec(timeframe=timeframe), sample)[1][0]["executed_at"], sample[4]["bar_open_at"])

    def test_short_and_multileg_specs_reject(self):
        cases = [(spec(position={"quantity": "-1"}), "short_not_allowed"),
                 (spec(direction="short"), "short_not_allowed"),
                 (spec(position={"quantity": "1", "leverage": "2"}), "short_not_allowed"),
                 (spec(legs=[{"symbol": "SPY"}]), "instrument_out_of_scope"),
                 (spec(instrument="option"), "instrument_out_of_scope")]
        for value, code in cases:
            with self.subTest(code=code):
                self.reject(value, code)

    def test_parameter_cross_bounds_unknown_fields_and_unsafe_execution(self):
        cases = [(spec(parameters={**PARAMETERS["momentum_long"], "exit_return_bps": "501"}), "parameter_out_of_range"),
                 (spec(parameters={**PARAMETERS["momentum_long"], "lookback_bars": True}), "parameter_out_of_range"),
                 (spec(execution={"timing": "same_bar_close"}), "lookahead_fill"),
                 (spec(timeframe="1s"), "timeframe_too_fast"),
                 (spec(position={"quantity": "1", "max_positions": 2}), "max_positions_exceeded"),
                 (spec(exits={"stop_loss_bps": "9"}), "parameter_out_of_range"),
                 (spec(account="forbidden"), "unknown_field"),
                 ({**spec(), "family": "execute_python"}, "unknown_family")]
        for value, code in cases:
            with self.subTest(code=code):
                self.reject(value, code)

    def test_hash_seals_defaults_and_rationale(self):
        value = strategy_spec(spec())
        self.assertEqual(value, strategy_spec(value))
        self.assertEqual(value, strategy_spec(spec(parameters={**PARAMETERS["momentum_long"], "enter_return_bps": "500.0"})))
        value["rationale"] = "Changed."
        self.reject(value, "hash_mismatch")

    def test_missing_earnings_requirements_and_dataset_reject(self):
        value = spec("post_earnings_drift_long", data_requirements=["ohlcv_pti_v1"])
        self.reject(value, "missing_dataset")
        rows = bars([100, 100, 110, 112])
        with self.assertRaisesRegex(Rejected, "missing_dataset"):
            run(spec("post_earnings_drift_long"), rows)


class WorkerFamilyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = {"schema_version": 1, "kind": "dataset_registry", "datasets": {}}
        self.worker = Worker(self.root / "registry.json", self.root / "artifacts")

    def register(self, name, rows, earnings=False):
        data = b"".join(canonical(row) + b"\n" for row in rows)
        manifest = {"schema_version": 1, "kind": "dataset_manifest", "dataset_id": name,
                    "classification": "synthetic", "partition": "development", "format": "jsonl",
                    "row_schema": "earnings_events_pti_v1" if earnings else "ohlcv_pti_v1", "symbols": ["SPY"],
                    "source": {"provider": "fixture", "source_reference": "task-2a", "license_reference": "CC0",
                               "retrieved_at": "2024-02-01T00:00:00.000Z", "snapshot_sha256": sha_bytes(data)},
                    "row_count": len(rows), "data_sha256": sha_bytes(data)}
        if earnings:
            manifest["coverage"] = {"first_available_at": rows[0]["available_at"], "last_available_at": rows[-1]["available_at"]}
        else:
            manifest.update(timeframe="1d", calendar_id="nyse_regular", price_basis="raw", currency="USD",
                            price_scale=2, cash_scale=2, quantity_scale=0,
                            coverage={"first_bar_open": rows[0]["bar_open_at"], "last_bar_close": rows[-1]["bar_close_at"]})
        manifest = sealed(manifest, "manifest_sha256")
        (self.root / (name + ".jsonl")).write_bytes(data)
        (self.root / (name + ".json")).write_bytes(canonical(manifest))
        self.registry["datasets"][name] = {"classification": "synthetic", "partition": "development",
            "manifest": name + ".json", "data": name + ".jsonl", "manifest_sha256": manifest["manifest_sha256"]}
        (self.root / "registry.json").write_bytes(canonical(self.registry))
        return manifest

    def request(self, value, rows):
        return {"op": "run_backtest", "strategy": value, "dataset_id": "bars",
                "evaluation_window": {"start": rows[0]["bar_open_at"], "end_exclusive": rows[-1]["bar_close_at"][:10] + "T23:00:00.000Z"},
                "initial_cash": "1000.00", "cost_model": {}}

    def test_every_family_runs_through_registered_worker(self):
        rows = bars([98, 99, 100, 101, 102, 90, 110, 120, 80, 90])
        self.register("bars", rows)
        earnings_manifest = self.register("earnings", [event(rows)], earnings=True)
        for dataset_id in ("bars", "earnings"):
            read = self.worker.handle_raw(canonical({"op": "read_manifest", "dataset_id": dataset_id}))
            self.assertTrue(read["ok"], read)
        for family in PARAMETERS:
            with self.subTest(family=family):
                request = self.request(spec(family), rows)
                if family == "post_earnings_drift_long":
                    request["earnings_dataset_id"] = "earnings"
                response = self.worker.handle_raw(canonical(request))
                self.assertTrue(response["ok"], response)
                self.assertIs(response["result"]["promoting"], False)
                if family == "post_earnings_drift_long":
                    self.assertEqual(response["request"]["earnings_manifest_sha256"], earnings_manifest["manifest_sha256"])

    def test_future_availability_rejects_at_worker_boundary(self):
        rows = bars([100, 100, 100, 110, 112])
        rows[2]["available_at"] = rows[3]["bar_close_at"]
        self.register("bars", rows)
        response = self.worker.handle_raw(canonical(self.request(spec(), rows)))
        self.assertEqual(response["error"]["code"], "future_data")
        self.assertIsNone(response["result"]["metrics"])

    def test_earnings_holdout_denied_before_open_and_missing_data_rejected(self):
        rows = bars([100, 100, 120, 121, 122, 125])
        self.register("bars", rows)
        request = self.request(spec("post_earnings_drift_long"), rows)
        response = self.worker.handle_raw(canonical(request))
        self.assertEqual(response["error"]["code"], "missing_dataset")
        self.register("earnings", [event(rows)], earnings=True)
        self.registry["datasets"]["earnings"].update(partition="sealed", data="absent.jsonl")
        (self.root / "registry.json").write_bytes(canonical(self.registry))
        request["earnings_dataset_id"] = "earnings"
        response = self.worker.handle_raw(canonical(request))
        self.assertEqual(response["error"]["code"], "dataset_access_denied")


if __name__ == "__main__":
    unittest.main()
