from __future__ import annotations

import hashlib
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from domain.data_foundation.ops_common import data_job_guard
from domain.data_foundation.runtime_io import atomic_write_json, read_json
from integrations.tushare.client import get_tushare_client
from storage.paths import DATA_FOUNDATION_ROOT


PACKAGE_PREFIX = "tushare-fullrebuild"
STAGING_ROOT = DATA_FOUNDATION_ROOT / "staging"
STATUS_FILE = DATA_FOUNDATION_ROOT / "tushare_full_rebuild_status.json"
PROXY_ENV_KEYS = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
]
RESEARCH_DAILY_FIELDS = [
    "code",
    "trade_date",
    "name",
    "list_status",
    "st_status",
    "list_date",
    "open",
    "high",
    "low",
    "close",
    "stk_limit_pre_close",
    "up_limit",
    "down_limit",
    "volume",
    "amount",
    "hfq_open",
    "hfq_high",
    "hfq_low",
    "hfq_close",
    "adj_factor",
    "turnover_rate",
    "turnover_rate_f",
    "pe_ttm",
    "pb",
    "ps_ttm",
    "dv_ttm",
    "total_mv",
    "float_mv",
    "total_share",
    "float_share",
    "free_share",
    "eps",
    "net_profit",
    "total_equity",
    "total_assets",
    "roe",
    "roa",
    "holder_num",
    "sm_net_vol",
    "sm_net_amount",
    "lg_net_vol",
    "lg_net_amount",
    "net_mf_vol",
    "net_mf_amount",
    "cost_15pct",
    "cost_85pct",
    "weight_avg",
    "margin_buy_amount",
    "margin_balance",
    "short_balance",
]
RESEARCH_DAILY_UNIT_FIELDS = {
    "volume": "hand",
    "amount": "thousand_cny",
    "total_mv": "ten_thousand_cny",
    "float_mv": "ten_thousand_cny",
    "total_share": "ten_thousand_shares",
    "float_share": "ten_thousand_shares",
    "free_share": "ten_thousand_shares",
    "sm_net_vol": "hand",
    "sm_net_amount": "ten_thousand_cny",
    "lg_net_vol": "hand",
    "lg_net_amount": "ten_thousand_cny",
    "net_mf_vol": "hand",
    "net_mf_amount": "ten_thousand_cny",
    "margin_buy_amount": "cny",
    "margin_balance": "cny",
    "short_balance": "cny",
}
RESEARCH_DAILY_STRING_FIELDS = {"code", "name", "list_status", "st_status", "list_date"}
RESEARCH_DAILY_DATETIME_FIELDS = {"trade_date"}
BENCHMARK_INDEX_CODES = ["000300.SH", "000905.SH", "000852.SH", "000001.SH", "399001.SZ", "399006.SZ", "000016.SH"]
REQUIRED_BENCHMARK_INDEX_CODES = ["000300.SH", "000905.SH", "000852.SH"]
OPTIONAL_BENCHMARK_INDEX_CODES = ["000001.SH", "399001.SZ", "399006.SZ", "000016.SH"]
STOCK_BASIC_FIELDS = "ts_code,name,list_status,list_date,delist_date"
NAMECHANGE_FIELDS = "ts_code,name,start_date,end_date,ann_date,change_reason"
STOCK_BASIC_DOWNLOAD_STATUSES = ["L", "P", "D"]
STOCK_BASIC_TRADABLE_STATUSES = {"L", "P"}
STOCK_ST_FIELDS = "ts_code,name,trade_date,type,type_name"
ST_STATUS_NORMAL = "NORMAL"
ST_STATUS_ST = "ST"
ST_STATUS_DELIST = "DELIST"
DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"
STK_LIMIT_FIELDS = "ts_code,trade_date,pre_close,up_limit,down_limit"
DAILY_BASIC_FIELDS = "ts_code,trade_date,turnover_rate,turnover_rate_f,pe_ttm,pb,ps_ttm,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv"
SUSPEND_D_FIELDS = "ts_code,trade_date,suspend_timing,suspend_type"
INDEX_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"
CYQ_PERF_WINDOW_DAYS = 365


@dataclass
class TushareRebuildConfig:
    start_date: str = "20180101"
    cutoff_date: str = "20260602"
    pad_trading_days: int = 120
    package_id: str | None = None
    resume: bool = True
    dry_run: bool = False
    max_trade_days: int | None = None
    max_codes: int | None = None
    trade_date_sleep_seconds: float = 0.15
    code_sleep_seconds: float = 0.15
    retry_attempts: int = 5
    retry_base_seconds: float = 2.0
    trade_date_chunk_size: int = 40
    proxy_mode: str = "direct"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    return read_json(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _date_slug(value: str) -> str:
    return str(value).replace("-", "")


def _stable_list_sha256(values: list[str]) -> str:
    payload = "\n".join(str(value) for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(8 * 1024**2)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _partition_set_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(_file_sha256(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_code_slug(code: str) -> str:
    return str(code).replace(".", "_").replace("/", "_")


def _normalize_trade_date(text: str) -> str:
    value = _date_slug(text)
    if len(value) != 8 or not value.isdigit():
        raise ValueError(f"invalid_date:{text}")
    return value


def _proxy_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for key in PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            snapshot[key] = value
    return snapshot


@contextmanager
def _proxy_mode(mode: str):
    saved = {key: os.environ.get(key) for key in PROXY_ENV_KEYS}
    normalized = str(mode or "inherit").strip().lower()
    try:
        if normalized == "direct":
            for key in PROXY_ENV_KEYS:
                os.environ.pop(key, None)
        yield
    finally:
        for key in PROXY_ENV_KEYS:
            previous = saved.get(key)
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _default_package_id(cutoff_date: str) -> str:
    return f"{PACKAGE_PREFIX}-{datetime.now().strftime('%Y%m%d_%H%M%S')}-target-{cutoff_date}"


def _package_root(package_id: str) -> Path:
    return STAGING_ROOT / package_id


def _manifest_path(package_root: Path) -> Path:
    return package_root / "manifest.json"


def _metadata_path(package_root: Path) -> Path:
    return _silver_root(package_root) / "metadata.json"


def _progress_path(package_root: Path) -> Path:
    return package_root / "full_rebuild_progress.json"


def _status_payload(**kwargs: Any) -> None:
    _write_json(STATUS_FILE, {"updated_at": _now(), **kwargs})


def _bronze_root(package_root: Path) -> Path:
    return package_root / "bronze" / "tushare_raw"


def _silver_root(package_root: Path) -> Path:
    return package_root / "silver"


def _gold_root(package_root: Path) -> Path:
    return package_root / "gold"


def _endpoint_dir(package_root: Path, endpoint: str) -> Path:
    return _bronze_root(package_root) / endpoint


def _date_file(package_root: Path, endpoint: str, trade_date: str) -> Path:
    return _endpoint_dir(package_root, endpoint) / f"{trade_date}.parquet"


def _code_file(package_root: Path, endpoint: str, code: str) -> Path:
    return _endpoint_dir(package_root, endpoint) / f"{_safe_code_slug(code)}.parquet"


def _ensure_layout(package_root: Path) -> None:
    for path in [
        _bronze_root(package_root),
        _silver_root(package_root),
        _gold_root(package_root) / "qlib",
        _gold_root(package_root) / "quantgpt",
    ]:
        path.mkdir(parents=True, exist_ok=True)


def _retry_call(
    fn: Callable[[], pd.DataFrame | None],
    *,
    attempts: int,
    base_seconds: float,
    stage: str,
    key: str,
) -> pd.DataFrame:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            frame = fn()
            if frame is None:
                return pd.DataFrame()
            if not isinstance(frame, pd.DataFrame):
                return pd.DataFrame(frame)
            return frame
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            sleep_seconds = _retry_sleep_seconds(stage=stage, attempt=attempt, base_seconds=base_seconds, error=exc)
            _status_payload(status="retrying", stage=stage, key=key, attempt=attempt, error=str(exc), sleep_seconds=sleep_seconds)
            time.sleep(sleep_seconds)
    raise RuntimeError(f"tushare_stage_failed:{stage}:{key}:{last_exc}") from last_exc


def _looks_like_rate_limit_error(error: Exception | str) -> bool:
    text = str(error or "").lower()
    return (
        "频率" in str(error or "")
        or "rate limit" in text
        or "rate_limit" in text
        or "too many requests" in text
        or "doc_id=108" in text
        or "200娆" in str(error or "")
    )


def _looks_like_transient_network_error(error: Exception | str) -> bool:
    text = str(error or "").lower()
    return (
        "connection reset by peer" in text
        or "connection aborted" in text
        or "read timed out" in text
        or "timeout" in text
        or "temporarily unavailable" in text
    )


def _retry_sleep_seconds(*, stage: str, attempt: int, base_seconds: float, error: Exception | str) -> float:
    sleep_seconds = base_seconds * attempt
    if _looks_like_rate_limit_error(error):
        if stage == "cyq_perf":
            return max(sleep_seconds, 75.0)
        return max(sleep_seconds, 30.0)
    if _looks_like_transient_network_error(error):
        if stage == "cyq_perf":
            return max(sleep_seconds, 20.0)
        return max(sleep_seconds, 10.0)
    return sleep_seconds


def _stage_sleep_seconds(stage_name: str, default_seconds: float) -> float:
    if stage_name == "cyq_perf":
        return max(default_seconds, 0.40)
    return default_seconds


def _stage_retry_attempts(stage_name: str, default_attempts: int) -> int:
    if stage_name == "cyq_perf":
        return max(default_attempts, 8)
    return default_attempts


def _date_windows(start_date: str, end_date: str, *, window_days: int) -> list[tuple[str, str]]:
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    if end < start:
        return []
    step = max(1, int(window_days))
    windows: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=step - 1), end)
        windows.append((cursor.strftime("%Y%m%d"), window_end.strftime("%Y%m%d")))
        cursor = window_end + timedelta(days=1)
    return windows


def _fetch_cyq_perf_windowed(pro, *, code: str, start_date: str, end_date: str, fields: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for chunk_start, chunk_end in _date_windows(start_date, end_date, window_days=CYQ_PERF_WINDOW_DAYS):
        frame = pro.cyq_perf(
            ts_code=code,
            start_date=chunk_start,
            end_date=chunk_end,
            fields=fields,
        )
        if frame is None or frame.empty:
            continue
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    if {"ts_code", "trade_date"}.issubset(combined.columns):
        combined = combined.drop_duplicates(subset=["ts_code", "trade_date"]).sort_values(["trade_date"]).reset_index(drop=True)
    return combined


def _empty_if_none(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    return frame


def _filter_selected_codes(frame: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    if frame.empty or "ts_code" not in frame.columns or not codes:
        return frame
    allowed = set(str(code) for code in codes)
    return frame[frame["ts_code"].astype(str).isin(allowed)].reset_index(drop=True)


def _is_delist_name(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.contains("退市", regex=False)


def _is_st_name(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.contains(r"^(?:\*?ST|SST)", case=False, regex=True)


def _normalize_stock_basic_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["ts_code", "name", "list_status", "list_date", "delist_date"]
    out = frame.copy() if frame is not None else pd.DataFrame(columns=columns)
    for column in columns:
        if column not in out.columns:
            out[column] = pd.NA
    out = out[columns].copy()
    out["ts_code"] = out["ts_code"].astype(str).str.strip()
    out = out[out["ts_code"].ne("") & out["ts_code"].ne("nan")].copy()
    out["list_status"] = out["list_status"].astype(str).str.strip().str.upper()
    return out.drop_duplicates(subset=["ts_code"], keep="first").reset_index(drop=True)


def _normalize_namechange_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    columns = ["ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"]
    out = frame.copy() if frame is not None else pd.DataFrame(columns=columns)
    for column in columns:
        if column not in out.columns:
            out[column] = pd.NA
    out = out[columns].copy()
    out["ts_code"] = out["ts_code"].astype(str).str.strip()
    out = out[out["ts_code"].ne("") & out["ts_code"].ne("nan")].copy()
    out["start_date"] = pd.to_datetime(out["start_date"].astype("string").str.replace("-", "", regex=False), format="%Y%m%d", errors="coerce").dt.normalize()
    out["end_date"] = pd.to_datetime(out["end_date"].astype("string").str.replace("-", "", regex=False), format="%Y%m%d", errors="coerce").dt.normalize()
    out = out.dropna(subset=["start_date"])
    out = out.drop_duplicates(subset=["ts_code", "name", "start_date", "end_date", "change_reason"], keep="first")
    return out.sort_values(["ts_code", "start_date", "end_date"], na_position="last").reset_index(drop=True)


def _fetch_stock_basic_statuses(pro) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for status in STOCK_BASIC_DOWNLOAD_STATUSES:
        frame = pro.stock_basic(list_status=status, fields=STOCK_BASIC_FIELDS)
        if frame is None:
            frame = pd.DataFrame(columns=STOCK_BASIC_FIELDS.split(","))
        frame = frame.copy()
        if "list_status" not in frame.columns:
            frame["list_status"] = status
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=STOCK_BASIC_FIELDS.split(","))
    combined = pd.concat(frames, ignore_index=True)
    return _normalize_stock_basic_frame(combined)


def _tradable_stock_basic(stock_basic: pd.DataFrame, *, as_of_date: str | None = None) -> pd.DataFrame:
    if stock_basic.empty:
        return stock_basic
    frame = _normalize_stock_basic_frame(stock_basic)
    tradable = frame["list_status"].isin(STOCK_BASIC_TRADABLE_STATUSES)
    if as_of_date:
        as_of = pd.to_datetime(str(as_of_date).replace("-", ""), format="%Y%m%d", errors="coerce")
        if not pd.isna(as_of):
            delist_dates = pd.to_datetime(
                frame["delist_date"].astype("string").str.replace("-", "", regex=False),
                format="%Y%m%d",
                errors="coerce",
            )
            listed_by_target = pd.to_datetime(
                frame["list_date"].astype("string").str.replace("-", "", regex=False),
                format="%Y%m%d",
                errors="coerce",
            ).le(as_of)
            delisted_after_target = frame["list_status"].eq("D") & delist_dates.notna() & delist_dates.gt(as_of)
            tradable = tradable | (listed_by_target & delisted_after_target)
    return frame[tradable].reset_index(drop=True)


def _apply_status_fields(
    frame: pd.DataFrame,
    *,
    stock_basic_df: pd.DataFrame | None = None,
    stock_st_df: pd.DataFrame | None = None,
    namechange_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    out = frame.copy()
    if out.empty:
        if "st_status" not in out.columns:
            out["st_status"] = pd.Series(dtype="string")
        return out

    if "code" not in out.columns and "ts_code" in out.columns:
        out["code"] = out["ts_code"].astype(str)
    out["code"] = out["code"].astype(str).str.strip()

    if stock_basic_df is not None and not stock_basic_df.empty:
        status_basic = _normalize_stock_basic_frame(stock_basic_df).rename(columns={"ts_code": "code"})
        status_basic = status_basic[["code", "name", "list_status", "list_date", "delist_date"]].drop_duplicates("code").set_index("code")
        mapped_name = out["code"].map(status_basic["name"]) if "name" in status_basic.columns else pd.Series(pd.NA, index=out.index)
        mapped_status = out["code"].map(status_basic["list_status"]) if "list_status" in status_basic.columns else pd.Series(pd.NA, index=out.index)
        mapped_list_date = out["code"].map(status_basic["list_date"]) if "list_date" in status_basic.columns else pd.Series(pd.NA, index=out.index)
        mapped_delist_date = out["code"].map(status_basic["delist_date"]) if "delist_date" in status_basic.columns else pd.Series(pd.NA, index=out.index)
        if "name" in out.columns:
            name_missing = out["name"].isna()
            out.loc[name_missing, "name"] = mapped_name.loc[name_missing]
        else:
            out["name"] = mapped_name
        if "list_status" not in out.columns:
            out["list_status"] = mapped_status
        else:
            current_status = out["list_status"].astype("string").str.strip().str.upper()
            replace_status = mapped_status.notna() & ~current_status.eq("I")
            out.loc[replace_status, "list_status"] = mapped_status.loc[replace_status]
        if "list_date" in out.columns:
            out["list_date"] = out["list_date"].combine_first(mapped_list_date)
        else:
            out["list_date"] = mapped_list_date
        if "delist_date" in out.columns:
            out["delist_date"] = out["delist_date"].combine_first(mapped_delist_date)
        else:
            out["delist_date"] = mapped_delist_date

    if "list_status" not in out.columns:
        out["list_status"] = pd.NA
    if "name" not in out.columns:
        out["name"] = pd.NA
    out["list_status"] = out["list_status"].astype("string").str.strip().str.upper()
    if "trade_date" in out.columns and "delist_date" in out.columns:
        out_dates = pd.to_datetime(out["trade_date"], errors="coerce").dt.normalize()
        delist_dates = pd.to_datetime(
            out["delist_date"].astype("string").str.replace("-", "", regex=False),
            format="%Y%m%d",
            errors="coerce",
        ).dt.normalize()
        current_status = out["list_status"].astype("string").str.strip().str.upper()
        listed_before_delist = delist_dates.isna() | (out_dates <= delist_dates)
        overwrite_listed = current_status.ne("I") & listed_before_delist
        out.loc[overwrite_listed, "list_status"] = "L"
        out.loc[current_status.ne("I") & delist_dates.notna() & (out_dates > delist_dates), "list_status"] = "D"
        out["list_status"] = out["list_status"].astype("string").str.strip().str.upper()
    else:
        out_dates = pd.to_datetime(out["trade_date"], errors="coerce").dt.normalize() if "trade_date" in out.columns else pd.Series(pd.NaT, index=out.index)

    st_hit = pd.Series(False, index=out.index)
    namechange_st_hit = pd.Series(False, index=out.index)
    namechange_delist_hit = pd.Series(False, index=out.index)
    has_pit_st_table = False
    if stock_st_df is not None and not stock_st_df.empty and {"ts_code", "trade_date"}.issubset(stock_st_df.columns) and "trade_date" in out.columns:
        has_pit_st_table = True
        st_pairs = stock_st_df[["ts_code", "trade_date"]].copy()
        st_pairs["code"] = st_pairs["ts_code"].astype(str).str.strip()
        st_pairs["trade_date"] = pd.to_datetime(st_pairs["trade_date"], format="%Y%m%d", errors="coerce").dt.normalize()
        st_keys = set(zip(st_pairs["code"], st_pairs["trade_date"]))
        out_dates = pd.to_datetime(out["trade_date"], errors="coerce").dt.normalize()
        st_hit = pd.Series(list(zip(out["code"], out_dates)), index=out.index).isin(st_keys)

    namechanges = _normalize_namechange_frame(namechange_df)
    if not namechanges.empty and "trade_date" in out.columns:
        chunk_codes = set(out["code"].astype(str).unique())
        for _, change in namechanges[namechanges["ts_code"].isin(chunk_codes)].iterrows():
            code = str(change["ts_code"])
            start = change["start_date"]
            end = change["end_date"]
            if pd.isna(start):
                continue
            in_range = out["code"].eq(code) & out_dates.ge(start)
            if not pd.isna(end):
                in_range &= out_dates.le(end)
            if not in_range.any():
                continue
            change_name = str(change.get("name") or "")
            reason = str(change.get("change_reason") or "")
            out.loc[in_range, "name"] = change_name
            if _is_st_name(pd.Series([change_name])).iloc[0] or "ST" in reason.upper():
                namechange_st_hit |= in_range
            if _is_delist_name(pd.Series([change_name])).iloc[0] or "终止上市" in reason:
                namechange_delist_hit |= in_range

    delist_hit = out["list_status"].eq("D")
    # SECURITY_NAME/stock_basic.name are not guaranteed point-in-time. Use name
    # matching only as a fallback when PIT status/namechange tables are unavailable.
    has_pit_namechange = not namechanges.empty
    st_name_hit = _is_st_name(out["name"]) if not has_pit_st_table and not has_pit_namechange else pd.Series(False, index=out.index)
    delist_name_hit = _is_delist_name(out["name"]) if not has_pit_namechange else pd.Series(False, index=out.index)
    out["st_status"] = ST_STATUS_NORMAL
    out.loc[st_hit | namechange_st_hit | st_name_hit, "st_status"] = ST_STATUS_ST
    out.loc[delist_hit | namechange_delist_hit | delist_name_hit, "st_status"] = ST_STATUS_DELIST
    out["st_status"] = out["st_status"].astype("string")
    return out


def _exclude_bj_codes(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "ts_code" not in frame.columns:
        return frame
    codes = frame["ts_code"].astype(str)
    return frame[~codes.str.endswith(".BJ")].reset_index(drop=True)


def _fetch_trade_calendar(pro, *, start_date: str, end_date: str) -> list[str]:
    df = pro.trade_cal(exchange="SSE", start_date=start_date, end_date=end_date, is_open="1", fields="cal_date,is_open")
    if df is None or df.empty:
        return []
    return sorted(str(v) for v in df["cal_date"].astype(str).tolist())


def tushare_preflight(
    *,
    start_date: str,
    cutoff_date: str,
    pad_trading_days: int = 120,
    max_trade_days: int | None = None,
    max_codes: int | None = None,
    proxy_mode: str = "direct",
    client=None,
    network_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start_date = _normalize_trade_date(start_date)
    cutoff_date = _normalize_trade_date(cutoff_date)
    calendar_probe_start = (datetime.strptime(start_date, "%Y%m%d") - timedelta(days=365)).strftime("%Y%m%d")
    with _proxy_mode(proxy_mode):
        if client is not None:
            pro = client
        elif network_report is not None:
            pro = get_tushare_client(network_mode=proxy_mode, network_report=network_report)
        else:
            pro = get_tushare_client(network_mode=proxy_mode)
        open_dates = _fetch_trade_calendar(pro, start_date=calendar_probe_start, end_date=cutoff_date)
        if not open_dates:
            raise RuntimeError("tushare_trade_calendar_empty")
        target_dates = [value for value in open_dates if start_date <= value <= cutoff_date]
        if not target_dates:
            raise RuntimeError("tushare_target_dates_empty")
        effective_target_date = target_dates[-1]
        first_target_idx = open_dates.index(target_dates[0])
        padded_idx = max(0, first_target_idx - max(0, int(pad_trading_days)))
        padded_start_date = open_dates[padded_idx]
        status_universe = _fetch_stock_basic_statuses(pro)
        universe = _tradable_stock_basic(status_universe, as_of_date=effective_target_date)
        resolved_network_report = network_report or getattr(pro, "network_report", None)
    if universe is None:
        universe = pd.DataFrame(columns=["ts_code"])
    universe = _exclude_bj_codes(universe)
    codes = sorted(str(v).strip() for v in universe.get("ts_code", pd.Series(dtype=str)).tolist() if str(v).strip())
    status_codes = []
    if "status_universe" in locals() and status_universe is not None and not status_universe.empty:
        status_codes = sorted(str(v).strip() for v in _exclude_bj_codes(status_universe).get("ts_code", pd.Series(dtype=str)).tolist() if str(v).strip())
    if max_codes is not None:
        codes = codes[: max(0, int(max_codes))]
    if max_trade_days is not None:
        target_dates = target_dates[: max(0, int(max_trade_days))]
    selected_target_date = target_dates[-1] if target_dates else None
    return {
        "status": "ok",
        "start_date": start_date,
        "cutoff_date": cutoff_date,
        "effective_target_date": effective_target_date,
        "selected_target_date": selected_target_date,
        "padded_start_date": padded_start_date,
        "trade_date_count": len(target_dates),
        "trade_dates": target_dates,
        "code_count": len(codes),
        "codes": codes,
        "codes_sha256": _stable_list_sha256(codes),
        "status_code_count": len(status_codes),
        "status_codes": status_codes,
        "status_codes_sha256": _stable_list_sha256(status_codes),
        "proxy_mode": proxy_mode,
        "proxy_env": _proxy_snapshot(),
        "calendar_probe_start": calendar_probe_start,
        "trade_dates_sha256": _stable_list_sha256(target_dates),
        "network": resolved_network_report,
    }


def _initial_progress(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "initialized",
        "package_id": plan["package_id"],
        "updated_at": _now(),
        "stages": {
            "stock_basic": {"cursor": 0, "total": 1, "status": "pending"},
            "daily": {"cursor": 0, "total": len(plan["trade_dates"]), "status": "pending"},
            "stk_limit": {"cursor": 0, "total": len(plan["trade_dates"]), "status": "pending"},
            "daily_basic": {"cursor": 0, "total": len(plan["trade_dates"]), "status": "pending"},
            "stock_st": {"cursor": 0, "total": len(plan["trade_dates"]), "status": "pending"},
            "suspend_d": {"cursor": 0, "total": len(plan["trade_dates"]), "status": "pending"},
            "adj_factor": {"cursor": 0, "total": len(plan["trade_dates"]), "status": "pending"},
            "moneyflow": {"cursor": 0, "total": len(plan["trade_dates"]), "status": "pending"},
            "margin_detail": {"cursor": 0, "total": len(plan["trade_dates"]), "status": "pending"},
            "pro_bar_hfq": {"cursor": 0, "total": len(plan["codes"]), "status": "pending"},
            "income": {"cursor": 0, "total": len(plan["codes"]), "status": "pending"},
            "balancesheet": {"cursor": 0, "total": len(plan["codes"]), "status": "pending"},
            "fina_indicator": {"cursor": 0, "total": len(plan["codes"]), "status": "pending"},
            "holder_num": {"cursor": 0, "total": len(plan["codes"]), "status": "pending"},
            "cyq_perf": {"cursor": 0, "total": len(plan["codes"]), "status": "pending"},
            "index_daily": {"cursor": 0, "total": len(BENCHMARK_INDEX_CODES), "status": "pending"},
            "raw_quality_report": {"cursor": 0, "total": 1, "status": "pending"},
            "assemble_research_daily": {"cursor": 0, "total": 0, "status": "pending"},
            "quality_report": {"cursor": 0, "total": 1, "status": "pending"},
        },
    }


def _normalize_progress(progress: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    expected = _initial_progress(plan)
    normalized = expected.copy()
    normalized.update({key: value for key, value in progress.items() if key != "stages"})
    normalized["stages"] = expected["stages"]
    existing_stages = progress.get("stages") or {}
    for stage_name, default_stage in expected["stages"].items():
        stage = dict(default_stage)
        stage.update(existing_stages.get(stage_name) or {})
        normalized["stages"][stage_name] = stage
    return normalized


def _write_manifest(package_root: Path, plan: dict[str, Any]) -> None:
    manifest = {
        "package_id": plan["package_id"],
        "status": "initialized",
        "created_at": _now(),
        "source": "tushare",
        "schema_version": "tushare_v1",
        "remote_only": True,
        "start_date": plan["start_date"],
        "cutoff_date": plan["cutoff_date"],
        "effective_target_date": plan["effective_target_date"],
        "selected_target_date": plan["selected_target_date"],
        "padded_start_date": plan["padded_start_date"],
        "proxy_mode": plan["proxy_mode"],
        "proxy_env": plan["proxy_env"],
        "trade_date_chunk_size": plan["trade_date_chunk_size"],
        "trade_date_count": plan["trade_date_count"],
        "trade_dates_sha256": plan["trade_dates_sha256"],
        "code_count": plan["code_count"],
        "codes_sha256": plan["codes_sha256"],
        "network": plan.get("network"),
        "research_daily_fields": RESEARCH_DAILY_FIELDS,
        "research_daily_units": RESEARCH_DAILY_UNIT_FIELDS,
    }
    _write_json(_manifest_path(package_root), manifest)


def _update_manifest(package_root: Path, **updates: Any) -> None:
    payload = _read_json(_manifest_path(package_root))
    payload.update(updates)
    payload["updated_at"] = _now()
    _write_json(_manifest_path(package_root), payload)


def _resume_manifest_mismatches(existing: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    keys = [
        "source",
        "schema_version",
        "remote_only",
        "start_date",
        "cutoff_date",
        "effective_target_date",
        "selected_target_date",
        "padded_start_date",
        "proxy_mode",
        "trade_date_chunk_size",
        "trade_date_count",
        "trade_dates_sha256",
        "code_count",
        "codes_sha256",
    ]
    expected = {
        "source": "tushare",
        "schema_version": "tushare_v1",
        "remote_only": True,
        "start_date": plan["start_date"],
        "cutoff_date": plan["cutoff_date"],
        "effective_target_date": plan["effective_target_date"],
        "selected_target_date": plan["selected_target_date"],
        "padded_start_date": plan["padded_start_date"],
        "proxy_mode": plan["proxy_mode"],
        "trade_date_chunk_size": plan["trade_date_chunk_size"],
        "trade_date_count": plan["trade_date_count"],
        "trade_dates_sha256": plan["trade_dates_sha256"],
        "code_count": plan["code_count"],
        "codes_sha256": plan["codes_sha256"],
    }
    mismatches: list[str] = []
    for key in keys:
        actual = existing.get(key)
        wanted = expected.get(key)
        if actual != wanted:
            mismatches.append(f"{key}:{actual!r}!={wanted!r}")
    return mismatches


def _save_progress(package_root: Path, progress: dict[str, Any]) -> None:
    progress["updated_at"] = _now()
    _write_json(_progress_path(package_root), progress)


def _load_or_init_package(config: TushareRebuildConfig, plan: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    package_id = config.package_id or _default_package_id(plan["effective_target_date"])
    plan["package_id"] = package_id
    package_root = _package_root(package_id)
    if package_root.exists() and config.resume:
        existing_manifest = _read_json(_manifest_path(package_root))
        if not existing_manifest:
            _write_manifest(package_root, plan)
            existing_manifest = _read_json(_manifest_path(package_root))
        if existing_manifest:
            mismatches = _resume_manifest_mismatches(existing_manifest, plan)
            if mismatches:
                raise RuntimeError(f"tushare_resume_manifest_mismatch:{';'.join(mismatches)}")
        progress = _read_json(_progress_path(package_root))
        if not progress:
            progress = _initial_progress(plan)
        progress = _normalize_progress(progress, plan)
        _save_progress(package_root, progress)
        return package_root, progress
    _ensure_layout(package_root)
    _write_manifest(package_root, plan)
    progress = _initial_progress(plan)
    _save_progress(package_root, progress)
    return package_root, progress


def _write_frame(path: Path, frame: pd.DataFrame, columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = frame.copy()
    if columns is not None:
        for column in columns:
            if column not in out.columns:
                out[column] = pd.Series(dtype="float64")
        out = out[columns]
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.unlink(missing_ok=True)
    try:
        out.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _run_single_frame_stage(
    *,
    package_root: Path,
    progress: dict[str, Any],
    stage_name: str,
    loader: Callable[[], pd.DataFrame],
    path: Path,
    columns: list[str] | None = None,
) -> None:
    stage = progress["stages"][stage_name]
    if stage["cursor"] >= stage["total"]:
        stage["status"] = "completed"
        _save_progress(package_root, progress)
        return
    stage["status"] = "running"
    _save_progress(package_root, progress)
    frame = loader()
    _write_frame(path, frame, columns)
    stage["cursor"] = stage["total"]
    stage["status"] = "completed"
    _save_progress(package_root, progress)


def _run_trade_date_stage(
    *,
    package_root: Path,
    progress: dict[str, Any],
    stage_name: str,
    trade_dates: list[str],
    sleep_seconds: float,
    fetcher: Callable[[str], pd.DataFrame],
    columns: list[str] | None = None,
) -> None:
    stage = progress["stages"][stage_name]
    stage["total"] = len(trade_dates)
    for idx in range(stage["cursor"], len(trade_dates)):
        trade_date = trade_dates[idx]
        stage["status"] = "running"
        stage["current_key"] = trade_date
        _save_progress(package_root, progress)
        frame = fetcher(trade_date)
        _write_frame(_date_file(package_root, stage_name, trade_date), frame, columns)
        stage["cursor"] = idx + 1
        stage["status"] = "completed" if stage["cursor"] >= stage["total"] else "running"
        _save_progress(package_root, progress)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


def _run_code_stage(
    *,
    package_root: Path,
    progress: dict[str, Any],
    stage_name: str,
    codes: list[str],
    sleep_seconds: float,
    fetcher: Callable[[str], pd.DataFrame],
    columns: list[str] | None = None,
    refresh_every: int | None = None,
    refresh_hook: Callable[[], None] | None = None,
) -> None:
    stage = progress["stages"][stage_name]
    stage["total"] = len(codes)
    start_cursor = int(stage["cursor"])
    for idx in range(start_cursor, len(codes)):
        code = codes[idx]
        if refresh_hook is not None and refresh_every and idx > start_cursor and idx % refresh_every == 0:
            refresh_hook()
        stage["status"] = "running"
        stage["current_key"] = code
        _save_progress(package_root, progress)
        frame = fetcher(code)
        _write_frame(_code_file(package_root, stage_name, code), frame, columns)
        stage["cursor"] = idx + 1
        stage["status"] = "completed" if stage["cursor"] >= stage["total"] else "running"
        _save_progress(package_root, progress)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


def _load_partition_frames(root: Path) -> pd.DataFrame:
    parts = sorted(root.glob("*.parquet"))
    if not parts:
        return pd.DataFrame()
    frames = [pd.read_parquet(path) for path in parts]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _prepare_effective_date(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    effective = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns]")
    for candidate in ["f_ann_date", "ann_date", "trade_date", "end_date"]:
        if candidate in out.columns:
            values = pd.to_datetime(out[candidate], format="%Y%m%d", errors="coerce")
            effective = effective.fillna(values)
    out["effective_date"] = effective
    return out


def _merge_pit(
    base: pd.DataFrame,
    source: pd.DataFrame,
    *,
    fields: list[str],
) -> pd.DataFrame:
    if base.empty:
        return base
    if source.empty:
        for field in fields:
            if field not in base.columns:
                base[field] = pd.NA
        return base
    work = source.copy()
    work = _prepare_effective_date(work)
    work["code"] = work["ts_code"].astype(str)
    if "end_date" in work.columns:
        work["source_end_date"] = pd.to_datetime(work["end_date"], format="%Y%m%d", errors="coerce")
    else:
        work["source_end_date"] = pd.NaT
    keep = ["code", "effective_date", "source_end_date", *[field for field in fields if field in work.columns]]
    work = work[keep].dropna(subset=["effective_date"]).sort_values(["code", "effective_date", "source_end_date"]).drop_duplicates(
        subset=["code", "effective_date"], keep="last"
    ).sort_values(["effective_date", "code"]).reset_index(drop=True)
    left = base.dropna(subset=["trade_date"]).sort_values(["trade_date", "code"]).reset_index(drop=True)
    merged = pd.merge_asof(
        left,
        work,
        by="code",
        left_on="trade_date",
        right_on="effective_date",
        direction="backward",
        allow_exact_matches=True,
    )
    drop_columns = [column for column in ["effective_date", "source_end_date"] if column in merged.columns]
    if drop_columns:
        merged = merged.drop(columns=drop_columns)
    return merged


def _assemble_research_daily_chunk(
    *,
    daily_df: pd.DataFrame,
    stock_basic_df: pd.DataFrame,
    stock_st_df: pd.DataFrame | None = None,
    namechange_df: pd.DataFrame | None = None,
    hfq_df: pd.DataFrame,
    adj_df: pd.DataFrame,
    daily_basic_df: pd.DataFrame,
    moneyflow_df: pd.DataFrame,
    margin_df: pd.DataFrame,
    cyq_perf_df: pd.DataFrame,
    income_df: pd.DataFrame,
    balancesheet_df: pd.DataFrame,
    fina_indicator_df: pd.DataFrame,
    holder_df: pd.DataFrame,
    stk_limit_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if daily_df.empty:
        return pd.DataFrame(columns=RESEARCH_DAILY_FIELDS)

    base = daily_df.copy()
    base["code"] = base["ts_code"].astype(str)
    base["trade_date"] = pd.to_datetime(base["trade_date"], format="%Y%m%d", errors="coerce")
    base = base.rename(columns={"vol": "volume"})
    base = base[["code", "trade_date", "open", "high", "low", "close", "volume", "amount"]]

    stock_basic = stock_basic_df.rename(columns={"ts_code": "code"})[
        [column for column in ["code", "name", "list_status", "list_date"] if column in stock_basic_df.columns or column == "code"]
    ].drop_duplicates(subset=["code"])
    base = base.merge(stock_basic, on="code", how="left")
    base = _apply_status_fields(base, stock_basic_df=stock_basic_df, stock_st_df=stock_st_df, namechange_df=namechange_df)

    if not hfq_df.empty:
        hfq = hfq_df.copy()
        hfq["code"] = hfq["ts_code"].astype(str)
        hfq["trade_date"] = pd.to_datetime(hfq["trade_date"], format="%Y%m%d", errors="coerce")
        hfq = hfq.rename(
            columns={
                "open": "hfq_open",
                "high": "hfq_high",
                "low": "hfq_low",
                "close": "hfq_close",
            }
        )
        base = base.merge(hfq[["code", "trade_date", "hfq_open", "hfq_high", "hfq_low", "hfq_close"]], on=["code", "trade_date"], how="left")

    if not adj_df.empty:
        adj = adj_df.copy()
        adj["code"] = adj["ts_code"].astype(str)
        adj["trade_date"] = pd.to_datetime(adj["trade_date"], format="%Y%m%d", errors="coerce")
        base = base.merge(adj[["code", "trade_date", "adj_factor"]], on=["code", "trade_date"], how="left")

    adj_factor_values = (
        pd.to_numeric(base["adj_factor"], errors="coerce")
        if "adj_factor" in base.columns
        else pd.Series(float("nan"), index=base.index, dtype="float64")
    )
    for raw_field, hfq_field in {
        "open": "hfq_open",
        "high": "hfq_high",
        "low": "hfq_low",
        "close": "hfq_close",
    }.items():
        derived = pd.to_numeric(base[raw_field], errors="coerce") * adj_factor_values
        if hfq_field in base.columns:
            base[hfq_field] = pd.to_numeric(base[hfq_field], errors="coerce").combine_first(derived)
        else:
            base[hfq_field] = derived

    if not daily_basic_df.empty:
        day_basic = daily_basic_df.copy()
        day_basic["code"] = day_basic["ts_code"].astype(str)
        day_basic["trade_date"] = pd.to_datetime(day_basic["trade_date"], format="%Y%m%d", errors="coerce")
        day_basic = day_basic.rename(columns={"circ_mv": "float_mv"})
        keep = [
            "code",
            "trade_date",
            "turnover_rate",
            "turnover_rate_f",
            "pe_ttm",
            "pb",
            "ps_ttm",
            "dv_ttm",
            "total_mv",
            "float_mv",
            "total_share",
            "float_share",
            "free_share",
        ]
        base = base.merge(day_basic[[column for column in keep if column in day_basic.columns]], on=["code", "trade_date"], how="left")

    stk_limit_df = stk_limit_df if stk_limit_df is not None else pd.DataFrame()
    if not stk_limit_df.empty:
        limit_price = stk_limit_df.copy()
        limit_price["code"] = limit_price["ts_code"].astype(str)
        limit_price["trade_date"] = pd.to_datetime(limit_price["trade_date"], format="%Y%m%d", errors="coerce")
        limit_price = limit_price.rename(columns={"pre_close": "stk_limit_pre_close"})
        base = base.merge(
            limit_price[
                [
                    column
                    for column in ["code", "trade_date", "stk_limit_pre_close", "up_limit", "down_limit"]
                    if column in limit_price.columns
                ]
            ],
            on=["code", "trade_date"],
            how="left",
        )

    if not moneyflow_df.empty:
        flow = moneyflow_df.copy()
        flow["code"] = flow["ts_code"].astype(str)
        flow["trade_date"] = pd.to_datetime(flow["trade_date"], format="%Y%m%d", errors="coerce")
        flow["sm_net_vol"] = flow.get("buy_sm_vol", 0) - flow.get("sell_sm_vol", 0)
        flow["sm_net_amount"] = flow.get("buy_sm_amount", 0) - flow.get("sell_sm_amount", 0)
        flow["lg_net_vol"] = flow.get("buy_lg_vol", 0) - flow.get("sell_lg_vol", 0)
        flow["lg_net_amount"] = flow.get("buy_lg_amount", 0) - flow.get("sell_lg_amount", 0)
        keep = [
            "code",
            "trade_date",
            "sm_net_vol",
            "sm_net_amount",
            "lg_net_vol",
            "lg_net_amount",
            "net_mf_vol",
            "net_mf_amount",
        ]
        base = base.merge(flow[[column for column in keep if column in flow.columns]], on=["code", "trade_date"], how="left")

    if not margin_df.empty:
        margin = margin_df.copy()
        margin["code"] = margin["ts_code"].astype(str)
        margin["trade_date"] = pd.to_datetime(margin["trade_date"], format="%Y%m%d", errors="coerce")
        margin = margin.rename(
            columns={
                "rzmre": "margin_buy_amount",
                "rzye": "margin_balance",
                "rqye": "short_balance",
            }
        )
        base = base.merge(
            margin[[column for column in ["code", "trade_date", "margin_buy_amount", "margin_balance", "short_balance"] if column in margin.columns]],
            on=["code", "trade_date"],
            how="left",
        )

    if not cyq_perf_df.empty:
        chips = cyq_perf_df.copy()
        chips["code"] = chips["ts_code"].astype(str)
        chips["trade_date"] = pd.to_datetime(chips["trade_date"], format="%Y%m%d", errors="coerce")
        base = base.merge(
            chips[[column for column in ["code", "trade_date", "cost_15pct", "cost_85pct", "weight_avg"] if column in chips.columns]],
            on=["code", "trade_date"],
            how="left",
        )

    base = _merge_pit(base, fina_indicator_df, fields=["eps", "roe", "roa"])
    base = _merge_pit(base, income_df, fields=["n_income_attr_p", "basic_eps"])
    base = _merge_pit(base, balancesheet_df, fields=["total_hldr_eqy_exc_min_int", "total_assets"])
    base = _merge_pit(base, holder_df, fields=["holder_num"])

    if "basic_eps" in base.columns:
        base["eps"] = base["eps"].combine_first(base["basic_eps"]) if "eps" in base.columns else base["basic_eps"]
        base = base.drop(columns=["basic_eps"])
    if "n_income_attr_p" in base.columns:
        base = base.rename(columns={"n_income_attr_p": "net_profit"})
    if "total_hldr_eqy_exc_min_int" in base.columns:
        base = base.rename(columns={"total_hldr_eqy_exc_min_int": "total_equity"})

    for field in RESEARCH_DAILY_FIELDS:
        if field not in base.columns:
            base[field] = pd.NA
    base = base[RESEARCH_DAILY_FIELDS].sort_values(["trade_date", "code"]).reset_index(drop=True)
    for field in RESEARCH_DAILY_FIELDS:
        if field in RESEARCH_DAILY_STRING_FIELDS:
            base[field] = base[field].astype("string")
        elif field in RESEARCH_DAILY_DATETIME_FIELDS:
            base[field] = pd.to_datetime(base[field], errors="coerce")
        else:
            base[field] = pd.to_numeric(base[field], errors="coerce").astype("float64")
    return base


def _write_hdf_table(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if frame.empty:
        if not path.exists():
            frame.to_hdf(path, key="data", mode="w", format="table")
        return
    min_itemsize: dict[str, int] | None = None
    if not path.exists():
        min_itemsize = {}
        for column in frame.columns:
            series = frame[column]
            if not pd.api.types.is_object_dtype(series) and not pd.api.types.is_string_dtype(series):
                continue
            lengths = series.dropna().astype(str).map(len)
            max_len = int(lengths.max()) if not lengths.empty else 0
            min_itemsize[column] = max(16, max_len + 8)
    frame.to_hdf(
        path,
        key="data",
        mode="a" if path.exists() else "w",
        format="table",
        append=path.exists(),
        data_columns=["code", "trade_date"],
        min_itemsize=min_itemsize,
    )


def _consolidated_hdf_path(package_root: Path, endpoint: str) -> Path:
    return _endpoint_dir(package_root, endpoint) / "_consolidated.h5"


def _rebuild_code_stage_hdf(
    package_root: Path,
    endpoint: str,
    *,
    data_columns: list[str] | None = None,
) -> Path:
    output_path = _consolidated_hdf_path(package_root, endpoint)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_files = sorted(_endpoint_dir(package_root, endpoint).glob("*.parquet"))
    receipt_path = output_path.with_suffix(".receipt.json")
    input_sha256 = _partition_set_sha256(source_files)
    receipt = _read_json(receipt_path)
    if (
        output_path.exists()
        and receipt.get("status") == "completed"
        and receipt.get("input_sha256") == input_sha256
        and receipt.get("output_sha256") == _file_sha256(output_path)
    ):
        return output_path
    output_path.unlink(missing_ok=True)
    receipt_path.unlink(missing_ok=True)
    working_path = output_path.with_name(f".{output_path.name}.tmp-{os.getpid()}")
    working_path.unlink(missing_ok=True)
    appended = False
    try:
        for path in source_files:
            frame = pd.read_parquet(path)
            if frame.empty:
                continue
            working = frame.copy()
            if "trade_date" in working.columns:
                working["trade_date"] = working["trade_date"].astype(str)
            working.to_hdf(
                working_path,
                key="data",
                mode="a" if appended else "w",
                format="table",
                append=appended,
                data_columns=data_columns or [column for column in ["ts_code", "trade_date"] if column in working.columns],
            )
            appended = True
        if not appended:
            pd.DataFrame().to_hdf(working_path, key="data", mode="w", format="table")
        os.replace(working_path, output_path)
    except Exception:
        working_path.unlink(missing_ok=True)
        raise
    _write_json(
        receipt_path,
        {
            "status": "completed",
            "endpoint": endpoint,
            "source_file_count": len(source_files),
            "input_sha256": input_sha256,
            "output_sha256": _file_sha256(output_path),
            "completed_at": _now(),
        },
    )
    return output_path


def _load_hdf_trade_date_range(path: Path, *, start_trade_date: str, end_trade_date: str) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    where = [f"trade_date >= '{start_trade_date}'", f"trade_date <= '{end_trade_date}'"]
    try:
        return pd.read_hdf(path, key="data", where=where)
    except KeyError:
        return pd.DataFrame()
    except Exception:
        frame = pd.read_hdf(path, key="data")
        if frame.empty or "trade_date" not in frame.columns:
            return frame
        trade_date = frame["trade_date"].astype(str)
        return frame[(trade_date >= start_trade_date) & (trade_date <= end_trade_date)].reset_index(drop=True)


def _build_index_daily_output(package_root: Path) -> str:
    df = _load_partition_frames(_endpoint_dir(package_root, "index_daily"))
    path = _silver_root(package_root) / "index_daily.h5"
    working_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    working_path.unlink(missing_ok=True)
    try:
        if df.empty:
            pd.DataFrame().to_hdf(working_path, key="data", mode="w", format="table")
        else:
            out = df.copy()
            out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce")
            out = out.rename(columns={"ts_code": "code", "vol": "volume"})
            out = out[[column for column in ["code", "trade_date", "open", "high", "low", "close", "volume", "amount"] if column in out.columns]]
            out.to_hdf(working_path, key="data", mode="w", format="table", data_columns=["code", "trade_date"])
        os.replace(working_path, path)
    except Exception:
        working_path.unlink(missing_ok=True)
        raise
    return str(path)


def _trade_date_to_timestamp(value: str | pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    stamp = pd.to_datetime(value, format="%Y%m%d", errors="coerce")
    if pd.isna(stamp):
        stamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(stamp):
        return None
    return pd.Timestamp(stamp).normalize()


def _raw_stage_duplicate_keys(root: Path, key_fields: list[str]) -> tuple[int, list[str]]:
    duplicate_count = 0
    samples: list[str] = []
    for path in sorted(root.glob("*.parquet")):
        frame = pd.read_parquet(path)
        if frame.empty:
            continue
        present = [field for field in key_fields if field in frame.columns]
        if len(present) != len(key_fields):
            continue
        duplicates = int(frame.duplicated(subset=key_fields).sum())
        duplicate_count += duplicates
        if duplicates and len(samples) < 5:
            samples.append(f"{path.name}:{duplicates}")
    return duplicate_count, samples


def _build_raw_quality_report(package_root: Path, manifest: dict[str, Any], trade_dates: list[str]) -> dict[str, Any]:
    stock_basic_path = _endpoint_dir(package_root, "stock_basic") / "all.parquet"
    if not stock_basic_path.exists():
        payload = {"passed": False, "issues": ["stock_basic_missing"], "warnings": []}
        _write_json(_silver_root(package_root) / "raw_quality_report.json", payload)
        return payload

    stock_basic = pd.read_parquet(stock_basic_path)
    stock_basic["ts_code"] = stock_basic["ts_code"].astype(str)
    stock_basic["list_date_ts"] = pd.to_datetime(stock_basic["list_date"], format="%Y%m%d", errors="coerce").dt.normalize()
    if "list_status" in stock_basic.columns:
        stock_basic["list_status"] = stock_basic["list_status"].astype("string").str.strip().str.upper()
    else:
        stock_basic["list_status"] = pd.NA
    list_date_map = stock_basic.set_index("ts_code")["list_date_ts"].to_dict()
    list_status_map = stock_basic.set_index("ts_code")["list_status"].to_dict()
    known_codes = set(stock_basic["ts_code"].tolist())
    effective_target = _trade_date_to_timestamp(manifest.get("effective_target_date") or manifest.get("selected_target_date"))
    eligible_codes = sorted(
        code
        for code, stamp in list_date_map.items()
        if (
            stamp is not None
            and effective_target is not None
            and stamp <= effective_target
            and list_status_map.get(code) in STOCK_BASIC_TRADABLE_STATUSES
        )
    )

    issues: list[str] = []
    warnings: list[str] = []

    stock_basic_duplicates = int(stock_basic.duplicated(subset=["ts_code"]).sum())
    if stock_basic_duplicates:
        issues.append(f"stock_basic_duplicate_codes:{stock_basic_duplicates}")
    invalid_list_dates = int(stock_basic["list_date_ts"].isna().sum())
    if invalid_list_dates:
        issues.append(f"stock_basic_invalid_list_date:{invalid_list_dates}")

    daily_root = _endpoint_dir(package_root, "daily")
    daily_code_counts: dict[str, int] = {}
    daily_first_seen: dict[str, pd.Timestamp] = {}
    daily_duplicate_keys = 0
    daily_unexpected_codes = 0
    daily_pre_listing_rows = 0
    daily_missing_dates = 0
    daily_missing_date_samples: list[str] = []

    for trade_date in trade_dates:
        path = _date_file(package_root, "daily", trade_date)
        if not path.exists():
            daily_missing_dates += 1
            if len(daily_missing_date_samples) < 5:
                daily_missing_date_samples.append(trade_date)
            continue
        frame = pd.read_parquet(path)
        if frame.empty:
            continue
        if {"ts_code", "trade_date"}.issubset(frame.columns):
            duplicates = int(frame.duplicated(subset=["ts_code", "trade_date"]).sum())
            daily_duplicate_keys += duplicates
        trade_stamp = _trade_date_to_timestamp(trade_date)
        for code in frame["ts_code"].astype(str).tolist():
            if code not in known_codes:
                daily_unexpected_codes += 1
                continue
            list_stamp = list_date_map.get(code)
            if trade_stamp is not None:
                if list_stamp is not None and trade_stamp < list_stamp:
                    daily_pre_listing_rows += 1
                    continue
                first_seen = daily_first_seen.get(code)
                if first_seen is None or trade_stamp < first_seen:
                    daily_first_seen[code] = trade_stamp
            daily_code_counts[code] = daily_code_counts.get(code, 0) + 1

    if daily_missing_dates:
        issues.append(f"daily_missing_dates:{daily_missing_dates}")
    if daily_duplicate_keys:
        issues.append(f"daily_duplicate_keys:{daily_duplicate_keys}")
    if daily_unexpected_codes:
        issues.append(f"daily_unexpected_codes:{daily_unexpected_codes}")
    if daily_pre_listing_rows:
        warnings.append(f"daily_pre_listing_rows:{daily_pre_listing_rows}")

    codes_without_daily_rows = [code for code in eligible_codes if daily_code_counts.get(code, 0) == 0]
    suspended_codes_by_date: dict[str, set[str]] = {}
    for trade_date in trade_dates:
        path = _date_file(package_root, "suspend_d", trade_date)
        if not path.exists():
            continue
        frame = pd.read_parquet(path)
        if frame.empty or "ts_code" not in frame.columns:
            continue
        if "trade_date" in frame.columns:
            frame = frame[frame["trade_date"].astype(str) == str(trade_date)]
        if frame.empty:
            continue
        suspended_codes_by_date[trade_date] = set(frame["ts_code"].astype(str).tolist())
    suspended_codes_without_daily_rows: list[str] = []
    for code in codes_without_daily_rows:
        list_stamp = list_date_map.get(code)
        expected_dates = [
            trade_date
            for trade_date in trade_dates
            if (trade_stamp := _trade_date_to_timestamp(trade_date)) is not None
            and (list_stamp is None or trade_stamp >= list_stamp)
        ]
        if expected_dates and all(code in suspended_codes_by_date.get(trade_date, set()) for trade_date in expected_dates):
            suspended_codes_without_daily_rows.append(code)
    suspended_code_set = set(suspended_codes_without_daily_rows)
    unsuspended_codes_without_daily_rows = [code for code in codes_without_daily_rows if code not in suspended_code_set]
    if unsuspended_codes_without_daily_rows:
        issues.append(f"daily_codes_without_rows:{len(unsuspended_codes_without_daily_rows)}")
    if suspended_codes_without_daily_rows:
        warnings.append(f"daily_codes_without_rows_suspended:{len(suspended_codes_without_daily_rows)}")

    first_trade_after_listing: list[str] = []
    for code, first_seen in daily_first_seen.items():
        list_stamp = list_date_map.get(code)
        if list_stamp is None or first_seen is None:
            continue
        if first_seen < list_stamp:
            first_trade_after_listing.append(f"{code}:{first_seen.date()}<{list_stamp.date()}")
    if first_trade_after_listing:
        warnings.append(f"daily_first_trade_before_list_date:{len(first_trade_after_listing)}")

    coverage_summary: dict[str, dict[str, Any]] = {}
    for endpoint in ["stk_limit", "daily_basic", "adj_factor", "moneyflow", "margin_detail"]:
        missing_dates = 0
        missing_codes = 0
        missing_bj_codes = 0
        missing_non_bj_codes = 0
        missing_date_samples: list[str] = []
        missing_code_samples: list[str] = []
        for trade_date in trade_dates:
            daily_path = _date_file(package_root, "daily", trade_date)
            if not daily_path.exists():
                continue
            trade_stamp = _trade_date_to_timestamp(trade_date)
            daily_codes = {
                code
                for code in pd.read_parquet(daily_path, columns=["ts_code"])["ts_code"].astype(str).tolist()
                if (
                    code in known_codes
                    and trade_stamp is not None
                    and (list_date_map.get(code) is None or trade_stamp >= list_date_map.get(code))
                )
            }
            if not daily_codes:
                continue
            path = _date_file(package_root, endpoint, trade_date)
            if not path.exists():
                missing_dates += 1
                if len(missing_date_samples) < 5:
                    missing_date_samples.append(trade_date)
                continue
            other_codes = set(pd.read_parquet(path, columns=["ts_code"])["ts_code"].astype(str).tolist())
            code_gap = sorted(daily_codes - other_codes)
            if code_gap:
                missing_codes += len(code_gap)
                missing_bj_codes += sum(1 for code in code_gap if str(code).startswith("920"))
                missing_non_bj_codes += sum(1 for code in code_gap if not str(code).startswith("920"))
                if len(missing_code_samples) < 5:
                    missing_code_samples.append(f"{trade_date}:{len(code_gap)}:{code_gap[:3]}")
        coverage_summary[endpoint] = {
            "missing_dates": missing_dates,
            "missing_codes": missing_codes,
            "missing_bj_codes": missing_bj_codes,
            "missing_non_bj_codes": missing_non_bj_codes,
            "missing_date_samples": missing_date_samples,
            "missing_code_samples": missing_code_samples,
        }
        if missing_dates or missing_codes:
            message = f"{endpoint}_coverage_gap:missing_dates={missing_dates}:missing_codes={missing_codes}"
            if endpoint in {"moneyflow", "margin_detail"}:
                warnings.append(message)
            elif endpoint == "daily_basic" and missing_non_bj_codes == 0 and missing_bj_codes > 0:
                warnings.append(f"{message}:bj_only={missing_bj_codes}")
            else:
                issues.append(message)

    code_stage_comparisons: dict[str, dict[str, Any]] = {}
    hfq_derivation = manifest.get("hfq_derivation") if isinstance(manifest.get("hfq_derivation"), dict) else {}
    hfq_derived_locally = hfq_derivation.get("mode") == "local"
    for endpoint in ["pro_bar_hfq", "cyq_perf"]:
        counts: dict[str, int] = {}
        unexpected_codes = 0
        pre_listing_rows = 0
        derivation = None
        if endpoint == "pro_bar_hfq" and hfq_derived_locally:
            derivation = str(hfq_derivation.get("formula") or "daily_ohlc_times_adj_factor")
            duplicate_keys = 0
            duplicate_samples: list[str] = []
            for trade_date in trade_dates:
                daily_path = _date_file(package_root, "daily", trade_date)
                adj_path = _date_file(package_root, "adj_factor", trade_date)
                if not daily_path.exists() or not adj_path.exists():
                    continue
                daily_frame = pd.read_parquet(daily_path)
                adj_frame = pd.read_parquet(adj_path)
                required_daily = {"ts_code", "trade_date", "open", "high", "low", "close"}
                required_adj = {"ts_code", "trade_date", "adj_factor"}
                if daily_frame.empty or not required_daily.issubset(daily_frame.columns) or not required_adj.issubset(adj_frame.columns):
                    continue
                daily_work = daily_frame[list(required_daily)].copy()
                adj_work = adj_frame[list(required_adj)].copy()
                daily_work["ts_code"] = daily_work["ts_code"].astype(str)
                daily_work["trade_date"] = daily_work["trade_date"].astype(str)
                adj_work["ts_code"] = adj_work["ts_code"].astype(str)
                adj_work["trade_date"] = adj_work["trade_date"].astype(str)
                duplicate_keys += int(daily_work.duplicated(subset=["ts_code", "trade_date"]).sum())
                duplicate_keys += int(adj_work.duplicated(subset=["ts_code", "trade_date"]).sum())
                merged = daily_work.merge(
                    adj_work.drop_duplicates(subset=["ts_code", "trade_date"], keep="last"),
                    on=["ts_code", "trade_date"],
                    how="left",
                )
                numeric = merged[["open", "high", "low", "close", "adj_factor"]].apply(pd.to_numeric, errors="coerce")
                valid = numeric.notna().all(axis=1)
                trade_stamp = _trade_date_to_timestamp(trade_date)
                for code in merged.loc[valid, "ts_code"].tolist():
                    if code not in known_codes:
                        unexpected_codes += 1
                        continue
                    list_stamp = list_date_map.get(code)
                    if list_stamp is not None and trade_stamp is not None and trade_stamp < list_stamp:
                        pre_listing_rows += 1
                        continue
                    counts[code] = counts.get(code, 0) + 1
        else:
            duplicate_keys, duplicate_samples = _raw_stage_duplicate_keys(
                _endpoint_dir(package_root, endpoint), ["ts_code", "trade_date"]
            )
            for path in sorted(_endpoint_dir(package_root, endpoint).glob("*.parquet")):
                frame = pd.read_parquet(path)
                if frame.empty:
                    code = path.stem.replace("_", ".")
                    counts[code] = 0
                    continue
                if "ts_code" not in frame.columns:
                    continue
                frame["ts_code"] = frame["ts_code"].astype(str)
                code = str(frame["ts_code"].iloc[0])
                effective_count = int(len(frame))
                if code not in known_codes:
                    unexpected_codes += int(len(frame))
                    continue
                list_stamp = list_date_map.get(code)
                if list_stamp is not None and "trade_date" in frame.columns:
                    trade_stamps = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="coerce").dt.normalize()
                    pre_listing_mask = trade_stamps < list_stamp
                    pre_listing_rows += int(pre_listing_mask.sum())
                    effective_count = int((~pre_listing_mask).sum())
                counts[code] = effective_count
        missing_codes = [code for code in eligible_codes if daily_code_counts.get(code, 0) > 0 and counts.get(code, 0) == 0]
        count_mismatches = [code for code in eligible_codes if daily_code_counts.get(code, 0) > 0 and counts.get(code, 0) not in (0, daily_code_counts.get(code, 0))]
        code_stage_comparisons[endpoint] = {
            "derivation": derivation,
            "duplicate_keys": duplicate_keys,
            "duplicate_samples": duplicate_samples,
            "unexpected_code_rows": unexpected_codes,
            "pre_listing_rows": pre_listing_rows,
            "codes_without_rows": len(missing_codes),
            "codes_without_rows_sample": missing_codes[:10],
            "row_count_mismatch_codes": len(count_mismatches),
            "row_count_mismatch_sample": count_mismatches[:10],
        }
        if duplicate_keys:
            issues.append(f"{endpoint}_duplicate_keys:{duplicate_keys}")
        if unexpected_codes:
            issues.append(f"{endpoint}_unexpected_code_rows:{unexpected_codes}")
        if pre_listing_rows:
            warnings.append(f"{endpoint}_pre_listing_rows:{pre_listing_rows}")
        if missing_codes:
            issues.append(f"{endpoint}_codes_without_rows:{len(missing_codes)}")
        if count_mismatches:
            if endpoint == "cyq_perf":
                warnings.append(f"{endpoint}_row_count_mismatch_codes:{len(count_mismatches)}")
            else:
                issues.append(f"{endpoint}_row_count_mismatch_codes:{len(count_mismatches)}")

    benchmark_summary: dict[str, str | None] = {}
    benchmark_target = manifest.get("effective_target_date") or manifest.get("selected_target_date")
    for code in BENCHMARK_INDEX_CODES:
        path = _code_file(package_root, "index_daily", code)
        latest_code_trade_date = None
        if path.exists():
            latest_series = pd.read_parquet(path, columns=["trade_date"])["trade_date"].astype(str)
            latest_code_trade_date = latest_series.max() if not latest_series.empty else None
        benchmark_summary[code] = latest_code_trade_date
        if code in REQUIRED_BENCHMARK_INDEX_CODES:
            if latest_code_trade_date != benchmark_target:
                issues.append(f"required_benchmark_stale:{code}:{latest_code_trade_date}")
        elif latest_code_trade_date != benchmark_target:
            warnings.append(f"optional_benchmark_stale:{code}:{latest_code_trade_date}")

    payload = {
        "passed": not issues,
        "issues": issues,
        "warnings": warnings,
        "stock_basic_code_count": int(len(stock_basic)),
        "eligible_code_count": int(len(eligible_codes)),
        "daily": {
            "missing_dates": daily_missing_dates,
            "missing_date_samples": daily_missing_date_samples,
            "duplicate_keys": daily_duplicate_keys,
            "unexpected_code_rows": daily_unexpected_codes,
            "pre_listing_rows": daily_pre_listing_rows,
            "codes_without_rows": len(codes_without_daily_rows),
            "codes_without_rows_sample": codes_without_daily_rows[:10],
            "suspended_codes_without_rows": len(suspended_codes_without_daily_rows),
            "suspended_codes_without_rows_sample": suspended_codes_without_daily_rows[:10],
            "unsuspended_codes_without_rows": len(unsuspended_codes_without_daily_rows),
            "unsuspended_codes_without_rows_sample": unsuspended_codes_without_daily_rows[:10],
        },
        "coverage_summary": coverage_summary,
        "code_stage_comparisons": code_stage_comparisons,
        "benchmark_summary": benchmark_summary,
    }
    _write_json(_silver_root(package_root) / "raw_quality_report.json", payload)
    return payload


def _write_metadata(package_root: Path, manifest: dict[str, Any]) -> str:
    payload = {
        "source": "tushare",
        "schema_version": "tushare_v1",
        "field_count": len(RESEARCH_DAILY_FIELDS),
        "research_daily_fields": RESEARCH_DAILY_FIELDS,
        "research_daily_units": RESEARCH_DAILY_UNIT_FIELDS,
        "effective_target_date": manifest.get("effective_target_date"),
        "selected_target_date": manifest.get("selected_target_date"),
        "start_date": manifest.get("start_date"),
        "cutoff_date": manifest.get("cutoff_date"),
        "trade_date_count": manifest.get("trade_date_count"),
        "code_count": manifest.get("code_count"),
        "proxy_mode": manifest.get("proxy_mode"),
        "remote_only": True,
    }
    path = _metadata_path(package_root)
    _write_json(path, payload)
    return str(path)


def _link_or_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except Exception:
        import shutil

        shutil.copy2(src, dst)


def _suspended_codes_for_expected_dates(
    package_root: Path,
    *,
    codes: list[str],
    list_date_map: dict[str, pd.Timestamp],
    trade_dates: list[str],
) -> list[str]:
    suspended_codes_by_date: dict[str, set[str]] = {}
    for trade_date in trade_dates:
        path = _date_file(package_root, "suspend_d", trade_date)
        if not path.exists():
            continue
        frame = pd.read_parquet(path)
        if frame.empty or "ts_code" not in frame.columns:
            continue
        if "trade_date" in frame.columns:
            frame = frame[frame["trade_date"].astype(str) == str(trade_date)]
        if frame.empty:
            continue
        suspended_codes_by_date[trade_date] = set(frame["ts_code"].astype(str).tolist())

    suspended_codes: list[str] = []
    for code in codes:
        list_stamp = list_date_map.get(code)
        expected_dates = [
            trade_date
            for trade_date in trade_dates
            if (trade_stamp := _trade_date_to_timestamp(trade_date)) is not None
            and (list_stamp is None or trade_stamp >= list_stamp)
        ]
        if expected_dates and all(code in suspended_codes_by_date.get(trade_date, set()) for trade_date in expected_dates):
            suspended_codes.append(code)
    return suspended_codes


def _build_quality_report(package_root: Path, research_daily_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if not research_daily_path.exists():
        return {"passed": False, "issues": ["research_daily_missing"], "warnings": []}
    df = pd.read_hdf(research_daily_path, key="data")
    duplicate_keys = int(df.duplicated(subset=["code", "trade_date"]).sum()) if not df.empty else 0
    latest_trade_date = str(pd.to_datetime(df["trade_date"]).max().date()) if not df.empty else None
    issues: list[str] = []
    warnings: list[str] = []
    if list(df.columns) != RESEARCH_DAILY_FIELDS:
        issues.append("research_daily_schema_mismatch")
    if duplicate_keys:
        issues.append(f"duplicate_keys:{duplicate_keys}")
    expected_latest_trade_date = manifest.get("selected_target_date") or manifest.get("effective_target_date")
    if expected_latest_trade_date:
        expected_latest_trade_date = f"{expected_latest_trade_date[:4]}-{expected_latest_trade_date[4:6]}-{expected_latest_trade_date[6:8]}"
    if latest_trade_date != expected_latest_trade_date:
        issues.append(f"latest_trade_date_mismatch:{latest_trade_date}:{expected_latest_trade_date}")
    list_date_sanity = {
        "pre_listing_rows": 0,
        "codes_without_rows": 0,
        "codes_without_rows_sample": [],
        "suspended_codes_without_rows": 0,
        "suspended_codes_without_rows_sample": [],
        "unsuspended_codes_without_rows": 0,
        "unsuspended_codes_without_rows_sample": [],
    }
    if not df.empty and "list_date" in df.columns:
        list_dates = pd.to_datetime(df["list_date"], format="%Y%m%d", errors="coerce").dt.normalize()
        trade_dates = pd.to_datetime(df["trade_date"], errors="coerce").dt.normalize()
        list_date_sanity["pre_listing_rows"] = int((trade_dates < list_dates).sum())
        if list_date_sanity["pre_listing_rows"]:
            issues.append(f"research_daily_pre_listing_rows:{list_date_sanity['pre_listing_rows']}")
    stock_basic_path = _endpoint_dir(package_root, "stock_basic") / "all.parquet"
    if stock_basic_path.exists():
        stock_basic = pd.read_parquet(stock_basic_path)
        if "list_status" in stock_basic.columns:
            stock_basic["list_status"] = stock_basic["list_status"].astype("string").str.strip().str.upper()
        else:
            stock_basic["list_status"] = pd.NA
        stock_basic["list_date_ts"] = pd.to_datetime(stock_basic["list_date"], format="%Y%m%d", errors="coerce").dt.normalize()
        target_ts = _trade_date_to_timestamp(manifest.get("effective_target_date") or manifest.get("selected_target_date"))
        list_status_map = stock_basic.set_index("ts_code")["list_status"].to_dict()
        eligible_codes = sorted(
            str(code)
            for code, stamp in stock_basic.set_index("ts_code")["list_date_ts"].to_dict().items()
            if (
                stamp is not None
                and target_ts is not None
                and stamp <= target_ts
                and list_status_map.get(code) in STOCK_BASIC_TRADABLE_STATUSES
            )
        )
        df_codes = set(df["code"].astype(str).tolist()) if not df.empty else set()
        missing_codes = [code for code in eligible_codes if code not in df_codes]
        trade_dates = sorted(path.stem for path in _endpoint_dir(package_root, "daily").glob("*.parquet"))
        list_date_map = stock_basic.set_index("ts_code")["list_date_ts"].to_dict()
        suspended_missing_codes = _suspended_codes_for_expected_dates(
            package_root,
            codes=missing_codes,
            list_date_map=list_date_map,
            trade_dates=trade_dates,
        )
        suspended_missing_set = set(suspended_missing_codes)
        unsuspended_missing_codes = [code for code in missing_codes if code not in suspended_missing_set]
        list_date_sanity["codes_without_rows"] = len(missing_codes)
        list_date_sanity["codes_without_rows_sample"] = missing_codes[:10]
        list_date_sanity["suspended_codes_without_rows"] = len(suspended_missing_codes)
        list_date_sanity["suspended_codes_without_rows_sample"] = suspended_missing_codes[:10]
        list_date_sanity["unsuspended_codes_without_rows"] = len(unsuspended_missing_codes)
        list_date_sanity["unsuspended_codes_without_rows_sample"] = unsuspended_missing_codes[:10]
        if unsuspended_missing_codes:
            issues.append(f"research_daily_codes_without_rows:{len(unsuspended_missing_codes)}")
        if suspended_missing_codes:
            warnings.append(f"research_daily_codes_without_rows_suspended:{len(suspended_missing_codes)}")
    missing_summary: dict[str, float] = {}
    required_fields = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "hfq_open",
        "hfq_high",
        "hfq_low",
        "hfq_close",
        "adj_factor",
        "pb",
    ]
    for field in required_fields:
        missing_summary[field] = float(df[field].isna().mean()) if field in df.columns and len(df) else 1.0
        if field in df.columns and len(df) and missing_summary[field] > 0.1:
            issues.append(f"missing_ratio_high:{field}:{missing_summary[field]:.4f}")
    pe_ttm_missing_pb_present_ratio = 0.0
    if "pe_ttm" in df.columns and len(df):
        missing_summary["pe_ttm"] = float(df["pe_ttm"].isna().mean())
        pe_missing = df["pe_ttm"].isna()
        if pe_missing.any() and "pb" in df.columns:
            pe_ttm_missing_pb_present_ratio = float(df.loc[pe_missing, "pb"].notna().mean())
        if missing_summary["pe_ttm"] > 0.1:
            if pe_ttm_missing_pb_present_ratio >= 0.8:
                warnings.append(
                    f"pe_ttm_structural_missing:{missing_summary['pe_ttm']:.4f}:pb_present_ratio={pe_ttm_missing_pb_present_ratio:.4f}"
                )
            else:
                issues.append(f"missing_ratio_high:pe_ttm:{missing_summary['pe_ttm']:.4f}")
    else:
        missing_summary["pe_ttm"] = 1.0
    price_sanity = {
        "high_lt_low": 0,
        "close_outside_range": 0,
        "open_outside_range": 0,
    }
    if not df.empty:
        price_sanity["high_lt_low"] = int((df["high"] < df["low"]).sum())
        price_sanity["close_outside_range"] = int(((df["close"] < df["low"]) | (df["close"] > df["high"])).sum())
        price_sanity["open_outside_range"] = int(((df["open"] < df["low"]) | (df["open"] > df["high"])).sum())
    for key, value in price_sanity.items():
        if value:
            issues.append(f"{key}:{value}")

    coverage_summary = {
        "stk_limit_missing_dates": 0,
        "stk_limit_missing_codes": 0,
        "daily_basic_missing_dates": 0,
        "daily_basic_missing_codes": 0,
        "adj_factor_missing_dates": 0,
        "adj_factor_missing_codes": 0,
    }
    coverage_samples: dict[str, list[str]] = {
        "stk_limit_missing_dates": [],
        "daily_basic_missing_dates": [],
        "adj_factor_missing_dates": [],
        "stk_limit_code_gaps": [],
        "daily_basic_code_gaps": [],
        "adj_factor_code_gaps": [],
    }
    for daily_path in sorted(_endpoint_dir(package_root, "daily").glob("*.parquet")):
        trade_date = daily_path.stem
        daily_codes = set(pd.read_parquet(daily_path, columns=["ts_code"])["ts_code"].astype(str).tolist())
        if not daily_codes:
            continue
        for endpoint, label in [("stk_limit", "stk_limit"), ("daily_basic", "daily_basic"), ("adj_factor", "adj_factor")]:
            path = _date_file(package_root, endpoint, trade_date)
            if not path.exists():
                coverage_summary[f"{label}_missing_dates"] += 1
                if len(coverage_samples[f"{label}_missing_dates"]) < 5:
                    coverage_samples[f"{label}_missing_dates"].append(trade_date)
                continue
            other_codes = set(pd.read_parquet(path, columns=["ts_code"])["ts_code"].astype(str).tolist())
            missing_count = len(daily_codes - other_codes)
            if missing_count:
                coverage_summary[f"{label}_missing_codes"] += missing_count
                sample_key = f"{label}_code_gaps"
                if len(coverage_samples[sample_key]) < 5:
                    coverage_samples[sample_key].append(f"{trade_date}:{missing_count}")

    if coverage_summary["stk_limit_missing_dates"] or coverage_summary["stk_limit_missing_codes"]:
        issues.append(
            f"stk_limit_coverage_gap:missing_dates={coverage_summary['stk_limit_missing_dates']}:missing_codes={coverage_summary['stk_limit_missing_codes']}"
        )
    if coverage_summary["daily_basic_missing_dates"] or coverage_summary["daily_basic_missing_codes"]:
        issues.append(
            f"daily_basic_coverage_gap:missing_dates={coverage_summary['daily_basic_missing_dates']}:missing_codes={coverage_summary['daily_basic_missing_codes']}"
        )
    if coverage_summary["adj_factor_missing_dates"] or coverage_summary["adj_factor_missing_codes"]:
        issues.append(
            f"adj_factor_coverage_gap:missing_dates={coverage_summary['adj_factor_missing_dates']}:missing_codes={coverage_summary['adj_factor_missing_codes']}"
        )

    benchmark_summary: dict[str, str | None] = {}
    for code in BENCHMARK_INDEX_CODES:
        path = _code_file(package_root, "index_daily", code)
        latest_code_trade_date = None
        if path.exists():
            latest_series = pd.read_parquet(path, columns=["trade_date"])["trade_date"].astype(str)
            latest_code_trade_date = latest_series.max() if not latest_series.empty else None
        benchmark_summary[code] = latest_code_trade_date
        if code in REQUIRED_BENCHMARK_INDEX_CODES:
            if latest_code_trade_date != (manifest.get("effective_target_date") or manifest.get("selected_target_date")):
                issues.append(f"required_benchmark_stale:{code}:{latest_code_trade_date}")
        elif latest_code_trade_date != (manifest.get("effective_target_date") or manifest.get("selected_target_date")):
            warnings.append(f"optional_benchmark_stale:{code}:{latest_code_trade_date}")

    payload = {
        "passed": not issues,
        "issues": issues,
        "warnings": warnings,
        "row_count": int(len(df)),
        "code_count": int(df["code"].nunique()) if not df.empty else 0,
        "latest_trade_date": latest_trade_date,
        "duplicate_keys": duplicate_keys,
        "missing_summary": missing_summary,
        "list_date_sanity": list_date_sanity,
        "price_sanity": price_sanity,
        "coverage_summary": coverage_summary,
        "coverage_samples": coverage_samples,
        "benchmark_summary": benchmark_summary,
        "pe_ttm_missing_pb_present_ratio": pe_ttm_missing_pb_present_ratio,
        "field_count": len(RESEARCH_DAILY_FIELDS),
    }
    _write_json(_silver_root(package_root) / "quality_report.json", payload)
    return payload


def _assemble_research_daily(
    *,
    package_root: Path,
    progress: dict[str, Any],
    trade_dates: list[str],
    trade_date_chunk_size: int,
) -> dict[str, Any]:
    stage = progress["stages"]["assemble_research_daily"]
    stock_basic_df = _load_partition_frames(_endpoint_dir(package_root, "stock_basic"))
    stock_st_all = _load_partition_frames(_endpoint_dir(package_root, "stock_st"))
    namechange_df = _load_partition_frames(_endpoint_dir(package_root, "namechange"))
    income_df = _load_partition_frames(_endpoint_dir(package_root, "income"))
    balancesheet_df = _load_partition_frames(_endpoint_dir(package_root, "balancesheet"))
    fina_indicator_df = _load_partition_frames(_endpoint_dir(package_root, "fina_indicator"))
    holder_df = _load_partition_frames(_endpoint_dir(package_root, "holder_num"))
    hfq_hdf_path = _rebuild_code_stage_hdf(package_root, "pro_bar_hfq")
    cyq_perf_hdf_path = _rebuild_code_stage_hdf(package_root, "cyq_perf")
    research_daily_path = _silver_root(package_root) / "research_daily.h5"
    canonical_daily_path = _silver_root(package_root) / "canonical_daily.h5"
    parts_root = _silver_root(package_root) / "assembly_parts"
    if stage["cursor"] == 0:
        if research_daily_path.exists():
            research_daily_path.unlink()
        if canonical_daily_path.exists():
            canonical_daily_path.unlink()
        shutil.rmtree(parts_root, ignore_errors=True)
    parts_root.mkdir(parents=True, exist_ok=True)

    chunk_size = max(1, int(trade_date_chunk_size))
    date_chunks = [trade_dates[idx : idx + chunk_size] for idx in range(0, len(trade_dates), chunk_size)]
    stage["total"] = len(date_chunks)
    for idx in range(min(int(stage["cursor"]), len(date_chunks))):
        part_path = parts_root / f"{idx:06d}.parquet"
        receipt_path = parts_root / f"{idx:06d}.receipt.json"
        receipt = _read_json(receipt_path)
        if (
            not part_path.exists()
            or receipt.get("status") != "completed"
            or receipt.get("sha256") != _file_sha256(part_path)
        ):
            stage["cursor"] = idx
            break
    for idx in range(stage["cursor"], len(date_chunks)):
        chunk_dates = date_chunks[idx]
        stage["status"] = "running"
        stage["current_key"] = f"{chunk_dates[0]}:{chunk_dates[-1]}"
        _save_progress(package_root, progress)
        daily_df = _load_partition_frames_for_dates(package_root, "daily", chunk_dates)
        hfq_df = _load_hdf_trade_date_range(
            hfq_hdf_path,
            start_trade_date=chunk_dates[0],
            end_trade_date=chunk_dates[-1],
        )
        adj_df = _load_partition_frames_for_dates(package_root, "adj_factor", chunk_dates)
        daily_basic_df = _load_partition_frames_for_dates(package_root, "daily_basic", chunk_dates)
        stk_limit_df = _load_partition_frames_for_dates(package_root, "stk_limit", chunk_dates)
        stock_st_df = stock_st_all[stock_st_all["trade_date"].astype(str).isin(set(chunk_dates))].copy() if not stock_st_all.empty and "trade_date" in stock_st_all.columns else pd.DataFrame()
        moneyflow_df = _load_partition_frames_for_dates(package_root, "moneyflow", chunk_dates)
        margin_df = _load_partition_frames_for_dates(package_root, "margin_detail", chunk_dates)
        cyq_perf_df = _load_hdf_trade_date_range(
            cyq_perf_hdf_path,
            start_trade_date=chunk_dates[0],
            end_trade_date=chunk_dates[-1],
        )
        frame = _assemble_research_daily_chunk(
            daily_df=daily_df,
            stock_basic_df=stock_basic_df,
            stock_st_df=stock_st_df,
            namechange_df=namechange_df,
            hfq_df=hfq_df,
            adj_df=adj_df,
            daily_basic_df=daily_basic_df,
            stk_limit_df=stk_limit_df,
            moneyflow_df=moneyflow_df,
            margin_df=margin_df,
            cyq_perf_df=cyq_perf_df,
            income_df=income_df,
            balancesheet_df=balancesheet_df,
            fina_indicator_df=fina_indicator_df,
            holder_df=holder_df,
        )
        part_path = parts_root / f"{idx:06d}.parquet"
        _write_frame(part_path, frame)
        _write_json(
            parts_root / f"{idx:06d}.receipt.json",
            {
                "status": "completed",
                "stage": "assemble_research_daily",
                "chunk_index": idx,
                "trade_dates": chunk_dates,
                "row_count": int(len(frame)),
                "sha256": _file_sha256(part_path),
                "completed_at": _now(),
            },
        )
        stage["cursor"] = idx + 1
        stage["status"] = "completed" if stage["cursor"] >= stage["total"] else "running"
        _save_progress(package_root, progress)

    working_hdf = research_daily_path.with_name(f".{research_daily_path.name}.tmp-{os.getpid()}")
    working_hdf.unlink(missing_ok=True)
    try:
        for idx in range(len(date_chunks)):
            part_path = parts_root / f"{idx:06d}.parquet"
            if not part_path.exists():
                raise RuntimeError(f"assembly_part_missing:{idx}")
            _write_hdf_table(working_hdf, pd.read_parquet(part_path))
        os.replace(working_hdf, research_daily_path)
    except Exception:
        working_hdf.unlink(missing_ok=True)
        raise
    _link_or_copy(research_daily_path, canonical_daily_path)
    return {
        "research_daily_path": str(research_daily_path),
        "canonical_daily_path": str(canonical_daily_path),
        "field_count": len(RESEARCH_DAILY_FIELDS),
    }


def _load_partition_frames_for_dates(package_root: Path, endpoint: str, trade_dates: list[str]) -> pd.DataFrame:
    parts = [_date_file(package_root, endpoint, trade_date) for trade_date in trade_dates]
    frames = [pd.read_parquet(path) for path in parts if path.exists()]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _load_partition_frames_for_codes(package_root: Path, endpoint: str, codes: list[str]) -> pd.DataFrame:
    parts = [_code_file(package_root, endpoint, code) for code in codes]
    frames = [pd.read_parquet(path) for path in parts if path.exists()]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


@data_job_guard("tushare_full_rebuild")
def tushare_full_rebuild(config: TushareRebuildConfig) -> dict[str, Any]:
    plan = tushare_preflight(
        start_date=config.start_date,
        cutoff_date=config.cutoff_date,
        pad_trading_days=config.pad_trading_days,
        max_trade_days=config.max_trade_days,
        max_codes=config.max_codes,
        proxy_mode=config.proxy_mode,
    )
    plan["trade_date_chunk_size"] = config.trade_date_chunk_size
    if config.dry_run:
        return {
            "status": "dry_run",
            "plan": plan,
            "research_daily_fields": RESEARCH_DAILY_FIELDS,
            "field_count": len(RESEARCH_DAILY_FIELDS),
        }

    package_root, progress = _load_or_init_package(config, plan)
    _status_payload(status="running", package_id=plan["package_id"], stage="starting")
    with _proxy_mode(config.proxy_mode):
        pro = get_tushare_client(network_mode=config.proxy_mode)

        _run_single_frame_stage(
            package_root=package_root,
            progress=progress,
            stage_name="stock_basic",
            loader=lambda: _retry_call(
                lambda: _fetch_stock_basic_statuses(pro),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="stock_basic",
                key="all",
            ),
            path=_endpoint_dir(package_root, "stock_basic") / "all.parquet",
            columns=["ts_code", "name", "list_status", "list_date", "delist_date"],
        )
        stock_basic_path = _endpoint_dir(package_root, "stock_basic") / "all.parquet"
        stock_basic_df = pd.read_parquet(stock_basic_path)
        _write_frame(stock_basic_path, _filter_selected_codes(stock_basic_df, plan.get("status_codes") or plan["codes"]), ["ts_code", "name", "list_status", "list_date", "delist_date"])

        _run_trade_date_stage(
            package_root=package_root,
            progress=progress,
            stage_name="daily",
            trade_dates=plan["trade_dates"],
            sleep_seconds=config.trade_date_sleep_seconds,
            fetcher=lambda trade_date: _retry_call(
                lambda: _filter_selected_codes(pro.daily(
                    trade_date=trade_date,
                    fields=DAILY_FIELDS,
                ), plan["codes"]),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="daily",
                key=trade_date,
            ),
            columns=["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"],
        )

        _run_trade_date_stage(
            package_root=package_root,
            progress=progress,
            stage_name="daily_basic",
            trade_dates=plan["trade_dates"],
            sleep_seconds=config.trade_date_sleep_seconds,
            fetcher=lambda trade_date: _retry_call(
                lambda: _filter_selected_codes(pro.daily_basic(
                    trade_date=trade_date,
                    fields=DAILY_BASIC_FIELDS,
                ), plan["codes"]),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="daily_basic",
                key=trade_date,
            ),
            columns=["ts_code", "trade_date", "turnover_rate", "turnover_rate_f", "pe_ttm", "pb", "ps_ttm", "dv_ttm", "total_share", "float_share", "free_share", "total_mv", "circ_mv"],
        )

        _run_trade_date_stage(
            package_root=package_root,
            progress=progress,
            stage_name="stk_limit",
            trade_dates=plan["trade_dates"],
            sleep_seconds=config.trade_date_sleep_seconds,
            fetcher=lambda trade_date: _retry_call(
                lambda: _filter_selected_codes(pro.stk_limit(
                    trade_date=trade_date,
                    fields=STK_LIMIT_FIELDS,
                ), plan["codes"]),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="stk_limit",
                key=trade_date,
            ),
            columns=["ts_code", "trade_date", "pre_close", "up_limit", "down_limit"],
        )

        _run_trade_date_stage(
            package_root=package_root,
            progress=progress,
            stage_name="stock_st",
            trade_dates=plan["trade_dates"],
            sleep_seconds=config.trade_date_sleep_seconds,
            fetcher=lambda trade_date: _retry_call(
                lambda: _filter_selected_codes(pro.stock_st(
                    trade_date=trade_date,
                    fields=STOCK_ST_FIELDS,
                ), plan["codes"]),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="stock_st",
                key=trade_date,
            ),
            columns=["ts_code", "name", "trade_date", "type", "type_name"],
        )

        _run_trade_date_stage(
            package_root=package_root,
            progress=progress,
            stage_name="suspend_d",
            trade_dates=plan["trade_dates"],
            sleep_seconds=config.trade_date_sleep_seconds,
            fetcher=lambda trade_date: _retry_call(
                lambda: _filter_selected_codes(pro.suspend_d(
                    start_date=trade_date,
                    end_date=trade_date,
                    fields=SUSPEND_D_FIELDS,
                ), plan["codes"]),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="suspend_d",
                key=trade_date,
            ),
            columns=["ts_code", "trade_date", "suspend_timing", "suspend_type"],
        )

        _run_trade_date_stage(
            package_root=package_root,
            progress=progress,
            stage_name="adj_factor",
            trade_dates=plan["trade_dates"],
            sleep_seconds=config.trade_date_sleep_seconds,
            fetcher=lambda trade_date: _retry_call(
                lambda: _filter_selected_codes(pro.adj_factor(
                    trade_date=trade_date,
                    fields="ts_code,trade_date,adj_factor",
                ), plan["codes"]),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="adj_factor",
                key=trade_date,
            ),
            columns=["ts_code", "trade_date", "adj_factor"],
        )

        _run_trade_date_stage(
            package_root=package_root,
            progress=progress,
            stage_name="moneyflow",
            trade_dates=plan["trade_dates"],
            sleep_seconds=config.trade_date_sleep_seconds,
            fetcher=lambda trade_date: _retry_call(
                lambda: _filter_selected_codes(pro.moneyflow(
                    trade_date=trade_date,
                    fields="ts_code,trade_date,buy_sm_vol,buy_sm_amount,sell_sm_vol,sell_sm_amount,buy_lg_vol,buy_lg_amount,sell_lg_vol,sell_lg_amount,net_mf_vol,net_mf_amount",
                ), plan["codes"]),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="moneyflow",
                key=trade_date,
            ),
            columns=["ts_code", "trade_date", "buy_sm_vol", "buy_sm_amount", "sell_sm_vol", "sell_sm_amount", "buy_lg_vol", "buy_lg_amount", "sell_lg_vol", "sell_lg_amount", "net_mf_vol", "net_mf_amount"],
        )

        _run_trade_date_stage(
            package_root=package_root,
            progress=progress,
            stage_name="margin_detail",
            trade_dates=plan["trade_dates"],
            sleep_seconds=config.trade_date_sleep_seconds,
            fetcher=lambda trade_date: _retry_call(
                lambda: _filter_selected_codes(pro.margin_detail(
                    trade_date=trade_date,
                    fields="trade_date,ts_code,rzye,rzmre,rqye",
                ), plan["codes"]),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="margin_detail",
                key=trade_date,
            ),
            columns=["trade_date", "ts_code", "rzye", "rzmre", "rqye"],
        )

        hfq_stage = progress["stages"]["pro_bar_hfq"]
        hfq_stage.update(
            {
                "status": "completed",
                "cursor": len(plan["codes"]),
                "total": len(plan["codes"]),
                "current_key": None,
                "derived_locally": True,
                "derivation": "daily_ohlc_times_adj_factor",
                "api_calls": 0,
            }
        )
        _update_manifest(
            package_root,
            hfq_derivation={
                "mode": "local",
                "formula": "daily_ohlc_times_adj_factor",
                "api_calls": 0,
            },
        )
        _save_progress(package_root, progress)

        _run_code_stage(
            package_root=package_root,
            progress=progress,
            stage_name="income",
            codes=plan["codes"],
            sleep_seconds=_stage_sleep_seconds("income", config.code_sleep_seconds),
            fetcher=lambda code: _retry_call(
                lambda: pro.income(ts_code=code, start_date=plan["padded_start_date"], end_date=plan["effective_target_date"], fields="ts_code,ann_date,f_ann_date,end_date,n_income_attr_p,basic_eps"),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="income",
                key=code,
            ),
            columns=["ts_code", "ann_date", "f_ann_date", "end_date", "n_income_attr_p", "basic_eps"],
        )

        _run_code_stage(
            package_root=package_root,
            progress=progress,
            stage_name="balancesheet",
            codes=plan["codes"],
            sleep_seconds=_stage_sleep_seconds("balancesheet", config.code_sleep_seconds),
            fetcher=lambda code: _retry_call(
                lambda: pro.balancesheet(ts_code=code, start_date=plan["padded_start_date"], end_date=plan["effective_target_date"], fields="ts_code,ann_date,f_ann_date,end_date,total_hldr_eqy_exc_min_int,total_assets"),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="balancesheet",
                key=code,
            ),
            columns=["ts_code", "ann_date", "f_ann_date", "end_date", "total_hldr_eqy_exc_min_int", "total_assets"],
        )

        _run_code_stage(
            package_root=package_root,
            progress=progress,
            stage_name="fina_indicator",
            codes=plan["codes"],
            sleep_seconds=_stage_sleep_seconds("fina_indicator", config.code_sleep_seconds),
            fetcher=lambda code: _retry_call(
                lambda: pro.fina_indicator(ts_code=code, start_date=plan["padded_start_date"], end_date=plan["effective_target_date"], fields="ts_code,ann_date,end_date,eps,roe,roa"),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="fina_indicator",
                key=code,
            ),
            columns=["ts_code", "ann_date", "end_date", "eps", "roe", "roa"],
        )

        _run_code_stage(
            package_root=package_root,
            progress=progress,
            stage_name="holder_num",
            codes=plan["codes"],
            sleep_seconds=_stage_sleep_seconds("holder_num", config.code_sleep_seconds),
            fetcher=lambda code: _retry_call(
                lambda: pro.stk_holdernumber(ts_code=code, start_date=plan["padded_start_date"], end_date=plan["effective_target_date"], fields="ts_code,ann_date,end_date,holder_num"),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="holder_num",
                key=code,
            ),
            columns=["ts_code", "ann_date", "end_date", "holder_num"],
        )

        _run_code_stage(
            package_root=package_root,
            progress=progress,
            stage_name="cyq_perf",
            codes=plan["codes"],
            sleep_seconds=_stage_sleep_seconds("cyq_perf", config.code_sleep_seconds),
            fetcher=lambda code: _retry_call(
                lambda: _fetch_cyq_perf_windowed(
                    pro,
                    code=code,
                    start_date=plan["start_date"],
                    end_date=plan["effective_target_date"],
                    fields="ts_code,trade_date,cost_15pct,cost_85pct,weight_avg",
                ),
                attempts=_stage_retry_attempts("cyq_perf", config.retry_attempts),
                base_seconds=config.retry_base_seconds,
                stage="cyq_perf",
                key=code,
            ),
            columns=["ts_code", "trade_date", "cost_15pct", "cost_85pct", "weight_avg"],
            refresh_every=25,
            refresh_hook=getattr(pro, "reset_session", None),
        )

        _run_code_stage(
            package_root=package_root,
            progress=progress,
            stage_name="index_daily",
            codes=BENCHMARK_INDEX_CODES,
            sleep_seconds=_stage_sleep_seconds("index_daily", config.code_sleep_seconds),
            fetcher=lambda code: _retry_call(
                lambda: pro.index_daily(ts_code=code, start_date=plan["start_date"], end_date=plan["effective_target_date"], fields=INDEX_DAILY_FIELDS),
                attempts=config.retry_attempts,
                base_seconds=config.retry_base_seconds,
                stage="index_daily",
                key=code,
            ),
            columns=["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"],
        )

    raw_quality_stage = progress["stages"]["raw_quality_report"]
    raw_quality_stage["status"] = "running"
    _save_progress(package_root, progress)
    manifest_snapshot = _read_json(_manifest_path(package_root))
    raw_quality = _build_raw_quality_report(package_root, manifest_snapshot, plan["trade_dates"])
    raw_quality_stage["cursor"] = 1
    raw_quality_stage["total"] = 1
    raw_quality_stage["status"] = "completed" if raw_quality.get("passed") else "failed"
    _save_progress(package_root, progress)
    if not raw_quality.get("passed"):
        progress["status"] = "failed"
        _save_progress(package_root, progress)
        _update_manifest(package_root, status="raw_quality_failed", raw_quality_report=raw_quality)
        _status_payload(status="failed", package_id=plan["package_id"], stage="raw_quality_report", raw_quality=raw_quality)
        raise RuntimeError("tushare_raw_quality_failed")

    assembly = _assemble_research_daily(
        package_root=package_root,
        progress=progress,
        trade_dates=plan["trade_dates"],
        trade_date_chunk_size=config.trade_date_chunk_size,
    )
    index_daily_path = _build_index_daily_output(package_root)
    manifest_snapshot = _read_json(_manifest_path(package_root))
    metadata_path = _write_metadata(package_root, manifest_snapshot)
    progress["stages"]["assemble_research_daily"]["status"] = "completed"
    _save_progress(package_root, progress)

    quality = _build_quality_report(package_root, _silver_root(package_root) / "research_daily.h5", manifest_snapshot)
    progress["stages"]["quality_report"]["cursor"] = 1
    progress["stages"]["quality_report"]["total"] = 1
    progress["stages"]["quality_report"]["status"] = "completed" if quality.get("passed") else "failed"
    progress["status"] = "completed" if quality.get("passed") else "failed"
    _save_progress(package_root, progress)
    assembly["index_daily_path"] = index_daily_path
    assembly["metadata_path"] = metadata_path
    final_status = "completed" if quality.get("passed") else "quality_failed"
    _update_manifest(package_root, status=final_status, raw_quality_report=raw_quality, quality_report=quality, outputs=assembly)
    _status_payload(status=final_status, package_id=plan["package_id"], raw_quality=raw_quality, quality=quality, outputs=assembly)
    if not quality.get("passed"):
        raise RuntimeError("tushare_assembled_quality_failed")
    return {
        "status": "completed",
        "package_id": plan["package_id"],
        "package_root": str(package_root),
        "effective_target_date": plan["effective_target_date"],
        "trade_date_count": len(plan["trade_dates"]),
        "code_count": len(plan["codes"]),
        "field_count": len(RESEARCH_DAILY_FIELDS),
        "quality_report": quality,
        "outputs": assembly,
    }


def tushare_full_rebuild_status(*, package_id: str | None = None, latest: bool = True) -> dict[str, Any]:
    if latest and not package_id:
        candidates = sorted(STAGING_ROOT.glob("*/manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        package_root = None
        for candidate in candidates:
            manifest = _read_json(candidate)
            if manifest.get("source") == "tushare":
                package_root = candidate.parent
                break
        if package_root is None:
            return {"status": "not_found"}
    else:
        if not package_id:
            raise ValueError("package_id_required")
        package_root = _package_root(package_id)
    return {
        "status": "ok",
        "package_root": str(package_root),
        "manifest": _read_json(_manifest_path(package_root)),
        "progress": _read_json(_progress_path(package_root)),
    }
