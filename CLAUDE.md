# ym-stock-data — 弈沐资本 A 股统一数据管道

本仓库的正式事实入口只有 public `query()`。provider 选择、鉴权、自动降级、
合法空集和 provenance 都由 canonical registry 管理；调用方不得在失败后绕过
registry 直连 source、MCP tool 或 sidecar。

## 正式用法

```python
from ym_stock_data import query

market = query("realtime_market")
sectors = query("sector_index", names=["半导体"])
snapshot = query("stock_snapshot", codes=["603290", "688187"])
kline = query("stock_kline", code="603290", period="daily", count=20)
screen = query(
    "review_sentiment",
    query="A股 IGBT 概念股 非ST 总市值 PE PB",
    limit=20,
)
```

结果统一为 contract 1.0：`data + _meta`。判断实际降级过程时读取
`result["_meta"]["attempts"]`、`result["_meta"]["provider_used"]`、
`auth`、`error_code`、`quality` 和 `fetched_at`，不能根据预设顺序猜来源。

## CLI 与诊断

所有仓库命令通过根目录 launcher 执行：

```bash
./ym-data doctor --json
./ym-data setup pywencai
```

`doctor` 只做离线、脱敏的配置和依赖检查，不证明 provider 在线。只有任务明确
授权联网时才运行 `./ym-data smoke --live`；不得自行拼装 live 探针。

## 四类正式来源与一个实验性源

五类来源不是每个 intent 都依次调用。registry 只把请求交给语义兼容的 provider，
并在 `_meta.attempts` 中保留每次真实尝试。

| 来源 | 所有权与角色 | 主要语义 |
| --- | --- | --- |
| WenCai OpenAPI | API key；自然语言主源 | 显式 `review_sentiment` 查询；401/403/429 进入跨进程 breaker 后继续兼容路由 |
| portable pywencai | 本管道可移植 runtime；无 OpenAPI key | WenCai 兼容降级；dependency missing、provider error 和合法空集分别记录 |
| TDX owned OAuth | 本管道自有只读 OAuth，仅请求 `mcp.read` | 兼容源失败后的选股、报价、K 线、研报、公告和新闻能力 |
| official Wind CLI | Wind 官方 CLI 自行鉴权 | 自然语言 `wind_screener`、显式 `wind_enrichment` 和严格验证后的 `filings` fallback |
| zero-auth PyTDX | 无鉴权 TCP 数据源；结构化 screener 为实验性显式能力 | 行情、快照、K 线和市场宽度；`pytdx_screener` 不进入自然语言自动降级或正式 live gate |

显式自然语言主链固定为 OpenAPI → pywencai → TDX screener → Wind screener。
`pytdx_screener` 只保留为实验性显式 provider；即使查询可被
`pytdx-structured-1` 完整消费，也不追加到 public `query()` route。运行时固定
`pytdx==1.72`，目录、quote 或价格未就绪均 fail closed，不调用其它 HTTP fallback。

合法空集、鉴权失败、依赖缺失、provider error 和网络失败不是同一种状态。只有
当前 intent 定义允许继续的空集才会进入下一兼容源；最终结果必须保留全部 attempts。

## TDX 自有鉴权与只读边界

首次登录和离线状态检查统一使用：

```bash
./ym-data auth login-tdx
./ym-data auth status-tdx
```

默认凭据保存到 macOS Keychain；只有显式选择 file store 时才使用本管道私有的
`0700` 目录和 `0600` 文件。命令、日志、doctor、smoke 和 receipts 都不得输出
token、Key、授权正文或凭据路径。

官方页面若只能完成 WorkBuddy 授权，可在弈沐明确授权下把凭据一次性迁入本管道
安全存储并记录 `imported_from=workbuddy`；运行时不得扫描或持续同步 WorkBuddy。

TDX 固定只读能力只有六项：`tdx_screener`、`tdx_quotes`、`tdx_kline`、
`wenda_report_query`、`wenda_notice_query`、`wenda_news_query`。每次连接必须先
`initialize` 和 `tools/list`，只校验当前目标 tool 的严格 schema；不允许任意 tool、
写入 tool、交易接口或 scope 扩张。401 只刷新一次，403 直接失败关闭。

## 兼容与开发边界

V1 `fetch()` 和 V2 `resolve()` 只是旧消费者的 compatibility wrapper，用来维持
历史业务 shape；它们不拥有第二条 provider chain，也不推荐新代码使用。未完成
side-by-side shape、provider/attempts、empty/error overwrite guard 前，不切换下游
默认值。

新代码只调用 `query()`，只通过 registry 增加 provider，并使用稳定错误码和
脱敏元数据。不得把数据查询结果当作交易授权，不发交易 POST，不调用券商接口，
不覆盖生产 runtime/cache/data。

更完整的 provider ownership、capability 和自动降级矩阵见 `README.md`；安装、
Keychain/file store 和登录验收步骤见 `docs/INSTALL.md` 与
`docs/TDX-MCP-备用源验证清单.md`。
