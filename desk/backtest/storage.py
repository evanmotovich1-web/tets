"""Host-selected registry and content-addressed artifacts. No agent paths or URLs."""
from datetime import timedelta
from pathlib import Path
import os
import tempfile

from .contracts import (Rejected, canonical, dec, digest, identifier, integer, literal,
                        loads, obj, require, sha, sha_bytes, stamp, verify)

MAX_DATA_BYTES = 8 * 1024 * 1024
MAX_ROWS = 10000


def bounded_read(path, limit=MAX_DATA_BYTES):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(limit + 1)
    except (FileNotFoundError, NotADirectoryError):
        raise Rejected("missing_path") from None
    require(len(data) <= limit, "file_too_large")
    return data


def child(root, name):
    require(type(name) is str and name and not Path(name).is_absolute(), "invalid_registry_path")
    root = Path(root).resolve()
    path = (root / name).resolve()
    require(path.is_relative_to(root), "registry_path_escape")
    return path


def eligible(row, decision_at):
    return stamp(row["bar_close_at"]) <= stamp(decision_at) and stamp(row["available_at"]) <= stamp(decision_at)


def load_dataset(registry_path, dataset_id, strategy_version=1):
    identifier(dataset_id)
    registry_path = Path(registry_path)
    registry = loads(bounded_read(registry_path, 65536))
    obj(registry, ["schema_version", "kind", "datasets"])
    literal(registry["schema_version"], 1)
    literal(registry["kind"], "dataset_registry")
    require(type(registry["datasets"]) is dict, "invalid_registry")
    require(dataset_id in registry["datasets"], "unknown_dataset")
    entry = obj(registry["datasets"][dataset_id], ["classification", "partition", "manifest", "data", "manifest_sha256"])
    # Deny sealed/unknown entries before resolving or opening any dataset paths.
    require(entry["classification"] in ("synthetic", "historical_research", "historical_unverified") and
            entry["partition"] in ("development", "validation"), "dataset_access_denied")
    manifest = loads(bounded_read(child(registry_path.parent, entry["manifest"]), 65536))
    if strategy_version is None:
        if type(manifest) is dict and manifest.get("row_schema") == "earnings_events_pti_v1":
            symbols = manifest.get("symbols")
            require(type(symbols) is list and len(symbols) == 1, "single_symbol_required")
            return load_earnings_dataset(registry_path, dataset_id, symbols[0])
        strategy_version = 2 if type(manifest) is dict and manifest.get("calendar_id") == "nyse_regular" else 1
    obj(manifest, ["schema_version", "kind", "dataset_id", "classification", "partition", "source", "format",
                   "row_schema", "timeframe", "calendar_id", "price_basis", "currency", "price_scale",
                   "quantity_scale", "cash_scale", "symbols", "coverage", "row_count", "data_sha256", "manifest_sha256"], ["provenance"])
    verify(manifest, "manifest_sha256")
    require(manifest["manifest_sha256"] == digest(entry["manifest_sha256"]), "hash_mismatch")
    for key in ["classification", "partition"]:
        literal(manifest[key], entry[key])
    literal(manifest["dataset_id"], dataset_id)
    observed_at = None
    if entry["classification"] == "historical_unverified":
        from .maverick_import import validate_manifest
        observed_at = validate_manifest(manifest)
        require(strategy_version == 2, "unsupported_value")
    else:
        require("provenance" not in manifest, "unknown_field")
    expected_fields = {"schema_version": 1, "kind": "dataset_manifest", "format": "jsonl",
                       "row_schema": "ohlcv_pti_v1", "price_basis": "split_dividend_adjusted" if observed_at else "raw"}
    if strategy_version == 1:
        expected_fields.update(timeframe="1d", calendar_id="weekday_utc_0930_1600")
    else:
        require(manifest["timeframe"] in ("1m", "5m", "15m", "1h", "1d"), "timeframe_too_fast")
        literal(manifest["calendar_id"], "nyse_regular")
    for key, expected in expected_fields.items():
        literal(manifest[key], expected)
    require(type(manifest["currency"]) is str and len(manifest["currency"]) == 3 and
            all("A" <= char <= "Z" for char in manifest["currency"]), "invalid_currency")
    for key in ["price_scale", "cash_scale"]:
        integer(manifest[key], 0, 6)
    literal(manifest["quantity_scale"], 0)
    require(type(manifest["symbols"]) is list and len(manifest["symbols"]) == 1, "single_symbol_required")
    identifier(manifest["symbols"][0])
    source = obj(manifest["source"], ["provider", "source_reference", "license_reference", "retrieved_at", "snapshot_sha256"])
    for key in ["provider", "source_reference", "license_reference"]:
        require(type(source[key]) is str and 0 < len(source[key]) <= 2000, "invalid_provenance")
    stamp(source["retrieved_at"])
    digest(source["snapshot_sha256"])
    coverage = obj(manifest["coverage"], ["first_bar_open", "last_bar_close"])
    stamp(coverage["first_bar_open"])
    stamp(coverage["last_bar_close"])
    integer(manifest["row_count"], 2, MAX_ROWS)
    data = bounded_read(child(registry_path.parent, entry["data"]))
    require(sha_bytes(data) == digest(manifest["data_sha256"]), "hash_mismatch")
    require(data.endswith(b"\n"), "invalid_jsonl")
    lines = data.splitlines()
    require(len(lines) == manifest["row_count"], "row_count_mismatch")
    rows = []
    previous_day = None
    for line in lines:
        require(len(line) <= 4096, "row_too_large")
        row = loads(line)
        obj(row, ["symbol", "bar_open_at", "bar_close_at", "available_at", "open", "high", "low", "close", "volume"])
        literal(row["symbol"], manifest["symbols"][0])
        opened, closed, available = (stamp(row[k]) for k in ["bar_open_at", "bar_close_at", "available_at"])
        require(opened < closed and (closed <= available <= stamp(observed_at) if observed_at else closed == available), "delayed_or_revised_bar" if strategy_version == 1 else "future_data")
        if strategy_version == 1:
            require(opened.date() == closed.date() and opened.weekday() < 5 and
                    row["bar_open_at"][11:] == "09:30:00.000Z" and row["bar_close_at"][11:] == "16:00:00.000Z", "calendar_mismatch")
        if previous_day is not None and strategy_version == 1:
            expected = previous_day + timedelta(days=1)
            while expected.weekday() > 4:
                expected += timedelta(days=1)
            require(opened.date() == expected, "noncontiguous_or_duplicate_bars")
        previous_day = opened.date()
        values = {k: dec(row[k], positive=True) for k in ["open", "high", "low", "close"]}
        require(all(v.as_tuple().exponent >= -manifest["price_scale"] for v in values.values()), "price_precision_mismatch")
        require(values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"], "invalid_ohlc")
        dec(row["volume"])
        rows.append(row)
    require(coverage["first_bar_open"] == rows[0]["bar_open_at"] and coverage["last_bar_close"] == rows[-1]["bar_close_at"], "coverage_mismatch")
    if strategy_version == 2:
        from .engine import validate_v2_rows
        validate_v2_rows(rows, manifest["timeframe"], observed_at=observed_at)
    return manifest, rows


def load_earnings_dataset(registry_path, dataset_id, symbol):
    """Host-registered auxiliary data, with the same pre-open partition denial."""
    registry_path = Path(registry_path)
    registry = loads(bounded_read(registry_path, 65536))
    obj(registry, ["schema_version", "kind", "datasets"])
    literal(registry["schema_version"], 1)
    literal(registry["kind"], "dataset_registry")
    identifier(dataset_id)
    require(type(registry["datasets"]) is dict and dataset_id in registry["datasets"], "missing_dataset")
    entry = obj(registry["datasets"][dataset_id], ["classification", "partition", "manifest", "data", "manifest_sha256"])
    require(entry["classification"] in ("synthetic", "historical_research") and
            entry["partition"] in ("development", "validation"), "dataset_access_denied")
    manifest = loads(bounded_read(child(registry_path.parent, entry["manifest"]), 65536))
    obj(manifest, ["schema_version", "kind", "dataset_id", "classification", "partition", "source", "format",
                   "row_schema", "symbols", "coverage", "row_count", "data_sha256", "manifest_sha256"])
    verify(manifest, "manifest_sha256")
    require(manifest["manifest_sha256"] == digest(entry["manifest_sha256"]), "hash_mismatch")
    for key, expected in {"schema_version": 1, "kind": "dataset_manifest", "dataset_id": dataset_id,
                          "classification": entry["classification"], "partition": entry["partition"],
                          "format": "jsonl", "row_schema": "earnings_events_pti_v1", "symbols": [symbol]}.items():
        literal(manifest[key], expected)
    source = obj(manifest["source"], ["provider", "source_reference", "license_reference", "retrieved_at", "snapshot_sha256"])
    for key in ("provider", "source_reference", "license_reference"):
        require(type(source[key]) is str and 0 < len(source[key]) <= 2000, "invalid_provenance")
    stamp(source["retrieved_at"])
    digest(source["snapshot_sha256"])
    integer(manifest["row_count"], 1, MAX_ROWS)
    data = bounded_read(child(registry_path.parent, entry["data"]))
    require(sha_bytes(data) == digest(manifest["data_sha256"]), "hash_mismatch")
    require(data.endswith(b"\n"), "invalid_jsonl")
    lines = data.splitlines()
    require(len(lines) == manifest["row_count"] and all(len(line) <= 4096 for line in lines), "row_count_mismatch")
    from .engine import validate_earnings
    events = validate_earnings([loads(line) for line in lines], symbol)
    coverage = obj(manifest["coverage"], ["first_available_at", "last_available_at"])
    require(coverage == {"first_available_at": events[0]["available_at"], "last_available_at": events[-1]["available_at"]}, "coverage_mismatch")
    return manifest, events


class ArtifactStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, value, hash_field=None):
        key = verify(value, hash_field)[hash_field] if hash_field else sha(value)
        data = canonical(value)
        target = self.root / (key + ".json")
        # Atomic publication: interrupted staging files are never readable receipts.
        fd, temporary = tempfile.mkstemp(prefix=".staging-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                require(not target.is_symlink(), "artifact_path_escape")
                require(bounded_read(target) == data, "artifact_integrity_failure")
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(temporary)
        return key

    def get(self, key, hash_field=None):
        path = self.root / (digest(key) + ".json")
        require(not path.is_symlink(), "artifact_path_escape")
        data = bounded_read(path)
        value = loads(data)
        require(canonical(value) == data, "artifact_integrity_failure")
        actual = verify(value, hash_field)[hash_field] if hash_field else sha(value)
        require(actual == key, "artifact_integrity_failure")
        return value
