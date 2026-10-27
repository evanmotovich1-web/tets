import copy,json,tempfile,unittest
from pathlib import Path
from league.learning import export_learning,train_cpu_student,verify_student,LearningError

def row(i,score=.01):return dict(decision_id='d'+str(i),candidate_id='c'+str(i),lineage_id='l'+str(i),world_group='w'+str(i),partition='development',classification='synthetic',observation=dict(fast_window=2+i%5,slow_window=8+i),outcome=dict(status='completed',score=score,replay_verified=True,receipt_sha256='a'*64),origin='host_seed')
class Tests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def test_loss_failure_and_unknown_are_retained(self):
  rows=[row(0,-.1),row(1)];r=row(2);r['outcome']=dict(status='unknown',score=None,replay_verified=False,receipt_sha256=None);rows.append(r)
  m=export_learning(rows,self.root);self.assertEqual(m['rows'],3);self.assertEqual(m['nonpositive'],1);self.assertEqual(m['unknown'],1)
 def test_duplicate_conflict(self):
  a=row(0);b=row(0,.2)
  with self.assertRaises(LearningError):export_learning([a,b],self.root)
 def test_extra_and_hidden_labels_reject(self):
  for edit in [lambda r:r.update(partition='holdout'),lambda r:r['observation'].update(reward=1),lambda r:r['outcome'].update(score=float('nan')),lambda r:r['outcome'].update(replay_verified=False)]:
   r=row(0);edit(r)
   with self.assertRaises(LearningError):export_learning([r],self.root)
 def test_unknown_score_rejected(self):
  r=row(0);r['outcome']['status']='unknown'
  with self.assertRaises(LearningError):export_learning([r],self.root)
 def test_linked_groups_cannot_cross_split(self):
  rows=[row(i)for i in range(4)];rows[1]['lineage_id']=rows[0]['lineage_id'];rows[2]['world_group']=rows[1]['world_group']
  m=export_learning(rows,self.root);splits=json.loads((self.root/'splits.json').read_text());self.assertEqual(splits['d0'],splits['d1']);self.assertEqual(splits['d1'],splits['d2']);self.assertEqual(m['components'],2)
 def test_shared_world_family_refuses_student_training(self):
  rows=[row(i)for i in range(4)]
  for r in rows:r['world_group']='one-family'
  export_learning(rows,self.root/'data')
  with self.assertRaises(LearningError):train_cpu_student(self.root/'data/records.jsonl',self.root/'student')
 def test_actual_cpu_training_reload_and_determinism(self):
  export_learning([row(i,.01*(i+1))for i in range(6)],self.root/'data')
  m=train_cpu_student(self.root/'data/records.jsonl',self.root/'student');self.assertTrue(m['parameters_changed']);self.assertFalse(m['main_model_trained']);self.assertEqual(verify_student(self.root/'student'),m)
  n=train_cpu_student(self.root/'data/records.jsonl',self.root/'student2');self.assertEqual(m,n)
 def test_tampered_checkpoint_rejected(self):
  export_learning([row(i,.01*(i+1))for i in range(6)],self.root/'data');train_cpu_student(self.root/'data/records.jsonl',self.root/'student')
  p=self.root/'student/checkpoint.json';x=json.loads(p.read_text());x['weights'][0]+=1;p.write_text(json.dumps(x))
  with self.assertRaises(LearningError):verify_student(self.root/'student')
 def test_tampered_source_data_rejected(self):
  export_learning([row(i)for i in range(4)],self.root/'data');p=self.root/'data/records.jsonl';p.write_text(p.read_text()+'{}\n')
  with self.assertRaises(LearningError):train_cpu_student(p,self.root/'student')
 def test_nonfinite_unknown_extra_fields_reject(self):
  for extra in [dict(secret='x'),dict(promoting=True)]:
   r=row(0);r.update(extra)
   with self.assertRaises(LearningError):export_learning([r],self.root)
 def test_metadata_only_student_tampering_rejected(self):
  for key,value in [('evaluation_mae',999),('constant_baseline_mae',999),('train_rows',999),('evaluation_rows',999),('parameters_changed',False)]:
   with self.subTest(key=key),tempfile.TemporaryDirectory()as temp:
    root=Path(temp);export_learning([row(i,.01*(i+1))for i in range(6)],root/'data');train_cpu_student(root/'data/records.jsonl',root/'student')
    p=root/'student/student-manifest.json';m=json.loads(p.read_text());m[key]=value;p.write_text(json.dumps(m))
    with self.assertRaises(LearningError):verify_student(root/'student')
 def test_candidate_identity_cannot_cross_groups(self):
  a,b=row(0),row(1);b['candidate_id']=a['candidate_id']
  with self.assertRaises(LearningError):export_learning([a,b],self.root)
 def test_dataset_counter_tampering_rejected(self):
  export_learning([row(i)for i in range(4)],self.root/'data');p=self.root/'data/dataset-manifest.json';m=json.loads(p.read_text());m['completed']=999;p.write_text(json.dumps(m))
  with self.assertRaises(LearningError):train_cpu_student(self.root/'data/records.jsonl',self.root/'student')

if __name__=='__main__':unittest.main()
