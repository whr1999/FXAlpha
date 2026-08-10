from __future__ import annotations

import ast
import json
import hashlib
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import asyncio
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from services._base import ServiceResult, err_result, ok_result
from domain.platform_evaluation import EvaluationProfileError, resolve_evaluation_profile
from storage.factor_registry import FactorRegistry
from storage.paths import (
    CONFIG_FILE,
    FACTOR_DATA_ROOT,
    FACTOR_DEFAULT_HOLDING_PERIOD,
    FACTOR_DEFAULT_BENCHMARK,
    FACTOR_DEFAULT_COST_RATE,
    FACTOR_DEFAULT_END_DATE,
    FACTOR_DEFAULT_MAX_DIRECTION_ATTEMPTS,
    FACTOR_DEFAULT_MAX_STAGNATION_ROUNDS,
    FACTOR_DEFAULT_N_CANDIDATES,
    FACTOR_DEFAULT_N_ROUNDS,
    FACTOR_ROLLING_MAX_HISTORY_MONTHS,
    FACTOR_ROLLING_MIN_HISTORY_MONTHS,
    FACTOR_ROLLING_MIN_DATES_PER_6M,
    FACTOR_ROLLING_PERIOD_WEIGHTS,
    FACTOR_ROLLING_RANK_IC_FULL_SCORE,
    FACTOR_ROLLING_SCHEMA_VERSION,
    FACTOR_ROLLING_SCORE_POLICY_VERSION,
    FACTOR_ROLLING_STABILITY_PENALTY,
    FACTOR_ROLLING_TRAILING_HORIZONS,
    FACTOR_DEFAULT_SEED_COUNT,
    FACTOR_DEFAULT_SEED_MAX_CONCURRENT,
    FACTOR_ADOPTED_VALUES_FILE,
    FACTOR_DEFAULT_START_DATE,
    FACTOR_DEFAULT_TOP_FRAC,
    FACTOR_DEFAULT_REBALANCE_ANCHOR,
    FACTOR_DEFAULT_UNIVERSE_DATE,
    FACTOR_DEFAULT_TARGET_ADOPTED,
    FACTOR_DEFAULT_UNIVERSE,
    FACTOR_VALUE_DEFAULT_END_DATE,
    FACTOR_VALUE_DEFAULT_START_DATE,
    FACTOR_RESEARCH_DEFAULT_ORCHESTRATION_MODE,
    FACTOR_PARQUET_DIR,
    FACTOR_REGISTRY_DB,
    LATEST_MODEL_STATUS_FILE,
    LATEST_PIPELINE_STATUS_FILE,
    MODEL_ACTIVE_FEATURE_DIR,
    MODEL_ACTIVE_FEATURE_FILE,
    MODEL_ACTIVE_FEATURE_MANIFEST,
    MODEL_FEATURE_SETS_ROOT,
    QLIB_DATA_ROOT,
    QUANTGPT_API_URL,
    QUANTGPT_ADOPTED_VALUES_FILE,
    QUANTGPT_CODE_ROOT,
    QUANTGPT_DB,
    QUANTGPT_DATA_DIR,
    QUANTGPT_RESEARCH_NOTES_DIR,
    FACTOR_RESEARCH_ROOT,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_CROSS_REVIEW_MODEL,
    LLM_MODEL,
    LLM_PROVIDER,
    get_live_st_exposure_guard_mode,
    load_live_config,
)
from domain.factor_research.auto_import import FACTOR_CATEGORY_TAXONOMY, STANDARD_FACTOR_CATEGORIES, classify_factor_expression
from domain.factor_research.deepseek_client import DeepSeekClientError, DeepSeekJSONClient
from domain.factor_research.orchestrator_prompt_contract import (
    ORCHESTRATOR_RESEARCH_SYSTEM as PROMPT_CONTRACT_RESEARCH_SYSTEM,
    ORCHESTRATOR_STAGE_BRIEFINGS as PROMPT_CONTRACT_STAGE_BRIEFINGS,
)
from domain.factor_research import quality_gate
from domain.factor_research.orchestrator import (
    deep_advice,
    ensure_factor_naming,
    expression_profile,
    gate_advice,
    novelty_advice,
    quick_advice,
)
from domain.factor_research.orchestrator_context import OrchestratorContextPack
from domain.runtime_memory import release_process_memory
from services.factor_active_values_service import enqueue_active_values_refresh, factor_active_values_status
from services.factor_library_audit_service import factor_library_audit
from services.factor_map_service import factor_map_context, factor_map_design_context

_AUTO_FACTOR_RESEARCH_DIRECTIONS = [
    "Mine A-share value and quality factors around low valuation, earnings quality, and price-volume triggered re-rating.",
    "Mine A-share momentum and trend factors around information diffusion, trend continuation, and price-volume confirmation.",
    "Mine A-share reversal factors around overreaction repair, short-term mean reversion, and volume shock unwinds.",
    "Mine A-share low-volatility and risk-preference factors around volatility regime shifts, defensive preference, and turnover-based crowding.",
    "Mine A-share price-volume factors around VWAP deviation, abnormal volume, and turnover imbalance.",
    "Mine A-share size and liquidity factors around small-cap premium, liquidity pressure, and turnover segmentation.",
]

_GUI_RUNS_LOCK = threading.Lock()
_RESEARCH_STEPS_LOCK = threading.Lock()
FACTOR_RESEARCH_GUIDANCE_MAX_CHARS = 500
_ORCHESTRATOR_EVENTS_LOCK = threading.Lock()
_ORCHESTRATOR_LLM_TRACES_LOCK = threading.Lock()
_GUI_RUNS: dict[str, dict] = {}
_GUI_EVENT_LIMIT = 500
_GUI_ORPHANED_SECONDS = 10 * 60
_AUTOMATION_ACTIVE_STATUSES = {"automation_running"}
FACTOR_API_BOOT_TS = datetime.now()
FACTOR_RESEARCH_STEPS_DIR = FACTOR_RESEARCH_ROOT / "research_steps"
FACTOR_RESEARCH_STEPS_FILE = FACTOR_RESEARCH_STEPS_DIR / "current.jsonl"
FACTOR_RESEARCH_STEPS_HISTORY_DIR = FACTOR_RESEARCH_STEPS_DIR / "history"
FACTOR_RESEARCH_STEPS_MAX_LINES = 5000
FACTOR_RESEARCH_STEPS_MAX_BYTES = 16 * 1024 * 1024
FACTOR_ORCHESTRATOR_EVENTS_DIR = FACTOR_RESEARCH_ROOT / "orchestrator_events"
FACTOR_ORCHESTRATOR_EVENTS_FILE = FACTOR_ORCHESTRATOR_EVENTS_DIR / "current.jsonl"
FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR = FACTOR_ORCHESTRATOR_EVENTS_DIR / "history"
FACTOR_ORCHESTRATOR_EVENTS_MAX_LINES = 6000
FACTOR_ORCHESTRATOR_EVENTS_MAX_BYTES = 24 * 1024 * 1024
FACTOR_ORCHESTRATOR_LLM_TRACES_DIR = FACTOR_RESEARCH_ROOT / "orchestrator_llm_traces"
FACTOR_ORCHESTRATOR_LLM_TRACES_FILE = FACTOR_ORCHESTRATOR_LLM_TRACES_DIR / "current.jsonl"
FACTOR_ORCHESTRATOR_LLM_TRACES_HISTORY_DIR = FACTOR_ORCHESTRATOR_LLM_TRACES_DIR / "history"
FACTOR_ORCHESTRATOR_LLM_TRACES_MAX_LINES = 2000
FACTOR_ORCHESTRATOR_LLM_TRACES_MAX_BYTES = 32 * 1024 * 1024
FACTOR_ORCHESTRATOR_LLM_TIMEOUT_DEFAULT = 900
FACTOR_ORCHESTRATOR_LLM_TIMEOUT_MAX = 1800
FACTOR_ORCHESTRATOR_TOOL_TIMEOUT_DEFAULT = 720
FACTOR_ORCHESTRATOR_TOOL_TIMEOUT_MAX = 1800
FACTOR_ORCHESTRATOR_STALE_TASK_SECONDS = FACTOR_ORCHESTRATOR_TOOL_TIMEOUT_MAX + 300
FACTOR_ORCHESTRATOR_TOOL_WORKER_MEMORY_MAX = os.environ.get("FXALPHA_ORCH_TOOL_WORKER_MEMORY_MAX", "14G")
FACTOR_ORCHESTRATOR_LLM_REQUEST_BUDGET = 500
FACTOR_ORCHESTRATOR_LLM_PAYLOAD_CHAR_BUDGET = 20_000_000
FACTOR_ORCHESTRATOR_EVENT_BUDGET = 1600
FACTOR_ORCHESTRATOR_LLM_MODELS = ("deepseek-v4-pro", "deepseek-v4-flash")
_QUANTGPT_SELF_HEAL_LOCK = threading.Lock()
_QUANTGPT_SELF_HEAL_COOLDOWN_SECONDS = 90
_QUANTGPT_SELF_HEAL_RETRY_SLEEP_SECONDS = 1.5
_QUANTGPT_SELF_HEAL_MAX_RETRIES = 4
_QUANTGPT_SELF_HEAL_STATE: dict[str, Any] = {
    "last_attempt_ts": "",
    "last_success_ts": "",
    "last_result": "never_attempted",
    "last_error": "",
}
_ORCHESTRATOR_LLM_BUDGETS: dict[str, dict[str, int]] = {}


def _normalize_orchestrator_llm_model(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw == "deepseek-v4":
        raw = "deepseek-v4-pro"
    if raw not in FACTOR_ORCHESTRATOR_LLM_MODELS:
        raise ValueError(f"unsupported_orchestrator_llm_model:{raw or 'missing'}")
    return raw


def _default_orchestrator_llm_model() -> str:
    try:
        return _normalize_orchestrator_llm_model(LLM_MODEL)
    except ValueError:
        return "deepseek-v4-flash"


def _orchestrator_llm_client(inputs: dict[str, Any]) -> DeepSeekJSONClient:
    return DeepSeekJSONClient(
        model=_normalize_orchestrator_llm_model(
            inputs.get("llm_model") or _default_orchestrator_llm_model()
        ),
        timeout=max(
            4,
            min(
                FACTOR_ORCHESTRATOR_LLM_TIMEOUT_MAX,
                int(inputs.get("llm_timeout_s") or FACTOR_ORCHESTRATOR_LLM_TIMEOUT_DEFAULT),
            ),
        ),
    )


class OrchestratorStopRequested(RuntimeError):
    """Internal control signal for a graceful operator pause or stop."""

    def __init__(self, action: str = "stop", request_id: str = "") -> None:
        self.action = str(action or "stop").strip().lower()
        self.request_id = str(request_id or "").strip()
        super().__init__(f"orchestrator_{self.action}_requested")


class OrchestratorWorkerHandle:
    """Small process handle used by the API response and existing GUI snapshot."""

    def __init__(self, *, name: str, unit: str = "", pid: int | None = None, mode: str = "process"):
        self.name = name
        self.unit = unit
        self.pid = pid
        self.ident = pid
        self.mode = mode


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.tmp.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        tmp_path.replace(path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            raise


_FACTOR_CONFIG_UPDATE_FIELDS: dict[str, tuple[str, str]] = {
    "qgpt_url": ("default_qgpt_url", "str"),
    "default_qgpt_url": ("default_qgpt_url", "str"),
    "universe": ("default_universe", "str"),
    "default_universe": ("default_universe", "str"),
    "holding_period": ("default_holding_period", "int"),
    "default_holding_period": ("default_holding_period", "int"),
    "benchmark": ("default_benchmark", "str"),
    "default_benchmark": ("default_benchmark", "str"),
    "target_adopted": ("default_target_adopted", "int"),
    "default_target_adopted": ("default_target_adopted", "int"),
    "n_candidates": ("default_n_candidates", "int"),
    "default_n_candidates": ("default_n_candidates", "int"),
    "n_rounds": ("default_n_rounds", "int"),
    "default_n_rounds": ("default_n_rounds", "int"),
    "top_frac": ("default_top_frac", "float"),
    "default_top_frac": ("default_top_frac", "float"),
    "cost_rate": ("default_cost_rate", "float"),
    "default_cost_rate": ("default_cost_rate", "float"),
    "rebalance_anchor": ("default_rebalance_anchor", "str_or_null"),
    "default_rebalance_anchor": ("default_rebalance_anchor", "str_or_null"),
    "universe_date": ("default_universe_date", "str_or_null"),
    "default_universe_date": ("default_universe_date", "str_or_null"),
}

_FACTOR_CONFIG_FORBIDDEN_FIELDS = {
    "start_date",
    "default_start_date",
    "end_date",
    "default_end_date",
    "default_orchestration_mode",
    "orchestration_mode",
    "api_key",
    "token",
    "password",
    "client_secret",
    "llm",
    "paths",
    "data_foundation",
}


def _live_factor_research_config() -> dict[str, Any]:
    try:
        config = load_live_config()
    except Exception:
        config = {}
    factor_config = config.get("factor_research") if isinstance(config, dict) else {}
    return dict(factor_config or {}) if isinstance(factor_config, dict) else {}


def _config_int(config: dict, key: str, fallback: int) -> int:
    try:
        return int(config.get(key, fallback))
    except Exception:
        return int(fallback)


def _config_float(config: dict, key: str, fallback: float) -> float:
    try:
        return float(config.get(key, fallback))
    except Exception:
        return float(fallback)


def _config_str(config: dict, key: str, fallback: str | None) -> str | None:
    value = config.get(key, fallback)
    if value is None:
        return None
    return str(value)


def _universe_options() -> list[dict[str, Any]]:
    return [
        {
            "value": "tradable_non_st",
            "label": "tradable_non_st - 固定基准日无 ST（生产默认，2026-06-01）",
            "production_default": True,
            "warning": "",
        },
        {
            "value": "all_market",
            "label": "all_market - 全市场（诊断/遗留对比）",
            "production_default": False,
            "warning": "诊断用途；生产因子挖掘默认应使用 tradable_non_st。",
        },
        {"value": "hs300", "label": "hs300", "production_default": False, "warning": ""},
        {"value": "csi500", "label": "csi500", "production_default": False, "warning": ""},
        {"value": "csi1000", "label": "csi1000", "production_default": False, "warning": ""},
        {"value": "csi2000", "label": "csi2000", "production_default": False, "warning": ""},
    ]


def factor_research_runtime_defaults(evaluation_mode: str | None = None) -> dict[str, Any]:
    config = _live_factor_research_config()
    evaluation = resolve_evaluation_profile(evaluation_mode)
    factor_window = evaluation["factor"]
    return {
        "evaluation_mode": evaluation["evaluation_mode"],
        "active_default_evaluation_mode": evaluation["active_default_mode"],
        "profile_version": evaluation["profile_version"],
        "evaluation_profile_version": evaluation["profile_version"],
        "evaluation_contract_hash": evaluation["config_snapshot_hash"],
        "evidence_class": evaluation["evidence_class"],
        "model_evaluation": evaluation["model"],
        "qgpt_url": _config_str(config, "default_qgpt_url", QUANTGPT_API_URL),
        "default_orchestration_mode": str(
            config.get("default_orchestration_mode", FACTOR_RESEARCH_DEFAULT_ORCHESTRATION_MODE)
        ).strip().lower(),
        "llm_model": _default_orchestrator_llm_model(),
        "llm_model_options": list(FACTOR_ORCHESTRATOR_LLM_MODELS),
        "universe": _config_str(config, "default_universe", FACTOR_DEFAULT_UNIVERSE),
        "production_universe": "tradable_non_st",
        "diagnostic_universe": "all_market",
        "universe_options": _universe_options(),
        "st_exposure_guard_mode": get_live_st_exposure_guard_mode(),
        "st_exposure_guard_scope": "counterfactual_all_market",
        "st_exposure_guard_label": "distress_proxy_exposure",
        "selection_start_date": factor_window["selection_start_date"],
        "selection_end_date": factor_window["selection_end_date"],
        "value_start_date": factor_window["value_start_date"],
        "value_end_date": factor_window["value_end_date"],
        "holding_period": _config_int(config, "default_holding_period", FACTOR_DEFAULT_HOLDING_PERIOD),
        "rolling_validation": {
            "schema_version": FACTOR_ROLLING_SCHEMA_VERSION,
            "score_policy_version": FACTOR_ROLLING_SCORE_POLICY_VERSION,
            "max_history_months": FACTOR_ROLLING_MAX_HISTORY_MONTHS,
            "min_history_months": FACTOR_ROLLING_MIN_HISTORY_MONTHS,
            "period_weights": list(FACTOR_ROLLING_PERIOD_WEIGHTS),
            "stability_penalty": FACTOR_ROLLING_STABILITY_PENALTY,
            "rank_ic_full_score": FACTOR_ROLLING_RANK_IC_FULL_SCORE,
            "min_dates_per_6m": FACTOR_ROLLING_MIN_DATES_PER_6M,
            "trailing_horizons_months": list(FACTOR_ROLLING_TRAILING_HORIZONS),
        },
        "benchmark": _config_str(config, "default_benchmark", FACTOR_DEFAULT_BENCHMARK),
        "target_adopted": _config_int(config, "default_target_adopted", FACTOR_DEFAULT_TARGET_ADOPTED),
        "n_candidates": _config_int(config, "default_n_candidates", FACTOR_DEFAULT_N_CANDIDATES),
        "n_rounds": _config_int(config, "default_n_rounds", FACTOR_DEFAULT_N_ROUNDS),
        "seed_count": _config_int(config, "default_seed_count", FACTOR_DEFAULT_SEED_COUNT),
        "seed_max_concurrent": _config_int(config, "default_seed_max_concurrent", FACTOR_DEFAULT_SEED_MAX_CONCURRENT),
        "max_direction_attempts": _config_int(config, "default_max_direction_attempts", FACTOR_DEFAULT_MAX_DIRECTION_ATTEMPTS),
        "max_stagnation_rounds": _config_int(config, "default_max_stagnation_rounds", FACTOR_DEFAULT_MAX_STAGNATION_ROUNDS),
        "top_frac": _config_float(config, "default_top_frac", FACTOR_DEFAULT_TOP_FRAC),
        "cost_rate": _config_float(config, "default_cost_rate", FACTOR_DEFAULT_COST_RATE),
        "neutralize_cap": True,
        "default_neutralize_cap": True,
        "neutralize_industry": False,
        "default_neutralize_industry": False,
        "rebalance_anchor": _config_str(config, "default_rebalance_anchor", FACTOR_DEFAULT_REBALANCE_ANCHOR),
        "universe_date": _config_str(config, "default_universe_date", FACTOR_DEFAULT_UNIVERSE_DATE),
    }


def _coerce_factor_config_value(key: str, value: Any, value_type: str) -> Any:
    if value in ("", None):
        if value_type == "str_or_null":
            return None
        raise ValueError(f"{key} cannot be empty")
    if value_type == "int":
        coerced = int(value)
        if coerced < 0:
            raise ValueError(f"{key} must be non-negative")
        return coerced
    if value_type == "float":
        coerced = float(value)
        if coerced < 0:
            raise ValueError(f"{key} must be non-negative")
        return coerced
    if value_type == "str_or_null":
        return None if value in ("", None) else str(value).strip()
    return str(value).strip()


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _replace_factor_research_config_text(text: str, updates: dict[str, Any]) -> str:
    lines = text.splitlines()
    section_start = next((idx for idx, line in enumerate(lines) if line.strip() == "factor_research:" and not line.startswith(" ")), -1)
    if section_start < 0:
        lines.append("")
        lines.append("factor_research:")
        section_start = len(lines) - 1
    section_end = len(lines)
    for idx in range(section_start + 1, len(lines)):
        line = lines[idx]
        if line and not line.startswith((" ", "\t")) and not line.lstrip().startswith("#"):
            section_end = idx
            break
    existing: dict[str, int] = {}
    for idx in range(section_start + 1, section_end):
        match = re.match(r"^(\s{2})([A-Za-z0-9_]+)\s*:", lines[idx])
        if match:
            existing[match.group(2)] = idx
    insertions: list[str] = []
    for key, value in updates.items():
        rendered = f"  {key}: {_yaml_scalar(value)}"
        if key in existing:
            lines[existing[key]] = rendered
        else:
            insertions.append(rendered)
    if insertions:
        lines[section_end:section_end] = insertions
    return "\n".join(lines).rstrip() + "\n"


def factor_research_update_config_defaults(updates: dict[str, Any]) -> ServiceResult:
    raw_updates = updates or {}
    unknown = sorted(
        key for key in raw_updates
        if key not in _FACTOR_CONFIG_UPDATE_FIELDS and key not in {"persist", "save"}
    )
    forbidden = sorted(key for key in raw_updates if key in _FACTOR_CONFIG_FORBIDDEN_FIELDS)
    if unknown or forbidden:
        return err_result(
            "invalid_factor_config_default_fields",
            inputs={"requested_fields": sorted(raw_updates.keys())},
            outputs={
                "status": "blocked",
                "unknown_fields": unknown,
                "forbidden_fields": forbidden,
                "allowed_fields": sorted(_FACTOR_CONFIG_UPDATE_FIELDS.keys()),
            },
        )
    coerced: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for input_key, raw_value in raw_updates.items():
        if input_key in {"persist", "save"}:
            continue
        config_key, value_type = _FACTOR_CONFIG_UPDATE_FIELDS[input_key]
        try:
            coerced[config_key] = _coerce_factor_config_value(config_key, raw_value, value_type)
        except Exception as exc:
            errors[config_key] = str(exc)
    if errors:
        return err_result(
            "invalid_factor_config_default_values",
            inputs={"requested_fields": sorted(raw_updates.keys())},
            outputs={"status": "blocked", "field_errors": errors},
        )
    if not coerced:
        return ok_result(
            inputs={"requested_fields": []},
            outputs={
                "status": "unchanged",
                "updated_fields": [],
                "runtime_defaults": factor_research_runtime_defaults(),
            },
            artifacts={"config_file": str(CONFIG_FILE)},
        )
    try:
        original = CONFIG_FILE.read_text(encoding="utf-8")
        next_text = _replace_factor_research_config_text(original, coerced)
        parsed = yaml.safe_load(next_text) or {}
        factor_config = parsed.get("factor_research") if isinstance(parsed, dict) else {}
        if not isinstance(factor_config, dict):
            raise ValueError("factor_research section missing after update")
        for key, value in coerced.items():
            if factor_config.get(key) != value:
                raise ValueError(f"roundtrip mismatch for {key}")
        backup = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(CONFIG_FILE, backup)
        _atomic_write_text(CONFIG_FILE, next_text)
    except Exception as exc:
        return err_result(
            "factor_config_defaults_update_failed",
            inputs={"requested_fields": sorted(raw_updates.keys())},
            outputs={"status": "failed", "error": str(exc)[:500]},
            artifacts={"config_file": str(CONFIG_FILE)},
        )
    warnings = []
    if coerced.get("default_universe") == "all_market":
        warnings.append("all_market is intended for diagnostics or legacy comparison; production default is tradable_non_st.")
    return ok_result(
        inputs={"requested_fields": sorted(raw_updates.keys())},
        outputs={
            "status": "updated",
            "updated_fields": sorted(coerced.keys()),
            "runtime_defaults": factor_research_runtime_defaults(),
        },
        artifacts={"config_file": str(CONFIG_FILE), "backup_file": str(backup)},
        warnings=warnings,
    )


def _clip_text(value: Any, limit: int = 700) -> str:
    text = str(value or "").strip()
    return text[:limit]


_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "x-api-key",
    "secret",
    "credential",
    "credentials",
    "token",
    "refresh_token",
    "client_secret",
    "password",
    "cookie",
    "set-cookie",
    "authorization",
    "bearer",
    "private_key",
)
_NON_SECRET_USAGE_KEYS = {
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_output_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "completion_tokens_details",
    "prompt_tokens_details",
}
_NON_SECRET_SEMANTIC_KEYS = {
    # These are factor-research vocabulary, not authentication material.  Keep
    # the legacy names readable in old/new audit traces while prompt payloads
    # migrate to the clearer *_used / *_lengths names.
    "field_tokens",
    "operator_tokens",
    "window_tokens",
    "fields_used",
    "operators_used",
    "window_lengths",
}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_\-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{12,}", re.I),
)
_SECRET_URL_USERINFO_PATTERN = re.compile(r"([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^@\s/]+@", re.I)


def _redact_secret_text(value: str) -> tuple[str, int]:
    text = str(value)
    count = 0
    known_values = [secret for secret in (LLM_API_KEY,) if secret and len(str(secret)) >= 8]
    for secret in known_values:
        if str(secret) in text:
            text = text.replace(str(secret), "***REDACTED_SECRET***")
            count += 1
    for pattern in _SECRET_VALUE_PATTERNS:
        text, n = pattern.subn("***REDACTED_SECRET***", text)
        count += n
    text, n = _SECRET_URL_USERINFO_PATTERN.subn(r"\1***REDACTED_SECRET***@", text)
    count += n
    return text, count


def _redact_orchestrator_payload(value: Any) -> tuple[Any, int]:
    """Redact secret-looking fields and string values before audit persistence."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if (
                lowered not in _NON_SECRET_USAGE_KEYS
                and lowered not in _NON_SECRET_SEMANTIC_KEYS
                and any(marker in lowered for marker in _SECRET_KEY_MARKERS)
            ):
                redacted[key_text] = "***REDACTED_SECRET***"
                count += 1
                continue
            child, child_count = _redact_orchestrator_payload(item)
            redacted[key_text] = child
            count += child_count
        return redacted, count
    if isinstance(value, list):
        items = []
        count = 0
        for item in value:
            child, child_count = _redact_orchestrator_payload(item)
            items.append(child)
            count += child_count
        return items, count
    if isinstance(value, str):
        return _redact_secret_text(value)
    return value, 0


def _orchestrator_secret_residue_count(value: Any) -> int:
    try:
        text = json.dumps(_jsonable(value), ensure_ascii=False, default=str)
    except Exception:
        text = str(value or "")
    count = 0
    for secret in (LLM_API_KEY,):
        if secret and len(str(secret)) >= 8 and str(secret) in text:
            count += 1
    for pattern in _SECRET_VALUE_PATTERNS:
        count += len(pattern.findall(text))
    count += len(_SECRET_URL_USERINFO_PATTERN.findall(text))
    return count


def _strip_orchestrator_full_payload_fields(record: dict) -> dict:
    cleaned = dict(record or {})
    for key in (
        "system_prompt",
        "user_prompt",
        "payload",
        "llm_request",
        "llm_result",
        "result",
        "raw_response",
        "raw_response_preview",
    ):
        if key in cleaned:
            cleaned[key] = "***REDACTED_SECRET_PAYLOAD_REMOVED***"
    return cleaned


def _orchestrator_redaction_status(
    redacted_count: int,
    *,
    surface: str,
    redaction_warning: str = "",
    residual_secret_like_count: int = 0,
) -> dict[str, Any]:
    status = {
        "surface": surface,
        "redacted": bool(redacted_count or redaction_warning),
        "redacted_field_count": int(redacted_count or 0),
        "policy": "secret_keys_and_secret_like_strings",
    }
    if redaction_warning:
        status["redaction_warning"] = redaction_warning
    if residual_secret_like_count:
        status["residual_secret_like_count"] = int(residual_secret_like_count)
    return status


def _persist_job(job: dict) -> None:
    """Deprecated compatibility hook.

    The factor-research GUI now treats research_steps/current.jsonl as the
    realtime source of truth.  Legacy jobs/*.json compatibility has been
    removed from production factor mining because it created a second,
    drift-prone progress stream.
    """
    return


def _job_is_active(job: dict) -> bool:
    if job.get("status") == "waiting_codex_mcp":
        latest_ts = _job_latest_ts(job)
        if latest_ts and (datetime.now() - latest_ts).total_seconds() > 6 * 3600:
            return False
    return (
        job.get("status") in {"queued", "running", "waiting_codex_mcp", "research_active", *_AUTOMATION_ACTIVE_STATUSES}
        and job.get("stage") != "process_restarted"
    )


def _supersede_waiting_codex_jobs_unlocked(*, keep_run_id: str | None = None) -> None:
    """Keep only the latest Codex-MCP research run active in the GUI.

    Starting factor research in true MCP mode creates a research contract for
    the foreground Codex session. Older waiting runs should not keep showing as
    active after a new one is created.
    """
    for run_id, job in list(_GUI_RUNS.items()):
        if keep_run_id and run_id == keep_run_id:
            continue
        if job.get("status") != "waiting_codex_mcp":
            continue
        job["status"] = "superseded"
        job["stage"] = "superseded_by_new_codex_mcp_run"
        job["finished_at"] = job.get("finished_at") or _now_iso()
        _append_job_event(
            job,
            {
                "event": "job_superseded",
                "message": "Superseded by a newer Codex MCP research run.",
                "superseded_by": keep_run_id,
            },
        )
        _persist_job(job)


def _job_snapshot(job: dict) -> dict:
    return {
        "run_id": job.get("run_id"),
        "status": job.get("status"),
        "stage": job.get("stage"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "inputs": job.get("inputs", {}),
        "summary": job.get("summary", {}),
        "latest_event": job.get("latest_event"),
        "event_count": len(job.get("events", [])),
        "guidance_history": job.get("guidance_history", []),
        "events": list(job.get("events", []))[-250:],
        "latest_result": job.get("latest_result"),
    }


def _active_orchestrator_job_unlocked(*, exclude_run_id: str | None = None) -> dict | None:
    active_jobs: list[tuple[datetime, dict]] = []
    for run_id, job in _GUI_RUNS.items():
        if exclude_run_id and run_id == exclude_run_id:
            continue
        inputs = job.get("inputs") if isinstance(job.get("inputs"), dict) else {}
        if str(inputs.get("orchestration_mode") or "").strip().lower() != "orchestrator":
            continue
        if not _job_is_active(job):
            continue
        if not _orchestrator_thread_alive(str(run_id)):
            continue
        latest_ts = _job_latest_ts(job) or datetime.min
        active_jobs.append((latest_ts, job))
    if not active_jobs:
        external = _active_external_orchestrator_job(exclude_run_id=exclude_run_id)
        return external or None
    active_jobs.sort(key=lambda item: item[0], reverse=True)
    return active_jobs[0][1]


def _orchestrator_thread_alive(run_id: str) -> bool:
    """Return whether the run's thread or detached worker process is alive."""
    run_id = str(run_id or "").strip()
    if not run_id:
        return False
    expected_name = f"fxalpha-orchestrator-{run_id}"
    if any(thread.name == expected_name and thread.is_alive() for thread in threading.enumerate()):
        return True
    safe_run_marker = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_id).strip("-.")[-52:]
    if safe_run_marker:
        try:
            completed = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "list-units",
                    "fxalpha-factor-orch-*.service",
                    "--state=running",
                    "--plain",
                    "--no-legend",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
            if completed.returncode == 0 and any(
                safe_run_marker in line.split(None, 1)[0]
                for line in (completed.stdout or "").splitlines()
                if line.strip()
            ):
                return True
        except Exception:
            pass
    worker = _latest_orchestrator_worker_event(run_id)
    action = str(worker.get("worker_action") or "").strip().lower()
    if action not in {"launch_requested", "started"}:
        return False
    unit = str(worker.get("worker_unit") or "").strip()
    if unit:
        try:
            completed = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
            if completed.returncode == 0:
                return True
        except Exception:
            pass
    try:
        pid = int(worker.get("worker_pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 1:
        cmdline = Path(f"/proc/{pid}/cmdline")
        try:
            command = cmdline.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            return "orchestrator_worker.py" in command and run_id in command
        except Exception:
            pass
    return False


def _active_external_orchestrator_job(*, exclude_run_id: str | None = None) -> dict[str, Any]:
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        limit=300,
        max_lines_per_file=300,
        include_history=False,
    )
    checked: set[str] = set()
    for event in records:
        run_id = str(event.get("run_id") or "").strip()
        if not run_id or run_id == exclude_run_id or run_id in checked:
            continue
        checked.add(run_id)
        if not _orchestrator_thread_alive(run_id):
            continue
        worker = event if str(event.get("event_type") or "") == "orchestrator_worker" else {}
        return {
            "run_id": run_id,
            "status": "running",
            "stage": str(event.get("stage") or "orchestrator_worker"),
            "started_at": worker.get("ts") or event.get("ts"),
            "finished_at": None,
            "inputs": {"orchestration_mode": "orchestrator"},
            "events": deque(maxlen=_GUI_EVENT_LIMIT),
            "summary": {
                "orchestration": "detached_orchestrator_worker",
                "worker_unit": worker.get("worker_unit"),
                "worker_pid": worker.get("worker_pid"),
            },
            "latest_event": _compact_orchestrator_event(event),
        }
    return {}


def _tail_jsonl_lines(path: Path, *, max_lines: int = 500, max_bytes: int = 16 * 1024 * 1024) -> list[str]:
    """Return recent JSONL lines without loading a potentially huge live log."""
    line_limit = max(1, int(max_lines or 1))
    byte_limit = max(4096, int(max_bytes or 0))
    newest_first: list[str] = []
    collected_bytes = 0
    for line in _reverse_jsonl_lines(path):
        line_bytes = len(line.encode("utf-8", errors="ignore")) + 1
        if newest_first and collected_bytes + line_bytes > byte_limit:
            break
        newest_first.append(line)
        collected_bytes += line_bytes
        if len(newest_first) >= line_limit or collected_bytes >= byte_limit:
            break
    newest_first.reverse()
    return newest_first


def _journal_paths(current_file: Path, history_dir: Path) -> list[Path]:
    """Return the live cache first, followed by immutable daily journals."""
    paths: list[Path] = []
    if current_file.exists():
        paths.append(current_file)
    if history_dir.exists():
        paths.extend(sorted(history_dir.glob("*.jsonl"), reverse=True))
    return paths


def _reverse_jsonl_lines(path: Path, *, chunk_bytes: int = 1024 * 1024):
    """Yield complete JSONL lines newest first without loading a history file."""
    try:
        size = path.stat().st_size
    except Exception:
        return
    if size <= 0:
        return
    try:
        with path.open("rb") as handle:
            position = size
            remainder = b""
            while position > 0:
                read_size = min(max(4096, int(chunk_bytes)), position)
                position -= read_size
                handle.seek(position)
                block = handle.read(read_size)
                parts = block.split(b"\n")
                if remainder:
                    parts[-1] += remainder
                remainder = parts[0]
                for raw in reversed(parts[1:]):
                    if raw:
                        yield raw.decode("utf-8", errors="ignore")
            if remainder:
                yield remainder.decode("utf-8", errors="ignore")
    except Exception:
        return


def _read_recent_journal_records(
    *,
    current_file: Path,
    history_dir: Path,
    limit: int,
    run_id: str | None = None,
    max_lines_per_file: int | None = None,
    max_bytes_per_file: int = 16 * 1024 * 1024,
    include_history: bool = True,
) -> tuple[list[dict], dict[str, int]]:
    """Tail-read current/history journals without treating current as history.

    `current.jsonl` is a bounded cache.  Daily files remain the complete audit
    trail, so a run-scoped read continues into history when that run has aged
    out of the cache.  Records are returned newest first.
    """
    max_items = max(1, int(limit or 1))
    selected_run_id = str(run_id or "").strip()
    per_file = (
        max(max_items, int(max_lines_per_file))
        if max_lines_per_file is not None
        else max(1000, max_items * 20)
    )
    records: list[dict] = []
    seen: set[str] = set()
    metrics = {"scanned_lines": 0, "parse_errors": 0, "history_files_scanned": 0, "source_files": 0}
    paths = _journal_paths(current_file, history_dir)
    if not include_history:
        paths = [path for path in paths if path == current_file]
    for path in paths:
        if path != current_file:
            metrics["history_files_scanned"] += 1
        metrics["source_files"] += 1
        # A named, old run must remain queryable after it leaves current.  Daily
        # journals are read backwards in chunks; ordinary live reads still use
        # a bounded tail for predictable polling cost.
        if selected_run_id and path != current_file:
            lines: Any = _reverse_jsonl_lines(path)
        else:
            lines = reversed(_tail_jsonl_lines(path, max_lines=per_file, max_bytes=max_bytes_per_file))
        for line in lines:
            metrics["scanned_lines"] += 1
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except Exception:
                metrics["parse_errors"] += 1
                continue
            if not isinstance(record, dict):
                continue
            if selected_run_id and str(record.get("run_id") or "") != selected_run_id:
                continue
            # current is copied into the daily journal.  Dedupe the exact
            # serialized record while retaining legitimate repeated events.
            dedupe_key = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            records.append(record)
            if len(records) >= max_items:
                return records, metrics
    return records, metrics


def _append_bounded_journal_record(
    *,
    current_file: Path,
    history_path: Path,
    serialized: str,
    max_lines: int,
    max_bytes: int,
    lock: threading.Lock,
) -> None:
    """Append the full audit record to history and retain a bounded live cache."""
    current_file.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with lock:
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
        with current_file.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
        try:
            current_size = current_file.stat().st_size
        except Exception:
            current_size = 0
        # Avoid a full-file read even when upgrading an already oversized
        # runtime journal.  History remains untouched and queryable.
        probe = _tail_jsonl_lines(
            current_file,
            max_lines=max(1, int(max_lines)) + 1,
            max_bytes=max(4096, int(max_bytes)),
        )
        if current_size > int(max_bytes) or len(probe) > int(max_lines):
            retained = _tail_jsonl_lines(
                current_file,
                max_lines=max(1, int(max_lines)),
                max_bytes=max(4096, int(max_bytes)),
            )
            _atomic_write_text(current_file, "\n".join(retained) + ("\n" if retained else ""))


def _begin_factor_research_live_journals(
    run_id: str,
    *,
    include_orchestrator_journals: bool = True,
) -> None:
    """Start a fresh live-cache window while preserving complete history.

    Every journal row is durably appended to its daily history file before it
    reaches current.jsonl.  A formal new run can therefore clear only the three
    live caches; explicit resume keeps the existing window intact.
    """
    if not str(run_id or "").strip():
        return
    journals = [(FACTOR_RESEARCH_STEPS_FILE, _RESEARCH_STEPS_LOCK)]
    if include_orchestrator_journals:
        journals.extend(
            (
                (FACTOR_ORCHESTRATOR_EVENTS_FILE, _ORCHESTRATOR_EVENTS_LOCK),
                (FACTOR_ORCHESTRATOR_LLM_TRACES_FILE, _ORCHESTRATOR_LLM_TRACES_LOCK),
            )
        )
    for current_file, lock in journals:
        current_file.parent.mkdir(parents=True, exist_ok=True)
        with lock:
            _atomic_write_text(current_file, "")


def factor_research_compact_journals(*, dry_run: bool = True) -> ServiceResult:
    """Explicitly converge legacy oversized live journals without losing audit rows.

    Current files are caches, while daily history is the durable journal.  This
    maintenance action backfills only missing serialized records into history,
    then atomically keeps the existing bounded tail.  It is intentionally an
    explicit operation rather than a status-read side effect.
    """
    journals = (
        (
            "research_steps",
            FACTOR_RESEARCH_STEPS_FILE,
            FACTOR_RESEARCH_STEPS_HISTORY_DIR,
            FACTOR_RESEARCH_STEPS_MAX_LINES,
            FACTOR_RESEARCH_STEPS_MAX_BYTES,
            _RESEARCH_STEPS_LOCK,
        ),
        (
            "orchestrator_events",
            FACTOR_ORCHESTRATOR_EVENTS_FILE,
            FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
            FACTOR_ORCHESTRATOR_EVENTS_MAX_LINES,
            FACTOR_ORCHESTRATOR_EVENTS_MAX_BYTES,
            _ORCHESTRATOR_EVENTS_LOCK,
        ),
        (
            "orchestrator_llm_traces",
            FACTOR_ORCHESTRATOR_LLM_TRACES_FILE,
            FACTOR_ORCHESTRATOR_LLM_TRACES_HISTORY_DIR,
            FACTOR_ORCHESTRATOR_LLM_TRACES_MAX_LINES,
            FACTOR_ORCHESTRATOR_LLM_TRACES_MAX_BYTES,
            _ORCHESTRATOR_LLM_TRACES_LOCK,
        ),
    )
    receipt_root = FACTOR_RESEARCH_ROOT.parent / "archive" / "factor_research" / (
        "journal_compaction_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    reports: list[dict[str, Any]] = []
    for name, current_file, history_dir, max_lines, max_bytes, lock in journals:
        reports.append(
            _compact_factor_research_journal(
                name=name,
                current_file=current_file,
                history_dir=history_dir,
                max_lines=max_lines,
                max_bytes=max_bytes,
                lock=lock,
                receipt_root=receipt_root,
                dry_run=dry_run,
            )
        )
    return ok_result(
        inputs={"dry_run": bool(dry_run)},
        outputs={
            "status": "dry_run" if dry_run else "completed",
            "journals": reports,
            "receipt_root": str(receipt_root) if not dry_run else "",
        },
        artifacts={
            "research_root": str(FACTOR_RESEARCH_ROOT),
            "archive_root": str(receipt_root.parent),
        },
    )


def _compact_factor_research_journal(
    *,
    name: str,
    current_file: Path,
    history_dir: Path,
    max_lines: int,
    max_bytes: int,
    lock: threading.Lock,
    receipt_root: Path,
    dry_run: bool,
) -> dict[str, Any]:
    if not current_file.exists():
        return {"journal": name, "status": "missing_current", "current_file": str(current_file)}
    with lock:
        history_hashes = _journal_serialized_hashes(history_dir)
        source_hash = hashlib.sha256()
        valid_rows = 0
        malformed_rows = 0
        history_backfill_rows = 0
        receipt_dir = receipt_root / name
        malformed_path = receipt_dir / "malformed_current_rows.jsonl"
        handles: dict[Path, Any] = {}
        malformed_handle: Any = None
        if not dry_run:
            history_dir.mkdir(parents=True, exist_ok=True)
            receipt_dir.mkdir(parents=True, exist_ok=True)
            malformed_handle = malformed_path.open("w", encoding="utf-8")
        with current_file.open("r", encoding="utf-8", errors="replace") as handle:
            try:
                for raw_line in handle:
                    source_hash.update(raw_line.encode("utf-8", errors="replace"))
                    serialized = raw_line.rstrip("\n")
                    if not serialized.strip():
                        continue
                    try:
                        record = json.loads(serialized)
                    except Exception:
                        malformed_rows += 1
                        if malformed_handle is not None:
                            malformed_handle.write(serialized + "\n")
                        continue
                    if not isinstance(record, dict):
                        malformed_rows += 1
                        if malformed_handle is not None:
                            malformed_handle.write(serialized + "\n")
                        continue
                    valid_rows += 1
                    line_hash = _journal_line_hash(serialized)
                    if line_hash in history_hashes:
                        continue
                    history_backfill_rows += 1
                    if not dry_run:
                        day = _journal_record_day(record)
                        history_path = history_dir / f"{day}.jsonl"
                        history_handle = handles.get(history_path)
                        if history_handle is None:
                            history_handle = history_path.open("a", encoding="utf-8")
                            handles[history_path] = history_handle
                        history_handle.write(serialized + "\n")
                    history_hashes.add(line_hash)
            finally:
                for history_handle in handles.values():
                    history_handle.close()
                if malformed_handle is not None:
                    malformed_handle.close()
        retained = _tail_jsonl_lines(
            current_file,
            max_lines=max(1, int(max_lines)),
            max_bytes=max(4096, int(max_bytes)),
        )
        retained = [
            line
            for line in retained
            if _journal_line_is_valid_record(line)
        ]
        latest_run_id = next(
            (
                _journal_line_run_id(line)
                for line in reversed(retained)
                if _journal_line_run_id(line)
            ),
            "",
        )
        retained_before_run_filter = len(retained)
        if latest_run_id:
            retained = [line for line in retained if _journal_line_run_id(line) == latest_run_id]
        current_bytes = current_file.stat().st_size
        report = {
            "journal": name,
            "current_file": str(current_file),
            "history_dir": str(history_dir),
            "source_sha256": source_hash.hexdigest(),
            "current_bytes_before": current_bytes,
            "valid_rows": valid_rows,
            "malformed_rows": malformed_rows,
            "history_backfill_rows": history_backfill_rows,
            "retained_rows": len(retained),
            "retained_bytes": sum(len(line.encode("utf-8")) + 1 for line in retained),
            "retained_run_id": latest_run_id,
            "dropped_prior_run_rows": retained_before_run_filter - len(retained),
            "max_lines": int(max_lines),
            "max_bytes": int(max_bytes),
            "status": "would_compact" if (current_bytes > max_bytes or valid_rows > max_lines or malformed_rows or retained_before_run_filter != len(retained)) else "already_bounded",
        }
        if dry_run:
            return report

        if not malformed_rows:
            malformed_path.unlink(missing_ok=True)
        _atomic_write_text(current_file, "\n".join(retained) + ("\n" if retained else ""))
        report["current_bytes_after"] = current_file.stat().st_size if current_file.exists() else 0
        report["status"] = "compacted"
        _atomic_write_text(receipt_dir / "receipt.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report


def _journal_serialized_hashes(history_dir: Path) -> set[str]:
    hashes: set[str] = set()
    if not history_dir.exists():
        return hashes
    for path in sorted(history_dir.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    serialized = raw_line.rstrip("\n")
                    if serialized.strip():
                        hashes.add(_journal_line_hash(serialized))
        except OSError:
            continue
    return hashes


def _journal_line_hash(serialized: str) -> str:
    return hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()


def _journal_record_day(record: dict) -> str:
    value = str(record.get("ts") or "")[:10]
    try:
        datetime.fromisoformat(value)
        return value
    except ValueError:
        return datetime.now().strftime("%Y-%m-%d")


def _journal_line_is_valid_record(serialized: str) -> bool:
    try:
        return isinstance(json.loads(serialized), dict)
    except Exception:
        return False


def _journal_line_run_id(serialized: str) -> str:
    try:
        record = json.loads(serialized)
    except Exception:
        return ""
    return str(record.get("run_id") or "").strip() if isinstance(record, dict) else ""


def _latest_orchestrator_event_from_file() -> dict:
    if not FACTOR_ORCHESTRATOR_EVENTS_FILE.exists():
        return {}
    lines = _tail_jsonl_lines(FACTOR_ORCHESTRATOR_EVENTS_FILE, max_lines=800)
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        tags = {str(tag) for tag in event.get("tags") or []}
        if "orchestrator" in tags and event.get("run_id"):
            return event
    return {}


def _latest_orchestrator_event_for_run(run_id: str) -> dict:
    run_text = str(run_id or "").strip()
    if not run_text:
        return {}
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        run_id=run_text,
        limit=1,
    )
    return records[0] if records else {}


def _latest_orchestrator_llm_trace_from_file() -> dict:
    if not FACTOR_ORCHESTRATOR_LLM_TRACES_FILE.exists():
        return {}
    lines = _tail_jsonl_lines(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE, max_lines=400)
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            trace = json.loads(line)
        except Exception:
            continue
        if not isinstance(trace, dict):
            continue
        if trace.get("run_id") and trace.get("trace_id"):
            return trace
    return {}


def _orchestrator_event_is_terminal(event: dict) -> bool:
    stage = str(event.get("stage") or "").strip()
    event_type = str(event.get("event_type") or "").strip()
    tags = {str(tag) for tag in event.get("tags") or []}
    return stage in {"checkpoint_stop", "blocker", "stop"} or event_type == "blocker" or "checkpoint_stop" in tags


def _latest_terminal_orchestrator_event_for_run(run_id: str) -> dict:
    run_text = str(run_id or "").strip()
    if not run_text:
        return {}
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        run_id=run_text,
        limit=120,
    )
    return next(
        (event for event in records if _orchestrator_event_is_terminal(event)),
        {},
    )


def _orchestrator_event_is_interruption_blocker(event: dict) -> bool:
    if not isinstance(event, dict) or not event:
        return False
    tags = {str(tag) for tag in event.get("tags") or []}
    if "orchestrator_interrupted" in tags:
        return True
    refs = event.get("evidence_refs") if isinstance(event.get("evidence_refs"), list) else []
    return any(isinstance(ref, dict) and ref.get("type") == "orchestrator_interrupted" for ref in refs)


def _latest_orchestrator_interruption_blocker(run_id: str | None = None) -> dict:
    run_text = str(run_id or "").strip()
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        run_id=run_text or None,
        limit=800,
    )
    for event in records:
        if _orchestrator_event_is_interruption_blocker(event):
            return event
    for step in _read_recent_research_steps(limit=80, run_id=run_text or None):
        if not isinstance(step, dict) or not _orchestrator_event_is_interruption_blocker(step):
            continue
        if run_text and str(step.get("run_id") or "").strip() != run_text:
            continue
        return step
    return {}


def _orchestrator_event_is_recoverable_llm_blocker(event: dict) -> bool:
    """Whether an explicitly requested resume may replay a durable checkpoint.

    This is intentionally narrower than a generic failed run: only JSON/schema
    response failures that the client already treats as retryable qualify.  A
    tool, data, gate, or import failure still requires its normal repair path.
    """
    if not isinstance(event, dict) or not _orchestrator_event_is_terminal(event):
        return False
    evidence = event.get("evidence_refs") if isinstance(event.get("evidence_refs"), list) else []
    error_text = " ".join(
        [str(event.get("decision") or ""), str(event.get("summary") or "")]
        + [str(ref.get("error") or "") for ref in evidence if isinstance(ref, dict)]
    ).lower()
    retryable_markers = (
        "llm_response_not_valid_json",
        "llm_response_not_json",
        "empty_llm_response",
        "missing_required_fields",
        "next_stage_not_allowed",
        "candidate_lanes_required",
        "candidate_lane_missing",
        "candidate_expression_missing",
    )
    return any(marker in error_text for marker in retryable_markers)


def _latest_orchestrator_recoverable_llm_blocker(run_id: str) -> dict:
    """Return the latest retriable LLM blocker together with its prior checkpoint.

    A human may stop a mistaken recovery attempt before it reaches a gate.  In
    that case the newest event is the stop marker, while the correct durable
    checkpoint remains immediately before the earlier LLM blocker.  Scan the
    run journal backwards and bind the blocker to that *preceding* checkpoint;
    never let a later partial replay replace the recovery evidence.
    """
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        run_id=str(run_id or "").strip() or None,
        limit=1200,
    )
    for index, event in enumerate(records):
        if not _orchestrator_event_is_recoverable_llm_blocker(event):
            continue
        for older_event in records[index + 1 :]:
            for ref in older_event.get("evidence_refs") or []:
                if isinstance(ref, dict) and ref.get("type") == "orchestrator_recovery_checkpoint":
                    recovered = dict(event)
                    recovered["_recovery_checkpoint"] = dict(ref)
                    return recovered
        return {}
    return {}


def _latest_orchestrator_research_step_interruption_candidate() -> dict:
    for step in _read_recent_research_steps(limit=80):
        if not isinstance(step, dict):
            continue
        tags = {str(tag) for tag in step.get("tags") or []}
        if "orchestrator" not in tags or not step.get("run_id"):
            continue
        if _orchestrator_event_is_terminal(step):
            continue
        if str(step.get("stage") or "").strip() in {"checkpoint_stop", "stop", "blocker"}:
            continue
        item = dict(step)
        item.setdefault("event_type", ((step.get("monitoring") or {}).get("event_type") if isinstance(step.get("monitoring"), dict) else "research_step"))
        item.setdefault("heartbeat_status", ((step.get("monitoring") or {}).get("heartbeat_status") if isinstance(step.get("monitoring"), dict) else "alive"))
        return item
    return {}


def _latest_orchestrator_interruption_candidate() -> dict:
    latest_event = _latest_orchestrator_event_from_file()
    if (
        latest_event
        and str(latest_event.get("stage") or "") == "orchestrator_worker"
        and str(latest_event.get("decision") or "") == "worker_exited"
    ):
        # The lifecycle event is written after both successful completion and
        # crashes. If this run already has a substantive terminal event, keep
        # that terminal truth instead of reclassifying a normal API restart as
        # an interrupted research handoff.
        terminal_event = _latest_terminal_orchestrator_event_for_run(
            str(latest_event.get("run_id") or "")
        )
        if terminal_event:
            return terminal_event
    latest_trace = _latest_orchestrator_llm_trace_from_file()
    latest_step = _latest_orchestrator_research_step_interruption_candidate()
    step_ts = _parse_iso_ts(str(latest_step.get("ts") or "")) if latest_step else None
    event_ts = _parse_iso_ts(str(latest_event.get("ts") or "")) if latest_event else None
    if not latest_trace:
        if latest_step and (not event_ts or (step_ts and step_ts >= event_ts)):
            return latest_step
        return latest_event
    trace_ts = _parse_iso_ts(str(latest_trace.get("ts") or ""))
    if event_ts and trace_ts and event_ts >= trace_ts:
        if latest_step and step_ts and step_ts > event_ts:
            return latest_step
        return latest_event
    if str(latest_trace.get("event_type") or "") != "llm_request":
        return latest_event
    run_id = str(latest_trace.get("run_id") or "")
    round_id = str(latest_trace.get("round_id") or "")
    stage = str(latest_trace.get("stage") or latest_trace.get("checkpoint") or "llm_request")
    latest_step = _latest_research_step_for_run(run_id, round_id=round_id)
    latest_step_transition = latest_step.get("stage_transition") if isinstance(latest_step.get("stage_transition"), dict) else {}
    previous_stage = latest_step.get("previous_stage") or (latest_event.get("stage") if latest_event else "")
    previous_stage_id = latest_step.get("previous_stage_id") or (latest_event.get("stage_id") if latest_event else "")
    return {
        "schema_version": "orchestrator_event_v1",
        "run_id": run_id,
        "round_id": round_id,
        "stage_seq": latest_step.get("stage_seq") or 0,
        "stage_id": latest_step.get("stage_id") or f"{round_id}:req_{stage}_{str(latest_trace.get('trace_id') or '')[-8:]}",
        "previous_stage": previous_stage,
        "previous_stage_id": previous_stage_id,
        "stage": stage,
        "summary": latest_step.get("summary") or f"LLM request for {stage} has no matching result/error trace.",
        "decision": latest_step.get("decision") or "llm_request_in_progress",
        "stage_transition": latest_step_transition
        or {
            "next_stage": stage,
            "next_action": "llm_review_in_progress",
            "llm_trace_id": latest_trace.get("trace_id"),
        },
        "event_type": "llm_request",
        "llm_trace_id": latest_trace.get("trace_id"),
        "evidence_refs": [
            {
                "type": "orphaned_llm_request",
                "trace_id": latest_trace.get("trace_id"),
                "trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
                "request_ts": latest_trace.get("ts"),
                "last_event_stage_id": latest_event.get("stage_id") if latest_event else "",
            }
        ],
        "tags": ["orchestrator", "deepseek_v4", "llm_request_progress", stage],
        "ts": latest_trace.get("ts"),
        "heartbeat_status": "alive",
    }


def _mark_stale_orchestrator_run_interrupted(*, stale_seconds: int = 180, run_id: str | None = None) -> dict:
    latest = _latest_orchestrator_event_for_run(str(run_id or "")) if run_id else _latest_orchestrator_interruption_candidate()
    if not latest:
        return {}
    if _orchestrator_event_is_interruption_blocker(latest):
        return latest
    if _orchestrator_event_is_terminal(latest):
        return {}
    latest_ts = _parse_iso_ts(str(latest.get("ts") or ""))
    boot_interrupted = latest_ts is not None and latest_ts < FACTOR_API_BOOT_TS
    stale_interrupted = latest_ts is not None and (datetime.now() - latest_ts).total_seconds() >= stale_seconds
    if not (boot_interrupted or stale_interrupted):
        return {}
    run_id = str(latest.get("run_id") or "")
    active_job = _GUI_RUNS.get(run_id)
    if isinstance(active_job, dict) and _job_is_active(active_job) and _orchestrator_thread_alive(run_id):
        return {}
    existing = _latest_orchestrator_interruption_blocker(run_id)
    if existing:
        return existing
    round_id = str(latest.get("round_id") or f"{run_id}:interrupted")
    age_s = int((datetime.now() - latest_ts).total_seconds()) if latest_ts else None
    stage_transition = latest.get("stage_transition") if isinstance(latest.get("stage_transition"), dict) else {}
    evidence_refs = latest.get("evidence_refs") if isinstance(latest.get("evidence_refs"), list) else []
    raw_event = _latest_orchestrator_event_from_file()
    raw_event_matches = (
        isinstance(raw_event, dict)
        and str(raw_event.get("run_id") or "") == run_id
        and str(raw_event.get("stage_id") or "") == str(latest.get("stage_id") or "")
    )
    if raw_event_matches and isinstance(raw_event.get("evidence_refs"), list):
        raw_refs = [ref for ref in raw_event.get("evidence_refs") or [] if isinstance(ref, dict)]
        if raw_refs:
            projected_refs = [
                ref
                for ref in evidence_refs
                if isinstance(ref, dict)
                and ref.get("type") in {"orchestrator_event", "llm_trace", "orphaned_llm_request", "candidate_lanes"}
            ]
            evidence_refs = [*raw_refs, *projected_refs]
    raw_lane_source = raw_event.get("candidate_lanes") if raw_event_matches else None
    lane_items = _orchestrator_candidate_lane_items(raw_lane_source, limit=4)
    if not lane_items:
        lane_items = _orchestrator_candidate_lane_items(latest.get("candidate_lanes"), limit=4)
    if not lane_items:
        for ref in evidence_refs:
            if not isinstance(ref, dict) or ref.get("type") != "candidate_lanes":
                continue
            lane_items = _orchestrator_candidate_lane_items(ref.get("items"), limit=4)
            if lane_items:
                break
    candidate_lanes = [
        _compact_candidate_lane_for_step(item)
        for item in lane_items
        if isinstance(item, dict)
    ]
    interrupt_reason = "interrupted_by_api_restart" if boot_interrupted else "interrupted_by_stale_heartbeat"
    llm_trace_id = latest.get("llm_trace_id") or stage_transition.get("llm_trace_id")
    return _orchestrator_stage_event(
        run_id=run_id,
        round_id=f"{run_id}:interrupted",
        stage_seq=99,
        stage="blocker",
        previous_stage=str(latest.get("stage") or ""),
        previous_stage_id=str(latest.get("stage_id") or ""),
        summary="Orchestrator 已中断，需重启：API 重启、LLM request 超时或 worker heartbeat stale 后，旧后台任务已不可继续等待。",
        decision="orchestrator_interrupted_requires_restart：已终止为 terminal blocker；不要继续等待旧 LLM/worker，下一次启动必须继承该 handoff。",
        next_stage="blocker",
        next_action="restart_orchestrator_with_interrupted_handoff",
        event_type="blocker",
        evidence_refs=[
            {
                "type": "orchestrator_interrupted",
                "interrupted_run_id": run_id,
                "interrupted_round_id": round_id,
                "last_stage": latest.get("stage"),
                "last_stage_id": latest.get("stage_id"),
                "last_ts": latest.get("ts"),
                "api_boot_ts": FACTOR_API_BOOT_TS.isoformat(timespec="seconds"),
                "interrupt_reason": interrupt_reason,
                "legacy_interrupt_reason": "api_boot_mismatch" if boot_interrupted else "stale_heartbeat",
                "age_s": age_s,
                "last_stage_transition": stage_transition,
                "last_llm_trace_id": llm_trace_id,
                "last_candidate_lanes": candidate_lanes,
                "last_evidence_refs": evidence_refs[:6],
            }
        ],
        tags=["blocker", "orchestrator_interrupted", "terminal_blocker"],
        priority="high",
        heartbeat_status="interrupted",
    )


def _orchestrator_interrupted_handoff(interrupted_event: dict | None) -> dict:
    if not isinstance(interrupted_event, dict) or not interrupted_event:
        return {}
    refs = interrupted_event.get("evidence_refs") if isinstance(interrupted_event.get("evidence_refs"), list) else []
    interrupted_ref = next((ref for ref in refs if isinstance(ref, dict) and ref.get("type") == "orchestrator_interrupted"), {})
    is_process_interruption = bool(interrupted_ref)
    last_stage = str(interrupted_ref.get("last_stage") or interrupted_event.get("previous_stage") or "").strip()
    last_stage_id = str(interrupted_ref.get("last_stage_id") or interrupted_event.get("previous_stage_id") or "").strip()
    last_transition = interrupted_ref.get("last_stage_transition") if isinstance(interrupted_ref.get("last_stage_transition"), dict) else {}
    last_candidates = interrupted_ref.get("last_candidate_lanes") if isinstance(interrupted_ref.get("last_candidate_lanes"), list) else []
    last_evidence = interrupted_ref.get("last_evidence_refs") if isinstance(interrupted_ref.get("last_evidence_refs"), list) else []
    last_llm_trace_id = str(interrupted_ref.get("last_llm_trace_id") or "").strip()
    interrupted_run_id = str(interrupted_ref.get("interrupted_run_id") or interrupted_event.get("run_id") or "")
    recovery_checkpoint = (
        dict(interrupted_event.get("_recovery_checkpoint") or {})
        if isinstance(interrupted_event.get("_recovery_checkpoint"), dict)
        else _latest_orchestrator_recovery_checkpoint(interrupted_run_id)
    )
    transition_target = str(last_transition.get("next_stage") or "").strip()
    if recovery_checkpoint:
        resume_target = "expression_design"
    elif transition_target in _ORCHESTRATOR_RESUME_STAGES:
        resume_target = transition_target
    else:
        resume_target = "thesis_design"
    return {
        "from_stage": "orchestrator_interrupted" if is_process_interruption else "orchestrator_recovery",
        "to_stage": resume_target,
        "reason": (
            (
                "上一轮 Orchestrator 非终止运行已在 API 重启或 stale 检查中标记 interrupted；"
                if is_process_interruption
                else "上一轮 Orchestrator 在可重试的 LLM JSON/schema 响应错误后安全停止；"
            )
            + f"恢复时必须先读取 last_stage={last_stage or 'unknown'} / last_stage_id={last_stage_id or 'unknown'}，"
            "再决定是否复用候选、回到表达式设计或重新开题。"
        ),
        "recommended_mutation": (
            "replay_existing_candidates_without_llm_redesign"
            if recovery_checkpoint
            else "restart_orchestrator_with_interrupted_handoff"
        ),
        "must_preserve": [value for value in (last_stage, last_stage_id, last_llm_trace_id) if value],
        "must_avoid": ["do_not_assume_interrupted_run_is_still_running", "do_not_drop_last_visible_stage"],
        "last_stage_transition": last_transition,
        "last_candidate_lanes": last_candidates[:4],
        "last_evidence_refs": last_evidence[:6],
        "interrupt_reason": interrupted_ref.get("interrupt_reason") if is_process_interruption else "retryable_llm_contract_error",
        "age_s": interrupted_ref.get("age_s"),
        "supporting_evidence_refs": refs[:4],
        "recovery_checkpoint": recovery_checkpoint,
    }


def _recent_import_diagnostics(jobs: list[dict], *, limit: int = 5) -> list[dict]:
    diagnostics: list[dict] = []
    for job in jobs:
        for event in reversed(job.get("events", []) or []):
            if event.get("tool") != "fxalpha_import_factors" or event.get("event") != "tool_call_completed":
                continue
            preview = event.get("result_preview") or ""
            payload = {}
            if isinstance(preview, str) and preview.strip():
                try:
                    raw = json.loads(preview)
                    payload = raw.get("outputs", raw) if isinstance(raw, dict) else {}
                except Exception:
                    payload = {"raw_preview": preview[:1000]}
            diagnostics.append(
                {
                    "run_id": job.get("run_id"),
                    "ts": event.get("ts"),
                    "imported": payload.get("imported"),
                    "skipped": payload.get("skipped"),
                    "errors": payload.get("errors", []),
                    "details": payload.get("details", []),
                }
            )
            break
        if len(diagnostics) >= limit:
            break
    return diagnostics


def _latest_four_step_blocks(active_job: dict | None, recent_jobs: list[dict]) -> dict:
    names = {
        "analysis_fact_pack_built": "fact_pack",
        "four_step_fact_collection": "fact_collection",
        "four_step_independent_judgment": "independent_judgment",
        "four_step_cross_review": "cross_review",
        "four_step_consensus": "consensus",
    }
    blocks: dict[str, Any] = {}
    # If a live job exists, do not blend in historical four-step conclusions.
    # Old consensus blocks are useful for audit, but showing them in the live
    # cockpit makes the current run look stopped or direction-constrained.
    jobs = [active_job] if active_job else [job for job in (recent_jobs or []) if job]
    for job in jobs:
        for event in reversed(job.get("events", []) or []):
            event_name = event.get("event")
            block_key = names.get(event_name)
            if not block_key or block_key in blocks:
                continue
            if event_name == "analysis_fact_pack_built":
                blocks[block_key] = event.get("fact_pack") or {}
            else:
                blocks[block_key] = event.get(block_key) or {}
            blocks.setdefault("run_id", job.get("run_id"))
            blocks.setdefault("updated_at", event.get("ts"))
        if len([key for key in names.values() if key in blocks]) >= len(names):
            break
    return blocks


def _append_job_event(job: dict, event: dict) -> None:
    events = job.setdefault("events", deque(maxlen=_GUI_EVENT_LIMIT))
    enriched = {"ts": _now_iso(), **event}
    events.append(enriched)
    job["latest_event"] = enriched
    stage = event.get("event")
    if stage:
        job["stage"] = stage
    if event.get("event") == "session_completed":
        job["summary"] = {
            "direction": event.get("direction"),
            "stop_reason": event.get("stop_reason"),
            "adopted_count": event.get("adopted_count", 0),
            "screened_out_count": event.get("screened_out_count", 0),
            "rounds": event.get("rounds", 0),
        }
    _persist_job(job)


def _parse_iso_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _sweep_gui_jobs(*, stale_session_completed_seconds: int = 90) -> None:
    now = datetime.now()
    for job in _GUI_RUNS.values():
        if job.get("status") not in {"queued", "running"}:
            continue
        if job.get("stage") != "session_completed":
            continue
        latest_event = job.get("latest_event") or {}
        latest_ts = _parse_iso_ts(latest_event.get("ts")) or _parse_iso_ts(job.get("started_at"))
        if latest_ts is None:
            continue
        if (now - latest_ts).total_seconds() < stale_session_completed_seconds:
            continue
        job["status"] = "completed"
        job["finished_at"] = job.get("finished_at") or _now_iso()
        if latest_event.get("event") != "job_finished":
            _append_job_event(
                job,
                {
                    "event": "job_finished",
                    "ok": True,
                    "err": "",
                    "reconciled": True,
                },
            )


def _job_latest_ts(job: dict) -> datetime | None:
    latest_event = job.get("latest_event") or {}
    return _parse_iso_ts(latest_event.get("ts")) or _parse_iso_ts(job.get("started_at"))


def _job_is_orphaned(job: dict, registry_summary: dict, latest_run: dict) -> bool:
    if job.get("status") not in {"queued", "running"}:
        return False
    latest_ts = _job_latest_ts(job)
    if latest_ts is None:
        return False
    registry_total = int(registry_summary.get("total", 0) or 0)
    registry_active = int(registry_summary.get("active", 0) or 0)
    latest_pipeline = ((latest_run or {}).get("pipeline", {}) or {}).get("overall_status", "")
    latest_result = (latest_run or {}).get("result", {}) or {}
    has_latest_payload = bool(latest_result or latest_pipeline)
    age_seconds = (datetime.now() - latest_ts).total_seconds()
    if registry_total == 0 and registry_active == 0 and not has_latest_payload and age_seconds >= _GUI_ORPHANED_SECONDS:
        return True
    if job.get("latest_result") is None and age_seconds >= (_GUI_ORPHANED_SECONDS * 2):
        return True
    return False


def _purge_orphaned_gui_jobs(registry_summary: dict, latest_run: dict) -> list[str]:
    orphaned: list[str] = []
    for run_id, job in list(_GUI_RUNS.items()):
        if _job_is_orphaned(job, registry_summary, latest_run):
            orphaned.append(run_id)
            _GUI_RUNS.pop(run_id, None)
    return orphaned


def _read_recent_research_notes(limit: int = 8) -> list[dict]:
    archive_dir = Path(QUANTGPT_RESEARCH_NOTES_DIR) / "archive"
    if not archive_dir.exists():
        return []
    notes = []
    for path in sorted(archive_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        notes.append(
            {
                "name": path.name,
                "path": str(path),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "preview": text[:1200],
            }
        )
    return notes


def _round_no_from_id(round_id: Any) -> int:
    match = re.search(r"\br(\d{4,})\b", str(round_id or ""))
    if not match:
        return 0
    try:
        return int(match.group(1))
    except Exception:
        return 0


def _normalize_thesis_payload(payload: Any, *, source: str, ts: str | None = None, fallback_horizon: Any = None) -> dict:
    if not payload:
        return {}
    if isinstance(payload, str):
        return {
            "name": _clip_text(payload, 96),
            "market_mechanism": _clip_text(payload, 420),
            "source": source,
            "updated_at": ts,
            "target_horizon": fallback_horizon,
        }
    if not isinstance(payload, dict):
        return {}
    thesis = dict(payload)
    thesis.setdefault("name", thesis.get("title") or thesis.get("thesis") or thesis.get("economic_thesis") or "未命名经济假设")
    thesis.setdefault("source", source)
    thesis.setdefault("updated_at", ts)
    if fallback_horizon and not thesis.get("target_horizon"):
        thesis["target_horizon"] = fallback_horizon
    if thesis.get("holding_period_days") and not thesis.get("target_horizon"):
        thesis["target_horizon"] = f"{thesis.get('holding_period_days')}D"
    return thesis


def _extract_thesis_cards(
    *,
    research_steps: list[dict],
    candidates: list[dict],
    limit: int = 24,
) -> list[dict]:
    cards: list[dict] = []
    seen: set[str] = set()

    def add(payload: Any, *, source: str, ts: str | None = None, fallback_horizon: Any = None, status: str | None = None) -> None:
        card = _normalize_thesis_payload(payload, source=source, ts=ts, fallback_horizon=fallback_horizon)
        if not card:
            return
        if status and not card.get("status"):
            card["status"] = status
        key = str(card.get("name") or card.get("market_mechanism") or card)[:180].lower()
        if key in seen:
            return
        seen.add(key)
        cards.append(card)

    for step in research_steps or []:
        extra = step.get("extra") if isinstance(step.get("extra"), dict) else {}
        status = extra.get("thesis_status") or step.get("thesis_status")
        horizon = extra.get("target_horizon") or step.get("target_horizon")
        theses = extra.get("economic_theses") or step.get("economic_theses") or []
        if isinstance(theses, dict):
            theses = [theses]
        for thesis in theses:
            add(thesis, source="research_step", ts=step.get("ts") or step.get("created_at"), fallback_horizon=horizon, status=status)
        if step.get("economic_thesis"):
            add(step.get("economic_thesis"), source="research_step", ts=step.get("ts") or step.get("created_at"), fallback_horizon=horizon, status=status)

    for candidate in candidates or []:
        add(
            candidate.get("economic_thesis"),
            source="candidate",
            ts=candidate.get("tool_ts") or candidate.get("ts"),
            fallback_horizon=candidate.get("target_horizon") or candidate.get("holding_period_days"),
            status=candidate.get("thesis_status") or candidate.get("quality_decision"),
        )

    return cards[:limit]


def _extract_recent_library(limit: int = 80) -> dict:
    listing = factor_registry_list(limit=limit, offset=0).to_dict()
    return {
        "total": listing.get("outputs", {}).get("total", 0),
        "items": listing.get("outputs", {}).get("items", []),
    }


def _compact_candidate_for_console(candidate: dict) -> dict:
    if not isinstance(candidate, dict):
        return {}
    keep_keys = {
        "candidate_id",
        "round_id",
        "stage_id",
        "name",
        "expression",
        "economic_thesis",
        "candidate_prompt",
        "hypothesis",
        "status",
        "task_id",
        "task_type",
        "source_tool",
        "screening_stage",
        "source_step_ts",
        "tool_ts",
        "duration_seconds",
        "score",
        "quick_score",
        "deep_score",
        "holding_period_days",
        "grade",
        "official_grade",
        "single_factor_decision",
        "quality_decision",
        "reject_reasons",
        "veto_reasons",
        "key_metrics",
        "component_scores",
        "penalties",
        "backtest_summary",
        "best_long_only_group_metrics",
        "long_short_diagnostic_metrics",
        "report_metrics",
        "strategy_used",
        "autocorrelation",
        "persistence_diagnostic",
        "anti_overfit_summary",
        "anti_overfit",
        "adversarial_validation",
        "neutralization_applied",
        "deep_validation",
        "gate_result",
        "novelty_guard",
        "novelty_correlation",
        "screening",
        "report_url",
        "report_path",
    }
    compact = {key: candidate.get(key) for key in keep_keys if key in candidate}
    rolling = candidate.get("rolling_validation")
    if isinstance(rolling, dict):
        trailing = rolling.get("trailing_horizons") or {}
        compact["rolling_validation"] = {
            "schema_version": rolling.get("schema_version"),
            "score_policy_version": rolling.get("score_policy_version"),
            "status": rolling.get("status"),
            "score": rolling.get("score"),
            "grade": rolling.get("grade"),
            "weighted_ic": rolling.get("weighted_ic"),
            "weighted_std": rolling.get("weighted_std"),
            "robust_ic": rolling.get("robust_ic"),
            "rolling_6m_ic": (trailing.get("6m") or {}).get("rank_ic"),
            "rolling_12m_ic": (trailing.get("12m") or {}).get("rank_ic"),
            "rolling_24m_ic": (trailing.get("24m") or {}).get("rank_ic"),
            "rolling_48m_ic": (trailing.get("48m") or {}).get("rank_ic"),
            "data_as_of_date": rolling.get("data_as_of_date"),
            "last_evaluable_signal_date": rolling.get("last_evaluable_signal_date"),
            "period_count": len(rolling.get("incremental_periods", []) or []),
        }
    return compact


def _truncate_text(value: object, *, limit: int = 360) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _compact_note_for_live(item: dict, *, preview_limit: int = 360) -> dict:
    if not isinstance(item, dict):
        return {}
    compact = {
        "name": item.get("name"),
        "type": item.get("type"),
        "title": item.get("title"),
        "path": item.get("path"),
        "updated_at": item.get("updated_at"),
        "source": item.get("source"),
        "run_id": item.get("run_id"),
        "round_start": item.get("round_start"),
        "round_end": item.get("round_end"),
        "round_count": item.get("round_count"),
        "target_horizon": item.get("target_horizon"),
        "thesis_name": item.get("thesis_name"),
        "candidate_plan_advice": item.get("candidate_plan_advice"),
        "evidence_refs": (item.get("evidence_refs") or [])[:8],
        "advisory_only": item.get("advisory_only"),
        "not_gate": item.get("not_gate"),
        "tags": item.get("tags") or [],
    }
    preview = item.get("preview")
    if preview:
        compact["preview"] = _truncate_text(preview, limit=preview_limit)
    return {key: value for key, value in compact.items() if value not in (None, "", [])}


def _compact_evidence_ref_for_live(ref: object, *, detail_limit: int = 2400) -> object:
    """Keep GUI evidence useful without embedding resumable workflow state.

    Recovery checkpoints intentionally contain the full thesis, hypothesis and
    candidate plan so the worker can resume.  They belong in the durable event
    journal, but copying them into every polling response makes the cockpit
    payload grow by megabytes.  The live projection keeps identity, metrics and
    counts; the source journal remains untouched.
    """

    if not isinstance(ref, dict):
        return _truncate_text(ref, limit=360) if isinstance(ref, str) else ref
    raw = json.dumps(_jsonable(ref), ensure_ascii=False, default=str)
    if len(raw) <= detail_limit:
        return _jsonable(ref)

    compact: dict[str, object] = {}
    scalar_keys = (
        "type",
        "source",
        "tool",
        "stage",
        "action",
        "status",
        "policy",
        "gate",
        "run_id",
        "round_id",
        "stage_id",
        "trace_id",
        "task_id",
        "candidate_id",
        "candidate_index",
        "factor_id",
        "factor_name",
        "expression",
        "resume_stage",
        "quick_score",
        "score",
        "grade",
        "ic",
        "icir",
        "rank_icir",
        "deep_score",
        "deep_action",
        "deep_reason",
        "anti_overfit_score",
        "adversarial_score",
        "novelty_score",
        "fatal_count",
        "warning_count",
        "skipped_count",
        "code_ready_count",
        "llm_selected_count",
        "code_fallback_count",
        "recommended_action",
        "next_thesis_policy",
        "note",
    )
    for key in scalar_keys:
        value = ref.get(key)
        if value not in (None, "", [], {}):
            compact[key] = _truncate_text(value, limit=520) if isinstance(value, str) else value

    for key in (
        "fatal_candidate_ids",
        "skipped_candidate_ids",
        "code_fallback_candidate_ids",
        "llm_rejected_code_ready_ids",
        "allowed_actions",
        "blocked_actions",
        "keepers",
        "dropped",
        "completed_task_refs",
    ):
        value = ref.get(key)
        if isinstance(value, list):
            compact[key] = _jsonable(value[:12])

    for key in ("lane_counts", "trajectory_metrics"):
        value = ref.get(key)
        if isinstance(value, dict):
            compact[key] = _jsonable(value)

    # Candidate lists remain useful to the GUI, but only their display and
    # scoring fields are needed in a live response.
    for key in ("items", "candidate_lanes", "candidate_lane_decisions"):
        value = ref.get(key)
        if not isinstance(value, list):
            continue
        rows = []
        for item in value[:12]:
            if not isinstance(item, dict):
                rows.append(item)
                continue
            rows.append(
                {
                    field: item.get(field)
                    for field in (
                        "candidate_id",
                        "candidate_index",
                        "factor_name",
                        "name",
                        "expression",
                        "action",
                        "decision",
                        "reason",
                        "quick_score",
                        "score",
                        "grade",
                        "deep_score",
                        "novelty_score",
                    )
                    if item.get(field) not in (None, "", [], {})
                }
            )
        compact[key] = rows

    for source_key, count_key in (
        ("candidates", "candidate_count"),
        ("planned_candidates", "planned_candidate_count"),
        ("stage_candidates", "stage_candidate_count"),
    ):
        value = ref.get(source_key)
        if isinstance(value, list):
            compact[count_key] = len(value)
    compact["detail_compacted"] = True
    return _jsonable(compact)


def _compact_research_step_for_live(step: dict, *, extra_limit: int = 5000) -> dict:
    if not isinstance(step, dict):
        return {}
    compact = {
        "schema_version": step.get("schema_version"),
        "ts": step.get("ts") or step.get("created_at"),
        "run_id": step.get("run_id"),
        "round_id": step.get("round_id"),
        "stage_seq": step.get("stage_seq"),
        "stage_id": step.get("stage_id"),
        "previous_stage": step.get("previous_stage"),
        "previous_stage_id": step.get("previous_stage_id"),
        "stage": step.get("stage"),
        "summary": _truncate_text(step.get("summary"), limit=520),
        "decision": _truncate_text(step.get("decision"), limit=360),
        "next": _truncate_text(step.get("next"), limit=360),
        "priority": step.get("priority"),
        "refs": (step.get("refs") or [])[:8],
        "evidence_refs": [
            _compact_evidence_ref_for_live(ref)
            for ref in (step.get("evidence_refs") or [])[:8]
        ],
        "tags": (step.get("tags") or [])[:12],
        "round": step.get("round"),
        "economic_thesis": step.get("economic_thesis"),
        "economic_theses": (step.get("economic_theses") or [])[:4],
        "target_horizon": step.get("target_horizon"),
        "stage_transition": step.get("stage_transition"),
        "transition_missing": step.get("transition_missing"),
        "extra_removed_keys": step.get("extra_removed_keys"),
    }
    extra = step.get("extra")
    if isinstance(extra, dict):
        compact_extra: dict[str, object] = {}
        for key in (
            "candidate",
            "automation_id",
            "gui_job_run_id",
            "research_state",
            "keeper",
            "batch_result",
            "selected_for_novelty",
            "selected_candidates",
            "candidates",
            "top_candidates",
            "deep_candidates",
            "imported",
            "imported_factor_ids",
            "quality_gate",
            "novelty_guard",
            "heartbeat_total",
            "direction",
        ):
            if key in extra:
                compact_extra[key] = extra.get(key)
        raw = json.dumps(_jsonable(compact_extra or extra), ensure_ascii=False, default=str)
        if len(raw) <= extra_limit:
            try:
                compact["extra"] = json.loads(raw)
            except Exception:
                compact["extra"] = raw
        else:
            compact["extra_preview"] = _truncate_text(raw, limit=extra_limit)
    return {key: value for key, value in compact.items() if value not in (None, "", [])}


def _compact_factor_library_for_live(factor_library: dict, *, limit: int = 24) -> dict:
    items = []
    for item in (factor_library or {}).get("items", [])[:limit]:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                key: item.get(key)
                for key in (
                    "factor_id",
                    "name",
                    "factor_name",
                    "category",
                    "expression",
                    "status",
                    "quick_score",
                    "deep_score",
                    "ic_ir",
                    "rank_ic_ir",
                    "created_at",
                    "updated_at",
                )
                if item.get(key) is not None
            }
        )
    return {"total": (factor_library or {}).get("total", 0), "items": items}


def _compact_task_for_live(task: dict) -> dict:
    if not isinstance(task, dict):
        return {}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    compact = {
        "task_id": task.get("task_id"),
        "task_type": task.get("task_type"),
        "status": task.get("status"),
        "expression": task.get("expression") or result.get("expression"),
        "session_id": task.get("session_id"),
        "created_at": task.get("created_at"),
        "completed_at": task.get("completed_at"),
        "duration_seconds": task.get("duration_seconds"),
        "error": _truncate_text(task.get("error"), limit=300) if task.get("error") else None,
    }
    if result:
        long_only = (
            result.get("best_long_only_group_metrics")
            if isinstance(result.get("best_long_only_group_metrics"), dict)
            else {}
        )
        compact["result"] = {
            key: result.get(key)
            for key in (
                "name",
                "expression",
                "score",
                "quick_score",
                "deep_score",
                "grade",
                "quality_decision",
                "single_factor_decision",
                "reject_reasons",
                "veto_reasons",
                "key_metrics",
                "anti_overfit_summary",
                "novelty_guard",
            )
            if result.get(key) is not None
        }
        if long_only:
            compact["result"]["best_long_only_group_metrics"] = {
                key: long_only.get(key)
                for key in ("annual_return", "sharpe", "max_drawdown", "turnover", "selected_group_is_flipped_low_side")
                if long_only.get(key) is not None
            }
    return {key: value for key, value in compact.items() if value is not None}


def _jsonable(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, deque):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return value


def _json_field(value):
    if isinstance(value, (dict, list)):
        return _jsonable(value)
    if isinstance(value, str) and value.strip():
        try:
            return _jsonable(json.loads(value))
        except Exception:
            return value
    return {} if value in (None, "") else value


def _tool_payload_from_event(event: dict | None) -> dict:
    if not isinstance(event, dict):
        return {}
    result = event.get("result")
    if isinstance(result, dict):
        nested = result.get("outputs", result)
        return nested if isinstance(nested, dict) else result
    preview = event.get("result_preview")
    if isinstance(preview, dict):
        payload = preview
    elif isinstance(preview, str) and preview.strip():
        try:
            payload = json.loads(preview)
        except Exception:
            return {"raw_preview": preview[:1200]}
    else:
        return {}
    if isinstance(payload, dict):
        nested = payload.get("outputs", payload)
        return nested if isinstance(nested, dict) else payload
    return {}


def _metric_value(candidate: dict, *names: str) -> Any:
    sources = [
        candidate.get("key_metrics") or {},
        candidate.get("backtest_summary") or {},
        candidate.get("gate_result") or {},
        candidate.get("report_metrics") or {},
    ]
    aliases = {
        "ic_ir": ("ic_ir", "ir"),
        "rank_ic_ir": ("rank_ic_ir", "rank_ir"),
        "rank_ic_mean": ("rank_ic_mean", "rank_ic"),
    }
    expanded: list[str] = []
    for name in names:
        expanded.extend(aliases.get(name, (name,)))
    for source in sources:
        for name in expanded:
            if name in source and source.get(name) is not None:
                return source.get(name)
    return None


def _candidate_digest(candidate: dict, event: dict | None = None, *, source: str = "") -> dict:
    compact = _compact_candidate_for_console(candidate)
    if not compact:
        return {}
    metrics = {
        "ic_mean": _metric_value(candidate, "ic_mean", "ic"),
        "ic_ir": _metric_value(candidate, "ic_ir"),
        "rank_ic_mean": _metric_value(candidate, "rank_ic_mean"),
        "rank_ic_ir": _metric_value(candidate, "rank_ic_ir"),
        "sharpe": _metric_value(candidate, "sharpe"),
        "annual_return": _metric_value(candidate, "annual_return", "annualized_return", "return"),
    }
    compact["metrics"] = {key: value for key, value in metrics.items() if value is not None}
    if event:
        compact["ts"] = event.get("ts")
        compact["session_id"] = event.get("session_id")
        compact["step"] = event.get("step")
    if source:
        compact["source"] = source
    return compact


_NON_FACTOR_CANDIDATE_NAMES = {
    "validated",
    "candidate",
    "candidates",
    "latest",
    "summary",
    "score",
    "quick",
    "deep",
    "backtest",
    "import",
    "gate",
    "novelty",
}


def _looks_like_non_factor_reference(value: object) -> bool:
    text = str(value or "").strip()
    lower = text.lower()
    if not text:
        return True
    if lower in _NON_FACTOR_CANDIDATE_NAMES:
        return True
    if re.fullmatch(r"20\d{6}(?:[_-]\d{4,6})?", text):
        return True
    if re.fullmatch(r"(?:round|batch)\d+(?:[_-].*)?", text, flags=re.I):
        return True
    if lower.startswith(("backtest_report_", "run_backtest_", "fxalpha_quality_gate_", "novelty_check_")):
        return True
    return False


def _payload_has_quick_score(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("quick_score") is not None or payload.get("score") is not None:
        return True
    for key in ("metrics", "key_metrics", "backtest_summary", "report_metrics"):
        value = payload.get(key)
        if isinstance(value, dict) and (value.get("quick_score") is not None or value.get("score") is not None):
            return True
    return False


def _score_review_step_is_running(step: dict, payload: dict) -> bool:
    blob = f"{step.get('summary') or ''} {step.get('decision') or ''} {step.get('next') or ''} {step.get('stage_transition') or ''}".lower()
    if _payload_has_quick_score(payload):
        return False
    return any(
        token in blob
        for token in (
            "进行中",
            "正在执行",
            "准备对",
            "等待 score_factor",
            "validate_and_score_in_progress",
            "in_progress",
            "running",
        )
    )


def _candidate_status_from_step(step: dict, payload: dict) -> tuple[str, str]:
    stage = str(step.get("stage") or payload.get("screening_stage") or payload.get("stage") or "research_step")
    payload_stage = str(payload.get("screening_stage") or payload.get("candidate_lane") or payload.get("precheck_status") or "").strip()
    if payload_stage in {
        "precheck_blocked",
        "precheck_warning",
        "planned_for_score",
        "semantic_revision",
        "candidate_plan_dropped",
    }:
        status_map = {
            "precheck_blocked": "blocked",
            "precheck_warning": "warning",
            "planned_for_score": "planned_for_score",
            "semantic_revision": "blocked",
            "candidate_plan_dropped": "dropped",
        }
        return payload_stage, status_map[payload_stage]
    blob = f"{step.get('summary') or ''} {step.get('decision') or ''} {step.get('next') or ''}".lower()
    if stage == "score_review":
        if _score_review_step_is_running(step, payload):
            return "quick_score_running", "running"
        return "quick_score", "scored"
    if stage == "deep_validation_review":
        if any(token in blob for token in ("failed", "reject", "do not submit", "不提交", "拒绝")):
            return "deep_validation", "rejected"
        return "deep_validation", "deep_validated"
    if stage == "import_gate_review":
        if any(token in blob for token in ("adopt", "import", "通过", "入库")):
            return "quality_gate", "adopted"
        if any(token in blob for token in ("reject", "screen", "blocked", "拒绝", "拦截")):
            return "quality_gate", "rejected"
        return "quality_gate", "reviewed"
    return stage, str(payload.get("status") or payload.get("quality_decision") or "recorded")


def _candidate_metric(payload: dict, *names: str) -> object:
    sources = [
        payload,
        payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {},
        payload.get("key_metrics") if isinstance(payload.get("key_metrics"), dict) else {},
        payload.get("backtest_summary") if isinstance(payload.get("backtest_summary"), dict) else {},
        payload.get("report_metrics") if isinstance(payload.get("report_metrics"), dict) else {},
        payload.get("gate_result") if isinstance(payload.get("gate_result"), dict) else {},
    ]
    for source in sources:
        for name in names:
            if isinstance(source, dict) and source.get(name) is not None:
                return source.get(name)
    return None


def _candidate_record_from_payload(payload: dict, step: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    candidate = dict(payload.get("candidate") if isinstance(payload.get("candidate"), dict) else payload)
    expression = str(candidate.get("expression") or "").strip()
    name = str(candidate.get("name") or candidate.get("factor_name") or candidate.get("candidate_id") or "").strip()
    explicit_candidate = bool(expression or candidate.get("score") is not None or candidate.get("quick_score") is not None or candidate.get("deep_score") is not None)
    if not explicit_candidate:
        return {}
    if not expression and _looks_like_non_factor_reference(name):
        return {}
    stage, status = _candidate_status_from_step(step, candidate)
    gate = candidate.get("gate_result") if isinstance(candidate.get("gate_result"), dict) else {}
    novelty = candidate.get("novelty_guard") if isinstance(candidate.get("novelty_guard"), dict) else candidate.get("novelty_correlation") if isinstance(candidate.get("novelty_correlation"), dict) else {}
    reject_reasons = []
    for field in ("reject_reasons", "veto_reasons"):
        values = candidate.get(field)
        if isinstance(values, list):
            reject_reasons.extend(str(item) for item in values if item)
        elif values:
            reject_reasons.append(str(values))
    precheck_warnings = candidate.get("precheck_warnings")
    if isinstance(precheck_warnings, list):
        reject_reasons.extend(str(item) for item in precheck_warnings if item and status == "blocked")
    if status in {"rejected", "blocked"} and step.get("decision"):
        reject_reasons.append(str(step.get("decision")))
    report_path = candidate.get("report_path")
    backtest = candidate.get("backtest_summary") if isinstance(candidate.get("backtest_summary"), dict) else {}
    if not report_path:
        report_path = backtest.get("report_path")
    record = {
        "candidate_id": candidate.get("candidate_id"),
        "run_id": candidate.get("run_id") or step.get("run_id"),
        "round_id": candidate.get("round_id") or step.get("round_id"),
        "stage_id": candidate.get("stage_id") or step.get("stage_id"),
        "name": name or expression[:80],
        "expression": expression,
        "stage": stage,
        "screening_stage": stage,
        "status": status,
        "candidate_lane": candidate.get("candidate_lane"),
        "precheck_status": candidate.get("precheck_status"),
        "precheck_instruction": candidate.get("precheck_instruction"),
        "precheck_warnings": candidate.get("precheck_warnings"),
        "decision_source": candidate.get("decision_source"),
        "candidate_plan_action": candidate.get("candidate_plan_action"),
        "matched_candidate_ids": candidate.get("matched_candidate_ids"),
        "matched_cluster_id": candidate.get("matched_cluster_id"),
        "matched_factor_ids": candidate.get("matched_factor_ids"),
        "grade": candidate.get("grade") or gate.get("official_grade"),
        "official_grade": candidate.get("official_grade") or gate.get("official_grade"),
        "quick_score": _candidate_metric(candidate, "quick_score", "score"),
        "deep_score": _candidate_metric(candidate, "deep_score"),
        "anti_overfit_score": _candidate_metric(candidate, "anti_overfit_score"),
        "adversarial_score": _candidate_metric(candidate, "adversarial_score"),
        "ic": _candidate_metric(candidate, "ic", "ic_mean"),
        "icir": _candidate_metric(candidate, "icir", "ic_ir"),
        "rank_ic": _candidate_metric(candidate, "rank_ic", "rank_ic_mean"),
        "rank_icir": _candidate_metric(candidate, "rank_icir", "rank_ic_ir"),
        "sharpe": _candidate_metric(candidate, "sharpe"),
        "annual_return": _candidate_metric(candidate, "annual_return", "annualized_return"),
        "metrics": {
            "ic_mean": _candidate_metric(candidate, "ic_mean", "ic"),
            "ic_ir": _candidate_metric(candidate, "ic_ir", "icir"),
            "rank_ic_mean": _candidate_metric(candidate, "rank_ic_mean", "rank_ic"),
            "rank_ic_ir": _candidate_metric(candidate, "rank_ic_ir", "rank_icir"),
            "sharpe": _candidate_metric(candidate, "sharpe"),
            "annual_return": _candidate_metric(candidate, "annual_return", "annualized_return"),
            "max_drawdown": _candidate_metric(candidate, "max_drawdown"),
        },
        "novelty": novelty,
        "novelty_guard": novelty,
        "gate_decision": gate.get("decision") or candidate.get("quality_decision"),
        "quality_decision": gate.get("decision") or candidate.get("quality_decision") or status,
        "single_factor_decision": candidate.get("single_factor_decision"),
        "status_label": candidate.get("status_label"),
        "status_reason": candidate.get("status_reason"),
        "novelty_reject_type": candidate.get("novelty_reject_type"),
        "novelty_reject_label": candidate.get("novelty_reject_label"),
        "reject_reason": "; ".join(dict.fromkeys(reject_reasons)),
        "reject_reasons": list(dict.fromkeys(reject_reasons)),
        "veto_reasons": candidate.get("veto_reasons"),
        "source_step_ts": step.get("ts") or step.get("created_at"),
        "source_stage": step.get("stage"),
        "source": "research_steps",
        "console_scope": "research_step",
        "report_path": report_path,
        "report_url": candidate.get("report_url"),
        "economic_thesis": candidate.get("economic_thesis"),
        "hypothesis": candidate.get("hypothesis"),
        "target_horizon": candidate.get("target_horizon") or candidate.get("holding_period_days"),
        "holding_period_days": candidate.get("holding_period_days"),
        "backtest_summary": backtest,
        "component_scores": candidate.get("component_scores"),
        "gate_result": gate,
        "deep_validation": candidate.get("deep_validation"),
        "anti_overfit": candidate.get("anti_overfit"),
        "anti_overfit_summary": candidate.get("anti_overfit_summary"),
        "adversarial_validation": candidate.get("adversarial_validation"),
        "rolling_validation": candidate.get("rolling_validation"),
    }
    return {key: _jsonable(value) for key, value in record.items() if value not in (None, "", [], {})}


def _candidate_payloads_from_research_step(step: dict) -> list[dict]:
    extra = _research_step_extra(step)
    payloads: list[dict] = []

    def add(item: object) -> None:
        if isinstance(item, dict):
            payloads.append(item)
        elif isinstance(item, list):
            for sub_item in item:
                add(sub_item)

    for key in (
        "candidate",
        "candidate_lanes",
        "candidate_decisions",
        "evidence_refs",
        "keeper",
        "selected_for_novelty",
        "selected_candidates",
        "candidates",
        "top_candidates",
        "deep_candidates",
        "adopted",
        "screened_out",
        "rejected",
    ):
        add(step.get(key))
        add(extra.get(key))
    batch = extra.get("batch_result")
    if isinstance(batch, dict):
        for key in ("candidates", "top_candidates", "selected", "validated", "rejected"):
            add(batch.get(key))
    gate = extra.get("quality_gate")
    if isinstance(gate, dict):
        for key in ("adopted", "screened_out", "rejected"):
            add(gate.get(key))
    monitoring = step.get("monitoring") if isinstance(step.get("monitoring"), dict) else {}
    watched: dict[str, dict] = {}
    for key in ("candidate_watch", "evidence_watch"):
        for idx, item in enumerate(monitoring.get(key) or []):
            if not isinstance(item, dict):
                continue
            watch_key = str(item.get("candidate_id") or item.get("expression") or item.get("factor_name") or f"{key}:{idx}")
            watched[watch_key] = {**watched.get(watch_key, {}), **item}
    for item in watched.values():
        add(item)
    return payloads


def _candidate_records_from_research_steps(research_steps: list[dict], *, limit: int = 40) -> list[dict]:
    by_key: dict[str, dict] = {}
    latest_ts: dict[str, str] = {}
    for step in reversed(research_steps or []):
        for payload in _candidate_payloads_from_research_step(step):
            record = _candidate_record_from_payload(payload, step)
            if not record:
                continue
            round_id = str(record.get("round_id") or "").strip()
            candidate_id = str(record.get("candidate_id") or "").strip()
            if round_id and candidate_id:
                key = f"process:{round_id}:{candidate_id}".lower()
            else:
                key = str(record.get("expression") or record.get("name") or "").strip().lower()
            if not key:
                continue
            by_key[key] = _merge_candidate_views(
                [by_key.get(key)] if by_key.get(key) else [],
                [record],
                limit=1,
            )[0]
            latest_ts[key] = str(record.get("source_step_ts") or latest_ts.get(key) or "")
    items = list(by_key.values())
    items.sort(key=lambda item: str(item.get("source_step_ts") or ""), reverse=True)
    return items[:limit]


def _candidate_records_from_orchestrator_events(*, limit: int = 80, scan_lines: int = 500) -> list[dict]:
    if not FACTOR_ORCHESTRATOR_EVENTS_FILE.exists():
        return []
    lines = _tail_jsonl_lines(
        FACTOR_ORCHESTRATOR_EVENTS_FILE,
        max_lines=max(1, int(scan_lines or 500)),
    )
    by_key: dict[str, dict] = {}
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        stage = str(event.get("stage") or "orchestrator_event")
        stage_step = {
            "ts": event.get("ts"),
            "run_id": event.get("run_id"),
            "round_id": event.get("round_id"),
            "stage_id": event.get("stage_id"),
            "stage": stage,
            "decision": event.get("decision") or event.get("summary") or "",
            "created_at": event.get("ts"),
        }
        grouped: dict[str, dict] = {}
        for idx, item in enumerate(_orchestrator_candidate_lane_items(event.get("candidate_lanes"), limit=24)):
            if not isinstance(item, dict):
                continue
            key = str(item.get("candidate_id") or item.get("expression") or item.get("factor_name") or f"lane:{idx}")
            grouped[key] = {**grouped.get(key, {}), **item}
        for idx, item in enumerate(event.get("evidence_refs") if isinstance(event.get("evidence_refs"), list) else []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("candidate_id") or item.get("expression") or item.get("factor_name") or f"evidence:{idx}")
            grouped[key] = {**grouped.get(key, {}), **item}
        for payload in grouped.values():
            record = _candidate_record_from_payload(payload, stage_step)
            if not record:
                continue
            key = str(record.get("expression") or record.get("name") or record.get("candidate_id") or "").strip().lower()
            if not key:
                continue
            by_key[key] = _merge_candidate_views([by_key.get(key)] if by_key.get(key) else [], [record], limit=1)[0]
            if len(by_key) >= limit:
                break
        if len(by_key) >= limit:
            break
    items = list(by_key.values())
    items.sort(key=lambda item: str(item.get("source_step_ts") or ""), reverse=True)
    return items[:limit]


def _runtime_view_from_research_steps(
    research_steps: list[dict],
    *,
    active_job: dict | None,
    quantgpt_summary: dict,
    registry_summary: dict,
    candidate_records: list[dict],
) -> dict:
    latest = (research_steps or [{}])[0] if research_steps else {}
    research_step_total = _count_research_step_history_lines()
    extra = _research_step_extra(latest)
    research_state = extra.get("research_state") if isinstance(extra.get("research_state"), dict) else {}
    transition = latest.get("stage_transition") if isinstance(latest.get("stage_transition"), dict) else {}
    phase_map = {
        "protocol_load": "Protocol Load",
        "pre_batch_decision": "Pre-batch Decision",
        "thesis_design": "Thesis Design",
        "hypothesis_design": "Hypothesis Design",
        "expression_design": "Expression Design",
        "brief": "Research Brief",
        "candidate_plan": "Candidate Plan",
        "score_review": "Score Review",
        "candidate_decision": "Candidate Decision",
        "novelty_review": "Novelty & ST Review",
        "deep_validation_review": "Deep Validation Review",
        "import_gate_review": "Import Gate Review",
        "import_review": "Import Review",
        "round_synthesis": "Round Synthesis",
        "four_step_summary": "Four-step Summary",
        "checkpoint_stop": "Checkpoint Stop",
        "blocker": "Blocked",
        "note": "Research Note",
    }
    stage = latest.get("stage") or (active_job or {}).get("stage") or "idle"
    stage_text = str(stage or "").strip().lower()
    if stage_text == "checkpoint_stop":
        runtime_status = "research_completed"
    elif stage_text == "blocker":
        runtime_status = "research_blocked"
    elif research_steps:
        runtime_status = (active_job or {}).get("status") or "research_active"
    else:
        runtime_status = (active_job or {}).get("status", "idle")
    updated_at = latest.get("ts") or latest.get("created_at") or ((active_job or {}).get("latest_event") or {}).get("ts")
    current_action = transition.get("next_action") or latest.get("decision") or latest.get("summary") or latest.get("next") or ""
    if stage_text == "blocker" and _orchestrator_event_is_interruption_blocker(latest):
        current_action = "已中断，需重启"
    imported = (
        research_state.get("valid_imports")
        or research_state.get("quality_gate_adopted")
        or (active_job or {}).get("summary", {}).get("valid_imports")
        or 0
    )
    return {
        "run_id": latest.get("run_id") or (active_job or {}).get("run_id"),
        "round_id": latest.get("round_id"),
        "stage": stage,
        "stage_seq": latest.get("stage_seq"),
        "stage_id": latest.get("stage_id"),
        "previous_stage": latest.get("previous_stage"),
        "previous_stage_id": latest.get("previous_stage_id"),
        "status": runtime_status,
        "current_phase": phase_map.get(str(stage), str(stage or "idle")),
        "current_action": current_action,
        "updated_at": updated_at,
        "latest_decision": latest.get("decision") or "",
        "next_action": transition.get("next_action") or latest.get("next") or "",
        "latest_step": latest,
        "stage_transition": transition,
        "progress_counts": {
            "research_steps": research_step_total or len(research_steps or []),
            "candidates": len(candidate_records or []),
            "imported": imported,
            "active_factors": registry_summary.get("active", 0),
            "quick_screened": research_state.get("quick_screened") or research_state.get("quick_screened_total"),
            "novelty_checked": research_state.get("novelty_checked") or research_state.get("novelty_checked_total"),
            "deep_validated": research_state.get("deep_validated") or research_state.get("deep_validation_count"),
            "quality_gate_adopted": research_state.get("quality_gate_adopted"),
            "quality_gate_rejected": research_state.get("quality_gate_rejected"),
            "qgpt_task_store_total": quantgpt_summary.get("total", 0),
            "qgpt_running": quantgpt_summary.get("running_count", 0),
        },
        "task_store_note": "no_task_store_records" if not quantgpt_summary.get("total") and research_steps else "",
    }


def _parse_task_ts(task: dict) -> str:
    return str(task.get("completed_at") or task.get("created_at") or "")


def _fetch_quantgpt_running_tasks(*, limit: int = 40) -> list[dict]:
    """Read active QuantGPT tasks from its existing DB without reconciling them."""
    db_path = QUANTGPT_DB
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = con.execute(
                "select id,user_id,session_id,status,task_type,parent_task_id,params,expression,result,error,created_at,updated_at "
                "from tasks where status = 'running' order by updated_at desc limit ?",
                (max(1, int(limit or 40)),),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [_quantgpt_db_task_from_row(row) for row in rows]


def _fetch_quantgpt_recent_tasks_snapshot(*, limit: int = 80) -> list[dict]:
    """Read the local QuantGPT task snapshot without a second HTTP round trip.

    The live-console route already probes QuantGPT health explicitly.  Reading
    the same task store through HTTP immediately afterwards added roughly a
    second to every cold GUI refresh and duplicated service work.  The SQLite
    store is QuantGPT's authoritative task ledger and this read remains
    strictly read-only.
    """
    db_path = QUANTGPT_DB
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = con.execute(
                "select id,user_id,session_id,status,task_type,parent_task_id,params,expression,result,error,created_at,updated_at "
                "from tasks order by updated_at desc limit ?",
                (max(1, int(limit or 80)),),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [_quantgpt_db_task_from_row(row) for row in rows]


def _quantgpt_stale_task_summary(tasks: list[dict], *, stale_seconds: int = FACTOR_ORCHESTRATOR_STALE_TASK_SECONDS) -> dict:
    """Expose potentially stuck QuantGPT task rows; never update status on read.

    QuantGPT's SQLite ``DateTime`` values are UTC but do not carry an offset.
    Treating them as local time makes a newly-created task appear several hours
    old on an Asia/Shanghai host, which can hide live work from the GUI.
    """
    stale: list[dict] = []
    threshold = max(60, int(stale_seconds or FACTOR_ORCHESTRATOR_STALE_TASK_SECONDS))
    for task in tasks or []:
        if str(task.get("status") or "").lower() not in {"running", "queued", "pending"}:
            continue
        ts_value = str(task.get("updated_at") or task.get("created_at") or "")
        task_ts = _parse_iso_ts(ts_value)
        if task_ts is None:
            continue
        try:
            if task_ts.tzinfo is None:
                task_ts = task_ts.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age_s = max(0, int((now - task_ts).total_seconds()))
        except Exception:
            continue
        if age_s < threshold:
            continue
        stale.append(
            {
                **_compact_task_for_live(task),
                "age_seconds": age_s,
                "last_activity_at": ts_value,
            }
        )
    return {
        "source": "quantgpt_db_read_only",
        "stale_threshold_seconds": threshold,
        "stale_count": len(stale),
        "tasks": stale[:20],
        "action": "inspect_or_retry_explicitly; no_status_mutation_on_read",
    }


def _quantgpt_summary_for_research_state(summary: dict, stale_indicator: dict | None) -> dict:
    """Do not let an old running task falsely make the current run active."""
    normalized = dict(summary or {})
    stale_rows = (stale_indicator or {}).get("tasks") if isinstance(stale_indicator, dict) else []
    stale_ids = {
        str(item.get("task_id") or item.get("id") or "")
        for item in stale_rows or []
        if isinstance(item, dict) and str(item.get("task_id") or item.get("id") or "")
    }
    running = normalized.get("running_tasks") if isinstance(normalized.get("running_tasks"), list) else []
    active_running = [
        task
        for task in running
        if str((task or {}).get("task_id") or (task or {}).get("id") or "") not in stale_ids
    ]
    observed_running_count = int(normalized.get("running_count") or len(running))
    normalized["observed_running_count"] = observed_running_count
    normalized["stale_running_count"] = max(0, observed_running_count - len(active_running))
    normalized["running_count"] = len(active_running)
    normalized["running_tasks"] = _jsonable(active_running)
    return normalized


def _fetch_quantgpt_recent_tasks(limit: int = 80, *, allow_restart: bool = False) -> list[dict]:
    """Read QuantGPT's native task store for foreground Codex/MCP runs.

    True MCP supervision happens in the foreground Codex window. FXAlpha keeps
    research_steps as the progress record while QuantGPT records every tool task;
    the GUI can safely observe that store without becoming a research runner.
    """
    health = _ensure_quantgpt_api_reachable(QUANTGPT_API_URL, allow_restart=allow_restart)
    if not health.get("reachable"):
        return []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = QUANTGPT_API_URL.rstrip("/")
    max_items = max(1, int(limit or 80))
    page_size = min(100, max_items)
    tasks: list[dict] = []
    page = 1
    while len(tasks) < max_items:
        url = f"{base}/api/v1/tasks?page={page}&page_size={page_size}"
        try:
            with opener.open(url, timeout=4) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            break
        page_tasks = payload.get("tasks") if isinstance(payload, dict) else []
        if not isinstance(page_tasks, list) or not page_tasks:
            break
        tasks.extend(task for task in page_tasks if isinstance(task, dict))
        if len(page_tasks) < page_size:
            break
        page += 1
    return tasks[:max_items]


def _research_step_task_ids(research_steps: list[dict], *, run_id: str = "", limit: int = 1000) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    active_run = str(run_id or ((research_steps or [{}])[0] or {}).get("run_id") or "").strip()
    for step in research_steps or []:
        if active_run and str(step.get("run_id") or "") != active_run:
            continue
        refs = step.get("evidence_refs") if isinstance(step.get("evidence_refs"), list) else []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            task_id = str(ref.get("task_id") or ref.get("qgpt_task_id") or "").strip()
            if not task_id or task_id in seen:
                continue
            seen.add(task_id)
            ids.append(task_id)
            if len(ids) >= max(1, int(limit or 1000)):
                return ids
    return ids


def _quantgpt_db_task_from_row(row: tuple) -> dict:
    task_id, user_id, session_id, status, task_type, parent_task_id, params, expression, result, error, created_at, updated_at = row
    return {
        "task_id": task_id,
        "id": task_id,
        "user_id": user_id,
        "session_id": session_id,
        "status": status,
        "task_type": task_type,
        "parent_task_id": parent_task_id,
        "params": _json_field(params),
        "expression": expression,
        "result": _json_field(result),
        "error": error,
        "created_at": created_at,
        "updated_at": updated_at,
        "completed_at": updated_at if status in {"completed", "failed", "error", "cancelled"} else None,
        "source": "quantgpt_db_by_task_id",
    }


def _fetch_quantgpt_tasks_by_ids(task_ids: list[str], *, limit: int = 1000) -> list[dict]:
    ids = []
    seen: set[str] = set()
    for task_id in task_ids or []:
        clean = str(task_id or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        ids.append(clean)
        if len(ids) >= max(1, int(limit or 1000)):
            break
    if not ids:
        return []
    db_path = QUANTGPT_DB
    if not db_path.exists():
        return []
    rows: list[tuple] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    con.execute(
                        "select id,user_id,session_id,status,task_type,parent_task_id,params,expression,result,error,created_at,updated_at "
                        f"from tasks where id in ({placeholders})",
                        chunk,
                    ).fetchall()
                )
        finally:
            con.close()
    except Exception:
        return []
    by_id = {str(row[0]): _quantgpt_db_task_from_row(row) for row in rows}
    return [by_id[task_id] for task_id in ids if task_id in by_id]


def _merge_quantgpt_tasks_by_id(*task_lists: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []
    for tasks in task_lists:
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id") or task.get("id") or "").strip()
            if not task_id:
                continue
            if task_id not in merged:
                order.append(task_id)
                merged[task_id] = task
            else:
                merged[task_id] = {**task, **merged[task_id]}
    return [merged[task_id] for task_id in order]


def _decision_view_from_research_steps(research_steps: list[dict]) -> dict:
    latest = (research_steps or [{}])[0] if research_steps else {}
    if not latest:
        return {}
    transition = latest.get("stage_transition") if isinstance(latest.get("stage_transition"), dict) else {}
    return {
        "schema_version": latest.get("schema_version"),
        "ts": latest.get("ts") or latest.get("created_at"),
        "run_id": latest.get("run_id"),
        "round_id": latest.get("round_id"),
        "stage_seq": latest.get("stage_seq"),
        "stage_id": latest.get("stage_id"),
        "previous_stage": latest.get("previous_stage"),
        "previous_stage_id": latest.get("previous_stage_id"),
        "stage": latest.get("stage"),
        "summary": latest.get("summary"),
        "decision": latest.get("decision"),
        "next": transition.get("next_action") or latest.get("next") or latest.get("next_action"),
        "priority": latest.get("priority"),
        "refs": latest.get("refs") or [],
        "evidence_refs": latest.get("evidence_refs") or [],
        "tags": latest.get("tags") or [],
        "round": latest.get("round") or latest.get("round_no"),
        "updated_at": latest.get("ts") or latest.get("created_at"),
        "stage_transition": transition,
    }


def _task_result_summary(task: dict) -> dict:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    if task.get("error"):
        return {"error": _truncate_text(task.get("error"), limit=260)}
    if not result:
        return {}
    outputs = result.get("outputs") if isinstance(result.get("outputs"), dict) else {}
    payload = outputs or result
    task_type = str(task.get("task_type") or "").lower()
    if task_type == "score":
        return {
            key: payload.get(key)
            for key in ("score", "quick_score", "grade", "quality_decision", "single_factor_decision")
            if payload.get(key) is not None
        }
    if task_type in {"backtest", "anti_overfit", "adversarial_validation", "rolling_validation"}:
        return {
            key: payload.get(key)
            for key in ("score", "recommendation", "passed_count", "total_count", "report_path", "status", "summary")
            if payload.get(key) is not None
        }
    if task_type == "novelty_check":
        return {
            "keepers": len(payload.get("keepers") or []),
            "dropped": len(payload.get("dropped") or []),
            "feedback": _truncate_text(payload.get("feedback"), limit=220),
        }
    if task_type == "quality_gate":
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        return {
            "adopted": len(payload.get("adopted") or []),
            "rejected": len(payload.get("rejected") or []),
            "screened_out": len(payload.get("screened_out") or []),
            "counts": counts,
            "feedback": _truncate_text(payload.get("feedback"), limit=220),
        }
    return {
        key: payload.get(key)
        for key in ("status", "score", "decision", "feedback", "report_path")
        if payload.get(key) is not None
    }


def _tool_timeline_from_tasks(tasks: list[dict], *, limit: int = 40) -> list[dict]:
    timeline = []
    for task in sorted(tasks or [], key=_parse_task_ts, reverse=True)[:limit]:
        timeline.append(
            {
                "task_id": task.get("task_id"),
                "task_type": task.get("task_type"),
                "status": task.get("status"),
                "expression": task.get("expression"),
                "created_at": task.get("created_at"),
                "completed_at": task.get("completed_at"),
                "duration": task.get("duration_seconds"),
                "error": _truncate_text(task.get("error"), limit=300) if task.get("error") else None,
                "result_summary": _jsonable(_task_result_summary(task)),
            }
        )
    return _jsonable(timeline)


def _task_stage(task: dict) -> str:
    task_type = str(task.get("task_type") or "").lower()
    status = str(task.get("status") or "").lower()
    failed = status in {"failed", "error", "score_error"} or bool(task.get("error"))
    if status == "running":
        if task_type == "score":
            return "quick_score_running"
        if task_type == "backtest":
            return "deep_validation_running"
        if task_type == "anti_overfit":
            return "anti_overfit_running"
        if task_type == "adversarial_validation":
            return "adversarial_validation_running"
        if task_type == "rolling_validation":
            return "rolling_validation_running"
        if task_type == "novelty_check":
            return "novelty_check_running"
        if task_type == "quality_gate":
            return "quality_gate_running"
        return f"{task_type or 'tool'}_running"
    if failed:
        if task_type == "score":
            return "quick_score_failed"
        if task_type == "backtest":
            return "deep_validation_failed"
        return f"{task_type or 'tool'}_failed"
    if task_type == "score":
        return "quick_score"
    if task_type == "backtest":
        return "deep_validation"
    if task_type == "anti_overfit":
        return "anti_overfit"
    if task_type == "adversarial_validation":
        return "adversarial_validation"
    if task_type == "rolling_validation":
        payload = _task_result_payload(task)
        rolling_status = str(payload.get("status") or (payload.get("summary") or {}).get("status") or "").lower()
        if rolling_status == "ok":
            return "rolling_validation_ready"
        if rolling_status in {"insufficient_history", "insufficient_data"}:
            return "rolling_validation_insufficient_history"
        if rolling_status in {"label_contract_error", "contract_error"}:
            return "rolling_validation_contract_error"
        return "rolling_validation_ready"
    if task_type == "novelty_check":
        return "novelty_check"
    if task_type == "quality_gate":
        return "quality_gate"
    return task_type or "quantgpt_task"


def _quantgpt_task_candidate_from_score(task: dict) -> dict:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    candidate = {
        "name": result.get("name") or task.get("expression"),
        "expression": result.get("expression") or task.get("expression"),
        "status": task.get("status"),
        "task_id": task.get("task_id"),
        "task_type": task.get("task_type"),
        "session_id": task.get("session_id"),
        "tool_ts": _parse_task_ts(task),
        "duration_seconds": task.get("duration_seconds"),
        "holding_period_days": (task.get("params") or {}).get("holding_period"),
        "source_tool": "quantgpt_task_store",
        "screening_stage": _task_stage(task),
    }
    if result:
        candidate.update(
            {
                "score": result.get("score"),
                "quick_score": result.get("quick_score", result.get("score")),
                "grade": result.get("grade"),
                "single_factor_decision": result.get("single_factor_decision"),
                "quality_decision": result.get("quality_decision"),
                "reject_reasons": result.get("reject_reasons"),
                "key_metrics": result.get("key_metrics"),
                "component_scores": result.get("component_scores"),
                "backtest_summary": result.get("backtest_summary"),
                "best_long_only_group_metrics": result.get("best_long_only_group_metrics"),
                "long_short_diagnostic_metrics": result.get("long_short_diagnostic_metrics"),
                "autocorrelation": result.get("autocorrelation"),
                "anti_overfit_summary": result.get("anti_overfit_summary"),
                "neutralization_applied": result.get("neutralization_applied"),
                "gate_result": result.get("gate_result"),
                "screening_hint": result.get("screening_hint"),
                "interpretation": result.get("interpretation"),
            }
        )
    if task.get("error"):
        candidate["reject_reasons"] = [str(task.get("error"))]
        candidate["quality_decision"] = "task_failed"
        candidate["screening_stage"] = _task_stage(task)
    return {key: value for key, value in candidate.items() if value is not None}


def _task_result_payload(task: dict) -> dict:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    outputs = result.get("outputs") if isinstance(result.get("outputs"), dict) else {}
    return outputs or result


def _task_param_candidates(task: dict) -> list[dict]:
    params = task.get("params") if isinstance(task.get("params"), dict) else {}
    candidates = params.get("candidates") if isinstance(params.get("candidates"), list) else []
    out = []
    for item in candidates:
        if isinstance(item, dict) and str(item.get("expression") or "").strip():
            out.append(item)
    expressions = params.get("expressions") if isinstance(params.get("expressions"), list) else []
    for expression in expressions:
        expr = str(expression or "").strip()
        if expr:
            out.append({"expression": expr})
    return out


def _task_candidate_payloads(task: dict) -> list[tuple[dict, str]]:
    payload = _task_result_payload(task)
    task_type = str(task.get("task_type") or "").lower()
    records: list[tuple[dict, str]] = []

    def add(item: object, source: str) -> None:
        if isinstance(item, dict):
            if item.get("expression") or item.get("candidate") or item.get("name") or item.get("factor_name"):
                records.append((item, source))
        elif isinstance(item, list):
            for sub_item in item:
                add(sub_item, source)

    if task_type == "score":
        add(payload, "score")
    elif task_type in {"backtest", "anti_overfit", "adversarial_validation", "rolling_validation"}:
        if task.get("expression"):
            add({"expression": task.get("expression"), **payload}, task_type)
        add(payload.get("candidate"), task_type)
    elif task_type == "novelty_check":
        for key, source in (
            ("keepers", "novelty_passed"),
            ("dropped", "novelty_rejected"),
            ("details", "novelty_detail"),
        ):
            add(payload.get(key), source)
    elif task_type == "quality_gate":
        for key, source in (
            ("adopted", "quality_gate_adopted"),
            ("rejected", "quality_gate_rejected"),
            ("screened_out", "quality_gate_screened"),
            ("dedup_dropped", "quality_gate_dedup"),
        ):
            add(payload.get(key), source)
        novelty = payload.get("novelty") if isinstance(payload.get("novelty"), dict) else {}
        for key, source in (("keepers", "novelty_passed"), ("dropped", "novelty_rejected")):
            add(novelty.get(key), source)

    if not records:
        for item in _task_param_candidates(task):
            records.append((item, f"{task_type}_input"))
    if not records and task.get("expression"):
        records.append(({"expression": task.get("expression")}, task_type or "task"))
    return records


def _candidate_expression_from_payload(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else payload
    return str(candidate.get("expression") or payload.get("expression") or "").strip()


def _candidate_name_from_payload(payload: dict, expression: str) -> str:
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else payload
    name = str(candidate.get("name") or candidate.get("factor_name") or payload.get("label") or "").strip()
    return name or expression[:80]


def _quality_gate_decision_from_source(source: str, payload: dict) -> str:
    screening = payload.get("screening") if isinstance(payload.get("screening"), dict) else {}
    gate = payload.get("gate_result") if isinstance(payload.get("gate_result"), dict) else {}
    if "adopted" in source:
        return "adopt"
    if "rejected" in source or "screened" in source or "dedup" in source:
        return screening.get("decision") or gate.get("decision") or "reject"
    return screening.get("decision") or gate.get("decision") or payload.get("quality_decision") or ""


def _task_candidate_metric(payload: dict, *names: str) -> object:
    return _candidate_metric(payload, *names)


def _populate_console_deep_score(candidate: dict) -> None:
    """Populate official deep score for read-only console candidate views."""

    if not isinstance(candidate, dict):
        return
    if candidate.get("deep_score") is not None:
        return
    try:
        quick_score = _candidate_metric(candidate, "quick_score", "score")
        deep_score, score_parts = quality_gate._compute_deep_score(candidate, quick_score=float(quick_score) if quick_score is not None else None)
    except Exception:
        return
    missing = score_parts.get("missing_components") if isinstance(score_parts, dict) else []
    if missing:
        return
    candidate["deep_score"] = deep_score
    candidate["official_grade"] = score_parts.get("official_grade")
    candidate["component_scores"] = candidate.get("component_scores") or score_parts.get("component_scores")
    deep_validation = candidate.get("deep_validation") if isinstance(candidate.get("deep_validation"), dict) else {}
    candidate["deep_validation"] = {
        **deep_validation,
        "deep_score": deep_score,
        "score_parts": score_parts,
        "source": deep_validation.get("source") or "console_quality_gate_preview",
    }


def _candidate_view_stage_rank(stage: object) -> int:
    text = str(stage or "").lower()
    running_penalty = -2 if "running" in text else 0
    order = (
        ("imported", 80),
        ("adopted", 70),
        ("quality_gate", 60),
        ("import_gate", 60),
        ("deep", 50),
        ("adversarial", 48),
        ("anti_overfit", 46),
        ("backtest", 44),
        ("novelty", 40),
        ("planned_for_score", 24),
        ("precheck_warning", 22),
        ("candidate_plan_dropped", 21),
        ("precheck_blocked", 20),
        ("quick", 30),
        ("score", 30),
    )
    for token, rank in order:
        if token in text:
            return rank + running_penalty
    return 0


def _candidate_view_decision_rank(value: object) -> int:
    text = str(value or "").lower()
    order = (
        ("imported", 80),
        ("adopt", 75),
        ("quality_gate", 65),
        ("deep_validate", 55),
        ("deep_validated", 54),
        ("success", 50),
        ("scored", 45),
        ("reject", 40),
        ("blocked", 35),
        ("running", 30),
        ("planned_for_score", 20),
        ("recorded", 10),
        ("dropped", 5),
    )
    for token, rank in order:
        if token in text:
            return rank
    return 0


def _merge_candidate_views(primary: list[dict], supplemental: list[dict], *, limit: int = 50) -> list[dict]:
    grouped: dict[str, dict] = {}

    def key_for(item: dict) -> str:
        round_id = str(item.get("round_id") or "").strip()
        candidate_id = str(item.get("candidate_id") or "").strip()
        if round_id and candidate_id:
            return f"process:{round_id}:{candidate_id}".lower()
        return str(item.get("expression") or item.get("name") or item.get("factor_name") or "").strip().lower()

    def merge(base: dict, extra: dict) -> dict:
        merged = dict(base)
        for key, value in (extra or {}).items():
            if value in (None, "", [], {}):
                continue
            if key in {"metrics", "gate_result", "deep_validation", "novelty_guard", "novelty_metrics", "screening"} and isinstance(value, dict):
                merged[key] = {**(merged.get(key) if isinstance(merged.get(key), dict) else {}), **value}
            elif key in {"reject_reasons", "veto_reasons", "task_history"} and isinstance(value, list):
                merged[key] = list((merged.get(key) or []) + [item for item in value if item not in (merged.get(key) or [])])
            elif key in {"screening_stage", "stage"}:
                current_rank = _candidate_view_stage_rank(merged.get(key) or merged.get("screening_stage"))
                next_rank = _candidate_view_stage_rank(value)
                if next_rank >= current_rank:
                    merged[key] = value
            elif key == "status":
                current_rank = _candidate_view_decision_rank(merged.get(key))
                next_rank = _candidate_view_decision_rank(value)
                if next_rank >= current_rank:
                    merged[key] = value
            elif key in {"quality_decision", "gate_decision"}:
                current_rank = _candidate_view_decision_rank(merged.get(key))
                next_rank = _candidate_view_decision_rank(value)
                if next_rank >= current_rank:
                    merged[key] = value
            elif merged.get(key) in (None, "", [], {}):
                merged[key] = value
        _populate_console_deep_score(merged)
        return merged

    for items in (primary or [], supplemental or []):
        for source in items:
            if not isinstance(source, dict):
                continue
            key = key_for(source)
            if not key:
                continue
            grouped[key] = merge(grouped.get(key) or {}, source)
    return sorted(
        grouped.values(),
        key=lambda item: str(item.get("latest_task_ts") or item.get("tool_ts") or item.get("source_step_ts") or item.get("ts") or ""),
        reverse=True,
    )[:limit]


def _quantgpt_task_candidates(tasks: list[dict], *, factor_library: dict | None = None, limit: int = 50) -> list[dict]:
    active_expressions = {
        str(item.get("expression") or "").strip()
        for item in (factor_library or {}).get("items", [])
        if str(item.get("status") or "").lower() == "active" and item.get("expression")
    }
    grouped: dict[str, dict] = {}
    for task in sorted(tasks or [], key=_parse_task_ts):
        task_type = str(task.get("task_type") or "").lower()
        result = _task_result_payload(task)
        for payload, source in _task_candidate_payloads(task):
            expression = _candidate_expression_from_payload(payload)
            if not expression:
                continue
            candidate_payload = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else payload
            novelty_guard = (
                candidate_payload.get("novelty_guard")
                if isinstance(candidate_payload.get("novelty_guard"), dict)
                else payload
                if source == "novelty_detail"
                else {}
            )
            screening = candidate_payload.get("screening") if isinstance(candidate_payload.get("screening"), dict) else {}
            gate = candidate_payload.get("gate_result") if isinstance(candidate_payload.get("gate_result"), dict) else {}
            entry = grouped.setdefault(
                expression,
                {
                    "name": _candidate_name_from_payload(candidate_payload, expression),
                    "display_name": _candidate_name_from_payload(candidate_payload, expression),
                    "expression": expression,
                    "source_tool": "quantgpt_task_store",
                    "console_scope": "task_store_history",
                    "task_history": [],
                },
            )
            entry["task_history"].append(
                {
                    "task_id": task.get("task_id"),
                    "task_type": task.get("task_type"),
                    "status": task.get("status"),
                    "created_at": task.get("created_at"),
                    "completed_at": task.get("completed_at"),
                    "duration_seconds": task.get("duration_seconds"),
                    "error": task.get("error"),
                    "source": source,
                    "result_summary": _task_result_summary(task),
                }
            )
            entry["name"] = entry.get("name") or _candidate_name_from_payload(candidate_payload, expression)
            entry["display_name"] = entry.get("display_name") or entry.get("name")
            entry["latest_status"] = task.get("status") or entry.get("latest_status")
            entry["latest_task_ts"] = _parse_task_ts(task) or entry.get("latest_task_ts")
            entry["tool_ts"] = entry["latest_task_ts"]
            entry["status"] = task.get("status") or entry.get("status")
            entry["task_id"] = task.get("task_id") or entry.get("task_id")
            entry["task_type"] = task.get("task_type") or entry.get("task_type")
            entry["session_id"] = task.get("session_id") or entry.get("session_id")
            entry["duration_seconds"] = task.get("duration_seconds") or entry.get("duration_seconds")
            entry["holding_period_days"] = (task.get("params") or {}).get("holding_period") or entry.get("holding_period_days")
            entry["screening_stage"] = source if source not in {"score"} else _task_stage(task)

            if task_type == "score":
                entry.update(_quantgpt_task_candidate_from_score({**task, "result": result}))
            elif task_type == "backtest":
                entry.update(
                    {
                        "backtest_status": task.get("status"),
                        "backtest_summary": result.get("backtest_summary") or result.get("metrics"),
                        "report_metrics": result.get("metrics"),
                        "neutralization_applied": result.get("neutralization_applied"),
                        "report_path": result.get("report_path"),
                        "screening_stage": "deep_validation",
                    }
                )
                if not entry.get("key_metrics"):
                    entry["key_metrics"] = result.get("backtest_summary") or result.get("metrics")
            elif task_type == "anti_overfit":
                score = result.get("score") or candidate_payload.get("score")
                entry.update(
                    {
                        "anti_overfit_status": task.get("status"),
                        "anti_overfit_score": score,
                        "anti_overfit_summary": {
                            "score": score,
                            "recommendation": result.get("recommendation"),
                            "passed_count": result.get("passed_count"),
                            "total_count": result.get("total_count"),
                            "test_scores": result.get("test_scores"),
                            "tests": result.get("tests"),
                        },
                        "autocorrelation": result.get("autocorrelation"),
                        "screening_stage": "anti_overfit",
                    }
                )
            elif task_type == "adversarial_validation":
                score = result.get("score") or candidate_payload.get("score")
                entry.update(
                    {
                        "adversarial_status": task.get("status"),
                        "adversarial_score": score,
                        "adversarial_validation": {
                            "score": score,
                            "recommendation": result.get("recommendation"),
                            "passed_count": result.get("passed_count"),
                            "total_count": result.get("total_count"),
                            "test_scores": result.get("test_scores"),
                            "tests": result.get("tests"),
                        },
                        "screening_stage": "adversarial_validation",
                    }
                )
            elif task_type == "rolling_validation":
                rolling_status = str(result.get("status") or "").lower()
                stage = {
                    "ok": "rolling_validation_ready",
                    "insufficient_history": "rolling_validation_insufficient_history",
                    "insufficient_data": "rolling_validation_insufficient_history",
                    "label_contract_error": "rolling_validation_contract_error",
                    "contract_error": "rolling_validation_contract_error",
                }.get(rolling_status, "rolling_validation_ready")
                entry.update(
                    {
                        "rolling_validation": result,
                        "rolling_status": rolling_status,
                        "screening_stage": stage,
                    }
                )
            elif task_type == "novelty_check":
                combined_guard = candidate_payload.get("combined_guard") or {}
                st_exposure_guard = candidate_payload.get("st_exposure_guard") or {}
                allowed = combined_guard.get("allowed") if combined_guard else novelty_guard.get("allowed")
                entry.update(
                    {
                        "novelty_status": "passed" if allowed is True or "passed" in source else "rejected" if allowed is False or "rejected" in source else task.get("status"),
                        "novelty_reason": combined_guard.get("reason") or novelty_guard.get("reason") or result.get("feedback"),
                        "novelty_metrics": novelty_guard,
                        "novelty_guard": novelty_guard,
                        "st_exposure_guard": st_exposure_guard,
                        "combined_guard": combined_guard,
                        "screening_stage": source,
                    }
                )
            elif task_type == "quality_gate":
                decision = _quality_gate_decision_from_source(source, candidate_payload)
                entry.update(
                    {
                        "quality_gate_status": task.get("status"),
                        "quality_gate_decision": decision,
                        "quality_gate_reason": screening.get("reason") or gate.get("reason") or result.get("feedback"),
                        "gate_decision": decision,
                        "quality_decision": decision or entry.get("quality_decision"),
                        "screening": screening or entry.get("screening"),
                        "gate_result": gate or entry.get("gate_result"),
                        "deep_validation": candidate_payload.get("deep_validation") or entry.get("deep_validation"),
                        "component_scores": candidate_payload.get("component_scores") or entry.get("component_scores"),
                        "veto_reasons": candidate_payload.get("veto_reasons") or entry.get("veto_reasons"),
                        "rolling_validation": candidate_payload.get("rolling_validation") or entry.get("rolling_validation"),
                        "screening_stage": source,
                    }
                )

            for target_key, names in {
                "quick_score": ("quick_score", "score"),
                "deep_score": ("deep_score",),
                "grade": ("grade", "official_grade"),
                "ic": ("ic", "ic_mean"),
                "icir": ("icir", "ic_ir"),
                "rank_ic": ("rank_ic", "rank_ic_mean"),
                "rank_icir": ("rank_icir", "rank_ic_ir"),
                "sharpe": ("sharpe",),
                "annual_return": ("annual_return", "annualized_return"),
            }.items():
                if target_key == "quick_score" and task_type != "score" and candidate_payload.get("quick_score") is None:
                    continue
                value = _task_candidate_metric(candidate_payload, *names)
                if value is not None:
                    entry[target_key] = value

            if task.get("error"):
                reasons = list(entry.get("reject_reasons") or [])
                reasons.append(str(task.get("error")))
                entry["reject_reasons"] = reasons
                entry["quality_decision"] = "task_failed"

    candidates = []
    for candidate in grouped.values():
        _populate_console_deep_score(candidate)
        expression = str(candidate.get("expression") or "").strip()
        if expression in active_expressions:
            candidate["registry_status"] = "active"
            candidate["quality_decision"] = "imported"
            candidate["screening_stage"] = "imported"
        else:
            candidate["registry_status"] = candidate.get("registry_status") or "not_in_registry"
        candidate["metrics"] = {
            "ic_mean": candidate.get("ic"),
            "ic_ir": candidate.get("icir"),
            "rank_ic_mean": candidate.get("rank_ic"),
            "rank_ic_ir": candidate.get("rank_icir"),
            "sharpe": candidate.get("sharpe"),
            "annual_return": candidate.get("annual_return"),
        }
        candidate["task_history"] = sorted(
            candidate.get("task_history") or [],
            key=lambda item: str(item.get("completed_at") or item.get("created_at") or ""),
            reverse=True,
        )[:12]
        digest = _candidate_digest(
            candidate,
            {"ts": candidate.get("tool_ts"), "session_id": candidate.get("session_id")},
            source="quantgpt_task_store",
        )
        if digest:
            digest.update(
                {
                    key: _jsonable(candidate.get(key))
                    for key in (
                        "display_name",
                        "latest_status",
                        "latest_task_ts",
                        "task_history",
                        "novelty_status",
                        "novelty_reason",
                        "novelty_metrics",
                        "backtest_status",
                        "anti_overfit_status",
                        "anti_overfit_score",
                        "adversarial_status",
                        "adversarial_score",
                        "quality_gate_status",
                        "quality_gate_decision",
                        "quality_gate_reason",
                        "gate_decision",
                        "registry_status",
                        "rolling_validation",
                        "deep_validation",
                        "gate_result",
                        "component_scores",
                        "veto_reasons",
                    )
                    if candidate.get(key) not in (None, "", [], {})
                }
            )
            candidates.append(digest)
    return sorted(candidates, key=lambda item: str(item.get("latest_task_ts") or item.get("tool_ts") or item.get("ts") or ""), reverse=True)[:limit]


def _quantgpt_task_summary(tasks: list[dict]) -> dict:
    by_type: dict[str, dict[str, int]] = {}
    for task in tasks or []:
        task_type = str(task.get("task_type") or "unknown")
        status = str(task.get("status") or "unknown")
        by_type.setdefault(task_type, {})
        by_type[task_type][status] = by_type[task_type].get(status, 0) + 1
    latest = sorted(tasks or [], key=_parse_task_ts, reverse=True)
    running = [task for task in latest if task.get("status") == "running"]
    return {
        "total": len(tasks or []),
        "by_type": by_type,
        "running_count": len(running),
        "latest_task": _jsonable(latest[0]) if latest else {},
        "latest_non_running_task": _jsonable(next((task for task in latest if task.get("status") != "running"), {})),
        "running_tasks": _jsonable(running[:8]),
    }


def _quantgpt_task_context(task: dict) -> dict:
    """Read the orchestrator lineage embedded in a QuantGPT task."""
    if not isinstance(task, dict):
        return {}
    params = task.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (TypeError, ValueError):
            params = {}
    if not isinstance(params, dict):
        params = {}
    return {
        "run_id": str(task.get("run_id") or params.get("run_id") or "").strip(),
        "round_id": str(task.get("round_id") or params.get("round_id") or "").strip(),
        "stage_id": str(task.get("stage_id") or params.get("stage_id") or "").strip(),
    }


def _quantgpt_tasks_for_research_run(tasks: list[dict], run_id: str) -> list[dict]:
    """Scope GUI task evidence to the run represented by research_steps.

    Tasks without lineage are intentionally excluded when a run is known: they
    cannot safely establish what the current research run is doing.  The raw
    QuantGPT task store remains untouched and run detail views already fetch by
    task ids from the canonical research log.
    """
    run_text = str(run_id or "").strip()
    if not run_text:
        return list(tasks or [])
    return [
        task
        for task in tasks or []
        if _quantgpt_task_context(task).get("run_id") == run_text
    ]


def _candidate_board_stage_from_step(step: dict, payload: dict) -> tuple[str, str]:
    stage = str(step.get("stage") or "").strip()
    if stage == "score_review":
        if payload.get("score") is not None or payload.get("quick_score") is not None:
            return "quick_score", str(payload.get("status") or "scored")
        if "candidate_" in str(step.get("stage_id") or "") or "正在执行 score_factor" in str(step.get("decision") or ""):
            return "quick_score_running", "running"
        return "score_review", "pending"
    if stage == "candidate_plan":
        lane = str(payload.get("candidate_lane") or payload.get("status") or "")
        if lane in {
            "planned_for_score",
            "precheck_blocked",
            "precheck_warning",
            "semantic_revision",
            "candidate_plan_dropped",
        }:
            return lane, str(payload.get("status") or lane)
        return "planned_for_score", str(payload.get("status") or "planned_for_score")
    if stage == "novelty_review":
        return "novelty_review", str(payload.get("status") or payload.get("quality_decision") or "reviewed")
    if stage == "deep_validation_review":
        return "deep_validation", str(payload.get("status") or payload.get("quality_decision") or "reviewed")
    if stage in {"import_gate_review", "import_review"}:
        return "quality_gate", str(payload.get("status") or payload.get("quality_decision") or "reviewed")
    return stage or "research_step", str(payload.get("status") or payload.get("quality_decision") or "recorded")


def _current_candidate_board_payloads(step: dict) -> list[dict]:
    payloads: list[dict] = []
    stage = str(step.get("stage") or "").strip()

    def add(item: object) -> None:
        if isinstance(item, dict):
            if stage in {"thesis_design", "hypothesis_design"} and item.get("candidate_id") and not item.get("expression"):
                return
            payloads.append(item)
        elif isinstance(item, list):
            for sub_item in item:
                add(sub_item)

    for key in ("candidate_lanes", "candidate_decisions"):
        add(step.get(key))
    for ref in step.get("evidence_refs") if isinstance(step.get("evidence_refs"), list) else []:
        if not isinstance(ref, dict):
            continue
        if ref.get("type") == "candidate_lanes":
            add(ref.get("items"))
        elif stage == "novelty_review" and ref.get("type") == "advice_summary":
            for item in ref.get("candidate_lane_decisions") if isinstance(ref.get("candidate_lane_decisions"), list) else []:
                if not isinstance(item, dict):
                    continue
                enriched = dict(item)
                action = str(enriched.get("action") or "").strip()
                reason = str(enriched.get("reason") or "").strip()
                combined_guard = enriched.get("combined_guard") if isinstance(enriched.get("combined_guard"), dict) else {}
                novelty_score = enriched.get("novelty_score")
                novelty_allowed = combined_guard.get("novelty_allowed")
                if novelty_allowed is None:
                    novelty_allowed = action == "advance_to_deep_validation"
                if novelty_score is not None or action or reason:
                    enriched["novelty_guard"] = {
                        "allowed": bool(novelty_allowed),
                        "decision": action,
                        "reason": reason,
                        "score": novelty_score,
                        "matched_existing_factor": enriched.get("matched_existing_factor"),
                    }
                    enriched["status"] = enriched.get("status") or ("success" if novelty_allowed else "reviewed")
                    enriched["quality_decision"] = enriched.get("quality_decision") or ("deep_validate" if novelty_allowed else "reject")
                    enriched["single_factor_decision"] = enriched.get("single_factor_decision") or enriched["quality_decision"]
                    enriched["candidate_lane"] = enriched.get("candidate_lane") or ("keepers" if novelty_allowed else "dropped")
                add(enriched)
        elif ref.get("candidate_id") or ref.get("expression"):
            payloads.append(ref)
    monitoring = step.get("monitoring") if isinstance(step.get("monitoring"), dict) else {}
    add(monitoring.get("candidate_watch"))
    return payloads


def _current_candidate_board(research_steps: list[dict], quantgpt_tasks: list[dict], *, limit: int = 40) -> dict:
    latest = (research_steps or [{}])[0] if research_steps else {}
    run_id = str(latest.get("run_id") or "").strip()
    current_round_id = str(latest.get("round_id") or "").strip()
    if not run_id or not current_round_id:
        return {
            "schema_version": "current_candidate_board_v1",
            "ok": False,
            "source": "research_steps_current_run",
            "errors": [{"code": "missing_active_run", "message": "latest research step has no run_id/round_id"}],
            "candidates": [],
        }

    tasks_by_id = {str(task.get("task_id") or ""): task for task in quantgpt_tasks or [] if task.get("task_id")}
    same_run = [
        step
        for step in research_steps or []
        if str(step.get("run_id") or "") == run_id and str(step.get("round_id") or "").strip()
    ]
    same_run.sort(key=lambda item: str(item.get("ts") or item.get("created_at") or ""))
    candidates: dict[str, dict] = {}
    errors: list[dict] = []
    round_ids = sorted({str(step.get("round_id") or "").strip() for step in same_run if step.get("round_id")})

    def process_key(step: dict, candidate_id: str) -> str:
        return f"{str(step.get('round_id') or '').strip()}:{str(candidate_id or '').strip()}".lower()

    def has_explicit_novelty_evidence(payload: dict) -> bool:
        """A generic novelty-review lane must not overwrite an earlier fact."""
        return any(
            payload.get(key) not in (None, "", [], {})
            for key in (
                "novelty_guard",
                "combined_guard",
                "novelty_score",
                "novelty_metrics",
                "novelty_correlation",
                "matched_existing_factor",
                "matched_existing_factor_id",
                "matched_existing_factor_name",
                "matched_existing_expression_summary",
                "matched_information_cluster_id",
            )
        )

    def refresh_display_state(candidate: dict) -> None:
        """Attach one canonical, user-facing status without overwriting evidence."""
        stage = str(candidate.get("screening_stage") or candidate.get("stage") or "").lower()
        decision = str(candidate.get("single_factor_decision") or candidate.get("quality_decision") or "").lower()
        novelty = candidate.get("novelty_guard") if isinstance(candidate.get("novelty_guard"), dict) else {}
        quick_score = candidate.get("quick_score", candidate.get("score"))

        candidate["final_decision"] = decision or "pending"
        if quick_score is not None and candidate.get("grade") not in (None, "", "--"):
            candidate["quick_grade"] = candidate.get("grade")
            candidate["grade_provenance"] = "quick_score"

        if novelty.get("allowed") is False:
            candidate["display_status_label"] = "因子库互相关拦截"
            candidate["display_status_reason"] = str(novelty.get("reason") or novelty.get("decision") or "novelty_rejected")
            return
        if "novelty" in stage:
            candidate["display_status_label"] = "待深验" if decision in {"deep_validate", "advance_to_deep_validation"} else "互相关检测中"
            candidate["display_status_reason"] = str(novelty.get("reason") or candidate.get("status_reason") or "")
            return
        if "quick_score" in stage or stage == "score_factor":
            if quick_score is None:
                candidate["display_status_label"] = "快筛中"
            elif decision in {"reject", "screen", "veto"}:
                candidate["display_status_label"] = "快筛拦截"
            else:
                candidate["display_status_label"] = "待互相关检测"
            quick_reason = str(candidate.get("screening_hint", {}).get("reason") if isinstance(candidate.get("screening_hint"), dict) else "")
            if quick_reason:
                candidate["display_status_reason"] = quick_reason

    def merge_candidate(candidate_id: str, payload: dict, step: dict) -> None:
        candidate_id = str(candidate_id or "").strip()
        if not candidate_id:
            errors.append({"code": "missing_candidate_id", "stage_id": step.get("stage_id"), "stage": step.get("stage")})
            return
        stage, status = _candidate_board_stage_from_step(step, payload)
        candidate_key = process_key(step, candidate_id)
        round_id = str(step.get("round_id") or "").strip()
        existing = candidates.get(candidate_key)
        expression = str(payload.get("expression") or (existing or {}).get("expression") or "").strip()
        if existing is None and not expression:
            return
        # Some review summaries repeat a candidate id only to describe the
        # batch.  Without a novelty metric/decision they are not per-candidate
        # novelty evidence.  Keep an earlier candidate-plan/score fact rather
        # than making a never-scored candidate look as if novelty had run.
        if stage == "novelty_review" and not has_explicit_novelty_evidence(payload):
            if existing is not None:
                existing.setdefault("stage_history", []).append(
                    {
                        "ts": step.get("ts") or step.get("created_at"),
                        "stage": step.get("stage"),
                        "stage_id": step.get("stage_id"),
                        "screening_stage": "novelty_review_unlinked",
                        "status": "summary_only",
                    }
                )
            return
        if existing is None:
            existing = {
                "schema_version": "current_candidate_v1",
                "run_id": run_id,
                "round_id": round_id,
                "candidate_id": candidate_id,
                "process_key": candidate_key,
                "stage_history": [],
                "tool_evidence": [],
                "evidence_errors": [],
                "source": "research_steps",
                "console_scope": "current_run",
            }
        if expression:
            existing["expression"] = expression
            existing["name"] = existing.get("name") or expression[:80]
        existing["stage"] = stage
        existing["screening_stage"] = stage
        existing["status"] = status
        existing["quality_decision"] = payload.get("quality_decision") or payload.get("single_factor_decision") or existing.get("quality_decision") or status
        for field in (
            # Candidate-plan facts are not score evidence, but they are the
            # authoritative explanation of why a candidate did or did not
            # proceed to the score tools.  Keep them in the board projection
            # so the UI never has to invent a generic stage label.
            "candidate_lane",
            "precheck_status",
            "precheck_instruction",
            "precheck_warnings",
            "status_label",
            "status_reason",
            "reason",
            "action",
            "keep",
            "score",
            "quick_score",
            "grade",
            "single_factor_decision",
            "reject_reasons",
            "screening_hint",
            "gate_result",
            "key_metrics",
            "backtest_summary",
            "deep_score",
            "deep_action",
            "deep_reason",
            "anti_overfit_score",
            "rolling_score",
            "adversarial_score",
            "ic",
            "icir",
            "rank_ic",
            "rank_icir",
            "rolling_validation",
            "anti_overfit",
            "adversarial_validation",
            "novelty_score",
            "novelty_guard",
            "combined_guard",
            "matched_existing_factor",
            "matched_existing_factor_id",
            "matched_existing_factor_name",
            "matched_existing_expression_summary",
            "matched_information_cluster_id",
            "matched_reference_source",
            "st_exposure_guard",
        ):
            value = payload.get(field)
            if value not in (None, "", [], {}):
                if field == "score":
                    existing["quick_score"] = value
                    existing["score"] = value
                else:
                    existing[field] = _jsonable(value)
        existing["source_step_ts"] = step.get("ts") or step.get("created_at")
        existing["source_stage_id"] = step.get("stage_id")
        existing["stage_history"].append(
            {
                "ts": step.get("ts") or step.get("created_at"),
                "stage": step.get("stage"),
                "stage_id": step.get("stage_id"),
                "screening_stage": stage,
                "status": status,
            }
        )
        candidates[candidate_key] = existing

    def candidate_has_research_step_evidence(candidate: dict, tool: str) -> bool:
        tool = str(tool or "")
        if tool == "score_factor":
            return (
                candidate.get("quick_score") is not None
                or candidate.get("score") is not None
                or bool(candidate.get("grade"))
                or str(candidate.get("status") or "") in {"invalid_expression", "score_error"}
            )
        if tool in {"fxalpha_novelty_check", "novelty_check", "novelty"}:
            return bool(candidate.get("novelty_guard") or candidate.get("combined_guard") or candidate.get("novelty_score") is not None)
        if tool in {"deep_validation", "run_backtest", "run_anti_overfit", "run_rolling_validation", "run_adversarial_validation"}:
            return bool(
                candidate.get("deep_score") is not None
                or candidate.get("backtest_summary")
                or candidate.get("rolling_validation")
                or candidate.get("anti_overfit")
                or candidate.get("adversarial_validation")
                or candidate.get("key_metrics")
            )
        if tool in {"fxalpha_quality_gate", "quality_gate", "fxalpha_import_factors", "import_factors"}:
            return bool(candidate.get("gate_result") or candidate.get("quality_decision"))
        return False

    def candidate_has_any_research_step_evidence(candidate: dict) -> bool:
        if not isinstance(candidate, dict):
            return False
        if candidate.get("quick_score") is not None or candidate.get("score") is not None:
            return True
        if candidate.get("deep_score") is not None or candidate.get("novelty_score") is not None:
            return True
        if candidate.get("grade") not in (None, "", "--", "P"):
            return True
        for key in (
            "screening_hint",
            "gate_result",
            "key_metrics",
            "backtest_summary",
            "rolling_validation",
            "anti_overfit",
            "adversarial_validation",
            "novelty_guard",
            "combined_guard",
        ):
            if candidate.get(key) not in (None, "", [], {}):
                return True
        return False

    def add_detached_tool_evidence(candidate: dict, *, tool: str, stage_id: object = None, task_id: str = "", message: str) -> None:
        note = {
            "tool": tool,
            "stage_id": stage_id,
            "message": message,
        }
        if task_id:
            note["task_id"] = task_id
        existing_notes = candidate.setdefault("detached_tool_evidence", [])
        if note not in existing_notes:
            existing_notes.append(note)
        if not task_id:
            legacy_notes = candidate.setdefault("unlinked_tool_evidence", [])
            if note not in legacy_notes:
                legacy_notes.append(note)

    for step in same_run:
        for payload in _current_candidate_board_payloads(step):
            if isinstance(payload, dict):
                merge_candidate(str(payload.get("candidate_id") or ""), payload, step)
        refs = step.get("evidence_refs") if isinstance(step.get("evidence_refs"), list) else []
        for ref in refs:
            if not isinstance(ref, dict) or not ref.get("tool"):
                continue
            candidate_id = str(ref.get("candidate_id") or "").strip()
            if not candidate_id:
                continue
            tool = str(ref.get("tool") or "")
            task_id = str(ref.get("task_id") or ref.get("qgpt_task_id") or "").strip()
            key = process_key(step, candidate_id)
            round_id = str(step.get("round_id") or "").strip()
            if key not in candidates and not ref.get("expression"):
                continue
            candidate = candidates.setdefault(
                key,
                {
                    "schema_version": "current_candidate_v1",
                    "run_id": run_id,
                    "round_id": round_id,
                    "candidate_id": candidate_id,
                    "process_key": key,
                    "stage_history": [],
                    "tool_evidence": [],
                    "evidence_errors": [],
                    "source": "research_steps",
                    "console_scope": "current_run",
                },
            )
            if not task_id:
                progress_tags = {str(tag) for tag in step.get("tags") or []}
                is_progress_ref = (
                    ref.get("candidate_index") is not None
                    or ref.get("candidate_total") is not None
                    or "tool_progress" in progress_tags
                    or "candidate_progress" in progress_tags
                )
                if is_progress_ref:
                    candidate.setdefault("pending_evidence", []).append(
                        {
                            "tool": tool,
                            "stage_id": step.get("stage_id"),
                            "candidate_index": ref.get("candidate_index"),
                            "candidate_total": ref.get("candidate_total"),
                            "message": "tool progress checkpoint; task_id is not available until the tool result is recorded",
                        }
                    )
                    continue
                if candidate_has_research_step_evidence(candidate, tool):
                    add_detached_tool_evidence(
                        candidate,
                        tool=tool,
                        stage_id=step.get("stage_id"),
                        message="tool evidence is summarized in research_steps; no QuantGPT task_id was emitted",
                    )
                    continue
                candidate["evidence_errors"].append(
                    {
                        "code": "missing_task_link",
                        "tool": tool,
                        "stage_id": step.get("stage_id"),
                        "message": "research step tool evidence has no task_id; QuantGPT DB result was not attached",
                    }
                )
                continue
            task = tasks_by_id.get(task_id)
            if not task:
                if candidate_has_research_step_evidence(candidate, tool):
                    add_detached_tool_evidence(
                        candidate,
                        tool=tool,
                        stage_id=step.get("stage_id"),
                        task_id=task_id,
                        message="tool evidence is summarized in research_steps; referenced QuantGPT task is not available in the recent task window",
                    )
                    continue
                candidate["evidence_errors"].append({"code": "task_not_found", "tool": tool, "task_id": task_id})
                continue
            result = task.get("result") if isinstance(task.get("result"), dict) else {}
            outputs = result.get("outputs") if isinstance(result.get("outputs"), dict) else {}
            payload = outputs or result
            task_type = str(task.get("task_type") or "").lower()
            candidate["tool_evidence"].append(
                {
                    "tool": tool,
                    "task_id": task_id,
                    "task_type": task.get("task_type"),
                    "status": task.get("status"),
                    "created_at": task.get("created_at"),
                    "completed_at": task.get("completed_at"),
                    "error": task.get("error"),
                }
            )
            is_score_task = tool == "score_factor" or task_type == "score"
            if is_score_task:
                for key in ("score", "quick_score", "grade", "single_factor_decision", "quality_decision", "reject_reasons", "screening_hint", "gate_result", "key_metrics", "backtest_summary"):
                    value = payload.get(key)
                    if value not in (None, "", [], {}):
                        if key == "score":
                            candidate["quick_score"] = value
                            candidate["score"] = value
                        else:
                            candidate[key] = _jsonable(value)
            else:
                for key in ("grade", "single_factor_decision", "quality_decision", "reject_reasons", "screening_hint", "gate_result", "key_metrics", "backtest_summary", "deep_score", "deep_action", "deep_reason", "rolling_validation", "anti_overfit", "adversarial_validation", "novelty_score", "novelty_guard", "combined_guard", "matched_existing_factor", "matched_existing_factor_id", "matched_existing_factor_name", "matched_existing_expression_summary", "matched_information_cluster_id", "matched_reference_source", "st_exposure_guard"):
                    value = payload.get(key)
                    if value not in (None, "", [], {}):
                        candidate[key] = _jsonable(value)
                if tool in {"run_backtest", "backtest"} or task_type == "backtest":
                    if payload.get("backtest_summary") not in (None, "", [], {}):
                        candidate["backtest_summary"] = _jsonable(payload.get("backtest_summary"))
                    elif payload not in ({}, None, ""):
                        candidate.setdefault("backtest_summary", _jsonable(payload))
                elif tool in {"run_anti_overfit", "anti_overfit"} or task_type == "anti_overfit":
                    if payload not in ({}, None, ""):
                        candidate["anti_overfit"] = _jsonable(payload)
                        if payload.get("score") is not None:
                            candidate["anti_overfit_score"] = payload.get("score")
                elif tool in {"run_rolling_validation", "rolling_validation"} or task_type == "rolling_validation":
                    if payload not in ({}, None, ""):
                        candidate["rolling_validation"] = _jsonable(payload)
                        if payload.get("score") is not None:
                            candidate["rolling_score"] = payload.get("score")
                elif tool in {"run_adversarial_validation", "adversarial_validation"} or task_type == "adversarial_validation":
                    if payload not in ({}, None, ""):
                        candidate["adversarial_validation"] = _jsonable(payload)
                        if payload.get("score") is not None:
                            candidate["adversarial_score"] = payload.get("score")

    running_score_candidates = [
        candidate
        for candidate in candidates.values()
        if candidate.get("stage") == "quick_score_running"
        and candidate.get("status") == "running"
        and candidate.get("quick_score") is None
        and not candidate.get("tool_evidence")
    ]
    if len(running_score_candidates) > 1:
        latest_running = max(
            running_score_candidates,
            key=lambda item: (str(item.get("source_step_ts") or ""), str(item.get("candidate_id") or "")),
        )
        latest_running_key = latest_running.get("process_key")
        for candidate in running_score_candidates:
            if candidate.get("process_key") == latest_running_key:
                continue
            candidate["stage"] = "quick_score_pending_result"
            candidate["screening_stage"] = "quick_score_pending_result"
            candidate["status"] = "pending_result"
            candidate["quality_decision"] = "pending_result"
            candidate["display_status_reason"] = "non_latest_score_progress_without_task_link"

    for candidate in candidates.values():
        refresh_display_state(candidate)
        if not candidate.get("expression"):
            candidate["evidence_errors"].append({"code": "missing_expression", "message": "candidate identity is incomplete"})
        if not candidate.get("run_id") or not candidate.get("round_id") or not candidate.get("candidate_id"):
            candidate["evidence_errors"].append({"code": "missing_identity", "message": "candidate has no full run/round/candidate identity"})
        if candidate.get("evidence_errors") and candidate_has_any_research_step_evidence(candidate):
            retained_errors = []
            for error in candidate.get("evidence_errors") or []:
                if not isinstance(error, dict):
                    retained_errors.append(error)
                    continue
                if error.get("code") in {"missing_task_link", "task_not_found"} and candidate_has_research_step_evidence(candidate, str(error.get("tool") or "")):
                    add_detached_tool_evidence(
                        candidate,
                        tool=str(error.get("tool") or ""),
                        stage_id=error.get("stage_id"),
                        task_id=str(error.get("task_id") or ""),
                        message="tool evidence is summarized in research_steps; task-store link issue was kept as a non-blocking attachment note",
                    )
                    continue
                retained_errors.append(error)
            candidate["evidence_errors"] = retained_errors

    ordered = sorted(candidates.values(), key=lambda item: (str(item.get("source_step_ts") or ""), str(item.get("candidate_id") or "")), reverse=True)[:limit]
    for candidate in ordered:
        candidate["stage_history"] = candidate.get("stage_history", [])[-12:]
    if same_run and not ordered:
        errors.append(
            {
                "code": "missing_candidate_projection",
                "message": "research steps exist for this run but none contain candidate_lanes, candidate_decisions, or linked candidate evidence",
            }
        )
    row_errors = [
        {"code": "candidate_evidence_error", "candidate_id": candidate.get("candidate_id"), "errors": candidate.get("evidence_errors")}
        for candidate in ordered
        if candidate.get("evidence_errors")
    ]
    return {
        "schema_version": "current_candidate_board_v1",
        "ok": not errors and not row_errors,
        "source": "research_steps_current_run",
        "tool_evidence_source": "quantgpt_tasks_by_explicit_task_id",
        "run_id": run_id,
        "round_id": current_round_id,
        "current_round_id": current_round_id,
        "round_ids": round_ids,
        "round_count": len(round_ids),
        "stage": latest.get("stage"),
        "updated_at": latest.get("ts") or latest.get("created_at"),
        "errors": _jsonable(errors + row_errors),
        "candidates": _jsonable(ordered),
    }


def _live_orchestrator_tool_workers() -> list[dict]:
    """Return live isolated ORCH tool workers launched outside QuantGPT HTTP."""

    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,rss=,etime=,cmd="],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []

    workers_by_payload: dict[str, dict] = {}
    for line in (completed.stdout or "").splitlines():
        if "tool_runner.py" not in line or "fxalpha_" not in line:
            continue
        if "fxalpha_score_factor_" not in line and "fxalpha_deep_validation_" not in line:
            continue
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, rss_kb, elapsed, cmd = parts
        payload_match = re.search(r"(\S*/fxalpha_(?:score_factor|deep_validation)_\S+/tool_payload\.json)", cmd)
        if not payload_match:
            continue
        payload_path = Path(payload_match.group(1))
        worker_key = str(payload_path)
        payload: dict = {}
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            payload = {}
        candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
        tool = str(payload.get("tool") or "")
        try:
            rss_mb = round(int(rss_kb) / 1024.0, 1)
        except Exception:
            rss_mb = None
        worker = {
            "pid": int(pid) if str(pid).isdigit() else pid,
            "tool": tool or ("deep_validation" if "fxalpha_deep_validation_" in worker_key else "score_factor"),
            "candidate_id": candidate.get("candidate_id"),
            "expression": candidate.get("expression"),
            "elapsed": elapsed,
            "rss_mb": rss_mb,
            "payload_path": str(payload_path),
            "execution": "systemd_or_subprocess_worker",
            "process_kind": "systemd_wrapper" if "systemd-run" in cmd else "python_worker",
        }
        previous = workers_by_payload.get(worker_key)
        if not previous or previous.get("process_kind") == "systemd_wrapper" or worker.get("process_kind") == "python_worker":
            workers_by_payload[worker_key] = worker
    return list(workers_by_payload.values())


def _reconcile_quantgpt_summary_with_readiness(summary: dict, readiness: dict) -> dict:
    """Prefer live QuantGPT health over stale DB task statuses for active state."""

    clean = dict(summary or {})
    health = ((readiness or {}).get("quantgpt_api") or {}).get("health") or {}
    active_tasks = ((readiness or {}).get("quantgpt_api") or {}).get("active_tasks")
    if active_tasks is None and isinstance(health, dict):
        active_tasks = health.get("active_tasks")
    if active_tasks is None:
        return clean
    try:
        active_count = int(active_tasks or 0)
    except Exception:
        return clean
    clean["live_active_tasks"] = active_count
    if active_count <= 0 and int(clean.get("running_count") or 0) > 0:
        live_workers = _live_orchestrator_tool_workers()
        running_tasks = clean.get("running_tasks") or []
        if live_workers:
            live_by_expr = {
                str(worker.get("expression") or ""): worker
                for worker in live_workers
                if worker.get("expression")
            }
            matched: list[dict] = []
            unmatched: list[dict] = []
            for task in running_tasks:
                expr = str((task or {}).get("expression") or "")
                worker = live_by_expr.get(expr)
                if worker:
                    merged = dict(task)
                    merged["orchestrator_worker_active"] = True
                    merged["worker"] = _jsonable(worker)
                    matched.append(merged)
                else:
                    unmatched.append(task)
            if matched:
                clean["running_count"] = len(matched)
                clean["running_tasks"] = _jsonable(matched[:8])
                clean["live_orchestrator_workers"] = _jsonable(live_workers[:8])
                clean["live_active_tasks"] = len(matched)
                clean["running_reconciled"] = "kept_by_live_orchestrator_tool_worker"
                if unmatched:
                    clean["stale_running_count"] = len(unmatched)
                    clean["stale_running_tasks"] = _jsonable(unmatched[:8])
                return clean
        clean["stale_running_count"] = clean.get("running_count")
        clean["stale_running_tasks"] = running_tasks
        clean["latest_stale_task"] = _jsonable((running_tasks or [{}])[0] if running_tasks else {})
        clean["running_count"] = 0
        clean["running_tasks"] = []
        latest_non_running = clean.get("latest_non_running_task") or {}
        if latest_non_running:
            clean["latest_task"] = latest_non_running
        by_type = clean.get("by_type")
        if isinstance(by_type, dict):
            scrubbed: dict[str, dict[str, int]] = {}
            stale_by_type: dict[str, int] = {}
            for task_type, counts in by_type.items():
                if not isinstance(counts, dict):
                    continue
                filtered = {
                    str(status): int(count)
                    for status, count in counts.items()
                    if str(status) != "running" and int(count or 0) > 0
                }
                running_here = int((counts or {}).get("running") or 0)
                if running_here > 0:
                    stale_by_type[str(task_type)] = running_here
                if filtered:
                    scrubbed[str(task_type)] = filtered
            clean["by_type"] = scrubbed
            if stale_by_type:
                clean["stale_running_by_type"] = stale_by_type
        clean["running_reconciled"] = "cleared_by_quantgpt_health_active_tasks_0"
    return clean


def _research_step_extra(step: dict) -> dict:
    extra = step.get("extra")
    if isinstance(extra, dict):
        return extra
    preview = step.get("extra_preview")
    if isinstance(preview, str) and preview.strip().startswith("{"):
        try:
            parsed = json.loads(preview)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _merge_candidates(primary: list[dict], secondary: list[dict], *, limit: int = 24) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for candidate in list(primary or []) + list(secondary or []):
        expression = str(candidate.get("expression") or "").strip()
        key = f"{expression}::{candidate.get('screening_stage') or candidate.get('source') or candidate.get('source_tool') or ''}"
        if not expression:
            key = f"{candidate.get('name')}::{candidate.get('tool_ts') or candidate.get('ts')}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(candidate)
        if len(merged) >= limit:
            break
    return merged


def _augment_digest_with_quantgpt_tasks(
    digest: dict,
    *,
    quantgpt_candidates: list[dict],
    quantgpt_summary: dict,
    recent_notes: list[dict],
    factor_library: dict,
) -> dict:
    digest = dict(digest or {})
    if quantgpt_candidates:
        digest["recent_candidates"] = _merge_candidates(quantgpt_candidates, digest.get("recent_candidates") or [])
    digest["quantgpt_task_summary"] = quantgpt_summary

    running = quantgpt_summary.get("running_tasks") or []
    latest = quantgpt_summary.get("latest_task") or {}
    if running:
        task = running[0]
        digest["current_phase"] = {
            "score": "Quick Score",
            "backtest": "Deep Validation",
            "anti_overfit": "Anti-overfit",
            "rolling_validation": "Rolling Validation",
        }.get(str(task.get("task_type")), "QuantGPT MCP")
        digest["current_action"] = f"QuantGPT 正在运行 {task.get('task_type')}: {task.get('expression')}"
        digest["updated_at"] = task.get("created_at") or digest.get("updated_at")
    elif quantgpt_candidates and str(digest.get("current_phase") or "").lower() in {
        "",
        "idle",
        "codex_mcp_supervision_required",
        "running",
    }:
        top = quantgpt_candidates[0]
        digest["current_phase"] = "MCP Task Store"
        digest["current_action"] = f"最近完成 {top.get('screening_stage')}: {top.get('expression')}"
        digest["updated_at"] = top.get("tool_ts") or top.get("ts") or digest.get("updated_at")
    elif latest and not digest.get("current_action"):
        digest["current_action"] = f"最近 QuantGPT 任务：{latest.get('task_type')} / {latest.get('status')}"

    tool_progress = dict(digest.get("tool_progress") or {})
    tools = dict(tool_progress.get("tools") or {})
    for task_type, statuses in (quantgpt_summary.get("by_type") or {}).items():
        tool_name = {
            "score": "score_factor",
            "backtest": "run_backtest",
            "anti_overfit": "run_anti_overfit",
            "adversarial_validation": "run_adversarial_validation",
            "rolling_validation": "run_rolling_validation",
        }.get(task_type, task_type)
        item = tools.setdefault(tool_name, {"started": 0, "completed": 0, "blocked": 0, "failed": 0})
        item["started"] = max(item.get("started", 0), sum(statuses.values()))
        item["completed"] = max(item.get("completed", 0), statuses.get("completed", 0))
        item["failed"] = max(item.get("failed", 0), statuses.get("failed", 0))
    if running:
        tool_progress["active_tool"] = {
            "score": "score_factor",
            "backtest": "run_backtest",
            "anti_overfit": "run_anti_overfit",
            "adversarial_validation": "run_adversarial_validation",
            "rolling_validation": "run_rolling_validation",
        }.get(str(running[0].get("task_type")), running[0].get("task_type"))
    tool_progress["tools"] = tools
    digest["tool_progress"] = tool_progress

    items = factor_library.get("items") or []
    if items:
        digest["latest_imported_factor"] = items[0]
    return digest


def _recent_score_candidates(events: list[dict], *, limit: int = 24) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()

    def add_candidate(candidate: dict, event: dict, source: str) -> None:
        if len(candidates) >= limit:
            return
        expression = str((candidate or {}).get("expression") or "")
        key = f"{source}:{expression or (candidate or {}).get('name') or event.get('ts')}"
        if not expression or key in seen:
            return
        seen.add(key)
        digest = _candidate_digest(candidate, event, source=source)
        if digest:
            candidates.append(digest)

    for event in reversed(events or []):
        if event.get("event") != "tool_call_completed":
            continue
        tool = event.get("tool")
        payload = _tool_payload_from_event(event)
        if tool == "score_factor":
            add_candidate(payload, event, "score_factor")
        elif tool == "run_backtest":
            add_candidate(payload.get("candidate") or payload, event, "run_backtest")
        elif tool == "fxalpha_novelty_check":
            for item in (payload.get("keepers") or []) + (payload.get("dropped") or []):
                add_candidate(item, event, "fxalpha_novelty_check")
        elif tool == "fxalpha_quality_gate":
            for item in (payload.get("adopted") or []) + (payload.get("screened_out") or []) + (payload.get("rejected") or []):
                add_candidate(item, event, "fxalpha_quality_gate")
        else:
            continue
        if len(candidates) >= limit:
            break
    return candidates


def _latest_event_by(events: list[dict], *, event_name: str | None = None, tool: str | None = None) -> dict | None:
    for event in reversed(events or []):
        if event_name and event.get("event") != event_name:
            continue
        if tool and event.get("tool") != tool:
            continue
        return event
    return None


def _latest_quality_gate_digest(events: list[dict]) -> dict:
    event = _latest_event_by(events, event_name="tool_call_completed", tool="fxalpha_quality_gate")
    payload = _tool_payload_from_event(event)
    if not payload:
        return {}
    adopted = payload.get("adopted") or []
    screened_out = payload.get("screened_out") or []
    rejected = payload.get("rejected") or []
    return {
        "ts": event.get("ts") if event else None,
        "run_id": event.get("run_id") if event else None,
        "round_id": event.get("round_id") if event else None,
        "stage_id": event.get("stage_id") if event else None,
        "ok": payload.get("ok", True),
        "counts": {
            "adopted": len(adopted),
            "screened_out": len(screened_out),
            "rejected": len(rejected),
        },
        "adopted": [_candidate_digest(item, event, source="quality_gate_adopted") for item in adopted[:8]],
        "screened_out": [_candidate_digest(item, event, source="quality_gate_screened") for item in screened_out[:8]],
        "rejected": [_candidate_digest(item, event, source="quality_gate_rejected") for item in rejected[:8]],
        "feedback": payload.get("feedback") or payload.get("summary") or payload.get("reason") or "",
    }


def _latest_llm_step(events: list[dict]) -> dict:
    event = _latest_event_by(events, event_name="agent_message")
    if not event:
        return {}
    content = str(event.get("content") or "")
    return {
        "ts": event.get("ts"),
        "step": event.get("step"),
        "content": content,
        "summary": content[:600],
    }


def _tool_progress(events: list[dict]) -> dict:
    progress: dict[str, dict] = {}
    active_tool = None
    for event in events or []:
        tool = event.get("tool")
        if not tool:
            continue
        item = progress.setdefault(tool, {"started": 0, "completed": 0, "blocked": 0, "failed": 0})
        if event.get("event") == "tool_call_started":
            item["started"] += 1
            active_tool = tool
        elif event.get("event") == "tool_call_completed":
            item["completed"] += 1
            if active_tool == tool:
                active_tool = None
        elif event.get("event") == "tool_call_blocked":
            item["blocked"] += 1
        if event.get("error") or event.get("ok") is False:
            item["failed"] += 1
    return {"active_tool": active_tool, "tools": progress}


def _infer_current_phase(job: dict | None, events: list[dict]) -> str:
    latest = (job or {}).get("latest_event") or (events[-1] if events else {})
    event_name = latest.get("event")
    tool = latest.get("tool")
    status = str((job or {}).get("status") or "").lower()
    stage = str((job or {}).get("stage") or "").lower()
    if "failed" in status or "blocker" in stage or event_name in {"automation_failed", "tool_blocker"}:
        return "Blocked"
    if (job or {}).get("status") == "completed" or event_name in {"job_finished", "session_completed"}:
        return "Done"
    if event_name == "tool_call_started":
        return {
            "fxalpha_context": "Context",
            "list_operators": "Context",
            "list_universes": "Context",
            "validate_expression": "Validate",
            "score_factor": "Scoring",
            "run_backtest": "Deep Validation",
            "run_rolling_validation": "Rolling Validation",
            "fxalpha_novelty_check": "Novelty & ST",
            "fxalpha_quality_gate": "Gate",
            "fxalpha_record_research_step": "Research Step",
            "fxalpha_import_factors": "Import",
        }.get(tool, "Tool")
    if event_name in {
        "analysis_fact_pack_built",
        "four_step_fact_collection",
        "four_step_independent_judgment",
        "four_step_cross_review",
        "four_step_consensus",
    }:
        return "Four-step"
    if event_name == "tool_call_completed":
        if tool == "score_factor":
            return "Scoring"
        if tool == "run_backtest":
            return "Deep Validation"
        if tool == "run_rolling_validation":
            return "Rolling Validation"
        if tool == "fxalpha_novelty_check":
            return "Novelty & ST"
        if tool == "fxalpha_quality_gate":
            return "Gate"
        if tool == "fxalpha_record_research_step":
            return "Research Step"
        if tool == "fxalpha_import_factors":
            return "Import"
    if event_name == "agent_message":
        return "Planning"
    if event_name == "agent_prompt_built":
        return "Context"
    return "Idle" if not job else str((job or {}).get("stage") or "Running")


def _current_action(job: dict | None, events: list[dict]) -> str:
    latest = (job or {}).get("latest_event") or (events[-1] if events else {})
    event_name = latest.get("event")
    tool = latest.get("tool")
    status = str((job or {}).get("status") or "").lower()
    stage = str((job or {}).get("stage") or "").lower()
    if "failed" in status or "blocker" in stage or event_name in {"automation_failed", "tool_blocker"}:
        return str(latest.get("message") or latest.get("reason") or "当前研究被阻断，请查看 blocker 原因")
    if event_name == "tool_call_started" and tool:
        args = latest.get("arguments") or {}
        expr = args.get("expression")
        if expr:
            return f"正在调用 {tool}: {expr}"
        return f"正在调用 {tool}"
    if event_name == "tool_call_completed" and tool:
        return f"{tool} 已返回，等待 Agent 判断下一步"
    if event_name == "agent_message":
        return str(latest.get("content") or "LLM 正在输出研究判断")[:240]
    if event_name == "four_step_consensus":
        consensus = latest.get("consensus") or {}
        return f"四步分析共识：{consensus.get('action', 'next')}"
    if (job or {}).get("status") == "completed":
        return "最近研究已结束，显示最后一次运行摘要"
    return str(event_name or "等待研究任务启动")


def _recent_llm_io_digest(events: list[dict], *, limit: int = 16) -> list[dict]:
    interesting = {
        "agent_prompt_built",
        "agent_message",
        "analysis_fact_pack_built",
        "four_step_fact_collection",
        "four_step_independent_judgment",
        "four_step_cross_review",
        "four_step_consensus",
        "four_step_protocol_blocked",
        "premature_finish_blocked",
        "tool_call_completed",
        "tool_call_blocked",
    }
    items: list[dict] = []
    for event in reversed(events or []):
        if event.get("event") not in interesting:
            continue
        if event.get("event") == "tool_call_completed" and event.get("tool") not in {
            "score_factor",
            "run_backtest",
            "run_rolling_validation",
            "fxalpha_quality_gate",
            "fxalpha_novelty_check",
            "fxalpha_import_factors",
            "fxalpha_record_research_step",
        }:
            continue
        payload = _tool_payload_from_event(event)
        summary = event.get("content") or event.get("reason") or payload.get("reason") or payload.get("feedback") or ""
        if not summary and payload:
            summary = json.dumps(_jsonable(payload), ensure_ascii=False)[:700]
        items.append(
            {
                "ts": event.get("ts"),
                "event": event.get("event"),
                "tool": event.get("tool"),
                "step": event.get("step"),
                "summary": str(summary)[:900],
                "raw": _jsonable(event),
            }
        )
        if len(items) >= limit:
            break
    return items


def _build_live_research_digest(
    active_job: dict | None,
    recent_jobs: list[dict],
    latest_research: dict,
    registry_summary: dict,
) -> dict:
    fallback_job = active_job or (recent_jobs[0] if recent_jobs else None)
    events = list((fallback_job or {}).get("events", []) or [])
    four_step = _latest_four_step_blocks(fallback_job, recent_jobs)
    score_candidates = _recent_score_candidates(events)
    quality_gate = _latest_quality_gate_digest(events)
    if not score_candidates and quality_gate:
        score_candidates = [
            *(quality_gate.get("adopted") or []),
            *(quality_gate.get("screened_out") or []),
            *(quality_gate.get("rejected") or []),
        ][:24]
    latest_summary = latest_research.get("summary") or latest_research.get("result", {}).get("summary", {}) or {}
    target = (fallback_job or {}).get("inputs", {}).get("target_adopted") or latest_summary.get("target_adopted")
    active_count = registry_summary.get("active") or 0
    latest_block = _latest_event_by(events, event_name="four_step_protocol_blocked") or _latest_event_by(
        events,
        event_name="tool_call_blocked",
    )
    active_task_count = len([job for job in recent_jobs if _job_is_active(job)])
    if fallback_job and _job_is_active(fallback_job) and active_task_count == 0:
        active_task_count = 1
    return {
        "run_id": (fallback_job or {}).get("run_id"),
        "session_id": ((fallback_job or {}).get("latest_event") or {}).get("session_id"),
        "status": (fallback_job or {}).get("status") or "idle",
        "stage": (fallback_job or {}).get("stage"),
        "current_phase": _infer_current_phase(fallback_job, events),
        "current_action": _current_action(fallback_job, events),
        "event_count": (fallback_job or {}).get("event_count") or len(events),
        "active_task_count": active_task_count,
        "updated_at": ((fallback_job or {}).get("latest_event") or {}).get("ts") or (fallback_job or {}).get("started_at"),
        "target_adopted": target,
        "active_factor_count": active_count,
        "target_progress": {
            "active": active_count,
            "target": target,
            "new_imported": latest_summary.get("imported") or (fallback_job or {}).get("summary", {}).get("adopted_count", 0),
        },
        "latest_llm_step": _latest_llm_step(events),
        "latest_hypotheses": (
            four_step.get("consensus", {}).get("next_hypotheses")
            or four_step.get("consensus", {}).get("next_actions")
            or []
        ),
        "latest_four_step": four_step,
        "recent_candidates": score_candidates,
        "latest_quality_gate": quality_gate,
        "tool_progress": _tool_progress(events),
        "blocking_reason": (latest_block or {}).get("reason") or (latest_block or {}).get("message"),
        "recent_llm_io": _recent_llm_io_digest(events),
    }


def _probe_quantgpt_api(qgpt_url: str) -> dict:
    last_error = ""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for path in ("/api/v1/health", "/openapi.json"):
        url = f"{qgpt_url.rstrip('/')}{path}"
        try:
            with opener.open(url, timeout=5) as resp:
                result = {"reachable": True, "url": url, "status_code": resp.status}
                if path == "/api/v1/health":
                    try:
                        body = resp.read(2000).decode("utf-8", errors="ignore")
                        payload = json.loads(body) if body else {}
                        if isinstance(payload, dict):
                            result["health"] = _jsonable(payload)
                            if payload.get("active_tasks") is not None:
                                result["active_tasks"] = int(payload.get("active_tasks") or 0)
                    except Exception:
                        pass
                return result
        except urllib.error.HTTPError as exc:
            last_error = str(exc)
            return {"reachable": False, "url": url, "status_code": exc.code, "error": last_error}
        except Exception as exc:
            last_error = str(exc)
    return {"reachable": False, "url": qgpt_url, "error": last_error}


def _quantgpt_self_heal_snapshot() -> dict:
    with _QUANTGPT_SELF_HEAL_LOCK:
        return dict(_QUANTGPT_SELF_HEAL_STATE)


def _quantgpt_self_heal_allowed(now_ts: float) -> bool:
    with _QUANTGPT_SELF_HEAL_LOCK:
        last_attempt_ts = _QUANTGPT_SELF_HEAL_STATE.get("last_attempt_ts") or ""
    if not last_attempt_ts:
        return True
    try:
        last_attempt = datetime.fromisoformat(str(last_attempt_ts))
    except Exception:
        return True
    return (now_ts - last_attempt.timestamp()) >= _QUANTGPT_SELF_HEAL_COOLDOWN_SECONDS


def _restart_quantgpt_http_service() -> tuple[bool, str]:
    restart_script = QUANTGPT_CODE_ROOT / "restart.sh"
    if not restart_script.exists():
        return False, f"restart_script_missing:{restart_script}"
    cmd = (
        f"cd {str(QUANTGPT_CODE_ROOT)!r} && "
        "QUANTGPT_SKIP_FRONTEND_BUILD=1 ./restart.sh"
    )
    try:
        completed = subprocess.run(
            ["bash", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except Exception as exc:
        return False, f"restart_exec_failed:{exc}"
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or f"exit_{completed.returncode}"
        return False, f"restart_nonzero:{detail[:260]}"
    return True, (completed.stdout or "restart_ok").strip()[:260]


def _ensure_quantgpt_api_reachable(qgpt_url: str, *, allow_restart: bool = True) -> dict:
    probe = _probe_quantgpt_api(qgpt_url)
    if probe.get("reachable"):
        with _QUANTGPT_SELF_HEAL_LOCK:
            _QUANTGPT_SELF_HEAL_STATE["last_success_ts"] = _now_iso()
            if not _QUANTGPT_SELF_HEAL_STATE.get("last_result"):
                _QUANTGPT_SELF_HEAL_STATE["last_result"] = "healthy"
        probe["self_heal"] = _quantgpt_self_heal_snapshot()
        return probe

    if not allow_restart:
        probe["self_heal"] = _quantgpt_self_heal_snapshot()
        return probe

    now = time.time()
    if not _quantgpt_self_heal_allowed(now):
        probe["self_heal"] = {
            **_quantgpt_self_heal_snapshot(),
            "cooldown_active": True,
            "cooldown_seconds": _QUANTGPT_SELF_HEAL_COOLDOWN_SECONDS,
        }
        return probe

    with _QUANTGPT_SELF_HEAL_LOCK:
        last_attempt_ts = _QUANTGPT_SELF_HEAL_STATE.get("last_attempt_ts") or ""
        if last_attempt_ts:
            try:
                last_attempt = datetime.fromisoformat(str(last_attempt_ts))
            except Exception:
                last_attempt = None
            if last_attempt and (now - last_attempt.timestamp()) < _QUANTGPT_SELF_HEAL_COOLDOWN_SECONDS:
                probe["self_heal"] = {
                    **dict(_QUANTGPT_SELF_HEAL_STATE),
                    "cooldown_active": True,
                    "cooldown_seconds": _QUANTGPT_SELF_HEAL_COOLDOWN_SECONDS,
                }
                return probe
        _QUANTGPT_SELF_HEAL_STATE["last_attempt_ts"] = _now_iso()
        _QUANTGPT_SELF_HEAL_STATE["last_result"] = "restart_attempted"
        _QUANTGPT_SELF_HEAL_STATE["last_error"] = str(probe.get("error") or "")

        restarted, restart_detail = _restart_quantgpt_http_service()
        if not restarted:
            _QUANTGPT_SELF_HEAL_STATE["last_result"] = "restart_failed"
            _QUANTGPT_SELF_HEAL_STATE["last_error"] = restart_detail
            probe["self_heal"] = dict(_QUANTGPT_SELF_HEAL_STATE)
            return probe

        reprobe = probe
        for _ in range(_QUANTGPT_SELF_HEAL_MAX_RETRIES):
            time.sleep(_QUANTGPT_SELF_HEAL_RETRY_SLEEP_SECONDS)
            reprobe = _probe_quantgpt_api(qgpt_url)
            if reprobe.get("reachable"):
                break

        if reprobe.get("reachable"):
            _QUANTGPT_SELF_HEAL_STATE["last_success_ts"] = _now_iso()
            _QUANTGPT_SELF_HEAL_STATE["last_result"] = "restart_recovered"
            _QUANTGPT_SELF_HEAL_STATE["last_error"] = ""
        else:
            _QUANTGPT_SELF_HEAL_STATE["last_result"] = "restart_no_recovery"
            _QUANTGPT_SELF_HEAL_STATE["last_error"] = str(reprobe.get("error") or restart_detail)

        reprobe["self_heal"] = dict(_QUANTGPT_SELF_HEAL_STATE)
        reprobe["self_heal"]["restart_detail"] = restart_detail
        return reprobe


def _load_llm_runtime_hint() -> dict:
    return {
        "source": str(Path.home() / "FXalpha" / "config.yaml"),
        "provider": LLM_PROVIDER,
        "model": LLM_MODEL,
        "cross_review_model": LLM_CROSS_REVIEW_MODEL,
        "base_url": LLM_BASE_URL,
        "api_key_present": bool(LLM_API_KEY),
    }


def _research_step_dedupe_key(item: dict) -> str:
    stage_id = str(item.get("stage_id") or "")
    progress_suffix = ""
    refs = item.get("evidence_refs") if isinstance(item.get("evidence_refs"), list) else []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        candidate_index = ref.get("candidate_index")
        candidate_id = ref.get("candidate_id")
        if candidate_index is not None or candidate_id:
            progress_suffix = json.dumps(
                {
                    "candidate_index": candidate_index,
                    "candidate_id": candidate_id,
                    "tool": ref.get("tool"),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            break
    if stage_id:
        return f"{stage_id}|{progress_suffix}" if progress_suffix else stage_id
    return json.dumps(
        {
            "ts": item.get("ts"),
            "run_id": item.get("run_id"),
            "round_id": item.get("round_id"),
            "stage": item.get("stage"),
            "stage_seq": item.get("stage_seq"),
            "progress": progress_suffix,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _read_recent_research_steps(limit: int = 20, *, run_id: str | None = None) -> list[dict]:
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_RESEARCH_STEPS_FILE,
        history_dir=FACTOR_RESEARCH_STEPS_HISTORY_DIR,
        run_id=str(run_id or "").strip() or None,
        limit=limit,
        max_lines_per_file=max(1000, int(limit or 1)),
        max_bytes_per_file=FACTOR_RESEARCH_STEPS_MAX_BYTES,
    )
    steps: list[dict] = []
    seen: set[str] = set()
    for item in records:
        dedupe_key = _research_step_dedupe_key(item)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        steps.append(item)
    return steps


def _orchestrator_run_candidate_trajectory(
    *,
    run_id: str,
    stage: str,
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Recover the real cross-candidate trajectory from completed run evidence.

    QuantGPT's adaptive evolution operates on every previously evaluated
    candidate, not only the candidates in the current batch.  Research steps
    already persist the required score/deep evidence, so the Orchestrator can
    reuse that journal without creating another state store.
    """

    clean_stage = str(stage or "").strip()
    if clean_stage not in {"score_review", "deep_validation_review"}:
        return []
    steps = sorted(
        _read_recent_research_steps(limit=512, run_id=run_id),
        key=lambda item: (
            str(item.get("ts") or ""),
            str(item.get("round_id") or ""),
            int(item.get("stage_seq") or 0),
        ),
    )
    records: list[dict[str, Any]] = []
    record_index: dict[tuple[str, str, str], int] = {}

    def expression_key(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or "")).lower()

    def add(item: dict, *, step: dict, score: Any) -> None:
        expression = str(item.get("expression") or "")
        candidate_id = str(item.get("candidate_id") or "")
        round_id = str(step.get("round_id") or item.get("round_id") or "")
        if not expression or score in (None, ""):
            return
        try:
            score_value = float(score)
        except (TypeError, ValueError):
            return
        record = {
            "run_id": run_id,
            "round_id": round_id,
            "candidate_id": candidate_id,
            "trajectory_id": item.get("trajectory_id"),
            "parent_candidate_id": item.get("parent_candidate_id"),
            "mutation_summary": item.get("mutation_summary"),
            "matched_region_uid": (
                item.get("matched_region_uid")
                or ((item.get("novelty_guard") or {}).get("matched_region_uid") if isinstance(item.get("novelty_guard"), dict) else None)
            ),
            "expression": expression,
            "score": score_value,
            "grade": item.get("grade"),
            "rolling_score": item.get("rolling_score"),
            "source_stage": clean_stage,
            "ts": step.get("ts"),
        }
        if clean_stage == "deep_validation_review":
            action = str(item.get("action") or "").strip()
            record["downstream_action"] = action
            record["parent_eligible"] = action in {
                "submit_quality_gate",
                "targeted_mutation",
                "complete_deep_evidence",
            }
        key = (round_id, candidate_id, expression)
        if key in record_index:
            records[record_index[key]] = record
        else:
            record_index[key] = len(records)
            records.append(record)

    for step in steps:
        if str(step.get("stage") or "") != clean_stage:
            continue
        refs = step.get("evidence_refs") if isinstance(step.get("evidence_refs"), list) else []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            if clean_stage == "score_review" and str(ref.get("type") or "") == "candidate_lanes":
                for item in ref.get("items") or []:
                    if isinstance(item, dict):
                        add(item, step=step, score=item.get("score", item.get("quick_score")))
            elif clean_stage == "deep_validation_review" and str(ref.get("type") or "") == "advice_summary":
                for item in ref.get("candidate_lane_decisions") or []:
                    if isinstance(item, dict):
                        add(item, step=step, score=item.get("deep_score"))
    if clean_stage == "score_review":
        novelty_by_key = {
            (
                str(item.get("round_id") or ""),
                str(item.get("candidate_id") or ""),
                expression_key(item.get("expression")),
            ): str(item.get("action") or "")
            for item in _orchestrator_run_novelty_history(
                run_id=run_id,
                limit=max(80, int(limit or 1) * 2),
            )
        }
        deep_by_key = {
            (
                str(item.get("round_id") or ""),
                str(item.get("candidate_id") or ""),
                expression_key(item.get("expression")),
            ): item
            for item in _orchestrator_run_candidate_trajectory(
                run_id=run_id,
                stage="deep_validation_review",
                limit=512,
            )
        }
        for record in records:
            key = (
                str(record.get("round_id") or ""),
                str(record.get("candidate_id") or ""),
                expression_key(record.get("expression")),
            )
            novelty_action = novelty_by_key.get(key, "")
            deep_outcome = deep_by_key.get(key)
            record["downstream_action"] = novelty_action or "not_novelty_approved"
            if deep_outcome:
                record["deep_score"] = deep_outcome.get("score")
                record["rolling_score"] = deep_outcome.get("rolling_score")
                record["deep_action"] = deep_outcome.get("downstream_action")
                record["deep_parent_eligible"] = deep_outcome.get("parent_eligible")
                # The Quick trajectory is the generic parent pool.  Once a
                # Deep terminal decision exists, only a gate-ready candidate
                # remains eligible here.  Deep-specific targeted mutation
                # continues to use the separate Deep trajectory below.
                record["parent_eligible"] = (
                    deep_outcome.get("downstream_action") == "submit_quality_gate"
                )
            else:
                record["parent_eligible"] = novelty_action == "advance_to_deep_validation"
    return records[-max(1, int(limit or 1)) :]


def _orchestrator_run_novelty_history(
    *,
    run_id: str,
    limit: int = 80,
) -> list[dict[str, Any]]:
    steps = sorted(
        _read_recent_research_steps(limit=512, run_id=run_id),
        key=lambda item: (
            str(item.get("ts") or ""),
            str(item.get("round_id") or ""),
            int(item.get("stage_seq") or 0),
        ),
    )
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for step in steps:
        if str(step.get("stage") or "") != "novelty_review":
            continue
        refs = step.get("evidence_refs") if isinstance(step.get("evidence_refs"), list) else []
        for ref in refs:
            if not isinstance(ref, dict) or str(ref.get("type") or "") != "advice_summary":
                continue
            for item in ref.get("candidate_lane_decisions") or []:
                if not isinstance(item, dict):
                    continue
                expression = str(item.get("expression") or "")
                candidate_id = str(item.get("candidate_id") or "")
                round_id = str(step.get("round_id") or "")
                key = (round_id, candidate_id, expression)
                if key in seen:
                    continue
                seen.add(key)
                records.append(
                    {
                        "run_id": run_id,
                        "round_id": round_id,
                        "candidate_id": candidate_id,
                        "expression": expression,
                        "action": item.get("action"),
                        "reason": item.get("reason"),
                        "matched_region_uid": item.get("matched_region_uid"),
                        "matched_information_cluster_id": item.get("matched_information_cluster_id"),
                        "matched_existing_factor_id": item.get("matched_existing_factor_id"),
                        "ts": step.get("ts"),
                    }
                )
    return records[-max(1, int(limit or 1)) :]


def _read_current_research_steps(*, run_id: str = "", limit: int = 1200) -> list[dict]:
    """Read the bounded live cache only; GUI polling must not scan all history."""
    clean_run_id = str(run_id or "").strip()
    lines = _reverse_jsonl_lines(FACTOR_RESEARCH_STEPS_FILE)
    rows: list[dict] = []
    seen: set[str] = set()
    for line in lines:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        if clean_run_id and str(item.get("run_id") or "") != clean_run_id:
            continue
        key = _research_step_dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        rows.append(item)
        if len(rows) >= max(1, int(limit or 1)):
            break
    return rows


def _count_research_step_history_lines() -> int:
    total = 0
    history_files = sorted(FACTOR_RESEARCH_STEPS_HISTORY_DIR.glob("*.jsonl"))
    for path in history_files:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                total += sum(1 for line in handle if line.strip())
        except Exception:
            continue
    if total > 0:
        return total
    try:
        with FACTOR_RESEARCH_STEPS_FILE.open("r", encoding="utf-8", errors="ignore") as handle:
            return sum(1 for line in handle if line.strip())
    except Exception:
        return 0


def _write_research_step(item: dict) -> None:
    FACTOR_RESEARCH_STEPS_DIR.mkdir(parents=True, exist_ok=True)
    FACTOR_RESEARCH_STEPS_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(_jsonable(item), ensure_ascii=False, default=str)
    history_date = str(item.get("ts") or _now_iso())[:10] or datetime.now().strftime("%Y-%m-%d")
    history_path = FACTOR_RESEARCH_STEPS_HISTORY_DIR / f"{history_date}.jsonl"
    _append_bounded_journal_record(
        current_file=FACTOR_RESEARCH_STEPS_FILE,
        history_path=history_path,
        serialized=serialized,
        max_lines=FACTOR_RESEARCH_STEPS_MAX_LINES,
        max_bytes=FACTOR_RESEARCH_STEPS_MAX_BYTES,
        lock=_RESEARCH_STEPS_LOCK,
    )


def _latest_research_step_for_run(run_id: str, round_id: str | None = None) -> dict:
    run_text = str(run_id or "").strip()
    round_text = str(round_id or "").strip()
    for step in _read_recent_research_steps(limit=48, run_id=run_text):
        if not isinstance(step, dict):
            continue
        if run_text and str(step.get("run_id") or "").strip() != run_text:
            continue
        if round_text and str(step.get("round_id") or "").strip() != round_text:
            continue
        return step
    return {}


def _operator_guidance_identity(step: dict | None) -> tuple[str, str]:
    step = step if isinstance(step, dict) else {}
    extra = step.get("extra") if isinstance(step.get("extra"), dict) else {}
    guidance_id = str(extra.get("guidance_id") or step.get("guidance_id") or step.get("stage_id") or "").strip()
    guidance_stage_id = str(step.get("stage_id") or "").strip()
    return guidance_id, guidance_stage_id


def _operator_guidance_delivery_matches(ref: dict, *, guidance_id: str, guidance_stage_id: str) -> bool:
    if not isinstance(ref, dict) or str(ref.get("type") or "") != "operator_guidance_delivery":
        return False
    delivered_id = str(ref.get("guidance_id") or "").strip()
    delivered_stage_id = str(ref.get("guidance_stage_id") or "").strip()
    return bool(
        (guidance_id and delivered_id == guidance_id)
        or (guidance_stage_id and delivered_stage_id == guidance_stage_id)
    )


def _latest_pending_operator_guidance(run_id: str) -> dict:
    """Return only the newest guidance when it has not reached an LLM request.

    Guidance is a one-shot operator message.  Once the request journal contains
    its delivery receipt it must never be selected again, and an older pending
    message must not reappear after a newer message supersedes it.
    """
    run_text = str(run_id or "").strip()
    if not run_text:
        return {}
    steps = _read_recent_research_steps(limit=512, run_id=run_text)
    latest = next(
        (step for step in steps if str(step.get("stage") or "") == "human_guidance"),
        {},
    )
    if not latest:
        return {}
    guidance_id, guidance_stage_id = _operator_guidance_identity(latest)
    delivered = any(
        _operator_guidance_delivery_matches(
            ref,
            guidance_id=guidance_id,
            guidance_stage_id=guidance_stage_id,
        )
        for step in steps
        for ref in (step.get("evidence_refs") or [])
        if isinstance(ref, dict)
    )
    if delivered:
        return {}
    extra = latest.get("extra") if isinstance(latest.get("extra"), dict) else {}
    return {
        "guidance_id": guidance_id,
        "stage_id": guidance_stage_id,
        "ts": latest.get("ts"),
        "summary": _clip_text(latest.get("summary"), 500),
        "author": extra.get("author") or latest.get("author"),
        "scope": "one_shot_next_llm_judgment",
        "policy": "consume_once_when_included_in_the_next_llm_request",
    }


def _context_pack_with_pending_operator_guidance(context_pack: dict, *, run_id: str) -> dict:
    """Refresh one-shot guidance at the LLM-call boundary.

    Context packs can live across several stages.  Copying and refreshing here
    prevents a guidance item embedded by an earlier stage from leaking into a
    later request after it has already been delivered.
    """
    effective = dict(context_pack or {})
    active_context = dict(
        effective.get("active_context")
        if isinstance(effective.get("active_context"), dict)
        else {}
    )
    active_context.pop("operator_guidance", None)
    pending = _latest_pending_operator_guidance(run_id)
    if pending:
        active_context["operator_guidance"] = pending
    effective["active_context"] = active_context
    return effective


def _orchestrator_prompt_digest(payload: dict | None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    context_pack = payload.get("context_pack") if isinstance(payload.get("context_pack"), dict) else {}
    active_context = context_pack.get("active_context") if isinstance(context_pack.get("active_context"), dict) else {}
    operator_guidance = (
        active_context.get("operator_guidance")
        if isinstance(active_context.get("operator_guidance"), dict)
        else {}
    )
    history = context_pack.get("history_context") if isinstance(context_pack.get("history_context"), dict) else {}
    short_term = history.get("short_term_history") if isinstance(history.get("short_term_history"), dict) else {}
    current_round = context_pack.get("current_round_context") if isinstance(context_pack.get("current_round_context"), dict) else {}
    output_contract = payload.get("output_contract") if isinstance(payload.get("output_contract"), dict) else {}
    context_budget = _prompt_context_budget(payload)
    raw_tool_evidence = context_pack.get("tool_evidence")
    tool_evidence = raw_tool_evidence if isinstance(raw_tool_evidence, dict) else {}
    upstream_handoff = context_pack.get("upstream_handoff") if isinstance(context_pack.get("upstream_handoff"), dict) else {}
    review_anchors = short_term.get("review_anchors") if isinstance(short_term.get("review_anchors"), list) else []
    recent_rounds = short_term.get("stage_relevant_steps") if isinstance(short_term.get("stage_relevant_steps"), list) else []
    history_used = []
    for anchor in review_anchors[:3]:
        if not isinstance(anchor, dict):
            continue
        stage = str(anchor.get("stage") or "").strip()
        decision = str(anchor.get("decision") or "").strip()
        judgment = str(anchor.get("judgment") or "").strip()
        if stage or decision or judgment:
            history_used.append(_clip_text(" / ".join(part for part in (stage, decision, judgment) if part), 160))
    if not history_used:
        for item in recent_rounds[:3]:
            if not isinstance(item, dict):
                continue
            stage = str(item.get("stage") or "").strip()
            decision = str(item.get("decision") or "").strip()
            summary = str(item.get("summary") or "").strip()
            if stage or decision or summary:
                history_used.append(_clip_text(" / ".join(part for part in (stage, decision, summary) if part), 160))
    tool_counts = []
    for key, value in tool_evidence.items():
        count = len(value) if isinstance(value, (list, dict)) else 0
        if count:
            tool_counts.append(f"{key}={count}")
    tool_summary = list(tool_counts[:4])
    operator_summary = tool_evidence.get("operator_list_summary") if isinstance(tool_evidence.get("operator_list_summary"), dict) else {}
    supported_operators = operator_summary.get("supported_operators") if isinstance(operator_summary.get("supported_operators"), list) else []
    normalization_operators = operator_summary.get("normalization_operators") if isinstance(operator_summary.get("normalization_operators"), list) else []
    availability = operator_summary.get("availability") if isinstance(operator_summary.get("availability"), dict) else {}
    if supported_operators:
        tool_summary.append(_clip_text(f"ops={','.join(str(item) for item in supported_operators[:8])}", 160))
    if normalization_operators:
        tool_summary.append(_clip_text(f"norm={','.join(str(item) for item in normalization_operators[:6])}", 160))
    if availability:
        availability_bits = []
        for key in ("cross_section_zscore", "group_zscore", "time_series_zscore", "percentile_rank"):
            if key in availability:
                availability_bits.append(f"{key}={availability.get(key)}")
        if availability_bits:
            tool_summary.append(_clip_text("avail=" + ",".join(availability_bits), 180))
    current_bits = []
    for key, label in (
        ("thesis", "thesis"),
        ("hypotheses", "hypothesis"),
        ("candidate_drafts", "parents"),
    ):
        value = current_round.get(key)
        size = len(value) if isinstance(value, list) else 0
        if size:
            current_bits.append(f"{label}={size}")
    handoff = current_round.get("handoff") if isinstance(current_round.get("handoff"), dict) else {}
    if handoff:
        current_bits.append("handoff=1")
    handoff_reason = _clip_text(upstream_handoff.get("reason"), 180) if upstream_handoff else ""
    required_fields = [str(item)[:80] for item in (output_contract.get("required_fields") or [])[:10] if str(item).strip()]
    allowed_next_stages = [
        str(item)[:80]
        for item in (output_contract.get("allowed_next_stages") or [])[:8]
        if str(item).strip()
    ]
    return {
        "stage_briefing": _clip_text(payload.get("stage_briefing"), 220),
        "history_used": history_used[:4],
        "facts": _clip_text(
            " | ".join(
                part
                for part in (
                    f"recent_rounds={len(recent_rounds)}" if recent_rounds else "",
                    f"review_anchors={len(review_anchors)}" if review_anchors else "",
                    ",".join(current_bits) if current_bits else "",
                    ",".join(tool_summary[:5]) if tool_summary else "",
                )
                if part
            ),
            360,
        ),
        "current_round_summary": current_bits[:6],
        "handoff_reason": handoff_reason,
        "tool_summary": tool_summary[:6],
        "required_fields": required_fields,
        "allowed_next_stages": allowed_next_stages,
        "context_budget": context_budget,
        "operator_guidance": {
            key: operator_guidance.get(key)
            for key in ("guidance_id", "stage_id", "ts", "author", "summary", "scope")
            if operator_guidance.get(key) not in (None, "")
        },
    }


def _write_orchestrator_llm_request_step(
    *,
    run_id: str,
    round_id: str,
    stage: str,
    checkpoint: str,
    trace_id: str,
    payload_chars: int,
    llm_model: str,
    prompt_digest: dict | None = None,
) -> None:
    latest = _latest_research_step_for_run(run_id, round_id=round_id)
    latest_transition = latest.get("stage_transition") if isinstance(latest.get("stage_transition"), dict) else {}
    prompt_digest = prompt_digest if isinstance(prompt_digest, dict) else {}
    history_preview = prompt_digest.get("history_used") if isinstance(prompt_digest.get("history_used"), list) else []
    history_used_text = "；".join(
        _clip_text(item, 120)
        for item in history_preview[:4]
        if str(item).strip()
    )
    handoff_reason = _clip_text(prompt_digest.get("handoff_reason"), 180)
    stage_briefing = _clip_text(prompt_digest.get("stage_briefing"), 220)
    facts_text = _clip_text(prompt_digest.get("facts"), 360)
    tool_summary = prompt_digest.get("tool_summary") if isinstance(prompt_digest.get("tool_summary"), list) else []
    knowledge_titles = prompt_digest.get("knowledge_titles") if isinstance(prompt_digest.get("knowledge_titles"), list) else []
    lineage_summary = prompt_digest.get("lineage_summary") if isinstance(prompt_digest.get("lineage_summary"), list) else []
    required_fields = prompt_digest.get("required_fields") if isinstance(prompt_digest.get("required_fields"), list) else []
    allowed_next_stages = (
        prompt_digest.get("allowed_next_stages") if isinstance(prompt_digest.get("allowed_next_stages"), list) else []
    )
    context_budget = prompt_digest.get("context_budget") if isinstance(prompt_digest.get("context_budget"), dict) else {}
    operator_guidance = (
        prompt_digest.get("operator_guidance")
        if isinstance(prompt_digest.get("operator_guidance"), dict)
        else {}
    )
    research_strategy = _clip_text(
        "；".join(
            part
            for part in (
                f"优先处理上游handoff：{handoff_reason}" if handoff_reason else "",
                f"阶段任务：{stage_briefing}" if stage_briefing else "",
                f"候选谱系：{', '.join(lineage_summary[:4])}" if lineage_summary else "",
                f"关键知识：{', '.join(knowledge_titles[:2])}" if knowledge_titles else "",
                f"工具证据：{', '.join(tool_summary[:3])}" if tool_summary else "",
                f"人工干预：{_clip_text(operator_guidance.get('summary'), 160)}" if operator_guidance else "",
                f"输出字段：{', '.join(required_fields[:6])}" if required_fields else "",
            )
            if part
        ),
        520,
    )
    stage_seq = int(latest.get("stage_seq") or 1)
    if str(latest.get("stage") or "").strip() != str(stage or "").strip():
        stage_seq = max(1, stage_seq + 1)
    request_suffix = str(trace_id).split(":")[-1] or uuid.uuid4().hex[:8]
    context_digest_text = _clip_text(
        " | ".join(
            part
            for part in (
                facts_text,
                f"lineage={','.join(lineage_summary[:4])}" if lineage_summary else "",
                f"required={','.join(required_fields[:6])}" if required_fields else "",
                f"allowed_next={','.join(allowed_next_stages[:4])}" if allowed_next_stages else "",
            )
            if part
        ),
        980,
    )
    step = {
        "schema_version": "research_step_v2",
        "ts": _now_iso(),
        "run_id": run_id,
        "round_id": round_id,
        "stage_seq": stage_seq,
        "stage_id": f"{round_id}:req_{checkpoint}_{request_suffix}",
        "previous_stage": str(latest.get("stage") or ""),
        "previous_stage_id": str(latest.get("stage_id") or ""),
        "stage": stage,
        "summary": (
            f"DeepSeek v4 已收到人工干预与 {stage} 阶段证据，正在生成研究判断。"
            if operator_guidance
            else f"DeepSeek v4 已收到 {stage} 阶段证据，正在生成研究判断。"
        ),
        "decision": "进入 LLM review 阶段，等待 DeepSeek v4 返回 JSON 决策。",
        "refs": [],
        "priority": "normal",
        "evidence_refs": [
            {
                "type": "llm_trace",
                "source": "llm_trace",
                "trace_id": trace_id,
                "trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
                "note": _clip_text(f"{stage} · llm_request", 120),
            },
            {
                "type": "context_pack_digest",
                "source": "llm_request",
                "payload_chars": payload_chars,
                "history_preview": history_preview[:4],
                "handoff_reason": handoff_reason,
                "knowledge_titles": knowledge_titles[:2],
                "tool_summary": tool_summary[:6],
                "lineage_summary": lineage_summary[:6],
                "required_fields": required_fields[:10],
                "allowed_next_stages": allowed_next_stages[:8],
                "context_budget": context_budget,
                "note": _clip_text(f"history={len(history_preview)}；tools={','.join(tool_summary[:3])}", 120),
            },
        ],
        "tags": ["orchestrator", "deepseek_v4", "llm_request_progress", str(stage or "")[:80]],
        "mode": "orchestrator",
        "llm_trace_id": trace_id,
        "monitoring": {
            "mode": "orchestrator",
            "event_type": "llm_request",
            "checkpoint": checkpoint,
            "llm_trace_id": trace_id,
            "llm_model": llm_model,
            "payload_chars": payload_chars,
            "context_budget": context_budget,
            "operator_guidance": operator_guidance,
        },
        "stage_transition": {
            "next_stage": str(stage or ""),
            "next_action": "llm_review_in_progress",
            "research_strategy": research_strategy,
            "facts": (
                _clip_text(f"上下文摘要（非模型判断）：{context_digest_text}", 1100)
                if context_digest_text
                else ""
            ),
            "judgment": _clip_text(
                f"等待 DeepSeek v4 返回 {stage} 阶段 JSON 研究判断；此记录仅表示请求已发出。",
                220,
            ),
            "mode": "orchestrator",
            "llm_model": llm_model,
            "llm_trace_id": trace_id,
            "why": _clip_text(
                "；".join(
                    part
                    for part in (
                        f"DeepSeek 请求已发出，等待模型返回本阶段判断。payload_chars={payload_chars}",
                        f"budget={context_budget.get('after_chars')}/{context_budget.get('max_payload_chars')}" if context_budget else "",
                        f"handoff={handoff_reason}" if handoff_reason else "",
                    )
                    if part
                ),
                320,
            ),
            "history_used": history_used_text or _clip_text(latest_transition.get("history_used"), 220),
        },
    }
    if operator_guidance:
        step["evidence_refs"].append(
            {
                "type": "operator_guidance_delivery",
                "source": "llm_request",
                "guidance_id": operator_guidance.get("guidance_id"),
                "guidance_stage_id": operator_guidance.get("stage_id"),
                "guidance_summary": _clip_text(operator_guidance.get("summary"), 240),
                "trace_id": trace_id,
                "delivered_to_stage": stage,
                "note": "最新人工干预已包含在本次 DeepSeek payload 中。",
            }
        )
    _write_research_step(step)


def _orchestrator_event_projection(event: dict) -> dict:
    transition = _clean_stage_transition_payload(
        event.get("stage_transition") if isinstance(event.get("stage_transition"), dict) else {},
        next_action=str(event.get("decision") or ""),
    )
    evidence_refs = _orchestrator_projection_evidence_refs(event)
    monitoring = _orchestrator_projection_monitoring(event)
    strategy_bits = [transition.get("research_strategy", "")]
    facts_bits = [transition.get("facts", "")]
    event_tags = {str(tag) for tag in (event.get("tags") or []) if str(tag).strip()}
    is_llm_result = str(event.get("event_type") or "") == "llm_result" or bool(
        {"llm_result", "llm_review"} & event_tags
    )
    allowed_actions = monitoring.get("allowed_actions") if isinstance(monitoring.get("allowed_actions"), list) else []
    blocked_actions = monitoring.get("blocked_actions") if isinstance(monitoring.get("blocked_actions"), list) else []
    candidate_watch = monitoring.get("candidate_watch") if isinstance(monitoring.get("candidate_watch"), list) else []
    evidence_watch = monitoring.get("evidence_watch") if isinstance(monitoring.get("evidence_watch"), list) else []
    if (allowed_actions or blocked_actions) and not is_llm_result:
        strategy_bits.append(
            _clip_text(
                "动作约束: "
                + "；".join(
                    part
                    for part in (
                        f"allow={','.join(str(item) for item in allowed_actions[:4])}" if allowed_actions else "",
                        f"block={','.join(str(item) for item in blocked_actions[:4])}" if blocked_actions else "",
                    )
                    if part
                ),
                320,
            )
        )
    if candidate_watch and not is_llm_result:
        facts_bits.append(
            _clip_text(
                "候选跟踪: "
                + "；".join(
                    _summarize_fact_item(item, fallback_idx=idx + 1)
                    for idx, item in enumerate(candidate_watch[:4])
                    if isinstance(item, dict)
                ),
                520,
            )
        )
    if evidence_watch and not is_llm_result:
        facts_bits.append(
            _clip_text(
                "证据摘要: "
                + "；".join(
                    _summarize_fact_item(item, fallback_idx=idx + 1)
                    for idx, item in enumerate(evidence_watch[:4])
                    if isinstance(item, dict)
                ),
                520,
            )
        )
    transition["research_strategy"] = _clip_text("；".join(part for part in strategy_bits if part), 520)
    transition["facts"] = _clip_text(" | ".join(part for part in facts_bits if part), 1800)
    return {
        "schema_version": "research_step_v2",
        "ts": event.get("ts") or _now_iso(),
        "run_id": str(event.get("run_id") or "manual")[:120],
        "round_id": str(event.get("round_id") or "round-unset")[:120],
        "stage_seq": int(event.get("stage_seq") or 1),
        "stage_id": str(event.get("stage_id") or "")[:180],
        "previous_stage": str(event.get("previous_stage") or ""),
        "previous_stage_id": str(event.get("previous_stage_id") or ""),
        "stage": str(event.get("stage") or "note"),
        "summary": _clip_text(event.get("summary", ""), 900),
        "decision": _clip_text(event.get("decision", ""), 260),
        "refs": [str(ref)[:180] for ref in (event.get("refs") or [])[:12] if str(ref).strip()],
        "priority": str(event.get("priority") or "normal"),
        "evidence_refs": evidence_refs[:16],
        "tags": [str(tag)[:80] for tag in (event.get("tags") or [])[:12] if str(tag).strip()],
        "mode": "orchestrator" if "orchestrator" in {str(tag) for tag in (event.get("tags") or [])} else "",
        "llm_trace_id": _clip_text(event.get("llm_trace_id", ""), 180),
        "monitoring": monitoring,
        "stage_transition": transition,
    }


def _factor_trajectory_id(
    *,
    run_id: str,
    round_id: str,
    candidate_id: str,
    expression: str,
) -> str:
    material = "|".join(
        str(value or "").strip()
        for value in (run_id, round_id, candidate_id, expression)
    )
    return f"ft_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def _compact_candidate_lane_for_step(candidate: dict) -> dict:
    if not isinstance(candidate, dict):
        return {}
    keys = (
        "candidate_id",
        "trajectory_id",
        "parent_candidate_id",
        "mutation_summary",
        "factor_map_id",
        "factor_map_audit_id",
        "matched_region_uid",
        "factor_name",
        "candidate_lane",
        "economic_thesis",
        "hypothesis",
        "expression",
        "status",
        "status_label",
        "status_reason",
        "precheck_status",
        "precheck_instruction",
        "precheck_warnings",
        "decision_source",
        "candidate_plan_action",
        "matched_candidate_ids",
        "matched_cluster_id",
        "matched_factor_ids",
        "score",
        "grade",
        "ic",
        "icir",
        "quick_score",
        "deep_score",
        "deep_score_policy_version",
        "deep_action",
        "deep_reason",
        "anti_overfit_score",
        "adversarial_score",
        "novelty_score",
        "rolling_score",
        "rolling_grade",
        "rolling_policy_version",
        "rolling_status",
        "rolling_6m_ic",
        "rolling_12m_ic",
        "rolling_24m_ic",
        "rolling_48m_ic",
        "rolling_weighted_ic",
        "rolling_weighted_std",
        "rolling_robust_ic",
        "task_id",
    )
    compact = {key: candidate.get(key) for key in keys if candidate.get(key) not in (None, "", [], {})}
    if "expression" in compact:
        compact["expression"] = _clip_text(compact["expression"], 260)
    if "hypothesis" in compact:
        compact["hypothesis"] = _clip_text(compact["hypothesis"], 160)
    return _jsonable(compact)


def _candidate_progress_brief(candidate: dict) -> str:
    if not isinstance(candidate, dict):
        return ""
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    expression = _clip_text(candidate.get("expression"), 140)
    factor_name = _clip_text(candidate.get("factor_name"), 80)
    parts = [part for part in (candidate_id, factor_name, expression) if part]
    return " | ".join(parts[:3])


def _compact_projection_candidate_watch(candidate: dict) -> dict:
    if not isinstance(candidate, dict):
        return {}
    compact = {
        key: candidate.get(key)
        for key in (
            "candidate_id",
            "factor_name",
            "candidate_lane",
            "trajectory_id",
            "parent_candidate_id",
            "mutation_summary",
            "matched_region_uid",
            "expression",
            "status",
            "status_label",
            "status_reason",
            "precheck_status",
            "precheck_instruction",
            "precheck_warnings",
            "score",
            "grade",
            "quick_score",
            "deep_score",
            "deep_score_policy_version",
            "ic",
            "icir",
            "novelty_score",
            "rolling_score",
            "rolling_grade",
            "rolling_policy_version",
            "rolling_status",
            "rolling_6m_ic",
            "rolling_12m_ic",
            "rolling_24m_ic",
            "rolling_48m_ic",
            "rolling_weighted_ic",
            "rolling_weighted_std",
            "rolling_robust_ic",
            "action",
            "reason",
            "weakest_component",
            "mutation_advice",
        )
        if candidate.get(key) not in (None, "", [], {})
    }
    if "expression" in compact:
        compact["expression"] = _clip_text(compact["expression"], 220)
    if "mutation_advice" in compact:
        compact["mutation_advice"] = _clip_text(compact["mutation_advice"], 220)
    return _jsonable(compact)


_ORCHESTRATOR_CANDIDATE_LANE_KEYS = (
    "keepers",
    "dropped",
    "adopted",
    "rejected",
    "screened_out",
    "failed",
    "imported",
    "details",
    "candidates",
)
_ORCHESTRATOR_NEGATIVE_LANE_KEYS = {"dropped", "rejected", "screened_out", "failed"}


def _orchestrator_candidate_lane_items(
    value: Any,
    *,
    include_negative: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Normalize score/novelty/gate/import candidate payloads."""

    sources: list[tuple[str, list[Any]]] = []
    if isinstance(value, list):
        sources.append(("candidate_lanes", value))
    elif isinstance(value, dict):
        for key in _ORCHESTRATOR_CANDIDATE_LANE_KEYS:
            if key in _ORCHESTRATOR_NEGATIVE_LANE_KEYS and not include_negative:
                continue
            items = value.get(key)
            if isinstance(items, list):
                sources.append((key, items))

    normalized: list[dict[str, Any]] = []
    for lane_name, items in sources:
        for item in items:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row.setdefault("candidate_lane", lane_name)
            if not str(row.get("candidate_id") or "").strip():
                row["candidate_id"] = _candidate_id(row, len(normalized))
            normalized.append(row)
            if limit is not None and len(normalized) >= limit:
                return normalized
    return normalized


def _orchestrator_candidate_lane_counts(value: Any) -> dict[str, int]:
    if isinstance(value, list):
        return {"candidate_lanes": len([item for item in value if isinstance(item, dict)])}
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for key in _ORCHESTRATOR_CANDIDATE_LANE_KEYS:
        items = value.get(key)
        if isinstance(items, list):
            counts[key] = len([item for item in items if isinstance(item, dict)])
    return counts


def _summarize_fact_item(item: Any, *, fallback_idx: int = 0) -> str:
    if isinstance(item, dict):
        candidate_id = str(item.get("candidate_id") or item.get("factor_name") or f"c{fallback_idx}" if fallback_idx else "").strip()
        action = str(item.get("action") or item.get("decision") or item.get("lane") or "").strip()
        reason = str(item.get("reason") or item.get("mutation_advice") or item.get("novelty_interpretation") or "").strip()
        descriptor = str(
            item.get("expression")
            or item.get("mechanism_summary")
            or item.get("signal_claim")
            or item.get("economic_rationale")
            or ""
        ).strip()
        metrics = []
        for label, key in (
            ("grade", "grade"),
            ("score", "score"),
            ("quick", "quick_score"),
            ("deep", "deep_score"),
            ("IC", "ic"),
            ("ICIR", "icir"),
            ("novelty", "novelty_score"),
        ):
            value = item.get(key)
            if value not in (None, "", [], {}):
                metrics.append(f"{label}={value}")
        head = " ".join(part for part in (candidate_id, action) if part)
        tail = "；".join(
            part
            for part in (
                " / ".join(metrics),
                _clip_text(descriptor, 160) if descriptor else "",
                reason,
            )
            if part
        )
        return "：".join(part for part in (head, tail) if part)
    if isinstance(item, str):
        return _clip_text(item, 220)
    return _clip_text(_jsonable(item), 220)


def _orchestrator_projection_monitoring(event: dict) -> dict:
    monitoring: dict[str, Any] = {
        "mode": "orchestrator",
        "event_type": str(event.get("event_type") or ""),
        "checkpoint": str(event.get("checkpoint") or event.get("stage") or ""),
        "heartbeat_status": str(event.get("heartbeat_status") or "alive"),
        "thread_id": str(event.get("thread_id") or ""),
        "llm_trace_id": str(event.get("llm_trace_id") or ""),
        "llm_model": str(((event.get("stage_transition") or {}).get("llm_model")) or event.get("llm_model") or ""),
        "context_budget": event.get("context_budget") if isinstance(event.get("context_budget"), dict) else {},
        "allowed_actions": [str(item)[:80] for item in (event.get("allowed_actions") or [])[:8] if str(item).strip()],
        "blocked_actions": [str(item)[:80] for item in (event.get("blocked_actions") or [])[:8] if str(item).strip()],
        "candidate_watch": [],
        "evidence_watch": [],
    }
    candidate_watch: dict[str, dict[str, Any]] = {}

    def _watch_key(item: dict, idx: int) -> str:
        return str(item.get("candidate_id") or item.get("expression") or item.get("factor_name") or f"idx:{idx}")

    candidate_lanes = _orchestrator_candidate_lane_items(event.get("candidate_lanes"), limit=8)
    for idx, item in enumerate(candidate_lanes):
        if not isinstance(item, dict):
            continue
        key = _watch_key(item, idx)
        candidate_watch[key] = _compact_projection_candidate_watch(item)

    advice = event.get("advice") if isinstance(event.get("advice"), dict) else {}
    for idx, item in enumerate((advice.get("candidate_lane_decisions") or [])[:8]):
        if not isinstance(item, dict):
            continue
        key = _watch_key(item, idx)
        existing = dict(candidate_watch.get(key) or {})
        merged = {
            **existing,
            **{
                key_name: item.get(key_name)
                for key_name in (
                    "candidate_id",
                    "factor_name",
                    "candidate_lane",
                    "expression",
                    "status",
                    "status_label",
                    "status_reason",
                    "precheck_status",
                    "precheck_instruction",
                    "precheck_warnings",
                    "score",
                    "grade",
                    "quick_score",
                    "deep_score",
                    "deep_score_policy_version",
                    "novelty_score",
                    "rolling_score",
                    "rolling_grade",
                    "rolling_policy_version",
                    "rolling_status",
                    "rolling_6m_ic",
                    "rolling_12m_ic",
                    "rolling_24m_ic",
                    "rolling_48m_ic",
                    "rolling_weighted_ic",
                    "rolling_weighted_std",
                    "rolling_robust_ic",
                    "action",
                    "reason",
                    "weakest_component",
                    "mutation_advice",
                )
                if item.get(key_name) not in (None, "", [], {})
            },
        }
        mutation_advice = item.get("mutation_advice")
        if not merged.get("mutation_advice") and isinstance(mutation_advice, dict):
            merged["mutation_advice"] = mutation_advice.get("instruction") or mutation_advice.get("type")
        candidate_watch[key] = _compact_projection_candidate_watch(merged)

    existing_refs = event.get("evidence_refs") if isinstance(event.get("evidence_refs"), list) else []
    evidence_watch: list[dict[str, Any]] = []
    for ref in existing_refs[:12]:
        if not isinstance(ref, dict):
            continue
        if ref.get("candidate_id") or ref.get("tool"):
            entry = {
                key: ref.get(key)
                for key in (
                    "candidate_id",
                    "tool",
                    "task_id",
                    "idempotency_key",
                    "policy",
                    "score",
                    "grade",
                    "quick_score",
                    "deep_score",
                    "ic",
                    "icir",
                    "anti_overfit_score",
                    "adversarial_score",
                    "novelty_score",
                    "rolling_score",
                    "rolling_grade",
                    "rolling_policy_version",
                    "rolling_status",
                    "rolling_6m_ic",
                    "rolling_12m_ic",
                    "rolling_24m_ic",
                    "rolling_48m_ic",
                    "rolling_weighted_ic",
                    "rolling_weighted_std",
                    "rolling_robust_ic",
                )
                if ref.get(key) not in (None, "", [], {})
            }
            if entry:
                evidence_watch.append(_jsonable(entry))
    monitoring["candidate_watch"] = [item for item in candidate_watch.values() if item][:6]
    monitoring["evidence_watch"] = evidence_watch[:8]
    return {key: value for key, value in monitoring.items() if value not in ("", [], {}, None)}


def _orchestrator_projection_evidence_refs(event: dict) -> list[dict]:
    refs: list[dict] = []
    refs.append(
        {
            "type": "orchestrator_event",
            "source": "orchestrator_event",
            "run_id": event.get("run_id"),
            "round_id": event.get("round_id"),
            "stage_id": event.get("stage_id"),
            "event_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE),
            "note": _clip_text(f"{event.get('stage') or 'stage'} · {event.get('decision') or event.get('summary') or ''}", 120),
        }
    )
    if event.get("llm_trace_id"):
        refs.append(
            {
                "type": "llm_trace",
                "source": "llm_trace",
                "trace_id": event.get("llm_trace_id"),
                "trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
                "note": _clip_text(
                    f"{((event.get('stage_transition') or {}).get('llm_model')) or event.get('stage') or 'llm'}",
                    120,
                ),
            }
        )
    if event.get("context_pack_digest"):
        refs.append(
            {
                "type": "context_pack_digest",
                "source": "context_pack_digest",
                "note": _clip_text(
                    json.dumps(_jsonable(event.get("context_pack_digest") or {}), ensure_ascii=False, default=str),
                    120,
                ),
                **_jsonable(event.get("context_pack_digest") or {}),
            }
        )
    candidate_lanes = _orchestrator_candidate_lane_items(event.get("candidate_lanes"), limit=12)
    if candidate_lanes:
        refs.append(
            {
                "type": "candidate_lanes",
                "source": "candidate_lanes",
                "count": len(candidate_lanes),
                "lane_counts": _orchestrator_candidate_lane_counts(event.get("candidate_lanes")),
                "note": _clip_text(
                    "；".join(
                        _summarize_fact_item(item, fallback_idx=idx + 1)
                        for idx, item in enumerate(candidate_lanes[:4])
                        if isinstance(item, dict)
                    ),
                    160,
                ),
                "items": [_compact_candidate_lane_for_step(item) for item in candidate_lanes[:6] if isinstance(item, dict)],
            }
        )
    advice = event.get("advice") if isinstance(event.get("advice"), dict) else {}
    if advice:
        refs.append(
            {
                "type": "advice_summary",
                "source": "advice",
                "action": advice.get("action") or advice.get("recommended_action"),
                "strategy": advice.get("strategy"),
                "next_thesis_policy": advice.get("next_thesis_policy"),
                "allowed_actions": _list_prefix(advice.get("allowed_actions"), 8),
                "blocked_actions": _list_prefix(advice.get("blocked_actions"), 8),
                "note": _clip_text(
                    "；".join(
                        part
                        for part in (
                            advice.get("action") or advice.get("recommended_action") or "",
                            f"allow={','.join(str(item) for item in _list_prefix(advice.get('allowed_actions'), 3))}" if advice.get("allowed_actions") else "",
                            f"block={','.join(str(item) for item in _list_prefix(advice.get('blocked_actions'), 3))}" if advice.get("blocked_actions") else "",
                        )
                        if part
                    ),
                    160,
                ),
                "candidate_lane_decisions": _list_prefix(advice.get("candidate_lane_decisions"), 6),
                "trajectory_metrics": advice.get("trajectory_metrics") or {},
            }
        )
    if event.get("allowed_actions") or event.get("blocked_actions"):
        refs.append(
            {
                "type": "action_guard",
                "source": "action_guard",
                "allowed_actions": _list_prefix(event.get("allowed_actions"), 8),
                "blocked_actions": _list_prefix(event.get("blocked_actions"), 8),
                "note": _clip_text(
                    "；".join(
                        part
                        for part in (
                            f"allow={','.join(str(item) for item in _list_prefix(event.get('allowed_actions'), 3))}" if event.get("allowed_actions") else "",
                            f"block={','.join(str(item) for item in _list_prefix(event.get('blocked_actions'), 3))}" if event.get("blocked_actions") else "",
                        )
                        if part
                    ),
                    160,
                ),
            }
        )
    existing = event.get("evidence_refs") if isinstance(event.get("evidence_refs"), list) else []
    refs.extend(_jsonable(existing)[:8])
    return [ref for ref in refs if isinstance(ref, dict) and ref]


def _write_orchestrator_event(event: dict, *, sync_research_step: bool = True) -> dict:
    FACTOR_ORCHESTRATOR_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    redacted_event, redacted_count = _redact_orchestrator_payload(dict(event or {}))
    clean_event = _jsonable(redacted_event)
    if not isinstance(clean_event, dict):
        clean_event = {}
    clean_event.setdefault("schema_version", "orchestrator_event_v1")
    clean_event.setdefault("ts", _now_iso())
    clean_event.setdefault("tags", [])
    residual_count = _orchestrator_secret_residue_count(clean_event)
    redaction_warning = ""
    if residual_count:
        clean_event = _strip_orchestrator_full_payload_fields(clean_event)
        redaction_warning = "secret_like_residue_payload_fields_removed"
        residual_count = _orchestrator_secret_residue_count(clean_event)
    clean_event["redaction_status"] = _orchestrator_redaction_status(
        redacted_count,
        surface="orchestrator_event",
        redaction_warning=redaction_warning,
        residual_secret_like_count=residual_count,
    )
    tags = [str(tag) for tag in clean_event.get("tags") or [] if str(tag).strip()]
    if "orchestrator" not in tags:
        tags.insert(0, "orchestrator")
    clean_event["tags"] = tags[:12]
    progress_only_tags = {"llm_request_progress", "tool_progress", "candidate_progress"}
    is_progress_only = bool({tag.lower() for tag in clean_event["tags"]} & progress_only_tags)
    if sync_research_step and not is_progress_only:
        projection = _orchestrator_event_projection(clean_event)
        clean_event["sync_status"] = "synced_to_research_step"
    elif sync_research_step:
        projection = {}
        # Tool/candidate heartbeat remains fully observable in the ORCH journal
        # and QuantGPT task DB, but is not a human/LLM stage transition.
        clean_event["sync_status"] = "event_only_progress"
    else:
        projection = {}
        clean_event["sync_status"] = "event_only"
    serialized = json.dumps(clean_event, ensure_ascii=False, default=str)
    history_date = str(clean_event.get("ts") or _now_iso())[:10] or datetime.now().strftime("%Y-%m-%d")
    history_path = FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR / f"{history_date}.jsonl"
    _append_bounded_journal_record(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_path=history_path,
        serialized=serialized,
        max_lines=FACTOR_ORCHESTRATOR_EVENTS_MAX_LINES,
        max_bytes=FACTOR_ORCHESTRATOR_EVENTS_MAX_BYTES,
        lock=_ORCHESTRATOR_EVENTS_LOCK,
    )
    if projection:
        _write_research_step(projection)
    return clean_event


def _write_orchestrator_llm_trace(record: dict) -> dict:
    FACTOR_ORCHESTRATOR_LLM_TRACES_DIR.mkdir(parents=True, exist_ok=True)
    FACTOR_ORCHESTRATOR_LLM_TRACES_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    redacted_record, redacted_count = _redact_orchestrator_payload(dict(record or {}))
    clean = _jsonable(redacted_record)
    if not isinstance(clean, dict):
        clean = {}
    clean.setdefault("schema_version", "orchestrator_llm_trace_v1")
    clean.setdefault("ts", _now_iso())
    clean.setdefault("trace_type", "llm_trace")
    residual_count = _orchestrator_secret_residue_count(clean)
    redaction_warning = ""
    if residual_count:
        clean = _strip_orchestrator_full_payload_fields(clean)
        redaction_warning = "secret_like_residue_payload_fields_removed"
        residual_count = _orchestrator_secret_residue_count(clean)
    clean["redaction_status"] = _orchestrator_redaction_status(
        redacted_count,
        surface="orchestrator_llm_trace",
        redaction_warning=redaction_warning,
        residual_secret_like_count=residual_count,
    )
    serialized = json.dumps(clean, ensure_ascii=False, default=str)
    history_date = str(clean.get("ts") or _now_iso())[:10] or datetime.now().strftime("%Y-%m-%d")
    history_path = FACTOR_ORCHESTRATOR_LLM_TRACES_HISTORY_DIR / f"{history_date}.jsonl"
    _append_bounded_journal_record(
        current_file=FACTOR_ORCHESTRATOR_LLM_TRACES_FILE,
        history_path=history_path,
        serialized=serialized,
        max_lines=FACTOR_ORCHESTRATOR_LLM_TRACES_MAX_LINES,
        max_bytes=FACTOR_ORCHESTRATOR_LLM_TRACES_MAX_BYTES,
        lock=_ORCHESTRATOR_LLM_TRACES_LOCK,
    )
    return clean


def _compact_orchestrator_event(record: dict, *, include_payload: bool = False) -> dict:
    compact: dict[str, Any] = {
        "schema_version": record.get("schema_version"),
        "ts": record.get("ts"),
        "run_id": record.get("run_id"),
        "round_id": record.get("round_id"),
        "stage_seq": record.get("stage_seq"),
        "stage_id": record.get("stage_id"),
        "previous_stage": record.get("previous_stage"),
        "previous_stage_id": record.get("previous_stage_id"),
        "stage": record.get("stage"),
        "event_type": record.get("event_type"),
        "checkpoint": record.get("checkpoint"),
        "summary": record.get("summary"),
        "decision": record.get("decision"),
        "priority": record.get("priority"),
        "stage_transition": record.get("stage_transition"),
        "tags": record.get("tags"),
        "sync_status": record.get("sync_status"),
        "allowed_actions": record.get("allowed_actions"),
        "blocked_actions": record.get("blocked_actions"),
        "redaction_status": record.get("redaction_status"),
    }
    evidence_refs = record.get("evidence_refs") if isinstance(record.get("evidence_refs"), list) else []
    candidate_lanes = _orchestrator_candidate_lane_items(record.get("candidate_lanes"), limit=24)
    advice = record.get("advice") if isinstance(record.get("advice"), dict) else {}
    compact["evidence_ref_count"] = len(evidence_refs)
    compact["candidate_lane_count"] = len(candidate_lanes)
    lane_counts = _orchestrator_candidate_lane_counts(record.get("candidate_lanes"))
    if lane_counts:
        compact["candidate_lane_counts"] = lane_counts
    if evidence_refs:
        compact["evidence_refs_preview"] = [
            _compact_evidence_ref_for_live(ref)
            for ref in evidence_refs[:8]
        ]
    if advice:
        compact["advice_summary"] = {
            "recommended_action": advice.get("recommended_action"),
            "next_thesis_policy": advice.get("next_thesis_policy"),
            "candidate_lane_decision_count": len(advice.get("candidate_lane_decisions") or []),
            "allowed_actions": advice.get("allowed_actions"),
            "blocked_actions": advice.get("blocked_actions"),
        }
    if include_payload:
        compact["event"] = record
    return {key: _jsonable(value) for key, value in compact.items() if value not in (None, "", [], {})}


def factor_research_orchestrator_events(
    *,
    run_id: str | None = None,
    limit: int = 80,
    include_payload: bool = False,
    include_history: bool = True,
) -> ServiceResult:
    """Read Orchestrator event records without mutating research state."""
    max_items = min(300, max(1, int(limit or 80)))
    selected_run_id = (run_id or "").strip() or None
    if not _journal_paths(FACTOR_ORCHESTRATOR_EVENTS_FILE, FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR):
        return ok_result(
            inputs={"run_id": selected_run_id, "limit": max_items, "include_payload": include_payload},
            outputs={"events": [], "count": 0, "event_file_exists": False},
            artifacts={"orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE)},
        )
    records, metrics = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        run_id=selected_run_id,
        limit=max_items,
        max_lines_per_file=max(1000, max_items * 20),
        max_bytes_per_file=FACTOR_ORCHESTRATOR_EVENTS_MAX_BYTES,
        include_history=include_history,
    )
    events = [_compact_orchestrator_event(record, include_payload=include_payload) for record in records]
    events.reverse()
    return ok_result(
        inputs={"run_id": selected_run_id, "limit": max_items, "include_payload": include_payload},
        outputs={
            "events": events,
            "count": len(events),
            "scanned_lines": metrics["scanned_lines"],
            "tail_read": not bool(selected_run_id),
            "history_reverse_read": bool(selected_run_id and metrics["history_files_scanned"]),
            "parse_errors": metrics["parse_errors"],
            "history_files_scanned": metrics["history_files_scanned"],
            "source_files": metrics["source_files"],
            "event_file_exists": FACTOR_ORCHESTRATOR_EVENTS_FILE.exists(),
        },
        artifacts={"orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE)},
    )


def _compact_orchestrator_llm_trace(record: dict, *, include_payload: bool = False) -> dict:
    event_type = str(record.get("event_type") or "")
    compact: dict[str, Any] = {
        "schema_version": record.get("schema_version"),
        "trace_id": record.get("trace_id"),
        "ts": record.get("ts"),
        "run_id": record.get("run_id"),
        "round_id": record.get("round_id"),
        "stage": record.get("stage"),
        "checkpoint": record.get("checkpoint"),
        "event_type": event_type,
        "llm_provider": record.get("llm_provider"),
        "llm_model": record.get("llm_model"),
        "llm_model_order": record.get("llm_model_order"),
        "payload_chars": record.get("payload_chars"),
        "elapsed_s": record.get("elapsed_s"),
        "error_type": record.get("error_type"),
        "error": record.get("error"),
        "raw_response_preview": record.get("raw_response_preview"),
        "redaction_status": record.get("redaction_status"),
    }
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    if result:
        candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
        compact["result_summary"] = {
            "stage": result.get("stage"),
            "decision": result.get("decision"),
            "judgment": result.get("judgment"),
            "why": result.get("why"),
            "next_action": result.get("next_action"),
            "next_stage": (result.get("stage_transition") or {}).get("next_stage") if isinstance(result.get("stage_transition"), dict) else None,
            "candidate_count": len(candidates),
            "candidate_ids": [item.get("candidate_id") for item in candidates[:8] if isinstance(item, dict)],
            "candidate_expressions": [item.get("expression") for item in candidates[:8] if isinstance(item, dict)],
            "economic_thesis_count": len(result.get("economic_theses") or result.get("economic_thesis") or []),
            "thesis_count": len(result.get("theses") or []),
            "hypothesis_count": len(result.get("hypotheses") or []),
            "candidate_decision_count": len(result.get("candidate_decisions") or []),
            "code_advice_alignment": result.get("code_advice_alignment"),
            "failure_summary": result.get("failure_summary"),
            "mutation_actions": result.get("mutation_actions"),
            "next_thesis_policy": result.get("next_thesis_policy"),
            "knowledge_note_title": (result.get("knowledge_note") or {}).get("title")
            if isinstance(result.get("knowledge_note"), dict)
            else None,
        }
    if include_payload:
        compact["system_prompt"] = record.get("system_prompt")
        compact["user_prompt"] = record.get("user_prompt")
        compact["payload"] = record.get("payload")
        compact["result"] = result or record.get("result")
    return {key: _jsonable(value) for key, value in compact.items() if value not in (None, "", [], {})}


def factor_research_orchestrator_traces(
    *,
    run_id: str | None = None,
    limit: int = 50,
    include_payload: bool = False,
    include_history: bool = True,
) -> ServiceResult:
    """Read DeepSeek request/response traces for Orchestrator mode.

    This is a read-only observability surface.  It never starts research and it
    does not write event/research_step records, so codex_mcp production runs
    remain isolated from Orchestrator debugging.
    """
    max_items = min(200, max(1, int(limit or 50)))
    selected_run_id = (run_id or "").strip() or None
    if not _journal_paths(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE, FACTOR_ORCHESTRATOR_LLM_TRACES_HISTORY_DIR):
        return ok_result(
            inputs={"run_id": selected_run_id, "limit": max_items, "include_payload": include_payload},
            outputs={"traces": [], "count": 0, "trace_file_exists": False},
            artifacts={"orchestrator_llm_trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE)},
        )
    records, metrics = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_LLM_TRACES_FILE,
        history_dir=FACTOR_ORCHESTRATOR_LLM_TRACES_HISTORY_DIR,
        run_id=selected_run_id,
        limit=max_items,
        max_lines_per_file=max(1000, max_items * 20),
        max_bytes_per_file=FACTOR_ORCHESTRATOR_LLM_TRACES_MAX_BYTES,
        include_history=include_history,
    )
    traces = [_compact_orchestrator_llm_trace(record, include_payload=include_payload) for record in records]
    traces.reverse()
    return ok_result(
        inputs={"run_id": selected_run_id, "limit": max_items, "include_payload": include_payload},
        outputs={
            "traces": traces,
            "count": len(traces),
            "scanned_lines": metrics["scanned_lines"],
            "tail_read": not bool(selected_run_id),
            "history_reverse_read": bool(selected_run_id and metrics["history_files_scanned"]),
            "parse_errors": metrics["parse_errors"],
            "history_files_scanned": metrics["history_files_scanned"],
            "source_files": metrics["source_files"],
            "trace_file_exists": FACTOR_ORCHESTRATOR_LLM_TRACES_FILE.exists(),
        },
        artifacts={"orchestrator_llm_trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE)},
    )


def _tool_intents_from_steps(steps: list[dict]) -> list[dict]:
    """Project pending ORCH tool work from the canonical process log only."""
    intents: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        refs = step.get("evidence_refs") if isinstance(step.get("evidence_refs"), list) else []
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("type") != "orchestrator_tool_intent":
                continue
            tool = str(ref.get("tool") or "")
            candidate_id = str(ref.get("candidate_id") or "")
            task_id = str(ref.get("task_id") or "")
            key = (tool, candidate_id, task_id)
            if not tool or key in seen:
                continue
            seen.add(key)
            intents.append(
                {
                    "run_id": step.get("run_id"),
                    "round_id": step.get("round_id"),
                    "stage": step.get("stage"),
                    "ts": step.get("ts"),
                    "tool": tool,
                    "candidate_id": candidate_id,
                    "task_id": task_id,
                    "idempotency_key": ref.get("idempotency_key"),
                    "policy": ref.get("policy"),
                }
            )
    return intents[:40]


def factor_research_run_view(*, run_id: str, limit: int = 120, include_history: bool = True) -> ServiceResult:
    """Return a read-only, run-scoped production research projection.

    This is deliberately a projection, not a new workflow state or manifest.
    Its four sources remain the existing process log, ORCH event journal, LLM
    trace journal, and QuantGPT task store.
    """
    selected_run_id = str(run_id or "").strip()
    if not selected_run_id:
        return err_result("run_id_required", inputs={"run_id": run_id, "limit": limit})
    max_items = min(300, max(1, int(limit or 120)))
    steps = _read_recent_research_steps(limit=max_items, run_id=selected_run_id)
    events_result = factor_research_orchestrator_events(
        run_id=selected_run_id,
        limit=max_items,
        include_payload=False,
        include_history=include_history,
    ).to_dict()
    traces_result = factor_research_orchestrator_traces(
        run_id=selected_run_id,
        limit=max_items,
        include_payload=False,
        include_history=include_history,
    ).to_dict()
    task_ids = _research_step_task_ids(steps, run_id=selected_run_id, limit=1000)
    tasks = _fetch_quantgpt_tasks_by_ids(task_ids, limit=1000)
    stale_quantgpt_tasks = _quantgpt_stale_task_summary(_fetch_quantgpt_running_tasks())
    task_summary = _quantgpt_summary_for_research_state(_quantgpt_task_summary(tasks), stale_quantgpt_tasks)
    intents = _tool_intents_from_steps(steps)
    latest_step = steps[0] if steps else {}
    latest_event_rows = (events_result.get("outputs") or {}).get("events") or []
    latest_event = latest_event_rows[-1] if latest_event_rows else {}
    latest_trace_rows = (traces_result.get("outputs") or {}).get("traces") or []
    return ok_result(
        inputs={"run_id": selected_run_id, "limit": max_items},
        outputs={
            "run_id": selected_run_id,
            "source_roles": {
                "research_steps": "authoritative_process_log",
                "orchestrator_events": "controller_event_journal",
                "llm_traces": "model_request_response_trace",
                "quantgpt_tasks": "tool_evidence_store",
            },
            "latest_step": _compact_research_step_for_live(latest_step) if latest_step else {},
            "latest_event": latest_event,
            "latest_trace": latest_trace_rows[-1] if latest_trace_rows else {},
            "research_steps": [_compact_research_step_for_live(step) for step in steps],
            "events": latest_event_rows,
            "traces": latest_trace_rows,
            "tool_intents": intents,
            "quantgpt_tasks": [_compact_task_for_live(task) for task in tasks],
            "quantgpt_task_summary": task_summary,
            "stale_quantgpt_tasks": stale_quantgpt_tasks,
        },
        artifacts={
            "research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE),
            "orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE),
            "orchestrator_llm_trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
            "quantgpt_db": str(QUANTGPT_DB),
        },
    )


def _run_view_for_runtime(runtime_view: dict | None, *, limit: int = 120) -> dict:
    """Attach the existing run projection to a console response when available."""
    runtime = runtime_view if isinstance(runtime_view, dict) else {}
    run_id = str(runtime.get("run_id") or "").strip()
    if not run_id:
        return {}
    result = factor_research_run_view(run_id=run_id, limit=limit, include_history=False).to_dict()
    if not result.get("ok"):
        return {}
    outputs = result.get("outputs") if isinstance(result.get("outputs"), dict) else {}
    return _jsonable(outputs)


def _complete_orchestrator_llm_json(
    *,
    client: DeepSeekJSONClient,
    run_id: str,
    round_id: str,
    stage: str,
    checkpoint: str,
    system: str,
    payload: dict,
    temperature: float = 0.15,
    max_tokens: int = 1800,
) -> dict:
    _raise_if_orchestrator_stop_requested(run_id)
    trace_id = f"{round_id}:{checkpoint}:{uuid.uuid4().hex[:8]}"
    visible_payload = _llm_visible_payload(payload)
    context_budget = _prompt_context_budget(payload)
    payload_chars = len(json.dumps(visible_payload, ensure_ascii=False, default=str))
    budget_state = _charge_orchestrator_llm_budget(run_id, payload_chars=payload_chars)
    prompt_digest = _orchestrator_prompt_digest(payload)
    _write_orchestrator_llm_trace(
        {
            "trace_id": trace_id,
            "run_id": run_id,
            "round_id": round_id,
            "stage": stage,
            "checkpoint": checkpoint,
            "event_type": "llm_request",
            "llm_provider": LLM_PROVIDER,
            "llm_model": client.preferred_model(),
            "llm_model_order": list(getattr(client, "model_order", lambda: [client.model])()),
            "llm_base_url": client.base_url,
            "timeout_s": client.timeout,
            "system_prompt": system,
            "payload": visible_payload,
            "payload_chars": payload_chars,
            "run_budget": budget_state,
            "context_budget": context_budget,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    )
    _write_orchestrator_llm_request_step(
        run_id=run_id,
        round_id=round_id,
        stage=stage,
        checkpoint=checkpoint,
        trace_id=trace_id,
        payload_chars=payload_chars,
        llm_model=client.preferred_model(),
        prompt_digest=prompt_digest,
    )
    started = time.time()
    try:
        result = client.complete_json(
            system=system,
            payload=visible_payload,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raw_preview = getattr(exc, "raw_preview", None)
        try:
            setattr(exc, "orchestrator_llm_trace_id", trace_id)
            setattr(exc, "orchestrator_llm_trace_file", str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE))
            setattr(exc, "orchestrator_llm_payload_chars", payload_chars)
        except Exception:
            pass
        _write_orchestrator_llm_trace(
            {
                "trace_id": trace_id,
                "run_id": run_id,
                "round_id": round_id,
                "stage": stage,
                "checkpoint": checkpoint,
                "event_type": "llm_error",
                "elapsed_s": round(time.time() - started, 3),
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:1000],
                "raw_response_preview": str(raw_preview)[:1200] if raw_preview else None,
            }
        )
        raise
    except BaseException as exc:
        try:
            setattr(exc, "orchestrator_llm_trace_id", trace_id)
            setattr(exc, "orchestrator_llm_trace_file", str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE))
            setattr(exc, "orchestrator_llm_payload_chars", payload_chars)
        except Exception:
            pass
        _write_orchestrator_llm_trace(
            {
                "trace_id": trace_id,
                "run_id": run_id,
                "round_id": round_id,
                "stage": stage,
                "checkpoint": checkpoint,
                "event_type": "llm_error",
                "elapsed_s": round(time.time() - started, 3),
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:1000] or exc.__class__.__name__,
            }
        )
        raise
    _write_orchestrator_llm_trace(
        {
            "trace_id": trace_id,
            "run_id": run_id,
            "round_id": round_id,
            "stage": stage,
            "checkpoint": checkpoint,
            "event_type": "llm_result",
            "llm_provider": LLM_PROVIDER,
            "llm_model": result.get("_orchestrator_llm_model") if isinstance(result, dict) else client.preferred_model(),
            "llm_model_order": result.get("_orchestrator_llm_model_order") if isinstance(result, dict) else list(client.model_order()),
            "elapsed_s": round(time.time() - started, 3),
            "usage": result.get("_orchestrator_llm_usage") if isinstance(result, dict) else None,
            "result": _sanitize_llm_payload(result),
        }
    )
    if isinstance(result, dict):
        result = dict(result)
        result["_orchestrator_llm_trace_id"] = trace_id
    return result


def _orchestrator_payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            return {"raw": text}
    return {}


def _orchestrator_tool_result_payload(result: ServiceResult | dict | str) -> dict:
    payload = result.to_dict() if isinstance(result, ServiceResult) else _orchestrator_payload(result)
    if not isinstance(payload, dict):
        return {}
    return payload.get("outputs", payload) if isinstance(payload.get("outputs", payload), dict) else payload


def _orchestrator_imported_count_and_items(import_payload: dict, import_candidates: list[dict] | None = None) -> tuple[int, list[dict]]:
    """Normalize import tool outputs across count-based and legacy list shapes."""
    if not isinstance(import_payload, dict):
        return 0, []
    raw_imported = import_payload.get("imported")
    details = import_payload.get("details")
    if isinstance(raw_imported, list):
        return len(raw_imported), raw_imported
    if isinstance(raw_imported, int):
        if isinstance(details, list) and details:
            return raw_imported, details
        return raw_imported, list(import_candidates or [])[:raw_imported]
    if isinstance(details, list):
        imported_details = [item for item in details if isinstance(item, dict) and item.get("factor_id")]
        return len(imported_details), imported_details
    return 0, []


def _orchestrator_import_event_summary(
    *,
    import_ok: bool,
    imported_count: int,
    requested_count: int,
    adopted_total: int,
    import_sync_status: dict | None,
) -> str:
    if not import_ok:
        return f"自动 import 失败，imported={imported_count}/{requested_count}，等待修复重试。"
    sync = import_sync_status if isinstance(import_sync_status, dict) else {}
    return (
        f"自动 import 完成，registry_imported={imported_count}，"
        f"active_values={sync.get('active_values', 'unknown')}，"
        f"model_snapshot={sync.get('model_snapshot', 'unknown')}，累计 imported={adopted_total}。"
    )


def _run_import_factors_isolated(
    *,
    candidates: list[dict],
    universe: str,
    start_date: str,
    end_date: str,
    selection_start_date: str,
    selection_end_date: str,
    category: str = "",
    submit_wq: bool = False,
    timeout_s: int | None = None,
) -> dict:
    """Run the heavy parquet/registry import in a fresh Python process."""
    timeout = int(timeout_s or FACTOR_ORCHESTRATOR_TOOL_TIMEOUT_DEFAULT)
    timeout = max(60, min(timeout, FACTOR_ORCHESTRATOR_TOOL_TIMEOUT_MAX))
    repo_root = Path(__file__).resolve().parents[1]
    payload = {
        "candidates": candidates or [],
        "universe": universe,
        "start_date": start_date,
        "end_date": end_date,
        "selection_start_date": selection_start_date,
        "selection_end_date": selection_end_date,
        "category": category,
        "submit_wq": submit_wq,
    }
    runner = r"""
import json
import sys
import traceback
from pathlib import Path

repo_root = Path.cwd()
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from domain.factor_research.auto_import import import_factors

input_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
payload = json.loads(input_path.read_text(encoding="utf-8"))
try:
    result = import_factors(
        payload.get("candidates") or [],
        universe=payload.get("universe") or FACTOR_DEFAULT_UNIVERSE,
        start_date=payload.get("start_date"),
        end_date=payload.get("end_date"),
        selection_start_date=payload.get("selection_start_date"),
        selection_end_date=payload.get("selection_end_date"),
        category=payload.get("category") or "",
        submit_wq=bool(payload.get("submit_wq")),
    )
    output_path.write_text(
        json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
except Exception as exc:
    output_path.write_text(
        json.dumps(
            {
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc()[-4000:],
            },
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    raise
"""
    with tempfile.TemporaryDirectory(prefix="fxalpha_import_") as tmp:
        tmp_dir = Path(tmp)
        input_path = tmp_dir / "import_payload.json"
        output_path = tmp_dir / "import_result.json"
        runner_path = tmp_dir / "import_runner.py"
        stdout_path = tmp_dir / "import_stdout.log"
        stderr_path = tmp_dir / "import_stderr.log"
        input_path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        runner_path.write_text(runner, encoding="utf-8")
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(repo_root) if not existing_pythonpath else f"{repo_root}:{existing_pythonpath}"

        def _tail_text(path: Path, limit: int = 1200) -> str:
            try:
                return path.read_text(encoding="utf-8", errors="replace")[-limit:]
            except Exception:
                return ""

        try:
            with stdout_path.open("w", encoding="utf-8") as stdout_fh, stderr_path.open("w", encoding="utf-8") as stderr_fh:
                systemd_run = shutil.which("systemd-run")
                import_systemd_disabled = os.environ.get("FXALPHA_IMPORT_DISABLE_SYSTEMD_RUN", "").lower() in {"1", "true", "yes"}
                if not import_systemd_disabled and _can_use_user_systemd_run(systemd_run, env):
                    unit = f"fxalpha-import-{uuid.uuid4().hex[:12]}"
                    args = [
                        systemd_run,
                        "--user",
                        "--wait",
                        "--collect",
                        "--quiet",
                        f"--unit={unit}",
                        f"--property=WorkingDirectory={repo_root}",
                        f"--setenv=PYTHONPATH={env['PYTHONPATH']}",
                        sys.executable,
                        str(runner_path),
                        str(input_path),
                        str(output_path),
                    ]
                    execution = "systemd_transient_unit"
                else:
                    args = [sys.executable, str(runner_path), str(input_path), str(output_path)]
                    execution = "direct_subprocess"
                completed = subprocess.run(
                    args,
                    cwd=str(repo_root),
                    env=env,
                    text=True,
                    stdout=stdout_fh,
                    stderr=stderr_fh,
                    timeout=timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            return {
                "imported": 0,
                "skipped": len(candidates or []),
                "errors": [f"isolated_import_timeout_after_{timeout}s"],
                "details": [
                    {
                        "status": "isolated_import_timeout",
                        "execution": locals().get("execution", "unknown"),
                        "timeout_s": timeout,
                        "stdout_tail": _tail_text(stdout_path),
                        "stderr_tail": _tail_text(stderr_path),
                    }
                ],
                "submit_wq": submit_wq,
            }
        result_payload: dict = {}
        if output_path.exists():
            try:
                result_payload = json.loads(output_path.read_text(encoding="utf-8"))
            except Exception as exc:
                result_payload = {"ok": False, "error": f"import_result_json_parse_failed:{exc}"}
        if completed.returncode != 0 or result_payload.get("ok") is not True:
            error = result_payload.get("error") or f"isolated_import_exit_{completed.returncode}"
            return {
                "imported": 0,
                "skipped": len(candidates or []),
                "errors": [str(error)],
                "details": [
                    {
                        "status": "isolated_import_failed",
                        "execution": locals().get("execution", "unknown"),
                        "returncode": completed.returncode,
                        "error_type": result_payload.get("error_type"),
                        "traceback": result_payload.get("traceback"),
                        "stdout_tail": _tail_text(stdout_path),
                        "stderr_tail": _tail_text(stderr_path),
                    }
                ],
                "submit_wq": submit_wq,
            }
        result = result_payload.get("result")
        return result if isinstance(result, dict) else {"imported": 0, "skipped": 0, "errors": ["isolated_import_missing_result"], "details": []}


def _orchestrator_mcp_server():
    root = str(QUANTGPT_CODE_ROOT)
    if root and root not in sys.path:
        sys.path.insert(0, root)
    db_url = f"sqlite+aiosqlite:///{QUANTGPT_DB.resolve()}"
    if not os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL") == "sqlite+aiosqlite:///./quantgpt.db":
        os.environ["DATABASE_URL"] = db_url
    os.environ.setdefault("QUANTGPT_TASK_BACKEND", "thread")
    if os.environ.get("QUANTGPT_TASK_BACKEND", "").lower() == "thread":
        try:
            from quantgpt import task_executor

            executor = getattr(task_executor, "_executor", None)
            if executor is not None and getattr(executor, "is_process_based", False):
                task_executor.shutdown_executor()
        except Exception:
            pass
    from quantgpt import mcp_server

    return mcp_server


def _orchestrator_tool_intent(*, tool: str, candidate: dict, contract: dict) -> dict[str, str]:
    """Build a stable identity for one production ORCH tool attempt.

    The identity is intentionally derived only from the research contract and
    candidate expression.  It is safe to persist in the existing event stream:
    QuantGPT uses the key to recover *completed* work, while failed work is
    executed again under the same logical attempt.
    """
    expression = str((candidate or {}).get("expression") or "").strip()
    candidate_id = str((candidate or {}).get("candidate_id") or "").strip()
    material = {
        "schema": "fxalpha_orchestrator_tool_intent_v1",
        "tool": str(tool or "").strip(),
        "run_id": str((contract or {}).get("run_id") or "").strip(),
        "round_id": str((contract or {}).get("round_id") or "").strip(),
        "candidate_id": candidate_id,
        "expression": expression,
        "universe": (contract or {}).get("universe"),
        "selection_start_date": (contract or {}).get("selection_start_date"),
        "selection_end_date": (contract or {}).get("selection_end_date"),
        "holding_period": (contract or {}).get("holding_period"),
        "benchmark": (contract or {}).get("benchmark"),
        "top_frac": (contract or {}).get("top_frac"),
        "cost_rate": (contract or {}).get("cost_rate"),
        "rebalance_anchor": (contract or {}).get("rebalance_anchor"),
        "neutralize_cap": bool((contract or {}).get("neutralize_cap")),
        "neutralize_industry": bool((contract or {}).get("neutralize_industry")),
    }
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    idempotency_key = f"fxalpha-orch-v1:{digest}"
    # This mirrors QuantGPT's deterministic_mcp_task_id without importing the
    # external package at service import time.
    task_type = "score" if tool == "score_factor" else "backtest"
    task_id = hashlib.sha256(f"{task_type}:{idempotency_key}".encode("utf-8")).hexdigest()[:12]
    return {
        "type": "orchestrator_tool_intent",
        "tool": str(tool or ""),
        "candidate_id": candidate_id,
        "idempotency_key": idempotency_key,
        "task_id": task_id,
        "policy": "completed_task_may_recover_failed_task_must_rerun",
    }


def _orchestrator_recovery_checkpoint(
    *,
    round_id: str,
    stage: str,
    thesis: dict | None,
    hypothesis: dict | None,
    candidates: list[dict],
    planned_candidates: list[dict],
    candidate_plan: dict | None,
    candidate_precheck: list[dict],
    resume_stage: str = "",
    stage_candidates: list[dict] | None = None,
    completed_task_refs: list[dict] | None = None,
) -> dict[str, Any]:
    """Persist only the minimum graph needed to replay a tool stage safely."""
    return _jsonable(
        {
            "type": "orchestrator_recovery_checkpoint",
            "schema_version": "factor_orchestrator_recovery_v1",
            "round_id": str(round_id or ""),
            "stage": str(stage or ""),
            "thesis": dict(thesis or {}),
            "hypothesis": dict(hypothesis or {}),
            "candidates": [dict(item) for item in candidates if isinstance(item, dict)][:12],
            "planned_candidates": [dict(item) for item in planned_candidates if isinstance(item, dict)][:12],
            "candidate_plan": dict(candidate_plan or {}),
            "candidate_precheck": [dict(item) for item in candidate_precheck if isinstance(item, dict)][:24],
            "resume_stage": str(resume_stage or stage or ""),
            "stage_candidates": [
                _compact_candidate_lane_for_step(item)
                for item in (stage_candidates or [])
                if isinstance(item, dict)
            ][:12],
            "completed_task_refs": [
                _jsonable(item)
                for item in (completed_task_refs or [])
                if isinstance(item, dict)
            ][:24],
            "policy": "process_recovery_replays_existing_candidates_no_llm_redesign",
        }
    )


def _latest_orchestrator_recovery_checkpoint(run_id: str) -> dict:
    """Read the latest durable checkpoint for one run without writing state."""
    run_text = str(run_id or "").strip()
    if not run_text:
        return {}
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        run_id=run_text,
        limit=1200,
    )
    for event in records:
        for ref in event.get("evidence_refs") or []:
            if isinstance(ref, dict) and ref.get("type") == "orchestrator_recovery_checkpoint":
                return dict(ref)
    return {}


async def _await_orchestrator_tool(coro, *, timeout_s: float) -> Any:
    return await asyncio.wait_for(coro, timeout=float(timeout_s))


def _run_async_tool(coro, *, timeout_s: float | None = None) -> Any:
    timeout = max(
        0.001,
        min(
            float(FACTOR_ORCHESTRATOR_TOOL_TIMEOUT_MAX),
            float(timeout_s or FACTOR_ORCHESTRATOR_TOOL_TIMEOUT_DEFAULT),
        ),
    )
    try:
        return asyncio.run(_await_orchestrator_tool(coro, timeout_s=timeout))
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"orchestrator_tool_timeout_after_{int(timeout)}s") from exc


def _can_use_user_systemd_run(systemd_run: str | None, env: dict[str, str]) -> bool:
    if not systemd_run:
        return False
    if os.environ.get("FXALPHA_ORCH_DISABLE_SYSTEMD_TOOL_WORKER", "").lower() in {"1", "true", "yes"}:
        return False
    # GUI/API processes launched outside the user session may have systemd-run
    # on PATH but no user bus; in that state systemd-run --user exits before
    # the worker starts and produces no tool result.
    return bool(env.get("DBUS_SESSION_BUS_ADDRESS") and env.get("XDG_RUNTIME_DIR"))


def _run_orchestrator_candidate_worker(
    *,
    tool: str,
    candidate: dict,
    contract: dict,
    timeout_s: int | None = None,
) -> dict:
    """Run heavy QuantGPT MCP candidate tools outside the API process."""
    timeout = int(timeout_s or FACTOR_ORCHESTRATOR_TOOL_TIMEOUT_DEFAULT)
    timeout = max(60, min(timeout, FACTOR_ORCHESTRATOR_TOOL_TIMEOUT_MAX))
    repo_root = Path(__file__).resolve().parents[1]
    payload = {"tool": tool, "candidate": candidate or {}, "contract": contract or {}}
    runner = r"""
import json
import sys
import traceback
from pathlib import Path

repo_root = Path.cwd()
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from services.factor_research_service import (
    _deep_validate_candidate_with_mcp,
    _score_candidate_with_mcp,
)

input_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
payload = json.loads(input_path.read_text(encoding="utf-8"))
try:
    tool = payload.get("tool")
    candidate = payload.get("candidate") or {}
    contract = payload.get("contract") or {}
    if tool == "score_factor":
        result = _score_candidate_with_mcp(candidate, contract=contract)
    elif tool == "deep_validation":
        result = _deep_validate_candidate_with_mcp(candidate, contract=contract)
    else:
        raise ValueError(f"unsupported_orchestrator_tool:{tool}")
    output_path.write_text(
        json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
except Exception as exc:
    output_path.write_text(
        json.dumps(
            {
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc()[-4000:],
            },
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    raise
"""
    with tempfile.TemporaryDirectory(prefix=f"fxalpha_{tool}_") as tmp:
        tmp_dir = Path(tmp)
        input_path = tmp_dir / "tool_payload.json"
        output_path = tmp_dir / "tool_result.json"
        runner_path = tmp_dir / "tool_runner.py"
        stdout_path = tmp_dir / "tool_stdout.log"
        stderr_path = tmp_dir / "tool_stderr.log"
        input_path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        runner_path.write_text(runner, encoding="utf-8")
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(repo_root) if not existing_pythonpath else f"{repo_root}:{existing_pythonpath}"

        def _tail_text(path: Path, limit: int = 1200) -> str:
            try:
                return path.read_text(encoding="utf-8", errors="replace")[-limit:]
            except Exception:
                return ""

        def _run_worker_process(*, use_systemd: bool) -> tuple[subprocess.CompletedProcess[str], str]:
            output_path.unlink(missing_ok=True)
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            with stdout_path.open("w", encoding="utf-8") as stdout_fh, stderr_path.open("w", encoding="utf-8") as stderr_fh:
                if use_systemd:
                    unit = f"fxalpha-{tool.replace('_', '-')}-{uuid.uuid4().hex[:12]}"
                    args = [
                        str(systemd_run),
                        "--user",
                        "--wait",
                        "--collect",
                        "--quiet",
                        f"--unit={unit}",
                        f"--property=WorkingDirectory={repo_root}",
                        "--property=MemoryAccounting=yes",
                        f"--property=MemoryMax={FACTOR_ORCHESTRATOR_TOOL_WORKER_MEMORY_MAX}",
                        f"--setenv=PYTHONPATH={env['PYTHONPATH']}",
                        sys.executable,
                        str(runner_path),
                        str(input_path),
                        str(output_path),
                    ]
                    execution_mode = "systemd_transient_unit"
                else:
                    args = [sys.executable, str(runner_path), str(input_path), str(output_path)]
                    execution_mode = "direct_subprocess"
                completed_process = subprocess.run(
                    args,
                    cwd=str(repo_root),
                    env=env,
                    text=True,
                    stdout=stdout_fh,
                    stderr=stderr_fh,
                    timeout=timeout,
                    check=False,
                )
            return completed_process, execution_mode

        systemd_run = shutil.which("systemd-run")
        use_systemd_worker = _can_use_user_systemd_run(systemd_run, env)
        try:
            completed, execution = _run_worker_process(use_systemd=use_systemd_worker)
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"{tool}_timeout_after_{timeout}s",
                "error_type": "TimeoutExpired",
                "execution": locals().get("execution", "unknown"),
                "stdout_tail": _tail_text(stdout_path),
                "stderr_tail": _tail_text(stderr_path),
            }
        result_payload: dict = {}
        if output_path.exists():
            try:
                result_payload = json.loads(output_path.read_text(encoding="utf-8"))
            except Exception as exc:
                result_payload = {"ok": False, "error": f"{tool}_result_json_parse_failed:{exc}"}
        if completed.returncode != 0 or result_payload.get("ok") is not True:
            return {
                "ok": False,
                "error": result_payload.get("error") or f"{tool}_exit_{completed.returncode}",
                "error_type": result_payload.get("error_type") or ("SystemdWorkerExit" if use_systemd_worker else None),
                "traceback": result_payload.get("traceback"),
                "execution": locals().get("execution", "unknown"),
                "returncode": completed.returncode,
                "stdout_tail": _tail_text(stdout_path),
                "stderr_tail": _tail_text(stderr_path),
            }
        return {
            "ok": True,
            "execution": locals().get("execution", "unknown"),
            "result": result_payload.get("result"),
        }


def _orchestrator_stage_event(
    *,
    run_id: str,
    round_id: str,
    stage_seq: int,
    stage: str,
    previous_stage: str,
    previous_stage_id: str,
    summary: str,
    decision: str,
    next_stage: str,
    next_action: str,
    event_type: str = "checkpoint",
    evidence_refs: list[dict] | None = None,
    tags: list[str] | None = None,
    priority: str = "normal",
    stage_id_suffix: str | None = None,
    sync_research_step: bool = True,
    **extra: Any,
) -> dict:
    stage_id = f"{round_id}:s{stage_seq:02d}_{stage}"
    clean_suffix = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(stage_id_suffix or "").strip()).strip("_")
    if clean_suffix:
        stage_id = f"{stage_id}:{clean_suffix[:80]}"
    facts = extra.pop("facts", "")
    judgment = extra.pop("judgment", summary)
    why = extra.pop("why", decision)
    history_used = extra.pop("history_used", "")
    heartbeat_status = str(extra.pop("heartbeat_status", "alive") or "alive")
    payload = {
        "schema_version": "orchestrator_event_v1",
        "run_id": run_id,
        "round_id": round_id,
        "stage_seq": stage_seq,
        "stage_id": stage_id,
        "previous_stage": previous_stage,
        "previous_stage_id": previous_stage_id,
        "stage": stage,
        "summary": summary,
        "decision": decision,
        "priority": priority,
        "stage_transition": {
            "next_stage": next_stage,
            "next_action": next_action,
            "judgment": _clip_text(judgment, 700),
            "why": _clip_text(why, 700),
            "facts": _clip_text(facts, 1800),
            "history_used": _clip_text(history_used, 900),
            "mode": "orchestrator",
        },
        "evidence_refs": evidence_refs or [],
        "tags": ["orchestrator", *(tags or []), stage],
        "event_type": event_type,
        "checkpoint": stage,
        "heartbeat_status": heartbeat_status,
        "thread_id": str(threading.get_ident()),
    }
    payload.update(extra)
    return _write_orchestrator_event(payload, sync_research_step=sync_research_step)


def _orchestrator_set_job(run_id: str, *, status: str | None = None, stage: str | None = None, event: dict | None = None, summary: dict | None = None) -> None:
    with _GUI_RUNS_LOCK:
        job = _GUI_RUNS.get(run_id)
        if not job:
            return
        if status:
            job["status"] = status
        if stage:
            job["stage"] = stage
        if summary:
            job["summary"] = summary
        if event:
            _append_job_event(job, event)
        _persist_job(job)


def _latest_orchestrator_control_request(run_id: str) -> dict[str, Any]:
    run_text = str(run_id or "").strip()
    if not run_text:
        return {}
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        run_id=run_text,
        limit=240,
        max_lines_per_file=600,
        include_history=False,
    )
    for event in records:
        if str(event.get("event_type") or "").strip().lower() != "operator_control":
            continue
        action = str(event.get("control_action") or "").strip().lower()
        if action in {"pause", "resume", "stop"}:
            return event
    return {}


def _write_orchestrator_control_request(*, run_id: str, action: str, reason: str = "") -> dict[str, Any]:
    action_text = str(action or "").strip().lower()
    if action_text not in {"pause", "resume", "stop"}:
        raise ValueError(f"unsupported_orchestrator_control_action:{action_text}")
    run_text = str(run_id or "").strip()
    request_id = f"ctl_{uuid.uuid4().hex[:12]}"
    return _write_orchestrator_event(
        {
            "schema_version": "orchestrator_control_v1",
            "run_id": run_text,
            "round_id": f"{run_text}:control",
            "stage_seq": 0,
            "stage_id": f"{run_text}:control:{request_id}",
            "stage": "operator_control",
            "event_type": "operator_control",
            "control_action": action_text,
            "control_request_id": request_id,
            "reason": str(reason or f"operator_requested_{action_text}").strip(),
            "summary": f"Operator requested Orchestrator {action_text}.",
            "decision": f"operator_{action_text}_requested",
            "stage_transition": {
                "next_stage": "checkpoint_stop" if action_text in {"pause", "stop"} else "protocol_load",
                "next_action": f"{action_text}_at_safe_checkpoint" if action_text in {"pause", "stop"} else "resume_from_durable_checkpoint",
                "mode": "orchestrator",
            },
            "tags": ["orchestrator", "operator_control", f"operator_{action_text}"],
            "heartbeat_status": "stopping" if action_text in {"pause", "stop"} else "starting",
        },
        sync_research_step=False,
    )


def _orchestrator_control_request(run_id: str) -> dict[str, Any]:
    run_text = str(run_id or "").strip()
    with _GUI_RUNS_LOCK:
        job = _GUI_RUNS.get(run_text)
        if job and (job.get("control_action") in {"pause", "stop"} or job.get("stop_requested")):
            return {
                "control_action": str(job.get("control_action") or "stop"),
                "control_request_id": str(job.get("control_request_id") or ""),
            }
    return _latest_orchestrator_control_request(run_text)


def _raise_if_orchestrator_stop_requested(run_id: str) -> None:
    request = _orchestrator_control_request(run_id)
    action = str(request.get("control_action") or "").strip().lower()
    if action in {"pause", "stop"}:
        raise OrchestratorStopRequested(action, str(request.get("control_request_id") or ""))


def _latest_operator_pause_event(run_id: str) -> dict[str, Any]:
    run_text = str(run_id or "").strip()
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        run_id=run_text or None,
        limit=500,
        max_lines_per_file=800,
    )
    for event in records:
        if str(event.get("event_type") or "").strip().lower() == "operator_control":
            later_action = str(event.get("control_action") or "").strip().lower()
            if later_action in {"resume", "stop"}:
                return {}
        tags = {str(tag).strip().lower() for tag in event.get("tags") or []}
        if str(event.get("stage") or "") != "checkpoint_stop":
            continue
        if "operator_pause" not in tags:
            return {}
        recovered = dict(event)
        checkpoint = _latest_orchestrator_recovery_checkpoint(run_text)
        if checkpoint:
            recovered["_recovery_checkpoint"] = checkpoint
        return recovered
    return {}


def _write_orchestrator_launch_event(*, run_id: str, inputs: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    return _write_orchestrator_event(
        {
            "schema_version": "orchestrator_control_v1",
            "run_id": str(run_id or "").strip(),
            "round_id": f"{str(run_id or '').strip()}:control",
            "stage_seq": 0,
            "stage_id": f"{str(run_id or '').strip()}:control:launch_{uuid.uuid4().hex[:10]}",
            "stage": "orchestrator_launch",
            "event_type": "orchestrator_launch",
            "summary": "Orchestrator launch specification accepted.",
            "decision": "launch_or_resume_worker",
            "inputs": _jsonable(inputs),
            "research_contract": _jsonable(contract),
            "stage_transition": {"next_stage": "protocol_load", "next_action": "start_orchestrator_worker", "mode": "orchestrator"},
            "tags": ["orchestrator", "operator_control", "orchestrator_launch"],
            "heartbeat_status": "starting",
        },
        sync_research_step=False,
    )


def _latest_orchestrator_launch_spec(run_id: str) -> dict[str, Any]:
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        run_id=str(run_id or "").strip() or None,
        limit=800,
    )
    for event in records:
        if str(event.get("event_type") or "") == "orchestrator_launch":
            return event
    return {}


def _write_orchestrator_worker_event(
    *,
    run_id: str,
    action: str,
    unit: str = "",
    pid: int | None = None,
    mode: str = "process",
) -> dict[str, Any]:
    action_text = str(action or "").strip().lower()
    return _write_orchestrator_event(
        {
            "schema_version": "orchestrator_control_v1",
            "run_id": str(run_id or "").strip(),
            "round_id": f"{str(run_id or '').strip()}:control",
            "stage_seq": 0,
            "stage_id": f"{str(run_id or '').strip()}:control:worker_{action_text}_{uuid.uuid4().hex[:8]}",
            "stage": "orchestrator_worker",
            "event_type": "orchestrator_worker",
            "worker_action": action_text,
            "worker_unit": str(unit or ""),
            "worker_pid": int(pid) if pid else None,
            "worker_mode": str(mode or "process"),
            "summary": f"Detached Orchestrator worker {action_text}.",
            "decision": f"worker_{action_text}",
            "stage_transition": {
                "next_stage": "protocol_load" if action_text in {"launch_requested", "started"} else "checkpoint_stop",
                "next_action": "run_orchestrator" if action_text in {"launch_requested", "started"} else "idle",
                "mode": "orchestrator",
            },
            "tags": ["orchestrator", "orchestrator_worker", f"worker_{action_text}"],
            "heartbeat_status": "running" if action_text in {"launch_requested", "started"} else "stopped",
        },
        sync_research_step=False,
    )


def _latest_orchestrator_worker_event(run_id: str) -> dict[str, Any]:
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        run_id=str(run_id or "").strip() or None,
        limit=120,
        max_lines_per_file=300,
        include_history=False,
    )
    for event in records:
        if str(event.get("event_type") or "") == "orchestrator_worker":
            return event
    return {}


def _charge_orchestrator_llm_budget(run_id: str, *, payload_chars: int) -> dict[str, int]:
    key = str(run_id or "")
    budget = _ORCHESTRATOR_LLM_BUDGETS.setdefault(key, {"requests": 0, "payload_chars": 0})
    budget["requests"] = int(budget.get("requests", 0)) + 1
    budget["payload_chars"] = int(budget.get("payload_chars", 0)) + max(0, int(payload_chars or 0))
    if budget["requests"] > FACTOR_ORCHESTRATOR_LLM_REQUEST_BUDGET:
        raise DeepSeekClientError(
            f"orchestrator_llm_request_budget_exceeded:{budget['requests']}>{FACTOR_ORCHESTRATOR_LLM_REQUEST_BUDGET}"
        )
    if budget["payload_chars"] > FACTOR_ORCHESTRATOR_LLM_PAYLOAD_CHAR_BUDGET:
        raise DeepSeekClientError(
            "orchestrator_llm_payload_budget_exceeded:"
            f"{budget['payload_chars']}>{FACTOR_ORCHESTRATOR_LLM_PAYLOAD_CHAR_BUDGET}"
        )
    return dict(budget)


def _orchestrator_round_event_budget_exceeded(round_events: list[dict]) -> bool:
    return len(round_events or []) > FACTOR_ORCHESTRATOR_EVENT_BUDGET


def _recent_orchestrator_anchors(*, limit: int = 8) -> list[dict]:
    if not FACTOR_ORCHESTRATOR_EVENTS_FILE.exists():
        return []
    lines = _tail_jsonl_lines(FACTOR_ORCHESTRATOR_EVENTS_FILE, max_lines=1500)
    anchors: list[dict] = []
    seen: set[str] = set()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        stage = str(event.get("stage") or "")
        if stage not in {"score_review", "novelty_review", "deep_validation_review"}:
            continue
        lanes = {
            str(item.get("candidate_id") or ""): item
            for item in _orchestrator_candidate_lane_items(event.get("candidate_lanes"), limit=24)
            if isinstance(item, dict)
        }
        for ref in event.get("evidence_refs") or []:
            if not isinstance(ref, dict):
                continue
            candidate_id = str(ref.get("candidate_id") or "")
            lane = lanes.get(candidate_id) or {}
            expression = str(lane.get("expression") or ref.get("expression") or "")
            if not expression:
                continue
            score = ref.get("score") or ref.get("quick_score")
            grade = str(ref.get("grade") or lane.get("grade") or "").upper()
            deep_score = ref.get("deep_score")
            is_quick_anchor = grade in {"A", "B"} or (isinstance(score, (int, float)) and score >= 70)
            is_deep_anchor = isinstance(deep_score, (int, float)) and deep_score >= 70
            if not is_quick_anchor and not is_deep_anchor:
                continue
            key = expression.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            anchors.append(
                {
                    "run_id": event.get("run_id"),
                    "round_id": event.get("round_id"),
                    "stage": stage,
                    "candidate_id": candidate_id or lane.get("candidate_id"),
                    "expression": expression[:420],
                    "economic_thesis": lane.get("economic_thesis"),
                    "hypothesis": lane.get("hypothesis"),
                    "score": score,
                    "grade": grade or None,
                    "deep_score": deep_score,
                    "deep_action": ref.get("deep_action"),
                    "summary": str(event.get("summary") or "")[:240],
                    "decision": str(event.get("decision") or "")[:240],
                }
            )
            if len(anchors) >= limit:
                return anchors
    return anchors


_EXPRESSION_NON_FIELD_TOKENS = {
    "abs",
    "clip",
    "delay",
    "exp",
    "group_rank",
    "group_zscore",
    "log",
    "max",
    "mean",
    "min",
    "power",
    "rank",
    "scale",
    "sigmoid",
    "sign",
    "sign_power",
    "sqrt",
    "sma",
    "tanh",
    "ts_argmax",
    "ts_argmin",
    "ts_av_diff",
    "ts_corr",
    "ts_cov",
    "ts_delta",
    "ts_max",
    "ts_mean",
    "ts_min",
    "ts_rank",
    "ts_std",
    "ts_sum",
    "ts_zscore",
    "wma",
    "where",
    "zscore",
}
_ORCHESTRATOR_GENERIC_PRICE_FIELDS = {"open", "high", "low", "close", "vwap"}
_ORCHESTRATOR_NON_NUMERIC_META_FIELDS = {"security_name", "list_status", "st_status", "list_date"}


def _expression_field_terms(expression: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(expression or ""))
    fields: list[str] = []
    for token in tokens:
        lowered = token.lower()
        factor_functions = globals().get("_FACTOR_EXPRESSION_FUNCTIONS", set())
        if lowered in _EXPRESSION_NON_FIELD_TOKENS or lowered in factor_functions:
            continue
        if lowered in {"true", "false", "nan", "inf"}:
            continue
        fields.append(lowered)
    return sorted(dict.fromkeys(fields))


def _recent_orchestrator_failure_feedback(*, limit_candidates: int = 5, limit_fields: int = 8) -> dict:
    if not FACTOR_ORCHESTRATOR_EVENTS_FILE.exists():
        return {}
    lines = _tail_jsonl_lines(FACTOR_ORCHESTRATOR_EVENTS_FILE, max_lines=1500)
    weak_candidates: list[dict] = []
    near_misses: list[dict] = []
    novelty_vetoes: list[dict] = []
    field_counts: dict[str, int] = {}
    seen_expr: set[str] = set()

    def add_field_counts(expression: str) -> list[str]:
        fields = _expression_field_terms(expression)
        for field in fields:
            if field in _ORCHESTRATOR_GENERIC_PRICE_FIELDS:
                continue
            field_counts[field] = field_counts.get(field, 0) + 1
        return fields

    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        stage = str(event.get("stage") or "")
        if stage not in {"score_review", "novelty_review", "deep_validation_review"}:
            continue
        lanes = {
            str(item.get("candidate_id") or ""): item
            for item in _orchestrator_candidate_lane_items(event.get("candidate_lanes"), limit=24)
            if isinstance(item, dict)
        }
        advice = event.get("advice") if isinstance(event.get("advice"), dict) else {}
        lane_decisions = advice.get("candidate_lane_decisions") if isinstance(advice.get("candidate_lane_decisions"), list) else []
        decision_by_expr = {
            str(item.get("expression") or "").strip().lower(): item
            for item in lane_decisions
            if isinstance(item, dict) and item.get("expression")
        }
        for ref in event.get("evidence_refs") or []:
            if not isinstance(ref, dict):
                continue
            candidate_id = str(ref.get("candidate_id") or "")
            lane = lanes.get(candidate_id) or {}
            expression = str(lane.get("expression") or ref.get("expression") or "")
            expr_key = expression.strip().lower()
            if not expression or expr_key in seen_expr:
                continue
            score = ref.get("score") if ref.get("score") is not None else ref.get("quick_score")
            deep_score = ref.get("deep_score")
            grade = str(ref.get("grade") or lane.get("grade") or "").upper()
            combined_candidate = {**lane, **ref}
            if _is_orchestrator_tool_infrastructure_error(combined_candidate):
                continue
            reject_reasons = combined_candidate.get("reject_reasons") or []
            if isinstance(reject_reasons, str):
                reject_reasons = [reject_reasons]
            is_tool_score_error = (
                combined_candidate.get("status") == "score_error"
                or "score_runtime_error" in reject_reasons
                or str(combined_candidate.get("validation") or "") == "score_factor_runtime_error"
            )
            decision = decision_by_expr.get(expr_key) or {}
            profile = expression_profile(expression)
            fields = _expression_field_terms(expression)
            novelty_counts = _orchestrator_candidate_lane_counts(event.get("candidate_lanes"))
            if stage == "novelty_review" and (
                ref.get("keepers") == 0
                or novelty_counts.get("keepers") == 0
                or "novelty_not_allowed" in json.dumps(advice, ensure_ascii=False)
            ):
                novelty_vetoes.append(
                    {
                        "stage": stage,
                        "summary": str(event.get("summary") or "")[:180],
                        "action": advice.get("action"),
                        "reason": "novelty_not_allowed_or_no_keepers",
                    }
                )
                continue
            is_weak_quick = stage == "score_review" and not is_tool_score_error and (
                grade in {"C", "D"} or (isinstance(score, (int, float)) and score < 60)
            )
            is_deep_near_miss = stage == "deep_validation_review" and isinstance(deep_score, (int, float)) and deep_score < 80
            if is_weak_quick:
                add_field_counts(expression)
                seen_expr.add(expr_key)
                weak_candidates.append(
                    {
                        "candidate_id": candidate_id or lane.get("candidate_id"),
                        "expression": expression[:220],
                        "score": score,
                        "grade": grade or None,
                        "fields": fields[:8],
                        "profile": {key: profile.get(key) for key in ("nesting_depth", "has_nonlinear", "windows", "length")},
                        "mutation_action": decision.get("action") or ref.get("deep_action"),
                        "reason": decision.get("reason") or ref.get("deep_reason"),
                    }
                )
            elif is_deep_near_miss:
                seen_expr.add(expr_key)
                near_misses.append(
                    {
                        "candidate_id": candidate_id or lane.get("candidate_id"),
                        "expression": expression[:220],
                        "quick_score": ref.get("quick_score"),
                        "deep_score": deep_score,
                        "deep_reason": ref.get("deep_reason"),
                        "ic": ref.get("ic"),
                        "icir": ref.get("icir"),
                        "fields": fields[:8],
                    }
                )
            if len(weak_candidates) >= limit_candidates and len(near_misses) >= 3:
                break

    weak_fields = [
        {"field": field, "recent_failure_count": count}
        for field, count in sorted(field_counts.items(), key=lambda item: (-item[1], item[0]))[:limit_fields]
    ]
    if not weak_candidates and not near_misses and not novelty_vetoes:
        return {}
    return {
        "weak_fields": weak_fields,
        "weak_candidates": weak_candidates[:limit_candidates],
        "deep_near_misses": near_misses[:3],
        "novelty_vetoes": novelty_vetoes[:3],
        "policy": [
            "weak_fields are advisory low-score fields from completed scoring only; tool/runtime infrastructure failures must not mark fields weak.",
            "Do not reuse weak_fields as the main thesis source unless the mutation explicitly changes mechanism or normalization.",
            "Prefer targeted mutation from deep_near_misses before exploring unrelated weak-field combinations.",
            "If novelty veto appears, orthogonalize the source or remove the crowded leg before deep validation.",
        ],
    }


def _build_orchestrator_context_pack(
    *,
    run_id: str,
    round_id: str,
    stage: str,
    contract: dict,
    round_events: list[dict],
) -> dict:
    context_result = factor_tool_context(skip_quantgpt_probe=True).to_dict()
    active_context = context_result.get("outputs", {}) if isinstance(context_result, dict) else {}
    active_context = dict(active_context or {})
    active_context.pop("must_read_contract", None)
    active_context["factor_map_context"] = dict(contract.get("factor_map_context") or {})
    active_context["orchestrator_contract"] = {
        "contract_source": "domain/factor_research/ORCHESTRATOR_README.md",
        "codex_foreground_required": False,
        "rolling_validation_policy": "required_deep_validation_evidence_included_in_deep_score",
        "candidate_plan_code_precheck": {
            "scope": "pre_score_schema_and_obvious_expression_error_triage",
            "hard_blocks": [
                "exact_active_expression",
                "batch_duplicate_expression",
                "unsupported_or_blocked_fields",
                "empty_expression",
                "known_zero_sparse_or_mutually_exclusive_constructs",
            ],
            "soft_marks": [],
            "candidate_plan_llm_may_skip": [
                "per_candidate_semantic_revision",
                "batch_semantic_duplicate",
                "library_near_copy",
            ],
            "uncertain_or_parent_mutation_defaults_to_score": True,
            "pure_code": True,
            "llm_context_key": "code_precheck",
            "gui_candidate_lanes": [
                "precheck_blocked",
                "semantic_revision",
                "candidate_plan_dropped",
                "planned_for_score",
            ],
            "not_replacement_for": "fxalpha_novelty_check_numeric_factor_value_correlation",
        },
    }
    active_context["recent_orchestrator_anchors"] = _recent_orchestrator_anchors(limit=8)
    active_context["recent_orchestrator_failure_feedback"] = _recent_orchestrator_failure_feedback()
    current_run_steps = _read_recent_research_steps(limit=72, run_id=run_id)
    global_steps = _read_recent_research_steps(limit=24)
    recent_steps: list[dict] = []
    seen_step_ids: set[str] = set()
    for step in [*current_run_steps, *global_steps]:
        # Human guidance remains in research_steps for audit/UI receipts, but
        # must not re-enter the model through generic short-term history.
        if str(step.get("stage") or "") == "human_guidance":
            continue
        step_id = str(step.get("stage_id") or "")
        if step_id and step_id in seen_step_ids:
            continue
        if step_id:
            seen_step_ids.add(step_id)
        recent_steps.append(step)
    # Keep the context-pack lineage consistent with the run-scoped GUI and
    # review views.  The raw task store contains historical runs; none of
    # those tasks should become implicit evidence for a new DeepSeek round.
    quantgpt_tasks = _quantgpt_tasks_for_research_run(
        _fetch_quantgpt_recent_tasks(limit=48, allow_restart=False),
        run_id,
    )
    quantgpt_summary = _quantgpt_task_summary(quantgpt_tasks)
    return OrchestratorContextPack(
        run_id=run_id,
        round_id=round_id,
        stage=stage,
        contract=contract,
        active_context=active_context,
        recent_steps=recent_steps,
        quantgpt_summary=quantgpt_summary,
        round_events=round_events,
    ).to_dict()


_ORCHESTRATOR_RESEARCH_SYSTEM = PROMPT_CONTRACT_RESEARCH_SYSTEM

_ORCHESTRATOR_STAGE_BRIEFINGS = PROMPT_CONTRACT_STAGE_BRIEFINGS

_ORCHESTRATOR_REVIEW_STAGES = {
    "score_review",
    "novelty_review",
    "deep_validation_review",
    "import_gate_review",
}

_ORCHESTRATOR_STAGE_REQUIRED = {
    "thesis_design": ["decision", "judgment", "why", "history_used", "theses", "next_action", "stage_transition", "confidence"],
    "hypothesis_design": ["decision", "judgment", "why", "history_used", "hypotheses", "next_action", "stage_transition", "confidence"],
    "expression_design": ["decision", "judgment", "why", "history_used", "next_action", "stage_transition", "confidence"],
    "candidate_plan": ["decision", "judgment", "why", "history_used", "candidate_lanes", "next_action", "stage_transition", "confidence"],
    "score_review": ["decision", "judgment", "why", "history_used", "candidate_decisions", "next_action", "stage_transition", "confidence"],
    "novelty_review": ["decision", "judgment", "why", "history_used", "candidate_decisions", "next_action", "stage_transition", "confidence"],
    "deep_validation_review": ["decision", "judgment", "why", "history_used", "candidate_decisions", "next_action", "stage_transition", "confidence"],
    "import_gate_review": ["decision", "judgment", "why", "history_used", "candidate_decisions", "next_action", "stage_transition", "confidence"],
    "import_review": ["decision", "judgment", "why", "history_used", "import_summary", "next_action", "stage_transition", "confidence"],
    "round_synthesis": ["decision", "judgment", "why", "history_used", "round_memory", "next_action", "stage_transition", "confidence"],
    "blocker_review": ["decision", "judgment", "why", "blocked_component", "recovery_action", "next_action", "stage_transition", "confidence"],
}

_ORCHESTRATOR_STAGE_SCHEMAS = {
    "thesis_design": {"stage": "thesis_design", "decision": "propose_theses", "judgment": "...", "why": "...", "history_used": ["..."], "theses": [{"thesis_id": "t1", "economic_rationale": "...", "expected_alpha_mechanism": "...", "preferred_data_families": ["..."], "avoid_patterns": ["..."], "priority": "high|normal|low"}], "next_action": "advance_to_hypothesis_design", "stage_transition": {"next_stage": "hypothesis_design", "reason": "..."}, "confidence": 0.0},
    "hypothesis_design": {"stage": "hypothesis_design", "decision": "propose_hypotheses", "judgment": "...", "why": "...", "history_used": [], "hypotheses": [{"hypothesis_id": "h1", "thesis_id": "t1", "signal_claim": "...", "expected_direction": "positive|negative|conditional", "candidate_variable_groups": [{"role": "main_signal|confirmation|risk_control", "fields": ["field_name"], "direction": "positive|negative", "operators": ["operator_name"]}], "window_policy": "...", "normalization_policy": "...", "risk_notes": [], "mutation_plan_if_fail": []}], "next_action": "advance_to_expression_design", "stage_transition": {"next_stage": "expression_design", "reason": "..."}, "confidence": 0.0},
    "expression_design": {"stage": "expression_design", "decision": "propose_candidates|blocked", "judgment": "...", "why": "...", "history_used": [], "candidates": [{"candidate_id": "c1", "hypothesis_id": "h1", "expression": "...", "expected_direction": "...", "mechanism_summary": "...", "mechanism_delta": "new relation versus parent or prior-run signature; not window-only", "complexity_intent": "simple|moderate", "factor_name_hint": "...", "parent_candidate_id": None, "mutation_summary": None}], "blocked_reason": None, "next_action": "validate_and_score|block_for_human", "stage_transition": {"next_stage": "candidate_plan|blocker_review", "reason": "..."}, "confidence": 0.0},
    "candidate_plan": {"stage": "candidate_plan", "decision": "run_batch|revise_expression|return_hypothesis", "judgment": "...", "why": "...", "history_used": [], "candidate_lanes": [{"candidate_id": "c1", "action": "score", "keep": True, "reason": "表达式合法且方向一致，进入 quick score。", "matched_candidate_ids": [], "matched_cluster_id": None, "matched_factor_ids": []}, {"candidate_id": "c2", "action": "revise_expression", "keep": False, "reason": "表达式方向或语义不一致，退回 expression_design 修正。", "matched_candidate_ids": [], "matched_cluster_id": None, "matched_factor_ids": []}], "next_action": "validate_and_score|return_expression_design|return_hypothesis_design", "stage_transition": {"next_stage": "score_review|expression_design|hypothesis_design", "reason": "per-candidate conservative budget decision"}, "confidence": 0.0},
    "score_review": {"stage": "score_review", "decision": "advance_some|mutate|return_hypothesis|return_thesis|reject_batch", "judgment": "...", "why": "...", "history_used": [], "candidate_decisions": [{"candidate_id": "c1", "action": "advance_to_novelty|revise_expression|return_hypothesis|return_thesis|reject", "failure_class": "keeper|direction_normalization|near_miss_mutate|no_parent_value|invalid_or_error", "reason": "当前评分证据、parent 价值和下一步动作", "mutation_advice": {"type": "simplify_expression|mutate_operator|mutate_normalization|mutate_signal_direction|mutate_interaction|explore_new_thesis", "preserve": "...", "change": "...", "avoid": "..."}}], "next_action": "run_novelty|return_expression_design|return_hypothesis_design|return_thesis_design|stop_round", "stage_transition": {"next_stage": "novelty_review|expression_design|hypothesis_design|thesis_design|round_synthesis", "reason": "best parent / no parent and next mutation route"}, "confidence": 0.0},
    "novelty_review": {"stage": "novelty_review", "decision": "advance_some|orthogonalize|reject_st_exposure|return_expression|return_hypothesis|return_thesis|reject_batch", "judgment": "...", "why": "...", "history_used": [], "candidate_decisions": [{"candidate_id": "c1", "action": "advance_to_deep_validation|novelty_reject|orthogonalize_expression|return_hypothesis|reject_st_exposure|keep_as_control|return_thesis|reject", "reason": "正式 novelty 证据及下一步", "novelty_interpretation": "...", "st_exposure_interpretation": "advisory_only|hard_veto|passed|missing", "preserve": "...", "change": "...", "avoid": "..."}], "next_action": "run_deep_validation|return_expression_design|return_hypothesis_design|return_thesis_design|stop_round", "stage_transition": {"next_stage": "deep_validation_review|expression_design|hypothesis_design|thesis_design|round_synthesis", "reason": "..."}, "confidence": 0.0},
    "deep_validation_review": {"stage": "deep_validation_review", "decision": "submit_gate|complete_evidence|targeted_mutation|recombine|explore|simplify|reject", "judgment": "...", "why": "...", "history_used": [], "candidate_decisions": [{"candidate_id": "c1", "action": "submit_quality_gate|complete_deep_evidence|targeted_mutation|recombine_from_best|explore_new_thesis|simplify_expression|reject|blocker", "reason": "gate readiness、完整 deep 证据和返回层级", "weakest_component": "quick|ic|icir|anti_overfit|rolling|adversarial|novelty|risk|complexity|evidence_missing", "preserve": "...", "change": "...", "avoid": "..."}], "next_action": "run_quality_gate|return_expression_design|return_hypothesis_design|return_thesis_design|block_for_human", "stage_transition": {"next_stage": "import_gate_review|expression_design|hypothesis_design|thesis_design|blocker_review", "reason": "actual next research entry; round_synthesis is executed by code first"}, "confidence": 0.0},
    "import_gate_review": {"stage": "import_gate_review", "decision": "adopted|repair_metadata|complete_evidence|gate_mismatch_feedback|reject", "judgment": "...", "why": "...", "history_used": [], "candidate_decisions": [{"candidate_id": "c1", "action": "import|gate_reject|repair_factor_name|complete_evidence|return_deep_validation|record_gate_mismatch|reject|blocker", "factor_name": "MechanismOperatorWindow", "reason": "正式 gate 结果和下一步", "gate_feedback_for_future": "..."}], "next_action": "import_factor|return_deep_validation|return_expression_design|stop_round", "stage_transition": {"next_stage": "import_review|deep_validation_review|expression_design|round_synthesis", "reason": "..."}, "confidence": 0.0},
    "import_review": {"stage": "import_review", "decision": "import_success|import_failed|repair_import", "judgment": "...", "why": "...", "history_used": [], "import_summary": {"candidate_id": "c1", "factor_name": "...", "safe_name": "...", "adopted": True}, "next_action": "round_synthesis|repair_import|stop_run", "stage_transition": {"next_stage": "round_synthesis|import_review|stop", "reason": "..."}, "confidence": 0.0},
    "round_synthesis": {"stage": "round_synthesis", "decision": "continue_next_round|stop_target_reached|round_budget_reached", "judgment": "...", "why": "...", "history_used": [], "round_memory": {"positive_lessons": ["r0004:c3: parent evidence supports the economic mechanism"], "negative_lessons": ["r0005:c2-c4: the tested relation was crowded; window-only changes have no value"], "next_round_handoff": "return level, parent evidence refs, mechanism to retain, role that must change, and failed mechanism", "suggested_start_stage": "thesis_design|hypothesis_design|expression_design", "avoid_patterns": ["same mechanism with window-only variation"], "promising_parents": ["r0004:c3: evidence reference only, not an expression template"]}, "next_action": "start_next_round|stop_run", "stage_transition": {"next_stage": "thesis_design|hypothesis_design|expression_design|checkpoint_stop", "reason": "..."}, "confidence": 0.0},
    "blocker_review": {"stage": "blocker_review", "decision": "retry|shrink_context_retry|repair_input|human_blocker", "judgment": "...", "why": "...", "blocked_component": "llm|tool|schema|data|metadata|unknown", "recovery_action": "...", "next_action": "retry_stage|block_for_human", "stage_transition": {"next_stage": "previous_stage|stop", "reason": "..."}, "confidence": 0.0},
}

for _stage_name, _required_fields in _ORCHESTRATOR_STAGE_REQUIRED.items():
    if "summary" not in _required_fields:
        _required_fields.insert(0, "summary")
    if "history_used" not in _required_fields:
        _required_fields.insert(4, "history_used")
    if _stage_name in _ORCHESTRATOR_STAGE_SCHEMAS:
        _stage_schema = _ORCHESTRATOR_STAGE_SCHEMAS[_stage_name]
        # Schema examples describe shape only.  Positive canned prose made the
        # model repeat "当前证据支持继续研究" even when tools showed a veto.
        _stage_schema["summary"] = "一句话说明本阶段实际处理对象和结果。"
        _stage_schema["judgment"] = "基于本次输入证据给出明确结论。"
        _stage_schema["why"] = "引用本次输入中的关键事实解释当前动作。"
        _stage_schema["history_used"] = []
        if isinstance(_stage_schema.get("stage_transition"), dict):
            _stage_schema["stage_transition"]["reason"] = "说明为何进入所选下一阶段。"

_ORCHESTRATOR_ALLOWED_NEXT_STAGES = {
    "thesis_design": {"hypothesis_design"},
    "hypothesis_design": {"expression_design"},
    "expression_design": {"candidate_plan", "blocker_review"},
    "candidate_plan": {"score_review", "expression_design", "hypothesis_design"},
    "score_review": {"novelty_review", "expression_design", "hypothesis_design", "thesis_design", "round_synthesis", "blocker_review"},
    "novelty_review": {"deep_validation_review", "expression_design", "hypothesis_design", "thesis_design", "round_synthesis", "blocker_review"},
    "deep_validation_review": {"import_gate_review", "expression_design", "hypothesis_design", "thesis_design", "round_synthesis", "blocker_review"},
    "import_gate_review": {"import_review", "deep_validation_review", "expression_design", "round_synthesis", "blocker_review"},
    "import_review": {"round_synthesis", "import_review", "blocker_review", "stop"},
    "round_synthesis": {"thesis_design", "hypothesis_design", "expression_design", "checkpoint_stop"},
    "blocker_review": {"previous_stage", "stop"},
}

_ORCHESTRATOR_STAGE_TAGS = {
    "thesis_design": ["thesis_design", "llm_result"],
    "hypothesis_design": ["hypothesis_design", "llm_result"],
    "expression_design": ["expression_design", "llm_result"],
    "candidate_plan": ["candidate_plan", "llm_result"],
    "score_review": ["score_review", "llm_review"],
    "novelty_review": ["novelty_review", "llm_review"],
    "deep_validation_review": ["deep_validation_review", "llm_review"],
    "import_gate_review": ["import_gate_review", "llm_review"],
    "import_review": ["import_review", "llm_review"],
    "round_synthesis": ["round_synthesis", "llm_result"],
    "blocker_review": ["blocker_review", "llm_result", "blocker"],
}

_ORCHESTRATOR_RESUME_STAGES = {"thesis_design", "hypothesis_design", "expression_design"}
_ORCHESTRATOR_TERMINAL_STAGES = {"checkpoint_stop", "stop", "blocker", "blocker_review"}
_ORCHESTRATOR_CONTEXT_BUDGET_DEFAULT = {
    "max_payload_chars": 76000,
    "history_limit": 8,
    "candidate_limit": 12,
    "tool_evidence_limit": 12,
}
_ORCHESTRATOR_CONTEXT_BUDGETS = {
    "thesis_design": {"max_payload_chars": 120000, "history_limit": 6, "candidate_limit": 10, "tool_evidence_limit": 10},
    "hypothesis_design": {"max_payload_chars": 120000, "history_limit": 6, "candidate_limit": 10, "tool_evidence_limit": 10},
    "expression_design": {"max_payload_chars": 120000, "history_limit": 6, "candidate_limit": 12, "tool_evidence_limit": 12},
    "candidate_plan": {"max_payload_chars": 120000, "history_limit": 6, "candidate_limit": 12, "tool_evidence_limit": 12},
    "score_review": {"max_payload_chars": 82000, "history_limit": 6, "candidate_limit": 12, "tool_evidence_limit": 12},
    "novelty_review": {"max_payload_chars": 82000, "history_limit": 6, "candidate_limit": 12, "tool_evidence_limit": 12},
    "deep_validation_review": {"max_payload_chars": 90000, "history_limit": 6, "candidate_limit": 12, "tool_evidence_limit": 12},
    "import_gate_review": {"max_payload_chars": 76000, "history_limit": 6, "candidate_limit": 12, "tool_evidence_limit": 12},
    "import_review": {"max_payload_chars": 70000, "history_limit": 6, "candidate_limit": 12, "tool_evidence_limit": 12},
    "round_synthesis": {"max_payload_chars": 70000, "history_limit": 6, "candidate_limit": 10, "tool_evidence_limit": 10},
    "blocker_review": {"max_payload_chars": 60000, "history_limit": 4, "candidate_limit": 10, "tool_evidence_limit": 10},
}

# Single prompt-context policy.  The factor map selects and explains thesis /
# hypothesis research questions; it is deliberately absent from expression
# generation and evidence-review stages so it cannot become an inaccurate
# candidate-level novelty precheck.  Synthesis sees actionable run guidance
# only, while formal score/novelty/deep/gate/import evidence remains authoritative.
_ORCHESTRATOR_STAGE_CONTEXT_POLICY = {
    "thesis_design": {
        "research_space": "fields", "factor_map": True, "complete_factor_map": True,
        "factor_map_mode": "full",
    },
    "hypothesis_design": {
        "research_space": "full", "factor_map": True, "complete_factor_map": False,
        "factor_map_mode": "related_only", "family_limit": 8,
    },
    "expression_design": {"research_space": "full", "factor_map": False},
    "candidate_plan": {"research_space": "full", "factor_map": False},
    "score_review": {"research_space": "none", "factor_map": False},
    "novelty_review": {"research_space": "none", "factor_map": False},
    "deep_validation_review": {"research_space": "none", "factor_map": False},
    "import_gate_review": {"research_space": "none", "factor_map": False},
    "import_review": {"research_space": "none", "factor_map": False},
    "round_synthesis": {
        "research_space": "none", "factor_map": True,
        "factor_map_mode": "affected_only",
    },
    "blocker_review": {"research_space": "none", "factor_map": False},
}

_ORCHESTRATOR_HANDOFF_VISIBLE_STAGES = {
    "thesis_design",
    "hypothesis_design",
    "expression_design",
    "candidate_plan",
    "score_review",
    "novelty_review",
    "deep_validation_review",
    "import_gate_review",
    "import_review",
    "round_synthesis",
    "blocker_review",
}


def _sanitize_llm_payload(payload: dict) -> dict:
    clean = dict(payload or {})
    clean.pop("api_key", None)
    clean.pop("prompt", None)
    clean.pop("messages", None)
    clean.pop("context_budget", None)
    clean.pop("_context_budget", None)
    clean.pop("_orchestrator_llm_model", None)
    clean.pop("_orchestrator_llm_model_order", None)
    clean.pop("_orchestrator_llm_usage", None)
    return clean


def _history_stage_priority(current_stage: str | None, step_stage: str | None) -> int:
    stage = str(step_stage or "")
    current = str(current_stage or "")
    if not stage:
        return 0
    priority_map = {
        "thesis_design": {
            "round_synthesis": 6,
            "deep_validation_review": 5,
            "novelty_review": 4,
            "score_review": 3,
            "expression_design": 2,
            "hypothesis_design": 1,
        },
        "hypothesis_design": {
            "deep_validation_review": 6,
            "round_synthesis": 5,
            "novelty_review": 4,
            "score_review": 3,
            "expression_design": 2,
            "thesis_design": 1,
        },
        "expression_design": {
            "deep_validation_review": 7,
            "round_synthesis": 6,
            "novelty_review": 5,
            "score_review": 4,
            "candidate_plan": 3,
            "hypothesis_design": 2,
            "thesis_design": 1,
        },
        "candidate_plan": {
            "candidate_plan": 6,
            "expression_design": 5,
            "score_review": 4,
            "deep_validation_review": 3,
            "round_synthesis": 2,
        },
        "score_review": {
            "deep_validation_review": 6,
            "score_review": 5,
            "candidate_plan": 5,
            "novelty_review": 3,
            "round_synthesis": 2,
        },
        "novelty_review": {
            "novelty_review": 6,
            "score_review": 5,
            "deep_validation_review": 4,
            "round_synthesis": 3,
            "candidate_plan": 2,
        },
        "deep_validation_review": {
            "deep_validation_review": 7,
            "novelty_review": 6,
            "score_review": 5,
            "import_gate_review": 4,
            "round_synthesis": 3,
        },
        "import_gate_review": {
            "import_gate_review": 7,
            "deep_validation_review": 6,
            "import_review": 5,
            "round_synthesis": 4,
            "novelty_review": 3,
        },
        "import_review": {
            "import_review": 7,
            "import_gate_review": 6,
            "deep_validation_review": 5,
            "round_synthesis": 4,
        },
        "round_synthesis": {
            "round_synthesis": 7,
            "deep_validation_review": 6,
            "novelty_review": 5,
            "score_review": 4,
            "import_gate_review": 3,
            "import_review": 2,
        },
    }
    return priority_map.get(current, {}).get(stage, 0)


def _history_step_is_substantive(step: dict) -> bool:
    if not isinstance(step, dict):
        return False
    stage = str(step.get("stage") or "").strip()
    tags = {str(tag) for tag in (step.get("tags") or [])}
    monitoring = step.get("monitoring") if isinstance(step.get("monitoring"), dict) else {}
    transition = _history_step_transition(step)
    if stage == "protocol_load":
        return False
    if "llm_request_progress" in tags or "tool_progress" in tags:
        return False
    if str(monitoring.get("event_type") or "") in {"llm_request", "checkpoint"} and not any(
        str(transition.get(key) or "").strip() for key in ("judgment", "why", "facts")
    ):
        return False
    if str(step.get("decision") or "").strip() == "进入 LLM review 阶段，等待 DeepSeek v4 返回 JSON 决策。":
        return False
    return bool(
        str(step.get("decision") or "").strip()
        or str(transition.get("judgment") or "").strip()
        or str(transition.get("why") or "").strip()
        or str(transition.get("facts") or "").strip()
    )


def _history_step_transition(step: dict) -> dict:
    if not isinstance(step, dict):
        return {}
    transition = dict(step.get("stage_transition") if isinstance(step.get("stage_transition"), dict) else {})
    for key in ("next_stage", "next_action", "judgment", "why", "history_used", "facts"):
        if transition.get(key) in (None, "", [], {}):
            transition[key] = step.get(key)
    return transition


def _compact_stage_history(context_pack: dict, *, stage: str | None = None, round_events: list[dict] | None = None) -> dict:
    context_pack = context_pack if isinstance(context_pack, dict) else {}
    active_context = context_pack.get("active_context") if isinstance(context_pack.get("active_context"), dict) else {}
    run_state = context_pack.get("run_state") if isinstance(context_pack.get("run_state"), dict) else {}
    raw_steps = [
        step
        for step in (context_pack.get("recent_steps") or [])
        if isinstance(step, dict) and str(step.get("stage") or "") != "human_guidance"
    ]
    stage_name = str(stage or "")
    if stage_name != "blocker_review":
        raw_steps = [step for step in raw_steps if str(step.get("stage") or "") != "blocker"]
    preferred_steps = [step for step in raw_steps if _history_step_is_substantive(step)]
    current_run_id = str(run_state.get("run_id") or "")
    review_stage = stage_name in {"score_review", "novelty_review", "deep_validation_review", "import_gate_review", "import_review"}
    if stage_name == "round_synthesis":
        summary_limit = 4
        anchor_limit = 2
        best_positive_limit = 2
        same_family_limit = 3
        same_run_seed = 2
        cross_run_seed = 1
    elif review_stage:
        # Review prompts retain the recent research chain. Current tool
        # evidence remains the only basis for decisions.
        summary_limit = 4
        anchor_limit = 2
        best_positive_limit = 2
        same_family_limit = 3
        same_run_seed = 2
        cross_run_seed = 1
    else:
        summary_limit = 3
        anchor_limit = 2
        best_positive_limit = 2
        same_family_limit = 2
        same_run_seed = 2
        cross_run_seed = 1
    selected_steps: list[dict] = []
    seen_step_ids: set[str] = set()
    same_run_preferred = [step for step in preferred_steps if str(step.get("run_id") or "") == current_run_id]
    cross_run_preferred = [step for step in preferred_steps if str(step.get("run_id") or "") != current_run_id]
    same_run_raw = [step for step in raw_steps if str(step.get("run_id") or "") == current_run_id]
    cross_run_raw = [step for step in raw_steps if str(step.get("run_id") or "") != current_run_id]

    def add_step(step: dict) -> None:
        step_id = str(step.get("stage_id") or "") or json.dumps(
            {
                "ts": step.get("ts"),
                "run_id": step.get("run_id"),
                "round_id": step.get("round_id"),
                "stage": step.get("stage"),
                "stage_seq": step.get("stage_seq"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if step_id in seen_step_ids:
            return
        seen_step_ids.add(step_id)
        selected_steps.append(step)

    for step in same_run_preferred[: min(same_run_seed, summary_limit)]:
        add_step(step)
    for step in cross_run_preferred[: max(0, min(cross_run_seed, summary_limit - len(selected_steps)))]:
        add_step(step)
    prioritized = sorted(
        same_run_preferred[min(same_run_seed, summary_limit):] + cross_run_preferred[max(0, min(cross_run_seed, summary_limit - len(selected_steps))):],
        key=lambda item: (
            0 if str(item.get("run_id") or "") == current_run_id else 1,
            -_history_stage_priority(stage, item.get("stage")),
            0 if str(item.get("decision") or "").strip() else 1,
        ),
    )
    for step in prioritized:
        if len(selected_steps) >= summary_limit:
            break
        if _history_stage_priority(stage, step.get("stage")) <= 0 and str(step.get("decision") or "").strip() == "":
            continue
        add_step(step)
    if len(selected_steps) < min(summary_limit, 3):
        for step in same_run_raw + cross_run_raw:
            if len(selected_steps) >= summary_limit:
                break
            add_step(step)

    recent_steps_compact = []
    for step in selected_steps:
        if not isinstance(step, dict):
            continue
        transition = _history_step_transition(step)
        recent_steps_compact.append(
            {
                "ts": step.get("ts") or step.get("created_at"),
                "stage": step.get("stage"),
                "summary": _clip_text(step.get("summary"), 180),
                "decision": _clip_text(step.get("decision"), 120),
                "next_stage": transition.get("next_stage"),
                "next_action": _clip_text(transition.get("next_action"), 120),
                "judgment": _clip_text(transition.get("judgment"), 160),
                "why": _clip_text(transition.get("why"), 160),
                "tags": (step.get("tags") or [])[:6],
            }
        )
    review_anchors = []
    anchor_candidates = sorted(
        selected_steps,
        key=lambda item: (
            -_history_stage_priority(stage, item.get("stage")),
            0
            if str(
                _history_step_transition(item).get("judgment")
                or _history_step_transition(item).get("why")
                or item.get("decision")
                or ""
            ).strip()
            else 1,
        ),
    )
    for step in anchor_candidates:
        transition = _history_step_transition(step)
        if _history_stage_priority(stage, step.get("stage")) <= 0:
            continue
        review_anchors.append(
            {
                "stage": step.get("stage"),
                "decision": _clip_text(step.get("decision"), 120),
                "judgment": _clip_text(transition.get("judgment"), 180),
                "why": _clip_text(transition.get("why"), 180),
                "next_stage": transition.get("next_stage"),
            }
        )
        if len(review_anchors) >= anchor_limit:
            break
    latest_round_handoff = {}
    for step in selected_steps + same_run_raw[:4]:
        if not isinstance(step, dict) or str(step.get("stage") or "") != "round_synthesis":
            continue
        transition = _history_step_transition(step)
        latest_round_handoff = {
            "stage": step.get("stage"),
            "decision": _clip_text(step.get("decision"), 120),
            "judgment": _clip_text(transition.get("judgment"), 180),
            "why": _clip_text(transition.get("why"), 180),
            "next_stage": transition.get("next_stage"),
            "next_action": _clip_text(transition.get("next_action"), 120),
        }
        latest_round_handoff = {key: value for key, value in latest_round_handoff.items() if value not in (None, "", [], {})}
        if latest_round_handoff:
            break
    same_round_events = [
        {
            "stage": event.get("stage"),
            "decision": _clip_text(event.get("decision"), 180),
            "summary": _clip_text(event.get("summary"), 220),
        }
        for event in (round_events or context_pack.get("round_events") or [])[-same_family_limit:]
        if isinstance(event, dict)
    ]
    recent_completed_rounds: list[dict[str, Any]] = []
    seen_round_ids: set[str] = set()
    for step in same_run_raw:
        if str(step.get("stage") or "") != "round_synthesis" or not _history_step_is_substantive(step):
            continue
        round_id = str(step.get("round_id") or "").strip()
        if not round_id or round_id in seen_round_ids:
            continue
        transition = _history_step_transition(step)
        recent_completed_rounds.append(
            _jsonable(
                _prune_empty_prompt_values(
                    {
                        "round_id": round_id,
                        "decision": _clip_text(step.get("decision"), 120),
                        "summary": _clip_text(step.get("summary"), 240),
                        "judgment": _clip_text(transition.get("judgment"), 200),
                        "why": _clip_text(transition.get("why"), 240),
                        "next_stage": transition.get("next_stage"),
                    }
                )
            )
        )
        seen_round_ids.add(round_id)
        if len(recent_completed_rounds) >= 3:
            break
    return _jsonable(
        _prune_empty_prompt_values({
            "short_term_history": {
                "latest_round_handoff": latest_round_handoff,
                "recent_completed_rounds": recent_completed_rounds,
                "stage_relevant_steps": recent_steps_compact,
                "positive_precedents": _candidate_context_anchors(active_context)[:best_positive_limit],
                "negative_precedents": _candidate_context_failure_feedback(active_context.get("recent_orchestrator_failure_feedback") or {}),
                "review_anchors": review_anchors,
                "recent_same_round_events": same_round_events,
            },
        })
    )


def _model_visible_stage_history(
    history_context: dict | None,
    *,
    has_upstream_handoff: bool,
    stage: str | None = None,
) -> dict:
    """Expose one handoff plus deduplicated completed-round facts.

    The durable history projection intentionally remains rich for the GUI,
    monitoring, and audits.  It is not a safe LLM decision surface, however:
    the same candidate can be present in stage summaries, positive anchors,
    and downstream failure feedback at the same time.  Actionable continuity is
    carried by ``upstream_handoff``.  Design and synthesis stages additionally
    receive one authoritative round_synthesis fact per recent round; they do
    not receive the conflicting positive/negative candidate projections.
    """

    if not isinstance(history_context, dict):
        return {}
    visible: dict[str, Any] = {}
    short_term = (
        history_context.get("short_term_history")
        if isinstance(history_context.get("short_term_history"), dict)
        else {}
    )
    latest_round_handoff = short_term.get("latest_round_handoff")
    if not has_upstream_handoff and isinstance(latest_round_handoff, dict) and latest_round_handoff:
        visible["short_term_history"] = {
            "latest_round_handoff": _jsonable(latest_round_handoff),
        }
    stage_name = str(stage or "")
    if stage_name in {"thesis_design", "hypothesis_design", "expression_design", "round_synthesis"}:
        recent_completed = [
            _jsonable(item)
            for item in (short_term.get("recent_completed_rounds") or [])[:3]
            if isinstance(item, dict)
        ]
        if recent_completed:
            visible.setdefault("short_term_history", {})["recent_completed_rounds"] = recent_completed
    return _jsonable(_prune_empty_prompt_values(visible))


def _stage_lineage_context(
    *,
    thesis_result: dict | None = None,
    hypothesis_result: dict | None = None,
    expression_result: dict | None = None,
    candidate_plan_result: dict | None = None,
    previous_review_advice: list[dict] | None = None,
    return_handoff: dict | None = None,
) -> dict:
    theses = (thesis_result or {}).get("theses") if isinstance(thesis_result, dict) else []
    hypotheses = (hypothesis_result or {}).get("hypotheses") if isinstance(hypothesis_result, dict) else []
    candidates = (expression_result or {}).get("candidates") if isinstance(expression_result, dict) else []
    return {
        "current_thesis": theses[:5] if isinstance(theses, list) else [],
        "current_hypothesis": hypotheses[:8] if isinstance(hypotheses, list) else [],
        "parent_candidates": candidates[:8] if isinstance(candidates, list) else [],
        "candidate_plan": candidate_plan_result or {},
        "previous_review_advice": _dedupe_compact_handoffs(previous_review_advice, limit=6),
        "return_reason_from_downstream": _compact_return_handoff(return_handoff),
    }


def _stage_allows_handoff_visibility(stage: str | None) -> bool:
    return str(stage or "") in _ORCHESTRATOR_HANDOFF_VISIBLE_STAGES


def _handoff_targets_stage(handoff: dict | None, stage: str | None) -> bool:
    if not isinstance(handoff, dict):
        return False
    target_stage = str(handoff.get("to_stage") or "").strip()
    current_stage = str(stage or "").strip()
    if not target_stage or not current_stage:
        return False
    # Handoffs are one-shot instructions for one research level.  Once that
    # level has produced a new result, downstream stages use the new lineage,
    # not the stale return reason.
    return target_stage == current_stage


def _filtered_handoffs_for_stage(handoffs: list[dict] | None, *, stage: str | None = None) -> list[dict]:
    current_stage = str(stage or "").strip()
    if not current_stage:
        return []
    relevant = [item for item in (handoffs or []) if _handoff_targets_stage(item, current_stage)]
    return _dedupe_compact_handoffs(relevant, limit=6)


def _normalize_prompt_list(value: Any, *, limit: int = 4, clip: int = 100) -> list[str]:
    if isinstance(value, str):
        text = _clip_text(value, clip)
        return [text] if text else []
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value[:limit]:
        text = _clip_text(item, clip)
        if text:
            normalized.append(text)
    return normalized


def _list_prefix(value: Any, limit: int) -> list[Any]:
    if isinstance(value, list):
        return value[:limit]
    if isinstance(value, tuple):
        return list(value[:limit])
    return []


def _candidate_ref_id(value: Any) -> str:
    ref = str(value or "").strip().lower()
    if re.match(r"^r\d{4}:", ref, flags=re.IGNORECASE):
        return ref.split(":", 1)[1]
    return ref


def _prioritize_referenced_parents(
    candidates: list[dict],
    preferred_parent_refs: list[str] | None,
) -> list[dict]:
    preferred_ids = [
        _candidate_ref_id(value)
        for value in (preferred_parent_refs or [])
        if _candidate_ref_id(value)
    ]
    if not preferred_ids:
        return candidates
    rank_by_id = {candidate_id: idx for idx, candidate_id in enumerate(preferred_ids)}
    indexed = list(enumerate(candidates))
    indexed.sort(
        key=lambda pair: (
            rank_by_id.get(
                _candidate_ref_id((pair[1] or {}).get("candidate_id"))
                if isinstance(pair[1], dict)
                else "",
                len(rank_by_id),
            ),
            pair[0],
        )
    )
    return [item for _, item in indexed]


def _compact_lineage_context_for_prompt(
    lineage_context: dict | None,
    *,
    stage: str | None = None,
    preferred_parent_refs: list[str] | None = None,
) -> dict:
    if not isinstance(lineage_context, dict):
        return {}
    stage_name = str(stage or "")
    thesis_limit = 3
    hypothesis_limit = 4
    parent_limit = 4
    include_candidate_plan = True
    if stage_name == "thesis_design":
        thesis_limit = 0
        hypothesis_limit = 0
        parent_limit = 2
        include_candidate_plan = False
    elif stage_name == "hypothesis_design":
        thesis_limit = 3
        hypothesis_limit = 0
        parent_limit = 2
        include_candidate_plan = False
    elif stage_name == "expression_design":
        thesis_limit = 3
        hypothesis_limit = 4
        parent_limit = 3
    elif stage_name == "candidate_plan":
        thesis_limit = 3
        hypothesis_limit = 4
        parent_limit = 4
    elif stage_name in {"score_review", "novelty_review", "deep_validation_review", "import_gate_review", "import_review", "round_synthesis"}:
        thesis_limit = 3
        hypothesis_limit = 4
        parent_limit = 4
        include_candidate_plan = True
    theses = []
    for item in _list_prefix(lineage_context.get("current_thesis"), thesis_limit):
        if not isinstance(item, dict):
            continue
        theses.append(
            {
                "thesis_id": item.get("thesis_id"),
                "economic_rationale": _clip_text(item.get("economic_rationale"), 140),
                "expected_alpha_mechanism": _clip_text(item.get("expected_alpha_mechanism"), 140),
                "preferred_data_families": _list_prefix(item.get("preferred_data_families"), 6),
                "avoid_patterns": _list_prefix(item.get("avoid_patterns"), 6),
                "priority": item.get("priority"),
            }
        )
    hypotheses = []
    for item in _list_prefix(lineage_context.get("current_hypothesis"), hypothesis_limit):
        if not isinstance(item, dict):
            continue
        hypotheses.append(
            {
                "hypothesis_id": item.get("hypothesis_id"),
                "thesis_id": item.get("thesis_id"),
                "signal_claim": _clip_text(item.get("signal_claim"), 160),
                "expected_direction": item.get("expected_direction"),
                "candidate_variable_groups": _compact_tool_evidence_leaf(item.get("candidate_variable_groups") or [], limit=4),
                "window_policy": _clip_text(item.get("window_policy"), 140),
                "normalization_policy": _clip_text(item.get("normalization_policy"), 140),
                "risk_notes": _normalize_prompt_list(item.get("risk_notes"), limit=4, clip=100),
                "mutation_plan_if_fail": _normalize_prompt_list(item.get("mutation_plan_if_fail"), limit=4, clip=100),
            }
        )
    parents = []
    parent_candidates = [
        item
        for item in (lineage_context.get("parent_candidates") or [])
        if isinstance(item, dict)
    ]
    parent_candidates = _prioritize_referenced_parents(
        parent_candidates,
        preferred_parent_refs,
    )
    for item in _list_prefix(parent_candidates, parent_limit):
        if not isinstance(item, dict):
            continue
        parents.append(
            _jsonable(_prune_empty_prompt_values({
                "candidate_id": item.get("candidate_id"),
                "hypothesis_id": item.get("hypothesis_id"),
                "expression": _clip_text(item.get("expression"), 180),
                "expected_direction": item.get("expected_direction"),
                "mechanism_summary": _clip_text(item.get("mechanism_summary"), 140),
                "complexity_intent": item.get("complexity_intent"),
                "factor_name_hint": _clip_text(item.get("factor_name_hint"), 80),
            }))
        )
    compact_plan = _compact_llm_stage_result_for_prompt(lineage_context.get("candidate_plan")) if include_candidate_plan else {}
    include_handoff = _stage_allows_handoff_visibility(stage)
    filtered_advice = _filtered_handoffs_for_stage(lineage_context.get("previous_review_advice") or [], stage=stage)
    filtered_return = (
        _compact_return_handoff(lineage_context.get("return_reason_from_downstream"))
        if _handoff_targets_stage(lineage_context.get("return_reason_from_downstream"), stage)
        else {}
    )
    return _jsonable(
        {
            "current_thesis": theses,
            "current_hypothesis": hypotheses,
            "parent_candidates": parents,
            "candidate_plan": compact_plan,
            "previous_review_advice": filtered_advice if include_handoff else [],
            "return_reason_from_downstream": filtered_return if include_handoff else {},
        }
    )


def _compact_current_round_context_for_prompt(
    lineage_context: dict | None,
    *,
    stage: str | None = None,
    preferred_parent_refs: list[str] | None = None,
) -> dict:
    lineage = _compact_lineage_context_for_prompt(
        lineage_context,
        stage=stage,
        preferred_parent_refs=preferred_parent_refs,
    )
    stage_name = str(stage or "")
    candidates_live_in_tool_evidence = stage_name in {
        "candidate_plan",
        "score_review",
        "novelty_review",
        "deep_validation_review",
        "import_gate_review",
        "import_review",
    }
    return _jsonable(
        _prune_empty_prompt_values(
            {
                "thesis": lineage.get("current_thesis") or [],
                "hypotheses": lineage.get("current_hypothesis") or [],
                "candidate_drafts": (
                    []
                    if candidates_live_in_tool_evidence
                    else lineage.get("parent_candidates") or []
                ),
                "candidate_plan": lineage.get("candidate_plan") or {},
            }
        )
    )


def _compact_return_handoff(handoff: dict | None) -> dict:
    if not isinstance(handoff, dict):
        return {}
    # Historical traces and interrupted runs may still carry the old free-form
    # handoff shape (for example, ``preserve: rank(...)``).  Never let that
    # legacy text re-enter a design prompt as an instruction.  Preserve its
    # evidence references and candidate identities, then normalize it to the
    # same mechanism-only contract used by newly created handoffs.
    binding_policy = str(handoff.get("binding_policy") or "")
    supported_policies = {
        "mechanism_and_evidence_only_not_literal_expression_instruction",
        "targeted_parent_mutation",
        "direction_normalization_global_sign_flip_only",
        "previous_run_research_continuity",
    }
    if binding_policy not in supported_policies:
        legacy_parent_refs = _handoff_parent_candidate_refs(
            [
                *_list_prefix(handoff.get("parent_candidate_refs"), 6),
                *_list_prefix(handoff.get("must_preserve"), 6),
                handoff.get("reason"),
                handoff.get("recommended_mutation"),
            ]
        )
        handoff = _mechanism_level_handoff(
            from_stage=str(handoff.get("from_stage") or ""),
            to_stage=str(handoff.get("to_stage") or "thesis_design"),
            parent_candidate_refs=legacy_parent_refs,
            evidence_refs=handoff.get("supporting_evidence_refs") if isinstance(handoff.get("supporting_evidence_refs"), list) else [],
        )
        binding_policy = "mechanism_and_evidence_only_not_literal_expression_instruction"
    refs = handoff.get("supporting_evidence_refs") if isinstance(handoff.get("supporting_evidence_refs"), list) else []
    compact_refs: list[dict[str, Any]] = []
    for ref in refs[:3]:
        if not isinstance(ref, dict):
            continue
        entry = _compact_metric_ref(ref)
        if entry:
            compact_refs.append(_jsonable(entry))
    return _jsonable(
        {
            "from_stage": handoff.get("from_stage"),
            "to_stage": handoff.get("to_stage"),
            "binding_policy": binding_policy,
            "reason": _clip_text(handoff.get("reason"), 320),
            "previous_run_id": _clip_text(handoff.get("previous_run_id"), 80),
            "previous_round_id": _clip_text(handoff.get("previous_round_id"), 100),
            "summary": _clip_text(handoff.get("summary"), 420),
            "judgment": _clip_text(handoff.get("judgment"), 420),
            "why": _clip_text(handoff.get("why"), 620),
            "history_used": _compact_tool_evidence_leaf(handoff.get("history_used"), limit=4),
            "must_preserve": [_clip_text(item, 120) for item in _list_prefix(handoff.get("must_preserve"), 5) if str(item).strip()],
            "must_change": [_clip_text(item, 120) for item in _list_prefix(handoff.get("must_change"), 5) if str(item).strip()],
            "must_avoid": [_clip_text(item, 120) for item in _list_prefix(handoff.get("must_avoid"), 5) if str(item).strip()],
            "recommended_mutation": _clip_text(handoff.get("recommended_mutation"), 160),
            "parent_candidate_refs": [str(item) for item in _list_prefix(handoff.get("parent_candidate_refs"), 6) if str(item).strip()],
            "supporting_evidence_refs": compact_refs,
        }
    )


def _dedupe_compact_handoffs(handoffs: list[dict] | None, *, limit: int = 6) -> list[dict]:
    compacted: list[dict] = []
    seen: set[str] = set()
    for item in reversed(handoffs or []):
        compact = _compact_return_handoff(item)
        if not compact:
            continue
        key = json.dumps(
            {
                "from_stage": compact.get("from_stage"),
                "to_stage": compact.get("to_stage"),
                "reason": compact.get("reason"),
                "recommended_mutation": compact.get("recommended_mutation"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if key in seen:
            continue
        seen.add(key)
        compacted.append(compact)
        if len(compacted) >= limit:
            break
    compacted.reverse()
    return compacted


def _select_prompt_handoff(
    *,
    stage: str,
    previous_advice: list[dict] | None = None,
    return_handoff: dict | None = None,
) -> dict:
    if not _stage_allows_handoff_visibility(stage):
        return {}
    if stage == "expression_design":
        for item in reversed(previous_advice or []):
            compact = _compact_return_handoff(item)
            if compact.get("binding_policy") == "direction_normalization_global_sign_flip_only":
                return compact
    if _handoff_targets_stage(return_handoff, stage):
        return _compact_return_handoff(return_handoff)
    # Without a current round handoff, prefer the latest instruction targeted
    # exactly at this stage.  Only then fall back to an upstream-stage handoff
    # that legitimately flows down to the current stage.  This prevents an old
    # hypothesis return from hiding a newer expression-specific diagnosis.
    compacted = [
        _compact_return_handoff(item)
        for item in (previous_advice or [])
        if isinstance(item, dict)
    ]
    for item in reversed(compacted):
        if str(item.get("to_stage") or "") == str(stage or ""):
            return item
    for item in reversed(compacted):
        if _handoff_targets_stage(item, stage):
            return item
    return {}


def _latest_previous_run_research_handoff(context_pack: dict, *, stage: str) -> dict:
    """Expose one completed cross-run research step to a fresh run.

    In-run handoffs remain authoritative.  This fallback is used only for the
    first thesis decision, where the new run otherwise has no research memory
    of what the immediately preceding run actually learned.
    """

    if stage != "thesis_design" or not isinstance(context_pack, dict):
        return {}
    run_state = context_pack.get("run_state") if isinstance(context_pack.get("run_state"), dict) else {}
    current_run_id = str(run_state.get("run_id") or "").strip()
    recent_steps = [step for step in (context_pack.get("recent_steps") or []) if isinstance(step, dict)]
    research_stages = {
        "thesis_design",
        "hypothesis_design",
        "expression_design",
        "candidate_plan",
        "score_review",
        "novelty_review",
        "deep_validation_review",
        "import_gate_review",
        "import_review",
        "round_synthesis",
    }
    if any(
        str(step.get("run_id") or "").strip() == current_run_id
        and str(step.get("stage") or "") in research_stages
        and _history_step_is_substantive(step)
        for step in recent_steps
    ):
        return {}
    # A normal fresh start must not resurrect an earlier design/review step
    # from a run that has already reached a terminal checkpoint.  Explicit
    # ``resume_run_id`` owns interrupted recovery; Factor Map and short-term
    # history own cross-run learning.
    previous_run_id = next(
        (
            str(step.get("run_id") or "").strip()
            for step in recent_steps
            if str(step.get("run_id") or "").strip()
            and str(step.get("run_id") or "").strip() != current_run_id
        ),
        "",
    )
    if previous_run_id:
        previous_run_steps = [
            step
            for step in recent_steps
            if str(step.get("run_id") or "").strip() == previous_run_id
        ]
        if any(
            str(step.get("stage") or "").strip()
            in {"checkpoint_stop", "blocker", "blocker_review"}
            or str(_history_step_transition(step).get("next_stage") or "").strip()
            in _ORCHESTRATOR_TERMINAL_STAGES
            for step in previous_run_steps
        ):
            return {}
    for step in recent_steps:
        step_run_id = str(step.get("run_id") or "").strip()
        if not step_run_id or step_run_id == current_run_id:
            continue
        if previous_run_id and step_run_id != previous_run_id:
            continue
        previous_stage = str(step.get("stage") or "")
        if previous_stage not in research_stages or not _history_step_is_substantive(step):
            continue
        transition = _history_step_transition(step)
        summary = str(step.get("summary") or "").strip()
        judgment = str(transition.get("judgment") or "").strip()
        why = str(transition.get("why") or "").strip()
        transition_target = str(transition.get("next_stage") or "").strip()
        continuity_target = (
            transition_target
            if transition_target in _ORCHESTRATOR_RESUME_STAGES
            else "thesis_design"
        )
        parent_refs = _scope_handoff_candidate_refs(
            [
                str(item.get("candidate_id") or "").strip()
                for item in _orchestrator_candidate_lane_items(
                    step.get("candidate_lanes"),
                    limit=4,
                )
                if str(item.get("candidate_id") or "").strip()
            ],
            round_id=str(step.get("round_id") or ""),
        )
        return _compact_return_handoff(
            {
                "from_stage": previous_stage,
                "to_stage": continuity_target,
                "binding_policy": "previous_run_research_continuity",
                "reason": str(transition.get("reason") or summary or judgment),
                "previous_run_id": step_run_id,
                "previous_round_id": step.get("round_id"),
                "summary": summary,
                "judgment": judgment,
                "why": why,
                "history_used": transition.get("history_used") or step.get("history_used") or [],
                "parent_candidate_refs": parent_refs,
                "must_preserve": (
                    ["上一 run 的正式证据仍支持该经济机制；当前 Thesis 只重建 lineage，不重新换题。"]
                    if continuity_target != "thesis_design"
                    else []
                ),
                "must_change": (
                    [f"沿用正式 transition，在本 run 进入 {continuity_target} 完成指定修改。"]
                    if continuity_target != "thesis_design"
                    else []
                ),
                "recommended_mutation": (
                    f"CONTINUE:{continuity_target}"
                    if continuity_target != "thesis_design"
                    else "REASSESS:thesis_design"
                ),
            }
        )
    return {}


def _compact_llm_stage_result_for_prompt(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}
    transition = result.get("stage_transition") if isinstance(result.get("stage_transition"), dict) else {}
    compact: dict[str, Any] = {
        "stage": result.get("stage"),
        "decision": _clip_text(result.get("decision"), 120),
        "judgment": _clip_text(result.get("judgment"), 220),
        "why": _clip_text(result.get("why"), 220),
        "next_action": _clip_text(result.get("next_action"), 120),
        "next_stage": transition.get("next_stage"),
        "confidence": result.get("confidence"),
    }
    list_limits = {"theses": 4, "hypotheses": 4, "candidate_lanes": 10, "candidate_decisions": 10}
    for key in ("theses", "hypotheses", "candidate_lanes", "candidate_decisions"):
        value = result.get(key)
        if isinstance(value, list) and value:
            compact[key] = _compact_tool_evidence_leaf(value, limit=list_limits[key])
        elif key == "candidate_lanes" and isinstance(value, dict):
            compact[key] = _compact_tool_evidence_leaf(value, limit=10)
    alignment = result.get("code_advice_alignment")
    if isinstance(alignment, dict) and alignment:
        compact["code_advice_alignment"] = _compact_tool_evidence_leaf(alignment, limit=4)
    round_memory = result.get("round_memory")
    if isinstance(round_memory, dict) and round_memory:
        parent_refs = _handoff_parent_candidate_refs(
            [
                *_list_prefix(round_memory.get("promising_parents"), 6),
                round_memory.get("next_round_handoff"),
            ]
        )
        compact["round_memory"] = {
            "suggested_start_stage": str(round_memory.get("suggested_start_stage") or ""),
            "binding_policy": "mechanism_and_evidence_only_not_literal_expression_instruction",
            "parent_candidate_refs": parent_refs,
            "has_positive_lessons": bool(round_memory.get("positive_lessons")),
            "has_negative_lessons": bool(round_memory.get("negative_lessons")),
            "has_avoid_patterns": bool(round_memory.get("avoid_patterns")),
        }
    return _jsonable({key: value for key, value in compact.items() if value not in (None, "", [], {})})


def _compact_score_factor_result(candidate: dict) -> dict:
    compact = _compact_orchestrator_candidate_for_diagnosis(candidate)
    compact["validation"] = _clip_text(candidate.get("validation"), 120)
    return _jsonable(_prune_empty_prompt_values(compact))


def _compact_novelty_guard_for_prompt(guard: dict | None) -> dict:
    if not isinstance(guard, dict):
        return {}
    compact = {
        "allowed": guard.get("allowed"),
        "reason": guard.get("reason"),
        "novelty_score": guard.get("novelty_score") if guard.get("novelty_score") is not None else guard.get("score"),
        "matched_existing_factor": guard.get("matched_existing_factor"),
        "matched_existing_factor_id": guard.get("matched_existing_factor_id"),
        "matched_existing_factor_name": guard.get("matched_existing_factor_name"),
        "matched_existing_expression_summary": guard.get("matched_existing_expression_summary"),
        "matched_information_cluster_id": guard.get("matched_information_cluster_id"),
        "matched_region_uid": guard.get("matched_region_uid"),
        "factor_map_id": guard.get("factor_map_id"),
        "factor_map_audit_id": guard.get("factor_map_audit_id"),
        "matched_reference_source": guard.get("matched_reference_source"),
        "max_existing_pearson": guard.get("max_existing_pearson"),
        "max_existing_rank_corr": guard.get("max_existing_rank_corr"),
        "p90_pearson": guard.get("p90_pearson"),
        "p90_rank_corr": guard.get("p90_rank_corr"),
        "max_pearson": guard.get("max_pearson"),
        "max_rank_corr": guard.get("max_rank_corr"),
        "thresholds": guard.get("thresholds") or {},
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _compact_combined_guard_for_prompt(guard: dict | None) -> dict:
    if not isinstance(guard, dict):
        return {}
    compact = {
        "allowed": guard.get("allowed"),
        "reason": guard.get("reason"),
        "novelty_allowed": guard.get("novelty_allowed"),
        "st_exposure_allowed": guard.get("st_exposure_allowed"),
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _compact_st_exposure_guard_for_prompt(guard: dict | None) -> dict:
    if not isinstance(guard, dict):
        return {}
    compact = {
        "available": guard.get("available"),
        "passed": guard.get("passed"),
        "mode": guard.get("mode"),
        "reason": guard.get("reason"),
        "avg_top50_ratio": guard.get("avg_top50_ratio"),
        "p95_top50_ratio": guard.get("p95_top50_ratio"),
        "latest_top50_ratio": guard.get("latest_top50_ratio"),
        "long_only_side": guard.get("long_only_side"),
        "selected_group_is_flipped_low_side": guard.get("selected_group_is_flipped_low_side"),
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _compact_novelty_candidate_for_prompt(candidate: dict) -> dict:
    compact = _compact_orchestrator_candidate_for_diagnosis(candidate)
    novelty = _compact_novelty_guard_for_prompt(candidate.get("novelty_guard"))
    combined = _compact_combined_guard_for_prompt(candidate.get("combined_guard"))
    st_guard = _compact_st_exposure_guard_for_prompt(candidate.get("st_exposure_guard"))
    if novelty:
        compact["novelty_guard"] = novelty
    if combined:
        compact["combined_guard"] = combined
    if st_guard:
        compact["st_exposure_guard"] = st_guard
    matched_region_name = (
        candidate.get("matched_region_name")
        or (candidate.get("novelty_guard") or {}).get("matched_region_name")
    )
    if matched_region_name:
        compact["matched_region_name"] = _clip_text(matched_region_name, 100)
    return _jsonable(_prune_empty_prompt_values(compact))


def _compact_similarity_detail_for_prompt(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    guard = _compact_novelty_guard_for_prompt(item.get("novelty_guard"))
    compact = {
        "candidate_id": item.get("candidate_id"),
        "factor_name": _clip_text(item.get("factor_name") or item.get("name"), 80),
        "expression": _clip_text(item.get("expression"), 220),
        "reason": item.get("reason"),
        "matched_existing_factor": item.get("matched_existing_factor"),
        "matched_existing_factor_id": item.get("matched_existing_factor_id"),
        "matched_existing_factor_name": item.get("matched_existing_factor_name"),
        "matched_existing_expression_summary": item.get("matched_existing_expression_summary"),
        "matched_information_cluster_id": item.get("matched_information_cluster_id"),
        "matched_region_uid": item.get("matched_region_uid"),
        "matched_region_name": item.get("matched_region_name"),
        "factor_map_id": item.get("factor_map_id"),
        "factor_map_audit_id": item.get("factor_map_audit_id"),
        "matched_reference_source": item.get("matched_reference_source"),
        "max_existing_pearson": item.get("max_existing_pearson"),
        "max_existing_rank_corr": item.get("max_existing_rank_corr"),
        "p90_pearson": item.get("p90_pearson"),
        "p90_rank_corr": item.get("p90_rank_corr"),
        "max_pearson": item.get("max_pearson"),
        "max_rank_corr": item.get("max_rank_corr"),
        "thresholds": item.get("thresholds") or {},
        "novelty_guard": guard,
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _attach_factor_map_region_names(
    payload: dict | None,
    *,
    factor_map: dict | None,
    run_id: str,
) -> dict:
    if not isinstance(payload, dict):
        return {}
    projected = factor_map_design_context(factor_map, run_id=run_id)
    names = {
        str(item.get("region_uid") or ""): str(item.get("name") or "")
        for item in (projected.get("regions") or [])
        if isinstance(item, dict) and str(item.get("region_uid") or "")
    }
    if not names:
        return payload
    for key in ("keepers", "dropped", "details"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            guard = item.get("novelty_guard") if isinstance(item.get("novelty_guard"), dict) else {}
            region_uid = str(
                item.get("matched_region_uid")
                or guard.get("matched_region_uid")
                or ""
            )
            name = names.get(region_uid)
            if not name:
                continue
            item["matched_region_name"] = name
            if guard:
                guard["matched_region_name"] = name
    return payload


def _compact_novelty_result_payload(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    details = payload.get("details") if isinstance(payload.get("details"), list) else []
    compact = {
        "keepers": [_compact_novelty_candidate_for_prompt(item) for item in (payload.get("keepers") or [])[:4] if isinstance(item, dict)],
        "dropped": [_compact_novelty_candidate_for_prompt(item) for item in (payload.get("dropped") or [])[:6] if isinstance(item, dict)],
        "details": [_compact_similarity_detail_for_prompt(item) for item in details[:6] if isinstance(item, dict)],
        "feedback": _clip_text(payload.get("feedback"), 260),
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _compact_deep_result_payload(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    compact: dict[str, Any] = {}
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    if candidates:
        compact["candidates"] = [_compact_orchestrator_candidate_for_diagnosis(item) for item in candidates[:10] if isinstance(item, dict)]
    evidence_refs = payload.get("evidence_refs") if isinstance(payload.get("evidence_refs"), list) else []
    if evidence_refs:
        compact["evidence_refs"] = _compact_tool_evidence_leaf(evidence_refs, limit=10)
    missing_evidence = payload.get("missing_evidence") if isinstance(payload.get("missing_evidence"), list) else []
    if missing_evidence:
        compact["missing_evidence"] = [
            {
                "candidate_id": item.get("candidate_id"),
                "components": _list_prefix(item.get("components"), 8),
            }
            for item in missing_evidence[:6]
            if isinstance(item, dict)
        ]
    system_errors = payload.get("system_errors") if isinstance(payload.get("system_errors"), list) else []
    if system_errors:
        compact["system_errors"] = [
            _jsonable(_prune_empty_prompt_values({
                "candidate_id": item.get("candidate_id"),
                "status": item.get("status"),
                "error": _clip_text(item.get("error"), 260),
                "source_tool": item.get("source_tool"),
            }))
            for item in system_errors[:6]
            if isinstance(item, dict)
        ]
    return _jsonable(compact)


def _deep_evidence_diagnostics(candidates: list[dict] | None) -> tuple[list[dict], list[dict]]:
    missing: list[dict] = []
    system_errors: list[dict] = []
    required = {
        "backtest": "backtest_summary",
        "anti_overfit": "anti_overfit",
        "rolling_validation": "rolling_validation",
        "adversarial_validation": "adversarial_validation",
    }
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("candidate_id")
        missing_components = [
            label
            for label, key in required.items()
            if not isinstance(candidate.get(key), dict) or not candidate.get(key)
        ]
        for component in quality_gate.missing_deep_components(candidate):
            if component not in missing_components:
                missing_components.append(component)
        if missing_components:
            missing.append({"candidate_id": candidate_id, "components": missing_components})
        if candidate.get("error") or str(candidate.get("status") or "").endswith("_error"):
            system_errors.append({
                "candidate_id": candidate_id,
                "status": candidate.get("status"),
                "error": _clip_text(candidate.get("error"), 260),
                "source_tool": candidate.get("source_tool"),
            })
    return _jsonable(missing), _jsonable(system_errors)


def _compact_gate_result_payload(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    compact: dict[str, Any] = {}
    for key in ("adopted", "rejected", "screened_out", "failed", "imported"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            compact[key] = [_compact_orchestrator_candidate_for_diagnosis(item) for item in value[:10] if isinstance(item, dict)]
        elif value not in (None, "", [], {}):
            compact[key] = value
    errors = payload.get("errors")
    if errors:
        compact["errors"] = _compact_tool_evidence_leaf(errors, limit=4)
    details = payload.get("details")
    if details:
        compact["details"] = _compact_tool_evidence_leaf(details, limit=6)
    return _jsonable(compact)


def _compact_prompt_advice(advice: dict | None) -> dict:
    if not isinstance(advice, dict):
        return {}
    lanes = []
    for item in (advice.get("candidate_lane_decisions") or [])[:10]:
        if not isinstance(item, dict):
            continue
        mutation = item.get("mutation_advice")
        diagnosis = item.get("mutation") if isinstance(item.get("mutation"), dict) else {}
        evolution = item.get("evolution_strategy") if isinstance(item.get("evolution_strategy"), dict) else {}
        score_parts = item.get("score_parts") if isinstance(item.get("score_parts"), dict) else {}
        lanes.append(
            _jsonable(_prune_empty_prompt_values({
                "candidate_id": item.get("candidate_id") or item.get("idx"),
                "action": item.get("action"),
                "reason": _clip_text(item.get("reason"), 120),
                "evolution_strategy": {
                    "strategy": evolution.get("strategy"),
                    "action": evolution.get("action"),
                    "reason": evolution.get("reason"),
                } if evolution else {},
                "trajectory_progress": _compact_tool_evidence_leaf(
                    item.get("trajectory_progress") or {},
                    limit=8,
                ),
                "mutation_diagnosis": {
                    "strategy": diagnosis.get("strategy"),
                    "action": diagnosis.get("action"),
                    "reason": diagnosis.get("reason"),
                    "details": _compact_tool_evidence_leaf(diagnosis.get("details") or {}, limit=6),
                } if diagnosis else {},
                "weakest_component": item.get("weakest_component"),
                "score": item.get("score") if item.get("score") is not None else item.get("quick_score"),
                "grade": item.get("grade"),
                "deep_score": item.get("deep_score"),
                "gap_to_gate": item.get("gap_to_gate"),
                "lowest_component": item.get("lowest_component"),
                "lowest_component_score": item.get("lowest_component_score"),
                "lowest_component_reference_status": item.get("lowest_component_reference_status"),
                "rolling_score": item.get("rolling_score"),
                "rolling_grade": item.get("rolling_grade"),
                "rolling_status": item.get("rolling_status"),
                "score_parts": {
                    "official_score": score_parts.get("official_score"),
                    "official_grade": score_parts.get("official_grade"),
                    "component_scores": score_parts.get("component_scores") or {},
                    "component_weights": score_parts.get("component_weights") or {},
                    "weighted_contributions": score_parts.get("weighted_contributions") or {},
                } if score_parts else {},
                "novelty_score": item.get("novelty_score"),
                "mutation_advice": (
                    _clip_text((mutation or {}).get("instruction") or (mutation or {}).get("type"), 160)
                    if isinstance(mutation, dict)
                    else _clip_text(mutation, 160)
                ),
            }))
        )
    compact = {
        "action": advice.get("action") or advice.get("recommended_action"),
        "strategy": advice.get("strategy"),
        "evolution_strategy": advice.get("evolution_strategy") or {},
        "next_thesis_policy": _clip_text(advice.get("next_thesis_policy"), 180),
        "allowed_actions": (advice.get("allowed_actions") or [])[:6],
        "blocked_actions": (advice.get("blocked_actions") or [])[:6],
        "trajectory_metrics": advice.get("trajectory_metrics") or {},
        "recombination_candidates": _compact_tool_evidence_leaf(
            advice.get("recombination_candidates") or [],
            limit=5,
        ),
        "candidate_lane_decisions": lanes,
        "import_summary": _compact_tool_evidence_leaf(advice.get("import_summary") or {}, limit=4),
        "warnings": _compact_tool_evidence_leaf(advice.get("warnings") or [], limit=4),
        "next_round_suggestions": _compact_tool_evidence_leaf(advice.get("next_round_suggestions") or [], limit=4),
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _compact_library_information_context(
    value: dict | None,
    *,
    stage: str | None = None,
    family_limit: int | None = None,
    run_id: str = "",
    affected_only: bool = False,
    relevant_fields: set[str] | None = None,
) -> dict:
    projected = factor_map_design_context(value, run_id=run_id)
    if not projected.get("available"):
        return projected
    regions = [item for item in (projected.get("regions") or []) if isinstance(item, dict)]
    normalized_regions: list[dict] = []
    for item in regions:
        normalized = dict(item)
        guidance = normalized.get("guidance") if isinstance(normalized.get("guidance"), dict) else {}
        if str(guidance.get("action") or "none") == "none":
            normalized.pop("guidance", None)
        normalized_regions.append(normalized)
    regions = normalized_regions
    requested_fields = {
        str(field or "").strip().lower()
        for field in (relevant_fields or set())
        if str(field or "").strip()
    }
    if requested_fields:
        related_regions = []
        for item in regions:
            region_fields = {
                str(field.get("field") or "").strip().lower()
                for field in (item.get("core_fields") or [])
                if isinstance(field, dict) and str(field.get("field") or "").strip()
            }
            if region_fields.intersection(requested_fields):
                related_regions.append(item)
        regions = related_regions
    if affected_only:
        regions = [
            item for item in regions
            if str((item.get("guidance") or {}).get("action") or "none") != "none"
        ]
        if not regions:
            return {}
    if family_limit is not None:
        regions = regions[: max(1, int(family_limit))]
    projected["regions"] = regions
    projected["region_count"] = len(regions)
    projected["purpose"] = (
        "说明active库已覆盖的信息关系及当前run轨迹；"
        "用于理解研究背景，不用于机会排名或替代正式候选门槛。"
    )
    return _jsonable(_prune_empty_prompt_values(projected))


def _research_fields_from_round_context(value: dict | None) -> set[str]:
    fields: set[str] = set()
    context = value if isinstance(value, dict) else {}
    for thesis in context.get("thesis") or []:
        if not isinstance(thesis, dict):
            continue
        for field in thesis.get("preferred_data_families") or []:
            text = str(field or "").strip().lower()
            if text:
                fields.add(text)
    for hypothesis in context.get("hypotheses") or []:
        if not isinstance(hypothesis, dict):
            continue
        for group in hypothesis.get("candidate_variable_groups") or []:
            if not isinstance(group, dict):
                continue
            for field in group.get("variables") or []:
                text = str(field or "").strip().lower()
                if text:
                    fields.add(text)
    return fields


def _compact_stage_active_context_for_prompt(
    *,
    run_state: dict,
    active_context: dict,
    stage: str,
    relevant_fields: set[str] | None = None,
) -> dict:
    policy = _ORCHESTRATOR_STAGE_CONTEXT_POLICY.get(
        str(stage or ""),
        {"family_limit": 12, "sample_limit": 24},
    )
    field_context = active_context.get("field_context") if isinstance(active_context.get("field_context"), dict) else {}
    operator_guidance = (
        active_context.get("operator_guidance")
        if isinstance(active_context.get("operator_guidance"), dict)
        else {}
    )
    research_space_mode = str(policy.get("research_space") or "none")
    run_contract = run_state.get("contract") if isinstance(run_state.get("contract"), dict) else {}
    research_space = _candidate_context_research_space_for_stage(field_context, stage=stage)
    if research_space_mode == "fields":
        research_space = {
            key: value
            for key, value in research_space.items()
            if key in {"supported_fields", "blocked_fields", "field_constraints"}
        }
    elif research_space_mode == "none":
        research_space = {}
    compact = {
        "operator_research_direction": ({
            "value": run_contract.get("direction") or "auto",
            "mode": (
                "autonomous_topic_selection"
                if str(run_contract.get("direction") or "auto").strip().lower() == "auto"
                else "operator_constrained"
            ),
            "instruction": (
                "auto authorizes autonomous thesis selection."
                if str(run_contract.get("direction") or "auto").strip().lower() == "auto"
                else (
                    "Treat this operator direction as the binding research scope for thesis, hypothesis, "
                    "expression and candidate decisions. Returns may revise the mechanism within that scope, "
                    "but must not silently replace the requested direction."
                )
            ),
        } if run_contract else {}),
        "research_contract": {
            key: run_contract.get(key)
            for key in (
                "universe",
                "selection_start_date",
                "selection_end_date",
                "value_start_date",
                "value_end_date",
                "benchmark",
                "holding_period",
                "n_groups",
                "top_frac",
                "cost_rate",
                "rebalance_anchor",
                "neutralize_cap",
                "neutralize_industry",
                "target_adopted",
                "n_candidates",
                "n_rounds",
                "submit_wq",
            )
            if run_contract.get(key) is not None
        },
        "research_space": research_space,
        "factor_map_context": (
            _compact_library_information_context(
                active_context.get("factor_map_context"),
                stage=stage,
                family_limit=(
                    None
                    if policy.get("complete_factor_map")
                    else int(policy.get("family_limit") or 12)
                ),
                run_id=str(run_state.get("run_id") or ""),
                affected_only=policy.get("factor_map_mode") == "affected_only",
                relevant_fields=(
                    relevant_fields
                    if policy.get("factor_map_mode") == "related_only"
                    else None
                ),
            )
            if policy.get("factor_map")
            else {}
        ),
        # This field is populated only at the LLM-call boundary.  It is absent
        # again after the single delivery receipt has been written.
        "operator_guidance": operator_guidance,
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _orchestrator_stage_payload(
    *,
    stage: str,
    context_pack: dict,
    stage_input: dict | None = None,
    lineage_context: dict | None = None,
    round_events: list[dict] | None = None,
    return_handoff: dict | None = None,
) -> dict:
    run_state = context_pack.get("run_state") if isinstance(context_pack.get("run_state"), dict) else {}
    active_context = context_pack.get("active_context") if isinstance(context_pack.get("active_context"), dict) else {}
    protocol = context_pack.get("protocol") if isinstance(context_pack.get("protocol"), dict) else {}
    previous_advice = []
    if isinstance(lineage_context, dict):
        previous_advice = lineage_context.get("previous_review_advice") or []
    active_handoff = _select_prompt_handoff(
        stage=stage,
        previous_advice=previous_advice,
        return_handoff=return_handoff,
    )
    previous_run_handoff = _latest_previous_run_research_handoff(context_pack, stage=stage)
    if not active_handoff or str(active_handoff.get("from_stage") or "") == "orchestrator_interrupted":
        active_handoff = previous_run_handoff or active_handoff
    tool_evidence = _compact_stage_tool_evidence_for_prompt(
        stage=stage,
        stage_input=stage_input,
        round_events=round_events,
    )
    code_advice = {}
    if isinstance(tool_evidence, dict) and isinstance(tool_evidence.get("code_advice"), dict):
        tool_evidence = dict(tool_evidence)
        code_advice = tool_evidence.pop("code_advice") or {}
    history_context = _model_visible_stage_history(
        _compact_stage_history(context_pack, stage=stage, round_events=round_events),
        has_upstream_handoff=bool(active_handoff),
        stage=stage,
    )
    current_round_context = _compact_current_round_context_for_prompt(
        lineage_context,
        stage=stage,
        preferred_parent_refs=active_handoff.get("parent_candidate_refs")
        if isinstance(active_handoff, dict)
        else [],
    )
    relevant_fields = _research_fields_from_round_context(current_round_context)
    active_prompt_context = _compact_stage_active_context_for_prompt(
        run_state=run_state,
        active_context=active_context,
        stage=stage,
        relevant_fields=relevant_fields,
    )
    context_sections = _prune_empty_prompt_values(
        {
            "upstream_handoff": active_handoff,
            "current_round_context": current_round_context,
            "tool_evidence": tool_evidence,
            "code_advice": code_advice,
            "active_context": active_prompt_context,
            "history_context": history_context,
        }
    )
    payload = {
        "task": "fxalpha_orchestrator_stage",
        "stage": stage,
        "stage_briefing": _ORCHESTRATOR_STAGE_BRIEFINGS.get(stage, stage),
        "context_pack": context_sections,
        "output_contract": {
            "required_fields": _ORCHESTRATOR_STAGE_REQUIRED.get(stage, []),
            "allowed_next_stages": sorted(_ORCHESTRATOR_ALLOWED_NEXT_STAGES.get(stage, set())),
            "schema_example": _ORCHESTRATOR_STAGE_SCHEMAS.get(stage, {}),
            "strict_json": True,
        },
    }
    return _apply_orchestrator_context_budget(payload, stage=stage)


def _compact_event_for_prompt(event: dict) -> dict:
    if not isinstance(event, dict):
        return {}
    transition = event.get("stage_transition") if isinstance(event.get("stage_transition"), dict) else {}
    compact = {
        "ts": event.get("ts"),
        "stage": event.get("stage"),
        "summary": _clip_text(event.get("summary"), 180),
        "decision": _clip_text(event.get("decision"), 120),
        "next_stage": transition.get("next_stage"),
        "next_action": _clip_text(transition.get("next_action"), 120),
        "judgment": _clip_text(transition.get("judgment"), 160),
        "why": _clip_text(transition.get("why"), 160),
    }
    refs = event.get("evidence_refs") if isinstance(event.get("evidence_refs"), list) else []
    evidence = []
    for ref in refs[:4]:
        if not isinstance(ref, dict):
            continue
        entry = _compact_metric_ref(ref)
        if entry:
            evidence.append(entry)
    if evidence:
        compact["evidence_refs"] = evidence
    return _jsonable(_prune_empty_prompt_values(compact))


def _orchestrator_supported_operator_palette() -> list[str]:
    """Return the operator palette we explicitly expose to Orchestrator LLM stages."""
    from domain.factor_research.operator_palette import production_operator_palette

    return production_operator_palette()


def _orchestrator_operator_contract() -> dict:
    from domain.factor_research.operator_palette import production_operator_signatures

    signatures = production_operator_signatures()
    return {
        "signatures": signatures,
        "hard_rules": [
            "Use only an exact listed operator name and arity.",
            "ts_av_diff accepts exactly (x, window); it is not a fast-minus-slow moving-average operator.",
            "Use ts_std, not ts_stddev.",
            "Never place an unvalidated expression example in a downstream handoff.",
        ],
    }


def _compact_operator_list_summary_for_prompt(value: Any) -> dict:
    ops: list[str] = []
    if isinstance(value, list):
        ops = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, dict):
        for key in ("supported_operators", "operator_palette", "operators", "all_operators"):
            items = value.get(key)
            if isinstance(items, list):
                ops.extend(str(item).strip() for item in items if str(item).strip())
        if not ops:
            for _, items in list(value.items())[:8]:
                if isinstance(items, list):
                    ops.extend(str(item).strip() for item in items if str(item).strip())
    ops = list(dict.fromkeys(op for op in ops if op))
    if not ops:
        ops = _orchestrator_supported_operator_palette()

    normalization_ops = [
        op for op in ops if op in {"rank", "zscore", "group_rank", "group_zscore", "scale", "ts_zscore"}
    ]
    timeseries_ops = [
        op
        for op in ops
        if op
        in {
            "ts_mean",
            "ts_std",
            "ts_max",
            "ts_min",
            "ts_sum",
            "ts_shift",
            "ts_delta",
            "ts_rank",
            "ts_argmax",
            "ts_argmin",
            "ts_corr",
            "ts_cov",
            "ts_av_diff",
            "ts_zscore",
            "decay_linear",
            "ema",
            "sma",
            "wma",
        }
    ]
    nonlinear_ops = [op for op in ops if op in {"tanh", "sigmoid", "sign_power", "power", "clip"}]
    availability = {
        "cross_section_rank": "rank" in ops,
        "cross_section_zscore": "zscore" in ops,
        "group_rank": "group_rank" in ops,
        "group_zscore": "group_zscore" in ops,
        "time_series_zscore": "ts_zscore" in ops,
        "percentile_rank": False,
        "industry_neutralize": "indneutralize" in ops,
    }
    notes = []
    if availability["cross_section_zscore"]:
        notes.append("cross_section_zscore_available_via_zscore")
    if availability["group_zscore"]:
        notes.append("group_zscore_available_for_group_standardization")
    if availability["time_series_zscore"]:
        notes.append("ts_zscore_is_time_series_only")
    if not availability["percentile_rank"]:
        notes.append("percentile_rank_not_in_current_operator_palette")
    return _jsonable(
        {
            "supported_operators": ops,
            "operator_signatures": {
                key: value
                for key, value in _orchestrator_operator_contract()["signatures"].items()
                if key in ops
            },
            "normalization_operators": normalization_ops[:12],
            "time_series_operators": timeseries_ops[:18],
            "nonlinear_operators": nonlinear_ops[:10],
            "availability": availability,
            "notes": notes[:4],
        }
    )


def _compact_tool_evidence_leaf(value: Any, *, limit: int = 6) -> Any:
    if isinstance(value, list):
        compact = []
        decision_keys = {
            "action",
            "weakest_component",
            "mutation_advice",
            "novelty_interpretation",
            "gate_feedback_for_future",
            "lane",
            "keep",
            "fatal",
            "warnings",
            "instruction",
        }
        for item in value[:limit]:
            if isinstance(item, dict):
                if item.get("tool") and "expression" not in item:
                    compact.append(_compact_metric_ref(item))
                elif any(key in item for key in decision_keys):
                    compact.append(_compact_metric_ref(item))
                elif any(
                    key in item
                    for key in (
                        "matched_existing_factor",
                        "max_existing_pearson",
                        "max_existing_rank_corr",
                        "p90_pearson",
                        "p90_rank_corr",
                        "max_pearson",
                        "max_rank_corr",
                    )
                ):
                    compact.append(_compact_similarity_detail_for_prompt(item))
                elif "expression" in item or "candidate_id" in item or "factor_name" in item:
                    compact.append(_compact_orchestrator_candidate_for_diagnosis(item))
                else:
                    compact.append(_compact_metric_ref(item))
            else:
                compact.append(_clip_text(item, 180))
        return _jsonable([item for item in compact if item not in ({}, "", None)])
    if isinstance(value, dict):
        candidate_items = _orchestrator_candidate_lane_items(value, limit=limit)
        if candidate_items:
            compact_candidates = [_compact_orchestrator_candidate_for_diagnosis(item) for item in candidate_items[:limit]]
            lane_counts = _orchestrator_candidate_lane_counts(value)
            return _jsonable(
                {
                    "lane_counts": lane_counts,
                    "candidates": compact_candidates,
                }
            )
        if value.get("tool") or value.get("action"):
            metric_ref = _compact_metric_ref(value)
            if metric_ref:
                return _jsonable(metric_ref)
        compact = {}
        for key in (
            "candidate_id",
            "factor_name",
            "idx",
            "expression",
            "tool",
            "task_id",
            "action",
            "fatal",
            "warnings",
            "instruction",
            "score",
            "grade",
            "quick_score",
            "deep_score",
            "deep_action",
            "deep_reason",
            "ic",
            "icir",
            "rank_ic",
            "rank_icir",
            "novelty_score",
            "allowed",
            "reason",
            "weakest_component",
            "mutation_advice",
            "novelty_interpretation",
            "gate_feedback_for_future",
            "matched_existing_factor",
            "matched_existing_factor_id",
            "matched_existing_factor_name",
            "matched_existing_expression_summary",
            "matched_information_cluster_id",
            "matched_reference_source",
            "anti_overfit_score",
            "adversarial_score",
            "adopted",
            "rejected",
            "screened_out",
            "imported",
            "failed",
            "status",
        ):
            if value.get(key) not in (None, "", [], {}):
                compact[key] = value.get(key)
        if not compact:
            for key, item in list(value.items())[:8]:
                if isinstance(item, (str, int, float, bool)):
                    compact[key] = _clip_text(item, 180) if isinstance(item, str) else item
                elif isinstance(item, list):
                    compact[key] = _compact_tool_evidence_leaf(item, limit=3)
        return _jsonable(compact)
    if isinstance(value, str):
        return _clip_text(value, 220)
    return _jsonable(value)


def _preserve_candidate_plan_tool_evidence(tool_evidence: Any) -> dict[str, Any]:
    if not isinstance(tool_evidence, dict):
        return {}
    compact = {
        "candidates": [
            _compact_candidate_plan_candidate_for_prompt(item)
            for item in (tool_evidence.get("candidates") or [])
            if isinstance(item, dict)
        ],
        "code_precheck": _compact_tool_evidence_leaf(tool_evidence.get("code_precheck") or [], limit=8),
        "protected_parent_mutation_candidate_ids": [
            str(value)
            for value in (tool_evidence.get("protected_parent_mutation_candidate_ids") or [])
            if str(value).strip()
        ][:12],
        "selection_policy": _jsonable(tool_evidence.get("selection_policy") or {}),
        "operator_contract": _jsonable(tool_evidence.get("operator_contract") or {}),
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _preserve_score_review_tool_evidence(tool_evidence: Any) -> dict[str, Any]:
    """Keep the complete scored batch available to a score-review retry.

    A score review is a batch decision.  Reducing it to the first few rows on
    retry makes the model reason about a different experiment from the one the
    tools actually completed, and can silently lose a later keeper.  History
    may be shortened for recovery; final validate/score evidence may not.
    """
    if not isinstance(tool_evidence, dict):
        return {}
    compact = {
        "candidate_lanes": [
            _compact_orchestrator_candidate_for_diagnosis(item)
            for item in _orchestrator_candidate_lane_items(tool_evidence.get("candidate_lanes"), limit=10)
        ],
        "validate_results": _compact_tool_evidence_leaf(tool_evidence.get("validate_results") or [], limit=10),
        "score_factor_results": [
            _compact_score_factor_result(item)
            for item in (tool_evidence.get("score_factor_results") or [])[:10]
            if isinstance(item, dict)
        ],
        "trajectory_metrics": _compact_tool_evidence_leaf(tool_evidence.get("trajectory_metrics") or {}, limit=6),
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _preserve_round_synthesis_tool_evidence(tool_evidence: Any) -> dict[str, Any]:
    """Keep every completed current-round candidate visible on a retry.

    A formatting-only repair must not turn a scored candidate into an
    apparently unsubmitted candidate.  Historical prose may be shortened, but
    the current round's authoritative candidate set is atomic.
    """
    if not isinstance(tool_evidence, dict):
        return {}
    compact = {
        "authoritative_outcome": _jsonable(tool_evidence.get("authoritative_outcome") or {}),
        "failed_candidates": [
            _compact_orchestrator_candidate_for_diagnosis(item)
            for item in _orchestrator_candidate_lane_items(
                tool_evidence.get("failed_candidates"),
                limit=12,
            )
            if isinstance(item, dict)
        ],
        "imported_candidates": [
            _compact_orchestrator_candidate_for_diagnosis(item)
            for item in _orchestrator_candidate_lane_items(
                tool_evidence.get("imported_candidates"),
                limit=12,
            )
            if isinstance(item, dict)
        ],
        "tool_evidence_summary": [
            _compact_metric_ref(item)
            for item in _orchestrator_candidate_lane_items(
                tool_evidence.get("tool_evidence_summary"),
                limit=12,
            )
            if isinstance(item, dict)
        ],
        "llm_decision_chain": _compact_tool_evidence_leaf(
            tool_evidence.get("llm_decision_chain") or [],
            limit=12,
        ),
        "candidate_plan_summary": _jsonable(tool_evidence.get("candidate_plan_summary") or {}),
        "precheck_summary": _jsonable(tool_evidence.get("precheck_summary") or {}),
        "operator_contract": _jsonable(tool_evidence.get("operator_contract") or {}),
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _compact_metric_ref(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    keys = (
        "idx",
        "candidate_id",
        "factor_name",
        "expression",
        "tool",
        "task_id",
        "status",
        "action",
        "fatal",
        "warnings",
        "instruction",
        "score",
        "grade",
        "quick_score",
        "deep_score",
        "deep_action",
        "deep_reason",
        "ic",
        "icir",
        "rank_ic",
        "rank_icir",
        "annual_return",
        "sharpe",
        "max_drawdown",
        "turnover",
        "novelty_score",
        "allowed",
        "anti_overfit_score",
        "adversarial_score",
        "gate_decision",
        "passed",
        "adopted",
        "reason",
        "feedback",
        "weakest_component",
        "mutation_advice",
        "novelty_interpretation",
        "gate_feedback_for_future",
        "lane",
        "keep",
        "note",
        "matched_existing_factor",
        "matched_existing_factor_id",
        "matched_existing_factor_name",
        "matched_existing_expression_summary",
        "matched_information_cluster_id",
        "matched_reference_source",
        "matched_candidate_ids",
        "matched_cluster_id",
        "matched_factor_ids",
        "decision_source",
        "candidate_plan_action",
    )
    return {
        key: (_clip_text(item.get(key), 220) if isinstance(item.get(key), str) else item.get(key))
        for key in keys
        if item.get(key) not in (None, "", [], {})
    }


def _compact_round_synthesis_stage_input(stage_input: dict | None, round_events: list[dict] | None = None) -> dict:
    stage_input = stage_input if isinstance(stage_input, dict) else {}
    compact: dict[str, Any] = {}
    if stage_input.get("reason"):
        compact["reason"] = _clip_text(stage_input.get("reason"), 180)
    if stage_input.get("authoritative_outcome"):
        compact["authoritative_outcome"] = _compact_tool_evidence_leaf(stage_input.get("authoritative_outcome"), limit=4)
    if stage_input.get("failed_candidates"):
        failed_candidates = stage_input.get("failed_candidates")
        compact["failed_candidates"] = [
            _compact_orchestrator_candidate_for_diagnosis(item)
            for item in _orchestrator_candidate_lane_items(failed_candidates, limit=4)
        ] or _compact_tool_evidence_leaf(failed_candidates, limit=4)
    if stage_input.get("code_precheck_summary"):
        compact["code_precheck_summary"] = _compact_tool_evidence_leaf(stage_input.get("code_precheck_summary"), limit=4)
    elif stage_input.get("code_precheck"):
        compact["code_precheck_summary"] = _candidate_plan_code_precheck_summary(stage_input.get("code_precheck") or [])
    if stage_input.get("code_precheck"):
        compact["code_precheck"] = _compact_tool_evidence_leaf(stage_input.get("code_precheck") or [], limit=6)
    if stage_input.get("adopted_factors"):
        compact["adopted_factors"] = [
            _compact_orchestrator_candidate_for_diagnosis(item)
            for item in _orchestrator_candidate_lane_items(stage_input.get("adopted_factors"), limit=4)
        ]
    tool_summary = stage_input.get("tool_evidence_summary")
    if isinstance(tool_summary, dict):
        compact["tool_evidence_summary"] = {
            key: _compact_tool_evidence_leaf(value, limit=4)
            for key, value in tool_summary.items()
        }
    elif tool_summary is not None:
        compact["tool_evidence_summary"] = _compact_tool_evidence_leaf(tool_summary, limit=4)
    llm_chain = stage_input.get("llm_decision_chain") if isinstance(stage_input.get("llm_decision_chain"), list) else (round_events or [])
    compact["llm_decision_chain"] = [
        _compact_event_for_prompt(item)
        for item in llm_chain[-3:]
        if isinstance(item, dict)
    ]
    code_advice = stage_input.get("code_advice") if isinstance(stage_input.get("code_advice"), dict) else {}
    if not code_advice:
        outcome = stage_input.get("authoritative_outcome") if isinstance(stage_input.get("authoritative_outcome"), dict) else {}
        next_stage = str(outcome.get("required_next_stage") or "").strip()
        next_action = str(outcome.get("required_next_action") or "").strip()
        stage_suggestion = {
            "thesis_design": "Start a genuinely different economic thesis; do not recycle the failed expression family.",
            "hypothesis_design": "Keep the viable thesis, revise the testable signal claim and confirmation mechanism.",
            "expression_design": "Keep the viable parent mechanism, mutate the expression as specified by the downstream handoff.",
            "checkpoint_stop": "Stop at the explicit checkpoint; do not invent another research round.",
        }.get(next_stage)
        suggestions = [stage_suggestion] if stage_suggestion else []
        if next_action:
            suggestions.append(f"Code-required next action: {next_action}")
        code_advice = {
            "next_round_suggestions": suggestions,
        }
    compact["code_advice"] = _compact_prompt_advice(code_advice)
    return _jsonable(_prune_empty_prompt_values(compact))


def _authoritative_round_outcome(
    *,
    from_stage: str,
    decision: str,
    next_stage: str,
    next_action: str,
    reason: str = "",
) -> dict:
    return {
        "from_stage": from_stage,
        "decision": decision,
        "required_next_stage": next_stage,
        "required_next_action": next_action,
        "reason": _clip_text(reason, 220),
    }


def _stage_context_budget(stage: str) -> dict[str, int]:
    budget = dict(_ORCHESTRATOR_CONTEXT_BUDGET_DEFAULT)
    stage_budget = _ORCHESTRATOR_CONTEXT_BUDGETS.get(str(stage or ""), {})
    for key, value in stage_budget.items():
        try:
            budget[key] = int(value)
        except Exception:
            continue
    return budget


def _json_payload_chars(payload: Any) -> int:
    try:
        return len(json.dumps(_jsonable(payload), ensure_ascii=False, default=str))
    except Exception:
        return len(str(payload or ""))


def _prompt_context_budget(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    budget = payload.get("_context_budget")
    if isinstance(budget, dict):
        return budget
    budget = payload.get("context_budget")
    return budget if isinstance(budget, dict) else {}


def _llm_visible_payload(payload: dict | None) -> dict:
    clean = _jsonable(payload if isinstance(payload, dict) else {})
    if not isinstance(clean, dict):
        return {}
    clean.pop("_context_budget", None)
    clean.pop("context_budget", None)
    return clean


def _limit_context_list(value: Any, *, limit: int) -> Any:
    if isinstance(value, list):
        return [_jsonable(item) for item in value[: max(0, limit)]]
    return value


def _prune_empty_prompt_values(value: Any) -> Any:
    """Drop prompt-only empty placeholders while preserving False/0 evidence."""

    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            pruned = _prune_empty_prompt_values(item)
            if pruned not in (None, "", [], {}):
                compact[key] = pruned
        return compact
    if isinstance(value, list):
        compact_list = []
        for item in value:
            pruned = _prune_empty_prompt_values(item)
            if pruned not in (None, "", [], {}):
                compact_list.append(pruned)
        return compact_list
    return value


def _apply_orchestrator_context_budget(payload: dict, *, stage: str) -> dict:
    clean = _jsonable(payload if isinstance(payload, dict) else {})
    if not isinstance(clean, dict):
        clean = {}
    budget = _stage_context_budget(stage)
    max_chars = int(budget.get("max_payload_chars") or 48000)
    before_chars = _json_payload_chars(clean)
    budget_stats = {
        "stage": stage,
        "max_payload_chars": max_chars,
        "before_chars": before_chars,
        "compressed": False,
    }
    if before_chars <= max_chars:
        budget_stats["after_chars"] = before_chars
        clean["_context_budget"] = budget_stats
        return clean

    compact = dict(clean)
    context_pack = compact.get("context_pack") if isinstance(compact.get("context_pack"), dict) else {}
    context_pack = dict(context_pack)
    history_limit = int(budget.get("history_limit") or 6)
    candidate_limit = int(budget.get("candidate_limit") or 6)
    tool_limit = int(budget.get("tool_evidence_limit") or 6)

    history = context_pack.get("history_context") if isinstance(context_pack.get("history_context"), dict) else {}
    if history:
        compact_history = dict(history)
        short_term = dict(compact_history.get("short_term_history") if isinstance(compact_history.get("short_term_history"), dict) else {})
        for key in (
            "stage_relevant_steps",
            "review_anchors",
            "positive_precedents",
            "recent_same_round_events",
        ):
            short_term[key] = _limit_context_list(short_term.get(key), limit=history_limit)
        negative = short_term.get("negative_precedents")
        if isinstance(negative, dict):
            short_term["negative_precedents"] = {
                "weak_fields": _limit_context_list(negative.get("weak_fields"), limit=min(8, history_limit)),
                "weak_candidates": _compact_tool_evidence_leaf(negative.get("weak_candidates") or [], limit=min(3, history_limit)),
                "deep_near_misses": _compact_tool_evidence_leaf(negative.get("deep_near_misses") or [], limit=min(3, history_limit)),
                "novelty_vetoes": _compact_tool_evidence_leaf(negative.get("novelty_vetoes") or [], limit=min(3, history_limit)),
                "policy": _limit_context_list(negative.get("policy"), limit=3),
            }
        compact_history["short_term_history"] = _jsonable(short_term)
        context_pack["history_context"] = _jsonable(compact_history)

    current_round = context_pack.get("current_round_context") if isinstance(context_pack.get("current_round_context"), dict) else {}
    if current_round:
        compact_current = dict(current_round)
        for key in ("thesis", "hypotheses", "candidate_drafts"):
            compact_current[key] = _limit_context_list(compact_current.get(key), limit=candidate_limit)
        context_pack["current_round_context"] = _jsonable(compact_current)

    if "tool_evidence" in context_pack:
        if stage == "candidate_plan":
            context_pack["tool_evidence"] = _preserve_candidate_plan_tool_evidence(context_pack.get("tool_evidence"))
        elif stage == "round_synthesis":
            context_pack["tool_evidence"] = _preserve_round_synthesis_tool_evidence(context_pack.get("tool_evidence"))
        else:
            context_pack["tool_evidence"] = _compact_tool_evidence_leaf(context_pack.get("tool_evidence"), limit=tool_limit)
    if "code_advice" in context_pack:
        context_pack["code_advice"] = _compact_tool_evidence_leaf(context_pack.get("code_advice"), limit=tool_limit)

    compact["context_pack"] = context_pack
    after_chars = _json_payload_chars(compact)
    if after_chars > max_chars:
        short_term = (history or {}).get("short_term_history") if isinstance((history or {}).get("short_term_history"), dict) else {}
        context_pack["history_context"] = {
            "short_term_history": {
                "latest_round_handoff": (short_term or {}).get("latest_round_handoff") or {},
                "stage_relevant_steps": _limit_context_list((short_term or {}).get("stage_relevant_steps"), limit=2),
                "review_anchors": _limit_context_list((short_term or {}).get("review_anchors"), limit=2),
            },
        }
        if stage == "candidate_plan":
            context_pack["tool_evidence"] = _preserve_candidate_plan_tool_evidence(context_pack.get("tool_evidence"))
        elif stage == "round_synthesis":
            context_pack["tool_evidence"] = _preserve_round_synthesis_tool_evidence(context_pack.get("tool_evidence"))
        else:
            context_pack["tool_evidence"] = _compact_tool_evidence_leaf(context_pack.get("tool_evidence"), limit=min(3, tool_limit))
        context_pack["code_advice"] = _compact_tool_evidence_leaf(context_pack.get("code_advice") or {}, limit=min(3, tool_limit))
        compact["context_pack"] = context_pack
        after_chars = _json_payload_chars(compact)
    if after_chars > max_chars:
        short_term = (history or {}).get("short_term_history") if isinstance((history or {}).get("short_term_history"), dict) else {}
        context_pack["history_context"] = {
            "short_term_history": {
                "stage_relevant_steps": _compact_tool_evidence_leaf((short_term or {}).get("stage_relevant_steps") or [], limit=1),
                "review_anchors": _compact_tool_evidence_leaf((short_term or {}).get("review_anchors") or [], limit=1),
            },
        }
        current_round = context_pack.get("current_round_context") if isinstance(context_pack.get("current_round_context"), dict) else {}
        context_pack["current_round_context"] = {
            "handoff": (current_round or {}).get("handoff") or {},
        }
        if stage == "candidate_plan":
            context_pack["tool_evidence"] = _preserve_candidate_plan_tool_evidence(context_pack.get("tool_evidence"))
        elif stage == "round_synthesis":
            context_pack["tool_evidence"] = _preserve_round_synthesis_tool_evidence(context_pack.get("tool_evidence"))
        else:
            context_pack["tool_evidence"] = _compact_tool_evidence_leaf(context_pack.get("tool_evidence"), limit=1)
        context_pack["code_advice"] = _compact_tool_evidence_leaf(context_pack.get("code_advice") or {}, limit=1)
        compact["context_pack"] = context_pack
        output_contract = compact.get("output_contract") if isinstance(compact.get("output_contract"), dict) else {}
        compact["output_contract"] = {
            "required_fields": _limit_context_list(output_contract.get("required_fields"), limit=12),
            "allowed_next_stages": _limit_context_list(output_contract.get("allowed_next_stages"), limit=12),
            "strict_json": output_contract.get("strict_json", True),
        }
        after_chars = _json_payload_chars(compact)

    budget_stats.update(
        {
            "after_chars": after_chars,
            "compressed": True,
            "history_limit": history_limit,
            "candidate_limit": candidate_limit,
            "tool_evidence_limit": tool_limit,
        }
    )
    compact["_context_budget"] = budget_stats
    return _jsonable(compact)


def _compact_stage_tool_evidence_for_prompt(
    *,
    stage: str,
    stage_input: dict | None,
    round_events: list[dict] | None = None,
) -> dict:
    if not isinstance(stage_input, dict):
        return {}
    if stage == "thesis_design":
        compact = {
            "blocked_or_failed_reasons": _compact_return_handoff(stage_input.get("blocked_or_failed_reasons")),
            "available_field_families": [str(item) for item in (stage_input.get("available_field_families") or [])[:18] if str(item).strip()],
            "target_constraints": stage_input.get("target_constraints") or {},
        }
        return _jsonable(_prune_empty_prompt_values(compact))
    if stage == "hypothesis_design":
        compact = {
            "field_requirements": _candidate_context_field_requirements(stage_input.get("field_context") or {}),
            "operator_constraints": _clip_text(stage_input.get("operator_constraints"), 220),
        }
        return _jsonable(_prune_empty_prompt_values(compact))
    if stage == "expression_design":
        compact = {
            "operator_list_summary": _compact_operator_list_summary_for_prompt(stage_input.get("operator_list_summary") or {}),
            "field_requirements": _candidate_context_field_requirements(stage_input.get("field_context") or {}),
            "expression_rules": _clip_text(stage_input.get("expression_rules"), 220),
            "complexity_limits": stage_input.get("complexity_limits") or {},
            "diversity_budget": stage_input.get("diversity_budget") or {},
            "candidate_budget": stage_input.get("candidate_budget") or {},
            "prior_expression_history": stage_input.get("prior_expression_history") or {},
        }
        return _jsonable(_prune_empty_prompt_values(compact))
    if stage == "candidate_plan":
        compact = {
            "candidates": [
                _compact_candidate_plan_candidate_for_prompt(item)
                for item in (stage_input.get("candidates") or [])
                if isinstance(item, dict)
            ],
            "code_precheck": _compact_tool_evidence_leaf(stage_input.get("code_precheck") or [], limit=8),
            "protected_parent_mutation_candidate_ids": [
                str(value)
                for value in (stage_input.get("protected_parent_mutation_candidate_ids") or [])
                if str(value).strip()
            ][:12],
            "selection_policy": stage_input.get("selection_policy") or {},
            "operator_contract": _orchestrator_operator_contract(),
        }
        return _jsonable(_prune_empty_prompt_values(compact))
    if stage == "round_synthesis":
        compact = _compact_round_synthesis_stage_input(stage_input, round_events=round_events)
        compact["operator_contract"] = _orchestrator_operator_contract()
        return compact
    if stage == "score_review":
        compact = {
            "candidate_lanes": [
                _compact_orchestrator_candidate_for_diagnosis(item)
                for item in _orchestrator_candidate_lane_items(stage_input.get("candidate_lanes"), limit=10)
            ],
            "validate_results": _compact_tool_evidence_leaf(stage_input.get("validate_results") or [], limit=10),
            "score_factor_results": [_compact_score_factor_result(item) for item in (stage_input.get("score_factor_results") or [])[:10] if isinstance(item, dict)],
            "trajectory_metrics": stage_input.get("trajectory_metrics") or {},
            "code_advice": _compact_prompt_advice(stage_input.get("code_advice")),
        }
        return _jsonable(_prune_empty_prompt_values(compact))
    if stage == "novelty_review":
        compact = {
            "score_review_summary": _compact_llm_stage_result_for_prompt(stage_input.get("score_review_summary")),
            "novelty_results": _compact_novelty_result_payload(stage_input.get("novelty_results")),
            "batch_similarity": _compact_tool_evidence_leaf(stage_input.get("batch_similarity") or [], limit=4),
            "active_pool_similarity": _compact_tool_evidence_leaf(stage_input.get("active_pool_similarity") or [], limit=4),
            "code_advice": _compact_prompt_advice(stage_input.get("code_advice")),
        }
        return _jsonable(_prune_empty_prompt_values(compact))
    if stage == "deep_validation_review":
        compact = {
            "score_review_summary": _compact_llm_stage_result_for_prompt(stage_input.get("score_review_summary")),
            "novelty_review_summary": _compact_llm_stage_result_for_prompt(stage_input.get("novelty_review_summary")),
            "deep_results": _compact_deep_result_payload(stage_input.get("deep_results")),
            "trajectory_metrics": stage_input.get("trajectory_metrics") or {},
            "code_advice": _compact_prompt_advice(stage_input.get("code_advice")),
        }
        return _jsonable(_prune_empty_prompt_values(compact))
    if stage == "import_gate_review":
        compact = {
            "deep_review_summary": _compact_llm_stage_result_for_prompt(stage_input.get("deep_review_summary")),
            "quality_gate_results": _compact_gate_result_payload(stage_input.get("quality_gate_results")),
            "metadata_check": _compact_tool_evidence_leaf(stage_input.get("metadata_check") or {}, limit=4),
            "missing_evidence": _compact_tool_evidence_leaf(stage_input.get("missing_evidence") or [], limit=6),
            "code_advice": _compact_prompt_advice(stage_input.get("code_advice")),
        }
        return _jsonable(_prune_empty_prompt_values(compact))
    if stage == "import_review":
        compact = {
            "gate_review_summary": _compact_llm_stage_result_for_prompt(stage_input.get("gate_review_summary")),
            "import_results": _compact_gate_result_payload(stage_input.get("import_results")),
            "registry_summary": _compact_tool_evidence_leaf(stage_input.get("registry_summary") or {}, limit=4),
            "import_sync_status": _compact_tool_evidence_leaf(stage_input.get("import_sync_status") or {}, limit=4),
            "adopted_total": stage_input.get("adopted_total"),
            "code_advice": _compact_prompt_advice(stage_input.get("code_advice")),
        }
        return _jsonable(_prune_empty_prompt_values(compact))
    return _jsonable(
        _prune_empty_prompt_values(
            {
                key: _compact_tool_evidence_leaf(value, limit=6)
                for key, value in stage_input.items()
            }
        )
    )


def _is_retryable_llm_contract_error(exc: Exception) -> bool:
    category = str(getattr(exc, "category", "") or "")
    if category:
        return category in {"empty_content", "json_parse_error", "schema_contract_error"}
    message = str(exc or "")
    return any(
        token in message
        for token in (
            "empty_llm_response",
            "llm_response_not_json",
            "llm_response_not_valid_json",
            "missing_required_fields",
            "next_stage_not_allowed",
            "candidates_required",
            "candidate_expression_missing",
            "candidate_lanes_required",
            "candidate_lane_missing_ids",
            "candidate_lane_missing_candidate_ids",
            "precheck_blocked_requires_code_fatal",
        )
    )


def _filter_expression_design_exact_repeats(
    result: dict,
    *,
    stage_input: dict | None,
    require_one_unique: bool,
) -> None:
    """Remove exact same-run repeats before they consume candidate-plan work.

    The full normalized history stays private and never inflates the model
    prompt.  A first response containing only repeats is retried with an
    explicit correction; if the repair still repeats everything, the normal
    deterministic candidate-plan precheck remains the final authority instead
    of turning research history into a runtime blocker.
    """

    if not isinstance(result, dict) or not isinstance(stage_input, dict):
        return
    prior_refs = stage_input.get("_private_prior_expression_refs")
    if not isinstance(prior_refs, dict) or not prior_refs:
        return
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return
    unique: list[dict] = []
    duplicates: list[dict[str, str]] = []
    for idx, item in enumerate(candidates):
        if not isinstance(item, dict):
            unique.append(item)
            continue
        expression = str(item.get("expression") or "").strip()
        normalized = _normalize_symbolic_expression(expression)
        matched = prior_refs.get(normalized) if normalized else None
        if not isinstance(matched, dict):
            unique.append(item)
            continue
        duplicates.append(
            {
                "candidate_id": _candidate_id(item, idx),
                "expression": _clip_text(expression, 260),
                "matched_round_id": str(matched.get("round_id") or ""),
            }
        )
    if not duplicates:
        return
    warning = "exact_prior_round_candidates_removed:" + ",".join(
        item["candidate_id"] for item in duplicates
    )
    result.setdefault("_orchestrator_validation_warnings", []).append(warning)
    if unique:
        result["candidates"] = unique
        return
    if require_one_unique:
        details = ";".join(
            f"{item['candidate_id']}={item['expression']}@{item['matched_round_id']}"
            for item in duplicates[:8]
        )
        raise DeepSeekClientError(
            f"expression_design:all_candidates_exact_prior_round:{details}",
            category="schema_contract_error",
        )


def _shrink_orchestrator_stage_payload_for_retry(
    payload: dict,
    *,
    stage: str,
    correction_reason: str = "",
) -> dict:
    compact = _jsonable(payload if isinstance(payload, dict) else {})
    stage_input = compact.get("stage_input") if isinstance(compact.get("stage_input"), dict) else {}
    context_pack = compact.get("context_pack") if isinstance(compact.get("context_pack"), dict) else {}
    history = context_pack.get("history_context") if isinstance(context_pack.get("history_context"), dict) else {}
    if history:
        compact_history = dict(history)
        short_term = dict(compact_history.get("short_term_history") if isinstance(compact_history.get("short_term_history"), dict) else {})
        if isinstance(short_term.get("stage_relevant_steps"), list):
            short_term["stage_relevant_steps"] = short_term["stage_relevant_steps"][:4]
        if isinstance(short_term.get("positive_precedents"), list):
            short_term["positive_precedents"] = short_term["positive_precedents"][:2]
        if isinstance(short_term.get("recent_same_round_events"), list):
            short_term["recent_same_round_events"] = short_term["recent_same_round_events"][:3]
        negative = short_term.get("negative_precedents")
        if isinstance(negative, dict):
            short_term["negative_precedents"] = {
                "weak_fields": (negative.get("weak_fields") or [])[:4],
                "weak_candidates": _compact_tool_evidence_leaf(negative.get("weak_candidates") or [], limit=2),
                "deep_near_misses": _compact_tool_evidence_leaf(negative.get("deep_near_misses") or [], limit=2),
                "novelty_vetoes": _compact_tool_evidence_leaf(negative.get("novelty_vetoes") or [], limit=2),
                "policy": (negative.get("policy") or [])[:2],
            }
        compact_history["short_term_history"] = _jsonable(short_term)
        context_pack["history_context"] = _jsonable(compact_history)
    current_round = context_pack.get("current_round_context") if isinstance(context_pack.get("current_round_context"), dict) else {}
    if current_round:
        compact_current = dict(current_round)
        for key, limit in {"thesis": 2, "hypotheses": 3, "candidate_drafts": 4}.items():
            if isinstance(compact_current.get(key), list):
                compact_current[key] = compact_current[key][:limit]
        context_pack["current_round_context"] = _jsonable(compact_current)
    tool_evidence = context_pack.get("tool_evidence")
    if tool_evidence is not None:
        if stage == "candidate_plan":
            context_pack["tool_evidence"] = _preserve_candidate_plan_tool_evidence(tool_evidence)
        elif stage == "score_review":
            context_pack["tool_evidence"] = _preserve_score_review_tool_evidence(tool_evidence)
        elif stage == "round_synthesis":
            context_pack["tool_evidence"] = _preserve_round_synthesis_tool_evidence(tool_evidence)
        else:
            context_pack["tool_evidence"] = _compact_tool_evidence_leaf(tool_evidence, limit=4)
    compact["context_pack"] = context_pack
    output_contract = compact.get("output_contract") if isinstance(compact.get("output_contract"), dict) else {}
    candidate_ids: list[str] = []
    if stage == "candidate_plan":
        candidates = ((context_pack.get("tool_evidence") or {}).get("candidates") or [])
        candidate_ids = [
            str(item.get("candidate_id") or "").strip()
            for item in candidates
            if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
        ]
    elif stage == "score_review":
        score_evidence = ((context_pack.get("tool_evidence") or {}).get("score_factor_results") or [])
        candidate_ids = [
            str(item.get("candidate_id") or "").strip()
            for item in score_evidence
            if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
        ]
    elif stage == "round_synthesis":
        round_candidates = [
            *((context_pack.get("tool_evidence") or {}).get("failed_candidates") or []),
            *((context_pack.get("tool_evidence") or {}).get("imported_candidates") or []),
        ]
        candidate_ids = [
            str(item.get("candidate_id") or "").strip()
            for item in round_candidates
            if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
        ]
    retry_instruction = (
        "上一轮返回未通过 JSON/schema/stage_transition 校验。现在只返回一个合法 JSON object，"
        "严格遵守 required_fields、schema_example 和 allowed_next_stages，不要输出任何解释文字。"
        "summary、judgment、why、history_used 和 stage_transition.reason 必须是可直接展示的自然中文。"
        "history_used 只能写实际采用的历史研究结论，绝对不能复制 history_context、tool_evidence、"
        "current_round_context、active_context 或 code_advice 等内部字段路径；无法自然描述时返回空数组。"
        "stage_transition 必须是 JSON object，例如 {\"next_stage\":\"expression_design\",\"reason\":\"...\"}，"
        "不能返回字符串。candidate_lanes 必须是逐候选 JSON array，例如 "
        "[{\"candidate_id\":\"c1\",\"action\":\"score\",\"keep\":true,\"reason\":\"无法确认重复，送真实快筛\"}]；"
        "skip_batch_duplicate 必须给 matched_candidate_ids，skip_library_near_copy 必须给 matched_cluster_id 和 matched_factor_ids。"
        "不能返回 {\"planned_for_score\":[...]} 这种分组 dict。"
    )
    if stage == "score_review" and candidate_ids:
        retry_instruction += (
            "本次快筛已完成；不得省略任何已评分候选。candidate_decisions 与 "
            "required_candidate_ids 中的每个 candidate_id 必须一一对应。"
        )
    if stage == "round_synthesis" and candidate_ids:
        retry_instruction += (
            "本次仅修复格式，不得改变已完成实验事实。failed_candidates/imported_candidates 中的"
            " required_candidate_ids 已全部执行；不得把其中任何候选写成未提交、未评分或不存在。"
        )
    if stage == "candidate_plan":
        retry_instruction += (
            "precheck_blocked 只能用于 code_precheck 中 fatal=true 的候选。"
            "若你发现非 fatal 候选存在明确方向、where 分支或 hypothesis 语义矛盾，"
            "只把对应 candidate_lanes 标成 action=revise_expression、keep=false；"
            "其余合格候选继续 score。只有没有任何候选可评分时，批次才返回 expression_design。"
            "若 code_precheck 出现 ambiguous_centered_leg_product，说明多个零中心腿直接相乘会同时"
            "奖励双高和双低，不能表达 AND 式联合确认；只退回对应候选，不得阻塞合格兄弟候选。"
            "若 code_precheck 出现 definite_hypothesis_direction_mismatch，代码已证明该单调字段腿"
            "与 hypothesis.direction 相反；对应候选必须 revise_expression，不能进入 score。"
        )
    if stage == "expression_design":
        retry_instruction += (
            "如果 correction 指出全部候选都与前序 round 完全重复，你必须二选一："
            "输出至少一个不在 exact_do_not_repeat 中的合法新候选；或者返回 decision=blocked、"
            "candidates=[]、next_action=block_for_human、stage_transition.next_stage=blocker_review。"
            "绝不能返回 propose_candidates 配空 candidates，也不能再次输出 correction 已列出的表达式。"
        )
        targeted_refs = (
            (stage_input or {}).get("_private_targeted_parent_refs")
            if isinstance(stage_input, dict)
            else []
        )
        if targeted_refs:
            retry_instruction += (
                "本次是 EXPLOIT/targeted parent mutation。只能输出1-2个候选；"
                "每个候选的 parent_candidate_id 必须引用 targeted_parent_refs 中的候选，"
                "并且只改变 upstream_handoff.must_change 指定的一项。不得混入其他 hypothesis 或新主线。"
            )
    if stage == "hypothesis_design":
        retry_instruction += (
            "每个 hypothesis 的 thesis_id 必须存在于 selected_theses，且候选变量中至少一个主信息字段"
            "必须来自该 thesis 的 preferred_data_families。不得只复用 thesis_id 而改写成无关经济主线。"
            "candidate_variable_groups 每项必须保留 role、fields 和 direction；direction 只用"
            " positive 或 negative。即使本次 correction 只要求修自然语言，也不能删除这些字段。"
        )
    compact["retry_contract"] = {
        "reason": "previous_response_violated_json_or_stage_contract",
        "correction": _clip_text(correction_reason, 1200),
        "stage": stage,
        "instruction": retry_instruction,
        "required_fields": output_contract.get("required_fields") or [],
        "allowed_next_stages": output_contract.get("allowed_next_stages") or [],
        "required_candidate_ids": candidate_ids,
        "targeted_parent_refs": (
            (stage_input or {}).get("_private_targeted_parent_refs") or []
            if isinstance(stage_input, dict)
            else []
        ),
        "candidate_lanes_shape": "array_of_objects_covering_every_required_candidate_id",
    }
    return _jsonable(compact)


def _normalize_stage_transition_shape(stage: str, result: dict) -> None:
    if not isinstance(result, dict):
        return
    raw = result.get("stage_transition")
    if isinstance(raw, dict):
        return
    if not isinstance(raw, str):
        return
    next_stage = raw.strip()
    allowed = _ORCHESTRATOR_ALLOWED_NEXT_STAGES.get(stage, set())
    if next_stage not in allowed:
        return
    result["stage_transition"] = {
        "next_stage": next_stage,
        "reason": "模型将阶段流转简写成字符串，系统已按允许路径还原。",
    }
    next_action = str(result.get("next_action") or "").strip()
    if not next_action or next_action in {"continue_research", "continue_next_round"}:
        result["next_action"] = _next_action_for_resume_stage(next_stage)
    result.setdefault("_orchestrator_validation_warnings", []).append(
        "stage_transition_string_normalized_to_object"
    )


def _normalize_candidate_plan_lanes_shape(result: dict, stage_input: dict | None = None) -> None:
    if not isinstance(result, dict):
        return
    lanes = result.get("candidate_lanes")
    if not isinstance(lanes, dict):
        return
    candidates = stage_input.get("candidates") if isinstance(stage_input, dict) else []
    candidate_by_id = {
        str(item.get("candidate_id") or "").strip(): item
        for item in (candidates or [])
        if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
    }

    def _ids(name: str) -> list[str]:
        raw = lanes.get(name)
        if not isinstance(raw, list):
            return []
        values: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                candidate_id = str(item.get("candidate_id") or item.get("id") or "").strip()
            else:
                candidate_id = str(item or "").strip()
            if candidate_id:
                values.append(candidate_id)
        return values

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    lane_specs = (
        ("planned_for_score", True, "primary", "planned_for_score"),
        ("precheck_warning", True, "variant", "precheck_warning"),
        ("precheck_blocked", False, "blocked", "precheck_blocked"),
        ("candidate_plan_dropped", False, "dropped", "candidate_plan_dropped"),
        ("dropped", False, "dropped", "candidate_plan_dropped"),
    )
    for key, keep, lane_name, reason in lane_specs:
        for candidate_id in _ids(key):
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            item = {
                "candidate_id": candidate_id,
                "lane": lane_name,
                "keep": keep,
                "reason": reason,
            }
            source = candidate_by_id.get(candidate_id) or {}
            if source.get("expression"):
                item["expression"] = source.get("expression")
            normalized.append(item)
    if not normalized:
        return
    result["candidate_lanes"] = normalized
    result.setdefault("_orchestrator_validation_warnings", []).append(
        "candidate_lanes_grouped_dict_normalized_to_list"
    )
    planned_ids = {item["candidate_id"] for item in normalized if item.get("keep") and item.get("lane") != "blocked"}
    transition = result.get("stage_transition") if isinstance(result.get("stage_transition"), dict) else {}
    if planned_ids:
        result["next_action"] = "validate_and_score"
        transition = dict(transition)
        transition["next_stage"] = "score_review"
        transition.setdefault("reason", "normalized_grouped_candidate_lanes_with_planned_for_score")
        result["stage_transition"] = transition


def _enforce_conservative_candidate_plan_lanes(result: dict, stage_input: dict | None) -> None:
    if not isinstance(result, dict) or not isinstance(stage_input, dict):
        return
    lanes = result.get("candidate_lanes") if isinstance(result.get("candidate_lanes"), list) else []
    candidates = [item for item in (stage_input.get("candidates") or []) if isinstance(item, dict)]
    candidate_by_id = {_candidate_id(item, idx): item for idx, item in enumerate(candidates)}
    candidate_ids = set(candidate_by_id)
    fatal_ids = {
        str(item.get("candidate_id") or "").strip()
        for item in (stage_input.get("code_precheck") or [])
        if isinstance(item, dict) and item.get("fatal")
    }
    definite_direction_revision_ids = {
        str(item.get("candidate_id") or "").strip()
        for item in (stage_input.get("code_precheck") or [])
        if isinstance(item, dict)
        and not item.get("fatal")
        and any(
            str(warning or "").startswith("definite_hypothesis_direction_mismatch:")
            for warning in (item.get("warnings") or [])
        )
    }
    parameter_only_matches = {
        str(item.get("candidate_id") or "").strip(): {
            str(value or "").strip()
            for value in (item.get("matched_candidate_ids") or [])
            if str(value or "").strip()
        }
        for item in (stage_input.get("code_precheck") or [])
        if isinstance(item, dict)
        and not item.get("fatal")
        and any(str(warning or "").startswith("batch_parameter_only_variant:") for warning in (item.get("warnings") or []))
    }
    information = stage_input.get("factor_map_context") if isinstance(stage_input.get("factor_map_context"), dict) else {}
    information_available = bool(information.get("available"))
    protected_parent_mutation_ids = {
        str(value or "").strip()
        for value in (stage_input.get("protected_parent_mutation_candidate_ids") or [])
        if str(value or "").strip()
    }
    clusters = [
        item
        for item in [
            *(information.get("regions") or information.get("information_families") or []),
            *(information.get("redundancy_clusters") or []),
        ]
        if isinstance(item, dict)
    ]
    cluster_members = {
        str(cluster.get("cluster_id") or ""): {
            str(member.get("factor_id") or ""): member
            for member in (cluster.get("members") or [])
            if isinstance(member, dict) and str(member.get("factor_id") or "")
        }
        for cluster in clusters
        if str(cluster.get("cluster_id") or "")
    }
    requested_actions = {
        str(item.get("candidate_id") or item.get("id") or "").strip(): _candidate_plan_lane_action(item)
        for item in lanes
        if isinstance(item, dict) and str(item.get("candidate_id") or item.get("id") or "").strip()
    }
    warnings = result.setdefault("_orchestrator_validation_warnings", [])
    for item in lanes:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or item.get("id") or "").strip()
        action = _candidate_plan_lane_action(item)
        if candidate_id in fatal_ids:
            item.update({"action": "precheck_blocked", "lane": "precheck_blocked", "keep": False})
            continue
        if candidate_id in definite_direction_revision_ids:
            item.update({"action": "revise_expression", "lane": "semantic_revision", "keep": False})
            item["reason"] = (
                "代码真值检查确认至少一个单调字段腿的正负号与 hypothesis.direction 相反；"
                "该候选必须先修正方向，不能进入 quick score。"
            )
            warnings.append(f"candidate_plan_direction_mismatch_forced_revision:{candidate_id}")
            continue
        if action == "revise_expression":
            item.update({"action": "revise_expression", "lane": "semantic_revision", "keep": False})
            if not str(item.get("reason") or "").strip():
                item["reason"] = "候选表达式与假设或方向语义不一致，单独退回表达式设计修正。"
            continue
        if candidate_id in protected_parent_mutation_ids:
            item.update({"action": "score", "lane": "warning", "keep": True})
            item["reason"] = str(item.get("reason") or "") + "；有效 parent 定向微调按保守策略送 quick score。"
            warnings.append(f"candidate_plan_parent_mutation_forced_to_score:{candidate_id}")
            continue
        if candidate_id in parameter_only_matches:
            valid_matches = {
                ref_id
                for ref_id in parameter_only_matches[candidate_id] & (candidate_ids - {candidate_id})
                if requested_actions.get(ref_id, "score") == "score"
                and _parameter_agnostic_expression_key((candidate_by_id.get(candidate_id) or {}).get("expression"))
                == _parameter_agnostic_expression_key((candidate_by_id.get(ref_id) or {}).get("expression"))
            }
            if valid_matches:
                item.update({"action": "skip_batch_duplicate", "lane": "candidate_plan_dropped", "keep": False})
                item["matched_candidate_ids"] = sorted(valid_matches)
                item["reason"] = (
                    "代码确认该候选与批内保留代表只有窗口或数值参数不同；"
                    "当前候选没有有效 parent 定向时间尺度实验依据，不消耗 quick score。"
                )
                warnings.append(f"candidate_plan_parameter_only_variant_skipped:{candidate_id}")
                continue
        if action == "skip_batch_duplicate":
            matched = {
                str(value or "").strip()
                for value in (item.get("matched_candidate_ids") or [])
                if str(value or "").strip()
            }
            valid_matches = {
                ref_id
                for ref_id in matched & (candidate_ids - {candidate_id})
                if requested_actions.get(ref_id) == "score"
                and _candidate_plan_symbolic_skip_compatible(candidate_by_id.get(candidate_id), candidate_by_id.get(ref_id))
            }
            if not valid_matches or not str(item.get("reason") or "").strip():
                item.update({"action": "score", "lane": "warning", "keep": True})
                item["reason"] = str(item.get("reason") or "") + "；批内重复证据不足，按保守策略送 quick score。"
                warnings.append(f"candidate_plan_skip_fail_open_to_score:{candidate_id}:batch_evidence_missing")
            else:
                item.update({"action": "skip_batch_duplicate", "lane": "candidate_plan_dropped", "keep": False})
                item["matched_candidate_ids"] = sorted(valid_matches)
            continue
        if action == "skip_library_near_copy":
            cluster_id = str(item.get("matched_cluster_id") or "").strip()
            matched_factors = {
                str(value or "").strip()
                for value in (item.get("matched_factor_ids") or [])
                if str(value or "").strip()
            }
            member_map = cluster_members.get(cluster_id, {})
            valid_factors = {
                factor_id
                for factor_id in matched_factors & set(member_map)
                if _candidate_plan_symbolic_skip_compatible(candidate_by_id.get(candidate_id), member_map.get(factor_id))
            }
            if not information_available or not cluster_id or not valid_factors or not str(item.get("reason") or "").strip():
                item.update({"action": "score", "lane": "warning", "keep": True})
                item["reason"] = str(item.get("reason") or "") + "；库内近似证据不足，按保守策略送 quick score。"
                warnings.append(f"candidate_plan_skip_fail_open_to_score:{candidate_id}:library_evidence_missing")
            else:
                item.update({"action": "skip_library_near_copy", "lane": "candidate_plan_dropped", "keep": False})
                item["matched_factor_ids"] = sorted(valid_factors)
            continue
        item.update({"action": "score", "keep": True})
        if str(item.get("lane") or "") in {"precheck_blocked", "candidate_plan_dropped", "dropped"}:
            item["lane"] = "warning"


def _candidate_plan_symbolic_skip_compatible(left: dict | None, right: dict | None) -> bool:
    """Require the same field channel and core operator set before honoring an LLM skip.

    This is an evidence-consistency check, not a novelty calculation. Window,
    numeric constant, or redundant outer-wrapper changes may still be treated
    as duplicates; a changed field source or normalization/operator channel
    fails open to empirical score.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_sig = _candidate_symbolic_signature(left)
    right_sig = _candidate_symbolic_signature(right)
    if not left_sig.get("normalized") or not right_sig.get("normalized"):
        return False
    return (
        set(left_sig.get("fields") or []) == set(right_sig.get("fields") or [])
        and set(left_sig.get("operators") or []) == set(right_sig.get("operators") or [])
    )


def _hypothesis_variable_names(hypothesis: dict | None) -> set[str]:
    if not isinstance(hypothesis, dict):
        return set()
    variables: set[str] = set()
    for group in hypothesis.get("candidate_variable_groups") or []:
        if not isinstance(group, dict):
            continue
        for value in group.get("variables") or []:
            name = str(value or "").strip()
            if name:
                variables.add(name)
        roles = group.get("roles") if isinstance(group.get("roles"), dict) else {}
        for value in roles:
            name = str(value or "").strip()
            if name:
                variables.add(name)
        for key in ("fields", "primary", "confirming", "condition", "field"):
            value = group.get(key)
            if isinstance(value, list):
                values = value
            else:
                values = [value]
            for item in values:
                name = str(item or "").strip()
                if name:
                    variables.add(name)
    return variables


def _validate_hypothesis_thesis_alignment(
    result: dict,
    stage_input: dict | None,
) -> None:
    selected_theses = (
        stage_input.get("selected_theses")
        if isinstance(stage_input, dict)
        and isinstance(stage_input.get("selected_theses"), list)
        else []
    )
    if not selected_theses:
        return
    thesis_by_id = {
        str(item.get("thesis_id") or "").strip(): item
        for item in selected_theses
        if isinstance(item, dict) and str(item.get("thesis_id") or "").strip()
    }
    failures: list[str] = []
    for hypothesis in result.get("hypotheses") or []:
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = str(hypothesis.get("hypothesis_id") or "").strip() or "unknown"
        thesis_id = str(hypothesis.get("thesis_id") or "").strip()
        thesis = thesis_by_id.get(thesis_id)
        if thesis is None:
            failures.append(f"{hypothesis_id}:unknown_thesis_id={thesis_id}")
            continue
        preferred = {
            str(value or "").strip()
            for value in (thesis.get("preferred_data_families") or [])
            if str(value or "").strip()
        }
        variables = _hypothesis_variable_names(hypothesis)
        if preferred and variables and preferred.isdisjoint(variables):
            failures.append(
                f"{hypothesis_id}:thesis={thesis_id}:"
                f"preferred={','.join(sorted(preferred))}:"
                f"hypothesis_fields={','.join(sorted(variables))}"
            )
        for group_idx, group in enumerate(hypothesis.get("candidate_variable_groups") or []):
            if not isinstance(group, dict):
                failures.append(f"{hypothesis_id}:group_{group_idx + 1}:invalid_group")
                continue
            fields = [
                str(value or "").strip()
                for value in (group.get("fields") or [])
                if str(value or "").strip()
            ]
            direction = str(group.get("direction") or "").strip().lower()
            if not fields:
                failures.append(f"{hypothesis_id}:group_{group_idx + 1}:fields_required")
            if direction not in {"positive", "negative"}:
                failures.append(
                    f"{hypothesis_id}:group_{group_idx + 1}:"
                    "direction_must_be_positive_or_negative"
                )
    if failures:
        raise DeepSeekClientError(
            "hypothesis_design:thesis_semantic_alignment_failed:" + ";".join(failures[:6]),
            category="schema_contract_error",
        )


def _validate_targeted_expression_parent_contract(
    result: dict,
    stage_input: dict | None,
) -> None:
    parent_refs = (
        stage_input.get("_private_targeted_parent_refs")
        if isinstance(stage_input, dict)
        and isinstance(stage_input.get("_private_targeted_parent_refs"), list)
        else []
    )
    parent_ids = {
        _candidate_ref_id(value)
        for value in parent_refs
        if _candidate_ref_id(value)
    }
    if not parent_ids or str(result.get("decision") or "") == "blocked":
        return
    candidates = [
        item for item in (result.get("candidates") or []) if isinstance(item, dict)
    ]
    if len(candidates) > 2:
        raise DeepSeekClientError(
            "expression_design:targeted_mutation_allows_at_most_2_candidates",
            category="schema_contract_error",
        )
    invalid = [
        str(item.get("candidate_id") or "unknown")
        for item in candidates
        if _candidate_ref_id(item.get("parent_candidate_id")) not in parent_ids
    ]
    if invalid:
        raise DeepSeekClientError(
            "expression_design:targeted_mutation_requires_referenced_parent:"
            + ",".join(invalid),
            category="schema_contract_error",
        )


def _validate_orchestrator_stage_result(stage: str, result: dict, *, stage_input: dict | None = None) -> None:
    if not isinstance(result, dict):
        raise DeepSeekClientError(f"{stage}:llm_result_must_be_object", category="schema_contract_error")
    _normalize_stage_transition_shape(stage, result)
    if stage == "candidate_plan":
        _normalize_candidate_plan_lanes_shape(result, stage_input=stage_input)
    missing = [key for key in _ORCHESTRATOR_STAGE_REQUIRED.get(stage, []) if key not in result]
    transition = result.get("stage_transition") if isinstance(result.get("stage_transition"), dict) else {}
    if not transition.get("next_stage"):
        missing.append("stage_transition.next_stage")
    if stage in _ORCHESTRATOR_ALLOWED_NEXT_STAGES:
        next_stage = str(transition.get("next_stage") or "")
        allowed = _ORCHESTRATOR_ALLOWED_NEXT_STAGES[stage]
        if next_stage not in allowed:
            raise DeepSeekClientError(f"{stage}:next_stage_not_allowed:{next_stage}", category="schema_contract_error")
    if missing:
        raise DeepSeekClientError(f"{stage}:missing_required_fields:{','.join(missing)}", category="schema_contract_error")
    transition_reason = str(transition.get("reason") or "").strip()
    if not transition_reason:
        raise DeepSeekClientError(
            f"{stage}:missing_required_fields:stage_transition.reason",
            category="schema_contract_error",
        )
    natural_text_fields = {
        "summary": str(result.get("summary") or "").strip(),
        "judgment": str(result.get("judgment") or "").strip(),
        "why": str(result.get("why") or "").strip(),
        "stage_transition.reason": transition_reason,
    }
    history_used = result.get("history_used")
    # Normalize the explanatory receipt in-process.  Its container shape is
    # irrelevant to the research decision and therefore must not spend a
    # second LLM call.  Preserve the model's words; only coerce string/list,
    # remove empty items, and apply the display bound.
    history_values = history_used if isinstance(history_used, list) else [history_used]
    history_text = [
        str(item or "").strip()
        for item in history_values
        if str(item or "").strip()
    ][:6]
    result["history_used"] = history_text
    for field_name, field_value in natural_text_fields.items():
        if not field_value or not re.search(r"[\u4e00-\u9fff]", field_value):
            raise DeepSeekClientError(
                f"{stage}:{field_name}_must_be_natural_chinese",
                category="schema_contract_error",
            )
    # ``history_used`` is an explanatory receipt, not an execution input.
    # Asking for natural Chinese remains part of the stage briefing, but prose
    # style must never trigger a second LLM call or alter a valid research
    # transition.  The July 25 production canary spent most of its repair calls
    # only rewriting internal evidence references in this field, whereas the
    # July 20 successful runs did not gate research on display prose.
    forbidden_paths = (
        "history_context.",
        "tool_evidence.",
        "active_context.",
        "current_round_context.",
        "code_advice.",
    )
    combined_natural_text = "\n".join([*natural_text_fields.values(), *history_text]).lower()
    leaked_paths = [path for path in forbidden_paths if path in combined_natural_text]
    if leaked_paths:
        # Prose quality must not override a valid machine decision or stop the
        # research state machine. Keep the model-authored text unchanged for
        # auditability, record the contract warning, and let code hard guards
        # remain authoritative for the actual transition.
        result.setdefault("_orchestrator_validation_warnings", []).append(
            f"natural_language_fields_contain_internal_paths:{','.join(leaked_paths)}"
        )
    if stage in _ORCHESTRATOR_REVIEW_STAGES:
        # Ignore any self-reported alignment from the model.  Preserve the
        # historical result field for dashboards, but derive it from the actual
        # code advice and returned candidate decisions.
        result["code_advice_alignment"] = _derive_code_advice_alignment(
            stage,
            result,
            stage_input,
        )
    if stage == "hypothesis_design":
        _validate_hypothesis_thesis_alignment(result, stage_input)
    if stage == "expression_design" and str(result.get("decision") or "") != "blocked":
        candidates = result.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise DeepSeekClientError("expression_design:candidates_required", category="schema_contract_error")
        bad = [
            idx
            for idx, item in enumerate(candidates)
            if not isinstance(item, dict) or not str(item.get("expression") or "").strip()
        ]
        if bad:
            raise DeepSeekClientError(f"expression_design:candidate_expression_missing:indexes={bad}", category="schema_contract_error")
        _validate_targeted_expression_parent_contract(result, stage_input)
    if stage == "candidate_plan":
        lanes = result.get("candidate_lanes")
        if not isinstance(lanes, list) or not lanes:
            raise DeepSeekClientError("candidate_plan:candidate_lanes_required", category="schema_contract_error")
        missing_ids = [
            idx
            for idx, item in enumerate(lanes)
            if not isinstance(item, dict) or not str(item.get("candidate_id") or item.get("id") or "").strip()
        ]
        if missing_ids:
            raise DeepSeekClientError(
                f"candidate_plan:candidate_lane_missing_ids:indexes={missing_ids}",
                category="schema_contract_error",
            )
        if isinstance(stage_input, dict):
            expected_ids = {
                str(item.get("candidate_id") or "").strip()
                for item in (stage_input.get("candidates") or [])
                if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
            }
            seen_ids = {
                str(item.get("candidate_id") or item.get("id") or "").strip()
                for item in lanes
                if isinstance(item, dict) and str(item.get("candidate_id") or item.get("id") or "").strip()
            }
            missing_candidate_ids = sorted(expected_ids - seen_ids)
            if missing_candidate_ids:
                raise DeepSeekClientError(
                    f"candidate_plan:candidate_lane_missing_candidate_ids:{','.join(missing_candidate_ids)}",
                    category="schema_contract_error",
                )
            fatal_ids = {
                str(item.get("candidate_id") or "").strip()
                for item in (stage_input.get("code_precheck") or [])
                if isinstance(item, dict)
                and item.get("fatal")
                and str(item.get("candidate_id") or "").strip()
            }
            invalid_model_blocks = sorted(
                {
                    str(item.get("candidate_id") or item.get("id") or "").strip()
                    for item in lanes
                    if isinstance(item, dict)
                    and _candidate_plan_lane_action(item) == "precheck_blocked"
                    and str(item.get("candidate_id") or item.get("id") or "").strip() not in fatal_ids
                }
            )
            if invalid_model_blocks:
                if _candidate_plan_requests_upstream_return(result):
                    for item in lanes:
                        if not isinstance(item, dict):
                            continue
                        candidate_id = str(
                            item.get("candidate_id") or item.get("id") or ""
                        ).strip()
                        if candidate_id not in invalid_model_blocks:
                            continue
                        item.update(
                            {
                                "action": "score",
                                "lane": "warning",
                                "keep": True,
                            }
                        )
                        item["reason"] = (
                            str(item.get("reason") or "")
                            + "；模型引用了不存在的代码 fatal，系统已按 fail-open 规则恢复为快筛候选。"
                        )
                    result.setdefault("_orchestrator_validation_warnings", []).append(
                        "candidate_plan_fake_code_blocks_failed_open_to_score:"
                        + ",".join(invalid_model_blocks)
                    )
                else:
                    raise DeepSeekClientError(
                        "candidate_plan:precheck_blocked_requires_code_fatal:"
                        + ",".join(invalid_model_blocks)
                        + ";return_expression_design_for_clear_semantic_mismatch",
                        category="schema_contract_error",
                    )
            ambiguous_centered_ids = sorted(
                {
                    str(item.get("candidate_id") or "").strip()
                    for item in (stage_input.get("code_precheck") or [])
                    if isinstance(item, dict)
                    and str(item.get("candidate_id") or "").strip()
                    and any(
                        str(warning or "").startswith("ambiguous_centered_leg_product:")
                        for warning in (item.get("warnings") or [])
                    )
                }
            )
            if ambiguous_centered_ids:
                for item in lanes:
                    if not isinstance(item, dict):
                        continue
                    candidate_id = str(
                        item.get("candidate_id") or item.get("id") or ""
                    ).strip()
                    if candidate_id not in ambiguous_centered_ids:
                        continue
                    item.update(
                        {
                            "action": "revise_expression",
                            "lane": "semantic_revision",
                            "keep": False,
                        }
                    )
                    item["reason"] = (
                        str(item.get("reason") or "")
                        + "；代码确认多个零中心腿直接相乘存在方向歧义，该候选单独退回修正。"
                    )
                result.setdefault("_orchestrator_validation_warnings", []).append(
                    "candidate_plan_ambiguous_centered_leg_routed_to_per_candidate_revision:"
                    + ",".join(ambiguous_centered_ids)
                )
            _enforce_conservative_candidate_plan_lanes(result, stage_input)


def _complete_orchestrator_stage_json(
    *,
    client: DeepSeekJSONClient,
    run_id: str,
    round_id: str,
    stage: str,
    context_pack: dict,
    stage_input: dict | None = None,
    lineage_context: dict | None = None,
    round_events: list[dict] | None = None,
    return_handoff: dict | None = None,
    temperature: float = 0.12,
    max_tokens: int = 1800,
) -> dict:
    context_pack = _context_pack_with_pending_operator_guidance(context_pack, run_id=run_id)
    payload = _orchestrator_stage_payload(
        stage=stage,
        context_pack=context_pack,
        stage_input=stage_input,
        lineage_context=lineage_context,
        round_events=round_events,
        return_handoff=return_handoff,
    )
    try:
        # A retry must never be given a smaller output envelope than the
        # original call.  The same required JSON object still has to fit, and
        # shrinking it turns a recoverable syntax error into deterministic
        # truncation for multi-candidate review stages.
        retry_max_tokens = max_tokens
        result = _complete_orchestrator_llm_json(
            client=client,
            run_id=run_id,
            round_id=round_id,
            stage=stage,
            checkpoint=stage,
            system=_ORCHESTRATOR_RESEARCH_SYSTEM,
            payload=payload,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if stage == "expression_design":
            _filter_expression_design_exact_repeats(
                result,
                stage_input=stage_input,
                require_one_unique=True,
            )
        _validate_orchestrator_stage_result(stage, result, stage_input=stage_input)
        return result
    except DeepSeekClientError as exc:
        if stage == "blocker_review" or not _is_retryable_llm_contract_error(exc):
            raise
        retry_payload = _shrink_orchestrator_stage_payload_for_retry(
            payload,
            stage=stage,
            correction_reason=str(exc),
        )
        result = _complete_orchestrator_llm_json(
            client=client,
            run_id=run_id,
            round_id=round_id,
            stage=stage,
            checkpoint=f"{stage}_repair",
            system=_ORCHESTRATOR_RESEARCH_SYSTEM,
            payload=retry_payload,
            temperature=0.0,
            max_tokens=retry_max_tokens,
        )
        if stage == "expression_design":
            _filter_expression_design_exact_repeats(
                result,
                stage_input=stage_input,
                require_one_unique=False,
            )
        _validate_orchestrator_stage_result(stage, result, stage_input=stage_input)
        return result


def _llm_transition(result: dict, *, default_next_stage: str, default_next_action: str, llm_model: str | None = None) -> dict:
    transition = result.get("stage_transition") if isinstance(result.get("stage_transition"), dict) else {}
    history_used = result.get("history_used")
    if isinstance(history_used, list):
        history_text = "；".join(str(item) for item in history_used[:6] if str(item).strip())
    else:
        history_text = _clip_text(history_used, 360)
    return {
        "next_stage": str(transition.get("next_stage") or default_next_stage),
        "next_action": str(result.get("next_action") or transition.get("next_action") or default_next_action),
        "judgment": _clip_text(result.get("judgment"), 700),
        "why": _clip_text(result.get("why"), 900),
        "history_used": history_text,
        "facts": _clip_text(result.get("facts"), 1800),
        "reason": _clip_text(transition.get("reason"), 700),
        "confidence": result.get("confidence"),
        "mode": "orchestrator",
        "llm_model": str(llm_model or result.get("_orchestrator_llm_model") or LLM_MODEL or ""),
    }


def _normalize_llm_stage_result(
    stage: str,
    result: dict,
    *,
    default_next_stage: str,
    default_next_action: str,
    stop_reason: str | None = None,
) -> dict:
    clean = dict(result or {})
    if stage != "round_synthesis":
        return clean
    transition = clean.get("stage_transition") if isinstance(clean.get("stage_transition"), dict) else {}
    transition = dict(transition)
    if str(default_next_action or "").startswith("start_next_round"):
        llm_next_stage = str(transition.get("next_stage") or "").strip()
        if llm_next_stage not in _ORCHESTRATOR_RESUME_STAGES:
            llm_next_stage = default_next_stage
        llm_next_action = str(clean.get("next_action") or transition.get("next_action") or default_next_action)
        if not llm_next_action.startswith("start_next_round"):
            llm_next_action = _next_action_for_resume_stage(llm_next_stage)
        clean["decision"] = "continue_next_round"
        clean["next_action"] = llm_next_action
        transition["next_stage"] = llm_next_stage
        transition["next_action"] = llm_next_action
    elif default_next_action == "stop_run":
        clean["decision"] = "stop_target_reached" if stop_reason == "target_reached" else "round_budget_reached"
        clean["next_action"] = "stop_run"
        transition["next_stage"] = "checkpoint_stop"
        transition["next_action"] = "stop_run"
    clean["stage_transition"] = transition
    return clean


def _record_llm_stage_event(
    *,
    run_id: str,
    round_id: str,
    stage_seq: int,
    stage: str,
    previous_stage: str,
    previous_stage_id: str,
    result: dict,
    summary: str,
    default_next_stage: str,
    default_next_action: str,
    event_type: str = "llm_result",
    evidence_refs: list[dict] | None = None,
    candidate_lanes: Any = None,
    advice: dict | None = None,
    allowed_actions: list[str] | None = None,
    blocked_actions: list[str] | None = None,
    extra_tags: list[str] | None = None,
    stop_reason: str | None = None,
    **extra: Any,
) -> dict:
    result = _normalize_llm_stage_result(
        stage,
        result,
        default_next_stage=default_next_stage,
        default_next_action=default_next_action,
        stop_reason=stop_reason,
    )
    llm_trace_id = result.pop("_orchestrator_llm_trace_id", None)
    llm_runtime_model = str(result.pop("_orchestrator_llm_model", "") or "")
    llm_provider_model = str(result.pop("_orchestrator_llm_provider_model", "") or "")
    llm_model_order = result.pop("_orchestrator_llm_model_order", None)
    transition = _llm_transition(
        result,
        default_next_stage=default_next_stage,
        default_next_action=default_next_action,
        llm_model=llm_runtime_model,
    )
    if llm_trace_id:
        transition["llm_trace_id"] = llm_trace_id
    stage_result = _sanitize_llm_payload(result)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "round_id": round_id,
        "stage_seq": stage_seq,
        "stage_id": f"{round_id}:s{stage_seq:02d}_{stage}",
        "previous_stage": previous_stage,
        "previous_stage_id": previous_stage_id,
        "stage": stage,
        "summary": _clip_text(result.get("summary"), 900) or summary,
        "system_summary": summary,
        "decision": _clip_text(result.get("decision"), 180),
        "stage_transition": transition,
        "event_type": event_type,
        "checkpoint": stage,
        "evidence_refs": evidence_refs or [],
        "tags": ["orchestrator", "deepseek_v4", *(_ORCHESTRATOR_STAGE_TAGS.get(stage, [stage])), *(extra_tags or [])],
        "llm_trace_id": llm_trace_id,
        "llm_model": llm_runtime_model or LLM_MODEL,
        "llm_result": stage_result,
    }
    if llm_provider_model:
        payload["llm_provider_model"] = llm_provider_model
    if llm_model_order:
        payload["llm_model_order"] = list(llm_model_order)
    if candidate_lanes is not None:
        payload["candidate_lanes"] = candidate_lanes
    if advice:
        payload["advice"] = advice
    if allowed_actions is not None:
        payload["allowed_actions"] = allowed_actions
    if blocked_actions is not None:
        payload["blocked_actions"] = blocked_actions
    payload.update(extra)
    return _write_orchestrator_event(payload, sync_research_step=True)


def _orchestrator_candidate_limit(n_candidates: int | None) -> int:
    return max(1, min(10, int(n_candidates or 3)))


def _candidate_context_field_context(field_context: dict) -> dict:
    if not isinstance(field_context, dict):
        return {}
    return {
        "supported_fields": (field_context.get("supported_fields") or [])[:32],
        "blocked_fields": _prompt_list_from_list_or_dict(field_context.get("blocked_fields"), limit=8),
        "aliases": {
            key: value
            for key, value in (field_context.get("aliases") or {}).items()
            if key in {"market_cap", "cap", "ps", "pb_ratio", "pe_ratio", "float_share"}
        },
    }


def _prompt_list_from_list_or_dict(value: Any, *, limit: int = 16) -> list[Any]:
    if isinstance(value, dict):
        return list(value.keys())[:limit]
    if isinstance(value, list):
        return value[:limit]
    if isinstance(value, tuple):
        return list(value[:limit])
    return []


def _candidate_context_research_space(field_context: dict) -> dict:
    return _candidate_context_research_space_for_stage(field_context, stage="full")


def _candidate_context_research_space_for_stage(field_context: dict, *, stage: str | None = None) -> dict:
    if not isinstance(field_context, dict):
        field_context = {}
    aliases = field_context.get("aliases") or field_context.get("field_aliases") or {}
    if isinstance(aliases, dict):
        aliases = {
            key: value
            for key, value in aliases.items()
            if key in {"market_cap", "cap", "ps", "pb_ratio", "pe_ratio", "float_share", "dividend_yield"}
        }
    else:
        aliases = {}
    descriptions = field_context.get("field_descriptions") if isinstance(field_context.get("field_descriptions"), dict) else {}
    supported_fields = _prompt_list_from_list_or_dict(field_context.get("supported_fields"), limit=56)
    field_descriptions = {
        field: _clip_text(descriptions.get(field), 120)
        for field in supported_fields
        if descriptions.get(field)
    }
    field_constraints = {
        "aliases": aliases,
        "unit_guidance": field_context.get("unit_guidance") or {},
        "missing_value_semantics": field_context.get("missing_value_semantics") or {},
        "neutralization_status": field_context.get("neutralization_status") or {},
        "field_descriptions": field_descriptions,
    }
    blocked_fields = _prompt_list_from_list_or_dict(field_context.get("blocked_fields"), limit=16)
    compact_constraints = {
        key: value
        for key, value in field_constraints.items()
        if value not in (None, "", [], {})
    }
    return _jsonable(
        {
            "supported_fields": supported_fields,
            "supported_operators": _orchestrator_supported_operator_palette(),
            "blocked_fields": blocked_fields,
            "field_constraints": compact_constraints,
        }
    )


def _candidate_context_field_requirements(field_context: dict) -> dict:
    if not isinstance(field_context, dict):
        return {}
    return _jsonable(
        {
            "blocked_fields": _prompt_list_from_list_or_dict(field_context.get("blocked_fields"), limit=12),
            "aliases": _candidate_context_research_space(field_context).get("field_constraints", {}).get("aliases") or {},
            "unit_guidance": field_context.get("unit_guidance") or {},
            "missing_value_semantics": field_context.get("missing_value_semantics") or {},
        }
    )


def _candidate_context_failure_feedback(feedback: dict) -> dict:
    if not isinstance(feedback, dict):
        return {}
    weak_candidates = []
    for item in (feedback.get("weak_candidates") or [])[:2]:
        if not isinstance(item, dict):
            continue
        weak_candidates.append(
            {
                "candidate_id": item.get("candidate_id"),
                "score": item.get("score"),
                "grade": item.get("grade"),
                "fields": (item.get("fields") or [])[:6],
                "mutation_action": item.get("mutation_action"),
                "reason": _clip_text(item.get("reason"), 80),
                "expression": _clip_text(item.get("expression"), 72),
            }
        )
    near_misses = []
    for item in (feedback.get("deep_near_misses") or [])[:2]:
        if not isinstance(item, dict):
            continue
        near_misses.append(
            {
                "candidate_id": item.get("candidate_id"),
                "quick_score": item.get("quick_score"),
                "deep_score": item.get("deep_score"),
                "deep_reason": item.get("deep_reason"),
                "ic": item.get("ic"),
                "icir": item.get("icir"),
                "fields": (item.get("fields") or [])[:6],
                "expression": _clip_text(item.get("expression"), 76),
            }
        )
    return {
        "weak_fields": _list_prefix(feedback.get("weak_fields"), 5),
        "weak_candidates": weak_candidates,
        "deep_near_misses": near_misses,
        "novelty_vetoes": _list_prefix(feedback.get("novelty_vetoes"), 2),
        "policy": _list_prefix(feedback.get("policy"), 2),
    }


def _candidate_context_anchors(active_context: dict) -> list[dict]:
    anchors = []
    if not isinstance(active_context, dict):
        return anchors
    for item in (active_context.get("recent_orchestrator_anchors") or [])[:3]:
        if not isinstance(item, dict):
            continue
        anchors.append(
            {
                "stage": item.get("stage"),
                "candidate_id": item.get("candidate_id"),
                "score": item.get("score"),
                "grade": item.get("grade"),
                "deep_score": item.get("deep_score"),
                "deep_action": item.get("deep_action"),
                "economic_thesis": _clip_text(item.get("economic_thesis"), 70),
                "hypothesis": _clip_text(item.get("hypothesis"), 80),
                "expression": _clip_text(item.get("expression"), 110),
            }
        )
    return anchors


def _score_candidate_with_mcp(
    candidate: dict,
    *,
    contract: dict,
) -> dict:
    mcp_server = _orchestrator_mcp_server()
    tool_intent = _orchestrator_tool_intent(tool="score_factor", candidate=candidate, contract=contract)
    expression = str(candidate.get("expression") or "")
    validation = mcp_server.validate_expression(expression, mode="local")
    if not str(validation).startswith("OK"):
        return {
            **candidate,
            "status": "invalid_expression",
            "validation_error": str(validation),
            # Validation failed before score_factor ran.  A synthetic zero/D
            # corrupts score distributions and turns a construction error
            # into fake observed factor performance.
            "score": None,
            "quick_score": None,
            "grade": None,
            "reject_reasons": ["validation_error"],
        }
    raw = _run_async_tool(
        mcp_server.score_factor(
            expression=expression,
            universe=contract.get("universe", FACTOR_DEFAULT_UNIVERSE),
            start_date=contract.get("selection_start_date"),
            end_date=contract.get("selection_end_date"),
            n_groups=int(contract.get("n_groups") or 5),
            holding_period=int(contract.get("holding_period") or FACTOR_DEFAULT_HOLDING_PERIOD),
            top_frac=float(contract.get("top_frac") or FACTOR_DEFAULT_TOP_FRAC),
            cost_rate=float(contract.get("cost_rate") or FACTOR_DEFAULT_COST_RATE),
            rebalance_anchor=contract.get("rebalance_anchor"),
            benchmark=contract.get("benchmark", FACTOR_DEFAULT_BENCHMARK),
            neutralize_cap=bool(contract.get("neutralize_cap")),
            neutralize_industry=bool(contract.get("neutralize_industry")),
            idempotency_key=tool_intent["idempotency_key"],
            run_id=str(contract.get("run_id") or ""),
            round_id=str(contract.get("round_id") or ""),
            stage_id=f"{contract.get('round_id')}:s05_score_review",
            candidate_id=str(candidate.get("candidate_id") or ""),
        )
    )
    payload = _orchestrator_tool_result_payload(raw)
    return {
        **candidate,
        **payload,
        "validation": validation,
        "source_tool": "score_factor",
        "orchestrator_tool_intent": tool_intent,
    }


def _score_candidate_with_mcp_isolated(candidate: dict, *, contract: dict) -> dict:
    if os.environ.get("FXALPHA_ORCH_DISABLE_TOOL_ISOLATION", "").lower() not in {"1", "true", "yes"}:
        worker = _run_orchestrator_candidate_worker(tool="score_factor", candidate=candidate, contract=contract)
        if worker.get("ok") is True and isinstance(worker.get("result"), dict):
            result = dict(worker["result"])
            result.setdefault("execution", worker.get("execution"))
            return result
        return {
            **candidate,
            "status": "score_error",
            "validation": "score_factor_runtime_error",
            "score": 0,
            "grade": "D",
            "reject_reasons": ["score_runtime_error"],
            "error": str(worker.get("error") or "score_factor_worker_failed")[:500],
            "error_type": worker.get("error_type"),
            "traceback": worker.get("traceback"),
            "execution": worker.get("execution", "unknown"),
            "previous_execution": worker.get("previous_execution"),
            "previous_error": worker.get("previous_error"),
            "returncode": worker.get("returncode"),
            "stdout_tail": worker.get("stdout_tail"),
            "stderr_tail": worker.get("stderr_tail"),
            "source_tool": "score_factor",
            "screening_stage": "quick_score_failed",
        }
    try:
        return _score_candidate_with_mcp(candidate, contract=contract)
    except Exception as exc:
        return {
            **candidate,
            "status": "score_error",
            "validation": "score_factor_runtime_error",
            "score": 0,
            "grade": "D",
            "reject_reasons": ["score_runtime_error"],
            "error": str(exc)[:500],
            "source_tool": "score_factor",
            "screening_stage": "quick_score_failed",
        }


def _is_orchestrator_tool_infrastructure_error(candidate: dict) -> bool:
    if not isinstance(candidate, dict):
        return False
    error_type = str(candidate.get("error_type") or "")
    error = str(candidate.get("error") or "")
    execution = str(candidate.get("execution") or "")
    if error_type in {"SystemdWorkerExit", "TimeoutExpired"}:
        return True
    if "timeout_after_" in error or "orchestrator_tool_timeout_after_" in error:
        return True
    if re.search(r"(score_factor|deep_validation)_exit_\d+", error):
        return True
    return bool(
        execution == "systemd_transient_unit"
        and candidate.get("status") in {"score_error", "deep_validation_error"}
        and candidate.get("returncode") not in {None, 0}
    )


def _deep_validate_candidate_with_mcp(candidate: dict, *, contract: dict) -> dict:
    mcp_server = _orchestrator_mcp_server()
    tool_intent = _orchestrator_tool_intent(tool="deep_validation", candidate=candidate, contract=contract)
    expression = str(candidate.get("expression") or "")
    tool_timeout_s = max(
        1,
        min(
            FACTOR_ORCHESTRATOR_TOOL_TIMEOUT_MAX,
            int(contract.get("tool_timeout_s") or FACTOR_ORCHESTRATOR_TOOL_TIMEOUT_DEFAULT),
        ),
    )
    common = {
        "expression": expression,
        "universe": contract.get("universe", FACTOR_DEFAULT_UNIVERSE),
        "start_date": contract.get("selection_start_date"),
        "end_date": contract.get("selection_end_date"),
        "holding_period": int(contract.get("holding_period") or FACTOR_DEFAULT_HOLDING_PERIOD),
        "neutralize_cap": bool(contract.get("neutralize_cap")),
        "neutralize_industry": bool(contract.get("neutralize_industry")),
        "run_id": str(contract.get("run_id") or ""),
        "round_id": str(contract.get("round_id") or ""),
        "stage_id": f"{contract.get('round_id')}:s08_deep_validation_review",
        "candidate_id": str(candidate.get("candidate_id") or ""),
    }
    bundle_runner = getattr(mcp_server, "fxalpha_run_deep_validation_bundle", None)
    if callable(bundle_runner):
        bundle_raw = _run_async_tool(
            bundle_runner(
                **common,
                n_groups=int(contract.get("n_groups") or 5),
                top_frac=float(contract.get("top_frac") or FACTOR_DEFAULT_TOP_FRAC),
                cost_rate=float(contract.get("cost_rate") or FACTOR_DEFAULT_COST_RATE),
                rebalance_anchor=contract.get("rebalance_anchor"),
                benchmark=contract.get("benchmark", FACTOR_DEFAULT_BENCHMARK),
                idempotency_key=tool_intent["idempotency_key"],
            ),
            timeout_s=tool_timeout_s,
        )
        bundle = _orchestrator_tool_result_payload(bundle_raw)
        backtest = {
            "backtest_summary": bundle.get("backtest_summary") or {},
            "metrics": bundle.get("metrics") or {},
            "report_path": bundle.get("report_path"),
        }
        anti = bundle.get("anti_overfit") if isinstance(bundle.get("anti_overfit"), dict) else {}
        rolling = bundle.get("rolling_validation") if isinstance(bundle.get("rolling_validation"), dict) else {}
        adversarial = bundle.get("adversarial_validation") if isinstance(bundle.get("adversarial_validation"), dict) else {}
        component_sequence = bundle.get("component_sequence") or [
            "run_backtest",
            "run_anti_overfit",
            "run_rolling_validation",
            "run_adversarial_validation",
        ]
    else:
        backtest_raw = _run_async_tool(
            mcp_server.run_backtest(
                **common,
                n_groups=int(contract.get("n_groups") or 5),
                top_frac=float(contract.get("top_frac") or FACTOR_DEFAULT_TOP_FRAC),
                cost_rate=float(contract.get("cost_rate") or FACTOR_DEFAULT_COST_RATE),
                rebalance_anchor=contract.get("rebalance_anchor"),
                benchmark=contract.get("benchmark", FACTOR_DEFAULT_BENCHMARK),
            ),
            timeout_s=tool_timeout_s,
        )
        anti_raw = _run_async_tool(mcp_server.run_anti_overfit(**common), timeout_s=tool_timeout_s)
        rolling_raw = _run_async_tool(mcp_server.run_rolling_validation(**common), timeout_s=tool_timeout_s)
        adversarial_raw = _run_async_tool(mcp_server.run_adversarial_validation(**common), timeout_s=tool_timeout_s)
        backtest = _orchestrator_tool_result_payload(backtest_raw)
        anti = _orchestrator_tool_result_payload(anti_raw)
        rolling = _orchestrator_tool_result_payload(rolling_raw)
        adversarial = _orchestrator_tool_result_payload(adversarial_raw)
        component_sequence = [
            "run_backtest",
            "run_anti_overfit",
            "run_rolling_validation",
            "run_adversarial_validation",
        ]
    backtest_anti = backtest.get("anti_overfit") if isinstance(backtest.get("anti_overfit"), dict) else {}
    enriched = {
        **candidate,
        "source_tool": "deep_validation",
        "deep_validation_bundle": {"component_sequence": component_sequence, "public_gateway": bool(callable(bundle_runner))},
        "backtest_summary": backtest.get("backtest_summary") or candidate.get("backtest_summary") or {},
        "key_metrics": backtest.get("backtest_summary") or candidate.get("key_metrics") or {},
        "report_metrics": backtest.get("metrics") or {},
        "report_path": backtest.get("report_path"),
        "anti_overfit": anti or backtest_anti,
        "rolling_validation": rolling,
        "adversarial_validation": adversarial,
        "holding_period_days": int(contract.get("holding_period") or FACTOR_DEFAULT_HOLDING_PERIOD),
        "orchestrator_tool_intent": tool_intent,
    }
    if "score" in candidate and "quick_score" not in enriched:
        enriched["quick_score"] = candidate.get("score")
    return enriched


def _deep_validate_candidate_with_mcp_isolated(candidate: dict, *, contract: dict) -> dict:
    if os.environ.get("FXALPHA_ORCH_DISABLE_TOOL_ISOLATION", "").lower() not in {"1", "true", "yes"}:
        worker = _run_orchestrator_candidate_worker(tool="deep_validation", candidate=candidate, contract=contract)
        if worker.get("ok") is True and isinstance(worker.get("result"), dict):
            result = dict(worker["result"])
            result.setdefault("execution", worker.get("execution"))
            return result
        return {
            **candidate,
            "status": "deep_validation_error",
            "deep_score": 0,
            "grade": candidate.get("grade") or "D",
            "reject_reasons": ["deep_validation_runtime_error"],
            "error": str(worker.get("error") or "deep_validation_worker_failed")[:500],
            "error_type": worker.get("error_type"),
            "traceback": worker.get("traceback"),
            "execution": worker.get("execution", "unknown"),
            "previous_execution": worker.get("previous_execution"),
            "previous_error": worker.get("previous_error"),
            "returncode": worker.get("returncode"),
            "stdout_tail": worker.get("stdout_tail"),
            "stderr_tail": worker.get("stderr_tail"),
            "source_tool": "deep_validation",
        }
    try:
        return _deep_validate_candidate_with_mcp(candidate, contract=contract)
    except Exception as exc:
        return {
            **candidate,
            "status": "deep_validation_error",
            "deep_score": 0,
            "grade": candidate.get("grade") or "D",
            "reject_reasons": ["deep_validation_runtime_error"],
            "error": str(exc)[:500],
            "source_tool": "deep_validation",
        }


def _deep_validate_candidate_with_evidence_retry(candidate: dict, *, contract: dict) -> dict:
    """Replay one read-only deep bundle when its required evidence is incomplete."""

    result = _deep_validate_candidate_with_mcp_isolated(candidate, contract=contract)
    missing, _ = _deep_evidence_diagnostics([result])
    if missing and not _is_orchestrator_tool_infrastructure_error(result):
        result = _deep_validate_candidate_with_mcp_isolated(candidate, contract=contract)
    return result


def _compact_candidate_plan_candidate_for_prompt(candidate: dict) -> dict:
    if not isinstance(candidate, dict):
        return {}
    compact = {
        "candidate_id": candidate.get("candidate_id"),
        "hypothesis_id": candidate.get("hypothesis_id"),
        "factor_name": _clip_text(candidate.get("factor_name") or candidate.get("factor_name_hint"), 80),
        "expression": _clip_text(candidate.get("expression"), 220),
        "expected_direction": candidate.get("expected_direction"),
        "mechanism_summary": _clip_text(candidate.get("mechanism_summary"), 260),
        "complexity_intent": candidate.get("complexity_intent"),
        "parent_candidate_id": candidate.get("parent_candidate_id"),
        "mutation_summary": _clip_text(candidate.get("mutation_summary"), 180),
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _compact_orchestrator_candidate_for_diagnosis(candidate: dict) -> dict:
    if not isinstance(candidate, dict):
        return {}
    backtest = candidate.get("backtest_summary") or candidate.get("key_metrics") or {}
    novelty = candidate.get("novelty_guard") or {}
    anti = candidate.get("anti_overfit") or {}
    adversarial = candidate.get("adversarial_validation") or {}
    rolling_value = candidate.get("rolling_validation")
    rolling_evidence = (
        rolling_value
        if isinstance(rolling_value, dict) and bool(rolling_value)
        else None
    )
    rolling = rolling_evidence or {}
    trailing = rolling.get("trailing_horizons") or {}
    compact = {
        "candidate_id": candidate.get("candidate_id"),
        "trajectory_id": candidate.get("trajectory_id"),
        "factor_map_id": candidate.get("factor_map_id") or novelty.get("factor_map_id"),
        "factor_map_audit_id": candidate.get("factor_map_audit_id") or novelty.get("factor_map_audit_id"),
        "matched_region_uid": candidate.get("matched_region_uid") or novelty.get("matched_region_uid"),
        "factor_name": _clip_text(candidate.get("factor_name") or candidate.get("factor_name_hint"), 80),
        "expression": _clip_text(candidate.get("expression"), 220),
        "economic_thesis": _clip_text(candidate.get("economic_thesis"), 100),
        "hypothesis": _clip_text(candidate.get("hypothesis"), 140),
        "parent_candidate_id": candidate.get("parent_candidate_id"),
        "mutation_summary": _clip_text(candidate.get("mutation_summary"), 120),
        "status": candidate.get("status"),
        "screening_stage": candidate.get("screening_stage"),
        "screening_hint": candidate.get("screening_hint"),
        "single_factor_decision": candidate.get("single_factor_decision"),
        "quality_decision": candidate.get("quality_decision"),
        "reject_reason": candidate.get("reject_reason"),
        "score": candidate.get("score") if candidate.get("score") is not None else candidate.get("quick_score"),
        "grade": candidate.get("grade"),
        "reject_reasons": (candidate.get("reject_reasons") or [])[:6],
        "ic": candidate.get("ic") if candidate.get("ic") is not None else backtest.get("ic_mean"),
        "icir": candidate.get("icir") if candidate.get("icir") is not None else backtest.get("ic_ir"),
        "rank_ic": candidate.get("rank_ic") if candidate.get("rank_ic") is not None else backtest.get("rank_ic_mean"),
        "rank_icir": candidate.get("rank_icir") if candidate.get("rank_icir") is not None else backtest.get("rank_ic_ir"),
        "annual_return": candidate.get("annual_return") if candidate.get("annual_return") is not None else backtest.get("annual_return"),
        "sharpe": candidate.get("sharpe") if candidate.get("sharpe") is not None else backtest.get("sharpe"),
        "max_drawdown": candidate.get("max_drawdown") if candidate.get("max_drawdown") is not None else backtest.get("max_drawdown"),
        "turnover": candidate.get("turnover") if candidate.get("turnover") is not None else backtest.get("turnover"),
        "backtest_summary": {
            key: backtest.get(key)
            for key in ("ic_mean", "ic_ir", "rank_ic_mean", "rank_ic_ir", "annual_return", "sharpe", "max_drawdown", "turnover")
            if backtest.get(key) is not None
        },
        "deep_score": candidate.get("deep_score"),
        "deep_score_policy_version": ((candidate.get("deep_validation") or {}).get("score_parts") or {}).get("deep_score_policy_version"),
        "deep_action": candidate.get("deep_action"),
        "deep_reason": candidate.get("deep_reason"),
        "flipped": (candidate.get("key_metrics") or {}).get("flipped"),
        "novelty_allowed": candidate.get("novelty_allowed") if candidate.get("novelty_allowed") is not None else novelty.get("allowed"),
        "novelty_score": candidate.get("novelty_score") if candidate.get("novelty_score") is not None else novelty.get("novelty_score"),
        "anti_overfit_score": candidate.get("anti_overfit_score") if candidate.get("anti_overfit_score") is not None else anti.get("score"),
        "anti_overfit_summary": anti.get("summary") if isinstance(anti, dict) else None,
        "rolling_score": candidate.get("rolling_score") if candidate.get("rolling_score") is not None else rolling.get("score"),
        "rolling_evidence_status": (
            "available"
            if rolling_evidence is not None
            else "joined_summary"
            if candidate.get("rolling_score") is not None
            else "not_joined"
        ),
        "rolling_grade": rolling.get("grade") if isinstance(rolling, dict) else None,
        "rolling_policy_version": rolling.get("score_policy_version") if isinstance(rolling, dict) else None,
        "rolling_status": rolling.get("status") if isinstance(rolling, dict) else None,
        "rolling_6m_ic": (trailing.get("6m") or {}).get("rank_ic"),
        "rolling_12m_ic": (trailing.get("12m") or {}).get("rank_ic"),
        "rolling_24m_ic": (trailing.get("24m") or {}).get("rank_ic"),
        "rolling_48m_ic": (trailing.get("48m") or {}).get("rank_ic"),
        "rolling_weighted_ic": rolling.get("weighted_ic") if isinstance(rolling, dict) else None,
        "rolling_weighted_std": rolling.get("weighted_std") if isinstance(rolling, dict) else None,
        "rolling_robust_ic": rolling.get("robust_ic") if isinstance(rolling, dict) else None,
        "rolling_period_count": (
            len(rolling.get("incremental_periods") or [])
            if rolling_evidence is not None
            else None
        ),
        "rolling_summary": rolling.get("summary") if isinstance(rolling, dict) else None,
        "adversarial_score": candidate.get("adversarial_score") if candidate.get("adversarial_score") is not None else adversarial.get("score"),
        "adversarial_summary": adversarial.get("summary") if isinstance(adversarial, dict) else None,
    }
    return _jsonable(_prune_empty_prompt_values(compact))


def _candidate_action_map(result: dict, *, key: str = "candidate_decisions") -> dict[str, dict]:
    decisions = result.get(key) if isinstance(result.get(key), list) else []
    mapped: dict[str, dict] = {}
    for idx, item in enumerate(decisions):
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or item.get("id") or "").strip()
        if not candidate_id and item.get("idx") is not None:
            candidate_id = f"c{int(item.get('idx')) + 1}" if str(item.get("idx")).isdigit() else str(item.get("idx"))
        if not candidate_id:
            candidate_id = f"c{idx + 1}"
        if candidate_id:
            mapped[candidate_id] = item
    return mapped


def _advice_action_map(advice: dict) -> dict[str, dict]:
    mapped: dict[str, dict] = {}
    for item in advice.get("candidate_lane_decisions") or []:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or item.get("id") or "").strip()
        if not candidate_id:
            raw_idx = item.get("idx") if item.get("idx") is not None else ""
            candidate_id = f"c{int(raw_idx) + 1}" if str(raw_idx).isdigit() else str(raw_idx).strip()
        if candidate_id:
            mapped[candidate_id] = item
    return mapped


def _derive_code_advice_alignment(
    stage: str,
    result: dict,
    stage_input: dict | None,
) -> dict:
    """Compare code advice with LLM decisions after the LLM response.

    This is an audit projection only.  The model no longer has to restate the
    code action or judge its own alignment, and this projection never changes a
    candidate decision or stage transition.
    """

    code_advice = (
        stage_input.get("code_advice")
        if isinstance(stage_input, dict) and isinstance(stage_input.get("code_advice"), dict)
        else {}
    )
    code_actions = _advice_action_map(code_advice) if code_advice else {}
    llm_actions = _candidate_action_map(result)
    items: list[dict[str, str]] = []
    statuses: list[str] = []

    equivalent_actions = {
        "mutate_window": {"revise_expression", "targeted_mutation"},
        "mutate_operator": {"revise_expression", "targeted_mutation"},
        "mutate_normalization": {"revise_expression", "targeted_mutation"},
        "mutate_signal": {"revise_expression", "targeted_mutation"},
        "mutate_signal_direction": {"revise_expression", "targeted_mutation"},
        "mutate_nonlinear": {"revise_expression", "targeted_mutation"},
        "mutate_interaction": {"revise_expression", "targeted_mutation"},
        "simplify": {"revise_expression", "simplify_expression"},
        "simplify_expression": {"revise_expression", "simplify_expression"},
        "regenerate_full": {"return_thesis", "explore_new_thesis"},
        "explore_new_thesis": {"return_thesis", "explore_new_thesis"},
        "recombine_from_best": {"return_hypothesis", "recombine_from_best", "revise_expression"},
        "orthogonalize_or_switch_source": {"orthogonalize_expression", "return_hypothesis", "return_thesis"},
        "deep_validate": {"advance_to_novelty", "advance_to_deep_validation"},
        "submit_quality_gate": {"submit_quality_gate"},
        "import": {"import"},
        "reject": {"reject", "quick_reject", "novelty_reject", "gate_reject"},
    }
    hard_advance_actions = {
        "advance_to_novelty",
        "advance_to_deep_validation",
        "submit_quality_gate",
        "import",
    }

    for candidate_id, code_item in code_actions.items():
        code_action = str(code_item.get("action") or code_item.get("recommended_action") or "unspecified").strip()
        llm_action = str((llm_actions.get(candidate_id) or {}).get("action") or "unspecified").strip()
        expected = equivalent_actions.get(code_action, {code_action})
        if llm_action == code_action:
            alignment = "follow"
            reason = "模型动作与代码建议一致。"
        elif llm_action in expected:
            alignment = "refine"
            reason = "模型保留代码建议方向，并细化为当前阶段动作。"
        else:
            alignment = "disagree"
            reason = "模型动作与代码建议不一致；实际执行仍受代码硬规则约束。"
        if code_action in hard_advance_actions and llm_action not in expected:
            alignment = "disagree"
        statuses.append(alignment)
        items.append(
            {
                "candidate_id": candidate_id,
                "code_action": code_action,
                "llm_action": llm_action,
                "alignment": alignment,
                "reason": reason,
            }
        )
    overall = (
        "disagree"
        if "disagree" in statuses
        else "refine"
        if "refine" in statuses
        else "follow"
    )
    return {
        "source": "deterministic_post_llm_audit",
        "stage": stage,
        "overall": overall,
        "items": items,
    }


def _candidate_id(candidate: dict, idx: int | None = None) -> str:
    raw = str(candidate.get("candidate_id") or "").strip()
    if raw:
        return raw
    if idx is not None:
        return f"c{idx + 1}"
    return ""


def _with_candidate_ids(candidates: list[dict]) -> list[dict]:
    enriched = []
    for idx, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        item = dict(candidate)
        item.setdefault("candidate_id", _candidate_id(item, idx))
        enriched.append(item)
    return enriched


def _normalize_symbolic_expression(expression: Any) -> str:
    return re.sub(r"\s+", "", str(expression or "").lower())


def _parameter_agnostic_expression_key(expression: Any) -> str:
    """Return an exact structural key with numeric parameters removed.

    This is intentionally narrower than a semantic-family comparison.  It
    matches expressions whose fields, operators, wrappers and interaction
    structure are identical and whose only difference is a numeric token such
    as a rolling window or scalar.  Digits embedded in field identifiers (for
    example ``cost_85pct``) are preserved.
    """

    normalized = _normalize_symbolic_expression(expression)
    return re.sub(r"(?<![a-z_])[-+]?\d+(?:\.\d+)?(?![a-z_])", "#", normalized)


def _candidate_symbolic_signature(candidate: dict) -> dict:
    expression = str(candidate.get("expression") or "")
    normalized = _normalize_symbolic_expression(expression)
    fields = _expression_field_terms(expression)
    operators = sorted(
        set(
            token
            for token in re.findall(r"\b[a-z_][a-z0-9_]*\b", normalized)
            if token in _FACTOR_EXPRESSION_FUNCTIONS or token.startswith("ts_") or token.startswith("group_")
        )
    )
    windows = sorted({int(match) for match in re.findall(r"\bts_[a-z_]+\([^)]*,(\d+)", normalized)})
    signature_key = "|".join(
        [
            ",".join(fields[:10]) or "no_fields",
            ",".join(operators[:10]) or "no_ops",
            ",".join(str(window) for window in windows[:8]) or "no_windows",
        ]
    )
    family_key = "|".join(
        [
            ",".join(fields[:8]) or "no_fields",
            ",".join(operators[:6]) or "no_ops",
        ]
    )
    return {
        "expression": expression,
        "normalized": normalized,
        "fields": fields,
        "operators": operators,
        "windows": windows,
        "signature_key": signature_key,
        "family_key": family_key,
    }


def _active_symbolic_context(active_factor_summary: dict | None) -> dict:
    summary = active_factor_summary if isinstance(active_factor_summary, dict) else {}
    active_factors = [item for item in (summary.get("active_factors") or []) if isinstance(item, dict)]
    crowding = summary.get("crowding_map") if isinstance(summary.get("crowding_map"), dict) else {}
    active_factors.extend(item for item in (crowding.get("expressions") or []) if isinstance(item, dict))
    representatives = summary.get("family_representatives") or crowding.get("family_representatives") or []
    normalized_expressions: set[str] = set()
    for item in active_factors:
        expression = str(item.get("expression") or "").strip()
        if not expression:
            continue
        sig = _candidate_symbolic_signature({"expression": expression})
        normalized_expressions.add(sig["normalized"])
    return {"normalized_expressions": normalized_expressions}


def _flatten_multiplication_legs(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return [
            *_flatten_multiplication_legs(node.left),
            *_flatten_multiplication_legs(node.right),
        ]
    return [node]


def _is_centered_signed_leg(node: ast.AST) -> bool:
    """Return whether a multiplication leg is explicitly centered around zero.

    Multiplying two such legs is not an AND-style confirmation: both
    ``positive * positive`` and ``negative * negative`` become positive.  The
    check is deliberately narrow and only recognizes explicit z-score roots;
    rank/tanh combinations used by the production library are left alone.
    """

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _is_centered_signed_leg(node.operand)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, (int, float)):
            return _is_centered_signed_leg(node.right)
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, (int, float)):
            return _is_centered_signed_leg(node.left)
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return False
    return str(node.func.id or "").strip().lower() in {"zscore", "ts_zscore"}


def _has_ambiguous_centered_leg_product(expression: str) -> bool:
    try:
        root = ast.parse(str(expression or ""), mode="eval").body
    except (SyntaxError, ValueError):
        return False
    legs = _flatten_multiplication_legs(root)
    return sum(1 for leg in legs if _is_centered_signed_leg(leg)) >= 2


_DIRECTION_MONOTONIC_UNARY_OPERATORS = {
    "rank",
    "zscore",
    "tanh",
    "ts_mean",
    "ts_rank",
    "ts_delta",
    "ts_sum",
}


def _simple_leg_field_sign(node: ast.AST) -> tuple[str, int] | None:
    """Return a provable field/sign pair for a simple monotonic expression leg.

    The check intentionally declines correlation, dispersion, conditionals,
    field-field arithmetic, and other non-monotonic structures.  It is used
    only to catch explicit sign inversions such as ``rank(ts_delta(x, 10))``
    when the hypothesis requires falling ``x``.
    """

    if isinstance(node, ast.Name):
        return str(node.id or "").strip(), 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        result = _simple_leg_field_sign(node.operand)
        if result is None:
            return None
        field, sign = result
        return field, -sign if isinstance(node.op, ast.USub) else sign
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, (int, float)):
            result = _simple_leg_field_sign(node.right)
            if result is None:
                return None
            field, sign = result
            return field, sign * (-1 if float(node.left.value) < 0 else 1)
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, (int, float)):
            result = _simple_leg_field_sign(node.left)
            if result is None:
                return None
            field, sign = result
            return field, sign * (-1 if float(node.right.value) < 0 else 1)
        return None
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or not node.args:
        return None
    operator = str(node.func.id or "").strip().lower()
    if operator not in _DIRECTION_MONOTONIC_UNARY_OPERATORS:
        return None
    return _simple_leg_field_sign(node.args[0])


def _hypothesis_field_directions(hypothesis: dict | None) -> dict[str, int]:
    directions: dict[str, int] = {}
    if not isinstance(hypothesis, dict):
        return directions
    for group in hypothesis.get("candidate_variable_groups") or []:
        if not isinstance(group, dict):
            continue
        raw_direction = str(group.get("direction") or "").strip().lower()
        sign = 1 if raw_direction in {"positive", "up", "high", "higher", "increase"} else -1 if raw_direction in {
            "negative",
            "down",
            "low",
            "lower",
            "decrease",
        } else 0
        if not sign:
            continue
        raw_fields: list[Any] = []
        for key in ("fields", "variables", "primary", "confirming", "condition", "field"):
            value = group.get(key)
            raw_fields.extend(value if isinstance(value, list) else [value])
        for value in raw_fields:
            field = str(value or "").strip()
            if field:
                directions[field] = sign
    return directions


def _definite_hypothesis_direction_warnings(
    candidate: dict,
    hypothesis: dict | None,
) -> list[str]:
    expected = _hypothesis_field_directions(hypothesis)
    if not expected:
        return []
    try:
        root = ast.parse(str(candidate.get("expression") or ""), mode="eval").body
    except (SyntaxError, ValueError):
        return []
    warnings: list[str] = []
    for leg in _flatten_multiplication_legs(root):
        inferred = _simple_leg_field_sign(leg)
        if inferred is None:
            continue
        field, actual_sign = inferred
        expected_sign = expected.get(field)
        if expected_sign and actual_sign != expected_sign:
            warnings.append(
                "definite_hypothesis_direction_mismatch:"
                f"{field}:expected_{'positive' if expected_sign > 0 else 'negative'}:"
                f"leg_{'positive' if actual_sign > 0 else 'negative'}"
            )
    return warnings


def _candidate_plan_code_precheck(
    candidates: list[dict],
    active_factor_summary: dict | None = None,
    prior_round_expression_refs: dict[str, dict] | None = None,
    hypotheses: list[dict] | None = None,
) -> list[dict]:
    try:
        from quantgpt.data_schema import AVAILABLE_FIELDS, BLOCKED_FIELDS, FIELD_ALIASES
    except Exception:
        AVAILABLE_FIELDS, BLOCKED_FIELDS, FIELD_ALIASES = {}, {}, {}
    try:
        from quantgpt.expression_parser import ExpressionParser

        expression_parser = ExpressionParser(mode="local")
    except Exception:
        expression_parser = None
    alpha_blocked_fields = {"up_limit", "down_limit", "backward_factor"}
    checks: list[dict] = []
    active_symbolic = _active_symbolic_context(active_factor_summary)
    active_normalized = active_symbolic.get("normalized_expressions") or set()
    prior_expression_refs = prior_round_expression_refs if isinstance(prior_round_expression_refs, dict) else {}
    hypothesis_by_id = {
        str(item.get("hypothesis_id") or "").strip(): item
        for item in (hypotheses or [])
        if isinstance(item, dict) and str(item.get("hypothesis_id") or "").strip()
    }
    seen_normalized: dict[str, str] = {}
    seen_parameter_structure: dict[str, str] = {}
    for idx, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        candidate_id = _candidate_id(candidate, idx)
        expression = str(candidate.get("expression") or "")
        symbolic = _candidate_symbolic_signature(candidate)
        normalized = symbolic["normalized"]
        parameter_structure = _parameter_agnostic_expression_key(expression)
        warnings: list[str] = []
        fatal = False
        matched_candidate_ids: list[str] = []
        matched_prior_round = prior_expression_refs.get(normalized) if normalized else None
        if not normalized:
            fatal = True
            warnings.append("empty_expression")
        elif expression_parser is not None:
            try:
                expression_parser.parse(expression)
            except Exception as exc:
                fatal = True
                warnings.append(f"expression_parser_error:{_clip_text(exc, 160)}")
        if isinstance(matched_prior_round, dict):
            fatal = True
            prior_round_id = str(matched_prior_round.get("round_id") or "prior_round")
            prior_candidate_id = str(matched_prior_round.get("candidate_id") or "prior_candidate")
            warnings.append(f"exact_prior_round_expression:{prior_round_id}:{prior_candidate_id}")
        if normalized in active_normalized:
            fatal = True
            warnings.append("exact_active_expression")
        if normalized in seen_normalized:
            fatal = True
            warnings.append(f"batch_duplicate_expression:{seen_normalized[normalized]}")
        elif normalized:
            seen_normalized[normalized] = candidate_id
            matched_parameter_candidate = seen_parameter_structure.get(parameter_structure)
            if parameter_structure and matched_parameter_candidate:
                warnings.append(f"batch_parameter_only_variant:{matched_parameter_candidate}")
                matched_candidate_ids.append(matched_parameter_candidate)
            elif parameter_structure:
                seen_parameter_structure[parameter_structure] = candidate_id
        has_cost_high_low = "close>cost_85pct" in normalized and "close<cost_15pct" in normalized
        if "*" in normalized and has_cost_high_low and normalized.count("where(") >= 2:
            fatal = True
            warnings.append("mutually_exclusive_cost_branches_multiplied")
        if "*" in normalized and normalized.count("where(") >= 2 and normalized.count(",0)") >= 2:
            fatal = True
            warnings.append("zero_sparse_conditional_product")
        if _has_ambiguous_centered_leg_product(expression):
            warnings.append(
                "ambiguous_centered_leg_product:"
                "zscore_or_ts_zscore_multiplication_rewards_both_double_high_and_double_low"
            )
        warnings.extend(
            _definite_hypothesis_direction_warnings(
                candidate,
                hypothesis_by_id.get(str(candidate.get("hypothesis_id") or "").strip()),
            )
        )
        non_numeric_meta_fields = [field for field in _expression_field_terms(expression) if field in _ORCHESTRATOR_NON_NUMERIC_META_FIELDS]
        if non_numeric_meta_fields:
            fatal = True
            warnings.append(f"non_numeric_meta_fields:{','.join(non_numeric_meta_fields)}")
        blocked_fields: list[str] = []
        unsupported_fields: list[str] = []
        for field in _expression_field_terms(expression):
            canonical = str(FIELD_ALIASES.get(field, field)) if isinstance(FIELD_ALIASES, dict) else field
            if field in alpha_blocked_fields or canonical in alpha_blocked_fields:
                blocked_fields.append(field)
                continue
            if isinstance(BLOCKED_FIELDS, dict) and (field in BLOCKED_FIELDS or canonical in BLOCKED_FIELDS):
                blocked_fields.append(field)
                continue
            if isinstance(AVAILABLE_FIELDS, dict) and AVAILABLE_FIELDS:
                if canonical not in AVAILABLE_FIELDS and field not in {"returns"}:
                    unsupported_fields.append(field)
        if blocked_fields:
            fatal = True
            warnings.append(f"blocked_alpha_fields:{','.join(sorted(dict.fromkeys(blocked_fields)))}")
        if unsupported_fields:
            fatal = True
            warnings.append(f"unsupported_fields:{','.join(sorted(dict.fromkeys(unsupported_fields)))}")
        if warnings:
            checks.append(
                {
                    "candidate_id": candidate_id,
                    "fatal": fatal,
                    "warnings": warnings,
                    "instruction": (
                        "drop_candidate"
                        if fatal
                        else "skip_batch_duplicate_unless_evidenced_parent_time_scale_experiment"
                        if matched_candidate_ids
                        else "revise_expression_before_score"
                        if any(
                            str(warning).startswith("definite_hypothesis_direction_mismatch:")
                            for warning in warnings
                        )
                        else "annotate_before_score"
                    ),
                    "expression": _clip_text(expression, 220),
                    "matched_candidate_ids": matched_candidate_ids,
                    "matched_prior_round": (
                        {
                            "round_id": matched_prior_round.get("round_id"),
                            "candidate_id": matched_prior_round.get("candidate_id"),
                            "stage_id": matched_prior_round.get("stage_id"),
                            "expression": _clip_text(matched_prior_round.get("expression"), 220),
                        }
                        if isinstance(matched_prior_round, dict)
                        else None
                    ),
                }
            )
    return checks


def _prior_round_expression_refs(run_id: str, current_round_id: str) -> dict[str, dict]:
    """Return normalized expressions generated in earlier rounds of this run.

    The durable orchestrator event journal is used instead of process memory so
    an explicit recovery cannot accidentally rescore an unchanged expression.
    """

    run_text = str(run_id or "").strip()
    current_round_no = _round_no_from_id(current_round_id)
    if not run_text or current_round_no <= 1:
        return {}
    records, _ = _read_recent_journal_records(
        current_file=FACTOR_ORCHESTRATOR_EVENTS_FILE,
        history_dir=FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR,
        run_id=run_text,
        limit=1500,
    )
    refs: dict[str, dict] = {}
    for event in records:
        if str(event.get("stage") or "") != "expression_design":
            continue
        round_id = str(event.get("round_id") or "")
        round_no = _round_no_from_id(round_id)
        if round_no <= 0 or round_no >= current_round_no:
            continue
        for idx, candidate in enumerate(event.get("candidate_lanes") or []):
            if not isinstance(candidate, dict):
                continue
            expression = str(candidate.get("expression") or "").strip()
            normalized = _normalize_symbolic_expression(expression)
            if not normalized:
                continue
            refs.setdefault(
                normalized,
                {
                    "run_id": run_text,
                    "round_id": round_id,
                    "stage_id": event.get("stage_id"),
                    "candidate_id": _candidate_id(candidate, idx),
                    "expression": expression,
                },
            )
    return refs


def _prior_round_expression_history(prior_refs: dict[str, dict] | None, *, limit: int = 36) -> dict:
    """Project exact same-run expression history once, grouped by round.

    This prompt view is proactive duplicate guidance only.  The complete
    normalized map remains in ``prior_refs`` for deterministic code precheck,
    so prompt truncation can never weaken the hard exact-repeat block.
    """

    rows = [item for item in (prior_refs or {}).values() if isinstance(item, dict)]
    rows.sort(
        key=lambda item: (
            -_round_no_from_id(str(item.get("round_id") or "")),
            str(item.get("candidate_id") or ""),
        )
    )
    visible = rows[: max(1, int(limit))]
    rounds: dict[str, list[dict]] = {}
    for item in visible:
        round_id = str(item.get("round_id") or "prior_round")
        rounds.setdefault(round_id, []).append(
            _jsonable(
                _prune_empty_prompt_values(
                    {
                        "candidate_id": item.get("candidate_id"),
                        "expression": _clip_text(item.get("expression"), 420),
                    }
                )
            )
        )
    operator_names = set(_orchestrator_supported_operator_palette())
    field_sets: Counter[str] = Counter()
    operator_sets: Counter[str] = Counter()
    round_ids: set[str] = set()
    for item in rows:
        expression = str(item.get("expression") or "")
        tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expression)
        operators = sorted({token for token in tokens if token in operator_names})
        fields = sorted(
            {
                token
                for token in tokens
                if token not in operator_names
                and token not in {"and", "or", "True", "False", "nan"}
            }
        )
        if operators:
            operator_sets[" + ".join(operators)] += 1
        if fields:
            field_sets[" + ".join(fields[:6])] += 1
        if str(item.get("round_id") or "").strip():
            round_ids.add(str(item.get("round_id")))
    return _jsonable(
        {
            "source": "same_run_expression_design_events",
            "policy": {
                "exact_repeat": "forbidden",
                "window_only_change": "not_a_new_mechanism",
                "promising_parent": "must_materially_change_expression",
                "code_precheck_remains_authoritative": True,
            },
            "exact_do_not_repeat": [
                {"round_id": round_id, "candidates": candidates}
                for round_id, candidates in rounds.items()
            ],
            "visible_expression_count": len(visible),
            "omitted_older_count": max(0, len(rows) - len(visible)),
            "full_history_digest": {
                "expression_count": len(rows),
                "round_count": len(round_ids),
                "most_repeated_field_sets": [
                    {"fields": key, "count": count}
                    for key, count in field_sets.most_common(10)
                ],
                "most_repeated_operator_sets": [
                    {"operators": key, "count": count}
                    for key, count in operator_sets.most_common(8)
                ],
                "instruction": "Avoid these crowded field/operator families unless the economic mechanism materially changes.",
            },
        }
    )


def _candidate_plan_code_precheck_summary(checks: list[dict]) -> dict:
    items = [item for item in (checks or []) if isinstance(item, dict)]
    fatal_items = [item for item in items if item.get("fatal")]
    soft_items = [item for item in items if not item.get("fatal")]
    reason_counts: dict[str, int] = {}
    for item in items:
        for warning in item.get("warnings") or []:
            reason = str(warning or "").split(":", 1)[0]
            if reason:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "tool": "candidate_plan_code_precheck",
        "scope": "pre_score_schema_and_obvious_expression_error_triage",
        "policy": "pure_code_error_guard_candidate_plan_semantic_budget_final_numeric_novelty_required",
        "fatal_count": len(fatal_items),
        "soft_warning_count": len(soft_items),
        "warning_count": len(items),
        "fatal_candidate_ids": [str(item.get("candidate_id") or "") for item in fatal_items if str(item.get("candidate_id") or "").strip()][:12],
        "soft_candidate_ids": [str(item.get("candidate_id") or "") for item in soft_items if str(item.get("candidate_id") or "").strip()][:12],
        "reason_counts": dict(sorted(reason_counts.items())),
        "items": [
            {
                "candidate_id": item.get("candidate_id"),
                "fatal": bool(item.get("fatal")),
                "warnings": (item.get("warnings") or [])[:6],
                "instruction": item.get("instruction"),
                "expression": _clip_text(item.get("expression"), 220),
                "matched_prior_round": item.get("matched_prior_round"),
                "matched_candidate_ids": item.get("matched_candidate_ids") or [],
            }
            for item in items[:12]
        ],
    }


def _candidate_plan_precheck_candidate_lanes(
    candidates: list[dict],
    checks: list[dict],
) -> list[dict]:
    """Expose the fatal-only pre-score policy as candidate process lanes."""

    check_by_id = {
        str(item.get("candidate_id") or "").strip(): item
        for item in (checks or [])
        if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
    }
    lanes: list[dict] = []
    for idx, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        candidate_id = _candidate_id(candidate, idx)
        item = dict(candidate)
        item["candidate_id"] = candidate_id
        check = check_by_id.get(candidate_id)
        if not check:
            item.setdefault("candidate_lane", "planned_for_score")
            item.setdefault("screening_stage", "candidate_plan")
            item.setdefault("status", "planned_for_score")
            item.setdefault("quality_decision", "planned_for_score")
            item.setdefault("status_label", "表达式预检通过")
            lanes.append(item)
            continue
        warnings = [str(warning) for warning in (check.get("warnings") or []) if warning]
        item["candidate_lane"] = "precheck_blocked" if check.get("fatal") else "precheck_warning"
        item["screening_stage"] = item["candidate_lane"]
        item["status"] = "blocked" if check.get("fatal") else "warning"
        item["quality_decision"] = item["status"]
        item["precheck_status"] = item["candidate_lane"]
        item["precheck_instruction"] = check.get("instruction")
        item["precheck_warnings"] = warnings[:8]
        item["reject_reasons"] = warnings[:8] if check.get("fatal") else []
        item["status_label"] = "表达式预检拦截" if check.get("fatal") else "表达式预检提示"
        item["status_reason"] = "; ".join(warnings[:4])
        lanes.append(item)
    return lanes


def _candidate_plan_result_lanes(candidates: list[dict], checks: list[dict], result: dict) -> list[dict]:
    """Project code blocks and conservative LLM skips into the existing candidate board."""
    lanes = _candidate_plan_precheck_candidate_lanes(candidates, checks)
    decisions = _candidate_plan_lane_map(result)
    for item in lanes:
        if item.get("candidate_lane") == "precheck_blocked":
            item["decision_source"] = "code_precheck"
            continue
        candidate_id = str(item.get("candidate_id") or "").strip()
        decision = decisions.get(candidate_id) or {}
        action = _candidate_plan_lane_action(decision)
        if action in {"revise_expression", "skip_batch_duplicate", "skip_library_near_copy"}:
            reason = str(decision.get("reason") or action)
            is_revision = action == "revise_expression"
            item.update({
                "candidate_lane": "semantic_revision" if is_revision else "candidate_plan_dropped",
                "screening_stage": "semantic_revision" if is_revision else "candidate_plan_dropped",
                "status": "blocked",
                "quality_decision": "revise_expression" if is_revision else "candidate_plan_dropped",
                "status_label": "表达式待修正" if is_revision else "表达式预检拦截",
                "status_reason": f"候选规划：{reason}",
                "decision_source": "candidate_plan_llm",
                "candidate_plan_action": action,
                "matched_candidate_ids": decision.get("matched_candidate_ids") or [],
                "matched_cluster_id": decision.get("matched_cluster_id"),
                "matched_factor_ids": decision.get("matched_factor_ids") or [],
                "grade": None,
                "quick_score": None,
            })
        else:
            item.update({
                "candidate_lane": "planned_for_score",
                "screening_stage": "candidate_plan",
                "status": "planned_for_score",
                "quality_decision": "planned_for_score",
                "status_label": "待快筛",
                "status_reason": str(decision.get("reason") or "Candidate Plan 保守放行，等待真实 quick score。"),
                "decision_source": "candidate_plan_llm",
                "candidate_plan_action": "score",
            })
    return lanes


_FACTOR_EXPRESSION_FUNCTIONS = {
    "abs",
    "and",
    "atr",
    "boll_lower",
    "boll_mid",
    "boll_upper",
    "clip",
    "decay_linear",
    "delay",
    "ema",
    "exp",
    "group_rank",
    "group_zscore",
    "indneutralize",
    "log",
    "macd",
    "max",
    "mean",
    "min",
    "obv",
    "or",
    "power",
    "product",
    "rank",
    "rsi",
    "scale",
    "sigmoid",
    "sign",
    "sign_power",
    "sma",
    "sqrt",
    "tanh",
    "trade_when",
    "ts_argmax",
    "ts_argmin",
    "ts_av_diff",
    "ts_corr",
    "ts_cov",
    "ts_delta",
    "ts_max",
    "ts_mean",
    "ts_min",
    "ts_rank",
    "ts_shift",
    "ts_std",
    "ts_sum",
    "ts_zscore",
    "wma",
    "where",
    "zscore",
}


_HANDOFF_PARENT_ACTIONS = {
    "direction_normalization",
    "mutate",
    "orthogonalize_expression",
    "return_hypothesis",
    "return_thesis",
    "revise_expression",
    "targeted_mutation",
    "recombine_from_best",
    "simplify_expression",
}


def _handoff_mutation_parent_candidate_ids(
    result: dict | None,
    evidence_refs: list[dict] | None,
) -> list[str]:
    """Select only candidates explicitly returned for further research."""

    selected: list[str] = []
    seen: set[str] = set()

    def add_from(item: Any) -> None:
        if not isinstance(item, dict):
            return
        action = str(
            item.get("action")
            or item.get("llm_action")
            or item.get("deep_action")
            or item.get("candidate_plan_action")
            or ""
        ).strip()
        if action not in _HANDOFF_PARENT_ACTIONS:
            return
        candidate_id = str(item.get("candidate_id") or "").strip()
        if not candidate_id or candidate_id in seen:
            return
        seen.add(candidate_id)
        selected.append(candidate_id)

    payload = result if isinstance(result, dict) else {}
    for key in ("candidate_decisions", "candidate_lanes"):
        for item in payload.get(key) if isinstance(payload.get(key), list) else []:
            add_from(item)
    for item in evidence_refs or []:
        add_from(item)
    return selected[:6]


def _scope_handoff_candidate_refs(candidate_ids: list[str], *, round_id: str = "") -> list[str]:
    round_scope = str(round_id or "").strip().rsplit(":", 1)[-1]
    if not re.fullmatch(r"r\d{4}", round_scope, flags=re.IGNORECASE):
        round_scope = ""
    scoped: list[str] = []
    seen: set[str] = set()
    for value in candidate_ids:
        candidate_id = str(value or "").strip()
        if not candidate_id:
            continue
        ref = candidate_id
        if round_scope and not re.match(r"^r\d{4}:", candidate_id, flags=re.IGNORECASE):
            ref = f"{round_scope.lower()}:{candidate_id}"
        if ref in seen:
            continue
        seen.add(ref)
        scoped.append(ref)
    return scoped[:6]


def _handoff_with_stage_narrative(handoff: dict, result: dict | None) -> dict:
    payload = result if isinstance(result, dict) else {}
    transition = payload.get("stage_transition") if isinstance(payload.get("stage_transition"), dict) else {}
    summary = _clip_text(payload.get("summary"), 420)
    judgment = _clip_text(payload.get("judgment"), 420)
    why = _clip_text(payload.get("why"), 620)
    reason = _clip_text(transition.get("reason") or why or judgment or summary, 320)
    enriched = dict(handoff)
    if reason:
        enriched["reason"] = reason
    if summary:
        enriched["summary"] = summary
    if judgment:
        enriched["judgment"] = judgment
    if why:
        enriched["why"] = why
    history_used = payload.get("history_used")
    if history_used not in (None, "", [], {}):
        enriched["history_used"] = _compact_tool_evidence_leaf(history_used, limit=4)
    return enriched


def _code_advice_handoff_strategy(code_advice: dict | None) -> dict:
    """Project code evolution advice into the existing handoff contract.

    The next design stage needs the selected strategy and candidate references,
    not another copy of scores or a literal expression recipe.
    """

    advice = _compact_prompt_advice(code_advice)
    if not advice:
        return {}
    evolution = advice.get("evolution_strategy") if isinstance(advice.get("evolution_strategy"), dict) else {}
    strategy = str(evolution.get("strategy") or advice.get("strategy") or "").strip().lower()
    action = str(evolution.get("action") or advice.get("action") or "").strip().lower()
    lane_strategies: list[str] = []
    lane_actions: list[str] = []
    treatments: list[str] = []
    for lane in advice.get("candidate_lane_decisions") or []:
        if not isinstance(lane, dict):
            continue
        lane_evolution = lane.get("evolution_strategy") if isinstance(lane.get("evolution_strategy"), dict) else {}
        lane_strategy = str(lane_evolution.get("strategy") or "").strip().lower()
        if lane_strategy:
            lane_strategies.append(lane_strategy)
        lane_action = str(lane.get("action") or lane_evolution.get("action") or "").strip().lower()
        if lane_action:
            lane_actions.append(lane_action)
        diagnosis = lane.get("mutation_diagnosis") if isinstance(lane.get("mutation_diagnosis"), dict) else {}
        treatment = str(diagnosis.get("strategy") or diagnosis.get("action") or lane_action).strip().lower()
        if treatment and treatment not in treatments:
            treatments.append(treatment)

    # Candidate-level novelty actions are more specific than the historical
    # top-level ``strategy=explore`` label.  A first correlation veto should
    # preserve the Quick A/B parent and perform a targeted orthogonalization;
    # only an explicit full-regeneration action may clear parent evidence.
    orthogonalize_actions = {"orthogonalize_or_switch_source", "orthogonalize_expression"}
    hard_explore_actions = {
        "explore_new_thesis",
        "regenerate_full",
        "return_thesis_design",
    }
    explicit_lane_actions = {item for item in lane_actions if item}
    if action in orthogonalize_actions or explicit_lane_actions.intersection(orthogonalize_actions):
        strategy = "exploit"
    elif action in hard_explore_actions or (
        explicit_lane_actions and explicit_lane_actions.issubset(hard_explore_actions)
    ):
        strategy = "explore"
    elif not strategy and lane_strategies:
        strategy = Counter(lane_strategies).most_common(1)[0][0]
    if not strategy:
        joined = " ".join([action, *treatments])
        if "explore" in joined or "regenerate_full" in joined:
            strategy = "explore"
        elif "recombine" in joined:
            strategy = "recombine"
        elif "simplify" in joined:
            strategy = "simplify"
        elif joined.strip():
            strategy = "exploit"
    if strategy not in {"exploit", "explore", "recombine", "simplify"}:
        return {}

    recombination_refs = [
        str(item.get("candidate_id") or "").strip()
        for item in advice.get("recombination_candidates") or []
        if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
    ][:6]
    treatment = treatments[0] if treatments else action
    instruction = {
        "exploit": "保留已获支持的 thesis/hypothesis，只执行代码诊断指出的一类定向修改。",
        "explore": "放弃当前弱机制族，重新选择 materially different 的经济主线。",
        "recombine": "使用列出的历史高分候选作为证据来源，重组互补信息关系；不得继续微调当前 parent。",
        "simplify": "保留核心经济关系，删除冗余信号腿、嵌套和包装，不增加新机制。",
    }[strategy]
    return {
        "strategy": strategy,
        "treatment": treatment,
        "instruction": instruction,
        "recombination_candidate_refs": recombination_refs,
    }


def _candidate_mutation_constraints(
    result: dict | None,
    parent_candidate_refs: list[str] | None,
) -> dict[str, list[str]]:
    parent_ids = {
        _candidate_ref_id(value)
        for value in (parent_candidate_refs or [])
        if _candidate_ref_id(value)
    }
    constraints = {"must_preserve": [], "must_change": [], "must_avoid": []}
    payload = result if isinstance(result, dict) else {}
    for item in payload.get("candidate_decisions") or []:
        if not isinstance(item, dict):
            continue
        candidate_id = _candidate_ref_id(item.get("candidate_id"))
        if parent_ids and candidate_id not in parent_ids:
            continue
        for source_key, target_key in (
            ("preserve", "must_preserve"),
            ("change", "must_change"),
            ("avoid", "must_avoid"),
        ):
            value = _clip_text(item.get(source_key), 240)
            if value and value not in constraints[target_key]:
                constraints[target_key].append(value)
    return constraints


def _ranked_code_advice_parent_refs(
    code_advice: dict | None,
    *,
    round_id: str,
    limit: int = 2,
) -> list[str]:
    lanes = [
        item
        for item in ((code_advice or {}).get("candidate_lane_decisions") or [])
        if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
    ]

    def _score(item: dict) -> float:
        try:
            return float(item.get("score"))
        except (TypeError, ValueError):
            return float("-inf")

    lanes.sort(key=lambda item: (-_score(item), str(item.get("candidate_id") or "")))
    return _scope_handoff_candidate_refs(
        [str(item.get("candidate_id") or "").strip() for item in lanes[: max(1, int(limit))]],
        round_id=round_id,
    )


def _return_handoff_from_stage(
    stage: str,
    result: dict,
    *,
    evidence_refs: list[dict] | None = None,
    round_id: str = "",
    code_advice: dict | None = None,
) -> dict:
    transition = result.get("stage_transition") if isinstance(result.get("stage_transition"), dict) else {}
    next_stage = str(transition.get("next_stage") or "")
    parent_refs = _scope_handoff_candidate_refs(
        _handoff_mutation_parent_candidate_ids(result, evidence_refs),
        round_id=round_id,
    )
    strategy_context = _code_advice_handoff_strategy(code_advice)
    strategy = str(strategy_context.get("strategy") or "")
    if strategy == "recombine":
        # Current-stage candidates may be safely scoped to the current round,
        # but recombination candidates come from cross-round trajectory
        # analysis.  An unscoped historical id such as ``c4`` must stay
        # unscoped instead of being mislabeled as the current round's c4.
        seen_parent_refs = {str(value).strip().lower() for value in parent_refs}
        for value in strategy_context.get("recombination_candidate_refs") or []:
            ref = str(value or "").strip().lower()
            if not ref or ref in seen_parent_refs:
                continue
            seen_parent_refs.add(ref)
            parent_refs.append(ref)
        parent_refs = parent_refs[:6]
    elif strategy == "explore":
        parent_refs = []
    elif strategy in {"exploit", "simplify"} and next_stage == "expression_design":
        ranked_refs = _ranked_code_advice_parent_refs(
            code_advice,
            round_id=round_id,
            limit=2,
        )
        if ranked_refs:
            ranked_ids = {_candidate_ref_id(value) for value in ranked_refs}
            selected = [
                value
                for value in parent_refs
                if _candidate_ref_id(value) in ranked_ids
            ]
            parent_refs = selected[:2] or ranked_refs[:2]
        else:
            parent_refs = parent_refs[:2]
    handoff = _mechanism_level_handoff(
        from_stage=stage,
        to_stage=next_stage,
        parent_candidate_refs=parent_refs,
        evidence_refs=evidence_refs,
    )
    if strategy:
        treatment = str(strategy_context.get("treatment") or "").strip()
        handoff["recommended_mutation"] = (
            f"{strategy.upper()}:{treatment}" if treatment else strategy.upper()
        )
        handoff["must_change"] = [str(strategy_context.get("instruction") or "")]
    if strategy == "exploit" and next_stage == "expression_design" and parent_refs:
        constraints = _candidate_mutation_constraints(result, parent_refs)
        handoff["binding_policy"] = "targeted_parent_mutation"
        handoff["must_preserve"] = constraints["must_preserve"] or [
            "保留被引用 parent 已获证据支持的主字段、变量角色、信号方向和组合结构。"
        ]
        handoff["must_change"] = constraints["must_change"] or [
            str(strategy_context.get("instruction") or "只执行代码诊断指出的一类定向修改。")
        ]
        handoff["must_avoid"] = constraints["must_avoid"] or [
            "不得更换主信息来源、加入无关 hypothesis，或同时改变多个机制角色。"
        ]
    return _handoff_with_stage_narrative(
        handoff,
        result,
    )


def _handoff_parent_candidate_refs(values: list[Any] | None) -> list[str]:
    """Extract only candidate identities from free-form review text.

    The raw round synthesis remains in the LLM trace and research steps for
    human review.  It is intentionally not copied into a next-round prompt,
    because phrases such as ``preserve: <expression>`` made models reproduce
    old formulas instead of reconsidering the underlying mechanism.
    """

    refs: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        for match in re.findall(
            r"\br\d{4}:c\d+(?:_[a-z0-9]+)*\b|\bc\d+(?:_[a-z0-9]+)*\b",
            str(value or ""),
            flags=re.IGNORECASE,
        ):
            normalized = match.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            refs.append(normalized)
            if len(refs) >= 6:
                return refs
    return refs


def _mechanism_level_handoff(
    *,
    from_stage: str,
    to_stage: str,
    evidence_refs: list[dict] | None = None,
    parent_candidate_refs: list[str] | None = None,
) -> dict:
    """Build the only handoff shape exposed to a later design stage.

    A handoff controls the *return level* (thesis / hypothesis / expression)
    and points at evidence.  It must not prescribe a field, operator, window,
    or literal expression.  Those remain evidence in the trace and are
    reconsidered by the target stage together with current research context.
    """

    target = str(to_stage or "thesis_design")
    intent_by_stage = {
        "thesis_design": "重新判断经济机制和研究主线；不要沿用已失效的信号设定。",
        "hypothesis_design": "保留有证据支持的研究主线，但重新定义可检验的信息关系。",
        "expression_design": "在既定假设下重新选择表达关系与确认结构；不得复制 parent 表达式。",
    }
    intent = intent_by_stage.get(target, "根据已记录证据重新判断下一步研究层级。")
    parents = [str(item) for item in (parent_candidate_refs or []) if str(item).strip()][:6]
    preserve = ["仅保留有证据支持的经济机制；parent 仅是证据索引，不是公式模板。"] if parents else []
    return {
        "from_stage": str(from_stage or ""),
        "to_stage": target,
        "binding_policy": "mechanism_and_evidence_only_not_literal_expression_instruction",
        "reason": intent,
        "must_preserve": preserve,
        "must_change": [intent],
        "must_avoid": ["不得生成本 run 已出现的完全相同表达式；不得只改变窗口。"],
        "recommended_mutation": "target_stage_reassesses_mechanism_from_evidence",
        "parent_candidate_refs": parents,
        "supporting_evidence_refs": evidence_refs or [],
    }


def _return_handoff_from_round_synthesis(
    synthesis: dict,
    event: dict | None = None,
    *,
    evidence_refs: list[dict] | None = None,
    fallback_next_stage: str = "thesis_design",
) -> dict:
    """Turn round_memory into the next round's primary handoff.

    Many failure branches run round_synthesis and immediately continue the outer
    loop. The synthesis memory must become the next round's visible handoff;
    otherwise the next design stages only see a weaker upstream review summary.
    """

    if not isinstance(synthesis, dict):
        synthesis = {}
    event_transition = event.get("stage_transition") if isinstance(event, dict) and isinstance(event.get("stage_transition"), dict) else {}
    result_transition = synthesis.get("stage_transition") if isinstance(synthesis.get("stage_transition"), dict) else {}
    transition = event_transition or result_transition
    round_memory = synthesis.get("round_memory") if isinstance(synthesis.get("round_memory"), dict) else {}
    memory_stage = str(round_memory.get("suggested_start_stage") or "").strip()
    next_stage = memory_stage if memory_stage in _ORCHESTRATOR_RESUME_STAGES else str(
        transition.get("next_stage") or result_transition.get("next_stage") or fallback_next_stage
    )
    parent_refs = _scope_handoff_candidate_refs(
        _handoff_parent_candidate_refs(
            [
                *_list_prefix(round_memory.get("promising_parents"), 6),
                round_memory.get("next_round_handoff"),
            ]
        ),
        round_id=str((event or {}).get("round_id") or ""),
    )
    handoff = _handoff_with_stage_narrative(
        _mechanism_level_handoff(
            from_stage="round_synthesis",
            to_stage=next_stage,
            parent_candidate_refs=parent_refs,
            evidence_refs=evidence_refs
            or (event.get("evidence_refs") if isinstance(event, dict) and isinstance(event.get("evidence_refs"), list) else []),
        ),
        synthesis,
    )
    next_round_handoff = _clip_text(round_memory.get("next_round_handoff"), 620)
    if next_round_handoff:
        # Round Synthesis owns the research judgment for the next round.
        # Keep its existing handoff prose as the primary instruction instead
        # of replacing it with a generic code-generated narrative.
        handoff["reason"] = next_round_handoff
    return handoff


def _adopt_round_synthesis_handoff(
    previous_review_advice: list[dict],
    synthesis: dict,
    event: dict | None = None,
    *,
    evidence_refs: list[dict] | None = None,
    fallback_next_stage: str = "thesis_design",
) -> dict:
    compact_advice = _dedupe_compact_handoffs(previous_review_advice, limit=6)
    pending_direction_handoff = next(
        (
            compact
            for compact in reversed(compact_advice)
            if compact.get("binding_policy") == "direction_normalization_global_sign_flip_only"
        ),
        None,
    )
    handoff = _return_handoff_from_round_synthesis(
        synthesis,
        event,
        evidence_refs=evidence_refs,
        fallback_next_stage=fallback_next_stage,
    )
    if handoff.get("to_stage") in _ORCHESTRATOR_RESUME_STAGES:
        previous_review_advice.append(handoff)
    return pending_direction_handoff or handoff


def _resume_stage_from_handoff(return_handoff: dict | None, *, fallback: str = "thesis_design") -> str:
    if not isinstance(return_handoff, dict):
        return fallback
    target = str(return_handoff.get("to_stage") or "").strip()
    if target in _ORCHESTRATOR_RESUME_STAGES:
        return target
    if target in {"candidate_plan", "score_review", "novelty_review", "deep_validation_review", "import_gate_review", "import_review"}:
        return "expression_design"
    if target in _ORCHESTRATOR_TERMINAL_STAGES:
        return "checkpoint_stop"
    return fallback


def _next_stage_from_return_handoff(
    return_handoff: dict | None,
    *,
    fallback: str = "thesis_design",
    continue_round: bool = True,
) -> str:
    if not continue_round:
        return "checkpoint_stop"
    resume_stage = _resume_stage_from_handoff(return_handoff, fallback=fallback)
    if resume_stage in _ORCHESTRATOR_TERMINAL_STAGES:
        return "checkpoint_stop"
    return resume_stage


def _next_action_for_resume_stage(next_stage: str) -> str:
    stage = str(next_stage or "").strip()
    if stage == "hypothesis_design":
        return "start_next_round_at_hypothesis_design"
    if stage == "expression_design":
        return "start_next_round_at_expression_design"
    if stage in {"checkpoint_stop", "stop"}:
        return "stop_run"
    if stage == "blocker_review":
        return "block_for_human"
    return "start_next_round"


def _round_synthesis_defaults(
    *,
    return_handoff: dict | None,
    round_no: int,
    inputs: dict,
    adopted_total: int,
    fallback: str = "thesis_design",
) -> tuple[str, str]:
    continue_round = _round_should_continue(round_no, inputs, adopted_total)
    next_stage = _next_stage_from_return_handoff(return_handoff, fallback=fallback, continue_round=continue_round)
    return next_stage, _next_action_for_resume_stage(next_stage)


def _authoritative_outcome_from_llm(
    *,
    from_stage: str,
    result: dict,
    fallback_next_stage: str = "thesis_design",
    fallback_next_action: str = "start_next_round",
    reason: str | None = None,
) -> dict:
    transition = result.get("stage_transition") if isinstance(result.get("stage_transition"), dict) else {}
    next_stage = str(transition.get("next_stage") or fallback_next_stage)
    next_action = str(result.get("next_action") or transition.get("next_action") or fallback_next_action)
    return _authoritative_round_outcome(
        from_stage=from_stage,
        decision=str(result.get("decision") or f"return_{next_stage}" or "return_upstream"),
        next_stage=next_stage,
        next_action=next_action,
        reason=str(reason or transition.get("reason") or result.get("why") or ""),
    )


def _candidate_plan_requests_upstream_return(result: dict) -> bool:
    transition = result.get("stage_transition") if isinstance(result.get("stage_transition"), dict) else {}
    next_stage = str(transition.get("next_stage") or "").strip()
    next_action = str(result.get("next_action") or transition.get("next_action") or "").strip()
    if next_stage in {"thesis_design", "hypothesis_design", "expression_design"}:
        return True
    if next_action.startswith("return_"):
        return True
    return False


def _candidate_plan_lane_map(result: dict) -> dict[str, dict]:
    return _candidate_action_map(result, key="candidate_lanes")


def _candidate_plan_lane_action(item: dict | None) -> str:
    item = item if isinstance(item, dict) else {}
    action = str(item.get("action") or item.get("lane") or "").strip()
    aliases = {
        "primary": "score",
        "representative": "score",
        "warning": "score",
        "planned_for_score": "score",
        "semantic_revision": "revise_expression",
        "return_expression_design": "revise_expression",
        "candidate_plan_dropped": "skip_batch_duplicate",
        "dropped": "skip_batch_duplicate",
        "blocked": "precheck_blocked",
    }
    action = aliases.get(action, action)
    if not action and item.get("keep") is True:
        return "score"
    return action


def _candidate_plan_score_candidates(candidates: list[dict], checks: list[dict], result: dict | None = None) -> list[dict]:
    """Apply code-fatal blocks and conservative LLM research-budget choices."""
    fatal_ids = {
        str(item.get("candidate_id") or "").strip()
        for item in (checks or [])
        if isinstance(item, dict) and item.get("fatal")
    }
    definite_direction_revision_ids = {
        str(item.get("candidate_id") or "").strip()
        for item in (checks or [])
        if isinstance(item, dict)
        and any(
            str(warning or "").startswith("definite_hypothesis_direction_mismatch:")
            for warning in (item.get("warnings") or [])
        )
    }
    lanes = _candidate_plan_lane_map(result or {})
    selected: list[dict] = []
    for idx, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        candidate_id = _candidate_id(candidate, idx)
        if candidate_id in fatal_ids or candidate_id in definite_direction_revision_ids:
            continue
        action = _candidate_plan_lane_action(lanes.get(candidate_id))
        # Missing or uncertain decisions fail open to score. Schema validation
        # normally prevents this path, but recovery must never silently drop a
        # usable parent mutation.
        if action not in {"revise_expression", "skip_batch_duplicate", "skip_library_near_copy"}:
            selected.append(candidate)
    return selected


def _protected_parent_mutation_candidate_ids(
    candidates: list[dict],
    *,
    prior_round_expression_refs: dict[str, dict] | None = None,
    allowed_parent_refs: list[str] | None = None,
) -> list[str]:
    """Return traceable, materially changed parent mutations only."""
    protected: list[str] = []
    prior_refs = prior_round_expression_refs if isinstance(prior_round_expression_refs, dict) else {}
    allowed_refs = {
        str(value or "").strip()
        for value in (allowed_parent_refs or [])
        if str(value or "").strip()
    }
    if not allowed_refs:
        return protected

    def _ref_matches(parent_id: str, ref: str) -> bool:
        return parent_id == ref or parent_id.endswith(f":{ref}") or ref.endswith(f":{parent_id}")

    prior_parent_refs: set[str] = set()
    for item in prior_refs.values():
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "").strip()
        round_id = str(item.get("round_id") or "").strip()
        if candidate_id:
            prior_parent_refs.add(candidate_id)
            if round_id:
                prior_parent_refs.add(f"{round_id.rsplit(':', 1)[-1]}:{candidate_id}")
    for idx, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        parent_id = str(candidate.get("parent_candidate_id") or "").strip()
        mutation_summary = str(candidate.get("mutation_summary") or "").strip()
        normalized = _normalize_symbolic_expression(candidate.get("expression"))
        parent_is_allowed = any(_ref_matches(parent_id, ref) for ref in allowed_refs)
        parent_is_prior = any(_ref_matches(parent_id, ref) for ref in prior_parent_refs)
        if (
            parent_id
            and parent_is_allowed
            and parent_is_prior
            and mutation_summary
            and normalized
            and normalized not in prior_refs
        ):
            protected.append(_candidate_id(candidate, idx))
    return protected


def _enforce_candidate_plan_score_transition(result: dict, *, score_candidate_count: int) -> dict:
    """Send valid lanes to Quick without restoring revised or skipped siblings."""

    guarded = dict(result or {})
    if score_candidate_count <= 0:
        return guarded
    # Candidate Plan is a per-candidate router. One bad expression must not
    # hold back valid siblings; revised/skipped lanes have already been removed
    # by _candidate_plan_score_candidates.
    transition = dict(guarded.get("stage_transition") if isinstance(guarded.get("stage_transition"), dict) else {})
    transition.update(
        {
            "next_stage": "score_review",
            "next_action": "validate_and_score_candidates",
            "reason": "candidate plan selected at least one candidate for empirical quick score",
            "selection_policy": "code_fatal_plus_conservative_llm_budget_triage",
        }
    )
    guarded["stage_transition"] = transition
    guarded["next_action"] = "validate_and_score_candidates"
    return guarded


_ORCHESTRATOR_ACTION_ALIASES = {
    "import_adopted_candidate": "import",
}


def _normalize_orchestrator_action(action: Any) -> str:
    normalized = str(action or "").strip()
    return _ORCHESTRATOR_ACTION_ALIASES.get(normalized, normalized)


def _llm_allowed_candidates(
    candidates: list[dict],
    result: dict,
    *,
    allow_actions: set[str],
) -> list[dict]:
    decisions = _candidate_action_map(result)
    if not decisions:
        return []
    selected: list[dict] = []
    for idx, candidate in enumerate(candidates or []):
        cid = _candidate_id(candidate, idx)
        action = _normalize_orchestrator_action((decisions.get(cid) or {}).get("action"))
        if action in allow_actions:
            selected.append(candidate)
    return selected


def _score_candidate_code_keeper(candidate: dict) -> bool:
    if not isinstance(candidate, dict):
        return False
    if candidate.get("status") != "success":
        return False
    if str(candidate.get("screening_stage") or "") != "quick_score":
        return False
    declared_grade = str(candidate.get("grade") or "").upper()
    if declared_grade not in {"A", "B"}:
        return False
    try:
        quick_score = float(candidate.get("quick_score", candidate.get("score")))
    except (TypeError, ValueError):
        return False
    hint = candidate.get("screening_hint") if isinstance(candidate.get("screening_hint"), dict) else {}
    thresholds = hint.get("thresholds") if isinstance(hint.get("thresholds"), dict) else {}
    try:
        quick_score_b = float(thresholds.get("quick_score_b", 70))
    except (TypeError, ValueError):
        quick_score_b = 70.0
    if quick_score < quick_score_b:
        return False
    return (
        hint.get("deep_validation_required") is True
        or str(candidate.get("single_factor_decision") or "") == "deep_validate"
        or str(candidate.get("quality_decision") or "") == "deep_validate"
    )


def _score_review_direction_revision_ids(result: dict) -> set[str]:
    """Return candidates DeepSeek explicitly routes to sign-only revision.

    Direction remains a score-review judgment. This helper only preserves the
    existing ``revise_expression`` route so the generic A/B code fallback does
    not immediately send the candidate to novelty anyway.
    """

    revisions: set[str] = set()
    for candidate_id, decision in _candidate_action_map(result).items():
        if _normalize_orchestrator_action(decision.get("action")) != "revise_expression":
            continue
        mutation = decision.get("mutation_advice") if isinstance(decision.get("mutation_advice"), dict) else {}
        if (
            str(decision.get("failure_class") or "") == "direction_normalization"
            and str(mutation.get("type") or "") == "mutate_signal_direction"
            and str(mutation.get("instruction") or "") == "global_sign_flip_only"
        ):
            revisions.add(str(candidate_id))
    return revisions


def _st_exposure_hard_blocks_candidate(candidate: dict) -> bool:
    guard = candidate.get("st_exposure_guard") if isinstance(candidate, dict) else {}
    if not isinstance(guard, dict) or not guard:
        return False
    mode = str(guard.get("mode") or "hard").lower()
    if mode in {"advisory", "diagnostic", "tag", "tag_only", "label"}:
        return False
    return guard.get("passed") is not True


def _novelty_candidate_code_keeper(candidate: dict) -> bool:
    if not isinstance(candidate, dict):
        return False
    guard = candidate.get("novelty_guard") if isinstance(candidate.get("novelty_guard"), dict) else {}
    if guard.get("allowed") is not True:
        return False
    combined = candidate.get("combined_guard") if isinstance(candidate.get("combined_guard"), dict) else {}
    if combined:
        return combined.get("allowed") is True and combined.get("novelty_allowed") is not False
    return not _st_exposure_hard_blocks_candidate(candidate)


def _code_authoritative_allowed_candidates(
    candidates: list[dict],
    result: dict,
    *,
    allow_actions: set[str],
    code_keeper,
    stage_label: str,
) -> tuple[list[dict], dict]:
    code_ready = [candidate for candidate in (candidates or []) if code_keeper(candidate)]
    llm_selected = _llm_allowed_candidates(code_ready, result, allow_actions=allow_actions)
    selected_by_id: dict[str, dict] = {}
    for idx, candidate in enumerate(llm_selected):
        selected_by_id[_candidate_id(candidate, idx) or f"llm_{idx}"] = candidate
    omitted: list[dict] = []
    rejected: list[str] = []
    decisions = _candidate_action_map(result)
    for idx, candidate in enumerate(code_ready):
        cid = _candidate_id(candidate, idx)
        if cid in selected_by_id:
            continue
        omitted.append(candidate)
        action = _normalize_orchestrator_action((decisions.get(cid) or {}).get("action"))
        if action and action not in allow_actions:
            rejected.append(cid)
        selected_by_id[cid or f"code_{idx}"] = candidate
    selected = list(selected_by_id.values())
    fallback_ids = [_candidate_id(candidate, idx) for idx, candidate in enumerate(omitted)]
    audit = {
        "tool": "code_advice_keeper",
        "stage": stage_label,
        "code_ready_count": len(code_ready),
        "llm_selected_count": len(llm_selected),
        "code_fallback_count": len(omitted),
        "code_fallback_candidate_ids": [cid for cid in fallback_ids if cid][:12],
        "llm_rejected_code_ready_ids": rejected[:12],
        "policy": "code_hard_evidence_authoritative_llm_disagreement_audit_only",
    }
    if omitted:
        audit["warning"] = (
            f"{stage_label}: LLM omitted {len(omitted)} code-ready candidate(s); "
            "advancing them under strict code keeper rules."
        )
    return selected, audit


def _apply_import_gate_factor_names(candidates: list[dict], gate_review: dict) -> list[dict]:
    """Carry LLM-approved mechanism names into the hard import payload."""
    decisions = _candidate_action_map(gate_review)
    enriched: list[dict] = []
    for idx, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        item = dict(candidate)
        cid = _candidate_id(item, idx)
        decision = decisions.get(cid) or {}
        proposed = str(decision.get("factor_name") or "").strip()
        if proposed:
            metadata = dict(item.get("metadata") if isinstance(item.get("metadata"), dict) else {})
            previous = str(item.get("factor_name") or item.get("name") or metadata.get("factor_name") or "").strip()
            item["factor_name"] = proposed
            item["llm_factor_name"] = proposed
            item["factor_name_source"] = "import_gate_review_llm"
            metadata["factor_name"] = proposed
            metadata["llm_factor_name"] = proposed
            metadata["factor_name_source"] = "import_gate_review_llm"
            if previous and previous != proposed:
                metadata["previous_factor_name"] = previous
                item["previous_factor_name"] = previous
            item["metadata"] = metadata
        enriched.append(item)
    return enriched


def _code_authoritative_gate_candidates(candidates: list[dict], result: dict) -> tuple[list[dict], list[dict]]:
    """Return code-ready gate candidates plus the LLM subset for audit.

    Deep validation hard evidence is authoritative for entering the official
    quality gate. LLM review can explain or disagree, but it must not silently
    drop a candidate that code has already marked ready for the official gate.
    """

    code_ready = [candidate for candidate in candidates or [] if isinstance(candidate, dict)]
    llm_selected = _llm_allowed_candidates(code_ready, result, allow_actions={"submit_quality_gate"})
    return code_ready, llm_selected


def _force_code_transition(result: dict, *, next_stage: str, next_action: str, reason: str) -> dict:
    guarded = dict(result or {})
    transition = dict(guarded.get("stage_transition") if isinstance(guarded.get("stage_transition"), dict) else {})
    transition["next_stage"] = next_stage
    transition["next_action"] = next_action
    transition["reason"] = reason
    transition["code_authoritative"] = True
    guarded["stage_transition"] = transition
    guarded["next_action"] = next_action
    return guarded


def _deep_research_review_before_synthesis(result: dict, code_advice: dict | None) -> dict:
    """Keep a legal next-round research entry before forcing synthesis.

    A compliant DeepSeek response chooses thesis/hypothesis/expression itself.
    Code advice is used only as a deterministic fallback when the response
    incorrectly names the immediate pipeline stage (usually round_synthesis)
    instead of the next research entry.
    """

    guarded = dict(result or {})
    transition = dict(guarded.get("stage_transition") if isinstance(guarded.get("stage_transition"), dict) else {})
    requested = str(transition.get("next_stage") or "").strip()
    if requested in _ORCHESTRATOR_RESUME_STAGES:
        return guarded

    advice = code_advice if isinstance(code_advice, dict) else {}
    strategy_tokens: list[str] = []
    top_strategy = advice.get("evolution_strategy")
    if isinstance(top_strategy, dict):
        strategy_tokens.extend(
            str(top_strategy.get(key) or "").lower()
            for key in ("strategy", "action")
        )
    strategy_tokens.extend(
        str(advice.get(key) or "").lower()
        for key in ("action", "strategy")
    )
    for lane in advice.get("candidate_lane_decisions") or []:
        if not isinstance(lane, dict):
            continue
        strategy_tokens.append(str(lane.get("action") or "").lower())
        evolution = lane.get("evolution_strategy") if isinstance(lane.get("evolution_strategy"), dict) else {}
        strategy_tokens.extend(
            str(evolution.get(key) or "").lower()
            for key in ("strategy", "action")
        )
        mutation = lane.get("mutation") if isinstance(lane.get("mutation"), dict) else {}
        strategy_tokens.extend(
            str(mutation.get(key) or "").lower()
            for key in ("strategy", "action")
        )

    joined = " ".join(token for token in strategy_tokens if token)
    if any(token in joined for token in ("explore", "regenerate_full", "explore_new_thesis")):
        resume_stage = "thesis_design"
    elif any(token in joined for token in ("recombine", "recombine_from_best")):
        resume_stage = "hypothesis_design"
    else:
        resume_stage = "expression_design"

    transition["next_stage"] = resume_stage
    transition["next_action"] = _next_action_for_resume_stage(resume_stage)
    transition["reason"] = (
        str(transition.get("reason") or guarded.get("why") or "").strip()
        or f"code advice fallback selected {resume_stage}"
    )
    transition["research_resume_fallback"] = True
    guarded["stage_transition"] = transition
    guarded["next_action"] = transition["next_action"]
    return guarded


def _round_synthesis_resume_transition(
    result: dict,
    *,
    fallback_next_stage: str,
    fallback_next_action: str,
) -> dict:
    """Keep the LLM's research return choice inside the code-owned run budget.

    Ending a weak mechanism is not the same as ending the whole run.  Only the
    code-owned fallback may stop the run (target reached, round budget, or a
    real blocker).  While budget remains, an LLM ``checkpoint_stop`` means
    "abandon this mechanism" and resumes from a fresh thesis.
    """

    guarded = dict(result or {})
    transition = dict(guarded.get("stage_transition") if isinstance(guarded.get("stage_transition"), dict) else {})
    round_memory = guarded.get("round_memory") if isinstance(guarded.get("round_memory"), dict) else {}
    requested_stage = str(transition.get("next_stage") or "").strip()
    memory_stage = str(round_memory.get("suggested_start_stage") or "").strip()
    code_requests_stop = fallback_next_stage in _ORCHESTRATOR_TERMINAL_STAGES
    if code_requests_stop:
        requested_stage = "checkpoint_stop"
    elif memory_stage in _ORCHESTRATOR_RESUME_STAGES:
        requested_stage = memory_stage
    elif requested_stage in _ORCHESTRATOR_TERMINAL_STAGES:
        # The current line may be exhausted, but the production run still has
        # budget.  Clear the local parent and start a genuinely new thesis.
        requested_stage = "thesis_design"
        round_memory = dict(round_memory)
        round_memory["suggested_start_stage"] = requested_stage
        round_memory["promising_parents"] = []
        guarded["round_memory"] = round_memory
        guarded["decision"] = "continue_next_round"
    elif requested_stage not in _ORCHESTRATOR_RESUME_STAGES:
        requested_stage = fallback_next_stage
    if not code_requests_stop and requested_stage not in _ORCHESTRATOR_RESUME_STAGES:
        requested_stage = "thesis_design"
    requested_action = str(guarded.get("next_action") or transition.get("next_action") or "").strip()
    expected_action = _next_action_for_resume_stage(requested_stage)
    if requested_action != expected_action:
        requested_action = expected_action
    transition["next_stage"] = requested_stage
    transition["next_action"] = requested_action
    if code_requests_stop:
        guarded["decision"] = (
            "stop_target_reached"
            if str(guarded.get("decision") or "") == "stop_target_reached"
            else "round_budget_reached"
        )
        transition["resume_policy"] = "code_owned_run_stop"
    else:
        transition["resume_policy"] = "llm_bounded_upstream_return"
    guarded["stage_transition"] = transition
    guarded["next_action"] = requested_action
    return guarded


def _official_gate_import_candidates(candidates: list[dict]) -> list[dict]:
    ready: list[dict] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        gate = candidate.get("gate_result") if isinstance(candidate.get("gate_result"), dict) else {}
        if gate.get("passed") is True:
            ready.append(candidate)
    return ready


def _round_budget_limit(inputs: dict) -> int | None:
    try:
        raw = int(inputs.get("n_rounds") or 0)
    except Exception:
        raw = 0
    if raw <= 0:
        return None
    return max(1, raw)


def _round_budget_reached(round_no: int, inputs: dict) -> bool:
    limit = _round_budget_limit(inputs)
    if limit is None:
        return False
    return round_no >= limit


def _round_should_continue(round_no: int, inputs: dict, adopted_total: int) -> bool:
    if adopted_total >= int(inputs.get("target_adopted") or 1):
        return False
    return not _round_budget_reached(round_no, inputs)


def _round_stop_reason(round_no: int, inputs: dict, adopted_total: int) -> str:
    if adopted_total >= int(inputs.get("target_adopted") or 1):
        return "target_reached"
    if _round_budget_reached(round_no, inputs):
        return "round_budget_reached"
    return "checkpoint_stop"


def _run_orchestrator_job(run_id: str, inputs: dict, contract: dict) -> None:
    """DeepSeek v4-backed Orchestrator runner.

    The earlier implementation kept most review decisions in code.  This
    runner keeps code as the process guard, but asks DeepSeek to make the
    thesis, hypothesis, expression, score, novelty, deep, gate, import, and
    round-synthesis research judgments with a compact multi-round context.
    """

    # Tool idempotency belongs to a run/round/candidate contract.  Keep the
    # run id in the in-memory controller contract so worker subprocesses use
    # exactly the same key as the parent process.
    contract = {**dict(contract or {}), "run_id": run_id}
    # Publish a visible startup checkpoint before the information audit.  That
    # audit can take minutes on a large active library; without this record the
    # GUI keeps projecting the previous completed run and makes a successful
    # Start click look like a no-op.
    _orchestrator_set_job(
        run_id,
        status="running",
        stage="information_audit_refresh",
        event={"event": "information_audit_refresh_started"},
    )
    _orchestrator_stage_event(
        run_id=run_id,
        round_id=f"{run_id}:startup",
        stage_seq=0,
        stage="protocol_load",
        previous_stage="",
        previous_stage_id="",
        summary="新研究已启动，正在刷新并固定本 run 的因子地图上下文。",
        decision="等待因子地图刷新完成后进入 thesis_design。",
        next_stage="protocol_load",
        next_action="refresh_factor_map_context",
        event_type="tool_progress",
        evidence_refs=[{"type": "factor_map_refresh", "status": "running"}],
        tags=["startup_progress", "factor_map_refresh"],
        stage_id_suffix="factor_map_refresh",
    )
    # Information clusters are research context, not a model-feature freeze gate.
    # Refresh them at every fresh ORCH run so a small registry change does not
    # blank the LLM's family context for the whole session.
    if not isinstance(contract.get("factor_map_context"), dict):
        audit_result = factor_library_audit(
            scope="information",
            status_filter="active",
            save_report=True,
            include_feature_sets=True,
        ).to_dict()
        audit_outputs = audit_result.get("outputs") if isinstance(audit_result, dict) else {}
        if audit_result.get("ok") and isinstance(audit_outputs, dict):
            contract["factor_map_context"] = factor_map_context()
        else:
            contract["factor_map_context"] = {
                "available": False,
                "schema_version": "factor_map_v3",
                "reason": "factor_map_refresh_failed",
                "refresh_error": str(audit_result.get("err") or "factor_map_refresh_failed"),
                "regions": [],
                "policy": {
                    "map_context_is_advisory_only": True,
                    "gate_or_score_effect": False,
                },
            }
    round_events: list[dict] = []
    client = _orchestrator_llm_client(inputs)
    adopted_total = 0
    previous_stage = ""
    previous_stage_id = ""
    initial_interrupted_handoff = contract.get("interrupted_handoff") if isinstance(contract.get("interrupted_handoff"), dict) else {}
    return_handoff: dict | None = dict(initial_interrupted_handoff) if initial_interrupted_handoff else None
    previous_review_advice: list[dict] = [dict(initial_interrupted_handoff)] if initial_interrupted_handoff else []
    last_thesis: dict | None = None
    last_hypothesis: dict | None = None
    last_expression_result: dict | None = None
    recovery_checkpoint = (
        dict(initial_interrupted_handoff.get("recovery_checkpoint") or {})
        if isinstance(initial_interrupted_handoff.get("recovery_checkpoint"), dict)
        else {}
    )
    if recovery_checkpoint and (
        not isinstance(recovery_checkpoint.get("candidates"), list)
        or not isinstance(recovery_checkpoint.get("planned_candidates"), list)
        or not isinstance(recovery_checkpoint.get("candidate_plan"), dict)
    ):
        recovery_checkpoint = {}
    if recovery_checkpoint:
        last_thesis = dict(recovery_checkpoint.get("thesis") or {})
        last_hypothesis = dict(recovery_checkpoint.get("hypothesis") or {})
    try:
        if not client.available():
            raise DeepSeekClientError("llm_api_key_missing")
        _orchestrator_set_job(run_id, status="running", stage="protocol_load", event={"event": "orchestrator_started"})
        round_limit = _round_budget_limit(inputs)
        recovered_round_no = _round_no_from_id(recovery_checkpoint.get("round_id")) if recovery_checkpoint else 0
        round_no = max(0, recovered_round_no - 1)
        while round_limit is None or round_no < round_limit:
            _raise_if_orchestrator_stop_requested(run_id)
            # A terminal handoff is authoritative.  Do not create another
            # empty round after Round Synthesis has already stopped the run.
            if (
                return_handoff
                and _resume_stage_from_handoff(return_handoff)
                in _ORCHESTRATOR_TERMINAL_STAGES
            ):
                break
            if _orchestrator_round_event_budget_exceeded(round_events):
                raise DeepSeekClientError(
                    f"orchestrator_event_budget_exceeded:{len(round_events)}>{FACTOR_ORCHESTRATOR_EVENT_BUDGET}"
                )
            round_no += 1
            round_id = f"{run_id}:r{round_no:04d}"
            contract["round_id"] = round_id
            resume_stage = _resume_stage_from_handoff(return_handoff, fallback="thesis_design")
            # A resumed run keeps its original round number (for tool
            # idempotency), so recovery is active on the checkpoint's target
            # round, not merely on a newly-created round 1.
            recovery_active = bool(recovery_checkpoint and round_no == recovered_round_no)
            if (round_no == 1 and not recovery_active) or not return_handoff:
                resume_stage = "thesis_design"
            if resume_stage == "hypothesis_design" and not last_thesis:
                resume_stage = "thesis_design"
            if resume_stage == "expression_design" and (not last_thesis or not last_hypothesis):
                resume_stage = "thesis_design"
            resume_action = _next_action_for_resume_stage(resume_stage)
            stage_seq = 1
            information_context = contract.get("factor_map_context") if isinstance(contract.get("factor_map_context"), dict) else {}
            protocol_refs = [
                {"llm_provider": LLM_PROVIDER, "llm_model": client.preferred_model(), "api_key_present": bool(LLM_API_KEY)},
                {
                    "type": "factor_map_context",
                    "available": bool(information_context.get("available")),
                    "map_id": information_context.get("map_id"),
                    "audit_id": information_context.get("audit_id"),
                    "registry_fingerprint": (information_context.get("audit") or {}).get("registry_fingerprint"),
                    "reason": information_context.get("reason"),
                    "policy": "pinned_at_run_start",
                },
            ]
            if initial_interrupted_handoff and round_no == 1:
                protocol_refs.append(
                    {
                        "type": "interrupted_handoff",
                        "handoff": _compact_return_handoff(initial_interrupted_handoff),
                    }
                )
            event = _orchestrator_stage_event(
                run_id=run_id,
                round_id=round_id,
                stage_seq=stage_seq,
                stage="protocol_load",
                previous_stage=previous_stage,
                previous_stage_id=previous_stage_id,
                summary="Orchestrator 后台模式启动：DeepSeek v4 做研究判断，代码控制工具、gate、import 和状态落盘。",
                decision=f"进入 {resume_stage}；先恢复上下文和历史 handoff，再按目标阶段继续研究。",
                next_stage=resume_stage,
                next_action=resume_action,
                event_type="checkpoint",
                evidence_refs=protocol_refs,
                tags=["protocol_load", "deepseek_v4"],
                research_contract={
                    **{key: value for key, value in contract.items() if key != "factor_map_context"},
                    "factor_map_id": information_context.get("map_id"),
                    "factor_map_audit_id": information_context.get("audit_id"),
                    "factor_map_available": bool(information_context.get("available")),
                },
            )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]

            if resume_stage == "thesis_design":
                context_pack = _build_orchestrator_context_pack(
                    run_id=run_id,
                    round_id=round_id,
                    stage="thesis_design",
                    contract=contract,
                    round_events=round_events,
                )
                lineage = _stage_lineage_context(previous_review_advice=previous_review_advice, return_handoff=return_handoff)
                thesis = _complete_orchestrator_stage_json(
                    client=client,
                    run_id=run_id,
                    round_id=round_id,
                    stage="thesis_design",
                    context_pack=context_pack,
                    stage_input={
                        "blocked_or_failed_reasons": return_handoff,
                        "available_field_families": ((context_pack.get("active_context") or {}).get("field_context") or {}).get("supported_fields", []),
                        "target_constraints": {
                            "need_active_factors": int(inputs.get("target_adopted") or 1),
                            "avoid_temporal_shuffle_hard_gate": True,
                            "gate_ic": "abs(IC)>=0.02",
                            "gate_icir": "abs(ICIR)>=0.3",
                        },
                    },
                    lineage_context=lineage,
                    round_events=round_events,
                    return_handoff=return_handoff,
                    max_tokens=3600,
                )
                last_thesis = dict(thesis)
                event = _record_llm_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=2,
                    stage="thesis_design",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    result=thesis,
                    summary="DeepSeek v4 已基于历史证据和 handoff 提出经济 thesis。",
                    default_next_stage="hypothesis_design",
                    default_next_action="advance_to_hypothesis_design",
                    candidate_lanes=thesis.get("theses") if isinstance(thesis.get("theses"), list) else [],
                )
                round_events.append(event)
                previous_stage, previous_stage_id = event["stage"], event["stage_id"]
                return_handoff = None
                previous_review_advice = []
            else:
                thesis = dict(last_thesis or {})
                event = _orchestrator_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=2,
                    stage="thesis_design",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    summary=f"根据 handoff 复用上一轮 thesis，跳过 thesis_design，直接进入 {resume_stage}。",
                    decision="复用已有经济 thesis；不重新随机换题。",
                    next_stage=resume_stage,
                    next_action=resume_action,
                    event_type="checkpoint",
                    candidate_lanes=thesis.get("theses") if isinstance(thesis.get("theses"), list) else [],
                    tags=["resume", "handoff_resume"],
                    facts=f"resume_stage={resume_stage}",
                    why=_clip_text((return_handoff or {}).get("reason"), 500),
                )
                round_events.append(event)
                previous_stage, previous_stage_id = event["stage"], event["stage_id"]

            if resume_stage in {"thesis_design", "hypothesis_design"}:
                context_pack = _build_orchestrator_context_pack(
                    run_id=run_id,
                    round_id=round_id,
                    stage="hypothesis_design",
                    contract=contract,
                    round_events=round_events,
                )
                lineage = _stage_lineage_context(thesis_result=thesis, previous_review_advice=previous_review_advice, return_handoff=return_handoff)
                try:
                    hypothesis = _complete_orchestrator_stage_json(
                        client=client,
                        run_id=run_id,
                        round_id=round_id,
                        stage="hypothesis_design",
                        context_pack=context_pack,
                        stage_input={
                            "selected_theses": thesis.get("theses") or [],
                            "field_context": _candidate_context_field_context(((context_pack.get("active_context") or {}).get("field_context") or {})),
                            "operator_constraints": "Use FXAlpha/QuantGPT supported operators only; do not change the research contract.",
                        },
                        lineage_context=lineage,
                        round_events=round_events,
                        return_handoff=return_handoff,
                        max_tokens=4200,
                    )
                except DeepSeekClientError as exc:
                    error_text = str(exc)
                    if "hypothesis_design:thesis_semantic_alignment_failed:" not in error_text:
                        raise
                    # The hypothesis is invalid, but the research service is
                    # healthy.  A second repair that still changes the thesis
                    # mechanism must return upstream instead of stopping the
                    # whole production run as an infrastructure blocker.
                    return_handoff = _mechanism_level_handoff(
                        from_stage="hypothesis_design",
                        to_stage="thesis_design",
                    )
                    return_handoff["reason"] = (
                        "候选假设使用的主信息字段与当前thesis不一致；"
                        "放弃本次错误hypothesis，从thesis_design建立字段和经济机制一致的新主线。"
                    )
                    previous_review_advice = [return_handoff]
                    last_thesis = None
                    last_hypothesis = None
                    event = _orchestrator_stage_event(
                        run_id=run_id,
                        round_id=round_id,
                        stage_seq=3,
                        stage="hypothesis_design",
                        previous_stage=previous_stage,
                        previous_stage_id=previous_stage_id,
                        summary="Hypothesis与当前thesis语义不一致，已安全返回thesis_design；研究服务继续运行。",
                        decision="thesis_semantic_alignment_failed_return_thesis",
                        next_stage="thesis_design",
                        next_action="start_next_round",
                        event_type="checkpoint",
                        evidence_refs=[{"type": "llm_contract_recovery", "error": error_text}],
                        tags=["contract_recovery", "thesis_semantic_alignment"],
                    )
                    round_events.append(event)
                    previous_stage, previous_stage_id = event["stage"], event["stage_id"]
                    continue
                last_hypothesis = dict(hypothesis)
                event = _record_llm_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=3,
                    stage="hypothesis_design",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    result=hypothesis,
                    summary="DeepSeek v4 已把经济 thesis 拆成可检验 hypothesis。",
                    default_next_stage="expression_design",
                    default_next_action="advance_to_expression_design",
                    candidate_lanes=hypothesis.get("hypotheses") if isinstance(hypothesis.get("hypotheses"), list) else [],
                )
                round_events.append(event)
                previous_stage, previous_stage_id = event["stage"], event["stage_id"]
                if resume_stage == "hypothesis_design":
                    return_handoff = None
                    previous_review_advice = []
            else:
                hypothesis = dict(last_hypothesis or {})
                event = _orchestrator_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=3,
                    stage="hypothesis_design",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    summary="根据 handoff 复用上一轮 hypothesis，跳过 hypothesis_design，直接进入 expression_design。",
                    decision="复用已有可检验假设；把下游建议交给 expression_design 修改表达式。",
                    next_stage="expression_design",
                    next_action="start_next_round_at_expression_design",
                    event_type="checkpoint",
                    candidate_lanes=hypothesis.get("hypotheses") if isinstance(hypothesis.get("hypotheses"), list) else [],
                    tags=["resume", "handoff_resume"],
                    facts="resume_stage=expression_design",
                    why=_clip_text((return_handoff or {}).get("reason"), 500),
                )
                round_events.append(event)
                previous_stage, previous_stage_id = event["stage"], event["stage_id"]

            context_pack = _build_orchestrator_context_pack(
                run_id=run_id,
                round_id=round_id,
                stage="expression_design",
                contract=contract,
                round_events=round_events,
            )
            lineage = _stage_lineage_context(
                thesis_result=thesis,
                hypothesis_result=hypothesis,
                expression_result=last_expression_result,
                previous_review_advice=previous_review_advice,
                return_handoff=return_handoff,
            )
            prior_round_expression_refs = _prior_round_expression_refs(run_id, round_id)
            compact_return_handoff = _compact_return_handoff(return_handoff)
            targeted_parent_refs = (
                compact_return_handoff.get("parent_candidate_refs") or []
                if compact_return_handoff.get("binding_policy") == "targeted_parent_mutation"
                else []
            )
            if recovery_active:
                candidates = _with_candidate_ids(recovery_checkpoint.get("candidates") or [])
                expression_result = {
                    "decision": "process_recovery_reuse_existing_candidates",
                    "judgment": "基础设施中断后复用已持久化的表达式候选，不重新调用 LLM 设计。",
                    "why": "候选设计已完成；故障恢复只允许重放同一批工具工作。",
                    "candidates": candidates,
                    "stage_transition": {"next_stage": "candidate_plan", "next_action": "replay_existing_candidate_plan"},
                }
            else:
                expression_result = _complete_orchestrator_stage_json(
                    client=client,
                    run_id=run_id,
                    round_id=round_id,
                    stage="expression_design",
                    context_pack=context_pack,
                    stage_input={
                        "hypotheses": hypothesis.get("hypotheses") or [],
                        "operator_list_summary": {
                            "supported_operators": _orchestrator_supported_operator_palette(),
                        },
                        "field_context": ((context_pack.get("active_context") or {}).get("field_context") or {}),
                        "expression_rules": (
                            "Return executable factor expressions only. No fallback expression. "
                            "No unsupported fields. Supported normalization operators include "
                            "rank, zscore, group_rank, group_zscore, scale, and ts_zscore "
                            "(time-series only). Do not invent percentile-rank. If upstream "
                            "asks for a normalization mutation with an unavailable operator, "
                            "choose the closest supported normalization that preserves the "
                            "economic mechanism and state the substitution explicitly; block "
                            "only when the mechanism truly cannot be preserved."
                        ),
                        "complexity_limits": {"max_nested_depth_soft": 8, "avoid_multi_leg_alpha_stack": True},
                        "diversity_budget": {
                            "policy": "independent_research_value_determines_output_count",
                            "same_expression_family_soft_max": 2,
                            "same_parameter_structure_max": 1,
                            "instruction": (
                                "Prefer independent economic mechanisms and confirmation relations before scoring. "
                                "A window, scalar, or normalization-only edit does not justify filling unused budget. "
                                "Return fewer candidates whenever independent research value runs out."
                            ),
                        },
                        "candidate_budget": {
                            "maximum_score_candidates": _orchestrator_candidate_limit(inputs.get("n_candidates")),
                            "minimum_candidates": 1,
                            "must_fill": False,
                            "policy": "maximum_is_compute_budget_not_output_target",
                            "instruction": "Any output count from 1 through maximum_score_candidates is normal; quality determines count.",
                        },
                        "prior_expression_history": _prior_round_expression_history(prior_round_expression_refs),
                        # Private deterministic contract used after the LLM
                        # returns. The referenced parent is promoted into
                        # current_round_context.candidate_drafts separately.
                        "_private_targeted_parent_refs": targeted_parent_refs,
                        # Private deterministic set used after the LLM returns.
                        # _compact_stage_tool_evidence_for_prompt deliberately
                        # excludes underscore-prefixed/private keys.
                        "_private_prior_expression_refs": prior_round_expression_refs,
                    },
                    lineage_context=lineage,
                    round_events=round_events,
                    return_handoff=return_handoff,
                    max_tokens=min(6000, 2200 + 500 * _orchestrator_candidate_limit(inputs.get("n_candidates"))),
                )
                candidates = _with_candidate_ids(expression_result.get("candidates") or [])
                last_expression_result = dict(expression_result)
                if any(
                    _compact_return_handoff(item).get("binding_policy")
                    == "direction_normalization_global_sign_flip_only"
                    for item in previous_review_advice
                ):
                    previous_review_advice = [
                        item
                        for item in previous_review_advice
                        if _compact_return_handoff(item).get("binding_policy")
                        != "direction_normalization_global_sign_flip_only"
                    ]
                    if (
                        _compact_return_handoff(return_handoff).get("binding_policy")
                        == "direction_normalization_global_sign_flip_only"
                    ):
                        return_handoff = None
            if resume_stage == "expression_design":
                return_handoff = None
                previous_review_advice = []
            active_factor_summary = ((context_pack.get("active_context") or {}).get("active_factor_summary") or {})
            candidate_precheck = _candidate_plan_code_precheck(
                candidates,
                active_factor_summary=active_factor_summary,
                prior_round_expression_refs=prior_round_expression_refs,
                hypotheses=(
                    hypothesis.get("hypotheses") or []
                    if isinstance(hypothesis, dict)
                    else []
                ),
            )
            fatal_precheck_ids = {
                str(item.get("candidate_id") or "").strip()
                for item in candidate_precheck
                if item.get("fatal")
            }
            if recovery_active:
                event = _orchestrator_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=4,
                    stage="expression_design",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    summary=f"基础设施恢复：复用 {len(candidates)} 个既有表达式候选，未重新调用 DeepSeek。",
                    decision="process_recovery_reuse_existing_candidates",
                    next_stage="candidate_plan",
                    next_action="replay_existing_candidate_plan",
                    event_type="checkpoint",
                    tags=["process_recovery", "candidate_replay"],
                    candidate_lanes=candidates,
                )
            else:
                event = _record_llm_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=4,
                    stage="expression_design",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    result=expression_result,
                    summary=f"DeepSeek v4 已生成 {len(candidates)} 个表达式候选；无表达式则会阻塞，不做 fallback。",
                    default_next_stage="candidate_plan",
                    default_next_action="review_candidate_batch_before_tools",
                    candidate_lanes=candidates,
                )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]
            if not candidates:
                return_handoff = _return_handoff_from_stage(
                    "expression_design",
                    expression_result,
                    round_id=round_id,
                )
                previous_review_advice.append(return_handoff)
                resume_next_stage, resume_next_action = _round_synthesis_defaults(
                    return_handoff=return_handoff,
                    round_no=round_no,
                    inputs=inputs,
                    adopted_total=adopted_total,
                    fallback="hypothesis_design",
                )
                context_pack = _build_orchestrator_context_pack(
                    run_id=run_id,
                    round_id=round_id,
                    stage="round_synthesis",
                    contract=contract,
                    round_events=round_events,
                )
                lineage = _stage_lineage_context(
                    thesis_result=thesis,
                    hypothesis_result=hypothesis,
                    expression_result={"candidates": candidates},
                    previous_review_advice=previous_review_advice,
                    return_handoff=return_handoff,
                )
                synthesis_input = {
                    "failed_candidates": [],
                    "reason": "expression_design_generated_no_candidates",
                    "authoritative_outcome": _authoritative_outcome_from_llm(
                        from_stage="expression_design",
                        result=expression_result,
                        fallback_next_stage=resume_next_stage,
                        fallback_next_action=resume_next_action,
                        reason="expression_design_generated_no_candidates",
                    ),
                    "llm_decision_chain": round_events,
                }
                synthesis = _complete_orchestrator_stage_json(
                    client=client,
                    run_id=run_id,
                    round_id=round_id,
                    stage="round_synthesis",
                    context_pack=context_pack,
                    stage_input=synthesis_input,
                    lineage_context=lineage,
                    round_events=round_events,
                    return_handoff=return_handoff,
                    max_tokens=4200,
                )
                synthesis = _round_synthesis_resume_transition(
                    synthesis,
                    fallback_next_stage=resume_next_stage,
                    fallback_next_action=resume_next_action,
                )
                event = _record_llm_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=5,
                    stage="round_synthesis",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    result=synthesis,
                    summary="本轮 expression_design 未生成候选，已压缩为下一轮 handoff。",
                    default_next_stage=resume_next_stage,
                    default_next_action=resume_next_action,
                )
                round_events.append(event)
                previous_stage, previous_stage_id = event["stage"], event["stage_id"]
                return_handoff = _adopt_round_synthesis_handoff(
                    previous_review_advice,
                    synthesis,
                    event,
                    fallback_next_stage=resume_next_stage,
                )
                continue

            context_pack = _build_orchestrator_context_pack(
                run_id=run_id,
                round_id=round_id,
                stage="candidate_plan",
                contract=contract,
                round_events=round_events,
            )
            lineage = _stage_lineage_context(
                thesis_result=thesis,
                hypothesis_result=hypothesis,
                expression_result={"candidates": candidates},
                previous_review_advice=previous_review_advice,
                return_handoff=return_handoff,
            )
            if recovery_active and isinstance(recovery_checkpoint.get("candidate_plan"), dict):
                candidate_plan = dict(recovery_checkpoint.get("candidate_plan") or {})
                candidate_precheck = [
                    dict(item)
                    for item in (recovery_checkpoint.get("candidate_precheck") or [])
                    if isinstance(item, dict)
                ]
                fatal_precheck_ids = {
                    str(item.get("candidate_id") or "").strip()
                    for item in candidate_precheck
                    if item.get("fatal")
                }
            else:
                candidate_plan = _complete_orchestrator_stage_json(
                    client=client,
                    run_id=run_id,
                    round_id=round_id,
                    stage="candidate_plan",
                    context_pack=context_pack,
                    stage_input={
                        "candidates": candidates,
                        "code_precheck": candidate_precheck,
                        "protected_parent_mutation_candidate_ids": _protected_parent_mutation_candidate_ids(
                            candidates,
                            prior_round_expression_refs=prior_round_expression_refs,
                            allowed_parent_refs=targeted_parent_refs,
                        ),
                        "factor_map_context": ((context_pack.get("active_context") or {}).get("factor_map_context") or {}),
                        "selection_policy": {
                            "uncertain_defaults_to_score": True,
                            "promising_parent_mutation_defaults_to_score": True,
                            "allowed_actions": [
                                "score",
                                "revise_expression",
                                "skip_batch_duplicate",
                                "skip_library_near_copy",
                            ],
                            "semantic_revision_is_per_candidate": True,
                            "valid_siblings_continue_to_score": True,
                            "library_skip_requires": ["matched_cluster_id", "matched_factor_ids", "reason"],
                            "batch_skip_requires": ["matched_candidate_ids", "reason"],
                        },
                    },
                    lineage_context=lineage,
                    round_events=round_events,
                    return_handoff=return_handoff,
                    max_tokens=4200,
                )
            precheck_summary = _candidate_plan_code_precheck_summary(candidate_precheck)
            candidate_plan = {
                **candidate_plan,
                "code_precheck": candidate_precheck,
                "code_precheck_summary": precheck_summary,
                "code_precheck_policy": {
                    "source": "pure_code_pre_score_guard",
                    "fatal_blocks_score": True,
                    "candidate_plan_semantic_budget_selection": True,
                    "uncertain_or_parent_mutation_defaults_to_score": True,
                    "not_replacement_for": "fxalpha_novelty_check",
                },
            }
            planned_candidates = _candidate_plan_score_candidates(candidates, candidate_precheck, candidate_plan)
            candidate_plan = _enforce_candidate_plan_score_transition(
                candidate_plan,
                score_candidate_count=len(planned_candidates),
            )
            precheck_candidate_lanes = _candidate_plan_result_lanes(candidates, candidate_precheck, candidate_plan)
            precheck_refs = []
            if candidate_precheck:
                precheck_refs.append(
                    {
                        "tool": "candidate_plan_code_precheck",
                        "fatal_count": len(fatal_precheck_ids),
                        "warning_count": len(candidate_precheck),
                        "fatal_candidate_ids": sorted(fatal_precheck_ids)[:8],
                        "reason_counts": precheck_summary.get("reason_counts") or {},
                        "policy": "pure_code_pre_score_guard_final_novelty_still_fxalpha_novelty_check",
                    }
                )
            skipped_plan_ids = [
                str(item.get("candidate_id") or "")
                for item in precheck_candidate_lanes
                if item.get("candidate_lane") == "candidate_plan_dropped"
            ]
            if skipped_plan_ids:
                precheck_refs.append({
                    "tool": "candidate_plan_llm_budget_triage",
                    "skipped_count": len(skipped_plan_ids),
                    "skipped_candidate_ids": skipped_plan_ids[:12],
                    "policy": "evidence_required_uncertain_or_parent_mutation_defaults_to_score",
                })
            plan_transition = candidate_plan.get("stage_transition") if isinstance(candidate_plan.get("stage_transition"), dict) else {}
            plan_next_stage = str(plan_transition.get("next_stage") or "").strip()
            plan_requests_return = _candidate_plan_requests_upstream_return(candidate_plan)
            precheck_note = f"，代码预检丢弃 {len(fatal_precheck_ids)} 个" if fatal_precheck_ids else ""
            if skipped_plan_ids:
                precheck_note += f"，Candidate Plan 有证据地跳过 {len(skipped_plan_ids)} 个重复候选"
            if plan_requests_return and plan_next_stage:
                plan_summary = (
                    f"DeepSeek v4 已完成 candidate_plan 注释：{len(planned_candidates)}/{len(candidates)} 个候选通过代码预检，"
                    f"当前批次无可评分候选并建议回到 {plan_next_stage}{precheck_note}。"
                )
            else:
                plan_summary = (
                    f"DeepSeek v4 已完成 candidate_plan：{len(planned_candidates)}/{len(candidates)} 个候选进入 validate/score"
                    f"{precheck_note}。"
                )
            if recovery_active:
                event = _orchestrator_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=5,
                    stage="candidate_plan",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    summary=f"基础设施恢复：复用既有 candidate_plan，重放 {len(planned_candidates)}/{len(candidates)} 个候选的工具链。",
                    decision="process_recovery_reuse_existing_candidate_plan",
                    next_stage="score_review",
                    next_action="validate_and_score_candidates",
                    event_type="checkpoint",
                    evidence_refs=precheck_refs,
                    tags=["process_recovery", "candidate_plan_replay"],
                    candidate_lanes=precheck_candidate_lanes,
                )
            else:
                event = _record_llm_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=5,
                    stage="candidate_plan",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    result=candidate_plan,
                    summary=plan_summary,
                    default_next_stage="score_review",
                    default_next_action="validate_and_score_candidates",
                    evidence_refs=precheck_refs,
                    candidate_lanes=precheck_candidate_lanes,
                )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]
            # From score_review onward, candidate_plan is part of the current
            # round lineage.  Rebuild the prompt lineage once here so every
            # downstream stage sees the same plan that actually governed the
            # scored batch.
            lineage = _stage_lineage_context(
                thesis_result=thesis,
                hypothesis_result=hypothesis,
                expression_result={"candidates": candidates},
                candidate_plan_result=candidate_plan,
                previous_review_advice=previous_review_advice,
                return_handoff=return_handoff,
            )
            if not planned_candidates:
                return_handoff = _return_handoff_from_stage(
                    "candidate_plan",
                    candidate_plan,
                    evidence_refs=precheck_refs,
                    round_id=round_id,
                )
                previous_review_advice.append(return_handoff)
                resume_next_stage, resume_next_action = _round_synthesis_defaults(
                    return_handoff=return_handoff,
                    round_no=round_no,
                    inputs=inputs,
                    adopted_total=adopted_total,
                    fallback="expression_design",
                )
                synthesis_input = {
                    "failed_candidates": candidates,
                    "reason": "candidate_plan_kept_none",
                    "code_precheck": candidate_precheck,
                    "code_precheck_summary": precheck_summary,
                    "authoritative_outcome": _authoritative_outcome_from_llm(
                        from_stage="candidate_plan",
                        result=candidate_plan,
                        fallback_next_stage=resume_next_stage,
                        fallback_next_action=resume_next_action,
                        reason="candidate_plan_kept_none",
                    ),
                    "llm_decision_chain": round_events,
                }
                synthesis = _complete_orchestrator_stage_json(
                    client=client,
                    run_id=run_id,
                    round_id=round_id,
                    stage="round_synthesis",
                    context_pack=context_pack,
                    stage_input=synthesis_input,
                    lineage_context=lineage,
                    round_events=round_events,
                    return_handoff=return_handoff,
                    max_tokens=4200,
                )
                synthesis = _round_synthesis_resume_transition(
                    synthesis,
                    fallback_next_stage=resume_next_stage,
                    fallback_next_action=resume_next_action,
                )
                event = _record_llm_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=6,
                    stage="round_synthesis",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    result=synthesis,
                    summary="本轮 candidate_plan 未保留候选，已压缩为下一轮 handoff。",
                    default_next_stage=resume_next_stage,
                    default_next_action=resume_next_action,
                )
                round_events.append(event)
                previous_stage, previous_stage_id = event["stage"], event["stage_id"]
                return_handoff = _adopt_round_synthesis_handoff(
                    previous_review_advice,
                    synthesis,
                    event,
                    fallback_next_stage=resume_next_stage,
                )
                continue
            if plan_requests_return:
                return_handoff = _return_handoff_from_stage(
                    "candidate_plan",
                    candidate_plan,
                    evidence_refs=precheck_refs,
                    round_id=round_id,
                )
                previous_review_advice.append(return_handoff)
                resume_next_stage, resume_next_action = _round_synthesis_defaults(
                    return_handoff=return_handoff,
                    round_no=round_no,
                    inputs=inputs,
                    adopted_total=adopted_total,
                    fallback="expression_design",
                )
                synthesis_input = {
                    "failed_candidates": planned_candidates or candidates,
                    "reason": f"candidate_plan_requested_return:{plan_next_stage or 'upstream'}",
                    "code_precheck": candidate_precheck,
                    "code_precheck_summary": precheck_summary,
                    "authoritative_outcome": _authoritative_outcome_from_llm(
                        from_stage="candidate_plan",
                        result=candidate_plan,
                        fallback_next_stage=resume_next_stage,
                        fallback_next_action=resume_next_action,
                        reason=f"candidate_plan_requested_return:{plan_next_stage or 'upstream'}",
                    ),
                    "llm_decision_chain": round_events,
                }
                synthesis = _complete_orchestrator_stage_json(
                    client=client,
                    run_id=run_id,
                    round_id=round_id,
                    stage="round_synthesis",
                    context_pack=context_pack,
                    stage_input=synthesis_input,
                    lineage_context=lineage,
                    round_events=round_events,
                    return_handoff=return_handoff,
                    max_tokens=4200,
                )
                synthesis = _round_synthesis_resume_transition(
                    synthesis,
                    fallback_next_stage=resume_next_stage,
                    fallback_next_action=resume_next_action,
                )
                event = _record_llm_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=6,
                    stage="round_synthesis",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    result=synthesis,
                    summary=f"candidate_plan 请求回退到 {plan_next_stage or 'upstream'}，本轮停止 validate/score 并形成下一轮 handoff。",
                    default_next_stage=resume_next_stage,
                    default_next_action=resume_next_action,
                )
                round_events.append(event)
                previous_stage, previous_stage_id = event["stage"], event["stage_id"]
                return_handoff = _adopt_round_synthesis_handoff(
                    previous_review_advice,
                    synthesis,
                    event,
                    fallback_next_stage=resume_next_stage,
                )
                continue

            recovery_ref = _orchestrator_recovery_checkpoint(
                round_id=round_id,
                stage="score_factor",
                thesis=thesis,
                hypothesis=hypothesis,
                candidates=candidates,
                planned_candidates=planned_candidates,
                candidate_plan=candidate_plan,
                candidate_precheck=candidate_precheck,
            )
            event = _orchestrator_stage_event(
                run_id=run_id,
                round_id=round_id,
                stage_seq=5,
                stage="score_review",
                previous_stage=previous_stage,
                previous_stage_id=previous_stage_id,
                summary=f"开始快筛：准备对 {len(planned_candidates)} 个候选执行 validate_expression 和 score_factor。",
                decision="进入快筛工具阶段，等待 score_factor 返回硬证据。",
                next_stage="score_review",
                next_action="validate_and_score_in_progress",
                event_type="checkpoint",
                evidence_refs=[
                    {
                        "tool": "score_factor",
                        "candidate_count": len(planned_candidates),
                    },
                    recovery_ref,
                ],
                tags=["tool_progress", "score_review_progress"],
                candidate_lanes=planned_candidates,
            )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]

            scored: list[dict] = []
            for idx, candidate in enumerate(planned_candidates, 1):
                _raise_if_orchestrator_stop_requested(run_id)
                progress_ref = {
                    **_orchestrator_tool_intent(tool="score_factor", candidate=candidate, contract=contract),
                    "candidate_index": idx,
                    "candidate_total": len(planned_candidates),
                    "candidate_id": candidate.get("candidate_id"),
                    "candidate_brief": _candidate_progress_brief(candidate),
                }
                progress_event = _orchestrator_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=5,
                    stage="score_review",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    summary=f"快筛进行中：{idx}/{len(planned_candidates)}",
                    decision=f"正在执行 score_factor：{_candidate_progress_brief(candidate) or f'candidate_{idx}'}",
                    next_stage="score_review",
                    next_action="validate_and_score_in_progress",
                    event_type="checkpoint",
                    evidence_refs=[progress_ref],
                    tags=["tool_progress", "score_review_progress", "candidate_progress"],
                    stage_id_suffix=f"candidate_{idx}_{candidate.get('candidate_id') or ''}",
                    candidate_lanes=[candidate],
                )
                round_events.append(progress_event)
                previous_stage, previous_stage_id = progress_event["stage"], progress_event["stage_id"]
                scored.append(_score_candidate_with_mcp_isolated(candidate, contract=contract))
                _raise_if_orchestrator_stop_requested(run_id)
            if _orchestrator_round_event_budget_exceeded(round_events):
                raise DeepSeekClientError(
                    f"orchestrator_event_budget_exceeded:{len(round_events)}>{FACTOR_ORCHESTRATOR_EVENT_BUDGET}"
                )
            score_error_count = sum(
                1
                for item in scored
                if item.get("status") == "score_error" or "score_runtime_error" in (item.get("reject_reasons") or [])
            )
            infrastructure_errors = [item for item in scored if _is_orchestrator_tool_infrastructure_error(item)]
            if infrastructure_errors:
                blocker_refs = [
                    {
                        "candidate_id": item.get("candidate_id"),
                        "tool": item.get("source_tool") or "score_factor",
                        "expression": item.get("expression"),
                        "error": item.get("error"),
                        "error_type": item.get("error_type"),
                        "execution": item.get("execution"),
                        "returncode": item.get("returncode"),
                        "stdout_tail": item.get("stdout_tail"),
                        "stderr_tail": item.get("stderr_tail"),
                    }
                    for item in infrastructure_errors[:6]
                ]
                event = _orchestrator_stage_event(
                    run_id=run_id,
                    round_id=f"{run_id}:blocker",
                    stage_seq=99,
                    stage="blocker",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    summary="score_factor 工具基础设施失败，已停止 Orchestrator；不把本轮候选作为字段或表达式失败学习。",
                    decision=f"tool_infrastructure_blocker：{len(infrastructure_errors)}/{len(scored)} 个候选出现 worker/systemd/timeout 级错误。",
                    next_stage="blocker",
                    next_action="fix_score_factor_worker_before_restart",
                    event_type="blocker",
                    evidence_refs=blocker_refs,
                    tags=["blocker", "tool_infrastructure_blocker", "score_review"],
                    priority="high",
                    candidate_lanes=scored,
                )
                _orchestrator_set_job(
                    run_id,
                    status="failed",
                    stage="blocker",
                    event={
                        "event": "orchestrator_failed",
                        "error": "score_factor_tool_infrastructure_blocker",
                        "stage_id": event.get("stage_id"),
                    },
                )
                return
            quick = quick_advice(
                scored,
                trajectory=_orchestrator_run_candidate_trajectory(
                    run_id=run_id,
                    stage="score_review",
                ),
            )
            context_pack = _build_orchestrator_context_pack(run_id=run_id, round_id=round_id, stage="score_review", contract=contract, round_events=round_events)
            score_review = _complete_orchestrator_stage_json(
                client=client,
                run_id=run_id,
                round_id=round_id,
                stage="score_review",
                context_pack=context_pack,
                stage_input={
                    "candidate_lanes": scored,
                    "validate_results": [{"candidate_id": item.get("candidate_id"), "validation": item.get("validation"), "status": item.get("status")} for item in scored],
                    "score_factor_results": [_compact_orchestrator_candidate_for_diagnosis(item) for item in scored],
                    "trajectory_metrics": quick.get("trajectory_metrics") or {},
                    "code_advice": quick,
                },
                lineage_context=lineage,
                round_events=round_events,
                # A 10-candidate review still needs a complete decision and
                # preserve/change/avoid handoff for every candidate.  Keep
                # enough room for the strict JSON object rather than accepting
                # truncation after all scoring tools have already finished.
                max_tokens=6000,
            )
            code_quick_ok = [candidate for candidate in scored if _score_candidate_code_keeper(candidate)]
            code_quick_ids = {
                _candidate_id(candidate, idx)
                for idx, candidate in enumerate(code_quick_ok)
            }
            direction_revision_ids = _score_review_direction_revision_ids(score_review) & code_quick_ids
            novelty_code_quick_ok = [
                candidate
                for idx, candidate in enumerate(code_quick_ok)
                if _candidate_id(candidate, idx) not in direction_revision_ids
            ]
            keepers, quick_keeper_audit = _code_authoritative_allowed_candidates(
                novelty_code_quick_ok,
                score_review,
                allow_actions={"advance_to_novelty"},
                code_keeper=_score_candidate_code_keeper,
                stage_label="score_review",
            )
            score_review = {
                **score_review,
                "code_keeper_audit": quick_keeper_audit,
                "direction_revision_candidate_ids": sorted(direction_revision_ids),
            }
            score_refs = [
                {
                    "candidate_id": item.get("candidate_id"),
                    "tool": "score_factor",
                    "score": item.get("score"),
                    "grade": item.get("grade"),
                    "task_id": ((item.get("fxalpha_gui_logging") or {}).get("refs") or [None])[0],
                }
                for item in scored
            ]
            score_refs.append(quick_keeper_audit)
            if direction_revision_ids:
                direction_handoff = _mechanism_level_handoff(
                    from_stage="score_review",
                    to_stage="expression_design",
                    parent_candidate_refs=sorted(direction_revision_ids),
                    evidence_refs=score_refs,
                )
                direction_handoff.update(
                    {
                        "binding_policy": "direction_normalization_global_sign_flip_only",
                        "reason": "A/B candidate has negative primary signed RankIC and must return to expression design before novelty.",
                        "must_preserve": ["保留 parent 的字段、算子、窗口和结构。"],
                        "must_change": ["仅给整个 parent 表达式增加一次整体负号。"],
                        "must_avoid": ["不得同时修改字段、算子、窗口或结构；不得重复翻转。"],
                        "recommended_mutation": "global_sign_flip_only",
                    }
                )
                previous_review_advice.append(direction_handoff)
            score_refs.append(
                _orchestrator_recovery_checkpoint(
                    round_id=round_id,
                    stage="score_review",
                    thesis=thesis,
                    hypothesis=hypothesis,
                    candidates=candidates,
                    planned_candidates=planned_candidates,
                    candidate_plan=candidate_plan,
                    candidate_precheck=candidate_precheck,
                    resume_stage="novelty_review" if keepers else "round_synthesis",
                    stage_candidates=keepers or scored,
                    completed_task_refs=[ref for ref in score_refs if isinstance(ref, dict)],
                )
            )
            event = _record_llm_stage_event(
                run_id=run_id,
                round_id=round_id,
                stage_seq=6,
                stage="score_review",
                previous_stage=previous_stage,
                previous_stage_id=previous_stage_id,
                result=score_review,
                summary=(
                    f"Quick score 完成，严格代码 keeper={len(code_quick_ok)}/{len(scored)}，工具错误={score_error_count}，"
                    f"DeepSeek 建议={quick_keeper_audit.get('llm_selected_count', 0)}，代码保底={quick_keeper_audit.get('code_fallback_count', 0)}，"
                    f"实际进入 novelty={len(keepers)}。"
                ),
                default_next_stage="novelty_review" if keepers else "round_synthesis",
                default_next_action="run_novelty_for_code_keeper_candidates" if keepers else "synthesize_score_failures",
                evidence_refs=score_refs,
                candidate_lanes=scored,
                advice=quick,
                allowed_actions=quick.get("allowed_actions", []),
                blocked_actions=quick.get("blocked_actions", []),
            )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]
            if not keepers:
                return_handoff = _return_handoff_from_stage(
                    "score_review",
                    score_review,
                    evidence_refs=score_refs,
                    round_id=round_id,
                    code_advice=quick,
                )
                previous_review_advice.append(return_handoff)
                resume_next_stage, resume_next_action = _round_synthesis_defaults(
                    return_handoff=return_handoff,
                    round_no=round_no,
                    inputs=inputs,
                    adopted_total=adopted_total,
                    fallback="expression_design",
                )
                synthesis = _complete_orchestrator_stage_json(
                    client=client,
                    run_id=run_id,
                    round_id=round_id,
                    stage="round_synthesis",
                    context_pack=context_pack,
                    stage_input={
                        "failed_candidates": scored,
                        "tool_evidence_summary": score_refs,
                        "authoritative_outcome": _authoritative_outcome_from_llm(
                            from_stage="score_review",
                            result=score_review,
                            fallback_next_stage=resume_next_stage,
                            fallback_next_action=resume_next_action,
                        ),
                        "code_advice": quick,
                        "llm_decision_chain": round_events,
                    },
                    lineage_context=lineage,
                    round_events=round_events,
                    return_handoff=return_handoff,
                    max_tokens=4200,
                )
                synthesis = _round_synthesis_resume_transition(
                    synthesis,
                    fallback_next_stage=resume_next_stage,
                    fallback_next_action=resume_next_action,
                )
                event = _record_llm_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=7,
                    stage="round_synthesis",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    result=synthesis,
                    summary="本轮未产生可进 novelty 的候选，已形成下一轮 score handoff。",
                    default_next_stage=resume_next_stage,
                    default_next_action=resume_next_action,
                    stop_reason=_round_stop_reason(round_no, inputs, adopted_total),
                )
                round_events.append(event)
                previous_stage, previous_stage_id = event["stage"], event["stage_id"]
                return_handoff = _adopt_round_synthesis_handoff(
                    previous_review_advice,
                    synthesis,
                    event,
                    fallback_next_stage=resume_next_stage,
                )
                continue

            novelty_result = factor_tool_novelty_check(
                candidates=keepers,
                start_date=contract.get("selection_start_date") or FACTOR_DEFAULT_START_DATE,
                end_date=contract.get("selection_end_date") or FACTOR_DEFAULT_END_DATE,
                run_id=run_id,
                round_id=round_id,
                _library_information_context=contract.get("factor_map_context") or {},
            )
            novelty_payload = _orchestrator_tool_result_payload(novelty_result)
            novelty_payload = _attach_factor_map_region_names(
                novelty_payload,
                factor_map=contract.get("factor_map_context"),
                run_id=run_id,
            )
            novelty_candidates = _with_candidate_ids(novelty_payload.get("keepers") or [])
            novelty_review_candidates = _with_candidate_ids(
                (novelty_payload.get("keepers") or []) + (novelty_payload.get("dropped") or [])
            )
            novelty_adv = novelty_advice(
                novelty_review_candidates or novelty_payload.get("details") or keepers,
                history=_orchestrator_run_novelty_history(run_id=run_id),
            )
            novelty_all_candidates = (novelty_payload.get("keepers") or []) + (novelty_payload.get("dropped") or [])
            st_veto_count = sum(
                1
                for item in novelty_all_candidates
                if str(((item.get("st_exposure_guard") or {}).get("reason") or "")).startswith("st_exposure")
                and str(((item.get("st_exposure_guard") or {}).get("mode") or "hard")).lower() != "advisory"
            )
            st_advisory_count = sum(
                1
                for item in novelty_all_candidates
                if str(((item.get("st_exposure_guard") or {}).get("mode") or "")).lower() == "advisory"
                and (item.get("st_exposure_guard") or {}).get("passed") is not True
            )
            context_pack = _build_orchestrator_context_pack(run_id=run_id, round_id=round_id, stage="novelty_review", contract=contract, round_events=round_events)
            novelty_review = _complete_orchestrator_stage_json(
                client=client,
                run_id=run_id,
                round_id=round_id,
                stage="novelty_review",
                context_pack=context_pack,
                stage_input={
                    "score_review_summary": score_review,
                    "novelty_results": novelty_payload,
                    "batch_similarity": novelty_payload.get("batch_similarity") or [],
                    "active_pool_similarity": novelty_payload.get("details") or [],
                    "code_advice": novelty_adv,
                },
                lineage_context=lineage,
                round_events=round_events,
                max_tokens=6000,
            )
            code_novelty_ok = [candidate for candidate in novelty_candidates if _novelty_candidate_code_keeper(candidate)]
            deep_inputs, novelty_keeper_audit = _code_authoritative_allowed_candidates(
                code_novelty_ok,
                novelty_review,
                allow_actions={"advance_to_deep_validation"},
                code_keeper=_novelty_candidate_code_keeper,
                stage_label="novelty_review",
            )
            novelty_review = {
                **novelty_review,
                "code_keeper_audit": novelty_keeper_audit,
            }
            event = _record_llm_stage_event(
                run_id=run_id,
                round_id=round_id,
                stage_seq=7,
                stage="novelty_review",
                previous_stage=previous_stage,
                previous_stage_id=previous_stage_id,
                result=novelty_review,
                summary=(
                    f"Novelty & 困境代理诊断完成，工具保留 {len(novelty_candidates)}/{len(keepers)}，"
                    f"严格代码 keeper={len(code_novelty_ok)}，hard ST veto={st_veto_count}，advisory flag={st_advisory_count}，"
                    f"DeepSeek 建议={novelty_keeper_audit.get('llm_selected_count', 0)}，代码保底={novelty_keeper_audit.get('code_fallback_count', 0)}，"
                    f"实际进入 deep={len(deep_inputs)}。"
                ),
                default_next_stage="deep_validation_review" if deep_inputs else "round_synthesis",
                default_next_action="run_deep_validation_for_code_keeper_novel_candidates" if deep_inputs else "synthesize_novelty_rejections",
                evidence_refs=[
                    {
                        "tool": "fxalpha_novelty_check",
                        "gate": "novelty_distress_proxy",
                        "keepers": len(novelty_candidates),
                        "dropped": len(novelty_payload.get("dropped") or []),
                        "st_exposure_hard_vetoed": st_veto_count,
                        "distress_proxy_advisory_flagged": st_advisory_count,
                    },
                    novelty_keeper_audit,
                    _orchestrator_recovery_checkpoint(
                        round_id=round_id,
                        stage="novelty_review",
                        thesis=thesis,
                        hypothesis=hypothesis,
                        candidates=candidates,
                        planned_candidates=planned_candidates,
                        candidate_plan=candidate_plan,
                        candidate_precheck=candidate_precheck,
                        resume_stage="deep_validation_review" if deep_inputs else "round_synthesis",
                        stage_candidates=deep_inputs or novelty_review_candidates,
                        completed_task_refs=[novelty_keeper_audit],
                    ),
                ],
                candidate_lanes=novelty_payload,
                advice=novelty_adv,
                allowed_actions=novelty_adv.get("allowed_actions", []),
                blocked_actions=novelty_adv.get("blocked_actions", []),
            )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]
            if not deep_inputs:
                return_handoff = _return_handoff_from_stage(
                    "novelty_review",
                    novelty_review,
                    round_id=round_id,
                    code_advice=novelty_adv,
                )
                previous_review_advice.append(return_handoff)
                resume_next_stage, resume_next_action = _round_synthesis_defaults(
                    return_handoff=return_handoff,
                    round_no=round_no,
                    inputs=inputs,
                    adopted_total=adopted_total,
                    fallback="expression_design",
                )
                synthesis = _complete_orchestrator_stage_json(
                    client=client,
                    run_id=run_id,
                    round_id=round_id,
                    stage="round_synthesis",
                    context_pack=context_pack,
                    stage_input={
                        "failed_candidates": novelty_payload,
                        "tool_evidence_summary": novelty_payload,
                        "authoritative_outcome": _authoritative_outcome_from_llm(
                            from_stage="novelty_review",
                            result=novelty_review,
                            fallback_next_stage=resume_next_stage,
                            fallback_next_action=resume_next_action,
                        ),
                        "code_advice": novelty_adv,
                        "llm_decision_chain": round_events,
                    },
                    lineage_context=lineage,
                    round_events=round_events,
                    return_handoff=return_handoff,
                    max_tokens=4200,
                )
                synthesis = _round_synthesis_resume_transition(
                    synthesis,
                    fallback_next_stage=resume_next_stage,
                    fallback_next_action=resume_next_action,
                )
                event = _record_llm_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=8,
                    stage="round_synthesis",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    result=synthesis,
                    summary="本轮 novelty 未放行 deep 候选，已形成下一轮 handoff。",
                    default_next_stage=resume_next_stage,
                    default_next_action=resume_next_action,
                    stop_reason=_round_stop_reason(round_no, inputs, adopted_total),
                )
                round_events.append(event)
                previous_stage, previous_stage_id = event["stage"], event["stage_id"]
                return_handoff = _adopt_round_synthesis_handoff(
                    previous_review_advice,
                    synthesis,
                    event,
                    fallback_next_stage=resume_next_stage,
                )
                continue

            event = _orchestrator_stage_event(
                run_id=run_id,
                round_id=round_id,
                stage_seq=7,
                stage="deep_validation_review",
                previous_stage=previous_stage,
                previous_stage_id=previous_stage_id,
                summary=f"开始深验：准备对 {len(deep_inputs)} 个 novelty 通过候选执行 backtest / anti_overfit / rolling / adversarial。",
                decision="进入 deep validation 工具阶段，等待深度证据返回。",
                next_stage="deep_validation_review",
                next_action="run_deep_validation_in_progress",
                event_type="checkpoint",
                evidence_refs=[
                    {
                        "tool": "deep_validation",
                        "candidate_count": len(deep_inputs),
                    }
                ],
                tags=["tool_progress", "deep_validation_progress"],
                candidate_lanes=deep_inputs,
            )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]

            deep_candidates: list[dict] = []
            for idx, candidate in enumerate(deep_inputs, 1):
                _raise_if_orchestrator_stop_requested(run_id)
                progress_ref = {
                    **_orchestrator_tool_intent(tool="deep_validation", candidate=candidate, contract=contract),
                    "candidate_index": idx,
                    "candidate_total": len(deep_inputs),
                    "candidate_id": candidate.get("candidate_id"),
                    "candidate_brief": _candidate_progress_brief(candidate),
                }
                progress_event = _orchestrator_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=7,
                    stage="deep_validation_review",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    summary=f"深验进行中：{idx}/{len(deep_inputs)}",
                    decision=f"正在执行 backtest/anti/rolling/adversarial：{_candidate_progress_brief(candidate) or f'candidate_{idx}'}",
                    next_stage="deep_validation_review",
                    next_action="run_deep_validation_in_progress",
                    event_type="checkpoint",
                    evidence_refs=[progress_ref],
                    tags=["tool_progress", "deep_validation_progress", "candidate_progress"],
                    stage_id_suffix=f"candidate_{idx}_{candidate.get('candidate_id') or ''}",
                    candidate_lanes=[candidate],
                )
                round_events.append(progress_event)
                previous_stage, previous_stage_id = progress_event["stage"], progress_event["stage_id"]
                deep_candidates.append(_deep_validate_candidate_with_evidence_retry(candidate, contract=contract))
                _raise_if_orchestrator_stop_requested(run_id)
            if _orchestrator_round_event_budget_exceeded(round_events):
                raise DeepSeekClientError(
                    f"orchestrator_event_budget_exceeded:{len(round_events)}>{FACTOR_ORCHESTRATOR_EVENT_BUDGET}"
                )
            infrastructure_errors = [item for item in deep_candidates if _is_orchestrator_tool_infrastructure_error(item)]
            if infrastructure_errors:
                blocker_refs = [
                    {
                        "candidate_id": item.get("candidate_id"),
                        "tool": item.get("source_tool") or "deep_validation",
                        "expression": item.get("expression"),
                        "error": item.get("error"),
                        "error_type": item.get("error_type"),
                        "execution": item.get("execution"),
                        "returncode": item.get("returncode"),
                        "stdout_tail": item.get("stdout_tail"),
                        "stderr_tail": item.get("stderr_tail"),
                    }
                    for item in infrastructure_errors[:6]
                ]
                event = _orchestrator_stage_event(
                    run_id=run_id,
                    round_id=f"{run_id}:blocker",
                    stage_seq=99,
                    stage="blocker",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    summary="deep_validation 工具基础设施失败，已停止 Orchestrator；不把本轮候选作为因子质量失败学习。",
                    decision=f"tool_infrastructure_blocker：{len(infrastructure_errors)}/{len(deep_candidates)} 个候选出现 worker/systemd/timeout 级错误。",
                    next_stage="blocker",
                    next_action="fix_deep_validation_worker_before_restart",
                    event_type="blocker",
                    evidence_refs=blocker_refs,
                    tags=["blocker", "tool_infrastructure_blocker", "deep_validation_review"],
                    priority="high",
                    candidate_lanes=deep_candidates,
                )
                _orchestrator_set_job(
                    run_id,
                    status="failed",
                    stage="blocker",
                    event={
                        "event": "orchestrator_failed",
                        "error": "deep_validation_tool_infrastructure_blocker",
                        "stage_id": event.get("stage_id"),
                    },
                )
                return
            deep_missing_evidence, deep_system_errors = _deep_evidence_diagnostics(deep_candidates)
            if deep_missing_evidence:
                event = _orchestrator_stage_event(
                    run_id=run_id,
                    round_id=f"{run_id}:blocker",
                    stage_seq=99,
                    stage="blocker",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    summary="deep_validation 必需证据在一次原样重放后仍不完整，已停止；不调用 LLM，也不把候选记为因子质量失败。",
                    decision=f"deep_validation_evidence_incomplete_after_retry：{len(deep_missing_evidence)}/{len(deep_candidates)} 个候选缺少必需组件。",
                    next_stage="blocker",
                    next_action="restore_deep_validation_evidence_before_restart",
                    event_type="blocker",
                    evidence_refs=deep_missing_evidence,
                    tags=["blocker", "deep_validation_evidence_incomplete", "deep_validation_review"],
                    priority="high",
                    candidate_lanes=deep_candidates,
                )
                _orchestrator_set_job(
                    run_id,
                    status="failed",
                    stage="blocker",
                    event={
                        "event": "orchestrator_failed",
                        "error": "deep_validation_evidence_incomplete_after_retry",
                        "stage_id": event.get("stage_id"),
                    },
                )
                return
            deep_adv = deep_advice(
                deep_candidates,
                trajectory=_orchestrator_run_candidate_trajectory(
                    run_id=run_id,
                    stage="deep_validation_review",
                ),
            )
            deep_lane_decisions = deep_adv.get("candidate_lane_decisions") or []
            code_gate_ready = [
                candidate
                for candidate, lane in zip(deep_candidates, deep_lane_decisions)
                if isinstance(lane, dict) and lane.get("action") == "submit_quality_gate"
            ]
            deep_evidence_refs: list[dict[str, Any]] = []
            for idx, item in enumerate(deep_candidates):
                lane = deep_lane_decisions[idx] if idx < len(deep_lane_decisions) and isinstance(deep_lane_decisions[idx], dict) else {}
                deep_evidence_refs.append(
                    {
                        "candidate_id": item.get("candidate_id"),
                        "tool": "deep_validation",
                        "quick_score": item.get("quick_score") or item.get("score"),
                        "ic": (item.get("backtest_summary") or {}).get("ic_mean"),
                        "icir": (item.get("backtest_summary") or {}).get("ic_ir"),
                        "deep_score": lane.get("deep_score"),
                        "deep_action": lane.get("action"),
                        "deep_reason": lane.get("reason"),
                        "anti_overfit_score": (item.get("anti_overfit") or {}).get("score"),
                        "adversarial_score": (item.get("adversarial_validation") or {}).get("score"),
                        "novelty_score": (item.get("novelty_guard") or {}).get("novelty_score"),
                    }
                )
            context_pack = _build_orchestrator_context_pack(run_id=run_id, round_id=round_id, stage="deep_validation_review", contract=contract, round_events=round_events)
            deep_review = _complete_orchestrator_stage_json(
                client=client,
                run_id=run_id,
                round_id=round_id,
                stage="deep_validation_review",
                context_pack=context_pack,
                stage_input={
                    "score_review_summary": score_review,
                    "novelty_review_summary": novelty_review,
                    "deep_results": {
                        "candidates": [_compact_orchestrator_candidate_for_diagnosis(item) for item in deep_candidates],
                        "evidence_refs": deep_evidence_refs,
                        "missing_evidence": deep_missing_evidence,
                        "system_errors": deep_system_errors,
                    },
                    "trajectory_metrics": deep_adv.get("trajectory_metrics") or {},
                    "code_advice": deep_adv,
                },
                lineage_context=lineage,
                round_events=round_events,
                max_tokens=6000,
            )
            # Preserve the model's bounded research return (thesis /
            # hypothesis / expression) before the immediate pipeline
            # transition is forced to round_synthesis.  The latter is an
            # execution stage, not the next round's research entry.
            deep_research_review = (
                deep_review
                if code_gate_ready
                else _deep_research_review_before_synthesis(deep_review, deep_adv)
            )
            deep_research_handoff = _return_handoff_from_stage(
                "deep_validation_review",
                deep_research_review,
                evidence_refs=deep_evidence_refs,
                round_id=round_id,
                code_advice=deep_adv,
            )
            gate_ready, llm_gate_ready = _code_authoritative_gate_candidates(code_gate_ready, deep_review)
            deep_next_stage = "import_gate_review" if gate_ready else "round_synthesis"
            deep_next_action = "run_quality_gate_for_ready_candidates" if gate_ready else "synthesize_deep_failures"
            deep_review = _force_code_transition(
                deep_review,
                next_stage=deep_next_stage,
                next_action=deep_next_action,
                reason=f"code_gate_ready={len(code_gate_ready)}; llm_selected={len(llm_gate_ready)}; code_authoritative_gate=true",
            )
            event = _record_llm_stage_event(
                run_id=run_id,
                round_id=round_id,
                stage_seq=8,
                stage="deep_validation_review",
                previous_stage=previous_stage,
                previous_stage_id=previous_stage_id,
                result=deep_review,
                summary=f"Deep validation 完成，代码 gate-ready={len(code_gate_ready)}/{len(deep_candidates)}，DeepSeek 建议送 gate={len(llm_gate_ready)}，实际送 gate={len(gate_ready)}。",
                default_next_stage=deep_next_stage,
                default_next_action=deep_next_action,
                evidence_refs=[
                    *deep_evidence_refs,
                    _orchestrator_recovery_checkpoint(
                        round_id=round_id,
                        stage="deep_validation_review",
                        thesis=thesis,
                        hypothesis=hypothesis,
                        candidates=candidates,
                        planned_candidates=planned_candidates,
                        candidate_plan=candidate_plan,
                        candidate_precheck=candidate_precheck,
                        resume_stage=deep_next_stage,
                        stage_candidates=gate_ready or deep_candidates,
                        completed_task_refs=deep_evidence_refs,
                    ),
                ],
                candidate_lanes=deep_candidates,
                advice=deep_adv,
                allowed_actions=deep_adv.get("allowed_actions", []),
                blocked_actions=deep_adv.get("blocked_actions", []),
            )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]
            if not gate_ready:
                return_handoff = deep_research_handoff
                previous_review_advice.append(return_handoff)
                resume_next_stage, resume_next_action = _round_synthesis_defaults(
                    return_handoff=return_handoff,
                    round_no=round_no,
                    inputs=inputs,
                    adopted_total=adopted_total,
                    fallback="expression_design",
                )
                synthesis = _complete_orchestrator_stage_json(
                    client=client,
                    run_id=run_id,
                    round_id=round_id,
                    stage="round_synthesis",
                    context_pack=context_pack,
                    stage_input={
                        "failed_candidates": deep_candidates,
                        "tool_evidence_summary": deep_evidence_refs,
                        "authoritative_outcome": _authoritative_outcome_from_llm(
                            from_stage="deep_validation_review",
                            result=deep_research_review,
                            fallback_next_stage=resume_next_stage,
                            fallback_next_action=resume_next_action,
                        ),
                        "code_advice": deep_adv,
                        "llm_decision_chain": round_events,
                    },
                    lineage_context=lineage,
                    round_events=round_events,
                    return_handoff=return_handoff,
                    max_tokens=4200,
                )
                synthesis = _round_synthesis_resume_transition(
                    synthesis,
                    fallback_next_stage=resume_next_stage,
                    fallback_next_action=resume_next_action,
                )
                event = _record_llm_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=9,
                    stage="round_synthesis",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    result=synthesis,
                    summary="本轮 deep 未放行 gate 候选，已形成下一轮 handoff。",
                    default_next_stage=resume_next_stage,
                    default_next_action=resume_next_action,
                    stop_reason=_round_stop_reason(round_no, inputs, adopted_total),
                )
                round_events.append(event)
                previous_stage, previous_stage_id = event["stage"], event["stage_id"]
                return_handoff = _adopt_round_synthesis_handoff(
                    previous_review_advice,
                    synthesis,
                    event,
                    fallback_next_stage=resume_next_stage,
                )
                continue

            named_gate_ready = [ensure_factor_naming(candidate)[0] for candidate in gate_ready]
            gate_result = factor_tool_quality_gate(
                candidates=named_gate_ready,
                start_date=contract.get("selection_start_date") or FACTOR_DEFAULT_START_DATE,
                end_date=contract.get("selection_end_date") or FACTOR_DEFAULT_END_DATE,
                min_abs_ic=float(inputs.get("min_abs_ic") or 0.02),
                min_ir=float(inputs.get("min_ir") or 0.3),
                stage="orchestrator",
                round_no=round_no,
                trusted_novelty_evidence=True,
                run_id=run_id,
                round_id=round_id,
            )
            gate_payload = _orchestrator_tool_result_payload(gate_result)
            adopted = _with_candidate_ids(gate_payload.get("adopted") or [])
            gate_adv = gate_advice(adopted + (gate_payload.get("rejected") or named_gate_ready))
            official_importable = _official_gate_import_candidates(adopted)
            context_pack = _build_orchestrator_context_pack(run_id=run_id, round_id=round_id, stage="import_gate_review", contract=contract, round_events=round_events)
            gate_review = _complete_orchestrator_stage_json(
                client=client,
                run_id=run_id,
                round_id=round_id,
                stage="import_gate_review",
                context_pack=context_pack,
                stage_input={
                    "deep_review_summary": deep_review,
                    "quality_gate_results": gate_payload,
                    "metadata_check": {
                        "candidate_count": len(named_gate_ready),
                        "evidence_complete": True,
                        "holding_period": contract.get("holding_period"),
                    },
                    "missing_evidence": deep_missing_evidence,
                    "code_advice": gate_adv,
                },
                lineage_context=lineage,
                round_events=round_events,
                max_tokens=5000,
            )
            import_candidates = _apply_import_gate_factor_names(
                _llm_allowed_candidates(official_importable, gate_review, allow_actions={"import"}),
                gate_review,
            )
            gate_next_stage = "import_review" if import_candidates else "round_synthesis"
            gate_next_action = "auto_import_gate_adopted_candidates" if import_candidates else "synthesize_gate_feedback"
            gate_review = _force_code_transition(
                gate_review,
                next_stage=gate_next_stage,
                next_action=gate_next_action,
                reason=(
                    f"official_adopted={len(adopted)}; gate_result_passed={len(official_importable)}; "
                    f"llm_selected_for_import={len(import_candidates)}"
                ),
            )
            event = _record_llm_stage_event(
                run_id=run_id,
                round_id=round_id,
                stage_seq=9,
                stage="import_gate_review",
                previous_stage=previous_stage,
                previous_stage_id=previous_stage_id,
                result=gate_review,
                summary=(
                    f"Quality gate 完成，official adopted={len(adopted)}，"
                    f"gate_result.passed={len(official_importable)}，DeepSeek 包装核验后允许 import={len(import_candidates)}。"
                ),
                default_next_stage=gate_next_stage,
                default_next_action=gate_next_action,
                evidence_refs=[
                    {
                        "tool": "fxalpha_quality_gate",
                        "adopted": len(adopted),
                        "gate_result_passed": len(official_importable),
                        "rejected": len(gate_payload.get("rejected") or []),
                    },
                    _orchestrator_recovery_checkpoint(
                        round_id=round_id,
                        stage="import_gate_review",
                        thesis=thesis,
                        hypothesis=hypothesis,
                        candidates=candidates,
                        planned_candidates=planned_candidates,
                        candidate_plan=candidate_plan,
                        candidate_precheck=candidate_precheck,
                        resume_stage=gate_next_stage,
                        stage_candidates=import_candidates or named_gate_ready,
                        completed_task_refs=[
                            {
                                "tool": "fxalpha_quality_gate",
                                "adopted": len(adopted),
                                "gate_result_passed": len(official_importable),
                            }
                        ],
                    ),
                ],
                candidate_lanes=gate_payload,
                advice=gate_adv,
                allowed_actions=gate_adv.get("allowed_actions", []),
                blocked_actions=gate_adv.get("blocked_actions", []),
            )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]
            if not import_candidates:
                return_handoff = _return_handoff_from_stage(
                    "import_gate_review",
                    gate_review,
                    round_id=round_id,
                )
                previous_review_advice.append(return_handoff)
                resume_next_stage, resume_next_action = _round_synthesis_defaults(
                    return_handoff=return_handoff,
                    round_no=round_no,
                    inputs=inputs,
                    adopted_total=adopted_total,
                    fallback="expression_design",
                )
                synthesis = _complete_orchestrator_stage_json(
                    client=client,
                    run_id=run_id,
                    round_id=round_id,
                    stage="round_synthesis",
                    context_pack=context_pack,
                    stage_input={
                        "failed_candidates": gate_payload,
                        "tool_evidence_summary": gate_payload,
                        "authoritative_outcome": _authoritative_outcome_from_llm(
                            from_stage="import_gate_review",
                            result=gate_review,
                            fallback_next_stage=resume_next_stage,
                            fallback_next_action=resume_next_action,
                        ),
                        "llm_decision_chain": round_events,
                    },
                    lineage_context=lineage,
                    round_events=round_events,
                    return_handoff=return_handoff,
                    max_tokens=4200,
                )
                synthesis = _round_synthesis_resume_transition(
                    synthesis,
                    fallback_next_stage=resume_next_stage,
                    fallback_next_action=resume_next_action,
                )
                event = _record_llm_stage_event(
                    run_id=run_id,
                    round_id=round_id,
                    stage_seq=10,
                    stage="round_synthesis",
                    previous_stage=previous_stage,
                    previous_stage_id=previous_stage_id,
                    result=synthesis,
                    summary="本轮 gate 未进入 import，已形成下一轮 handoff。",
                    default_next_stage=resume_next_stage,
                    default_next_action=resume_next_action,
                    stop_reason=_round_stop_reason(round_no, inputs, adopted_total),
                )
                round_events.append(event)
                previous_stage, previous_stage_id = event["stage"], event["stage_id"]
                return_handoff = _adopt_round_synthesis_handoff(
                    previous_review_advice,
                    synthesis,
                    event,
                    fallback_next_stage=resume_next_stage,
                )
                continue

            event = _orchestrator_stage_event(
                run_id=run_id,
                round_id=round_id,
                stage_seq=10,
                stage="import_review",
                previous_stage=previous_stage,
                previous_stage_id=previous_stage_id,
                summary=f"开始自动 import：准备导入 {len(import_candidates)} 个 gate 通过候选。",
                decision="进入隔离 import 子进程；等待 parquet/value sync/registry 写入返回。",
                next_stage="import_review",
                next_action="fxalpha_import_factors_in_progress",
                event_type="checkpoint",
                evidence_refs=[
                    {
                        "tool": "fxalpha_import_factors",
                        "candidate_count": len(import_candidates),
                        "execution": "isolated_subprocess",
                    }
                ],
                candidate_lanes={"import_candidates": import_candidates},
                tags=["tool_progress", "import_review_progress"],
            )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]

            import_result = factor_tool_import(
                candidates=import_candidates,
                universe=contract.get("universe", FACTOR_DEFAULT_UNIVERSE),
                start_date=contract.get("value_start_date") or FACTOR_VALUE_DEFAULT_START_DATE,
                end_date=contract.get("value_end_date") or FACTOR_VALUE_DEFAULT_END_DATE,
                selection_start_date=contract.get("selection_start_date") or FACTOR_DEFAULT_START_DATE,
                selection_end_date=contract.get("selection_end_date") or FACTOR_DEFAULT_END_DATE,
                submit_wq=bool(contract.get("submit_wq")),
                run_id=run_id,
                round_id=round_id,
            )
            import_payload = _orchestrator_tool_result_payload(import_result)
            imported_count, imported_items = _orchestrator_imported_count_and_items(import_payload, import_candidates)
            import_errors = import_payload.get("errors") or []
            import_sync_status = import_payload.get("import_sync_status") if isinstance(import_payload.get("import_sync_status"), dict) else {}
            import_ok = imported_count == len(import_candidates) and not import_errors
            if import_ok:
                adopted_total += imported_count
            context_pack = _build_orchestrator_context_pack(run_id=run_id, round_id=round_id, stage="import_review", contract=contract, round_events=round_events)
            import_review = _complete_orchestrator_stage_json(
                client=client,
                run_id=run_id,
                round_id=round_id,
                stage="import_review",
                context_pack=context_pack,
                stage_input={
                    "gate_review_summary": gate_review,
                    "import_results": import_payload,
                    "registry_summary": import_payload.get("registry_summary") or {},
                    "import_sync_status": import_sync_status,
                    "adopted_total": adopted_total,
                    "code_advice": {
                        "import_summary": {
                            "requested_count": len(import_candidates),
                            "imported_count": imported_count,
                            "failed_count": max(0, len(import_candidates) - imported_count),
                        },
                        "warnings": import_errors,
                    },
                },
                lineage_context=lineage,
                round_events=round_events,
                max_tokens=4000,
            )
            import_review = _force_code_transition(
                import_review,
                next_stage="round_synthesis" if import_ok else "import_review",
                next_action="write_round_synthesis" if import_ok else "repair_import",
                reason=f"import_tool_completed; imported={imported_count}/{len(import_candidates)}; errors={len(import_errors)}",
            )
            if not import_ok:
                import_review["decision"] = "import_failed"
                import_review["judgment"] = (
                    f"自动 import 未确认成功：imported={imported_count}/{len(import_candidates)}，"
                    f"errors={import_errors[:3]}。active registry 未确认增加，必须修复后重试。"
                )
                import_review["why"] = "严格入库要求 registry 写入和因子值落盘均成功；imported=0 或存在 errors 不能标记为成功。"
            event = _record_llm_stage_event(
                run_id=run_id,
                round_id=round_id,
                stage_seq=11,
                stage="import_review",
                previous_stage=previous_stage,
                previous_stage_id=previous_stage_id,
                result=import_review,
                summary=_orchestrator_import_event_summary(
                    import_ok=import_ok,
                    imported_count=imported_count,
                    requested_count=len(import_candidates),
                    adopted_total=adopted_total,
                    import_sync_status=import_sync_status,
                ),
                default_next_stage="round_synthesis" if import_ok else "import_review",
                default_next_action="write_round_synthesis" if import_ok else "repair_import",
                evidence_refs=[
                    {
                        "tool": "fxalpha_import_factors",
                        "imported": imported_count,
                        "errors": import_errors,
                        "registry_imported": import_payload.get("registry_imported"),
                        "active_values_refresh_status": import_payload.get("active_values_refresh_status"),
                        "model_feature_refresh_status": import_payload.get("model_feature_refresh_status"),
                        "import_sync_status": import_sync_status,
                    }
                ],
                candidate_lanes=import_payload,
                event_type="llm_result",
            )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]
            if not import_ok:
                _orchestrator_set_job(
                    run_id,
                    status="blocked",
                    stage="import_review",
                    event={
                        "event": "orchestrator_import_failed",
                        "round_id": round_id,
                        "imported": imported_count,
                        "expected_imported": len(import_candidates),
                        "errors": import_errors,
                    },
                )
                return

            resume_next_stage, resume_next_action = _round_synthesis_defaults(
                return_handoff=None,
                round_no=round_no,
                inputs=inputs,
                adopted_total=adopted_total,
                fallback="thesis_design",
            )
            synthesis = _complete_orchestrator_stage_json(
                client=client,
                run_id=run_id,
                round_id=round_id,
                stage="round_synthesis",
                context_pack=context_pack,
                stage_input={
                    "round_events": round_events,
                    "tool_evidence_summary": {"score": score_refs, "novelty": novelty_payload, "deep": deep_evidence_refs, "gate": gate_payload, "import": import_payload},
                    "authoritative_outcome": _authoritative_outcome_from_llm(
                        from_stage="import_review",
                        result=import_review,
                        fallback_next_stage=resume_next_stage,
                        fallback_next_action=resume_next_action,
                    ),
                    "llm_decision_chain": round_events,
                    "adopted_factors": imported_items,
                    "failed_candidates": [],
                },
                lineage_context=lineage,
                round_events=round_events,
                max_tokens=4200,
            )
            synthesis = _round_synthesis_resume_transition(
                synthesis,
                fallback_next_stage=resume_next_stage,
                fallback_next_action=resume_next_action,
            )
            event = _record_llm_stage_event(
                run_id=run_id,
                round_id=round_id,
                stage_seq=11,
                stage="round_synthesis",
                previous_stage=previous_stage,
                previous_stage_id=previous_stage_id,
                result=synthesis,
                summary="本轮 round_synthesis 完成，已生成下一轮短期 handoff。",
                default_next_stage=resume_next_stage,
                default_next_action=resume_next_action,
                stop_reason=_round_stop_reason(round_no, inputs, adopted_total),
                evidence_refs=[],
            )
            round_events.append(event)
            previous_stage, previous_stage_id = event["stage"], event["stage_id"]
            return_handoff = _adopt_round_synthesis_handoff(
                previous_review_advice,
                synthesis,
                event,
                evidence_refs=[],
                fallback_next_stage=resume_next_stage,
            )
            if adopted_total >= int(inputs.get("target_adopted") or 1):
                break

        _orchestrator_stage_event(
            run_id=run_id,
            round_id=f"{run_id}:stop",
            stage_seq=99,
            stage="checkpoint_stop",
            previous_stage=previous_stage,
            previous_stage_id=previous_stage_id,
            summary=f"Orchestrator 后台运行结束，累计 imported={adopted_total}。",
            decision="达到目标或轮次结束，停止后台任务。",
            next_stage="checkpoint_stop",
            next_action="idle",
            event_type="checkpoint",
            evidence_refs=[{"adopted_total": adopted_total}],
            tags=["checkpoint_stop"],
        )
        _orchestrator_set_job(run_id, status="completed", stage="checkpoint_stop", event={"event": "orchestrator_completed", "adopted_total": adopted_total})
    except OrchestratorStopRequested as control_exc:
        control_action = control_exc.action if control_exc.action in {"pause", "stop"} else "stop"
        paused = control_action == "pause"
        control_label = "暂停" if paused else "结束"
        recovery_checkpoint = _latest_orchestrator_recovery_checkpoint(run_id) if paused else {}
        completion_ref = {
            "type": f"operator_{control_action}_completed",
            "control_request_id": control_exc.request_id,
            "adopted_total": adopted_total,
        }
        event = _orchestrator_stage_event(
            run_id=run_id,
            round_id=f"{run_id}:stop",
            stage_seq=99,
            stage="checkpoint_stop",
            previous_stage=previous_stage,
            previous_stage_id=previous_stage_id,
            summary=f"Orchestrator 已按操作员请求安全{control_label}，累计 imported={adopted_total}。",
            decision=(
                "operator_pause_completed：已保存恢复检查点，可继续同一 run。"
                if paused
                else "operator_stop_completed：本次 run 已正式结束；下次应创建新 run。"
            ),
            next_stage="checkpoint_stop",
            next_action="idle",
            event_type="checkpoint",
            evidence_refs=[completion_ref, *([recovery_checkpoint] if recovery_checkpoint else [])],
            tags=["checkpoint_stop", f"operator_{control_action}"],
            priority="high",
            heartbeat_status="paused" if paused else "stopped",
            control_action=control_action,
            control_request_id=control_exc.request_id,
        )
        _orchestrator_set_job(
            run_id,
            status="paused" if paused else "completed",
            stage="checkpoint_stop",
            event={
                "event": "orchestrator_paused" if paused else "orchestrator_stopped",
                "adopted_total": adopted_total,
                "stage_id": event.get("stage_id"),
            },
        )
    except BaseException as exc:
        trace_id = getattr(exc, "orchestrator_llm_trace_id", None)
        blocker_refs = [{"error": str(exc)[:500], "type": exc.__class__.__name__}]
        if trace_id:
            blocker_refs.append(
                {
                    "type": "llm_trace",
                    "trace_id": trace_id,
                    "trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
                    "payload_chars": getattr(exc, "orchestrator_llm_payload_chars", None),
                }
            )
        event = _orchestrator_stage_event(
            run_id=run_id,
            round_id=f"{run_id}:blocker",
            stage_seq=99,
            stage="blocker",
            previous_stage=previous_stage,
            previous_stage_id=previous_stage_id,
            summary="Orchestrator 后台任务遇到系统、工具或 LLM schema 问题，已停止。",
            decision=f"阻塞原因：{str(exc)[:160]}",
            next_stage="blocker",
            next_action="fix_orchestrator_runtime_issue_before_restart",
            event_type="blocker",
            evidence_refs=blocker_refs,
            tags=["blocker"],
            priority="high",
            llm_trace_id=trace_id,
        )
        _orchestrator_set_job(run_id, status="failed", stage="blocker", event={"event": "orchestrator_failed", "error": str(exc)[:500], "stage_id": event.get("stage_id")})


def _start_orchestrator_background(run_id: str, inputs: dict, contract: dict) -> OrchestratorWorkerHandle:
    """Launch ORCH outside the API process so API restarts do not kill research."""
    run_text = str(run_id or "").strip()
    worker_script = Path(__file__).resolve().parents[1] / "scripts" / "factor_research" / "orchestrator_worker.py"
    safe_run_marker = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_text).strip("-.")[-52:]
    unit = f"fxalpha-factor-orch-{safe_run_marker or hashlib.sha1(run_text.encode('utf-8')).hexdigest()[:12]}-{uuid.uuid4().hex[:6]}.service"
    command = [sys.executable, str(worker_script), "--run-id", run_text, "--worker-unit", unit]
    systemd_command = [
        "systemd-run",
        "--user",
        f"--unit={unit.removesuffix('.service')}",
        "--collect",
        "--property=Restart=no",
        "--property=KillMode=control-group",
        f"--property=WorkingDirectory={Path(__file__).resolve().parents[1]}",
        *command,
    ]
    try:
        completed = subprocess.run(
            systemd_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )
    except Exception as exc:
        completed = None
        systemd_error = str(exc)
    else:
        systemd_error = str(completed.stderr or "").strip()
    if completed is not None and completed.returncode == 0:
        _write_orchestrator_worker_event(
            run_id=run_text,
            action="launch_requested",
            unit=unit,
            mode="systemd_transient",
        )
        return OrchestratorWorkerHandle(
            name=f"fxalpha-orchestrator-{run_text}",
            unit=unit,
            mode="systemd_transient",
        )

    process = subprocess.Popen(
        command,
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    _write_orchestrator_worker_event(
        run_id=run_text,
        action="launch_requested",
        pid=process.pid,
        mode="detached_process",
    )
    handle = OrchestratorWorkerHandle(
        name=f"fxalpha-orchestrator-{run_text}",
        pid=process.pid,
        mode="detached_process",
    )
    handle.systemd_error = systemd_error
    return handle


RESEARCH_DECISION_STAGES = {
    "protocol_load",
    "pre_batch_decision",
    "thesis_design",
    "hypothesis_design",
    "expression_design",
    "candidate_plan",
    "score_review",
    "candidate_decision",
    "novelty_review",
    "deep_validation_review",
    "import_gate_review",
    "import_review",
    "round_synthesis",
    "checkpoint_stop",
    "blocker",
    # Backward-compatible legacy/display stages.
    "brief",
    "four_step_summary",
    "human_guidance",
    "note",
}


RESEARCH_STRICT_CONTRACT_STAGES = {
    "protocol_load",
    "pre_batch_decision",
    "thesis_design",
    "hypothesis_design",
    "expression_design",
    "candidate_plan",
    "score_review",
    "candidate_decision",
    "novelty_review",
    "deep_validation_review",
    "import_gate_review",
    "import_review",
    "round_synthesis",
    "checkpoint_stop",
    "blocker",
}


RESEARCH_STEP_DEPRECATED_EXTRA_KEYS = {
    # Flow-control fields belong in top-level stage_transition.
    "stage_transition",
    "four_step",
    "official_strategy",
    "consensus_next_action",
    "resume_cursor",
    "trajectory_snapshot",
    "next_allowed_action",
    # Large metric/evidence blobs belong in tool task store and should be
    # summarized in stage_transition.facts / judgment / history_used.
    "score_summary",
    "backtest_summary",
    "deep_evidence",
    "deep_validation",
    "gate_result",
    "quality_gate",
    "novelty_guard",
    "anti_overfit",
    "adversarial_validation",
    "rolling_validation",
    "metrics",
}


def _clean_research_step_extra(extra: dict) -> tuple[dict, list[str]]:
    """Keep research-step extra lightweight; remove old control/metric blobs."""
    if not isinstance(extra, dict):
        return {}, []
    clean: dict[str, Any] = {}
    removed: list[str] = []
    for key, value in extra.items():
        if key in RESEARCH_STEP_DEPRECATED_EXTRA_KEYS:
            removed.append(key)
            continue
        clean[key] = value
    return clean, removed


def _clean_stage_transition_payload(raw: dict | None, *, next_action: str = "") -> dict[str, str]:
    transition = _jsonable(raw) if raw else {}
    if not isinstance(transition, dict):
        transition = {}
    cleaned = {
        "next_stage": _clip_text(transition.get("next_stage", ""), 120),
        "next_action": _clip_text(transition.get("next_action", ""), 360),
        "research_strategy": _clip_text(transition.get("research_strategy", ""), 520),
        "facts": _clip_text(transition.get("facts", ""), 1800),
        "judgment": _clip_text(transition.get("judgment", ""), 700),
        "why": _clip_text(transition.get("why", ""), 700),
        "history_used": _clip_text(transition.get("history_used", ""), 900),
        "reason": _clip_text(transition.get("reason", ""), 700),
    }
    for optional_key, limit in {
        "llm_trace_id": 180,
        "mode": 80,
        "llm_model": 80,
        "confidence": 40,
    }.items():
        optional_value = _clip_text(transition.get(optional_key, ""), limit)
        if optional_value:
            cleaned[optional_key] = optional_value
    if next_action and not cleaned["next_action"]:
        cleaned["next_action"] = _clip_text(next_action, 260)
    return cleaned


def _normalize_round_stage_ids(
    *,
    run_id: str,
    round_no: int | None,
    round_id: str,
    stage_id: str,
    stage: str,
    stage_seq: int | None,
) -> tuple[str, str]:
    clean_round = str(round_id or "").strip()
    if not clean_round and round_no is not None:
        run_prefix = str(run_id or "").strip()
        clean_round = f"{run_prefix}:r{round_no:04d}" if run_prefix else f"round-r{round_no:04d}"
    if not clean_round:
        clean_round = "round-unset"
    clean_stage_id = str(stage_id or "").strip()
    if not clean_stage_id:
        seq_part = f"s{int(stage_seq):02d}" if stage_seq is not None else "s00"
        clean_stage_id = f"{clean_round}:{seq_part}_{stage}"
    return clean_round[:120], clean_stage_id[:180]


def _infer_research_stage_seq(round_id: str, explicit_stage_seq: int | None) -> int:
    if explicit_stage_seq is not None:
        try:
            return max(1, int(explicit_stage_seq))
        except Exception:
            return 1
    latest_for_round = [
        step
        for step in _read_recent_research_steps(limit=FACTOR_RESEARCH_STEPS_MAX_LINES)
        if str(step.get("round_id") or "") == str(round_id or "")
    ]
    seqs: list[int] = []
    for step in latest_for_round:
        try:
            seqs.append(int(step.get("stage_seq")))
        except Exception:
            continue
    return (max(seqs) + 1) if seqs else 1


def _latest_stage_transition(*, run_id: str = "", round_id: str = "") -> tuple[dict, dict]:
    """Return the latest transition for one run, never another run by accident."""
    clean_run_id = str(run_id or "").strip()
    clean_round_id = str(round_id or "").strip()
    steps = _read_recent_research_steps(limit=40, run_id=clean_run_id or None)
    if clean_round_id:
        steps = [step for step in steps if str(step.get("round_id") or "") == clean_round_id]
    latest = steps[0] if steps else {}
    transition = latest.get("stage_transition") if isinstance(latest.get("stage_transition"), dict) else {}
    return latest, transition


def _authoritative_status_and_pipeline(
    runtime_view: dict | None,
    decision_view: dict | None,
    quantgpt_summary: dict | None = None,
) -> tuple[str, dict]:
    runtime_view = runtime_view or {}
    decision_view = decision_view or {}
    quantgpt_summary = quantgpt_summary or {}
    running_count = int(quantgpt_summary.get("running_count") or 0)
    if running_count > 0:
        running_task = (quantgpt_summary.get("running_tasks") or [{}])[0]
        task_type = running_task.get("task_type") or "mcp_tool"
        return "running_mcp_tools", {
            "overall_status": "running_mcp_tools",
            "ok": True,
            "error": "",
            "active_run_id": runtime_view.get("run_id"),
            "active_stage": f"quantgpt:{task_type}",
            "message": f"QuantGPT MCP task is running: {task_type}",
        }
    if runtime_view.get("run_id"):
        runtime_status = str(runtime_view.get("status") or "")
        current_phase = runtime_view.get("current_phase") or runtime_view.get("stage_id") or runtime_view.get("stage_id")
        if (
            runtime_status not in {"research_completed", "completed", "research_blocked", "blocked", "failed"}
            and int(quantgpt_summary.get("running_count") or 0) <= 0
            and _runtime_view_looks_orphaned(runtime_view)
        ):
            overall_status = "research_blocked"
            message = "orchestrator_orphaned_after_service_restart_or_stale_quantgpt_task"
        elif runtime_status in {"research_completed", "completed"}:
            overall_status = "research_completed"
            message = runtime_view.get("current_action") or "Research run is complete."
        elif runtime_status in {"research_blocked", "blocked", "failed"}:
            overall_status = "research_blocked"
            message = runtime_view.get("current_action") or decision_view.get("decision") or "Research run is blocked."
        else:
            overall_status = "research_active"
            message = runtime_view.get("current_action") or decision_view.get("decision") or decision_view.get("summary") or ""
        return overall_status, {
            "overall_status": overall_status,
            "ok": True,
            "error": "",
            "active_run_id": runtime_view.get("run_id"),
            "active_stage": current_phase,
            "message": message,
        }
    return "idle", {"overall_status": "idle", "ok": True, "error": "", "message": "No active research steps recorded."}


def _runtime_view_with_authoritative_liveness(runtime_view: dict, pipeline: dict) -> dict:
    """Do not let an old Orchestrator event masquerade as a live worker."""
    runtime_view = dict(runtime_view or {})
    if (
        pipeline.get("overall_status") != "research_blocked"
        or pipeline.get("message") != "orchestrator_orphaned_after_service_restart_or_stale_quantgpt_task"
    ):
        return runtime_view
    latest_step = runtime_view.get("latest_step") if isinstance(runtime_view.get("latest_step"), dict) else {}
    transition = runtime_view.get("stage_transition") if isinstance(runtime_view.get("stage_transition"), dict) else {}
    runtime_view["status"] = "research_blocked"
    runtime_view["current_phase"] = "Blocked"
    runtime_view["current_action"] = "已中断，需重启"
    runtime_view["next_action"] = "restart_orchestrator_with_interrupted_handoff"
    runtime_view["heartbeat_status"] = "interrupted"
    runtime_view["interrupted"] = True
    runtime_view["interrupted_reason"] = "interrupted_by_api_restart_or_stale_heartbeat"
    runtime_view["last_visible_stage"] = {
        "run_id": runtime_view.get("run_id"),
        "round_id": runtime_view.get("round_id"),
        "stage_id": runtime_view.get("stage_id"),
        "stage": latest_step.get("stage") or runtime_view.get("current_phase"),
        "updated_at": runtime_view.get("updated_at"),
        "decision": runtime_view.get("latest_decision"),
        "next_action": transition.get("next_action") or runtime_view.get("next_action"),
    }
    latest = dict(latest_step)
    latest["status"] = "research_blocked"
    latest["heartbeat_status"] = "interrupted"
    latest["display_stage_override"] = "Blocked"
    runtime_view["latest_step"] = latest
    return runtime_view


def _orchestrator_run_has_recent_activity(
    run_id: str,
    *,
    within_s: int | None = None,
    max_lines: int = 400,
) -> bool:
    """Treat recent event/trace writes as liveness during long tool/LLM waits."""
    run_id = str(run_id or "").strip()
    if not run_id:
        return False
    within_s = int(within_s or max(180, FACTOR_ORCHESTRATOR_LLM_TIMEOUT_MAX + 60))
    latest_seen: datetime | None = None
    for path in (FACTOR_ORCHESTRATOR_EVENTS_FILE, FACTOR_ORCHESTRATOR_LLM_TRACES_FILE):
        try:
            tail: deque[str] = deque(maxlen=max_lines)
            with path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    raw = raw.strip()
                    if raw:
                        tail.append(raw)
        except Exception:
            continue
        for raw in reversed(tail):
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if str(obj.get("run_id") or "") != run_id:
                continue
            ts = obj.get("ts") or obj.get("created_at")
            if not ts:
                continue
            try:
                seen = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                continue
            if latest_seen is None or seen > latest_seen:
                latest_seen = seen
            break
    if latest_seen is None:
        return False
    now = datetime.now(latest_seen.tzinfo) if latest_seen.tzinfo is not None else datetime.now()
    age_s = now.timestamp() - latest_seen.timestamp()
    return age_s <= within_s


def _runtime_view_looks_orphaned(runtime_view: dict) -> bool:
    latest = runtime_view.get("latest_step") if isinstance(runtime_view.get("latest_step"), dict) else {}
    tags = {str(tag) for tag in (latest.get("tags") or [])}
    if "orchestrator" not in tags:
        return False
    stage = str(latest.get("stage") or "")
    if stage in {"checkpoint_stop", "blocker"}:
        return False
    ts = latest.get("ts") or latest.get("created_at") or runtime_view.get("updated_at")
    if not ts:
        return False
    try:
        updated = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        boot = FACTOR_API_BOOT_TS
        if boot.tzinfo is None and updated.tzinfo is not None:
            boot = boot.replace(tzinfo=updated.tzinfo)
        elif boot.tzinfo is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=boot.tzinfo)
        if updated < boot:
            return True
        now = datetime.now(updated.tzinfo) if updated.tzinfo is not None else datetime.now()
        age_s = now.timestamp() - updated.timestamp()
    except Exception:
        return False
    run_id = str(runtime_view.get("run_id") or "")
    if _orchestrator_run_has_recent_activity(run_id):
        return False
    return age_s > 60


def _stage_guard_result(
    tool_name: str,
    *,
    allowed_stages: set[str],
    run_id: str = "",
    round_id: str = "",
) -> ServiceResult | None:
    """Block critical production tool calls when the previous decision step is missing.

    The guard is intentionally lightweight: it only checks that Codex left a
    visible stage-transition decision before expensive or irreversible stages.
    It does not make factor-quality judgments.
    """
    if run_id or round_id:
        latest, transition = _latest_stage_transition(run_id=run_id, round_id=round_id)
    else:
        latest, transition = _latest_stage_transition()
    latest_stage = str(latest.get("stage") or "").strip()
    next_stage = str(transition.get("next_stage") or "").strip()
    next_action = str(transition.get("next_action") or transition.get("research_strategy") or "").strip()
    accepted = {stage for stage in (latest_stage, next_stage) if stage}
    if transition and accepted.intersection(allowed_stages):
        return None
    return err_result(
        (
            f"{tool_name} blocked: missing required research step stage_transition. "
            f"Record fxalpha_record_research_step before this tool."
        ),
        inputs={
            "tool": tool_name,
            "required_previous_stages": sorted(allowed_stages),
            "latest_stage": latest_stage,
            "latest_next_stage": next_stage,
            "latest_next_action": next_action,
            "latest_step_ts": latest.get("ts") or latest.get("created_at"),
            "run_id": str(run_id or ""),
            "round_id": str(round_id or ""),
        },
        outputs={
            "required_fix": (
                "Record the missing research process log before this tool. "
                "Use schema_version=research_step_v2 with previous_stage, stage, stage_transition.next_stage, "
                "next_action, facts, judgment, research_strategy, why, and history_used."
            ),
            "latest_research_step": latest,
        },
    )


def _augment_digest_with_research_steps(digest: dict, research_steps: list[dict]) -> dict:
    digest = dict(digest or {})
    digest["research_steps"] = research_steps[:20]
    if not research_steps:
        return digest
    qgpt_summary = digest.get("quantgpt_task_summary") or {}
    has_running_quantgpt_task = bool(qgpt_summary.get("running_count") or qgpt_summary.get("running_tasks"))
    latest = research_steps[0]
    digest["latest_llm_step"] = {
        "schema_version": latest.get("schema_version"),
        "ts": latest.get("ts"),
        "run_id": latest.get("run_id"),
        "round_id": latest.get("round_id"),
        "stage_seq": latest.get("stage_seq"),
        "stage_id": latest.get("stage_id"),
        "previous_stage": latest.get("previous_stage"),
        "previous_stage_id": latest.get("previous_stage_id"),
        "stage": latest.get("stage"),
        "summary": latest.get("summary"),
        "decision": latest.get("decision"),
        "next": latest.get("next"),
        "priority": latest.get("priority"),
        "refs": latest.get("refs") or [],
        "evidence_refs": latest.get("evidence_refs") or [],
        "tags": latest.get("tags") or [],
        "stage_transition": latest.get("stage_transition") if isinstance(latest.get("stage_transition"), dict) else {},
        "extra_removed_keys": latest.get("extra_removed_keys") or [],
        "extra": latest.get("extra") or {},
        "economic_thesis": latest.get("economic_thesis"),
        "target_horizon": latest.get("target_horizon") or (latest.get("extra") or {}).get("target_horizon"),
    }
    digest["run_id"] = latest.get("run_id") or digest.get("run_id")
    digest["round_id"] = latest.get("round_id") or digest.get("round_id")
    digest["stage"] = latest.get("stage") or digest.get("stage")
    digest["stage_transition"] = (
        latest.get("stage_transition")
        if isinstance(latest.get("stage_transition"), dict)
        else digest.get("stage_transition")
    )
    if has_running_quantgpt_task:
        # A live MCP tool is the strongest signal for "what is happening now".
        # Keep the LLM step available for display, but do not let an older brief
        # overwrite a running score/backtest/anti-overfit task in the cockpit.
        return digest
    terminal_status = str(digest.get("status") or "").lower()
    terminal_stage = str(digest.get("stage") or "").lower()
    if "failed" in terminal_status or "blocker" in terminal_stage or terminal_status in {"automation_completed", "completed"}:
        return digest
    if latest.get("stage") in RESEARCH_DECISION_STAGES:
        transition = latest.get("stage_transition") if isinstance(latest.get("stage_transition"), dict) else {}
        digest["current_action"] = (
            transition.get("next_action")
            or latest.get("summary")
            or latest.get("next")
            or digest.get("current_action")
        )
        digest["current_phase"] = {
            "protocol_load": "Protocol Load",
            "pre_batch_decision": "Pre-batch Decision",
            "brief": "Research Brief",
            "candidate_plan": "Candidate Plan",
            "score_review": "Score Review",
            "candidate_decision": "Candidate Decision",
            "novelty_review": "Novelty & ST Review",
            "deep_validation_review": "Deep Validation Review",
            "import_gate_review": "Import Gate Review",
            "import_review": "Import Review",
            "four_step_summary": "Four-step Summary",
            "checkpoint_stop": "Checkpoint Stop",
            "blocker": "Blocked",
        }.get(str(latest.get("stage")), digest.get("current_phase"))
        digest["updated_at"] = latest.get("ts") or digest.get("updated_at")
    return digest


def _compact_live_digest_for_console(digest: dict) -> dict:
    """Remove fields duplicated at the top level of /factor/console/live."""

    compact = dict(digest or {})
    for key in (
        "candidate_task_view",
        "recent_candidates",
        "tool_timeline",
        "research_steps",
        "candidate_records",
        "factor_library",
        "current_candidate_board",
        "run_view",
    ):
        compact.pop(key, None)
    return compact


def _research_step_run_id(step: dict) -> str:
    extra = step.get("extra") if isinstance(step.get("extra"), dict) else {}
    for value in (
        step.get("run_id"),
        extra.get("run_id"),
        extra.get("gui_job_run_id"),
        extra.get("automation_id"),
    ):
        if value:
            return str(value)[:160]
    return "research_steps"


def _research_step_summary_counts(steps: list[dict]) -> dict:
    summary: dict[str, Any] = {}

    def merge_value(target: str, value: Any) -> None:
        if value is None:
            return
        try:
            number = int(value)
        except Exception:
            summary[target] = value
            return
        current = summary.get(target)
        try:
            current_number = int(current)
        except Exception:
            current_number = 0
        summary[target] = max(current_number, number)

    for step in reversed(steps or []):
        extra = step.get("extra") if isinstance(step.get("extra"), dict) else {}
        research_state = extra.get("research_state") if isinstance(extra.get("research_state"), dict) else {}
        counts = extra.get("counts") if isinstance(extra.get("counts"), dict) else {}
        metrics = extra.get("metrics") if isinstance(extra.get("metrics"), dict) else {}
        for source in (research_state, counts, metrics, extra):
            merge_value("quick_screened_count", source.get("quick_screened_total") or source.get("quick_screened_count") or source.get("quick_screened"))
            merge_value("novelty_checked_count", source.get("novelty_checked_total") or source.get("novelty_checked_count") or source.get("novelty_checked"))
            merge_value("deep_validation_count", source.get("deep_validation_count") or source.get("deep_validation_total") or source.get("deep_validation"))
            merge_value("quality_gate_adopted_count", source.get("quality_gate_adopted") or source.get("quality_gate_adopted_count"))
            merge_value("quality_gate_rejected_count", source.get("quality_gate_rejected") or source.get("quality_gate_rejected_count"))
            merge_value("valid_imports", source.get("valid_imports") or source.get("import_count"))
    return summary


def _research_step_to_event(step: dict) -> dict:
    return {
        "ts": step.get("ts") or step.get("created_at"),
        "stage": step.get("stage"),
        "event": "research_step",
        "message": step.get("next") or step.get("decision") or step.get("summary"),
        "summary": step.get("summary"),
        "decision": step.get("decision"),
        "next": step.get("next"),
        "priority": step.get("priority"),
        "refs": step.get("refs") or [],
        "research_step": True,
    }


def _research_step_status(latest: dict, steps: list[dict]) -> str:
    stage = str(latest.get("stage") or "").lower()
    priority = str(latest.get("priority") or "").lower()
    extra = latest.get("extra") if isinstance(latest.get("extra"), dict) else {}
    explicit = extra.get("run_status") or extra.get("status")
    if explicit:
        return str(explicit)
    if stage == "blocker" or priority == "blocker":
        return "research_blocked"
    if stage == "checkpoint_stop":
        return "research_completed"
    if stage == "four_step_summary":
        state = extra.get("research_state") if isinstance(extra.get("research_state"), dict) else {}
        target = state.get("target_valid_imports") or state.get("target_imports")
        valid = state.get("valid_imports")
        try:
            if target is not None and valid is not None and int(valid) >= int(target):
                return "research_completed"
        except Exception:
            pass
    return "research_active"


def _research_steps_as_jobs(research_steps: list[dict], *, limit: int = 20) -> list[dict]:
    if not research_steps:
        return []
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for step in research_steps:
        run_id = _research_step_run_id(step)
        if run_id not in grouped:
            grouped[run_id] = []
            order.append(run_id)
        grouped[run_id].append(step)
    jobs: list[dict] = []
    for run_id in order[:limit]:
        steps = grouped[run_id]
        latest = steps[0]
        events = [_research_step_to_event(step) for step in reversed(steps)]
        extra = latest.get("extra") if isinstance(latest.get("extra"), dict) else {}
        jobs.append(
            {
                "run_id": run_id,
                "status": _research_step_status(latest, steps),
                "stage": latest.get("stage") or "research_step",
                "started_at": (steps[-1].get("ts") or steps[-1].get("created_at")),
                "finished_at": None,
                "inputs": {
                    "source": "research_steps",
                    "automation_id": extra.get("automation_id"),
                    "target_horizon": extra.get("target_horizon") or latest.get("target_horizon"),
                    "orchestration_mode": "codex_native_mcp",
                },
                "summary": _research_step_summary_counts(steps),
                "latest_event": _research_step_to_event(latest),
                "event_count": len(steps),
                "guidance_history": [],
                "events": events[-_GUI_EVENT_LIMIT:],
                "latest_result": None,
                "source": "research_steps",
            }
        )
    return jobs


def _factor_readiness(qgpt_url: str, *, skip_quantgpt_probe: bool = False, allow_quantgpt_restart: bool = False) -> dict:
    instrument_file = QLIB_DATA_ROOT / "instruments" / "all.txt"
    quantgpt_api_status = (
        {
            "reachable": True,
            "url": qgpt_url,
            "skipped": True,
            "reason": "called_from_quantgpt_mcp_to_avoid_recursive_health_probe",
            "self_heal": _quantgpt_self_heal_snapshot(),
        }
        if skip_quantgpt_probe
        else _ensure_quantgpt_api_reachable(qgpt_url, allow_restart=allow_quantgpt_restart)
    )
    if "self_heal" not in quantgpt_api_status:
        quantgpt_api_status["self_heal"] = _quantgpt_self_heal_snapshot()
    quantgpt_api_status["service_recovery"] = {
        "allow_restart": bool(allow_quantgpt_restart and not skip_quantgpt_probe),
        "mode": "explicit_startup_or_recovery" if allow_quantgpt_restart and not skip_quantgpt_probe else "read_only_probe",
    }
    readiness = {
        "quantgpt_code_root": {
            "path": str(QUANTGPT_CODE_ROOT),
            "exists": QUANTGPT_CODE_ROOT.exists(),
        },
        "quantgpt_stock_data": {
            "path": str(QUANTGPT_DATA_DIR),
            "exists": QUANTGPT_DATA_DIR.exists(),
            "parquet_count": len(list(QUANTGPT_DATA_DIR.glob("*.parquet"))) if QUANTGPT_DATA_DIR.exists() else 0,
        },
        "quantgpt_research_notes": {
            "path": str(QUANTGPT_RESEARCH_NOTES_DIR),
            "exists": QUANTGPT_RESEARCH_NOTES_DIR.exists(),
            "markdown_count": len(list(QUANTGPT_RESEARCH_NOTES_DIR.rglob("*.md"))) if QUANTGPT_RESEARCH_NOTES_DIR.exists() else 0,
        },
        "qlib_instruments": {
            "path": str(instrument_file),
            "exists": instrument_file.exists(),
        },
        "factor_data_root": {
            "path": str(FACTOR_DATA_ROOT),
            "exists": FACTOR_DATA_ROOT.exists(),
        },
        "factor_registry_db": {
            "path": str(FACTOR_REGISTRY_DB),
            "exists": FACTOR_REGISTRY_DB.exists(),
        },
        "factor_parquet_dir": {
            "path": str(FACTOR_PARQUET_DIR),
            "exists": FACTOR_PARQUET_DIR.exists(),
        },
        "factor_adopted_values": {
            "path": str(FACTOR_ADOPTED_VALUES_FILE),
            "exists": FACTOR_ADOPTED_VALUES_FILE.exists(),
            "canonical": True,
        },
        "active_factor_values": factor_active_values_status(
            holding_period_days=FACTOR_DEFAULT_HOLDING_PERIOD
        ).to_dict().get("outputs", {}),
        "quantgpt_adopted_values": {
            "path": str(QUANTGPT_ADOPTED_VALUES_FILE),
            "exists": QUANTGPT_ADOPTED_VALUES_FILE.exists(),
            "shared_with_active_factor_values": True,
        },
        "quantgpt_api": quantgpt_api_status,
        "llm_runtime": _load_llm_runtime_hint(),
    }
    readiness["ready_for_research"] = (
        readiness["quantgpt_code_root"]["exists"]
        and readiness["quantgpt_stock_data"]["exists"]
        and readiness["qlib_instruments"]["exists"]
        and readiness["quantgpt_api"]["reachable"]
    )
    return readiness


def factor_research_run(
    *,
    direction: str = "auto",
    universe: str = FACTOR_DEFAULT_UNIVERSE,
    n_candidates: int = FACTOR_DEFAULT_N_CANDIDATES,
    n_rounds: int = FACTOR_DEFAULT_N_ROUNDS,
    target_adopted: int = FACTOR_DEFAULT_TARGET_ADOPTED,
    qgpt_url: str = QUANTGPT_API_URL,
    mcp_url: str | None = None,
    max_agent_steps: int = 40,
    start_date: str = FACTOR_DEFAULT_START_DATE,
    end_date: str | None = FACTOR_DEFAULT_END_DATE,
    holding_period: int = FACTOR_DEFAULT_HOLDING_PERIOD,
    benchmark: str = FACTOR_DEFAULT_BENCHMARK,
    n_groups: int = 5,
    top_frac: float = FACTOR_DEFAULT_TOP_FRAC,
    cost_rate: float = FACTOR_DEFAULT_COST_RATE,
    rebalance_anchor: str | None = FACTOR_DEFAULT_REBALANCE_ANCHOR,
    neutralize_industry: bool = False,
    neutralize_cap: bool = True,
    universe_date: str | None = FACTOR_DEFAULT_UNIVERSE_DATE,
    seed_count: int = FACTOR_DEFAULT_SEED_COUNT,
    seed_max_concurrent: int = FACTOR_DEFAULT_SEED_MAX_CONCURRENT,
    max_direction_attempts: int = FACTOR_DEFAULT_MAX_DIRECTION_ATTEMPTS,
    max_stagnation_rounds: int = FACTOR_DEFAULT_MAX_STAGNATION_ROUNDS,
    poll_timeout_s: int = 900,
    min_abs_ic: float = 0.02,
    min_ir: float = 0.3,
    auto_sessions: int = 1,
    seed_batch_rounds: int = 0,
    seed_batch_max_candidates: int = 0,
    dry_run: bool = False,
    submit_wq: bool = False,
    progress_callback=None,
    guidance_callback=None,
) -> ServiceResult:
    max_agent_steps = min(300, max(4, int(max_agent_steps or 40)))
    readiness = _factor_readiness(qgpt_url, allow_quantgpt_restart=True)
    inputs = {
        "direction": direction,
        "universe": universe,
        "n_candidates": n_candidates,
        "n_rounds": n_rounds,
        "target_adopted": target_adopted,
        "qgpt_url": qgpt_url,
        "mcp_url": mcp_url,
        "max_agent_steps": max_agent_steps,
        "start_date": start_date,
        "end_date": end_date,
        "holding_period": holding_period,
        "benchmark": benchmark,
        "n_groups": n_groups,
        "top_frac": top_frac,
        "cost_rate": cost_rate,
        "rebalance_anchor": rebalance_anchor,
        "neutralize_industry": neutralize_industry,
        "neutralize_cap": neutralize_cap,
        "universe_date": universe_date,
        "seed_count": seed_count,
        "seed_max_concurrent": seed_max_concurrent,
        "max_direction_attempts": max_direction_attempts,
        "max_stagnation_rounds": max_stagnation_rounds,
        "poll_timeout_s": poll_timeout_s,
        "min_abs_ic": min_abs_ic,
        "min_ir": min_ir,
        "auto_sessions": auto_sessions,
        "seed_batch_rounds": seed_batch_rounds,
        "seed_batch_max_candidates": seed_batch_max_candidates,
        "dry_run": dry_run,
        "submit_wq": submit_wq,
    }

    if dry_run:
        return ok_result(
            inputs=inputs,
            outputs={"status": "dry_run", "readiness": readiness},
            artifacts={
                "research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE),
                "factor_registry_db": str(FACTOR_REGISTRY_DB),
                "factor_parquet_dir": str(FACTOR_PARQUET_DIR),
            },
            warnings=_research_input_warnings(seed_batch_rounds, readiness),
        )

    if not readiness["ready_for_research"]:
        return err_result(
            "factor_research is not ready",
            inputs=inputs,
            outputs={"status": "blocked", "readiness": readiness},
            artifacts={
                "research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE),
                "factor_registry_db": str(FACTOR_REGISTRY_DB),
                "factor_parquet_dir": str(FACTOR_PARQUET_DIR),
            },
        )

    return err_result(
        "factor_research_start_required",
        inputs=inputs,
        outputs={
            "status": "blocked",
            "reason": "use_governed_factor_research_start",
            "message": (
                "Unattended runner fallback has been removed. "
                "Use /factor/research/start or the GUI. The default production mode is Orchestrator; "
                "select codex_mcp explicitly for manual MCP debugging or evidence review."
            ),
            "required_runtime": "orchestrator_default_or_explicit_codex_mcp_debug",
        },
        artifacts={
            "research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE),
            "factor_registry_db": str(FACTOR_REGISTRY_DB),
            "factor_parquet_dir": str(FACTOR_PARQUET_DIR),
        },
    )

def _research_input_warnings(seed_batch_rounds: int, readiness: dict) -> list[str]:
    warnings: list[str] = []
    if not readiness["ready_for_research"]:
        warnings.append("quantgpt_api_unreachable")
    if seed_batch_rounds > 0:
        warnings.append("legacy_local_seed_batch_ignored_quantgpt_subsystem_only")
    return warnings


def _process_cmdline(pid: str | int) -> str:
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _process_systemd_unit(pid: str | int) -> str:
    try:
        cgroup = Path(f"/proc/{int(pid)}/cgroup").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return "fxalpha-api-18081.service" if "fxalpha-api-18081.service" in cgroup else ""


def _api_18081_owner_status() -> dict[str, Any]:
    owner = {
        "api_owner_pid": "",
        "api_owner_cmd": "",
        "api_owner_unit": "",
        "api_owner_conflict": False,
    }
    try:
        completed = subprocess.run(
            ["ss", "-ltnp", "sport = :18081"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except Exception as exc:
        owner["api_owner_error"] = str(exc)
        return owner
    match = re.search(r"pid=(\d+)", completed.stdout or "")
    if not match:
        return owner
    pid = match.group(1)
    cmd = _process_cmdline(pid)
    unit = _process_systemd_unit(pid)
    owner.update(
        {
            "api_owner_pid": pid,
            "api_owner_cmd": cmd,
            "api_owner_unit": unit,
            "api_owner_conflict": bool("scripts/start_fxalpha_api_18081.py" in cmd and unit not in {"fxalpha-api-18081.service"}),
        }
    )
    return owner


def _code_advice_alignment_summary(*, scan_lines: int = 300) -> dict[str, Any]:
    counts = {"follow": 0, "refine": 0, "disagree": 0, "missing_or_autofilled": 0}
    by_stage: dict[str, dict[str, int]] = {}
    if not FACTOR_ORCHESTRATOR_EVENTS_FILE.exists():
        return {"counts": counts, "by_stage": by_stage}
    lines = _tail_jsonl_lines(
        FACTOR_ORCHESTRATOR_EVENTS_FILE,
        max_lines=max(1, int(scan_lines or 300)),
    )
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        result = event.get("llm_result") if isinstance(event.get("llm_result"), dict) else {}
        warnings = result.get("_orchestrator_validation_warnings") if isinstance(result.get("_orchestrator_validation_warnings"), list) else []
        if any("code_advice_alignment" in str(item) for item in warnings):
            counts["missing_or_autofilled"] += 1
        alignment = result.get("code_advice_alignment") if isinstance(result.get("code_advice_alignment"), dict) else {}
        items = alignment.get("items") if isinstance(alignment.get("items"), list) else []
        stage = str(event.get("stage") or "unknown")
        stage_counts = by_stage.setdefault(stage, {"follow": 0, "refine": 0, "disagree": 0, "missing_or_autofilled": 0})
        if any("code_advice_alignment" in str(item) for item in warnings):
            stage_counts["missing_or_autofilled"] += 1
        for item in items:
            if not isinstance(item, dict):
                continue
            value = str(item.get("alignment") or "").strip().lower()
            if value in counts:
                counts[value] += 1
                stage_counts[value] += 1
    return {"counts": counts, "by_stage": by_stage}


def factor_seed_mine(
    *,
    target_new: int,
    universe: str = FACTOR_DEFAULT_UNIVERSE,
    start_date: str = FACTOR_DEFAULT_START_DATE,
    end_date: str | None = None,
    holding_period: int = FACTOR_DEFAULT_HOLDING_PERIOD,
    max_candidates: int = 0,
    dry_run: bool = False,
) -> ServiceResult:
    qgpt_url = QUANTGPT_API_URL
    readiness = _factor_readiness(qgpt_url, allow_quantgpt_restart=True)
    inputs = {
        "target_new": target_new,
        "universe": universe,
        "start_date": start_date,
        "end_date": end_date,
        "holding_period": holding_period,
        "max_candidates": max_candidates,
        "dry_run": dry_run,
        "mode": "legacy_local_batch_miner_debug",
    }
    return err_result(
        "legacy_disabled: use /factor/research/start for QuantGPT MCP factor research",
        inputs=inputs,
        outputs={
            "status": "legacy_disabled",
            "readiness": readiness,
            "replacement_endpoint": "/factor/research/start",
            "reason": "FXAlpha local seed mining has been physically archived; QuantGPT MCP owns production factor research.",
        },
        artifacts={
            "research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE),
            "factor_registry_db": str(FACTOR_REGISTRY_DB),
            "factor_parquet_dir": str(FACTOR_PARQUET_DIR),
        },
    )


def factor_research_status() -> ServiceResult:
    registry = FactorRegistry()
    summary = registry.summary()
    readiness = _factor_readiness(QUANTGPT_API_URL)
    api_owner = _api_18081_owner_status()
    runtime_defaults = factor_research_runtime_defaults()
    active_values_store = readiness.get("active_factor_values", {}) or {}
    research_steps = _read_recent_research_steps(limit=20)
    current_run_id = str((research_steps[0] if research_steps else {}).get("run_id") or "").strip()
    quantgpt_tasks = _quantgpt_tasks_for_research_run(
        _fetch_quantgpt_recent_tasks(limit=80, allow_restart=False),
        current_run_id,
    )
    quantgpt_summary = _reconcile_quantgpt_summary_with_readiness(_quantgpt_task_summary(quantgpt_tasks), readiness)
    stale_quantgpt_tasks = _quantgpt_stale_task_summary(_fetch_quantgpt_running_tasks())
    quantgpt_summary = _quantgpt_summary_for_research_state(quantgpt_summary, stale_quantgpt_tasks)
    code_advice_alignment = _code_advice_alignment_summary()
    candidate_records = _candidate_records_from_research_steps(research_steps, limit=40)
    runtime_view = _runtime_view_from_research_steps(
        research_steps,
        active_job=None,
        quantgpt_summary=quantgpt_summary,
        registry_summary=summary,
        candidate_records=candidate_records,
    )
    decision_view = _decision_view_from_research_steps(research_steps)
    status, pipeline = _authoritative_status_and_pipeline(runtime_view, decision_view, quantgpt_summary)
    # Status is a read model.  It may report an orphaned worker as blocked, but
    # only an explicit start/recovery action may append the terminal blocker.
    runtime_view = _runtime_view_with_authoritative_liveness(runtime_view, pipeline)
    return ok_result(
        outputs={
            "status": status,
            "registry_summary": summary,
            "active_values_store": active_values_store,
            "readiness": readiness,
            "api_service": api_owner,
            "pipeline": pipeline,
            "code_advice_alignment": code_advice_alignment,
            "runtime_defaults": runtime_defaults,
            "runtime_view": _jsonable(runtime_view),
            "decision_view": _jsonable(decision_view),
            "quantgpt_task_summary": _jsonable(quantgpt_summary),
            "stale_quantgpt_tasks": _jsonable(stale_quantgpt_tasks),
        },
        artifacts={
            "factor_registry_db": str(FACTOR_REGISTRY_DB),
            "factor_parquet_dir": str(FACTOR_PARQUET_DIR),
        },
    )


def _console_status_and_pipeline(
    status_outputs: dict,
    active_job: dict | None,
    quantgpt_summary: dict | None = None,
) -> tuple[str, dict]:
    return (
        status_outputs.get("status", "idle"),
        status_outputs.get("pipeline", {}) or {},
    )


def factor_research_console_state(*, limit: int = 80) -> ServiceResult:
    status = factor_research_status().to_dict()
    registry_summary = status.get("outputs", {}).get("registry_summary", {}) or {}
    orphaned_run_ids: list[str] = []
    recent_notes = _read_recent_research_notes()
    research_steps = _read_recent_research_steps(limit=20)
    current_run_id = str((research_steps[0] if research_steps else {}).get("run_id") or "").strip()
    recent_jobs = _research_steps_as_jobs(research_steps, limit=limit)
    active_job = recent_jobs[0] if recent_jobs else None
    factor_library = _extract_recent_library(limit=limit)
    quantgpt_tasks = _quantgpt_tasks_for_research_run(
        _fetch_quantgpt_recent_tasks(limit=120, allow_restart=False),
        current_run_id,
    )
    quantgpt_summary = _quantgpt_task_summary(quantgpt_tasks)
    stale_quantgpt_tasks = (status.get("outputs", {}) or {}).get("stale_quantgpt_tasks", {}) or {}
    quantgpt_summary = _quantgpt_summary_for_research_state(quantgpt_summary, stale_quantgpt_tasks)
    tool_timeline = _tool_timeline_from_tasks(quantgpt_tasks, limit=60)
    candidate_records = _candidate_records_from_research_steps(research_steps, limit=40)
    event_candidate_records = _candidate_records_from_orchestrator_events(limit=40)
    candidate_task_view = _merge_candidate_views(
        _quantgpt_task_candidates(quantgpt_tasks, factor_library=factor_library, limit=50),
        [*candidate_records, *event_candidate_records],
        limit=50,
    )
    quantgpt_candidates = candidate_task_view
    decision_view = _decision_view_from_research_steps(research_steps)
    thesis_cards = _extract_thesis_cards(
        research_steps=research_steps,
        candidates=quantgpt_candidates,
        limit=12,
    )
    runtime_view = _runtime_view_from_research_steps(
        research_steps,
        active_job=active_job,
        quantgpt_summary=quantgpt_summary,
        registry_summary=registry_summary,
        candidate_records=quantgpt_candidates,
    )
    live_digest = _build_live_research_digest(active_job, recent_jobs, {}, registry_summary)
    live_digest = _augment_digest_with_quantgpt_tasks(
        live_digest,
        quantgpt_candidates=quantgpt_candidates,
        quantgpt_summary=quantgpt_summary,
        recent_notes=recent_notes,
        factor_library=factor_library,
    )
    live_digest = _augment_digest_with_research_steps(live_digest, research_steps)
    live_digest["runtime_view"] = runtime_view
    live_digest["decision_view"] = decision_view
    live_digest["tool_timeline"] = tool_timeline[:40]
    live_digest["candidate_task_view"] = _jsonable(candidate_task_view[:50])
    live_digest["candidate_records"] = _jsonable(candidate_records[:40])
    live_digest["recent_candidates"] = candidate_task_view[:50] or _jsonable(candidate_records[:50])
    live_digest["thesis_cards"] = thesis_cards[:8]
    console_status, pipeline = _console_status_and_pipeline(
        status.get("outputs", {}) or {},
        active_job,
        quantgpt_summary,
    )
    run_view = _run_view_for_runtime(runtime_view, limit=min(120, max(40, int(limit or 80))))
    live_digest["run_view"] = run_view
    return ok_result(
        outputs={
            "status": console_status,
            "pipeline": pipeline,
            "readiness": status.get("outputs", {}).get("readiness", {}),
            "registry_summary": status.get("outputs", {}).get("registry_summary", {}),
            "active_values_store": status.get("outputs", {}).get("active_values_store", {}),
            "orphaned_run_ids": orphaned_run_ids,
            "recent_notes": recent_notes,
            "thesis_cards": _jsonable(thesis_cards),
            "research_steps": _jsonable(research_steps),
            "runtime_view": _jsonable(runtime_view),
            "run_view": _jsonable(run_view),
            "decision_view": _jsonable(decision_view),
            "tool_timeline": _jsonable(tool_timeline),
            "candidate_task_view": _jsonable(candidate_task_view[:50]),
            "candidate_records": _jsonable(candidate_records[:40]),
            "factor_library": factor_library,
            "quantgpt_task_summary": _jsonable(quantgpt_summary),
            "stale_quantgpt_tasks": _jsonable(stale_quantgpt_tasks),
            "quantgpt_recent_tasks": _jsonable(quantgpt_tasks[:20]),
            "latest_four_step_blocks": _jsonable(_latest_four_step_blocks(active_job, recent_jobs)),
            "live_research_digest": _jsonable(live_digest),
            "diagnostics": {
                "quantgpt_task_summary": _jsonable(quantgpt_summary),
                "candidate_records_from_research_steps": _jsonable(candidate_records[:40]),
                "raw_quantgpt_tasks": _jsonable(quantgpt_tasks[:40]),
            },
        },
        artifacts={
            "factor_registry_db": str(FACTOR_REGISTRY_DB),
            "research_notes_root": str(QUANTGPT_RESEARCH_NOTES_DIR),
        },
    )


def _live_console_run_id(active_controller_job: dict | None, latest_live_steps: list[dict]) -> str:
    active_run_id = str((active_controller_job or {}).get("run_id") or "").strip()
    if active_run_id:
        return active_run_id
    return str((latest_live_steps[0] if latest_live_steps else {}).get("run_id") or "").strip()


def factor_research_console_live(*, limit: int = 40) -> ServiceResult:
    """Lightweight cockpit payload for the GUI polling loop."""
    try:
        registry_summary = FactorRegistry().summary()
        active_values_store = factor_active_values_status(
            holding_period_days=FACTOR_DEFAULT_HOLDING_PERIOD
        ).to_dict().get("outputs", {})
    except Exception:
        registry_summary = {}
    quantgpt_api = {"reachable": False, "url": f"{QUANTGPT_API_URL.rstrip('/')}/api/v1/health"}
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(quantgpt_api["url"], timeout=1.2) as resp:
            quantgpt_api.update({"reachable": resp.status == 200, "status_code": resp.status})
            try:
                body = resp.read(2000).decode("utf-8", errors="ignore")
                payload = json.loads(body) if body else {}
                if isinstance(payload, dict):
                    quantgpt_api["health"] = _jsonable(payload)
                    if payload.get("active_tasks") is not None:
                        quantgpt_api["active_tasks"] = int(payload.get("active_tasks") or 0)
            except Exception:
                pass
    except Exception as exc:
        quantgpt_api["error"] = str(exc)[:180]
    readiness = {
        "quantgpt_code_root": {"path": str(QUANTGPT_CODE_ROOT), "exists": QUANTGPT_CODE_ROOT.exists()},
        "factor_registry_db": {"path": str(FACTOR_REGISTRY_DB), "exists": FACTOR_REGISTRY_DB.exists()},
        "quantgpt_api": quantgpt_api,
        "ready_for_research": bool(QUANTGPT_CODE_ROOT.exists() and FACTOR_REGISTRY_DB.exists() and quantgpt_api.get("reachable")),
    }
    status_outputs = {
        "registry_summary": registry_summary,
        "readiness": readiness,
    }
    recent_notes = [_compact_note_for_live(item, preview_limit=260) for item in _read_recent_research_notes(limit=4)]
    # The detached worker/control plane becomes authoritative immediately after
    # Start, before the first normal research step exists.  Prefer that run so
    # the cockpit never falls back to the previous completed run during startup.
    with _GUI_RUNS_LOCK:
        active_controller_job = _active_orchestrator_job_unlocked()
        active_controller_job = _job_snapshot(active_controller_job) if active_controller_job else {}
    latest_live_steps = _read_current_research_steps(limit=1)
    active_controller_run_id = str(active_controller_job.get("run_id") or "")
    live_run_id = _live_console_run_id(active_controller_job, latest_live_steps)
    research_steps_full = _read_current_research_steps(
        run_id=live_run_id,
        limit=min(1200, max(120, int(limit or 40) * 20)),
    )
    step_jobs = _research_steps_as_jobs(research_steps_full, limit=limit)
    recent_jobs = list(step_jobs)
    if active_controller_job:
        recent_jobs = [
            active_controller_job,
            *[job for job in step_jobs if str(job.get("run_id") or "") != active_controller_run_id],
        ]
    active_job = active_controller_job or (recent_jobs[0] if recent_jobs else None)
    research_steps = [_compact_research_step_for_live(item) for item in research_steps_full[:12]]
    factor_library_full = _extract_recent_library(limit=40)
    factor_library = _compact_factor_library_for_live(factor_library_full, limit=24)
    quantgpt_tasks = _quantgpt_tasks_for_research_run(
        _fetch_quantgpt_recent_tasks_snapshot(limit=80),
        live_run_id,
    )
    referenced_task_ids = _research_step_task_ids(research_steps_full, limit=1000)
    referenced_quantgpt_tasks = _fetch_quantgpt_tasks_by_ids(referenced_task_ids, limit=1000)
    candidate_board_tasks = _merge_quantgpt_tasks_by_id(referenced_quantgpt_tasks, quantgpt_tasks)
    quantgpt_summary = _reconcile_quantgpt_summary_with_readiness(_quantgpt_task_summary(quantgpt_tasks), readiness)
    stale_quantgpt_tasks = _quantgpt_stale_task_summary(_fetch_quantgpt_running_tasks())
    quantgpt_summary = _quantgpt_summary_for_research_state(quantgpt_summary, stale_quantgpt_tasks)
    tool_timeline = _tool_timeline_from_tasks(quantgpt_tasks, limit=40)
    current_candidate_board = _current_candidate_board(research_steps_full, candidate_board_tasks, limit=500)
    candidate_records = _candidate_records_from_research_steps(research_steps_full, limit=40)
    event_candidate_records = _candidate_records_from_orchestrator_events(limit=40)
    candidate_task_view = _merge_candidate_views(
        _quantgpt_task_candidates(quantgpt_tasks, factor_library=factor_library_full, limit=50),
        [*candidate_records, *event_candidate_records],
        limit=50,
    )
    quantgpt_candidates = candidate_task_view
    decision_view = _decision_view_from_research_steps(research_steps_full)
    thesis_cards = _extract_thesis_cards(
        research_steps=research_steps_full,
        candidates=quantgpt_candidates,
        limit=10,
    )
    runtime_view = _runtime_view_from_research_steps(
        research_steps_full,
        active_job=active_job,
        quantgpt_summary=quantgpt_summary,
        registry_summary=registry_summary,
        candidate_records=quantgpt_candidates,
    )
    console_status, pipeline = _authoritative_status_and_pipeline(runtime_view, decision_view, quantgpt_summary)
    runtime_view = _runtime_view_with_authoritative_liveness(runtime_view, pipeline)
    status_outputs["status"] = console_status
    status_outputs["pipeline"] = pipeline
    live_digest = _build_live_research_digest(active_job, recent_jobs, {}, registry_summary)
    live_digest = _augment_digest_with_quantgpt_tasks(
        live_digest,
        quantgpt_candidates=quantgpt_candidates,
        quantgpt_summary=quantgpt_summary,
        recent_notes=recent_notes,
        factor_library=factor_library,
    )
    live_digest = _augment_digest_with_research_steps(live_digest, research_steps)
    live_digest["runtime_view"] = runtime_view
    live_digest["decision_view"] = decision_view
    live_digest["tool_timeline"] = tool_timeline
    live_digest["current_candidate_board"] = _jsonable(current_candidate_board)
    live_digest["candidate_task_view"] = _jsonable(candidate_task_view[:50])
    live_digest["candidate_records"] = _jsonable(candidate_records[:40])
    live_digest["recent_candidates"] = candidate_task_view[:50] or _jsonable(candidate_records[:50])
    live_digest["thesis_cards"] = thesis_cards[:8]
    # The candidate board is built above from every step in the current run.
    # This run view only feeds the live progress/tool widgets, so a bounded tail
    # avoids duplicating the full journal in every GUI polling response.
    run_view = _run_view_for_runtime(runtime_view, limit=40)
    live_digest["run_view"] = run_view
    return ok_result(
        outputs={
            "status": console_status,
            "pipeline": pipeline,
            "readiness": readiness,
            "registry_summary": registry_summary,
            "active_values_store": active_values_store,
            "recent_notes": recent_notes,
            "thesis_cards": _jsonable(thesis_cards),
            "research_steps": _jsonable(research_steps),
            "runtime_view": _jsonable(runtime_view),
            "run_view": _jsonable(run_view),
            "decision_view": _jsonable(decision_view),
            "tool_timeline": _jsonable(tool_timeline),
            "current_candidate_board": _jsonable(current_candidate_board),
            "candidate_task_view": _jsonable(candidate_task_view[:50]),
            "candidate_records": _jsonable(candidate_records[:40]),
            "factor_library": factor_library,
            "quantgpt_task_summary": _jsonable(quantgpt_summary),
            "stale_quantgpt_tasks": _jsonable(stale_quantgpt_tasks),
            "quantgpt_recent_tasks": _jsonable([_compact_task_for_live(task) for task in quantgpt_tasks[:12]]),
            "recent_import_diagnostics": _recent_import_diagnostics(recent_jobs, limit=5),
            "latest_four_step_blocks": _jsonable(_latest_four_step_blocks(active_job, recent_jobs[:1])),
            "live_research_digest": _jsonable(_compact_live_digest_for_console(live_digest)),
            "diagnostics": {
                "quantgpt_task_summary": _jsonable(quantgpt_summary),
                "current_candidate_board_ok": current_candidate_board.get("ok"),
                "current_candidate_board_error_count": len(current_candidate_board.get("errors") or []),
                "candidate_record_count": len(candidate_records or []),
                "candidate_task_view_count": len(candidate_task_view or []),
                "quantgpt_task_count": len(quantgpt_tasks or []),
                "referenced_task_id_count": len(referenced_task_ids),
                "referenced_task_db_hit_count": len(referenced_quantgpt_tasks),
                "candidate_board_task_count": len(candidate_board_tasks),
                "quantgpt_recent_task_sample": _jsonable([_compact_task_for_live(task) for task in quantgpt_tasks[:5]]),
            },
        },
        artifacts={
            "factor_registry_db": str(FACTOR_REGISTRY_DB),
            "research_notes_root": str(QUANTGPT_RESEARCH_NOTES_DIR),
        },
    )


def factor_research_console_full() -> ServiceResult:
    status = factor_research_status().to_dict()
    registry_summary = status.get("outputs", {}).get("registry_summary", {}) or {}
    recent_notes = _read_recent_research_notes(limit=20)
    research_steps = _read_recent_research_steps(limit=60)
    recent_jobs = _research_steps_as_jobs(research_steps, limit=50)
    active_job = recent_jobs[0] if recent_jobs else None
    factor_library = _extract_recent_library(limit=200)
    quantgpt_tasks = _fetch_quantgpt_recent_tasks(limit=160, allow_restart=False)
    quantgpt_summary = _quantgpt_task_summary(quantgpt_tasks)
    stale_quantgpt_tasks = (status.get("outputs", {}) or {}).get("stale_quantgpt_tasks", {}) or {}
    quantgpt_summary = _quantgpt_summary_for_research_state(quantgpt_summary, stale_quantgpt_tasks)
    tool_timeline = _tool_timeline_from_tasks(quantgpt_tasks, limit=100)
    candidate_records = _candidate_records_from_research_steps(research_steps, limit=100)
    event_candidate_records = _candidate_records_from_orchestrator_events(limit=100)
    candidate_task_view = _merge_candidate_views(
        _quantgpt_task_candidates(quantgpt_tasks, factor_library=factor_library, limit=50),
        [*candidate_records, *event_candidate_records],
        limit=50,
    )
    quantgpt_candidates = candidate_task_view
    decision_view = _decision_view_from_research_steps(research_steps)
    thesis_cards = _extract_thesis_cards(
        research_steps=research_steps,
        candidates=quantgpt_candidates,
        limit=30,
    )
    runtime_view = _runtime_view_from_research_steps(
        research_steps,
        active_job=active_job,
        quantgpt_summary=quantgpt_summary,
        registry_summary=registry_summary,
        candidate_records=quantgpt_candidates,
    )
    live_digest = _build_live_research_digest(active_job, recent_jobs, {}, registry_summary)
    live_digest = _augment_digest_with_quantgpt_tasks(
        live_digest,
        quantgpt_candidates=quantgpt_candidates,
        quantgpt_summary=quantgpt_summary,
        recent_notes=recent_notes,
        factor_library=factor_library,
    )
    live_digest = _augment_digest_with_research_steps(live_digest, research_steps)
    live_digest["runtime_view"] = runtime_view
    live_digest["decision_view"] = decision_view
    live_digest["tool_timeline"] = tool_timeline
    live_digest["candidate_task_view"] = _jsonable(candidate_task_view[:50])
    live_digest["candidate_records"] = _jsonable(candidate_records[:100])
    live_digest["recent_candidates"] = candidate_task_view[:50] or _jsonable(candidate_records[:50])
    live_digest["thesis_cards"] = thesis_cards[:12]
    console_status, pipeline = _console_status_and_pipeline(
        status.get("outputs", {}) or {},
        active_job,
        quantgpt_summary,
    )
    run_view = _run_view_for_runtime(runtime_view, limit=120)
    live_digest["run_view"] = run_view
    return ok_result(
        outputs={
            "status": console_status,
            "pipeline": pipeline,
            "readiness": status.get("outputs", {}).get("readiness", {}),
            "registry_summary": registry_summary,
            "recent_notes": _jsonable(recent_notes),
            "thesis_cards": _jsonable(thesis_cards),
            "research_steps": _jsonable(research_steps),
            "runtime_view": _jsonable(runtime_view),
            "run_view": _jsonable(run_view),
            "decision_view": _jsonable(decision_view),
            "tool_timeline": _jsonable(tool_timeline),
            "candidate_task_view": _jsonable(candidate_task_view[:50]),
            "candidate_records": _jsonable(candidate_records[:100]),
            "factor_library": _jsonable(factor_library),
            "quantgpt_task_summary": _jsonable(quantgpt_summary),
            "stale_quantgpt_tasks": _jsonable(stale_quantgpt_tasks),
            "quantgpt_recent_tasks": _jsonable(quantgpt_tasks[:30]),
            "recent_import_diagnostics": _jsonable(_recent_import_diagnostics(recent_jobs)),
            "latest_four_step_blocks": _jsonable(_latest_four_step_blocks(active_job, recent_jobs)),
            "live_research_digest": _jsonable(live_digest),
            "diagnostics": {
                "quantgpt_task_summary": _jsonable(quantgpt_summary),
                "candidate_records_from_research_steps": _jsonable(candidate_records[:100]),
                "raw_quantgpt_tasks": _jsonable(quantgpt_tasks[:100]),
            },
        },
        artifacts={
            "factor_registry_db": str(FACTOR_REGISTRY_DB),
            "research_notes_root": str(QUANTGPT_RESEARCH_NOTES_DIR),
        },
    )


def _orchestrator_stale_preview(*, stale_seconds: int = 180) -> dict[str, Any]:
    latest = _latest_orchestrator_interruption_candidate()
    if not latest or _orchestrator_event_is_terminal(latest):
        return {"stale": False, "latest_event": _compact_orchestrator_event(latest) if latest else {}}
    latest_ts = _parse_iso_ts(str(latest.get("ts") or ""))
    age_s = int((datetime.now() - latest_ts).total_seconds()) if latest_ts else None
    boot_interrupted = latest_ts is not None and latest_ts < FACTOR_API_BOOT_TS
    stale_interrupted = age_s is not None and age_s >= stale_seconds
    run_id = str(latest.get("run_id") or "")
    active_in_process = _orchestrator_thread_alive(run_id)
    return {
        "stale": bool((boot_interrupted or stale_interrupted) and not active_in_process),
        "reason": (
            ""
            if active_in_process
            else "api_boot_mismatch"
            if boot_interrupted
            else "stale_heartbeat"
            if stale_interrupted
            else ""
        ),
        "age_s": age_s,
        "latest_event": _compact_orchestrator_event(latest),
        "active_in_current_process": active_in_process,
    }


def factor_research_preflight(qgpt_url: str | None = None) -> ServiceResult:
    runtime_defaults = factor_research_runtime_defaults()
    effective_qgpt_url = str(qgpt_url or runtime_defaults.get("qgpt_url") or QUANTGPT_API_URL)
    readiness = _factor_readiness(effective_qgpt_url, allow_quantgpt_restart=False)
    active_job_snapshot: dict[str, Any] = {}
    with _GUI_RUNS_LOCK:
        active_job = _active_orchestrator_job_unlocked()
        if active_job:
            active_job_snapshot = _job_snapshot(active_job)
    stale_preview = _orchestrator_stale_preview()
    qgpt_ok = bool((readiness.get("quantgpt_api") or {}).get("reachable"))
    blocking_errors: list[str] = []
    if not qgpt_ok:
        blocking_errors.append("quantgpt_api_unreachable")
    can_start = qgpt_ok and not bool(active_job_snapshot)
    return ok_result(
        inputs={"qgpt_url": effective_qgpt_url},
        outputs={
            "api_ok": True,
            "qgpt_ok": qgpt_ok,
            "qgpt_url": effective_qgpt_url,
            "can_start": can_start,
            "blocking_errors": blocking_errors,
            "active_orchestrator_run": active_job_snapshot,
            "has_active_orchestrator_run": bool(active_job_snapshot),
            "stale_or_interrupted": stale_preview,
            "runtime_defaults": runtime_defaults,
            "doctor_hint": (
                "QuantGPT API is unreachable; use the existing Windows/WSL doctor or recovery launcher before starting."
                if not qgpt_ok
                else ""
            ),
            "readiness": readiness,
            "config": {
                "config_file": str(CONFIG_FILE),
                "default_orchestration_mode": runtime_defaults.get("default_orchestration_mode"),
                "config_save_policy": "explicit_save_only",
            },
        },
        artifacts={
            "config_file": str(CONFIG_FILE),
            "orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE),
            "orchestrator_llm_trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
        },
    )


def _request_factor_research_control(
    *,
    action: str,
    run_id: str | None = None,
    reason: str = "",
) -> ServiceResult:
    action_text = str(action or "").strip().lower()
    if action_text not in {"pause", "stop"}:
        return err_result("invalid_factor_research_control_action", inputs={"action": action_text, "run_id": run_id})
    target_job: dict | None = None
    with _GUI_RUNS_LOCK:
        if run_id:
            job = _GUI_RUNS.get(str(run_id))
            if job and str((job.get("inputs") or {}).get("orchestration_mode") or "").strip().lower() == "orchestrator":
                target_job = job
        if target_job is None:
            target_job = _active_orchestrator_job_unlocked()
        if not target_job:
            paused_event = _latest_operator_pause_event(str(run_id or "")) if run_id else {}
            if action_text == "stop" and paused_event:
                target_run_id = str(paused_event.get("run_id") or run_id or "")
                control_event = _write_orchestrator_control_request(
                    run_id=target_run_id,
                    action="stop",
                    reason=reason or "operator_stopped_paused_run",
                )
                completion = _write_orchestrator_event(
                    {
                        "run_id": target_run_id,
                        "round_id": f"{target_run_id}:stop",
                        "stage_seq": 99,
                        "stage_id": f"{target_run_id}:stop:s99_operator_stop_{uuid.uuid4().hex[:8]}",
                        "stage": "checkpoint_stop",
                        "previous_stage": paused_event.get("stage"),
                        "previous_stage_id": paused_event.get("stage_id"),
                        "summary": "Paused Orchestrator run was formally ended by the operator.",
                        "decision": "operator_stop_completed",
                        "stage_transition": {"next_stage": "checkpoint_stop", "next_action": "idle", "mode": "orchestrator"},
                        "event_type": "checkpoint",
                        "tags": ["checkpoint_stop", "operator_stop"],
                        "control_action": "stop",
                        "control_request_id": control_event.get("control_request_id"),
                        "heartbeat_status": "stopped",
                    }
                )
                return ok_result(
                    inputs={"action": action_text, "run_id": run_id, "reason": reason},
                    outputs={
                        "accepted": True,
                        "status": "completed",
                        "actual_state": "completed",
                        "requested_state": "completed",
                        "run_id": target_run_id,
                        "control_request_id": control_event.get("control_request_id"),
                        "event": _compact_orchestrator_event(completion),
                    },
                    artifacts={"orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE)},
                )
            return ok_result(
                inputs={"action": action_text, "run_id": run_id, "reason": reason},
                outputs={
                    "accepted": False,
                    "status": "idle",
                    "actual_state": "idle",
                    "requested_state": "paused" if action_text == "pause" else "completed",
                    "message": "no_active_orchestrator_run",
                },
                artifacts={"orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE)},
            )
        target_run_id = str(target_job.get("run_id") or "")
        control_event = _write_orchestrator_control_request(
            run_id=target_run_id,
            action=action_text,
            reason=reason or f"operator_requested_{action_text}",
        )
        request_id = str(control_event.get("control_request_id") or "")
        target_job["status"] = f"{action_text}_requested"
        target_job["stage"] = f"operator_{action_text}_requested"
        target_job["stop_requested"] = True
        target_job["control_action"] = action_text
        target_job["control_request_id"] = request_id
        target_job["stop_reason"] = reason or f"operator_requested_{action_text}"
        _append_job_event(
            target_job,
            {
                "event": f"operator_{action_text}_requested",
                "reason": target_job["stop_reason"],
                "message": f"Operator requested Orchestrator {action_text}; worker will exit at the next safe checkpoint.",
                "control_request_id": request_id,
            },
        )
    return ok_result(
        inputs={"action": action_text, "run_id": run_id, "reason": reason},
        outputs={
            "accepted": True,
            "status": f"{action_text}_requested",
            "actual_state": f"{action_text}_requested",
            "requested_state": "paused" if action_text == "pause" else "completed",
            "run_id": target_run_id,
            "control_request_id": request_id,
            "event": _compact_orchestrator_event(control_event),
        },
        artifacts={"orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE)},
    )


def factor_research_pause(run_id: str | None = None, reason: str = "operator_requested_pause") -> ServiceResult:
    return _request_factor_research_control(action="pause", run_id=run_id, reason=reason)


def factor_research_stop(run_id: str | None = None, reason: str = "operator_requested_stop") -> ServiceResult:
    return _request_factor_research_control(action="stop", run_id=run_id, reason=reason)


def factor_research_resume(run_id: str) -> ServiceResult:
    """Resume a paused ORCH run from its durable launch spec and checkpoint."""
    run_text = str(run_id or "").strip()
    if not run_text:
        return err_result("resume_run_id_required", inputs={"run_id": run_id})
    launch = _latest_orchestrator_launch_spec(run_text)
    launch_inputs = launch.get("inputs") if isinstance(launch, dict) else None
    if not isinstance(launch_inputs, dict) or not launch_inputs:
        return err_result(
            "resume_launch_spec_not_found",
            inputs={"run_id": run_text},
            outputs={"status": "blocked", "run_id": run_text},
            artifacts={"orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE)},
        )
    inputs = dict(launch_inputs)
    inputs.pop("resume_run_id", None)
    if not inputs.get("evaluation_mode"):
        legacy_window = (str(inputs.get("start_date") or ""), str(inputs.get("end_date") or ""))
        matching_modes = []
        for candidate_mode in ("research", "production"):
            candidate = resolve_evaluation_profile(candidate_mode)
            factor_window = candidate["factor"]
            if legacy_window == (
                factor_window["selection_start_date"],
                factor_window["selection_end_date"],
            ):
                matching_modes.append(candidate_mode)
        if not any(legacy_window):
            # Pre-contract ORCH launches did not persist dates. They were all
            # created by the formerly production-only factor entrypoint.
            matching_modes = ["production"]
        if len(matching_modes) != 1:
            return err_result(
                "legacy_resume_evaluation_mode_ambiguous",
                inputs={"run_id": run_text, "legacy_window": legacy_window},
                outputs={
                    "status": "blocked",
                    "message": "Legacy run has no immutable evaluation profile and its window does not identify one profile uniquely.",
                },
            )
        inputs["evaluation_mode"] = matching_modes[0]
    for derived_key in (
        "evaluation_profile_version",
        "profile_version",
        "evaluation_contract_hash",
        "evidence_class",
        "value_start_date",
        "value_end_date",
    ):
        inputs.pop(derived_key, None)
    inputs["orchestration_mode"] = "orchestrator"
    return factor_research_start(resume_run_id=run_text, **inputs)


def factor_research_control_state(*, include_services: bool = True) -> ServiceResult:
    """Small authoritative read model for start/pause/resume/stop controls."""
    latest_steps = _read_current_research_steps(limit=1)
    latest_step = latest_steps[0] if latest_steps else {}
    run_id = str(latest_step.get("run_id") or "").strip()
    active_snapshot: dict[str, Any] = {}
    if include_services:
        with _GUI_RUNS_LOCK:
            active = _active_orchestrator_job_unlocked()
            if active:
                active_snapshot = _job_snapshot(active)
                run_id = str(active_snapshot.get("run_id") or run_id)
    if active_snapshot and str(latest_step.get("run_id") or "") != run_id:
        latest_step = {}

    control = _latest_orchestrator_control_request(run_id) if run_id else {}
    control_action = str(control.get("control_action") or "").strip().lower()
    job_status = str(active_snapshot.get("status") or "").strip().lower()
    latest_stage = str(latest_step.get("stage") or "").strip().lower()
    tags = {str(tag).strip().lower() for tag in latest_step.get("tags") or []}

    if latest_stage == "checkpoint_stop" and "operator_pause" in tags and control_action != "resume":
        state = "paused"
    elif latest_stage == "blocker":
        state = "blocked"
    elif latest_stage == "checkpoint_stop":
        state = "completed"
    elif job_status in {"pause_requested", "stop_requested"}:
        state = job_status
    elif active_snapshot and control_action in {"pause", "stop"}:
        state = f"{control_action}_requested"
    elif active_snapshot and _job_is_active(active_snapshot):
        state = "running"
    elif run_id and control_action in {"pause", "stop"}:
        state = f"{control_action}_requested"
    elif run_id and control_action == "resume":
        state = "resume_requested"
    else:
        state = "idle"

    allowed_actions: list[str]
    if state == "running":
        allowed_actions = ["pause", "stop", "guidance"]
    elif state == "paused":
        allowed_actions = ["resume", "stop", "guidance"]
    elif state in {"pause_requested", "resume_requested"}:
        allowed_actions = ["guidance"] if run_id else []
    elif state == "stop_requested":
        allowed_actions = []
    elif state == "blocked":
        if include_services:
            recoverable = bool(
                _latest_orchestrator_interruption_blocker(run_id)
                or _latest_orchestrator_recoverable_llm_blocker(run_id)
                or _latest_operator_pause_event(run_id)
            )
            allowed_actions = (["resume", "stop", "guidance"] if recoverable else ["start"])
        else:
            # The overview only renders state/identity.  Recoverability requires
            # scanning historical journals and is evaluated on the research page.
            allowed_actions = []
    else:
        allowed_actions = ["start"]

    quantgpt_health = (
        _ensure_quantgpt_api_reachable(QUANTGPT_API_URL, allow_restart=False)
        if include_services
        else {"skipped": True, "reason": "compact_overview_read"}
    )
    return ok_result(
        outputs={
            "state": state,
            "run_id": run_id or None,
            "round_id": latest_step.get("round_id"),
            "stage": latest_step.get("stage"),
            "stage_id": latest_step.get("stage_id"),
            "allowed_actions": allowed_actions,
            "control_request": _compact_orchestrator_event(control) if control else {},
            "active_job": active_snapshot,
            "services": {
                "api": {"reachable": True},
                "quantgpt": quantgpt_health,
            },
        },
        artifacts={
            "research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE),
            "orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE),
        },
    )


def factor_research_start(
    *,
    direction: str = "auto",
    universe: str = FACTOR_DEFAULT_UNIVERSE,
    n_candidates: int = FACTOR_DEFAULT_N_CANDIDATES,
    n_rounds: int = FACTOR_DEFAULT_N_ROUNDS,
    target_adopted: int = FACTOR_DEFAULT_TARGET_ADOPTED,
    qgpt_url: str = QUANTGPT_API_URL,
    mcp_url: str | None = None,
    max_agent_steps: int = 40,
    start_date: str = FACTOR_DEFAULT_START_DATE,
    end_date: str | None = FACTOR_DEFAULT_END_DATE,
    holding_period: int = FACTOR_DEFAULT_HOLDING_PERIOD,
    benchmark: str = FACTOR_DEFAULT_BENCHMARK,
    n_groups: int = 5,
    top_frac: float = FACTOR_DEFAULT_TOP_FRAC,
    cost_rate: float = FACTOR_DEFAULT_COST_RATE,
    rebalance_anchor: str | None = FACTOR_DEFAULT_REBALANCE_ANCHOR,
    neutralize_industry: bool = False,
    neutralize_cap: bool = True,
    universe_date: str | None = FACTOR_DEFAULT_UNIVERSE_DATE,
    seed_count: int = FACTOR_DEFAULT_SEED_COUNT,
    seed_max_concurrent: int = FACTOR_DEFAULT_SEED_MAX_CONCURRENT,
    max_direction_attempts: int = FACTOR_DEFAULT_MAX_DIRECTION_ATTEMPTS,
    max_stagnation_rounds: int = FACTOR_DEFAULT_MAX_STAGNATION_ROUNDS,
    poll_timeout_s: int = 900,
    min_abs_ic: float = 0.02,
    min_ir: float = 0.3,
    auto_sessions: int = 1,
    seed_batch_rounds: int = 0,
    seed_batch_max_candidates: int = 0,
    submit_wq: bool = False,
    orchestration_mode: str = FACTOR_RESEARCH_DEFAULT_ORCHESTRATION_MODE,
    llm_model: str | None = None,
    llm_timeout_s: int = FACTOR_ORCHESTRATOR_LLM_TIMEOUT_DEFAULT,
    resume_run_id: str | None = None,
    evaluation_mode: str | None = None,
    evaluation_profile_snapshot: dict[str, Any] | None = None,
) -> ServiceResult:
    max_agent_steps = min(300, max(4, int(max_agent_steps or 40)))
    orchestration_mode = (orchestration_mode or FACTOR_RESEARCH_DEFAULT_ORCHESTRATION_MODE).strip().lower()
    requested_resume_run_id = str(resume_run_id or "").strip()
    try:
        if requested_resume_run_id and isinstance(evaluation_profile_snapshot, dict):
            evaluation = dict(evaluation_profile_snapshot)
            factor_window = dict(evaluation.get("factor") or {})
            required_window_fields = {
                "selection_start_date",
                "selection_end_date",
                "value_start_date",
                "value_end_date",
            }
            if not required_window_fields.issubset(factor_window):
                raise EvaluationProfileError("resume_evaluation_profile_snapshot_incomplete")
            if evaluation_mode and evaluation.get("evaluation_mode") != str(evaluation_mode).strip().lower():
                raise EvaluationProfileError("resume_evaluation_mode_snapshot_mismatch")
        else:
            evaluation = resolve_evaluation_profile(evaluation_mode)
            factor_window = evaluation["factor"]
    except EvaluationProfileError as exc:
        return err_result(
            "invalid_evaluation_profile",
            inputs={"evaluation_mode": evaluation_mode, "resume_run_id": requested_resume_run_id or None},
            outputs={"status": "blocked", "detail": str(exc)},
        )
    evaluation_mode = str(evaluation["evaluation_mode"])
    try:
        selected_llm_model = _normalize_orchestrator_llm_model(
            llm_model or _default_orchestrator_llm_model()
        )
    except ValueError:
        return err_result(
            "invalid_orchestrator_llm_model",
            inputs={"llm_model": llm_model},
            outputs={
                "status": "blocked",
                "requested_model": str(llm_model or ""),
                "allowed_models": list(FACTOR_ORCHESTRATOR_LLM_MODELS),
                "message": "Choose one supported DeepSeek model for the new Orchestrator run.",
            },
        )
    start_date = factor_window["selection_start_date"]
    end_date = factor_window["selection_end_date"]
    value_start_date = factor_window["value_start_date"]
    value_end_date = factor_window["value_end_date"]
    run_id = requested_resume_run_id or f"fr_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    inputs = {
        "evaluation_mode": evaluation_mode,
        "profile_version": evaluation["profile_version"],
        "evaluation_profile_version": evaluation["profile_version"],
        "evaluation_contract_hash": evaluation["config_snapshot_hash"],
        "evidence_class": evaluation["evidence_class"],
        "evaluation_profile_snapshot": evaluation,
        "orchestration_mode": orchestration_mode,
        "llm_model": selected_llm_model,
        "direction": direction,
        "universe": universe,
        "n_candidates": n_candidates,
        "n_rounds": n_rounds,
        "target_adopted": target_adopted,
        "qgpt_url": qgpt_url,
        "mcp_url": mcp_url,
        "max_agent_steps": max_agent_steps,
        "start_date": start_date,
        "end_date": end_date,
        "value_start_date": value_start_date,
        "value_end_date": value_end_date,
        "holding_period": holding_period,
        "benchmark": benchmark,
        "n_groups": n_groups,
        "top_frac": top_frac,
        "cost_rate": cost_rate,
        "rebalance_anchor": rebalance_anchor,
        "neutralize_industry": neutralize_industry,
        "neutralize_cap": neutralize_cap,
        "universe_date": universe_date,
        "seed_count": seed_count,
        "seed_max_concurrent": seed_max_concurrent,
        "max_direction_attempts": max_direction_attempts,
        "max_stagnation_rounds": max_stagnation_rounds,
        "poll_timeout_s": poll_timeout_s,
        "min_abs_ic": min_abs_ic,
        "min_ir": min_ir,
        "auto_sessions": auto_sessions,
        "seed_batch_rounds": seed_batch_rounds,
        "seed_batch_max_candidates": seed_batch_max_candidates,
        "submit_wq": submit_wq,
        "llm_timeout_s": max(
            4,
            min(
                FACTOR_ORCHESTRATOR_LLM_TIMEOUT_MAX,
                int(llm_timeout_s or FACTOR_ORCHESTRATOR_LLM_TIMEOUT_DEFAULT),
            ),
        ),
        "resume_run_id": requested_resume_run_id or None,
    }
    allowed_modes = {"codex_mcp", "orchestrator"}
    if orchestration_mode not in allowed_modes:
        return err_result(
            "invalid_orchestration_mode",
            inputs=inputs,
            outputs={
                "status": "blocked",
                "requested_mode": orchestration_mode,
                "allowed_modes": sorted(allowed_modes),
                "message": "Use orchestrator for default production research or codex_mcp for explicit manual debugging. Legacy runner fallback has been removed.",
            },
            artifacts={"research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE)},
        )

    interrupted_event: dict[str, Any] = {}
    job = {
        "run_id": run_id,
        "status": "queued",
        "stage": "queued",
        "started_at": _now_iso(),
        "finished_at": None,
        "inputs": inputs,
        "events": deque(maxlen=_GUI_EVENT_LIMIT),
        "guidance_history": [],
        "latest_result": None,
        "summary": {},
        "latest_event": None,
    }
    with _GUI_RUNS_LOCK:
        _sweep_gui_jobs()
        status = factor_research_status().to_dict()
        existing_job = _active_orchestrator_job_unlocked()
        if orchestration_mode == "codex_mcp" and existing_job:
            snapshot = _job_snapshot(existing_job)
            return err_result(
                "orchestrator_run_already_active",
                inputs=inputs,
                outputs={
                    "status": "blocked",
                    "requested_mode": "codex_mcp",
                    "active_run_id": snapshot.get("run_id"),
                    "message": (
                        "Codex MCP debugging cannot start while a production "
                        "Orchestrator run is active. Stop or complete that run first."
                    ),
                    "active_orchestrator_run": snapshot,
                },
                artifacts={
                    "research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE),
                    "orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE),
                    "orchestrator_llm_trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
                },
            )
        _supersede_waiting_codex_jobs_unlocked(keep_run_id=None)
        if orchestration_mode == "orchestrator":
            if existing_job:
                snapshot = _job_snapshot(existing_job)
                return ok_result(
                    inputs=inputs,
                    outputs={
                        "status": "running",
                        "run_id": snapshot.get("run_id"),
                        "deduplicated": True,
                        "message": "An Orchestrator background run is already active; reusing the existing run.",
                        "job": snapshot,
                        "orchestrator": {
                            "event_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE),
                            "llm_trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
                            "llm_trace_schema": "orchestrator_llm_trace_v1",
                            "mode": "deepseek_background_event_plus_research_step_projection",
                            "thread": ((snapshot.get("summary") or {}).get("thread") if isinstance(snapshot.get("summary"), dict) else None),
                        },
                    },
                    artifacts={
                        "orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE),
                        "orchestrator_llm_trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
                        "research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE),
                    },
                    warnings=["orchestrator_run_already_active"],
                )
            if requested_resume_run_id:
                interrupted_event = _latest_orchestrator_interruption_blocker(requested_resume_run_id)
                if not interrupted_event:
                    interrupted_event = _mark_stale_orchestrator_run_interrupted(run_id=requested_resume_run_id)
                if not interrupted_event:
                    # A caller can explicitly resume a durable score/deep
                    # checkpoint after a retryable LLM JSON/schema failure.
                    # Do not generalize this to tool/data/gate blockers.
                    interrupted_event = _latest_orchestrator_recoverable_llm_blocker(requested_resume_run_id)
                if not interrupted_event:
                    interrupted_event = _latest_operator_pause_event(requested_resume_run_id)
                if not interrupted_event:
                    return err_result(
                        "resume_run_not_recoverable_or_not_found",
                        inputs=inputs,
                        outputs={"status": "blocked", "run_id": requested_resume_run_id},
                    )
            else:
                interrupted_event = _mark_stale_orchestrator_run_interrupted()
        _purge_orphaned_gui_jobs(
            status.get("outputs", {}).get("registry_summary", {}) or {},
            {},
        )
        _GUI_RUNS[run_id] = job
        _append_job_event(job, {"event": "job_queued"})

    if not requested_resume_run_id:
        _begin_factor_research_live_journals(
            run_id,
            include_orchestrator_journals=orchestration_mode == "orchestrator",
        )

    if orchestration_mode == "orchestrator":
        interrupted_handoff = _orchestrator_interrupted_handoff(interrupted_event)
        if interrupted_handoff and not requested_resume_run_id:
            # A fresh run records the previous interruption for observability,
            # but must never inherit its recovery round number or replay its
            # durable candidate checkpoint. Explicit resume_run_id is the only
            # authority to continue an interrupted run.
            interrupted_handoff = dict(interrupted_handoff)
            interrupted_handoff.pop("recovery_checkpoint", None)
            interrupted_handoff["to_stage"] = "thesis_design"
            interrupted_handoff["recommended_mutation"] = "start_fresh_run_after_recording_previous_interruption"
        research_contract = {
            "contract_source": "orchestrator",
            "evaluation_mode": evaluation_mode,
            "profile_version": evaluation["profile_version"],
            "evaluation_profile_version": evaluation["profile_version"],
            "evaluation_contract_hash": evaluation["config_snapshot_hash"],
            "evidence_class": evaluation["evidence_class"],
            "evaluation_profile_snapshot": evaluation,
            "direction": direction,
            "universe": universe,
            "selection_start_date": start_date,
            "selection_end_date": end_date,
            "value_start_date": value_start_date,
            "value_end_date": value_end_date,
            "benchmark": benchmark,
            "holding_period": holding_period,
            "n_groups": n_groups,
            "top_frac": top_frac,
            "cost_rate": cost_rate,
            "rebalance_anchor": rebalance_anchor,
            "neutralize_cap": neutralize_cap,
            "neutralize_industry": neutralize_industry,
            "target_adopted": target_adopted,
            "n_candidates": n_candidates,
            "n_rounds": n_rounds,
            "submit_wq": submit_wq,
            "llm_model": selected_llm_model,
            "llm_model_selection": {
                "scope": "run_pinned_primary",
                "fallback_policy": "configured_cross_review_model",
            },
            "evidence_chain": [
                "score_factor",
                "fxalpha_novelty_check",
                "run_backtest",
                "run_anti_overfit",
                "run_rolling_validation",
                "run_adversarial_validation",
                "fxalpha_quality_gate",
                "fxalpha_import_factors",
            ],
            "rolling_validation_policy": "required_deep_validation_evidence_included_in_deep_score",
            "candidate_plan_code_precheck": {
                "scope": "pre_score_schema_and_obvious_expression_error_triage",
                "pure_code": True,
                "llm_context_key": "code_precheck",
                "soft_marks_block_score": False,
                "uncertain_defaults_to_score": True,
                "promising_parent_mutation_defaults_to_score": True,
                "llm_skip_actions": ["revise_expression", "skip_batch_duplicate", "skip_library_near_copy"],
                "gui_candidate_lanes": [
                    "precheck_blocked",
                    "semantic_revision",
                    "planned_for_score",
                    "candidate_plan_dropped",
                ],
                "not_replacement_for": "fxalpha_novelty_check_numeric_factor_value_correlation",
            },
            "codex_foreground_required": False,
        }
        if interrupted_handoff:
            research_contract["interrupted_handoff"] = interrupted_handoff
        if requested_resume_run_id:
            research_contract["recovery_attempt"] = True
            _write_orchestrator_control_request(
                run_id=run_id,
                action="resume",
                reason="operator_requested_resume",
            )
        _write_orchestrator_launch_event(run_id=run_id, inputs=inputs, contract=research_contract)
        thread = _start_orchestrator_background(run_id, inputs, research_contract)
        with _GUI_RUNS_LOCK:
            active = _GUI_RUNS.get(run_id)
            if active:
                active["status"] = "running"
                active["stage"] = "orchestrator_background_started"
                active["summary"] = {
                    "orchestration": "deepseek_background_orchestrator",
                    "llm_model": selected_llm_model,
                    "evaluation_mode": evaluation_mode,
                    "evaluation_profile_version": evaluation["profile_version"],
                    "event_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE),
                    "llm_trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
                    "research_step_projection": True,
                    "thread": thread.name,
                    "thread_id": getattr(thread, "ident", None),
                    "worker_mode": getattr(thread, "mode", "process"),
                    "worker_unit": getattr(thread, "unit", ""),
                    "worker_pid": getattr(thread, "pid", None),
                    "interrupted_previous_run": bool(interrupted_event),
                }
                _append_job_event(
                    active,
                    {
                        "event": "orchestrator_background_started",
                        "message": "DeepSeek-backed Orchestrator detached worker started.",
                        "event_ref": {"path": str(FACTOR_ORCHESTRATOR_EVENTS_FILE), "run_id": run_id},
                        "thread": thread.name,
                        "thread_id": getattr(thread, "ident", None),
                        "worker_mode": getattr(thread, "mode", "process"),
                        "worker_unit": getattr(thread, "unit", ""),
                        "worker_pid": getattr(thread, "pid", None),
                        "interrupted_previous_run": bool(interrupted_event),
                    },
                )
                _persist_job(active)
        return ok_result(
            inputs=inputs,
            outputs={
                "status": "running",
                "run_id": run_id,
                "llm_model": selected_llm_model,
                "evaluation_mode": evaluation_mode,
                "profile_version": evaluation["profile_version"],
                "evaluation_profile_version": evaluation["profile_version"],
                "evidence_class": evaluation["evidence_class"],
                "job": _job_snapshot(job),
                "orchestrator": {
                    "event_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE),
                    "llm_trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
                    "llm_trace_schema": "orchestrator_llm_trace_v1",
                    "mode": "deepseek_background_event_plus_research_step_projection",
                    "thread": thread.name,
                    "thread_id": getattr(thread, "ident", None),
                    "worker_mode": getattr(thread, "mode", "process"),
                    "worker_unit": getattr(thread, "unit", ""),
                    "worker_pid": getattr(thread, "pid", None),
                    "interrupted_previous_run": bool(interrupted_event),
                    "interrupted_event_stage_id": interrupted_event.get("stage_id") if interrupted_event else None,
                },
            },
            artifacts={
                "orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE),
                "orchestrator_llm_trace_file": str(FACTOR_ORCHESTRATOR_LLM_TRACES_FILE),
                "research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE),
            },
        )

    if orchestration_mode == "codex_mcp":
        codex_mcp_instructions = {
            "mode": "codex_mcp",
            "mcp_server": "quantgpt",
            "codex_config": str(
                Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
                / "config.toml"
            ),
            "quantgpt_code_root": str(QUANTGPT_CODE_ROOT),
            "research_contract": {
                "evaluation_mode": evaluation_mode,
                "profile_version": evaluation["profile_version"],
                "evaluation_profile_version": evaluation["profile_version"],
                "evaluation_contract_hash": evaluation["config_snapshot_hash"],
                "evidence_class": evaluation["evidence_class"],
                "evaluation_profile_snapshot": evaluation,
                "universe": universe,
                "selection_start_date": start_date,
                "selection_end_date": end_date,
                "value_start_date": value_start_date,
                "value_end_date": value_end_date,
                "benchmark": benchmark,
                "holding_period": holding_period,
                "n_groups": n_groups,
                "neutralize_cap": neutralize_cap,
                "neutralize_industry": neutralize_industry,
                "target_adopted": target_adopted,
                "submit_wq": submit_wq,
            },
            "required_mcp_flow": [
                "list_operators / list_universes / fxalpha_context(run_id=run_id)",
                (
                    "use research_contract.selection_start_date to research_contract.selection_end_date for "
                    "score_factor, run_backtest, run_anti_overfit, run_rolling_validation, run_adversarial_validation, "
                    "fxalpha_novelty_check, and fxalpha_quality_gate; do not select/import factors using "
                    "the model final test window"
                ),
                "fxalpha_record_research_step(stage=protocol_load) after reading context, using research_step_v2 with previous_stage/stage/stage_transition.next_stage",
                "fxalpha_code_advice(checkpoint=candidate_plan) for the shared code precheck; the LLM reviews every non-fatal lane before score",
                "validate_expression",
                "score_factor fast screening",
                "fxalpha_code_advice(checkpoint=score_review) after the scored batch; treat it as evidence, not an automatic LLM replacement",
                "fxalpha_record_research_step(stage=score_review) after each scored batch, including round_id, stage_seq, previous_stage, facts, judgment, next_stage, next_action, research_strategy, why, history_used, evidence_refs, and one candidate_lanes/candidate_decisions entry per evaluated candidate",
                "fxalpha_code_advice(checkpoint=novelty_review) and fxalpha_record_research_step(stage=novelty_review) after fxalpha_novelty_check, before deep validation; persist each keeper/drop decision in candidate_lanes or candidate_decisions",
                "run_backtest / diagnose_factor when useful / run_anti_overfit / run_rolling_validation / run_adversarial_validation as required deep-validation evidence",
                "fxalpha_code_advice(checkpoint=deep_validation_review) and fxalpha_record_research_step(stage=deep_validation_review) after deep validation, before import gate",
                "compute_factor_values for novelty/self-correlation when needed",
                "fxalpha_quality_gate as import gate",
                "fxalpha_code_advice(checkpoint=import_gate_review) and fxalpha_record_research_step(stage=import_gate_review) after import gate, including adopted/screened/rejected reasons",
                (
                    "fxalpha_import_factors for accepted factors, using research_contract.value_start_date "
                    "to research_contract.value_end_date and preserving selection_start_date/selection_end_date metadata"
                ),
                "fxalpha_record_research_step(stage=import_review) after fxalpha_import_factors when import was attempted",
            ],
            "legacy_fallback": "removed",
        }
        _write_research_step(
            {
                "schema_version": "research_step_v2",
                "ts": _now_iso(),
                "run_id": run_id,
                "round_id": f"{run_id}:r0000",
                "stage_seq": 1,
                "stage_id": f"{run_id}:r0000:s01_protocol_load",
                "previous_stage": None,
                "previous_stage_id": None,
                "stage": "protocol_load",
                "summary": "GUI created an explicit Codex MCP debugging/review run. Progress should be recorded through research_steps, not job files.",
                "decision": "Waiting for a human-supervised Codex session to continue through native QuantGPT MCP tools and record progress with fxalpha_record_research_step.",
                "refs": [run_id, "factor_research_start"],
                "priority": "normal",
                "stage_transition": {
                    "next_stage": "pre_batch_decision",
                    "next_action": "Open a Codex session with QuantGPT MCP tools, read PROMPT.md, call list_operators and fxalpha_context, then design the first thesis-first candidate batch.",
                    "research_strategy": "normal process flow to pre-batch decision",
                    "facts": "FXAlpha GUI created an explicit native Codex MCP debugging/review run and exposed the shared governed workflow.",
                    "judgment": "Research has not started yet; Codex must load PROMPT.md and governed context before candidate generation.",
                    "why": "MCP mode is intentionally manual and must remain grounded in the same current context, factor map, evidence, and quality rules as production ORCH.",
                    "history_used": "No in-run history yet; use the factor map and recent research steps before candidate design.",
                },
                "evidence_refs": [],
                "tags": ["protocol_load", "codex_native_mcp"],
                "extra": {
                    "source": "factor_research_start",
                    "runtime_contract": "codex_native_mcp_debug",
                    "evaluation_mode": evaluation_mode,
                    "profile_version": evaluation["profile_version"],
                    "evaluation_profile_version": evaluation["profile_version"],
                    "evaluation_contract_hash": evaluation["config_snapshot_hash"],
                    "evidence_class": evaluation["evidence_class"],
                    "job_files_deprecated": True,
                    "research_contract": codex_mcp_instructions["research_contract"],
                    "required_mcp_flow": codex_mcp_instructions["required_mcp_flow"],
                },
            }
        )
        with _GUI_RUNS_LOCK:
            _supersede_waiting_codex_jobs_unlocked(keep_run_id=run_id)
            active = _GUI_RUNS.get(run_id)
            if active:
                active["status"] = "waiting_codex_mcp"
                active["stage"] = "waiting_for_codex_mcp"
                active["summary"] = {"orchestration": "codex_mcp_debug", "legacy_runner_started": False}
                _append_job_event(
                    active,
                    {
                        "event": "codex_mcp_supervision_required",
                        "message": "Explicit MCP debugging/review mode is waiting for human-supervised Codex tool calls. Production defaults to Orchestrator.",
                        "instructions": codex_mcp_instructions,
                    },
                )
                _persist_job(active)
        return ok_result(
            inputs=inputs,
            outputs={
                "status": "waiting_codex_mcp",
                "run_id": run_id,
                "debug_mode": True,
                "job": _job_snapshot(job),
                "codex_mcp": codex_mcp_instructions,
            },
            artifacts={"research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE)},
            warnings=[
                "Codex MCP is an explicit manual debugging/review mode. Restart Codex after MCP config changes, then supervise QuantGPT MCP tools directly."
            ],
        )

def _factor_research_guidance_rejection(run_id: str, latest_step: dict) -> str:
    """Return a user-facing reason when a run cannot consume another guidance."""
    run_text = str(run_id or "").strip()
    if not run_text:
        return "guidance requires a run_id"
    with _GUI_RUNS_LOCK:
        job = dict(_GUI_RUNS.get(run_text) or {})
    job_status = str(job.get("status") or "").strip().lower()
    control_action = str(_orchestrator_control_request(run_text).get("control_action") or "").strip().lower()
    if control_action == "stop" or job_status == "stop_requested":
        return "the run is stopping and has no next LLM judgment"
    if job_status in {"running", "pause_requested", "resume_requested", "paused"}:
        return ""
    if not latest_step:
        return "run_id was not found in active jobs or research_steps"
    latest_stage = str(latest_step.get("stage") or "").strip().lower()
    tags = {str(tag).strip().lower() for tag in (latest_step.get("tags") or [])}
    if latest_stage == "checkpoint_stop":
        if "operator_pause" in tags:
            return ""
        return "the run is completed and has no next LLM judgment"
    if latest_stage == "blocker":
        recoverable = bool(
            _latest_orchestrator_interruption_blocker(run_text)
            or _latest_orchestrator_recoverable_llm_blocker(run_text)
            or _latest_operator_pause_event(run_text)
        )
        return "" if recoverable else "the blocked run is not resumable; start a new run instead"
    if latest_stage == "stop" or "operator_stop" in tags:
        return "the run is stopping and has no next LLM judgment"
    return "the run is not active, paused, or recoverable"


def factor_research_add_guidance(*, run_id: str, message: str, author: str = "operator") -> ServiceResult:
    clean = (message or "").strip()
    if not clean:
        return err_result("guidance message is empty", inputs={"run_id": run_id, "author": author})
    if len(clean) > FACTOR_RESEARCH_GUIDANCE_MAX_CHARS:
        return err_result(
            f"guidance message exceeds {FACTOR_RESEARCH_GUIDANCE_MAX_CHARS} characters",
            inputs={"run_id": run_id, "author": author, "message_chars": len(clean)},
        )
    run_id = str(run_id or "").strip()
    guidance_id = f"guidance_{uuid.uuid4().hex[:12]}"
    latest_step, latest_transition = _latest_stage_transition(run_id=run_id)
    rejection = _factor_research_guidance_rejection(run_id, latest_step)
    if rejection:
        return err_result(rejection, inputs={"run_id": run_id, "author": author})
    next_stage = str(latest_transition.get("next_stage") or latest_step.get("stage") or "pre_batch_decision")
    next_action = str(latest_transition.get("next_action") or "apply_operator_guidance_before_next_research_action")
    guidance_transition = {
        "next_stage": next_stage,
        "next_action": next_action,
        "research_strategy": "apply this operator guidance once in the next LLM judgment without changing the current stage unless that judgment returns upstream",
        "facts": "A one-shot operator guidance message was recorded for the current run.",
        "judgment": "The next LLM judgment must incorporate this guidance once; later stages must not inherit it.",
        "why": "Human guidance can change the next research judgment, but it is consumed when delivered and does not become persistent history.",
        "history_used": str(latest_step.get("stage_id") or "No prior in-run stage was found."),
    }
    with _GUI_RUNS_LOCK:
        job = _GUI_RUNS.get(run_id)
        if not job:
            factor_tool_record_research_step(
                stage="human_guidance",
                summary=clean,
                decision=f"Operator guidance recorded by {author}.",
                next_action="ORCH should consume this guidance once in the next LLM research judgment.",
                refs=[run_id] if run_id else [],
                priority="normal",
                run_id=run_id or "",
                stage_transition=guidance_transition,
                tags=["human_guidance"],
                extra={
                    "source": "factor_research_add_guidance",
                    "author": author,
                    "guidance_id": guidance_id,
                    "job_files_deprecated": True,
                },
            )
            return ok_result(
                inputs={"run_id": run_id, "author": author, "message": clean},
                outputs={"status": "recorded", "run_id": run_id, "guidance_id": guidance_id, "source": "research_steps"},
            )
        guidance_item = {
            "guidance_id": guidance_id,
            "author": author,
            "message": clean,
            "created_at": _now_iso(),
        }
        job.setdefault("guidance_history", []).append(guidance_item)
        _append_job_event(job, {"event": "guidance_received", "guidance": guidance_item})
        _persist_job(job)
        factor_tool_record_research_step(
            stage="human_guidance",
            summary=clean,
            decision=f"Operator guidance recorded by {author}.",
            next_action="ORCH should consume this guidance once in the next LLM research judgment.",
            refs=[run_id],
            priority="normal",
            run_id=run_id,
            stage_transition=guidance_transition,
            tags=["human_guidance"],
            extra={
                "source": "factor_research_add_guidance",
                "author": author,
                "guidance_id": guidance_id,
                "job_files_deprecated": True,
            },
        )
    return ok_result(
        inputs={"run_id": run_id, "author": author, "message": clean},
        outputs={"status": "recorded", "run_id": run_id, "guidance_id": guidance_id, "source": "research_steps"},
    )


def factor_research_reset(*, clear_model_features: bool = True) -> ServiceResult:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = FACTOR_DATA_ROOT.parent.parent / "runtime" / "reset_backups" / timestamp
    backup_root.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []

    def _backup_if_exists(src: Path, rel: str) -> None:
        if not src.exists():
            return
        dest = backup_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        moved.append(str(src))

    _backup_if_exists(FACTOR_REGISTRY_DB, "factors/factor_registry.db")
    _backup_if_exists(FACTOR_ACTIVE_ADOPTED_VALUES_FILE, "factors/active_adopted_factor_values.parquet")

    if FACTOR_PARQUET_DIR.exists():
        dest_dir = backup_root / "factors/parquet"
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(FACTOR_PARQUET_DIR), str(dest_dir))
        moved.append(str(FACTOR_PARQUET_DIR))

    if clear_model_features:
        if MODEL_FEATURE_SETS_ROOT.exists():
            dest_dir = backup_root / "model/feature_sets"
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(MODEL_FEATURE_SETS_ROOT), str(dest_dir))
            moved.append(str(MODEL_FEATURE_SETS_ROOT))
        _backup_if_exists(MODEL_ACTIVE_FEATURE_FILE, "model/active/combined_factors_df.parquet")
        _backup_if_exists(MODEL_ACTIVE_FEATURE_MANIFEST, "model/active/manifest.json")

    for status_file in (LATEST_PIPELINE_STATUS_FILE, LATEST_MODEL_STATUS_FILE):
        _backup_if_exists(status_file, f"runtime/{status_file.name}")

    FACTOR_PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    if clear_model_features:
        MODEL_FEATURE_SETS_ROOT.mkdir(parents=True, exist_ok=True)
        MODEL_ACTIVE_FEATURE_DIR.mkdir(parents=True, exist_ok=True)

    with _GUI_RUNS_LOCK:
        _GUI_RUNS.clear()

    registry = FactorRegistry()
    after_summary = registry.summary()
    payload = {
        "status": "reset_completed",
        "backup_root": str(backup_root),
        "moved": moved,
        "registry_summary": after_summary,
        "cleared_model_features": clear_model_features,
    }
    return ok_result(
        inputs={"clear_model_features": clear_model_features},
        outputs=payload,
        artifacts={"backup_root": str(backup_root), "factor_registry_db": str(FACTOR_REGISTRY_DB)},
    )




def factor_submit_wq_active(*, universe: str = FACTOR_DEFAULT_UNIVERSE, min_icir: float = 0.3) -> ServiceResult:
    try:
        from domain.factor_research.wq_submitter import submit_all_active

        result = submit_all_active(universe=universe, min_icir=min_icir)
        return ok_result(
            inputs={"universe": universe, "min_icir": min_icir},
            outputs=result,
            artifacts={"factor_registry_db": str(FACTOR_REGISTRY_DB)},
        )
    except Exception as e:
        return err_result(str(e), inputs={"universe": universe, "min_icir": min_icir})


def factor_wq_status() -> ServiceResult:
    try:
        from domain.factor_research.wq_submitter import check_submission_status

        result = check_submission_status()
        return ok_result(
            outputs=result,
            artifacts={"factor_registry_db": str(FACTOR_REGISTRY_DB)},
        )
    except Exception as e:
        return err_result(str(e))

def factor_registry_list(
    *,
    status: str = "active",
    category: str = "all",
    min_icir: float = 0.0,
    sort_by: str = "icir",
    limit: int = 20,
    offset: int = 0,
    holding_period_days: int | None = None,
    compact: bool = False,
) -> ServiceResult:
    registry = FactorRegistry()
    rows, total = registry.list_all(
        status=status,
        category=category,
        min_icir=min_icir,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
        holding_period_days=holding_period_days,
    )
    rows = [_enrich_factor_registry_row(row) for row in rows]
    if compact:
        rows = [
            {
                key: row.get(key)
                for key in (
                    "factor_id",
                    "name",
                    "expression",
                    "category",
                    "status",
                    "holding_period_days",
                    "icir",
                    "rank_icir",
                    "deep_score",
                )
            }
            for row in rows
        ]
    return ok_result(
        inputs={
            "status": status,
            "category": category,
            "min_icir": min_icir,
            "sort_by": sort_by,
            "limit": limit,
            "offset": offset,
            "holding_period_days": holding_period_days,
            "compact": compact,
        },
        outputs={"total": total, "items": rows, "registry_summary": registry.summary(), "compact": compact},
        artifacts={"factor_registry_db": str(FACTOR_REGISTRY_DB)},
    )


def _enrich_factor_registry_row(row: dict) -> dict:
    """Expose complete factor metrics stored in metadata without a DB migration."""
    item = dict(row or {})
    metadata = item.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metrics = metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {}
    backtest = metadata.get("backtest_summary") if isinstance(metadata.get("backtest_summary"), dict) else {}

    def first(*values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return None

    item["metadata"] = metadata
    item["economic_thesis"] = metadata.get("economic_thesis") if isinstance(metadata.get("economic_thesis"), dict) else {}
    item["hypothesis"] = first(item.get("hypothesis"), metadata.get("hypothesis"))
    item["rank_ic_mean"] = first(item.get("rank_ic"), metrics.get("rank_ic"), backtest.get("rank_ic_mean"))
    item["rank_icir"] = first(metrics.get("rank_icir"), backtest.get("rank_ic_ir"), backtest.get("rank_icir"))
    item["annual_return"] = first(metrics.get("annual_return"), backtest.get("annual_return"))
    item["quick_score"] = first(metrics.get("quick_score"), metadata.get("quick_score"))
    item["deep_score"] = first(metrics.get("deep_score"), metadata.get("deep_score"), (metadata.get("deep_validation") or {}).get("deep_score") if isinstance(metadata.get("deep_validation"), dict) else None)
    item["max_drawdown"] = first(item.get("max_drawdown"), metrics.get("max_drawdown"), backtest.get("max_drawdown"))
    item["turnover"] = first(item.get("turnover"), metrics.get("turnover"), backtest.get("turnover"))
    item["holding_period_days"] = int(first(item.get("holding_period_days"), metadata.get("holding_period_days"), 5) or 5)
    anti_overfit = metadata.get("anti_overfit") if isinstance(metadata.get("anti_overfit"), dict) else {}
    anti_overfit_summary = metadata.get("anti_overfit_summary") if isinstance(metadata.get("anti_overfit_summary"), dict) else {}
    item["anti_overfit"] = anti_overfit or anti_overfit_summary
    item["anti_overfit_summary"] = anti_overfit_summary or anti_overfit
    item["adversarial_validation"] = metadata.get("adversarial_validation") if isinstance(metadata.get("adversarial_validation"), dict) else {}
    item["persistence_diagnostic"] = metadata.get("persistence_diagnostic") if isinstance(metadata.get("persistence_diagnostic"), dict) else {}
    item["novelty_guard"] = metadata.get("novelty_guard") if isinstance(metadata.get("novelty_guard"), dict) else {}
    item["metadata_incomplete"] = bool(metadata.get("metadata_incomplete"))
    item["metadata_incomplete_reasons"] = metadata.get("metadata_incomplete_reasons") or []
    return item


_CONTEXT_EVIDENCE_REF_PREVIEW_LIMIT = 3
_CONTEXT_TEXT_PREVIEW_LIMIT = 240
_CONTEXT_FAILURE_NOTE_LIMIT = 160
_CONTEXT_FAILURE_PREVIEW_LIMIT = 3
_CONTEXT_COVERAGE_PREVIEW_LIMIT = 8


def _truncate_context_text(value: Any, limit: int = _CONTEXT_TEXT_PREVIEW_LIMIT) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _compact_active_factor_for_context(row: dict, *, supported_fields: set[str] | None = None) -> dict:
    """Return the minimal active-factor payload needed for research de-duplication."""
    item = dict(row or {})
    expression = re.sub(r"\s+", " ", str(item.get("expression") or "")).strip()
    signature = _candidate_symbolic_signature({"expression": expression}) if expression else {
        "fields": [],
        "operators": [],
        "windows": [],
    }
    supported = {str(field).lower() for field in (supported_fields or set()) if str(field).strip()}
    fields_used = [
        field
        for field in (signature.get("fields") or [])
        if not supported or str(field).lower() in supported
    ]
    return {
        "name": item.get("name"),
        "hypothesis": _truncate_context_text(item.get("hypothesis")),
        # Keep the complete expression until semantic fields/operators/windows
        # have been derived.  Prompt compactors may create an explicit preview,
        # but must never tokenize that preview again.
        "expression": expression,
        "expression_complete": True,
        "fields_used": fields_used,
        "operators_used": list(signature.get("operators") or []),
        "window_lengths": list(signature.get("windows") or []),
    }


def _compact_latest_step_for_context(step: dict) -> dict:
    """Return only the fields needed to resume the research process."""
    if not isinstance(step, dict):
        return {}
    evidence_refs = step.get("evidence_refs") if isinstance(step.get("evidence_refs"), list) else []
    tags = step.get("tags") if isinstance(step.get("tags"), list) else []
    return {
        "schema_version": step.get("schema_version"),
        "ts": step.get("ts") or step.get("created_at"),
        "run_id": step.get("run_id"),
        "round_id": step.get("round_id"),
        "stage_seq": step.get("stage_seq"),
        "stage_id": step.get("stage_id"),
        "previous_stage": step.get("previous_stage"),
        "previous_stage_id": step.get("previous_stage_id"),
        "stage": step.get("stage"),
        "summary": _truncate_context_text(step.get("summary")),
        "decision": _truncate_context_text(step.get("decision")),
        "priority": step.get("priority"),
        "tags": tags[:8],
        "evidence_refs": evidence_refs[:_CONTEXT_EVIDENCE_REF_PREVIEW_LIMIT],
    }


def factor_registry_duplicate_audit() -> ServiceResult:
    registry = FactorRegistry()
    duplicates = registry.audit_active_duplicates()
    return ok_result(
        outputs={
            "duplicate_groups": len(duplicates),
            "duplicate_factor_count": sum(len(group.get("duplicates", [])) for group in duplicates),
            "groups": duplicates,
            "summary": registry.summary(),
        },
        artifacts={"factor_registry_db": str(FACTOR_REGISTRY_DB)},
    )


def factor_registry_retire_duplicates(
    *,
    dry_run: bool = True,
    reason: str = "duplicate_active_expression",
) -> ServiceResult:
    registry = FactorRegistry()
    result = registry.retire_active_duplicates(dry_run=dry_run, reason=reason)
    result["summary_after"] = registry.summary()
    return ok_result(
        inputs={"dry_run": dry_run, "reason": reason},
        outputs=result,
        artifacts={"factor_registry_db": str(FACTOR_REGISTRY_DB)},
    )


def _quantgpt_field_context(limit_files: int = 8) -> dict:
    """Lightweight field availability summary for the QuantGPT MCP Agent."""
    from quantgpt.data_schema import AVAILABLE_FIELDS, BLOCKED_FIELDS, FIELD_ALIASES

    files = sorted(QUANTGPT_DATA_DIR.glob("*.parquet"))[: max(1, limit_files)] if QUANTGPT_DATA_DIR.exists() else []
    columns: dict[str, int] = {}
    non_null: dict[str, int] = {}
    rows_seen = 0
    examples: list[str] = []
    for path in files:
        df = None
        try:
            import pandas as pd

            df = pd.read_parquet(path)
        except Exception:
            continue
        examples.append(path.name)
        rows_seen += len(df)
        for col in df.columns:
            columns[str(col)] = columns.get(str(col), 0) + 1
            try:
                non_null[str(col)] = non_null.get(str(col), 0) + int(df[col].notna().sum())
            except Exception:
                pass
        del df
    release_process_memory("quantgpt_field_context_sample_completed")
    alpha_blocked_fields = {
        "up_limit": "Price-limit metadata is available for execution audits but is not an alpha-expression input.",
        "down_limit": "Price-limit metadata is available for execution audits but is not an alpha-expression input.",
        "backward_factor": "Adjustment metadata is handled by the data layer and is not an alpha-expression input.",
    }
    supported = sorted(
        field
        for field in AVAILABLE_FIELDS
        if field in columns
        and field not in BLOCKED_FIELDS
        and field not in alpha_blocked_fields
    )
    aliases = dict(FIELD_ALIASES)
    blocked_fields = dict(BLOCKED_FIELDS)
    blocked_fields.update(alpha_blocked_fields)
    unexpected_sample_fields = sorted(
        col for col in columns
        if col not in AVAILABLE_FIELDS and col not in {"trade_date", "stock_code"}
    )
    coverage_rows = {
        col: {
            "sampled_files": count,
            "sample_file_coverage": round(count / max(len(examples), 1), 4),
            "non_null_rows": non_null.get(col, 0),
            "non_null_rate": round(non_null.get(col, 0) / max(rows_seen, 1), 4),
        }
        for col, count in sorted(columns.items())
    }
    neutralization_status = {
        "industry_neutralization": {
            "available": "industry" in supported,
            "default_enabled": False,
            "source_field": "industry" if "industry" in supported else None,
            "reason": None if "industry" in supported else "industry column is not present in the current production raw dataset; baostock fetch is disabled in production scoring",
        },
        "cap_neutralization": {
            "available": "total_mv" in supported,
            "default_enabled": True,
            "source_field": "total_mv" if "total_mv" in supported else None,
            "reason": None if "total_mv" in supported else "total_mv column is not present; cap neutralization will be skipped",
        },
    }
    partial_coverage_fields = [
        col for col, meta in coverage_rows.items()
        if meta["sample_file_coverage"] < 1.0
    ]
    sparse_non_null_fields = [
        col for col, meta in coverage_rows.items()
        if meta["non_null_rate"] < 0.98
    ]
    attention_fields = [
        col for col in sorted(set(partial_coverage_fields + sparse_non_null_fields))
    ]
    compact_coverage = {
        col: coverage_rows[col]
        for col in attention_fields[:_CONTEXT_COVERAGE_PREVIEW_LIMIT]
    }
    return {
        "sampled_files": len(examples),
        "sampled_rows": rows_seen,
        "examples": examples[:3],
        "supported_fields": supported,
        "supported_field_count": len(supported),
        "aliases": aliases,
        "field_aliases": aliases,
        "field_descriptions": {
            field: AVAILABLE_FIELDS[field]
            for field in AVAILABLE_FIELDS
            if field not in BLOCKED_FIELDS and field not in alpha_blocked_fields
        },
        "unit_guidance": {
            "amount": "Daily turnover amount is in thousand CNY.",
            "moneyflow_amount_fields": (
                "sm_net_amount, lg_net_amount, and net_mf_amount are in ten-thousand CNY; "
                "for money-flow-to-turnover ratios use net_mf_amount * 10 / amount."
            ),
            "market_value_fields": "total_mv and float_mv are in ten-thousand CNY.",
            "share_fields": "tot_share, float_a_share, and free_share are in ten-thousand shares.",
            "margin_amount_fields": "margin_balance, margin_buy_amount, and short_balance are in CNY.",
        },
        "missing_value_semantics": {
            "margin_and_securities_lending": "Missing values are interpreted as zero balance/activity before expression evaluation.",
            "dividend_yield": "dv_ttm and alias dividend_yield missing values are interpreted as zero reported dividend yield.",
            "valuation_tail": "PE/PB/PS-style missing or non-positive values are mapped to worst-rank raw values for inverse valuation expressions.",
            "other_fields": "Other missing values remain NaN in the factor-expression layer.",
        },
        "coverage_by_field": compact_coverage,
        "coverage_summary": {
            "fully_covered_field_count": sum(
                1 for meta in coverage_rows.values() if meta["sample_file_coverage"] == 1.0
            ),
            "partial_file_coverage_fields": partial_coverage_fields[:_CONTEXT_COVERAGE_PREVIEW_LIMIT],
            "sparse_non_null_fields": sparse_non_null_fields[:_CONTEXT_COVERAGE_PREVIEW_LIMIT],
            "unexpected_sample_fields": unexpected_sample_fields[:_CONTEXT_COVERAGE_PREVIEW_LIMIT],
            "coverage_rows_included": list(compact_coverage.keys()),
            "note": (
                "coverage_by_field only includes fields that need attention in the sampled files. "
                "Use supported_fields for the complete field list."
            ),
        },
        "blocked_fields": blocked_fields,
        "blocked_field_count": len(blocked_fields),
        "neutralization_status": neutralization_status,
    }


def _fxalpha_must_read_contract() -> dict:
    """High-signal research contract extracted from PROMPT.md and README.md."""
    project_root = Path(__file__).resolve().parents[1]
    prompt_path = QUANTGPT_CODE_ROOT / "PROMPT.md"
    research_root = project_root / "domain" / "factor_research"
    return {
        "prompt_path": str(prompt_path),
        "readme_path": str(research_root / "README.md"),
        "must_read_before_candidate_generation": True,
        "not_automatic": True,
        "default_production_mode": "orchestrator",
        "production_runbook_path": str(research_root / "ORCHESTRATOR_README.md"),
        "mcp_debug_prompt_path": str(prompt_path),
        "production_boundary": [
            "Use the governed FXAlpha Orchestrator as the default production factor-mining controller.",
            "Keep Codex direct native QuantGPT MCP available as an explicit manual debugging, failure-isolation, and evidence-review mode.",
            "ORCH and MCP must share the same score, novelty, deep-validation, quality-gate, import, and research-step contracts; MCP is not a second quality standard.",
            "Do not replace score, novelty, validation, quality gate, or import with shell scripts, curl, HTTP glue, temporary Python clients, or legacy runners.",
            "In explicit MCP debugging mode, if native QuantGPT MCP tools are missing, stop before candidate generation with mcp_native_tools_missing.",
        ],
        "required_workflow": {
            "startup_and_context": [
                "list_operators",
                "fxalpha_context",
                "list_universes when needed",
                "fxalpha_record_research_step(stage=protocol_load)",
            ],
            "research_design_and_quick_screen": [
                "pre_batch_decision",
                "thesis-first candidate design: economic_thesis -> hypothesis -> expression",
                "candidate_plan with code_precheck",
                "validate_expression",
                "score_factor",
                "score_review",
                "candidate_decision when candidate selection needs judgment",
            ],
            "formal_validation_and_import": [
                "fxalpha_novelty_check",
                "novelty_review",
                "run_backtest",
                "diagnose_factor when useful",
                "run_anti_overfit",
                "run_rolling_validation",
                "run_adversarial_validation",
                "deep_validation_review",
                "fxalpha_quality_gate",
                "import_gate_review",
                "fxalpha_import_factors only for quality-gate adopted candidates",
                "import_review",
            ],
            "stop_and_blocker": [
                "checkpoint_stop",
                "blocker",
            ],
        },
        "research_discipline": [
            "Every batch is thesis-first: economic_thesis -> hypothesis -> expression.",
            "Complex expressions are overfitting-risk signals and should trigger simplify-first review.",
            "Prefer the smallest tradable translation when it tests the same thesis.",
            "Every retained component must map to an economic or market-microstructure mechanism.",
            "Run pre-score expression code_precheck inside candidate_plan; fatal schema/expression failures project precheck_blocked and do not reach validate_expression or score_factor.",
            "code_precheck handles deterministic expression errors; Candidate Plan projects planned_for_score or candidate_plan_dropped and may skip only evidenced batch duplicates or library near-copies; uncertain or promising-parent mutations default to score.",
            "Candidate Plan is only a pre-score research-budget decision; every scored survivor still passes the official numeric fxalpha_novelty_check.",
            "Library family context comes only from the pinned information audit; final novelty remains fxalpha_novelty_check over factor values.",
        ],
        "logging_contract": {
            "tool": "fxalpha_record_research_step",
            "gui_rule": "The FXAlpha GUI reads LLM process output only from fxalpha_record_research_step.",
            "language_rule": "summary and decision must use concise Chinese.",
            "formal_chain": "previous_stage -> stage -> stage_transition.next_stage",
            "naming_contract": {
                "shared_by": ["codex_mcp", "orchestrator_projection"],
                "run_id": "stable GUI/heartbeat/manual research-session id reused for every step in the same session",
                "round_id": "{run_id}:rNNNN for normal research rounds; terminal/recovery rows may use {run_id}:stop, {run_id}:blocker, or {run_id}:interrupted",
                "stage_seq": "integer order inside the round",
                "stage_id": "{round_id}:sNN_stage with zero-padded stage_seq and snake_case stage",
                "progress_stage_id": "candidate/tool progress may append :candidate_N_<candidate_id> or another short suffix while preserving the same base stage_id",
                "previous_stage_id": "must point to the immediately previous visible research-step row across Codex MCP and Orchestrator mode handoffs",
            },
            "evidence_rule": "Raw tool metrics stay in the task store and should be referenced through evidence_refs instead of copied into research steps.",
            "required_stages": [
                "protocol_load",
                "pre_batch_decision",
                "candidate_plan",
                "score_review",
                "candidate_decision",
                "novelty_review",
                "deep_validation_review",
                "import_gate_review",
                "import_review",
                "checkpoint_stop",
                "blocker",
            ],
        },
        "import_rules": [
            "fxalpha_quality_gate is the only final import gate.",
            "score_factor is quick screening only and cannot justify import by itself.",
            "Only final score_factor payloads with status=success, screening_stage=quick_score, declared grade A/B, quick_score meeting the B threshold, and explicit deep-validation hint/decision may advance to novelty.",
            "Novelty must be checked against the same holding_period_days active pool and within-batch near neighbors.",
            "Novelty rejection remains a hard pre-deep-validation gate: batch_redundancy, active_pool_low_information_gain, or novelty_correlation_veto must stop the candidate in the current round.",
            "Only fxalpha_novelty_check keepers with novelty_guard.allowed true and combined_guard.allowed true when present may advance to deep validation; LLM disagreement is audit-only.",
            "The advisory mode described below applies only to the counterfactual distress_proxy_exposure diagnostic, not to novelty correlation.",
            "Production factor values use a fixed 2026-06-01 tradable_non_st baseline for factor cross-sections; status fields remain metadata for diagnostics and downstream tradability checks.",
            "The ST exposure object in fxalpha_novelty_check is a counterfactual all-market distress_proxy_exposure diagnostic by default, not a production-row ST-membership check.",
            "Default st_exposure_guard_mode is advisory: a failed distress_proxy_exposure diagnostic must be recorded as risk_tags/advisory_flag and must not by itself block deep_validation, fxalpha_quality_gate, or fxalpha_import_factors.",
            "Only when st_exposure_guard_mode is hard may reject_st_exposure / st_exposure_veto stop a candidate before deep validation or import.",
            "The distress_proxy_exposure diagnostic must normalize factor-value stock indexes: qlib-style sh.600000/sz.000004, market-code 600000.SH/000004.SZ, and instrument 600000sh/000004sz must resolve to the same stock identity map before ST-name checks.",
            "Import should use the value-output window, not the selection window.",
            "Candidates entering import consideration must carry holding-period, novelty, backtest/deep-validation, anti-overfit, run_rolling_validation as required deep-validation evidence, adversarial, economic_thesis, and hypothesis evidence.",
            "Candidates entering deep validation or quality gate must receive a final import-or-reject decision with recorded reasons.",
        ],
        "stop_policy_summary": [
            "Low score, crowded novelty, weak thesis, or gate rejection are not stop reasons by themselves.",
            "Allowed early stops are limited to tool, service, logging, or runtime blockers.",
            "If cutoff occurs before the target is met, record checkpoint_stop with live resume state.",
        ],
    }


def factor_tool_context(
    *,
    evaluation_mode: str | None = None,
    skip_quantgpt_probe: bool = False,
    run_id: str = "",
) -> ServiceResult:
    """Platform context exposed to the QuantGPT factor-research subsystem."""
    runtime_defaults = factor_research_runtime_defaults(evaluation_mode=evaluation_mode)
    registry = FactorRegistry()
    active = [_enrich_factor_registry_row(row) for row in registry.list_active(min_icir=-1e9, holding_period_days=FACTOR_DEFAULT_HOLDING_PERIOD)]
    latest_step, latest_transition = _latest_stage_transition()
    field_context = _quantgpt_field_context()
    governed_factor_map = factor_map_context()
    design_factor_map = factor_map_design_context(
        governed_factor_map,
        run_id=str(run_id or "").strip(),
    )
    supported_fields = {
        str(field).lower()
        for field in (field_context.get("supported_fields") or [])
        if str(field).strip()
    }
    return ok_result(
        outputs={
            "must_read_contract": _fxalpha_must_read_contract(),
            "config": {
                "evaluation_mode": runtime_defaults["evaluation_mode"],
                "active_default_evaluation_mode": runtime_defaults["active_default_evaluation_mode"],
                "profile_version": runtime_defaults["profile_version"],
                "evaluation_profile_version": runtime_defaults["evaluation_profile_version"],
                "evaluation_contract_hash": runtime_defaults["evaluation_contract_hash"],
                "evidence_class": runtime_defaults["evidence_class"],
                "default_universe": runtime_defaults["universe"],
                "default_start_date": runtime_defaults["selection_start_date"],
                "default_end_date": runtime_defaults["selection_end_date"],
                "default_value_start_date": runtime_defaults["value_start_date"],
                "default_value_end_date": runtime_defaults["value_end_date"],
                "default_holding_period": FACTOR_DEFAULT_HOLDING_PERIOD,
                "rolling_validation": {
                    "schema_version": FACTOR_ROLLING_SCHEMA_VERSION,
                    "score_policy_version": FACTOR_ROLLING_SCORE_POLICY_VERSION,
                    "max_history_months": FACTOR_ROLLING_MAX_HISTORY_MONTHS,
                    "min_history_months": FACTOR_ROLLING_MIN_HISTORY_MONTHS,
                    "period_weights": list(FACTOR_ROLLING_PERIOD_WEIGHTS),
                    "stability_penalty": FACTOR_ROLLING_STABILITY_PENALTY,
                    "rank_ic_full_score": FACTOR_ROLLING_RANK_IC_FULL_SCORE,
                    "min_dates_per_6m": FACTOR_ROLLING_MIN_DATES_PER_6M,
                    "trailing_horizons_months": list(FACTOR_ROLLING_TRAILING_HORIZONS),
                },
                "default_benchmark": FACTOR_DEFAULT_BENCHMARK,
                "default_top_frac": FACTOR_DEFAULT_TOP_FRAC,
                "default_cost_rate": FACTOR_DEFAULT_COST_RATE,
                "default_neutralize_cap": True,
                "default_neutralize_industry": False,
                "st_exposure_guard_mode": get_live_st_exposure_guard_mode(),
                "st_exposure_guard_scope": "counterfactual_all_market",
                "st_exposure_guard_label": "distress_proxy_exposure",
                "st_exposure_guard_default_behavior": "advisory_risk_tag_not_hard_veto",
            },
            "active_factor_summary": {
                "registry_summary": registry.summary(),
                "active_factor_count": len(active),
                "active_factors": [
                    _compact_active_factor_for_context(row, supported_fields=supported_fields)
                    for row in active
                ],
            },
            "latest_stage_transition": {
                "latest_stage": latest_step.get("stage"),
                "latest_step_ts": latest_step.get("ts") or latest_step.get("created_at"),
                "stage_transition": latest_transition,
                "latest_step": _compact_latest_step_for_context(latest_step),
            },
            "field_context": field_context,
            "factor_map_context": design_factor_map,
            "readiness": _factor_readiness(QUANTGPT_API_URL, skip_quantgpt_probe=skip_quantgpt_probe),
        },
        artifacts={
            "factor_registry_db": str(FACTOR_REGISTRY_DB),
            "factor_adopted_values": str(FACTOR_ADOPTED_VALUES_FILE),
        },
    )


def factor_tool_code_advice(
    *,
    checkpoint: str,
    candidates: list[dict] | None = None,
    trajectory: list[dict] | None = None,
    history: list[dict] | None = None,
    hypotheses: list[dict] | None = None,
    run_id: str = "",
    round_id: str = "",
    repeated_same_family: bool = False,
) -> ServiceResult:
    """Expose the same deterministic Candidate Plan and review advice used by ORCH.

    This is a read-only decision aid.  It does not replace the LLM review,
    numeric novelty, deep validation, quality gate, import, or research-step
    logging.  Fatal Candidate Plan findings remain code-owned; all other advice
    is evidence that the LLM may accept, refine, or reject with a reason.
    """

    clean_checkpoint = str(checkpoint or "").strip().lower()
    checkpoint_aliases = {
        "candidate_plan": "candidate_plan",
        "code_precheck": "candidate_plan",
        "score": "score_review",
        "score_review": "score_review",
        "quick": "score_review",
        "novelty": "novelty_review",
        "novelty_review": "novelty_review",
        "deep": "deep_validation_review",
        "deep_validation_review": "deep_validation_review",
        "gate": "import_gate_review",
        "import_gate_review": "import_gate_review",
    }
    normalized_checkpoint = checkpoint_aliases.get(clean_checkpoint)
    if not normalized_checkpoint:
        return err_result(
            "unsupported_code_advice_checkpoint",
            inputs={"checkpoint": checkpoint},
            outputs={"allowed_checkpoints": sorted(set(checkpoint_aliases.values()))},
        )

    candidate_items = [dict(item) for item in (candidates or []) if isinstance(item, dict)]
    trajectory_items = [dict(item) for item in (trajectory or []) if isinstance(item, dict)]
    history_items = [dict(item) for item in (history or []) if isinstance(item, dict)]

    if normalized_checkpoint == "candidate_plan":
        context_result = factor_tool_context(skip_quantgpt_probe=True, run_id=run_id)
        active_summary = (
            context_result.outputs.get("active_factor_summary")
            if context_result.ok and isinstance(context_result.outputs, dict)
            else {}
        )
        prior_refs = _prior_round_expression_refs(run_id, round_id)
        checks = _candidate_plan_code_precheck(
            candidate_items,
            active_factor_summary=active_summary,
            prior_round_expression_refs=prior_refs,
            hypotheses=hypotheses or [],
        )
        lanes = _candidate_plan_precheck_candidate_lanes(candidate_items, checks)
        scoreable_count = sum(
            str(item.get("candidate_lane") or "") != "precheck_blocked"
            for item in lanes
        )
        advice = {
            "checkpoint": "candidate_plan",
            "action": "review_then_score" if scoreable_count else "return_expression_design",
            "strategy": "code_precheck",
            "code_precheck": _candidate_plan_code_precheck_summary(checks),
            "candidate_lane_decisions": lanes,
            "scoreable_count": scoreable_count,
            "blocked_count": len(lanes) - scoreable_count,
            "llm_review_required": True,
            "allowed_actions": ["score_factor"] if scoreable_count else ["expression_design"],
            "blocked_actions": ["score_factor"] if not scoreable_count else [],
        }
    elif normalized_checkpoint == "score_review":
        advice = quick_advice(candidate_items, trajectory=trajectory_items)
    elif normalized_checkpoint == "novelty_review":
        advice = novelty_advice(
            candidate_items,
            repeated_same_family=bool(repeated_same_family),
            history=history_items or trajectory_items,
        )
    elif normalized_checkpoint == "deep_validation_review":
        advice = deep_advice(candidate_items, trajectory=trajectory_items)
    else:
        advice = gate_advice(candidate_items)

    return ok_result(
        inputs={
            "checkpoint": normalized_checkpoint,
            "candidate_count": len(candidate_items),
            "trajectory_count": len(trajectory_items),
            "history_count": len(history_items),
            "run_id": str(run_id or ""),
            "round_id": str(round_id or ""),
        },
        outputs={
            "advisory_only": normalized_checkpoint != "candidate_plan",
            "fatal_precheck_is_code_owned": normalized_checkpoint == "candidate_plan",
            "shared_logic_source": "domain.factor_research.orchestrator",
            "advice": _jsonable(advice),
        },
    )


def factor_tool_classify_factor(*, expression: str, category: str = "") -> ServiceResult:
    """Classify a factor expression using the WorldQuant-aligned taxonomy."""
    classification = classify_factor_expression(expression, category)
    return ok_result(
        inputs={"expression": expression, "category": category},
        outputs={
            **classification,
            "standard_categories": list(STANDARD_FACTOR_CATEGORIES),
            "taxonomy": FACTOR_CATEGORY_TAXONOMY,
            "mcp_guidance": (
                "Call this before fxalpha_import_factors when category is unclear. "
                "If a factor combines multiple data-source groups, keep Composite as the primary category "
                "and preserve detailed category_tags in metadata."
            ),
        },
    )


def factor_tool_record_research_step(
    *,
    stage: str,
    summary: str,
    decision: str = "",
    next_action: str = "",
    refs: list[str] | None = None,
    priority: str = "normal",
    run_id: str = "",
    round_no: int | None = None,
    round_id: str = "",
    stage_seq: int | None = None,
    stage_id: str = "",
    previous_stage: str = "",
    previous_stage_id: str = "",
    stage_transition: dict | None = None,
    evidence_refs: list[dict] | None = None,
    candidate_lanes: list[dict] | dict | None = None,
    candidate_decisions: list[dict] | dict | None = None,
    tags: list[str] | None = None,
    extra: dict | None = None,
) -> ServiceResult:
    """Record a Codex/LLM research-process log for GUI display and replay.

    summary: factual summary of the completed stage.
    decision: short summary of the next move.
    stage_transition.next_action: formal detailed next step.
    """
    allowed_priorities = {"low", "normal", "high", "blocker"}
    clean_stage = str(stage or "note").strip() or "note"
    if clean_stage not in RESEARCH_DECISION_STAGES:
        clean_stage = "note"
    clean_priority = str(priority or "normal").strip().lower()
    if clean_priority not in allowed_priorities:
        clean_priority = "normal"
    clean_summary = _clip_text(summary, 700)
    if not clean_summary:
        return err_result(
            "research step summary is empty",
            inputs={"stage": stage, "decision": decision, "next": next_action},
        )
    raw_extra = _jsonable(extra) if extra else {}
    if not isinstance(raw_extra, dict):
        raw_extra = {}
    forbidden_extra_keys = sorted(set(raw_extra).intersection(RESEARCH_STEP_DEPRECATED_EXTRA_KEYS))
    if "stage_transition" in raw_extra:
        return err_result(
            "research step extra contains forbidden control fields; use top-level research_step fields only",
            inputs={
                "stage": stage,
                "run_id": run_id,
                "round_id": round_id,
                "forbidden_extra_keys": forbidden_extra_keys,
            },
        )
    clean_run_id = str(run_id or "").strip()[:120] or "manual"
    recent_steps = _read_recent_research_steps(
        limit=FACTOR_RESEARCH_STEPS_MAX_LINES,
        run_id=clean_run_id,
    )
    latest_step = recent_steps[0] if recent_steps else {}
    provisional_round_id = str(round_id or "").strip()
    if not provisional_round_id and round_no is not None:
        provisional_round_id = f"{clean_run_id}:r{round_no:04d}" if clean_run_id else f"round-r{round_no:04d}"
    if not provisional_round_id:
        provisional_round_id = str(latest_step.get("round_id") or "").strip() or f"{clean_run_id}:r0001"
    clean_stage_seq = _infer_research_stage_seq(provisional_round_id, stage_seq)
    clean_round_id, clean_stage_id = _normalize_round_stage_ids(
        run_id=clean_run_id,
        round_no=round_no,
        round_id=provisional_round_id,
        stage_id=stage_id,
        stage=clean_stage,
        stage_seq=clean_stage_seq,
    )
    clean_previous_stage = str(previous_stage or "").strip()
    clean_previous_stage_id = str(previous_stage_id or "").strip()
    if not clean_previous_stage and latest_step:
        clean_previous_stage = str(latest_step.get("stage") or "").strip()
    if not clean_previous_stage_id and latest_step:
        clean_previous_stage_id = str(latest_step.get("stage_id") or "").strip()
    clean_transition = _clean_stage_transition_payload(stage_transition, next_action=next_action)
    clean_decision = _clip_text(decision, 180)
    if not clean_decision:
        clean_decision = _clip_text(clean_transition.get("next_action", ""), 180)
    if clean_stage in RESEARCH_STRICT_CONTRACT_STAGES:
        missing_fields: list[str] = []
        if not clean_run_id:
            missing_fields.append("run_id")
        if not clean_round_id:
            missing_fields.append("round_id")
        if not clean_stage_seq:
            missing_fields.append("stage_seq")
        if not clean_stage_id:
            missing_fields.append("stage_id")
        if not clean_summary:
            missing_fields.append("summary")
        if not clean_decision:
            missing_fields.append("decision")
        if not clean_priority:
            missing_fields.append("priority")
        if not clean_transition.get("next_stage"):
            missing_fields.append("stage_transition.next_stage")
        if not clean_transition.get("next_action"):
            missing_fields.append("stage_transition.next_action")
        if missing_fields:
            return err_result(
                "research_step_v2 decision-stage schema violation",
                inputs={
                    "stage": clean_stage,
                    "run_id": clean_run_id,
                    "round_id": clean_round_id,
                    "stage_seq": clean_stage_seq,
                    "stage_id": clean_stage_id,
                    "previous_stage": clean_previous_stage,
                    "previous_stage_id": clean_previous_stage_id,
                    "missing_fields": missing_fields,
                },
                outputs={
                    "required_schema": {
                        "schema_version": "research_step_v2",
                        "required_top_level": [
                            "schema_version",
                            "ts",
                            "run_id",
                            "round_id",
                            "stage_seq",
                            "stage_id",
                            "previous_stage",
                            "previous_stage_id",
                            "stage",
                            "summary",
                            "decision",
                            "priority",
                        ],
                        "required_stage_transition": ["next_stage", "next_action"],
                        "optional_stage_transition": [
                            "research_strategy",
                            "facts",
                            "judgment",
                            "why",
                            "history_used",
                        ],
                        "optional_arrays": ["evidence_refs", "tags"],
                    }
                },
            )
    item = {
        "schema_version": "research_step_v2",
        "ts": _now_iso(),
        "run_id": clean_run_id,
        "round_id": clean_round_id,
        "stage_seq": clean_stage_seq,
        "stage_id": clean_stage_id,
        "previous_stage": clean_previous_stage,
        "previous_stage_id": clean_previous_stage_id,
        "stage": clean_stage,
        "summary": clean_summary,
        "decision": clean_decision,
        "refs": [str(ref)[:180] for ref in (refs or [])[:12] if str(ref).strip()],
        "priority": clean_priority,
    }
    clean_evidence_refs = _jsonable(evidence_refs or [])
    if not isinstance(clean_evidence_refs, list):
        clean_evidence_refs = []
    item["evidence_refs"] = clean_evidence_refs[:12]
    # Candidate-level facts are the canonical source for the live candidate
    # board.  ORCH events already write these fields, but native MCP sessions
    # previously had no place to persist them and therefore rendered an empty
    # board despite completed score/novelty/deep tools.
    clean_candidate_lanes = _orchestrator_candidate_lane_items(candidate_lanes, limit=80)
    clean_candidate_decisions = _orchestrator_candidate_lane_items(candidate_decisions, limit=80)
    if clean_candidate_lanes:
        item["candidate_lanes"] = _jsonable(clean_candidate_lanes)
    if clean_candidate_decisions:
        item["candidate_decisions"] = _jsonable(clean_candidate_decisions)
    clean_tags = [str(tag)[:80] for tag in (tags or [])[:12] if str(tag).strip()]
    item["tags"] = clean_tags
    clean_extra, removed_extra_keys = _clean_research_step_extra(raw_extra)
    item["stage_transition"] = clean_transition
    if removed_extra_keys:
        item["extra_removed_keys"] = sorted(set(removed_extra_keys))
    if extra:
        if clean_extra:
            item["extra"] = clean_extra
    _write_research_step(item)
    return ok_result(
        inputs={
            "stage": stage,
            "summary": summary,
            "decision": decision,
            "next": next_action,
            "refs": refs or [],
            "priority": priority,
            "run_id": run_id,
            "round_no": round_no,
            "round_id": round_id,
            "stage_seq": stage_seq,
            "stage_id": stage_id,
            "previous_stage": previous_stage,
            "previous_stage_id": previous_stage_id,
            "stage_transition": stage_transition or {},
            "evidence_refs": evidence_refs or [],
            "candidate_lanes": candidate_lanes or [],
            "candidate_decisions": candidate_decisions or [],
            "tags": tags or [],
            "extra_removed_keys": item.get("extra_removed_keys", []),
        },
        outputs={
            "recorded": item,
            "recent_steps": _read_recent_research_steps(limit=20),
            "retention": {
                "path": str(FACTOR_RESEARCH_STEPS_FILE),
                "history_dir": str(FACTOR_RESEARCH_STEPS_HISTORY_DIR),
                "max_lines": FACTOR_RESEARCH_STEPS_MAX_LINES,
                "gui_default_limit": 20,
            },
        },
        artifacts={
            "research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE),
            "research_steps_history_dir": str(FACTOR_RESEARCH_STEPS_HISTORY_DIR),
        },
    )


def factor_tool_record_orchestrator_event(
    *,
    event: dict,
    sync_research_step: bool = True,
) -> ServiceResult:
    """Record a full orchestrator event and optionally project it to research_steps."""
    if not isinstance(event, dict):
        return err_result("orchestrator event must be a JSON object", inputs={"event": event})
    required = ["run_id", "round_id", "stage", "summary", "decision", "stage_transition"]
    missing = [key for key in required if not event.get(key)]
    transition = event.get("stage_transition") if isinstance(event.get("stage_transition"), dict) else {}
    if not transition.get("next_stage"):
        missing.append("stage_transition.next_stage")
    if not transition.get("next_action"):
        missing.append("stage_transition.next_action")
    if missing:
        return err_result(
            "orchestrator_event_v1 schema violation",
            inputs={"missing_fields": missing, "event": event},
        )
    recorded = _write_orchestrator_event(event, sync_research_step=sync_research_step)
    return ok_result(
        inputs={"sync_research_step": sync_research_step},
        outputs={"recorded": recorded},
        artifacts={
            "orchestrator_events_file": str(FACTOR_ORCHESTRATOR_EVENTS_FILE),
            "research_steps_file": str(FACTOR_RESEARCH_STEPS_FILE),
        },
    )


def factor_tool_quality_gate(
    *,
    candidates: list[dict],
    start_date: str = FACTOR_DEFAULT_START_DATE,
    end_date: str = FACTOR_DEFAULT_END_DATE,
    min_abs_ic: float = 0.02,
    min_ir: float = 0.3,
    extra_existing_candidates: list[dict] | None = None,
    stage: str = "round",
    round_no: int | None = None,
    family: str | None = None,
    trusted_novelty_evidence: bool = False,
    run_id: str = "",
    round_id: str = "",
) -> ServiceResult:
    """FXAlpha local hard guard exposed as a platform tool to QuantGPT."""
    from domain.factor_research.quality_gate import evaluate_candidate_quality

    guard = _stage_guard_result(
        "fxalpha_quality_gate",
        allowed_stages={"deep_validation_review", "import_gate_review"},
        run_id=run_id,
        round_id=round_id,
    )
    if guard is not None:
        return guard
    report = evaluate_candidate_quality(
        candidates or [],
        start_date=start_date,
        end_date=end_date,
        min_abs_ic=min_abs_ic,
        min_ir=min_ir,
        extra_existing_candidates=extra_existing_candidates or [],
        stage=stage,
        round_no=round_no,
        family=family,
        trusted_novelty_evidence=trusted_novelty_evidence,
    )
    return ok_result(
        inputs={
            "candidate_count": len(candidates or []),
            "start_date": start_date,
            "end_date": end_date,
            "min_abs_ic": min_abs_ic,
            "min_ir": min_ir,
            "stage": stage,
            "round_no": round_no,
            "family": family,
            "trusted_novelty_evidence_requested": bool(trusted_novelty_evidence),
        },
        outputs=report,
        artifacts={"factor_registry_db": str(FACTOR_REGISTRY_DB)},
    )


def factor_tool_novelty_check(
    *,
    candidates: list[dict],
    start_date: str = FACTOR_DEFAULT_START_DATE,
    end_date: str = FACTOR_DEFAULT_END_DATE,
    extra_existing_candidates: list[dict] | None = None,
    pearson_threshold: float = 0.75,
    rank_threshold: float = 0.80,
    p90_pearson_threshold: float | None = None,
    p90_rank_threshold: float | None = None,
    run_id: str = "",
    round_id: str = "",
    _library_information_context: dict | None = None,
) -> ServiceResult:
    """Novelty and counterfactual distress-proxy review exposed as a platform tool to QuantGPT."""
    from domain.factor_research.dedup import assess_active_pool_novelty
    from domain.factor_research.quality_gate import attach_novelty_evidence

    guard = _stage_guard_result(
        "fxalpha_novelty_check",
        allowed_stages={"score_review", "candidate_decision", "novelty_review"},
        run_id=run_id,
        round_id=round_id,
    )
    if guard is not None:
        return guard
    information_context = (
        _library_information_context
        if isinstance(_library_information_context, dict)
        else factor_map_context()
    )
    cluster_by_factor_id: dict[str, str] = {}
    region_by_factor_id: dict[str, str] = {}
    for cluster in (
        information_context.get("regions")
        or information_context.get("information_families")
        or []
    ):
        if not isinstance(cluster, dict):
            continue
        cluster_id = str(cluster.get("cluster_id") or "").strip()
        region_uid = str(cluster.get("region_uid") or "").strip()
        if not cluster_id:
            continue
        members = list(cluster.get("members") or [])
        representative = cluster.get("representative")
        if isinstance(representative, dict):
            members.append(representative)
        for member in members:
            if not isinstance(member, dict):
                continue
            factor_id = str(member.get("factor_id") or "").strip()
            if factor_id:
                cluster_by_factor_id.setdefault(factor_id, cluster_id)
                if region_uid:
                    region_by_factor_id.setdefault(factor_id, region_uid)
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        candidate.setdefault(
            "trajectory_id",
            _factor_trajectory_id(
                run_id=run_id,
                round_id=round_id,
                candidate_id=str(candidate.get("candidate_id") or candidate.get("id") or ""),
                expression=str(candidate.get("expression") or ""),
            ),
        )
        candidate.setdefault("factor_map_id", information_context.get("map_id"))
        candidate.setdefault("factor_map_audit_id", information_context.get("audit_id"))
    result = assess_active_pool_novelty(
        candidates or [],
        start_date=start_date,
        end_date=end_date,
        extra_existing_candidates=extra_existing_candidates or [],
        information_cluster_by_factor_id=cluster_by_factor_id,
        information_region_by_factor_id=region_by_factor_id,
        factor_map_id=str(information_context.get("map_id") or ""),
        factor_map_audit_id=str(information_context.get("audit_id") or ""),
        pearson_threshold=pearson_threshold,
        rank_threshold=rank_threshold,
        p90_pearson_threshold=p90_pearson_threshold,
        p90_rank_threshold=p90_rank_threshold,
    )
    result = attach_novelty_evidence(
        result,
        start_date=start_date,
        end_date=end_date,
        extra_existing_candidates=extra_existing_candidates or [],
        pearson_threshold=pearson_threshold,
        rank_threshold=rank_threshold,
        p90_pearson_threshold=p90_pearson_threshold,
        p90_rank_threshold=p90_rank_threshold,
    )
    return ok_result(
        inputs={
            "candidate_count": len(candidates or []),
            "start_date": start_date,
            "end_date": end_date,
            "pearson_threshold": pearson_threshold,
            "rank_threshold": rank_threshold,
            "p90_pearson_threshold": p90_pearson_threshold,
            "p90_rank_threshold": p90_rank_threshold,
            "st_exposure_guard_mode": get_live_st_exposure_guard_mode(),
            "distress_proxy_guard": {
                "scope": "counterfactual_all_market",
                "label": "distress_proxy_exposure",
                "avg_top50_ratio": 0.05,
                "p95_top50_ratio": 0.15,
                "top_n": 50,
                "stock_code_formats": ["sh.600000", "sz.000004", "600000.SH", "000004.SZ", "600000sh", "000004sz"],
            },
        },
        outputs=result,
        artifacts={"factor_registry_db": str(FACTOR_REGISTRY_DB)},
    )


def factor_tool_import(
    *,
    candidates: list[dict],
    universe: str,
    start_date: str = FACTOR_VALUE_DEFAULT_START_DATE,
    end_date: str = FACTOR_VALUE_DEFAULT_END_DATE,
    selection_start_date: str = FACTOR_DEFAULT_START_DATE,
    selection_end_date: str = FACTOR_DEFAULT_END_DATE,
    category: str = "",
    submit_wq: bool = False,
    run_id: str = "",
    round_id: str = "",
) -> ServiceResult:
    """Import tool exposed to QuantGPT after candidates pass FXAlpha guards.

    `start_date` / `end_date` are the value-output window. The import decision
    itself must have been made on `selection_start_date` / `selection_end_date`.
    """
    guard = _stage_guard_result(
        "fxalpha_import_factors",
        allowed_stages={"import_gate_review", "import_review"},
        run_id=run_id,
        round_id=round_id,
    )
    if guard is not None:
        return guard
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        candidate.setdefault("run_id", run_id)
        candidate.setdefault("round_id", round_id)
        candidate.setdefault(
            "trajectory_id",
            _factor_trajectory_id(
                run_id=run_id,
                round_id=round_id,
                candidate_id=str(candidate.get("candidate_id") or candidate.get("id") or ""),
                expression=str(candidate.get("expression") or ""),
            ),
        )
    result = _run_import_factors_isolated(
        candidates=candidates or [],
        universe=universe,
        start_date=start_date,
        end_date=end_date,
        selection_start_date=selection_start_date,
        selection_end_date=selection_end_date,
        category=category,
        submit_wq=submit_wq,
    )
    refresh_state: dict[str, Any] = {}
    imported_count = int(result.get("imported") or 0)
    registry_imported = imported_count > 0
    result["registry_imported"] = registry_imported
    result["active_values_refresh_required"] = False
    result["active_values_refresh_status"] = "not_required"
    result["model_feature_refresh_status"] = "not_required"
    result["model_feature_snapshot_status"] = "not_required"
    result["model_feature_snapshot_trigger"] = "model_side"
    result["import_sync_status"] = {
        "registry_imported": registry_imported,
        "active_values": "not_required",
        "model_snapshot": "not_required",
        "trigger_owner": "model_side",
    }
    if registry_imported:
        try:
            refresh_state = _enqueue_active_values_refresh_after_import()
            refresh_status = str(refresh_state.get("status") or "queued")
            model_required = bool(refresh_state.get("model_refresh_required", True))
            result["active_values_refresh_required"] = True
            result["active_values_refresh"] = refresh_state
            result["active_values_refresh_status"] = refresh_status
            model_snapshot_status = "refresh_required" if model_required else str((refresh_state.get("model_refresh") or {}).get("status") or "skipped")
            result["model_feature_snapshot_status"] = model_snapshot_status
            result["model_feature_snapshot_trigger"] = "model_side"
            # Compatibility alias. This no longer means a model-side rebuild was queued.
            result["model_feature_refresh_status"] = model_snapshot_status
            result["import_sync_status"] = {
                "registry_imported": True,
                "active_values": refresh_status,
                "model_snapshot": model_snapshot_status,
                "trigger_owner": "model_side",
            }
        except Exception as exc:
            refresh_state = {"status": "active_values_refresh_enqueue_failed", "last_error": str(exc)}
            result["active_values_refresh_required"] = True
            result["active_values_refresh"] = refresh_state
            result["active_values_refresh_status"] = "active_values_refresh_enqueue_failed"
            result["model_feature_snapshot_status"] = "not_started"
            result["model_feature_snapshot_trigger"] = "model_side"
            result["model_feature_refresh_status"] = "not_started"
            result["import_sync_status"] = {
                "registry_imported": True,
                "active_values": "active_values_refresh_enqueue_failed",
                "model_snapshot": "not_started",
                "trigger_owner": "model_side",
                "last_error": str(exc),
            }
    return ok_result(
        inputs={
            "candidate_count": len(candidates or []),
            "universe": universe,
            "start_date": start_date,
            "end_date": end_date,
            "selection_start_date": selection_start_date,
            "selection_end_date": selection_end_date,
            "category": category,
            "submit_wq": submit_wq,
        },
        outputs=result,
        artifacts={
            "factor_registry_db": str(FACTOR_REGISTRY_DB),
            "factor_parquet_dir": str(FACTOR_PARQUET_DIR),
            "active_values_refresh_state": refresh_state,
        },
    )


def _enqueue_active_values_refresh_after_import() -> dict[str, Any]:
    """Queue refresh in a process that will outlive the import caller.

    A detached Orchestrator worker exits as soon as its target is reached. A
    daemon thread started inside that worker would therefore be interrupted
    immediately after a successful import. In that runtime only, hand the
    existing refresh request to the long-lived 18081 API process. Normal API
    and foreground callers keep using the in-process service directly.
    """
    if os.environ.get("FXALPHA_ORCHESTRATOR_WORKER") != "1":
        return enqueue_active_values_refresh(
            holding_period_days=FACTOR_DEFAULT_HOLDING_PERIOD,
            trigger="fxalpha_import_factors",
            refresh_model=True,
        )

    base_url = str(
        os.environ.get("FXALPHA_PLATFORM_API_URL") or "http://127.0.0.1:18081"
    ).rstrip("/")
    body = json.dumps(
        {
            "holding_period_days": FACTOR_DEFAULT_HOLDING_PERIOD,
            "trigger": "fxalpha_import_factors",
            "refresh_model": True,
            "source_mode": "tail",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/factor/active-values/refresh",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    state = payload.get("outputs") if isinstance(payload, dict) else None
    if not isinstance(state, dict):
        raise RuntimeError("active_values_refresh_api_invalid_response")
    return {**state, "durable_owner": "fxalpha-api-18081"}
