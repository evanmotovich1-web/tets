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


def snapshot():
    return dict(models=models(), research=research(), library=library())
