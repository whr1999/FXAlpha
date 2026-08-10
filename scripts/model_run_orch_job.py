#!/usr/bin/env python3
"""Run a model ORCH job from the command line."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from domain.model.orchestrator import orchestrator_start
from domain.model.feature_sets import feature_snapshot
from domain.model.state_store import ModelStateStore
from domain.model.walk_forward import start_production_rolling


def main() -> int:
    parser = argparse.ArgumentParser(description="Start a model ORCH training job.")
    parser.add_argument("--evaluation-mode", choices=("research", "production"), default="research")
    parser.add_argument("--feature-set-id", default="")
    parser.add_argument("--source-round-group-id", default="")
    parser.add_argument("--campaign-id", default="")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--max-stage", default="round_synthesis")
    parser.add_argument("--execute-qlib", action="store_true")
    parser.add_argument("--write-registry", action="store_true")
    parser.add_argument("--baseline-model-params-json", default="{}")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        baseline_model_params = json.loads(args.baseline_model_params_json or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid --baseline-model-params-json: {exc}") from exc
    if not isinstance(baseline_model_params, dict):
        raise SystemExit("--baseline-model-params-json must decode to an object")
    state = ModelStateStore()

    start_payload = {
        "event": "start",
        "job_id": args.job_id,
        "session_id": args.session_id,
        "feature_set_id": args.feature_set_id,
        "evaluation_mode": args.evaluation_mode,
        "rounds": args.rounds,
        "execute_qlib": args.execute_qlib,
        "write_registry": args.write_registry,
        "baseline_model_params": baseline_model_params,
        "checkpoint_stop_policy": "three_consecutive_non_improving_rounds",
    }
    print(json.dumps(start_payload, ensure_ascii=False), flush=True)
    try:
        if args.evaluation_mode == "production":
            result = start_production_rolling(
                args.source_round_group_id,
                state=state,
                write_registry=args.write_registry,
                campaign_id=args.campaign_id or None,
                job_id=args.job_id,
                resume=args.resume,
            )
            state.upsert_job(
                args.job_id,
                status=("interrupted" if result.get("status") == "interrupted" else ("completed" if result.get("ok") else "failed")),
                stage="production_rolling",
                mode="orch",
                payload={"result_summary": {"status": result.get("status"), "decision": result.get("decision"), "err": result.get("err")}},
            )
        else:
            feature_set_id = args.feature_set_id
            if not feature_set_id:
                snapshot = feature_snapshot()
                if not snapshot.get("ok") or not snapshot.get("feature_set_id"):
                    raise RuntimeError(f"feature_snapshot_failed:{snapshot}")
                feature_set_id = str(snapshot["feature_set_id"])
                state.upsert_job(
                    args.job_id,
                    status="running",
                    stage="feature_snapshot_preflight",
                    mode="orch",
                    payload={"feature_set_id": feature_set_id, "snapshot_mode": snapshot.get("mode")},
                )
            result = orchestrator_start(
                feature_set_id=feature_set_id,
                n_rounds=args.rounds,
                max_stage=args.max_stage,
                run_id=args.job_id,
                session_id=args.session_id,
                execute_qlib=args.execute_qlib,
                write_registry=args.write_registry,
                baseline_model_params=baseline_model_params,
                resume=args.resume,
                state=state,
            )
    except Exception as exc:
        state.upsert_job(
            args.job_id,
            status="failed",
            stage="blocker",
            mode="orch",
            payload={"error": str(exc), "traceback": traceback.format_exc()},
        )
        print(
            json.dumps(
                {
                    "event": "exception",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1

    finish_payload = {
        "event": "finish",
        "ok": result.get("ok"),
        "status": (result.get("job") or {}).get("status"),
        "stage": (result.get("job") or {}).get("stage"),
        "session_id": args.session_id,
        "job_id": args.job_id,
        "result": result,
    }
    print(json.dumps(finish_payload, ensure_ascii=False, default=str), flush=True)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
