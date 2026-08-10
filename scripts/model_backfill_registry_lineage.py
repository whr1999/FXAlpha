#!/usr/bin/env python3
"""Fill existing model registry lineage columns from feature manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from domain.model.contracts import MODEL_SYSTEM_VERSION
from domain.model.registry_lineage import registry_lineage
from storage.model_registry import ModelRegistry


def _metadata(row: dict) -> dict:
    try:
        value = json.loads(row.get("metadata") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _experiment(row: dict) -> dict:
    path = Path(str(row.get("run_dir") or row.get("workspace_path") or "")) / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return dict(payload.get("experiment") or {})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes; default is preview only")
    args = parser.parse_args()
    registry = ModelRegistry()
    changes: list[dict] = []
    unresolved: list[dict] = []
    for row in registry.list_models("all"):
        if row.get("status") not in {"research", "candidate", "production"}:
            continue
        metadata = _metadata(row)
        if metadata.get("model_system_version") != MODEL_SYSTEM_VERSION:
            continue
        feature_set_id = str(row.get("feature_set_id") or metadata.get("feature_set_id") or "")
        lineage = registry_lineage(feature_set_id, _experiment(row))
        missing = [
            name
            for name, value in (
                ("feature_set_id", row.get("feature_set_id")),
                ("feature_set_fingerprint", row.get("feature_set_fingerprint")),
                ("factor_ids", json.loads(row.get("factor_ids") or "[]")),
                ("feature_count", row.get("feature_count")),
            )
            if not value
        ]
        if not missing:
            continue
        available = {
            "feature_set_id": feature_set_id,
            "feature_set_fingerprint": lineage.get("feature_set_fingerprint"),
            "factor_ids": lineage.get("factor_ids"),
            "feature_count": lineage.get("feature_count"),
        }
        fillable = [name for name in missing if available.get(name)]
        item = {"model_id": row.get("model_id"), "model_run_id": row.get("model_run_id"), "missing": missing, "fillable": fillable, "feature_set_id": feature_set_id}
        if not fillable:
            unresolved.append(item)
            continue
        changes.append(item)
        if args.apply:
            registry.update_run_result(
                model_run_id=str(row.get("model_run_id") or ""),
                feature_set_id=feature_set_id,
                status=str(row.get("status") or "research"),
                **lineage,
            )
    print(json.dumps({"ok": True, "apply": args.apply, "changed": len(changes), "unresolved": len(unresolved), "items": changes, "unresolved_items": unresolved}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
