# YM-data-pipeline V3 消费端能力冻结

**目标：** 在修改任何消费端前，把 14 个真实 production bypass 所需的数据语义收回 `ym_stock_data.query()`，保证迁移不改口径、不隐藏空集、不把 fallback 报成正常。

## 已确认的契约缺口

| 消费端 | 现有语义 | V3 必须补齐 | 验收边界 |
| --- | --- | --- | --- |
| `Market_Watch/scripts/board_history.py` | 同花顺二级行业全量日快照，含涨跌幅、净流入、上下涨家数和成交额 | `sector_index` 显式 `universe=all`，不得用空 `codes/names` 暗示全量 | 至少 10 行；taxonomy 和字段单位固定；全量失败不得回空表写入历史 |
| `Market_Watch/scripts/local_query_specs.py` | 东财涨停池主体 + 同花顺 `reason_type` 失败软降级 | 新的显式涨停原因 capability/intent，供应者和东财池分开记 provenance | 日期必须命中；reason 缺失只降级字段，不伪造原因；不在 consumer 保留 vendor URL |
| `YiMu_IR` 三个报告构建器 | 日 K、实时行情、板块、研究来源；技术指标使用东财前复权 | `stock_kline` 显式 `adjust=none|qfq|hfq`、日期区间与字段契约 | 不能用未复权数据静默替代 qfq；报告引用与市场数据 provenance 分开 |
| `live-dashboard/scripts/backfill_history.py` | 三大指数日 K，按请求历史日期回填 | 独立 `index_kline` 契约，含 index code/market、period、date range/pagination | 不运行真实 DB 回填验收；用 fixture 保证日期、OHLC 与缺日行为 |
| `live-dashboard/scripts/backfill_intraday.py` | 个股与指数 5 分钟线，需要可分页历史区间 | `stock_kline`/`index_kline` 显式 5m 日期区间和完整性元数据 | 不得把最近 N 条冒充完整区间；截断/分页未证明时 fail closed |
| `live-dashboard/scripts/collectors/quotes.py` | 实时报价、1 分钟 trend、15 分钟 K、部分直连 PyTDX | `stock_snapshot`、`stock_kline(period=1m|15m)` 与字段/age/provenance 契约 | 实时与历史条分开验收；旧 provider fallback 只能放在一个默认关闭的 rollback switch 后 |
| `live-dashboard` 其他 collector/bridge/config | 消费端自己声明 provider 顺序和 fallback label | provider 顺序只在 pipeline policy；consumer 只读公共 contract | UI/internal API 读不当 provider；四个 protected write boundary 不改语义 |
| `live-trading/shadow_trading/data_router.py` | legacy `fetch()` + 腾讯二次补字段 + dashboard local fallback | 单次 `query("stock_snapshot")` 提供完整 shape；local dashboard 只作显式 rollback | 仅 shadow/paper，不连券商、不写生产事实；fallback 必须显式降级 |

## 实施顺序

1. 用 TDD 在 pipeline 补齐上述 public intent/params/shape，并证明旧 contract 1.0 兼容。
2. 用 provider-smoke V3 对每个新 capability 做直测；没有当日通过证据的 capability 不进自动 route。
3. 按 `Market_Watch -> YiMu_IR -> live-trading -> live-dashboard` 逐仓库迁移；每个消费端先做 side-by-side shape/provenance/empty/error overwrite guard。
4. 重跑 consumer audit；production bypass 必须归零，protected write 保持 4/4，unknown/unreviewed ambiguity 必须为 0 才可冻结 Task 5。
5. 本阶段不部署、不重启、不运行回填写入、不提交/推送。
