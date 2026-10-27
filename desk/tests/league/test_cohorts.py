import hashlib,json,tempfile,unittest
from pathlib import Path
from league.runtime import run_league,verify_league,LeagueError,validate,population,world_group,world_seed
from league.learning import export_learning,train_cpu_student,LearningError

COHORTS=[dict(id='A',world_families=['trend_v1','regime_v1']),dict(id='B',world_families=['meanrev_v1'])]
def config(**extra):
    c=dict(experiment_id='cohort-league',generations=2,population=4,seeds=[11,23],seed=5,max_drawdown=.1,max_jobs=32,wall_seconds=30,cohorts=COHORTS);c.update(extra);return c
class Fake:
    binding='fake-development-v1';deterministic_replay=True
    def __init__(self):self.calls=[]
    def __call__(self,c,seed):
        self.calls.append((c['cohort'],seed));h=hashlib.sha256(json.dumps([c,seed],sort_keys=True).encode()).hexdigest()
        return dict(score=.01+c['genome']['fast_window']*.001-(seed%7)*.0001,max_drawdown=.01,receipt_sha256=h,manifest_sha256=h,replay_verified=True,classification='synthetic',partition='development')
    def verify(self,r):return True

class CohortTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_cohort_config_is_strict(self):
        validate(config())
        for bad in ([COHORTS[0]],[dict(id='A',world_families=['trend_v1']),dict(id='B',world_families=['trend_v1'])],
                    [dict(id='A',world_families=['nope']),COHORTS[1]],[COHORTS[0],dict(id='A',world_families=['meanrev_v1'])]):
            with self.subTest(bad=bad),self.assertRaises(LeagueError):validate(config(cohorts=bad))
    def test_offspring_inherit_cohort_and_crossing_is_refused(self):
        c=config();g0=population(c,0,[])
        self.assertEqual([x['cohort']for x in g0],['A','B','A','B'])
        g1=population(c,1,g0[:2])
        for child in g1:
            if child['parents']:self.assertEqual(child['cohort'],next(p['cohort']for p in g0 if p['candidate_id']==child['parents'][0]))
        with self.assertRaises(LeagueError):population(c,1,[dict(g0[0],cohort='Z')])
    def test_each_cohort_runs_only_its_own_world_families(self):
        e=Fake();r=run_league(self.root,config(),e);self.assertEqual(r['status'],'completed');verify_league(self.root,e)
        fam={'A':{world_seed('trend_v1',11),world_seed('trend_v1',23),world_seed('regime_v1',11),world_seed('regime_v1',23)},'B':{world_seed('meanrev_v1',11),world_seed('meanrev_v1',23)}}
        for cohort,seed in e.calls:self.assertIn(seed,fam[cohort])
        sealed=json.loads((self.root/'cohorts.json').read_text());self.assertTrue(sealed['assigned_before_any_job']);self.assertEqual(sealed['cohorts'],COHORTS)
    def test_disjoint_cohorts_give_a_trainable_split_and_one_family_still_refuses(self):
        run_league(self.root,config(),Fake())
        rows=[json.loads(l)for l in (self.root/'experiences.jsonl').read_text().splitlines()]
        self.assertEqual({r['world_group']for r in rows},{'synthetic-family-v1','synthetic-family-regime_v1','synthetic-family-meanrev_v1'})
        with tempfile.TemporaryDirectory()as out:
            m=export_learning(rows,out);self.assertGreaterEqual(m['components'],2);self.assertTrue(m['training_eligible'])
            s=train_cpu_student(Path(out)/'records.jsonl',Path(out)/'student');self.assertFalse(s['main_model_trained'])
        legacy=[dict(r,world_group='synthetic-family-v1')for r in rows]
        with tempfile.TemporaryDirectory()as out:
            m=export_learning(legacy,out);self.assertEqual(m['components'],1);self.assertFalse(m['training_eligible'])
            with self.assertRaises(LearningError):train_cpu_student(Path(out)/'records.jsonl',Path(out)/'student')
    def test_cohort_change_after_start_is_refused(self):
        run_league(self.root,config(),Fake(),stop_after_jobs=1)
        swapped=[dict(id='A',world_families=['meanrev_v1']),dict(id='B',world_families=['trend_v1','regime_v1'])]
        with self.assertRaisesRegex(LeagueError,'cohort_assignment_changed'):run_league(self.root,config(cohorts=swapped),Fake())
    def test_legacy_worlds_keep_their_identity(self):
        self.assertEqual(world_seed('trend_v1',11),11);self.assertEqual(world_group(11),'synthetic-family-v1')

if __name__=='__main__':unittest.main()
