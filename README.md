# YM-data-pipeline v2.0

弈沐资本 A 股统一数据管道。为看板/复盘/投研提供统一数据基座。

## v2.0 治理方案

v2.0 的目标是从“统一数据源路由”升级为“统一入口、统一策略、统一口径、按场景调用”。详细方案见：

- [YM-data-pipeline-2.0-数据源治理方案.md](docs/YM-data-pipeline-2.0-数据源治理方案.md)
- [YM-data-pipeline-v2.0-MVP-试运行记录.md](docs/YM-data-pipeline-v2.0-MVP-试运行记录.md)

## 用法

### v1 生产入口

```python
from ym_stock_data import fetch

# L2 技术分析 (PyTDX TCP 长连接)
fetch("quotes", codes=["688017"])          # 个股实时报价
fetch("index")                              # 三大指数
fetch("breadth")                            # 涨跌分布
fetch("sector_index", names=["算力"])       # 板块指数
fetch("kline", code="688017", period="daily")

# L1 基础行情
fetch("iwencai", query="涨停 非st")         # 问财全能查询
fetch("ths_hot")                            # 同花顺热点+题材归因
fetch("tencent", codes=["688017"])          # PE/PB/市值

# L3 资金流向
fetch("northbound")                         # 北向资金分钟级
fetch("dragon_tiger")                       # 龙虎榜
fetch("sector_inflow", top_n=20)            # 行业板块净流入
```

### v2.0 MVP 旁路入口

v2.0 新增 `ym_stock_data.v2.resolve()`，当前只用于 Agent 验证、红方草稿和方案调试，不切 live-dashboard、不切正式复盘、不用于盘中交易决策。

```python
from ym_stock_data.v2 import resolve

resolve("realtime_market")    # 直连 sources.pytdx.fetch_index，补 source_chain/data_scope/staleness
resolve("stock_snapshot", codes=["002475", "002281"])  # 直连 sources.pytdx.fetch_quotes
resolve("stock_kline", code="002475", period="daily")  # 直连 sources.pytdx.fetch_kline，返回 bars/MA
resolve("review_sentiment")   # 按 fields.json 去重执行复盘情绪问财模板
```

边界：
- 生产脚本继续使用 `from ym_stock_data import fetch`。
- v2 直接复用 `sources/*`，不再经过 v1 `fetch()` 路由。
- v2 返回统一 `_meta`，包含 `source_chain`、`data_scope`、`fetched_at`、`confidence`。
- 超过字段 `staleness_sec` 的数据会标注 `confidence: "stale"`。
- `stock_snapshot` 当前只承诺 v1 `quotes` 已有字段，不承诺 MACD 和资金流。
- `stock_kline` 当前使用 PyTDX K 线源，支持 `daily` / `weekly` / `monthly` / `60m` / `15m` / `5m`，TDX MCP 暂作为交叉校验和备源毕业候选。
- `review_sentiment` 默认批量执行字段策略里的问财 query；传入 `query=...` 时只执行单条 query，便于调试。
- v2 与 v1 冲突时，以当前 v1 生产链路为准。

## 架构（5 层）

| 层 | 内容 | 工具 |
|---|---|---|
| L0 | 复盘基线 | gen_dashboard_data.py（不动） |
| L1 | 基础行情 | 腾讯 PE/PB / 同花顺热点 / 问财 |
| L2 | 技术分析 | PyTDX TCP（5s-30s 实时） |
| L3 | 资金流向 | 北向 / 龙虎榜 / 行业净流入 |
| L4 | 研报/公告/新闻 | 东财 reportapi / 巨潮 / 财联社 |

## 数据源（10 个，全部通过验证）

| 数据源 | 协议 | 鉴权 | 文件 |
|--------|------|------|------|
| PyTDX (通达信) | TCP 7709 | 无 | `sources/pytdx.py` |
| 问财 OpenAPI | HTTP POST | API KEY | `sources/iwencai.py` |
| 同花顺热点 | HTTP GET | 无 | `sources/ths_hot.py` |
| 腾讯财经 | HTTP GET | 无 | `sources/tencent.py` |
| 东财龙虎榜 | HTTP GET | Referer | `sources/eastmoney.py` |
| 北向资金(同花顺) | HTTP GET | 无 | `sources/northbound.py` |
| 行业板块(同花顺) | HTTP GET | 无 | `sources/ths_industry.py` |
| 东财研报 | HTTP GET | Referer | `sources/research.py` |
| 巨潮公告 | HTTP POST | 无 | `sources/filings.py` |
| 财联社新闻 | HTTP GET | 无 | `sources/news.py` |

## 安装

```bash
pip install -e ~/Documents/YM_Capital/YM-data-pipeline
```

## 测试

```bash
python3 tests/test_pytdx.py     # PyTDX 连接/报价/指数/K线
python3 tests/test_iwencai.py   # 问财查询/批量/热度
python3 tests/test_sources.py   # 7 个 HTTP 源端到端
```

## 本地

```
YM-data-pipeline/
├── ym_stock_data/             # Python 包 (23 文件, 2635 行)
│   ├── fetch.py               # 统一路由 → 15 种 data_type
│   ├── config.py              # 全局配置
│   ├── sources/               # 10 个数据源适配器
│   ├── v2/                    # v2.0 MVP 旁路 resolve()
│   ├── utils/                 # 缓存 + 重试
│   └── consumer/              # 看板适配器
├── tests/                     # 17 项测试
└── scripts/compare.py         # 新老系统对比
```
