# YM-data-pipeline — 数据公共入口

## 查询与环境

- Agent / 新代码唯一 Python API：`from ym_stock_data import query`，不得 import 私有 sources 或 V2；CLI 用根目录 `./ym-data`。
- 先运行 `./ym-data doctor --json` 读取脱敏配置状态；doctor 不证明在线。系统 Python 缺依赖不证明 provider 不可用。
- launcher 按 checkout/worktree 使用 uv cache 外置环境；裸 uv run 仅适用于不受 File Provider dotpath 问题影响的开发验证。配置与 Python 示例见 README。
- 结果保留 data/_meta、status、provider_used、attempts、quality、fetched_at 和稳定错误码；合法 empty、失败和缺依赖不能混为一谈。

## 按任务读取

- 普通查询：README 中意图/参数和环境说明；无需读历史实施计划或全量 provider 细节。
- provider/路由/契约开发：`docs/agent-contract.md`、相关实现和测试。
- 安装/认证：`docs/INSTALL.md`；WorkBuddy 凭据只允许明确授权的一次性受控迁入，运行时不得扫描或持续同步。
- 手工能力验收：`docs/ACCEPTANCE_RUNBOOK.md`；五日验收已退出定时和默认完成门槛，只在用户明确要求时运行。`./ym-data smoke --live` 是显式在线探针，不授权登录或后续连续验收。

## Provider 边界

- TDX 仅限已注册的六项只读能力与 mcp.read；401/403、scope、schema 与 auth/provenance 处理见开发契约。
- Wind 只用显式 `wind_enrichment`、严格 `filings` fallback，以及 review_sentiment 的专用 `wind_screener` / `stock_data.search_stocks`；泛化 wind_mcp 不接管行情或泛选股。
- `pytdx_screener` 仅供实验性显式诊断，不进入 canonical 自动降级链或正式 live gate。公开问财链的 provider 顺序与 empty/失败语义见开发契约，不自行增加旁路。
- Key、token、credentials 不进入 argv、日志、doctor、receipt 或 Git。

## 下游与回滚

- 数据查询不授予交易权限；不得对真实 8088 发 POST，不覆盖生产 data/cache/runtime，不调用券商。
- 下游迁移必须保留业务 shape、provenance、attempts、empty/error overwrite guard 与 observation-only 边界；既有 legacy rollback 的默认切换按开发契约验证。
- 先看 git status，保护已有改动，显式暂存；不 reset、clean 或 stash 他人工作。
- 行为变更按失败测试、最小实现、聚焦验证推进；公共契约、路由或凭据改动追加全量 unittest、compileall 与敏感路径扫描。纯文档改动运行相关文档契约测试与 git diff --check。
- push、部署和认证按用户各自授权执行，已有授权不重复询问。
