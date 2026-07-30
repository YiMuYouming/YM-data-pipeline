# ym-stock-data 安装与 provider 配置

## 基础安装与诊断

```bash
./ym-data doctor --json
```

`./ym-data` 是正式 repo CLI 入口。它解析 launcher 所在目录的绝对路径；调用方没有显式设置 `UV_PROJECT_ENVIRONMENT` 时，使用 `uv cache dir` 和项目路径 SHA-256 选择 checkout/worktree 独立的外置环境，再运行项目 console entry。它不会把环境放在项目根目录，也不会记录参数或凭据。显式 `UV_PROJECT_ENVIRONMENT` 始终由调用方所有并原样保留。

uv 选择顺序为：显式绝对路径 `YM_DATA_UV_BIN`、PATH 中逐项候选、macOS 常见位置 `/opt/homebrew/bin/uv` 与 `/usr/local/bin/uv`。每个候选必须先通过脱敏的 `--version` 探针。显式 override 无效时 fail-fast，不会偷偷降级到其它 uv。

在 macOS Documents File Provider 管理的 checkout 中，项目内 `.venv`、editable `.pth` 可能被重新标记为 hidden。外置环境可避免 Python 因 hidden `.pth` 跳过 editable 安装。裸 `uv run ym-data ...` 仅作为非 File Provider 环境的底层调用，不是正式使用入口。

`doctor` 只读检查每个 provider，不发在线查询，不输出 token、异常正文或查询结果行。供应商在线状态必须通过显式只读探针验证，不能由单元测试或 `configured_unverified` 推断。

## 零鉴权 profile

基础安装提供 PyTDX、腾讯、东方财富、同花顺、巨潮与财联社等零鉴权通道。先运行：

```bash
./ym-data doctor --json
./ym-data query realtime_market
```

零鉴权不代表在线可用；以 query 返回的 `provider_used`、`attempts`、`quality` 与 freshness 为准。

结构化选股第五源使用固定 `pytdx==1.72`，provider id 为 `pytdx_screener`。它只在
query 含唯一 `沪深A股` / 沪市 / 深市 universe、至少一个审核过的 AND filter，且
被编译器完整消费时追加到 Wind 之后。支持 `非ST`、`非停牌`、单一股票代码、
`最新价`、`涨幅` 及固定比较/区间语法；数值条件必须带 `非停牌`。不支持北交所，
也不支持行业、概念、PE、PB、排名、OR 或日期。doctor 仍只报告
`configured_unverified`，不连接 TCP；线上只能由显式 `./ym-data smoke --live`
中经 canonical registry 直接取得 `pytdx_screener` 的脱敏结构化 case 验证。
该 case 不经过前四源，因此不会被更早成功或合法空集遮蔽；它只记录安全元数据。

## 完整投研 profile

问财网页降级运行时由项目统一安装到 `~/.ym-stock-data/runtimes/pywencai`：

```bash
./ym-data doctor --json
./ym-data setup pywencai
./ym-data doctor --json
```

`setup pywencai` 是显式写入命令，会先打印目标目录，再用 `uv venv --python 3.12` 创建隔离环境，并通过 `uv pip install --python <runtime-python> pywencai==0.13.1 pandas numpy` 安装固定兼容链。两个子进程均使用参数列表和 `shell=False`，失败输出只报告脱敏状态。问财 OpenAPI Key 应由进程环境或密钥管理器注入；不把编辑 shell rc 文件作为主要配置方式，也不要把 Key 写入仓库。

## TDX profile

TDX 是管道自有 OAuth 凭据下的只读增强源，不是通用自动替代源。首次登录必须由弈沐显式执行：

```bash
./ym-data auth status-tdx
./ym-data auth login-tdx
./ym-data auth status-tdx
```

`login-tdx` 由本管道完成 protected-resource / authorization-server
discovery、DCR、authorization-code、PKCE S256、localhost callback 和 state
校验。浏览器只在显式登录命令中打开，且只请求 `mcp.read`；返回其它 scope
（尤其 `mcp.write`）会 fail closed。默认 secure store 是 macOS Keychain，secret
不会进入 argv。只有明确选择显式 `--store file` 时才启用文件 fallback：目录 `0700`、文件和锁 `0600`、原子写入，并用跨线程/跨进程锁串行 refresh。可用 `--file-path` 选择自有 custom path；父目录、文件或锁只要是 symlink、非当前用户 ownership 或权限过宽就 fail closed，管道不会替调用方 chmod 任意既有目录。

文件 fallback 示例（路径必须由调用者明确给出或接受项目默认值）：

```bash
./ym-data auth login-tdx --store file
./ym-data auth login-tdx --store file --file-path /private/path/tdx.json
./ym-data auth status-tdx
```

只有 login 完整成功后，管道才以私有原子文件保存非敏感 store selector。后续
canonical query、doctor、smoke 与无 override 的 status 共用该选择；selector
记录可能包含 custom path，但 CLI、doctor 和 smoke receipt 只输出 store 类型，
不会输出路径。失败、取消、超时或 selector 写入失败均不切换当前选择。

管道不会读取或导入其它应用的凭据，也不会扫描外部 credential 目录。缺少
owned credential 时，doctor 报告 `auth_missing`；过期且不可 refresh 时报告
`auth_expired`。doctor 和 `status-tdx` 都离线，只输出脱敏状态。401 只强制
refresh 一次并重建 MCP session，最多重试一次；403 是 permission failure，
不会扩 scope 或伪装 expired。

MCP 使用固定生产依赖 `mcp==2.0.0` 的官方 Python SDK 与 Streamable HTTP；该
稳定版要求 Python 3.10+，与本项目 `requires-python >=3.10` 一致。直接使用的
SDK HTTP client 固定为 `httpx2==2.9.1`，Keychain adapter 固定为
`keyring==25.7.0`。
每次只读调用都必须先通过 `initialize`、`tools/list` 和本次请求 capability 的
allowlist schema gate；其它 capability 的缺失或 schema drift 不会连带禁用本次
能力，完整六项健康只能由 acceptance 1.3 / `five-source-capabilities-v1` 的 21-case smoke 分项验收；每项 receipt 只保留 initialize、tools/list、schema、read-only、tool-call 的 pass/fail 与页/session/refresh/call 计数，不保存 schema、content、endpoint 或 session 标识。任意额外、交易、写入工具会在 transport 前被拒绝。只有根线程后续取得
明确授权并完成真实 `tools/list` 与一个白名单只读小调用，才能称为在线。本轮
离线实现没有执行真实 DCR、没有打开浏览器，也不称为在线接通。

正式 live smoke 会在单次矩阵中继续执行所有 case，不因前项失败遮蔽后项；只有 OpenAPI、pywencai、TDX 六项、Wind 三项和 PyTDX direct 都返回非空 success，且 canonical 受控五源顺序与 injected/live origin 完全匹配，`gate_status` 才为 `pass`。一次 smoke 不授权首次 OAuth 登录，也不会自动安排五个交易日。

## Wind profile

Wind 仅用于显式研究增强与交叉验证，不参与实时行情的通用降级。`doctor` 只检查官方配置是否存在，并报告 `configured_unverified` 或 `unavailable`；它不读取或打印配置内容。在线可用性仍需后续显式只读 smoke 验证。

## 兼容入口边界

新代码使用：

```python
from ym_stock_data import query

result = query("stock_snapshot", codes=["600519"])
```

`fetch()` 与 `v2.resolve()` 仅是兼容投影。没有 canonical 等价 intent 的旧 880 板块、15 分钟指数、热点、北向、资金流与部分内容检索仍明确标记为 `legacy_direct`，构成永久兼容边界。它们不推荐新代码使用，不承诺迁移时间，也不会把 880 语义偷换成同花顺 881。
