from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from domain.data_foundation.ops_common import data_job_guard
from domain.data_foundation.runtime_io import atomic_write_json, read_json
from domain.data_foundation.tushare_production import MIN_ITEMSIZE, _append_hdf
from domain.data_foundation.tushare_rebuild import (
    NAMECHANGE_FIELDS,
    STOCK_ST_FIELDS,
    STAGING_ROOT,
    _apply_status_fields,
    _fetch_stock_basic_statuses,
    _is_delist_name,
    _is_st_name,
    _normalize_namechange_frame,
    _normalize_stock_basic_frame,
    _proxy_mode,
)
from integrations.tushare.client import get_tushare_client
from storage.paths import CURRENT_PRODUCTION_DATASET_FILE, PRODUCTION_RAW_HDF5, PRODUCTION_RAW_METADATA


PACKAGE_PREFIX = "tushare-status-backfill"


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


def _default_package_id(source_package_id: str | None) -> str:
    suffix = f"-source-{source_package_id}" if source_package_id else ""
    return f"{PACKAGE_PREFIX}-{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"


def _read_json(path: Path) -> dict[str, Any]:
    return read_json(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _extract_trade_dates(source_hdf: Path) -> list[str]:
    dates: set[str] = set()
    with pd.HDFStore(source_hdf, mode="r") as store:
        nrows = int(store.get_storer("/daily").nrows or 0)
        for start in range(0, nrows, 500_000):
            chunk = store.select("/daily", start=start, stop=min(start + 500_000, nrows), columns=["kline_time"])
            stamps = pd.to_datetime(chunk["kline_time"], errors="coerce").dt.strftime("%Y%m%d")
            dates.update(str(value) for value in stamps.dropna().tolist())
    return sorted(dates)


def _is_rate_limit_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "频率超限" in message or "rate" in message or "limit" in message


def _fetch_stock_st_for_dates(pro, trade_dates: list[str], *, min_interval_seconds: float = 0.25, max_retries: int = 5) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    last_call_at = 0.0
    for trade_date in trade_dates:
        for attempt in range(max_retries + 1):
            elapsed = time.monotonic() - last_call_at
            if elapsed < min_interval_seconds:
                time.sleep(min_interval_seconds - elapsed)
            try:
                frame = pro.stock_st(trade_date=trade_date, fields=STOCK_ST_FIELDS)
                last_call_at = time.monotonic()
                break
            except Exception as exc:
                last_call_at = time.monotonic()
                if not _is_rate_limit_error(exc) or attempt >= max_retries:
                    raise
                time.sleep(min(60.0, 10.0 * (attempt + 1)))
        if frame is None:
            frame = pd.DataFrame(columns=STOCK_ST_FIELDS.split(","))
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=STOCK_ST_FIELDS.split(","))
    return pd.concat(frames, ignore_index=True)


def _namechange_candidate_codes(stock_basic_df: pd.DataFrame) -> list[str]:
    stock_basic = _normalize_stock_basic_frame(stock_basic_df)
    if stock_basic.empty:
        return []
    current_status = stock_basic["list_status"].astype("string").str.strip().str.upper()
    has_delist_date = stock_basic["delist_date"].astype("string").str.strip().notna()
    has_delist_date &= stock_basic["delist_date"].astype("string").str.strip().ne("")
    has_delist_date &= stock_basic["delist_date"].astype("string").str.strip().str.lower().ne("nan")
    mask = current_status.isin(["D", "P"]) | has_delist_date | _is_st_name(stock_basic["name"]) | _is_delist_name(stock_basic["name"])
    return sorted(stock_basic.loc[mask, "ts_code"].astype(str).dropna().unique().tolist())


def _fetch_stock_namechanges(
    pro,
    stock_basic_df: pd.DataFrame,
    *,
    min_interval_seconds: float = 0.25,
    max_retries: int = 5,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    last_call_at = 0.0
    for code in _namechange_candidate_codes(stock_basic_df):
        for attempt in range(max_retries + 1):
            elapsed = time.monotonic() - last_call_at
            if elapsed < min_interval_seconds:
                time.sleep(min_interval_seconds - elapsed)
            try:
                frame = pro.namechange(ts_code=code, fields=NAMECHANGE_FIELDS)
                last_call_at = time.monotonic()
                break
            except Exception as exc:
                last_call_at = time.monotonic()
                if not _is_rate_limit_error(exc) or attempt >= max_retries:
                    raise
                time.sleep(min(60.0, 10.0 * (attempt + 1)))
        if frame is None or frame.empty:
            continue
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=NAMECHANGE_FIELDS.split(","))
    return _normalize_namechange_frame(pd.concat(frames, ignore_index=True))


def _status_counts(frame: pd.DataFrame) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for column in ["list_status", "st_status"]:
        if column in frame.columns:
            result[column] = {str(k): int(v) for k, v in frame[column].astype(str).value_counts(dropna=False).sort_index().items()}
    return result


@data_job_guard("tushare_status_backfill")
def build_tushare_status_backfill(
    *,
    package_id: str | None = None,
    source_hdf: Path | str = PRODUCTION_RAW_HDF5,
    stock_basic_df: pd.DataFrame | None = None,
    stock_st_df: pd.DataFrame | None = None,
    namechange_df: pd.DataFrame | None = None,
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
    raw_root = package_root / "raw"
    final_output_hdf = raw_root / "stock_daily.h5"
    output_hdf = raw_root / f".stock_daily.h5.tmp-{os.getpid()}"
    manifest_path = package_root / "manifest.json"
    output_hdf.unlink(missing_ok=True)

    trade_dates: list[str] = []
    if fetch_live and (stock_basic_df is None or stock_st_df is None or namechange_df is None):
        if stock_st_df is None:
            trade_dates = _extract_trade_dates(source_hdf)
        with _proxy_mode(proxy_mode):
            pro = get_tushare_client(network_mode=proxy_mode)
            if stock_basic_df is None:
                stock_basic_df = _fetch_stock_basic_statuses(pro)
            if stock_st_df is None:
                stock_st_df = _fetch_stock_st_for_dates(pro, trade_dates)
            if namechange_df is None:
                namechange_df = _fetch_stock_namechanges(pro, stock_basic_df if stock_basic_df is not None else pd.DataFrame())

    stock_basic_df = _normalize_stock_basic_frame(stock_basic_df if stock_basic_df is not None else pd.DataFrame())
    stock_st_df = stock_st_df if stock_st_df is not None else pd.DataFrame()
    namechange_df = _normalize_namechange_frame(namechange_df)
    if not stock_basic_df.empty:
        (package_root / "raw" / "stock_basic").mkdir(parents=True, exist_ok=True)
        _atomic_write_parquet(stock_basic_df, package_root / "raw" / "stock_basic" / "all.parquet")
    if not stock_st_df.empty:
        (package_root / "raw" / "stock_st").mkdir(parents=True, exist_ok=True)
        _atomic_write_parquet(stock_st_df, package_root / "raw" / "stock_st" / "all.parquet")
    (package_root / "raw" / "namechange").mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(namechange_df, package_root / "raw" / "namechange" / "all.parquet")

    row_count = 0
    source_row_count = 0
    info_frame = None
    before_counts: dict[str, dict[str, int]] = {}
    after_counts: dict[str, dict[str, int]] = {}
    changed_counts = {"list_status": 0, "st_status": 0}
    with pd.HDFStore(source_hdf, mode="r") as store:
        nrows = int(store.get_storer("/daily").nrows or 0)
        source_row_count = nrows
        if "/info" in store:
            info_frame = store["/info"]
        for start in range(0, nrows, int(chunk_rows)):
            chunk = store.select("/daily", start=start, stop=min(start + int(chunk_rows), nrows))
            if chunk.empty:
                continue
            work = chunk.copy()
            if "trade_date" not in work.columns:
                work = work.reset_index()
            if "name" not in work.columns and "SECURITY_NAME" in work.columns:
                work["name"] = work["SECURITY_NAME"]
            before = work[[column for column in ["list_status", "st_status"] if column in work.columns]].copy()
            updated = _apply_status_fields(work, stock_basic_df=stock_basic_df, stock_st_df=stock_st_df, namechange_df=namechange_df)
            if "SECURITY_NAME" in updated.columns and "name" in updated.columns:
                updated["SECURITY_NAME"] = updated["name"]
            if "name" in updated.columns and "name" not in chunk.columns:
                updated = updated.drop(columns=["name"])
            for column in ["list_status", "st_status"]:
                if column in before.columns:
                    changed_counts[column] += int((before[column].astype(str).fillna("") != updated[column].astype(str).fillna("")).sum())
                else:
                    changed_counts[column] += int(updated[column].notna().sum())
            if "trade_date" in updated.columns:
                updated = updated.set_index("trade_date")
                updated.index.name = "trade_date"
            _append_hdf(output_hdf, "/daily", updated, append=output_hdf.exists(), min_itemsize={key: value for key, value in MIN_ITEMSIZE.items() if key in updated.columns})
            row_count += int(len(updated))
            for column, counts in _status_counts(updated).items():
                target = after_counts.setdefault(column, {})
                for key, value in counts.items():
                    target[key] = target.get(key, 0) + value
            for column, counts in _status_counts(work).items():
                target = before_counts.setdefault(column, {})
                for key, value in counts.items():
                    target[key] = target.get(key, 0) + value
    if info_frame is not None:
        info_frame.to_hdf(output_hdf, key="/info", mode="a", format="table")
    os.replace(output_hdf, final_output_hdf)
    output_hdf = final_output_hdf

    source_metadata_path = source_hdf.with_name("metadata.json")
    if not source_metadata_path.exists() and PRODUCTION_RAW_METADATA.exists():
        source_metadata_path = PRODUCTION_RAW_METADATA
    metadata = _read_json(source_metadata_path)
    metadata.update(
        {
            "package_id": package_id,
            "package_kind": "tushare_status_backfill",
            "source_package_id": source_package_id,
            "source_hdf": str(source_hdf),
            "status_backfill_package_id": package_id,
            "status_backfill_generated_at": _now(),
            "status_fields": {
                "list_status": ["L", "P", "D", "I"],
                "st_status": ["NORMAL", "ST", "DELIST"],
            },
        }
    )
    notes = list(metadata.get("notes") or [])
    notes.append("This metadata belongs to a staged status backfill package that only adds/refreshes list_status and st_status.")
    metadata["notes"] = notes
    raw_root.mkdir(parents=True, exist_ok=True)
    _write_json(raw_root / "metadata.json", metadata)

    report = {
        "status": "completed",
        "package_id": package_id,
        "package_root": str(package_root),
        "source_hdf": str(source_hdf),
        "output_hdf": str(output_hdf),
        "row_count": row_count,
        "source_row_count": source_row_count,
        "changed_counts": changed_counts,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "stock_basic_rows": int(len(stock_basic_df)),
        "stock_st_rows": int(len(stock_st_df)),
        "namechange_rows": int(len(namechange_df)),
        "namechange_code_count": int(namechange_df["ts_code"].nunique()) if not namechange_df.empty and "ts_code" in namechange_df.columns else 0,
        "trade_date_count": len(trade_dates),
        "created_at": _now(),
    }
    _write_json(package_root / "status_backfill_report.json", report)
    _write_json(manifest_path, {"source": "tushare", "package_kind": "status_backfill", **report})
    return report
