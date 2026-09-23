# YM-data-pipeline — Claude Code 入口

共同操作规范和任务路由只维护在 [AGENTS.md](AGENTS.md)。provider、环境与错误语义按 README / docs/agent-contract.md，认证按 docs/INSTALL.md；本文件只保留快速定位。

- Python：`from ym_stock_data import query`；CLI：`./ym-data doctor --json`。doctor 是离线配置检查，不证明在线。
- 真实来源看 `result["_meta"]["attempts"]`、`result["_meta"]["provider_used"]` 及 auth/error_code/quality/fetched_at，不按预设顺序猜测。
- README 的来源索引包括 WenCai OpenAPI、portable pywencai、TDX owned OAuth、official Wind CLI、zero-auth PyTDX；它们不是每个 intent 的固定调用顺序。
- 认证命令：`./ym-data auth login-tdx` 与 `./ym-data auth status-tdx`，只在对应授权任务执行；WorkBuddy 凭据迁入及只读 scope 边界见安装契约。
- V1/V2 仅为 compatibility wrapper，不作为新调用入口或第二条降级链。
- 数据查询不产生交易授权；不调用券商、不写生产 data/cache/runtime，已有任务授权不重复询问。
