# TDX MCP 备用源验证清单

TDX MCP 只用于 Agent 人工备用与交叉验证。它不进入 Python 自动降级链，
不读取旧结果伪装当前事实，也不单独触发交易建议。

## 连接状态机

| 状态 | 判据 | Agent 行为 |
| --- | --- | --- |
| `ready` | `initialize`、`tools/list` 成功 | 可人工调用并标注 `source=tdx_mcp` |
| `auth_missing` | 找不到 WorkBuddy TDX credentials | 请弈沐在 WorkBuddy 重新登录 |
| `token_expired` | refresh 失败或 401 | 停止调用，不基于旧缓存回答 |
| `session_invalid` | `No valid session ID provided` | 重启 stdio wrapper，重新 initialize |
| `tool_unavailable` | tools/list 无目标工具 | 降级回本地管道或问财，不猜结果 |

## 20 例对账

每例必须记录查询时间、参数、本地基准源、TDX MCP 字段、差异解释、
以及是否满足交叉验证用途。未填写的例子不算完成。

| # | 分组 | 样例 | 查询时间 | 参数 | 本地基准 | TDX 字段 | 差异解释 | 结论 |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `tdx_quotes` | 沪主板 | 待填 | 600519 | V2 `stock_snapshot` | 待填 | 待填 | 待验证 |
| 2 | `tdx_quotes` | 深主板 | 待填 | 000001 | V2 `stock_snapshot` | 待填 | 待填 | 待验证 |
| 3 | `tdx_quotes` | 创业板 | 待填 | 300750 | V2 `stock_snapshot` | 待填 | 待填 | 待验证 |
| 4 | `tdx_quotes` | 科创板 | 待填 | 688981 | V2 `stock_snapshot` | 待填 | 待填 | 待验证 |
| 5 | `tdx_kline` | 日线 | 待填 | 600519 daily | V2 `stock_kline` | 待填 | 待填 | 待验证 |
| 6 | `tdx_kline` | 周线 | 待填 | 600519 weekly | V2 `stock_kline` | 待填 | 待填 | 待验证 |
| 7 | `tdx_kline` | 15 分钟 | 待填 | 600519 15m | V2 `stock_kline` | 待填 | 待填 | 待验证 |
| 8 | `tdx_kline` | 60 分钟 | 待填 | 600519 60m | V2 `stock_kline` | 待填 | 待填 | 待验证 |
| 9 | `tdx_screener` | 主题 | 待填 | 机器人概念 | `review_sentiment` | 待填 | 待填 | 待验证 |
| 10 | `tdx_screener` | 估值 | 待填 | 低 PE/PB | `review_sentiment` | 待填 | 待填 | 待验证 |
| 11 | `tdx_screener` | 涨停 | 待填 | 今日涨停 | `market_limit_state` | 待填 | 待填 | 待验证 |
| 12 | `tdx_screener` | 非 ST 组合 | 待填 | 主题+估值+非 ST | `review_sentiment` | 待填 | 待填 | 待验证 |
| 13 | `wenda_report_query` | 个股研报 | 待填 | 600519 | `research` | 待填 | 待填 | 待验证 |
| 14 | `wenda_report_query` | 行业研报 | 待填 | 半导体 | `industry_research` | 待填 | 待填 | 待验证 |
| 15 | `wenda_notice_query` | 沪市公告 | 待填 | 600519 | `filings` | 待填 | 待填 | 待验证 |
| 16 | `wenda_notice_query` | 深市公告 | 待填 | 000001 | `filings` | 待填 | 待填 | 待验证 |
| 17 | `wenda_news_query` | 公司新闻 | 待填 | 贵州茅台 | `iwencai_content` | 待填 | 待填 | 待验证 |
| 18 | `wenda_news_query` | 主题新闻 | 待填 | 机器人 | `iwencai_content` | 待填 | 待填 | 待验证 |
| 19 | `tdx_lookup_stock` | 名称查沪市代码 | 待填 | 贵州茅台 | 本地代码表 | 待填 | 待填 | 待验证 |
| 20 | `tdx_lookup_stock` | 名称查深市代码 | 待填 | 平安银行 | 本地代码表 | 待填 | 待填 | 待验证 |

## 晋升门禁

TDX MCP 仍是 Agent 人工备用源；不由 `fetch()`/`resolve()` 自动调用，
不读取其旧结果伪装本地 source，不因工具可用就提升为交易事实源。
只有完成上述 20 例并连续 5 个交易日对账后，才讨论在 `fields.json`
中把个别字段从 `cross_check_only` 提升为 `fallback_candidate`。
