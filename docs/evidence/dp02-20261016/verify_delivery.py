"""Scoped verification and manifest seal; never reads credentials or runtime DBs."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent

def git(*args, cwd=ROOT):
    return subprocess.check_output(["git", *args], cwd=cwd)

def main():
    commands = []
    for label, argv in [
        ("compileall", [sys.executable, "-m", "compileall", "-q", "ym_stock_data", "scripts", "tests", "docs/evidence/dp02-20261016"]),
        ("diff-check", ["git", "diff", "HEAD", "--check", "--", ".", ":(exclude)docs/evidence/dp02-20261016/*.stderr", ":(exclude)docs/evidence/dp02-20261016/*.patch", ":(exclude)docs/evidence/dp02-20261016/historical/**"]),
        ("gateway-patch-check", ["git", "-C", "/Users/yimu/Projects/YM_Capital/live-dashboard", "apply", "--check", str(OUT / "handoff-gateway.patch")]),
        ("bots-patch-check", ["git", "-C", "/Users/yimu/Projects/YM_Capital/_wt/tb06-20261016/trading-bots", "apply", "--check", str(OUT / "handoff-bots.patch")]),
    ]:
        started = datetime.now(timezone.utc).isoformat()
        tick = time.monotonic()
        result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)
        OUT.joinpath(label + ".stdout").write_text(result.stdout)
        OUT.joinpath(label + ".stderr").write_text(result.stderr)
        commands.append({"argv": argv, "started_at": started, "elapsed_ms": int((time.monotonic() - tick) * 1000),
                         "exit_code": result.returncode, "stdout": label + ".stdout", "stderr": label + ".stderr"})
        assert result.returncode == 0, label
    baseline = json.loads(OUT.joinpath("baseline-ids.json").read_text())
    final = json.loads(OUT.joinpath("final-ids.json").read_text())
    before, after = baseline["outcomes"], final["outcomes"]
    comparison = {"baseline_tests_run": baseline["tests_run"], "final_tests_run": final["tests_run"],
                  "new_failures": [key for key, value in after.items() if value in {"failed", "error"} and before.get(key) not in {"failed", "error"}],
                  "lost_prior_passes": [key for key, value in before.items() if value == "passed" and after.get(key) != "passed"],
                  "missing_ids": sorted(set(before) - set(after)), "added_ids": sorted(set(after) - set(before)),
                  "baseline_nonpassing": {key: value for key, value in before.items() if value != "passed"},
                  "final_nonpassing": {key: value for key, value in after.items() if value != "passed"}}
    assert before and not comparison["new_failures"] and not comparison["lost_prior_passes"] and not comparison["missing_ids"]
    OUT.joinpath("test-id-comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")

    anchor = json.loads(OUT.joinpath("00-baseline.json").read_text())
    main_path = Path("/Users/yimu/Projects/YM_Capital/YM-data-pipeline")
    drift = []
    for name, expected in anchor["main_tracked_blobs"].items():
        actual = git("hash-object", str(main_path / name), cwd=main_path).decode().strip()
        if actual != expected:
            drift.append(name)
    current_head = git("rev-parse", "HEAD", cwd=main_path).decode().strip()
    current_status = git("status", "--porcelain", cwd=main_path).decode()
    integrity = {"expected_head": anchor["main_head"], "actual_head": current_head, "main_status": current_status,
                 "tracked_count": len(anchor["main_tracked_blobs"]), "content_drift": drift}
    OUT.joinpath("main-integrity.json").write_text(json.dumps(integrity, indent=2) + "\n")
    assert current_head == anchor["main_head"] and current_status == anchor["main_status"] and not drift
    for entry in json.loads(OUT.joinpath("historical-index.json").read_text()):
        assert hashlib.sha256(Path(entry["source"]).read_bytes()).hexdigest() == entry["sha256"]
        assert hashlib.sha256((ROOT / entry["copy"]).read_bytes()).hexdigest() == entry["sha256"]
    for entry in json.loads(OUT.joinpath("cross-repo-source-bindings.json").read_text()).values():
        assert hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
    filing = json.loads(OUT.joinpath("filings-input-binding.json").read_text())
    assert hashlib.sha256(Path(filing["source"]).read_bytes()).hexdigest() == filing["sha256"]
    assert hashlib.sha256(OUT.joinpath("historical/intel_600825_filings.json").read_bytes()).hexdigest() == filing["sha256"]
    for name in ("cross-repo-proof.json", "historical-replay.json"):
        assert json.loads(OUT.joinpath(name).read_text())["passed"]

    files = set(filter(None, git("diff", "--name-only", "-z").decode().split("\0")))
    files |= set(filter(None, git("diff", "--cached", "--name-only", "-z").decode().split("\0")))
    files |= set(filter(None, git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")))
    sensitive = re.compile(r"(^|/)(\.env(?:\..*)?|credentials(?:\..*)?|.*\.(?:pem|key)|.*\.sqlite3)$", re.I)
    literal_patterns = [re.compile(r"Bearer\s+[A-Za-z0-9_.-]{32,}"),
                        re.compile(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_\-]{24,}"),
                        re.compile(r'''["'](?:access_token|refresh_token|api_key|client_secret|token)["']\s*:\s*["'][A-Za-z0-9_./+\-]{24,}["']''')]
    findings = []
    for name in sorted(files):
        assert not sensitive.search(name), "Sensitive path excluded from reads and delivery"
        path = ROOT / name
        if not path.is_file():
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        for index, pattern in enumerate(literal_patterns):
            if pattern.search(body):
                findings.append({"path": name, "pattern_id": index})
    assert files and not findings, "Literal scan failed; values intentionally withheld"
    OUT.joinpath("sensitive-scan.json").write_text(json.dumps({"scope": "changed delivery files only, no credential stores", "file_count": len(files), "findings": findings, "passed": True}, indent=2) + "\n")
    online = [json.loads(path.read_text()) for path in OUT.glob("online-*.json")]
    for record in online:
        for attempt in record.get("result", {}).get("_meta", {}).get("attempts", []):
            assert type(attempt.get("latency_ms")) is int and attempt["latency_ms"] >= 0
    OUT.joinpath("verification.json").write_text(json.dumps({"commands": commands, "online_public_query_count": len(online),
        "live_attempt_latency_contract_passed": True, "baseline_errors_retained": 2, "passed": True}, indent=2) + "\n")
    print(json.dumps({"passed": True, "tests": final["tests_run"], "added_tests": len(comparison["added_ids"]),
                      "new_failures": 0, "lost_prior_passes": 0, "online_queries": len(online)}))

if __name__ == "__main__":
    main()
