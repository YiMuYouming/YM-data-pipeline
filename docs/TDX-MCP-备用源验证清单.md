# TDX MCP owned OAuth 与只读验证清单

TDX MCP 是 canonical `query()` 路由中的受管只读 fallback，只在前置语义兼容源
失败或合法空集后运行。它不直接暴露任意 tool，不单独形成交易授权。本轮只完成
离线实现和假服务测试；没有执行真实登录、DCR 或 live probe，因此当前不得称为
在线接通。

## Owned auth 状态机

| 状态 | 离线判据 | 行为 |
| --- | --- | --- |
| `auth_missing` | Keychain / 显式 0600 store 没有本管道凭据 | 停止；需弈沐明确执行 `./ym-data auth login-tdx` |
| `configured_unverified` | owned token + `mcp.read` 存在 | 只表示已配置，不证明在线 |
| `auth_expired` | 缺 scope、过期且不可 refresh、store 无效 | 停止；不继承旧结果 |
| `AUTH_EXPIRED` | 401 后强刷并重建 session，唯一重试仍失败 | 停止；不继续重试 |
| `AUTH_FORBIDDEN` | 403 | permission fail closed，不扩 scope、不伪装 expired |
| `MCP_ERROR` | initialize / tools/list / schema / payload 失败 | 停止该 provider，保留脱敏 attempt |

默认 secure store 是 macOS Keychain。文件 fallback 只能显式选择：目录 `0700`，
凭据文件与 refresh lock `0600`，原子写入；跨线程/跨进程 refresh 只能执行一次。
doctor 与 `status-tdx` 均离线，不输出 token、HTTP body、session、异常正文或业务行。

## 固定协议和工具门禁

- OAuth：protected resource discovery → authorization-server discovery → DCR →
  authorization-code + PKCE S256 → localhost state validation → refresh rotation。
- Scope：只接受精确 `mcp.read`；缺 scope 或出现 `mcp.write` 都失败。
- Transport：官方 `mcp==2.0.0` Python SDK，Streamable HTTP。
- 每次 session：`initialize` → `tools/list` 六项 schema gate → `tools/call`。
- 只读 allowlist：`tdx_screener`、`tdx_quotes`、`tdx_kline`、
  `wenda_report_query`、`wenda_notice_query`、`wenda_news_query`。
- 白名单外、额外 required 参数、schema drift、显式 destructive annotation、
  交易/写入工具全部在调用前拒绝。

## 离线 TDD 验收矩阵

| 场景 | 必须验证 |
| --- | --- |
| `auth_missing` | probe 不联网，canonical attempt 为 `AUTH_MISSING` |
| 首次登录 | fake AS 覆盖 discovery、DCR、S256、state、code exchange |
| 取消 / 超时 / 错 state | 不落盘，错误文本脱敏 |
| refresh rotation | access/refresh token 同时轮换；未返回新 refresh 时保留旧值 |
| 并发 refresh | 多线程、多进程只发送一次 refresh；401 同 token 强刷只一次 |
| expired / 缺 scope / write scope | `auth_expired` 或 `TdxScopeError`，不默许旧 token |
| 401 | 强刷一次、重建 session、最多重试一次 |
| 403 | `AUTH_FORBIDDEN`，不 refresh、不 reauthorize |
| `tools/list` | 缺任一六项或 schema drift 都不进入 `tools/call` |
| allowlist | 任意额外、交易、写入 tool 在 auth/transport 前拒绝 |
| secret sentinel | 不进入 argv、stdout、stderr、doctor、receipt、异常或 Git diff |
| offline | fake AS / fake MCP 全部禁止公网 |

## 后续 live read-only 验收（需要另行授权）

1. 弈沐一次性执行 `./ym-data auth login-tdx`，确认浏览器授权页只请求
   `mcp.read`。
2. 运行 `./ym-data auth status-tdx` 与 `./ym-data doctor --json`，只能得出
   `configured_unverified`，不能据此宣称在线。
3. 取得再次明确授权后，运行一次 `./ym-data smoke --live`；receipt 只保留
   状态、provider、attempt、row count、错误码和耗时，不保留响应或业务行。
4. 只读 probe 必须观察到 SDK `initialize`、完整 `tools/list` schema gate 与
   一个 allowlist 小调用成功，才可记录当次 online evidence。
5. 任何 401/403、schema drift、额外 required 字段或工具缺项都立即停止；禁止
   交易工具和任意 tool。
