# 指数查询的可选运行预算

`query("realtime_market", request_budget_seconds=5.0)` 和
`query("index_kline", index_code="000001.SH", period="1m", count=1,
request_budget_seconds=5.0)` 接受有限正整数或浮点数。bool、字符串、None、零、负数、
NaN、无穷及无法转换为有限浮点数的大整数抛 `ValueError`；其他 intent 一律拒绝此参数。
该参数在公共管道内消费，不进入 provider 参数、请求或业务数据。

一个 public query 只有一个单调 deadline。原路由源序、质量门、合法空集语义和重试
保持；每次 provider 构造和调用在既有 `_bounded_provider_call` 中受剩余时间约束。
预算用尽后不启动下一来源，构造完成时已过期也不再调用 provider。
返回后及质量、归一化、信封处理完成后再次检查时间；迟到数据不会作为成功返回。
失败仍通过 canonical `build_result` 返回完整 `data` / `_meta`。

预算路径每个 `(provider, intent)` 仅容纳一个尚未结束的 worker；代码、日期、请求
参数、剩余预算变化不会增加槽。caller 超时不会释放槽，worker 实际结束才按身份清理。
新请求不等待旧 worker、不复用其结果；忙时可按原源序尝试仍有预算的下一个来源。
迟到 worker 的结果只属于已经结束的原调用，后续请求不能读取。

稳定错误码及安全诊断：

| 错误码 | 含义 | `attempt_diagnostics.kind` / `requested` |
| --- | --- | --- |
| `QUERY_BUDGET_EXCEEDED` | 总预算耗尽或结果迟到 | `request_budget`；是否已开始构造/调用 |
| `REQUEST_INFLIGHT` | 相同来源及意图已有未结束 worker | `request_budget` / false |
| `REQUEST_START_FAILED` | 本地 worker 启动失败，槽已释放 | `request_budget` / false |

`attempts` 保留原四字段形状；未发起的后备尝试明确 `requested=false`。诊断不复制
原始异常正文、响应或凭据。上述本地预算/占槽行为不写全局 provider 熔断状态。
不传参数仍使用原同步路径；既有 `index_intraday_compare` 的预算与状态保持独立。

这是工程等待预算，不能强杀 Python 中已进入上游的 worker，也不承诺操作系统硬实时。
worker 卡住期间相应槽保持忙，调用方得到安全缺失；主线程还存在本地调度和信封处理开销。
天元以两个意图合计默认 5 秒传入剩余预算，不改变金融规则、成交条件或行情时效门。

离线回归：`tests/test_index_request_budget.py`、原 `tests/test_compare_budget.py`。
所有新增阻塞测试最终释放并 join worker，未调用真实公共行情、模型或运行账本。
