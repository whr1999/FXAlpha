from __future__ import annotations

import configparser
import gc
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from domain.data_foundation.convert_to_quantgpt import (
    convert as convert_quantgpt,
    convert_incremental_from_delta as convert_quantgpt_incremental_from_delta,
    quantgpt_contract_report,
)
from domain.data_foundation.ops_common import (
    DATA_JOB_LOCK_DIR,
    DAILY_STATUS_FILE,
    PROMOTION_BACKUP_ROOT,
    PRODUCTION_LOCK_DIR,
    _acquire_lock,
    _build_snapshot,
    _disk_and_memory,
    _lock_owner,
    _proc_matches,
    _qlib_index_readiness,
    _quantgpt_health,
    _read_json,
    _release_lock,
    _replace_path,
    _restore_state_files,
    _rollback,
    _snapshot_state_files,
    _write_daily_status,
    data_job_guard,
)
from domain.data_foundation.quality_check import check as run_quality_check
from domain.data_foundation.runtime_io import atomic_write_json, atomic_write_text, read_json
from domain.data_foundation.tushare_production import (
    COMPAT_ROOT_NAME,
    LEGACY_COLUMNS,
    LEGACY_NUMERIC_COLUMNS,
    LEGACY_STRING_COLUMNS,
    MIN_ITEMSIZE,
    PRODUCTION_QUALITY_FILE,
    PRODUCTION_RAW_QUALITY_FILE,
    REQUIRED_BENCHMARKS,
    _append_hdf,
    _compat_manifest_path,
    _daily_hdf_columns,
    raw_chunk_to_qlib_frame,
    _read_manifest,
    _snapshot_for_compat,
    _write_compat_metadata,
    _write_trading_calendar,
    prepare_tushare_production_artifacts,
)
from domain.data_foundation.tushare_rebuild import TushareRebuildConfig, _apply_status_fields, tushare_full_rebuild, tushare_preflight
from domain.platform_ops.cleanup_executor import run_cleanup
from integrations.tushare.client import get_tushare_client, tushare_network_preflight
from storage.paths import (
    CURRENT_PRODUCTION_DATASET_FILE,
    DATA_FOUNDATION_ROOT,
    LATEST_STATUS_FILE,
    PROJECT_ROOT,
    PRODUCTION_RAW_HDF5,
    PRODUCTION_RAW_METADATA,
    PRODUCTION_TRADING_CALENDAR_FILE,
    PRODUCTION_TRADING_CALENDAR_META,
    QLIB_CONVERT_SCRIPT,
    QLIB_DATA_ROOT,
    QLIB_INDEX_CONVERT_SCRIPT,
    QLIB_INDEX_META,
    QLIB_STOCK_META,
    QUANTGPT_BENCHMARK_DIR,
    QUANTGPT_DATA_DIR,
)


PACKAGE_PREFIX = "tushare-daily"
STAGING_ROOT = DATA_FOUNDATION_ROOT / "staging"
SOURCE_REBUILD_INTERRUPTED_AFTER_SECONDS = 10 * 60
PRODUCTION_AUDIT_ROOT = DATA_FOUNDATION_ROOT / "audits"
DEFAULT_DEEP_SAMPLE_COUNT = 20
QLIB_FLOAT32_PRICE_TOLERANCE = 0.01
MEMORY_HEADROOM_WAIT_SECONDS = 15 * 60
MEMORY_HEADROOM_SAMPLE_SECONDS = 5
MEMORY_HEADROOM_STABLE_SAMPLES = 2
SAMPLE_EXCLUDED_INDEX_CODES = {"000300.SH", "000905.SH", "000852.SH", "000001.SH", "399001.SZ", "399006.SZ", "000016.SH"}
DAILY_STAGE_SEQUENCE = [
    "source_rebuild",
    "source_prepare_production",
    "merge_production_hdf",
    "merged_quality_check",
    "build_compat_outputs",
    "completed",
]
DAILY_NUMERIC_SCHEMA_EXTENSION_COLUMNS = ["up_limit", "down_limit", "stk_limit_pre_close"]
DAILY_STRING_SCHEMA_EXTENSION_COLUMNS = ["limit_source_kind"]
DAILY_SCHEMA_EXTENSION_COLUMNS = [*DAILY_NUMERIC_SCHEMA_EXTENSION_COLUMNS, *DAILY_STRING_SCHEMA_EXTENSION_COLUMNS]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _source_progress_path(source_package_id: str | None) -> Path | None:
    if not source_package_id:
        return None
    return STAGING_ROOT / str(source_package_id) / "full_rebuild_progress.json"


def _collect_status_values(payload: Any) -> list[str]:
    statuses: list[str] = []
    if isinstance(payload, dict):
        value = payload.get("status")
        if value is not None:
            statuses.append(str(value))
        for child in payload.values():
            statuses.extend(_collect_status_values(child))
    elif isinstance(payload, list):
        for child in payload:
            statuses.extend(_collect_status_values(child))
    return statuses


def _source_progress_summary(source_package_id: str | None) -> dict[str, Any] | None:
    path = _source_progress_path(source_package_id)
    if path is None or not path.exists():
        return None
    payload = _read_json(path)
    if not payload:
        return {"status": "unreadable", "path": str(path)}
    updated_at = payload.get("updated_at") or payload.get("generated_at") or payload.get("last_update_at")
    parsed = _parse_iso_time(updated_at)
    age_seconds = None
    if parsed is not None:
        now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        age_seconds = max(0, int((now - parsed).total_seconds()))
    status_values = _collect_status_values(payload)
    running_like = any(value == "running" for value in status_values)
    completed_like = any(value == "completed" for value in status_values)
    stale = bool(running_like and not completed_like and age_seconds is not None and age_seconds > SOURCE_REBUILD_INTERRUPTED_AFTER_SECONDS)
    return {
        "status": payload.get("status"),
        "path": str(path),
        "updated_at": updated_at,
        "age_seconds": age_seconds,
        "stale_after_seconds": SOURCE_REBUILD_INTERRUPTED_AFTER_SECONDS,
        "has_running_stage": running_like,
        "has_completed_stage": completed_like,
        "stale": stale,
        "current_stage": payload.get("current_stage") or payload.get("stage"),
        "current_key": payload.get("current_key"),
        "cursor": payload.get("cursor"),
        "total": payload.get("total"),
    }


def _has_active_data_process() -> bool:
    try:
        return bool(_proc_matches())
    except Exception:
        return False


def _with_interruption_status(payload: dict[str, Any]) -> dict[str, Any]:
    enriched = _enrich_daily_manifest(payload)
    if enriched.get("status") != "stage_in_progress" or enriched.get("current_stage") != "source_rebuild":
        return enriched
    summary = _source_progress_summary(enriched.get("source_package_id"))
    if summary:
        enriched["source_progress_summary"] = summary
    if summary and summary.get("stale") and not _has_active_data_process():
        enriched["status"] = "interrupted_resumable"
        enriched["interrupted_reason"] = "source_rebuild_progress_stale_no_active_process"
        enriched["interrupted_at"] = _now()
    return enriched


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _daily_stage_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("status") == "completed":
        return {
            "current_stage": "completed",
            "completed_stages": list(DAILY_STAGE_SEQUENCE),
            "completed_stage_count": len(DAILY_STAGE_SEQUENCE),
            "total_stage_count": len(DAILY_STAGE_SEQUENCE),
        }
    completed: list[str] = []
    if payload.get("source_rebuild", {}).get("status") == "completed":
        completed.append("source_rebuild")
    if payload.get("source_prepare_production", {}).get("status") == "completed":
        completed.append("source_prepare_production")
    if payload.get("merge_result"):
        completed.append("merge_production_hdf")
    quality_report = payload.get("quality_report")
    compat_root = payload.get("compat_root") or payload.get("package_root")
    if quality_report or compat_root:
        quality_path = Path(str(quality_report)) if quality_report else Path(str(compat_root)) / COMPAT_ROOT_NAME / "quality_report.json"
        if quality_path.exists():
            completed.append("merged_quality_check")
    if payload.get("compat_manifest") or payload.get("snapshot"):
        completed.append("build_compat_outputs")
    if payload.get("status") == "completed":
        completed.append("completed")
    current_stage = payload.get("current_stage")
    if not current_stage:
        for stage_name in DAILY_STAGE_SEQUENCE:
            if stage_name not in completed:
                current_stage = stage_name
                break
        else:
            current_stage = "completed"
    return {
        "current_stage": current_stage,
        "completed_stages": completed,
        "completed_stage_count": len(completed),
        "total_stage_count": len(DAILY_STAGE_SEQUENCE),
    }


def _enrich_daily_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    source_progress = _source_progress_summary(enriched.get("source_package_id"))
    if source_progress:
        enriched["source_progress_summary"] = source_progress
    stage_summary = _daily_stage_summary(enriched)
    enriched["current_stage"] = stage_summary["current_stage"]
    enriched["stage_summary"] = stage_summary
    return enriched


def _package_id(target_date: str) -> str:
    return f"{PACKAGE_PREFIX}-{datetime.now().strftime('%Y%m%d_%H%M%S')}-target-{target_date}"


def _resolve_package(package_id: str | None = None, latest: bool = False) -> tuple[Path, dict[str, Any]]:
    if package_id:
        root = STAGING_ROOT / str(package_id)
        manifest = _read_manifest(root / "manifest.json")
        if not manifest:
            raise FileNotFoundError(f"manifest missing: {root / 'manifest.json'}")
        return root, manifest
    if not latest:
        raise ValueError("package_id is required unless latest=True")
    manifests = sorted(STAGING_ROOT.glob(f"{PACKAGE_PREFIX}-*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for manifest_path in manifests:
        payload = _read_manifest(manifest_path)
        if payload:
            return manifest_path.parent, payload
    raise FileNotFoundError("no Tushare daily staging package found")


def latest_staging_package() -> dict[str, Any] | None:
    try:
        _, manifest = _resolve_package(latest=True)
        return _with_interruption_status(manifest)
    except Exception:
        return None


def _iter_daily_manifests() -> list[tuple[Path, dict[str, Any]]]:
    matches: list[tuple[Path, dict[str, Any]]] = []
    manifests = sorted(STAGING_ROOT.glob(f"{PACKAGE_PREFIX}-*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for manifest_path in manifests:
        payload = _read_manifest(manifest_path)
        if payload:
            matches.append((manifest_path.parent, _with_interruption_status(payload)))
    return matches


def _daily_manifest_matches_preflight(manifest: dict[str, Any], preflight: dict[str, Any]) -> bool:
    if manifest.get("source") != "tushare":
        return False
    if manifest.get("package_kind") != "daily_update":
        return False
    if manifest.get("status") == "promoted":
        return False
    return (
        str(manifest.get("target_date") or "") == str(preflight.get("target_date") or "")
        and str(manifest.get("selected_target_date") or "") == str(preflight.get("selected_target_date") or "")
        and str(manifest.get("effective_target_date") or "") == str(preflight.get("effective_target_date") or "")
        and str(manifest.get("replace_from_date") or "") == str(preflight.get("replace_from_date") or "")
    )


def _find_reusable_daily_package(preflight: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    reusable_statuses = {"stage_in_progress", "interrupted_resumable", "failed", "completed"}
    for root, manifest in _iter_daily_manifests():
        if manifest.get("status") not in reusable_statuses:
            continue
        if _daily_manifest_matches_preflight(manifest, preflight):
            return root, manifest
    return None


def _require_tushare_production() -> dict[str, Any]:
    current = _read_json(CURRENT_PRODUCTION_DATASET_FILE)
    if not current:
        raise RuntimeError("current_production_dataset_missing")
    if current.get("source") != "tushare":
        raise RuntimeError(f"unsupported_production_source:{current.get('source')}")
    return current


def _production_latest_trade_date(current: dict[str, Any]) -> str:
    latest = (current.get("latest_dates") or {}).get("hdf5") or current.get("latest_trade_date")
    if not latest:
        raise RuntimeError("production_latest_trade_date_missing")
    return str(latest).replace("-", "")


def _latest_dates_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "hdf5": snapshot.get("latest_hdf5_trade_date"),
        "qlib": snapshot.get("latest_qlib_trade_date"),
        "quantgpt": snapshot.get("latest_quantgpt_trade_date"),
    }


def _latest_dates_from_status(payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    return _latest_dates_from_snapshot(snapshot)


def _date_mismatches(expected: dict[str, Any], actual: dict[str, Any], *, prefix: str) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for key in ["hdf5", "qlib", "quantgpt"]:
        expected_value = expected.get(key)
        actual_value = actual.get(key)
        if expected_value and not actual_value:
            mismatches.append(
                {
                    "code": f"{prefix}_{key}_actual_missing",
                    "surface": key,
                    "expected": expected_value,
                    "actual": actual_value,
                }
            )
        elif expected_value and actual_value and str(expected_value) != str(actual_value):
            mismatches.append(
                {
                    "code": f"{prefix}_{key}_latest_mismatch",
                    "surface": key,
                    "expected": expected_value,
                    "actual": actual_value,
                }
            )
    return mismatches


def production_consistency_status(
    *,
    current: dict[str, Any] | None = None,
    latest_status: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_payload = dict(current) if isinstance(current, dict) else _read_json(CURRENT_PRODUCTION_DATASET_FILE)
    latest_payload = dict(latest_status) if isinstance(latest_status, dict) else _read_json(LATEST_STATUS_FILE)
    actual_snapshot = dict(snapshot) if isinstance(snapshot, dict) else _build_snapshot(deep=False)
    actual_dates = _latest_dates_from_snapshot(actual_snapshot)
    registry_dates = current_payload.get("latest_dates") if isinstance(current_payload.get("latest_dates"), dict) else {}
    status_dates = _latest_dates_from_status(latest_payload)
    issues: list[str] = []
    mismatches: list[dict[str, Any]] = []

    if not current_payload:
        issues.append("current_production_dataset_missing")
    if not current_payload.get("production_package_id"):
        issues.append("production_package_id_missing")
    missing_actual = [key for key in ["hdf5", "qlib", "quantgpt"] if not actual_dates.get(key)]
    issues.extend(f"production_surface_missing:{key}" for key in missing_actual)
    nonempty_actual = {str(value) for value in actual_dates.values() if value}
    if len(nonempty_actual) != 1 or missing_actual:
        issues.append("production_surface_latest_dates_mismatch")
    mismatches.extend(_date_mismatches(registry_dates, actual_dates, prefix="registry_vs_actual"))
    mismatches.extend(_date_mismatches(status_dates, actual_dates, prefix="latest_status_vs_actual"))
    if mismatches:
        issues.append("production_registry_actual_mismatch")
    partial_promote_detected = bool(mismatches or len(nonempty_actual) > 1 or missing_actual)
    return {
        "status": "passed" if not issues else "failed",
        "partial_promote_detected": partial_promote_detected,
        "actual_latest_dates": actual_dates,
        "registry_latest_dates": dict(registry_dates),
        "latest_status_dates": status_dates,
        "production_package_id": current_payload.get("production_package_id"),
        "promotion_id": current_payload.get("promotion_id"),
        "mismatches": mismatches,
        "issues": issues,
    }


def _is_wsl() -> bool:
    try:
        osrelease = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        return False
    return "microsoft" in osrelease or "wsl" in osrelease


def _discover_wslconfig_paths() -> list[Path]:
    root = Path("/mnt/c/Users")
    if not root.exists():
        return []
    try:
        return sorted(root.glob("*/.wslconfig"))
    except Exception:
        return []


def _parse_gui_applications_disabled(path: Path) -> bool | None:
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8-sig")
    except Exception:
        return None
    if not parser.has_section("wsl2") or not parser.has_option("wsl2", "guiApplications"):
        return None
    try:
        return parser.getboolean("wsl2", "guiApplications") is False
    except ValueError:
        return None


def _wsl_stability_preflight(*, wslconfig_paths: list[Path] | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "ok",
        "is_wsl": _is_wsl(),
        "checked_paths": [],
        "gui_applications_disabled": None,
        "issues": [],
        "warnings": [],
    }
    if not report["is_wsl"]:
        return report

    paths = wslconfig_paths if wslconfig_paths is not None else _discover_wslconfig_paths()
    report["checked_paths"] = [str(path) for path in paths]
    values = []
    for path in paths:
        if path.exists():
            value = _parse_gui_applications_disabled(path)
            if value is not None:
                values.append(value)
    if any(values):
        report["gui_applications_disabled"] = True
        return report
    if paths:
        report["gui_applications_disabled"] = False
        report["status"] = "failed"
        report["issues"].append("wslg_gui_applications_enabled_for_headless_data_job")
        return report

    report["status"] = "warning"
    report["warnings"].append("wslconfig_not_found_cannot_verify_wslg_disabled")
    return report


def _hdf_smoke_preflight() -> dict[str, Any]:
    smoke_dir = DATA_FOUNDATION_ROOT / "hdf_smoke"
    smoke_path = smoke_dir / f"hdf_smoke_{os.getpid()}.h5"
    try:
        smoke_dir.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(
            [{"trade_date": pd.Timestamp("2000-01-03"), "code": "000001.SZ", "close": 1.0}]
        ).set_index("trade_date")
        frame.to_hdf(smoke_path, key="/daily", mode="w", format="table")
        loaded = pd.read_hdf(smoke_path, key="/daily")
        if loaded.empty or float(loaded["close"].iloc[0]) != 1.0:
            raise RuntimeError("hdf_smoke_readback_mismatch")
        return {"status": "ok", "path": str(smoke_path)}
    except Exception as exc:
        return {"status": "failed", "path": str(smoke_path), "error": str(exc)}
    finally:
        try:
            if smoke_path.exists():
                smoke_path.unlink()
            if smoke_dir.exists() and not any(smoke_dir.iterdir()):
                smoke_dir.rmdir()
        except Exception:
            pass


def _stability_preflight() -> dict[str, Any]:
    stability = _wsl_stability_preflight()
    hdf_smoke = _hdf_smoke_preflight()
    stability["hdf_smoke"] = hdf_smoke
    if hdf_smoke.get("status") != "ok":
        stability["status"] = "failed"
        stability.setdefault("issues", []).append("hdf_smoke_failed")
    return stability


def _network_direct_probe_summary(network: dict[str, Any]) -> dict[str, Any]:
    host_route_gate = network.get("host_route_gate") or {}
    http_probe = network.get("http_probe") or {}
    selected_route = network.get("selected_route") or {}
    return {
        "status": network.get("status"),
        "route_iface": selected_route.get("iface"),
        "route_gateway": selected_route.get("gateway"),
        "resolved_ips": network.get("resolved_ips") or [],
        "reachable_ips": network.get("reachable_ips") or [],
        "http_probe_ip": http_probe.get("ip"),
        "host_route_gate_status": host_route_gate.get("status"),
        "host_route_issues": host_route_gate.get("issues") or [],
        "issues": network.get("issues") or [],
        "warnings": network.get("warnings") or [],
    }


def _cleanup_preview_summary() -> dict[str, Any]:
    try:
        preview = run_cleanup(profile="safe", execute=False, write_report=False)
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}
    summary = preview.get("summary") or {}
    return {
        "status": "ok",
        "profile": "safe",
        "reclaimable_bytes": summary.get("reclaimable_bytes"),
        "reclaimable_human": summary.get("reclaimable_human"),
        "candidate_count": summary.get("candidate_count"),
        "executable_count": summary.get("executable_count"),
        "blocked_count": summary.get("blocked_count"),
        "by_kind": summary.get("by_kind") or {},
    }


def _post_promote_cleanup() -> dict[str, Any]:
    preview = run_cleanup(profile="safe", execute=False, write_report=True)
    summary = preview.get("summary") or {}
    reclaimable = int(summary.get("reclaimable_bytes") or 0)
    result = {
        "preview": {
            "status": "completed",
            "report_path": preview.get("report_path"),
            "reclaimable_bytes": reclaimable,
            "reclaimable_human": summary.get("reclaimable_human"),
            "candidate_count": summary.get("candidate_count"),
            "executable_count": summary.get("executable_count"),
            "blocked_count": summary.get("blocked_count"),
        },
        "execute": None,
        "execute_policy": {
            "profile": "safe",
            "trigger": "explicit_operator_approval_required",
            "eligible": False,
            "blocked_reason": "cleanup_execution_decoupled_from_daily_routine",
        },
    }
    return result


def _normalize_target_date_value(target_date: str | None) -> str | None:
    text = str(target_date or "auto").strip().lower()
    if text in {"", "auto"}:
        return None
    text = text.replace("-", "")
    if not (text.isdigit() and len(text) == 8):
        raise ValueError(f"target_date must be auto or YYYYMMDD: {target_date}")
    return text


def _expected_published_stock_count() -> int:
    quality = _read_json(PRODUCTION_QUALITY_FILE)
    latest_activity = quality.get("latest_code_activity") if isinstance(quality.get("latest_code_activity"), dict) else {}
    value = int((latest_activity or {}).get("latest_day_stock_count") or 0)
    # Missing quality evidence must not turn a tiny partial response into a
    # publish-complete signal. The production A-share universe is much larger
    # than this conservative floor.
    return value if value > 0 else 1000


def _tushare_auto_target_date(*, today: str | None = None, lookback_days: int = 14, client=None) -> str:
    today_value = _normalize_target_date_value(today) or datetime.now().strftime("%Y%m%d")
    start_value = (datetime.strptime(today_value, "%Y%m%d") - timedelta(days=max(1, int(lookback_days)))).strftime("%Y%m%d")
    pro = client if client is not None else get_tushare_client(network_mode="direct")
    calendar = pro.trade_cal(
        exchange="SSE",
        start_date=start_value,
        end_date=today_value,
        fields="exchange,cal_date,is_open,pretrade_date",
    )
    if calendar is None or calendar.empty:
        raise RuntimeError("tushare_auto_target_calendar_empty")
    open_dates = sorted(
        str(row.cal_date)
        for row in calendar.itertuples(index=False)
        if str(getattr(row, "is_open", "")) in {"1", "1.0", "True", "true"}
    )
    minimum_expected_rows = max(1, int(np.ceil(_expected_published_stock_count() * 0.95)))
    for trade_date in reversed(open_dates):
        if trade_date == today_value and datetime.now().strftime("%Y%m%d") == today_value and datetime.now().hour < 19:
            continue
        daily = pro.daily(trade_date=trade_date, fields="ts_code,trade_date")
        if daily is None or daily.empty:
            continue
        if len(daily) < minimum_expected_rows:
            continue
        daily_basic = pro.daily_basic(trade_date=trade_date, fields="ts_code,trade_date")
        if daily_basic is None or daily_basic.empty:
            continue
        minimum_daily_basic_rows = max(1, int(len(daily) * 0.95))
        if len(daily_basic) < minimum_daily_basic_rows:
            continue
        return trade_date
    raise RuntimeError("tushare_auto_target_daily_unavailable")


def _target_date_value(target_date: str | None) -> str:
    explicit_target = _normalize_target_date_value(target_date)
    if explicit_target:
        return explicit_target
    return _tushare_auto_target_date()


def _promotion_idle_state() -> dict[str, Any]:
    qgpt = _quantgpt_health()
    processes = _proc_matches()
    blockers: list[str] = []
    active_tasks = ((qgpt.get("payload") or {}).get("active_tasks") or 0) if qgpt.get("ok") else None
    if active_tasks:
        blockers.append("quantgpt_has_active_tasks")
    if processes:
        blockers.append("data_or_research_processes_active")
    return {
        "blockers": blockers,
        "quantgpt_health": qgpt,
        "processes": processes,
    }


def _wait_for_idle(timeout_minutes: int) -> dict[str, Any]:
    deadline = time.time() + max(1, int(timeout_minutes)) * 60
    state = _promotion_idle_state()
    while state["blockers"] and time.time() < deadline:
        time.sleep(30)
        state = _promotion_idle_state()
    state["timed_out"] = bool(state["blockers"])
    return state


def _compact_tushare_preflight(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": plan.get("status"),
        "start_date": plan.get("start_date"),
        "cutoff_date": plan.get("cutoff_date"),
        "effective_target_date": plan.get("effective_target_date"),
        "selected_target_date": plan.get("selected_target_date"),
        "padded_start_date": plan.get("padded_start_date"),
        "trade_date_count": plan.get("trade_date_count"),
        "trade_dates_head": (plan.get("trade_dates") or [])[:5],
        "trade_dates_tail": (plan.get("trade_dates") or [])[-5:],
        "trade_dates_sha256": plan.get("trade_dates_sha256"),
        "code_count": plan.get("code_count"),
        "codes_sha256": plan.get("codes_sha256"),
        "proxy_mode": plan.get("proxy_mode"),
        "network": plan.get("network"),
    }


def _wait_for_memory_headroom(
    *,
    timeout_seconds: int = MEMORY_HEADROOM_WAIT_SECONDS,
    sample_seconds: float = MEMORY_HEADROOM_SAMPLE_SECONDS,
    stable_samples: int = MEMORY_HEADROOM_STABLE_SAMPLES,
) -> dict[str, Any]:
    started = time.monotonic()
    consecutive = 0
    samples = 0
    latest = _disk_and_memory()
    while True:
        samples += 1
        if latest.get("disk_ok") and latest.get("mem_ok"):
            consecutive += 1
        else:
            consecutive = 0
        if consecutive >= max(1, int(stable_samples)):
            return {
                "status": "ready",
                "waited_seconds": round(time.monotonic() - started, 3),
                "sample_count": samples,
                "stable_sample_count": consecutive,
                "resources": latest,
            }
        elapsed = time.monotonic() - started
        if elapsed >= max(0, int(timeout_seconds)):
            return {
                "status": "timed_out",
                "waited_seconds": round(elapsed, 3),
                "sample_count": samples,
                "stable_sample_count": consecutive,
                "resources": latest,
            }
        time.sleep(max(0.0, float(sample_seconds)))
        latest = _disk_and_memory()


def data_daily_preflight(target_date: str | None = None, *, for_promotion: bool = True) -> dict[str, Any]:
    # Capture resource headroom before consistency/network/HDF probes allocate
    # memory. The gate is about whether it is safe to start those operations,
    # not about charging the preflight's own working set a second time.
    resources = _disk_and_memory()
    current = _require_tushare_production()
    explicit_target = _normalize_target_date_value(target_date)
    current_latest = _production_latest_trade_date(current)
    production_consistency = production_consistency_status(current=current)
    locks = {
        "production_update": _lock_owner(PRODUCTION_LOCK_DIR) if PRODUCTION_LOCK_DIR.exists() else {},
        "data_job": _lock_owner(DATA_JOB_LOCK_DIR) if DATA_JOB_LOCK_DIR.exists() else {},
    }
    network = dict(tushare_network_preflight(verify_http=True))
    network["direct_probe_summary"] = _network_direct_probe_summary(network)
    stability = _stability_preflight()
    cleanup_preview = {"status": "deferred", "reason": "not_on_preflight_critical_path"}
    blockers: list[str] = []
    warnings: list[str] = []

    if not resources.get("disk_ok"):
        blockers.append("disk_free_below_80gb")
    if not resources.get("mem_ok"):
        blockers.append("mem_available_below_8gb")
    if production_consistency.get("partial_promote_detected") or production_consistency.get("status") != "passed":
        blockers.append("partial_promote_detected")
    if locks["production_update"] and locks["production_update"].get("alive", True):
        blockers.append("production_update_lock_active")
    data_job_owner = locks["data_job"]
    if (
        data_job_owner
        and data_job_owner.get("alive", True)
        and data_job_owner.get("pid") != os.getpid()
    ):
        blockers.append("data_job_lock_active")
    if network.get("status") != "ok":
        blockers.append("tushare_network_not_direct")
    if stability.get("status") == "failed":
        blockers.extend(stability.get("issues") or [])
    warnings.extend(stability.get("warnings") or [])

    idle = _promotion_idle_state()
    if for_promotion:
        blockers.extend(idle["blockers"])
    else:
        warnings.extend(idle["blockers"])

    target = explicit_target
    target_client = None
    target_resolution_error = None
    if target is None and network.get("status") == "ok":
        try:
            target_client = get_tushare_client(network_mode="direct", network_report=network)
            target = _tushare_auto_target_date(client=target_client)
        except Exception as exc:
            target_resolution_error = str(exc)
            blockers.append("auto_target_resolution_failed")
    elif target is None:
        target_resolution_error = "network_not_ready"

    if target is None:
        return {
            "status": "blocked",
            "already_current": False,
            "target_date": target_date or "auto",
            "selected_target_date": None,
            "effective_target_date": None,
            "replace_from_date": current_latest,
            "current_latest_trade_date": current_latest,
            "source_rebuild": None,
            "blockers": blockers,
            "warnings": warnings,
            "resources": resources,
            "stability": stability,
            "cleanup_preview_summary": cleanup_preview,
            "production_consistency": production_consistency,
            "network": network,
            "idle_state": idle,
            "current_production_dataset": current,
            "target_resolution_error": target_resolution_error,
            "generated_at": _now(),
        }

    if target <= current_latest:
        return {
            "status": "go" if not blockers else "blocked",
            "already_current": True,
            "target_date": target,
            "selected_target_date": current_latest,
            "effective_target_date": current_latest,
            "replace_from_date": current_latest,
            "current_latest_trade_date": current_latest,
            "blockers": blockers,
            "warnings": warnings,
            "resources": resources,
            "stability": stability,
            "cleanup_preview_summary": cleanup_preview,
            "production_consistency": production_consistency,
            "network": network,
            "idle_state": idle,
            "current_production_dataset": current,
            "target_resolution_error": target_resolution_error,
            "generated_at": _now(),
        }

    if blockers:
        return {
            "status": "blocked",
            "already_current": False,
            "target_date": target,
            "selected_target_date": target,
            "effective_target_date": target,
            "replace_from_date": current_latest,
            "current_latest_trade_date": current_latest,
            "source_rebuild": None,
            "blockers": blockers,
            "warnings": warnings,
            "resources": resources,
            "stability": stability,
            "cleanup_preview_summary": cleanup_preview,
            "production_consistency": production_consistency,
            "network": network,
            "idle_state": idle,
            "current_production_dataset": current,
            "target_resolution_error": target_resolution_error,
            "generated_at": _now(),
        }

    plan = tushare_preflight(
        start_date=current_latest,
        cutoff_date=target,
        pad_trading_days=120,
        proxy_mode="direct",
        client=target_client,
        network_report=network,
    )
    selected_target = plan.get("selected_target_date") or plan.get("effective_target_date")
    return {
        "status": "go" if not blockers else "blocked",
        "already_current": bool(selected_target and selected_target <= current_latest),
        "target_date": target,
        "selected_target_date": selected_target,
        "effective_target_date": plan.get("effective_target_date"),
        "replace_from_date": current_latest,
        "current_latest_trade_date": current_latest,
        "source_rebuild": {
            "start_date": current_latest,
            "cutoff_date": target,
            "padded_start_date": plan.get("padded_start_date"),
            "trade_date_count": plan.get("trade_date_count"),
            "code_count": plan.get("code_count"),
            "proxy_mode": "direct",
        },
        "blockers": blockers,
        "warnings": warnings,
        "resources": resources,
        "stability": stability,
        "cleanup_preview_summary": cleanup_preview,
        "production_consistency": production_consistency,
        "network": network,
        "idle_state": idle,
        "current_production_dataset": current,
        "tushare_preflight": _compact_tushare_preflight(plan),
        "generated_at": _now(),
    }


def _write_daily_manifest(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    enriched = _enrich_daily_manifest(payload)
    _write_json(root / "manifest.json", enriched)
    _write_daily_status({"status": enriched.get("status"), "latest_stage": enriched, "generated_at": _now()})
    return enriched


def _copy_quality_reports(source_package_root: Path, compat_root: Path) -> tuple[str, str]:
    source_quality = source_package_root / "silver" / "quality_report.json"
    source_raw_quality = source_package_root / "silver" / "raw_quality_report.json"
    compat_quality = compat_root / "source_window_quality_report.json"
    compat_raw_quality = compat_root / "raw_quality_report.json"
    shutil.copy2(source_quality, compat_quality)
    raw_payload = _read_json(source_raw_quality)
    raw_payload.setdefault("notes", []).append(
        "This raw quality report covers the Tushare daily rebuild window used to refresh production."
    )
    _write_json(compat_raw_quality, raw_payload)
    return str(compat_quality), str(compat_raw_quality)


def _merge_compat_hdf(*, production_hdf: Path, delta_hdf: Path, output_hdf: Path, replace_from_date: str, chunk_rows: int = 250000) -> dict[str, Any]:
    replace_from = pd.Timestamp(datetime.strptime(replace_from_date, "%Y%m%d"))
    output_hdf.parent.mkdir(parents=True, exist_ok=True)
    working_hdf = output_hdf.with_name(f".{output_hdf.name}.tmp-{os.getpid()}")
    if working_hdf.exists():
        working_hdf.unlink()

    raw_carry: dict[str, float] = {}
    adj_carry: dict[str, float] = {}
    original_rows = 0
    preserved_rows = 0
    removed_rows = 0
    boundary_repaired_rows = 0
    info_frame: pd.DataFrame | None = None
    itemsize_for = lambda frame: {key: value for key, value in MIN_ITEMSIZE.items() if key in frame.columns}
    production_columns: list[str] | None = None
    schema_alignment: dict[str, Any] = {
        "status": "aligned",
        "production_column_count": None,
        "delta_missing_columns_filled": [],
        "delta_extra_columns_ignored": [],
    }
    with pd.HDFStore(delta_hdf, mode="r") as delta_schema_store:
        delta_schema_columns = list(delta_schema_store.select("/daily", start=0, stop=1).columns)
    known_daily_columns = set(LEGACY_COLUMNS) | {"list_date", "delist_date", *DAILY_SCHEMA_EXTENSION_COLUMNS}

    def derive_limit_source_kind(frame: pd.DataFrame) -> pd.Series:
        up_limit = pd.to_numeric(frame.get("up_limit"), errors="coerce")
        down_limit = pd.to_numeric(frame.get("down_limit"), errors="coerce")
        if not isinstance(up_limit, pd.Series):
            up_limit = pd.Series(pd.NA, index=frame.index, dtype="float64")
        if not isinstance(down_limit, pd.Series):
            down_limit = pd.Series(pd.NA, index=frame.index, dtype="float64")
        official = up_limit.notna() & down_limit.notna()
        if "LIST_DATE" in frame.columns:
            list_dates = pd.to_datetime(frame["LIST_DATE"].astype("string").str.replace("-", "", regex=False), format="%Y%m%d", errors="coerce")
        elif "list_date" in frame.columns:
            list_dates = pd.to_datetime(frame["list_date"], errors="coerce")
        else:
            list_dates = pd.Series(pd.NaT, index=frame.index)
        if "kline_time" in frame.columns:
            trade_dates = pd.to_datetime(frame["kline_time"], errors="coerce")
        elif "trade_date" in frame.columns:
            trade_dates = pd.to_datetime(frame["trade_date"], errors="coerce")
        else:
            trade_dates = pd.Series(pd.NaT, index=frame.index)
        structural_no_limit = official.eq(False) & list_dates.dt.normalize().eq(trade_dates.dt.normalize())
        index_row = (
            frame["list_status"].astype("string").str.upper().eq("I")
            if "list_status" in frame.columns
            else pd.Series(False, index=frame.index)
        )
        values = pd.Series("missing", index=frame.index, dtype="string")
        values.loc[index_row] = "index"
        values.loc[structural_no_limit] = "structural_no_limit"
        values.loc[official] = "official"
        return values

    def align_to_production_schema(frame: pd.DataFrame, *, role: str) -> pd.DataFrame:
        nonlocal production_columns
        work = frame.copy()
        if "list_date" in work.columns:
            work["list_date"] = work["list_date"].astype("object")
        elif "LIST_DATE" in work.columns:
            work["list_date"] = work["LIST_DATE"].astype("object")
        if "delist_date" in work.columns:
            work["delist_date"] = work["delist_date"].astype("object")
        if production_columns is None:
            production_columns = list(work.columns)
            # Build the union before the first preserved production chunk is written.
            # Known columns introduced by the daily source are a compatible schema
            # extension; genuinely unknown columns still require an explicit migration.
            for column in delta_schema_columns:
                if column in known_daily_columns and column not in production_columns:
                    production_columns.append(column)
            for column in DAILY_SCHEMA_EXTENSION_COLUMNS:
                if column not in production_columns:
                    production_columns.append(column)
            schema_alignment["production_column_count"] = len(production_columns)
        if role == "delta":
            missing = [column for column in production_columns if column not in work.columns]
            extra = [column for column in work.columns if column not in production_columns]
            schema_alignment["delta_missing_columns_filled"] = sorted(
                set(schema_alignment["delta_missing_columns_filled"])
                | {column for column in missing if column not in DAILY_SCHEMA_EXTENSION_COLUMNS}
            )
            schema_alignment["delta_extra_columns_ignored"] = sorted(
                set(schema_alignment["delta_extra_columns_ignored"]) | set(extra)
            )
            if extra:
                schema_alignment["status"] = "schema_migration_required"
                raise RuntimeError(f"daily_delta_schema_migration_required:{','.join(sorted(extra))}")
        for column in production_columns:
            if column not in work.columns:
                work[column] = pd.Series(pd.NA, index=work.index, dtype="object")
        for column in LEGACY_NUMERIC_COLUMNS:
            if column in work.columns:
                work[column] = pd.to_numeric(work[column], errors="coerce").astype("float64")
        derived_limit_source = derive_limit_source_kind(work)
        if "limit_source_kind" not in work.columns:
            work["limit_source_kind"] = derived_limit_source
        else:
            source = work["limit_source_kind"].astype("string")
            blank = source.isna() | source.str.strip().isin(["", "<NA>", "nan", "None"])
            source.loc[blank] = derived_limit_source.loc[blank]
            work["limit_source_kind"] = source
        for column in set(LEGACY_STRING_COLUMNS) | set(DAILY_STRING_SCHEMA_EXTENSION_COLUMNS):
            if column in work.columns:
                work[column] = work[column].astype("string")
        ordered = work.loc[:, production_columns].copy()
        numeric_columns = set(LEGACY_NUMERIC_COLUMNS) | set(DAILY_NUMERIC_SCHEMA_EXTENSION_COLUMNS)
        string_columns = set(LEGACY_STRING_COLUMNS) | set(DAILY_STRING_SCHEMA_EXTENSION_COLUMNS)
        rebuilt: dict[str, Any] = {}
        for column in production_columns:
            if column in numeric_columns:
                rebuilt[column] = pd.to_numeric(ordered[column], errors="coerce").astype("float64").to_numpy()
            elif column in string_columns:
                rebuilt[column] = ordered[column].astype("string")
            else:
                rebuilt[column] = ordered[column]
        return pd.DataFrame(rebuilt, index=ordered.index)

    with pd.HDFStore(production_hdf, mode="r") as store:
        original_rows = int(store.get_storer("/daily").nrows or 0)
        if "/info" in store:
            info_frame = store["/info"]
        for start in range(0, original_rows, chunk_rows):
            chunk = store.select("/daily", start=start, stop=min(start + chunk_rows, original_rows))
            if chunk.empty:
                continue
            schema_probe = chunk.head(1).copy()
            if "st_status" not in schema_probe.columns:
                if "name" not in schema_probe.columns and "SECURITY_NAME" in schema_probe.columns:
                    schema_probe["name"] = schema_probe["SECURITY_NAME"]
                    schema_probe = _apply_status_fields(schema_probe).drop(columns=["name"])
                else:
                    schema_probe = _apply_status_fields(schema_probe)
            align_to_production_schema(schema_probe, role="production")
            trade_stamp = pd.to_datetime(chunk["kline_time"], errors="coerce")
            keep = trade_stamp < replace_from
            if not keep.any():
                continue
            preserved = chunk.loc[keep].copy()
            preserved_codes = preserved["code"].astype(str)
            preserved["code"] = preserved_codes
            if "st_status" not in preserved.columns:
                if "name" not in preserved.columns and "SECURITY_NAME" in preserved.columns:
                    preserved["name"] = preserved["SECURITY_NAME"]
                    preserved = _apply_status_fields(preserved).drop(columns=["name"])
                else:
                    preserved = _apply_status_fields(preserved)
            preserved = align_to_production_schema(preserved, role="production")
            raw_carry.update(preserved.dropna(subset=["close"]).groupby("code", sort=False)["close"].last().to_dict())
            if "adj_close" in preserved.columns:
                adj_carry.update(preserved.dropna(subset=["adj_close"]).groupby("code", sort=False)["adj_close"].last().to_dict())
            _append_hdf(working_hdf, "/daily", preserved, append=working_hdf.exists(), min_itemsize=itemsize_for(preserved))
            preserved_rows += int(len(preserved))
    removed_rows = int(original_rows - preserved_rows)
    if info_frame is not None:
        info_frame.to_hdf(working_hdf, key="/info", mode="a", format="table")

    delta_rows = 0
    with pd.HDFStore(delta_hdf, mode="r") as store:
        nrows = int(store.get_storer("/daily").nrows or 0)
        for start in range(0, nrows, chunk_rows):
            chunk = store.select("/daily", start=start, stop=min(start + chunk_rows, nrows))
            if chunk.empty:
                continue
            work = chunk.copy()
            if "st_status" not in work.columns:
                if "name" not in work.columns and "SECURITY_NAME" in work.columns:
                    work["name"] = work["SECURITY_NAME"]
                    work = _apply_status_fields(work).drop(columns=["name"])
                else:
                    work = _apply_status_fields(work)
            codes = work["code"].astype(str)
            raw_values = codes.map(raw_carry)
            raw_fill = pd.Series(False, index=work.index)
            adj_fill = pd.Series(False, index=work.index)
            if {"pre_close", "close"}.issubset(work.columns):
                raw_fill = work["pre_close"].isna() & raw_values.notna()
                if raw_fill.any():
                    work.loc[raw_fill, "pre_close"] = raw_values.loc[raw_fill].astype(float).to_numpy()
            if {"adj_pre_close", "adj_close"}.issubset(work.columns):
                adj_values = codes.map(adj_carry)
                adj_fill = work["adj_pre_close"].isna() & adj_values.notna()
                if adj_fill.any():
                    work.loc[adj_fill, "adj_pre_close"] = adj_values.loc[adj_fill].astype(float).to_numpy()
            repair_mask = raw_fill | adj_fill
            if repair_mask.any():
                if {"pct_chg", "close", "pre_close"}.issubset(work.columns):
                    raw_valid = repair_mask & work["pre_close"].notna() & work["pre_close"].ne(0)
                    work.loc[raw_valid, "pct_chg"] = (
                        (work.loc[raw_valid, "close"].astype(float) - work.loc[raw_valid, "pre_close"].astype(float))
                        / work.loc[raw_valid, "pre_close"].astype(float)
                        * 100.0
                    )
                if {"amp", "high", "low", "pre_close"}.issubset(work.columns):
                    raw_valid = repair_mask & work["pre_close"].notna() & work["pre_close"].ne(0)
                    work.loc[raw_valid, "amp"] = (
                        (work.loc[raw_valid, "high"].astype(float) - work.loc[raw_valid, "low"].astype(float))
                        / work.loc[raw_valid, "pre_close"].astype(float)
                        * 100.0
                    )
                if {"adj_pct_chg", "adj_close", "adj_pre_close"}.issubset(work.columns):
                    adj_valid = repair_mask & work["adj_pre_close"].notna() & work["adj_pre_close"].ne(0)
                    work.loc[adj_valid, "adj_pct_chg"] = (
                        (work.loc[adj_valid, "adj_close"].astype(float) - work.loc[adj_valid, "adj_pre_close"].astype(float))
                        / work.loc[adj_valid, "adj_pre_close"].astype(float)
                        * 100.0
                    )
                if {"adj_amp", "adj_high", "adj_low", "adj_pre_close"}.issubset(work.columns):
                    adj_valid = repair_mask & work["adj_pre_close"].notna() & work["adj_pre_close"].ne(0)
                    work.loc[adj_valid, "adj_amp"] = (
                        (work.loc[adj_valid, "adj_high"].astype(float) - work.loc[adj_valid, "adj_low"].astype(float))
                        / work.loc[adj_valid, "adj_pre_close"].astype(float)
                        * 100.0
                    )
                boundary_repaired_rows += int(repair_mask.sum())
            work = align_to_production_schema(work, role="delta")
            _append_hdf(working_hdf, "/daily", work, append=True, min_itemsize=itemsize_for(work))
            delta_rows += int(len(work))

    with pd.HDFStore(working_hdf, mode="r") as store:
        if "/daily" not in store:
            raise RuntimeError("merged_hdf_missing_daily_key")
        final_rows = int(store.get_storer("/daily").nrows or 0)
    working_hdf.replace(output_hdf)

    return {
        "preserved_rows": preserved_rows,
        "removed_rows": removed_rows,
        "delta_rows": delta_rows,
        "boundary_repaired_rows": boundary_repaired_rows,
        "schema_alignment": schema_alignment,
        "final_rows": final_rows,
        "final_hdf": str(output_hdf),
    }


def _extract_window_hdf(*, source_hdf: Path, output_hdf: Path, replace_from_date: str, chunk_rows: int = 250000) -> dict[str, Any]:
    replace_from = pd.Timestamp(datetime.strptime(replace_from_date, "%Y%m%d"))
    output_hdf.parent.mkdir(parents=True, exist_ok=True)
    working_hdf = output_hdf.with_name(f".{output_hdf.name}.tmp-{os.getpid()}")
    working_hdf.unlink(missing_ok=True)

    written_rows = 0
    info_frame: pd.DataFrame | None = None
    itemsize_for = lambda frame: {key: value for key, value in MIN_ITEMSIZE.items() if key in frame.columns}
    try:
        with pd.HDFStore(source_hdf, mode="r") as store:
            nrows = int(store.get_storer("/daily").nrows or 0)
            if "/info" in store:
                info_frame = store["/info"]
            for start in range(0, nrows, chunk_rows):
                chunk = store.select("/daily", start=start, stop=min(start + chunk_rows, nrows))
                if chunk.empty:
                    continue
                trade_stamp = pd.to_datetime(chunk["kline_time"], errors="coerce")
                window = chunk.loc[trade_stamp >= replace_from].copy()
                if window.empty:
                    continue
                _append_hdf(working_hdf, "/daily", window, append=working_hdf.exists(), min_itemsize=itemsize_for(window))
                written_rows += int(len(window))
        if info_frame is not None:
            info_frame.to_hdf(working_hdf, key="/info", mode="a", format="table")
        if not working_hdf.exists():
            raise RuntimeError(f"window_hdf_empty:{replace_from_date}")
        os.replace(working_hdf, output_hdf)
    except Exception:
        working_hdf.unlink(missing_ok=True)
        raise
    return {
        "status": "completed",
        "source_hdf": str(source_hdf),
        "window_hdf": str(output_hdf),
        "replace_from_date": replace_from_date,
        "written_rows": written_rows,
    }


def _recover_existing_merge_result(*, output_hdf: Path, delta_hdf: Path, selected_target_date: str) -> dict[str, Any] | None:
    if not output_hdf.exists() or output_hdf.stat().st_size <= 0:
        return None
    try:
        with pd.HDFStore(output_hdf, mode="r") as store:
            if "/daily" not in store:
                return None
            final_rows = int(store.get_storer("/daily").nrows or 0)
            tail = store.select("/daily", start=max(0, final_rows - 250000), stop=final_rows)
        if tail.empty or "kline_time" not in tail.columns:
            return None
        latest = pd.to_datetime(tail["kline_time"], errors="coerce").max()
        target = pd.Timestamp(datetime.strptime(selected_target_date, "%Y%m%d"))
        if pd.isna(latest) or latest < target:
            return None
        with pd.HDFStore(delta_hdf, mode="r") as store:
            delta_rows = int(store.get_storer("/daily").nrows or 0)
        return {
            "preserved_rows": max(0, final_rows - delta_rows),
            "delta_rows": delta_rows,
            "final_rows": final_rows,
            "final_hdf": str(output_hdf),
            "recovered_from_existing_output": True,
        }
    except Exception:
        return None


def _trade_date_key(value: Any) -> str | None:
    try:
        stamp = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(stamp):
        return None
    return pd.Timestamp(stamp).strftime("%Y-%m-%d")


def _production_hdf_audit(*, replace_from_date: str | None = None, full_scan: bool = False, chunk_rows: int = 250000) -> dict[str, Any]:
    if not PRODUCTION_RAW_HDF5.exists():
        return {"status": "failed", "issues": ["production_hdf_missing"], "path": str(PRODUCTION_RAW_HDF5)}
    replace_from = None
    if replace_from_date and not full_scan:
        replace_from = pd.Timestamp(datetime.strptime(str(replace_from_date).replace("-", ""), "%Y%m%d"))

    seen_keys: set[str] = set()
    duplicate_count = 0
    scanned_rows = 0
    latest_trade_date: str | None = None
    latest_rows = 0
    latest_stock_rows = 0
    latest_core_nulls = {field: 0 for field in ["open", "high", "low", "close", "volume", "amount", "pre_close"]}
    latest_structural_nulls = {field: 0 for field in ["pre_close"]}
    price_issue_count = 0
    scanned_min_date: str | None = None
    scanned_max_date: str | None = None

    with pd.HDFStore(PRODUCTION_RAW_HDF5, mode="r") as store:
        if "/daily" not in store:
            return {"status": "failed", "issues": ["production_hdf_daily_key_missing"], "path": str(PRODUCTION_RAW_HDF5)}
        total_rows = int(store.get_storer("/daily").nrows or 0)
        for start in range(0, total_rows, chunk_rows):
            chunk = store.select("/daily", start=start, stop=min(start + chunk_rows, total_rows))
            if chunk.empty:
                continue
            dates = pd.to_datetime(chunk["kline_time"], errors="coerce")
            mask = dates.notna()
            if replace_from is not None:
                mask &= dates >= replace_from
            window = chunk.loc[mask].copy()
            if window.empty:
                continue
            window_dates = dates.loc[mask]
            date_keys = window_dates.dt.strftime("%Y-%m-%d")
            scanned_rows += int(len(window))
            chunk_min = str(date_keys.min())
            chunk_max = str(date_keys.max())
            scanned_min_date = chunk_min if scanned_min_date is None else min(scanned_min_date, chunk_min)
            scanned_max_date = chunk_max if scanned_max_date is None else max(scanned_max_date, chunk_max)

            keys = window["code"].astype(str) + "|" + date_keys.astype(str)
            duplicate_count += int(keys.duplicated().sum())
            for key in keys.drop_duplicates():
                if key in seen_keys:
                    duplicate_count += 1
                else:
                    seen_keys.add(str(key))

            price_mask = pd.Series(False, index=window.index)
            for field in ["open", "high", "low", "close"]:
                if field in window.columns:
                    price_mask |= pd.to_numeric(window[field], errors="coerce").le(0)
            if {"high", "low"}.issubset(window.columns):
                price_mask |= pd.to_numeric(window["high"], errors="coerce").lt(pd.to_numeric(window["low"], errors="coerce"))
            if {"volume"}.issubset(window.columns):
                price_mask |= pd.to_numeric(window["volume"], errors="coerce").lt(0)
            if {"amount"}.issubset(window.columns):
                price_mask |= pd.to_numeric(window["amount"], errors="coerce").lt(0)
            price_issue_count += int(price_mask.fillna(False).sum())

            chunk_latest = chunk_max
            latest_mask = date_keys == chunk_latest
            if latest_trade_date is None or chunk_latest > latest_trade_date:
                latest_trade_date = chunk_latest
                latest_rows = 0
                latest_stock_rows = 0
                latest_core_nulls = {field: 0 for field in latest_core_nulls}
                latest_structural_nulls = {field: 0 for field in latest_structural_nulls}
            if chunk_latest == latest_trade_date:
                latest_slice = window.loc[latest_mask]
                latest_rows += int(len(latest_slice))
                if "list_status" in latest_slice.columns:
                    latest_stock_rows += int((~latest_slice["list_status"].astype("string").str.upper().eq("I")).sum())
                else:
                    latest_stock_rows += int(
                        (~latest_slice["code"].astype(str).isin(SAMPLE_EXCLUDED_INDEX_CODES)).sum()
                    )
                if {"LIST_DATE", "kline_time"}.issubset(latest_slice.columns):
                    list_text = latest_slice["LIST_DATE"].astype("string").str.replace("-", "", regex=False).str.slice(0, 8)
                    trade_text = pd.to_datetime(latest_slice["kline_time"], errors="coerce").dt.strftime("%Y%m%d")
                    listing_day = pd.Series(list_text.to_numpy() == trade_text.to_numpy(), index=latest_slice.index)
                else:
                    listing_day = pd.Series(False, index=latest_slice.index)
                for field in latest_core_nulls:
                    if field in latest_slice.columns:
                        null_mask = latest_slice[field].isna()
                        structural_mask = (
                            pd.Series(null_mask.to_numpy() & listing_day.to_numpy(), index=latest_slice.index)
                            if field == "pre_close"
                            else pd.Series(False, index=latest_slice.index)
                        )
                        latest_core_nulls[field] += int((null_mask & ~structural_mask).sum())
                        if field in latest_structural_nulls:
                            latest_structural_nulls[field] += int(structural_mask.sum())
                    else:
                        latest_core_nulls[field] += int(len(latest_slice))

    issues: list[str] = []
    if duplicate_count:
        issues.append("duplicate_code_kline_time")
    if any(latest_core_nulls.values()):
        issues.append("latest_core_nulls")
    if price_issue_count:
        issues.append("price_sanity_issues")
    return {
        "status": "passed" if not issues else "failed",
        "path": str(PRODUCTION_RAW_HDF5),
        "scope": "full" if full_scan else "replace_window",
        "replace_from_date": replace_from_date,
        "scanned_rows": scanned_rows,
        "scanned_min_date": scanned_min_date,
        "scanned_max_date": scanned_max_date,
        "latest_trade_date": latest_trade_date,
        "latest_rows": latest_rows,
        "latest_stock_rows": latest_stock_rows,
        "duplicate_code_kline_time": duplicate_count,
        "latest_core_nulls": latest_core_nulls,
        "latest_structural_nulls": latest_structural_nulls,
        "price_sanity_issues": price_issue_count,
        "issues": issues,
    }


def _qlib_provider_price_audit() -> dict[str, Any]:
    stock_meta = _read_json(QLIB_STOCK_META)
    index_meta = _read_json(QLIB_INDEX_META)
    issues: list[str] = []
    index_readiness = _qlib_index_readiness(QLIB_INDEX_META.parent)

    stock_price_mode = stock_meta.get("price_mode")
    index_price_mode = index_meta.get("price_mode")
    stock_valid_field_count = int(stock_meta.get("valid_field_count") or 0)

    if stock_price_mode != "adjusted_ohlc_plus_factor_for_qlib_exchange":
        issues.append("qlib_stock_price_mode_not_adjusted_plus_factor")
    if stock_meta.get("raw_price_fields_retained") is not True:
        issues.append("qlib_stock_raw_audit_fields_not_retained")
    if stock_valid_field_count < 37:
        issues.append("qlib_stock_change_field_not_exported")
    if index_price_mode != "index_raw_close_identity_adjusted":
        issues.append("qlib_index_price_mode_missing_or_invalid")
    if index_meta.get("change_field") != "pct_chg_decimal":
        issues.append("qlib_index_change_field_missing_or_invalid")
    if index_meta.get("factor_field") != "constant_one_when_missing":
        issues.append("qlib_index_factor_field_missing_or_invalid")
    if index_readiness.get("status") != "passed":
        issues.append("qlib_index_artifacts_not_ready")
        issues.extend(str(item) for item in index_readiness.get("issues", []))

    return {
        "status": "passed" if not issues else "failed",
        "stock_meta_path": str(QLIB_STOCK_META),
        "index_meta_path": str(QLIB_INDEX_META),
        "stock_price_mode": stock_price_mode,
        "stock_raw_price_fields_retained": stock_meta.get("raw_price_fields_retained"),
        "stock_valid_field_count": stock_valid_field_count,
        "index_price_mode": index_price_mode,
        "index_change_field": index_meta.get("change_field"),
        "index_factor_field": index_meta.get("factor_field"),
        "index_readiness": index_readiness,
        "issues": issues,
    }


def _audit_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _yyyymmdd_compact(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace("-", "")[:8]
    return text if text.isdigit() and len(text) == 8 else None


def _code_to_qlib(code: str) -> str:
    if "." not in code:
        return code.lower()
    number, market = str(code).split(".", 1)
    return f"{number}{market.lower()}"


def _code_to_quantgpt_path(code: str) -> Path:
    if "." not in code:
        return QUANTGPT_DATA_DIR / f"{code.lower()}.parquet"
    number, market = str(code).split(".", 1)
    return QUANTGPT_DATA_DIR / f"{market.lower()}_{number}.parquet"


def _to_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _float_close(left: Any, right: Any, *, tolerance: float) -> bool:
    left_value = _to_float(left)
    right_value = _to_float(right)
    if left_value is None and right_value is None:
        return True
    if left_value is None or right_value is None:
        return False
    return abs(left_value - right_value) <= max(tolerance, abs(right_value) * 1e-6)


def _read_qlib_bin_value(code: str, field: str, target_iso: str) -> float | None:
    calendar = _read_qlib_calendar(QLIB_DATA_ROOT)
    if target_iso not in calendar:
        return None
    calendar_index = calendar.index(target_iso)
    path = QLIB_DATA_ROOT / "features" / _code_to_qlib(code) / f"{field}.day.bin"
    if not path.exists():
        return None
    try:
        values = np.fromfile(path, dtype="<f4")
    except Exception:
        return None
    if values.size <= 1:
        return None
    start = int(values[0])
    position = calendar_index - start
    if position < 0 or position >= values.size - 1:
        return None
    value = float(values[position + 1])
    return None if np.isnan(value) else value


def _sample_audit_codes(raw_latest: pd.DataFrame, sample_count: int, target_date: str) -> list[str]:
    codes = sorted(
        str(code)
        for code in raw_latest.get("code", pd.Series(dtype="string")).dropna().unique()
        if str(code) not in SAMPLE_EXCLUDED_INDEX_CODES
    )
    if sample_count <= 0 or not codes:
        return []
    selected: list[str] = []
    if {"pre_close", "LIST_DATE"} <= set(raw_latest.columns):
        structural = raw_latest[
            raw_latest["pre_close"].isna()
            & raw_latest["LIST_DATE"].astype("string").str.replace("-", "", regex=False).str.slice(0, 8).eq(target_date)
        ]
        for code in sorted(str(item) for item in structural.get("code", pd.Series(dtype="string")).dropna().unique()):
            if code in codes and code not in selected:
                selected.append(code)
            if len(selected) >= sample_count:
                return selected
    if len(codes) <= sample_count:
        for code in codes:
            if code not in selected:
                selected.append(code)
        return selected[:sample_count]
    positions = np.linspace(0, len(codes) - 1, num=sample_count, dtype=int).tolist()
    for position in positions:
        code = codes[int(position)]
        if code not in selected:
            selected.append(code)
    for code in codes:
        if len(selected) >= sample_count:
            break
        if code not in selected:
            selected.append(code)
    return selected[:sample_count]


def _read_raw_latest_slice(target_iso: str, *, tail_rows: int = 350000) -> pd.DataFrame:
    with pd.HDFStore(PRODUCTION_RAW_HDF5, "r") as store:
        nrows = int(store.get_storer("/daily").nrows or 0)
        frame = store.select("/daily", start=max(0, nrows - tail_rows), stop=nrows)
    return frame[pd.to_datetime(frame["kline_time"], errors="coerce").dt.strftime("%Y-%m-%d").eq(target_iso)].copy()


def _direct_tushare_latest_frames(target_date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    pro = get_tushare_client(network_mode="direct")
    daily = pro.daily(trade_date=target_date)
    limits = pro.stk_limit(trade_date=target_date)
    daily = pd.DataFrame() if daily is None else daily.rename(columns={"ts_code": "code", "vol": "volume"})
    limits = pd.DataFrame() if limits is None else limits.rename(columns={"ts_code": "code"})
    return daily, limits


def _deep_sample_quality_audit(*, latest_date: str | None, sample_count: int) -> dict[str, Any]:
    if sample_count <= 0:
        return {"status": "skipped", "sample_count": 0, "issues": [], "warnings": []}
    target_iso = latest_date or ""
    target_date = _yyyymmdd_compact(target_iso)
    if not target_date:
        return {"status": "failed", "sample_count": 0, "issues": ["deep_sample_target_date_missing"], "warnings": []}
    target_iso = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    raw_latest = _read_raw_latest_slice(target_iso)
    if raw_latest.empty:
        return {
            "status": "failed",
            "target_date": target_date,
            "target_iso": target_iso,
            "sample_count": 0,
            "issues": ["raw_latest_slice_empty"],
            "warnings": [],
        }
    daily, limits = _direct_tushare_latest_frames(target_date)
    if daily.empty:
        issues.append({"code": "tushare_direct_daily_empty"})
    if limits.empty:
        issues.append({"code": "tushare_direct_stk_limit_empty"})
    sample_codes = _sample_audit_codes(raw_latest, sample_count, target_date)
    raw_by_code = raw_latest.set_index("code", drop=False)
    daily_by_code = daily.set_index("code", drop=False) if not daily.empty and "code" in daily.columns else pd.DataFrame()
    limit_by_code = limits.set_index("code", drop=False) if not limits.empty and "code" in limits.columns else pd.DataFrame()
    samples: list[dict[str, Any]] = []
    direct_fields = [("open", "open"), ("high", "high"), ("low", "low"), ("close", "close"), ("pre_close", "pre_close"), ("volume", "volume"), ("amount", "amount")]
    limit_fields = [("up_limit", "up_limit"), ("down_limit", "down_limit")]
    for code in sample_codes:
        sample_issues: list[dict[str, Any]] = []
        raw = raw_by_code.loc[code]
        if isinstance(raw, pd.DataFrame):
            raw = raw.iloc[-1]
        direct = daily_by_code.loc[code] if not daily_by_code.empty and code in daily_by_code.index else None
        if isinstance(direct, pd.DataFrame):
            direct = direct.iloc[-1]
        limit = limit_by_code.loc[code] if not limit_by_code.empty and code in limit_by_code.index else None
        if isinstance(limit, pd.DataFrame):
            limit = limit.iloc[-1]
        if direct is None:
            sample_issues.append({"surface": "tushare_daily", "reason": "missing_direct_daily_row"})
        else:
            for raw_field, direct_field in direct_fields:
                if not _float_close(raw.get(raw_field), direct.get(direct_field), tolerance=1e-4):
                    list_text = _yyyymmdd_compact(raw.get("LIST_DATE") or raw.get("list_date"))
                    if raw_field == "pre_close" and list_text == target_date and _to_float(raw.get(raw_field)) is None:
                        continue
                    sample_issues.append(
                        {
                            "surface": "tushare_daily",
                            "field": raw_field,
                            "raw": _to_float(raw.get(raw_field)),
                            "direct": _to_float(direct.get(direct_field)),
                        }
                    )
        if limit is None:
            sample_issues.append({"surface": "tushare_stk_limit", "reason": "missing_direct_limit_row"})
        else:
            for raw_field, limit_field in limit_fields:
                if not _float_close(raw.get(raw_field), limit.get(limit_field), tolerance=1e-4):
                    sample_issues.append(
                        {
                            "surface": "tushare_stk_limit",
                            "field": raw_field,
                            "raw": _to_float(raw.get(raw_field)),
                            "direct": _to_float(limit.get(limit_field)),
                        }
                    )
        quantgpt_file = _code_to_quantgpt_path(code)
        quantgpt_row = None
        if quantgpt_file.exists():
            try:
                quantgpt = pd.read_parquet(quantgpt_file)
                date_column = "trade_date" if "trade_date" in quantgpt.columns else "date"
                quantgpt_rows = quantgpt[pd.to_datetime(quantgpt[date_column], errors="coerce").dt.strftime("%Y-%m-%d").eq(target_iso)]
                if not quantgpt_rows.empty:
                    quantgpt_row = quantgpt_rows.iloc[-1]
            except Exception as exc:
                sample_issues.append({"surface": "quantgpt", "reason": "read_failed", "error": str(exc)})
        if quantgpt_row is None:
            sample_issues.append({"surface": "quantgpt", "reason": "missing_sample_row", "path": str(quantgpt_file)})
        else:
            for raw_field, qg_field in [("adj_open", "open"), ("adj_high", "high"), ("adj_low", "low"), ("adj_close", "close"), ("adj_pre_close", "pre_close"), ("up_limit", "up_limit"), ("down_limit", "down_limit")]:
                if raw_field not in raw.index:
                    continue
                if not _float_close(raw.get(raw_field), quantgpt_row.get(qg_field), tolerance=1e-3):
                    list_text = _yyyymmdd_compact(raw.get("LIST_DATE") or raw.get("list_date"))
                    if raw_field == "adj_pre_close" and list_text == target_date and _to_float(raw.get(raw_field)) is None:
                        continue
                    sample_issues.append(
                        {
                            "surface": "quantgpt",
                            "field": qg_field,
                            "raw": _to_float(raw.get(raw_field)),
                            "quantgpt": _to_float(quantgpt_row.get(qg_field)),
                        }
                    )
        for raw_field, qlib_field in [("adj_open", "open"), ("adj_high", "high"), ("adj_low", "low"), ("adj_close", "close")]:
            if raw_field not in raw.index:
                continue
            qlib_value = _read_qlib_bin_value(code, qlib_field, target_iso)
            if not _float_close(raw.get(raw_field), qlib_value, tolerance=QLIB_FLOAT32_PRICE_TOLERANCE):
                sample_issues.append(
                    {
                        "surface": "qlib",
                        "field": qlib_field,
                        "raw": _to_float(raw.get(raw_field)),
                        "qlib": qlib_value,
                        "tolerance": QLIB_FLOAT32_PRICE_TOLERANCE,
                    }
                )
        if sample_issues:
            issues.append({"code": "sample_cross_surface_mismatch", "sample_code": code, "issues": sample_issues})
        samples.append({"code": code, "status": "passed" if not sample_issues else "failed", "issues": sample_issues})
    return {
        "status": "passed" if not issues else "failed",
        "target_date": target_date,
        "target_iso": target_iso,
        "requested_sample_count": int(sample_count),
        "sample_count": len(sample_codes),
        "sample_codes": sample_codes,
        "failed_samples": [sample["code"] for sample in samples if sample["status"] != "passed"],
        "raw_latest_rows": int(len(raw_latest)),
        "tushare_daily_rows": int(len(daily)),
        "tushare_stk_limit_rows": int(len(limits)),
        "qlib_float32_price_tolerance": QLIB_FLOAT32_PRICE_TOLERANCE,
        "samples": samples,
        "issues": issues,
        "warnings": warnings,
    }


def _write_audit_report(payload: dict[str, Any]) -> str:
    PRODUCTION_AUDIT_ROOT.mkdir(parents=True, exist_ok=True)
    path = PRODUCTION_AUDIT_ROOT / f"production_quality_audit_{_audit_stamp()}.json"
    atomic_write_json(path, payload)
    return str(path)


def _production_schema_alignment_audit() -> dict[str, Any]:
    required_columns = ["LIST_DATE", "list_date", "delist_date", "up_limit", "down_limit"]
    issues: list[str] = []
    if not PRODUCTION_RAW_HDF5.exists():
        return {
            "status": "failed",
            "path": str(PRODUCTION_RAW_HDF5),
            "columns": [],
            "required_columns": required_columns,
            "missing_columns": required_columns,
            "issues": ["production_hdf_missing"],
        }
    try:
        with pd.HDFStore(PRODUCTION_RAW_HDF5, mode="r") as store:
            if "/daily" not in store:
                return {
                    "status": "failed",
                    "path": str(PRODUCTION_RAW_HDF5),
                    "columns": [],
                    "required_columns": required_columns,
                    "missing_columns": required_columns,
                    "issues": ["production_hdf_daily_key_missing"],
                }
            frame = store.select("/daily", start=0, stop=1)
    except Exception as exc:
        return {
            "status": "failed",
            "path": str(PRODUCTION_RAW_HDF5),
            "columns": [],
            "required_columns": required_columns,
            "missing_columns": required_columns,
            "issues": ["schema_alignment_read_failed"],
            "error": str(exc),
        }
    columns = [str(column) for column in frame.columns]
    missing = [column for column in required_columns if column not in columns]
    if missing:
        issues.append("schema_alignment_missing_required_columns")
    return {
        "status": "passed" if not issues else "failed",
        "path": str(PRODUCTION_RAW_HDF5),
        "columns": columns,
        "column_count": len(columns),
        "required_columns": required_columns,
        "missing_columns": missing,
        "LIST_DATE_present": "LIST_DATE" in columns,
        "list_date_present": "list_date" in columns,
        "delist_date_present": "delist_date" in columns,
        "limit_columns_present": all(column in columns for column in ["up_limit", "down_limit"]),
        "issues": issues,
    }


def _audit_issue_code(issue: Any) -> str:
    if isinstance(issue, dict):
        return str(issue.get("code") or issue.get("reason") or issue)
    return str(issue)


def production_audit_summary(
    *,
    replace_from_date: str | None = None,
    full_scan: bool = False,
    deep_sample_count: int = 0,
    write_report: bool = False,
) -> dict[str, Any]:
    state_issues: list[str] = []

    def required_json(path: Path, issue_code: str) -> dict[str, Any]:
        try:
            return read_json(path, strict=True)
        except Exception as exc:
            state_issues.append(issue_code)
            return {"_read_error": str(exc)}

    current = required_json(CURRENT_PRODUCTION_DATASET_FILE, "current_production_dataset_missing_or_invalid")
    latest_status = required_json(LATEST_STATUS_FILE, "latest_status_missing_or_invalid")
    quality = required_json(PRODUCTION_QUALITY_FILE, "production_quality_report_missing_or_invalid")
    raw_quality = required_json(PRODUCTION_RAW_QUALITY_FILE, "production_raw_quality_report_missing_or_invalid")
    try:
        snapshot = _build_snapshot(deep=True)
    except Exception as exc:
        snapshot = {"snapshot_error": str(exc)}
        state_issues.append("fresh_production_snapshot_failed")
    latest_dates = {
        "hdf": snapshot.get("latest_hdf5_trade_date"),
        "qlib": snapshot.get("latest_qlib_trade_date"),
        "quantgpt": snapshot.get("latest_quantgpt_trade_date"),
    }
    nonempty_latest = {str(value) for value in latest_dates.values() if value}
    missing_latest = [name for name, value in latest_dates.items() if not value]
    audit_replace_from = replace_from_date
    if not audit_replace_from and not full_scan:
        audit_replace_from = str(current.get("latest_trade_date") or latest_dates.get("hdf") or "").replace("-", "")
    hdf_audit = _production_hdf_audit(replace_from_date=audit_replace_from, full_scan=full_scan)
    schema_alignment = _production_schema_alignment_audit()
    production_consistency = production_consistency_status(current=current, latest_status=latest_status, snapshot=snapshot)
    deep_sample_audit = _deep_sample_quality_audit(
        latest_date=str(current.get("latest_trade_date") or latest_dates.get("hdf") or ""),
        sample_count=int(deep_sample_count or 0),
    )
    issues: list[str] = list(state_issues)
    if not current or current.get("_read_error"):
        issues.append("current_production_dataset_missing")
    issues.extend(f"production_surface_missing:{name}" for name in missing_latest)
    if len(nonempty_latest) != 1 or missing_latest:
        issues.append("latest_dates_mismatch")
    if quality.get("passed") is not True:
        issues.append("production_quality_failed")
    if raw_quality.get("passed") is not True:
        issues.append("production_raw_quality_failed")
    if hdf_audit.get("status") != "passed":
        issues.extend([str(item) for item in hdf_audit.get("issues", [])])
    if schema_alignment.get("status") != "passed":
        issues.extend([str(item) for item in schema_alignment.get("issues", [])])
    if production_consistency.get("status") != "passed":
        issues.extend([str(item) for item in production_consistency.get("issues", [])])
    if production_consistency.get("partial_promote_detected"):
        issues.append("partial_promote_detected")
    if deep_sample_audit.get("status") == "failed":
        issues.append("deep_sample_quality_failed")
        issues.extend(_audit_issue_code(item) for item in deep_sample_audit.get("issues", []))
    qlib_audit = _qlib_provider_price_audit()
    if qlib_audit.get("status") != "passed":
        issues.extend([str(item) for item in qlib_audit.get("issues", [])])
    quantgpt_contract = quantgpt_contract_report(QUANTGPT_DATA_DIR, sample_limit=None)
    if not quantgpt_contract.get("ok"):
        issues.append("quantgpt_contract_invalid")
    expected_quantgpt_latest = hdf_audit.get("latest_stock_rows")
    actual_quantgpt_latest = snapshot.get("quantgpt_stocks_on_hdf5_latest_date")
    if expected_quantgpt_latest is None or actual_quantgpt_latest is None:
        issues.append("quantgpt_latest_coverage_missing")
    elif int(expected_quantgpt_latest) != int(actual_quantgpt_latest):
        issues.append("quantgpt_latest_coverage_mismatch")
    issues = sorted(set(issues))
    result = {
        "status": "passed" if not issues else "failed",
        "generated_at": _now(),
        "production_package_id": current.get("production_package_id"),
        "latest_trade_date": current.get("latest_trade_date") or latest_dates.get("hdf"),
        "replace_from_date": audit_replace_from,
        "full_scan": bool(full_scan),
        "latest_dates": latest_dates,
        "latest_dates_aligned": len(nonempty_latest) == 1 and not missing_latest,
        "quality_report": {
            "path": str(PRODUCTION_QUALITY_FILE),
            "passed": quality.get("passed") if quality else None,
        },
        "raw_quality_report": {
            "path": str(PRODUCTION_RAW_QUALITY_FILE),
            "passed": raw_quality.get("passed") if raw_quality else None,
        },
        "hdf_audit": hdf_audit,
        "qlib_provider_audit": qlib_audit,
        "quantgpt_contract_audit": quantgpt_contract,
        "quantgpt_latest_coverage": {
            "expected_from_hdf_latest_stock_rows": expected_quantgpt_latest,
            "actual_quantgpt_latest_rows": actual_quantgpt_latest,
        },
        "schema_alignment": schema_alignment,
        "production_consistency": production_consistency,
        "deep_sample_audit": deep_sample_audit,
        "deep_sample_count": int(deep_sample_count or 0),
        "audit_report_path": None,
        "warnings": deep_sample_audit.get("warnings", []),
        "issues": issues,
    }
    if write_report:
        report_path = _write_audit_report(result)
        result["audit_report_path"] = report_path
        atomic_write_json(Path(report_path), result)
    return result


def record_production_audit_result(result: dict[str, Any]) -> dict[str, Any]:
    """Persist the audit gate for the current production package."""
    current = _read_json(CURRENT_PRODUCTION_DATASET_FILE)
    latest_status = _read_json(LATEST_STATUS_FILE)
    passed = result.get("status") == "passed"
    summary = {
        "status": "passed" if passed else "failed",
        "generated_at": result.get("generated_at") or _now(),
        "production_package_id": result.get("production_package_id") or current.get("production_package_id"),
        "latest_trade_date": result.get("latest_trade_date") or current.get("latest_trade_date"),
        "replace_from_date": result.get("replace_from_date"),
        "audit_report_path": result.get("audit_report_path"),
        "issues": list(result.get("issues") or []),
    }

    if current:
        artifact_readiness = dict(current.get("artifact_readiness") or current.get("consumer_readiness") or {})
        artifact_readiness["qlib_model_benchmark_indices"] = _qlib_index_readiness(
            QLIB_DATA_ROOT,
            expected_latest=summary.get("latest_trade_date"),
        ).get("status") == "passed"
        current["artifact_readiness"] = artifact_readiness
        current["consumer_readiness"] = artifact_readiness if passed else {name: False for name in artifact_readiness}
        current["consumer_readiness_gate"] = "open" if passed else "blocked_by_production_audit"
        current["production_audit"] = summary

    snapshot = latest_status.get("snapshot")
    if isinstance(snapshot, dict):
        # This runs only after an explicit production audit, never from the
        # read-only GUI endpoint.  It persists exact QuantGPT latest-day
        # coverage so the dashboard need not trade correctness for polling
        # latency.
        refreshed_snapshot = _build_snapshot(deep=True)
        refreshed_snapshot.update(
            {
                key: value
                for key, value in snapshot.items()
                if key not in refreshed_snapshot and key not in {"status_snapshot_source"}
            }
        )
        snapshot = refreshed_snapshot
        latest_status["snapshot"] = snapshot
        artifact_readiness = dict(snapshot.get("artifact_readiness") or snapshot.get("consumer_readiness") or {})
        artifact_readiness["qlib_model_benchmark_indices"] = _qlib_index_readiness(
            QLIB_DATA_ROOT,
            expected_latest=summary.get("latest_trade_date"),
        ).get("status") == "passed"
        snapshot["artifact_readiness"] = artifact_readiness
        snapshot["consumer_readiness"] = artifact_readiness if passed else {name: False for name in artifact_readiness}
        snapshot["consumer_readiness_gate"] = "open" if passed else "blocked_by_production_audit"
    latest_status["status"] = "completed" if passed else "production_audit_failed"
    latest_status["consumer_readiness_gate"] = "open" if passed else "blocked_by_production_audit"
    latest_status["production_audit"] = summary
    state_snapshot = _snapshot_state_files([CURRENT_PRODUCTION_DATASET_FILE, LATEST_STATUS_FILE, DAILY_STATUS_FILE])
    try:
        if current:
            atomic_write_json(CURRENT_PRODUCTION_DATASET_FILE, current)
        atomic_write_json(LATEST_STATUS_FILE, latest_status)
        _write_daily_status(
            {
                "status": "audit_passed" if passed else "production_audit_failed",
                "package_id": summary.get("production_package_id"),
                "promotion_id": current.get("promotion_id") if current else None,
                "production_audit": summary,
            }
        )
    except Exception as exc:
        state_rollback = _restore_state_files(state_snapshot)
        if state_rollback.get("status") != "passed":
            raise RuntimeError(f"audit_state_write_and_rollback_failed:{exc}:{state_rollback.get('errors')}") from exc
        raise
    return summary



def _write_compat_metadata_for_daily(
    *,
    output_meta: Path,
    source_package_root: Path,
    source_manifest: dict[str, Any],
    current_dataset: dict[str, Any],
    replace_from_date: str,
) -> None:
    _write_compat_metadata(output_meta, source_package_root, source_manifest, 7)
    payload = _read_json(output_meta)
    payload.update(
        {
            "package_kind": "tushare_daily_update",
            "merged_from_production_package_id": current_dataset.get("production_package_id"),
            "replace_from_date": replace_from_date,
            "generated_at": _now(),
        }
    )
    notes = list(payload.get("notes") or [])
    notes.append("This metadata belongs to a staged daily merge package built from current production plus a direct Tushare rebuild window.")
    payload["notes"] = notes
    _write_json(output_meta, payload)


def _run_subprocess(cmd: list[str], *, name: str) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False)
    return {
        "name": name,
        "success": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
        "command": cmd,
    }


def _latest_hdf_trade_date_light(hdf_path: Path) -> str | None:
    if not hdf_path.exists():
        return None
    latest = None
    with pd.HDFStore(hdf_path, mode="r") as store:
        if "/daily" not in store:
            return None
        nrows = int(store.get_storer("/daily").nrows or 0)
        for start in range(0, nrows, 500000):
            chunk = store.select("/daily", start=start, stop=min(start + 500000, nrows), columns=["kline_time"])
            if chunk.empty:
                continue
            chunk_latest = pd.to_datetime(chunk["kline_time"], errors="coerce").max()
            if pd.isna(chunk_latest):
                continue
            latest = chunk_latest if latest is None else max(latest, chunk_latest)
    return str(pd.Timestamp(latest).date()) if latest is not None else None


def _qlib_calendar_latest(qlib_root: Path) -> str | None:
    cal = qlib_root / "calendars" / "day.txt"
    if not cal.exists():
        return None
    latest = None
    with cal.open("r", encoding="utf-8") as fh:
        for line in fh:
            value = line.strip()
            if value:
                latest = value
    return latest


def _recover_existing_qlib_step(qlib_root: Path, *, name: str, expected_latest: str | None) -> dict[str, Any] | None:
    if name == "qlib_index_convert":
        readiness = _qlib_index_readiness(qlib_root, expected_latest=expected_latest)
        if readiness.get("status") != "passed":
            return None
        return {
            "name": name,
            "success": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "command": ["reused_existing_qlib_index_output"],
            "reused_existing_output": True,
            "calendar_latest_date": readiness.get("calendar_latest_date"),
            "required_codes": readiness.get("required_codes"),
        }

    latest = _qlib_calendar_latest(qlib_root)
    instruments = qlib_root / "instruments" / "all.txt"
    features = qlib_root / "features"
    meta = qlib_root / "stock_converter_meta.json"
    if not latest or (expected_latest and latest != expected_latest):
        return None
    if not instruments.exists() or not features.exists() or not meta.exists():
        return None
    try:
        feature_dir_count = sum(1 for path in features.iterdir() if path.is_dir())
    except OSError:
        return None
    if feature_dir_count < 5000:
        return None
    return {
        "name": name,
        "success": True,
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "command": ["reused_existing_qlib_output"],
        "reused_existing_output": True,
        "calendar_latest_date": latest,
        "feature_dir_count": feature_dir_count,
    }


def _recover_existing_quality_report(
    path: Path,
    *,
    expected_latest: str | None,
    replace_from_date: str | None,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    report = _read_json(path)
    if report.get("passed") is not True:
        return None
    latest = report.get("latest_trade_date")
    if expected_latest and latest != expected_latest:
        return None
    report_replace = str(report.get("replace_from_date") or "").replace("-", "")
    expected_replace = str(replace_from_date or "").replace("-", "")
    if expected_replace and report_replace and report_replace != expected_replace:
        return None
    report["reused_existing_output"] = True
    return report


_PRODUCTION_QUALITY_SUMMARY_FIELDS = frozenset(
    {
        "field_groups",
        "latest_code_activity",
        "metadata_quality",
        "limit_price_quality",
        "factor_adjusted_quality",
        "schema_summary",
    }
)


def _is_complete_production_quality_report(report: dict[str, Any] | None) -> bool:
    """Return whether a report is safe to publish to the production dashboard."""
    return bool(
        isinstance(report, dict)
        and report.get("passed") is True
        and _PRODUCTION_QUALITY_SUMMARY_FIELDS.issubset(report)
    )


def _snapshot_for_daily_compat(
    compat_root: Path,
    *,
    latest_hdf5: str | None,
    latest_quantgpt: str | None,
) -> dict[str, Any]:
    qlib_root = compat_root / "qlib"
    quantgpt_root = compat_root / "quantgpt"
    latest_qlib = _qlib_calendar_latest(qlib_root)
    benchmarks = sorted((quantgpt_root / "benchmark").glob("*.parquet"))
    qlib_index_readiness = _qlib_index_readiness(qlib_root, expected_latest=latest_qlib)
    quantgpt_contract = quantgpt_contract_report(quantgpt_root / "stocks", sample_limit=None)
    return {
        "latest_hdf5_trade_date": latest_hdf5,
        "latest_qlib_trade_date": latest_qlib,
        "latest_quantgpt_trade_date": latest_quantgpt,
        # This is a cheap, persisted cardinality from the conversion contract.
        # The exact latest-day coverage ratio is refreshed after promotion by the
        # explicit production audit, never by GUI polling.
        "quantgpt_stock_parquet_count": quantgpt_contract.get("stock_file_count"),
        "quantgpt_benchmark_file_count": len(benchmarks),
        "quantgpt_benchmark_files": [p.name for p in benchmarks],
        "quantgpt_contract": quantgpt_contract,
        "consumer_readiness": {
            "quantgpt_factor_mining": (quantgpt_root / "stocks").exists() and len(benchmarks) >= 3,
            "qlib_model_training": (qlib_root / "calendars" / "day.txt").exists(),
            "qlib_paper_trading": (qlib_root / "calendars" / "day.txt").exists(),
            "qlib_model_benchmark_indices": qlib_index_readiness.get("status") == "passed",
        },
        "qlib_index_readiness": qlib_index_readiness,
    }


_QLIB_STOCK_CONVERTER_MODULE = None
_QLIB_INDEX_CONVERTER_MODULE = None


def _load_module_from_path(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot_load_module:{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _qlib_stock_converter_module():
    global _QLIB_STOCK_CONVERTER_MODULE
    if _QLIB_STOCK_CONVERTER_MODULE is None:
        _QLIB_STOCK_CONVERTER_MODULE = _load_module_from_path(
            PROJECT_ROOT / "scripts" / "data_foundation" / "tushare_raw_to_qlib.py",
            "_fxalpha_tushare_raw_to_qlib",
        )
    return _QLIB_STOCK_CONVERTER_MODULE


def _qlib_index_converter_module():
    global _QLIB_INDEX_CONVERTER_MODULE
    if _QLIB_INDEX_CONVERTER_MODULE is None:
        _QLIB_INDEX_CONVERTER_MODULE = _load_module_from_path(
            PROJECT_ROOT / "scripts" / "data_foundation" / "tushare_index_to_qlib.py",
            "_fxalpha_tushare_index_to_qlib",
        )
    return _QLIB_INDEX_CONVERTER_MODULE


def _yyyymmdd_to_iso(value: str) -> str:
    text = str(value).strip().replace("-", "")
    return datetime.strptime(text, "%Y%m%d").date().isoformat()


def _read_qlib_calendar(qlib_root: Path) -> list[str]:
    path = qlib_root / "calendars" / "day.txt"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_qlib_calendar(qlib_root: Path, calendar: list[str]) -> None:
    path = qlib_root / "calendars" / "day.txt"
    atomic_write_text(path, "\n".join(calendar) + ("\n" if calendar else ""))


def _previous_calendar_date(calendar: list[str], date_iso: str) -> str | None:
    previous = [item for item in calendar if item < date_iso]
    return previous[-1] if previous else None


def _read_raw_window_frame(source_hdf: Path, *, from_date_iso: str, chunk_rows: int = 250000) -> pd.DataFrame:
    from_ts = pd.Timestamp(from_date_iso)
    columns = _daily_hdf_columns(source_hdf)
    frames: list[pd.DataFrame] = []
    with pd.HDFStore(source_hdf, mode="r") as store:
        if "/daily" not in store:
            raise KeyError(f"/daily missing from {source_hdf}")
        nrows = int(store.get_storer("/daily").nrows or 0)
        for start in range(0, nrows, chunk_rows):
            chunk = store.select("/daily", start=start, stop=min(start + chunk_rows, nrows), columns=columns)
            dates = pd.to_datetime(chunk["kline_time"], errors="coerce")
            window = chunk.loc[dates.ge(from_ts)].copy()
            if not window.empty:
                frames.append(window)
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=False).sort_values(["kline_time", "code"], kind="mergesort")


def _read_qlib_instruments(path: Path) -> dict[str, tuple[str, str]]:
    if not path.exists():
        return {}
    out: dict[str, tuple[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        inst, start_date, end_date = line.split("\t")
        out[inst] = (start_date, end_date)
    return out


def _write_qlib_instruments(path: Path, instruments: dict[str, tuple[str, str]]) -> None:
    lines = [f"{inst}\t{dates[0]}\t{dates[1]}" for inst, dates in sorted(instruments.items())]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def _write_qlib_bin_file(file_path: Path, data: np.ndarray, start_index: int) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = np.hstack([[float(start_index)], data.astype(np.float32)]).astype("<f4")
    tmp_path = file_path.with_name(f".{file_path.name}.tmp-{os.getpid()}")
    tmp_path.unlink(missing_ok=True)
    try:
        with open(tmp_path, "wb") as fh:
            payload.tofile(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, file_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _patch_qlib_bin_file(
    file_path: Path,
    calendar_index: dict[str, int],
    values: pd.Series,
    *,
    clear_from_iso: str | None = None,
    clear_to_iso: str | None = None,
) -> bool:
    clear_positions = [
        position
        for date_key, position in calendar_index.items()
        if (clear_from_iso is None or date_key >= clear_from_iso) and (clear_to_iso is None or date_key <= clear_to_iso)
    ]
    update_positions: list[int] = []
    update_values: list[float] = []
    for date_value, value in values.items():
        date_key = str(pd.Timestamp(date_value).date())
        if date_key not in calendar_index:
            continue
        update_positions.append(calendar_index[date_key])
        update_values.append(float(value) if pd.notna(value) else float("nan"))
    all_positions = sorted(set(clear_positions) | set(update_positions))
    if not all_positions:
        return False

    if file_path.exists() and file_path.stat().st_size >= 8:
        payload = np.fromfile(file_path, dtype="<f4")
        existing_start = int(payload[0])
        existing_values = payload[1:].astype(np.float32)
    else:
        if not any(pd.notna(value) for value in update_values):
            return False
        existing_start = min(all_positions)
        existing_values = np.array([], dtype=np.float32)

    new_start = min(existing_start, min(all_positions))
    existing_end = existing_start + len(existing_values) - 1 if len(existing_values) else new_start - 1
    new_end = max(existing_end, max(all_positions))
    merged = np.full(new_end - new_start + 1, np.nan, dtype=np.float32)
    if len(existing_values):
        offset = existing_start - new_start
        merged[offset: offset + len(existing_values)] = existing_values
    for pos in clear_positions:
        merged[pos - new_start] = np.nan
    for pos, value in zip(update_positions, update_values):
        merged[pos - new_start] = np.float32(value)
    if existing_start == new_start and len(existing_values) == len(merged) and np.array_equal(existing_values, merged, equal_nan=True):
        return False
    _write_qlib_bin_file(file_path, merged, new_start)
    return True


def _patch_stock_qlib_bins(
    *,
    qlib_root: Path,
    qlib_frame: pd.DataFrame,
    calendar: list[str],
    write_from_iso: str,
) -> dict[str, Any]:
    converter = _qlib_stock_converter_module()
    field_map = converter.FIELD_MAP
    normalize_instrument = converter.normalize_instrument
    calendar_index = {date: idx for idx, date in enumerate(calendar)}
    instruments_path = qlib_root / "instruments" / "all.txt"
    instrument_book = _read_qlib_instruments(instruments_path)
    valid_h5_cols = [col for col in qlib_frame.columns if col in field_map]
    affected = 0
    rewritten = 0
    delta_instruments = set(qlib_frame.index.get_level_values("instrument").unique().tolist()) if not qlib_frame.empty else set()
    all_instruments = sorted(set(instrument_book) | {normalize_instrument(str(inst)) for inst in delta_instruments})
    if all_instruments:
        for inst_normalized in all_instruments:
            matching = [inst for inst in delta_instruments if normalize_instrument(str(inst)) == inst_normalized]
            stock_data = qlib_frame.xs(matching[0], level="instrument").sort_index() if matching else pd.DataFrame(columns=valid_h5_cols)
            stock_data = stock_data[stock_data.index.map(lambda item: str(pd.Timestamp(item).date()) >= write_from_iso)]
            stock_data = stock_data[stock_data.index.map(lambda item: str(pd.Timestamp(item).date()) in calendar_index)]
            affected += 1
            inst_dir = qlib_root / "features" / inst_normalized
            patched_any = False
            for h5_col in valid_h5_cols:
                series = stock_data[h5_col].copy() if h5_col in stock_data.columns else pd.Series(dtype="float64")
                if _patch_qlib_bin_file(
                    inst_dir / f"{field_map[h5_col]}.day.bin",
                    calendar_index,
                    series,
                    clear_from_iso=write_from_iso,
                    clear_to_iso=calendar[-1] if calendar else None,
                ):
                    patched_any = True
            if patched_any:
                rewritten += 1
            if not stock_data.empty:
                dates = [str(pd.Timestamp(item).date()) for item in stock_data.index]
                old = instrument_book.get(inst_normalized)
                start_date = min([old[0]] + dates) if old else min(dates)
                end_date = max([old[1]] + dates) if old else max(dates)
                instrument_book[inst_normalized] = (start_date, end_date)
    _write_qlib_instruments(instruments_path, instrument_book)
    return {
        "affected_instrument_count": affected,
        "rewritten_instrument_count": rewritten,
        "valid_field_count": len(valid_h5_cols),
        "valid_fields": [field_map[col] for col in valid_h5_cols],
    }


def _patch_index_qlib_bins(
    *,
    qlib_root: Path,
    raw_window: pd.DataFrame,
    calendar: list[str],
    write_from_iso: str,
) -> dict[str, Any]:
    converter = _qlib_index_converter_module()
    calendar_index = {date: idx for idx, date in enumerate(calendar)}
    work = raw_window.copy()
    if work.empty:
        return {"affected_index_count": 0, "updated": [], "valid_field_count": len(converter.FIELD_MAP)}
    work["trade_date"] = pd.to_datetime(work["kline_time"], errors="coerce")
    work = work[work["trade_date"].notna()].set_index("trade_date").sort_index()
    updated: list[dict[str, Any]] = []
    for source_code, qlib_code in converter.INDEX_CODE_MAP.items():
        idx = work[work["code"].astype(str).eq(source_code)].copy()
        idx = idx[idx.index.map(lambda item: str(pd.Timestamp(item).date()) >= write_from_iso)]
        idx = idx[idx.index.map(lambda item: str(pd.Timestamp(item).date()) in calendar_index)]
        if "change" not in idx.columns and "pct_chg" in idx.columns:
            idx["change"] = pd.to_numeric(idx["pct_chg"], errors="coerce") / 100.0
        if "factor" not in idx.columns:
            idx["factor"] = 1.0
        patched_fields = 0
        out_dir = qlib_root / "features" / qlib_code
        for src_field, out_field in converter.FIELD_MAP.items():
            values = idx[src_field] if src_field in idx.columns else pd.Series(dtype="float64")
            if _patch_qlib_bin_file(
                out_dir / f"{out_field}.day.bin",
                calendar_index,
                values,
                clear_from_iso=write_from_iso,
                clear_to_iso=calendar[-1] if calendar else None,
            ):
                patched_fields += 1
        if patched_fields:
            updated.append(
                {
                    "source_code": source_code,
                    "qlib_code": qlib_code,
                    "latest_date": str(pd.Timestamp(idx.index.max()).date()) if not idx.empty else None,
                    "count": int(len(idx)),
                    "patched_field_count": patched_fields,
                }
            )
    return {
        "affected_index_count": len(updated),
        "updated": updated,
        "valid_field_count": len(converter.FIELD_MAP),
    }


def _build_daily_qlib_outputs(
    *,
    merged_hdf: Path,
    qlib_root: Path,
    replace_from_date: str,
    expected_latest: str | None,
) -> dict[str, Any]:
    seed_calendar = _read_qlib_calendar(QLIB_DATA_ROOT)
    if not seed_calendar:
        raise RuntimeError("production_qlib_seed_calendar_missing")
    replace_from_iso = _yyyymmdd_to_iso(replace_from_date)
    context_from_iso = _previous_calendar_date(seed_calendar, replace_from_iso) or replace_from_iso

    if qlib_root.exists():
        shutil.rmtree(qlib_root)
    shutil.copytree(QLIB_DATA_ROOT, qlib_root)

    raw_window = _read_raw_window_frame(merged_hdf, from_date_iso=context_from_iso)
    if raw_window.empty:
        raise RuntimeError(f"qlib_daily_window_empty:{context_from_iso}")
    window_dates = [
        str(pd.Timestamp(item).date())
        for item in pd.to_datetime(raw_window["kline_time"], errors="coerce").dropna().sort_values().unique()
    ]
    calendar = sorted(set(seed_calendar) | set(window_dates))
    _write_qlib_calendar(qlib_root, calendar)

    stock_window = raw_chunk_to_qlib_frame(raw_window)
    converter = _qlib_stock_converter_module()
    qlib_frame = converter._prepare_qlib_price_semantics(stock_window) if not stock_window.empty else stock_window
    stock_patch = _patch_stock_qlib_bins(
        qlib_root=qlib_root,
        qlib_frame=qlib_frame,
        calendar=calendar,
        write_from_iso=replace_from_iso,
    )
    index_patch = _patch_index_qlib_bins(
        qlib_root=qlib_root,
        raw_window=raw_window,
        calendar=calendar,
        write_from_iso=replace_from_iso,
    )

    latest_after = calendar[-1] if calendar else None
    stock_meta = {
        "effective_mode": "daily_window_patch",
        "mode": "daily_window_patch",
        "seed_qlib_root": str(QLIB_DATA_ROOT),
        "replace_from_date": replace_from_iso,
        "context_from_date": context_from_iso,
        "calendar_latest_date": latest_after,
        "source_latest_date": expected_latest,
        "price_mode": "adjusted_ohlc_plus_factor_for_qlib_exchange",
        "vwap_mode": "adjusted_vwap_from_raw_amount_volume_times_factor",
        "chip_cost_mode": "adjusted_chip_cost_from_cyq_perf_cost_lines_times_factor",
        "sealed_limit_mode": "one_price_limit_and_relative_turnover_ratio",
        "sealed_limit_contract_version": getattr(converter, "SEALED_LIMIT_CONTRACT_VERSION", None),
        "sealed_turnover_ratio_threshold": getattr(converter, "SEALED_TURNOVER_RATIO_THRESHOLD", None),
        "raw_price_fields_retained": True,
        "generated_at": _now(),
        "meta_path": str(QLIB_STOCK_META),
        "output_dir": str(QLIB_DATA_ROOT),
        **stock_patch,
    }
    _write_json(qlib_root / "stock_converter_meta.json", stock_meta)

    index_converter = _qlib_index_converter_module()
    index_meta = {
        "kind": "qlib_index_converter",
        "effective_mode": "daily_window_patch",
        "mode": "daily_window_patch",
        "replace_from_date": replace_from_iso,
        "context_from_date": context_from_iso,
        "source_latest_date": expected_latest,
        "calendar_latest_date": latest_after,
        "price_mode": "index_raw_close_identity_adjusted",
        "change_field": "pct_chg_decimal",
        "factor_field": "constant_one_when_missing",
        "generated_at": _now(),
        "meta_path": str(QLIB_INDEX_META),
        "output_dir": str(QLIB_DATA_ROOT),
        "required_codes": sorted(index_converter.INDEX_CODE_MAP.values()),
        **index_patch,
    }
    _write_json(qlib_root / "index_converter_meta.json", index_meta)

    return {
        "stock_step": {
            "name": "qlib_convert",
            "success": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "command": ["daily_window_patch_from_seed"],
            "calendar_latest_date": latest_after,
            **stock_patch,
        },
        "index_step": {
            "name": "qlib_index_convert",
            "success": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "command": ["daily_window_patch_from_seed"],
            "calendar_latest_date": latest_after,
            **index_patch,
        },
    }


def _build_compat_outputs(
    *,
    root: Path,
    merged_hdf: Path,
    delta_hdf: Path | None = None,
    replace_from_date: str | None = None,
) -> dict[str, Any]:
    compat_root = root / COMPAT_ROOT_NAME
    qlib_root = compat_root / "qlib"
    quantgpt_root = compat_root / "quantgpt"
    qlib_root.mkdir(parents=True, exist_ok=True)
    quantgpt_root.mkdir(parents=True, exist_ok=True)

    expected_latest = _latest_hdf_trade_date_light(merged_hdf)
    qlib_step = _recover_existing_qlib_step(qlib_root, name="qlib_convert", expected_latest=expected_latest)
    qlib_index_step = _recover_existing_qlib_step(qlib_root, name="qlib_index_convert", expected_latest=expected_latest)
    if (qlib_step is None or qlib_index_step is None) and delta_hdf is not None and replace_from_date:
        qlib_daily = _build_daily_qlib_outputs(
            merged_hdf=merged_hdf,
            qlib_root=qlib_root,
            replace_from_date=replace_from_date,
            expected_latest=expected_latest,
        )
        qlib_step = qlib_daily["stock_step"]
        qlib_index_step = qlib_daily["index_step"]
    if qlib_step is None:
        qlib_step = _run_subprocess(
            [
                sys.executable,
                str(QLIB_CONVERT_SCRIPT),
                "--mode",
                "full",
                "--source-h5",
                str(merged_hdf),
                "--output-dir",
                str(qlib_root),
                "--json",
            ],
            name="qlib_convert",
        )
    if not qlib_step["success"]:
        raise RuntimeError(f"tushare_daily_qlib_convert_failed:{qlib_step['stderr'] or qlib_step['stdout']}")

    if qlib_index_step is None:
        qlib_index_step = _run_subprocess(
            [
                sys.executable,
                str(QLIB_INDEX_CONVERT_SCRIPT),
                "--mode",
                "full",
                "--source-h5",
                str(merged_hdf),
                "--qlib-dir",
                str(qlib_root),
                "--json",
            ],
            name="qlib_index_convert",
        )
    if not qlib_index_step["success"]:
        raise RuntimeError(f"tushare_daily_qlib_index_convert_failed:{qlib_index_step['stderr'] or qlib_index_step['stdout']}")
    qlib_index_readiness = _qlib_index_readiness(qlib_root, expected_latest=expected_latest)
    if qlib_index_readiness.get("status") != "passed":
        raise RuntimeError(f"tushare_daily_qlib_index_artifacts_not_ready:{qlib_index_readiness.get('issues')}")

    quantgpt_stocks_dir = quantgpt_root / "stocks"
    quantgpt_benchmark_dir = quantgpt_root / "benchmark"
    quantgpt_delta_extract = None
    if delta_hdf is not None and replace_from_date:
        quantgpt_delta_hdf = compat_root / "raw" / "quantgpt_delta_window.h5"
        quantgpt_delta_extract = _extract_window_hdf(
            source_hdf=merged_hdf,
            output_hdf=quantgpt_delta_hdf,
            replace_from_date=replace_from_date,
        )
        quantgpt_result = convert_quantgpt_incremental_from_delta(
            quantgpt_delta_hdf,
            quantgpt_stocks_dir,
            quantgpt_benchmark_dir,
            replace_from_date=replace_from_date,
            seed_output_dir=QUANTGPT_DATA_DIR,
            seed_benchmark_dir=QUANTGPT_BENCHMARK_DIR,
        )
    else:
        quantgpt_result = convert_quantgpt(
            merged_hdf,
            quantgpt_stocks_dir,
            benchmark_dir=quantgpt_benchmark_dir,
        )
    if quantgpt_result.get("status") == "failed":
        raise RuntimeError(f"tushare_daily_quantgpt_convert_failed:{quantgpt_result.get('error')}")
    calendar_result = _write_trading_calendar(
        merged_hdf,
        compat_root / "raw" / "trade_calendar.txt",
        compat_root / "raw" / "trade_calendar_meta.json",
    )

    return {
        "qlib": qlib_step,
        "qlib_index": qlib_index_step,
        "qlib_index_readiness": qlib_index_readiness,
        "quantgpt_delta_extract": quantgpt_delta_extract,
        "quantgpt": quantgpt_result,
        "trading_calendar": calendar_result,
        "snapshot": _snapshot_for_daily_compat(
            compat_root,
            latest_hdf5=expected_latest,
            latest_quantgpt=quantgpt_result.get("latest_date"),
        ),
    }


@data_job_guard("daily_stage_update")
def data_stage_update(
    target_date: str | None = None,
    *,
    dry_run: bool = False,
    _validated_preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # The governed daily routine has already run this expensive gate. Reusing
    # that immutable result keeps the target date fixed and avoids counting a
    # second preflight's own memory allocation against the resource gate.
    preflight = dict(_validated_preflight) if _validated_preflight is not None else data_daily_preflight(
        target_date, for_promotion=False
    )
    if preflight["status"] != "go":
        return {"status": "blocked", "stage": "preflight", "preflight": preflight}
    if preflight.get("already_current"):
        return {
            "status": "already_current",
            "target_date": preflight.get("selected_target_date"),
            "preflight": preflight,
        }

    reusable = _find_reusable_daily_package(preflight)
    reused_existing_package = reusable is not None
    existing_manifest = reusable[1] if reusable else None
    daily_package_id = (
        str(existing_manifest.get("package_id"))
        if existing_manifest and existing_manifest.get("package_id")
        else _package_id(str(preflight["selected_target_date"]))
    )
    source_package_id = (
        str(existing_manifest.get("source_package_id"))
        if existing_manifest and existing_manifest.get("source_package_id")
        else f"{daily_package_id}-source"
    )
    root = reusable[0] if reusable else STAGING_ROOT / daily_package_id
    compat_root = root / COMPAT_ROOT_NAME
    if dry_run:
        return {
            "status": "dry_run",
            "package_id": daily_package_id,
            "source_package_id": source_package_id,
            "package_root": str(root),
            "compat_root": str(compat_root),
            "reused_existing_package": reused_existing_package,
            "existing_status": existing_manifest.get("status") if existing_manifest else None,
            "existing_interrupted_reason": existing_manifest.get("interrupted_reason") if existing_manifest else None,
            "source_progress_summary": existing_manifest.get("source_progress_summary") if existing_manifest else None,
            "preflight": preflight,
        }

    if existing_manifest and existing_manifest.get("status") == "completed":
        manifest = _enrich_daily_manifest(dict(existing_manifest))
        manifest["reused_existing_package"] = True
        return manifest

    current_dataset = _require_tushare_production()
    manifest = dict(existing_manifest or {})
    manifest.update(
        {
            "package_id": daily_package_id,
            "status": "stage_in_progress",
            "source": "tushare",
            "package_kind": "daily_update",
            "target_date": preflight.get("target_date"),
            "effective_target_date": preflight.get("effective_target_date"),
            "selected_target_date": preflight.get("selected_target_date"),
            "replace_from_date": preflight.get("replace_from_date"),
            "source_package_id": source_package_id,
            "package_root": str(root),
            "preflight": preflight,
            "reused_existing_package": reused_existing_package,
        }
    )
    if existing_manifest:
        manifest["resumed_at"] = _now()
        if existing_manifest.get("status") == "interrupted_resumable":
            manifest["resume_reason"] = "interrupted_resumable"
        manifest.pop("error", None)
        manifest.pop("failed_at", None)
        manifest.pop("interrupted_at", None)
        manifest.pop("interrupted_reason", None)
    else:
        manifest["created_at"] = _now()
    root.mkdir(parents=True, exist_ok=True)
    manifest["current_stage"] = "source_rebuild"
    manifest = _write_daily_manifest(root, manifest)

    try:
        manifest["current_stage"] = "source_rebuild"
        manifest = _write_daily_manifest(root, manifest)
        existing_source_result = manifest.get("source_rebuild") if isinstance(manifest.get("source_rebuild"), dict) else None
        source_package_root = STAGING_ROOT / source_package_id
        source_manifest = _read_manifest(source_package_root / "manifest.json")
        if existing_source_result and existing_source_result.get("status") == "completed":
            source_result = existing_source_result
        elif source_manifest.get("status") == "completed":
            source_result = source_manifest
        else:
            source_result = tushare_full_rebuild(
                TushareRebuildConfig(
                    start_date=str(preflight["current_latest_trade_date"]),
                    cutoff_date=str(preflight["selected_target_date"]),
                    pad_trading_days=120,
                    package_id=source_package_id,
                    resume=True,
                    dry_run=False,
                    proxy_mode="direct",
                    trade_date_chunk_size=40,
                )
            )
        if source_result.get("status") != "completed":
            raise RuntimeError(f"source_rebuild_failed:{source_result.get('status')}")
        manifest["source_rebuild"] = source_result
        manifest["current_stage"] = "source_prepare_production"
        manifest = _write_daily_manifest(root, manifest)

        manifest["current_stage"] = "source_prepare_production"
        manifest = _write_daily_manifest(root, manifest)
        existing_prepare = manifest.get("source_prepare_production") if isinstance(manifest.get("source_prepare_production"), dict) else None
        if existing_prepare and existing_prepare.get("status") == "completed":
            compat_prepare = existing_prepare
        else:
            compat_prepare = prepare_tushare_production_artifacts(package_id=source_package_id, force=True, dry_run=False)
        if compat_prepare.get("status") != "completed":
            raise RuntimeError(f"source_prepare_production_failed:{compat_prepare.get('status')}")
        manifest["source_prepare_production"] = compat_prepare
        manifest["current_stage"] = "merge_production_hdf"
        manifest = _write_daily_manifest(root, manifest)

        compat_root.mkdir(parents=True, exist_ok=True)
        raw_root = compat_root / "raw"
        raw_root.mkdir(parents=True, exist_ok=True)
        merged_hdf = raw_root / "stock_daily.h5"
        delta_hdf = Path(compat_prepare["compat_root"]) / "raw" / "stock_daily.h5"
        merge_result = manifest.get("merge_result")
        if merge_result and not Path(str(merge_result.get("final_hdf", ""))).exists():
            merge_result = None
        if not merge_result:
            merge_result = _recover_existing_merge_result(
                output_hdf=merged_hdf,
                delta_hdf=delta_hdf,
                selected_target_date=str(preflight["selected_target_date"]),
            )
        if not merge_result:
            merge_result = _merge_compat_hdf(
                production_hdf=PRODUCTION_RAW_HDF5,
                delta_hdf=delta_hdf,
                output_hdf=merged_hdf,
                replace_from_date=str(preflight["replace_from_date"]),
            )
        merge_result["derived_price_recompute"] = {
            "status": "skipped",
            "reason": "daily_merge_repairs_delta_boundary_fields_during_streaming_merge",
        }
        manifest["merge_result"] = merge_result
        manifest["current_stage"] = "merged_quality_check"
        manifest = _write_daily_manifest(root, manifest)

        source_package_root = STAGING_ROOT / source_package_id
        _write_compat_metadata_for_daily(
            output_meta=raw_root / "metadata.json",
            source_package_root=source_package_root,
            source_manifest=_read_manifest(source_package_root / "manifest.json"),
            current_dataset=current_dataset,
            replace_from_date=str(preflight["replace_from_date"]),
        )
        quality_copy, raw_quality_copy = _copy_quality_reports(source_package_root, compat_root)
        manifest["source_quality_report"] = quality_copy
        manifest["source_raw_quality_report"] = raw_quality_copy

        compatibility_quality = _recover_existing_quality_report(
            compat_root / "daily_compat_quality_report.json",
            expected_latest=str(preflight["selected_target_date"])[:4]
            + "-"
            + str(preflight["selected_target_date"])[4:6]
            + "-"
            + str(preflight["selected_target_date"])[6:8],
            replace_from_date=str(preflight["replace_from_date"]),
        )
        if compatibility_quality is None:
            compatibility_quality = run_quality_check(
                merged_hdf,
                profile="daily_compat",
                replace_from_date=str(preflight["replace_from_date"]),
            )
            _write_json(compat_root / "daily_compat_quality_report.json", compatibility_quality)
        if not compatibility_quality.get("passed"):
            raise RuntimeError("merged_compat_quality_failed")

        # Keep the fast window gate distinct from the canonical production
        # report.  The latter is what the GUI consumes and therefore must
        # contain the full quality/metadata summary, not only daily-compat
        # fields.
        production_quality = _recover_existing_quality_report(
            compat_root / "quality_report.json",
            expected_latest=str(preflight["selected_target_date"])[:4]
            + "-"
            + str(preflight["selected_target_date"])[4:6]
            + "-"
            + str(preflight["selected_target_date"])[6:8],
            replace_from_date=None,
        )
        if not _is_complete_production_quality_report(production_quality):
            production_quality = run_quality_check(merged_hdf, profile="deep_full")
            _write_json(compat_root / "quality_report.json", production_quality)
        if not _is_complete_production_quality_report(production_quality):
            raise RuntimeError("merged_production_quality_failed")
        manifest["current_stage"] = "build_compat_outputs"
        manifest["quality_report"] = str(compat_root / "quality_report.json")
        manifest["daily_compat_quality_report"] = str(compat_root / "daily_compat_quality_report.json")
        manifest["raw_quality_report"] = str(compat_root / "raw_quality_report.json")
        manifest = _write_daily_manifest(root, manifest)

        outputs = _build_compat_outputs(
            root=root,
            merged_hdf=merged_hdf,
            delta_hdf=delta_hdf,
            replace_from_date=str(preflight["replace_from_date"]),
        )
        compat_manifest = {
            "status": "completed",
            "package_id": daily_package_id,
            "package_root": str(root),
            "compat_root": str(compat_root),
            "created_at": _now(),
            "source": "tushare",
            "schema_version": current_dataset.get("schema_version", "tushare_v1"),
            "compatibility_mode": "tushare_raw_hdf_compat",
            "snapshot": outputs["snapshot"],
            "quantgpt": outputs["quantgpt"],
            "merge_result": merge_result,
            "source_package_id": source_package_id,
        }
        _write_json(_compat_manifest_path(root), compat_manifest)

        manifest.update(
            {
                "status": "completed",
                "completed_at": _now(),
                "snapshot": outputs["snapshot"],
                "compat_manifest": str(_compat_manifest_path(root)),
                "quality_report": str(compat_root / "quality_report.json"),
                "daily_compat_quality_report": str(compat_root / "daily_compat_quality_report.json"),
                "raw_quality_report": str(compat_root / "raw_quality_report.json"),
            }
        )
        manifest["current_stage"] = "completed"
        manifest = _write_daily_manifest(root, manifest)
        return manifest
    except Exception as exc:
        manifest.update({"status": "failed", "failed_at": _now(), "error": str(exc)})
        manifest = _write_daily_manifest(root, manifest)
        return manifest


def _validate_stage_for_promotion(manifest: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if manifest.get("status") != "completed":
        issues.append("staging_package_not_completed")
    compat_manifest_value = manifest.get("compat_manifest")
    if not compat_manifest_value:
        issues.append("compatibility_manifest_missing")
        return issues
    compat_manifest = _read_manifest(Path(str(compat_manifest_value)))
    if compat_manifest.get("status") != "completed":
        issues.append("compatibility_artifacts_not_completed")
        return issues
    compat_root = Path(str(compat_manifest_value)).parent
    quality_report_path = compat_root / "quality_report.json"
    quality_report = _read_json(quality_report_path)
    if not _is_complete_production_quality_report(quality_report):
        issues.append("production_quality_report_incomplete")
    snapshot = compat_manifest.get("snapshot") or {}
    required = [
        snapshot.get("latest_hdf5_trade_date"),
        snapshot.get("latest_qlib_trade_date"),
        snapshot.get("latest_quantgpt_trade_date"),
    ]
    if not all(required):
        issues.append("missing_required_latest_dates")
    if len({value for value in required if value}) > 1:
        issues.append("latest_dates_mismatch")
    if int(snapshot.get("quantgpt_benchmark_file_count") or 0) < len(REQUIRED_BENCHMARKS):
        issues.append("quantgpt_benchmark_files_missing")
    quantgpt_contract = snapshot.get("quantgpt_contract") or {}
    if not quantgpt_contract.get("ok"):
        issues.append("quantgpt_contract_invalid")
    qlib_index_readiness = _qlib_index_readiness(
        compat_root / "qlib",
        expected_latest=snapshot.get("latest_qlib_trade_date"),
    )
    if qlib_index_readiness.get("status") != "passed":
        issues.append("qlib_index_artifacts_not_ready")
        issues.extend(str(item) for item in qlib_index_readiness.get("issues", []))
    return issues


def _daily_promotion_targets(compat_root: Path) -> list[tuple[str, Path, Path]]:
    targets: list[tuple[str, Path, Path]] = [
        ("raw_hdf", compat_root / "raw" / "stock_daily.h5", PRODUCTION_RAW_HDF5),
        ("raw_metadata", compat_root / "raw" / "metadata.json", PRODUCTION_RAW_METADATA),
        ("trade_calendar", compat_root / "raw" / "trade_calendar.txt", PRODUCTION_TRADING_CALENDAR_FILE),
        ("trade_calendar_meta", compat_root / "raw" / "trade_calendar_meta.json", PRODUCTION_TRADING_CALENDAR_META),
        ("quality_report", compat_root / "quality_report.json", PRODUCTION_QUALITY_FILE),
        ("raw_quality_report", compat_root / "raw_quality_report.json", PRODUCTION_RAW_QUALITY_FILE),
    ]
    for name in ["features", "calendars", "instruments"]:
        targets.append((f"qlib_{name}", compat_root / "qlib" / name, QLIB_DATA_ROOT / name))
    for name in ["stock_converter_meta.json", "index_converter_meta.json"]:
        src = compat_root / "qlib" / name
        if src.exists():
            targets.append((f"qlib_{name}", src, QLIB_DATA_ROOT / name))
    targets.extend(
        [
            ("quantgpt_stocks", compat_root / "quantgpt" / "stocks", QUANTGPT_DATA_DIR),
            ("quantgpt_benchmark", compat_root / "quantgpt" / "benchmark", QUANTGPT_BENCHMARK_DIR),
        ]
    )
    return targets


def _promotion_journal_path(backup_root: Path) -> Path:
    return backup_root / "promote_journal.json"


def _target_mismatches(targets: list[tuple[str, Path, Path]]) -> list[dict[str, str]]:
    mismatches: list[dict[str, str]] = []
    for label, src, dest in targets:
        if not src.exists():
            mismatches.append({"label": label, "src": str(src), "dest": str(dest), "reason": "source_missing"})
        elif not _promotion_path_equivalent(src, dest):
            mismatches.append({"label": label, "src": str(src), "dest": str(dest), "reason": "target_not_equivalent"})
    return mismatches


def _promotion_file_equivalent(src: Path, dest: Path, *, compare_small_content: bool = True) -> bool:
    try:
        if not src.is_file() or not dest.is_file():
            return False
        src_stat = src.stat()
        dest_stat = dest.stat()
        if src_stat.st_size != dest_stat.st_size:
            return False
        src_digest = hashlib.sha256()
        dest_digest = hashlib.sha256()
        with src.open("rb") as src_fh, dest.open("rb") as dest_fh:
            while True:
                src_chunk = src_fh.read(8 * 1024**2)
                dest_chunk = dest_fh.read(8 * 1024**2)
                if not src_chunk and not dest_chunk:
                    break
                src_digest.update(src_chunk)
                dest_digest.update(dest_chunk)
        return src_digest.digest() == dest_digest.digest()
    except OSError:
        return False


def _promotion_dir_equivalent(src: Path, dest: Path) -> bool:
    if not src.is_dir() or not dest.is_dir():
        return False
    try:
        src_files: dict[str, Path] = {}
        dest_files: dict[str, Path] = {}
        for root, _dirnames, filenames in os.walk(src):
            base = Path(root)
            for filename in filenames:
                path = base / filename
                src_files[path.relative_to(src).as_posix()] = path
        for root, _dirnames, filenames in os.walk(dest):
            base = Path(root)
            for filename in filenames:
                path = base / filename
                dest_files[path.relative_to(dest).as_posix()] = path
        if set(src_files) != set(dest_files):
            return False
        for rel, src_path in src_files.items():
            if not _promotion_file_equivalent(src_path, dest_files[rel], compare_small_content=False):
                return False
        return True
    except OSError:
        return False


def _promotion_path_equivalent(src: Path, dest: Path) -> bool:
    if src.is_file():
        return _promotion_file_equivalent(src, dest)
    if src.is_dir():
        return _promotion_dir_equivalent(src, dest)
    return False


def _latest_promotion_backup_for_package(package_id: str | None) -> Path | None:
    if not package_id:
        return None
    matches = sorted(PROMOTION_BACKUP_ROOT.glob(f"promote-*-{package_id}"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return matches[0] if matches else None


def _write_promotion_journal(path: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = _now()
    _write_json(path, payload)


def _project_path_string(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _record_promotion_target(journal_path: Path, *, label: str, src: Path, dest: Path) -> None:
    journal = _read_json(journal_path)
    completed = list(journal.get("completed_targets") or [])
    completed.append({"label": label, "src": str(src), "dest": str(dest), "completed_at": _now()})
    journal["completed_targets"] = completed
    journal["current_target"] = label
    _write_promotion_journal(journal_path, journal)


def _mark_promotion_in_progress(
    *,
    current_dataset: dict[str, Any],
    promotion_id: str,
    package_id: str | None,
) -> None:
    """Close consumer readiness before the first production path changes."""
    current = dict(current_dataset)
    artifact_readiness = dict(current.get("artifact_readiness") or current.get("consumer_readiness") or {})
    current["artifact_readiness"] = artifact_readiness
    current["consumer_readiness"] = {name: False for name in artifact_readiness}
    current["consumer_readiness_gate"] = "promotion_in_progress"
    current["production_audit"] = {
        "status": "pending_promotion",
        "production_package_id": package_id,
        "promotion_id": promotion_id,
    }
    current["promotion_in_progress"] = {
        "promotion_id": promotion_id,
        "package_id": package_id,
        "started_at": _now(),
    }
    atomic_write_json(CURRENT_PRODUCTION_DATASET_FILE, current)

    latest = _read_json(LATEST_STATUS_FILE)
    latest_snapshot = dict(latest.get("snapshot") or {})
    latest_artifacts = dict(
        latest_snapshot.get("artifact_readiness")
        or latest_snapshot.get("consumer_readiness")
        or artifact_readiness
    )
    latest_snapshot["artifact_readiness"] = latest_artifacts
    latest_snapshot["consumer_readiness"] = {name: False for name in latest_artifacts}
    latest_snapshot["consumer_readiness_gate"] = "promotion_in_progress"
    latest.update(
        {
            "status": "promotion_in_progress",
            "promotion_id": promotion_id,
            "promoted_from_package_id": package_id,
            "snapshot": latest_snapshot,
        }
    )
    atomic_write_json(LATEST_STATUS_FILE, latest)
    _write_daily_status(
        {
            "status": "promotion_in_progress",
            "promotion_id": promotion_id,
            "package_id": package_id,
            "consumer_readiness_gate": "promotion_in_progress",
        }
    )


def _commit_promotion_status(
    *,
    manifest: dict[str, Any],
    compat_root: Path,
    compat_manifest: dict[str, Any],
    promotion_id: str,
    backup_root: Path | None = None,
    reconciled_from_equivalent_files: bool = False,
) -> dict[str, Any]:
    snapshot = compat_manifest.get("snapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        snapshot = _snapshot_for_daily_compat(compat_root, latest_hdf5=None, latest_quantgpt=None)
    else:
        snapshot = dict(snapshot)
    quantgpt_contract = snapshot.get("quantgpt_contract")
    if isinstance(quantgpt_contract, dict):
        quantgpt_contract = dict(quantgpt_contract)
        quantgpt_contract["contract_file"] = str(QUANTGPT_DATA_DIR / "_conversion_contract.json")
        snapshot["quantgpt_contract"] = quantgpt_contract
    artifact_readiness = dict(snapshot.get("artifact_readiness") or snapshot.get("consumer_readiness") or {})
    snapshot["artifact_readiness"] = artifact_readiness
    snapshot["consumer_readiness"] = {name: False for name in artifact_readiness}
    snapshot["consumer_readiness_gate"] = "pending_production_audit"
    production_snapshot = {
        "status": "completed",
        "snapshot": snapshot,
        "steps": [],
        "promoted_from_package_id": manifest.get("package_id"),
        "promotion_id": promotion_id,
    }
    if reconciled_from_equivalent_files:
        production_snapshot["reconciled_from_equivalent_files"] = True
    atomic_write_json(LATEST_STATUS_FILE, production_snapshot)
    current_dataset = {
        "status": "production",
        "source": "tushare",
        "schema_version": "tushare_v1",
        "compatibility_mode": "tushare_raw_hdf_compat",
        "updated_at": _now(),
        "latest_trade_date": production_snapshot["snapshot"].get("latest_hdf5_trade_date"),
        "source_target_date": manifest.get("effective_target_date"),
        "production_package_id": manifest.get("package_id"),
        "promotion_id": promotion_id,
        "canonical_read_paths": {
            "production_raw_hdf5": _project_path_string(PRODUCTION_RAW_HDF5),
            "production_raw_metadata": _project_path_string(PRODUCTION_RAW_METADATA),
            "production_trading_calendar": _project_path_string(PRODUCTION_TRADING_CALENDAR_FILE),
            "production_trading_calendar_meta": _project_path_string(PRODUCTION_TRADING_CALENDAR_META),
            "qlib_root": _project_path_string(QLIB_DATA_ROOT),
            "qlib_features": _project_path_string(QLIB_DATA_ROOT / "features"),
            "qlib_calendars": _project_path_string(QLIB_DATA_ROOT / "calendars"),
            "qlib_instruments": _project_path_string(QLIB_DATA_ROOT / "instruments"),
            "quantgpt_stocks": _project_path_string(QUANTGPT_DATA_DIR),
            "quantgpt_benchmark": _project_path_string(QUANTGPT_BENCHMARK_DIR),
            "tushare_quality_report": _project_path_string(PRODUCTION_QUALITY_FILE),
            "tushare_raw_quality_report": _project_path_string(PRODUCTION_RAW_QUALITY_FILE),
        },
        "latest_dates": {
            "hdf5": production_snapshot["snapshot"].get("latest_hdf5_trade_date"),
            "qlib": production_snapshot["snapshot"].get("latest_qlib_trade_date"),
            "quantgpt": production_snapshot["snapshot"].get("latest_quantgpt_trade_date"),
        },
        "required_benchmarks": REQUIRED_BENCHMARKS,
        "artifact_readiness": artifact_readiness,
        "consumer_readiness": {name: False for name in artifact_readiness},
        "consumer_readiness_gate": "pending_production_audit",
        "production_audit": {"status": "pending", "production_package_id": manifest.get("package_id")},
        "do_not_use_as_production": [
            _project_path_string(STAGING_ROOT),
            _project_path_string(PROMOTION_BACKUP_ROOT),
        ],
        "notes": [
            "Production readers must use data/ paths, not runtime/data_foundation/staging.",
            "This production package is sourced from Tushare daily direct rebuild windows and merged through a compatibility bridge.",
            "trade_calendar.txt is the canonical production trading calendar for daily orchestration and audits.",
            "Use tushare_quality_report as the canonical production quality report for this production package.",
        ],
    }
    if reconciled_from_equivalent_files:
        current_dataset["reconciled_from_equivalent_files"] = True
    atomic_write_json(CURRENT_PRODUCTION_DATASET_FILE, current_dataset)
    payload = {
        "status": "promoted",
        "promotion_id": promotion_id,
        "package_id": manifest.get("package_id"),
        "promoted_at": _now(),
        "backup_root": str(backup_root) if backup_root is not None else None,
        "snapshot": production_snapshot.get("snapshot", {}),
    }
    if reconciled_from_equivalent_files:
        payload["reconciled_from_equivalent_files"] = True
    _write_daily_status(payload)
    return payload


@data_job_guard("daily_promote")
def data_promote_staged(
    *,
    package_id: str | None = None,
    latest: bool = False,
    wait_idle: bool = False,
    timeout_minutes: int = 180,
    dry_run: bool = False,
) -> dict[str, Any]:
    root, manifest = _resolve_package(package_id=package_id, latest=latest)
    issues = _validate_stage_for_promotion(manifest)
    idle = _wait_for_idle(timeout_minutes) if wait_idle else _promotion_idle_state()
    issues.extend(idle["blockers"])
    if issues:
        return {"status": "blocked", "package_id": manifest.get("package_id"), "blockers": issues, "idle_state": idle}

    compat_root = root / COMPAT_ROOT_NAME
    compat_manifest = _read_manifest(_compat_manifest_path(root))
    if dry_run:
        return {
            "status": "dry_run",
            "package_id": manifest.get("package_id"),
            "compat_root": str(compat_root),
            "snapshot": compat_manifest.get("snapshot"),
        }

    current_dataset = _read_json(CURRENT_PRODUCTION_DATASET_FILE)
    if current_dataset.get("production_package_id") == manifest.get("package_id"):
        prior_backup = _latest_promotion_backup_for_package(str(manifest.get("package_id") or ""))
        prior_journal = _promotion_journal_path(prior_backup) if prior_backup is not None else None
        return {
            "status": "already_promoted",
            "package_id": manifest.get("package_id"),
            "promote_journal_path": str(prior_journal) if prior_journal is not None and prior_journal.exists() else None,
        }

    readiness = _qlib_index_readiness(compat_root / "qlib")
    if readiness.get("status") != "passed":
        issues = ",".join(str(item) for item in readiness.get("issues", []))
        return {
            "status": "blocked",
            "package_id": manifest.get("package_id"),
            "blockers": [f"qlib_index_artifacts_not_ready:{issues}"],
            "idle_state": idle,
        }

    targets = _daily_promotion_targets(compat_root)
    prior_backup = _latest_promotion_backup_for_package(str(manifest.get("package_id") or ""))
    if prior_backup is not None:
        already_replaced_mismatches = _target_mismatches(targets)
    else:
        already_replaced_mismatches = [{"reason": "no_prior_promotion_backup"}]
    if not already_replaced_mismatches:
        promotion_id = prior_backup.name if prior_backup else f"promote-reconcile-{datetime.now().strftime('%Y%m%d_%H%M%S')}-{manifest.get('package_id')}"
        reconcile_journal = _promotion_journal_path(prior_backup) if prior_backup is not None else None
        state_snapshot = _snapshot_state_files([LATEST_STATUS_FILE, CURRENT_PRODUCTION_DATASET_FILE, DAILY_STATUS_FILE])
        try:
            payload = _commit_promotion_status(
                manifest=manifest,
                compat_root=compat_root,
                compat_manifest=compat_manifest,
                promotion_id=promotion_id,
                backup_root=prior_backup,
                reconciled_from_equivalent_files=True,
            )
            payload["reconciled_from_equivalent_files"] = True
            if reconcile_journal is not None:
                _write_promotion_journal(
                    reconcile_journal,
                    {
                        "status": "reconciled_committed",
                        "promotion_id": promotion_id,
                        "package_id": manifest.get("package_id"),
                        "compat_root": str(compat_root),
                        "backup_root": str(prior_backup),
                        "reconciled_from_equivalent_files": True,
                        "committed_at": _now(),
                        "targets": [{"label": label, "src": str(src), "dest": str(dest)} for label, src, dest in targets],
                    },
                )
                payload["promote_journal_path"] = str(reconcile_journal)
            else:
                payload["promote_journal_path"] = None
            _write_daily_status(payload)
            return payload
        except Exception as exc:
            state_rollback = _restore_state_files(state_snapshot)
            if state_rollback.get("status") != "passed":
                raise RuntimeError(f"promotion_reconcile_state_rollback_failed:{exc}:{state_rollback.get('errors')}") from exc
            raise

    promotion_id = f"promote-{datetime.now().strftime('%Y%m%d_%H%M%S')}-{manifest.get('package_id')}"
    backup_root = PROMOTION_BACKUP_ROOT / promotion_id
    journal_path = _promotion_journal_path(backup_root)
    replaced: list[tuple[Path, Path, bool]] = []
    state_snapshot = _snapshot_state_files([LATEST_STATUS_FILE, CURRENT_PRODUCTION_DATASET_FILE, DAILY_STATUS_FILE])
    _acquire_lock(PRODUCTION_LOCK_DIR)
    try:
        _write_promotion_journal(
            journal_path,
            {
                "status": "in_progress",
                "promotion_id": promotion_id,
                "package_id": manifest.get("package_id"),
                "compat_root": str(compat_root),
                "backup_root": str(backup_root),
                "started_at": _now(),
                "targets": [{"label": label, "src": str(src), "dest": str(dest)} for label, src, dest in targets],
                "completed_targets": [],
            },
        )
        _mark_promotion_in_progress(
            current_dataset=current_dataset,
            promotion_id=promotion_id,
            package_id=manifest.get("package_id"),
        )
        for label, src, dest in targets:
            _replace_path(src, dest, backup_root, replaced)
            _record_promotion_target(journal_path, label=label, src=src, dest=dest)
        post_replace_mismatches = _target_mismatches(targets)
        if post_replace_mismatches:
            _write_promotion_journal(
                journal_path,
                {
                    **_read_json(journal_path),
                    "status": "verification_failed",
                    "mismatches": post_replace_mismatches,
                },
            )
            raise RuntimeError(f"promotion_target_mismatch:{post_replace_mismatches[:3]}")

        payload = _commit_promotion_status(
            manifest=manifest,
            compat_root=compat_root,
            compat_manifest=compat_manifest,
            promotion_id=promotion_id,
            backup_root=backup_root,
        )
        payload["promote_journal_path"] = str(journal_path)
        _write_daily_status(payload)
        _write_promotion_journal(
            journal_path,
            {
                **_read_json(journal_path),
                "status": "committed",
                "committed_at": _now(),
            },
        )
        return payload
    except Exception as exc:
        if journal_path.exists():
            try:
                _write_promotion_journal(
                    journal_path,
                    {
                        **_read_json(journal_path),
                        "status": "rollback_started",
                        "error": str(exc),
                        "rollback_started_at": _now(),
                    },
                )
            except Exception:
                pass
        rollback = _rollback(replaced)
        state_rollback = _restore_state_files(state_snapshot)
        if journal_path.exists():
            try:
                _write_promotion_journal(
                    journal_path,
                    {
                        **_read_json(journal_path),
                        "status": "rolled_back"
                        if rollback.get("status") == "passed" and state_rollback.get("status") == "passed"
                        else "rollback_failed",
                        "rolled_back_at": _now(),
                        "rollback": rollback,
                        "state_rollback": state_rollback,
                    },
                )
            except Exception:
                pass
        if rollback.get("status") != "passed" or state_rollback.get("status") != "passed":
            raise RuntimeError(
                f"promotion_failed_and_rollback_failed:{exc}:files={rollback.get('errors')}:state={state_rollback.get('errors')}"
            ) from exc
        raise
    finally:
        _release_lock(PRODUCTION_LOCK_DIR)


@data_job_guard("daily_routine")
def data_daily_routine(
    *,
    target_date: str | None = "auto",
    wait_idle: bool = True,
    timeout_minutes: int = 180,
    dry_run: bool = False,
) -> dict[str, Any]:
    preflight = data_daily_preflight(target_date, for_promotion=False)
    resource_wait = None
    if preflight.get("status") != "go" and set(preflight.get("blockers") or []) == {"mem_available_below_8gb"}:
        resource_wait = _wait_for_memory_headroom()
        if resource_wait.get("status") == "ready":
            preflight = data_daily_preflight(target_date, for_promotion=False)
        preflight["resource_wait"] = resource_wait
    if preflight["status"] != "go":
        return {"status": "blocked", "stage": "preflight", "preflight": preflight}
    if preflight.get("already_current"):
        return {
            "status": "already_current",
            "target_date": preflight.get("selected_target_date"),
            "preflight": preflight,
        }
    stage = data_stage_update(target_date, dry_run=dry_run, _validated_preflight=preflight)
    if dry_run or stage.get("status") != "completed":
        return {
            "status": stage.get("status"),
            "target_date": preflight.get("selected_target_date"),
            "preflight": preflight,
            "stage": stage,
        }
    promote = data_promote_staged(
        package_id=stage.get("package_id"),
        latest=False,
        wait_idle=wait_idle,
        timeout_minutes=timeout_minutes,
        dry_run=False,
    )
    if promote.get("status") not in {"promoted", "already_promoted"}:
        return {
            "status": "promote_failed",
            "target_date": preflight.get("selected_target_date"),
            "preflight": preflight,
            "stage": stage,
            "promote": promote,
        }
    try:
        post_promote_cleanup = _post_promote_cleanup()
    except Exception as exc:
        post_promote_cleanup = {
            "preview": {"status": "failed", "error": str(exc)},
            "execute": None,
            "execute_policy": {
                "profile": "safe",
                "eligible": False,
                "blocked_reason": "cleanup_preview_failed",
            },
        }
    try:
        post_promote_audit = production_audit_summary(
            replace_from_date=str(preflight.get("replace_from_date") or ""),
            full_scan=False,
            deep_sample_count=DEFAULT_DEEP_SAMPLE_COUNT,
            write_report=True,
        )
    except Exception as exc:
        post_promote_audit = {"status": "failed", "issues": ["post_promote_audit_exception"], "error": str(exc)}
    production_audit_gate = record_production_audit_result(post_promote_audit)
    cleanup_preview = post_promote_cleanup.get("preview") or {}
    cleanup_execute = post_promote_cleanup.get("execute")
    cleanup_execute = cleanup_execute if isinstance(cleanup_execute, dict) else None
    cleanup_reclaimed_bytes = int(cleanup_execute.get("deleted_bytes") or 0) if cleanup_execute else 0
    return {
        "status": "completed" if post_promote_audit.get("status") == "passed" else "promoted_audit_failed",
        "target_date": preflight.get("selected_target_date"),
        "preflight": preflight,
        "stage": stage,
        "promote": promote,
        "promote_journal_path": promote.get("promote_journal_path"),
        "post_promote_audit": post_promote_audit,
        "production_audit_gate": production_audit_gate,
        "post_promote_audit_report_path": post_promote_audit.get("audit_report_path"),
        "post_promote_cleanup_preview": post_promote_cleanup.get("preview"),
        "post_promote_cleanup_execute": post_promote_cleanup.get("execute"),
        "post_promote_cleanup_policy": post_promote_cleanup.get("execute_policy"),
        "post_promote_cleanup_preview_report_path": cleanup_preview.get("report_path"),
        "post_promote_cleanup_execute_report_path": cleanup_execute.get("report_path") if cleanup_execute else None,
        "post_promote_cleanup_preview_reclaimable_bytes": int(cleanup_preview.get("reclaimable_bytes") or 0),
        "post_promote_cleanup_reclaimed_bytes": cleanup_reclaimed_bytes,
    }
