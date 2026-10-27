"""Canonical-store integration checks, in addition to the migrated 30 tests."""
from contextlib import closing
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from league.runtime import LeagueError, run_league, verify_league
from league.worker_adapter import CampaignEvaluator


class LiveBindingTests(unittest.TestCase):
    def test_frozen_fleet_provenance_reaches_native_campaign(self):
        with tempfile.TemporaryDirectory() as state:
            root = Path(state)
            evaluator = CampaignEvaluator(root)
            spec = evaluator.strategy_spec(dict(schema_version=2, family='sma_cross_long', symbol='SYNTH',
                parameters=dict(fast_window=3, slow_window=12), position=dict(quantity='1'),
                execution=dict(order_type='market', limit_offset_bps='0'), rationale='Fixture author, not a real model.'))
            process = subprocess.run(['node', 'tests/fixtures/league-fleet.mjs', state, json.dumps(spec)],
                cwd=evaluator.desk, capture_output=True, text=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = evaluator(dict(candidate_id='fleet-slot', genome=spec['parameters'],
                fleet=dict(stateDir=state, campaignId='league-fleet-proof', attempt=3)), 11)
            self.assertTrue(evaluator.verify(result))
            summary = json.loads((root/'campaigns/league-fleet-slot-11/summary.json').read_text())
            episode = json.loads(Path(summary['episodes']['path']).read_text())
            self.assertEqual(episode['worker']['provider'], 'fixture')
            self.assertEqual(episode['worker']['slot'], 'pi')
            self.assertFalse(episode['worker']['agent_written'])

    def test_live_store_restart_reuses_native_admission_and_exports_native_ids(self):
        class Interrupted(CampaignEvaluator):
            interrupt = True

            def __call__(self, candidate, seed):
                result = super().__call__(candidate, seed)
                if self.interrupt:
                    self.interrupt = False
                    raise RuntimeError('crash after native commit, before league projection')
                return result

        with tempfile.TemporaryDirectory() as state:
            root = Path(state)
            evaluator = Interrupted(root)
            config = dict(experiment_id='canonical-recovery', generations=1, population=4,
                          seeds=[11], seed=5, max_drawdown=.05, max_jobs=4, wall_seconds=180)
            with self.assertRaises(LeagueError):
                run_league(root, config, evaluator)
            with closing(sqlite3.connect(root/'campaign.sqlite')) as db:
                self.assertEqual(db.execute('select count(*) from campaign_journal where seq=0').fetchone()[0], 1)
            result = run_league(root, config, evaluator)
            self.assertEqual(result['completed_jobs'], 4)
            self.assertEqual(verify_league(root, evaluator), result)
            self.assertEqual(list(root.rglob('league.sqlite')), [])
            self.assertEqual(list(root.rglob('campaign.sqlite')), [root/'campaign.sqlite'])
            self.assertFalse((root/'verified').exists())
            rows = [json.loads(line) for line in (root/'experiences.jsonl').read_text().splitlines()]
            native_ids = []
            for summary_path in root.glob('campaigns/*/summary.json'):
                summary = json.loads(summary_path.read_text())
                self.assertEqual(summary['N'], summary['ledger_decision_count'])
                self.assertEqual(summary['N'], 1)
                self.assertFalse(summary['orders'])
                self.assertFalse(summary['paper'])
                self.assertFalse(summary['candidates'][0]['verdict']['passed'])
                episode = json.loads(Path(summary['episodes']['path']).read_text())
                native_ids.append(episode['decision_id'])
            self.assertEqual(sorted(r['decision_id'] for r in rows), sorted(native_ids))
            with closing(sqlite3.connect(root/'campaign.sqlite')) as db:
                self.assertEqual(db.execute('select count(*) from campaign_journal where seq=0').fetchone()[0], 4)
                # A projection cannot invent a passing result or hide a native attempt.
                db.execute("update campaign_journal set payload='{}' where rowid=(select min(rowid) from campaign_journal)");db.commit()
            with self.assertRaises(LeagueError):
                verify_league(root, evaluator)

    def test_foreign_snapshot_refused(self):
        with tempfile.TemporaryDirectory() as state:
            with self.assertRaisesRegex(LeagueError, 'live canonical desk'):
                CampaignEvaluator(state, '/tmp/old-source-snapshot')

    def test_live_source_drift_refused(self):
        with tempfile.TemporaryDirectory() as state:
            evaluator = CampaignEvaluator(state)
            self.assertIn('runtime/author-fleet.mjs', evaluator.source_files)
            self.assertIn('runtime/campaign.mjs', evaluator.source_files)
            self.assertIn('runtime/episodes.mjs', evaluator.source_files)
            self.assertIn('backtest/walkforward.py', evaluator.source_files)
            evaluator.source_files['runtime/campaign.mjs'] = '0'*64
            with self.assertRaisesRegex(LeagueError, 'source drift'):
                evaluator.check_source()


if __name__ == '__main__':
    unittest.main()
