from __future__ import annotations

import os
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config.yaml"
CONFIG_EXAMPLE_FILE = PROJECT_ROOT / "config.example.yaml"
_CONFIG_FILE_EXPLICIT = "FXALPHA_CONFIG_FILE" in os.environ
_REQUESTED_CONFIG_FILE = Path(
    os.environ.get("FXALPHA_CONFIG_FILE", str(DEFAULT_CONFIG_FILE))
).expanduser()
CONFIG_FILE = (
    _REQUESTED_CONFIG_FILE
    if _CONFIG_FILE_EXPLICIT or _REQUESTED_CONFIG_FILE.exists()
    else CONFIG_EXAMPLE_FILE
)


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        source = CONFIG_FILE
    else:
        return {}
    return yaml.safe_load(source.read_text(encoding="utf-8")) or {}


def load_live_config() -> dict:
    """Read the selected config, or the safe example for an unconfigured clone."""
    return _load_config()


CONFIG = _load_config()
PATHS = CONFIG.get("paths", {})
DATA_FOUNDATION = CONFIG.get("data_foundation", {})
FACTOR_RESEARCH = CONFIG.get("factor_research", {})
MODEL = CONFIG.get("model", CONFIG.get("model0703", {}))
PIPELINE = CONFIG.get("pipeline", {})
LLM = CONFIG.get("llm", {})
QUANT_RESEARCH_LLM = LLM.get("quant_research", {})

DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "runtime"
DEFAULT_THIRD_PARTY_ROOT = PROJECT_ROOT / "third_party"
DEFAULT_DATA_FOUNDATION_SCRIPT_ROOT = PROJECT_ROOT / "scripts" / "data_foundation"
DEFAULT_QUANTGPT_CODE_ROOT = DEFAULT_THIRD_PARTY_ROOT / "quantgpt"
DEFAULT_QUANTGPT_DB = DEFAULT_QUANTGPT_CODE_ROOT / "quantgpt.db"
DEFAULT_QLIB_SOURCE_ROOT = DEFAULT_THIRD_PARTY_ROOT / "qlib"


def _env_bool(name: str, default: bool) -> bool:
    """Parse a conservative boolean env override for platform feature flags."""
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return str(raw if raw is not None else default).strip()


def _rooted_path(value: str | Path) -> Path:
    """Resolve configured relative paths against the repository, not the CWD."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


DATA_ROOT = _rooted_path(PATHS.get("data_root", str(DEFAULT_DATA_ROOT)))
PRODUCTION_RAW_ROOT = DATA_ROOT / "raw" / "tushare"
QUANTGPT_ROOT = DATA_ROOT / "quantgpt"
QLIB_DEFAULT_DATA_ROOT = DATA_ROOT / "qlib"
FACTOR_DEFAULT_DATA_ROOT = DATA_ROOT / "factors"
MODEL_DEFAULT_DATA_ROOT = DATA_ROOT / "model"
METADATA_DEFAULT_ROOT = DATA_ROOT / "metadata"


def _runtime_path(value: str | Path) -> Path:
    """Resolve a runtime path against the configured runtime root.

    Existing configurations commonly store values such as
    ``runtime/model/latest_status.json``.  Strip that compatibility prefix so
    the same configuration can be moved under an external ``runtime_root``.
    """
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == "runtime":
        path = Path(*path.parts[1:])
    return RUNTIME_ROOT / path

PRODUCTION_RAW_HDF5 = _rooted_path(
    PATHS.get(
        "production_raw_hdf5",
        PATHS.get("tushare_hdf5", str(PRODUCTION_RAW_ROOT / "stock_daily.h5")),
    )
).expanduser()
PRODUCTION_RAW_METADATA = _rooted_path(
    PATHS.get(
        "production_raw_metadata",
        PATHS.get("tushare_metadata", str(PRODUCTION_RAW_ROOT / "metadata.json")),
    )
).expanduser()
PRODUCTION_TRADING_CALENDAR_FILE = _rooted_path(
    PATHS.get("production_trading_calendar_file", str(PRODUCTION_RAW_ROOT / "trade_calendar.txt"))
).expanduser()
PRODUCTION_TRADING_CALENDAR_META = _rooted_path(
    PATHS.get("production_trading_calendar_meta", str(PRODUCTION_RAW_ROOT / "trade_calendar_meta.json"))
).expanduser()
THIRD_PARTY_ROOT = _rooted_path(PATHS.get("third_party_root", str(DEFAULT_THIRD_PARTY_ROOT)))
QLIB_SOURCE_ROOT = _rooted_path(PATHS.get("qlib_source_root", str(DEFAULT_QLIB_SOURCE_ROOT)))
QUANTGPT_DATA_DIR = _rooted_path(PATHS.get("quantgpt_data_dir", str(QUANTGPT_ROOT / "stocks")))
QUANTGPT_BENCHMARK_DIR = _rooted_path(PATHS.get("quantgpt_benchmark_dir", str(QUANTGPT_ROOT / "benchmark")))
QUANTGPT_CODE_ROOT = _rooted_path(PATHS.get("quantgpt_code_root", str(DEFAULT_QUANTGPT_CODE_ROOT)))
QUANTGPT_DB = _rooted_path(PATHS.get("quantgpt_db", str(DEFAULT_QUANTGPT_DB))).expanduser()
QUANTGPT_RESEARCH_NOTES_DIR = _rooted_path(
    PATHS.get("quantgpt_research_notes_dir", str(QUANTGPT_CODE_ROOT / "research_notes"))
).expanduser()
QLIB_DATA_ROOT = _rooted_path(PATHS.get("qlib_data_root", str(QLIB_DEFAULT_DATA_ROOT)))
FACTOR_DATA_ROOT = _rooted_path(PATHS.get("factor_data_root", str(FACTOR_DEFAULT_DATA_ROOT)))
FACTOR_PARQUET_DIR = _rooted_path(
    PATHS.get("factor_parquet_dir", str(FACTOR_DEFAULT_DATA_ROOT / "parquet"))
).expanduser()
FACTOR_ACTIVE_ADOPTED_VALUES_FILE = _rooted_path(
    PATHS.get("factor_active_adopted_values_file", str(FACTOR_DEFAULT_DATA_ROOT / "active_adopted_factor_values.parquet"))
).expanduser()
# Compatibility aliases. FXAlpha and embedded QuantGPT share one canonical
# active-factor value store; callers must not create independent wide copies.
FACTOR_ADOPTED_VALUES_FILE = FACTOR_ACTIVE_ADOPTED_VALUES_FILE
QUANTGPT_ADOPTED_VALUES_FILE = FACTOR_ACTIVE_ADOPTED_VALUES_FILE
FACTOR_ACTIVE_ADOPTED_VALUES_MANIFEST = _rooted_path(
    PATHS.get("factor_active_adopted_values_manifest", str(FACTOR_DEFAULT_DATA_ROOT / "active_adopted_factor_values.manifest.json"))
).expanduser()
FACTOR_REGISTRY_DB = _rooted_path(
    PATHS.get("factor_registry_db", str(FACTOR_DEFAULT_DATA_ROOT / "factor_registry.db"))
).expanduser()
MODEL_DATA_ROOT = _rooted_path(PATHS.get("model_data_root", str(MODEL_DEFAULT_DATA_ROOT)))
MODEL_FEATURES_ROOT = _rooted_path(
    PATHS.get("model_features_root", str(MODEL_DEFAULT_DATA_ROOT / "features"))
).expanduser()
MODEL_FEATURE_SETS_ROOT = _rooted_path(
    PATHS.get("model_feature_sets_root", str(MODEL_FEATURES_ROOT / "feature_sets"))
).expanduser()
MODEL_ACTIVE_FEATURE_DIR = _rooted_path(
    PATHS.get("model_active_feature_dir", str(MODEL_FEATURES_ROOT / "active"))
).expanduser()
MODEL_ACTIVE_FEATURE_FILE = _rooted_path(
    PATHS.get("model_active_feature_file", str(MODEL_ACTIVE_FEATURE_DIR / "combined_factors_df.parquet"))
).expanduser()
MODEL_ACTIVE_FEATURE_MANIFEST = _rooted_path(
    PATHS.get("model_active_feature_manifest", str(MODEL_ACTIVE_FEATURE_DIR / "manifest.json"))
).expanduser()
MODEL_REGISTRY_DB = _rooted_path(
    PATHS.get("model_registry_db", str(MODEL_DEFAULT_DATA_ROOT / "model_registry.db"))
).expanduser()
METADATA_ROOT = _rooted_path(PATHS.get("metadata_root", str(METADATA_DEFAULT_ROOT)))
STOCK_IDENTITY_CACHE = _rooted_path(
    PATHS.get("stock_identity_cache", str(METADATA_ROOT / "stock_identity_map.parquet"))
).expanduser()
STOCK_IDENTITY_CACHE_META = _rooted_path(
    PATHS.get("stock_identity_cache_meta", str(METADATA_ROOT / "stock_identity_map_meta.json"))
).expanduser()
FACTOR_RESEARCH_NOTES_ROOT = QUANTGPT_RESEARCH_NOTES_DIR

QLIB_CONVERT_SCRIPT = _rooted_path(
    PATHS.get("qlib_convert_script", str(DEFAULT_DATA_FOUNDATION_SCRIPT_ROOT / "convert_to_qlib.py"))
).expanduser()
QLIB_INDEX_CONVERT_SCRIPT = _rooted_path(
    PATHS.get("qlib_index_convert_script", str(DEFAULT_DATA_FOUNDATION_SCRIPT_ROOT / "convert_index_to_qlib.py"))
).expanduser()
QLIB_STOCK_META = QLIB_DATA_ROOT / "stock_converter_meta.json"
QLIB_INDEX_META = QLIB_DATA_ROOT / "index_converter_meta.json"
QLIB_CALENDAR_FILE = QLIB_DATA_ROOT / "calendars" / "day.txt"

RUNTIME_ROOT = _rooted_path(PATHS.get("runtime_root", str(DEFAULT_RUNTIME_ROOT)))
DATA_FOUNDATION_ROOT = RUNTIME_ROOT / "data_foundation"
LATEST_STATUS_FILE = _runtime_path(DATA_FOUNDATION.get("latest_status_file", "runtime/data_foundation/latest_status.json"))
CURRENT_PRODUCTION_DATASET_FILE = _runtime_path(DATA_FOUNDATION.get(
    "current_production_dataset_file",
    "runtime/data_foundation/CURRENT_PRODUCTION_DATASET.json",
))
FACTOR_RESEARCH_ROOT = RUNTIME_ROOT / "factor_research"
MODEL_RUNTIME_ROOT = RUNTIME_ROOT / "model"
MODEL_RUNS_ROOT = MODEL_RUNTIME_ROOT / "runs"
LATEST_MODEL_STATUS_FILE = _runtime_path(MODEL.get(
    "latest_status_file",
    "runtime/model/latest_status.json",
))
TRADING_RUNTIME_ROOT = RUNTIME_ROOT / 'trading'
PREDICTION_RUNTIME_ROOT = TRADING_RUNTIME_ROOT / 'prediction'
PREDICTION_FEATURE_RUNTIME_ROOT = TRADING_RUNTIME_ROOT / 'prediction_features'
SCORES_RUNTIME_ROOT = TRADING_RUNTIME_ROOT / 'scores'
TARGETS_RUNTIME_ROOT = TRADING_RUNTIME_ROOT / 'targets'
PAPER_TRADING_RUNTIME_ROOT = TRADING_RUNTIME_ROOT / 'paper_trading'
RECOMMENDATIONS_RUNTIME_ROOT = TRADING_RUNTIME_ROOT / 'recommendations'
TRADING_LATEST_STATUS_FILE = TRADING_RUNTIME_ROOT / 'latest_status.json'
TRADING_RISK_POLICY_CONFIG_FILE = TRADING_RUNTIME_ROOT / 'risk_policy.json'
TRADING_RISK_LATEST_FILE = TRADING_RUNTIME_ROOT / 'latest_risk_decision.json'
LATEST_PREDICTION_STATUS_FILE = TRADING_RUNTIME_ROOT / 'latest_prediction_status.json'
DAILY_OPS_RUNTIME_ROOT = RUNTIME_ROOT / 'daily_ops'
LATEST_DAILY_OPS_STATUS_FILE = DAILY_OPS_RUNTIME_ROOT / 'latest_status.json'
TRADING_DATA_ROOT = _rooted_path(PATHS.get("trading_data_root", str(DATA_ROOT / "trading")))
TRADING_EXECUTION_LOG_DB = _rooted_path(
    PATHS.get("trading_execution_log_db", str(TRADING_DATA_ROOT / "execution_log.db"))
).expanduser()
ACTIVE_MODEL_FEATURE_SET_FILE = _runtime_path(MODEL.get(
    "active_feature_set_file",
    "runtime/model/active_feature_set.json",
))
LATEST_PIPELINE_STATUS_FILE = _runtime_path(PIPELINE.get("latest_status_file", "runtime/pipeline/latest_status.json"))
PIPELINE_DEFAULT_FACTOR_TARGET = int(PIPELINE.get("default_factor_target", 10))
PIPELINE_DEFAULT_FACTOR_SESSIONS = int(PIPELINE.get("default_factor_sessions", 3))
PIPELINE_DEFAULT_MODEL_FAMILY = str(PIPELINE.get("default_model_family", MODEL.get("default_family", "lgbm")))
MODEL_DEFAULT_START_DATE = MODEL.get("default_start_date", "2022-01-01")
MODEL_DEFAULT_END_DATE = MODEL.get("default_end_date")
MODEL_DEFAULT_SELECTION_CUTOFF = MODEL.get("default_selection_cutoff")
MODEL_DEFAULT_FORWARD_PERIOD = int(MODEL.get("default_forward_period", 5))
MODEL_DEFAULT_FACTOR_HOLDING_PERIOD = int(MODEL.get("default_factor_holding_period", FACTOR_RESEARCH.get("default_holding_period", 5)))
MODEL_DEFAULT_STATUS_FILTER = MODEL.get("default_status_filter", "active")
MODEL_DEFAULT_FAMILY = MODEL.get("default_family", "lgbm")
MODEL_DEFAULT_BENCHMARK = str(MODEL.get("default_benchmark", "000300sh"))
MODEL_DEFAULT_TOPK = int(MODEL.get("default_topk", 20))
MODEL_DEFAULT_N_DROP = int(MODEL.get("default_n_drop", 2))
MODEL_DEFAULT_VALID_MONTHS = int(MODEL.get("default_valid_months", 6))
MODEL_DEFAULT_TEST_MONTHS = int(MODEL.get("default_test_months", 10))
MODEL_DEFAULT_POLICY = str(MODEL.get("default_policy", "qlib_lgbm_canonical"))
MODEL_ALLOWED_POLICIES = tuple(
    MODEL.get("allowed_policies", ["qlib_lgbm_canonical", "lgbm_safe", "tabular_ensemble", "sequence_experimental"])
)


def _model_segments() -> dict[str, list[str]]:
    raw = MODEL.get("default_segments") or {}
    fallback = {
        "train": ["2022-01-04", "2024-12-31"],
        "valid": ["2025-01-02", "2025-06-30"],
        "test": ["2025-07-01", "2026-07-01"],
    }
    out: dict[str, list[str]] = {}
    for key, default_value in fallback.items():
        value = raw.get(key) if isinstance(raw, dict) else None
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            out[key] = [str(value[0]), str(value[1])]
        elif isinstance(value, dict):
            start = value.get("start") or value.get("start_date") or value.get("begin") or value.get("from")
            end = value.get("end") or value.get("end_date") or value.get("to")
            out[key] = [str(start), str(end)] if start and end else list(default_value)
        else:
            out[key] = list(default_value)
    return out


MODEL_DEFAULT_SEGMENTS = _model_segments()
MODEL_EVALUATION_MODE = str(MODEL.get("evaluation_mode", "research"))
_model_comparison_seeds = MODEL.get("comparison_seed_set", [42, 17, 83])
MODEL_COMPARISON_SEED_SET = tuple(int(seed) for seed in _model_comparison_seeds)
MODEL_SCREENING_SEED = int(MODEL.get("screening_seed", 42))
MODEL_CONFIRMATION_SEEDS = tuple(int(seed) for seed in MODEL.get("confirmation_seeds", [17, 83]))
MODEL_RESEARCH_SCORING = dict(MODEL.get("research_scoring") or {})
MODEL_ROLLING = dict(MODEL.get("rolling") or {})
MODEL_SEED_SOTA_SCORE_THRESHOLD = float(MODEL.get("seed_sota_score_threshold", 60.0))
MODEL_FORWARD_TEST = dict(MODEL.get("forward_test") or {})
MODEL_PRODUCTION_REFIT = dict(MODEL.get("production_refit") or {})
MODEL_SAMPLE_WEIGHT_POLICIES = tuple(
    MODEL.get("allowed_sample_weight_policies", ["top50_smooth2_bottom50_smooth1p5_mean_norm", "sticky", "none"])
)
MODEL_DEFAULT_SAMPLE_WEIGHT_POLICY = str(
    MODEL.get("default_sample_weight_policy", "top50_smooth2_bottom50_smooth1p5_mean_norm")
)
MODEL_DEFAULT_SAMPLE_WEIGHT_KWARGS = dict(
    MODEL.get(
        "default_sample_weight_kwargs",
        {
            "top_n": 50,
            "top_max": 2.0,
            "bottom_n": 50,
            "bottom_max": 1.5,
            "normalize_mean": True,
        },
    )
)
QUANTGPT_API_URL = FACTOR_RESEARCH.get("default_qgpt_url", "http://127.0.0.1:8003")
FACTOR_RESEARCH_DEFAULT_ORCHESTRATION_MODE = str(
    FACTOR_RESEARCH.get("default_orchestration_mode", "orchestrator")
).strip().lower()
FACTOR_DEFAULT_UNIVERSE = FACTOR_RESEARCH.get("default_universe", "tradable_non_st")
def _require_config_date(*candidates: tuple[dict, str], label: str) -> str:
    for source, key in candidates:
        value = source.get(key)
        if value:
            return str(value)
    keys = ", ".join(f"{name}" for _, name in candidates)
    raise RuntimeError(f"missing required config date: {label} ({keys})")


FACTOR_DEFAULT_START_DATE = _require_config_date(
    (FACTOR_RESEARCH, "default_start_date"),
    (MODEL, "default_start_date"),
    label="factor selection start date",
)
FACTOR_DEFAULT_END_DATE = _require_config_date(
    (FACTOR_RESEARCH, "default_end_date"),
    label="factor selection end date",
)
FACTOR_VALUE_DEFAULT_START_DATE = FACTOR_RESEARCH.get("default_value_start_date", FACTOR_DEFAULT_START_DATE)
FACTOR_VALUE_DEFAULT_END_DATE = FACTOR_RESEARCH.get("default_value_end_date", FACTOR_DEFAULT_END_DATE)
FACTOR_DEFAULT_HOLDING_PERIOD = int(FACTOR_RESEARCH.get("default_holding_period", 5))
FACTOR_DEFAULT_BENCHMARK = FACTOR_RESEARCH.get("default_benchmark", "hs300")
FACTOR_DEFAULT_TOP_FRAC = float(FACTOR_RESEARCH.get("default_top_frac", 0.2))
FACTOR_DEFAULT_COST_RATE = float(FACTOR_RESEARCH.get("default_cost_rate", 0.003))
FACTOR_DEFAULT_REBALANCE_ANCHOR = FACTOR_RESEARCH.get("default_rebalance_anchor")
FACTOR_DEFAULT_UNIVERSE_DATE = FACTOR_RESEARCH.get("default_universe_date")
FACTOR_ROLLING_VALIDATION = FACTOR_RESEARCH.get("rolling_validation", {}) or {}
FACTOR_ROLLING_SCHEMA_VERSION = str(FACTOR_ROLLING_VALIDATION.get("schema_version", "rolling_validation_v2"))
FACTOR_ROLLING_SCORE_POLICY_VERSION = str(
    FACTOR_ROLLING_VALIDATION.get("score_policy_version", "rolling_ic_recency_robust_v1")
)
FACTOR_ROLLING_MAX_HISTORY_MONTHS = int(FACTOR_ROLLING_VALIDATION.get("max_history_months", 48))
FACTOR_ROLLING_MIN_HISTORY_MONTHS = int(FACTOR_ROLLING_VALIDATION.get("min_history_months", 24))
FACTOR_ROLLING_PERIOD_WEIGHTS = tuple(
    float(value) for value in FACTOR_ROLLING_VALIDATION.get("period_weights", [0.40, 0.25, 0.15, 0.12, 0.08])
)
FACTOR_ROLLING_STABILITY_PENALTY = float(FACTOR_ROLLING_VALIDATION.get("stability_penalty", 0.25))
FACTOR_ROLLING_RANK_IC_FULL_SCORE = float(FACTOR_ROLLING_VALIDATION.get("rank_ic_full_score", 0.08))
FACTOR_ROLLING_MIN_DATES_PER_6M = int(FACTOR_ROLLING_VALIDATION.get("min_dates_per_6m", 60))
FACTOR_ROLLING_TRAILING_HORIZONS = tuple(
    int(value) for value in FACTOR_ROLLING_VALIDATION.get("trailing_horizons_months", [6, 12, 24, 36, 48])
)
FACTOR_DEFAULT_N_CANDIDATES = int(FACTOR_RESEARCH.get("default_n_candidates", 10))
FACTOR_DEFAULT_N_ROUNDS = int(FACTOR_RESEARCH.get("default_n_rounds", 3))
FACTOR_DEFAULT_TARGET_ADOPTED = int(FACTOR_RESEARCH.get("default_target_adopted", 3))
FACTOR_DEFAULT_SEED_COUNT = int(FACTOR_RESEARCH.get("default_seed_count", 3))
FACTOR_DEFAULT_SEED_MAX_CONCURRENT = int(FACTOR_RESEARCH.get("default_seed_max_concurrent", 3))
FACTOR_DEFAULT_MAX_DIRECTION_ATTEMPTS = int(FACTOR_RESEARCH.get("default_max_direction_attempts", 3))
FACTOR_DEFAULT_MAX_STAGNATION_ROUNDS = int(FACTOR_RESEARCH.get("default_max_stagnation_rounds", 3))
FACTOR_DEFAULT_QUICK_SCORE_MAX_STOCKS = int(FACTOR_RESEARCH.get("quick_score_max_stocks", 360))
FACTOR_DEFAULT_QUICK_SCORE_MAX_DATES = int(FACTOR_RESEARCH.get("quick_score_max_dates", 252))
FACTOR_DEFAULT_DEEP_VALIDATE_TOP_N = int(FACTOR_RESEARCH.get("deep_validate_top_n", 2))
FACTOR_ENABLE_WQ_SUBMIT = bool(FACTOR_RESEARCH.get("enable_wq_submit", False))


def normalize_st_exposure_guard_mode(value: str | None) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"advisory", "diagnostic", "tag", "tag_only", "label"}:
        return "advisory"
    if mode in {"hard", "strict", "block", "blocking"}:
        return "hard"
    return "advisory"


FACTOR_ST_EXPOSURE_GUARD_MODE = normalize_st_exposure_guard_mode(
    _env_str(
        "FXALPHA_ST_EXPOSURE_GUARD_MODE",
        str(FACTOR_RESEARCH.get("st_exposure_guard_mode", "advisory")),
    )
)


def get_live_st_exposure_guard_mode() -> str:
    import os

    env_value = os.environ.get("FXALPHA_ST_EXPOSURE_GUARD_MODE")
    if env_value:
        return normalize_st_exposure_guard_mode(env_value)
    factor_cfg = get_live_factor_research_config()
    return normalize_st_exposure_guard_mode(factor_cfg.get("st_exposure_guard_mode", FACTOR_ST_EXPOSURE_GUARD_MODE))


def get_live_factor_research_config() -> dict:
    return load_live_config().get("factor_research", {}) or {}


def get_live_factor_default_start_date() -> str:
    config = load_live_config()
    factor_cfg = config.get("factor_research", {}) or {}
    model_cfg = config.get("model", {}) or {}
    return _require_config_date(
        (factor_cfg, "default_start_date"),
        (model_cfg, "default_start_date"),
        label="factor selection start date",
    )


def get_live_factor_default_end_date() -> str:
    factor_cfg = get_live_factor_research_config()
    return _require_config_date(
        (factor_cfg, "default_end_date"),
        label="factor selection end date",
    )


def get_live_factor_value_default_start_date() -> str:
    factor_cfg = get_live_factor_research_config()
    return factor_cfg.get("default_value_start_date") or get_live_factor_default_start_date()


def get_live_factor_value_default_end_date() -> str:
    factor_cfg = get_live_factor_research_config()
    return factor_cfg.get("default_value_end_date") or get_live_factor_default_end_date()

LLM_PROVIDER = QUANT_RESEARCH_LLM.get("provider", "deepseek")
LLM_API_KEY = QUANT_RESEARCH_LLM.get("api_key", "")
LLM_BASE_URL = QUANT_RESEARCH_LLM.get("base_url", "https://api.deepseek.com/v1")
LLM_MODEL = QUANT_RESEARCH_LLM.get("model", "deepseek-v4-flash")
LLM_CROSS_REVIEW_MODEL = QUANT_RESEARCH_LLM.get("cross_review_model", LLM_MODEL)
