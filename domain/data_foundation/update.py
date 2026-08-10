from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from domain.data_foundation.convert_to_quantgpt import quantgpt_contract_report
from storage.paths import (
    CURRENT_PRODUCTION_DATASET_FILE,
    LATEST_STATUS_FILE,
    PRODUCTION_RAW_HDF5,
    PRODUCTION_RAW_METADATA,
    PRODUCTION_TRADING_CALENDAR_FILE,
    QLIB_CALENDAR_FILE,
    QLIB_DATA_ROOT,
    QLIB_INDEX_META,
    QLIB_STOCK_META,
    QUANTGPT_BENCHMARK_DIR,
    QUANTGPT_DATA_DIR,
)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _safe_latest_parquet_date(parquet_file: Path) -> pd.Timestamp | None:
    try:
        df = pd.read_parquet(parquet_file, columns=["trade_date"])
        if df.empty:
            return None
        return pd.to_datetime(df["trade_date"]).max()
    except Exception:
        return None


def _quantgpt_snapshot(hdf5_latest_date: str | None, deep: bool) -> dict:
    stock_files = sorted(QUANTGPT_DATA_DIR.glob("*.parquet"))
    benchmark_files = sorted(QUANTGPT_BENCHMARK_DIR.glob("*.parquet"))
    payload = {
        "quantgpt_stock_parquet_count": len(stock_files),
        "quantgpt_benchmark_file_count": len(benchmark_files),
        "quantgpt_benchmark_files": [p.name for p in benchmark_files],
        "latest_quantgpt_trade_date": None,
    }
    payload["quantgpt_contract"] = quantgpt_contract_report(
        QUANTGPT_DATA_DIR,
        sample_limit=None if deep else 50,
    )
    if not stock_files:
        return payload

    if not deep:
        sample_dates = [dt for dt in (_safe_latest_parquet_date(p) for p in stock_files[:20]) if dt is not None]
        if sample_dates:
            payload["latest_quantgpt_trade_date"] = str(max(sample_dates).date())
        return payload

    latest_dates: list[pd.Timestamp] = []
    same_as_hdf5 = 0
    stale = 0
    stale_examples: list[str] = []
    for parquet_file in stock_files:
        latest = _safe_latest_parquet_date(parquet_file)
        if latest is None:
            continue
        latest_dates.append(latest)
        latest_str = str(latest.date())
        if hdf5_latest_date and latest_str == hdf5_latest_date:
            same_as_hdf5 += 1
        elif hdf5_latest_date and latest_str < hdf5_latest_date:
            stale += 1
            if len(stale_examples) < 10:
                stale_examples.append(parquet_file.name)

    if latest_dates:
        payload["latest_quantgpt_trade_date"] = str(max(latest_dates).date())
    payload["quantgpt_stocks_on_hdf5_latest_date"] = same_as_hdf5
    payload["quantgpt_stale_stock_count"] = stale
    payload["quantgpt_stale_stock_examples"] = stale_examples
    payload["quantgpt_latest_coverage_ratio"] = round(same_as_hdf5 / len(stock_files), 4) if stock_files else None
    return payload


def _calendar_latest_date() -> str | None:
    if not QLIB_CALENDAR_FILE.exists():
        return None
    lines = [line.strip() for line in QLIB_CALENDAR_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    return lines[-1] if lines else None


def _latest_hdf5_trade_date_light(hdf5_path: Path) -> str | None:
    if not hdf5_path.exists():
        return None
    latest = None
    with pd.HDFStore(hdf5_path, mode="r") as store:
        if "/daily" not in store:
            return None
        nrows = int(store.get_storer("/daily").nrows or 0)
        for start in range(0, nrows, 500000):
            chunk = store.select("/daily", start=start, stop=min(start + 500000, nrows), columns=["kline_time"])
            if chunk.empty:
                continue
            chunk_latest = pd.to_datetime(chunk["kline_time"], errors="coerce").max()
            if pd.isna(chunk_latest):
                continue
            latest = chunk_latest if latest is None else max(latest, chunk_latest)
    return str(pd.Timestamp(latest).date()) if latest is not None else None


def _build_snapshot(deep: bool = False) -> dict:
    snapshot = {
        "production_raw_hdf5": str(PRODUCTION_RAW_HDF5),
        "production_raw_metadata": str(PRODUCTION_RAW_METADATA),
        "production_trading_calendar": str(PRODUCTION_TRADING_CALENDAR_FILE),
        "qlib_data_root": str(QLIB_DATA_ROOT),
        "quantgpt_data_dir": str(QUANTGPT_DATA_DIR),
        "quantgpt_benchmark_dir": str(QUANTGPT_BENCHMARK_DIR),
        "latest_hdf5_trade_date": None,
        "latest_qlib_trade_date": _calendar_latest_date(),
        "latest_quantgpt_trade_date": None,
    }
    snapshot["latest_hdf5_trade_date"] = _latest_hdf5_trade_date_light(PRODUCTION_RAW_HDF5)

    qlib_stock_meta = _read_json(QLIB_STOCK_META)
    qlib_index_meta = _read_json(QLIB_INDEX_META)
    snapshot["qlib_stock_meta"] = qlib_stock_meta
    snapshot["qlib_index_meta"] = qlib_index_meta

    snapshot.update(_quantgpt_snapshot(snapshot["latest_hdf5_trade_date"], deep=deep))
    snapshot["consumer_readiness"] = {
        "quantgpt_factor_mining": bool(snapshot.get("quantgpt_stock_parquet_count")) and bool(snapshot.get("quantgpt_benchmark_file_count")),
        "qlib_model_training": QLIB_CALENDAR_FILE.exists(),
        "qlib_paper_trading": QLIB_CALENDAR_FILE.exists(),
    }
    return snapshot


def _artifact_contract() -> dict:
    return {
        "production_raw_hdf5": str(PRODUCTION_RAW_HDF5),
        "production_raw_metadata": str(PRODUCTION_RAW_METADATA),
        "production_trading_calendar": str(PRODUCTION_TRADING_CALENDAR_FILE),
        "qlib_data_root": str(QLIB_DATA_ROOT),
        "quantgpt_data_dir": str(QUANTGPT_DATA_DIR),
        "quantgpt_benchmark_dir": str(QUANTGPT_BENCHMARK_DIR),
        "latest_status_file": str(LATEST_STATUS_FILE),
        "current_production_dataset_file": str(CURRENT_PRODUCTION_DATASET_FILE),
    }


def data_foundation_status() -> dict:
    if LATEST_STATUS_FILE.exists():
        payload = json.loads(LATEST_STATUS_FILE.read_text(encoding="utf-8-sig"))
        snapshot = payload.get("snapshot")
        if isinstance(snapshot, dict):
            snapshot["latest_qlib_trade_date"] = _calendar_latest_date()
            snapshot["qlib_stock_meta"] = _read_json(QLIB_STOCK_META)
            snapshot["qlib_index_meta"] = _read_json(QLIB_INDEX_META)
            # Status is a read-only snapshot surface. Deep parquet/schema probes
            # belong to explicit audit and preflight paths, not GUI polling.
            snapshot.setdefault("status_snapshot_source", str(LATEST_STATUS_FILE))
        return payload
    return {
        "status": "not_started",
        "snapshot": _build_snapshot(deep=False),
        "steps": [],
        "artifacts": _artifact_contract(),
    }
