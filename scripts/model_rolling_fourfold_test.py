from __future__ import annotations

import argparse
import gc
import json
import pickle
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from domain.model.contracts import DEFAULT_PORTFOLIO, default_r1_experiment
from domain.model.qlib_direct import _risk_value, _run_backtest, run_direct_qlib_seed
from storage.paths import MODEL_FEATURE_SETS_ROOT, MODEL_RUNTIME_ROOT, QLIB_DATA_ROOT


ACTIVE_FEATURE_SET = MODEL_RUNTIME_ROOT / "active_feature_set.json"
OUT_ROOT = MODEL_RUNTIME_ROOT / "rolling_diagnostics"
CALENDAR_FILE = QLIB_DATA_ROOT / "calendars" / "day.txt"
SEED = 42


LEGACY_REQUESTED_FOLDS = (
    {
        "fold_id": "wf1_2024h2",
        "train_start": "2022-01-04",
        "valid_start": "2024-01-02",
        "test_start": "2024-07-01",
        "test_end": "2024-12-31",
    },
    {
        "fold_id": "wf2_2025h1",
        "train_start": "2022-01-04",
        "valid_start": "2024-07-01",
        "test_start": "2025-01-02",
        "test_end": "2025-06-30",
    },
    {
        "fold_id": "wf3_2025h2",
        "train_start": "2022-01-04",
        "valid_start": "2025-01-02",
        "test_start": "2025-07-01",
        "test_end": "2025-12-31",
    },
    {
        "fold_id": "wf4_2026h1",
        "train_start": "2022-01-04",
        "valid_start": "2025-07-01",
        "test_start": "2026-01-05",
        "test_end": "2026-06-30",
    },
)


def _requested_folds(feature_manifest: dict[str, Any], calendar: pd.DatetimeIndex) -> list[dict[str, str]]:
    """Build four six-month folds backwards from the frozen snapshot end.

    A first-of-month snapshot end is treated as an exclusive boundary, which
    keeps the final fold on the last fully completed month.
    """
    raw_end = feature_manifest.get("resolved_end_date") or feature_manifest.get("actual_end_date") or feature_manifest.get("end_date")
    if not raw_end:
        raise ValueError("feature manifest has no rolling evaluation end")
    boundary = pd.Timestamp(raw_end).normalize()
    if boundary.day == 1:
        boundary -= pd.Timedelta(days=1)
    end_pos = calendar.searchsorted(boundary, side="right") - 1
    if end_pos < 0:
        raise ValueError("rolling evaluation end precedes calendar")
    final_end = pd.Timestamp(calendar[end_pos]).normalize()
    train_start = str(feature_manifest.get("actual_start_date") or feature_manifest.get("start_date") or "2022-01-04")
    requested: list[dict[str, str]] = []
    for reverse_index in range(3, -1, -1):
        fold_end_boundary = final_end + pd.Timedelta(days=1) - pd.DateOffset(months=6 * reverse_index)
        fold_end_pos = calendar.searchsorted(fold_end_boundary, side="left") - 1
        test_end = pd.Timestamp(calendar[fold_end_pos]).normalize()
        test_start_target = test_end + pd.Timedelta(days=1) - pd.DateOffset(months=6)
        test_start = _calendar_date(calendar, str(test_start_target.date()))
        valid_start_target = test_start - pd.DateOffset(months=6)
        valid_start = _calendar_date(calendar, str(valid_start_target.date()))
        requested.append(
            {
                "fold_id": f"wf{len(requested) + 1}_{test_start.strftime('%Y%m')}_{test_end.strftime('%Y%m')}",
                "train_start": train_start,
                "valid_start": str(valid_start.date()),
                "test_start": str(test_start.date()),
                "test_end": str(test_end.date()),
            }
        )
    return requested


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _qlib_source_identity() -> dict[str, Any]:
    from storage.paths import QLIB_SOURCE_ROOT

    root = QLIB_SOURCE_ROOT
    payload: dict[str, Any] = {"source_root": str(root)}
    try:
        payload["git_commit"] = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, timeout=10
        ).strip()
    except Exception as exc:
        payload["git_commit_error"] = str(exc)
    return payload


def _calendar() -> pd.DatetimeIndex:
    dates = pd.to_datetime(CALENDAR_FILE.read_text(encoding="utf-8").splitlines())
    return pd.DatetimeIndex(dates).normalize().sort_values().unique()


def _calendar_date(calendar: pd.DatetimeIndex, value: str) -> pd.Timestamp:
    target = pd.Timestamp(value).normalize()
    position = calendar.searchsorted(target, side="left")
    if position >= len(calendar):
        raise ValueError(f"calendar date is out of range: {value}")
    return pd.Timestamp(calendar[position]).normalize()


def _purged_end(
    calendar: pd.DatetimeIndex,
    next_segment_start: str,
    *,
    label_exit_shift_days: int,
) -> pd.Timestamp:
    boundary = _calendar_date(calendar, next_segment_start)
    boundary_index = int(calendar.get_loc(boundary))
    # A signal on day T uses the exit price on T + label_exit_shift_days.
    # Require that exit date to be strictly earlier than the next segment.
    end_index = boundary_index - int(label_exit_shift_days) - 1
    if end_index < 0:
        raise ValueError(f"insufficient calendar history before {next_segment_start}")
    return pd.Timestamp(calendar[end_index]).normalize()


def _build_folds(feature_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    calendar = _calendar()
    exit_shift = int(feature_manifest.get("label_exit_shift_days") or 0)
    if exit_shift <= 0:
        raise ValueError("feature manifest is missing positive label_exit_shift_days")
    folds: list[dict[str, Any]] = []
    for requested in _requested_folds(feature_manifest, calendar):
        train_start = _calendar_date(calendar, requested["train_start"])
        valid_start = _calendar_date(calendar, requested["valid_start"])
        test_start = _calendar_date(calendar, requested["test_start"])
        test_end_position = calendar.searchsorted(pd.Timestamp(requested["test_end"]), side="right") - 1
        test_end = pd.Timestamp(calendar[test_end_position]).normalize()
        train_end = _purged_end(calendar, str(valid_start.date()), label_exit_shift_days=exit_shift)
        valid_end = _purged_end(calendar, str(test_start.date()), label_exit_shift_days=exit_shift)
        train_exit = pd.Timestamp(calendar[int(calendar.get_loc(train_end)) + exit_shift])
        valid_exit = pd.Timestamp(calendar[int(calendar.get_loc(valid_end)) + exit_shift])
        checks = {
            "train_label_exit_before_valid_start": bool(train_exit < valid_start),
            "valid_label_exit_before_test_start": bool(valid_exit < test_start),
            "test_window_not_reversed": bool(test_start <= test_end),
        }
        if not all(checks.values()):
            raise ValueError(f"fold boundary check failed for {requested['fold_id']}: {checks}")
        folds.append(
            {
                "fold_id": requested["fold_id"],
                "segments": {
                    "train": [str(train_start.date()), str(train_end.date())],
                    "valid": [str(valid_start.date()), str(valid_end.date())],
                    "test": [str(test_start.date()), str(test_end.date())],
                },
                "label_boundary": {
                    "label_exit_shift_days": exit_shift,
                    "train_last_label_exit": str(train_exit.date()),
                    "valid_first_feature_date": str(valid_start.date()),
                    "valid_last_label_exit": str(valid_exit.date()),
                    "test_first_feature_date": str(test_start.date()),
                },
                "checks": checks,
            }
        )
    return folds


def _completed_fold_result(fold_dir: Path, expected_segments: dict[str, list[str]]) -> dict[str, Any] | None:
    result_path = fold_dir / "fold_result.json"
    pred_path = fold_dir / "pred.pkl"
    if not result_path.exists() or not pred_path.exists():
        return None
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("status") != "complete" or payload.get("segments") != expected_segments:
        return None
    return payload


def _prediction_audit(pred: pd.Series, test_segment: list[str]) -> dict[str, Any]:
    if not isinstance(pred.index, pd.MultiIndex):
        raise ValueError("prediction index is not MultiIndex")
    datetimes = pd.to_datetime(pred.index.get_level_values("datetime")).normalize()
    start, end = pd.Timestamp(test_segment[0]), pd.Timestamp(test_segment[1])
    outside = (datetimes < start) | (datetimes > end)
    return {
        "row_count": int(len(pred)),
        "date_count": int(pd.Index(datetimes).nunique()),
        "instrument_count": int(pred.index.get_level_values("instrument").nunique()),
        "date_min": str(pd.Timestamp(datetimes.min()).date()),
        "date_max": str(pd.Timestamp(datetimes.max()).date()),
        "duplicate_index_count": int(pred.index.duplicated().sum()),
        "outside_test_row_count": int(outside.sum()),
        "nan_score_count": int(pd.to_numeric(pred, errors="coerce").isna().sum()),
    }


def _next_calendar_date(calendar: pd.DatetimeIndex, value: str | pd.Timestamp) -> pd.Timestamp | None:
    target = pd.Timestamp(value).normalize()
    position = calendar.searchsorted(target, side="right")
    if position >= len(calendar):
        return None
    return pd.Timestamp(calendar[position]).normalize()


def _fold_window_contract(
    calendar: pd.DatetimeIndex,
    report_index: pd.Index,
    test_segment: list[str],
) -> dict[str, Any]:
    """Describe signal dates separately from the dates on which they trade."""

    signal_start, signal_end = (pd.Timestamp(value).normalize() for value in test_segment)
    theoretical_start = _next_calendar_date(calendar, signal_start)
    theoretical_end = _next_calendar_date(calendar, signal_end)
    report_dates = pd.DatetimeIndex(pd.to_datetime(report_index)).normalize().sort_values().unique()
    actual_dates = report_dates
    if theoretical_start is not None:
        actual_dates = actual_dates[actual_dates >= theoretical_start]
    if theoretical_end is not None:
        actual_dates = actual_dates[actual_dates <= theoretical_end]
    return {
        "signal_window": [str(signal_start.date()), str(signal_end.date())],
        "execution_shift_trading_days": 1,
        "theoretical_execution_window": [
            str(theoretical_start.date()) if theoretical_start is not None else None,
            str(theoretical_end.date()) if theoretical_end is not None else None,
        ],
        "observed_execution_window": [
            str(pd.Timestamp(actual_dates.min()).date()) if len(actual_dates) else None,
            str(pd.Timestamp(actual_dates.max()).date()) if len(actual_dates) else None,
        ],
        "observed_execution_days": int(len(actual_dates)),
        "last_signal_executed_in_backtest": bool(
            theoretical_end is not None and len(report_dates) > 0 and theoretical_end <= report_dates.max()
        ),
    }


def _label_coverage(label: pd.Series | pd.DataFrame, pred: pd.Series) -> dict[str, Any]:
    if isinstance(label, pd.DataFrame):
        label = label.iloc[:, 0]
    label = pd.to_numeric(label, errors="coerce")
    pred_dates = pd.DatetimeIndex(pd.to_datetime(pred.index.get_level_values("datetime"))).normalize()
    available = label.notna()
    available_dates = pd.DatetimeIndex(
        pd.to_datetime(label.index.get_level_values("datetime")[available])
    ).normalize()
    available_date_set = set(available_dates)
    prediction_date_set = set(pred_dates)
    missing_realized_dates = sorted(prediction_date_set - available_date_set)
    return {
        "prediction_date_count": int(len(prediction_date_set)),
        "realized_label_date_count": int(len(prediction_date_set & available_date_set)),
        "label_data_end": str(pd.Timestamp(available_dates.max()).date()) if len(available_dates) else None,
        "signal_dates_without_realized_label_count": int(len(missing_realized_dates)),
        "signal_dates_without_realized_label": [str(pd.Timestamp(value).date()) for value in missing_realized_dates],
    }


def _fold_portfolio_metrics(
    report: pd.DataFrame,
    window_contract: dict[str, Any],
) -> dict[str, Any]:
    from qlib.contrib.evaluate import risk_analysis

    observed = window_contract["observed_execution_window"]
    frame = report.copy()
    frame.index = pd.to_datetime(frame.index)
    if observed[0] is None or observed[1] is None:
        frame = frame.iloc[0:0]
    else:
        start, end = pd.Timestamp(observed[0]), pd.Timestamp(observed[1])
        frame = frame[(frame.index >= start) & (frame.index <= end)]
    net = pd.to_numeric(frame["return"], errors="coerce") - pd.to_numeric(frame["cost"], errors="coerce")
    bench = pd.to_numeric(frame.get("bench", 0.0), errors="coerce")
    excess_risk = risk_analysis(net - bench)
    net_risk = risk_analysis(net)
    gross_risk = risk_analysis(pd.to_numeric(frame["return"], errors="coerce"))
    return {
        "metric_authority": "continuous_stitched_account_execution_slice",
        "window_contract": window_contract,
        "report_days": int(len(frame)),
        "excess_annualized_ret_with_cost": _risk_value(excess_risk, "annualized_return"),
        "excess_information_ratio_with_cost": _risk_value(excess_risk, "information_ratio"),
        "strategy_annualized_ret": _risk_value(net_risk, "annualized_return"),
        "max_drawdown": _risk_value(net_risk, "max_drawdown"),
        "net_strategy_annualized_ret": _risk_value(net_risk, "annualized_return"),
        "net_max_drawdown": _risk_value(net_risk, "max_drawdown"),
        "gross_strategy_annualized_ret": _risk_value(gross_risk, "annualized_return"),
        "gross_max_drawdown": _risk_value(gross_risk, "max_drawdown"),
        "avg_turnover": float(pd.to_numeric(frame["turnover"], errors="coerce").mean()),
        "avg_cost": float(pd.to_numeric(frame["cost"], errors="coerce").mean()),
    }


def _portfolio_boundary_audit(
    report: pd.DataFrame,
    positions_path: Path,
    folds: list[dict[str, Any]],
    *,
    initial_account: float = 100_000_000.0,
) -> dict[str, Any]:
    with positions_path.open("rb") as handle:
        positions = pickle.load(handle)
    if not isinstance(positions, dict) or not positions:
        return {"passed": False, "reason": "positions_not_available", "boundaries": []}
    report_frame = report.copy()
    report_frame.index = pd.to_datetime(report_frame.index)
    account_return = pd.to_numeric(report_frame["account"], errors="coerce").pct_change()
    net_return = (
        pd.to_numeric(report_frame["return"], errors="coerce")
        - pd.to_numeric(report_frame["cost"], errors="coerce")
    )
    continuity_residual = (account_return - net_return).abs()
    position_keys = sorted(pd.Timestamp(key) for key in positions)
    rows: list[dict[str, Any]] = []
    for fold in folds[1:]:
        boundary = pd.Timestamp(fold["segments"]["test"][0])
        prior_key = max(key for key in position_keys if key < boundary)
        current_key = min(key for key in position_keys if key >= boundary)
        prior_position = positions[prior_key]
        current_position = positions[current_key]
        prior_stocks = set(prior_position.get_stock_list()) if hasattr(prior_position, "get_stock_list") else set()
        current_stocks = set(current_position.get_stock_list()) if hasattr(current_position, "get_stock_list") else set()
        prior_account = float(report_frame.loc[prior_key, "account"])
        current_account = float(report_frame.loc[current_key, "account"])
        reset_to_initial = abs(current_account - initial_account) <= max(1.0, initial_account * 1.0e-9)
        boundary_residual = float(continuity_residual.loc[current_key])
        account_math_continuous = bool(pd.notna(boundary_residual) and boundary_residual <= 1.0e-10)
        rows.append(
            {
                "boundary": str(boundary.date()),
                "prior_date": str(prior_key.date()),
                "current_date": str(current_key.date()),
                "prior_account": prior_account,
                "current_account": current_account,
                "prior_holding_count": len(prior_stocks),
                "current_holding_count": len(current_stocks),
                "holding_overlap_count": len(prior_stocks & current_stocks),
                "reset_to_initial_account": reset_to_initial,
                "account_return": float(account_return.loc[current_key]),
                "reported_net_return": float(net_return.loc[current_key]),
                "continuity_residual": boundary_residual,
                "account_math_continuous": account_math_continuous,
                "passed": account_math_continuous,
            }
        )
    finite_residual = continuity_residual.dropna()
    return {
        "passed": all(row["passed"] for row in rows),
        "not_applicable": not rows,
        "method": "account_pct_change_equals_return_minus_cost",
        "tolerance": 1.0e-10,
        "full_report_max_continuity_residual": float(finite_residual.max()) if len(finite_residual) else None,
        "boundaries": rows,
    }


def _report_integrity(report: pd.DataFrame) -> dict[str, Any]:
    numeric = report.apply(pd.to_numeric, errors="coerce")
    inf_count = int(numeric.isin([float("inf"), float("-inf")]).sum().sum())
    nan_count = int(numeric.isna().sum().sum())
    return {
        "row_count": int(len(report)),
        "index_unique": bool(report.index.is_unique),
        "index_monotonic": bool(report.index.is_monotonic_increasing),
        "nan_count": nan_count,
        "inf_count": inf_count,
        "passed": bool(report.index.is_unique and report.index.is_monotonic_increasing and nan_count == 0 and inf_count == 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Model four-fold rolling diagnostic.")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--feature-set-id", default="")
    parser.add_argument("--source-round-group-id", default="")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--max-folds", type=int, default=4)
    args = parser.parse_args()

    active = json.loads(ACTIVE_FEATURE_SET.read_text(encoding="utf-8"))
    feature_set_id = args.feature_set_id or str(active.get("feature_set_id") or "")
    if feature_set_id == str(active.get("feature_set_id") or ""):
        feature_manifest = active
    else:
        manifest_path = MODEL_FEATURE_SETS_ROOT / feature_set_id / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(manifest_path)
        feature_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    feature_path = Path(str(feature_manifest.get("combined_factors_file") or feature_manifest.get("combined_file") or ""))
    if not feature_path.is_absolute():
        feature_path = MODEL_FEATURE_SETS_ROOT / feature_set_id / "combined_factors_df.parquet"
    if not feature_path.exists():
        raise FileNotFoundError(feature_path)
    run_id = args.run_id or datetime.now().strftime(f"wf4_all33_seed{int(args.seed)}_%Y%m%d_%H%M%S")
    run_root = OUT_ROOT / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    result_path = run_root / "result.json"
    feature_identity_before = {
        "path": str(feature_path),
        "size": feature_path.stat().st_size,
        "mtime_ns": feature_path.stat().st_mtime_ns,
    }
    folds = _build_folds(feature_manifest)[: max(1, min(int(args.max_folds), 4))]
    source_experiment = default_r1_experiment()
    if args.source_round_group_id:
        from domain.model.state_store import ModelStateStore

        source_round = ModelStateStore().get_round(args.source_round_group_id)
        if not source_round:
            raise ValueError(f"source round not found: {args.source_round_group_id}")
        if str(source_round.get("feature_set_id") or "") != feature_set_id:
            raise ValueError("source round feature_set_id does not match rolling feature_set_id")
        source_experiment = dict(source_round.get("experiment") or source_experiment)
    base = {
        "schema_version": "model_rolling_fourfold_diagnostic_v1",
        "status": "running",
        "run_id": run_id,
        "started_at": _utc_now(),
        "feature_set_id": feature_set_id,
        "source_round_group_id": args.source_round_group_id or None,
        "feature_set_fingerprint": feature_manifest.get("feature_set_fingerprint"),
        "feature_identity_before": feature_identity_before,
        "factor_count": len(feature_manifest.get("factor_ids") or []),
        "seed": int(args.seed),
        "portfolio": dict(DEFAULT_PORTFOLIO),
        "qlib_source": _qlib_source_identity(),
        "folds": folds,
        "production_registry_write": False,
        "active_pointer_update": False,
    }
    _write_json(result_path, base)

    fold_results: list[dict[str, Any]] = []
    predictions: list[pd.Series] = []
    for index, fold in enumerate(folds, 1):
        fold_dir = run_root / "folds" / fold["fold_id"]
        fold_dir.mkdir(parents=True, exist_ok=True)
        completed = _completed_fold_result(fold_dir, fold["segments"])
        print(f"[rolling] fold {index}/{len(folds)} {fold['fold_id']} start", flush=True)
        if completed is None:
            experiment = dict(source_experiment)
            experiment["segments"] = fold["segments"]
            direct = run_direct_qlib_seed(
                feature_set_id=feature_set_id,
                experiment=experiment,
                seed=int(args.seed),
                run_dir=fold_dir,
            )
            pred = pd.read_pickle(fold_dir / "pred.pkl")
            if isinstance(pred, pd.DataFrame):
                pred = pred.iloc[:, 0]
            audit = _prediction_audit(pred, fold["segments"]["test"])
            if audit["duplicate_index_count"] or audit["outside_test_row_count"]:
                raise ValueError(f"prediction audit failed for {fold['fold_id']}: {audit}")
            completed = {
                "status": "complete",
                "fold_id": fold["fold_id"],
                "segments": fold["segments"],
                "label_boundary": fold["label_boundary"],
                "checks": fold["checks"],
                "standalone_fold_diagnostic_metrics": direct.get("metrics") or {},
                "standalone_metric_authority": "diagnostic_only_account_resets_per_fold",
                "prediction_audit": audit,
                "label_coverage": _label_coverage(pd.read_pickle(fold_dir / "label.pkl"), pred),
                "artifacts": direct.get("artifacts") or {},
                "completed_at": _utc_now(),
            }
            _write_json(fold_dir / "fold_result.json", completed)
        else:
            pred = pd.read_pickle(fold_dir / "pred.pkl")
            if isinstance(pred, pd.DataFrame):
                pred = pred.iloc[:, 0]
            if "metrics" in completed and "standalone_fold_diagnostic_metrics" not in completed:
                completed["standalone_fold_diagnostic_metrics"] = completed.pop("metrics")
                completed["standalone_metric_authority"] = "diagnostic_only_account_resets_per_fold"
            label_path = fold_dir / "label.pkl"
            if label_path.exists():
                completed["label_coverage"] = _label_coverage(pd.read_pickle(label_path), pred)
            completed["reused_existing"] = True
        fold_results.append(completed)
        predictions.append(pred.rename("score"))
        _write_json(result_path, {**base, "fold_results": fold_results, "completed_folds": len(fold_results)})
        print(f"[rolling] fold {index}/{len(folds)} {fold['fold_id']} complete", flush=True)
        del pred
        gc.collect()

    stitched = pd.concat(predictions).sort_index().rename("score")
    duplicate_index_count = int(stitched.index.duplicated().sum())
    if duplicate_index_count:
        raise ValueError(f"stitched prediction has {duplicate_index_count} duplicate rows")
    stitched_path = run_root / "stitched_pred.pkl"
    stitched.to_pickle(stitched_path)
    stitched_dates = pd.to_datetime(stitched.index.get_level_values("datetime")).normalize()
    stitched_audit = {
        "row_count": int(len(stitched)),
        "date_count": int(pd.Index(stitched_dates).nunique()),
        "instrument_count": int(stitched.index.get_level_values("instrument").nunique()),
        "date_min": str(pd.Timestamp(stitched_dates.min()).date()),
        "date_max": str(pd.Timestamp(stitched_dates.max()).date()),
        "duplicate_index_count": duplicate_index_count,
        "single_continuous_backtest": True,
    }
    report, rolling_metrics, portfolio_artifacts, backtest_error = _run_backtest(
        stitched,
        portfolio=dict(DEFAULT_PORTFOLIO),
        benchmark="000300sh",
        artifact_dir=run_root / "stitched_portfolio_analysis",
        label="fourfold_stitched_top20_drop2",
    )
    if backtest_error:
        raise RuntimeError(backtest_error)
    report_path = run_root / "stitched_ret.pkl"
    report.to_pickle(report_path)
    curve = pd.DataFrame(index=pd.to_datetime(report.index))
    curve["net_return"] = pd.to_numeric(report["return"], errors="coerce") - pd.to_numeric(report["cost"], errors="coerce")
    curve["benchmark_return"] = pd.to_numeric(report.get("bench", 0.0), errors="coerce")
    curve["excess_return"] = curve["net_return"] - curve["benchmark_return"]
    curve["strategy_curve"] = (1.0 + curve["net_return"].fillna(0.0)).cumprod()
    curve["benchmark_curve"] = (1.0 + curve["benchmark_return"].fillna(0.0)).cumprod()
    curve["excess_curve"] = (1.0 + curve["excess_return"].fillna(0.0)).cumprod()
    curve.to_csv(run_root / "stitched_curve.csv", index_label="datetime")
    calendar = _calendar()
    fold_window_contracts = {
        fold["fold_id"]: _fold_window_contract(calendar, report.index, fold["segments"]["test"])
        for fold in folds
    }
    fold_portfolio_metrics = {
        fold["fold_id"]: _fold_portfolio_metrics(report, fold_window_contracts[fold["fold_id"]])
        for fold in folds
    }
    report_integrity = _report_integrity(report)
    positions_path = Path(str(portfolio_artifacts.get("positions_pkl") or ""))
    portfolio_boundary_audit = _portfolio_boundary_audit(report, positions_path, folds)
    feature_identity_after = {
        "path": str(feature_path),
        "size": feature_path.stat().st_size,
        "mtime_ns": feature_path.stat().st_mtime_ns,
    }
    reliability = {
        "all_fold_boundary_checks_passed": all(all(fold["checks"].values()) for fold in folds),
        "all_fold_prediction_audits_passed": all(
            int(result["prediction_audit"].get("duplicate_index_count") or 0) == 0
            and int(result["prediction_audit"].get("outside_test_row_count") or 0) == 0
            for result in fold_results
        ),
        "stitched_prediction_unique": duplicate_index_count == 0,
        "feature_input_unchanged_during_run": feature_identity_before == feature_identity_after,
        "continuous_portfolio_was_run_once_after_prediction_stitch": True,
        "portfolio_state_continuous_at_fold_boundaries": bool(portfolio_boundary_audit.get("passed")),
        "stitched_report_integrity_passed": bool(report_integrity.get("passed")),
        "production_registry_untouched": True,
    }
    final = {
        **base,
        "status": "complete" if all(reliability.values()) else "failed_reliability_check",
        "completed_at": _utc_now(),
        "fold_results": fold_results,
        "metric_authority": {
            "formal": "rolling_metrics_and_fold_portfolio_metrics_from_one_continuous_stitched_account",
            "diagnostic_only": "fold_results.standalone_fold_diagnostic_metrics",
        },
        "fold_window_contracts": fold_window_contracts,
        "fold_portfolio_metrics": fold_portfolio_metrics,
        "stitched_prediction_audit": stitched_audit,
        "rolling_metrics": rolling_metrics,
        "portfolio_artifacts": portfolio_artifacts,
        "portfolio_boundary_audit": portfolio_boundary_audit,
        "report_integrity": report_integrity,
        "feature_identity_after": feature_identity_after,
        "reliability": reliability,
        "artifacts": {
            "result": str(result_path),
            "stitched_prediction": str(stitched_path),
            "stitched_return": str(report_path),
            "stitched_curve_csv": str(run_root / "stitched_curve.csv"),
        },
    }
    _write_json(result_path, final)
    print(json.dumps(_jsonable({"status": final["status"], "result": str(result_path), "rolling_metrics": rolling_metrics, "reliability": reliability}), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
