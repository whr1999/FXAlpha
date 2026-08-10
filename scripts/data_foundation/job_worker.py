#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path

from domain.data_foundation.runtime_io import atomic_write_json, read_json
from domain.data_foundation.tushare_daily import data_daily_routine
from domain.data_foundation.tushare_rebuild import TushareRebuildConfig, tushare_full_rebuild


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _update(path: Path, **updates) -> dict:
    payload = read_json(path, strict=True)
    payload.update(updates)
    payload["updated_at"] = _now()
    atomic_write_json(path, payload)
    return payload


def _heartbeat(path: Path, stop: threading.Event) -> None:
    while not stop.wait(15):
        try:
            _update(path, heartbeat_at=_now())
        except Exception:
            pass


def run(job_path: Path) -> int:
    job = read_json(job_path, strict=True)
    stop = threading.Event()
    _update(
        job_path,
        status="running",
        pid=os.getpid(),
        started_at=job.get("started_at") or _now(),
        heartbeat_at=_now(),
    )
    thread = threading.Thread(target=_heartbeat, args=(job_path, stop), daemon=True, name="fxalpha-data-heartbeat")
    thread.start()
    try:
        if job.get("mode") == "daily":
            result = data_daily_routine(
                target_date=job.get("target_date") or "auto",
                wait_idle=True,
                timeout_minutes=int(job.get("timeout_minutes") or 180),
                dry_run=bool(job.get("dry_run", True)),
            )
            ok = result.get("status") in {"completed", "dry_run", "already_current"}
        elif job.get("mode") == "full_rebuild":
            target = str(job.get("target_date") or "auto")
            if target == "auto":
                target = _now()[:10].replace("-", "")
            result = tushare_full_rebuild(
                TushareRebuildConfig(
                    cutoff_date=target.replace("-", ""),
                    dry_run=bool(job.get("dry_run", True)),
                )
            )
            ok = result.get("status") in {"completed", "dry_run"}
        else:
            raise ValueError(f"unsupported_data_update_mode:{job.get('mode')}")
        stop.set()
        thread.join(timeout=2)
        _update(
            job_path,
            status="completed" if ok else "failed",
            ok=ok,
            result=result,
            error=None if ok else f"data_job_incomplete:{result.get('status')}",
            finished_at=_now(),
            heartbeat_at=_now(),
        )
        return 0 if ok else 1
    except Exception as exc:
        stop.set()
        thread.join(timeout=2)
        _update(
            job_path,
            status="failed",
            ok=False,
            error=str(exc),
            finished_at=_now(),
            heartbeat_at=_now(),
        )
        return 1
    finally:
        stop.set()
        if thread.is_alive():
            thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-path", required=True)
    args = parser.parse_args()
    return run(Path(args.job_path).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
