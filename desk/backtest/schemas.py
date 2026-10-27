"""Exported structural JSON Schemas. Semantic and hash checks live in contracts.py.

Run `python -m backtest.schemas` only when changing these schema definitions.
"""
from pathlib import Path
from .contracts import canonical


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}


def const(value):
    return {"const": value}


HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
ID = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"}
DECIMAL = {"type": "string", "maxLength": 40, "pattern": "^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$"}
TIME = {"type": "string", "pattern": r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$"}
TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}
NONNEGATIVE = {"type": "integer", "minimum": 0}
COST = obj({"commission_bps": {**DECIMAL, "default": "0"},
            "slippage_bps_per_side": {**DECIMAL, "default": "0"}, "rounding": const("half_even")}, [])
WINDOW = obj({"start": TIME, "end_exclusive": TIME})
ENGINE = obj({"name": const("backtesting.py"), "version": const("0.6.6"), "adapter_version": TEXT,
              "dependency_lock_sha256": HASH, "source_tree_sha256": HASH})

STRATEGY_V1 = obj({
    "schema_version": const(1), "kind": const("strategy_spec"), "family": const("sma_cross_long"),
    "symbol": ID, "timeframe": const("1d"),
    "parameters": obj({"fast_window": {"type": "integer", "minimum": 1, "maximum": 199},
                       "slow_window": {"type": "integer", "minimum": 2, "maximum": 200}}),
    "position": obj({"quantity": {"type": "string", "pattern": "^[1-9][0-9]*(\\.0+)?$", "maxLength": 40},
                     "max_positions": const(1)}, ["quantity"]),
    "signal": obj({"field": const("close"), "enter": const("fast_gt_slow"), "exit": const("fast_lte_slow")}),
    "execution": obj({"timing": const("next_bar_open"), "pending_on_end": const("cancel"), "terminal_position": const("mark_to_market")}),
    "strategy_sha256": HASH,
}, ["symbol", "parameters", "position"])


def optional(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def bars(low=2, high=500):
    return {"type": "integer", "minimum": low, "maximum": high}


PARAMETERS_V2 = {
    "sma_cross_long": STRATEGY_V1["properties"]["parameters"],
    "momentum_long": obj({"lookback_bars": bars(), "enter_return_bps": DECIMAL,
                          "exit_return_bps": DECIMAL, "trend_filter_sma": optional(bars())},
                         ["lookback_bars", "enter_return_bps", "exit_return_bps"]),
    "mean_reversion_long": obj({"lookback_bars": bars(5), "enter_z": DECIMAL,
                                "exit_z": DECIMAL, "trend_filter_sma": optional(bars())},
                               ["lookback_bars", "enter_z", "exit_z"]),
    "breakout_long": obj({"entry_channel_bars": bars(), "exit_channel_bars": bars(),
                          "min_volume_ratio": optional(DECIMAL)}, ["entry_channel_bars", "exit_channel_bars"]),
    "post_earnings_drift_long": obj({"min_surprise_pct": DECIMAL, "entry_delay_bars": bars(1, 10),
                                    "hold_bars": bars(1, 60), "max_gap_bps": optional(DECIMAL)},
                                   ["min_surprise_pct", "entry_delay_bars", "hold_bars"]),
}
V2_PROPERTIES = {
    "schema_version": const(2), "kind": const("strategy_spec"),
    "family": {"enum": list(PARAMETERS_V2)},
    "symbol": {"type": "string", "pattern": "^[A-Z][A-Z0-9]{0,4}([.-][A-Z])?$"},
    "timeframe": {"enum": ["1m", "5m", "15m", "1h", "1d"]},
    "parameters": {"type": "object"},
    "position": {"anyOf": [
        obj({"sizing": const("fixed_quantity"), "quantity": STRATEGY_V1["properties"]["position"]["properties"]["quantity"],
             "max_positions": const(1)}, ["quantity"]),
        obj({"sizing": const("fixed_notional"), "notional_usd": DECIMAL, "max_positions": const(1)},
            ["sizing", "notional_usd"])]},
    "exits": obj({"stop_loss_bps": optional(DECIMAL), "take_profit_bps": optional(DECIMAL),
                  "max_hold_bars": optional(bars(1, 500))}, []),
    "execution": obj({"timing": const("next_bar_open"), "order_type": {"enum": ["limit", "market"]},
                      "limit_offset_bps": DECIMAL, "pending_on_end": const("cancel"),
                      "terminal_position": const("mark_to_market")}, []),
    "data_requirements": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True,
                          "items": {"enum": ["ohlcv_pti_v1", "earnings_events_pti_v1"]},
                          "contains": const("ohlcv_pti_v1")},
    "rationale": TEXT, "strategy_sha256": HASH,
}
STRATEGY_V2 = obj(V2_PROPERTIES, ["schema_version", "family", "symbol", "parameters", "position", "rationale"])
STRATEGY_V2["anyOf"] = [
    {"properties": {"family": const(family), "parameters": parameters,
                    **({"timeframe": {"enum": ["1h", "1d"]},
                        "data_requirements": {"type": "array", "contains": const("earnings_events_pti_v1")}}
                       if family == "post_earnings_drift_long" else {})},
     "required": ["family", "parameters", *(["data_requirements"] if family == "post_earnings_drift_long" else [])]}
    for family, parameters in PARAMETERS_V2.items()
]
# Keep the root closed for schema consumers; each version is independently closed.
STRATEGY = obj({key: {} for key in STRATEGY_V1["properties"] | V2_PROPERTIES}, ["symbol", "parameters", "position"])
STRATEGY["anyOf"] = [STRATEGY_V1, STRATEGY_V2]

MANIFEST = obj({
    "schema_version": const(1), "kind": const("dataset_manifest"), "dataset_id": ID,
    "classification": {"enum": ["synthetic", "historical_research", "historical_unverified"]}, "partition": {"enum": ["development", "validation"]},
    "source": obj({"provider": TEXT, "source_reference": TEXT, "license_reference": TEXT,
                   "retrieved_at": TIME, "snapshot_sha256": HASH}),
    "format": const("jsonl"), "row_schema": const("ohlcv_pti_v1"), "timeframe": {"enum": ["1m", "5m", "15m", "1h", "1d"]},
    "calendar_id": {"enum": ["weekday_utc_0930_1600", "nyse_regular"]}, "price_basis": {"enum": ["raw", "split_dividend_adjusted"]},
    "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
    "price_scale": {"type": "integer", "minimum": 0, "maximum": 6}, "quantity_scale": const(0),
    "cash_scale": {"type": "integer", "minimum": 0, "maximum": 6},
    "symbols": {"type": "array", "items": ID, "minItems": 1, "maxItems": 1},
    "coverage": obj({"first_bar_open": TIME, "last_bar_close": TIME}),
    "row_count": {"type": "integer", "minimum": 2, "maximum": 10000},
    "data_sha256": HASH, "manifest_sha256": HASH,
})
MANIFEST["properties"]["provenance"] = {"type": "object"}
MANIFEST["allOf"] = [{
    "if": {"properties": {"classification": const("historical_unverified")}},
    "then": {"required": ["provenance"], "properties": {"partition": {"enum": ["development", "validation"]}, "timeframe": const("1d"),
             "calendar_id": const("nyse_regular"), "price_basis": const("split_dividend_adjusted")}},
    "else": {"not": {"required": ["provenance"]}, "properties": {"price_basis": const("raw")}},
}]

REQUEST = obj({
    "schema_version": const(1), "kind": const("backtest_request"), "strategy_sha256": HASH, "manifest_sha256": HASH,
    "evaluation_window": WINDOW, "initial_cash": DECIMAL,
    "cost_model": obj(COST["properties"]), "engine": ENGINE, "seed": const(0), "request_sha256": HASH,
})
REQUEST["properties"]["earnings_manifest_sha256"] = HASH

METRICS = obj({**{key: DECIMAL for key in ["initial_cash", "final_cash", "final_equity", "realized_net_pnl",
                     "unrealized_pnl", "commission_total", "slippage_cost_total"]},
               **{key: NONNEGATIVE for key in ["completed_trades", "open_positions", "rejected_orders", "cancelled_pending_orders"]},
               **{key: {"anyOf": [DECIMAL, {"type": "null"}]} for key in ["total_return", "max_drawdown"]}})
NULL_HASH = {"anyOf": [HASH, {"type": "null"}]}
RESULT = obj({
    "schema_version": const(1), "kind": const("backtest_result"), "request_sha256": NULL_HASH,
    "status": {"enum": ["completed", "rejected", "failed", "unknown"]},
    "qualification": const("research_only"), "promoting": const(False),
    "reason_codes": {"type": "array", "items": ID, "uniqueItems": True},
    "artifacts": obj({key: NULL_HASH for key in ["orders_sha256", "fills_sha256", "equity_sha256"]}),
    "metrics": {"anyOf": [METRICS, {"type": "null"}]}, "receipt_sha256": HASH,
})
RESULT["allOf"] = [{"if": {"properties": {"status": const("completed")}},
                     "then": {"properties": {"metrics": METRICS, "request_sha256": HASH,
                               "artifacts": obj({key: HASH for key in ["orders_sha256", "fills_sha256", "equity_sha256"]})}},
                     "else": {"properties": {"metrics": {"type": "null"}}}}]

SCHEMAS = {"strategy-spec": STRATEGY, "dataset-manifest": MANIFEST,
           "backtest-request": REQUEST, "backtest-result": RESULT}
SCHEMAS["earnings-event"] = obj({"available_at": TIME, "eps_actual": DECIMAL, "eps_estimate": DECIMAL,
                                "session": {"enum": ["bmo", "amc"]}, "symbol": V2_PROPERTIES["symbol"]},
                               ["available_at", "eps_actual", "eps_estimate", "session"])
SCHEMAS["earnings-dataset-manifest"] = obj({
    **{key: MANIFEST["properties"][key] for key in ("schema_version", "kind", "dataset_id", "classification", "partition", "source", "format", "data_sha256", "manifest_sha256")},
    "row_schema": const("earnings_events_pti_v1"),
    "symbols": {"type": "array", "items": V2_PROPERTIES["symbol"], "minItems": 1, "maxItems": 1},
    "coverage": obj({"first_available_at": TIME, "last_available_at": TIME}),
    "row_count": bars(1, 10000),
})


def exported():
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema} for name, schema in SCHEMAS.items()}


if __name__ == "__main__":
    folder = Path(__file__).parent / "contracts"
    folder.mkdir(exist_ok=True)
    for name, schema in exported().items():
        (folder / (name + ".schema.json")).write_bytes(canonical(schema) + b"\n")
