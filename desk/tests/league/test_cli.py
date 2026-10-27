import json,sys,tempfile,types,unittest
from pathlib import Path
from league.cli import live_import_guard,current_binding_guard
from league.runtime import run_league,LeagueError
from test_runtime import Fake,config
class Tests(unittest.TestCase):
 def test_cached_foreign_backtest_module_refused(self):
  name='backtest.foreign_probe';old=sys.modules.get(name);module=types.ModuleType(name);module.__file__='/tmp/foreign-backtest.py';sys.modules[name]=module
  try:
   with self.assertRaises(LeagueError):live_import_guard(Path('/chosen-snapshot'))
  finally:
   if old is None:sys.modules.pop(name,None)
   else:sys.modules[name]=old
 def test_completed_run_binding_must_match(self):
  with tempfile.TemporaryDirectory()as root:
   evaluator=Fake();run_league(root,config(),evaluator);current_binding_guard(root,evaluator)
   evaluator.binding='wrong-backend'
   with self.assertRaises(LeagueError):current_binding_guard(root,evaluator)
if __name__=='__main__':unittest.main()
