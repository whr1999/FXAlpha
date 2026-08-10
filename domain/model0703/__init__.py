"""Read-only import compatibility for artifacts created under the old module name.

New code must import :mod:`domain.model`.  The aliases below exist only because
Python pickles and historical Qlib manifests persist fully-qualified module
paths and therefore cannot be renamed in place safely.
"""

from __future__ import annotations

import importlib
import sys

from domain.model import LEGACY_MODEL_SYSTEM_VERSIONS, MODEL_SYSTEM_VERSION

_COMPATIBLE_SUBMODULES = (
    "context",
    "contracts",
    "feature_set_builder",
    "feature_sets",
    "forward_test",
    "gate",
    "orchestrator",
    "paths",
    "preflight",
    "production_refit",
    "qlib_direct",
    "qlib_runner",
    "qlib_strategy",
    "registry_lineage",
    "research_confirmation",
    "reweight",
    "rolling_scoring",
    "scoring",
    "seed_worker",
    "state_store",
    "training_contract",
    "validation",
    "walk_forward",
    "window_config",
)

for _name in _COMPATIBLE_SUBMODULES:
    _module = importlib.import_module(f"domain.model.{_name}")
    sys.modules.setdefault(f"{__name__}.{_name}", _module)

sys.modules.setdefault(f"{__name__}.weighted_lgbm", sys.modules["domain.model.reweight"])

__all__ = ["MODEL_SYSTEM_VERSION", "LEGACY_MODEL_SYSTEM_VERSIONS"]
