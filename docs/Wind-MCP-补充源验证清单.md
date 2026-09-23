# Wind MCP 补充源验证清单

## 当前定位

- 状态：`registered_experimental`
- 显式入口：`ym_stock_data.query("wind_enrichment", ...)`
- 默认行情路由：关闭
- 自动 fallback：仅 `filings`，且排在巨潮与 TDX 公告之后
- 交易用途：禁止单独触发交易判断或交易授权

Wind 当前只补充研究证据，不替换 PyTDX、腾讯、东方财富、问财、巨潮等现有来源，
不接实时行情、K 线、分钟数据、新闻、泛选股或 `stock_event` 自动路由。

## 已开放范围

1. 公司档案与业务映射
2. 财务与增长指标
3. 股本、股东、实控人与限售
4. 增发、并购、ST、分红等公司事件
5. Beta、波动率、Sharpe、VaR 等风险指标
6. 指数 PE/PB/PS 与历史分位
7. 公司公告和定期报告检索

实时行情、K 线、分钟行情、选股筛选、新闻和通用 analytics 暂不开放。

## 单例记录字段

每次验证至少记录：

- 查询时间与交易日
- capability 与原始问题
- Wind 工具路由
- 对照主源及其快照时间
- 关键字段、单位、币种、报告期、复权/合并口径
- 一致字段、差异字段、缺失字段
- Wind 调用状态、错误码、额度影响
- 采用结论：`supporting` / `conflicting` / `unusable`

不得把 Key、完整配置文件或认证信息写进记录。

## Gate 1：离线契约

- [x] Wind 显式增强统一进入 canonical `query()`；旧 experimental sidecar 已完成 parity 后删除。
- [x] capability 白名单不含实时行情、K 线和选股。
- [x] Key 不进入 subprocess 参数。
- [x] 超时、认证错误、无 CLI 都形成固定枚举 error code，不泄露错误正文。
- [x] CLI 成功但 Wind 数据包带错误时仍为 provider error。
- [x] 公告 fallback 仅接受明确的 `filings: list`；缺容器、错类型或泛化文本不能终止链。
- [x] capability manifest 从实际 registry/routes 派生，标明 `default_route=false`。
- [x] 2026-07-29 Task 10 全量 210 项单元测试通过（`ResourceWarning` 视为 error）；提交前继续执行 diff gate。
- [x] 2026-07-22 完成 `company_profile` 最小真实探针，认证成功且返回非空数据。

## Gate 2：20 例字段对账

至少完成 20 例，其中：

- [ ] 财务指标 6 例，覆盖利润表、资产负债表、现金流和成长性。
- [ ] 股东/股本 3 例，覆盖实控人、前十大股东和限售。
- [ ] 公司事件 3 例，覆盖并购/再融资、分红和风险事件。
- [ ] 指数估值 3 例，覆盖 PE、PB 和历史分位。
- [ ] 公告检索 3 例，核对公告标题、日期和官方原文。
- [ ] 风险指标 2 例，核对观察窗口和计算口径。

任何数值差异必须先检查报告期、单位、币种、复权、合并口径和更新时间，不能直接判定某一方错误。

## Gate 3：连续 5 个交易日稳定性

- [ ] 连续 5 个交易日完成至少一例真实调用并留存非 Git 快照。
- [ ] 记录成功率、P50/P95 延迟、空结果率、错误码分布和积分消耗。
- [ ] 无 Key 泄露、错误自动重试或调用风暴。
- [ ] 现有消费者与非公告降级链保持不变。

## 晋升规则

只有 Gate 1-3 全部完成后，才能讨论把公告以外某一个明确字段族提升为
`fallback_candidate`。晋升必须逐字段审查并新增 policy、fixture、质量语义和消费者验证；
不得整体把 Wind 提升为默认源，也不得把 Wind Alice 接入数据路由。公司事件各 subtype
在字段级等价性证明完成前，保持显式 `wind_enrichment`/cross-check，不注册自动 fallback。

未完成晋升时，研究报告只能把 Wind 标为补充证据或交叉验证来源；若与公告或现有主源冲突，
保留冲突并回到官方公告、交易所或公司披露核查。
