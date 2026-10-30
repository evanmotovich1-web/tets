"""Bounded, local projections for the research terminal; never starts research or inference."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading

import options_short_engine as engine

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'desk/runtime/.state'
MODEL_LOCK = threading.Lock()


def read(path):
    try:
        if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            return None
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def models():
    path = STATE / 'harness2/model_roster.json'
    saved = read(path)
    return dict(state='saved' if saved else 'defaults', updated_at=(saved or {}).get('updated_at'),
                roster=engine.load_sticky_model_roster(path), providers=list(engine.SUPPORTED_PROVIDERS),
                defaults=engine.DEFAULT_PROVIDER_MODELS,
                provider_connections=engine.load_provider_connections(),
                note='Assignments are configuration, not evidence that these models have run.')


def save_models(value):
    if not isinstance(value, dict) or set(value) != {'assignments'}:
        raise ValueError('Expected model assignments only')
    assignments = value['assignments']
    valid = {r['agent_id'] for r in engine.DEFAULT_AGENTIC_TRADER_ROSTER}
    if not isinstance(assignments, dict) or not assignments or not set(assignments) <= valid:
        raise ValueError('Choose a known research role')
    for model in assignments.values():
        if not isinstance(model, str) or not 1 <= len(model) <= 160 or any(ord(c) < 32 for c in model):
            raise ValueError('Enter a provider and model ID')
        engine.resolve_provider_model(model)
    path = STATE / 'harness2/model_roster.json'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with MODEL_LOCK:
        if path.is_symlink():
            raise ValueError('Invalid model settings file')
        fd, name = tempfile.mkstemp(prefix='.models-', dir=path.parent)
        os.close(fd)
        temp = Path(name)
        try:
            if path.exists():
                temp.write_bytes(path.read_bytes())
            engine.configure_sticky_models(state_path=temp, assignments=[k+'='+v for k,v in assignments.items()])
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return models()


def research():
    paths = [STATE/'tets/adw_latest_run.json', STATE/'harness2/adw_latest_run.json']
    run = next((value for path in paths if (value := read(path)) and
                value.get('kind') == 'algo_harness2_adw_pipeline_run' and
                value.get('live_orders') is False and value.get('broker_orders') is False), None)
    if not run:
        return dict(state='unavailable', message='No saved research simulation on this Mac.', accounts=[], phases=[])
    # These CLI phases run local Python calculations. Model IDs label assignments; they are not inference receipts.
    fleet = run.get('shadow_fleet') or {}
    return dict(state='saved_simulation', created_at=run.get('created_at'), symbol=run.get('symbol'),
                dataset=run.get('dataset'), data_basis='Historical or fallback input; provenance unavailable in this saved run.' if not run.get('dataset') else 'See saved dataset provenance.',
                model_execution='No model inference receipt in this workflow. Model names are assignments.',
                options_basis='Option simulations can use theoretical prices; these are not broker fills.',
                phases=[{k:p.get(k) for k in ('phase_id','title','gate_passed','gate_check','tool_calls')} for p in run.get('phases',[])[:10]],
                accounts=fleet.get('accounts',[])[:24], initial_equity=fleet.get('total_initial_equity'),
                final_equity=fleet.get('total_final_equity'), net_pnl=fleet.get('total_fleet_net_pnl'),
                daily_loss_stop_pct=fleet.get('daily_loss_stop_pct'),
                all_phases_passed=run.get('all_phases_passed'),
                jev_screening=(run.get('jev_decision_filter') or {}).get('screening'),
                sssf_trace=run.get('sssf_trace'),
                synced_triad_contract=run.get('synced_triad_contract'),
                maverick_mcp_telemetry=run.get('maverick_mcp_telemetry'),
                jev_decision_filter=run.get('jev_decision_filter'),
                paper_account_orders=run.get('paper_account_orders'))


def library():
    """Read the merged Algo library without importing its runtime or creating a database."""
    path = ROOT.parent/'algo/desk/runtime/.state/library/catalog.sqlite'
    if not path.is_file() or path.is_symlink():
        return dict(state='unavailable', message='No Algo experiment catalog on this Mac.', attempts=[])
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1)
        connection.execute('PRAGMA query_only=ON')
        connection.set_progress_handler(lambda: 1, 200000)
        counts = dict(attempts=connection.execute('SELECT count(*) FROM attempts').fetchone()[0],
                      strategies=connection.execute('SELECT count(*) FROM strategies').fetchone()[0])
        attempts=[]
        for payload, verified in connection.execute('SELECT payload,verified FROM attempts ORDER BY rowid DESC LIMIT 30'):
            if len(payload)>1024*1024:continue
            item=json.loads(payload)
            attempts.append({**{k:item.get(k) for k in ('id','status','classification','recorded_at','metrics')},
                             'author':{k:(item.get('author') or {}).get(k) for k in ('model_id','provider','agent_written')},
                             'catalog_verified':bool(verified)})
        return dict(state='available', counts=counts, attempts=attempts,
                    note='Catalog attempts include failures and repeated strategies; they are not independent profitable strategies.')
    except (sqlite3.Error, ValueError, OSError):
        return dict(state='unavailable',message='The experiment catalog could not be read.', attempts=[])
    finally:
        if connection:connection.close()


def broker():
    path = STATE / 'harness2/broker_allocation.json'
    return engine.load_broker_allocation(path)


def save_broker(value):
    if not isinstance(value, dict) or not value:
        raise ValueError('Expected broker allocation settings')
    path = STATE / 'harness2/broker_allocation.json'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with MODEL_LOCK:
        if path.is_symlink():
            raise ValueError('Invalid broker settings file')
        return engine.save_broker_allocation(value, state_path=path)


def alpaca_paper():
    cred_path = STATE / 'paper/credentials.json'
    has_keys = False
    key_hint = None
    try:
        if cred_path.is_file() and not cred_path.is_symlink() and not (cred_path.stat().st_mode & 0o077):
            raw = json.loads(cred_path.read_text())
            if isinstance(raw, dict) and raw.get('api_key') and raw.get('secret_key'):
                has_keys = True
                k = str(raw['api_key'])
                key_hint = (k[:4] + '••••' + k[-2:]) if len(k) >= 6 else 'PK••••'
    except (OSError, ValueError):
        pass
    conn = read(STATE / 'tets/paper-connection.json') or (read(STATE / 'tets/connections.json') or {}).get('alpaca_paper') or {}
    act = read(STATE / 'tets/activity.json') or {}
    acct = act.get('account') if isinstance(act.get('account'), dict) else {}
    perf = act.get('performance') if isinstance(act.get('performance'), dict) else {}
    cfg = read(STATE / 'tets/config.json') or {}
    state = conn.get('state') if has_keys else 'not_connected'
    if has_keys and not state:
        state = 'connected' if acct else 'keys_saved'
    return dict(
        state=state or 'not_connected',
        mode=cfg.get('mode') or 'shadow',
        broker='alpaca_paper',
        endpoint='https://paper-api.alpaca.markets',
        data_endpoint='https://data.alpaca.markets',
        has_api_keys=has_keys,
        key_hint=key_hint,
        checked_at=conn.get('checked_at'),
        equity_usd=acct.get('equity') or conn.get('equity'),
        cash_usd=acct.get('cash') or conn.get('cash'),
        buying_power_usd=acct.get('buying_power') or conn.get('buying_power'),
        today_pnl_usd=perf.get('strategy_price_pnl_usd'),
        live_orders=False,
    )


def save_alpaca_paper(value):
    if not isinstance(value, dict) or not value:
        raise ValueError('Expected Alpaca paper settings')
    api_key = str(value.get('api_key') or '').strip()
    secret_key = str(value.get('secret_key') or '').strip()
    mode = value.get('mode')
    if mode is not None and mode not in ('shadow', 'paper'):
        raise ValueError('Mode must be shadow or paper')
    if not api_key and not secret_key and mode is None:
        raise ValueError('Provide Alpaca paper API keys or mode')
    with MODEL_LOCK:
        if api_key or secret_key:
            if not (api_key and secret_key):
                raise ValueError('Both Alpaca paper API key and secret key are required')
            if len(api_key) > 160 or len(secret_key) > 160 or any(ord(c) < 33 for c in api_key + secret_key):
                raise ValueError('Invalid Alpaca paper key format')
            cred_path = STATE / 'paper/credentials.json'
            cred_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if cred_path.is_symlink():
                raise ValueError('Invalid credentials file')
            tmp_path = cred_path.with_suffix('.tmp')
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'api_key': api_key, 'secret_key': secret_key}, stream, indent=2)
                stream.write('\n')
            tmp_path.replace(cred_path)
            try:
                import runner
                conn = runner.check_paper({'api_key': api_key, 'secret_key': secret_key})
                conn_path = STATE / 'tets/paper-connection.json'
                conn_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                conn_path.write_text(json.dumps(conn, indent=2) + '\n')
            except Exception:
                pass
        if mode in ('shadow', 'paper'):
            cfg_path = STATE / 'tets/config.json'
            cfg_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            cur_cfg = read(cfg_path) or {'mode': 'shadow', 'watchlist': ['SPY', 'QQQ', 'NVDA', 'AAPL', 'MSFT'], 'news_sources': ['yahoo_finance', 'yahoo_finance_rss', 'bbc_business_rss', 'alpaca_news_when_connected']}
            cur_cfg['mode'] = mode
            cfg_path.write_text(json.dumps(cur_cfg, indent=2) + '\n')
    return alpaca_paper()


def deepseek():
    keys_path = STATE / 'harness2/provider_keys.json'
    roster_path = STATE / 'harness2/model_roster.json'
    has_key = False
    key_hint = None
    updated_at = None
    env_key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
    if env_key:
        has_key = True
        key_hint = (env_key[:5] + '••••' + env_key[-4:]) if len(env_key) >= 10 else 'sk-••••'
    try:
        if keys_path.is_file() and not keys_path.is_symlink():
            raw = json.loads(keys_path.read_text())
            ds = (raw.get('providers') or {}).get('deepseek') if isinstance(raw, dict) else None
            if isinstance(ds, dict) and ds.get('token'):
                has_key = True
                tok = str(ds['token'])
                key_hint = (tok[:5] + '••••' + tok[-4:]) if len(tok) >= 10 else 'sk-••••'
                updated_at = ds.get('updated_at')
    except Exception:
        pass
    roster = engine.load_sticky_model_roster(roster_path)
    deepseek_agents = [r['agent_id'] for r in roster if str(r.get('primary_model', '')).startswith('deepseek:')]
    return dict(
        provider='deepseek',
        endpoint='https://api.deepseek.com/v1',
        state='connected' if has_key else 'not_connected',
        has_api_key=has_key,
        key_hint=key_hint,
        updated_at=updated_at,
        deepseek_agent_count=len(deepseek_agents),
        total_agents=len(roster),
        all_agents_deepseek=len(deepseek_agents) == len(roster),
        models=['deepseek-chat', 'deepseek-reasoner', 'deepseek-v4.1-flash', 'deepseek-r1'],
        roster={r['agent_id']: r['primary_model'] for r in roster},
    )


def save_deepseek(value):
    if not isinstance(value, dict):
        raise ValueError('Expected DeepSeek configuration payload')
    api_key = str(value.get('api_key') or '').strip()
    apply_all = bool(value.get('apply_all_agents', True))
    chat_model = str(value.get('chat_model') or 'deepseek-chat').strip()
    reasoner_model = str(value.get('reasoner_model') or 'deepseek-reasoner').strip()
    with MODEL_LOCK:
        if api_key:
            if len(api_key) > 200 or any(ord(c) < 33 for c in api_key):
                raise ValueError('Invalid DeepSeek API key format')
            engine.save_provider_key('deepseek', api_key, keys_path=STATE / 'harness2/provider_keys.json')
            # Also sync to Hermes Agent ~/.hermes/.env if present
            try:
                hermes_dir = Path.home() / '.hermes'
                if hermes_dir.is_dir():
                    env_file = hermes_dir / '.env'
                    lines = env_file.read_text().splitlines() if env_file.is_file() else []
                    lines = [ln for ln in lines if not ln.startswith(('DEEPSEEK_API_KEY=', 'OPENAI_API_KEY=', 'OPENAI_BASE_URL='))]
                    lines.append(f'DEEPSEEK_API_KEY={api_key}')
                    lines.append('OPENAI_BASE_URL=https://api.deepseek.com/v1')
                    lines.append(f'OPENAI_API_KEY={api_key}')
                    env_file.write_text('\n'.join(lines) + '\n')
                    os.chmod(env_file, 0o600)
            except Exception:
                pass
        if apply_all:
            roster_path = STATE / 'harness2/model_roster.json'
            roster_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            engine.configure_sticky_models(
                state_path=roster_path,
                assignments=[
                    f'stage_0_jev_router=deepseek:{chat_model}',
                    f'phase_1_scout_recon=deepseek:{chat_model}',
                    f'trader_price_structure=deepseek:{reasoner_model}',
                    f'trader_theta_mean_reversion=deepseek:{chat_model}',
                    f'phase_3_risk_auditor=deepseek:{reasoner_model}',
                    f'phase_4_fleet_dispatcher=deepseek:{chat_model}',
                ],
            )
            try:
                import onboarding
                prof = onboarding.load_profile() or onboarding.defaults()
                prof['ai']['provider'] = 'deepseek'
                prof['ai']['model'] = chat_model
                onboarding.save_profile(prof)
            except Exception:
                pass
    return deepseek()


def snapshot():
    import market_universe
    return dict(
        models=models(),
        broker=broker(),
        alpaca_paper=alpaca_paper(),
        deepseek=deepseek(),
        market_universe=market_universe.catalog(),
        research=research(),
        library=library(),
    )
