from __future__ import annotations

import os
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from storage.paths import FACTOR_ACTIVE_ADOPTED_VALUES_FILE, PROJECT_ROOT, QUANTGPT_API_URL, QUANTGPT_CODE_ROOT, RUNTIME_ROOT

from .service_health import http_json_health


GUI_URL = "http://127.0.0.1:18081/gui/"
_STARTUP_TIMEOUT_SECONDS = 20.0
_POLL_INTERVAL_SECONDS = 0.5
_LOG_TAIL_LINES = 12
_SERVICE_LOG_DIR = RUNTIME_ROOT / "api_logs"


def _pid_file(name: str) -> Path:
    return _SERVICE_LOG_DIR / f"{name}.pid"


def _log_file(name: str, stream: str) -> Path:
    return _SERVICE_LOG_DIR / f"{name}.{stream}"


def _parse_url_port(url: str, default_host: str, default_port: int) -> tuple[str, int]:
    parsed = urlparse(url)
    return parsed.hostname or default_host, int(parsed.port or default_port)


_qgpt_host, _qgpt_port = _parse_url_port(QUANTGPT_API_URL, "127.0.0.1", 8003)

SERVICE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "service_key": "fxalpha_api",
        "service_name": "fxalpha_api_18081",
        "host": "127.0.0.1",
        "port": 18081,
        "health_url": "http://127.0.0.1:18081/health",
        "cwd": PROJECT_ROOT,
        "command": ["python3", "scripts/start_fxalpha_api_18081.py"],
    },
    {
        "service_key": "quantgpt_api",
        "service_name": "quantgpt_8003",
        "host": _qgpt_host,
        "port": _qgpt_port,
        "health_url": f"{QUANTGPT_API_URL.rstrip('/')}/api/v1/health",
        "cwd": QUANTGPT_CODE_ROOT,
        "command": ["python3", "-m", "quantgpt", "--transport", "http", "--host", _qgpt_host, "--port", str(_qgpt_port)],
        "env": {"FXALPHA_ADOPTED_VALUES_FILE": str(FACTOR_ACTIVE_ADOPTED_VALUES_FILE)},
    },
)


def _port_open(host: str, port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.3)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _read_pid(pid_path: Path) -> int | None:
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _tail_lines(path: Path, limit: int = _LOG_TAIL_LINES) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def _service_snapshot(spec: dict[str, Any]) -> dict[str, Any]:
    service_name = str(spec["service_name"])
    pid_path = _pid_file(service_name)
    out_path = _log_file(service_name, "out")
    err_path = _log_file(service_name, "err")
    pid = _read_pid(pid_path)
    health = http_json_health(str(spec["health_url"]))
    port_open = _port_open(str(spec["host"]), int(spec["port"]))
    return {
        "service_key": spec["service_key"],
        "service_name": service_name,
        "host": spec["host"],
        "port": spec["port"],
        "cwd": str(spec["cwd"]),
        "command": [str(part) for part in spec["command"]],
        "pid_file": str(pid_path),
        "pid_file_exists": pid_path.exists(),
        "pid": pid,
        "pid_running": _pid_running(pid),
        "stdout_log": str(out_path),
        "stderr_log": str(err_path),
        "stderr_tail": _tail_lines(err_path),
        "port_open": port_open,
        "health": health,
        "healthy": bool(port_open and health.get("ok")),
    }


def _start_service(spec: dict[str, Any]) -> int:
    service_name = str(spec["service_name"])
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    _SERVICE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _log_file(service_name, "out")
    err_path = _log_file(service_name, "err")
    env_prefix = " ".join(
        f"{shlex.quote(str(key))}={shlex.quote(str(value))}" for key, value in (spec.get("env") or {}).items()
    )
    cmd = " ".join(shlex.quote(str(part)) for part in spec["command"])
    if env_prefix:
        cmd = f"env {env_prefix} {cmd}"
    script = (
        f"nohup {cmd} </dev/null >> {shlex.quote(str(out_path))} "
        f"2>> {shlex.quote(str(err_path))} & echo $!"
    )
    launched = subprocess.run(
        ["bash", "-lc", script],
        cwd=str(spec["cwd"]),
        capture_output=True,
        text=True,
        check=True,
        start_new_session=True,
    )
    pid_text = (launched.stdout or "").strip().splitlines()[-1]
    pid = int(pid_text)
    _pid_file(service_name).write_text(f"{pid}\n", encoding="utf-8")
    return pid


def _wait_for_health(spec: dict[str, Any], timeout_seconds: float = _STARTUP_TIMEOUT_SECONDS) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    snapshot = _service_snapshot(spec)
    while time.time() < deadline:
        if snapshot["healthy"]:
            return snapshot
        time.sleep(_POLL_INTERVAL_SECONDS)
        snapshot = _service_snapshot(spec)
    return snapshot


def gui_service_status_snapshot() -> dict[str, Any]:
    services = {spec["service_key"]: _service_snapshot(spec) for spec in SERVICE_SPECS}
    health_checks = {key: value["health"] for key, value in services.items()}
    overall_ready = all(service["healthy"] for service in services.values())
    return {
        "status": "ready" if overall_ready else "degraded",
        "gui_url": GUI_URL,
        "fxalpha_api": services["fxalpha_api"],
        "quantgpt_api": services["quantgpt_api"],
        "health_checks": health_checks,
        "operator_note": (
            "Services are healthy. Open gui_url in Codex browser."
            if overall_ready
            else "One or more GUI services are unavailable or unhealthy. Inspect health_checks and stderr_tail."
        ),
        "open_in_codex_browser_hint": f"Open {GUI_URL} in Codex browser after the GUI services are healthy.",
    }


def ensure_gui_services_started() -> dict[str, Any]:
    started_services: list[str] = []
    already_healthy_services: list[str] = []
    unhealthy_existing_services: list[str] = []

    snapshots: dict[str, dict[str, Any]] = {}
    for spec in SERVICE_SPECS:
        snapshot = _service_snapshot(spec)
        snapshots[spec["service_key"]] = snapshot
        if snapshot["healthy"]:
            already_healthy_services.append(str(spec["service_key"]))
        elif snapshot["port_open"]:
            unhealthy_existing_services.append(str(spec["service_key"]))
        else:
            _start_service(spec)
            started_services.append(str(spec["service_key"]))

    for spec in SERVICE_SPECS:
        if spec["service_key"] in started_services:
            snapshots[spec["service_key"]] = _wait_for_health(spec)
        else:
            snapshots[spec["service_key"]] = _service_snapshot(spec)

    health_checks = {key: value["health"] for key, value in snapshots.items()}
    overall_ready = all(service["healthy"] for service in snapshots.values())
    status = "ready" if overall_ready else "degraded"
    operator_note = "Services are healthy. Open gui_url in Codex browser."
    if unhealthy_existing_services:
        operator_note = (
            "Some GUI services were already listening but unhealthy, so they were left untouched by idempotent start. "
            "Inspect health_checks and stderr_tail."
        )
    elif not overall_ready:
        operator_note = "GUI services did not become healthy within the startup timeout. Inspect health_checks and stderr_tail."

    return {
        "status": status,
        "gui_url": GUI_URL,
        "fxalpha_api": snapshots["fxalpha_api"],
        "quantgpt_api": snapshots["quantgpt_api"],
        "started_services": started_services,
        "already_healthy_services": already_healthy_services,
        "unhealthy_existing_services": unhealthy_existing_services,
        "health_checks": health_checks,
        "operator_note": operator_note,
        "open_in_codex_browser_hint": f"Open {GUI_URL} in Codex browser after the GUI services are healthy.",
    }
