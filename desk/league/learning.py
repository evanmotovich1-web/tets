"""Loss-inclusive development export and separate CPU score-regression baseline.
This is NOT main-model/LLM distillation and cannot promote a checkpoint.
"""
from pathlib import Path
import json,math,re,hashlib,tempfile

class LearningError(ValueError):pass
def need(ok,msg):
 if not ok:raise LearningError(msg)
def text(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)
def h(data):return hashlib.sha256(data).hexdigest()
def fh(p):return h(p.read_bytes())
def exact(v,keys):need(type(v)is dict and set(v)==set(keys),'unknown/missing learning fields')
def save(p,v):
 p.parent.mkdir(parents=True,exist_ok=True);data=text(v)+'\n'
 if p.exists():need(p.read_text()==data,'immutable learning output changed')
 else:p.write_text(data)
def validated(r):
 exact(r,['decision_id','candidate_id','lineage_id','world_group','partition','classification','observation','outcome','origin'])
 for k in ['decision_id','candidate_id','lineage_id','world_group']:need(type(r[k])is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',r[k]),'invalid group/decision identity')
 need(r['partition']=='development'and r['classification']=='synthetic','training partition denied')
 need(r['origin']in('host_seed','host_mutation','real_author'),'unknown source origin')
 exact(r['observation'],['fast_window','slow_window']);fast=r['observation']['fast_window'];slow=r['observation']['slow_window']
 need(type(fast)is int and type(slow)is int and 1<=fast<slow<=200,'invalid observation')
 o=r['outcome'];exact(o,['status','score','replay_verified','receipt_sha256'])
 need(o['status']in('completed','failed','unknown','rejected')and type(o['replay_verified'])is bool,'invalid outcome')
 need(o['receipt_sha256']is None or type(o['receipt_sha256'])is str and re.fullmatch('[0-9a-f]{64}',o['receipt_sha256']),'bad receipt reference')
 if o['status']=='completed':need(type(o['score'])in(int,float)and math.isfinite(o['score'])and o['replay_verified']is True and o['receipt_sha256']is not None,'unverified score')
 else:need(o['score']is None and o['replay_verified']is False,'unresolved outcome cannot have score')
 return r

def group_split(rows):
 parent={r['decision_id']:r['decision_id']for r in rows}
 def find(x):
  while parent[x]!=x:parent[x]=parent[parent[x]];x=parent[x]
  return x
 seen={}
 for r in rows:
  for key in ['candidate_id','lineage_id','world_group']:
   identity=(key,r[key])
   if identity in seen:
    a,b=find(r['decision_id']),find(seen[identity]);parent[max(a,b)]=min(a,b)
   else:seen[identity]=r['decision_id']
 groups=sorted(set(find(x)for x in parent));evaluation=groups[-1]if len(groups)>1 else None
 return {r['decision_id']:('evaluation'if find(r['decision_id'])==evaluation else 'train')for r in rows},len(groups)

def dataset_manifest(root,rows,input_rows,components):
 return dict(kind='synthetic_development_learning_export',input_rows=len(input_rows),rows=len(rows),duplicates=len(input_rows)-len(rows),components=components,training_eligible=components>=2,completed=sum(r['outcome']['status']=='completed'for r in rows),unknown=sum(r['outcome']['status']=='unknown'for r in rows),nonpositive=sum(r['outcome']['score']is not None and r['outcome']['score']<=0 for r in rows),input_sha256=fh(root/'source-records.jsonl'),data_sha256=fh(root/'records.jsonl'),splits_sha256=fh(root/'splits.json'),source_sha256=fh(Path(__file__)),main_model_trained=False,promoting=False)

def export_learning(records,out_dir):
 need(type(records)is list and len(records)<=100000,'unbounded learning input');unique={};identities={}
 for raw in records:
  r=validated(raw);identity=(r['lineage_id'],r['observation'],r['origin']);need(r['candidate_id']not in identities or identities[r['candidate_id']]==identity,'contradictory candidate identity');identities[r['candidate_id']]=identity
  old=unique.get(r['decision_id']);need(old is None or old==r,'conflicting duplicate decision');unique[r['decision_id']]=r
 rows=[unique[k]for k in sorted(unique)];splits,components=group_split(rows);root=Path(out_dir);root.mkdir(parents=True,exist_ok=True)
 input_data=''.join(text(r)+'\n'for r in records).encode();input_path=root/'source-records.jsonl'
 if input_path.exists():need(input_path.read_bytes()==input_data,'source learning input changed')
 else:input_path.write_bytes(input_data)
 data=''.join(text(r)+'\n'for r in rows).encode();p=root/'records.jsonl'
 if p.exists():need(p.read_bytes()==data,'dataset output changed')
 else:p.write_bytes(data)
 save(root/'splits.json',splits)
 m=dataset_manifest(root,rows,records,components)
 save(root/'dataset-manifest.json',m);return m

def load(path):
 path=Path(path);m=json.loads((path.parent/'dataset-manifest.json').read_text())
 need(fh(path)==m['data_sha256']and fh(path.parent/'splits.json')==m['splits_sha256'],'learning dataset integrity failure')
 rows=[validated(json.loads(line))for line in path.read_text().splitlines()];need(len(rows)==m['rows']and len({r['decision_id']for r in rows})==len(rows),'dataset denominator mismatch')
 input_rows=[validated(json.loads(line))for line in(path.parent/'source-records.jsonl').read_text().splitlines()];unique={};identities={}
 for r in input_rows:
  need(r['decision_id']not in unique or unique[r['decision_id']]==r,'conflicting source duplicate');unique[r['decision_id']]=r
  identity=(r['lineage_id'],r['observation'],r['origin']);need(r['candidate_id']not in identities or identities[r['candidate_id']]==identity,'contradictory candidate metadata');identities[r['candidate_id']]=identity
 need(rows==[unique[k]for k in sorted(unique)],'source/deduplicated rows disagree')
 splits,components=group_split(rows);need(splits==json.loads((path.parent/'splits.json').read_text()),'group split integrity failure')
 need(m==dataset_manifest(path.parent,rows,input_rows,components),'dataset manifest claims disagree with evidence')
 return rows,splits,m

def features(r):return [1.,r['observation']['fast_window']/200.,r['observation']['slow_window']/200.]
def predict(weights,r):return sum(a*b for a,b in zip(weights,features(r)))

def train_cpu_student(dataset_path,out_dir):
 rows,splits,data_manifest=load(dataset_path);need(data_manifest['components']>=2,'shared ancestry/world family prevents independent split')
 verified=[r for r in rows if r['outcome']['status']=='completed'];train=[r for r in verified if splits[r['decision_id']]=='train'];evaluation=[r for r in verified if splits[r['decision_id']]=='evaluation']
 need(len(train)>=2 and len(evaluation)>=1,'insufficient verified split examples')
 weights=[0.,0.,0.]
 for _ in range(400):
  gradient=[0.,0.,0.]
  for r in train:
   err=predict(weights,r)-r['outcome']['score']
   for i,x in enumerate(features(r)):gradient[i]+=err*x/len(train)
  weights=[w-.1*(g+.001*w)for w,g in zip(weights,gradient)]
 need(all(math.isfinite(w)for w in weights)and any(abs(w)>1e-12 for w in weights),'CPU baseline did not change parameters')
 baseline=sum(r['outcome']['score']for r in train)/len(train)
 predictions=[dict(decision_id=r['decision_id'],prediction=predict(weights,r),target=r['outcome']['score'])for r in evaluation]
 root=Path(out_dir);root.mkdir(parents=True,exist_ok=True)
 checkpoint=dict(kind='cpu_strategy_score_regressor',algorithm='deterministic_ridge_gradient_v1',weights=weights,iterations=400,main_model_trained=False,promoting=False)
 save(root/'checkpoint.json',checkpoint);save(root/'predictions.json',predictions)
 # Copy all verified dataset bytes; the baseline remains reloadable without a mutable external path.
 for name in ['source-records.jsonl','records.jsonl','splits.json','dataset-manifest.json']:
  data=(Path(dataset_path).parent/name).read_bytes();p=root/'data'/name;p.parent.mkdir(parents=True,exist_ok=True)
  if p.exists():need(p.read_bytes()==data,'student source data changed')
  else:p.write_bytes(data)
 manifest=dict(kind='cpu_baseline_training_result',checkpoint_sha256=fh(root/'checkpoint.json'),predictions_sha256=fh(root/'predictions.json'),dataset_sha256=data_manifest['data_sha256'],source_sha256=fh(Path(__file__)),train_rows=len(train),evaluation_rows=len(evaluation),parameters_changed=True,evaluation_mae=sum(abs(p['prediction']-p['target'])for p in predictions)/len(predictions),constant_baseline_mae=sum(abs(baseline-r['outcome']['score'])for r in evaluation)/len(evaluation),main_model_trained=False,financial_edge_proven=False,promoting=False)
 save(root/'student-manifest.json',manifest);return manifest

def verify_student(out_dir):
 root=Path(out_dir);m=json.loads((root/'student-manifest.json').read_text())
 need(fh(root/'checkpoint.json')==m['checkpoint_sha256']and fh(root/'predictions.json')==m['predictions_sha256']and fh(Path(__file__))==m['source_sha256'],'student checkpoint integrity failure')
 rows,splits,data_manifest=load(root/'data/records.jsonl');need(data_manifest['data_sha256']==m['dataset_sha256'],'student dataset mismatch')
 weights=json.loads((root/'checkpoint.json').read_text())['weights'];need(len(weights)==3 and all(type(w)in(int,float)and math.isfinite(w)for w in weights),'invalid CPU weights')
 evaluation=[r for r in rows if r['outcome']['status']=='completed'and splits[r['decision_id']]=='evaluation']
 expected=[dict(decision_id=r['decision_id'],prediction=predict(weights,r),target=r['outcome']['score'])for r in evaluation]
 need(expected==json.loads((root/'predictions.json').read_text()),'student reload prediction disagreement')
 need(m['main_model_trained']is False and m['promoting']is False,'student authority mismatch')
 # Replay this tiny deterministic CPU fit to reconstruct all reported counts/MAE/change claims.
 with tempfile.TemporaryDirectory(prefix='cpu-student-verify-')as tmp:
  expected_manifest=train_cpu_student(root/'data/records.jsonl',Path(tmp))
 need(m==expected_manifest,'student measurement metadata disagreement')
 return m
