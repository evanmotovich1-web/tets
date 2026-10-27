"""Frozen-spec expanding-window research. No agent holdout reader exists here.

Provider captures and preregistrations are immutable in a host-owned SQLite DB.
Stationary bootstrap: Politis/Romano (1994), doi:10.1080/01621459.1994.10476870.
Holm step-down: Holm (1979), doi:10.2307/4615733. Resampling units are market
days, not trades; block_bars counts observations of that daily return series.
"""
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from zoneinfo import ZoneInfo
import os
import sqlite3
import sys

import numpy as np

from .contracts import (Rejected, canonical, costs, dec, digest, identifier, integer, loads, minimum_bars,
                        norm, obj, require, rounded, scaled, sealed, sha, sha_bytes, stamp, strategy_spec, verify, window)
from .engine import simulate, validate_v2_rows
from .storage import bounded_read, load_dataset, load_earnings_dataset

PARTITIONS = ("development", "validation")


def walkforward_costs(value, timeframe):
    obj(value, ["commission_bps", "slippage_bps_per_side", "regulatory_fee_bps_on_sells", "rounding"])
    result = costs({k: v for k, v in value.items() if k != "regulatory_fee_bps_on_sells"})
    result["regulatory_fee_bps_on_sells"] = norm(dec(value["regulatory_fee_bps_on_sells"], high=Decimal("1000")))
    require(Decimal(result["slippage_bps_per_side"]) >= (5 if timeframe == "1d" else 10), "cost_floor")
    return result


def validate_preregistration(value, spec, requested_at):
    obj(value, ["schema_version", "kind", "campaign_id", "created_at", "author", "strategy_sha256", "datasets",
                "cost_model", "baselines", "pass_criteria", "paper_criteria", "preregistration_sha256"])
    verify(value, "preregistration_sha256")
    # Version 1 carried shadow_criteria. Shadow testing is retired; v1 records are
    # never reinterpreted under the backtest-only ladder.
    require(value["schema_version"] != 1, "preregistration_version_retired")
    require(value["schema_version"] == 2 and value["kind"] == "preregistration", "not_preregistered")
    require(stamp(value["created_at"]) < stamp(requested_at) and value["strategy_sha256"] == spec["strategy_sha256"], "not_preregistered")
    identifier(value["campaign_id"])
    author = obj(value["author"], ["slot", "model", "role"])
    require(author["role"] == "author" and all(type(v) is str and 0 < len(v) <= 200 for v in author.values()), "invalid_author")
    datasets = obj(value["datasets"], ["development", "validation", "holdout_id"])
    digest(datasets["development"]); digest(datasets["validation"]); identifier(datasets["holdout_id"])
    walkforward_costs(value["cost_model"], spec["timeframe"])
    require(value["baselines"] == ["buy_and_hold", "cash"], "missing_baseline")
    criteria = obj(value["pass_criteria"], ["min_independent_trades", "max_drawdown_pct", "min_excess_return_vs_buy_and_hold_pct", "alpha", "test", "bootstrap"])
    integer(criteria["min_independent_trades"], 30, 10000)
    dec(criteria["max_drawdown_pct"], positive=True, high=Decimal("15"))
    dec(criteria["min_excess_return_vs_buy_and_hold_pct"], high=Decimal("1000000"))
    dec(criteria["alpha"], positive=True, high=Decimal("0.05"))
    require(criteria["test"] == "stationary_block_bootstrap_excess_daily_returns", "unsupported_statistical_test")
    bootstrap = obj(criteria["bootstrap"], ["block_bars", "resamples", "seed"])
    integer(bootstrap["block_bars"], 1, 500); integer(bootstrap["resamples"], 1000, 10000); integer(bootstrap["seed"], 0, 2**32 - 1)
    # Optional paper-money bar. null means the candidate is judged on backtests only;
    # the paper stage itself stays disabled unless Evan enables it for a campaign.
    if value["paper_criteria"] is not None:
        paper = obj(value["paper_criteria"], ["min_market_days", "min_round_trips", "max_fill_model_drift_bps", "min_excess_return_vs_buy_and_hold_pct"])
        integer(paper["min_market_days"], 5, 10000); integer(paper["min_round_trips"], 10, 1000000)
        dec(paper["max_fill_model_drift_bps"], high=Decimal("25"))
        require(dec(paper["min_excess_return_vs_buy_and_hold_pct"], high=Decimal("1000000")) >= 0, "parameter_out_of_range")
    return deepcopy(value)


def provider_manifest(snapshot, partition, *, development_manifest=None):
    if type(snapshot) is dict and snapshot.get("provider") == "maverick_yfinance":
        from .maverick_import import maverick_manifest
        return maverick_manifest(snapshot, partition, development_manifest)
    require(development_manifest is None, "unexpected_development_reference")
    require(partition in PARTITIONS, "dataset_access_denied")
    obj(snapshot, ["schema_version", "kind", "provider", "classification", "symbol", "timeframe", "received_at", "rows",
                   "availability_basis", "point_in_time_verified", "historical_eligible", "counts", "raw_response_sha256", "data_sha256", "snapshot_sha256"])
    verify(snapshot, "snapshot_sha256")
    require(snapshot["schema_version"] == 1 and snapshot["kind"] == "bar_snapshot", "invalid_provider_snapshot")
    # Current 2.b live captures explicitly lack historical provenance. A caller
    # setting boolean eligibility flags cannot turn them into qualified history.
    require(snapshot["provider"] == "recorded_fixture" and snapshot["classification"] == "synthetic", "provider_history_unqualified")
    digest(snapshot["raw_response_sha256"]); stamp(snapshot["received_at"])
    rows = deepcopy(snapshot["rows"])
    require(type(rows) is list and 2 <= len(rows) <= 10000, "invalid_provider_rows")
    for row in rows:
        obj(row, ["symbol", "bar_open_at", "bar_close_at", "available_at", "open", "high", "low", "close", "volume"])
        require(row["symbol"] == snapshot["symbol"], "symbol_out_of_scope")
        prices = {k: dec(row[k], positive=True) for k in ("open", "high", "low", "close")}
        require(prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"], "invalid_ohlc")
        dec(row["volume"])
        require(stamp(row["available_at"]) <= stamp(snapshot["received_at"]), "future_data")
    validate_v2_rows(rows, snapshot["timeframe"])
    data = b"".join(canonical(row) + b"\n" for row in rows)
    require(sha_bytes(data) == digest(snapshot["data_sha256"]), "hash_mismatch")
    counts = obj(snapshot["counts"], ["raw_rows", "returned_rows", "excluded"])
    require(type(counts["raw_rows"]) is int and type(counts["returned_rows"]) is int and counts["returned_rows"] == len(rows) and
            type(counts["excluded"]) is dict and all(type(n) is int and n >= 0 for n in counts["excluded"].values()) and
            counts["raw_rows"] == len(rows) + sum(counts["excluded"].values()), "provider_count_mismatch")
    scale = max(2, max(-Decimal(row[key]).as_tuple().exponent for row in rows for key in ("open", "high", "low", "close")))
    require(scale <= 6, "price_precision_mismatch")
    manifest = sealed({"schema_version": 1, "kind": "dataset_manifest", "dataset_id": "capture:" + snapshot["snapshot_sha256"],
        "classification": "synthetic", "partition": partition,
        "source": {"provider": "recorded_fixture", "source_reference": snapshot["snapshot_sha256"], "license_reference": "synthetic_fixture",
                   "retrieved_at": snapshot["received_at"], "snapshot_sha256": snapshot["snapshot_sha256"]},
        "format": "jsonl", "row_schema": "ohlcv_pti_v1", "timeframe": snapshot["timeframe"], "calendar_id": "nyse_regular",
        "price_basis": "raw", "currency": "USD", "price_scale": scale, "cash_scale": scale, "quantity_scale": 0,
        "symbols": [snapshot["symbol"]], "coverage": {"first_bar_open": rows[0]["bar_open_at"], "last_bar_close": rows[-1]["bar_close_at"]},
        "row_count": len(rows), "data_sha256": snapshot["data_sha256"]}, "manifest_sha256")
    return manifest, rows


def stationary_bootstrap_pvalue(excess_daily_returns, *, block_bars=10, resamples=10000, seed=0):
    """One-sided mean-excess test using a centered stationary bootstrap null.

    Geometric blocks continue circularly with probability 1 - 1/block_bars.
    Add-one Monte Carlo correction prevents a reported p-value of zero.
    """
    integer(block_bars, 1, 500); integer(resamples, 1000, 10000); integer(seed, 0, 2**32 - 1)
    x = np.asarray([float(Decimal(value)) for value in excess_daily_returns], dtype=float)
    require(len(x) <= 10000 and np.isfinite(x).all(), "invalid_daily_returns")
    require(len(x) * resamples <= 50000000, "bootstrap_budget_exceeded")
    if len(x) < 2 or x.mean() <= 0:
        return "1"
    mean = float(x.mean()); centered = x - mean
    rng = np.random.default_rng(seed); exceed = 0
    for offset in range(0, resamples, 128):
        size = min(128, resamples - offset)
        indices = rng.integers(0, len(x), size=size); totals = centered[indices].copy()
        for _ in range(1, len(x)):
            restart = rng.random(size) < 1 / block_bars
            fresh = rng.integers(0, len(x), size=size)
            indices = np.where(restart, fresh, (indices + 1) % len(x))
            totals += centered[indices]
        exceed += int(np.count_nonzero(totals / len(x) >= mean))
    return norm((Decimal(exceed + 1) / Decimal(resamples + 1)).quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING))


def holm_adjust(pvalues, alpha="0.05"):
    """Includes missing/rejected/abandoned candidates supplied as p=1."""
    level = dec(alpha, positive=True, high=Decimal("0.05"))
    require(type(pvalues) is dict and pvalues, "empty_search_family")
    ordered = sorted((dec(p, high=Decimal(1)), key) for key, p in pvalues.items())
    adjusted, maximum = {}, Decimal(0)
    for rank, (p, key) in enumerate(ordered):
        maximum = max(maximum, min(Decimal(1), p * (len(ordered) - rank)))
        adjusted[key] = {"adjusted_pvalue": norm(maximum), "passed": maximum <= level,
                         "threshold": norm(level / (len(ordered) - rank)), "rank": rank + 1, "N": len(ordered)}
    return adjusted


def buy_and_hold(rows, initial_cash, cost_model, price_scale, cash_scale):
    cash = Decimal(initial_cash)
    price = rounded(Decimal(rows[0]["open"]) * (1 + Decimal(cost_model["slippage_bps_per_side"]) / 10000), price_scale)
    rate = Decimal(cost_model["commission_bps"]) / 10000
    count = int(cash // (price * (1 + rate)))
    while count and rounded(price * count, cash_scale) + rounded(price * count * rate, cash_scale) > cash:
        count -= 1
    fee = rounded(price * count * rate, cash_scale)
    cash -= price * count + fee
    equity = [{"at": row["bar_close_at"], "equity": scaled(cash + count * Decimal(row["close"]), cash_scale)} for row in rows]
    return {"quantity": str(count), "entry_price": scaled(price, price_scale), "commission_total": scaled(fee, cash_scale),
            "slippage_cost_total": scaled((price - Decimal(rows[0]["open"])) * count, cash_scale), "regulatory_fee_total": scaled(0, cash_scale),
            "terminal_position": "mark_to_market", "equity": equity}


def daily_returns(equity, initial_cash):
    daily = {}
    for row in equity:
        day = stamp(row["at"]).astimezone(ZoneInfo("America/New_York")).date().isoformat()
        daily[day] = Decimal(row["equity"])
    previous = Decimal(initial_cash); result = []
    for day, value in daily.items():
        require(previous > 0, "nonpositive_equity")
        result.append({"market_day": day, "return": norm(value / previous - 1)})
        previous = value
    return result


def evaluate_rows(spec, preregistration, campaign, manifest, rows, *, warmup_rows=None, earnings_events=None):
    config = campaign["walkforward"]
    initial = campaign["initial_cash"]
    costs_used = walkforward_costs(preregistration["cost_model"], spec["timeframe"])
    warmup = deepcopy(warmup_rows) if warmup_rows is not None else rows[:config["initial_train_bars"]]
    testing = rows if warmup_rows is not None else rows[len(warmup):]
    require(len(warmup) >= minimum_bars(spec) and testing, "insufficient_warmup")
    require(warmup[-1]["bar_close_at"] < testing[0]["bar_open_at"] or warmup[-1]["bar_close_at"] == testing[0]["bar_open_at"], "partition_overlap")
    combined = warmup + testing
    with redirect_stdout(sys.stderr):
        orders, fills, equity, metrics = simulate(spec, manifest, combined, initial, costs_used,
            earnings_events=earnings_events, decision_start_at=warmup[-1]["bar_close_at"])
    equity = equity[len(warmup):]
    baseline = buy_and_hold(testing, initial, costs_used, manifest["price_scale"], manifest["cash_scale"])
    strategy_daily = daily_returns(equity, initial); baseline_daily = daily_returns(baseline["equity"], initial)
    require([r["market_day"] for r in strategy_daily] == [r["market_day"] for r in baseline_daily], "baseline_window_mismatch")
    excess = [norm(Decimal(a["return"]) - Decimal(b["return"])) for a, b in zip(strategy_daily, baseline_daily)]
    completed = []; opened = None; last_exit = None
    for fill in fills:
        if fill["side"] == "buy": opened = fill["executed_at"]
        elif opened is not None:
            if last_exit is None or opened >= last_exit:
                completed.append({"entry_at": opened, "exit_at": fill["executed_at"]}); last_exit = fill["executed_at"]
            opened = None
    final = Decimal(equity[-1]["equity"]); bh_final = Decimal(baseline["equity"][-1]["equity"])
    peak = Decimal(initial); drawdown = Decimal(0)
    for row in equity:
        value = Decimal(row["equity"]); peak = max(peak, value); drawdown = max(drawdown, (peak - value) / peak)
    folds = []
    for start in range(0, len(testing), config["test_bars"]):
        end = min(start + config["test_bars"], len(testing)); before = initial if start == 0 else equity[start - 1]["equity"]
        folds.append({"train_start": combined[0]["bar_open_at"], "train_end": combined[len(warmup) + start - 1]["bar_close_at"],
          "test_start": testing[start]["bar_open_at"], "test_end": testing[end - 1]["bar_close_at"],
          "test_rows": end - start, "starting_equity": before, "final_equity": equity[end - 1]["equity"], "strategy_sha256": spec["strategy_sha256"]})
    bootstrap = preregistration["pass_criteria"]["bootstrap"]
    return {"mode": "expanding_window_frozen_spec", "position_policy": "continuous_across_folds", "cost_model": costs_used,
      **({"availability_policy": "hindsight_validation_only" if manifest.get("partition") == "validation" else "hindsight_development_only", "point_in_time_verified": False}
         if manifest.get("classification") == "historical_unverified" else {}),
      "warmup_rows": len(warmup), "evaluated_rows": len(testing), "folds": folds, "orders": orders, "fills": fills, "equity": equity,
      "metrics": {**metrics, "max_drawdown": norm(drawdown), "independent_trades": len(completed),
                  "excess_return_vs_buy_and_hold_pct": norm((final - bh_final) / Decimal(initial) * 100),
                  "excess_return_vs_cash_pct": norm((final / Decimal(initial) - 1) * 100)},
      "baselines": {"buy_and_hold": baseline, "cash": {"initial_cash": initial, "final_equity": initial, "total_return": "0"}},
      "independent_trade_intervals": completed, "daily_excess_returns": excess, "resampling_unit": "market_day",
      "daily_observations": len(excess), "pvalue": stationary_bootstrap_pvalue(excess, **bootstrap)}


class CampaignRegistry:
    """Host-owned state. No registry or ledger paths are accepted from a spec."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=2)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS datasets (hash TEXT PRIMARY KEY, payload BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, payload BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS registrations (hash TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, payload BLOB NOT NULL, spec BLOB NOT NULL, status TEXT NOT NULL, error TEXT);
          CREATE TABLE IF NOT EXISTS evaluations (registration TEXT NOT NULL, partition TEXT NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(registration, partition));
          CREATE TABLE IF NOT EXISTS holdout_requests (window_hash TEXT PRIMARY KEY, registration TEXT NOT NULL, payload BLOB NOT NULL);
        """)
        os.chmod(self.path, 0o600)

    def close(self): self.db.close()

    def _store_dataset(self, manifest, rows):
        verify(manifest, "manifest_sha256")
        if manifest.get("classification") == "historical_unverified":
            from .maverick_import import STATE, validate_manifest
            require(self.path.resolve().is_relative_to(STATE.resolve()), "gitignored_state_required")
            validate_manifest(manifest)
        payload = canonical({"manifest": manifest, "rows": rows})
        prior = self.db.execute("SELECT payload FROM datasets WHERE hash=?", (manifest["manifest_sha256"],)).fetchone()
        require(prior is None or prior[0] == payload, "dataset_integrity_failure")
        with self.db: self.db.execute("INSERT OR IGNORE INTO datasets VALUES (?,?)", (manifest["manifest_sha256"], payload))
        return manifest

    def import_provider(self, snapshot, partition, *, development_manifest_sha256=None):
        development = self.dataset(development_manifest_sha256)["manifest"] if development_manifest_sha256 is not None else None
        return self._store_dataset(*provider_manifest(snapshot, partition, development_manifest=development))

    def import_registered_dataset(self, registry_path, dataset_id):
        # Existing storage checks deny holdout before opening its manifest/data.
        manifest, rows = load_dataset(registry_path, dataset_id, strategy_version=None)
        return self._store_dataset(manifest, rows)

    def import_registered_earnings(self, registry_path, dataset_id, symbol):
        return self._store_dataset(*load_earnings_dataset(registry_path, dataset_id, symbol))

    def dataset(self, key):
        row = self.db.execute("SELECT payload FROM datasets WHERE hash=?", (digest(key),)).fetchone()
        require(row is not None, "missing_dataset")
        data = loads(row[0]); verify(data["manifest"], "manifest_sha256")
        require(data["manifest"]["partition"] in PARTITIONS, "dataset_access_denied")
        require(sha_bytes(b"".join(canonical(r) + b"\n" for r in data["rows"])) == data["manifest"]["data_sha256"], "dataset_integrity_failure")
        if data["manifest"]["classification"] == "historical_unverified":
            from .maverick_import import validate_manifest
            validate_v2_rows(data["rows"], data["manifest"]["timeframe"], observed_at=validate_manifest(data["manifest"]))
        return data

    def create_campaign(self, value):
        obj(value, ["schema_version", "kind", "campaign_id", "created_at", "max_candidates", "datasets", "holdout_window",
                    "initial_cash", "walkforward", "campaign_sha256"], ["earnings_datasets"])
        verify(value, "campaign_sha256"); identifier(value["campaign_id"]); stamp(value["created_at"])
        require(value["schema_version"] == 1 and value["kind"] == "walkforward_campaign", "invalid_campaign")
        integer(value["max_candidates"], 1, 50); dec(value["initial_cash"], positive=True)
        obj(value["datasets"], ["development", "validation", "holdout_id"]); identifier(value["datasets"]["holdout_id"])
        obj(value["walkforward"], ["initial_train_bars", "test_bars"])
        integer(value["walkforward"]["initial_train_bars"], 2, 9999); integer(value["walkforward"]["test_bars"], 1, 10000)
        development = self.dataset(value["datasets"]["development"])["manifest"]
        validation = self.dataset(value["datasets"]["validation"])["manifest"]
        require(development["partition"] == "development" and validation["partition"] == "validation" and
                all(development[field] == validation[field] for field in ("symbols", "timeframe", "currency", "price_basis", "classification")), "strategy_dataset_mismatch")
        for manifest in (development, validation):
            initial = Decimal(value["initial_cash"])
            require(initial == rounded(initial, manifest["cash_scale"]), "cash_precision_mismatch")
        require(development["coverage"]["last_bar_close"] < validation["coverage"]["first_bar_open"], "partition_overlap")
        if validation["classification"] == "historical_unverified":
            require(validation["provenance"]["development_manifest_sha256"] == development["manifest_sha256"] and
                    validation["provenance"]["development_last_bar_close"] == development["coverage"]["last_bar_close"], "development_reference_mismatch")
        held = window(value["holdout_window"])
        require(stamp(held["start"]) > stamp(validation["coverage"]["last_bar_close"]), "holdout_not_after_validation")
        if "earnings_datasets" in value:
            obj(value["earnings_datasets"], ["development", "validation"])
            for partition, key in value["earnings_datasets"].items():
                manifest = self.dataset(key)["manifest"]
                require(manifest["row_schema"] == "earnings_events_pti_v1" and manifest["partition"] == partition and manifest["symbols"] == development["symbols"], "strategy_dataset_mismatch")
        payload = canonical(value)
        prior = self.db.execute("SELECT payload FROM campaigns WHERE id=?", (value["campaign_id"],)).fetchone()
        require(prior is None or prior[0] == payload, "campaign_immutable")
        with self.db: self.db.execute("INSERT OR IGNORE INTO campaigns VALUES (?,?)", (value["campaign_id"], payload))
        return deepcopy(value)

    def campaign(self, campaign_id):
        row = self.db.execute("SELECT payload FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        require(row is not None, "unknown_campaign")
        result = loads(row[0]); verify(result, "campaign_sha256"); return result

    def register(self, campaign_id, spec_value, preregistration, requested_at):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            return self._register(campaign_id, spec_value, preregistration, requested_at)

    def _register(self, campaign_id, spec_value, preregistration, requested_at):
        campaign = self.campaign(campaign_id)
        # Count every attempted preregistration, including malformed, duplicate-rule
        # and over-budget attempts. Exact replay of the same bytes counts once.
        key = sha(preregistration)
        if type(preregistration) is dict and "preregistration_sha256" in preregistration:
            try: verify(preregistration, "preregistration_sha256"); key = preregistration["preregistration_sha256"]
            except Rejected: pass
        prior = self.db.execute("SELECT payload, spec, status, error FROM registrations WHERE hash=?", (key,)).fetchone()
        if prior:
            require(prior[0] == canonical(preregistration) and prior[1] == canonical(spec_value), "registration_conflict")
            return {"registration_sha256": key, "status": prior[2], "error": prior[3]}
        count = self.db.execute("SELECT COUNT(*) FROM registrations WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
        status, error = "registered", None
        try:
            spec = strategy_spec(spec_value)
            validate_preregistration(preregistration, spec, requested_at)
            require(preregistration["campaign_id"] == campaign_id and preregistration["datasets"] == campaign["datasets"] and
                    stamp(campaign["created_at"]) < stamp(preregistration["created_at"]), "not_preregistered")
            require(count < campaign["max_candidates"], "candidate_limit")
        except Rejected as rejected:
            status, error = "rejected", rejected.code
        self.db.execute("INSERT INTO registrations VALUES (?,?,?,?,?,?)", (key, campaign_id, canonical(preregistration), canonical(spec_value), status, error))
        return {"registration_sha256": key, "status": status, "error": error}

    def registration(self, key):
        row = self.db.execute("SELECT campaign_id,payload,spec,status,error FROM registrations WHERE hash=?", (digest(key),)).fetchone()
        require(row is not None, "not_preregistered")
        require(row[3] == "registered", row[4] or "not_preregistered")
        return self.campaign(row[0]), loads(row[1]), strategy_spec(loads(row[2]))

    def abandon(self, key):
        self.registration(key)
        with self.db: self.db.execute("UPDATE registrations SET status='abandoned',error='abandoned' WHERE hash=?", (key,))

    def family(self, campaign_id):
        current = self.campaign(campaign_id)
        data = {current["datasets"][p] for p in PARTITIONS}
        ids = [row[0] for row in self.db.execute("SELECT id,payload FROM campaigns")
               if data.intersection(loads(row[1])["datasets"][p] for p in PARTITIONS)]
        placeholders = ",".join("?" for _ in ids)
        return [row[0] for row in self.db.execute(f"SELECT hash FROM registrations WHERE campaign_id IN ({placeholders}) ORDER BY hash", ids)]

    def verdict(self, key, partition, minimum_search_n=None):
        campaign, prereg, spec = self.registration(key)
        require(partition in PARTITIONS, "dataset_access_denied")
        family = self.family(campaign["campaign_id"]); pvalues = {}
        require(minimum_search_n is None or (type(minimum_search_n) is int and 1 <= minimum_search_n <= 1000000), "invalid_search_n")
        for member in family:
            row = self.db.execute("SELECT payload FROM evaluations WHERE registration=? AND partition=?", (member, partition)).fetchone()
            status = self.db.execute("SELECT status FROM registrations WHERE hash=?", (member,)).fetchone()[0]
            if row and status == "registered":
                result = loads(row[0]); verify(result, "result_sha256"); pvalues[member] = result["pvalue"]
            else:
                pvalues[member] = "1"
        # Global count of strategies tried (desk exposure index): strategies tried outside this
        # dataset family still count. They enter Holm as p=1, which can only make the test stricter.
        padded = max(0, (minimum_search_n or 0) - len(pvalues))
        for i in range(padded): pvalues[f"exposure:{i:07d}"] = "1"
        adjustment = holm_adjust(pvalues, prereg["pass_criteria"]["alpha"])[key]
        row = self.db.execute("SELECT payload FROM evaluations WHERE registration=? AND partition=?", (key, partition)).fetchone()
        require(row is not None, "stage_not_evaluated")
        evaluation = loads(row[0]); verify(evaluation, "result_sha256")
        failures = []
        metrics = evaluation["metrics"]; criteria = prereg["pass_criteria"]
        if metrics["independent_trades"] < criteria["min_independent_trades"]: failures.append("min_independent_trades")
        if Decimal(metrics["max_drawdown"]) * 100 > Decimal(criteria["max_drawdown_pct"]): failures.append("max_drawdown_pct")
        if Decimal(metrics["excess_return_vs_buy_and_hold_pct"]) < Decimal(criteria["min_excess_return_vs_buy_and_hold_pct"]): failures.append("min_excess_return")
        if not adjustment["passed"]: failures.append("holm_adjustment")
        return sealed({"kind": "walkforward_verdict", "partition": partition, "strategy_sha256": spec["strategy_sha256"],
          "preregistration_sha256": key, "result_sha256": evaluation["result_sha256"], "N": len(family) + padded,
          "search_family_sha256": sha(family) if not padded else sha({"family": family, "exposure_padding": padded}), "adjustment": adjustment, "passed": not failures, "failures": failures,
          "data_classification": evaluation["data_classification"], "qualification": "research_only", "promoting": False}, "verdict_sha256")

    def evaluate(self, key, partition, requested_at, minimum_search_n=None):
        require(partition in PARTITIONS, "dataset_access_denied")
        campaign, prereg, spec = self.registration(key)
        validate_preregistration(prereg, spec, requested_at)
        if partition == "validation":
            require(self.verdict(key, "development", minimum_search_n)["passed"], "development_gate")
        dataset = self.dataset(campaign["datasets"][partition])
        manifest, rows = dataset["manifest"], dataset["rows"]
        require(manifest["symbols"] == [spec["symbol"]] and manifest["timeframe"] == spec["timeframe"], "strategy_dataset_mismatch")
        warmup = self.dataset(campaign["datasets"]["development"])["rows"] if partition == "validation" else None
        events, event_hashes = None, []
        if spec["family"] == "post_earnings_drift_long":
            require("earnings_datasets" in campaign, "missing_dataset")
            stages = PARTITIONS if partition == "validation" else ("development",)
            events = []
            for stage in stages:
                event_key = campaign["earnings_datasets"][stage]; event_hashes.append(event_key)
                events.extend(self.dataset(event_key)["rows"])
            events.sort(key=lambda e: e["available_at"])
        try:
            result = evaluate_rows(spec, prereg, campaign, manifest, rows, warmup_rows=warmup, earnings_events=events)
        except Rejected as error:
            with self.db: self.db.execute("UPDATE registrations SET status='failed',error=? WHERE hash=?", (error.code, key))
            raise
        from .worker import provenance
        result = sealed({"schema_version": 1, "kind": "walkforward_result", "strategy_sha256": spec["strategy_sha256"],
            "preregistration_sha256": key, "campaign_sha256": campaign["campaign_sha256"], "manifest_sha256": manifest["manifest_sha256"],
            "earnings_manifest_sha256s": event_hashes, "partition": partition, "data_classification": manifest["classification"],
            "qualification": "research_only", "promoting": False, "engine": provenance(), **result}, "result_sha256")
        payload = canonical(result)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            prior = self.db.execute("SELECT payload FROM evaluations WHERE registration=? AND partition=?", (key, partition)).fetchone()
            require(prior is None or prior[0] == payload, "evaluation_config_drift")
            self.db.execute("INSERT OR IGNORE INTO evaluations VALUES (?,?,?)", (key, partition, payload))
        return {"result": result, "verdict": self.verdict(key, partition, minimum_search_n)}

    def request_holdout(self, key, ledger_path):
        campaign, prereg, spec = self.registration(key)
        # No holdout manifest or rows are opened, even after designation. Only a
        # one-use, hash-bound packet for a separate operator evaluator is issued.
        stages = [self.verdict(key, p) for p in PARTITIONS]
        require(all(v["passed"] and v["data_classification"] == "historical_research" for v in stages), "holdout_stage_gate")
        ledger = loads(bounded_read(ledger_path, 65536))
        obj(ledger, ["schema_version", "windows"])
        require(ledger["schema_version"] == 1 and type(ledger["windows"]) is list and len(ledger["windows"]) <= 10000 and
                all(type(entry) is dict for entry in ledger["windows"]), "holdout_not_designated")
        matches = [entry for entry in ledger["windows"] if entry.get("holdout_id") == prereg["datasets"]["holdout_id"] and
                   entry.get("strategy_sha256") == spec["strategy_sha256"] and entry.get("window") == campaign["holdout_window"] and
                   entry.get("status") == "designated" and entry.get("designated_by") == "Evan"]
        require(len(matches) == 1, "holdout_not_designated")
        window_hash = sha(campaign["holdout_window"])
        packet = sealed({"kind": "operator_holdout_request", "strategy_sha256": spec["strategy_sha256"], "preregistration_sha256": key,
          "holdout_id": prereg["datasets"]["holdout_id"], "window": campaign["holdout_window"], "designation_sha256": sha(ledger),
          "verdict_sha256s": [v["verdict_sha256"] for v in stages], "qualification": "research_only", "promoting": False}, "request_sha256")
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                for (payload,) in self.db.execute("SELECT payload FROM holdout_requests"):
                    prior = loads(payload)["window"]
                    require(prior["end_exclusive"] <= campaign["holdout_window"]["start"] or
                            prior["start"] >= campaign["holdout_window"]["end_exclusive"], "holdout_already_requested")
                self.db.execute("INSERT INTO holdout_requests VALUES (?,?,?)", (window_hash, key, canonical(packet)))
        except sqlite3.IntegrityError:
            raise Rejected("holdout_already_requested") from None
        return packet


def main():
    registry = None
    try:
        raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        require(len(raw) <= 8 * 1024 * 1024, "message_too_large")
        message = loads(raw)
        require(type(message) is dict and type(message.get("op")) is str, "invalid_operation")
        path = os.environ.get("TRADING_DESK_WALKFORWARD_STATE")
        require(path and Path(path).is_absolute(), "walkforward_state_required")
        registry = CampaignRegistry(path)
        if message["op"] == "import_provider":
            obj(message, ["op", "snapshot", "partition"], ["development_manifest_sha256"])
            value = registry.import_provider(message["snapshot"], message["partition"],
                development_manifest_sha256=message.get("development_manifest_sha256"))
        elif message["op"] == "evaluate":
            obj(message, ["op", "registration_sha256", "partition", "requested_at"], ["minimum_search_n"])
            value = registry.evaluate(message["registration_sha256"], message["partition"], message["requested_at"], message.get("minimum_search_n"))
        elif message["op"] == "create_campaign":
            obj(message, ["op", "campaign"]); value = registry.create_campaign(message["campaign"])
        elif message["op"] == "register":
            obj(message, ["op", "campaign_id", "spec", "preregistration", "requested_at"])
            value = registry.register(message["campaign_id"], message["spec"], message["preregistration"], message["requested_at"])
        else:
            raise Rejected("unsupported_operation")
        response = {"ok": True, "value": value}
    except Rejected as error:
        response = {"ok": False, "error": error.code}
    except Exception:
        response = {"ok": False, "error": "walkforward_failure"}
    finally:
        if registry is not None: registry.close()
    sys.stdout.buffer.write(canonical(response) + b"\n")
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
