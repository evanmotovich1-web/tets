"""Explicit foreground CLI; no scheduled services, broker or main-model trainer."""
import argparse,json,sys,sqlite3
from pathlib import Path
from .runtime import run_league,verify_league,LeagueError,require,filehash
from .worker_adapter import CampaignEvaluator
from .learning import export_learning,train_cpu_student,verify_student,LearningError

def live_import_guard(source):
 expected=(Path(source)/'backtest').resolve()
 for name,module in list(sys.modules.items()):
  if name=='backtest'or name.startswith('backtest.'):
   location=getattr(module,'__file__',None)
   require(location is not None and Path(location).resolve().is_relative_to(expected),'cached backtest module outside live desk')

def current_binding_guard(state,evaluator):
 from . import runtime
 db=sqlite3.connect('file:'+str((Path(state)/'campaign.sqlite').resolve())+'?mode=ro',uri=True)
 try:frozen=json.loads(db.execute("select v from league_meta where k='frozen'").fetchone()[0])
 finally:db.close()
 require(frozen['runtime_sha256']==filehash(Path(runtime.__file__))and frozen['evaluator_binding']==evaluator.binding,'current code/evaluator differs from completed run')

def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['run','verify','export','cpu-baseline','verify-student']);p.add_argument('--state',type=Path,required=True);p.add_argument('--desk',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--config',type=Path);p.add_argument('--pause-after',type=int);p.add_argument('--out',type=Path);p.add_argument('--dataset',type=Path);a=p.parse_args()
 try:
  if a.command in('run','verify'):
   live_import_guard(a.desk)
   e=CampaignEvaluator(a.state,a.desk)
   live_import_guard(a.desk)
   if a.command=='run':
    if not a.config:p.error('--config required')
    result=run_league(a.state,json.loads(a.config.read_text()),e,a.pause_after)
   else:
    current_binding_guard(a.state,e)
    result=verify_league(a.state,e)
  elif a.command=='export':
   if not a.out:p.error('--out required')
   e=CampaignEvaluator(a.state,a.desk);current_binding_guard(a.state,e);verify_league(a.state,e)
   rows=[json.loads(line)for line in(a.state/'experiences.jsonl').read_text().splitlines()]
   result=export_learning(rows,a.out)
  elif a.command=='cpu-baseline':
   if not a.dataset or not a.out:p.error('--dataset and --out required')
   result=train_cpu_student(a.dataset,a.out)
  else:result=verify_student(a.state)
  print(json.dumps(result,indent=2));return 0
 except(LeagueError,LearningError)as error:print(json.dumps(dict(error=str(error),main_model_trained=False)));return 2
if __name__=='__main__':raise SystemExit(main())
