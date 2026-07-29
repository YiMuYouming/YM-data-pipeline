"""Canonical aggregate helpers for unified query results."""

from __future__ import annotations

from typing import Any


def aggregate_review_sentiment(query_rows: list[dict]) -> dict:
    """Build top-level sentiment aggregates from iwencai query results."""
    limit_up_returns = []
    failed_limit_rate = None
    highest_board = None

    for row in query_rows:
        query = row.get("query", "")
        records = _extract_records(row.get("result", {}))

        if "昨日涨停" in query and "今日涨跌幅" in query:
            limit_up_returns.extend(_first_number(record, ("今日涨跌幅", "涨跌幅")) for record in records)

        if "炸板率" in query:
            for record in records:
                value = _first_number(record, ("炸板率",))
                if value is not None:
                    failed_limit_rate = value
                    break

        if "连板数" in query:
            board_values = [_first_number(record, ("连板数", "连续涨停天数")) for record in records]
            board_values = [value for value in board_values if value is not None]
            if board_values:
                highest_board = int(max(board_values))

    limit_up_returns = [value for value in limit_up_returns if value is not None]
    aggregates = {}
    if limit_up_returns:
        avg_return = round(sum(limit_up_returns) / len(limit_up_returns), 2)
        red_rate = round(sum(1 for value in limit_up_returns if value > 0) / len(limit_up_returns) * 100, 2)
        aggregates.update({
            "涨停收益均值": avg_return,
            "红盘率": red_rate,
            "limit_up_return_avg": avg_return,
            "red_rate": red_rate,
        })
    if failed_limit_rate is not None:
        aggregates.update({
            "炸板率": failed_limit_rate,
            "failed_limit_rate": failed_limit_rate,
        })
    if highest_board is not None:
        aggregates.update({
            "最高板": highest_board,
            "highest_board": highest_board,
        })

    return aggregates


def _extract_records(result: Any) -> list[dict]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    for key in ("datas", "data", "rows", "items"):
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _first_number(record: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in record:
            return _parse_number(record[key])
    for key, value in record.items():
        if any(target in str(key) for target in keys):
            return _parse_number(value)
    return None


def _parse_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.startswith("+"):
        text = text[1:]
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None
