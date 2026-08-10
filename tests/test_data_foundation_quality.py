from pathlib import Path

import numpy as np
import pandas as pd

from domain.data_foundation.quality_check import check
from domain.data_foundation.tushare_production import recompute_derived_price_fields


def test_quality_check_flags_daily_price_coverage_gaps(tmp_path):
    hdf_path = Path(tmp_path) / "stock_daily.h5"
    rows = []
    for date in ["2026-01-02", "2026-01-05"]:
        for idx in range(100):
            missing_pre_close = date == "2026-01-05" and idx >= 50
            rows.append(
                {
                    "trade_date": pd.Timestamp(date),
                    "code": f"{idx:06d}.SZ",
                    "kline_time": pd.Timestamp(date),
                    "SECURITY_NAME": "x",
                    "MARKET_CODE": "SZ",
                    "list_status": "L",
                    "st_status": "NORMAL",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.5,
                    "close": 10.0,
                    "volume": 1000.0,
                    "amount": 10000.0,
                    "pre_close": np.nan if missing_pre_close else 10.0,
                    "backward_factor": 1.0,
                    "adj_open": 10.0,
                    "adj_high": 10.5,
                    "adj_low": 9.5,
                    "adj_close": 10.0,
                    "adj_pre_close": np.nan if missing_pre_close else 10.0,
                    "adj_pct_chg": np.nan if missing_pre_close else 0.0,
                    "adj_amp": np.nan if missing_pre_close else 10.0,
                }
            )
    daily = pd.DataFrame(rows).set_index("trade_date")
    with pd.HDFStore(hdf_path, mode="w") as store:
        store.put("/daily", daily, format="fixed")

    result = check(hdf_path)

    assert not result["passed"]
    assert any("market_core_daily_coverage daily coverage below 98%" in issue for issue in result["issues"])
    assert any("adjusted_price_daily_coverage daily coverage below 98%" in issue for issue in result["issues"])


def test_recompute_derived_price_fields_repairs_window_edge_nulls(tmp_path):
    hdf_path = Path(tmp_path) / "stock_daily.h5"
    daily = pd.DataFrame(
        {
            "code": ["000001.SZ", "000001.SZ", "000001.SZ"],
            "kline_time": pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03"]),
            "SECURITY_NAME": ["x", "x", "x"],
            "MARKET_CODE": ["SZ", "SZ", "SZ"],
            "LIST_DATE": ["19910403", "19910403", "19910403"],
            "list_status": ["L", "L", "L"],
            "st_status": ["NORMAL", "NORMAL", "NORMAL"],
            "open": [10.0, 11.0, 12.0],
            "high": [10.5, 11.5, 12.5],
            "low": [9.5, 10.5, 11.5],
            "close": [10.0, 11.0, 12.0],
            "volume": [1000.0, 1000.0, 1000.0],
            "amount": [10000.0, 11000.0, 12000.0],
            "pre_close": [np.nan, np.nan, 11.0],
            "pct_chg": [np.nan, np.nan, 9.0909],
            "amp": [np.nan, np.nan, 9.0909],
            "backward_factor": [2.0, 2.0, 2.0],
            "adj_open": [20.0, 22.0, 24.0],
            "adj_high": [21.0, 23.0, 25.0],
            "adj_low": [19.0, 21.0, 23.0],
            "adj_close": [20.0, 22.0, 24.0],
            "adj_pre_close": [np.nan, np.nan, 22.0],
            "adj_pct_chg": [np.nan, np.nan, 9.0909],
            "adj_amp": [np.nan, np.nan, 9.0909],
        },
        index=pd.DatetimeIndex(["2026-06-01", "2026-06-02", "2026-06-03"], name="trade_date"),
    )
    with pd.HDFStore(hdf_path, mode="w") as store:
        store.put("/daily", daily, format="fixed")

    result = recompute_derived_price_fields(hdf_path)
    repaired = pd.read_hdf(hdf_path, "/daily").sort_values(["code", "kline_time"])

    assert result["status"] == "completed"
    assert pd.isna(repaired["pre_close"].iloc[0])
    assert repaired["pre_close"].iloc[1:].tolist() == [10.0, 11.0]
    assert pd.isna(repaired["adj_pre_close"].iloc[0])
    assert repaired["adj_pre_close"].iloc[1:].tolist() == [20.0, 22.0]
    assert repaired["pct_chg"].iloc[1] == 10.0
    assert repaired["adj_pct_chg"].iloc[1] == 10.0


def test_daily_compat_flags_preboundary_benchmark_gap(tmp_path):
    hdf_path = Path(tmp_path) / "stock_daily.h5"
    rows = []
    for code in ["000001.SZ", "000300.SH", "000905.SH", "000852.SH"]:
        for date in ["2026-06-16", "2026-06-17"]:
            rows.append(
                {
                    "trade_date": pd.Timestamp(date),
                    "code": code,
                    "kline_time": pd.Timestamp(date),
                    "list_status": "I" if code.endswith(".SH") else "L",
                    "st_status": "NORMAL",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.5,
                    "close": 10.0,
                    "volume": 1000.0,
                    "amount": 10000.0,
                    "pre_close": 10.0,
                    "backward_factor": 1.0,
                    "adj_open": 10.0,
                    "adj_high": 10.5,
                    "adj_low": 9.5,
                    "adj_close": 10.0,
                    "adj_pre_close": 10.0,
                }
            )
    rows.append(
        {
            "trade_date": pd.Timestamp("2026-06-15"),
            "code": "000001.SZ",
            "kline_time": pd.Timestamp("2026-06-15"),
            "list_status": "L",
            "st_status": "NORMAL",
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.0,
            "volume": 1000.0,
            "amount": 10000.0,
            "pre_close": 10.0,
            "backward_factor": 1.0,
            "adj_open": 10.0,
            "adj_high": 10.5,
            "adj_low": 9.5,
            "adj_close": 10.0,
            "adj_pre_close": 10.0,
        }
    )
    daily = pd.DataFrame(rows).set_index("trade_date")
    daily.to_hdf(hdf_path, key="/daily", mode="w", format="table")

    result = check(hdf_path, profile="daily_compat", replace_from_date="20260616")

    assert not result["passed"]
    assert "daily_compat preboundary benchmark missing: 000300.SH:2026-06-15" in result["issues"]


def test_daily_compat_allows_latest_listing_day_pre_close_null(tmp_path):
    hdf_path = Path(tmp_path) / "stock_daily.h5"
    rows = []
    for code in ["000300.SH", "000905.SH", "000852.SH"]:
        rows.append(
            {
                "trade_date": pd.Timestamp("2026-06-26"),
                "code": code,
                "kline_time": pd.Timestamp("2026-06-26"),
                "LIST_DATE": "20050101",
                "list_status": "I",
                "st_status": "NORMAL",
                "open": 10.0,
                "high": 10.5,
                "low": 9.5,
                "close": 10.0,
                "volume": 1000.0,
                "amount": 10000.0,
                "pre_close": 10.0,
                "backward_factor": 1.0,
                "adj_open": 10.0,
                "adj_high": 10.5,
                "adj_low": 9.5,
                "adj_close": 10.0,
                "adj_pre_close": 10.0,
            }
        )
    rows.append(
        {
            "trade_date": pd.Timestamp("2026-06-26"),
            "code": "001399.SZ",
            "kline_time": pd.Timestamp("2026-06-26"),
            "LIST_DATE": "20260626",
            "list_status": "L",
            "st_status": "NORMAL",
            "open": 31.6,
            "high": 76.0,
            "low": 31.5,
            "close": 42.0,
            "volume": 2838122.24,
            "amount": 13030990.0,
            "pre_close": np.nan,
            "backward_factor": 1.0,
            "adj_open": 31.6,
            "adj_high": 76.0,
            "adj_low": 31.5,
            "adj_close": 42.0,
            "adj_pre_close": np.nan,
        }
    )
    daily = pd.DataFrame(rows).set_index("trade_date")
    daily.to_hdf(hdf_path, key="/daily", mode="w", format="table")

    result = check(hdf_path, profile="daily_compat", replace_from_date="20260626")

    assert result["passed"]
    assert result["latest_nulls"]["pre_close"] == 0
    assert result["latest_structural_nulls"]["pre_close"] == 1
