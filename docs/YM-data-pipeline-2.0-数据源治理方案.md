# YM-data-pipeline v2.0 数据源治理实施方案

> **给各米执行用**：这是一份治理方案，不是一次性改造单。正式派工时必须进入 `/Users/yimu/agent-board`，按 `board.py` 创建、领取、提交、验收任务。执行复杂代码任务时建议使用 `executing-plans` 或 `subagent-driven-development`。

**版本**：2.0.0

**目标**：把弈沐资本 A 股数据查询从“多个工具各查各的”治理成“统一入口、统一策略、统一口径、按场景调用”的数据基座。

**核心结论**：当前 `YM-data-pipeline` 已有统一入口 `ym_stock_data.fetch()`，但它只是数据源路由器，还不是场景决策层。下一步要补的是“意图路由 + 字段策略表 + 消费端收敛 + Skill 规范化”。

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

---

## 3. 目标架构

### 3.1 三层架构

```
用户/Agent/看板/复盘
        ↓
Intent Router：按场景选择数据策略
        ↓
Field Policy：字段主源、备源、频率、口径、限流
        ↓
Source Adapter：PyTDX / iwencai / TDX MCP / HTTP 源 / NeoData / Tushare
        ↓
Normalized Result：统一字段、_meta、source_chain、confidence、staleness
```

### 3.2 新增概念

#### Intent Router

统一处理“场景意图”，例如：

| intent | 用途 | 首选源 | 备源 |
|---|---|---|---|
| `realtime_market` | 看板指数、涨跌家数、成交额 | PyTDX | 东财 fallback |
| `realtime_quotes` | 自选股实时行情 | PyTDX | 腾讯/easyquotation |
| `realtime_sectors` | 板块实时涨跌和均线 | PyTDX | baseline |
| `sentiment_intraday` | 盘中情绪节点 | 问财固定 query | 东财涨跌停池 |
| `auction_snapshot` | 竞价 5 维 | 问财固定 query | baseline |
| `review_sentiment` | 盘后情绪复盘 | 问财批量 query | pywencai / TDX MCP |
| `single_stock_research` | 单票调研 | PyTDX + 腾讯 + 研报 + 公告 | TDX MCP / 问财 |
| `topic_screening` | 主题筛选 | 问财 | TDX MCP screener / NeoData |
| `news_policy_check` | 新闻政策核验 | 财联社 + 联网官方源 | TDX MCP news |

#### Field Policy

字段级策略表应从 `pipeline_coverage.json` 升级为可执行配置，至少包含：

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

统一口径：

1. 先问 `ym_stock_data` 是否覆盖。
2. 覆盖则用 `from ym_stock_data import fetch`。
3. 不覆盖但属于金融结构化数据，优先 TDX MCP。
4. TDX MCP 不覆盖再用 NeoData/Tushare。
5. 实时新闻、政策、版本、法规必须联网查证。

禁止：
- 训练数据直接回答实时行情。
- 手工估算涨停数、板块涨停数。
- 混用主力资金口径不标注来源。

---

## 5. 实施路线

### Phase 0：冻结口径和清点入口

**目标**：先把现在有哪些入口、字段、调用路径查清楚。

负责人建议：欧米主导，稳米协助。

产出：
- 数据源入口清单。
- 字段策略初版。
- “哪些脚本绕过 fetch”清单。

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

### Phase 1：建立 Field Policy v1

**目标**：把 `pipeline_coverage.json` 从说明文档升级成机器可读策略。

负责人建议：欧米设计，黑米或稳米落地。

建议新增：
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/ym_stock_data/policy.py`
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/ym_stock_data/policies/fields.json`
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/tests/test_policy.py`

策略内容：
- 字段名。
- 所属 intent。
- 主源。
- 备源。
- 刷新频率。
- 口径。
- 限流等级。
- 可否盘中使用。
- 可否直接参与交易判断。

验收：
- `python3 -m pytest tests/test_policy.py -v` 通过。
- 任意字段能查到来源策略。
- 找不到策略的字段返回明确错误，不静默猜测。

### Phase 2：新增 Intent Router

**目标**：新增场景入口，不让 Agent 和业务脚本直接记物理源。

负责人建议：欧米设计，黑米落地，欧米验收。

建议新增：
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/ym_stock_data/resolve.py`
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/tests/test_resolve.py`

建议 API：

```python
from ym_stock_data import resolve

resolve("realtime_market")
resolve("realtime_quotes", codes=["600519"])
resolve("sentiment_intraday")
resolve("review_sentiment")
resolve("single_stock_research", code="600519", name="贵州茅台")
resolve("topic_screening", query="机器人概念 近5日涨幅为正 非涨停")
```

规则：
- `fetch()` 保留为底层兼容入口。
- `resolve()` 成为 Agent 和业务场景优先入口。
- `resolve()` 返回标准 `_meta.intent`、`source_chain`、`data_scope`、`confidence`。

验收：
- 每个核心 intent 有测试。
- 问财失败时会按策略降级。
- 高频 intent 不调用问财。

### Phase 3：收敛 live-dashboard 消费层

**目标**：看板只调用 `resolve()` 或受控 collector，不直接调用 source。

负责人建议：稳米/黑米执行，欧米验收。

重点文件：
- `/Users/yimu/Documents/YM_Capital/live-dashboard/scripts/collectors/quotes.py`
- `/Users/yimu/Documents/YM_Capital/live-dashboard/scripts/collectors/iwencai_poll.py`
- `/Users/yimu/Documents/YM_Capital/live-dashboard/scripts/collectors/market_data.py`
- `/Users/yimu/Documents/YM_Capital/live-dashboard/scripts/snapshot_auction.py`
- `/Users/yimu/Documents/YM_Capital/live-dashboard/scripts/bridge.py`
- `/Users/yimu/Documents/YM_Capital/live-dashboard/config/sources.yaml`

原则：
- 高频 collector 只走实时 intent。
- 盘中情绪 collector 只走 `sentiment_intraday`。
- 竞价快照只走 `auction_snapshot`。
- 桥接层只合并缓存，不自己决定数据源策略。

验收：
- 看板启动后 `/api/health` 能显示各数据源状态。
- PyTDX 失败时不清空上一笔有效行情。
- 问财失败时情绪模块降级明确显示。
- 不出现同一字段多个口径互相覆盖。

### Phase 4：规范 Skill 和 Agent 使用

**目标**：让各米查询数据时遵循统一规则。

负责人建议：欧米写规范，稳米同步 WorkBuddy，洋米同步 Claude，黑米同步 Cursor。

需要更新：
- `/Users/yimu/.agents/skills/ym-a-stock-pipeline/SKILL.md`
- `/Users/yimu/.codex/skills/ym-a-stock-pipeline/SKILL.md`
- `/Users/yimu/Documents/Mi_Agents/skills/registry.md`
- WorkBuddy 对应 skill。

规则：
- Skill 中保留“什么时候用什么”，不复制大量实现细节。
- 所有 A 股结构化数据优先 `resolve()`。
- 问财作为 `resolve()` 的一个策略源，不再让每个 Skill 自己写降级链。
- TDX MCP 标注为“交互补盲/投研补充”，不默认替代实时管道。

验收：
- 各 Agent 的 A 股查询说明口径一致。
- 不再出现“NeoData 优先覆盖所有金融数据”的旧口径。
- 红方、复盘、监控类 Skill 明确哪些字段必须走统一管道。

### Phase 5：样例库和回归测试

**目标**：用固定样例防止后续工具越加越乱。

负责人建议：稳米整理样例，欧米设计测试，黑米补测试。

建议新增：
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/tests/fixtures/query_cases.json`
- `/Users/yimu/Documents/YM_Capital/YM-data-pipeline/tests/test_query_cases.py`

样例类型：
- 实时行情：指数、自选股、板块。
- 情绪：昨日涨停收益、连板收益、封板率、炸板率。
- 复盘：连板梯队、龙虎榜、涨停原因。
- 单票：600519、688017、300476。
- 补盲：问财查不到时 TDX MCP/NeoData 的用法记录。

验收：
- 样例可重复跑。
- 输出带 `_meta`。
- 字段口径不变时测试稳定。

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
- 找出所有绕过 `ym_stock_data.fetch()` 或未来 `resolve()` 的调用。

命令建议：

```bash
cd /Users/yimu/Documents/YM_Capital
rg -n "ym_stock_data|iwencai|pywencai|PyTDX|pytdx|tdx|NeoData|Tushare|akshare|easyquotation" .
```

输出：
- 文件路径。
- 调用源。
- 所属场景。
- 是否应迁移。

验收：
- 清单覆盖 `YM-data-pipeline`、`live-dashboard`、相关 Skill。

### 任务 B：字段策略表 v1

执行者：欧米设计，黑米落地。

目标：
- 建立 `fields.json` 和读取 API。

关键字段必须覆盖：
- 指数、成交额、上涨家数、下跌家数。
- 涨停家数、跌停家数、封板率、炸板率。
- 昨日涨停收益、连板收益、炸板收益、晋级率。
- 最高板、次高板、连板股列表。
- 个股最新价、涨幅、量比、换手、MA5/MA10/MA20。
- 板块涨跌幅、板块资金、北向资金。
- 研报、公告、新闻。

验收：
- 每个字段都有主源、备源、口径、刷新频率。

### 任务 C：Intent Router v1

执行者：黑米执行，欧米验收。

目标：
- 新增 `resolve()`。

必须支持：
- `realtime_market`
- `realtime_quotes`
- `realtime_sectors`
- `sentiment_intraday`
- `auction_snapshot`
- `review_sentiment`
- `single_stock_research`
- `topic_screening`

验收：
- 核心 intent 的测试通过。
- 低频 intent 可以调用问财。
- 高频 intent 不调用问财。

### 任务 D：看板接入

执行者：稳米或黑米。

目标：
- live-dashboard collector 改为按 intent 使用管道。

验收：
- 看板本地启动成功。
- `/api/health` 正常。
- 手动禁用 PyTDX 后降级路径明确。
- 问财失败不会影响实时行情。

### 任务 E：Skill 同步

执行者：稳米主导，欧米审。

目标：
- 各 Agent Skill 统一数据源规则。

验收：
- `ym-a-stock-pipeline` 成为 A 股查询总入口说明。
- `iwencai-data` 被定义为问财专项，不再承担全局路由。
- `tushare` 明确为最后补盲。
- TDX MCP 明确为投研补充和问财补盲。

### 任务 F：样例和回归

执行者：稳米整理，黑米补测试。

目标：
- 建立 query cases。

验收：
- 每个核心场景有样例。
- 输出能标注 source、query、口径。

---

## 8. 风险和控制

### 风险 1：过早把 TDX MCP 放进生产链路

控制：
- TDX MCP 短期只作为投研补充。
- 进入实时链路前必须有 20 个以上样例验证。

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
- 先新增 `resolve()`，不删除 `fetch()`。
- live-dashboard 分模块迁移。
- 每次迁移保留上一笔有效数据保护。

---

## 9. 推荐优先级

第一优先级：
1. 数据入口审计。
2. Field Policy v1。
3. Intent Router v1。

第二优先级：
4. live-dashboard 高频链路接入。
5. 复盘/竞价固定 query 接入。

第三优先级：
6. Skill 同步。
7. TDX MCP 样例库。
8. NeoData/Tushare 补盲规范。

---

## 10. 最小可行版本

如果先做一个小闭环，建议只做这 4 件：

1. 新增 `resolve("realtime_market")`，内部走 `fetch("index")`。
2. 新增 `resolve("review_sentiment")`，内部走固定问财 query 模板。
3. 新增 `fields.json`，覆盖 20 个最关键字段。
4. 更新 `ym-a-stock-pipeline` Skill，告诉各米优先用 `resolve()`。

这个版本不影响看板现有运行，但能先把“各米临时查询”和“复盘查询”收拢。

---

## 11. 验收标准

整体治理完成的标准：

- Agent 查询 A 股数据时能先判断 intent，而不是凭经验选工具。
- 看板实时链路不依赖问财、MCP、联网搜索。
- 复盘情绪链路固定问财 query，并保留原始 query 和 source。
- 单票调研能组合多个源，但每个字段有来源和口径。
- 所有关键字段可从策略表查到主源和备源。
- 各 Skill 不再互相冲突。
- 失败时暴露事实：哪个源失败、用了哪个降级、数据新鲜度多少。

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
- 高频看板链路不接问财、TDX MCP、联网搜索。
- 问财 query 要固定模板并保留原始 query。
- 涉及主力资金、板块涨停数、情绪指标必须标注口径。
- 改完必须跑相关测试或本地验证，失败要如实汇报。
```
