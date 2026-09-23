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
- 下一轮只需针对 StockToday qfq 瞬时错误、`fund_flow` 缺备源和 2027 交易日历处理；先用独立实例复现，再决定是否改路由。不要从本地数据库或缓存覆盖 Hermes，也不要将本次数据接入推断为下单授权。
