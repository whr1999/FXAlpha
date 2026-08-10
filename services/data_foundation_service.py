from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from threading import Lock
from typing import Any
from uuid import uuid4

from services._base import ServiceResult, err_result, ok_result
from domain.data_foundation.runtime_io import atomic_write_json
from domain.data_foundation.tushare_daily import (
    DAILY_STATUS_FILE,
    data_daily_preflight as run_data_daily_preflight,
    data_daily_routine as run_data_daily_routine,
    data_promote_staged as run_data_promote_staged,
    data_stage_update as run_data_stage_update,
    latest_staging_package,
    production_consistency_status,
    production_audit_summary,
    record_production_audit_result,
)
from integrations.tushare.client import tushare_network_preflight
from domain.data_foundation.tushare_rebuild import (
    TushareRebuildConfig,
    tushare_full_rebuild,
    tushare_full_rebuild_status,
    tushare_preflight,
)
from domain.data_foundation.tushare_production import (
    prepare_tushare_production_artifacts,
    promote_tushare_production_artifacts,
)
from domain.data_foundation.tushare_limit_backfill import build_tushare_limit_backfill
from domain.data_foundation.tushare_status_backfill import build_tushare_status_backfill
from domain.data_foundation.stock_metadata import build_stock_identity_cache, stock_identity_cache_status
from domain.data_foundation.quality_check import check as run_quality_check
from domain.data_foundation.update import data_foundation_status
from storage.paths import CURRENT_PRODUCTION_DATASET_FILE, DATA_FOUNDATION_ROOT, PRODUCTION_RAW_HDF5, QLIB_DATA_ROOT


GUI_JOBS_ROOT = DATA_FOUNDATION_ROOT / "gui_jobs"
DATA_QUERY_MAX_FIELDS = 64
DATA_QUERY_MAX_ROWS = 6000
DATA_QUERY_DEFAULT_FIELDS = ["volume", "PE", "PB"]
DATA_QUERY_FIELD_GROUPS = {
    "价格衍生": [
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "amp",
        "pct_chg",
        "backward_factor",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
        "adj_pre_close",
        "adj_pct_chg",
        "adj_amp",
    ],
    "成交": ["volume", "amount", "turnover_rate", "turnover_rate_f", "free_share"],
    "估值": ["PE", "PB", "ps_ttm", "dv_ttm", "total_mv", "float_mv", "circ_mv"],
    "财务基本面": [
        "TOT_SHARE",
        "FLOAT_A_SHARE",
        "EPS",
        "NET_PROFIT",
        "TOT_EQUITY",
        "TOTAL_ASSETS",
        "NET_ASSET_PS",
        "HOLDER_NUM",
        "ROE",
        "ROA",
    ],
    "资金": [
        "BORROW_MONEY_BAL",
        "PURCH_BORROW_MONEY",
        "SEC_LENDING_BAL",
        "MARGIN_TRADE_BAL",
        "sm_net_vol",
        "sm_net_amount",
        "lg_net_vol",
        "lg_net_amount",
        "net_mf_vol",
        "net_mf_amount",
        "margin_buy_amount",
        "margin_balance",
        "short_balance",
        "fin_balance",
    ],
    "筹码成本": ["cost_15pct", "cost_85pct", "weight_avg"],
    "交易约束审计": ["up_limit", "down_limit", "limit_source_kind"],
}
DATA_QUERY_EXCLUDED_FIELDS = {
    "code",
    "kline_time",
    "trade_date",
    "SECURITY_NAME",
    "MARKET_CODE",
    "LIST_DATE",
    "list_status",
    "st_status",
}
INDEX_CODE_ALIASES = {
    "000300": "000300.SH",
    "000300.SH": "000300.SH",
    "000300SH": "000300.SH",
    "000300sh": "000300.SH",
    "沪深300": "000300.SH",
}
_JOB_LOCK = Lock()
DAILY_GUI_STAGE_SEQUENCE = [
    "source_rebuild",
    "source_prepare_production",
    "merge_production_hdf",
    "merged_quality_check",
    "build_compat_outputs",
    "completed",
]


def _production_quality_report(current_dataset: dict | None = None) -> dict:
    current_dataset = current_dataset or {}
    canonical_paths = current_dataset.get("canonical_read_paths") or {}
    quality_path = canonical_paths.get("tushare_quality_report") if current_dataset.get("source") == "tushare" else None
    if quality_path:
        configured = Path(str(quality_path)).expanduser()
        candidates = [
            configured if configured.is_absolute() else CURRENT_PRODUCTION_DATASET_FILE.parents[2] / configured,
        ]
        # Dataset manifests intentionally store portable repository-relative
        # paths.  A release checkout may, however, mount the durable production
        # HDF tree outside that checkout.  Resolve a report colocated with the
        # configured HDF before falling back to an expensive live quality scan.
        if not configured.is_absolute():
            canonical_hdf = Path(str(canonical_paths.get("production_raw_hdf5") or ""))
            try:
                report_suffix = configured.relative_to(canonical_hdf.parent)
            except (TypeError, ValueError):
                report_suffix = Path(configured.name)
            candidates.append(PRODUCTION_RAW_HDF5.parent / report_suffix)
        for resolved in dict.fromkeys(candidates):
            try:
                if resolved.exists():
                    return json.loads(resolved.read_text(encoding="utf-8"))
            except Exception:
                continue
    try:
        return run_quality_check()
    except Exception as exc:
        return {"passed": False, "issues": [f"production_quality_check_failed: {exc}"], "warnings": []}


def _data_quality_summary(snapshot: dict, quality: dict) -> dict:
    groups = quality.get("field_groups") or {}
    market = groups.get("market_core_fields") or {}
    fundamental = groups.get("valuation_and_fundamental_fields") or {}
    margin = groups.get("margin_fields") or {}
    return {
        "passed": quality.get("passed") if quality else None,
        "n_rows": quality.get("n_rows"),
        "missing_pct": quality.get("missing_pct"),
        "latest_trade_date": quality.get("latest_trade_date") or snapshot.get("latest_hdf5_trade_date"),
        "latest_code_activity": quality.get("latest_code_activity") or {},
        "field_groups": groups,
        "market_core_max_missing_pct": market.get("max_missing_pct"),
        "fundamental_max_missing_pct": fundamental.get("max_missing_pct"),
        "margin_max_missing_pct": margin.get("max_missing_pct"),
        "zero_close_ratio": quality.get("zero_close_ratio"),
        "schema_summary": quality.get("schema_summary") or {},
        "factor_adjusted_quality": quality.get("factor_adjusted_quality") or {},
        "quantgpt_coverage_ratio": snapshot.get("quantgpt_latest_coverage_ratio"),
        "quantgpt_stale_stock_count": snapshot.get("quantgpt_stale_stock_count"),
        "metadata_quality": quality.get("metadata_quality") or {},
        "benchmark_index_quality": quality.get("benchmark_index_quality") or {},
        "limit_price_quality": quality.get("limit_price_quality") or {},
        "issues": quality.get("issues") or [],
        "warnings": quality.get("warnings") or ([] if quality else ["quality_report_missing"]),
    }


def _latest_production_audit_report() -> dict[str, Any]:
    audit_root = DATA_FOUNDATION_ROOT / "audits"
    if not audit_root.exists():
        return {"path": None, "status": "missing"}
    candidates = sorted(
        [
            *audit_root.glob("production_quality_audit_*.json"),
            *audit_root.glob("production_quality_deep_audit_*.json"),
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return {"path": None, "status": "missing"}
    latest = candidates[0]
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"path": str(latest), "status": "read_failed", "error": str(exc)}
    return {
        "path": str(latest),
        "status": payload.get("status"),
        "generated_at": payload.get("generated_at"),
        "latest_trade_date": payload.get("latest_trade_date"),
        "replace_from_date": payload.get("replace_from_date"),
        "deep_sample_count": payload.get("deep_sample_count"),
        "issues": payload.get("issues") or [],
        "production_package_id": payload.get("production_package_id"),
    }


def _production_health(current_dataset: dict[str, Any], latest_audit: dict[str, Any]) -> dict[str, Any]:
    audit_applies = bool(
        latest_audit.get("path")
        and latest_audit.get("production_package_id") == current_dataset.get("production_package_id")
        and latest_audit.get("latest_trade_date") == current_dataset.get("latest_trade_date")
    )
    if audit_applies and latest_audit.get("status") != "passed":
        return {"status": "blocked", "reason": "latest_production_audit_failed", "audit": latest_audit}
    persisted = current_dataset.get("production_audit") or {}
    if persisted.get("status") == "failed":
        return {"status": "blocked", "reason": "persisted_production_audit_failed", "audit": persisted}
    if persisted.get("status") == "pending" or current_dataset.get("consumer_readiness_gate") == "pending_production_audit":
        return {"status": "blocked", "reason": "production_audit_pending", "audit": persisted}
    passed = audit_applies or persisted.get("status") == "passed"
    return {
        "status": "ready" if passed else "unknown",
        "reason": "production_audit_passed" if passed else "production_audit_missing",
        "audit": latest_audit if audit_applies else persisted,
    }


def data_status() -> ServiceResult:
    try:
        result = data_foundation_status()
        snapshot = result.get("snapshot", {})
        daily_status = {}
        if DAILY_STATUS_FILE.exists():
            daily_status = json.loads(DAILY_STATUS_FILE.read_text(encoding="utf-8"))
        daily_status = _sanitize_daily_status(daily_status)
        current_dataset = {}
        if CURRENT_PRODUCTION_DATASET_FILE.exists():
            current_dataset = json.loads(CURRENT_PRODUCTION_DATASET_FILE.read_text(encoding="utf-8"))
        quality = _production_quality_report(current_dataset)
        latest_audit = _latest_production_audit_report()
        production_consistency = production_consistency_status(
            current=current_dataset,
            latest_status=result,
            snapshot=snapshot,
        )
        production_health = _production_health(current_dataset, latest_audit)
        partial_detected = bool(production_consistency.get("partial_promote_detected"))
        return ok_result(
            outputs={
                "status": "production_audit_failed" if production_health.get("status") == "blocked" else result.get("status"),
                "snapshot": snapshot,
                "data_quality_summary": _data_quality_summary(snapshot, quality),
                "steps": result.get("steps", []),
                "daily_update": daily_status,
                "latest_staging_package": latest_staging_package(),
                "current_production_dataset": current_dataset,
                "production_consistency": production_consistency,
                "partial_promote_status": {
                    "status": "blocked" if partial_detected else "clear",
                    "detected": partial_detected,
                    "issues": production_consistency.get("issues") or [],
                    "mismatches": production_consistency.get("mismatches") or [],
                },
                "latest_audit_report": latest_audit,
                "production_health": production_health,
            },
            artifacts=result.get("artifacts", {}),
        )
    except Exception as e:
        return err_result(str(e))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "read_failed", "path": str(path), "error": str(exc)}
    return {}


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _sanitize_daily_status(status: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(status, dict) or not status:
        return {}
    stage = status.get("latest_stage") if isinstance(status.get("latest_stage"), dict) else status
    package_root_text = stage.get("package_root") if isinstance(stage, dict) else None
    if not package_root_text:
        return status
    package_root = Path(str(package_root_text)).expanduser()
    staging_root = DATA_FOUNDATION_ROOT / "staging"
    invalid_reason = None
    if not package_root.exists():
        invalid_reason = "daily_status_package_root_missing"
    elif not _path_is_under(package_root, staging_root):
        invalid_reason = "daily_status_package_root_outside_staging"
    if not invalid_reason:
        return status

    sanitized = dict(status)
    sanitized["status"] = "stale_invalid"
    sanitized["invalid_reason"] = invalid_reason
    sanitized["invalid_package_root"] = str(package_root)
    if isinstance(status.get("latest_stage"), dict):
        latest_stage = dict(status["latest_stage"])
        latest_stage["status"] = "stale_invalid"
        latest_stage["invalid_reason"] = invalid_reason
        sanitized["latest_stage"] = latest_stage
    return sanitized


def _daily_status_stage(status: dict[str, Any]) -> dict[str, Any]:
    latest_stage = status.get("latest_stage")
    if isinstance(latest_stage, dict):
        return latest_stage
    return status


def _write_gui_job(job: dict[str, Any]) -> dict[str, Any]:
    GUI_JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    path = GUI_JOBS_ROOT / f"{job['job_id']}.json"
    job["job_path"] = str(path)
    atomic_write_json(path, job)
    return job


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
        if value <= 0:
            return False
        os.kill(value, 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def _job_is_running(job: dict[str, Any]) -> bool:
    status = str(job.get("status") or "").lower()
    if status == "running":
        return _pid_alive(job.get("pid"))
    if status != "queued":
        return False
    try:
        created = datetime.fromisoformat(str(job.get("created_at")))
        return (datetime.now() - created).total_seconds() < 120
    except Exception:
        return False


def _gui_jobs() -> list[dict[str, Any]]:
    try:
        candidates = sorted(GUI_JOBS_ROOT.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except Exception:
        return []
    return [_read_json_file(path) for path in candidates if path.is_file()]


def _latest_gui_job() -> dict[str, Any]:
    jobs = _gui_jobs()
    return jobs[0] if jobs else {}


def _active_gui_job() -> dict[str, Any] | None:
    for job in _gui_jobs():
        if _job_is_running(job):
            return job
        if str(job.get("status") or "").lower() in {"queued", "running"}:
            job["status"] = "interrupted"
            job["error"] = "worker_missing_or_stale"
            job["finished_at"] = _now()
            _write_gui_job(job)
    return None


def _stage_events_from_daily_status(daily_status: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    daily_status = _daily_status_stage(daily_status)
    for key in [
        "source_rebuild",
        "source_prepare_production",
        "merge_result",
        "quality_report",
        "raw_quality_report",
        "compat_manifest",
        "snapshot",
    ]:
        value = daily_status.get(key)
        if value:
            events.append({"stage": key, "status": value.get("status") if isinstance(value, dict) else "recorded"})
    if daily_status.get("status"):
        events.append({"stage": daily_status.get("current_stage") or "daily_update", "status": daily_status.get("status")})
    return events[-12:]


def _text_status(value: Any, fallback: str) -> str:
    return str(value) if value not in (None, "") else fallback


def _compact_progress(progress: dict[str, Any]) -> dict[str, Any]:
    stages = {}
    for name, stage in (progress.get("stages") or {}).items():
        stages[name] = {
            "cursor": stage.get("cursor"),
            "total": stage.get("total"),
            "status": stage.get("status"),
            "current_key": stage.get("current_key"),
        }
    return {
        "status": progress.get("status"),
        "package_id": progress.get("package_id"),
        "updated_at": progress.get("updated_at"),
        "stages": stages,
    }


def _compact_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "package_id": manifest.get("package_id"),
        "package_kind": manifest.get("package_kind"),
        "status": manifest.get("status"),
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "start_date": manifest.get("start_date"),
        "cutoff_date": manifest.get("cutoff_date"),
        "target_date": manifest.get("target_date"),
        "effective_target_date": manifest.get("effective_target_date"),
        "selected_target_date": manifest.get("selected_target_date"),
        "replace_from_date": manifest.get("replace_from_date"),
        "trade_date_count": manifest.get("trade_date_count"),
        "code_count": manifest.get("code_count"),
        "current_stage": manifest.get("current_stage"),
        "stage_summary": manifest.get("stage_summary"),
    }


def _compact_daily_status(status: dict[str, Any]) -> dict[str, Any]:
    top_status = status
    status = _daily_status_stage(status)
    snapshot = status.get("snapshot") or {}
    source_rebuild = status.get("source_rebuild") if isinstance(status.get("source_rebuild"), dict) else {}
    source_prepare = status.get("source_prepare_production") if isinstance(status.get("source_prepare_production"), dict) else {}
    merge_result = status.get("merge_result") if isinstance(status.get("merge_result"), dict) else {}
    stage_summary = status.get("stage_summary") or {}
    if not stage_summary and status.get("status") in {"completed", "promoted", "already_promoted"}:
        stage_summary = {
            "current_stage": "completed",
            "completed_stages": DAILY_GUI_STAGE_SEQUENCE,
            "completed_stage_count": len(DAILY_GUI_STAGE_SEQUENCE),
            "total_stage_count": len(DAILY_GUI_STAGE_SEQUENCE),
        }
    return {
        "status": top_status.get("status") if top_status.get("status") == "stale_invalid" else status.get("status"),
        "invalid_reason": top_status.get("invalid_reason"),
        "invalid_package_root": top_status.get("invalid_package_root"),
        "current_stage": status.get("current_stage") or stage_summary.get("current_stage"),
        "stage_summary": stage_summary,
        "package_id": status.get("package_id"),
        "promotion_id": status.get("promotion_id"),
        "promoted_at": status.get("promoted_at"),
        "created_at": status.get("created_at"),
        "updated_at": status.get("updated_at"),
        "completed_at": status.get("completed_at"),
        "failed_at": status.get("failed_at"),
        "error": status.get("error"),
        "source_rebuild": {
            "status": source_rebuild.get("status"),
            "package_id": source_rebuild.get("package_id"),
            "trade_date_count": source_rebuild.get("trade_date_count"),
            "code_count": source_rebuild.get("code_count"),
        },
        "source_prepare_production": {
            "status": source_prepare.get("status"),
            "stock_rows": source_prepare.get("stock_rows"),
            "index_rows": source_prepare.get("index_rows"),
        },
        "merge_result": {
            "preserved_rows": merge_result.get("preserved_rows"),
            "removed_rows": merge_result.get("removed_rows"),
            "delta_rows": merge_result.get("delta_rows"),
            "boundary_repaired_rows": merge_result.get("boundary_repaired_rows"),
        },
        "quality_report": status.get("quality_report"),
        "raw_quality_report": status.get("raw_quality_report"),
        "compat_manifest": status.get("compat_manifest"),
        "snapshot": {
            "latest_hdf5_trade_date": snapshot.get("latest_hdf5_trade_date"),
            "latest_qlib_trade_date": snapshot.get("latest_qlib_trade_date"),
            "latest_quantgpt_trade_date": snapshot.get("latest_quantgpt_trade_date"),
            "quantgpt_stock_parquet_count": snapshot.get("quantgpt_stock_parquet_count"),
            "quantgpt_latest_coverage_ratio": snapshot.get("quantgpt_latest_coverage_ratio"),
            "quantgpt_stale_stock_count": snapshot.get("quantgpt_stale_stock_count"),
        },
    }


def _latest_full_rebuild_status() -> dict[str, Any]:
    staging_root = DATA_FOUNDATION_ROOT / "staging"
    try:
        candidates = sorted(staging_root.glob("*/full_rebuild_progress.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except Exception as exc:
        return {"status": "read_failed", "error": str(exc)}
    if not candidates:
        return {"status": "not_found"}
    progress_path = candidates[0]
    package_root = progress_path.parent
    manifest = _read_json_file(package_root / "manifest.json")
    return {
        "status": "ok",
        "package_root": str(package_root),
        "manifest": _compact_manifest(manifest),
        "progress": _compact_progress(_read_json_file(progress_path)),
    }


def data_live_status() -> ServiceResult:
    """Read-only GUI status aggregation for daily/full-rebuild monitoring."""
    try:
        daily_status = _sanitize_daily_status(_read_json_file(DAILY_STATUS_FILE))
        current_dataset = _read_json_file(CURRENT_PRODUCTION_DATASET_FILE)
        latest_package = latest_staging_package()
        full_rebuild = _latest_full_rebuild_status()
        latest_job = _latest_gui_job()
        active_job = _active_gui_job() or {}
        compact_daily = _compact_daily_status(daily_status)
        return ok_result(
            outputs={
                "status": "running" if active_job else _text_status(daily_status.get("status"), "idle"),
                "daily_update": compact_daily,
                "daily_stage_summary": compact_daily.get("stage_summary") or {},
                "full_rebuild": full_rebuild if full_rebuild else {"status": "not_found"},
                "source_progress": (full_rebuild or {}).get("progress") or {},
                "latest_staging_package": _compact_manifest(latest_package or {}),
                "current_production_dataset": {
                    "status": current_dataset.get("status"),
                    "source": current_dataset.get("source"),
                    "schema_version": current_dataset.get("schema_version"),
                    "updated_at": current_dataset.get("updated_at"),
                    "latest_trade_date": current_dataset.get("latest_trade_date"),
                    "production_package_id": current_dataset.get("production_package_id"),
                    "latest_dates": current_dataset.get("latest_dates") or {},
                },
                "active_job": active_job,
                "latest_job": latest_job,
                "events": _stage_events_from_daily_status(daily_status),
                "generated_at": _now(),
            }
        )
    except Exception as e:
        return err_result(str(e))


def _launch_data_job_worker(job: dict[str, Any]) -> dict[str, Any]:
    worker_script = Path(__file__).resolve().parents[1] / "scripts" / "data_foundation" / "job_worker.py"
    job_path = Path(str(job["job_path"]))
    command = [sys.executable, str(worker_script), "--job-path", str(job_path)]
    safe_job = "".join(char if char.isalnum() or char in "-_" else "-" for char in str(job["job_id"]))[-56:]
    unit = f"fxalpha-data-{safe_job}"
    systemd_run = shutil.which("systemd-run")
    if systemd_run:
        job.update({"launch_mode": "systemd_transient", "worker_unit": unit})
        _write_gui_job(job)
        systemd_command = [
            systemd_run,
            "--user",
            f"--unit={unit}",
            "--collect",
            "--property=Restart=no",
            "--property=KillMode=control-group",
            f"--property=WorkingDirectory={Path(__file__).resolve().parents[1]}",
            f"--setenv=PYTHONPATH={Path(__file__).resolve().parents[1]}",
            *command,
        ]
        completed = subprocess.run(
            systemd_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )
        if completed.returncode == 0:
            return job
        job["systemd_error"] = str(completed.stderr or completed.stdout or "").strip()[-1000:]

    job.update({"launch_mode": "detached_process", "worker_unit": None})
    _write_gui_job(job)
    process = subprocess.Popen(
        command,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    job["launcher_pid"] = process.pid
    return job


def data_update_start(
    *,
    mode: str = "daily",
    target_date: str | None = "auto",
    dry_run: bool = True,
    timeout_minutes: int = 180,
    confirm: bool = False,
) -> ServiceResult:
    inputs = {
        "mode": mode,
        "target_date": target_date or "auto",
        "dry_run": dry_run,
        "timeout_minutes": timeout_minutes,
        "confirm": confirm,
    }
    try:
        if mode not in {"daily", "full_rebuild"}:
            return err_result("unsupported_data_update_mode", inputs=inputs)
        if not dry_run and not confirm:
            return err_result("data_update_execution_confirmation_required", inputs=inputs)
        with _JOB_LOCK:
            active = _active_gui_job()
            if active:
                return ok_result(inputs=inputs, outputs={"status": "already_running", "active_job": active})
            job = {
                "job_id": f"data-gui-{datetime.now().strftime('%Y%m%d_%H%M%S')}-{uuid4().hex[:8]}",
                "mode": mode,
                "target_date": target_date or "auto",
                "dry_run": dry_run,
                "timeout_minutes": timeout_minutes,
                "status": "queued",
                "created_at": _now(),
            }
            _write_gui_job(job)
            try:
                _launch_data_job_worker(job)
            except Exception as exc:
                job.update({"status": "failed", "ok": False, "error": f"worker_launch_failed:{exc}", "finished_at": _now()})
                _write_gui_job(job)
                raise
        return ok_result(inputs=inputs, outputs={"status": "started", "job": job})
    except Exception as e:
        return err_result(str(e), inputs=inputs)


def _normalize_code(raw: str | None) -> str:
    value = str(raw or "").strip().upper().replace("_", ".")
    if not value:
        raise ValueError("code_required")
    if value in INDEX_CODE_ALIASES:
        return INDEX_CODE_ALIASES[value]
    compact = value.replace(".", "")
    if len(compact) == 8 and compact[-2:] in {"SH", "SZ", "BJ"}:
        return f"{compact[:6]}.{compact[-2:]}"
    if len(compact) == 6 and compact.isdigit():
        suffix = "SH" if compact.startswith(("5", "6", "9")) else "BJ" if compact.startswith(("4", "8")) else "SZ"
        return f"{compact}.{suffix}"
    return value


def _query_code_candidates(raw: str | None) -> list[str]:
    normalized = _normalize_code(raw)
    compact = str(raw or "").strip().upper().replace("_", ".").replace(".", "")
    if not (len(compact) == 6 and compact.isdigit()):
        return [normalized]
    candidates = [normalized]
    for suffix in ("SZ", "SH", "BJ"):
        candidate = f"{compact}.{suffix}"
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _code_has_rows(code: str) -> bool:
    import pandas as pd

    with pd.HDFStore(PRODUCTION_RAW_HDF5, mode="r") as store:
        sample = store.select("/daily", where=f"code == {repr(code)}", columns=["code"])
    return not sample.empty


def _resolve_query_code(raw: str | None) -> str:
    candidates = _query_code_candidates(raw)
    for candidate in candidates:
        try:
            if _code_has_rows(candidate):
                return candidate
        except Exception:
            break
    return candidates[0]


def _hdf_columns() -> list[str]:
    import pandas as pd

    with pd.HDFStore(PRODUCTION_RAW_HDF5, mode="r") as store:
        storer = store.get_storer("/daily")
        return list(storer.non_index_axes[0][1])


def _query_field_groups(columns: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    seen: set[str] = set()
    for name, fields in DATA_QUERY_FIELD_GROUPS.items():
        present = [field for field in fields if field in columns and field not in DATA_QUERY_EXCLUDED_FIELDS]
        if present:
            grouped[name] = present
            seen.update(present)
    extras = [
        field
        for field in columns
        if field not in seen and field not in DATA_QUERY_EXCLUDED_FIELDS
    ]
    if extras:
        grouped["其他字段"] = extras
    return grouped


def data_query_fields() -> ServiceResult:
    try:
        columns = _hdf_columns()
        groups = _query_field_groups(columns)
        defaults = [field for field in DATA_QUERY_DEFAULT_FIELDS if field in columns]
        return ok_result(
            outputs={
                "hdf_path": str(PRODUCTION_RAW_HDF5),
                "columns": columns,
                "groups": groups,
                "default_fields": defaults,
                "transforms": ["zscore", "index100", "raw", "pct_change"],
                "benchmark_defaults": [{"code": "000300.SH", "label": "沪深300"}],
            }
        )
    except Exception as e:
        return err_result(str(e))


def data_benchmark_series(
    *,
    code: str = "000300.SH",
    start: str | None = None,
    end: str | None = None,
) -> ServiceResult:
    """Read one benchmark close series directly from the compact Qlib provider files."""
    inputs = {"code": code, "start": start, "end": end}
    try:
        normalized = _normalize_code(code)
        qlib_code = normalized.replace(".", "").lower()
        calendar_path = QLIB_DATA_ROOT / "calendars" / "day.txt"
        feature_path = QLIB_DATA_ROOT / "features" / qlib_code / "close.day.bin"
        if not calendar_path.is_file():
            raise FileNotFoundError(f"qlib_calendar_missing:{calendar_path}")
        if not feature_path.is_file():
            raise FileNotFoundError(f"qlib_benchmark_close_missing:{feature_path}")

        calendar = [line.strip()[:10] for line in calendar_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        payload = feature_path.read_bytes()
        if len(payload) < 8 or len(payload) % 4:
            raise ValueError("qlib_benchmark_close_invalid")
        values = struct.unpack(f"<{len(payload) // 4}f", payload)
        calendar_start = int(values[0])
        rows: list[dict[str, Any]] = []
        for offset, raw_value in enumerate(values[1:]):
            calendar_index = calendar_start + offset
            if calendar_index < 0 or calendar_index >= len(calendar):
                continue
            trade_date = calendar[calendar_index]
            if start and trade_date < str(start)[:10]:
                continue
            if end and trade_date > str(end)[:10]:
                continue
            value = float(raw_value)
            if not math.isfinite(value):
                continue
            rows.append({"date": trade_date, "code": normalized, "close": value})
        if len(rows) > DATA_QUERY_MAX_ROWS:
            rows = rows[-DATA_QUERY_MAX_ROWS:]
        return ok_result(
            inputs=inputs,
            outputs={
                "metadata": {
                    "code": normalized,
                    "row_count": len(rows),
                    "start": rows[0]["date"] if rows else None,
                    "end": rows[-1]["date"] if rows else None,
                    "source": "qlib_binary_close",
                },
                "rows": rows,
            },
        )
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def _parse_fields(raw_fields: str | list[str] | None, columns: list[str]) -> list[str]:
    if isinstance(raw_fields, str):
        candidates = [part.strip() for part in raw_fields.split(",") if part.strip()]
    elif isinstance(raw_fields, list):
        candidates = [str(part).strip() for part in raw_fields if str(part).strip()]
    else:
        candidates = []
    if not candidates:
        candidates = DATA_QUERY_DEFAULT_FIELDS
    invalid = [field for field in candidates if field not in set(columns)]
    if invalid:
        raise ValueError(f"invalid_fields:{','.join(invalid)}")
    selected: list[str] = []
    for field in candidates:
        if field not in selected:
            selected.append(field)
    return selected[:DATA_QUERY_MAX_FIELDS]


def _read_code_frame(code: str, columns: list[str]):
    import pandas as pd

    available = set(_hdf_columns())
    select_columns = [col for col in ["code", "kline_time", *columns] if col in available]
    with pd.HDFStore(PRODUCTION_RAW_HDF5, mode="r") as store:
        return store.select("/daily", where=f"code == {repr(code)}", columns=select_columns)


def _filter_dates(df, start: str | None, end: str | None):
    if df.empty:
        return df
    result = df.copy()
    result["kline_time"] = result["kline_time"].astype(str).str.slice(0, 10)
    if start:
        result = result[result["kline_time"] >= start]
    if end:
        result = result[result["kline_time"] <= end]
    return result.sort_values("kline_time")


def _transform_values(values: list[Any], transform: str) -> list[float | None]:
    numeric: list[float | None] = []
    for value in values:
        try:
            number = float(value)
            numeric.append(number if number == number else None)
        except Exception:
            numeric.append(None)
    if transform == "raw":
        return numeric
    if transform == "pct_change":
        output: list[float | None] = []
        previous: float | None = None
        for number in numeric:
            if number is None or previous in (None, 0):
                output.append(None)
            else:
                output.append((number / previous - 1) * 100)
            if number is not None:
                previous = number
        return output
    valid = [value for value in numeric if value is not None]
    if not valid:
        return [None for _ in numeric]
    if transform == "index100":
        base = next((value for value in numeric if value not in (None, 0)), None)
        return [(value / base * 100) if value is not None and base else None for value in numeric]
    if transform == "zscore":
        mean = sum(valid) / len(valid)
        variance = sum((value - mean) ** 2 for value in valid) / len(valid)
        std = variance ** 0.5
        return [((value - mean) / std) if value is not None and std else None for value in numeric]
    raise ValueError(f"invalid_transform:{transform}")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "item"):
        value = value.item()
    if value != value:
        return None
    return value


def _frame_rows(df, fields: list[str]) -> list[dict[str, Any]]:
    rows = []
    for row in df.to_dict(orient="records"):
        item = {"date": str(row.get("kline_time", ""))[:10], "code": row.get("code")}
        for field in fields:
            item[field] = _jsonable(row.get(field))
        rows.append(item)
    return rows


def _missing_rates(df, fields: list[str]) -> dict[str, float]:
    total = max(1, len(df))
    return {field: float(df[field].isna().sum()) / total if field in df.columns else 1.0 for field in fields}


def data_query(
    *,
    code: str,
    start: str | None = None,
    end: str | None = None,
    fields: str | list[str] | None = None,
    benchmark: str | None = None,
    transform: str = "zscore",
) -> ServiceResult:
    inputs = {"code": code, "start": start, "end": end, "fields": fields, "benchmark": benchmark, "transform": transform}
    try:
        columns = _hdf_columns()
        selected_fields = _parse_fields(fields, columns)
        required = list(dict.fromkeys([*selected_fields, "open", "high", "low", "close", "SECURITY_NAME", "list_status", "st_status"]))
        required = [field for field in required if field in columns]
        normalized = _resolve_query_code(code)
        df = _filter_dates(_read_code_frame(normalized, required), start, end)
        if len(df) > DATA_QUERY_MAX_ROWS:
            df = df.tail(DATA_QUERY_MAX_ROWS)
        benchmark_rows: list[dict[str, Any]] = []
        if benchmark:
            bench_code = _normalize_code(benchmark)
            bench_fields = [field for field in ["close", "adj_close"] if field in columns]
            if bench_fields:
                bench_df = _filter_dates(_read_code_frame(bench_code, bench_fields), start, end)
                benchmark_rows = _frame_rows(bench_df, bench_fields)
        row_fields = list(dict.fromkeys([*required, *selected_fields]))
        rows = _frame_rows(df, row_fields)
        numeric_fields = [
            field for field in selected_fields
            if field in df.columns
            and str(df[field].dtype) != "object"
            and field not in {"open", "high", "low", "close", "list_status", "st_status"}
        ]
        series = []
        for field in numeric_fields:
            values = df[field].tolist()
            series.append(
                {
                    "field": field,
                    "kind": "line",
                    "transform": transform,
                    "points": [
                        {"date": str(date)[:10], "value": value}
                        for date, value in zip(df["kline_time"].tolist(), _transform_values(values, transform))
                    ],
                }
            )
        if benchmark_rows:
            bench_values = [row.get("close") for row in benchmark_rows]
            series.append(
                {
                    "field": "benchmark_close",
                    "label": _normalize_code(benchmark),
                    "kind": "benchmark",
                    "transform": transform,
                    "points": [
                        {"date": row["date"], "value": value}
                        for row, value in zip(benchmark_rows, _transform_values(bench_values, transform))
                    ],
                }
            )
        metadata = {
            "code": normalized,
            "row_count": len(df),
            "start": str(df["kline_time"].iloc[0])[:10] if not df.empty else None,
            "end": str(df["kline_time"].iloc[-1])[:10] if not df.empty else None,
            "security_name": str(df["SECURITY_NAME"].dropna().iloc[-1]) if "SECURITY_NAME" in df.columns and not df["SECURITY_NAME"].dropna().empty else "",
            "latest_list_status": str(df["list_status"].dropna().iloc[-1]) if "list_status" in df.columns and not df["list_status"].dropna().empty else "",
            "latest_st_status": str(df["st_status"].dropna().iloc[-1]) if "st_status" in df.columns and not df["st_status"].dropna().empty else "",
        }
        return ok_result(
            inputs=inputs,
            outputs={
                "metadata": metadata,
                "fields": selected_fields,
                "transform": transform,
                "missing_rate": _missing_rates(df, selected_fields) if not df.empty else {field: 1.0 for field in selected_fields},
                "rows": rows,
                "chart_series": series,
                "benchmark_rows": benchmark_rows,
            },
        )
    except Exception as e:
        return err_result(str(e), inputs=inputs)


def stock_metadata_status() -> ServiceResult:
    try:
        return ok_result(outputs=stock_identity_cache_status())
    except Exception as e:
        return err_result(str(e))


def stock_metadata_refresh(force: bool = False) -> ServiceResult:
    try:
        return ok_result(inputs={"force": force}, outputs=build_stock_identity_cache(force=force))
    except Exception as e:
        return err_result(str(e), inputs={"force": force})


def data_daily_preflight(target_date: str | None = None) -> ServiceResult:
    try:
        result = run_data_daily_preflight(target_date)
        return ok_result(inputs={"target_date": target_date}, outputs=result, warnings=result.get("warnings", []))
    except Exception as e:
        return err_result(str(e), inputs={"target_date": target_date})


def data_stage_update(target_date: str | None = None, dry_run: bool = False) -> ServiceResult:
    try:
        result = run_data_stage_update(target_date, dry_run=dry_run)
        ok = result.get("status") in {"completed", "dry_run"}
        return (ok_result if ok else err_result)(
            *(["data_stage_update_failed"] if not ok else []),
            inputs={"target_date": target_date, "dry_run": dry_run},
            outputs=result,
        )
    except Exception as e:
        return err_result(str(e), inputs={"target_date": target_date, "dry_run": dry_run})


def data_promote_staged(
    *,
    package_id: str | None = None,
    latest: bool = False,
    wait_idle: bool = False,
    timeout_minutes: int = 180,
    dry_run: bool = False,
) -> ServiceResult:
    inputs = {
        "package_id": package_id,
        "latest": latest,
        "wait_idle": wait_idle,
        "timeout_minutes": timeout_minutes,
        "dry_run": dry_run,
    }
    try:
        result = run_data_promote_staged(
            package_id=package_id,
            latest=latest,
            wait_idle=wait_idle,
            timeout_minutes=timeout_minutes,
            dry_run=dry_run,
        )
        ok = result.get("status") in {"promoted", "dry_run", "already_promoted"}
        return (ok_result if ok else err_result)(
            *(["data_promote_staged_failed"] if not ok else []),
            inputs=inputs,
            outputs=result,
            warnings=result.get("blockers", []),
        )
    except Exception as e:
        return err_result(str(e), inputs=inputs)


def data_daily_routine(
    *,
    target_date: str | None = "auto",
    wait_idle: bool = True,
    timeout_minutes: int = 180,
    dry_run: bool = False,
) -> ServiceResult:
    inputs = {
        "target_date": target_date,
        "wait_idle": wait_idle,
        "timeout_minutes": timeout_minutes,
        "dry_run": dry_run,
    }
    try:
        result = run_data_daily_routine(
            target_date=target_date,
            wait_idle=wait_idle,
            timeout_minutes=timeout_minutes,
            dry_run=dry_run,
        )
        ok = result.get("status") in {"completed", "dry_run", "already_current"}
        return (ok_result if ok else err_result)(
            *(["data_daily_routine_incomplete"] if not ok else []),
            inputs=inputs,
            outputs=result,
        )
    except Exception as e:
        return err_result(str(e), inputs=inputs)


def data_production_audit(
    *,
    replace_from_date: str | None = None,
    full_scan: bool = False,
    deep_sample_count: int = 0,
    write_report: bool = False,
) -> ServiceResult:
    inputs = {
        "replace_from_date": replace_from_date,
        "full_scan": full_scan,
        "deep_sample_count": deep_sample_count,
        "write_report": write_report,
    }
    try:
        result = production_audit_summary(
            replace_from_date=replace_from_date,
            full_scan=full_scan,
            deep_sample_count=deep_sample_count,
            write_report=write_report,
        )
        record_production_audit_result(result)
        ok = result.get("status") == "passed"
        return (ok_result if ok else err_result)(
            *(["data_production_audit_failed"] if not ok else []),
            inputs=inputs,
            outputs=result,
            warnings=result.get("issues", []),
        )
    except Exception as e:
        return err_result(str(e), inputs=inputs)


def data_tushare_preflight(
    *,
    start_date: str = "20180101",
    cutoff_date: str = "20260602",
    pad_trading_days: int = 120,
    max_trade_days: int | None = None,
    max_codes: int | None = None,
    proxy_mode: str = "direct",
) -> ServiceResult:
    inputs = {
        "start_date": start_date,
        "cutoff_date": cutoff_date,
        "pad_trading_days": pad_trading_days,
        "max_trade_days": max_trade_days,
        "max_codes": max_codes,
        "proxy_mode": proxy_mode,
    }
    try:
        result = tushare_preflight(
            start_date=start_date,
            cutoff_date=cutoff_date,
            pad_trading_days=pad_trading_days,
            max_trade_days=max_trade_days,
            max_codes=max_codes,
            proxy_mode=proxy_mode,
        )
        return ok_result(inputs=inputs, outputs=result)
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def data_tushare_network(*, verify_http: bool = True, repair_routes: bool = False) -> ServiceResult:
    inputs = {"verify_http": verify_http, "repair_routes": repair_routes}
    try:
        result = tushare_network_preflight(verify_http=verify_http, repair_routes=repair_routes)
        ok = result.get("status") == "ok"
        return (ok_result if ok else err_result)(
            *(["data_tushare_network_failed"] if not ok else []),
            inputs=inputs,
            outputs=result,
        )
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def data_tushare_full_rebuild(
    *,
    start_date: str = "20180101",
    cutoff_date: str = "20260602",
    pad_trading_days: int = 120,
    package_id: str | None = None,
    resume: bool = True,
    dry_run: bool = False,
    max_trade_days: int | None = None,
    max_codes: int | None = None,
    proxy_mode: str = "direct",
    trade_date_chunk_size: int = 40,
) -> ServiceResult:
    inputs = {
        "start_date": start_date,
        "cutoff_date": cutoff_date,
        "pad_trading_days": pad_trading_days,
        "package_id": package_id,
        "resume": resume,
        "dry_run": dry_run,
        "max_trade_days": max_trade_days,
        "max_codes": max_codes,
        "proxy_mode": proxy_mode,
        "trade_date_chunk_size": trade_date_chunk_size,
    }
    try:
        result = tushare_full_rebuild(
            TushareRebuildConfig(
                start_date=start_date,
                cutoff_date=cutoff_date,
                pad_trading_days=pad_trading_days,
                package_id=package_id,
                resume=resume,
                dry_run=dry_run,
                max_trade_days=max_trade_days,
                max_codes=max_codes,
                proxy_mode=proxy_mode,
                trade_date_chunk_size=trade_date_chunk_size,
            )
        )
        ok = result.get("status") in {"completed", "dry_run"}
        return (ok_result if ok else err_result)(
            *(["data_tushare_full_rebuild_failed"] if not ok else []),
            inputs=inputs,
            outputs=result,
        )
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def data_tushare_prepare_production(
    *,
    package_id: str | None = None,
    latest: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> ServiceResult:
    inputs = {
        "package_id": package_id,
        "latest": latest,
        "force": force,
        "dry_run": dry_run,
    }
    try:
        result = prepare_tushare_production_artifacts(
            package_id=package_id,
            latest=latest,
            force=force,
            dry_run=dry_run,
        )
        ok = result.get("status") in {"completed", "dry_run"}
        return (ok_result if ok else err_result)(
            *(["data_tushare_prepare_production_failed"] if not ok else []),
            inputs=inputs,
            outputs=result,
        )
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def data_tushare_status_backfill(
    *,
    package_id: str | None = None,
    proxy_mode: str = "direct",
    fetch_live: bool = True,
) -> ServiceResult:
    inputs = {"package_id": package_id, "proxy_mode": proxy_mode, "fetch_live": fetch_live}
    try:
        result = build_tushare_status_backfill(package_id=package_id, proxy_mode=proxy_mode, fetch_live=fetch_live)
        ok = result.get("status") == "completed"
        return (ok_result if ok else err_result)(
            *(["data_tushare_status_backfill_failed"] if not ok else []),
            inputs=inputs,
            outputs=result,
        )
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def data_tushare_limit_backfill(
    *,
    package_id: str | None = None,
    proxy_mode: str = "direct",
    fetch_live: bool = True,
) -> ServiceResult:
    inputs = {"package_id": package_id, "proxy_mode": proxy_mode, "fetch_live": fetch_live}
    try:
        result = build_tushare_limit_backfill(package_id=package_id, proxy_mode=proxy_mode, fetch_live=fetch_live)
        ok = result.get("status") == "completed"
        return (ok_result if ok else err_result)(
            *(["data_tushare_limit_backfill_failed"] if not ok else []),
            inputs=inputs,
            outputs=result,
        )
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def data_tushare_promote_staged(
    *,
    package_id: str | None = None,
    latest: bool = False,
    dry_run: bool = False,
) -> ServiceResult:
    inputs = {
        "package_id": package_id,
        "latest": latest,
        "dry_run": dry_run,
    }
    try:
        result = promote_tushare_production_artifacts(
            package_id=package_id,
            latest=latest,
            dry_run=dry_run,
        )
        ok = result.get("status") in {"promoted", "dry_run"}
        return (ok_result if ok else err_result)(
            *(["data_tushare_promote_staged_failed"] if not ok else []),
            inputs=inputs,
            outputs=result,
        )
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def data_tushare_full_rebuild_status(*, package_id: str | None = None, latest: bool = True) -> ServiceResult:
    inputs = {"package_id": package_id, "latest": latest}
    try:
        result = tushare_full_rebuild_status(package_id=package_id, latest=latest)
        return ok_result(inputs=inputs, outputs=result)
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)
