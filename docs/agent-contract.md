# 数据管道开发契约

仅在修改 provider、路由、契约、凭据或下游迁移时读取相关章节。相对路径以仓库根为准。
当前 canonical checkout 是 `/Users/yimu/Projects/YM_Capital/YM-data-pipeline`。

当前发布状态与后续业务测试见 [渠道工作区总览](README.md)；历史计划不是新增验收门槛。

Agent / Skill 的唯一数据入口是 `from ym_stock_data import query` 与仓库根
`./ym-data`。provider 选择和 fallback 只能由 canonical router 执行，不能由消费端
自行串接；没有匹配能力时停在 public contract 并报告 source gap。

## 公共 API 与契约

新代码只调用 `query(intent, **params)`，不得直接 import `ym_stock_data.sources` 或 `ym_stock_data.v2`。结果统一使用 contract 1.0：`data` 加 `_meta`，其中必须保留 `status`、真实 `provider_used`、完整 `attempts`、`quality`、`fetched_at` 与稳定错误码。

正常、合法空集和失败都只由 canonical `build_result` 构造。参数验证发生在任何 provider 调用前。合法空集是否继续由各 RouteSpec 的 `empty_policy` 决定；带显式 `query` 的 `review_sentiment` 按 `continue_until_exhausted` 策略继续穷尽自然语言 screener，顺序固定为 OpenAPI → pywencai → TDX screener → 专用 Wind `stock_data.search_stocks`。`pytdx_screener` 只保留为实验性显式 provider，不进入 canonical 自动降级链，也不进入正式 live gate。Wind 必须使用 `wind_screener` 专名并严格验证 tabular `Wind代码`，不得借泛化 `wind_mcp` 扩展其它 intent。穷尽不保证有结果。畸形 payload、无效空响应、route 外 provenance、鉴权或 provider 错误必须形成可审计 attempt 并按兼容路由继续。

V1 `fetch()` 与 V2 `resolve()` 仅是 compatibility wrapper：允许维持旧 shape，但不得拥有第二条 provider chain。不要在新文档或脚本中推荐它们，也不要用强制 `DeprecationWarning` 破坏消费者。

V3 metadata 是 contract 1.0 的增量字段：`_meta.pipeline_version`、
`_meta.route_policy_version`、`_meta.source_tier` 和
`_meta.policy_evidence_sha256` 必须与真实 `provider_used`、完整 `_meta.attempts`、
`_meta.quality`、`_meta.fetched_at` 一起保留。quality 的 status、returned_count、
reason_codes、coverage/missing 和明确的 source_gap 不能被空结果或 fallback 覆盖。

StockToday 的 catalog 只说明可请求的 method/parameter 边界，不是 promotion 证据。
catalog presence、HTTP 200 和非空都不等于 accepted primary；主源晋级需要 reviewed
receipt、字段/过滤/分页/时效证据和 active capability policy。

实时行情时效按上海交易所可更新行情的时段计算：09:15–09:25、09:30–11:30、
13:00–15:00；其余时段暂停 60 秒行情年龄计时，但始终核对行情所属交易日，
并保留原始 `quote_time` 和接收 `fetched_at`。午休、收盘后及周末只能接受
最近交易时段末端的行情；开市后超时仍按质量失败继续降级。
StockToday 的 A 股实时报价 observation 与统一质量门共用同一交易时段计龄和 60 秒阈值；不能让适配器
先按自然时间标记 `stale`，使有效的午休或收盘行情提前降级。`index_kline(codes=...)`
要求每个请求指数恰好对应一份有有效 K 线的结果，缺失、重复或代码错配均不能
终止降级链。无日期热榜及非实时资金查询使用管道自带的上交所交易日历；
日历不覆盖的年份报 `CALENDAR_UNAVAILABLE`，调用方可明确传入 `trade_date`。

## Provider 边界

- 按用途执行 RouteSpec（2026-09-27 起）：StockToday 是唯一核心源，腾讯是唯一降级后备（个股逐只补、大盘整组切）；东方财富、新浪、PyTDX、TDX 退出默认路由，只作显式诊断；问财只服务带 `query` 的显式研究。当前套餐只含个股实时：大盘 `realtime_market` 与 `index_intraday_compare` 以腾讯为首要源，指数分钟 K 线从东方财富指数起，盘中板块由同花顺提供，默认路由不再调用 `rt_idx_k`、`rt_sw_k`、`idx_mins`（2026-09-28 正式口径）。
- 时效以 `_meta.data_as_of` 计：盘中 ≤180 秒 fresh、180–600 秒 aging（个股逐只补腾讯）、>600 秒 stale（整组切腾讯）；全市场指标（`market_intraday_state`）不换源、不拼腾讯，失败即报错，由消费端停在最后正常值并标时间。
- 指标（情绪、涨跌停、炸板率、晋级率、最高板、涨停/连板/炸板收益、赚钱效应、连板风险值）只在 `ym_stock_data/indicators.py` 实现，登记在 `v3/indicators.v1.json`，定义以 Vault《交易指标术语表》为准；`tests/test_indicators.py` 会拦截其他位置出现的公式。
- TDX 不接任何默认路由，不调用任意 tool，不做交易写入。
- TDX 登录优先走本仓库 `./ym-data auth login-tdx`；状态只走离线
  `./ym-data auth status-tdx`。默认 macOS Keychain，文件 fallback 必须显式
  `--store file`。若官方授权页只能回到 WorkBuddy，可在弈沐明确授权下做一次性受控凭据迁入并记录 `imported_from=workbuddy`；运行时代码不得扫描、读取或持续同步 WorkBuddy 凭据。
- TDX OAuth 只允许 `mcp.read`，使用 authorization-code + PKCE S256 和 state
  校验；403 不扩 scope。MCP 固定使用官方 `mcp==2.0.0` Streamable HTTP，
  `tools/list` 六项 schema gate 通过前不得 `tools/call`。
- Wind official CLI 仅支持显式 `wind_enrichment`、严格验证后的 `filings` fallback，以及显式 `review_sentiment(query=...)` 的专用 `wind_screener`；后者只调用 `stock_data.search_stocks`，沪深股票族与 `_all_share_codes` 一致，北交所当前仅允许 `920xxx.BJ`。不得让泛化 `wind_mcp` 接行情、K 线、分钟、新闻、泛选股或 `stock_event`。
- WenCai OpenAPI 401/403/429 使用跨进程 breaker；pywencai 依赖缺失与 provider error 必须区分。
- 实验性零鉴权 `pytdx_screener` 固定 `pytdx==1.72`，只接受唯一沪深 universe 加至少一个 `非ST` / `非停牌` / 单代码 / `最新价` / `涨幅` AND 条件；数值条件必须含 `非停牌`。不支持北交所，也不支持行业、概念、PE、PB、排名、OR 或日期。它必须读取完整目录与完整 quotes，batch 不超过 80，不调用现有 source fallback；当前只允许显式 provider 诊断/开发调用，不作为 public `query()` 自动兜底。
- Key、token、credentials 不进入 argv、日志、doctor、CLI 输出、receipt 或 Git。
- 不发交易 POST、不调用券商、不部署、不 push，除非弈沐另行明确授权。

完整 ownership、setup、doctor 状态、capability 和 automatic fallback 表见 `README.md`。

## DP-01 空、失败、降级与板块口径

`attempts[].error_code`：成功可为 null；合法空表使用 `EMPTY_UPSTREAM`，不表示源不可用。
失败保留具体原因：`INVALID_RESPONSE`（无效 wire/table 或业务 shape）、
`INVALID_EMPTY`（声明空但 shape 无效）、`STATUS_DATA_MISMATCH`（声明空但含业务行）、
`PARSE_FAILED`（同花顺第一页未能解析）、`HTTP_<status>`、`DEPENDENCY_MISSING`、
`PLAN_NOT_ENTITLED`、`AUTH_DENIED`、`RATE_LIMITED` 与具体 `QUALITY_*`。不得从无效页、
缺依赖或缺必需快照批次推断合法空。`board_members` 的业务行容器是 `boards`；
空 `boards` 与缺少该容器不同。盘中全市场必需批次空报 `SNAPSHOT_BATCH_EMPTY`，
整个复合意图为 error，保留最后正常值的决定仍由消费者执行。

每个 degraded 信封必须带 `quality.reason_codes`。降级链保留先前失败码；
StockToday 的 unknown/stale 时间、截断、缺代码、重复代码分别写
`DATA_TIME_UNKNOWN`、`DATA_STALE`、`UPSTREAM_TRUNCATED`、`MISSING_CODES`、
`DUPLICATE_CODES`。字段覆盖短缺沿用 `missing_items` / `coverage_shortfall`；
板块强度的 typed gap 同时进入 `quality.gaps` 和 reason_codes，不能用 normal
包裹全字段 missing。无法细分的已有 partial 原因用 `QUALITY_PARTIAL`，不冒充套餐或鉴权错误。

`sector_index` 的行业代码允许裸 `881xxx` 或 `.TI` 后缀；网页输出仍是裸 code。
同花顺行业摘要不含行业指数点位：第 4 列是 `volume_wan_lots`（万手），第 9 列是
`avg_price`（元），不是点位或平盘家数。兼容字段 `latest` / `flat_count` 留 null。
`stocktoday_data(api_name="ths_daily", params=...)` 的 OHLC 则是供应商行业指数日线点位，
`avg_price` 是另一个字段。两边数据时间和量纲不同，不能拿成交量与日线 close 比价。

`market_board_strength` 必须给 `board_ids` 或 definitions 才有选择集；空选择集是
empty，不表示全市场无板块。`member_coverage` 由持有成员证据的调用方给，缺失或
低于 0.8 时字段保持 null；不自动赋 1.0。相对五日线、逐板块中军尚无生产者，
概念不使用行业资金流替代。源缺口如实保留，相关能力方案见 DP-01 证据目录。

`market_limit_state` 读取按日发布的 `limit_list_d` / `limit_step`，盘中当天表可能
尚无行；合法上游空不证明实时涨跌停为零。盘中状态使用 `market_intraday_state`，
不得把每日空表当成实时值。`latency_ms` 对成功/合法空的涨跌停复合查询记录全部
子请求耗时；既有空池三次读取策略本次不增加重试、不改额度。

`realtime_market` 的腾讯快照增加科创50（000688.SH）、上证50（000016.SH），保持
原上证、深成、创业板字段。成交额仍只累加上证和深成，避免子集指数重复计入；
它是现有沪深口径，不是含北交所的全市场成交额。科创50/上证50按供应商响应提供，
本次不新建 N1 实时指数意图、不扩指数分钟 K 线路由。

## DP-02 时间、空窗口与复合查询诊断（候选）

本节描述 10-16 隔离修复候选，发布与生产回读另行处理。

- `sector_index` 的同花顺摘要没有可验证的行情时间时，`data_as_of` 继续 null、
  freshness 继续 unknown，结果为 degraded 并带 `DATA_TIME_UNKNOWN`；不拿 fetched_at
  冒充行情时间。名称按源目录精确匹配，名称缺失是合法 empty / partial，不自行给
  “出版”“出版传媒”“军工”等宽泛词补别名。
- 默认日 K 的最新 bar 早于日历要求的最近已完成交易日时，freshness 标 stale，给
  `reason=bars_before_latest_completed_trade_date`、expected_trade_date、last_bar_trade_date，
  quality 带 `BAR_TRADE_DATE_LAG`、信封为 degraded。它只说明日期落后，不推断停牌；
  显式历史日期的 freshness=historical 保持。停复牌需要独立 `suspend_d` 或正式公告证据。
- `index_intraday_compare` 的 `data_as_of` 取三指数都存在的最后一个完成时段，累计行
  不参与；时间行或日期不足则仍 unknown。当天首个 5/15/60 分钟时段分别在
  09:35/09:45/10:30 完成；此前是合法 empty，reason_codes 包含
  `NO_COMPLETED_INTRADAY_BARS`，quality.status=empty、returned_count=0，不发无意义的
  分时查询。首个窗口之后的源失败或缺行仍保留 `INCOMPLETE_INDEX_COMPARE`，不降格为合法空。
- `market_limit_state(include_promotion=True)` 的公共参数归一化会生成 previous_date；
  StockToday 适配器必须消费该已校验日期，不能忽略晋级请求。复合查询失败的 attempt
  latency_ms 包含失败前的子请求时间，不只是最后一跳的耗时。GET 网关负责把文本
  boolean 转成 Python bool，公共 query 仍严格拒绝字符串布尔值。
- `market_intraday_state` 的 observation.batches 增加每个 rt_k 批次的 latency_ms，
  observation.limit_metadata 保留辅助日表的状态、稳定码、行数、匹配行数和耗时。
  日表 empty 时 source_gaps 另带 `limit_up_metadata_empty`；错误/日期不匹配时为
  `limit_up_metadata_unavailable`。不使用另一个来源填 industry 或 seal_time，不把源缺失写成业务零。
- `industry_flow(trade_date=...)` 是显式日表查询，不是盘中查询。
  `use_case="realtime_poll"` 的既有路线先读同花顺、再读 StockToday；后备请求显式
  带当前日期、仍严格过滤非当日行。日表日期本身不证明盘中时点；没有源时间时
  data_as_of=null + degraded/DATA_TIME_UNKNOWN。同花顺页面解析失败必须保留 PARSE_FAILED，
  不能先变成 note/空表再冒充合法 empty。
- 公共 `_meta.attempts` 保留四字段（provider/status/error_code/latency_ms）。消费者
  的脱敏/裁剪层也应保留源给出的合法非负整数；若旧回执缺失延迟，不补造测量值。
  `attempt_classification_trace` 是独立的三字段分类轨迹，不与 attempts 混用。

## 铸剑 P2-A 数据标准化（000985.CSI 与最后封板时间）

本节是 2026-10-18 隔离树的最小施工记录，只覆盖两项字段/归一化暴露，
不改路由、不扩 provider、不新增依赖。

回归证据在**仓库内**：`tests/fixtures/zhujian_p2/` 的两份最小脱敏夹具
（`index_daily_000985_csi.json`、`limit_list_d_up_20261016.json`）与
`tests/test_zhujian_p2_standardization.py`。夹具由 P1 封存响应逐行裁剪而来，
并在 `_source_sealed_file` / `_source_sealed_sha256` / `_source_collected_at` 中
记录来源与原封存文件 hash，可离线复核。测试在 socket 层断网运行，
**不需要任何 Scratch 目录即可在干净 clone 中复现**。这些是**已封存响应的离线重放**，
不是新的在线证据；施工期的外部证据 JSON 只作本地审计记录，不随仓库发布。

### `index_kline` 的 CSI 后缀

`normalize_index_code`（`api.py`）与 `StockTodayProvider._index_code`（`providers/stocktoday.py`）
接受 `\d{6}\.(?:SH|SZ|CSI)`。要点：

- **SH/SZ 与裸代码行为完全不变**：裸 6 位仍按既有规则推断（`000001`→`.SH`，
  其余→`.SZ`）。裸 `000985` 仍归一化为 `000985.SZ`，**不会**静默变成 CSI 基准。
- **CSI 是显式第三种后缀，不是 SH/SZ 的别名**。接受它只放宽客户端校验，
  不等于给后备源发明的映射。
- **只有 canonical StockToday 日线路径被证实**（`index_daily` 以 `000985.CSI`
  取到真实日线）。`eastmoney_index._INDEX_MARKETS` 仍只登记
  `000001.SH/399001.SZ/399006.SZ`，对 `000985.CSI` 诚实返回 `INVALID_PARAMS`；
  本次**没有**为腾讯/东方财富发明 CSI 映射。
- **分钟线仍是明确 gap**：CSI + 分钟周期不经过 StockToday（`idx_mins` 不在套餐内），
  后备源又无 CSI 映射，因此诚实失败。周/月线未在本次证据范围内。
- **基准缺失不换指数**：上游空就是合法空（`EMPTY_UPSTREAM`），
  不得改用上证/沪深300 冒充中证全指。

### 涨停行封板时点

`limit_list_d` 的 `first_time`/`last_time` 逐条透传为标准命名的
`first_limit_time`/`last_limit_time`（沿现有 `code/name/price/pct/limit_days/industry`
行形状追加，不改动既有字段）：

- **两个字段语义独立**，不折叠、不互相回填。真实样本 20261016 涨停池 61 行里
  **30 行 first≠last**，正是「早盘首封、盘中炸板、下午再封」的反例。
- **原来源时点按上游字符串原样透传**（`HHMMSS`，如 `"130012"`），不重写格式、
  不补零、不按时区换算。
- **两态，不是三态**：`null` → `None`，空串 → `""`。**整列缺失与显式 `null`
  同归 `None`**，本契约不区分这两者（都需要先定 wire 层是否区分缺失列再改）。
  任一情况下都**不回落**到 `first_limit_time`，也不写成 0。
- 阻断条款仍然有效：**不得用首封冒充最后封板**；历史封存数据不回填，保持 unknown。
- `fd_amount` 语义仍未证实，本包**不推断**其含义，也不据此产出封成比。

## 铸剑 P2-B 数据合同（F03 名册时点 / F09 分钟比较元数据）

只增不改：既有 `members`/`member_count`/`data.data_as_of` 与
`index_intraday_compare` 旧三键全部原样保留。回归见
`tests/test_p2b_contract_f03.py`、`tests/test_p2b_contract_f09.py`，
夹具 `tests/fixtures/p2b/`（含来源与原封存 hash）。

### F03 board_members：成员真实时点保持 unknown

`ths_member` 请求不带时间窗（`start_date`/`end_date` 皆空），上游按**当前名册**
返回；`trade_date` 只是本地缓存分组键。因此：

- **`membership_as_of` 恒为 `null`、`membership_time_status` 恒为 `"unknown"`**。
- **不得**把请求日、缓存标签或 `fetched_at` 赋给 `_meta.data_as_of`；
  消费者不得用缓存标签认证成员新鲜或历史成员。真实历史成员**无法验收**。
- `data.data_as_of` 为**兼容保留**，语义仅是缓存分组标签，不是成员生效日。
- 新增 `requested_trade_date`（请求日语义）与 `cache_trade_date`（缓存键语义），
  两者分开命名，避免与观测时点混淆。
- `_quality_failure_code` 对 `board_members` 的既有豁免不变：成员按交易日缓存，
  用 `fetched_at` 判新旧会误判 `QUALITY_STALE`。

**去重口径（每板块新增）**：`unique_member_count`、`duplicate_member_codes`、
`invalid_member_codes`。唯一计数只按**有效证券代码**计——**ASCII** 六位数字 +
已识别后缀 `SH`/`SZ`/`BJ`（`123456.ZZ`、`600519.US`、全角数字都不算）。
无效成员只单列，**不从既有 `members` 里删**，也不做 strip 修剪。
`members`/`member_count` 原样保留，既有消费者的分母**不变**。
上游静默空仍进 `missing`，不回声 0 名成员；未知维度不补 0。

去重元数据在**返回投影统一派生**，因此 fresh fetch 与**既有缓存命中**拿到同一套
字段（旧缓存的 191/189 同样补齐）；既有缓存文件不改写、不靠清缓存绕过，
缓存写入时刻也**不会**被提升为 `membership_as_of` 或 `_meta.data_as_of`。

>下游 `Market_Watch` 的 `compute_member_coverage` 目前用未去重行数作分母。
>按证券去重已获批准（D21），但**接线不在本包**，也不改 MW/LD。
>实测口径（881121 / 20261016）：MW 的 `a_share` 与 `len(codes)` 取**同一份
>未去重列表**，重复码在分子分母各计一次，故为 **191/191 = 1.0**；去重后
> **189/189 = 1.0**，净差 0。是否真的影响覆盖率取决于重复码是否都带 A 股后缀，
> **不能一概断言"虚高"**。

### F09 index_intraday_compare：series_metadata 与 bar 计数

旧键名里的「15min」是历史遗留（键名不随 period 变），5m/60m 的响应仍挂在
「…15min」下。**本包不改旧键、不复制行**，只新增 `series_metadata` 映射，
按每个旧键给出：`index_code`、`period`、`correct_label`（如 `上证5min`）、
`row_count`、`bar_count`（**排除 `_cum`**）、`cum_row_count`、
`expected_bars_per_full_day`（5m=48 / 15m=16 / 60m=4）、`bar_completeness`。

- `bar_count` 是**返回行数**，不是完整性认证：`bar_completeness` 恒为
  `"unverified"`。凑满 48 行**不能**据此宣称当天已完成——本包没有盘中完成
  标记的证据。
- `_cum` 累计行（`t="累计"`）不是 bar，不计入 `bar_count`；
  既有 `_data_as_of` 与 `_quality_failure_code` 对 `_cum` 的过滤不变。
- `data.period` 保留真实值。三个指数的 metadata **各按自己的行数计算**，
  不借另一指数的计数。
- tencent / eastmoney_index / sina_index 三个 provider 共用
  `build_index_intraday_compare`，一次改动覆盖全部当前 provider；
  该路径不适用的 provider 不伪造覆盖。

## 铸剑 P2-C1：连板数与炸板次数语义分离

涨停明细的 `limit_days`（几板）与 `break_times`（炸板几次）是两个不同事实。
回归见 `tests/test_p2c_limit_count_semantics.py`，夹具
`tests/fixtures/p2c/limit_list_d_up_20261016.json`。

### 字段口径

- `limit_days` **只取来源明确的 `limit_times`**。缺失/null、`0`、`bool`、
  小数、NaN/Inf、负数一律为 `None`（未知）：**不回退 `open_times`、不补 1、不取整**。
  涨停池里连板数至少为 1，故 `0` 不是合法板数。
- `break_times` **独立取 `open_times`**；合法 `0` 保持 `0`，非法同样为 `None`。
- 既有 `code/name/price/pct/limit_days/industry/first_limit_time/last_limit_time`
  语义不变；未取到第二份数据源，`break_times` 与 `market_limit_state` 池里的
  同名字段同源同义。

### 最高板与 known_max_board

- `max_board` = **完整已知**板数的最大值。非空池只要有一行 `limit_days` 未知，
  `max_board` 即 `None`——**不把已知子集的最大值当全体最大**。
- `known_max_board` 另行给出可观察的子集最大值，供观察用，**不冒充完整最高板**。
- **真正空的涨停池仍返回 `0`**；加载失败（error）不是空池，不产生 `max_board`。
- 下游 `intent_normalizers` 只做 `max_board` 透传，未再兜底。

### 与封存库写入路径的关系（round3 更正）

**更正前一版结论**：此前写「`market_limit_state` 不经过 `_limit_row`，
所以本包的 `None` 语义不会流入该路径」——**该结论错误**。
`normalize_stocktoday_limits` 自己就会造板数：原实现在 `limit_times` 缺失时
回退 `step_boards.get(code, (1, ""))[0]`，即**STEP 参考板数，或字面量 1**，
`_call_limit_state` 随后聚合这些数。所以「不经过本包路径」并不等于「完整」。

round3 修正后的口径，终修补充质量贯穿与字段独立：

- `normalize_stocktoday_limits` 的 `limit_days` **只取 `limit_times`**
  （非 bool 整数且 `>= 1`）；缺失为 `None`，
  **不回退 STEP、也不补 1**。行被结构错位拒绝时整池失败；`close` 缺值
  **不会**把合法板数写成未知。
- STEP 仍作**参考来源**参与交叉核验，`_board_source` 标记保留供追溯
  （如 `limit_step_partial_reference_unverified`、`limit_list_d_ladder_disagreement`），
  但 `reference_unverified` 的 STEP **不是确定板数**，不进入 `limit_days`。
- `limit_step` 的 `nums` 只接受**无需截断**的整数：`2.5`/`"2.5"`/bool/NaN/Inf
  一律**抛错**（`int()` 会把 2.5 洗成 2、把 True 洗成 1）。
  纯数字字符串（如 `"3"`）按整数接受——既有合同与调用链都传这种形态，
  封存证据不足以判定它非法。
- 板数是否可信**只看 `limit_times` 本身**；`close` 异常只影响**价格字段**，
  不会把一个合法板数降级成未知，也不会清空同行合法 `pct` / `break_times`
  或同池健康行。具名完整结构里 `close=None` 不是结构错位。
- 真正结构错位依据类型证据拒绝：例如 `close` 为字符串/bool/容器，且错位行
  超过池大小四分之一时，normalize 抛错（公共链 `LIMIT_POOL_INVALID`）。
- 非空池有任一未知 → `max_board` 为 `None`（unknown），
  `known_max_board` 只含有证据的板数；名称/价格/涨跌停价/`zt_count` 等
  可靠字段**完整保留**，只隔离板数这一个字段。
- 未知板数必须贯穿公共 `query("market_limit_state")` 的质量信息：
  `quality.status=partial`，`reason_codes` 含 `LIMIT_DAYS_UNKNOWN`，
  `data.source_gaps` 标明受影响字段（`limit_days`/`max_board`）和代码。
  不能只因业务行出现 `limit_days` 键就当作已披露缺口。
- `break_times` 在 `_limit_row`、`market_limit_state` 与 `ingest_limits`
  同义：严格非 bool 整数且 `>= 0`，合法 `0` 保留，非法为 `None`。store
  对直接调用做同口径防御，避免 SQLite INTEGER 亲和性把 `2.5` 收成 real。
- `MarketFactStore.ingest_limits` 对不完整板数**严格拒写**
  （`limit_events.board_count` NOT NULL，报 `duplicate or malformed limit stock row`），
  **不改存储 schema**，**既有有效 run 原子不变**；生产 refresh 拒写该部分后
  仍可继续独立的 daily_ohlc。
- **D/Z 池的 0 是既有口径**，本包不借此改动；无真实样本的部分标为未知。

## 下游与回滚

迁移消费者时保留业务 shape、provider provenance、attempts、质量 reason codes、合法 empty 语义和 observation-only 边界。旧路径若暂时保留，只能集中在一个默认 `legacy` 的 rollback switch 后；只有同一时点 side-by-side 对业务 shape、provider/attempts、空/error overwrite guard 全部通过，才可考虑切换默认值。

不得对 live-dashboard 真实 8088 发 POST，不得覆盖生产 data/cache/runtime，不得把数据查询结果当成交易授权。
