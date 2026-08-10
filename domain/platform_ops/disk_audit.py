from __future__ import annotations

from datetime import datetime
from pathlib import Path
import copy
import subprocess
import threading
import time
from typing import Iterable

from storage.paths import (
    DATA_FOUNDATION_ROOT,
    FACTOR_DATA_ROOT,
    MODEL_DATA_ROOT,
    PROJECT_ROOT,
    QUANTGPT_CODE_ROOT,
    RUNTIME_ROOT,
)

KEY_PATHS: list[tuple[str, Path]] = [
    ("data", PROJECT_ROOT / "data"),
    ("runtime", RUNTIME_ROOT),
    ("data_foundation_runtime", DATA_FOUNDATION_ROOT),
    ("data_foundation_staging", DATA_FOUNDATION_ROOT / "staging"),
    ("data_foundation_production_backups", DATA_FOUNDATION_ROOT / "production_backups"),
    ("logs", PROJECT_ROOT / "log"),
    ("pickle_cache", PROJECT_ROOT / "pickle_cache"),
    ("quantgpt_engine", QUANTGPT_CODE_ROOT),
    ("quantgpt_reports", QUANTGPT_CODE_ROOT / "reports"),
    ("factor_assets", FACTOR_DATA_ROOT),
    ("model_assets", MODEL_DATA_ROOT),
    ("reset_backups", RUNTIME_ROOT / "reset_backups"),
]

_AUDIT_CACHE_TTL_SECONDS = 60.0
_AUDIT_CACHE_LOCK = threading.Lock()
_AUDIT_CACHE: dict | None = None
_AUDIT_CACHE_MONOTONIC = 0.0


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def safe_stat(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        stat = safe_stat(path)
        return int(stat.st_size) if stat else 0
    try:
        result = subprocess.run(
            ["du", "-sb", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.split()[0])
    except Exception:
        pass
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            stat = safe_stat(item)
            if stat:
                total += int(stat.st_size)
    return total


def path_summary(path: Path, *, known_sizes: dict[Path, int] | None = None) -> dict:
    exists = path.exists()
    resolved = path.resolve()
    if known_sizes is not None and resolved in known_sizes:
        size = int(known_sizes[resolved])
    else:
        size = path_size(path) if exists else 0
    stat = safe_stat(path) if exists else None
    return {
        "path": str(path),
        "exists": exists,
        "bytes": size,
        "human_size": format_bytes(size),
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat() if stat else None,
    }


def top_level_summaries(
    paths: Iterable[tuple[str, Path]],
    *,
    known_sizes: dict[Path, int] | None = None,
) -> list[dict]:
    items = []
    for name, path in paths:
        summary = path_summary(path, known_sizes=known_sizes)
        summary["name"] = name
        items.append(summary)
    return sorted(items, key=lambda item: item["bytes"], reverse=True)


def _single_pass_sizes() -> dict[Path, int]:
    """Collect project and governed key-path sizes with one filesystem walk."""
    result = subprocess.run(
        ["du", "-b", "--max-depth=3", str(PROJECT_ROOT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return {}
    sizes: dict[Path, int] = {}
    for raw_line in result.stdout.splitlines():
        size_text, separator, path_text = raw_line.partition("\t")
        if not separator:
            continue
        try:
            sizes[Path(path_text).resolve()] = int(size_text)
        except (OSError, ValueError):
            continue
    return sizes


def build_disk_audit(*, force_refresh: bool = False) -> dict:
    global _AUDIT_CACHE, _AUDIT_CACHE_MONOTONIC
    now_monotonic = time.monotonic()
    with _AUDIT_CACHE_LOCK:
        cache_age = now_monotonic - _AUDIT_CACHE_MONOTONIC
        if not force_refresh and _AUDIT_CACHE is not None and cache_age < _AUDIT_CACHE_TTL_SECONDS:
            cached = copy.deepcopy(_AUDIT_CACHE)
            cached["cache"] = {
                "hit": True,
                "age_seconds": round(cache_age, 3),
                "ttl_seconds": _AUDIT_CACHE_TTL_SECONDS,
            }
            return cached

        started = time.monotonic()
        try:
            known_sizes = _single_pass_sizes()
        except Exception:
            known_sizes = {}
        key_summaries = top_level_summaries(KEY_PATHS, known_sizes=known_sizes)
        if PROJECT_ROOT.resolve() in known_sizes:
            project_total_bytes = int(known_sizes[PROJECT_ROOT.resolve()])
        else:
            project_total_bytes = path_size(PROJECT_ROOT)
        payload = {
        "generated_at": utc_now(),
        "project_root": str(PROJECT_ROOT),
        "scan_duration_ms": round((time.monotonic() - started) * 1000, 1),
        "cache": {
            "hit": False,
            "age_seconds": 0.0,
            "ttl_seconds": _AUDIT_CACHE_TTL_SECONDS,
        },
        "project_total": {
            "path": str(PROJECT_ROOT),
            "exists": PROJECT_ROOT.exists(),
            "bytes": project_total_bytes,
            "human_size": format_bytes(project_total_bytes),
            "note": "deduplicated project scan; real project usage without summing overlapping parent/child key paths",
        },
        "top_level": key_summaries,
        "key_paths": key_summaries,
        }
        _AUDIT_CACHE = copy.deepcopy(payload)
        _AUDIT_CACHE_MONOTONIC = time.monotonic()
        return payload
