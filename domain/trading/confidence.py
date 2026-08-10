from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


CONFIDENCE_CASH_CONTRACT = "confidence_cash_top20_drop2_hold5_open_v2"
CONFIDENCE_POLICY_VERSION = "confidence_cash_v2"


def is_confidence_cash_contract(strategy_contract_version: str | None) -> bool:
    return str(strategy_contract_version or "").startswith("confidence_cash_")


def default_confidence_policy() -> dict[str, Any]:
    return {
        "version": CONFIDENCE_POLICY_VERSION,
        "execution_version": "target_weight_v2",
        "selection_policy": "strictly_above_tied_topk_boundary",
        "weak_model_tree_threshold": 1,
        "weak_model_multiplier": 0.5,
        "performance_multiplier": 1.0,
        "risk_reduction_overrides_n_drop": True,
    }


def normalize_confidence_policy(value: dict[str, Any] | None) -> dict[str, Any]:
    policy = {**default_confidence_policy(), **dict(value or {})}
    policy["version"] = str(policy.get("version") or CONFIDENCE_POLICY_VERSION)
    policy["execution_version"] = str(policy.get("execution_version") or "target_weight_v2")
    policy["selection_policy"] = str(
        policy.get("selection_policy") or "strictly_above_tied_topk_boundary"
    )
    for key in ("weak_model_multiplier", "performance_multiplier"):
        policy[key] = min(max(float(policy.get(key, 1.0)), 0.0), 1.0)
    policy["weak_model_tree_threshold"] = max(int(policy.get("weak_model_tree_threshold", 1)), 0)
    policy["risk_reduction_overrides_n_drop"] = bool(
        policy.get("risk_reduction_overrides_n_drop", True)
    )
    return policy


def _find_first(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _find_first(value, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_first(value, key)
            if found is not None:
                return found
    return None


def load_model_confidence_evidence(model_context: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(str(model_context.get("recorder_run_dir") or model_context.get("run_dir") or ""))
    evidence: dict[str, Any] = {
        "status": "unavailable",
        "run_dir": str(run_dir) if str(run_dir) != "." else "",
        "model_id": str(model_context.get("model_id") or ""),
        "model_run_id": str(model_context.get("model_run_id") or ""),
    }
    for name in ("training_diagnostics.json", "metrics.json", "manifest.json"):
        path = run_dir / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        trees_built = _find_first(payload, "trees_built")
        best_iteration = _find_first(payload, "best_iteration")
        best_iteration_ratio = _find_first(payload, "best_iteration_ratio")
        early_stopped = _find_first(payload, "early_stopped")
        if trees_built is None and best_iteration is None:
            continue
        evidence.update(
            {
                "status": "available",
                "source_file": str(path),
                "trees_built": int(trees_built) if trees_built is not None else None,
                "best_iteration": int(best_iteration) if best_iteration is not None else None,
                "best_iteration_ratio": (
                    float(best_iteration_ratio) if best_iteration_ratio is not None else None
                ),
                "early_stopped": bool(early_stopped) if early_stopped is not None else None,
            }
        )
        break
    return evidence


def score_boundary_evidence(score_df: pd.DataFrame, *, topk: int) -> dict[str, Any]:
    ranked = score_df.copy()
    ranked["score"] = pd.to_numeric(ranked["score"], errors="coerce")
    ranked = ranked.dropna(subset=["score"])
    ranked = ranked.sort_values(["score", "instrument"], ascending=[False, True]).reset_index(drop=True)
    if ranked.empty:
        return {
            "topk_boundary_score": None,
            "strictly_above_boundary": 0,
            "equal_to_boundary": 0,
            "boundary_tied": False,
            "selected_count": 0,
        }
    boundary_index = min(int(topk), len(ranked)) - 1
    boundary_score = float(ranked.iloc[boundary_index]["score"])
    boundary_tied = bool(
        len(ranked) > int(topk)
        and float(ranked.iloc[int(topk)]["score"]) == boundary_score
    )
    strictly_above = int((ranked["score"] > boundary_score).sum())
    equal_to = int((ranked["score"] == boundary_score).sum())
    selected_count = strictly_above if boundary_tied else min(int(topk), len(ranked))
    return {
        "topk_boundary_score": boundary_score,
        "strictly_above_boundary": strictly_above,
        "equal_to_boundary": equal_to,
        "boundary_tied": boundary_tied,
        "selected_count": selected_count,
    }


def evaluate_confidence(
    *,
    score_quality: dict[str, Any],
    boundary: dict[str, Any],
    topk: int,
    model_evidence: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    evidence_as_of: str = "",
    label_cutoff_date: str = "",
) -> dict[str, Any]:
    resolved = normalize_confidence_policy(policy)
    model_evidence = dict(model_evidence or {"status": "unavailable"})
    reasons: list[str] = []

    trees_built = model_evidence.get("trees_built")
    model_multiplier = 1.0
    model_state = "unavailable"
    if trees_built is not None:
        if int(trees_built) <= int(resolved["weak_model_tree_threshold"]):
            model_multiplier = float(resolved["weak_model_multiplier"])
            model_state = "weak"
            reasons.append(f"model_tree_count_weak:{int(trees_built)}")
        else:
            model_state = "strong"

    record_count = int(score_quality.get("record_count") or 0)
    unique_count = int(score_quality.get("unique_score_count") or 0)
    min_unique = min(max(int(topk) * 3, 20), max(record_count // 20, 1))
    selection_state = "strong"
    if unique_count < min_unique:
        selection_state = "weak"
        reasons.append(f"score_unique_below_reference:{unique_count}<{min_unique}")
    if bool(boundary.get("boundary_tied")):
        selection_state = "weak"
        reasons.append(
            "topk_boundary_tied:"
            f"strict={int(boundary.get('strictly_above_boundary') or 0)},"
            f"equal={int(boundary.get('equal_to_boundary') or 0)}"
        )

    performance_multiplier = float(resolved["performance_multiplier"])
    performance_state = "neutral_unavailable" if performance_multiplier == 1.0 else "risk_reduced"
    if performance_multiplier < 1.0:
        reasons.append(f"performance_multiplier:{performance_multiplier:.4f}")

    exposure_multiplier = min(model_multiplier, performance_multiplier)
    selected_count = int(boundary.get("selected_count") or 0)
    slot_weight = exposure_multiplier / int(topk)
    target_stock_exposure = min(max(slot_weight * selected_count, 0.0), 1.0)
    target_cash_weight = 1.0 - target_stock_exposure
    if exposure_multiplier <= 0 or selected_count <= 0:
        state = "no_trade"
    elif model_state == "weak" or selection_state == "weak" or performance_state == "risk_reduced":
        state = "weak"
    else:
        state = "strong"

    return {
        "confidence_policy_version": resolved["version"],
        "execution_version": resolved["execution_version"],
        "confidence_state": state,
        "exposure_multiplier": exposure_multiplier,
        "model_confidence": {
            "state": model_state,
            "multiplier": model_multiplier,
            "evidence": model_evidence,
        },
        "selection_confidence": {
            "state": selection_state,
            "unique_score_count": unique_count,
            "required_unique_reference": min_unique,
            **boundary,
        },
        "performance_confidence": {
            "state": performance_state,
            "multiplier": performance_multiplier,
            "label_cutoff_date": label_cutoff_date,
            "note": "neutral until enough fully observed live-performance evidence is available",
        },
        "selected_count": selected_count,
        "slot_weight": slot_weight,
        "target_stock_exposure": target_stock_exposure,
        "target_cash_weight": target_cash_weight,
        "evidence_as_of": evidence_as_of,
        "label_cutoff_date": label_cutoff_date,
        "reasons": reasons,
        "policy": resolved,
    }
