from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from domain.factor_research.active_values_store import (
    active_values_store_summary,
    build_active_values_store,
    current_active_registry_fingerprint,
)
from domain.factor_research.active_values_tail_refresh import refresh_active_values_tail
from domain.runtime_memory import release_process_memory
from services._base import err_result, ok_result
from storage.paths import FACTOR_DEFAULT_HOLDING_PERIOD, RUNTIME_ROOT


ACTIVE_VALUES_REFRESH_STATUS_FILE = RUNTIME_ROOT / "factor_active_values" / "latest_status.json"
ACTIVE_VALUES_REFRESH_JOBS_DB = RUNTIME_ROOT / "factor_active_values" / "jobs.sqlite"

_REFRESH_LOCK = threading.Lock()
_REFRESH_THREAD: threading.Thread | None = None
_REFRESH_STATE: dict[str, Any] = {
    "status": "idle",
    "last_error": "",
    "last_requested_at": "",
    "last_started_at": "",
    "last_finished_at": "",
    "registry_fingerprint": "",
    "model_refresh": {},
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_jobs_db() -> None:
    ACTIVE_VALUES_REFRESH_JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ACTIVE_VALUES_REFRESH_JOBS_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_values_refresh_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                trigger TEXT,
                source_mode TEXT,
                refresh_model INTEGER,
                holding_period_days INTEGER,
                requested_registry_fingerprint TEXT,
                built_registry_fingerprint TEXT,
                created_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                last_error TEXT,
                payload_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_active_values_refresh_jobs_created "
            "ON active_values_refresh_jobs(created_at DESC)"
        )


def _refresh_job_id(*, trigger: str, registry_fingerprint: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    short_fp = (registry_fingerprint or "nofp")[:12]
    clean_trigger = "".join(ch if ch.isalnum() else "_" for ch in str(trigger or "manual")).strip("_")[:24] or "manual"
    return f"avj_{stamp}_{clean_trigger}_{short_fp}"


def _row_to_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    result = dict(row)
    try:
        result["payload"] = json.loads(result.pop("payload_json") or "{}")
    except Exception:
        result["payload"] = {}
    result["refresh_model"] = bool(result.get("refresh_model"))
    return result


def _write_refresh_job(job_id: str, **updates: Any) -> dict[str, Any]:
    _ensure_jobs_db()
    now = _now()
    payload_updates = dict(updates.pop("payload", {}) or {})
    with sqlite3.connect(ACTIVE_VALUES_REFRESH_JOBS_DB) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT * FROM active_values_refresh_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if existing:
            current = dict(existing)
            try:
                current_payload = json.loads(current.get("payload_json") or "{}")
            except Exception:
                current_payload = {}
            fields = {
                key: current.get(key)
                for key in current.keys()
                if key not in {"job_id", "payload_json"}
            }
            fields.update(updates)
            fields["payload_json"] = json.dumps({**current_payload, **payload_updates}, ensure_ascii=False, default=str)
            conn.execute(
                "UPDATE active_values_refresh_jobs SET "
                + ", ".join(f"{key} = ?" for key in fields)
                + " WHERE job_id = ?",
                [fields[key] for key in fields] + [job_id],
            )
        else:
            fields = {
                "job_id": job_id,
                "status": str(updates.get("status") or "queued"),
                "trigger": str(updates.get("trigger") or ""),
                "source_mode": str(updates.get("source_mode") or "parquet"),
                "refresh_model": 1 if updates.get("refresh_model") else 0,
                "holding_period_days": updates.get("holding_period_days"),
                "requested_registry_fingerprint": str(updates.get("requested_registry_fingerprint") or ""),
                "built_registry_fingerprint": str(updates.get("built_registry_fingerprint") or ""),
                "created_at": str(updates.get("created_at") or now),
                "started_at": str(updates.get("started_at") or ""),
                "finished_at": str(updates.get("finished_at") or ""),
                "last_error": str(updates.get("last_error") or ""),
                "payload_json": json.dumps(payload_updates, ensure_ascii=False, default=str),
            }
            conn.execute(
                """
                INSERT INTO active_values_refresh_jobs (
                    job_id, status, trigger, source_mode, refresh_model, holding_period_days,
                    requested_registry_fingerprint, built_registry_fingerprint,
                    created_at, started_at, finished_at, last_error, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [fields[key] for key in (
                    "job_id",
                    "status",
                    "trigger",
                    "source_mode",
                    "refresh_model",
                    "holding_period_days",
                    "requested_registry_fingerprint",
                    "built_registry_fingerprint",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "last_error",
                    "payload_json",
                )],
            )
        row = conn.execute(
            "SELECT * FROM active_values_refresh_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return _row_to_job(row) or {"job_id": job_id, **updates}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _duration_seconds(start: str | None, end: str | None = None) -> float | None:
    started = _parse_dt(start)
    if not started:
        return None
    finished = _parse_dt(end) or datetime.now()
    return max(0.0, (finished - started).total_seconds())


def _latest_refresh_job() -> dict[str, Any] | None:
    if not ACTIVE_VALUES_REFRESH_JOBS_DB.exists():
        return None
    _ensure_jobs_db()
    with sqlite3.connect(ACTIVE_VALUES_REFRESH_JOBS_DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM active_values_refresh_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return _row_to_job(row)


def _write_state(state: dict[str, Any]) -> None:
    ACTIVE_VALUES_REFRESH_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, default=str)
    tmp_path = ACTIVE_VALUES_REFRESH_STATUS_FILE.with_name(
        f"{ACTIVE_VALUES_REFRESH_STATUS_FILE.name}.tmp.{threading.get_ident()}"
    )
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(ACTIVE_VALUES_REFRESH_STATUS_FILE)


def _load_state() -> dict[str, Any]:
    if ACTIVE_VALUES_REFRESH_STATUS_FILE.exists():
        try:
            disk = json.loads(ACTIVE_VALUES_REFRESH_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            disk = {}
    else:
        disk = {}
    with _REFRESH_LOCK:
        running = bool(_REFRESH_THREAD and _REFRESH_THREAD.is_alive())
        state = {**_REFRESH_STATE, **disk}
        if running:
            state.update({k: v for k, v in _REFRESH_STATE.items() if v})
            state["status"] = "running"
        state["_thread_alive"] = running
        return state


def _set_state(**updates: Any) -> dict[str, Any]:
    with _REFRESH_LOCK:
        _REFRESH_STATE.update(updates)
        state = dict(_REFRESH_STATE)
    _write_state(state)
    return state


def _run_refresh(
    *,
    job_id: str | None = None,
    holding_period_days: int | None,
    trigger: str,
    refresh_model: bool,
    registry_fingerprint: str,
    source_mode: str,
) -> None:
    job_id = job_id or _refresh_job_id(trigger=trigger, registry_fingerprint=registry_fingerprint)
    _write_refresh_job(
        job_id,
        status="running",
        trigger=trigger,
        source_mode=source_mode,
        refresh_model=refresh_model,
        holding_period_days=holding_period_days,
        requested_registry_fingerprint=registry_fingerprint,
        started_at=_now(),
        last_error="",
    )
    _set_state(
        job_id=job_id,
        status="running",
        trigger=trigger,
        source_mode=source_mode,
        last_error="",
        last_started_at=_now(),
        registry_fingerprint=registry_fingerprint,
        requested_registry_fingerprint=registry_fingerprint,
        active_values_refresh_required=True,
        model_refresh_required=bool(refresh_model),
        model_snapshot_refresh_required=bool(refresh_model),
    )
    try:
        if source_mode in {"tail", "compute_tail"}:
            tail_result = refresh_active_values_tail(holding_period_days=holding_period_days, run_id=job_id)
            manifest = tail_result.get("active_values_manifest") or {}
        else:
            manifest = build_active_values_store(holding_period_days=holding_period_days, source_mode=source_mode)
        latest_fingerprint, _ = current_active_registry_fingerprint(holding_period_days=holding_period_days)
        if latest_fingerprint and latest_fingerprint != str(manifest.get("registry_fingerprint") or ""):
            if source_mode in {"tail", "compute_tail"}:
                tail_result = refresh_active_values_tail(holding_period_days=holding_period_days, run_id=job_id)
                manifest = tail_result.get("active_values_manifest") or {}
            else:
                manifest = build_active_values_store(holding_period_days=holding_period_days, source_mode=source_mode)
        model_payload: dict[str, Any] = {"status": "skipped"}
        if refresh_model:
            model_payload = {
                "ok": True,
                "status": "refresh_required",
                "reason": "active_values_refreshed_without_freezing_model_feature_set",
                "responsibility_boundary": "model_feature_snapshot_must_freeze_after_active_values_ready",
                "registry_fingerprint": str(manifest.get("registry_fingerprint") or registry_fingerprint),
            }
        model_ok = True
        _set_state(
            job_id=job_id,
            status="completed" if model_ok else "model_refresh_failed",
            active_values_refresh_required=False,
            model_refresh_required=bool(refresh_model),
            model_snapshot_refresh_required=bool(refresh_model),
            last_finished_at=_now(),
            last_error="" if model_ok else str(model_payload.get("err") or "model_refresh_failed"),
            active_values_manifest=manifest,
            requested_registry_fingerprint=str(manifest.get("registry_fingerprint") or registry_fingerprint),
            model_refresh=model_payload,
            registry_fingerprint=str(manifest.get("registry_fingerprint") or registry_fingerprint),
            built_registry_fingerprint=str(manifest.get("registry_fingerprint") or registry_fingerprint),
            source_mode=source_mode,
        )
        _write_refresh_job(
            job_id,
            status="completed" if model_ok else "model_refresh_failed",
            requested_registry_fingerprint=str(manifest.get("registry_fingerprint") or registry_fingerprint),
            built_registry_fingerprint=str(manifest.get("registry_fingerprint") or registry_fingerprint),
            finished_at=_now(),
            last_error="" if model_ok else str(model_payload.get("err") or "model_refresh_failed"),
            payload={"active_values_manifest": manifest, "model_refresh": model_payload},
        )
    except Exception as exc:
        _set_state(
            job_id=job_id,
            status="active_values_refresh_failed",
            active_values_refresh_required=True,
            model_refresh_required=bool(refresh_model),
            model_snapshot_refresh_required=bool(refresh_model),
            last_finished_at=_now(),
            last_error=str(exc),
        )
        _write_refresh_job(
            job_id,
            status="failed",
            finished_at=_now(),
            last_error=str(exc),
        )
    finally:
        release = release_process_memory("active_values_refresh_finished")
        with _REFRESH_LOCK:
            _REFRESH_STATE["last_memory_release"] = release
            state = dict(_REFRESH_STATE)
            state["last_memory_release"] = release
        try:
            _write_state(state)
        except Exception:
            pass


def enqueue_active_values_refresh(
    *,
    holding_period_days: int | None = FACTOR_DEFAULT_HOLDING_PERIOD,
    trigger: str = "manual",
    refresh_model: bool = True,
    dry_run: bool = False,
    source_mode: str = "tail",
) -> dict[str, Any]:
    source_mode = str(source_mode or "tail").strip().lower()
    if source_mode not in {"parquet", "tail", "compute_tail", "compute"}:
        return {
            "status": "invalid_source_mode",
            "ok": False,
            "source_mode": source_mode,
            "supported_source_modes": ["parquet", "tail", "compute_tail", "compute"],
            "active_values_refresh_required": True,
            "model_refresh_required": bool(refresh_model),
            "model_snapshot_refresh_required": bool(refresh_model),
        }
    registry_fingerprint, _ = current_active_registry_fingerprint(holding_period_days=holding_period_days)
    if dry_run:
        summary = active_values_store_summary(holding_period_days=holding_period_days)
        return {
            "status": "dry_run",
            "trigger": trigger,
            "dry_run": True,
            "would_queue": bool(summary.get("stale")),
            "active_values_refresh_required": bool(summary.get("stale")),
            "model_refresh_required": bool(refresh_model and summary.get("stale")),
            "model_snapshot_refresh_required": bool(refresh_model and summary.get("stale")),
            "model_snapshot_trigger": "model_side",
            "requested_registry_fingerprint": registry_fingerprint,
            "source_mode": source_mode,
            "responsibility_boundary": "active_values_refresh_assembles_factor_store_values; model_only_checks_and_freezes_feature_sets",
            "summary": summary,
        }
    with _REFRESH_LOCK:
        global _REFRESH_THREAD
        running = bool(_REFRESH_THREAD and _REFRESH_THREAD.is_alive())
        latest_job = _latest_refresh_job()
        if running:
            state = dict(_REFRESH_STATE)
            state["status"] = "running"
            state["active_values_job"] = latest_job or {}
            _write_state(state)
            return state
        job_id = _refresh_job_id(trigger=trigger, registry_fingerprint=registry_fingerprint)
        _write_refresh_job(
            job_id,
            status="queued",
            trigger=trigger,
            source_mode=source_mode,
            refresh_model=refresh_model,
            holding_period_days=holding_period_days,
            requested_registry_fingerprint=registry_fingerprint,
            created_at=_now(),
            payload={
                "active_values_refresh_required": True,
                "model_refresh_required": bool(refresh_model),
                "model_snapshot_refresh_required": bool(refresh_model),
                "responsibility_boundary": "active_values_refresh_assembles_factor_store_values; model_only_checks_and_freezes_feature_sets",
            },
        )
        _REFRESH_STATE.update(
            {
                "job_id": job_id,
                "last_requested_at": _now(),
                "requested_trigger": trigger,
                "requested_registry_fingerprint": registry_fingerprint,
                "source_mode": source_mode,
                "active_values_refresh_required": True,
                "model_refresh_required": bool(refresh_model),
                "model_snapshot_refresh_required": bool(refresh_model),
            }
        )
        _REFRESH_THREAD = threading.Thread(
            target=_run_refresh,
            kwargs={
                "job_id": job_id,
                "holding_period_days": holding_period_days,
                "trigger": trigger,
                "refresh_model": refresh_model,
                "registry_fingerprint": registry_fingerprint,
                "source_mode": source_mode,
            },
            name="fxalpha-active-values-refresh",
            daemon=True,
        )
        _REFRESH_THREAD.start()
        state = dict(_REFRESH_STATE)
        state["status"] = "queued"
        state["active_values_job"] = _latest_refresh_job() or {}
    _write_state(state)
    return state


def factor_active_values_status(*, holding_period_days: int | None = FACTOR_DEFAULT_HOLDING_PERIOD) -> Any:
    try:
        summary = active_values_store_summary(holding_period_days=holding_period_days)
        state = _load_state()
        latest_job = _latest_refresh_job()
        if state.get("last_error") and summary.get("stale"):
            summary["last_error"] = state.get("last_error")
        refresh_status = (latest_job or {}).get("status") or state.get("status", "idle")
        active_values_required = bool(summary.get("stale") or state.get("active_values_refresh_required"))
        if (
            refresh_status in {"queued", "running"}
            and not state.get("_thread_alive")
            and not summary.get("stale")
        ):
            refresh_status = "completed_after_restart"
            active_values_required = False
        elif refresh_status in {"queued", "running"} and not state.get("_thread_alive"):
            refresh_status = "interrupted"
            active_values_required = True
            if latest_job:
                latest_job = {**latest_job, "status": "interrupted", "resume_available": True}
        model_snapshot_marked = bool(state.get("model_snapshot_refresh_required") or state.get("model_refresh_required"))
        model_snapshot_currently_stale = model_snapshot_marked
        model_snapshot_staleness: dict[str, Any] = {}
        try:
            from domain.model.feature_set_builder import active_feature_snapshot_staleness

            try:
                model_snapshot_staleness = active_feature_snapshot_staleness(
                    holding_period_days=holding_period_days or FACTOR_DEFAULT_HOLDING_PERIOD,
                    # The staleness response embeds its input summary for audit
                    # evidence. Pass a shallow copy so adding that response back
                    # to ``summary`` cannot create a self-referential structure.
                    active_values_summary=dict(summary),
                )
            except TypeError as exc:
                if "active_values_summary" not in str(exc):
                    raise
                # Compatibility with older extensions that implement the prior
                # one-argument hook. Current production code uses the fast path.
                model_snapshot_staleness = active_feature_snapshot_staleness(
                    holding_period_days=holding_period_days or FACTOR_DEFAULT_HOLDING_PERIOD,
                )
            model_snapshot_currently_stale = bool(model_snapshot_staleness.get("stale"))
        except Exception as exc:
            model_snapshot_staleness = {"status": "unavailable", "reason": str(exc)}
        summary.update(
            {
                "refresh_status": refresh_status,
                "job_id": (latest_job or {}).get("job_id") or state.get("job_id", ""),
                "active_values_refresh_required": active_values_required,
                "active_values_status": "ready" if not active_values_required else ("refreshing" if refresh_status in {"queued", "running"} else "stale"),
                "safe_to_freeze_feature_set": not active_values_required,
                "feature_snapshot_blocked": active_values_required,
                "feature_snapshot_block_reason": "" if not active_values_required else (summary.get("stale_message") or summary.get("stale_reason") or refresh_status or "active_values_not_ready"),
                "required_action": "none" if not active_values_required else "refresh_active_values_from_factor_store",
                "refresh_source_mode_default": "tail",
                "model_computes_factor_values": False,
                "responsibility_boundary": "factor_import/build_refresh owns factor values; model only checks active values readiness and freezes immutable feature sets",
                "model_refresh_required": model_snapshot_currently_stale,
                "model_snapshot_refresh_required": model_snapshot_currently_stale,
                "model_snapshot_refresh_marked": model_snapshot_marked,
                "model_snapshot_currently_stale": model_snapshot_currently_stale,
                "model_snapshot_staleness": model_snapshot_staleness,
                "model_snapshot_trigger": "model_side",
                "last_requested_at": state.get("last_requested_at", ""),
                "last_started_at": (latest_job or {}).get("started_at") or state.get("last_started_at", ""),
                "last_finished_at": (latest_job or {}).get("finished_at") or state.get("last_finished_at", ""),
                "requested_registry_fingerprint": (latest_job or {}).get("requested_registry_fingerprint") or state.get("requested_registry_fingerprint", ""),
                "built_registry_fingerprint": (latest_job or {}).get("built_registry_fingerprint") or state.get("built_registry_fingerprint") or summary.get("manifest_registry_fingerprint") or "",
                "current_registry_fingerprint": summary.get("registry_fingerprint") or "",
                "source_mode": (latest_job or {}).get("source_mode") or state.get("source_mode") or "tail",
                "factor_count": summary.get("factor_count") or summary.get("active_count"),
                "final_manifest_path": summary.get("manifest_path"),
                "duration_seconds": _duration_seconds((latest_job or {}).get("started_at") or state.get("last_started_at", ""), (latest_job or {}).get("finished_at") or state.get("last_finished_at", "")),
                "progress": {
                    "status": refresh_status,
                    "job_id": (latest_job or {}).get("job_id") or state.get("job_id", ""),
                    "factor_count": summary.get("factor_count") or summary.get("active_count"),
                    "duration_seconds": _duration_seconds((latest_job or {}).get("started_at") or state.get("last_started_at", ""), (latest_job or {}).get("finished_at") or state.get("last_finished_at", "")),
                    "manifest_path": summary.get("manifest_path"),
                },
                "active_values_job": latest_job or {},
                "resume_available": bool((latest_job or {}).get("resume_available")),
                "resume_action": "POST /factor/active-values/refresh source_mode=tail refresh_model=false" if bool((latest_job or {}).get("resume_available")) else "",
                "state_file": str(ACTIVE_VALUES_REFRESH_STATUS_FILE),
                "jobs_db": str(ACTIVE_VALUES_REFRESH_JOBS_DB),
            }
        )
        return ok_result(outputs=summary, artifacts={"state_file": str(ACTIVE_VALUES_REFRESH_STATUS_FILE), "jobs_db": str(ACTIVE_VALUES_REFRESH_JOBS_DB)})
    except Exception as exc:
        return err_result(str(exc), artifacts={"state_file": str(ACTIVE_VALUES_REFRESH_STATUS_FILE), "jobs_db": str(ACTIVE_VALUES_REFRESH_JOBS_DB)})
