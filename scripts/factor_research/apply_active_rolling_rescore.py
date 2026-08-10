#!/usr/bin/env python3
"""Apply active-factor rolling audit results and recompute official deep scores.

The script is dry-run by default. With --write it updates only the factor
metadata JSON blob; factor lifecycle status is intentionally left untouched.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from domain.factor_research import quality_gate  # noqa: E402
from storage.factor_registry import FactorRegistry  # noqa: E402


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _rolling_from_audit(row: dict[str, Any], *, source_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    existing = row.get("rolling_validation")
    if isinstance(existing, dict) and existing:
        return existing

    status = row.get("rolling_status")
    summary = {
        "status": status,
        "n_windows": row.get("n_windows"),
        "mean_test_ic": row.get("mean_test_ic"),
        "mean_test_ir": row.get("mean_test_ir"),
        "mean_train_ic": row.get("mean_train_ic"),
        "window_scheme": row.get("window_scheme") or {
            "train_months": config.get("train_months"),
            "valid_months": config.get("valid_months"),
            "test_months": config.get("test_months"),
            "step_months": config.get("step_months"),
            "min_dates_per_split": config.get("min_dates_per_split"),
            "holding_period": row.get("holding_period_days"),
        },
    }
    return {
        "status": status,
        "score": row.get("rolling_score"),
        "summary": summary,
        "decay_analysis": {
            "mean_decay": row.get("mean_decay"),
            "status": row.get("decay_status"),
        },
        "windows": row.get("windows") or [],
        "source": {
            "tool": "scripts/factor_research/run_active_rolling_audit.py",
            "audit_path": str(source_path),
            "applied_at": _utc_now(),
            "selection_start_date": config.get("selection_start_date"),
            "selection_end_date": config.get("selection_end_date"),
        },
    }


def _recompute_metadata(
    registry_row: dict[str, Any],
    rolling: dict[str, Any],
    *,
    source_path: Path,
    write: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    md = _metadata(registry_row)
    before_score = (
        (md.get("metrics") or {}).get("deep_score")
        if isinstance(md.get("metrics"), dict)
        else None
    )
    before_gate = md.get("gate_result") if isinstance(md.get("gate_result"), dict) else {}

    candidate = dict(registry_row)
    candidate.update(md)
    candidate["metadata"] = md
    candidate["rolling_validation"] = rolling
    if isinstance(candidate.get("deep_validation"), dict):
        candidate["deep_validation"]["rolling_validation"] = rolling

    adopted, rejected = quality_gate.apply_gate([candidate])
    updated = (adopted or rejected)[0]
    new_md = dict(md)
    new_md["rolling_validation"] = rolling
    if isinstance(updated.get("deep_validation"), dict):
        new_md["deep_validation"] = updated["deep_validation"]
        new_md["deep_validation"]["rolling_validation"] = rolling
    if isinstance(updated.get("gate_result"), dict):
        new_md["gate_result"] = updated["gate_result"]

    metrics = dict(new_md.get("metrics") or {})
    metrics["quick_score"] = updated.get("quick_score")
    metrics["deep_score"] = updated.get("deep_score")
    new_md["metrics"] = metrics
    new_md["evidence_schema_version"] = new_md.get("evidence_schema_version") or "fxalpha_evidence_v1"
    new_md["rolling_rescore"] = {
        "tool": "scripts/factor_research/apply_active_rolling_rescore.py",
        "source_audit_path": str(source_path),
        "applied_at": _utc_now() if write else None,
        "dry_run": not write,
        "scoring_policy": "quality_gate._compute_deep_score current contract",
        "before_deep_score": before_score,
        "after_deep_score": updated.get("deep_score"),
        "before_gate_passed": before_gate.get("passed"),
        "after_gate_passed": (updated.get("gate_result") or {}).get("passed"),
        "veto_reasons": updated.get("veto_reasons") or [],
    }

    result = {
        "factor_id": registry_row.get("factor_id"),
        "name": registry_row.get("name"),
        "before_deep_score": before_score,
        "after_deep_score": updated.get("deep_score"),
        "delta": round(float(updated.get("deep_score") or 0.0) - float(before_score or 0.0), 1)
        if before_score is not None
        else None,
        "rolling_score": rolling.get("score"),
        "gate_passed": (updated.get("gate_result") or {}).get("passed"),
        "veto_reasons": updated.get("veto_reasons") or [],
        "missing_components": (updated.get("deep_validation") or {}).get("score_parts", {}).get("missing_components") or [],
    }
    return new_md, result


def _backup_registry(registry: FactorRegistry) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = registry.db_path.with_suffix(registry.db_path.suffix + f".bak_rolling_rescore_{stamp}")
    shutil.copy2(registry.db_path, backup)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rolling-json", required=True, help="Output JSON from run_active_rolling_audit.py")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "runtime" / "factor_research" / "active_rolling_rescore_apply.json"))
    parser.add_argument("--write", action="store_true", help="Persist recomputed metadata to factor_registry.db")
    parser.add_argument("--no-backup", action="store_true", help="Skip SQLite backup when --write is used")
    args = parser.parse_args()

    rolling_path = Path(args.rolling_json).resolve()
    payload = _load_json(rolling_path)
    config = (payload.get("summary") or {}).get("config") or {}
    rows = payload.get("results") or []
    by_id = {str(row.get("factor_id")): row for row in rows if row.get("factor_id")}

    registry = FactorRegistry()
    active = registry.list_active(min_icir=-1e9)
    missing = sorted({str(row.get("factor_id")) for row in active} - set(by_id))
    if missing:
        raise RuntimeError(f"Rolling audit missing active factor ids: {missing[:10]}")

    backup_path = None
    if args.write and not args.no_backup:
        backup_path = _backup_registry(registry)

    results: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    for row in active:
        full = registry.get(str(row["factor_id"])) or row
        audit_row = by_id[str(row["factor_id"])]
        rolling = _rolling_from_audit(audit_row, source_path=rolling_path, config=config)
        new_md, result = _recompute_metadata(full, rolling, source_path=rolling_path, write=args.write)
        if args.write:
            registry.update_meta(str(row["factor_id"]), new_md)
        for reason in result.get("veto_reasons") or []:
            reason_counts[str(reason)] += 1
        results.append(result)

    summary = {
        "generated_at": _utc_now(),
        "write": bool(args.write),
        "active_count": len(active),
        "updated_count": len(results) if args.write else 0,
        "backup_path": str(backup_path) if backup_path else None,
        "gate_pass_count": sum(1 for item in results if item.get("gate_passed") is True),
        "gate_fail_count": sum(1 for item in results if item.get("gate_passed") is False),
        "missing_component_count": sum(1 for item in results if item.get("missing_components")),
        "reason_counts": dict(reason_counts),
        "rolling_json": str(rolling_path),
    }
    output = {"summary": summary, "results": results}
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"summary": summary, "output": str(out_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
