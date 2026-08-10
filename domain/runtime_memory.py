from __future__ import annotations

import ctypes
import gc
import os
from typing import Any


def current_rss_mb() -> float | None:
    """Return current process RSS in MiB on Linux, or None when unavailable."""
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as fh:
            pages = int((fh.read().split() or ["0"])[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024, 2)
    except Exception:
        return None


def release_process_memory(reason: str = "") -> dict[str, Any]:
    """Best-effort cleanup for long-lived Python services after heavy pandas work."""
    before = current_rss_mb()
    collected = gc.collect()
    malloc_trim_ok: bool | None = None
    try:
        libc = ctypes.CDLL("libc.so.6")
        malloc_trim_ok = bool(libc.malloc_trim(0))
    except Exception:
        malloc_trim_ok = None
    after = current_rss_mb()
    return {
        "reason": reason,
        "rss_mb_before": before,
        "rss_mb_after": after,
        "gc_collected": collected,
        "malloc_trim_ok": malloc_trim_ok,
    }
