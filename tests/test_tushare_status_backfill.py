from pathlib import Path

import pandas as pd

from domain.data_foundation.tushare_status_backfill import build_tushare_status_backfill


def test_status_backfill_adds_two_status_fields_without_dropping_rows(tmp_path, monkeypatch):
    source_hdf = Path(tmp_path) / "stock_daily.h5"
    daily = pd.DataFrame(
        [
            {
                "trade_date": pd.Timestamp("2026-01-02"),
                "code": "000001.SZ",
                "kline_time": pd.Timestamp("2026-01-02"),
                "SECURITY_NAME": "平安银行",
                "MARKET_CODE": "SZ",
                "LIST_DATE": "19910403",
                "list_status": "L",
                "close": 10.0,
            },
            {
                "trade_date": pd.Timestamp("2026-01-02"),
                "code": "000002.SZ",
                "kline_time": pd.Timestamp("2026-01-02"),
                "SECURITY_NAME": "普通名称",
                "MARKET_CODE": "SZ",
                "LIST_DATE": "19910129",
                "list_status": "L",
                "close": 20.0,
            },
            {
                "trade_date": pd.Timestamp("2026-01-02"),
                "code": "688287.SH",
                "kline_time": pd.Timestamp("2026-01-02"),
                "SECURITY_NAME": "退市观典",
                "MARKET_CODE": "SH",
                "LIST_DATE": "20220525",
                "list_status": "L",
                "close": 30.0,
            },
            {
                "trade_date": pd.Timestamp("2026-01-02"),
                "code": "000003.SZ",
                "kline_time": pd.Timestamp("2026-01-02"),
                "SECURITY_NAME": "*ST当前名",
                "MARKET_CODE": "SZ",
                "LIST_DATE": "19910101",
                "list_status": "L",
                "close": 40.0,
            },
        ]
    ).set_index("trade_date")
    daily.to_hdf(source_hdf, key="/daily", mode="w", format="table")
    monkeypatch.setattr("domain.data_foundation.tushare_status_backfill.STAGING_ROOT", tmp_path / "staging")
    monkeypatch.setattr("domain.data_foundation.tushare_status_backfill.CURRENT_PRODUCTION_DATASET_FILE", tmp_path / "CURRENT.json")

    result = build_tushare_status_backfill(
        package_id="status-test",
        source_hdf=source_hdf,
        fetch_live=False,
        stock_basic_df=pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "000002.SZ", "688287.SH", "000003.SZ"],
                "name": ["平安银行", "普通名称", "退市观典", "*ST当前名"],
                "list_status": ["L", "P", "D", "L"],
                "list_date": ["19910403", "19910129", "20220525", "19910101"],
                "delist_date": [None, None, "20251231", None],
            }
        ),
        stock_st_df=pd.DataFrame(
            {
                "ts_code": ["000002.SZ"],
                "name": ["普通名称"],
                "trade_date": ["20260102"],
                "type": ["ST"],
                "type_name": ["风险警示板"],
            }
        ),
    )

    out = pd.read_hdf(result["output_hdf"], key="/daily").sort_values("code")
    by_code = out.set_index("code")
    assert result["row_count"] == 4
    assert by_code.loc["000001.SZ", "list_status"] == "L"
    assert by_code.loc["000001.SZ", "st_status"] == "NORMAL"
    assert by_code.loc["000002.SZ", "list_status"] == "L"
    assert by_code.loc["000002.SZ", "st_status"] == "ST"
    assert by_code.loc["688287.SH", "list_status"] == "D"
    assert by_code.loc["688287.SH", "st_status"] == "DELIST"
    assert by_code.loc["688287.SH", "close"] == 30.0
    assert by_code.loc["000003.SZ", "list_status"] == "L"
    assert by_code.loc["000003.SZ", "st_status"] == "NORMAL"


def test_status_backfill_uses_point_in_time_namechange_for_st_and_delist(tmp_path, monkeypatch):
    source_hdf = Path(tmp_path) / "stock_daily.h5"
    daily = pd.DataFrame(
        [
            {
                "trade_date": pd.Timestamp("2024-10-31"),
                "code": "688287.SH",
                "kline_time": pd.Timestamp("2024-10-31"),
                "SECURITY_NAME": "退市观典",
                "MARKET_CODE": "SH",
                "LIST_DATE": "20220525",
                "list_status": "D",
                "close": 10.0,
            },
            {
                "trade_date": pd.Timestamp("2024-11-04"),
                "code": "688287.SH",
                "kline_time": pd.Timestamp("2024-11-04"),
                "SECURITY_NAME": "退市观典",
                "MARKET_CODE": "SH",
                "LIST_DATE": "20220525",
                "list_status": "D",
                "close": 9.0,
            },
            {
                "trade_date": pd.Timestamp("2025-04-30"),
                "code": "688287.SH",
                "kline_time": pd.Timestamp("2025-04-30"),
                "SECURITY_NAME": "退市观典",
                "MARKET_CODE": "SH",
                "LIST_DATE": "20220525",
                "list_status": "D",
                "close": 8.0,
            },
            {
                "trade_date": pd.Timestamp("2026-05-19"),
                "code": "688287.SH",
                "kline_time": pd.Timestamp("2026-05-19"),
                "SECURITY_NAME": "退市观典",
                "MARKET_CODE": "SH",
                "LIST_DATE": "20220525",
                "list_status": "D",
                "close": 7.0,
            },
        ]
    ).set_index("trade_date")
    daily.to_hdf(source_hdf, key="/daily", mode="w", format="table")
    monkeypatch.setattr("domain.data_foundation.tushare_status_backfill.STAGING_ROOT", tmp_path / "staging")
    monkeypatch.setattr("domain.data_foundation.tushare_status_backfill.CURRENT_PRODUCTION_DATASET_FILE", tmp_path / "CURRENT.json")

    result = build_tushare_status_backfill(
        package_id="namechange-test",
        source_hdf=source_hdf,
        fetch_live=False,
        stock_basic_df=pd.DataFrame(
            {
                "ts_code": ["688287.SH"],
                "name": ["退市观典"],
                "list_status": ["D"],
                "list_date": ["20220525"],
                "delist_date": ["20260610"],
            }
        ),
        stock_st_df=pd.DataFrame(columns=["ts_code", "name", "trade_date", "type", "type_name"]),
        namechange_df=pd.DataFrame(
            {
                "ts_code": ["688287.SH", "688287.SH", "688287.SH", "688287.SH"],
                "name": ["观典防务", "ST观典", "*ST观典", "退市观典"],
                "start_date": ["20220525", "20241104", "20250430", "20260519"],
                "end_date": ["20241103", "20250429", "20260518", None],
                "ann_date": ["20220525", "20241104", "20250430", "20260519"],
                "change_reason": ["首发上市", "实施其他风险警示", "实施退市风险警示", "终止上市"],
            }
        ),
    )

    out = pd.read_hdf(result["output_hdf"], key="/daily").reset_index().sort_values("trade_date")
    assert out["SECURITY_NAME"].tolist() == ["观典防务", "ST观典", "*ST观典", "退市观典"]
    assert out["st_status"].tolist() == ["NORMAL", "ST", "ST", "DELIST"]
    assert out["list_status"].tolist() == ["L", "L", "L", "L"]
