from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from storage.model_registry import ModelRegistry
from storage.paths import MODEL_CONFIRMATION_SEEDS, MODEL_RESEARCH_SCORING

from .contracts import MODEL_SYSTEM_VERSION, SCORE_REVIEW_VERSION, SOURCE_MODULE, utc_now
from .qlib_runner import run_round
from .registry_lineage import registry_lineage
from .scoring import individual_performance_score, individual_score_components, performance_hard_blocks
from .state_store import ModelStateStore
from .validation import audit_seed_run


def _metric(row: dict[str, Any], *keys: str) -> float:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    for key in keys:
        if metrics.get(key) is not None:
            return float(metrics[key])
    return 0.0


def _register_research_seed(
    registry: ModelRegistry,
    round_row: dict[str, Any],
    seed_run: dict[str, Any],
    *,
    confirmation: dict[str, Any],
) -> tuple[str, str]:
    model_run_id = str(seed_run.get("model_run_id") or "")
    validation = audit_seed_run(seed_run)
    hard_flaw = bool(validation.get("hard_blocks") or validation.get("blocking_findings"))
    status = "archived" if hard_flaw else "research"
    metadata = {
        "model_system_version": MODEL_SYSTEM_VERSION,
        "source_module": SOURCE_MODULE,
        "evaluation_mode": "research",
        "model_run_id": model_run_id,
        "round_group_id": round_row.get("round_group_id"),
        "feature_set_id": round_row.get("feature_set_id"),
        "seed": seed_run.get("seed"),
        "research_score": (seed_run.get("score") or {}).get("research_score"),
        "confirmed_research_score": confirmation.get("confirmed_research_score"),
        "research_confirmation": confirmation,
        "validation": validation,
        "asset_status": status,
        "portfolio": "top20/drop2/hold5",
    }
    lineage = registry_lineage(str(round_row.get("feature_set_id") or ""), dict(round_row.get("experiment") or {}))
    existing = next((row for row in registry.list_models("all") if row.get("model_run_id") == model_run_id), None)
    if existing:
        registry.update_run_result(
            model_run_id=model_run_id,
            metrics=seed_run.get("metrics") or {},
            run_dir=str(seed_run.get("artifact_dir") or ""),
            workspace_path=str(seed_run.get("artifact_dir") or ""),
            feature_set_id=str(round_row.get("feature_set_id") or ""),
            **lineage,
            status=status,
            metadata=metadata,
        )
        return str(existing.get("model_id") or ""), status
    model_id = registry.register(
        model_run_id=model_run_id,
        feature_set_id=str(round_row.get("feature_set_id") or ""),
        **lineage,
        model_type="FXAlpha model research LGBM",
        model_family="lgbm",
        metrics=seed_run.get("metrics") or {},
        run_dir=str(seed_run.get("artifact_dir") or ""),
        workspace_path=str(seed_run.get("artifact_dir") or ""),
        status=status,
        metadata=metadata,
    )
    return model_id, status


def score_research_confirmation(
    round_group_id: str,
    *,
    state: ModelStateStore | None = None,
    registry: ModelRegistry | None = None,
    write_registry: bool = True,
) -> dict[str, Any]:
    state = state or ModelStateStore()
    rows = [row for row in state.list_seed_runs(round_group_id=round_group_id) if row.get("status") == "completed"]
    by_seed = {int(row.get("seed")): row for row in rows}
    required = {42, *[int(seed) for seed in MODEL_CONFIRMATION_SEEDS]}
    if set(by_seed) != required:
        return {"ok": False, "err": "research_confirmation_requires_complete_42_17_83", "found": sorted(by_seed)}

    scores: list[float] = []
    ir_values: list[float] = []
    return_values: list[float] = []
    drawdowns: list[float] = []
    seed_results: list[dict[str, Any]] = []
    hard_flaws: list[str] = []
    for seed in (42, *[int(value) for value in MODEL_CONFIRMATION_SEEDS]):
        row = by_seed[seed]
        metrics = dict(row.get("metrics") or {})
        score = individual_performance_score(metrics)
        components, warnings = individual_score_components(metrics)
        blocks = performance_hard_blocks(metrics)
        if blocks:
            hard_flaws.extend(f"seed_{seed}:{block}" for block in blocks)
        score_payload = {
            "score_review_version": SCORE_REVIEW_VERSION,
            "phase": "confirmation",
            "seed": seed,
            "model_run_id": row.get("model_run_id"),
            "research_score": score,
            "component_scores": components,
            "warnings": warnings,
            "hard_blocks": blocks,
            "generated_at": utc_now(),
        }
        state.upsert_seed_run({**row, "score": score_payload})
        scores.append(score)
        ir_values.append(_metric(row, "excess_information_ratio_with_cost", "sharpe"))
        return_values.append(_metric(row, "excess_annualized_ret_with_cost", "annualized_ret"))
        drawdowns.append(abs(_metric(row, "max_drawdown")))
        seed_results.append({
            "seed": seed,
            "research_score": score,
            "excess_information_ratio_with_cost": ir_values[-1],
            "excess_annualized_ret_with_cost": return_values[-1],
            "abs_max_drawdown": drawdowns[-1],
        })

    cfg = dict(MODEL_RESEARCH_SCORING)
    confirmed_score = round(float(statistics.median(scores)), 3)
    seed42_score = scores[0]
    ir_std = float(statistics.pstdev(ir_values))
    return_std = float(statistics.pstdev(return_values))
    gates = {
        "no_research_hard_flaw": not hard_flaws,
        "at_least_two_positive_ir": sum(value > 0 for value in ir_values) >= 2,
        "worst_seed_drawdown_within_limit": max(drawdowns) <= float(cfg.get("max_worst_seed_drawdown", 0.30)),
        "ir_std_within_limit": ir_std <= float(cfg.get("max_ir_std", 0.60)),
        "return_std_within_limit": return_std <= float(cfg.get("max_return_std", 0.20)),
        "confirmed_score_drop_within_limit": confirmed_score >= seed42_score - float(cfg.get("confirmation_score_drop_limit", 10.0)),
    }
    confirmation = {
        "status": "passed" if all(gates.values()) else "failed",
        "confirmed_research_score": confirmed_score,
        "seed42_research_score": seed42_score,
        "ir_std": round(ir_std, 6),
        "return_std": round(return_std, 6),
        "hard_flaws": hard_flaws,
        "gates": gates,
        "seed_results": seed_results,
        "generated_at": utc_now(),
    }
    round_row = state.get_round(round_group_id) or {}
    experiment = dict(round_row.get("experiment") or {})
    metadata = dict(experiment.get("research_metadata") or {})
    metadata.update({"research_confirmation": confirmation, "confirmed_research_score": confirmed_score})
    experiment["research_metadata"] = metadata
    round_row.update({"experiment": experiment, "stage": "research_confirmation", "updated_at": utc_now()})
    state.upsert_round(round_row)

    if write_registry:
        registry = registry or ModelRegistry()
        official_seed_run = next(
            (row for row in state.list_seed_runs(round_group_id=round_group_id) if int(row.get("seed") or -1) == 42),
            None,
        )
        if official_seed_run:
            model_id, status = _register_research_seed(registry, round_row, official_seed_run, confirmation=confirmation)
            state.upsert_seed_run({**official_seed_run, "registry_model_id": model_id, "registry_status": status})
    return {"ok": True, "round_group_id": round_group_id, "confirmation": confirmation}


def register_research_screening_round(
    round_group_id: str,
    *,
    state: ModelStateStore | None = None,
    registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    state = state or ModelStateStore()
    round_row = state.get_round(round_group_id)
    if not round_row:
        return {"ok": False, "err": "round_group_not_found"}
    rows = [row for row in state.list_seed_runs(round_group_id=round_group_id) if int(row.get("seed") or -1) == 42]
    if len(rows) != 1 or (rows[0].get("score") or {}).get("research_score") is None:
        return {"ok": False, "err": "screening_score_required"}
    registry = registry or ModelRegistry()
    confirmation = {"status": "not_run", "reason": "session_best_confirmation_only"}
    model_id, status = _register_research_seed(registry, round_row, rows[0], confirmation=confirmation)
    state.upsert_seed_run({**rows[0], "registry_model_id": model_id, "registry_status": status})
    return {"ok": True, "model_id": model_id, "asset_status": status}


def confirm_research_round(
    round_group_id: str,
    *,
    execute_qlib: bool = False,
    state: ModelStateStore | None = None,
    registry: ModelRegistry | None = None,
    write_registry: bool = True,
) -> dict[str, Any]:
    state = state or ModelStateStore()
    result = run_round(
        round_group_id=round_group_id,
        state=state,
        execute_qlib=execute_qlib,
        seeds=[int(seed) for seed in MODEL_CONFIRMATION_SEEDS],
        phase="confirmation",
    )
    if not result.get("ok"):
        return result
    return score_research_confirmation(
        round_group_id,
        state=state,
        registry=registry,
        write_registry=write_registry,
    )
