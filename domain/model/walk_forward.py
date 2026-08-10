from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from storage.model_registry import ModelRegistry
from storage.paths import MODEL_ROLLING, MODEL_RUNTIME_ROOT, PROJECT_ROOT

from .contracts import DEFAULT_PORTFOLIO, MODEL_SYSTEM_VERSION, rolling_contract, utc_now
from .rolling_scoring import score_rolling_campaign, score_rolling_seed
from .registry_lineage import registry_lineage
from .state_store import ModelStateStore


ROLLING_ROOT = MODEL_RUNTIME_ROOT / "rolling"
SeedRunner = Callable[[int, str, str, str], dict[str, Any]]


def _default_seed_runner(seed: int, campaign_id: str, feature_set_id: str, source_round_group_id: str) -> dict[str, Any]:
    run_id = f"{campaign_id}_seed{seed}"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "model_rolling_fourfold_test.py"),
        "--run-id",
        run_id,
        "--feature-set-id",
        feature_set_id,
        "--source-round-group-id",
        source_round_group_id,
        "--seed",
        str(seed),
        "--max-folds",
        "4",
    ]
    timeout_seconds = max(60, int(os.getenv("MODEL_ROLLING_SEED_TIMEOUT_SECONDS", "14400")))
    try:
        proc = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "failed",
            "seed": seed,
            "error": f"rolling_seed_timeout:{timeout_seconds}",
            "stdout_tail": str(exc.stdout or "")[-4000:],
            "stderr_tail": str(exc.stderr or "")[-4000:],
        }
    result_path = MODEL_RUNTIME_ROOT / "rolling_diagnostics" / run_id / "result.json"
    if proc.returncode != 0 or not result_path.exists():
        return {
            "status": "failed",
            "seed": seed,
            "error": f"rolling_seed_runner_failed:{proc.returncode}",
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
    return json.loads(result_path.read_text(encoding="utf-8"))


def _preliminary_gates(seed_result: dict[str, Any], seed_score: dict[str, Any]) -> dict[str, bool]:
    folds = list((seed_result.get("fold_portfolio_metrics") or {}).values())
    fold_ir = [float(row.get("excess_information_ratio_with_cost") or 0.0) for row in folds]
    rolling_metrics = seed_result.get("rolling_metrics") or {}
    reliability = seed_result.get("reliability") or {}
    threshold = float(MODEL_ROLLING.get("preliminary_score_threshold", 60.0))
    return {
        "four_folds_complete": len(folds) == 4,
        "reliability_passed": bool(reliability) and all(bool(value) for value in reliability.values()),
        "preliminary_score_reached": bool(seed_score.get("ok")) and float(seed_score.get("score") or 0.0) >= threshold,
        "at_least_three_positive_fold_ir": sum(value > 0 for value in fold_ir) >= 3,
        "latest_fold_ir_positive": bool(fold_ir) and fold_ir[-1] > 0,
        "drawdown_within_limit": abs(float(rolling_metrics.get("max_drawdown") or 0.0)) <= float(MODEL_ROLLING.get("max_median_drawdown", 0.30)),
    }


def _register_candidate(
    *,
    registry: ModelRegistry,
    campaign_id: str,
    source_round: dict[str, Any],
    campaign_result: dict[str, Any],
    campaign_dir: Path,
) -> str:
    seed_results = campaign_result["seed_results"]
    # Candidate performance is the official Seed42 Rolling result.  Seed17/83
    # remain admission/stability evidence and never replace the formal model by
    # having a better observed backtest.
    metrics = dict((seed_results.get(42) or {}).get("rolling_metrics") or {})
    metadata = {
        "model_system_version": MODEL_SYSTEM_VERSION,
        "evaluation_mode": "production",
        "asset_status": "candidate",
        "rolling_campaign_id": campaign_id,
        "source_round_group_id": source_round.get("round_group_id"),
        "feature_set_id": source_round.get("feature_set_id"),
        "rolling_score": campaign_result["score"].get("rolling_score"),
        "rolling_score_version": campaign_result["score"].get("score_version"),
        "rolling_gates": campaign_result["score"].get("gates"),
        "seed_policy": rolling_contract()["seed_policy"],
        "portfolio": "top20/drop2/hold5",
        "evidence_path": str(campaign_dir / "campaign.json"),
        "production_refit_seed": 42,
        "best_seed_selection_allowed": False,
    }
    model_run_id = f"rolling_{campaign_id}"
    feature_set_id = str(source_round.get("feature_set_id") or "")
    lineage = registry_lineage(feature_set_id, dict(source_round.get("experiment") or {}))
    existing = next((row for row in registry.list_models("all") if row.get("model_run_id") == model_run_id), None)
    if existing:
        registry.update_run_result(
            model_run_id=model_run_id,
            metrics=metrics,
            run_dir=str(campaign_dir),
            workspace_path=str(campaign_dir),
            feature_set_id=feature_set_id,
            **lineage,
            status="candidate",
            metadata=metadata,
        )
        return str(existing.get("model_id") or "")
    return registry.register(
        model_run_id=model_run_id,
        feature_set_id=feature_set_id,
        **lineage,
        model_type="FXAlpha model rolling candidate",
        model_family="lgbm",
        metrics=metrics,
        run_dir=str(campaign_dir),
        workspace_path=str(campaign_dir),
        status="candidate",
        metadata=metadata,
    )


def start_production_rolling(
    source_round_group_id: str,
    *,
    state: ModelStateStore | None = None,
    registry: ModelRegistry | None = None,
    seed_runner: SeedRunner | None = None,
    write_registry: bool = True,
    campaign_id: str | None = None,
    job_id: str | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    state = state or ModelStateStore()
    source_round = state.get_round(source_round_group_id)
    if not source_round:
        return {"ok": False, "err": "source_round_group_not_found", "source_round_group_id": source_round_group_id}
    metadata = ((source_round.get("experiment") or {}).get("research_metadata") or {})
    confirmation = metadata.get("research_confirmation") if isinstance(metadata, dict) else {}
    if not isinstance(confirmation, dict) or confirmation.get("status") != "passed":
        return {"ok": False, "err": "research_confirmation_pass_required", "source_round_group_id": source_round_group_id}
    campaign_id = campaign_id or f"model_roll_{utc_now().replace(':', '').replace('-', '').replace('+', '_')}"
    campaign_dir = ROLLING_ROOT / campaign_id
    campaign_dir.mkdir(parents=True, exist_ok=True)
    runner = seed_runner or _default_seed_runner
    feature_set_id = str(source_round.get("feature_set_id") or "")

    campaign_path = campaign_dir / "campaign.json"
    previous: dict[str, Any] = {}
    if resume and campaign_path.exists():
        try:
            previous = json.loads(campaign_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            previous = {}
    seed_results: dict[int, dict[str, Any]] = {
        int(seed): dict(result)
        for seed, result in dict(previous.get("seed_results") or {}).items()
        if str(seed).isdigit() and isinstance(result, dict)
    }
    base = {
        "schema_version": "model_rolling_campaign_v1",
        "campaign_id": campaign_id,
        "evaluation_mode": "production",
        "source_round_group_id": source_round_group_id,
        "feature_set_id": feature_set_id,
        "portfolio": dict(DEFAULT_PORTFOLIO),
        "rolling_contract": rolling_contract(),
        "seed_results": seed_results,
        "started_at": previous.get("started_at") or utc_now(),
        "updated_at": utc_now(),
        "status": "running",
    }
    campaign_path.write_text(json.dumps(base, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    if job_id and state.job_stop_requested(job_id):
        result = {**base, "ok": True, "status": "interrupted", "decision": "operator_stop", "completed_at": utc_now()}
        campaign_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return result

    seed42 = seed_results.get(42) if (seed_results.get(42) or {}).get("status") == "complete" else runner(42, campaign_id, feature_set_id, source_round_group_id)
    seed_results[42] = seed42
    preliminary_score = score_rolling_seed(seed42) if seed42.get("status") == "complete" else {"ok": False, "err": seed42.get("error") or "seed42_failed"}
    preliminary_gates = _preliminary_gates(seed42, preliminary_score) if preliminary_score.get("ok") else {"seed42_complete": False}
    preliminary_passed = all(preliminary_gates.values())
    base.update({"preliminary": {"score": preliminary_score, "gates": preliminary_gates, "passed": preliminary_passed}, "seed_results": seed_results, "updated_at": utc_now()})
    campaign_path.write_text(json.dumps(base, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if job_id and state.job_stop_requested(job_id):
        result = {**base, "ok": True, "status": "interrupted", "decision": "operator_stop", "completed_at": utc_now()}
        campaign_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return result
    if not preliminary_passed:
        result = {**base, "ok": True, "status": "research", "decision": "stop_after_seed42", "candidate_created": False, "completed_at": utc_now()}
        campaign_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return result

    for seed in (17, 83):
        if (seed_results.get(seed) or {}).get("status") != "complete":
            seed_results[seed] = runner(seed, campaign_id, feature_set_id, source_round_group_id)
        base.update({"seed_results": seed_results, "updated_at": utc_now()})
        campaign_path.write_text(json.dumps(base, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        if job_id and state.job_stop_requested(job_id):
            result = {**base, "ok": True, "status": "interrupted", "decision": "operator_stop", "completed_at": utc_now()}
            campaign_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            return result
    if any(row.get("status") != "complete" for row in seed_results.values()):
        result = {**base, "seed_results": seed_results, "ok": False, "status": "failed", "err": "rolling_confirmation_seed_failed", "completed_at": utc_now()}
        campaign_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return result

    score = score_rolling_campaign(seed_results)
    candidate_passed = bool(score.get("ok") and score.get("candidate_passed"))
    result = {
        **base,
        "seed_results": seed_results,
        "score": score,
        "ok": True,
        "status": "candidate" if candidate_passed else "research",
        "decision": "candidate" if candidate_passed else "rolling_gate_failed",
        "candidate_created": candidate_passed and write_registry,
        "completed_at": utc_now(),
    }
    campaign_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if candidate_passed and write_registry:
        registry = registry or ModelRegistry()
        result["candidate_model_id"] = _register_candidate(
            registry=registry,
            campaign_id=campaign_id,
            source_round=source_round,
            campaign_result=result,
            campaign_dir=campaign_dir,
        )
        campaign_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result
