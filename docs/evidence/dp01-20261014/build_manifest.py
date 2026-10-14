"""Freeze changed code and evidence; external final receipt binds commit SHA."""
import datetime
import hashlib
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[3]
EVIDENCE = pathlib.Path(__file__).parent
EXCLUDED = {str((EVIDENCE / name).relative_to(ROOT)) for name in (
    "manifest.json", "manifest.sha256", "delivery-receipt.json")}
changed = subprocess.check_output(["git", "diff", "--name-only", "-z"], cwd=ROOT).decode().split("\0")
new = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
staged = subprocess.check_output(["git", "diff", "--cached", "--name-only", "-z"], cwd=ROOT).decode().split("\0")
paths = sorted(set(filter(None, changed + new + staged)) - EXCLUDED)
files = [{"path": rel, "bytes": (ROOT / rel).stat().st_size,
          "sha256": hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()} for rel in paths]
assert files
manifest = {"schema": "dp01.delivery-manifest.v1", "task": "DP-01-20261014",
            "baseline_head": "e472227fca5cb53d56caddab54f2dd3a93509056",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "head_binding": "Local commit containing manifest; final SHA recorded in external delivery-receipt.json",
            "excludes": sorted(EXCLUDED),
            "exclusion_reason": "Manifest/hash cannot hash themselves; post-commit receipt binds final HEAD and this manifest hash",
            "files": files}
path = EVIDENCE / "manifest.json"
path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
digest = hashlib.sha256(path.read_bytes()).hexdigest()
EVIDENCE.joinpath("manifest.sha256").write_text(digest + "  manifest.json\n")
print(json.dumps({"file_count": len(files), "manifest_sha256": digest}))
