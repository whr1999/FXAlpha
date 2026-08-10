from __future__ import annotations

from pathlib import Path
from typing import Optional
import json

import numpy as np
import pandas as pd

from storage.paths import PRODUCTION_RAW_HDF5


FIELD_GROUPS = {
    'identity_fields': {
        'fields': ['code', 'kline_time', 'SECURITY_NAME', 'MARKET_CODE', 'list_status', 'st_status'],
        'max_missing_pct': 0.0,
    },
    'market_core_fields': {
        'fields': ['open', 'high', 'low', 'close', 'volume', 'amount', 'pre_close'],
        'max_missing_pct': 0.02,
    },
    'factor_adjusted_fields': {
        'fields': ['backward_factor', 'adj_open', 'adj_high', 'adj_low', 'adj_close', 'adj_pre_close', 'adj_pct_chg', 'adj_amp'],
        'max_missing_pct': 0.02,
    },
    'valuation_and_fundamental_fields': {
        'fields': [
            'LIST_DATE',
            'TOT_SHARE',
            'FLOAT_A_SHARE',
            'EPS',
            'NET_PROFIT',
            'TOT_EQUITY',
            'TOTAL_ASSETS',
            'NET_ASSET_PS',
            'HOLDER_NUM',
            'PE',
            'PB',
            'ROE',
            'ROA',
            'total_mv',
            'float_mv',
            'turnover_rate',
        ],
        'max_missing_pct': 0.05,
    },
    'margin_fields': {
        'fields': ['BORROW_MONEY_BAL', 'PURCH_BORROW_MONEY', 'SEC_LENDING_BAL', 'MARGIN_TRADE_BAL'],
        'max_missing_pct': 0.60,
    },
}

DAILY_FIELD_COVERAGE_GROUPS = {
    'market_core_daily_coverage': {
        'fields': ['open', 'high', 'low', 'close', 'volume', 'amount', 'pre_close'],
        'min_daily_coverage_ratio': 0.98,
        'max_zero_coverage_days': 0,
        'allow_first_day_null_fields': ['pre_close'],
    },
    'adjusted_price_daily_coverage': {
        'fields': ['backward_factor', 'adj_open', 'adj_high', 'adj_low', 'adj_close', 'adj_pre_close'],
        'min_daily_coverage_ratio': 0.98,
        'max_zero_coverage_days': 0,
        'allow_first_day_null_fields': ['adj_pre_close'],
    },
    'pit_fundamental_daily_coverage': {
        'fields': [
            'EPS', 'NET_PROFIT', 'TOT_EQUITY', 'TOTAL_ASSETS', 'NET_ASSET_PS',
            'TOT_SHARE', 'FLOAT_A_SHARE', 'HOLDER_NUM', 'PE', 'PB', 'ROE', 'ROA',
        ],
        'min_daily_coverage_ratio': 0.30,
        'max_zero_coverage_days': 0,
    },
}
REQUIRED_BENCHMARK_INDEX_CODES = ['000300.SH', '000905.SH', '000852.SH']
OPTIONAL_INDEX_CODES = ['000001.SH', '000016.SH', '399001.SZ', '399006.SZ']
INDEX_CODES = set(REQUIRED_BENCHMARK_INDEX_CODES + OPTIONAL_INDEX_CODES)
CORE_MARKET_FIELDS = ['open', 'high', 'low', 'close', 'volume', 'amount']
DAILY_COMPAT_FIELDS = [
    'code', 'kline_time', 'LIST_DATE', 'list_status', 'st_status', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pre_close',
    'backward_factor', 'adj_open', 'adj_high', 'adj_low', 'adj_close', 'adj_pre_close',
]


def _latest_code_activity(df: pd.DataFrame) -> dict:
    if df.empty or 'code' not in df.columns:
        return {}
    trade_dates = pd.to_datetime(df['kline_time'] if 'kline_time' in df.columns else df.index, errors='coerce')
    code_activity = (
        pd.DataFrame({'code': df['code'].astype(str).to_numpy(), 'trade_date': pd.Series(trade_dates, copy=False).to_numpy()})
        .dropna(subset=['trade_date'])
        .reset_index(drop=True)
        .sort_values(['code', 'trade_date'])
        .groupby('code', sort=False)['trade_date']
        .max()
        .rename('last_trade_date')
        .reset_index()
    )
    if code_activity.empty:
        return {}
    latest_trade_date = pd.Timestamp(code_activity['last_trade_date'].max()).normalize()
    code_activity['calendar_gap_days'] = (latest_trade_date - code_activity['last_trade_date'].dt.normalize()).dt.days.astype(int)
    code_activity['is_index'] = code_activity['code'].isin(INDEX_CODES)
    stale = code_activity[code_activity['calendar_gap_days'] > 0].copy()
    stale['last_trade_date'] = stale['last_trade_date'].dt.strftime('%Y-%m-%d')
    stock_activity = code_activity[~code_activity['is_index']]
    stale_stock = stale[~stale['is_index']]
    recent_stale = stale_stock[stale_stock['calendar_gap_days'] <= 7]
    long_stale = stale_stock[stale_stock['calendar_gap_days'] > 7]
    return {
        'latest_trade_date': str(latest_trade_date.date()),
        'code_count': int(len(code_activity)),
        'stock_code_count': int(len(stock_activity)),
        'latest_day_code_count': int((code_activity['calendar_gap_days'] == 0).sum()),
        'latest_day_stock_count': int(((code_activity['calendar_gap_days'] == 0) & ~code_activity['is_index']).sum()),
        'stale_code_count': int(len(stale)),
        'stale_stock_count': int(len(stale_stock)),
        'recent_stale_stock_count': int(len(recent_stale)),
        'long_stale_stock_count': int(len(long_stale)),
        'stale_codes': stale_stock['code'].tolist()[:100],
        'stale_examples': stale_stock[['code', 'last_trade_date', 'calendar_gap_days']].head(20).to_dict(orient='records'),
    }


def _metadata_quality(hdf5_path: Path, latest_code_activity: dict | None = None) -> dict:
    meta_path = hdf5_path.with_name('metadata.json')
    if not meta_path.exists():
        return {'present': False, 'issues': [], 'warnings': ['metadata.json not found next to HDF5'], 'metadata': {}}
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
    except Exception as exc:
        return {'present': False, 'issues': [f'Cannot read metadata.json: {exc}'], 'warnings': [], 'metadata': {}}
    quality = meta.get('last_update_quality') or {}
    issues: list[str] = []
    warnings: list[str] = []
    expected = int(quality.get('expected_stock_count') or 0)
    processed = int(quality.get('processed_code_count') or 0)
    latest_count = int(quality.get('latest_day_stock_count') or 0)
    missing_final = int(quality.get('kline_missing_final_count') or 0)
    process_failures = int(quality.get('process_failure_count') or 0)
    historical_window = bool(quality.get('historical_window'))
    derived_latest_stock_count = int((latest_code_activity or {}).get('latest_day_stock_count') or 0)
    stale_codes = (latest_code_activity or {}).get('stale_codes') or []
    if expected:
        if processed != expected:
            target = warnings if historical_window else issues
            target.append(f'processed_code_count {processed} != expected_stock_count {expected}')
        if latest_count != expected:
            target = warnings if historical_window else issues
            detail = f'latest_day_stock_count {latest_count} != expected_stock_count {expected}'
            if derived_latest_stock_count:
                detail += f' (derived latest_day_stock_count={derived_latest_stock_count})'
            target.append(detail)
    if missing_final:
        issues.append(f'kline_missing_final_count {missing_final} > 0')
    if process_failures:
        issues.append(f'process_failure_count {process_failures} > 0')
    missing_latest = quality.get('latest_day_missing_codes') or []
    if missing_latest:
        target = warnings if historical_window else issues
        detail = f'latest_day_missing_codes not empty: {missing_latest[:20]}'
        if stale_codes:
            detail += f' ; derived stale_codes sample: {stale_codes[:20]}'
        target.append(detail)
    if historical_window:
        warnings.append('metadata latest-day counts come from historical-window raw build snapshots and are not used as hard quality gates')
    return {'present': True, 'quality': quality, 'issues': issues, 'warnings': warnings, 'metadata': meta}


def _benchmark_index_quality(df: pd.DataFrame) -> dict:
    checks = []
    issues = []
    if 'code' not in df.columns or df.empty:
        return {'checks': checks, 'issues': ['benchmark index check requires non-empty code column']}
    latest = pd.Timestamp(df.index.max()).normalize()
    for code in REQUIRED_BENCHMARK_INDEX_CODES:
        subset = df[df['code'] == code]
        check = {'code': code, 'present': not subset.empty, 'latest_date': None, 'core_nulls': {}}
        if subset.empty:
            issues.append(f'benchmark index missing: {code}')
            checks.append(check)
            continue
        code_latest = pd.Timestamp(subset.index.max()).normalize()
        check['latest_date'] = str(code_latest.date())
        if code_latest != latest:
            issues.append(f'benchmark index {code} latest {code_latest.date()} != HDF5 latest {latest.date()}')
        latest_rows = subset[pd.to_datetime(subset.index).normalize() == code_latest]
        for field in CORE_MARKET_FIELDS:
            if field not in latest_rows.columns:
                check['core_nulls'][field] = 'missing_field'
                issues.append(f'benchmark index {code} missing core field: {field}')
            else:
                null_count = int(latest_rows[field].isna().sum())
                check['core_nulls'][field] = null_count
                if null_count:
                    issues.append(f'benchmark index {code} latest has null {field}: {null_count}')
        checks.append(check)
    return {'checks': checks, 'issues': issues, 'hdf5_latest_date': str(latest.date())}


def _daily_field_coverage(df: pd.DataFrame) -> dict:
    if df.empty or 'kline_time' not in df.columns:
        return {}
    dates = pd.to_datetime(df['kline_time']).dt.normalize()
    total_by_date = dates.groupby(dates).size()
    first_date = dates.min()
    checks: dict[str, dict] = {}
    for group_name, group_def in DAILY_FIELD_COVERAGE_GROUPS.items():
        expected_fields = list(group_def['fields'])
        allow_first_day_null_fields = set(group_def.get('allow_first_day_null_fields') or [])
        present_fields = [field for field in expected_fields if field in df.columns]
        missing_fields = [field for field in expected_fields if field not in df.columns]
        field_checks: dict[str, dict] = {}
        zero_coverage_days: set[str] = set()
        low_coverage_days: set[str] = set()
        worst_daily_coverage = 1.0
        worst_field = None

        for field in present_fields:
            valid_counts = df[field].notna().groupby(dates).sum()
            coverage = (valid_counts / total_by_date).fillna(0.0)
            if field in allow_first_day_null_fields and pd.notna(first_date):
                valid_counts = valid_counts[valid_counts.index != first_date]
                coverage = coverage[coverage.index != first_date]
            zero_days = [str(day.date()) for day, value in valid_counts.items() if int(value) == 0]
            low_days = [str(day.date()) for day, value in coverage.items() if float(value) < float(group_def['min_daily_coverage_ratio'])]
            field_min = float(coverage.min()) if not coverage.empty else 0.0
            if field_min < worst_daily_coverage:
                worst_daily_coverage = field_min
                worst_field = field
            zero_coverage_days.update(zero_days)
            low_coverage_days.update(low_days)
            field_checks[field] = {
                'min_daily_coverage_ratio': field_min,
                'zero_coverage_days': zero_days[:20],
                'zero_coverage_day_count': len(zero_days),
                'low_coverage_days': low_days[:20],
                'low_coverage_day_count': len(low_days),
            }

        checks[group_name] = {
            'present_fields': present_fields,
            'missing_fields': missing_fields,
            'field_checks': field_checks,
            'worst_field': worst_field,
            'worst_daily_coverage_ratio': worst_daily_coverage if present_fields else 0.0,
            'zero_coverage_days': sorted(zero_coverage_days)[:50],
            'zero_coverage_day_count': len(zero_coverage_days),
            'low_coverage_days': sorted(low_coverage_days)[:50],
            'low_coverage_day_count': len(low_coverage_days),
            'min_daily_coverage_ratio': group_def['min_daily_coverage_ratio'],
            'max_zero_coverage_days': group_def['max_zero_coverage_days'],
        }
    return checks


def _limit_price_quality(df: pd.DataFrame) -> dict:
    required = ['up_limit', 'down_limit']
    missing_fields = [field for field in required if field not in df.columns]
    if missing_fields:
        return {
            'present': False,
            'passed': False,
            'missing_fields': missing_fields,
            'issues': [f"limit_price_missing_fields:{','.join(missing_fields)}"],
            'warnings': [],
        }
    if 'list_status' in df.columns:
        stock_mask = ~df['list_status'].astype(str).eq('I')
    elif 'code' in df.columns:
        stock_mask = ~df['code'].astype(str).isin(INDEX_CODES)
    else:
        stock_mask = pd.Series(True, index=df.index)
    stock = df.loc[stock_mask].copy()
    official = stock['up_limit'].notna() & stock['down_limit'].notna()
    structural = (
        stock['limit_source_kind'].astype(str).eq('structural_no_limit')
        if 'limit_source_kind' in stock.columns
        else pd.Series(False, index=stock.index)
    )
    missing = ~(official | structural)
    missing_rows = stock.loc[missing, ['code', 'kline_time']].copy() if {'code', 'kline_time'}.issubset(stock.columns) else pd.DataFrame()
    if not missing_rows.empty:
        missing_rows['trade_date'] = pd.to_datetime(missing_rows['kline_time'], errors='coerce').dt.strftime('%Y%m%d')
    issues = []
    if int(missing.sum()):
        issues.append(
            f"limit_price_coverage_gap:missing_rows={int(missing.sum())}:"
            f"missing_codes={int(missing_rows['code'].nunique()) if not missing_rows.empty else 0}"
        )
    warnings = []
    if 'limit_source_kind' not in stock.columns:
        warnings.append('limit_source_kind_missing_structural_no_limit_cannot_be_distinguished')
    return {
        'present': True,
        'passed': not issues,
        'stock_row_count': int(len(stock)),
        'official_row_count': int(official.sum()),
        'structural_no_limit_row_count': int(structural.sum()),
        'missing_row_count': int(missing.sum()),
        'coverage_ratio': float((official | structural).mean()) if len(stock) else 1.0,
        'missing_code_samples': sorted(missing_rows['code'].astype(str).unique().tolist())[:10] if not missing_rows.empty else [],
        'missing_date_samples': (
            [f"{idx}:{int(value)}" for idx, value in missing_rows.groupby('trade_date')['code'].nunique().sort_values(ascending=False).head(10).items()]
            if not missing_rows.empty
            else []
        ),
        'issues': issues,
        'warnings': warnings,
    }


def _factor_adjusted_quality(df: pd.DataFrame) -> dict:
    required = ['code', 'close', 'backward_factor', 'adj_close', 'adj_open', 'adj_high', 'adj_low', 'adj_pre_close']
    missing = [field for field in required if field not in df.columns]
    if missing:
        return {'present': False, 'issues': [f'factor_adjusted_quality missing fields: {missing}'], 'warnings': []}
    return _factor_adjusted_quality_with_meta(df, meta={})


def _legacy_adjusted_compat_mode(meta: dict) -> bool:
    return (
        str(meta.get('schema_version') or '') == 'tushare_v1'
        and str(meta.get('price_mode') or '') == 'raw_with_legacy_adjusted_compat_columns'
    )


def _factor_adjusted_quality_with_meta(df: pd.DataFrame, meta: dict) -> dict:
    required = ['code', 'close', 'backward_factor', 'adj_close', 'adj_open', 'adj_high', 'adj_low', 'adj_pre_close']
    missing = [field for field in required if field not in df.columns]
    if missing:
        return {'present': False, 'issues': [f'factor_adjusted_quality missing fields: {missing}'], 'warnings': []}
    work = df.copy()
    work['code'] = work['code'].astype(str)
    work = work.sort_values(['code', work.index.name or 'kline_time'])
    latest_factor = work.groupby('code', sort=False)['backward_factor'].transform('last')
    valid = work['backward_factor'].notna()
    legacy_mode = _legacy_adjusted_compat_mode(meta)
    field_pairs = [('open', 'adj_open'), ('high', 'adj_high'), ('low', 'adj_low'), ('close', 'adj_close')]
    mismatches = {}
    issues = []
    price_atol = 0.02
    for raw_field, adj_field in field_pairs:
        if legacy_mode:
            expected = work.loc[valid, raw_field] * work.loc[valid, 'backward_factor']
        else:
            normalized = valid & latest_factor.notna() & latest_factor.ne(0)
            expected = work.loc[normalized, raw_field] * work.loc[normalized, 'backward_factor'] / latest_factor.loc[normalized]
            actual = work.loc[normalized, adj_field]
            mask = ~(np.isclose(expected.astype(float), actual.astype(float), rtol=1e-5, atol=price_atol) | (expected.isna() & actual.isna()))
            mismatch_rows = work.loc[normalized].loc[mask, ['code', raw_field, 'backward_factor', adj_field]].head(20).to_dict(orient='records')
            mismatches[adj_field] = {'mismatch_count': int(mask.sum()), 'examples': mismatch_rows}
            if mask.any():
                issues.append(f'{adj_field} not self-consistent with raw price + backward_factor ({int(mask.sum())} rows)')
            continue
        actual = work.loc[valid, adj_field]
        mask = ~(np.isclose(expected.astype(float), actual.astype(float), rtol=1e-5, atol=price_atol) | (expected.isna() & actual.isna()))
        mismatch_rows = work.loc[valid].loc[mask, ['code', raw_field, 'backward_factor', adj_field]].head(20).to_dict(orient='records')
        mismatches[adj_field] = {'mismatch_count': int(mask.sum()), 'examples': mismatch_rows}
        if mask.any():
            issues.append(f'{adj_field} not self-consistent with raw price + backward_factor ({int(mask.sum())} rows)')
    expected_pre = work.groupby('code', sort=False)['adj_close'].shift(1)
    pre_mask = expected_pre.notna() & work['adj_pre_close'].notna()
    pre_bad = ~(np.isclose(expected_pre.loc[pre_mask].astype(float), work.loc[pre_mask, 'adj_pre_close'].astype(float), rtol=1e-5, atol=1e-5))
    pre_examples = work.loc[pre_mask].loc[pre_bad, ['code', 'adj_close', 'adj_pre_close']].head(20).to_dict(orient='records')
    if pre_bad.any():
        issues.append(f'adj_pre_close not aligned with prior adj_close ({int(pre_bad.sum())} rows)')
    return {
        'present': True,
        'issues': issues,
        'warnings': [],
        'field_mismatches': mismatches,
        'adj_pre_close_mismatch_count': int(pre_bad.sum()),
        'adj_pre_close_examples': pre_examples,
        'mode': 'legacy_raw_times_backward_factor' if legacy_mode else 'normalized_by_latest_backward_factor',
    }


def _is_tushare_v1_legacy_adjusted(meta: dict) -> bool:
    return _legacy_adjusted_compat_mode(meta)


def _demote_tushare_v1_compat_issues(issues: list[str], warnings: list[str], df: pd.DataFrame, meta: dict) -> list[str]:
    if not _is_tushare_v1_legacy_adjusted(meta):
        return issues
    remaining: list[str] = []
    for issue in issues:
        if (
            issue.startswith('valuation_and_fundamental_fields missing rate')
            and issue.endswith('at field PE')
            and 'PE' in df.columns
            and 'PB' in df.columns
            and len(df)
        ):
            pe_missing = df['PE'].isna()
            pb_present_ratio = float(df.loc[pe_missing, 'PB'].notna().mean()) if pe_missing.any() else 0.0
            if pb_present_ratio >= 0.8:
                warnings.append(f"pe_ttm_structural_missing:{float(pe_missing.mean()):.4f}:pb_present_ratio={pb_present_ratio:.4f}")
                continue
        if 'not self-consistent with raw price + backward_factor' in issue:
            warnings.append(f"tushare_legacy_adjusted_compat:{issue}")
            continue
        remaining.append(issue)
    return remaining


def _hdf_daily_columns(hdf5_path: Path) -> list[str]:
    try:
        with pd.HDFStore(hdf5_path, mode='r') as store:
            storer = store.get_storer('/daily')
            axes = getattr(storer, 'non_index_axes', None) or []
            if axes:
                return list(axes[0][1])
    except Exception:
        return []
    return []


def _read_daily_compat_frame(hdf5_path: Path) -> pd.DataFrame:
    available = _hdf_daily_columns(hdf5_path)
    columns = [field for field in DAILY_COMPAT_FIELDS if not available or field in available]
    try:
        if columns:
            return pd.read_hdf(hdf5_path, key='/daily', columns=columns)
    except Exception:
        pass
    return pd.read_hdf(hdf5_path, key='/daily')


def _daily_compat_check(hdf5_path: Path, replace_from_date: str | None = None) -> dict:
    issues: list[str] = []
    warnings: list[str] = []
    if not hdf5_path.exists():
        return {'passed': False, 'profile': 'daily_compat', 'issues': [f'HDF5 file not found: {hdf5_path}'], 'warnings': []}
    try:
        df = _read_daily_compat_frame(hdf5_path).reset_index(drop=False)
    except Exception as exc:
        return {'passed': False, 'profile': 'daily_compat', 'issues': [f'Cannot read HDF5: {exc}'], 'warnings': []}
    if df.empty:
        return {'passed': False, 'profile': 'daily_compat', 'issues': ['HDF5 daily table is empty'], 'warnings': []}

    missing_fields = [field for field in DAILY_COMPAT_FIELDS if field not in df.columns]
    if missing_fields:
        issues.append(f'daily_compat missing fields: {", ".join(missing_fields)}')
    if 'code' not in df.columns or 'kline_time' not in df.columns:
        return {
            'passed': False,
            'profile': 'daily_compat',
            'issues': issues or ['daily_compat requires code and kline_time'],
            'warnings': warnings,
        }

    df['code'] = df['code'].astype(str)
    trade_dates = pd.to_datetime(df['kline_time'], errors='coerce')
    latest_trade_date = trade_dates.max()
    if pd.isna(latest_trade_date):
        issues.append('daily_compat cannot determine latest trade date')
        latest_iso = None
    else:
        latest_iso = str(pd.Timestamp(latest_trade_date).date())

    scanned = df.copy()
    preboundary_benchmark_checks: list[dict] = []
    if replace_from_date:
        replace_from_text = str(replace_from_date).replace('-', '')
        replace_from = pd.Timestamp(
            f'{replace_from_text[:4]}-{replace_from_text[4:6]}-{replace_from_text[6:8]}'
        )
        all_dates = pd.to_datetime(df['kline_time'], errors='coerce')
        previous_dates = all_dates[all_dates < replace_from]
        if not previous_dates.empty:
            preboundary_date = pd.Timestamp(previous_dates.max()).normalize()
            preboundary_rows = df[all_dates.dt.normalize() == preboundary_date]
            for code in REQUIRED_BENCHMARK_INDEX_CODES:
                present = bool((preboundary_rows['code'] == code).any())
                check = {'code': code, 'date': str(preboundary_date.date()), 'present': present}
                preboundary_benchmark_checks.append(check)
                if not present:
                    issues.append(f"daily_compat preboundary benchmark missing: {code}:{preboundary_date.date()}")
        scanned = scanned[all_dates >= replace_from]
        if scanned.empty:
            issues.append(f'daily_compat window empty from replace_from_date={replace_from_date}')
    scanned_dates = pd.to_datetime(scanned['kline_time'], errors='coerce') if not scanned.empty else pd.Series(dtype='datetime64[ns]')

    duplicate_keys = int(scanned.duplicated(['code', 'kline_time']).sum()) if not scanned.empty else 0
    if duplicate_keys:
        issues.append(f'daily_compat duplicate code/kline_time keys: {duplicate_keys}')

    price_sanity = {}
    if {'high', 'low'}.issubset(scanned.columns):
        price_sanity['high_lt_low'] = int((scanned['high'] < scanned['low']).sum())
        if price_sanity['high_lt_low']:
            issues.append(f"daily_compat high_lt_low: {price_sanity['high_lt_low']}")
    if {'close', 'high', 'low'}.issubset(scanned.columns):
        price_sanity['close_outside_range'] = int(((scanned['close'] > scanned['high']) | (scanned['close'] < scanned['low'])).sum())
        if price_sanity['close_outside_range']:
            issues.append(f"daily_compat close_outside_range: {price_sanity['close_outside_range']}")
    if {'open', 'high', 'low'}.issubset(scanned.columns):
        price_sanity['open_outside_range'] = int(((scanned['open'] > scanned['high']) | (scanned['open'] < scanned['low'])).sum())
        if price_sanity['open_outside_range']:
            issues.append(f"daily_compat open_outside_range: {price_sanity['open_outside_range']}")

    latest_rows = scanned[scanned_dates == scanned_dates.max()].reset_index(drop=True) if not scanned.empty and not scanned_dates.empty else scanned.iloc[0:0].reset_index(drop=True)
    latest_nulls: dict[str, int] = {}
    latest_structural_nulls: dict[str, int] = {}
    if not latest_rows.empty and {'LIST_DATE', 'kline_time'}.issubset(latest_rows.columns):
        latest_list_text = latest_rows['LIST_DATE'].astype('string').str.replace('-', '', regex=False).str.slice(0, 8)
        latest_trade_text = pd.to_datetime(latest_rows['kline_time'], errors='coerce').dt.strftime('%Y%m%d')
        latest_listing_day = pd.Series(latest_list_text.to_numpy() == latest_trade_text.to_numpy(), index=latest_rows.index)
    else:
        latest_listing_day = pd.Series(False, index=latest_rows.index)
    for field in [field for field in CORE_MARKET_FIELDS + ['pre_close'] if field in latest_rows.columns]:
        null_mask = latest_rows[field].isna()
        structural_mask = (
            pd.Series(null_mask.to_numpy() & latest_listing_day.to_numpy(), index=latest_rows.index)
            if field == 'pre_close'
            else pd.Series(False, index=latest_rows.index)
        )
        null_count = int((null_mask & ~structural_mask).sum())
        latest_nulls[field] = null_count
        if structural_mask.any():
            latest_structural_nulls[field] = int(structural_mask.sum())
        if null_count:
            issues.append(f'daily_compat latest rows null {field}: {null_count}')

    benchmark_checks = []
    if latest_iso:
        for code in REQUIRED_BENCHMARK_INDEX_CODES:
            subset = df[df['code'] == code]
            latest = None
            if not subset.empty:
                latest = pd.to_datetime(subset['kline_time'], errors='coerce').max()
            check = {'code': code, 'present': not subset.empty, 'latest_date': str(latest.date()) if pd.notna(latest) else None}
            benchmark_checks.append(check)
            if subset.empty:
                issues.append(f'daily_compat benchmark index missing: {code}')
            elif check['latest_date'] != latest_iso:
                issues.append(f"daily_compat benchmark index {code} latest {check['latest_date']} != HDF5 latest {latest_iso}")

    scanned_range = {
        'start': str(scanned_dates.min().date()) if not scanned_dates.empty and pd.notna(scanned_dates.min()) else None,
        'end': str(scanned_dates.max().date()) if not scanned_dates.empty and pd.notna(scanned_dates.max()) else None,
    }
    return {
        'passed': not issues,
        'profile': 'daily_compat',
        'issues': issues,
        'warnings': warnings,
        'latest_trade_date': latest_iso,
        'replace_from_date': replace_from_date,
        'scanned_rows': int(len(scanned)),
        'scanned_range': scanned_range,
        'missing_fields': missing_fields,
        'duplicate_keys': duplicate_keys,
        'price_sanity': price_sanity,
        'latest_nulls': latest_nulls,
        'latest_structural_nulls': latest_structural_nulls,
        'benchmark_index_quality': {'checks': benchmark_checks, 'preboundary_checks': preboundary_benchmark_checks},
    }


def check(
    hdf5_path: Optional[Path] = None,
    *,
    profile: str = 'deep_full',
    replace_from_date: str | None = None,
) -> dict:
    issues: list[str] = []
    warnings: list[str] = []

    hdf5_path = Path(hdf5_path or PRODUCTION_RAW_HDF5).expanduser()
    if profile == 'daily_compat':
        return _daily_compat_check(hdf5_path, replace_from_date=replace_from_date)
    if profile != 'deep_full':
        return {'passed': False, 'issues': [f'unsupported quality profile: {profile}'], 'warnings': []}
    if not hdf5_path.exists():
        return {'passed': False, 'issues': [f'HDF5 file not found: {hdf5_path}'], 'warnings': []}

    try:
        df = pd.read_hdf(hdf5_path, key='/daily')
    except Exception as e:
        return {'passed': False, 'issues': [f'Cannot read HDF5: {e}'], 'warnings': []}

    n_rows = len(df)
    if n_rows < 2000:
        warnings.append(f'Low row count: {n_rows}')

    latest_code_activity = _latest_code_activity(df)
    metadata_quality = _metadata_quality(hdf5_path, latest_code_activity=latest_code_activity)
    meta = metadata_quality.get('metadata') or {}

    field_stats: dict[str, dict] = {}
    group_checks: dict[str, dict] = {}
    overall_missing_max = 0.0
    for group_name, group_def in FIELD_GROUPS.items():
        expected_fields = list(group_def['fields'])
        present_fields = [field for field in expected_fields if field in df.columns]
        missing_fields = [field for field in expected_fields if field not in df.columns]
        if not present_fields:
            issues.append(f'Missing expected group: {group_name}')
            group_checks[group_name] = {
                'present_fields': [],
                'missing_fields': missing_fields,
                'max_missing_pct': 1.0,
                'threshold': group_def['max_missing_pct'],
                'worst_field': None,
            }
            overall_missing_max = max(overall_missing_max, 1.0)
            continue
        if missing_fields:
            issues.append(f'{group_name} missing fields: {", ".join(missing_fields)}')

        group_missing = df[present_fields].isnull().mean().sort_values(ascending=False)
        worst_field = str(group_missing.index[0])
        worst_missing = float(group_missing.iloc[0])
        overall_missing_max = max(overall_missing_max, worst_missing)
        group_checks[group_name] = {
            'present_fields': present_fields,
            'missing_fields': missing_fields,
            'max_missing_pct': worst_missing,
            'threshold': group_def['max_missing_pct'],
            'worst_field': worst_field,
        }
        if worst_missing > group_def['max_missing_pct']:
            issues.append(
                f"{group_name} missing rate {worst_missing:.2%} exceeds threshold "
                f"{group_def['max_missing_pct']:.0%} at field {worst_field}"
            )
        for field in present_fields:
            series = df[field]
            zero_ratio = None
            if pd.api.types.is_numeric_dtype(series):
                zero_ratio = float((series == 0).mean())
            field_stats[field] = {'missing_pct': float(series.isnull().mean()), 'zero_ratio': zero_ratio}

    daily_field_coverage = _daily_field_coverage(df)
    for group_name, coverage in daily_field_coverage.items():
        if coverage['zero_coverage_day_count'] > coverage['max_zero_coverage_days']:
            days = ', '.join(coverage['zero_coverage_days'][:10])
            issues.append(f'{group_name} has zero-covered PIT days: {days}')
        if coverage['low_coverage_day_count'] > 0:
            days = ', '.join(coverage['low_coverage_days'][:10])
            issues.append(f"{group_name} daily coverage below {coverage['min_daily_coverage_ratio']:.0%}: {days}")

    for col in ['close', 'open', 'high', 'low', 'volume', 'amount']:
        if col in df.columns and (df[col] < 0).any():
            issues.append(f'Negative values in column: {col}')

    if 'close' in df.columns:
        zero_close_ratio = float((df['close'] == 0).mean())
        if zero_close_ratio > 0.001:
            issues.append(f'Zero close ratio {zero_close_ratio:.4%} is too high')
        elif zero_close_ratio > 0:
            warnings.append(f'Observed zero close rows: {zero_close_ratio:.4%}')
    else:
        zero_close_ratio = None

    latest_trade_date = None
    if 'kline_time' in df.columns and not df.empty:
        latest_trade_date = str(pd.to_datetime(df['kline_time']).max().date())

    if latest_code_activity.get('stale_stock_count'):
        warnings.append(
            'latest-day stale stock codes present: '
            f"{latest_code_activity['stale_stock_count']} "
            f"(recent<=7d {latest_code_activity['recent_stale_stock_count']}, "
            f"long>7d {latest_code_activity['long_stale_stock_count']})"
        )
    issues.extend(metadata_quality.get('issues', []))
    warnings.extend(metadata_quality.get('warnings', []))
    benchmark_index_quality = _benchmark_index_quality(df)
    issues.extend(benchmark_index_quality.get('issues', []))
    limit_price_quality = _limit_price_quality(df)
    issues.extend(limit_price_quality.get('issues', []))
    warnings.extend(limit_price_quality.get('warnings', []))
    factor_adjusted_quality = _factor_adjusted_quality_with_meta(df, meta)
    issues.extend(factor_adjusted_quality.get('issues', []))
    warnings.extend(factor_adjusted_quality.get('warnings', []))
    issues = _demote_tushare_v1_compat_issues(issues, warnings, df, meta)

    schema_summary = {
        'schema_version': meta.get('schema_version'),
        'price_mode': meta.get('price_mode'),
        'cache_mode': meta.get('cache_mode'),
        'effective_target_date': meta.get('effective_target_date'),
        'historical_limit_source_untrusted': bool(meta.get('historical_limit_source_untrusted')),
        'adjusted_price_mode': meta.get('adjusted_price_mode'),
    }

    return {
        'passed': not issues,
        'n_rows': n_rows,
        'missing_pct': overall_missing_max,
        'field_groups': group_checks,
        'field_stats': field_stats,
        'daily_field_coverage': daily_field_coverage,
        'issues': issues,
        'warnings': warnings,
        'zero_close_ratio': zero_close_ratio,
        'latest_trade_date': latest_trade_date,
        'latest_code_activity': latest_code_activity,
        'metadata_quality': metadata_quality,
        'benchmark_index_quality': benchmark_index_quality,
        'limit_price_quality': limit_price_quality,
        'factor_adjusted_quality': factor_adjusted_quality,
        'schema_summary': schema_summary,
        'file': str(hdf5_path),
    }
