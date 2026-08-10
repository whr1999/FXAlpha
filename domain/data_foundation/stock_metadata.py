from __future__ import annotations

import json
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from domain.data_foundation.runtime_io import atomic_write_json
from storage.paths import PRODUCTION_RAW_HDF5, STOCK_IDENTITY_CACHE, STOCK_IDENTITY_CACHE_META


def instrument_to_market_code(instrument: str) -> str:
    inst = str(instrument).strip()
    lower = inst.lower()
    if lower.endswith("sz") and len(lower) >= 8:
        return f"{lower[:6]}.SZ"
    if lower.endswith("sh") and len(lower) >= 8:
        return f"{lower[:6]}.SH"
    if "." in inst:
        code, market = inst.split(".", 1)
        return f"{code.zfill(6)}.{market.upper()}"
    return inst.upper()


def market_code_to_instrument(market_code: str) -> str:
    raw = str(market_code).strip()
    if "." not in raw:
        return raw.lower()
    code, market = raw.split(".", 1)
    suffix = market.strip().lower()
    if suffix in {"sh", "sz"}:
        return f"{code.zfill(6)}{suffix}"
    return raw.lower()


def _cache_is_stale() -> bool:
    if not STOCK_IDENTITY_CACHE.exists() or not STOCK_IDENTITY_CACHE_META.exists():
        return True
    if not PRODUCTION_RAW_HDF5.exists():
        return False
    return STOCK_IDENTITY_CACHE.stat().st_mtime < PRODUCTION_RAW_HDF5.stat().st_mtime


def _normalize_source_frame(df: pd.DataFrame) -> pd.DataFrame:
    source = df.copy()
    if isinstance(source.index, pd.MultiIndex) or source.index.name is not None:
        source = source.reset_index()

    required = {"code", "SECURITY_NAME"}
    missing = sorted(required - set(source.columns))
    if missing:
        raise ValueError(f"production raw daily missing columns: {missing}")

    keep = [col for col in ["code", "SECURITY_NAME", "MARKET_CODE", "LIST_DATE", "list_status", "st_status", "trade_date", "kline_time"] if col in source.columns]
    source = source[keep].copy()
    source = source.dropna(subset=["code", "SECURITY_NAME"])
    source["market_code"] = source["code"].astype(str).map(instrument_to_market_code)
    source["instrument"] = source["market_code"].map(market_code_to_instrument)
    source["security_name"] = source["SECURITY_NAME"].astype(str)

    sort_cols = [col for col in ["trade_date", "kline_time"] if col in source.columns]
    if sort_cols:
        source = source.sort_values(sort_cols)
    latest = source.drop_duplicates("market_code", keep="last").copy()

    out_cols = ["market_code", "instrument", "security_name"]
    optional_map = {
        "MARKET_CODE": "source_market_code",
        "LIST_DATE": "list_date",
        "list_status": "list_status",
        "st_status": "st_status",
        "trade_date": "latest_trade_date",
        "kline_time": "latest_kline_time",
    }
    for src, dst in optional_map.items():
        if src in latest.columns:
            latest[dst] = latest[src]
            out_cols.append(dst)

    return latest[out_cols].sort_values("instrument").reset_index(drop=True)


def build_stock_identity_cache(force: bool = False) -> dict[str, Any]:
    if not force and not _cache_is_stale():
        return stock_identity_cache_status()
    if not PRODUCTION_RAW_HDF5.exists():
        raise FileNotFoundError(f"Production raw HDF5 not found: {PRODUCTION_RAW_HDF5}")

    daily = pd.read_hdf(PRODUCTION_RAW_HDF5, key="daily")
    identity = _normalize_source_frame(daily)

    STOCK_IDENTITY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    working_cache = STOCK_IDENTITY_CACHE.with_name(f".{STOCK_IDENTITY_CACHE.name}.tmp-{os.getpid()}")
    working_cache.unlink(missing_ok=True)
    try:
        identity.to_parquet(working_cache, index=False)
        os.replace(working_cache, STOCK_IDENTITY_CACHE)
    except Exception:
        working_cache.unlink(missing_ok=True)
        raise
    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(PRODUCTION_RAW_HDF5),
        "source_mtime": datetime.fromtimestamp(PRODUCTION_RAW_HDF5.stat().st_mtime).isoformat(timespec="seconds"),
        "cache": str(STOCK_IDENTITY_CACHE),
        "record_count": int(len(identity)),
        "fields": list(identity.columns),
    }
    atomic_write_json(STOCK_IDENTITY_CACHE_META, meta)
    _load_identity_rows.cache_clear()
    return stock_identity_cache_status()


@lru_cache(maxsize=1)
def _load_identity_rows() -> pd.DataFrame:
    if _cache_is_stale():
        build_stock_identity_cache(force=True)
    if not STOCK_IDENTITY_CACHE.exists():
        return pd.DataFrame(columns=["market_code", "instrument", "security_name"])
    return pd.read_parquet(STOCK_IDENTITY_CACHE)


def load_stock_identity_map(auto_build: bool = True) -> dict[str, str]:
    if auto_build and _cache_is_stale():
        build_stock_identity_cache(force=True)
    rows = _load_identity_rows()
    if rows.empty:
        return {}
    by_instrument = rows.set_index("instrument")["security_name"].astype(str).to_dict()
    by_market_code = rows.set_index("market_code")["security_name"].astype(str).to_dict()
    return {**by_market_code, **by_instrument}


def load_stock_identity_rows(auto_build: bool = True) -> pd.DataFrame:
    if auto_build and _cache_is_stale():
        build_stock_identity_cache(force=True)
    return _load_identity_rows().copy()


def load_stock_identity_rows_for_window(start_date: str, end_date: str) -> pd.DataFrame:
    """Load point-in-time identity and status rows for an inclusive trade-date window."""
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if pd.isna(start) or pd.isna(end) or start > end:
        raise ValueError("invalid stock identity date window")
    if not PRODUCTION_RAW_HDF5.exists():
        raise FileNotFoundError(f"Production raw HDF5 not found: {PRODUCTION_RAW_HDF5}")
    source = pd.read_hdf(
        PRODUCTION_RAW_HDF5,
        key="daily",
        where=f"index>=Timestamp('{start.date()}') & index<=Timestamp('{end.date()}')",
        columns=["code", "SECURITY_NAME", "MARKET_CODE", "LIST_DATE", "list_status", "st_status"],
    )
    if source.empty:
        return pd.DataFrame(
            columns=["trade_date", "market_code", "instrument", "security_name", "list_status", "st_status"]
        )
    source = source.reset_index()
    date_column = "trade_date" if "trade_date" in source.columns else source.columns[0]
    source["trade_date"] = pd.to_datetime(source[date_column]).dt.normalize()
    source["market_code"] = source["code"].astype(str).map(instrument_to_market_code)
    source["instrument"] = source["market_code"].map(market_code_to_instrument)
    source["security_name"] = source["SECURITY_NAME"].fillna("").astype(str)
    keep = ["trade_date", "market_code", "instrument", "security_name", "list_status", "st_status"]
    return (
        source[keep]
        .sort_values(["trade_date", "instrument"])
        .drop_duplicates(["trade_date", "market_code"], keep="last")
        .reset_index(drop=True)
    )


def security_name_for_instrument(instrument: str, name_map: dict[str, str] | None = None) -> str:
    mapping = name_map if name_map is not None else load_stock_identity_map()
    market_code = instrument_to_market_code(instrument)
    return mapping.get(str(instrument), "") or mapping.get(market_code, "") or ""


def stock_identity_cache_status() -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if STOCK_IDENTITY_CACHE_META.exists():
        try:
            meta = json.loads(STOCK_IDENTITY_CACHE_META.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    return {
        "available": STOCK_IDENTITY_CACHE.exists(),
        "stale": _cache_is_stale(),
        "cache": str(STOCK_IDENTITY_CACHE),
        "meta": str(STOCK_IDENTITY_CACHE_META),
        "source": str(PRODUCTION_RAW_HDF5),
        "source_exists": PRODUCTION_RAW_HDF5.exists(),
        "record_count": meta.get("record_count"),
        "generated_at": meta.get("generated_at"),
        "fields": meta.get("fields", []),
    }
