#!/usr/bin/env python3
"""Read-only heartbeat monitor for FXAlpha factor-research orchestrator runs."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _get_json(base_url: str, path: str, params: dict[str, object] | None = None) -> dict:
    query = f"?{urlencode(params)}" if params else ""
    with urlopen(f"{base_url.rstrip('/')}{path}{query}", timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _event_summary(events_payload: dict) -> dict:
    outputs = events_payload.get("outputs") or {}
    events = outputs.get("events") or []
    latest = events[-1] if events else {}
    return {
        "event_count": len(events),
        "latest_ts": latest.get("ts"),
        "latest_run_id": latest.get("run_id"),
        "latest_stage": latest.get("stage"),
        "latest_stage_id": latest.get("stage_id"),
        "latest_checkpoint": latest.get("checkpoint"),
        "latest_heartbeat_status": latest.get("heartbeat_status"),
        "latest_summary": latest.get("summary"),
        "latest_decision": latest.get("decision"),
    }


def collect_once(base_url: str, run_id: str) -> dict:
    row: dict = {"ts": _now(), "run_id": run_id, "ok": True, "warnings": []}
    try:
        health = _get_json(base_url, "/health")
        preflight = _get_json(base_url, "/factor/research/preflight")
        status = _get_json(base_url, "/factor/status")
        active_values = _get_json(base_url, "/factor/active-values/status")
        model_status = _get_json(base_url, "/model/status")
        events = _get_json(
            base_url,
            "/factor/research/orchestrator-events",
            {"run_id": run_id, "limit": 1, "include_payload": "false"},
        )
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        row.update({"ok": False, "error": repr(exc)})
        return row

    outputs = status.get("outputs") or {}
    pipeline = outputs.get("pipeline") or {}
    registry = outputs.get("registry_summary") or {}
    preflight_outputs = preflight.get("outputs") or {}
    runtime_defaults = preflight_outputs.get("runtime_defaults") or {}
    active_values_outputs = active_values.get("outputs") or {}
    model_outputs = model_status.get("outputs") or {}

    row.update(
        {
            "health_ok": bool(health.get("ok")),
            "qgpt_ok": bool(preflight_outputs.get("qgpt_ok")),
            "can_start": bool(preflight_outputs.get("can_start")),
            "active_run_id": pipeline.get("active_run_id"),
            "overall_status": pipeline.get("overall_status") or outputs.get("status"),
            "active_stage": pipeline.get("active_stage"),
            "message": pipeline.get("message"),
            "registry_active": registry.get("active"),
            "registry_retired": registry.get("retired"),
            "default_universe": runtime_defaults.get("universe"),
            "active_values_stale": active_values_outputs.get("stale"),
            "active_values_refresh_required": active_values_outputs.get("active_values_refresh_required"),
            "model_refresh_required": active_values_outputs.get("model_refresh_required"),
            "active_values_registry_fingerprint": active_values_outputs.get("registry_fingerprint"),
            "model_status": model_outputs.get("status"),
            "model_feature_set_stale": model_outputs.get("feature_set_stale"),
            "model_registry_fingerprint": model_outputs.get("factor_registry_fingerprint"),
            "model_feature_fingerprint": model_outputs.get("active_feature_fingerprint")
            or model_outputs.get("active_feature_registry_fingerprint"),
            "event": _event_summary(events),
        }
    )

    if row["default_universe"] != "tradable_non_st":
        row["warnings"].append("default_universe_not_tradable_non_st")
    if row["active_run_id"] and row["active_run_id"] != run_id:
        row["warnings"].append("active_run_id_mismatch")
    if row["active_values_stale"] or row["active_values_refresh_required"] or row["model_refresh_required"]:
        row["warnings"].append("active_values_or_model_refresh_required")
    if row["overall_status"] in {"research_blocked", "failed", "error"}:
        row["warnings"].append("orchestrator_not_healthy")
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--api", default="http://127.0.0.1:18081")
    parser.add_argument("--interval-s", type=int, default=60)
    parser.add_argument("--max-samples", type=int, default=0, help="0 means run forever.")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    # Monitoring samples are diagnostics, not factor-research run state.
    output = Path(args.output) if args.output else Path("runtime/diagnostics/factor_research/orchestrator_heartbeat") / f"{args.run_id}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)

    sample = 0
    while True:
        sample += 1
        row = collect_once(args.api, args.run_id)
        row["sample"] = sample
        with output.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if args.max_samples and sample >= args.max_samples:
            return 0 if row.get("ok") else 1
        time.sleep(max(5, args.interval_s))


if __name__ == "__main__":
    sys.exit(main())
