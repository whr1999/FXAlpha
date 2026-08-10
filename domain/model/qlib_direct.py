from __future__ import annotations

import json
import math
import pickle
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from storage.paths import MODEL_DEFAULT_TEST_MONTHS, MODEL_DEFAULT_VALID_MONTHS, PROJECT_ROOT, QLIB_DATA_ROOT, QLIB_SOURCE_ROOT

from .contracts import (
    DEFAULT_PORTFOLIO,
    DEFAULT_SAMPLE_WEIGHT_POLICY,
    LIMIT_THRESHOLD,
)
from .training_contract import model_training_contract


def _ensure_qlib0627_path() -> None:
    if QLIB_SOURCE_ROOT.exists() and str(QLIB_SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(QLIB_SOURCE_ROOT))
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _training_diagnostics(
    evals_result: dict[str, Any],
    *,
    booster: Any,
    configured_n_estimators: int,
    early_stopping_rounds: int,
) -> dict[str, Any]:
    """Normalize LightGBM's recorded train/valid curves into auditable evidence."""
    datasets = {str(name): curves for name, curves in (evals_result or {}).items() if isinstance(curves, dict)}
    valid_name = next((name for name in datasets if name.lower() in {"valid", "validation", "valid_0"}), "")
    train_name = next((name for name in datasets if name.lower() in {"train", "training"}), "")
    if not valid_name:
        valid_name = next((name for name in datasets if name != train_name), "")
    metric_name = ""
    if valid_name:
        metric_name = next(iter(datasets.get(valid_name, {})), "")
    if not metric_name and train_name:
        metric_name = next(iter(datasets.get(train_name, {})), "")

    def curve(dataset_name: str) -> list[float]:
        raw = (datasets.get(dataset_name) or {}).get(metric_name) if dataset_name and metric_name else []
        return [float(value) for value in (raw or []) if value is not None]

    train_curve = curve(train_name)
    valid_curve = curve(valid_name)
    evaluated_iterations = len(valid_curve) or len(train_curve)
    raw_best = getattr(booster, "best_iteration", 0) if booster is not None else 0
    try:
        best_iteration = int(raw_best or 0)
    except (TypeError, ValueError):
        best_iteration = 0
    if best_iteration <= 0 and valid_curve:
        best_iteration = min(range(len(valid_curve)), key=valid_curve.__getitem__) + 1
    if evaluated_iterations:
        best_iteration = min(max(best_iteration, 1), evaluated_iterations)
    best_index = best_iteration - 1 if best_iteration else None

    current_iteration = getattr(booster, "current_iteration", None) if booster is not None else None
    try:
        trees_built = int(current_iteration()) if callable(current_iteration) else int(current_iteration or evaluated_iterations)
    except (TypeError, ValueError):
        trees_built = evaluated_iterations

    checkpoint_indices: set[int] = set()
    if evaluated_iterations:
        for ratio in (0.0, 0.10, 0.25, 0.50, 0.75, 1.0):
            checkpoint_indices.add(round((evaluated_iterations - 1) * ratio))
        if best_index is not None:
            checkpoint_indices.add(best_index)
            checkpoint_indices.add(round((best_index + evaluated_iterations - 1) / 2))
    checkpoints = [
        {
            "iteration": index + 1,
            "train_loss": train_curve[index] if index < len(train_curve) else None,
            "valid_loss": valid_curve[index] if index < len(valid_curve) else None,
        }
        for index in sorted(checkpoint_indices)
    ]
    train_at_best = train_curve[best_index] if best_index is not None and best_index < len(train_curve) else None
    valid_at_best = valid_curve[best_index] if best_index is not None and best_index < len(valid_curve) else None
    valid_at_stop = valid_curve[-1] if valid_curve else None
    return {
        "available": bool(metric_name and evaluated_iterations),
        "metric_name": metric_name or None,
        "train_dataset_name": train_name or None,
        "valid_dataset_name": valid_name or None,
        "configured_n_estimators": int(configured_n_estimators),
        "evaluated_iterations": int(evaluated_iterations),
        "trees_built": int(trees_built),
        "best_iteration": int(best_iteration) if best_iteration else None,
        "best_iteration_ratio": round(best_iteration / configured_n_estimators, 6) if best_iteration and configured_n_estimators else None,
        "early_stopping_rounds": int(early_stopping_rounds),
        "early_stopped": bool(evaluated_iterations and evaluated_iterations < configured_n_estimators),
        "train_loss_at_best": train_at_best,
        "valid_loss_at_best": valid_at_best,
        "train_valid_gap_at_best": (valid_at_best - train_at_best) if valid_at_best is not None and train_at_best is not None else None,
        "valid_loss_at_stop": valid_at_stop,
        "valid_deterioration_after_best": (valid_at_stop - valid_at_best) if valid_at_stop is not None and valid_at_best is not None else None,
        "valid_improvement_from_start": (valid_curve[0] - valid_at_best) if valid_curve and valid_at_best is not None else None,
        "curve_checkpoints": checkpoints,
        "curves": {
            "train": train_curve,
            "valid": valid_curve,
        },
    }


def _load_manifest(feature_set_id: str) -> dict[str, Any]:
    from .feature_set_builder import load_feature_set_manifest

    manifest = load_feature_set_manifest(feature_set_id)
    if not manifest:
        raise FileNotFoundError(f"feature set manifest not found: {feature_set_id}")
    return manifest


def _load_feature_frame(manifest: dict[str, Any], *, max_instruments: int | None = None, max_rows: int | None = None) -> pd.DataFrame:
    path = Path(str(manifest.get("combined_factors_file") or ""))
    if not path.exists():
        raise FileNotFoundError(f"combined feature parquet not found: {path}")
    df = pd.read_parquet(path)
    if not isinstance(df.index, pd.MultiIndex) or "datetime" not in df.index.names or "instrument" not in df.index.names:
        raise ValueError("feature frame must have MultiIndex(datetime,instrument)")
    df = df.sort_index()
    if max_instruments:
        instruments = sorted(df.index.get_level_values("instrument").unique())[: int(max_instruments)]
        df = df[df.index.get_level_values("instrument").isin(instruments)]
    if max_rows:
        df = df.iloc[: int(max_rows)]
    if "label" not in df.columns.get_level_values(0):
        raise ValueError("feature frame missing label group")
    if "feature" not in df.columns.get_level_values(0):
        raise ValueError("feature frame missing feature group")
    return df


def _trading_day_on_or_after(date: pd.Timestamp, calendar: pd.DatetimeIndex) -> pd.Timestamp:
    pos = calendar.searchsorted(date, side="left")
    return calendar[min(pos, len(calendar) - 1)]


def _trading_day_on_or_before(date: pd.Timestamp, calendar: pd.DatetimeIndex) -> pd.Timestamp:
    pos = calendar.searchsorted(date, side="right") - 1
    return calendar[max(0, min(pos, len(calendar) - 1))]


def _segment_pair(value: Any) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return (pd.Timestamp(value[0]).normalize(), pd.Timestamp(value[1]).normalize())
    if isinstance(value, dict):
        start = value.get("start") or value.get("start_date") or value.get("begin") or value.get("from")
        end = value.get("end") or value.get("end_date") or value.get("to")
        if start and end:
            return (pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize())
    return None


def _explicit_segments(df: pd.DataFrame, experiment: dict[str, Any]) -> dict[str, tuple[str, str]] | None:
    raw = experiment.get("segments") if isinstance(experiment.get("segments"), dict) else None
    if not raw:
        return None
    dates = pd.DatetimeIndex(pd.to_datetime(df.index.get_level_values("datetime")).unique()).sort_values()
    if dates.empty:
        raise ValueError("feature frame has no dates")
    segments: dict[str, tuple[str, str]] = {}
    for key in ("train", "valid", "test"):
        pair = _segment_pair(raw.get(key))
        if not pair:
            raise ValueError(f"experiment segments missing {key} start/end")
        start, end = pair
        if start > end:
            raise ValueError(f"experiment segment {key} start after end: {start.date()}>{end.date()}")
        segments[key] = (
            _trading_day_on_or_after(start, dates).strftime("%Y-%m-%d"),
            _trading_day_on_or_before(end, dates).strftime("%Y-%m-%d"),
        )
    return segments


def _segments(df: pd.DataFrame, manifest: dict[str, Any], experiment: dict[str, Any]) -> dict[str, tuple[str, str]]:
    explicit = _explicit_segments(df, experiment)
    if explicit:
        return explicit
    dates = pd.DatetimeIndex(pd.to_datetime(df.index.get_level_values("datetime")).unique()).sort_values()
    if dates.empty:
        raise ValueError("feature frame has no dates")
    start = pd.Timestamp(manifest.get("start_date") or dates.min()).normalize()
    end = pd.Timestamp(manifest.get("resolved_end_date") or manifest.get("end_date") or dates.max()).normalize()
    start = _trading_day_on_or_after(start, dates)
    end = _trading_day_on_or_before(end, dates)
    valid_months = int(manifest.get("valid_months") or MODEL_DEFAULT_VALID_MONTHS)
    test_months = int(manifest.get("test_months") or MODEL_DEFAULT_TEST_MONTHS)
    test_start = (end.replace(day=1) - pd.DateOffset(months=max(test_months - 1, 0))).normalize()
    valid_start = (test_start - pd.DateOffset(months=max(valid_months, 0))).normalize()
    valid_start = _trading_day_on_or_after(valid_start, dates)
    test_start = _trading_day_on_or_after(test_start, dates)
    train_end = _trading_day_on_or_before(valid_start - pd.Timedelta(days=1), dates)
    valid_end = _trading_day_on_or_before(test_start - pd.Timedelta(days=1), dates)
    return {
        "train": (start.strftime("%Y-%m-%d"), train_end.strftime("%Y-%m-%d")),
        "valid": (valid_start.strftime("%Y-%m-%d"), valid_end.strftime("%Y-%m-%d")),
        "test": (test_start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
    }


def _processor_configs(segments: dict[str, tuple[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    train_start, train_end = segments["train"]
    infer_processors = [
        {"class": "ProcessInf", "kwargs": {}},
        {
            "class": "RobustZScoreNorm",
            "kwargs": {
                "fields_group": "feature",
                "clip_outlier": True,
                "fit_start_time": train_start,
                "fit_end_time": train_end,
            },
        },
        {"class": "CSZFillna", "kwargs": {"fields_group": "feature"}},
    ]
    learn_processors = [
        {"class": "DropnaLabel", "kwargs": {"fields_group": "label"}},
        {"class": "CSZScoreNorm", "kwargs": {"fields_group": "label", "method": "zscore"}},
    ]
    return infer_processors, learn_processors


def _model_kwargs(experiment: dict[str, Any], seed: int) -> dict[str, Any]:
    params = dict(experiment.get("qlib_model_kwargs") or experiment.get("training_hyperparameters") or {})
    params.pop("sample_weight_policy", None)
    params.pop("sample_weight_kwargs", None)
    # A round's persisted comparison panel is authoritative.  Experiment
    # normalization supplies a baseline seed (normally 42), but retaining it
    # here collapses every member of the 42/17/83 panel into the same model.
    # Override all LightGBM randomness controls together so each seed run is a
    # reproducible, genuinely independent peer experiment.
    effective_seed = int(seed)
    params["seed"] = effective_seed
    params["feature_fraction_seed"] = effective_seed
    params["bagging_seed"] = effective_seed
    params["data_random_seed"] = effective_seed
    params["drop_seed"] = effective_seed
    params.setdefault("bin_construct_sample_cnt", 5_000_000)
    # Make repeated runs of the same seed/config reproducible on this platform.
    params["deterministic"] = True
    params["force_col_wise"] = True
    if "learning_rate" in params and "lr" not in params:
        params["lr"] = params["learning_rate"]
    params.setdefault("loss", "mse")
    params["n_jobs"] = 1
    params["num_threads"] = 1
    return params


def _daily_prediction_metrics(pred: pd.Series, label: pd.Series) -> dict[str, Any]:
    joined = pd.concat([pred.rename("score"), label.rename("label")], axis=1).dropna()
    if joined.empty:
        return {"rank_ic": None, "rank_icir": None, "date_count": 0}
    rank_ics: list[float] = []
    for _dt, group in joined.groupby(level="datetime", sort=True):
        if len(group) < 20:
            continue
        value = group["score"].corr(group["label"], method="spearman")
        if pd.notna(value):
            rank_ics.append(float(value))
    s = pd.Series(rank_ics, dtype="float64")
    return {
        "date_count": int(len(s)),
        "rank_ic": float(s.mean()) if len(s) else None,
        "rank_icir": float(s.mean() / s.std()) if len(s) > 1 and float(s.std()) != 0.0 else None,
    }


def _risk_value(risk: pd.DataFrame, key: str) -> float | None:
    try:
        return float(risk.loc[key].iloc[0])
    except Exception:
        return None


def _write_pickle_artifact(path: Path, payload: Any) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            pickle.dump(payload, fh)
        return str(path)
    except Exception:
        return None


def _position_weight_summary(positions: Any) -> dict[str, Any]:
    if positions is None:
        return {"available": False}
    try:
        if isinstance(positions, dict):
            last_key = sorted(positions.keys())[-1]
            last_pos = positions[last_key]
        else:
            last_key = None
            last_pos = positions[-1] if hasattr(positions, "__getitem__") else positions
        weights: dict[str, float] = {}
        for method in ("get_stock_weight_dict", "get_stock_weight"):
            if hasattr(last_pos, method):
                value = getattr(last_pos, method)()
                if isinstance(value, dict):
                    weights = {str(k): float(v) for k, v in value.items()}
                    break
        if not weights and hasattr(last_pos, "position"):
            raw = getattr(last_pos, "position")
            if isinstance(raw, dict):
                weights = {str(k): float(v.get("amount", 0.0) or v.get("weight", 0.0) or 0.0) for k, v in raw.items() if isinstance(v, dict)}
        if not weights:
            return {"available": True, "summary_supported": False}
        ordered = sorted(weights.items(), key=lambda item: abs(item[1]), reverse=True)
        return {
            "available": True,
            "date": str(last_key) if last_key is not None else "",
            "holding_count": len(ordered),
            "top_weight": ordered[0][1] if ordered else None,
            "top10_weight_sum": sum(abs(v) for _, v in ordered[:10]),
            "top_holdings": [{"instrument": k, "weight": v} for k, v in ordered[:20]],
        }
    except Exception as exc:
        return {"available": True, "summary_supported": False, "error": str(exc)}


def _run_backtest(
    pred: pd.Series,
    *,
    portfolio: dict[str, Any],
    benchmark: str,
    artifact_dir: Path,
    label: str,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any], str | None]:
    _ensure_qlib0627_path()
    import qlib
    from qlib.backtest import backtest
    from qlib.config import REG_CN
    from qlib.contrib.evaluate import risk_analysis

    qlib.init(provider_uri=str(QLIB_DATA_ROOT), region=REG_CN, auto_mount=False, expression_cache=None, dataset_cache=None)
    pred = pred.sort_index()
    dates = pd.Index(sorted(set(pd.to_datetime(pred.index.get_level_values("datetime")).normalize())))
    if dates.empty:
        raise ValueError("prediction has no dates")
    strategy_config = {
        "class": "FXAlphaTopkDropoutStrategy",
        "module_path": "domain.model.qlib_strategy",
        "kwargs": {
            "signal": pred,
            "topk": int(portfolio.get("topk", DEFAULT_PORTFOLIO["topk"])),
            "n_drop": int(portfolio.get("n_drop", DEFAULT_PORTFOLIO["n_drop"])),
            "hold_thresh": int(portfolio.get("hold_thresh", DEFAULT_PORTFOLIO["hold_thresh"])),
            "forbid_all_trade_at_limit": False,
        },
    }
    executor_config = {
        "class": "SimulatorExecutor",
        "module_path": "qlib.backtest.executor",
        "kwargs": {
            "time_per_step": "day",
            "generate_portfolio_metrics": True,
            "verbose": False,
            "indicator_config": {"show_indicator": False},
        },
    }
    exchange_kwargs = {
        "freq": "day",
        "deal_price": "open",
        "open_cost": 0.0005,
        "close_cost": 0.0015,
        "min_cost": 5,
        "limit_threshold": tuple(LIMIT_THRESHOLD),
    }
    portfolio_metric_dict, _indicator_dict = backtest(
        start_time=str(dates.min().date()),
        end_time=str(dates.max().date()),
        executor=executor_config,
        strategy=strategy_config,
        account=100_000_000,
        benchmark=benchmark,
        exchange_kwargs=exchange_kwargs,
    )
    report_df, positions = portfolio_metric_dict["1day"]
    net_return = report_df["return"] - report_df["cost"]
    excess = net_return - report_df.get("bench", 0.0)
    excess_risk = risk_analysis(excess)
    net_risk = risk_analysis(net_return)
    gross_risk = risk_analysis(report_df["return"])
    metrics = {
        "annualized_ret": _risk_value(excess_risk, "annualized_return"),
        "excess_annualized_ret_with_cost": _risk_value(excess_risk, "annualized_return"),
        "excess_information_ratio_with_cost": _risk_value(excess_risk, "information_ratio"),
        # Backward-compatible public names now follow the same after-cost
        # return stream as the saved strategy curve.  Explicit gross/net
        # fields remove the old ambiguity for reports and downstream gates.
        "max_drawdown": _risk_value(net_risk, "max_drawdown"),
        "strategy_annualized_ret": _risk_value(net_risk, "annualized_return"),
        "net_max_drawdown": _risk_value(net_risk, "max_drawdown"),
        "net_strategy_annualized_ret": _risk_value(net_risk, "annualized_return"),
        "gross_max_drawdown": _risk_value(gross_risk, "max_drawdown"),
        "gross_strategy_annualized_ret": _risk_value(gross_risk, "annualized_return"),
        "turnover": float(pd.to_numeric(report_df["turnover"], errors="coerce").mean()) if "turnover" in report_df else None,
        "avg_cost": float(report_df["cost"].mean()) if "cost" in report_df else None,
    }
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report_path = artifact_dir / "report_normal_1day.pkl"
    positions_path = artifact_dir / "positions_normal_1day.pkl"
    indicator_path = artifact_dir / "indicator_normal_1day.pkl"
    summary_path = artifact_dir / "summary.json"
    report_df.to_pickle(report_path)
    positions_ref = _write_pickle_artifact(positions_path, positions)
    indicator_ref = _write_pickle_artifact(indicator_path, _indicator_dict) if _indicator_dict is not None else None
    summary = {
        "portfolio_label": label,
        "portfolio": portfolio,
        "benchmark": benchmark,
        "metrics": metrics,
        "positions_summary": _position_weight_summary(positions),
    }
    summary_path.write_text(json.dumps(_jsonable(summary), indent=2, ensure_ascii=False), encoding="utf-8")
    artifacts = {
        "portfolio_label": label,
        "portfolio_analysis_dir": str(artifact_dir),
        "report_pkl": str(report_path),
        "positions_pkl": positions_ref,
        "indicator_pkl": indicator_ref,
        "summary_file": str(summary_path),
    }
    return report_df, metrics, artifacts, None


def run_direct_qlib_seed(
    *,
    feature_set_id: str,
    experiment: dict[str, Any],
    seed: int,
    run_dir: Path,
    debug: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ensure_qlib0627_path()
    import qlib
    from qlib.config import REG_CN
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.data.dataset.loader import StaticDataLoader
    from qlib.workflow import R

    from .reweight import FXAlphaWeightedLGBModel

    run_dir.mkdir(parents=True, exist_ok=True)
    qlib.init(provider_uri=str(QLIB_DATA_ROOT), region=REG_CN, auto_mount=False, expression_cache=None, dataset_cache=None)
    debug = debug or {}
    manifest = _load_manifest(feature_set_id)
    df = _load_feature_frame(
        manifest,
        max_instruments=debug.get("max_instruments"),
        max_rows=debug.get("max_rows"),
    )
    segments = _segments(df, manifest, experiment)
    infer_processors, learn_processors = _processor_configs(segments)
    handler = DataHandlerLP(
        instruments=None,
        start_time=segments["train"][0],
        end_time=segments["test"][1],
        data_loader=StaticDataLoader(df),
        infer_processors=infer_processors,
        learn_processors=learn_processors,
        process_type=DataHandlerLP.PTYPE_A,
        drop_raw=False,
    )
    dataset = DatasetH(handler=handler, segments=segments)
    params = _model_kwargs(experiment, seed)
    num_boost_round = int(params.pop("n_estimators", params.pop("num_boost_round", 1000)) or 1000)
    early_stopping_rounds = int(params.pop("early_stopping_rounds", 50) or 50)
    sample_weight_kwargs = dict(experiment.get("sample_weight_kwargs") or {})
    sample_weight_policy = str(experiment.get("sample_weight_policy") or DEFAULT_SAMPLE_WEIGHT_POLICY)
    effective_sample_weight_policy = str(experiment.get("effective_sample_weight_policy") or sample_weight_policy)
    evals_result: dict[str, Any] = {}
    model = FXAlphaWeightedLGBModel(
        **params,
        num_boost_round=num_boost_round,
        early_stopping_rounds=early_stopping_rounds,
        sample_weight_policy=effective_sample_weight_policy,
        sample_weight_kwargs=sample_weight_kwargs,
    )
    with R.start(experiment_name="fxalpha_model"):
        model.fit(dataset, evals_result=evals_result, verbose_eval=int(debug.get("verbose_eval", 100)))
    training_diagnostics = _training_diagnostics(
        evals_result,
        booster=model.model,
        configured_n_estimators=num_boost_round,
        early_stopping_rounds=early_stopping_rounds,
    )
    (run_dir / "training_diagnostics.json").write_text(
        json.dumps(_jsonable(training_diagnostics), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    pred = model.predict(dataset, segment="test").rename("score").sort_index()
    pred.to_pickle(run_dir / "pred.pkl")
    with (run_dir / "model.pkl").open("wb") as fh:
        pickle.dump(model, fh)
    test_label = dataset.prepare("test", col_set="label", data_key=DataHandlerLP.DK_I)
    label_series = test_label.iloc[:, 0] if isinstance(test_label, pd.DataFrame) else test_label
    label_series.to_pickle(run_dir / "label.pkl")
    pred_metrics = _daily_prediction_metrics(pred, label_series)
    portfolio = dict(experiment.get("portfolio") or DEFAULT_PORTFOLIO)
    benchmark = str(experiment.get("benchmark") or "000300sh")
    params_payload = {
        "feature_set_id": feature_set_id,
        "seed": int(seed),
        "segments": segments,
        "model_kwargs": params,
        "num_boost_round": num_boost_round,
        "early_stopping_rounds": early_stopping_rounds,
        "sample_weight_policy": sample_weight_policy,
        "effective_sample_weight_policy": effective_sample_weight_policy,
        "sample_weight_kwargs": sample_weight_kwargs,
        "portfolio": portfolio,
        "benchmark": benchmark,
        "processors": {"infer_processors": infer_processors, "learn_processors": learn_processors},
    }
    _write_pickle_artifact(run_dir / "params.pkl", params_payload)
    backtest_error = None
    portfolio_artifacts: dict[str, Any] = {}
    try:
        ret_df, bt_metrics, portfolio_artifacts, backtest_error = _run_backtest(
            pred,
            portfolio=portfolio,
            benchmark=benchmark,
            artifact_dir=run_dir / "portfolio_analysis",
            label="portfolio",
        )
        if ret_df.empty:
            backtest_error = "backtest_return_empty"
            bt_metrics = {}
    except Exception as exc:
        ret_df = pd.DataFrame()
        bt_metrics = {}
        backtest_error = str(exc)
    ret_df.to_pickle(run_dir / "ret.pkl")
    compact_training_diagnostics = {key: value for key, value in training_diagnostics.items() if key != "curves"}
    metrics = {
        **bt_metrics,
        **{k: v for k, v in pred_metrics.items() if v is not None},
        "training_diagnostics": compact_training_diagnostics,
    }
    (run_dir / "metrics.json").write_text(json.dumps(_jsonable(metrics), indent=2, ensure_ascii=False), encoding="utf-8")
    feature_importance = pd.Series(model.model.feature_importance(), index=df["feature"].columns).sort_values(ascending=False)
    feature_importance.to_csv(run_dir / "feature_importance.csv")
    artifact_manifest = {
        "model_direct_qlib_version": "v2_training_diagnostics",
        "contract": model_training_contract(),
        "feature_set_id": feature_set_id,
        "feature_set_manifest": manifest,
        "seed": int(seed),
        "segments": segments,
        "debug": debug,
        "row_count": int(len(df)),
        "feature_count": int(df["feature"].shape[1]),
        "resolved_training_params": {**params, "num_boost_round": num_boost_round, "early_stopping_rounds": early_stopping_rounds, "seed": int(seed)},
        "resolved_reweight_params": {
            "requested_sample_weight_policy": sample_weight_policy,
            "effective_sample_weight_policy": effective_sample_weight_policy,
            "sample_weight_kwargs": sample_weight_kwargs,
        },
        "resolved_portfolio_params": {
            "portfolio": portfolio,
            "benchmark": benchmark,
            "deal_price": "open",
            "limit_threshold": list(LIMIT_THRESHOLD),
            "forbid_all_trade_at_limit": False,
            "portfolio_artifacts": portfolio_artifacts,
        },
        "resolved_processors": {"infer_processors": infer_processors, "learn_processors": learn_processors},
        "config_audit": {
            "passed": backtest_error is None,
            "direct_qlib_adapter": True,
            "backtest_error": backtest_error,
        },
        "artifacts": {
            "pred": str(run_dir / "pred.pkl"),
            "label": str(run_dir / "label.pkl"),
            "ret": str(run_dir / "ret.pkl"),
            "model": str(run_dir / "model.pkl"),
            "params": str(run_dir / "params.pkl"),
            "metrics": str(run_dir / "metrics.json"),
            "feature_importance": str(run_dir / "feature_importance.csv"),
            "training_diagnostics": str(run_dir / "training_diagnostics.json"),
            "portfolio": portfolio_artifacts,
        },
        "metrics": metrics,
    }
    (run_dir / "direct_qlib_manifest.json").write_text(
        json.dumps(_jsonable(artifact_manifest), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return artifact_manifest
