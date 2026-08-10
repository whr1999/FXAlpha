from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from storage.model_registry import ModelRegistry

from .contracts import MODEL_SYSTEM_VERSION, is_model_system_version, production_contract, stable_json, utc_now
from .feature_sets import all_active_pointer_summary, feature_set_catalog_summary
from .paths import MODEL_CONTEXT_SNAPSHOTS, MODEL_MCP_TRACES, MODEL_ORCHESTRATOR_TRACES, MODEL_RESEARCH_STEPS
from .state_store import ModelStateStore, append_jsonl, read_jsonl


def _decode_metadata(row: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(row.get("metadata") or "{}")
    except Exception:
        return {}


def _model_rows(status: str = "all", limit: int = 30) -> list[dict[str, Any]]:
    rows = ModelRegistry().list_models(status)
    out: list[dict[str, Any]] = []
    for row in rows:
        md = _decode_metadata(row)
        if is_model_system_version(md.get("model_system_version")):
            item = dict(row)
            item["metadata"] = md
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _safe_round(value: Any, digits: int = 4) -> Any:
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return value


def _truncate(value: Any, max_chars: int = 800) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3] + "..."


def _compact_list(values: Any, *, limit: int = 6, item_chars: int = 500) -> list[Any]:
    if not isinstance(values, list):
        return []
    return [_truncate(item, item_chars) if isinstance(item, str) else item for item in values[:limit]]


def _compact_seed_run(row: dict[str, Any], *, include_artifact: bool = True) -> dict[str, Any]:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    score = row.get("score") if isinstance(row.get("score"), dict) else {}
    gate = row.get("gate") if isinstance(row.get("gate"), dict) else {}
    payload = {
        "model_run_id": row.get("model_run_id"),
        "round_group_id": row.get("round_group_id"),
        "seed": row.get("seed"),
        "status": row.get("status"),
        "registry_status": row.get("registry_status"),
        "registry_model_id": row.get("registry_model_id"),
        "metrics": {
            key: _safe_round(metrics.get(key))
            for key in (
                "annualized_ret",
                "excess_annualized_ret_with_cost",
                "excess_information_ratio_with_cost",
                "max_drawdown",
                "rank_ic",
                "rank_icir",
                "strategy_annualized_ret",
                "avg_cost",
                "date_count",
            )
            if key in metrics
        },
        "score": {
            "research_score": _safe_round(score.get("research_score", score.get("sota_score")), 3),
            "decision": score.get("decision"),
            "hard_blocks": score.get("hard_blocks") or [],
            "score_review_version": score.get("score_review_version"),
        },
        "gate": {
            "gate_status": gate.get("gate_status"),
            "asset_status": gate.get("asset_status"),
            "reason": gate.get("reason"),
            "warnings": gate.get("warnings") or [],
        },
    }
    if include_artifact:
        payload["artifact_dir"] = row.get("artifact_dir")
    return payload


def _compact_round(row: dict[str, Any], *, include_seed_runs: bool = True) -> dict[str, Any]:
    experiment = row.get("experiment") if isinstance(row.get("experiment"), dict) else {}
    model_kwargs = experiment.get("qlib_model_kwargs") if isinstance(experiment.get("qlib_model_kwargs"), dict) else {}
    payload = {
        "round_group_id": row.get("round_group_id"),
        "feature_set_id": row.get("feature_set_id"),
        "status": row.get("status"),
        "stage": row.get("stage"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "seed_set": row.get("seed_set") or [],
        "experiment_signature": row.get("experiment_signature"),
        "experiment_summary": {
            "baseline_kind": experiment.get("baseline_kind"),
            "sample_weight_policy": experiment.get("sample_weight_policy"),
            "feature_missing_strategy": experiment.get("feature_missing_strategy"),
            "portfolio": experiment.get("portfolio"),
            "benchmark": experiment.get("benchmark"),
            "qlib_model_kwargs": {
                key: model_kwargs.get(key)
                for key in (
                    "learning_rate",
                    "lr",
                    "num_leaves",
                    "max_depth",
                    "lambda_l1",
                    "lambda_l2",
                    "feature_fraction",
                )
                if key in model_kwargs
            },
        },
    }
    if include_seed_runs:
        payload["seed_runs"] = [_compact_seed_run(seed, include_artifact=False) for seed in (row.get("seed_runs") or [])]
    return payload


def _compact_registry_row(row: dict[str, Any]) -> dict[str, Any]:
    md = row.get("metadata") if isinstance(row.get("metadata"), dict) else _decode_metadata(row)
    return {
        "model_id": row.get("model_id") or row.get("id"),
        "model_run_id": row.get("model_run_id"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "round_group_id": md.get("round_group_id"),
        "seed": md.get("seed"),
        "research_score": _safe_round(md.get("research_score"), 3),
        "confirmed_research_score": _safe_round(md.get("confirmed_research_score"), 3),
        "rolling_score": _safe_round(md.get("rolling_score"), 3),
        "gate_status": md.get("gate_status"),
        "feature_set_id": md.get("feature_set_id") or row.get("feature_set_id"),
        "asset_status": md.get("asset_status") or row.get("status"),
        "sample_weight_policy": md.get("sample_weight_policy"),
        "portfolio": md.get("portfolio"),
        "benchmark": md.get("benchmark"),
    }


def _compact_research_step(row: dict[str, Any], *, include_extra: bool = True) -> dict[str, Any]:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    compact_extra: dict[str, Any] = {}
    if include_extra:
        for key in (
            "score_summary",
            "gate_summary",
            "validation_summary",
            "parameter_lessons",
            "next_experiment_guidance",
        ):
            if key in extra:
                value = extra.get(key)
                if key == "validation_summary":
                    value = _truncate(value, 900)
                elif key == "parameter_lessons":
                    value = _compact_list(value, limit=4, item_chars=500)
                elif key == "next_experiment_guidance":
                    value = _truncate(value, 900)
                compact_extra[key] = value
    return {
        "ts": row.get("ts"),
        "job_id": row.get("job_id"),
        "stage": row.get("stage"),
        "round_no": row.get("round_no"),
        "round_group_id": row.get("round_group_id"),
        "feature_set_id": row.get("feature_set_id"),
        "decision": row.get("decision"),
        "next": row.get("next"),
        "summary": _truncate(row.get("summary"), 900),
        "evidence_refs": _compact_list(row.get("evidence_refs") or [], limit=8 if include_extra else 3, item_chars=300),
        "stage_transition": row.get("stage_transition") or {},
        "extra": compact_extra,
    }


def _build_llm_evidence_window(
    *,
    recent_rounds: list[dict[str, Any]],
    recent_steps: list[dict[str, Any]],
    seed_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    synthesis_steps = [
        _compact_research_step(row)
        for row in recent_steps
        if row.get("stage") == "round_synthesis"
    ][-5:]
    compact_recent_steps = [_compact_research_step(row, include_extra=False) for row in recent_steps[-12:]]
    return {
        "policy": {
            "rounds": "latest_first",
            "max_recent_rounds": 5,
            "max_round_synthesis_steps": 5,
            "max_research_steps": 12,
            "max_seed_runs": 15,
            "use_compact_evidence_only": True,
            "do_not_infer_missing_metrics": True,
        },
        "current_seed_runs": [_compact_seed_run(row) for row in seed_runs[:15]],
        "recent_rounds": [_compact_round(row, include_seed_runs=True) for row in recent_rounds[:5]],
        "recent_round_syntheses": list(reversed(synthesis_steps)),
        "recent_research_steps": list(reversed(compact_recent_steps)),
    }


PLANNER_LGBM_PARAMETER_KEYS = (
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


def _planner_lgbm_parameters(experiment: dict[str, Any]) -> dict[str, Any]:
    model_kwargs = experiment.get("qlib_model_kwargs") if isinstance(experiment.get("qlib_model_kwargs"), dict) else {}
    training = experiment.get("training_hyperparameters") if isinstance(experiment.get("training_hyperparameters"), dict) else {}
    merged = dict(training)
    merged.update(model_kwargs)
    parameters = {key: merged.get(key) for key in PLANNER_LGBM_PARAMETER_KEYS if key in merged}
    if "learning_rate" not in parameters and "lr" in merged:
        parameters["learning_rate"] = merged.get("lr")
    return parameters


def _planner_seed_result(seed_run: dict[str, Any]) -> dict[str, Any]:
    metrics = seed_run.get("metrics") if isinstance(seed_run.get("metrics"), dict) else {}
    score = seed_run.get("score") if isinstance(seed_run.get("score"), dict) else {}
    validation = seed_run.get("validation") if isinstance(seed_run.get("validation"), dict) else {}
    return {
        "seed": seed_run.get("seed"),
        "research_score": _safe_round(score.get("research_score", score.get("sota_score")), 3),
        "score_decision": score.get("decision"),
        "metrics": {
            key: _safe_round(metrics.get(key))
            for key in (
                "excess_annualized_ret_with_cost",
                "excess_information_ratio_with_cost",
                "max_drawdown",
                "rank_ic",
                "rank_icir",
                "avg_cost",
            )
            if metrics.get(key) is not None
        },
        "training_diagnostics": metrics.get("training_diagnostics") or {},
        "validation": {
            key: validation.get(key)
            for key in ("status", "hard_blocks", "warnings")
            if validation.get(key) not in (None, [], {})
        },
    }


def _planner_lesson(step: dict[str, Any] | None) -> dict[str, Any]:
    extra = step.get("extra") if isinstance((step or {}).get("extra"), dict) else {}
    return {
        "parameter_lessons": _compact_list(extra.get("parameter_lessons"), limit=3, item_chars=360),
        "next_experiment_guidance": _truncate(extra.get("next_experiment_guidance"), 500),
        "next_parameter_change_rationale": _compact_list(extra.get("next_parameter_change_rationale"), limit=3, item_chars=360),
    }


def _planner_reference_outcome(round_row: dict[str, Any]) -> dict[str, Any]:
    seed_runs = round_row.get("seed_runs") if isinstance(round_row.get("seed_runs"), list) else []
    seed42 = next((seed for seed in seed_runs if int(seed.get("seed") or -1) == 42), {})
    seed42_score = (seed42.get("score") or {}).get("research_score", (seed42.get("score") or {}).get("sota_score"))
    return {
        "seed42_research_score": _safe_round(seed42_score, 3),
        "research_confirmation": (((round_row.get("experiment") or {}).get("research_metadata") or {}).get("research_confirmation") or {}),
    }


def _build_experiment_plan_context(*, state: ModelStateStore, feature_set_id: str) -> dict[str, Any]:
    all_rounds = state.list_rounds(limit=160)
    current_rounds = [row for row in all_rounds if row.get("feature_set_id") == feature_set_id]
    recent_steps = read_jsonl(MODEL_RESEARCH_STEPS, limit=100)
    synthesis_by_round = {
        str(row.get("round_group_id")): row
        for row in recent_steps
        if row.get("stage") == "round_synthesis" and row.get("feature_set_id") == feature_set_id
    }
    recent_rounds = []
    for index, row in enumerate(current_rounds[:5]):
        seed_runs = row.get("seed_runs") if isinstance(row.get("seed_runs"), list) else []
        seed42 = next((seed for seed in seed_runs if int(seed.get("seed") or -1) == 42), {})
        confirmation = (((row.get("experiment") or {}).get("research_metadata") or {}).get("research_confirmation") or {})
        recent_rounds.append(
            {
                "recency": "latest" if index == 0 else f"previous_{index}",
                "parameters": _planner_lgbm_parameters(row.get("experiment") or {}),
                "seed42_result": _planner_seed_result(seed42) if seed42 else {},
                "seed_audit": {
                    "status": confirmation.get("status"),
                    "audit_reference_score": _safe_round(confirmation.get("confirmed_research_score"), 3),
                    "gates": confirmation.get("gates") or {},
                    "seed_count": len(confirmation.get("seed_results") or []),
                } if confirmation else {},
                "lesson": _planner_lesson(synthesis_by_round.get(str(row.get("round_group_id")))),
            }
        )

    ledger = []
    seen_parameters: set[str] = set()
    for row in current_rounds:
        parameters = _planner_lgbm_parameters(row.get("experiment") or {})
        signature = stable_json(parameters)
        if not parameters or signature in seen_parameters:
            continue
        seen_parameters.add(signature)
        ledger.append(parameters)
        if len(ledger) >= 16:
            break

    references = []
    reference_counts: dict[str, int] = {}
    for row in all_rounds:
        source_feature_set_id = str(row.get("feature_set_id") or "")
        if not source_feature_set_id or source_feature_set_id == feature_set_id:
            continue
        if reference_counts.get(source_feature_set_id, 0) >= 1:
            continue
        parameters = _planner_lgbm_parameters(row.get("experiment") or {})
        if not parameters:
            continue
        reference_counts[source_feature_set_id] = reference_counts.get(source_feature_set_id, 0) + 1
        references.append({"parameters": parameters, "outcome": _planner_reference_outcome(row)})
        if len(references) >= 3:
            break

    return {
        "context_version": "model_experiment_plan_context_v1",
        "stage": "experiment_plan",
        "research_evidence": {
            "recent_rounds": recent_rounds,
            "parameter_ledger": ledger,
            "cross_feature_references": references,
        },
        "correction": {},
    }


def build_context_pack(
    *,
    stage: str = "context_review",
    round_group_id: str | None = None,
    selected_feature_set_id: str | None = None,
    include_registry: bool = True,
    state: ModelStateStore | None = None,
) -> dict[str, Any]:
    state = state or ModelStateStore()
    round_payload = state.get_round(round_group_id) if round_group_id else None
    requested_feature_set = str((round_payload or {}).get("feature_set_id") or selected_feature_set_id or "")
    if stage == "experiment_plan":
        return _build_experiment_plan_context(state=state, feature_set_id=requested_feature_set)
    raw_seed_runs = state.list_seed_runs(round_group_id=round_group_id) if round_group_id else state.list_seed_runs(limit=9)
    lightweight_stage = stage in {"round_synthesis", "research_confirmation", "research_score", "train_backtest_seed42"}
    all_active_pointer = {} if lightweight_stage else all_active_pointer_summary()
    feature_set_catalog = {"items": [], "count": 0, "omitted_for_stage": stage} if lightweight_stage else feature_set_catalog_summary(limit=30)
    recent_steps = read_jsonl(MODEL_RESEARCH_STEPS, limit=50)
    human_guidance = [row for row in recent_steps if row.get("stage") == "human_guidance"][-5:]
    raw_registry_recent = _model_rows("all", 30) if include_registry else []
    raw_registry_production = _model_rows("production", 30) if include_registry else []
    recent_rounds = state.list_rounds(limit=10)
    compact_seed_runs = [_compact_seed_run(row) for row in raw_seed_runs]
    compact_registry_recent = [_compact_registry_row(row) for row in raw_registry_recent[:15]]
    compact_registry_production = [_compact_registry_row(row) for row in raw_registry_production[:15]]
    llm_evidence_window = _build_llm_evidence_window(
        recent_rounds=recent_rounds,
        recent_steps=recent_steps,
        seed_runs=raw_seed_runs,
    )
    return {
        "context_id": f"model_ctx_{uuid.uuid4().hex[:12]}",
        "generated_at": utc_now(),
        "model_system_version": MODEL_SYSTEM_VERSION,
        "stage": stage,
        "production_contract": production_contract(),
        "active_context": {
            "feature_set_catalog": feature_set_catalog,
            "all_active_pointer": all_active_pointer,
            "round_group": round_payload,
            "seed_runs": compact_seed_runs,
            "training_feature_set_selection": {
                "mode": "explicit_feature_set_id" if requested_feature_set else "context_only",
                "requested_feature_set": requested_feature_set,
                "supports_multiple_feature_sets": True,
            },
        },
        "lineage_context": {
            "selected_feature_set_id": requested_feature_set,
            "available_feature_set_count": feature_set_catalog.get("count"),
            "all_active_feature_set_id": all_active_pointer.get("feature_set_id"),
            "all_active_updates_pointer": all_active_pointer.get("updates_active_feature_pointer"),
        },
        "history_context": {
            "recent_rounds": [_compact_round(row, include_seed_runs=False) for row in recent_rounds[:10]],
            "recent_research_steps": [_compact_research_step(row, include_extra=False) for row in recent_steps[-6:]],
            "recent_registry": compact_registry_recent,
            "production_models": compact_registry_production,
        },
        "llm_evidence_window": llm_evidence_window,
        "tool_evidence": {
            "state_store": "runtime/model/jobs.sqlite",
            "research_steps": "runtime/model/research_steps/current.jsonl",
            "orchestrator_traces": "runtime/model/orchestrator_traces/current.jsonl",
            "mcp_traces": "runtime/model/mcp_traces/current.jsonl",
        },
        "admission_policy": {
            "validation_required_before_gate": True,
            "forbid_default_clean_validation": True,
            "shadow_metrics_production_registry_forbidden": True,
            "hard_blocks": [
                "missing_full_validation_before_gate",
                "shadow_metrics_to_production_registry",
                "invalid_label_processor_portfolio_or_sample_weight_contract",
                "active_values_stale_for_all_active_feature_set",
                "qlib_data_unavailable",
            ],
            "blocker_categories": {
                "hard_contract_blocker": "Stop for label, processor, portfolio, sample-weight, seed policy, schema, or validation contract errors.",
                "external_data_blocker": "Stop for active values stale, Qlib data missing, or service/data availability issues.",
                "schema_warning_or_repairable": "Normalize non-critical fields and record a warning when core execution fields remain valid.",
            },
            "performance_hard_blocks": ["invalid_or_missing_training_backtest_evidence"],
            "legacy_trace_markers_to_ignore_for_current_path": [
                "local_contract_planner",
                "context_recorded_not_called",
            ],
        },
        "round_synthesis_contract": {
            "stage": "round_synthesis",
            "decision_values": ["continue", "checkpoint_stop", "blocked"],
            "transition_rules": {
                "continue": {"next": "experiment_plan", "meaning": "Use actual round evidence to guide the next parameter plan."},
                "checkpoint_stop": {"next": "human_review|checkpoint_stop", "meaning": "Pause after a valid synthesis for manual review or budget stop."},
                "blocked": {"next": "blocker", "meaning": "Only use for missing evidence, invalid state, or an external workflow blocker."},
            },
            "required_fields": [
                "summary",
                "next",
                "round_group_id",
                "previous_parameters",
                "seed42_result",
                "score_summary",
                "gate_summary",
                "validation_summary",
                "parameter_lessons",
                "next_experiment_guidance",
                "next_parameter_change_rationale",
                "evidence_refs",
            ],
            "must_use_actual_seed_metrics": True,
            "forbid_best_seed_or_ensemble_language": True,
            "poor_model_performance_is_not_blocked_if_next_experiment_is_possible": True,
        },
        "human_guidance": human_guidance,
        "feature_set_catalog": feature_set_catalog,
        "all_active_pointer": all_active_pointer,
        "round_group": round_payload,
        "seed_runs": compact_seed_runs,
        "registry": {
            "recent": compact_registry_recent,
            "production": compact_registry_production,
        },
        "allowed_actions": [
            "context",
            "protocol",
            "feature_snapshot",
            "session_start",
            "submit_experiment",
            "run_round",
            "research_score",
            "confirm_research_round",
            "start_production_rolling",
            "round_synthesis",
            "promote",
        ],
        "blocked_actions": [
            "rdagent_execute_main_chain",
            "round_level_sota_scoring",
            "best_seed_selection",
        ],
    }


def write_context_snapshot(context_pack: dict[str, Any], *, root: Path | None = None) -> Path:
    base = root or MODEL_CONTEXT_SNAPSHOTS
    base.mkdir(parents=True, exist_ok=True)
    context_id = str(context_pack.get("context_id") or f"model_ctx_{uuid.uuid4().hex[:12]}")
    path = base / f"{context_id}.json"
    path.write_text(json.dumps(context_pack, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def record_mcp_context(
    stage: str,
    context_pack: dict[str, Any],
    *,
    expected_action: str,
    submitted_payload: dict[str, Any] | None = None,
    validation_result: dict[str, Any] | None = None,
    job_id: str = "",
    run_id: str = "",
    round_group_id: str = "",
    model_run_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    snapshot_path = write_context_snapshot(context_pack)
    context_id = str(context_pack.get("context_id") or snapshot_path.stem)
    event = {
        "mode": "mcp",
        "event_type": "context_snapshot" if submitted_payload is None else "operator_submission",
        "stage": stage,
        "job_id": job_id or run_id,
        "run_id": run_id or job_id,
        "session_id": session_id,
        "round_group_id": round_group_id,
        "model_run_id": model_run_id,
        "context_id": context_id,
        "context_snapshot_path": str(snapshot_path),
        "context_pack": context_pack,
        "expected_action": expected_action,
        "allowed_actions": context_pack.get("allowed_actions", []),
        "blocked_actions": context_pack.get("blocked_actions", []),
    }
    if submitted_payload is not None:
        event["submitted_payload"] = submitted_payload
    if validation_result is not None:
        event["validation_result"] = validation_result
    append_jsonl(MODEL_MCP_TRACES, event)
    return event


def record_orch_trace(
    stage: str,
    *,
    system_prompt: str,
    stage_briefing: str,
    context_pack: dict[str, Any],
    output_contract: dict[str, Any] | None = None,
    parsed_response: dict[str, Any] | None = None,
    result_summary: dict[str, Any] | None = None,
    job_id: str = "",
    round_no: int | None = None,
    round_group_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    context_id = str(context_pack.get("context_id") or f"model_ctx_{uuid.uuid4().hex[:12]}")
    event = {
        "mode": "orch",
        "event_type": "llm_result" if parsed_response is not None else "llm_request",
        "stage": stage,
        "job_id": job_id,
        "session_id": session_id,
        "round_no": round_no,
        "round_group_id": round_group_id,
        "system_prompt": system_prompt,
        "stage_briefing": stage_briefing,
        "context_id": context_id,
        "context_pack": context_pack,
        "output_contract": output_contract or {},
    }
    if parsed_response is not None:
        event["parsed_response"] = parsed_response
        event["result_summary"] = result_summary or {}
    append_jsonl(MODEL_ORCHESTRATOR_TRACES, event)
    return event
