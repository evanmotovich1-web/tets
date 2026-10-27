"""Synthetic mechanics and analytic statistical fixtures. No holdout data."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
import tempfile
import unittest

from backtest.contracts import Rejected, canonical, sealed, sha_bytes, strategy_spec
from backtest.walkforward import (CampaignRegistry, buy_and_hold, daily_returns, evaluate_rows, holm_adjust,
                                 provider_manifest, stationary_bootstrap_pvalue, validate_preregistration, walkforward_costs)


def rows(start, count, profitable=False):
    day = datetime.fromisoformat(start).date(); result = []
    pattern = [100, 100, 101, 102, 101, 100, 99, 100]
    for i in range(count):
        while day.weekday() > 4: day += timedelta(days=1)
        close = Decimal(pattern[i % len(pattern)])
        opening = Decimal(90 if profitable and i % 8 == 3 else 110 if profitable and i % 8 == 6 else close)
        opened = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        closed = opened.replace(hour=16, minute=0)
        text = lambda value: value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        result.append({"symbol": "SPY", "bar_open_at": text(opened), "bar_close_at": text(closed), "available_at": text(closed),
          "open": str(opening), "high": str(max(opening, close) + 1), "low": str(min(opening, close) - 1), "close": str(close), "volume": "1000"})
        day += timedelta(days=1)
    return result


def capture(data):
    return sealed({"schema_version": 1, "kind": "bar_snapshot", "provider": "recorded_fixture", "classification": "synthetic",
      "symbol": "SPY", "timeframe": "1d", "received_at": "2025-12-01T00:00:00.000Z", "rows": data,
      "availability_basis": "source", "point_in_time_verified": False, "historical_eligible": False,
      "counts": {"raw_rows": len(data), "returned_rows": len(data), "excluded": {}}, "raw_response_sha256": "0" * 64,
      "data_sha256": sha_bytes(b"".join(canonical(r) + b"\n" for r in data))}, "snapshot_sha256")


def specification():
    return strategy_spec({"schema_version": 2, "family": "momentum_long", "symbol": "SPY", "parameters": {
      "lookback_bars": 2, "enter_return_bps": "50", "exit_return_bps": "0"}, "position": {"quantity": "1"},
      "execution": {"order_type": "market"}, "rationale": "Synthetic accounting and time-order fixture."})


class WalkForwardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = CampaignRegistry(self.root / "campaigns.sqlite")
        self.addCleanup(self.store.close)
        self.dev_rows = rows("2023-01-09", 96, profitable=True)
        self.val_rows = rows("2024-01-08", 80, profitable=True)
        self.dev = self.store.import_provider(capture(self.dev_rows), "development")
        self.val = self.store.import_provider(capture(self.val_rows), "validation")
        self.spec = specification()
        self.campaign = self.new_campaign("campaign-one")
        self.prereg = self.record(self.campaign)

    def new_campaign(self, name, maximum=50):
        return self.store.create_campaign(sealed({"schema_version": 1, "kind": "walkforward_campaign", "campaign_id": name,
          "created_at": "2025-12-01T01:00:00.000Z", "max_candidates": maximum,
          "datasets": {"development": self.dev["manifest_sha256"], "validation": self.val["manifest_sha256"], "holdout_id": "sealed-window-one"},
          "holdout_window": {"start": "2026-01-01T00:00:00.000Z", "end_exclusive": "2026-04-01T00:00:00.000Z"},
          "initial_cash": "1000.00", "walkforward": {"initial_train_bars": 8, "test_bars": 16}}, "campaign_sha256"))

    def record(self, campaign, **changes):
        record = {"schema_version": 2, "kind": "preregistration", "campaign_id": campaign["campaign_id"],
          "created_at": "2025-12-01T02:00:00.000Z", "author": {"slot": "unit", "model": "synthetic", "role": "author"},
          "strategy_sha256": self.spec["strategy_sha256"], "datasets": campaign["datasets"],
          "cost_model": {"commission_bps": "1", "slippage_bps_per_side": "5", "regulatory_fee_bps_on_sells": "1", "rounding": "half_even"},
          "baselines": ["buy_and_hold", "cash"], "pass_criteria": {"min_independent_trades": 30, "max_drawdown_pct": "15",
            "min_excess_return_vs_buy_and_hold_pct": "0", "alpha": "0.05", "test": "stationary_block_bootstrap_excess_daily_returns",
            "bootstrap": {"block_bars": 10, "resamples": 1000, "seed": 0}},
          "paper_criteria": None, **changes}
        return sealed(record, "preregistration_sha256")

    def register(self, record=None, spec=None):
        record = record or self.prereg
        return self.store.register(record["campaign_id"], spec or self.spec, record, "2025-12-02T00:00:00.000Z")

    def test_identical_spec_data_configuration_produce_identical_trades_and_receipts(self):
        key = self.register()["registration_sha256"]
        first = self.store.evaluate(key, "development", "2025-12-02T00:00:00.000Z")
        second = self.store.evaluate(key, "development", "2025-12-03T00:00:00.000Z")
        self.assertEqual(canonical(first), canonical(second))
        self.assertGreater(len(first["result"]["fills"]), 0)

    def test_future_bar_mutation_cannot_change_earlier_decisions_or_fills(self):
        before = evaluate_rows(self.spec, self.prereg, self.campaign, self.dev, self.dev_rows)
        changed = deepcopy(self.dev_rows)
        for row in changed[-8:]: row.update(open="500", close="500", high="501", low="499")
        after = evaluate_rows(self.spec, self.prereg, self.campaign, self.dev, changed)
        cutoff = changed[-8]["bar_open_at"]
        self.assertEqual([o for o in before["orders"] if o["decision_at"] < cutoff], [o for o in after["orders"] if o["decision_at"] < cutoff])
        self.assertEqual([f for f in before["fills"] if f["executed_at"] < cutoff], [f for f in after["fills"] if f["executed_at"] < cutoff])

    def test_folds_are_chronological_disjoint_and_keep_one_continuous_account(self):
        result = evaluate_rows(self.spec, self.prereg, self.campaign, self.dev, self.dev_rows)
        self.assertEqual(sum(f["test_rows"] for f in result["folds"]), len(self.dev_rows) - 8)
        self.assertEqual(result["folds"][1]["starting_equity"], result["folds"][0]["final_equity"])
        for fold in result["folds"]: self.assertLessEqual(fold["train_end"], fold["test_start"])
        self.assertEqual(result["position_policy"], "continuous_across_folds")
        self.assertTrue(all(o["decision_at"] >= self.dev_rows[7]["bar_close_at"] for o in result["orders"]))

    def test_commission_slippage_and_sell_fees_reconcile_against_fills(self):
        result = evaluate_rows(self.spec, self.prereg, self.campaign, self.dev, self.dev_rows)
        cash, held = Decimal("1000"), 0
        commissions = regulatory = Decimal(0)
        for fill in result["fills"]:
            fee, reg = Decimal(fill["commission"]), Decimal(fill.get("regulatory_fee", "0"))
            commissions += fee; regulatory += reg
            count = int(fill["quantity"]); held += count if fill["side"] == "buy" else -count
            cash += (-1 if fill["side"] == "buy" else 1) * Decimal(fill["notional"]) - fee - reg
        self.assertEqual(Decimal(result["metrics"]["final_equity"]), cash + held * Decimal(self.dev_rows[-1]["close"]))
        self.assertEqual(Decimal(result["metrics"]["commission_total"]), commissions)
        self.assertEqual(Decimal(result["metrics"]["regulatory_fee_total"]), regulatory)
        self.assertGreater(regulatory, 0); self.assertGreater(Decimal(result["metrics"]["slippage_cost_total"]), 0)

    def test_higher_preregistered_costs_reduce_account_equity(self):
        lower = evaluate_rows(self.spec, self.prereg, self.campaign, self.dev, self.dev_rows)
        expensive = self.record(self.campaign, cost_model={**self.prereg["cost_model"], "commission_bps": "100"})
        higher = evaluate_rows(self.spec, expensive, self.campaign, self.dev, self.dev_rows)
        self.assertLess(Decimal(higher["metrics"]["final_equity"]), Decimal(lower["metrics"]["final_equity"]))

    def test_buy_hold_uses_first_test_open_same_cash_and_entry_costs(self):
        data = rows("2024-01-08", 2)
        data[0].update(open="100", high="101", low="99", close="100")
        data[1].update(open="120", high="121", low="119", close="120")
        baseline = buy_and_hold(data, "1000.00", {"commission_bps": "100", "slippage_bps_per_side": "100"}, 2, 2)
        self.assertEqual(baseline["quantity"], "9"); self.assertEqual(baseline["entry_price"], "101.00")
        self.assertEqual(baseline["commission_total"], "9.09")
        self.assertEqual(baseline["equity"][-1]["equity"], "1161.91")
        self.assertEqual(baseline["regulatory_fee_total"], "0.00")

    def test_intraday_returns_are_collapsed_to_market_days(self):
        values = [{"at": "2024-01-08T15:00:00.000Z", "equity": "1010"}, {"at": "2024-01-08T16:00:00.000Z", "equity": "1020"},
                  {"at": "2024-01-09T15:00:00.000Z", "equity": "1030"}]
        result = daily_returns(values, "1000")
        self.assertEqual(len(result), 2); self.assertEqual(result[0]["return"], "0.02")

    def test_cost_floor_and_preregistration_timing_cannot_be_weakened(self):
        with self.assertRaisesRegex(Rejected, "cost_floor"):
            walkforward_costs({**self.prereg["cost_model"], "slippage_bps_per_side": "4.99"}, "1d")
        with self.assertRaisesRegex(Rejected, "cost_floor"):
            walkforward_costs(self.prereg["cost_model"], "1m")
        with self.assertRaisesRegex(Rejected, "not_preregistered"):
            validate_preregistration(self.prereg, self.spec, self.prereg["created_at"])

    def test_shadow_era_records_are_never_reinterpreted_and_paper_bar_is_optional_but_strict(self):
        later = "2025-12-02T00:00:00.000Z"
        old = {k: v for k, v in self.prereg.items() if k not in ("paper_criteria", "preregistration_sha256")}
        old = sealed({**old, "schema_version": 1, "shadow_criteria": {"min_market_days": 5, "min_decisions": 20,
                      "max_quote_age_ms_p95": 2000, "max_fill_model_drift_bps": "25"}}, "preregistration_sha256")
        with self.assertRaises(Rejected):
            validate_preregistration(old, self.spec, later)
        v1_shape = sealed({**{k: v for k, v in self.prereg.items() if k != "preregistration_sha256"}, "schema_version": 1}, "preregistration_sha256")
        with self.assertRaisesRegex(Rejected, "preregistration_version_retired"):
            validate_preregistration(v1_shape, self.spec, later)
        paper = {"min_market_days": 5, "min_round_trips": 10, "max_fill_model_drift_bps": "25", "min_excess_return_vs_buy_and_hold_pct": "0"}
        validate_preregistration(self.record(self.campaign, paper_criteria=paper), self.spec, later)
        for loose in ({"min_market_days": 4}, {"min_round_trips": 9}, {"max_fill_model_drift_bps": "26"}, {"min_excess_return_vs_buy_and_hold_pct": "-1"}):
            with self.assertRaises(Rejected):
                validate_preregistration(self.record(self.campaign, paper_criteria={**paper, **loose}), self.spec, later)

    def test_bootstrap_is_seeded_one_sided_and_never_reports_zero(self):
        values = ["0.01"] * 30
        p = stationary_bootstrap_pvalue(values, resamples=1000, seed=7)
        self.assertEqual(p, stationary_bootstrap_pvalue(values, resamples=1000, seed=7))
        self.assertGreater(Decimal(p), 0); self.assertLess(Decimal(p), Decimal("0.002"))
        self.assertEqual(stationary_bootstrap_pvalue(["-0.01"] * 30, resamples=1000), "1")
        self.assertEqual(stationary_bootstrap_pvalue(["0.01"], resamples=1000), "1")

    def test_holm_matches_known_stepdown_values(self):
        result = holm_adjust({"a": "0.01", "b": "0.03", "c": "0.04"})
        self.assertEqual([result[k]["adjusted_pvalue"] for k in ("a", "b", "c")], ["0.03", "0.06", "0.06"])
        self.assertEqual([result[k]["passed"] for k in ("a", "b", "c")], [True, False, False])
        self.assertTrue(all(r["N"] == 3 for r in result.values()))

    def test_search_N_deduplicates_replays_and_includes_rejected_abandoned_and_prior_campaigns(self):
        key = self.register()["registration_sha256"]
        self.register(); self.assertEqual(len(self.store.family("campaign-one")), 1)
        self.store.abandon(key)
        bad = self.record(self.campaign, created_at="2025-12-01T02:01:00.000Z", baselines=["cash"])
        self.assertEqual(self.register(bad)["status"], "rejected")
        other = self.new_campaign("campaign-two")
        self.register(self.record(other))
        self.assertEqual(len(self.store.family("campaign-two")), 3)

    def analytic_result(self, key, partition, p="0.04", classification="synthetic"):
        # Analytic verdict fixture only, not the output of an economic backtest.
        result = sealed({"pvalue": p, "data_classification": classification, "metrics": {
          "independent_trades": 30, "max_drawdown": "0", "excess_return_vs_buy_and_hold_pct": "1"}}, "result_sha256")
        with self.store.db: self.store.db.execute("INSERT OR REPLACE INTO evaluations VALUES (?,?,?)", (key, partition, canonical(result)))

    def test_global_search_n_from_the_exposure_index_can_only_make_holm_stricter(self):
        key = self.register()["registration_sha256"]; self.analytic_result(key, "development")
        local = self.store.verdict(key, "development")
        self.assertEqual(self.store.verdict(key, "development", None), local)
        self.assertEqual(self.store.verdict(key, "development", 1), local, "a floor below the family size changes nothing")
        wide = self.store.verdict(key, "development", 5)
        self.assertEqual(wide["N"], 5); self.assertTrue(local["passed"]); self.assertFalse(wide["passed"])
        self.assertIn("holm_adjustment", wide["failures"]); self.assertNotEqual(wide["search_family_sha256"], local["search_family_sha256"])
        with self.assertRaisesRegex(Rejected, "invalid_search_n"): self.store.verdict(key, "development", 0)

    def test_earlier_pass_is_recomputed_and_revoked_when_N_grows(self):
        key = self.register()["registration_sha256"]; self.analytic_result(key, "development")
        self.assertTrue(self.store.verdict(key, "development")["passed"])
        bad = self.record(self.campaign, created_at="2025-12-01T02:01:00.000Z", baselines=["cash"])
        self.register(bad)
        verdict = self.store.verdict(key, "development")
        self.assertEqual(verdict["N"], 2); self.assertFalse(verdict["passed"])
        self.assertIn("holm_adjustment", verdict["failures"])

    def test_candidate_limit_is_fixed_and_rejected_attempt_still_counts(self):
        campaign = self.new_campaign("limited", maximum=1)
        self.assertEqual(self.register(self.record(campaign))["status"], "registered")
        result = self.register(self.record(campaign, created_at="2025-12-01T02:01:00.000Z"))
        self.assertEqual(result["error"], "candidate_limit")
        self.assertEqual(len(self.store.family("limited")), 2)

    def test_failed_evaluation_remains_in_search_denominator(self):
        body = {k: v for k, v in self.campaign.items() if k != "campaign_sha256"}
        body.update(campaign_id="failed-campaign", walkforward={"initial_train_bars": 9999, "test_bars": 16})
        campaign = self.store.create_campaign(sealed(body, "campaign_sha256"))
        key = self.register(self.record(campaign))["registration_sha256"]
        with self.assertRaisesRegex(Rejected, "insufficient_warmup"): self.store.evaluate(key, "development", "2025-12-02T00:00:00.000Z")
        self.assertIn(key, self.store.family("campaign-one"))
        self.assertEqual(self.store.db.execute("SELECT status FROM registrations WHERE hash=?", (key,)).fetchone()[0], "failed")

    def test_campaign_and_pinned_data_are_immutable(self):
        changed = {k: v for k, v in self.campaign.items() if k != "campaign_sha256"}
        changed["initial_cash"] = "2000.00"
        with self.assertRaisesRegex(Rejected, "campaign_immutable"):
            self.store.create_campaign(sealed(changed, "campaign_sha256"))
        payload = self.store.dataset(self.dev["manifest_sha256"])
        payload["rows"][0]["close"] = "999"
        with self.store.db: self.store.db.execute("UPDATE datasets SET payload=? WHERE hash=?", (canonical(payload), self.dev["manifest_sha256"]))
        with self.assertRaisesRegex(Rejected, "dataset_integrity_failure"): self.store.dataset(self.dev["manifest_sha256"])

    def test_validation_is_gated_and_uses_development_as_past_context(self):
        key = self.register()["registration_sha256"]
        with self.assertRaisesRegex(Rejected, "stage_not_evaluated"):
            self.store.evaluate(key, "validation", "2025-12-02T00:00:00.000Z")
        self.analytic_result(key, "development", p="0.001")
        result = self.store.evaluate(key, "validation", "2025-12-02T00:00:00.000Z")["result"]
        self.assertEqual(result["warmup_rows"], len(self.dev_rows))
        self.assertEqual(result["evaluated_rows"], len(self.val_rows))
        self.assertTrue(all(o["decision_at"] >= self.dev_rows[-1]["bar_close_at"] for o in result["orders"]))

    def test_holdout_denied_before_ledger_or_dataset_access(self):
        key = self.register()["registration_sha256"]
        with patch("backtest.walkforward.bounded_read", side_effect=AssertionError("no holdout reads")):
            with self.assertRaises(Rejected): self.store.request_holdout(key, self.root / "absent-ledger")
        with self.assertRaisesRegex(Rejected, "dataset_access_denied"):
            self.store.evaluate(key, "holdout", "2025-12-02T00:00:00.000Z")
        with self.assertRaisesRegex(Rejected, "dataset_access_denied"):
            self.store.import_provider({}, "holdout")

    def test_holdout_packet_is_designation_bound_one_use_and_never_opens_rows(self):
        key = self.register()["registration_sha256"]
        # Trusted-stage analytic fixtures exercise the operator packet boundary.
        for partition in ("development", "validation"):
            self.analytic_result(key, partition, p="0.001", classification="historical_research")
        ledger = {"schema_version": 1, "windows": [{"holdout_id": "sealed-window-one", "window": self.campaign["holdout_window"],
          "strategy_sha256": self.spec["strategy_sha256"], "status": "designated", "designated_by": "Evan"}]}
        fixture = self.root / "operator-designation.fixture.json"; fixture.write_bytes(canonical(ledger))
        before = fixture.read_bytes()
        packet = self.store.request_holdout(key, fixture)
        self.assertEqual(packet["kind"], "operator_holdout_request"); self.assertNotIn("rows", packet)
        self.assertEqual(before, fixture.read_bytes())
        with self.assertRaisesRegex(Rejected, "holdout_already_requested"): self.store.request_holdout(key, fixture)

    def test_wrong_holdout_designation_and_synthetic_grades_cannot_open_holdout(self):
        key = self.register()["registration_sha256"]
        for partition in ("development", "validation"): self.analytic_result(key, partition, p="0.001")
        with self.assertRaisesRegex(Rejected, "holdout_stage_gate"): self.store.request_holdout(key, self.root / "absent")
        for partition in ("development", "validation"): self.analytic_result(key, partition, p="0.001", classification="historical_research")
        fixture = self.root / "wrong-designation.fixture.json"; fixture.write_bytes(canonical({"schema_version": 1, "windows": []}))
        with self.assertRaisesRegex(Rejected, "holdout_not_designated"): self.store.request_holdout(key, fixture)

    def test_renaming_a_holdout_cannot_reuse_the_same_window(self):
        key = self.register()["registration_sha256"]
        for partition in ("development", "validation"): self.analytic_result(key, partition, p="0.001", classification="historical_research")
        fixture = self.root / "designation.fixture.json"
        entry = {"holdout_id": "sealed-window-one", "window": self.campaign["holdout_window"], "strategy_sha256": self.spec["strategy_sha256"],
                 "status": "designated", "designated_by": "Evan"}
        fixture.write_bytes(canonical({"schema_version": 1, "windows": [entry]})); self.store.request_holdout(key, fixture)
        body = {k: v for k, v in self.campaign.items() if k != "campaign_sha256"}
        body.update(campaign_id="aliased-window", datasets={**body["datasets"], "holdout_id": "alias-for-same-window"})
        campaign = self.store.create_campaign(sealed(body, "campaign_sha256"))
        alias = self.register(self.record(campaign))["registration_sha256"]
        for partition in ("development", "validation"): self.analytic_result(alias, partition, p="0.001", classification="historical_research")
        entry["holdout_id"] = "alias-for-same-window"; fixture.write_bytes(canonical({"schema_version": 1, "windows": [entry]}))
        with self.assertRaisesRegex(Rejected, "holdout_already_requested"): self.store.request_holdout(alias, fixture)

    def test_unqualified_live_capture_and_checksum_tampering_are_refused(self):
        live = capture(self.dev_rows)
        body = {k: v for k, v in live.items() if k != "snapshot_sha256"}; body.update(provider="robinhood_mcp", classification="unqualified_market_data")
        with self.assertRaisesRegex(Rejected, "provider_history_unqualified"): provider_manifest(sealed(body, "snapshot_sha256"), "development")
        broken = capture(self.dev_rows); broken["rows"][0]["close"] = "999"
        with self.assertRaisesRegex(Rejected, "hash_mismatch"): provider_manifest(broken, "development")

    def test_future_availability_and_partition_overlap_reject(self):
        future = deepcopy(self.dev_rows); future[0]["available_at"] = future[1]["bar_close_at"]
        with self.assertRaisesRegex(Rejected, "future_data"): self.store.import_provider(capture(future), "development")
        overlap = self.store.import_provider(capture(self.dev_rows), "validation")
        body = {k: v for k, v in self.campaign.items() if k != "campaign_sha256"}
        body["campaign_id"] = "overlap"; body["datasets"] = {**body["datasets"], "validation": overlap["manifest_sha256"]}
        with self.assertRaisesRegex(Rejected, "partition_overlap"): self.store.create_campaign(sealed(body, "campaign_sha256"))


if __name__ == "__main__": unittest.main()
