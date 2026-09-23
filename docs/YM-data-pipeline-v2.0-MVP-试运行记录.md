# YM-data-pipeline v2.0 MVP 试运行记录

> **2026-07-29 归档说明**：本文记录已经结束的 V2 MVP 试运行，以下 `resolve()` 示例和“不接生产消费端”等结论仅用于解释历史验证过程，不再是现行使用说明。正式公共入口为 `from ym_stock_data import query`，返回 contract 1.0；V1/V2 只保留兼容包装（compatibility wrapper）。实时 provider 状态使用根目录 `./ym-data doctor --json`，只有显式调用根目录 `./ym-data smoke --live` 才运行只读在线探针。

> 当前迁移仍受 rollback switch 保护：默认是否切换必须由同一时点 side-by-side 的业务 shape、provider/attempts、合法 empty 与 error overwrite guard 证据决定。TDX/Wind provider 已进入受治理 registry，但没有鉴权或在线探针证据时不得宣称可用，也不得用于交易写入。

**状态**：v2.0 MVP 已合并到 `main`

**当前 main 提交**：试运行前用 `git log -1 --oneline` 确认。

**试运行周期**：1-2 个交易日

**目标**：让稳米/洋米在真实查数场景里试用 `ym_stock_data.v2.resolve()`，验证 intent 入口、字段口径、新鲜度标记、个股行情和问财批量 query 是否有用。试运行期间不接生产消费端。

---

## 1. 使用边界

### 可以使用 v2 MVP 的场景

- 稳米/洋米临时查盘面数据。
- 红方对抗草稿。
- 复盘前的数据验证。
- 对比“用 intent 查”和“直接 fetch 查”的结果是否一致。
- 检查 `_meta.source_chain`、`_meta.data_scope`、`_meta.confidence` 是否能帮助判断数据来源和可靠性。

### 禁止使用 v2 MVP 的场景

- 不接 live-dashboard collector。
- 不接正式复盘生成脚本。
- 不用于盘中交易决策。
- 不让自动化脚本依赖 v2 输出。

---

## 2. 试运行命令

```bash
cd /Users/yimu/Documents/YM_Capital/YM-data-pipeline
git pull

python3 - <<'PY'
from ym_stock_data.v2 import resolve

cases = [
    ("realtime_market", {}),
    ("sector_index", {"codes": ["881124"]}),
    ("sector_index", {"names": ["消费电子", "通信设备"]}),
    ("stock_snapshot", {"codes": ["002475", "002281"]}),
    ("stock_kline", {"code": "002475", "period": "15m", "count": 20}),
    ("review_sentiment", {}),
]

for intent, kwargs in cases:
    r = resolve(intent, **kwargs)
    print("\n==", intent, "==")
    print(r["_meta"])
    print(list(r["data"])[:5] if isinstance(r["data"], dict) else type(r["data"]))
PY
```

必要时只查单条问财模板，便于定位：

```bash
python3 - <<'PY'
from ym_stock_data.v2 import resolve

r = resolve("review_sentiment", query="昨日涨停 今日涨跌幅 非st")
print(r["_meta"])
print(r["data"])
PY
```

---

## 3. 试运行观察项

每次试运行记录以下内容：

- 哪些 query 慢。
- 哪些 query 空。
- 是否出现 `confidence: "stale"`。
- 是否出现 `confidence: "unknown"`。
- `source_chain` 是否真实反映来源和降级。
- `data_scope` 是否足够清楚，尤其是问财/同花顺/东财口径。
- `sector_index` 是否严格返回同花顺 881xxx，尤其是 `消费电子=881124`，不能出现中证 931xxx 或 TDX 880xxx。
- `stock_snapshot` 个股字段是否满足临时查票，尤其是最新价、涨幅、量比、换手、MA5/MA10/MA20。
- `stock_kline` 是否满足技术分析基础需求，尤其是 bars、last_close、MA5/MA10/MA20、`period` 和 `count` 参数。
- `review_sentiment` 的 6 组问财 query 是否够用，顶层 `涨停收益均值`、`红盘率`、`炸板率`、`最高板` 是否合理。
- 和原来 `fetch("index")` / `fetch("quotes")` / `fetch("iwencai")` 直接查有没有明显差异。

### sector_index 当前边界

- 主源：`sources.ths_industry.fetch_sector_index()`，不经过 v1 `fetch()` 路由。
- 只接受同花顺行业 `881xxx` 代码；传 `931xxx` 必须报错。
- 支持名称查询：`消费电子 -> 881124`，`通信设备 -> 881129`。
- 返回重点：`items`、`by_code`、`by_name`、`missing`，字段包含 `change_pct` 和 `main_net_inflow_yi`。

### stock_kline 当前边界

- 主源：`sources.pytdx.fetch_kline()`，不经过 v1 `fetch()` 路由。
- 支持周期：`daily` / `weekly` / `monthly` / `60m` / `15m` / `5m`。
- 返回重点：`bars`、`last_close`、`mas.MA5`、`mas.MA10`、`mas.MA20`、`requested_count`、`returned_bars`。
- TDX MCP 暂不作为默认主源；先用于真实数据交叉校验，稳定后再按备源毕业标准纳入生产候选。

### review_sentiment 当前 6 组 query

- `涨停 跌停 非st`
- `昨日首板 今日连板 晋级率 封板率 非st`
- `昨日炸板 今日涨跌幅 炸板率 非st`
- `昨日涨停 今日涨跌幅 非st`
- `昨日连板 今日涨跌幅 非st`
- `今日连板 股票简称 连板数 非st`

---

## 4. 问题记录模板

```text
日期：
执行人：
intent：
query（如有）：
现象：
_meta：
与 v1 fetch 对比：
判断：
建议：
```

---

## 5. 两天后触发条件

试运行 1-2 个交易日后，如果没有出现阻断问题，进入 v2.1。

阻断问题包括：

- `resolve("realtime_market")` 经常无法返回标准 `_meta`。
- `resolve("stock_snapshot")` 经常无法返回标准 `_meta` 或个股报价字段。
- `review_sentiment` 多数 query 为空或明显不稳定。
- `confidence` 标记误导，例如过期数据仍显示 normal。
- `source_chain` 无法解释实际来源。
- 字段口径仍然容易混用。

---

## 6. v2.1 落地顺序

试运行通过后，按以下顺序落地：

1. 继续补强真实查票 intent
   - 用洋米/稳米试运行记录补齐 `stock_snapshot` 字段缺口。
   - 只接 v1 已有稳定字段；MACD、资金流等缺源字段单独立项。

2. `scripts/compare_v1_v2.py`
   - 对比 v1 `fetch()` 和 v2 `resolve()` 的关键字段。
   - 先覆盖 `realtime_market`、`sector_index`、`stock_snapshot`、`stock_kline`、`review_sentiment`。
   - 输出 source、data_scope、confidence、差异字段。

3. `ym-data doctor`
   - 检查 PyTDX、问财、pywencai、package data、环境变量。
   - 没有凭证或 TCP 不通时给出明确缺项。

4. portable 安装检查
   - 补 `.env.example`。
   - 补安装文档。
   - 验证新机器 `git pull` 后哪些能力可用，哪些能力需要凭证。

5. TDX MCP 样例库和毕业评估
   - 至少 20 个 sample query。
   - 连续 5 个交易日对账。
   - 记录延迟、可用率、字段口径。

v2.1 完成前，仍不迁移 live-dashboard 和正式复盘。消费端迁移属于 v2.2。
