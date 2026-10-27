"""One bounded JSON request on stdin, one JSON response on stdout.

Host environment: TRADING_DESK_REGISTRY and TRADING_DESK_ARTIFACT_DIR.
No registry/artifact paths are accepted from tool arguments. See PROTOCOL.md.
"""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from importlib import metadata
from pathlib import Path
import os
import sys
import uuid

from . import ADAPTER_VERSION
from .contracts import (MAX_MESSAGE, Rejected, canonical, costs, dec, digest, identifier,
                        loads, norm, obj, require, sealed, sha, sha_bytes, stamp,
                        strategy_spec, window, minimum_bars)
from .storage import ArtifactStore, bounded_read, load_dataset, load_earnings_dataset

PACKAGE = Path(__file__).resolve().parent
DEFAULT_REGISTRY = PACKAGE / "fixtures" / "registry.json"


def provenance():
    lock = bounded_read(PACKAGE.parent / "requirements.lock", 65536)
    for line in lock.decode().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, version = line.split("==")
        require(metadata.version(name) == version, "dependency_version_mismatch")
    import backtesting
    sources = {"adapter/" + str(p.relative_to(PACKAGE)): sha_bytes(p.read_bytes())
               for p in sorted(PACKAGE.glob("*.py"))}
    sources.update({"adapter/contracts/" + p.name: sha_bytes(p.read_bytes())
                    for p in sorted((PACKAGE / "contracts").glob("*.json"))})
    upstream = Path(backtesting.__file__).parent
    sources.update({"engine/" + str(p.relative_to(upstream)): sha_bytes(p.read_bytes())
                    for p in sorted(upstream.rglob("*.py")) if "test" not in p.parts})
    sources["python_version"] = sys.version.split()[0]
    return {"name": "backtesting.py", "version": metadata.version("backtesting"), "adapter_version": ADAPTER_VERSION,
            "dependency_lock_sha256": sha_bytes(lock), "source_tree_sha256": sha(sources)}


def result(status, reasons, request_sha256=None, artifacts=None, metrics=None):
    require(status == "completed" or metrics is None, "failed_metrics_forbidden")
    return sealed({"schema_version": 1, "kind": "backtest_result", "request_sha256": request_sha256,
                   "status": status, "qualification": "research_only", "promoting": False,
                   "reason_codes": sorted(set(reasons)),
                   "artifacts": artifacts or {"orders_sha256": None, "fills_sha256": None, "equity_sha256": None},
                   "metrics": metrics}, "receipt_sha256")


def read_receipt(store, key):
    receipt = store.get(key, "receipt_sha256")
    obj(receipt, ["schema_version", "kind", "request_sha256", "status", "qualification", "promoting", "reason_codes", "artifacts", "metrics", "receipt_sha256"])
    require(receipt["kind"] == "backtest_result" and receipt["schema_version"] == 1 and
            receipt["qualification"] == "research_only" and receipt["promoting"] is False, "invalid_receipt")
    require(receipt["status"] in ("completed", "rejected", "failed", "unknown"), "invalid_receipt")
    if receipt["request_sha256"]:
        request = store.get(receipt["request_sha256"], "request_sha256")
        store.get(request["strategy_sha256"], "strategy_sha256")
        store.get(request["manifest_sha256"], "manifest_sha256")
        if "earnings_manifest_sha256" in request:
            store.get(request["earnings_manifest_sha256"], "manifest_sha256")
    if receipt["status"] == "completed":
        require(receipt["request_sha256"] and type(receipt["metrics"]) is dict, "invalid_receipt")
        for field, kind in [("orders_sha256", "orders"), ("fills_sha256", "fills"), ("equity_sha256", "equity")]:
            artifact = store.get(receipt["artifacts"][field])
            require(artifact["kind"] == kind and artifact["request_sha256"] == receipt["request_sha256"], "artifact_integrity_failure")
    else:
        require(receipt["metrics"] is None, "failed_metrics_forbidden")
    return receipt


class Worker:
    def __init__(self, registry_path, artifact_dir):
        self.registry_path = Path(registry_path)
        self.store = ArtifactStore(artifact_dir)
        self.attempts = ArtifactStore(Path(artifact_dir) / "attempts")

    def record(self, attempt_id, event_type, input_sha256, payload):
        # Execution evidence only, not the authoritative Pi Durable trial ledger.
        event = {"schema_version": 1, "kind": "worker_attempt_event", "attempt_id": attempt_id,
                 "event_type": event_type, "input_sha256": input_sha256,
                 "occurred_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 "payload": payload}
        self.attempts.put(event)
        return event

    def handle_raw(self, raw):
        attempt = uuid.uuid4().hex
        input_hash = sha_bytes(raw)
        events = [self.record(attempt, "attempt_started", input_hash, {})]
        try:
            require(len(raw) <= MAX_MESSAGE, "message_too_large")
            message = loads(raw)
            if isinstance(message, dict) and message.get("op") in ("validate_strategy", "run_backtest"):
                events.append(self.record(attempt, "proposal_recorded", input_hash, {"proposal_sha256": sha(message.get("strategy"))}))
            response = self.dispatch(message)
        except Rejected as error:
            receipt = result("rejected", [error.code])
            self.store.put(receipt, "receipt_sha256")
            response = {"ok": False, "error": {"code": error.code}, "result": receipt}
        except Exception as error:
            print(f"worker failure: {type(error).__name__}", file=sys.stderr)
            receipt = result("failed", ["worker_failure"])
            self.store.put(receipt, "receipt_sha256")
            response = {"ok": False, "error": {"code": "worker_failure"}, "result": receipt}
        payload = {"ok": response["ok"]}
        if "result" in response:
            payload["receipt_sha256"] = response["result"]["receipt_sha256"]
        terminal_event = "attempt_completed" if response["ok"] else (
            "attempt_failed" if response.get("result", {}).get("status") == "failed" else "attempt_rejected")
        events.append(self.record(attempt, terminal_event, input_hash, payload))
        return {"schema_version": 1, "kind": "worker_response", **response, "events": events}

    def dispatch(self, message):
        require(type(message) is dict and type(message.get("op")) is str, "missing_operation")
        op = message["op"]
        if op == "validate_strategy":
            obj(message, ["op", "strategy"])
            spec = strategy_spec(message["strategy"])
            self.store.put(spec, "strategy_sha256")
            return {"ok": True, "strategy": spec}
        if op == "read_manifest":
            obj(message, ["op", "dataset_id"])
            manifest, _ = load_dataset(self.registry_path, message["dataset_id"], strategy_version=None)
            self.store.put(manifest, "manifest_sha256")
            return {"ok": True, "manifest": manifest}
        if op == "read_receipt":
            obj(message, ["op", "receipt_sha256"])
            return {"ok": True, "result": read_receipt(self.store, digest(message["receipt_sha256"]))}
        if op == "run_backtest":
            obj(message, ["op", "strategy", "dataset_id", "evaluation_window", "initial_cash", "cost_model"], ["earnings_dataset_id"])
            return self.run(message)
        if op == "run_marketlab_study":
            obj(message, ["op"], optional=("n_seeds",))
            return self.run_marketlab(message)
        raise Rejected("unsupported_operation")

    def run(self, message):
        from .engine import simulate
        spec = strategy_spec(message["strategy"])
        manifest, all_rows = load_dataset(self.registry_path, message["dataset_id"], spec["schema_version"])
        events = earnings_manifest = None
        needs_events = spec["schema_version"] == 2 and "earnings_events_pti_v1" in spec["data_requirements"]
        if needs_events:
            require("earnings_dataset_id" in message, "missing_dataset")
            earnings_manifest, events = load_earnings_dataset(self.registry_path, message["earnings_dataset_id"], spec["symbol"])
            require(earnings_manifest["partition"] == manifest["partition"] and earnings_manifest["classification"] == manifest["classification"], "dataset_access_denied")
        else:
            require("earnings_dataset_id" not in message, "unknown_field")
        require(manifest["symbols"] == [spec["symbol"]] and manifest["timeframe"] == spec["timeframe"], "strategy_dataset_mismatch")
        # Whole quantities and price precision <= cash precision keep upstream
        # floating notional consistent with the exact rounded persisted ledger.
        require(manifest["price_scale"] <= manifest["cash_scale"], "precision_incompatible_with_engine")
        evaluation = window(message["evaluation_window"])
        start, end = stamp(evaluation["start"]), stamp(evaluation["end_exclusive"])
        upper_bound = stamp(all_rows[-1]["bar_close_at"]).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        require(start >= stamp(all_rows[0]["bar_open_at"]) and end <= upper_bound, "window_outside_dataset")
        hindsight = manifest["classification"] == "historical_unverified"
        # This class is research-only and retains honest fetch availability.
        # Historical windows are hypothetical replays, explicitly labeled below.
        rows = [r for r in all_rows if start <= stamp(r["bar_open_at"]) and
                stamp(r["bar_close_at"] if hindsight else r["available_at"]) < end]
        require(rows and rows[0]["bar_open_at"] == evaluation["start"], "window_not_bar_aligned")
        require(len(rows) >= minimum_bars(spec), "insufficient_warmup")
        # No pre-window observations, implicit padding, or partial final bars.
        require(not any(stamp(r["bar_open_at"]) < end <= stamp(r["bar_close_at"]) for r in all_rows), "partial_bar_window")
        initial = dec(message["initial_cash"], positive=True)
        require(initial == initial.quantize(Decimal(1).scaleb(-manifest["cash_scale"])), "cash_precision_mismatch")
        cost_model = costs(message["cost_model"])
        request = sealed({"schema_version": 1, "kind": "backtest_request", "strategy_sha256": spec["strategy_sha256"],
                          "manifest_sha256": manifest["manifest_sha256"], "evaluation_window": evaluation,
                          "initial_cash": norm(initial), "cost_model": cost_model, "engine": provenance(), "seed": 0,
                          **({"earnings_manifest_sha256": earnings_manifest["manifest_sha256"]} if earnings_manifest else {})}, "request_sha256")
        self.store.put(spec, "strategy_sha256")
        self.store.put(manifest, "manifest_sha256")
        if earnings_manifest:
            self.store.put(earnings_manifest, "manifest_sha256")
        self.store.put(request, "request_sha256")
        try:
            with redirect_stdout(sys.stderr):
                if events is None:
                    orders, fills, equity, metrics = simulate(spec, manifest, rows, norm(initial), cost_model)
                else:
                    orders, fills, equity, metrics = simulate(spec, manifest, rows, norm(initial), cost_model, earnings_events=events)
            artifacts = {}
            for kind, values in [("orders", orders), ("fills", fills), ("equity", equity)]:
                artifacts[kind + "_sha256"] = self.store.put({"schema_version": 1, "kind": kind,
                    "request_sha256": request["request_sha256"], "rows": values})
            reasons = ["qualification_harness_unavailable", "engine_native_float"]
            if hindsight:
                reasons.append("historical_unverified_hindsight")
            receipt = result("completed", reasons, request["request_sha256"], artifacts, metrics)
        except Rejected as error:
            receipt = result("failed", [error.code], request["request_sha256"])
        except Exception as error:
            print(f"engine failure: {type(error).__name__}", file=sys.stderr)
            receipt = result("failed", ["engine_failure"], request["request_sha256"])
        self.store.put(receipt, "receipt_sha256")
        read_receipt(self.store, receipt["receipt_sha256"])
        return {"ok": receipt["status"] == "completed", "request": request, "result": receipt}

    def run_marketlab(self, message):
        from .marketlab_bridge import find_marketlab, run_study
        seeds = message.get("n_seeds", 1)
        root = find_marketlab()
        require(root is not None, "marketlab_unavailable")
        spec = sealed({"schema_version": 1, "kind": "strategy", "engine": "marketlab", "n_seeds": seeds}, "strategy_sha256")
        manifest = sealed({"schema_version": 1, "kind": "manifest", "engine": "marketlab"}, "manifest_sha256")
        request = sealed({"schema_version": 1, "kind": "backtest_request", "strategy_sha256": spec["strategy_sha256"],
                          "manifest_sha256": manifest["manifest_sha256"], "engine": {"name": "marketlab"},
                          "n_seeds": seeds, "seed": 0}, "request_sha256")
        self.store.put(spec, "strategy_sha256")
        self.store.put(manifest, "manifest_sha256")
        self.store.put(request, "request_sha256")
        try:
            metrics = run_study(seeds)
            require(metrics.get("promoting") is False, "marketlab_promotion_forbidden")
            artifacts = {}
            for kind in ("orders", "fills", "equity"):
                artifacts[kind + "_sha256"] = self.store.put({
                    "schema_version": 1, "kind": kind,
                    "request_sha256": request["request_sha256"], "rows": [],
                })
            receipt = result("completed", ["qualification_harness_unavailable", "marketlab_study"],
                             request["request_sha256"], artifacts, metrics)
        except Rejected as error:
            receipt = result("failed", [error.code], request["request_sha256"])
        self.store.put(receipt, "receipt_sha256")
        read_receipt(self.store, receipt["receipt_sha256"])
        return {"ok": receipt["status"] == "completed", "request": request, "result": receipt}


def main():
    raw = sys.stdin.buffer.read(MAX_MESSAGE + 1)
    try:
        registry = os.environ.get("TRADING_DESK_REGISTRY", str(DEFAULT_REGISTRY))
        artifact_dir = os.environ.get("TRADING_DESK_ARTIFACT_DIR")
        require(artifact_dir, "artifact_directory_required")
        response = Worker(registry, artifact_dir).handle_raw(raw)
    except Exception as error:
        code = error.code if isinstance(error, Rejected) else "worker_initialization_failed"
        response = {"schema_version": 1, "kind": "worker_response", "ok": False,
                    "error": {"code": code}, "result": result("failed", [code]), "events": []}
    sys.stdout.buffer.write(canonical(response) + b"\n")
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
