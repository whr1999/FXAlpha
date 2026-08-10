from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from storage.paths import CONFIG_FILE, PROJECT_ROOT


EVALUATION_MODES = ("research", "production")
EVALUATION_STATE_FILE = PROJECT_ROOT / "runtime" / "platform" / "evaluation_mode.json"


class EvaluationProfileError(ValueError):
    """Raised when an evaluation profile or requested mode is invalid."""


def _read_config(config_file: Path = CONFIG_FILE) -> dict[str, Any]:
    if not config_file.exists():
        raise EvaluationProfileError(f"config_not_found:{config_file}")
    parsed = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    if not isinstance(parsed, dict):
        raise EvaluationProfileError("config_root_must_be_mapping")
    return parsed


def _evaluation_config(config_file: Path = CONFIG_FILE) -> dict[str, Any]:
    config = _read_config(config_file)
    section = config.get("platform_evaluation")
    if not isinstance(section, dict):
        raise EvaluationProfileError("platform_evaluation_config_missing")
    return section


def _read_runtime_state(state_file: Path = EVALUATION_STATE_FILE) -> dict[str, Any]:
    if not state_file.exists():
        return {}
    try:
        parsed = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _validate_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode not in EVALUATION_MODES:
        raise EvaluationProfileError(
            f"invalid_evaluation_mode:{mode or '<empty>'};allowed={','.join(EVALUATION_MODES)}"
        )
    return mode


def _validated_factor_contract(profile: dict[str, Any]) -> dict[str, str]:
    factor = profile.get("factor")
    if not isinstance(factor, dict):
        raise EvaluationProfileError("evaluation_profile_factor_contract_missing")
    required = (
        "selection_start_date",
        "selection_end_date",
        "value_start_date",
        "value_end_date",
    )
    result = {key: str(factor.get(key) or "").strip() for key in required}
    missing = [key for key, value in result.items() if not value]
    if missing:
        raise EvaluationProfileError(f"evaluation_profile_factor_fields_missing:{','.join(missing)}")
    if result["selection_start_date"] > result["selection_end_date"]:
        raise EvaluationProfileError("selection_window_is_reversed")
    if result["value_start_date"] > result["value_end_date"]:
        raise EvaluationProfileError("value_window_is_reversed")
    if result["selection_end_date"] > result["value_end_date"]:
        raise EvaluationProfileError("selection_end_after_value_end")
    return result


def resolve_evaluation_profile(
    evaluation_mode: str | None = None,
    *,
    config_file: Path = CONFIG_FILE,
    state_file: Path = EVALUATION_STATE_FILE,
) -> dict[str, Any]:
    """Resolve a stable task contract without mutating runtime state."""
    section = _evaluation_config(config_file)
    runtime_state = _read_runtime_state(state_file)
    configured_default = _validate_mode(section.get("default_mode", "production"))
    active_default = configured_default
    if runtime_state.get("evaluation_mode"):
        try:
            active_default = _validate_mode(runtime_state.get("evaluation_mode"))
        except EvaluationProfileError:
            active_default = configured_default
    mode = _validate_mode(evaluation_mode) if evaluation_mode is not None else active_default
    profiles = section.get("profiles")
    if not isinstance(profiles, dict) or not isinstance(profiles.get(mode), dict):
        raise EvaluationProfileError(f"evaluation_profile_missing:{mode}")
    profile = dict(profiles[mode])
    factor = _validated_factor_contract(profile)
    model = dict(profile.get("model") or {})
    canonical = {
        "schema_version": str(section.get("schema_version") or "evaluation_contract_v1"),
        "evaluation_mode": mode,
        "profile_version": str(profile.get("profile_version") or f"{mode}_v1"),
        "label": str(profile.get("label") or mode),
        "evidence_class": str(profile.get("evidence_class") or "unspecified"),
        "factor": factor,
        "model": model,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    canonical["config_snapshot_hash"] = hashlib.sha256(encoded).hexdigest()
    canonical["active_default_mode"] = active_default
    canonical["configured_default_mode"] = configured_default
    canonical["runtime_state_source"] = str(state_file) if runtime_state.get("evaluation_mode") else "config_default"
    return canonical


def evaluation_profile_status(
    *,
    config_file: Path = CONFIG_FILE,
    state_file: Path = EVALUATION_STATE_FILE,
) -> dict[str, Any]:
    active = resolve_evaluation_profile(config_file=config_file, state_file=state_file)
    profiles = {
        mode: resolve_evaluation_profile(mode, config_file=config_file, state_file=state_file)
        for mode in EVALUATION_MODES
    }
    return {
        "schema_version": active["schema_version"],
        "active_default_mode": active["active_default_mode"],
        "active_profile": active,
        "profiles": profiles,
        "switch_scope": "new_tasks_only",
        "running_tasks_immutable": True,
        "factor_library_membership_policy": "shared_unchanged_stage1",
        "model_profile_consumption": "not_enabled_stage1",
        "state_file": str(state_file),
    }


def set_default_evaluation_mode(
    evaluation_mode: str,
    *,
    changed_by: str = "operator",
    config_file: Path = CONFIG_FILE,
    state_file: Path = EVALUATION_STATE_FILE,
) -> dict[str, Any]:
    """Atomically set the default mode used only by future task creation."""
    mode = _validate_mode(evaluation_mode)
    resolved = resolve_evaluation_profile(mode, config_file=config_file, state_file=state_file)
    payload = {
        "schema_version": "evaluation_mode_state_v1",
        "evaluation_mode": mode,
        "profile_version": resolved["profile_version"],
        "config_snapshot_hash": resolved["config_snapshot_hash"],
        "changed_at": datetime.now().isoformat(timespec="seconds"),
        "changed_by": str(changed_by or "operator").strip() or "operator",
        "switch_scope": "new_tasks_only",
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{state_file.name}.", dir=str(state_file.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, state_file)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return evaluation_profile_status(config_file=config_file, state_file=state_file)
