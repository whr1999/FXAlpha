from __future__ import annotations

import hashlib
from pathlib import Path
import traceback
from typing import Any

from domain.factor_research.deepseek_client import DeepSeekClientError, DeepSeekJSONClient
from storage.model_registry import ModelRegistry

from .context import build_context_pack, record_orch_trace
from .contracts import (
    MODEL_SYSTEM_VERSION,
    SAMPLE_WEIGHT_POLICIES,
    default_r1_experiment,
    normalize_research_baseline_overrides,
    stable_json,
    validate_experiment_contract,
    utc_now,
)
from .feature_sets import model_feature_set_preflight
from .paths import MODEL_ORCHESTRATOR_EVENTS, MODEL_RESEARCH_STEPS
from .preflight import model_preflight
from .qlib_runner import run_round, submit_experiment
from .research_confirmation import confirm_research_round, register_research_screening_round
from .scoring import improvement_vs_reference, meaningfully_improves, round_research_metrics, score_round
from .state_store import ModelStateStore, append_jsonl


ORCH_PROMPT_PATH = Path(__file__).with_name("ORCH_PROMPT.md")
MCP_PROMPT_PATH = Path(__file__).with_name("MCP_PROMPT.md")
EXPERIMENT_PLAN_BRIEFING = (
    "Diagnose the current session evidence and change one coherent LGBM parameter group from the platform-best round."
)
ROUND_SYNTHESIS_BRIEFING = (
    "Deterministically summarize the completed Seed 42 research round for logs and GUI; no LLM call is made."
)
CHECKPOINT_STOP_POLICY = "three_consecutive_non_improving_rounds"

PARAMETER_GROUPS = {
    "capacity": {"num_leaves", "max_depth", "min_data_in_leaf"},
    "boosting": {"learning_rate", "n_estimators", "early_stopping_rounds"},
    "regularization": {"lambda_l1", "lambda_l2"},
    "feature_sampling": {"feature_fraction"},
}
PARAMETER_BOUNDS = {
    "learning_rate": (0.01, 0.10),
    "num_leaves": (16, 256),
    "max_depth": (4, 12),
    "min_data_in_leaf": (5, 200),
    "feature_fraction": (0.60, 1.00),
    "lambda_l1": (0.0, 300.0),
    "lambda_l2": (0.0, 600.0),
    "n_estimators": (500, 5000),
    "early_stopping_rounds": (30, 300),
}


def model_system_prompt() -> str:
    if ORCH_PROMPT_PATH.exists():
        return ORCH_PROMPT_PATH.read_text(encoding="utf-8")
    return (
        "FXAlpha model research planner. Ordinary rounds run Seed 42; only the session-best round gets Seed 17/83 confirmation."
    )


def model_mcp_prompt() -> str:
    if MCP_PROMPT_PATH.exists():
        return MCP_PROMPT_PATH.read_text(encoding="utf-8")
    return "FXAlpha model MCP prompt. Use model tools in canonical order and preserve state evidence."


def _event(
    *,
    job_id: str,
    event_type: str,
    stage: str,
    status: str = "",
    round_no: int | None = None,
    round_group_id: str = "",
    payload: dict[str, Any] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    record = {
        "schema_version": "model_orchestrator_event_v1",
        "mode": "orch",
        "event_type": event_type,
        "model_system_version": MODEL_SYSTEM_VERSION,
        "job_id": job_id,
        "session_id": session_id,
        "stage": stage,
        "status": status,
        "round_no": round_no,
        "round_group_id": round_group_id,
        "payload": payload or {},
    }
    append_jsonl(MODEL_ORCHESTRATOR_EVENTS, record)
    return record


def _research_step(
    *,
    job_id: str,
    stage: str,
    summary: str,
    decision: str = "",
    next_stage: str = "",
    round_no: int | None = None,
    round_group_id: str = "",
    feature_set_id: str = "",
    refs: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    payload = {
        "schema_version": "research_step_v2",
        "mode": "orch",
        "job_id": job_id,
        "session_id": session_id,
        "stage": stage,
        "summary": summary,
        "decision": decision,
        "next": next_stage,
        "stage_transition": {"next_stage": next_stage, "reason": decision},
        "round_no": round_no,
        "round_group_id": round_group_id,
        "feature_set_id": feature_set_id,
        "evidence_refs": refs or [],
        "extra": extra or {},
    }
    append_jsonl(MODEL_RESEARCH_STEPS, payload)
    return payload


def _blocker(
    *,
    code: str,
    stage: str,
    human_message: str = "",
    repair_action: str = "",
    resume_from: str = "",
    affected_round: str = "",
    category: str | None = None,
) -> dict[str, Any]:
    raw = str(code or human_message or "unknown_blocker")
    if category:
        blocker_category = category
    elif any(token in raw for token in ("active_values", "qlib_data", "service_unavailable", "feature_set_preflight")):
        blocker_category = "external_data_blocker"
    elif any(token in raw for token in ("llm_", "schema", "missing", "invalid")):
        blocker_category = "hard_contract_blocker"
    else:
        blocker_category = "hard_contract_blocker"
    return {
        "code": raw,
        "category": blocker_category,
        "stage": stage,
        "human_message": human_message or raw,
        "repair_action": repair_action,
        "resume_from": resume_from or stage,
        "affected_round": affected_round,
    }


def _coerce_scalar(value: Any) -> Any:
    if isinstance(value, str):
        raw = value.strip()
        lower = raw.lower()
        if lower in {"true", "false"}:
            return lower == "true"
        try:
            if raw and all(ch not in raw for ch in ".eE"):
                return int(raw)
            return float(raw)
        except Exception:
            return value
    return value


LLM_TUNABLE_LGBM_KEYS = {
    "learning_rate",
    "lr",
    "num_leaves",
    "max_depth",
    "min_data_in_leaf",
    "feature_fraction",
    "colsample_bytree",
    "lambda_l1",
    "lambda_l2",
    "n_estimators",
    "early_stopping_rounds",
}

CORE_EXPERIMENT_PARAM_KEYS = (
    "learning_rate",
    "lr",
    "num_leaves",
    "max_depth",
    "min_data_in_leaf",
    "feature_fraction",
    "colsample_bytree",
    "subsample",
    "lambda_l1",
    "lambda_l2",
    "n_estimators",
    "early_stopping_rounds",
)


def _core_experiment_params(experiment: dict[str, Any]) -> dict[str, Any]:
    params = experiment.get("qlib_model_kwargs") if isinstance(experiment.get("qlib_model_kwargs"), dict) else {}
    training = experiment.get("training_hyperparameters") if isinstance(experiment.get("training_hyperparameters"), dict) else {}
    merged = dict(training)
    merged.update(params)
    return {key: merged.get(key) for key in CORE_EXPERIMENT_PARAM_KEYS if key in merged}


def _core_experiment_fingerprint(experiment: dict[str, Any]) -> str:
    payload = {
        "feature_missing_strategy": experiment.get("feature_missing_strategy"),
        "core_lgbm_params": _core_experiment_params(experiment),
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()[:16]


class ExperimentPlanCorrectionError(DeepSeekClientError):
    """A retryable plan error that preserves the rejected plan for DeepSeek."""

    def __init__(self, message: str, *, plan_rejection: dict[str, Any]):
        super().__init__(message)
        self.plan_rejection = plan_rejection


def _experiment_plan_correction_context(
    *,
    error: str,
    plan_rejection: dict[str, Any] | None,
) -> dict[str, Any]:
    if not plan_rejection:
        return {
            "message": "上一份计划未通过服务端校验。请重新输出完整 JSON，不要停止或 checkpoint。",
            "error": error,
        }

    conflicting_round = plan_rejection.get("conflicting_round") if isinstance(plan_rejection.get("conflicting_round"), dict) else {}
    prior_round_value = conflicting_round.get("round_no")
    prior_round = prior_round_value if prior_round_value is not None else "unknown"
    correction = {
        "message": (
            f"你刚才提交的 LGBM 参数与已完成的第 {prior_round} 轮完全相同，不是新的实验。"
            "请根据当前会话证据重新设计一个有明确假设的参数组合。可以只改必要参数，但不能再次输出相同组合。"
        ),
        "conflicting_round": conflicting_round,
    }
    if isinstance(plan_rejection.get("rejected_parameter_changes"), list):
        correction["rejected_parameter_changes"] = plan_rejection["rejected_parameter_changes"]
    return correction


def _sanitize_llm_model_kwargs(params: Any, *, field: str, warnings: list[str]) -> Any:
    if not isinstance(params, dict):
        return params
    default_params = default_r1_experiment().get(field)
    if not isinstance(default_params, dict):
        default_params = {}
    sanitized: dict[str, Any] = {}
    for key, value in params.items():
        if key in LLM_TUNABLE_LGBM_KEYS:
            sanitized[key] = value
        else:
            if key not in default_params or default_params.get(key) != value:
                warnings.append(f"fixed_model_kwarg_ignored:{field}.{key}")
    return sanitized


def _normalize_llm_experiment(experiment: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    raw_normalized: dict[str, Any] = {}
    warnings: list[str] = []
    for key, value in (experiment or {}).items():
        if isinstance(value, dict):
            raw_normalized[key] = {sub_key: _coerce_scalar(sub_value) for sub_key, sub_value in value.items()}
        else:
            raw_normalized[key] = _coerce_scalar(value)
    fixed_fields = {
        "portfolio",
        "benchmark",
        "deal_price",
        "limit_threshold",
        "forbid_all_trade_at_limit",
        "pre_shift_pred",
        "segments",
        "qlib_processors",
        "label_forward_period",
        "factor_holding_period_days",
        "label_execution_deal_price",
        "sample_weight_policy",
        "sample_weight_kwargs",
    }
    for key in sorted(fixed_fields.intersection(raw_normalized)):
        warnings.append(f"fixed_contract_field_ignored:{key}")
    normalized = default_r1_experiment()
    for key in (
        "baseline_kind",
        "feature_missing_strategy",
        "qlib_model_kwargs",
        "training_hyperparameters",
        "strict_r1_baseline",
    ):
        if key in raw_normalized:
            normalized[key] = raw_normalized[key]
    if "sample_weight_policy" in raw_normalized:
        normalized["sample_weight_policy"] = raw_normalized["sample_weight_policy"]
    if "sample_weight_kwargs" in raw_normalized:
        normalized["sample_weight_kwargs"] = raw_normalized["sample_weight_kwargs"]
    if "qlib_model_kwargs" in raw_normalized:
        normalized["qlib_model_kwargs"] = _sanitize_llm_model_kwargs(normalized.get("qlib_model_kwargs"), field="qlib_model_kwargs", warnings=warnings)
    if "training_hyperparameters" in raw_normalized:
        normalized["training_hyperparameters"] = _sanitize_llm_model_kwargs(normalized.get("training_hyperparameters"), field="training_hyperparameters", warnings=warnings)
    if "sample_weight_policy" in raw_normalized and raw_normalized.get("sample_weight_policy") not in SAMPLE_WEIGHT_POLICIES:
        # Do not repair illegal sample-weight policy; validate_experiment_contract will block it.
        return normalized, warnings
    contract = validate_experiment_contract(normalized)
    if contract.get("passed"):
        normalized = contract["normalized"]
    return normalized, warnings


def _compact_score_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "seed": row.get("seed"),
            "model_run_id": row.get("model_run_id"),
            "research_score": row.get("research_score"),
            "decision": row.get("decision"),
        }
        for row in results
    ]


def _round_metrics_for_shadow(round_no: int) -> dict[str, dict[str, float]]:
    profiles = [
        {
            "42": {"annualized_ret": 0.30, "excess_annualized_ret_with_cost": 0.30, "excess_information_ratio_with_cost": 1.10, "max_drawdown": -0.13, "rank_ic": 0.022, "rank_icir": 0.16},
            "17": {"annualized_ret": 0.18, "excess_annualized_ret_with_cost": 0.18, "excess_information_ratio_with_cost": 0.75, "max_drawdown": -0.16, "rank_ic": 0.016, "rank_icir": 0.10},
            "83": {"annualized_ret": -0.08, "excess_annualized_ret_with_cost": -0.08, "excess_information_ratio_with_cost": -0.15, "max_drawdown": -0.24, "rank_ic": -0.004, "rank_icir": -0.02},
        },
        {
            "42": {"annualized_ret": 0.42, "excess_annualized_ret_with_cost": 0.42, "excess_information_ratio_with_cost": 1.55, "max_drawdown": -0.12, "rank_ic": 0.028, "rank_icir": 0.20},
            "17": {"annualized_ret": 0.35, "excess_annualized_ret_with_cost": 0.35, "excess_information_ratio_with_cost": 1.30, "max_drawdown": -0.14, "rank_ic": 0.024, "rank_icir": 0.16},
            "83": {"annualized_ret": 0.21, "excess_annualized_ret_with_cost": 0.21, "excess_information_ratio_with_cost": 0.85, "max_drawdown": -0.18, "rank_ic": 0.018, "rank_icir": 0.11},
        },
        {
            "42": {"annualized_ret": 0.36, "excess_annualized_ret_with_cost": 0.36, "excess_information_ratio_with_cost": 1.35, "max_drawdown": -0.15, "rank_ic": 0.026, "rank_icir": 0.18},
            "17": {"annualized_ret": 0.31, "excess_annualized_ret_with_cost": 0.31, "excess_information_ratio_with_cost": 1.10, "max_drawdown": -0.16, "rank_ic": 0.022, "rank_icir": 0.14},
            "83": {"annualized_ret": 0.28, "excess_annualized_ret_with_cost": 0.28, "excess_information_ratio_with_cost": 1.00, "max_drawdown": -0.17, "rank_ic": 0.020, "rank_icir": 0.13},
        },
    ]
    return profiles[min(max(round_no - 1, 0), len(profiles) - 1)]


def _experiment_plan_payload(
    *,
    round_no: int,
    tuning_state: dict[str, Any],
    round_roles: dict[str, Any],
    completed_rounds: list[dict[str, Any]],
    correction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "task": "model_experiment_plan",
        "stage": "experiment_plan",
        "round_no": round_no,
        "tuning_state": tuning_state,
        "round_roles": round_roles,
        "completed_rounds": completed_rounds,
    }
    if correction:
        payload["correction"] = correction
    return payload


def _strip_llm_private_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if not str(key).startswith("_orchestrator_llm_")}


PLANNER_OUTPUT_LGBM_KEYS = (
    "learning_rate",
    "num_leaves",
    "max_depth",
    "min_data_in_leaf",
    "feature_fraction",
    "lambda_l1",
    "lambda_l2",
    "n_estimators",
    "early_stopping_rounds",
)


def _retryable_experiment_plan_error(error: Exception) -> bool:
    message = str(error)
    retryable_prefixes = (
        "llm_duplicate_core_experiment_parameters:",
        "llm_parameter_",
        "llm_hypothesis_missing",
        "llm_hypothesis_not_string",
        "llm_evidence_interpretation_missing",
        "llm_evidence_interpretation_not_string",
        "llm_risks_to_watch_not_string_list",
        "llm_next_move_invalid:",
        "llm_output_unknown_fields:",
        "llm_stage_mismatch:",
        "llm_decision_mismatch:",
    )
    return any(message.startswith(prefix) for prefix in retryable_prefixes)


def _tunable_parameters(experiment: dict[str, Any]) -> dict[str, Any]:
    params = dict(experiment.get("qlib_model_kwargs") or experiment.get("training_hyperparameters") or {})
    return {key: params.get(key) for key in PLANNER_OUTPUT_LGBM_KEYS}


def _parameter_group(parameters: list[str]) -> str:
    matches = [name for name, members in PARAMETER_GROUPS.items() if set(parameters).issubset(members)]
    if len(matches) != 1:
        raise DeepSeekClientError("llm_parameter_changes_must_use_one_group")
    return matches[0]


def _experiment_from_parameter_changes(
    reference_experiment: dict[str, Any],
    changes: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    if not 1 <= len(changes) <= 3:
        raise DeepSeekClientError("llm_parameter_changes_count_must_be_1_to_3")
    reference = _tunable_parameters(reference_experiment)
    normalized_changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in changes:
        if not isinstance(raw, dict):
            raise DeepSeekClientError("llm_parameter_change_not_object")
        parameter = str(raw.get("parameter") or "")
        if parameter not in PLANNER_OUTPUT_LGBM_KEYS or parameter in seen:
            raise DeepSeekClientError(f"llm_parameter_change_invalid:{parameter}")
        seen.add(parameter)
        old_value = _coerce_scalar(raw.get("from"))
        new_value = _coerce_scalar(raw.get("to"))
        if old_value != reference.get(parameter):
            raise DeepSeekClientError(f"llm_parameter_change_from_mismatch:{parameter}:{old_value}!={reference.get(parameter)}")
        lower, upper = PARAMETER_BOUNDS[parameter]
        try:
            numeric = float(new_value)
        except (TypeError, ValueError) as exc:
            raise DeepSeekClientError(f"llm_parameter_change_not_numeric:{parameter}") from exc
        if not lower <= numeric <= upper:
            raise DeepSeekClientError(f"llm_parameter_change_out_of_bounds:{parameter}:{numeric}")
        if new_value == old_value:
            raise DeepSeekClientError(f"llm_parameter_change_noop:{parameter}")
        if parameter in {"num_leaves", "max_depth", "min_data_in_leaf", "n_estimators", "early_stopping_rounds"}:
            new_value = int(numeric)
        else:
            new_value = numeric
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise DeepSeekClientError(f"llm_parameter_change_reason_missing:{parameter}")
        normalized_changes.append({"parameter": parameter, "from": old_value, "to": new_value, "reason": reason})
    group = _parameter_group([row["parameter"] for row in normalized_changes])
    merged = dict(reference)
    for row in normalized_changes:
        merged[row["parameter"]] = row["to"]
    if int(merged["num_leaves"]) > 2 ** int(merged["max_depth"]):
        raise DeepSeekClientError("llm_parameter_relation_num_leaves_exceeds_depth_capacity")
    if int(merged["early_stopping_rounds"]) >= int(merged["n_estimators"]):
        raise DeepSeekClientError("llm_parameter_relation_early_stop_not_below_estimators")
    experiment = dict(reference_experiment)
    # A new parameter experiment inherits the best round's contract and model
    # settings, never its persisted identity. Keeping round_group_id would make
    # submit_experiment silently reuse the reference round instead of creating
    # a distinct candidate.
    experiment.pop("round_group_id", None)
    experiment.pop("feature_set_id", None)
    experiment.pop("seed_set", None)
    experiment.pop("seed_policy", None)
    model_kwargs = dict(experiment.get("qlib_model_kwargs") or {})
    training = dict(experiment.get("training_hyperparameters") or {})
    model_kwargs.update(merged)
    model_kwargs["lr"] = model_kwargs["learning_rate"]
    training.update(model_kwargs)
    experiment.update(
        {
            "baseline_kind": "deepseek_lgbm_experiment_v2",
            "qlib_model_kwargs": model_kwargs,
            "training_hyperparameters": training,
        }
    )
    return experiment, normalized_changes, group


def _require_deepseek_experiment_plan_v2(
    *,
    client: DeepSeekJSONClient,
    system_prompt: str,
    payload: dict[str, Any],
    requested_feature_set_id: str,
    reference_experiment: dict[str, Any],
    prior_history: list[dict[str, Any]],
) -> dict[str, Any]:
    result = client.complete_json(system=system_prompt, payload=payload, temperature=0.12, max_tokens=1800)
    parsed = _strip_llm_private_fields(result)
    allowed_fields = {
        "stage",
        "decision",
        "evidence_interpretation",
        "next_move",
        "hypothesis",
        "parameter_changes",
        "risks_to_watch",
    }
    unknown_fields = sorted(set(parsed) - allowed_fields)
    if unknown_fields:
        raise DeepSeekClientError(f"llm_output_unknown_fields:{','.join(unknown_fields)}")
    if str(parsed.get("stage") or "") != "experiment_plan":
        raise DeepSeekClientError(f"llm_stage_mismatch:{parsed.get('stage')}")
    decision = str(parsed.get("decision") or "")
    if decision != "submit_experiment":
        raise DeepSeekClientError(f"llm_decision_mismatch:{decision}")
    if "hypothesis" not in parsed or parsed.get("hypothesis") is None:
        raise DeepSeekClientError("llm_hypothesis_missing")
    if not isinstance(parsed.get("hypothesis"), str):
        raise DeepSeekClientError("llm_hypothesis_not_string")
    if "evidence_interpretation" not in parsed or parsed.get("evidence_interpretation") is None:
        raise DeepSeekClientError("llm_evidence_interpretation_missing")
    if not isinstance(parsed.get("evidence_interpretation"), str):
        raise DeepSeekClientError("llm_evidence_interpretation_not_string")
    if "risks_to_watch" in parsed and not (
        isinstance(parsed.get("risks_to_watch"), list)
        and all(isinstance(item, str) for item in parsed.get("risks_to_watch") or [])
    ):
        raise DeepSeekClientError("llm_risks_to_watch_not_string_list")
    hypothesis = parsed["hypothesis"].strip()
    evidence = parsed["evidence_interpretation"].strip()
    next_move = str(parsed.get("next_move") or "")
    if not hypothesis:
        raise DeepSeekClientError("llm_hypothesis_missing")
    if not evidence:
        raise DeepSeekClientError("llm_evidence_interpretation_missing")
    if next_move not in {"explore", "converge", "simplify", "regularize", "capacity_expand", "robustness_retest"}:
        raise DeepSeekClientError(f"llm_next_move_invalid:{next_move}")
    experiment, changes, group = _experiment_from_parameter_changes(
        reference_experiment,
        parsed.get("parameter_changes") if isinstance(parsed.get("parameter_changes"), list) else [],
    )
    experiment["research_metadata"] = {
        "reference_round_group_id": (payload.get("tuning_state") or {}).get("best_round_group_id"),
        "hypothesis": hypothesis,
        "evidence_interpretation": evidence,
        "next_move": next_move,
        "parameter_group": group,
        "parameter_changes": changes,
        "risks_to_watch": parsed.get("risks_to_watch") if isinstance(parsed.get("risks_to_watch"), list) else [],
    }
    contract = validate_experiment_contract(experiment)
    if not contract.get("passed"):
        raise DeepSeekClientError("llm_experiment_contract_failed:" + ",".join(contract.get("errors") or []))
    normalized = contract["normalized"]
    fingerprint = _core_experiment_fingerprint(normalized)
    for prior in prior_history:
        if str(prior.get("core_experiment_fingerprint") or "") == fingerprint:
            raise ExperimentPlanCorrectionError(
                f"llm_duplicate_core_experiment_parameters:matches_round_{prior.get('round_no')}:{fingerprint}",
                plan_rejection={
                    "code": "duplicate_core_experiment_parameters",
                    "conflicting_round": prior,
                    "rejected_parameter_changes": changes,
                },
            )
    return {
        **parsed,
        "summary": hypothesis,
        "next": "train_backtest_seed42",
        "feature_set_id": requested_feature_set_id,
        "experiment_hypothesis": hypothesis,
        "parameter_search_strategy": next_move,
        "parameter_change_rationale": changes,
        "changed_knobs": [row["parameter"] for row in changes],
        "parameter_changes": changes,
        "parameter_group": group,
        "risks_to_watch": parsed.get("risks_to_watch") if isinstance(parsed.get("risks_to_watch"), list) else [],
        "experiment_json": normalized,
        "core_experiment_fingerprint": fingerprint,
        "core_experiment_params": _core_experiment_params(normalized),
        "planner_mode": "deepseek",
        "llm_call_status": "called",
        "llm_model": result.get("_orchestrator_llm_model"),
        "llm_provider_model": result.get("_orchestrator_llm_provider_model"),
        "llm_usage": result.get("_orchestrator_llm_usage"),
        "schema_warnings": contract.get("warnings") or [],
    }


def _round0_experiment_plan(*, feature_set_id: str, baseline_model_params: dict[str, Any] | None = None) -> dict[str, Any]:
    validation = normalize_research_baseline_overrides(baseline_model_params)
    if not validation["passed"]:
        raise ValueError("invalid_baseline_model_params:" + ",".join(validation["errors"]))
    normalized_overrides = dict(validation["normalized"])
    model_params = dict(default_r1_experiment()["qlib_model_kwargs"])
    model_params.update(normalized_overrides)
    experiment = default_r1_experiment(
        {
            "baseline_kind": "model_orch_round0_custom_baseline" if normalized_overrides else "model_orch_round0_baseline",
            "qlib_model_kwargs": model_params,
            "training_hyperparameters": dict(model_params),
            "research_metadata": {
                "baseline_parameter_source": "operator_override" if normalized_overrides else "platform_default",
                "baseline_model_params": normalized_overrides,
            },
        }
    )
    return {
        "stage": "experiment_plan",
        "decision": "submit_experiment",
        "summary": (
            "Round 0 baseline uses operator-configured Qlib parameters without an LLM call."
            if normalized_overrides
            else "Round 0 baseline uses the model default configuration without an LLM call."
        ),
        "next": "train_backtest_seed42",
        "feature_set_id": feature_set_id,
        "next_move": "baseline",
        "experiment_json": experiment,
        "core_experiment_fingerprint": _core_experiment_fingerprint(experiment),
        "core_experiment_params": _core_experiment_params(experiment),
        "nearest_prior_parameter_diff": {},
        "planner_mode": "system_round0_baseline",
        "llm_call_status": "not_called",
        "evidence_refs": ["model_operator_baseline_overrides" if normalized_overrides else "model_default_contract"],
    }


def _compact_completed_rounds_for_llm(completed_rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = {
        "round_group_id",
        "round_no",
        "round_kind",
        "reference_round_group_id",
        "hypothesis",
        "parameter_changes",
        "parameters",
        "seed_results",
        "round_metrics",
        "improvement_vs_reference",
        "improved_platform_best",
    }
    return [{key: row.get(key) for key in allowed if key in row} for row in completed_rounds]


def _deterministic_round_synthesis(round_record: dict[str, Any], *, consecutive_no_improvement: int) -> dict[str, Any]:
    improved = bool(round_record.get("improved_platform_best"))
    delta = (round_record.get("improvement_vs_reference") or {}).get("research_score")
    if not round_record.get("reference_round_group_id"):
        summary = "Seed 42 研究评分已完成确定性汇总；该轮作为当前会话比较基准。"
    elif improved and delta is not None:
        summary = f"本轮 Seed 42 研究评分较参考轮提升 {delta:.3f}，更新为会话最优轮。"
    else:
        summary = f"本轮未达到研究评分至少提升 1.0 的门槛；连续未改善 {consecutive_no_improvement} 轮。"
    decision = "checkpoint_stop" if consecutive_no_improvement >= 3 else "continue"
    return {
        "stage": "round_synthesis",
        "decision": decision,
        "summary": summary,
        "next": "human_review" if decision == "checkpoint_stop" else "experiment_plan",
        "round_group_id": round_record.get("round_group_id"),
        "reference_round_group_id": round_record.get("reference_round_group_id"),
        "improved_platform_best": improved,
        "consecutive_no_improvement": consecutive_no_improvement,
        "round_metrics": round_record.get("round_metrics") or {},
        "improvement_vs_reference": round_record.get("improvement_vs_reference") or {},
        "parameter_lessons": [summary],
        "evidence_refs": [str(round_record.get("round_group_id") or "")],
        "planner_mode": "deterministic_platform_summary",
        "llm_call_status": "not_called",
    }


def _mark_round_checkpoint(state: ModelStateStore, round_group_id: str, stage: str) -> None:
    round_payload = state.get_round(round_group_id) or {}
    if not round_payload:
        return
    round_payload["status"] = "interrupted"
    round_payload["stage"] = stage
    round_payload["updated_at"] = utc_now()
    state.upsert_round(round_payload)


def run_round_synthesis(
    *,
    round_group_id: str,
    round_no: int = 1,
    job_id: str | None = None,
    write_registry: bool = False,
    state: ModelStateStore | None = None,
    client: DeepSeekJSONClient | None = None,
) -> dict[str, Any]:
    state = state or ModelStateStore()
    round_payload = state.get_round(round_group_id)
    if not round_payload:
        return {"ok": False, "err": "round_group_not_found", "round_group_id": round_group_id}
    seed_runs = state.list_seed_runs(round_group_id=round_group_id)
    if not seed_runs:
        return {"ok": False, "err": "seed_runs_missing", "round_group_id": round_group_id}
    if any(not (row.get("score") or {}).get("score_review_version") for row in seed_runs):
        return {"ok": False, "err": "score_review_required_before_round_synthesis", "round_group_id": round_group_id}

    resolved_job_id = job_id or f"model_orch_synthesis_{round_group_id}"
    registry = ModelRegistry() if write_registry else ModelRegistry(state.runtime_root / "orchestrator_shadow_registry.db")
    system_prompt = model_system_prompt()
    feature_set_id = str(round_payload.get("feature_set_id") or "")
    state.upsert_job(
        resolved_job_id,
        status="running",
        stage="round_synthesis",
        mode="orch",
        current_round_group_id=round_group_id,
        payload={"round_group_id": round_group_id, "write_registry": write_registry},
    )
    _event(
        job_id=resolved_job_id,
        event_type="stage_start",
        stage="registry_write",
        status="running",
        round_no=round_no,
        round_group_id=round_group_id,
        payload={"registry": "production" if write_registry else "shadow"},
    )
    gate_result = (
        register_research_screening_round(round_group_id, state=state, registry=registry)
        if write_registry
        else {"ok": True, "asset_status": "research", "registry_write": "skipped_shadow"}
    )
    if not gate_result.get("ok"):
        state.upsert_job(resolved_job_id, status="failed", stage="blocker", mode="orch", current_round_group_id=round_group_id, payload={"error": gate_result})
        _event(job_id=resolved_job_id, event_type="failed", stage="blocker", status="failed", round_no=round_no, round_group_id=round_group_id, payload={"error": gate_result})
        return {"ok": False, "err": "research_registry_write_failed", "round_group_id": round_group_id, "gate_result": gate_result}
    score_summary = _compact_score_results([row.get("score") or {} for row in state.list_seed_runs(round_group_id=round_group_id)])
    gate_summary = [gate_result]
    _event(
        job_id=resolved_job_id,
        event_type="stage_complete",
        stage="registry_write",
        status="completed",
        round_no=round_no,
        round_group_id=round_group_id,
        payload={"results": gate_summary, "registry": "production" if write_registry else "shadow"},
    )

    _event(job_id=resolved_job_id, event_type="stage_start", stage="round_synthesis", status="running", round_no=round_no, round_group_id=round_group_id)
    synthesis_context = build_context_pack(stage="round_synthesis", round_group_id=round_group_id, state=state, include_registry=False)
    round_metrics = round_research_metrics(state.list_seed_runs(round_group_id=round_group_id))
    synthesis = _deterministic_round_synthesis(
        {
            "round_no": round_no,
            "round_group_id": round_group_id,
            "round_metrics": round_metrics,
            "improvement_vs_reference": {},
            "improved_platform_best": False,
        },
        consecutive_no_improvement=0,
    )
    record_orch_trace(
        "round_synthesis",
        system_prompt=system_prompt,
        stage_briefing=ROUND_SYNTHESIS_BRIEFING,
        context_pack=synthesis_context,
        job_id=resolved_job_id,
        round_no=round_no,
        round_group_id=round_group_id,
        parsed_response=synthesis,
        result_summary={
            "round_no": round_no,
            "research_count": sum(1 for row in gate_summary if row.get("asset_status") == "research"),
            "planner_mode": "deterministic_platform_summary",
            "llm_call_status": "not_called",
        },
    )
    _research_step(
        job_id=resolved_job_id,
        stage="round_synthesis",
        summary=str(synthesis.get("summary") or ""),
        decision=str(synthesis.get("decision") or ""),
        next_stage=str(synthesis.get("next") or ""),
        round_no=round_no,
        round_group_id=round_group_id,
        feature_set_id=feature_set_id,
        refs=synthesis.get("evidence_refs") if isinstance(synthesis.get("evidence_refs"), list) else [],
        extra=synthesis,
    )
    round_payload = state.get_round(round_group_id) or round_payload
    round_payload["stage"] = "round_synthesis"
    synthesis_decision = str(synthesis.get("decision") or "")
    round_payload["status"] = "completed" if synthesis_decision != "blocked" else "interrupted"
    round_payload["updated_at"] = utc_now()
    state.upsert_round(round_payload)
    job_status = "interrupted" if synthesis_decision == "blocked" else "completed"
    job_stage = "blocker" if synthesis_decision == "blocked" else "round_synthesis"
    job = state.upsert_job(
        resolved_job_id,
        status=job_status,
        stage=job_stage,
        mode="orch",
        current_round_group_id=round_group_id,
        payload={
            "round_group_id": round_group_id,
            "feature_set_id": feature_set_id,
            "score_summary": score_summary,
            "gate_summary": gate_summary,
            "round_synthesis": synthesis,
            "write_registry": write_registry,
        },
    )
    _event(job_id=resolved_job_id, event_type="stage_complete", stage="round_synthesis", status="completed", round_no=round_no, round_group_id=round_group_id, payload=synthesis)
    _event(job_id=resolved_job_id, event_type="complete", stage="round_synthesis", status=job_status, round_no=round_no, round_group_id=round_group_id, payload={"decision": synthesis.get("decision"), "next": synthesis.get("next")})
    return {
        "ok": True,
        "job": job,
        "round_group_id": round_group_id,
        "score_summary": score_summary,
        "gate_summary": gate_summary,
        "round_synthesis": synthesis,
        "registry_target": "production" if write_registry else "shadow",
    }


def _stage_start(state: ModelStateStore, job_id: str, stage: str, *, round_no: int | None = None, round_group_id: str = "", payload: dict[str, Any] | None = None) -> None:
    job = state.get_job(job_id) or {}
    session_id = str((payload or {}).get("session_id") or (job.get("payload") or {}).get("session_id") or "")
    merged_payload = {**(job.get("payload") or {}), **(payload or {})}
    state.upsert_job(
        job_id,
        status="running",
        stage=stage,
        mode="orch",
        current_round_group_id=round_group_id or job.get("current_round_group_id", ""),
        payload=merged_payload,
    )
    _sync_session_progress(state, session_id, job_id=job_id, stage=stage, round_no=round_no, round_group_id=round_group_id, status="running", payload=merged_payload)
    _event(job_id=job_id, session_id=session_id, event_type="stage_start", stage=stage, status="running", round_no=round_no, round_group_id=round_group_id, payload=payload)


def _stage_complete(state: ModelStateStore, job_id: str, stage: str, *, round_no: int | None = None, round_group_id: str = "", payload: dict[str, Any] | None = None) -> None:
    job = state.get_job(job_id) or {}
    session_id = str((payload or {}).get("session_id") or (job.get("payload") or {}).get("session_id") or "")
    merged_payload = {**(job.get("payload") or {}), "last_stage_result": payload or {}}
    for key in ("best_round_group_id", "consecutive_no_improvement"):
        if payload and key in payload:
            merged_payload[key] = payload[key]
    state.upsert_job(
        job_id,
        status="running",
        stage=stage,
        mode="orch",
        current_round_group_id=round_group_id or job.get("current_round_group_id", ""),
        payload=merged_payload,
    )
    _sync_session_progress(state, session_id, job_id=job_id, stage=stage, round_no=round_no, round_group_id=round_group_id, status="running", payload=merged_payload)
    _event(job_id=job_id, session_id=session_id, event_type="stage_complete", stage=stage, status="completed", round_no=round_no, round_group_id=round_group_id, payload=payload)


def _sync_session_progress(
    state: ModelStateStore,
    session_id: str,
    *,
    job_id: str,
    stage: str,
    round_no: int | None = None,
    round_group_id: str = "",
    status: str = "running",
    payload: dict[str, Any] | None = None,
) -> None:
    if not session_id:
        return
    current = state.get_session(session_id) or {}
    current_payload = dict(current.get("payload") or {})
    if payload:
        current_payload.update(payload)
    current_payload["current_round_no"] = round_no
    current_payload["current_round_group_id"] = round_group_id or current_payload.get("current_round_group_id", "")
    round_ids = list(current.get("round_group_ids") or [])
    if round_group_id and round_group_id not in round_ids:
        round_ids.append(round_group_id)
    model_run_ids = list(current.get("model_run_ids") or [])
    for seed in state.list_seed_runs(round_group_id=round_group_id) if round_group_id else []:
        model_run_id = seed.get("model_run_id")
        if model_run_id and model_run_id not in model_run_ids:
            model_run_ids.append(model_run_id)
    state.upsert_session(
        {
            "session_id": session_id,
            "status": status or current.get("status") or "running",
            "mode": current.get("mode") or "orch",
            "feature_set_id": current.get("feature_set_id") or current_payload.get("feature_set_id") or "",
            "n_rounds_requested": current.get("n_rounds_requested") or current_payload.get("n_rounds") or 0,
            "n_rounds_completed": current.get("n_rounds_completed") or 0,
            "active_job_id": job_id,
            "parent_job_id": current.get("parent_job_id") or current_payload.get("parent_job_id") or "",
            "current_stage": stage,
            "current_blocker": current.get("current_blocker") or {},
            "round_group_ids": round_ids,
            "model_run_ids": model_run_ids,
            "blocker_history": current.get("blocker_history") or [],
            "payload": current_payload,
        }
    )


def orchestrator_start(
    *,
    feature_set_id: str | None = None,
    n_rounds: int = 1,
    max_stage: str = "round_synthesis",
    run_id: str | None = None,
    session_id: str | None = None,
    parent_job_id: str | None = None,
    execute_qlib: bool = False,
    write_registry: bool = False,
    baseline_model_params: dict[str, Any] | None = None,
    resume: bool = False,
    state: ModelStateStore | None = None,
    client: DeepSeekJSONClient | None = None,
) -> dict[str, Any]:
    state = state or ModelStateStore()
    client = client or DeepSeekJSONClient()
    tuning_rounds = max(0, int(n_rounds))
    total_rounds = tuning_rounds + 1
    job_id = run_id or f"model_orch_{utc_now().replace(':', '').replace('-', '')}"
    session_id = session_id or parent_job_id or f"msession_{utc_now().replace(':', '').replace('-', '')}"
    shadow_registry = None if write_registry else ModelRegistry(state.runtime_root / "orchestrator_shadow_registry.db")
    baseline_validation = normalize_research_baseline_overrides(baseline_model_params)
    if not baseline_validation["passed"]:
        return {
            "ok": False,
            "err": "invalid_baseline_model_params",
            "errors": baseline_validation["errors"],
        }
    resolved_baseline_model_params = dict(baseline_validation["normalized"])
    existing_session = state.get_session(session_id) if resume else None
    existing_payload = dict((existing_session or {}).get("payload") or {})
    resumed_rounds = list(existing_payload.get("completed_rounds") or [])
    resumed_round_ids = [str(row.get("round_group_id") or "") for row in resumed_rounds if row.get("round_group_id")]
    if resume:
        state.upsert_job(
            job_id,
            status="running",
            stage="protocol_load",
            mode="orch",
            payload={"cancel_requested": False, "resumed_at": utc_now()},
        )
    state.upsert_session(
        {
            "session_id": session_id,
            "status": "running",
            "mode": "orch",
            "feature_set_id": feature_set_id or "",
            "n_rounds_requested": total_rounds,
            "n_rounds_completed": len(resumed_rounds),
            "active_job_id": job_id,
            "parent_job_id": parent_job_id or "",
            "current_stage": "protocol_load",
            "round_group_ids": resumed_round_ids,
            "model_run_ids": (existing_session or {}).get("model_run_ids") or [],
            "payload": {**existing_payload, "execute_qlib": execute_qlib, "write_registry": write_registry, "max_stage": max_stage, "baseline_model_params": resolved_baseline_model_params, "checkpoint_stop_policy": CHECKPOINT_STOP_POLICY, "n_tuning_rounds_requested": tuning_rounds, "baseline_round_included": True, "resumed": bool(resume)},
        }
    )
    job = state.upsert_job(
        job_id,
        status="running",
        stage="protocol_load",
        mode="orch",
        payload={
            "session_id": session_id,
            "parent_job_id": parent_job_id or "",
            "feature_set_id": feature_set_id,
            "n_rounds": total_rounds,
            "n_tuning_rounds_requested": tuning_rounds,
            "baseline_round_included": True,
            "max_stage": max_stage,
            "execute_qlib": execute_qlib,
            "write_registry": write_registry,
            "baseline_model_params": resolved_baseline_model_params,
            "resume": bool(resume),
            "checkpoint_stop_policy": CHECKPOINT_STOP_POLICY,
        },
    )
    _event(job_id=job_id, session_id=session_id, event_type="start", stage="protocol_load", status="running", payload=job)
    _research_step(job_id=job_id, session_id=session_id, stage="protocol_load", summary="Loaded model protocol and runtime defaults.", decision="continue", next_stage="feature_snapshot_preflight", feature_set_id=feature_set_id or "")
    context_pack = (
        {"stage": "context_review", "selected_feature_set_id": feature_set_id, "feature_set_catalog": {"items": [], "count": 0, "omitted": "explicit_feature_set_id"}}
        if feature_set_id
        else build_context_pack(stage="context_review", selected_feature_set_id=feature_set_id, state=state, include_registry=False)
    )
    if not feature_set_id:
        catalog_items = (context_pack.get("feature_set_catalog") or {}).get("items") or []
        if catalog_items:
            feature_set_id = str(catalog_items[0].get("feature_set_id") or "")
    preflight = model_preflight(feature_set_id=feature_set_id)
    if not preflight.get("passed"):
        blocker = preflight.get("blocker") or _blocker(code="feature_snapshot_preflight_failed", stage="feature_snapshot_preflight")
        job = state.upsert_job(
            job_id,
            status="failed",
            stage="blocker",
            mode="orch",
            payload={"session_id": session_id, "feature_set_id": feature_set_id, "preflight": preflight, "blocker": blocker},
        )
        state.upsert_session(
            {
                "session_id": session_id,
                "status": "failed",
                "mode": "orch",
                "feature_set_id": feature_set_id or "",
                "n_rounds_requested": total_rounds,
                "n_rounds_completed": 0,
                "active_job_id": job_id,
                "parent_job_id": parent_job_id or "",
                "current_stage": "blocker",
                "current_blocker": blocker,
                "blocker_history": [blocker],
                "payload": {"preflight": preflight},
            }
        )
        _event(job_id=job_id, session_id=session_id, event_type="failed", stage="feature_snapshot_preflight", status="failed", payload={"preflight": preflight, "blocker": blocker})
        _research_step(job_id=job_id, session_id=session_id, stage="blocker", summary="Feature snapshot preflight blocked ORCH before DeepSeek.", decision=blocker.get("human_message") or blocker.get("code"), next_stage="feature_snapshot_preflight", feature_set_id=feature_set_id or "", extra={"preflight": preflight, "blocker": blocker})
        return {"ok": False, "err": "feature_snapshot_preflight_failed", "job": job, "session_id": session_id, "session": state.get_session(session_id), "feature_set_id": feature_set_id, "preflight": preflight, "blocker": blocker}
    _event(job_id=job_id, session_id=session_id, event_type="stage_complete", stage="feature_snapshot_preflight", status="completed", payload={"preflight": preflight})
    state.upsert_session(
        {
            "session_id": session_id,
            "status": "running",
            "mode": "orch",
            "feature_set_id": feature_set_id or "",
            "n_rounds_requested": total_rounds,
            "n_rounds_completed": len(resumed_rounds),
            "active_job_id": job_id,
            "parent_job_id": parent_job_id or "",
            "current_stage": "context_review",
            "round_group_ids": resumed_round_ids,
            "model_run_ids": (existing_session or {}).get("model_run_ids") or [],
            "payload": {**existing_payload, "preflight": preflight, "execute_qlib": execute_qlib, "write_registry": write_registry, "max_stage": max_stage, "baseline_model_params": resolved_baseline_model_params, "checkpoint_stop_policy": CHECKPOINT_STOP_POLICY, "n_tuning_rounds_requested": tuning_rounds, "baseline_round_included": True, "resumed": bool(resume)},
        }
    )
    system_prompt = model_system_prompt()
    completed_rounds: list[dict[str, Any]] = resumed_rounds
    experiment_history: list[dict[str, Any]] = []
    for completed in completed_rounds:
        completed_round = state.get_round(str(completed.get("round_group_id") or "")) or {}
        completed_experiment = dict(completed_round.get("experiment") or {})
        experiment_history.append(
            {
                "round_no": completed.get("round_no"),
                "round_group_id": completed.get("round_group_id"),
                "feature_set_id": feature_set_id,
                "source": "resumed_session",
                "experiment_signature": completed_round.get("experiment_signature"),
                "core_experiment_fingerprint": _core_experiment_fingerprint(completed_experiment),
                "core_experiment_params": _core_experiment_params(completed_experiment),
                "summary": completed.get("hypothesis") or completed.get("round_label") or "completed round",
            }
        )
    best_round_group_id = str(existing_payload.get("best_round_group_id") or "")
    best_round_metrics: dict[str, Any] = {}
    if best_round_group_id:
        best_round = state.get_round(best_round_group_id) or {}
        best_round_metrics = dict(((best_round.get("experiment") or {}).get("research_metadata") or {}).get("round_metrics") or {})
    consecutive_no_improvement = int(existing_payload.get("consecutive_no_improvement") or 0)
    stop_decision = ""
    stop_next = ""
    confirmation_result: dict[str, Any] = {}
    previous_synthesis: dict[str, Any] | None = None
    start_round_no = max([int(row.get("round_no") or 0) for row in completed_rounds], default=-1) + 1
    try:
        for round_no in range(start_round_no, tuning_rounds + 1):
            if state.job_stop_requested(job_id):
                stop_decision = "operator_stop"
                stop_next = "resume_same_session"
                break
            is_round0 = round_no == 0
            _stage_start(state, job_id, "context_review", round_no=round_no, payload={"feature_set_id": feature_set_id, "session_id": session_id})
            context_pack = build_context_pack(stage="experiment_plan", selected_feature_set_id=feature_set_id, state=state)
            _stage_complete(state, job_id, "context_review", round_no=round_no, payload={"context_id": context_pack.get("context_id"), "feature_set_count": (context_pack.get("feature_set_catalog") or {}).get("count")})

            _stage_start(state, job_id, "experiment_plan", round_no=round_no, payload={"planner": "model_orch"})
            stage_briefing = EXPERIMENT_PLAN_BRIEFING
            private_prior_history = list(experiment_history)
            reference_round = state.get_round(best_round_group_id) if best_round_group_id else None
            reference_experiment = dict((reference_round or {}).get("experiment") or default_r1_experiment())
            baseline_round_group_id = str(completed_rounds[0].get("round_group_id") or "") if completed_rounds else ""
            latest_round_group_id = str(completed_rounds[-1].get("round_group_id") or "") if completed_rounds else ""
            llm_payload = _experiment_plan_payload(
                round_no=round_no,
                tuning_state={
                    "best_round_group_id": best_round_group_id or None,
                    "best_round_metrics": best_round_metrics,
                    "best_parameters": _tunable_parameters(reference_experiment),
                    "consecutive_no_improvement": consecutive_no_improvement,
                    "meaningful_improvement_rule": "research_score must improve by at least 1.0",
                },
                round_roles={
                    "baseline_round_group_id": baseline_round_group_id or None,
                    "best_round_group_id": best_round_group_id or None,
                    "latest_round_group_id": latest_round_group_id or None,
                },
                completed_rounds=_compact_completed_rounds_for_llm(completed_rounds),
            )
            if not is_round0:
                record_orch_trace(
                    "experiment_plan",
                    system_prompt=system_prompt,
                    stage_briefing=stage_briefing,
                    context_pack=context_pack,
                    job_id=job_id,
                    session_id=session_id,
                    round_no=round_no,
                    output_contract={
                        "feature_set_id": feature_set_id,
                        "round_no": round_no,
                        "required_seed_policy": "seed42_screening_then_session_best_confirmation",
                        "execute_qlib": execute_qlib,
                        "planner_mode": "deepseek",
                        "llm_call_status": "call_required",
                        "private_duplicate_history_count": len(private_prior_history),
                        "llm_payload": llm_payload,
                    },
                )
            plan_attempt = 1
            plan_error = ""
            plan_rejection: dict[str, Any] | None = None
            parsed_response: dict[str, Any] | None = _round0_experiment_plan(
                feature_set_id=feature_set_id or "",
                baseline_model_params=resolved_baseline_model_params,
            ) if is_round0 else None
            if is_round0:
                plan_attempt = 4
            while plan_attempt <= 3:
                attempt_payload = dict(llm_payload)
                if plan_error:
                    attempt_payload["correction"] = _experiment_plan_correction_context(
                        error=plan_error,
                        plan_rejection=plan_rejection,
                    )
                    record_orch_trace(
                        "experiment_plan",
                        system_prompt=system_prompt,
                        stage_briefing=stage_briefing,
                        context_pack=context_pack,
                        job_id=job_id,
                        session_id=session_id,
                        round_no=round_no,
                        output_contract={
                            "feature_set_id": feature_set_id,
                            "round_no": round_no,
                            "required_seed_policy": "seed42_screening_then_session_best_confirmation",
                            "execute_qlib": execute_qlib,
                            "planner_mode": "deepseek",
                            "llm_call_status": "retry_required",
                            "retry_attempt": plan_attempt,
                            "private_duplicate_history_count": len(private_prior_history),
                            "llm_payload": attempt_payload,
                        },
                    )
                try:
                    parsed_response = _require_deepseek_experiment_plan_v2(
                        client=client,
                        system_prompt=system_prompt,
                        payload=attempt_payload,
                        requested_feature_set_id=feature_set_id or "",
                        reference_experiment=reference_experiment,
                        prior_history=private_prior_history,
                    )
                    if plan_error:
                        parsed_response["llm_retry_count"] = plan_attempt - 1
                        parsed_response["llm_retry_resolved_error"] = plan_error
                    break
                except Exception as exc:
                    plan_error = str(exc)
                    plan_rejection = getattr(exc, "plan_rejection", None)
                    retryable = _retryable_experiment_plan_error(exc)
                    record_orch_trace(
                        "experiment_plan",
                        system_prompt=system_prompt,
                        stage_briefing=stage_briefing,
                        context_pack=context_pack,
                        job_id=job_id,
                        session_id=session_id,
                        round_no=round_no,
                        parsed_response={
                            "stage": "blocker" if not retryable or plan_attempt >= 3 else "experiment_plan",
                            "decision": "blocked" if not retryable or plan_attempt >= 3 else "retry_deepseek",
                            "summary": (
                                "DeepSeek experiment_plan failed; retrying with explicit feedback."
                                if retryable and plan_attempt < 3
                                else "DeepSeek experiment_plan failed; no fallback planner executed."
                            ),
                            "next": "experiment_plan" if retryable and plan_attempt < 3 else "blocker",
                            "error": plan_error,
                            "planner_mode": "deepseek",
                            "llm_call_status": "retry" if retryable and plan_attempt < 3 else "failed",
                            "retry_attempt": plan_attempt,
                        },
                        result_summary={
                            "decision": "retry_deepseek" if retryable and plan_attempt < 3 else "blocked",
                            "round_no": round_no,
                            "planner_mode": "deepseek",
                            "llm_call_status": "retry" if retryable and plan_attempt < 3 else "failed",
                            "retry_attempt": plan_attempt,
                            "error": plan_error,
                        },
                    )
                    if not retryable or plan_attempt >= 3:
                        raise
                    plan_attempt += 1
            if parsed_response is None:
                raise DeepSeekClientError(f"llm_experiment_plan_retry_exhausted:{plan_error}")
            parsed_experiment = parsed_response.get("experiment_json")
            if not isinstance(parsed_experiment, dict):
                raise DeepSeekClientError(
                    "llm_experiment_json_not_object_after_normalize:"
                    f"{type(parsed_experiment).__name__}"
                )
            experiment = dict(parsed_experiment)
            research_metadata = dict(experiment.get("research_metadata") or {})
            research_metadata.update(
                {
                    "round_no": round_no,
                    "round_kind": "baseline" if is_round0 else "tuning",
                    "round_label": "Round 0 · 基准测试" if is_round0 else f"Round {round_no} · 参数研究",
                    "reference_round_group_id": best_round_group_id or None,
                    "hypothesis": parsed_response.get("experiment_hypothesis") or parsed_response.get("summary"),
                    "parameter_changes": parsed_response.get("parameter_changes") or [],
                }
            )
            experiment["research_metadata"] = research_metadata
            if not execute_qlib:
                experiment["metrics_by_seed"] = _round_metrics_for_shadow(round_no)
            record_orch_trace(
                "experiment_plan",
                system_prompt=system_prompt,
                stage_briefing=stage_briefing,
                context_pack=context_pack,
                job_id=job_id,
                session_id=session_id,
                round_no=round_no,
                round_group_id="",
                parsed_response=parsed_response,
                result_summary={
                    "decision": "submit_experiment",
                    "round_no": round_no,
                    "shadow_metrics": not execute_qlib,
                    "planner_mode": parsed_response.get("planner_mode") or "deepseek",
                    "llm_call_status": parsed_response.get("llm_call_status") or "called",
                    "llm_model": parsed_response.get("llm_model"),
                    "llm_provider_model": parsed_response.get("llm_provider_model"),
                },
            )
            submitted = submit_experiment(feature_set_id=feature_set_id or "", experiment=experiment, state=state)
            if not submitted.get("ok"):
                raise RuntimeError(f"submit_experiment_failed:{submitted.get('validation_result')}")
            round_group_id = submitted["round_group"]["round_group_id"]
            persisted_round = state.get_round(round_group_id) or submitted["round_group"]
            persisted_round["round_no"] = round_no
            persisted_round["round_kind"] = "baseline" if is_round0 else "tuning"
            persisted_round["round_label"] = "Round 0 · 基准测试" if is_round0 else f"Round {round_no} · 参数研究"
            state.upsert_round(persisted_round)
            experiment_history.append(
                {
                    "round_no": round_no,
                    "round_group_id": round_group_id,
                    "feature_set_id": feature_set_id,
                    "source": "current_job",
                    "experiment_signature": submitted["round_group"].get("experiment_signature"),
                    "core_experiment_fingerprint": parsed_response.get("core_experiment_fingerprint") or _core_experiment_fingerprint(parsed_experiment),
                    "core_experiment_params": parsed_response.get("core_experiment_params") or _core_experiment_params(parsed_experiment),
                    "nearest_prior_parameter_diff": parsed_response.get("nearest_prior_parameter_diff") or {},
                    "next_move": parsed_response.get("next_move"),
                    "summary": parsed_response.get("summary"),
                    "round_kind": "baseline" if is_round0 else "tuning",
                    "round_label": "Round 0 · 基准测试" if is_round0 else f"Round {round_no} · 参数研究",
                }
            )
            _stage_complete(state, job_id, "experiment_plan", round_no=round_no, round_group_id=round_group_id, payload={"round_group_id": round_group_id, "experiment_signature": submitted["round_group"].get("experiment_signature")})
            _research_step(job_id=job_id, session_id=session_id, stage="experiment_plan", summary=parsed_response["summary"], decision="submitted", next_stage="train_backtest_seed42", round_no=round_no, round_group_id=round_group_id, feature_set_id=feature_set_id or "", refs=["model_default_contract" if is_round0 else "orchestrator_traces/current.jsonl"], extra={"round_kind": "baseline" if is_round0 else "tuning", "round_label": "Round 0 · 基准测试" if is_round0 else f"Round {round_no} · 参数研究", "planner_mode": parsed_response.get("planner_mode"), "llm_call_status": parsed_response.get("llm_call_status")})

            if max_stage == "experiment_plan":
                _mark_round_checkpoint(state, round_group_id, "experiment_plan")
                completed_rounds.append({"round_no": round_no, "round_group_id": round_group_id, "stopped_at": max_stage})
                continue

            _stage_start(state, job_id, "train_backtest_seed42", round_no=round_no, round_group_id=round_group_id, payload={"execute_qlib": execute_qlib, "seed": 42})
            run_result = run_round(round_group_id=round_group_id, state=state, execute_qlib=execute_qlib, seeds=[42], phase="screening")
            if not run_result.get("ok"):
                raise RuntimeError(f"run_round_failed:{run_result}")
            _stage_complete(state, job_id, "train_backtest_seed42", round_no=round_no, round_group_id=round_group_id, payload={"seed_statuses": {str(row.get("seed")): row.get("status") for row in run_result.get("seed_runs", [])}})

            if max_stage in {"train_backtest_seed42", "train_backtest_3seed"}:
                _mark_round_checkpoint(state, round_group_id, "train_backtest_seed42")
                completed_rounds.append({"round_no": round_no, "round_group_id": round_group_id, "stopped_at": max_stage})
                continue

            _stage_start(state, job_id, "research_score", round_no=round_no, round_group_id=round_group_id)
            score_result = score_round(round_group_id, state=state)
            if not score_result.get("ok"):
                raise RuntimeError(f"score_review_failed:{score_result}")
            score_summary = _compact_score_results(score_result.get("results", []))
            _stage_complete(
                state,
                job_id,
                "research_score",
                round_no=round_no,
                round_group_id=round_group_id,
                payload={
                    "results": score_summary,
                    "round_consistency": score_result.get("round_consistency"),
                },
            )

            if max_stage in {"research_score", "score_review"}:
                _mark_round_checkpoint(state, round_group_id, "research_score")
                completed_rounds.append({"round_no": round_no, "round_group_id": round_group_id, "scores": score_summary, "stopped_at": max_stage})
                continue

            _stage_start(state, job_id, "registry_write", round_no=round_no, round_group_id=round_group_id, payload={"asset_status": "research", "registry": "production" if write_registry else "shadow"})
            registry_target = ModelRegistry() if write_registry else shadow_registry
            gate_result = (
                register_research_screening_round(round_group_id, state=state, registry=registry_target)
                if write_registry
                else {"ok": True, "asset_status": "research", "registry_write": "skipped_shadow"}
            )
            if not gate_result.get("ok"):
                raise RuntimeError(f"research_registry_write_failed:{gate_result}")
            gate_summary = [gate_result]
            _stage_complete(state, job_id, "registry_write", round_no=round_no, round_group_id=round_group_id, payload={"results": gate_summary, "registry": "production" if write_registry else "shadow"})
            persisted_seed_runs = state.list_seed_runs(round_group_id=round_group_id)
            current_round_metrics = round_research_metrics(persisted_seed_runs)
            reference_round_group_id = best_round_group_id or None
            comparison = improvement_vs_reference(current_round_metrics, best_round_metrics) if best_round_metrics else {}
            improved_platform_best = is_round0 or meaningfully_improves(current_round_metrics, best_round_metrics)
            if improved_platform_best:
                best_round_group_id = round_group_id
                best_round_metrics = current_round_metrics
                consecutive_no_improvement = 0
            else:
                consecutive_no_improvement += 1
            seed_results = []
            for seed_run in persisted_seed_runs:
                metrics = dict(seed_run.get("metrics") or {})
                seed_results.append(
                    {
                        "seed": seed_run.get("seed"),
                        "model_run_id": seed_run.get("model_run_id"),
                        "research_score": (seed_run.get("score") or {}).get("research_score"),
                        "excess_annualized_ret_with_cost": metrics.get("excess_annualized_ret_with_cost"),
                        "excess_information_ratio_with_cost": metrics.get("excess_information_ratio_with_cost"),
                        "max_drawdown": metrics.get("max_drawdown"),
                        "rank_ic": metrics.get("rank_ic"),
                        "rank_icir": metrics.get("rank_icir"),
                        "turnover": metrics.get("turnover"),
                        "training_diagnostics": metrics.get("training_diagnostics") or {},
                    }
                )
            round_record = {
                "round_no": round_no,
                "round_kind": "baseline" if is_round0 else "tuning",
                "round_label": "Round 0 · 基准测试" if is_round0 else f"Round {round_no} · 参数研究",
                "round_group_id": round_group_id,
                "reference_round_group_id": reference_round_group_id,
                "hypothesis": parsed_response.get("experiment_hypothesis") or parsed_response.get("summary"),
                "parameter_changes": parsed_response.get("parameter_changes") or [],
                "parameters": _tunable_parameters(parsed_experiment),
                "seed_results": seed_results,
                "round_metrics": current_round_metrics,
                "improvement_vs_reference": comparison,
                "improved_platform_best": improved_platform_best,
                "scores": score_summary,
                "gate": gate_summary,
                "round_synthesis_decision": "pending",
            }
            evaluated_round = state.get_round(round_group_id) or {}
            evaluated_experiment = dict(evaluated_round.get("experiment") or {})
            evaluated_metadata = dict(evaluated_experiment.get("research_metadata") or {})
            evaluated_metadata.update(
                {
                    "round_metrics": current_round_metrics,
                    "improvement_vs_reference": comparison,
                    "improved_platform_best": improved_platform_best,
                    "best_round_group_id_after_evaluation": best_round_group_id,
                    "consecutive_no_improvement": consecutive_no_improvement,
                }
            )
            evaluated_experiment["research_metadata"] = evaluated_metadata
            evaluated_round["experiment"] = evaluated_experiment
            state.upsert_round(evaluated_round)
            completed_rounds.append(round_record)

            _stage_start(state, job_id, "round_synthesis", round_no=round_no, round_group_id=round_group_id)
            synthesis_context = build_context_pack(stage="round_synthesis", round_group_id=round_group_id, state=state, include_registry=False)
            previous_synthesis = _deterministic_round_synthesis(
                round_record,
                consecutive_no_improvement=consecutive_no_improvement,
            )
            previous_synthesis["best_round_group_id"] = best_round_group_id
            record_orch_trace(
                "round_synthesis",
                system_prompt=system_prompt,
                stage_briefing=ROUND_SYNTHESIS_BRIEFING,
                context_pack=synthesis_context,
                job_id=job_id,
                session_id=session_id,
                round_no=round_no,
                round_group_id=round_group_id,
                parsed_response=previous_synthesis,
                result_summary={
                    "round_no": round_no,
                    "candidate_count": sum(1 for row in gate_summary if row.get("asset_status") == "candidate"),
                    "planner_mode": "deterministic_platform_summary",
                    "llm_call_status": "not_called",
                    "best_round_group_id": best_round_group_id,
                    "consecutive_no_improvement": consecutive_no_improvement,
                },
            )
            _stage_complete(state, job_id, "round_synthesis", round_no=round_no, round_group_id=round_group_id, payload=previous_synthesis)
            synthesis_decision = str(previous_synthesis.get("decision") or "")
            synthesis_next = str(previous_synthesis.get("next") or "")
            _research_step(job_id=job_id, session_id=session_id, stage="round_synthesis", summary=str(previous_synthesis.get("summary") or ""), decision=synthesis_decision, next_stage=synthesis_next, round_no=round_no, round_group_id=round_group_id, feature_set_id=feature_set_id or "", refs=previous_synthesis.get("evidence_refs") if isinstance(previous_synthesis.get("evidence_refs"), list) else [], extra=previous_synthesis)
            if completed_rounds and completed_rounds[-1].get("round_group_id") == round_group_id:
                completed_rounds[-1]["round_synthesis_decision"] = synthesis_decision
            else:
                completed_rounds.append({"round_no": round_no, "round_group_id": round_group_id, "scores": score_summary, "gate": gate_summary, "round_synthesis_decision": synthesis_decision})
            state.upsert_session(
                {
                    "session_id": session_id,
                    "status": "running",
                    "mode": "orch",
                    "feature_set_id": feature_set_id or "",
                    "n_rounds_requested": total_rounds,
                    "n_rounds_completed": len(completed_rounds),
                    "active_job_id": job_id,
                    "parent_job_id": parent_job_id or "",
                    "current_stage": "round_synthesis",
                    "round_group_ids": [row.get("round_group_id") for row in completed_rounds if row.get("round_group_id")],
                    "model_run_ids": [seed.get("model_run_id") for row in state.list_rounds(limit=100) for seed in (row.get("seed_runs") or []) if seed.get("model_run_id")],
                    "payload": {"completed_rounds": completed_rounds, "preflight": preflight, "best_round_group_id": best_round_group_id, "consecutive_no_improvement": consecutive_no_improvement, "checkpoint_stop_policy": CHECKPOINT_STOP_POLICY},
                }
            )
            if synthesis_decision and synthesis_decision != "continue":
                should_stop = synthesis_decision in {"blocked", "checkpoint_stop"}
                _event(
                    job_id=job_id,
                    session_id=session_id,
                    event_type="checkpoint_stop" if synthesis_decision == "checkpoint_stop" else "blocked",
                    stage="round_synthesis",
                    status="interrupted" if should_stop else "running",
                    round_no=round_no,
                    round_group_id=round_group_id,
                    payload={
                        "decision": synthesis_decision,
                        "next": synthesis_next,
                        "summary": previous_synthesis.get("summary"),
                        "checkpoint_stop_policy": CHECKPOINT_STOP_POLICY,
                    },
                )
                if should_stop:
                    stop_decision = synthesis_decision
                    stop_next = synthesis_next
                    break
        if state.job_stop_requested(job_id) and not stop_decision:
            stop_decision = "operator_stop"
            stop_next = "resume_same_session"
        if best_round_group_id and max_stage == "round_synthesis" and not stop_decision:
            _stage_start(state, job_id, "research_confirmation", round_group_id=best_round_group_id, payload={"seeds": [17, 83], "execute_qlib": execute_qlib})
            confirmation_registry = ModelRegistry() if write_registry else shadow_registry
            confirmation_result = confirm_research_round(
                best_round_group_id,
                execute_qlib=execute_qlib,
                state=state,
                registry=confirmation_registry,
                write_registry=write_registry,
            )
            if not confirmation_result.get("ok"):
                raise RuntimeError(f"research_confirmation_failed:{confirmation_result}")
            _stage_complete(state, job_id, "research_confirmation", round_group_id=best_round_group_id, payload=confirmation_result.get("confirmation") or {})
            _research_step(
                job_id=job_id,
                session_id=session_id,
                stage="research_confirmation",
                summary="会话最优轮已完成 Seed 17/83 确认。",
                decision=str((confirmation_result.get("confirmation") or {}).get("status") or ""),
                next_stage="production_rolling" if (confirmation_result.get("confirmation") or {}).get("status") == "passed" else "human_review",
                round_group_id=best_round_group_id,
                feature_set_id=feature_set_id or "",
                extra=confirmation_result.get("confirmation") or {},
            )
        final_status = "interrupted" if stop_decision else "completed"
        final_stage = "blocker" if stop_decision == "blocked" else ("checkpoint_stop" if stop_decision in {"checkpoint_stop", "operator_stop"} else ("round_synthesis" if max_stage == "round_synthesis" else max_stage))
        job = state.upsert_job(job_id, status=final_status, stage=final_stage, mode="orch", current_round_group_id=(completed_rounds[-1].get("round_group_id") if completed_rounds else ""), payload={"session_id": session_id, "feature_set_id": feature_set_id, "n_rounds": total_rounds, "n_tuning_rounds_requested": tuning_rounds, "baseline_round_included": True, "baseline_model_params": resolved_baseline_model_params, "completed_rounds": completed_rounds, "best_round_group_id": best_round_group_id, "consecutive_no_improvement": consecutive_no_improvement, "research_confirmation": confirmation_result, "execute_qlib": execute_qlib, "write_registry": write_registry, "checkpoint_stop_policy": CHECKPOINT_STOP_POLICY, "stop_decision": stop_decision, "stop_next": stop_next})
        session = state.upsert_session(
            {
                "session_id": session_id,
                "status": final_status,
                "mode": "orch",
                "feature_set_id": feature_set_id or "",
                "n_rounds_requested": total_rounds,
                "n_rounds_completed": len(completed_rounds),
                "active_job_id": job_id,
                "parent_job_id": parent_job_id or "",
                "current_stage": final_stage,
                "round_group_ids": [row.get("round_group_id") for row in completed_rounds if row.get("round_group_id")],
                "model_run_ids": [seed.get("model_run_id") for round_id in [row.get("round_group_id") for row in completed_rounds] for seed in state.list_seed_runs(round_group_id=round_id) if seed.get("model_run_id")],
                "payload": {"completed_rounds": completed_rounds, "preflight": preflight, "best_round_group_id": best_round_group_id, "consecutive_no_improvement": consecutive_no_improvement, "research_confirmation": confirmation_result, "baseline_model_params": resolved_baseline_model_params, "checkpoint_stop_policy": CHECKPOINT_STOP_POLICY, "stop_decision": stop_decision, "stop_next": stop_next, "n_tuning_rounds_requested": tuning_rounds, "baseline_round_included": True},
            }
        )
        _event(job_id=job_id, session_id=session_id, event_type="complete" if not stop_decision else "interrupted", stage=final_stage, status=final_status, payload={"completed_rounds": completed_rounds, "stop_decision": stop_decision, "stop_next": stop_next})
        return {"ok": True, "job": job, "session": session, "completed_rounds": completed_rounds, "research_confirmation": confirmation_result, "context_pack": context_pack, "registry_target": "production" if write_registry else "shadow"}
    except Exception as exc:
        traceback_text = traceback.format_exc()
        blocker = _blocker(code=str(exc), stage="blocker", human_message=str(exc), repair_action="inspect_blocker_then_resume_same_session", resume_from="blocker")
        job = state.upsert_job(job_id, status="failed", stage="blocker", mode="orch", payload={"session_id": session_id, "error": str(exc), "traceback": traceback_text, "feature_set_id": feature_set_id, "completed_rounds": completed_rounds, "best_round_group_id": best_round_group_id, "consecutive_no_improvement": consecutive_no_improvement, "blocker": blocker})
        session = state.upsert_session(
            {
                "session_id": session_id,
                "status": "failed",
                "mode": "orch",
                "feature_set_id": feature_set_id or "",
                "n_rounds_requested": total_rounds,
                "n_rounds_completed": len(completed_rounds),
                "active_job_id": job_id,
                "parent_job_id": parent_job_id or "",
                "current_stage": "blocker",
                "current_blocker": blocker,
                "round_group_ids": [row.get("round_group_id") for row in completed_rounds if row.get("round_group_id")],
                "blocker_history": [blocker],
                "payload": {"completed_rounds": completed_rounds, "best_round_group_id": best_round_group_id, "consecutive_no_improvement": consecutive_no_improvement, "traceback": traceback_text},
            }
        )
        _event(job_id=job_id, session_id=session_id, event_type="failed", stage="blocker", status="failed", payload={"error": str(exc), "traceback": traceback_text, "completed_rounds": completed_rounds, "blocker": blocker})
        _research_step(job_id=job_id, session_id=session_id, stage="blocker", summary="ORCH run failed.", decision=str(exc), next_stage="blocker", feature_set_id=feature_set_id or "", extra={"completed_rounds": completed_rounds, "blocker": blocker, "traceback": traceback_text})
        return {"ok": False, "err": str(exc), "traceback": traceback_text, "job": job, "session": session, "blocker": blocker, "completed_rounds": completed_rounds}
