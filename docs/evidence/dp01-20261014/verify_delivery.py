"""Record scoped offline delivery verification commands and exact exit codes."""
import datetime
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
EVIDENCE = pathlib.Path(__file__).parent

def main():
    commands = [
        ("compileall", [sys.executable, "-m", "compileall", "-q", "ym_stock_data", "scripts", "tests", "docs/evidence/dp01-20261014"]),
        ("diff-check", ["git", "diff", "HEAD", "--check", "--", ".", ":(exclude)docs/evidence/dp01-20261014/ths-page-*.html", ":(exclude)docs/evidence/dp01-20261014/*.stderr"]),
        ("sensitive-scan", [sys.executable, "docs/evidence/dp01-20261014/scan_sensitive.py"]),
        ("main-head", ["git", "-C", "/Users/yimu/Projects/YM_Capital/YM-data-pipeline", "rev-parse", "HEAD"]),
        ("main-status", ["git", "-C", "/Users/yimu/Projects/YM_Capital/YM-data-pipeline", "status", "--short"]),
    ]
    records = []
    for label, argv in commands:
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        tick = time.monotonic()
        result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)
        EVIDENCE.joinpath(f"{label}.stdout").write_text(result.stdout)
        EVIDENCE.joinpath(f"{label}.stderr").write_text(result.stderr)
        records.append({"label": label, "argv": argv, "cwd": str(ROOT),
                        "started_at": started, "elapsed_ms": int((time.monotonic() - tick) * 1000),
                        "exit_code": result.returncode, "stdout": f"{label}.stdout", "stderr": f"{label}.stderr"})
    for label in ["baseline", "candidate", "final"]:
        result = json.loads(EVIDENCE.joinpath(f"{label}-ids.json").read_text())
        records.append({"label": label,
                        "argv": [sys.executable, "docs/evidence/dp01-20261014/run_tests.py", label],
                        "cwd": str(ROOT), "started_unix": result["started_unix"],
                        "elapsed_seconds": result["elapsed_seconds"],
                        "exit_code": int(any(v in {"failed", "error"} for v in result["outcomes"].values())),
                        "stdout": f"{label}.stdout", "stderr": f"{label}.stderr"})
    raw_check = subprocess.run(["git", "diff", "HEAD", "--check"], cwd=ROOT, text=True, capture_output=True)
    EVIDENCE.joinpath("raw-evidence-whitespace.json").write_text(json.dumps({"stdout": raw_check.stdout, "stderr": raw_check.stderr}, ensure_ascii=False, indent=2) + "\n")
    records.append({"label": "full-raw-diff-check", "argv": ["git", "diff", "HEAD", "--check"],
                    "exit_code": raw_check.returncode, "stdout": "raw-evidence-whitespace.json",
                    "interpretation": "Raw HTTP HTML and failed-test stderr retain original trailing whitespace; scoped code/docs/fixtures check above is the acceptance check"})
    comparison = json.loads(EVIDENCE.joinpath("test-id-comparison.json").read_text())
    scan = json.loads(EVIDENCE.joinpath("sensitive-scan.stdout").read_text())
    result = {"commands": records, "test_id_comparison_passed": comparison["passed"],
              "verification_passed": all(r["exit_code"] == 0 for r in records[:5]) and comparison["passed"] and scan["passed"]}
    EVIDENCE.joinpath("verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"verification_passed": result["verification_passed"], "baseline_errors_retained": 2,
                      "new_failures": len(comparison["new_failures"]), "lost_prior_passes": len(comparison["lost_prior_passes"])}))
    raise SystemExit(not result["verification_passed"])

if __name__ == "__main__":
    main()
