#!/usr/bin/env python3
"""Convert FXAlpha's canonical Tushare raw HDF directly to Qlib bins.

Production callers use the canonical raw ``/daily`` table; no intermediate
model-framework dataset is created.
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
from tqdm import tqdm
import warnings

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage.paths import PRODUCTION_RAW_HDF5, QLIB_DATA_ROOT, QLIB_STOCK_META
from domain.data_foundation.runtime_io import atomic_write_json, atomic_write_text
from domain.data_foundation.limit_execution import (
    DEFAULT_SEALED_TURNOVER_RATIO_THRESHOLD,
    LimitExecutionColumns,
    open_sealed_limit_fields,
)

warnings.filterwarnings('ignore')

H5_FILE = PRODUCTION_RAW_HDF5
OUTPUT_DIR = QLIB_DATA_ROOT
FEATURES_DIR = OUTPUT_DIR / 'features'
INSTRUMENTS_DIR = OUTPUT_DIR / 'instruments'
CALENDARS_DIR = OUTPUT_DIR / 'calendars'
CALENDAR_FILE = CALENDARS_DIR / 'day.txt'
INSTRUMENTS_FILE = INSTRUMENTS_DIR / 'all.txt'
META_FILE = QLIB_STOCK_META


def ensure_repo_data_dirs() -> None:
    for path in (OUTPUT_DIR, FEATURES_DIR, INSTRUMENTS_DIR, CALENDARS_DIR, H5_FILE.parent):
        path.mkdir(parents=True, exist_ok=True)

FIELD_MAP = {
    '$open': 'open', '$high': 'high', '$low': 'low', '$close': 'close', '$vwap': 'vwap', '$volume': 'volume', '$amount': 'amount',
    '$pre_close': 'pre_close', '$high_limited': 'high_limited', '$low_limited': 'low_limited', '$amp': 'amp',
    '$pe': 'pe', '$pb': 'pb', '$roe': 'roe', '$roa': 'roa', '$eps': 'eps', '$net_profit': 'net_profit',
    '$total_mv': 'total_mv', '$float_mv': 'float_mv', '$turnover_rate': 'turnover_rate', '$pct_chg': 'pct_chg',
    '$holder_num': 'holder_num', '$tot_equity': 'tot_equity', '$factor': 'factor', '$total_assets': 'total_assets',
    '$float_a_share': 'float_a_share', '$tot_share': 'tot_share', '$borrow_money_bal': 'borrow_money_bal',
    '$margin_trade_bal': 'margin_trade_bal', '$sec_lending_bal': 'sec_lending_bal', '$purch_borrow_money': 'purch_borrow_money',
    '$net_asset_ps': 'net_asset_ps',
    '$cost_15pct': 'cost_15pct', '$cost_85pct': 'cost_85pct', '$weight_avg': 'weight_avg',
    '$raw_open': 'raw_open', '$raw_high': 'raw_high', '$raw_low': 'raw_low', '$raw_close': 'raw_close',
    '$raw_pre_close': 'raw_pre_close', '$raw_pct_chg': 'raw_pct_chg', '$raw_amp': 'raw_amp', '$raw_change': 'raw_change',
    '$raw_vwap': 'raw_vwap', '$raw_cost_15pct': 'raw_cost_15pct', '$raw_cost_85pct': 'raw_cost_85pct', '$raw_weight_avg': 'raw_weight_avg',
    '$raw_up_limit': 'raw_up_limit', '$raw_down_limit': 'raw_down_limit',
    '$change': 'change',
    '$up_limit': 'up_limit', '$down_limit': 'down_limit',
    '$limit_rate': 'limit_rate', '$limit_buy': 'limit_buy', '$limit_sell': 'limit_sell',
    '$limit_buy_open': 'limit_buy_open', '$limit_sell_open': 'limit_sell_open',
    '$limit_buy_mid_oc': 'limit_buy_mid_oc', '$limit_sell_mid_oc': 'limit_sell_mid_oc',
    '$one_price_up_limit': 'one_price_up_limit', '$one_price_down_limit': 'one_price_down_limit',
    '$limit_turnover_ratio': 'limit_turnover_ratio', '$limit_low_liquidity': 'limit_low_liquidity',
    '$limit_buy_open_sealed': 'limit_buy_open_sealed', '$limit_sell_open_sealed': 'limit_sell_open_sealed',
    '$limit_buy_fallback': 'limit_buy_fallback', '$limit_sell_fallback': 'limit_sell_fallback',
    '$limit_source_official': 'limit_source_official', '$limit_source_no_limit': 'limit_source_no_limit',
    '$hit_up_limit_intraday': 'hit_up_limit_intraday', '$hit_down_limit_intraday': 'hit_down_limit_intraday',
}

RAW_OHLC_FIELDS = ('$open', '$high', '$low', '$close')
RAW_LIMIT_FIELDS = ('$up_limit', '$down_limit')
RAW_CHIP_COST_FIELDS = ('$cost_15pct', '$cost_85pct', '$weight_avg')
LIMIT_EPSILON = 0.005
LIMIT_PRICE_TOLERANCE = 1e-4
SEALED_TURNOVER_RATIO_THRESHOLD = DEFAULT_SEALED_TURNOVER_RATIO_THRESHOLD
SEALED_LIMIT_CONTRACT_VERSION = 'one_price_turnover_rate_only_v2'


def normalize_instrument(inst: str) -> str:
    inst = str(inst).upper()
    if inst.startswith('SH') or inst.startswith('SZ'):
        code_suffix = inst[2:] + inst[:2]
    else:
        code_suffix = inst
    return code_suffix.lower()


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


def _load_h5() -> tuple[pd.DataFrame, str]:
    """Load and normalize the canonical raw HDF source."""
    with pd.HDFStore(H5_FILE, mode='r') as store:
        keys = set(store.keys())
    if '/daily' in keys:
        from domain.data_foundation.tushare_production import (
            QLIB_RAW_FIELD_MAP,
            QLIB_RAW_OPTIONAL_COLUMNS,
            _daily_hdf_columns,
            _iter_daily_hdf_chunks,
            raw_chunk_to_qlib_frame,
        )

        available = set(_daily_hdf_columns(H5_FILE))
        required = set(QLIB_RAW_FIELD_MAP.values()) | {
            'code', 'kline_time', 'st_status', 'open', 'close', 'pct_chg'
        }
        missing = sorted(required - available)
        if missing:
            raise RuntimeError(f'qlib_raw_source_missing_columns:{missing}')
        read_columns = sorted(required | (available & set(QLIB_RAW_OPTIONAL_COLUMNS)))
        frames = [
            frame
            for chunk in _iter_daily_hdf_chunks(H5_FILE, columns=read_columns)
            if not (frame := raw_chunk_to_qlib_frame(chunk)).empty
        ]
        if not frames:
            empty_index = pd.MultiIndex.from_arrays([[], []], names=['datetime', 'instrument'])
            return pd.DataFrame(index=empty_index), 'canonical_raw_hdf'
        df = pd.concat(frames, axis=0, copy=False).sort_index()
        return _prepare_qlib_price_semantics(df), 'canonical_raw_hdf'
    raise KeyError(f'/daily does not exist in {H5_FILE}')


def _prepare_qlib_price_semantics(df: pd.DataFrame) -> pd.DataFrame:
    """Expose adjusted prices as Qlib's canonical trading price fields.

    The normalized raw frame stores raw prices plus ``$factor``.
    Qlib's official collectors write adjusted OHLC into the canonical price
    fields and keep ``$factor`` only for trade-unit rounding.  ``Exchange`` uses
    ``$close`` directly for mark-to-market and does not turn raw price plus
    factor into an adjusted return stream, so the Qlib provider must adjust
    price-like fields before writing bins.

    Official limit prices remain in raw exchange price space.  Limit booleans
    are therefore calculated from raw open/close/up_limit/down_limit snapshots
    before canonical price fields are adjusted.
    """
    if df.empty or '$factor' not in df.columns:
        return df
    out = df.copy()
    factor = pd.to_numeric(out['$factor'], errors='coerce')
    for field in RAW_OHLC_FIELDS:
        if field not in out.columns:
            continue
        raw_field = f"$raw_{field[1:]}"
        if raw_field not in out.columns:
            out[raw_field] = out[field]
    for field in RAW_LIMIT_FIELDS:
        if field not in out.columns:
            continue
        raw_field = f"$raw_{field[1:]}"
        if raw_field not in out.columns:
            out[raw_field] = out[field]
    if '$pre_close' in out.columns:
        if '$raw_pre_close' not in out.columns:
            out['$raw_pre_close'] = out['$pre_close']
        raw_pre_close = pd.to_numeric(out['$raw_pre_close'], errors='coerce')
        if '$close' in out.columns:
            shifted_close = pd.to_numeric(out['$close'], errors='coerce').groupby(level='instrument').shift(1)
            out['$pre_close'] = shifted_close.where(shifted_close.notna(), raw_pre_close)
        else:
            out['$pre_close'] = raw_pre_close
    if '$pct_chg' in out.columns:
        if '$raw_pct_chg' not in out.columns:
            out['$raw_pct_chg'] = out['$pct_chg']
        if '$close' in out.columns:
            pre_close = pd.to_numeric(out.get('$pre_close'), errors='coerce').replace(0, np.nan)
            out['$pct_chg'] = (
                (pd.to_numeric(out['$close'], errors='coerce') - pre_close)
                / pre_close
                * 100.0
            )
    if {'$amount', '$volume'}.issubset(out.columns):
        volume = pd.to_numeric(out['$volume'], errors='coerce').replace(0, np.nan)
        raw_vwap = pd.to_numeric(out['$amount'], errors='coerce') * 10.0 / volume
        out['$raw_vwap'] = raw_vwap
        out['$vwap'] = raw_vwap
    for field in RAW_CHIP_COST_FIELDS:
        if field not in out.columns:
            continue
        raw_field = f"$raw_{field[1:]}"
        if raw_field not in out.columns:
            out[raw_field] = out[field]
    if '$close' in out.columns and '$pre_close' in out.columns:
        pre_close = pd.to_numeric(out.get('$pre_close'), errors='coerce').replace(0, np.nan)
        out['$raw_change'] = (pd.to_numeric(out['$close'], errors='coerce') - pre_close) / pre_close
        out['$change'] = out['$raw_change']
    _ensure_limit_fields(out)
    for field in RAW_OHLC_FIELDS:
        if field in out.columns:
            out[field] = pd.to_numeric(out[field], errors='coerce') * factor
    if '$pre_close' in out.columns and '$close' in out.columns:
        adjusted_prev_close = pd.to_numeric(out['$close'], errors='coerce').groupby(level='instrument').shift(1)
        raw_pre_close = pd.to_numeric(out.get('$raw_pre_close'), errors='coerce')
        out['$pre_close'] = adjusted_prev_close.where(adjusted_prev_close.notna(), raw_pre_close * factor)
    elif '$pre_close' in out.columns:
        out['$pre_close'] = pd.to_numeric(out['$pre_close'], errors='coerce') * factor
    if '$volume' in out.columns:
        out['$volume'] = pd.to_numeric(out['$volume'], errors='coerce') / factor.replace(0, np.nan)
    if '$vwap' in out.columns:
        out['$vwap'] = pd.to_numeric(out['$vwap'], errors='coerce') * factor
    for field in RAW_CHIP_COST_FIELDS:
        if field in out.columns:
            out[field] = pd.to_numeric(out[field], errors='coerce') * factor
    if '$close' in out.columns and '$pre_close' in out.columns:
        adjusted_pre_close = pd.to_numeric(out.get('$pre_close'), errors='coerce').replace(0, np.nan)
        out['$change'] = (pd.to_numeric(out['$close'], errors='coerce') - adjusted_pre_close) / adjusted_pre_close
    if '$pct_chg' in out.columns:
        out['$pct_chg'] = pd.to_numeric(out.get('$change'), errors='coerce') * 100.0
    if '$amp' in out.columns:
        if '$raw_amp' not in out.columns:
            out['$raw_amp'] = out['$amp']
        if {'$high', '$low', '$pre_close'}.issubset(out.columns):
            pre_close = pd.to_numeric(out['$pre_close'], errors='coerce').replace(0, np.nan)
            out['$amp'] = (
                (pd.to_numeric(out['$high'], errors='coerce') - pd.to_numeric(out['$low'], errors='coerce'))
                / pre_close
                * 100.0
            )
    return out


def _limit_rate_for_instrument(instrument: str) -> float:
    inst = str(instrument).lower()
    code = inst[:6]
    suffix = inst[6:]
    if suffix == 'bj' or code.startswith(('8', '4', '920')):
        return 0.30
    if (suffix == 'sh' and code.startswith('688')) or (suffix == 'sz' and code.startswith(('300', '301'))):
        return 0.20
    return 0.10


def _st_limit_mask(df: pd.DataFrame) -> pd.Series:
    if '$st_status' not in df.columns:
        return pd.Series(False, index=df.index)
    st_status = df['$st_status'].astype(str).str.upper()
    return st_status.str.contains('ST', na=False) | st_status.isin({'PT', '*ST', 'SST'})


def _ensure_limit_fields(df: pd.DataFrame) -> None:
    if df.empty:
        return
    required_limit_fields = {
        '$limit_rate', '$limit_buy', '$limit_sell',
        '$limit_buy_open', '$limit_sell_open',
        '$limit_buy_mid_oc', '$limit_sell_mid_oc',
        '$one_price_up_limit', '$one_price_down_limit',
        '$limit_turnover_ratio', '$limit_low_liquidity',
        '$limit_buy_open_sealed', '$limit_sell_open_sealed',
        '$limit_buy_fallback', '$limit_sell_fallback',
        '$limit_source_official',
        '$hit_up_limit_intraday', '$hit_down_limit_intraday',
    }
    if required_limit_fields.issubset(df.columns):
        return
    instruments = df.index.get_level_values('instrument').astype(str)
    rates = pd.Series(
        [_limit_rate_for_instrument(inst) for inst in instruments],
        index=df.index,
        dtype='float32',
    )
    rates = rates.mask(_st_limit_mask(df), 0.05).astype('float32')
    change_source = '$raw_change' if '$raw_change' in df.columns else '$change'
    if change_source not in df.columns:
        if {'$raw_close', '$raw_pre_close'}.issubset(df.columns):
            raw_pre_close = pd.to_numeric(df['$raw_pre_close'], errors='coerce').replace(0, np.nan)
            df['$raw_change'] = (pd.to_numeric(df['$raw_close'], errors='coerce') - raw_pre_close) / raw_pre_close
            change_source = '$raw_change'
        else:
            return
    change = pd.to_numeric(df[change_source], errors='coerce')
    if '$limit_rate' not in df.columns:
        df['$limit_rate'] = rates
    close_field = '$raw_close' if '$raw_close' in df.columns else '$close'
    open_field = '$raw_open' if '$raw_open' in df.columns else '$open'
    high_field = '$raw_high' if '$raw_high' in df.columns else '$high'
    low_field = '$raw_low' if '$raw_low' in df.columns else '$low'
    has_official = {'$up_limit', '$down_limit', close_field}.issubset(df.columns)
    if has_official:
        up_limit = pd.to_numeric(df['$up_limit'], errors='coerce')
        down_limit = pd.to_numeric(df['$down_limit'], errors='coerce')
        close = pd.to_numeric(df[close_field], errors='coerce')
        official_mask = up_limit.notna() & down_limit.notna()
        official_buy = close.ge(up_limit - LIMIT_PRICE_TOLERANCE).fillna(False)
        official_sell = close.le(down_limit + LIMIT_PRICE_TOLERANCE).fillna(False)
        if open_field in df.columns:
            open_price = pd.to_numeric(df[open_field], errors='coerce')
            official_buy_open = open_price.ge(up_limit - LIMIT_PRICE_TOLERANCE).fillna(False)
            official_sell_open = open_price.le(down_limit + LIMIT_PRICE_TOLERANCE).fillna(False)
            mid_oc_price = (open_price + close) / 2.0
            official_buy_mid_oc = mid_oc_price.ge(up_limit - LIMIT_PRICE_TOLERANCE).fillna(False)
            official_sell_mid_oc = mid_oc_price.le(down_limit + LIMIT_PRICE_TOLERANCE).fillna(False)
        else:
            official_buy_open = pd.Series(False, index=df.index)
            official_sell_open = pd.Series(False, index=df.index)
            official_buy_mid_oc = pd.Series(False, index=df.index)
            official_sell_mid_oc = pd.Series(False, index=df.index)
        if {high_field, low_field}.issubset(df.columns):
            high = pd.to_numeric(df[high_field], errors='coerce')
            low = pd.to_numeric(df[low_field], errors='coerce')
            hit_up_intraday = high.ge(up_limit - LIMIT_PRICE_TOLERANCE).fillna(False)
            hit_down_intraday = low.le(down_limit + LIMIT_PRICE_TOLERANCE).fillna(False)
        else:
            hit_up_intraday = pd.Series(False, index=df.index)
            hit_down_intraday = pd.Series(False, index=df.index)
        if '$limit_source_official' not in df.columns:
            df['$limit_source_official'] = official_mask.astype('float32')
        sealed_fields = open_sealed_limit_fields(
            df,
            LimitExecutionColumns(
                open=open_field,
                high=high_field,
                low=low_field,
                close=close_field,
                up_limit='$up_limit',
                down_limit='$down_limit',
                turnover_rate='$turnover_rate',
            ),
            official_mask=official_mask,
            price_tolerance=LIMIT_PRICE_TOLERANCE,
            turnover_ratio_threshold=SEALED_TURNOVER_RATIO_THRESHOLD,
        )
    else:
        official_mask = pd.Series(False, index=df.index)
        official_buy = pd.Series(False, index=df.index)
        official_sell = pd.Series(False, index=df.index)
        official_buy_open = pd.Series(False, index=df.index)
        official_sell_open = pd.Series(False, index=df.index)
        official_buy_mid_oc = pd.Series(False, index=df.index)
        official_sell_mid_oc = pd.Series(False, index=df.index)
        hit_up_intraday = pd.Series(False, index=df.index)
        hit_down_intraday = pd.Series(False, index=df.index)
        sealed_fields = {
            'one_price_up_limit': pd.Series(False, index=df.index, dtype='float32'),
            'one_price_down_limit': pd.Series(False, index=df.index, dtype='float32'),
            'limit_turnover_ratio': pd.Series(np.nan, index=df.index, dtype='float32'),
            'limit_low_liquidity': pd.Series(False, index=df.index, dtype='float32'),
            'limit_buy_open_sealed': pd.Series(False, index=df.index, dtype='float32'),
            'limit_sell_open_sealed': pd.Series(False, index=df.index, dtype='float32'),
        }
    threshold = pd.to_numeric(df['$limit_rate'], errors='coerce').fillna(rates) - LIMIT_EPSILON
    fallback_buy = change.ge(threshold).fillna(False)
    fallback_sell = change.le(-threshold).fillna(False)
    if '$limit_buy' not in df.columns:
        df['$limit_buy'] = official_buy.where(official_mask, False).astype('float32')
    if '$limit_sell' not in df.columns:
        df['$limit_sell'] = official_sell.where(official_mask, False).astype('float32')
    if '$limit_buy_open' not in df.columns:
        df['$limit_buy_open'] = official_buy_open.where(official_mask, False).astype('float32')
    if '$limit_sell_open' not in df.columns:
        df['$limit_sell_open'] = official_sell_open.where(official_mask, False).astype('float32')
    if '$limit_buy_mid_oc' not in df.columns:
        df['$limit_buy_mid_oc'] = official_buy_mid_oc.where(official_mask, False).astype('float32')
    if '$limit_sell_mid_oc' not in df.columns:
        df['$limit_sell_mid_oc'] = official_sell_mid_oc.where(official_mask, False).astype('float32')
    for name, series in sealed_fields.items():
        column = f'${name}'
        if column not in df.columns:
            df[column] = pd.to_numeric(series, errors='coerce').astype('float32')
    if '$limit_buy_fallback' not in df.columns:
        df['$limit_buy_fallback'] = fallback_buy.astype('float32')
    if '$limit_sell_fallback' not in df.columns:
        df['$limit_sell_fallback'] = fallback_sell.astype('float32')
    if '$hit_up_limit_intraday' not in df.columns:
        df['$hit_up_limit_intraday'] = hit_up_intraday.where(official_mask, False).astype('float32')
    if '$hit_down_limit_intraday' not in df.columns:
        df['$hit_down_limit_intraday'] = hit_down_intraday.where(official_mask, False).astype('float32')


def _load_existing_calendar() -> list[str]:
    if not CALENDAR_FILE.exists():
        return []
    return [line.strip() for line in CALENDAR_FILE.read_text(encoding='utf-8').splitlines() if line.strip()]


def _load_existing_instruments() -> dict[str, tuple[str, str]]:
    if not INSTRUMENTS_FILE.exists():
        return {}
    data = {}
    for line in INSTRUMENTS_FILE.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        inst, start_date, end_date = line.split('\t')
        data[inst] = (start_date, end_date)
    return data


def _load_existing_meta() -> dict[str, Any]:
    if not META_FILE.exists():
        return {}
    try:
        return json.loads(META_FILE.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {}


def _calendar_from_df(df: pd.DataFrame) -> list[str]:
    calendar = df.index.get_level_values(0).unique().sort_values()
    return [str(pd.Timestamp(d).date()) for d in calendar]


def _latest_source_date(df: pd.DataFrame) -> str | None:
    return None if df.empty else str(pd.Timestamp(df.index.get_level_values(0).max()).date())


def _valid_fields(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns.tolist() if c in FIELD_MAP]


def _rewrite_one(
    stock_data: pd.DataFrame,
    calendar_list: list[str],
    valid_h5_cols: list[str],
    calendar_index: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    if len(stock_data) == 0:
        return None
    inst_normalized = normalize_instrument(str(stock_data.attrs.get('instrument', '')))
    if not inst_normalized:
        raise RuntimeError('instrument attribute missing while rewriting bins')
    stock_dates_str = [str(pd.Timestamp(d).date()) for d in stock_data.index]
    if calendar_index is None:
        calendar_index = {d: i for i, d in enumerate(calendar_list)}
    valid_dates = [d for d in stock_dates_str if d in calendar_index]
    if not valid_dates:
        return None
    start_idx = calendar_index[valid_dates[0]]
    end_idx = calendar_index[valid_dates[-1]]
    full_index = pd.to_datetime(calendar_list[start_idx:end_idx + 1])
    inst_dir = FEATURES_DIR / inst_normalized
    inst_dir.mkdir(parents=True, exist_ok=True)
    for h5_col in valid_h5_cols:
        out_field = FIELD_MAP[h5_col]
        full_series = pd.Series(np.nan, dtype=np.float32, index=full_index)
        actual_series = stock_data[h5_col].astype(np.float32).copy()
        actual_series.index = pd.to_datetime(actual_series.index)
        actual_series = actual_series[(actual_series.index >= full_index[0]) & (actual_series.index <= full_index[-1])]
        full_series.update(actual_series)
        write_bin_file(inst_dir / f'{out_field}.day.bin', full_series.values, start_idx)
    return {'instrument': inst_normalized, 'start_date': valid_dates[0], 'end_date': valid_dates[-1]}


def _full_convert(df: pd.DataFrame) -> dict[str, Any]:
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    INSTRUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    CALENDARS_DIR.mkdir(parents=True, exist_ok=True)
    calendar_list = _calendar_from_df(df)
    calendar_index = {d: i for i, d in enumerate(calendar_list)}
    instruments = df.index.get_level_values('instrument').unique().tolist()
    valid_h5_cols = _valid_fields(df)
    instrument_info = []
    for inst in tqdm(instruments, desc='全量转换股票'):
        stock_data = df.xs(inst, level='instrument').copy()
        stock_data.attrs['instrument'] = inst
        result = _rewrite_one(stock_data, calendar_list, valid_h5_cols, calendar_index)
        if result is not None:
            instrument_info.append(result)
    atomic_write_text(
        INSTRUMENTS_FILE,
        ''.join(f"{item['instrument']}\t{item['start_date']}\t{item['end_date']}\n" for item in instrument_info),
    )
    atomic_write_text(CALENDAR_FILE, ''.join(f'{date}\n' for date in calendar_list))
    return {'effective_mode': 'full', 'calendar_latest_date': calendar_list[-1] if calendar_list else None, 'instrument_count': len(instrument_info), 'valid_field_count': len(valid_h5_cols)}


def _incremental_convert(df: pd.DataFrame) -> dict[str, Any]:
    existing_calendar = _load_existing_calendar()
    if not existing_calendar:
        result = _full_convert(df)
        result['effective_mode'] = 'full_fallback'
        return result
    source_latest = _latest_source_date(df)
    qlib_latest = existing_calendar[-1]
    instrument_book = _load_existing_instruments()
    existing_meta = _load_existing_meta()
    force_rewrite_reason = ''
    if existing_meta.get('sealed_limit_contract_version') != SEALED_LIMIT_CONTRACT_VERSION:
        force_rewrite_reason = 'sealed_limit_contract_version_mismatch'
    all_instruments = sorted(df.index.get_level_values('instrument').unique().tolist())
    missing_instruments = [
        inst for inst in all_instruments
        if normalize_instrument(inst) not in instrument_book
    ]
    valid_h5_cols = _valid_fields(df)
    expected_bin_fields = [FIELD_MAP[c] for c in valid_h5_cols]
    missing_field_instruments: list[str] = []
    missing_field_examples: dict[str, list[str]] = {}
    if force_rewrite_reason:
        missing_field_instruments = [
            inst for inst in all_instruments
            if normalize_instrument(inst) in instrument_book
        ]
        for inst in missing_field_instruments[:20]:
            missing_field_examples[normalize_instrument(inst)] = [force_rewrite_reason]
    else:
        for inst in all_instruments:
            inst_normalized = normalize_instrument(inst)
            if inst_normalized not in instrument_book:
                continue
            inst_dir = FEATURES_DIR / inst_normalized
            missing_fields = [
                field for field in expected_bin_fields
                if not (inst_dir / f'{field}.day.bin').exists()
            ]
            if missing_fields:
                missing_field_instruments.append(inst)
                if len(missing_field_examples) < 20:
                    missing_field_examples[inst_normalized] = missing_fields[:20]
    if source_latest is None:
        return {'effective_mode': 'incremental', 'skipped': True, 'reason': 'source HDF is empty', 'source_latest_date': None, 'qlib_latest_date': qlib_latest, 'valid_field_count': len(valid_h5_cols), 'valid_fields': [FIELD_MAP[c] for c in valid_h5_cols]}
    if source_latest <= qlib_latest and not missing_instruments and not missing_field_instruments:
        return {'effective_mode': 'incremental', 'skipped': True, 'reason': 'no new source dates beyond existing Qlib calendar latest date and no missing field bins', 'source_latest_date': source_latest, 'qlib_latest_date': qlib_latest, 'valid_field_count': len(valid_h5_cols), 'valid_fields': [FIELD_MAP[c] for c in valid_h5_cols], 'sealed_limit_contract_version': SEALED_LIMIT_CONTRACT_VERSION}
    full_calendar = _calendar_from_df(df)
    full_calendar_index = {d: i for i, d in enumerate(full_calendar)}
    new_dates = [d for d in full_calendar if d > qlib_latest]
    incremental_df = df.loc[df.index.get_level_values('datetime') > pd.Timestamp(qlib_latest)].copy()
    affected_by_date = sorted(incremental_df.index.get_level_values('instrument').unique().tolist()) if not incremental_df.empty else []
    affected_instruments = sorted(set(affected_by_date) | set(missing_instruments) | set(missing_field_instruments))
    rewritten = 0
    for inst in tqdm(affected_instruments, desc='增量重写受影响股票'):
        stock_data = df.xs(inst, level='instrument').copy()
        stock_data.attrs['instrument'] = inst
        result = _rewrite_one(stock_data, full_calendar, valid_h5_cols, full_calendar_index)
        if result is not None:
            instrument_book[result['instrument']] = (result['start_date'], result['end_date'])
            rewritten += 1
    merged_calendar = list(existing_calendar)
    for d in new_dates:
        if d not in merged_calendar:
            merged_calendar.append(d)
    atomic_write_text(CALENDAR_FILE, ''.join(f'{date}\n' for date in merged_calendar))
    atomic_write_text(
        INSTRUMENTS_FILE,
        ''.join(f'{inst}\t{instrument_book[inst][0]}\t{instrument_book[inst][1]}\n' for inst in sorted(instrument_book)),
    )
    return {'effective_mode': 'incremental', 'skipped': False, 'source_latest_date': source_latest, 'qlib_latest_date_before': qlib_latest, 'qlib_latest_date_after': merged_calendar[-1] if merged_calendar else None, 'new_dates': new_dates, 'missing_instrument_count': len(missing_instruments), 'missing_instruments': missing_instruments[:500], 'missing_field_instrument_count': len(missing_field_instruments), 'missing_field_examples': missing_field_examples, 'force_rewrite_reason': force_rewrite_reason, 'affected_instrument_count': len(affected_instruments), 'rewritten_instrument_count': rewritten, 'valid_field_count': len(valid_h5_cols), 'valid_fields': [FIELD_MAP[c] for c in valid_h5_cols], 'sealed_limit_contract_version': SEALED_LIMIT_CONTRACT_VERSION}


def _write_meta(summary: dict[str, Any]) -> None:
    atomic_write_json(META_FILE, summary)


def configure_paths(*, source_h5: str | None = None, output_dir: str | None = None, meta_path: str | None = None) -> None:
    global H5_FILE, OUTPUT_DIR, FEATURES_DIR, INSTRUMENTS_DIR, CALENDARS_DIR, CALENDAR_FILE, INSTRUMENTS_FILE, META_FILE
    if source_h5:
        H5_FILE = Path(source_h5).expanduser()
    if output_dir:
        OUTPUT_DIR = Path(output_dir).expanduser()
        FEATURES_DIR = OUTPUT_DIR / 'features'
        INSTRUMENTS_DIR = OUTPUT_DIR / 'instruments'
        CALENDARS_DIR = OUTPUT_DIR / 'calendars'
        CALENDAR_FILE = CALENDARS_DIR / 'day.txt'
        INSTRUMENTS_FILE = INSTRUMENTS_DIR / 'all.txt'
        META_FILE = OUTPUT_DIR / 'stock_converter_meta.json'
    if meta_path:
        META_FILE = Path(meta_path).expanduser()


def convert(mode: str = 'auto') -> dict[str, Any]:
    df, source_kind = _load_h5()
    requested_mode = mode
    if mode == 'auto':
        mode = 'incremental' if CALENDAR_FILE.exists() and INSTRUMENTS_FILE.exists() else 'full'
    result = _incremental_convert(df) if mode == 'incremental' else _full_convert(df)
    result.update({
        'kind': 'qlib_stock_converter',
        'mode': requested_mode,
        'source_kind': source_kind,
        'source_hdf': str(H5_FILE),
        'source_latest_date': _latest_source_date(df),
        'source_instrument_count': int(df.index.get_level_values('instrument').nunique()) if not df.empty else 0,
        'price_mode': 'adjusted_ohlc_plus_factor_for_qlib_exchange',
        'vwap_mode': 'adjusted_vwap_from_raw_amount_volume_times_factor',
        'chip_cost_mode': 'adjusted_chip_cost_from_cyq_perf_cost_lines_times_factor',
        'sealed_limit_mode': 'one_price_limit_and_relative_turnover_ratio',
        'sealed_limit_contract_version': SEALED_LIMIT_CONTRACT_VERSION,
        'sealed_turnover_ratio_threshold': SEALED_TURNOVER_RATIO_THRESHOLD,
        'raw_price_fields_retained': True,
        'generated_at': datetime.now().isoformat(),
        'meta_path': str(META_FILE),
        'output_dir': str(OUTPUT_DIR),
    })
    _write_meta(result)
    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Convert canonical Tushare raw HDF directly to Qlib format')
    p.add_argument('--mode', choices=['auto', 'incremental', 'full'], default='auto')
    p.add_argument('--source-h5')
    p.add_argument('--output-dir')
    p.add_argument('--meta-path')
    p.add_argument('--json', action='store_true')
    return p.parse_args()


def main() -> None:
    args = parse_args()
    ensure_repo_data_dirs()
    configure_paths(source_h5=args.source_h5, output_dir=args.output_dir, meta_path=args.meta_path)
    summary = convert(mode=args.mode)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
