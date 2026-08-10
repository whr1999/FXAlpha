import pandas as pd
import pytest

recommendation = pytest.importorskip("domain.trading.recommendation")
signals = pytest.importorskip("domain.trading.signals")
stock_metadata = pytest.importorskip("domain.data_foundation.stock_metadata")


def test_build_order_preview_diff_and_lot_rounding(monkeypatch):
    monkeypatch.setattr(
        recommendation,
        "_estimated_price_map",
        lambda trade_date, instruments: {"SH600000": 10.0, "SZ000001": 20.0},
    )
    target_df = pd.DataFrame(
        [
            {"trade_date": "2026-05-20", "instrument": "SH600000", "weight": 0.2},
            {"trade_date": "2026-05-20", "instrument": "SZ000001", "weight": 0.1},
        ]
    )
    score_df = pd.DataFrame(
        [
            {"instrument": "SH600000", "score": 0.8},
            {"instrument": "SZ000001", "score": 0.7},
        ]
    )
    current_state = {
        "positions": {
            "SH600000": {"amount": 100, "price": 9.5},
            "SZ000001": {"amount": 200, "price": 18.0},
        }
    }

    preview = recommendation.build_order_preview(
        signal_date="2026-05-19",
        execution_date="2026-05-20",
        target_df=target_df,
        score_df=score_df,
        current_state=current_state,
        total_capital=100000,
    )

    by_code = {row["instrument"]: row for row in preview.to_dict("records")}
    assert by_code["SH600000"]["target_shares"] == 2000
    assert by_code["SH600000"]["delta_shares"] == 1900
    assert by_code["SH600000"]["action"] == "buy"
    assert by_code["SZ000001"]["target_shares"] == 500
    assert by_code["SZ000001"]["delta_shares"] == 300


def test_pending_execution_date_uses_next_calendar(monkeypatch):
    monkeypatch.setattr(
        recommendation,
        "next_trading_date",
        lambda signal_date: "2026-05-20" if signal_date == "2026-05-19" else None,
    )

    assert recommendation.resolve_pending_execution_date("2026-05-19") == "2026-05-20"
    assert recommendation.resolve_pending_execution_date("2026-05-19", "2026-05-21") == "2026-05-21"


def test_st_filter_excludes_delist_named_candidates(monkeypatch):
    monkeypatch.setattr(
        signals,
        "load_stock_identity_rows",
        lambda: pd.DataFrame(
            [
                {"market_code": "688287.SH", "security_name": "退市观典", "list_status": "L", "st_status": "DELIST"},
                {"market_code": "000056.SZ", "security_name": "皇庭国际", "list_status": "L", "st_status": "ST"},
                {"market_code": "600000.SH", "security_name": "浦发银行", "list_status": "L", "st_status": "NORMAL"},
            ]
        ),
    )
    score_df = pd.DataFrame(
        [
            {"instrument": "688287sh", "score": 0.99},
            {"instrument": "000056sz", "score": 0.98},
            {"instrument": "600000sh", "score": 0.97},
        ]
    )

    filtered, summary = signals._apply_st_filter(score_df)

    assert filtered["instrument"].tolist() == ["600000sh"]
    assert summary["st_filtered_instruments"] == ["688287sh", "000056sz"]
    assert summary["st_filtered_names"] == ["退市观典", "皇庭国际"]


def test_point_in_time_identity_window_does_not_apply_future_st_status(monkeypatch, tmp_path):
    hdf = tmp_path / "stock_daily.h5"
    frame = pd.DataFrame(
        [
            {"trade_date": "2026-07-01", "code": "000001.SZ", "SECURITY_NAME": "样本A", "MARKET_CODE": "SZ", "LIST_DATE": "20200101", "list_status": "L", "st_status": "NORMAL"},
            {"trade_date": "2026-07-02", "code": "000001.SZ", "SECURITY_NAME": "ST样本A", "MARKET_CODE": "SZ", "LIST_DATE": "20200101", "list_status": "L", "st_status": "ST"},
            {"trade_date": "2026-07-01", "code": "600000.SH", "SECURITY_NAME": "样本B", "MARKET_CODE": "SH", "LIST_DATE": "20200101", "list_status": "L", "st_status": "NORMAL"},
            {"trade_date": "2026-07-02", "code": "600000.SH", "SECURITY_NAME": "样本B", "MARKET_CODE": "SH", "LIST_DATE": "20200101", "list_status": "L", "st_status": "NORMAL"},
        ]
    )
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.set_index("trade_date")
    frame.to_hdf(
        hdf,
        key="daily",
        format="table",
        data_columns=["code", "SECURITY_NAME", "MARKET_CODE", "LIST_DATE", "list_status", "st_status"],
    )
    monkeypatch.setattr(stock_metadata, "PRODUCTION_RAW_HDF5", hdf)

    history = stock_metadata.load_stock_identity_rows_for_window("2026-07-01", "2026-07-02")
    scores = pd.DataFrame([{"instrument": "000001sz", "score": 1.0}, {"instrument": "600000sh", "score": 0.9}])
    day1 = history.loc[history["trade_date"] == pd.Timestamp("2026-07-01")]
    day2 = history.loc[history["trade_date"] == pd.Timestamp("2026-07-02")]

    eligible_day1, summary_day1 = signals._apply_st_filter(scores, identity_rows=day1)
    eligible_day2, summary_day2 = signals._apply_st_filter(scores, identity_rows=day2)

    assert eligible_day1["instrument"].tolist() == ["000001sz", "600000sh"]
    assert eligible_day2["instrument"].tolist() == ["600000sh"]
    assert summary_day1["identity_match_ratio"] == 1.0
    assert summary_day2["identity_policy"] == "point_in_time_trade_date"
