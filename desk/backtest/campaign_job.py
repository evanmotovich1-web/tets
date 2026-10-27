"""One foreground, bounded development job. Writes only its host-selected directory.

The native worker supplies receipts; the desk walk-forward evaluator is the judge.
This path has no author, brokerage, paper, holdout or validation operation.
"""
from contextlib import redirect_stdout
from pathlib import Path
from datetime import datetime, timezone
import os
import signal
import sys
import uuid

from .contracts import (Rejected, canonical, costs, loads, norm, obj, require, sealed,
                        sha, stamp, strategy_spec)
from .storage import load_dataset
from .walkforward import evaluate_rows
from .worker import Worker, provenance, read_receipt


class JobTimeout(Exception):
    pass


def run(message, root, registry):
    obj(message, ['strategy', 'dataset_id', 'evaluation_window', 'initial_cash', 'cost_model', 'screen', 'walkforward'])
    spec = strategy_spec(message['strategy'])
    require(spec['schema_version'] == 2, 'spec_v2_required')
    manifest, all_rows = load_dataset(registry, message['dataset_id'], 2)
    require(manifest['partition'] == 'development', 'dataset_access_denied')
    # The legacy receipt cost contract supports commission and slippage. This
    # campaign pins regulatory fees to zero explicitly, rather than omitting them.
    cost_model = costs(message['cost_model'])
    initial = norm(message['initial_cash'])
    request = sealed(dict(schema_version=1, kind='backtest_request', strategy_sha256=spec['strategy_sha256'],
        manifest_sha256=manifest['manifest_sha256'], evaluation_window=message['evaluation_window'],
        initial_cash=initial, cost_model=cost_model, engine=provenance(), seed=0), 'request_sha256')
    worker = Worker(registry, root/'artifacts')
    worker.store.put(request, 'request_sha256')  # immutable acceptance before execution
    reply = worker.dispatch(dict(op='run_backtest', strategy=spec, dataset_id=message['dataset_id'],
        evaluation_window=message['evaluation_window'], initial_cash=message['initial_cash'], cost_model=cost_model))
    require(reply['ok'] and reply['request'] == request, 'incomplete_receipt')
    receipt = read_receipt(worker.store, reply['result']['receipt_sha256'])
    require(receipt['request_sha256'] == request['request_sha256'], 'request_binding_mismatch')
    start, end = map(stamp, (message['evaluation_window']['start'], message['evaluation_window']['end_exclusive']))
    rows = [r for r in all_rows if start <= stamp(r['bar_open_at']) and stamp(r['bar_close_at']) < end]
    prereg = dict(cost_model={**cost_model, 'regulatory_fee_bps_on_sells': '0'}, pass_criteria=message['screen'])
    config = dict(initial_cash=message['initial_cash'], walkforward=message['walkforward'])
    evaluation = sealed(evaluate_rows(spec, prereg, config, manifest, rows), 'evaluation_sha256')
    return dict(status='completed', code=None, spec=spec, request=request, receipt=receipt,
        evaluation=evaluation, classification=manifest['classification'], manifest_sha256=manifest['manifest_sha256'])


def main():
    root = Path(os.environ['TRADING_DESK_CAMPAIGN_JOB']).resolve()
    registry = Path(os.environ['TRADING_DESK_REGISTRY']).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = lambda: datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    started = timestamp()
    def timeout(*_): raise JobTimeout()
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, int(os.environ['TRADING_DESK_JOB_TIMEOUT_MS'])/1000)
    try:
        raw = sys.stdin.buffer.read(65537)
        require(len(raw) <= 65536, 'message_too_large')
        with redirect_stdout(sys.stderr): value = run(loads(raw), root, registry)
    except JobTimeout:
        value = dict(status='timed_out', code='job_timeout')
    except Rejected as error:
        value = dict(status='rejected', code=error.code)
    except Exception:
        value = dict(status='failed', code='campaign_job_failed')
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    result = sealed(dict(schema_version=1, kind='campaign_job_result', qualification='research_only', promoting=False,
        execution=dict(pid=os.getpid(), started_at=started, finished_at=timestamp()),
        input_sha256=sha(loads(raw)), **value), 'job_result_sha256')
    temporary = root/('result.'+uuid.uuid4().hex+'.tmp')
    with temporary.open('xb') as stream:
        os.chmod(temporary, 0o600); stream.write(canonical(result)); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, root/'result.json')
    fd = os.open(root, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
    print(result['job_result_sha256'])


if __name__ == '__main__': main()
