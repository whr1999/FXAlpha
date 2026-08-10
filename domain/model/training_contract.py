from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

from storage.paths import (
    MODEL_DEFAULT_BENCHMARK,
    MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    MODEL_DEFAULT_N_DROP,
    MODEL_DEFAULT_TOPK,
)


R1_BASELINE_KIND = "qlib_official_alpha158_lgbm_fxalpha_top20_drop2_hold5"

QLIB_OFFICIAL_ALPHA158_LGBM_PARAMS: dict[str, Any] = {
    "loss": "mse",
    "learning_rate": 0.2,
    "lr": 0.2,
    "feature_fraction": 0.8879,
    "bagging_fraction": 0.8789,
    "lambda_l1": 205.6999,
    "lambda_l2": 580.9768,
    "max_depth": 8,
    "num_leaves": 210,
    "n_estimators": 1000,
    "early_stopping_rounds": 50,
    "min_data_in_leaf": 20,
    "boosting_type": "gbdt",
    "seed": 42,
    "n_jobs": 4,
    "verbosity": -1,
}

QLIB_REQUIRED_PROCESSORS = {
    "ProcessInf",
    "RobustZScoreNorm",
    "CSZFillna",
    "DropnaLabel",
    "CSZScoreNorm",
}

QLIB_REQUIRED_MODEL_KEYS = {
    "loss",
    "learning_rate",
    "n_estimators",
    "early_stopping_rounds",
    "num_leaves",
    "max_depth",
    "feature_fraction",
    "bagging_fraction",
    "lambda_l1",
    "lambda_l2",
    "min_data_in_leaf",
    "seed",
    "n_jobs",
}

QLIB_REQUIRED_MODEL_CLASS = "FXAlphaWeightedLGBModel"
QLIB_REQUIRED_MODEL_MODULE = "domain.model.reweight"
QLIB_REQUIRED_SAMPLE_WEIGHT_POLICY = "top50_smooth2_bottom50_smooth1p5_mean_norm"
QLIB_REQUIRED_SAMPLE_WEIGHT_KWARGS = {
    "top_n": 50,
    "top_max": 2.0,
    "bottom_n": 50,
    "bottom_max": 1.5,
    "normalize_mean": True,
}
QLIB_REQUIRED_DEAL_PRICE = "open"
QLIB_REQUIRED_LIMIT_THRESHOLD = ["$limit_buy_open_sealed", "$limit_sell_open_sealed"]
QLIB_REQUIRED_FORBID_ALL_TRADE_AT_LIMIT = False
QLIB_REQUIRED_STRATEGY_CLASS = "FXAlphaTopkDropoutStrategy"
QLIB_REQUIRED_STRATEGY_MODULE = "domain.model.qlib_strategy"
QLIB_REQUIRED_PORT_ANALYSIS_CLASS = "PortAnaRecord"
QLIB_REQUIRED_PORT_ANALYSIS_MODULE = "qlib.workflow.record_temp"
QLIB_REQUIRED_HOLD_THRESH = MODEL_DEFAULT_FACTOR_HOLDING_PERIOD

QLIB_EXPECTED_PROCESSOR_CHAIN = {
    "infer_processors": [
        {"class": "ProcessInf", "kwargs": {}},
        {"class": "RobustZScoreNorm", "kwargs": {"fields_group": "feature", "clip_outlier": True}},
        {"class": "CSZFillna", "kwargs": {"fields_group": "feature"}},
    ],
    "learn_processors": [
        {"class": "DropnaLabel", "kwargs": {"fields_group": "label"}},
        {"class": "CSZScoreNorm", "kwargs": {"fields_group": "label", "method": "zscore"}},
    ],
}

QLIB_CANONICAL_PROCESSORS = {
    "infer_processors": [
        "ProcessInf",
        "RobustZScoreNorm(fields_group=feature, clip_outlier=True)",
        "CSZFillna(fields_group=feature)",
    ],
    "learn_processors": ["DropnaLabel", "CSZScoreNorm(fields_group=label)"],
}

LGBM_PARAM_ALIASES = {
    "colsample_bytree": "feature_fraction",
    "subsample": "bagging_fraction",
    "num_threads": "n_jobs",
    "objective": "loss",
    "learning_rate": "lr",
}


def _coerce_number(value: Any) -> Any:
    if isinstance(value, (int, float)) or value is None:
        return value
    text = str(value).strip()
    if not text:
        return value
    try:
        if re.fullmatch(r"[-+]?\d+", text):
            return int(text)
        return float(text)
    except Exception:
        return value


def normalize_lgbm_training_params(params: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(params or {})
    for source, target in LGBM_PARAM_ALIASES.items():
        if source not in normalized or normalized.get(source) is None:
            continue
        if target in normalized and normalized.get(target) is not None:
            source_value = _coerce_number(normalized[source])
            target_value = _coerce_number(normalized[target])
            if source_value != target_value:
                raise ValueError(f"{source} and {target} conflict; provide only one value or make them equal")
        normalized[target] = normalized[source]

    if normalized.get("loss") is None:
        normalized["loss"] = "mse"
    normalized["loss"] = str(normalized["loss"]).lower()
    if normalized["loss"] not in {"mse", "binary"}:
        raise ValueError("unsupported Qlib LGBModel loss=%r; supported values are: binary, mse" % normalized["loss"])

    for key in (
        "learning_rate",
        "lr",
        "feature_fraction",
        "bagging_fraction",
        "lambda_l1",
        "lambda_l2",
    ):
        if key in normalized:
            normalized[key] = _coerce_number(normalized[key])
    for key in (
        "max_depth",
        "num_leaves",
        "n_estimators",
        "early_stopping_rounds",
        "min_data_in_leaf",
        "bagging_freq",
        "seed",
        "n_jobs",
        "num_threads",
    ):
        if key in normalized and normalized[key] is not None:
            normalized[key] = int(_coerce_number(normalized[key]))

    if normalized.get("learning_rate") is None and normalized.get("lr") is not None:
        normalized["learning_rate"] = normalized["lr"]
    if normalized.get("lr") is None and normalized.get("learning_rate") is not None:
        normalized["lr"] = normalized["learning_rate"]
    return normalized


def audit_lgbm_task_model_kwargs(expected_params: dict[str, Any] | None, actual_kwargs: dict[str, Any] | None) -> dict[str, Any]:
    expected = normalize_lgbm_training_params(expected_params or {})
    actual = {key: _coerce_number(value) for key, value in dict(actual_kwargs or {}).items()}
    checks = {
        "loss": expected.get("loss"),
        "learning_rate": expected.get("learning_rate") if expected.get("learning_rate") is not None else expected.get("lr"),
        "n_estimators": expected.get("n_estimators"),
        "early_stopping_rounds": expected.get("early_stopping_rounds"),
        "num_leaves": expected.get("num_leaves"),
        "max_depth": expected.get("max_depth"),
        "feature_fraction": expected.get("feature_fraction"),
        "bagging_fraction": expected.get("bagging_fraction"),
        "bagging_freq": expected.get("bagging_freq"),
        "lambda_l1": expected.get("lambda_l1"),
        "lambda_l2": expected.get("lambda_l2"),
        "min_data_in_leaf": expected.get("min_data_in_leaf"),
        "boosting_type": expected.get("boosting_type"),
        "seed": expected.get("seed"),
        "n_jobs": expected.get("n_jobs"),
    }
    mismatches: list[str] = []
    for key, expected_value in checks.items():
        if expected_value is None:
            continue
        if key not in actual:
            mismatches.append(f"{key}_missing:expected={expected_value}")
            continue
        actual_value = actual.get(key)
        if isinstance(expected_value, (int, float)) and isinstance(actual_value, (int, float)):
            if abs(float(actual_value) - float(expected_value)) > 1e-12:
                mismatches.append(f"{key}_mismatch:artifact={actual_value},expected={expected_value}")
        elif str(actual_value) != str(expected_value):
            mismatches.append(f"{key}_mismatch:artifact={actual_value},expected={expected_value}")
    return {
        "passed": not mismatches,
        "mismatches": mismatches,
        "expected_training_params": {key: value for key, value in checks.items() if value is not None},
        "artifact_model_kwargs": actual,
    }


def audit_qlib_task_portfolio_config(
    expected_portfolio: dict[str, Any] | None,
    actual_config: dict[str, Any] | None,
) -> dict[str, Any]:
    expected = dict(expected_portfolio or {})
    actual = dict(actual_config or {})
    strategy = actual.get("strategy")
    backtest = actual.get("backtest")
    strategy_kwargs = strategy.get("kwargs") if isinstance(strategy, dict) else {}
    exchange_kwargs = backtest.get("exchange_kwargs") if isinstance(backtest, dict) else {}
    strategy_kwargs = strategy_kwargs if isinstance(strategy_kwargs, dict) else {}
    exchange_kwargs = exchange_kwargs if isinstance(exchange_kwargs, dict) else {}
    strategy_class = str(strategy.get("class") or "") if isinstance(strategy, dict) else ""
    strategy_module = str(strategy.get("module_path") or "") if isinstance(strategy, dict) else ""

    expected_topk = int(expected.get("topk", MODEL_DEFAULT_TOPK))
    expected_n_drop = int(expected.get("n_drop", MODEL_DEFAULT_N_DROP))
    expected_hold_thresh = int(expected.get("hold_thresh", QLIB_REQUIRED_HOLD_THRESH))
    expected_benchmark = str(expected.get("benchmark") or MODEL_DEFAULT_BENCHMARK)
    expected_deal_price = str(expected.get("deal_price") or QLIB_REQUIRED_DEAL_PRICE)
    expected_limit_threshold = list(expected.get("limit_threshold") or QLIB_REQUIRED_LIMIT_THRESHOLD)
    expected_forbid_all = bool(
        expected.get("forbid_all_trade_at_limit", QLIB_REQUIRED_FORBID_ALL_TRADE_AT_LIMIT)
    )

    mismatches: list[str] = []
    if not actual:
        mismatches.append("port_analysis_config_missing")
    if not isinstance(strategy, dict):
        mismatches.append("strategy_config_missing")
    if not isinstance(backtest, dict):
        mismatches.append("backtest_config_missing")
    if strategy_class != QLIB_REQUIRED_STRATEGY_CLASS:
        mismatches.append(f"strategy_class_mismatch:artifact={strategy_class},expected={QLIB_REQUIRED_STRATEGY_CLASS}")
    if strategy_module != QLIB_REQUIRED_STRATEGY_MODULE:
        mismatches.append(f"strategy_module_mismatch:artifact={strategy_module},expected={QLIB_REQUIRED_STRATEGY_MODULE}")

    actual_topk = strategy_kwargs.get("topk")
    if actual_topk is None:
        mismatches.append(f"topk_missing:expected={expected_topk}")
    elif int(_coerce_number(actual_topk)) != expected_topk:
        mismatches.append(f"topk_mismatch:artifact={actual_topk},expected={expected_topk}")

    actual_n_drop = strategy_kwargs.get("n_drop")
    if actual_n_drop is None:
        mismatches.append(f"n_drop_missing:expected={expected_n_drop}")
    elif int(_coerce_number(actual_n_drop)) != expected_n_drop:
        mismatches.append(f"n_drop_mismatch:artifact={actual_n_drop},expected={expected_n_drop}")

    actual_hold_thresh = strategy_kwargs.get("hold_thresh")
    if actual_hold_thresh is None:
        mismatches.append(f"hold_thresh_missing:expected={expected_hold_thresh}")
    elif int(_coerce_number(actual_hold_thresh)) != expected_hold_thresh:
        mismatches.append(f"hold_thresh_mismatch:artifact={actual_hold_thresh},expected={expected_hold_thresh}")

    if "forbid_all_trade_at_limit" not in strategy_kwargs:
        mismatches.append(f"forbid_all_trade_at_limit_missing:expected={expected_forbid_all}")
    elif bool(strategy_kwargs.get("forbid_all_trade_at_limit")) != expected_forbid_all:
        mismatches.append(
            "forbid_all_trade_at_limit_mismatch:"
            f"artifact={strategy_kwargs.get('forbid_all_trade_at_limit')},expected={expected_forbid_all}"
        )

    actual_benchmark = str((backtest or {}).get("benchmark") or "")
    if actual_benchmark != expected_benchmark:
        mismatches.append(f"benchmark_mismatch:artifact={actual_benchmark},expected={expected_benchmark}")

    actual_deal_price = exchange_kwargs.get("deal_price")
    normalized_deal_price = str(actual_deal_price or "").strip().lstrip("$")
    if normalized_deal_price != expected_deal_price:
        mismatches.append(f"deal_price_mismatch:artifact={actual_deal_price},expected={expected_deal_price}")

    actual_limit_threshold = exchange_kwargs.get("limit_threshold")
    if not isinstance(actual_limit_threshold, (list, tuple)) or list(actual_limit_threshold) != expected_limit_threshold:
        mismatches.append(
            f"limit_threshold_mismatch:artifact={actual_limit_threshold},expected={expected_limit_threshold}"
        )

    return {
        "passed": not mismatches,
        "mismatches": mismatches,
        "expected_portfolio_params": {
            "topk": expected_topk,
            "n_drop": expected_n_drop,
            "hold_thresh": expected_hold_thresh,
            "benchmark": expected_benchmark,
            "deal_price": expected_deal_price,
            "limit_threshold": expected_limit_threshold,
            "forbid_all_trade_at_limit": expected_forbid_all,
            "strategy_class": QLIB_REQUIRED_STRATEGY_CLASS,
            "strategy_module": QLIB_REQUIRED_STRATEGY_MODULE,
        },
        "artifact_strategy_kwargs": dict(strategy_kwargs),
        "artifact_exchange_kwargs": dict(exchange_kwargs),
        "artifact_backtest_config": dict(backtest) if isinstance(backtest, dict) else {},
    }


def r1_official_lgbm_params() -> dict[str, Any]:
    return dict(QLIB_OFFICIAL_ALPHA158_LGBM_PARAMS)


def fxalpha_default_sample_weight_params() -> dict[str, Any]:
    return {
        "sample_weight_policy": QLIB_REQUIRED_SAMPLE_WEIGHT_POLICY,
        "sample_weight_kwargs": dict(QLIB_REQUIRED_SAMPLE_WEIGHT_KWARGS),
    }


def fxalpha_default_portfolio_params() -> dict[str, Any]:
    return {
        "topk": MODEL_DEFAULT_TOPK,
        "n_drop": MODEL_DEFAULT_N_DROP,
        "hold_thresh": QLIB_REQUIRED_HOLD_THRESH,
        "portfolio": {"topk": MODEL_DEFAULT_TOPK, "n_drop": MODEL_DEFAULT_N_DROP, "hold_thresh": QLIB_REQUIRED_HOLD_THRESH},
        "portfolio.topk": MODEL_DEFAULT_TOPK,
        "portfolio.n_drop": MODEL_DEFAULT_N_DROP,
        "portfolio.hold_thresh": QLIB_REQUIRED_HOLD_THRESH,
        "benchmark": MODEL_DEFAULT_BENCHMARK,
    }


def apply_r1_official_baseline(experiment_json: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(experiment_json, ensure_ascii=False, default=str))
    params = r1_official_lgbm_params()
    sample_weight = fxalpha_default_sample_weight_params()
    portfolio = fxalpha_default_portfolio_params()

    def patch_task(task: dict[str, Any]) -> dict[str, Any]:
        patched = dict(task)
        patched["training_hyperparameters"] = {
            **dict(patched.get("training_hyperparameters") or {}),
            **params,
            **sample_weight,
        }
        patched["qlib_model_kwargs"] = {
            **dict(patched.get("qlib_model_kwargs") or patched.get("model_kwargs") or {}),
            **params,
            **sample_weight,
        }
        hyper = dict(patched.get("hyperparameters") or {})
        hyper.update(portfolio)
        patched["hyperparameters"] = hyper
        patched["portfolio"] = portfolio["portfolio"]
        patched["benchmark"] = MODEL_DEFAULT_BENCHMARK
        patched["baseline_kind"] = R1_BASELINE_KIND
        return patched

    if isinstance(payload.get("tasks"), dict):
        payload["tasks"] = {name: patch_task(task) if isinstance(task, dict) else task for name, task in payload["tasks"].items()}
    elif isinstance(payload.get("tasks"), list):
        payload["tasks"] = [patch_task(task) if isinstance(task, dict) else task for task in payload["tasks"]]
    elif any(k in payload for k in ("training_hyperparameters", "qlib_model_kwargs", "model_kwargs", "portfolio", "hypothesis_rationale")):
        payload.update(patch_task(payload))
    else:
        metadata_keys = {
            "feature_set_id",
            "policy",
            "model_policy",
            "model_family",
            "train",
            "valid",
            "test",
            "benchmark",
        }
        patched_any = False
        for key, value in list(payload.items()):
            if key in metadata_keys or not isinstance(value, dict):
                continue
            payload[key] = patch_task(value)
            patched_any = True
        if not patched_any:
            payload.update(patch_task(payload))
    payload["baseline_kind"] = R1_BASELINE_KIND
    payload["r1_baseline_note"] = (
        "R1 uses Qlib official Alpha158 LightGBM model parameters while keeping "
        "FXAlpha local universe/window/portfolio: one top20/drop2/hold5 open-execution backtest."
    )
    return payload


def _extract_scalar(text: str, key: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(key)}:\s*([^#\n]+?)\s*$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _extract_segment(text: str, key: str) -> list[str] | None:
    match = re.search(
        rf"^\s*{re.escape(key)}:\s*\[(\d{{4}}-\d{{2}}-\d{{2}}),\s*(\d{{4}}-\d{{2}}-\d{{2}})\]\s*$",
        text,
        flags=re.MULTILINE,
    )
    if not match:
        return None
    return [match.group(1), match.group(2)]


def _extract_limit_threshold(text: str, parsed_config: dict[str, Any]) -> Any:
    exchange_kwargs = (
        ((parsed_config.get("port_analysis_config") or {}).get("backtest") or {}).get("exchange_kwargs")
        if isinstance(parsed_config, dict)
        else {}
    )
    if isinstance(exchange_kwargs, dict) and "limit_threshold" in exchange_kwargs:
        return exchange_kwargs.get("limit_threshold")
    raw = _extract_scalar(text, "limit_threshold")
    if raw is None:
        return None
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [item.strip().strip('"').strip("'") for item in raw[1:-1].split(",")]
    return raw


def _extract_deal_price(text: str, parsed_config: dict[str, Any]) -> Any:
    exchange_kwargs = (
        ((parsed_config.get("port_analysis_config") or {}).get("backtest") or {}).get("exchange_kwargs")
        if isinstance(parsed_config, dict)
        else {}
    )
    if isinstance(exchange_kwargs, dict) and "deal_price" in exchange_kwargs:
        return exchange_kwargs.get("deal_price")
    raw = _extract_scalar(text, "deal_price")
    return raw.strip().strip('"').strip("'") if raw is not None else None


def _extract_strategy_kwargs(parsed_config: dict[str, Any]) -> dict[str, Any]:
    port_config = parsed_config.get("port_analysis_config") if isinstance(parsed_config, dict) else {}
    strategy = (port_config or {}).get("strategy") if isinstance(port_config, dict) else {}
    kwargs = (strategy or {}).get("kwargs") if isinstance(strategy, dict) else {}
    return kwargs if isinstance(kwargs, dict) else {}


def _extract_forbid_all_trade_at_limit(text: str, parsed_config: dict[str, Any]) -> Any:
    strategy_kwargs = _extract_strategy_kwargs(parsed_config)
    if "forbid_all_trade_at_limit" in strategy_kwargs:
        return strategy_kwargs.get("forbid_all_trade_at_limit")
    raw = _extract_scalar(text, "forbid_all_trade_at_limit")
    if raw is None:
        return None
    normalized = raw.strip().strip('"').strip("'").lower()
    if normalized in {"false", "0", "no"}:
        return False
    if normalized in {"true", "1", "yes"}:
        return True
    return raw


def _extract_record_entries(parsed_config: dict[str, Any]) -> list[dict[str, Any]]:
    task = parsed_config.get("task") if isinstance(parsed_config, dict) else {}
    records = (task or {}).get("record") if isinstance(task, dict) else []
    return records if isinstance(records, list) else []


def _env_number(env: dict[str, Any], key: str, default: Any = None) -> Any:
    value = env.get(key, default)
    return _coerce_number(value)


def _resolve_config_number(raw: str | None, env: dict[str, Any], env_key: str, default: Any = None) -> Any:
    if raw is None:
        return None
    text = str(raw).strip().strip('"').strip("'")
    if "{{" in text and "}}" in text:
        if env.get(env_key) is not None:
            return _coerce_number(env.get(env_key))
        match = re.search(r"default\(([-+]?\d+(?:\.\d+)?)\)", text)
        if match:
            return _coerce_number(match.group(1))
        return default
    return _coerce_number(text)


def _load_yaml_config(text: str) -> tuple[dict[str, Any], str | None]:
    try:
        parsed = yaml.safe_load(text) if text else {}
    except Exception as exc:
        return {}, f"yaml_parse_error:{exc.__class__.__name__}:{exc}"
    if parsed is None:
        return {}, None
    if not isinstance(parsed, dict):
        return {}, "yaml_root_not_mapping"
    return parsed, None


def enforce_required_limit_threshold_text(text: str) -> tuple[str, bool]:
    """Normalize formal Qlib model configs to provider-backed open-price execution."""

    required = '["$limit_buy_open_sealed", "$limit_sell_open_sealed"]'
    required_deal_price = QLIB_REQUIRED_DEAL_PRICE
    required_forbid_all = "false"
    required_hold_thresh = str(QLIB_REQUIRED_HOLD_THRESH)
    lines = text.splitlines()
    rewritten: list[str] = []
    changed = False
    in_exchange_kwargs = False
    exchange_indent = -1
    exchange_child_indent: int | None = None
    inserted_limit = False
    inserted_deal_price = False
    pending_port_record_module = False
    pending_topk_strategy = False
    pending_strategy_module = False
    strategy_indent = -1
    in_topk_kwargs = False
    topk_kwargs_indent = -1
    topk_kwargs_child_indent: int | None = None
    inserted_forbid_all = False
    inserted_hold_thresh = False

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))

        if in_topk_kwargs and stripped and indent <= topk_kwargs_indent:
            child_indent = topk_kwargs_child_indent if topk_kwargs_child_indent is not None else topk_kwargs_indent + 2
            if not inserted_forbid_all:
                rewritten.append(" " * child_indent + f"forbid_all_trade_at_limit: {required_forbid_all}")
                changed = True
                inserted_forbid_all = True
            if not inserted_hold_thresh:
                rewritten.append(" " * child_indent + f"hold_thresh: {required_hold_thresh}")
                changed = True
                inserted_hold_thresh = True
            in_topk_kwargs = False

        if in_exchange_kwargs and stripped and indent <= exchange_indent:
            child_indent = exchange_child_indent if exchange_child_indent is not None else exchange_indent + 2
            if not inserted_limit:
                rewritten.append(" " * child_indent + f"limit_threshold: {required}")
                changed = True
                inserted_limit = True
            if not inserted_deal_price:
                rewritten.append(" " * child_indent + f"deal_price: {required_deal_price}")
                changed = True
                inserted_deal_price = True
            in_exchange_kwargs = False

        if stripped.startswith("exchange_kwargs:"):
            in_exchange_kwargs = True
            exchange_indent = indent
            exchange_child_indent = None
            inserted_limit = False
            inserted_deal_price = False
            rewritten.append(line)
            continue

        if re.match(r"^\s*-\s*class:\s*(PortAnaRecord|FXAlphaOpenNextPortAnaRecord)\s*$", line):
            old_class = stripped.split(":", 1)[1].strip()
            prefix = line[: line.find(old_class)]
            new_line = f"{prefix}{QLIB_REQUIRED_PORT_ANALYSIS_CLASS}"
            rewritten.append(new_line)
            changed = changed or new_line != line
            pending_port_record_module = True
            continue

        if re.match(r"^\s*class:\s*(TopkDropoutStrategy|FXAlphaTopkDropoutStrategy)\s*$", line):
            old_class = stripped.split(":", 1)[1].strip()
            prefix = line[: line.find(old_class)]
            new_line = f"{prefix}{QLIB_REQUIRED_STRATEGY_CLASS}"
            pending_topk_strategy = True
            pending_strategy_module = True
            strategy_indent = indent
            rewritten.append(new_line)
            changed = changed or new_line != line
            continue

        if pending_strategy_module and re.match(r"^\s*module_path:\s*.*$", line):
            new_line = " " * indent + f"module_path: {QLIB_REQUIRED_STRATEGY_MODULE}"
            rewritten.append(new_line)
            changed = changed or new_line != line
            pending_strategy_module = False
            continue

        if pending_topk_strategy and re.match(r"^\s*kwargs:\s*$", line):
            if pending_strategy_module:
                rewritten.append(" " * (strategy_indent + 2) + f"module_path: {QLIB_REQUIRED_STRATEGY_MODULE}")
                changed = True
                pending_strategy_module = False
            in_topk_kwargs = True
            topk_kwargs_indent = indent
            topk_kwargs_child_indent = None
            inserted_forbid_all = False
            inserted_hold_thresh = False
            pending_topk_strategy = False
            rewritten.append(line)
            continue

        if pending_port_record_module and re.match(r"^\s*module_path:\s*.*$", line):
            new_line = " " * indent + f"module_path: {QLIB_REQUIRED_PORT_ANALYSIS_MODULE}"
            rewritten.append(new_line)
            changed = changed or new_line != line
            pending_port_record_module = False
            continue

        if in_exchange_kwargs and re.match(r"^\s*limit_threshold:\s*.*$", line):
            exchange_child_indent = indent
            new_line = " " * indent + f"limit_threshold: {required}"
            rewritten.append(new_line)
            changed = changed or new_line != line
            inserted_limit = True
            continue

        if in_exchange_kwargs and re.match(r"^\s*deal_price:\s*.*$", line):
            exchange_child_indent = indent
            new_line = " " * indent + f"deal_price: {required_deal_price}"
            rewritten.append(new_line)
            changed = changed or new_line != line
            inserted_deal_price = True
            continue

        if in_topk_kwargs and re.match(r"^\s*forbid_all_trade_at_limit:\s*.*$", line):
            topk_kwargs_child_indent = indent
            new_line = " " * indent + f"forbid_all_trade_at_limit: {required_forbid_all}"
            rewritten.append(new_line)
            changed = changed or new_line != line
            inserted_forbid_all = True
            continue

        if in_topk_kwargs and re.match(r"^\s*hold_thresh:\s*.*$", line):
            topk_kwargs_child_indent = indent
            new_line = " " * indent + f"hold_thresh: {required_hold_thresh}"
            rewritten.append(new_line)
            changed = changed or new_line != line
            inserted_hold_thresh = True
            continue

        if in_topk_kwargs and stripped:
            topk_kwargs_child_indent = indent

        rewritten.append(line)

    if in_topk_kwargs:
        child_indent = topk_kwargs_child_indent if topk_kwargs_child_indent is not None else topk_kwargs_indent + 2
        if not inserted_forbid_all:
            rewritten.append(" " * child_indent + f"forbid_all_trade_at_limit: {required_forbid_all}")
            changed = True
        if not inserted_hold_thresh:
            rewritten.append(" " * child_indent + f"hold_thresh: {required_hold_thresh}")
            changed = True

    if in_exchange_kwargs:
        child_indent = exchange_child_indent if exchange_child_indent is not None else exchange_indent + 2
        if not inserted_limit:
            rewritten.append(" " * child_indent + f"limit_threshold: {required}")
            changed = True
        if not inserted_deal_price:
            rewritten.append(" " * child_indent + f"deal_price: {required_deal_price}")
            changed = True

    final = "\n".join(rewritten) + ("\n" if text.endswith("\n") or changed else "")
    return final, changed


def _processor_class(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("class") or "")
    return ""


def _processor_kwargs(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    kwargs = entry.get("kwargs") or {}
    return kwargs if isinstance(kwargs, dict) else {}


def _processor_chain(config: dict[str, Any], key: str) -> list[Any]:
    handler = config.get("data_handler_config")
    if not isinstance(handler, dict):
        return []
    chain = handler.get(key) or []
    return chain if isinstance(chain, list) else []


def _values_equal(actual: Any, expected: Any) -> bool:
    actual_value = _coerce_number(actual)
    expected_value = _coerce_number(expected)
    if isinstance(actual_value, (int, float)) and isinstance(expected_value, (int, float)):
        return abs(float(actual_value) - float(expected_value)) <= 1e-12
    return actual_value == expected_value


def audit_processor_chain(config: dict[str, Any]) -> dict[str, Any]:
    """Audit the exact Qlib processor contract used by the canonical model lane."""

    violations: list[str] = []
    resolved: dict[str, list[dict[str, Any]]] = {}
    for chain_key, expected_chain in QLIB_EXPECTED_PROCESSOR_CHAIN.items():
        actual_chain = _processor_chain(config, chain_key)
        resolved[chain_key] = [
            {"class": _processor_class(entry), "kwargs": _processor_kwargs(entry)}
            for entry in actual_chain
        ]
        actual_classes = [_processor_class(entry) for entry in actual_chain]
        expected_classes = [str(item["class"]) for item in expected_chain]
        if actual_classes != expected_classes:
            violations.append(
                f"{chain_key}_order_mismatch:file={actual_classes},expected={expected_classes}"
            )
            continue
        for idx, expected in enumerate(expected_chain):
            kwargs = _processor_kwargs(actual_chain[idx])
            expected_kwargs = dict(expected.get("kwargs") or {})
            for key, expected_value in expected_kwargs.items():
                if key == "fields_group" and _processor_class(actual_chain[idx]) == "DropnaLabel" and key not in kwargs:
                    continue
                if key == "method" and _processor_class(actual_chain[idx]) == "CSZScoreNorm" and key not in kwargs:
                    continue
                if key not in kwargs:
                    violations.append(
                        f"{chain_key}[{idx}].{key}_missing:expected={expected_value}"
                    )
                    continue
                if not _values_equal(kwargs.get(key), expected_value):
                    violations.append(
                        f"{chain_key}[{idx}].{key}_mismatch:file={kwargs.get(key)},expected={expected_value}"
                    )
    return {"passed": not violations, "violations": violations, "resolved": resolved}


def render_qlib_config_template(config_path: str | Path, run_env: dict[str, Any]) -> dict[str, Any]:
    """Render a workspace Qlib YAML template in place before qrun.

    Qlib executes the rendered workspace YAML directly. FXAlpha therefore
    resolves template variables before qrun sees the final configuration.
    """

    path = Path(config_path)
    if not path.exists():
        return {
            "rendered": False,
            "config_path": str(path),
            "template_backup_path": "",
            "error": "config_file_missing",
        }
    original = path.read_text(encoding="utf-8")
    has_template = "{{" in original or "{%" in original
    if not has_template:
        patched, patched_limit_threshold = enforce_required_limit_threshold_text(original)
        if patched_limit_threshold:
            path.write_text(patched, encoding="utf-8")
        return {
            "rendered": False,
            "config_path": str(path),
            "template_backup_path": "",
            "unresolved_tokens": [],
            "contract_patches": {
                "limit_threshold": patched_limit_threshold,
            },
        }
    backup_path = path.with_suffix(path.suffix + ".template")
    if not backup_path.exists():
        backup_path.write_text(original, encoding="utf-8")
    try:
        rendered = Environment(undefined=StrictUndefined).from_string(original).render(**{k: v for k, v in (run_env or {}).items()})
    except Exception as exc:
        return {
            "rendered": False,
            "config_path": str(path),
            "template_backup_path": str(backup_path),
            "unresolved_tokens": sorted(set(re.findall(r"{{.*?}}|{%.*?%}", original, flags=re.DOTALL))),
            "error": f"template_render_failed:{exc}",
        }
    unresolved = sorted(set(re.findall(r"{{.*?}}|{%.*?%}", rendered, flags=re.DOTALL)))
    rendered, patched_limit_threshold = enforce_required_limit_threshold_text(rendered)
    path.write_text(rendered, encoding="utf-8")
    return {
        "rendered": True,
        "config_path": str(path),
        "template_backup_path": str(backup_path),
        "unresolved_tokens": unresolved,
        "contract_patches": {
            "limit_threshold": patched_limit_threshold,
        },
    }


def audit_workspace_model_config(
    *,
    config_path: str | Path,
    run_env: dict[str, Any],
    windows: dict[str, Any],
    expected_feature_count: int | None = None,
) -> dict[str, Any]:
    path = Path(config_path)
    violations: list[str] = []
    warnings: list[str] = []
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if not text:
        violations.append("config_file_missing_or_empty")
    parsed_config, yaml_error = _load_yaml_config(text)
    if yaml_error:
        violations.append(yaml_error)
    unresolved_templates = sorted(set(re.findall(r"{{.*?}}|{%.*?%}", text, flags=re.DOTALL)))
    if unresolved_templates:
        violations.append("unresolved_template_tokens")

    processors = {
        _processor_class(entry)
        for chain_key in ("infer_processors", "learn_processors")
        for entry in _processor_chain(parsed_config, chain_key)
        if _processor_class(entry)
    }
    if not processors:
        processors = set(re.findall(r"class:\s*([A-Za-z_][A-Za-z0-9_]*)", text))
    missing_processors = sorted(QLIB_REQUIRED_PROCESSORS - processors)
    if missing_processors:
        violations.append("missing_processors:" + ",".join(missing_processors))
    processor_audit = audit_processor_chain(parsed_config)
    violations.extend(processor_audit["violations"])

    missing_model_keys = sorted(key for key in QLIB_REQUIRED_MODEL_KEYS if key not in text)
    if missing_model_keys:
        violations.append("missing_model_keys:" + ",".join(missing_model_keys))

    resolved_training = normalize_lgbm_training_params(
        {
            "loss": run_env.get("model_loss", "mse"),
            "lr": run_env.get("lr"),
            "learning_rate": run_env.get("lr"),
            "n_estimators": run_env.get("n_estimators"),
            "early_stopping_rounds": run_env.get("early_stopping_rounds"),
            "num_leaves": run_env.get("num_leaves"),
            "max_depth": run_env.get("max_depth"),
            "feature_fraction": run_env.get("feature_fraction"),
            "bagging_fraction": run_env.get("bagging_fraction"),
            "bagging_freq": run_env.get("bagging_freq"),
            "lambda_l1": run_env.get("lambda_l1"),
            "lambda_l2": run_env.get("lambda_l2"),
            "min_data_in_leaf": run_env.get("min_data_in_leaf"),
            "boosting_type": run_env.get("boosting_type", "gbdt"),
            "seed": run_env.get("seed", 42),
            "n_jobs": run_env.get("n_jobs", 4),
        }
    )
    resolved_sample_weight = {
        "sample_weight_policy": run_env.get("sample_weight_policy") or QLIB_REQUIRED_SAMPLE_WEIGHT_POLICY,
        "sample_weight_kwargs": {
            "top_n": int(_env_number(run_env, "sample_weight_top_n", QLIB_REQUIRED_SAMPLE_WEIGHT_KWARGS["top_n"])),
            "top_max": float(_env_number(run_env, "sample_weight_top_max", QLIB_REQUIRED_SAMPLE_WEIGHT_KWARGS["top_max"])),
            "bottom_n": int(_env_number(run_env, "sample_weight_bottom_n", QLIB_REQUIRED_SAMPLE_WEIGHT_KWARGS["bottom_n"])),
            "bottom_max": float(_env_number(run_env, "sample_weight_bottom_max", QLIB_REQUIRED_SAMPLE_WEIGHT_KWARGS["bottom_max"])),
            "normalize_mean": str(
                run_env.get(
                    "sample_weight_normalize_mean",
                    QLIB_REQUIRED_SAMPLE_WEIGHT_KWARGS["normalize_mean"],
                )
            ).strip().lower() not in {"false", "0", "no"},
        },
    }
    resolved_training.update(resolved_sample_weight)
    resolved_portfolio = {
        "topk": int(_env_number(run_env, "strategy_topk", MODEL_DEFAULT_TOPK)),
        "n_drop": int(_env_number(run_env, "strategy_n_drop", MODEL_DEFAULT_N_DROP)),
        "hold_thresh": int(_env_number(run_env, "strategy_hold_thresh", QLIB_REQUIRED_HOLD_THRESH)),
        "benchmark": str(run_env.get("QLIB_BENCHMARK") or MODEL_DEFAULT_BENCHMARK),
        "deal_price": QLIB_REQUIRED_DEAL_PRICE,
        "limit_threshold": QLIB_REQUIRED_LIMIT_THRESHOLD,
        "forbid_all_trade_at_limit": QLIB_REQUIRED_FORBID_ALL_TRADE_AT_LIMIT,
        "strategy_class": QLIB_REQUIRED_STRATEGY_CLASS,
        "strategy_module": QLIB_REQUIRED_STRATEGY_MODULE,
        "signal_execution": "qlib_native_previous_signal_open",
    }
    if resolved_portfolio["topk"] != MODEL_DEFAULT_TOPK or resolved_portfolio["n_drop"] != MODEL_DEFAULT_N_DROP:
        violations.append(
            "portfolio_not_fxalpha_default:"
            f"topk={resolved_portfolio['topk']},n_drop={resolved_portfolio['n_drop']},"
            f"expected={MODEL_DEFAULT_TOPK}/{MODEL_DEFAULT_N_DROP}"
        )
    file_topk = _extract_scalar(text, "topk")
    file_n_drop = _extract_scalar(text, "n_drop")
    file_hold_thresh = _extract_scalar(text, "hold_thresh")
    resolved_file_topk = _resolve_config_number(file_topk, run_env, "strategy_topk", MODEL_DEFAULT_TOPK)
    resolved_file_n_drop = _resolve_config_number(file_n_drop, run_env, "strategy_n_drop", MODEL_DEFAULT_N_DROP)
    resolved_file_hold_thresh = _resolve_config_number(
        file_hold_thresh,
        run_env,
        "strategy_hold_thresh",
        QLIB_REQUIRED_HOLD_THRESH,
    )
    if resolved_file_topk is not None and int(resolved_file_topk) != resolved_portfolio["topk"]:
        violations.append(f"topk_mismatch:file={file_topk},env={resolved_portfolio['topk']}")
    if resolved_file_n_drop is not None and int(resolved_file_n_drop) != resolved_portfolio["n_drop"]:
        violations.append(f"n_drop_mismatch:file={file_n_drop},env={resolved_portfolio['n_drop']}")
    if resolved_file_hold_thresh is None:
        violations.append(f"hold_thresh_missing:expected={resolved_portfolio['hold_thresh']}")
    elif int(resolved_file_hold_thresh) != resolved_portfolio["hold_thresh"]:
        violations.append(f"hold_thresh_mismatch:file={file_hold_thresh},env={resolved_portfolio['hold_thresh']}")
    file_limit_threshold = _extract_limit_threshold(text, parsed_config)
    if file_limit_threshold is None:
        violations.append(f"limit_threshold_missing:expected={QLIB_REQUIRED_LIMIT_THRESHOLD}")
    elif not isinstance(file_limit_threshold, (list, tuple)) or list(file_limit_threshold) != QLIB_REQUIRED_LIMIT_THRESHOLD:
        violations.append(
            f"limit_threshold_mismatch:file={file_limit_threshold},expected={QLIB_REQUIRED_LIMIT_THRESHOLD}"
        )
    file_deal_price = _extract_deal_price(text, parsed_config)
    normalized_file_deal_price = str(file_deal_price or "").strip().lstrip("$")
    if normalized_file_deal_price != QLIB_REQUIRED_DEAL_PRICE:
        violations.append(f"deal_price_mismatch:file={file_deal_price},expected={QLIB_REQUIRED_DEAL_PRICE}")
    file_forbid_all = _extract_forbid_all_trade_at_limit(text, parsed_config)
    if file_forbid_all is None:
        violations.append(
            f"forbid_all_trade_at_limit_missing:expected={QLIB_REQUIRED_FORBID_ALL_TRADE_AT_LIMIT}"
        )
    elif bool(file_forbid_all) != QLIB_REQUIRED_FORBID_ALL_TRADE_AT_LIMIT:
        violations.append(
            "forbid_all_trade_at_limit_mismatch:"
            f"file={file_forbid_all},expected={QLIB_REQUIRED_FORBID_ALL_TRADE_AT_LIMIT}"
        )
    strategy_config = (parsed_config.get("port_analysis_config") or {}).get("strategy") if isinstance(parsed_config, dict) else {}
    strategy_config = strategy_config if isinstance(strategy_config, dict) else {}
    file_strategy_class = str(strategy_config.get("class") or "")
    file_strategy_module = str(strategy_config.get("module_path") or "")
    if file_strategy_class != QLIB_REQUIRED_STRATEGY_CLASS:
        violations.append(f"strategy_class_mismatch:file={file_strategy_class},expected={QLIB_REQUIRED_STRATEGY_CLASS}")
    if file_strategy_module != QLIB_REQUIRED_STRATEGY_MODULE:
        violations.append(f"strategy_module_mismatch:file={file_strategy_module},expected={QLIB_REQUIRED_STRATEGY_MODULE}")
    record_entries = _extract_record_entries(parsed_config)
    port_records = [
        entry
        for entry in record_entries
        if isinstance(entry, dict)
        and str(entry.get("class") or "") in {QLIB_REQUIRED_PORT_ANALYSIS_CLASS, "FXAlphaOpenNextPortAnaRecord"}
    ]
    if not any(
        str(entry.get("class") or "") == QLIB_REQUIRED_PORT_ANALYSIS_CLASS
        and str(entry.get("module_path") or "") == QLIB_REQUIRED_PORT_ANALYSIS_MODULE
        for entry in port_records
    ):
        violations.append(
            "port_analysis_record_mismatch:"
            f"expected={QLIB_REQUIRED_PORT_ANALYSIS_MODULE}.{QLIB_REQUIRED_PORT_ANALYSIS_CLASS}"
        )
    file_training_param_map = {
        "learning_rate": ("lr", resolved_training.get("lr")),
        "n_estimators": ("n_estimators", resolved_training.get("n_estimators")),
        "early_stopping_rounds": ("early_stopping_rounds", resolved_training.get("early_stopping_rounds")),
        "num_leaves": ("num_leaves", resolved_training.get("num_leaves")),
        "max_depth": ("max_depth", resolved_training.get("max_depth")),
        "feature_fraction": ("feature_fraction", resolved_training.get("feature_fraction")),
        "bagging_fraction": ("bagging_fraction", resolved_training.get("bagging_fraction")),
        "lambda_l1": ("lambda_l1", resolved_training.get("lambda_l1")),
        "lambda_l2": ("lambda_l2", resolved_training.get("lambda_l2")),
        "min_data_in_leaf": ("min_data_in_leaf", resolved_training.get("min_data_in_leaf")),
        "seed": ("seed", resolved_training.get("seed")),
        "n_jobs": ("n_jobs", resolved_training.get("n_jobs")),
    }
    for file_key, (env_key, expected_value) in file_training_param_map.items():
        if expected_value is None:
            continue
        raw_value = _extract_scalar(text, file_key)
        resolved_value = _resolve_config_number(raw_value, run_env, env_key, expected_value)
        if raw_value is not None and resolved_value != expected_value:
            violations.append(f"{file_key}_mismatch:file={raw_value},env={expected_value}")
    if "LGBModel" not in text:
        violations.append("missing_LGBModel")
    if "DatasetH" not in text:
        violations.append("missing_DatasetH")
    if "DataHandlerLP" not in text:
        violations.append("missing_DataHandlerLP")

    segments = {
        "train": _extract_segment(text, "train"),
        "valid": _extract_segment(text, "valid"),
        "test": _extract_segment(text, "test"),
    }
    expected_segments = {
        "train": [windows.get("train_start"), windows.get("train_end")],
        "valid": [windows.get("valid_start"), windows.get("valid_end")],
        "test": [windows.get("test_start"), windows.get("test_end")],
    }
    for key, expected in expected_segments.items():
        if all(expected) and segments.get(key) and list(segments[key] or []) != list(expected):
            violations.append(f"{key}_segment_mismatch:file={segments[key]},expected={expected}")

    if expected_feature_count is not None:
        num_features = _env_number(run_env, "num_features")
        if num_features is not None and int(num_features) != int(expected_feature_count):
            warnings.append(f"num_features_env_mismatch:env={num_features},expected={expected_feature_count}")
        if "StaticDataLoader" not in text:
            violations.append("missing_StaticDataLoader_for_platform_features")
        if "combined_factors_df.parquet" not in text:
            violations.append("missing_platform_combined_factors_df")
        if "Alpha158" in text:
            violations.append("alpha158_handler_present_in_platform_feature_config")

    return {
        "passed": not violations,
        "config_path": str(path),
        "violations": violations,
        "warnings": warnings,
        "resolved_training_params": resolved_training,
        "resolved_portfolio_params": resolved_portfolio,
        "resolved_processors": {
            "classes": sorted(processors),
            "required": sorted(QLIB_REQUIRED_PROCESSORS),
            "missing": missing_processors,
            "chains": processor_audit["resolved"],
        },
        "resolved_windows": dict(windows or {}),
        "segments": segments,
        "unresolved_template_tokens": unresolved_templates,
    }


def model_training_contract() -> dict[str, Any]:
    """Return the active contract from the sole production model package."""
    from .contracts import production_contract

    contract = production_contract()
    contract["qlib_contract"] = {
        "processor_chain": QLIB_EXPECTED_PROCESSOR_CHAIN,
        "model_class": QLIB_REQUIRED_MODEL_CLASS,
        "model_module": QLIB_REQUIRED_MODEL_MODULE,
        "sample_weight_policy": QLIB_REQUIRED_SAMPLE_WEIGHT_POLICY,
        "sample_weight_kwargs": QLIB_REQUIRED_SAMPLE_WEIGHT_KWARGS,
        "strategy_class": QLIB_REQUIRED_STRATEGY_CLASS,
        "strategy_module": QLIB_REQUIRED_STRATEGY_MODULE,
        "deal_price": QLIB_REQUIRED_DEAL_PRICE,
        "limit_threshold": QLIB_REQUIRED_LIMIT_THRESHOLD,
    }
    return contract
