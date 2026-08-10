from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from domain.factor_research.active_values_store import current_active_registry_fingerprint
from storage.paths import QLIB_CALENDAR_FILE, QLIB_DATA_ROOT, QLIB_INDEX_META

from .contracts import LABEL_CONTRACT
from .feature_sets import (
    active_values_readiness,
    feature_set_freshness,
    load_feature_set_manifest,
    validate_feature_set_manifest_for_model,
)


def _qlib_data_availability() -> dict[str, Any]:
    required = {
        "qlib_data_root": QLIB_DATA_ROOT,
        "calendar_day": QLIB_CALENDAR_FILE,
        "index_meta": QLIB_INDEX_META,
    }
    missing = [name for name, path in required.items() if not Path(path).exists()]
    return {
        "passed": not missing,
        "missing": missing,
        "qlib_data_root": str(QLIB_DATA_ROOT),
        "calendar_day": str(QLIB_CALENDAR_FILE),
        "index_meta": str(QLIB_INDEX_META),
    }


def _label0_column_present(manifest: dict[str, Any]) -> tuple[bool, str]:
    path_raw = str(manifest.get("combined_factors_file") or manifest.get("feature_file") or "")
    if not path_raw:
        return False, "combined_factors_file_missing"
    path = Path(path_raw)
    if not path.exists():
        return False, f"combined_factors_file_not_found:{path}"
    try:
        columns = pd.read_parquet(path, engine="pyarrow").columns
    except Exception as exc:
        return False, f"combined_factors_file_unreadable:{exc}"
    if ("label", "LABEL0") in columns:
        return True, ""
    if "LABEL0" in columns:
        return True, "label0_flat_column_detected"
    return False, "label0_column_missing"


def _label0_contract(manifest: dict[str, Any] | None) -> dict[str, Any]:
    manifest = manifest or {}
    label_present, label_warning = _label0_column_present(manifest) if manifest else (False, "feature_set_manifest_missing")
    checks = {
        "label_name": "LABEL0",
        "label_forward_period": int(manifest.get("label_forward_period") or 0) == int(LABEL_CONTRACT["label_forward_period"]),
        "factor_holding_period_days": int(manifest.get("factor_holding_period_days") or manifest.get("holding_period_days") or 0) == int(LABEL_CONTRACT["factor_holding_period_days"]),
        "label_execution_deal_price": str(manifest.get("label_execution_deal_price") or "") == LABEL_CONTRACT["label_execution_deal_price"],
        "label_return_mode": str(manifest.get("label_return_mode") or "") == LABEL_CONTRACT["label_return_mode"],
        "label_column_present": label_present,
    }
    return {
        "passed": all(value is True or key == "label_name" for key, value in checks.items()),
        **checks,
        "warning": label_warning,
    }


def model_preflight(
    *,
    feature_set_id: str | None = None,
    all_active: bool | None = None,
    factor_holding_period_days: int = 5,
) -> dict[str, Any]:
    current_fp, active_records = current_active_registry_fingerprint(holding_period_days=factor_holding_period_days)
    readiness = active_values_readiness(factor_holding_period_days=factor_holding_period_days)
    requested_fp = readiness.get("requested_registry_fingerprint") or readiness.get("registry_fingerprint") or current_fp
    built_fp = readiness.get("built_registry_fingerprint") or readiness.get("manifest_registry_fingerprint") or ""
    manifest_fp = readiness.get("manifest_registry_fingerprint") or built_fp
    fingerprint_match = bool(current_fp and manifest_fp and current_fp == manifest_fp)
    manifest = load_feature_set_manifest(feature_set_id) if feature_set_id else None
    validation = validate_feature_set_manifest_for_model(manifest) if feature_set_id else {"passed": True, "errors": [], "warnings": []}
    freshness = feature_set_freshness(manifest) if feature_set_id else {"stale": False, "freshness_scope": "pre_snapshot_all_active"}
    selection_mode = str((manifest or {}).get("factor_selection_mode") or ("all_active" if not feature_set_id else "manual_or_historical"))
    requires_active_values = bool(all_active) if all_active is not None else (not feature_set_id or selection_mode == "all_active")
    qlib_data = _qlib_data_availability()
    label0 = _label0_contract(manifest) if manifest else {
        "passed": True,
        **LABEL_CONTRACT,
        "label_column_present": "pending_feature_snapshot",
    }
    errors: list[str] = []
    warnings: list[str] = []
    if requires_active_values and not readiness.get("safe_to_freeze_feature_set"):
        errors.append("active_values_not_safe_to_freeze")
    if requires_active_values and not fingerprint_match:
        errors.append("active_values_registry_fingerprint_mismatch")
    if feature_set_id and not validation.get("passed"):
        errors.extend(validation.get("errors") or [])
    if feature_set_id and not label0.get("passed"):
        errors.append("label0_contract_failed")
    if feature_set_id and selection_mode == "all_active" and freshness.get("stale"):
        errors.append(f"all_active_feature_set_stale:{freshness.get('stale_reason') or 'unknown'}")
    if feature_set_id and selection_mode != "all_active" and freshness.get("stale"):
        warnings.append(f"historical_or_subset_freshness_warning:{freshness.get('stale_reason') or 'unknown'}")
    if not qlib_data.get("passed"):
        errors.append("qlib_data_unavailable")
    stale_message = ""
    if not fingerprint_match:
        stale_message = f"active values stale because registry changed from {manifest_fp or 'unknown'} to {current_fp or 'unknown'}"
    return {
        "passed": not errors,
        "stage": "feature_snapshot_preflight",
        "feature_set_id": feature_set_id,
        "selection_mode": selection_mode,
        "requires_active_values": requires_active_values,
        "active_factor_count": len(active_records),
        "active_values_status": readiness.get("active_values_status"),
        "requested_registry_fingerprint": requested_fp,
        "built_registry_fingerprint": built_fp,
        "current_registry_fingerprint": current_fp,
        "manifest_registry_fingerprint": manifest_fp,
        "fingerprint_match": fingerprint_match,
        "stale_reason": stale_message or readiness.get("feature_snapshot_block_reason") or "",
        "model_snapshot_refresh_required": bool(readiness.get("model_snapshot_refresh_required")),
        "safe_to_freeze_feature_set": bool(readiness.get("safe_to_freeze_feature_set")) and fingerprint_match,
        "active_values_readiness": readiness,
        "feature_snapshot_readiness": {
            "manifest_found": bool(manifest) if feature_set_id else "pending_all_active_snapshot",
            "validation": validation,
            "freshness": freshness,
            "updates_active_pointer": bool((manifest or {}).get("updates_active_feature_pointer")) if manifest else True,
        },
        "label0_contract": label0,
        "qlib_data_availability": qlib_data,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
        "blocker": {
            "code": errors[0] if errors else "",
            "category": "external_data_blocker" if any("active_values" in err or "qlib_data" in err for err in errors) else ("hard_contract_blocker" if errors else ""),
            "stage": "feature_snapshot_preflight",
            "human_message": stale_message or ("; ".join(errors) if errors else ""),
            "repair_action": "refresh_active_values_from_parquet_then_freeze_feature_snapshot" if errors else "",
            "resume_from": "feature_snapshot_preflight",
            "affected_round": "",
        },
    }


__all__ = ["model_preflight"]
