"""问财研报、公告和新闻内容搜索旁路。"""

from __future__ import annotations

import os
import secrets

import requests


IWENCAI_BASE = os.environ.get(
    "IWENCAI_BASE_URL", "https://openapi.iwencai.com"
)
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
        if not isinstance(item, dict):
            continue
        uid = str(
            item.get("uid")
            or f'{item.get("title", "")}|{item.get("publish_date", "")}'
        )
        score = float(item.get("score") or 0)
        if uid not in best or score > float(best[uid].get("score") or 0):
            best[uid] = item
    return sorted(
        best.values(),
        key=lambda item: str(item.get("publish_date") or ""),
        reverse=True,
    )


def search_content(
    query: str,
    channel: str = "report",
    limit: int = 20,
) -> dict:
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
    try:
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
    except Exception as exc:
        return {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "items": [],
            "source": "iwencai_content",
        }
    if payload.get("status_code", 0) != 0:
        return {
            "error": payload.get("status_msg") or "iwencai content error",
            "error_type": "provider_error",
            "items": [],
            "source": "iwencai_content",
        }
    deduplicated = _deduplicate(payload.get("data") or [])
    return {
        "query": query,
        "channel": channel,
        "total": len(deduplicated),
        "items": deduplicated,
        "source": "iwencai_content",
    }
