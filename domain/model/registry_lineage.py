from __future__ import annotations

from typing import Any

from .feature_set_builder import load_feature_set_manifest


def registry_lineage(feature_set_id: str, experiment: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map existing feature/window provenance into the registry's existing columns."""

    manifest = load_feature_set_manifest(feature_set_id) or {}
    experiment = experiment or {}
    segments = experiment.get("segments") if isinstance(experiment.get("segments"), dict) else {}
    train = segments.get("train") if isinstance(segments.get("train"), (list, tuple)) else []
    return {
        "feature_set_fingerprint": str(manifest.get("feature_set_fingerprint") or ""),
        "factor_ids": [str(value) for value in (manifest.get("factor_ids") or []) if value],
        "feature_count": int(manifest.get("feature_count") or manifest.get("factor_count") or 0),
        "train_start": str(train[0]) if len(train) >= 2 else "",
        "train_end": str(train[1]) if len(train) >= 2 else "",
    }


__all__ = ["registry_lineage"]
