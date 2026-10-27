# Backtest worker handoff

The fixture sections below describe the legacy v1 cycle. Spec v2 and the newer research components are documented in `../docs/` and `../README.md`.

## Invocation

From `desk/`, use the fixed application-local executable:

```text
backtest/.venv/bin/python -m backtest.worker
```

The host must set `TRADING_DESK_ARTIFACT_DIR` to a controlled writable directory. `TRADING_DESK_REGISTRY` selects the trusted registry file. If omitted, the bundled synthetic fixture registry is used. No message can override these paths. Send exactly one JSON object and close stdin. Exit 0 means `ok:true`. Exit 1 still returns one structured JSON response. Diagnostics and engine warnings go to stderr.

Use a fixed argument array, no shell, a restricted environment, a 30-second timeout, and bounded stdout/stderr capture. Await exit and terminate/await a timed-out child. The worker bounds input to 64 KiB, registry/manifest to 64 KiB, and data to 8 MiB/10,000 bars. These are application checks, not an OS sandbox or hard process-memory limit. The host owns process isolation and single-writer enforcement.

Install reproducibly into this application only:

```text
python3 -m venv backtest/.venv
backtest/.venv/bin/python -m pip install -r requirements.lock
```

The initial environment was installed and tested on Python 3.14.7, macOS arm64. All installed dependency versions are pinned in `requirements.lock` and checked at execution. Other Python/platform combinations require validation. Python version and engine/adapter source hashes are bound into request identity.

## Operations and responses

All four operation argument objects reject additional properties. The transport object uses `op`; it does not accept runtime role/campaign/job fields.

| Operation | Arguments | Successful response fields |
|---|---|---|
| `validate_strategy` | `strategy` | `strategy` with expanded defaults and `strategy_sha256` |
| `read_manifest` | `dataset_id` | `manifest`, after registry policy and data-byte verification |
| `run_backtest` | `strategy`, `dataset_id`, `evaluation_window`, `initial_cash`, `cost_model` | `request`, `result` |
| `read_receipt` | `receipt_sha256` | `result`, after request and artifact-reference verification |

Every response contains `schema_version:1`, `kind:"worker_response"`, `ok`, and `events`. Failure responses contain a non-promoting result with `metrics:null`. Pre-request rejection has `request_sha256:null`. An engine failure after request acceptance retains its request hash. Host code must inspect `result.status`, not merely `ok`, when retrieving previously failed receipts.

Example request: `backtest/fixtures/request.json`. The expected final equity is `1005.40`, commissions `2.30`, two fills, and one closed trade. There is also one cancelled terminal buy intent. The bundled data is wholly synthetic, not market data.

Structural schemas are in `backtest/contracts/*.schema.json`. `contracts.py` is the authoritative semantic validator. `schemas.py` exports the structural definitions and tests ensure exported files match. For an author schema tool, return `strategy-spec.schema.json`; passing its structural checks alone does not accept a strategy.

## Precision and compatibility restrictions

Only `sma_cross_long`, one symbol, fixed positive **whole-unit** quantity, no leverage/shorting/pyramiding. Fractional quantities fail explicitly rather than becoming engine relative-sizing orders. Fast/slow windows are 1–199/2–200 with slow greater than fast. Cash is positive and at most 1 billion. Costs are each 0–1000 bps.

Only `1d`, raw prices, `weekday_utc_0930_1600`, and one currency are implemented. This fixture calendar requires every weekday and has no holidays, DST, corporate actions, revisions, or delayed bars. An exchange calendar is not silently inferred. Price/cash scales are 0–6 and price scale must not exceed cash scale. Quantity scale is zero. Unsupported metadata fails closed.

A window starts exactly at a registered bar open, contains full finalized bars, and supplies its own warmup. No pre-window padding or prior observations are loaded into the engine. `end_exclusive` may fall between completed bars, but never inside a bar, and cannot exceed midnight following the dataset's final day.

Strategy decisions use only the engine's current historical close slice. Execution is at the following opening, with rounded per-side slippage and commission. Native engine arithmetic and SMA calculation use floating point. Actual engine fills are reconciled to a Decimal ledger, and disagreement prevents completion. This is not arbitrary-precision engine accounting.

Persisted cash, executed prices and monetary metrics use their declared fixed scale (`1005.40`). Other decimal inputs expand to minimal decimal strings before hashing (`1000.00` becomes `1000`). Ratios are rounded half-even to 12 places. This convention is deterministic. RFC 8785 canonicalization is provided by pinned `rfc8785`, not an approximate sorted-JSON implementation.

## Artifacts and trial ownership

Immutable files are `<digest>.json`. Strategies, manifests, requests and receipts hash the canonical object excluding their own hash field. Orders/fills/equity artifacts hash their full object. Publication uses fsync plus an atomic hard-link from a staging file. Existing bytes under the same digest must be identical. Interrupted staging files cannot be read as receipts. Startup does not delete them automatically.

Receipt identity excludes wall clock, attempt UUID, machine paths and secrets. It binds dataset timestamps and provenance transitively through the manifest, since those describe the evaluated data. Source and lock changes intentionally change request identity.

`events` and `artifacts/attempts/` are **worker execution evidence only**, not a second authoritative experiment ledger. Each invocation gets an attempt UUID. Even malformed messages have an input digest and attempt outcome. Valid JSON proposal submissions, including invalid specs and duplicates, emit `proposal_recorded` with their proposal digest. These evidence records are outside deterministic receipt hashes.

**Sol must register the trial and persist the full proposed strategy/model provenance in Pi Durable before invoking the worker**, then transactionally record returned hashes/events and phase advancement. A changed proposal/dataset/window/cost is a new trial. Retries retain the runtime's trial ID. Identical worker artifacts are reused, while duplicate attempts remain recorded. Do not interpret worker attempt UUIDs as trial IDs or assume atomicity across artifact publication and Pi Durable transactions. Reconcile with `read_receipt` after an uncertain response before retrying.

The fallback qualification harness is unavailable. Completed receipts always contain `qualification:"research_only"`, `promoting:false`, and reason `qualification_harness_unavailable`. No receipt grants deployment, holdout access, paper orders, or live orders.

## MarketLab embed

`run_marketlab_study` is the only worker door into MarketLab. It does not replace the synthetic SMA fixture. If `MARKETLAB_ROOT` or `~/code/marketlab` is missing, the worker returns `marketlab_unavailable` and `promoting: false`. A MarketLab PASS is still `qualification: research_only`. Cap `n_seeds` at 5 from this desk. Do not treat the study as live or paper trading.

## Validation


```text
backtest/.venv/bin/python -m unittest discover -s tests/backtest -v
```

No network, credentials, broker tools, generated-code execution, services, or background processes are used by these tests. The direct worker subprocess tests use `subprocess.run(..., timeout=30)` and await completion.

---
Governed by AGENTS.md — see the repository rules.
