from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from storage.paths import (
    ACTIVE_MODEL_FEATURE_SET_FILE,
    CURRENT_PRODUCTION_DATASET_FILE,
    DATA_FOUNDATION_ROOT,
    FACTOR_ACTIVE_ADOPTED_VALUES_MANIFEST,
    FACTOR_PARQUET_DIR,
    FACTOR_REGISTRY_DB,
    MODEL_ACTIVE_FEATURE_MANIFEST,
    MODEL_FEATURE_SETS_ROOT,
    MODEL_REGISTRY_DB,
    MODEL_RUNTIME_ROOT,
    MODEL_RUNS_ROOT,
    PROJECT_ROOT,
    RUNTIME_ROOT,
)

from .cleanup_policy import (
    DEFAULT_RETENTION_DAYS,
    cleanup_categories,
    is_under,
    is_manual_review,
    is_protected,
)
from .disk_audit import format_bytes, path_size, utc_now

MAINTENANCE_ROOT = RUNTIME_ROOT / "maintenance"
CLEANUP_RUNS_ROOT = MAINTENANCE_ROOT / "cleanup_runs"
LATEST_STATUS_FILE = MAINTENANCE_ROOT / "latest_status.json"
DATA_FOUNDATION_LOCK_NAMES = (
    "production_update.lock",
    "update.lock",
    "staging.lock",
    "promote.lock",
)
PICKLE_CACHE_SUFFIXES = {".pkl", ".pickle", ".zip", ".joblib"}
SAFE_SUMMARY_KINDS = (
    "pickle_cache",
    "logs",
    "quantgpt_reports",
    "reset_backups",
    "data_foundation_staging",
    "data_foundation_production_backups",
    "data_foundation_misc_backups",
    "factor_parquet_archive",
    "retired_factor_values",
    "model_feature_sets",
    "trading_prediction_features",
    "factor_value_repair",
    "model_quarantine",
    "model_archive",
    "model_official_qlib_isolation",
    "model_diagnostics",
    "model_audits",
    "model_diagnostic_recomputed_factors",
    "factor_research_trace_history",
    "factor_research_event_history",
    "factor_research_repair_backups",
    "factor_research_registry_backups",
    "runtime_test_tmp",
    "runtime_task_tmp",
)


@dataclass
class CleanupCandidate:
    path: str
    kind: str
    bytes: int
    human_size: str
    modified_at: str | None
    reason: str
    risk: str
    profile: str
    executable: bool = True
    blocked_reason: str = ""
    protected_reason: str = ""


def _stat_time(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _older_than(path: Path, days: int, now: datetime) -> bool:
    if days <= 0:
        return True
    try:
        return datetime.fromtimestamp(path.stat().st_mtime) < now - timedelta(days=days)
    except OSError:
        return False


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path)


def _fast_file_tree_size(path: Path) -> int:
    total = 0
    try:
        items = path.rglob("*") if path.is_dir() else [path]
        for item in items:
            if item.is_file():
                try:
                    total += int(item.stat().st_size)
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _add_path(mapping: dict[Path, str], value: Any, reason: str) -> None:
    if value is None:
        return
    text = str(value).strip()
    if not text:
        return
    mapping[Path(text)] = reason


def _walk_json_values(obj: Any, keys: set[str]) -> list[str]:
    values: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and value:
                values.append(str(value))
            values.extend(_walk_json_values(value, keys))
    elif isinstance(obj, list):
        for value in obj:
            values.extend(_walk_json_values(value, keys))
    return values


def _feature_set_dir(feature_set_id: str) -> Path:
    return MODEL_FEATURE_SETS_ROOT / str(feature_set_id)


def _normalize_existing_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser()


def _path_matches(path: Path, refs: dict[Path, str]) -> str:
    resolved = _normalize_existing_path(path)
    for ref, reason in refs.items():
        ref_resolved = _normalize_existing_path(ref)
        if resolved == ref_resolved or is_under(resolved, ref_resolved) or is_under(ref_resolved, resolved):
            return reason
    return ""


def _load_json_any(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _active_factor_value_paths() -> set[Path]:
    data = _load_json_any(FACTOR_ACTIVE_ADOPTED_VALUES_MANIFEST)
    paths: set[Path] = set()
    if not isinstance(data, dict):
        return paths
    for record in data.get("factor_records") or []:
        if not isinstance(record, dict):
            continue
        value = record.get("data_path")
        if value:
            paths.add(_normalize_existing_path(Path(str(value))))
    return paths


def _retired_factor_value_paths() -> dict[Path, str]:
    paths: dict[Path, str] = {}
    if not FACTOR_REGISTRY_DB.exists():
        return paths
    try:
        conn = sqlite3.connect(f"file:{FACTOR_REGISTRY_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT factor_id, metadata FROM factors WHERE status='retired'").fetchall()
        conn.close()
    except Exception:
        return paths
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except Exception:
            metadata = {}
        value = metadata.get("data_path") if isinstance(metadata, dict) else None
        if not value:
            continue
        path = Path(str(value))
        if path.exists() and path.is_file():
            paths[_normalize_existing_path(path)] = str(row["factor_id"] or "retired_factor")
    return paths


def _active_feature_protection_map() -> dict[Path, str]:
    protected: dict[Path, str] = {}
    data = _load_json_any(ACTIVE_MODEL_FEATURE_SET_FILE)
    if not isinstance(data, dict):
        return protected
    for key in [
        "feature_set_path",
        "manifest_path",
        "combined_factors_path",
        "manifest_file",
        "combined_factors_file",
        "feature_file",
    ]:
        _add_path(protected, data.get(key), "active_model_feature_snapshot")
    feature_set_id = data.get("feature_set_id")
    if feature_set_id:
        protected[_feature_set_dir(str(feature_set_id))] = "active_model_feature_snapshot"
    return protected


def _active_feature_protection() -> list[Path]:
    return list(_active_feature_protection_map())


def _model_registry_feature_refs() -> dict[Path, str]:
    feature_sets: dict[Path, str] = {}
    if not MODEL_REGISTRY_DB.exists():
        return feature_sets
    try:
        conn = sqlite3.connect(f"file:{MODEL_REGISTRY_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT feature_set_id, status, metadata, created_at "
            "FROM models ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
    except Exception:
        return feature_sets
    for row in rows:
        status = str(row["status"] or "").lower()
        reason = f"model_registry_{status or 'unknown'}"
        feature_set_id = str(row["feature_set_id"] or "").strip()
        if feature_set_id:
            feature_sets[_feature_set_dir(feature_set_id)] = reason
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except Exception:
            metadata = {}
        for value in _walk_json_values(metadata, {"feature_set_path", "manifest_path", "combined_factors_path"}):
            _add_path(feature_sets, value, reason)
    return feature_sets


def _add_model_feature_references(data: Any, reason: str, feature_sets: dict[Path, str]) -> None:
    for feature_set_id in _walk_json_values(data, {"feature_set_id"}):
        feature_sets[_feature_set_dir(feature_set_id)] = reason
    for key in [
        "feature_set_path",
        "manifest_path",
        "manifest_file",
        "combined_factors_path",
        "combined_factors_file",
        "platform_combined_factors_file",
        "feature_file",
    ]:
        for value in _walk_json_values(data, {key}):
            _add_path(feature_sets, value, reason)


def _recent_model_run_manifests(limit: int = 10) -> list[Path]:
    if not MODEL_RUNS_ROOT.exists():
        return []
    manifests: list[Path] = []
    try:
        run_dirs = [path for path in MODEL_RUNS_ROOT.iterdir() if path.is_dir()]
    except OSError:
        return []
    for run_dir in sorted(run_dirs, key=_mtime, reverse=True)[: max(1, int(limit or 10))]:
        manifest = run_dir / "manifest.json"
        if manifest.exists():
            manifests.append(manifest)
    return manifests


def _latest_orchestrator_model_run_ids(limit: int = 5) -> list[str]:
    events_file = MODEL_RUNTIME_ROOT / "orchestrator_events" / "current.jsonl"
    if not events_file.exists():
        return []
    seen: list[str] = []
    try:
        rows = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:
        return []
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        for value in _walk_json_values(row, {"model_run_id", "latest_model_run_id"}):
            model_run_id = str(value).strip()
            if model_run_id and model_run_id not in seen:
                seen.append(model_run_id)
            if len(seen) >= limit:
                return seen
    return seen


def _model_runtime_feature_refs() -> dict[Path, str]:
    feature_sets: dict[Path, str] = {}
    for path, reason in (
        (ACTIVE_MODEL_FEATURE_SET_FILE, "active_model_feature_snapshot"),
        (MODEL_ACTIVE_FEATURE_MANIFEST, "active_model_feature_manifest"),
    ):
        _add_model_feature_references(_load_json_any(path), reason, feature_sets)
    for manifest in _recent_model_run_manifests(limit=10):
        _add_model_feature_references(_load_json_any(manifest), "model_run_manifest_recent", feature_sets)
    for model_run_id in _latest_orchestrator_model_run_ids(limit=5):
        manifest = MODEL_RUNS_ROOT / model_run_id / "manifest.json"
        _add_model_feature_references(_load_json_any(manifest), "model_orchestrator_latest_run", feature_sets)
    return feature_sets


def _candidate(
    path: Path,
    kind: str,
    reason: str,
    risk: str,
    profile: str,
    extra_protected: list[Path],
    blocked_reason_override: str = "",
    protected_reason: str = "",
    size_hint: int | None = None,
    enforce_global_protection: bool = True,
) -> CleanupCandidate:
    blocked_reason = blocked_reason_override
    executable = not bool(blocked_reason_override)
    if executable and enforce_global_protection and is_protected(path, extra=extra_protected):
        executable = False
        blocked_reason = "protected_asset"
    elif executable and enforce_global_protection and is_manual_review(path):
        executable = False
        blocked_reason = "manual_review_required"
    size = path_size(path) if size_hint is None else int(size_hint)
    return CleanupCandidate(
        path=str(path),
        kind=kind,
        bytes=size,
        human_size=format_bytes(size),
        modified_at=_stat_time(path),
        reason=reason,
        risk=risk,
        profile=profile,
        executable=executable,
        blocked_reason=blocked_reason,
        protected_reason=protected_reason or blocked_reason,
    )


def _collect_cache_dirs(extra_protected: list[Path]) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    roots = [
        PROJECT_ROOT / "domain",
        PROJECT_ROOT / "services",
        PROJECT_ROOT / "storage",
        PROJECT_ROOT / "scripts",
        PROJECT_ROOT / "integrations",
        PROJECT_ROOT / "mcp_servers",
        PROJECT_ROOT / "external" / "quantgpt" / "quantgpt",
        PROJECT_ROOT / "tests",
    ]
    for pattern, kind in [("__pycache__", "pycache"), (".pytest_cache", "pytest_cache")]:
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob(pattern):
                if path.is_dir():
                    size_hint = _fast_file_tree_size(path)
                    candidates.append(
                        _candidate(
                            path,
                            kind=kind,
                            reason=f"Regenerable Python cache directory: {pattern}",
                            risk="low",
                            profile="safe",
                            extra_protected=extra_protected,
                            size_hint=size_hint,
                        )
                    )
    for path, kind in (
        (PROJECT_ROOT / "__pycache__", "pycache"),
        (PROJECT_ROOT / ".pytest_cache", "pytest_cache"),
    ):
        if path.is_dir():
            candidates.append(
                _candidate(
                    path,
                    kind=kind,
                    reason=f"Regenerable root cache directory: {path.name}",
                    risk="low",
                    profile="safe",
                    extra_protected=extra_protected,
                    size_hint=_fast_file_tree_size(path),
                )
            )
    return candidates


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _active_process_uses(path: Path) -> bool:
    target = str(path.resolve())
    proc_root = Path("/proc")
    if not proc_root.exists():
        return False
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
        except OSError:
            continue
        if target in cmdline:
            return True
    return False


def _live_lock_reason(path: Path) -> str:
    for lock in path.rglob(".lock"):
        try:
            raw = lock.read_text(encoding="utf-8", errors="ignore").strip()
            pid = int(raw) if raw.isdigit() else None
        except OSError:
            pid = None
        if _pid_alive(pid):
            return f"live_lock_pid_{pid}"
    return ""


def _collect_runtime_test_tmp(*, profile: str, now: datetime, days: int) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    roots = [RUNTIME_ROOT / "test-tmp", RUNTIME_ROOT / "tmp_pytest", RUNTIME_ROOT / "tmp" / "test-tmp"]
    for root in roots:
        if not root.exists():
            continue
        sessions = sorted(root.glob("pytest-of-*/pytest-*"), key=_mtime, reverse=True)
        for path in sessions:
            if not path.is_dir():
                continue
            blocked_reason = ""
            if _active_process_uses(path):
                blocked_reason = "active_process_reference"
            elif lock_reason := _live_lock_reason(path):
                blocked_reason = lock_reason
            elif not _older_than(path, days, now):
                blocked_reason = f"retained_by_{days}_day_retention"
            candidates.append(
                _candidate(
                    path,
                    kind="runtime_test_tmp",
                    reason=f"Pytest temporary session; only inactive sessions older than {days} day(s) are removable.",
                    risk="low",
                    profile=profile,
                    extra_protected=[],
                    blocked_reason_override=blocked_reason,
                    protected_reason=blocked_reason,
                )
            )
        # Python 3.11's pathlib glob can omit a broken symlink when the
        # symlink is the final path component. Enumerate the live parent
        # directories and address the well-known link directly so cleanup is
        # deterministic across supported Python versions.
        session_roots = sorted(path for path in root.glob("pytest-of-*") if path.is_dir())
        for session_root in session_roots:
            current = session_root / "pytest-current"
            if current.is_symlink() and not current.exists():
                candidates.append(
                    _candidate(
                        current,
                        kind="runtime_test_tmp",
                        reason="Broken pytest-current symlink left after its inactive session was removed.",
                        risk="low",
                        profile=profile,
                        extra_protected=[],
                        size_hint=0,
                    )
                )
    return candidates


def _collect_runtime_task_tmp(*, profile: str, now: datetime, days: int) -> list[CleanupCandidate]:
    root = RUNTIME_ROOT / "tmp"
    if not root.exists():
        return []
    candidates: list[CleanupCandidate] = []
    for path in sorted([item for item in root.iterdir() if item.is_dir() and item.name.startswith("fxalpha_")], key=_mtime, reverse=True):
        terminal = any(path.glob("*result*.json"))
        blocked_reason = ""
        if _active_process_uses(path):
            blocked_reason = "active_process_reference"
        elif lock_reason := _live_lock_reason(path):
            blocked_reason = lock_reason
        elif not terminal:
            blocked_reason = "terminal_result_missing"
        elif not _older_than(path, days, now):
            blocked_reason = f"retained_by_{days}_day_retention"
        candidates.append(
            _candidate(
                path,
                kind="runtime_task_tmp",
                reason=f"Completed FXAlpha temporary task; only inactive terminal tasks older than {days} day(s) are removable.",
                risk="low",
                profile=profile,
                extra_protected=[],
                blocked_reason_override=blocked_reason,
                protected_reason=blocked_reason,
            )
        )
    return candidates


def _collect_file_tree(
    root: Path,
    kind: str,
    days: int,
    now: datetime,
    risk: str,
    profile: str,
    reason: str,
    extra_protected: list[Path],
) -> list[CleanupCandidate]:
    if not root.exists():
        return []
    candidates: list[CleanupCandidate] = []
    for path in root.rglob("*"):
        if path.is_file() and _older_than(path, days, now):
            candidates.append(
                _candidate(
                    path,
                    kind=kind,
                    reason=reason,
                    risk=risk,
                    profile=profile,
                    extra_protected=extra_protected,
                )
            )
    return candidates


def _collect_pickle_cache_files(
    root: Path,
    days: int,
    now: datetime,
    risk: str,
    profile: str,
    extra_protected: list[Path],
) -> list[CleanupCandidate]:
    if not root.exists():
        return []
    candidates: list[CleanupCandidate] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in PICKLE_CACHE_SUFFIXES:
            continue
        if not _older_than(path, days, now):
            continue
        candidates.append(
            _candidate(
                path,
                kind="pickle_cache",
                reason=f"Regenerable pickle_cache artifact ({path.suffix.lower()}); older than {days} days.",
                risk=risk,
                profile=profile,
                extra_protected=extra_protected,
            )
        )
    return candidates


def _collect_child_dirs(
    root: Path,
    kind: str,
    days: int,
    now: datetime,
    risk: str,
    profile: str,
    reason: str,
    extra_protected: list[Path],
    keep_latest: int = 0,
    enforce_global_protection: bool = True,
) -> list[CleanupCandidate]:
    if not root.exists():
        return []
    children = [path for path in root.iterdir() if path.is_dir()]
    children = sorted(children, key=_mtime, reverse=True)
    keep = set(children[:keep_latest])
    candidates: list[CleanupCandidate] = []
    for path in children:
        if path in keep:
            continue
        if _older_than(path, days, now):
            candidates.append(
                _candidate(
                    path,
                    kind=kind,
                    reason=reason,
                    risk=risk,
                    profile=profile,
                    extra_protected=extra_protected,
                    enforce_global_protection=enforce_global_protection,
                )
            )
    return candidates


def _collect_retired_factor_values(
    *,
    profile: str,
    extra_protected: list[Path],
) -> list[CleanupCandidate]:
    active_paths = _active_factor_value_paths()
    retired_paths = _retired_factor_value_paths()
    candidates: list[CleanupCandidate] = []
    parquet_root = _normalize_existing_path(FACTOR_PARQUET_DIR)
    for path, factor_id in retired_paths.items():
        if path in active_paths:
            blocked_reason = "active_manifest_factor_value"
        elif not (path == parquet_root or is_under(path, parquet_root)):
            blocked_reason = "outside_factor_parquet_root"
        else:
            blocked_reason = ""
        candidates.append(
            _candidate(
                path,
                kind="retired_factor_values",
                reason=f"Single-factor parquet for retired factor {factor_id}; active manifest references remain protected.",
                risk="medium",
                profile=profile,
                extra_protected=extra_protected,
                blocked_reason_override=blocked_reason,
                protected_reason=blocked_reason,
                enforce_global_protection=False,
            )
        )
    return candidates


def _collect_model_feature_sets(
    *,
    profile: str,
    now: datetime,
    keep_latest: int,
    active_refs: dict[Path, str],
    model_registry_refs: dict[Path, str],
    model_runtime_refs: dict[Path, str],
) -> list[CleanupCandidate]:
    if not MODEL_FEATURE_SETS_ROOT.exists():
        return []
    children = sorted([path for path in MODEL_FEATURE_SETS_ROOT.iterdir() if path.is_dir()], key=_mtime, reverse=True)
    recent_keep = set(children[:keep_latest])
    fresh_cutoff = now - timedelta(hours=48)
    candidates: list[CleanupCandidate] = []
    protection_refs = {**model_runtime_refs, **model_registry_refs, **active_refs}
    for path in children:
        modified = datetime.fromtimestamp(_mtime(path)) if _mtime(path) else None
        blocked_reason = _path_matches(path, protection_refs)
        if not blocked_reason and path in recent_keep:
            blocked_reason = f"retained_recent_{keep_latest}_model_feature_sets"
        if not blocked_reason and modified and modified >= fresh_cutoff:
            blocked_reason = "fresh_within_48h"
        candidates.append(
            _candidate(
                path,
                kind="model_feature_sets",
                reason="Old model feature set directory; active, registry-referenced, recent, and fresh feature sets are protected.",
                risk="medium",
                profile=profile,
                extra_protected=[],
                blocked_reason_override=blocked_reason,
                protected_reason=blocked_reason,
            )
        )
    return candidates


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _data_foundation_busy() -> str:
    for lock_name in DATA_FOUNDATION_LOCK_NAMES:
        lock_path = DATA_FOUNDATION_ROOT / lock_name
        if lock_path.exists():
            return f"{lock_name.replace('.', '_')}_exists"
    status = _load_json(DATA_FOUNDATION_ROOT / "daily_update_status.json")
    status_values: list[str] = []
    for key in ["status", "stage", "promote_status"]:
        value = status.get(key)
        if value:
            status_values.append(str(value).lower())
    latest_stage = status.get("latest_stage")
    if isinstance(latest_stage, dict):
        for key in ["status", "stage", "promote_status"]:
            value = latest_stage.get(key)
            if value:
                status_values.append(str(value).lower())
    running_markers = {"running", "in_progress", "staging", "promoting", "downloading", "converting"}
    if any(value in running_markers or value.endswith("_running") for value in status_values):
        return "data_daily_update_running"
    return ""


def _current_data_foundation_refs() -> tuple[str, str, str]:
    current = _load_json(CURRENT_PRODUCTION_DATASET_FILE)
    production_package_id = str(current.get("production_package_id") or "")
    promotion_id = str(current.get("promotion_id") or "")
    if not production_package_id:
        status = _load_json(DATA_FOUNDATION_ROOT / "daily_update_status.json")
        production_package_id = str(status.get("package_id") or "")
        promotion_id = promotion_id or str(status.get("promotion_id") or "")
        promotion = status.get("last_successful_promotion")
        if isinstance(promotion, dict):
            production_package_id = production_package_id or str(promotion.get("package_id") or "")
            promotion_id = promotion_id or str(promotion.get("promotion_id") or "")
    missing: list[str] = []
    if not production_package_id:
        missing.append("current_production_package")
    if not promotion_id:
        missing.append("current_promotion_backup")
    missing_reason = "missing_" + "_and_".join(missing) if missing else ""
    return production_package_id, promotion_id, missing_reason


def _collect_data_foundation_dirs(profile: str, now: datetime, keep_extra: int) -> list[CleanupCandidate]:
    fresh_cutoff = now - timedelta(hours=24)
    staging_root = DATA_FOUNDATION_ROOT / "staging"
    backup_root = DATA_FOUNDATION_ROOT / "production_backups"
    current_stage, current_backup, missing_current_reason = _current_data_foundation_refs()
    busy_reason = _data_foundation_busy()
    global_block_reason = busy_reason or missing_current_reason

    specs = [
        (
            staging_root,
            "data_foundation_staging",
            current_stage,
            "current_production_staging_package",
            "Data foundation staging package; non-production build artifact used for validation and rollback.",
        ),
        (
            backup_root,
            "data_foundation_production_backups",
            current_backup,
            "current_production_backup",
            "Data foundation production backup; used for rollback after promote.",
        ),
    ]
    candidates: list[CleanupCandidate] = []
    for root, kind, current_name, current_reason, description in specs:
        if not root.exists():
            continue
        children = sorted([path for path in root.iterdir() if path.is_dir()], key=_mtime, reverse=True)
        current_paths = {path for path in children if path.name == current_name}
        extra_kept: set[Path] = set()
        for path in children:
            if path in current_paths:
                continue
            if len(extra_kept) >= keep_extra:
                break
            extra_kept.add(path)
        for path in children:
            blocked_reason = ""
            protected_reason = ""
            modified = datetime.fromtimestamp(_mtime(path)) if _mtime(path) else None
            if global_block_reason:
                blocked_reason = global_block_reason
                protected_reason = global_block_reason
            elif path in current_paths:
                blocked_reason = current_reason
                protected_reason = current_reason
            elif modified and modified >= fresh_cutoff:
                blocked_reason = "fresh_within_24h"
                protected_reason = "fresh_within_24h"
            elif path in extra_kept:
                blocked_reason = f"retained_recent_{keep_extra}_{kind}"
                protected_reason = blocked_reason
            elif not _older_than(path, int(DEFAULT_RETENTION_DAYS.get("data_foundation_min_age_days", 7)), now):
                blocked_reason = "retained_by_7_day_retention"
                protected_reason = blocked_reason
            candidates.append(
                _candidate(
                    path,
                    kind=kind,
                    reason=f"{description} Keep current production reference plus latest {keep_extra} extra; protect packages newer than 7 days.",
                    risk="medium" if profile == "safe" else "high",
                    profile=profile,
                    extra_protected=[],
                    blocked_reason_override=blocked_reason,
                    protected_reason=protected_reason,
                )
            )
    return candidates


def _collect_data_foundation_misc_backups(
    *,
    profile: str,
    now: datetime,
    days: int,
) -> list[CleanupCandidate]:
    root = DATA_FOUNDATION_ROOT / "backups"
    if not root.exists():
        return []
    busy_reason = _data_foundation_busy()
    fresh_cutoff = now - timedelta(hours=24)
    candidates: list[CleanupCandidate] = []
    for path in sorted([item for item in root.iterdir() if item.is_dir()], key=_mtime, reverse=True):
        modified = datetime.fromtimestamp(_mtime(path)) if _mtime(path) else None
        blocked_reason = ""
        if busy_reason:
            blocked_reason = busy_reason
        elif modified and modified >= fresh_cutoff:
            blocked_reason = "fresh_within_24h"
        elif not _older_than(path, days, now):
            blocked_reason = f"retained_by_{days}_day_retention"
        candidates.append(
            _candidate(
                path,
                kind="data_foundation_misc_backups",
                reason=f"Data foundation repair/diagnostic backup; current production paths are not read from this runtime backup root. Older than {days} days.",
                risk="medium",
                profile=profile,
                extra_protected=[],
                blocked_reason_override=blocked_reason,
                protected_reason=blocked_reason,
            )
        )
    return candidates


def build_cleanup_candidates(
    *,
    profile: str = "safe",
    retention_days: dict[str, int] | None = None,
) -> list[CleanupCandidate]:
    if profile not in {"safe", "aggressive"}:
        raise ValueError(f"unsupported cleanup profile: {profile}")
    policy = {**DEFAULT_RETENTION_DAYS, **(retention_days or {})}
    now = datetime.now()
    active_feature_refs = _active_feature_protection_map()
    model_registry_feature_refs = _model_registry_feature_refs()
    model_runtime_feature_refs = _model_runtime_feature_refs()
    extra_protected = list(
        {
            *active_feature_refs.keys(),
            *model_registry_feature_refs.keys(),
            *model_runtime_feature_refs.keys(),
        }
    )
    candidates = _collect_cache_dirs(extra_protected)

    for category in cleanup_categories():
        if profile not in category.profiles:
            continue
        days = int(policy.get(category.retention_key, 7))
        if category.name == "pickle_cache":
            candidates.extend(
                _collect_pickle_cache_files(
                    category.root,
                    days=days,
                    now=now,
                    risk=category.risk,
                    profile=profile,
                    extra_protected=extra_protected,
                )
            )
        elif category.name in {
            "reset_backups",
            "model_runs",
            "trading_prediction_features",
            "factor_value_repair",
            "model_quarantine",
            "factor_research_repair_backups",
            "model_archive",
            "model_official_qlib_isolation",
            "model_diagnostics",
            "model_audits",
            "model_diagnostic_recomputed_factors",
            "factor_parquet_archive",
        }:
            keep_latest = 1
            if category.name == "reset_backups":
                keep_latest = int(policy.get("reset_backups_keep_latest", 1))
            elif category.name == "model_runs":
                keep_latest = 2
            elif category.name == "trading_prediction_features":
                keep_latest = int(policy.get("trading_prediction_features_keep_latest", 1))
            elif category.name == "factor_value_repair":
                keep_latest = int(policy.get("factor_value_repair_keep_latest", 1))
            elif category.name == "model_quarantine":
                keep_latest = int(policy.get("model_quarantine_keep_latest", 1))
            elif category.name == "factor_research_repair_backups":
                keep_latest = int(policy.get("factor_research_repair_backups_keep_latest", 1))
            elif category.name in {
                "model_archive",
                "model_official_qlib_isolation",
                "model_diagnostics",
                "model_audits",
                "model_diagnostic_recomputed_factors",
                "factor_parquet_archive",
            }:
                keep_latest = 0
            candidates.extend(
                _collect_child_dirs(
                    category.root,
                    kind=category.name,
                    days=days,
                    now=now,
                    risk=category.risk,
                    profile=profile,
                    reason=f"{category.description} Older than {days} days.",
                    extra_protected=extra_protected,
                    keep_latest=keep_latest,
                    enforce_global_protection=category.name != "factor_parquet_archive",
                )
            )
        elif category.name == "retired_factor_values":
            candidates.extend(
                _collect_retired_factor_values(
                    profile=profile,
                    extra_protected=extra_protected,
                )
            )
        else:
            candidates.extend(
                _collect_file_tree(
                    category.root,
                    kind=category.name,
                    days=days,
                    now=now,
                    risk=category.risk,
                    profile=profile,
                    reason=f"{category.description} Older than {days} days.",
                    extra_protected=extra_protected,
                )
            )

    candidates.extend(
        _collect_data_foundation_dirs(
            profile,
            now,
            keep_extra=int(policy.get("data_foundation_keep_extra", 1)),
        )
    )
    candidates.extend(
        _collect_data_foundation_misc_backups(
            profile=profile,
            now=now,
            days=int(policy.get("data_foundation_misc_backups", 2)),
        )
    )

    if profile == "aggressive":
        candidates.extend(
            _collect_model_feature_sets(
                profile=profile,
                now=now,
                keep_latest=int(policy.get("feature_sets_keep_latest", 5)),
                active_refs=active_feature_refs,
                model_registry_refs=model_registry_feature_refs,
                model_runtime_refs=model_runtime_feature_refs,
            )
        )

    candidates.extend(
        _collect_runtime_test_tmp(
            profile=profile,
            now=now,
            days=int(policy.get("runtime_test_tmp", 1)),
        )
    )
    candidates.extend(
        _collect_runtime_task_tmp(
            profile=profile,
            now=now,
            days=int(policy.get("runtime_task_tmp", 1)),
        )
    )

    unique: dict[str, CleanupCandidate] = {}
    for item in candidates:
        unique[item.path] = item
    return sorted(unique.values(), key=lambda item: item.bytes, reverse=True)


def summarize_candidates(candidates: list[CleanupCandidate]) -> dict[str, Any]:
    executable = [item for item in candidates if item.executable]
    blocked = [item for item in candidates if not item.executable]
    by_kind: dict[str, dict[str, Any]] = {
        kind: {"count": 0, "bytes": 0, "human_size": "0 B"} for kind in SAFE_SUMMARY_KINDS
    }
    for item in candidates:
        bucket = by_kind.setdefault(item.kind, {"count": 0, "bytes": 0, "human_size": "0 B"})
        bucket["count"] += 1
        if item.executable:
            bucket["bytes"] += item.bytes
            bucket["human_size"] = format_bytes(bucket["bytes"])
    reclaimable = sum(item.bytes for item in executable)
    return {
        "candidate_count": len(candidates),
        "executable_count": len(executable),
        "blocked_count": len(blocked),
        "reclaimable_bytes": reclaimable,
        "reclaimable_human": format_bytes(reclaimable),
        "by_kind": by_kind,
        "top_candidates": [asdict(item) for item in executable[:30]],
        "blocked_candidates": [asdict(item) for item in blocked[:20]],
    }


def run_cleanup(
    *,
    profile: str = "safe",
    execute: bool = False,
    retention_days: dict[str, int] | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    candidates = build_cleanup_candidates(profile=profile, retention_days=retention_days)
    summary = summarize_candidates(candidates)
    deleted: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    if execute:
        for item in candidates:
            if not item.executable:
                continue
            path = Path(item.path)
            try:
                if path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
                deleted.append(asdict(item))
            except Exception as exc:
                errors.append({"path": item.path, "error": str(exc)})

    result = {
        "generated_at": utc_now(),
        "profile": profile,
        "dry_run": not execute,
        "executed": execute,
        "retention_days": {**DEFAULT_RETENTION_DAYS, **(retention_days or {})},
        "summary": summary,
        "deleted_count": len(deleted),
        "deleted_bytes": sum(item["bytes"] for item in deleted),
        "deleted_human": format_bytes(sum(item["bytes"] for item in deleted)),
        "deleted": deleted[:200],
        "errors": errors,
    }
    if write_report:
        CLEANUP_RUNS_ROOT.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = CLEANUP_RUNS_ROOT / f"cleanup_{stamp}_{profile}_{'execute' if execute else 'dry_run'}.json"
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        result["report_path"] = str(report_path)
        LATEST_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        LATEST_STATUS_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    else:
        result["report_path"] = None
    return result
