# StockToday 接入边界

本文件是 Agent-facing 说明，canonical checkout 为
`/Users/yimu/Projects/YM_Capital/YM-data-pipeline`。Agent 和新消费端只能使用
`from ym_stock_data import query` 或仓库根 `./ym-data`；provider 选择、顺序、
鉴权、质量判断和 evidence 绑定由管道负责。

历史原生接入的更正与验收材料只保留在
`shared/audits/2026-09-22-stocktoday-native/`。该目录是历史证据，禁止作为 Agent runtime entry、
安装来源或旁路运行时。

## 凭据边界

凭据值不写入文档、代码、日志、receipt、命令参数或环境回执。Agent 只提示用户
通过 canonical CLI 完成受控操作：

```sh
./ym-data auth set-stocktoday --stdin
./ym-data auth status-stocktoday
./ym-data doctor --json
```

除此之外不记录凭据存储实现、账户标识、内部目录或 provider 专用客户端细节；这也是
旧消费者 compatibility wrapper 与 Agent public contract 的边界。

## 公共查询入口

标准化行情由 canonical RouteSpec 按用途自动选择来源。默认 Agent 查询的
`stock_snapshot`、日/周/月/分钟 `stock_kline`、
`market_limit_state`、`market_limit_board`、`market_hot_rank`、`market_intraday_state`
和默认 `review_sentiment` 以 StockToday 为第一源（含 `realtime_poll`），腾讯是个股后备；
大盘 `realtime_market` 与 `index_intraday_compare` 由腾讯作首要源（套餐不含指数实时）；
当前路由与发布边界见 [渠道工作区总览](README.md)。异常、超时或允许继续的合法空集时，
只有存在语义等价后备的 intent 才按固定顺序降级。长尾接口使用
`stocktoday_data`，保留 provider-native 字段：

```python
from ym_stock_data import query

limits = query(
    "stocktoday_data",
    api_name="limit_list_d",
    params={"trade_date": "20260921", "limit_type": "U"},
)
bars = query(
    "stock_kline",
    code="600519",
    period="60m",
    count=10,
)
print(limits["_meta"])
```

```sh
./ym-data intent "查涨停板"
./ym-data intent "查实时个股" 'codes=["600519","000001"]'
./ym-data query stocktoday_data api_name=limit_step 'params={"trade_date":"20260921"}'
```

Agent 不读取私有 inventory、不调用私有模块、不拼接 provider fallback，也不把未确认的
参数或结果扩写成事实。未覆盖请求先使用本地目录命令，再停在 public contract：

```sh
./ym-data stocktoday catalog --json
./ym-data stocktoday catalog ths_hot --json
```

目录中没有匹配方法或参数时，返回明确的 unsupported 参数或 `source_gap`。

## 本地预算边界

代码缺省值就是文档口径的 **80 次/分钟、18000 次/日**（供应商 Skill 记 100/20000，
超限报 `请求超限20000次`；缺省取更保守的一档）。环境变量 `YM_STOCKTODAY_PER_MINUTE`
/ `YM_STOCKTODAY_PER_DAY`（兼容 `YIMU_` 前缀）可显式覆盖；写 `0` 是显式关闭上限，
非法值回落到缺省而不是静默失守。跨进程计数在 SQLite 事务内完成，分钟窗口与上海
日界（UTC+8 零点）都按真实时间判定；失败的请求同样计入额度（本地登记量**不等于**
供应商成功调用量，也不等于已购额度）。

- 预算库位置可用 `YM_STOCKTODAY_BUDGET_PATH` 指向隔离目录（测试、旁路诊断）；
  旧库只做 `ALTER TABLE ADD COLUMN` 迁移，不清历史登记。
- 预算耗尽的失败码是 `LOCAL_BUDGET_EXHAUSTED`。provider 的
  `provenance.local_budget` 与公共 query 的 `_meta.local_budget` /
  `_meta.attempt_diagnostics[kind=local_budget]` 都给出脱敏归属：`scope=minute|day`
  （**日额度严格优先于分钟窗口**）、`used`、`cap`、`wait_seconds`（仅分钟：真实滚动
  60 秒窗口内最早一条滑出的秒数，≥1 且 ≤60；日额度为 null）、`requester={pid,source}`
  与当前分钟按来源的计数。来源标签由 `YM_STOCKTODAY_REQUEST_SOURCE` 显式提供
  （如 `tianyuan`、`bots`、`cli`），不写凭据。公共契约见
  `shared/implementation/2026-11-03-repairs/MW/pipeline-interface.md`。
- 父目录不存在时首次使用会自动建目录（正常初始化）；只有文件存在却读不通才按
  `LOCAL_BUDGET_UNAVAILABLE` fail closed。
- 429 与「IP 异常临时封禁」这类 403 都是**供应商限流**，返回 `RATE_LIMITED` 并落盘
  冷却：优先用 `Retry-After` 或正文「预计 N 分钟后解封」，解析不到时用保守默认
  （限流 60 秒、临时封禁 15 分钟，夹在 60 秒~1 小时）；并发写冷却取较晚截止，
  不会被后到的短冷却缩短。冷却期间不发请求、不消耗额度。
- 真正的 401/403 鉴权失败仍是 `AUTH_DENIED`，不冷却、不缓存；公共信封的
  `_meta.reason_code` 让鉴权失败严格优先于预算/限流诊断。冷却状态在
  `_meta.vendor_cooldown`（provider、`vendor_reason`、`retry_after_seconds`、
  `cooldown_seconds`、`cooldown_persisted`），预算在 `_meta.local_budget`；两者都
  带 `provider`，不会冒充当前成功数据。冷却/预算状态库读不出来时按
  `LOCAL_BUDGET_UNAVAILABLE` fail closed，不当作「无冷却」继续打上游。
- 消费链只对传输层瞬时失败（`TIMEOUT`/`NETWORK_ERROR` 等）重试一次；限流、预算
  与鉴权失败原样上报，不再重试。

2026-09-27 实测（`shared/archive/research/ssot-audit-2026-09-27/raw/phase0/stocktoday_probe_20260927.txt`）：

- `token_info` 在本网关返回 HTTP 404“接口不存在”，无法用它核对套餐和额度；
  额度按上段供应商文档执行，待弈沐确认实际购买档位。
- `rt_idx_k`、`rt_idx_tick`、`rt_sw_k`、`idx_mins` 返回“该接口为龙虾套餐专属”，当前套餐不含；
  2026-09-28 弈沐确认暂不加购：默认路由不再调用这些接口，指数由腾讯、板块由同花顺作首要源；
  显式调用时适配器记为 `PLAN_NOT_ENTITLED`，30 分钟内不再调用。
  拒绝记录按 token 指纹（SHA-256 前 8 位）分键，换 token 自动失效；`./ym-data stocktoday entitlement --refresh`
  绕过缓存逐个探测 `rt_idx_k`、`rt_idx_min`、`rt_idx_tick`、`rt_sw_k`、`idx_mins`、`rt_k`、`rt_min`、`stk_limit`。
  开通检查只要求 `rt_k`、`rt_min`、`stk_limit`（`required_ok`，决定退出码）；其余五个拒绝记为
  `outside_plan`（套餐外，预期），若某天变成 `entitled` 说明已加购，可评估切回。
- `rt_k` 通配 `6*.SH`、`0*.SZ`、`3*.SZ`、`*.BJ` 四批覆盖 5527 只，科创板每只重复一行（按
  `updated_at` 取最新）；003xxx 共 42 只任何写法都不返回，个股报价由腾讯逐只补。
- 休市日 `rt_k` 把上一交易日行情盖成当天日期（9-25 行 = 9-24 收盘），管道按交易日历改回，
  原始时间保留在 `vendor_quote_time`。
- `stk_limit`、`limit_list_d`、`limit_step`、`rt_min`、`ths_daily`、`moneyflow_ind_ths` 可用；
  网关偶发超时；约 8% 的涨跌停池读取会返回假空表，三池空结果连续读 3 次都空才判为 empty。

## 目录与数据口径

方法目录只定义显式 `stocktoday_data` 的方法与参数边界，不证明单个长尾方法当前可用。
目录查询不联网、不读取凭据；实际调用仍必须检查 canonical `_meta`。标准化 intent
使用统一股票代码、日期、单位、复权说明与事实时间；长尾 `stocktoday_data` 保留
provider-native 口径，不能被调用方当成已经标准化的数据。

结果必须保留 contract 1.0 及 V3 `_meta`：
`pipeline_version`、`route_policy_version`、`source_tier`、
`policy_evidence_sha256`、真实 `provider_used`、完整 `attempts`、`quality`、
`fetched_at` 和 source gap/reason codes。缺字段、过期、截断、非法空集、provider
error 或未验证状态不能被补成成功。

## 标准化质量语义

- `stock_snapshot` 和 `stock_kline` 只按 canonical contract 返回；缺失字段不补零，
  不猜测均线或收盘状态。
- `_meta.fetched_at` 是抓取时间；实际数据时点、年龄、缺票和截断信息留在
  observation/quality 中。
- `stocktoday_data` 不自动翻页；部分返回必须保留完整性缺口。
- 第一源与后备顺序只由 canonical RouteSpec 决定；调用方不能自行改序或混合字段。

任何历史安装命令、原生运行时、私有客户端、内部协议或 provider-specific 路径都
不属于本 Agent contract；需要核对时只能查看上述历史证据，并不得将其作为 Agent
runtime entry。
