from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import error, request

from domain.platform_ops.service_health import http_json_health
from services._base import err_result, ok_result
from storage.paths import LLM_API_KEY, PROJECT_ROOT, QUANTGPT_API_URL

_PROCESS_STARTED_AT = time.time()
_USAGE_STATUS_FILE = PROJECT_ROOT / "runtime" / "platform" / "runtime_usage_status.json"
_CODEX_USAGE_SNAPSHOT_FILE = PROJECT_ROOT / "runtime" / "platform" / "codex_usage_snapshot.json"
_CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
_CODEX_LOGS_DB = Path(
    os.environ.get("FXALPHA_CODEX_LOGS_DB", str(_CODEX_HOME / "logs_2.sqlite"))
).expanduser()
_CODEX_STATE_DB = Path(
    os.environ.get("FXALPHA_CODEX_STATE_DB", str(_CODEX_HOME / "state_5.sqlite"))
).expanduser()
_DEEPSEEK_TRACE_FILE = PROJECT_ROOT / "runtime" / "factor_research" / "orchestrator_llm_traces" / "current.jsonl"
_DEEPSEEK_TRACE_ROOT = _DEEPSEEK_TRACE_FILE.parent
_DEEPSEEK_MODEL_TRACE_ROOT = PROJECT_ROOT / "runtime" / "model" / "orchestrator_traces"
_DEEPSEEK_BALANCE_HISTORY_FILE = PROJECT_ROOT / "runtime" / "platform" / "deepseek_balance_history.json"
_DEEPSEEK_BALANCE_HISTORY_RETENTION = timedelta(days=8)
_CODEX_TOKEN_TS_RE = re.compile(r'"timestamp":"([^"]+)"')
_CODEX_TOKEN_TOTAL_RE = re.compile(r'"last_token_usage":\{.*?"total_tokens":(\d+)', re.ASCII)
_CODEX_SESSION_ID_RE = re.compile(r'"session_id":"([^"]+)"')
_CODEX_SESSION_CACHE_TTL_SECONDS = 300
_CODEX_SESSION_WINDOWS_CACHE: dict[str, Any] = {"signature": None, "computed_at": 0.0, "value": {}}
_USAGE_CACHE_TTL_SECONDS = 60.0
_USAGE_CACHE_LOCK = threading.Lock()
_USAGE_CACHE: dict[str, Any] = {"computed_at": 0.0, "value": None, "warnings": []}
_TRACE_TAIL_BYTES = 16 * 1024 * 1024
_TRACE_TAIL_MAX_LINES = 1200
_AUTOMATION_CONTROL_LOCK = threading.Lock()
_AUTOMATION_AUDIT_FILE = PROJECT_ROOT / "runtime" / "platform" / "automation_control_audit.jsonl"
_AUTOMATION_WORKFLOWS = {
    "data_foundation": {
        "service_unit": "fxalpha-data-daily.service",
        "timer_unit": "fxalpha-data-daily.timer",
        "default_time": "02:00",
        "schedule_prefix": "周二至周六",
        "schedule_suffix": "（启动后补检）",
    },
    "paper_trading": {
        "service_unit": "fxalpha-paper-fleet-daily.service",
        "timer_unit": "fxalpha-paper-fleet-daily.timer",
        "default_time": "07:30",
        "schedule_prefix": "周二至周六",
        "schedule_suffix": "（数据日更成功后立即运行，定时器用于兜底补检）",
    },
}
_AUTOMATION_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_USAGE_ALLOWED_KEYS = {
    "configured",
    "remaining",
    "used_tokens",
    "limit_tokens",
    "window",
    "updated_at",
    "requests",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost",
    "currency",
    "note",
    "status",
}
_CODEX_OFFICIAL_ALLOWED_KEYS = {
    "configured",
    "source",
    "remaining",
    "used_tokens",
    "limit_tokens",
    "window",
    "updated_at",
    "status",
    "note",
    "unauthorized",
}
_DEEPSEEK_TRACE_ALLOWED_KEYS = {
    "configured",
    "source",
    "requests",
    "results",
    "errors",
    "request_count",
    "last_24h",
    "last_7d",
    "source_label",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "payload_chars",
    "estimated_prompt_tokens",
    "exact_usage_records",
    "missing_usage_records",
    "trace_request_count",
    "trace_result_count",
    "cost",
    "cost_usd",
    "cost_note",
    "pricing",
    "currency",
    "window",
    "updated_at",
    "status",
    "note",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _human_bytes(value: float | int | None) -> str:
    if value is None:
        return "--"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(amount) < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


def _bytes_from_gb(value: float | int | None) -> int | None:
    if value is None:
        return None
    return int(float(value) * 1024**3)


def _read_cpu() -> tuple[int, int] | None:
    try:
        parts = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        values = [int(item) for item in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle
    except Exception:
        return None


def _cpu_percent() -> float | None:
    first = _read_cpu()
    if not first:
        return None
    time.sleep(0.05)
    second = _read_cpu()
    if not second:
        return None
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)


def _memory() -> dict[str, Any]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    except Exception:
        return {"available": False}
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    used = total - available if total is not None and available is not None else None
    percent = round((used / total) * 100, 1) if total else None
    return {
        "available": True,
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "percent": percent,
        "total_human": _human_bytes(total),
        "used_human": _human_bytes(used),
        "available_human": _human_bytes(available),
    }


def _disk() -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(PROJECT_ROOT)
    except Exception:
        return {"available": False, "path": str(PROJECT_ROOT)}
    percent = round((usage.used / usage.total) * 100, 1) if usage.total else None
    return {
        "available": True,
        "path": str(PROJECT_ROOT),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "percent": percent,
        "total_human": _human_bytes(usage.total),
        "used_human": _human_bytes(usage.used),
        "free_human": _human_bytes(usage.free),
        "cap_source": "filesystem_statvfs",
        "observed_total_bytes": usage.total,
        "observed_total_human": _human_bytes(usage.total),
        "observed_used_bytes": usage.used,
        "observed_used_human": _human_bytes(usage.used),
    }


def _systemd_user_unit(unit: str) -> dict[str, Any]:
    properties = (
        "ActiveState",
        "SubState",
        "Result",
        "MainPID",
        "NRestarts",
        "ExecMainStatus",
        "ActiveEnterTimestamp",
        "InactiveEnterTimestamp",
        "NextElapseUSecRealtime",
        "LastTriggerUSec",
        "ExecMainStartTimestamp",
        "ExecMainExitTimestamp",
        "MemoryPeak",
        "MemorySwapPeak",
        "CPUUsageNSec",
        "UnitFileState",
        "LoadState",
        "FragmentPath",
        "DropInPaths",
    )
    command = ["systemctl", "--user", "show", unit, "--no-pager"]
    command.extend(f"--property={item}" for item in properties)
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"unit": unit, "available": False, "error": str(exc)}
    if completed.returncode != 0:
        return {
            "unit": unit,
            "available": False,
            "error": (completed.stderr or completed.stdout or "systemctl_show_failed").strip(),
        }
    values = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    integer_fields = {
        "MainPID",
        "NRestarts",
        "ExecMainStatus",
        "CPUUsageNSec",
    }
    for key in integer_fields:
        try:
            values[key] = int(values.get(key) or 0)
        except (TypeError, ValueError):
            values[key] = 0
    optional_integer_fields = {"MemoryPeak", "MemorySwapPeak"}
    for key in optional_integer_fields:
        raw_value = values.get(key)
        try:
            values[key] = int(raw_value) if raw_value not in {None, "", "[not set]"} else None
        except (TypeError, ValueError):
            values[key] = None
    return {
        "unit": unit,
        "available": True,
        "active_state": values.get("ActiveState", "unknown"),
        "sub_state": values.get("SubState", "unknown"),
        "result": values.get("Result", ""),
        "main_pid": values.get("MainPID", 0),
        "restart_count": values.get("NRestarts", 0),
        "exit_status": values.get("ExecMainStatus", 0),
        "active_since": values.get("ActiveEnterTimestamp", ""),
        "inactive_since": values.get("InactiveEnterTimestamp", ""),
        "next_trigger": values.get("NextElapseUSecRealtime", ""),
        "last_trigger": values.get("LastTriggerUSec", ""),
        "execution_started_at": values.get("ExecMainStartTimestamp", ""),
        "execution_finished_at": values.get("ExecMainExitTimestamp", ""),
        "memory_peak_bytes": values.get("MemoryPeak", 0),
        "memory_peak_human": _human_bytes(values.get("MemoryPeak", 0)),
        "swap_peak_bytes": values.get("MemorySwapPeak", 0),
        "swap_peak_human": _human_bytes(values.get("MemorySwapPeak", 0)),
        "cpu_seconds": round(float(values.get("CPUUsageNSec", 0)) / 1_000_000_000, 1),
        "unit_file_state": values.get("UnitFileState", ""),
        "load_state": values.get("LoadState", ""),
        "fragment_path": values.get("FragmentPath", ""),
        "drop_in_paths": values.get("DropInPaths", ""),
        "memory_peak_recorded": values.get("MemoryPeak") is not None,
        "swap_peak_recorded": values.get("MemorySwapPeak") is not None,
    }


def _automation_override_root() -> Path:
    configured = os.environ.get("FXALPHA_SYSTEMD_USER_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".config" / "systemd" / "user"


def _automation_override_path(timer_unit: str) -> Path:
    return _automation_override_root() / f"{timer_unit}.d" / "fxalpha-schedule.conf"


def _automation_schedule_time(workflow_key: str) -> str:
    config = _AUTOMATION_WORKFLOWS[workflow_key]
    override_path = _automation_override_path(config["timer_unit"])
    try:
        content = override_path.read_text(encoding="utf-8")
    except OSError:
        return str(config["default_time"])
    match = re.search(r"OnCalendar=Tue\.\.Sat \*-\*-\* (\d{2}:\d{2}):00 Asia/Shanghai", content)
    return match.group(1) if match else str(config["default_time"])


def _automation_schedule_label(workflow_key: str) -> str:
    config = _AUTOMATION_WORKFLOWS[workflow_key]
    return f"{config['schedule_prefix']} {_automation_schedule_time(workflow_key)}{config['schedule_suffix']}"


def _run_systemctl_user(*args: str, timeout: int = 12) -> tuple[bool, str]:
    command = ["systemctl", "--user", *args]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    message = (completed.stderr or completed.stdout or "").strip()
    return completed.returncode == 0, message


def _record_automation_control_audit(payload: dict[str, Any]) -> None:
    audit_path = Path(os.environ.get("FXALPHA_AUTOMATION_AUDIT_FILE", str(_AUTOMATION_AUDIT_FILE))).expanduser()
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": _now(), **payload}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def platform_automation_control(
    *,
    workflow: str,
    action: str,
    schedule_time: str | None = None,
    confirm: bool = False,
) -> Any:
    workflow_key = str(workflow or "").strip()
    action_key = str(action or "").strip()
    config = _AUTOMATION_WORKFLOWS.get(workflow_key)
    if config is None:
        return err_result("automation_workflow_not_allowed", inputs={"workflow": workflow_key, "action": action_key})
    if action_key not in {"resume", "pause", "run_now", "update_schedule"}:
        return err_result("automation_action_not_allowed", inputs={"workflow": workflow_key, "action": action_key})
    if not confirm:
        return err_result(
            "automation_write_confirmation_required",
            inputs={"workflow": workflow_key, "action": action_key},
            outputs={"required_confirm": True},
        )

    normalized_time = str(schedule_time or "").strip()
    if action_key == "update_schedule" and not _AUTOMATION_TIME_RE.fullmatch(normalized_time):
        return err_result(
            "automation_schedule_time_invalid",
            inputs={"workflow": workflow_key, "action": action_key, "schedule_time": normalized_time},
            outputs={"expected": "HH:MM"},
        )

    service_unit = str(config["service_unit"])
    timer_unit = str(config["timer_unit"])
    audit_payload = {"workflow": workflow_key, "action": action_key, "schedule_time": normalized_time or None}
    with _AUTOMATION_CONTROL_LOCK:
        if action_key == "run_now":
            current = _systemd_user_unit(service_unit)
            if current.get("active_state") in {"active", "activating"}:
                result = err_result("automation_service_already_running", inputs=audit_payload, outputs={"service": current})
                _record_automation_control_audit({**audit_payload, "ok": False, "error": result.err})
                return result
            succeeded, message = _run_systemctl_user("start", "--no-block", service_unit)
        elif action_key == "pause":
            succeeded, message = _run_systemctl_user("disable", "--now", timer_unit)
        elif action_key == "resume":
            succeeded, message = _run_systemctl_user("enable", "--now", timer_unit)
        else:
            override_path = _automation_override_path(timer_unit)
            previous_content = override_path.read_text(encoding="utf-8") if override_path.exists() else None
            override_content = (
                "# Managed by FXAlpha production automation console.\n"
                "[Timer]\n"
                "OnCalendar=\n"
                f"OnCalendar=Tue..Sat *-*-* {normalized_time}:00 Asia/Shanghai\n"
            )
            try:
                override_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = override_path.with_suffix(".tmp")
                temporary_path.write_text(override_content, encoding="utf-8")
                os.replace(temporary_path, override_path)
                reload_ok, reload_message = _run_systemctl_user("daemon-reload")
                restart_ok, restart_message = _run_systemctl_user("restart", timer_unit) if reload_ok else (False, "")
                succeeded = reload_ok and restart_ok
                message = reload_message or restart_message
                if not succeeded:
                    if previous_content is None:
                        override_path.unlink(missing_ok=True)
                    else:
                        override_path.write_text(previous_content, encoding="utf-8")
                    _run_systemctl_user("daemon-reload")
                    _run_systemctl_user("restart", timer_unit)
            except OSError as exc:
                succeeded, message = False, str(exc)

    if not succeeded:
        _record_automation_control_audit({**audit_payload, "ok": False, "error": message or "systemctl_failed"})
        return err_result(
            "automation_systemd_control_failed",
            inputs=audit_payload,
            outputs={"message": message or "systemctl_failed", "timer_unit": timer_unit, "service_unit": service_unit},
        )

    refreshed = _background_workflow(
        service_unit=service_unit,
        timer_unit=timer_unit,
        schedule=_automation_schedule_label(workflow_key),
    )
    _record_automation_control_audit({**audit_payload, "ok": True})
    return ok_result(
        inputs=audit_payload,
        outputs={"status": "accepted", "workflow": workflow_key, "action": action_key, "automation": refreshed},
    )


def _background_workflow(*, service_unit: str, timer_unit: str, schedule: str) -> dict[str, Any]:
    service = _systemd_user_unit(service_unit)
    timer = _systemd_user_unit(timer_unit)
    if service.get("active_state") in {"active", "activating"}:
        status = "running"
    elif service.get("result") == "failed" or timer.get("active_state") == "failed":
        status = "failed"
    elif timer.get("active_state") == "active" and timer.get("sub_state") == "waiting":
        status = "scheduled"
    elif not service.get("available") or not timer.get("available"):
        status = "unavailable"
    else:
        status = "idle"
    if service.get("active_state") in {"active", "activating"}:
        service_state = "running"
    elif service.get("result") == "failed" or int(service.get("exit_status") or 0) != 0:
        service_state = "failed"
    elif (
        service.get("active_state") == "inactive"
        and service.get("sub_state") == "dead"
        and service.get("result") == "success"
    ):
        service_state = "completed"
    elif service.get("available") is False:
        service_state = "unavailable"
    else:
        service_state = "idle"
    timer_state = (
        "waiting"
        if timer.get("active_state") == "active" and timer.get("sub_state") == "waiting"
        else "failed"
        if timer.get("active_state") == "failed"
        else "unavailable"
        if timer.get("available") is False
        else "idle"
    )
    return {
        "status": status,
        "schedule": schedule,
        "service": {**service, "operational_state": service_state},
        "timer": {**timer, "operational_state": timer_state},
    }


def _automations_status() -> dict[str, Any]:
    return {
        "runtime": "WSL user systemd",
        "data_foundation": _background_workflow(
            service_unit="fxalpha-data-daily.service",
            timer_unit="fxalpha-data-daily.timer",
            schedule=_automation_schedule_label("data_foundation"),
        ),
        "paper_trading": _background_workflow(
            service_unit="fxalpha-paper-fleet-daily.service",
            timer_unit="fxalpha-paper-fleet-daily.timer",
            schedule=_automation_schedule_label("paper_trading"),
        ),
    }


def platform_automation_status() -> Any:
    return ok_result(outputs={"status": "ready", "automations": _automations_status()})


def _usage_path() -> Path:
    return Path(os.environ.get("FXALPHA_RUNTIME_USAGE_STATUS_FILE", str(_USAGE_STATUS_FILE))).expanduser()


def _sanitize_usage_block(raw: Any, allowed: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"configured": False}
    keys = allowed or _USAGE_ALLOWED_KEYS
    clean = {key: raw.get(key) for key in keys if key in raw}
    clean["configured"] = bool(clean.get("configured", True))
    return clean


def _codex_official(raw: Any) -> dict[str, Any]:
    clean = _sanitize_usage_block(raw, _CODEX_OFFICIAL_ALLOWED_KEYS)
    clean.setdefault("source", "not_configured")
    clean.setdefault("configured", False)
    return clean


def _codex_logs_db_path() -> Path | None:
    configured = os.environ.get("FXALPHA_CODEX_LOGS_DB")
    if configured:
        return Path(configured).expanduser()
    if _CODEX_LOGS_DB.exists():
        return _CODEX_LOGS_DB
    for path in Path("/mnt/c/Users").glob("*/.codex/logs_2.sqlite"):
        return path
    return None


def _iso_from_epoch_seconds(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value)).isoformat(timespec="seconds")
    except Exception:
        return None


def _sanitize_codex_limit_block(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, Any] = {
        "allowed": raw.get("allowed"),
        "limit_reached": raw.get("limit_reached"),
    }
    for key in ("primary", "secondary"):
        block = raw.get(key)
        if not isinstance(block, dict):
            continue
        used = block.get("used_percent")
        try:
            remaining = round(max(0.0, 100.0 - float(used)), 1)
        except Exception:
            remaining = None
        clean[key] = {
            "used_percent": used,
            "remaining_percent": remaining,
            "window_minutes": block.get("window_minutes"),
            "reset_after_seconds": block.get("reset_after_seconds"),
            "reset_at": block.get("reset_at"),
            "reset_at_iso": _iso_from_epoch_seconds(block.get("reset_at")),
        }
    return clean


def _parse_codex_rate_limit_event(body: str) -> dict[str, Any] | None:
    marker = '{"type":"codex.rate_limits"'
    start = str(body or "").find(marker)
    if start < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(body[start:])
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("type") != "codex.rate_limits":
        return None
    additional: dict[str, Any] = {}
    raw_additional = payload.get("additional_rate_limits")
    if isinstance(raw_additional, dict):
        for model, limits in raw_additional.items():
            additional[str(model)] = _sanitize_codex_limit_block(limits)
    return {
        "configured": True,
        "source": "codex_desktop_rate_limits_log",
        "status": "observed",
        "plan_type": payload.get("plan_type"),
        "rate_limits": _sanitize_codex_limit_block(payload.get("rate_limits")),
        "code_review_rate_limits": _sanitize_codex_limit_block(payload.get("code_review_rate_limits")),
        "additional_rate_limits": additional,
        "updated_at": payload.get("event.timestamp") or _now(),
    }


def _codex_rate_limits_from_logs(warnings: list[str]) -> dict[str, Any]:
    path = _codex_logs_db_path()
    if not path or not path.exists():
        warnings.append("codex_logs_db_missing")
        return {"configured": False, "source": "codex_desktop_rate_limits_log", "status": "missing_logs_db"}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=1.0)
        rows = con.execute(
            """
            select feedback_log_body
            from logs
            where feedback_log_body like '%"type":"codex.rate_limits"%'
            order by id desc
            limit 30
            """
        ).fetchall()
        con.close()
    except Exception as exc:
        warnings.append("codex_rate_limits_log_read_failed")
        return {
            "configured": False,
            "source": "codex_desktop_rate_limits_log",
            "status": "read_failed",
            "error": type(exc).__name__,
        }
    for (body,) in rows:
        parsed = _parse_codex_rate_limit_event(body or "")
        if parsed:
            parsed["source_path"] = str(path)
            return parsed
    warnings.append("codex_rate_limits_event_missing")
    return {"configured": False, "source": "codex_desktop_rate_limits_log", "status": "event_missing", "source_path": str(path)}


def _codex_rate_limits_from_sessions(warnings: list[str]) -> dict[str, Any]:
    """Read the newest rate-limit window emitted by the local Codex client.

    Session token_count events are the freshest supported local observation.  The
    logs database can lag behind the active client by days, so it is only a
    fallback for older installations.
    """
    paths = sorted(
        _codex_recent_session_paths(days=7),
        key=lambda item: item.stat().st_mtime_ns if item.exists() else 0,
        reverse=True,
    )
    latest: tuple[str, dict[str, Any], Path] | None = None
    for path in paths[:24]:
        for line in _tail_jsonl_lines(path, max_lines=4000, max_bytes=2 * 1024 * 1024):
            if '"type":"token_count"' not in line or '"rate_limits"' not in line:
                continue
            try:
                record = json.loads(line)
                payload = record.get("payload") if isinstance(record, dict) else {}
                limits = payload.get("rate_limits") if isinstance(payload, dict) else {}
                timestamp = str(record.get("timestamp") or "")
            except Exception:
                continue
            if not isinstance(limits, dict) or not timestamp:
                continue
            if latest is None or timestamp > latest[0]:
                latest = (timestamp, limits, path)
    if latest is None:
        warnings.append("codex_session_rate_limits_missing")
        return {"configured": False, "source": "codex_session_token_count", "status": "event_missing"}
    timestamp, limits, path = latest

    def clean_window(block: Any) -> dict[str, Any]:
        if not isinstance(block, dict):
            return {}
        used = block.get("used_percent")
        try:
            remaining = round(max(0.0, 100.0 - float(used)), 1)
        except Exception:
            remaining = None
        reset_at = block.get("resets_at") or block.get("reset_at")
        return {
            "used_percent": used,
            "remaining_percent": remaining,
            "window_minutes": block.get("window_minutes"),
            "reset_at": reset_at,
            "reset_at_iso": _iso_from_epoch_seconds(reset_at),
        }

    return {
        "configured": True,
        "source": "codex_session_token_count",
        "status": "observed",
        "plan_type": limits.get("plan_type"),
        "rate_limits": {
            "limit_reached": bool(limits.get("rate_limit_reached_type")),
            "primary": clean_window(limits.get("primary")),
            "secondary": clean_window(limits.get("secondary")),
        },
        "updated_at": timestamp,
        "source_path": str(path),
    }


def _codex_snapshot_path() -> Path:
    return Path(os.environ.get("FXALPHA_CODEX_USAGE_SNAPSHOT_FILE", str(_CODEX_USAGE_SNAPSHOT_FILE))).expanduser()


def _codex_state_db_path() -> Path | None:
    configured = os.environ.get("FXALPHA_CODEX_STATE_DB")
    if configured:
        return Path(configured).expanduser()
    if _CODEX_STATE_DB.exists():
        return _CODEX_STATE_DB
    for path in Path("/mnt/c/Users").glob("*/.codex/state_5.sqlite"):
        return path
    return None


def _codex_recent_session_paths(days: int = 7) -> list[Path]:
    paths: list[Path] = []
    sessions_root = _CODEX_HOME / "sessions"
    archived_root = _CODEX_HOME / "archived_sessions"
    now = datetime.utcnow()
    for offset in range(days + 1):
        day = now - timedelta(days=offset)
        day_dir = sessions_root / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
        if day_dir.exists():
            paths.extend(sorted(day_dir.rglob("*.jsonl")))
        if archived_root.exists():
            paths.extend(sorted(archived_root.glob(f"rollout-{day.strftime('%Y-%m-%d')}T*.jsonl")))
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        value = str(path)
        if value in seen:
            continue
        seen.add(value)
        unique.append(path)
    return unique


def _codex_session_signature(paths: list[Path]) -> tuple[tuple[str, int, int], ...]:
    signature: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except Exception:
            continue
        signature.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
    return tuple(signature)


def _codex_token_windows_from_session_events(warnings: list[str]) -> dict[str, Any]:
    paths = _codex_recent_session_paths(days=7)
    if not paths:
        warnings.append("codex_session_files_missing")
        return {}
    signature = _codex_session_signature(paths)
    cache_age = time.time() - float(_CODEX_SESSION_WINDOWS_CACHE.get("computed_at") or 0.0)
    if signature == _CODEX_SESSION_WINDOWS_CACHE.get("signature") and cache_age < _CODEX_SESSION_CACHE_TTL_SECONDS:
        cached = _CODEX_SESSION_WINDOWS_CACHE.get("value")
        if isinstance(cached, dict):
            return dict(cached)
    now = datetime.utcnow()
    cutoffs = {
        "last_24h": now - timedelta(hours=24),
        "last_7d": now - timedelta(days=7),
    }
    session_events: dict[str, list[tuple[datetime, int, int, int, int, int]]] = {}
    files_scanned = 0
    try:
        for path in paths:
            files_scanned += 1
            session_id = path.stem
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for raw_line in handle:
                    if '"type":"session_meta"' in raw_line and session_id == path.stem:
                        match = _CODEX_SESSION_ID_RE.search(raw_line)
                        if match:
                            session_id = match.group(1)
                    if '"type":"token_count"' not in raw_line:
                        continue
                    try:
                        record = json.loads(raw_line)
                        ts_value = str(record.get("timestamp") or "")
                        payload = record.get("payload") if isinstance(record, dict) else {}
                        info = payload.get("info") if isinstance(payload, dict) else {}
                        total_usage = info.get("total_token_usage") if isinstance(info, dict) else {}
                        ts_dt = datetime.fromisoformat(ts_value.replace("Z", "+00:00")).replace(tzinfo=None)
                        total_tokens = int(total_usage.get("total_tokens") or 0)
                        cached_tokens = int(total_usage.get("cached_input_tokens") or 0)
                        input_tokens = int(total_usage.get("input_tokens") or 0)
                        output_tokens = int(total_usage.get("output_tokens") or 0)
                        reasoning_tokens = int(total_usage.get("reasoning_output_tokens") or 0)
                    except Exception:
                        continue
                    if total_tokens <= 0:
                        continue
                    session_events.setdefault(session_id, []).append(
                        (ts_dt, total_tokens, cached_tokens, input_tokens, output_tokens, reasoning_tokens)
                    )
    except Exception as exc:
        warnings.append("codex_session_event_read_failed")
        return {"session_window_error": type(exc).__name__}
    windows: dict[str, Any] = {}
    for key, cutoff in cutoffs.items():
        tokens_used = cached_input_tokens = input_tokens = output_tokens = reasoning_tokens = events = 0
        sessions = 0
        updated_at: str | None = None
        for observations in session_events.values():
            observations.sort(key=lambda item: item[0])
            after = [item for item in observations if item[0] >= cutoff]
            if not after:
                continue
            before = [item for item in observations if item[0] < cutoff]
            baseline_total = before[-1][1] if before else 0
            baseline_cached = before[-1][2] if before else 0
            baseline_input = before[-1][3] if before else 0
            baseline_output = before[-1][4] if before else 0
            baseline_reasoning = before[-1][5] if before else 0
            tokens_used += max(0, after[-1][1] - baseline_total)
            cached_input_tokens += max(0, after[-1][2] - baseline_cached)
            input_tokens += max(0, after[-1][3] - baseline_input)
            output_tokens += max(0, after[-1][4] - baseline_output)
            reasoning_tokens += max(0, after[-1][5] - baseline_reasoning)
            events += len(after)
            sessions += 1
            latest = after[-1][0].isoformat(timespec="seconds")
            if updated_at is None or latest > updated_at:
                updated_at = latest
        windows[key] = {
            "threads": sessions,
            "sessions": sessions,
            "events": events,
            "tokens_used": tokens_used,
            "cached_input_tokens": cached_input_tokens,
            "uncached_input_tokens": max(0, input_tokens - cached_input_tokens),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_tokens,
            "non_cached_tokens": max(0, tokens_used - cached_input_tokens),
            "updated_at": updated_at,
            "source": "codex_session_rollout_events",
            "aggregation": "per_session_cumulative_delta",
        }
    value = {
        "last_24h": windows["last_24h"],
        "last_7d": windows["last_7d"],
        "session_source_path": str(_CODEX_HOME / "sessions"),
        "session_files_scanned": files_scanned,
    }
    _CODEX_SESSION_WINDOWS_CACHE["signature"] = signature
    _CODEX_SESSION_WINDOWS_CACHE["computed_at"] = time.time()
    _CODEX_SESSION_WINDOWS_CACHE["value"] = dict(value)
    return value


def _codex_token_windows_from_state(warnings: list[str]) -> dict[str, Any]:
    path = _codex_state_db_path()
    if not path or not path.exists():
        warnings.append("codex_state_db_missing")
        return {}
    now = time.time()
    windows: dict[str, Any] = {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=1.0)
        for key, seconds in (("last_24h", 24 * 3600), ("last_7d", 7 * 24 * 3600)):
            row = con.execute(
                """
                select count(*), coalesce(sum(tokens_used), 0), max(updated_at)
                from threads
                where updated_at >= ?
                """,
                (now - seconds,),
            ).fetchone()
            windows[key] = {
                "threads": int(row[0] or 0),
                "tokens_used": int(row[1] or 0),
                "updated_at": _iso_from_epoch_seconds(row[2]),
                "source": "codex_state_sqlite",
            }
        con.close()
    except Exception as exc:
        warnings.append("codex_state_window_read_failed")
        return {"state_window_error": type(exc).__name__}
    windows["state_source_path"] = str(path)
    return windows


def _codex_local_observed(warnings: list[str]) -> dict[str, Any]:
    path = _codex_snapshot_path()
    if not path.exists():
        warnings.append("codex_local_snapshot_missing")
        return {
            "configured": False,
            "source": "codex_desktop_sqlite_snapshot",
            "status": "missing_snapshot",
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warnings.append("codex_local_snapshot_invalid")
        return {
            "configured": False,
            "source": "codex_desktop_sqlite_snapshot",
            "status": "invalid_snapshot",
            "error": type(exc).__name__,
        }
    outputs = raw.get("outputs") if isinstance(raw, dict) and isinstance(raw.get("outputs"), dict) else raw
    if not isinstance(outputs, dict):
        warnings.append("codex_local_snapshot_invalid")
        return {
            "configured": False,
            "source": "codex_desktop_sqlite_snapshot",
            "status": "invalid_snapshot",
        }
    clean: dict[str, Any] = {
        "configured": True,
        "source": "codex_desktop_sqlite_snapshot",
        "status": "observed",
        "generated_at": outputs.get("generated_at") or raw.get("generated_at"),
    }
    for key in (
        "current_thread",
        "last_24h",
        "last_7d",
        "project",
        "warnings",
    ):
        value = outputs.get(key)
        if isinstance(value, (dict, list)):
            clean[key] = value
    for key in ("window", "state_source_path", "session_source_path", "session_files_scanned"):
        if key in outputs:
            clean[key] = outputs.get(key)
    live_windows = _codex_token_windows_from_session_events(warnings)
    if live_windows:
        clean.update(live_windows)
        clean["window"] = "Codex session last_token_usage aggregate"
    elif not clean.get("last_24h") and not clean.get("last_7d"):
        clean.update(_codex_token_windows_from_state(warnings))
    return clean


def _deepseek_api_key() -> str:
    for name in ("FXALPHA_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_KEY"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    return str(LLM_API_KEY or "").strip()


def _wsl_gateway_proxy_url() -> str | None:
    try:
        for line in Path("/etc/resolv.conf").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("nameserver"):
                host = line.split()[1]
                return f"http://{host}:7890"
    except Exception:
        return None
    return None


def _deepseek_balance_request(api_key: str, *, proxy_url: str | None = None) -> dict[str, Any]:
    req = request.Request(
        "https://api.deepseek.com/user/balance",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="GET",
    )
    opener = request.build_opener(request.ProxyHandler({"http": proxy_url, "https": proxy_url})) if proxy_url else request.build_opener()
    with opener.open(req, timeout=4) as response:
        return json.loads(response.read().decode("utf-8"))


def _decimal_balance(value: Any) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _deepseek_balance_history() -> list[dict[str, Any]]:
    try:
        raw = json.loads(_DEEPSEEK_BALANCE_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    samples = raw.get("samples") if isinstance(raw, dict) else []
    if not isinstance(samples, list):
        return []
    clean: list[dict[str, Any]] = []
    for sample in samples:
        if not isinstance(sample, dict) or not isinstance(sample.get("balances"), dict):
            continue
        try:
            recorded_at = datetime.fromisoformat(str(sample.get("recorded_at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if recorded_at.tzinfo is not None:
            recorded_at = recorded_at.replace(tzinfo=None)
        if datetime.now() - recorded_at > _DEEPSEEK_BALANCE_HISTORY_RETENTION:
            continue
        balances = {
            str(currency): amount
            for currency, value in sample["balances"].items()
            if (amount := _decimal_balance(value)) is not None
        }
        if balances:
            clean.append({"recorded_at": recorded_at.isoformat(timespec="seconds"), "balances": balances})
    return clean


def _store_deepseek_balance_sample(balance_infos: list[dict[str, Any]], recorded_at: str) -> dict[str, Any]:
    """Persist successful official-balance reads and return the previous-sample delta.

    The official endpoint exposes balance, not an invoice or usage total.  The
    returned delta is deliberately a balance change, so deposits/refunds remain
    visible as positive changes rather than being reported as token spending.
    """
    balances = {
        str(item.get("currency") or "").upper(): amount
        for item in balance_infos
        if str(item.get("currency") or "").strip()
        if (amount := _decimal_balance(item.get("total_balance"))) is not None
    }
    if not balances:
        return {}
    samples = _deepseek_balance_history()
    previous = samples[-1] if samples else None
    changes: dict[str, Any] = {}
    for currency, current in balances.items():
        previous_balance = _decimal_balance((previous or {}).get("balances", {}).get(currency))
        if previous_balance is None:
            continue
        changes[currency] = {
            "previous_balance": previous_balance,
            "current_balance": current,
            "delta": round(current - previous_balance, 6),
            "previous_at": previous.get("recorded_at"),
        }
    samples.append({"recorded_at": recorded_at, "balances": balances})
    try:
        _DEEPSEEK_BALANCE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _DEEPSEEK_BALANCE_HISTORY_FILE.with_name(f".{_DEEPSEEK_BALANCE_HISTORY_FILE.name}.tmp")
        tmp_path.write_text(json.dumps({"version": 1, "samples": samples}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(_DEEPSEEK_BALANCE_HISTORY_FILE)
    except OSError:
        # A history-write issue must not make the official balance unavailable.
        pass
    return changes


def _deepseek_balance_window_changes(
    balance_infos: list[dict[str, Any]],
    *,
    recorded_at: str,
    hours: int = 24,
) -> dict[str, Any]:
    """Return the observed official-balance change over a completed window.

    This remains a balance delta, not an invoice: a positive delta is exposed
    as an increase so callers do not mistake a recharge for negative spend.
    """
    balances = {
        str(item.get("currency") or "").upper(): amount
        for item in balance_infos
        if str(item.get("currency") or "").strip()
        if (amount := _decimal_balance(item.get("total_balance"))) is not None
    }
    if not balances:
        return {}
    try:
        end_at = datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00"))
        if end_at.tzinfo is not None:
            end_at = end_at.replace(tzinfo=None)
    except ValueError:
        end_at = datetime.now()
    target_at = end_at - timedelta(hours=hours)
    baseline = None
    for sample in _deepseek_balance_history():
        try:
            sample_at = datetime.fromisoformat(str(sample.get("recorded_at") or "").replace("Z", "+00:00"))
            if sample_at.tzinfo is not None:
                sample_at = sample_at.replace(tzinfo=None)
        except ValueError:
            continue
        if sample_at <= target_at:
            baseline = sample
    if not baseline:
        return {}
    out: dict[str, Any] = {}
    for currency, current_balance in balances.items():
        previous_balance = _decimal_balance((baseline.get("balances") or {}).get(currency))
        if previous_balance is None:
            continue
        out[currency] = {
            "baseline_balance": previous_balance,
            "current_balance": current_balance,
            "delta": round(current_balance - previous_balance, 6),
            "baseline_at": baseline.get("recorded_at"),
            "window_hours": hours,
        }
    return out


def _deepseek_official_balance(warnings: list[str]) -> dict[str, Any]:
    api_key = _deepseek_api_key()
    if not api_key:
        warnings.append("deepseek_balance_key_missing")
        return {
            "configured": False,
            "source": "https://api.deepseek.com/user/balance",
            "status": "missing_api_key",
        }
    try:
        payload = _deepseek_balance_request(api_key)
    except error.HTTPError as exc:
        status = "unauthorized" if exc.code in {401, 403} else "http_error"
        warnings.append(f"deepseek_balance_{status}")
        return {
            "configured": True,
            "source": "https://api.deepseek.com/user/balance",
            "status": status,
            "http_status": exc.code,
            "updated_at": _now(),
        }
    except Exception as direct_exc:
        proxy_url = _wsl_gateway_proxy_url()
        if proxy_url:
            try:
                payload = _deepseek_balance_request(api_key, proxy_url=proxy_url)
            except error.HTTPError as exc:
                status = "unauthorized" if exc.code in {401, 403} else "http_error"
                warnings.append(f"deepseek_balance_proxy_{status}")
                return {
                    "configured": True,
                    "source": "https://api.deepseek.com/user/balance",
                    "status": status,
                    "http_status": exc.code,
                    "proxy": "wsl_gateway_7890",
                    "updated_at": _now(),
                }
            except Exception as proxy_exc:
                warnings.append("deepseek_balance_unavailable")
                return {
                    "configured": True,
                    "source": "https://api.deepseek.com/user/balance",
                    "status": "unavailable",
                    "error": type(proxy_exc).__name__,
                    "direct_error": type(direct_exc).__name__,
                    "proxy": "wsl_gateway_7890",
                    "updated_at": _now(),
                }
        else:
            warnings.append("deepseek_balance_unavailable")
            return {
                "configured": True,
                "source": "https://api.deepseek.com/user/balance",
                "status": "unavailable",
                "error": type(direct_exc).__name__,
                "updated_at": _now(),
            }
    infos = payload.get("balance_infos") if isinstance(payload, dict) else []
    clean_infos = []
    if isinstance(infos, list):
        for item in infos:
            if not isinstance(item, dict):
                continue
            clean_infos.append({
                "currency": item.get("currency"),
                "total_balance": item.get("total_balance"),
                "granted_balance": item.get("granted_balance"),
                "topped_up_balance": item.get("topped_up_balance"),
            })
    updated_at = _now()
    balance_changes = _store_deepseek_balance_sample(clean_infos, updated_at)
    return {
        "configured": True,
        "source": "https://api.deepseek.com/user/balance",
        "status": "ok",
        "is_available": bool(payload.get("is_available")) if isinstance(payload, dict) else False,
        "balance_infos": clean_infos,
        "balance_changes": balance_changes,
        "balance_24h_changes": _deepseek_balance_window_changes(clean_infos, recorded_at=updated_at),
        "proxy": "direct_or_wsl_gateway",
        "updated_at": updated_at,
    }


def _deepseek_observed_trace(raw: Any) -> dict[str, Any]:
    clean = _sanitize_usage_block(raw, _DEEPSEEK_TRACE_ALLOWED_KEYS)
    clean.setdefault("source", "local_orchestrator_llm_traces")
    return clean


def _deepseek_trace_paths() -> list[Path]:
    override = os.environ.get("FXALPHA_DEEPSEEK_TRACE_FILE")
    if override:
        path = Path(override).expanduser()
        return [path] if path.exists() else []
    configured_root = os.environ.get("FXALPHA_DEEPSEEK_TRACE_ROOT")
    roots = [Path(configured_root).expanduser()] if configured_root else [_DEEPSEEK_TRACE_ROOT, _DEEPSEEK_MODEL_TRACE_ROOT]
    paths: list[Path] = []
    for root in roots:
        current = root / "current.jsonl"
        if current.exists():
            paths.append(current)
        history = root / "history"
        if history.exists():
            paths.extend(sorted(history.glob("*.jsonl")))
    return paths


def _tail_jsonl_lines(path: Path, *, max_lines: int = _TRACE_TAIL_MAX_LINES, max_bytes: int = _TRACE_TAIL_BYTES) -> list[str]:
    try:
        size = path.stat().st_size
    except Exception:
        return []
    if size <= 0:
        return []
    max_bytes = max(4096, int(max_bytes or 0))
    try:
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(max(0, size - max_bytes))
                data = handle.read(max_bytes)
                lines = data.decode("utf-8", errors="ignore").splitlines()
                if lines:
                    lines = lines[1:]
            else:
                lines = handle.read().decode("utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    return lines[-max(1, int(max_lines or 1)) :]


def _deepseek_pricing() -> dict[str, Any]:
    def price(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except Exception:
            return default

    return {
        "currency": "USD",
        "cache_hit_input_usd_per_1m": price("FXALPHA_DEEPSEEK_CACHE_HIT_USD_PER_1M", 0.0028),
        "cache_miss_input_usd_per_1m": price("FXALPHA_DEEPSEEK_CACHE_MISS_USD_PER_1M", 0.14),
        "input_usd_per_1m": price("FXALPHA_DEEPSEEK_INPUT_USD_PER_1M", 0.14),
        "output_usd_per_1m": price("FXALPHA_DEEPSEEK_OUTPUT_USD_PER_1M", 0.28),
        "model": "deepseek-v4-flash",
        "source": "deepseek_official_pricing_2026-07-12",
    }


def _deepseek_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    pricing: dict[str, Any],
    *,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
) -> float:
    accounted_input = max(0, cache_hit_tokens) + max(0, cache_miss_tokens)
    if accounted_input < prompt_tokens:
        cache_miss_tokens += prompt_tokens - accounted_input
    return round(
        (cache_hit_tokens / 1_000_000) * float(pricing.get("cache_hit_input_usd_per_1m") or 0)
        + (cache_miss_tokens / 1_000_000) * float(pricing.get("cache_miss_input_usd_per_1m") or pricing.get("input_usd_per_1m") or 0)
        + (completion_tokens / 1_000_000) * float(pricing.get("output_usd_per_1m") or 0),
        6,
    )


def _numeric(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value))
    except Exception:
        return 0


def _usage_from_trace_record(record: dict[str, Any]) -> dict[str, int]:
    usage = record.get("usage")
    if not isinstance(usage, dict):
        result = record.get("result")
        usage = result.get("usage") if isinstance(result, dict) and isinstance(result.get("usage"), dict) else {}
    if not isinstance(usage, dict) or not usage:
        parsed = record.get("parsed_response")
        if isinstance(parsed, dict):
            usage = parsed.get("llm_usage") or parsed.get("usage")
            if not isinstance(usage, dict) and isinstance(parsed.get("extra"), dict):
                usage = parsed["extra"].get("llm_usage") or parsed["extra"].get("usage")
    if not isinstance(usage, dict):
        usage = {}
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    return {
        "prompt_tokens": _numeric(usage.get("prompt_tokens") or usage.get("input_tokens")),
        "completion_tokens": _numeric(usage.get("completion_tokens") or usage.get("output_tokens")),
        "total_tokens": _numeric(usage.get("total_tokens")),
        "prompt_cache_hit_tokens": _numeric(usage.get("prompt_cache_hit_tokens") or prompt_details.get("cached_tokens")),
        "prompt_cache_miss_tokens": _numeric(usage.get("prompt_cache_miss_tokens")),
    }


def _estimated_usage_from_trace_record(record: dict[str, Any]) -> dict[str, int]:
    usage = _usage_from_trace_record(record)
    if usage["prompt_tokens"] or usage["completion_tokens"] or usage["total_tokens"]:
        if usage["total_tokens"] <= 0:
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        return usage
    event_type = str(record.get("event_type") or "")
    prompt_tokens = 0
    completion_tokens = 0
    if event_type == "llm_request":
        prompt_tokens = int((_numeric(record.get("payload_chars")) + 3) / 4)
    elif event_type == "llm_result":
        try:
            result_chars = len(json.dumps(record.get("result", {}), ensure_ascii=False))
        except Exception:
            result_chars = 0
        completion_tokens = int((result_chars + 3) / 4) if result_chars else 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _has_exact_usage(record: dict[str, Any]) -> bool:
    usage = _usage_from_trace_record(record)
    return bool(usage["prompt_tokens"] or usage["completion_tokens"] or usage["total_tokens"])


def _deepseek_trace_usage_from_files(warnings: list[str]) -> dict[str, Any]:
    paths = _deepseek_trace_paths()
    pricing = _deepseek_pricing()
    base: dict[str, Any] = {
        "configured": bool(paths),
        "source": "local_orchestrator_llm_traces",
        "source_path": str(_DEEPSEEK_TRACE_ROOT),
        "source_files": [str(path) for path in paths],
        "source_label": "来自本地 trace",
        "status": "missing_trace_file",
        "request_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "exact_usage_records": 0,
        "missing_usage_records": 0,
        "trace_request_count": 0,
        "trace_result_count": 0,
        "cost_usd": 0,
        "cost_note": "DeepSeek response usage × 配置单价",
        "pricing": pricing,
        "note": "DeepSeek 官方 balance API 不提供用量聚合；这里聚合因子研究和模型研究 trace 中保存的 response usage。",
    }
    if not paths:
        warnings.append("deepseek_trace_file_missing")
        return base
    prompt_tokens = completion_tokens = total_tokens = cache_hit_tokens = cache_miss_tokens = 0
    requests = results = errors = parse_errors = payload_chars = exact_usage_records = missing_usage_records = 0
    now = datetime.now()
    windows = {
        "last_24h": {"request_count": 0, "missing_usage_records": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0},
        "last_7d": {"request_count": 0, "missing_usage_records": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0},
    }
    latest_ts = ""
    models: set[str] = set()
    seen: set[tuple[str, str, str]] = set()
    scanned_files = 0
    scanned_lines = 0
    for path in paths:
        lines = _tail_jsonl_lines(path)
        if not lines:
            parse_errors += 1
            warnings.append("deepseek_trace_read_failed")
            continue
        scanned_files += 1
        scanned_lines += len(lines)
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except Exception:
                parse_errors += 1
                continue
            if not isinstance(record, dict):
                continue
            provider = str(record.get("llm_provider") or "").lower()
            model = str(record.get("llm_model") or "").lower()
            if provider and "deepseek" not in provider and "deepseek" not in model:
                continue
            event_type = str(record.get("event_type") or "")
            ts_value = str(record.get("ts") or "")
            dedupe_key = (str(record.get("trace_id") or ""), event_type, ts_value)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            if record.get("llm_model"):
                models.add(str(record.get("llm_model")))
            if ts_value > latest_ts:
                latest_ts = ts_value
            if event_type == "llm_request":
                requests += 1
                payload_chars += _numeric(record.get("payload_chars"))
                continue
            elif event_type == "llm_result":
                results += 1
            elif event_type == "llm_error":
                errors += 1
                continue
            if event_type != "llm_result":
                continue
            try:
                ts_dt = datetime.fromisoformat(ts_value.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                ts_dt = None
            if not _has_exact_usage(record):
                missing_usage_records += 1
                if ts_dt is not None:
                    age = now - ts_dt
                    for key, horizon in (("last_24h", timedelta(hours=24)), ("last_7d", timedelta(days=7))):
                        if timedelta(0) <= age <= horizon:
                            windows[key]["missing_usage_records"] += 1
                continue
            exact_usage_records += 1
            usage = _usage_from_trace_record(record)
            if usage["total_tokens"] <= 0:
                usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
            prompt_tokens += usage["prompt_tokens"]
            completion_tokens += usage["completion_tokens"]
            total_tokens += usage["total_tokens"]
            cache_hit_tokens += usage["prompt_cache_hit_tokens"]
            cache_miss_tokens += usage["prompt_cache_miss_tokens"]
            if ts_dt is not None:
                age = now - ts_dt
                for key, horizon in (("last_24h", timedelta(hours=24)), ("last_7d", timedelta(days=7))):
                    if timedelta(0) <= age <= horizon:
                        windows[key]["request_count"] += 1
                        windows[key]["prompt_tokens"] += usage["prompt_tokens"]
                        windows[key]["completion_tokens"] += usage["completion_tokens"]
                        windows[key]["total_tokens"] += usage["total_tokens"]
                        windows[key]["prompt_cache_hit_tokens"] += usage["prompt_cache_hit_tokens"]
                        windows[key]["prompt_cache_miss_tokens"] += usage["prompt_cache_miss_tokens"]
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    for block in windows.values():
        block["cost_usd"] = _deepseek_cost_usd(
            block["prompt_tokens"],
            block["completion_tokens"],
            pricing,
            cache_hit_tokens=block["prompt_cache_hit_tokens"],
            cache_miss_tokens=block["prompt_cache_miss_tokens"],
        )
        block["cost_note"] = "精确 usage × DeepSeek V4 Flash 官方价表快照"
    status = "observed_exact_usage" if exact_usage_records else "usage_missing_in_historical_trace"
    return {
        **base,
        "configured": True,
        "status": status,
        "requests": requests,
        "results": results,
        "errors": errors,
        "request_count": exact_usage_records,
        "trace_request_count": requests,
        "trace_result_count": results,
        "exact_usage_records": exact_usage_records,
        "missing_usage_records": missing_usage_records,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "prompt_cache_hit_tokens": cache_hit_tokens,
        "prompt_cache_miss_tokens": cache_miss_tokens,
        "cost_usd": _deepseek_cost_usd(
            prompt_tokens,
            completion_tokens,
            pricing,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
        ),
        "payload_chars": payload_chars,
        "estimated_prompt_tokens": 0,
        "window": "factor research + model orchestrator trace tails",
        "tail_read": True,
        "scanned_files": scanned_files,
        "scanned_lines": scanned_lines,
        "updated_at": latest_ts or _now(),
        "models": sorted(models)[:8],
        "parse_errors": parse_errors,
        **windows,
    }


def _compute_usage_status(warnings: list[str]) -> dict[str, Any]:
    path = _usage_path()
    usage: dict[str, Any] = {
        "source_path": str(path),
        "codex": {
            "official": _codex_rate_limits_from_sessions(warnings),
            "local_observed": _codex_local_observed(warnings),
        },
        "deepseek": {
            "official_balance": _deepseek_official_balance(warnings),
            "observed_trace": _deepseek_trace_usage_from_files(warnings),
        },
    }
    if not path.exists():
        warnings.append("runtime_usage_status_missing")
        return usage
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warnings.append("runtime_usage_status_invalid")
        usage["codex"]["official"] = {"configured": False, "error": type(exc).__name__, "status": "invalid_status_file"}
        usage["deepseek"]["observed_trace"] = {"configured": False, "error": type(exc).__name__, "status": "invalid_status_file"}
        return usage
    codex_raw = raw.get("codex") if isinstance(raw, dict) else {}
    deepseek_raw = raw.get("deepseek") if isinstance(raw, dict) else {}
    configured_official = _codex_official(codex_raw.get("official") if isinstance(codex_raw, dict) and isinstance(codex_raw.get("official"), dict) else codex_raw)
    if configured_official.get("configured") is True and not usage["codex"]["official"].get("configured"):
        usage["codex"]["official"] = configured_official
    if (
        not usage["codex"]["local_observed"].get("configured")
        and isinstance(codex_raw, dict)
        and isinstance(codex_raw.get("local_observed"), dict)
    ):
        usage["codex"]["local_observed"] = _sanitize_usage_block(codex_raw.get("local_observed"))
    configured_trace = _deepseek_observed_trace(
        deepseek_raw.get("observed_trace") if isinstance(deepseek_raw, dict) and isinstance(deepseek_raw.get("observed_trace"), dict) else deepseek_raw
    )
    if configured_trace.get("configured") is True and not usage["deepseek"]["observed_trace"].get("configured"):
        usage["deepseek"]["observed_trace"] = configured_trace
    return usage


def _usage_status(warnings: list[str]) -> dict[str, Any]:
    now = time.monotonic()
    with _USAGE_CACHE_LOCK:
        value = _USAGE_CACHE.get("value")
        age = now - float(_USAGE_CACHE.get("computed_at") or 0.0)
        if isinstance(value, dict) and age < _USAGE_CACHE_TTL_SECONDS:
            warnings.extend(str(item) for item in (_USAGE_CACHE.get("warnings") or []) if item not in warnings)
            cached = json.loads(json.dumps(value, ensure_ascii=False, default=str))
            cached["cache"] = {"hit": True, "age_seconds": round(age, 3), "ttl_seconds": _USAGE_CACHE_TTL_SECONDS}
            return cached

        computed_warnings: list[str] = []
        computed = _compute_usage_status(computed_warnings)
        _USAGE_CACHE["computed_at"] = time.monotonic()
        _USAGE_CACHE["value"] = json.loads(json.dumps(computed, ensure_ascii=False, default=str))
        _USAGE_CACHE["warnings"] = list(computed_warnings)
        warnings.extend(item for item in computed_warnings if item not in warnings)
        computed["cache"] = {"hit": False, "age_seconds": 0.0, "ttl_seconds": _USAGE_CACHE_TTL_SECONDS}
        return computed


def platform_runtime_status(*, compact: bool = False) -> Any:
    warnings: list[str] = []
    # The overview only needs a bounded liveness probe.  Usage aggregation scans
    # local Codex/LLM traces and belongs to the asynchronously loaded usage cards,
    # not the first-paint system rail.
    quantgpt_health = http_json_health(
        f"{QUANTGPT_API_URL}/api/v1/health",
        timeout=0.6 if compact else 2.0,
    )
    gui_index = PROJECT_ROOT / "gui" / "index.html"
    uptime_seconds = max(0, int(time.time() - _PROCESS_STARTED_AT))
    return ok_result(
        outputs={
            "status": "ready",
            "system": {
                "generated_at": _now(),
                "disk": _disk(),
                "process_uptime_seconds": uptime_seconds,
                "process_uptime_human": f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m",
            },
            "services": {
                "api": {"ok": True, "service": "fxalpha-api"},
                "gui": {"ok": gui_index.exists(), "index": str(gui_index)},
                "quantgpt": quantgpt_health,
            },
            "automations": _automations_status(),
            "usage": {} if compact else _usage_status(warnings),
        },
        warnings=warnings,
    )
