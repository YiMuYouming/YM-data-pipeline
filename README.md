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
| `review_sentiment` | 市场宽度或显式自然语言筛选 | `query`, `limit`, `expected_row_shape`, `expected_count`, `date` |
| `market_limit_state` | 涨跌停池聚合 | 无 |
| `stock_event` | 个股低频事件 | `event`, `code` |
| `research` / `filings` / `news` | 研报、公告、新闻 | `code` 等 intent 参数 |
| `wind_enrichment` | 显式 Wind 研究增强 | `capability`, `code` / `codes`, `fields`, `params` |

先看公开能力和脱敏状态：

```bash
./ym-data list
./ym-data doctor --json
```

根目录 `./ym-data` 是正式 repo CLI 入口。它按项目绝对路径为每个 checkout/worktree 选择独立的 uv cache 外置环境；调用方显式设置 `UV_PROJECT_ENVIRONMENT` 时会保留该值。launcher 会选择实际通过 `--version` 探针的 uv；需要固定二进制时可显式设置绝对路径 `YM_DATA_UV_BIN`，无效 override 会直接失败而不降级。这样 macOS Documents File Provider 即使给项目内 dotpath 标记 hidden，也不会影响外置环境中的 editable `.pth`。路径和参数都按参数边界传递，不写入凭据。

`doctor` 不联网验证数据业务，不打印 token、Key、异常正文或业务行。只有显式 `./ym-data smoke --live` 才运行只读在线探针；默认 smoke 不联网。裸 `uv run ym-data ...` 是底层调用，只适用于不受 File Provider dotpath 影响的环境，不再作为正式 CLI 指引。

## 五日验收记录

盘后从离线 `./ym-data acceptance template --date YYYY-MM-DD` 开始。唯一字段契约、同日去重、一次性 live 命令、下游安全探针、build/validate 和自检步骤见 [`docs/ACCEPTANCE_RUNBOOK.md`](docs/ACCEPTANCE_RUNBOOK.md)；不要复制 schema 或自行补字段。

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

TDX route provider 只在所有排在其前的语义兼容源失败后调用；这既包括零鉴权源，也包括显式 `review_sentiment` 中的 OpenAPI 与 pywencai。`tdx_mcp` 只聚合诊断状态，不参与 RouteSpec。

| provider id | ownership / setup | doctor 状态 | intended capabilities / RouteSpec 次序 | automatic fallback |
| --- | --- | --- | --- | --- |
| `pytdx` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `realtime_market`、`stock_snapshot`、`stock_kline` 第一源 | 允许；失败后按对应 RouteSpec 继续 |
| `eastmoney` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `realtime_market` 第二源 | 允许；仅在 `pytdx` 失败后 |
| `tencent` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `realtime_market` 第三源；`stock_snapshot` 第二源；日周月 `stock_kline` 第二源 | 允许；只按上述 RouteSpec 次序 |
| `sina` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `stock_snapshot` 第三源；分钟 `stock_kline` 第二源 | 允许；只按上述 RouteSpec 次序 |
| `ths_industry` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `sector_index` 唯一源 | 否；当前无语义兼容后继源 |
| `pytdx_breadth` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | 默认 `review_sentiment` 第一源 | 允许；失败后进入 `eastmoney_breadth` |
| `eastmoney_breadth` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | 默认 `review_sentiment` 第二源 | 允许；仅在 `pytdx_breadth` 失败后 |
| `eastmoney_limit_pool` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `market_limit_state` 唯一源；默认 `review_sentiment` 第三源 | 仅作为默认情绪链末级 fallback；自身 intent 无后继源 |
| `eastmoney_datacenter` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `stock_event` 唯一源 | 否；当前无语义兼容后继源 |
| `eastmoney_research` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `research` 第一源 | 允许；失败后进入 `tdx_report` |
| `cninfo` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `filings` 第一源 | 允许；失败后进入 `tdx_notice` |
| `cls` | 零鉴权；无 setup | `configured_unverified` 或明确错误 | `news` 第一源 | 允许；失败后进入 `tdx_news` |
| `iwencai_openapi` | API key；由既有安全环境提供，不打印配置值 | `configured_unverified` / `breaker_open` / auth 错误 | 显式 `review_sentiment` 第一源 | 允许；失败后进入 `pywencai` |
| `pywencai` | 可移植 runtime；`./ym-data setup pywencai` | `configured_unverified` / `dependency_missing` / `unavailable` | 显式 `review_sentiment` 第二源 | 允许；仅在 `iwencai_openapi` 失败后 |
| `tdx_mcp` | owned OAuth；`./ym-data auth import-tdx --from-workbuddy` | TDX 总状态 `ready` / `auth_missing` / `auth_expired` | 诊断聚合，无 RouteSpec | 否；不执行业务查询 |
| `tdx_screener` | owned OAuth；同上 | 独立能力状态 | 显式 `review_sentiment` 第三源 | 允许；仅在 `iwencai_openapi`、`pywencai` 失败后 |
| `tdx_quotes` | owned OAuth；同上 | 独立能力状态 | `stock_snapshot` 第四源 | 允许；仅在 `pytdx`、`tencent`、`sina` 失败后 |
| `tdx_kline` | owned OAuth；同上 | 独立能力状态 | 日周月及分钟 `stock_kline` 第三源 | 允许；仅在对应周期前置兼容源失败后 |
| `tdx_report` | owned OAuth；同上 | 独立能力状态 | `research` 第二源 | 允许；仅在 `eastmoney_research` 失败后 |
| `tdx_notice` | owned OAuth；同上 | 独立能力状态 | `filings` 第二源 | 允许；仅在 `cninfo` 失败后 |
| `tdx_news` | owned OAuth；同上 | 独立能力状态 | `news` 第二源 | 允许；仅在 `cls` 失败后 |
| `wind_mcp` | official CLI；由 CLI 管理配置 | `configured_unverified` 或 runtime 错误 | 显式 `wind_enrichment` 唯一源 | 否；只响应显式调用 |
| `wind_documents` | official CLI；由 CLI 管理配置 | `configured_unverified` 或 runtime 错误 | `filings` 第三源 | 允许；仅在 `cninfo`、`tdx_notice` 失败后 |

`setup pywencai` 只有显式执行时才写 `~/.ym-stock-data`，固定使用 Python 3.12 兼容环境。setup 返回的 `ready` 仅表示 runtime installed，不是 doctor 在线状态，也不证明在线。`auth import-tdx --from-workbuddy` 只读取唯一明确候选并 fail closed；不会扫描整个 WorkBuddy，也不会输出凭据。Wind 鉴权由 official CLI 自行判断，管道只映射脱敏错误码。

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
