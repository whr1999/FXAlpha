from __future__ import annotations

import logging
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow.parquet as pq

from domain.data_foundation.runtime_io import atomic_write_json
from storage.paths import PRODUCTION_RAW_HDF5, QUANTGPT_BENCHMARK_DIR, QUANTGPT_DATA_DIR

logger = logging.getLogger(__name__)

FIELD_MAP = {
    'open': ['adj_open', 'open'],
    'high': ['adj_high', 'high'],
    'low': ['adj_low', 'low'],
    'close': ['adj_close', 'close'],
    'up_limit': ['up_limit'],
    'down_limit': ['down_limit'],
    'volume': ['volume'],
    'amount': ['amount'],
    'pct_change': ['adj_pct_chg', 'pct_chg'],
    'pre_close': ['adj_pre_close', 'pre_close'],
    'pe': ['PE'],
    'pb': ['PB'],
    'ps_ttm': ['ps_ttm'],
    'dv_ttm': ['dv_ttm'],
    'roe': ['ROE'],
    'roa': ['ROA'],
    'total_mv': ['total_mv'],
    'float_mv': ['float_mv'],
    'turnover_rate': ['turnover_rate'],
    'turnover_rate_f': ['turnover_rate_f'],
    'eps': ['EPS'],
    'net_profit': ['NET_PROFIT'],
    'tot_equity': ['TOT_EQUITY'],
    'total_assets': ['TOTAL_ASSETS'],
    'net_asset_ps': ['NET_ASSET_PS'],
    'tot_share': ['TOT_SHARE'],
    'float_a_share': ['FLOAT_A_SHARE'],
    'free_share': ['free_share'],
    'holder_num': ['HOLDER_NUM'],
    'amp': ['adj_amp', 'amp'],
    'security_name': ['SECURITY_NAME'],
    'list_status': ['list_status'],
    'st_status': ['st_status'],
    'list_date': ['LIST_DATE'],
    'borrow_money_bal': ['BORROW_MONEY_BAL'],
    'purch_borrow_money': ['PURCH_BORROW_MONEY'],
    'sec_lending_bal': ['SEC_LENDING_BAL'],
    'margin_trade_bal': ['MARGIN_TRADE_BAL'],
    'margin_buy_amount': ['margin_buy_amount'],
    'margin_balance': ['margin_balance'],
    'short_balance': ['short_balance'],
    'sm_net_vol': ['sm_net_vol'],
    'sm_net_amount': ['sm_net_amount'],
    'lg_net_vol': ['lg_net_vol'],
    'lg_net_amount': ['lg_net_amount'],
    'net_mf_vol': ['net_mf_vol'],
    'net_mf_amount': ['net_mf_amount'],
    'cost_15pct': ['cost_15pct'],
    'cost_85pct': ['cost_85pct'],
    'weight_avg': ['weight_avg'],
    'backward_factor': ['backward_factor'],
}

CHIP_COST_RAW_FIELDS = ('cost_15pct', 'cost_85pct', 'weight_avg')
TEXT_FIELDS = {'stock_code', 'security_name', 'list_status', 'st_status', 'list_date'}
DATA_CONTRACT_VERSION = 'quantgpt_adjusted_price_v3_explicit_vwap_chip_cost'
CONTRACT_FILE = '_conversion_contract.json'
REQUIRED_COLUMNS = sorted(set(FIELD_MAP) | {'vwap'})
FIELD_SEMANTICS = {
    'open': 'HFQ adjusted price copied from adj_open when available.',
    'high': 'HFQ adjusted price copied from adj_high when available.',
    'low': 'HFQ adjusted price copied from adj_low when available.',
    'close': 'HFQ adjusted price copied from adj_close when available.',
    'pre_close': 'HFQ adjusted previous close copied from adj_pre_close when available.',
    'vwap': 'HFQ adjusted VWAP derived once in conversion: amount(thousand CNY) * 10 / volume(hand) * backward_factor.',
    'cost_15pct': 'HFQ adjusted chip cost line: Tushare cyq_perf raw cost_15pct * backward_factor.',
    'cost_85pct': 'HFQ adjusted chip cost line: Tushare cyq_perf raw cost_85pct * backward_factor.',
    'weight_avg': 'HFQ adjusted chip weighted average cost: Tushare cyq_perf raw weight_avg * backward_factor.',
    'amount': 'Tushare daily amount, unit thousand CNY; retained for liquidity expressions, not a price field.',
    'volume': 'Tushare daily vol renamed to volume, unit hand.',
    'up_limit': 'Official raw upper limit price from Tushare stk_limit; audit/tradability only.',
    'down_limit': 'Official raw lower limit price from Tushare stk_limit; audit/tradability only.',
}

BENCHMARK_CODES = {
    '000300.SH': 'benchmark_hs300.parquet',
    '000905.SH': 'benchmark_csi500.parquet',
    '000852.SH': 'benchmark_csi1000.parquet',
}


def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.unlink(missing_ok=True)
    try:
        frame.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _truncate_parquet_window(root: Path, replace_from_ts: pd.Timestamp) -> dict[str, int]:
    scanned = 0
    rewritten = 0
    removed_rows = 0
    for parquet_path in sorted(root.glob('*.parquet')):
        scanned += 1
        existing = pd.read_parquet(parquet_path)
        if 'trade_date' not in existing.columns:
            raise RuntimeError(f'quantgpt_trade_date_missing:{parquet_path}')
        trade_dates = pd.to_datetime(existing['trade_date'], errors='coerce')
        keep = trade_dates.lt(replace_from_ts)
        removed = int((~keep).sum())
        if not removed:
            continue
        trimmed = existing.loc[keep].reset_index(drop=True)
        _atomic_write_parquet(trimmed, parquet_path)
        rewritten += 1
        removed_rows += removed
    return {'files_scanned': scanned, 'files_rewritten': rewritten, 'rows_removed': removed_rows}


def _map_code(amazing_code: str) -> str:
    code = str(amazing_code).strip()
    if '.' not in code:
        return code
    num, market = code.split('.')
    return f'{market.lower()}.{num}'


def _to_quantgpt_frame(stock_df: pd.DataFrame, am_code: str) -> pd.DataFrame:
    stock_df = stock_df.reset_index(drop=True)
    out = pd.DataFrame()
    out['trade_date'] = pd.to_datetime(stock_df['kline_time'])
    out['stock_code'] = _map_code(am_code)
    for dst, candidates in FIELD_MAP.items():
        source = next((src for src in candidates if src in stock_df.columns), None)
        if source is not None:
            out[dst] = stock_df[source]
    _apply_adjusted_research_price_fields(out)
    out = out[out['trade_date'].notna()].sort_values('trade_date').reset_index(drop=True)
    return _normalize_quantgpt_output_frame(out)


def _normalize_quantgpt_output_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep persisted QuantGPT parquet dtypes stable across full and delta writes."""
    out = frame.copy()
    if 'trade_date' in out.columns:
        out['trade_date'] = pd.to_datetime(out['trade_date'], errors='coerce')
    for field in TEXT_FIELDS:
        if field in out.columns:
            out[field] = out[field].astype('string')
    for field in out.columns:
        if field in TEXT_FIELDS or field == 'trade_date':
            continue
        out[field] = pd.to_numeric(out[field], errors='coerce')
    return out


def _apply_adjusted_research_price_fields(out: pd.DataFrame) -> None:
    """Materialize derived QuantGPT research-price fields in one audited place.

    Tushare daily stores ``amount`` in thousand CNY and ``vol``/``volume`` in
    hands, so raw VWAP in CNY/share is ``amount * 1000 / (volume * 100)``.
    QuantGPT consumes adjusted research prices, therefore VWAP and chip cost
    lines are multiplied by ``backward_factor`` exactly once during conversion.
    """
    factor = pd.to_numeric(out.get('backward_factor'), errors='coerce')
    if factor is None:
        return
    for field in CHIP_COST_RAW_FIELDS:
        if field in out.columns:
            out[field] = pd.to_numeric(out[field], errors='coerce') * factor
    if {'amount', 'volume'} <= set(out.columns):
        amount = pd.to_numeric(out['amount'], errors='coerce')
        volume = pd.to_numeric(out['volume'], errors='coerce').replace(0, pd.NA)
        out['vwap'] = (amount * 10.0 / volume) * factor


def _parquet_latest_date(parquet_path: Path) -> str | None:
    try:
        df = pd.read_parquet(parquet_path, columns=['trade_date'])
        if df.empty:
            return None
        return pd.to_datetime(df['trade_date']).max().date().isoformat()
    except Exception:
        return None


def _contract_path(output_dir: Path) -> Path:
    return output_dir / CONTRACT_FILE


def _write_contract(output_dir: Path) -> None:
    payload = {
        'data_contract_version': DATA_CONTRACT_VERSION,
        'price_mode': 'adjusted_from_adj_fields_with_adjusted_vwap_and_chip_cost',
        'required_columns': REQUIRED_COLUMNS,
        'field_semantics': FIELD_SEMANTICS,
        'derived_fields': {
            'vwap': 'amount(thousand CNY) * 10 / volume(hand) * backward_factor',
            'cost_15pct': 'cyq_perf.cost_15pct * backward_factor',
            'cost_85pct': 'cyq_perf.cost_85pct * backward_factor',
            'weight_avg': 'cyq_perf.weight_avg * backward_factor',
        },
        'forbidden_runtime_fallbacks': [
            'Do not derive vwap inside QuantGPT evaluator, Rust bridge, factor_compute, or market_data fetchers.',
            'Do not apply backward_factor again inside factor expressions or model feature snapshots.',
        ],
    }
    atomic_write_json(_contract_path(output_dir), payload)


def _contract_is_current(output_dir: Path) -> bool:
    path = _contract_path(output_dir)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return False
    return (
        payload.get('data_contract_version') == DATA_CONTRACT_VERSION
        and set(payload.get('required_columns') or []) >= set(REQUIRED_COLUMNS)
    )


def _parquet_has_required_schema(parquet_path: Path) -> bool:
    try:
        columns = set(pq.ParquetFile(parquet_path).schema_arrow.names)
    except Exception:
        return False
    return set(REQUIRED_COLUMNS).issubset(columns)


def quantgpt_contract_report(output_dir: Path, *, sample_limit: int | None = 50) -> dict:
    output_dir = Path(output_dir).expanduser()
    contract_path = _contract_path(output_dir)
    payload: dict = {}
    if contract_path.exists():
        try:
            payload = json.loads(contract_path.read_text(encoding='utf-8'))
        except Exception as exc:
            payload = {'read_error': str(exc)}

    files = sorted(output_dir.glob('*.parquet')) if output_dir.exists() else []
    sampled = files if sample_limit is None else files[: max(0, int(sample_limit))]
    bad_examples: list[dict] = []
    for parquet_file in sampled:
        try:
            columns = set(pq.ParquetFile(parquet_file).schema_arrow.names)
            missing = sorted(set(REQUIRED_COLUMNS) - columns)
        except Exception as exc:
            missing = [f'read_error:{exc}']
        if missing:
            bad_examples.append({'file': parquet_file.name, 'missing_columns': missing[:20]})
            if len(bad_examples) >= 10:
                break

    contract_current = _contract_is_current(output_dir)
    schema_ok = not bad_examples and bool(files)
    return {
        'ok': bool(contract_current and schema_ok),
        'data_contract_version': payload.get('data_contract_version'),
        'expected_contract_version': DATA_CONTRACT_VERSION,
        'contract_file': str(contract_path),
        'contract_exists': contract_path.exists(),
        'contract_current': contract_current,
        'stock_file_count': len(files),
        'sampled_file_count': len(sampled),
        'schema_sample_ok': schema_ok,
        'bad_schema_examples': bad_examples,
        'required_columns': REQUIRED_COLUMNS,
    }


def _quantgpt_to_amazing_code(quantgpt_code: str) -> str | None:
    try:
        market, num = str(quantgpt_code).split('.', 1)
    except ValueError:
        return None
    if not market or not num:
        return None
    return f'{num}.{market.upper()}'


def convert(
    hdf5_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    benchmark_dir: Optional[Path] = None,
    codes: Optional[list[str]] = None,
) -> dict:
    hdf5_path = Path(hdf5_path or PRODUCTION_RAW_HDF5).expanduser()
    output_dir = Path(output_dir or QUANTGPT_DATA_DIR).expanduser()
    benchmark_dir = Path(benchmark_dir or QUANTGPT_BENCHMARK_DIR).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    benchmark_dir.mkdir(parents=True, exist_ok=True)

    if not hdf5_path.exists():
        return {'status': 'failed', 'error': f'HDF5 not found: {hdf5_path}'}

    t0 = time.time()
    df = pd.read_hdf(hdf5_path, key='/daily').reset_index()
    df['code'] = df['code'].astype(str).str.strip()
    if 'list_status' in df.columns:
        df['list_status'] = df['list_status'].astype(str).str.strip()
        index_codes = set(df.loc[df['list_status'].eq('I'), 'code'].dropna().unique())
    else:
        index_codes = set()

    if codes:
        df = df[df['code'].isin(codes)]
        if df.empty:
            return {'status': 'failed', 'error': 'No matching codes found'}
        index_codes &= set(df['code'].unique())

    all_codes = sorted(df['code'].unique())
    stock_codes = [code for code in all_codes if code not in BENCHMARK_CODES and code not in index_codes]
    updated = 0
    errors = 0
    failed_codes: list[dict] = []
    benchmark_updated = 0
    latest_dates: list[pd.Timestamp] = []

    for am_code in all_codes:
        stock_df = df[df['code'] == am_code].copy()
        if stock_df.empty:
            continue
        try:
            out = _to_quantgpt_frame(stock_df, am_code)
            if out.empty:
                continue
            latest_dates.append(out['trade_date'].max())
            benchmark_name = BENCHMARK_CODES.get(am_code)
            if am_code in index_codes and benchmark_name is None:
                continue
            if benchmark_name is None:
                parquet_path = output_dir / f"{out['stock_code'].iloc[0].replace('.', '_')}.parquet"
                _atomic_write_parquet(_normalize_quantgpt_output_frame(out), parquet_path)
                updated += 1
            else:
                _atomic_write_parquet(_normalize_quantgpt_output_frame(out), benchmark_dir / benchmark_name)
                benchmark_updated += 1
        except Exception as exc:
            errors += 1
            failed_codes.append({'code': am_code, 'error': str(exc)[:500]})
            logger.exception('quantgpt_convert_code_failed code=%s', am_code)

    latest_date = max(latest_dates).strftime('%Y-%m-%d') if latest_dates else None
    if updated or benchmark_updated:
        _write_contract(output_dir)
    return {
        'status': 'failed' if errors else 'completed',
        'total_stocks': len(stock_codes),
        'updated': updated,
        'errors': errors,
        'latest_date': latest_date,
        'duration_seconds': round(time.time() - t0, 1),
        'output_dir': str(output_dir),
        'benchmark_dir': str(benchmark_dir),
        'benchmark_updated': benchmark_updated,
        'failed_codes': failed_codes[:20],
        'price_mode': 'adjusted_from_adj_fields_with_adjusted_vwap_and_chip_cost',
        'data_contract_version': DATA_CONTRACT_VERSION,
    }


def convert_incremental(
    hdf5_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    benchmark_dir: Optional[Path] = None,
) -> dict:
    hdf5_path = Path(hdf5_path or PRODUCTION_RAW_HDF5).expanduser()
    output_dir = Path(output_dir or QUANTGPT_DATA_DIR).expanduser()

    if not hdf5_path.exists():
        return {'status': 'failed', 'error': 'HDF5 not found'}

    hdf5_df = pd.read_hdf(hdf5_path, key='/daily')
    hdf5_df['code'] = hdf5_df['code'].astype(str).str.strip()
    hdf5_latest_date = pd.to_datetime(hdf5_df['kline_time']).max().date().isoformat()

    parquet_codes = set()
    stale_codes: list[str] = []
    if output_dir.exists():
        for parquet_file in output_dir.glob('*.parquet'):
            code = parquet_file.stem.replace('_', '.', 1)
            parquet_codes.add(code)
            if not _parquet_has_required_schema(parquet_file) or (_parquet_latest_date(parquet_file) or '') < hdf5_latest_date:
                stale_codes.append(code)

    if not parquet_codes or not _contract_is_current(output_dir):
        result = convert(hdf5_path, output_dir, benchmark_dir=benchmark_dir)
        result['mode'] = 'full_from_incremental' if not parquet_codes else 'full_from_contract_mismatch'
        return result

    hdf5_codes = sorted(hdf5_df['code'].unique())
    missing_codes = [
        code for code in hdf5_codes
        if _map_code(code) not in parquet_codes
    ]
    codes_to_update = sorted(set(stale_codes))

    if not codes_to_update and not missing_codes:
        return {
            'status': 'up_to_date',
            'total_stocks': len(parquet_codes),
            'new_codes_created': 0,
            'stale_codes_updated': 0,
            'codes_updated': 0,
            'hdf5_latest_date': hdf5_latest_date,
        }

    h5_codes = []
    for qc in codes_to_update:
        amazing_code = _quantgpt_to_amazing_code(qc)
        if amazing_code:
            h5_codes.append(amazing_code)
    h5_codes.extend(missing_codes)

    result = convert(hdf5_path, output_dir, benchmark_dir=benchmark_dir, codes=sorted(set(h5_codes)))
    result['mode'] = 'incremental'
    result['new_codes_created'] = len(missing_codes)
    result['stale_codes_updated'] = len(codes_to_update)
    result['codes_updated'] = len(set(h5_codes))
    result['parquet_total'] = len(parquet_codes)
    result['hdf5_latest_date'] = hdf5_latest_date
    return result


def convert_incremental_from_delta(
    delta_hdf5_path: Path,
    output_dir: Path,
    benchmark_dir: Path,
    *,
    replace_from_date: str,
    seed_output_dir: Optional[Path] = None,
    seed_benchmark_dir: Optional[Path] = None,
) -> dict:
    delta_hdf5_path = Path(delta_hdf5_path).expanduser()
    output_dir = Path(output_dir).expanduser()
    benchmark_dir = Path(benchmark_dir).expanduser()
    seed_output_dir = Path(seed_output_dir).expanduser() if seed_output_dir else None
    seed_benchmark_dir = Path(seed_benchmark_dir).expanduser() if seed_benchmark_dir else None

    if not delta_hdf5_path.exists():
        return {'status': 'failed', 'error': f'delta HDF5 not found: {delta_hdf5_path}'}

    replace_from_ts = pd.Timestamp(str(replace_from_date).replace('-', ''))
    t0 = time.time()

    shutil.rmtree(output_dir, ignore_errors=True)
    shutil.rmtree(benchmark_dir, ignore_errors=True)
    if seed_output_dir and seed_output_dir.exists():
        seed_report = quantgpt_contract_report(seed_output_dir, sample_limit=None)
        if not seed_report.get('ok'):
            raise RuntimeError(
                'quantgpt_seed_contract_mismatch: '
                f"contract={seed_report.get('data_contract_version')}, "
                f"expected={DATA_CONTRACT_VERSION}, "
                f"examples={seed_report.get('bad_schema_examples')}"
            )
        shutil.copytree(seed_output_dir, output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
    if seed_benchmark_dir and seed_benchmark_dir.exists():
        shutil.copytree(seed_benchmark_dir, benchmark_dir)
    else:
        benchmark_dir.mkdir(parents=True, exist_ok=True)

    stock_truncate = _truncate_parquet_window(output_dir, replace_from_ts)
    benchmark_truncate = _truncate_parquet_window(benchmark_dir, replace_from_ts)

    delta_df = pd.read_hdf(delta_hdf5_path, key='/daily').reset_index()
    if delta_df.empty:
        _write_contract(output_dir)
        return {
            'status': 'completed',
            'mode': 'incremental_from_delta',
            'codes_updated': 0,
            'benchmark_updated': 0,
            'latest_date': None,
            'duration_seconds': round(time.time() - t0, 1),
            'output_dir': str(output_dir),
            'benchmark_dir': str(benchmark_dir),
            'price_mode': 'adjusted_from_adj_fields_with_adjusted_vwap_and_chip_cost',
            'data_contract_version': DATA_CONTRACT_VERSION,
            'stock_window_truncate': stock_truncate,
            'benchmark_window_truncate': benchmark_truncate,
        }

    delta_df['code'] = delta_df['code'].astype(str).str.strip()
    if 'list_status' in delta_df.columns:
        delta_df['list_status'] = delta_df['list_status'].astype(str).str.strip()
        index_codes = set(delta_df.loc[delta_df['list_status'].eq('I'), 'code'].dropna().unique())
    else:
        index_codes = set()

    latest_dates: list[pd.Timestamp] = []
    updated = 0
    benchmark_updated = 0
    errors = 0
    failed_codes: list[dict] = []

    for am_code in sorted(delta_df['code'].unique()):
        stock_df = delta_df[delta_df['code'] == am_code].copy()
        if stock_df.empty:
            continue
        try:
            out = _to_quantgpt_frame(stock_df, am_code)
            if out.empty:
                continue
            latest_dates.append(out['trade_date'].max())
            benchmark_name = BENCHMARK_CODES.get(am_code)
            if am_code in index_codes and benchmark_name is None:
                continue
            if benchmark_name is None:
                parquet_path = output_dir / f"{out['stock_code'].iloc[0].replace('.', '_')}.parquet"
            else:
                parquet_path = benchmark_dir / benchmark_name

            if parquet_path.exists():
                existing = pd.read_parquet(parquet_path)
                existing['trade_date'] = pd.to_datetime(existing['trade_date'])
                merged = pd.concat([existing, out], ignore_index=True)
            else:
                merged = out
            merged = (
                merged.sort_values('trade_date')
                .drop_duplicates(subset=['trade_date'], keep='last')
                .reset_index(drop=True)
            )
            merged = _normalize_quantgpt_output_frame(merged)
            _atomic_write_parquet(merged, parquet_path)
            if benchmark_name is None:
                updated += 1
            else:
                benchmark_updated += 1
        except Exception as exc:
            errors += 1
            failed_codes.append({'code': am_code, 'error': str(exc)[:500]})
            logger.exception('quantgpt_incremental_code_failed code=%s', am_code)

    latest_date = max(latest_dates).strftime('%Y-%m-%d') if latest_dates else None
    if updated or benchmark_updated or stock_truncate['files_rewritten'] or benchmark_truncate['files_rewritten']:
        _write_contract(output_dir)
    return {
        'status': 'failed' if errors else 'completed',
        'mode': 'incremental_from_delta',
        'codes_updated': updated,
        'benchmark_updated': benchmark_updated,
        'errors': errors,
        'failed_codes': failed_codes[:20],
        'latest_date': latest_date,
        'duration_seconds': round(time.time() - t0, 1),
        'output_dir': str(output_dir),
        'benchmark_dir': str(benchmark_dir),
        'replace_from_date': str(replace_from_ts.date()),
        'price_mode': 'adjusted_from_adj_fields_with_adjusted_vwap_and_chip_cost',
        'data_contract_version': DATA_CONTRACT_VERSION,
        'stock_window_truncate': stock_truncate,
        'benchmark_window_truncate': benchmark_truncate,
    }
