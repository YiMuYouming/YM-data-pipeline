"""Single implementation of the trading indicators in the Vault glossary.

Definitions live in 《交易指标术语表》; ``v3/indicators.v1.json`` registers each
indicator's section, formula, inputs, universe and definition status.  Intraday
(StockToday snapshot) and post-close (market_facts) callers use these same
functions, so a formula change happens here and nowhere else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

REGISTRY_PATH = Path(__file__).resolve().parent / "v3" / "indicators.v1.json"
REGISTRY = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
INDICATOR_VERSION = REGISTRY["version"]

# Prices are quoted to 0.01 CNY; anything within half a tick is the same price.
_PRICE_TOLERANCE = 0.005


def _bands(key: str) -> list[dict]:
    for item in REGISTRY["indicators"]:
        if item["key"] == key:
            return item["bands"]
    raise KeyError(key)


def _band(value: float | None, key: str) -> str | None:
    if value is None:
        return None
    for band in _bands(key):
        if "below" in band and value < band["below"]:
            return band["label"]
        if "through" in band and value <= band["through"]:
            return band["label"]
        if "above" in band and value > band["above"]:
            return band["label"]
    return None


def needs_vault_detail() -> list[dict]:
    """Indicators whose glossary definition is incomplete (report to the owner)."""

    return [
        {"key": item["key"], "name": item["name"],
         "glossary_section": item["glossary_section"],
         "detail_needed": item.get("detail_needed")}
        for item in REGISTRY["indicators"]
        if item["definition_status"] == "needs_vault_detail"
    ]


def is_st(name: str | None) -> bool:
    return "ST" in str(name or "").upper()


def breadth(changes: Iterable[float]) -> dict:
    """§3.1 up / down / flat counts over one universe of daily percent changes."""

    up = down = flat = 0
    for change in changes:
        if change > 0:
            up += 1
        elif change < 0:
            down += 1
        else:
            flat += 1
    return {"up": up, "down": down, "flat": flat, "total": up + down + flat,
            "up_down_ratio": round(up / down, 6) if down else None}


def emotion(up: int, down: int) -> float | None:
    """§3.1 情绪值 = 上涨 ÷ (上涨 + 下跌) × 100."""

    total = up + down
    return round(up / total * 100, 6) if total else None


def legacy_emotion_all_rows(up: int, rows: int) -> float | None:
    """Pre-2026-09-27 value (flat stocks in the denominator). Remove after 2026-10-04."""

    return round(up / rows * 100, 6) if rows else None


def emotion_band(score: float | None) -> str | None:
    return _band(score, "emotion")


def limit_state(price, high, up_limit, down_limit) -> str | None:
    """Intraday limit state from exchange limit prices: up / down / broken."""

    if price is None:
        return None
    if up_limit and abs(price - up_limit) <= _PRICE_TOLERANCE:
        return "up"
    if down_limit and abs(price - down_limit) <= _PRICE_TOLERANCE:
        return "down"
    if up_limit and high is not None and high >= up_limit - _PRICE_TOLERANCE:
        return "broken"
    return None


def broken_rate(broken: int, sealed: int) -> float | None:
    """§1.4 炸板率 = 曾涨停未封死 ÷ 曾触及涨停 × 100."""

    touched = broken + sealed
    return round(broken / touched * 100, 6) if touched else None


def seal_rate(broken: int, sealed: int) -> float | None:
    rate = broken_rate(broken, sealed)
    return round(100 - rate, 6) if rate is not None else None


def broken_rate_band(rate: float | None) -> str | None:
    return _band(rate, "broken_rate")


def promotion(previous_boards: Mapping[str, int], current_boards: Mapping[str, int]) -> dict:
    """§1.1 tiered promotion: yesterday N-board sealing N+1 today."""

    promoted = {
        code for code, board in previous_boards.items()
        if current_boards.get(code) == board + 1
    }

    def rate(previous_board: int | None) -> dict:
        universe = sorted(
            code for code, board in previous_boards.items()
            if previous_board is None or board == previous_board
        )
        winners = [code for code in universe if code in promoted]
        return {
            "numerator": len(winners),
            "denominator": len(universe),
            "pct": round(len(winners) / len(universe) * 100, 6) if universe else None,
            "promoted_codes": winners,
            "denominator_codes": universe,
        }

    return {
        "one_to_two": rate(1),
        "two_to_three": rate(2),
        "three_to_four": rate(3),
        "overall": rate(None),
    }


def board_heights(current_boards: Mapping[str, int]) -> dict:
    """§1.3 最高板 and the next lower board level present."""

    levels = sorted(set(current_boards.values()), reverse=True)
    return {"highest": levels[0] if levels else None,
            "second_highest": levels[1] if len(levels) > 1 else None}


def cohort_return(codes: Iterable[str], changes: Mapping[str, float]) -> float | None:
    """§1.5 mean change of a fixed cohort; incomplete cohorts give no value."""

    codes = list(codes)
    if not codes or any(code not in changes for code in codes):
        return None
    return round(sum(changes[code] for code in codes) / len(codes), 6)


def money_effect(limit_up_return: float | None) -> str | None:
    """§3.2 — registered as needs_vault_detail; legacy single-threshold rule."""

    if limit_up_return is None:
        return None
    if limit_up_return > 2:
        return "好"
    if limit_up_return < 0:
        return "差"
    return "一般"


def board_risk(overall_promotion_pct: float | None) -> float | None:
    """§3.3 — registered as needs_vault_detail; legacy promotion proxy."""

    if overall_promotion_pct is None:
        return None
    return round(max(0.0, min(1.0, 1.0 - overall_promotion_pct / 100 * 1.8)), 2)


def summarize(
    *,
    changes: Mapping[str, float],
    limit_sets: Mapping[str, Iterable[str]],
    current_boards: Mapping[str, int],
    previous_boards: Mapping[str, int] | None,
    previous_broken: Iterable[str] | None,
) -> dict:
    """All registered indicators from one consistent set of inputs.

    ``changes`` covers the traded universe (emotion/breadth); ``limit_sets``,
    boards and cohorts are already restricted to non-ST stocks.
    """

    counts = breadth(changes.values())
    score = emotion(counts["up"], counts["down"])
    up_codes = sorted(limit_sets.get("up") or [])
    down_codes = sorted(limit_sets.get("down") or [])
    broken_codes = sorted(limit_sets.get("broken") or [])
    rate = broken_rate(len(broken_codes), len(up_codes))
    result = {
        "indicator_version": INDICATOR_VERSION,
        "emotion": score,
        "emotion_band": emotion_band(score),
        "up_count": counts["up"],
        "down_count": counts["down"],
        "flat_count": counts["flat"],
        "traded_count": counts["total"],
        "up_down_ratio": counts["up_down_ratio"],
        "limit_up_count": len(up_codes),
        "limit_down_count": len(down_codes),
        "broken_count": len(broken_codes),
        "broken_rate": rate,
        "broken_rate_band": broken_rate_band(rate),
        "seal_rate": seal_rate(len(broken_codes), len(up_codes)),
        **{f"board_{key}": value for key, value in board_heights(current_boards).items()},
        "consecutive_count": sum(board >= 2 for board in current_boards.values()),
        "promotion": None,
        "limit_up_return": None,
        "consecutive_return": None,
        "broken_return": None,
        "money_effect": None,
        "board_risk": None,
    }
    if previous_boards:
        tiers = promotion(previous_boards, current_boards)
        result["promotion"] = tiers
        result["limit_up_return"] = cohort_return(previous_boards, changes)
        result["consecutive_return"] = cohort_return(
            [code for code, board in previous_boards.items() if board >= 2], changes
        )
        result["board_risk"] = board_risk(tiers["overall"]["pct"])
    if previous_broken is not None:
        result["broken_return"] = cohort_return(previous_broken, changes)
    result["money_effect"] = money_effect(result["limit_up_return"])
    return result
