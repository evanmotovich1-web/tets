from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN
from datetime import datetime, timedelta
import random
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backtest.contracts import (MAX_MESSAGE, Rejected, canonical, sealed, sha, sha_bytes,
                                strategy_spec, verify)
from backtest.storage import ArtifactStore, eligible, load_dataset
from backtest.worker import DEFAULT_REGISTRY, Worker, read_receipt

APP = Path(__file__).resolve().parents[2]
FIXTURES = DEFAULT_REGISTRY.parent


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = self.root / "fixture"
        shutil.copytree(FIXTURES, self.fixture)
        self.worker = Worker(self.fixture / "registry.json", self.root / "artifacts")
        self.request = json.loads((FIXTURES / "request.json").read_text())

    def call(self, message=None):
        return self.worker.handle_raw(canonical(self.request if message is None else message))

    def run_ok(self):
        response = self.call()
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["status"], "completed")
        self.assertEqual(response["result"]["qualification"], "research_only")
        self.assertIs(response["result"]["promoting"], False)
        return response

    def rows(self):
        return [json.loads(line) for line in (self.fixture / "sma-four-bars.jsonl").read_text().splitlines()]

    def rewrite_dataset(self, rows=None, manifest_change=None):
        manifest = json.loads((self.fixture / "manifest.json").read_text())
        manifest.pop("manifest_sha256")
        if rows is not None:
            data = b"".join(canonical(row) + b"\n" for row in rows)
            (self.fixture / "sma-four-bars.jsonl").write_bytes(data)
            manifest.update(data_sha256=sha_bytes(data), row_count=len(rows))
            manifest["source"]["snapshot_sha256"] = sha_bytes(data)
            manifest["coverage"] = {"first_bar_open": rows[0]["bar_open_at"], "last_bar_close": rows[-1]["bar_close_at"]}
        if manifest_change:
            manifest_change(manifest)
        manifest = sealed(manifest, "manifest_sha256")
        (self.fixture / "manifest.json").write_bytes(canonical(manifest))
        registry = json.loads((self.fixture / "registry.json").read_text())
        registry["datasets"][self.request["dataset_id"]]["manifest_sha256"] = manifest["manifest_sha256"]
        (self.fixture / "registry.json").write_bytes(canonical(registry))

    def artifact(self, response, kind):
        return self.worker.store.get(response["result"]["artifacts"][kind + "_sha256"])["rows"]

    def rejected(self, code, message=None):
        response = self.call(message)
        self.assertFalse(response["ok"], response)
        self.assertIn(code, response["result"]["reason_codes"])
        self.assertIsNone(response["result"]["metrics"])
        return response

    def test_accounting_exact_fixture(self):
        response = self.run_ok()
        metrics = response["result"]["metrics"]
        for key, expected in {"final_equity": "1005.40", "final_cash": "1005.40", "commission_total": "2.30",
                              "slippage_cost_total": "2.30", "realized_net_pnl": "5.40", "total_return": "0.0054",
                              "max_drawdown": "0.02221", "completed_trades": 1, "open_positions": 0}.items():
            self.assertEqual(metrics[key], expected)
        fills = self.artifact(response, "fills")
        self.assertEqual(len(fills), 2)
        self.assertEqual([f["price"] for f in fills], ["111.10", "118.80"])
        self.assertEqual([f["commission"] for f in fills], ["1.11", "1.19"])
        self.assertEqual(fills[0]["executed_at"], "2024-01-10T09:30:00.000Z")
        self.assertEqual(self.artifact(response, "equity")[2]["cash"], "887.79")
        self.assertEqual(self.artifact(response, "equity")[2]["equity"], "977.79")

    def test_half_even_commission_and_whole_quantity(self):
        rows = self.rows()
        rows[2]["open"] = "100.50"
        self.rewrite_dataset(rows)
        self.request["cost_model"]["slippage_bps_per_side"] = "0"
        first = self.run_ok()
        self.assertEqual(self.artifact(first, "fills")[0]["commission"], "1.00")
        self.assertEqual(first["result"]["metrics"]["final_equity"], "1017.30")
        self.request["strategy"]["position"]["quantity"] = "3"
        second = self.run_ok()
        self.assertEqual(self.artifact(second, "fills")[0]["commission"], "3.02")
        self.assertEqual(second["result"]["metrics"]["final_equity"], "1051.88")

    def test_half_even_execution_price(self):
        rows = self.rows()
        rows[2]["open"] = "100.50"
        self.rewrite_dataset(rows)
        response = self.run_ok()
        self.assertEqual(self.artifact(response, "fills")[0]["price"], "101.50")

    def test_zero_cost(self):
        self.request["cost_model"] = {}
        self.assertEqual(self.run_ok()["result"]["metrics"]["final_equity"], "1010.00")

    def test_insufficient_cash_no_resizing(self):
        self.request["initial_cash"] = "112.20"
        response = self.run_ok()
        self.assertEqual(self.artifact(response, "fills"), [])
        self.assertEqual(response["result"]["metrics"]["final_equity"], "112.20")
        self.assertEqual(response["result"]["metrics"]["rejected_orders"], 1)

    def test_exact_cash_boundary(self):
        self.request["initial_cash"] = "112.21"
        self.assertEqual(len(self.artifact(self.run_ok(), "fills")), 2)

    def test_terminal_buy_cancelled(self):
        self.request["evaluation_window"]["end_exclusive"] = "2024-01-10T00:00:00.000Z"
        response = self.run_ok()
        self.assertEqual(self.artifact(response, "fills"), [])
        self.assertEqual(response["result"]["metrics"]["cancelled_pending_orders"], 1)

    def test_open_position_not_liquidated(self):
        self.request["evaluation_window"]["end_exclusive"] = "2024-01-11T00:00:00.000Z"
        metrics = self.run_ok()["result"]["metrics"]
        for key, expected in {"final_cash": "887.79", "final_equity": "977.79", "unrealized_pnl": "-21.10",
                              "realized_net_pnl": "0.00", "commission_total": "1.11", "open_positions": 1,
                              "completed_trades": 0, "cancelled_pending_orders": 1}.items():
            self.assertEqual(metrics[key], expected)

    def test_no_trade_is_completed_research(self):
        rows = self.rows()
        for row in rows:
            for key in ["open", "high", "low", "close"]:
                row[key] = "100.00"
        self.rewrite_dataset(rows)
        response = self.run_ok()
        self.assertEqual(self.artifact(response, "fills"), [])
        self.assertEqual(response["result"]["metrics"]["total_return"], "0")

    def test_future_mutation_preserves_past_decisions_and_fills(self):
        before = self.run_ok()
        rows = self.rows()
        rows[-1].update(open="200.00", high="210.00", low="190.00", close="205.00")
        self.rewrite_dataset(rows)
        after = self.run_ok()
        self.assertNotEqual(before["request"]["request_sha256"], after["request"]["request_sha256"])
        self.assertEqual(self.artifact(before, "fills")[0], self.artifact(after, "fills")[0])
        self.assertEqual(self.artifact(before, "orders")[:2], self.artifact(after, "orders")[:2])
        self.assertEqual(self.artifact(before, "equity")[:3], self.artifact(after, "equity")[:3])

    def test_delayed_data_denied(self):
        rows = self.rows()
        rows[1]["available_at"] = "2024-01-10T16:00:00.000Z"
        self.assertFalse(eligible(rows[1], "2024-01-09T16:00:00.000Z"))
        self.rewrite_dataset(rows)
        self.rejected("delayed_or_revised_bar")

    def test_reproducible_hashes_defaults_and_order(self):
        before = self.run_ok()
        self.request = dict(reversed(list(self.request.items())))
        self.request["strategy"] = strategy_spec(self.request["strategy"])
        self.request["cost_model"]["rounding"] = "half_even"
        self.request["initial_cash"] = "1000"
        self.request["cost_model"]["commission_bps"] = "100.00"
        after = self.run_ok()
        self.assertEqual(before["request"], after["request"])
        self.assertEqual(before["result"], after["result"])
        self.assertNotEqual(before["events"][0]["attempt_id"], after["events"][0]["attempt_id"])

    def test_changed_cost_changes_request(self):
        before = self.run_ok()
        self.request["cost_model"]["commission_bps"] = "99"
        self.assertNotEqual(before["request"]["request_sha256"], self.run_ok()["request"]["request_sha256"])

    def test_adapter_version_bound(self):
        before = self.run_ok()
        with patch("backtest.worker.ADAPTER_VERSION", "test-version"):
            after = self.run_ok()
        self.assertNotEqual(before["request"]["request_sha256"], after["request"]["request_sha256"])

    def test_sealed_registry_denied_before_read(self):
        registry = json.loads((self.fixture / "registry.json").read_text())
        entry = registry["datasets"][self.request["dataset_id"]]
        entry.update(partition="sealed", data="missing-secret.jsonl")
        (self.fixture / "registry.json").write_bytes(canonical(registry))
        self.rejected("dataset_access_denied")
        self.request["partition"] = "development"
        self.rejected("unknown_field")

    def test_registry_cannot_escape_directory(self):
        registry = json.loads((self.fixture / "registry.json").read_text())
        registry["datasets"][self.request["dataset_id"]]["data"] = "../secret.jsonl"
        (self.fixture / "registry.json").write_bytes(canonical(registry))
        self.rejected("registry_path_escape")

    def test_dataset_symlink_escape_denied(self):
        path = self.fixture / "sma-four-bars.jsonl"
        outside = self.root / "outside.jsonl"
        path.rename(outside)
        path.symlink_to(outside)
        self.rejected("registry_path_escape")

    def test_missing_dataset_path(self):
        (self.fixture / "sma-four-bars.jsonl").unlink()
        self.rejected("missing_path")

    def test_missing_registry_path(self):
        (self.fixture / "registry.json").unlink()
        self.rejected("missing_path")

    def test_missing_receipt(self):
        self.rejected("missing_path", {"op": "read_receipt", "receipt_sha256": "a" * 64})

    def test_tampered_data(self):
        path = self.fixture / "sma-four-bars.jsonl"
        path.write_bytes(path.read_bytes().replace(b"100.00", b"101.00", 1))
        self.rejected("hash_mismatch")

    def test_missing_bars_not_filled(self):
        rows = self.rows()
        del rows[1]
        self.rewrite_dataset(rows)
        self.rejected("noncontiguous_or_duplicate_bars")

    def test_duplicate_bars(self):
        rows = self.rows()
        rows[1] = deepcopy(rows[0])
        self.rewrite_dataset(rows)
        self.rejected("noncontiguous_or_duplicate_bars")

    def test_invalid_ohlc(self):
        rows = self.rows()
        rows[1]["low"] = "200.00"
        self.rewrite_dataset(rows)
        self.rejected("invalid_ohlc")

    def test_unsupported_calendar(self):
        self.rewrite_dataset(manifest_change=lambda m: m.update(calendar_id="NYSE"))
        self.rejected("unsupported_value")

    def test_window_bounds_and_warmup(self):
        original = deepcopy(self.request)
        for field, value, code in [("start", "2023-12-01T09:30:00.000Z", "window_outside_dataset"),
                                   ("end_exclusive", "2024-01-09T00:00:00.000Z", "insufficient_warmup"),
                                   ("end_exclusive", "2024-01-10T12:00:00.000Z", "partial_bar_window")]:
            with self.subTest(code=code):
                self.request = deepcopy(original)
                self.request["evaluation_window"][field] = value
                self.rejected(code)

    def test_fractional_quantity_not_proportional(self):
        self.request["strategy"]["position"]["quantity"] = "0.5"
        self.rejected("fractional_quantity_unsupported")

    def test_invalid_strategy_fields(self):
        original = deepcopy(self.request)
        mutations = [lambda s: s.update(code="print('execute')"),
                     lambda s: s.update(family="arbitrary_python"),
                     lambda s: s["parameters"].update(slow_window=1),
                     lambda s: s["position"].update(max_positions=2),
                     lambda s: s["parameters"].update(fast_window=True),
                     lambda s: s["position"].update(quantity="-1")]
        for mutation in mutations:
            self.request = deepcopy(original)
            mutation(self.request["strategy"])
            response = self.call()
            self.assertFalse(response["ok"])
            self.assertIsNone(response["result"]["metrics"])

    def test_payload_bounds_duplicates_nonfinite(self):
        for raw, code in [(b'{"op":"x","op":"y"}', "duplicate_json_key"),
                          (b'{"op":NaN}', "nonfinite_json"), (b'{', "malformed_json"),
                          (b'x' * (MAX_MESSAGE + 1), "message_too_large")]:
            with self.subTest(code=code):
                response = self.worker.handle_raw(raw)
                self.assertEqual(response["error"]["code"], code)
                self.assertIsNone(response["result"]["metrics"])

    def test_invalid_cost_cash_and_nested_objects(self):
        original = deepcopy(self.request)
        mutations = [lambda r: r.update(initial_cash="0"),
                     lambda r: r.update(initial_cash=1000),
                     lambda r: r.update(initial_cash="1000.001"),
                     lambda r: r["cost_model"].update(commission_bps="-1"),
                     lambda r: r["cost_model"].update(slippage_bps_per_side="1001"),
                     lambda r: r["cost_model"].update(rounding="up"),
                     lambda r: r["cost_model"].update(spread="1"),
                     lambda r: r["evaluation_window"].update(extra="ignored?"),
                     lambda r: r["strategy"].update(signal={"field": "close", "enter": "fast_gt_slow", "exit": "fast_lte_slow", "code": "pass"})]
        for mutation in mutations:
            self.request = deepcopy(original)
            mutation(self.request)
            response = self.call()
            self.assertFalse(response["ok"], response)
            self.assertEqual(response["result"]["status"], "rejected")
            self.assertIsNone(response["result"]["metrics"])

    def test_unknown_dataset_and_client_path_denied(self):
        self.request["dataset_id"] = "not-registered"
        self.rejected("unknown_dataset")
        self.request["dataset_id"] = "../../secret"
        self.rejected("invalid_id")

    def test_manifest_hash_and_identity_mismatch(self):
        manifest = json.loads((self.fixture / "manifest.json").read_text())
        manifest["currency"] = "EUR"
        (self.fixture / "manifest.json").write_bytes(canonical(manifest))
        self.rejected("hash_mismatch")

    def test_unknown_operation_and_properties(self):
        self.rejected("unsupported_operation", {"op": "execute_python", "code": "pass"})
        self.rejected("unknown_field", {"op": "read_manifest", "dataset_id": self.request["dataset_id"], "path": "/tmp/private"})

    def test_trial_evidence_includes_invalid_and_duplicate_proposals(self):
        good = self.call({"op": "validate_strategy", "strategy": self.request["strategy"]})
        duplicate = self.call({"op": "validate_strategy", "strategy": self.request["strategy"]})
        bad = self.call({"op": "validate_strategy", "strategy": {"code": "not executable"}})
        self.assertTrue(good["ok"] and duplicate["ok"])
        self.assertFalse(bad["ok"])
        files = list((self.root / "artifacts" / "attempts").glob("*.json"))
        events = [json.loads(p.read_text()) for p in files]
        self.assertEqual(sum(e["event_type"] == "proposal_recorded" for e in events), 3)
        self.assertEqual(len({e["attempt_id"] for e in events}), 3)

    def test_receipt_round_trip_and_missing_artifact(self):
        response = self.run_ok()
        receipt = response["result"]
        retrieved = self.call({"op": "read_receipt", "receipt_sha256": receipt["receipt_sha256"]})
        self.assertEqual(retrieved["result"], receipt)
        (self.worker.store.root / (receipt["artifacts"]["fills_sha256"] + ".json")).unlink()
        self.rejected("missing_path", {"op": "read_receipt", "receipt_sha256": receipt["receipt_sha256"]})

    def test_content_address_collision_is_integrity_failure(self):
        response = self.run_ok()
        receipt = response["result"]
        path = self.worker.store.root / (receipt["receipt_sha256"] + ".json")
        path.write_bytes(b"{}")
        with self.assertRaisesRegex(Rejected, "artifact_integrity_failure"):
            self.worker.store.put(receipt, "receipt_sha256")

    def test_engine_error_has_no_metrics(self):
        with patch("backtest.engine.simulate", side_effect=RuntimeError("not completed")):
            response = self.call()
        self.assertFalse(response["ok"])
        self.assertEqual(response["result"]["status"], "failed")
        self.assertIsNone(response["result"]["metrics"])
        self.assertIsNotNone(response["result"]["request_sha256"])

    def test_manifest_operation_verifies_bytes(self):
        response = self.call({"op": "read_manifest", "dataset_id": self.request["dataset_id"]})
        self.assertTrue(response["ok"])
        verify(response["manifest"], "manifest_sha256")

    def test_foreground_worker_protocol(self):
        environment = {"PATH": os.environ.get("PATH", ""), "TRADING_DESK_REGISTRY": str(self.fixture / "registry.json"),
                       "TRADING_DESK_ARTIFACT_DIR": str(self.root / "child-artifacts")}
        child = subprocess.run([sys.executable, "-m", "backtest.worker"], input=canonical(self.request),
                               cwd=APP, env=environment, capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(len(child.stdout.splitlines()), 1)
        response = json.loads(child.stdout)
        self.assertEqual(response["result"]["metrics"]["final_equity"], "1005.40")

    def test_exported_schemas_are_current(self):
        from backtest.schemas import exported
        for name, schema in exported().items():
            path = APP / "backtest" / "contracts" / (name + ".schema.json")
            self.assertEqual(json.loads(path.read_text()), schema)
            self.assertIs(schema["additionalProperties"], False)

    def test_replay_reuses_artifacts_and_ignores_staging(self):
        before = self.run_ok()
        original_paths = sorted(p.name for p in self.worker.store.root.glob("*.json"))
        (self.worker.store.root / ".staging-interrupted").write_bytes(b"partial")
        self.worker = Worker(self.fixture / "registry.json", self.root / "artifacts")
        after = self.run_ok()
        self.assertEqual(before["result"], after["result"])
        self.assertEqual(original_paths, sorted(p.name for p in self.worker.store.root.glob("*.json")))

    def test_same_receipt_in_different_artifact_directory(self):
        before = self.run_ok()
        self.worker = Worker(self.fixture / "registry.json", self.root / "other-artifacts")
        self.assertEqual(before["result"], self.run_ok()["result"])

    def test_dependency_drift_fails_closed(self):
        with patch("backtest.worker.metadata.version", return_value="wrong-version"):
            self.rejected("dependency_version_mismatch")

    def test_seeded_multi_trade_ledger_reconciliation(self):
        total_fills = 0
        for seed in range(12):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                rows = []
                day = datetime(2024, 1, 8)
                for _ in range(24):
                    opening = Decimal(rng.randrange(9000, 12001)) / 100
                    closing = Decimal(rng.randrange(9000, 12001)) / 100
                    rows.append({"symbol": "SYNTH", "bar_open_at": day.strftime("%Y-%m-%dT09:30:00.000Z"),
                                 "bar_close_at": day.strftime("%Y-%m-%dT16:00:00.000Z"),
                                 "available_at": day.strftime("%Y-%m-%dT16:00:00.000Z"),
                                 "open": str(opening), "close": str(closing), "high": "125.00", "low": "85.00", "volume": "1000"})
                    day += timedelta(days=1)
                    while day.weekday() > 4:
                        day += timedelta(days=1)
                self.rewrite_dataset(rows)
                self.request["evaluation_window"]["end_exclusive"] = (datetime.fromisoformat(rows[-1]["bar_close_at"].replace("Z", "+00:00")) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z")
                response = self.run_ok()
                fills = self.artifact(response, "fills")
                total_fills += len(fills)
                cash, held, commissions = Decimal("1000.00"), 0, Decimal(0)
                by_time = {r["bar_open_at"]: r for r in rows}
                for fill in fills:
                    raw = Decimal(by_time[fill["executed_at"]]["open"])
                    multiplier = Decimal("1.01") if fill["side"] == "buy" else Decimal("0.99")
                    price = (raw * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
                    fee = (price / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
                    self.assertEqual(Decimal(fill["price"]), price)
                    self.assertEqual(Decimal(fill["commission"]), fee)
                    if fill["side"] == "buy":
                        self.assertEqual(held, 0)
                        cash -= price + fee
                        held = 1
                    else:
                        self.assertEqual(held, 1)
                        cash += price - fee
                        held = 0
                    commissions += fee
                metrics = response["result"]["metrics"]
                self.assertEqual(Decimal(metrics["final_cash"]), cash)
                self.assertEqual(Decimal(metrics["commission_total"]), commissions)
                self.assertEqual(Decimal(metrics["final_equity"]), cash + held * Decimal(rows[-1]["close"]))
        self.assertGreater(total_fills, 60)

    def test_missing_artifact_configuration_fails_closed(self):
        child = subprocess.run([sys.executable, "-m", "backtest.worker"], input=b'{}', cwd=APP,
                               env={"PATH": os.environ.get("PATH", "")}, capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 1)
        self.assertEqual(json.loads(child.stdout)["error"]["code"], "artifact_directory_required")


if __name__ == "__main__":
    unittest.main()
