# Wind MCP 实验补充源 Handoff

原始记录：2026-07-22；迁移更新：2026-07-29

项目：`/Users/yimu/Documents/YM_Capital/YM-data-pipeline`

当前状态：历史 sidecar 已在 Task 10 完成 parity，迁移到正式 provider registry；待本次提交

## 一句话结论

Wind MCP 已从独立 sidecar 迁移为受治理 provider。显式研究调用统一走
`ym_stock_data.query("wind_enrichment", ...)`；自动兼容降级只开放 `filings`，并严格排在
巨潮与 TDX 公告之后。实时行情、K 线、分钟数据、新闻、泛选股与 `stock_event` 均不接 Wind。

弈沐的明确要求：**让 Wind 做补充，不要轻易改动现有通道。**

## 已完成

1. 新增 `ym_stock_data.query("wind_enrichment", ...)` 正式显式入口；删除无外部引用的旧 sidecar。
2. 只开放七类非实时补充能力：
   - `company_profile`
   - `fundamentals`
   - `equity_holders`
   - `company_events`
   - `risk_metrics`
   - `index_fundamentals`
   - `announcements`
3. 明确排除实时行情、K 线、分钟行情、选股、新闻和通用 analytics。
4. capability manifest 从实际 registry/routes 派生：
   - `status=registered_experimental`
   - `automatic_fallback_intents=[filings]`
   - `default_route=false`
5. Key 只由 Wind 官方 CLI 从既有安全配置读取，不进入 Python 参数、命令行、结果或本文档。
6. 认证错误、超时、CLI 缺失、无效 JSON、Wind 数据包内部错误均返回固定枚举；不透传 stderr、payload message、agent action 或异常正文。
7. 公告结果只有明确 `filings: list` 才能成功/有效空集；公司事件各 subtype 未证明等价，因此不注册自动 fallback。
8. 保留 Gate 1-3 验证与晋升清单。

## 本轮文件

2026-07-22 原始新增（2026-07-29 已迁移/删除）：

- `ym_stock_data/experimental/__init__.py`
- `ym_stock_data/experimental/wind_sidecar.py`
- `tests/test_wind_sidecar.py`
- `docs/Wind-MCP-补充源验证清单.md`
- `docs/handoffs/2026-07-22-wind-mcp-sidecar-handoff.md`

修改：

- `ym_stock_data/v2/capabilities.py`
- `tests/test_v2_capabilities.py`
- `README.md`

未修改：

- `ym_stock_data/fetch.py`
- `ym_stock_data/v2/resolve.py`
- live-dashboard、Market Watch、Portal 和任何生产消费者

## 当前调用方式

```bash
uv run python - <<'PY'
from ym_stock_data import query

result = query(
    "wind_enrichment",
    capability="fundamentals",
    params={"question": "600519.SH 2025年ROE和净利润增速"},
)
print(result["_meta"])
PY
```

成功结果必须保留这些标识：

```text
_meta.provider_used=wind_mcp
_meta.status=success
_meta.contract_version=1.0
```

## 已完成验证

2026-07-22 sidecar 历史完整验证结果：

```text
Ran 127 tests in 0.140s
OK
test_exit=0
core_routes_unchanged=yes
wind_boundary_check=ok
```

同时完成：

- `uv run python -m compileall -q ym_stock_data tests`
- `git diff --check`
- 新文件尾随空白扫描
- capability manifest 边界断言
- `SUPPORTED_INTENTS` 中无任何 Wind intent

真实小额探针：

- 时间：2026-07-22 16:31（Asia/Shanghai）
- capability：`company_profile`
- 问题：`600519.SH公司基本档案`
- 结果：认证成功，Wind 返回非空结构化数据，内层 `error=None`
- 未保存原始数据快照，未调用实时行情或 K 线

## 尚未完成

Gate 2：20 例字段对账尚未开始。

- 财务指标 6 例
- 股东/股本 3 例
- 公司事件 3 例
- 指数估值 3 例
- 公告检索 3 例
- 风险指标 2 例

Gate 3：连续 5 个交易日稳定性观察尚未开始。

- 每日至少一例真实调用
- 记录成功率、P50/P95 延迟、空结果率、错误码和积分消耗
- 快照必须放在非 Git 目录

在 Gate 2、Gate 3 完成前，不讨论公告之外的任何自动 fallback 晋升。

## 下一空间接续建议

优先顺序：

1. 先检查当前未提交 diff，确认只包含本 handoff 列出的文件。
2. 再决定是先做 Gate 2 对账，还是让 YiMu_IR 研究编排显式使用 canonical query。
3. 若更新 YiMu_IR，只在需要财务、股东、公司事件、指数估值或公告补证时显式调用。
4. Wind 与现有主源冲突时保留冲突，检查报告期、单位、币种、复权、合并口径和更新时间，并回到交易所/公告/公司披露核验。
5. 不把 Wind Alice 接入确定性数据路由；Alice 继续留在 IR 做分析、归因和反方审查。

可复制给下一空间的任务说明：

```text
继续 YM-data-pipeline 的 Wind MCP 实验补充源工作。先读
/Users/yimu/Documents/YM_Capital/YM-data-pipeline/docs/handoffs/2026-07-22-wind-mcp-sidecar-handoff.md
和 docs/Wind-MCP-补充源验证清单.md，检查 git status 与现有 diff。保持 Wind 为
registered_experimental，只允许显式 wind_enrichment 与严格 filings fallback，不接实时行情、
K线、分钟、新闻、泛选股或 stock_event。根据本次目标执行 Gate 2 字段对账，或在 YiMu_IR
中增加显式 canonical query，并重新跑全量测试与边界检查。
```

## 交接检查命令

```bash
cd /Users/yimu/Documents/YM_Capital/YM-data-pipeline
git status --short
git diff --check
uv run python -m unittest discover -s tests

uv run python - <<'PY'
from ym_stock_data.v2 import capability_manifest
wind = capability_manifest()["providers"]["wind_mcp"]
print(wind)
PY
```

预期：全量测试通过，Wind manifest 的 routes 只含 `filings` 与 `wind_enrichment`，且
`default_route=false`。
