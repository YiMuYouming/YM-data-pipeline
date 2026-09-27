# YM-data-pipeline V3.0 Implementation Plan

> **Execution:** When implementation is authorized, continue through the tasks in the current session. Delegate only under applicable user/project rules. No additional skill is a prerequisite. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one auditable data SSOT for every YiMu Capital agent and consumer, qualify StockToday method by method, and promote it only for canonical capabilities whose live evidence satisfies the V3 gate.

**Architecture:** `from ym_stock_data import query` and `./ym-data` remain the only public runtime entry points. V3 adds a frozen vendor-method inventory, a resumable live audit runner, an evidence-bound provider policy, and a repository-owned Skill; consumers never choose or chain providers themselves. The current result contract remains backward compatible while V3 adds provenance and promotion metadata additively.

**Tech Stack:** Python 3.11+, stdlib `unittest`, `requests`, SQLite, JSON, macOS Keychain, existing `uv` launcher.

**Authority and safety:** The StockToday Token is loaded only through `ym_stock_data.providers.stocktoday_auth.load_token`. Tests use synthetic tokens. No code may read `TUSHARE_TOKEN`, print upstream bodies, use HTTP backup IPs, follow redirects, call trading endpoints, write live-dashboard runtime data, or send a POST to port 8088. Existing unrelated dirty files are preserved. Commit, push, deployment and service restart are outside this plan.

**Continuity:** Current verified progress, review blockers and recovery steps are recorded in `docs/superpowers/progress/2026-09-22-ym-data-pipeline-v3.md`. Update that record after each task or material review result.

---

## File structure

- Create `ym_stock_data/providers/stocktoday_inventory.py`: validate and load the frozen vendor catalog.
- Create `ym_stock_data/providers/stocktoday_methods.v3.json`: generated, credential-free snapshot of the public methods endpoint.
- Create `scripts/refresh_stocktoday_inventory.py`: explicit HTTPS catalog refresh/check command.
- Create `ym_stock_data/stocktoday_audit.py`: resumable, rate-bounded live audit engine and classifications.
- Create `ym_stock_data/v3/stocktoday-probes.v3.json`: packaged deterministic probe parameters and acceptance expectations for every method.
- Create `ym_stock_data/v3/provider-policy.v3.json`: packaged capability-specific primary/fallback policy, initially evidence-gated.
- Create `ym_stock_data/provider_policy.py`: validate policy and bind it to audit evidence hashes.
- Create `tests/test_stocktoday_inventory.py`, `tests/test_stocktoday_audit.py`, `tests/test_provider_policy_v3.py`, `tests/test_consumer_inventory_v3.py`.
- Create `scripts/audit_data_consumers.py`: detect production bypasses of the public API.
- Create `config/data-consumers.v3.json`: authoritative consumer and migration inventory.
- Create `skills/ym-a-stock-pipeline/SKILL.md`: repository-owned, platform-neutral agent instructions.
- Modify `ym_stock_data/providers/stocktoday_catalog.py`, `ym_stock_data/providers/stocktoday.py`, `ym_stock_data/routing.py`, `ym_stock_data/api.py`, `ym_stock_data/__main__.py`, `ym_stock_data/v2/capabilities.py`, `README.md`, `docs/agent-contract.md`, `docs/INSTALL.md`, and `docs/STOCKTODAY.md`.

---

### Task 1: Freeze and validate the complete StockToday inventory

**Files:**
- Create: `tests/test_stocktoday_inventory.py`
- Create: `ym_stock_data/providers/stocktoday_inventory.py`
- Create: `ym_stock_data/providers/stocktoday_methods.v3.json`
- Create: `scripts/refresh_stocktoday_inventory.py`
- Modify: `ym_stock_data/providers/stocktoday_catalog.py`
- Modify: `pyproject.toml`

- [x] **Step 1: Write the failing inventory tests**

The test must prove that the frozen catalog is internally consistent, contains the 245 unique methods currently returned by `https://stocktoday.cn/api/tools/methods`, records the source URL and SHA-256, and treats `token_info` as a separate account diagnostic rather than a data method.

```python
class StockTodayInventoryTests(unittest.TestCase):
    def test_frozen_inventory_is_complete_and_unique(self):
        inventory = load_inventory()
        names = [item["name"] for item in inventory["methods"]]
        self.assertEqual(245, len(names))
        self.assertEqual(245, len(set(names)))
        self.assertEqual("https://stocktoday.cn/api/tools/methods", inventory["source_url"])
        self.assertRegex(inventory["payload_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("token_info", names)

    def test_inventory_rejects_duplicate_or_unsafe_method_names(self):
        with self.assertRaises(ValueError):
            validate_inventory({"methods": [{"name": "daily"}, {"name": "daily"}]})
        with self.assertRaises(ValueError):
            validate_inventory({"methods": [{"name": "../daily"}]})
```

- [x] **Step 2: Run the test and verify RED**

Run:

```bash
UV_PROJECT_ENVIRONMENT=/Users/yimu/.cache/uv/ym-stock-data-project-envs/820fc41fa4498762b7c09fa0c0aac4fb7ce8dc7116fcc4cf86704dd62204e950 /opt/homebrew/bin/uv run --no-sync python -m unittest discover -s tests -p 'test_stocktoday_inventory.py' -v
```

Expected: import failure because `stocktoday_inventory` does not exist.

- [x] **Step 3: Implement the inventory loader and refresh script**

Required public surface:

```python
INVENTORY_PATH = Path(__file__).with_name("stocktoday_methods.v3.json")
METHOD_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

def validate_inventory(payload: dict) -> dict: ...
def load_inventory(path: Path = INVENTORY_PATH) -> dict: ...
def method_map(path: Path = INVENTORY_PATH) -> dict[str, dict]: ...
```

The refresh script must use only the fixed HTTPS URL, `timeout=(4, 20)`, and `allow_redirects=False`; require a successful response envelope whose declared method total equals the flattened total; flatten groups without losing category/subcategory, examples, parameter defaults or original supplier order; sort methods by name; write atomically; and support `--check` so CI can report drift without changing files. Source and frozen payloads must reject unknown structural fields, sensitive key names and token-shaped example/default values. It must never accept a custom URL or credential. Package configuration must include the frozen JSON, and a wheel install smoke test must build from a temporary project copy without changing ignored artifacts in the checkout, then prove that the installed inventory module loads all 245 methods without relying on the source tree.

- [x] **Step 4: Generate the frozen JSON and verify GREEN**

Run:

```bash
UV_PROJECT_ENVIRONMENT=/Users/yimu/.cache/uv/ym-stock-data-project-envs/820fc41fa4498762b7c09fa0c0aac4fb7ce8dc7116fcc4cf86704dd62204e950 /opt/homebrew/bin/uv run --no-sync python scripts/refresh_stocktoday_inventory.py
UV_PROJECT_ENVIRONMENT=/Users/yimu/.cache/uv/ym-stock-data-project-envs/820fc41fa4498762b7c09fa0c0aac4fb7ce8dc7116fcc4cf86704dd62204e950 /opt/homebrew/bin/uv run --no-sync python -m unittest discover -s tests -p 'test_stocktoday_inventory.py' -v
```

Expected: 245 unique methods; tests pass; generated JSON contains no token-shaped values.

---

### Task 2: Build the complete, resumable live audit harness

**Files:**
- Create: `tests/test_stocktoday_audit.py`
- Create: `ym_stock_data/stocktoday_audit.py`
- Create: `ym_stock_data/v3/__init__.py`
- Create: `ym_stock_data/v3/stocktoday-probes.v3.json`
- Modify: `ym_stock_data/providers/stocktoday.py`
- Modify: `ym_stock_data/__main__.py`
- Modify: `pyproject.toml`

- [x] **Step 1: Write failing tests for coverage, resumption and classifications**

```python
class StockTodayAuditTests(unittest.TestCase):
    def test_probe_manifest_covers_inventory_plus_account_diagnostic(self):
        inventory = load_inventory()
        probes = load_probes()
        self.assertEqual(set(method_map()), set(probes["methods"]))
        self.assertEqual("token_info", probes["account_diagnostic"]["name"])

    def test_audit_never_repeats_completed_case_on_resume(self):
        first = run_audit(cases=[case("daily"), case("stock_basic")], transport=fake_transport)
        second = run_audit(cases=[case("daily"), case("stock_basic")], resume=first, transport=fail_if_called)
        self.assertEqual(first["cases"], second["cases"])

    def test_empty_and_filter_mismatch_are_not_primary_eligible(self):
        self.assertEqual("reachable_empty", classify(valid_empty_response()))
        self.assertEqual("semantic_fail", classify(filter_mismatch_response()))
        self.assertFalse(primary_eligible(classify(valid_empty_response())))
```

- [x] **Step 2: Run the tests and verify RED**

Run the managed `uv run --no-sync python -m unittest discover -s tests -p 'test_stocktoday_audit.py' -v` command with the repository's external `UV_PROJECT_ENVIRONMENT`.

Expected: import failure because the audit module and probe manifest do not exist.

- [x] **Step 3: Implement the audit engine**

Required classifications are exactly:

```python
AUDIT_STATUSES = {
    "pass_nonempty", "reachable_empty", "auth_denied", "rate_limited",
    "timeout", "invalid_params", "schema_error", "semantic_fail",
    "provider_error", "not_run",
}
```

Each case receipt must contain method, category, sanitized parameters, requested fields, start/end timestamps, latency, row count, column names, provider code, classification, filter checks, duplicate-key checks, pagination/truncation checks, and a payload hash. It must not contain returned rows, response messages, the Token, Keychain metadata, host override or exception text.

The runner must execute sequentially, honor the existing shared `RequestBudget`, persist an atomic checkpoint after every case, resume by `(inventory_sha256, probe_manifest_sha256, method)`, and stop cleanly on repeated rate-limit/auth errors. A valid empty proves reachability only; it does not qualify the method as a primary source.

- [x] **Step 4: Build probe coverage for all 245 methods**

Use each vendor example as the first probe. For the 36 methods without examples, add explicit sanitized parameters in `ym_stock_data/v3/stocktoday-probes.v3.json`. Sparse methods may have a second historical probe, but every extra request must be declared in the manifest. Dates must be fixed reproducible historical dates or resolved from the canonical trade calendar and then written into the receipt. Package configuration and a wheel-install smoke test must prove that the canonical manifest is available outside the source checkout; no duplicate root-level copy is allowed.

- [x] **Step 5: Expose CLI commands without leaking credentials**

Add:

```text
./ym-data stocktoday inventory --json
./ym-data stocktoday audit --live --output <absolute-or-repo-relative-json>
./ym-data stocktoday audit --live --resume <receipt.json> --output <receipt.json>
./ym-data stocktoday audit-status <receipt.json>
```

`--live` is mandatory. Output paths must reject symlinks and directories outside the repository `outputs/` or the approved shared audit directory. Stdout contains counts and classifications only.

- [x] **Step 6: Verify GREEN**

Run focused tests, compileall and CLI help. Expected: every inventory method has at least one probe; no network is used by unit tests.

---

### Task 3: Add an evidence-bound V3 provider policy

**Files:**
- Create: `tests/test_provider_policy_v3.py`
- Create: `ym_stock_data/provider_policy.py`
- Create: `ym_stock_data/v3/provider-policy.v3.json`
- Modify: `ym_stock_data/routing.py`
- Modify: `ym_stock_data/api.py`
- Modify: `ym_stock_data/v2/capabilities.py`

- [x] **Step 1: Write failing policy tests**

```python
class ProviderPolicyV3Tests(unittest.TestCase):
    def test_stocktoday_cannot_be_primary_without_matching_pass_evidence(self):
        policy = policy_with_primary("stock_snapshot", "stocktoday", evidence_sha="missing")
        with self.assertRaises(PolicyEvidenceError):
            compile_policy(policy, audit_receipt=passing_receipt(sha="other"))

    def test_primary_is_capability_specific(self):
        compiled = compile_policy(snapshot_pass_policy(), audit_receipt=snapshot_pass_receipt())
        self.assertEqual("stocktoday", compiled.route("stock_snapshot").providers[0])
        self.assertNotEqual("stocktoday", compiled.route("stock_kline", {"period": "60m"}).providers[0])

    def test_fallback_success_cannot_hide_failed_primary(self):
        result = query_with_policy(primary_timeout_then_fallback_empty())
        self.assertEqual("error", result["_meta"]["status"])
        self.assertEqual(["stocktoday", "pytdx"], result["_meta"]["source_chain"])
```

- [x] **Step 2: Run tests and verify RED**

Run the managed `uv run --no-sync python -m unittest discover -s tests -p 'test_provider_policy_v3.py' -v` command with the repository's external `UV_PROJECT_ENVIRONMENT`.

- [x] **Step 3: Implement additive policy metadata and fail-closed loading**

The policy schema must include `policy_version`, `pipeline_version`, per-capability provider order, empty policy, maximum age, minimum audit classification, audit receipt SHA-256 and expiration. Missing, malformed, expired or mismatched policy evidence must leave current V2 routes unchanged and report `policy_status="inactive"`; it must never silently promote StockToday.

Add these `_meta` fields without removing contract 1.0 fields:

```python
{
    "pipeline_version": "3.0",
    "route_policy_version": "3.0",
    "source_tier": "primary|fallback|explicit",
    "policy_evidence_sha256": "...|null",
}
```

- [x] **Step 4: Keep the initial policy inactive until the live audit is reviewed**

`ym_stock_data/v3/provider-policy.v3.json` must contain the intended capability mappings but set StockToday promotion to inactive. No production route changes in this task.

- [x] **Step 5: Verify GREEN and legacy compatibility**

Run focused policy/routing tests, all legacy compatibility tests, and the full unit suite.

---

### Task 4: Make the repository-owned CLI and Skill the only agent entry

**Files:**
- Create: `skills/ym-a-stock-pipeline/SKILL.md`
- Create: `tests/test_global_skill_v3.py`
- Modify: `README.md`
- Modify: `docs/agent-contract.md`
- Modify: `docs/INSTALL.md`
- Modify: `docs/STOCKTODAY.md`

**状态：已完成并通过双重复审（spec/quality 均 APPROVED，P0/P1/P2 = 0/0/0）。**

- [x] **Step 1: Write the failing documentation/Skill contract test**

The test must require the Skill to name only `from ym_stock_data import query` and `./ym-data`, forbid direct `tushare`, private provider imports, arbitrary URLs, consumer-side fallback and Token arguments, and document the V3 provenance fields.

- [x] **Step 2: Run the test and verify RED**

Run the managed `uv run --no-sync python -m unittest discover -s tests -p 'test_global_skill_v3.py' -v` command with the repository's external `UV_PROJECT_ENVIRONMENT`.

- [x] **Step 3: Create the canonical Skill**

The Skill must route natural-language requests to canonical intents, show CLI/Python examples, explain quality and source-gap semantics, and instruct every agent to stop at the public contract. StockToday-specific method calls remain available through `stocktoday_data`, but agents may not infer that catalog presence means acceptance.

- [x] **Step 4: Install from the repository without duplicate editable copies**

After verification, replace the Codex Skill entry with a symlink to `skills/ym-a-stock-pipeline`, and use the same repository path for any other local agent platform that supports filesystem Skills. Do not invent a WorkBuddy/OpenClaw location; detect and verify each target before installing. The CLI remains the universal fallback for platforms without Skills.

- [x] **Step 5: Verify GREEN**

Run documentation contract tests, resolve the symlink, and invoke `./ym-data --help` from a fresh shell.

#### Task 4 第二轮复审修复（2026-09-23）

- [x] 用 mutation tests 覆盖 ```、~~~、缩进块和 inline/prose 变体；canonical Skill 改为全文扫描 forbidden runtime symbols、私有 imports、vendor URLs 和 credential arguments。
- [x] 从 `all_route_specs()` 动态断言 Skill 覆盖全部 canonical intents，并补齐 `market_limit_state` 与 `stock_event`。
- [x] 将 WorkBuddy backup 归一为自包含相对软链接 `workbuddy -> agents`，manifest 同时保留 original target 与 normalized restore target。
- [x] 收紧 `docs/STOCKTODAY.md` 为 Agent-facing public contract；移除原生安装、私有客户端、内部 endpoint 和 secure-store 细节，保留历史审计路径与禁止 runtime entry 说明；更新 active acceptance Runbook 到 Projects checkout。
- [x] 增加 dev-only `pyyaml==6.0.3`、离线 lock 和无 `PYTHONPATH` 的 official validator 命令；Task 4 仍等待独立复审。

#### Task 4 第三轮复审修复（2026-09-23）

- [x] 先以 mutation tests 复现 mixed root import、spaced dotted import、alias、括号、多行括号、分号和反斜杠续行绕过；再将 scanner 改为全文结构化 import gate，只允许精确的 `from ym_stock_data import query`。
- [x] global Skill 测试从 backup parent 按 manifest 的 canonical replacement 动态选择确定性最新有效批次，不固定日期目录；当前四个 active canonical symlink 保持不变。
- [x] 在既有 backup 批次增加自包含 `inventory.json` 与目录级 `entries_sha256`，逐项覆盖 codex/agents/claude 内容和 `workbuddy -> agents` 相对软链接；manifest/inventory 的排除规则避免 self-hash 循环，并明确 scope 仅四个 exact installation targets。
- [x] 保持历史文档/V2 示例的 historical classification 与 active-doc allowlist，不批量改写历史材料；Task 5 仍未开始。

#### Task 4 第四轮复审修复（2026-09-23）

- [x] 先以 mutation tests 复现 split-string `importlib.import_module()`、`__import__()` 及动态执行原语绕过；再加入保守 fail-closed gate，拒绝 `importlib`、`import_module`、`__import__`、`exec`、`eval`、`compile` 和 `__builtins__`，不宣称抵御任意恶意混淆。
- [x] 将 `_discover_backup_root()` 收紧为真实目录、严格 `YYYYMMDDTHHMMSS±HHMM` 名称、manifest `Created` aware 时间与目录名一致、manifest/inventory canonical replacement 一致、inventory scope/四个 exact targets 完整一致；多候选按 aware datetime 选择最新有效批次。
- [x] 用 tempfile 覆盖 symlink、裸目录、名称/Created 不一致、manifest/inventory mismatch、两个不同 offset 的有效批次，证明不会按 lexicographic artifact 选择；当前四端 active symlink 与备份实体内容不变。
- [x] 历史文档继续保持 historical classification，不作为 active runtime/doc gate；Task 5 仍未开始。

---

### Task 5: Inventory and migrate every production consumer

**Files:**
- Create: `tests/test_consumer_inventory_v3.py`
- Create: `scripts/audit_data_consumers.py`
- Create: `config/data-consumers.v3.json`
- Generate when the Keychain credential is available: `outputs/consumer-audit/<timestamp>/jev-sidecar.json`
- Modify only after reading each repository's `AGENTS.md`: production entry points in `Market_Watch`, `live-dashboard`, `YiMu_IR`, `YiMu_Quant`, `YiMu_Intelligence`, `live-trading`, `portal`, and `ai-rule-system`.

- [x] **Step 1: Write the failing inventory test**

The test must fail when a production file imports `ym_stock_data.sources`, `ym_stock_data.v2`, `tushare`, `pytdx`, `akshare`, `pywencai`, `easyquotation`, or calls known vendor URLs outside the pipeline provider package. Tests, archives, fixtures and generated reports are classified separately and cannot hide production violations.

- [x] **Step 2: Run the test and verify RED against the current tree**

Expected: current direct/bypass consumers are listed with repository, file, line, dependency and migration owner.

- [x] **Step 3: Freeze `config/data-consumers.v3.json`**

Each consumer record must contain repository, entry point, business shape, canonical intent, freshness requirement, current source, target policy, rollback switch and migration status. Unknown consumers are a failing audit result.

After the deterministic scanner has produced the complete candidate set, a bounded TypeSafe/JEV sidecar may classify ambiguous findings as production bypass, legal provider-internal code, test/fixture, archive/generated artifact, or human-review-required. Code owns inclusion and migration status: JEV failure, low confidence or no-match keeps the item in the audit and may never suppress a finding. Preserve the selected label, full probability distribution, confidence, model/version, sanitized state hash and request/token counts separately from deterministic evidence.

#### Task 5A inventory freeze（2026-09-23）

- [x] 只读核对八个消费者仓库的 `pwd`/`realpath`、各自 `AGENTS.md`（`live-trading` 无该文件，按其 `README.md` 边界核对）和 dirty worktree；未修改消费者仓库。
- [x] 先写 `tests/test_consumer_inventory_v3.py` 并复现 RED，再实现 stdlib-only `scripts/audit_data_consumers.py`；扫描不执行消费者、不读 Keychain、不联网、不调用 provider。
- [x] 冻结 `config/data-consumers.v3.json`：8 个仓库、32 条已分类 consumer/entry 记录；未知/未分类状态由 schema 校验拒绝。
- [x] 真实扫描输出 `outputs/consumer-audit/20260923T014826+0800/`：7,978 个 file entries、71 个显式工具/依赖排除、26,584 条结构化 findings；production bypass 431、approved compatibility 49、test 4,507、archive 1,970、generated 18,449、documentation 1,178、unknown 0、ambiguous 0。
- [x] manifest `file_set_sha256` 可重算，findings/summary 与落盘结果一致；不写 source excerpt、URL query、凭据或原始回退文本。JEV 未启动，因为当前分类结果全部由确定性规则给出且没有 ambiguous finding。
- [ ] Task 5 Step 4/5：逐仓库迁移、改生产代码、运行消费者脚本、provider/StockToday 真实审计均未开始。

#### Task 5A 第二轮修复（2026-09-23）

- [x] 将 detection 与 classification 分离：provider token/UI 标签为 production_reference，依赖清单为 dependency_metadata，.superpowers/brainstorm 为 documentation，compiled/ 为 generated；board_history.py 保持 production。
- [x] 泛化 HTTP client 不再自动判 bypass；内部 GET 分类为 approved_internal_transport，POST/PUT/PATCH/DELETE 或 8088 写形态分类为 production_write_transport 并 fail closed；multiline vendor call 使用有限上下文。
- [x] compatibility allowlist 改为 repository + relative path + match_kind + purpose；同名路径跨 repo 不会放行；外部 production symlink、oversized/read error 继续 fail closed。
- [x] config/data-consumers.v3.json 从 32 条扩至 39 条并新增 finding_files；真实 production bypass 14 个文件均有且仅有一个 owner，覆盖为 14/14，unclassified 为 0。
- [x] 第二批输出位于 outputs/consumer-audit/20260923T023403+0800/：7,978 个 file entries、71 个显式排除、23,282 条 findings；production bypass 43、production reference 355、dependency metadata 9、approved internal transport 30、production write transport 6、approved compatibility 9、test 4,308、archive 1,899、generated 15,554、documentation 1,069、unknown 0、ambiguous 0。
- [x] manifest file_set_sha256 为 0804ce88ea659a26b6e986836f800038915e82d6a79ea9fcd61b09506475cfea，findings logical SHA-256 为 627f732f023a31c74b1962cda71bca5bc0b85e509652ba929dc49bd76c379599；覆盖、脱敏和配置 schema 验证通过。
- [x] TDD/验证已覆盖同名 allowlist、generic/internal GET、multiline vendor、internal POST/8088 write、dependency manifest、UI provider label、external symlink、oversized/read error 和 CLI fail-closed；全量 unittest 581 passed、5 skipped。CLI 因 6 条 production_write_transport 返回 1，不能将写路径自动批准为 compatibility。

- [ ] **Step 4: Migrate one repository at a time under its own tests**

For every consumer, first add a failing test that proves it calls the public API and preserves business shape/provenance/overwrite guards. Then replace direct source logic with `query()`. Do not remove a rollback path until side-by-side evidence passes. Do not alter trade authorization or write production runtime data.

- [ ] **Step 5: Verify all consumer repositories**

Run each repository's documented focused and full tests. The consumer audit must report zero unclassified production bypasses.

---

### Task 6: Audit every live provider and decide StockToday promotion per capability

**Files:**
- Generate: `outputs/stocktoday-audit/<timestamp>/receipt.json`
- Generate: `outputs/stocktoday-audit/<timestamp>/summary.md`
- Generate when enabled: `outputs/stocktoday-audit/<timestamp>/jev-semantic-sidecar.json`
- Generate: `outputs/provider-audit/<timestamp>/provider-smoke.v3.json`
- Create after independent review: `ym_stock_data/v3/stocktoday-audit-receipt.v3.json`
- Modify after review: `ym_stock_data/v3/provider-policy.v3.json` and its code-side logical SHA-256 approval anchor

- [ ] **Step 0: Run a read-only smoke matrix over every registered/local provider**

Start from the exact runtime registry and `./ym-data doctor --json`; do not maintain a hand-copied provider list. Exercise each registered provider through its real adapter with a safe canonical probe for every capability it claims. Include the WorkBuddy-connected Tencent WeStock and TDX channels as external fallback candidates, but call only read-only market-data tools. Record provider, capability, auth status, runtime route, latency, rows, columns, observation timestamp, newest fact timestamp, freshness, filter/shape checks, classification and sanitized error code. A configured credential or WorkBuddy “去对话” state proves connection only, not data readiness. Any untested or `configured_unverified` provider is ineligible for V3 automatic primary/fallback routing.

- [ ] **Step 1: Run the 245-method audit plus `token_info` diagnostic**

Use the managed launcher and Keychain-backed credential. Run sequentially and resume the same receipt after rate-limit or network interruption; never restart completed calls.

- [ ] **Step 2: Independently verify the receipt**

Check inventory/probe hashes, exact case coverage, zero duplicate method IDs, redaction, row/column plausibility, filter adherence, duplicate keys, truncation and rate-budget evidence. Re-run a small stratified sample from every category.

JEV may batch narrow semantic judgments over sanitized method descriptions, parameters, requested filters and returned column names to prioritize suspicious pass cases. It receives no rows, Token, authorization header or Keychain metadata. Its score and confidence remain separate from deterministic classification and evidence coverage; service failure is no-loss, and no JEV answer may promote a method or remove a re-test.

- [ ] **Step 3: Apply promotion criteria**

A canonical capability may promote StockToday only when all required methods are `pass_nonempty`, field/unit/time semantics are proven, freshness meets the consumer SLA, coverage is complete, and the same probe passes repeatedly. `reachable_empty`, one-off success, unknown minute-bar closure, filter mismatch, duplicate quotes or truncation are disqualifying until resolved.

- [ ] **Step 4: Update and test the active policy**

Bind each promotion to the reviewed receipt hash and expiry. Run routing tests and live read-only smoke queries. Do not deploy or restart consumers in this task.

- [ ] **Step 5: Reconcile the provider matrix with the final policy**

Every provider named by an automatic V3 route must have current passing capability evidence in `provider-smoke.v3.json` or the reviewed StockToday receipt. Providers that are missing, stale, auth-failed, semantically incompatible or reachable-empty remain explicit/manual diagnostics only. Verify that fallback order cannot silently substitute a lower-quality source while reporting a clean result.

---

### Task 7: Final independent review and acceptance

**Files:**
- Generate: `outputs/v3-acceptance/<timestamp>/verification.json`
- Generate: `outputs/v3-acceptance/<timestamp>/consumer-matrix.json`

- [ ] **Step 1: Spec compliance review**

Map every requirement in this plan to a file, test or receipt. Missing evidence is incomplete.

- [ ] **Step 2: Code quality review**

Review credential boundaries, URL allowlists, atomic writes, resume semantics, contract compatibility, fallback behavior, cache/snapshot semantics and consumer rollback switches.

- [ ] **Step 3: Fresh verification**

Run:

```bash
./ym-data doctor --json
UV_PROJECT_ENVIRONMENT=/Users/yimu/.cache/uv/ym-stock-data-project-envs/820fc41fa4498762b7c09fa0c0aac4fb7ce8dc7116fcc4cf86704dd62204e950 /opt/homebrew/bin/uv run --no-sync python -m compileall -q ym_stock_data scripts tests
UV_PROJECT_ENVIRONMENT=/Users/yimu/.cache/uv/ym-stock-data-project-envs/820fc41fa4498762b7c09fa0c0aac4fb7ce8dc7116fcc4cf86704dd62204e950 /opt/homebrew/bin/uv run --no-sync python -m unittest discover -s tests -v
git diff --check
```

Then run each migrated consumer's documented test command and a secret scan that compares in-memory Keychain values without printing them.

- [ ] **Step 4: Production-cutover gate**

Cutover is eligible only when all tests pass, all production consumers are classified, promoted capabilities have current live evidence, and rollback commands are documented. Commit, push, deployment, service restart and runtime-data migration require their own authorization and are not implied by passing this gate.

---

## Self-review

- Spec coverage: full method inventory, live audit, capability-level promotion, canonical Skill/CLI, secure Key usage, all consumers, fallback isolation and independent acceptance are represented.
- Placeholder scan: no `TBD`, `TODO`, “similar to”, or unbounded “add tests” steps remain.
- Type consistency: inventory SHA, probe SHA, audit receipt SHA and policy evidence SHA are distinct names throughout; StockToday method inventory excludes the separate `token_info` diagnostic.
- Current evidence incorporated: the live methods endpoint exposes 245 methods; the installed reference lists 235 and includes `token_info`, so V3 treats documentation and live catalog as separate versioned inputs.
