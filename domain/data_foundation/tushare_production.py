from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from domain.data_foundation.convert_to_quantgpt import convert as convert_quantgpt, quantgpt_contract_report
from domain.data_foundation.limit_execution import (
    DEFAULT_SEALED_TURNOVER_RATIO_THRESHOLD,
    LimitExecutionColumns,
    open_sealed_limit_fields,
)
from domain.data_foundation.ops_common import (
    PROMOTION_BACKUP_ROOT,
    PRODUCTION_LOCK_DIR,
    _acquire_lock,
    _promote_qlib_market_data,
    _qlib_index_readiness,
    _read_json,
    _release_lock,
    _replace_path,
    _restore_state_files,
    _rollback,
    _snapshot_state_files,
    _target_date_iso,
    _write_daily_status,
    data_job_guard,
)
from domain.data_foundation.runtime_io import atomic_write_json, atomic_write_text
from storage.paths import (
    CURRENT_PRODUCTION_DATASET_FILE,
    DATA_FOUNDATION_ROOT,
    LATEST_STATUS_FILE,
    PROJECT_ROOT,
    PRODUCTION_RAW_HDF5,
    PRODUCTION_RAW_METADATA,
    PRODUCTION_TRADING_CALENDAR_FILE,
    PRODUCTION_TRADING_CALENDAR_META,
    QLIB_CONVERT_SCRIPT,
    QLIB_DATA_ROOT,
    QLIB_INDEX_CONVERT_SCRIPT,
    QUANTGPT_BENCHMARK_DIR,
    QUANTGPT_DATA_DIR,
)


STAGING_ROOT = DATA_FOUNDATION_ROOT / "staging"
COMPAT_ROOT_NAME = "production_compat"
COMPAT_MANIFEST_NAME = "production_compat_manifest.json"
PRODUCTION_QUALITY_FILE = PRODUCTION_RAW_HDF5.with_name("tushare_quality_report.json")
PRODUCTION_RAW_QUALITY_FILE = PRODUCTION_RAW_HDF5.with_name("tushare_raw_quality_report.json")
TUSHARE_PRODUCTION_QUALITY_FILE = PRODUCTION_QUALITY_FILE
TUSHARE_PRODUCTION_RAW_QUALITY_FILE = PRODUCTION_RAW_QUALITY_FILE
INDEX_NAME_MAP = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000016.SH": "上证50",
}
REQUIRED_BENCHMARKS = ["benchmark_hs300.parquet", "benchmark_csi500.parquet", "benchmark_csi1000.parquet"]
QLIB_RAW_FIELD_MAP = {
    "$open": "open", "$close": "close", "$high": "high", "$low": "low", "$volume": "volume", "$amount": "amount",
    "$pre_close": "pre_close", "$pct_chg": "pct_chg", "$amp": "amp", "$turnover_rate": "turnover_rate",
    "$pe": "PE", "$pb": "PB",
    "$total_mv": "total_mv", "$float_mv": "float_mv", "$roe": "ROE", "$roa": "ROA", "$eps": "EPS",
    "$net_profit": "NET_PROFIT", "$tot_equity": "TOT_EQUITY", "$total_assets": "TOTAL_ASSETS",
    "$net_asset_ps": "NET_ASSET_PS", "$holder_num": "HOLDER_NUM", "$tot_share": "TOT_SHARE",
    "$float_a_share": "FLOAT_A_SHARE", "$borrow_money_bal": "BORROW_MONEY_BAL",
    "$purch_borrow_money": "PURCH_BORROW_MONEY", "$sec_lending_bal": "SEC_LENDING_BAL",
    "$margin_trade_bal": "MARGIN_TRADE_BAL", "$factor": "backward_factor",
    "$cost_15pct": "cost_15pct", "$cost_85pct": "cost_85pct", "$weight_avg": "weight_avg",
}
QLIB_RAW_OPTIONAL_COLUMNS = ["up_limit", "down_limit", "limit_source_kind"]
LEGACY_NUMERIC_COLUMNS = [
    "open", "high", "low", "close", "volume", "amount", "pre_close", "stk_limit_pre_close",
    "up_limit", "down_limit",
    "amp", "pct_chg", "backward_factor", "adj_open", "adj_high", "adj_low", "adj_close", "adj_pre_close",
    "adj_pct_chg", "adj_amp", "TOT_SHARE", "FLOAT_A_SHARE", "EPS", "NET_PROFIT", "TOT_EQUITY",
    "TOTAL_ASSETS", "NET_ASSET_PS", "HOLDER_NUM", "PE", "PB", "ROE", "ROA", "total_mv", "float_mv",
    "turnover_rate", "BORROW_MONEY_BAL", "PURCH_BORROW_MONEY", "SEC_LENDING_BAL", "MARGIN_TRADE_BAL",
    "turnover_rate_f", "ps_ttm", "dv_ttm", "free_share", "sm_net_vol", "sm_net_amount", "lg_net_vol",
    "lg_net_amount", "net_mf_vol", "net_mf_amount", "cost_15pct", "cost_85pct", "weight_avg",
    "margin_buy_amount", "margin_balance", "short_balance",
]
LEGACY_STRING_COLUMNS = ["code", "SECURITY_NAME", "MARKET_CODE", "LIST_DATE", "list_status", "st_status", "limit_source_kind"]
LEGACY_COLUMNS = [
    "code", "kline_time", "SECURITY_NAME", "MARKET_CODE", "LIST_DATE", "list_status", "st_status", "limit_source_kind",
    *LEGACY_NUMERIC_COLUMNS,
]
MIN_ITEMSIZE = {
    "code": 12,
    "SECURITY_NAME": 48,
    "MARKET_CODE": 4,
    "LIST_DATE": 16,
    "list_status": 8,
    "st_status": 16,
    "limit_source_kind": 24,
}
LIMIT_PRICE_TOLERANCE = 1e-4
LIMIT_EPSILON = 0.005
SEALED_TURNOVER_RATIO_THRESHOLD = DEFAULT_SEALED_TURNOVER_RATIO_THRESHOLD


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _compat_root(package_root: Path) -> Path:
    return package_root / COMPAT_ROOT_NAME


def _compat_manifest_path(package_root: Path) -> Path:
    return _compat_root(package_root) / COMPAT_MANIFEST_NAME


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _resolve_tushare_package(package_id: str | None = None, latest: bool = False) -> tuple[Path, dict[str, Any]]:
    if package_id:
        root = STAGING_ROOT / package_id
    elif latest:
        manifests = sorted(STAGING_ROOT.glob("tushare-fullrebuild-*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not manifests:
            raise FileNotFoundError("No completed Tushare staging package found")
        root = manifests[0].parent
    else:
        raise ValueError("package_id is required unless latest=True")
    manifest = _read_manifest(root / "manifest.json")
    if not manifest:
        raise FileNotFoundError(f"manifest not found under {root}")
    if manifest.get("source") != "tushare":
        raise ValueError(f"{root.name} is not a Tushare package")
    return root, manifest


def _safe_ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    out = pd.Series(np.nan, index=num.index, dtype="float64")
    valid = den.notna() & den.ne(0)
    out.loc[valid] = num.loc[valid].astype(float) / den.loc[valid].astype(float)
    return out


def _changed_value_count(before: pd.Series | None, after: pd.Series) -> int:
    if before is None:
        return int(after.notna().sum())
    before_num = pd.to_numeric(before, errors="coerce")
    after_num = pd.to_numeric(after, errors="coerce")
    same = (
        np.isclose(before_num.astype(float), after_num.astype(float), rtol=1e-10, atol=1e-10)
        | (before_num.isna() & after_num.isna())
    )
    return int((~same).sum())


def recompute_derived_price_fields(hdf5_path: Path | str) -> dict[str, Any]:
    """Recompute previous-close and return fields after production HDF merges."""
    hdf5_path = Path(hdf5_path).expanduser()
    if not hdf5_path.exists():
        raise FileNotFoundError(f"production_hdf_missing:{hdf5_path}")

    with pd.HDFStore(hdf5_path, mode="r") as store:
        daily = store["/daily"]
        info = store["/info"] if "/info" in store else None

    required = {"code", "kline_time", "open", "high", "low", "close", "adj_open", "adj_high", "adj_low", "adj_close"}
    missing = sorted(required - set(daily.columns))
    if missing:
        raise RuntimeError(f"cannot_recompute_derived_price_fields_missing:{missing}")

    work = daily.reset_index()
    if "trade_date" not in work.columns:
        work["trade_date"] = pd.to_datetime(work["kline_time"], errors="coerce")
    work["kline_time"] = pd.to_datetime(work["kline_time"], errors="coerce")
    work["code"] = work["code"].astype(str)
    work = work.sort_values(["code", "kline_time"], kind="mergesort").reset_index(drop=True)

    fields = ["pre_close", "pct_chg", "amp", "adj_pre_close", "adj_pct_chg", "adj_amp"]
    before = {field: work[field].copy() if field in work.columns else None for field in fields}

    work["pre_close"] = work.groupby("code", sort=False)["close"].shift(1)
    work["pct_chg"] = _safe_ratio(work["close"] - work["pre_close"], work["pre_close"]) * 100.0
    work["amp"] = _safe_ratio(work["high"] - work["low"], work["pre_close"]) * 100.0
    work["adj_pre_close"] = work.groupby("code", sort=False)["adj_close"].shift(1)
    work["adj_pct_chg"] = _safe_ratio(work["adj_close"] - work["adj_pre_close"], work["adj_pre_close"]) * 100.0
    work["adj_amp"] = _safe_ratio(work["adj_high"] - work["adj_low"], work["adj_pre_close"]) * 100.0

    changed_counts = {field: _changed_value_count(before[field], work[field]) for field in fields}
    missing_counts_after = {field: int(work[field].isna().sum()) for field in fields}

    out = work.sort_values(["kline_time", "code"], kind="mergesort").set_index("trade_date")
    out.index.name = "trade_date"
    temp_file = hdf5_path.with_suffix(".h5.derived.tmp")
    if temp_file.exists():
        temp_file.unlink()
    _append_hdf(temp_file, "/daily", out, append=False, min_itemsize={key: value for key, value in MIN_ITEMSIZE.items() if key in out.columns})
    if info is not None:
        info.to_hdf(temp_file, key="/info", mode="a", format="table")
    temp_file.replace(hdf5_path)

    return {
        "status": "completed",
        "file": str(hdf5_path),
        "row_count": int(len(out)),
        "changed_counts": changed_counts,
        "missing_counts_after": missing_counts_after,
    }


def _fmt_list_date(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return series.dt.strftime("%Y%m%d")
    return series.astype("string").str.replace("-", "", regex=False)


def _append_hdf(path: Path, key: str, frame: pd.DataFrame, *, append: bool, min_itemsize: dict[str, int] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "key": key,
        "mode": "a" if append else "w",
        "format": "table",
        "append": append,
        "complevel": 1,
        "complib": "zlib",
    }
    if min_itemsize:
        kwargs["min_itemsize"] = min_itemsize
    frame.to_hdf(path, **kwargs)


def _instrument_from_code(code: str) -> str:
    num, market = str(code).split(".")
    return f"{num}{market.lower()}"


def _limit_rate_from_code(code: str, st_status: object = None) -> float:
    status = str(st_status or "").strip().upper()
    if status in {"ST", "*ST", "SST", "PT"}:
        return 0.05
    num, market = str(code).split(".")
    suffix = market.lower()
    if suffix == "bj" or num.startswith(("8", "4", "920")):
        return 0.30
    if (suffix == "sh" and num.startswith("688")) or (suffix == "sz" and num.startswith(("300", "301"))):
        return 0.20
    return 0.10


def _official_limit_flags(
    source: pd.DataFrame,
    limit_rate: pd.Series,
) -> tuple[
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    dict[str, pd.Series],
]:
    pct_chg = pd.to_numeric(source.get("pct_chg"), errors="coerce") / 100.0
    fallback_threshold = limit_rate - LIMIT_EPSILON
    fallback_buy = pct_chg.ge(fallback_threshold).fillna(False)
    fallback_sell = pct_chg.le(-fallback_threshold).fillna(False)

    up_limit = pd.to_numeric(source.get("up_limit"), errors="coerce")
    down_limit = pd.to_numeric(source.get("down_limit"), errors="coerce")
    close = pd.to_numeric(source.get("close"), errors="coerce")
    open_price = pd.to_numeric(source.get("open"), errors="coerce")
    has_official = up_limit.notna() & down_limit.notna()
    limit_source_kind = source.get("limit_source_kind")
    if limit_source_kind is None:
        structural_no_limit = pd.Series(False, index=source.index)
    else:
        structural_no_limit = pd.Series(limit_source_kind, index=source.index).astype(str).eq("structural_no_limit")
    official_buy = close.ge(up_limit - LIMIT_PRICE_TOLERANCE).fillna(False)
    official_sell = close.le(down_limit + LIMIT_PRICE_TOLERANCE).fillna(False)
    official_buy_open = open_price.ge(up_limit - LIMIT_PRICE_TOLERANCE).fillna(False)
    official_sell_open = open_price.le(down_limit + LIMIT_PRICE_TOLERANCE).fillna(False)
    mid_oc_price = (open_price + close) / 2.0
    official_buy_mid_oc = mid_oc_price.ge(up_limit - LIMIT_PRICE_TOLERANCE).fillna(False)
    official_sell_mid_oc = mid_oc_price.le(down_limit + LIMIT_PRICE_TOLERANCE).fillna(False)

    sealed_fields = open_sealed_limit_fields(
        source,
        LimitExecutionColumns(
            open="open",
            high="high",
            low="low",
            close="close",
            up_limit="up_limit",
            down_limit="down_limit",
            turnover_rate="turnover_rate",
        ),
        official_mask=has_official,
        price_tolerance=LIMIT_PRICE_TOLERANCE,
        turnover_ratio_threshold=SEALED_TURNOVER_RATIO_THRESHOLD,
    )

    return (
        official_buy.where(has_official, False).astype("float32"),
        official_sell.where(has_official, False).astype("float32"),
        (has_official | structural_no_limit).astype("float32"),
        official_buy_open.where(has_official, False).astype("float32"),
        official_sell_open.where(has_official, False).astype("float32"),
        official_buy_mid_oc.where(has_official, False).astype("float32"),
        official_sell_mid_oc.where(has_official, False).astype("float32"),
        fallback_buy.astype("float32"),
        fallback_sell.astype("float32"),
        sealed_fields,
    )


def _intraday_limit_hits(source: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    up_limit = pd.to_numeric(source.get("up_limit"), errors="coerce")
    down_limit = pd.to_numeric(source.get("down_limit"), errors="coerce")
    high = pd.to_numeric(source.get("high"), errors="coerce")
    low = pd.to_numeric(source.get("low"), errors="coerce")
    has_official = up_limit.notna() & down_limit.notna()
    hit_up = high.ge(up_limit - LIMIT_PRICE_TOLERANCE).fillna(False).where(has_official, False)
    hit_down = low.le(down_limit + LIMIT_PRICE_TOLERANCE).fillna(False).where(has_official, False)
    return hit_up.astype("float32"), hit_down.astype("float32")


def _limit_source_kind_for_frame(frame: pd.DataFrame) -> pd.Series:
    up_limit = pd.to_numeric(frame.get("up_limit"), errors="coerce")
    down_limit = pd.to_numeric(frame.get("down_limit"), errors="coerce")
    official = up_limit.notna() & down_limit.notna()
    if "LIST_DATE" in frame.columns:
        list_dates = pd.to_datetime(frame["LIST_DATE"].astype("string").str.replace("-", "", regex=False), format="%Y%m%d", errors="coerce")
    elif "list_date" in frame.columns:
        list_dates = pd.to_datetime(frame["list_date"], errors="coerce")
    else:
        list_dates = pd.Series(pd.NaT, index=frame.index)
    if "kline_time" in frame.columns:
        trade_dates = pd.to_datetime(frame["kline_time"], errors="coerce")
    else:
        trade_dates = pd.to_datetime(frame.get("trade_date"), errors="coerce")
    structural_no_limit = official.eq(False) & list_dates.dt.normalize().eq(trade_dates.dt.normalize())
    index_row = (
        frame["list_status"].astype("string").str.upper().eq("I")
        if "list_status" in frame.columns
        else pd.Series(False, index=frame.index)
    )
    values = np.where(
        official,
        "official",
        np.where(structural_no_limit, "structural_no_limit", np.where(index_row, "index", "missing")),
    )
    return pd.Series(values, index=frame.index, dtype="string")


def _normalize_stock_chunk(chunk: pd.DataFrame, raw_carry: dict[str, float], adj_carry: dict[str, float]) -> tuple[pd.DataFrame, dict[str, float], dict[str, float]]:
    work = chunk.copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"])
    work["list_date"] = pd.to_datetime(work["list_date"], errors="coerce")
    work = work.sort_values(["trade_date", "code"]).reset_index(drop=True)
    work["kline_time"] = work["trade_date"]
    work["SECURITY_NAME"] = work["name"].astype("string")
    work["MARKET_CODE"] = work["code"].astype("string").str.split(".").str[-1]
    work["LIST_DATE"] = _fmt_list_date(work["list_date"])
    work["list_status"] = work["list_status"].astype("string")
    if "st_status" not in work.columns:
        work["st_status"] = "NORMAL"
    work["st_status"] = work["st_status"].fillna("NORMAL").astype("string")
    work["limit_source_kind"] = _limit_source_kind_for_frame(work)

    fallback_pre_close = work.groupby("code", sort=False)["close"].shift(1)
    first_rows = ~work["code"].duplicated()
    fill_mask = first_rows & fallback_pre_close.isna()
    fallback_pre_close.loc[fill_mask] = work.loc[fill_mask, "code"].map(raw_carry)
    official_pre_close = pd.to_numeric(work.get("stk_limit_pre_close"), errors="coerce")
    if not isinstance(official_pre_close, pd.Series):
        official_pre_close = pd.Series(np.nan, index=work.index, dtype="float64")
    work["stk_limit_pre_close"] = official_pre_close
    work["pre_close"] = official_pre_close.combine_first(fallback_pre_close)
    work["adj_pre_close"] = work.groupby("code", sort=False)["hfq_close"].shift(1)
    adj_fill_mask = first_rows & work["adj_pre_close"].isna()
    work.loc[adj_fill_mask, "adj_pre_close"] = work.loc[adj_fill_mask, "code"].map(adj_carry)

    work["pct_chg"] = _safe_ratio(work["close"] - work["pre_close"], work["pre_close"]) * 100.0
    work["amp"] = _safe_ratio(work["high"] - work["low"], work["pre_close"]) * 100.0
    work["adj_open"] = work["hfq_open"]
    work["adj_high"] = work["hfq_high"]
    work["adj_low"] = work["hfq_low"]
    work["adj_close"] = work["hfq_close"]
    work["adj_pct_chg"] = _safe_ratio(work["adj_close"] - work["adj_pre_close"], work["adj_pre_close"]) * 100.0
    work["adj_amp"] = _safe_ratio(work["adj_high"] - work["adj_low"], work["adj_pre_close"]) * 100.0
    work["backward_factor"] = work["adj_factor"]

    work["TOT_SHARE"] = work["total_share"]
    work["FLOAT_A_SHARE"] = work["float_share"]
    work["EPS"] = work["eps"]
    work["NET_PROFIT"] = work["net_profit"]
    work["TOT_EQUITY"] = work["total_equity"]
    work["TOTAL_ASSETS"] = work["total_assets"]
    work["PE"] = work["pe_ttm"]
    work["PB"] = work["pb"]
    work["ROE"] = work["roe"]
    work["ROA"] = work["roa"]
    work["NET_ASSET_PS"] = np.where(
        work["total_share"].notna() & work["total_share"].ne(0),
        work["total_equity"].astype(float) / (work["total_share"].astype(float) * 10000.0),
        np.nan,
    )
    work["HOLDER_NUM"] = work["holder_num"]
    work["BORROW_MONEY_BAL"] = work["margin_balance"]
    work["PURCH_BORROW_MONEY"] = work["margin_buy_amount"]
    work["SEC_LENDING_BAL"] = work["short_balance"]
    work["MARGIN_TRADE_BAL"] = work[["margin_balance", "short_balance"]].sum(axis=1, min_count=1)

    raw_carry.update(work.groupby("code", sort=False)["close"].last().dropna().to_dict())
    adj_carry.update(work.groupby("code", sort=False)["hfq_close"].last().dropna().to_dict())

    out = work[["trade_date", *LEGACY_COLUMNS]].copy().set_index("trade_date")
    out.index.name = "trade_date"
    for col in LEGACY_NUMERIC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    for col in LEGACY_STRING_COLUMNS:
        out[col] = out[col].astype("string")
    out["kline_time"] = pd.to_datetime(out["kline_time"])
    return out, raw_carry, adj_carry


def _normalize_index_frame(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"])
    work = work.sort_values(["code", "trade_date"]).reset_index(drop=True)
    work["kline_time"] = work["trade_date"]
    work["SECURITY_NAME"] = work["code"].map(INDEX_NAME_MAP).fillna(work["code"]).astype("string")
    work["MARKET_CODE"] = work["code"].astype("string").str.split(".").str[-1]
    work["LIST_DATE"] = work.groupby("code", sort=False)["trade_date"].transform("min").dt.strftime("%Y%m%d")
    work["list_status"] = "I"
    work["st_status"] = "NORMAL"
    work["limit_source_kind"] = "index"
    work["pre_close"] = work.groupby("code", sort=False)["close"].shift(1)
    work["pct_chg"] = _safe_ratio(work["close"] - work["pre_close"], work["pre_close"]) * 100.0
    work["amp"] = _safe_ratio(work["high"] - work["low"], work["pre_close"]) * 100.0
    work["backward_factor"] = 1.0
    work["adj_open"] = work["open"]
    work["adj_high"] = work["high"]
    work["adj_low"] = work["low"]
    work["adj_close"] = work["close"]
    work["adj_pre_close"] = work["pre_close"]
    work["adj_pct_chg"] = work["pct_chg"]
    work["adj_amp"] = work["amp"]
    for col in [
        "up_limit", "down_limit", "stk_limit_pre_close",
        "TOT_SHARE", "FLOAT_A_SHARE", "EPS", "NET_PROFIT", "TOT_EQUITY", "TOTAL_ASSETS", "NET_ASSET_PS",
        "HOLDER_NUM", "PE", "PB", "ROE", "ROA", "total_mv", "float_mv", "turnover_rate", "BORROW_MONEY_BAL",
        "PURCH_BORROW_MONEY", "SEC_LENDING_BAL", "MARGIN_TRADE_BAL", "turnover_rate_f", "ps_ttm", "dv_ttm",
        "free_share", "sm_net_vol", "sm_net_amount", "lg_net_vol", "lg_net_amount", "net_mf_vol", "net_mf_amount",
        "cost_15pct", "cost_85pct", "weight_avg", "margin_buy_amount", "margin_balance", "short_balance",
    ]:
        work[col] = np.nan
    out = work[["trade_date", *LEGACY_COLUMNS]].copy().set_index("trade_date")
    out.index.name = "trade_date"
    for col in LEGACY_NUMERIC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    for col in LEGACY_STRING_COLUMNS:
        out[col] = out[col].astype("string")
    out["kline_time"] = pd.to_datetime(out["kline_time"])
    return out


def _write_compat_metadata(path: Path, package_root: Path, manifest: dict[str, Any], index_count: int) -> None:
    payload = {
        "source": "tushare",
        "compatibility_mode": "tushare_raw_hdf_compat",
        "schema_version": manifest.get("schema_version", "tushare_v1"),
        "package_id": manifest.get("package_id"),
        "effective_target_date": manifest.get("effective_target_date"),
        "latest_trade_date": _target_date_iso(manifest.get("effective_target_date")),
        "stock_code_count": int(manifest.get("code_count") or 0),
        "index_code_count": int(index_count),
        "generated_at": _now(),
        "source_quality_report": str(PRODUCTION_QUALITY_FILE.relative_to(PROJECT_ROOT)),
        "source_raw_quality_report": str(PRODUCTION_RAW_QUALITY_FILE.relative_to(PROJECT_ROOT)),
        "source_package_id": manifest.get("package_id"),
        "price_mode": "raw_with_legacy_adjusted_compat_columns",
        "adjusted_price_mode": "legacy_raw_times_backward_factor",
        "notes": [
            "PE is populated from Tushare pe_ttm for compatibility.",
            "QuantGPT consumes adjusted prices from adj_* fields via convert_to_quantgpt.",
            "Qlib is generated directly from raw OHLC plus backward_factor.",
            "Qlib bin conversion adjusts canonical OHLC/VWAP/chip fields before writing bins; raw prices remain only as audit and limit-source fields.",
        ],
    }
    atomic_write_json(path, payload)


def _write_trading_calendar(source_hdf: Path, calendar_path: Path, meta_path: Path) -> dict[str, Any]:
    if not source_hdf.exists():
        raise FileNotFoundError(f"production_raw_hdf_missing:{source_hdf}")
    dates: set[str] = set()
    with pd.HDFStore(source_hdf, mode="r") as store:
        if "/daily" not in store:
            raise RuntimeError("production_raw_hdf_daily_key_missing")
        nrows = int(store.get_storer("/daily").nrows or 0)
        for start in range(0, nrows, 500000):
            chunk = store.select("/daily", start=start, stop=min(start + 500000, nrows), columns=["kline_time"])
            parsed = pd.to_datetime(chunk["kline_time"], errors="coerce").dropna()
            dates.update(str(pd.Timestamp(item).date()) for item in parsed)
    if not dates:
        raise RuntimeError("production_trade_calendar_empty")
    calendar_path.parent.mkdir(parents=True, exist_ok=True)
    lines = sorted(dates)
    atomic_write_text(calendar_path, "\n".join(lines) + "\n")
    payload = {
        "source": "tushare",
        "generated_at": _now(),
        "calendar_source_hdf": str(source_hdf),
        "date_count": len(lines),
        "first_date": lines[0],
        "latest_date": lines[-1],
    }
    atomic_write_json(meta_path, payload)
    return payload


def _latest_hdf_trade_date_light(hdf_path: Path) -> str | None:
    if not hdf_path.exists():
        return None
    latest = None
    with pd.HDFStore(hdf_path, mode="r") as store:
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


def _daily_hdf_columns(source_hdf: Path, key: str = "/daily") -> list[str]:
    with pd.HDFStore(source_hdf, mode="r") as store:
        if key not in store:
            raise KeyError(f"{key} missing from {source_hdf}")
        storer = store.get_storer(key)
        axes = getattr(storer, "non_index_axes", None) or []
        if axes:
            return [str(col) for col in axes[0][1]]
        sample = store.select(key, start=0, stop=1)
    return [str(col) for col in sample.columns]


def _iter_daily_hdf_chunks(
    source_hdf: Path,
    *,
    columns: list[str],
    chunk_rows: int = 250000,
    key: str = "/daily",
):
    with pd.HDFStore(source_hdf, mode="r") as store:
        if key not in store:
            raise KeyError(f"{key} missing from {source_hdf}")
        nrows = int(store.get_storer(key).nrows or 0)
        for start in range(0, nrows, chunk_rows):
            yield store.select(key, start=start, stop=min(start + chunk_rows, nrows), columns=columns)


def _limit_rate_series_for_frame(source: pd.DataFrame) -> pd.Series:
    code = source["code"].astype(str)
    num = code.str.split(".", n=1).str[0]
    market = code.str.split(".", n=1).str[1].str.lower()
    rate = pd.Series(0.10, index=source.index, dtype="float32")
    rate.loc[market.eq("bj") | num.str.startswith(("8", "4", "920"), na=False)] = 0.30
    rate.loc[
        (market.eq("sh") & num.str.startswith("688", na=False))
        | (market.eq("sz") & num.str.startswith(("300", "301"), na=False))
    ] = 0.20
    status = source.get("st_status")
    if status is not None:
        st_status = status.astype(str).str.strip().str.upper()
        rate.loc[st_status.isin({"ST", "*ST", "SST", "PT"})] = 0.05
    return rate.astype("float32")


def raw_chunk_to_qlib_frame(source_chunk: pd.DataFrame) -> pd.DataFrame:
    """Normalize a raw-HDF chunk into Qlib source field semantics.

    This is an in-memory transformation and does not create an intermediate
    model-framework dataset.
    """
    index_codes = set(INDEX_NAME_MAP)
    source = source_chunk[~source_chunk["code"].isin(index_codes)].copy()
    if source.empty:
        return pd.DataFrame()
    for col in QLIB_RAW_OPTIONAL_COLUMNS:
        if col not in source.columns:
            source[col] = np.nan
    source = source[~source["code"].isin(index_codes)].copy()
    source["datetime"] = pd.to_datetime(source["kline_time"], errors="coerce")
    source["instrument"] = source["code"].map(_instrument_from_code)
    source = source[source["datetime"].notna() & source["instrument"].notna()]
    if source.empty:
        return pd.DataFrame()

    out = pd.DataFrame(index=source.index)
    for dst, src in QLIB_RAW_FIELD_MAP.items():
        out[dst] = pd.to_numeric(source[src], errors="coerce").astype("float32")
    volume = pd.to_numeric(source["volume"], errors="coerce").replace(0, np.nan)
    raw_vwap = pd.to_numeric(source["amount"], errors="coerce") * 10.0 / volume
    out["$vwap"] = raw_vwap.astype("float32")
    limit_rate = _limit_rate_series_for_frame(source)
    (
        limit_buy,
        limit_sell,
        official_source,
        limit_buy_open,
        limit_sell_open,
        limit_buy_mid_oc,
        limit_sell_mid_oc,
        fallback_buy,
        fallback_sell,
        sealed_fields,
    ) = _official_limit_flags(source, limit_rate)
    out["$limit_rate"] = limit_rate
    out["$up_limit"] = pd.to_numeric(source.get("up_limit"), errors="coerce").astype("float32")
    out["$down_limit"] = pd.to_numeric(source.get("down_limit"), errors="coerce").astype("float32")
    out["$limit_buy"] = limit_buy
    out["$limit_sell"] = limit_sell
    out["$limit_buy_open"] = limit_buy_open
    out["$limit_sell_open"] = limit_sell_open
    out["$limit_buy_mid_oc"] = limit_buy_mid_oc
    out["$limit_sell_mid_oc"] = limit_sell_mid_oc
    out["$one_price_up_limit"] = sealed_fields["one_price_up_limit"]
    out["$one_price_down_limit"] = sealed_fields["one_price_down_limit"]
    out["$limit_turnover_ratio"] = sealed_fields["limit_turnover_ratio"]
    out["$limit_low_liquidity"] = sealed_fields["limit_low_liquidity"]
    out["$limit_buy_open_sealed"] = sealed_fields["limit_buy_open_sealed"]
    out["$limit_sell_open_sealed"] = sealed_fields["limit_sell_open_sealed"]
    out["$limit_buy_fallback"] = fallback_buy
    out["$limit_sell_fallback"] = fallback_sell
    out["$limit_source_official"] = official_source
    if "limit_source_kind" in source.columns:
        out["$limit_source_no_limit"] = source["limit_source_kind"].astype(str).eq("structural_no_limit").astype("float32")
    hit_up, hit_down = _intraday_limit_hits(source)
    out["$hit_up_limit_intraday"] = hit_up
    out["$hit_down_limit_intraday"] = hit_down
    out["datetime"] = source["datetime"]
    out["instrument"] = source["instrument"]
    return out.set_index(["datetime", "instrument"]).sort_index()


def _snapshot_for_compat(
    compat_root: Path,
    *,
    latest_hdf5: str | None = None,
    latest_quantgpt: str | None = None,
) -> dict[str, Any]:
    raw_root = compat_root / "raw"
    qlib_root = compat_root / "qlib"
    quantgpt_root = compat_root / "quantgpt"
    if latest_hdf5 is None:
        latest_hdf5 = _latest_hdf_trade_date_light(raw_root / "stock_daily.h5")
    latest_qlib = None
    cal = qlib_root / "calendars" / "day.txt"
    if cal.exists():
        lines = [line.strip() for line in cal.read_text(encoding="utf-8").splitlines() if line.strip()]
        latest_qlib = lines[-1] if lines else None
    if latest_quantgpt is None and (quantgpt_root / "stocks").exists():
        # Full parquet scans are expensive here. The Tushare converters report
        # latest_date; when unavailable, fall back to the raw HDF latest date.
        latest_quantgpt = latest_hdf5
    benchmarks = sorted((quantgpt_root / "benchmark").glob("*.parquet"))
    return {
        "latest_hdf5_trade_date": latest_hdf5,
        "latest_qlib_trade_date": latest_qlib,
        "latest_quantgpt_trade_date": latest_quantgpt,
        "quantgpt_benchmark_file_count": len(benchmarks),
        "quantgpt_benchmark_files": [p.name for p in benchmarks],
        "quantgpt_contract": quantgpt_contract_report(quantgpt_root / "stocks", sample_limit=None),
        "consumer_readiness": {
            "quantgpt_factor_mining": (quantgpt_root / "stocks").exists() and len(benchmarks) >= 3,
            "qlib_model_training": cal.exists(),
            "qlib_paper_trading": cal.exists(),
            "qlib_model_benchmark_indices": _qlib_index_readiness(
                qlib_root,
                expected_latest=latest_qlib,
            ).get("status") == "passed",
        },
    }


def _prepare_raw_backfill_compat(package_root: Path, manifest: dict[str, Any], compat_root: Path, compat_manifest_path: Path, *, force: bool = False) -> dict[str, Any]:
    raw_source_hdf = package_root / "raw" / "stock_daily.h5"
    raw_source_meta = package_root / "raw" / "metadata.json"
    if not raw_source_hdf.exists():
        raise FileNotFoundError(f"raw backfill HDF missing: {raw_source_hdf}")
    if compat_root.exists() and force:
        shutil.rmtree(compat_root)
    compat_root.mkdir(parents=True, exist_ok=True)

    raw_root = compat_root / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    output_hdf = raw_root / "stock_daily.h5"
    output_meta = raw_root / "metadata.json"
    if output_hdf.exists():
        output_hdf.unlink()
    shutil.copy2(raw_source_hdf, output_hdf)
    if raw_source_meta.exists():
        shutil.copy2(raw_source_meta, output_meta)
    else:
        atomic_write_json(
            output_meta,
            {
                "source": "tushare",
                "schema_version": manifest.get("schema_version", "tushare_v1"),
                "package_id": manifest.get("package_id"),
                "package_kind": manifest.get("package_kind", "raw_backfill"),
                "generated_at": _now(),
            },
        )

    calendar_payload = _write_trading_calendar(
        output_hdf,
        raw_root / "trade_calendar.txt",
        raw_root / "trade_calendar_meta.json",
    )

    from domain.data_foundation.quality_check import check as run_quality_check

    quality_report = run_quality_check(output_hdf, profile="deep_full")
    raw_quality_report = run_quality_check(output_hdf, profile="daily_compat")
    atomic_write_json(compat_root / "quality_report.json", quality_report)
    atomic_write_json(compat_root / "raw_quality_report.json", raw_quality_report)
    if not quality_report.get("passed"):
        raise RuntimeError(f"Raw backfill quality check failed: {quality_report.get('issues')}")
    if not raw_quality_report.get("passed"):
        raise RuntimeError(f"Raw backfill daily compatibility check failed: {raw_quality_report.get('issues')}")

    qlib_step = subprocess.run(
        [sys.executable, str(QLIB_CONVERT_SCRIPT), "--mode", "full", "--source-h5", str(output_hdf), "--output-dir", str(compat_root / "qlib"), "--json"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if qlib_step.returncode != 0:
        raise RuntimeError(f"Qlib compatibility conversion failed: {(qlib_step.stderr or qlib_step.stdout)[-1000:]}")
    qlib_index_step = subprocess.run(
        [sys.executable, str(QLIB_INDEX_CONVERT_SCRIPT), "--mode", "full", "--source-h5", str(output_hdf), "--qlib-dir", str(compat_root / "qlib"), "--json"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if qlib_index_step.returncode != 0:
        raise RuntimeError(f"Qlib index conversion failed: {(qlib_index_step.stderr or qlib_index_step.stdout)[-1000:]}")
    qlib_index_readiness = _qlib_index_readiness(compat_root / "qlib", expected_latest=_latest_hdf_trade_date_light(output_hdf))
    if qlib_index_readiness.get("status") != "passed":
        raise RuntimeError(f"Qlib index artifacts not ready: {qlib_index_readiness.get('issues')}")

    qgpt_result = convert_quantgpt(output_hdf, compat_root / "quantgpt" / "stocks", benchmark_dir=compat_root / "quantgpt" / "benchmark")
    if qgpt_result.get("status") == "failed":
        raise RuntimeError(f"QuantGPT compatibility conversion failed: {qgpt_result.get('error')}")

    with pd.HDFStore(output_hdf, mode="r") as store:
        daily = store.select("/daily", columns=["code", "list_status"])
        index_rows = int(daily["list_status"].astype(str).eq("I").sum()) if "list_status" in daily.columns else 0
        stock_rows = int(len(daily) - index_rows)

    result = {
        "status": "completed",
        "package_id": manifest.get("package_id"),
        "package_root": str(package_root),
        "compat_root": str(compat_root),
        "created_at": _now(),
        "source": "tushare",
        "schema_version": manifest.get("schema_version", "tushare_v1"),
        "package_kind": manifest.get("package_kind", "raw_backfill"),
        "stock_rows": stock_rows,
        "index_rows": index_rows,
        "snapshot": _snapshot_for_compat(
            compat_root,
            latest_hdf5=_latest_hdf_trade_date_light(output_hdf),
            latest_quantgpt=qgpt_result.get("latest_date"),
        ),
        "quantgpt": qgpt_result,
        "trading_calendar": calendar_payload,
        "quality_report": quality_report,
        "raw_quality_report": raw_quality_report,
    }
    atomic_write_json(compat_manifest_path, result)
    return result


@data_job_guard("tushare_prepare_production")
def prepare_tushare_production_artifacts(*, package_id: str | None = None, latest: bool = False, force: bool = False, dry_run: bool = False, chunk_rows: int = 250000) -> dict[str, Any]:
    package_root, manifest = _resolve_tushare_package(package_id=package_id, latest=latest)
    if manifest.get("status") != "completed":
        raise ValueError(f"package not completed: {manifest.get('status')}")
    compat_root = _compat_root(package_root)
    compat_manifest_path = _compat_manifest_path(package_root)
    if compat_manifest_path.exists() and not force:
        existing = _read_json(compat_manifest_path)
        if existing.get("status") == "completed":
            return existing
    if dry_run:
        return {
            "status": "dry_run",
            "package_id": manifest.get("package_id"),
            "package_root": str(package_root),
            "compat_root": str(compat_root),
        }

    if manifest.get("package_kind") in {"status_backfill", "limit_backfill"}:
        if manifest.get("package_kind") == "limit_backfill":
            report = _read_json(package_root / "limit_backfill_report.json")
            coverage = report.get("coverage") or {}
            if not coverage or not coverage.get("passed", False):
                raise RuntimeError(f"Limit backfill coverage check failed: {coverage}")
        return _prepare_raw_backfill_compat(package_root, manifest, compat_root, compat_manifest_path, force=force)

    if compat_root.exists() and force:
        shutil.rmtree(compat_root)
    compat_root.mkdir(parents=True, exist_ok=True)
    silver_root = package_root / "silver"
    research_h5 = silver_root / "research_daily.h5"
    index_h5 = silver_root / "index_daily.h5"
    if not research_h5.exists() or not index_h5.exists():
        raise FileNotFoundError("silver research_daily.h5 or index_daily.h5 missing")

    raw_root = compat_root / "raw"
    output_hdf = raw_root / "stock_daily.h5"
    output_meta = raw_root / "metadata.json"
    if output_hdf.exists():
        output_hdf.unlink()

    raw_carry: dict[str, float] = {}
    adj_carry: dict[str, float] = {}
    stock_rows = 0
    with pd.HDFStore(research_h5, mode="r") as store:
        nrows = int(store.get_storer("/data").nrows or 0)
        for start in range(0, nrows, chunk_rows):
            chunk = store.select("/data", start=start, stop=min(start + chunk_rows, nrows))
            if chunk.empty:
                continue
            normalized, raw_carry, adj_carry = _normalize_stock_chunk(chunk, raw_carry, adj_carry)
            _append_hdf(output_hdf, "/daily", normalized, append=output_hdf.exists(), min_itemsize=MIN_ITEMSIZE)
            stock_rows += int(len(normalized))

    index_df = pd.read_hdf(index_h5, key="/data")
    index_rows = 0
    if not index_df.empty:
        normalized_index = _normalize_index_frame(index_df)
        _append_hdf(output_hdf, "/daily", normalized_index, append=True, min_itemsize=MIN_ITEMSIZE)
        index_rows = int(len(normalized_index))

    _write_compat_metadata(output_meta, package_root, manifest, int(index_df["code"].nunique()) if not index_df.empty else 0)
    calendar_payload = _write_trading_calendar(
        output_hdf,
        raw_root / "trade_calendar.txt",
        raw_root / "trade_calendar_meta.json",
    )
    shutil.copy2(silver_root / "quality_report.json", compat_root / "quality_report.json")
    shutil.copy2(silver_root / "raw_quality_report.json", compat_root / "raw_quality_report.json")

    qlib_step = subprocess.run(
        [sys.executable, str(QLIB_CONVERT_SCRIPT), "--mode", "full", "--source-h5", str(output_hdf), "--output-dir", str(compat_root / "qlib"), "--json"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if qlib_step.returncode != 0:
        raise RuntimeError(f"Qlib compatibility conversion failed: {(qlib_step.stderr or qlib_step.stdout)[-1000:]}")
    qlib_index_step = subprocess.run(
        [sys.executable, str(QLIB_INDEX_CONVERT_SCRIPT), "--mode", "full", "--source-h5", str(output_hdf), "--qlib-dir", str(compat_root / "qlib"), "--json"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if qlib_index_step.returncode != 0:
        raise RuntimeError(f"Qlib index conversion failed: {(qlib_index_step.stderr or qlib_index_step.stdout)[-1000:]}")
    qlib_index_readiness = _qlib_index_readiness(compat_root / "qlib", expected_latest=_latest_hdf_trade_date_light(output_hdf))
    if qlib_index_readiness.get("status") != "passed":
        raise RuntimeError(f"Qlib index artifacts not ready: {qlib_index_readiness.get('issues')}")
    qgpt_result = convert_quantgpt(output_hdf, compat_root / "quantgpt" / "stocks", benchmark_dir=compat_root / "quantgpt" / "benchmark")
    if qgpt_result.get("status") == "failed":
        raise RuntimeError(f"QuantGPT compatibility conversion failed: {qgpt_result.get('error')}")

    result = {
        "status": "completed",
        "package_id": manifest.get("package_id"),
        "package_root": str(package_root),
        "compat_root": str(compat_root),
        "created_at": _now(),
        "source": "tushare",
        "schema_version": manifest.get("schema_version", "tushare_v1"),
        "stock_rows": stock_rows,
        "index_rows": index_rows,
        "snapshot": _snapshot_for_compat(
            compat_root,
            latest_hdf5=_latest_hdf_trade_date_light(output_hdf),
            latest_quantgpt=qgpt_result.get("latest_date"),
        ),
        "quantgpt": qgpt_result,
        "trading_calendar": calendar_payload,
    }
    atomic_write_json(compat_manifest_path, result)
    return result


@data_job_guard("tushare_promote_production")
def promote_tushare_production_artifacts(*, package_id: str | None = None, latest: bool = False, dry_run: bool = False) -> dict[str, Any]:
    package_root, manifest = _resolve_tushare_package(package_id=package_id, latest=latest)
    compat_root = _compat_root(package_root)
    compat_manifest = _read_json(_compat_manifest_path(package_root))
    if compat_manifest.get("status") != "completed":
        raise ValueError("production compatibility artifacts are not ready")
    snapshot = compat_manifest.get("snapshot") or {}
    qlib_index_readiness = _qlib_index_readiness(
        compat_root / "qlib",
        expected_latest=snapshot.get("latest_qlib_trade_date"),
    )
    if qlib_index_readiness.get("status") != "passed":
        if dry_run:
            return {
                "status": "blocked",
                "package_id": manifest.get("package_id"),
                "compat_root": str(compat_root),
                "blockers": ["qlib_index_artifacts_not_ready", *qlib_index_readiness.get("issues", [])],
                "qlib_index_readiness": qlib_index_readiness,
            }
        raise ValueError(f"production Qlib index artifacts are not ready: {qlib_index_readiness.get('issues')}")
    if dry_run:
        return {
            "status": "dry_run",
            "package_id": manifest.get("package_id"),
            "compat_root": str(compat_root),
            "qlib_index_readiness": qlib_index_readiness,
        }

    promotion_id = f"promote-{datetime.now().strftime('%Y%m%d_%H%M%S')}-{manifest.get('package_id')}"
    backup_root = PROMOTION_BACKUP_ROOT / promotion_id
    replaced: list[tuple[Path, Path, bool]] = []
    state_snapshot = _snapshot_state_files([LATEST_STATUS_FILE, CURRENT_PRODUCTION_DATASET_FILE, DAILY_STATUS_FILE])
    _acquire_lock(PRODUCTION_LOCK_DIR)
    try:
        _replace_path(compat_root / "raw" / "stock_daily.h5", PRODUCTION_RAW_HDF5, backup_root, replaced)
        _replace_path(compat_root / "raw" / "metadata.json", PRODUCTION_RAW_METADATA, backup_root, replaced)
        _replace_path(compat_root / "raw" / "trade_calendar.txt", PRODUCTION_TRADING_CALENDAR_FILE, backup_root, replaced)
        _replace_path(compat_root / "raw" / "trade_calendar_meta.json", PRODUCTION_TRADING_CALENDAR_META, backup_root, replaced)
        _replace_path(compat_root / "quality_report.json", PRODUCTION_QUALITY_FILE, backup_root, replaced)
        _replace_path(compat_root / "raw_quality_report.json", PRODUCTION_RAW_QUALITY_FILE, backup_root, replaced)
        _promote_qlib_market_data(compat_root / "qlib", QLIB_DATA_ROOT, backup_root, replaced)
        _replace_path(compat_root / "quantgpt" / "stocks", QUANTGPT_DATA_DIR, backup_root, replaced)
        _replace_path(compat_root / "quantgpt" / "benchmark", QUANTGPT_BENCHMARK_DIR, backup_root, replaced)

        snapshot = compat_manifest.get("snapshot")
        if not isinstance(snapshot, dict) or not snapshot:
            snapshot = _snapshot_for_compat(compat_root)
        else:
            snapshot = dict(snapshot)
        quantgpt_contract = snapshot.get("quantgpt_contract")
        if isinstance(quantgpt_contract, dict):
            quantgpt_contract = dict(quantgpt_contract)
            quantgpt_contract["contract_file"] = str(QUANTGPT_DATA_DIR / "_conversion_contract.json")
            snapshot["quantgpt_contract"] = quantgpt_contract
        artifact_readiness = dict(snapshot.get("artifact_readiness") or snapshot.get("consumer_readiness") or {})
        snapshot["artifact_readiness"] = artifact_readiness
        snapshot["consumer_readiness"] = {name: False for name in artifact_readiness}
        snapshot["consumer_readiness_gate"] = "pending_production_audit"
        production_snapshot = {
            "status": "completed",
            "snapshot": snapshot,
            "steps": [],
            "promoted_from_package_id": manifest.get("package_id"),
            "promotion_id": promotion_id,
        }
        atomic_write_json(LATEST_STATUS_FILE, production_snapshot)
        current_dataset = {
            "status": "production",
            "source": "tushare",
            "schema_version": manifest.get("schema_version", "tushare_v1"),
            "compatibility_mode": "tushare_raw_hdf_compat",
            "updated_at": _now(),
            "latest_trade_date": production_snapshot["snapshot"].get("latest_hdf5_trade_date"),
            "source_target_date": manifest.get("effective_target_date"),
            "production_package_id": manifest.get("package_id"),
            "promotion_id": promotion_id,
            "canonical_read_paths": {
                "production_raw_hdf5": str(PRODUCTION_RAW_HDF5.relative_to(PROJECT_ROOT)),
                "production_raw_metadata": str(PRODUCTION_RAW_METADATA.relative_to(PROJECT_ROOT)),
                "production_trading_calendar": str(PRODUCTION_TRADING_CALENDAR_FILE.relative_to(PROJECT_ROOT)),
                "production_trading_calendar_meta": str(PRODUCTION_TRADING_CALENDAR_META.relative_to(PROJECT_ROOT)),
                "qlib_root": str(QLIB_DATA_ROOT.relative_to(PROJECT_ROOT)),
                "qlib_features": str((QLIB_DATA_ROOT / "features").relative_to(PROJECT_ROOT)),
                "qlib_calendars": str((QLIB_DATA_ROOT / "calendars").relative_to(PROJECT_ROOT)),
                "qlib_instruments": str((QLIB_DATA_ROOT / "instruments").relative_to(PROJECT_ROOT)),
                "quantgpt_stocks": str(QUANTGPT_DATA_DIR.relative_to(PROJECT_ROOT)),
                "quantgpt_benchmark": str(QUANTGPT_BENCHMARK_DIR.relative_to(PROJECT_ROOT)),
                "tushare_quality_report": str(PRODUCTION_QUALITY_FILE.relative_to(PROJECT_ROOT)),
                "tushare_raw_quality_report": str(PRODUCTION_RAW_QUALITY_FILE.relative_to(PROJECT_ROOT)),
            },
            "latest_dates": {
                "hdf5": production_snapshot["snapshot"].get("latest_hdf5_trade_date"),
                "qlib": production_snapshot["snapshot"].get("latest_qlib_trade_date"),
                "quantgpt": production_snapshot["snapshot"].get("latest_quantgpt_trade_date"),
            },
            "required_benchmarks": REQUIRED_BENCHMARKS,
            "artifact_readiness": artifact_readiness,
            "consumer_readiness": {name: False for name in artifact_readiness},
            "consumer_readiness_gate": "pending_production_audit",
            "production_audit": {"status": "pending", "production_package_id": manifest.get("package_id")},
            "do_not_use_as_production": [
                str(STAGING_ROOT.relative_to(PROJECT_ROOT)),
                str(PROMOTION_BACKUP_ROOT.relative_to(PROJECT_ROOT)),
            ],
            "notes": [
                "Production readers must use data/ paths, not runtime/data_foundation/staging.",
                "This production package is sourced from Tushare and exported through a compatibility bridge.",
                "trade_calendar.txt is the canonical production trading calendar for daily orchestration and audits.",
                "Use tushare_quality_report as the canonical quality report for this production package.",
            ],
        }
        atomic_write_json(CURRENT_PRODUCTION_DATASET_FILE, current_dataset)
        payload = {
            "status": "promoted",
            "promotion_id": promotion_id,
            "package_id": manifest.get("package_id"),
            "promoted_at": _now(),
            "backup_root": str(backup_root),
            "snapshot": production_snapshot.get("snapshot", {}),
        }
        _write_daily_status(payload)
        return payload
    except Exception as exc:
        rollback = _rollback(replaced)
        state_rollback = _restore_state_files(state_snapshot)
        if rollback.get("status") != "passed" or state_rollback.get("status") != "passed":
            raise RuntimeError(
                f"tushare_promotion_failed_and_rollback_failed:{exc}:files={rollback.get('errors')}:state={state_rollback.get('errors')}"
            ) from exc
        raise
    finally:
        _release_lock(PRODUCTION_LOCK_DIR)
