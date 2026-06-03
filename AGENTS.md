# ym-stock-data — 弈沐资本 A 股数据管道

弈沐资本统一数据获取基座。零鉴权优先，多源自动降级。

## 一句话用法

```python
from ym_stock_data import fetch

# 实时行情
fetch("quotes", codes=["688017", "300476"])     # 个股报价+MA
fetch("index")                                   # 三大指数+涨跌家数
fetch("breadth")                                 # 全市场涨跌分布
fetch("sector_index", names=["算力", "CPO"])     # 板块指数
fetch("kline_15m")                               # 三大指数15分钟量价
fetch("northbound")                              # 北向资金实时
fetch("ths_hot")                                 # 同花顺热榜+题材
fetch("sector_inflow")                           # 行业板块净流入
fetch("dragon_tiger")                            # 龙虎榜

# 问财查询（auto-fallback）
fetch("iwencai", query="昨日涨停 今日涨跌幅")     # OpenAPI→pywencai 自动降级
```

## 安装

```bash
cd YM-data-pipeline
pip install -e .              # 基础安装（PyTDX/requests/akshare）
pip install -e .[pywencai]    # 启用问财 pywencai 降级能力
```

## 数据源降级策略

| 源 | 优先 | 降级 | 说明 |
|------|------|------|------|
| PyTDX | TCP 长连接 | easyquotation | 零鉴权，TCP 7709 端口 |
| 问财 | OpenAPI | pywencai 网页抓取 | OpenAPI 额度耗尽自动切 pywencai |
| 腾讯 | HTTP API | — | PE/PB 等财务数据 |

问财降级自动进行：OpenAPI 401/403/429 → 5min 内不走 OpenAPI → pywencai 接管 → 5min 后自动重试。

## 目录结构

```
ym_stock_data/
├── fetch.py          # 统一入口 fetch() — 路由到各源
├── config.py         # 服务器列表/超时/路径配置
├── sources/
│   ├── pytdx.py      # PyTDX 行情（个股/指数/K线/板块/涨跌分布）
│   ├── iwencai.py    # 问财 OpenAPI + pywencai 自动降级
│   ├── ths_hot.py    # 同花顺热榜
│   ├── northbound.py # 北向资金
│   ├── ths_industry.py # 行业板块净流入
│   ├── eastmoney.py  # 龙虎榜
│   ├── tencent.py    # 腾讯 PE/PB/市值
│   ├── research.py   # 个股研报
│   ├── filings.py    # 公司公告
│   └── news.py       # 实时新闻
└── __init__.py
```

## 配置（config.py）

```python
PYTDX_SERVERS = [(ip, 7709)]      # 通达信行情服务器
PYTDX_CONNECT_TIMEOUT = 5         # 连接超时（秒）
PYTDX_MAX_AGE = 60                # 连接复用时长（超时自动重连）
PYTDX_MAX_FAIL = 3                # 连续失败切换兜底
IWENCAI_API_KEY_PATH = ~/.zshrc   # 问财 API Key 读取路径
PYWENCAI_VENV                     # pywencai 运行环境路径
```

## 问财 API Key

IWENCAI_API_KEY 读取优先级：环境变量 → ~/.zshrc → ~/.bash_profile → ~/.bashrc

## 线程安全

所有 PyTDX 调用受 `threading.Lock` 保护，多个 collector 线程可安全共享。详见 `sources/pytdx.py` 的 `_get_api()`。

## 所属项目

- 代码: `~/Documents/YM_Capital/YM-data-pipeline/`
- 被 live-dashboard bridge.py、WorkBuddy 脚本 import
- live-dashboard AGENTS.md: `~/Documents/YM_Capital/live-dashboard/AGENTS.md`
