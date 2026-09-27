"""Read-only P4 comparison of per-stock promotion metrics and their evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .market_facts import MarketFactStore
from .trading_calendar import TradeCalendarUnavailable, is_trading_day


_RATE_KEYS = (
    ("one_to_two", "one_to_two"),
    ("two_to_three", "two_to_three"),
    ("three_to_four_plus", "three_to_four"),
)


def _rate(value: Any, *, numerator_key: str = "numerator_codes",
          denominator_key: str = "denominator_codes") -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    numerator_codes = sorted(set(value.get(numerator_key) or []))
    denominator_codes = sorted(set(value.get(denominator_key) or []))
    numerator = value.get("numerator", len(numerator_codes))
    denominator = value.get("denominator", len(denominator_codes))
    pct = value.get("pct")
    if denominator == 0:
        pct = None
    elif pct is None:
        pct = round(numerator / denominator * 100, 6)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "pct": pct,
        "numerator_codes": numerator_codes,
        "denominator_codes": denominator_codes,
    }


def _compare_rate(candidate: Any, pipeline: Any, *,
                  candidate_numerator_key: str = "numerator_codes",
                  pipeline_numerator_key: str = "promoted_codes") -> dict[str, Any]:
    left = _rate(candidate, numerator_key=candidate_numerator_key)
    right = _rate(pipeline, numerator_key=pipeline_numerator_key,
                  denominator_key="denominator_codes")
    code_differences = {
        "numerator_candidate_only": sorted(set(left["numerator_codes"]) - set(right["numerator_codes"])),
        "numerator_pipeline_only": sorted(set(right["numerator_codes"]) - set(left["numerator_codes"])),
        "denominator_candidate_only": sorted(set(left["denominator_codes"]) - set(right["denominator_codes"])),
        "denominator_pipeline_only": sorted(set(right["denominator_codes"]) - set(left["denominator_codes"])),
    }
    pct_match = (
        left["pct"] is None and right["pct"] is None
        or left["pct"] is not None and right["pct"] is not None
        and abs(float(left["pct"]) - float(right["pct"])) <= 0.000001
    )
    matched = (
        left["numerator"] == right["numerator"]
        and left["denominator"] == right["denominator"]
        and left["numerator_codes"] == right["numerator_codes"]
        and left["denominator_codes"] == right["denominator_codes"]
        and pct_match
    )
    return {
        "matched": matched,
        "candidate": left,
        "pipeline": right,
        "difference": {
            "numerator": right["numerator"] - left["numerator"],
            "denominator": right["denominator"] - left["denominator"],
            "pct_points": (
                round(float(right["pct"]) - float(left["pct"]), 6)
                if left["pct"] is not None and right["pct"] is not None else None
            ),
        },
        "code_differences": code_differences,
    }


def _source_hash_checks(candidate: dict, fact_report: dict) -> dict[str, Any]:
    candidate_source = candidate.get("source") or {}
    evidence = fact_report.get("limit_evidence") or {}
    result = {}
    for side in ("previous", "current"):
        row = evidence.get(side) or {}
        expected_key = f"{side}_ingest_payload_sha256"
        expected = candidate_source.get(expected_key)
        observed = row.get("payload_sha256")
        result[side] = {
            "expected": expected,
            "observed": observed,
            "matched": bool(expected and observed and expected == observed),
        }
    return result


def _source_provenance(candidate: dict, fact_report: dict) -> dict[str, Any]:
    candidate_source = candidate.get("source") or {}
    evidence = fact_report.get("limit_evidence") or {}
    result = {}
    for side in ("previous", "current"):
        observed = evidence.get(side) or {}
        result[side] = {
            "candidate": {
                "provider": candidate_source.get("provider"),
                "fetched_at": candidate_source.get(f"{side}_fetched_at"),
                "source_time_status": candidate_source.get(f"{side}_source_time_status"),
                "payload_sha256": candidate_source.get(f"{side}_ingest_payload_sha256"),
                "stock_rows_sha256": candidate_source.get(f"{side}_sorted_stock_rows_sha256"),
                "board_source": candidate_source.get(f"board_source_{side}"),
            },
            "pipeline": {
                "provider": observed.get("provider"),
                "fetched_at": observed.get("fetched_at"),
                "source_time_status": observed.get("source_time_status"),
                "payload_sha256": observed.get("payload_sha256"),
                "stock_rows_sha256": None,
                "board_source": observed.get("board_source"),
            },
        }
    return result


def _expected_fill_subset(promotion: dict) -> dict[str, Any] | None:
    rates = promotion.get("rates") or {}
    overall = _rate(rates.get("overall"), numerator_key="promoted_codes")
    tier_names = ("one_to_two", "two_to_three", "three_to_four")
    if any(not isinstance(rates.get(name), dict) for name in tier_names):
        return None
    winners = sorted({
        code
        for name in tier_names
        for code in (rates[name].get("promoted_codes") or [])
    })
    denominator_codes = overall["denominator_codes"]
    numerator_codes = sorted(set(winners) & set(denominator_codes))
    numerator, denominator = len(numerator_codes), len(denominator_codes)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "pct": round(numerator / denominator * 100, 6) if denominator else None,
        "promoted_codes": numerator_codes,
        "denominator_codes": denominator_codes,
    }


def build_consumer_input(candidate: dict, fact_report: dict | None) -> dict[str, Any]:
    """Project one read-only recomputation into the code-free consumer contract."""
    fact_report = fact_report if isinstance(fact_report, dict) else {}
    promotion = fact_report.get("promotion") or {}
    if not promotion:
        return {
            "schema_version": "market-watch.promotion-metrics.v1",
            "authority": {"mode": "shadow_only", "active": False},
            "trade_date": candidate.get("trade_date"),
            "previous_trade_date": candidate.get("previous_trade_date"),
            "status": "blocked",
            "metrics": {},
            "provenance": {},
            "source_conflicts": [],
            "gaps": ["promotion_recompute_unavailable"],
            "validation_gate": {
                "reliable_history_days": None,
                "unexplained_differences": None,
                "authority_switch": "blocked",
            },
        }

    pipeline_rates = promotion.get("rates") or {}
    tier_keys = (
        ("one_to_two", "one_to_two"),
        ("two_to_three", "two_to_three"),
        ("three_to_four_plus", "three_to_four"),
    )
    metrics = {}
    gaps = list(fact_report.get("source_gaps") or [])
    for output_key, source_key in tier_keys:
        source = pipeline_rates.get(source_key) or {}
        numerator, denominator = source.get("numerator"), source.get("denominator")
        pct = source.get("pct")
        status = "available" if denominator else "unavailable"
        if not denominator:
            gaps.append(f"promotion_denominator_empty:{output_key}")
        metrics[output_key] = {
            "numerator": numerator,
            "denominator": denominator,
            "pct": pct if denominator else None,
            "status": status,
            "definition_status": "candidate",
        }

    all_tiers = pipeline_rates.get("overall") or {}
    fill_subset = _expected_fill_subset(promotion)
    current_consecutive = promotion.get("current_consecutive_codes") or []
    previous_consecutive = promotion.get("previous_consecutive_codes") or []
    literal_denominator = len(previous_consecutive)
    literal_numerator = len(current_consecutive)
    overall_candidates = [
        {
            "definition": "all_tier_promotions_over_previous_nonst_limitups",
            "numerator": all_tiers.get("numerator"),
            "denominator": all_tiers.get("denominator"),
            "pct": all_tiers.get("pct"),
            "definition_status": "unresolved",
        },
        {
            "definition": "current_consecutive_over_previous_consecutive",
            "numerator": literal_numerator,
            "denominator": literal_denominator,
            "pct": round(literal_numerator / literal_denominator * 100, 6) if literal_denominator else None,
            "definition_status": "unresolved",
        },
    ]
    if fill_subset is not None:
        overall_candidates.append({
            "definition": "market_watch_tier_subset_over_previous_nonst_limitups",
            "numerator": fill_subset["numerator"],
            "denominator": fill_subset["denominator"],
            "pct": fill_subset["pct"],
            "definition_status": "unresolved",
        })
    gaps.append("overall_definition_unresolved")

    source = candidate.get("source") or {}
    evidence = fact_report.get("limit_evidence") or {}
    provenance = {}
    for side in ("previous", "current"):
        row = evidence.get(side) or {}
        source_hash_key = f"{side}_sorted_stock_rows_sha256"
        payload_hash_key = f"{side}_ingest_payload_sha256"
        provenance[side] = {
            "provider": row.get("provider") or source.get("provider"),
            "fetched_at": row.get("fetched_at"),
            "source_time_status": row.get("source_time_status"),
            "payload_sha256": row.get("payload_sha256") or source.get(payload_hash_key),
            "stock_rows_sha256": source.get(source_hash_key),
            "board_source_status": row.get("board_source") or source.get(
                f"board_source_{side}"
            ),
        }
        if provenance[side]["source_time_status"] not in {"available", "verified"}:
            gaps.append(f"{side}_source_time_unavailable")
    conflicts = []
    external = str(source.get("external_provider_conflict") or "")
    if "not_assessed" in external:
        conflicts.append("external_provider_conflict_not_assessed")
        gaps.append("external_provider_conflict_not_assessed")
    if not source.get("target_fill_promotion_rates_provider_match"):
        gaps.append("provider_not_matched_to_fill_route")
    provenance["external_provider_conflict"] = external or "not_assessed"
    status = "partial" if gaps or conflicts else "ready"
    return {
        "schema_version": "market-watch.promotion-metrics.v1",
        "authority": {"mode": "shadow_only", "active": False},
        "trade_date": candidate.get("trade_date"),
        "previous_trade_date": candidate.get("previous_trade_date"),
        "status": status,
        "metrics": {**metrics, "overall_candidates": overall_candidates},
        "provenance": provenance,
        "source_conflicts": list(dict.fromkeys(conflicts)),
        "gaps": list(dict.fromkeys(gaps)),
        "validation_gate": {
            "reliable_history_days": None,
            "unexplained_differences": None,
            "authority_switch": "blocked",
        },
    }


def _reliability(candidate: dict, fact_report: dict,
                 hash_checks: dict[str, Any]) -> dict[str, Any]:
    source = candidate.get("source") or {}
    blockers: list[str] = []
    if not all(check["matched"] for check in hash_checks.values()):
        blockers.append("source_hash_mismatch_or_unavailable")
    if not source.get("target_fill_promotion_rates_provider_match"):
        blockers.append("provider_not_matched_to_fill_route")
    for side in ("previous", "current"):
        status = source.get(f"{side}_source_time_status")
        if status not in {"available", "verified"}:
            blockers.append("source_time_unavailable")
            break
    external = str(source.get("external_provider_conflict") or "").lower()
    source_conflicts = candidate.get("source_conflicts") or {}
    external_check = str(
        source_conflicts.get("between_stocktoday_and_fill_script_sources") or external
    ).lower()
    if "not_assessed" in external or "not_assessed" in external_check or not external_check:
        blockers.append("external_provider_conflict_not_assessed")
    if fact_report.get("source_gaps"):
        blockers.append("pipeline_source_gaps_present")
    return {
        "reliable_day": not blockers,
        "blockers": list(dict.fromkeys(blockers)),
    }


def compare_candidate_day(candidate: dict, fact_report: dict | None) -> dict[str, Any]:
    """Compare one baseline day with a read-only MarketFactStore recomputation."""
    fact_report = fact_report if isinstance(fact_report, dict) else {}
    promotion = fact_report.get("promotion") or {}
    candidate_rates = candidate.get("rates") or {}
    pipeline_rates = promotion.get("rates") or {}
    hash_checks = _source_hash_checks(candidate, fact_report)
    source_hashes_match = bool(hash_checks) and all(
        check["matched"] for check in hash_checks.values()
    )

    rate_comparisons = {}
    for candidate_key, pipeline_key in _RATE_KEYS:
        if candidate_rates.get(candidate_key) is None or pipeline_rates.get(pipeline_key) is None:
            rate_comparisons[candidate_key] = {
                "matched": False,
                "status": "unavailable",
                "candidate": _rate(candidate_rates.get(candidate_key)),
                "pipeline": _rate(pipeline_rates.get(pipeline_key), numerator_key="promoted_codes"),
                "code_differences": None,
            }
        else:
            rate_comparisons[candidate_key] = _compare_rate(
                candidate_rates[candidate_key], pipeline_rates[pipeline_key]
            )

    all_tiers = _compare_rate(
        candidate_rates.get("overall_candidate_all_tiers_over_previous_nonst_limitups"),
        pipeline_rates.get("overall"),
    )
    expected_fill_subset = _expected_fill_subset(promotion)
    fill_subset_candidate = candidate_rates.get("overall_current_market_watch_fill_subset")
    fill_subset = (
        _compare_rate(fill_subset_candidate, expected_fill_subset, pipeline_numerator_key="promoted_codes")
        if expected_fill_subset is not None else {
            "matched": False,
            "status": "unavailable",
            "candidate": _rate(fill_subset_candidate),
            "pipeline": None,
            "code_differences": None,
        }
    )
    candidate_high_promotions = set(
        ((candidate.get("high_board") or {}).get("previous_board4plus_promoted_codes") or [])
    )
    pipeline_high_promotions = set(promotion.get("promoted_high_board_codes") or [])
    fill_subset_codes = set((expected_fill_subset or {}).get("promoted_codes") or [])
    literal_candidate = _rate(
        candidate_rates.get("overall_literal_consecutive_over_previous_consecutive")
    )
    actual_literal = {
        "numerator": len(promotion.get("current_consecutive_codes") or []),
        "denominator": len(promotion.get("previous_consecutive_codes") or []),
        "pct": (
            round(
                len(promotion.get("current_consecutive_codes") or [])
                / len(promotion.get("previous_consecutive_codes") or []) * 100,
                6,
            )
            if promotion.get("previous_consecutive_codes") else None
        ),
        "numerator_codes": sorted(promotion.get("current_consecutive_codes") or []),
        "denominator_codes": sorted(promotion.get("previous_consecutive_codes") or []),
    }
    literal = {
        "definition_status": "unresolved",
        "candidate": literal_candidate,
        "literal_current_consecutive_over_previous_consecutive": actual_literal,
        "candidate_numerator_outside_denominator_codes": sorted(
            set(literal_candidate["numerator_codes"])
            - set(literal_candidate["denominator_codes"])
        ),
        "candidate_pct_over_100": (
            literal_candidate["pct"] is not None and literal_candidate["pct"] > 100
        ),
    }
    candidate_st = candidate.get("st") or {}
    st_result = {}
    for side in ("previous", "current"):
        candidate_codes = sorted(candidate_st.get(f"{side}_codes") or [])
        pipeline_codes = sorted(promotion.get(f"excluded_st_{side}_codes") or [])
        st_result[side] = {
            "candidate_codes": candidate_codes,
            "pipeline_codes": pipeline_codes,
            "matched": candidate_codes == pipeline_codes,
            "candidate_count": candidate_st.get(f"{side}_excluded_count_by_name"),
            "pipeline_count": promotion.get(f"excluded_st_{side}"),
            "code_differences": {
                "candidate_only": sorted(set(candidate_codes) - set(pipeline_codes)),
                "pipeline_only": sorted(set(pipeline_codes) - set(candidate_codes)),
            },
        }

    unexplained = [
        name for name, result in rate_comparisons.items()
        if result.get("status") != "unavailable" and not result["matched"]
    ]
    if not all_tiers["matched"]:
        unexplained.append("overall_all_tiers")
    if fill_subset.get("status") != "unavailable" and not fill_subset["matched"]:
        unexplained.append("overall_market_watch_fill_subset")
    if any(not value["matched"] for value in st_result.values()):
        unexplained.append("st_exclusion_sets")

    reliability = _reliability(candidate, fact_report, hash_checks)
    return {
        "trade_date": candidate.get("trade_date"),
        "previous_trade_date": candidate.get("previous_trade_date"),
        "recompute_status": "recomputed" if promotion else "source_unavailable",
        "source_hash_checks": hash_checks,
        "source_hashes_match": source_hashes_match,
        "source_provenance": _source_provenance(candidate, fact_report),
        "provider": {
            "candidate": (candidate.get("source") or {}).get("provider"),
            "pipeline_current": ((fact_report.get("limit_evidence") or {}).get("current") or {}).get("provider"),
            "target_fill_provider_match": (candidate.get("source") or {}).get(
                "target_fill_promotion_rates_provider_match"
            ),
        },
        "row_coverage": {
            "candidate": candidate.get("row_coverage"),
            "pipeline_previous_non_st": promotion.get("previous_non_st_count"),
            "pipeline_current_non_st": promotion.get("current_non_st_count"),
        },
        "st_exclusion": st_result,
        "high_board": {
            "candidate_previous_board4plus_codes": sorted(
                (candidate.get("high_board") or {}).get("previous_board4plus_codes") or []
            ),
            "pipeline_previous_board4plus_codes": sorted(
                promotion.get("previous_high_board_codes") or []
            ),
            "candidate_promoted_codes": sorted(candidate_high_promotions),
            "pipeline_promoted_codes": sorted(pipeline_high_promotions),
            "omitted_by_market_watch_fill_subset": sorted(pipeline_high_promotions - fill_subset_codes),
        },
        "rates": rate_comparisons,
        "overall_candidates": {
            "all_tiers_over_previous_nonst_limitups": all_tiers,
            "market_watch_fill_subset": {
                **fill_subset,
                "omitted_high_board_promoted_codes": sorted(pipeline_high_promotions - fill_subset_codes),
            },
            "literal_consecutive": literal,
        },
        "source_conflicts": {
            "baseline_internal_ladder": ((candidate.get("source_conflicts") or {}).get(
                "within_stocktoday_ladder_crosscheck"
            )),
            "external_provider": (candidate.get("source") or {}).get("external_provider_conflict"),
            "pipeline_source_gaps": list(fact_report.get("source_gaps") or []),
        },
        "empty_denominators": sorted(candidate.get("empty_denominators") or []),
        "unexplained_difference_metrics": sorted(set(unexplained)),
        "reliability": reliability,
        "consumer_input": build_consumer_input(candidate, fact_report),
    }


def find_sample_session_gaps(trade_dates: list[str]) -> dict[str, Any]:
    """List exchange sessions omitted between sampled dates, not missing source data."""
    parsed = sorted({date.fromisoformat(value) for value in trade_dates})
    gaps = []
    try:
        for left, right in zip(parsed, parsed[1:]):
            omitted = []
            cursor = left + timedelta(days=1)
            while cursor < right:
                if is_trading_day(cursor):
                    omitted.append(cursor.isoformat())
                cursor += timedelta(days=1)
            if omitted:
                gaps.append({
                    "from": left.isoformat(),
                    "to": right.isoformat(),
                    "omitted_sample_sessions": omitted,
                    "interpretation": "candidate_sample_gap",
                })
    except TradeCalendarUnavailable as exc:
        return {"status": "unavailable", "reason": str(exc), "gaps": gaps}
    return {"status": "verified", "gaps": gaps}


def build_comparison_report(baseline: dict,
                            fact_reports: dict[str, dict]) -> dict[str, Any]:
    candidates = baseline.get("candidate_days") or []
    days = [
        compare_candidate_day(candidate, fact_reports.get(candidate.get("trade_date", "")))
        for candidate in candidates
    ]
    source_hash_recomputed = sum(day["source_hashes_match"] for day in days)
    reliable_days = sum(day["reliability"]["reliable_day"] for day in days)
    comparable_days = [day for day in days if day["source_hashes_match"]]
    unexplained = sum(len(day["unexplained_difference_metrics"]) for day in comparable_days)
    gap_report = find_sample_session_gaps([day["trade_date"] for day in days if day.get("trade_date")])
    blockers = []
    if reliable_days < 10:
        blockers.append(f"reliable_history_days_below_10:{reliable_days}")
    if any(
        not day["source_hashes_match"] for day in days
    ):
        blockers.append("source_hash_recompute_incomplete")
    if any("source_time_unavailable" in day["reliability"]["blockers"] for day in days):
        blockers.append("source_time_unavailable")
    if any("external_provider_conflict_not_assessed" in day["reliability"]["blockers"] for day in days):
        blockers.append("external_provider_conflict_not_assessed")
    if any("provider_not_matched_to_fill_route" in day["reliability"]["blockers"] for day in days):
        blockers.append("provider_not_matched_to_fill_route")
    blockers.append("overall_definition_unresolved")
    return {
        "schema_version": "market-watch.promotion-diff-report.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline": {
            "schema_version": baseline.get("schema_version"),
            "prepared_at": baseline.get("prepared_at"),
            "candidate_day_count": len(candidates),
            "canonical_json_sha256": hashlib.sha256(
                json.dumps(baseline, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        },
        "sample_coverage": gap_report,
        "p4_gate": {
            "required_reliable_days": 10,
            "source_hash_recomputed_days": source_hash_recomputed,
            "reliable_history_days": reliable_days,
            "unexplained_difference_count_on_recomputed_candidates": unexplained,
            "unexplained_difference_count_on_reliable_days": (
                sum(len(day["unexplained_difference_metrics"]) for day in days
                    if day["reliability"]["reliable_day"])
                if reliable_days else None
            ),
            "overall_definition_resolved": False,
            "authority_switch_allowed": False,
            "blockers": list(dict.fromkeys(blockers)),
        },
        "days": days,
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only per-stock promotion comparison for the P4 baseline"
    )
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    baseline_bytes = args.baseline.read_bytes()
    baseline = json.loads(baseline_bytes)
    candidates = baseline.get("candidate_days") or []
    database = args.database
    if database is None and candidates:
        database = Path(candidates[0]["source"]["database_path"])
    if database is None:
        parser.error("baseline does not name a read-only source database; pass --database")
    store = MarketFactStore(database, read_only=True)
    fact_reports = {}
    for candidate in candidates:
        trade_date = str(candidate.get("trade_date") or "").replace("-", "")
        iso_date = candidate.get("trade_date")
        try:
            fact_reports[iso_date] = store.report(trade_date)
        except (OSError, ValueError) as exc:
            fact_reports[iso_date] = {
                "trade_date": trade_date,
                "source_gaps": [f"recompute_failed:{type(exc).__name__}:{exc}"],
            }
    result = build_comparison_report(baseline, fact_reports)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result["p4_gate"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
