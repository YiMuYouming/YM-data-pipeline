# 生产消费端统一入口清单 v3

日期：2026-09-23

本清单只记录当前代码接入事实。所有生产消费者均把 provider 选择、超时、质量门和降级交给
`YM-data-pipeline`；Python 公共入口为 `from ym_stock_data import query`，Agent/CLI 入口为
`./ym-data`。兼容 `fetch()` 只保留在 pipeline 包内部，不允许消费者借此选择 provider。

| 消费端 | 已接入能力 | 质量与失败行为 | 代码验收 |
| --- | --- | --- | --- |
| `Market_Watch/scripts/board_history.py` | `query("industry_flow", limit=100, use_case="realtime_poll")` | 要求可用状态且行业行数不少于 10；保留 pipeline meta/source；异常不写快照 | `tests.test_board_history` |
| `Market_Watch/scripts/local_query_specs.py` | `market_limit_state`；`query("legacy_hot_rank", trade_date=..., limit=200)` | 日期、涨停池计数与原因行均校验；生产默认不再直连 10jqka | `tests.test_local_query_specs` |
| `live-dashboard/scripts/collectors/quotes.py` | `stock_snapshot`、`realtime_market`、`review_sentiment`、`sector_index`、`northbound_flow`、`legacy_hot_rank`、`index_intraday_compare` | 实时行情统一带 `use_case=realtime_poll`；不完整/过期/空数据不覆盖缓存；15 分钟比较一次读取三指数标准结果 | `tests.test_quotes_collectors` |
| `live-dashboard/scripts/collectors/market_data.py` | `industry_flow`、`news` | 行业资金流使用实时 profile；错误或空集不写缓存 | `tests.test_market_data_collector` |
| `live-dashboard/scripts/collectors/iwencai_poll.py` | `review_sentiment`、`market_limit_state` | 只接受公共结果；计数和日期不完整时保留旧缓存 | 目标 collector 回归 |
| `live-dashboard/scripts/backfill_history.py` | `index_kline` 日线日期范围 | 三指数分别读取并校验可用状态；失败即停止该次回填，不写伪造 0 值 | `tests.test_backfill_pipeline_routes` |
| `live-dashboard/scripts/backfill_intraday.py` | 股票 `stock_kline` 与指数 `index_kline` 5 分钟日期范围 | pipeline 负责分页、日期过滤和 provider 降级；失败即停止该次回填 | `tests.test_backfill_pipeline_routes` |
| `live-dashboard/scripts/ops/refresh_close_baseline.py` | `realtime_market`、`review_sentiment`、`market_limit_state` | 三大指数或涨跌停结构不完整时拒绝生成可应用结果 | `tests.test_refresh_close_baseline` |
| `live-trading/shadow_trading/data_router.py` | `stock_snapshot`，仅 shadow/paper | 保存原始 `quote_time`、接收 `fetched_at`、`quality_status` 与 provider；旧行情不重标为当前行情 | `tests.test_shadow_trading` |
| `YiMu_IR/scripts/build_20260630_semiconductor_three_stock.py` | `stock_kline(adjustment="qfq")` | 明确要求 qfq；多取一根前置收盘后再计算首行涨跌幅/振幅，禁止未复权替代 | `tests.test_build_20260630_semiconductor_three_stock` |
| `YiMu_IR` 其余两份研究生成器 | `realtime_market`、`sector_index`、`stock_snapshot`、`review_sentiment`、`stock_kline` | 每个 intent 独立保留 meta/error；失败不伪造业务数据 | `py_compile` |

## 真实只读消费验收

- dashboard：quotes 在 PyTDX 时间过旧后自动使用腾讯；index 由 PyTDX direct 返回完整三指数；northbound、旧热榜、指数 15 分钟比较、行业资金流均经公共 intent 返回。
- Market Watch：行业表返回 90 行；2026-09-22 涨停原因返回 63 个代码。
- dashboard 回填：2026-09-21 至 2026-09-22 三指数日线各 2 行；2026-09-22 股票和指数 5 分钟线各 48 行，时间范围 09:35 至 15:00。
- YiMu_IR：前复权 helper 返回目标 2 行，首行及后续行的涨跌幅、振幅和涨跌额均可计算。
- shadow：600519 返回腾讯降级行情，同时保留原始行情时间、接收时间和 `partial` 质量状态。

## 运行状态边界

代码已位于 Projects checkout（Documents 路径解析到同一目录），但监听 8088 的进程是到远端服务的
SSH 隧道，监听 18088 的本地代理也是既有长生命周期进程。本轮未同步远端、未重启、未部署、未发
8088 POST，也未接入下单。运行服务切换必须单独执行并回读验证。
