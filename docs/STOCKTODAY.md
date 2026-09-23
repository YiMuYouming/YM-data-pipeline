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

标准化行情由 canonical RouteSpec 自动选择来源。StockToday 已是
`realtime_market`、`stock_snapshot`、日/周/月/分钟 `stock_kline`、
`market_limit_board` 和 `market_hot_rank` 的第一源；异常、超时或合法空集时，
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

管道没有从购买信息或官方 endpoint 获得精确额度，因此不臆造本地限额：
`YM_STOCKTODAY_PER_DAY` 与 `YM_STOCKTODAY_PER_MINUTE` 默认均为 `0`，表示本地不设
假上限。只有在明确掌握真实购买限制时，才通过环境变量显式配置；上游返回的 429、
限流或 breaker 状态仍会保留，跨进程预算锁也继续生效。

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
