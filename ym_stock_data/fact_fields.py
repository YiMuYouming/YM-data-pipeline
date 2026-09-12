"""ym_stock_data.fact_fields — 字段合同与事实时间（P3.1）

两个问题（方案 02 篇第 4、5 节）：

1. **字段合同**：每个使用中的 setup 必须声明它需要哪些字段。缺字段只影响
   依赖它的判断（该 setup 记为 unknown），**不清空其它 setup**，也不能因为
   取不到就临时把必要字段降成可选。

2. **事实时间**：每项关键事实分别保存 ``event_at`` / ``received_at`` /
   ``computed_at``（派生指标才有）/ ``source`` / ``quality``。
   来源没有事件时间时必须显式 ``event_at=None`` 并记为 unknown，
   **不得用 fetched_now 冒充实时**。

provider 的 success/empty/degraded/error 与条件的 true/false/unknown
是两套不同含义，本模块不把前者翻译成后者。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ_SHANGHAI = timezone(timedelta(hours=8))

# 时钟偏移容差：事件时间略微晚于本机时钟不算异常。
CLOCK_SKEW_TOLERANCE_MS = 1000

# ── 字段合同 ────────────────────────────────────────────────────────
#
# ``required_for`` 说明字段在策略里的用途；``optional`` 必须在合同里显式声明，
# 不允许运行时按"取不到"临时降级。
FIELD_CONTRACTS: dict[str, dict] = {
    "trend_pullback": {
        "description": "趋势回踩（普通回踩）",
        "required_fields": (
            "price",
            "ma5_daily",
            "ma10_daily",
            "ma10_60m",
            "ma10_60m_dir",
            "volume_ratio",
            "change_pct",
        ),
        "optional_fields": ("ma20_daily",),
        "closed_bar_fields": ("ma10_60m", "ma10_60m_dir"),
    },
    "trend_strong": {
        "description": "强趋势多周期",
        "required_fields": (
            "price", "ma5_daily", "ma10_daily", "ma20_daily",
            "ma10_60m", "ma10_60m_dir", "ma5_15m", "volume_ratio",
        ),
        "optional_fields": (),
        "closed_bar_fields": ("ma10_60m", "ma10_60m_dir", "ma5_15m"),
    },
    "lianban_height": {
        "description": "连板高度与承接",
        "required_fields": (
            "price", "change_pct", "limit_up_count_avg_3d",
            "promotion_1_to_2_pct", "promotion_2_to_3_pct",
            "promotion_2_to_3_avg_3d", "highest_board",
        ),
        "optional_fields": ("promotion_3_to_4_pct",),
        "closed_bar_fields": (),
    },
    "position_risk": {
        "description": "持仓风险与可卖数量",
        "required_fields": ("price", "sellable_qty", "quantity"),
        "optional_fields": (),
        "closed_bar_fields": (),
    },
}

# 账户类字段来自 Hermes/pnl.db 权威，不来自行情 provider。
ACCOUNT_FIELDS = frozenset({"sellable_qty", "quantity", "cash", "total_asset"})


def contract_for(setup_id: str) -> dict:
    try:
        return FIELD_CONTRACTS[setup_id]
    except KeyError as exc:
        raise KeyError(f"unknown_setup:{setup_id}") from exc


def list_setups() -> list[str]:
    return sorted(FIELD_CONTRACTS)


# ── 三值逻辑 ────────────────────────────────────────────────────────

def all_required(values) -> bool | None:
    """三值 AND。

    - 任一 ``False`` → ``False``（条件确实不满足）；
    - 否则任一 ``None`` → ``None``（必要事实未知，**不等于 False，也不授予触发**）；
    - 全部 ``True`` → ``True``。
    """
    items = list(values)
    if any(value is False for value in items):
        return False
    if any(value is None for value in items):
        return None
    return True


def evaluate_setup(setup_id: str, facts: dict) -> dict:
    """按字段合同评估一个 setup，返回三值结论与缺失明细。

    缺字段只让**这个** setup 变 unknown；调用方不得据此清空其它 setup。
    """
    contract = contract_for(setup_id)
    missing_required, missing_optional, unknown_optional = [], [], []
    for field in contract["required_fields"]:
        if field not in facts or facts.get(field) is None:
            missing_required.append(field)
    for field in contract["optional_fields"]:
        if field not in facts or facts.get(field) is None:
            missing_optional.append(field)

    if missing_required:
        state = "unknown"
    else:
        state = "evaluable"

    # 已收盘 K 线口径：未收盘的数据不得当成已完成 bar。
    closed_bar_blocked = [
        field for field in contract["closed_bar_fields"]
        if facts.get(f"{field}__bar_closed") is False
    ]
    if closed_bar_blocked and state == "evaluable":
        state = "unknown"

    return {
        "setup_id": setup_id,
        "state": state,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "closed_bar_not_finished": closed_bar_blocked,
        "required_for": "setup_evaluation",
        # 三值语义：
        #   - ``None``：必要事实未知 → 条件**不可判定**（不等于条件为假）；
        #   - ``"computable"``：必要事实齐备 → 条件**可计算**，
        #     但具体真假由规则引擎判定，本模块不代它给结论，更不授予触发。
        "condition_truth": None if state == "unknown" else "computable",
    }


def evaluate_setups(setup_ids, facts: dict) -> dict:
    """批量评估；每个 setup 独立结论，互不牵连。"""
    results = {setup_id: evaluate_setup(setup_id, facts) for setup_id in setup_ids}
    return {
        "setups": results,
        "unknown_setups": sorted(k for k, v in results.items() if v["state"] == "unknown"),
        "evaluable_setups": sorted(k for k, v in results.items() if v["state"] == "evaluable"),
    }


# ── 事实时间 ────────────────────────────────────────────────────────

def parse_time(value) -> datetime | None:
    """解析带时区的 ISO 时间；无时区或不可解析一律 None（不可验证）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def event_age_ms(event_at, now: datetime | None = None) -> int | None:
    """事实年龄（毫秒）。

    ``event_at`` 缺失/不可验证 → ``None``（unknown），不是 0。
    事件时间明显晚于现在 → ``ValueError('clock_skew')``。
    """
    parsed = parse_time(event_at)
    if parsed is None:
        return None
    reference = now or datetime.now(TZ_SHANGHAI)
    if reference.tzinfo is None:
        raise ValueError("timezone_required")
    age_ms = (reference - parsed).total_seconds() * 1000
    if age_ms < -CLOCK_SKEW_TOLERANCE_MS:
        raise ValueError("clock_skew")
    return max(0, int(age_ms))


def build_fact(name: str, *, event_at=None, received_at=None, computed_at=None,
               source: str = "", source_mode: str = "", quality: str = "",
               instrument: str = "", trading_date: str = "") -> dict:
    """构造一条事实记录。

    关键规则：``event_at`` 缺失时**保持 None 与 unknown**，
    绝不用 ``received_at`` 或当前时间顶替。
    """
    fact = {
        "fact": name,
        "event_at": event_at,
        "received_at": received_at,
        "computed_at": computed_at,
        "source": source or None,
        "source_mode": source_mode or None,
        "quality": quality or None,
        "instrument": instrument or None,
        "trading_date": trading_date or None,
        "event_at_verified": parse_time(event_at) is not None,
        "status": "unknown",
        "reason": None,
    }
    if fact["event_at_verified"]:
        fact["status"] = "measured"
    else:
        fact["reason"] = "event_at_missing"
    return fact


def fact_is_fresh(fact: dict, max_age_ms: int, now: datetime | None = None) -> dict:
    """按**事件时间**判断新鲜度。

    ``received_at`` 新而 ``event_at`` 旧时，结论仍是旧。
    """
    row = fact if isinstance(fact, dict) else {}
    age = None
    try:
        age = event_age_ms(row.get("event_at"), now)
    except ValueError as exc:
        return {"fresh": False, "age_ms": None, "reason": str(exc)}
    if age is None:
        return {"fresh": False, "age_ms": None, "reason": "event_at_unknown"}
    if age > int(max_age_ms):
        return {"fresh": False, "age_ms": age, "reason": "event_at_too_old"}
    return {"fresh": True, "age_ms": age, "reason": None}


def build_derived_fact(name: str, *, inputs, computed_at, source: str = "",
                       quality: str = "", instrument: str = "",
                       trading_date: str = "") -> dict:
    """派生指标的 facts 记录：``event_at`` 取**输入事实中最新的事件时间**。

    不以计算时刻冒充事件时间；无输入事件时间时保持 unknown。
    """
    event_times = [parse_time(getattr(item, "get", lambda *_: None)("event_at"))
                   for item in (inputs or [])]
    event_times = [value for value in event_times if value is not None]
    event_at = max(event_times).isoformat() if event_times else None
    return build_fact(
        name, event_at=event_at, received_at=computed_at, computed_at=computed_at,
        source=source, quality=quality, instrument=instrument,
        trading_date=trading_date,
    )
