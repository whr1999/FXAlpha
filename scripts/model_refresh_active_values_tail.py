#!/usr/bin/env python
"""Refresh the missing tail of active factor values before model training.

This script is intentionally narrower than a full ``source_mode=compute``
refresh.  It recomputes only the dates after the current active-values parquet
and then rebuilds the wide active-values table from the refreshed factor
parquets.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from domain.factor_research.active_values_store import (
    build_active_values_store,
    current_active_registry_fingerprint,
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
from services.factor_active_values_service import _set_state, _write_refresh_job
from storage.paths import FACTOR_ACTIVE_ADOPTED_VALUES_FILE, QLIB_DATA_ROOT, RUNTIME_ROOT
from domain.factor_research.active_values_tail_refresh import refresh_active_values_tail


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _calendar_dates(start_date: str, end_date: str) -> list[pd.Timestamp]:
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


def _active_values_max_date(path: Path) -> pd.Timestamp | None:
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=[])
    if len(df) == 0:
        return None
    idx = df.index
    if isinstance(idx, pd.MultiIndex):
        if "trade_date" in idx.names:
            values = idx.get_level_values("trade_date")
        elif "datetime" in idx.names:
            values = idx.get_level_values("datetime")
        else:
            values = idx.get_level_values(-1)
        return pd.to_datetime(values).max().normalize()
    return None


def _factor_date_bounds(df: pd.DataFrame) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    if len(df) == 0:
        return None, None
    if isinstance(df.index, pd.MultiIndex):
        if "datetime" in df.index.names:
            dates = pd.to_datetime(df.index.get_level_values("datetime")).normalize()
        elif "trade_date" in df.index.names:
            dates = pd.to_datetime(df.index.get_level_values("trade_date")).normalize()
        else:
            dates = pd.to_datetime(df.index.get_level_values(-1)).normalize()
        return dates.min(), dates.max()
    return None, None


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
    start, end = _factor_date_bounds(combined)
    return {
        "factor_id": record.get("factor_id"),
        "path": str(data_path),
        "rows": int(len(combined)),
        "tail_rows": int(len(out_tail)),
        "start_date": str(start.date()) if start is not None else "",
        "end_date": str(end.date()) if end is not None else "",
        "data_mtime": stat.st_mtime,
        "data_size": stat.st_size,
    }


def refresh_tail(*, end_date: str | None = None, start_date: str | None = None) -> dict[str, Any]:
    lineage = resolve_active_values_lineage(end_date=end_date)
    target_end = pd.Timestamp(lineage["value_end_date"]).normalize()
    current_max = _active_values_max_date(FACTOR_ACTIVE_ADOPTED_VALUES_FILE)
    registry_fingerprint, records = current_active_registry_fingerprint(holding_period_days=5, end_date=str(target_end.date()))
    records_to_refresh: list[tuple[dict[str, Any], pd.Timestamp]] = []
    factor_date_status: list[dict[str, Any]] = []
    for record in records:
        path = Path(str(record.get("data_path") or ""))
        factor_max: pd.Timestamp | None = None
        if path.exists():
            try:
                existing = pd.read_parquet(path, columns=[])
                _, factor_max = _factor_date_bounds(existing)
            except Exception:
                factor_max = None
        if start_date:
            factor_tail_start = pd.Timestamp(start_date).normalize()
        elif factor_max is not None:
            dates_after = [day for day in _calendar_dates(str(factor_max.date()), str(target_end.date())) if day > factor_max]
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
                "needs_refresh": needs_refresh,
            }
        )
        if needs_refresh:
            records_to_refresh.append((record, factor_tail_start))
    if not records_to_refresh:
        return {
            "status": "already_current",
            "current_max_date": str(current_max.date()) if current_max is not None else "",
            "target_end_date": str(target_end.date()),
            "factor_count": len(records),
            "factor_date_status": factor_date_status,
        }
    tail_start = min(item[1] for item in records_to_refresh)
    run_id = f"avj_{datetime.now().strftime('%Y%m%d_%H%M%S')}_tail_{registry_fingerprint[:12]}"
    _write_refresh_job(
        run_id,
        status="running",
        trigger="model_tail_refresh",
        source_mode="compute_tail",
        refresh_model=False,
        holding_period_days=5,
        requested_registry_fingerprint=registry_fingerprint,
        started_at=_now(),
        payload={
            "tail_start_date": str(tail_start.date()),
            "target_end_date": str(target_end.date()),
            "factor_count": len(records),
            "refresh_factor_count": len(records_to_refresh),
            "factor_date_status": factor_date_status,
        },
    )
    _set_state(
        job_id=run_id,
        status="running",
        trigger="model_tail_refresh",
        source_mode="compute_tail",
        last_error="",
        last_started_at=_now(),
        requested_registry_fingerprint=registry_fingerprint,
        registry_fingerprint=registry_fingerprint,
        active_values_refresh_required=True,
        model_refresh_required=False,
        model_snapshot_refresh_required=False,
    )

    try:
        expressions = [str(record.get("expression") or "") for record, _ in records_to_refresh if record.get("expression")]
        required_columns = set(_required_market_columns(expressions)) | NON_ST_FILTER_COLUMNS
        load_start = _warmup_start_date(str(tail_start.date()))
        market_df = _load_market_data(
            start_date=load_start,
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
            factor_df = _trim_factor_output(factor_df, str(factor_tail_start.date()), str(target_end.date()))
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
            if idx % 10 == 0:
                _write_refresh_job(
                    run_id,
                    status="running",
                    payload={"progress": {"completed_factors": idx, "total_factors": len(records_to_refresh)}},
                )

        manifest = build_active_values_store(source_mode="parquet", run_id=run_id, end_date=str(target_end.date()))
        _set_state(
            job_id=run_id,
            status="completed",
            active_values_refresh_required=False,
            model_refresh_required=False,
            model_snapshot_refresh_required=True,
            last_finished_at=_now(),
            last_error="",
            active_values_manifest=manifest,
            requested_registry_fingerprint=str(manifest.get("registry_fingerprint") or registry_fingerprint),
            registry_fingerprint=str(manifest.get("registry_fingerprint") or registry_fingerprint),
            built_registry_fingerprint=str(manifest.get("registry_fingerprint") or registry_fingerprint),
            source_mode="compute_tail",
        )
        _write_refresh_job(
            run_id,
            status="completed",
            requested_registry_fingerprint=str(manifest.get("registry_fingerprint") or registry_fingerprint),
            built_registry_fingerprint=str(manifest.get("registry_fingerprint") or registry_fingerprint),
            finished_at=_now(),
            payload={"active_values_manifest": manifest, "refreshed_factors": refreshed},
        )
        return {
            "status": "completed",
            "job_id": run_id,
            "tail_start_date": str(tail_start.date()),
            "target_end_date": str(target_end.date()),
            "factor_count": len(records),
            "refresh_factor_count": len(records_to_refresh),
            "active_values_manifest": manifest,
            "refreshed_factors_preview": refreshed[:5],
        }
    except Exception as exc:
        _set_state(
            job_id=run_id,
            status="active_values_refresh_failed",
            active_values_refresh_required=True,
            last_finished_at=_now(),
            last_error=str(exc),
        )
        _write_refresh_job(run_id, status="failed", finished_at=_now(), last_error=str(exc))
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh only the missing active-values tail for model clean starts.")
    parser.add_argument("--start-date", default="", help="Optional explicit tail start date.")
    parser.add_argument("--end-date", default="", help="Optional explicit target end date.")
    parser.add_argument("--output", default="", help="Optional JSON report path.")
    args = parser.parse_args()
    result = refresh_active_values_tail(start_date=args.start_date or None, end_date=args.end_date or None)
    text = json.dumps(_jsonable(result), ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        report = RUNTIME_ROOT / "model" / "latest_active_values_tail_refresh.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
