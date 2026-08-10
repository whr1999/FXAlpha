#!/usr/bin/env python3
"""Read-only, registry-free model objective and window comparison.

The experiment intentionally does not import FXAlpha's model service,
orchestrator, state store, registry, or production refit modules.  Its only
mutable target is runtime/research_sandbox/model_rank.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import platform
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from sklearn.linear_model import Ridge


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "model" / "features" / "feature_sets"
SANDBOX_ROOT = PROJECT_ROOT / "runtime" / "research_sandbox" / "model_rank"
DEFAULT_FEATURE_SET_ID = os.environ.get("FXALPHA_SANDBOX_FEATURE_SET_ID", "")
SCHEMA_VERSION = "isolated_model_rank_screen_v1"
SEED = 42


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    family: str
    objective: str
    weighting: str
    window: str


@dataclass(frozen=True)
class Fold:
    fold_id: str
    valid_start: str
    valid_end: str


FULL_FOLDS = (
    Fold("inner_2024h2", "2024-07-01", "2024-12-31"),
    Fold("inner_2025h1", "2025-01-01", "2025-06-30"),
    Fold("inner_2025h2", "2025-07-01", "2025-12-31"),
)

SMOKE_FOLDS = (Fold("smoke_2025q1", "2025-01-01", "2025-03-31"),)


def candidate_matrix() -> tuple[Candidate, ...]:
    rows: list[Candidate] = []
    for window in ("expanding", "recent36m"):
        rows.extend(
            [
                Candidate(f"ridge_equal_{window}", "ridge", "ridge", "equal", window),
                Candidate(f"lgb_mse_equal_{window}", "lightgbm", "regression", "equal", window),
                Candidate(f"lgb_mse_current_{window}", "lightgbm", "regression", "current", window),
                Candidate(f"lgb_huber_equal_{window}", "lightgbm", "huber", "equal", window),
                Candidate(f"lgb_rank_equal_{window}", "lightgbm", "rank_xendcg", "equal", window),
                Candidate(f"lgb_rank_topfocus_{window}", "lightgbm", "rank_xendcg", "top_focus", window),
            ]
        )
    return tuple(rows)


def smoke_candidates() -> tuple[Candidate, ...]:
    return (
        Candidate("smoke_mse", "lightgbm", "regression", "equal", "recent36m"),
        Candidate("smoke_huber", "lightgbm", "huber", "equal", "recent36m"),
        Candidate("smoke_rank", "lightgbm", "rank_xendcg", "equal", "recent36m"),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _head_sha256(path: Path, byte_count: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(byte_count))
    return digest.hexdigest()


def _safe_output_dir(run_id: str) -> Path:
    root = SANDBOX_ROOT.resolve()
    out = (SANDBOX_ROOT / run_id).resolve()
    if root not in out.parents:
        raise ValueError(f"sandbox output escaped allowed root: {out}")
    out.mkdir(parents=True, exist_ok=False)
    return out


def _snapshot(feature_set_id: str) -> tuple[Path, dict[str, Any]]:
    manifest_path = SNAPSHOT_ROOT / feature_set_id / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("feature_set_id")) != feature_set_id:
        raise ValueError("feature_set_id mismatch")
    if bool(manifest.get("updates_active_feature_pointer")):
        raise ValueError("sandbox requires an immutable non-pointer-updating snapshot")
    parquet_path = Path(str(manifest.get("combined_factors_file") or manifest.get("feature_file") or ""))
    if not parquet_path.exists():
        raise FileNotFoundError(parquet_path)
    return parquet_path, manifest


def _calendar(parquet_path: Path) -> pd.DatetimeIndex:
    table = pq.read_table(parquet_path, columns=["datetime"])
    dates = pd.DatetimeIndex(pd.to_datetime(table.column("datetime").to_pandas())).normalize().unique()
    return pd.DatetimeIndex(sorted(dates))


def _on_or_after(dates: pd.DatetimeIndex, value: str | pd.Timestamp) -> pd.Timestamp:
    target = pd.Timestamp(value).normalize()
    idx = int(dates.searchsorted(target, side="left"))
    if idx >= len(dates):
        raise ValueError(f"date after snapshot end: {target.date()}")
    return pd.Timestamp(dates[idx])


def _on_or_before(dates: pd.DatetimeIndex, value: str | pd.Timestamp) -> pd.Timestamp:
    target = pd.Timestamp(value).normalize()
    idx = int(dates.searchsorted(target, side="right")) - 1
    if idx < 0:
        raise ValueError(f"date before snapshot start: {target.date()}")
    return pd.Timestamp(dates[idx])


def resolve_segments(dates: pd.DatetimeIndex, fold: Fold, window: str, smoke: bool) -> dict[str, str]:
    valid_start = _on_or_after(dates, fold.valid_start)
    valid_end = _on_or_before(dates, fold.valid_end)
    start_idx = int(dates.get_loc(valid_start))
    train_end_idx = start_idx - 6  # five complete trading days between train and valid
    if train_end_idx < 0:
        raise ValueError("not enough history for five-day purge")
    train_end = pd.Timestamp(dates[train_end_idx])
    if smoke:
        train_start = _on_or_after(dates, train_end - pd.DateOffset(months=6))
    elif window == "recent36m":
        train_start = _on_or_after(dates, train_end - pd.DateOffset(months=36))
    else:
        train_start = _on_or_after(dates, "2022-01-01")
    return {
        "train_start": str(train_start.date()),
        "train_end": str(train_end.date()),
        "valid_start": str(valid_start.date()),
        "valid_end": str(valid_end.date()),
        "purge_trading_days": 5,
    }


def load_slice(parquet_path: Path, start: str, end: str) -> pd.DataFrame:
    frame = pd.read_parquet(
        parquet_path,
        filters=[("datetime", ">=", pd.Timestamp(start)), ("datetime", "<=", pd.Timestamp(end))],
    ).sort_index()
    if frame.empty:
        raise ValueError(f"empty parquet slice: {start}..{end}")
    return frame


def _raw_parts(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, int]:
    features = frame["feature"].astype("float32", copy=True)
    label = frame["label"]["LABEL0"].astype("float64", copy=True)
    inf_count = int(np.isinf(features.to_numpy(copy=False)).sum())
    features.replace([np.inf, -np.inf], np.nan, inplace=True)
    label.replace([np.inf, -np.inf], np.nan, inplace=True)
    keep = label.notna()
    return features.loc[keep], label.loc[keep], inf_count


def _fit_robust_state(features: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    values = features.to_numpy(dtype="float32", copy=False)
    median = np.nanmedian(values, axis=0).astype("float32")
    mad = np.nanmedian(np.abs(values - median), axis=0).astype("float32")
    scale = mad * np.float32(1.4826) + np.float32(1e-12)
    return median, scale


def _transform_features(features: pd.DataFrame, median: np.ndarray, scale: np.ndarray) -> pd.DataFrame:
    values = features.to_numpy(dtype="float32", copy=True)
    values -= median
    values /= scale
    np.clip(values, -3.0, 3.0, out=values)
    out = pd.DataFrame(values, index=features.index, columns=features.columns)
    daily_mean = out.groupby(level="datetime", sort=False).transform("mean")
    out = out.fillna(daily_mean).fillna(0.0).astype("float32")
    return out


def _zscore_label(label: pd.Series) -> pd.Series:
    grouped = label.groupby(level="datetime", sort=False)
    mean = grouped.transform("mean")
    std = grouped.transform("std").replace(0.0, np.nan)
    return ((label - mean) / std).replace([np.inf, -np.inf], np.nan).fillna(0.0).astype("float32")


def _relevance_label(label: pd.Series) -> pd.Series:
    rank_desc = label.groupby(level="datetime", sort=False).rank(method="first", ascending=False)
    group_size = label.groupby(level="datetime", sort=False).transform("size")
    relevance = pd.Series(np.zeros(len(label), dtype="int8"), index=label.index)
    relevance.loc[rank_desc <= group_size * 0.20] = 1
    relevance.loc[rank_desc <= 100] = 2
    relevance.loc[rank_desc <= 50] = 3
    relevance.loc[rank_desc <= 20] = 4
    return relevance


def _current_weights(label_z: pd.Series) -> np.ndarray:
    top_rank = label_z.groupby(level="datetime", sort=False).rank(method="first", ascending=False)
    bottom_rank = label_z.groupby(level="datetime", sort=False).rank(method="first", ascending=True)
    weights = pd.Series(np.ones(len(label_z), dtype="float32"), index=label_z.index)
    top = top_rank <= 50
    bottom = bottom_rank <= 50
    top_weight = (1.0 + (51.0 - top_rank.loc[top]) / 50.0).astype("float32")
    weights.loc[top] = top_weight
    bottom_weight = (1.0 + 0.5 * (51.0 - bottom_rank.loc[bottom]) / 50.0).astype("float32")
    weights.loc[bottom] = np.maximum(weights.loc[bottom].to_numpy(), bottom_weight.to_numpy()).astype("float32")
    weights /= weights.groupby(level="datetime", sort=False).transform("mean")
    return weights.to_numpy(dtype="float32", copy=False)


def _top_focus_weights(relevance: pd.Series) -> np.ndarray:
    weights = np.ones(len(relevance), dtype="float32")
    rel = relevance.to_numpy(copy=False)
    weights[rel == 3] = 1.5
    weights[rel == 4] = 2.0
    return weights


def _group_sizes(index: pd.MultiIndex) -> np.ndarray:
    sizes = pd.Series(1, index=index).groupby(level="datetime", sort=False).size()
    return sizes.to_numpy(dtype="int32")


def _common_lgb_params(objective: str, smoke: bool) -> dict[str, Any]:
    params: dict[str, Any] = {
        "objective": objective,
        "learning_rate": 0.04,
        "num_leaves": 96,
        "max_depth": 8,
        "min_data_in_leaf": 10,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 1,
        "lambda_l1": 20.0,
        "lambda_l2": 50.0,
        "max_bin": 63,
        "force_col_wise": True,
        "deterministic": True,
        "num_threads": 4,
        "verbosity": -1,
        "seed": SEED,
        "feature_fraction_seed": SEED,
        "bagging_seed": SEED,
        "data_random_seed": SEED,
    }
    if objective == "rank_xendcg":
        params.update(
            {
                "metric": "ndcg",
                "eval_at": [20, 50],
                "num_leaves": 64,
                "min_data_in_leaf": 50,
                "lambda_l1": 0.0,
                "lambda_l2": 1.0,
            }
        )
    elif objective == "huber":
        params.update({"metric": "huber", "alpha": 0.9})
    else:
        params.update({"metric": "l2"})
    if smoke:
        params["num_threads"] = 2
    return params


def _fit_predict(
    candidate: Candidate,
    train_x: pd.DataFrame,
    train_label_raw: pd.Series,
    valid_x: pd.DataFrame,
    valid_label_raw: pd.Series,
    smoke: bool,
) -> tuple[np.ndarray, dict[str, Any]]:
    label_z_train = _zscore_label(train_label_raw)
    label_z_valid = _zscore_label(valid_label_raw)
    if candidate.family == "ridge":
        model = Ridge(alpha=10.0, fit_intercept=True, solver="lsqr")
        model.fit(train_x.to_numpy(copy=False), label_z_train.to_numpy(copy=False))
        pred = model.predict(valid_x.to_numpy(copy=False)).astype("float32")
        return pred, {"family": "ridge", "alpha": 10.0}

    train_relevance = _relevance_label(train_label_raw) if candidate.objective == "rank_xendcg" else None
    valid_relevance = _relevance_label(valid_label_raw) if candidate.objective == "rank_xendcg" else None
    if candidate.weighting == "current":
        train_weight = _current_weights(label_z_train)
    elif candidate.weighting == "top_focus":
        if train_relevance is None:
            raise ValueError("top_focus requires a ranking objective")
        train_weight = _top_focus_weights(train_relevance)
    else:
        train_weight = None

    train_label = train_relevance if train_relevance is not None else label_z_train
    valid_label = valid_relevance if valid_relevance is not None else label_z_valid
    group_train = _group_sizes(train_x.index) if train_relevance is not None else None
    group_valid = _group_sizes(valid_x.index) if valid_relevance is not None else None
    dtrain = lgb.Dataset(
        train_x.to_numpy(copy=False),
        label=train_label.to_numpy(copy=False),
        weight=train_weight,
        group=group_train,
        feature_name=[str(column) for column in train_x.columns],
        free_raw_data=False,
    )
    dvalid = lgb.Dataset(
        valid_x.to_numpy(copy=False),
        label=valid_label.to_numpy(copy=False),
        group=group_valid,
        reference=dtrain,
        feature_name=[str(column) for column in valid_x.columns],
        free_raw_data=False,
    )
    params = _common_lgb_params(candidate.objective, smoke)
    evals_result: dict[str, Any] = {}
    booster = lgb.train(
        params,
        dtrain,
        num_boost_round=80 if smoke else 1200,
        valid_sets=[dvalid],
        valid_names=["valid"],
        callbacks=[
            lgb.early_stopping(20 if smoke else 100, verbose=False),
            lgb.record_evaluation(evals_result),
            lgb.log_evaluation(period=0),
        ],
    )
    pred = booster.predict(valid_x.to_numpy(copy=False), num_iteration=booster.best_iteration).astype("float32")
    return pred, {
        "family": "lightgbm",
        "params": params,
        "best_iteration": int(booster.best_iteration or 0),
        "best_score": _jsonable(booster.best_score),
    }


def _daily_rank_ic(pred: pd.Series, label: pd.Series) -> tuple[float | None, float | None, int]:
    values: list[float] = []
    joined = pd.concat([pred.rename("pred"), label.rename("label")], axis=1).dropna()
    for _dt, group in joined.groupby(level="datetime", sort=False):
        if len(group) < 20:
            continue
        corr = group["pred"].corr(group["label"], method="spearman")
        if pd.notna(corr):
            values.append(float(corr))
    series = pd.Series(values, dtype="float64")
    if series.empty:
        return None, None, 0
    std = float(series.std())
    return float(series.mean()), (float(series.mean()) / std if std else None), int(len(series))


def _equal_rank_baseline(valid_raw_features: pd.DataFrame) -> pd.Series:
    ranked = valid_raw_features.groupby(level="datetime", sort=False).rank(pct=True)
    return ranked.mean(axis=1).astype("float32")


def _topn_label_mean(score: pd.Series, label: pd.Series, n: int) -> float | None:
    joined = pd.concat([score.rename("score"), label.rename("label")], axis=1).dropna()
    daily: list[float] = []
    for _dt, group in joined.groupby(level="datetime", sort=False):
        if len(group) < n:
            continue
        daily.append(float(group.nlargest(n, "score")["label"].mean()))
    return float(pd.Series(daily).mean()) if daily else None


def evaluate_predictions(pred: np.ndarray, index: pd.MultiIndex, label: pd.Series, baseline: pd.Series) -> dict[str, Any]:
    prediction = pd.Series(pred, index=index, name="score")
    rank_ic, rank_icir, date_count = _daily_rank_ic(prediction, label)
    baseline_ic, baseline_icir, _ = _daily_rank_ic(baseline, label)
    top20 = _topn_label_mean(prediction, label, 20)
    base_top20 = _topn_label_mean(baseline, label, 20)
    return {
        "rank_ic": rank_ic,
        "rank_icir": rank_icir,
        "baseline_rank_ic": baseline_ic,
        "baseline_rank_icir": baseline_icir,
        "rank_ic_uplift": (rank_ic - baseline_ic) if rank_ic is not None and baseline_ic is not None else None,
        "top20_forward_return_mean": top20,
        "baseline_top20_forward_return_mean": base_top20,
        "top20_uplift_bps": (top20 - base_top20) * 10000.0 if top20 is not None and base_top20 is not None else None,
        "date_count": date_count,
        "prediction_std": float(prediction.std()),
    }


def _prediction_path(out_dir: Path, candidate_id: str, fold_id: str) -> Path:
    return out_dir / "predictions" / candidate_id / f"{fold_id}.parquet"


def run(args: argparse.Namespace) -> int:
    started = time.time()
    parquet_path, manifest = _snapshot(args.feature_set_id)
    run_id = args.run_id or f"rank_screen_{args.mode}_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir = _safe_output_dir(run_id)
    dates = _calendar(parquet_path)
    smoke = args.mode == "smoke"
    folds = SMOKE_FOLDS if smoke else FULL_FOLDS
    candidates = smoke_candidates() if smoke else candidate_matrix()
    plan = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "mode": args.mode,
        "isolation": {
            "registry_write": False,
            "orchestrator_call": False,
            "active_pointer_update": False,
            "production_runtime_write": False,
            "allowed_output_root": str(SANDBOX_ROOT),
        },
        "feature_set_id": args.feature_set_id,
        "feature_set_fingerprint": manifest.get("snapshot_content_fingerprint") or manifest.get("feature_set_fingerprint"),
        "feature_file": str(parquet_path),
        "feature_file_head_1m_sha256": _head_sha256(parquet_path),
        "candidates": [asdict(row) for row in candidates],
        "folds": [asdict(row) for row in folds],
        "versions": {
            "python": sys.version,
            "platform": platform.platform(),
            "lightgbm": lgb.__version__,
            "pandas": pd.__version__,
            "numpy": np.__version__,
        },
        "pid": os.getpid(),
        "started_at": pd.Timestamp.now().isoformat(),
    }
    _write_json(out_dir / "plan.json", plan)

    results: list[dict[str, Any]] = []
    for fold in folds:
        windows = sorted({candidate.window for candidate in candidates})
        valid_segments = resolve_segments(dates, fold, windows[0], smoke)
        valid_frame = load_slice(parquet_path, valid_segments["valid_start"], valid_segments["valid_end"])
        valid_raw_x, valid_label, valid_inf = _raw_parts(valid_frame)
        baseline = _equal_rank_baseline(valid_raw_x)
        del valid_frame
        gc.collect()

        for window in windows:
            segments = resolve_segments(dates, fold, window, smoke)
            train_frame = load_slice(parquet_path, segments["train_start"], segments["train_end"])
            train_raw_x, train_label, train_inf = _raw_parts(train_frame)
            del train_frame
            median, scale = _fit_robust_state(train_raw_x)
            train_x = _transform_features(train_raw_x, median, scale)
            valid_x = _transform_features(valid_raw_x, median, scale)
            del train_raw_x
            gc.collect()

            for candidate in [row for row in candidates if row.window == window]:
                candidate_started = time.time()
                pred, training = _fit_predict(candidate, train_x, train_label, valid_x, valid_label, smoke)
                metrics = evaluate_predictions(pred, valid_x.index, valid_label, baseline)
                row = {
                    "candidate": asdict(candidate),
                    "fold": asdict(fold),
                    "segments": segments,
                    "train_rows": int(len(train_x)),
                    "valid_rows": int(len(valid_x)),
                    "train_inf_count": train_inf,
                    "valid_inf_count": valid_inf,
                    "training": training,
                    "metrics": metrics,
                    "elapsed_seconds": round(time.time() - candidate_started, 3),
                }
                results.append(row)
                pred_path = _prediction_path(out_dir, candidate.candidate_id, fold.fold_id)
                pred_path.parent.mkdir(parents=True, exist_ok=True)
                pd.DataFrame({"score": pred, "label": valid_label.to_numpy(copy=False)}, index=valid_x.index).to_parquet(pred_path)
                _write_json(out_dir / "results" / candidate.candidate_id / f"{fold.fold_id}.json", row)
                print(json.dumps({"candidate": candidate.candidate_id, "fold": fold.fold_id, **metrics}, ensure_ascii=False), flush=True)
                gc.collect()

            del train_x, train_label, valid_x, median, scale
            gc.collect()

        del valid_raw_x, valid_label, baseline
        gc.collect()

    summary_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        matched = [row for row in results if row["candidate"]["candidate_id"] == candidate.candidate_id]
        metric_rows = [row["metrics"] for row in matched]

        def median_metric(key: str) -> float | None:
            values = [float(row[key]) for row in metric_rows if row.get(key) is not None]
            return float(np.median(values)) if values else None

        summary_rows.append(
            {
                "candidate": asdict(candidate),
                "folds_completed": len(matched),
                "median_rank_ic": median_metric("rank_ic"),
                "median_rank_ic_uplift": median_metric("rank_ic_uplift"),
                "median_top20_uplift_bps": median_metric("top20_uplift_bps"),
                "positive_rank_ic_folds": sum((row.get("rank_ic") or 0.0) > 0 for row in metric_rows),
                "positive_top20_uplift_folds": sum((row.get("top20_uplift_bps") or 0.0) > 0 for row in metric_rows),
                "elapsed_seconds": round(sum(float(row["elapsed_seconds"]) for row in matched), 3),
            }
        )
    summary_rows.sort(
        key=lambda row: (
            row["positive_top20_uplift_folds"],
            row["median_top20_uplift_bps"] if row["median_top20_uplift_bps"] is not None else -1e9,
            row["median_rank_ic"] if row["median_rank_ic"] is not None else -1e9,
        ),
        reverse=True,
    )
    summary = {
        **plan,
        "status": "completed",
        "completed_at": pd.Timestamp.now().isoformat(),
        "elapsed_seconds": round(time.time() - started, 3),
        "result_count": len(results),
        "ranking": summary_rows,
        "evidence_scope": "isolated_signal_screen_not_formal_rolling_not_promotion_eligible",
    }
    _write_json(out_dir / "summary.json", summary)
    print(json.dumps({"status": "completed", "summary": str(out_dir / "summary.json")}, ensure_ascii=False), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("smoke", "screen"), default="smoke")
    parser.add_argument(
        "--feature-set-id",
        default=DEFAULT_FEATURE_SET_ID,
        required=not bool(DEFAULT_FEATURE_SET_ID),
        help="Local feature-set ID; alternatively set FXALPHA_SANDBOX_FEATURE_SET_ID outside Git",
    )
    parser.add_argument("--run-id", default="")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
