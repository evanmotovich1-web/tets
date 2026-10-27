"""Phase 1: bind an agent-written spec to frozen Maverick development bars.

Not a qualification campaign. promoting stays false.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import json
import os
import sys

DESK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DESK))
from backtest.contracts import canonical, costs, sealed, strategy_spec, verify
from backtest.independent_replay import replay
from backtest.storage import load_dataset
from backtest.worker import Worker, provenance, read_receipt

PROOF = Path(os.environ['PHASE1_PROOF_DIR']).resolve()
SNAPSHOT = DESK / 'runtime/.state/maverick/snapshots/5f2f1e2b2f2759d2769a13306fdc281aad3763a56882440c5498fcd05caba931'


def save(name, value):
    path = PROOF / name
    data = canonical(value) + b'\n'
    path.write_bytes(data)
    return value


def main():
    status = json.loads((PROOF / 'author-status.json').read_text())
    if status.get('agent_written') is not True or status.get('available') is not True:
        raise SystemExit('author_unavailable')
    spec = strategy_spec(json.loads((PROOF / 'authored-spec.json').read_text()))
    if spec['strategy_sha256'] != status['strategy_sha256']:
        raise SystemExit('strategy_hash_mismatch')
    if spec['family'] != 'sma_cross_long' or spec['symbol'] != 'SPY':
        raise SystemExit('unexpected_spec')

    registry = SNAPSHOT / 'registry.json'
    dataset_id = next(iter(json.loads(registry.read_text())['datasets']))
    manifest, rows = load_dataset(registry, dataset_id, 2)
    mutated = deepcopy(spec)
    mutated.pop('strategy_sha256')
    mutated['parameters']['fast_window'] = 1 if spec['parameters']['fast_window'] != 1 else 2
    mutated['parameters']['slow_window'] = 2 if spec['parameters']['slow_window'] != 2 else 3
    mutated = strategy_spec(mutated)
    if mutated['strategy_sha256'] == spec['strategy_sha256']:
        raise SystemExit('mutation_identity_collision')

    cost_model = dict(commission_bps='10', slippage_bps_per_side='5', rounding='half_even')
    initial = '10000.000000'
    end = (datetime.fromisoformat(rows[-1]['bar_close_at'].replace('Z', '+00:00')).replace(
        hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    window = dict(start=rows[0]['bar_open_at'], end_exclusive=end)
    worker = Worker(registry, PROOF / 'trials/artifacts')
    trials = {}
    replays = {}
    for label, candidate in [('baseline', spec), ('window_mutation', mutated)]:
        request = sealed(dict(
            schema_version=1, kind='backtest_request', strategy_sha256=candidate['strategy_sha256'],
            manifest_sha256=manifest['manifest_sha256'], evaluation_window=window, initial_cash='10000',
            cost_model=costs(cost_model), engine=provenance(), seed=0), 'request_sha256')
        save(f'{label}-accepted-request.json', request)
        save(f'{label}-spec.json', candidate)
        reply = worker.dispatch(dict(
            op='run_backtest', strategy=candidate, dataset_id=dataset_id,
            evaluation_window=window, initial_cash=initial, cost_model=cost_model))
        if not reply['ok']:
            raise SystemExit(reply)
        receipt = reply['result']
        verify(receipt, 'receipt_sha256')
        if request != reply['request'] or receipt['request_sha256'] != request['request_sha256']:
            raise SystemExit('request_binding_mismatch')
        if read_receipt(worker.store, receipt['receipt_sha256']) != receipt:
            raise SystemExit('receipt_reread_mismatch')
        if receipt['qualification'] != 'research_only' or receipt['promoting'] is not False:
            raise SystemExit('promotion_leak')
        save(f'{label}-receipt.json', receipt)
        artifacts = {kind: worker.store.get(receipt['artifacts'][kind + '_sha256'])['rows'] for kind in ['orders', 'fills', 'equity']}
        independent = replay(candidate, rows, initial, cost_model, manifest['price_scale'], manifest['cash_scale'])
        for kind in ['orders', 'fills', 'equity']:
            if independent[kind] != artifacts[kind]:
                raise SystemExit(f'independent_{kind}_mismatch')
        for field, value in independent['metrics'].items():
            if field == 'regulatory_fee_total' and field not in receipt['metrics']:
                if Decimal(value) != 0:
                    raise SystemExit('regulatory_fee_mismatch')
            elif value != receipt['metrics'][field]:
                raise SystemExit(f'metric_mismatch:{field}')
        save(f'{label}-independent-replay.json', independent)
        trials[label] = dict(
            request_sha256=request['request_sha256'], receipt_sha256=receipt['receipt_sha256'],
            strategy_sha256=candidate['strategy_sha256'], fills=len(artifacts['fills']),
            orders=len(artifacts['orders']), metrics=receipt['metrics'],
            independent_match=True, request_binding_match=True)
        replays[label] = independent

    base, mut = replays['baseline'], replays['window_mutation']
    if not base['fills'] or not mut['fills']:
        raise SystemExit('no_fills')
    if [f['executed_at'] for f in base['fills']] == [f['executed_at'] for f in mut['fills']] and \
       [f['quantity'] for f in base['fills']] == [f['quantity'] for f in mut['fills']]:
        raise SystemExit('mutation_did_not_change_fills')

    summary = dict(
        schema_version=1, kind='phase1_proof', agent_written_proven=True,
        authorship='cli_codex', author_status=status, research_only=True, promoting=False,
        data=dict(
            bars=len(rows), first=rows[0]['bar_open_at'], last=rows[-1]['bar_close_at'],
            data_sha256=manifest['data_sha256'], manifest_sha256=manifest['manifest_sha256'],
            dataset_id=dataset_id, point_in_time_verified=False, classification=manifest['classification'],
            partition=manifest['partition']),
        trials=trials,
        mutation=dict(
            kind='sma_window_change',
            baseline_windows=spec['parameters'],
            mutated_windows=mutated['parameters'],
            baseline_fills=len(base['fills']),
            mutated_fills=len(mut['fills']),
            fill_times_changed=[f['executed_at'] for f in base['fills']] != [f['executed_at'] for f in mut['fills']]),
        prompt_sha256=status['prompt_sha256'],
        provenance=status['provenance'])
    save('verification.json', summary)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
