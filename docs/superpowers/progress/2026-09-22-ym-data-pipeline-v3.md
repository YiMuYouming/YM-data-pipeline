# YM-data-pipeline V3.0 接续记录

> 本文以下各 Task 状态与“本轮不执行部署”是发布前的历史记录。2026-09-23 Hermes 生产发布、实际回读及未关闭缺口，以 [生产发布与接续记录](../../releases/2026-09-23-v3-production.md) 为准；不得再用本页旧状态断言生产未同步或整体完成。

更新时间：2026-09-23（Asia/Shanghai）

## 目标与边界

- 唯一公共入口保持为 `from ym_stock_data import query` 和 `./ym-data`。
- StockToday 只有在能力级真实审计证据通过后才能晋级；目录存在或接口可达不能作为主源证据。
- Token 只从 macOS Keychain 读取，不写入仓库、日志、回执或命令参数。
- 本轮不执行部署、服务重启、生产运行数据写入、commit 或 push。
- 保留工作区内全部既有改动，不做 reset、stash 或 clean。

## 当前进度

| 任务 | 状态 | 当前证据 |
| --- | --- | --- |
| Task 1：冻结 StockToday 完整目录 | 已完成并通过双重审查 | 245 个数据方法；`token_info` 独立；inventory logical SHA-256 `eed063e91d066e1180c6f15e7355f886e8d33937e31c5333fcf5fcc709693ff1` |
| Task 2：可恢复全量审计引擎 | 已完成并通过双重审查 | 246 个审计 case；233 个 probe 要求 total；12 个 exact-point probe 允许无 total；217 个 probe 声明可参与主源评估；probe logical SHA-256 `8778ddb7a9ddec43641d3f68a553a415e44c73a7b218e21d95a83d4f40a542a1`；最近全量 521 passed、5 skipped |
| Task 3：证据绑定路由策略 | 已完成并通过独立复审 | 第五轮 full 558 passed、5 skipped；质量复审原始 A/B/C 门禁 7/7，APPROVED，P0/P1/P2 均为 0；packaged policy 仍 `active=false`、reviewed receipt 不存在 |
| Task 4：全局 Skill/CLI | 已完成并通过双重复审 | 第四轮 spec/quality 均 APPROVED，P0/P1/P2 均为 0；global Skill 14 passed，full 572 passed、5 skipped |
| Task 5：消费端清查和迁移 | Task 5A 第二轮完成；迁移未开始 | 第二批 43 条 production bypass / 14 文件，14/14 owner 覆盖；6 条 production_write_transport 保持 fail closed |
| Task 6：StockToday 真实全接口审计与晋级 | 已有现场，等待 runner hotfix 后续续跑 | live receipt 已有效 checkpoint 108/246；从 `idx_factor_pro` 续跑，晋级仍按能力独立决定，不强求全量主源 |
| Task 7：最终验收 | 待开始 | doctor、全量测试、消费者检查、密钥扫描、验收回执、独立复审 |

## WorkBuddy / 腾讯自选股 MCP 初查

- 已定位插件 ID `westock-mcp@workbuddy-connector-plugins-official`，显示名“腾讯自选股”；本地账号状态记录存在。
- 插件 Skill 声明覆盖 A 股、港股、美股行情与选股，也包含自选、提醒和模拟交易写能力。
- `/Users/yimu/.workbuddy/settings.json` 中该插件虽为 disabled，但 WorkBuddy 5.6.2 连接器页对“腾讯自选股”和“通达信”均显示“去对话”，未连接项显示“连接”；运行态因此确认为已连接，具体只读工具仍待 Task 6 实调。
- Task 5/6 只测试只读行情与数据能力；不调用添加/删除自选、提醒、模拟交易或其他写接口。
- 若只读能力通过，它只能作为 YM-data-pipeline provider/fallback 接入，消费者不得直接调用 MCP 绕过统一契约。

## 本机 provider 基线

- `./ym-data doctor --json` 当前列出 26 个 provider；summary 为 `configured_unverified=26`、`ready=0`、`auth_missing=0`、`auth_expired=0`。
- 该结果仅证明注册、依赖或凭据状态，不能证明实时可用。Task 6 已扩展为对运行时注册表中的全部 provider 做真实只读 capability smoke，并与最终 policy 逐项对账。
- 自动主/备路由若没有当前 capability 级证据，V3 必须 fail closed；不能把 `configured_unverified` 当作 ready。

## Task 4 Skill 安装与验证证据

- 先创建 `tests/test_global_skill_v3.py` 并运行 managed `uv` 测试，得到预期 RED：5 个测试、16 个失败；失败项覆盖 Skill 缺失、文档契约、版本和四个入口的软链接状态。
- canonical Skill 为 `/Users/yimu/Projects/YM_Capital/YM-data-pipeline/skills/ym-a-stock-pipeline/SKILL.md`；只暴露 `from ym_stock_data import query` 与仓库根目录 `./ym-data`，并记录 V3 `_meta` provenance/quality/source-gap 语义及 StockToday 目录不等于晋级。
- `README.md`、`docs/agent-contract.md`、`docs/INSTALL.md`、`docs/STOCKTODAY.md` 已统一 canonical checkout、V3 元数据、compatibility wrapper 边界；项目版本为 `3.0.0`，公共结果 `contract_version` 仍为 `1.0`。
- 四个已确认入口均直接解析到 canonical Skill：`/Users/yimu/.codex/skills/ym-a-stock-pipeline`、`/Users/yimu/.agents/skills/ym-a-stock-pipeline`、`/Users/yimu/.claude/skills/ym-a-stock-pipeline`、`/Users/yimu/.workbuddy/skills/ym-a-stock-pipeline`；原对象备份在 `/Users/yimu/.ym-stock-data/backups/skills/20260922T233630+0800/`，清单和原始 symlink 关系记录于该目录的 `manifest.md`。
- `quick_validate.py` 已在无 `PYTHONPATH` 的 managed uv 环境输出 `Skill is valid!`；`pyyaml==6.0.3` 只加入 dev dependency group，并从已有本机 uv cache 离线同步，未联网或写入凭据。
- 首轮 global Skill 契约、Python contract、legacy compatibility、routing 与 V3 policy focused suite 共 58 tests 通过；fresh shell `./ym-data --help` 退出码 0，作为首轮基线保留。

### Task 4 第二轮修复证据（2026-09-23）

- 第二轮 RED：扩展后的 global Skill suite 共 11 个测试、20 个失败子断言；失败覆盖 tilde/缩进/inline Markdown 绕过、两个漏列 intent、StockToday 旁路文档、active Runbook 旧路径、WorkBuddy 绝对备份链接和 dev dependency 缺失。
- 第二轮 GREEN：global Skill suite 11 passed；全文扫描不再依赖 fenced block，mutation 覆盖 ```、~~~、缩进块和 inline；intent 断言动态读取 `ym_stock_data.routing.all_route_specs()` 的 12 个唯一 intent。
- WorkBuddy backup 已变为 `/Users/yimu/.ym-stock-data/backups/skills/20260922T233630+0800/workbuddy -> agents`，解析到同一 backup root 下的 `agents/SKILL.md`；manifest 保留原活动绝对 target 和 normalized restore target。当前四个活动入口仍直接解析到 canonical repository Skill，未改其它 Skill。
- `docs/STOCKTODAY.md` 现在只保留 public Python/CLI、质量/晋级边界和历史证据路径；`docs/ACCEPTANCE_RUNBOOK.md` 的 active `pipeline_root` 已切换到 Projects checkout。历史 handoff/2.0/superpowers/audit 材料未批量改写，仅由 active-doc allowlist 排除并在本记录分类。
- `pyproject.toml` 的 `[dependency-groups].dev` 锁定 `pyyaml==6.0.3`；`uv.lock` 已离线校验，managed env 通过 `uv sync --offline` 安装，官方 `quick_validate.py` 无 `PYTHONPATH` 通过。Task 5 未开始，仍待独立质量复审。

### Task 4 第三轮修复证据（2026-09-23）

- 第二轮规格复审为 APPROVED，P0/P1/P2 均为 0；独立复现 569 passed、5 skipped，12 个 runtime intent、四端入口、StockToday 文档、active Runbook 和离线 validator 均通过。
- 第二轮质量复审为 NOT APPROVED：`from ym_stock_data import query, fetch` 与 `from ym_stock_data . v2 import resolve` 均是可执行 Python，却可绕过当前 scanner，定为 P1。
- 第三轮 RED：global Skill suite 为 12 个测试方法、6 个失败项；5 个新增 import mutation 与缺失 backup inventory 按预期失败。
- 第三轮 GREEN：global Skill suite 12 passed；全文扫描保留 ```、~~~、缩进、inline/prose 覆盖，并将根模块 import 收紧为精确 public `from ym_stock_data import query`，拒绝 mixed、spaced dotted、alias、括号、多行括号、分号和反斜杠续行。
- backup test 从 `/Users/yimu/.ym-stock-data/backups/skills` 动态匹配 manifest 的 canonical replacement，并按目录名确定性选择最新有效批次，不固定 `20260922T233630+0800`；当前四个 active canonical symlink 未改动。
- 既有 backup 批次新增 `inventory.json`，`entries_sha256` 为 `6cf2aae0b126424d7fdcd5791562d6bc627692e76cb687c8cd5be2c1d79fb89b`；测试逐项重算 codex/agents/claude 的目录、文件 SHA-256、`workbuddy -> agents` 相对软链并比对。manifest/inventory 明确排除以避免 self-hash 循环；证明范围仅是四个 exact installation targets，不声称证明整个 Skills 树历史未改动。
- 历史文档中的 Documents 路径和 V2/private 示例继续作为历史事实保留，明确排除于 active runtime/doc gate；不批量改写历史材料。
- 第三轮全套验证：focused contract/routing/docs/CLI 69 passed；full unittest 570 passed、5 skipped；official `quick_validate.py` 无 `PYTHONPATH`、managed uv offline 通过；fresh `./ym-data --help`、compileall、`uv lock --offline --check`、`git diff --check`、secret scan 和四端 symlink/inventory 重算均通过。未联网、未读 Keychain、未调用 live provider、未写生产、未部署/重启、未 commit/push。

### Task 4 第四轮修复证据（2026-09-23）

- 第三轮质量复审确认 mixed、spaced dotted、alias、括号、多行、分号和续行导入均已拦截，inventory 也可独立重算；但 `importlib.import_module("ym_stock_data" + ".v2")` 与 `__import__("ym_stock_data" + ".sources")` 仍是合法 Python 且未命中 scanner，继续定为 P1。
- 第四轮 RED：global Skill suite 为 14 个测试、7 个失败项；6 个动态 import/执行 mutation 与 selector 临时目录反例按预期暴露。
- 第四轮 GREEN：global Skill suite 14 passed；保留精确 public import allowlist，并对 Agent-facing Skill 文本保守拒绝 `importlib`、`import_module`、`__import__`、`exec`、`eval`、`compile`、`__builtins__` 等动态旁路。该 gate 是静态安全边界，不承诺抵御任意恶意混淆；canonical Skill 本身继续通过。
- `_discover_backup_root()` 现在只接受真实目录和 `YYYYMMDDTHHMMSS±HHMM` 名称；manifest `Created` 的 aware ISO 时间必须规范化等于目录名，manifest/inventory 的 canonical replacement 必须等于当前 canonical Skill，inventory scope 与四个 exact install targets 必须完整一致；无有效候选时 fail closed，多候选按 aware datetime 选择最新。
- tempfile 测试覆盖 symlink candidate、裸目录、Created/name mismatch、manifest mismatch、inventory mismatch，以及两个不同 offset 的有效批次；当前四端 active canonical symlink、既有 backup 实体和 inventory hash 未改动。历史文档仍是 historical classification，不属于 active runtime/doc gate。
- 第四轮全套验证：focused contract/routing/docs/CLI 71 passed；full unittest 572 passed、5 skipped；official `quick_validate.py` 在 managed uv offline、无 `PYTHONPATH` 下通过；fresh `./ym-data --help`、compileall、`uv lock --offline --check`、`git diff --check`、secret scan、四端 symlink/inventory 重算均通过。未联网、未读 Keychain、未调用 live provider、未写生产、未部署/重启、未 commit/push。
- 第四轮最终双重复审：spec 与 quality 均为 APPROVED，P0/P1/P2 均为 0。独立探针允许 3 个 public 形式、拒绝 13 个静态/动态旁路；selector 拒绝 10 类非法候选并按 aware datetime 正确选择最新有效批次。Task 4 正式关闭。

### Task 5A 确定性消费端 inventory 冻结（2026-09-23）

- 八个仓库逐一完成 `pwd`/`realpath`、AGENTS/README 边界和 dirty worktree 记录；`live-trading` 没有 `AGENTS.md`，按 `README.md` 作为项目边界；没有修改八个消费者仓库。
- TDD 证据：先运行 `tests/test_consumer_inventory_v3.py` 得到缺失扫描器的 RED，再完成 stdlib-only 扫描器；扫描不执行消费者脚本，不联网、不读 Keychain、不访问 provider/StockToday。
- 冻结文件：`config/data-consumers.v3.json`，覆盖 8 个仓库和 32 条已分类 consumer/entry 记录；未知或未分类 migration status 会被 schema validator 拒绝。
- 真实审计：`outputs/consumer-audit/20260923T014826+0800/`；7,978 个 file entries，71 个显式工具/依赖排除，26,584 条 findings；production bypass 431、approved compatibility 49、test 4,507、archive 1,970、generated 18,449、documentation 1,178、unknown 0、ambiguous 0。
- 结构化验收：manifest `file_set_sha256=9cc84adaeb92d550946a3a4343228c417f0c2c1f9c089911847239953bd6cce1` 已重算一致，findings logical SHA-256 为 `290d4ae811ceb2193982edd6684be56a86d774a5873804b923673f0e0db96cc7`；findings/summary 落盘一致；没有 source excerpt、URL query、凭据或原始 fallback 文本；fixture 覆盖 `.gitignore` 不生效、语言分类、archive/generated/test/fixture 和外部生产 symlink fail-closed。
- Task5A 验证：全量 `uv run python -m unittest discover -s tests -q` 为 576 passed、5 skipped；`uv run python -m compileall -q ym_stock_data scripts tests`、`git diff --check` 和实现/config/审计输出范围的 secret scan 通过。测试断言中的 `api_key=` 只用于验证脱敏，未计入凭据扫描。
- JEV/TypeSafe 未启动：当前没有 ambiguous finding；Task 5 migration、八仓库生产代码修改、消费者执行、真实 provider/StockToday audit、部署/重启/commit/push 均未开始。

### Task 5A 首版根审查拒绝（2026-09-23）

- 首版 `unknown=0` 不能证明清单完整：431 条 `production_bypass` 分布在 67 个文件，而 32 条 consumer records 只精确覆盖其中 19 个生产文件，仍有 48 个文件没有迁移 owner。
- 首版把 dependency manifest 中的 `httpx`、前端读取本项目 API 的 `fetch`、UI 文案里的 provider 名称、`.superpowers` brainstorm、`skills-lock.json` 和 `compiled/` 生成物误算成 production bypass；同时泛化内部 API 规则可能把 POST/8088 写调用错误批准为 compatibility。
- 第二轮必须把执行性旁路、普通引用、依赖元数据、内部只读 transport、生成/文档材料和写调用分开；每个真实 production bypass 文件必须反向映射到一个明确 consumer record，未覆盖时 CLI 与测试 fail closed。
- 首版输出保留为历史审计证据，不覆盖、不作为迁移基线。JEV 继续不启动，直到确定性分类全集真实收敛。

### Task 5A 第二轮修复完成（2026-09-23）

- detection 与 classification 已拆开：provider token/UI 标签为 production_reference，依赖清单为 dependency_metadata，.superpowers/brainstorm 为 documentation，compiled/ 为 generated；保留 board_history.py 为 production。
- 泛化 HTTP 客户端不再自动成为 bypass；内部 GET 为 approved_internal_transport，POST/PUT/PATCH/DELETE 或 8088 写形态为 production_write_transport 并 fail closed。multiline vendor request 使用有限上下文识别。
- compatibility allowlist 已改为 repo + relative path + match_kind + purpose；同名路径跨仓库不会被放行。外部 production symlink、oversized、read error 继续输出 unknown。
- config/data-consumers.v3.json 已扩至 39 条记录并新增 finding_files；真实 production bypass 的 14 个文件全部唯一 owner，covered_production_files=14、unclassified_production_files=0。
- 第二批审计：outputs/consumer-audit/20260923T023403+0800/，7,978 个 file entries、71 个显式排除、23,282 条 findings；production bypass 43、production reference 355、dependency metadata 9、approved internal transport 30、production write transport 6、approved compatibility 9、test 4,308、archive 1,899、generated 15,554、documentation 1,069、unknown 0、ambiguous 0。
- manifest file_set_sha256=0804ce88ea659a26b6e986836f800038915e82d6a79ea9fcd61b09506475cfea；findings logical SHA-256 为 627f732f023a31c74b1962cda71bca5bc0b85e509652ba929dc49bd76c379599；配置与覆盖重算通过，secret scan 通过。
- TDD/验证：新增边界 fixture、owner gate、fail-closed exit、allowlist/mutation 测试；全量 unittest 为 581 passed、5 skipped。CLI 返回 1 仅因 6 条 production_write_transport，不能将其自动批准为 compatibility；Task 5 迁移、消费者执行、JEV/Task 6 仍未开始。

### Task 4 首轮复审遗留项（历史记录，已在后续轮次处理）

- `tests/test_global_skill_v3.py` 必须扫描 Skill 全文并覆盖 Markdown ```、`~~~`、缩进块、inline/prose 变体，不能只检查行首反引号 fenced blocks。
- Skill intent 映射必须动态覆盖 `all_route_specs()` 的全部 canonical intent；当前漏列 `market_limit_state` 与 `stock_event`。
- WorkBuddy 备份必须自包含；原绝对链接目标作为历史元数据保留，但 backup 内恢复对象不得依赖已替换的活动 `.agents` 路径。
- `docs/STOCKTODAY.md` 只保留 canonical Python/CLI 和历史审计位置，不给 Agent 提供 ClawHub、vendor endpoint、原生 Skill/runtime、native client 或 secure-store 标识等旁路。
- 活动 `docs/ACCEPTANCE_RUNBOOK.md` 必须使用 Projects checkout；历史 handoff/2.0/superpowers 文件保留事实并明确排除于 active path gate。
- official `quick_validate.py` 必须在默认 managed uv dev 环境可复现；不得继续依赖临时外部 `PYTHONPATH`，进度记录要如实写明依赖与命令。

## Task 3 当前必须关闭的问题

1. 活跃策略必须保留 `review_sentiment(query=...)` 的问财语义路由。
2. canonical policy 必须由代码内逻辑哈希锚定；receipt 自身哈希只证明自洽，不能单独授权主源晋级。
3. 每个 capability 需要 provider allowlist，且 `max_age_sec` 不得比 V2 基线更宽松。
4. StockToday 的 evidence 必须精确绑定实际运行 method、周期、字段和 filter；日线证据不能晋级周线/月线。
5. receipt 与 case 时间必须是带时区 ISO-8601，并满足区间包含关系。
6. compiled policy 缓存必须线程安全、支持原子替换后重载、过期即失效，且不能混用 policy/receipt。
7. 审计回执写入必须防止中间目录 symlink TOCTOU。
8. 必须使用真实 `StockTodayProvider` fake transport 测试活跃路由，不能只靠 FakeProvider。

### 第二轮复审新增缺口

9. Cache identity 必须绑定 policy、receipt、inventory 与 probe manifest 的实际内容；inventory/probe 漂移或同 inode/size/mtime 内容变化必须立即 fail closed。
10. `stock_snapshot` 自动 route/allowlist 不得包含实际返回 `PROVIDER_ADAPTER_MISSING` 的 `sina`；需要逐 provider/intent dispatch 一致性测试。
11. receipt 的 duplicate/pagination 子对象必须严格要求完整字段、字段类型与声明值一致；缺失、unknown 或 not_checked 不能保留 `primary_eligible=true`。
12. Policy evidence 必须把实际 receipt columns 与 canonical probe 的全部 `expected_fields` 对齐；只满足 capability 最低列不足以晋级。
13. Probe 声明的每个 filter check 必须在 receipt 中存在对应且通过的实际检查；空、缺失、unknown 或失败均不能 primary-eligible/active。

### 第三轮实现状态

- Cache key 已绑定 policy、receipt、inventory、probe manifest 的内容逻辑哈希；同 inode/size/mtime 内容变化、原子替换、畸形文件与 inventory/probe 漂移均应失效并 fail closed。
- `sina` 已从 `stock_snapshot` allowlist 与 packaged route 删除，仅保留其已实现的分钟线能力。
- duplicate、pagination、filter 子对象已改为严格完整 schema；缺失、未知、`not_checked`、类型错误或声明不一致均不得晋级。
- receipt columns 必须覆盖 canonical probe 的全部 `expected_fields`，并逐项绑定 probe 声明的 filter checks。
- packaged policy 仍为 `active=false`，正式 reviewed receipt 仍不存在；真实审计和独立复审完成前不会自动晋级。

### Task 3 第四轮修复证据（历史记录）

- canonical `stock_snapshot` route 已移除 Sina；`tdx_quotes` 从第四源调整为第三源；Sina 仍只保留在分钟 `stock_kline` route。
- 新增表驱动、无网络 route/provider intent 一致性测试：实际调用已 patch 的 PyTDX/Tencent local dispatch，直接核对 TDX `tdx_quotes` intent contract，并确认 `LocalProvider("sina").call("stock_snapshot", ...)` 为 `PROVIDER_ADAPTER_MISSING`。
- README 与 TDX registry 测试已同步，不再把 Sina 写成 snapshot fallback；历史计划文档保留为历史记录。
- 第四轮验证：focused 118 passed；full 556 passed、5 skipped；compileall 与 `git diff --check` 通过。packaged policy 仍 `active=false`，reviewed receipt 仍不存在。

### 第五轮修复证据

- 已关闭 duplicate key 声明越界：receipt 与 primary gate 同时要求 key fields 属于实际 `columns`、`expected_fields` 和 canonical probe declaration；虚构字段即使把 `primary_eligible` 改为 `false` 并重签名，也会被 receipt validation 直接拒绝。
- 已关闭 pagination declaration 宽松放行：统一要求五个字段、严格字段值/类型和 fail-closed optional policy；空、缺失、未知字段、错误类型、policy drift 与 canonical probe 不一致均在 receipt validation 直接拒绝。
- 新增第五轮 RED→GREEN 反例覆盖上述两类篡改；第四轮 route、Sina minute route、cache/evidence/filter 行为保持不变。focused 85 passed；full 558 passed、5 skipped；compileall 与 `git diff --check` 通过。
- packaged 边界保持 observation-only：`stock_snapshot` route 为 `pytdx -> tencent -> tdx_quotes`，分钟 route 保留 Sina；Sina snapshot 返回 `dependency_missing / PROVIDER_ADAPTER_MISSING`；packaged policy `active=false`、compiled status `inactive`，reviewed receipt 不存在。
- 第五轮最终独立复审：bogus duplicate key 在 `primary_eligible=true/false` 两种重签场景均拒绝；pagination 的空、缺失、错类型、额外字段和策略漂移均拒绝；canonical probe 与 route 未放宽。7 passed，APPROVED，P0/P1/P2 为 0。

## JEV / TypeSafe 评估

- 可用于旁路语义审计：消费端用途分类、接口描述与返回字段的语义一致性、异常案例聚类、人工复核优先级。
- 不参与事实判定：哈希、时间、字段、重复键、分页、时效、provider 选择、晋级和降级结果全部由代码判断。
- JEV 输出必须与 `score`、`confidence`、数据质量和证据覆盖分开保存；低置信或服务失败时保留全部原始候选，不能缩小审计范围。
- 已核对当前官方文档：使用 `POST https://api.typesafe.ai/v1/systemone`，一次请求可承载多个独立 typed question；本机 Keychain 服务 `typesafe-api` 已存在。
- 决定纳入 Task 5/6，作为自过期、可关闭、失败时 no-loss 的 sidecar；主审计、晋级和迁移状态仍由确定性代码控制。

## 恢复步骤

1. 读取本文件和 `docs/superpowers/plans/2026-09-22-ym-data-pipeline-v3.md`。
2. 检查实现任务 `01a0c879-74f5-75b2-839e-c9e39ba46705` 的最新状态与本轮验证证据。
3. Task 4 已通过双审；开始 Task 5，先读取八个消费仓库各自 `AGENTS.md`，只做确定性 inventory/分类，再逐仓库迁移。
4. 每个消费者迁移前先冻结 current source、business shape、freshness、rollback 和 canonical intent；逐仓库验证，不触碰生产运行数据或交易授权。

当前双重复审任务：spec `01a0c897-49a3-75d2-87af-c71eb09a355c`；quality `01a0c897-a01a-7622-bd6d-d35bc8c3e8b9`。实现任务：`01a0c879-74f5-75b2-839e-c9e39ba46705`。Task 4 已关闭，下一步为 Task 5 消费端清查和逐仓库迁移。

## 禁止把以下状态误读为完成

- 单元测试通过不代表真实 StockToday 全接口通过。

## 2026-09-23 Task5A third-round continuation

- Root-review P0/P1 fixes landed only in YM-data-pipeline scanner/tests/config/output; no consumer repository was modified, and no deploy/restart/commit/push occurred.
- Scanner now performs syntax-aware Python import detection, preserves executable dynamic Python strings, and performs deterministic same-file URL dataflow. `Market_Watch/scripts/local_query_specs.py` is a proven `production_bypass` migration file via `THS_REASON_URL -> f-string -> urllib Request/urlopen` helper; unresolved same-file URL/transport paths remain `ambiguous` and fail closed.
- Weak directories no longer hide executable `.py/.js/.ts/.sh` source; strict vendor host matching rejects evil suffixes. Production bypass coverage is `14/14`, unknown is `0`, orphan owner claims `0`, missing entry points `0`.
- Write transports remain six findings in three files, all three owners are `protected_write_boundary`; `write_transport_files=3`, `protected=3`, `unprotected=0`. Their policy is explicitly not market-data migration and do not modify write semantics; they are excluded from the Task5 migration list.
- Agent tooling inventory is explicit in config, manifest, summary and `file_set_sha256`: five files under `.agents/skills`, including Wind specialized/out-of-band channels and the canonical-query research flow; `.agents/memory`, download/cache-like paths are not scanned. Current channel SHA-256: `232c92bad862c8f2783d8da08989f7c0648f641e973925274ca6cbca31008472`.
- New audit bundle: `outputs/consumer-audit/20260923T033948+0800/`; `manifest.json` SHA-256 `2958503f2a746c9fded33babdb45256d18cfbc38e5969bfc4a6bf1aec16ea037`, `findings.json` SHA-256 `504ef3f6b304a776252254b71cd7dfce5ca3ef2f4954886f76b7d72801da1d34`, `summary.json` SHA-256 `5d4b0c362dc3481adaf221c424a9cb9e7e8b358a0b7182e9a71f00ec2cd91e90`; manifest file-set SHA-256 `0e3e37f80b752c5d45768c5633ec936777cd8b544f9f6bc88b8b17d9992818fd`.
- Performance evidence: the original AST implementation ran about five minutes at 98% CPU and was stopped; the repaired focused consumer suite is `11 passed` in `59.885s` wall time (`58.24s` user), with file-content and repository stat-signature caches. The real eight-repository scan took about `46.9s` in the focused run; output generation completed with `ambiguous_findings=26`, so the CLI remains intentionally fail-closed rather than suppressing human review.
- Task6 current field checkpoint is recorded above and remains untouched; the next implementation after this Task5A close is the queued receipt-validator hotfix, not a rerun of completed StockToday cases.

## 2026-09-23 StockToday receipt-validator hotfix

- Root cause fixed in `ym_stock_data/stocktoday_audit.py`: canonical `schema_error`/`provider_code=INVALID_RESPONSE` cases may have declared duplicate keys absent from returned `columns`, but only when the declaration remains canonical/subset of `expected_fields` and duplicate checks are `not_applicable`.
- Verifiable `pass_nonempty`, `semantic_fail`, and `reachable_empty` cases still require every declared duplicate key in actual columns; bogus-key/red-team gates remain strict.
- TDD evidence: the real-shape `idx_factor_pro` regression was RED at validator line 1551, then GREEN. Focused StockToday suite: `50 passed` in `28.802s`; project full suite: `585 passed, 5 skipped` in `100.617s`.
- The live receipt `outputs/stocktoday-audit/20260923T025829+0800/receipt.json` was not opened, rewritten, or re-run; the valid `108/246` checkpoint remains the resume source. No token, response body/line, or Keychain metadata was logged.
- System-Python full-suite attempt is not acceptance evidence: it used macOS Python 3.9.6 and failed environment imports plus a Python>=3.10 wheel smoke case. Acceptance uses the locked `uv run python` environment.

## 2026-09-23 receipt-validator root-acceptance follow-up

- Independent red-team found and reproduced a forged success-class bypass: `provider_code="INVALID_RESPONSE"` combined with missing duplicate columns, `duplicate_key_checks=not_applicable`, and `primary_eligible=false` could enter the prior OR-based schema exception.
- Fixed only in `ym_stock_data/stocktoday_audit.py` and `tests/test_stocktoday_audit.py`: `schema_unverifiable` is now true only for `classification == "schema_error"`. `pass_nonempty`, `semantic_fail`, and `reachable_empty` remain strict even with provider code `INVALID_RESPONSE`; canonical `idx_factor_pro` schema error with provider code `0` remains accepted.
- Live receipt remains untouched and no new audit cases were run; focused and full validation are required after this follow-up.
- Validation complete: StockToday focused `50 passed` in `28.059s`; project full suite `585 passed, 5 skipped` in `102.181s`; compileall, diff check and targeted secret scan passed. Receipt mtime/size remains `Sep 23 03:01:15 2026 / 255284 bytes`.

## 2026-09-23 continuity checkpoint

- Task6 live receipt：`outputs/stocktoday-audit/20260923T025829+0800/receipt.json`；已有有效 checkpoint `108/246`，不得重打已完成 case。
- 当前 counts：`semantic_fail=77`、`schema_error=28`、`provider_error=3`，其余为 0；provider error cases 为 `dc_concept`、`dc_concept_cons`、`fund_div`，均 `provider_code=1`。
- 第一个未完成 case：`idx_factor_pro`。runner 停止原因为 `validate_receipt` 将 schema_error 的声明 `duplicate_key_fields` 与空 columns 冲突；后续 hotfix 只放宽 schema_error/INVALID_RESPONSE 的 duplicate declaration 校验，不能把 receipt 改成假结果。
- 只读 pagination 探针：daily 同参数 `limit=13` 的 `offset=0` 与 `offset=13` 均返回相同 13 行，`total=None`；offset 未生效，非空不能当作完整覆盖。
- 本记录不包含 token、响应正文/行或 Keychain 元数据。
- HTTP 200 或非空响应不代表语义、字段、时效、过滤和分页正确。
- JEV 高分不代表接口可晋级，也不授予交易或生产切换权限。
- Task 1/2 完成不代表消费者已经迁移或生产已经切换。

## 2026-09-23 Task5A fourth-round scanner and receipt consistency

- 本轮仍只修改 YM-data-pipeline scanner/tests/config/progress 与 StockToday receipt validator/tests；没有修改八个消费者仓库，没有 migrate、deploy、restart、commit、push，也没有启动 JEV。
- Repository scan cache 已绑定 inode、ctime_ns、size、mtime、mode 和 regular-file content SHA-256；同大小/恢复 mtime 的 clean-to-`import pytdx` 替换会重新扫描。repository cache key 与 clue cache key 已分离，避免 clue key 覆盖 repository cache identity。
- `os.scandir`/list/stat/read/decode/readlink 失败均写入 repository manifest 或 unknown finding 并 fail closed；agent channel 的 symlink、scan/stat/read error 不再 `continue` 吞掉。`.agents/memory/cache/download` 和同类目录只有在 manifest 记录 repository、channel、path、reason、evidence 后才可排除。
- Python AST 现在覆盖 `importlib.import_module`、`__import__`、provider import alias、private `ym_stock_data` alias access；字符串 prose 不触发，public `from ym_stock_data import query` 不触发。内部 HTTP 的 method/target 可跨任意行、变量和同文件 helper 追踪；已区分 `production_write_transport`、`approved_internal_transport`，method/target 未知保持 `ambiguous`。
- `config/data-consumers.v3.json` 新增真实 `.claude/skills/wind-mcp-skill` symlink human-review channel，并把 `live-dashboard/scripts/end_to_end_verify.py` 的内部 POST test bridge 明确登记为 `protected_write_boundary`；最终为 6 个 agent channel、4 个 write transport，4 个均受保护，unprotected 为 0。
- Receipt validator 新增 classification/provider_code consistency table：`pass_nonempty`、`reachable_empty`、`semantic_fail` 必须为整数 0；`schema_error+0` 合法；auth/rate 接受实际生成器保留的非零上游/HTTP 整数或对应错误字符串；timeout、invalid_params、not_run 和 provider_error 按生成器的安全码/类型 fail closed。新增 `api_key`、`private_key`、`auth`、access-token 等敏感短键拒绝，同时保留 `auth_status`、`auth_required` 等合法 provider metadata。
- Consumer bundle 输出仅允许写入 `outputs/consumer-audit/`；中间目录、目标和临时目标 symlink 均拒绝。落盘 manifest/findings/summary 将机器绝对路径转换为 repository-relative URI，并绑定 scanner/config/test source SHA-256；file-set SHA-256 在脱敏后的字段集合上重算。
- TDD/验证：scanner focused `14 passed`，StockToday focused `53 passed`，full `uv run python -m unittest discover -s tests -q` 为 `591 passed, 5 skipped`；py_compile、compileall、两次 bundle 独立复核、source/config/test binding、file-set 重算、路径/secret scan 均通过。
- 唯一新 bundle：`outputs/consumer-audit/20260923T044207+0800/`。最终 hashes：`manifest.json=6c84b356cfdd79f5675d2344993c98fc43d3d7e1dd72bb78993137a7291124c6`、`findings.json=abc53c3c08df8bee29cbf72ffe6e2f600da2b61afa18e301e4205a6ab4d61cbb`、`summary.json=7c9f8ae92ddf73194cd221370d927b58be7533ca5f142e19b3f9c50612265a60`；manifest `file_set_sha256=ca7e42db571a3b1c9f9d45829564c191c871a48ede56c4d01f9e80d9b0d3f7d6`，agent channel SHA-256=`9f10b73dfdffdddabbbf91298624d10a32268b255a8a2fdf6d3900ece143dae3`。
- Bundle summary：`total_findings=31847`、`production_bypass_files=14`、`covered=14`、`production_bypass=35`、`production_reference=482`、`approved_internal_transport=30`、`production_write_transport=7`、`write_transport_files=4`、`protected=4`、`unknown=1`、`ambiguous=1014`、orphan/missing/unclassified/unprotected 均 0。CLI 预期返回 1，原因是 agent symlink review 与 ambiguous findings，未将其自动批准或隐藏。
- 本轮只读复核了既有 live receipt，没有重写或重跑：`outputs/stocktoday-audit/20260923T025829+0800/receipt.json`，`246/246`；`file_sha256=0c350fdb46c29da8bfc76454d767432d014ef14c568d585c334a2649c2fbd336`、`receipt_sha256=24d516eac5a2367774f96ee67af63d1a54f946901bec81e822752de91678c4af`。counts 为 `pass_nonempty=5`、`reachable_empty=2`、`semantic_fail=126`、`schema_error=67`、`provider_error=44`、`timeout=2`，其余为 0；`primary_eligible=0`，pass methods 仅 `rt_etf_tick`、`rt_hk_k`、`rt_idx_k`、`rt_idx_tick`、`rt_sw_k`；`token_info` 为 provider_error，pagination pass 为 0。StockToday 不做 overall promotion。

## 2026-09-23 Task5A fourth-round root-acceptance correction

- 根验收拒绝第四轮首 bundle `outputs/consumer-audit/20260923T044207+0800/`：`Market_Watch/scripts/jev_shadow_rerank.py` 的 `_number(...dict.get...)`、本地普通 helper 调用和非 HTTP 对象 `.request(...)` 被错误扩大为 `ambiguous_internal_transport`，其中 1,004 条 ambiguous 不能进入 JEV/复审。首 bundle 保留为历史证据，不覆盖。
- TDD RED：新增真实 `jev_shadow_rerank.py` 指定 8 行回归、普通 `dict.get`/本地对象 `.request` 反例，以及跨函数内部 POST/未知 HTTP helper fixture；旧实现按预期失败。
- GREEN 修复：`helper_specs` 只为 `_function_transport_params` 已证明参数抵达 HTTP transport 的 helper 建立跨函数传播；普通 helper 不再构造 unknown method/target。`_is_http_transport_call` 不再把任意尾部 `.request` 当成 HTTP，仅保留 bare/已知 HTTP namespace，并单独保留 `urllib.request.Request` 构造器及其 target/method dataflow；远距离内部 POST 仍为 `production_write_transport`，未知 HTTP helper 仍为 `ambiguous`。
- 修复后 scanner focused 为 `17 passed`；StockToday focused `53 passed`；项目全量 `594 passed, 5 skipped`。py_compile、compileall、`git diff --check`、bundle source binding/file-set/绝对路径与 symlink 复核均通过；未启动 JEV，未改八个消费者仓库，未 migrate/deploy/restart/commit/push。
- 唯一新 bundle：`outputs/consumer-audit/20260923T050327+0800/`；`044207` 历史 manifest/findings/summary 哈希仍分别为 `6c84b356cfdd79f5675d2344993c98fc43d3d7e1dd72bb78993137a7291124c6`、`abc53c3c08df8bee29cbf72ffe6e2f600da2b61afa18e301e4205a6ab4d61cbb`、`7c9f8ae92ddf73194cd221370d927b58be753ca5f142e19b3f9c50612265a60`；新 bundle 三文件哈希分别为 `57dc4b930a9392a2f227ecd89f157cdc19e0c55a420efab4cd9b7a79180137e6`、`217b057f7f2148acc611e42852aac6bb8af2a92c04feff38de7d509da518783d`、`5f6b83830dac2f376e9df504226fbb0905dd7d2a325028f874d7f12472388367`。
- 新 bundle `file_set_sha256=ca7e42db571a3b1c9f9d45829564c191c871a48ede56c4d01f9e80d9b0d3f7d6`，scanner/config/test bindings 为 `cb2e5804af0d892894ca2a6daed1804a36a86348c10285df4b793ac24502a8f4`、`40ed7d165c2f0ebeaa42a433854a34ca8f6ee5e245288ab0fa3046e7d9309b67`、`72b117f798e9bef137b9308b8cd7236cb1fe5abb9545b075e255d152b7a71e6a`。
- 新 bundle summary：`total_findings=23239`、`production_bypass_files=14`、`covered=14`、`production_bypass=35`、`production_reference=483`、`approved_internal_transport=30`、`production_write_transport=7`、`write_transport_files=4`、`protected=4`、`unprotected=0`、`unknown=1`、`ambiguous=37`、orphan/missing/unclassified 均 0。指定 8 行均无 ambiguous；剩余 37 条为真实/未证明的 transport ambiguity，仍 fail closed，不进入 JEV。
- 既有 live StockToday receipt 仍只读验证通过且未重写/重跑：`246/246`，`file_sha256=0c350fdb46c29da8bfc76454d767432d014ef14c568d585c334a2649c2fbd336`、`receipt_sha256=24d516eac5a2367774f96ee67af63d1a54f946901bec81e822752de91678c4af`，`primary_eligible=0`；StockToday 继续不做 overall promotion。

## 2026-09-23 本机渠道 baseline live smoke 交叉证据

- 只读核验既有 receipt：`/Users/yimu/.ym-stock-data/smoke/2026-09-23T051625+0800.json`，SHA-256 `52eb9f492987af82a0b3c8edab887eefba6aba1ed674e6c0c91fd09542b4dad8`，schema `2`，21 个 case，`gate_status=fail`、`chain_status=fail`。receipt 未重写。
- 状态计数：`success=7`、`degraded=4`、`auth_error=6`、`configured_unverified=1`、`empty=1`、`provider_error=1`、`error=1`。source 结论为 `iwencai_openapi=pass`、`wind=pass`、`pywencai=fail`、`tdx=fail`。
- 可用实测：问财 OpenAPI 两个查询成功；Wind enrichment、Wind screener 和 Wind documents 成功；板块指数与涨跌停状态的 canonical case 成功。
- 必须修复/不得晋级：TDX snapshot/screener/kline/report/notice/news 全部 `AUTH_EXPIRED`；PyWenCai 为 `AttributeError/provider_error`；PyTDX screener 仅 `configured_unverified`；canonical TDX fallback 为 `CONTROLLED_ROUTE_DRIFT`；stock event 为空。
- `realtime_market`、`stock_snapshot`、`stock_kline`、`review_sentiment` 的 canonical case 都使用了 `INTERNAL_FALLBACK`，只能记为 degraded，不能证明原定 provider 可用。该 21-case baseline 不是 26-provider 直测矩阵，Task 6A 已交由 Luna Max 实现并实跑，严禁自动改路由。

## 2026-09-23 Task5A 050327 独立双审结论

- Spec reviewer 对 scanner 重算、bundle 三件套一致性、37 条 ambiguity 真实邻接、write boundary、StockToday receipt 和 594+5 全量测试给出 `APPROVED (P0/P1/P2=0)`。
- Quality reviewer 对已证实安全攻击面给出 `P0=0`、无安全 P1，但对尚未闭环的验收状态给出 `REJECTED`：37 条 ambiguity、1 个 `.claude/skills/wind-mcp-skill` symlink unknown、2 个 agent human review 尚存在；另有 standalone receipt validator 未直接绑定 canonical probe hash，以及 `bearer`/`client_id` 短敏感键未拒绝的 P2 hardening。
- 人工只读核对 symlink：`YiMu_IR/.claude/skills/wind-mcp-skill -> ../../.agents/skills/wind-mcp-skill`，解析后仍在同仓库，目标已被库存为 `out_of_band_provider_channel`。待用 expected relative target + target tree/content hash 做确定性 alias 绑定；目标漂移或越界时仍 fail closed。
- 人工只读核对 `portal/.agents/skills/portal-sync-flow/SKILL.md`：SHA-256 `1562af82ffb100f0be1fc16fa4f557e28d96c7122f16ad6e7c865ba8533bb185`，内容是 Portal 发布/验收流程，无第三方市场数据 provider 或直接市场数据 transport；待以内容 hash 和 non-data channel 规则固化后关闭 human review。
- Task5A 当前不得宣告最终通过；Luna Max 已收到修复队列。新 bundle 必须保留旧 050327 不覆盖，并在 JEV no-loss 旁路+人工复核后再发起独立双审。

## 2026-09-23 WorkBuddy 外部渠道清单检查

- 本地 WorkBuddy `mcp-tool-list.json` 只读清单证据确认通达信组 20 个 tools，包含 `tdx_quotes`、`tdx_kline`、`tdx_screener`、`tdx_indicator_select`、`wenda_notice_query`、`wenda_report_query`、`wenda_news_query`；同组的 `tdx_add_favorite`、UI 和 listening 工具不进入数据渠道白名单。
- 腾讯自选股组 81 个 tools，可候选的只读数据工具包含 `data_quote`、`data_kline`、`data_minute`、`data_index`、`data_market_overview`、`data_sector`、`data_trade_calendar`、`data_notice`、`data_news`等。`portfolio_watchlist_*`、`portfolio_paper_trade`、`portfolio_paper_cancel`、`portfolio_tips_set` 等写工具永久排除，不会因连接存在而获得调用权。
- 当前 Mac 锁屏，WorkBuddy UI 真实读调用尚未执行；只能证明工具已连接/可见，不能证明数据就绪。解锁后需对每个候选做小样本读取、时效/字段/过滤/空集校验，与本机 26-provider 矩阵分开存证。

## 2026-09-23 消费端 capability freeze

- 新增架构冻结文档：`docs/superpowers/plans/2026-09-23-v3-capability-freeze.md`。在任何 consumer 改代码前，先补齐全量同花顺二级行业、涨停原因、qfq/hfq K 线、日期区间/分页、指数 K 线和 1m/15m 口径。
- 已确认这些是业务语义缺口，不是单纯 import 替换。未补齐前不得把 consumer 标为 migrated，也不得用另一个未证明数据源静默填空。
- 顺序冻结为 `Market_Watch -> YiMu_IR -> live-trading -> live-dashboard`；每个仓库先 side-by-side shape/provenance/empty/error overwrite guard，不运行真实回填写入。

## 2026-09-23 Task5A deterministic hardening bundle

- StockToday standalone validator hardening完成：receipt 现在必须绑定当前 canonical `stocktoday-probes.v3.json` 的 manifest hash；manifest load/hash drift、旧 hash 或无法建立 canonical probe index 均 fail closed，不再因 hash mismatch 跳过声明对照。receipt safety 新增 `bearer`、`client_id` 短敏感键拒绝，同时保留 `auth_required`、`auth_status`、`auth_source` 等安全认证元数据。
- Agent tooling hardening完成：`YiMu_IR/.claude/skills/wind-mcp-skill -> ../../.agents/skills/wind-mcp-skill` 以 expected relative target 与 target tree/content SHA-256 `3b3195d2ee65c3c1040d0a2d398cbdc62056c9386dc5f6a637236e573836ff14` 确定性绑定；target drift、external/absolute、cycle、missing、nested symlink、tree hash drift 均 fail closed。Portal `portal/.agents/skills/portal-sync-flow/SKILL.md` 以 content SHA-256 `1562af82ffb100f0be1fc16fa4f557e28d96c7122f16ad6e7c865ba8533bb185` 绑定为 `specialized_non_data`，仅保持 Portal 发布流程，不作为市场数据渠道。
- 新 bundle：`outputs/consumer-audit/20260923T054958+0800/`；manifest SHA-256 `37b91e7d9f2922334b3479595b003148bb105dcc99dce3a81c4231bc238baac7`，findings SHA-256 `e2ad0cdd19bb482dfbf21804be1cf14cd27c1d7907aa9b27f6900438b73ae994`，summary SHA-256 `6ad1e6c44eccb769148930df160ec5f10b3aaf534803e8ad4c32b19a7de804dd`；manifest `file_set_sha256=b72bc5a9d37368ac6f74d778132d554f8a5b34cb40b589e18952d900272e6b34`。
- 新 bundle summary：`total_findings=23238`、`production_bypass_files=14`、`covered=14`、`production_bypass=35`、`production_reference=483`、`approved_internal_transport=30`、`production_write_transport=7`、`write_transport_files=4`、`protected=4`、`unprotected=0`、`unknown=0`、`ambiguous=37`、agent human-review/scan-error=0、orphan/missing/unclassified=0。37 条 ambiguity 完整保留，未自动 suppress，CLI 仍 fail closed。
- 最新 bundle source bindings：scanner `6f4405877bbfa31e8ae9c5dfa8eba243794fffab2b0d6b4ef764c5e73174b789`；config `0a4ceca1e1d3f119427917ca74cf9998f309dd69ce7ff453f99faba9e4b08394`；test sources `9790ab7d10185db12a07631519f0e368d6462d8f783065cd2c5c2ebf4eaf8ae7`。旧 `050327` 三文件未覆盖且哈希保持不变。
- 本轮 TDD 新增 manifest-drift、short-sensitive-key、alias target/tree hash、Portal content-hash 回归；consumer focused `19 passed`，StockToday targeted hardening `2 passed`。Reviewed-resolution 尚未落盘：必须等待 Task6A receipt-integrity 修复后运行最终 JEV sidecar，随后按最新 bundle、每条 `excerpt_sha256`、文件 SHA 与 sidecar hash 逐条绑定人工输入；不以路径静默关闭任何 finding。

## 2026-09-23 Task6A provider-smoke V3 initial live evidence (superseded by root review)

- Task6A 在 Task5A 双审等待期间独立完成；未调用 canonical fallback 作为 provider 证据，未修改 consumer scanner、`outputs/consumer-audit/20260923T050327+0800/`、八个消费者仓库或 JEV/TypeSafe。全程仅执行只读 provider adapter 调用，没有 8088 POST、watchlist/trade 或生产写入。
- 新增 `ym_stock_data/v3/provider-smoke.v3.json` 与 `ym_stock_data/provider_smoke_v3.py`：manifest 精确绑定当前 `ym_stock_data.api.PROVIDER_REGISTRY` 的 26 个 provider；每个 registry provider 都有独立 direct call，唯一 `tdx_mcp` 使用 direct provider probe（无 canonical intent 时记录 `configured_unverified`），外部 WorkBuddy Tencent stock MCP 与 external TongdaXin 保留为 `external_pending`，不计入 26。
- TDD：新增 `tests/test_provider_smoke_v3.py`；首轮 RED 为 7 个未实现失败，随后形成首版 receipt；当时 focused `7 passed`、项目全量 `601 passed, 5 skipped`，均为初版 runner 的历史快照，不是当前根审查后的验收数字。
- live receipt：`outputs/provider-audit/20260923T053503+0800/provider-smoke.v3.json`；SHA-256 `9777b89d8622599997e38d6a4307491b4638cc912c97c8b9c6875a268ceac552`。bounds 为 `case_timeout=25s`、`global_timeout=840s`、`case_count=26`、`read_only=true`、`canonical_fallback_used=false`；文件权限 `0600`、时间目录权限 `0700`、无残留临时文件，validator 独立复核通过。
- live source bindings：runner `c6df27b2a69c173f4cff86191699365d4627108d666148a260717f3b8606aca8`；probe manifest `ea6c0875957c49997d8ef854dc7d226c1b07b7fb6a4248b0450826825679bf5d`；registry count `26`，registry binding SHA `76cc07d70ee7223ffcf476b31029307f7c98c9db4bf66359c71d432dc4edc73e`。receipt 只保留 provider/intent/capability/status/error code/type/row count/latency/time/schema-field/freshness/provenance/auth summary，未写入业务行、响应正文、token、cookie、authorization 或绝对路径。
- live case counts：`success=14`、`degraded=1`、`empty=2`、`configured_unverified=1`、`auth_error=6`、`provider_error=2`；`dependency_missing=0`、`rate_limited=0`、`invalid_params=0`、`timeout=0`。success providers 为 `cls`、`cninfo`、`eastmoney`、`eastmoney_breadth`、`eastmoney_limit_pool`、`eastmoney_research`、`iwencai_openapi`、`sina`、`stocktoday`、`tencent`、`ths_industry`、`wind_documents`、`wind_mcp`、`wind_screener`；`pytdx` 明确为 `degraded`（provider-internal `fallback_from=tencent`），`eastmoney_datacenter`/`pytdx_breadth` 为空，`pytdx_screener`/`pywencai` 为 provider error，六个 `tdx_*` 数据 adapter 为 `AUTH_EXPIRED/auth_error`，`tdx_mcp` 为 probe-only `configured_unverified/NO_DIRECT_INTENT`。
- baseline 交叉证据只读保持不变：`/Users/yimu/.ym-stock-data/smoke/2026-09-23T051625+0800.json` SHA-256 `52eb9f492987af82a0b3c8edab887eefba6aba1ed674e6c0c91fd09542b4dad8`，仍为 21-case `gate_status=fail`、`chain_status=fail`；没有用 baseline 覆盖或替换本次 26-provider direct matrix。
- Task6A 初版结论已被 spec/quality root review 重新打开：旧 receipt 只保留历史观察，不作为当前 33-case matrix 的最终 live evidence；根审查修复完成前不授予 provider 晋级、consumer 迁移、生产切换、交易或外部渠道调用权限，JEV/TypeSafe sidecar 仍后置。

## 2026-09-23 provider-smoke CLI accidental fixture cleanup

- 为验证新增正式 `ym-data provider-smoke` CLI 的参数解析，误触发了一次真实 runner：`--case-timeout=0.01`、`--global-timeout=0.02`；该次生成了 33 个 case 且全部为 `timeout`，不是正式 live evidence，也不代表 provider 状态。
- 误创建 receipt：`outputs/provider-audit/20260923T072855+0800/provider-smoke.v3.json`，SHA-256 `d6042a329b08c5c11b4475d571b7630602181267bb39f2a1cd9aa0026af1db16`。确认只由本轮误操作创建后，已删除该单一 timestamp 目录；既有 `20260923T053503+0800` receipt 与其他历史证据未改动。
- 后续 CLI 验证仅使用 parser/private fixture seam，不再调用真实 provider 或 canonical output；该误触发结果不进入 Task6A 正式证据、progress counts 或后续 JEV 输入。

## 2026-09-23 Task6A root-review offline GREEN checkpoint

- provider-smoke V3 当前 case universe 由 `routing.all_route_specs()` 的 31 个 route/provider-intent 组合加上明确登记的 `pytdx_screener` explicit-only 组合确定性校验；manifest 共 33 cases：32 个 direct calls、1 个 `tdx_mcp` probe-only。registry 仍为 26 providers，外部渠道仍只有 2 个 `external_pending`，不计入 case universe。
- runner 已改为每 case 独立 multiprocessing worker；父进程按 case/global budget terminate/kill/join，超时不依赖可被 provider 吞掉的 SIGALRM。fixture 注入 loader/clock 时 receipt 明确 `live=false`，且禁止写 canonical `outputs/provider-audit`；只有默认真实 registry loader、真实 clock 与 canonical output root 才可生成 `live=true`。
- receipt validator 现在绑定 canonical provider/intent/operation/capability/direct-probe entry、effective provider 与 verified internal fallback 关系、success/empty/degraded 与 row/error/schema 一致性、receipt/case/fetched_at/latency 时间区间；拒绝短 bearer、query-style password/token、Windows absolute path、超限 fields/schema，保留安全 auth metadata 白名单。
- source binding 新增 baseline logical path `ym-stock-data/smoke/2026-09-23T051625+0800.json` + SHA-256 `52eb9f492987af82a0b3c8edab887eefba6aba1ed674e6c0c91fd09542b4dad8`、registry/adapter module content hashes，以及 `ym_stock_data/v3/provider-policy.v3.json` logical path + SHA-256 + parsed `active=false`。旧 `20260923T053503+0800` receipt SHA-256 `9777b89d8622599997e38d6a4307491b4638cc912c97c8b9c6875a268ceac552` 未改动。
- 新增正式 `ym-data provider-smoke` CLI 子命令；`pyproject.toml` package-data 纳入 `provider-smoke.v3.json`，wheel smoke 从无 source tree 安装环境加载 runner 与 33-entry manifest 成功。
- 离线验证：provider-smoke focused `15 passed`；wheel 合并 smoke `1 passed`；项目全量 `uv run python -m unittest discover -s tests -q` 为 `612 passed, 5 skipped`；`compileall`、`git diff --check`、manifest/source binding/policy active/path/secret checks 均通过。该段记录的是正式 live 前的 offline checkpoint；随后 live evidence 见下一节，JEV/TypeSafe sidecar 仍未启动。

## 2026-09-23 Task6A root-review final live evidence

- 在两位 reviewer 最终意见全部落地、offline GREEN 后，按 `case_timeout=25s`、`global_timeout=840s` 运行一次完整正式 `provider-smoke`；仍未启动 JEV/TypeSafe、未修改 consumer、未 deploy/restart/commit/push、未对 8088 发 POST。
- 新 receipt：`outputs/provider-audit/20260923T073443+0800/provider-smoke.v3.json`；SHA-256 `704233a1ac5dccab00b4ef65578a87260c05eb790219c7fc31793b3114e3ddae`；`live=true`、`case_count=33`、`read_only=true`、`canonical_fallback_used=false`，文件 `0600`、timestamp 目录 `0700`、无临时文件；validator 独立复核通过。旧 `outputs/provider-audit/20260923T053503+0800/` 保留且 SHA-256 仍为 `9777b89d8622599997e38d6a4307491b4638cc912c97c8b9c6875a268ceac552`。
- live counts：`success=16`、`degraded=4`、`empty=4`、`configured_unverified=1`、`auth_error=6`、`provider_error=2`、`timeout=0`；`dependency_missing=0`、`rate_limited=0`、`invalid_params=0`。33 cases 是 32 direct route/explicit-only combinations + 1 `tdx_mcp` probe-only，不把外部 WorkBuddy/TongdaXin 两个 pending 渠道计入。
- 关键观察：`pytdx` 的 `stock_snapshot`、`realtime_market`、`stock_kline` 均为 `degraded`，且 `effective_provider=tencent`、`fallback_from=pytdx`、`kind=source_internal`、`verified=true`；六个 TDX data adapters 为 `auth_error/AUTH_EXPIRED`；`tdx_mcp` 仍是 `configured_unverified/NO_DIRECT_INTENT`；`stocktoday_data(rt_k)` 与 StockToday `stock_snapshot`、daily `stock_kline` 分 case，不能外推 StockToday overall ready/route eligibility。
- live source bindings：runner `ef05004dd67dd8c1cd91e41f6e6e00620e42dd8849ade1592f8092204311452a`；manifest `404c52e0b1986fae17bc393141a78b1800bdb01faab75491bf4162b1a7edccfe`；registry source `ym_stock_data/api.py` SHA `0c20d66fcd7c3a1550fbeca0446f538f564f1ced1c8256a2b7bcce4c168143c2`；policy `ym_stock_data/v3/provider-policy.v3.json` SHA `d49dbe7ac87f866cbb8139be27b5345c226ef6ae6315f285834cbc15245dcef6` 且 parsed `active=false`；baseline logical path `ym-stock-data/smoke/2026-09-23T051625+0800.json` SHA `52eb9f492987af82a0b3c8edab887eefba6aba1ed674e6c0c91fd09542b4dad8`。adapter module hashes随 receipt 的 `source_binding.adapter_sources` 保存。
- 结论边界：本 receipt 证明的是本次受控 33-case direct adapter 观察和上述明确降级/认证状态，不授予 provider 晋级、canonical route 改写、consumer migration、生产发布、交易或外部渠道调用权限。Task5A reviewed-resolution/JEV 仍后置，等待单独授权。

## 2026-09-23 Task6A close and scope stop

- `073443` receipt 已完成最终只读复核：validator pass、SHA-256 `704233a1ac5dccab00b4ef65578a87260c05eb790219c7fc31793b3114e3ddae`、33 cases、timeout=0、无临时文件，receipt `0600`、时间目录 `0700`。Task6A 到此关闭。
- 本阶段不再追加 receipt framework、全接口推广、StockToday 246-case 全通过门槛或 JEV/reviewed-resolution；下一阶段等待用户另发最小实现任务。
- 已记录的最小路由方向仅作为下一阶段边界：消费者统一走 `query()/ym-data`；StockToday 仅以已实测历史/日 K 作为候选主源；腾讯主供 realtime/stock_snapshot；失败/空集沿现有链显式标记 `degraded` fallback，禁止静默混源。本节未实施这些路由改动。

## 2026-09-23 WorkBuddy 外部只读 live 证据（独立于本地 provider matrix）

- 证据时间：2026-09-23 07:54，盘前；来源为 WorkBuddy 外部 MCP，只读调用，不写入本地 receipt/provider matrix，不执行下单或任何写操作。
- 腾讯自选股 MCP：`data_quote(600519)` 为 `ok`、1 行、快照日期 `2026-09-22`；`data_kline(日K3条)` 为 `ok`、3 行、日期范围 `2026-09-18..22`；`data_market_overview` 为 `ok`、1 个聚合行、19 字段、日期 `2026-09-22`。
- 通达信 MCP：`tdx_quotes(600519,setcode=1)` 为 `success`、1 行、时间 `2026-09-22 15:30`；`tdx_kline(period=4,wantNum=3)` 为 `success`、3 行、日期范围 `2026-09-18..22`。
- 交叉一致字段：收盘 `1253.80`、涨幅 `+0.10%`、昨收 `1252.57`、最高 `1265.88`、最低 `1248.10`、总量 `24573` 手。内外盘拆分与 PE 口径不同，降级时不得把两种来源字段混合成一个事实。
- 边界：该段只证明 WorkBuddy 外部渠道在该时点的只读可读性和字段交叉证据；不并入本地 26/33-provider matrix，不改变本地路由、provider-policy、生产状态或交易授权。

## 2026-09-23 V3 高频 canonical intent 与 StockToday 长尾发现收缩实现

- 本轮只修改 `YM-data-pipeline` 内的 canonical route、StockToday adapter、intent registry、CLI、global Skill、README 和对应回归测试；没有启动新的 audit/provider-smoke/JEV 框架，没有修改消费者仓库、部署、重启、commit/push、交易或 8088 写入。
- 高频短语现在固定映射为：`查涨停板`/`查跌停板` → `market_limit_board(kind=up/down)`；`查同花顺热榜`/`查东财热榜` → `market_hot_rank(source=ths/dc)`；`查板块`/`查行业` → `sector_index(names/codes)`。专用高频短语不再落入无 fallback 的 `stocktoday_data`。
- `market_limit_board` 固定路由为 `StockToday → eastmoney_limit_pool`，支持 `up/down/broken/yesterday`；`yesterday` 在 StockToday 明确返回 `incompatible` 后交由 Eastmoney 投影。无 `date` 时 StockToday 只选择返回行中最大有效 `trade_date`，写入 `data.date` 与 `observation.selected_trade_date`；有行但无有效日期则明确 `SEMANTIC_DATE_MISSING`，不猜日期、不混历史行。
- 既有 `market_limit_state` 未采用 StockToday，因为当前 adapter 未观测 `zb_count`、`yzt_count`、`break_rate` 时会写入零值，可能污染既有复盘聚合契约；已恢复 Eastmoney 唯一聚合源。既有 `sector_index` 保留原 `ths_industry` 唯一路由，避免用 StockToday `ths_index` metadata 替换价格/表现语义。StockToday 只在新且字段语义明确的 `market_limit_board` 上做第一源。
- `market_hot_rank` 只使用语义匹配的 StockToday `ths_hot`/`dc_hot`，无同义 fallback；失败/空集附带 `source_gap=no_semantically_equivalent_hot_rank_fallback`。adapter 不请求 `fields` 子集、不传不稳定的 `is_new=Y`，取完整表后本地按 `trade_date` 选择最新完成日，再应用 `limit`。本地 catalog 仍保留完整方法参数，显式 `stocktoday_data` 不受影响。
- 外部提供的 live 根因对照：同一时段 exact example 带 `fields` 连续触发 `UPSTREAM_ERROR`；不传字段子集可 HTTP 200/upstream_code=0。canonical 只读 live：无 `trade_date` 的 `ths` 为 timeout 15099ms、`dc` 为 `UPSTREAM_ERROR` 10532ms，均保留 source gap；显式 `trade_date=20260922` 且不带字段子集时，`ths_hot` 5 条成功约 1760ms，`dc_hot` 5 条成功约 519ms，均 `provider_used=stocktoday`、`source_tier=primary`、选中日为 `20260922`。
- canonical `market_limit_board(kind=up)` 无日期真实查询：StockToday `UPSTREAM_ERROR` 2294ms；随后 Eastmoney 成功约 4302ms，63 条，最终 `status=degraded`、`source_tier=fallback`，没有混用两源字段。该结果是只读 metadata 摘要，不落盘业务行。
- 新增离线、无凭据、无网络的唯一长尾发现入口：`./ym-data stocktoday catalog [keyword] --json`。精确关键词按方法名/描述/分类/子分类做确定性子串匹配；输出方法名、描述、分类、允许参数、示例和 inventory hash。global Skill 已要求在显式 `stocktoday_data` 前先使用该 catalog，不读取私有 inventory、不自由猜 API。
- TDD/验证：高频路由、provider、registry、public API、Skill、文档和 catalog CLI focused 共 `124 passed`；CLI `./ym-data stocktoday catalog ths_hot --json` 返回 1 个方法、允许参数和安全示例，未触发网络或 provider。此前全量旧 suite 的历史兼容断言仍有 15 failures/17 errors，主要集中在旧 V2 直接 provider 预期和未随新 route universe 更新的 provider-smoke manifest；未以这些旧断言宣称全量通过，也未启动其框架修复。

## 2026-09-23 收缩目标最终链路与消费端接入

- 公共入口保持唯一：Python `from ym_stock_data import query`，CLI `./ym-data`。StockToday 是 `realtime_market`、`stock_snapshot`、日/周/月/分钟 `stock_kline`、`market_limit_board`、`market_hot_rank` 的第一源；`sector_index` 与 `market_limit_state` 保留原有精确语义来源。后备顺序只来自 RouteSpec，失败或空集不由消费端自行找源。
- 中文 registry 可直接执行涨停板、跌停板、同花顺/东财热榜、实时大盘、实时个股、日周月/分钟 K 线、行业/板块。CLI 已将未加内层引号的 `code=600519`、`trade_date=20260922` 等按字符串参数处理，避免数值误解析。
- 全局 Skill 的 Codex、Claude、Agents 与 WorkBuddy 入口仍指向同一 canonical Skill；长尾接口只通过 `./ym-data stocktoday catalog [keyword] --json` 发现，再走 `stocktoday_data`，不读取私有 inventory 或拼接 provider fallback。
- 实际消费端接入：live-dashboard 的实时个股、市场指数、宽度/情绪、涨跌停计数与新闻；live-trading shadow 快照；YiMu_IR 两个研究脚本。逐文件状态与最小验证命令见 `docs/production-consumers-v3.md`。
- 未接入但保留原生产能力：全量同花顺二级行业/880 板块历史、sector inflow、北向、带题材归因的旧 hot_list、指数分钟线与历史回填、前复权 K 线、涨停原因 enrichment。没有等价 shape 时不关闭旧能力、不伪装统一口径。
- 生产运行时修复：public API 不再在 import 阶段强加载可选 TDX MCP 依赖；系统 Python 缺少 `keyring` 时通过 macOS Security framework 读取同一 Keychain 项，并在长生命周期进程内缓存成功 token。没有写入明文 token，也没有改为文件存储。
- 真实业务验收：`查同花顺热榜 trade_date=20260922 limit=2` 为 StockToday primary success 2 条；`查日K code=600519 count=3` 为 StockToday primary success 3 条；`查涨停板` 中 StockToday `UPSTREAM_ERROR` 后 Eastmoney fallback success 63 条，最终 `degraded/fallback`；`查实时个股 codes=[600519]` 为 StockToday primary 返回 1 条并因字段质量标记 `degraded`。
- 生产消费端只读验收：live-dashboard 系统 Python 直接调用 quotes/index 均使用 StockToday primary，单次观测约 0.48s/0.23s；live-trading shadow 返回 1 条且 source 为 StockToday。主源失败的固定降级由上述涨停链及自动化 route/fallback 测试覆盖。
- 最终聚焦回归：管道 121 tests、live-dashboard 28 tests、live-trading 27 tests、Market_Watch 47 tests 通过；YiMu_IR 目标脚本 `py_compile` 通过；相关仓库 `git diff --check` 通过。未启动/重启/部署服务，未写生产数据，未向 8088 发 POST，未 commit/push，未执行下单。

## 2026-09-23 V3 实时长尾日期语义与分页验收

- 补齐 canonical 长尾的 realtime profile：`industry_flow` 使用 `ths_industry → StockToday`，`northbound_flow` 保留 `northbound` 当前分钟序列，`legacy_hot_rank` 使用 `ths_hot → StockToday`，`index_intraday_compare` 使用 `PyTDX index → StockToday`。实时三类资金/热榜请求不自动注入上一完成日；适配器只接受当前交易日或明确的当前会话语义，历史请求仍按显式 `trade_date` 或最近完成日规则处理。
- 旧 `fetch()` 兼容入口已映射到上述 canonical intent，并保留旧业务 shape：北向的 `date/minutes/minute_count`、热榜的 `stocks/reason_stats/zt_count`、行业流入的 `top/bottom` 投影、指数分钟比较的三组指数字段及 `yesterdayAmt`。字段不完整时走质量失败/固定降级，不把 `items/bars` 冒充旧结构。
- PyTDX 股票与指数 1m/5m 历史区间改为内部 800 行分页，按日期边界过滤、去重、排序；StockToday 分钟接口的日期窗口转换为上海交易时段，并在未指定 `count` 时保留完整返回，不再静默截断为 60 行。
- 真实只读验收：股票 5m 单日返回 48 bars、双日返回 96 bars；指数 5m 单日返回 48 bars、双日返回 96 bars；四次均为 `StockToday empty → PyTDX/PyTDX index success`，最终 `status=degraded`，日期范围与请求一致。未落盘业务数据。
- 本轮最终聚焦回归：`test_v3_core_repairs.py`、`test_routing.py`、`test_public_api.py`、`test_provider_stocktoday.py` 共 `102 passed`、`29 subtests passed`；`compileall` 与 `git diff --check` 通过。consumer inventory/audit 辅助测试及 `scripts` 包入口不在本轮范围，未继续修复。
- 保持边界：未修改消费者仓库，未部署、重启、commit、push，未向 8088 发 POST，未执行交易或外部提交。

## 2026-09-23 指数分钟线 canonical source 与消费端收口

- 指数历史 K 线新增独立 EastMoney `push2his` adapter，统一把返回的手转换为股、成交额保留元；`index_kline` 路由收口为 `StockToday → EastMoney index → PyTDX index`。PyTDX 指数分钟线因 `vol` 口径不可验证而 fail-closed，不再进入 canonical 结果；PyTDX 指数日/周/月使用 `*10000` 量级修正。
- `index_intraday_compare/kline_15m` 收口为 `EastMoney index → StockToday`。EastMoney 源边界生成旧消费者需要的三组 `t/chg/vol/volRatio/amount/yesterdayAmt/累计` 形状，StockToday 只有形成同一形状才可降级；不接受 PyTDX 指数分钟量。
- live-dashboard `quotes.py` 与 pipeline `consumer/dashboard.py` 均改为一次调用公共 `query("index_intraday_compare", period="15m", use_case="realtime_poll")`，保留原三组输出键，不再由消费端分别调用三次 `index_kline`。EastMoney 请求增加源边界最多 3 次、短 backoff 和 `Connection: close`，不改变全局 HTTP client 的重试策略。
- 同批完成 K 线单位修正：腾讯日/周/月成交量按手转股但缺失成交额保持 `None` 并触发质量失败；PyTDX 股票日/周/月 `*100`、分钟 `*1`；StockToday 本地日/分钟预算默认均为 `0`，只有显式环境变量才启用本地 ceiling，未臆造购买额度。
- TDD/验证：管道聚焦回归 `110 passed, 29 subtests passed`；provider-smoke manifest/source-binding targeted `3 passed, 4 subtests passed`；live-dashboard quotes collector `27 passed`；两仓库 `compileall` 与 `git diff --check` 通过。EastMoney 真实只读验收（交易日 `20260922`）：公共 `query("index_kline")` 三指数均为 `StockToday empty → eastmoney_index success`、最终 `degraded`，各 `48` 根、`share/CNY`；公共 15m compare 为 `eastmoney_index` primary，三组均有累计行。未部署、重启、commit、push，未向 8088 发 POST，未写生产业务数据。

## 2026-09-23 Sina fallback 与 EastMoney stock qfq 收口

- EastMoney 指数分钟线连续断开/限流时新增独立 `sina_index` fallback，仅承接 `1m/5m/15m/60m`；其源边界直接保留新浪返回的股、元口径。`index_kline` 变为 `StockToday → EastMoney index → Sina index → PyTDX index`，`index_intraday_compare` 变为 `EastMoney index → Sina index → StockToday`；PyTDX 指数分钟仍不可进入 canonical。
- compare builder 抽为共享构建器，EastMoney 与 Sina 使用同一组三指数旧 shape；请求窗口固定为有界 `200` 根，避免新浪 `datalen=2000` 返回空集。EastMoney 的有界 retry/backoff 逻辑也抽为可复用源边界，个股 EastMoney K 线沿用同一策略。
- 新增 `eastmoney_stock`，使用 `fqt=0/1` 严格对应 `adjustment=none/qfq`，日/周/月成交量手转股、成交额保留元；日/周/月路由为 `StockToday → EastMoney stock → Tencent → PyTDX → TDX`。qfq 与 none 不互换，错误 adjustment 会被质量门拒绝。
- 模拟回归覆盖 EastMoney 限流后 Sina fallback、Sina JSONP/单位、EastMoney `fqt`、qfq 主源失败后 EastMoney 成功及 adjustment mismatch。真实只读验收：Sina 三指数 15m 各 `16` 根，共享 compare 三组各 `17` 行（含累计）；模拟 EastMoney `RATE_LIMITED` 后 canonical 由 Sina 成功；600519 同日 none/qfq 均为 EastMoney `share/CNY`，公共 qfq 为 `StockToday invalid → eastmoney_stock success`、最终 `degraded`。
- 本轮验证：管道聚焦 `115 passed, 29 subtests passed`；provider-smoke manifest/source-binding targeted `3 passed, 4 subtests passed`；provider-policy targeted `2 passed, 3 subtests passed`；compileall 与 `git diff --check` 通过。未部署、重启、commit、push，未向 8088 发 POST，未写生产业务数据。

## 2026-09-23 V3 代码验收收口（替代此前阶段性“最终”表述）

> 此处“代码验收完成”结论已被下面的交易时段、指数批量和节假日反例撤回；以文末补验记录为准。

- 统一路由已按用途冻结在 `YM-data-pipeline`：`realtime_poll` 的大盘与个股快照使用 `PyTDX → 腾讯 → 已验证旧源`；Agent、研究查询和历史 K 线优先 StockToday，不能等价适配的研报、公告、新闻和行业专用结构继续由管道选择可靠旧源。消费端只传 intent、参数和 `use_case`，不再自行选择 provider。
- PyTDX 源边界只返回直接 TCP 结果；直连失败返回 `PYTDX_DIRECT_UNAVAILABLE`，不再把内部腾讯结果记作 PyTDX。2026-09-23 盘中只读探针中，`realtime_market(use_case=realtime_poll)` 由 PyTDX direct 返回完整三大指数、`status=success/source_tier=primary`；设置 `YIMU_DISABLE_PYTDX=1` 后，同一查询记录 PyTDX 失败并由腾讯返回完整三指数，结果为 `degraded/fallback`。
- StockToday 本地预算默认 `per_minute=0/per_day=0`，不再设置人为 5,000 次日上限；只有显式环境变量才启用本地 ceiling，供应商 429/额度错误保持为上游真实状态并进入固定降级链。
- K 线契约固定为 `datetime`、OHLC、`volume` 股、`amount` 元和显式 `adjustment`。`none` 与 `qfq` 不互换，指数只接受 `none`；主源返回复权、字段、单位、日期或未收盘 bar 不符时继续尝试等价备用源。盘中真实对照的 StockToday 与 PyTDX 2026-09-22 贵州茅台未复权日 K 的 OHLC 完全一致，二者均为 `share/CNY`，成交量相差 94 股、成交额相差 68 元；StockToday 前复权日 K 真实查询返回 2 根、`qfq/share/CNY`。
- 时效与结构质量门已收口：实时快照缺股票、缺必要字段、行情时间过旧，K 线日期/时间/单位/复权不符，三大指数缺项，以及分钟比较缺任一指数或必要字段，均记录 `quality_failure` 并继续备用源。盘中 15 分钟指数比较观测到 EastMoney 结构不完整后继续由 Sina 返回三组各 9 行，没有把原表或不完整结果记为 success。
- Agent 入口已确定性覆盖涨跌停、同花顺/东财热榜、实时大盘/个股、日周月/分钟 K、前复权日 K、行业/板块；行业资金流、市场资金流、北向、旧热榜、指数历史/分钟线和回填均为 canonical intent。无日期“查同花顺热榜”真实返回 StockToday primary、日期 `20260922`、100 行。其余 235+ 长尾接口只能先查本地 catalog，再走 `stocktoday_data`，不允许 Agent 猜接口或拼接来源。
- 消费端代码收口完成：live-dashboard 行情/大盘/宽度/行业/北向/旧热榜/指数分钟/历史回填，Market Watch 行业与涨停原因，YiMu_IR 前复权 K 线，以及 live-trading shadow 均使用 public `query()`。生产代码搜索不再发现消费者导入 `ym_stock_data.fetch`；仅归档脚本保留历史引用。shadow 保留 `quote_time`、接收 `fetched_at` 和 `quality_status`，盘中真实回读分别为 `11:35:50`、`11:36:10`、`partial`，没有把旧行情重标为当前时间。
- 真实消费回读：dashboard 个股由腾讯降级、大盘由 PyTDX direct、15 分钟比较由 Sina 降级且三者均有业务结果；Market Watch 行业 90 行、2026-09-22 涨停原因 63 个代码；YiMu_IR 前复权 helper 返回 2 行且涨跌幅/涨跌额/振幅/量额完整。一次 YiMu_IR 实时调用遇到上游瞬时失败并 fail closed，随后同一 public 入口重试恢复为 StockToday success；没有返回未复权或伪造数据。
- 代码验收：`uv run python -m unittest discover -s tests -q` 完整执行 675 个测试并通过，5 个按既有条件跳过；旧 V2 测试已改为验证当前 canonical 透传、严格质量和固定降级，没有放宽生产行为。live-dashboard 统一查询回归 31 项、Market Watch 50 项、live-trading shadow 29 项、YiMu_IR 前复权消费回归通过。五个相关仓库 `compileall` 和 `git diff --check` 均通过。
- 运行状态仍单独管理：Projects 与 Documents 指向同一 checkout，但 8088 是既有远端 SSH 隧道、18088 是既有本地代理；本轮没有同步远端、重启、部署、发送 8088 POST、写生产业务数据或接入下单。运行服务尚未切换到本次代码，必须在单独授权后执行并回读验证。

## 2026-09-23 用户反例后的收窄修复与补验

- 撤回此前“整体完成”结论。用户离线复现发现：12:00 查询 11:30 行情被固定 60 秒规则误判过期；`index_kline(codes=...)` 请求三指数只返回一只时质量门误判通过；无日期热榜只跳过周末而不识别休市日。三项均已写成失败回归后最小修复，保留原路由和消费端接入。
- 快照及大盘的来源时间与行情时间改按上交所实际交易时段计龄：09:15–09:25、09:30–11:30、13:00–15:00；午休、收盘后、周末暂停交易时钟，但必须属于最近应有的交易日。例：12:00 的 11:30、17:00 的 15:00、9 月 26 日的 9 月 24 日收盘行情可作为最近时段快照；11:20 午休旧行情、13:02 的 11:30 行情和昨日行情在今日交易时段均拒绝。原始 `quote_time` 与 `fetched_at` 均不改写。
- 批量指数历史按请求代码逐一核对完整性、重复和错配；部分结果记录 `QUALITY_INDEX_INCOMPLETE` 并试下一等价源。所有源均只有部分结果时最终为 `error/provider_used=null`，不把一只指数冒充三只；单指数错码也明确拒绝。
- 管道包内新增 2026 年上交所休市日历，按[上证公告〔2025〕45号](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)核对。无日期热榜、非实时资金查询据此选择最近已完成交易日；9 月 25 日回退至 9 月 24 日，10 月 8 日开盘前回退至 9 月 30 日。日历不覆盖的年份明确 `CALENDAR_UNAVAILABLE`，不会按工作日推断；可显式提供日期。wheel 构建确认日历文件已打包。
- 离线回归：针对三类反例及交易恢复边界的 10 项测试通过；全量管道 `685 tests OK (5 skipped)`，`compileall`、`git diff --check` 通过。消费者相关回归：live-dashboard 37 项、Market Watch 50 项、shadow 29 项、YiMu_IR 前复权 1 项均通过。
- 真实只读查询：盘中快照 PyTDX 行情质量失败后由腾讯返回 `degraded/fallback`，保留原行情时间；三指数 2026-09-22 日线由 StockToday 同时返回 `000001.SH/399001.SZ/399006.SZ`；无日期同花顺热榜由 StockToday 返回 `trade_date=20260922`。这证明当前业务链可读；上述休市日和午休边界由确定性离线回归证明。
- 运行边界不变：远端 8088 服务及 18088 代理尚未同步/重启，未执行 8088 POST、生产写入或下单。2026 年以外日历需在官方年度公告发布后更新；未覆盖年份当前会显式失败，不宣称无限期自动覆盖。
- 补验发现并修正抓取时间与行情时间混用：非交易日新抓取的 `fetched_at` 是当日接收时间，不能要求它等于上一交易日；行情新旧仍以原始 `quote_time` 校验。新增周末新抓取通过、周末过旧抓取拒绝回归；聚焦回归 80 项通过。

## 2026-09-23 StockToday 主源时效补修

- 用户离线复现指出 StockToday 适配器仍用自然时间判 `stale`：12:00 的 11:30 原始行情先被适配器误标过期，统一入口因此记录 `QUALITY_STALE` 并提前降级。真实 StockToday 响应结构的完整路由测试先复现 RED：第一条 attempt 是 `QUALITY_STALE`，未能使用主源。
- 将 A 股实时 observation 与统一质量门改用同一交易时段计龄函数；适配器以自身抓取时钟计算年龄，不改写原始 `updated_at`/`trade_time`、`quote_time` 或 `fetched_at`。A 股实时报价的适配器阈值与 canonical 路由同为 60 秒；分钟数据仍按各自频率预算，港股与期货不套用 A 股交易时段。
- 离线业务回归验证：StockToday 个股与三指数 11:30 行情在 12:00 均保持 `fresh/primary`；个股 15:00 行情在 17:00、9 月 24 日收盘行情在 9 月 26 日仍保持原行情时间并使用主源；13:03 的旧个股报价与 13:01:30 的旧指数报价均记 `QUALITY_STALE` 后由腾讯备用源返回。聚焦回归 121 项通过；最终全量回归 689 项通过、5 项按条件跳过，`compileall` 与 `git diff --check` 通过。
- 仅本地代码补验；没有同步/重启 8088 或 18088，生产运行链路仍未验收，不恢复“整体完成”标记。
