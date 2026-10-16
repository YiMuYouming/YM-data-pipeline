"""Offline source-snapshot proof and reviewable patches; never writes target repos."""
import ast
import difflib
import hashlib
import json
from pathlib import Path
import types
import unittest
from unittest.mock import Mock

OUT = Path(__file__).parent
TARGETS = {
    "gateway": Path("/Users/yimu/Projects/YM_Capital/live-dashboard/scripts/agent_api.py"),
    "bots": Path("/Users/yimu/Projects/YM_Capital/_wt/tb06-20261016/trading-bots/tools/read_pipeline_role.py"),
}

def load_gateway(source):
    module = types.ModuleType("offline_gateway")
    module.__file__ = str(TARGETS["gateway"])
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    def validate(intent, params):
        from ym_stock_data.api import _validate_params
        values = dict(params)
        _validate_params(intent, values)
        return {"data": values}
    module._pipeline_query = Mock(side_effect=validate)
    return module

def load_diagnostics(source):
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_local_gap_diagnostics")
    namespace = {"_brief_reason": lambda exc: str(exc)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "offline_diagnostics", "exec"), namespace)
    return namespace["_local_gap_diagnostics"]

def main():
    original = {name: path.read_text() for name, path in TARGETS.items()}
    gateway_marker = '    try:\n        envelope = _pipeline_query(intent, values)'
    decoder = '''    if "include_promotion" in values:
        raw = values["include_promotion"]
        if not isinstance(raw, str) or raw.lower() not in {"true", "false"}:
            return _fail(400, "agent_param_type_invalid", parameter="include_promotion")
        values["include_promotion"] = raw.lower() == "true"
'''
    assert original["gateway"].count(gateway_marker) == 1
    patched_gateway = original["gateway"].replace(gateway_marker, decoder + gateway_marker)
    bots_marker = "for attempt in attempts if isinstance(attempt, dict)] if isinstance(attempts, list) else 'unavailable'}"
    # Preserve a real nonnegative integer; absent/invalid measurements stay null.
    old = "for key in ('provider', 'status', 'error_code')}\n                         " + bots_marker
    new = "for key in ('provider', 'status', 'error_code')} | {'latency_ms': attempt.get('latency_ms') if type(attempt.get('latency_ms')) is int and attempt['latency_ms'] >= 0 else None}\n                         " + bots_marker
    assert original["bots"].count(old) == 1
    patched_bots = original["bots"].replace(old, new)
    baseline_gateway, fixed_gateway = load_gateway(original["gateway"]), load_gateway(patched_gateway)
    before_diag, after_diag = load_diagnostics(original["bots"]), load_diagnostics(patched_bots)
    params = {"date": ["20261016"], "include_promotion": ["True"]}
    baseline_status, baseline_payload = baseline_gateway.handle_pipeline("market_limit_state", params, {})
    fixed_status, fixed_payload = fixed_gateway.handle_pipeline("market_limit_state", params, {})
    bad_status, _ = fixed_gateway.handle_pipeline("market_limit_state", {"include_promotion": ["true", "false"]}, {})
    envelope = {"_meta": {"status": "error", "attempts": [{"provider": "stocktoday", "status": "timeout", "error_code": "TIMEOUT", "latency_ms": 15423}]}}
    before, after = before_diag(envelope), after_diag(envelope)
    assert baseline_status == 502 and "must be boolean" in baseline_payload["detail"]
    assert fixed_status == 200 and fixed_gateway._pipeline_query.call_args.args[1]["include_promotion"] is True
    assert fixed_payload["data"]["previous_date"] == "20261015"
    assert bad_status == 400
    assert "latency_ms" not in before["attempts"][0] and after["attempts"][0]["latency_ms"] == 15423
    missing = after_diag({"_meta": {"attempts": [{"provider": "x", "status": "error", "error_code": "X"}]}})
    assert missing["attempts"][0]["latency_ms"] is None
    OUT.joinpath("cross-repo-source-bindings.json").write_text(json.dumps({name: {"path": str(TARGETS[name]), "sha256": hashlib.sha256(value.encode()).hexdigest()} for name, value in original.items()}, indent=2) + "\n")
    for name, updated in (("gateway", patched_gateway), ("bots", patched_bots)):
        relative = "scripts/agent_api.py" if name == "gateway" else "tools/read_pipeline_role.py"
        OUT.joinpath(f"handoff-{name}.patch").write_text("".join(difflib.unified_diff(original[name].splitlines(True), updated.splitlines(True), fromfile="a/" + relative, tofile="b/" + relative)))
    result = {"mode": "offline proposed patch only; target worktrees not changed", "gateway_before": {"status": baseline_status, "body": baseline_payload}, "gateway_proposed": {"status": fixed_status, "include_promotion_forwarded_as_boolean": True, "normalized_previous_date": fixed_payload["data"]["previous_date"], "repeated_parameter_status": bad_status}, "bots_before": before, "bots_proposed": after, "missing_latency_is_not_fabricated": missing, "passed": True}
    OUT.joinpath("cross-repo-proof.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
