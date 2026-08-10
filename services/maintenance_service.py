from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from domain.platform_ops.cleanup_executor import LATEST_STATUS_FILE, run_cleanup
from domain.platform_ops.disk_audit import build_disk_audit
from domain.platform_ops.service_health import http_json_health
from services._base import err_result, ok_result
from storage.paths import QUANTGPT_API_URL


def _preview_freshness(preview: dict[str, Any]) -> dict[str, Any]:
    generated_at = str(preview.get("generated_at") or "")
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))
        return {
            "cleanup_preview_generated_at": generated_at,
            "cleanup_preview_age_seconds": age_seconds,
            "cleanup_preview_stale": age_seconds > 900,
        }
    except Exception:
        return {
            "cleanup_preview_generated_at": generated_at or None,
            "cleanup_preview_age_seconds": None,
            "cleanup_preview_stale": True,
        }


def maintenance_status(*, include_disk_audit: bool = True) -> Any:
    try:
        audit = (
            build_disk_audit()
            if include_disk_audit
            else {
                "status": "deferred",
                "mode": "snapshot_only",
                "reason": "deep_disk_audit_not_requested",
                "deep_status_hint": "/maintenance/status?deep=true",
            }
        )
        if LATEST_STATUS_FILE.exists():
            preview = json.loads(LATEST_STATUS_FILE.read_text(encoding="utf-8"))
        else:
            preview = {
                "profile": "safe",
                "dry_run": True,
                "executed": False,
                "summary": {
                    "candidate_count": 0,
                    "executable_count": 0,
                    "reclaimable_bytes": 0,
                    "reclaimable_human": "0 B",
                    "by_kind": {},
                    "top_candidates": [],
                },
                "report_path": None,
                "note": "Run POST /maintenance/cleanup with execute=false to build the first cleanup preview.",
            }
        freshness = _preview_freshness(preview)
        qgpt_health = http_json_health(f"{QUANTGPT_API_URL}/api/v1/health")
        return ok_result(
            outputs={
                "status": "ready",
                "disk_audit": audit,
                "disk_audit_mode": "deep" if include_disk_audit else "snapshot_only",
                "cleanup_preview": preview,
                **freshness,
                "service_health": {
                    "quantgpt": qgpt_health,
                },
            },
            artifacts={
                "latest_cleanup_report": preview.get("report_path"),
            },
        )
    except Exception as exc:
        return err_result("maintenance_status_failed", outputs={"detail": str(exc)})


def maintenance_cleanup(
    *,
    profile: str = "safe",
    execute: bool = False,
    retention_days: dict[str, int] | None = None,
) -> Any:
    try:
        result = run_cleanup(profile=profile, execute=execute, retention_days=retention_days)
        return ok_result(
            inputs={
                "profile": profile,
                "execute": execute,
                "retention_days": retention_days or {},
            },
            outputs=result,
            artifacts={"report_path": result.get("report_path")},
            warnings=[] if not execute else ["cleanup_executed"],
        )
    except ValueError as exc:
        return err_result("invalid_cleanup_request", inputs={"profile": profile}, outputs={"detail": str(exc)})
    except Exception as exc:
        return err_result("maintenance_cleanup_failed", outputs={"detail": str(exc)})
