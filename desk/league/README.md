# Canonical development league

Run from `algo/desk` with the desk's existing Node dependencies and Python venv:

```sh
backtest/.venv/bin/python -m unittest discover -s tests/league -v
backtest/.venv/bin/python -m league run --state runtime/.state/league/example --config league/smoke-config.json --pause-after 3
backtest/.venv/bin/python -m league run --state runtime/.state/league/example --config league/smoke-config.json --pause-after 3
backtest/.venv/bin/python -m league verify --state runtime/.state/league/example
backtest/.venv/bin/python -m league export --state runtime/.state/league/example --out runtime/.state/league-learning/example
```

Repeat a bounded `run` invocation until its status is `completed`; verification and export require completion. A `STOP` file in the selected state directory pauses admissions. Resume with the same frozen configuration and source after the operator removes that file. Processes stay in the foreground. No service, scheduler, inference call or order path is installed.

`runtime.py` retains the tested generation, ancestry, mutation, selection, retirement, persistent budget and recovery behavior from the workstation overlay. Default participants are **host-generated SMA parameter genomes**, not model-written agents. The synthetic generator has one world family with multiple seeds. Positive-return and drawdown screening selects development population slots, not independently qualified strategies. Fitness uses native worker returns; the separate native walk-forward verdict remains authoritative for research qualification, including its unchanged synthetic-data refusal. Paper and orders stay off.

The live sibling desk supplies `runtime/campaign.mjs`, `runtime/author-fleet.mjs`, `backtest/walkforward.py`, `runtime/episodes.mjs`, contracts, worker and independent replay. Foreign snapshots and cached foreign backtest imports are refused. Source hashes bind every run and prevent resuming it under changed code. No snapshot desk files were copied into the canonical implementation.

There is one SQLite state store per league: `<state>/campaign.sqlite`. Namespaced `campaign_metadata` and `campaign_journal` tables belong to the existing campaign manager. Its admissions, normalized strategy hashes, accepted request hashes, native receipts and episode projections are authoritative. `league_*` tables contain scheduling, ancestry and checked selection projections in that same database. They cannot authorize trades or overwrite native outcomes. Standalone desk campaigns keep their existing storage format. Reconciliation reopens the same native campaign rather than submitting an unknown job again. The auxiliary manager lock database contains no research state.

Learning exports project the native episode decision IDs and attach ancestry/world grouping. They are derived files, not another writable episode ledger. Losses and unresolved outcomes are retained. Shared ancestry or world families still prevent an independent training split. The optional CPU score regressor remains a small testable baseline, not main-model training or financial-edge evidence.

The bridge's `runLeagueCampaign({plan,stateDir,fleet})` can resolve an existing frozen fleet attempt through `candidatesFromFleet`. `fleet` contains `stateDir`, `campaignId` and numeric `attempt`. Its normalized rule hash must match the requested genome. `CampaignEvaluator.execute` accepts this same reference as `candidate.fleet`. This reads the existing fleet registry and preserves its actual model/fixture provenance; it does not invoke authors or invent authorship for mutations. Default population generation remains host-owned.

All run data belongs under gitignored `runtime/.state/`. Workstation runs, including the failed `cpu-first-v1`, remain historical evidence in their original location. This consolidation does not deploy to the PC or train a main model.

## Cohorts and world families

Add `cohorts` to a config to assign disjoint ancestry cohorts and world families (`trend_v1`, `meanrev_v1`, `regime_v1`) before any job runs. Offspring inherit their parent's cohort; each cohort runs only on its own families; `cohorts.json` is sealed with the plan and a later change is refused (`cohort_assignment_changed`). With two or more cohorts the learning export can split; a single-family run still refuses training. See `../docs/fairness.md`.

## Real-author lanes

`league/authors.mjs` runs one bounded author generation through the existing fleet. `league/author-lanes.json` binds each role to one explicit adapter and model:

- `author_codex`: `cli_codex`
- `author_claude`: `cli_claude`
- `author_hermes`: `localhost` (Ollama)
- `author_pi`: `fixture`

Every author gets the same frozen round view, the same token budget and the same attempt cap (`max_attempts_per_author`).

- CLI lanes need `TRADING_DESK_ALLOW_CLI_AUTHOR=1`. Without it they are recorded as `disabled` in `candidates/<campaign>/generation.json` and never called.
- An author that is unavailable or times out mid-run is logged as an `unavailable` or `timed_out` attempt, which counts toward N. Its lane closes for the rest of the run. Nothing replaces it.
- Every call appends a hash-chained line to `candidates/<campaign>/calls.jsonl`, with elapsed time, tokens and `cost_usd` (computed only when a lane declares `price_per_mtok`).
- League genomes produced by host mutation stay `strategy_genome` and are never model authors.

```sh
node league/authors.mjs --campaign-id <id> [--state-dir <dir>] [--lanes <file>]
```
