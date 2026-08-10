#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domain.model.orchestrator import orchestrator_start


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run same-window model ORCH replay across explicit feature sets.")
    parser.add_argument("--feature-set-id", action="append", dest="feature_set_ids", required=True, help="Feature set id to replay. Repeat for 54/56/60 comparisons.")
    parser.add_argument("--rounds", type=int, default=1, help="Rounds per feature set.")
    parser.add_argument("--max-stage", default="experiment_plan", choices=["experiment_plan", "train_backtest_3seed", "score_review", "forward_test", "round_synthesis"], help="Stop stage for replay.")
    parser.add_argument("--execute-qlib", action="store_true", help="Run real qlib0627 workflow instead of shadow metrics.")
    parser.add_argument("--write-registry", action="store_true", help="Write production registry. Leave false for comparison dry runs.")
    parser.add_argument("--run-prefix", default="model_orch_replay", help="Run id prefix.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    outputs: list[dict[str, Any]] = []
    for idx, feature_set_id in enumerate(args.feature_set_ids, start=1):
        run_id = f"{args.run_prefix}_{idx}_{feature_set_id}"
        session_id = f"{args.run_prefix}_session_{idx}_{feature_set_id}"
        result = orchestrator_start(
            feature_set_id=feature_set_id,
            n_rounds=args.rounds,
            max_stage=args.max_stage,
            run_id=run_id,
            session_id=session_id,
            execute_qlib=args.execute_qlib,
            write_registry=args.write_registry,
        )
        outputs.append(
            {
                "feature_set_id": feature_set_id,
                "run_id": run_id,
                "session_id": session_id,
                "ok": bool(result.get("ok")),
                "err": result.get("err"),
                "completed_rounds": result.get("completed_rounds") or [],
                "job": {
                    "status": (result.get("job") or {}).get("status"),
                    "stage": (result.get("job") or {}).get("stage"),
                },
            }
        )
        if not result.get("ok"):
            break
    print(json.dumps({"ok": all(row["ok"] for row in outputs), "items": outputs}, ensure_ascii=False, indent=2))
    return 0 if all(row["ok"] for row in outputs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
