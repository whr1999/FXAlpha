"""Compute QuantGPT factor values into FXalpha factor parquet artifacts."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

from domain.runtime_memory import release_process_memory
from storage.paths import (
    FACTOR_ADOPTED_VALUES_FILE,
    FACTOR_PARQUET_DIR,
    QLIB_DATA_ROOT,
    QUANTGPT_ADOPTED_VALUES_FILE,
    QUANTGPT_CODE_ROOT,
    get_live_factor_value_default_end_date,
    get_live_factor_value_default_start_date,
)


logger = logging.getLogger(__name__)

DEFAULT_WARMUP_DAYS = 400
DEFAULT_MIN_DAILY_FACTOR_VALUES = 300
NON_ST_FILTER_COLUMNS = {"list_status", "st_status"}
FACTOR_BASE_COMPUTE_SEMANTICS_VERSION = "fxalpha_factor_eval_v4_static_non_st_20260601_full_ts"
SEMANTIC_MISSING_POLICY_VERSION = "factor_expression_semantic_missing_v2_dividend_zero"
FACTOR_COMPUTE_SEMANTICS_VERSION = (
    f"{FACTOR_BASE_COMPUTE_SEMANTICS_VERSION}__{SEMANTIC_MISSING_POLICY_VERSION}"
)


def _ensure_quantgpt_import_path() -> None:
    if str(QUANTGPT_CODE_ROOT) not in sys.path:
        sys.path.insert(0, str(QUANTGPT_CODE_ROOT))


def _qlib_to_bs(qcode: str) -> str:
    qcode = qcode.strip()
    if qcode.endswith("sh"):
        return f"sh.{qcode[:-2]}"
    if qcode.endswith("sz"):
        return f"sz.{qcode[:-2]}"
    return qcode


def _bs_to_qlib(bs: str) -> str:
    parts = bs.split(".")
    if len(parts) == 2:
        return f"{parts[1]}{parts[0]}"
    return bs


def _qlib_to_bs_instrument(instrument: str) -> str:
    instrument = instrument.strip()
    if instrument.endswith("sh"):
        return f"sh.{instrument[:-2]}"
    if instrument.endswith("sz"):
        return f"sz.{instrument[:-2]}"
    return instrument


def _resolve_date_bounds(
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, str]:
    """Normalize optional date bounds for factor computation."""
    resolved_start = start_date or get_live_factor_value_default_start_date()
    resolved_end = end_date or get_live_factor_value_default_end_date()
    return resolved_start, resolved_end


def _warmup_start_date(start_date: str, warmup_days: int = DEFAULT_WARMUP_DAYS) -> str:
    """Load earlier rows so ts_* expressions are valid at the requested start date."""
    return (pd.Timestamp(start_date) - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")


def _required_market_columns(expressions: list[str]) -> set[str]:
    """Return the minimal QuantGPT cache columns needed for expressions."""
    _ensure_quantgpt_import_path()
    from quantgpt.data_schema import normalize_field_name
    from quantgpt.expression_parser import extract_components

    columns: set[str] = {"trade_date", "stock_code"}
    for expression in expressions:
        components = extract_components(expression)
        fields = {normalize_field_name(field) for field in components.get("fields", set())}
        operators = set(components.get("operators", set()))
        for field in fields:
            if field.startswith("adv") and field[3:].isdigit():
                columns.add("volume")
                continue
            if field == "vwap":
                columns.add("vwap")
                continue
            if field == "returns":
                columns.add("close")
                continue
            if field == "cap":
                columns.add("total_mv")
                continue
            if field in {"day", "weekday", "month"}:
                columns.add("trade_date")
                continue
            columns.add(field)
        if "atr" in operators:
            columns.update({"high", "low", "close"})
    return columns


def _trim_factor_output(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    if df.empty:
        return df
    dates = df.index.get_level_values("datetime")
    return df[
        (dates >= pd.Timestamp(start_date))
        & (dates <= pd.Timestamp(end_date))
    ].copy()


def expected_trading_dates(start_date: str, end_date: str) -> list[str]:
    """Return expected Qlib trading dates for a factor value window."""
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    calendar_path = QLIB_DATA_ROOT / "calendars" / "day.txt"
    if calendar_path.exists():
        dates: list[str] = []
        for line in calendar_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            day = pd.Timestamp(text).normalize()
            if start <= day <= end:
                dates.append(str(day.date()))
        return dates
    return [str(day.date()) for day in pd.bdate_range(start, end)]


def audit_factor_value_coverage(
    factor_df: pd.DataFrame,
    start_date: str,
    end_date: str,
    *,
    min_daily_valid: int = DEFAULT_MIN_DAILY_FACTOR_VALUES,
) -> dict:
    expected = expected_trading_dates(start_date, end_date)
    if factor_df.empty:
        return {
            "passed": False,
            "reason": "no_factor_values",
            "start_date": start_date,
            "end_date": end_date,
            "expected_days": len(expected),
            "valid_days": 0,
            "missing_dates": expected,
            "missing_count": len(expected),
            "low_daily_valid": [],
            "min_daily_valid_threshold": min_daily_valid,
            "min_daily_valid": 0,
        }

    if not isinstance(factor_df.index, pd.MultiIndex) or "datetime" not in factor_df.index.names:
        return {
            "passed": False,
            "reason": "factor_index_missing_datetime",
            "start_date": start_date,
            "end_date": end_date,
            "expected_days": len(expected),
            "valid_days": 0,
            "missing_dates": expected,
            "missing_count": len(expected),
            "low_daily_valid": [],
            "min_daily_valid_threshold": min_daily_valid,
            "min_daily_valid": 0,
        }

    dates = pd.to_datetime(factor_df.index.get_level_values("datetime")).normalize()
    daily_valid = dates.value_counts().sort_index()
    daily_valid.index = [str(day.date()) for day in daily_valid.index]
    daily_counts = {str(day): int(count) for day, count in daily_valid.items()}
    missing_dates = [day for day in expected if day not in daily_counts]
    low_daily_valid = [
        {"date": day, "valid": count}
        for day, count in daily_counts.items()
        if day in expected and count < min_daily_valid
    ]
    observed_counts = [daily_counts[day] for day in expected if day in daily_counts]
    min_observed = min(observed_counts) if observed_counts else 0
    passed = not missing_dates and not low_daily_valid
    reason = ""
    if missing_dates:
        reason = "missing_trading_days"
    elif low_daily_valid:
        reason = "low_daily_valid_count"

    return {
        "passed": passed,
        "reason": reason,
        "start_date": start_date,
        "end_date": end_date,
        "expected_days": len(expected),
        "valid_days": len(observed_counts),
        "missing_dates": missing_dates,
        "missing_count": len(missing_dates),
        "low_daily_valid": low_daily_valid[:50],
        "low_daily_valid_count": len(low_daily_valid),
        "min_daily_valid_threshold": min_daily_valid,
        "min_daily_valid": int(min_observed),
        "median_daily_valid": float(pd.Series(observed_counts).median()) if observed_counts else 0.0,
        "max_daily_valid": int(max(observed_counts)) if observed_counts else 0,
    }


def _load_market_data(
    start_date: str | None = None,
    end_date: str | None = None,
    max_stocks: int = 0,
    required_columns: set[str] | None = None,
    *,
    filter_non_st: bool = False,
) -> pd.DataFrame:
    """Load QuantGPT cache into one combined DataFrame."""
    start_date, end_date = _resolve_date_bounds(start_date, end_date)
    _ensure_quantgpt_import_path()
    from quantgpt.market_data import MarketDataFetcher, filter_non_st_market_data

    instrument_file = QLIB_DATA_ROOT / "instruments" / "all.txt"
    if not instrument_file.exists():
        raise FileNotFoundError(f"Missing qlib instruments file: {instrument_file}")

    instruments: list[str] = []
    with instrument_file.open("r", encoding="utf-8") as fh:
        for line in fh:
            code, start, end = line.strip().split("\t")
            if end >= start_date and start <= end_date:
                instruments.append(code)

    if max_stocks > 0:
        instruments = instruments[:max_stocks]

    logger.info("Loading %d stocks into combined DataFrame", len(instruments))
    market_fetcher = MarketDataFetcher()

    frames: list[pd.DataFrame] = []
    failed = 0
    for idx, qcode in enumerate(instruments):
        if idx and idx % 1000 == 0:
            logger.info("Loaded %d/%d instruments", idx, len(instruments))
        bs_code = _qlib_to_bs(qcode)
        df = market_fetcher._load_cache(bs_code)
        if df is None or len(df) < 20:
            failed += 1
            continue
        df = df.copy()
        df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)]
        if filter_non_st:
            df = filter_non_st_market_data(df)
        if len(df) < 5:
            failed += 1
            continue
        if required_columns:
            keep = [col for col in df.columns if col in required_columns]
            missing = sorted(required_columns - set(keep))
            if missing:
                logger.debug("Market cache %s missing optional requested columns: %s", bs_code, missing)
            df = df.loc[:, keep]
        frames.append(df)

    if not frames:
        logger.warning("No market data loaded for factor computation")
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    logger.info(
        "Combined market data rows=%d stocks=%d excluded=%d",
        len(combined),
        combined["stock_code"].nunique(),
        failed,
    )
    return combined


def _compute_factor_from_market_df(
    market_df: pd.DataFrame,
    expression: str,
    *,
    universe: str = "tradable_non_st",
) -> pd.DataFrame:
    _ensure_quantgpt_import_path()
    from quantgpt.factor_evaluator import evaluate_factor_frame

    logger.info("Computing factor: %s", expression[:80])
    try:
        result_df = evaluate_factor_frame(
            market_df,
            expression,
            universe=universe,
            mode="local",
        )
    except Exception as exc:
        logger.error("Factor computation failed: %s", exc)
        return pd.DataFrame()

    column_name = expression[:40]
    out = result_df.rename(columns={"factor_value": "_factor"}).copy()
    out["instrument"] = out["stock_code"].map(_bs_to_qlib)
    out = out.rename(columns={"trade_date": "datetime", "_factor": column_name})
    out["datetime"] = pd.to_datetime(out["datetime"])
    out = out[["datetime", "instrument", column_name]].dropna(subset=[column_name])
    out = out.set_index(["datetime", "instrument"]).sort_index()
    out[column_name] = out[column_name].astype("float32")
    return out


def non_st_output_index(market_df: pd.DataFrame) -> pd.MultiIndex:
    """Build a PIT non-ST index for explicit diagnostics only.

    Model labels and prediction features must use the adopted factor-value
    static universe and must not call this helper.
    """
    _ensure_quantgpt_import_path()
    from quantgpt.market_data import filter_non_st_market_data

    if market_df.empty:
        return pd.MultiIndex.from_arrays([[], []], names=["datetime", "instrument"])
    filtered = filter_non_st_market_data(market_df)
    if filtered is None or filtered.empty:
        return pd.MultiIndex.from_arrays([[], []], names=["datetime", "instrument"])
    frame = filtered[["trade_date", "stock_code"]].copy()
    frame["datetime"] = pd.to_datetime(frame["trade_date"])
    frame["instrument"] = frame["stock_code"].map(_bs_to_qlib)
    return pd.MultiIndex.from_frame(frame[["datetime", "instrument"]])


def filter_output_to_non_st_rows(output_df: pd.DataFrame, market_df: pd.DataFrame) -> pd.DataFrame:
    """Apply a PIT non-ST row mask to an explicit diagnostic output only.

    This is not part of the model feature, label, or prediction path.
    """
    if output_df.empty:
        return output_df
    keep_index = non_st_output_index(market_df)
    return output_df[output_df.index.isin(keep_index)].copy()


def _build_adopted_value_frame(expression: str, factor_df: pd.DataFrame) -> pd.DataFrame:
    if factor_df.empty:
        return pd.DataFrame()
    value_col = factor_df.columns[0]
    adopted = factor_df.reset_index().rename(columns={"datetime": "trade_date"})
    adopted["stock_code"] = adopted["instrument"].map(_qlib_to_bs_instrument)
    adopted["trade_date"] = pd.to_datetime(adopted["trade_date"]).dt.normalize()
    adopted = adopted[["stock_code", "trade_date", value_col]].dropna(subset=[value_col])
    adopted = adopted.rename(columns={value_col: expression})
    return adopted.set_index(["stock_code", "trade_date"]).sort_index()


def _merge_adopted_values(path: Path, expression: str, adopted_df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = pd.DataFrame()
    if path.exists():
        try:
            existing = pd.read_parquet(path)
        except Exception as exc:
            logger.warning("Failed to read adopted-value store %s: %s", path, exc)
            existing = pd.DataFrame()

    if not existing.empty and not isinstance(existing.index, pd.MultiIndex):
        if {"stock_code", "trade_date"} <= set(existing.columns):
            existing = existing.set_index(["stock_code", "trade_date"]).sort_index()

    combined = existing.drop(columns=[expression], errors="ignore") if not existing.empty else pd.DataFrame()
    combined = combined.join(adopted_df, how="outer") if not combined.empty else adopted_df
    combined = combined.sort_index()
    combined.to_parquet(path, engine="pyarrow")
    del existing
    del combined
    release_process_memory("adopted_values_merge_completed")


def sync_adopted_factor_values(expression: str, factor_df: pd.DataFrame) -> list[str]:
    adopted_df = _build_adopted_value_frame(expression, factor_df)
    if adopted_df.empty:
        return []

    written: list[str] = []
    for path in dict.fromkeys((FACTOR_ADOPTED_VALUES_FILE, QUANTGPT_ADOPTED_VALUES_FILE)):
        try:
            _merge_adopted_values(path, expression, adopted_df)
            written.append(str(path))
        except Exception as exc:
            logger.warning("Failed to sync adopted values to %s: %s", path, exc)
    return written


def compute_factor(
    expression: str,
    start_date: str | None = None,
    end_date: str | None = None,
    max_stocks: int = 0,
    *,
    filter_non_st: bool = True,
) -> pd.DataFrame:
    start_date, end_date = _resolve_date_bounds(start_date, end_date)
    load_start_date = _warmup_start_date(start_date)
    required_columns = _required_market_columns([expression])
    if filter_non_st:
        required_columns = set(required_columns) | NON_ST_FILTER_COLUMNS
    market_df = _load_market_data(
        start_date=load_start_date,
        end_date=end_date,
        max_stocks=max_stocks,
        required_columns=required_columns,
        filter_non_st=False,
    )
    if market_df.empty:
        return pd.DataFrame()
    if filter_non_st:
        out = _compute_factor_from_market_df(market_df, expression)
    else:
        out = _compute_factor_from_market_df(market_df, expression, universe="all_market")
    out = _trim_factor_output(out, start_date, end_date)
    logger.info(
        "Factor values rows=%d instruments=%d",
        len(out),
        out.index.get_level_values("instrument").nunique() if not out.empty else 0,
    )
    return out


def compute_and_save(
    expression: str,
    factor_name: str,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    filter_non_st: bool = True,
    sync_adopted_values: bool = False,
) -> str | None:
    start_date, end_date = _resolve_date_bounds(start_date, end_date)
    df = compute_factor(expression, start_date, end_date, filter_non_st=filter_non_st)
    return save_factor_frame(expression, factor_name, df, sync_adopted_values=sync_adopted_values)


def save_factor_frame(
    expression: str,
    factor_name: str,
    df: pd.DataFrame,
    output_dir: str | Path | None = None,
    *,
    sync_adopted_values: bool = False,
) -> str | None:
    if df.empty:
        logger.warning("No values for %s", factor_name)
        return None

    safe_name = "".join(ch if ch.isalnum() else "_" for ch in factor_name)[:40]
    adopted_paths: list[str] = []
    if sync_adopted_values:
        adopted_paths = sync_adopted_factor_values(expression, df)
    to_save = df.copy()
    to_save.columns = pd.MultiIndex.from_product([["feature"], [safe_name]])

    output_dir = Path(output_dir or FACTOR_PARQUET_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = output_dir / f"factor_{safe_name}.parquet"
    to_save.to_parquet(str(parquet_path), engine="pyarrow")
    if adopted_paths:
        logger.info("Synced adopted factor values for %s to %s", safe_name, adopted_paths)
    logger.info("Saved %s to %s", safe_name, parquet_path)
    return str(parquet_path)


def compute_and_save_many(
    specs: list[tuple[str, str]],
    start_date: str | None = None,
    end_date: str | None = None,
    output_dir: str | Path | None = None,
    *,
    filter_non_st: bool = True,
) -> dict[str, str]:
    start_date, end_date = _resolve_date_bounds(start_date, end_date)
    output_dir = Path(output_dir or FACTOR_PARQUET_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    load_start_date = _warmup_start_date(start_date)
    required_columns = _required_market_columns([expression for expression, _ in specs])
    if filter_non_st:
        required_columns = set(required_columns) | NON_ST_FILTER_COLUMNS
    market_df = _load_market_data(
        start_date=load_start_date,
        end_date=end_date,
        required_columns=required_columns,
        filter_non_st=False,
    )
    if market_df.empty:
        return {}

    saved: dict[str, str] = {}
    for expression, factor_name in specs:
        if filter_non_st:
            df = _compute_factor_from_market_df(market_df, expression)
        else:
            df = _compute_factor_from_market_df(market_df, expression, universe="all_market")
        df = _trim_factor_output(df, start_date, end_date)
        if df.empty:
            logger.warning("No values for %s", factor_name)
            continue
        safe_name = "".join(ch if ch.isalnum() else "_" for ch in factor_name)[:80]
        adopted_paths = sync_adopted_factor_values(expression, df)
        df.columns = pd.MultiIndex.from_product([["feature"], [safe_name]])
        parquet_path = output_dir / f"factor_{safe_name}.parquet"
        df.to_parquet(str(parquet_path), engine="pyarrow")
        saved[factor_name] = str(parquet_path)
        if adopted_paths:
            logger.info("Synced adopted factor values for %s to %s", safe_name, adopted_paths)
        logger.info("Saved %s to %s", safe_name, parquet_path)
    return saved


def batch_compute_pending() -> int:
    from storage.factor_registry import FactorRegistry

    registry = FactorRegistry()
    pending = [f for f in registry.list_all(limit=1000)[0] if f["status"] == "pending"]
    logger.info("Pending factors to compute: %d", len(pending))

    computed = 0
    for factor in pending:
        factor_id = factor["factor_id"]
        expression = factor.get("expression", factor.get("name", factor_id))
        factor_name = f"QGF_{factor_id.split('_')[-1][:6]}"
        try:
            parquet_path = compute_and_save(expression, factor_name)
        except Exception as exc:
            logger.error("Compute failed for %s: %s", factor_id, exc)
            continue
        if parquet_path:
            registry.update_status(factor_id, "active")
            registry.update_meta(
                factor_id,
                {"data_path": parquet_path, "data_column": factor_name},
            )
            computed += 1
    return computed


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "batch":
        batch_compute_pending()
    elif len(sys.argv) > 1:
        expr = sys.argv[1]
        name = sys.argv[2] if len(sys.argv) > 2 else "test"
        compute_and_save(expr, name)
    else:
        batch_compute_pending()
