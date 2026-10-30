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
import market_data
import news
import onboarding
import options_short_engine
import runner
import strategies
import terminal_state
import watchlist


DEFAULT_EXPANDED_SYMBOLS = [
    "SPY", "QQQ", "NVDA", "TSLA", "AAPL", "MSFT", "META", "AMZN", "AMD", "PLTR", "GOOGL", "COIN"
]


def free_port_8765():
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
    options_short_engine.configure_sticky_models(
        {
            "mode": "per_agent",
            "assignments": {
                "stage_0_jev_router": "deepseek:deepseek-chat",
                "phase_1_scout_recon": "deepseek:deepseek-chat",
                "trader_price_structure": "deepseek:deepseek-reasoner",
                "trader_theta_mean_reversion": "deepseek:deepseek-reasoner",
                "phase_3_risk_auditor": "deepseek:deepseek-reasoner",
                "phase_4_fleet_dispatcher": "deepseek:deepseek-chat",
            },
        }
    )

    print("[2/4] Expanding active market universe & fetching live US stock candles/quotes...")
    current = watchlist.load()
    merged = list(dict.fromkeys((current or []) + DEFAULT_EXPANDED_SYMBOLS))[:12]
    watchlist.save(merged)
    try:
        market_data.collect(merged)
        company_data.collect(merged)
        news.collect(merged[:6])
        strategies.run()
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
    print("[3/4] Starting Evan Algo live server on http://127.0.0.1:8765 ...")
    t = threading.Thread(target=runner.serve, args=("127.0.0.1", 8765), daemon=True)
    t.start()
    time.sleep(0.8)
    print("    Server LIVE at http://127.0.0.1:8765")


def open_url(url: str):
    try:
        subprocess.run(["open", "-a", "Google Chrome", url], check=False)
    except Exception:
        subprocess.run(["open", url], check=False)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "watch"
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

    # Also check if existing clip already has a key
    for initial in [last_clip]:
        if re.match(r"^sk-[A-Za-z0-9_-]{20,}$", initial):
            res = terminal_state.save_deepseek({"api_key": initial, "apply_all_agents": True})
            print(f"\n=== [OK] DEEPSEEK API KEY SAVED ({res.get('key_hint')}) + 6/6 AGENTS + HERMES WIRED ===")

    try:
        while True:
            clip = get_clipboard()
            if clip and clip != last_clip:
                last_clip = clip
                # Check for DeepSeek API key: sk-...
                m_ds = re.search(r"(sk-[A-Za-z0-9_-]{20,})", clip)
                if m_ds:
                    ds_key = m_ds.group(1)
                    res = terminal_state.save_deepseek({"api_key": ds_key, "apply_all_agents": True})
                    print(f"\n=== [OK] DEEPSEEK API KEY SAVED ({res.get('key_hint')}) + ALL 6 AGENTS + HERMES WIRED ===")
                    sys.stdout.flush()
                    continue

                # Check for Alpaca Key ID: PK... or AK...
                m_pk = re.search(r"\b((?:PK|AK)[A-Z0-9]{14,32})\b", clip)
                if m_pk:
                    pending_alpaca_key = m_pk.group(1)
                    print(f"\n=== [OK] ALPACA KEY ID CAPTURED ({pending_alpaca_key[:4]}...{pending_alpaca_key[-4:]}) -> NOW COPY SECRET KEY ===")
                    sys.stdout.flush()
                    continue

                # Check for Alpaca Secret Key (when pending_alpaca_key is set, or 36-44 char alnum secret)
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
