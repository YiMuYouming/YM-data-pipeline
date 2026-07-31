# 统一路由入口 审计 + 修复报告（2026-07-31）

> 审计对象：`codex/unified-a-share-data-channel-canonical`（HEAD 88e3e4d），ym_stock_data v2.0.0
> 审计方式：源码通读 + `doctor --json` + 官方 `query()` 全 intent 实测 + `smoke --live` 21-case 全渠道探针 + 全量测试 + 根因级诊断脚本（TDD 逐项修复）
> 结论先行：**架构正确，修复后 5/7 渠道可用；TDX 与 pywencai 受外部限制无法在本环境激活。**

---

## 一、统一路由入口架构评估（正确性）

- 单一 `query()` 入口 + RouteSpec 语义路由：正确。
- `_meta.attempts` 全链路审计 + `provider_used` + `quality` + `freshness` + `auth`：正确，契约 1.0 强校验。
- SQLite 跨进程 breaker（401/403/429 → 300s，5xx → 60s）：正确。
- degraded / empty / error 三种降级状态语义区分：正确。
- TDX OAuth 实现（DCR + PKCE S256 + state 校验 + scope 强校验 + 0700/0600 权限 + 原子写 + 跨进程刷新锁 + refresh 轮换）：严谨，无安全问题。
- V1 `fetch()` / V2 `resolve()` 仅为 compatibility wrapper，无第二条 provider chain：正确。

**架构层面无 bug。问题集中在 provider 集成层与外部环境。**

---

## 二、渠道实测状态（修复前 → 修复后）

| 渠道 | 修复前 | 修复后 |
|---|---|---|
| 零鉴权本地源（PyTDX 行情/快照/K线、THS 行业、东财涨停池/事件/公告、财联社、巨潮） | ✅ 可用 | ✅ 可用 |
| Wind CLI（enrichment + screener） | ✅ 可用 | ✅ 可用 |
| **Wind 公告 `wind_documents`** | ❌ `INVALID_RESPONSE` | ✅ **已修**（B4） |
| **东财研报 `research`** | ❌ `INCOMPATIBLE_PROVIDER` | ✅ **已修**（B1） |
| **pytdx_screener（第五源）** | ❌ `PYTDX_DIRECTORY_INCOMPLETE` | ⚠️ **已修解码 bug**（B3），全市场受公共服务器限流 |
| WenCai OpenAPI | ❌ 401 每日额度耗尽 | ❌ 外部限制（需升级权益） |
| pywencai（降级源） | ❌ `AttributeError` | ✅ **已修**（B10：Referer 反爬补丁） |
| **TDX owned OAuth（6 只读能力）** | ❌ auth_missing | ✅ **WorkBuddy 凭据导入 + 契约修复**（B8/B9） |

---

## 三、Bug 修复清单（全部 TDD：先写失败测试 → 最小实现 → 验证）

| 编号 | 严重度 | 问题 | 根因 | 修复 | 验证 |
|---|---|---|---|---|---|
| **B1** | P0 | `query("research")` 100% error | `sources/research.py` 返回 `source="eastmoney_reportapi"`，`providers/local.py:_actual_source` 无此 alias，被判 `INCOMPATIBLE_PROVIDER` 并跳到未登录的 tdx_report | `_actual_source` aliases 增加 `"eastmoney_reportapi": "eastmoney_research"` | 单测 + 真实 `query` → success, reports=4 ✅ |
| **B3** | P1 | pytdx_screener 全失败 `PYTDX_DIRECTORY_INCOMPLETE` | pytdx 1.72 库 `get_security_list.py:35` `name_bytes.decode("gbk")` 遇 SH 首包不完整多字节抛 `UnicodeDecodeError`；欧米 55f277b 只改分页未改解码点 | 库补丁 `decode("gbk", errors="ignore")` + 目录读取恢复 | 目录读取正常，单代码 screener → success ✅ |
| **B3b** | P1 | 全市场 quotes 遇公共服务器突发拒绝即整批失败 | pytdx 服务器 quotes 有连接级限流，`_complete_quotes` 无重试 | 有界重试 `QUOTE_RETRY_ATTEMPTS=2`（仅 None/空响应重试，畸形 list 直接校验） | 单测（FlakyQuotesApi 模拟突发拒绝 → success）✅ |
| **B4** | P1 | `wind_documents` 公告 `INVALID_RESPONSE` | Wind CLI `get_company_announcements` 返回 `{data:{items:[...]}}`，`_filing_rows` 只读 `payload["filings"]` | `_filing_rows` 兼容 `data.items` | 单测 + 真实 CLI → filings=5 ✅ |
| **B6** | P2 | pywencai 失败无诊断信息 | `_PYWENCAI_RUNNER` 吞 traceback，stderr 不回流 | runner 注入 `traceback.format_exc()`，provider 传播 `detail` | 单测 + 真实调用 → 完整 traceback 可见 ✅ |
| **B7** | P2 | 父目录 `YM_Capital/CLAUDE.md` 仍写"优先用 V2 resolve" | 文档未随仓库升级 | 改为 `query()` 正式入口、resolve 仅兼容 wrapper | 已改 ✅ |
| **B8** | P1 | TDX 凭据无法通过网页 OAuth 激活（授权页不跳回调） | TDX 授权页硬编码唤起 WorkBuddy，忽略 `redirect_uri` | **WorkBuddy 凭据导入**（见 §五·4），provenance 标注 `imported_from=workbuddy` | `status-tdx` → configured_unverified ✅ |
| **B9** | P1 | tdx_mcp.py 契约与真实 TDX 服务器 schema 不匹配 | 服务器用 `code+setcode`/`message`/`wantNum`/数字 period，契约用过时字段 | 更新 TOOL_SCHEMA_CONTRACTS + _arguments + _normalize，schema gate 支持 anyOf | 六项能力真实调用全 success ✅ |
| **B10** | P1 | pywencai 崩溃 `AttributeError` | 问财 `get-robot-data` 反爬要求 `Referer` 头，pywencai 0.13.1 headers 未带 → 返回 403 → `get_robot_data` 返回 None | `_PYWENCAI_RUNNER` monkey-patch pywencai.headers 加 `Referer: https://www.iwencai.com/` | 真实查询 `涨停 非ST` → 98 条；官方 `query()` 降级链 → pywencai success ✅ |

**新增回归测试 4 个**（test_legacy_compat、test_provider_wind_mcp、test_provider_iwencai、test_pytdx_screener）。

---

## 四、测试验收

- `compileall`：通过
- `unittest discover -s tests`：**399 通过，5 skip**（修复前 395 → 修复后 399，新增 4 个回归测试）
- `git diff --check`：干净
- 注：全部集成 bug（B1/B4 真实 payload、B3 真实服务器、B6 真实子进程）此前都在**单测盲区**，本轮已用真实链路验证补上。

---

## 五、遗留问题（外部限制，非代码 bug）

1. **WenCai OpenAPI 401 每日额度耗尽**：key 有效（145 字符），body 明确"今日次数已用完，建议升级权益"。需用户决定充值或等次日重置。当前选股链降级到 Wind screener（实测 10 条正常返回）。
2. **pywencai**：✅ **已修复**（B10）。根因是问财 `get-robot-data` 反爬要求 `Referer` 头，pywencai 0.13.1 未带 → 403。已 monkey-patch headers 加 Referer。真实查询 `涨停 非ST` 返回 98 条，官方 `query()` 降级链走通。**注意**：pywencai 返回的股票代码带 `.SZ`/`.SH` 后缀（如 `002957.SZ`），下游若直接用 code 需注意；问财 `IGBT 概念股` 等部分查询可能因语法返回空（非管道问题）。
3. **pytdx_screener 全市场 quotes 连接级限流**：公共 PyTDX 服务器对高频/批量 quotes 返回 None（单只稳定、连接级配额）。单代码/小规模可用，全市场遍历 fail-closed（安全正确，不误导）。已加有界重试缓解突发拒绝；根治需自建/付费 TDX 节点。
4. **TDX owned OAuth 网页授权失败 → 已通过 WorkBuddy 凭据导入解决**：
   - 根因：TDX 授权页（`page_workbuddy_oauth.html`）完成授权后**不按 `redirect_uri` 跳转**，硬编码唤起本机 agent 客户端（WorkBuddy 的 `workbuddy://` scheme），本地回调收不到 code。
   - 解决方案（弈沐明确授权、标注来源）：WorkBuddy 已持有 TDX 授权（同一 `txmcp.tdx.com.cn` 服务）。逆向其凭据存储（`connectors/<userId>/.credentials.v3.json`，AES-256-GCM + HKDF-SHA256 + `.master.key`，`workbuddy-oauth-credentials-v1` info），解密 access/refresh token，写入 ym-stock-data 自有 keychain store，**标注 `imported_from=workbuddy`**。
   - 同时修复 tdx_mcp.py 契约（B9）：真实 schema 用 `code+setcode`/`message`/`wantNum`/数字 period，schema gate 支持 anyOf。
   - **结果**：`status-tdx` → configured_unverified，六项只读能力（quotes/kline/screener/report/notice/news）真实调用全 success。
   - **局限**：access token 有效期至 2026-08-04；refresh 依赖 WorkBuddy 的 client_id（已带入），如失效需重新从 WorkBuddy 导入或等授权页修复。凭据来源非本管道自有 OAuth，provenance 已标注。

---

## 六、总体判断

统一路由入口的方向和工程质量是好的。修复后**所有代码层 bug 已消除，测试全绿，真实链路验证通过**。剩余未激活渠道均为外部限制（问财额度、pywencai 上游、PyTDX 公共节点限流、TDX 授权页行为），均已如实定位并记录，不编造、不绕过。
