from __future__ import annotations

import argparse
import json
import os
import traceback
from pathlib import Path
from typing import Any


for _key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "LIGHTGBM_NUM_THREADS"):
    os.environ.setdefault(_key, "1")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one model Qlib seed in an isolated worker process.")
    parser.add_argument("--feature-set-id", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--experiment-json", required=True)
    parser.add_argument("--result-json", required=True)
    args = parser.parse_args()

    result_path = Path(args.result_json)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        experiment = json.loads(Path(args.experiment_json).read_text(encoding="utf-8"))
        from .qlib_direct import run_direct_qlib_seed

        result = run_direct_qlib_seed(
            feature_set_id=args.feature_set_id,
            experiment=experiment,
            seed=int(args.seed),
            run_dir=Path(args.run_dir),
            debug=experiment.get("execution_debug") or {},
        )
        result_path.write_text(
            json.dumps({"ok": True, "result": _jsonable(result)}, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return 0
    except Exception as exc:
        result_path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc()[-8000:],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
