"""Seal task changes. Final commit SHA is bound by a post-commit external receipt."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent

def main():
    paths = set()
    for args in (["diff", "--name-only", "-z"], ["diff", "--cached", "--name-only", "-z"], ["ls-files", "--others", "--exclude-standard", "-z"]):
        paths.update(filter(None, subprocess.check_output(["git", *args], cwd=ROOT).decode().split("\0")))
    excluded = {str((OUT / name).relative_to(ROOT)) for name in ("manifest.json", "manifest.sha256", "delivery-receipt.json")}
    paths -= excluded
    assert paths
    manifest = {"schema": "dp02.delivery-manifest.v1", "task": "DP-02-20261016",
                "baseline_head": json.loads(OUT.joinpath("00-baseline.json").read_text())["main_head"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "head_binding": "Local commit containing this manifest; exact final SHA in post-commit delivery-receipt.json",
                "excludes": sorted(excluded), "reason": "No self hash; external receipt binds final HEAD and manifest sha256",
                "files": [{"path": name, "bytes": (ROOT / name).stat().st_size,
                           "sha256": hashlib.sha256((ROOT / name).read_bytes()).hexdigest()} for name in sorted(paths)]}
    file = OUT / "manifest.json"
    file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    OUT.joinpath("manifest.sha256").write_text(digest + "  manifest.json\n")
    print(json.dumps({"files": len(paths), "manifest_sha256": digest}))

if __name__ == "__main__":
    main()
