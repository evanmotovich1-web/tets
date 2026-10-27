from contextlib import closing
import json,os,tempfile,unittest
from pathlib import Path
from league.worker_adapter import CampaignEvaluator
from league.runtime import LeagueError
SOURCE=Path(__file__).resolve().parents[2]
class AdapterTests(unittest.TestCase):
    def test_real_campaign_receipt_replay_and_tamper(self):
        with tempfile.TemporaryDirectory()as root:
            e=CampaignEvaluator(root,SOURCE);c=dict(candidate_id='smoke-candidate',genome=dict(fast_window=3,slow_window=12),parents=[],origin='host_seed',generation=0)
            result=e(c,11);self.assertTrue(e.verify(result));self.assertEqual(result['classification'],'synthetic')
            duplicate=dict(c,candidate_id='same-genome-another-agent');again=e(duplicate,11)
            self.assertEqual(result['receipt_sha256'],again['receipt_sha256']);self.assertTrue(e.verify(again))
            import sqlite3
            with closing(sqlite3.connect(Path(root)/'campaign.sqlite')) as db:
                event=json.loads(db.execute("select payload from campaign_journal where seq=0 order by campaign_id limit 1").fetchone()[0])
            p=Path(event['job_dir'])/'result.json';p.write_text(p.read_text()+' ')
            with self.assertRaises(LeagueError):e.verify(result)
if __name__=='__main__':unittest.main()
