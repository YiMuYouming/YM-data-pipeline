# 外部 A 股能力渐进整合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有 V1 `fetch()`、V2 `resolve()` 和生产消费者边界的前提下，把问财/通达信官方能力与 `simonlin1212/a-stock-data` 中已验证的端点经验渐进吸收到 YM-data-pipeline。

**Architecture:** 保持“两层入口、一个 source 层”的现状：现有 V1/V2 入口只做兼容性扩展，所有网络实现继续落在 `ym_stock_data/sources/`。TDX MCP 保持独立的人工备用源，不 import 进 Python 自动降级链；`a-stock-data` 只作为端点与失败知识来源，代码必须重写进现有 `_meta`、质量、线程安全和测试契约。

**Tech Stack:** Python 3.10+、`requests`、`urllib`、`unittest`、现有 PyTDX/问财 V2、Codex TDX MCP stdio wrapper；不新增生产依赖。

> **2026-07-14 联动更新：** 本计划是数据能力实现细案；跨项目顺序、补充能力和 YiMu_IR 接入门槛以 [`2026-07-14-pipeline-to-ir-research-upgrade.md`](./2026-07-14-pipeline-to-ir-research-upgrade.md) 为准。YiMu_IR 的 Skill 改造必须等 `capability_manifest` 契约稳定后执行，不能直接假设新 intent 已可用。

---

## 0. 不变边界与晋升门槛

### 必须保持不变

- `from ym_stock_data import fetch` 继续可用，已有 route 的参数和顶层字段不删除。
- `resolve()` 现有五个 intent 的输入、`data + _meta` 形状和 quality 语义不变。
- `live-dashboard`、Market Watch、复盘脚本本计划内不改，不自动切换新 intent。
- TDX MCP 不成为自动主源或自动 fallback；OAuth 失效时必须显式报错。
- 不安装 `a-stock-data` 的全局 Skill，不引入 `mootdx`、`stockstats`。
- 不关闭 TLS 校验，不复制对方 `CERT_NONE` 备用实现。

### 新能力三段晋升

```text
外部端点/官方工具
       ↓  Gate A：fixture 单测 + 失败契约 + source 时间戳
sources/ 旁路函数
       ↓  Gate B：live smoke + 5 日对账 + 无旧接口回归
V1 新 route（只增不改）
       ↓  Gate C：字段策略 + quality + source_chain
V2 新 intent（仍不接生产消费者）
```

### 分期顺序

1. 第一批：修复已经坐实的旧接口，不改变业务契约。
2. 第二批：建立线程安全的东财请求治理层。
3. 第三批：增加涨跌停情绪与低频股票事件旁路能力。
4. 第四批：补问财内容搜索、固化 TDX MCP 人工备用流程。
5. 第五批：旁路晋升 V2、跑五个交易日双轨对账。

---

## 1. 文件结构锁定

### 新建

| 文件 | 单一职责 |
| --- | --- |
| `ym_stock_data/sources/eastmoney_http.py` | 东财域名的线程安全 session、节流、重试、breaker |
| `ym_stock_data/sources/limit_state.py` | 涨停/炸板/跌停/昨涨停池及市场情绪聚合 |
| `ym_stock_data/sources/stock_events.py` | 解禁、两融、大宗、股东户数、分红等低频事实 |
| `ym_stock_data/sources/iwencai_content.py` | 问财研报/公告/新闻内容搜索，不改变结构化选股 `iwencai.query()` |
| `tests/test_eastmoney_http.py` | 请求治理的并发、节流、breaker 测试 |
| `tests/test_limit_state.py` | 涨跌停解析和情绪计算测试 |
| `tests/test_stock_events.py` | 低频事件字段映射测试 |
| `tests/test_iwencai_content.py` | 内容搜索鉴权、错误与去重测试 |
| `scripts/compare_external_sources.py` | 旁路对账，结果只写用户缓存目录，不进 Git |
| `docs/TDX-MCP-备用源验证清单.md` | OAuth、工具契约、20 例验证与来源标注规则 |
| `THIRD_PARTY_NOTICES.md` | 记录 `a-stock-data` Apache-2.0 来源和改写范围 |

### 只做兼容修改

| 文件 | 允许修改 |
| --- | --- |
| `ym_stock_data/sources/news.py` | 旧财联社 URL 切到 v1 签名接口，保持返回字段 |
| `ym_stock_data/sources/research.py` | 东财服务端按 `code` 过滤，保持返回字段 |
| `ym_stock_data/sources/northbound.py` | 只增加可靠性元数据，不删除 `hgt/sgt` 字段 |
| `ym_stock_data/fetch.py` | 仅追加新 route，不改旧 route |
| `ym_stock_data/v2/adapters.py` | 仅追加新 adapter |
| `ym_stock_data/v2/resolve.py` | 仅追加新 intent 分支和 `SUPPORTED_INTENTS` 项 |
| `ym_stock_data/v2/policies/fields.json` | 仅追加字段策略 |
| `README.md`、`AGENTS.md` | 新入口、边界和验证命令 |

---

## Task 1: 冻结旧接口契约并修复财联社、研报

**Files:**
- Create: `tests/test_news_research_contracts.py`
- Modify: `ym_stock_data/sources/news.py`
- Modify: `ym_stock_data/sources/research.py`

- [ ] **Step 1: 为财联社新签名和原返回形状写失败测试**

```python
import unittest
from unittest.mock import Mock, patch

from ym_stock_data.sources import news


class NewsContractTests(unittest.TestCase):
    @patch("ym_stock_data.sources.news.requests.get")
    def test_fetch_news_uses_signed_v1_endpoint_and_keeps_shape(self, get):
        response = Mock()
        response.json.return_value = {
            "errno": 0,
            "data": {"roll_data": [{
                "id": 7,
                "ctime": 1783990800,
                "title": "测试标题",
                "content": "测试正文",
            }]},
        }
        get.return_value = response

        result = news.fetch_news(limit=1)

        url = get.call_args.args[0]
        self.assertIn("/v1/roll/get_roll_list?", url)
        self.assertIn("sign=", url)
        self.assertEqual(1, result["total"])
        self.assertEqual("测试标题", result["items"][0]["title"])
        self.assertEqual("cls_telegraph", result["source"])
```

- [ ] **Step 2: 为研报服务端过滤和原返回形状写失败测试**

```python
from ym_stock_data.sources import research


class ResearchContractTests(unittest.TestCase):
    @patch("ym_stock_data.sources.research.requests.get")
    def test_fetch_reports_sends_code_to_server(self, get):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "data": [{
                "stockCode": "600519",
                "title": "公司研报",
                "publishDate": "2026-07-14",
                "orgSName": "机构",
                "emRatingName": "增持",
                "predictThisYearEps": 10,
                "predictNextYearEps": 11,
                "predictNextTwoYearEps": 12,
                "infoCode": "ABC",
            }],
            "TotalPage": 1,
        }
        get.return_value = response

        result = research.fetch_reports("600519", days=90, max_pages=1)

        self.assertEqual("600519", get.call_args.kwargs["params"]["code"])
        self.assertEqual(1, result["total"])
        self.assertEqual("eastmoney_reportapi", result["source"])
```

- [ ] **Step 3: 运行定向测试，确认先失败**

Run:

```bash
uv run python -m unittest tests.test_news_research_contracts -v
```

Expected: 财联社断言因仍为 `nodeapi` 失败；研报断言因 params 无 `code` 失败。

- [ ] **Step 4: 最小修改 `news.py`，保留旧返回契约**

在 `fetch_news()` 请求前加入签名 helper，并把解析改为 `data.roll_data`：

```python
import hashlib


def _signed_roll_url(limit: int) -> str:
    params = {
        "appName": "CailianpressWeb",
        "os": "web",
        "sv": "7.7.5",
        "last_time": "",
        "refresh_type": "1",
        "rn": str(limit),
    }
    query = "&".join(f"{key}={params[key]}" for key in sorted(params))
    sha1 = hashlib.sha1(query.encode("utf-8")).hexdigest()
    sign = hashlib.md5(sha1.encode("utf-8")).hexdigest()
    return f"https://www.cls.cn/v1/roll/get_roll_list?{query}&sign={sign}"
```

请求必须继续带超时、UA、Referer；HTTP/JSON 错误继续返回 `{total, items, error}`，不得向调用方抛未处理异常。

- [ ] **Step 5: 最小修改 `research.py`，由服务端按代码过滤**

每页 params 使用以下字段；删除“拉全市场后客户端匹配”的必要性，但保留防御性代码校验：

```python
params = {
    "pageSize": 50,
    "pageNo": page,
    "qType": 0,
    "beginTime": start,
    "endTime": end,
    "industryCode": "*",
    "industry": "*",
    "rating": "*",
    "ratingChange": "*",
    "orgCode": "",
    "code": code,
    "rcode": "",
}
```

翻页停止条件必须同时支持空页和 `TotalPage`：

```python
items = data.get("data") or []
if not items:
    break
all_reports.extend(item for item in items if item.get("stockCode") == code)
if page >= int(data.get("TotalPage") or 1):
    break
```

- [ ] **Step 6: 运行定向测试和全套测试**

Run:

```bash
uv run python -m unittest tests.test_news_research_contracts -v
uv run python -m unittest discover -s tests -v
```

Expected: 定向测试通过；现有 66 项加新增用例全部通过。

- [ ] **Step 7: 运行两项低频 live smoke**

Run:

```bash
uv run python - <<'PY'
from ym_stock_data import fetch

news = fetch("news", limit=3)
reports = fetch("research", code="600519", days=90, max_pages=1)
print(news["total"], news["_meta"])
print(reports["total"], reports["_meta"])
assert news["total"] > 0
assert reports["total"] > 0
PY
```

Expected: 两个断言通过；不创建仓库内文件。

- [ ] **Step 8: 提交独立修复**

```bash
git add ym_stock_data/sources/news.py ym_stock_data/sources/research.py tests/test_news_research_contracts.py
git commit -m "fix: refresh news and research endpoints"
```

---

## Task 2: 建立线程安全的东财请求治理层

**Files:**
- Create: `ym_stock_data/sources/eastmoney_http.py`
- Create: `tests/test_eastmoney_http.py`
- Modify: `ym_stock_data/config.py`
- Modify: `ym_stock_data/sources/eastmoney.py`
- Modify: `ym_stock_data/sources/research.py`
- Modify: `tests/test_news_research_contracts.py`

- [ ] **Step 1: 写节流、429 breaker、403 breaker 失败测试**

```python
import unittest
from unittest.mock import Mock, patch

from ym_stock_data.sources.eastmoney_http import EastmoneyClient


class EastmoneyClientTests(unittest.TestCase):
    def test_second_request_waits_for_minimum_interval(self):
        client = EastmoneyClient(min_interval=1.0, jitter=(0.0, 0.0))
        response = Mock(status_code=200)
        with patch.object(client.session, "get", return_value=response), \
             patch("ym_stock_data.sources.eastmoney_http.time.monotonic", side_effect=[10.0, 10.0, 10.2, 11.0]), \
             patch("ym_stock_data.sources.eastmoney_http.time.sleep") as sleep:
            client.get("https://example.eastmoney.com/a")
            client.get("https://example.eastmoney.com/b")
        sleep.assert_called_once_with(0.8)

    def test_403_opens_breaker_without_immediate_retry(self):
        client = EastmoneyClient(min_interval=0, breaker_seconds=60)
        response = Mock(status_code=403)
        with patch.object(client.session, "get", return_value=response) as get, \
             patch("ym_stock_data.sources.eastmoney_http.time.monotonic", return_value=100.0):
            first = client.get("https://example.eastmoney.com/a")
            second = client.get("https://example.eastmoney.com/a")
        self.assertEqual(403, first.status_code)
        self.assertTrue(second.skipped_by_breaker)
        get.assert_called_once()
```

- [ ] **Step 2: 运行测试确认模块尚不存在**

Run:

```bash
uv run python -m unittest tests.test_eastmoney_http -v
```

Expected: FAIL，`ModuleNotFoundError: ym_stock_data.sources.eastmoney_http`。

- [ ] **Step 3: 在 `config.py` 增加仅供东财客户端使用的配置**

```python
EASTMONEY_MIN_INTERVAL = 1.0
EASTMONEY_JITTER_MIN = 0.1
EASTMONEY_JITTER_MAX = 0.5
EASTMONEY_BREAKER_SECONDS = 60
EASTMONEY_RATE_BREAKER_SECONDS = 300
```

- [ ] **Step 4: 实现 `EastmoneyClient`**

实现要求：

```python
from dataclasses import dataclass
import random
import threading
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


@dataclass
class BreakerResponse:
    status_code: int = 0
    skipped_by_breaker: bool = True
    reason: str = "eastmoney_breaker_open"

    def json(self):
        return {"error": self.reason}


class EastmoneyClient:
    def __init__(self, min_interval=1.0, jitter=(0.1, 0.5), breaker_seconds=60):
        self.min_interval = min_interval
        self.jitter = jitter
        self.breaker_seconds = breaker_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.6,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._breaker_until = 0.0

    def get(self, url, *, params=None, headers=None, timeout=15, **kwargs):
        with self._lock:
            now = time.monotonic()
            if now < self._breaker_until:
                return BreakerResponse()
            wait = self.min_interval - (now - self._last_call)
            if wait > 0:
                wait += random.uniform(*self.jitter)
                time.sleep(wait)
            response = self.session.get(
                url, params=params, headers=headers, timeout=timeout, **kwargs
            )
            self._last_call = time.monotonic()
            if response.status_code == 403:
                self._breaker_until = self._last_call + self.breaker_seconds
            elif response.status_code == 429:
                self._breaker_until = self._last_call + 300
            return response


CLIENT = EastmoneyClient()

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def query_datacenter(
    report_name: str,
    *,
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict]:
    response = CLIENT.get(
        DATACENTER_URL,
        params={
            "reportName": report_name,
            "columns": "ALL",
            "filter": filter_str,
            "pageNumber": "1",
            "pageSize": str(page_size),
            "sortColumns": sort_columns,
            "sortTypes": sort_types,
            "source": "WEB",
            "client": "WEB",
        },
        headers={"Referer": "https://data.eastmoney.com/"},
        timeout=15,
    )
    if getattr(response, "skipped_by_breaker", False):
        raise RuntimeError(response.reason)
    response.raise_for_status()
    payload = response.json()
    return ((payload.get("result") or {}).get("data") or [])
```

- [ ] **Step 5: 只迁移两个已有东财调用方**

在 `eastmoney.py` 与 `research.py` 中使用：

```python
from .eastmoney_http import CLIENT

response = CLIENT.get(url, params=params, headers=headers, timeout=15)
if getattr(response, "skipped_by_breaker", False):
    return {
        "error": response.reason,
        "error_type": "breaker_open",
        "_source": "none",
    }
```

不要在本任务迁移其他 source，控制回归面。

同步把 Task 1 的研报测试 patch 点改为新边界，避免测试绕开客户端：

```python
@patch("ym_stock_data.sources.research.CLIENT.get")
def test_fetch_reports_sends_code_to_server(self, get):
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "data": [{"stockCode": "600519", "title": "公司研报"}],
        "TotalPage": 1,
    }
    get.return_value = response
    result = research.fetch_reports("600519", days=90, max_pages=1)
    self.assertEqual("600519", get.call_args.kwargs["params"]["code"])
    self.assertEqual(1, result["total"])
```

- [ ] **Step 6: 运行定向和全量测试**

```bash
uv run python -m unittest tests.test_eastmoney_http tests.test_news_research_contracts -v
uv run python -m unittest discover -s tests -v
```

Expected: 所有用例通过；现有 `fetch("dragon_tiger")`、`fetch("research")` 顶层 shape 不变。

- [ ] **Step 7: 提交请求治理层**

```bash
git add ym_stock_data/config.py ym_stock_data/sources/eastmoney_http.py ym_stock_data/sources/eastmoney.py ym_stock_data/sources/research.py tests/test_eastmoney_http.py tests/test_news_research_contracts.py
git commit -m "feat: govern eastmoney http requests"
```

---

## Task 3: 修正北向字段语义，不删除旧字段

**Files:**
- Create: `tests/test_northbound_semantics.py`
- Modify: `ym_stock_data/sources/northbound.py`
- Modify: `ym_stock_data/v2/policies/fields.json`

- [ ] **Step 1: 写兼容性测试**

```python
import unittest
from unittest.mock import Mock, patch

from ym_stock_data.sources import northbound


class NorthboundSemanticsTests(unittest.TestCase):
    @patch("ym_stock_data.sources.northbound.requests.get")
    def test_sgt_is_retained_but_marked_reference_only(self, get):
        response = Mock()
        response.json.return_value = {
            "time": ["09:31"], "hgt": [1.2], "sgt": [2.3]
        }
        response.raise_for_status.return_value = None
        get.return_value = response

        result = northbound.fetch_realtime()

        self.assertEqual(2.3, result["sgt_current_yi"])
        self.assertEqual("reference_only", result["sgt_reliability"])
        self.assertEqual("intraday_reference", result["data_scope"])
        self.assertEqual("hkex_daily", result["authoritative_source"])
```

- [ ] **Step 2: 运行确认失败**

```bash
uv run python -m unittest tests.test_northbound_semantics -v
```

Expected: 新的可靠性字段不存在。

- [ ] **Step 3: 仅追加语义字段**

`fetch_realtime()` 返回值追加：

```python
"hgt_reliability": "intraday_reference",
"sgt_reliability": "reference_only",
"authoritative_source": "hkex_daily",
"data_scope": "intraday_reference",
"trade_usage": "辅助，不单独触发交易",
```

本任务不删除、不置空 `sgt_current_yi`，避免已有消费者 KeyError。

- [ ] **Step 4: 字段策略只增不改**

在 `fields.json` 追加 `northbound_intraday` 的 scope 说明；不把它接入现有五个 intent。

- [ ] **Step 5: 运行测试并提交**

```bash
uv run python -m unittest tests.test_northbound_semantics -v
uv run python -m unittest discover -s tests -v
git add ym_stock_data/sources/northbound.py ym_stock_data/v2/policies/fields.json tests/test_northbound_semantics.py
git commit -m "fix: clarify northbound intraday scope"
```

---

## Task 4: 新增涨跌停情绪旁路 source 和 V1 route

**Files:**
- Create: `ym_stock_data/sources/limit_state.py`
- Create: `tests/test_limit_state.py`
- Modify: `ym_stock_data/fetch.py`
- Modify: `THIRD_PARTY_NOTICES.md`

- [ ] **Step 1: 用 fixture 写四池解析与情绪计算测试**

```python
import unittest
from unittest.mock import patch

from ym_stock_data.sources import limit_state


class LimitStateTests(unittest.TestCase):
    @patch("ym_stock_data.sources.limit_state.CLIENT.get")
    def test_fetch_limit_state_calculates_break_rate_and_height(self, get):
        payloads = [
            {"data": {"pool": [{"c": "600001", "n": "甲", "lbc": 3}]}},
            {"data": {"pool": [{"c": "600002", "n": "乙"}]}},
            {"data": {"pool": [{"c": "600003", "n": "丙"}]}},
            {"data": {"pool": []}},
        ]
        get.side_effect = [type("R", (), {"json": lambda self, p=p: p})() for p in payloads]

        result = limit_state.fetch_limit_state("20260714")

        self.assertEqual(1, result["zt_count"])
        self.assertEqual(1, result["zb_count"])
        self.assertEqual(50.0, result["break_rate"])
        self.assertEqual(3, result["max_board"])
        self.assertEqual("eastmoney_limit_pool", result["source"])
```

- [ ] **Step 2: 实现 source，错误必须显式返回**

四个端点限定在本模块常量中；每个请求走 `eastmoney_http.CLIENT`：

```python
POOL_ENDPOINTS = {
    "zt": ("getTopicZTPool", "fbt:asc"),
    "zb": ("getTopicZBPool", "fbt:asc"),
    "dt": ("getTopicDTPool", "fund:asc"),
    "yzt": ("getYesterdayZTPool", "zs:desc"),
}
POOL_TOKEN = "7eea3edcaed734bea9cbfc24409ed989"


def _fetch_pool(kind: str, date: str) -> list[dict]:
    endpoint, sort = POOL_ENDPOINTS[kind]
    response = CLIENT.get(
        f"https://push2ex.eastmoney.com/{endpoint}",
        params={
            "ut": POOL_TOKEN,
            "dpt": "wz.ztzt",
            "Pageindex": 0,
            "pagesize": 10000,
            "sort": sort,
            "date": date,
        },
        headers={"Referer": "https://quote.eastmoney.com/"},
        timeout=10,
    )
    if getattr(response, "skipped_by_breaker", False):
        raise RuntimeError(response.reason)
    payload = response.json()
    return (payload.get("data") or {}).get("pool") or []
```

各池 normalizer 只保留稳定字段：`c→code`、`n→name`、`p/1000→price`、`zdp→pct`、`lbc→limit_days`、`fund→seal_fund`、`zbc→break_times`、`hybk→industry`。实现完整聚合入口：

```python
from datetime import datetime


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_pool(rows: list[dict]) -> list[dict]:
    return [{
        "code": str(row.get("c") or ""),
        "name": str(row.get("n") or ""),
        "price": _number(row.get("p")) / 1000,
        "pct": _number(row.get("zdp")),
        "limit_days": int(_number(row.get("lbc"), 1)),
        "seal_fund": _number(row.get("fund")),
        "break_times": int(_number(row.get("zbc"))),
        "industry": str(row.get("hybk") or ""),
    } for row in rows]


def fetch_limit_state(date: str | None = None) -> dict:
    query_date = date or datetime.now().strftime("%Y%m%d")
    try:
        pools = {
            kind: _normalize_pool(_fetch_pool(kind, query_date))
            for kind in POOL_ENDPOINTS
        }
    except Exception as exc:
        return {
            "date": query_date,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "source": "eastmoney_limit_pool",
        }
    zt, zb = pools["zt"], pools["zb"]
    denominator = len(zt) + len(zb)
    return {
        "date": query_date,
        "zt_count": len(zt),
        "zb_count": len(zb),
        "dt_count": len(pools["dt"]),
        "yzt_count": len(pools["yzt"]),
        "break_rate": round(len(zb) / denominator * 100, 2) if denominator else 0.0,
        "max_board": max(
            (int(row.get("limit_days") or 1) for row in zt),
            default=0,
        ),
        "pools": pools,
        "source": "eastmoney_limit_pool",
    }
```

任一主请求异常时返回 `error`、`error_type`、`source`，不得用空池伪装正常交易日。

- [ ] **Step 3: 仅追加 V1 route**

在 `_ROUTES` 追加：

```python
"limit_state": (
    "limit_state",
    "fetch_limit_state",
    {"layer": 3, "desc": "涨停/炸板/跌停/昨涨停与连板情绪"},
),
```

本任务不修改 `review_sentiment`，避免问财路径的结果突然换口径。

- [ ] **Step 4: 记录第三方来源**

`THIRD_PARTY_NOTICES.md` 写明：端点发现参考 `simonlin1212/a-stock-data` commit `9ed665cc9773457bc23fed6b770b2b5a8cede40f`，Apache-2.0；实现已按 YM-data-pipeline 契约重写。

- [ ] **Step 5: 验证并提交**

```bash
uv run python -m unittest tests.test_limit_state -v
uv run python -m unittest discover -s tests -v
uv run python - <<'PY'
from ym_stock_data import fetch
r = fetch("limit_state")
print(r.get("zt_count"), r.get("zb_count"), r.get("_meta"))
assert "_meta" in r
PY
git add ym_stock_data/sources/limit_state.py ym_stock_data/fetch.py tests/test_limit_state.py THIRD_PARTY_NOTICES.md
git commit -m "feat: add limit-state sidecar source"
```

---

## Task 5: 新增低频股票事件旁路，不一次性接入 V2

**Files:**
- Create: `ym_stock_data/sources/stock_events.py`
- Create: `tests/test_stock_events.py`
- Modify: `ym_stock_data/fetch.py`

- [ ] **Step 1: 为统一函数和字段白名单写失败测试**

```python
import unittest
from unittest.mock import patch

from ym_stock_data.sources import stock_events


class StockEventsTests(unittest.TestCase):
    @patch("ym_stock_data.sources.stock_events.eastmoney_datacenter")
    def test_fetch_lockup_maps_only_stable_fields(self, query):
        query.return_value = [{
            "SECURITY_CODE": "600519",
            "FREE_DATE": "2026-08-01",
            "FREE_SHARES_TYPE": "首发原股东限售股份",
            "FREE_SHARES": 100,
            "ABLE_FREE_SHARES": 80,
        }]

        result = stock_events.fetch_stock_event("lockup", "600519")

        self.assertEqual("2026-08-01", result["items"][0]["date"])
        self.assertEqual(80, result["items"][0]["able_shares"])
        self.assertEqual("eastmoney_datacenter", result["source"])

    def test_unknown_event_is_rejected(self):
        with self.assertRaises(ValueError):
            stock_events.fetch_stock_event("unknown", "600519")
```

- [ ] **Step 2: 实现共用 datacenter helper 和五种事件配置**

允许的 `event` 固定为：

```python
from .eastmoney_http import query_datacenter as eastmoney_datacenter


EVENTS = {
    "lockup": {
        "report": "RPT_LIFT_STAGE", "code_field": "SECURITY_CODE",
        "sort": "FREE_DATE",
    },
    "margin": {
        "report": "RPTA_WEB_RZRQ_GGMX", "code_field": "SCODE",
        "sort": "DATE",
    },
    "block_trade": {
        "report": "RPT_DATA_BLOCKTRADE", "code_field": "SECURITY_CODE",
        "sort": "TRADE_DATE",
    },
    "holder_num": {
        "report": "RPT_HOLDERNUMLATEST", "code_field": "SECURITY_CODE",
        "sort": "END_DATE",
    },
    "dividend": {
        "report": "RPT_SHAREBONUS_DET", "code_field": "SECURITY_CODE",
        "sort": "EX_DIVIDEND_DATE",
    },
}
```

统一入口：

```python
def fetch_stock_event(event: str, code: str, page_size: int = 30) -> dict:
    if event not in EVENTS:
        raise ValueError(f"不支持的股票事件: {event}")
    config = EVENTS[event]
    rows = eastmoney_datacenter(
        report_name=config["report"],
        filter_str=f'({config["code_field"]}="{code}")',
        page_size=page_size,
        sort_columns=config["sort"],
        sort_types="-1",
    )
    return {
        "event": event,
        "code": code,
        "total": len(rows),
        "items": NORMALIZERS[event](rows),
        "source": "eastmoney_datacenter",
    }
```

每种事件的 normalizer 必须写显式字段映射；不得返回 `columns="ALL"` 的原始整行给消费者。

字段映射固定如下：

```python
NORMALIZERS = {
    "lockup": lambda rows: [{
        "date": str(row.get("FREE_DATE") or "")[:10],
        "type": row.get("FREE_SHARES_TYPE") or "",
        "shares": row.get("FREE_SHARES") or 0,
        "able_shares": row.get("ABLE_FREE_SHARES") or 0,
    } for row in rows],
    "margin": lambda rows: [{
        "date": str(row.get("DATE") or "")[:10],
        "rzye": row.get("RZYE") or 0,
        "rzmre": row.get("RZMRE") or 0,
        "rqye": row.get("RQYE") or 0,
        "rzrqye": row.get("RZRQYE") or 0,
    } for row in rows],
    "block_trade": lambda rows: [{
        "date": str(row.get("TRADE_DATE") or "")[:10],
        "price": row.get("DEAL_PRICE") or 0,
        "close": row.get("CLOSE_PRICE") or 0,
        "volume": row.get("DEAL_VOLUME") or 0,
        "amount": row.get("DEAL_AMT") or 0,
        "buyer": row.get("BUYER_NAME") or "",
        "seller": row.get("SELLER_NAME") or "",
    } for row in rows],
    "holder_num": lambda rows: [{
        "date": str(row.get("END_DATE") or "")[:10],
        "holder_num": row.get("HOLDER_NUM") or 0,
        "change_num": row.get("HOLDER_NUM_CHANGE") or 0,
        "change_ratio": row.get("HOLDER_NUM_RATIO") or 0,
        "avg_shares": row.get("AVG_FREE_SHARES") or 0,
    } for row in rows],
    "dividend": lambda rows: [{
        "date": str(row.get("EX_DIVIDEND_DATE") or "")[:10],
        "bonus_rmb": row.get("PRETAX_BONUS_RMB") or 0,
        "transfer_ratio": row.get("TRANSFER_RATIO") or 0,
        "bonus_ratio": row.get("BONUS_RATIO") or 0,
        "plan": row.get("ASSIGN_PROGRESS") or "",
    } for row in rows],
}
```

- [ ] **Step 3: 只新增一个通用 V1 route**

```python
"stock_event": (
    "stock_events",
    "fetch_stock_event",
    {"layer": 4, "desc": "解禁/两融/大宗/股东户数/分红低频事实"},
),
```

调用示例固定为 `fetch("stock_event", event="lockup", code="600519")`，不为五种事件制造五套 route。

- [ ] **Step 4: 验证并提交**

```bash
uv run python -m unittest tests.test_stock_events -v
uv run python -m unittest discover -s tests -v
git add ym_stock_data/sources/stock_events.py ym_stock_data/fetch.py tests/test_stock_events.py
git commit -m "feat: add low-frequency stock events"
```

---

## Task 6: 补问财内容搜索，保持结构化问财原路径不变

**Files:**
- Create: `ym_stock_data/sources/iwencai_content.py`
- Create: `tests/test_iwencai_content.py`
- Modify: `ym_stock_data/fetch.py`

- [ ] **Step 1: 写鉴权头、channel 白名单和错误测试**

```python
import unittest
from unittest.mock import Mock, patch

from ym_stock_data.sources import iwencai_content


class IwencaiContentTests(unittest.TestCase):
    @patch.dict("os.environ", {"IWENCAI_API_KEY": "token"})
    @patch("ym_stock_data.sources.iwencai_content.requests.post")
    def test_search_content_uses_report_search_contract(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"status_code": 0, "data": [{"uid": "1"}]}
        post.return_value = response

        result = iwencai_content.search_content("机器人", channel="report", limit=10)

        headers = post.call_args.kwargs["headers"]
        payload = post.call_args.kwargs["json"]
        self.assertEqual("Bearer token", headers["Authorization"])
        self.assertEqual("report-search", headers["X-Claw-Skill-Id"])
        self.assertEqual(["report"], payload["channels"])
        self.assertEqual("iwencai_content", result["source"])

    def test_search_content_rejects_unknown_channel(self):
        with self.assertRaises(ValueError):
            iwencai_content.search_content("机器人", channel="social")
```

- [ ] **Step 2: 实现独立内容搜索模块**

```python
import os
import secrets
import requests


IWENCAI_BASE = os.environ.get("IWENCAI_BASE_URL", "https://openapi.iwencai.com")
CHANNELS = {"report", "announcement", "news"}


def _headers(key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Claw-Call-Type": "normal",
        "X-Claw-Skill-Id": "report-search",
        "X-Claw-Skill-Version": "2.0.0",
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": secrets.token_hex(32),
    }


def _deduplicate(items: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for item in items:
        uid = str(item.get("uid") or f'{item.get("title", "")}|{item.get("publish_date", "")}')
        score = float(item.get("score") or 0)
        if uid not in best or score > float(best[uid].get("score") or 0):
            best[uid] = item
    return sorted(
        best.values(),
        key=lambda item: str(item.get("publish_date") or ""),
        reverse=True,
    )


def search_content(query: str, channel: str = "report", limit: int = 20) -> dict:
    if channel not in CHANNELS:
        raise ValueError(f"不支持的内容频道: {channel}")
    key = os.environ.get("IWENCAI_API_KEY", "")
    if not key:
        return {
            "error": "IWENCAI_API_KEY 未配置",
            "error_type": "auth_missing",
            "items": [],
            "source": "iwencai_content",
        }
    response = requests.post(
        f"{IWENCAI_BASE}/v1/comprehensive/search",
        json={
            "channels": [channel],
            "app_id": "AIME_SKILL",
            "query": query,
            "size": limit,
        },
        headers=_headers(key),
        timeout=30,
    )
    if response.status_code != 200:
        return {
            "error": f"HTTP {response.status_code}",
            "error_type": "http_error",
            "items": [],
            "source": "iwencai_content",
        }
    payload = response.json()
    if payload.get("status_code", 0) != 0:
        return {
            "error": payload.get("status_msg") or "iwencai content error",
            "error_type": "provider_error",
            "items": [],
            "source": "iwencai_content",
        }
    items = payload.get("data") or []
    deduplicated = _deduplicate(items)
    return {
        "query": query,
        "channel": channel,
        "total": len(deduplicated),
        "items": deduplicated,
        "source": "iwencai_content",
    }
```

本模块不得调用或修改 `iwencai.query()` 的 breaker 状态，避免内容搜索失败拖垮结构化选股。

- [ ] **Step 3: 追加 V1 route**

```python
"iwencai_content": (
    "iwencai_content",
    "search_content",
    {"layer": 4, "desc": "问财研报/公告/新闻自然语言内容搜索"},
),
```

- [ ] **Step 4: 验证并提交**

```bash
uv run python -m unittest tests.test_iwencai_content -v
uv run python -m unittest discover -s tests -v
git add ym_stock_data/sources/iwencai_content.py ym_stock_data/fetch.py tests/test_iwencai_content.py
git commit -m "feat: add isolated iwencai content search"
```

---

## Task 7: 固化 TDX MCP 为可观测的人工备用源

**Files:**
- Create: `docs/TDX-MCP-备用源验证清单.md`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: 写清楚连接状态机**

文档必须包含下面的唯一状态表：

| 状态 | 判据 | Agent 行为 |
| --- | --- | --- |
| `ready` | `initialize`、`tools/list` 成功 | 可人工调用并标注 `source=tdx_mcp` |
| `auth_missing` | 找不到 WorkBuddy TDX credentials | 请弈沐在 WorkBuddy 重新登录 |
| `token_expired` | refresh 失败或 401 | 停止调用，不基于旧缓存回答 |
| `session_invalid` | `No valid session ID provided` | 重启 stdio wrapper，重新 initialize |
| `tool_unavailable` | tools/list 无目标工具 | 降级回本地管道或问财，不猜结果 |

- [ ] **Step 2: 固化 20 个对账样例**

清单分成五组，每组四例：

- `tdx_quotes`：沪主板、深主板、创业板、科创板。
- `tdx_kline`：日线、周线、15 分钟、60 分钟。
- `tdx_screener`：主题、估值、涨停、非 ST 组合条件。
- `wenda_report_query` / `wenda_notice_query`：各两例。
- `wenda_news_query` / `tdx_lookup_stock`：各两例。

每例记录：查询时间、参数、本地基准源、TDX MCP 字段、差异解释、是否满足交叉验证用途。

- [ ] **Step 3: 明确禁止自动接入**

在 README 与 AGENTS 同步以下规则：

```text
TDX MCP 仍是 Agent 人工备用源；不由 fetch()/resolve() 自动调用，不读取其旧结果伪装本地 source，不因工具可用就提升为交易事实源。只有完成 20 例 + 连续 5 个交易日对账后，才讨论在 fields.json 中把个别字段从 cross_check_only 提升为 fallback_candidate。
```

- [ ] **Step 4: 文档静态验证并提交**

```bash
rg -n "ready|auth_missing|token_expired|session_invalid|tool_unavailable" docs/TDX-MCP-备用源验证清单.md
rg -n "不由 fetch\(\)/resolve\(\) 自动调用|cross_check_only" README.md AGENTS.md
git diff --check
git add docs/TDX-MCP-备用源验证清单.md README.md AGENTS.md
git commit -m "docs: formalize tdx mcp fallback gates"
```

---

## Task 8: 旁路晋升两个 V2 intent，不改现有 intent

**Files:**
- Modify: `ym_stock_data/v2/adapters.py`
- Modify: `ym_stock_data/v2/resolve.py`
- Modify: `ym_stock_data/v2/policies/fields.json`
- Modify: `tests/test_v2_mvp.py`

- [ ] **Step 1: 为两个新 intent 写失败测试**

```python
@patch("ym_stock_data.v2.adapters.fetch_limit_state")
def test_market_limit_state_is_additive(self, fetch_limit_state):
    fetch_limit_state.return_value = {
        "zt_count": 30,
        "zb_count": 10,
        "break_rate": 25.0,
        "max_board": 4,
        "source": "eastmoney_limit_pool",
    }
    result = resolve("market_limit_state", date="20260714", _now=ts("2026-07-14T15:10:00+08:00"))
    self.assertEqual(30, result["data"]["zt_count"])
    self.assertEqual("market_limit_state", result["_meta"]["intent"])
    self.assertEqual(["eastmoney_limit_pool"], result["_meta"]["source_chain"])


@patch("ym_stock_data.v2.adapters.fetch_stock_event")
def test_stock_event_is_additive(self, fetch_stock_event):
    fetch_stock_event.return_value = {
        "event": "lockup", "code": "600519", "total": 1,
        "items": [{"date": "2026-08-01"}], "source": "eastmoney_datacenter",
    }
    result = resolve("stock_event", event="lockup", code="600519", _now=ts("2026-07-14T15:10:00+08:00"))
    self.assertEqual(1, result["data"]["total"])
    self.assertEqual("stock_event", result["_meta"]["intent"])
```

- [ ] **Step 2: adapters 只直连 source，不经过 V1 fetch**

```python
from ym_stock_data.sources.limit_state import fetch_limit_state as _fetch_limit_state
from ym_stock_data.sources.stock_events import fetch_stock_event as _fetch_stock_event


def fetch_limit_state(date: str | None = None) -> dict:
    return _with_meta(
        _fetch_limit_state(date=date),
        data_type="limit_state",
        source="eastmoney_limit_pool",
    )


def fetch_stock_event(event: str, code: str, page_size: int = 30) -> dict:
    return _with_meta(
        _fetch_stock_event(event=event, code=code, page_size=page_size),
        data_type="stock_event",
        source="eastmoney_datacenter",
    )
```

- [ ] **Step 3: resolve 只追加分支**

`SUPPORTED_INTENTS` 追加 `market_limit_state`、`stock_event`。两个分支必须走 `assess_quality()`、`normalize_result()`，并分别设置：

```python
data_scope="东财涨跌停池口径"
trade_usage="复盘辅助，不单独触发交易"
```

```python
data_scope="东财低频事件口径"
trade_usage="风险与基本面辅助，不单独触发交易"
```

不得修改 `review_sentiment` 的 primary/fallback。

- [ ] **Step 4: fields policy 追加字段**

`market_limit_state` 至少登记：`zt_count`、`zb_count`、`dt_count`、`break_rate`、`max_board`；`stock_event` 登记 `event`、`code`、`items`。限流类设为 `limited`，盘后情绪新鲜度设 1800 秒，低频事件设 86400 秒。

- [ ] **Step 5: 验证旧五个 intent 未回归**

```bash
uv run python -m unittest tests.test_v2_mvp tests.test_v2_quality -v
uv run python -m unittest discover -s tests -v
uv run python - <<'PY'
from ym_stock_data.v2.resolve import resolve

for intent, kwargs in [
    ("realtime_market", {}),
    ("sector_index", {"names": ["半导体"]}),
    ("stock_snapshot", {"codes": ["600519"]}),
    ("stock_kline", {"code": "600519", "period": "daily", "count": 5}),
]:
    result = resolve(intent, **kwargs)
    assert result["_meta"]["intent"] == intent
    assert "data" in result and "_meta" in result
PY
```

- [ ] **Step 6: 提交 V2 旁路能力**

```bash
git add ym_stock_data/v2/adapters.py ym_stock_data/v2/resolve.py ym_stock_data/v2/policies/fields.json tests/test_v2_mvp.py
git commit -m "feat: add sidecar market and event intents"
```

---

## Task 9: 五个交易日双轨对账与是否晋升决策

**Files:**
- Create: `scripts/compare_external_sources.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: 实现本地对账脚本，数据不进仓库**

脚本固定写到 `~/.ym-stock-data/compare/YYYY-MM-DD.json`：

```python
#!/usr/bin/env python3
import json
from datetime import datetime
from pathlib import Path

from ym_stock_data.v2.resolve import resolve


def main() -> int:
    now = datetime.now().astimezone()
    snapshot = {
        "queried_at": now.isoformat(),
        "local": {
            "review_sentiment": resolve(
                "review_sentiment",
                query="昨日涨停 今日涨跌幅 非st",
                limit=50,
            ),
            "market_limit_state": resolve("market_limit_state"),
        },
        "manual_tdx_mcp": {
            "status": "not_called",
            "note": "TDX MCP 由 Agent 按验证清单人工填写，不在脚本内自动调用",
        },
    }
    output_dir = Path.home() / ".ym-stock-data" / "compare"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{now.date().isoformat()}.json"
    output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

脚本只使用标准库 `json/pathlib/datetime` 和现有 `resolve()`。

- [ ] **Step 2: 连续五个交易日运行**

```bash
uv run python scripts/compare_external_sources.py
```

每日检查：涨停数、炸板数、炸板率、最高板、查询成功率、`source_chain`、`confidence`、字段缺失。

- [ ] **Step 3: 使用明确晋升标准**

`market_limit_state` 只有同时满足以下条件，才允许单独开后续任务讨论成为 `review_sentiment` 的自动 fallback：

- 五个交易日均返回，成功率 100%。
- 与问财同义字段差异可解释，涨停/跌停家数绝对差不超过 2。
- 炸板率的分母口径写清，差异不被静默平均。
- 任何降级结果都有 `source_chain`、`confidence` 和 `quality.reason_codes`。
- 没有增加 live-dashboard 或复盘脚本的请求次数。

TDX MCP 只有满足文档中的 20 例和五日对账，才能从 `cross_check_only` 讨论提升为 `fallback_candidate`；本计划不执行提升。

- [ ] **Step 4: 更新入口文档**

README/AGENTS 增加两个新 V2 示例，但明确仍为旁路：

```python
resolve("market_limit_state")
resolve("stock_event", event="lockup", code="600519")
```

- [ ] **Step 5: 最终验证**

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q ym_stock_data scripts
git diff --check
git status --short
```

Expected: 测试 0 failures、编译 exit 0、diff check exit 0；工作区只包含本计划明确列出的文件。

- [ ] **Step 6: 提交对账与文档**

```bash
git add scripts/compare_external_sources.py README.md AGENTS.md
git commit -m "docs: add external source graduation gates"
```

---

## 2. 推荐执行批次与停机点

### Batch A：立即修复，低风险

- Task 1：财联社 + 研报。
- Task 2：东财请求治理。
- Task 3：北向语义。

完成后可以停。此时只有旧能力修复和元数据增强，没有新增 V2 消费路径。

### Batch B：新增旁路能力，中风险

- Task 4：涨跌停情绪。
- Task 5：低频股票事件。
- Task 6：问财内容搜索。
- Task 7：TDX MCP 人工备用门槛。

完成后也可以停。新能力可由 Agent 显式调用，但不会影响现有五个 V2 intent。

### Batch C：V2 晋升与观察，中高风险

- Task 8：新增两个 V2 intent。
- Task 9：五个交易日双轨观察。

只有五日观察通过，才另开计划讨论消费者迁移或 `review_sentiment` 自动 fallback；不在本计划内直接推进生产切换。

---

## 3. 回滚策略

- Task 1-3 每项独立提交；出现问题只 revert 对应提交，不触碰 V2。
- Task 4-7 都是新增 route/module；回滚时删除新 route 即可，旧 route 不受影响。
- Task 8 新 intent 是 append-only；删除两个分支、policy 和测试即可恢复原五 intent。
- TDX MCP wrapper 位于 `/Users/yimu/.codex/mcp/tdx-finance-mcp.py`，本计划只读，不修改 OAuth 缓存、不复制 token。
- 双轨快照位于用户缓存目录，不进入 Git，不被生产消费者读取。

---

## 4. 自审结果

- 需求覆盖：已同时纳入问财、TDX MCP、`a-stock-data`，并保持当前 V1/V2/消费者边界。
- 无框架替换：没有引入第三套路由、没有引入 mootdx、没有自动调用 MCP。
- 可分步：三个 Batch 均有独立停机点；每个 Task 有测试、live smoke 或静态验证。
- 可回滚：修复、source、V1 route、V2 intent 分开提交。
- 安全：未关闭 TLS；未把第三方空列表当正常；未写真实交易授权逻辑。
- 待执行时的第一条命令：`git status --short`，确认弈沐或其他 Agent 没有新增未说明改动。
