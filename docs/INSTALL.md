# ym-stock-data 安装与 provider 配置

## 基础安装与诊断

```bash
uv sync
uv run ym-data doctor --json
```

`doctor` 只读检查每个 provider，不发在线查询，不输出 token、异常正文或查询结果行。供应商在线状态必须通过显式只读探针验证，不能由单元测试或 `configured_unverified` 推断。

## 零鉴权 profile

基础安装提供 PyTDX、腾讯、东方财富、同花顺、巨潮与财联社等零鉴权通道。先运行：

```bash
uv sync
uv run ym-data doctor --json
uv run ym-data query realtime_market
```

零鉴权不代表在线可用；以 query 返回的 `provider_used`、`attempts`、`quality` 与 freshness 为准。

## 完整投研 profile

问财网页降级运行时由项目统一安装到 `~/.ym-stock-data/runtimes/pywencai`：

```bash
uv sync
uv run ym-data doctor --json
uv run ym-data setup pywencai
uv run ym-data doctor --json
```

`setup pywencai` 是显式写入命令，会先打印目标目录，再用 `uv venv --python 3.12` 创建隔离环境，并通过 `uv pip install --python <runtime-python> pywencai==0.13.1 pandas numpy` 安装固定兼容链。两个子进程均使用参数列表和 `shell=False`，失败输出只报告脱敏状态。问财 OpenAPI Key 应由进程环境或密钥管理器注入；不把编辑 shell rc 文件作为主要配置方式，也不要把 Key 写入仓库。

## TDX profile

TDX 是管道自有 OAuth 凭据下的只读增强源，不是通用自动替代源。Task 8 只提供安全命令入口：

```bash
uv run ym-data auth import-tdx --from-workbuddy
```

当前命令会先打印计划目标 `~/.ym-stock-data/auth/tdx.json`，随后明确报告 Task 9 尚未就绪；它不会扫描 WorkBuddy，也不会写入凭据。凭据导入、刷新和 MCP 只读适配必须在 Task 9 完成后才能使用。

## Wind profile

Wind 仅用于显式研究增强与交叉验证，不参与实时行情的通用降级。`doctor` 只检查官方配置是否存在，并报告 `configured_unverified` 或 `unavailable`；它不读取或打印配置内容。在线可用性仍需后续显式只读 smoke 验证。

## 兼容入口边界

新代码使用：

```python
from ym_stock_data import query

result = query("stock_snapshot", codes=["600519"])
```

`fetch()` 与 `v2.resolve()` 仅是兼容投影。旧 880 板块、15 分钟指数、热点、北向、资金流与部分内容检索仍明确标记为 `legacy_direct`，等待 Task 13 补齐等价 intent 或迁移消费者；不会把 880 语义偷换成同花顺 881。
