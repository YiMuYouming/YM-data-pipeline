# YM-data-pipeline V3 生产发布与接续（2026-09-23）

## 发布边界与实际位置

- 本机 `/Users/yimu/Documents/YM_Capital` 解析到 `/Users/yimu/Projects/YM_Capital`，同一套文件只处理一次。
- 本机管道：`YM-data-pipeline`，开发分支 `codex/unified-a-share-data-channel-canonical`，代码提交 `d4d99a6`；全局 Skill 的 Codex、agents、Claude、WorkBuddy 四个入口均链接到此仓库 `skills/ym-a-stock-pipeline`。
- Hermes 管道：`/home/agentuser/YM-data-pipeline`，生产分支 `codex/daily-flow-fact-contract-20260912`，运行代码提交 `e103db2`，`.venv` 中 editable 安装版本 `3.0.0`。生产凭据在 root-only `/etc/yimu/stocktoday.env`，由 systemd drop-in 注入；值未进入 Git、日志、命令参数或本文件。
- 本机看板：`live-dashboard` 开发分支 `wip/2026-09-15-live-system-snapshot`，代码提交 `f7efab6`。Hermes 看板：`/home/agentuser/YiMu-Capital` 生产分支 `codex/hermes-bridge-20260912`，代码提交 `dffd4c9`；`yimu-live-dashboard.service` 以该目录 `.venv/bin/python scripts/bridge.py 8088` 运行，依赖上述 Hermes 管道。8088 的本机 SSH 隧道和 18088 代理不是发布目标。
- Market Watch：本机 `Market_Watch`，分支 `feat/local-spec-migration`，提交 `ac9ed40`；YiMu IR：本机 `YiMu_IR`，`main`，提交 `4ae8bde`；shadow：本机 `live-trading`，`main`，提交 `84bdd43`。三者是按需运行的本机消费者，Hermes 上无对应工作目录或服务。本机变更已推送对应分支；未声称存在远端运行实例。

## 发布前保护与回滚

- Hermes 管道发布前提交 `b01db08f65dbbb866da51aa94b402c93c1ccdbe6`；看板发布前提交 `ab5dc6f76bde6608e8cc32fc97d0e10896f084d9`。两仓库的 `rollback/pre-ym-data-v3-20260923` tag 已推送。
- Hermes `/home/agentuser/release-backups/20260923-ym-data-v3/` 保存看板旧 venv、pip freeze 与旧 HEAD。生产 DB、缓存与账户事实未从本机覆盖，也未进入 Git。
- 回滚前先核对两仓库工作树干净及当前 HEAD，再在 Hermes 将两个代码目录切到各自 rollback tag（或从 tag 创建恢复分支），必要时恢复 `dashboard-venv-pre`，然后重启 `yimu-live-dashboard.service`；回读 `/api/health` 与 `/api/live/quotes`。保持远端 `data/`、账户 DB、缓存原状。若仅某项消费端失败，先回退对应消费端，不动其他已验证环境。

## 实际业务回读（Hermes，2026-09-23 13:00–13:24 Asia/Shanghai）

- 真实服务 GET `/api/live/quotes`：35 只跟踪股票均有 `price` 与看板 `最新价`，原始 `quote_time` 为 `13:23:51+08:00`、缓存更新时间为 `13:23:56+08:00`，两者未混同；`_meta.provider_used=tencent`、质量 `partial`，PyTDX 在该主机直接返回不可用或过期后按序降级。三大指数齐全，三条 15 分钟比较各有 10 行；热榜 43 行、行业 40 行、北向 262 行。`/api/health` 的行情分项为 `live`、覆盖 35/35；总状态当时为 `degraded`，唯一原因是既有 `iwencai: delayed`。
- 用 Hermes 服务相同 venv 与凭据环境调用公共 `query()`：贵州茅台行情 `stocktoday/success`，原始报价时间 `13:19:39+08:00`；实时三大指数 `stocktoday/success`；同花顺热榜无日期请求自动取最近完成交易日 `20260922`、20 行；涨停板 `stocktoday` 空数据时降到 `eastmoney_limit_pool`、63 行；未复权日 K `stocktoday/success`、2 根，前复权日 K 成功时 `stocktoday` 或 `eastmoney_stock`、2 根，均保持 `datetime`、`volume_unit=share`、`amount_unit=CNY` 和请求的复权方式；行业资金流 10 行；市场资金流成功时 1 行；指数 15 分钟比较东方财富不完整后降到新浪，三指数各 17 行、质量 `partial`。
- 本机真实消费调用：Market Watch 行业表 90 行、涨停原因 63 条；YiMu IR 前复权 K 线 2 根；shadow 股票报价 1 条，保留 `13:09:56` 原始行情时间和 `partial` 质量。全局 Skill 四个入口均解析到上述 canonical Skill。
- 隔离进程故障注入：`YIMU_DISABLE_PYTDX=1` 时实时股票请求降到腾讯，保留报价时间；仅在进程内将 StockToday 模拟为 timeout 时，前复权 K 线降到东方财富，仍为 `qfq/share/CNY`，2 根。未修改生产配置。
- 发布中发现看板只识别 `最新价` 而新标准行只含 `price`，导致首次健康检查覆盖 0；已增加兼容投影并回归。又发现逐股 PyTDX 历史条请求使 35 股轮询耗时约 22 秒、共用采集锁堵塞；已将高频轮询改为单批报价、移除跨能力全局锁，重启后行情持续更新。股票报价与指数实时任务不再报实例占满，较慢的广度及指数分钟任务仍偶有跳过，不能将全部调度视为无缺口。
- 发现指数分钟三条数组落盘时漏存 `kline_15m_date`、且丢失统一入口 `_meta`；冷启动会返回空数组。看板 `dffd4c9` 修复了日期与元数据的保存、恢复及 `/api/live/quotes` 回传。首次采集后 13:32 回读三指数各 11 行、日期 `2026-09-23`、`provider_used=eastmoney_index`、质量 `normal`，磁盘缓存也保留同一日期与来源；13:32:17 再次重启后立即回读仍为各 11 行与相同元数据。

## 未关闭限制

- 2026 交易日历以外无日期请求会明确报错，2027 日历待官方日历可用后补充。
- Hermes 的 PyTDX 个股直连在本轮出现 `QUALITY_SNAPSHOT_STALE` 或 `PYTDX_DIRECT_UNAVAILABLE`；看板实用腾讯备用，不能把腾讯结果算作 PyTDX 成功。
- StockToday 前复权请求偶发 `INVALID_RESPONSE`，东方财富同口径备源可成功，但一次密集回读中前复权曾全链路 `error`；`fund_flow` 也曾瞬时 `error`，当前路由仅有 StockToday，无可靠备源。外部可用性仍有短时缺口，不宣称“零数据缺口”或整体目标全部完成。
- 看板总健康的 `iwencai: delayed` 属本次发布范围外的旧采集时效问题；行情分项本轮为 `live`。远端不存在 Market Watch、YiMu IR、shadow 运行服务，只有本机代码与 Git 分支已更新。
- 全局 Skill 只同步到本机四个实际入口；Hermes 未安装 Agent Skill，因该主机的服务通过 Python 公共接口调用，且本机 Skill 中的绝对路径不适用于 Hermes。

## Git 推送与接续

- 管道开发分支：`b472259`（V3）与 `d4d99a6`（高频修复）；生产分支：`af387f2`（集成）与 `e103db2`（高频修复）。
- 看板开发分支：`d5000bd`（接入）、`1849a64`（字段投影）、`10ef37e`（采集锁）、`f7efab6`（指数分钟日期与元数据）；生产分支：`e28d943`、`8c26b1e`、`84dffc2`、`dffd4c9`。均已推送。
- Market Watch `ac9ed40`、YiMu IR `4ae8bde`、live-trading `84bdd43` 已推送。各仓库其他脏文件保持原状，未纳入上述提交。
- 上述为 13:32 首轮发布时点的接续记录；同日下午的稳定性续发布见下文。不要从本地数据库或缓存覆盖 Hermes，也不要将本次数据接入推断为下单授权。

## 稳定性续发布（2026-09-23 午后）

### 变更与回滚

- 指数分钟比较的公共 `query()` 总预算为 14 秒，依固定路由给东方财富 3 秒、新浪 7 秒、StockToday 4 秒；东方财富比较专用请求不再叠加源级与 HTTP 层重试，三指数任一缺失即停止该源并尝试下一个。连续两次失败后该比较源跳过 60 秒，到期自动探测，成功即清除熔断。熔断只作用于指数分钟比较，不封锁同一 provider 的其它能力。
- PyTDX 将 Hermes 已验证可返回业务报价的节点排在前面，单轮建连限制为 7 秒，空响应／读异常使旧 socket 失效，下次采集重新连接。Hermes 服务的 `YIMU_DISABLE_PYTDX` 从 `1` 改为 `0`；配置原件保存在 root-only `/etc/yimu/release-backups/20260923-stability/10-cloud-fallback.conf`。生产服务在 14:07:26 重启，管道运行提交 `5a0e9c1`，看板代码提交 `248ff01`（仅更新运维文档；运行代码仍为 `dffd4c9`）。本机管道开发分支提交 `d5d5e95`、`8b543b6`；看板开发分支运维说明提交 `85cf8f9`。
- StockToday `pro_bar` 前复权及 `moneyflow_mkt_dc` 大盘资金流仅在 `INVALID_RESPONSE` 时重试一次；鉴权、限流等错误不重试。前复权已有同口径东方财富备用；市场资金流没有已验证同口径备用，失败仍明确为单源缺口，不复用其它资金流指标充当成功。
- 仅 PyTDX 直连异常时，可恢复上述 `10-cloud-fallback.conf` 并 `daemon-reload`、重启服务，保留已验证的指数分钟预算修复。需回退全部管道代码时，生产前提交 `40c88d4` 和 Git tag `rollback/pre-ym-data-stability-20260923` 可回溯；先核对远端状态，保留远端 DB、缓存与事实，不从本机覆盖。看板原运行提交 `dffd4c9` 可独立回退文档。

### 生产读回与来源界限

- Hermes PyTDX 的前 3 个原顺序节点 TCP 可连但业务握手被重置；后续节点能返回真实报价。隔离进程读取 35/35 跟踪股，首次 27 只原始时间在 60 秒内；随后 4 个可用节点逐一复测均为 28/35，最旧报价约 9 分钟，换节点未消除旧时间。因此生产服务 35 股批量采集显示 `pytdx / QUALITY_SNAPSHOT_STALE → tencent / success`，没有将腾讯计成 PyTDX；三大指数实测为 `pytdx / success`。现有本机 LaunchAgent／crontab 和 Hermes timer 均未发现持续行情中转；项目记载的本机到云端同步是收盘出包与账户事实，SSH 隧道不是行情源。
- Hermes 生产凭据下，前复权日 K 返回 `stocktoday/success`、2 根、`qfq/share/CNY`；市场资金流返回 `stocktoday/success`、1 行。两者偶发 `INVALID_RESPONSE` 已加有界重试；本次回读没有触发该重试，不能据此宣称上游不再失败。
- `iwencai: delayed` 有两层原因：采集每 10 分钟一次，健康判定 3 分钟后就从 live 进入 delayed；隔离业务查询还返回 `iwencai_openapi HTTP_401`、`pywencai PYWENCAI_RUNTIME_MISSING`、`tdx_screener AUTH_EXPIRED`、`wind_screener CLI_NOT_FOUND`。当前缓存的 `连板股数`、`最高板`、`晋级率` 缺失，涨停详情 `stocks=0`，旧的部分收益字段被保留；连板情绪证据可能因此被 `SENTIMENT_STALE` 阻断，W26 详情消费会转而依赖热榜等较弱证据。同期 `ths_hot` 热榜仍有 47 行，个股报价覆盖不受此问题影响。Tushare 的[同花顺行业资金流](https://tushare.pro/document/2?doc_id=343)是盘后更新，[涨跌停榜](https://tushare.pro/document/2?doc_id=355)约 16:00 更新；不能直接替换盘中问财动态查询而保持同一时效和语义。问财认证／盘中语义映射列为独立事项，本轮不改采集器。
- 2027 无日期查询的交易日历：最迟 **2026-12-15** 检查并导入已公布的 2027 交易所日历；未覆盖时维持明确报错，不猜工作日。

### 连续交易时段观察

- 14:08:02–14:19:46（正常交易，服务按原调度轮询）连续读回，无 HTTP 读错；35 只跟踪股每次均有报价，实际 `provider_used=tencent`。34 次显示 PyTDX 原始时间过旧的 `QUALITY_SNAPSHOT_STALE`，另 2 次为 `PYTDX_DIRECT_UNAVAILABLE`，随后均由腾讯成功返回；最终提供给消费者的原始报价年龄最大 25 秒，接收时间与报价时间分别保留。多节点直连仍有约 7/35 只超过 60 秒，不把整批直接晋级为 PyTDX 成功。
- 实时三大指数 35 次为 `pytdx/success`，14:18:46 一次直连失败转 `tencent/degraded`，14:19:06 自动恢复到 `pytdx/success`。三指数分钟比较每次均有三组数据，来自 `sina_index`；期间成功采集时间更新 11 次，缓存原始 `fetched_at` 的最大年龄 114 秒，三组 K 线从各 13 根推进到各 14 根。东方财富在到期后实际重新探测，但仍有不完整或超时，没有观察到该源恢复；短期熔断被真实触发，不能把新浪结果归作东方财富。
- 隔离进程使用生产同一凭据环境做真实业务查询：指数分钟比较一次 3.41 秒返回新浪三组各 13 根，另一次三源相继超时约 14.01 秒明确 `error`，不再拖到先前约 40 秒。新浪成功的采集尝试耗时约 2.8–6.5 秒，新增 7 秒备用预算覆盖了 14:17:32 的 6.484 秒成功请求。服务在失败时沿用最近成功的分钟缓存，但 `fetched_at` 保持原采集时间，没有写成当前时间。
- 总健康在部分采样为 `degraded`；其中绝大多数对应 `iwencai: delayed` 的 3 分钟阈值与 10 分钟采集周期冲突，另有一次非问财的短时 degraded，未在本轮采样中留到分项原因。同期报价覆盖与三指数分钟数组完整，不能把总健康写成全程 healthy。
- 原始 JSONL 在 Hermes `/home/agentuser/release-backups/20260923-ym-data-stability/observation-final.jsonl`，本机副本 `/Users/yimu/Projects/YM_Capital/shared/audits/2026-09-23-v3-stability/observation-final.jsonl`；首次配置与预算窗口的对照记录在同目录 `observation-pre-final.jsonl`。记录不入 Git，未触及生产 DB 或缓存。完整管道回归、聚焦故障/恢复回归、编译和差异敏感路径扫描通过。

### Git 与未关闭项

- 管道开发分支 `codex/unified-a-share-data-channel-canonical`：`d5d5e95`、`8b543b6`；Hermes 生产分支 `codex/daily-flow-fact-contract-20260912`：`803781c`、`5a0e9c1`。看板运维说明开发分支 `85cf8f9`、Hermes 生产分支 `248ff01`。以上代码与文档已推送；Hermes 两仓库工作树干净。当前服务运行管道 `5a0e9c1`（包 3.0.0，editable 指向 `/home/agentuser/YM-data-pipeline`）、看板 `248ff01`、配置 `YIMU_DISABLE_PYTDX=0`。
- 仍未关闭：PyTDX 35 股原始时间不全达标，批量报价生产实际使用腾讯；东方财富指数分钟仍多次不完整／超时，新浪是实际可用源，偶发全链路查询 14 秒后仍可能 `error`；StockToday `fund_flow` 无等价备源，前复权瞬时重试不能保证上游永不失败；问财 OpenAPI 鉴权与盘中语义数据缺口独立处理。2027 日历按上文维护日处理。
