from __future__ import annotations

from pathlib import Path

from storage.paths import MODEL_RUNTIME_ROOT as PLATFORM_MODEL_RUNTIME_ROOT
from storage.paths import QLIB_SOURCE_ROOT


MODEL_RUNTIME_ROOT = PLATFORM_MODEL_RUNTIME_ROOT
MODEL_JOBS_DB = MODEL_RUNTIME_ROOT / "jobs.sqlite"
MODEL_RESEARCH_STEPS = MODEL_RUNTIME_ROOT / "research_steps" / "current.jsonl"
MODEL_CONTEXT_SNAPSHOTS = MODEL_RUNTIME_ROOT / "context_snapshots"
MODEL_ORCHESTRATOR_EVENTS = MODEL_RUNTIME_ROOT / "orchestrator_events" / "current.jsonl"
MODEL_ORCHESTRATOR_TRACES = MODEL_RUNTIME_ROOT / "orchestrator_traces" / "current.jsonl"
MODEL_MCP_TRACES = MODEL_RUNTIME_ROOT / "mcp_traces" / "current.jsonl"
MODEL_RUNS_ROOT = MODEL_RUNTIME_ROOT / "runs"
MODEL_ACTIVE_PRODUCTION = MODEL_RUNTIME_ROOT / "active_production_model.json"
MODEL_ROLLING_ROOT = MODEL_RUNTIME_ROOT / "rolling"
MODEL_MANUAL_PROMOTION_AUDIT = MODEL_RUNTIME_ROOT / "manual_promotion_audit" / "current.jsonl"

# Compatibility alias retained for existing model manifests and callers.  New
# code resolves the pinned source from ``paths.qlib_source_root``.
QLIB0627_ROOT = QLIB_SOURCE_ROOT


def ensure_model_dirs(root: Path | None = None) -> dict[str, Path]:
    runtime_root = root or MODEL_RUNTIME_ROOT
    paths = {
        "runtime_root": runtime_root,
        "jobs_db": runtime_root / "jobs.sqlite",
        "research_steps": runtime_root / "research_steps" / "current.jsonl",
        "context_snapshots": runtime_root / "context_snapshots",
        "orchestrator_events": runtime_root / "orchestrator_events" / "current.jsonl",
        "orchestrator_traces": runtime_root / "orchestrator_traces" / "current.jsonl",
        "mcp_traces": runtime_root / "mcp_traces" / "current.jsonl",
        "runs_root": runtime_root / "runs",
    }
    for key, path in paths.items():
        if key == "jobs_db":
            path.parent.mkdir(parents=True, exist_ok=True)
        elif path.suffix == ".jsonl":
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    return paths
