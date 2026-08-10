from __future__ import annotations

import json
import pickle
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from domain.data_foundation.stock_metadata import load_stock_identity_map, market_code_to_instrument, security_name_for_instrument
from storage.paths import PRODUCTION_RAW_HDF5, QLIB_DATA_ROOT

from .contracts import (
    DEFAULT_PORTFOLIO,
    DEFAULT_SAMPLE_WEIGHT_POLICY,
    LIMIT_THRESHOLD,
    MODEL_SYSTEM_VERSION,
    is_model_system_version,
)
from .scoring import performance_hard_blocks


VALIDATION_RULE_VERSION = "model_validation_v2_single_top20"
_STYLE_AUDIT_FRAME_CACHE: pd.DataFrame | None = None
_PIT_STATUS_FRAME_CACHE: pd.DataFrame | None = None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except Exception:
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_pickle(path: Path) -> Any:
    with path.open("rb") as fh:
        return pickle.load(fh)


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _manifest_artifacts(manifest: dict[str, Any]) -> dict[str, Any]:
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    direct = manifest.get("direct_qlib") if isinstance(manifest.get("direct_qlib"), dict) else {}
    direct_artifacts = direct.get("artifacts") if isinstance(direct.get("artifacts"), dict) else {}
    return {**direct_artifacts, **artifacts}


def _path_exists(raw: Any) -> bool:
    return bool(raw) and Path(str(raw)).exists()


def _artifact_check(run_dir: Path, manifest: dict[str, Any] | None = None) -> tuple[dict[str, Any], pd.DataFrame | None, Any | None]:
    manifest = manifest or {}
    required = {
        "manifest": run_dir / "manifest.json",
        "metrics": run_dir / "metrics.json",
        "ret": run_dir / "ret.pkl",
        "pred": run_dir / "pred.pkl",
    }
    errors: list[str] = []
    warnings: list[str] = []
    readable: dict[str, bool] = {}
    ret_df: pd.DataFrame | None = None
    pred_obj: Any | None = None
    for name, path in required.items():
        if not path.exists():
            errors.append(f"{name}_missing:{path}")
            readable[name] = False
            continue
        try:
            if path.suffix == ".json":
                payload_json = json.loads(path.read_text(encoding="utf-8"))
                if name == "metrics" and not payload_json:
                    errors.append("metrics_json_empty")
            else:
                payload = _read_pickle(path)
                if name == "ret":
                    if not isinstance(payload, pd.DataFrame):
                        errors.append("ret_pkl_not_dataframe")
                    elif payload.empty:
                        errors.append("ret_pkl_empty")
                    else:
                        ret_df = payload
                elif name == "pred":
                    if not hasattr(payload, "empty") and not hasattr(payload, "__len__"):
                        errors.append("pred_pkl_not_series_or_dataframe")
                    elif hasattr(payload, "empty") and bool(payload.empty):
                        errors.append("pred_pkl_empty")
                    elif hasattr(payload, "__len__") and len(payload) == 0:
                        errors.append("pred_pkl_empty")
                    else:
                        pred_obj = payload
            readable[name] = True
        except Exception as exc:
            errors.append(f"{name}_unreadable:{exc}")
            readable[name] = False
    if ret_df is not None:
        required_cols = {"account", "return", "turnover", "cost", "bench"}
        missing = sorted(required_cols - set(ret_df.columns))
        if missing:
            errors.append("ret_pkl_missing_columns:" + ",".join(missing))
        if len(ret_df) < 20:
            warnings.append("ret_pkl_short_history_below_20_days")
    if (manifest.get("runner") or {}).get("execute_qlib"):
        artifacts = _manifest_artifacts(manifest)
        for name, fallback in {
            "label": run_dir / "label.pkl",
            "params": run_dir / "params.pkl",
        }.items():
            raw = artifacts.get(name) or fallback
            readable[name] = _path_exists(raw)
            if not readable[name]:
                errors.append(f"{name}_missing:{raw}")
    return (
        {
            "status": "blocked" if errors else ("review_required" if warnings else "clean"),
            "errors": errors,
            "warnings": warnings,
            "readable": readable,
            "run_dir": str(run_dir),
        },
        ret_df,
        pred_obj,
    )


def _manifest_contract_check(manifest: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not is_model_system_version(manifest.get("model_system_version")):
        errors.append("model_system_version_mismatch")
    runner = manifest.get("runner") if isinstance(manifest.get("runner"), dict) else {}
    if not runner.get("execute_qlib"):
        errors.append("shadow_runner_not_production_eligible")
    if runner.get("direct_qlib_error"):
        errors.append(f"direct_qlib_error:{runner.get('direct_qlib_error')}")

    experiment = manifest.get("experiment") if isinstance(manifest.get("experiment"), dict) else {}
    resolved_windows = manifest.get("resolved_windows") if isinstance(manifest.get("resolved_windows"), dict) else {}
    segments = resolved_windows.get("segments") if isinstance(resolved_windows.get("segments"), dict) else experiment.get("segments")
    if not isinstance(segments, dict):
        errors.append("resolved_segments_missing")
    else:
        for key in ("train", "valid", "test"):
            window = segments.get(key)
            if not isinstance(window, (list, tuple)) or len(window) < 2 or not window[0] or not window[1]:
                errors.append(f"resolved_segment_{key}_missing")
    if experiment.get("sample_weight_policy") != DEFAULT_SAMPLE_WEIGHT_POLICY:
        errors.append(f"unsupported_sample_weight_policy:{experiment.get('sample_weight_policy')}")
    if bool(experiment.get("pre_shift_pred")):
        errors.append("pred_pkl_must_not_be_pre_shifted")

    processors = manifest.get("resolved_processors") if isinstance(manifest.get("resolved_processors"), dict) else {}
    infer = [str(item.get("class")) for item in processors.get("infer_processors") or [] if isinstance(item, dict)]
    learn = [str(item.get("class")) for item in processors.get("learn_processors") or [] if isinstance(item, dict)]
    for expected in ("ProcessInf", "RobustZScoreNorm", "CSZFillna"):
        if expected not in infer:
            errors.append(f"infer_processor_missing:{expected}")
    for expected in ("DropnaLabel", "CSZScoreNorm"):
        if expected not in learn:
            errors.append(f"learn_processor_missing:{expected}")

    portfolio = manifest.get("resolved_portfolio_params") if isinstance(manifest.get("resolved_portfolio_params"), dict) else {}
    configured = portfolio.get("portfolio") if isinstance(portfolio.get("portfolio"), dict) else {}
    if str(portfolio.get("deal_price") or "") != "open":
        errors.append("portfolio_deal_price_must_be_open")
    if list(portfolio.get("limit_threshold") or []) != list(LIMIT_THRESHOLD):
        errors.append("limit_threshold_mismatch")
    checks = [
        ("portfolio_topk", configured.get("topk"), DEFAULT_PORTFOLIO["topk"]),
        ("portfolio_n_drop", configured.get("n_drop"), DEFAULT_PORTFOLIO["n_drop"]),
        ("portfolio_hold_thresh", configured.get("hold_thresh"), DEFAULT_PORTFOLIO["hold_thresh"]),
    ]
    for label, value, expected in checks:
        if int(value if value is not None else expected) != int(expected):
            errors.append(f"{label}_mismatch:{value}!=expected:{expected}")
    return {"status": "blocked" if errors else ("review_required" if warnings else "clean"), "errors": errors, "warnings": warnings}


def _portfolio_artifact_check(manifest: dict[str, Any]) -> dict[str, Any]:
    runner = manifest.get("runner") if isinstance(manifest.get("runner"), dict) else {}
    if not runner.get("execute_qlib"):
        return {"status": "review_required", "errors": [], "warnings": ["portfolio_artifacts_skipped_for_shadow_runner"]}
    portfolio = manifest.get("resolved_portfolio_params") if isinstance(manifest.get("resolved_portfolio_params"), dict) else {}
    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}
    artifacts = portfolio.get("portfolio_artifacts") if isinstance(portfolio.get("portfolio_artifacts"), dict) else {}
    report = artifacts.get("report_pkl")
    positions = artifacts.get("positions_pkl")
    summary = artifacts.get("summary_file")
    details["portfolio"] = {"report_pkl": report, "positions_pkl": positions, "summary_file": summary}
    if not _path_exists(report):
        errors.append("portfolio_report_missing")
    if not _path_exists(positions):
        errors.append("portfolio_positions_missing")
    if not _path_exists(summary):
        warnings.append("portfolio_summary_missing")
    return {
        "status": "blocked" if errors else ("review_required" if warnings else "clean"),
        "errors": errors,
        "warnings": warnings,
        "details": details,
    }


def _daily_checks(ret_df: pd.DataFrame | None, metrics: dict[str, Any]) -> dict[str, Any]:
    if ret_df is None or ret_df.empty:
        return {"status": "blocked", "errors": ["ret_daily_unavailable"], "warnings": []}
    warnings: list[str] = []
    errors: list[str] = []
    turnover = pd.to_numeric(ret_df.get("turnover"), errors="coerce").fillna(0.0)
    cost = pd.to_numeric(ret_df.get("cost"), errors="coerce").fillna(0.0)
    returns = pd.to_numeric(ret_df.get("return"), errors="coerce").fillna(0.0)
    bench = pd.to_numeric(ret_df.get("bench"), errors="coerce").fillna(0.0)
    if float(turnover.mean()) > 0.35:
        warnings.append("mean_turnover_above_35pct")
    if float(turnover.quantile(0.95)) > 0.80:
        warnings.append("p95_turnover_above_80pct")
    if float(cost.mean()) <= 0:
        warnings.append("cost_series_all_zero_or_missing")
    excess = returns - cost - bench
    positive_ratio = float((excess > 0).mean()) if len(excess) else 0.0
    if len(excess) >= 20 and positive_ratio < 0.40:
        warnings.append("daily_excess_positive_ratio_below_40pct")
    hard_blocks = performance_hard_blocks(metrics)
    errors.extend(hard_blocks)
    return {
        "status": "blocked" if errors else ("review_required" if warnings else "clean"),
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "day_count": int(len(ret_df)),
            "mean_turnover": _safe_float(turnover.mean()),
            "p95_turnover": _safe_float(turnover.quantile(0.95)),
            "mean_cost": _safe_float(cost.mean()),
            "daily_excess_positive_ratio": _safe_float(positive_ratio),
        },
    }


def _cost_concentration_checks(ret_df: pd.DataFrame | None) -> dict[str, Any]:
    if ret_df is None or ret_df.empty:
        return {"status": "blocked", "errors": ["ret_daily_unavailable"], "warnings": []}
    warnings: list[str] = []
    returns = pd.to_numeric(ret_df.get("return"), errors="coerce").fillna(0.0)
    cost = pd.to_numeric(ret_df.get("cost"), errors="coerce").fillna(0.0)
    bench = pd.to_numeric(ret_df.get("bench"), errors="coerce").fillna(0.0)
    net = returns - cost - bench
    gross_abs = float(returns.abs().sum())
    cost_abs = float(cost.abs().sum())
    positive = net[net > 0].sort_values(ascending=False)
    if gross_abs > 0 and cost_abs / gross_abs > 0.30:
        warnings.append("aggregate_cost_above_30pct_of_abs_gross_return")
    if len(positive) >= 5 and float(positive.head(5).sum()) / max(float(positive.sum()), 1e-12) > 0.70:
        warnings.append("top5_positive_days_explain_above_70pct_positive_excess")
    if len(net) >= 20 and float(net.abs().max()) > max(float(net.abs().median()) * 12.0, 0.05):
        warnings.append("single_day_return_outlier")
    return {
        "status": "review_required" if warnings else "clean",
        "errors": [],
        "warnings": warnings,
        "summary": {
            "aggregate_cost_to_abs_gross_return": _safe_float(cost_abs / gross_abs) if gross_abs > 0 else None,
            "max_abs_daily_excess": _safe_float(net.abs().max()),
        },
    }


def _prediction_checks(pred_obj: Any | None) -> dict[str, Any]:
    if pred_obj is None:
        return {"status": "blocked", "errors": ["pred_unavailable"], "warnings": []}
    warnings: list[str] = []
    errors: list[str] = []
    index = getattr(pred_obj, "index", None)
    names = list(getattr(index, "names", []) or [])
    if "datetime" not in names or "instrument" not in names:
        warnings.append("pred_index_not_multiindex_datetime_instrument")
    return {
        "status": "blocked" if errors else ("review_required" if warnings else "clean"),
        "errors": errors,
        "warnings": warnings,
        "summary": {"row_count": int(len(pred_obj)) if hasattr(pred_obj, "__len__") else None},
    }


def _tradability_checks(pred_obj: Any | None) -> dict[str, Any]:
    if pred_obj is None:
        return {"status": "blocked", "errors": ["pred_unavailable"], "warnings": []}
    try:
        prediction = _prediction_st_exposure(pred_obj)
        index = getattr(pred_obj, "index", None)
        names = list(getattr(index, "names", []) or [])
        unique_count = int(pd.Index(index.get_level_values("instrument")).astype(str).nunique()) if "instrument" in names else None
        warnings: list[str] = []
        if unique_count is not None and unique_count < 20:
            warnings.append("prediction_universe_below_20_instruments")
        if prediction.get("available"):
            topk = _safe_float(prediction.get("topk_avg_st_like_ratio")) or 0.0
            top50 = _safe_float(prediction.get("top50_avg_st_like_ratio")) or 0.0
            top50_p95 = _safe_float(prediction.get("top50_p95_st_like_ratio")) or 0.0
            top1 = _safe_float(prediction.get("score_top1pct_st_like_ratio")) or 0.0
            if topk > 0.10:
                warnings.append("prediction_top20_st_like_ratio_above_10pct")
            if top50 > 0.10:
                warnings.append("prediction_top50_st_like_ratio_above_10pct")
            if top50_p95 > 0.20:
                warnings.append("prediction_top50_p95_st_like_ratio_above_20pct")
            if top1 > 0.15:
                warnings.append("prediction_top1pct_st_like_ratio_above_15pct")
        else:
            warnings.append(str(prediction.get("reason") or "prediction_st_exposure_unavailable"))
    except Exception as exc:
        return {"status": "review_required", "errors": [], "warnings": [f"tradability_basic_check_failed:{exc}"]}
    return {
        "status": "review_required" if warnings else "clean",
        "errors": [],
        "warnings": warnings,
        "summary": {
            "unique_instruments": unique_count,
            "st_like_prediction_rows": prediction.get("topk_avg_st_like_count") if prediction.get("available") else None,
        },
        "note": "PIT ST/delist audit is computed from pred.pkl joined to production raw status by date/instrument.",
        "prediction": prediction,
        "risk_flags": {
            "top20_st_like_ratio": prediction.get("topk_avg_st_like_ratio"),
            "top50_st_like_ratio": prediction.get("top50_avg_st_like_ratio"),
            "top50_p95_st_like_ratio": prediction.get("top50_p95_st_like_ratio"),
            "top50_latest_st_like_ratio": prediction.get("top50_latest_st_like_ratio"),
            "top1pct_st_like_ratio": prediction.get("score_top1pct_st_like_ratio"),
        },
    }


def _style_exposure_checks(pred_obj: Any | None) -> dict[str, Any]:
    if pred_obj is None:
        return {"status": "blocked", "errors": ["pred_unavailable"], "warnings": []}
    try:
        series = pred_obj.iloc[:, 0] if isinstance(pred_obj, pd.DataFrame) else pred_obj
        values = pd.to_numeric(series, errors="coerce").dropna()
    except Exception as exc:
        return {"status": "review_required", "errors": [], "warnings": [f"prediction_style_summary_failed:{exc}"]}
    warnings: list[str] = []
    if values.empty:
        return {"status": "blocked", "errors": ["prediction_values_unavailable"], "warnings": []}
    if float(values.std()) <= 1e-12:
        warnings.append("prediction_score_degenerate_zero_variance")
    if float(values.abs().quantile(0.99)) > max(float(values.abs().median()) * 50.0, 1e-9):
        warnings.append("prediction_score_extreme_tail")
    return {
        "status": "review_required" if warnings else "clean",
        "errors": [],
        "warnings": warnings,
        "summary": {
            "score_mean": _safe_float(values.mean()),
            "score_std": _safe_float(values.std()),
            "score_p99_abs": _safe_float(values.abs().quantile(0.99)),
        },
    }


def _normalize_qlib_instrument(value: Any) -> str:
    text = str(value or "").strip()
    if re.match(r"^[A-Z]{2}\d{6}$", text):
        return f"{text[2:]}{text[:2].lower()}"
    return text.lower()


def _is_st_like_name(name: str) -> bool:
    text = str(name or "").strip()
    return bool(re.search(r"^(?:\*?ST|SST)|退市", text, flags=re.IGNORECASE))


def _pit_status_frame() -> tuple[pd.DataFrame | None, str]:
    global _PIT_STATUS_FRAME_CACHE
    if _PIT_STATUS_FRAME_CACHE is not None:
        return _PIT_STATUS_FRAME_CACHE, str(PRODUCTION_RAW_HDF5)
    if not PRODUCTION_RAW_HDF5.exists():
        return None, f"PIT status source missing: {PRODUCTION_RAW_HDF5}"
    try:
        try:
            raw = pd.read_hdf(
                PRODUCTION_RAW_HDF5,
                key="daily",
                columns=["code", "kline_time", "st_status", "list_status", "SECURITY_NAME"],
            )
        except TypeError:
            raw = pd.read_hdf(PRODUCTION_RAW_HDF5, key="daily")
    except Exception as exc:
        return None, f"PIT status source unreadable: {exc}"
    if raw.empty or "code" not in raw.columns:
        return None, "PIT status source empty or missing code"
    date_col = "kline_time" if "kline_time" in raw.columns else "trade_date"
    if date_col not in raw.columns:
        return None, "PIT status source missing date column"
    frame = raw.copy()
    frame["datetime"] = pd.to_datetime(frame[date_col], errors="coerce").dt.normalize()
    frame["instrument"] = frame["code"].astype(str).map(market_code_to_instrument).map(_normalize_qlib_instrument)
    frame = frame.dropna(subset=["datetime", "instrument"])
    keep = ["datetime", "instrument"]
    for column in ["st_status", "list_status", "SECURITY_NAME"]:
        if column not in frame.columns:
            frame[column] = ""
        keep.append(column)
    frame = frame[keep].drop_duplicates(["datetime", "instrument"], keep="last")
    frame = frame.set_index(["datetime", "instrument"]).sort_index()
    _PIT_STATUS_FRAME_CACHE = frame
    return frame, str(PRODUCTION_RAW_HDF5)


def _enrich_with_pit_status(rows: pd.DataFrame, *, date_column: str = "datetime") -> tuple[pd.DataFrame, str]:
    frame, source = _pit_status_frame()
    enriched = rows.copy()
    enriched[date_column] = pd.to_datetime(enriched[date_column], errors="coerce").dt.normalize()
    enriched["instrument"] = enriched["instrument"].astype(str).map(_normalize_qlib_instrument)
    if frame is None or frame.empty:
        for column in ["st_status", "list_status", "SECURITY_NAME"]:
            if column not in enriched.columns:
                enriched[column] = pd.NA
        enriched["pit_status_matched"] = False
        return enriched, source
    status = frame.reset_index()
    enriched = enriched.merge(
        status,
        how="left",
        left_on=[date_column, "instrument"],
        right_on=["datetime", "instrument"],
        suffixes=("", "_pit"),
    )
    if date_column != "datetime" and "datetime_pit" in enriched.columns:
        enriched = enriched.drop(columns=["datetime_pit"])
    status_cols = ["st_status", "list_status", "SECURITY_NAME"]
    enriched["pit_status_matched"] = enriched[status_cols].notna().any(axis=1)
    return enriched, source


def _is_st_like_pit_row(row: pd.Series, security_name: str = "") -> bool:
    def text_value(value: Any) -> str:
        if value is None:
            return ""
        try:
            if pd.isna(value):
                return ""
        except Exception:
            pass
        return str(value)

    list_status = text_value(row.get("list_status")).upper()
    st_status = text_value(row.get("st_status")).upper()
    if list_status and list_status not in {"L", "LISTED", "NORMAL"}:
        return True
    if st_status:
        return st_status in {"ST", "*ST", "SST", "PT", "DELIST", "DELISTED"}
    return _is_st_like_name(security_name or text_value(row.get("SECURITY_NAME")))


def _prediction_st_exposure(pred_obj: Any | None) -> dict[str, Any]:
    if pred_obj is None:
        return {"available": False, "reason": "pred.pkl missing or unreadable"}
    try:
        score = pred_obj.iloc[:, 0] if isinstance(pred_obj, pd.DataFrame) else pred_obj
        score = pd.to_numeric(score, errors="coerce").dropna()
    except Exception as exc:
        return {"available": False, "reason": f"pred.pkl unreadable: {exc}"}
    if score.empty or not isinstance(score.index, pd.MultiIndex):
        return {"available": False, "reason": "pred.pkl missing MultiIndex scores"}
    df = score.rename("score").reset_index()
    if "datetime" not in df.columns or "instrument" not in df.columns:
        return {"available": False, "reason": "prediction index missing datetime/instrument"}
    df, source = _enrich_with_pit_status(df, date_column="datetime")
    try:
        name_map = load_stock_identity_map()
    except Exception:
        name_map = {}
    df["security_name"] = df["SECURITY_NAME"].fillna("").astype(str)
    fallback_names = df["instrument"].map(lambda x: security_name_for_instrument(x, name_map))
    df["security_name"] = df["security_name"].where(df["security_name"].astype(bool), fallback_names)
    df["is_st_like"] = df.apply(lambda row: _is_st_like_pit_row(row, str(row.get("security_name") or "")), axis=1)
    ranked = df.sort_values(
        ["datetime", "score", "instrument"],
        ascending=[True, False, True],
        kind="mergesort",
    )
    topk = ranked.groupby("datetime").head(int(DEFAULT_PORTFOLIO["topk"]))
    top50 = ranked.groupby("datetime").head(50)
    topk_by_day = topk.groupby("datetime")["is_st_like"].agg(["sum", "mean", "count"])
    top50_by_day = top50.groupby("datetime")["is_st_like"].agg(["sum", "mean", "count"])
    latest_dt = ranked["datetime"].max()
    latest_topk = topk.loc[topk["datetime"] == latest_dt, ["datetime", "instrument", "security_name", "score", "is_st_like"]].copy()
    latest_top50_st = top50.loc[
        (top50["datetime"] == latest_dt) & top50["is_st_like"],
        ["datetime", "instrument", "security_name", "score", "is_st_like"],
    ].copy()
    for frame in (latest_topk, latest_top50_st):
        if not frame.empty:
            frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
    q99 = df["score"].quantile(0.99)
    top1 = df[df["score"] >= q99]
    latest_topk_rows = [
        {key: _json_safe(value) for key, value in row.items()}
        for row in latest_topk.to_dict(orient="records")
    ]
    latest_top50_st_rows = [
        {key: _json_safe(value) for key, value in row.items()}
        for row in latest_top50_st.to_dict(orient="records")
    ]
    return {
        "available": True,
        "status_source": source,
        "status_match_ratio": _safe_float(df["pit_status_matched"].mean()) if len(df) else 0.0,
        "universe_st_like_ratio": _safe_float(df["is_st_like"].mean()) if len(df) else 0.0,
        "topk": int(DEFAULT_PORTFOLIO["topk"]),
        "topk_day_count": int(len(topk_by_day)),
        "topk_avg_st_like_count": _safe_float(topk_by_day["sum"].mean()) if len(topk_by_day) else 0.0,
        "topk_avg_st_like_ratio": _safe_float(topk_by_day["mean"].mean()) if len(topk_by_day) else 0.0,
        "topk_any_st_like_day_ratio": _safe_float((topk_by_day["sum"] > 0).mean()) if len(topk_by_day) else 0.0,
        "top50_day_count": int(len(top50_by_day)),
        "top50_avg_st_like_count": _safe_float(top50_by_day["sum"].mean()) if len(top50_by_day) else 0.0,
        "top50_avg_st_like_ratio": _safe_float(top50_by_day["mean"].mean()) if len(top50_by_day) else 0.0,
        "top50_p95_st_like_ratio": _safe_float(top50_by_day["mean"].quantile(0.95)) if len(top50_by_day) else 0.0,
        "top50_latest_st_like_ratio": _safe_float(top50_by_day["mean"].iloc[-1]) if len(top50_by_day) else 0.0,
        "top50_any_st_like_day_ratio": _safe_float((top50_by_day["sum"] > 0).mean()) if len(top50_by_day) else 0.0,
        "score_top1pct_st_like_ratio": _safe_float(top1["is_st_like"].mean()) if len(top1) else 0.0,
        "latest_topk": latest_topk_rows,
        "latest_top50_st_hits": latest_top50_st_rows,
    }


def _load_qlib_style_source() -> pd.DataFrame:
    from .qlib_direct import _ensure_qlib0627_path

    _ensure_qlib0627_path()
    import qlib
    from qlib.config import REG_CN
    from qlib.data import D

    qlib.init(
        provider_uri=str(QLIB_DATA_ROOT),
        region=REG_CN,
        auto_mount=False,
        expression_cache=None,
        dataset_cache=None,
    )
    fields = ["$total_mv", "$float_mv", "$roe", "$eps", "$net_profit"]
    return D.features(D.instruments("all"), fields, freq="day")


def _style_audit_source_frame() -> tuple[pd.DataFrame | None, str]:
    global _STYLE_AUDIT_FRAME_CACHE
    if _STYLE_AUDIT_FRAME_CACHE is not None:
        return _STYLE_AUDIT_FRAME_CACHE, f"qlib_provider:{QLIB_DATA_ROOT}"
    try:
        raw = _load_qlib_style_source()
    except Exception as exc:
        return None, f"qlib style source unreadable: {exc}"
    required = ["$total_mv", "$float_mv", "$roe", "$eps", "$net_profit"]
    missing = [column for column in required if column not in raw.columns]
    if missing:
        return None, f"style source missing columns: {missing}"

    frame = raw[required].copy()
    frame.columns = ["total_mv", "float_mv", "roe", "eps", "net_profit"]
    frame = frame.reset_index()
    if "datetime" not in frame.columns or "instrument" not in frame.columns:
        return None, "style source index missing datetime/instrument"
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    frame["instrument"] = frame["instrument"].map(_normalize_qlib_instrument)
    for column in ["total_mv", "float_mv", "roe", "eps", "net_profit"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["datetime", "instrument"])
    frame = frame[(frame["total_mv"] > 0) | (frame["float_mv"] > 0)].copy()
    if frame.empty:
        return None, "style source has no positive market-cap rows"

    frame = frame.sort_values(["instrument", "datetime"])
    for column in ["eps", "net_profit"]:
        previous = frame.groupby("instrument", sort=False)[column].shift(252)
        growth = (frame[column] / previous) - 1.0
        frame[f"{column}_growth_1y"] = growth.where(previous > 0)

    frame = frame.set_index(["datetime", "instrument"]).sort_index()
    by_date = frame.groupby(level=0, group_keys=False)
    frame["total_mv_pct"] = by_date["total_mv"].rank(pct=True)
    frame["float_mv_pct"] = by_date["float_mv"].rank(pct=True)
    frame["roe_pct"] = by_date["roe"].rank(pct=True)
    frame["eps_growth_pct"] = by_date["eps_growth_1y"].rank(pct=True)
    frame["net_profit_growth_pct"] = by_date["net_profit_growth_1y"].rank(pct=True)
    frame["is_small_cap"] = (frame["total_mv_pct"] <= 0.20) | (frame["float_mv_pct"] <= 0.20)
    frame["growth_score_pct"] = frame[["eps_growth_pct", "net_profit_growth_pct", "roe_pct"]].max(axis=1, skipna=True)
    frame["is_high_growth"] = frame["growth_score_pct"] >= 0.80
    frame["is_blue_chip"] = ((frame["total_mv_pct"] >= 0.80) | (frame["float_mv_pct"] >= 0.80)) & (frame["roe_pct"] >= 0.60)
    keep = [
        "total_mv",
        "float_mv",
        "roe",
        "eps_growth_1y",
        "net_profit_growth_1y",
        "total_mv_pct",
        "float_mv_pct",
        "roe_pct",
        "growth_score_pct",
        "is_small_cap",
        "is_high_growth",
        "is_blue_chip",
    ]
    _STYLE_AUDIT_FRAME_CACHE = frame[keep]
    return _STYLE_AUDIT_FRAME_CACHE, f"qlib_provider:{QLIB_DATA_ROOT}"


def _prediction_style_exposure(pred_obj: Any | None) -> dict[str, Any]:
    if pred_obj is None:
        return {"available": False, "status": "blocked", "reason": "pred.pkl missing or unreadable"}
    try:
        score = pred_obj.iloc[:, 0] if isinstance(pred_obj, pd.DataFrame) else pred_obj
        score = pd.to_numeric(score, errors="coerce").dropna()
    except Exception as exc:
        return {"available": False, "status": "review_required", "reason": f"prediction unreadable: {exc}"}
    if score.empty:
        return {"available": False, "status": "blocked", "reason": "prediction scores unavailable"}
    df = score.rename("score").reset_index()
    if "datetime" not in df.columns or "instrument" not in df.columns:
        return {"available": False, "status": "review_required", "reason": "prediction index missing datetime/instrument"}
    style_frame, source = _style_audit_source_frame()
    if style_frame is None or style_frame.empty:
        return {"available": False, "status": "review_required", "reason": source}
    prediction_latest = pd.to_datetime(df["datetime"], errors="coerce").max()
    style_latest = pd.to_datetime(style_frame.index.get_level_values("datetime"), errors="coerce").max()
    if pd.notna(prediction_latest) and (pd.isna(style_latest) or style_latest < prediction_latest):
        return {
            "available": False,
            "status": "review_required",
            "reason": "qlib_style_data_stale",
            "source": source,
            "prediction_latest_date": str(pd.Timestamp(prediction_latest).date()),
            "style_latest_date": str(pd.Timestamp(style_latest).date()) if pd.notna(style_latest) else None,
        }

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df["instrument"] = df["instrument"].astype(str).map(_normalize_qlib_instrument)
    ranked = df.dropna(subset=["datetime", "instrument"]).sort_values(
        ["datetime", "score", "instrument"],
        ascending=[True, False, True],
        kind="mergesort",
    )

    def enrich(topk: int) -> pd.DataFrame:
        selected = ranked.groupby("datetime", sort=True).head(topk).set_index(["datetime", "instrument"])
        return selected.join(style_frame, how="left").reset_index()

    def summarize(frame: pd.DataFrame, topk: int) -> dict[str, Any]:
        available = frame[frame["total_mv"].notna() | frame["float_mv"].notna()].copy()
        if available.empty:
            return {"available": False, "reason": f"no style rows matched top{topk} predictions"}
        by_day = available.groupby("datetime", sort=True)
        small_ratio = by_day["is_small_cap"].mean()
        growth_ratio = by_day["is_high_growth"].mean()
        blue_chip_ratio = by_day["is_blue_chip"].mean()
        latest_dt = available["datetime"].max()
        latest = (
            available.loc[available["datetime"] == latest_dt]
            .sort_values(["score", "instrument"], ascending=[False, True], kind="mergesort")
            .head(topk)
            .copy()
        )
        if "datetime" in latest.columns:
            latest["datetime"] = pd.to_datetime(latest["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
        latest_cols = [
            "datetime",
            "instrument",
            "score",
            "total_mv",
            "float_mv",
            "total_mv_pct",
            "float_mv_pct",
            "roe_pct",
            "growth_score_pct",
            "is_small_cap",
            "is_high_growth",
            "is_blue_chip",
        ]
        rows = [
            {key: _json_safe(value) for key, value in row.items()}
            for row in latest[[col for col in latest_cols if col in latest.columns]].to_dict(orient="records")
        ]
        return {
            "available": True,
            "topk": int(topk),
            "day_count": int(len(small_ratio)),
            "style_row_match_ratio": _safe_float(len(available) / len(frame)) if len(frame) else None,
            "avg_small_cap_ratio": _safe_float(small_ratio.mean()) if len(small_ratio) else None,
            "latest_small_cap_ratio": _safe_float(small_ratio.iloc[-1]) if len(small_ratio) else None,
            "p95_small_cap_ratio": _safe_float(small_ratio.quantile(0.95)) if len(small_ratio) else None,
            "avg_high_growth_ratio": _safe_float(growth_ratio.mean()) if len(growth_ratio) else None,
            "latest_high_growth_ratio": _safe_float(growth_ratio.iloc[-1]) if len(growth_ratio) else None,
            "p95_high_growth_ratio": _safe_float(growth_ratio.quantile(0.95)) if len(growth_ratio) else None,
            "avg_blue_chip_ratio": _safe_float(blue_chip_ratio.mean()) if len(blue_chip_ratio) else None,
            "latest_blue_chip_ratio": _safe_float(blue_chip_ratio.iloc[-1]) if len(blue_chip_ratio) else None,
            "p95_blue_chip_ratio": _safe_float(blue_chip_ratio.quantile(0.95)) if len(blue_chip_ratio) else None,
            "avg_total_mv_percentile": _safe_float(available["total_mv_pct"].mean()) if available["total_mv_pct"].notna().any() else None,
            "avg_float_mv_percentile": _safe_float(available["float_mv_pct"].mean()) if available["float_mv_pct"].notna().any() else None,
            "latest_rows": rows,
        }

    topk = summarize(enrich(int(DEFAULT_PORTFOLIO["topk"])), int(DEFAULT_PORTFOLIO["topk"]))
    top50 = summarize(enrich(50), 50)

    strategy_warnings: list[str] = []
    infra_warnings: list[str] = []
    topk_small = _safe_float(topk.get("avg_small_cap_ratio")) if topk.get("available") else None
    top50_small = _safe_float(top50.get("avg_small_cap_ratio")) if top50.get("available") else None
    topk_growth = _safe_float(topk.get("avg_high_growth_ratio")) if topk.get("available") else None
    top50_growth = _safe_float(top50.get("avg_high_growth_ratio")) if top50.get("available") else None
    topk_blue = _safe_float(topk.get("avg_blue_chip_ratio")) if topk.get("available") else None
    top50_blue = _safe_float(top50.get("avg_blue_chip_ratio")) if top50.get("available") else None
    match_ratio = _safe_float(top50.get("style_row_match_ratio")) if top50.get("available") else None

    if match_ratio is not None and match_ratio < 0.80:
        infra_warnings.append("style_row_match_ratio_below_80pct")
    if topk_small is not None and topk_small > 0.40:
        strategy_warnings.append("prediction_top20_small_cap_ratio_above_40pct")
    if top50_small is not None and top50_small > 0.35:
        strategy_warnings.append("prediction_top50_small_cap_ratio_above_35pct")
    if topk_growth is not None and topk_growth > 0.70:
        strategy_warnings.append("prediction_top20_high_growth_ratio_above_70pct")
    if top50_growth is not None and top50_growth > 0.60:
        strategy_warnings.append("prediction_top50_high_growth_ratio_above_60pct")
    if top50_blue is not None and top50_blue < 0.15:
        strategy_warnings.append("prediction_top50_blue_chip_ratio_below_15pct")

    warnings = strategy_warnings + infra_warnings
    return {
        "available": bool(topk.get("available") or top50.get("available")),
        "status": "review_required" if strategy_warnings else ("warning" if infra_warnings else "clean"),
        "source": source,
        "definitions": {
            "small_cap": "同一交易日 Qlib 全市场 $total_mv 或 $float_mv 分位 <= 20%。",
            "high_growth": "同一交易日 max(EPS 252交易日同比增速分位, 净利润252交易日同比增速分位, ROE分位) >= 80%。",
            "blue_chip": "同一交易日 $total_mv 或 $float_mv 分位 >= 80%，且 ROE 分位 >= 60%。",
            "prediction_scope": "按 pred.pkl 每日预测分数排序，分别审计正式 top20 与 top50。",
        },
        "top20_prediction": topk,
        "top50_prediction": top50,
        "risk_flags": {
            "top20_small_cap_ratio": topk_small,
            "top50_small_cap_ratio": top50_small,
            "top20_high_growth_ratio": topk_growth,
            "top50_high_growth_ratio": top50_growth,
            "top20_blue_chip_ratio": topk_blue,
            "top50_blue_chip_ratio": top50_blue,
            "style_row_match_ratio": match_ratio,
        },
        "warnings": warnings,
        "strategy_warnings": strategy_warnings,
        "infra_warnings": infra_warnings,
    }


def audit_seed_run(seed_run: dict[str, Any]) -> dict[str, Any]:
    model_run_id = str(seed_run.get("model_run_id") or "")
    run_dir = Path(str(seed_run.get("artifact_dir") or ""))
    manifest = _read_json(run_dir / "manifest.json")
    metrics = dict(seed_run.get("metrics") or _read_json(run_dir / "metrics.json") or {})
    artifact_check, ret_df, pred_obj = _artifact_check(run_dir, manifest)
    checks = {
        "artifact_integrity": artifact_check,
        "manifest_contract": _manifest_contract_check(manifest),
        "portfolio_artifacts": _portfolio_artifact_check(manifest),
        "daily_backtest_quality": _daily_checks(ret_df, metrics),
        "cost_and_concentration": _cost_concentration_checks(ret_df),
        "prediction_artifact": _prediction_checks(pred_obj),
        "tradability_exposure": _tradability_checks(pred_obj),
        "prediction_distribution": _style_exposure_checks(pred_obj),
        "model_style_exposure": _prediction_style_exposure(pred_obj),
    }
    hard_blocks: list[str] = []
    warnings: list[str] = []
    for name, check in checks.items():
        if check.get("status") == "blocked":
            hard_blocks.append(name)
        if check.get("status") in {"warning", "review_required"}:
            warnings.append(name)
    status = "blocked" if hard_blocks else ("review_required" if warnings else "clean")
    payload = {
        "status": status,
        "validation_rule_version": VALIDATION_RULE_VERSION,
        "model_system_version": MODEL_SYSTEM_VERSION,
        "model_run_id": model_run_id,
        "round_group_id": seed_run.get("round_group_id"),
        "seed": seed_run.get("seed"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hard_blocks": hard_blocks,
        "warnings": warnings,
        "checks": checks,
        "promotion_rule": (
            "Blocked by artifact/config/performance hard failures. "
            "Review-required checks expose cost/concentration, tradability, and score-distribution risks."
        ),
    }
    try:
        out_path = run_dir / "validation_audit.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        payload["artifact_path"] = str(out_path)
    except Exception:
        pass
    return payload


def audit_round_seed_runs(seed_runs: list[dict[str, Any]], *, state: Any | None = None) -> dict[str, dict[str, Any]]:
    audits: dict[str, dict[str, Any]] = {}
    for row in seed_runs:
        model_run_id = str(row.get("model_run_id") or "")
        if not model_run_id:
            continue
        audit = audit_seed_run(row)
        audits[model_run_id] = audit
        if state is not None:
            try:
                state.upsert_seed_run({**row, "validation": audit})
            except Exception:
                pass
    return audits


def audit_round_group_seed_runs(round_group_id: str, *, state: Any | None = None) -> dict[str, dict[str, Any]]:
    if state is None:
        from .state_store import ModelStateStore

        state = ModelStateStore()
    return audit_round_seed_runs(state.list_seed_runs(round_group_id=round_group_id), state=state)
