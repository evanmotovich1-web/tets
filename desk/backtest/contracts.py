"""Strict v1 contracts. All persisted numeric financial values are decimal strings."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import re
import rfc8785

MAX_MESSAGE = 64 * 1024
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


class Rejected(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code="invalid_input"):
    if not condition:
        raise Rejected(code)


def obj(value, required, optional=()):
    require(type(value) is dict, "expected_object")
    require(set(required) <= value.keys(), "missing_field")
    require(value.keys() <= set(required) | set(optional), "unknown_field")
    return value


def literal(value, expected):
    require(type(value) is type(expected) and value == expected, "unsupported_value")


def integer(value, low, high):
    require(type(value) is int and low <= value <= high, "invalid_integer")
    return value


def identifier(value):
    require(isinstance(value, str) and ID.fullmatch(value), "invalid_id")
    return value


def digest(value):
    require(isinstance(value, str) and HEX.fullmatch(value), "invalid_digest")
    return value


def dec(value, low=Decimal("0"), high=Decimal("1000000000"), positive=False):
    require(type(value) is str and len(value) <= 40 and DECIMAL.fullmatch(value), "invalid_decimal")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise Rejected("invalid_decimal") from None
    require(result.is_finite() and low <= result <= high, "decimal_out_of_range")
    require(not positive or result > 0, "nonpositive_value")
    require(result.as_tuple().exponent >= -12, "excess_decimal_precision")
    return result


def norm(value):
    value = Decimal(value)
    return "0" if value == 0 else format(value.normalize(), "f")


def rounded(value, scale):
    return Decimal(value).quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_EVEN)


def scaled(value, scale):
    return format(rounded(value, scale), f".{scale}f")


def stamp(value):
    require(type(value) is str and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value), "invalid_timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Rejected("invalid_timestamp") from None


def canonical(value):
    try:
        return rfc8785.dumps(value)
    except (ValueError, TypeError, OverflowError):
        raise Rejected("not_canonical_json") from None


def sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha(value):
    return sha_bytes(canonical(value))


def sealed(value, field):
    value = deepcopy(value)
    value[field] = sha(value)
    return value


def verify(value, field):
    require(type(value) is dict and field in value, "missing_digest")
    expected = digest(value[field])
    require(sha({k: v for k, v in value.items() if k != field}) == expected, "hash_mismatch")
    return value


def loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    def invalid(_):
        raise Rejected("nonfinite_json")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise Rejected("malformed_json") from None


STRATEGY_DEFAULTS = {
    "schema_version": 1, "kind": "strategy_spec", "family": "sma_cross_long",
    "timeframe": "1d", "signal": {"field": "close", "enter": "fast_gt_slow", "exit": "fast_lte_slow"},
    "execution": {"timing": "next_bar_open", "pending_on_end": "cancel", "terminal_position": "mark_to_market"},
}


def strategy_spec(value):
    if type(value) is dict and value.get("schema_version") == 2:
        return strategy_spec_v2(value)
    obj(value, ["symbol", "parameters", "position"], [*STRATEGY_DEFAULTS, "strategy_sha256"])
    result = deepcopy(STRATEGY_DEFAULTS)
    result.update(deepcopy(value))
    for key, expected in STRATEGY_DEFAULTS.items():
        if isinstance(expected, dict):
            obj(result[key], expected)
            for k, v in expected.items():
                literal(result[key][k], v)
        else:
            literal(result[key], expected)
    identifier(result["symbol"])
    parameters = obj(result["parameters"], ["fast_window", "slow_window"])
    fast = integer(parameters["fast_window"], 1, 199)
    integer(parameters["slow_window"], fast + 1, 200)
    position = obj(result["position"], ["quantity"], ["max_positions"])
    position.setdefault("max_positions", 1)
    literal(position["max_positions"], 1)
    quantity = dec(position["quantity"], positive=True, high=Decimal("1000000"))
    require(quantity == quantity.to_integral_value(), "fractional_quantity_unsupported")
    position["quantity"] = norm(quantity)
    supplied = result.pop("strategy_sha256", None)
    result = sealed(result, "strategy_sha256")
    require(supplied is None or supplied == result["strategy_sha256"], "hash_mismatch")
    return result


TIMEFRAMES = ("1m", "5m", "15m", "1h", "1d")
FAMILY_FIELDS = {
    "sma_cross_long": ("fast_window", "slow_window"),
    "momentum_long": ("lookback_bars", "enter_return_bps", "exit_return_bps", "trend_filter_sma"),
    "mean_reversion_long": ("lookback_bars", "enter_z", "exit_z", "trend_filter_sma"),
    "breakout_long": ("entry_channel_bars", "exit_channel_bars", "min_volume_ratio"),
    "post_earnings_drift_long": ("min_surprise_pct", "entry_delay_bars", "hold_bars", "max_gap_bps"),
}


def strategy_spec_v2(value):
    # Prohibited instrument/direction requests get explicit codes, never defaults.
    require(not any(k in value for k in ("legs", "instrument", "asset_class", "options")), "instrument_out_of_scope")
    require(not any(k in value for k in ("short", "direction", "side", "margin", "leverage")), "short_not_allowed")
    require(not any(k in value for k in ("session", "extended_hours")), "extended_hours_not_allowed")
    obj(value, ["schema_version", "family", "symbol", "parameters", "position", "rationale"],
        ["kind", "timeframe", "exits", "execution", "data_requirements", "strategy_sha256"])
    result = deepcopy(value)
    literal(result["schema_version"], 2)
    result.setdefault("kind", "strategy_spec")
    literal(result["kind"], "strategy_spec")
    require(type(result["symbol"]) is str and re.fullmatch(r"[A-Z][A-Z0-9]{0,4}(?:[.-][A-Z])?", result["symbol"]), "symbol_out_of_scope")
    result.setdefault("timeframe", "1d")
    require(result["timeframe"] in TIMEFRAMES, "timeframe_too_fast")
    family = result["family"]
    require(type(family) is str and family in FAMILY_FIELDS, "unknown_family")
    p = result["parameters"]
    optional = [k for k in FAMILY_FIELDS[family] if k in ("trend_filter_sma", "min_volume_ratio", "max_gap_bps")]
    obj(p, [k for k in FAMILY_FIELDS[family] if k not in optional], optional)
    for k in optional:
        p.setdefault(k, None)

    def number(target, key, low, high, nullable=False):
        if nullable and target[key] is None:
            return None
        try:
            parsed = dec(target[key], Decimal(str(low)), Decimal(str(high)))
        except Rejected:
            raise Rejected("parameter_out_of_range") from None
        target[key] = norm(parsed)
        return parsed

    def count(target, key, low, high):
        require(type(target[key]) is int and low <= target[key] <= high, "parameter_out_of_range")
        return target[key]

    if family == "sma_cross_long":
        count(p, "fast_window", 1, 199)
        count(p, "slow_window", p["fast_window"] + 1, 200)
    elif family == "momentum_long":
        count(p, "lookback_bars", 2, 500)
        enter = number(p, "enter_return_bps", -500, 5000)
        exit_value = number(p, "exit_return_bps", -5000, 5000)
        require(exit_value <= enter, "parameter_out_of_range")
    elif family == "mean_reversion_long":
        count(p, "lookback_bars", 5, 500)
        enter = number(p, "enter_z", -6, -0.5)
        exit_value = number(p, "exit_z", -6, 3)
        require(exit_value >= enter, "parameter_out_of_range")
    elif family == "breakout_long":
        count(p, "entry_channel_bars", 2, 500)
        count(p, "exit_channel_bars", 2, 500)
        number(p, "min_volume_ratio", 1, 10, nullable=True)
    else:
        require(result["timeframe"] in ("1h", "1d"), "timeframe_too_fast")
        number(p, "min_surprise_pct", 0, 200)
        count(p, "entry_delay_bars", 1, 10)
        count(p, "hold_bars", 1, 60)
        number(p, "max_gap_bps", 0, 5000, nullable=True)
    if "trend_filter_sma" in p and p["trend_filter_sma"] is not None:
        count(p, "trend_filter_sma", 2, 500)

    position = result["position"]
    require(type(position) is dict, "expected_object")
    require(not any(k in position for k in ("short", "direction", "side", "margin", "leverage")), "short_not_allowed")
    sizing = position.get("sizing", "fixed_quantity")
    require(sizing in ("fixed_quantity", "fixed_notional"), "parameter_out_of_range")
    amount = "quantity" if sizing == "fixed_quantity" else "notional_usd"
    obj(position, [amount], ["sizing", "max_positions"])
    position.setdefault("sizing", sizing)
    position.setdefault("max_positions", 1)
    require(type(position["max_positions"]) is int and position["max_positions"] == 1, "max_positions_exceeded")
    if sizing == "fixed_quantity":
        require(not (type(position[amount]) is str and position[amount].startswith("-")), "short_not_allowed")
        quantity = number(position, amount, 1, 1000000)
        require(quantity == quantity.to_integral_value(), "parameter_out_of_range")
    else:
        number(position, amount, 1, 1000)

    exits = result.setdefault("exits", {})
    obj(exits, [], ["stop_loss_bps", "take_profit_bps", "max_hold_bars"])
    for key in ("stop_loss_bps", "take_profit_bps", "max_hold_bars"):
        exits.setdefault(key, None)
    number(exits, "stop_loss_bps", 10, 5000, nullable=True)
    number(exits, "take_profit_bps", 10, 10000, nullable=True)
    if exits["max_hold_bars"] is not None:
        count(exits, "max_hold_bars", 1, 500)
    execution = result.setdefault("execution", {})
    defaults = {"timing": "next_bar_open", "order_type": "limit", "limit_offset_bps": "10",
                "pending_on_end": "cancel", "terminal_position": "mark_to_market"}
    obj(execution, [], defaults)
    for key, default in defaults.items():
        execution.setdefault(key, default)
    require(execution["timing"] == "next_bar_open", "lookahead_fill")
    require(execution["order_type"] in ("limit", "market"), "parameter_out_of_range")
    number(execution, "limit_offset_bps", 0, 100)
    require(execution["pending_on_end"] == "cancel" and execution["terminal_position"] == "mark_to_market", "parameter_out_of_range")
    requirements = result.setdefault("data_requirements", ["ohlcv_pti_v1"])
    require(type(requirements) is list and 1 <= len(requirements) <= 2 and
            all(type(item) is str and item in ("ohlcv_pti_v1", "earnings_events_pti_v1") for item in requirements) and
            len(set(requirements)) == len(requirements) and "ohlcv_pti_v1" in requirements, "missing_dataset")
    if family == "post_earnings_drift_long":
        require("earnings_events_pti_v1" in requirements, "missing_dataset")
    require(type(result["rationale"]) is str and 1 <= len(result["rationale"]) <= 2000, "parameter_out_of_range")
    supplied = result.pop("strategy_sha256", None)
    result = sealed(result, "strategy_sha256")
    require(supplied is None or supplied == result["strategy_sha256"], "hash_mismatch")
    return result


def minimum_bars(spec):
    p = spec["parameters"]
    if spec["schema_version"] == 1:
        return p["slow_window"]
    if spec["family"] == "sma_cross_long":
        return p["slow_window"] + 1
    if spec["family"] in ("momentum_long", "mean_reversion_long"):
        return max(p["lookback_bars"], p.get("trend_filter_sma") or 0) + 1
    if spec["family"] == "breakout_long":
        return max(p["entry_channel_bars"], p["exit_channel_bars"]) + 1
    return 2


def costs(value):
    obj(value, [], ["commission_bps", "slippage_bps_per_side", "rounding"])
    result = {"commission_bps": "0", "slippage_bps_per_side": "0", "rounding": "half_even", **value}
    literal(result["rounding"], "half_even")
    for key in ["commission_bps", "slippage_bps_per_side"]:
        result[key] = norm(dec(result[key], high=Decimal("1000")))
    return result


def window(value):
    obj(value, ["start", "end_exclusive"])
    require(stamp(value["start"]) < stamp(value["end_exclusive"]), "invalid_window")
    return deepcopy(value)
