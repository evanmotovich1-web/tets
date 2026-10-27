# tets

Private standalone algo test runner. Uses the reviewed Algo strategy/backtest engine with Yahoo live-data shadow observation, optional Alpaca paper execution and timestamped news. It does not install OpenTerminal, Next.js, Docker or Hermes.

## Install

From this repo's folder, run one command:

```sh
sh install.sh
```

It installs the dependencies, runs the model/strategy/capital/goal/liquidity wizard, checks the sources, and opens the running terminal with its settings side panel. On Macs with 8 GB RAM or less it automatically selects the low-memory profile. `TETS_PROFILE=normal sh install.sh` forces the normal profile; `sh install-2017-mac.sh` remains available for an explicit low-memory install.

On Mac use Monterey 12 or newer. Allow internet access and about 3 GB free disk. The scripts install Node/npm, uv, managed Python, locked Python/npm dependencies, Yahoo/yfinance and the pinned Alpaca paper helpers. They then run setup, a synthetic accounting self-test, source connection checks and the algo, opening a lightweight status page at **http://127.0.0.1:8765**. Ctrl-C stops the foreground run. The default run lasts at most eight hours; no background service is installed.

No system Python or Homebrew is required. If Git is missing, complete Apple's Command Line Tools Install dialog; the same command waits for it. Normal mode uses a 3 GB Node heap default and more package concurrency. The 2017 profile uses a 1.5 GB Node heap, low package concurrency and one numerical thread. The profile is remembered on restart. Neither profile downloads a local model. A heap cap is not a total memory limit, and physical 2017 performance remains to be measured there.

## Setup and keys

1. Choose **Yahoo shadow** (default, no account/API keys) or **Alpaca paper** (simulated broker execution).
2. Choose strategy, symbol, timeframe, allocated capital, entry budget, profit goal, loss stop, liquidity and optional AI connection. The lightest setup is rule-based. Goals are not forecasts. Shadow mode uses intraday rules, capital and entry budget; AI review, liquidity gating and loss-stop controls are paper-mode features.
3. For paper mode, setup opens [Alpaca](https://app.alpaca.markets/) so you can create a Paper Only account and generate **paper** keys. Enter both keys at hidden local prompts. Tets saves them in a private, gitignored file and checks the paper account endpoint. You can press Enter to connect later with `sh tets connect`.

API keys belong to your account: this repo cannot create an account or issue keys for you, and includes no shared credentials. Yahoo market data, Yahoo company headlines/RSS and BBC business RSS are configured automatically and need no keys. RSS keeps headlines, links and publication times, without downloading article bodies. Optional Alpaca stock/crypto news reuses your Alpaca keys; actual news access and delay depend on your entitlement. The terminal side panel shows the selected model, algorithm, capital, entry budget, profit goal, liquidity and actual AI state. Its main view shows observed prices, decisions, orders/fills and P&L, with broker paper results distinguished from assumed virtual fills. It distinguishes connected, empty, unavailable and credentials-required states. Keys never appear in the status API or Git history.

Optional AI reviews use an existing signed-in Codex/Claude CLI or a separately installed local OpenAI-compatible model. Rule-based mode is fully included. No paid account, model credentials or model weights are bundled. Unavailable AI blocks an AI-reviewed paper entry; it never overrides risk controls. Headlines are recorded/displayed for context and never generate orders.

Yahoo shadow uses indicative prices and assumed virtual fills; it is not broker execution. Alpaca paper uses IEX completed bars and broker-reported paper orders/fills. There is no real-money mode and no automatic strategy promotion.

## Commands

```sh
sh tets start                  # run the selected mode and open local status
sh tets start --no-open        # same runner without opening a browser
sh tets start --seconds 60     # bounded one-minute test
sh tets setup                  # change mode/settings
sh tets connect                # enter/check Alpaca paper keys locally
sh tets doctor                 # check Yahoo bars, news and paper connection
sh tets doctor --offline       # installed package/profile check only
sh tets news                   # fetch current source-labeled headlines
sh tets status                 # read actual runner/activity/source snapshots
sh tets self-test              # synthetic engine/receipt check; no network/model
```

Use `TETS_PORT=8766 sh tets start` if 8765 is occupied; the runner never kills another process to take its port. A closed market or missing quote remains visible; neither is fabricated activity. Quitting paper mode stops new decisions; existing paper orders/positions remain at Alpaca.

For unattended installation without starting services or opening account pages:

```sh
TETS_MODE=shadow TETS_NO_START=1 TETS_NO_OPEN=1 sh install.sh
```

Runtime logs, profiles, keys, downloaded tools and all observations stay under ignored `desk/runtime/.state/`. Start each machine from source; do not copy virtual environments between Macs. `SOURCE-PROVENANCE.json` records the copied Algo source hashes. Original source is MIT; see `AGPL-NOTICE.md` for the backtesting dependency's license.
