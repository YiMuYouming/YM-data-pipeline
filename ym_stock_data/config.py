"""全局配置 — 服务器、缓存、API KEY 路径"""

from pathlib import Path

# 缓存目录
CACHE_DIR = Path.home() / ".ym-stock-data" / "cache"

# PyTDX 服务器列表 (IP, port)
PYTDX_SERVERS = [
    ("110.41.147.114", 7709),
    ("119.147.212.81", 7709),
    ("124.70.176.52", 7709),
    ("47.100.236.28", 7709),
    ("121.36.54.217", 7709),
    ("124.71.85.110", 7709),
]

# 连接超时 (秒) — 注意 PyTDX 的参数名是 time_out
PYTDX_CONNECT_TIMEOUT = 5
# 连接有效时长 (秒, 超过自动重连)
PYTDX_MAX_AGE = 60
# 连续失败 N 次切换兜底
PYTDX_MAX_FAIL = 3

# HTTP 请求超时
HTTP_TIMEOUT = 15

# 问财 API KEY 路径
IWENCAI_API_KEY_PATH = Path.home() / ".zshrc"

# pywencai venv（OpenAPI 额度耗尽时自动降级）
PYWENCAI_VENV = str(Path.home() / "WorkBuddy/Tools/iwencai-venv/lib/python3.14/site-packages")
