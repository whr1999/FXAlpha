from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from storage.paths import (
    ACTIVE_MODEL_FEATURE_SET_FILE,
    FACTOR_ADOPTED_VALUES_FILE,
    FACTOR_DATA_ROOT,
    FACTOR_REGISTRY_DB,
    MODEL_ACTIVE_FEATURE_DIR,
    MODEL_DATA_ROOT,
    MODEL_REGISTRY_DB,
    MODEL_RUNTIME_ROOT,
    PROJECT_ROOT,
    PREDICTION_FEATURE_RUNTIME_ROOT,
    QLIB_DATA_ROOT,
    QUANTGPT_CODE_ROOT,
    QUANTGPT_DATA_DIR,
    QUANTGPT_RESEARCH_NOTES_DIR,
    RUNTIME_ROOT,
)


DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "pycache": 0,
    "pytest_cache": 0,
    "pickle_cache": 1,
    "logs": 3,
    "quantgpt_reports": 7,
    "reset_backups": 7,
    "reset_backups_keep_latest": 1,
    "data_foundation_keep_extra": 2,
    "data_foundation_min_age_days": 7,
    "data_foundation_misc_backups": 2,
    "trading_prediction_features": 30,
    "trading_prediction_features_keep_latest": 1,
    "model_runs": 30,
    "feature_sets_keep_latest": 5,
    "factor_value_repair": 2,
    "factor_value_repair_keep_latest": 1,
    "model_quarantine": 2,
    "model_quarantine_keep_latest": 1,
    "model_archive": 30,
    "model_official_qlib_isolation": 7,
    "model_diagnostics": 30,
    "model_audits": 7,
    "model_diagnostic_recomputed_factors": 7,
    "factor_parquet_archive": 7,
    "retired_factor_values": 0,
    "factor_research_trace_history": 30,
    "factor_research_repair_backups": 7,
    "factor_research_repair_backups_keep_latest": 1,
    "factor_research_registry_backups": 14,
    "factor_research_event_history": 30,
    "runtime_test_tmp": 1,
    "runtime_task_tmp": 1,
}


@dataclass(frozen=True)
class CleanupCategory:
    name: str
    root: Path
    retention_key: str
    risk: str
    profiles: tuple[str, ...]
    description: str


def _p(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def project_path(*parts: str) -> Path:
    return (PROJECT_ROOT / Path(*parts)).resolve()


def cleanup_categories() -> list[CleanupCategory]:
    return [
        CleanupCategory(
            name="pickle_cache",
            root=project_path("pickle_cache"),
            retention_key="pickle_cache",
            risk="medium",
            profiles=("safe", "aggressive"),
            description="Regenerable experiment pickle/zip cache files.",
        ),
        CleanupCategory(
            name="quantgpt_reports",
            root=QUANTGPT_CODE_ROOT / "reports",
            retention_key="quantgpt_reports",
            risk="low",
            profiles=("safe", "aggressive"),
            description="QuantGPT HTML backtest reports; regenerable from expressions.",
        ),
        CleanupCategory(
            name="logs",
            root=project_path("log"),
            retention_key="logs",
            risk="low",
            profiles=("safe", "aggressive"),
            description="Runtime logs and third-party training logs.",
        ),
        CleanupCategory(
            name="reset_backups",
            root=RUNTIME_ROOT / "reset_backups",
            retention_key="reset_backups",
            risk="medium",
            profiles=("safe", "aggressive"),
            description="Manual reset backups; useful for audit but not production inputs.",
        ),
        CleanupCategory(
            name="trading_prediction_features",
            root=PREDICTION_FEATURE_RUNTIME_ROOT,
            retention_key="trading_prediction_features",
            risk="medium",
            profiles=("aggressive",),
            description="Old generated trading prediction feature snapshots.",
        ),
        CleanupCategory(
            name="factor_value_repair",
            root=RUNTIME_ROOT / "factor_research" / "value_repair",
            retention_key="factor_value_repair",
            risk="medium",
            profiles=("safe", "aggressive"),
            description="Completed factor value repair staging and bad-data backup runs.",
        ),
        CleanupCategory(
            name="model_quarantine",
            root=MODEL_RUNTIME_ROOT / "quarantine",
            retention_key="model_quarantine",
            risk="medium",
            profiles=("safe", "aggressive"),
            description="Old quarantined bad model feature snapshots and manifests.",
        ),
        CleanupCategory(
            name="model_archive",
            root=MODEL_RUNTIME_ROOT / "archive",
            retention_key="model_archive",
            risk="medium",
            profiles=("aggressive",),
            description="Archived model research feature snapshots and frozen experiment copies.",
        ),
        CleanupCategory(
            name="model_official_qlib_isolation",
            root=MODEL_RUNTIME_ROOT / "official_qlib0627_isolation",
            retention_key="model_official_qlib_isolation",
            risk="medium",
            profiles=("safe", "aggressive"),
            description="Isolated official-Qlib model research scratch runs and recomputed cache datasets.",
        ),
        CleanupCategory(
            name="model_diagnostics",
            root=MODEL_RUNTIME_ROOT / "diagnostics",
            retention_key="model_diagnostics",
            risk="medium",
            profiles=("aggressive",),
            description="Model diagnostic scratch outputs and staging datasets.",
        ),
        CleanupCategory(
            name="model_audits",
            root=MODEL_RUNTIME_ROOT / "audits",
            retention_key="model_audits",
            risk="medium",
            profiles=("safe", "aggressive"),
            description="Model audit experiment outputs; summary JSON/CSV can be regenerated from reruns.",
        ),
        CleanupCategory(
            name="model_diagnostic_recomputed_factors",
            root=MODEL_RUNTIME_ROOT / "diagnostic_recomputed_factors",
            retention_key="model_diagnostic_recomputed_factors",
            risk="medium",
            profiles=("safe", "aggressive"),
            description="Diagnostic recomputed factor-value scratch panels from model research.",
        ),
        CleanupCategory(
            name="factor_parquet_archive",
            root=FACTOR_DATA_ROOT / "parquet" / "archive",
            retention_key="factor_parquet_archive",
            risk="medium",
            profiles=("safe", "aggressive"),
            description="Archived retired or superseded single-factor parquet values outside the live parquet root.",
        ),
        CleanupCategory(
            name="retired_factor_values",
            root=FACTOR_DATA_ROOT / "parquet",
            retention_key="retired_factor_values",
            risk="medium",
            profiles=("safe", "aggressive"),
            description="Live-root single-factor parquet files whose registry rows are retired and absent from the active manifest.",
        ),
        CleanupCategory(
            name="factor_research_trace_history",
            root=RUNTIME_ROOT / "factor_research" / "orchestrator_llm_traces" / "history",
            retention_key="factor_research_trace_history",
            risk="low",
            profiles=("safe", "aggressive"),
            description="Historical redacted factor-research LLM trace files; current trace remains protected outside this root.",
        ),
        CleanupCategory(
            name="factor_research_event_history",
            root=RUNTIME_ROOT / "factor_research" / "orchestrator_events" / "history",
            retention_key="factor_research_event_history",
            risk="low",
            profiles=("safe", "aggressive"),
            description="Historical factor-research orchestrator event files; current event stream remains protected outside this root.",
        ),
        CleanupCategory(
            name="factor_research_repair_backups",
            root=RUNTIME_ROOT / "factor_research" / "repair_backups",
            retention_key="factor_research_repair_backups",
            risk="medium",
            profiles=("safe", "aggressive"),
            description="Old factor-research repair backup directories.",
        ),
        CleanupCategory(
            name="factor_research_registry_backups",
            root=RUNTIME_ROOT / "factor_research" / "registry_backups",
            retention_key="factor_research_registry_backups",
            risk="medium",
            profiles=("safe", "aggressive"),
            description="Old factor-research registry backup files.",
        ),
        CleanupCategory(
            name="model_runs",
            root=MODEL_RUNTIME_ROOT / "runs",
            retention_key="model_runs",
            risk="medium",
            profiles=("aggressive",),
            description="Old model run diagnostics; model registry artifacts are protected separately.",
        ),
    ]


def protected_paths() -> list[Path]:
    return [
        _p(PROJECT_ROOT / "config.yaml"),
        _p(QUANTGPT_DATA_DIR),
        _p(QLIB_DATA_ROOT),
        _p(FACTOR_DATA_ROOT / "parquet"),
        _p(FACTOR_REGISTRY_DB),
        _p(FACTOR_ADOPTED_VALUES_FILE),
        _p(MODEL_REGISTRY_DB),
        _p(MODEL_ACTIVE_FEATURE_DIR),
        _p(ACTIVE_MODEL_FEATURE_SET_FILE),
        _p(QUANTGPT_RESEARCH_NOTES_DIR),
    ]


def manual_review_paths() -> list[Path]:
    return [
        _p(FACTOR_ADOPTED_VALUES_FILE),
    ]


def is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def is_protected(path: Path, extra: Iterable[Path] | None = None) -> bool:
    resolved = _p(path)
    for protected in [*protected_paths(), *(extra or [])]:
        protected_resolved = _p(protected)
        if resolved == protected_resolved or is_under(resolved, protected_resolved):
            return True
    return False


def is_manual_review(path: Path) -> bool:
    resolved = _p(path)
    return any(resolved == item or is_under(resolved, item) for item in manual_review_paths())
