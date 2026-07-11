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

import http.client
import os, re, json, socket, urllib.error, urllib.request, secrets, sys, time
from datetime import datetime, timezone
from pathlib import Path

IWENCAI_BASE = "https://openapi.iwencai.com"
_DEFAULT_FIELDS = ["涨跌幅", "成交额", "主力净流入", "换手率", "收盘价"]

_API_KEY = None
_PYWENCAI = None  # 惰性加载
_OPENAPI_DOWN_AT = 0  # OpenAPI 被拒绝的时间戳，5min 内不再尝试
_PYWENCAI_DOWN_AT = 0  # pywencai 降级失败的时间戳，5min 内不再尝试
_OPENAPI_BREAKER_AT = 0
_OPENAPI_BREAKER_SECONDS = 300
_OPENAPI_FAILURE_TYPE = "rate_limit"
_OPENAPI_LAST_ERROR = None
_PYWENCAI_LAST_ERROR = None


class _InvalidOpenAPIResponse(ValueError):
    """OpenAPI returned valid JSON that does not satisfy its object contract."""


def _rows_from_openapi_container(value) -> list[dict] | None:
    if isinstance(value, list):
        if all(isinstance(row, dict) for row in value):
            return value
        return None
    if isinstance(value, dict):
        for key in ("datas", "rows", "items", "list", "result", "data"):
            if key in value:
                rows = _rows_from_openapi_container(value[key])
                if rows is not None:
                    return rows
    return None


def _validate_openapi_result(result) -> dict:
    if not isinstance(result, dict) or not result or result.get("error"):
        raise _InvalidOpenAPIResponse(
            f"expected non-error object with rows, got {type(result).__name__}"
        )

    rows = None
    for key in ("datas", "data", "result"):
        if key in result:
            rows = _rows_from_openapi_container(result[key])
            if rows is not None:
                break
    if rows is None:
        raise _InvalidOpenAPIResponse("response has no parseable data/result rows")

    result.setdefault("datas", rows)
    result.setdefault("row_count", len(rows))
    return result


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
            return _limit_pywencai_rows(_json.loads(out), limit)
        return {"error": r.stderr[:200], "query": query_str, "_source": "pywencai"}
    except _sp.TimeoutExpired:
        return {"error": "timeout", "query": query_str, "_source": "pywencai"}
    except Exception as e:
        return {"error": str(e), "query": query_str, "_source": "pywencai"}


def _limit_pywencai_rows(result: dict, limit: int) -> dict:
    """Apply the public query limit to a pywencai-compatible payload."""
    if not isinstance(result, dict):
        return result
    limited = dict(result)
    datas = result.get("datas")
    if isinstance(datas, list):
        row_limit = max(0, int(limit))
        limited["datas"] = datas[:row_limit]
        limited["row_count"] = len(limited["datas"])
    return limited


def _set_openapi_breaker(*, failure_type: str, error: str, seconds: int) -> dict:
    global _OPENAPI_DOWN_AT, _OPENAPI_BREAKER_AT
    global _OPENAPI_BREAKER_SECONDS, _OPENAPI_FAILURE_TYPE, _OPENAPI_LAST_ERROR

    now = time.time()
    _OPENAPI_DOWN_AT = now
    _OPENAPI_BREAKER_AT = now
    _OPENAPI_BREAKER_SECONDS = seconds
    _OPENAPI_FAILURE_TYPE = failure_type
    _OPENAPI_LAST_ERROR = error
    return {
        "failure_type": failure_type,
        "openapi_error": error,
        "breaker_seconds": seconds,
    }


def _active_openapi_breaker() -> dict | None:
    if not _OPENAPI_DOWN_AT:
        return None

    if _OPENAPI_BREAKER_AT == _OPENAPI_DOWN_AT:
        seconds = _OPENAPI_BREAKER_SECONDS
        failure_type = _OPENAPI_FAILURE_TYPE
        openapi_error = _OPENAPI_LAST_ERROR
    else:
        # Backward compatibility for callers/tests that set only
        # _OPENAPI_DOWN_AT under the historical 300-second contract.
        seconds = 300
        failure_type = "rate_limit"
        openapi_error = None

    if time.time() - _OPENAPI_DOWN_AT >= seconds:
        return None
    return {
        "failure_type": failure_type,
        "openapi_error": openapi_error,
        "breaker_seconds": seconds,
    }


def _fallback_meta(result: dict, context: dict, *, requested_count: int) -> dict:
    enriched = dict(result)
    meta = dict(enriched.get("_meta", {}))
    requested_count = max(0, int(requested_count))
    datas = enriched.get("datas")
    returned_count = len(datas) if isinstance(datas, list) else 0
    meta.update({
        "fallback_from": "openapi",
        "fallback_to": "pywencai",
        "failure_type": context["failure_type"],
        "provider": enriched.get("_source") or "none",
        "query_time": datetime.fromtimestamp(time.time(), timezone.utc).isoformat(),
        "coverage": {
            "requested_count": requested_count,
            "returned_count": returned_count,
            "ratio": returned_count / requested_count if requested_count else None,
        },
        "fallback_reason": context["failure_type"],
    })
    if context.get("openapi_error"):
        meta["openapi_error"] = context["openapi_error"]
    enriched["_meta"] = meta
    return enriched


def _all_paths_dead_result(
    query_str: str,
    *,
    context: dict,
    pywencai_error: str,
    requested_count: int,
) -> dict:
    failure_type = context["failure_type"]
    if failure_type == "rate_limit":
        message = "OpenAPI 限流且 pywencai 不可用"
    elif failure_type == "auth":
        message = "OpenAPI 鉴权失败且 pywencai 不可用"
    else:
        message = "OpenAPI 不可用且 pywencai 不可用"

    result = {
        "error": message,
        "error_type": "all_paths_dead",
        "pywencai": pywencai_error,
        "query": query_str,
        "_source": "none",
    }
    if context.get("openapi_error"):
        result["openapi"] = context["openapi_error"]
    return _fallback_meta(result, context, requested_count=requested_count)


def _pywencai_or_all_dead(
    query_str: str,
    limit: int = 50,
    *,
    context: dict | None = None,
    openapi_error: str | None = None,
) -> dict:
    """Run pywencai fallback once, then cache failure for the current process."""
    global _PYWENCAI_DOWN_AT, _PYWENCAI_LAST_ERROR
    if context is None:
        context = {
            "failure_type": "rate_limit",
            "openapi_error": openapi_error,
            "breaker_seconds": 300,
        }

    fallback = _limit_pywencai_rows(_pywencai_query(query_str, limit), limit)
    if "error" not in fallback:
        _PYWENCAI_DOWN_AT = 0
        _PYWENCAI_LAST_ERROR = None
        return _fallback_meta(fallback, context, requested_count=limit)

    _PYWENCAI_DOWN_AT = time.time()
    _PYWENCAI_LAST_ERROR = fallback.get("error", "unknown")
    return _all_paths_dead_result(
        query_str,
        context=context,
        pywencai_error=_PYWENCAI_LAST_ERROR,
        requested_count=limit,
    )


def _dispatch_pywencai_fallback(query_str: str, limit: int, *, context: dict) -> dict:
    """Honor the pywencai failure cache for every OpenAPI failure class."""
    if _PYWENCAI_DOWN_AT and (time.time() - _PYWENCAI_DOWN_AT) < 300:
        return _all_paths_dead_result(
            query_str,
            context=context,
            pywencai_error=_PYWENCAI_LAST_ERROR or "cached failure",
            requested_count=limit,
        )
    return _pywencai_or_all_dead(query_str, limit, context=context)


def query(query_str: str, limit: int = 50, page: int = 1) -> dict:
    """问财查询 → OpenAPI 优先，失败自动降级 pywencai

    Returns:
        {columns: [...], datas: [{列名:值}, ...], row_count: N, _source: "openapi"|"pywencai"}
    """
    key = _load_api_key()
    if not key:
        return _limit_pywencai_rows(_pywencai_query(query_str, limit), limit)

    # OpenAPI 熔断期间直接走 pywencai，不再浪费请求。
    # 鉴权/限流保持历史 300s，5xx/网络错误使用 60s。
    # pywencai 也挂了则直接返回诊断，不重试
    global _OPENAPI_DOWN_AT, _PYWENCAI_DOWN_AT
    breaker = _active_openapi_breaker()
    if breaker:
        return _dispatch_pywencai_fallback(query_str, limit, context=breaker)

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
            result = _validate_openapi_result(
                json.loads(resp.read().decode("utf-8"))
            )
            result["_source"] = "openapi"
            _OPENAPI_DOWN_AT = 0  # 恢复
            return result
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429):
            failure_type = "rate_limit" if e.code == 429 else "auth"
            context = _set_openapi_breaker(
                failure_type=failure_type,
                error=f"HTTP {e.code}",
                seconds=300,
            )
            return _dispatch_pywencai_fallback(query_str, limit, context=context)
        if 500 <= e.code <= 599:
            context = _set_openapi_breaker(
                failure_type="http_5xx",
                error=f"HTTP {e.code}: {e.reason}",
                seconds=60,
            )
            return _dispatch_pywencai_fallback(query_str, limit, context=context)
        return {"error": f"HTTP {e.code}: {e.reason}", "query": query_str}
    except (TimeoutError, socket.timeout) as e:
        context = _set_openapi_breaker(
            failure_type="timeout",
            error=str(e) or "timeout",
            seconds=60,
        )
        return _dispatch_pywencai_fallback(query_str, limit, context=context)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        is_timeout = isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower()
        context = _set_openapi_breaker(
            failure_type="timeout" if is_timeout else "network",
            error=str(e),
            seconds=60,
        )
        return _dispatch_pywencai_fallback(query_str, limit, context=context)
    except (json.JSONDecodeError, UnicodeDecodeError, _InvalidOpenAPIResponse) as e:
        context = _set_openapi_breaker(
            failure_type="invalid_response",
            error=str(e),
            seconds=60,
        )
        return _dispatch_pywencai_fallback(query_str, limit, context=context)
    except http.client.HTTPException as e:
        context = _set_openapi_breaker(
            failure_type="network",
            error=str(e),
            seconds=60,
        )
        return _dispatch_pywencai_fallback(query_str, limit, context=context)
    except OSError as e:
        context = _set_openapi_breaker(
            failure_type="network",
            error=str(e),
            seconds=60,
        )
        return _dispatch_pywencai_fallback(query_str, limit, context=context)
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
