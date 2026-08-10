#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services import factor_research_service as service


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one durable FXAlpha factor ORCH worker.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--worker-unit", default="")
    args = parser.parse_args()
    run_id = str(args.run_id or "").strip()
    # Active-values refresh must be owned by the long-lived API process, not
    # by this target-bounded worker which exits immediately after completion.
    os.environ["FXALPHA_ORCHESTRATOR_WORKER"] = "1"
    launch = service._latest_orchestrator_launch_spec(run_id)
    inputs = launch.get("inputs") if isinstance(launch, dict) else None
    contract = launch.get("research_contract") if isinstance(launch, dict) else None
    if not isinstance(inputs, dict) or not isinstance(contract, dict):
        raise SystemExit(f"orchestrator_launch_spec_missing:{run_id}")

    unit = os.environ.get("SYSTEMD_UNIT", "")
    if os.environ.get("INVOCATION_ID") and not unit:
        unit = str(args.worker_unit or "")
    service._write_orchestrator_worker_event(
        run_id=run_id,
        action="started",
        unit=unit,
        pid=os.getpid(),
        mode="systemd_transient" if unit else "detached_process",
    )
    try:
        service._run_orchestrator_job(run_id, dict(inputs), dict(contract))
    finally:
        service._write_orchestrator_worker_event(
            run_id=run_id,
            action="exited",
            unit=unit,
            pid=os.getpid(),
            mode="systemd_transient" if unit else "detached_process",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
