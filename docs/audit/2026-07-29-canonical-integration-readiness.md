# Canonical integration readiness audit — 2026-07-29

## 结论

状态：`integration_blocked`。本文是只读 readiness 证据，不表示已经集成，也不授权 Task 14。

正式路径 `/Users/yimu/Documents/YM_Capital/YM-data-pipeline` 仍停在不含 public `query` 的 `f246fef`。冻结范围共有 11 个受保护叶子路径：9 个 Git-visible 源码/文档状态路径，以及 2 个被 `.gitignore` 折叠的 generated `.pyc`。9 个源码/文档文件现已通过临时 Git index 保存到独立 preservation ref，并逐项验证 blob 一致；两个 `.pyc` 只保留在本文 SHA256 清单中。canonical switch 仍未授权、未执行，因此状态继续是 `integration_blocked`。

## 冻结快照

```text
audit_at: 2026-07-29T21:57:31+0800
canonical_path: /Users/yimu/Documents/YM_Capital/YM-data-pipeline
canonical_branch: codex/market-watch-selection-closure
canonical_head: f246fefd7b8f143c81f2bdf5da8d4f8900f7bfea
canonical_staged_paths: 0
canonical_git_visible_status_paths: 9
canonical_generated_ignored_leaf_paths: 2
canonical_protected_leaf_paths: 11
canonical_has_public_query: false
canonical_resolver_exception: ImportError
implementation_path: /Users/yimu/.codex/worktrees/47a4/YM-data-pipeline
implementation_branch: codex/unified-a-share-data-channel-checkpoint4
implementation_reference_head: 88fa1a1bcbc6f7368d93d97c6e723ea056e6cfbc
implementation_head_at_preservation_audit: 8543bcd82dd311b045ee66cce0e84e1aa6c06e88
preservation_branch: codex/wind-sidecar-preservation-f246fef
preservation_commit: 0e802995f9987fac0b3574c241c0184a3d36722a
preservation_parent: f246fefd7b8f143c81f2bdf5da8d4f8900f7bfea
preservation_method: temporary_git_index
preservation_verified_paths: 9
canonical_real_index_unchanged: true
canonical_switch_authorized: false
canonical_switch_executed: false
merge_base: f246fefd7b8f143c81f2bdf5da8d4f8900f7bfea
left_right_count_f246fef_vs_88fa1a1: 0 20
classification_counts: exact_same=1 incorporated_and_evolved=5 intentionally_removed_after_parity=3 generated_binary_excluded=2 unresolved_conflict=0
```

`canonical_has_public_query` 的证据来自 README 规定环境中的真实导入阶段：在 canonical cwd 通过其 `uv` 环境加载 Market_Watch `scripts/run_c15_scan.py::_default_resolver`，实际模块为 canonical `ym_stock_data/__init__.py`，`hasattr(ym_stock_data, "query")` 为 false，并在任何 provider 调用或 scan 写入前抛出 `ImportError`。

## Canonical dirty inventory

SHA256 均为冻结时工作区文件内容，不是 Git blob id。普通 `git status --short --untracked-files=all` 给出 9 个源码/文档状态路径；`git status --ignored=matching` 将 `__pycache__/` 折叠为 ignored 目录，因此又用只读 leaf scan 枚举其中 2 个 `.pyc`。表内 11 行是完整受保护叶子清单；staged 集合为空。generated binary 不作为实现或 parity 证据，但未经用户授权同样不得删除。

<!-- canonical-dirty-inventory:start -->
| path | frozen status | canonical SHA256 | classification | implementation evidence |
| --- | --- | --- | --- | --- |
| `README.md` | tracked_modified | `f012dea69b30f9c54c12306b45835ac3cc85add5f07cc0300589fdcab12f7470` | `incorporated_and_evolved` | `e882690` 注册 Wind，`edf1ca7` 统一入口，`88fa1a1` 枚举 ownership；88fa 文件 SHA256 `99234b0861dfd59b1c6a3f43f316e4e8357b8b4df7a12d8927fd138c8c013bfe` |
| `tests/test_v2_capabilities.py` | tracked_modified | `46e9187964414295908387dc38d355a92704fe21842b5210a522e8d80d4601ec` | `incorporated_and_evolved` | `e882690` 改为验证 registered Wind 与实际 fallback routes；88fa 文件 SHA256 `a11aa22a3f504a30edec22443660696fc310f674e2e3403fbda4e53c4c9285e9` |
| `ym_stock_data/v2/capabilities.py` | tracked_modified | `14d43622a5c097208dd0cdb42b3fa20ade5fd21401b9fcb9794a60abc599cf76` | `incorporated_and_evolved` | `e882690` 从 `api.PROVIDER_REGISTRY` 和 `all_route_specs()` 派生 manifest；88fa 文件 SHA256 `f6b9feab6ae16f30b94ebfac659c4f79897b67b5bf92f83b1bc3259162aad8d4` |
| `docs/Wind-MCP-补充源验证清单.md` | untracked | `c9451155836e1a24ca250aeb0c2cc6dd86b7bb6af3adafca320c4cfabf1e2923` | `incorporated_and_evolved` | `e882690` 新增注册化版本，记录 old sidecar parity 删除、严格 filings fallback 与 Gate 1；88fa 文件 SHA256 `c3c06e368301a6c855cdc1baba7f39dda13a267f7bd33e5ebc222d7a3c353edc` |
| `docs/handoffs/2026-07-22-wind-mcp-sidecar-handoff.md` | untracked | `f3b692b5ddf069dee9e7c7906d59d91ee18dde7bd2eff41d357f4bef1b69a4d8` | `incorporated_and_evolved` | `e882690` 新增迁移 handoff，列出 provider registry、canonical query、固定错误码和 parity 删除；88fa 文件 SHA256 `d4dc580ad1838b2a2a1db43ed47e1b8c1679a3a8ae1f15727625c19e7fe3a650` |
| `docs/superpowers/plans/2026-07-29-unified-a-share-data-channel.md` | untracked | `04f7c9aaf836ed2e1fe3e3220d7cd67ce6e4f5398a6ae0e1c48f7764721e884b` | `exact_same` | 88fa 同路径 SHA256 完全相同；无需重放 |
| `tests/test_wind_sidecar.py` | untracked | `87e7e0ee30a56d878d720fc015b4663e6b8d02c09a56c18410a1ad4cfb735918` | `intentionally_removed_after_parity` | 替代为 `tests/test_provider_wind_mcp.py`，由 `e882690` 创建并经 `ec4757c`、`e524a97` 加固；替代文件 SHA256 `b914a6009a5c234603798182a8975206434c8a161cc8db6cf9aad492fea301a7` |
| `ym_stock_data/experimental/__init__.py` | untracked | `0cfe754a73077ffa59ab53afdeb0faf02222b35376f54191b72e99f2b2406721` | `intentionally_removed_after_parity` | ownership 已进入 `ym_stock_data/providers/__init__.py` 与 `api.PROVIDER_REGISTRY`，提交 `e882690`；不保留 experimental 第二入口 |
| `ym_stock_data/experimental/__pycache__/__init__.cpython-314.pyc` | ignored_generated | `9c63871ab51afea372b85851dba50363f3736fb542ad7665f45e14b6fb6badc8` | `generated_binary_excluded` | Python 3.14 生成物；implementation 无对应实现文件，不用于等价性判断，切换前也不得擅自删除 |
| `ym_stock_data/experimental/__pycache__/wind_sidecar.cpython-314.pyc` | ignored_generated | `bbcd501f6c331ac3e4d0b7c8f7bcd869ceaa95a5839b8dcc422dcba048c13f89` | `generated_binary_excluded` | Python 3.14 生成物；implementation 无对应实现文件，不用于等价性判断，切换前也不得擅自删除 |
| `ym_stock_data/experimental/wind_sidecar.py` | untracked | `706fe0bb8983d70277b81440977f3620f18c9d359752ad36cd054b8891642eba` | `intentionally_removed_after_parity` | 替代为 `ym_stock_data/providers/wind_mcp.py`，由 `e882690` 创建并经 `ec4757c`、`e524a97` 加固；替代文件 SHA256 `278402559ae6c648dba8937b138ebbd3d60e9df4f7b1ee5f5d6964991fd064df` |
<!-- canonical-dirty-inventory:end -->

## 分类判定

### exact_same

实施计划的 canonical 与 implementation SHA256 都是 `04f7c9a...e884b`。它已经存在于 implementation，不需要复制或应用。

### incorporated_and_evolved

五个文件都保留了 canonical dirty 的 Wind 边界意图，但事实所有权已变化：

- README 的 manual experimental sidecar 指引已替换为唯一 `query()`、registry/RouteSpec ownership 和 doctor/smoke 边界。
- capability manifest 从手写 `manual_sources.wind_mcp` 提升为由实际 registry/routes 派生；兼容 alias 不再是第二份事实。
- capability 测试同时验证 `registered_experimental`、显式 `wind_enrichment`、仅 `filings` 自动 fallback 和 forbidden intents。
- Wind 清单与 handoff 保留七项 capability 和非交易边界，并新增严格 payload、鉴权脱敏、单标的参数与真实 CLI shape 证据。

这些文件不是 exact same，且 canonical tracked patch 对 `88fa1a1` 的 `git apply --check` 全部失败；失败位置正是已被统一入口/派生 manifest 取代的旧段落。结论是“已吸收并演化”，不是“可以重新应用”。

### intentionally_removed_after_parity

旧 `experimental` 包和 `tests/test_wind_sidecar.py` 在 canonical 仍是未跟踪文件。implementation 的 `e882690` 建立正式 provider、registry、routing、doctor、normalizer 和替代测试；`ec4757c` 锁定工具参数，`e524a97` 锁定真实 tabular payload。当前 active tree 对 `ym_stock_data.experimental` / `fetch_wind_enrichment` 无代码引用，handoff 只保留历史文件名。

对这三个旧文件执行“从 `/dev/null` 新增”的 `git apply --check` 会机械通过，因为目标路径已不存在；但真正应用会复活第二入口和第二 ownership，违反统一通道边界。因此分类为 parity 后有意删除，而不是缺失文件。

### generated_binary_excluded

两个 `.pyc` 由 canonical 的 Python 3.14 运行产生，被仓库 `.gitignore:1` 的 `__pycache__/` 规则忽略。它们不参与源码、contract 或 parity 证明，也不应带入 implementation commit；但它们是冻结时 canonical 磁盘状态的一部分，必须保留 SHA256 记录，且在任何切换前不得未经授权删除。

### unresolved_conflict

`unresolved_conflict: 0`。这里的 0 表示 9 个源码/文档路径都有语义分类，另 2 个 generated binary 有明确 excluded 规则；不表示 canonical 已可切换。tracked patch 的机械冲突与 canonical 无 public query 仍是 integration blocker。

## 只读模拟证据

执行的命令类别仅包含 `git status`、`shasum`、`git log`、`git diff`、`git diff --no-index`、ancestry 检查和 `git apply --check`。没有 apply、copy、move、delete、checkout、reset、stash 或 canonical 写入。

1. `merge-base(f246fef, 88fa1a1) = f246fef`，左右计数 `0 20`：implementation 是 canonical HEAD 的后继提交链。
2. no-index 结果：计划 0 diff；其余五个保留文件均有内容差异；旧 sidecar/test 与新 provider/test 均是结构性迁移而非重命名。
3. canonical 三个 tracked patch 对 `88fa1a1` 的 `git apply --check` 均报 `patch does not apply`。
4. 两个 Wind 文档和计划按“新增文件”检查时报告目标已存在；其中计划 hash 相同，Wind 文档 hash 不同且已演化。
5. 三个旧 experimental/test 文件按“新增文件”检查会通过，但 active-reference 和 parity 证据判定不得重新引入。

## Preservation ref 复核

后续保护动作使用临时 Git index 建立 `codex/wind-sidecar-preservation-f246fef`，没有切换或写入 canonical worktree，也没有改动 canonical 真实 index。只读复核结果：

1. branch 与 commit 均解析为 `0e802995f9987fac0b3574c241c0184a3d36722a`，唯一父提交为 `f246fefd7b8f143c81f2bdf5da8d4f8900f7bfea`。
2. 对表内 9 个 Git-visible 源码/文档文件逐项执行 canonical 工作树 `git hash-object`，均与 `0e802995:<path>` 的 blob OID 相等，结果 `9/9 MATCH`。
3. preservation commit 相对父提交恰好包含这 9 个显式路径；canonical staged 仍为 0，原有 tracked/untracked 状态保持不变。
4. 两个 ignored `.pyc` 不进入 preservation commit；其 SHA256 仍为表内值，未删除、未改写。

这个 ref 解决的是 dirty 源码/文档字节的可恢复性，不等于 unified implementation 已进入 canonical，也不授权 branch switch。

## 推荐的可恢复集成步骤

preservation ref 已建立并验证；以下集成与切换步骤尚未执行，每一阶段都必须由用户另行明确授权。

1. 保留 implementation ref：`codex/unified-a-share-data-channel-checkpoint4`。本报告的代码比较基准是 `88fa1a1`，preservation 复核开始时 implementation HEAD 是 `8543bcd`；纠偏前回滚 ref 是 `edf1ca7`。
2. 保留 preservation ref：`codex/wind-sidecar-preservation-f246fef` / `0e802995`。两个 `.pyc` 继续只由本文 leaf manifest/hash 保护，不进入 Git commit，也不得从 canonical 删除。
3. 使用 implementation worktree 做消费者集成验证，通过显式 `YM_DATA_PIPELINE_PATH` 指向该路径；Market_Watch、live-dashboard 均使用临时输出/no-save，不进入 Task 14。
4. 只有业务 shape、provider/attempts、合法 empty、error overwrite guard 和 canonical invocation 全部通过后，才讨论正式路径切换。
5. 如获正式切换授权，必须以已验证的 preservation ref 为可恢复基线，先规划独立 integration branch/worktree；不得直接在仍 dirty 的 canonical checkout 上切 branch、reset 或 stash。

## 精确回滚与授权边界

- 当前 canonical 代码 ref：`f246fefd7b8f143c81f2bdf5da8d4f8900f7bfea`。它本身不包含 9 个 Git-visible dirty 文件或 2 个 generated binary 的工作区字节。
- 已验证的源码/文档保护 ref：`codex/wind-sidecar-preservation-f246fef` / `0e802995f9987fac0b3574c241c0184a3d36722a`；父提交为 `f246fef`，包含 9 个显式路径。两个 generated binary 仅由本文 SHA256 清单保护。
- 当前 implementation 比较 ref：`88fa1a1bcbc6f7368d93d97c6e723ea056e6cfbc`。
- preservation 复核开始时 implementation HEAD：`8543bcd82dd311b045ee66cce0e84e1aa6c06e88`。
- Task 13 ownership 纠偏前回滚 ref：`edf1ca72472f581c83e8d5a200ce8c1a8a12fd1c`。
- 消费者临时验证的回滚是移除 `YM_DATA_PIPELINE_PATH` / 保持 `YM_DATA_API_MODE=legacy`；不需要 Git 变更。
- 未来切换后的回滚目标必须包含已验证的 `0e802995` preservation ref 和本文两个 `.pyc` 的外部哈希证据，而不能只依赖未提交工作树或 `f246fef`。

仍需用户明确授权的边界：任何 canonical staging/commit/branch switch、创建或删除 integration worktree、把 unified implementation 集成到正式路径、部署、push、Task 14。本文 follow-up 仅只读复核已存在的 preservation ref，并更新 implementation 内的审计文档与静态门禁；未执行上述动作。
