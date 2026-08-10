from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from domain.model.validation import audit_seed_run
from storage.model_registry import ModelRegistry


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except Exception:
        return default


def _run_dir(row: dict[str, Any], metadata: dict[str, Any]) -> Path | None:
    refs = metadata.get("artifact_refs") if isinstance(metadata.get("artifact_refs"), dict) else {}
    for raw in [row.get("run_dir"), row.get("workspace_path"), refs.get("run_dir"), refs.get("artifact_dir")]:
        if raw:
            path = Path(str(raw))
            if path.exists():
                return path
    model_run_id = str(row.get("model_run_id") or metadata.get("model_run_id") or "")
    if model_run_id:
        path = Path("runtime/model/runs") / model_run_id
        if path.exists():
            return path
    return None


def _metrics(row: dict[str, Any], run_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    metrics = metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {}
    if not metrics:
        path = run_dir / "metrics.json"
        if path.exists():
            metrics = _loads(path.read_text(encoding="utf-8"), {})
    out = dict(metrics or {})
    for key in [
        "annualized_ret",
        "excess_annualized_ret_with_cost",
        "excess_information_ratio_with_cost",
        "max_drawdown",
        "rank_ic",
        "rank_icir",
        "strategy_annualized_ret",
        "strategy_sharpe",
    ]:
        if out.get(key) is None and row.get(key) is not None:
            out[key] = row.get(key)
    return out


def _registry_rows(registry: ModelRegistry, *, include_production: bool = False) -> list[dict[str, Any]]:
    statuses = ["candidate"] + (["production"] if include_production else [])
    rows: list[dict[str, Any]] = []
    for status in statuses:
        rows.extend(registry.list_models(status))
    filtered: list[dict[str, Any]] = []
    for row in rows:
        metadata = _loads(row.get("metadata"), {})
        if metadata.get("model_system_version") == "model":
            filtered.append(row)
    return filtered


def recompute_candidate_validation(*, include_production: bool = False, dry_run: bool = False) -> dict[str, Any]:
    registry = ModelRegistry()
    rows = _registry_rows(registry, include_production=include_production)
    results: list[dict[str, Any]] = []
    for row in rows:
        metadata = _loads(row.get("metadata"), {})
        run_dir = _run_dir(row, metadata)
        model_run_id = str(row.get("model_run_id") or metadata.get("model_run_id") or "")
        if run_dir is None:
            results.append({"model_run_id": model_run_id, "status": "skipped", "reason": "run_dir_missing"})
            continue
        metrics = _metrics(row, run_dir, metadata)
        audit = audit_seed_run(
            {
                "model_run_id": model_run_id,
                "round_group_id": metadata.get("round_group_id"),
                "seed": metadata.get("seed"),
                "metrics": metrics,
                "artifact_dir": str(run_dir),
            }
        )
        updated_metadata = {
            **metadata,
            "validation": audit,
            "validation_status": audit.get("status"),
            "validation_rule_version": audit.get("validation_rule_version"),
            "validation_hard_blocks": audit.get("hard_blocks") or [],
            "validation_warnings": audit.get("warnings") or [],
            "validation_artifact_path": audit.get("artifact_path") or str(run_dir / "validation_audit.json"),
        }
        if not dry_run:
            registry.update_run_result(
                model_run_id=model_run_id,
                metrics=metrics,
                run_dir=str(run_dir),
                workspace_path=str(run_dir),
                status=str(row.get("status") or "candidate"),
                metadata=updated_metadata,
            )
        style = (audit.get("checks") or {}).get("model_style_exposure") or {}
        tradability = (audit.get("checks") or {}).get("tradability_exposure") or {}
        results.append(
            {
                "model_id": row.get("model_id"),
                "model_run_id": model_run_id,
                "asset_status": row.get("status"),
                "status": audit.get("status"),
                "hard_blocks": audit.get("hard_blocks") or [],
                "warnings": audit.get("warnings") or [],
                "artifact_path": audit.get("artifact_path"),
                "st_top10": ((tradability.get("prediction") or {}).get("top10_avg_st_like_ratio")),
                "small_cap_top10": ((style.get("top10_prediction") or {}).get("avg_small_cap_ratio")),
                "blue_chip_top10": ((style.get("top10_prediction") or {}).get("avg_blue_chip_ratio")),
            }
        )
    return {
        "ok": True,
        "dry_run": dry_run,
        "include_production": include_production,
        "count": len(results),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute model validation for registry candidates.")
    parser.add_argument("--include-production", action="store_true", help="Also refresh production model validation.")
    parser.add_argument("--dry-run", action="store_true", help="Do not update registry metadata.")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    args = parser.parse_args()
    payload = recompute_candidate_validation(include_production=args.include_production, dry_run=args.dry_run)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
