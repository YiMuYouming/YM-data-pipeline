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
```

## V2 调用入口

V2 旁路入口用于 Agent 验证、投研查询、红方草稿和方案调试。调用前优先使用项目环境，避免系统 Python 缺依赖造成误判：

```bash
cd /Users/yimu/Documents/YM_Capital/YM-data-pipeline
uv run python - <<'PY'
from ym_stock_data.v2.resolve import resolve

print(resolve("realtime_market")["_meta"])
print(resolve("sector_index", names=["半导体"])["_meta"])
print(resolve("stock_snapshot", codes=["603290", "688187"])["_meta"])
print(resolve("stock_kline", code="603290", period="daily", count=20)["_meta"])
print(resolve("review_sentiment", query="A股 IGBT 概念股 非ST 总市值 PE PB", limit=20)["_meta"])
print(resolve("market_limit_state")["_meta"])
print(resolve("stock_event", event="lockup", code="600519")["_meta"])
PY
```

当前 V2 支持的 intent：

| intent | 用途 | 示例 |
| --- | --- | --- |
| `realtime_market` | 三大指数、成交额、涨跌家数 | `resolve("realtime_market")` |
| `sector_index` | 同花顺 881 行业板块 | `resolve("sector_index", names=["半导体"])` |
| `stock_snapshot` | 个股实时行情/均线快照 | `resolve("stock_snapshot", codes=["603290"])` |
| `stock_kline` | 个股 K 线/均线 | `resolve("stock_kline", code="603290", period="daily", count=20)` |
| `review_sentiment` | 问财 query 执行与复盘情绪聚合 | `resolve("review_sentiment", query="涨停 非ST", limit=20)` |
| `market_limit_state` | 涨停/炸板/跌停池聚合（旁路实验） | `resolve("market_limit_state")` |
| `stock_event` | 个股低频事件（旁路实验） | `resolve("stock_event", event="lockup", code="600519")` |

额度规则：`resolve("review_sentiment")` 不传 `query` 时默认先走 PyTDX breadth，不调用问财；PyTDX breadth 不可用时最多降级为 1 次问财。显式字符串 `query=...` 才执行单次自然语言查询；只有显式列表 `query=[...]` 才允许批量问财。成功的 OpenAPI 结果默认缓存 300 秒，可通过 `IWENCAI_QUERY_CACHE_TTL=0..1800` 调整。

`market_limit_state` 与 `stock_event` 当前均为 `experimental` 旁路能力，只用于研究和连续五个交易日对账；未通过 Gate 3 前不改变任何现有消费者路由。

注意：旧 flat 入口 `fetch("iwencai", query="...")` 当前可能因参数名兼容问题报 `query() got an unexpected keyword argument 'query'`。这不代表 V2 或问财源不可用。遇到该错误时，优先改用 `resolve("review_sentiment", query=...)`；若只需底层原始问财结果，可直接调用 `ym_stock_data.sources.iwencai.query("...", limit=...)`。

### TDX MCP 备用源

当问财 OpenAPI + pywencai 都失败、额度耗尽、返回空结果或关键字段异常时，可以使用 Codex MCP `tdx-finance` 做备用查询和交叉验证。它是授权型增强源，不是 V2 自动主源；不要把 TDX MCP 结果写成 `resolve(...)` 的结果。

常用工具：

| 工具 | 用途 |
| --- | --- |
| `tdx_screener` | 自然语言条件选股；问财挂了时优先用它补主题/板块候选 |
| `tdx_quotes` | 个股实时行情、换手率、成交额、盘口、PE、市值等细节 |
| `tdx_kline` | K 线交叉校验 |
| `tdx_lookup_stock` | 名称查代码和 setcode |
| `wenda_report_query` | 研报补充 |
| `wenda_notice_query` | 公告补充 |
| `wenda_news_query` | 新闻/题材催化补充 |

硬规则：

- 主流程仍优先使用 `YM-data-pipeline` 的本地 V2/V1 管道。
- 使用 TDX MCP 时，输出必须标注 `source=tdx_mcp` 和查询时间。
- 如果 `tdx-finance` 未暴露工具、`tools/list` 失败、token 过期、HTTP 401/400、或 WorkBuddy OAuth 缓存失效，必须直接告诉弈沐需要重新授权；禁止猜测、补齐或基于旧缓存下结论。
- TDX MCP 只补“宽度”和“细节”，不能单独触发交易建议。
- TDX MCP 不由 `fetch()`/`resolve()` 自动调用；只有完成 20 例与连续 5 个交易日对账，才讨论把个别字段从 `cross_check_only` 提升为 `fallback_candidate`，本轮不执行提升。

## 投研输出约定

- 在 `/Users/yimu/Documents/YM_Capital/YiMu_IR/` 做 A 股主题研究时，输出文件放到 `outputs/`。
- 长报告默认做单文件可读 HTML，文件名使用中文主题名，例如 `outputs/IGBT功率半导体A股核心股票观察研报.html`。
- 查询得到的行情/估值/候选池快照保留为 JSON，例如 `outputs/igbt_iwencai_snapshot_20260626.json`。
- 最终汇报必须说明数据快照时间、使用入口、验证命令或静态校验结果，并标注研究观察不构成投资建议。

## 安装

```bash
cd YM-data-pipeline
pip install -e .              # 基础安装（PyTDX/requests/akshare）
pip install -e .[pywencai]    # 启用问财 pywencai 降级能力
```

## 数据源降级策略

| 源 | 优先 | 降级 | 说明 |
|------|------|------|------|
| PyTDX | TCP 长连接 + 业务探针 | 指数：东财→腾讯；报价：腾讯→easyquotation；K线：腾讯/新浪 | 零鉴权；握手成功但业务空数据视为坏节点并自动轮换 |
| 问财 | OpenAPI | pywencai 网页抓取 → TDX MCP 人工备用 | OpenAPI 额度耗尽自动切 pywencai；两者都挂时才用授权型 TDX MCP |
| 腾讯 | HTTP API | — | PE/PB 等财务数据 |

问财降级自动进行：OpenAPI 401/403/429 → 300 秒 breaker；5xx、网络、超时或无效响应 → 60 秒 breaker；breaker 期间由带失败缓存的 pywencai 接管，到期后再试 OpenAPI。成功的 OpenAPI 查询另有 300 秒进程内结果缓存，重复请求不会再次消耗额度。

TDX MCP 不自动接入代码降级链；它用于 Agent 开线策划、投研筛选和手工交叉验证。需要授权时向弈沐确认，不要自行臆测结果。

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
IWENCAI_QUERY_CACHE_TTL = 300      # OpenAPI 成功结果缓存秒数；0 关闭，最大 1800
PYWENCAI_VENV                     # pywencai 运行环境路径
```

## 问财 API Key

IWENCAI_API_KEY 读取优先级：环境变量 → ~/.zshrc → ~/.bash_profile → ~/.bashrc

## 线程安全

所有 PyTDX 调用受 `threading.Lock` 保护，多个 collector 线程可安全共享。详见 `sources/pytdx.py` 的 `_get_api()`。
节点连接后必须通过轻量报价探针才会进入连接池；全池失败会短期熔断，避免每个 V2 intent 重复等待。V2 fallback 的顶层 `source` 和 `source_chain` 必须反映实际供应商，不能把腾讯/新浪降级结果继续标成 PyTDX。

## 所属项目

- 代码: `~/Documents/YM_Capital/YM-data-pipeline/`
- 被 live-dashboard bridge.py、WorkBuddy 脚本 import
- live-dashboard AGENTS.md: `~/Documents/YM_Capital/live-dashboard/AGENTS.md`
