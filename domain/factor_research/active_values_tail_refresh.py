from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from domain.factor_research.active_values_store import (
    build_active_values_store,
    current_active_registry_fingerprint,
    load_active_values_manifest,
    resolve_active_values_lineage,
)
from domain.factor_research.factor_compute import (
    NON_ST_FILTER_COLUMNS,
    _compute_factor_from_market_df,
    _load_market_data,
    _required_market_columns,
    _trim_factor_output,
    _warmup_start_date,
)
from storage.paths import FACTOR_ACTIVE_ADOPTED_VALUES_FILE, FACTOR_DEFAULT_HOLDING_PERIOD, QLIB_DATA_ROOT


def calendar_dates(start_date: str, end_date: str) -> list[pd.Timestamp]:
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    cal = QLIB_DATA_ROOT / "calendars/day.txt"
    if cal.exists():
        out: list[pd.Timestamp] = []
        for line in cal.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            day = pd.Timestamp(text).normalize()
            if start <= day <= end:
                out.append(day)
        return out
    return [pd.Timestamp(day).normalize() for day in pd.bdate_range(start, end)]


def parquet_index_date_bounds(path: Path) -> tuple[pd.Timestamp | None, pd.Timestamp | None, int, int]:
    if not path.exists():
        return None, None, 0, 0
    df = pd.read_parquet(path, columns=[])
    if len(df) == 0 or not isinstance(df.index, pd.MultiIndex):
        return None, None, int(len(df)), 0
    if "trade_date" in df.index.names:
        raw_dates = df.index.get_level_values("trade_date")
    elif "datetime" in df.index.names:
        raw_dates = df.index.get_level_values("datetime")
    else:
        raw_dates = df.index.get_level_values(-1)
    dates = pd.to_datetime(raw_dates).normalize()
    return dates.min(), dates.max(), int(len(df)), int(dates.nunique())


def _append_factor_tail(
    *,
    record: dict[str, Any],
    factor_df: pd.DataFrame,
    tail_start: pd.Timestamp,
    run_id: str,
) -> dict[str, Any]:
    data_path = Path(str(record.get("data_path") or ""))
    data_column = str(record.get("data_column") or "")
    if not data_path:
        raise RuntimeError(f"{record.get('factor_id')}: missing data_path")
    if not data_column:
        raise RuntimeError(f"{record.get('factor_id')}: missing data_column")
    if factor_df.empty:
        raise RuntimeError(f"{record.get('factor_id')}: empty computed tail")

    out_tail = factor_df.copy()
    out_tail.columns = pd.MultiIndex.from_product([["feature"], [data_column]])

    existing = pd.read_parquet(data_path) if data_path.exists() else pd.DataFrame()
    if not existing.empty:
        existing_dates = pd.to_datetime(existing.index.get_level_values("datetime")).normalize()
        existing = existing[existing_dates < tail_start].copy()
    combined = pd.concat([existing, out_tail], axis=0).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]

    tmp_path = data_path.with_name(f".tmp.{run_id}.{data_path.name}")
    combined.to_parquet(tmp_path, engine="pyarrow")
    os.replace(tmp_path, data_path)
    stat = data_path.stat()
    start, end, rows, unique_dates = parquet_index_date_bounds(data_path)
    return {
        "factor_id": record.get("factor_id"),
        "path": str(data_path),
        "rows": rows,
        "unique_dates": unique_dates,
        "tail_rows": int(len(out_tail)),
        "start_date": str(start.date()) if start is not None else "",
        "end_date": str(end.date()) if end is not None else "",
        "data_mtime": stat.st_mtime,
        "data_size": stat.st_size,
    }


def refresh_active_values_tail(
    *,
    holding_period_days: int | None = FACTOR_DEFAULT_HOLDING_PERIOD,
    start_date: str | None = None,
    end_date: str | None = None,
    run_id: str | None = None,
    sync_quantgpt: bool = True,
) -> dict[str, Any]:
    lineage = resolve_active_values_lineage(start_date=None, end_date=end_date)
    target_end = pd.Timestamp(lineage["value_end_date"]).normalize()
    current_start, current_max, current_rows, current_unique_dates = parquet_index_date_bounds(
        FACTOR_ACTIVE_ADOPTED_VALUES_FILE
    )
    registry_fingerprint, records = current_active_registry_fingerprint(
        holding_period_days=holding_period_days,
        end_date=str(target_end.date()),
    )

    records_to_refresh: list[tuple[dict[str, Any], pd.Timestamp]] = []
    factor_date_status: list[dict[str, Any]] = []
    for record in records:
        path = Path(str(record.get("data_path") or ""))
        _, factor_max, factor_rows, factor_unique_dates = parquet_index_date_bounds(path)
        if start_date:
            factor_tail_start = pd.Timestamp(start_date).normalize()
        elif factor_max is not None:
            dates_after = [
                day
                for day in calendar_dates(str(factor_max.date()), str(target_end.date()))
                if day > factor_max
            ]
            factor_tail_start = dates_after[0] if dates_after else target_end + pd.Timedelta(days=1)
        else:
            factor_tail_start = pd.Timestamp(lineage["value_start_date"]).normalize()
        needs_refresh = factor_tail_start <= target_end
        factor_date_status.append(
            {
                "factor_id": record.get("factor_id"),
                "data_path": str(path),
                "current_max_date": str(factor_max.date()) if factor_max is not None else "",
                "tail_start_date": str(factor_tail_start.date()) if needs_refresh else "",
                "target_end_date": str(target_end.date()),
                "rows": factor_rows,
                "unique_dates": factor_unique_dates,
                "needs_refresh": needs_refresh,
            }
        )
        if needs_refresh:
            records_to_refresh.append((record, factor_tail_start))

    run_id = run_id or f"avj_{datetime.now().strftime('%Y%m%d_%H%M%S')}_tail_{registry_fingerprint[:12]}"
    manifest = load_active_values_manifest() or {}
    manifest_registry_fingerprint = str(manifest.get("registry_fingerprint") or "")
    manifest_factor_count = int(manifest.get("factor_count") or 0)
    if (
        not records_to_refresh
        and current_max is not None
        and current_max >= target_end
        and manifest_registry_fingerprint == registry_fingerprint
        and manifest_factor_count == len(records)
    ):
        return {
            "status": "already_current",
            "job_id": run_id,
            "source_mode": "compute_tail",
            "target_end_date": str(target_end.date()),
            "current_before": {
                "start_date": str(current_start.date()) if current_start is not None else "",
                "end_date": str(current_max.date()) if current_max is not None else "",
                "rows": current_rows,
                "unique_dates": current_unique_dates,
            },
            "final_active_values": {
                "start_date": str(current_start.date()) if current_start is not None else "",
                "end_date": str(current_max.date()) if current_max is not None else "",
                "rows": current_rows,
                "unique_dates": current_unique_dates,
            },
            "factor_count": len(records),
            "refresh_factor_count": 0,
            "factor_date_status": factor_date_status,
            "refreshed_factors": [],
            "active_values_manifest": manifest,
        }

    if records_to_refresh:
        tail_start = min(item[1] for item in records_to_refresh)
        expressions = [
            str(record.get("expression") or "")
            for record, _ in records_to_refresh
            if record.get("expression")
        ]
        required_columns = set(_required_market_columns(expressions)) | NON_ST_FILTER_COLUMNS
        market_df = _load_market_data(
            start_date=_warmup_start_date(str(tail_start.date())),
            end_date=str(target_end.date()),
            required_columns=required_columns,
            filter_non_st=False,
        )
        if market_df.empty:
            raise RuntimeError(f"no market data for tail refresh {tail_start.date()}..{target_end.date()}")
        refreshed: list[dict[str, Any]] = []
        for idx, (record, factor_tail_start) in enumerate(records_to_refresh, start=1):
            expression = str(record.get("expression") or "")
            factor_df = _compute_factor_from_market_df(market_df, expression)
            factor_df = _trim_factor_output(
                factor_df,
                str(factor_tail_start.date()),
                str(target_end.date()),
            )
            refreshed.append(
                {
                    **_append_factor_tail(
                        record=record,
                        factor_df=factor_df,
                        tail_start=factor_tail_start,
                        run_id=run_id,
                    ),
                    "ordinal": idx,
                }
            )
    else:
        refreshed = []

    manifest = build_active_values_store(
        holding_period_days=holding_period_days,
        run_id=run_id,
        end_date=str(target_end.date()),
        source_mode="parquet",
        sync_quantgpt=sync_quantgpt,
    )
    final_start, final_end, final_rows, final_unique_dates = parquet_index_date_bounds(
        FACTOR_ACTIVE_ADOPTED_VALUES_FILE
    )
    return {
        "status": "completed" if records_to_refresh else "already_current_rebuilt",
        "job_id": run_id,
        "source_mode": "compute_tail",
        "target_end_date": str(target_end.date()),
        "current_before": {
            "start_date": str(current_start.date()) if current_start is not None else "",
            "end_date": str(current_max.date()) if current_max is not None else "",
            "rows": current_rows,
            "unique_dates": current_unique_dates,
        },
        "final_active_values": {
            "start_date": str(final_start.date()) if final_start is not None else "",
            "end_date": str(final_end.date()) if final_end is not None else "",
            "rows": final_rows,
            "unique_dates": final_unique_dates,
        },
        "factor_count": len(records),
        "refresh_factor_count": len(records_to_refresh),
        "factor_date_status": factor_date_status,
        "refreshed_factors": refreshed,
        "active_values_manifest": manifest,
    }
