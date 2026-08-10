from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .training_contract import (
    QLIB_OFFICIAL_ALPHA158_LGBM_PARAMS,
    QLIB_REQUIRED_MODEL_CLASS,
    QLIB_REQUIRED_MODEL_MODULE,
    QLIB_REQUIRED_PROCESSORS,
    QLIB_REQUIRED_STRATEGY_CLASS,
    QLIB_REQUIRED_STRATEGY_MODULE,
    normalize_lgbm_training_params,
)
from storage.paths import (
    MODEL_DEFAULT_BENCHMARK,
    MODEL_DEFAULT_END_DATE,
    MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    MODEL_DEFAULT_FORWARD_PERIOD,
    MODEL_DEFAULT_N_DROP,
    MODEL_DEFAULT_TOPK,
    MODEL_DEFAULT_START_DATE,
    MODEL_DEFAULT_STATUS_FILTER,
    MODEL_DEFAULT_SAMPLE_WEIGHT_KWARGS,
    MODEL_DEFAULT_SAMPLE_WEIGHT_POLICY,
    MODEL_DEFAULT_SEGMENTS,
    MODEL_COMPARISON_SEED_SET,
    MODEL_CONFIRMATION_SEEDS,
    MODEL_PRODUCTION_REFIT,
    MODEL_ROLLING,
    MODEL_SCREENING_SEED,
    MODEL_SAMPLE_WEIGHT_POLICIES,
    MODEL_SEED_SOTA_SCORE_THRESHOLD,
)


MODEL_SYSTEM_VERSION = "model"
LEGACY_MODEL_SYSTEM_VERSIONS = ("model0703",)
SOURCE_MODULE = "domain.model"
SCORE_REVIEW_VERSION = "model_research_score_v1"
GATE_VERSION = "model_rolling_candidate_gate_v1"


def is_model_system_version(value: Any) -> bool:
    """Return true for the canonical name and immutable historical metadata."""

    return str(value or "") in {MODEL_SYSTEM_VERSION, *LEGACY_MODEL_SYSTEM_VERSIONS}

ASSET_STATUSES = ("research", "candidate", "production", "archived")
JOB_STATUSES = ("queued", "running", "completed", "failed", "cancelled", "interrupted")
STAGES = (
    "protocol_load",
    "context_review",
    "feature_snapshot",
    "experiment_plan",
    "train_backtest_seed42",
    "research_score",
    "research_confirmation",
    "rolling_preliminary",
    "rolling_confirmation",
    "rolling_score",
    "registry_write",
    "round_synthesis",
    "checkpoint_stop",
    "blocker",
)
GATE_STATUSES = ("pass", "pass_with_warnings", "reject")

FEATURE_MISSING_STRATEGIES = ("qlib_processor_only", "structural_zero_v2", "semantic_fill_v1")
DEFAULT_FEATURE_MISSING_STRATEGY = "qlib_processor_only"

SAMPLE_WEIGHT_POLICIES = tuple(MODEL_SAMPLE_WEIGHT_POLICIES)
DEFAULT_SAMPLE_WEIGHT_POLICY = MODEL_DEFAULT_SAMPLE_WEIGHT_POLICY
DEFAULT_SAMPLE_WEIGHT_KWARGS = dict(MODEL_DEFAULT_SAMPLE_WEIGHT_KWARGS)

DEFAULT_PORTFOLIO = {
    "topk": MODEL_DEFAULT_TOPK,
    "n_drop": MODEL_DEFAULT_N_DROP,
    "hold_thresh": MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    "deal_price": "open",
    "benchmark": MODEL_DEFAULT_BENCHMARK,
}

LABEL_CONTRACT = {
    "label_name": "LABEL0",
    "label_forward_period": MODEL_DEFAULT_FORWARD_PERIOD,
    "factor_holding_period_days": MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    "label_execution_deal_price": "open",
    "label_return_mode": "next_open_to_forward_open",
}

LIMIT_THRESHOLD = ["$limit_buy_open_sealed", "$limit_sell_open_sealed"]
SEED_SOTA_SCORE_THRESHOLD = MODEL_SEED_SOTA_SCORE_THRESHOLD


def default_segments() -> dict[str, list[str]]:
    return {key: list(value) for key, value in MODEL_DEFAULT_SEGMENTS.items()}


def _segments_from_config(raw: Any, fallback: dict[str, list[str]], *, require_test_only: bool = False) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    keys = ("test",) if require_test_only else ("train", "valid", "test")
    raw = raw if isinstance(raw, dict) else {}
    for key in keys:
        default_value = fallback.get(key, ["", ""])
        value = raw.get(key)
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            out[key] = [str(value[0]), str(value[1])]
        elif isinstance(value, dict):
            start = value.get("start") or value.get("start_date") or value.get("begin") or value.get("from")
            end = value.get("end") or value.get("end_date") or value.get("to")
            out[key] = [str(start), str(end)] if start and end else list(default_value)
        else:
            out[key] = list(default_value)
    return out


def staged_seed_contract() -> dict[str, Any]:
    return {
        "screening_seed": int(MODEL_SCREENING_SEED),
        "confirmation_seeds": [int(seed) for seed in MODEL_CONFIRMATION_SEEDS],
        "planned_seed_set": [int(MODEL_SCREENING_SEED), *[int(seed) for seed in MODEL_CONFIRMATION_SEEDS]],
        "ordinary_round_executes": [int(MODEL_SCREENING_SEED)],
        "session_best_confirmation_only": True,
        "best_seed_selection_allowed": False,
    }


def forward_test_contract() -> dict[str, Any]:
    """Deprecated compatibility surface; forward testing is not an active stage."""
    return {"enabled": False, "deprecated": True, "replacement": "research_confirmation_and_production_rolling"}


def rolling_contract() -> dict[str, Any]:
    cfg = dict(MODEL_ROLLING)
    return {
        "profile": str(cfg.get("profile", "four_fold_expanding_6m_v1")),
        "fold_count": int(cfg.get("fold_count", 4)),
        "valid_months": int(cfg.get("valid_months", 6)),
        "test_months": int(cfg.get("test_months", 6)),
        "purge_trading_days": int(cfg.get("purge_trading_days", 5)),
        "preliminary_score_threshold": float(cfg.get("preliminary_score_threshold", 60.0)),
        "candidate_score_threshold": float(cfg.get("candidate_score_threshold", 70.0)),
        "portfolio": dict(DEFAULT_PORTFOLIO),
        "seed_policy": staged_seed_contract(),
    }


def production_refit_contract() -> dict[str, Any]:
    cfg = dict(MODEL_PRODUCTION_REFIT)
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "shift_months": int(cfg.get("shift_months", 12)),
        "segments": _segments_from_config(
            cfg.get("segments"),
            {
                "train": ["2023-01-03", "2025-12-31"],
                "valid": ["2026-01-02", "2026-06-30"],
                "test": ["2026-01-02", "2026-07-01"],
            },
        ),
        "source": "formal_rolling_campaign_candidate",
        "writes_new_production_model": True,
    }


MODEL_R1_BASELINE_KIND = "model_fxalpha_calibrated_lgbm_highcap_fast_stochastic_top20_drop2_hold5"

MODEL_CALIBRATED_R1_LGBM_PARAMS: dict[str, Any] = {
    **QLIB_OFFICIAL_ALPHA158_LGBM_PARAMS,
    "learning_rate": 0.04,
    "lr": 0.04,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.9,
    # The 2026-07-18 All33 two-window study showed that joint 90% feature/row
    # sampling materially improved the median and worst fixed-panel seed.  Keep
    # row bagging enabled as a system-fixed part of the calibrated anchor.
    "bagging_freq": 1,
    "lambda_l1": 20,
    "lambda_l2": 50,
    "max_depth": 8,
    "num_leaves": 96,
    "n_estimators": 2400,
    "early_stopping_rounds": 100,
    "min_data_in_leaf": 10,
    # Bin construction remains effectively full-sample for the current
    # dataset.  Sampling seeds are pinned within each model run and replaced by
    # the persisted 42/17/83 comparison panel in the seed worker.  Selection
    # must use aggregate/worst-seed evidence, never the best test seed.
    "bin_construct_sample_cnt": 5_000_000,
    "seed": 42,
    "feature_fraction_seed": 42,
    "bagging_seed": 42,
    "data_random_seed": 42,
    "drop_seed": 42,
}

DEFAULT_QLIB_MODEL_KWARGS = dict(MODEL_CALIBRATED_R1_LGBM_PARAMS)

RESEARCH_BASELINE_CONFIGURABLE_KEYS = (
    "learning_rate",
    "num_leaves",
    "max_depth",
    "min_data_in_leaf",
    "feature_fraction",
    "bagging_fraction",
    "bagging_freq",
    "lambda_l1",
    "lambda_l2",
    "n_estimators",
    "early_stopping_rounds",
    "bin_construct_sample_cnt",
)

RESEARCH_BASELINE_PARAMETER_BOUNDS: dict[str, tuple[float, float]] = {
    # The operator-facing research baseline also supports Qlib's published
    # Alpha158 anchor (learning_rate=0.2).  ORCH's later automated moves keep
    # their own narrower tuning bounds.
    "learning_rate": (0.005, 0.30),
    "num_leaves": (16, 256),
    "max_depth": (4, 12),
    "min_data_in_leaf": (5, 200),
    "feature_fraction": (0.60, 1.00),
    "bagging_fraction": (0.50, 1.00),
    "bagging_freq": (0, 20),
    "lambda_l1": (0.0, 300.0),
    "lambda_l2": (0.0, 600.0),
    "n_estimators": (500, 5000),
    "early_stopping_rounds": (30, 300),
    "bin_construct_sample_cnt": (100_000, 10_000_000),
}

RESEARCH_BASELINE_INTEGER_KEYS = {
    "num_leaves",
    "max_depth",
    "min_data_in_leaf",
    "bagging_freq",
    "n_estimators",
    "early_stopping_rounds",
    "bin_construct_sample_cnt",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def experiment_signature(experiment: dict[str, Any]) -> str:
    payload = dict(experiment or {})
    payload.pop("round_group_id", None)
    payload.pop("seed_set", None)
    payload.pop("seed_policy", None)
    digest = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
    return digest[:16]


def round_group_id_from(feature_set_id: str, signature: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = hashlib.sha256(f"{feature_set_id}:{signature}".encode("utf-8")).hexdigest()[:8]
    return f"mround_{stamp}_{short}"


def staged_seed_set(round_group_id: str, signature: str) -> list[int]:
    """Return the fixed screening and confirmation seed identities.

    The arguments remain part of the public API for compatibility, but must
    not influence the panel. Otherwise each ablation is measured on different
    model randomness and round-level means are not comparable.
    """

    del round_group_id, signature
    seeds = [int(seed) for seed in MODEL_COMPARISON_SEED_SET]
    if len(seeds) != 3 or seeds[0] != 42 or len(set(seeds)) != 3:
        raise ValueError("model.comparison_seed_set_must_be_three_unique_seeds_starting_with_42")
    return seeds


# Historical import compatibility. New code must use ``staged_seed_set`` so a
# planned three-seed identity is not mistaken for three executions per round.
fixed_three_seed_set = staged_seed_set


def seed_policy(seed_set: list[int]) -> dict[str, Any]:
    return {
        "mode": "staged_screening_then_confirmation",
        "screening_seed": int(MODEL_SCREENING_SEED),
        "confirmation_seeds": [int(seed) for seed in MODEL_CONFIRMATION_SEEDS],
        "planned_seed_set": list(seed_set),
        "executed_seed_set": [int(MODEL_SCREENING_SEED)],
        "cross_round_stable": True,
        "best_seed_selection_allowed": False,
    }


@dataclass
class RoundGroup:
    round_group_id: str
    feature_set_id: str
    experiment_signature: str
    seed_set: list[int]
    experiment: dict[str, Any]
    seed_policy: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    stage: str = "experiment_plan"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not data.get("seed_policy"):
            data["seed_policy"] = seed_policy(self.seed_set)
        return data


def production_contract() -> dict[str, Any]:
    return {
        "model_system_version": MODEL_SYSTEM_VERSION,
        "execution_main_chain": "direct_qlib0627_workflow",
        "asset_statuses": list(ASSET_STATUSES),
        "job_statuses": list(JOB_STATUSES),
        "stages": list(STAGES),
        "gate_statuses": list(GATE_STATUSES),
        "feature_missing_strategies": list(FEATURE_MISSING_STRATEGIES),
        "default_feature_missing_strategy": DEFAULT_FEATURE_MISSING_STRATEGY,
        "label_contract": dict(LABEL_CONTRACT),
        "evaluation_modes": ["research", "production"],
        "research_seed_policy": staged_seed_contract(),
        "production_rolling": rolling_contract(),
        "processor_chain": {
            "infer_processors": [
                {"class": "ProcessInf", "kwargs": {}},
                {"class": "RobustZScoreNorm", "kwargs": {"fields_group": "feature", "clip_outlier": True}},
                {"class": "CSZFillna", "kwargs": {"fields_group": "feature"}},
            ],
            "learn_processors": [
                {"class": "DropnaLabel", "kwargs": {"fields_group": "label"}},
                {"class": "CSZScoreNorm", "kwargs": {"fields_group": "label", "method": "zscore"}},
            ],
        },
        "model_class": "FXAlphaWeightedLGBModel",
        "model_module": QLIB_REQUIRED_MODEL_MODULE,
        "r1_baseline_kind": MODEL_R1_BASELINE_KIND,
        "r1_default_lgbm_params": dict(DEFAULT_QLIB_MODEL_KWARGS),
        "r1_default_source": "fxalpha_default_parameter_study_20260718",
        "qlib_official_alpha158_lgbm_params": dict(QLIB_OFFICIAL_ALPHA158_LGBM_PARAMS),
        "research_baseline_overrides": research_baseline_override_contract(),
        "sample_weight_policies": list(SAMPLE_WEIGHT_POLICIES),
        "default_sample_weight_policy": DEFAULT_SAMPLE_WEIGHT_POLICY,
        "default_sample_weight_kwargs": dict(DEFAULT_SAMPLE_WEIGHT_KWARGS),
        "portfolio": dict(DEFAULT_PORTFOLIO),
        "benchmark": MODEL_DEFAULT_BENCHMARK,
        "formal_backtest": {"deal_price": "open", "limit_threshold": list(LIMIT_THRESHOLD)},
        "pred_shift_policy": "do_not_pre_shift_pred_pkl",
        "production_refit": production_refit_contract(),
        "window_defaults": {
            "start_date": MODEL_DEFAULT_START_DATE,
            "end_date": MODEL_DEFAULT_END_DATE,
            "status_filter": MODEL_DEFAULT_STATUS_FILTER,
            "segments": default_segments(),
        },
        "seed_policy": seed_policy(staged_seed_set("", "")),
    }


def default_r1_experiment(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "baseline_kind": MODEL_R1_BASELINE_KIND,
        "model_policy": "qlib_lgbm_canonical",
        "model_family": "lgbm",
        "model_class": QLIB_REQUIRED_MODEL_CLASS,
        "model_module": QLIB_REQUIRED_MODEL_MODULE,
        "qlib_model_kwargs": dict(DEFAULT_QLIB_MODEL_KWARGS),
        "training_hyperparameters": dict(DEFAULT_QLIB_MODEL_KWARGS),
        "feature_missing_strategy": DEFAULT_FEATURE_MISSING_STRATEGY,
        "sample_weight_policy": DEFAULT_SAMPLE_WEIGHT_POLICY,
        "sample_weight_kwargs": dict(DEFAULT_SAMPLE_WEIGHT_KWARGS),
        "portfolio": dict(DEFAULT_PORTFOLIO),
        "benchmark": MODEL_DEFAULT_BENCHMARK,
        "deal_price": "open",
        "limit_threshold": list(LIMIT_THRESHOLD),
        "forbid_all_trade_at_limit": False,
        "pre_shift_pred": False,
        "segments": default_segments(),
        "qlib_processors": production_contract()["processor_chain"],
        **LABEL_CONTRACT,
    }
    payload.update(overrides or {})
    return payload


def research_baseline_override_contract() -> dict[str, Any]:
    return {
        "configurable_keys": list(RESEARCH_BASELINE_CONFIGURABLE_KEYS),
        "bounds": {
            key: {"min": bounds[0], "max": bounds[1]}
            for key, bounds in RESEARCH_BASELINE_PARAMETER_BOUNDS.items()
        },
        "defaults": {
            key: DEFAULT_QLIB_MODEL_KWARGS.get(key)
            for key in RESEARCH_BASELINE_CONFIGURABLE_KEYS
        },
        "fixed": {
            "loss": DEFAULT_QLIB_MODEL_KWARGS.get("loss", "mse"),
            "boosting_type": DEFAULT_QLIB_MODEL_KWARGS.get("boosting_type", "gbdt"),
            "seed_policy": "seed42_screening_then_seed17_83_confirmation",
            "runtime_threads": 1,
        },
    }


def normalize_research_baseline_overrides(raw: dict[str, Any] | None) -> dict[str, Any]:
    if raw in (None, {}):
        return {"passed": True, "errors": [], "normalized": {}}
    if not isinstance(raw, dict):
        return {"passed": False, "errors": ["baseline_model_params_must_be_mapping"], "normalized": {}}
    errors: list[str] = []
    candidate = dict(raw)
    if candidate.get("lr") not in (None, ""):
        try:
            lr_value = float(candidate["lr"])
            learning_rate_value = (
                float(candidate["learning_rate"])
                if candidate.get("learning_rate") not in (None, "")
                else lr_value
            )
            if lr_value != learning_rate_value:
                errors.append("baseline_model_param_conflict:lr!=learning_rate")
            else:
                candidate["learning_rate"] = lr_value
        except (TypeError, ValueError):
            errors.append("baseline_model_param_not_numeric:lr")
    unknown = sorted(set(candidate) - set(RESEARCH_BASELINE_CONFIGURABLE_KEYS) - {"lr"})
    if unknown:
        errors.append("baseline_model_params_unknown:" + ",".join(unknown))
    normalized: dict[str, Any] = {}
    for key in RESEARCH_BASELINE_CONFIGURABLE_KEYS:
        value = candidate.get(key)
        if value in (None, ""):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            errors.append(f"baseline_model_param_not_numeric:{key}")
            continue
        lower, upper = RESEARCH_BASELINE_PARAMETER_BOUNDS[key]
        if not lower <= numeric <= upper:
            errors.append(f"baseline_model_param_out_of_bounds:{key}:{numeric};allowed={lower}-{upper}")
            continue
        normalized[key] = int(numeric) if key in RESEARCH_BASELINE_INTEGER_KEYS else numeric
    merged = dict(DEFAULT_QLIB_MODEL_KWARGS)
    merged.update(normalized)
    if int(merged["num_leaves"]) > 2 ** int(merged["max_depth"]):
        errors.append("baseline_model_param_relation:num_leaves_exceeds_depth_capacity")
    if int(merged["early_stopping_rounds"]) >= int(merged["n_estimators"]):
        errors.append("baseline_model_param_relation:early_stopping_not_below_estimators")
    if float(merged.get("bagging_fraction", 1.0)) < 1.0 and int(merged.get("bagging_freq", 0)) <= 0:
        errors.append("baseline_model_param_relation:bagging_freq_required_when_fraction_below_one")
    if "learning_rate" in normalized:
        normalized["lr"] = normalized["learning_rate"]
    return {"passed": not errors, "errors": errors, "normalized": normalized}


def _coerce_portfolio(value: Any, default: dict[str, Any], *, label: str, errors: list[str] | None = None) -> dict[str, Any]:
    if value in (None, ""):
        return dict(default)
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        match = re.fullmatch(r"top(\d+)\s*/\s*drop(\d+)\s*/\s*hold(\d+)", raw, flags=re.I)
        if match:
            topk, n_drop, hold = (int(part) for part in match.groups())
            return {**default, "topk": topk, "n_drop": n_drop, "hold_thresh": hold}
        if errors is not None:
            errors.append(f"{label}_portfolio_format_invalid:{raw}")
        return dict(default)
    if errors is not None:
        errors.append(f"{label}_portfolio_type_invalid:{type(value).__name__}")
    return dict(default)


def _portfolio_errors(exp: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    portfolio = _coerce_portfolio(exp.get("portfolio"), DEFAULT_PORTFOLIO, label="portfolio", errors=errors)
    checks = [
        ("portfolio_topk", portfolio.get("topk"), DEFAULT_PORTFOLIO["topk"]),
        ("portfolio_n_drop", portfolio.get("n_drop"), DEFAULT_PORTFOLIO["n_drop"]),
        ("portfolio_hold_thresh", portfolio.get("hold_thresh"), DEFAULT_PORTFOLIO["hold_thresh"]),
    ]
    for label, value, expected in checks:
        if int(value if value is not None else expected) != int(expected):
            errors.append(f"{label}_mismatch:{value}!=expected:{expected}")
    if str(portfolio.get("deal_price") or "open") != "open":
        errors.append("portfolio_deal_price_must_be_open")
    if str(exp.get("benchmark") or MODEL_DEFAULT_BENCHMARK) != MODEL_DEFAULT_BENCHMARK:
        errors.append(f"benchmark_mismatch:{exp.get('benchmark')}!=expected:{MODEL_DEFAULT_BENCHMARK}")
    limit_threshold = exp.get("limit_threshold") or LIMIT_THRESHOLD
    if not isinstance(limit_threshold, (list, tuple)) or list(limit_threshold) != LIMIT_THRESHOLD:
        errors.append(f"limit_threshold_mismatch:{limit_threshold}!=expected:{LIMIT_THRESHOLD}")
    if _as_bool(exp.get("forbid_all_trade_at_limit", False)):
        errors.append("forbid_all_trade_at_limit_must_be_false")
    return errors


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "0", "no", "n", "off", ""}:
            return False
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
    return bool(value)


def _normalized_portfolio(exp: dict[str, Any]) -> dict[str, Any]:
    portfolio = _coerce_portfolio(exp.get("portfolio"), DEFAULT_PORTFOLIO, label="portfolio")
    return {**DEFAULT_PORTFOLIO, **portfolio, "deal_price": "open", "benchmark": MODEL_DEFAULT_BENCHMARK}


def _processor_errors(processors: Any) -> list[str]:
    if processors in (None, ""):
        return []
    text = stable_json(processors) if not isinstance(processors, str) else processors
    missing = sorted(item for item in QLIB_REQUIRED_PROCESSORS if item not in text)
    return ["processor_policy_drift:" + ",".join(missing)] if missing else []


def _seed_policy_errors(exp: dict[str, Any]) -> list[str]:
    policy = exp.get("seed_policy")
    if not isinstance(policy, dict):
        return []
    errors: list[str] = []
    if bool(policy.get("use_ensemble")):
        errors.append("seed_policy_ensemble_forbidden")
    if str(policy.get("mode") or "").strip() in {"best_seed", "diagnostic_seed_only", "main_seed"}:
        errors.append(f"seed_policy_mode_forbidden:{policy.get('mode')}")
    if str(policy.get("mode") or "staged_screening_then_confirmation").strip() != "staged_screening_then_confirmation":
        errors.append(f"seed_policy_mode_must_be_staged_screening_then_confirmation:{policy.get('mode')}")
    return errors


def _strip_sample_weight_fields(params: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(params or {})
    cleaned.pop("sample_weight_policy", None)
    cleaned.pop("sample_weight_kwargs", None)
    return cleaned


def _extract_sample_weight(exp: dict[str, Any], errors: list[str]) -> tuple[str, dict[str, Any]]:
    candidates: list[tuple[str, Any]] = [
        ("root", exp.get("sample_weight_policy")),
        ("qlib_model_kwargs", (exp.get("qlib_model_kwargs") or {}).get("sample_weight_policy") if isinstance(exp.get("qlib_model_kwargs"), dict) else None),
        ("training_hyperparameters", (exp.get("training_hyperparameters") or {}).get("sample_weight_policy") if isinstance(exp.get("training_hyperparameters"), dict) else None),
    ]
    explicit = [(source, str(value).strip()) for source, value in candidates if value not in (None, "")]
    policies = {value for _source, value in explicit}
    if len(policies) > 1:
        errors.append("sample_weight_policy_conflict:" + ",".join(f"{source}={value}" for source, value in explicit))
    policy = explicit[0][1] if explicit else DEFAULT_SAMPLE_WEIGHT_POLICY
    if policy != DEFAULT_SAMPLE_WEIGHT_POLICY:
        errors.append(f"sample_weight_policy_fixed_contract:{policy}!=expected:{DEFAULT_SAMPLE_WEIGHT_POLICY}")

    kwargs_candidates: list[tuple[str, dict[str, Any]]] = []
    for source, payload in (
        ("root", exp.get("sample_weight_kwargs")),
        ("qlib_model_kwargs", (exp.get("qlib_model_kwargs") or {}).get("sample_weight_kwargs") if isinstance(exp.get("qlib_model_kwargs"), dict) else None),
        ("training_hyperparameters", (exp.get("training_hyperparameters") or {}).get("sample_weight_kwargs") if isinstance(exp.get("training_hyperparameters"), dict) else None),
    ):
        if isinstance(payload, dict) and payload:
            kwargs_candidates.append((source, dict(payload)))
    if len({stable_json(value) for _source, value in kwargs_candidates}) > 1:
        errors.append("sample_weight_kwargs_conflict:" + ",".join(source for source, _value in kwargs_candidates))
    kwargs = dict(kwargs_candidates[0][1]) if kwargs_candidates else dict(DEFAULT_SAMPLE_WEIGHT_KWARGS)
    return policy, kwargs


def _normalize_model_kwargs(exp: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    qlib_raw = _strip_sample_weight_fields(exp.get("qlib_model_kwargs") if isinstance(exp.get("qlib_model_kwargs"), dict) else {})
    training_raw = _strip_sample_weight_fields(exp.get("training_hyperparameters") if isinstance(exp.get("training_hyperparameters"), dict) else {})
    try:
        qlib_params = normalize_lgbm_training_params(qlib_raw)
        training_params = normalize_lgbm_training_params(training_raw)
    except Exception as exc:
        errors.append(f"model_kwargs_invalid:{exc}")
        return {}
    if qlib_raw and training_raw:
        for key, q_value in qlib_params.items():
            if key in training_params and training_params.get(key) != q_value:
                errors.append(f"model_kwargs_conflict:{key}:qlib={q_value},training={training_params.get(key)}")
    merged = dict(DEFAULT_QLIB_MODEL_KWARGS)
    merged.update(qlib_params)
    merged.update(training_params)
    try:
        return normalize_lgbm_training_params(merged)
    except Exception as exc:
        errors.append(f"model_kwargs_invalid:{exc}")
        return {}


def validate_experiment_contract(experiment: dict[str, Any]) -> dict[str, Any]:
    exp = dict(experiment or {})
    errors: list[str] = []
    warnings: list[str] = []
    for deprecated in ("primary_portfolio", "secondary_portfolio"):
        if deprecated in exp:
            errors.append(f"deprecated_dual_portfolio_field:{deprecated}")
    feature_missing_strategy = str(exp.get("feature_missing_strategy") or DEFAULT_FEATURE_MISSING_STRATEGY)
    sample_weight_policy, sample_weight_kwargs = _extract_sample_weight(exp, errors)
    if feature_missing_strategy not in FEATURE_MISSING_STRATEGIES:
        errors.append(f"unsupported_feature_missing_strategy:{feature_missing_strategy}")
    if sample_weight_policy not in SAMPLE_WEIGHT_POLICIES:
        errors.append(f"unsupported_sample_weight_policy:{sample_weight_policy}")
    if exp.get("label_forward_period") not in (None, MODEL_DEFAULT_FORWARD_PERIOD):
        errors.append(f"label_forward_period_mismatch:{exp.get('label_forward_period')}")
    if exp.get("factor_holding_period_days") not in (None, MODEL_DEFAULT_FACTOR_HOLDING_PERIOD):
        errors.append(f"factor_holding_period_days_mismatch:{exp.get('factor_holding_period_days')}")
    if str(exp.get("label_execution_deal_price") or "open") != "open":
        errors.append("label_execution_deal_price_must_be_open")
    if str(exp.get("deal_price") or "open") != "open":
        errors.append("backtest_deal_price_must_be_open")
    if exp.get("pre_shift_pred") is True:
        errors.append("pred_pkl_must_not_be_pre_shifted")
    if str(exp.get("model_class") or QLIB_REQUIRED_MODEL_CLASS) != QLIB_REQUIRED_MODEL_CLASS:
        errors.append(f"model_class_mismatch:{exp.get('model_class')}!=expected:{QLIB_REQUIRED_MODEL_CLASS}")
    if str(exp.get("model_module") or QLIB_REQUIRED_MODEL_MODULE) != QLIB_REQUIRED_MODEL_MODULE:
        errors.append(f"model_module_mismatch:{exp.get('model_module')}!=expected:{QLIB_REQUIRED_MODEL_MODULE}")
    errors.extend(_portfolio_errors(exp))
    errors.extend(_processor_errors(exp.get("qlib_processors")))
    errors.extend(_seed_policy_errors(exp))
    model_kwargs = _normalize_model_kwargs(exp, errors)
    portfolio = _normalized_portfolio(exp)
    segments = exp.get("segments") if isinstance(exp.get("segments"), dict) else default_segments()
    if exp.get("strict_r1_baseline") is True:
        expected = normalize_lgbm_training_params(DEFAULT_QLIB_MODEL_KWARGS)
        for key, expected_value in expected.items():
            if key not in model_kwargs:
                errors.append(f"r1_model_param_missing:{key}")
            elif model_kwargs.get(key) != expected_value:
                errors.append(f"r1_model_param_mismatch:{key}={model_kwargs.get(key)}!=expected:{expected_value}")
    if sample_weight_policy == "top50_smooth2_bottom50_smooth1p5_mean_norm":
        kwargs = dict(DEFAULT_SAMPLE_WEIGHT_KWARGS)
        kwargs.update(sample_weight_kwargs or {})
        for key, expected in DEFAULT_SAMPLE_WEIGHT_KWARGS.items():
            if kwargs.get(key) != expected:
                errors.append(f"sample_weight_{key}_mismatch:{kwargs.get(key)}!=expected:{expected}")
        sample_weight_kwargs = kwargs
    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "normalized": {
            **exp,
            "feature_missing_strategy": feature_missing_strategy,
            "sample_weight_policy": sample_weight_policy,
            "sample_weight_kwargs": sample_weight_kwargs,
            "effective_sample_weight_policy": DEFAULT_SAMPLE_WEIGHT_POLICY,
            "baseline_kind": exp.get("baseline_kind") or MODEL_R1_BASELINE_KIND,
            "model_policy": exp.get("model_policy") or "qlib_lgbm_canonical",
            "model_family": exp.get("model_family") or "lgbm",
            "model_class": QLIB_REQUIRED_MODEL_CLASS,
            "model_module": QLIB_REQUIRED_MODEL_MODULE,
            "qlib_model_kwargs": model_kwargs,
            "training_hyperparameters": model_kwargs,
            "portfolio": portfolio,
            "benchmark": exp.get("benchmark") or MODEL_DEFAULT_BENCHMARK,
            "deal_price": "open",
            "limit_threshold": list(LIMIT_THRESHOLD),
            "forbid_all_trade_at_limit": False,
            "segments": segments,
            "qlib_processors": exp.get("qlib_processors") or production_contract()["processor_chain"],
            **LABEL_CONTRACT,
        },
    }
