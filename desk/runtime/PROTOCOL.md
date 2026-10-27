# Runtime integration handoff

The fixture sections below describe the legacy v1 cycle. Spec v2 and the newer research components are documented in `../docs/` and `../README.md`.

## Entry points

From `desk/`, install the locked JavaScript dependencies with `npm ci --ignore-scripts --no-audit --no-fund`, then run:

```text
npm run test:runtime
npm run tick -- --event-id fixture:001 --provider fixture --state-dir /tmp/trading-desk-fixture
```

The Python worker/environment is Terra's separate installation boundary. See `../backtest/PROTOCOL.md`. The runtime uses only the fixed app-local `backtest/.venv/bin/python -m backtest.worker` command with an argument array, no shell. Host-selected registry/artifact paths are passed through a restricted environment, never model arguments.

Default state is `runtime/.state/`, ignored by `runtime/.gitignore`. A user-supplied state directory must be private and outside version control. `agent.sqlite` is Pi Durable's authoritative storage. It contains jobs, accepted model outputs, budgets, append-only trial events, checkpoints, and saved research cards. There is no second database or cross-database atomicity claim. Artifacts and worker replies are content-addressed/rebuildable execution evidence, not authoritative experiment ledgers.

`runTick(options)` in `tick.mjs` returns a job snapshot and cards. `DeskState` in `workflow.mjs` is the authoritative session document. `schemas.mjs` exports version-one job, generation, and experiment-event schemas. Job writes validate the strict root schema. Role/tool argument schemas live in `contracts.mjs`; executable strategy semantics remain worker-owned.

A completed fixture has final equity `1005.40`, commissions `2.30`, two fills and one closed trade. Cards and receipts always say `research_only` and `promoting:false`.

## Provenance and Node compatibility

The inspected news source is `research/trading-desk/upstream/pi-durable-news-agent` at `273b79bb8d3d8907cae9fbd6abd3da163ee2a343`. This implementation reuses its API patterns, not its news roles or workspace installation:

- `apps/shared/packages/durable-agent/src/harness.ts`: `Harness.open` with SQLite and a single-writer lock.
- `apps/shared/packages/durable-agent/src/tools.ts`: `hook(ToolTask, {beforeTool})` authorization before execution.
- `apps/dna-server-api/src/news/sweep-task.ts`: `defineTask`, transactional documents/checkpoints, and persisted retry phases.

All three runtime packages (`pi-durable`, `pi-ai`, Chord) are pinned to `1.1.0` in the application lock. Registry metadata and installed package manifests declare Node `>=22.19.0`. The upstream *workspace* declares Node `>=24`. Those are different constraints. This separate app declares `>=22.19.0`; actual Pi Durable 1.1.0 SQLite open/root/close, workflow, hook, and crash-recovery tests passed on the available Node `22.23.2`. No global Node installation was changed, no dependency was downgraded, and upstream workspace compatibility on Node 22 is not claimed. Node's SQLite experimental warning remains visible. The integrator should explicitly accept this evidenced app-specific engine floor rather than report an upstream Node-24 run.

The SDK's README and installed declarations were inspected before implementation. Ordinary Pi transcript persistence is not used as a substitute for durable tasks. Model adapters run inside application-defined Pi Durable task phases, rather than native generation turns. Accepted outputs, intent reservations, tool results and checkpoint advancement are committed through Pi Durable. Role extensions also install actual Pi Durable `beforeTool` hooks; a negative test exercises an offered rogue tool through the real built-in ToolTask. The custom adapter dispatcher invokes the same allowlist function before schema validation or handler execution.

## Workflow and recovery

```text
admit/desk -> scout -> propose/author -> validate -> backtest/runner
 -> referee -> save_card -> done
```

The host registers a trial before inference. All accepted author proposals, including semantically invalid ones, are logged before worker validation. Model outputs are committed before their tools run. Pending accepted calls survive restart and are not regenerated.

A trigger is keyed by source, campaign and event ID. Its payload digest must match on replay. Job IDs additionally bind the workflow version and payload hash. An event with changed payload conflicts. An event with changed execution policy, model route, dependency lock or worker source snapshot also conflicts instead of silently resuming under different rules. Different event IDs with identical requests produce separate trials but the same deterministic worker receipt.

One process owns the state directory using an exclusive PID/hostname lock. A dead local PID's lock can be reclaimed. A malformed lock, foreign-host lock or possibly reused live PID fails closed and requires operator inspection.

Each phase has at most three transient attempts with persisted 1-second and 4-second delays. A tick checkpoints retry state and exits rather than installing a scheduler. Repeating the event resumes it after the persisted deadline. The tick budget is 60 seconds and operations are limited to 30 seconds. Model/tool/token caps are persisted. Tokens are reserved before inference; unknown usage never refunds the reservation. Authorization, validation and integrity failures are not automatically retried.

Worker intent is committed before spawning. Successful responses are atomically published to an operation-keyed reply cache. After a crash, the runtime first reconciles that cache. If a backtest reply was lost, it searches at most 5,000 content-addressed artifacts for a unique receipt matching the accepted strategy, manifest, window, cash and costs, then has the worker re-verify referenced artifacts. Ambiguous matches fail closed. It does not independently construct a backtest request hash.

If no completed receipt can be found after uncertain backtest execution, the job remains `unknown`; later ticks reconcile but do not blindly execute it again. An uncertain non-fixture model call also remains `unknown` without automatic paid retry. Manual resolution of an unrecoverable unknown result is intentionally not implemented. No card is saved without a completed, freshly re-verified receipt and a referee record. Read-receipt verification bypasses caches so artifact removal/tampering cannot hide behind a cached read.

## Providers

Fixture mode is default, deterministic and network-free. No acceptance test calls a live model.

Optional localhost configuration:

```text
TRADING_DESK_MODEL_ID=<explicit-server-model-id>
TRADING_DESK_LOCAL_BASE_URL=http://127.0.0.1:11434/v1
TRADING_DESK_LOCAL_API_KEY=<optional-process-env-value>
npm run tick -- --event-id local:001 --provider localhost
```

Optional direct DeepSeek configuration:

```text
TRADING_DESK_MODEL_ID=<explicit-current-direct-model-id>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=<process-env-only>
TRADING_DESK_ALLOW_PAID_INFERENCE=1
npm run tick -- --event-id deepseek:001 --provider deepseek
```

These are environment-variable templates, not credentials. No model ID is silently supplied. Local endpoints must be explicit loopback URLs. DeepSeek must use the direct HTTPS host, not OpenRouter or a proxy. Redirects, embedded URL credentials, query strings, fragments, missing usage, oversized bodies and malformed tool output fail closed. There is no provider fallback. Keys and upstream error bodies are never included in durable error records or public receipts. Live localhost/DeepSeek inference remains unverified.

## Boundaries

Only registered dataset IDs can be used, and only host-configured research IDs are offered. Worker registry classification remains authoritative. The runner cannot change strategy, costs or evaluation policy through its tool arguments. No sealed-holdout API, broker/order tool, general shell/file tool, scheduler installer, detached process or training entry point exists.

This is not an OS sandbox. The child has restricted environment, bounded input/output, a timeout, and bounded worker datasets. Network denial, filesystem confinement, and a hard process-memory limit are not enforced by an OS sandbox and must not be claimed. Model agents cannot select executable paths, subprocess arguments or artifact locations. A separately reviewed OS isolation layer is needed before accepting arbitrary external code; this release accepts no such code.

Validation is synthetic research engineering, not evidence of trading edge, profitability, paper execution or live readiness.

---
Governed by AGENTS.md — see the repository rules.
