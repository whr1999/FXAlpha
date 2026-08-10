from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from domain.data_foundation.ops_common import data_job_guard
from domain.data_foundation.runtime_io import atomic_write_json, read_json
from domain.data_foundation.tushare_production import INDEX_NAME_MAP, MIN_ITEMSIZE, _append_hdf
from domain.data_foundation.tushare_rebuild import STAGING_ROOT, STK_LIMIT_FIELDS, _proxy_mode
from domain.data_foundation.tushare_status_backfill import _extract_trade_dates, _is_rate_limit_error
from integrations.tushare.client import get_tushare_client
from storage.paths import CURRENT_PRODUCTION_DATASET_FILE, PRODUCTION_RAW_HDF5, PRODUCTION_RAW_METADATA


PACKAGE_PREFIX = "tushare-limit-backfill"
CODE_ALIASES = {
    # Tushare stk_limit uses the old ticker before the 2019-12-16 code change,
    # while the normalized production HDF keeps the current ticker across history.
    "001914.SZ": [("000043.SZ", None, "20191213")],
}


def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    working_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    working_path.unlink(missing_ok=True)
    try:
        frame.to_parquet(working_path, index=False)
        os.replace(working_path, path)
    except Exception:
        working_path.unlink(missing_ok=True)
        raise


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    return read_json(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _default_package_id(source_package_id: str | None) -> str:
    suffix = f"-source-{source_package_id}" if source_package_id else ""
    return f"{PACKAGE_PREFIX}-{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"


def _normalize_stk_limit(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["code", "trade_date", "up_limit", "down_limit", "stk_limit_pre_close"])
    work = frame.copy()
    if "code" not in work.columns and "ts_code" in work.columns:
        work["code"] = work["ts_code"]
    if "stk_limit_pre_close" not in work.columns and "pre_close" in work.columns:
        work["stk_limit_pre_close"] = work["pre_close"]
    for column in ["code", "trade_date"]:
        if column not in work.columns:
            raise ValueError(f"stk_limit_missing_required_column:{column}")
    for column in ["up_limit", "down_limit", "stk_limit_pre_close"]:
        if column not in work.columns:
            work[column] = np.nan
        work[column] = pd.to_numeric(work[column], errors="coerce")
    work["code"] = work["code"].astype(str)
    work["trade_date"] = pd.to_datetime(work["trade_date"].astype(str), errors="coerce").dt.strftime("%Y%m%d")
    work = work[work["trade_date"].notna() & work["code"].notna()].copy()
    work = work.drop_duplicates(["code", "trade_date"], keep="last")
    return work[["code", "trade_date", "up_limit", "down_limit", "stk_limit_pre_close"]]


def _missing_pre_close_trade_dates(source_hdf: Path, *, chunk_rows: int = 250_000) -> list[str]:
    """Return only stock dates whose official stk_limit pre-close is absent."""
    missing_dates: set[str] = set()
    with pd.HDFStore(source_hdf, mode="r") as store:
        nrows = int(store.get_storer("/daily").nrows or 0)
        probe = store.select("/daily", start=0, stop=1)
        if "stk_limit_pre_close" not in probe.columns:
            return _extract_trade_dates(source_hdf)
        columns = [column for column in ["kline_time", "list_status", "code", "stk_limit_pre_close"] if column in probe.columns]
        for start in range(0, nrows, int(chunk_rows)):
            chunk = store.select("/daily", start=start, stop=min(start + int(chunk_rows), nrows), columns=columns)
            if chunk.empty:
                continue
            if "list_status" in chunk.columns:
                stock = ~chunk["list_status"].astype(str).eq("I")
            else:
                stock = ~chunk["code"].astype(str).isin(set(INDEX_NAME_MAP))
            missing = stock & pd.to_numeric(chunk["stk_limit_pre_close"], errors="coerce").isna()
            if missing.any():
                missing_dates.update(
                    pd.to_datetime(chunk.loc[missing, "kline_time"], errors="coerce").dt.strftime("%Y%m%d").dropna().tolist()
                )
    return sorted(missing_dates)


def _alias_limit_frame(limit_df: pd.DataFrame) -> pd.DataFrame:
    if limit_df.empty:
        return limit_df
    frames = [limit_df]
    for canonical_code, aliases in CODE_ALIASES.items():
        for alias_code, start_date, end_date in aliases:
            alias_rows = limit_df[limit_df["code"].eq(alias_code)].copy()
            if start_date:
                alias_rows = alias_rows[alias_rows["trade_date"].ge(str(start_date))]
            if end_date:
                alias_rows = alias_rows[alias_rows["trade_date"].le(str(end_date))]
            if alias_rows.empty:
                continue
            alias_rows["source_code"] = alias_rows["code"]
            alias_rows["code"] = canonical_code
            frames.append(alias_rows)
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(["code", "trade_date"], keep="last")
    return combined


def _fetch_stk_limit_for_dates(
    pro,
    trade_dates: list[str],
    *,
    min_interval_seconds: float = 0.25,
    max_retries: int = 5,
    package_root: Path | None = None,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    last_call_at = 0.0
    raw_dir = package_root / "raw" / "stk_limit" if package_root is not None else None
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
    for trade_date in trade_dates:
        for attempt in range(max_retries + 1):
            elapsed = time.monotonic() - last_call_at
            if elapsed < min_interval_seconds:
                time.sleep(min_interval_seconds - elapsed)
            try:
                frame = pro.stk_limit(trade_date=trade_date, fields=STK_LIMIT_FIELDS)
                last_call_at = time.monotonic()
                break
            except Exception as exc:
                last_call_at = time.monotonic()
                if not _is_rate_limit_error(exc) or attempt >= max_retries:
                    raise
                time.sleep(min(60.0, 10.0 * (attempt + 1)))
        if frame is None:
            frame = pd.DataFrame(columns=STK_LIMIT_FIELDS.split(","))
        if raw_dir is not None:
            _atomic_write_parquet(frame, raw_dir / f"{trade_date}.parquet")
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=STK_LIMIT_FIELDS.split(","))
    return pd.concat(frames, ignore_index=True)


def _limit_coverage_report(source_rows: int, stock_rows: pd.DataFrame, output: pd.DataFrame, limit_df: pd.DataFrame) -> dict[str, Any]:
    stock_mask = ~output["code"].isin(set(INDEX_NAME_MAP))
    stock_output = output.loc[stock_mask].copy()
    official_mask = stock_output["up_limit"].notna() & stock_output["down_limit"].notna()
    if "LIST_DATE" in stock_output.columns:
        list_date = pd.to_datetime(stock_output["LIST_DATE"], format="%Y%m%d", errors="coerce")
    else:
        list_date = pd.Series(pd.NaT, index=stock_output.index)
    kline_time = pd.to_datetime(stock_output["kline_time"], errors="coerce")
    structural_no_limit = (~official_mask) & list_date.notna() & kline_time.notna() & kline_time.dt.normalize().eq(list_date.dt.normalize())
    missing = stock_output.loc[(~official_mask) & (~structural_no_limit), ["code", "kline_time"]].copy()
    missing["trade_date"] = pd.to_datetime(missing["kline_time"], errors="coerce").dt.strftime("%Y%m%d")
    missing_by_date = missing.groupby("trade_date")["code"].nunique().sort_values(ascending=False) if not missing.empty else pd.Series(dtype="int64")
    return {
        "passed": int(((~official_mask) & (~structural_no_limit)).sum()) == 0,
        "source_row_count": int(source_rows),
        "output_row_count": int(len(output)),
        "stock_row_count": int(len(stock_output)),
        "stk_limit_row_count": int(len(limit_df)),
        "official_limit_row_count": int(official_mask.sum()),
        "structural_no_limit_row_count": int(structural_no_limit.sum()),
        "missing_limit_row_count": int(((~official_mask) & (~structural_no_limit)).sum()),
        "coverage_ratio": float((official_mask | structural_no_limit).mean()) if len(stock_output) else 1.0,
        "missing_date_count": int(missing["trade_date"].nunique()) if not missing.empty else 0,
        "missing_code_count": int(missing["code"].nunique()) if not missing.empty else 0,
        "missing_date_samples": [f"{idx}:{int(value)}" for idx, value in missing_by_date.head(10).items()],
        "missing_code_samples": sorted(missing["code"].astype(str).unique().tolist())[:10] if not missing.empty else [],
        "input_stock_row_count": int(len(stock_rows)),
    }


@data_job_guard("tushare_limit_backfill")
def build_tushare_limit_backfill(
    *,
    package_id: str | None = None,
    source_hdf: Path | str = PRODUCTION_RAW_HDF5,
    stk_limit_df: pd.DataFrame | None = None,
    fetch_live: bool = True,
    proxy_mode: str = "direct",
    chunk_rows: int = 250_000,
) -> dict[str, Any]:
    source_hdf = Path(source_hdf).expanduser()
    if not source_hdf.exists():
        raise FileNotFoundError(f"source_hdf_missing:{source_hdf}")

    current = _read_json(CURRENT_PRODUCTION_DATASET_FILE)
    source_package_id = current.get("production_package_id")
    package_id = package_id or _default_package_id(source_package_id)
    package_root = STAGING_ROOT / package_id
    final_output_hdf = package_root / "raw" / "stock_daily.h5"
    output_hdf = final_output_hdf.with_name(f".stock_daily.h5.tmp-{os.getpid()}")
    manifest_path = package_root / "manifest.json"
    output_hdf.unlink(missing_ok=True)

    trade_dates = _extract_trade_dates(source_hdf)
    fetch_trade_dates: list[str] = []
    if stk_limit_df is None and fetch_live:
        fetch_trade_dates = _missing_pre_close_trade_dates(source_hdf, chunk_rows=chunk_rows)
        with _proxy_mode(proxy_mode):
            pro = get_tushare_client(network_mode=proxy_mode)
            stk_limit_df = _fetch_stk_limit_for_dates(pro, fetch_trade_dates, package_root=package_root)
    limit_df = _normalize_stk_limit(stk_limit_df)
    limit_df = _alias_limit_frame(limit_df)
    limit_index = limit_df.set_index(["code", "trade_date"]) if not limit_df.empty else pd.DataFrame()

    source_row_count = 0
    output_row_count = 0
    changed_counts = {"up_limit": 0, "down_limit": 0}
    pre_close_changed_count = 0
    stock_rows_for_report: list[pd.DataFrame] = []
    output_for_report: list[pd.DataFrame] = []
    info_frame = None
    with pd.HDFStore(source_hdf, mode="r") as store:
        source_row_count = int(store.get_storer("/daily").nrows or 0)
        if "/info" in store:
            info_frame = store["/info"]
        for start in range(0, source_row_count, int(chunk_rows)):
            chunk = store.select("/daily", start=start, stop=min(start + int(chunk_rows), source_row_count))
            if chunk.empty:
                continue
            work = chunk.copy()
            if "trade_date" not in work.columns:
                work = work.reset_index()
            work["trade_date_key"] = pd.to_datetime(work["kline_time"], errors="coerce").dt.strftime("%Y%m%d")
            join_index = pd.MultiIndex.from_frame(work[["code", "trade_date_key"]].rename(columns={"trade_date_key": "trade_date"}))
            if limit_index.empty:
                matched = pd.DataFrame(index=work.index, columns=["up_limit", "down_limit", "stk_limit_pre_close"])
            else:
                matched = limit_index.reindex(join_index)[["up_limit", "down_limit", "stk_limit_pre_close"]].reset_index(drop=True)
                matched.index = work.index
            for column in ["up_limit", "down_limit"]:
                before = pd.to_numeric(work[column], errors="coerce") if column in work.columns else pd.Series(np.nan, index=work.index)
                after = pd.to_numeric(matched[column], errors="coerce").combine_first(before)
                changed = ~(np.isclose(before.astype(float), after.astype(float), rtol=1e-10, atol=1e-10) | (before.isna() & after.isna()))
                changed_counts[column] += int(changed.sum())
                work[column] = after
            if "LIST_DATE" in work.columns:
                list_date = pd.to_datetime(work["LIST_DATE"], format="%Y%m%d", errors="coerce")
            else:
                list_date = pd.Series(pd.NaT, index=work.index)
            kline_time = pd.to_datetime(work["kline_time"], errors="coerce")
            structural_no_limit = (
                work["up_limit"].isna()
                & work["down_limit"].isna()
                & list_date.notna()
                & kline_time.notna()
                & kline_time.dt.normalize().eq(list_date.dt.normalize())
            )
            work["limit_source_kind"] = np.where(
                work["up_limit"].notna() & work["down_limit"].notna(),
                "official",
                np.where(structural_no_limit, "structural_no_limit", "missing"),
            )
            existing_pre_close = (
                pd.to_numeric(work["stk_limit_pre_close"], errors="coerce")
                if "stk_limit_pre_close" in work.columns
                else pd.Series(np.nan, index=work.index, dtype="float64")
            )
            work["stk_limit_pre_close"] = pd.to_numeric(matched["stk_limit_pre_close"], errors="coerce").combine_first(existing_pre_close)
            official_pre_close = work["stk_limit_pre_close"]
            if "pre_close" in work.columns:
                prior_pre_close = pd.to_numeric(work["pre_close"], errors="coerce")
                changed = official_pre_close.notna() & ~(
                    np.isclose(prior_pre_close.astype(float), official_pre_close.astype(float), rtol=1e-10, atol=1e-10)
                    | (prior_pre_close.isna() & official_pre_close.isna())
                )
                pre_close_changed_count += int(changed.sum())
                work.loc[official_pre_close.notna(), "pre_close"] = official_pre_close.loc[official_pre_close.notna()]
                if "close" in work.columns and "pct_chg" in work.columns:
                    denominator = pd.to_numeric(work["pre_close"], errors="coerce").replace(0, np.nan)
                    work["pct_chg"] = (pd.to_numeric(work["close"], errors="coerce") - denominator) / denominator * 100.0
                if {"high", "low", "amp"}.issubset(work.columns):
                    denominator = pd.to_numeric(work["pre_close"], errors="coerce").replace(0, np.nan)
                    work["amp"] = (
                        pd.to_numeric(work["high"], errors="coerce") - pd.to_numeric(work["low"], errors="coerce")
                    ) / denominator * 100.0
            report_cols = [
                column
                for column in ["code", "kline_time", "LIST_DATE", "pre_close", "stk_limit_pre_close", "up_limit", "down_limit", "limit_source_kind"]
                if column in work.columns
            ]
            output_for_report.append(work[report_cols].copy())
            stock_rows_for_report.append(work.loc[~work["code"].isin(set(INDEX_NAME_MAP)), ["code", "kline_time"]].copy())
            work = work.drop(columns=["trade_date_key"], errors="ignore")
            if "trade_date" in work.columns:
                work = work.set_index("trade_date")
                work.index.name = "trade_date"
            _append_hdf(output_hdf, "/daily", work, append=output_hdf.exists(), min_itemsize={key: value for key, value in MIN_ITEMSIZE.items() if key in work.columns})
            output_row_count += int(len(work))
    if info_frame is not None:
        info_frame.to_hdf(output_hdf, key="/info", mode="a", format="table")
    os.replace(output_hdf, final_output_hdf)
    output_hdf = final_output_hdf

    source_metadata_path = source_hdf.with_name("metadata.json")
    if not source_metadata_path.exists() and PRODUCTION_RAW_METADATA.exists():
        source_metadata_path = PRODUCTION_RAW_METADATA
    metadata = _read_json(source_metadata_path)
    notes = list(metadata.get("notes") or [])
    notes.append("This metadata belongs to a staged Tushare stk_limit backfill package. It does not promote production data.")
    metadata.update(
        {
            "package_id": package_id,
            "package_kind": "tushare_limit_backfill",
            "source_package_id": source_package_id,
            "source_hdf": str(source_hdf),
            "limit_backfill_package_id": package_id,
            "limit_backfill_generated_at": _now(),
            "notes": notes,
        }
    )
    _write_json(package_root / "raw" / "metadata.json", metadata)

    output_sample = pd.concat(output_for_report, ignore_index=True) if output_for_report else pd.DataFrame(columns=["code", "kline_time", "up_limit", "down_limit"])
    stock_sample = pd.concat(stock_rows_for_report, ignore_index=True) if stock_rows_for_report else pd.DataFrame(columns=["code", "kline_time"])
    coverage = _limit_coverage_report(source_row_count, stock_sample, output_sample, limit_df)
    report = {
        "status": "completed",
        "package_id": package_id,
        "package_root": str(package_root),
        "source_hdf": str(source_hdf),
        "output_hdf": str(output_hdf),
        "row_count": output_row_count,
        "source_row_count": source_row_count,
        "trade_date_count": len(trade_dates),
        "fetch_trade_date_count": len(fetch_trade_dates),
        "fetch_trade_dates": fetch_trade_dates,
        "changed_counts": changed_counts,
        "pre_close_changed_count": pre_close_changed_count,
        "coverage": coverage,
        "created_at": _now(),
    }
    _write_json(package_root / "limit_backfill_report.json", report)
    _write_json(manifest_path, {"source": "tushare", "package_kind": "limit_backfill", **report})
    return report
