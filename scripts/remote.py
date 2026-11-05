#!/usr/bin/env python3
"""Remote helper for 2017 Mac setup: DeepSeek + 6 Agents + Alpaca Paper + Full US Market Universe + Clipboard Auto-Capture."""
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _cand in (
    ROOT / "desk" / "runtime" / ".state" / "shadow" / ".venv" / "bin" / "python",
    ROOT / "desk" / "backtest" / ".venv" / "bin" / "python",
):
    if _cand.exists():
        if Path(sys.executable).resolve() != _cand.resolve():
            os.execv(str(_cand), [str(_cand), str(Path(__file__).resolve())] + sys.argv[1:])
        break

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import company_data
import market_universe
import onboarding
import options_short_engine
import replay_workbench
import runner
import shadow
import terminal_state


DEFAULT_EXPANDED_SYMBOLS = [
    "SPY", "QQQ", "NVDA", "TSLA", "AAPL", "MSFT", "META", "AMZN", "AMD", "PLTR", "GOOGL", "COIN"
]


def free_port_8765():
    try:
        runner._cleanup_previous_runner()
    except Exception:
        pass
    try:
        out = subprocess.check_output(["lsof", "-ti", "tcp:8765"], text=True).strip()
        for pid_str in out.splitlines():
            pid = int(pid_str.strip())
            if pid != os.getpid():
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
        time.sleep(0.5)
    except Exception:
        pass


def configure_deepseek_agents_and_market():
    print("[1/4] Assigning all 6 ADW agents to DeepSeek (deepseek-chat + deepseek-reasoner)...")
    terminal_state.save_deepseek({"apply_all_agents": True})

    print("[2/4] Expanding active market universe & warming up live US stock quotes/candles...")
    cfg = runner.read(runner.CONFIG) if runner.CONFIG.exists() else {"mode": "shadow"}
    current = cfg.get("watchlist") or ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]
    merged = list(dict.fromkeys(list(current) + DEFAULT_EXPANDED_SYMBOLS))[:12]
    shadow.save(
        runner.CONFIG,
        {
            "mode": cfg.get("mode", "shadow"),
            "watchlist": merged[:5],
            "universe_watchlist": merged,
            "news_sources": [
                "yahoo_finance",
                "yahoo_finance_rss",
                "bbc_business_rss",
                "alpaca_news_when_connected",
            ],
        },
    )
    try:
        values = []
        for sym in merged[:8]:
            values.append(company_data.collect(sym))
        shadow.save(
            runner.STATE / "market.json",
            {"observed_at": runner.stamp(), "companies": values, "refresh_seconds": 3600},
        )
        runner.collect_news()
    except Exception as exc:
        print(f"    Market warm-up warning: {exc}")
    print(f"    Active live symbols ({len(merged)}): {', '.join(merged)}")


def get_clipboard() -> str:
    try:
        return subprocess.check_output(["pbpaste"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def start_server_background():
    free_port_8765()
    print("[3/4] Starting Evan Algo live server & shadow engine on http://127.0.0.1:8765 ...")
    try:
        replay_workbench.ensure_seed()
    except Exception:
        pass
    server = runner.open_server(8765, allow_fallback=True)
    server.daemon_threads = True
    stop = threading.Event()
    runner.refresh_sources(stop, onboarding.load_profile() or onboarding.defaults())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Also launch background shadow worker so live candles/signals stream continuously
    run_dir = runner.STATE / "runs" / "live"
    command = [
        str(shadow.VENV / "bin" / "python"),
        str(ROOT / "scripts" / "shadow.py"),
        "watch",
        "--run-dir",
        str(run_dir),
        "--activity",
        str(runner.ACTIVITY),
    ]
    try:
        log = (runner.STATE / "algo.log").open("w")
        child = subprocess.Popen(
            command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        shadow.save(
            runner.STATE / "runner.json",
            {
                "state": "running",
                "mode": "shadow",
                "pid": os.getpid(),
                "child_pid": child.pid,
                "started_at": runner.stamp(),
                "run_dir": str(run_dir),
                "url": f"http://127.0.0.1:{server.server_port}",
                "live_orders": False,
            },
        )
    except Exception as exc:
        print(f"    Shadow worker note: {exc}")
    print(f"    Server LIVE at http://127.0.0.1:{server.server_port}")
    return server


def open_url(url: str):
    try:
        subprocess.run(["open", "-a", "Google Chrome", url], check=False)
    except Exception:
        subprocess.run(["open", url], check=False)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if cmd == "--dry-run":
        print("DRY RUN OK:", len(market_universe.STOCK_DIRECTORY), "stocks in directory")
        return
    if cmd == "--init":
        configure_deepseek_agents_and_market()
        return
    if cmd == "open-deepseek":
        open_url("https://platform.deepseek.com/api_keys")
        return
    if cmd == "open-alpaca":
        open_url("https://app.alpaca.markets/paper/dashboard/overview")
        return
    if cmd == "open-tets":
        open_url("http://127.0.0.1:8765")
        return

    configure_deepseek_agents_and_market()
    start_server_background()
    open_url("https://platform.deepseek.com/api_keys")

    print("[4/4] Clipboard Auto-Capture ACTIVE! Copy DeepSeek key (sk-...) or Alpaca key (PK... + secret) in Chrome.")
    last_clip = get_clipboard()
    pending_alpaca_key = None

    if re.match(r"^sk-[A-Za-z0-9_-]{20,}$", last_clip):
        res = terminal_state.save_deepseek({"api_key": last_clip, "apply_all_agents": True})
        print(f"\n=== [OK] DEEPSEEK API KEY SAVED ({res.get('key_hint')}) + 6/6 AGENTS + HERMES WIRED ===")

    try:
        while True:
            clip = get_clipboard()
            if clip and clip != last_clip:
                last_clip = clip
                m_ds = re.search(r"(sk-[A-Za-z0-9_-]{20,})", clip)
                if m_ds:
                    ds_key = m_ds.group(1)
                    res = terminal_state.save_deepseek({"api_key": ds_key, "apply_all_agents": True})
                    print(f"\n=== [OK] DEEPSEEK API KEY SAVED ({res.get('key_hint')}) + ALL 6 AGENTS + HERMES WIRED ===")
                    sys.stdout.flush()
                    continue

                m_pk = re.search(r"\b((?:PK|AK)[A-Z0-9]{14,32})\b", clip)
                if m_pk:
                    pending_alpaca_key = m_pk.group(1)
                    print(f"\n=== [OK] ALPACA KEY ID CAPTURED ({pending_alpaca_key[:4]}...{pending_alpaca_key[-4:]}) -> NOW COPY SECRET KEY ===")
                    sys.stdout.flush()
                    continue

                m_sec = re.search(r"\b([A-Za-z0-9]{32,52})\b", clip)
                if m_sec and not clip.startswith("sk-"):
                    secret_candidate = m_sec.group(1)
                    if pending_alpaca_key and secret_candidate != pending_alpaca_key:
                        res = terminal_state.save_alpaca_paper(
                            {
                                "api_key": pending_alpaca_key,
                                "secret_key": secret_candidate,
                                "mode": "paper",
                                "auto_execute": False,
                            }
                        )
                        acct = (res.get("connection") or {}).get("account") or {}
                        eq = acct.get("equity") or acct.get("buying_power") or "100000"
                        ok = (res.get("connection") or {}).get("ok")
                        print(f"\n=== [OK] ALPACA PAPER ACCOUNT SAVED (verified={ok}, equity=${eq}) ===")
                        sys.stdout.flush()
                        continue
            time.sleep(0.35)
    except KeyboardInterrupt:
        print("\nExiting remote helper.")


if __name__ == "__main__":
    main()
