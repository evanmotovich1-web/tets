"""Phase 1 acceptance: agent-written spec, frozen development bars, independent replay."""
from pathlib import Path
import json
import os
import unittest

PROOF = Path(os.environ.get(
    'PHASE1_PROOF_DIR',
    Path(__file__).resolve().parents[2] / 'runtime/.state/phase1-hermes-20261025'))


class Phase1AuthenticTrialTests(unittest.TestCase):
    def setUp(self):
        self.status_path = PROOF / 'author-status.json'
        self.verification_path = PROOF / 'verification.json'
        if not self.status_path.is_file() or not self.verification_path.is_file():
            self.skipTest(f'phase1 proof not produced yet: {PROOF}')
        self.status = json.loads(self.status_path.read_text())
        self.summary = json.loads(self.verification_path.read_text())

    def test_author_is_agent_written_cli_not_fixture(self):
        self.assertTrue(self.status['available'])
        self.assertTrue(self.status['agent_written'])
        self.assertEqual(self.status['provenance']['transport'], 'cli')
        self.assertTrue(self.status['provenance']['agent_written'])
        self.assertRegex(self.status['provenance']['raw_response_sha256'], r'^[0-9a-f]{64}$')
        self.assertRegex(self.status['prompt_sha256'], r'^[0-9a-f]{64}$')
        self.assertRegex(self.status['strategy_sha256'], r'^[0-9a-f]{64}$')

    def test_receipt_stays_research_only(self):
        self.assertTrue(self.summary['agent_written_proven'])
        self.assertTrue(self.summary['research_only'])
        self.assertFalse(self.summary['promoting'])
        for trial in self.summary['trials'].values():
            self.assertTrue(trial['independent_match'])
            self.assertTrue(trial['request_binding_match'])
            self.assertGreater(trial['fills'], 0)

    def test_window_mutation_changes_actual_fills(self):
        mutation = self.summary['mutation']
        self.assertNotEqual(mutation['baseline_windows'], mutation['mutated_windows'])
        self.assertTrue(
            mutation['fill_times_changed']
            or mutation['baseline_fills'] != mutation['mutated_fills'])

    def test_dataset_is_development_hindsight_only(self):
        data = self.summary['data']
        self.assertEqual(data['classification'], 'historical_unverified')
        self.assertEqual(data['partition'], 'development')
        self.assertFalse(data['point_in_time_verified'])
        self.assertGreaterEqual(data['bars'], 20)


if __name__ == '__main__':
    unittest.main()
