# ym-stock-data 项目执行规则

本仓库是弈沐资本 A 股统一数据获取基座。正式公共入口只有：

```python
from ym_stock_data import query

result = query("stock_snapshot", codes=["603290", "688187"])
print(result["_meta"])
```

## 开工与验证

- 先读本文件、`README.md` 和当前实施计划，再检查 `git status --short`。
- 保护已有 dirty work；禁止 reset、clean、stash。所有 staging 使用显式文件路径。
- 正式 repo CLI 使用根目录 `./ym-data ...`；它为每个 checkout/worktree 选择 uv cache 外置环境，避免 macOS File Provider 的 hidden editable `.pth`。裸 `uv run ...` 仅用于不受该问题影响的底层开发验证。系统 Python 缺依赖不能证明 provider 不可用。
- 改动按失败测试 → 最小实现 → 聚焦测试 → 全量测试推进。
- 基础健康检查：`./ym-data doctor --json`。doctor 只报告脱敏状态，不证明在线。
- 只有显式 `./ym-data smoke --live` 才联网；报告不得保存业务行、Key、token、stderr 或异常正文。
- 五日验收工具从离线 `./ym-data acceptance template --date YYYY-MM-DD` 开始；当前基线是 acceptance 1.3 / smoke schema 2 / `four-source-capabilities-v1` 的 21-case 能力矩阵。2026-08-04 起不再定时运行，也不作为当前 Goal 的闭环门槛；只有弈沐再次明确授权手工验收时，才按 `docs/ACCEPTANCE_RUNBOOK.md` 执行，不得自行拼装 schema、重复 live 调用或把一次 smoke 当成登录授权。
- 完成前运行 `uv run python -m compileall -q ym_stock_data scripts tests`、`uv run python -m unittest discover -s tests -v`、`git diff --check` 和敏感路径扫描。

## 公共 API 与契约

新代码只调用 `query(intent, **params)`，不得直接 import `ym_stock_data.sources` 或 `ym_stock_data.v2`。结果统一使用 contract 1.0：`data` 加 `_meta`，其中必须保留 `status`、真实 `provider_used`、完整 `attempts`、`quality`、`fetched_at` 与稳定错误码。

正常、合法空集和失败都只由 canonical `build_result` 构造。参数验证发生在任何 provider 调用前。合法空集默认终止路由；只有带显式 `query` 的 `review_sentiment` 按 RouteSpec 的 `continue_until_exhausted` 策略继续穷尽自然语言 screener，顺序固定为 OpenAPI → pywencai → TDX screener → 专用 Wind `stock_data.search_stocks`。`pytdx_screener` 只保留为实验性显式 provider，不进入 canonical 自动降级链，也不进入正式 live gate。Wind 必须使用 `wind_screener` 专名并严格验证 tabular `Wind代码`，不得借泛化 `wind_mcp` 扩展其它 intent。穷尽不保证有结果。畸形 payload、无效空响应、route 外 provenance、鉴权或 provider 错误必须形成可审计 attempt 并按兼容路由继续。

V1 `fetch()` 与 V2 `resolve()` 仅是 compatibility wrapper：允许维持旧 shape，但不得拥有第二条 provider chain。不要在新文档或脚本中推荐它们，也不要用强制 `DeprecationWarning` 破坏消费者。

## Provider 边界

- 零鉴权源优先；TDX owned OAuth 只在兼容源失败后调用固定六项只读能力。
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
