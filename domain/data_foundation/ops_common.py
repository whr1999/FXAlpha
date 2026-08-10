from __future__ import annotations

import json
import hashlib
import os
import shutil
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from domain.data_foundation.runtime_io import atomic_write_bytes, atomic_write_json, read_json
from domain.data_foundation.update import _build_snapshot
from storage.paths import DATA_FOUNDATION_ROOT, PROJECT_ROOT, QUANTGPT_API_URL


PROMOTION_BACKUP_ROOT = DATA_FOUNDATION_ROOT / "production_backups"
DAILY_STATUS_FILE = DATA_FOUNDATION_ROOT / "daily_update_status.json"
PRODUCTION_LOCK_DIR = DATA_FOUNDATION_ROOT / "production_update.lock"
DATA_JOB_LOCK_DIR = DATA_FOUNDATION_ROOT / "data_job.lock"
MIN_DISK_BYTES = 80 * 1024**3
MIN_MEM_BYTES = 8 * 1024**3
REQUIRED_QLIB_INDEX_CODES = ["000300sh", "000905sh", "000852sh", "000001sh", "399001sz", "399006sz", "000016sh"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    return read_json(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _last_nonempty_line(path: Path) -> str | None:
    if not path.exists():
        return None
    latest = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            value = line.strip()
            if value:
                latest = value
    return latest


def _qlib_index_readiness(
    qlib_root: Path,
    *,
    expected_latest: str | None = None,
    required_codes: list[str] | None = None,
) -> dict[str, Any]:
    required = list(required_codes or REQUIRED_QLIB_INDEX_CODES)
    meta_path = qlib_root / "index_converter_meta.json"
    calendar_path = qlib_root / "calendars" / "day.txt"
    features_root = qlib_root / "features"
    meta = _read_json(meta_path)
    calendar_latest = _last_nonempty_line(calendar_path)
    meta_latest = meta.get("calendar_latest_date") or meta.get("source_latest_date")
    missing_codes: list[str] = []
    missing_close_files: list[str] = []
    issues: list[str] = []

    if not qlib_root.exists():
        issues.append("qlib_root_missing")
    if not meta_path.exists():
        issues.append("qlib_index_meta_missing")
    if not calendar_latest:
        issues.append("qlib_calendar_missing_or_empty")
    if expected_latest and calendar_latest != expected_latest:
        issues.append("qlib_calendar_latest_mismatch")
    if expected_latest and meta_latest and str(meta_latest) != expected_latest:
        issues.append("qlib_index_meta_latest_mismatch")
    if meta_path.exists():
        if meta.get("price_mode") != "index_raw_close_identity_adjusted":
            issues.append("qlib_index_price_mode_missing_or_invalid")
        if meta.get("change_field") != "pct_chg_decimal":
            issues.append("qlib_index_change_field_missing_or_invalid")
        if meta.get("factor_field") != "constant_one_when_missing":
            issues.append("qlib_index_factor_field_missing_or_invalid")

    for code in required:
        code_dir = features_root / code
        close_file = code_dir / "close.day.bin"
        if not code_dir.is_dir():
            missing_codes.append(code)
            continue
        if not close_file.exists() or close_file.stat().st_size <= 0:
            missing_close_files.append(code)
    if missing_codes:
        issues.append("qlib_index_feature_dirs_missing")
    if missing_close_files:
        issues.append("qlib_index_close_files_missing")

    return {
        "status": "passed" if not issues else "failed",
        "qlib_root": str(qlib_root),
        "meta_path": str(meta_path),
        "calendar_latest_date": calendar_latest,
        "meta_latest_date": meta_latest,
        "required_codes": required,
        "missing_codes": missing_codes,
        "missing_close_files": missing_close_files,
        "issues": issues,
    }


def _write_daily_status(payload: dict[str, Any]) -> None:
    previous = _read_json(DAILY_STATUS_FILE)
    enriched = dict(payload)
    if enriched.get("status") == "promoted":
        enriched.setdefault("last_successful_promotion", dict(enriched))
    elif previous.get("last_successful_promotion"):
        enriched["last_successful_promotion"] = previous["last_successful_promotion"]
    elif previous.get("status") == "promoted":
        enriched["last_successful_promotion"] = previous
    _write_json(DAILY_STATUS_FILE, enriched)


def _target_date_iso(target_date: str | None) -> str:
    text = str(target_date or "auto").strip().lower()
    if text in {"", "auto"}:
        text = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    text = text.replace("-", "")
    if not (text.isdigit() and len(text) == 8):
        raise ValueError(f"target_date must be auto or YYYYMMDD: {target_date}")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _process_rss_bytes(entry: Path) -> int:
    try:
        for line in (entry / "status").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1]) * 1024
    except Exception:
        return 0
    return 0


def _top_memory_processes(proc_root: Path = Path("/proc"), *, limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not proc_root.exists():
        return rows
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            raw = (entry / "cmdline").read_bytes()
            cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
            if not cmd:
                cmd = (entry / "comm").read_text(encoding="utf-8", errors="ignore").strip()
            rss_bytes = _process_rss_bytes(entry)
            if rss_bytes <= 0:
                continue
            lower_cmd = cmd.lower()
            suspected_stale_helper = "quantgpt" in lower_cmd and "--transport" in lower_cmd and "stdio" in lower_cmd
            cwd = os.readlink(entry / "cwd") if (entry / "cwd").exists() else ""
            row: dict[str, Any] = {
                "pid": pid,
                "rss_bytes": int(rss_bytes),
                "rss_mb": round(rss_bytes / 1024**2, 1),
                "cwd": cwd,
                "cmd": cmd[:500],
                "suspected_stale_helper": suspected_stale_helper,
            }
            if suspected_stale_helper:
                row["hint"] = "quantgpt_stdio_may_be_stale"
            rows.append(row)
        except Exception:
            continue
    rows.sort(key=lambda item: int(item.get("rss_bytes") or 0), reverse=True)
    return rows[: max(1, int(limit))]


def _disk_and_memory(
    *,
    meminfo_path: Path = Path("/proc/meminfo"),
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    disk = shutil.disk_usage(PROJECT_ROOT)
    mem_available = None
    mem_total = None
    if meminfo_path.exists():
        values: dict[str, int] = {}
        for line in meminfo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                values[parts[0].rstrip(":")] = int(parts[1]) * 1024
        mem_available = values.get("MemAvailable")
        mem_total = values.get("MemTotal")
    mem_ok = mem_available is None or mem_available >= MIN_MEM_BYTES
    report = {
        "disk_free_bytes": int(disk.free),
        "disk_total_bytes": int(disk.total),
        "disk_ok": disk.free >= MIN_DISK_BYTES,
        "mem_available_bytes": mem_available,
        "mem_total_bytes": mem_total,
        "mem_ok": mem_ok,
        "min_disk_bytes": MIN_DISK_BYTES,
        "min_mem_bytes": MIN_MEM_BYTES,
    }
    if not mem_ok:
        top_processes = _top_memory_processes(proc_root=proc_root)
        report["top_memory_processes"] = top_processes
        report["suspected_stale_helpers"] = [
            process for process in top_processes if process.get("suspected_stale_helper")
        ]
    return report


def _proc_matches() -> list[dict[str, Any]]:
    keywords = [
        "cli.py data-daily-routine",
        "cli.py data-stage-update",
        "cli.py data-promote-staged",
        "cli.py data-tushare-full-rebuild",
        "cli.py data-tushare-prepare-production",
        "cli.py data-tushare-promote-staged",
        "scripts/data_foundation/job_worker.py",
        "cli.py factor",
        "cli.py model",
        "pipeline-run",
        "model_train",
        "factor_research",
        "daily_data_update",
        "download_update",
        "convert_to_qlib",
    ]
    rows: list[dict[str, Any]] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return rows
    own_process_tree = {os.getpid()}
    cursor = os.getpid()
    while cursor > 1:
        try:
            fields = (proc_root / str(cursor) / "stat").read_text(encoding="utf-8", errors="replace").split()
            cursor = int(fields[3])
        except Exception:
            break
        own_process_tree.add(cursor)
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            if not raw:
                continue
            cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
            if not cmd or int(entry.name) in own_process_tree:
                continue
            # The long-lived QuantGPT stdio server owns worker children even
            # while idle. Its authoritative active-task count is checked via
            # the health endpoint, so process presence alone is not a blocker.
            if any(k in cmd for k in keywords):
                cwd = os.readlink(entry / "cwd") if (entry / "cwd").exists() else ""
                rows.append({"pid": int(entry.name), "cwd": cwd, "cmd": cmd})
        except Exception:
            continue
    return rows


def _pid_start_ticks(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace").split()
        return int(fields[21])
    except Exception:
        return None


def _quantgpt_health() -> dict[str, Any]:
    url = f"{QUANTGPT_API_URL.rstrip()}/api/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "url": url, "payload": payload}
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def _lock_owner(lock_dir: Path) -> dict[str, Any]:
    owner = _read_json(lock_dir / "owner.json")
    pid = owner.get("pid")
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
            owner["alive"] = True
        except ProcessLookupError:
            owner["alive"] = False
        except PermissionError:
            owner["alive"] = True
        expected_start = owner.get("pid_start_ticks")
        actual_start = _pid_start_ticks(pid)
        if owner.get("alive") and expected_start is not None and actual_start != expected_start:
            owner["alive"] = False
            owner["stale_reason"] = "pid_reused"
        owner["actual_pid_start_ticks"] = actual_start
    return owner


def _acquire_lock(lock_dir: Path, *, owner: dict[str, Any] | None = None) -> dict[str, Any]:
    requested_owner = dict(owner or {})
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_dir.mkdir()
        payload = {
            "pid": os.getpid(),
            "pid_start_ticks": _pid_start_ticks(os.getpid()),
            "started_at": _now(),
            "heartbeat_at": _now(),
            "command": " ".join(os.sys.argv),
            **requested_owner,
        }
        _write_json(lock_dir / "owner.json", payload)
        return payload
    except FileExistsError as exc:
        current_owner = _lock_owner(lock_dir)
        if current_owner and current_owner.get("alive") is False:
            shutil.rmtree(lock_dir, ignore_errors=True)
            return _acquire_lock(lock_dir, owner=requested_owner)
        raise RuntimeError(f"lock exists: {lock_dir} owner={current_owner}") from exc


def _refresh_lock(lock_dir: Path, **updates: Any) -> dict[str, Any]:
    owner = _lock_owner(lock_dir)
    if not owner or owner.get("pid") != os.getpid() or owner.get("pid_start_ticks") != _pid_start_ticks(os.getpid()):
        raise RuntimeError(f"lock_not_owned:{lock_dir}")
    owner.pop("alive", None)
    owner.pop("actual_pid_start_ticks", None)
    owner.update(updates)
    owner["heartbeat_at"] = _now()
    _write_json(lock_dir / "owner.json", owner)
    return owner


@contextmanager
def _data_job_lease(mode: str):
    current = _lock_owner(DATA_JOB_LOCK_DIR) if DATA_JOB_LOCK_DIR.exists() else {}
    same_process = bool(
        current
        and current.get("pid") == os.getpid()
        and current.get("pid_start_ticks") == _pid_start_ticks(os.getpid())
    )
    acquired = False
    if same_process:
        _refresh_lock(DATA_JOB_LOCK_DIR, mode=mode)
    else:
        _acquire_lock(DATA_JOB_LOCK_DIR, owner={"mode": mode})
        acquired = True
    try:
        yield _lock_owner(DATA_JOB_LOCK_DIR)
    finally:
        if acquired:
            _release_lock(DATA_JOB_LOCK_DIR)


def data_job_guard(mode: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with _data_job_lease(mode):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def _release_lock(lock_dir: Path) -> None:
    shutil.rmtree(lock_dir, ignore_errors=True)


def _replace_path(src: Path, dest: Path, backup_root: Path, replaced: list[tuple[Path, Path, bool]]) -> None:
    if _path_equivalent(src, dest):
        return
    backup = backup_root / dest.relative_to(PROJECT_ROOT)
    backup.parent.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    working = dest.with_name(f".{dest.name}.promote-{os.getpid()}-{backup_root.name}")
    if working.exists():
        if working.is_dir():
            shutil.rmtree(working)
        else:
            working.unlink()
    # Materialize and close the new artifact while the current production path
    # remains readable. The visible-path gap is then limited to two local
    # renames, rather than the duration of a potentially large copy.
    try:
        if src.is_dir():
            shutil.copytree(src, working)
        else:
            shutil.copy2(src, working)
    except Exception:
        if working.exists():
            if working.is_dir():
                shutil.rmtree(working, ignore_errors=True)
            else:
                working.unlink(missing_ok=True)
        raise
    had_destination = dest.exists()
    try:
        if had_destination:
            if backup.exists():
                if backup.is_dir():
                    shutil.rmtree(backup)
                else:
                    backup.unlink()
            shutil.move(str(dest), str(backup))
        replaced.append((dest, backup, had_destination))
        os.replace(working, dest)
    except Exception:
        if working.exists():
            if working.is_dir():
                shutil.rmtree(working, ignore_errors=True)
            else:
                working.unlink(missing_ok=True)
        raise


def _file_equivalent(src: Path, dest: Path) -> bool:
    try:
        if not src.is_file() or not dest.is_file():
            return False
        if src.stat().st_size != dest.stat().st_size:
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


def _dir_signature(root: Path) -> tuple[int, int, str] | None:
    try:
        digest = hashlib.sha256()
        count = 0
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            filenames.sort()
            base = Path(dirpath)
            for filename in filenames:
                path = base / filename
                rel = path.relative_to(root).as_posix()
                stat = path.stat()
                count += 1
                total_size += stat.st_size
                digest.update(rel.encode("utf-8", errors="surrogateescape"))
                digest.update(b"\0")
                digest.update(str(stat.st_size).encode("ascii"))
                digest.update(b"\0")
                with path.open("rb") as fh:
                    while True:
                        chunk = fh.read(8 * 1024**2)
                        if not chunk:
                            break
                        digest.update(chunk)
        return count, total_size, digest.hexdigest()
    except OSError:
        return None


def _dir_equivalent_quick(src: Path, dest: Path) -> bool:
    if not src.is_dir() or not dest.is_dir():
        return False
    src_sig = _dir_signature(src)
    dest_sig = _dir_signature(dest)
    return src_sig is not None and src_sig == dest_sig


def _path_equivalent(src: Path, dest: Path) -> bool:
    if not src.exists() or not dest.exists():
        return False
    if src.is_file():
        return _file_equivalent(src, dest)
    if src.is_dir():
        return _dir_equivalent_quick(src, dest)
    return False


def _promote_qlib_market_data(
    src_root: Path,
    dest_root: Path,
    backup_root: Path,
    replaced: list[tuple[Path, Path, bool]],
) -> None:
    readiness = _qlib_index_readiness(src_root)
    if readiness.get("status") != "passed":
        issues = ",".join(str(item) for item in readiness.get("issues", []))
        raise RuntimeError(f"qlib_index_artifacts_not_ready:{issues}")
    for name in ["features", "calendars", "instruments"]:
        _replace_path(src_root / name, dest_root / name, backup_root, replaced)
    for name in ["stock_converter_meta.json", "index_converter_meta.json"]:
        src = src_root / name
        if src.exists():
            _replace_path(src, dest_root / name, backup_root, replaced)


def _rollback(replaced: list[tuple[Path, Path, bool]]) -> dict[str, Any]:
    restored: list[str] = []
    errors: list[dict[str, str]] = []
    for dest, backup, had_destination in reversed(replaced):
        try:
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            if had_destination and backup.exists():
                shutil.move(str(backup), str(dest))
                restored.append(str(dest))
            elif had_destination:
                errors.append({"dest": str(dest), "backup": str(backup), "error": "backup_missing"})
            else:
                restored.append(str(dest))
        except Exception as exc:
            errors.append({"dest": str(dest), "backup": str(backup), "error": str(exc)})
    return {"status": "passed" if not errors else "failed", "restored": restored, "errors": errors}


def _snapshot_state_files(paths: list[Path]) -> dict[Path, bytes | None]:
    return {Path(path): Path(path).read_bytes() if Path(path).exists() else None for path in paths}


def _restore_state_files(snapshot: dict[Path, bytes | None]) -> dict[str, Any]:
    restored: list[str] = []
    errors: list[dict[str, str]] = []
    for path, content in snapshot.items():
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_bytes(path, content)
            restored.append(str(path))
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return {"status": "passed" if not errors else "failed", "restored": restored, "errors": errors}
