# YM-data-pipeline v2.0

弈沐资本 A 股统一数据管道。为看板/复盘/投研提供统一数据基座。

## v2.0 治理方案

v2.0 的目标是从“统一数据源路由”升级为“统一入口、统一策略、统一口径、按场景调用”。详细方案见：

- [YM-data-pipeline-2.0-数据源治理方案.md](docs/YM-data-pipeline-2.0-数据源治理方案.md)

## 用法

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
│   ├── utils/                 # 缓存 + 重试
│   └── consumer/              # 看板适配器
├── tests/                     # 17 项测试
└── scripts/compare.py         # 新老系统对比
```
