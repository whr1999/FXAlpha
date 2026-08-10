from __future__ import annotations

import json
import gzip
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from domain.platform_evaluation import EvaluationProfileError
from services.data_foundation_service import (
    data_benchmark_series,
    data_daily_routine,
    data_live_status,
    data_promote_staged,
    data_query,
    data_query_fields,
    data_stage_update,
    data_status,
    data_update_start,
    stock_metadata_refresh,
    stock_metadata_status,
)
from services.factor_research_service import (
    factor_research_add_guidance,
    factor_research_console_full,
    factor_research_console_live,
    factor_research_console_state,
    factor_research_control_state,
    factor_research_orchestrator_events,
    factor_research_orchestrator_traces,
    factor_research_run_view,
    factor_research_preflight,
    factor_research_pause,
    factor_research_reset,
    factor_research_runtime_defaults,
    factor_research_resume,
    factor_research_start,
    factor_research_stop,
    factor_research_update_config_defaults,
    factor_tool_classify_factor,
    factor_tool_context,
    factor_tool_import,
    factor_tool_novelty_check,
    factor_tool_quality_gate,
    factor_tool_record_orchestrator_event,
    factor_tool_record_research_step,
    factor_registry_duplicate_audit,
    factor_registry_list,
    factor_registry_retire_duplicates,
    factor_research_run,
    factor_seed_mine,
    factor_research_status,
    factor_submit_wq_active,
    factor_wq_status,
)
from services.factor_library_audit_service import (
    enqueue_factor_library_audit,
    factor_feature_set_recommendations,
    factor_library_audit,
    factor_library_audit_run_status,
    factor_library_audit_status,
    factor_retire_plan,
)
from services.factor_map_service import factor_map_status
from services.factor_non_st_migration_service import (
    factor_non_st_migration_execute,
    factor_non_st_migration_plan,
    factor_non_st_migration_status,
)
from services.factor_active_values_service import enqueue_active_values_refresh, factor_active_values_status
from services.pipeline_service import pipeline_run, pipeline_status
from services.maintenance_service import maintenance_cleanup, maintenance_status
from services.platform_runtime_service import platform_automation_control, platform_automation_status, platform_runtime_status
from services.platform_evaluation_service import platform_evaluation_set_mode, platform_evaluation_status
from storage.paths import (
    FACTOR_DEFAULT_COST_RATE,
    FACTOR_DEFAULT_END_DATE,
    FACTOR_DEFAULT_HOLDING_PERIOD,
    FACTOR_DEFAULT_MAX_STAGNATION_ROUNDS,
    FACTOR_DEFAULT_N_ROUNDS,
    FACTOR_DEFAULT_START_DATE,
    FACTOR_DEFAULT_TARGET_ADOPTED,
    FACTOR_DEFAULT_TOP_FRAC,
    FACTOR_DEFAULT_UNIVERSE,
    FACTOR_VALUE_DEFAULT_END_DATE,
    FACTOR_VALUE_DEFAULT_START_DATE,
    FACTOR_RESEARCH_DEFAULT_ORCHESTRATION_MODE,
    MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    MODEL_DEFAULT_FORWARD_PERIOD,
    MODEL_DEFAULT_START_DATE,
    MODEL_DEFAULT_STATUS_FILTER,
    MODEL_DEFAULT_TOPK,
    PROJECT_ROOT,
)

_OPTIONAL_IMPORT_ERRORS: dict[str, str] = {}


def _query_bool(query: dict, name: str, default: bool = False) -> bool:
    raw = (query.get(name) or [str(default).lower()])[0]
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _body_bool(body: dict, name: str, default: bool = False) -> bool:
    raw = body.get(name, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _body_value(body: dict, name: str, default):
    value = body.get(name)
    return default if value in (None, "") else value


def _body_list(body: dict, name: str) -> list[str] | None:
    value = body.get(name)
    if value in (None, ""):
        return None
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).replace("\n", ",").split(",") if item.strip()]

try:
    from services.daily_ops_service import daily_ops_routine, daily_ops_status
except Exception as exc:  # pragma: no cover - defensive bootstrapping
    _OPTIONAL_IMPORT_ERRORS["daily_ops"] = str(exc)
    daily_ops_routine = None
    daily_ops_status = None

try:
    from services.paper_fleet_service import (
        paper_account_create,
        paper_account_set_status,
        paper_fleet_preflight,
        paper_fleet_run,
        paper_fleet_status,
        paper_replay_plan,
        paper_replay_run,
    )
except Exception as exc:  # pragma: no cover - defensive bootstrapping
    _OPTIONAL_IMPORT_ERRORS["paper_fleet"] = str(exc)
    paper_account_create = None
    paper_account_set_status = None
    paper_fleet_preflight = None
    paper_fleet_run = None
    paper_fleet_status = None
    paper_replay_plan = None
    paper_replay_run = None

MODEL_PRODUCTION_MODULE = "model"
MODEL_GET_ALIASES = {
    "/model/research/orch-traces": "/model/orchestrator/traces",
    "/model/research/runs": "/model/runs",
}
MODEL_POST_ALIASES = {
    "/model/feature-refresh": "/model/tools/feature-snapshot",
    "/model/promote": "/model/promote",
    "/model/tools/context": "/model/tools/context",
    "/model/tools/protocol": "/model/tools/protocol",
    "/model/tools/monitor": "/model/tools/context",
    "/model/tools/feature-snapshot": "/model/tools/feature-snapshot",
    "/model/tools/session-start": "/model/tools/session-start",
    "/model/tools/submit-experiment": "/model/tools/submit-experiment",
    "/model/tools/run": "/model/tools/run-round",
    "/model/tools/validate": "/model/tools/confirm-research-round",
    "/model/tools/research-step": "/model/tools/research-step",
    "/model/orchestrator/start": "/model/orchestrator/start",
}

# Compatibility routes are read/write aliases only; all documentation and GUI
# traffic use /model.  They preserve automation written before the rename.
_LEGACY_MODEL_API_PREFIX = "/model0703"
_MODEL_GET_ROUTES = (
    "/status", "/feature-sets", "/preflight", "/backtest", "/runs",
    "/forward-tests", "/registry", "/production", "/research/current",
    "/research/journal", "/orchestrator/status", "/context/current",
    "/orchestrator/events", "/orchestrator/traces", "/mcp/traces",
)
_MODEL_POST_ROUTES = (
    "/tools/context", "/tools/protocol", "/tools/feature-snapshot",
    "/tools/session-start", "/tools/submit-experiment", "/tools/run-round",
    "/tools/score-review", "/tools/confirm-research-round",
    "/tools/start-production-rolling", "/tools/round-synthesis",
    "/tools/research-step", "/orchestrator/start", "/jobs/stop",
    "/jobs/resume", "/promote",
)
MODEL_GET_ALIASES.update({f"{_LEGACY_MODEL_API_PREFIX}{suffix}": f"/model{suffix}" for suffix in _MODEL_GET_ROUTES})
MODEL_POST_ALIASES.update({f"{_LEGACY_MODEL_API_PREFIX}{suffix}": f"/model{suffix}" for suffix in _MODEL_POST_ROUTES})

try:
    from services.prediction_service import pred_status, pred_status_snapshot, pred_update, score_export, target_build
except Exception as exc:  # pragma: no cover - defensive bootstrapping
    _OPTIONAL_IMPORT_ERRORS["prediction"] = str(exc)
    pred_status = None
    pred_status_snapshot = None
    pred_update = None
    score_export = None
    target_build = None

try:
    from services.model_service import (
        model_backtest,
        model_current_context,
        model_feature_sets,
        model_mcp_traces,
        model_orchestrator_events,
        model_job_resume,
        model_job_stop,
        model_orchestrator_start,
        model_orchestrator_status,
        model_orchestrator_traces,
        model_preflight_status,
        model_production,
        model_promote,
        model_registry,
        model_research_current,
        model_research_journal,
        model_runs,
        model_status,
        model_tool_context,
        model_tool_feature_snapshot,
        model_tool_confirm_research_round,
        model_tool_protocol,
        model_tool_research_step,
        model_tool_round_synthesis,
        model_tool_run_round,
        model_tool_score_review,
        model_tool_session_start,
        model_tool_start_production_rolling,
        model_tool_submit_experiment,
    )
except Exception as exc:  # pragma: no cover - defensive bootstrapping
    _OPTIONAL_IMPORT_ERRORS["model"] = str(exc)
    model_backtest = None
    model_current_context = None
    model_feature_sets = None
    model_mcp_traces = None
    model_orchestrator_events = None
    model_job_resume = None
    model_job_stop = None
    model_orchestrator_start = None
    model_orchestrator_status = None
    model_orchestrator_traces = None
    model_preflight_status = None
    model_production = None
    model_promote = None
    model_registry = None
    model_research_current = None
    model_research_journal = None
    model_runs = None
    model_status = None
    model_tool_context = None
    model_tool_feature_snapshot = None
    model_tool_confirm_research_round = None
    model_tool_protocol = None
    model_tool_research_step = None
    model_tool_round_synthesis = None
    model_tool_run_round = None
    model_tool_score_review = None
    model_tool_session_start = None
    model_tool_start_production_rolling = None
    model_tool_submit_experiment = None

try:
    from services.trading_service import (
        paper_trade_run,
        trading_daily_preflight,
        trading_daily_routine,
        trading_execute_pending,
        trading_paper_backfill,
        trading_recommend,
        trading_risk_policy_status,
        trading_risk_policy_update,
        trading_status,
    )
    from services.trading_ledger_export_service import build_trading_ledger_xlsx
except Exception as exc:  # pragma: no cover - defensive bootstrapping
    _OPTIONAL_IMPORT_ERRORS["trading"] = str(exc)
    paper_trade_run = None
    trading_daily_preflight = None
    trading_daily_routine = None
    trading_execute_pending = None
    trading_paper_backfill = None
    trading_recommend = None
    trading_risk_policy_status = None
    trading_risk_policy_update = None
    trading_status = None
    build_trading_ledger_xlsx = None

GUI_ROOT = Path(__file__).parent / "gui"
GUI_RUNTIME_FILES = {
    "codex_usage_snapshot.json": Path(__file__).parent / "runtime" / "platform" / "codex_usage_snapshot.json",
    "deepseek_usage_snapshot.json": Path(__file__).parent / "runtime" / "platform" / "deepseek_usage_snapshot.json",
    "factor_overview_snapshot.json": Path(__file__).parent / "runtime" / "platform" / "factor_overview_snapshot.json",
}
_GET_CACHE_TTLS = {
    "/platform/runtime-status": 30.0,
    "/maintenance/status": 60.0,
    "/data/status": 15.0,
    "/factor/console/live": 3.0,
    "/factor/console": 3.0,
    "/model/status": 5.0,
    "/model/registry": 10.0,
    "/factor/library/audit/status": 15.0,
    "/paper/status": 10.0,
    "/paper/fleet/status": 10.0,
    "/pipeline/status": 5.0,
    "/pred/status": 30.0,
    "/trade/status": 30.0,
    "/trade/risk-policy": 5.0,
}
_GET_CACHE_LOCK = threading.Lock()
_GET_RESPONSE_CACHE: dict[str, tuple[float, bytes]] = {}
_DATA_PREFLIGHT_LOCK = threading.Lock()
_DATA_PREFLIGHT_CACHE: dict[str, tuple[float, dict]] = {}
_DATA_PREFLIGHT_CACHE_TTL_SECONDS = 60.0
_LOCAL_ORIGIN_RE = re.compile(
    r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::[1-9][0-9]{0,4})?$",
    re.IGNORECASE,
)
_GUI_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _normalize_preflight_target_date(target_date: str | None) -> str:
    text = str(target_date or "auto").strip().lower()
    if text in {"", "auto"}:
        return "auto"
    compact = text.replace("-", "")
    if re.fullmatch(r"[0-9]{8}", compact) is None:
        raise ValueError("target_date must be auto, YYYYMMDD, or YYYY-MM-DD")
    try:
        parsed = datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("target_date must be a valid calendar date") from exc
    return parsed.strftime("%Y-%m-%d")


def _allowed_cors_origin(value: str | None) -> str | None:
    origin = str(value or "").strip()
    if origin == "null":
        return "null"
    if _LOCAL_ORIGIN_RE.fullmatch(origin) is None:
        return None
    parsed = urlparse(origin)
    try:
        port = parsed.port
    except ValueError:
        return None
    hostname = str(parsed.hostname or "").lower()
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    serialized_host = "[::1]" if hostname == "::1" else hostname
    port_suffix = f":{port}" if port is not None else ""
    return f"{parsed.scheme.lower()}://{serialized_host}{port_suffix}"


def _resolve_gui_asset(rel_path: str) -> Path | None:
    root = GUI_ROOT.resolve()
    relative = Path(rel_path)
    if relative.is_absolute():
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _gui_content_type(path: Path) -> str:
    return _GUI_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _isolated_data_daily_preflight(target_date: str = "auto") -> tuple[dict, int]:
    """Run the memory-heavy data preflight outside the long-lived API process."""
    try:
        target = _normalize_preflight_target_date(target_date)
    except ValueError as exc:
        return {
            "ok": False,
            "err": "invalid_target_date",
            "inputs": {"target_date": str(target_date or "")},
            "outputs": {"error": str(exc)},
            "artifacts": {},
            "warnings": [],
        }, 400
    now = time.monotonic()
    with _DATA_PREFLIGHT_LOCK:
        cached = _DATA_PREFLIGHT_CACHE.get(target)
        if cached is not None and cached[0] > now:
            return cached[1], 200 if cached[1].get("ok") else 500

        command = [
            sys.executable,
            str(PROJECT_ROOT / "cli.py"),
            "data-daily-preflight",
            "--target-date",
            target,
        ]
        try:
            # The date is reduced to "auto" or a validated calendar value and
            # passed as one argv element with shell=False.
            # codeql[py/command-line-injection]
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            payload = {
                "ok": False,
                "err": "data_daily_preflight_subprocess_failed",
                "inputs": {"target_date": target},
                "outputs": {"error": str(exc)},
                "artifacts": {},
                "warnings": [],
            }
            return payload, 500

        payload = None
        stdout = completed.stdout or ""
        candidates = [0] + [index + 1 for index, char in enumerate(stdout) if char == "\n"]
        for index in reversed(candidates):
            candidate = stdout[index:].strip()
            if not candidate.startswith("{"):
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                payload = parsed
                break
        if payload is None:
            payload = {
                "ok": False,
                "err": "data_daily_preflight_invalid_output",
                "inputs": {"target_date": target},
                "outputs": {
                    "returncode": completed.returncode,
                    "stdout_tail": stdout[-4000:],
                    "stderr_tail": (completed.stderr or "")[-4000:],
                },
                "artifacts": {},
                "warnings": [],
            }
        elif completed.returncode != 0 and payload.get("ok") is not False:
            payload = {
                "ok": False,
                "err": "data_daily_preflight_process_exit_nonzero",
                "inputs": {"target_date": target},
                "outputs": {
                    "returncode": completed.returncode,
                    "preflight": payload,
                    "stderr_tail": (completed.stderr or "")[-4000:],
                },
                "artifacts": {},
                "warnings": [],
            }

        status = 200 if payload.get("ok") else 500
        if status == 200:
            _DATA_PREFLIGHT_CACHE[target] = (
                time.monotonic() + _DATA_PREFLIGHT_CACHE_TTL_SECONDS,
                payload,
            )
        return payload, status


class APIHandler(BaseHTTPRequestHandler):
    def _send_download(self, body: bytes, *, filename: str, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_json_bytes(self, body: bytes, *, status: int = 200, cache_state: str | None = None) -> None:
        use_gzip = len(body) >= 16_384 and "gzip" in str(self.headers.get("Accept-Encoding") or "").lower()
        wire_body = gzip.compress(body, compresslevel=5) if use_gzip else body
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(wire_body)))
        self.send_header("Connection", "close")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        allowed_origin = _allowed_cors_origin(self.headers.get("Origin"))
        if allowed_origin is not None:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if cache_state:
            self.send_header("X-FXAlpha-Cache", cache_state)
        self.end_headers()
        try:
            self.wfile.write(wire_body)
        except (BrokenPipeError, ConnectionResetError):
            # A browser may cancel an obsolete polling request after a newer
            # refresh starts.  This is not an API failure and should not flood
            # the service journal with handler tracebacks.
            return

    def _serve_cached_get(self, path: str) -> bool:
        ttl = _GET_CACHE_TTLS.get(path)
        if ttl is None:
            return False
        now = time.monotonic()
        with _GET_CACHE_LOCK:
            cached = _GET_RESPONSE_CACHE.get(self.path)
            if cached is None or cached[0] <= now:
                _GET_RESPONSE_CACHE.pop(self.path, None)
                return False
            body = cached[1]
        self._send_json_bytes(body, cache_state="HIT")
        return True

    def _service_unavailable(self, module: str) -> None:
        self._send_json(
            {
                "ok": False,
                "error": "service_unavailable",
                "module": module,
                "detail": _OPTIONAL_IMPORT_ERRORS.get(module, f"{module} service is unavailable"),
            },
            status=503,
        )

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        path = urlparse(self.path).path
        ttl = _GET_CACHE_TTLS.get(path) if self.command == "GET" and status == 200 else None
        if ttl is not None:
            with _GET_CACHE_LOCK:
                _GET_RESPONSE_CACHE[self.path] = (time.monotonic() + ttl, body)
        self._send_json_bytes(body, status=status, cache_state="MISS" if ttl is not None else None)

    def do_OPTIONS(self):
        self._send_json({"ok": True})

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)
        if self._serve_cached_get(path):
            return
        if path == "/gui" or path == "/gui/":
            self._serve_gui_file("index.html")
            return
        if path.startswith("/gui/"):
            rel = path[len("/gui/"):]
            if not rel:
                rel = "index.html"
            self._serve_gui_file(rel)
            return
        if path == "/health":
            self._send_json(
                {
                    "ok": True,
                    "service": "fxalpha-api",
                    "modules": ["data_foundation", "factor_research", "model", "prediction", "trading", "pipeline", "maintenance"],
                    "factor_production_module": "factor_research",
                    "optional_import_errors": _OPTIONAL_IMPORT_ERRORS,
                }
            )
            return
        if path == "/platform/runtime-status":
            self._send_json(platform_runtime_status(compact=_query_bool(query, "compact", False)).to_dict())
            return
        if path == "/platform/automation-status":
            self._send_json(platform_automation_status().to_dict())
            return
        if path == "/platform/evaluation-profile":
            self._send_json(platform_evaluation_status().to_dict())
            return
        if path == "/data/status":
            self._send_json(data_status().to_dict())
            return
        if path == "/data/live-status":
            self._send_json(data_live_status().to_dict())
            return
        if path == "/data/query/fields":
            self._send_json(data_query_fields().to_dict())
            return
        if path == "/data/benchmark-series":
            result = data_benchmark_series(
                code=(query.get("code") or ["000300.SH"])[0],
                start=(query.get("start") or [None])[0] or None,
                end=(query.get("end") or [None])[0] or None,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if path == "/data/query":
            query_code = (query.get("code") or [""])[0]
            query_fields = (query.get("fields") or [None])[0]
            query_benchmark = (query.get("benchmark") or [None])[0] or None
            compact_code = str(query_code).strip().upper().replace(".", "")
            if compact_code == "000300SH" and str(query_fields or "").strip() == "close" and not query_benchmark:
                # Compatibility for already-open GUI tabs from before /data/benchmark-series.
                # Avoid loading the full production HDF just to render the paper-account benchmark.
                result = data_benchmark_series(
                    code="000300.SH",
                    start=(query.get("start") or [None])[0] or None,
                    end=(query.get("end") or [None])[0] or None,
                )
            else:
                result = data_query(
                    code=query_code,
                    start=(query.get("start") or [None])[0] or None,
                    end=(query.get("end") or [None])[0] or None,
                    fields=query_fields,
                    benchmark=query_benchmark,
                    transform=(query.get("transform") or ["index100"])[0] or "index100",
                )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if path == "/data/daily-preflight":
            payload, status = _isolated_data_daily_preflight(
                target_date=(query.get("target_date") or ["auto"])[0]
            )
            self._send_json(payload, status=status)
            return
        if path == "/data/stock-metadata":
            self._send_json(stock_metadata_status().to_dict())
            return
        if path in {"/factor/status", "/factor/research/status"}:
            self._send_json(factor_research_status().to_dict())
            return
        if path == "/factor/research/preflight":
            result = factor_research_preflight(qgpt_url=(query.get("qgpt_url") or [None])[0])
            self._send_json(result.to_dict())
            return
        if path == "/factor/research/control":
            result = (
                factor_research_control_state(include_services=False)
                if _query_bool(query, "compact", False)
                else factor_research_control_state()
            )
            self._send_json(result.to_dict())
            return
        if path == "/factor/console/live":
            self._send_json(factor_research_console_live().to_dict())
            return
        if path == "/factor/console":
            self._send_json(factor_research_console_state().to_dict())
            return
        if path == "/factor/console/full":
            self._send_json(factor_research_console_full().to_dict())
            return
        if path == "/factor/research/orchestrator-events":
            include_payload = str((query.get("include_payload") or ["false"])[0]).lower() in {"1", "true", "yes", "on"}
            include_history = str((query.get("include_history") or ["false"])[0]).lower() in {"1", "true", "yes", "on"}
            self._send_json(
                factor_research_orchestrator_events(
                    run_id=(query.get("run_id") or [None])[0],
                    limit=int((query.get("limit") or ["80"])[0] or 80),
                    include_payload=include_payload,
                    include_history=include_history,
                ).to_dict()
            )
            return
        if path == "/factor/research/orchestrator-traces":
            include_payload = str((query.get("include_payload") or ["false"])[0]).lower() in {"1", "true", "yes", "on"}
            include_history = str((query.get("include_history") or ["false"])[0]).lower() in {"1", "true", "yes", "on"}
            self._send_json(
                factor_research_orchestrator_traces(
                    run_id=(query.get("run_id") or [None])[0],
                    limit=int((query.get("limit") or ["50"])[0] or 50),
                    include_payload=include_payload,
                    include_history=include_history,
                ).to_dict()
            )
            return
        if path == "/factor/research/run-view":
            result = factor_research_run_view(
                run_id=(query.get("run_id") or [""])[0],
                limit=int((query.get("limit") or ["120"])[0] or 120),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if path == "/factors":
            self._send_json(
                factor_registry_list(
                    status=(query.get("status") or ["active"])[0],
                    category=(query.get("category") or ["all"])[0],
                    min_icir=float((query.get("min_icir") or ["0"])[0] or 0),
                    sort_by=(query.get("sort_by") or ["icir"])[0],
                    limit=int((query.get("limit") or ["200"])[0] or 200),
                    offset=int((query.get("offset") or ["0"])[0] or 0),
                    holding_period_days=(
                        int((query.get("holding_period_days") or [""])[0])
                        if (query.get("holding_period_days") or [""])[0]
                        else None
                    ),
                    compact=_query_bool(query, "compact", False),
                ).to_dict()
            )
            return
        if path == "/factor/registry/duplicates":
            self._send_json(factor_registry_duplicate_audit().to_dict())
            return
        if path == "/factor/library/audit/status":
            self._send_json(
                factor_library_audit_status(
                    scope=(query.get("scope") or ["all"])[0],
                    compact=_query_bool(query, "compact", False),
                ).to_dict()
            )
            return
        if path == "/factor/map":
            self._send_json(
                factor_map_status(region_uid=(query.get("region_uid") or [""])[0]).to_dict()
            )
            return
        if path == "/factor/library/audit/run-status":
            self._send_json(factor_library_audit_run_status().to_dict())
            return
        if path == "/factor/library/non-st-migration/status":
            self._send_json(factor_non_st_migration_status().to_dict())
            return
        if path == "/factor/active-values/status":
            self._send_json(factor_active_values_status().to_dict())
            return
        if path == "/factor/wq-status":
            self._send_json(factor_wq_status().to_dict())
            return
        if path == "/model/validation":
            self._send_json(
                {
                    "ok": False,
                    "error": "legacy_validation_endpoint_archived",
                    "message": "Use research confirmation and formal production rolling before promotion.",
                    "production_model_module": MODEL_PRODUCTION_MODULE,
                },
                status=410,
            )
            return
        path = MODEL_GET_ALIASES.get(path, path)
        if path == "/model/status":
            if model_status is None:
                self._service_unavailable("model")
                return
            payload = model_status(compact=True).to_dict()
            payload.setdefault("outputs", {})["production_model_module"] = MODEL_PRODUCTION_MODULE
            self._send_json(payload)
            return
        if path == "/model/feature-sets":
            if model_feature_sets is None:
                self._service_unavailable("model")
                return
            raw_limit = (query.get("limit") or ["0"])[0] or "0"
            result = model_feature_sets(
                limit=int(raw_limit) if str(raw_limit).isdigit() else None,
                compact=str((query.get("compact") or ["false"])[0]).lower() in {"1", "true", "yes", "on"},
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/preflight":
            if model_preflight_status is None:
                self._service_unavailable("model")
                return
            result = model_preflight_status(feature_set_id=(query.get("feature_set_id") or [None])[0])
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/backtest":
            if model_backtest is None:
                self._service_unavailable("model")
                return
            result = model_backtest(
                model_id=(query.get("model_id") or [None])[0],
                model_run_id=(query.get("model_run_id") or [None])[0],
                rolling_campaign_id=(query.get("rolling_campaign_id") or [None])[0],
                rolling_seed=int((query.get("rolling_seed") or ["42"])[0]) if str((query.get("rolling_seed") or ["42"])[0]).isdigit() else None,
                selector=(query.get("selector") or ["latest"])[0],
                include_daily=_query_bool(query, "include_daily", False),
                max_daily_holdings=int((query.get("max_daily_holdings") or ["30"])[0] or 30),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/runs":
            if model_runs is None:
                self._service_unavailable("model")
                return
            result = model_runs(
                round_group_id=(query.get("round_group_id") or [None])[0],
                limit=int((query.get("limit") or ["50"])[0] or 50),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/forward-tests":
            self._send_json({"ok": False, "error": "forward_test_removed", "replacement": "research_confirmation_and_production_rolling"}, status=410)
            return
        if path == "/model/registry":
            if model_registry is None:
                self._service_unavailable("model")
                return
            result = model_registry(
                status=(query.get("status") or ["library"])[0],
                include_archived=_query_bool(query, "include_archived", False),
                limit=int((query.get("limit") or ["0"])[0] or 0) or None,
                compact=_query_bool(query, "compact", False),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/production":
            if model_production is None:
                self._service_unavailable("model")
                return
            result = model_production()
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/research/current":
            if model_research_current is None:
                self._service_unavailable("model")
                return
            result = model_research_current()
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/research/journal":
            if model_research_journal is None:
                self._service_unavailable("model")
                return
            result = model_research_journal(limit=int((query.get("limit") or ["80"])[0] or 80))
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/orchestrator/status":
            if model_orchestrator_status is None:
                self._service_unavailable("model")
                return
            result = model_orchestrator_status()
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/context/current":
            if model_current_context is None:
                self._service_unavailable("model")
                return
            result = model_current_context(
                job_id=(query.get("job_id") or [None])[0],
                run_id=(query.get("run_id") or [None])[0],
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/orchestrator/events":
            if model_orchestrator_events is None:
                self._service_unavailable("model")
                return
            result = model_orchestrator_events(
                limit=int((query.get("limit") or ["80"])[0] or 80),
                include_payload=_query_bool(query, "include_payload", False),
                job_id=(query.get("job_id") or [None])[0],
                run_id=(query.get("run_id") or [None])[0],
                session_id=(query.get("session_id") or [None])[0],
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/orchestrator/traces":
            if model_orchestrator_traces is None:
                self._service_unavailable("model")
                return
            result = model_orchestrator_traces(
                limit=int((query.get("limit") or ["50"])[0] or 50),
                include_payload=_query_bool(query, "include_payload", False),
                job_id=(query.get("job_id") or [None])[0],
                run_id=(query.get("run_id") or [None])[0],
                session_id=(query.get("session_id") or [None])[0],
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/model/mcp/traces":
            if model_mcp_traces is None:
                self._service_unavailable("model")
                return
            result = model_mcp_traces(
                limit=int((query.get("limit") or ["50"])[0] or 50),
                include_payload=_query_bool(query, "include_payload", False),
                job_id=(query.get("job_id") or [None])[0],
                run_id=(query.get("run_id") or [None])[0],
                session_id=(query.get("session_id") or [None])[0],
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/pred/status":
            if pred_status_snapshot is None:
                self._service_unavailable("prediction")
                return
            self._send_json(pred_status_snapshot().to_dict())
            return
        if path == "/trade/status":
            if trading_status is None:
                self._service_unavailable("trading")
                return
            prediction = pred_status_snapshot() if pred_status_snapshot is not None else None
            self._send_json(
                trading_status(
                    model_id=(query.get("model_id") or [None])[0],
                    model_run_id=(query.get("model_run_id") or [None])[0],
                    prediction=prediction,
                    compact=_query_bool(query, "compact", False),
                ).to_dict()
            )
            return
        if path == "/trade/ledger/export":
            if build_trading_ledger_xlsx is None:
                self._service_unavailable("trading")
                return
            account_id = str((query.get("account_id") or [""])[0] or "").strip()
            if not account_id:
                self._send_json({"ok": False, "error": "account_id_required"}, status=400)
                return
            try:
                filename, workbook = build_trading_ledger_xlsx(
                    account_id=account_id,
                    trade_date=(query.get("trade_date") or [None])[0],
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=404)
                return
            self._send_download(
                workbook,
                filename=filename,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            return
        if path == "/trade/risk-policy":
            if trading_risk_policy_status is None:
                self._service_unavailable("trading")
                return
            try:
                history_days = int((query.get("history_days") or ["160"])[0] or 160)
            except (TypeError, ValueError):
                history_days = 160
            result = trading_risk_policy_status(
                account_id=(query.get("account_id") or [None])[0],
                history_days=max(20, min(history_days, 520)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/daily-ops/status":
            if daily_ops_status is None:
                self._service_unavailable("daily_ops")
                return
            self._send_json(daily_ops_status().to_dict())
            return
        if path in {"/paper/status", "/paper/fleet/status"}:
            if paper_fleet_status is None:
                self._service_unavailable("paper_fleet")
                return
            self._send_json(paper_fleet_status(compact=_query_bool(query, "compact", False)).to_dict())
            return
        if path == "/paper/fleet/preflight":
            if paper_fleet_preflight is None:
                self._service_unavailable("paper_fleet")
                return
            result = paper_fleet_preflight(target_date=(query.get("target_date") or [None])[0])
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/paper/replay/plan":
            if paper_replay_plan is None:
                self._service_unavailable("paper_fleet")
                return
            result = paper_replay_plan(
                account_id=str((query.get("account_id") or [""])[0]),
                from_date=(query.get("from_date") or [None])[0],
                to_date=(query.get("to_date") or [None])[0],
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if path == "/trade/daily-preflight":
            if trading_daily_preflight is None:
                self._service_unavailable("trading")
                return
            self._send_json(
                trading_daily_preflight(
                    model_id=(query.get("model_id") or [None])[0],
                    model_run_id=(query.get("model_run_id") or [None])[0],
                    signal_date=(query.get("signal_date") or [None])[0],
                    topk=int((query.get("topk") or [str(MODEL_DEFAULT_TOPK)])[0] or MODEL_DEFAULT_TOPK),
                    total_capital=float((query.get("total_capital") or ["1000000"])[0] or 1000000),
                ).to_dict()
            )
            return
        if path == "/pipeline/status":
            self._send_json(pipeline_status().to_dict())
            return
        if path == "/maintenance/status":
            self._send_json(
                maintenance_status(
                    include_disk_audit=_query_bool(query, "deep", False),
                ).to_dict()
            )
            return
        self._send_json({"ok": False, "error": "not_found"}, status=404)

    def _serve_gui_file(self, rel_path: str) -> None:
        runtime_file = GUI_RUNTIME_FILES.get(rel_path)
        if runtime_file is not None:
            if not runtime_file.exists() or not runtime_file.is_file():
                self._send_json({"ok": False, "error": "gui_runtime_snapshot_not_found"}, status=404)
                return
            full = runtime_file
        else:
            full = _resolve_gui_asset(rel_path)
            if full is None:
                self._send_json({"ok": False, "error": "gui_not_found"}, status=404)
                return
        body = full.read_bytes()
        parsed_request = urlparse(self.path)
        is_versioned_asset = bool(parse_qs(parsed_request.query).get("v")) and full.suffix.lower() in {".css", ".js"}
        use_gzip = (
            full.suffix.lower() in {".css", ".js", ".html"}
            and len(body) >= 16_384
            and "gzip" in str(self.headers.get("Accept-Encoding") or "").lower()
        )
        wire_body = gzip.compress(body, compresslevel=5) if use_gzip else body
        self.send_response(200)
        self.send_header("Content-Type", _gui_content_type(full))
        self.send_header("Content-Length", str(len(wire_body)))
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if is_versioned_asset:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(wire_body)

    def do_POST(self):
        with _GET_CACHE_LOCK:
            _GET_RESPONSE_CACHE.clear()
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "invalid_json"}, status=400)
            return
        confirm_execution = body.get("confirm") is True
        requested_dry_run = bool(body.get("dry_run", True))
        legacy_trading_writes = {
            "/trade/paper": "POST /paper/run",
            "/trade/recommend": "POST /paper/run",
            "/trade/execute-pending": "POST /paper/run",
            "/trade/paper-backfill": "POST /paper/replay",
            "/trade/daily-routine": "POST /paper/run",
            "/daily-ops/routine": "POST /paper/run",
        }
        if self.path in legacy_trading_writes:
            self._send_json(
                {
                    "ok": False,
                    "error": "legacy_trading_write_endpoint_retired",
                    "endpoint": self.path,
                    "replacement": legacy_trading_writes[self.path],
                    "detail": "Production paper-account writes are only accepted through the fleet/replay state machine.",
                },
                status=410,
            )
            return
        if self.path in {
            "/paper/accounts",
            "/paper/accounts/status",
            "/paper/run",
            "/paper/fleet/run",
            "/paper/replay",
            "/paper/replay/run",
        } and not confirm_execution:
            self._send_json(
                {"ok": False, "error": "paper_write_confirmation_required", "required": {"confirm": True}},
                status=400,
            )
            return
        if self.path == "/platform/automation-control" and not confirm_execution:
            self._send_json(
                {"ok": False, "error": "automation_write_confirmation_required", "required": {"confirm": True}},
                status=400,
            )
            return
        if self.path == "/trade/risk-policy" and not confirm_execution:
            self._send_json(
                {"ok": False, "error": "risk_policy_write_confirmation_required", "required": {"confirm": True}},
                status=400,
            )
            return
        if self.path in {"/data/stage-update", "/data/promote-staged", "/data/daily-routine", "/data/update/start"}:
            if not requested_dry_run and not confirm_execution:
                self._send_json(
                    {"ok": False, "error": "data_write_confirmation_required", "required": {"confirm": True}},
                    status=400,
                )
                return
        if self.path == "/data/daily-preflight":
            payload, status = _isolated_data_daily_preflight(
                target_date=body.get("target_date", "auto")
            )
            self._send_json(payload, status=status)
            return
        if self.path == "/paper/accounts":
            if paper_account_create is None:
                self._service_unavailable("paper_fleet")
                return
            result = paper_account_create(
                account_id=str(body.get("account_id") or ""),
                display_name=body.get("display_name"),
                account_mode=str(body.get("account_mode") or "fixed_model"),
                model_id=body.get("model_id"),
                model_run_id=body.get("model_run_id"),
                initial_capital=float(body.get("initial_capital", 1000000.0)),
                effective_from=str(body.get("effective_from") or ""),
                strategy_contract_version=str(body.get("strategy_contract_version") or "top20_drop2_hold5_open_v1"),
                topk=int(body.get("topk", MODEL_DEFAULT_TOPK)),
                n_drop=int(body.get("n_drop", 2)),
                hold_thresh=int(body.get("hold_thresh", 5)),
                deal_price=str(body.get("deal_price") or "open"),
                metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/paper/accounts/status":
            if paper_account_set_status is None:
                self._service_unavailable("paper_fleet")
                return
            result = paper_account_set_status(
                account_id=str(body.get("account_id") or ""),
                status=str(body.get("status") or ""),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path in {"/paper/run", "/paper/fleet/run"}:
            if paper_fleet_run is None:
                self._service_unavailable("paper_fleet")
                return
            result = paper_fleet_run(
                target_date=body.get("target_date"),
                confirm_long_replay=bool(body.get("confirm_long_replay", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path in {"/paper/replay", "/paper/replay/run"}:
            if paper_replay_run is None:
                self._service_unavailable("paper_fleet")
                return
            result = paper_replay_run(
                account_id=str(body.get("account_id") or ""),
                from_date=body.get("from_date"),
                to_date=body.get("to_date"),
                confirm_long_replay=bool(body.get("confirm_long_replay", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/platform/automation-control":
            result = platform_automation_control(
                workflow=str(body.get("workflow") or ""),
                action=str(body.get("action") or ""),
                schedule_time=body.get("schedule_time"),
                confirm=confirm_execution,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/data/stage-update":
            result = data_stage_update(
                target_date=body.get("target_date", "auto"),
                dry_run=requested_dry_run,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/data/promote-staged":
            result = data_promote_staged(
                package_id=body.get("package_id"),
                latest=bool(body.get("latest", False)),
                wait_idle=bool(body.get("wait_idle", False)),
                timeout_minutes=int(body.get("timeout_minutes", 180)),
                dry_run=requested_dry_run,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/data/daily-routine":
            result = data_daily_routine(
                target_date=body.get("target_date", "auto"),
                wait_idle=not bool(body.get("no_wait_idle", False)),
                timeout_minutes=int(body.get("timeout_minutes", 180)),
                dry_run=requested_dry_run,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/data/update/start":
            result = data_update_start(
                mode=body.get("mode", "daily"),
                target_date=body.get("target_date", "auto"),
                timeout_minutes=int(body.get("timeout_minutes", 180)),
                dry_run=requested_dry_run,
                confirm=confirm_execution,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/daily-ops/routine":
            if daily_ops_routine is None:
                self._service_unavailable("daily_ops")
                return
            result = daily_ops_routine(
                target_date=body.get("target_date", "auto"),
                timeout_minutes=int(body.get("timeout_minutes", 180)),
                topk=int(body.get("topk", MODEL_DEFAULT_TOPK)),
                total_capital=float(body.get("total_capital", 1000000.0)),
                dry_run=bool(body.get("dry_run", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/data/stock-metadata/refresh":
            result = stock_metadata_refresh(force=bool(body.get("force", False)))
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/maintenance/cleanup":
            execute = bool(body.get("execute", False))
            result = maintenance_cleanup(
                profile=body.get("profile", "safe"),
                execute=execute,
                retention_days=body.get("retention_days") if isinstance(body.get("retention_days"), dict) else None,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/submit-wq":
            result = factor_submit_wq_active(
                universe=body.get("universe", FACTOR_DEFAULT_UNIVERSE),
                min_icir=float(body.get("min_icir", 0.3)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/tools/context":
            result = factor_tool_context(
                evaluation_mode=str(body.get("evaluation_mode") or "").strip() or None,
                skip_quantgpt_probe=bool(body.get("skip_quantgpt_probe", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/tools/classify":
            result = factor_tool_classify_factor(
                expression=body.get("expression", ""),
                category=body.get("category", ""),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/tools/research-step":
            result = factor_tool_record_research_step(
                stage=body.get("stage", "note"),
                summary=body.get("summary", ""),
                decision=body.get("decision", ""),
                next_action=body.get("next") or body.get("next_action", ""),
                refs=body.get("refs", []),
                priority=body.get("priority", "normal"),
                run_id=body.get("run_id", ""),
                round_no=body.get("round_no") or body.get("round"),
                round_id=body.get("round_id", ""),
                stage_seq=body.get("stage_seq"),
                stage_id=body.get("stage_id", ""),
                previous_stage=body.get("previous_stage", ""),
                previous_stage_id=body.get("previous_stage_id", ""),
                stage_transition=body.get("stage_transition") if isinstance(body.get("stage_transition"), dict) else None,
                evidence_refs=body.get("evidence_refs") if isinstance(body.get("evidence_refs"), list) else None,
                tags=body.get("tags") if isinstance(body.get("tags"), list) else None,
                extra=body.get("extra") if isinstance(body.get("extra"), dict) else None,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/tools/orchestrator-event":
            result = factor_tool_record_orchestrator_event(
                event=body.get("event") if isinstance(body.get("event"), dict) else body,
                sync_research_step=bool(body.get("sync_research_step", True)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/tools/quality-gate":
            result = factor_tool_quality_gate(
                candidates=body.get("candidates", []),
                start_date=body.get("start_date", FACTOR_DEFAULT_START_DATE),
                end_date=body.get("end_date", FACTOR_DEFAULT_END_DATE),
                min_abs_ic=float(body.get("min_abs_ic", 0.02)),
                min_ir=float(body.get("min_ir", 0.3)),
                extra_existing_candidates=body.get("extra_existing_candidates", []),
                stage=body.get("stage", "round"),
                round_no=body.get("round_no"),
                family=body.get("family"),
                run_id=str(body.get("run_id") or ""),
                round_id=str(body.get("round_id") or ""),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/tools/novelty-check":
            result = factor_tool_novelty_check(
                candidates=body.get("candidates", []),
                start_date=body.get("start_date", FACTOR_DEFAULT_START_DATE),
                end_date=body.get("end_date", FACTOR_DEFAULT_END_DATE),
                extra_existing_candidates=body.get("extra_existing_candidates", []),
                pearson_threshold=float(body.get("pearson_threshold", 0.75)),
                rank_threshold=float(body.get("rank_threshold", 0.80)),
                p90_pearson_threshold=(
                    float(body["p90_pearson_threshold"])
                    if body.get("p90_pearson_threshold") is not None
                    else None
                ),
                p90_rank_threshold=(
                    float(body["p90_rank_threshold"])
                    if body.get("p90_rank_threshold") is not None
                    else None
                ),
                run_id=str(body.get("run_id") or ""),
                round_id=str(body.get("round_id") or ""),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/tools/import":
            result = factor_tool_import(
                candidates=body.get("candidates", []),
                universe=body.get("universe", FACTOR_DEFAULT_UNIVERSE),
                start_date=body.get("start_date", FACTOR_VALUE_DEFAULT_START_DATE),
                end_date=body.get("end_date", FACTOR_VALUE_DEFAULT_END_DATE),
                selection_start_date=body.get("selection_start_date", FACTOR_DEFAULT_START_DATE),
                selection_end_date=body.get("selection_end_date", FACTOR_DEFAULT_END_DATE),
                category=body.get("category", ""),
                submit_wq=bool(body.get("submit_wq", False)),
                run_id=str(body.get("run_id") or ""),
                round_id=str(body.get("round_id") or ""),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/research":
            result = factor_research_run(
                direction=body.get("direction", "auto"),
                universe=body.get("universe", FACTOR_DEFAULT_UNIVERSE),
                n_candidates=int(body.get("n_candidates", 10)),
                n_rounds=int(body.get("n_rounds", FACTOR_DEFAULT_N_ROUNDS)),
                target_adopted=int(body.get("target_adopted", FACTOR_DEFAULT_TARGET_ADOPTED)),
                qgpt_url=body.get("qgpt_url", "http://127.0.0.1:8003"),
                mcp_url=body.get("mcp_url"),
                max_agent_steps=min(300, max(4, int(body.get("max_agent_steps", 40)))),
                start_date=body.get("start_date", FACTOR_DEFAULT_START_DATE),
                end_date=body.get("end_date", FACTOR_DEFAULT_END_DATE),
                holding_period=int(body.get("holding_period", 5)),
                benchmark=body.get("benchmark", "hs300"),
                n_groups=int(body.get("n_groups", 5)),
                top_frac=float(body.get("top_frac", FACTOR_DEFAULT_TOP_FRAC)),
                cost_rate=float(body.get("cost_rate", FACTOR_DEFAULT_COST_RATE)),
                rebalance_anchor=body.get("rebalance_anchor"),
                neutralize_industry=bool(body.get("neutralize_industry", False)),
                neutralize_cap=_body_bool(body, "neutralize_cap", True),
                universe_date=body.get("universe_date"),
                seed_count=int(body.get("seed_count", 3)),
                seed_max_concurrent=int(body.get("seed_max_concurrent", 3)),
                max_direction_attempts=int(body.get("max_direction_attempts", 3)),
                max_stagnation_rounds=int(body.get("max_stagnation_rounds", FACTOR_DEFAULT_MAX_STAGNATION_ROUNDS)),
                poll_timeout_s=int(body.get("poll_timeout_s", 900)),
                min_abs_ic=float(body.get("min_abs_ic", 0.02)),
                min_ir=float(body.get("min_ir", 0.3)),
                auto_sessions=int(body.get("auto_sessions", 1)),
                seed_batch_rounds=int(body.get("seed_batch_rounds", 0)),
                seed_batch_max_candidates=int(body.get("seed_batch_max_candidates", 0)),
                dry_run=bool(body.get("dry_run", False)),
                submit_wq=bool(body.get("submit_wq", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/research/start":
            requested_evaluation_mode = str(body.get("evaluation_mode") or "").strip().lower() or None
            try:
                defaults = factor_research_runtime_defaults(evaluation_mode=requested_evaluation_mode)
            except EvaluationProfileError as exc:
                self._send_json(
                    {"ok": False, "err": "invalid_evaluation_profile", "outputs": {"detail": str(exc)}},
                    status=400,
                )
                return
            result = factor_research_start(
                direction=_body_value(body, "direction", "auto"),
                universe=_body_value(body, "universe", defaults.get("universe", FACTOR_DEFAULT_UNIVERSE)),
                n_candidates=int(_body_value(body, "n_candidates", defaults.get("n_candidates", 10))),
                n_rounds=int(_body_value(body, "n_rounds", defaults.get("n_rounds", FACTOR_DEFAULT_N_ROUNDS))),
                target_adopted=int(_body_value(body, "target_adopted", defaults.get("target_adopted", FACTOR_DEFAULT_TARGET_ADOPTED))),
                qgpt_url=_body_value(body, "qgpt_url", defaults.get("qgpt_url", "http://127.0.0.1:8003")),
                mcp_url=body.get("mcp_url"),
                max_agent_steps=min(300, max(4, int(body.get("max_agent_steps", 40)))),
                start_date=_body_value(body, "start_date", defaults.get("selection_start_date", FACTOR_DEFAULT_START_DATE)),
                end_date=_body_value(body, "end_date", defaults.get("selection_end_date", FACTOR_DEFAULT_END_DATE)),
                holding_period=int(_body_value(body, "holding_period", defaults.get("holding_period", 5))),
                benchmark=_body_value(body, "benchmark", defaults.get("benchmark", "hs300")),
                n_groups=int(body.get("n_groups", 5)),
                top_frac=float(_body_value(body, "top_frac", defaults.get("top_frac", FACTOR_DEFAULT_TOP_FRAC))),
                cost_rate=float(_body_value(body, "cost_rate", defaults.get("cost_rate", FACTOR_DEFAULT_COST_RATE))),
                rebalance_anchor=_body_value(body, "rebalance_anchor", defaults.get("rebalance_anchor")),
                neutralize_industry=_body_bool(body, "neutralize_industry", False),
                neutralize_cap=_body_bool(
                    body,
                    "neutralize_cap",
                    bool(defaults.get("neutralize_cap", defaults.get("default_neutralize_cap", True))),
                ),
                universe_date=_body_value(body, "universe_date", defaults.get("universe_date")),
                seed_count=int(body.get("seed_count", 3)),
                seed_max_concurrent=int(body.get("seed_max_concurrent", 3)),
                max_direction_attempts=int(body.get("max_direction_attempts", 3)),
                max_stagnation_rounds=int(body.get("max_stagnation_rounds", FACTOR_DEFAULT_MAX_STAGNATION_ROUNDS)),
                poll_timeout_s=int(body.get("poll_timeout_s", 900)),
                min_abs_ic=float(body.get("min_abs_ic", 0.02)),
                min_ir=float(body.get("min_ir", 0.3)),
                auto_sessions=int(body.get("auto_sessions", 1)),
                seed_batch_rounds=int(body.get("seed_batch_rounds", 0)),
                seed_batch_max_candidates=int(body.get("seed_batch_max_candidates", 0)),
                submit_wq=_body_bool(body, "submit_wq", False),
                orchestration_mode=str(_body_value(body, "orchestration_mode", FACTOR_RESEARCH_DEFAULT_ORCHESTRATION_MODE)),
                llm_model=str(body.get("llm_model") or "").strip() or None,
                llm_timeout_s=int(body.get("llm_timeout_s", 600)),
                resume_run_id=str(body.get("resume_run_id") or "").strip() or None,
                evaluation_mode=requested_evaluation_mode,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/platform/evaluation-profile":
            result = platform_evaluation_set_mode(
                evaluation_mode=str(body.get("evaluation_mode") or ""),
                changed_by=str(body.get("changed_by") or "web_gui"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/factor/research/stop":
            result = factor_research_stop(
                run_id=body.get("run_id") or None,
                reason=str(body.get("reason") or "operator_requested_stop"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/research/pause":
            result = factor_research_pause(
                run_id=body.get("run_id") or None,
                reason=str(body.get("reason") or "operator_requested_pause"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/research/resume":
            result = factor_research_resume(run_id=str(body.get("run_id") or "").strip())
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/factor/research/config-defaults":
            result = factor_research_update_config_defaults(body)
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/factor/research/guidance":
            result = factor_research_add_guidance(
                run_id=body.get("run_id", ""),
                message=body.get("message", ""),
                author=body.get("author", "operator"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/factor/seed-mine":
            result = factor_seed_mine(
                target_new=int(body.get("target_new", 6)),
                universe=body.get("universe", FACTOR_DEFAULT_UNIVERSE),
                start_date=body.get("start_date", FACTOR_DEFAULT_START_DATE),
                end_date=body.get("end_date", FACTOR_DEFAULT_END_DATE),
                holding_period=int(body.get("holding_period", 5)),
                max_candidates=int(body.get("max_candidates", 0)),
                dry_run=bool(body.get("dry_run", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/reset":
            result = factor_research_reset(
                clear_model_features=not bool(body.get("keep_model_features", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/registry/retire-duplicates":
            result = factor_registry_retire_duplicates(
                dry_run=bool(body.get("dry_run", True)),
                reason=body.get("reason", "duplicate_active_expression"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/library/audit/run":
            audit_kwargs = {
                "scope": body.get("scope", "all"),
                "status_filter": body.get("status_filter", "active"),
                "save_report": bool(body.get("save_report", True)),
                "include_feature_sets": bool(body.get("include_feature_sets", True)),
                "audit_window_start": body.get("audit_window_start"),
                "audit_window_end": body.get("audit_window_end"),
                "min_valid_days": body.get("min_valid_days", 120),
                "min_common_stocks": body.get("min_common_stocks", 300),
                "redundancy_threshold_rank_p90": body.get("redundancy_threshold_rank_p90", 0.80),
                "redundancy_threshold_pearson_p90": body.get("redundancy_threshold_pearson_p90", 0.75),
                "family_dependency_cut": body.get("family_dependency_cut", 0.55),
            }
            result = (
                enqueue_factor_library_audit(**audit_kwargs)
                if bool(body.get("async", False))
                else factor_library_audit(**audit_kwargs)
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/map/refresh":
            result = enqueue_factor_library_audit(
                scope="information",
                status_filter="active",
                save_report=True,
                include_feature_sets=True,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/library/audit/feature-sets":
            result = factor_feature_set_recommendations()
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/library/audit/retire-plan":
            result = factor_retire_plan()
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/library/non-st-migration/plan":
            result = factor_non_st_migration_plan(
                limit=int(body.get("limit", 0) or 0),
                offset=int(body.get("offset", 0) or 0),
                run_id=body.get("run_id"),
                holding_period_days=int(body.get("holding_period_days", FACTOR_DEFAULT_HOLDING_PERIOD)),
                selection_start_date=body.get("selection_start_date", FACTOR_DEFAULT_START_DATE),
                selection_end_date=body.get("selection_end_date", FACTOR_DEFAULT_END_DATE),
                value_start_date=body.get("value_start_date", FACTOR_VALUE_DEFAULT_START_DATE),
                value_end_date=body.get("value_end_date", FACTOR_VALUE_DEFAULT_END_DATE),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/factor/library/non-st-migration/execute":
            result = factor_non_st_migration_execute(
                run_id=str(body.get("run_id") or ""),
                confirm=str(body.get("confirm") or ""),
                refresh_model=bool(body.get("refresh_model", True)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/factor/active-values/refresh":
            state = enqueue_active_values_refresh(
                holding_period_days=int(body.get("holding_period_days", FACTOR_DEFAULT_HOLDING_PERIOD)),
                trigger=str(body.get("trigger", "api")),
                refresh_model=_body_bool(body, "refresh_model", True),
                dry_run=_body_bool(body, "dry_run", False),
                source_mode=str(body.get("source_mode") or "tail"),
            )
            self._send_json({"ok": state.get("ok", True) is not False, "outputs": state}, status=200 if state.get("ok", True) is not False else 400)
            return
        archived_model_posts = {
            "/model/train",
            "/model/resume",
            "/model/tools/prepare-hypothesis",
            "/model/tools/submit-hypothesis",
            "/model/tools/prepare-experiment",
            "/model/tools/develop",
            "/model/tools/prepare-feedback",
            "/model/tools/submit-feedback",
            "/model/tools/seed-stability",
            "/model/human-guidance",
        }
        if self.path in archived_model_posts:
            self._send_json(
                {
                    "ok": False,
                    "error": "legacy_model_endpoint_archived",
                    "message": "The legacy RD-Agent model workflow is archived. Production model training now uses model.",
                    "production_model_module": MODEL_PRODUCTION_MODULE,
                    "replacement_endpoints": [
                        "/model/tools/context",
                        "/model/tools/feature-snapshot",
                        "/model/tools/session-start",
                        "/model/tools/submit-experiment",
                        "/model/tools/run",
                        "/model/tools/validate",
                        "/model/orchestrator/start",
                        "/model/promote",
                    ],
                },
                status=410,
            )
            return
        if self.path in MODEL_POST_ALIASES:
            self.path = MODEL_POST_ALIASES[self.path]
        if self.path == "/model/tools/context":
            if model_tool_context is None:
                self._service_unavailable("model")
                return
            result = model_tool_context(
                stage=body.get("stage", "context_review"),
                round_group_id=body.get("round_group_id") or None,
                feature_set_id=body.get("feature_set_id") or None,
                job_id=body.get("job_id"),
                run_id=body.get("run_id"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/model/tools/protocol":
            if model_tool_protocol is None:
                self._service_unavailable("model")
                return
            result = model_tool_protocol()
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/model/tools/feature-snapshot":
            if model_tool_feature_snapshot is None:
                self._service_unavailable("model")
                return
            result = model_tool_feature_snapshot(
                feature_set_id=body.get("feature_set_id"),
                status_filter=body.get("status_filter", MODEL_DEFAULT_STATUS_FILTER),
                start_date=body.get("start_date", MODEL_DEFAULT_START_DATE),
                end_date=body.get("end_date"),
                label_forward_period=int(body.get("label_forward_period", MODEL_DEFAULT_FORWARD_PERIOD)),
                factor_holding_period_days=int(body.get("factor_holding_period_days", MODEL_DEFAULT_FACTOR_HOLDING_PERIOD)),
                factor_ids=_body_list(body, "factor_ids"),
                feature_missing_strategy=body.get("feature_missing_strategy", "qlib_processor_only"),
                dry_run=_body_bool(body, "dry_run", False),
                source_feature_set_id=body.get("source_feature_set_id") or None,
                source_type=body.get("source_type") or None,
                recommendation_family=body.get("recommendation_family") or None,
                audit_recommendation_id=body.get("audit_recommendation_id") or None,
                provenance_note=body.get("provenance_note") or None,
                job_id=body.get("job_id") or body.get("run_id") or "",
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/model/tools/session-start":
            if model_tool_session_start is None:
                self._service_unavailable("model")
                return
            result = model_tool_session_start(feature_set_id=body.get("feature_set_id"), job_id=body.get("job_id"), run_id=body.get("run_id"))
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/model/tools/submit-experiment":
            if model_tool_submit_experiment is None:
                self._service_unavailable("model")
                return
            result = model_tool_submit_experiment(
                feature_set_id=body.get("feature_set_id") or (body.get("experiment_json") or body.get("experiment") or {}).get("feature_set_id") or "",
                experiment=body.get("experiment_json") or body.get("experiment") or {},
                job_id=body.get("job_id"),
                run_id=body.get("run_id"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/model/tools/run-round":
            if model_tool_run_round is None:
                self._service_unavailable("model")
                return
            result = model_tool_run_round(
                round_group_id=body.get("round_group_id", ""),
                execute_qlib=_body_bool(body, "execute_qlib", False),
                job_id=body.get("job_id"),
                run_id=body.get("run_id"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/model/tools/score-review":
            if model_tool_score_review is None:
                self._service_unavailable("model")
                return
            result = model_tool_score_review(round_group_id=body.get("round_group_id", ""), job_id=body.get("job_id"), run_id=body.get("run_id"))
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/model/tools/confirm-research-round":
            if model_tool_confirm_research_round is None:
                self._service_unavailable("model")
                return
            result = model_tool_confirm_research_round(
                round_group_id=body.get("round_group_id", ""),
                execute_qlib=_body_bool(body, "execute_qlib", False),
                write_registry=_body_bool(body, "write_registry", True),
                job_id=body.get("job_id"),
                run_id=body.get("run_id"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/model/tools/start-production-rolling":
            if model_tool_start_production_rolling is None:
                self._service_unavailable("model")
                return
            result = model_tool_start_production_rolling(
                source_round_group_id=body.get("source_round_group_id", ""),
                write_registry=_body_bool(body, "write_registry", True),
                campaign_id=body.get("campaign_id") or None,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/model/tools/round-synthesis":
            if model_tool_round_synthesis is None:
                self._service_unavailable("model")
                return
            result = model_tool_round_synthesis(
                round_group_id=body.get("round_group_id", ""),
                round_no=int(body.get("round_no", 1)),
                write_registry=_body_bool(body, "write_registry", False),
                job_id=body.get("job_id"),
                run_id=body.get("run_id"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/model/tools/research-step":
            if model_tool_research_step is None:
                self._service_unavailable("model")
                return
            result = model_tool_research_step(
                stage=body.get("stage", "note"),
                summary=body.get("summary", ""),
                decision=body.get("decision", ""),
                next=body.get("next", ""),
                refs=body.get("refs") or [],
                extra=body.get("extra") or {},
                round_group_id=body.get("round_group_id", ""),
                model_run_id=body.get("model_run_id", ""),
                feature_set_id=body.get("feature_set_id", ""),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/model/orchestrator/start":
            if model_orchestrator_start is None:
                self._service_unavailable("model")
                return
            result = model_orchestrator_start(
                evaluation_mode=body.get("evaluation_mode", "research"),
                feature_set_id=body.get("feature_set_id"),
                source_round_group_id=body.get("source_round_group_id"),
                n_rounds=int(body.get("n_rounds", 1)),
                max_stage=body.get("max_stage", "round_synthesis"),
                run_id=body.get("run_id") or None,
                session_id=body.get("session_id") or body.get("resume_session_id") or None,
                parent_job_id=body.get("parent_job_id") or None,
                execute_qlib=bool(body.get("execute_qlib", False)),
                write_registry=bool(body.get("write_registry", False)),
                baseline_model_params=body.get("baseline_model_params") or None,
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/model/jobs/stop":
            if model_job_stop is None:
                self._service_unavailable("model")
                return
            result = model_job_stop(job_id=body.get("job_id") or None)
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/model/jobs/resume":
            if model_job_resume is None:
                self._service_unavailable("model")
                return
            result = model_job_resume(job_id=str(body.get("job_id") or ""))
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/model/promote":
            if model_promote is None:
                self._service_unavailable("model")
                return
            result = model_promote(
                model_id=body.get("model_id"),
                model_run_id=body.get("model_run_id"),
                execute_qlib=_body_bool(body, "execute_qlib", True),
                dry_run=_body_bool(body, "dry_run", False),
                manual_override_reason=body.get("manual_override_reason"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/pred/update":
            if pred_update is None:
                self._service_unavailable("prediction")
                return
            result = pred_update(
                model_id=body.get("model_id"),
                model_run_id=body.get("model_run_id"),
                to_date=body.get("to_date"),
                from_date=body.get("from_date"),
                dry_run=bool(body.get("dry_run", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/score/export":
            if score_export is None:
                self._service_unavailable("prediction")
                return
            result = score_export(
                model_id=body.get("model_id"),
                model_run_id=body.get("model_run_id"),
                as_of_date=body.get("as_of_date"),
                topk=body.get("topk"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/target/build":
            if target_build is None:
                self._service_unavailable("prediction")
                return
            result = target_build(
                model_id=body.get("model_id"),
                model_run_id=body.get("model_run_id"),
                topk=int(body.get("topk", MODEL_DEFAULT_TOPK)),
                weighting=body.get("weighting", "equal"),
                total_capital=body.get("total_capital"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/trade/paper":
            if paper_trade_run is None:
                self._service_unavailable("trading")
                return
            result = paper_trade_run(
                model_id=body.get("model_id"),
                model_run_id=body.get("model_run_id"),
                topk=int(body.get("topk", MODEL_DEFAULT_TOPK)),
                total_capital=float(body.get("total_capital", 1000000.0)),
                ensure_pred_latest=not bool(body.get("skip_pred_update", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/trade/recommend":
            if trading_recommend is None:
                self._service_unavailable("trading")
                return
            result = trading_recommend(
                model_id=body.get("model_id"),
                model_run_id=body.get("model_run_id"),
                signal_date=body.get("signal_date"),
                execution_date=body.get("execution_date"),
                topk=int(body.get("topk", MODEL_DEFAULT_TOPK)),
                total_capital=float(body.get("total_capital", 1000000.0)),
                ensure_pred_latest=not bool(body.get("skip_pred_update", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/trade/risk-policy":
            if trading_risk_policy_update is None:
                self._service_unavailable("trading")
                return
            changes = body.get("config") if isinstance(body.get("config"), dict) else body
            changes = {key: value for key, value in changes.items() if key != "confirm"}
            result = trading_risk_policy_update(changes)
            self._send_json(result.to_dict(), status=200 if result.ok else 400)
            return
        if self.path == "/trade/daily-preflight":
            if trading_daily_preflight is None:
                self._service_unavailable("trading")
                return
            result = trading_daily_preflight(
                model_id=body.get("model_id"),
                model_run_id=body.get("model_run_id"),
                signal_date=body.get("signal_date"),
                topk=int(body.get("topk", MODEL_DEFAULT_TOPK)),
                total_capital=float(body.get("total_capital", 1000000.0)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/trade/execute-pending":
            if trading_execute_pending is None:
                self._service_unavailable("trading")
                return
            result = trading_execute_pending(
                recommendation_id=body.get("recommendation_id"),
                total_capital=body.get("total_capital"),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/trade/paper-backfill":
            if trading_paper_backfill is None:
                self._service_unavailable("trading")
                return
            result = trading_paper_backfill(
                model_id=body.get("model_id"),
                model_run_id=body.get("model_run_id"),
                target_date=body.get("target_date"),
                total_capital=float(body.get("total_capital", 1000000.0)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/trade/daily-routine":
            if trading_daily_routine is None:
                self._service_unavailable("trading")
                return
            result = trading_daily_routine(
                model_id=body.get("model_id"),
                model_run_id=body.get("model_run_id"),
                signal_date=body.get("signal_date"),
                topk=int(body.get("topk", MODEL_DEFAULT_TOPK)),
                total_capital=float(body.get("total_capital", 1000000.0)),
                ensure_pred_latest=not bool(body.get("skip_pred_update", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        if self.path == "/pipeline/run":
            result = pipeline_run(
                end_date=body.get("end_date", FACTOR_VALUE_DEFAULT_END_DATE),
                skip_download=bool(body.get("skip_download", True)),
                direction=body.get("direction", "auto"),
                universe=body.get("universe", FACTOR_DEFAULT_UNIVERSE),
                n_candidates=int(body.get("n_candidates", 12)),
                n_rounds=int(body.get("n_rounds", 6)),
                target_adopted=int(body.get("target_adopted", 10)),
                factor_sessions=int(body.get("factor_sessions", 3)),
                qgpt_url=body.get("qgpt_url", "http://127.0.0.1:8003"),
                mcp_url=body.get("mcp_url"),
                max_agent_steps=min(300, max(4, int(body.get("max_agent_steps", 60)))),
                start_date=body.get("start_date", FACTOR_DEFAULT_START_DATE),
                factor_end_date=body.get("factor_end_date", FACTOR_DEFAULT_END_DATE),
                holding_period=int(body.get("holding_period", 5)),
                benchmark=body.get("benchmark", "hs300"),
                top_frac=float(body.get("top_frac", FACTOR_DEFAULT_TOP_FRAC)),
                cost_rate=float(body.get("cost_rate", FACTOR_DEFAULT_COST_RATE)),
                rebalance_anchor=body.get("rebalance_anchor"),
                universe_date=body.get("universe_date"),
                seed_count=int(body.get("seed_count", 3)),
                seed_max_concurrent=int(body.get("seed_max_concurrent", 3)),
                max_direction_attempts=int(body.get("max_direction_attempts", 3)),
                max_stagnation_rounds=int(body.get("max_stagnation_rounds", 3)),
                model_family=body.get("model_family", "lgbm"),
                model_loop_n=body.get("model_loop_n", 1),
                model_step_n=body.get("model_step_n"),
                seed_batch_rounds=int(body.get("seed_batch_rounds", 0)),
                seed_batch_max_candidates=int(body.get("seed_batch_max_candidates", 0)),
                dry_run=bool(body.get("dry_run", False)),
            )
            self._send_json(result.to_dict(), status=200 if result.ok else 500)
            return
        self._send_json({"ok": False, "error": "not_found"}, status=404)

    def log_message(self, format, *args):
        pass


def start_api(host: str = "127.0.0.1", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), APIHandler)
    print(f"[fxalpha-api] serving on http://{host}:{port}")
    server.serve_forever()
