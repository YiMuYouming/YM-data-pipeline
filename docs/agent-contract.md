# 数据管道开发契约

仅在修改 provider、路由、契约、凭据或下游迁移时读取相关章节。相对路径以仓库根为准。
当前 canonical checkout 是 `/Users/yimu/Projects/YM_Capital/YM-data-pipeline`。

当前发布状态与后续业务测试见 [渠道工作区总览](README.md)；历史计划不是新增验收门槛。

Agent / Skill 的唯一数据入口是 `from ym_stock_data import query` 与仓库根
`./ym-data`。provider 选择和 fallback 只能由 canonical router 执行，不能由消费端
自行串接；没有匹配能力时停在 public contract 并报告 source gap。

## 公共 API 与契约

新代码只调用 `query(intent, **params)`，不得直接 import `ym_stock_data.sources` 或 `ym_stock_data.v2`。结果统一使用 contract 1.0：`data` 加 `_meta`，其中必须保留 `status`、真实 `provider_used`、完整 `attempts`、`quality`、`fetched_at` 与稳定错误码。

正常、合法空集和失败都只由 canonical `build_result` 构造。参数验证发生在任何 provider 调用前。合法空集是否继续由各 RouteSpec 的 `empty_policy` 决定；带显式 `query` 的 `review_sentiment` 按 `continue_until_exhausted` 策略继续穷尽自然语言 screener，顺序固定为 OpenAPI → pywencai → TDX screener → 专用 Wind `stock_data.search_stocks`。`pytdx_screener` 只保留为实验性显式 provider，不进入 canonical 自动降级链，也不进入正式 live gate。Wind 必须使用 `wind_screener` 专名并严格验证 tabular `Wind代码`，不得借泛化 `wind_mcp` 扩展其它 intent。穷尽不保证有结果。畸形 payload、无效空响应、route 外 provenance、鉴权或 provider 错误必须形成可审计 attempt 并按兼容路由继续。

V1 `fetch()` 与 V2 `resolve()` 仅是 compatibility wrapper：允许维持旧 shape，但不得拥有第二条 provider chain。不要在新文档或脚本中推荐它们，也不要用强制 `DeprecationWarning` 破坏消费者。

V3 metadata 是 contract 1.0 的增量字段：`_meta.pipeline_version`、
`_meta.route_policy_version`、`_meta.source_tier` 和
`_meta.policy_evidence_sha256` 必须与真实 `provider_used`、完整 `_meta.attempts`、
`_meta.quality`、`_meta.fetched_at` 一起保留。quality 的 status、returned_count、
reason_codes、coverage/missing 和明确的 source_gap 不能被空结果或 fallback 覆盖。

StockToday 的 catalog 只说明可请求的 method/parameter 边界，不是 promotion 证据。
catalog presence、HTTP 200 和非空都不等于 accepted primary；主源晋级需要 reviewed
receipt、字段/过滤/分页/时效证据和 active capability policy。

实时行情时效按上海交易所可更新行情的时段计算：09:15–09:25、09:30–11:30、
13:00–15:00；其余时段暂停 60 秒行情年龄计时，但始终核对行情所属交易日，
并保留原始 `quote_time` 和接收 `fetched_at`。午休、收盘后及周末只能接受
最近交易时段末端的行情；开市后超时仍按质量失败继续降级。
StockToday 的 A 股实时报价 observation 与统一质量门共用同一交易时段计龄和 60 秒阈值；不能让适配器
先按自然时间标记 `stale`，使有效的午休或收盘行情提前降级。`index_kline(codes=...)`
要求每个请求指数恰好对应一份有有效 K 线的结果，缺失、重复或代码错配均不能
终止降级链。无日期热榜及非实时资金查询使用管道自带的上交所交易日历；
日历不覆盖的年份报 `CALENDAR_UNAVAILABLE`，调用方可明确传入 `trade_date`。

## Provider 边界

- 按用途执行 RouteSpec：默认行情/历史查询优先 StockToday；高频个股轮询优先腾讯、大盘轮询优先 PyTDX。TDX owned OAuth 仅按对应路由作为后备，调用固定六项只读能力。
- TDX 不接 realtime/default breadth/sector，不调用任意 tool，不做交易写入。
- TDX 登录优先走本仓库 `./ym-data auth login-tdx`；状态只走离线
  `./ym-data auth status-tdx`。默认 macOS Keychain，文件 fallback 必须显式
  `--store file`。若官方授权页只能回到 WorkBuddy，可在弈沐明确授权下做一次性受控凭据迁入并记录 `imported_from=workbuddy`；运行时代码不得扫描、读取或持续同步 WorkBuddy 凭据。
- TDX OAuth 只允许 `mcp.read`，使用 authorization-code + PKCE S256 和 state
  校验；403 不扩 scope。MCP 固定使用官方 `mcp==2.0.0` Streamable HTTP，
  `tools/list` 六项 schema gate 通过前不得 `tools/call`。
- Wind official CLI 仅支持显式 `wind_enrichment`、严格验证后的 `filings` fallback，以及显式 `review_sentiment(query=...)` 的专用 `wind_screener`；后者只调用 `stock_data.search_stocks`，沪深股票族与 `_all_share_codes` 一致，北交所当前仅允许 `920xxx.BJ`。不得让泛化 `wind_mcp` 接行情、K 线、分钟、新闻、泛选股或 `stock_event`。
- WenCai OpenAPI 401/403/429 使用跨进程 breaker；pywencai 依赖缺失与 provider error 必须区分。
- 实验性零鉴权 `pytdx_screener` 固定 `pytdx==1.72`，只接受唯一沪深 universe 加至少一个 `非ST` / `非停牌` / 单代码 / `最新价` / `涨幅` AND 条件；数值条件必须含 `非停牌`。不支持北交所，也不支持行业、概念、PE、PB、排名、OR 或日期。它必须读取完整目录与完整 quotes，batch 不超过 80，不调用现有 source fallback；当前只允许显式 provider 诊断/开发调用，不作为 public `query()` 自动兜底。
- Key、token、credentials 不进入 argv、日志、doctor、CLI 输出、receipt 或 Git。
- 不发交易 POST、不调用券商、不部署、不 push，除非弈沐另行明确授权。

完整 ownership、setup、doctor 状态、capability 和 automatic fallback 表见 `README.md`。

## 下游与回滚

迁移消费者时保留业务 shape、provider provenance、attempts、质量 reason codes、合法 empty 语义和 observation-only 边界。旧路径若暂时保留，只能集中在一个默认 `legacy` 的 rollback switch 后；只有同一时点 side-by-side 对业务 shape、provider/attempts、空/error overwrite guard 全部通过，才可考虑切换默认值。

不得对 live-dashboard 真实 8088 发 POST，不得覆盖生产 data/cache/runtime，不得把数据查询结果当成交易授权。
