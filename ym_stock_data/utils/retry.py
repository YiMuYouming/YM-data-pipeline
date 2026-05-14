"""重试装饰器 + HTTP 工具"""

import time, functools


def retry(max_attempts=3, delay=1.0, backoff=2.0, exceptions=(Exception,)):
    """重试装饰器：失败后等待 delay*backoff^n 秒再试"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_err = e
                    if attempt < max_attempts - 1:
                        time.sleep(delay * (backoff ** attempt))
            raise last_err
        return wrapper
    return decorator


def http_get(url, params=None, headers=None, timeout=15):
    """安全 HTTP GET，自动抛异常"""
    import requests
    resp = requests.get(url, params=params, headers=headers or {}, timeout=timeout)
    resp.raise_for_status()
    return resp


def http_post(url, data=None, headers=None, timeout=15):
    """安全 HTTP POST"""
    import requests
    resp = requests.post(url, data=data, headers=headers or {}, timeout=timeout)
    resp.raise_for_status()
    return resp
