# DP-02 跨仓接续：两份补丁未应用目标仓库

本单仅修改 DP-02 的 YM-data-pipeline 隔离 worktree。以下补丁在目标源码的内存
副本上离线验证，不修改 live-dashboard 主检出或 TB-06 分支，不表示已部署。
目标源路径与 sha256 见 `cross-repo-source-bindings.json`；源码变动后应重新核对。

## 轻舟 GET 网关的 boolean 反序列化

原始 502 record 明确：`market_limit_state include_promotion must be boolean`。
`scripts/agent_api.py::handle_pipeline` 将 parse_qs 的单值取出后只处理 codes/names，
`"True"` 原样交给严格的 public query，因而报 502。它与不带该参数时的真实
StockToday TIMEOUT 是独立问题。直接 public `query(..., include_promotion=True)` 不会
出现这项类型错误，不能放宽管道的 bool gate 来掩盖调用层缺陷。

`handoff-gateway.patch` 在网关白名单检查后只解码 include_promotion 的 true/false，
重复值和非布尔文本返回 400，不扩大 intent、参数、token 或写入权限。
`cross-repo-proof.json` 的离线回放：原源码 502，补丁内存副本 200，管道收到 Python
bool 并规范化 previous_date=20261015；重复参数 400。没有访问真实 8088。

## Bots gap 脱敏丢失 latency_ms

`tools/read_pipeline_role.py::_local_gap_diagnostics` 仅拷贝 provider/status/error_code；
`_safe_local_gap` 再用这个三字段列表覆盖原 `_meta.attempts`。因此 mac_local 的 error /
empty（如 daily_basic、早盘指数）会缺 latency_ms，而成功信封与网关信封通常保留。
公共 pipeline 的 builder/validator 本就要求四字段；本次 17 份新查询均保留延迟字段。

`handoff-bots.patch` 保留原始非负整数延迟；缺失、布尔值、非法数值保持 null，
不把“没有测量”伪装成 0。离线验证 TIMEOUT 的 15423ms 在原裁剪后丢失，补丁副本
完整保留；缺失样本没有凭空变成数值。由 Bots 所有者在自己的施工分支接续审计。

## filings：导入环境错误，供应商并未被调用

用户补充的 `/tmp/intel_600825_filings.json` 已冻结到 historical/ 并绑定 sha256。
实际输入为 code=600825、days=7，未带 max_pages，10:05:54 调用 46.299ms 后得到
`pipeline_unavailable: Public pipeline import unavailable; no gateway or CLI fallback.`。
该消息仅出现在 `_load_public_query()` 失败的 catch 分支；calling 尚未设为 true，
因此不能归因 cninfo/Wind 超时、套餐或无公告。前一次 root/storage 多余键被拒也
发生在网络之前，另记为调用信封错误，不混为此次 import failure。

异常类型、解释器、完整 traceback 没有保留，不能倒推出究竟缺哪个依赖或路径。
本单按原参数在管道 launcher 的外置托管 Python 环境读查一次：cninfo 返回 10 条
公告、450ms，完整信封见 `online-filings.json`。这是当前独立环境读回，不证明
盘中情报进程的导入环境已修复。

下一步由 Bots 启动侧核实“实际解释器 + public pipeline import”与托管环境一致，
并在脱敏错误里保留异常类型；不写异常原文、凭据，不增加 gateway/CLI 运行时旁路。
本单未修改共享 Python、安装依赖、.env、服务启动或认证配置。
