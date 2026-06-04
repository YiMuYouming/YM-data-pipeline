"""问财数据查询 — OpenAPI 主路径 + pywencai 自动降级

用法:
    from ym_stock_data.sources import iwencai
    raw = iwencai.query("涨停 非st", limit=50)
    stocks = iwencai.query_stocks(["信维通信"])
    rank = iwencai.query_rank("信维通信")

降级策略:
    1. OpenAPI (v1/query2data) → 速度快、有额度限制
    2. pywencai 网页抓取 → 无额度、稍慢（自动切换）
"""

import os, re, json, urllib.request, secrets, sys
from pathlib import Path

IWENCAI_BASE = "https://openapi.iwencai.com"
_DEFAULT_FIELDS = ["涨跌幅", "成交额", "主力净流入", "换手率", "收盘价"]

_API_KEY = None
_PYWENCAI = None  # 惰性加载
_OPENAPI_DOWN_AT = 0  # OpenAPI 被拒绝的时间戳，5min 内不再尝试
_PYWENCAI_DOWN_AT = 0  # pywencai 降级失败的时间戳，5min 内不再尝试


def _load_api_key() -> str:
    global _API_KEY
    if _API_KEY:
        return _API_KEY
    key = os.environ.get("IWENCAI_API_KEY")
    if key:
        _API_KEY = key
        return key
    for rc in [os.path.expanduser(p) for p in ["~/.zshrc", "~/.bash_profile", "~/.bashrc"]]:
        try:
            with open(rc, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r'export\s+IWENCAI_API_KEY=["\']?(.+?)["\']?\s*$', line)
                    if m:
                        _API_KEY = m.group(1)
                        return _API_KEY
        except (FileNotFoundError, PermissionError):
            continue
    return None


def _iwencai_headers() -> dict:
    return {
        "Authorization": f"Bearer {_load_api_key()}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "X-Claw-Call-Type": "normal",
        "X-Claw-Skill-Id": "hithink-astock-selector",
        "X-Claw-Skill-Version": "2.0.0",
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": secrets.token_hex(32),
    }


def _get_pywencai():
    """惰性加载 pywencai（仅降级时触发）"""
    global _PYWENCAI
    if _PYWENCAI is not None:
        return _PYWENCAI
    try:
        from ..config import PYWENCAI_VENV
        if PYWENCAI_VENV not in sys.path:
            sys.path.insert(0, PYWENCAI_VENV)
    except ImportError:
        pass
    try:
        import pywencai as pw
        import pandas as pd
        import warnings
        warnings.filterwarnings("ignore")
        _PYWENCAI = (pw, pd)
        return _PYWENCAI
    except ImportError:
        _PYWENCAI = False
        return None


def _pywencai_query(query_str: str, limit: int = 50) -> dict:
    """pywencai 查询 → OpenAPI 兼容格式（subprocess 调用 data-venv Python，绕过代码签名冲突）"""
    import subprocess as _sp
    import json as _json
    
    try:
        from ..config import PYWENCAI_PYTHON as _py
    except ImportError:
        _py = str(Path.home() / "WorkBuddy/Tools/data-venv/bin/python3")
    
    _code = f"""
import sys, json, numpy as np
sys.path.insert(0, '{str(Path(__file__).parent.parent)}')
try:
    import pywencai as pw
    import pandas as pd
    result = pw.get(query={_json.dumps(query_str)}, loop_first=True)
    if result is None:
        print(json.dumps({{"datas": [], "columns": [], "row_count": 0, "_source": "pywencai"}}))
    elif isinstance(result, pd.DataFrame):
        if result.empty:
            print(json.dumps({{"datas": [], "columns": [], "row_count": 0, "_source": "pywencai"}}))
        else:
            datas = result.to_dict(orient="records")
            def native(v):
                if v is None: return None
                if isinstance(v, (np.integer,)): return int(v)
                if isinstance(v, (np.floating,)): return float(v) if v == v else None
                if isinstance(v, np.ndarray): return v.tolist()
                if isinstance(v, float) and (v != v): return None
                return v
            for row in datas:
                for k in row:
                    row[k] = native(row[k])
            print(json.dumps({{"datas": datas, "columns": [{{"index_name": c}} for c in result.columns],
                    "row_count": len(datas), "_source": "pywencai"}}, ensure_ascii=False))
    elif isinstance(result, dict):
        # 个股查询可能返回 dict 或 内含 DataFrame
        import pandas as _pd
        nested_df = None
        for _v in result.values():
            if isinstance(_v, _pd.DataFrame):
                nested_df = _v
                break
        if nested_df is not None:
            # dict 内含 DataFrame → 降级为 DataFrame 路径
            if nested_df.empty:
                print(json.dumps({{"datas": [], "columns": [], "row_count": 0, "_source": "pywencai"}}))
            else:
                datas = nested_df.to_dict(orient="records")
                def native(v):
                    if v is None: return None
                    if isinstance(v, (np.integer,)): return int(v)
                    if isinstance(v, (np.floating,)): return float(v) if v == v else None
                    if isinstance(v, np.ndarray): return v.tolist()
                    if isinstance(v, float) and (v != v): return None
                    return v
                for row in datas:
                    for k in row:
                        row[k] = native(row[k])
                print(json.dumps({{"datas": datas, "columns": [{{"index_name": c}} for c in nested_df.columns],
                        "row_count": len(datas), "_source": "pywencai"}}, ensure_ascii=False))
        else:
            # 纯 dict（个股单行数据）
            datas = [result]
            has_nested = any(isinstance(v, (list, dict, _pd.DataFrame, _pd.Series)) for v in result.values())
            if has_nested:
                print(json.dumps({{"error": f"dict有嵌套:{{{{k:type(v).__name__ for k,v in result.items()}}}}",
                        "query": {_json.dumps(query_str)}, "_source": "pywencai"}}))
            else:
                print(json.dumps({{"datas": datas, "columns": [{{"index_name": k}} for k in result.keys()],
                        "row_count": 1, "_source": "pywencai"}}, ensure_ascii=False))
    else:
        print(json.dumps({{"error": f"未知类型: {{type(result).__name__}}", "query": {_json.dumps(query_str)}, "_source": "pywencai"}}))
except Exception as e:
    print(json.dumps({{"error": str(e), "query": {_json.dumps(query_str)}, "_source": "pywencai"}}))
"""
    try:
        r = _sp.run([_py, "-c", _code], capture_output=True, text=True, timeout=45)
        out = r.stdout.strip()
        if out:
            return _json.loads(out)
        return {"error": r.stderr[:200], "query": query_str, "_source": "pywencai"}
    except _sp.TimeoutExpired:
        return {"error": "timeout", "query": query_str, "_source": "pywencai"}
    except Exception as e:
        return {"error": str(e), "query": query_str, "_source": "pywencai"}


def _pywencai_or_all_dead(query_str: str, limit: int = 50, openapi_error: str = None) -> dict:
    """Run pywencai fallback once, then cache failure for the current process."""
    import time as _t

    global _PYWENCAI_DOWN_AT
    fallback = _pywencai_query(query_str, limit)
    if "error" not in fallback:
        _PYWENCAI_DOWN_AT = 0
        return fallback

    _PYWENCAI_DOWN_AT = _t.time()
    result = {
        "error": "OpenAPI 额度耗尽 + pywencai 不可用，额度明日重置",
        "error_type": "all_paths_dead",
        "pywencai": fallback.get("error", "unknown"),
        "query": query_str,
        "_source": "none",
    }
    if openapi_error:
        result["openapi"] = openapi_error
    return result


def query(query_str: str, limit: int = 50, page: int = 1) -> dict:
    """问财查询 → OpenAPI 优先，失败自动降级 pywencai

    Returns:
        {columns: [...], datas: [{列名:值}, ...], row_count: N, _source: "openapi"|"pywencai"}
    """
    key = _load_api_key()
    if not key:
        return _pywencai_query(query_str, limit)

    # OpenAPI 被拒绝后 5min 内直接走 pywencai，不再浪费请求
    # pywencai 也挂了则直接返回诊断，不重试
    global _OPENAPI_DOWN_AT, _PYWENCAI_DOWN_AT
    if _OPENAPI_DOWN_AT and (__import__("time").time() - _OPENAPI_DOWN_AT) < 300:
        if _PYWENCAI_DOWN_AT and (__import__("time").time() - _PYWENCAI_DOWN_AT) < 300:
            return {
                "error": "OpenAPI 额度耗尽 + pywencai 不可用，额度明日重置",
                "error_type": "all_paths_dead",
                "query": query_str, "_source": "none",
            }
        return _pywencai_or_all_dead(query_str, limit)

    req = urllib.request.Request(
        f"{IWENCAI_BASE}/v1/query2data",
        data=json.dumps({
            "query": query_str, "page": str(page),
            "limit": str(limit), "is_cache": "1", "expand_index": "true",
        }).encode("utf-8"),
        headers=_iwencai_headers(),
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            result["_source"] = "openapi"
            _OPENAPI_DOWN_AT = 0  # 恢复
            return result
    except urllib.request.HTTPError as e:
        # 401/429/403 → 标记不可用，降级 pywencai
        if e.code in (401, 403, 429):
            import time as _t
            _OPENAPI_DOWN_AT = _t.time()
            # pywencai 最近也挂了 → 跳过重试，返回完整诊断
            if _PYWENCAI_DOWN_AT and (_t.time() - _PYWENCAI_DOWN_AT) < 300:
                return {
                    "error": f"OpenAPI {e.code} + pywencai 均不可用（已缓存），额度明日重置",
                    "error_type": "all_paths_dead",
                    "query": query_str, "_source": "none",
                }
            return _pywencai_or_all_dead(query_str, limit, openapi_error=f"HTTP {e.code}")
        return {"error": f"HTTP {e.code}: {e.reason}", "query": query_str}
    except Exception as e:
        return {"error": str(e), "query": query_str}


def search(query_str: str, channel: str = "report", size: int = 30) -> dict:
    """问财综合搜索 → /v1/comprehensive/search（仅 OpenAPI，不支持 pywencai）"""
    key = _load_api_key()
    if not key:
        raise ValueError("IWENCAI_API_KEY 未设置")
    req = urllib.request.Request(
        f"{IWENCAI_BASE}/v1/comprehensive/search",
        data=json.dumps({
            "channels": channel, "app_id": "AIME_SKILL",
            "query": query_str, "size": size,
        }).encode("utf-8"),
        headers=_iwencai_headers(),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.request.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}", "query": query_str}
    except Exception as e:
        return {"error": str(e), "query": query_str}
    if raw.get("status_code") != 0:
        return {"error": f"status_code={raw.get('status_code')}", "query": query_str}
    articles = raw.get("articles", raw.get("data", []))
    return {"articles": articles, "total": len(articles), "channel": channel, "query": query_str}


def query_stocks(codes_or_names: list, fields: list = None) -> dict:
    if not codes_or_names:
        return {}
    f = fields or _DEFAULT_FIELDS
    f_str = " ".join(f) if isinstance(f, list) else f
    raw = query(" ".join(codes_or_names) + " " + f_str, limit=len(codes_or_names))
    if "error" in raw:
        return {name: {} for name in codes_or_names}
    datas = raw.get("datas", [])
    result = {}
    for item in datas:
        if not isinstance(item, dict):
            continue
        key = item.get("股票代码", "") or item.get("code", "")
        if not key:
            continue
        result[key] = item
    return result


def query_rank(code_or_name: str) -> dict:
    raw = query(f"{code_or_name} 今日热股人气排名 热度值", limit=1)
    if "error" in raw:
        return raw
    datas = raw.get("datas", [])
    if datas:
        return datas[0]
    return {"error": "无数据"}
