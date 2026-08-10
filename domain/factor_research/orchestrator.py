from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from domain.factor_research.auto_import import canonical_factor_name, classify_factor_expression, factor_name_quality_reason, generate_factor_name
from domain.factor_research import quality_gate


NORMALIZATION_OPS = {"rank", "zscore", "scale", "tanh", "sigmoid"}
NONLINEAR_OPS = {"tanh", "sigmoid", "power", "sign_power", "log", "sqrt", "exp"}
EVOLUTION_STRATEGIES = {"exploit", "explore", "recombine", "simplify"}
MUTATION_STRATEGIES = {
    "mutate_window",
    "mutate_operator",
    "mutate_normalization",
    "mutate_signal_type",
    "mutate_nonlinear",
    "mutate_interaction",
    "simplify",
    "regenerate_full",
}
_EXPRESSION_RESERVED_WORDS = {
    "and",
    "or",
    "not",
    "true",
    "false",
    "none",
    "nan",
    "inf",
}
_OPERATOR_REPLACEMENTS = {
    "ts_mean": ["decay_linear", "ts_sum", "ts_rank"],
    "ts_std": ["ts_mean", "ts_rank", "ts_zscore"],
    "ts_delta": ["ts_shift", "ts_rank", "ts_av_diff"],
    "ts_corr": ["ts_cov", "ts_rank"],
    "ts_cov": ["ts_corr", "ts_rank"],
    "ts_rank": ["rank", "zscore", "ts_zscore"],
    "rank": ["zscore", "scale", "tanh"],
    "zscore": ["rank", "scale", "tanh"],
    "decay_linear": ["ts_mean", "ts_sum"],
    "ts_max": ["ts_min", "ts_argmax"],
    "ts_min": ["ts_max", "ts_argmin"],
}


@dataclass(frozen=True)
class TrajectoryMetrics:
    exploration_diversity: float
    convergence_rate: float
    stability_score: float
    consecutive_declines: int
    best_score: float
    best_expression: str
    num_iterations: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "exploration_diversity": self.exploration_diversity,
            "convergence_rate": self.convergence_rate,
            "stability_score": self.stability_score,
            "consecutive_declines": self.consecutive_declines,
            "best_score": self.best_score,
            "best_expression": self.best_expression,
            "num_iterations": self.num_iterations,
        }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except Exception:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def expression_profile(expression: str | None) -> dict[str, Any]:
    expr = str(expression or "")
    lower = expr.lower()
    depth = 0
    max_depth = 0
    for char in expr:
        if char == "(":
            depth += 1
            max_depth = max(max_depth, depth)
        elif char == ")":
            depth = max(0, depth - 1)
    operator_calls = {
        match.group(1).lower()
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expr)
    }
    identifiers = {
        token.lower()
        for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expr)
    }
    used_base_vars = sorted(
        token
        for token in identifiers
        if token not in operator_calls and token not in _EXPRESSION_RESERVED_WORDS
    )
    windows = sorted({int(match) for match in re.findall(r"ts_\w+\([^,]+,\s*(\d+)\)", lower)})
    return {
        "nesting_depth": max_depth,
        "has_normalization": any(f"{op}(" in lower for op in NORMALIZATION_OPS),
        "has_nonlinear": any(f"{op}(" in lower for op in NONLINEAR_OPS),
        "operators": sorted(operator_calls),
        "base_signal_count": len(used_base_vars),
        "base_signals": used_base_vars,
        "windows": windows,
        "length": len(expr),
    }


def analyze_trajectory(iterations: list[dict[str, Any]] | None) -> dict[str, Any]:
    items = iterations or []
    if not items:
        return TrajectoryMetrics(0, 0, 1.0, 0, 0, "", 0).to_dict()
    scores = [_safe_float(item.get("score"), 0.0) for item in items]
    n = len(scores)
    # A high Quick score is not a reusable parent after a downstream novelty,
    # deep, or gate decision has formally rejected it.  Keep every observation
    # in the trend metrics, but choose ``best`` only from parent-eligible
    # observations.  Current candidates omit this flag and remain eligible.
    eligible_indexes = [
        idx
        for idx, item in enumerate(items)
        if item.get("parent_eligible") is not False
    ]
    best_idx = max(eligible_indexes or range(n), key=lambda idx: scores[idx])
    best_score = scores[best_idx]
    mean_score = sum(scores) / n if n else 0.0
    variance = sum((score - mean_score) ** 2 for score in scores) / n if n else 0.0
    std = math.sqrt(variance)
    diversity = min(std / mean_score, 1.0) if n >= 2 and mean_score > 0 else 0.0
    if n >= 2:
        x_mean = (n - 1) / 2.0
        y_mean = mean_score
        denom = sum((idx - x_mean) ** 2 for idx in range(n))
        slope = sum((idx - x_mean) * (scores[idx] - y_mean) for idx in range(n)) / denom if denom else 0.0
        convergence = _clamp(slope / 10.0, 0.0, 1.0)
    else:
        convergence = 0.0
    stability = max(0.0, 1.0 - (std / best_score)) if n >= 2 and best_score > 0 else 1.0
    declines = 0
    for idx in range(n - 1, 0, -1):
        if scores[idx] < scores[idx - 1]:
            declines += 1
        else:
            break
    return TrajectoryMetrics(
        exploration_diversity=round(diversity, 3),
        convergence_rate=round(convergence, 3),
        stability_score=round(stability, 3),
        consecutive_declines=declines,
        best_score=best_score,
        best_expression=str(items[best_idx].get("expression") or ""),
        num_iterations=n,
    ).to_dict()


def meta_strategy(metrics: dict[str, Any], current_score: float, nesting_depth: int = 0) -> dict[str, str]:
    n = int(metrics.get("num_iterations") or 0)
    diversity = _safe_float(metrics.get("exploration_diversity"))
    convergence = _safe_float(metrics.get("convergence_rate"))
    stability = _safe_float(metrics.get("stability_score"), 1.0)
    declines = int(metrics.get("consecutive_declines") or 0)
    best_score = _safe_float(metrics.get("best_score"))
    if nesting_depth > 8:
        return {"strategy": "simplify", "action": "simplify_expression", "reason": "nesting_depth_gt_8"}
    if declines >= 2 and n >= 3:
        return {"strategy": "recombine", "action": "recombine_from_best", "reason": "plateaued_consecutive_declines"}
    if current_score < 30 and n <= 3:
        return {"strategy": "explore", "action": "explore_new_thesis", "reason": "early_low_score"}
    if best_score - current_score > 20 and n >= 2:
        return {"strategy": "recombine", "action": "recombine_from_best", "reason": "large_gap_to_best"}
    if current_score >= 60 and diversity < 0.3:
        return {"strategy": "exploit", "action": "targeted_mutation", "reason": "strong_score_low_diversity"}
    if diversity > 0.6 and convergence < 0.4:
        return {"strategy": "explore", "action": "explore_new_thesis", "reason": "high_diversity_low_convergence"}
    if 30 <= current_score < 60 and stability > 0.6:
        return {"strategy": "exploit", "action": "targeted_mutation", "reason": "medium_score_stable"}
    return {"strategy": "exploit", "action": "targeted_mutation", "reason": "default"}


def _suggest_operator_replacements(expression: str | None) -> dict[str, list[str]]:
    lower = str(expression or "").lower()
    return {
        operator: list(replacements)
        for operator, replacements in _OPERATOR_REPLACEMENTS.items()
        if f"{operator}(" in lower
    }


def _merge_trajectory(
    prior: list[dict[str, Any]] | None,
    current: list[dict[str, Any]] | None,
    *,
    score_getter=None,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for idx, item in enumerate([*(prior or []), *(current or [])]):
        if not isinstance(item, dict):
            continue
        expression = str(item.get("expression") or "")
        candidate_id = str(item.get("candidate_id") or "")
        round_id = str(item.get("round_id") or "")
        trajectory_id = str(item.get("trajectory_id") or "")
        if trajectory_id:
            key = ("trajectory", trajectory_id, "")
        elif round_id:
            key = (round_id, candidate_id, expression)
        elif candidate_id and expression:
            key = ("candidate", candidate_id, expression)
        else:
            key = ("", "", "")
        if key != ("", "", "") and key in seen:
            continue
        if key != ("", "", ""):
            seen.add(key)
        score = score_getter(item) if score_getter else item.get("score", item.get("quick_score"))
        merged.append(
            {
                **item,
                "expression": expression,
                "score": _safe_float(score, 0.0),
                "_trajectory_order": idx,
            }
        )
    return merged


def _structural_trajectory_signature(expression: Any) -> str:
    """Return an in-memory fallback signature, never a persisted identity."""

    profile = expression_profile(str(expression or ""))
    return "|".join(
        [
            ",".join(profile.get("base_signals") or []) or "no_fields",
            ",".join(profile.get("operators") or []) or "no_ops",
        ]
    )


def _candidate_ref_matches(item: dict[str, Any], reference: Any) -> bool:
    ref = str(reference or "").strip().lower()
    candidate_id = str(item.get("candidate_id") or "").strip().lower()
    if not ref or not candidate_id:
        return False
    if ref == candidate_id:
        return True
    if not ref.endswith(f":{candidate_id}"):
        return False
    # Scoped references use ``...:rNNNN:cN``.  Respect that round when it is
    # present; otherwise the most recent matching candidate is selected.
    ref_prefix = ref.rsplit(":", 1)[0].rsplit(":", 1)[-1]
    item_round = str(item.get("round_id") or "").strip().lower().rsplit(":", 1)[-1]
    if ref_prefix.startswith("r") and item_round.startswith("r"):
        return ref_prefix == item_round
    return True


def _candidate_region_uid(candidate: dict[str, Any]) -> str:
    novelty = _novelty_guard(candidate)
    return str(
        candidate.get("matched_region_uid")
        or novelty.get("matched_region_uid")
        or ""
    ).strip()


def _scoped_candidate_trajectory(
    candidate: dict[str, Any],
    iterations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Select the existing trajectory records that explain this candidate.

    Explicit parent lineage wins, then the existing Factor Map region.  A
    fields+operators signature is only a last-resort in-memory view for legacy
    records that predate those links.
    """

    ordered = sorted(
        [item for item in iterations if isinstance(item, dict)],
        key=lambda item: int(item.get("_trajectory_order") or 0),
    )
    candidate_id = str(candidate.get("candidate_id") or "")
    expression = str(candidate.get("expression") or "")
    round_id = str(candidate.get("round_id") or "")
    current_matches = [
        item
        for item in ordered
        if str(item.get("candidate_id") or "") == candidate_id
        and str(item.get("expression") or "") == expression
        and (not round_id or str(item.get("round_id") or "") == round_id)
    ]
    if not current_matches:
        current_matches = [
            item
            for item in ordered
            if str(item.get("candidate_id") or "") == candidate_id
            and str(item.get("expression") or "") == expression
        ]
    current = current_matches[-1] if current_matches else {
        **candidate,
        "score": candidate.get("score", candidate.get("deep_score")),
        "_trajectory_order": len(ordered),
    }
    current_order = int(current.get("_trajectory_order") or 0)
    prior = [
        item
        for item in ordered
        if int(item.get("_trajectory_order") or 0) < current_order
    ]

    parent_ref = candidate.get("parent_candidate_id") or current.get("parent_candidate_id")
    lineage: list[dict[str, Any]] = []
    used_orders: set[int] = set()
    while parent_ref:
        parent = next(
            (
                item
                for item in reversed(prior)
                if int(item.get("_trajectory_order") or 0) not in used_orders
                and _candidate_ref_matches(item, parent_ref)
            ),
            None,
        )
        if parent is None:
            break
        lineage.append(parent)
        used_orders.add(int(parent.get("_trajectory_order") or 0))
        parent_ref = parent.get("parent_candidate_id")
    if lineage:
        return [*reversed(lineage), current], "parent_lineage"

    region_uid = _candidate_region_uid(candidate) or _candidate_region_uid(current)
    if region_uid:
        region_items = [
            item
            for item in prior
            if _candidate_region_uid(item) == region_uid
        ]
        if region_items:
            return [*region_items, current], "factor_map_region"

    signature = _structural_trajectory_signature(expression)
    signature_items = [
        item
        for item in prior
        if _structural_trajectory_signature(item.get("expression")) == signature
    ]
    if signature_items:
        return [*signature_items, current], "structural_fallback"
    return [current], "current_only"


def analyze_candidate_progress(
    candidate: dict[str, Any],
    iterations: list[dict[str, Any]],
    *,
    current_score: float,
    nesting_depth: int = 0,
) -> dict[str, Any]:
    """Analyze progress inside the candidate's existing lineage/region view."""

    scoped, scope = _scoped_candidate_trajectory(candidate, iterations)
    metrics = analyze_trajectory(scoped)
    evolution = meta_strategy(metrics, current_score, nesting_depth)
    deep_scores = [
        _safe_float(item.get("score"))
        for item in scoped
        if item.get("score") not in (None, "")
    ]
    rolling_scores = [
        _safe_float(item.get("rolling_score"))
        for item in scoped
        if item.get("rolling_score") not in (None, "")
    ]
    deep_gain = (
        round(deep_scores[-1] - deep_scores[0], 3)
        if len(deep_scores) >= 2
        else None
    )
    rolling_gain = (
        round(rolling_scores[-1] - rolling_scores[0], 3)
        if len(rolling_scores) >= 2
        else None
    )
    meaningful_gain = bool(
        (deep_gain is not None and deep_gain >= 1.0)
        or (rolling_gain is not None and rolling_gain >= 2.0)
    )
    failed_attempts = sum(score < 80.0 for score in deep_scores)
    prior_actions = [
        str(item.get("downstream_action") or item.get("action") or "")
        for item in scoped[:-1]
    ]
    min_attempts = 2 if scope == "parent_lineage" else 3
    if (
        nesting_depth <= 8
        and failed_attempts >= min_attempts
        and not meaningful_gain
    ):
        if "recombine_from_best" in prior_actions or failed_attempts >= 4:
            evolution = {
                "strategy": "explore",
                "action": "explore_new_thesis",
                "reason": f"{scope}_stalled_after_recombine",
            }
        else:
            evolution = {
                "strategy": "recombine",
                "action": "recombine_from_best",
                "reason": f"{scope}_plateau_without_meaningful_gain",
            }
    return {
        "scope": scope,
        "attempts": len(deep_scores),
        "failed_attempts": failed_attempts,
        "deep_gain": deep_gain,
        "rolling_gain": rolling_gain,
        "meaningful_gain": meaningful_gain,
        "trajectory_metrics": metrics,
        "evolution_strategy": evolution,
        "recombination_candidates": (
            top_trajectory_segments(scoped)
            if evolution.get("strategy") == "recombine"
            else []
        ),
    }


def _mutation_for_evolution(
    evolution: dict[str, str],
    diagnosis: dict[str, Any],
) -> tuple[str, str]:
    if str(diagnosis.get("strategy") or "") in {"regenerate_full", "simplify"}:
        return str(diagnosis.get("action") or "explore_new_thesis"), str(diagnosis.get("reason") or "local_hard_diagnosis")
    strategy = str(evolution.get("strategy") or "exploit")
    if strategy == "simplify":
        return "simplify_expression", str(evolution.get("reason") or "simplify")
    if strategy == "explore":
        return "explore_new_thesis", str(evolution.get("reason") or "explore")
    if strategy == "recombine":
        return "recombine_from_best", str(evolution.get("reason") or "recombine")
    return str(diagnosis.get("action") or "targeted_mutation"), str(diagnosis.get("reason") or "targeted_mutation")


def top_trajectory_segments(
    iterations: list[dict[str, Any]] | None,
    *,
    min_score_ratio: float = 0.5,
    limit: int = 5,
) -> list[dict[str, Any]]:
    items = [
        item
        for item in (iterations or [])
        if isinstance(item, dict)
        and item.get("expression")
        and item.get("parent_eligible") is not False
    ]
    if not items:
        return []
    best_score = max(_safe_float(item.get("score")) for item in items)
    threshold = best_score * max(0.0, min(1.0, float(min_score_ratio)))
    qualified = [
        item
        for item in items
        if _safe_float(item.get("score")) >= threshold
    ]
    qualified.sort(key=lambda item: _safe_float(item.get("score")), reverse=True)
    return [
        {
            "round_id": item.get("round_id"),
            "candidate_id": item.get("candidate_id"),
            "expression": item.get("expression"),
            "score": _safe_float(item.get("score")),
            "grade": item.get("grade"),
        }
        for item in qualified[: max(1, int(limit or 1))]
    ]


def mutation_diagnosis(candidate: dict[str, Any]) -> dict[str, Any]:
    expression = str(candidate.get("expression") or "")
    score = _safe_float(candidate.get("score", candidate.get("quick_score")), 0.0)
    grade = str(candidate.get("grade") or "").upper()
    metrics = candidate.get("key_metrics") or candidate.get("backtest_summary") or {}
    ic_mean = _safe_float(metrics.get("ic_mean"))
    ic_ir = _safe_float(metrics.get("ic_ir"))
    profile = expression_profile(expression)
    if score < 20:
        return {"strategy": "regenerate_full", "action": "explore_new_thesis", "reason": "score_lt_20", "details": {"score": score}}
    if abs(ic_mean) < 0.005:
        return {
            "strategy": "mutate_operator",
            "action": "mutate_operator",
            "reason": "abs_ic_lt_0_005",
            "details": {
                "ic_mean": ic_mean,
                "suggested_replacements": _suggest_operator_replacements(expression),
            },
        }
    if ic_mean < -0.01 and grade in {"A", "B"}:
        return {"strategy": "mutate_signal_type", "action": "mutate_signal_direction", "reason": "negative_ic", "details": {"ic_mean": ic_mean}}
    if ic_mean < -0.01:
        return {
            "strategy": "regenerate_full",
            "action": "explore_new_thesis",
            "reason": "negative_ic_without_quick_keeper",
            "details": {"ic_mean": ic_mean, "grade": grade, "score": score},
        }
    if profile["nesting_depth"] > 8:
        return {"strategy": "simplify", "action": "simplify_expression", "reason": "nesting_depth_gt_8", "details": profile}
    if 20 <= score < 50 and not profile["has_nonlinear"]:
        return {"strategy": "mutate_nonlinear", "action": "mutate_nonlinear", "reason": "medium_score_no_nonlinear", "details": profile}
    if ic_ir < 0.5 and not profile["has_normalization"]:
        return {"strategy": "mutate_normalization", "action": "mutate_normalization", "reason": "low_icir_no_normalization", "details": {"ic_ir": ic_ir, **profile}}
    if profile["base_signal_count"] <= 1:
        return {"strategy": "mutate_interaction", "action": "mutate_interaction", "reason": "single_signal", "details": profile}
    return {"strategy": "mutate_window", "action": "adjust_window_or_signal_frequency", "reason": "default_window_mutation", "details": profile}


def quick_advice(candidates: list[dict[str, Any]], trajectory: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    complete_trajectory = _merge_trajectory(trajectory, candidates)
    trajectory_metrics = analyze_trajectory(complete_trajectory)
    lane_decisions: list[dict[str, Any]] = []
    allowed = []
    rejected = []
    for idx, candidate in enumerate(candidates or []):
        expression = str(candidate.get("expression") or "")
        grade = str(candidate.get("grade") or "").upper()
        score = _safe_float(candidate.get("score", candidate.get("quick_score")), 0.0)
        backtest = candidate.get("backtest_summary") if isinstance(candidate.get("backtest_summary"), dict) else {}
        metrics = candidate.get("key_metrics") if isinstance(candidate.get("key_metrics"), dict) else {}
        signed_rank_ic = _safe_float(
            candidate.get(
                "rank_ic",
                backtest.get("rank_ic_mean", metrics.get("rank_ic_mean", metrics.get("ic_mean"))),
            )
        )
        already_sign_flipped = str(candidate.get("mutation_summary") or "") == "global_sign_flip_only"
        valid = not candidate.get("validation_error") and candidate.get("status") not in {"invalid_field", "score_error", "invalid_runtime", "error"}
        profile = expression_profile(expression)
        evolution = meta_strategy(trajectory_metrics, score, profile["nesting_depth"])
        diagnosis = mutation_diagnosis(candidate)
        if not valid:
            action = "repair_candidate_plan"
            reason = "invalid_expression_or_score_runtime"
        elif grade in {"A", "B"}:
            if signed_rank_ic < 0 and already_sign_flipped:
                action = "reject_as_negative_evidence"
                reason = "direction_normalization_failed"
            elif signed_rank_ic < 0:
                action = "mutate_signal_direction"
                reason = "negative_ic_ab_keeper"
            else:
                action = "advance_to_novelty"
                reason = "quick_grade_ab"
        else:
            if score or grade:
                action, reason = _mutation_for_evolution(evolution, diagnosis)
            else:
                action, reason = "reject_as_negative_evidence", "no_quick_signal"
        if profile["nesting_depth"] > 8:
            action = "simplify_expression"
            reason = "nesting_depth_gt_8"
        decision = {
            "idx": idx,
            "candidate_id": candidate.get("candidate_id"),
            "trajectory_id": candidate.get("trajectory_id"),
            "parent_candidate_id": candidate.get("parent_candidate_id"),
            "mutation_summary": candidate.get("mutation_summary"),
            "matched_region_uid": _candidate_region_uid(candidate) or None,
            "expression": expression,
            "score": score,
            "grade": grade,
            "action": action,
            "reason": reason,
            "expression_profile": profile,
            "evolution_strategy": evolution,
            "mutation": diagnosis,
        }
        lane_decisions.append(decision)
        (allowed if action == "advance_to_novelty" else rejected).append(decision)
    top_action = "advance_to_novelty" if allowed else (rejected[0]["action"] if rejected else "request_candidate_plan")
    current_score = _safe_float(complete_trajectory[-1].get("score")) if complete_trajectory else 0.0
    top_evolution = meta_strategy(
        trajectory_metrics,
        current_score,
        expression_profile(complete_trajectory[-1].get("expression"))["nesting_depth"] if complete_trajectory else 0,
    )
    hard_explore_lanes = [
        item
        for item in rejected
        if str((item.get("mutation") or {}).get("strategy") or "") == "regenerate_full"
        and str(item.get("action") or "") == "explore_new_thesis"
    ]
    if rejected and not allowed and len(hard_explore_lanes) == len(rejected):
        # A local hard diagnosis such as score<20 means the current mechanism
        # has no parent value. Historical score gaps must not turn that into
        # RECOMBINE, especially when the historical best later failed novelty.
        top_evolution = {
            "strategy": "explore",
            "action": "explore_new_thesis",
            "reason": "all_current_candidates_require_full_regeneration",
        }
    return {
        "checkpoint": "score_review",
        "action": top_action,
        "strategy": "normal_process" if allowed else "mutation_required",
        "evolution_strategy": top_evolution,
        "trajectory_metrics": trajectory_metrics,
        "recombination_candidates": (
            top_trajectory_segments(complete_trajectory)
            if top_evolution.get("strategy") == "recombine"
            else []
        ),
        "candidate_lane_decisions": lane_decisions,
        "allowed_actions": ["fxalpha_novelty_check", "candidate_decision"] if allowed else ["candidate_plan", "pre_batch_decision"],
        "blocked_actions": ["fxalpha_quality_gate", "fxalpha_import_factors"],
    }


def _novelty_guard(candidate: dict[str, Any]) -> dict[str, Any]:
    return (
        candidate.get("novelty_guard")
        or candidate.get("novelty")
        or (candidate.get("screening") or {}).get("novelty_guard")
        or (candidate.get("deep_validation") or {}).get("novelty_guard")
        or {}
    )


def _combined_guard(candidate: dict[str, Any]) -> dict[str, Any]:
    return (
        candidate.get("combined_guard")
        or (candidate.get("screening") or {}).get("combined_guard")
        or (candidate.get("deep_validation") or {}).get("combined_guard")
        or {}
    )


def _st_exposure_guard(candidate: dict[str, Any]) -> dict[str, Any]:
    return (
        candidate.get("st_exposure_guard")
        or (candidate.get("screening") or {}).get("st_exposure_guard")
        or (candidate.get("deep_validation") or {}).get("st_exposure_guard")
        or {}
    )


def _st_exposure_hard_blocks(st_guard: dict[str, Any]) -> bool:
    mode = str((st_guard or {}).get("mode") or "hard").strip().lower()
    return bool(st_guard) and st_guard.get("passed") is not True and mode != "advisory"


def _novelty_family_key(candidate: dict[str, Any]) -> str:
    guard = _novelty_guard(candidate)
    return str(
        guard.get("matched_region_uid")
        or candidate.get("matched_region_uid")
        or guard.get("matched_information_cluster_id")
        or candidate.get("matched_information_cluster_id")
        or guard.get("matched_existing_factor_id")
        or candidate.get("matched_existing_factor_id")
        or ""
    ).strip()


def novelty_advice(
    candidates: list[dict[str, Any]],
    repeated_same_family: bool = False,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rejected_family_counts: dict[str, int] = {}
    for item in history or []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "")
        if action == "advance_to_deep_validation":
            continue
        family_key = _novelty_family_key(item)
        if family_key:
            rejected_family_counts[family_key] = rejected_family_counts.get(family_key, 0) + 1
    decisions = []
    keepers = []
    for idx, candidate in enumerate(candidates or []):
        guard = _novelty_guard(candidate)
        combined = _combined_guard(candidate)
        st_guard = _st_exposure_guard(candidate)
        thresholds = guard.get("thresholds") or {}
        pearson_threshold = _safe_float(thresholds.get("pearson"), 0.75)
        rank_threshold = _safe_float(thresholds.get("rank_corr"), 0.80)
        p90_pearson_threshold = _safe_float(thresholds.get("p90_pearson"), max(0.0, pearson_threshold - 0.05))
        p90_rank_threshold = _safe_float(thresholds.get("p90_rank_corr"), max(0.0, rank_threshold - 0.05))
        allowed = bool(combined.get("allowed")) if combined else bool(guard.get("allowed"))
        max_pearson = _safe_float(guard.get("max_existing_pearson"))
        max_rank = _safe_float(guard.get("max_existing_rank_corr"))
        p90_pearson = _safe_float(guard.get("p90_pearson"))
        p90_rank = _safe_float(guard.get("p90_rank_corr"))
        family_key = _novelty_family_key(candidate)
        prior_family_rejections = rejected_family_counts.get(family_key, 0) if family_key else 0
        # One earlier formal rejection plus the current rejection is already a
        # cross-round repeat.  Requiring two earlier rejections spent a third
        # novelty attempt inside a region the active library had already
        # explained, which trapped otherwise healthy B candidates in local
        # window/operator mutations.
        family_repeated = bool(repeated_same_family or prior_family_rejections >= 1)
        if guard.get("allowed") is True and _st_exposure_hard_blocks(st_guard):
            action, reason = "reject_st_exposure", str(st_guard.get("reason") or "st_exposure_veto")
        elif allowed:
            action, reason = "advance_to_deep_validation", "novelty_allowed"
        elif family_repeated:
            action, reason = "explore_new_thesis", "repeated_same_family_novelty_veto"
        elif p90_pearson >= p90_pearson_threshold or p90_rank >= p90_rank_threshold:
            action, reason = "orthogonalize_or_switch_source", "family_crowded_p90_threshold"
        elif max_pearson >= pearson_threshold or max_rank >= rank_threshold:
            action, reason = "orthogonalize_or_switch_source", "active_pool_correlation_threshold"
        else:
            action, reason = "keep_best_drop_variants", str(guard.get("reason") or "novelty_not_allowed")
        decision = {
            "idx": idx,
            "candidate_id": candidate.get("candidate_id"),
            "trajectory_id": candidate.get("trajectory_id"),
            "parent_candidate_id": candidate.get("parent_candidate_id"),
            "mutation_summary": candidate.get("mutation_summary"),
            "factor_map_id": guard.get("factor_map_id") or candidate.get("factor_map_id"),
            "factor_map_audit_id": guard.get("factor_map_audit_id") or candidate.get("factor_map_audit_id"),
            "expression": candidate.get("expression", ""),
            "action": action,
            "reason": reason,
            "novelty_score": _safe_float(guard.get("novelty_score")),
            "matched_existing_factor": guard.get("matched_existing_factor"),
            "matched_existing_factor_id": guard.get("matched_existing_factor_id"),
            "matched_reference_source": guard.get("matched_reference_source"),
            "matched_information_cluster_id": guard.get("matched_information_cluster_id"),
            "matched_region_uid": guard.get("matched_region_uid"),
            "novelty_family_key": family_key or None,
            "prior_family_rejections": prior_family_rejections,
            "repeated_same_family": family_repeated,
            "st_exposure_guard": st_guard,
            "combined_guard": combined,
        }
        decisions.append(decision)
        if action == "advance_to_deep_validation":
            keepers.append(decision)
    top_action = "advance_to_deep_validation" if keepers else "orthogonalize_or_switch_source"
    rejected_actions = {
        str(item.get("action") or "")
        for item in decisions
        if str(item.get("action") or "")
    }
    if not keepers and rejected_actions == {"reject_st_exposure"}:
        top_action = "reject_st_exposure"
    elif not keepers and rejected_actions == {"explore_new_thesis"}:
        top_action = "explore_new_thesis"
    if keepers:
        strategy = "normal_process"
    elif top_action == "explore_new_thesis":
        strategy = "explore"
    elif top_action == "orthogonalize_or_switch_source":
        # A first formal novelty veto does not invalidate a Quick A/B
        # candidate's economic mechanism.  Preserve that candidate as a
        # parent and change one information source / confirmation relation.
        strategy = "exploit"
    else:
        strategy = "reject"
    return {
        "checkpoint": "novelty_review",
        "action": top_action,
        "strategy": strategy,
        "candidate_lane_decisions": decisions,
        "allowed_actions": ["run_backtest", "run_anti_overfit", "run_rolling_validation", "run_adversarial_validation"] if keepers else ["candidate_plan", "pre_batch_decision"],
        "blocked_actions": ["fxalpha_quality_gate", "fxalpha_import_factors"],
    }


def ensure_factor_naming(candidate: dict[str, Any], category: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = dict(candidate or {})
    expression = str(updated.get("expression") or "")
    existing_name = str(updated.get("factor_name") or updated.get("name") or (updated.get("metadata") or {}).get("factor_name") or "").strip()
    existing_category_info = updated.get("category_info") or (updated.get("metadata") or {}).get("category_info")
    category_info = existing_category_info if isinstance(existing_category_info, dict) else classify_factor_expression(expression, category)
    proposed_name = existing_name or str(category_info.get("suggested_factor_name") or generate_factor_name(expression, category_info)).strip()
    repair_reason = factor_name_quality_reason(proposed_name, expression)
    factor_name, status = canonical_factor_name(expression, category_info, proposed_name=proposed_name)
    factor_name = " ".join(factor_name.split())[:80]
    if existing_name and existing_category_info and status == "provided":
        status = "provided"
    elif repair_reason:
        status = "repaired"
    elif existing_name:
        status = "repaired" if not existing_category_info else status
    else:
        status = "generated"
    if not factor_name:
        status = "missing_blocked"
    updated["factor_name"] = factor_name
    updated["category_info"] = category_info
    metadata = dict(updated.get("metadata") or {})
    metadata["factor_name"] = factor_name
    metadata["category_info"] = category_info
    updated["metadata"] = metadata
    return updated, {
        "factor_name_status": status,
        "factor_name": factor_name,
        "category_info": category_info,
        "factor_name_repair_reason": repair_reason,
    }


def deep_advice(
    candidates: list[dict[str, Any]],
    trajectory: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prepared_candidates: list[tuple[dict[str, Any], dict[str, Any], float, dict[str, Any]]] = []
    for raw_candidate in candidates or []:
        candidate, naming = ensure_factor_naming(raw_candidate)
        quick_score = quality_gate._extract_quick_score(candidate)
        deep_score, score_parts = quality_gate._compute_deep_score(candidate, quick_score=quick_score)
        prepared_candidates.append((candidate, naming, deep_score, score_parts))
    complete_trajectory = _merge_trajectory(
        trajectory,
        [
            {
                "candidate_id": candidate.get("candidate_id"),
                "trajectory_id": candidate.get("trajectory_id"),
                "round_id": candidate.get("round_id"),
                "parent_candidate_id": candidate.get("parent_candidate_id"),
                "mutation_summary": candidate.get("mutation_summary"),
                "matched_region_uid": _candidate_region_uid(candidate) or None,
                "expression": candidate.get("expression"),
                "score": deep_score,
                "rolling_score": quality_gate._extract_rolling_score(candidate),
            }
            for candidate, _, deep_score, _ in prepared_candidates
        ],
    )
    trajectory_metrics = analyze_trajectory(complete_trajectory)
    decisions = []
    gate_ready = []
    top_recombination_candidates: list[dict[str, Any]] = []
    for idx, (candidate, naming, deep_score, score_parts) in enumerate(prepared_candidates):
        rolling = candidate.get("rolling_validation") or {}
        trailing = rolling.get("trailing_horizons") or {}
        quick_score = quality_gate._extract_quick_score(candidate)
        bs = quality_gate._extract_backtest_summary(candidate)
        missing = quality_gate._missing_deep_components(candidate)
        component_scores = score_parts.get("component_scores") or {}
        comparable = {
            key: value for key, value in component_scores.items()
            if key in {"quick_core", "anti_overfit", "rolling", "adversarial"} and value is not None
        }
        weakest = min(comparable, key=comparable.get) if comparable else "deep_score"
        threshold_checks = quality_gate._threshold_checks(bs, min_abs_ic=0.02, min_ir=0.3)
        profile = expression_profile(candidate.get("expression"))
        progress = analyze_candidate_progress(
            candidate,
            complete_trajectory,
            current_score=deep_score,
            nesting_depth=profile["nesting_depth"],
        )
        evolution = progress["evolution_strategy"]
        if idx == 0:
            top_recombination_candidates = list(progress.get("recombination_candidates") or [])
        action = "submit_quality_gate"
        reason = "deep_score_ready"
        if missing:
            action, reason = "complete_deep_evidence", "missing_deep_components"
        elif threshold_checks["ic_abs"]["passed"] is False:
            action, reason = "mutate_operator", "ic_below_0_02"
        elif threshold_checks["ir_abs"]["passed"] is False:
            action, reason = "mutate_normalization", "icir_below_0_3"
        elif profile["nesting_depth"] > 8:
            action, reason = "simplify_expression", "nesting_depth_gt_8"
        elif deep_score < 80:
            # Compare raw 0-100 component scores.  Comparing weighted points
            # would incorrectly label every 10%-weight component as weakest.
            if evolution["strategy"] == "recombine":
                action, reason = "recombine_from_best", evolution["reason"]
            elif evolution["strategy"] == "explore":
                action, reason = "explore_new_thesis", evolution["reason"]
            elif evolution["strategy"] == "simplify":
                action, reason = "simplify_expression", evolution["reason"]
            else:
                action, reason = "targeted_mutation", f"deep_score_lt_80_lowest_component_{weakest}"
        if naming["factor_name_status"] == "missing_blocked":
            action, reason = "repair_factor_name", "missing_factor_name"
        decision = {
            "idx": idx,
            "candidate_id": candidate.get("candidate_id"),
            "trajectory_id": candidate.get("trajectory_id"),
            "parent_candidate_id": candidate.get("parent_candidate_id"),
            "mutation_summary": candidate.get("mutation_summary"),
            "matched_region_uid": _candidate_region_uid(candidate) or None,
            "factor_name": naming["factor_name"],
            "expression": candidate.get("expression", ""),
            "action": action,
            "reason": reason,
            "evolution_strategy": evolution,
            "trajectory_progress": {
                "scope": progress.get("scope"),
                "attempts": progress.get("attempts"),
                "failed_attempts": progress.get("failed_attempts"),
                "deep_gain": progress.get("deep_gain"),
                "rolling_gain": progress.get("rolling_gain"),
                "meaningful_gain": progress.get("meaningful_gain"),
            },
            "deep_score": deep_score,
            "gap_to_gate": round(max(0.0, 80.0 - float(deep_score)), 1),
            "deep_score_policy_version": score_parts.get("deep_score_policy_version"),
            "quick_score": quick_score,
            "ic": bs.get("ic_mean"),
            "icir": bs.get("ic_ir"),
            "rank_ic": bs.get("rank_ic_mean"),
            "rank_icir": bs.get("rank_ic_ir"),
            "anti_overfit_score": (candidate.get("anti_overfit") or {}).get("score"),
            "rolling_score": rolling.get("score"),
            "rolling_grade": rolling.get("grade"),
            "rolling_policy_version": rolling.get("score_policy_version"),
            "rolling_status": rolling.get("status"),
            "lowest_component": weakest if deep_score < 80 and not missing else None,
            "lowest_component_score": comparable.get(weakest) if deep_score < 80 and not missing and comparable else None,
            "lowest_component_reference_status": (
                "grade_b_or_better"
                if deep_score < 80
                and not missing
                and weakest == "rolling"
                and (
                    str(rolling.get("grade") or "").upper() in {"A", "B"}
                    or float(quality_gate._extract_rolling_score(candidate) or 0.0) >= 70.0
                )
                else "lowest_raw_score_only"
                if deep_score < 80 and not missing
                else None
            ),
            "rolling_6m_ic": (trailing.get("6m") or {}).get("rank_ic"),
            "rolling_12m_ic": (trailing.get("12m") or {}).get("rank_ic"),
            "rolling_24m_ic": (trailing.get("24m") or {}).get("rank_ic"),
            "rolling_48m_ic": (trailing.get("48m") or {}).get("rank_ic"),
            "rolling_weighted_ic": rolling.get("weighted_ic"),
            "rolling_weighted_std": rolling.get("weighted_std"),
            "rolling_robust_ic": rolling.get("robust_ic"),
            "adversarial_score": (candidate.get("adversarial_validation") or {}).get("score"),
            "novelty_score": (candidate.get("novelty_guard") or {}).get("novelty_score"),
            "score_parts": score_parts,
            "missing_components": missing,
            "threshold_checks": threshold_checks,
            "factor_name_status": naming["factor_name_status"],
        }
        decisions.append(decision)
        if action == "submit_quality_gate":
            gate_ready.append(decision)
    return {
        "checkpoint": "deep_validation_review",
        "action": "submit_quality_gate" if gate_ready else (decisions[0]["action"] if decisions else "complete_deep_evidence"),
        "strategy": "normal_process" if gate_ready else "mutation_required",
        "evolution_strategy": (
            decisions[0].get("evolution_strategy")
            if decisions
            else {"strategy": "exploit", "action": "targeted_mutation", "reason": "no_candidates"}
        ),
        "trajectory_metrics": trajectory_metrics,
        "recombination_candidates": (
            top_recombination_candidates
            if decisions and (decisions[0].get("evolution_strategy") or {}).get("strategy") == "recombine"
            else []
        ),
        "candidate_lane_decisions": decisions,
        "allowed_actions": ["fxalpha_quality_gate"] if gate_ready else ["candidate_plan", "pre_batch_decision", "run_anti_overfit", "run_rolling_validation", "run_adversarial_validation"],
        "blocked_actions": [] if gate_ready else ["fxalpha_quality_gate", "fxalpha_import_factors"],
    }


def gate_advice(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = []
    adopted = []
    for idx, candidate in enumerate(candidates or []):
        candidate, naming = ensure_factor_naming(candidate)
        gate = candidate.get("gate_result") or {}
        reason = str(gate.get("reason") or "")
        deep_score = _safe_float(gate.get("deep_score", candidate.get("deep_score")))
        bs = quality_gate._extract_backtest_summary(candidate)
        threshold_checks = quality_gate._threshold_checks(bs, min_abs_ic=0.02, min_ir=0.3)
        if gate.get("passed") or reason == "quality_gate_adopted":
            action, decision_reason = "import", "quality_gate_adopted"
        elif "missing_" in reason or "requires_deep_validation" in reason:
            action, decision_reason = "return_to_deep_validation", reason or "missing_gate_evidence"
        elif "holding_period_mismatch" in reason or "data_abnormal" in reason:
            action, decision_reason = "blocker", reason
        elif deep_score < 80 or not threshold_checks["ic_abs"]["passed"] or not threshold_checks["ir_abs"]["passed"]:
            action, decision_reason = "gate_mismatch_feedback", "business_rejection_should_have_been_caught_by_deep_advice"
        else:
            action, decision_reason = "gate_mismatch_feedback", reason or "quality_gate_rejected"
        entry = {
            "idx": idx,
            "candidate_id": candidate.get("candidate_id"),
            "expression": candidate.get("expression", ""),
            "action": action,
            "reason": decision_reason,
            "gate_result": gate,
            "factor_name_status": naming["factor_name_status"],
            "factor_name": naming["factor_name"],
        }
        decisions.append(entry)
        if action == "import":
            adopted.append(entry)
    return {
        "checkpoint": "import_gate_review",
        "action": "import" if adopted else (decisions[0]["action"] if decisions else "return_to_deep_validation"),
        "strategy": "quality_control",
        "candidate_lane_decisions": decisions,
        "allowed_actions": ["fxalpha_import_factors"] if adopted else ["deep_validation_review", "candidate_plan"],
        "blocked_actions": [] if adopted else ["fxalpha_import_factors"],
    }
