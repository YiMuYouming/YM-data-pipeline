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
- 五日验收从离线 `./ym-data acceptance template --date YYYY-MM-DD` 开始；唯一执行入口与安全探针见 `docs/ACCEPTANCE_RUNBOOK.md`，不要自行拼装 schema 或重复 live 调用。
- 完成前运行 `uv run python -m compileall -q ym_stock_data scripts tests`、`uv run python -m unittest discover -s tests -v`、`git diff --check` 和敏感路径扫描。

## 公共 API 与契约

新代码只调用 `query(intent, **params)`，不得直接 import `ym_stock_data.sources` 或 `ym_stock_data.v2`。结果统一使用 contract 1.0：`data` 加 `_meta`，其中必须保留 `status`、真实 `provider_used`、完整 `attempts`、`quality`、`fetched_at` 与稳定错误码。

正常、合法空集和失败都只由 canonical `build_result` 构造。参数验证发生在任何 provider 调用前。合法空集默认终止路由；只有带显式 `query` 的 `review_sentiment` 按 RouteSpec 的 `continue_until_exhausted` 策略继续穷尽自然语言 screener，顺序固定为 OpenAPI → pywencai → TDX screener → 专用 Wind `stock_data.search_stocks`，直到非空成功或兼容源耗尽。Wind 必须使用 `wind_screener` 专名并严格验证 tabular `Wind代码`，不得借泛化 `wind_mcp` 扩展其它 intent；行情/宽度/K 线源也不得冒充自然语言 screener。穷尽不保证有结果。畸形 payload、无效空响应、route 外 provenance、鉴权或 provider 错误必须形成可审计 attempt 并按兼容路由继续。

V1 `fetch()` 与 V2 `resolve()` 仅是 compatibility wrapper：允许维持旧 shape，但不得拥有第二条 provider chain。不要在新文档或脚本中推荐它们，也不要用强制 `DeprecationWarning` 破坏消费者。

## Provider 边界

- 零鉴权源优先；TDX owned OAuth 只在兼容源失败后调用固定六项只读能力。
- TDX 不接 realtime/default breadth/sector，不调用任意 tool，不做交易写入。
- Wind official CLI 仅支持显式 `wind_enrichment`、严格验证后的 `filings` fallback，以及显式 `review_sentiment(query=...)` 的专用 `wind_screener`；后者只调用 `stock_data.search_stocks`，沪深股票族与 `_all_share_codes` 一致，北交所当前仅允许 `920xxx.BJ`。不得让泛化 `wind_mcp` 接行情、K 线、分钟、新闻、泛选股或 `stock_event`。
- WenCai OpenAPI 401/403/429 使用跨进程 breaker；pywencai 依赖缺失与 provider error 必须区分。
- Key、token、credentials 不进入 argv、日志、doctor、CLI 输出、receipt 或 Git。
- 不发交易 POST、不调用券商、不部署、不 push，除非弈沐另行明确授权。

完整 ownership、setup、doctor 状态、capability 和 automatic fallback 表见 `README.md`。

## 下游与回滚

迁移消费者时保留业务 shape、provider provenance、attempts、质量 reason codes、合法 empty 语义和 observation-only 边界。旧路径若暂时保留，只能集中在一个默认 `legacy` 的 rollback switch 后；只有同一时点 side-by-side 对业务 shape、provider/attempts、空/error overwrite guard 全部通过，才可考虑切换默认值。

不得对 live-dashboard 真实 8088 发 POST，不得覆盖生产 data/cache/runtime，不得把数据查询结果当成交易授权。
