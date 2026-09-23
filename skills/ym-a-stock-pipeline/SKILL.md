---
name: ym-a-stock-pipeline
description: 弈沐资本 A 股统一数据入口；把行情、板块、筛选、K 线、研报及盘后每日涨跌停事实和短线派生指标映射到 public query() 或仓库根目录 ./ym-data。
metadata:
  short-description: A 股统一数据入口
---

# 弈沐资本 A 股统一数据入口

仓库 canonical checkout 是 `/Users/yimu/Projects/YM_Capital/YM-data-pipeline`。
Python 只使用 `from ym_stock_data import query`；命令行只使用仓库根目录的
`./ym-data`。路由、鉴权、质量判断和来源记录由数据管道负责，调用方只描述业务意图，
不得自行选择来源、串接降级或绕过公共入口。

## 意图映射

| 用户意图 | canonical intent | 参数提示 |
| --- | --- | --- |
| 市场全景、指数、涨跌家数 | `realtime_market` | 无 |
| 行业/板块 | `sector_index` | `names` 或 881 `codes` |
| 个股当前快照 | `stock_snapshot` | `codes` |
| 日/周/月/分钟 K 线 | `stock_kline` | `code`、`period`；可选日期范围与复权方式 |
| 指数历史 K 线 | `index_kline` | `index_code` 或 `codes`、`period`；可选日期范围 |
| 三大指数分钟比较 | `index_intraday_compare` | `period=5m/15m/60m`；可选 `trade_date` |
| 行业资金流 | `industry_flow` | 可选 `trade_date`、`limit` |
| 市场资金流 | `fund_flow` | 可选 `trade_date`、`limit` |
| 北向资金 | `northbound_flow` | 可选 `trade_date`、`limit`；仅作辅助参考 |
| 热榜与题材归因 | `legacy_hot_rank` | 可选 `trade_date`、`limit` |
| 涨停、跌停、炸板、昨日涨停明细 | `market_limit_board` | `kind=up/down/broken/yesterday`；可选 `date` |
| 同花顺/东财热榜 | `market_hot_rank` | `source=ths/dc`；可选 `trade_date`、`limit` |
| 涨跌停状态聚合 | `market_limit_state` | 兼容查询；新请求优先使用明细 intent |
| 已封存交易日的晋级率与短线收益 | `market_facts` | 可选 `trade_date=YYYYMMDD`；逐项检查缺口和来源时间 |
| 个股低频事件 | `stock_event` | 只传受支持的事件参数 |
| 热度、问财型筛选 | `review_sentiment` | 把用户原始筛选写入 `query` |
| 研报、公告、新闻 | `research`、`filings`、`news` | 只传 canonical 参数 |
| 显式 StockToday 数据集 | `stocktoday_data` | `api_name`、`params`；可选 `fields` |
| 显式 Wind 研究增强 | `wind_enrichment` | 仅用于研究增强，不改变行情路由 |

先将口语请求映射为一个 intent 和最小参数集，不擅自补充筛选条件。复杂研究拆成多个
独立查询，并分别保留结果、时间和质量信息。

## 每日事实与弈沐指标

用户问“今日情绪、昨日涨停/连板/炸板今日收益、实际晋级率”时，用
`query("market_facts", trade_date="YYYYMMDD")`；无日期按上海时间和交易所日历
选最近已完成交易日。此意图只读运行环境的事实库；该日未采集或缺库时显式失败，
不得以旧日期结果回答今日问题。`./ym-data market-facts report --date YYYYMMDD`
仍是等价的本机只读诊断命令。

| 问法 | 报告字段 | 必须检查 |
| --- | --- | --- |
| 昨日涨停今日平均收益 | `yesterday_limit_up_return_pct` | `return_cohort_counts.yesterday_limit_up` 与对应 `return_evidence` |
| 昨日二板及以上今日平均收益 | `yesterday_consecutive_return_pct` | 历史板数未经核实时值为空 |
| 昨日炸板今日平均收益 | `yesterday_broken_return_pct` | 昨日炸板完整名单与今日全部报价覆盖 |
| 次日晋级率 | `promotion_overall_by_code`；分层看 `promotion.rates` | 同源相邻日名单；板数冲突时分层为空 |
| 市场情绪 | `yimu_emotion` | 分母是当日有日线的股票，**不是**全部上市股票；`ths_emotion_equivalent` 仍为空 |
| 连板股三日风险 | `consecutive_break_risk` | 当前为空；定义、复权价格和一年窗口尚未完成 |

每项先看 `source_gaps`、`limit_daily_quality` 和原始 `fetched_at`；缺报价、停牌、
历史榜单与日线冲突时只报告缺口，不补零。精确同花顺热榜仍用
`market_hot_rank(source=ths)`，不可由这些涨跌幅指标伪造。

## 公共入口示例

```bash
cd /Users/yimu/Projects/YM_Capital/YM-data-pipeline
./ym-data query stock_snapshot 'codes=["600519","000001"]'
./ym-data query review_sentiment 'query="连续涨停 非ST"' limit=20
./ym-data doctor --json
```

```python
from ym_stock_data import query

result = query("stock_kline", code="600519", period="daily", count=20)
print(result["_meta"])
```

上面 `query` / `intent` CLI 与 Python `query()` 是同一公共契约；`market-facts` 子命令保留为采集和只读诊断。Agent 只传业务参数；不静默重试、不拼接第二条
数据链，也不补造缺失数据。

## K 线与日期

K 线检查 `datetime`、`volume_unit=share`、`amount_unit=CNY` 和 `adjustment`。
`stock_kline` 的复权方式只能为 `none` 或 `qfq`；分钟 K 线不接受 `qfq`。
`index_kline` 当前只接受 `adjustment=none`。日期可写 `YYYYMMDD` 或
`YYYY-MM-DD`，进入管道后统一为 `YYYYMMDD` 并按闭区间筛选。不要自行分页或转换成交量、
成交额口径。批量 `codes` 查询只在全部指数均返回有效 K 线时成功；部分结果由管道继续降级。

## 高频短语

自然语言只接受已登记的精确短语；registry 固定入口、参数名和必填字段，不根据相似文字
自由猜接口。使用 CLI：

| 精确短语 | 固定入口 | 必填参数 |
| --- | --- | --- |
| `查涨停板` / `查跌停板` | `market_limit_board`，固定 `kind=up/down` | 无；可选 `date` |
| `查同花顺热榜` | `market_hot_rank`，固定 `source=ths` | 无；可选 `trade_date`、`limit` |
| `查东财热榜` | `market_hot_rank`，固定 `source=dc` | 无；可选 `trade_date`、`limit` |
| `查实时大盘` | `realtime_market` | 无 |
| `查实时个股` | `stock_snapshot` | `codes` |
| `查日K` / `查周K` / `查月K` | `stock_kline`，固定周期 | `code` |
| `查前复权日K` | `stock_kline`，固定 `period=daily`、`adjustment=qfq` | `code` |
| `查分钟K` | `stock_kline` | `code`、`period=5m/15m/60m` |
| `查板块` / `查行业` | `sector_index` | `names` 或 881 `codes` |

```bash
./ym-data intent "查涨停板" 'trade_date="20260923"'
./ym-data intent "查同花顺热榜"
./ym-data intent "查实时个股" 'codes=["600519"]'
./ym-data intent "查分钟K" 'code="600519"' 'period="15m"' 'count=3'
```

无日期的热榜查询按上交所已确认交易日历选择最近已完成交易日（上海时间 15:00
收盘后才选当天），以返回的 `trade_date` 判断数据日期，不自行将盘中缺失解释为零。
当前日历覆盖 2026 年；超出覆盖范围会明确报 `CALENDAR_UNAVAILABLE`，此时需显式
传入 `trade_date`，不得按周一至周五自行猜测。

## StockToday 长尾接口

高频 intent 未覆盖的 StockToday 请求，先使用本地、无凭据、无网络的 catalog：

```bash
./ym-data stocktoday catalog --json
./ym-data stocktoday catalog ths_hot --json
```

确认 catalog 返回的 `name` 与参数后，才可通过 `query("stocktoday_data", ...)` 显式查询。
不得自由猜接口名或把长尾接口拼入 canonical 路由。

## 结果与质量

每次查询都检查 `_meta.status`、`provider_used`、`source_tier`、完整 `attempts`、
`fetched_at` 与 `quality`。同时保留 `pipeline_version`、`route_policy_version` 和
`policy_evidence_sha256`；`source_tier` 用 `primary`、`fallback` 或 `explicit` 区分来源
角色。保留 `quality.reason_codes`、`missing`、coverage 和 `source_gap`。合法空集、来源
错误、缺少依赖、降级成功和未验证状态不能混为一谈；字段、时效或完整性不足时，不补零、
不猜测，也不把空结果说成事实。

## 边界

- 不直接导入来源实现、私有模块或供应商 SDK。
- 不在调用方实现来源降级、来源白名单、重试预算或路由顺序。
- 不把凭据、Token、Key 或任意 URL 放入命令参数、日志、Skill 或代码。
- 旧兼容入口仅供尚未迁移的消费者使用，不属于 Agent 的查询入口。
- 数据查询不授予交易授权；不调用交易或券商写接口，不向真实 8088 发 POST。

遇到未覆盖的请求，停在公共契约并报告不支持的参数或 `source_gap`，不要自行寻找旁路。
