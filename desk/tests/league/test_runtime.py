import hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from league.runtime import run_league,verify_league,LeagueError

def config():return dict(experiment_id='test-league',generations=2,population=4,seeds=[11,23],seed=5,max_drawdown=.1,max_jobs=16,wall_seconds=30)
class Fake:
    binding='fake-development-v1';deterministic_replay=True
    def __init__(self,score=.01):self.calls=0;self.score=score
    def __call__(self,c,seed):
        self.calls+=1;h=hashlib.sha256(json.dumps([c,seed],sort_keys=True).encode()).hexdigest()
        return dict(score=self.score+c['genome']['fast_window']*.001,max_drawdown=.01,receipt_sha256=h,manifest_sha256=h,replay_verified=True,classification='synthetic',partition='development')
    def verify(self,r):return True
class Tests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_two_generations_and_idempotent_completion(self):
        e=Fake();r=run_league(self.root,config(),e);self.assertEqual(r['completed_jobs'],16);self.assertEqual(r['generations_completed'],2)
        self.assertEqual(r['agent_kind'],'strategy_genome');self.assertFalse(r['main_model_trained']);self.assertEqual(e.calls,16)
        self.assertEqual(run_league(self.root,config(),e),r);self.assertEqual(e.calls,16);verify_league(self.root,e)
    def test_pause_resume_has_no_duplicate_work(self):
        e=Fake();r=run_league(self.root,config(),e,stop_after_jobs=3);self.assertEqual(r['status'],'paused');self.assertFalse((self.root/'DONE.json').exists())
        r=run_league(self.root,config(),e);self.assertEqual(e.calls,16);self.assertEqual(r['completed_jobs'],16)
    def test_stop_file(self):
        (self.root/'STOP').touch();e=Fake();r=run_league(self.root,config(),e);self.assertEqual(r['status'],'paused');self.assertEqual(e.calls,0)
        (self.root/'STOP').unlink();self.assertEqual(run_league(self.root,config(),e)['status'],'completed')
    def test_config_drift_refused(self):
        run_league(self.root,config(),Fake(),stop_after_jobs=1);c=config();c['seed']=99
        with self.assertRaises(LeagueError):run_league(self.root,c,Fake())
    def test_invalid_result_blocks_completion(self):
        for field,value in [('score',float('nan')),('partition','holdout'),('classification','historical_research'),('replay_verified',False)]:
            with self.subTest(field=field),tempfile.TemporaryDirectory()as root:
                base=Fake()
                def bad(c,s):r=base(c,s);r[field]=value;return r
                bad.binding='bad';bad.deterministic_replay=True
                with self.assertRaises(LeagueError):run_league(root,config(),bad)
                self.assertFalse((Path(root)/'DONE.json').exists())
    def test_all_can_fail_and_records_remain(self):
        r=run_league(self.root,config(),Fake(score=-1));self.assertEqual(r['selected_candidates'],0);self.assertEqual(r['attempted_candidates'],8)
        self.assertEqual(len((self.root/'experiences.jsonl').read_text().splitlines()),16)
    def test_generation_snapshot_is_identical(self):
        run_league(self.root,config(),Fake());db=sqlite3.connect(self.root/'campaign.sqlite')
        try:
            for g in range(2):self.assertEqual(db.execute('select count(distinct snapshot_sha256) from league_jobs where generation=?',(g,)).fetchone()[0],1)
        finally:db.close()
    def test_tampered_export_is_detected(self):
        run_league(self.root,config(),Fake());p=self.root/'experiences.jsonl';p.write_text(p.read_text()+'{}\n')
        with self.assertRaises(LeagueError):verify_league(self.root)
    def test_tampered_job_is_detected(self):
        run_league(self.root,config(),Fake());db=sqlite3.connect(self.root/'campaign.sqlite');db.execute("update league_jobs set result='{}' where rowid=1");db.commit();db.close()
        with self.assertRaises(LeagueError):verify_league(self.root)
    def test_single_manager_lock(self):
        db=sqlite3.connect(self.root/'manager-lock.sqlite',timeout=0);db.execute('BEGIN EXCLUSIVE')
        try:
            with self.assertRaises(LeagueError):run_league(self.root,config(),Fake())
        finally:db.close()
    def test_budget_and_unknown_fields(self):
        for change in [dict(max_jobs=2),dict(seeds=[1,1]),dict(generations=99),dict(broker=True)]:
            c=config();c.update(change)
            with self.assertRaises(LeagueError):run_league(self.root,c,Fake())
    def test_external_receipt_verifier_can_refuse(self):
        e=Fake();run_league(self.root,config(),e);e.verify=lambda r:False
        with self.assertRaises(LeagueError):verify_league(self.root,e)
    def test_repeated_unknown_resumes_cannot_exceed_attempt_cap(self):
        calls=[]
        def broken(c,s):calls.append(c['candidate_id']);raise RuntimeError('unresolved')
        broken.binding='broken-fixed';broken.deterministic_replay=True
        for _ in range(4):
            with self.assertRaises(LeagueError):run_league(self.root,config(),broken)
        self.assertEqual(len(calls),2)
    def test_completed_job_cannot_be_rolled_back_before_resume(self):
        for sql in ["update league_jobs set status='pending' where status='completed'","update league_jobs set status='unknown' where status='completed'","update league_jobs set result=null where status='completed'","delete from league_jobs where status='completed'"]:
            with self.subTest(sql=sql),tempfile.TemporaryDirectory()as root:
                e=Fake();run_league(root,config(),e,stop_after_jobs=1)
                db=sqlite3.connect(Path(root)/'campaign.sqlite');db.execute(sql);db.commit();db.close()
                with self.assertRaises(LeagueError):run_league(root,config(),e)
                self.assertEqual(e.calls,1)

if __name__=='__main__':unittest.main()
