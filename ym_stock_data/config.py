"""全局配置 — 服务器、缓存、API KEY 路径"""

from pathlib import Path

# 缓存目录
CACHE_DIR = Path.home() / ".ym-stock-data" / "cache"

# PyTDX 服务器列表 (IP, port)
PYTDX_SERVERS = [
    # 2026-07-13 业务探针验证：报价与日线均非空。全部为 PyTDX 公共零鉴权节点。
    ("123.125.108.14", 7709),
    ("115.238.56.198", 7709),
    ("60.12.136.250", 7709),
    ("115.238.90.165", 7709),
    # 80 端口用于部分网络环境无法直连 7709 时兜底。
    ("202.108.253.139", 80),
    ("180.153.18.172", 80),
]

# 连接超时 (秒) — 注意 PyTDX 的参数名是 time_out
PYTDX_CONNECT_TIMEOUT = 5
# 连接有效时长 (秒, 超过自动重连)
PYTDX_MAX_AGE = 60
# 连续失败 N 次切换兜底
PYTDX_MAX_FAIL = 3

# HTTP 请求超时
HTTP_TIMEOUT = 15

# 东方财富 HTTP 请求治理
EASTMONEY_MIN_INTERVAL = 1.0
EASTMONEY_JITTER_MIN = 0.1
EASTMONEY_JITTER_MAX = 0.5
EASTMONEY_BREAKER_SECONDS = 60
EASTMONEY_RATE_BREAKER_SECONDS = 300

# 问财 API KEY 路径
IWENCAI_API_KEY_PATH = Path.home() / ".zshrc"

# pywencai venv（OpenAPI 额度耗尽时自动降级）
PYWENCAI_VENV = str(Path.home() / ".workbuddy/binaries/python/envs/default/lib/python3.13/site-packages")

# pywencai + pytdx 降级 Python 路径（data-venv python3.12，已装 pywencai）
PYWENCAI_PYTHON = str(Path.home() / "WorkBuddy/Tools/data-venv/bin/python3")
