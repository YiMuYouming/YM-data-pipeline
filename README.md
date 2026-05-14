# ym-stock-data

弈沐资本 A 股统一数据平台。为看板/复盘/投研/未来交易所提供统一数据基座。

## 用法

```python
from ym_stock_data import fetch

# L2 技术分析 (PyTDX TCP 长连接)
fetch("quotes", codes=["688017"])          # 个股实时报价
fetch("index")                              # 三大指数
fetch("breadth")                            # 涨跌分布
fetch("sector_index", names=["算力"])       # 板块指数880xxx
fetch("kline", code="688017", period="daily")   # K线+均线
fetch("kline_15m")                          # 15min量价

# L1 基础行情
fetch("iwencai", query="涨停 非st")         # 问财全能查询
fetch("ths_hot")                            # 同花顺热点+题材归因
fetch("tencent", codes=["688017"])          # PE/PB/市值

# L3 资金流向
fetch("northbound")                         # 北向资金分钟级
fetch("dragon_tiger")                       # 龙虎榜
fetch("sector_inflow", top_n=20)            # 行业板块净流入
```

## 架构

- **L0 复盘基线**: gen_dashboard_data.py（不动）
- **L1 基础行情**: 腾讯PE/PB / 同花顺热点 / 问财
- **L2 技术分析**: PyTDX TCP 连接（5s-30s 实时）
- **L3 资金流向**: 北向 / 龙虎榜 / 行业净流入
- **L4 研报/公告/新闻**: （建设中）

## 数据源

| 数据源 | 协议 | 鉴权 | 稳定性 |
|---|---|---|---|
| PyTDX (通达信) | TCP 7709 | 无 | 极高 |
| 问财 OpenAPI | HTTP POST | API KEY | 高 |
| 同花顺热点 | HTTP GET | 无 | 极高 |
| 腾讯财经 | HTTP GET | 无 | 极高 |
| 东财龙虎榜 | HTTP GET | 无(需Referer) | 高 |
| 北向资金(同花顺) | HTTP GET | 无 | 极高 |
| 行业板块(同花顺) | HTTP GET | 无 | 高 |

## 安装

```bash
pip install -e ym-stock-data
```

## 验证标准

```
fetch("index")        → 三大指数
fetch("quotes")       → 个股报价/涨幅/量比
fetch("ths_hot")      → 80+只含reason
fetch("tencent")      → PE/PB/市值
fetch("northbound")   → 262分钟点
fetch("sector_inflow") → 50个行业含净流入
fetch("iwencai")      → A股全字段查询
```
