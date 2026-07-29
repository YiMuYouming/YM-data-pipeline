# Unified A-Share Data Channel Baseline

- Captured at: `2026-07-29T17:11:15+08:00`
- Worktree: `/Users/yimu/.codex/worktrees/47a4/YM-data-pipeline`
- HEAD: `f246fefd7b8f143c81f2bdf5da8d4f8900f7bfea`
- Branch: detached HEAD (Codex worktree)
- Python: `3.14.5` via `uv run`
- Plan SHA-256: `04f7c9aaf836ed2e1fe3e3220d7cd67ce6e4f5398a6ae0e1c48f7764721e884b`
- Canonical-plan comparison: identical bytes at capture time

## Protected pre-existing work

```text
 M README.md
 M tests/test_v2_capabilities.py
 M ym_stock_data/v2/capabilities.py
?? docs/Wind-MCP-补充源验证清单.md
?? docs/handoffs/
?? docs/superpowers/plans/2026-07-29-unified-a-share-data-channel.md
?? tests/test_wind_sidecar.py
?? ym_stock_data/experimental/
```

These files pre-date this implementation task and remain protected. Only the
plan is eligible for the Task 1 documentation commit; the Wind sidecar and its
related edits are not staged.

## Sanitized provider/environment facts

Only booleans were read and recorded. No key, token, credential content,
Authorization header, query result, or response body was printed or persisted.

```json
{
  "configured_pywencai_python_exists": true,
  "iwencai_key_present": true,
  "tdx_wrapper_exists": true,
  "wind_global_config_exists": true
}
```

`codex mcp list` reported the `tdx-finance` wrapper as enabled. That inventory
result does not prove its credentials or live read-only calls are healthy.

## Pre-implementation provider observations

The preceding audit recorded these sanitized outcomes for the plan: WenCai
OpenAPI returned `HTTP_401`; pywencai intermittently failed with
`NoneType...get`; TDX credentials were missing; a local Wind Skill was present;
and six non-WenCai intents were usable. They are baseline observations, not a
claim of current provider availability. Checkpoint 1 reports fresh live probes
separately from offline unit-test results.

## Safety receipt

- No trading POST, broker call, ticket mutation, deployment, or push occurred.
- No credential value was captured.
- `uv run` initialized the ignored worktree-local `.venv`; it is not staged.
