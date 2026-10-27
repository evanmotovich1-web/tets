import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backtest.contracts import canonical
from backtest.marketlab_bridge import find_marketlab, run_study
from backtest.worker import Worker


class MarketLabBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = Worker(self.root / "registry.json", self.root / "artifacts")

    def test_missing_checkout_is_unavailable(self):
        with patch.dict(os.environ, {"MARKETLAB_ROOT": str(self.root / "absent")}, clear=False):
            os.environ["MARKETLAB_ROOT"] = str(self.root / "absent")
            self.assertIsNone(find_marketlab())
            response = self.worker.handle_raw(canonical({"op": "run_marketlab_study"}))
        self.assertFalse(response["ok"])
        self.assertIn("marketlab_unavailable", response["result"]["reason_codes"])
        self.assertIs(response["result"]["promoting"], False)
        self.assertIsNone(response["result"]["metrics"])

    def test_stub_study_stays_research_only(self):
        lab = self.root / "marketlab-src"
        pkg = lab / "marketlab"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "__main__.py").write_text(
            "from pathlib import Path\n"
            "import json\n"
            "out = Path('out')\n"
            "out.mkdir(exist_ok=True)\n"
            "out.joinpath('report.results.json').write_text(json.dumps({\n"
            "  'n_seeds_per_personality': 1,\n"
            "  'rows': [{'strategy': 'Vol Target 15%', 'personality': 'mean-rev', 'verdict': 'PASS'}]\n"
            "}) + '\\n')\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"MARKETLAB_ROOT": str(lab)}):
            metrics = run_study(1)
            response = self.worker.handle_raw(canonical({"op": "run_marketlab_study", "n_seeds": 1}))
        self.assertEqual(metrics["pass_count"], 1)
        self.assertIs(metrics["promoting"], False)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["qualification"], "research_only")
        self.assertIs(response["result"]["promoting"], False)
        self.assertEqual(response["result"]["metrics"]["verdicts"][0]["verdict"], "PASS")
