# YM-data-pipeline v2.0 数据源治理实施方案

> **给各米执行用**：这是一份治理方案，不是一次性改造单。正式派工时必须进入 `/Users/yimu/agent-board`，按 `board.py` 创建、领取、提交、验收任务。执行复杂代码任务时建议使用 `executing-plans` 或 `subagent-driven-development`。

**版本**：2.0.0

**目标**：把弈沐资本 A 股数据查询从“多个工具各查各的”治理成“统一入口、统一策略、统一口径、按场景调用”的数据基座。

**核心结论**：当前 `YM-data-pipeline` 已有统一入口 `ym_stock_data.fetch()`，但它只是数据源路由器，还不是场景决策层。下一步要补的是“v2 旁路核心 + 意图路由 + 字段策略表 + 双轨对比 + Skill 规范化”，消费端迁移放到 v2.2。

**涉及项目**：
- 数据管道：`/Users/yimu/Documents/YM_Capital/YM-data-pipeline/`
- 看板项目：`/Users/yimu/Documents/YM_Capital/live-dashboard/`
- 治理文档：`/Users/yimu/Documents/YM_Capital/YM-data-pipeline/docs/`
- 跨 Agent 任务板：`/Users/yimu/agent-board/`

---

## 1. 当前问题

### 1.1 真实现状

当前数据体系不是没有统一入口，而是统一入口没有管住所有消费场景。

已存在：
- `ym_stock_data.fetch()`：统一路由 15 类 `data_type`。
- `pipeline_coverage.json`：字段级来源、频率、状态说明。
- `live-dashboard/config/sources.yaml`：看板数据源降级链雏形。
- 多个 Skill：提供问财、A 股管道、红方复盘、监控等使用说明。
- 新增 TDX MCP：可用于通达信行情、K 线、选股、公告、研报、宏观查询。

主要分叉：
- 有的脚本走 `fetch()`。
- 有的脚本直接 import `ym_stock_data.sources.iwencai.query`。
- 看板 collector 里仍有自己的频率、缓存、降级和字段清洗逻辑。
- Skill 里保留了旧口径和独立命令，例如 `iwencai_query.py`、NeoData、Tushare、联网查。
- 不同工具返回的“主力净流入、板块涨停数、情绪值、封板率”等口径可能不一致。

### 1.2 根因

根因不是工具太多，而是缺少三层治理：

1. **意图层**：用户或 Agent 问“查某票”“做复盘”“看盘中情绪”时，没有统一规则决定该问哪个源。
2. **字段层**：每个字段的主源、备源、口径、刷新频率、能否用于交易判断没有被程序强制执行。
3. **消费层**：看板、复盘、Skill、临时查询仍能绕过主入口直接调源。

---

## 2. 数据源分工原则

### 2.1 总原则

按稳定性和用途分层：

1. **实时生产链路优先结构化源**：PyTDX、同花顺固定 HTTP、腾讯、北向等。
2. **自然语言源用于低频复杂查询**：问财、TDX MCP。
3. **广覆盖源用于补盲**：NeoData、Tushare、联网搜索。
4. **人工数据和复盘笔记仍是主观 SSOT**：持仓、操作、板块定性、锚定股状态不能被工具覆盖。

### 2.2 数据源能力矩阵

| 数据源 | 当前入口 | 优点 | 局限 | 推荐角色 |
|---|---|---|---|---|
| PyTDX TCP | `fetch("quotes/index/breadth/sector_index/kline/kline_15m")` | 快、零鉴权、适合 5s/30s 实时 | TCP 环境敏感，非交易时段/云端可能失败，不懂自然语言 | 看板实时主源 |
| 同花顺热点 | `fetch("ths_hot")` | 零鉴权，强势股和题材归因好 | 字段和页面结构可能变化 | 涨停列表、题材词频 |
| 同花顺行业资金 | `fetch("sector_inflow")` | 行业净流入稳定、低成本 | 口径是同花顺行业，不等于问财概念 | 板块资金辅助 |
| 北向资金 | `fetch("northbound")` | 分钟级、低成本 | 北向口径只覆盖沪深股通 | 看板资金模块 |
| 腾讯财经 | `fetch("tencent")` | PE/PB/市值/换手稳定、零鉴权 | 财务字段有限 | 单票基础估值 |
| 东财龙虎榜 | `fetch("dragon_tiger")` | 盘后结构化 | 频率低，Referer 依赖 | 复盘一次性拉取 |
| 东财研报 | `fetch("research")` | 研报/评级/目标价结构化 | 覆盖和更新有延迟 | 单票调研 |
| 巨潮公告 | `fetch("filings")` | 公告权威 | 摘要弱，需要后续解析 PDF | 公告源 |
| 财联社新闻 | `fetch("news")` | 实时快讯 | 噪声大，不能直接当结论 | 新闻提醒 |
| 问财 OpenAPI | `fetch("iwencai")` | 自然语言强，复合条件强 | 限额、字段漂移、返回不稳定 | 竞价、复盘、复杂筛选 |
| pywencai | `fetch("iwencai")` 降级 | 无 OpenAPI 额度消耗 | 慢、网页结构风险 | 问财降级 |
| TDX MCP | Codex/WorkBuddy MCP | 交互式强，覆盖行情/K线/选股/公告/研报/宏观 | 参数和口径需校准，OAuth 依赖登录 | 投研补充、问财补盲 |
| NeoData | Skill | 覆盖广，适合自然语言金融问答 | 口径不进入实时主链路 | 问财/TDX 不足时补盲 |
| Tushare | Skill | 历史和专题数据广 | 鉴权/积分/非实时 | 特殊历史专题 |
| 联网搜索 | Web | 新闻、政策、官网资料 | 不稳定、不可直接量化 | 高风险事实核验 |

### 2.3 TDX MCP 升级标准

TDX MCP 默认角色是投研补充和问财补盲，不进入生产实时链路。

从“投研补充”升级为“备源”必须同时满足：
- 20 个以上 sample query 覆盖行情、K 线、选股、公告、研报等核心场景。
- 连续 5 个交易日与 PyTDX、问财或权威源对账，关键字段一致或差异可解释。
- 常用查询延迟小于 2 秒。
- 交易时段可用率大于 99%。
- 字段口径已写入 `data_scope`，不能只凭工具名判断可信度。
- OAuth、登录态、失败重试和错误信息可观测。

未达到以上标准前，TDX MCP 不作为盘中交易决策数据源。

---

## 3. 目标架构

### 3.1 版本边界

v2.0 采用“旁路重搭新核心，不接生产消费端”的边界。

v2.0 要做：
- 在当前仓库内新增 `ym_stock_data/v2/`。
- 复用已验证的旧 `sources/` 和 `fetch()`，但不修改它们的返回契约。
- 只实现 MVP：`realtime_market`、`stock_snapshot`、`review_sentiment` 三个 intent，30 个关键字段策略，标准 `_meta`。
- 把数据新鲜度检查写进 `resolve()`，超出阈值必须标注 `confidence: "stale"`。
- 新增 v2 MVP 测试和少量样例。
- 保持 live-dashboard 和复盘脚本继续走当前链路。

v2.0 不做：
- 不重写 10 个旧数据源。
- 不删除或替换 `fetch()`。
- 不把 live-dashboard collector 直接切到 v2。
- 不把复盘生成链路直接切到 v2。
- 不在 v2.0 强制完成完整动态路由、全量 query case、Skill 全量同步、compare 平台。
- 不承诺新电脑 `git pull` 后无配置即完整运行，portable 工程化放到 v2.1。

后续版本：
- **v2.1 portable + 扩展验证**：清理硬编码本机路径，补 `.env.example`、`.[all]` 依赖、`ym-data doctor`、安装文档、smoke test、v1/v2 compare、query cases。
- **v2.2 消费端迁移**：live-dashboard 和复盘逐模块切到 v2，切换前必须双轨对比。

### 3.2 旁路双轨架构

```
生产链路 v1（保持不动）
live-dashboard / 复盘脚本
        ↓
fetch() / sources / 当前 collector
        ↓
现有 dashboard_live.json / dashboard_data.json

旁路链路 v2（新增，不接生产）
Agent 验证 / 测试 / smoke 脚本 / v2.1 compare 脚本
        ↓
ym_stock_data.v2.resolve()
        ↓
Field Policy + Intent Router
        ↓
V2 Adapter 包装旧 fetch()/sources/MCP/外部源
        ↓
Normalized Result + source_chain + data_scope
```

### 3.3 v2.0 MVP 推荐目录结构

新增目录只放 v2 逻辑，旧代码继续保留。v2.0 先控制在约 6 个文件，避免为了当前规模建立过重框架。

```text
ym_stock_data/
├── fetch.py                 # v1 物理源入口，保持兼容
├── sources/                 # v1 已验证 source，保持兼容
└── v2/
    ├── __init__.py          # 导出 resolve
    ├── resolve.py           # v2 MVP intent router，先硬编码 3 个 intent
    ├── normalize.py         # 统一返回结构
    ├── adapters.py          # 包装 v1 fetch/sources，不直接复制 source
    └── policies/
        └── fields.json      # 30 个关键字段的主源、备源、口径、频率

tests/
├── test_v2_mvp.py
└── fixtures/
    └── v2_mvp_cases.json
```

v2.1 再扩展：
- `policy.py`
- `doctor.py`
- `policies/intents.json`
- `scripts/compare_v1_v2.py`
- 完整 query cases 和 Skill 同步。

### 3.4 新增概念

#### Intent Router

统一处理“场景意图”，例如：

| intent | 用途 | 首选源 | 备源 |
|---|---|---|---|
| `realtime_market` | 看板指数、涨跌家数、成交额 | PyTDX | 东财 fallback |
| `stock_snapshot` | 个股临时查票、快速技术快照 | PyTDX | 腾讯/easyquotation |
| `realtime_quotes` | 自选股实时行情，后续可由 `stock_snapshot` 扩展 | PyTDX | 腾讯/easyquotation |
| `realtime_sectors` | 板块实时涨跌和均线 | PyTDX | baseline |
| `sentiment_intraday` | 盘中情绪节点 | 问财固定 query | 东财涨跌停池 |
| `auction_snapshot` | 竞价 5 维 | 问财固定 query | baseline |
| `review_sentiment` | 盘后情绪复盘 | 问财批量 query | pywencai / TDX MCP |
| `single_stock_research` | 单票调研 | PyTDX + 腾讯 + 研报 + 公告 | TDX MCP / 问财 |
| `topic_screening` | 主题筛选 | 问财 | TDX MCP screener / NeoData |
| `news_policy_check` | 新闻政策核验 | 财联社 + 联网官方源 | TDX MCP news |

#### Field Policy

字段级策略表不要直接覆盖 `pipeline_coverage.json`。v2.0 先新增 `ym_stock_data/v2/policies/fields.json`，等 v2 稳定后再决定是否合并或替代 `pipeline_coverage.json`。

字段策略至少包含：

```json
{
  "field": "昨日涨停收益",
  "intent": "review_sentiment",
  "primary": {"source": "iwencai", "query": "昨日涨停 今日涨跌幅 非st"},
  "fallback": [{"source": "pywencai"}],
  "freq": "盘后/10min",
  "data_scope": "问财口径",
  "trade_usage": "辅助，不单独触发交易",
  "staleness_sec": 1800,
  "rate_class": "limited"
}
```

#### Normalized Result

所有上游结果最终应带：

```json
{
  "data": {},
  "_meta": {
    "intent": "review_sentiment",
    "source": "iwencai",
    "source_chain": ["iwencai_openapi"],
    "fetched_at": "2026-06-03T17:30:00+08:00",
    "staleness_sec": 0,
    "data_scope": "问财口径",
    "confidence": "normal",
    "error": false
  }
}
```

#### Staleness Check

`resolve()` 必须按字段策略检查新鲜度。只要上游返回带 `fetched_at` 或可推导查询时间，就必须计算 `age_sec`：

```python
if age_sec > policy["staleness_sec"]:
    meta["confidence"] = "stale"
    meta["warn"] = f"数据距今 {age_sec}s，超过阈值 {policy['staleness_sec']}s"
```

规则：
- `confidence: "normal"` 表示未超过字段阈值。
- `confidence: "stale"` 表示数据可展示但不能直接用于盘中交易决策。
- 无法取得 `fetched_at` 时，`confidence` 至少为 `"unknown"`，并在 `_meta.warn` 说明原因。
- 对实时字段，过期数据不能静默当成最新数据返回。

### 3.5 生产保护规则

v2.0 的所有实现必须满足：
- 新增文件为主，除导出入口和文档外，不改旧 source 的行为。
- `from ym_stock_data import fetch` 的旧调用必须继续可用。
- v2.0 默认只由 Agent 验证、测试或 smoke 脚本调用。
- live-dashboard 和复盘脚本没有明确迁移任务时，不 import `ym_stock_data.v2`。
- v2 输出和 v1 输出不一致时，以 v1 生产链路为准，v2 记录差异。

---

## 4. 场景使用规则

### 4.1 盘中实时看板

使用目标：稳定、低延迟、不会被限流影响。

| 模块 | 主源 | 备源 | 禁用/慎用 |
|---|---|---|---|
| 三大指数 | PyTDX | 东财 index fallback | 问财 |
| 自选股报价 | PyTDX | 腾讯/easyquotation | 问财 |
| 涨跌家数 | PyTDX | 东财 index fallback | TDX MCP |
| 15min K线/量比 | PyTDX | 缓存昨日基线 | 问财 |
| 板块指数 | PyTDX | baseline | 问财 |
| 北向资金 | 同花顺北向 | 缓存 | 联网 |
| 行业净流入 | 同花顺行业 | 空值保留 | 问财混口径 |
| 热榜/涨停 | 同花顺热点 | 东财涨跌停池 | 手工估算 |

规则：
- 5s/30s 高频任务不调用问财、TDX MCP、联网搜索。
- 如果 PyTDX 失败，允许降级，但必须在 `_meta.source_chain` 或看板健康状态里标注。
- 实时链路宁可保留上一笔有效数据，也不要用空结果覆盖。

### 4.2 竞价节点

使用目标：9:26-9:28 一次性捕获竞价情绪。

主源：
- 问财固定 query 模板。

备源：
- 如果问财空结果，用上一日 baseline 或部分东财涨跌停池补位。

规则：
- 竞价查询每天固定一次或少量重跑。
- 结果必须带日期和查询时间，防止拿昨日字段当今日字段。
- 不用 TDX MCP 替代问财竞价快照，除非后续证明 TDX MCP 能稳定返回同等字段。

### 4.3 盘后复盘

使用目标：完整、可解释、口径一致。

主源：
- 情绪、涨停链条、晋级率、封板率：问财。
- 龙虎榜：东财。
- 研报：东财/TDX MCP。
- 公告：巨潮/TDX MCP。
- 新闻：财联社/联网核验。

规则：
- 问财是盘后复盘主源，但必须固定 query 模板，不允许每个 Agent 随意改问法。
- 同一字段同一篇复盘中不能混用问财/东财/同花顺口径。
- 涉及“主力净流入”的结论必须标注口径。
- 对交易判断有影响的数据，至少保留 `_meta.source` 和原始 query。

### 4.4 单票调研

推荐顺序：

1. PyTDX：价格、涨跌幅、K线、均线、量比。
2. 腾讯：PE/PB/市值/换手。
3. 巨潮/东财/TDX MCP：公告、研报、评级、目标价。
4. 问财：技术形态、资金面、行业位置、复合筛选。
5. 联网：政策、公司事件、新闻原文核验。

规则：
- 单票结论不使用单一数据源直接下判断。
- 价格和技术面以结构化源优先。
- 新闻和公告类信息优先找原始公告或权威媒体。

### 4.5 Agent 临时问数

v2.0 前统一口径：

1. 先问 `ym_stock_data` 是否覆盖。
2. 覆盖则用 `from ym_stock_data import fetch`。
3. 不覆盖但属于金融结构化数据，优先 TDX MCP。
4. TDX MCP 不覆盖再用 NeoData/Tushare。
5. 实时新闻、政策、版本、法规必须联网查证。

禁止：
- 训练数据直接回答实时行情。
- 手工估算涨停数、板块涨停数。
- 混用主力资金口径不标注来源。

v2.0 MVP 完成后的使用边界：

| 场景 | 用 v1 `fetch()` | 用 v2 `resolve()` |
|---|---:|---:|
| live-dashboard collector | 是 | 否，v2.2 才切 |
| 复盘脚本生成 | 是 | 否，v2.2 才切 |
| Agent 临时查票 | 当前默认 | 推荐用于非交易验证 |
| 红方对抗数据 Q1-Q6 | 当前默认 | 推荐用于非交易验证 |
| 盘中交易决策 | 是 | 否，v2 验证期禁用于交易 |
| 盘后方案设计/工具调试 | 可用 | 推荐 |

边界解释：
- “生产场景”指会写入 live-dashboard、复盘正式产物、交易决策记录或自动化脚本的链路。
- “验证场景”指 Agent 临时查询、方案设计、红方对抗草稿、对比实验，不直接触发交易动作。
- 同一结论如果用于交易，当日必须回到 v1 生产链路或权威源复核。

---

## 5. 实施路线

### Phase 0：冻结边界和入口审计

**目标**：先把现在有哪些入口、字段、调用路径查清楚，并明确 v2.0 不切生产链路。

负责人建议：欧米主导，稳米协助。

产出：
- 数据源入口清单。
- 字段策略初版。
- “哪些脚本绕过 fetch”清单。
- v1/v2 边界声明。

检查路径：
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/ym_stock_data/fetch.py`
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/pipeline_coverage.json`
- `/Users/yimu/Documents/YM_Capital/live-dashboard/config/sources.yaml`
- `/Users/yimu/Documents/YM_Capital/live-dashboard/scripts/collectors/`
- `/Users/yimu/.agents/skills/`
- `/Users/yimu/.workbuddy/skills/`

验收：
- 每个 live-dashboard collector 是否走 `fetch()` 有明确结论。
- 每个关键字段有主源/备源/频率/口径。
- 文档明确：v2.0 不修改 live-dashboard 和复盘消费端。

### Phase 1：v2.0 MVP 闭环

**目标**：用最小代码实现一个可验证的 v2 旁路入口，不做完整平台化。

负责人建议：欧米设计并验收，黑米或洋米落地。

建议新增：
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/ym_stock_data/v2/__init__.py`
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/ym_stock_data/v2/resolve.py`
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/ym_stock_data/v2/normalize.py`
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/ym_stock_data/v2/adapters.py`
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/ym_stock_data/v2/policies/fields.json`
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/tests/test_v2_mvp.py`

MVP 功能：
- `resolve("realtime_market")`：包装 `fetch("index")`，补标准 `_meta`。
- `resolve("stock_snapshot")`：包装 `fetch("quotes")`，补个股行情 `_meta`。
- `resolve("review_sentiment")`：按字段策略批量执行问财 query，保留原始 query。
- `fields.json`：覆盖 30 个关键字段，含 `data_scope`、`trade_usage`、`staleness_sec`、`rate_class`。
- `normalize.py`：统一返回 `data` 和 `_meta`。
- `resolve()`：检查 `staleness_sec`，过期时标注 `confidence: "stale"` 和 `warn`。

建议 API：

```python
from ym_stock_data.v2 import resolve

resolve("realtime_market")
resolve("stock_snapshot", codes=["002475", "002281"])
resolve("review_sentiment")
```

规则：
- `fetch()` 保留为 v1 底层兼容入口。
- `ym_stock_data.v2.resolve()` 是 v2 优先入口。
- v2.0 阶段不从 `ym_stock_data.__init__` 顶层导出 `resolve`，避免旧脚本误用。
- `resolve()` 返回标准 `_meta.intent`、`source_chain`、`data_scope`、`confidence`。
- v2 只能包装旧 `fetch()` 或 source，不复制旧 source 实现。
- 旧 `fetch()`、旧 `sources/` 不因 Phase 1 变化。
- 先用硬编码路由实现 3 个 intent，不急于抽象动态路由。

验收：
- `python3 -m py_compile ym_stock_data/v2/*.py` 通过。
- `python3 -c "from ym_stock_data.v2 import resolve; resolve('realtime_market')"` 可运行。
- `python3 -m pytest tests/test_v2_mvp.py -v` 通过；如果本机没有 pytest，至少用 `python3 -m py_compile` 和 smoke command 替代。
- 30 个关键字段能查到来源策略。
- `realtime_market` 不调用问财、TDX MCP、联网搜索。
- 新鲜度超过阈值时 `_meta.confidence` 不是 `"normal"`。
- v1 旧调用不受影响：`from ym_stock_data import fetch` 仍然可用。
- live-dashboard 和复盘没有新增 v2 import。

### Phase 2：v2.0 收口

**目标**：只把 MVP 交给各米试用，不扩大改造面。

负责人建议：欧米主导。

产出：
- README 或方案文档中补充 v1/v2 使用边界。
- 给稳米/洋米的试用说明。
- 明确 v2.0 不迁移 production consumer。

验收：
- `git diff -- ym_stock_data/fetch.py ym_stock_data/sources` 为空或仅包含明确无行为变化的注释。
- 给各米的说明明确“验证可用、交易禁用、生产不切”。
- 后续事项进入 v2.1/v2.2 backlog。

### v2.1 Backlog：portable + 扩展验证

**目标**：让新电脑 `git pull` 后能按文档安装和体检。

范围：
- `.env.example`
- `.[all]` 可选依赖
- 去掉 WorkBuddy 硬编码路径或改为环境变量默认
- `ym-data doctor`
- `docs/INSTALL.md`
- smoke tests
- `scripts/compare_v1_v2.py`
- 完整 query cases
- Skill 全量同步
- TDX MCP 样例库

验收：
- 新环境按 `docs/INSTALL.md` 能安装。
- 没有凭证时输出明确缺项，不崩溃。
- 没有 PyTDX TCP 时能显示 fallback 状态。
- v1/v2 compare 覆盖 `realtime_market`、`stock_snapshot`、`review_sentiment`。

### v2.2 Backlog：live-dashboard / 复盘迁移

**目标**：消费端逐模块切换到 v2。

迁移顺序：
1. 新闻、研报、公告。
2. 行业资金、北向。
3. 盘后复盘情绪。
4. 竞价快照。
5. 盘中实时行情。

硬规则：
- 每个模块切换前必须有 v1/v2 compare 报告。
- 每次只切一个模块。
- 支持一键回退到 v1。
- 连续几个交易日稳定后再切核心实时字段。

验收：
- 看板启动后 `/api/health` 显示 v2 source_chain 和 fallback。
- 复盘生成保留原始 query 和 data_scope。
- 不出现空结果覆盖上一笔有效实时行情。

---

## 6. 各米分工建议

### 欧米

适合任务：
- 设计 `Field Policy` 和 `Intent Router`。
- 代码审查。
- 验收口径一致性。
- 判断哪些字段能用于交易判断。

交付标准：
- 输出清晰接口和测试边界。
- 不直接做大面积机械迁移。

### 稳米

适合任务：
- 盘中/盘后流程落地。
- WorkBuddy Skill 同步。
- 复盘字段和问财模板整理。
- 运行看板和采集器验证。

交付标准：
- 所有查询模板有来源、频率、失败处理。
- 复盘数据可追溯到原始 query。

### 洋米

适合任务：
- 大量终端验证。
- 跑测试、部署、环境修复。
- 扫描全仓绕过入口的脚本。
- Git 分支/提交/回滚保护。

交付标准：
- 给出命令输出摘要。
- 不隐藏失败源。

### 黑米

适合任务：
- 按明确任务改局部代码。
- 替换 import 路径。
- 补测试。
- 修文档中的旧调用示例。

交付标准：
- 范围小、文件清楚、测试通过。
- 不自行重构架构。

### 紫米

适合任务：
- 异步查政策、新闻、外部资料。
- 远程环境验证。
- 补充 TDX MCP/NeoData/Tushare 查询样例。

交付标准：
- 输出来源链接和日期。
- 不直接改本地文件。

---

## 7. 执行任务拆分

正式执行时，建议在 agent-board 拆成以下任务。

### 任务 A：数据入口审计

执行者：洋米或稳米。

目标：
- 找出所有绕过 `ym_stock_data.fetch()`、直接调 source、直接调问财/TDX/NeoData/Tushare/联网的调用。
- 标注每个调用属于生产链路、复盘链路、Skill 链路还是临时脚本。
- 明确 v2.0 不迁移这些调用，只建立审计清单和后续迁移建议。

命令建议：

```bash
cd /Users/yimu/Documents/YM_Capital
rg -n "ym_stock_data|iwencai|pywencai|PyTDX|pytdx|tdx|NeoData|Tushare|akshare|easyquotation" .
```

输出：
- 文件路径。
- 调用源。
- 所属场景。
- 是否属于生产消费端。
- v2.0 是否允许改动，默认不改。
- v2.2 迁移建议。

验收：
- 清单覆盖 `YM-data-pipeline`、`live-dashboard`、相关 Skill。
- 没有把审计任务变成直接迁移任务。

### 任务 B：v2.0 MVP 闭环

执行者：欧米设计，黑米或洋米落地。

目标：
- 新增 `ym_stock_data/v2/`。
- 只包装旧 `fetch()` / `sources/`，不复制旧数据源实现。
- 提供 `ym_stock_data.v2.resolve()` 和标准 `_meta`。
- 先实现 `realtime_market` 和 `review_sentiment`，不急于实现全量 intent。
- 实现新鲜度检查，超阈值返回 `confidence: "stale"`。

建议文件：
- `ym_stock_data/v2/__init__.py`
- `ym_stock_data/v2/resolve.py`
- `ym_stock_data/v2/adapters.py`
- `ym_stock_data/v2/normalize.py`
- `ym_stock_data/v2/policies/fields.json`
- `tests/test_v2_mvp.py`

验收：
- `python3 -c "from ym_stock_data.v2 import resolve; resolve('realtime_market')"` 通过。
- `from ym_stock_data import fetch` 旧入口不受影响。
- live-dashboard 和复盘没有新增 v2 import。
- `fields.json` 覆盖 30 个关键字段。
- 高频字段的主源不能是问财、TDX MCP、联网搜索。
- 过期数据不会以 `confidence: "normal"` 返回。

### 任务 C：v2.0 使用边界同步

执行者：稳米主导，欧米审。

目标：
- 各 Agent 先同步 v1/v2 使用边界，不做 Skill 全量改造。

验收：
- live-dashboard 和复盘脚本继续走 v1。
- Agent 临时查票、红方草稿、方案验证可试用 v2。
- 盘中交易决策禁用 v2 验证期结果。
- TDX MCP 明确为投研补充和问财补盲。

### Backlog：v2.1 portable / 扩展验证

执行者：洋米主导，欧米审。

目标：
- 清理硬编码路径、环境变量、可选依赖、安装说明、doctor 能力。
- 增加 v1/v2 compare 脚本、完整 query cases、Skill 全量同步。

验收：
- 输出 v2.1 portable issue 清单。
- 明确哪些能力 `git pull` 后可用，哪些需要凭证或本机服务。

### Backlog：v2.2 消费端迁移

执行者：欧米设计，稳米/黑米后续执行。

目标：
- 设计 live-dashboard 和复盘逐模块迁移顺序。
- 每个模块必须包含 compare、灰度、回退方案。

验收：
- 没有 v1/v2 对比报告的模块不得迁移。
- 不允许一次性全量替换 live-dashboard collector。

---

## 8. 风险和控制

### 风险 1：过早把 TDX MCP 放进生产链路

控制：
- TDX MCP 短期只作为投研补充。
- 升级为备源前必须满足 2.3 的毕业标准：20 个以上样例、连续 5 个交易日对账、延迟和可用率达标、口径已标注。

### 风险 2：问财 query 被各 Agent 改写

控制：
- 固定 query 模板进入 policy。
- 复盘字段只允许从模板调用。

### 风险 3：字段名漂移

控制：
- 所有问财解析函数必须用关键词匹配和日期后缀清洗。
- 原始 columns 保留到调试输出。

### 风险 4：口径混用

控制：
- `_meta.data_scope` 必填。
- 主力资金、板块涨停数、情绪类指标必须显示来源。

### 风险 5：改造影响盘中稳定

控制：
- v2.0 只新增 `ym_stock_data.v2.resolve()`，不删除 `fetch()`。
- live-dashboard 和复盘迁移放到 v2.2。
- 每次未来迁移都必须保留上一笔有效数据保护。
- compare 报告没有通过前，不切生产消费端。

---

## 9. 推荐优先级

第一优先级：
1. 数据入口审计。
2. v2.0 MVP 闭环：`realtime_market`、`stock_snapshot`、`review_sentiment`、30 个字段、标准 `_meta`。
3. 新鲜度检查：`confidence: "stale"` 和 `_meta.warn`。
4. v1/v2 使用边界同步。

第二优先级：
5. TDX MCP 样例库和毕业评估。
6. v2.1 portable 工程化。
7. v1/v2 双轨对比脚本。

第三优先级：
8. query cases 和回归测试扩展。
9. Skill 全量同步。
10. NeoData/Tushare 补盲规范。
11. v2.2 live-dashboard / 复盘迁移。

---

## 10. 最小可行版本

如果先做一个小闭环，建议只做这 4 件：

1. 新增 `ym_stock_data/v2/`，导出 `ym_stock_data.v2.resolve()`。
2. 新增 `resolve("realtime_market")`，内部只包装 `fetch("index")`，并返回标准 `_meta`。
3. 新增 `resolve("stock_snapshot")`，内部只包装 `fetch("quotes")`，用于真实个股查票试运行。
4. 新增 `resolve("review_sentiment")`，内部按字段策略批量执行问财 query，并保留原始 query。
5. 新增 `ym_stock_data/v2/policies/fields.json`，覆盖 30 个最关键字段。

同时必须补一个非功能要求：
- `resolve()` 按 `staleness_sec` 检查数据新鲜度，超时返回 `confidence: "stale"`。

这个版本不影响看板和复盘现有运行，只让 v2 具备可验证的旁路入口。Skill 可以提示各米在非生产验证时使用 `ym_stock_data.v2.resolve()`，但不能要求 live-dashboard 或正式复盘无缝切换。

---

## 11. 验收标准

v2.0 完成的标准：

- 新增 v2 核心在 `ym_stock_data/v2/`，旧 `fetch()` 和旧 `sources/` 兼容。
- live-dashboard 和复盘生产链路没有被 v2.0 改动。
- Agent 在验证场景查询 A 股数据时能先判断 intent，而不是凭经验选工具。
- 看板实时链路不依赖问财、MCP、联网搜索。
- 复盘情绪相关的 v2 intent 按字段策略批量执行固定问财 query，并保留原始 query 和 source。
- 所有关键字段可从策略表查到主源和备源。
- 每个 v2 返回都带 `_meta.source_chain`、`data_scope`、`fetched_at`、`confidence`。
- 新鲜度超出字段阈值时能明确标注 stale，不静默当成实时数据。
- Agent v1/v2 使用边界已写清楚。
- 失败时暴露事实：哪个源失败、用了哪个降级、数据新鲜度多少。

v2.0 不以“live-dashboard 已切换”“复盘已切换”“compare 平台完成”“Skill 全量同步”为验收标准；这些属于 v2.1/v2.2。

---

## 12. 给执行 Agent 的开工提示

复制给执行 Agent：

```text
你要执行 YM-data-pipeline 数据源治理任务。先读：
1. /Users/yimu/Documents/YM_Capital/YM-data-pipeline/docs/YM-data-pipeline-2.0-数据源治理方案.md
2. /Users/yimu/Documents/YM_Capital/YM-data-pipeline/AGENTS.md
3. /Users/yimu/Documents/YM_Capital/YM-data-pipeline/README.md
4. /Users/yimu/Documents/YM_Capital/YM-data-pipeline/pipeline_coverage.json
5. /Users/yimu/Documents/YM_Capital/live-dashboard/config/sources.yaml

硬规则：
- 不改无关文件。
- 不绕过 agent-board 派工流程。
- v2.0 只在 YM-data-pipeline 内新增旁路核心，默认不改 live-dashboard 和复盘生产消费端。
- 不从 ym_stock_data 顶层导出 resolve；v2 入口必须是 ym_stock_data.v2.resolve。
- 不删除、不替换、不破坏现有 fetch() 和 sources 返回契约。
- v2.0 MVP 只实现 realtime_market、stock_snapshot 和 review_sentiment；不要扩成全量平台。
- resolve() 必须按 staleness_sec 检查新鲜度，过期数据标注 stale。
- 高频看板链路不接问财、TDX MCP、联网搜索。
- 问财 query 要固定模板并保留原始 query。
- 涉及主力资金、板块涨停数、情绪指标必须标注口径。
- v2 与 v1 数据冲突时，以 v1 当前生产链路为准，v2 只记录差异。
- TDX MCP 未达到毕业标准前只能做投研补充和问财补盲。
- 改完必须跑相关测试或本地验证，失败要如实汇报。
```
