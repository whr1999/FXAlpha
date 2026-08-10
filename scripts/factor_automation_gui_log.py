#!/usr/bin/env python3
"""Compatibility writer for Codex automation progress.

Production GUI progress now comes from
runtime/factor_research/research_steps/current.jsonl.  This script is kept only
so older heartbeat prompts do not fail; it translates start/event/finish calls
into research-step records and no longer writes runtime/factor_research/jobs.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.factor_research_service import factor_tool_record_research_step

JOBS_DIR = ROOT / "runtime" / "factor_research" / "jobs"
RESEARCH_STEPS_PATH = ROOT / "runtime" / "factor_research" / "research_steps" / "current.jsonl"
EVENT_LIMIT = 500
DEFAULT_AUTOMATION_ID = "fxalpha-factor-mining-follow-up"
AUTOMATION_SOURCES = {"codex_automation", "codex_heartbeat_automation", "codex_cron_automation"}
SUMMARY_ALIASES = {
    "quick_screened": "quick_screened_count",
    "quick_screened_total": "quick_screened_count",
    "quick_screened_count": "quick_screened_count",
    "novelty_checked": "novelty_checked_count",
    "novelty_checked_total": "novelty_checked_count",
    "novelty_checked_count": "novelty_checked_count",
    "deep_validation": "deep_validation_count",
    "deep_validation_total": "deep_validation_count",
    "deep_validation_count": "deep_validation_count",
    "deep_validation_count_total": "deep_validation_count",
    "quality_gate_adopted": "quality_gate_adopted_count",
    "quality_gate_adopted_total": "quality_gate_adopted_count",
    "quality_gate_adopted_count": "quality_gate_adopted_count",
    "valid_imports": "valid_imports",
    "valid_imports_total": "valid_imports",
    "import_count": "valid_imports",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def jsonable(value: Any) -> Any:
    if isinstance(value, deque):
        return list(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value


def safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value))


def job_path(run_id: str) -> Path:
    return JOBS_DIR / f"{safe_id(run_id)}.json"


def load_job(run_id: str) -> dict | None:
    path = job_path(run_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def find_active_job(automation_id: str) -> dict | None:
    if not JOBS_DIR.exists():
        return None
    jobs: list[dict] = []
    for path in sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:80]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("source") not in AUTOMATION_SOURCES:
            continue
        if (data.get("inputs") or {}).get("automation_id") != automation_id:
            continue
        if data.get("status") == "automation_running":
            jobs.append(data)
    return jobs[0] if jobs else None


def parse_extra(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def parse_metrics(items: list[str]) -> dict:
    metrics: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        try:
            metrics[key] = float(value) if "." in value else int(value)
        except Exception:
            metrics[key] = value
    return metrics


def numeric_value(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            number = float(value) if "." in value else int(value)
        except Exception:
            return None
        return number
    return None


def collect_summary_metrics(extra: Any) -> dict[str, int | float]:
    if not isinstance(extra, dict):
        return {}
    found: dict[str, int | float] = {}
    candidates: list[dict] = [extra]
    for key in ("counts", "summary", "metrics"):
        value = extra.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for payload in candidates:
        for raw_key, raw_value in payload.items():
            key = SUMMARY_ALIASES.get(str(raw_key))
            if not key:
                continue
            value = numeric_value(raw_value)
            if value is None:
                continue
            found[key] = max(value, found.get(key, 0))
    return found


def merge_summary(job: dict, metrics: dict[str, Any]) -> None:
    if not metrics:
        return
    summary = job.setdefault("summary", {})
    normalized = collect_summary_metrics(metrics)
    for key, value in normalized.items():
        current = numeric_value(summary.get(key)) or 0
        summary[key] = max(current, value)
    for key, value in metrics.items():
        normalized_key = SUMMARY_ALIASES.get(str(key), str(key))
        if normalized_key in SUMMARY_ALIASES.values():
            current = numeric_value(summary.get(normalized_key)) or 0
            number = numeric_value(value)
            if number is not None:
                summary[normalized_key] = max(current, number)
        else:
            summary[normalized_key] = value
    job.setdefault("metrics", {}).update(metrics)


def step_belongs_to_job(step: dict, run_id: str) -> bool:
    if not run_id:
        return False
    if str(step.get("run_id") or "") == run_id:
        return True
    refs = step.get("refs") or []
    if isinstance(refs, list) and run_id in {str(ref) for ref in refs}:
        return True
    extra = step.get("extra") or {}
    if isinstance(extra, dict) and str(extra.get("run_id") or "") == run_id:
        return True
    return False


def sync_summary_from_research_steps(job: dict) -> None:
    run_id = str(job.get("run_id") or "")
    if not run_id or not RESEARCH_STEPS_PATH.exists():
        return
    synced: dict[str, int | float] = {}
    try:
        lines = RESEARCH_STEPS_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for line in lines[-2000:]:
        try:
            step = json.loads(line)
        except Exception:
            continue
        if not isinstance(step, dict) or not step_belongs_to_job(step, run_id):
            continue
        for key, value in collect_summary_metrics(step.get("extra") or {}).items():
            synced[key] = max(value, synced.get(key, 0))
    if synced:
        merge_summary(job, synced)


def persist(job: dict) -> None:
    events = list(job.get("events") or [])[-EVENT_LIMIT:]
    job["events"] = events
    job["event_count"] = len(events)
    job["updated_at"] = now_iso()
    latest = job.get("latest_event") or {}
    status = str(job.get("status") or "")
    priority = "blocker" if status in {"automation_blocked", "automation_failed"} else "normal"
    summary = latest.get("message") or job.get("message") or "Codex automation progress updated."
    # Compatibility heartbeats are operational notes, not LLM research-stage
    # transitions.  Delegate the write to the governed service so current and
    # daily history remain atomic and bounded; never rewrite current.jsonl here.
    result = factor_tool_record_research_step(
        stage="note",
        summary=summary,
        decision=status,
        next_action=str(latest.get("event") or job.get("stage") or "compatibility_note"),
        refs=[str(job.get("run_id") or "")],
        priority=priority,
        run_id=str(job.get("run_id") or ""),
        evidence_refs=[
            {
                "source": "factor_automation_gui_log_compat",
                "automation_id": job.get("automation_id") or (job.get("inputs") or {}).get("automation_id"),
                "event": latest.get("event"),
            }
        ],
        tags=["compatibility", "automation"],
        extra={
            "source": "factor_automation_gui_log_compat",
            "job_files_deprecated": True,
            "automation_id": job.get("automation_id") or (job.get("inputs") or {}).get("automation_id"),
            "metrics": job.get("metrics") or {},
            "research_state": job.get("summary") or {},
            "inputs": job.get("inputs") or {},
        },
    )
    if not result.ok:
        raise RuntimeError(result.error or "compatibility_research_step_write_failed")


def append_event(job: dict, event: str, message: str, stage: str, extra: dict | None = None) -> None:
    enriched = {"ts": now_iso(), "stage": stage or event, "event": event, "message": message}
    if extra:
        enriched["extra"] = extra
    events = list(job.get("events") or [])
    events.append(enriched)
    job["events"] = events[-EVENT_LIMIT:]
    job["latest_event"] = enriched
    job["stage"] = stage or event
    job["message"] = message


def command_start(args: argparse.Namespace) -> dict:
    run_id = args.run_id or f"auto_{safe_id(args.automation_id)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    job = {
        "run_id": run_id,
        "status": "automation_running",
        "stage": args.stage or "automation_started",
        "started_at": now_iso(),
        "finished_at": None,
        "inputs": {
            "automation_id": args.automation_id,
            "orchestration_mode": "codex_native_mcp",
            "runtime_contract": "factor_mining_native_mcp_only",
            "native_mcp_tools": "unknown",
            "prompt_loaded": False,
            "knowledge_loaded": False,
            "invalid_glue_detected": False,
            "target_holding_period": args.holding_period,
            "universe": args.universe,
            "benchmark": args.benchmark,
        },
        "automation_id": args.automation_id,
        "message": args.message or "Codex automation started.",
        "metrics": {},
        "summary": {
            "quick_screened_count": 0,
            "novelty_checked_count": 0,
            "deep_validation_count": 0,
            "quality_gate_adopted_count": 0,
            "valid_imports": 0,
        },
        "latest_event": None,
        "events": [],
        "latest_result": None,
        "pending_guidance": [],
        "guidance_history": [],
        "source": "codex_heartbeat_automation",
    }
    append_event(job, "automation_started", args.message or "Codex automation started.", job["stage"], parse_extra(args.extra_json))
    persist(job)
    return job


def command_event(args: argparse.Namespace) -> dict:
    job = load_job(args.run_id) if args.run_id else find_active_job(args.automation_id)
    if not job:
        args.stage = args.stage or "automation_event_without_active_job"
        job = command_start(args)
    metrics = parse_metrics(args.metric)
    if metrics:
        merge_summary(job, metrics)
    extra = parse_extra(args.extra_json)
    if metrics:
        extra["metrics"] = metrics
    append_event(job, args.event or "automation_event", args.message or "Codex automation progress updated.", args.stage or args.event or "automation_event", extra)
    persist(job)
    return job


def command_finish(args: argparse.Namespace) -> dict:
    job = load_job(args.run_id) if args.run_id else find_active_job(args.automation_id)
    if not job:
        args.stage = args.stage or "automation_finished_without_active_job"
        job = command_start(args)
    metrics = parse_metrics(args.metric)
    if metrics:
        merge_summary(job, metrics)
    job["status"] = args.status
    job["finished_at"] = now_iso()
    extra = parse_extra(args.extra_json)
    if metrics:
        extra["metrics"] = metrics
    append_event(job, args.event or args.status, args.message or "Codex automation finished.", args.stage or args.status, extra)
    persist(job)
    return job


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "event", "finish"])
    parser.add_argument("--automation-id", default=DEFAULT_AUTOMATION_ID)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--stage", default="")
    parser.add_argument("--event", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--extra-json", default="")
    parser.add_argument("--metric", action="append", default=[])
    parser.add_argument("--status", default="automation_completed", choices=["automation_completed", "automation_blocked", "automation_failed"])
    parser.add_argument("--holding-period", type=int, default=5)
    parser.add_argument("--universe", default="all_market")
    parser.add_argument("--benchmark", default="hs300")
    args = parser.parse_args()

    if args.command == "start":
        job = command_start(args)
    elif args.command == "event":
        job = command_event(args)
    else:
        job = command_finish(args)
    print(json.dumps({"ok": True, "run_id": job.get("run_id"), "status": job.get("status"), "stage": job.get("stage")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
