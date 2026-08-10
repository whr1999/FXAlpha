#!/usr/bin/env python3
"""Convert Tushare index rows from stock_daily.h5 to Qlib benchmark bins.

The source HDF is the production Tushare raw store.  The default import path is
kept independent from the legacy AmazingData adapter tree.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage.paths import PRODUCTION_RAW_HDF5, QLIB_CALENDAR_FILE, QLIB_DATA_ROOT, QLIB_INDEX_META
from domain.data_foundation.runtime_io import atomic_write_json

SOURCE_H5 = PRODUCTION_RAW_HDF5
QLIB_DIR = QLIB_DATA_ROOT
FEATURES_DIR = QLIB_DIR / 'features'
CALENDAR_PATH = QLIB_CALENDAR_FILE
META_FILE = QLIB_INDEX_META


def ensure_repo_data_dirs() -> None:
    for path in (QLIB_DIR, FEATURES_DIR, QLIB_DIR / 'calendars', SOURCE_H5.parent):
        path.mkdir(parents=True, exist_ok=True)

INDEX_CODE_MAP = {
    '000300.SH': '000300sh', '000001.SH': '000001sh', '399001.SZ': '399001sz', '399006.SZ': '399006sz',
    '000016.SH': '000016sh', '000905.SH': '000905sh', '000852.SH': '000852sh',
}
FIELD_MAP = {'open': 'open', 'high': 'high', 'low': 'low', 'close': 'close', 'volume': 'volume', 'amount': 'amount', 'pre_close': 'pre_close', 'pct_chg': 'pct_chg', 'change': 'change', 'factor': 'factor'}


def write_bin_file(file_path: Path, data: np.ndarray, start_index: int) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = np.hstack([[float(start_index)], data.astype(np.float32)]).astype('<f4')
    working_path = file_path.with_name(f'.{file_path.name}.tmp-{os.getpid()}')
    working_path.unlink(missing_ok=True)
    try:
        with open(working_path, 'wb') as f:
            payload.tofile(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(working_path, file_path)
    except Exception:
        working_path.unlink(missing_ok=True)
        raise


def load_calendar() -> list[str]:
    calendar = [line.strip() for line in CALENDAR_PATH.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not calendar:
        raise ValueError(f'calendar empty: {CALENDAR_PATH}')
    return calendar


def _source_latest_index_date(df: pd.DataFrame) -> str | None:
    idx = df[df['code'].isin(INDEX_CODE_MAP.keys())].copy()
    return None if idx.empty else str(pd.Timestamp(idx.index.max()).date())


def _rewrite_one_index(df: pd.DataFrame, source_code: str, qlib_code: str, calendar: list[str]) -> dict[str, Any] | None:
    idx = df[df['code'] == source_code].copy()
    if idx.empty:
        return None
    idx = idx.sort_index()
    calendar_index = {d: i for i, d in enumerate(calendar)}
    idx_dates = [str(pd.Timestamp(d).date()) for d in idx.index]
    valid_dates = [d for d in idx_dates if d in calendar_index]
    if not valid_dates:
        return None
    start_idx = calendar_index[valid_dates[0]]
    end_idx = calendar_index[valid_dates[-1]]
    full_dates = pd.to_datetime(calendar[start_idx:end_idx + 1])
    out_dir = FEATURES_DIR / qlib_code
    out_dir.mkdir(parents=True, exist_ok=True)
    if 'change' not in idx.columns and 'pct_chg' in idx.columns:
        idx['change'] = pd.to_numeric(idx['pct_chg'], errors='coerce') / 100.0
    if 'factor' not in idx.columns:
        idx['factor'] = 1.0
    for src_field, out_field in FIELD_MAP.items():
        if src_field not in idx.columns:
            continue
        series = pd.Series(np.nan, index=full_dates, dtype=np.float32)
        actual = idx[src_field].copy()
        actual.index = pd.to_datetime([str(pd.Timestamp(d).date()) for d in idx.index])
        actual = actual[(actual.index >= full_dates[0]) & (actual.index <= full_dates[-1])]
        series.update(actual.astype(np.float32))
        write_bin_file(out_dir / f'{out_field}.day.bin', series.values, start_idx)
    return {'source_code': source_code, 'qlib_code': qlib_code, 'latest_date': valid_dates[-1], 'count': len(valid_dates)}


def _full_convert(df: pd.DataFrame, calendar: list[str]) -> dict[str, Any]:
    updated = []
    for source_code, qlib_code in INDEX_CODE_MAP.items():
        result = _rewrite_one_index(df, source_code, qlib_code, calendar)
        if result is not None:
            updated.append(result)
    return {'effective_mode': 'full', 'updated_count': len(updated), 'updated': updated, 'calendar_latest_date': calendar[-1]}


def _read_existing_index_latest_date(qlib_code: str, calendar: list[str]) -> str | None:
    close_file = FEATURES_DIR / qlib_code / 'close.day.bin'
    if not close_file.exists():
        return None
    with open(close_file, 'rb') as f:
        payload = np.frombuffer(f.read(), dtype='<f4')
    if len(payload) <= 1:
        return None
    start_idx = int(payload[0])
    data = payload[1:]
    valid_idx = np.where(~np.isnan(data))[0]
    if len(valid_idx) == 0:
        return None
    latest_idx = start_idx + int(valid_idx[-1])
    if latest_idx >= len(calendar):
        return None
    return calendar[latest_idx]


def _incremental_convert(df: pd.DataFrame, calendar: list[str]) -> dict[str, Any]:
    source_latest = _source_latest_index_date(df)
    qlib_calendar_latest = calendar[-1]
    if source_latest is None:
        return {'effective_mode': 'incremental', 'skipped': True, 'reason': 'no index rows found in source H5', 'source_latest_date': None, 'qlib_calendar_latest_date': qlib_calendar_latest}

    affected = []
    status = []
    for source_code, qlib_code in INDEX_CODE_MAP.items():
        idx = df[df['code'] == source_code].copy()
        if idx.empty:
            status.append({'source_code': source_code, 'qlib_code': qlib_code, 'source_latest_date': None, 'existing_latest_date': _read_existing_index_latest_date(qlib_code, calendar), 'updated': False, 'reason': 'source_missing'})
            continue
        source_code_latest = str(pd.Timestamp(idx.index.max()).date())
        existing_latest = _read_existing_index_latest_date(qlib_code, calendar)
        needs_update = existing_latest is None or source_code_latest > existing_latest
        if needs_update:
            result = _rewrite_one_index(df, source_code, qlib_code, calendar)
            if result is not None:
                affected.append(result)
                status.append({'source_code': source_code, 'qlib_code': qlib_code, 'source_latest_date': source_code_latest, 'existing_latest_date': existing_latest, 'updated': True})
                continue
        status.append({'source_code': source_code, 'qlib_code': qlib_code, 'source_latest_date': source_code_latest, 'existing_latest_date': existing_latest, 'updated': False, 'reason': 'already_fresh'})

    skipped = len(affected) == 0
    return {
        'effective_mode': 'incremental',
        'skipped': skipped,
        'reason': 'all benchmark features already fresh' if skipped else None,
        'source_latest_date': source_latest,
        'qlib_calendar_latest_date': qlib_calendar_latest,
        'affected_index_count': len(affected),
        'affected': affected,
        'index_status': status,
    }


def _write_meta(summary: dict[str, Any]) -> None:
    atomic_write_json(META_FILE, summary)


def configure_paths(*, source_h5: str | None = None, qlib_dir: str | None = None, meta_path: str | None = None) -> None:
    global SOURCE_H5, QLIB_DIR, FEATURES_DIR, CALENDAR_PATH, META_FILE
    if source_h5:
        SOURCE_H5 = Path(source_h5).expanduser()
    if qlib_dir:
        QLIB_DIR = Path(qlib_dir).expanduser()
        FEATURES_DIR = QLIB_DIR / 'features'
        CALENDAR_PATH = QLIB_DIR / 'calendars' / 'day.txt'
        META_FILE = QLIB_DIR / 'index_converter_meta.json'
    if meta_path:
        META_FILE = Path(meta_path).expanduser()


def convert(mode: str = 'auto') -> dict[str, Any]:
    if not SOURCE_H5.exists():
        raise FileNotFoundError(f'source H5 not found: {SOURCE_H5}')
    if not CALENDAR_PATH.exists():
        raise FileNotFoundError(f'calendar not found: {CALENDAR_PATH}')
    df = pd.read_hdf(SOURCE_H5, key='/daily')
    calendar = load_calendar()
    requested_mode = mode
    if mode == 'auto':
        mode = 'incremental' if FEATURES_DIR.exists() else 'full'
    result = _incremental_convert(df, calendar) if mode == 'incremental' else _full_convert(df, calendar)
    result.update({
        'kind': 'qlib_index_converter',
        'mode': requested_mode,
        'calendar_latest_date': calendar[-1],
        'price_mode': 'index_raw_close_identity_adjusted',
        'change_field': 'pct_chg_decimal',
        'factor_field': 'constant_one_when_missing',
        'generated_at': datetime.now().isoformat(),
        'meta_path': str(META_FILE),
        'output_dir': str(QLIB_DIR),
    })
    _write_meta(result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='将指数刷新到 Qlib features/<index>/')
    parser.add_argument('--mode', choices=['auto', 'incremental', 'full'], default='auto')
    parser.add_argument('--source-h5')
    parser.add_argument('--qlib-dir')
    parser.add_argument('--meta-path')
    parser.add_argument('--json', action='store_true')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_repo_data_dirs()
    configure_paths(source_h5=args.source_h5, qlib_dir=args.qlib_dir, meta_path=args.meta_path)
    summary = convert(mode=args.mode)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
