"""Fail-closed MarketLab study runner. Does not reimplement MarketLab."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from .contracts import Rejected, integer, require


def find_marketlab() -> Path | None:
    env = os.environ.get("MARKETLAB_ROOT")
    if env:
        root = Path(env).expanduser()
        return root if (root / "marketlab" / "__main__.py").is_file() else None
    homes = [Path.home() / "code" / "marketlab", Path("/Users/evanmotovich/code/marketlab")]
    for root in homes:
        if (root / "marketlab" / "__main__.py").is_file():
            return root
    return None


def _python(root: Path) -> Path:
    local = root / ".venv" / "bin" / "python"
    return local if local.is_file() else Path(sys.executable)


def run_study(n_seeds=1):
    seeds = integer(n_seeds, 1, 5)
    located = find_marketlab()
    require(located is not None, "marketlab_unavailable")
    root = located
    out = root / "out"
    out.mkdir(exist_ok=True)
    proc = subprocess.run(
        [str(_python(root)), "-m", "marketlab", str(seeds)],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", "C"),
        },
    )
    if proc.returncode != 0:
        raise Rejected("marketlab_run_failed")
    report = root / "out" / "report.results.json"
    require(report.is_file(), "marketlab_results_missing")
    payload = json.loads(report.read_text(encoding="utf-8"))
    require(type(payload) is dict, "marketlab_results_invalid")
    rows = payload.get("rows")
    if rows is None:
        rows = payload.get("results")
    require(type(rows) is list and rows, "marketlab_results_invalid")
    verdicts = []
    for row in rows:
        require(type(row) is dict, "marketlab_results_invalid")
        verdicts.append({
            "strategy": row["strategy"] if "strategy" in row else None,
            "personality": row["personality"] if "personality" in row else None,
            "verdict": row["verdict"] if "verdict" in row else None,
        })
    return {
        "engine": "marketlab",
        "root": str(root),
        "n_seeds": seeds,
        "verdicts": verdicts,
        "pass_count": sum(1 for item in verdicts if item.get("verdict") == "PASS"),
        "fail_count": sum(1 for item in verdicts if item.get("verdict") == "FAIL"),
        "qualification": "research_only",
        "promoting": False,
    }
