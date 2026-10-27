"""Rewrite a copied fixture so one bar is knowable only after its close."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backtest.contracts import canonical, sealed, sha_bytes

root = Path(sys.argv[1])
rows = [json.loads(line) for line in (root / "sma-four-bars.jsonl").read_text().splitlines()]
rows[1]["available_at"] = "2024-01-10T16:00:00.000Z"
data = b"".join(canonical(row) + b"\n" for row in rows)
(root / "sma-four-bars.jsonl").write_bytes(data)
manifest = json.loads((root / "manifest.json").read_text())
manifest.pop("manifest_sha256")
manifest["data_sha256"] = sha_bytes(data)
manifest["row_count"] = len(rows)
manifest["source"]["snapshot_sha256"] = sha_bytes(data)
manifest = sealed(manifest, "manifest_sha256")
(root / "manifest.json").write_bytes(canonical(manifest))
registry = json.loads((root / "registry.json").read_text())
registry["datasets"]["synthetic:sma-four-bars:v1"]["manifest_sha256"] = manifest["manifest_sha256"]
(root / "registry.json").write_bytes(canonical(registry))
