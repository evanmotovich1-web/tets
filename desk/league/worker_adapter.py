"""Real campaign adapter: no financial screen weakening and no new simulator."""
from pathlib import Path
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from contextlib import contextmanager
import hashlib,json,math,random,subprocess,sys,sqlite3,os
from .runtime import LeagueError,require,put,encoded,filehash,digest,valid_result,WORLD_BASE,WORLD_FAMILIES,world_group

class CampaignEvaluator:
    deterministic_replay=False
    timeout_seconds=45
    def __init__(self,root,desk=None):
        self.root=Path(root).resolve();self.desk=Path(desk or Path(__file__).resolve().parents[1]).resolve()
        require(self.desk==Path(__file__).resolve().parents[1], 'only the live canonical desk is supported')
        self.source_files=self.live_sources();self.check_source()
        self.binding='live-campaign:'+digest(self.source_files)
        self.root.mkdir(parents=True,exist_ok=True)
        for name,module in list(sys.modules.items()):
            if name=='backtest' or name.startswith('backtest.'):
                location=getattr(module,'__file__',None)
                require(location is not None and Path(location).resolve().is_relative_to(self.desk/'backtest'),'foreign backtest module')
        from backtest.contracts import canonical,sealed,strategy_spec
        from backtest.storage import ArtifactStore,load_dataset
        from backtest.worker import read_receipt
        from backtest.independent_replay import replay
        self.canonical=canonical;self.sealed=sealed;self.strategy_spec=strategy_spec;self.Store=ArtifactStore;self.load_dataset=load_dataset;self.read_receipt=read_receipt;self.replay=replay
    def live_sources(self):
        files={}
        for directory in ['backtest','runtime','league','config']:
            for current,dirs,names in os.walk(self.desk/directory):
                dirs[:]=sorted(d for d in dirs if not d.startswith('.') and d not in ('__pycache__','node_modules'))
                for name in sorted(names):
                    p=Path(current)/name
                    if p.suffix in ('.py','.mjs','.json'):files[str(p.relative_to(self.desk))]=filehash(p)
        for name in ['requirements.lock','package-lock.json']:
            files[name]=filehash(self.desk/name)
        return files
    def check_source(self):
        require(self.live_sources()==self.source_files,'live desk source drift')
    @contextmanager
    def state_db(self):
        db=sqlite3.connect('file:'+str(self.root/'campaign.sqlite')+'?mode=ro',uri=True)
        try:yield db
        finally:db.close()
    def world(self,seed):
        root=self.root/'worlds'/str(seed);rng=random.Random(seed);day=datetime(2024,1,8);last=100.;rows=[];family=seed//WORLD_BASE
        require(0<=family<len(WORLD_FAMILIES),'unknown world family')
        def price(v):return format(Decimal(str(v)).quantize(Decimal('.01')),'.2f')
        for i in range(128):
            while day.weekday()>4:day+=timedelta(days=1)
            op=price(max(10,last+rng.gauss(0,.3)))
            # Distinct generator families (task 1.b item 6); family 0 is the original world, byte-identical.
            if family==1:cl=price(max(10,float(op)+.15*(100-float(op))+.6*math.sin(i/3)+rng.gauss(0,.8)))
            elif family==2:cl=price(max(10,float(op)+(.35 if (i//16)%2==0 else -.35)+rng.gauss(0,.7)))
            else:cl=price(max(10,float(op)+(.45 if (i//32)%2==0 else -.25)+.9*math.sin(i/4)+rng.gauss(0,.6)))
            def stamp(hour,minute):return day.replace(hour=hour,minute=minute,tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
            rows.append(dict(symbol='SYNTH',bar_open_at=stamp(9,30),bar_close_at=stamp(16,0),available_at=stamp(16,0),open=op,close=cl,high=price(max(float(op),float(cl))+.8),low=price(min(float(op),float(cl))-.8),volume='100000'))
            day+=timedelta(days=1);last=float(cl)
        data=b''.join(self.canonical(r)+b'\n'for r in rows);h=hashlib.sha256(data).hexdigest();dataset=f'synthetic:league-world:{seed}'
        template=json.loads((self.desk/'backtest/fixtures/manifest.json').read_text());template.pop('manifest_sha256')
        template.update(dataset_id=dataset,calendar_id='nyse_regular',row_count=len(rows),data_sha256=h,coverage=dict(first_bar_open=rows[0]['bar_open_at'],last_bar_close=rows[-1]['bar_close_at']))
        template['source']=dict(provider='host-synthetic-development',source_reference=f'worker_adapter-v1:seed={seed}'if family==0 else f'worker_adapter-v2:family={WORLD_FAMILIES[family]}:seed={seed}',license_reference='internally generated synthetic research data',retrieved_at='2026-10-25T00:00:00.000Z',snapshot_sha256=h)
        manifest=self.sealed(template,'manifest_sha256');put(root/'manifest.json',manifest)
        if (root/'bars.jsonl').exists():require((root/'bars.jsonl').read_bytes()==data,'world drift')
        else:(root/'bars.jsonl').write_bytes(data)
        registry=dict(schema_version=1,kind='dataset_registry',datasets={dataset:dict(classification='synthetic',partition='development',data='bars.jsonl',manifest='manifest.json',manifest_sha256=manifest['manifest_sha256'])});put(root/'registry.json',registry)
        end=(datetime.fromisoformat(rows[-1]['bar_close_at'].replace('Z','+00:00')).replace(hour=0,minute=0)+timedelta(days=1)).isoformat(timespec='milliseconds').replace('+00:00','Z')
        return root,dataset,dict(start=rows[0]['bar_open_at'],end_exclusive=end)
    def __call__(self,candidate,seed):return self.execute(candidate,seed)
    def reconcile(self,candidate,seed):
        # Existing campaign reconciles a committed result or returns unknown; never launches another accepted unknown job.
        return self.execute(candidate,seed)
    def execute(self,candidate,seed):
        self.check_source();world,dataset,window=self.world(seed)
        spec=self.strategy_spec(dict(schema_version=2,family='sma_cross_long',symbol='SYNTH',parameters=candidate['genome'],position=dict(quantity='1'),execution=dict(order_type='market',limit_offset_bps='0'),rationale='Host-evolved synthetic strategy genome; not an LLM-authored or qualified strategy.'))
        campaign_id='league-'+candidate['candidate_id']+'-'+str(seed);plan_path=self.root/'plans'/(campaign_id+'.json')
        plan=dict(schema_version=1,kind='backtest_campaign',campaign_id=campaign_id,registry=str(world/'registry.json'),dataset_id=dataset,evaluation_window=window,initial_cash='10000',cost_model=dict(commission_bps='10',slippage_bps_per_side='5',rounding='half_even'),walkforward=dict(initial_train_bars=32,test_bars=32),screen={},limits=dict(concurrency=1,max_attempts=1,max_wall_ms=30000,job_timeout_ms=20000),candidates=[dict(id=candidate['candidate_id'],strategy=spec,source=dict(kind='unverified_external'))])
        put(plan_path,dict(plan=plan,fleet=candidate['fleet'])if 'fleet'in candidate else plan)
        process=subprocess.run(['node',str(Path(__file__).with_name('campaign_bridge.mjs')),str(self.desk),str(plan_path),str(self.root)],capture_output=True,text=True,timeout=45)
        require(process.returncode==0,'campaign failed: '+process.stderr[-800:])
        summary=json.loads(process.stdout.strip());require(summary['status']=='completed'and summary['counts']['completed']==1,'campaign incomplete or unknown')
        campaign_root=self.root/'campaigns'/campaign_id
        results=list((campaign_root/'jobs').rglob('result.json'));require(len(results)==1,'ambiguous campaign result')
        raw=json.loads(results[0].read_text());require(raw['status']=='completed','worker not complete')
        receipt=raw['receipt'];record=dict(job_dir=str(results[0].parent),registry=str(world/'registry.json'),dataset_id=dataset,campaign_summary=str(campaign_root/'summary.json'),result_sha256=filehash(results[0]),summary_sha256=filehash(campaign_root/'summary.json'),manifest_sha256=raw['manifest_sha256'],receipt_sha256=receipt['receipt_sha256'])
        r=dict(score=float(receipt['metrics']['total_return']),max_drawdown=float(receipt['metrics']['max_drawdown']),receipt_sha256=receipt['receipt_sha256'],manifest_sha256=raw['manifest_sha256'],replay_verified=True,classification=raw['classification'],partition='development')
        valid_result(r);require(self.verify_record(record,r),'independent replay failed')
        # Receipts, requests, identities and admission counts belong to campaign.mjs.
        # This adapter stores no second receipt/strategy registry or locator cache.
        require(self.verify(r),'canonical receipt replay failed');return r
    def record(self,campaign_id):
        with self.state_db() as db:
            rows=db.execute('select payload from campaign_journal where campaign_id=? order by seq',(campaign_id,)).fetchall()
        events=[json.loads(r[0]) for r in rows]
        require(len(events)==2 and events[0]['kind']=='attempt' and events[1]['kind']=='result','campaign incomplete or unknown')
        previous='0'*64
        for index,event in enumerate(events):
            body={k:v for k,v in event.items() if k!='entry_sha256'}
            require(event['seq']==index and event['previous_sha256']==previous and hashlib.sha256(self.canonical(body)).hexdigest()==event['entry_sha256'],'native campaign journal drift')
            previous=event['entry_sha256']
        raw=events[1]['output'];job=Path(events[0]['job_dir']);summary_path=self.root/'campaigns'/campaign_id/'summary.json'
        summary=json.loads(summary_path.read_text())
        require(job.resolve().is_relative_to(self.root) and (job/'result.json').read_bytes()==self.canonical(raw),'campaign output drift')
        require(summary['attempts_sha256']==previous and summary['N']==summary['registry_attempt_count']==summary['ledger_decision_count']==summary['ledger_outcome_count']==1,'native attempt/episode mismatch')
        require(hashlib.sha256(self.canonical({k:v for k,v in summary.items() if k!='summary_sha256'})).hexdigest()==summary['summary_sha256'],'native summary drift')
        exported=Path(summary['episodes']['path']).resolve();require(exported.is_relative_to(self.root),'episode path escape')
        episodes=[json.loads(line)for line in exported.read_text().splitlines()]
        require(len(episodes)==1 and hashlib.sha256(self.canonical(episodes)).hexdigest()==summary['episodes']['export_sha256'] and episodes[0]['outcome']['receipts']==[raw['receipt']['receipt_sha256']],'native episode drift')
        with self.state_db() as db:
            plan=json.loads(db.execute("select value from campaign_metadata where campaign_id=? and key='plan'",(campaign_id,)).fetchone()[0])
        require(hashlib.sha256(self.canonical({k:v for k,v in plan.items() if k!='plan_sha256'})).hexdigest()==plan['plan_sha256'] and plan['plan_sha256']==summary['plan_sha256'],'native plan drift')
        return dict(job_dir=str(job),registry=plan['registry'],dataset_id=plan['dataset_id'],campaign_summary=str(summary_path),result_sha256=filehash(job/'result.json'),summary_sha256=filehash(summary_path),manifest_sha256=raw['manifest_sha256'],receipt_sha256=raw['receipt']['receipt_sha256'])
    def seed_from_campaign(self,campaign_id):return int(campaign_id.rsplit('-',1)[1])
    def verify(self,r):
        self.check_source()
        with self.state_db() as db:
            rows=db.execute('select campaign_id,payload from campaign_journal order by campaign_id,seq').fetchall()
        matches=[cid for cid,payload in rows if json.loads(payload).get('output',{}).get('receipt',{}).get('receipt_sha256')==r['receipt_sha256']]
        require(bool(matches),'receipt absent from authoritative campaign journal')
        return all(self.verify_record(self.record(cid),r) for cid in matches)
    def verify_record(self,record,r):
        job=Path(record['job_dir']).resolve();require(job.is_relative_to(self.root),'job path escape')
        registry=Path(record['registry']).resolve();summary_path=Path(record['campaign_summary']).resolve()
        require(registry.is_relative_to(self.root)and summary_path.is_relative_to(self.root),'campaign reference path escape')
        require(filehash(job/'result.json')==record['result_sha256']and filehash(summary_path)==record['summary_sha256'],'campaign output drift')
        result=json.loads((job/'result.json').read_text());store=self.Store(job/'artifacts');receipt=self.read_receipt(store,r['receipt_sha256'])
        require(result['receipt']==receipt and receipt['status']=='completed'and receipt['qualification']=='research_only'and receipt['promoting']is False,'receipt mismatch')
        request=store.get(receipt['request_sha256'],'request_sha256');spec=store.get(request['strategy_sha256'],'strategy_sha256')
        manifest,rows=self.load_dataset(registry,record['dataset_id'],2)
        require(manifest['classification']=='synthetic'and manifest['partition']=='development'and manifest['manifest_sha256']==r['manifest_sha256']==request['manifest_sha256'],'dataset binding mismatch')
        replay=self.replay(spec,rows,request['initial_cash'],request['cost_model'],manifest['price_scale'],manifest['cash_scale'])
        for kind in ['orders','fills','equity']:
            artifact=store.get(receipt['artifacts'][kind+'_sha256']);require(artifact['request_sha256']==receipt['request_sha256']and artifact['rows']==replay[kind],'independent '+kind+' mismatch')
        for key,value in replay['metrics'].items():
            if key not in receipt['metrics']:
                # Campaign pins regulatory fees to zero; the legacy native receipt omits that separate zero field.
                require(key=='regulatory_fee_total' and Decimal(str(value))==0 and Decimal(request['cost_model'].get('regulatory_fee_bps_on_sells','0'))==0,'missing nonzero accounting metric')
            else:require(Decimal(str(receipt['metrics'][key]))==Decimal(str(value)),'independent accounting mismatch: '+key)
        require(float(receipt['metrics']['total_return'])==r['score']and float(receipt['metrics']['max_drawdown'])==r['max_drawdown'],'fitness not bound to receipt')
        return True

    def experiences(self,db):
        # Learning is a projection of native episode decisions, never another ledger.
        populations={c['candidate_id']:c for row in db.execute('select population from league_generations')for c in json.loads(row[0])}
        for job in db.execute('select * from league_jobs order by generation,candidate,seed'):
            cid='league-'+job['candidate']+'-'+str(job['seed']);c=populations[job['candidate']]
            summary_path=self.root/'campaigns'/cid/'summary.json'
            require(summary_path.is_file(),'native campaign outcome unavailable')
            summary=json.loads(summary_path.read_text())
            rows=[json.loads(line)for line in Path(summary['episodes']['path']).read_text().splitlines()]
            require(len(rows)==1,'ambiguous native episode');episode=rows[0]
            r=json.loads(job['result'])if job['result']else None
            yield dict(decision_id=episode['decision_id'],candidate_id=c['candidate_id'],lineage_id=c['lineage_id'],world_group=world_group(job['seed']),partition='development',classification='synthetic',observation=c['genome'],outcome=dict(status='completed'if r else 'unknown',score=r['score']if r else None,replay_verified=bool(r),receipt_sha256=r['receipt_sha256']if r else None),origin=c['origin'])

    def verify_schedule(self,db):
        for job in db.execute("select * from league_jobs where status='completed'"):
            record=self.record('league-'+job['candidate']+'-'+str(job['seed']))
            result=json.loads(job['result'])
            require(record['receipt_sha256']==result['receipt_sha256'],'schedule references another campaign receipt')
            self.verify_record(record,result)
