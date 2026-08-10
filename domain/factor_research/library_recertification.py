"""Read-only full-library factor recertification helpers.

The recertification job deliberately writes only under ``runtime/``.  It
recomputes current and retired registry expressions with the production factor
semantics, but it never changes registry membership or production factor-value
artifacts.  Lifecycle labels emitted here are recommendations for operator
review, not registry mutations.
"""

from __future__ import annotations

import json
import math
import os
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from domain.platform_evaluation import resolve_evaluation_profile
from storage.factor_registry import FactorRegistry
from storage.paths import RUNTIME_ROOT, get_live_factor_research_config


RECERTIFICATION_ROOT = RUNTIME_ROOT / "factor_research" / "library_recertification"
SCHEMA_VERSION = "factor_library_recertification_v1"
POLICY_VERSION = "factor_library_lifecycle_advice_v1"

OFFICIAL_DEEP_SCORE_MIN = 80.0
ACTIVE_RETENTION_DEEP_SCORE_MIN = 79.5
OFFICIAL_QUICK_SCORE_MIN = 70.0
OFFICIAL_ABS_RANK_IC_MIN = 0.02
OFFICIAL_ABS_RANK_ICIR_MIN = 0.30

HARD_EXIT_DEEP_SCORE = 70.0
HARD_EXIT_QUICK_SCORE = 55.0
HARD_EXIT_ABS_RANK_IC = 0.01
HARD_EXIT_ABS_RANK_ICIR = 0.15

BEHAVIORAL_CORRELATION_REVIEW = 0.85


def normalized_expression(expression: str) -> str:
    return re.sub(r"\s+", "", str(expression or "")).lower()


def json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Series):
        return value.to_dict()
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    raise TypeError(f"not_json_serializable:{type(value).__name__}")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def registry_snapshot() -> list[dict[str, Any]]:
    registry = FactorRegistry()
    rows, total = registry.list_all(status="all", min_icir=-1e9, limit=100_000)
    if len(rows) != total:
        raise RuntimeError(f"registry_snapshot_incomplete:{len(rows)}/{total}")
    snapshot: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw_metadata = item.get("metadata")
        if isinstance(raw_metadata, str):
            try:
                item["metadata"] = json.loads(raw_metadata or "{}")
            except json.JSONDecodeError:
                item["metadata"] = {"unparsed_metadata": raw_metadata}
        elif not isinstance(raw_metadata, dict):
            item["metadata"] = {}
        snapshot.append(item)
    return sorted(snapshot, key=lambda row: str(row.get("factor_id") or ""))


def rolling_config() -> dict[str, Any]:
    config = get_live_factor_research_config().get("rolling_validation") or {}
    return {
        "max_history_months": int(config.get("max_history_months", 48)),
        "min_history_months": int(config.get("min_history_months", 24)),
        "period_weights": tuple(float(v) for v in config.get("period_weights", (0.40, 0.25, 0.15, 0.12, 0.08))),
        "stability_penalty": float(config.get("stability_penalty", 0.25)),
        "rank_ic_full_score": float(config.get("rank_ic_full_score", 0.08)),
        "min_dates_per_6m": int(config.get("min_dates_per_6m", 60)),
        "horizons": tuple(int(v) for v in config.get("trailing_horizons_months", (6, 12, 24, 36, 48))),
    }


def prior_retirement_reason(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for value in (
        row.get("retire_reason"),
        metadata.get("retired_reason"),
        metadata.get("retirement_reason"),
        metadata.get("status_reason"),
        metadata.get("reason"),
    ):
        if value:
            return str(value)
    return ""


def has_policy_review_reason(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(token in text for token in ("st_exposure", "distress", "backward_factor", "invalid_field"))


def official_quality_pass(result: dict[str, Any]) -> bool:
    summary = result.get("backtest_summary") or {}
    return bool(
        result.get("status") == "success"
        and float(result.get("quick_score") or 0.0) >= OFFICIAL_QUICK_SCORE_MIN
        and float(result.get("deep_score") or 0.0) >= OFFICIAL_DEEP_SCORE_MIN
        and abs(float(summary.get("rank_ic_mean") or 0.0)) >= OFFICIAL_ABS_RANK_IC_MIN
        and abs(float(summary.get("rank_ic_ir") or 0.0)) >= OFFICIAL_ABS_RANK_ICIR_MIN
    )


def active_retention_pass(result: dict[str, Any]) -> bool:
    """Allow a narrow 0.5-point tolerance only for an already-active factor."""
    summary = result.get("backtest_summary") or {}
    return bool(
        result.get("status") == "success"
        and float(result.get("quick_score") or 0.0) >= OFFICIAL_QUICK_SCORE_MIN
        and float(result.get("deep_score") or 0.0) >= ACTIVE_RETENTION_DEEP_SCORE_MIN
        and abs(float(summary.get("rank_ic_mean") or 0.0)) >= OFFICIAL_ABS_RANK_IC_MIN
        and abs(float(summary.get("rank_ic_ir") or 0.0)) >= OFFICIAL_ABS_RANK_ICIR_MIN
    )


def classify_lifecycle(result: dict[str, Any], registry_row: dict[str, Any]) -> dict[str, Any]:
    """Return conservative lifecycle advice without mutating the registry."""
    current_status = str(registry_row.get("status") or "")
    summary = result.get("backtest_summary") or {}
    quick = float(result.get("quick_score") or 0.0)
    deep = float(result.get("deep_score") or 0.0)
    abs_ic = abs(float(summary.get("rank_ic_mean") or 0.0))
    abs_icir = abs(float(summary.get("rank_ic_ir") or 0.0))
    direction_review = bool(result.get("direction_review"))
    quality_pass = official_quality_pass(result)
    retention_pass = active_retention_pass(result)
    reason = prior_retirement_reason(registry_row)

    evidence = {
        "current_status": current_status,
        "quality_pass": quality_pass,
        "active_retention_pass": retention_pass,
        "quick_score": round(quick, 1),
        "deep_score": round(deep, 1),
        "abs_rank_ic": round(abs_ic, 6),
        "abs_rank_icir": round(abs_icir, 6),
        "direction_review": direction_review,
        "prior_retirement_reason": reason,
        "policy_review_required": has_policy_review_reason(reason),
    }

    if result.get("status") != "success":
        advice = "exit_candidate" if current_status == "active" else "keep_retired"
        return {"advice": advice, "reason": str(result.get("error_code") or result.get("status") or "recompute_failed"), "evidence": evidence}

    # A sign rewrite is useful only when the recomputed signal is otherwise
    # strong enough to reach the deep-validation lane.  Weak flipped factors
    # remain weak factors and must not be promoted into expression repair work.
    if direction_review and quick >= OFFICIAL_QUICK_SCORE_MIN:
        return {
            "advice": "direction_review",
            "reason": "best_long_only_side_requires_expression_level_sign_review",
            "evidence": evidence,
        }

    if current_status == "active":
        if retention_pass:
            reason_code = (
                "passes_current_official_quality_thresholds"
                if quality_pass
                else "passes_active_retention_tolerance_79_5"
            )
            return {"advice": "keep_active", "reason": reason_code, "evidence": evidence}
        hard_weak = (
            deep < HARD_EXIT_DEEP_SCORE
            or quick < HARD_EXIT_QUICK_SCORE
            or abs_ic < HARD_EXIT_ABS_RANK_IC
            or abs_icir < HARD_EXIT_ABS_RANK_ICIR
        )
        return {
            "advice": "exit_candidate" if hard_weak else "active_review",
            "reason": "material_quality_failure" if hard_weak else "below_official_gate_but_not_materially_weak",
            "evidence": evidence,
        }

    if quality_pass and has_policy_review_reason(reason):
        return {
            "advice": "policy_review",
            "reason": "quality_recovered_but_prior_policy_retirement_requires_separate_clearance",
            "evidence": evidence,
        }
    if quality_pass:
        return {"advice": "restore_candidate", "reason": "passes_current_official_quality_thresholds", "evidence": evidence}
    return {"advice": "keep_retired", "reason": "does_not_pass_current_official_quality_thresholds", "evidence": evidence}


def build_manifest(
    *,
    evaluation_mode: str,
    run_id: str,
    rows: list[dict[str, Any]],
    workers: int,
) -> dict[str, Any]:
    profile = resolve_evaluation_profile(evaluation_mode)
    factor = profile["factor"]
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "evaluation_profile": profile,
        "selection_window": {
            "start_date": factor["selection_start_date"],
            "end_date": factor["selection_end_date"],
        },
        "value_window": {
            "start_date": factor["value_start_date"],
            "end_date": factor["value_end_date"],
        },
        "universe": "tradable_non_st",
        "holding_period_days": 5,
        "neutralize_cap": True,
        "neutralize_industry": False,
        "cost_rate": 0.003,
        "registry_status_counts": counts,
        "factor_count": len(rows),
        "workers": int(workers),
        "registry_mutation": False,
        "production_factor_value_mutation": False,
        "compute_note": (
            "Expressions are evaluated with canonical factor_evaluator tradable_non_st semantics. The fixed non-ST "
            "mask is present during cross-sectional operators such as rank and is also applied before backtest."
        ),
        "score_contract": {
            "quick": "FXAlpha local long-only quick score",
            "deep": {"quick": 0.55, "anti_overfit": 0.15, "rolling": 0.20, "adversarial": 0.10},
            "official_pass": {
                "quick_score_min": OFFICIAL_QUICK_SCORE_MIN,
                "deep_score_min": OFFICIAL_DEEP_SCORE_MIN,
                "abs_rank_ic_min": OFFICIAL_ABS_RANK_IC_MIN,
                "abs_rank_icir_min": OFFICIAL_ABS_RANK_ICIR_MIN,
            },
            "rolling": rolling_config(),
        },
        "registry_rows": rows,
    }


def result_path(run_dir: Path, factor_id: str) -> Path:
    return run_dir / "results" / f"{factor_id}.json"


def completed_factor_ids(run_dir: Path) -> set[str]:
    completed: set[str] = set()
    for path in (run_dir / "results").glob("*.json") if (run_dir / "results").exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("factor_id") and payload.get("terminal") is True:
            completed.add(str(payload["factor_id"]))
    return completed


def summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("backtest_summary") or {}
    rolling = result.get("rolling_validation") or {}
    advice = result.get("lifecycle_advice") or {}
    horizons = rolling.get("trailing_horizons") or {}

    def horizon_ic(months: int) -> float | None:
        value = horizons.get(f"{months}m", horizons.get(str(months), horizons.get(months)))
        if isinstance(value, dict):
            for key in ("rank_ic", "rank_ic_mean", "ic"):
                if value.get(key) is not None:
                    return float(value[key])
        return None

    return {
        "factor_id": result.get("factor_id"),
        "name": result.get("name"),
        "current_status": result.get("current_status"),
        "status": result.get("status"),
        "quick_score": result.get("quick_score"),
        "quick_grade": result.get("quick_grade"),
        "deep_score": result.get("deep_score"),
        "deep_grade": result.get("deep_grade"),
        "rank_ic_mean": summary.get("rank_ic_mean"),
        "rank_ic_ir": summary.get("rank_ic_ir"),
        "annual_return": summary.get("annual_return"),
        "sharpe": summary.get("sharpe"),
        "max_drawdown": summary.get("max_drawdown"),
        "turnover": summary.get("turnover"),
        "flipped": summary.get("flipped"),
        "anti_overfit_score": (result.get("anti_overfit") or {}).get("score"),
        "rolling_score": rolling.get("score"),
        "rolling_6m_ic": horizon_ic(6),
        "rolling_12m_ic": horizon_ic(12),
        "rolling_24m_ic": horizon_ic(24),
        "adversarial_score": (result.get("adversarial_validation") or {}).get("score"),
        "quality_pass": official_quality_pass(result),
        "advice": advice.get("advice"),
        "advice_reason": advice.get("reason"),
        "prior_retirement_reason": (advice.get("evidence") or {}).get("prior_retirement_reason"),
        "runtime_seconds": result.get("runtime_seconds"),
        "error_code": result.get("error_code"),
    }


def load_results(run_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in sorted((run_dir / "results").glob("*.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if result.get("terminal") is True:
            results.append(result)
    return results


def _mean_daily_spearman(left: pd.DataFrame, right: pd.DataFrame) -> float | None:
    merged = left.merge(right, on=["trade_date", "stock_code"], suffixes=("_left", "_right"))
    if merged.empty:
        return None
    daily: list[float] = []
    for _, day in merged.groupby("trade_date", sort=False):
        if len(day) < 50:
            continue
        corr = day["factor_value_left"].corr(day["factor_value_right"], method="spearman")
        if corr is not None and np.isfinite(corr):
            daily.append(float(corr))
    return float(np.mean(daily)) if daily else None


def behavioral_redundancy(run_dir: Path, candidate_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Compare monthly cross-sections for lifecycle-relevant factors only."""
    ids = sorted(set(str(value) for value in candidate_ids if value))
    samples: dict[str, pd.DataFrame] = {}
    for factor_id in ids:
        path = run_dir / "correlation_samples" / f"{factor_id}.parquet"
        if not path.exists():
            continue
        frame = pd.read_parquet(path)
        if {"trade_date", "stock_code", "factor_value"}.issubset(frame.columns):
            samples[factor_id] = frame[["trade_date", "stock_code", "factor_value"]]
    pairs: list[dict[str, Any]] = []
    available = sorted(samples)
    for i, left_id in enumerate(available):
        for right_id in available[i + 1 :]:
            corr = _mean_daily_spearman(samples[left_id], samples[right_id])
            if corr is None or abs(corr) < BEHAVIORAL_CORRELATION_REVIEW:
                continue
            pairs.append(
                {
                    "left_factor_id": left_id,
                    "right_factor_id": right_id,
                    "mean_daily_spearman": round(corr, 6),
                    "abs_mean_daily_spearman": round(abs(corr), 6),
                    "threshold": BEHAVIORAL_CORRELATION_REVIEW,
                }
            )
    return sorted(pairs, key=lambda item: -item["abs_mean_daily_spearman"])


def apply_exact_duplicate_advice(results: list[dict[str, Any]]) -> None:
    groups: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        groups.setdefault(normalized_expression(result.get("expression", "")), []).append(result)
    for expression, group in groups.items():
        if not expression or len(group) < 2:
            continue
        ordered = sorted(
            group,
            key=lambda item: (
                str(item.get("current_status")) != "active",
                -float(item.get("deep_score") or 0.0),
                str(item.get("factor_id")),
            ),
        )
        keeper = ordered[0]
        for duplicate in ordered[1:]:
            advice = duplicate.get("lifecycle_advice") or {}
            if duplicate.get("current_status") == "retired" and advice.get("advice") == "restore_candidate":
                advice["advice"] = "keep_retired_duplicate"
                advice["reason"] = f"exact_expression_duplicate_of:{keeper.get('factor_id')}"
            elif duplicate.get("current_status") == "active":
                advice["advice"] = "active_redundancy_review"
                advice["reason"] = f"exact_expression_duplicate_of:{keeper.get('factor_id')}"
            duplicate["lifecycle_advice"] = advice


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    manifest: Path
    status: Path
    results: Path
    correlation_samples: Path

    @classmethod
    def create(cls, run_id: str) -> "RunPaths":
        run_dir = RECERTIFICATION_ROOT / run_id
        paths = cls(
            run_dir=run_dir,
            manifest=run_dir / "manifest.json",
            status=run_dir / "status.json",
            results=run_dir / "results",
            correlation_samples=run_dir / "correlation_samples",
        )
        paths.results.mkdir(parents=True, exist_ok=True)
        paths.correlation_samples.mkdir(parents=True, exist_ok=True)
        return paths


def traceback_payload(exc: BaseException) -> dict[str, Any]:
    return {
        "status": "error",
        "error_code": type(exc).__name__,
        "error": str(exc)[:2000],
        "traceback": traceback.format_exc(limit=20),
    }
