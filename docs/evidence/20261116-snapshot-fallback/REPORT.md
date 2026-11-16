# 严格实时自动快照后备逐票保留

2026-11-16，关联天元TY-R042。候选代码`ef4a3bb87d9ea39797f51bfa0061f04d5b707de8`，基点`fddc7455586b29955bdbb882a57efe8df7bad86c`。状态：机制修复、聚焦及独立规格/质量通过，联合发布与实际加载由本轮主控继续核验。

## 根因与最小修复

公共`query('stock_snapshot', quote_age_policy='wall_clock', max_quote_age_seconds=...)`自动路由中，主源失败后的Tencent整批后备原遇一个坏行即拒整批；主源部分成功后的逐码填补已有丢坏保好。新增测试先暴露差异，再仅将严格自动整批后备纳入逐票处理。

沿原完整字段、正价格、代码匹配、非负量额、同日、非未来和自然秒阈值检查；好行保留、坏行剔除，返回前再验时效。默认非严格策略、显式source、K线和其它intent、provider顺序与请求/重试次数保持。

部分合格返回`status=degraded`、`quality.status=partial`，准确保留requested/returned/missing_count/coverage，满足请求=返回+缺失。新增`_meta.snapshot_rejected`仅记录代码到稳定质量原因；原provider、attempts、quote_time、fetched_at及data_as_of可回核，不伪造filled_by_fallback。全坏或最终全到期返回error、provider_used=None、非fresh，保留真实Tencent质量失败attempt，不能形成fresh空成功。

只改`ym_stock_data/api.py`及新增`tests/test_snapshot_fallback_partial.py`，无新依赖、源、表、模块或作业。原15项WIP保持，不修改或混入原README/agent-contract等并行内容。当前新增字段和限定语义由本文补充，不改交易授权。

## 验证

- 最初生产未改前：新7父测试20个子案例预期失败；误用不支持source值的初试保留，修正夹具后重新证明红灯。
- 最终新增9项及相关7组共82项通过；独立规格另7组公共query夹具、独立质量另12组边界通过，含UTC时刻、错码/字段/类型、全坏/全过期及计数守恒。
- 副本全量base1238、candidate1247；相同两个环境错误与5个显式在线跳过，无新增失败。错误分别是个人Skill备份canonical绑定、副本缺实际事实库；未伪造或弱化检查。真实主仓合并前1276项OK、5跳过，合并后全量待接续。
- `compileall ym_stock_data tests`通过；改动2文件敏感路径/字面凭据扫描0发现；有效Ruff api.py既有11项、无新增，新测试0诊断；非空差异检查通过。

api.py SHA256：`10b147605754badcd744c985c860593ebf992b261f53abb953a745522b0c8988`；新测试：`ae603ff57c0356ebc88be8858cf64444102217dcafb0cb89a89640b02b8ebd21`。原始日志在天元主仓`runtime/reviews/2026-11-16/r041-r042/`，含命令、时间和exit；本数据仓没有写生产data/cache。

天元当天失败Tencent原始行未封存，修复证明机制，不能猜定605255或某字段导致10:05/10:31失败。隔离假provider通过不代表自然failover已观察；现存历史失败、降级分钟及缺行情仍保留。实际消费者必须按已有授权重新启动后核加载，不用Git HEAD替代运行证明。
