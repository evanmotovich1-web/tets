"""Persistent league scheduling above a host-owned, receipt-verifying evaluator."""
from contextlib import contextmanager
from pathlib import Path
import hashlib,json,math,random,re,sqlite3,time

class LeagueError(ValueError):pass

def require(ok,msg):
    if not ok:raise LeagueError(msg)
def encoded(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(v):return hashlib.sha256(encoded(v).encode()).hexdigest()
def filehash(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def exact(v,keys):require(type(v)is dict and set(v)==set(keys),'unexpected or missing fields')
def integer(v,a,b):require(type(v)is int and a<=v<=b,'integer out of bounds')
def put(p,value):
    p.parent.mkdir(parents=True,exist_ok=True);data=encoded(value)+'\n'
    if p.exists():require(p.read_text()==data,'immutable file conflict: '+p.name)
    else:
        tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(data);tmp.replace(p)
# World families (task 1.b item 6). Distinct generator families, not just seeds. A world is
# identified by an int seed: family index * WORLD_BASE + seed. Family 0 keeps legacy seeds intact.
WORLD_FAMILIES=('trend_v1','meanrev_v1','regime_v1');WORLD_BASE=1000000
def world_seed(family,seed):return WORLD_FAMILIES.index(family)*WORLD_BASE+seed
def world_family(world):return WORLD_FAMILIES[world//WORLD_BASE]
def world_group(world):return 'synthetic-family-v1' if world<WORLD_BASE else 'synthetic-family-'+world_family(world)
def validate_cohorts(cohorts,seeds):
    require(type(cohorts)is list and 2<=len(cohorts)<=4,'at least two cohorts required')
    seen_ids=set();seen_families=set()
    for c in cohorts:
        exact(c,['id','world_families']);require(type(c['id'])is str and re.fullmatch(r'[A-Z][A-Za-z0-9_-]{0,15}',c['id'])and c['id']not in seen_ids,'bad cohort id');seen_ids.add(c['id'])
        f=c['world_families'];require(type(f)is list and 1<=len(f)<=3 and all(x in WORLD_FAMILIES for x in f),'unknown world family')
        require(not seen_families.intersection(f)and len(set(f))==len(f),'world families must be disjoint across cohorts');seen_families.update(f)
    require(all(s<WORLD_BASE for s in seeds),'seed too large for cohort world identity')
def world_seeds(c,candidate):
    if 'cohorts' not in c:return list(c['seeds'])
    families=next(x['world_families']for x in c['cohorts']if x['id']==candidate['cohort'])
    return [world_seed(f,s)for f in families for s in c['seeds']]
def validate(c):
    require(type(c)is dict,'bad config');exact({k:v for k,v in c.items()if k!='cohorts'},['experiment_id','generations','population','seeds','seed','max_drawdown','max_jobs','wall_seconds'])
    require(type(c['experiment_id'])is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',c['experiment_id']),'bad experiment id')
    integer(c['generations'],1,4);integer(c['population'],4,8);integer(c['seed'],0,2**31-1)
    require(type(c['seeds'])is list and 1<=len(c['seeds'])<=8,'bad seeds')
    for s in c['seeds']:integer(s,0,2**31-1)
    require(len(c['seeds'])==len(set(c['seeds'])),'duplicate seeds')
    integer(c['max_jobs'],1,256);integer(c['wall_seconds'],1,300)
    require(type(c['max_drawdown'])in(int,float) and math.isfinite(c['max_drawdown'])and 0<c['max_drawdown']<=1,'bad drawdown cap')
    per=len(c['seeds'])
    if 'cohorts' in c:validate_cohorts(c['cohorts'],c['seeds']);per=len(c['seeds'])*max(len(x['world_families'])for x in c['cohorts'])
    require(c['generations']*c['population']*per<=c['max_jobs'],'global job budget exceeded')
def valid_result(r):
    exact(r,['score','max_drawdown','receipt_sha256','manifest_sha256','replay_verified','classification','partition'])
    for k in ['score','max_drawdown']:require(type(r[k])in(int,float)and math.isfinite(r[k]),'nonfinite fitness')
    require(0<=r['max_drawdown']<=1,'invalid drawdown')
    for k in ['receipt_sha256','manifest_sha256']:require(type(r[k])is str and re.fullmatch('[0-9a-f]{64}',r[k]),'invalid receipt identity')
    require(r['replay_verified']is True and r['classification']=='synthetic'and r['partition']=='development','unverified or forbidden fitness')

@contextmanager
def manager(root):
    root.mkdir(parents=True,exist_ok=True);lock=sqlite3.connect(root/'manager-lock.sqlite',timeout=0)
    try:
        try:lock.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError as e:raise LeagueError('manager already active')from e
        yield
    finally:lock.close()
def connect(root):
    db=sqlite3.connect(root/'campaign.sqlite');db.row_factory=sqlite3.Row
    db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS league_meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS league_generations(g INTEGER PRIMARY KEY,population TEXT NOT NULL,snapshot TEXT NOT NULL,snapshot_sha256 TEXT NOT NULL,selection TEXT);
CREATE TABLE IF NOT EXISTS league_jobs(id TEXT PRIMARY KEY,generation INTEGER NOT NULL,candidate TEXT NOT NULL,seed INTEGER NOT NULL,snapshot_sha256 TEXT NOT NULL,status TEXT NOT NULL,result TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS league_events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,prev TEXT NOT NULL,hash TEXT NOT NULL);''')
    return db
def event(db,kind,payload):
    last=db.execute('select seq,hash from league_events order by seq desc limit 1').fetchone();seq=last['seq']+1 if last else 0;prev=last['hash']if last else '0'*64
    h=digest(dict(seq=seq,kind=kind,payload=payload,prev=prev))
    db.execute('insert into league_events values(?,?,?,?,?)',(seq,kind,encoded(payload),prev,h));return h

def audit(db):
    prev='0'*64;league_generations={};league_jobs={};selections={};attempts={};frozen=None
    for seq,row in enumerate(db.execute('select * from league_events order by seq')):
        p=json.loads(row['payload']);require(row['seq']==seq and row['prev']==prev and row['hash']==digest(dict(seq=seq,kind=row['kind'],payload=p,prev=prev)),'journal tampered');prev=row['hash'];kind=row['kind']
        if kind=='plan':
            require(seq==0 and frozen is None,'unexpected plan event');frozen=p
        elif kind=='generation':
            require(frozen is not None and p['g']==len(league_generations),'invalid generation ordering');g=p['g'];c=frozen['config'];prior=selections.get(g-1,dict(winners=[],public=[]))
            require(p['population']==population(c,g,prior['winners'])and p['snapshot']==dict(generation=g,classification='synthetic',partition='development',previous_development=prior['public']),'generation differs from frozen plan')
            league_generations[g]=p
            for candidate in p['population']:
                for seed in world_seeds(c,candidate):
                    jid=digest([c['experiment_id'],g,candidate['candidate_id'],seed]);league_jobs[jid]=dict(id=jid,generation=g,candidate=candidate['candidate_id'],seed=seed,snapshot_sha256=digest(p['snapshot']),status='pending',result=None,error=None)
        elif kind in('started','unknown','completed'):
            require(p['id']in league_jobs,'unknown job identity');j=league_jobs[p['id']]
            if kind=='started':
                n=attempts.get(p['id'],0)+1;attempts[p['id']]=n
                require(j['status']in('pending','running','unknown')and n<=frozen['per_job_attempt_limit']and sum(attempts.values())<=frozen['attempt_budget']and p['attempt']==n,'invalid/exhausted job lifecycle')
                require(p['reserved_wall_ms']==frozen['evaluator_timeout_ms'],'wall reservation drift');j.update(status='running',result=None,error=None)
            else:
                require(j['status']=='running','terminal result without reservation');integer(p['elapsed_ms'],1,frozen['evaluator_timeout_ms'])
                if kind=='completed':valid_result(p['result']);j.update(status='completed',result=encoded(p['result']),error=None)
                else:j.update(status='unknown',result=None,error=p['code'])
        elif kind=='selection':
            require(p['g']in league_generations and p['g']not in selections,'invalid selection event');selections[p['g']]=p['selection']
        else:raise LeagueError('unknown journal event')
    require(frozen is not None and [dict(r)for r in db.execute('select * from league_meta')]==[dict(k='frozen',v=encoded(frozen))],'frozen metadata differs from journal')
    actual_jobs={r['id']:dict(r)for r in db.execute('select * from league_jobs')};require(actual_jobs==league_jobs,'job identity/lifecycle differs from journal')
    actual=list(db.execute('select * from league_generations order by g'));require(len(actual)==len(league_generations),'missing/extra generation')
    for row in actual:
        p=league_generations[row['g']];s=selections.get(row['g'])
        require(row['population']==encoded(p['population'])and row['snapshot']==encoded(p['snapshot'])and row['snapshot_sha256']==digest(p['snapshot'])and row['selection']==(encoded(s)if s else None),'generation state differs from journal')
        if s:require(s==selection(db,row['g'],frozen['config'],p['population']),'selection inconsistent with verified league_jobs')
    return prev

def budget_used(db):
    counts={};reserved={};wall_ms=0
    for row in db.execute('select kind,payload from league_events order by seq'):
        p=json.loads(row['payload'])
        if row['kind']=='started':
            counts[p['id']]=counts.get(p['id'],0)+1;reserved[p['id']]=p['reserved_wall_ms'];wall_ms+=p['reserved_wall_ms']
        elif row['kind']in('completed','unknown'):wall_ms-=reserved.pop(p['id'])-p['elapsed_ms']
    return counts,wall_ms

def dbhash(db):
    tables=['league_meta','league_generations','league_jobs','league_events']
    tables += [r[0]for r in db.execute("select name from sqlite_master where type='table' and name in ('campaign_metadata','campaign_journal') order by name")]
    return digest({t:[dict(r)for r in db.execute('select * from '+t+' order by 1,2')]for t in tables})
def population(c,g,parents):
    rng=random.Random(c['seed']+g*10007);out=[]
    for slot in range(c['population']):
        parent=parents[slot%len(parents)]if parents and slot<c['population']-2 else None
        if parent:
            genome=dict(parent['genome'])
            if slot>=2:genome=dict(fast_window=max(1,min(7,genome['fast_window']+rng.choice([-1,1]))),slow_window=max(8,min(20,genome['slow_window']+rng.choice([-2,2]))))
        else:genome=dict(fast_window=rng.randint(1,7),slow_window=rng.randint(8,20))
        cid=digest([c['experiment_id'],g,slot,genome])[:24]
        item=dict(candidate_id=cid,genome=genome,parents=[parent['candidate_id']]if parent else [],lineage_id=parent['lineage_id']if parent else cid,origin='host_mutation'if parent else 'host_seed',operator='elite_copy'if parent and slot<2 else ('bounded_mutation'if parent else 'explore'),generation=g)
        if 'cohorts' in c:
            # Offspring inherit the parent's cohort; founders are assigned by slot before any job runs.
            cohort=parent['cohort']if parent else c['cohorts'][slot%len(c['cohorts'])]['id']
            require(cohort in {x['id']for x in c['cohorts']},'unknown cohort');require(not parent or parent.get('cohort')==cohort,'mutation crosses cohorts')
            item['cohort']=cohort
        out.append(item)
    return out

def selection(db,g,c,pop):
    rows=[]
    for candidate in pop:
        league_jobs=db.execute('select * from league_jobs where generation=? and candidate=? order by seed',(g,candidate['candidate_id'])).fetchall()
        require(len(league_jobs)==len(world_seeds(c,candidate))and all(j['status']=='completed'for j in league_jobs),'incomplete generation cannot select')
        values=[json.loads(j['result'])for j in league_jobs];worst=min(v['score']for v in values);risk=max(v['max_drawdown']for v in values)
        rows.append(dict(candidate=candidate,worst_score=worst,mean_score=sum(v['score']for v in values)/len(values),max_drawdown=risk,qualified=worst>0 and risk<=c['max_drawdown']))
    ranked=sorted([r for r in rows if r['qualified']],key=lambda r:(-r['worst_score'],-r['mean_score'],r['candidate']['candidate_id']))
    # Preserve different horizons/parameter cells; never invent mechanism diversity.
    winners=[];seen=set()
    for r in ranked:
        cell=(r['candidate']['genome']['fast_window']//3,r['candidate']['genome']['slow_window']//5)
        if cell not in seen:winners.append(r);seen.add(cell)
        if len(winners)==2:break
    ids={r['candidate']['candidate_id']for r in winners}
    public=[dict(candidate_id=r['candidate']['candidate_id'],genome=r['candidate']['genome'],worst_score=r['worst_score'],mean_score=r['mean_score'],max_drawdown=r['max_drawdown'],selected=r['candidate']['candidate_id']in ids)for r in rows]
    return dict(winners=[r['candidate']for r in winners],public=public,retired=[r['candidate']['candidate_id']for r in rows if r['candidate']['candidate_id']not in ids])

def external_verify(evaluator,r):
    if evaluator is not None and hasattr(evaluator,'verify'):require(evaluator.verify(r)is True,'external receipt verification refused')

def summary(db,status):
    selections=[json.loads(r[0])for r in db.execute('select selection from league_generations where selection is not null')]
    native=None
    if db.execute("select 1 from sqlite_master where name='campaign_journal'").fetchone():
        events=[json.loads(r[0])for r in db.execute('select payload from campaign_journal order by campaign_id,seq')]
        native=dict(attempts=sum(e['kind']=='attempt'for e in events),outcomes=sum(e['kind']=='result'for e in events),receipt_ids=sorted({e['output']['receipt']['receipt_sha256']for e in events if e['kind']=='result'and e['output'].get('receipt')}))
    return dict(status=status,agent_kind='strategy_genome',qualification='synthetic_research_only',main_model_trained=False,promoting=False,native_campaign=native,completed_jobs=db.execute("select count(*) from league_jobs where status='completed'").fetchone()[0],unknown_jobs=db.execute("select count(*) from league_jobs where status in ('unknown','running')").fetchone()[0],attempted_candidates=sum(len(json.loads(r[0]))for r in db.execute('select population from league_generations')),generations_completed=len(selections),selected_candidates=sum(len(s['winners'])for s in selections),retired_candidates=sum(len(s['retired'])for s in selections))

def experience(db):
    candidates={x['candidate_id']:x for row in db.execute('select population from league_generations')for x in json.loads(row[0])}
    for row in db.execute('select * from league_jobs order by generation,candidate,seed'):
        c=candidates[row['candidate']];r=json.loads(row['result'])if row['result']else None
        yield dict(decision_id=row['id'],candidate_id=c['candidate_id'],lineage_id=c['lineage_id'],world_group=world_group(row['seed']),partition='development',classification='synthetic',observation=c['genome'],outcome=dict(status='completed'if r else 'unknown',score=r['score']if r else None,replay_verified=bool(r),receipt_sha256=r['receipt_sha256']if r else None),origin=c['origin'])

def finalize(root,db,evaluator):
    audit(db)
    if hasattr(evaluator,'verify_schedule'):evaluator.verify_schedule(db)
    for row in db.execute("select result from league_jobs where status='completed'"):external_verify(evaluator,json.loads(row[0]))
    result=summary(db,'completed');put(root/'summary.json',result)
    rows=evaluator.experiences(db)if hasattr(evaluator,'experiences')else experience(db)
    data=''.join(encoded(r)+'\n'for r in rows);p=root/'experiences.jsonl'
    if p.exists():require(p.read_text()==data,'experience export changed')
    else:p.write_text(data)
    excluded={'MANIFEST.json','DONE.json','STOP','manager-lock.sqlite','campaign.sqlite'}
    files={str(p.relative_to(root)):filehash(p)for p in sorted(root.rglob('*'))if p.is_file()and p.name not in excluded and not p.name.endswith(('-wal','-shm','-journal'))}
    manifest=dict(files=files,db_sha256=dbhash(db),journal_sha256=audit(db),qualification='synthetic_research_only');put(root/'MANIFEST.json',manifest)
    put(root/'DONE.json',dict(status='completed',manifest_sha256=filehash(root/'MANIFEST.json'),main_model_trained=False))
    return result

def verify_league(state_dir,evaluator=None):
    root=Path(state_dir);require((root/'DONE.json').is_file(),'league incomplete')
    done=json.loads((root/'DONE.json').read_text());require(done['manifest_sha256']==filehash(root/'MANIFEST.json'),'completion manifest altered')
    manifest=json.loads((root/'MANIFEST.json').read_text())
    for name,h in manifest['files'].items():
        p=(root/name).resolve();require(p.is_relative_to(root.resolve())and p.is_file()and filehash(p)==h,'file tampered: '+name)
    db=connect(root)
    try:
        if evaluator is not None:
            frozen=json.loads(db.execute("select v from league_meta where k='frozen'").fetchone()[0])
            require(frozen['runtime_sha256']==filehash(Path(__file__))and frozen['evaluator_binding']==evaluator.binding,'current code/evaluator differs from completed run')
        require(audit(db)==manifest['journal_sha256']and dbhash(db)==manifest['db_sha256'],'database tampered')
        if hasattr(evaluator,'verify_schedule'):evaluator.verify_schedule(db)
        for row in db.execute("select result from league_jobs where status='completed'"):external_verify(evaluator,json.loads(row[0]))
        expected=summary(db,'completed');require(json.loads((root/'summary.json').read_text())==expected,'summary altered');return expected
    finally:db.close()

def run_league(state_dir,config,evaluator,stop_after_jobs=None):
    validate(config);root=Path(state_dir);start=time.monotonic();performed=0
    binding=getattr(evaluator,'binding',None);require(type(binding)is str and 0<len(binding)<=256,'evaluator binding required')
    with manager(root):
        db=connect(root)
        try:
            timeout_ms=int(math.ceil(getattr(evaluator,'timeout_seconds',1)*1000));integer(timeout_ms,1,60000)
            frozen=dict(config=config,evaluator_binding=binding,runtime_sha256=filehash(Path(__file__)),attempt_budget=config['max_jobs']*2,per_job_attempt_limit=2,evaluator_timeout_ms=timeout_ms)
            prior=db.execute("select v from league_meta where k='frozen'").fetchone()
            if prior:
                require(json.loads(prior[0])['config'].get('cohorts')==config.get('cohorts'),'cohort_assignment_changed')
                require(json.loads(prior[0])==frozen,'configuration or evaluator drift')
            else:
                db.execute('insert into league_meta values(?,?)',('frozen',encoded(frozen)));event(db,'plan',frozen);db.commit();put(root/'plan.json',frozen)
                if 'cohorts' in config:put(root/'cohorts.json',dict(cohorts=config['cohorts'],cohorts_sha256=digest(config['cohorts']),assigned_before_any_job=True))
            audit(db)
            if (root/'DONE.json').exists():return verify_league(root,evaluator)
            parents=[];previous=[]
            for g in range(config['generations']):
                row=db.execute('select * from league_generations where g=?',(g,)).fetchone()
                if row:
                    pop=json.loads(row['population']);snapshot=json.loads(row['snapshot'])
                else:
                    pop=population(config,g,parents);snapshot=dict(generation=g,classification='synthetic',partition='development',previous_development=previous)
                    sh=digest(snapshot);db.execute('insert into league_generations values(?,?,?,?,null)',(g,encoded(pop),encoded(snapshot),sh));event(db,'generation',dict(g=g,population=pop,snapshot=snapshot))
                    for candidate in pop:
                        for seed in world_seeds(config,candidate):
                            jid=digest([config['experiment_id'],g,candidate['candidate_id'],seed]);db.execute('insert into league_jobs values(?,?,?,?,?,?,?,?)',(jid,g,candidate['candidate_id'],seed,sh,'pending',None,None))
                    db.commit();put(root/'generations'/f'{g}.json',dict(population=pop,public_snapshot=snapshot,snapshot_sha256=sh))
                for candidate in pop:
                    for seed in world_seeds(config,candidate):
                        if (root/'STOP').exists()or time.monotonic()-start>=config['wall_seconds']or(stop_after_jobs is not None and performed>=stop_after_jobs):return summary(db,'paused')
                        row=db.execute('select * from league_jobs where generation=? and candidate=? and seed=?',(g,candidate['candidate_id'],seed)).fetchone()
                        if row['status']=='completed':external_verify(evaluator,json.loads(row['result']));continue
                        recovering=row['status']in('running','unknown')
                        if recovering:require(hasattr(evaluator,'reconcile')or getattr(evaluator,'deterministic_replay',False)is True,'unknown result requires reconciliation')
                        counts,used_wall_ms=budget_used(db)
                        require(counts.get(row['id'],0)<frozen['per_job_attempt_limit']and sum(counts.values())<frozen['attempt_budget'],'persisted attempt budget exhausted')
                        if used_wall_ms+timeout_ms>config['wall_seconds']*1000:return summary(db,'paused')
                        db.execute("update league_jobs set status='running',error=null,result=null where id=?",(row['id'],));event(db,'started',dict(id=row['id'],recovering=recovering,attempt=counts.get(row['id'],0)+1,reserved_wall_ms=timeout_ms));db.commit();attempt_start=time.monotonic()
                        try:
                            r=evaluator.reconcile(candidate,seed)if recovering and hasattr(evaluator,'reconcile')else evaluator(candidate,seed)
                            valid_result(r);external_verify(evaluator,r)
                        except Exception as error:
                            db.execute("update league_jobs set status='unknown',error=? where id=?",(type(error).__name__,row['id']));event(db,'unknown',dict(id=row['id'],code=type(error).__name__,elapsed_ms=min(timeout_ms,max(1,math.ceil((time.monotonic()-attempt_start)*1000)))));db.commit()
                            raise LeagueError('evaluation unresolved: '+str(error))from error
                        db.execute("update league_jobs set status='completed',result=?,error=null where id=?",(encoded(r),row['id']));event(db,'completed',dict(id=row['id'],result=r,elapsed_ms=min(timeout_ms,max(1,math.ceil((time.monotonic()-attempt_start)*1000)))));db.commit();performed+=1
                row=db.execute('select selection from league_generations where g=?',(g,)).fetchone()
                if hasattr(evaluator,'verify_schedule'):evaluator.verify_schedule(db)
                selected=selection(db,g,config,pop)
                if row[0]:require(json.loads(row[0])==selected,'selection changed')
                else:db.execute('update league_generations set selection=? where g=?',(encoded(selected),g));event(db,'selection',dict(g=g,selection=selected));db.commit()
                parents=selected['winners'];previous=selected['public']
            return finalize(root,db,evaluator)
        finally:db.close()
