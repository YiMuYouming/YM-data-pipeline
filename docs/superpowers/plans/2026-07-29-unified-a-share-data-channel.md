# Unified A-Share Data Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fragmented V1/V2/direct-source/manual-MCP usage with one production-grade `ym_stock_data.query()` entrypoint, portable provider runtimes, explicit authentication health, semantically safe fallback, and verified consumer migration.

**Architecture:** Introduce a small core router that owns the result contract and dispatches each intent through a short, capability-specific provider chain. Existing source modules remain the implementation base; `fetch()` and `resolve()` become compatibility wrappers over the same router instead of independent routing systems. Provider availability, breaker state, authentication state, attempts, provenance, and freshness are exposed through one contract and `ym-data doctor`; TDX and Wind are never generic substitutes and are only callable for whitelisted compatible capabilities.

**Tech Stack:** Python 3.10+, stdlib `dataclasses`/`json`/`sqlite3`/`subprocess`/`unittest`, existing `uv` project environment, existing source modules, optional pywencai runtime, TDX OAuth MCP wrapper, official Wind CLI, existing downstream scripts in live-dashboard and Market_Watch.

---

## Non-negotiable boundaries

1. Preserve the pre-existing dirty worktree. At plan start it contains edits to `README.md`, `tests/test_v2_capabilities.py`, `ym_stock_data/v2/capabilities.py`, plus untracked Wind sidecar/docs/tests. Never reset, clean, stash, or stage them implicitly.
2. No trading POST, broker order, ticket mutation, production deployment, Git push, or consumer data write is authorized by this plan.
3. TDX and Wind are read-only data providers. A successful provider response never creates trade authorization.
4. Fallback is per intent and per semantic contract. A provider that cannot return the required business shape must return `incompatible` or `unavailable`; it must not silently populate a different meaning.
5. `provider_used` is the actual successful provider. `source_chain` and `attempts` include every attempted provider in order. On total failure, `provider_used=null`.
6. Secrets, tokens, credential file contents, and raw Authorization headers must never appear in logs, results, fixtures, snapshots, or commits.
7. Unit tests are network-free. Live provider checks run only through the explicit `ym-data smoke --live` command and write snapshots outside Git under `~/.ym-stock-data/smoke/`.
8. Existing consumers migrate one at a time and keep a compatibility rollback path until five successful trading days are recorded.
9. For explicit `review_sentiment`, a final `empty` is valid only when every compatible screener attempt is a semantically valid empty; any auth/provider/dependency error keeps the exhausted result at `error` with `provider_used=null`.

## Target public contract

```python
from ym_stock_data import query

result = query(
    "review_sentiment",
    query="今日热股人气排名前20 非ST",
    limit=20,
)
```

Every result must follow this shape:

```python
{
    "data": {},
    "_meta": {
        "contract_version": "1.0",
        "intent": "review_sentiment",
        "status": "success",  # success|degraded|empty|error
        "provider_used": "pywencai",
        "source": "pywencai",  # compatibility alias
        "source_chain": ["iwencai_openapi", "pywencai"],
        "attempts": [
            {
                "provider": "iwencai_openapi",
                "status": "auth_error",
                "error_code": "HTTP_401",
                "latency_ms": 120,
            },
            {
                "provider": "pywencai",
                "status": "success",
                "error_code": None,
                "latency_ms": 2100,
            },
        ],
        "fetched_at": "2026-07-29T17:00:00+08:00",
        "data_scope": "问财自然语言选股口径",
        "quality": {
            "status": "normal",
            "returned_count": 20,
            "reason_codes": [],
        },
        "freshness": {
            "status": "fresh",
            "age_sec": 0,
            "max_age_sec": 1800,
        },
        "auth": {
            "required": False,
            "status": "not_required",
        },
        "trade_usage": "辅助，不单独触发交易",
    },
}
```

Canonical intent chains for this implementation:

| Intent | Ordered providers | Automatic use boundary |
| --- | --- | --- |
| `realtime_market` | `pytdx` → `eastmoney` → `tencent` | zero-auth only |
| `sector_index` | `ths_industry` | no semantic substitute |
| `stock_snapshot` | `pytdx` → `tencent` → `sina` → `tdx_quotes` | normalized quote fields only |
| `stock_kline` | day/week/month: `pytdx` → `tencent` → `tdx_kline`; minute: `pytdx` → `sina` → `tdx_kline` | period-aware |
| `review_sentiment` without explicit query | `pytdx_breadth` → `eastmoney_breadth` → `eastmoney_limit_pool` | market breadth/limit-state aggregate only |
| `review_sentiment` with explicit query | `iwencai_openapi` → `pywencai` → `tdx_screener` → `wind_screener`; fully compiled structured沪深 query then appends `pytdx_screener` | stock-screen rows only; final empty requires every provider in the selected four- or five-source route to be valid empty |
| `market_limit_state` | `eastmoney_limit_pool` | no natural-language fallback |
| `stock_event` | `eastmoney_datacenter` | only whitelisted event families; no Wind fallback |
| `research` | `eastmoney_research` → `tdx_report` | report rows only |
| `filings` | `cninfo` → `tdx_notice` → `wind_documents` | announcement metadata/document retrieval |
| `news` | `cls` → `tdx_news` | news rows; material facts still require primary-source verification |
| `wind_enrichment` | `wind_mcp` | explicit research enrichment, never generic fallback |

## Planned file map

Create:

- `ym_stock_data/contracts.py` — result/attempt/auth/freshness constructors and validation.
- `ym_stock_data/routing.py` — canonical intent registry and short provider chains.
- `ym_stock_data/provider_state.py` — cross-process breaker and provider health SQLite store.
- `ym_stock_data/providers/__init__.py` — provider registry exports.
- `ym_stock_data/providers/base.py` — provider protocol and normalized provider outcome.
- `ym_stock_data/providers/local.py` — adapters over existing zero-auth source modules.
- `ym_stock_data/providers/iwencai.py` — OpenAPI and pywencai providers with independent diagnostics.
- `ym_stock_data/providers/tdx_mcp.py` — read-only TDX command/MCP adapter and owned credential discovery.
- `ym_stock_data/providers/wind_mcp.py` — promoted form of the current experimental Wind sidecar.
- `ym_stock_data/doctor.py` — machine-readable environment/provider diagnostics.
- `ym_stock_data/smoke.py` — explicit live checks and non-Git snapshot writer.
- `tests/test_contracts.py`
- `tests/test_routing.py`
- `tests/test_provider_state.py`
- `tests/test_provider_iwencai.py`
- `tests/test_provider_tdx_mcp.py`
- `tests/test_provider_wind_mcp.py`
- `tests/test_doctor.py`
- `tests/test_public_api.py`
- `tests/test_legacy_compat.py`
- `docs/INSTALL.md`
- `.env.example`

Modify:

- `ym_stock_data/__init__.py` — export `query`; preserve legacy exports.
- `ym_stock_data/fetch.py` — compatibility mapping only; stop overwriting provider metadata.
- `ym_stock_data/v2/resolve.py` — compatibility wrapper over `query` after contract parity.
- `ym_stock_data/v2/capabilities.py` — derive manifest from the canonical registry.
- `ym_stock_data/sources/iwencai.py` — retain low-level HTTP parsing only; remove external runtime discovery and process-local routing ownership.
- `ym_stock_data/config.py` — remove hard-coded external pywencai paths; define explicit config locations.
- `ym_stock_data/__main__.py` — add `query`, `doctor`, `setup pywencai`, owned `auth login-tdx` / `status-tdx`, and `smoke` subcommands.
- `pyproject.toml` — add a `full` extra and keep pywencai optional for zero-auth-only installs.
- `README.md` and `AGENTS.md` — document one public entrypoint and provider boundary.
- `scripts/compare_external_sources.py` — call the public API and record actual provider attempts.
- `tests/test_v2_capabilities.py` and existing V2 tests — assert wrapper parity rather than a second router.
- Current untracked Wind sidecar/docs/tests — migrate their behavior into the provider module, preserving proven security and error contracts.
- Downstream files listed in Task 12 — migrate one consumer at a time.

Delete only after replacement tests pass:

- `ym_stock_data/experimental/wind_sidecar.py`
- `ym_stock_data/experimental/__init__.py` if it has no remaining exports.

Do not delete V1 `fetch()` or V2 `resolve()` in this release.

---

### Task 1: Capture the baseline and protect existing work

**Files:**
- Create: `docs/audit/2026-07-29-unified-channel-baseline.md`
- Test: none; this is an evidence receipt.

- [ ] **Step 1: Record Git and environment baseline**

Run:

```bash
cd /Users/yimu/Documents/YM_Capital/YM-data-pipeline
git status --short
git rev-parse HEAD
git branch --show-current
uv run python --version
codex mcp list
```

Expected: preserve the pre-existing Wind-related dirty files and record the actual branch/HEAD without changing them.

- [ ] **Step 2: Record sanitized provider probes**

Run a script that records booleans/status only—never token values:

```python
from pathlib import Path
from ym_stock_data.sources import iwencai

baseline = {
    "iwencai_key_present": bool(iwencai._load_api_key()),
    "configured_pywencai_python_exists": Path(
        "/legacy/external/data-venv/bin/python3"
    ).exists(),
    "tdx_wrapper_exists": Path(
        "/Users/yimu/.codex/mcp/tdx-finance-mcp.py"
    ).exists(),
    "wind_global_config_exists": (Path.home() / ".wind-aifinmarket/config").exists(),
}
```

Write only the sanitized output and the already-observed live outcomes into the baseline document: OpenAPI `HTTP 401`, intermittent pywencai `NoneType...get`, TDX credentials missing, Wind local Skill present, six non-WenCai intents usable.

- [ ] **Step 3: Commit only the plan and baseline receipt**

```bash
git add docs/superpowers/plans/2026-07-29-unified-a-share-data-channel.md \
        docs/audit/2026-07-29-unified-channel-baseline.md
git diff --cached --check
git commit -m "docs: plan unified A-share data channel"
```

Expected: none of the pre-existing Wind files are staged by this commit.

---

### Task 2: Define the one-result contract first

**Files:**
- Create: `ym_stock_data/contracts.py`
- Create: `tests/test_contracts.py`

- [ ] **Step 1: Write failing contract tests**

Add `unittest.TestCase` coverage for these exact invariants:

```python
import unittest

from ym_stock_data.contracts import ProviderAttempt, build_result, validate_result


class ResultContractTests(unittest.TestCase):
    def test_success_uses_actual_provider_and_preserves_attempt_order(self):
        result = build_result(
            intent="review_sentiment",
            data={"rows": [{"股票代码": "600519"}]},
            status="degraded",
            provider_used="pywencai",
            attempts=[
                ProviderAttempt("iwencai_openapi", "auth_error", "HTTP_401", 10),
                ProviderAttempt("pywencai", "success", None, 20),
            ],
            data_scope="问财自然语言选股口径",
            trade_usage="辅助，不单独触发交易",
            quality={"status": "normal", "returned_count": 1, "reason_codes": []},
            max_age_sec=1800,
        )
        self.assertEqual("pywencai", result["_meta"]["provider_used"])
        self.assertEqual("pywencai", result["_meta"]["source"])
        self.assertEqual(
            ["iwencai_openapi", "pywencai"],
            result["_meta"]["source_chain"],
        )
        self.assertEqual("1.0", result["_meta"]["contract_version"])
        validate_result(result)

    def test_total_failure_has_no_provider_used(self):
        result = build_result(
            intent="review_sentiment",
            data=None,
            status="error",
            provider_used=None,
            attempts=[ProviderAttempt("iwencai_openapi", "auth_error", "HTTP_401", 10)],
            data_scope="问财自然语言选股口径",
            trade_usage="辅助，不单独触发交易",
            quality={"status": "error", "returned_count": 0, "reason_codes": ["source_error"]},
            max_age_sec=1800,
        )
        self.assertIsNone(result["_meta"]["provider_used"])
        validate_result(result)
```

- [ ] **Step 2: Run the focused test and verify it fails**

```bash
uv run python -m unittest tests.test_contracts -v
```

Expected: FAIL because `ym_stock_data.contracts` does not exist.

- [ ] **Step 3: Implement the contract module**

Implement:

```python
@dataclass(frozen=True)
class ProviderAttempt:
    provider: str
    status: str
    error_code: str | None
    latency_ms: int


def build_result(
    *,
    intent: str,
    data: object,
    status: str,
    provider_used: str | None,
    attempts: list[ProviderAttempt],
    data_scope: str,
    trade_usage: str,
    quality: dict,
    max_age_sec: int,
    fetched_at: str | None = None,
    auth: dict | None = None,
) -> dict:
    """Build contract 1.0 without leaking provider secrets."""


def validate_result(result: dict) -> None:
    """Raise ValueError when required keys, enums, or provenance invariants fail."""
```

Allowed statuses are `success`, `degraded`, `empty`, `error`. Allowed attempt statuses are `success`, `empty`, `auth_error`, `dependency_missing`, `timeout`, `network_error`, `provider_error`, `breaker_open`, and `incompatible`.

- [ ] **Step 4: Run the focused test and full unit suite**

```bash
uv run python -m unittest tests.test_contracts -v
uv run python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 5: Commit explicit paths**

```bash
git add ym_stock_data/contracts.py tests/test_contracts.py
git diff --cached --check
git commit -m "feat: define unified data result contract"
```

---

### Task 3: Add the provider protocol and canonical route registry

**Files:**
- Create: `ym_stock_data/providers/base.py`
- Create: `ym_stock_data/providers/__init__.py`
- Create: `ym_stock_data/routing.py`
- Create: `tests/test_routing.py`

- [ ] **Step 1: Write failing routing tests**

Tests must assert:

```python
class RoutingTests(unittest.TestCase):
    def test_explicit_screen_uses_four_compatible_providers(self):
        spec = route_for("review_sentiment", {"query": "今日涨停 非ST"})
        self.assertEqual(
            ("iwencai_openapi", "pywencai", "tdx_screener", "wind_screener"),
            spec.providers,
        )

    def test_default_sentiment_never_calls_natural_language_sources(self):
        spec = route_for("review_sentiment", {})
        self.assertEqual(
            ("pytdx_breadth", "eastmoney_breadth", "eastmoney_limit_pool"),
            spec.providers,
        )

    def test_wind_is_not_a_realtime_market_fallback(self):
        self.assertNotIn("wind_mcp", route_for("realtime_market", {}).providers)
```

- [ ] **Step 2: Run and observe the import failure**

```bash
uv run python -m unittest tests.test_routing -v
```

- [ ] **Step 3: Implement focused types**

Use these interfaces:

```python
@dataclass(frozen=True)
class ProviderOutcome:
    provider: str
    status: str
    data: object = None
    error_code: str | None = None
    detail: str | None = None
    fetched_at: str | None = None
    latency_ms: int = 0
    quality: dict | None = None
    auth: dict | None = None


class Provider(Protocol):
    name: str

    def probe(self) -> dict: ...
    def call(self, intent: str, params: dict) -> ProviderOutcome: ...


@dataclass(frozen=True)
class RouteSpec:
    intent: str
    providers: tuple[str, ...]
    data_scope: str
    trade_usage: str
    max_age_sec: int
```

`route_for(intent, params)` must be deterministic and side-effect free. Do not read auth or network state in the registry.

- [ ] **Step 4: Verify and commit**

```bash
uv run python -m unittest tests.test_routing -v
uv run python -m compileall -q ym_stock_data
git add ym_stock_data/providers/base.py ym_stock_data/providers/__init__.py \
        ym_stock_data/routing.py tests/test_routing.py
git diff --cached --check
git commit -m "feat: add capability-specific provider routing"
```

---

### Task 4: Add cross-process provider health and breaker state

**Files:**
- Create: `ym_stock_data/provider_state.py`
- Create: `tests/test_provider_state.py`
- Modify: `ym_stock_data/config.py`

- [ ] **Step 1: Write failing state tests**

Use a temporary SQLite path and two independent `ProviderState` instances. Assert that one instance opening an `iwencai_openapi` breaker is visible to the other, that expiration clears it, and that no token/error body is persisted.

```python
first.record_failure(
    provider="iwencai_openapi",
    failure_type="auth_error",
    error_code="HTTP_401",
    breaker_seconds=300,
)
self.assertEqual("HTTP_401", second.active_breaker("iwencai_openapi")["error_code"])
```

- [ ] **Step 2: Run and verify failure**

```bash
uv run python -m unittest tests.test_provider_state -v
```

- [ ] **Step 3: Implement SQLite-backed state**

Use `~/.ym-stock-data/state/providers.sqlite3` by default, WAL mode, a five-second SQLite timeout, and this table:

```sql
CREATE TABLE IF NOT EXISTS provider_state (
    provider TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    failure_type TEXT,
    error_code TEXT,
    opened_at REAL,
    expires_at REAL,
    updated_at REAL NOT NULL
)
```

Expose only `active_breaker()`, `record_failure()`, `record_success()`, and `snapshot()`. Never persist exception bodies, queries, tokens, or response rows.

- [ ] **Step 4: Verify concurrent access and commit**

```bash
uv run python -m unittest tests.test_provider_state -v
uv run python -m unittest discover -s tests -v
git add ym_stock_data/provider_state.py ym_stock_data/config.py tests/test_provider_state.py
git diff --cached --check
git commit -m "feat: share provider breaker state across processes"
```

---

### Task 5: Make WenCai fallback portable and independently observable

**Files:**
- Create: `ym_stock_data/providers/iwencai.py`
- Create: `tests/test_provider_iwencai.py`
- Modify: `ym_stock_data/sources/iwencai.py`
- Modify: `ym_stock_data/config.py`
- Modify: `pyproject.toml`
- Create: `.env.example`

- [ ] **Step 1: Write failing provider tests**

Cover these exact cases with mocks:

1. OpenAPI 401 returns `ProviderOutcome(status="auth_error", error_code="HTTP_401")` and opens the shared 300-second breaker.
2. An active breaker avoids another HTTP call.
3. pywencai missing returns `dependency_missing`, not a vague `ImportError`.
4. pywencai `NoneType...get` returns `provider_error` and preserves only the exception type/code, not HTML/body data.
5. pywencai success limits rows and returns `provider="pywencai"`.
6. The provider never refers to a user-specific external runtime directory.

- [ ] **Step 2: Run the failing test**

```bash
uv run python -m unittest tests.test_provider_iwencai -v
```

- [ ] **Step 3: Split low-level transport from routing**

Keep `_validate_openapi_result()` and row parsing in `sources/iwencai.py`. Move breaker decisions and provider outcomes to `providers/iwencai.py`.

Runtime discovery order for pywencai must be:

1. `YM_PYWENCAI_PYTHON` when explicitly configured and executable.
2. Current `sys.executable` when both `pywencai` and `pandas` import successfully.
3. Project-owned `~/.ym-stock-data/runtimes/pywencai/bin/python`.
4. Otherwise `dependency_missing` with action `ym-data setup pywencai`.

Do not inspect unrelated external runtime paths.

- [ ] **Step 4: Add the managed setup contract**

The CLI implementation added later must execute these operations, without shell interpolation:

```text
uv venv --python 3.12 ~/.ym-stock-data/runtimes/pywencai
uv pip install --python ~/.ym-stock-data/runtimes/pywencai/bin/python \
  pywencai==0.13.1 pandas numpy
```

Use argument lists through `subprocess.run(shell=False)`. Add `pywencai` and `pandas` to `[project.optional-dependencies].full`; do not force them into the zero-auth base install.

- [ ] **Step 5: Verify no hard-coded dependency remains**

```bash
rg -n '/legacy/external|external/data-venv' ym_stock_data
```

Expected: no matches.

- [ ] **Step 6: Run tests and commit**

```bash
uv run python -m unittest tests.test_provider_iwencai tests.test_iwencai_fallback -v
uv run python -m unittest discover -s tests -v
git add .env.example pyproject.toml ym_stock_data/config.py \
        ym_stock_data/sources/iwencai.py ym_stock_data/providers/iwencai.py \
        tests/test_provider_iwencai.py
git diff --cached --check
git commit -m "fix: make WenCai fallback portable and observable"
```

---

### Task 6: Build the canonical `query()` router

**Files:**
- Create: `ym_stock_data/api.py`
- Create: `ym_stock_data/providers/local.py`
- Create: `tests/test_public_api.py`
- Modify: `ym_stock_data/providers/__init__.py`
- Modify: `ym_stock_data/__init__.py`

- [ ] **Step 1: Write failing end-to-end router tests**

Use fake providers and assert:

- success stops the chain;
- `empty` stops when empty is a valid final result;
- `auth_error`, `dependency_missing`, timeout, and incompatible outcomes continue only when the route allows it;
- success after one failure returns `status=degraded`;
- total failure returns `status=error`, `provider_used=None`, and all attempts;
- the public import is exactly `from ym_stock_data import query`.

- [ ] **Step 2: Run and verify failure**

```bash
uv run python -m unittest tests.test_public_api -v
```

- [ ] **Step 3: Implement router behavior**

Provide:

```python
def query(intent: str, **params) -> dict:
    """Resolve one canonical intent through semantically compatible providers."""
```

The router must:

1. Validate params before any provider call.
2. Load `RouteSpec` from `route_for()`.
3. Skip a provider with an active breaker and append a `breaker_open` attempt.
4. Call providers in order.
5. Validate successful data against the intent-specific normalizer/quality function.
6. Return through `build_result()` exactly once.
7. Never catch `KeyboardInterrupt` or `SystemExit`.

- [ ] **Step 4: Adapt existing local sources without rewriting them**

`providers/local.py` must call current source functions for PyTDX, Tencent, Sina, THS, Eastmoney, CNInfo, CLS, and existing research functions. Do not copy HTTP implementations into the provider layer.

- [ ] **Step 5: Verify and commit**

```bash
uv run python -m unittest tests.test_public_api tests.test_routing -v
uv run python -m unittest discover -s tests -v
git add ym_stock_data/api.py ym_stock_data/providers/local.py \
        ym_stock_data/providers/__init__.py ym_stock_data/__init__.py \
        tests/test_public_api.py
git diff --cached --check
git commit -m "feat: add one public A-share data query entrypoint"
```

---

### Task 7: Convert `fetch()` and `resolve()` into compatibility wrappers

**Files:**
- Modify: `ym_stock_data/fetch.py`
- Modify: `ym_stock_data/v2/resolve.py`
- Modify: `ym_stock_data/v2/adapters.py`
- Create: `tests/test_legacy_compat.py`
- Modify: existing V1/V2 tests.

- [ ] **Step 1: Write failing compatibility tests**

Assert:

```python
fetch("iwencai", query="今日涨停", limit=3)
resolve("review_sentiment", query="今日涨停", limit=3)
query("review_sentiment", query="今日涨停", limit=3)
```

all reach the same router and preserve the same `provider_used`, `attempts`, and business rows. Also assert that V1 no longer overwrites provider `_meta`.

- [ ] **Step 2: Run focused tests and observe the existing V1 signature failure**

```bash
uv run python -m unittest tests.test_legacy_compat -v
```

- [ ] **Step 3: Implement explicit mapping tables**

`fetch()` maps legacy data types to canonical intents and parameter names. The `iwencai` mapping must convert `query=` to the canonical `query` parameter without passing it to `sources.iwencai.query` directly.

`resolve()` validates its historical params and delegates to `query()`. Keep V2 normalization helpers only where required for legacy shape compatibility; do not maintain a second provider chain.

- [ ] **Step 4: Verify representative parity**

```bash
uv run python -m unittest tests.test_legacy_compat tests.test_v2_mvp \
  tests.test_v2_quality tests.test_iwencai_fallback -v
uv run python -m unittest discover -s tests -v
```

- [ ] **Step 5: Commit**

```bash
git add ym_stock_data/fetch.py ym_stock_data/v2/resolve.py \
        ym_stock_data/v2/adapters.py tests/test_legacy_compat.py \
        tests/test_v2_mvp.py tests/test_v2_quality.py
git diff --cached --check
git commit -m "refactor: converge legacy APIs on unified router"
```

---

### Task 8: Add `ym-data doctor` and setup commands

**Files:**
- Create: `ym_stock_data/doctor.py`
- Create: `tests/test_doctor.py`
- Modify: `ym_stock_data/__main__.py`
- Create: `docs/INSTALL.md`

- [ ] **Step 1: Write failing doctor tests**

Doctor output must be JSON serializable and use only these provider states:

```text
ready | auth_missing | auth_expired | dependency_missing |
configured_unverified | breaker_open | unavailable
```

Tests must verify that missing TDX credentials, missing pywencai runtime, and a present Wind config are reported separately; no provider failure may collapse into generic `pipeline unavailable`.

- [ ] **Step 2: Run and verify failure**

```bash
uv run python -m unittest tests.test_doctor -v
```

- [ ] **Step 3: Implement CLI parsing without adding a framework dependency**

Supported commands:

```text
ym-data query INTENT key=value...
ym-data doctor [--json]
ym-data setup pywencai
ym-data auth login-tdx [--store keychain|file]
ym-data auth status-tdx [--store keychain|file]
ym-data smoke --live
ym-data list
```

Use `argparse`. `doctor` and `auth status-tdx` are read-only. `setup pywencai` and `auth login-tdx` are explicit mutating commands; login output is sanitized and never prints its credential target or values.

- [ ] **Step 4: Document clean installation paths**

`docs/INSTALL.md` must include:

```bash
uv sync
uv run ym-data doctor --json
uv run ym-data setup pywencai
uv run ym-data doctor --json
```

Describe zero-auth-only, full research, TDX, and Wind profiles independently. Do not instruct users to edit shell rc files as the primary configuration method.

- [ ] **Step 5: Verify and commit**

```bash
uv run python -m unittest tests.test_doctor -v
uv run ym-data doctor --json | uv run python -m json.tool >/dev/null
git add ym_stock_data/doctor.py ym_stock_data/__main__.py tests/test_doctor.py docs/INSTALL.md
git diff --cached --check
git commit -m "feat: add provider doctor and setup workflow"
```

---

### Task 9: Make TDX a pipeline-owned optional provider

**Files:**
- Create: `ym_stock_data/providers/tdx_mcp.py`
- Create: `tests/test_provider_tdx_mcp.py`
- Modify: `ym_stock_data/doctor.py`
- Modify: `ym_stock_data/__main__.py`
- Reference read-only: official MCP Python SDK 2.0 protocol and transport APIs

- [ ] **Step 1: Write failing TDX adapter tests**

Cover:

1. No owned credentials → `auth_missing` without scanning arbitrary files.
2. Fake authorization server covers discovery, DCR, authorization-code + PKCE S256, localhost callback, state validation, cancellation, timeout, refresh and rotation.
3. Keychain is the macOS default; explicit file fallback uses directory `0700`, credential/lock `0600`, atomic write, and cross-thread/process refresh locking.
4. Scope is exactly `mcp.read`; missing scope and `mcp.write` fail closed.
5. `tdx_screener`, `tdx_quotes`, `tdx_kline`, `wenda_report_query`, `wenda_notice_query`, and `wenda_news_query` map only to compatible intents.
6. Official SDK `initialize` and `tools/list` schema gate precede `tools/call`; 401 retries once after refresh/session rebuild, while 403 never escalates scope.

- [ ] **Step 2: Run and verify failure**

```bash
uv run python -m unittest tests.test_provider_tdx_mcp -v
```

- [ ] **Step 3: Implement owned credential storage**

Default store:

```text
macOS Keychain service ym-stock-data/tdx-oauth
```

The explicit file fallback contains only this pipeline's OAuth entry and client metadata required for refresh. Its directory is `0700`, file and lock are `0600`, and writes are atomic. No credential import exists.

- [ ] **Step 4: Implement a read-only MCP session adapter**

Use fixed `mcp==2.0.0` as the only MCP protocol implementation with Streamable HTTP. Keep OAuth ownership in this project, inject a read-only bearer header, and expose no write/trading tool.

- [ ] **Step 5: Run offline tests, then diagnose current live state**

```bash
uv run python -m unittest tests.test_provider_tdx_mcp tests.test_doctor -v
uv run ym-data doctor --json
```

Expected offline state before user authorization: `tdx_mcp=auth_missing`. Do not claim TDX fallback is operational until a separately authorized real `tools/list`/small read-only call passes.

- [ ] **Step 6: Commit**

```bash
git add ym_stock_data/providers/tdx_mcp.py ym_stock_data/doctor.py \
        ym_stock_data/__main__.py tests/test_provider_tdx_mcp.py
git diff --cached --check
git commit -m "feat: add independently diagnosed TDX provider"
```

---

### Task 10: Promote Wind sidecar into the provider registry

**Files:**
- Create: `ym_stock_data/providers/wind_mcp.py`
- Create: `tests/test_provider_wind_mcp.py`
- Modify: `ym_stock_data/providers/__init__.py`
- Modify: `ym_stock_data/routing.py`
- Modify: `ym_stock_data/doctor.py`
- Modify: `ym_stock_data/v2/capabilities.py`
- Modify: `tests/test_v2_capabilities.py`
- Modify: `README.md`
- Migrate/delete after parity: `ym_stock_data/experimental/wind_sidecar.py`, `tests/test_wind_sidecar.py`.

- [ ] **Step 1: Preserve the existing Wind security contract in failing provider tests**

Tests must retain these existing assertions:

- no API key in command arguments;
- `shell=False`;
- CLI missing, auth error, timeout, invalid JSON, and embedded payload error are explicit;
- no price, K-line, minute, news, or generic screener capability in automatic Wind routes;
- `wind_enrichment` stays explicit;
- event/filing fallback is allowed only for whitelisted compatible capabilities.

- [ ] **Step 2: Run focused tests before migration**

```bash
uv run python -m unittest tests.test_wind_sidecar tests.test_provider_wind_mcp -v
```

Expected: new provider tests fail; old sidecar tests pass.

- [ ] **Step 3: Move behavior, do not duplicate it**

Port the validated sidecar behavior into `providers/wind_mcp.py`. Discovery order:

1. `WIND_MCP_SKILL_DIR`.
2. global `~/.agents/skills/wind-mcp-skill`.
3. current YiMu_IR local Skill path as a compatibility fallback.

Doctor must report which scope was found (`global`, `project_compat`, or `missing`) without printing config contents.

- [ ] **Step 4: Derive capability manifest from canonical routing**

`capability_manifest()` must report actual registered providers and routes, not manually duplicated status dictionaries. It must still expose lifecycle and automatic-fallback boundaries.

- [ ] **Step 5: Delete superseded experimental code only after parity**

```bash
uv run python -m unittest tests.test_provider_wind_mcp tests.test_v2_capabilities -v
rg -n 'experimental\.wind_sidecar|fetch_wind_enrichment' .
```

Expected before deletion: only migration documentation/compatibility references remain. If external code still imports the old path, leave a deprecated re-export rather than breaking it.

- [ ] **Step 6: Commit only the reviewed Wind set**

```bash
git add README.md ym_stock_data/providers/wind_mcp.py \
        ym_stock_data/providers/__init__.py ym_stock_data/routing.py \
        ym_stock_data/doctor.py ym_stock_data/v2/capabilities.py \
        tests/test_provider_wind_mcp.py tests/test_v2_capabilities.py \
        ym_stock_data/experimental tests/test_wind_sidecar.py \
        docs/Wind-MCP-补充源验证清单.md docs/handoffs
git diff --cached --check
git commit -m "feat: register Wind as governed research provider"
```

Before committing, inspect `git diff --cached --name-status` and confirm every staged Wind file belongs to this migration.

---

### Task 11: Add live smoke checks that the normal unit suite cannot fake

**Files:**
- Create: `ym_stock_data/smoke.py`
- Modify: `ym_stock_data/__main__.py`
- Modify: `scripts/compare_external_sources.py`
- Convert or rename: `tests/test_iwencai.py`, `tests/test_sources.py`, `tests/test_pytdx.py`.

- [ ] **Step 1: Stop presenting undiscovered function tests as full coverage**

Move live-only cases into `ym_stock_data/smoke.py` or convert them to an explicitly skipped `tests/integration/` suite. `unittest discover` must not silently ignore files whose names look like tests.

- [ ] **Step 2: Implement the live smoke matrix**

`ym-data smoke --live` must call one bounded example for:

- `realtime_market`
- `sector_index`
- `stock_snapshot`
- `stock_kline`
- explicit-query `review_sentiment`
- `market_limit_state`
- `stock_event`
- TDX probe if configured
- Wind probe if configured

Write sanitized JSON to:

```text
~/.ym-stock-data/smoke/YYYY-MM-DDTHHMMSS+0800.json
```

Each row stores intent, params with no secret values, status, provider used, attempts, row count, latency, and error codes.

- [ ] **Step 3: Make compare consume public results**

`scripts/compare_external_sources.py` must stop writing `manual_tdx_mcp.status=not_called` as if it were a completed provider check. It should call the public API, record `auth_missing`/`unavailable` explicitly, and never invent a source result.

- [ ] **Step 4: Verify offline and one current live run**

```bash
uv run python -m unittest discover -s tests -v
uv run ym-data smoke --live
uv run python - <<'PY'
import json
from pathlib import Path

smoke_dir = Path.home() / ".ym-stock-data" / "smoke"
latest = max(smoke_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
json.loads(latest.read_text(encoding="utf-8"))
print(latest)
PY
```

Expected: zero-auth core routes succeed; WenCai/TDX/Wind reflect their actual current states without crashing the entire run.

- [ ] **Step 5: Commit**

```bash
git add ym_stock_data/smoke.py ym_stock_data/__main__.py \
        scripts/compare_external_sources.py tests
git diff --cached --check
git commit -m "test: add explicit live provider smoke gate"
```

---

### Task 12: Migrate downstream consumers one at a time

**Files:**
- Modify: `/Users/yimu/Documents/YM_Capital/Market_Watch/scripts/run_c15_scan.py`
- Modify: `/Users/yimu/Documents/YM_Capital/live-dashboard/scripts/collectors/iwencai_poll.py`
- Modify: `/Users/yimu/Documents/YM_Capital/live-dashboard/scripts/poll_iwencai.py`
- Modify: `/Users/yimu/Documents/YM_Capital/live-dashboard/scripts/snapshot_auction.py`
- Modify only if required by parity: live-dashboard market/quote collectors.
- Modify: relevant Market Watch/YiMu_IR Skills and AGENTS routing instructions.
- Test: corresponding downstream unit tests.

- [ ] **Step 1: Produce a read-only consumer inventory**

```bash
rg -n 'from ym_stock_data|import ym_stock_data|sources\.iwencai|v2\.resolve' \
  /Users/yimu/Documents/YM_Capital/live-dashboard \
  /Users/yimu/Documents/YM_Capital/Market_Watch \
  /Users/yimu/Documents/YM_Capital/YiMu_IR \
  -g '*.py' -g '*.md' -g '!**/outputs/**' -g '!**/_archive/**'
```

Classify each as production, review, research, test, or documentation before editing.

- [ ] **Step 2: Migrate Market Watch first**

Replace its V2 import with `from ym_stock_data import query`, preserve the existing C1.5 quality/ledger contract, and add a regression asserting `attempts`, `provider_used`, and degraded observation-only behavior survive.

Run:

```bash
cd /Users/yimu/Documents/YM_Capital/Market_Watch
python3 -m unittest tests.test_run_c15_scan -v
```

- [ ] **Step 3: Migrate the non-writing live-dashboard query helper**

Update `scripts/poll_iwencai.py` first. Preserve its output schema and explicitly log provider/error status. Do not write production data during verification; run without `--save`.

- [ ] **Step 4: Migrate scheduled collectors behind a rollback switch**

Add one environment-controlled compatibility switch:

```text
YM_DATA_API_MODE=unified|legacy
```

Default to `legacy` for the first verification run, compare both results without allowing unified empty/error output to overwrite a valid legacy cache, then switch the default only after comparison evidence passes.

- [ ] **Step 5: Verify live-dashboard without POST or production writes**

Run its preflight and targeted collector tests. Use mocks or temporary output paths. Do not execute `--save`, deployment, restart, or any command that mutates production runtime data.

- [ ] **Step 6: Update Skill routing**

All Agent-facing instructions must say:

```python
from ym_stock_data import query
```

They must instruct `ym-data doctor --json` when a provider fails. Remove advice that requires remembering V1 vs V2 or manually discovering the Wind sidecar. Keep TDX/Wind provenance and trade-safety boundaries.

- [ ] **Step 7: Commit each repository separately with explicit paths**

Do not mix pipeline, Market Watch, live-dashboard, or YiMu_IR changes in one Git commit. Do not push.

---

### Task 13: Documentation, deprecation, and final static gates

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/YM-data-pipeline-2.0-数据源治理方案.md`
- Modify: `docs/YM-data-pipeline-v2.0-MVP-试运行记录.md`

- [ ] **Step 1: Replace dual-entry instructions**

README and AGENTS must present `query()` as the only recommended public entry. V1/V2 sections become compatibility notes, not competing usage guides.

- [ ] **Step 2: Document provider ownership**

For every provider state whether it is zero-auth, API key, owned OAuth, or official CLI config; state setup command, doctor state, intended capabilities, and whether automatic fallback is allowed.

- [ ] **Step 3: Add deprecation tests**

Legacy APIs must remain functional and may emit `DeprecationWarning`, but warnings must not break existing consumers. No removal date is promised until downstream migration evidence exists.

- [ ] **Step 4: Run static gates**

```bash
uv run python -m compileall -q ym_stock_data scripts tests
uv run python -m unittest discover -s tests -v
git diff --check
rg -n '/legacy/external|external/data-venv' ym_stock_data README.md AGENTS.md
rg -n 'from ym_stock_data\.sources|ym_stock_data\.v2\.resolve' \
  /Users/yimu/Documents/YM_Capital/live-dashboard/scripts \
  /Users/yimu/Documents/YM_Capital/Market_Watch/scripts
```

Expected: no hard-coded pywencai runtime; no migrated production consumer bypasses the public API.

- [ ] **Step 5: Commit documentation and deprecation work**

```bash
git add README.md AGENTS.md docs ym_stock_data tests
git diff --cached --check
git commit -m "docs: make unified query the formal data channel"
```

Review the staged file list first so this command does not capture unrelated dirty files; replace the broad pathspec with explicit files if any unrelated work remains.

---

### Task 14: Five-trading-day acceptance and closure receipt

> **2026-07-29 CLI environment amendment:** The formal repo CLI entry is the root launcher `./ym-data`. It selects a checkout-specific external uv environment so canonical checkouts managed by macOS File Provider do not depend on a hidden project-local editable `.pth`. Bare `uv run ym-data ...` remains a lower-level command for environments not affected by File Provider metadata.

> **2026-07-30 provider-scope amendment:** The 新五源范围（五类受管来源）is exactly WenCai OpenAPI, portable pywencai, TDX owned OAuth, the official Wind CLI, and zero-auth PyTDX. The natural-language screener chain remains four sources: `iwencai_openapi` → `pywencai` → `tdx_screener` → `wind_screener`. A constrained `pytdx_screener` is now enabled only when a query contains one reviewed沪深 universe, at least one reviewed filter, and is completely consumed by `pytdx-structured-1`; it then becomes the fifth and final route provider. It supports `非ST`, `非停牌`, single-code, `最新价`, and `涨幅` AND filters, fixes `pytdx==1.72`, and does not support北交所 or 行业、概念、PE、PB、排名、OR 或日期. Because this dynamic fifth-source route, the Wind screener route, and the empty/error overwrite guard changed after the earlier observations, the five-trading-day 验收窗口必须重新开始 from the first eligible trading day after this amendment; earlier evidence may be retained as historical context but cannot count toward the restarted five-day graduation gate.

**Files:**
- Create outside Git: `~/.ym-stock-data/acceptance/YYYY-MM-DD.json`
- Create after the observation window: `docs/audit/YYYY-MM-DD-unified-channel-acceptance.md`

- [ ] **Step 1: Define the daily acceptance command**

```bash
cd /Users/yimu/Documents/YM_Capital/YM-data-pipeline
./ym-data acceptance template --date YYYY-MM-DD
```

Run only after 16:10 Asia/Shanghai and follow `docs/ACCEPTANCE_RUNBOOK.md` as the single authoritative daily procedure. Its formal health-check entry is `./ym-data doctor --json`; the runbook owns exact same-day deduplication, official calendar confirmation, that one doctor/smoke run, sanitized downstream probes, build/validate, and final self-check, so do not duplicate its schema here.

- [ ] **Step 2: Record exact daily gates**

Each daily record must contain:

- Git HEAD and dirty status summary;
- doctor provider states;
- success/error/empty counts by intent;
- provider attempt chains;
- OpenAPI 401 count and whether the shared breaker prevented repeats;
- pywencai success rate and error codes;
- TDX/Wind auth and small read-only probe state when configured;
- P50/P95 latency;
- zero secret leakage assertion;
- downstream comparison status.

- [ ] **Step 3: Apply graduation rules**

An automatic fallback may remain enabled only when all are true:

1. required semantics match;
2. five trading days completed;
3. at least 20 representative cases completed for TDX/Wind capabilities being promoted;
4. no silent empty overwrite;
5. errors and authentication remain visible;
6. no trade authorization behavior is introduced.

Otherwise keep that provider explicit or cross-check-only and record the remaining source gap.

- [ ] **Step 4: Produce the closure receipt**

The final audit document must list:

- exact commits per repository;
- exact tests and live probes run;
- five dated snapshot paths and hashes;
- provider graduation decisions;
- consumer migration and rollback state;
- unresolved auth/source gaps;
- confirmation that no broker/trading mutation occurred;
- whether the old direct-source/V2 recommendations can be retired.

- [ ] **Step 5: Independent review gate**

Return the completed implementation to the original audit task. The reviewer must inspect actual diffs, rerun task-specific tests, read current `doctor`/smoke outputs, and verify downstream imports. Implementation self-report alone is not sufficient for closure.

---

## Execution checkpoints

The implementation task must stop and report at these checkpoints:

1. After Tasks 1–5: contract, state, and portable WenCai foundation.
2. After Tasks 6–8: unified API, legacy parity, and doctor.
3. After Tasks 9–11: TDX/Wind providers and live smoke.
4. Before Task 12 writes outside this repository: show the proposed downstream diff scope.
5. After Task 13: provide complete per-repository diff and test summary.
6. After five trading days: return the acceptance receipt for independent closure review.

At any checkpoint, do not hide a provider failure behind passing unit tests. A provider can be implemented correctly while live state remains `auth_missing`, `auth_expired`, or `unavailable`; report those separately.

## Self-review checklist

- [x] One public API is defined and legacy APIs converge on it.
- [x] WenCai 401, pywencai portability, process-shared breakers, and real live checks are covered.
- [x] TDX and Wind discovery/auth/runtime ownership are covered without exposing credentials.
- [x] Semantic fallback boundaries are explicit by intent.
- [x] Existing Wind dirty work is preserved and migrated only in its own task.
- [x] Downstream migration is staged with rollback and no production writes during verification.
- [x] Five-trading-day evidence and independent review are required for closure.
- [x] No trading, deployment, push, or secret-handling authority is inferred.
