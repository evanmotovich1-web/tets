import json,tempfile,unittest
from pathlib import Path
from league.runtime import run_league
from tests.league.test_cohorts import Fake,config

class AuthorshipTests(unittest.TestCase):
    def test_host_genomes_are_never_model_authors(self):
        with tempfile.TemporaryDirectory()as root:
            r=run_league(root,config(),Fake())
            self.assertEqual(r['agent_kind'],'strategy_genome');self.assertFalse(r['main_model_trained'])
            rows=[json.loads(l)for l in (Path(root)/'experiences.jsonl').read_text().splitlines()]
            self.assertTrue(rows);self.assertEqual({x['origin']for x in rows}-{'host_seed','host_mutation'},set())
            for g in sorted((Path(root)/'generations').glob('*.json')):
                for c in json.loads(g.read_text())['population']:
                    self.assertIn(c['origin'],('host_seed','host_mutation'));self.assertNotIn('agent_written',c)

if __name__=='__main__':unittest.main()
