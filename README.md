# Evan Algo

Private standalone algo test runner. Uses the reviewed Algo strategy/backtest engine with Yahoo live-data shadow observation, optional Alpaca paper execution and timestamped news. It does not install OpenTerminal, Next.js, Docker or Hermes.

Evan Algo opens as a full-width desktop workspace in dark and light blues, with four large choices: Live market, Research, Backtest replay, and Models & settings. Back and Home replace the sidebar. The navigation flow follows Tunza, while the colors and layout are adapted for a computer. Companies sit above the chart; news, virtual account details and observations follow in reading order. The Inter variable font is bundled locally under its SIL Open Font License, with system fallbacks and no external font request. Existing status, research, model-configuration and replay backends remain authoritative.

## Install

From this repo's folder, run one command:

```sh
sh install.sh
```

It installs the dependencies, runs the model/strategy/capital/goal/liquidity wizard, checks the sources, and opens the running terminal with its settings view. On Macs with 8 GB RAM or less it automatically selects the low-memory profile. `TETS_PROFILE=normal sh install.sh` forces the normal profile; `sh install-2017-mac.sh` remains available for an explicit low-memory install.

On Mac use Monterey 12 or newer. Allow internet access and about 3 GB free disk. The scripts install Node/npm, uv, managed Python, locked Python/npm dependencies, Yahoo/yfinance and the pinned Alpaca paper helpers. They then run setup, a synthetic accounting self-test, source connection checks and the algo, opening a lightweight status page at **http://127.0.0.1:8765**. Ctrl-C stops the foreground run. The default run lasts at most eight hours; no background service is installed.

No system Python or Homebrew is required. If Git is missing, complete Apple's Command Line Tools Install dialog; the same command waits for it. Normal mode uses a 3 GB Node heap default and more package concurrency. The 2017 profile uses a 1.5 GB Node heap, low package concurrency and one numerical thread. The profile is remembered on restart. Neither profile downloads a local model. A heap cap is not a total memory limit, and physical 2017 performance remains to be measured there.

## Setup and keys

1. Choose **Yahoo shadow** (default, no account/API keys) or **Alpaca paper** (simulated broker execution).
2. Choose strategy, symbol, timeframe, allocated capital, entry budget, profit goal, loss stop, liquidity and optional AI connection. The lightest setup is rule-based. Goals are not forecasts. Shadow mode uses intraday rules, capital and entry budget; AI review, liquidity gating and loss-stop controls are paper-mode features.
3. For paper mode, setup opens [Alpaca](https://app.alpaca.markets/) so you can create a Paper Only account and generate **paper** keys. Enter both keys at hidden local prompts. Tets saves them in a private, gitignored file and checks the paper account endpoint. You can press Enter to connect later with `sh tets connect`.

API keys belong to your account: this repo cannot create an account or issue keys for you, and includes no shared credentials. Yahoo market data, Yahoo company headlines/RSS and BBC business RSS are configured automatically and need no keys. RSS keeps headlines, links and publication times, without downloading article bodies. Optional Alpaca stock/crypto news reuses your Alpaca keys; actual news access and delay depend on your entitlement. The settings view shows the selected model, algorithm, capital, entry budget, profit goal, liquidity and actual AI state. Its main view shows observed prices, decisions, orders/fills and P&L, with broker paper results distinguished from assumed virtual fills. It distinguishes connected, empty, unavailable and credentials-required states. Keys never appear in the status API or Git history.

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
sh tets verify-hardware        # save actual hardware, runtime, backtest and HTTP proof; no keys
```

The same install command automatically chooses a free port if 8765 is occupied and prints/opens the actual URL. `TETS_PORT=8766 sh tets start` selects an explicit port; an occupied explicit port is refused. The runner never kills another process to take its port. A closed market or missing quote remains visible; neither is fabricated activity. Quitting paper mode stops new decisions; existing paper orders/positions remain at Alpaca.

## Shadow watchlist, signals and moving charts

Run `sh install.sh`, select **Yahoo shadow**, and enter up to five symbols when asked. The default list includes your primary symbol, AAPL and MSFT. The same command detects an 8 GB Mac and uses the older-Mac profile; `sh install-2017-mac.sh` explicitly selects it. Both install everything needed for this view, including the Yahoo feed. No chart library, local model, browser extension or API key is required. Requires macOS Monterey 12 or newer.

The terminal shows each company's observed price, data freshness, last signal and the symbol currently being checked. Select a company to see its completed candlesticks, BUY/SELL signal markers and latest quote line, or switch to the observed live-quote chart. “Follow scanner” follows actual company checks; manual selection pins a chart. Quotes refresh the display once per second when received. Completed bars are fetched sequentially in watchlist rounds, with a 30-second pause after each round. The UI does not invent price movement when a feed is quiet or the market is closed.

Every company uses the same selected canonical strategy rules, evaluated on new completed regular-session bars. A signal is an observed rule result, not an order or a forecast. Warmup candles never create historical trades. Assumed virtual fills require a later fresh quote and include modeled slippage. The capital is divided into separate virtual sleeves across the watchlist; total equity is shown separately from the selected company's equity. A sleeve too small for one share can show a BUY signal followed by a rejected virtual intent. The activity ledger records this reason rather than inventing a fill. This is a bounded rule scanner, not AI company research or automated selection of a winning strategy.

Change the saved list with `sh tets setup`, or override it for one run:

```sh
TETS_SYMBOLS=SPY,AAPL,MSFT sh tets start
```

The primary setup symbol is always included, and the total is capped at five. The runner shares one quote stream, keeps at most 600 warmup bars per company and displays the latest 120 candles/quotes. Full forward signals and observation provenance remain in the bounded local run journal. Missing/stale data, source errors and bar revisions remain visible. This watchlist applies to Yahoo shadow mode; Alpaca paper mode keeps its configured single-symbol execution.

Every install also runs the installed regression suite, including chart rendering logic without newer Safari abort helpers, signal timing, symbol routing and capital accounting. Intel installs explicitly select Monterey-compatible wheels, repair incompatible wheels from earlier installs and audit native dependencies' minimum macOS versions. Hardware evidence distinguishes native Intel from Rosetta. Intel execution on Apple Silicon does not establish physical 2017 or Monterey execution.

## Combined market terminal and Algo Lab replay

The workspace now separates **Market**, **Research runs**, **Models & setup** and **Algo Lab replay**. Market keeps the chart, watchlist, headlines and current virtual decisions together. Research runs reads the saved CLI fleet report and, when present, the adjacent Algo experiment catalog in read-only mode. Missing reports stay empty; simulated P&L keeps its actual sign. A saved fleet is a historical calculation, not twelve running broker accounts. Older reports without dataset provenance are labeled accordingly. The merged ADW Python workflow's model names are assignments, not inference receipts; its technical indicators are local calculations, and option prices can be theoretical.

Models & setup edits the canonical sticky roster used by `sh tets adw` and `sh tets models`. Saving an assignment starts no model, inference or trade. Existing provider restrictions remain enforced. The local save endpoint accepts only same-origin JSON and known role assignments. Paper entry-review settings remain separate from research role assignments.

`sh tets dashboard` reopens the UI and source refreshers over existing observations without starting another trading worker. This is useful if the browser/server has closed while an observation worker still owns its activity lock. The UI distinguishes an active observation writer from a stopped foreground runner. The usual one-command installer and both Mac profiles remain available; this UI adds no dependency.

The Market view uses the blue workspace design and starts with the observed live-quote chart. Company selection changes the price chart, company snapshot and company-specific headlines together. Quote times and stale states remain visible. Fundamentals refresh hourly; headlines refresh every minute and retain publisher timestamps. The feed is unofficial Yahoo indicative data, and provider delay is not guaranteed.

For companies, **estimated market cap** is snapshot shares outstanding multiplied by the last observed quote, with the share snapshot time shown. It is not an exchange-published live market-cap feed. If shares are unavailable, the UI shows a reported provider snapshot or an unavailable state. Funds such as SPY show reported fund assets instead of pretending to have a company market cap. Reported day volume is also a timestamped snapshot.

The **Algo Lab replay** tab connects to an existing verified historical report from the adjacent Algo checkout, or from `desk/runtime/.state/tets/replay/backtest-report.json` and `backtest-report.html`. The connector checks the report digest, simulated-order flags, independent accounting checks and matching embedded HTML data before serving it. It never starts OpenTerminal or adds its dependencies. On a Mac without a saved report, the tab says that no replay is available; previous machine data is not bundled into the installer. Historical playback and simulated backtest fills remain separate from current market observations and prospective virtual fills.

For unattended installation without starting services or opening account pages:

```sh
TETS_MODE=shadow TETS_NO_START=1 TETS_NO_OPEN=1 sh install.sh
```

Runtime logs, profiles, keys, downloaded tools and all observations stay under ignored `desk/runtime/.state/`. Start each machine from source; do not copy virtual environments between Macs. `SOURCE-PROVENANCE.json` records the copied Algo source hashes. Original source is MIT; see `AGPL-NOTICE.md` for the backtesting dependency's license.

## Backtesting does not need an API key

The backtest engine runs locally. Yahoo historical/forward observations and the free RSS adapters also work without keys. The synthetic `self-test` checks accounting and runtime health; it is not a historical profitability result. Select option 1 to install and run without entering any broker credential.

The optional Alpaca prompt is for simulated **broker execution**, account positions/orders and Alpaca data/news. You can configure the same paper-account key pair on your own computers. Those machines then share one simulated account and its limits; independent runners on the same symbol can conflict and pause on the paper ledger's position checks. Giving the same key to every user would give every user control of that same account. Free pricing does not make the key public. A shared data service would be a separate design with provider-authorized data access, not a bundled broker secret.

Every install now saves `desk/runtime/.state/tets/hardware-verification.json`: actual model, RAM, OS, native/translated architecture, Python/Node runtime architectures, a completed synthetic receipt and HTTP checks. `sh tets verify-hardware` refreshes this local evidence without broker keys or order submissions. A successful Rosetta run checks Intel code execution on Apple Silicon; it does not establish a physical 2017 Mac or Monterey run. MacBookAir7,2 covers both 2015 and 2017 Air models, so that identifier alone does not prove the year.

---
Governed by [AGENTS.md](AGENTS.md).
