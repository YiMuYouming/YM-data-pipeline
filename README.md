# ym-stock-data

弈沐资本 A 股统一数据通道。正式公共入口只有 `ym_stock_data.query()`；所有成功、合法空集和失败都返回 contract 1.0，并在 `_meta` 中保留真实 provider、attempt chain、质量与错误码。

## 快速开始

在项目环境中调用，避免把系统 Python 缺依赖误判为 provider 不可用：

```bash
cd /Users/yimu/Documents/YM_Capital/YM-data-pipeline
uv run python - <<'PY'
from ym_stock_data import query

print(query("realtime_market")["_meta"])
print(query("sector_index", names=["半导体"])["_meta"])
print(query("stock_snapshot", codes=["603290", "688187"])["_meta"])
print(query("stock_kline", code="603290", period="daily", count=20)["_meta"])
print(query("review_sentiment", query="A股 IGBT 概念股 非ST", limit=20)["_meta"])
PY
```

主要 intent：

| intent | 用途 | 关键参数 |
| --- | --- | --- |
| `realtime_market` | 指数、成交额、涨跌家数 | 无 |
| `sector_index` | 行业板块 | `names` / `codes` |
| `stock_snapshot` | 个股行情与均线快照 | `codes` |
| `stock_kline` | 个股 K 线 | `code`, `period`, `count` |
| `review_sentiment` | 市场宽度或显式自然语言筛选 | `query`, `limit`, `page` |
| `market_limit_state` | 涨跌停池聚合 | 无 |
| `stock_event` | 个股低频事件 | `event`, `code` |
| `research` / `filings` / `news` | 研报、公告、新闻 | `code` 等 intent 参数 |
| `wind_enrichment` | 显式 Wind 研究增强 | `capability`, `code` / `codes`, `fields`, `params` |

先看公开能力和脱敏状态：

```bash
uv run ym-data list
uv run ym-data doctor --json
```

`doctor` 不联网验证数据业务，不打印 token、Key、异常正文或业务行。只有显式 `uv run ym-data smoke --live` 才运行只读在线探针；默认 smoke 不联网。

## 统一结果契约

每次 canonical 调用都返回：

```text
{
  "data": ...,
  "_meta": {
    "contract_version": "1.0",
    "status": "success | empty | degraded | error",
    "provider_used": "真实成功 provider，失败时为 null",
    "attempts": [{"provider": "...", "status": "...", "error_code": "..."}],
    "quality": {"status": "...", "returned_count": 0},
    "fetched_at": "带时区时间"
  }
}
```

仅语义有效的空集会终止路由；无效空响应、畸形 payload、鉴权失败或 route 外 provenance 会形成可审计 attempt，再尝试下一个语义兼容源。单元测试通过不等于 provider 在线，在线状态以当次只读 probe 为准。

## Provider ownership 与路由边界

| provider 类 | ownership | setup | doctor 状态 | intended capabilities | automatic fallback |
| --- | --- | --- | --- | --- | --- |
| PyTDX、东财、腾讯、新浪、同花顺、财联社、巨潮等本地/HTTP provider | 零鉴权 | 无 | `configured_unverified` 或明确错误 | 行情、板块、宽度、涨跌停、研报、公告、新闻等各自白名单能力 | 允许；只在 RouteSpec 中按语义兼容顺序降级 |
| 问财 OpenAPI | API key | 由既有安全环境提供，不打印配置值 | `configured_unverified`、breaker 或 auth 错误 | 显式 `review_sentiment` | 允许；失败后进入可移植 runtime，再进入兼容 TDX 能力 |
| pywencai | 可移植 runtime | `uv run ym-data setup pywencai` | `ready` / `dependency_missing` / `unavailable` | 显式 `review_sentiment` 兼容源 | 允许；只在 OpenAPI 失败后调用 |
| TDX MCP | owned OAuth | `uv run ym-data auth import-tdx --from-workbuddy` | 总状态与六能力分别为 `ready` / `auth_missing` / `auth_expired` | `tdx_screener→review_sentiment`、`tdx_quotes→stock_snapshot`、`tdx_kline→stock_kline`、研报、公告、新闻 | 允许；仅在零鉴权兼容源失败后，不进入 realtime/default breadth/sector |
| Wind MCP | official CLI | 由官方 CLI 按其配置优先级管理；管道不读取或复制 Key | `configured_unverified` / runtime 错误 | 显式 `wind_enrichment`，及严格验证后的 `filings` 兼容源 | 仅 `filings` 白名单 fallback；不接价格、K 线、分钟、新闻、泛选股或 `stock_event` |

`setup pywencai` 只有显式执行时才写 `~/.ym-stock-data`，固定使用 Python 3.12 兼容环境。`auth import-tdx --from-workbuddy` 只读取唯一明确候选并 fail closed；不会扫描整个 WorkBuddy，也不会输出凭据。Wind 鉴权由 official CLI 自行判断，管道只映射脱敏错误码。

TDX 与 Wind 只允许固定只读工具白名单。它们不是交易入口，不发交易 POST，不调用券商，也不能单独触发交易建议。

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
