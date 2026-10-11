# 数据管道到 YiMu_IR 研究升级总实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to按阶段执行。除非弈沐明确授权并行 Agent，否则单 Agent 顺序推进；每个 Gate 通过后再进入下一阶段。

**Goal:** 在不改变 YM-data-pipeline 现有 V1/V2 兼容边界、不影响 live-dashboard 等生产消费者的前提下，先补齐研究所需的数据能力，再升级 YiMu_IR 研究 Skill，使其能按研究场景生成可审计的证据包、报告和质量缺口。

**Architecture:** YM-data-pipeline 继续是 A 股结构化事实入口；YiMu_IR Skill 只负责编排、证据分层、研究判断和交付，不复制数据源实现。两项目通过版本化 `capability_manifest` 与研究证据包 JSON 解耦。TDX MCP 保持 Agent 手工交叉验证源，不进入 Python 自动降级链。

**Detailed plans:**

- 数据能力实现细案：[`2026-07-14-external-data-capability-integration.md`](./2026-07-14-external-data-capability-integration.md)
- IR Skill 实现细案：[`2026-07-14-yimu-ir-research-skill-upgrade.md`](/Users/yimu/Documents/YM_Capital/YiMu_IR/docs/superpowers/plans/2026-07-14-yimu-ir-research-skill-upgrade.md)

---

## 1. 不变边界

- 不删除或改名现有 `fetch()` route、五个 V2 intent 和既有返回字段。
- 新能力先 source/V1 旁路，再经 fixture、live smoke、对账后晋升 V2。
- YiMu_IR 在能力未就绪时必须输出 `unavailable` / `source_gap`，不得静默跳过或补猜。
- 行情、K 线、情绪和低频事件优先来自 YM-data-pipeline；公司业务、客户供应关系、财务口径以公告、年报、交易所文件为准。
- TDX MCP 只作人工备用或交叉校验，必须记录 `source=tdx_mcp` 与查询时间。
- 本计划不接真实交易、不改变 live-dashboard 账户事实、不迁移生产消费者。

## 2. 能力覆盖裁决

### 本轮必须纳入

| 能力 | 数据管道落点 | IR 使用场景 | 晋升要求 |
| --- | --- | --- | --- |
| 财联社涨跌停接口修复 | `sources/cls.py` | 情绪、题材强度 | 签名 fixture + live smoke |
| 研报代码筛选修复 | `sources/research.py` | 个股/行业研究 | server code fixture |
| HTTP session 线程安全 | `sources/eastmoney.py` | 所有东财能力 | 并发单测 |
| 北向字段语义修复 | `sources/northbound.py` | 市场背景 | 单位、时间戳、语义测试 |
| 涨跌停结构化事实 | `fetch("market_limit_state")` / V2 | 主题、产业链、复盘 | 双源字段对账 |
| 限售解禁、两融、大宗、股东户数、分红 | `fetch("stock_event")` / V2 | 个股、持仓、事件 | 事件子类型 fixture |
| 问财新闻/研报/公告内容 | `fetch("iwencai_content")` | 催化、证据搜索 | 内容类型 fixture |
| 行业研报 `qType=1` | `fetch("industry_research")` | 主题/产业链 | 代码、行业两类查询 |
| 巨潮动态 `orgId` | `sources/filings.py` | 公告与公司事实 | 代码解析 + 缓存 + 失败契约 |
| V2 `market_limit_state`、`stock_event` | `resolve()` | IR 统一编排 | policy + quality + source chain |
| 能力清单 | `ym_stock_data.v2.capabilities` | IR 运行前探测 | 稳定 schema + 单测 |

### 只作为备用，不阻塞首轮 IR 升级

- 交易所官方龙虎榜/公告：作为东财、巨潮失败后的官方备用设计，先做接口调查与 fixture，不强行在首轮接入全部交易所。
- 通达信概念/互动易/热榜：保留在观察池；除非证明能填补当前明确空洞，不引入第二套概念分类 SSOT。
- 分钟级资金流与 120 日资金流：当前不作为基础研究必要条件，避免把不稳定流量指标升级成核心事实。
- ETF 期权、`mootdx`、`stockstats`、关闭 TLS 校验：本轮明确不做。

## 3. 跨项目能力契约

### 3.1 新增能力清单

**Files:**

- Create: `ym_stock_data/v2/capabilities.py`
- Modify: `ym_stock_data/v2/__init__.py`
- Test: `tests/test_v2_capabilities.py`

实现只读函数，不发网络请求：

```python
CAPABILITY_SCHEMA_VERSION = "1.0"


def capability_manifest() -> dict:
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "v2_intents": {
            "realtime_market": {"status": "stable"},
            "sector_index": {"status": "stable"},
            "stock_snapshot": {"status": "stable"},
            "stock_kline": {"status": "stable"},
            "review_sentiment": {"status": "stable"},
            "market_limit_state": {"status": "experimental"},
            "stock_event": {"status": "experimental"},
        },
        "v1_routes": {
            "iwencai_content": {"status": "experimental"},
            "industry_research": {"status": "experimental"},
            "research": {"status": "stable"},
            "filings": {"status": "stable"},
            "news": {"status": "stable"},
        },
        "manual_sources": {
            "tdx_mcp": {"status": "manual_cross_check_only"},
        },
    }
```

契约规则：

- 只允许 append-only 添加能力；删除或改名必须提升 `schema_version`。
- `stable` 可进入默认研究编排；`experimental` 只有 IR 场景显式需要时调用。
- `unavailable` 不从清单删除，由调用结果的 quality gate 记录。
- Skill 不读取源模块内部函数，不依据文件是否存在推断能力。

测试：

```bash
uv run python -m unittest tests.test_v2_capabilities -v
```

### 3.2 研究证据包契约

YiMu_IR 生成的 JSON 顶层固定为：

```json
{
  "schema_version": "1.0",
  "research_type": "theme",
  "created_at": "2026-07-14T12:00:00+08:00",
  "request": {},
  "capability_manifest": {},
  "calls": {},
  "source_summary": {},
  "quality_gate": {
    "status": "normal",
    "missing_capabilities": [],
    "source_errors": [],
    "stale_calls": [],
    "contradictions": []
  },
  "candidate_universe": [],
  "records": []
}
```

`quality_gate.status` 只有三种：

- `normal`：核心能力齐全，允许进入结论层。
- `partial`：部分数据失败，允许形成研究观察，但报告必须展示缺口。
- `blocked`：关键事实缺失或互相矛盾，不允许生成确定性排名/交易语言。

## 4. 执行顺序与 Gate

### Phase 0：冻结基线与脏工作区裁决（0.25 天）

- [ ] `git status --short`，逐项标记现有改动归属。
- [ ] 跑现有 V1/V2 单测并保存基线结果。
- [ ] 确认不把当前未提交改动覆盖、回退或混入无关提交。

```bash
cd /Users/yimu/Documents/YM_Capital/YM-data-pipeline
uv run python -m unittest tests.test_v2_mvp tests.test_v2_quality -v
git diff --check
```

**Gate 0:** 基线失败必须先记录，不得把旧失败算成新回归。

### Phase 1：旧能力修复（0.75—1 天）

执行数据细案 Task 1—3：CLS、研报筛选、Eastmoney 线程安全、北向语义。

**Gate 1:** 旧 route 字段不删除；目标测试和现有 V2 测试通过；live smoke 的失败能输出真实源与错误类型。

### Phase 2：研究能力旁路（1—1.5 天）

执行数据细案 Task 4—7，并补三项：

1. `industry_research` 支持行业名与股票代码，底层明确 `qType=1`。
2. 巨潮公告查询按股票代码动态解析 `orgId`，短期缓存，解析失败不得复用错误公司 ID。
3. `capability_manifest()` 暴露当前稳定/实验能力，供 IR 探测。

**Gate 2:** 每个新 route 均有成功、空结果、超时/异常 fixture；没有消费者自动迁移；TDX MCP 未写入自动 fallback。

### Phase 3：V2 晋升与双轨观察（0.75—1 天 + 5 个交易日）

- [ ] 晋升 `market_limit_state`、`stock_event`。
- [ ] 校验 `_meta.source`、`source_chain`、`quality`、时间戳。
- [ ] 连续五个交易日保存双轨快照，比较条数、核心字段、异常率和时效。
- [ ] 每日仅保存缓存目录，不提交行情快照。

**Gate 3:** 兼容测试全绿；五日内没有静默空结果；差异均能解释为时间、口径或源故障，而非错误映射。

### Phase 4：升级 YiMu_IR Skill（1—1.5 天）

按 IR 细案执行，先建立研究证据包，再改 Skill 路由和文档。不要把 API 大全直接写进 `SKILL.md`。

**Gate 4:** Skill 能处理主题/产业链、完整个股、事件映射、持仓研究四种前向案例；缺失能力会进入 `quality_gate`；HTML、JSON、index 和 QA 契约一致。

### Phase 5：跨项目验收（0.5 天）

固定四个案例：

1. 主题/产业链：宽问财失败时按环节拆查询，保留候选池来源。
2. 完整个股：行情、K 线、业务/公告、研报、事件、同业与风险齐全。
3. 事件映射：从新闻/公告事实到 A 股受益链，区分直接、间接、蹭概念。
4. 持仓研究：账户事实只读 live-dashboard；研究 Skill 不推测持仓、不发交易授权。

**Gate 5:** 每个案例都产出证据包；至少一个降级案例证明 `partial/blocked` 生效；旧五 intent 和现有消费者无回归。

## 5. 工期与停机点

| 阶段 | 主动实施时间 | 可独立停止 |
| --- | ---: | --- |
| 基线 + 旧能力修复 | 1—1.25 天 | 是 |
| 新研究能力 + manifest | 1—1.5 天 | 是 |
| V2 晋升 | 0.75—1 天 | 是 |
| 五交易日观察 | 每日 15—30 分钟 | 必须等观察完成 |
| IR Skill 升级 | 1—1.5 天 | 是 |
| 跨项目验收 | 0.5 天 | 最终 Gate |

总主动工时约 **4.25—5.75 个工作日**，自然历时约 **7—10 天**（包含 5 个交易日观察）。若只做到“数据修复 + IR Skill 灰度版”，约 **3—4 个工作日**，但不能宣称生产级稳定。

## 6. 提交与回滚

- 数据源修复、新 route、V2 intent、capability manifest、IR Skill 分开提交。
- YiMu_IR 当前若仍非 Git 仓库，实施前先向弈沐确认由哪个上级仓库托管；计划文档本身不擅自初始化 Git。
- IR Skill 读取 manifest 后才启用新调用；回滚 Skill 时不需要回滚数据源。
- 任一新能力异常时降为 `experimental` / `unavailable`，不删除旧能力、不改变消费者默认路由。

## 7. 完成定义

- [ ] 数据管道详细计划的必做项完成并验证。
- [ ] `capability_manifest()` 有版本、有测试、与真实实现一致。
- [ ] YiMu_IR Skill 不再只列五个简单调用，而是按场景编排证据。
- [ ] 研究证据包能解释来源、时间、失败、缺口和矛盾。
- [ ] 四个前向案例通过，HTML/JSON/index/QA 齐全。
- [ ] 不影响现有框架、生产消费者和交易授权边界。
