"""Scan delivery code/fixtures/evidence, never credential storage or .env."""
import datetime
import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[3]
changed = subprocess.check_output(["git", "diff", "--name-only", "-z"], cwd=ROOT).decode().split("\0")
new = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
staged = subprocess.check_output(["git", "diff", "--cached", "--name-only", "-z"], cwd=ROOT).decode().split("\0")
files = sorted(set(filter(None, changed + new + staged)))
blocked = []
findings = []
credential_paths = re.compile(r"(^|/)(\.env(?:\..*)?|credentials(?:\..*)?|.*\.(?:pem|key)|providers\.sqlite3|stocktoday-budget\.sqlite3)$", re.I)
literal_patterns = [
    re.compile(r"Bearer\s+[A-Za-z0-9_.-]{32,}"),
    re.compile(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_\-]{24,}"),
    re.compile(r'''["'](?:access_token|refresh_token|api_key|client_secret|token)["']\s*:\s*["'][A-Za-z0-9_./+\-]{24,}["']'''),
]
for rel in files:
    if credential_paths.search(rel):
        blocked.append(rel)
        continue
    path = ROOT / rel
    if not path.is_file() or path.suffix == ".pyc":
        continue
    body = path.read_text(encoding="utf-8", errors="replace")
    for index, pattern in enumerate(literal_patterns):
        if pattern.search(body):
            findings.append({"path": rel, "pattern_id": index})
record = {"observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
          "scope": "changed delivery files only; no credential stores read",
          "file_count": len(files), "blocked_sensitive_paths": blocked,
          "literal_findings": findings, "passed": bool(files) and not blocked and not findings}
print(json.dumps(record, indent=2))
raise SystemExit(not record["passed"])
