---
name: iwencai-data
description: 弈沐资本问财式 A 股自然语言筛选入口。仅在明确要求问财、自然语言选股或 review_sentiment 时使用；通过 YM-data-pipeline 公共 query() 查询，不自行选择或串接备用源。
metadata:
  short-description: 问财式 A 股自然语言筛选
---

# 问财式自然语言查询

此 Skill 供 Codex、Claude Code 和其他 Agent 共用。行情、热榜、涨跌停等有专用 intent 时，先按 `ym-a-stock-pipeline` 的能力目录调用专用入口；需要自然语言筛选时才使用 `review_sentiment`。

## 唯一调用入口

在 `/Users/yimu/Projects/YM_Capital/YM-data-pipeline` 中使用公共 API：

```python
from ym_stock_data import query

result = query("review_sentiment", query="今日热股人气排名前50", limit=50)
```

命令行诊断：

```sh
cd /Users/yimu/Projects/YM_Capital/YM-data-pipeline
./ym-data doctor --json
```

`doctor` 只报告脱敏配置，不证明线上接口可用。查询结果以 `_meta.status`、`provider_used`、`attempts`、`quality`、`fetched_at` 和 `error_code` 为准；核对 `limit`、实际行数、字段和交易日。空结果或旧数据保留缺口，不推测或补造。

## 路由边界

- Provider 选择、超时、失败切换均由公共 `query()` 内的路由负责；当前自然语言链见仓库 `docs/agent-contract.md`。Agent 不另设降级顺序，不把私有 source、V1/V2 `resolve()`、`pywencai`、TDX/Wind 直连或旧 WorkBuddy/NeoData 脚本当第二入口。
- 用户原始筛选条件应保留；必要时拆分过长的语义查询，并分别记录参数与结果。不能为了取得非空结果自行改变核心条件。
- 查询失败时报告实际错误、attempt chain、时间和影响范围；资金流只能作辅助证据，查询结果本身不授予交易权限。
