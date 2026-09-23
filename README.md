# ym-stock-data

弈沐资本 A 股统一数据通道。canonical checkout 是
`/Users/yimu/Projects/YM_Capital/YM-data-pipeline`。正式公共入口只有
`from ym_stock_data import query` 与仓库根 `./ym-data`；所有成功、合法空集和失败都返回
contract 1.0，并在 `_meta` 中保留真实 provider、attempt chain、质量与错误码。

## 快速开始

在项目环境中调用，避免把系统 Python 缺依赖误判为 provider 不可用：

```bash
cd /Users/yimu/Projects/YM_Capital/YM-data-pipeline
uv run python - <<'PY'
from ym_stock_data import query

print(query("realtime_market")["_meta"])
print(query("sector_index", names=["半导体"])["_meta"])
print(query("stock_snapshot", codes=["603290", "688187"])["_meta"])
print(query("stock_kline", code="603290", period="daily", count=20)["_meta"])
print(query("review_sentiment", query="A股 IGBT 概念股 非ST", limit=20)["_meta"])
print(query("review_sentiment", query="沪深A股 非ST 非停牌 最新价>=10 涨幅<5%", limit=20)["_meta"])
PY
```

### 每日涨跌停事实与短线派生指标（本地试用）

`./ym-data market-facts` 把涨停、跌停、炸板名单和全市场未复权日线写入独立的
`data/market-facts.sqlite3`。只写入校验通过的整日结果；间歇错误会按日期列入
回填回执，不会把失败榜单当成零。运行数据库不入 Git。

```sh
./ym-data market-facts backfill-history --start 20260901 --end 20260923 --max-days 25
./ym-data market-facts backfill-daily --start 20260901 --end 20260923 --max-days 25
./ym-data market-facts report --date 20260923
```

`report` 给出昨日涨停、昨日二板及以上、昨日炸板三个固定名单的今日平均涨跌幅，
并保留来源、交易日、样本数和缺口。弈沐上涨占比当前以有交易日线的股票为分母；
日线不含停牌股票，不能标为同花顺“上涨家数 / 全部上市股票”的精确同口径值。
部分旧版 `limit_list_d` 历史行字段错位；同日 `limit_step` 板数只作待核参考，
错误价格不入库，分层晋级和昨日连板收益在板数未核实时留空。榜单与日线逐股冲突
也会阻断相关派生值。连板股三日风险和一年中位数尚未产出。
此库尚未接入生产消费者。

逐项试用时只运行 `report`；它以 SQLite 只读模式打开现有本机库，库不存在会明确报错，
不会在查询时建库。建议按下面顺序检查 `source_gaps`、`return_evidence`、
`limit_daily_quality` 和各指标是否为 `null`：

```sh
./ym-data market-facts report --date 20260923  # 正常盘后样本
./ym-data market-facts report --date 20260107  # 缺股时只保留完整的炸板收益
./ym-data market-facts report --date 20260428  # 涨停榜与日线冲突，晋级率阻断
```

这是历史数据快照的本机验证，不代表今日实时行情或生产消费端已接入。

主要 intent：

| intent | 用途 | 关键参数 |
| --- | --- | --- |
| `realtime_market` | 指数、成交额、涨跌家数 | 无 |
| `sector_index` | 行业板块 | `names` / `codes` |
| `stock_snapshot` | 个股行情与均线快照 | `codes` |
| `stock_kline` | 个股 K 线 | `code`, `period`, `count` |
| `stocktoday_data` | 显式 StockToday 只读数据集 | `api_name`, `params`, `fields`, `max_rows` |
| `review_sentiment` | 市场宽度或显式自然语言筛选 | `query`, `limit`, `expected_row_shape`, `expected_count`, `date`, `lang`, `version` |
| `market_limit_state` | 涨跌停池聚合 | 无 |
| `market_limit_board` | 涨停、跌停、炸板、昨日涨停明细 | `kind`, `date` |
| `market_hot_rank` | 同花顺或东财热榜 | `source`, `trade_date`, `limit` |
| `stock_event` | 个股低频事件 | `event`, `code` |
| `research` / `filings` / `news` | 研报、公告、新闻 | `code` 等 intent 参数 |
| `wind_enrichment` | 显式 Wind 研究增强 | `capability`, `code` / `codes`, `fields`, `params` |

先看公开能力和脱敏状态：

```bash
./ym-data list
./ym-data doctor --json
```

根目录 `./ym-data` 是正式 repo CLI 入口。它按项目绝对路径为每个 checkout/worktree 选择独立的 uv cache 外置环境；调用方显式设置 `UV_PROJECT_ENVIRONMENT` 时会保留该值。launcher 会选择实际通过 `--version` 探针的 uv；需要固定二进制时可显式设置绝对路径 `YM_DATA_UV_BIN`，无效 override 会直接失败而不降级。这样 macOS Documents File Provider 即使给项目内 dotpath 标记 hidden，也不会影响外置环境中的 editable `.pth`。路径和参数都按参数边界传递，不写入凭据。

`doctor` 不联网验证数据业务，不打印 token、Key、异常正文或业务行。只有显式 `./ym-data smoke --live` 才运行只读在线探针；默认 smoke 不联网。裸 `uv run ym-data ...` 是底层调用，只适用于不受 File Provider dotpath 影响的环境，不再作为正式 CLI 指引。

TDX 由本管道自行完成 OAuth discovery、DCR、authorization-code + PKCE
S256、state 校验和 refresh rotation。首次授权命令是
`./ym-data auth login-tdx`，离线查看脱敏状态使用
`./ym-data auth status-tdx`。登录只请求 `mcp.read`；任何 `mcp.write`、403
scope escalation 或白名单外工具都 fail closed。本轮离线实现没有执行真实登录，
也没有证明线上 TDX 已接通。

## 手工五日验收工具

该工具只在弈沐再次明确授权手工验收时使用，不再由 automation 定时运行，也不作为当前统一数据通道 Goal 的闭环门槛。盘后从离线 `./ym-data acceptance template --date YYYY-MM-DD` 开始；当前契约使用 acceptance 1.3、smoke schema 2 和 `four-source-capabilities-v1` baseline，严格要求 21 个固定 case：保留原 10 个核心位置，将 PyTDX case 改为不联网的可选 provider 状态，并分别直测 OpenAPI、pywencai、TDX 六项、Wind 三项与 canonical TDX 受控降级。只有 OpenAPI、pywencai、TDX、Wind 四类 `source_status` 全 pass、`chain_status=pass` 且 `gate_status=pass` 才写 acceptance；可选 PyTDX 状态不进入 gate，empty、doctor configured、TCP 可达都不算正式能力已通。`smoke --live` 总会在成功写入脱敏 receipt 后回显三层 gate；gate fail 返回非零，但不会删除失败证据。provider 指标只统计 `origin=live`，受控链的 injected/live attempts 单独保存。旧 acceptance 1.0/1.1 可只读验证；未发布的 acceptance 1.2 / `five-source-structured-v1` 只有在 acceptance 与 smoke 文件权限、日期、哈希和完整性全部通过时才会作为不计数历史忽略，畸形旧文件会阻断而不是静默跳过。唯一字段契约、同日去重、一次性 live 命令、下游安全探针、build/validate 和自检步骤见 [`docs/ACCEPTANCE_RUNBOOK.md`](docs/ACCEPTANCE_RUNBOOK.md)；不要复制 schema 或自行补字段。一次 live smoke 不授权 TDX 登录，也不会自动启动后续五日测试。

Direct 能力只有非空 `success` 才算 pass；即使 attempt 中存在 live success，case 为 `degraded` 仍不能通过 source gate。TDX report/notice 与 Wind filings 使用固定 365 天只读窗口（Wind 仍为 `max_pages=1`），在不增加调用次数的前提下降低公告静默期假阴性；语义合法 empty 仍只表示可达空集，不表示该能力已通。

## 统一结果契约

每次 canonical 调用都返回：

```text
{
  "data": ...,
  "_meta": {
    "contract_version": "1.0",
    "pipeline_version": "3.0",
    "route_policy_version": "3.0",
    "source_tier": "primary | fallback | explicit",
    "policy_evidence_sha256": null,
    "status": "success | empty | degraded | error",
    "provider_used": "真实成功 provider，失败时为 null",
    "attempts": [{"provider": "...", "status": "...", "error_code": "..."}],
    "quality": {"status": "...", "returned_count": 0, "reason_codes": []},
    "fetched_at": "带时区时间"
  }
}
```

## Agent / Skill 唯一入口（V3）

Agent、Skill 和新消费端统一从本 Projects checkout 工作：
`/Users/yimu/Projects/YM_Capital/YM-data-pipeline`。Python 只使用
`from ym_stock_data import query`，命令行只使用仓库根 `./ym-data`；CLI 是没有 Skill
的平台的同一公共入口，不是第二条 provider 链。

V3 `_meta` 的 `pipeline_version`、`route_policy_version`、`source_tier` 和
`policy_evidence_sha256` 与 contract 1.0 的 `provider_used`、完整 `attempts`、
`quality`、`fetched_at` 一起判断结果。`quality.reason_codes`、`missing`、coverage
以及必要时的 `source_gap` 必须保留，不能把合法 empty、provider error、缺依赖、
降级成功或未验证状态混写成成功。

`fetch()`、`v2.resolve()` 和旧直接入口仅是 compatibility wrapper，只服务尚未迁移的
旧消费者；它们不是 Agent 推荐入口，也不是查询失败后的手工降级步骤。调用方不得直接
选择 provider、拼接 fallback、调用 vendor SDK 或任意 URL。

当前最小行情路由：StockToday 是 `realtime_market`、`stock_snapshot`、日/周/月/分钟
`stock_kline` 的统一第一源；合法空集、错误或超时后，才按各自 RouteSpec 进入腾讯、
PyTDX、东财/TDX 等现有源，并以 `degraded`/`source_tier=fallback` 保留降级事实。分钟
K 线固定为 StockToday → PyTDX → Sina → TDX。StockToday 不是所有能力的 overall primary，
每个 intent 的顺序只由 RouteSpec 决定。

高频中文短语由仓库内 deterministic intent registry 固定映射，CLI 使用
`./ym-data intent "查涨停板"`、`查跌停板`、`查同花顺热榜`、`查东财热榜`、`查实时个股`、
`查实时大盘`、`查日K/查周K/查月K`、`查分钟K`、`查板块/查行业`；每个短语的 API、
必填参数和允许参数见 canonical Skill。任意已开通的其他接口仍可通过受控
`stocktoday_data(api_name, params)` 直达，调用方不得自由猜 API 或自建降级链。

StockToday 的 catalog 只是显式 `stocktoday_data` 的方法/参数边界；长尾结果保留
provider-native 口径。标准化 intent 的第一源与降级顺序以 RouteSpec 为准，所有调用
仍必须检查字段、过滤、分页、时效、`quality` 与 `source_gap`。

合法空集默认终止路由；唯一例外是带显式 `query` 的 `review_sentiment`，它固定按 OpenAPI → pywencai → TDX screener → Wind `stock_data.search_stocks` 的顺序穷尽四个语义兼容来源。`pytdx_screener` 不再追加到 public route，只保留为实验性显式 provider。只有当次四源 route 的所有 attempt 都是语义有效 empty 时，最终状态才是 `empty`；任一前序 auth/provider/依赖错误都不得被后续 empty 覆盖，链路耗尽后仍是 `error` 且 `provider_used=null`。

实验性 `pytdx_screener` 只接受唯一的 `沪深A股`、`沪市A股` / `上交所A股`、`深市A股` / `深交所A股` universe，并要求至少一个 `非ST`、`非停牌`、单一 `股票代码为/是/=六位代码`、`最新价` 或 `涨幅` AND 条件；数值条件还必须同时带 `非停牌`。比较符和 `到` / `至` / `~` 区间以固定语法完整消费。不支持北交所，也不支持行业、概念、PE、PB、排名、OR 或日期。它使用固定 `pytdx==1.72` 直接读取沪深完整目录与 quotes，每批最多 80 个，不调用既有 `fetch_quotes` 或腾讯、东财、Sina fallback。目录或 quote 不完整、全部价格未就绪时只能报稳定错误，不能伪装合法空集；当前不参与自动 fallback 或正式 live gate。

Wind 只通过专用 `wind_screener` 进入自然语言链，严格读取已验证 tabular envelope 的精确 `Wind代码` 列，不复用泛化 `wind_mcp` enrichment。它只接受沪市 `600/601/603/605/688/689`、深市 `000/001/002/003/300/301` 与北交所自 2025-10 全面启用的 `920` 股票族，并校验交易所 suffix；指数、ETF、旧北交所代码族和交易所错配均 fail closed。穷尽不保证一定有结果。无效空响应、畸形 payload、鉴权失败或 route 外 provenance 会形成可审计 attempt，再尝试下一个语义兼容源。单元测试通过不等于 provider 在线，在线状态以当次只读 probe 为准。

## Provider ownership 与路由边界

TDX route provider 只在所有排在其前的语义兼容源失败或合法空集后调用；显式 `review_sentiment` 的 Wind screener 只在 OpenAPI、pywencai、TDX screener 均未返回非空成功后调用。`tdx_mcp` 只聚合诊断状态，不参与 RouteSpec。

| provider id | ownership / setup | doctor 状态 | intended capabilities / RouteSpec 次序 | automatic fallback |
| --- | --- | --- | --- | --- |
| `pytdx` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `realtime_market`、`stock_snapshot`、日周月/分钟 `stock_kline` 后备 | 允许；只按对应 RouteSpec 次序 |
| `pytdx_index` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `index_kline` 日/周/月末级后备；不提供指数分钟 K 线 | 允许；仅在 StockToday、东财指数与新浪指数失败或合法空集后 |
| `eastmoney` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `realtime_market` 后备末级 | 允许；仅在前置源失败或合法空集后 |
| `eastmoney_index` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `index_intraday_compare` 第一源；`index_kline` 在 StockToday 后 | 允许；只按对应 RouteSpec 次序 |
| `eastmoney_stock` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | 日/周/月 `stock_kline` 第二源；明确保持 `none` / `qfq` 复权语义 | 允许；仅在 StockToday 失败或合法空集后 |
| `tencent` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `realtime_market`、`stock_snapshot`、日周月 `stock_kline` 第一后备源 | 允许；仅在 StockToday 失败或合法空集后 |
| `sina` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | 分钟 `stock_kline` 第三源 | 允许；只按上述 RouteSpec 次序 |
| `sina_index` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `index_intraday_compare` 第二源；`index_kline` 在东财指数之后，仅支持分钟周期 | 允许；只按对应 RouteSpec 次序 |
| `ths_industry` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `sector_index` 唯一源；`industry_flow` 默认链后备，保留价格/表现语义 | `industry_flow` 仅按对应 RouteSpec 次序 |
| `pytdx_breadth` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | 默认 `review_sentiment` 第一源 | 允许；失败后进入 `eastmoney_breadth` |
| `eastmoney_breadth` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | 默认 `review_sentiment` 第二源 | 允许；仅在 `pytdx_breadth` 失败后 |
| `eastmoney_limit_pool` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `market_limit_state` 唯一聚合源；`market_limit_board` 降级源；默认 `review_sentiment` 第三源 | `market_limit_board` 仅在 StockToday 失败或合法空集后；既有聚合不静默换源 |
| `eastmoney_datacenter` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `stock_event` 唯一源 | 否；当前无语义兼容后继源 |
| `eastmoney_research` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `research` 第一源 | 允许；失败后进入 `tdx_report` |
| `northbound` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `northbound_flow` 默认链第二源；`realtime_poll` 使用该来源的当前分钟序列 | 允许；仅按对应 RouteSpec 次序 |
| `ths_hot` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `legacy_hot_rank` 默认链第二源；`realtime_poll` 第一源，须满足旧业务 shape | 允许；仅按对应 RouteSpec 次序 |
| `cninfo` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `filings` 第一源 | 允许；失败后进入 `tdx_notice` |
| `cls` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `news` 第一源 | 允许；失败后进入 `tdx_news` |
| `iwencai_openapi` | API key；优先环境，其次管道 Keychain，再兼容旧 profile；不打印配置值 | `configured_unverified` / `breaker_open` / auth 错误 | 显式 `review_sentiment` 第一源 | 允许；失败或合法空集后进入 `pywencai` |
| `pywencai` | 可移植 runtime；`./ym-data setup pywencai` | `configured_unverified` / `dependency_missing` / `unavailable` | 显式 `review_sentiment` 第二源 | 允许；仅在 `iwencai_openapi` 失败或合法空集后 |
| `pytdx_screener` | 实验性零鉴权；固定 `pytdx==1.72`；无 setup | `configured_unverified` 或明确错误 | 仅显式 provider 诊断/开发调用；无 RouteSpec | 否；不进入 public `query()` 自动降级或正式 live gate |
| `tdx_mcp` | owned OAuth；`./ym-data auth login-tdx`，`./ym-data auth status-tdx` | TDX 总状态 `configured_unverified` / `auth_missing` / `auth_expired` | 诊断聚合，无 RouteSpec | 否；不执行业务查询 |
| `tdx_screener` | owned OAuth；同上 | 独立能力状态 | 显式 `review_sentiment` 第三源 | 允许；仅在 `iwencai_openapi`、`pywencai` 失败或合法空集后 |
| `tdx_quotes` | owned OAuth；同上 | 独立能力状态 | `stock_snapshot` 后备末级 | 允许；仅在前置源失败或合法空集后 |
| `tdx_kline` | owned OAuth；同上 | 独立能力状态 | 日周月及分钟 `stock_kline` 第三源 | 允许；仅在对应周期前置兼容源失败后 |
| `tdx_report` | owned OAuth；同上 | 独立能力状态 | `research` 第二源 | 允许；仅在 `eastmoney_research` 失败后 |
| `tdx_notice` | owned OAuth；同上 | 独立能力状态 | `filings` 第二源 | 允许；仅在 `cninfo` 失败后 |
| `tdx_news` | owned OAuth；同上 | 独立能力状态 | `news` 第二源 | 允许；仅在 `cls` 失败后 |
| `wind_screener` | official CLI；由 CLI 管理配置 | `configured_unverified` 或 runtime 错误 | 显式 `review_sentiment` 第四源；仅 `stock_data.search_stocks` | 允许；前三个自然语言 screener 失败或合法空集后 |
| `wind_mcp` | official CLI；由 CLI 管理配置 | `configured_unverified` 或 runtime 错误 | 显式 `wind_enrichment` 唯一源 | 否；只响应显式调用 |
| `wind_documents` | official CLI；由 CLI 管理配置 | `configured_unverified` 或 runtime 错误 | `filings` 第三源 | 允许；仅在 `cninfo`、`tdx_notice` 失败后 |
| `stocktoday` | 独立 API key；macOS Keychain；`./ym-data auth set-stocktoday --stdin` | `configured_unverified` / `auth_missing` / `unavailable` | `stocktoday_data`、`realtime_market`、`stock_snapshot`、日周月/分钟 `stock_kline`、`market_limit_board`、`market_hot_rank`、`index_kline`、`index_intraday_compare`、`industry_flow`、`fund_flow`、`northbound_flow`、`legacy_hot_rank` 第一源或后备；不替代既有板块/聚合语义 | 是；失败或合法空集后按 RouteSpec 降级，见 [接入说明](docs/STOCKTODAY.md) |

`setup pywencai` 只有显式执行时才写 `~/.ym-stock-data`，固定使用 Python 3.12 兼容环境。setup 返回的 `ready` 仅表示 runtime installed，不是 doctor 在线状态，也不证明在线。OpenAPI Key 的优先级为当前进程环境、管道专用 macOS Keychain、旧 profile 兼容读取；不得写入仓库或日志。TDX 首次默认把本管道自有凭据保存到 macOS Keychain；只有显式 `--store file` 才使用目录 `0700`、文件和锁 `0600` 的原子文件 fallback，`--file-path` 可指定自有文件位置。成功登录或弈沐明确授权的一次性受控迁入后，后续 canonical query、doctor、smoke 和无 override 的 `auth status-tdx` 只使用本管道安全存储；从 WorkBuddy 迁入时必须记录 `imported_from=workbuddy`。运行时代码不会扫描、读取或持续同步 WorkBuddy credential 目录。失败、取消或超时不会切换。selector 与凭据文件都拒绝 symlink、宽权限和非当前用户 ownership，任何输出都不包含自定义路径或凭据。Wind 鉴权由 official CLI 自行判断，管道只映射脱敏错误码。

TDX MCP transport 固定使用官方 `mcp==2.0.0` SDK 的 Streamable HTTP。
每个 session 必须先通过 `initialize` 和本次请求 capability 的 `tools/list`
schema gate，才允许 `tools/call`；其它白名单 capability 的缺失或 schema drift
不会连带禁用本次能力，完整六项健康只能由后续 smoke/acceptance 分项验收。401 会强制 refresh、重建 session 并最多重试一次；403 直接报告
permission failure，不伪装成 expired。TDX 与 Wind 只允许固定只读工具白名单。
它们不是交易入口，不发交易 POST，不调用券商，也不能单独触发交易建议。

## Wind 显式研究增强

```python
from ym_stock_data import query

result = query(
    "wind_enrichment",
    capability="company_profile",
    code="600519.SH",
    params={"question": "公司主营业务", "lang": "中文"},
)
print(result["_meta"])
```

单次只允许一个标的；`code` 与 `codes` 不可同时提供，`codes` 最多一个。`top_k` 仅适用于 `announcements`。未知参数在调用 provider 前直接拒绝，不会静默丢弃。

## 兼容入口

V1 `fetch()` 和 V2 `resolve()` 仅为旧消费者保留的 compatibility wrapper，不再是推荐入口，也不拥有第二套路由。它们投影 canonical 结果并维持既有业务形状；暂未拥有 canonical intent 的旧 key 明确标记为 `legacy_direct`。在下游迁移和 side-by-side 证据完成前不承诺删除日期，且不会用强制 `DeprecationWarning` 破坏现有消费者。

仍待迁移的 production 消费者必须集中在一个 rollback switch 后；新代码不得直接 import `ym_stock_data.sources` 或 `ym_stock_data.v2`。

## 安装与验证

```bash
uv sync
uv run python -m compileall -q ym_stock_data scripts tests
uv run python -m unittest discover -s tests -v
git diff --check
```

不要使用系统 Python 的缺依赖结果判断供应商状态。pywencai 的锁文件依赖来自 `pyproject.toml` 与 `uv.lock`；运行时隔离环境由显式 setup 命令管理。

## 投研输出约定

在 `/Users/yimu/Documents/YM_Capital/YiMu_IR/` 做主题研究时，输出到 `outputs/`，保留数据快照、时间、入口与验证方式。研究观察不构成投资建议。
