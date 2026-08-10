from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

from storage.model_registry import ModelRegistry

from .contracts import (
    DEFAULT_FEATURE_MISSING_STRATEGY,
    DEFAULT_SAMPLE_WEIGHT_POLICY,
    GATE_VERSION,
    MODEL_SYSTEM_VERSION,
    SCORE_REVIEW_VERSION,
    SEED_SOTA_SCORE_THRESHOLD,
    SOURCE_MODULE,
    utc_now,
)
from .forward_test import run_forward_test
from .production_refit import production_refit_model
from .scoring import performance_hard_blocks
from .state_store import ModelStateStore
from .validation import audit_seed_run


def _validation_for(model_run_id: str, validation_by_run: dict[str, Any] | None = None) -> dict[str, Any]:
    if validation_by_run and model_run_id in validation_by_run:
        return dict(validation_by_run[model_run_id])
    return {}


def _validation_for_seed_run(seed_run: dict[str, Any], validation_by_run: dict[str, Any] | None = None) -> dict[str, Any]:
    model_run_id = str(seed_run.get("model_run_id") or "")
    supplied = _validation_for(model_run_id, validation_by_run)
    if supplied:
        return supplied
    cached = seed_run.get("validation")
    if isinstance(cached, dict) and cached:
        return dict(cached)
    return audit_seed_run(seed_run)


def _gate_status(validation: dict[str, Any]) -> str:
    status = str(validation.get("status") or validation.get("validation_status") or "").lower()
    hard_blocks = validation.get("hard_blocks") or validation.get("blocking_findings") or []
    if hard_blocks or status in {"blocked", "reject", "failed"}:
        return "reject"
    warnings = validation.get("warnings") or validation.get("review_required") or []
    if warnings or status in {"review_required", "warning", "warn"}:
        return "pass_with_warnings"
    if status in {"clean", "passed", "pass", "ok"}:
        return "pass"
    return "pass_with_warnings"


def _artifact_audit(seed_run: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(str(seed_run.get("artifact_dir") or ""))
    required = {
        "manifest": run_dir / "manifest.json",
        "metrics": run_dir / "metrics.json",
        "ret": run_dir / "ret.pkl",
        "pred": run_dir / "pred.pkl",
    }
    errors: list[str] = []
    warnings: list[str] = []
    readable: dict[str, bool] = {}
    for name, path in required.items():
        if not path.exists():
            errors.append(f"{name}_missing:{path}")
            readable[name] = False
            continue
        try:
            if path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
            elif path.suffix == ".pkl":
                with path.open("rb") as fh:
                    payload = pickle.load(fh)
                if name in {"ret", "pred"} and hasattr(payload, "empty") and bool(payload.empty):
                    errors.append(f"{name}_empty:{path}")
                    readable[name] = False
                    continue
            readable[name] = True
        except Exception as exc:
            errors.append(f"{name}_unreadable:{exc}")
            readable[name] = False
    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "run_dir": str(run_dir),
        "readable": readable,
    }


def _session_id_for_round(state: ModelStateStore, round_group_id: str) -> str:
    try:
        for session in state.list_sessions(limit=100):
            if round_group_id in set(session.get("round_group_ids") or []):
                return str(session.get("session_id") or "")
    except Exception:
        return ""
    return ""


def _metadata(round_payload: dict[str, Any], seed_run: dict[str, Any], gate_payload: dict[str, Any], *, session_id: str = "") -> dict[str, Any]:
    experiment = dict(round_payload.get("experiment") or {})
    score = dict(seed_run.get("score") or {})
    validation = dict(gate_payload.get("validation") or {})
    validation_status = validation.get("status") or validation.get("validation_status")
    artifact_dir = str(seed_run.get("artifact_dir") or "")
    manifest: dict[str, Any] = {}
    if artifact_dir:
        try:
            manifest = json.loads((Path(artifact_dir) / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    manifest_artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    direct = manifest.get("direct_qlib") if isinstance(manifest.get("direct_qlib"), dict) else {}
    direct_artifacts = direct.get("artifacts") if isinstance(direct.get("artifacts"), dict) else {}
    resolved_portfolio = manifest.get("resolved_portfolio_params") if isinstance(manifest.get("resolved_portfolio_params"), dict) else {}
    merged_artifacts = {**direct_artifacts, **manifest_artifacts}
    artifact_refs = {
        "run_dir": artifact_dir,
        "manifest": str(Path(artifact_dir) / "manifest.json") if artifact_dir else "",
        "metrics": str(Path(artifact_dir) / "metrics.json") if artifact_dir else "",
        "ret_pkl": str(Path(artifact_dir) / "ret.pkl") if artifact_dir else "",
        "pred_pkl": str(Path(artifact_dir) / "pred.pkl") if artifact_dir else "",
        "label_pkl": merged_artifacts.get("label") or (str(Path(artifact_dir) / "label.pkl") if artifact_dir else ""),
        "params_pkl": merged_artifacts.get("params") or (str(Path(artifact_dir) / "params.pkl") if artifact_dir else ""),
    }
    return {
        "model_system_version": MODEL_SYSTEM_VERSION,
        "source_module": SOURCE_MODULE,
        "model_run_id": seed_run.get("model_run_id"),
        "round_group_id": seed_run.get("round_group_id"),
        "session_id": session_id,
        "feature_set_id": round_payload.get("feature_set_id"),
        "experiment_signature": round_payload.get("experiment_signature"),
        "seed": seed_run.get("seed"),
        "sota_score": score.get("sota_score"),
        "score_review_version": score.get("score_review_version") or SCORE_REVIEW_VERSION,
        "score_review_decision": score.get("decision"),
        "gate_version": GATE_VERSION,
        "gate_status": gate_payload.get("gate_status"),
        "asset_status": gate_payload.get("asset_status"),
        "validation_required": True,
        "validation": validation,
        "validation_status": validation_status,
        "validation_rule_version": validation.get("validation_rule_version"),
        "validation_artifact_path": validation.get("artifact_path"),
        "validation_hard_blocks": validation.get("hard_blocks") or validation.get("blocking_findings") or [],
        "validation_warnings": validation.get("warnings") or validation.get("review_required") or [],
        "forward_test": seed_run.get("forward") or gate_payload.get("forward_test") or {},
        "artifact_refs": artifact_refs,
        "artifacts": merged_artifacts,
        "resolved_portfolio_params": resolved_portfolio,
        "feature_snapshot_policy_version": experiment.get("feature_snapshot_policy_version"),
        "feature_missing_strategy": experiment.get("feature_missing_strategy") or DEFAULT_FEATURE_MISSING_STRATEGY,
        "sample_weight_policy": experiment.get("sample_weight_policy") or DEFAULT_SAMPLE_WEIGHT_POLICY,
        "sample_weight_kwargs": experiment.get("sample_weight_kwargs") or {},
        "portfolio": "top20/drop2/hold5",
        "benchmark": experiment.get("benchmark") or "000300sh",
    }


def _register_or_update_seed_model(
    registry: ModelRegistry,
    *,
    model_run_id: str,
    feature_set_id: str,
    metrics: dict[str, Any],
    run_dir: str,
    asset_status: str,
    metadata: dict[str, Any],
) -> tuple[str, str]:
    existing = None
    for row in registry.list_models("all"):
        if row.get("model_run_id") == model_run_id:
            existing = row
            break
    if existing:
        existing_status = str(existing.get("status") or "")
        effective_asset_status = asset_status
        if existing_status == "production":
            metadata = {
                **metadata,
                "gate_asset_status_before_registry_preserve": asset_status,
                "asset_status": "production",
                "production_status_preserved": True,
            }
            effective_asset_status = "production"
        registry.update_run_result(
            model_run_id=model_run_id,
            metrics=metrics,
            workspace_path=run_dir,
            run_dir=run_dir,
            status=effective_asset_status,
            metadata=metadata,
        )
        return str(existing["model_id"]), effective_asset_status
    model_id = registry.register(
        model_run_id=model_run_id,
        feature_set_id=feature_set_id,
        model_type="FXAlpha model weighted LGBM",
        model_family="lgbm",
        metrics=metrics,
        run_dir=run_dir,
        workspace_path=run_dir,
        status=asset_status,
        metadata=metadata,
    )
    return model_id, asset_status


def run_sota_gate(
    *,
    round_group_id: str,
    state: ModelStateStore | None = None,
    registry: ModelRegistry | None = None,
    validation_by_run: dict[str, Any] | None = None,
    threshold: float = SEED_SOTA_SCORE_THRESHOLD,
    run_forward: bool = True,
) -> dict[str, Any]:
    state = state or ModelStateStore()
    registry = registry or ModelRegistry()
    round_payload = state.get_round(round_group_id)
    if not round_payload:
        return {"ok": False, "err": "round_group_not_found", "round_group_id": round_group_id}
    forward_result = (
        run_forward_test(round_group_id=round_group_id, state=state, threshold=threshold)
        if run_forward
        else {"ok": True, "round_group_id": round_group_id, "skipped_by_caller": True}
    )
    if not forward_result.get("ok"):
        return {"ok": False, "err": "forward_test_failed", "round_group_id": round_group_id, "forward_result": forward_result}
    outputs: list[dict[str, Any]] = []
    for seed_run in state.list_seed_runs(round_group_id=round_group_id):
        score = dict(seed_run.get("score") or {})
        forward = dict(seed_run.get("forward") or {})
        sota_score = float(score.get("sota_score") or 0.0)
        hard_blocks = list(score.get("hard_blocks") or [])
        hard_blocks.extend(block for block in performance_hard_blocks(seed_run.get("metrics") or {}) if block not in hard_blocks)
        model_run_id = seed_run["model_run_id"]
        validation: dict[str, Any] = {}
        if hard_blocks:
            gate_status = "reject"
            asset_status = "archived"
            reason = "performance_hard_block"
            validation = {"hard_blocks": sorted(set(hard_blocks)), "validation_skipped": "performance_hard_block"}
        elif sota_score < threshold:
            gate_status = "reject"
            asset_status = "archived"
            reason = "below_sota_score_threshold"
            validation = {"validation_skipped": "below_sota_score_threshold"}
        elif forward.get("status") == "reject":
            gate_status = "reject"
            asset_status = "archived"
            reason = "forward_test_reject"
            validation = {
                "hard_blocks": ["forward_test_reject"],
                "forward_test": forward,
                "validation_skipped": "forward_test_reject",
            }
        elif forward.get("status") not in {"pass", "watch"}:
            gate_status = "reject"
            asset_status = "archived"
            reason = "forward_test_required"
            validation = {
                "hard_blocks": ["forward_test_required"],
                "forward_test": forward,
                "validation_skipped": "forward_test_required",
            }
        else:
            validation = _validation_for_seed_run(seed_run, validation_by_run)
            if validation:
                seed_run = state.upsert_seed_run({**seed_run, "validation": validation})
            if forward.get("status") == "watch":
                warnings = list(validation.get("warnings") or validation.get("review_required") or [])
                warnings.append("forward_test_watch")
                validation = {**validation, "warnings": sorted(set(warnings)), "forward_test": forward}
            else:
                validation = {**validation, "forward_test": forward}
            artifact_audit = _artifact_audit(seed_run)
            if not artifact_audit["passed"]:
                validation = {**validation, "artifact_audit": artifact_audit}
                gate_status = "reject"
            else:
                validation = {**validation, "artifact_audit": artifact_audit}
                gate_status = _gate_status(validation)
                if forward.get("status") == "watch" and gate_status == "pass":
                    gate_status = "pass_with_warnings"
            asset_status = "candidate" if gate_status in {"pass", "pass_with_warnings"} else "archived"
            reason = "gate_" + gate_status
        gate_payload = {
            "gate_version": GATE_VERSION,
            "model_run_id": model_run_id,
            "round_group_id": round_group_id,
            "seed": int(seed_run["seed"]),
            "sota_score": sota_score,
            "threshold": threshold,
            "gate_status": gate_status,
            "asset_status": asset_status,
            "reason": reason,
            "forward_test": forward,
            "validation": validation,
            "generated_at": utc_now(),
        }
        md = _metadata(round_payload, seed_run, gate_payload, session_id=_session_id_for_round(state, round_group_id))
        model_id, effective_asset_status = _register_or_update_seed_model(
            registry,
            model_run_id=model_run_id,
            feature_set_id=round_payload.get("feature_set_id", ""),
            metrics=seed_run.get("metrics") or {},
            run_dir=seed_run.get("artifact_dir") or "",
            asset_status=asset_status,
            metadata=md,
        )
        if effective_asset_status != asset_status:
            gate_payload["gate_asset_status_before_registry_preserve"] = asset_status
            gate_payload["asset_status"] = effective_asset_status
            gate_payload["reason"] = "production_status_preserved"
        updated = state.upsert_seed_run(
            {
                **seed_run,
                "gate": gate_payload,
                "validation": validation,
                "registry_status": effective_asset_status,
                "registry_model_id": model_id,
            }
        )
        outputs.append({**gate_payload, "registry_model_id": model_id, "seed_run": updated})
    round_payload["stage"] = "registry_write"
    round_payload["updated_at"] = utc_now()
    state.upsert_round(round_payload)
    return {"ok": True, "round_group_id": round_group_id, "forward_result": forward_result, "results": outputs}


def promote_model(
    model_id: str | None = None,
    model_run_id: str | None = None,
    *,
    registry: ModelRegistry | None = None,
    execute_qlib: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    return production_refit_model(
        model_id=model_id,
        model_run_id=model_run_id,
        registry=registry,
        execute_qlib=execute_qlib,
        dry_run=dry_run,
    )
