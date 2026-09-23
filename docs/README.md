# 渠道工作区总览与后续测试

核对日期：2026-09-23（Asia/Shanghai）。V3 统一入口已发布，可在此基础上继续业务测试；
不承诺所有上游永远可用，也不把本机试用代码算作生产功能。本次收尾只整理文档。

## 从哪里开始

| 要做的事 | 唯一入口或依据 |
| --- | --- |
| Agent 查询和意图参数映射 | [全局 Skill](../skills/ym-a-stock-pipeline/SKILL.md)，公共 `query()` / 根目录 `./ym-data` |
| 安装、运行环境 | [INSTALL](INSTALL.md) |
| StockToday 目录、认证与额度边界 | [STOCKTODAY](STOCKTODAY.md) |
| 标准结果、质量、开发边界 | [agent-contract](agent-contract.md) |
| 确认实际路由 | [routing.py](../ym_stock_data/routing.py)，以代码为准，本页只列常用路由 |
| 哪些消费端已接入 | [生产消费端清单](production-consumers-v3.md) |
| 发布版本、回读、回滚依据 | [2026-09-23 发布记录](releases/2026-09-23-v3-production.md) |

`superpowers/plans`、`superpowers/progress` 和日期化审计材料保留作历史证据，
不作为恢复全接口审计、JEV 或新增框架的任务单。后续按真实业务失败定位，不重复重建管道。

## 已发布与本机试用分开

| 对象 | 收尾时核对的状态 |
| --- | --- |
| Hermes 管道 | HEAD `1275298`；运行代码 `62e776f`；包版本 `3.0.0` |
| Hermes 看板 | HEAD `214d024`；运行代码 `dffd4c9`；systemd 服务 active |
| 本机 Market Watch / YiMu IR / shadow | 已接入公共入口，按需运行；Hermes 无对应实例，见消费端清单 |
| 本机 Agent Skill | Codex、agents、Claude、WorkBuddy 四入口链接到仓库同一份 Skill |
| 本机 `market-facts` | 尚未提交的本地试用及配套 Skill/README 修改；未发布 Hermes，不属于生产验收 |

本机开发分支与 Hermes 生产分支提交号不同，不能仅凭 hash 不同判断漏同步。
核对的是已发布代码及进程加载版本。本机 8088 是 Hermes 隧道，18088 是代理。
发布过程中的旧版本和“未部署”陈述均按时间理解，不覆盖发布记录末尾更新。

## 常用固定路由

消费者只声明用途、业务参数，不自己选择 provider 或拼后备链。

| 能力/用途 | 固定顺序 |
| --- | --- |
| 高频个股 `stock_snapshot(use_case="realtime_poll")` | 腾讯 → PyTDX → TDX |
| 高频大盘 `realtime_market(use_case="realtime_poll")` | PyTDX → 腾讯 → 东方财富 |
| 默认 Agent 个股 | StockToday → 腾讯 → PyTDX → TDX |
| 默认 Agent 大盘 | StockToday → 腾讯 → PyTDX → 东方财富 |
| 股票日/周/月 K 线 | StockToday → 东方财富 → 腾讯 → PyTDX → TDX |
| 股票分钟 K 线 | StockToday → PyTDX → Sina → TDX |
| 指数 K 线 | StockToday → 东方财富指数 → Sina 指数 → PyTDX 指数；按周期能力筛选，Sina 支持分钟、PyTDX 支持日/周/月 |
| 三指数分钟比较 | 东方财富指数 → Sina 指数 → StockToday |
| 涨跌停板 | StockToday → 东方财富涨跌停池 |
| 同花顺/东财热榜 `market_hot_rank`、市场资金流 `fund_flow` | StockToday；当前无已验证的等价后备 |
| 其他已开通接口 | 先查 catalog，再通过 `stocktoday_data` 直达；结果保留上游原生字段，不冒充标准化结果 |

高频个股改为腾讯第一源，是针对 Hermes PyTDX 个股批量报价过旧的已发布修复。
不能再把腾讯返回记为 PyTDX 直连成功。PyTDX 大盘直连是否成功独立判断。
额度配置不再假设每天 5,000 次：本地默认不设臆造上限；真实购买额度仍须以账户权益核实，
上游 429/限流不会被绕过，详见 StockToday 文档。

## 验收时必须读的字段

- 同时读 `_meta.status`、`provider_used`、`attempts`、`quality`、`source_tier` 和缺口；不能仅看 HTTP 200 或看板总健康状态。
- 原始 `quote_time` / K 线时间与 `fetched_at` 分开；旧数据不能重新标成当前行情。盘中、午休、收盘按交易时段质量规则判断。
- K 线核对股票/指数代码、日期范围、时间字段、`volume_unit=share`、`amount_unit=CNY` 及请求的复权方式。`none` 失败不能用 `qfq` 顶替。
- 缺票、缺字段、过期和大盘缺指数应继续尝试语义兼容后备；没有后备时明确缺口，不补零或宣称成功。
- 无日期热榜取最近已完成交易日；以实际返回日期为准。当前日历仅覆盖 2026 年，跨年须维护日历，不能猜工作日。

## 下一轮测试入口

从仓库根目录执行。下面查询是按需联网的业务测试，不是要求全量探测接口；
日期例子固定为发布日，测试其他交易日时显式替换。

```sh
./ym-data doctor --json
./ym-data intent "查同花顺热榜"
./ym-data intent "查涨停板" 'trade_date="20260923"'
./ym-data intent "查实时个股" 'codes=["600519","000001"]'
./ym-data query stock_snapshot 'codes=["600519","000001"]' 'use_case="realtime_poll"'
./ym-data query realtime_market 'use_case="realtime_poll"'
./ym-data query stock_kline 'code="600519"' 'period="daily"' 'count=5' 'adjustment="none"'
./ym-data query stock_kline 'code="600519"' 'period="daily"' 'count=5' 'adjustment="qfq"'
./ym-data query index_intraday_compare 'period="15m"' 'trade_date="20260923"'
./ym-data stocktoday catalog ths_hot --json
```

`doctor` 和 catalog 是离线配置/目录检查，不证明上游在线。业务查询后再 GET
`/api/health` 与 `/api/live/quotes`，核对实际消费端的代码、日期、覆盖、来源和时间。
不要为了测试运行回填、覆盖缓存、重启服务或向真实 8088 发 POST。

主源故障测试仅在隔离测试进程注入 timeout、过期、缺字段等响应：检查 attempts 顺序、
实际后备、数据口径与缺口。禁止为测试修改生产鉴权或禁用生产源。
每条记录写清：测试时间、环境/版本、请求参数、返回日期、实际来源、耗时、覆盖、
质量缺口、是否满足业务要求。代码测试与生产回读分别记录，不用测试数量代表生产完成。

## 已知限制与停止条件

- StockToday 市场资金流及标准热榜仍有单源边界；上游不可用时只能明确失败，不能编造等价备用。
- 指数分钟比较依靠有预算的后备切换；一次成功不等于持续稳定。需要在实际交易时段继续观察。
- 看板行情完整不代表情绪/问财链路也完整；此前健康总状态仍受问财时效影响，应分别查看。
- 2027 年日历尚未补齐；跨年维护是明确后续项，本页不是自动提醒或已完成承诺。
- `market-facts` 如需纳入正式链路，须另验收其字段、时间、缺口及消费结果；当前不因本次文档提交而获得发布状态。

本轮可结束文档整理并交接业务测试；“零数据缺口”不能作为已达成事实。
后续只修真实失败与契约不一致，不扩大审计范围，不接入实际下单。
