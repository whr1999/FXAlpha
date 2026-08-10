from __future__ import annotations

import json
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any

from storage.model_registry import ModelRegistry
from storage.paths import QLIB_CALENDAR_FILE

from .contracts import MODEL_SYSTEM_VERSION, SOURCE_MODULE, is_model_system_version, production_refit_contract, utc_now
from .feature_set_builder import load_feature_set_manifest
from .paths import MODEL_ACTIVE_PRODUCTION, MODEL_MANUAL_PROMOTION_AUDIT, MODEL_ROLLING_ROOT, MODEL_RUNS_ROOT
from .qlib_runner import _run_direct_qlib_seed_isolated
from .registry_lineage import registry_lineage
from .validation import audit_seed_run


PRODUCTION_REFIT_VERSION = "model_production_refit_v1"


def _calendar_dates() -> list[datetime]:
    try:
        return sorted({datetime.strptime(line.strip(), "%Y-%m-%d") for line in QLIB_CALENDAR_FILE.read_text(encoding="utf-8").splitlines() if line.strip()})
    except (OSError, ValueError):
        return []


def _dynamic_refit_segments(feature_set_id: str, contract: dict[str, Any]) -> dict[str, list[str]]:
    """Anchor the existing train/valid shapes to the latest snapshot trading day."""

    manifest = load_feature_set_manifest(feature_set_id) or {}
    latest_raw = str(manifest.get("actual_end_date") or manifest.get("latest_date") or "")
    calendar = _calendar_dates()
    if not latest_raw or not calendar:
        return dict(contract.get("segments") or {})
    latest = datetime.strptime(latest_raw[:10], "%Y-%m-%d")
    eligible = [value for value in calendar if value <= latest]
    if not eligible:
        return dict(contract.get("segments") or {})
    valid_end = eligible[-1]
    configured = dict(contract.get("segments") or {})
    train_cfg = configured.get("train") or ["2023-01-03", "2025-12-31"]
    valid_cfg = configured.get("valid") or ["2026-01-02", "2026-06-30"]
    train_days = max(252, (datetime.strptime(train_cfg[1], "%Y-%m-%d") - datetime.strptime(train_cfg[0], "%Y-%m-%d")).days)
    valid_days = max(90, (datetime.strptime(valid_cfg[1], "%Y-%m-%d") - datetime.strptime(valid_cfg[0], "%Y-%m-%d")).days)
    valid_floor = valid_end.fromordinal(valid_end.toordinal() - valid_days)
    valid_start = next((value for value in eligible if value >= valid_floor), eligible[0])
    valid_index = eligible.index(valid_start)
    purge_days = 5
    train_end = eligible[max(0, valid_index - purge_days - 1)]
    train_floor = train_end.fromordinal(train_end.toordinal() - train_days)
    train_start = next((value for value in eligible if value >= train_floor), eligible[0])
    fmt = lambda value: value.strftime("%Y-%m-%d")
    return {
        "train": [fmt(train_start), fmt(train_end)],
        "valid": [fmt(valid_start), fmt(valid_end)],
        "test": [fmt(valid_start), fmt(valid_end)],
    }


def _write_active_production_pointer(row: dict[str, Any]) -> None:
    previous: dict[str, Any] = {}
    if MODEL_ACTIVE_PRODUCTION.exists():
        previous = _read_json(MODEL_ACTIVE_PRODUCTION)
    payload = {
        "model_id": row.get("model_id"),
        "model_run_id": row.get("model_run_id"),
        "feature_set_id": row.get("feature_set_id"),
        "run_dir": row.get("run_dir"),
        "activated_at": utc_now(),
        "previous": {key: previous.get(key) for key in ("model_id", "model_run_id", "feature_set_id", "run_dir", "activated_at") if previous.get(key)},
    }
    MODEL_ACTIVE_PRODUCTION.parent.mkdir(parents=True, exist_ok=True)
    temporary = MODEL_ACTIVE_PRODUCTION.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(MODEL_ACTIVE_PRODUCTION)


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata")
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _source_manifest(row: dict[str, Any], md: dict[str, Any]) -> dict[str, Any]:
    artifact_refs = md.get("artifact_refs") if isinstance(md.get("artifact_refs"), dict) else {}
    candidates = [
        artifact_refs.get("manifest"),
        Path(str(row.get("run_dir") or row.get("workspace_path") or "")) / "manifest.json",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate))
        if path.exists():
            manifest = _read_json(path)
            if manifest:
                return manifest
    return {}


def _find_candidate(registry: ModelRegistry, *, model_id: str | None = None, model_run_id: str | None = None) -> dict[str, Any] | None:
    if model_id:
        row = registry.get(model_id)
        return row if row else None
    if model_run_id:
        for row in registry.list_models("candidate"):
            if row.get("model_run_id") == model_run_id:
                return row
    return None


def _manual_exception_source(model_run_id: str | None, reason: str | None) -> dict[str, Any]:
    clean_run_id = str(model_run_id or "").strip()
    clean_reason = str(reason or "").strip()
    if not clean_run_id:
        return {"ok": False, "err": "manual_promotion_model_run_id_required"}
    if len(clean_reason) < 8:
        return {"ok": False, "err": "manual_promotion_reason_required"}
    campaign_path = MODEL_ROLLING_ROOT / clean_run_id / "campaign.json"
    campaign = _read_json(campaign_path)
    if not campaign:
        return {"ok": False, "err": "manual_promotion_rolling_campaign_not_found", "campaign_path": str(campaign_path)}
    if str(campaign.get("campaign_id") or "") != clean_run_id:
        return {"ok": False, "err": "manual_promotion_campaign_identity_mismatch", "campaign_path": str(campaign_path)}
    if str(campaign.get("evaluation_mode") or "") != "production":
        return {"ok": False, "err": "manual_promotion_formal_rolling_required"}
    source_round_group_id = str(campaign.get("source_round_group_id") or "")
    feature_set_id = str(campaign.get("feature_set_id") or "")
    if not source_round_group_id or not feature_set_id:
        return {"ok": False, "err": "manual_promotion_campaign_lineage_missing"}
    seed_results = campaign.get("seed_results") if isinstance(campaign.get("seed_results"), dict) else {}
    seed42 = seed_results.get("42") or seed_results.get(42) or {}
    reliability = seed42.get("reliability") if isinstance(seed42.get("reliability"), dict) else {}
    folds = seed42.get("fold_portfolio_metrics") if isinstance(seed42.get("fold_portfolio_metrics"), dict) else {}
    artifacts = seed42.get("artifacts") if isinstance(seed42.get("artifacts"), dict) else {}
    result_path = Path(str(artifacts.get("result") or ""))
    if seed42.get("status") != "complete" or len(folds) != 4:
        return {"ok": False, "err": "manual_promotion_seed42_fourfold_incomplete"}
    if not reliability or not all(bool(value) for value in reliability.values()):
        return {"ok": False, "err": "manual_promotion_seed42_reliability_failed", "reliability": reliability}
    if not result_path.is_file():
        return {"ok": False, "err": "manual_promotion_seed42_artifact_missing", "path": str(result_path)}
    exception = {
        "policy_version": "model_manual_promotion_exception_v1",
        "reason": clean_reason,
        "requested_at": utc_now(),
        "source_campaign_id": clean_run_id,
        "source_campaign_status": campaign.get("status"),
        "source_campaign_decision": campaign.get("decision"),
        "source_candidate_created": bool(campaign.get("candidate_created")),
        "evidence_path": str(campaign_path),
        "seed42_result_path": str(result_path),
        "fixed_production_refit_seed": 42,
        "gate_bypass_scope": "candidate_admission_only",
        "production_refit_and_validation_required": True,
    }
    source = {
        "model_id": f"manual_exception::{clean_run_id}",
        "model_run_id": clean_run_id,
        "feature_set_id": feature_set_id,
        "status": "candidate",
        "metrics": dict(seed42.get("rolling_metrics") or {}),
        "run_dir": str(campaign_path.parent),
        "workspace_path": str(campaign_path.parent),
        "metadata": {
            "model_system_version": MODEL_SYSTEM_VERSION,
            "evaluation_mode": "production",
            "rolling_campaign_id": clean_run_id,
            "source_round_group_id": source_round_group_id,
            "feature_set_id": feature_set_id,
            "model_run_id": clean_run_id,
            "manual_promotion_exception": exception,
            "artifact_refs": {"manifest": str(campaign_path)},
        },
    }
    return {"ok": True, "source": source, "exception": exception}


def _record_manual_promotion_audit(exception: dict[str, Any], *, status: str, **extra: Any) -> None:
    from .state_store import append_jsonl

    append_jsonl(
        MODEL_MANUAL_PROMOTION_AUDIT,
        {
            **exception,
            **extra,
            "status": status,
            "recorded_at": utc_now(),
        },
    )


def _feature_context(feature_set_id: str) -> tuple[dict[str, Any], str, str]:
    feature_manifest = load_feature_set_manifest(feature_set_id) or {}
    combined_file = str(feature_manifest.get("combined_factors_file") or feature_manifest.get("feature_file") or "")
    latest_date = str(feature_manifest.get("latest_date") or feature_manifest.get("actual_end_date") or "")
    return feature_manifest, combined_file, latest_date


def _repair_production_feature_context(row: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(str(row.get("run_dir") or row.get("workspace_path") or ""))
    manifest_path = run_dir / "manifest.json"
    payload = _read_json(manifest_path)
    feature_set_id = str(row.get("feature_set_id") or payload.get("feature_set_id") or "")
    feature_manifest, combined_file, latest_date = _feature_context(feature_set_id)
    if not payload or not combined_file or not latest_date:
        return {
            "repaired": False,
            "manifest_path": str(manifest_path),
            "feature_set_id": feature_set_id,
            "combined_file": combined_file,
            "latest_date": latest_date,
        }
    changed = False
    for key, value in (
        ("feature_set_manifest", feature_manifest),
        ("platform_combined_factors_file", combined_file),
        ("latest_date", latest_date),
    ):
        if payload.get(key) != value:
            payload[key] = value
            changed = True
    if changed:
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(manifest_path)
    return {
        "repaired": changed,
        "manifest_path": str(manifest_path),
        "feature_set_id": feature_set_id,
        "combined_file": combined_file,
        "latest_date": latest_date,
    }


def _production_run_id(source_model_run_id: str) -> str:
    stamp = utc_now().replace("-", "").replace(":", "").replace("+", "z").replace(".", "")
    short = source_model_run_id.removeprefix("mrun_").removeprefix("m0703_")[:48].strip("_") or "source"
    return f"model_prod_{short}_{stamp}"


def _write_shadow_artifacts(run_dir: Path, metrics: dict[str, Any], model_run_id: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    with (run_dir / "ret.pkl").open("wb") as fh:
        pickle.dump({"shadow_artifact": True, "kind": "production_refit_ret", "model_run_id": model_run_id, "metrics": metrics}, fh)
    with (run_dir / "pred.pkl").open("wb") as fh:
        pickle.dump({"shadow_artifact": True, "kind": "production_refit_pred", "model_run_id": model_run_id}, fh)


def production_refit_model(
    *,
    model_id: str | None = None,
    model_run_id: str | None = None,
    registry: ModelRegistry | None = None,
    execute_qlib: bool = True,
    dry_run: bool = False,
    manual_override_reason: str | None = None,
) -> dict[str, Any]:
    registry = registry or ModelRegistry()
    source = _find_candidate(registry, model_id=model_id, model_run_id=model_run_id)
    manual_exception: dict[str, Any] = {}
    if not source and manual_override_reason:
        resolved = _manual_exception_source(model_run_id, manual_override_reason)
        if not resolved.get("ok"):
            return resolved
        source = dict(resolved.get("source") or {})
        manual_exception = dict(resolved.get("exception") or {})
    if not source:
        return {"ok": False, "err": "candidate_model_not_found"}
    md = _metadata(source)
    if not is_model_system_version(md.get("model_system_version")):
        return {"ok": False, "err": "not_model_asset"}
    if source.get("status") != "candidate":
        return {"ok": False, "err": "only_candidate_can_be_promoted", "status": source.get("status")}
    if md.get("evaluation_mode") != "production" or not md.get("rolling_campaign_id"):
        return {"ok": False, "err": "formal_rolling_candidate_required"}
    contract = production_refit_contract()
    if not contract.get("enabled", True):
        return {"ok": False, "err": "production_refit_disabled"}
    source_model_run_id = str(source.get("model_run_id") or md.get("model_run_id") or "")
    source_round_group_id = str(md.get("source_round_group_id") or "")
    from .state_store import ModelStateStore

    source_round = ModelStateStore().get_round(source_round_group_id)
    if not source_round:
        return {"ok": False, "err": "rolling_source_round_missing", "source_round_group_id": source_round_group_id}
    experiment = dict(source_round.get("experiment") or {})
    if not experiment:
        return {"ok": False, "err": "source_experiment_missing", "source_model_run_id": source_model_run_id}
    feature_set_id = str(source_round.get("feature_set_id") or md.get("feature_set_id") or source.get("feature_set_id") or "")
    feature_set_manifest, platform_combined_factors_file, platform_factor_latest_date = _feature_context(feature_set_id)
    segments = _dynamic_refit_segments(feature_set_id, contract)
    experiment["segments"] = segments
    seed = 42
    existing_for_candidate = next(
        (
            row
            for row in registry.list_models("production")
            if (_metadata(row).get("source_candidate_model_id") == source.get("model_id"))
            or (_metadata(row).get("source_candidate_model_run_id") == source_model_run_id)
        ),
        None,
    )
    if existing_for_candidate and not dry_run:
        feature_context = _repair_production_feature_context(existing_for_candidate)
        _write_active_production_pointer(existing_for_candidate)
        if manual_exception:
            _record_manual_promotion_audit(
                manual_exception,
                status="reused_existing_production",
                production_model_id=existing_for_candidate.get("model_id"),
                production_model_run_id=existing_for_candidate.get("model_run_id"),
                feature_context=feature_context,
            )
        return {
            "ok": True,
            "reused_existing_production": True,
            "production_model_id": existing_for_candidate.get("model_id"),
            "production_model_run_id": existing_for_candidate.get("model_run_id"),
            "source_model_id": source.get("model_id"),
            "source_model_run_id": source_model_run_id,
            "production": existing_for_candidate,
            "feature_context": feature_context,
            "manual_promotion_exception": manual_exception or None,
        }
    production_model_run_id = _production_run_id(source_model_run_id)
    run_dir = MODEL_RUNS_ROOT / production_model_run_id
    existing_production = next(
        (
            row
            for row in registry.list_models("production")
            if str(row.get("model_run_id") or "") == production_model_run_id
        ),
        None,
    )
    if existing_production and not dry_run:
        _write_active_production_pointer(existing_production)
        return {
            "ok": True,
            "reused_existing_production": True,
            "production_model_id": existing_production.get("model_id"),
            "production_model_run_id": production_model_run_id,
            "source_model_id": source.get("model_id"),
            "source_model_run_id": source_model_run_id,
            "production": existing_production,
        }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "source_model_run_id": source_model_run_id,
            "production_model_run_id": production_model_run_id,
            "feature_set_id": feature_set_id,
            "seed": seed,
            "segments": segments,
            "experiment": experiment,
            "manual_promotion_exception": manual_exception or None,
        }
    if manual_exception:
        _record_manual_promotion_audit(
            manual_exception,
            status="production_refit_started",
            production_model_run_id=production_model_run_id,
        )
    if execute_qlib:
        worker_result = _run_direct_qlib_seed_isolated(
            feature_set_id=feature_set_id,
            experiment=experiment,
            seed=seed,
            run_dir=run_dir,
        )
        if not worker_result.get("ok") or not isinstance(worker_result.get("result"), dict):
            if manual_exception:
                _record_manual_promotion_audit(
                    manual_exception,
                    status="production_refit_failed",
                    production_model_run_id=production_model_run_id,
                    error=str(worker_result.get("error") or "production_refit_worker_failed"),
                )
            return {
                "ok": False,
                "err": "production_refit_execution_failed",
                "message": str(worker_result.get("error") or "production_refit_worker_failed"),
                "worker": {key: value for key, value in worker_result.items() if key != "ok"},
                "source_model_run_id": source_model_run_id,
                "production_model_run_id": production_model_run_id,
                "feature_set_id": feature_set_id,
                "segments": segments,
            }
        direct = dict(worker_result.get("result") or {})
        config_audit = direct.get("config_audit") if isinstance(direct.get("config_audit"), dict) else {}
        if config_audit and not config_audit.get("passed"):
            if manual_exception:
                _record_manual_promotion_audit(
                    manual_exception,
                    status="production_refit_failed",
                    production_model_run_id=production_model_run_id,
                    error=str(config_audit.get("backtest_error") or "production_refit_config_audit_failed"),
                )
            return {
                "ok": False,
                "err": "production_refit_execution_failed",
                "message": str(config_audit.get("backtest_error") or "production_refit_config_audit_failed"),
                "source_model_run_id": source_model_run_id,
                "production_model_run_id": production_model_run_id,
                "feature_set_id": feature_set_id,
                "segments": segments,
                "direct_result": direct,
            }
        metrics = dict(direct.get("metrics") or {})
    else:
        metrics = dict(source.get("metrics") or {})
        direct = {"shadow_production_refit": True, "metrics": metrics}
        _write_shadow_artifacts(run_dir, metrics, production_model_run_id)
    direct_artifacts = direct.get("artifacts") if isinstance(direct.get("artifacts"), dict) else {}
    manifest = {
        "model_system_version": MODEL_SYSTEM_VERSION,
        "model_run_id": production_model_run_id,
        "source_model_run_id": source_model_run_id,
        "production_refit_version": PRODUCTION_REFIT_VERSION,
        "seed": seed,
        "feature_set_id": feature_set_id,
        "feature_set_manifest": feature_set_manifest,
        "platform_combined_factors_file": platform_combined_factors_file,
        "latest_date": platform_factor_latest_date,
        "experiment": experiment,
        "resolved_training_params": direct.get("resolved_training_params") or direct.get("resolved_model_params") or {},
        "resolved_reweight_params": direct.get("resolved_reweight_params") or {},
        "resolved_portfolio_params": direct.get("resolved_portfolio_params") or {},
        "resolved_processors": direct.get("resolved_processors") or {},
        "runner": {
            "main_chain": "direct_qlib0627_workflow",
            "execute_qlib": bool(execute_qlib),
            "shadow_contract_runner": not bool(execute_qlib),
            "direct_qlib_error": (direct.get("config_audit") or {}).get("backtest_error") if isinstance(direct.get("config_audit"), dict) else "",
        },
        "direct_qlib": direct,
        "artifacts": {
            "manifest": str(run_dir / "manifest.json"),
            "metrics": str(run_dir / "metrics.json"),
            "ret": direct_artifacts.get("ret") or str(run_dir / "ret.pkl"),
            "pred": direct_artifacts.get("pred") or str(run_dir / "pred.pkl"),
            "label": direct_artifacts.get("label") or str(run_dir / "label.pkl"),
            "params": direct_artifacts.get("params") or str(run_dir / "params.pkl"),
        },
        "generated_at": utc_now(),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    seed_payload = {
        "model_run_id": production_model_run_id,
        "round_group_id": str(md.get("round_group_id") or ""),
        "seed": seed,
        "metrics": metrics,
        "artifact_dir": str(run_dir),
    }
    validation = audit_seed_run(seed_payload)
    if validation.get("hard_blocks") or validation.get("status") in {"blocked", "failed", "reject"}:
        if manual_exception:
            _record_manual_promotion_audit(
                manual_exception,
                status="production_refit_validation_failed",
                production_model_run_id=production_model_run_id,
                validation=validation,
            )
        return {
            "ok": False,
            "err": "production_refit_validation_failed",
            "validation": validation,
            "source_model_run_id": source_model_run_id,
            "production_model_run_id": production_model_run_id,
            "direct_result": direct,
        }
    production_md = {
        **md,
        "model_system_version": MODEL_SYSTEM_VERSION,
        "source_module": SOURCE_MODULE,
        "asset_status": "production",
        "production_refit": {
            "version": PRODUCTION_REFIT_VERSION,
            "source_model_id": source.get("model_id"),
            "source_model_run_id": source_model_run_id,
            "production_model_run_id": production_model_run_id,
            "segments": segments,
            "seed": seed,
            "execute_qlib": bool(execute_qlib),
            "generated_at": utc_now(),
        },
        "source_candidate_model_id": source.get("model_id"),
        "source_candidate_model_run_id": source_model_run_id,
        "model_run_id": production_model_run_id,
        "seed": seed,
        "metrics": metrics,
        "validation": validation,
        "validation_status": validation.get("status"),
        "artifact_refs": {
            "run_dir": str(run_dir),
            "manifest": str(run_dir / "manifest.json"),
            "metrics": str(run_dir / "metrics.json"),
            "ret_pkl": str(run_dir / "ret.pkl"),
            "pred_pkl": str(run_dir / "pred.pkl"),
        },
        "promoted_at": utc_now(),
    }
    lineage = registry_lineage(feature_set_id, {"segments": segments})
    new_model_id = registry.register(
        model_run_id=production_model_run_id,
        feature_set_id=feature_set_id,
        **lineage,
        model_type="FXAlpha model production refit weighted LGBM",
        model_family="lgbm",
        metrics=metrics,
        run_dir=str(run_dir),
        workspace_path=str(run_dir),
        status="production",
        metadata=production_md,
    )
    production_row = registry.get(new_model_id) or {}
    _write_active_production_pointer(production_row)
    if manual_exception:
        _record_manual_promotion_audit(
            manual_exception,
            status="promoted",
            production_model_id=new_model_id,
            production_model_run_id=production_model_run_id,
            validation=validation,
        )
    return {
        "ok": True,
        "production_model_id": new_model_id,
        "production_model_run_id": production_model_run_id,
        "source_model_id": source.get("model_id"),
        "source_model_run_id": source_model_run_id,
        "production": production_row,
        "validation": validation,
        "production_refit": production_md["production_refit"],
        "manual_promotion_exception": manual_exception or None,
    }
