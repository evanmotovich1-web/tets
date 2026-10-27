"""Frozen real-data proof setup, stage diagnostics and independent accounting audit.

No model, broker, paper or holdout reader. The Node phase owns author/worker calls.
"""
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import argparse
import json
import shutil
import sqlite3

from .contracts import canonical, sealed, verify, sha_bytes, Rejected
from .storage import load_dataset, ArtifactStore
from .worker import read_receipt
from .walkforward import CampaignRegistry, evaluate_rows
from .independent_replay import replay

APP=Path(__file__).resolve().parents[1]


def setup(root,development_registry,validation_registry):
    root=Path(root).resolve();assert root.is_relative_to(APP/'runtime/.state')
    assert not(root/'setup.json').exists(),'proof setup is immutable; reuse existing phases'
    result={};entries={}
    for partition,registry in [('development',development_registry),('validation',validation_registry)]:
        raw=json.loads(Path(registry).read_text());ids=list(raw['datasets']);assert len(ids)==1
        manifest,rows=load_dataset(registry,ids[0],2)
        assert manifest['partition']==partition and manifest['classification']=='historical_unverified'
        folder=root/'data'/partition;folder.mkdir(parents=True)
        (folder/'manifest.json').write_bytes(canonical(manifest)+b'\n');(folder/'data.jsonl').write_bytes(b''.join(canonical(r)+b'\n'for r in rows))
        entries[ids[0]]=dict(classification=manifest['classification'],partition=partition,manifest=partition+'/manifest.json',data=partition+'/data.jsonl',manifest_sha256=manifest['manifest_sha256'])
        result[partition]=manifest
    registry=root/'data/registry.json';registry.write_bytes(canonical(dict(schema_version=1,kind='dataset_registry',datasets=entries))+b'\n')
    result.update(campaign_id='maverick-real-author-proof-20261026',created_at=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'),registry=str(registry),
        research_scope=dict(symbol='SPY',timeframe='1d',family='sma_cross_long',quantity='1',order_type='market'),
        dataset_bindings={'SPY@1d':dict(development=result['development']['manifest_sha256'],validation=result['validation']['manifest_sha256'],holdout_id='not-designated-never-opened')})
    result['evaluation_window']=dict(start=result['development']['coverage']['first_bar_open'],end_exclusive='2024-03-29T00:00:00.000Z')
    (root/'setup.json').write_bytes(canonical(result)+b'\n')
    store=CampaignRegistry(root/'campaign.sqlite')
    try:
        for manifest in (result['development'],result['validation']):store.import_registered_dataset(registry,manifest['dataset_id'])
        campaign=store.create_campaign(sealed(dict(schema_version=1,kind='walkforward_campaign',campaign_id=result['campaign_id'],created_at=result['created_at'],max_candidates=16,
            datasets=result['dataset_bindings']['SPY@1d'],holdout_window=dict(start='2027-01-01T00:00:00.000Z',end_exclusive='2027-04-01T00:00:00.000Z'),
            initial_cash='10000.000000',walkforward=dict(initial_train_bars=16,test_bars=16)),'campaign_sha256'))
    finally:store.close()
    return dict(ready=True,development_rows=result['development']['row_count'],validation_rows=result['validation']['row_count'])


def verify_accounting(spec,rows,initial,costs,manifest,actual,decision_start_at=None,equity_trim=0):
    expected=replay(spec,rows,initial,costs,manifest['price_scale'],manifest['cash_scale'],decision_start_at=decision_start_at)
    for kind in ('orders','fills','equity'):
        values=expected[kind][equity_trim:]if kind=='equity'else expected[kind]
        assert actual[kind]==values,'independent '+kind+' mismatch'
    return expected


def audit(root):
    root=Path(root).resolve();config=json.loads((root/'setup.json').read_text());cid=config['campaign_id']
    entries=[json.loads(line)for line in(root/'candidates'/f'{cid}.jsonl').read_text().splitlines()]
    store=CampaignRegistry(root/'campaign.sqlite');proof=[]
    try:
        dev=store.dataset(config['development']['manifest_sha256']);val=store.dataset(config['validation']['manifest_sha256'])
        # Every author outcome counts as a registration, even if no valid spec exists.
        for e in entries:
            if e['status']=='registered':
                spec=json.loads((root/'candidates'/cid/'specs'/(e['strategy_sha256']+'.json')).read_text())
                prereg=json.loads((root/'candidates'/cid/'preregistrations'/(e['preregistration_sha256']+'.json')).read_text())
            else:spec={};prereg=dict(kind='failed_author_attempt',source_entry_sha256=e['entry_sha256'])
            store.register(cid,spec,prereg,datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'))
        for e in entries:
            if e['status']!='registered':continue
            key=e['preregistration_sha256'];campaign=store.campaign(cid)
            spec=json.loads((root/'candidates'/cid/'specs'/(e['strategy_sha256']+'.json')).read_text())
            prereg=json.loads((root/'candidates'/cid/'preregistrations'/(key+'.json')).read_text())
            rec=dict(strategy_sha256=spec['strategy_sha256'],agent_written=e['author']['agent_written'])
            try:rec['development_verdict']=store.evaluate(key,'development',datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'))['verdict']
            except Rejected as error:
                assert error.code=='insufficient_warmup';rec['development_error']=error.code
            try:rec['validation']=store.evaluate(key,'validation',datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'))
            except Rejected as error:
                assert error.code in ('development_gate','insufficient_warmup');rec['validation_gate']=error.code
                diagnostic=evaluate_rows(spec,prereg,campaign,val['manifest'],val['rows'],warmup_rows=dev['rows'])
                # Diagnostic is never a stage pass, verdict, promotion or selector label.
                verify_accounting(spec,dev['rows']+val['rows'],campaign['initial_cash'],prereg['cost_model'],val['manifest'],diagnostic,
                    decision_start_at=dev['rows'][-1]['bar_close_at'],equity_trim=len(dev['rows']))
                rec['validation_diagnostic']=dict(stage_accepted=False,independent_replay=True,fills=len(diagnostic['fills']),data_classification='historical_unverified')
            proof.append(rec)
        events=[json.loads(row[0])for row in store.db.execute('select payload from campaign_journal where campaign_id=? order by seq',(cid,))]
        admissions=[e for e in events if e['kind']=='attempt'];results=[e for e in events if e['kind']=='result']
        replayed=[]
        for e in results:
            output=e['output'];admission=admissions[e['attempt']];folder=Path(admission['job_dir'])/'artifacts'
            # A later stage can reject after a worker receipt has already completed.
            # Audit those durable receipts too; never hide orphaned completed work.
            for file in folder.glob('*.json'):
                value=json.loads(file.read_text())
                if value.get('kind')!='backtest_result' or value.get('status')!='completed':continue
                artifacts=ArtifactStore(folder);receipt=read_receipt(artifacts,value['receipt_sha256']);request=artifacts.get(receipt['request_sha256'],'request_sha256');spec=artifacts.get(request['strategy_sha256'],'strategy_sha256')
                assert request['manifest_sha256']==dev['manifest']['manifest_sha256']
                if output['status']=='completed':assert request==output['request']and spec==output['spec']and receipt==output['receipt']
                rows={kind:artifacts.get(receipt['artifacts'][kind+'_sha256'])['rows']for kind in ('orders','fills','equity')}
                expected=verify_accounting(spec,dev['rows'],request['initial_cash'],request['cost_model'],dev['manifest'],rows)
                for k,v in expected['metrics'].items():
                    if k=='regulatory_fee_total'and k not in receipt['metrics']:assert Decimal(v)==0
                    else:assert Decimal(str(receipt['metrics'][k]))==Decimal(str(v)),k
                replayed.append(receipt['receipt_sha256'])
        summary=json.loads((root/'campaigns'/cid/'summary.json').read_text())
        assert len(entries)==len(admissions)==len(results)==summary['N']==summary['ledger_decision_count']==summary['ledger_outcome_count']
        author_episodes=[json.loads(line)for line in(root/'episodes'/f'{cid}.export.jsonl').read_text().splitlines()]
        assert len(author_episodes)==len(entries)and all(e['label_status']=='resolved'for e in author_episodes)
        assert store.db.execute('select count(*) from registrations where campaign_id=?',(cid,)).fetchone()[0]==len(entries)
        assert store.db.execute('select count(*) from holdout_requests').fetchone()[0]==0
        report=dict(qualification='research_only',promoting=False,paper=False,orders=False,N=len(entries),registry_attempts=len(admissions),author_registry_attempts=len(entries),ledger_decisions=summary['ledger_decision_count'],ledger_outcomes=summary['ledger_outcome_count'],
            author_episode_decisions=len(author_episodes),completed_receipts=len(replayed),independent_replay_matches=len(replayed),receipt_hashes=replayed,stage_results=proof,
            actual_model_registered=sum(e['status']=='registered'and e['author']['agent_written']for e in entries),statuses={s:sum(e['status']==s for e in entries)for s in {e['status']for e in entries}},holdout_requests=0)
        (root/'proof-report.json').write_bytes(canonical(report)+b'\n');return report
    finally:store.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['setup','audit']);p.add_argument('--state',type=Path,required=True);p.add_argument('--development-registry');p.add_argument('--validation-registry');a=p.parse_args()
    print(json.dumps(setup(a.state,a.development_registry,a.validation_registry)if a.phase=='setup'else audit(a.state)))


if __name__=='__main__':main()
