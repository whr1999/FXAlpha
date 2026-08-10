from __future__ import annotations

import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from domain.factor_research.active_values_store import (
    active_values_store_summary,
    current_active_registry_fingerprint,
)
from domain.factor_research.factor_compute import _bs_to_qlib
from .feature_set_builder import (
    ACTIVE_FEATURE_SNAPSHOT_CONTRACT_VERSION,
    ACTIVE_POINTER_UPDATE_POLICY,
    FEATURE_MISSING_STRATEGY_DEFAULT,
    FEATURE_MISSING_STRATEGY_QLIB_ONLY,
    FEATURE_MISSING_STRATEGY_SEMANTIC_V1,
    FEATURE_MISSING_STRATEGY_STRUCTURAL_ZERO_V2,
    FEATURE_MISSING_STRATEGIES,
    FEATURE_SNAPSHOT_POLICY_VERSION,
    IMMUTABLE_SNAPSHOT_UPDATE_POLICY,
    LABEL_ENTRY_SHIFT_DAYS,
    LABEL_FILTER_POLICY,
    LABEL_MODE_DEFAULT,
    LABEL_PRICE_MODE,
    LABEL_SOURCE_PRICE_FIELD,
    _apply_feature_missing_fill_policy,
    _build_label_frame,
    _feature_missing_policy_label,
    _feature_missing_report,
    _feature_special_fill_policy_version,
    _semantic_missing_policy_candidate,
    _write_parquet_chunked,
    active_feature_snapshot_staleness,
    build_active_feature_set,
    compute_feature_set_fingerprint,
    load_active_feature_set_manifest,
    load_active_values_manifest,
    load_feature_set_manifest,
)
from .window_config import resolve_model_end_date
from storage.paths import (
    ACTIVE_MODEL_FEATURE_SET_FILE,
    MODEL_ACTIVE_FEATURE_DIR,
    MODEL_ACTIVE_FEATURE_FILE,
    MODEL_ACTIVE_FEATURE_MANIFEST,
    MODEL_DEFAULT_END_DATE,
    MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    MODEL_DEFAULT_FORWARD_PERIOD,
    MODEL_DEFAULT_START_DATE,
    MODEL_DEFAULT_STATUS_FILTER,
    MODEL_FEATURE_SETS_ROOT,
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return str(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def validate_feature_set_manifest_for_model(manifest: dict[str, Any] | None) -> dict[str, Any]:
    manifest = manifest or {}
    errors: list[str] = []
    warnings: list[str] = []
    if not manifest:
        errors.append("feature_set_manifest_missing")
    if manifest.get("feature_snapshot_policy_version") != FEATURE_SNAPSHOT_POLICY_VERSION:
        errors.append("feature_snapshot_policy_version_mismatch")
    if manifest.get("feature_missing_strategy") != FEATURE_MISSING_STRATEGY_DEFAULT:
        warnings.append("non_default_feature_missing_strategy")
    if manifest.get("feature_missing_strategy") not in FEATURE_MISSING_STRATEGIES:
        errors.append("unsupported_feature_missing_strategy")
    if int(manifest.get("label_forward_period") or 0) != MODEL_DEFAULT_FORWARD_PERIOD:
        errors.append("label_forward_period_mismatch")
    if int(manifest.get("factor_holding_period_days") or manifest.get("holding_period_days") or 0) != MODEL_DEFAULT_FACTOR_HOLDING_PERIOD:
        errors.append("factor_holding_period_days_mismatch")
    if str(manifest.get("label_price_mode") or "") != LABEL_PRICE_MODE:
        errors.append("label_price_mode_mismatch")
    if str(manifest.get("label_source_price_field") or "") != LABEL_SOURCE_PRICE_FIELD:
        errors.append("label_source_price_field_mismatch")
    if int(manifest.get("label_entry_shift_days") or 0) != LABEL_ENTRY_SHIFT_DAYS:
        errors.append("label_entry_shift_days_mismatch")
    if int(manifest.get("label_exit_shift_days") or 0) != LABEL_ENTRY_SHIFT_DAYS + MODEL_DEFAULT_FORWARD_PERIOD:
        errors.append("label_exit_shift_days_mismatch")
    if str(manifest.get("label_execution_deal_price") or "") != "open":
        errors.append("label_execution_deal_price_mismatch")
    if str(manifest.get("label_return_mode") or "") != "next_open_to_forward_open":
        errors.append("label_return_mode_mismatch")
    if manifest.get("label_uses_adjusted_price") is not True:
        errors.append("label_uses_adjusted_price_mismatch")
    combined_path_raw = str(manifest.get("combined_factors_file") or manifest.get("feature_file") or "")
    combined_path = Path(combined_path_raw)
    if not combined_path_raw:
        errors.append("combined_factors_file_missing")
    elif not combined_path.exists():
        errors.append("combined_factors_file_not_found")
    else:
        try:
            columns = pd.read_parquet(combined_path).columns
            if ("label", "LABEL0") not in columns and "LABEL0" not in columns:
                errors.append("label0_column_missing")
        except Exception as exc:
            errors.append(f"combined_factors_file_unreadable:{exc}")
    if int(manifest.get("factor_count") or 0) <= 0:
        errors.append("factor_count_missing")
    if int(manifest.get("feature_count") or 0) <= 0:
        errors.append("feature_count_missing")
    return {"passed": not errors, "errors": errors, "warnings": warnings}


def active_values_readiness(
    *,
    factor_holding_period_days: int = MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Status callers often already computed the authoritative active-values
    # summary.  Reusing it avoids re-reading parquet metadata and rescanning the
    # registry several times in one GUI request while preserving the exact same
    # readiness contract.
    summary = summary or active_values_store_summary(holding_period_days=factor_holding_period_days)
    stale = bool(summary.get("stale"))
    refresh_status = str(summary.get("refresh_status") or "")
    active_values_status = "ready" if not stale else "stale"
    registry_fp = summary.get("registry_fingerprint") or summary.get("current_registry_fingerprint")
    manifest_fp = summary.get("manifest_registry_fingerprint") or summary.get("built_registry_fingerprint")
    stale_message = summary.get("stale_message") or (
        f"active values stale because registry changed from {manifest_fp} to {registry_fp}"
        if manifest_fp and registry_fp and manifest_fp != registry_fp
        else str(summary.get("stale_reason") or "active_values_not_ready")
    )
    return {
        "active_values_status": active_values_status,
        "safe_to_freeze_feature_set": not stale,
        "feature_snapshot_blocked": stale,
        "feature_snapshot_block_reason": "" if not stale else stale_message,
        "required_action": "none" if not stale else "refresh_active_values_from_factor_store",
        "refresh_source_mode_default": "tail",
        "model_computes_factor_values": False,
        "responsibility_boundary": "factor import/build refresh owns factor values; model only checks active values readiness and freezes immutable feature sets",
        "registry_fingerprint": registry_fp,
        "requested_registry_fingerprint": summary.get("requested_registry_fingerprint") or registry_fp,
        "built_registry_fingerprint": summary.get("built_registry_fingerprint") or manifest_fp,
        "current_registry_fingerprint": summary.get("current_registry_fingerprint") or registry_fp,
        "manifest_registry_fingerprint": manifest_fp,
        "fingerprint_match": bool(registry_fp and manifest_fp and registry_fp == manifest_fp),
        "stale_reason": "" if not stale else stale_message,
        "model_snapshot_refresh_required": bool(summary.get("model_snapshot_refresh_required") or stale),
        "active_factor_count": summary.get("active_count"),
        "factor_count": summary.get("factor_count"),
        "column_count": summary.get("column_count"),
        "resolved_universe": summary.get("resolved_universe") or summary.get("universe"),
        "stale_reasons": summary.get("stale_reasons") or ([] if not stale else [summary.get("stale_reason")]),
        "active_values_path": summary.get("path"),
        "active_values_manifest_path": summary.get("manifest_path"),
        "latest_generated_at": summary.get("latest_generated_at"),
        "refresh_status": refresh_status,
        "active_values_job": summary.get("active_values_job") or {},
        "progress": summary.get("progress") or {},
        "summary": summary,
    }


def _snapshot_contract_payload(result: dict[str, Any], *, active_values_readiness_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else validate_feature_set_manifest_for_model(manifest)
    active_values_manifest = (
        (manifest.get("active_values_lineage") or {}).get("manifest_path")
        or (active_values_readiness_payload or result.get("active_values_readiness") or {}).get("active_values_manifest_path")
    )
    return {
        **result,
        "feature_set_id": manifest.get("feature_set_id") or result.get("feature_set_id"),
        "factor_count": manifest.get("factor_count"),
        "feature_count": manifest.get("feature_count"),
        "label0_contract": {
            "label_name": "LABEL0",
            "label_forward_period": manifest.get("label_forward_period"),
            "factor_holding_period_days": manifest.get("factor_holding_period_days") or manifest.get("holding_period_days"),
            "label_execution_deal_price": manifest.get("label_execution_deal_price"),
            "label_return_mode": manifest.get("label_return_mode"),
            "label_uses_adjusted_price": manifest.get("label_uses_adjusted_price"),
            "passed": validation.get("passed"),
        },
        "active_values_manifest": active_values_manifest,
        "fingerprint_match": bool((active_values_readiness_payload or result.get("active_values_readiness") or {}).get("fingerprint_match", True)),
        "updates_active_pointer": bool(manifest.get("updates_active_feature_pointer")),
        "missing_strategy": manifest.get("feature_missing_strategy") or result.get("request", {}).get("feature_missing_strategy"),
        "snapshot_kind": "all_active" if str(manifest.get("factor_selection_mode") or "") == "all_active" else "manual_or_subset_immutable",
        "pointer_policy": "all_active_updates_active_pointer" if bool(manifest.get("updates_active_feature_pointer")) else "immutable_experiment_artifact",
        "provenance": _feature_set_provenance_from_manifest(manifest) or {
            "source_type": manifest.get("source_type") or result.get("request", {}).get("source_type"),
            "source_feature_set_id": manifest.get("source_feature_set_id") or result.get("request", {}).get("source_feature_set_id"),
            "recommendation_family": manifest.get("recommendation_family") or result.get("request", {}).get("recommendation_family"),
            "audit_recommendation_id": manifest.get("audit_recommendation_id") or result.get("request", {}).get("audit_recommendation_id"),
        },
    }


def _assert_active_values_ready_for_all_active_snapshot(*, factor_holding_period_days: int) -> dict[str, Any]:
    readiness = active_values_readiness(factor_holding_period_days=factor_holding_period_days)
    if not readiness["safe_to_freeze_feature_set"]:
        raise RuntimeError(
            "model feature snapshot blocked: active values are not ready; "
            "refresh active values from the factor store before freezing model features "
            f"(reason={readiness.get('feature_snapshot_block_reason')}, "
            f"registry_fingerprint={readiness.get('registry_fingerprint')}, "
            f"manifest_registry_fingerprint={readiness.get('manifest_registry_fingerprint')}, "
            f"action={readiness.get('required_action')}, default_source_mode=tail)"
        )
    return readiness


def _candidate_source_manifests(source_feature_set_id: str | None = None) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    if source_feature_set_id:
        path = MODEL_FEATURE_SETS_ROOT / source_feature_set_id / "manifest.json"
        if path.exists():
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                manifest = None
        else:
            manifest = load_feature_set_manifest(source_feature_set_id)
        if isinstance(manifest, dict):
            manifests.append(manifest)
        return manifests
    for path in sorted(MODEL_FEATURE_SETS_ROOT.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            manifests.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return manifests


def _manifest_contains_factor_ids(manifest: dict[str, Any], factor_ids: list[str]) -> bool:
    present = {str(item.get("factor_id") or "") for item in manifest.get("factor_records") or [] if isinstance(item, dict)}
    return set(map(str, factor_ids)).issubset(present)


def _source_manifest_signature(source: dict[str, Any]) -> str:
    return str(
        source.get("feature_set_signature")
        or source.get("snapshot_signature")
        or source.get("feature_set_id")
        or ""
    )


def _feature_set_provenance_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    existing = manifest.get("feature_set_provenance")
    if isinstance(existing, dict) and existing:
        return existing
    selection_mode = str(manifest.get("factor_selection_mode") or "")
    source_type = manifest.get("source_type") or (
        "all_active" if selection_mode == "all_active" else "manual_or_historical"
    )
    return {
        "source_type": source_type,
        "source_feature_set_id": manifest.get("source_feature_set_id"),
        "source_manifest_signature": manifest.get("source_manifest_signature"),
        "recommendation_family": manifest.get("recommendation_family"),
        "audit_recommendation_id": manifest.get("audit_recommendation_id"),
        "provenance_note": manifest.get("provenance_note"),
        "immutable_experiment_artifact": not bool(manifest.get("updates_active_feature_pointer")),
        "updates_active_pointer": bool(manifest.get("updates_active_feature_pointer")),
        "legacy_provenance_synthesized": True,
    }


def _materialize_subset_from_source(
    *,
    feature_set_id: str,
    factor_ids: list[str],
    source_feature_set_id: str | None = None,
    feature_missing_strategy: str = FEATURE_MISSING_STRATEGY_DEFAULT,
    source_type: str | None = None,
    recommendation_family: str | None = None,
    audit_recommendation_id: str | None = None,
    provenance_note: str | None = None,
) -> dict[str, Any]:
    if feature_missing_strategy not in FEATURE_MISSING_STRATEGIES:
        raise ValueError(
            f"unsupported feature_missing_strategy={feature_missing_strategy}; "
            f"supported={sorted(FEATURE_MISSING_STRATEGIES)}"
        )
    source = next(
        (manifest for manifest in _candidate_source_manifests(source_feature_set_id) if _manifest_contains_factor_ids(manifest, factor_ids)),
        None,
    )
    if not source:
        raise RuntimeError(
            "model explicit feature snapshot requires a source feature set containing all requested factor_ids; "
            f"missing_from_available_sources={factor_ids[:5]}"
        )
    source_path = Path(str(source.get("combined_factors_file") or source.get("feature_file") or ""))
    if not source_path.exists():
        raise FileNotFoundError(f"source combined feature parquet not found: {source_path}")
    source_missing_strategy = str(source.get("feature_missing_strategy") or FEATURE_MISSING_STRATEGY_DEFAULT)
    if source_missing_strategy not in FEATURE_MISSING_STRATEGIES:
        raise ValueError(f"source feature set has unsupported feature_missing_strategy={source_missing_strategy}")
    if source_missing_strategy != FEATURE_MISSING_STRATEGY_DEFAULT and source_missing_strategy != feature_missing_strategy:
        raise ValueError(
            "cannot materialize model subset with a different missing strategy from an already-prefilled source "
            f"(source={source_missing_strategy}, requested={feature_missing_strategy})"
        )
    records_by_id = {
        str(item.get("factor_id")): dict(item)
        for item in source.get("factor_records") or []
        if isinstance(item, dict)
    }
    selected_records = [records_by_id[str(fid)] for fid in factor_ids]
    selected_feature_cols = [
        ("feature", str(record.get("data_column") or ""))
        for record in selected_records
        if record.get("data_column")
    ]
    df = pd.read_parquet(source_path)
    label_cols = [col for col in df.columns if isinstance(col, tuple) and col[0] == "label"]
    if ("label", "LABEL0") not in label_cols:
        raise RuntimeError("source snapshot missing label/LABEL0 column")
    missing_cols = [col for col in selected_feature_cols if col not in df.columns]
    if missing_cols:
        raise RuntimeError(f"requested feature columns missing in source snapshot: {missing_cols[:5]}")
    if not selected_feature_cols:
        raise RuntimeError("requested factor_ids resolved to zero feature columns")
    subset = df.loc[:, selected_feature_cols + label_cols].copy()
    start_for_report = str(source.get("start_date") or source.get("actual_start_date") or source.get("value_start_date") or subset.index.get_level_values("datetime").min().date())
    end_for_report = str(source.get("resolved_end_date") or source.get("end_date") or source.get("actual_end_date") or subset.index.get_level_values("datetime").max().date())
    raw_feature_missing_summary, raw_feature_coverage_report = _feature_missing_report(
        subset,
        selected_feature_cols,
        start_date=start_for_report,
        end_date=end_for_report,
    )
    semantic_missing_audit_report: list[dict[str, Any]] = []
    if source_missing_strategy == FEATURE_MISSING_STRATEGY_DEFAULT:
        subset, semantic_missing_audit_report = _apply_feature_missing_fill_policy(
            subset,
            selected_records,
            feature_missing_strategy=feature_missing_strategy,
        )
    else:
        source_reports = source.get("semantic_missing_audit_report") or source.get("feature_imputation_report") or []
        selected = set(map(str, factor_ids))
        semantic_missing_audit_report = [
            dict(item)
            for item in source_reports
            if isinstance(item, dict) and str(item.get("factor_id") or "") in selected
        ]
    feature_imputation_report = [
        item for item in semantic_missing_audit_report if int(item.get("filled_count") or 0) > 0
    ]
    post_snapshot_feature_missing_summary, feature_coverage_report = _feature_missing_report(
        subset,
        selected_feature_cols,
        start_date=start_for_report,
        end_date=end_for_report,
    )
    feature_set_dir = MODEL_FEATURE_SETS_ROOT / feature_set_id
    feature_set_dir.mkdir(parents=True, exist_ok=True)
    combined_path = feature_set_dir / "combined_factors_df.parquet"
    manifest_path = feature_set_dir / "manifest.json"
    subset.to_parquet(combined_path)
    feature_dates = pd.to_datetime(subset.index.get_level_values("datetime"))
    label_available_sample_count = int(subset[[("label", "LABEL0")]].notna().all(axis=1).sum())
    label_missing_sample_count = int(len(subset) - label_available_sample_count)
    label_forward_period = int(source.get("label_forward_period") or MODEL_DEFAULT_FORWARD_PERIOD)
    factor_holding_period_days = int(source.get("factor_holding_period_days") or source.get("holding_period_days") or MODEL_DEFAULT_FACTOR_HOLDING_PERIOD)
    generated_at = datetime.now().isoformat(timespec="seconds")
    inherited_source = {key: value for key, value in source.items() if key not in {"feature_set_fingerprint"}}
    resolved_source_type = source_type or ("audit_recommended" if recommendation_family or audit_recommendation_id else "manual_subset")
    feature_set_provenance = {
        "source_type": resolved_source_type,
        "source_feature_set_id": source.get("feature_set_id"),
        "source_manifest_signature": _source_manifest_signature(source),
        "recommendation_family": recommendation_family,
        "audit_recommendation_id": audit_recommendation_id,
        "provenance_note": provenance_note,
        "immutable_experiment_artifact": True,
        "updates_active_pointer": False,
    }
    manifest = {
        **inherited_source,
        "feature_set_id": feature_set_id,
        "generated_at": generated_at,
        "source_type": resolved_source_type,
        "source_feature_set_id": source.get("feature_set_id"),
        "source_manifest_signature": _source_manifest_signature(source),
        "recommendation_family": recommendation_family,
        "audit_recommendation_id": audit_recommendation_id,
        "provenance_note": provenance_note,
        "feature_set_provenance": feature_set_provenance,
        "feature_set_signature": f"{feature_set_id}:{generated_at}",
        "factor_selection_mode": "explicit_factor_ids_from_source_snapshot",
        "factor_ids": list(map(str, factor_ids)),
        "factor_records": selected_records,
        "factor_count": len(selected_records),
        "feature_count": len(selected_feature_cols),
        "combined_factors_file": str(combined_path),
        "feature_file": str(combined_path),
        "manifest_file": str(manifest_path),
        "updates_active_feature_pointer": False,
        "active_pointer_update_policy": "immutable_snapshot_only",
        "model_immutable_subset_snapshot": True,
        "feature_missing_strategy": feature_missing_strategy,
        "feature_snapshot_policy_version": FEATURE_SNAPSHOT_POLICY_VERSION,
        "label_forward_period": label_forward_period,
        "holding_period_days": factor_holding_period_days,
        "factor_holding_period_days": factor_holding_period_days,
        "label_price_mode": source.get("label_price_mode") or LABEL_PRICE_MODE,
        "label_mode": source.get("label_mode") or "raw_open_return_v1",
        "label_source_price_field": source.get("label_source_price_field") or LABEL_SOURCE_PRICE_FIELD,
        "label_entry_shift_days": int(source.get("label_entry_shift_days") or LABEL_ENTRY_SHIFT_DAYS),
        "label_exit_shift_days": int(source.get("label_exit_shift_days") or LABEL_ENTRY_SHIFT_DAYS + label_forward_period),
        "label_execution_deal_price": source.get("label_execution_deal_price") or "open",
        "label_return_mode": source.get("label_return_mode") or "next_open_to_forward_open",
        "label_adjustment_field": source.get("label_adjustment_field"),
        "label_uses_adjusted_price": source.get("label_uses_adjusted_price", True),
        "label_filter_policy": source.get("label_filter_policy") or LABEL_FILTER_POLICY,
        "label_sample_count": int(source.get("label_sample_count") or label_available_sample_count),
        "label_available_sample_count": label_available_sample_count,
        "label_missing_sample_count": label_missing_sample_count,
        "post_label_drop_sample_count": int(len(subset)),
        "feature_special_fill_policy_version": _feature_special_fill_policy_version(feature_missing_strategy),
        "prefill_applied": bool(feature_imputation_report) or bool(source.get("prefill_applied")),
        "feature_missing_policy": _feature_missing_policy_label(feature_missing_strategy),
        "raw_feature_missing_summary": raw_feature_missing_summary,
        "raw_feature_coverage_report": raw_feature_coverage_report,
        "semantic_missing_audit_report": semantic_missing_audit_report,
        "feature_imputation_report": feature_imputation_report,
        "post_snapshot_feature_missing_summary": post_snapshot_feature_missing_summary,
        "feature_missing_summary": post_snapshot_feature_missing_summary,
        "feature_coverage_report": feature_coverage_report,
        "sample_count": int(len(subset)),
        "shape": list(subset.shape),
        "actual_start_date": str(feature_dates.min().date()),
        "actual_end_date": str(feature_dates.max().date()),
        "latest_date": str(feature_dates.max().date()),
        "lineage_note": "model explicit subset snapshot materialized from immutable source snapshot; all-active pointer freshness does not gate this artifact.",
    }
    manifest_path.write_text(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _active_values_to_qlib_index(values: pd.DataFrame) -> pd.DataFrame:
    frame = values.copy()
    if isinstance(frame.index, pd.MultiIndex):
        names = list(frame.index.names)
        if names == ["stock_code", "trade_date"] or {"stock_code", "trade_date"} <= set(names):
            frame = frame.reset_index()
        elif names == ["datetime", "instrument"] or {"datetime", "instrument"} <= set(names):
            frame.index = frame.index.set_names(["datetime", "instrument"])
            return frame.sort_index()
    if {"stock_code", "trade_date"} <= set(frame.columns):
        frame["datetime"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
        frame["instrument"] = frame["stock_code"].map(lambda code: _bs_to_qlib(str(code)))
        drop_cols = [col for col in ("stock_code", "trade_date") if col in frame.columns]
        frame = frame.drop(columns=drop_cols)
        return frame.set_index(["datetime", "instrument"]).sort_index()
    if {"datetime", "instrument"} <= set(frame.columns):
        frame["datetime"] = pd.to_datetime(frame["datetime"]).dt.normalize()
        frame["instrument"] = frame["instrument"].astype(str)
        return frame.set_index(["datetime", "instrument"]).sort_index()
    raise RuntimeError("active values wide table must contain stock_code/trade_date or datetime/instrument index")


def _active_values_lineage_from_manifest(active_values_manifest: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": active_values_manifest.get("schema_version"),
        "generated_at": active_values_manifest.get("generated_at"),
        "resolved_universe": active_values_manifest.get("resolved_universe") or active_values_manifest.get("universe"),
        "universe": active_values_manifest.get("resolved_universe") or active_values_manifest.get("universe"),
        "value_start_date": active_values_manifest.get("value_start_date"),
        "value_end_date": active_values_manifest.get("value_end_date"),
        "filter_non_st_before_expression": active_values_manifest.get("filter_non_st_before_expression"),
        "compute_semantics_version": active_values_manifest.get("compute_semantics_version"),
        "source_mode": active_values_manifest.get("source_mode"),
        "source_data_kind": active_values_manifest.get("source_data_kind"),
        "source_data_fingerprint": active_values_manifest.get("source_data_fingerprint"),
        "source_data_signature": active_values_manifest.get("source_data_signature"),
        "registry_fingerprint": active_values_manifest.get("registry_fingerprint") or readiness.get("manifest_registry_fingerprint"),
        "audit_anchor": active_values_manifest.get("audit_anchor"),
        "stale": False,
        "manifest_path": readiness.get("active_values_manifest_path"),
        "active_values_path": readiness.get("active_values_path"),
    }


def _record_alias(record: dict[str, Any], used_aliases: set[str]) -> str:
    raw = str(record.get("data_column") or record.get("factor_id") or record.get("expression") or "feature")
    alias = "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_")[:80] or "feature"
    if alias in used_aliases:
        suffix = str(record.get("factor_id") or record.get("expression") or "")[-8:]
        alias = f"{alias}_{''.join(ch if ch.isalnum() else '_' for ch in suffix).strip('_')}"
    used_aliases.add(alias)
    return alias


def _snapshot_content_fingerprint(
    *,
    records: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    label_forward_period: int,
    factor_holding_period_days: int,
    feature_missing_strategy: str,
    active_values_lineage: dict[str, Any],
) -> str:
    """Fingerprint logical snapshot inputs without including its generated id/path."""

    payload = {
        "schema": "model_snapshot_content_v1",
        "factors": sorted(
            (
                str(record.get("factor_id") or record.get("expression") or record.get("data_column") or ""),
                str(record.get("expression") or record.get("data_column") or ""),
            )
            for record in records
        ),
        "start_date": str(start_date),
        "end_date": str(end_date),
        "label_forward_period": int(label_forward_period),
        "factor_holding_period_days": int(factor_holding_period_days),
        "feature_missing_strategy": str(feature_missing_strategy),
        "source_data_fingerprint": str(active_values_lineage.get("source_data_fingerprint") or ""),
        "registry_fingerprint": str(active_values_lineage.get("registry_fingerprint") or ""),
        "resolved_universe": str(active_values_lineage.get("resolved_universe") or active_values_lineage.get("universe") or ""),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_snapshot_content_fingerprint(manifest: dict[str, Any]) -> str:
    existing = str(manifest.get("snapshot_content_fingerprint") or "")
    if existing:
        return existing
    return _snapshot_content_fingerprint(
        records=list(manifest.get("factor_records") or []),
        start_date=str(manifest.get("start_date") or manifest.get("actual_start_date") or ""),
        end_date=str(manifest.get("resolved_end_date") or manifest.get("end_date") or manifest.get("actual_end_date") or ""),
        label_forward_period=int(manifest.get("label_forward_period") or MODEL_DEFAULT_FORWARD_PERIOD),
        factor_holding_period_days=int(manifest.get("factor_holding_period_days") or manifest.get("holding_period_days") or MODEL_DEFAULT_FACTOR_HOLDING_PERIOD),
        feature_missing_strategy=str(manifest.get("feature_missing_strategy") or FEATURE_MISSING_STRATEGY_DEFAULT),
        active_values_lineage=dict(manifest.get("active_values_lineage") or {}),
    )


def _reuse_snapshot_as_active(manifest: dict[str, Any]) -> dict[str, Any]:
    """Point active metadata at an immutable snapshot without copying its parquet."""

    MODEL_ACTIVE_FEATURE_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_ACTIVE_FEATURE_MANIFEST.write_text(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2), encoding="utf-8")
    ACTIVE_MODEL_FEATURE_SET_FILE.parent.mkdir(parents=True, exist_ok=True)
    pointer = {**manifest, "updated_at": datetime.now().isoformat(timespec="seconds")}
    ACTIVE_MODEL_FEATURE_SET_FILE.write_text(json.dumps(_jsonable(pointer), ensure_ascii=False, indent=2), encoding="utf-8")
    result = dict(manifest)
    result["snapshot_reused"] = True
    return result


def _build_all_active_snapshot_from_active_values(
    *,
    feature_set_id: str | None,
    status_filter: str,
    start_date: str,
    end_date: str | None,
    label_forward_period: int,
    factor_holding_period_days: int,
    feature_missing_strategy: str,
    readiness: dict[str, Any],
) -> dict[str, Any]:
    if feature_missing_strategy not in FEATURE_MISSING_STRATEGIES:
        raise ValueError(
            f"unsupported feature_missing_strategy={feature_missing_strategy}; "
            f"supported={sorted(FEATURE_MISSING_STRATEGIES)}"
        )
    if status_filter != MODEL_DEFAULT_STATUS_FILTER:
        raise ValueError("model all-active snapshot requires default active status_filter")
    feature_set_id = feature_set_id or f"fs-model-active-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    requested_end_date = end_date or MODEL_DEFAULT_END_DATE or datetime.now().strftime("%Y-%m-%d")
    resolved_end_date = resolve_model_end_date(end_date)
    active_values_path = Path(str(readiness.get("active_values_path") or ""))
    active_values_manifest_path = Path(str(readiness.get("active_values_manifest_path") or ""))
    if not active_values_path.exists():
        raise FileNotFoundError(f"active values wide table not found: {active_values_path}")
    active_values_manifest = load_active_values_manifest(active_values_manifest_path) if active_values_manifest_path.exists() else None
    active_values_manifest = active_values_manifest or {}
    records = [
        dict(item)
        for item in active_values_manifest.get("factor_records") or []
        if isinstance(item, dict)
    ]
    if not records:
        records = [
            {
                "factor_id": str(column),
                "name": str(column),
                "expression": str(column),
                "data_column": str(column),
                "holding_period_days": factor_holding_period_days,
            }
            for column in pd.read_parquet(active_values_path).columns
        ]
    active_values_lineage = _active_values_lineage_from_manifest(active_values_manifest, readiness)
    content_fingerprint = _snapshot_content_fingerprint(
        records=records,
        start_date=start_date,
        end_date=resolved_end_date,
        label_forward_period=label_forward_period,
        factor_holding_period_days=factor_holding_period_days,
        feature_missing_strategy=feature_missing_strategy,
        active_values_lineage=active_values_lineage,
    )
    for prior_path in sorted(MODEL_FEATURE_SETS_ROOT.glob("*/manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            prior = json.loads(prior_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        prior_data = Path(str(prior.get("combined_factors_file") or prior.get("feature_file") or ""))
        if prior_data.exists() and _manifest_snapshot_content_fingerprint(prior) == content_fingerprint:
            return _reuse_snapshot_as_active(prior)
    values = _active_values_to_qlib_index(pd.read_parquet(active_values_path))
    date_index = pd.to_datetime(values.index.get_level_values("datetime"))
    values = values[(date_index >= pd.Timestamp(start_date)) & (date_index <= pd.Timestamp(resolved_end_date))].copy()
    if values.empty:
        raise RuntimeError(f"active values wide table has no rows in model window {start_date}..{resolved_end_date}")

    feature_parts: list[pd.DataFrame] = []
    factor_records: list[dict[str, Any]] = []
    used_aliases: set[str] = set()
    for record in records:
        expression = str(record.get("expression") or record.get("data_column") or record.get("factor_id") or "")
        if not expression or expression not in values.columns:
            continue
        alias = _record_alias(record, used_aliases)
        part = values[[expression]].copy()
        part.columns = pd.MultiIndex.from_product([["feature"], [alias]])
        feature_parts.append(part)
        data_path = str(record.get("data_path") or active_values_path)
        data_file = Path(data_path)
        semantic_missing_policy = _semantic_missing_policy_candidate(record, {"data_column": alias})
        factor_records.append(
            {
                "factor_id": str(record.get("factor_id") or expression),
                "name": str(record.get("name") or ""),
                "expression": expression,
                "data_path": data_path,
                "active_values_path": str(active_values_path),
                "active_values_column": expression,
                "data_column": alias,
                "holding_period_days": record.get("holding_period_days") or factor_holding_period_days,
                "ic_mean": record.get("ic_mean"),
                "icir": record.get("icir"),
                "coverage": record.get("coverage") or {},
                "semantic_missing_policy_candidate": semantic_missing_policy,
                "missing_fill_policy": "qlib_processor_neutral_fill",
                "missing_fill_count": 0,
                "data_mtime": data_file.stat().st_mtime if data_file.exists() else active_values_path.stat().st_mtime,
                "data_size": data_file.stat().st_size if data_file.exists() else active_values_path.stat().st_size,
            }
        )
    if not factor_records or not feature_parts:
        raise RuntimeError("active values wide table did not contain any selected active factor columns")

    label = _build_label_frame(
        start_date=start_date,
        end_date=resolved_end_date,
        forward_period=label_forward_period,
        label_mode=LABEL_MODE_DEFAULT,
    )
    combined_features = pd.concat(feature_parts, axis=1, join="outer").sort_index()
    combined = combined_features.join(label, how="left")
    feature_cols = [col for col in combined.columns if isinstance(col, tuple) and col[0] == "feature"]
    label_cols = [col for col in combined.columns if isinstance(col, tuple) and col[0] == "label"]
    raw_feature_missing_summary, raw_feature_coverage_report = _feature_missing_report(
        combined,
        feature_cols,
        start_date=start_date,
        end_date=resolved_end_date,
    )
    combined, semantic_missing_audit_report = _apply_feature_missing_fill_policy(
        combined,
        factor_records,
        feature_missing_strategy=feature_missing_strategy,
    )
    feature_imputation_report = [
        item for item in semantic_missing_audit_report if int(item.get("filled_count") or 0) > 0
    ]
    post_snapshot_feature_missing_summary, feature_coverage_report = _feature_missing_report(
        combined,
        feature_cols,
        start_date=start_date,
        end_date=resolved_end_date,
    )
    if combined.empty:
        raise RuntimeError("combined feature set is empty after joining active values and LABEL0")

    feature_set_dir = MODEL_FEATURE_SETS_ROOT / feature_set_id
    feature_set_dir.mkdir(parents=True, exist_ok=True)
    combined_path = feature_set_dir / "combined_factors_df.parquet"
    manifest_path = feature_set_dir / "manifest.json"
    _write_parquet_chunked(combined, combined_path)

    feature_dates = pd.to_datetime(combined.index.get_level_values("datetime"))
    label_sample_count = int(label.shape[0])
    label_available_sample_count = int(combined[label_cols].notna().all(axis=1).sum()) if label_cols else 0
    label_missing_sample_count = int(len(combined) - label_available_sample_count) if label_cols else int(len(combined))
    active_factor_registry_fingerprint, _ = current_active_registry_fingerprint(
        holding_period_days=factor_holding_period_days
    )
    generated_at = datetime.now().isoformat(timespec="seconds")
    fingerprint = compute_feature_set_fingerprint(
        feature_set_id=feature_set_id,
        factor_records=factor_records,
        combined_factors_file=combined_path,
        label_forward_period=label_forward_period,
        factor_holding_period_days=factor_holding_period_days,
        feature_missing_strategy=feature_missing_strategy,
        label_mode=LABEL_MODE_DEFAULT,
    )
    actual_start_date = str(pd.Timestamp(feature_dates.min()).date())
    actual_end_date = str(pd.Timestamp(feature_dates.max()).date())
    manifest = {
        "feature_set_id": feature_set_id,
        "active_feature_snapshot_contract_version": ACTIVE_FEATURE_SNAPSHOT_CONTRACT_VERSION,
        "feature_snapshot_policy_version": FEATURE_SNAPSHOT_POLICY_VERSION,
        "generated_at": generated_at,
        "status_filter": status_filter,
        "factor_ids": [str(record.get("factor_id") or "") for record in factor_records],
        "factor_selection_mode": "all_active",
        "source_type": "all_active",
        "feature_set_provenance": {
            "source_type": "all_active",
            "source_feature": "active_values_wide_table",
            "active_values_manifest": str(active_values_manifest_path),
            "active_values_path": str(active_values_path),
            "active_factor_registry_fingerprint": active_factor_registry_fingerprint,
            "immutable_experiment_artifact": False,
            "updates_active_pointer": True,
        },
        "feature_source": "active_values_wide_table",
        "model_uses_active_values_wide_table": True,
        "model_computes_factor_values": False,
        "start_date": start_date,
        "end_date": resolved_end_date,
        "requested_end_date": requested_end_date,
        "resolved_end_date": resolved_end_date,
        "label_forward_period": label_forward_period,
        "holding_period_days": factor_holding_period_days,
        "factor_holding_period_days": factor_holding_period_days,
        "label_price_mode": LABEL_PRICE_MODE,
        "label_mode": LABEL_MODE_DEFAULT,
        "label_source_price_field": LABEL_SOURCE_PRICE_FIELD,
        "label_entry_shift_days": LABEL_ENTRY_SHIFT_DAYS,
        "label_exit_shift_days": LABEL_ENTRY_SHIFT_DAYS + label_forward_period,
        "label_execution_deal_price": "open",
        "label_return_mode": "next_open_to_forward_open",
        "label_adjustment_field": None,
        "label_uses_adjusted_price": True,
        "combined_factors_file": str(combined_path),
        "feature_file": str(combined_path),
        "manifest_file": str(manifest_path),
        "updates_active_feature_pointer": True,
        "active_pointer_update_policy": ACTIVE_POINTER_UPDATE_POLICY,
        "feature_set_fingerprint": fingerprint,
        "snapshot_content_fingerprint": content_fingerprint,
        "active_factor_registry_fingerprint": active_factor_registry_fingerprint,
        "active_values_lineage": active_values_lineage,
        "shape": list(combined.shape),
        "sample_count": int(combined.shape[0]),
        "label_sample_count": label_sample_count,
        "label_available_sample_count": label_available_sample_count,
        "label_missing_sample_count": label_missing_sample_count,
        "post_label_drop_sample_count": int(combined.shape[0]),
        "label_filter_policy": LABEL_FILTER_POLICY,
        "feature_special_fill_policy_version": _feature_special_fill_policy_version(feature_missing_strategy),
        "feature_missing_strategy": feature_missing_strategy,
        "prefill_applied": bool(feature_imputation_report),
        "feature_missing_policy": _feature_missing_policy_label(feature_missing_strategy),
        "raw_feature_missing_summary": raw_feature_missing_summary,
        "raw_feature_coverage_report": raw_feature_coverage_report,
        "semantic_missing_audit_report": semantic_missing_audit_report,
        "feature_imputation_report": feature_imputation_report,
        "post_snapshot_feature_missing_summary": post_snapshot_feature_missing_summary,
        "feature_missing_summary": post_snapshot_feature_missing_summary,
        "feature_coverage_report": feature_coverage_report,
        "actual_start_date": actual_start_date,
        "actual_end_date": actual_end_date,
        "latest_date": actual_end_date,
        "factor_count": len(factor_records),
        "feature_count": len(feature_cols),
        "factor_records": factor_records,
    }
    manifest_path.write_text(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2), encoding="utf-8")

    MODEL_ACTIVE_FEATURE_DIR.mkdir(parents=True, exist_ok=True)
    _write_parquet_chunked(combined, MODEL_ACTIVE_FEATURE_FILE)
    MODEL_ACTIVE_FEATURE_MANIFEST.write_text(
        json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ACTIVE_MODEL_FEATURE_SET_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_MODEL_FEATURE_SET_FILE.write_text(
        json.dumps(
            _jsonable(
                {
                    **manifest,
                    "combined_factors_file": str(MODEL_ACTIVE_FEATURE_FILE),
                    "feature_file": str(MODEL_ACTIVE_FEATURE_FILE),
                    "manifest_file": str(MODEL_ACTIVE_FEATURE_MANIFEST),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest


def feature_set_freshness(manifest: dict[str, Any] | None) -> dict[str, Any]:
    manifest = manifest or {}
    if not manifest:
        return {"stale": True, "stale_reason": "feature_set_manifest_missing", "stale_reasons": ["feature_set_manifest_missing"]}
    holding_period = int(manifest.get("factor_holding_period_days") or manifest.get("holding_period_days") or MODEL_DEFAULT_FACTOR_HOLDING_PERIOD)
    try:
        freshness = active_feature_snapshot_staleness(manifest, holding_period_days=holding_period)
    except Exception as exc:
        return {"stale": True, "stale_reason": "freshness_check_failed", "stale_reasons": ["freshness_check_failed"], "error": str(exc)}
    selection_mode = str(manifest.get("factor_selection_mode") or "")
    freshness["feature_set_id"] = manifest.get("feature_set_id")
    freshness["factor_selection_mode"] = selection_mode
    freshness["freshness_scope"] = "current_all_active_required" if selection_mode == "all_active" else "immutable_historical_or_subset"
    return freshness


def model_feature_set_preflight(feature_set_id: str) -> dict[str, Any]:
    manifest = load_feature_set_manifest(feature_set_id)
    validation = validate_feature_set_manifest_for_model(manifest)
    freshness = feature_set_freshness(manifest)
    errors = list(validation.get("errors") or [])
    warnings = list(validation.get("warnings") or [])
    if manifest and str(manifest.get("factor_selection_mode") or "") == "all_active" and bool(freshness.get("stale")):
        errors.append(f"all_active_feature_set_stale:{freshness.get('stale_reason') or 'unknown'}")
    if manifest and str(manifest.get("factor_selection_mode") or "") != "all_active" and bool(freshness.get("stale")):
        warnings.append(f"historical_or_subset_freshness_warning:{freshness.get('stale_reason') or 'unknown'}")
    return {
        "passed": not errors,
        "feature_set_id": feature_set_id,
        "manifest_found": bool(manifest),
        "validation": validation,
        "freshness": freshness,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
        "required_action": "" if not errors else "refresh_active_values_and_freeze_current_all_active_feature_set_or_choose_valid_feature_set",
    }


def feature_snapshot(
    *,
    feature_set_id: str | None = None,
    factor_ids: list[str] | None = None,
    status_filter: str = MODEL_DEFAULT_STATUS_FILTER,
    start_date: str = MODEL_DEFAULT_START_DATE,
    end_date: str | None = MODEL_DEFAULT_END_DATE,
    label_forward_period: int = MODEL_DEFAULT_FORWARD_PERIOD,
    factor_holding_period_days: int = MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    feature_missing_strategy: str = FEATURE_MISSING_STRATEGY_DEFAULT,
    dry_run: bool = False,
    source_feature_set_id: str | None = None,
    source_type: str | None = None,
    recommendation_family: str | None = None,
    audit_recommendation_id: str | None = None,
    provenance_note: str | None = None,
) -> dict[str, Any]:
    if dry_run:
        readiness = active_values_readiness(factor_holding_period_days=factor_holding_period_days) if not factor_ids else None
        return _snapshot_contract_payload({
            "ok": True,
            "mode": "dry_run",
            "active_values_readiness": readiness,
            "request": {
                "feature_set_id": feature_set_id,
                "factor_ids": factor_ids,
                "status_filter": status_filter,
                "start_date": start_date,
                "end_date": end_date,
                "label_forward_period": label_forward_period,
                "factor_holding_period_days": factor_holding_period_days,
                "feature_missing_strategy": feature_missing_strategy,
                "source_feature_set_id": source_feature_set_id,
                "source_type": source_type,
                "recommendation_family": recommendation_family,
                "audit_recommendation_id": audit_recommendation_id,
                "provenance_note": provenance_note,
            },
        }, active_values_readiness_payload=readiness)
    if feature_set_id and not factor_ids:
        manifest = load_feature_set_manifest(feature_set_id)
        if manifest:
            freshness = feature_set_freshness(manifest)
            return _snapshot_contract_payload({
                "ok": True,
                "mode": "existing_snapshot",
                "feature_set_id": feature_set_id,
                "manifest": manifest,
                "validation": validate_feature_set_manifest_for_model(manifest),
                "freshness": freshness,
            })
    if factor_ids:
        feature_set_id = feature_set_id or f"fs-model-subset-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        manifest = _materialize_subset_from_source(
            feature_set_id=feature_set_id,
            factor_ids=factor_ids,
            source_feature_set_id=source_feature_set_id,
            feature_missing_strategy=feature_missing_strategy,
            source_type=source_type,
            recommendation_family=recommendation_family,
            audit_recommendation_id=audit_recommendation_id,
            provenance_note=provenance_note,
        )
        return _snapshot_contract_payload({
            "ok": True,
            "mode": "materialized_subset_snapshot",
            "feature_set_id": manifest.get("feature_set_id"),
            "manifest": manifest,
            "validation": validate_feature_set_manifest_for_model(manifest),
        })
    readiness = _assert_active_values_ready_for_all_active_snapshot(
        factor_holding_period_days=factor_holding_period_days
    )
    manifest = _build_all_active_snapshot_from_active_values(
        feature_set_id=feature_set_id,
        status_filter=status_filter,
        start_date=start_date,
        end_date=end_date,
        label_forward_period=label_forward_period,
        factor_holding_period_days=factor_holding_period_days,
        feature_missing_strategy=feature_missing_strategy,
        readiness=readiness,
    )
    return _snapshot_contract_payload({
        "ok": True,
        "mode": "reused_snapshot" if manifest.get("snapshot_reused") else "built_snapshot",
        "feature_set_id": manifest.get("feature_set_id"),
        "manifest": manifest,
        "active_values_readiness": readiness,
        "validation": validate_feature_set_manifest_for_model(manifest),
        "freshness": feature_set_freshness(manifest),
    }, active_values_readiness_payload=readiness)


def all_active_pointer_summary() -> dict[str, Any]:
    manifest = load_active_feature_set_manifest() or {}
    return {
        "feature_set_id": manifest.get("feature_set_id"),
        "factor_count": manifest.get("factor_count"),
        "feature_count": manifest.get("feature_count"),
        "feature_snapshot_policy_version": manifest.get("feature_snapshot_policy_version"),
        "feature_missing_strategy": manifest.get("feature_missing_strategy"),
        "label_forward_period": manifest.get("label_forward_period"),
        "factor_holding_period_days": manifest.get("factor_holding_period_days"),
        "label_execution_deal_price": manifest.get("label_execution_deal_price"),
        "updates_active_feature_pointer": manifest.get("updates_active_feature_pointer"),
        "manifest_path": manifest.get("manifest_path"),
    }


def feature_set_catalog_summary(limit: int = 30, *, compact: bool = False) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    readiness = active_values_readiness()
    for path in sorted(MODEL_FEATURE_SETS_ROOT.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if compact:
            combined_path = Path(str(manifest.get("combined_factors_file") or manifest.get("feature_file") or ""))
            selection_mode = str(manifest.get("factor_selection_mode") or "")
            snapshot_fp = str(
                manifest.get("active_factor_registry_fingerprint")
                or (manifest.get("active_values_lineage") or {}).get("registry_fingerprint")
                or ""
            )
            current_fp = str(readiness.get("registry_fingerprint") or "")
            stale_reasons = []
            if not combined_path.exists():
                stale_reasons.append("combined_factors_file_not_found")
            if manifest.get("feature_snapshot_policy_version") != FEATURE_SNAPSHOT_POLICY_VERSION:
                stale_reasons.append("feature_snapshot_policy_version_mismatch")
            if selection_mode == "all_active" and snapshot_fp and current_fp and snapshot_fp != current_fp:
                stale_reasons.append("active_registry_fingerprint_mismatch")
            freshness = {
                "stale": bool(stale_reasons),
                "stale_reason": ",".join(stale_reasons),
                "stale_reasons": stale_reasons,
                "factor_registry_fingerprint": snapshot_fp,
                "current_active_factor_count": readiness.get("active_factor_count"),
                "compact": True,
            }
        else:
            freshness = feature_set_freshness(manifest)
        items.append(
            {
                "feature_set_id": manifest.get("feature_set_id") or path.parent.name,
                "factor_count": manifest.get("factor_count"),
                "feature_count": manifest.get("feature_count"),
                "factor_selection_mode": manifest.get("factor_selection_mode") or manifest.get("status_filter") or "snapshot",
                "feature_snapshot_policy_version": manifest.get("feature_snapshot_policy_version"),
                "feature_missing_strategy": manifest.get("feature_missing_strategy"),
                "updates_active_feature_pointer": bool(manifest.get("updates_active_feature_pointer")),
                "source_feature_set_id": manifest.get("source_feature_set_id"),
                "source_type": manifest.get("source_type") or ("all_active" if str(manifest.get("factor_selection_mode") or "") == "all_active" else "manual_or_historical"),
                "feature_set_provenance": _feature_set_provenance_from_manifest(manifest),
                "recommendation_family": manifest.get("recommendation_family"),
                "audit_recommendation_id": manifest.get("audit_recommendation_id"),
                "factor_ids_preview": list(manifest.get("factor_ids") or [])[:20],
                "manifest_path": str(path),
                "combined_factors_file": manifest.get("combined_factors_file") or manifest.get("feature_file"),
                "generated_at": manifest.get("generated_at") or manifest.get("created_at"),
                "freshness": freshness,
                "trainable": not bool(freshness.get("stale")) if str(manifest.get("factor_selection_mode") or "") == "all_active" else True,
                "warnings": freshness.get("stale_reasons") or [],
            }
        )
        if len(items) >= int(limit):
            break
    return {
        "items": items,
        "count": len(items),
        "active_values_readiness": readiness,
        "supports_multiple_feature_sets": True,
        "selection_contract": {
            "explicit_feature_set_id": "preferred for model training and audit-derived subsets",
            "all_active": "allowed only when explicitly building a fresh all-active snapshot",
        },
    }


__all__ = [
    "FEATURE_MISSING_STRATEGY_DEFAULT",
    "FEATURE_MISSING_STRATEGY_QLIB_ONLY",
    "FEATURE_MISSING_STRATEGY_SEMANTIC_V1",
    "FEATURE_MISSING_STRATEGY_STRUCTURAL_ZERO_V2",
    "FEATURE_SNAPSHOT_POLICY_VERSION",
    "active_values_readiness",
    "all_active_pointer_summary",
    "feature_set_catalog_summary",
    "feature_snapshot",
    "feature_set_freshness",
    "load_active_feature_set_manifest",
    "load_feature_set_manifest",
    "model_feature_set_preflight",
    "validate_feature_set_manifest_for_model",
]
