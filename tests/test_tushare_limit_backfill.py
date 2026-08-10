from __future__ import annotations

import json

import pandas as pd
import pytest

from domain.data_foundation import tushare_limit_backfill


def _write_source_hdf(path):
    frame = pd.DataFrame(
        {
            "code": ["000001.SZ", "000002.SZ", "000001.SH"],
            "kline_time": [pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-02")],
            "high": [10.2, 20.3, 3010.0],
            "low": [9.8, 19.7, 2990.0],
            "close": [10.0, 20.0, 3000.0],
            "pre_close": [9.9, 19.9, 2995.0],
            "pct_chg": [1.0, 0.5, 0.2],
            "amp": [4.0, 3.0, 0.7],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-02")] * 3, name="trade_date"),
    )
    frame.to_hdf(path, key="/daily", mode="w", format="table")


def test_limit_backfill_adds_official_limit_prices_without_promoting(tmp_path, monkeypatch):
    source_hdf = tmp_path / "stock_daily.h5"
    current_file = tmp_path / "CURRENT.json"
    _write_source_hdf(source_hdf)
    current_file.write_text(json.dumps({"production_package_id": "prod-old"}), encoding="utf-8")
    monkeypatch.setattr(tushare_limit_backfill, "STAGING_ROOT", tmp_path / "staging")
    monkeypatch.setattr(tushare_limit_backfill, "CURRENT_PRODUCTION_DATASET_FILE", current_file)

    result = tushare_limit_backfill.build_tushare_limit_backfill(
        package_id="limit-pkg",
        source_hdf=source_hdf,
        fetch_live=False,
        stk_limit_df=pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "000002.SZ"],
                "trade_date": ["20260102", "20260102"],
                "pre_close": [9.5, 19.5],
                "up_limit": [10.45, 21.45],
                "down_limit": [8.55, 17.55],
            }
        ),
    )

    assert result["status"] == "completed"
    assert result["package_id"] == "limit-pkg"
    assert result["coverage"]["stock_row_count"] == 2
    assert result["coverage"]["passed"] is True
    assert result["coverage"]["official_limit_row_count"] == 2
    assert result["coverage"]["coverage_ratio"] == 1.0
    assert result["changed_counts"] == {"up_limit": 2, "down_limit": 2}
    out = pd.read_hdf(tmp_path / "staging" / "limit-pkg" / "raw" / "stock_daily.h5", key="/daily")
    stock = out[out["code"].eq("000001.SZ")].iloc[0]
    index = out[out["code"].eq("000001.SH")].iloc[0]
    assert stock["up_limit"] == 10.45
    assert stock["down_limit"] == 8.55
    assert stock["stk_limit_pre_close"] == 9.5
    assert stock["pre_close"] == 9.5
    assert stock["pct_chg"] == pytest.approx((10.0 - 9.5) / 9.5 * 100.0)
    assert stock["amp"] == pytest.approx((10.2 - 9.8) / 9.5 * 100.0)
    assert pd.isna(index["up_limit"])


def test_limit_backfill_fetches_stk_limit_by_existing_trade_dates(tmp_path, monkeypatch):
    source_hdf = tmp_path / "stock_daily.h5"
    current_file = tmp_path / "CURRENT.json"
    _write_source_hdf(source_hdf)
    current_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tushare_limit_backfill, "STAGING_ROOT", tmp_path / "staging")
    monkeypatch.setattr(tushare_limit_backfill, "CURRENT_PRODUCTION_DATASET_FILE", current_file)

    class FakePro:
        def __init__(self):
            self.calls = []

        def stk_limit(self, **kwargs):
            self.calls.append(kwargs["trade_date"])
            return pd.DataFrame(
                {
                    "ts_code": ["000001.SZ", "000002.SZ"],
                    "trade_date": [kwargs["trade_date"], kwargs["trade_date"]],
                    "pre_close": [9.5, 19.5],
                    "up_limit": [10.45, 21.45],
                    "down_limit": [8.55, 17.55],
                }
            )

    fake = FakePro()
    monkeypatch.setattr(tushare_limit_backfill, "get_tushare_client", lambda network_mode="direct": fake)

    result = tushare_limit_backfill.build_tushare_limit_backfill(
        package_id="limit-live",
        source_hdf=source_hdf,
        proxy_mode="direct",
    )

    assert fake.calls == ["20260102"]
    assert result["coverage"]["official_limit_row_count"] == 2
    assert (tmp_path / "staging" / "limit-live" / "raw" / "stk_limit" / "20260102.parquet").exists()


def test_missing_pre_close_dates_and_partial_fetch_preserve_existing_values(tmp_path, monkeypatch):
    source_hdf = tmp_path / "stock_daily.h5"
    current_file = tmp_path / "CURRENT.json"
    frame = pd.DataFrame(
        {
            "code": ["000001.SZ", "000001.SZ"],
            "kline_time": pd.to_datetime(["2026-01-02", "2026-01-05"]),
            "list_status": ["L", "L"],
            "high": [10.2, 10.6],
            "low": [9.8, 10.1],
            "close": [10.0, 10.5],
            "pre_close": [9.5, 10.0],
            "pct_chg": [5.0, 5.0],
            "amp": [4.0, 5.0],
            "stk_limit_pre_close": [9.5, None],
            "up_limit": [10.45, 11.0],
            "down_limit": [8.55, 9.0],
        },
        index=pd.DatetimeIndex(pd.to_datetime(["2026-01-02", "2026-01-05"]), name="trade_date"),
    )
    frame.to_hdf(source_hdf, key="/daily", mode="w", format="table")
    current_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tushare_limit_backfill, "STAGING_ROOT", tmp_path / "staging")
    monkeypatch.setattr(tushare_limit_backfill, "CURRENT_PRODUCTION_DATASET_FILE", current_file)

    assert tushare_limit_backfill._missing_pre_close_trade_dates(source_hdf, chunk_rows=1) == ["20260105"]
    result = tushare_limit_backfill.build_tushare_limit_backfill(
        package_id="partial-limit",
        source_hdf=source_hdf,
        fetch_live=False,
        stk_limit_df=pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": ["20260105"],
                "pre_close": [10.2],
                "up_limit": [11.22],
                "down_limit": [9.18],
            }
        ),
        chunk_rows=1,
    )

    out = pd.read_hdf(result["output_hdf"], key="/daily").sort_values("kline_time")
    assert out.iloc[0]["stk_limit_pre_close"] == 9.5
    assert out.iloc[0]["up_limit"] == 10.45
    assert out.iloc[1]["stk_limit_pre_close"] == 10.2
    assert out.iloc[1]["pre_close"] == 10.2
