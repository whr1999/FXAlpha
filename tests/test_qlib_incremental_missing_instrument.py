import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

DATA_FOUNDATION_SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts" / "data_foundation"
sys.path.insert(0, str(DATA_FOUNDATION_SCRIPT_ROOT))

convert_to_qlib_v3 = pytest.importorskip("tushare_raw_to_qlib")


def _read_bin(path: Path) -> tuple[int, np.ndarray]:
    payload = np.fromfile(path, dtype="<f4")
    return int(payload[0]), payload[1:]


def test_qlib_price_semantics_adjusts_vwap_and_chip_cost():
    index = pd.MultiIndex.from_product(
        [pd.to_datetime(["2026-01-02"]), ["000001sz"]],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame(
        {
            "$open": [10.0],
            "$high": [11.0],
            "$low": [9.0],
            "$close": [10.5],
            "$pre_close": [9.5],
            "$amount": [1000.0],
            "$volume": [100.0],
            "$factor": [2.0],
            "$cost_15pct": [9.0],
            "$cost_85pct": [11.0],
            "$weight_avg": [10.0],
        },
        index=index,
    )

    out = convert_to_qlib_v3._prepare_qlib_price_semantics(df)

    row = out.iloc[0]
    assert row["$vwap"] == 200.0
    assert row["$raw_vwap"] == 100.0
    assert row["$volume"] == 50.0
    assert row["$cost_15pct"] == 18.0
    assert row["$cost_85pct"] == 22.0
    assert row["$weight_avg"] == 20.0
    assert row["$raw_cost_15pct"] == 9.0


def test_qlib_incremental_rewrites_missing_instrument_without_new_dates(tmp_path):
    dates = pd.to_datetime(["2026-05-28", "2026-05-29"])
    index = pd.MultiIndex.from_product(
        [dates, ["000001sz", "000002sz"]],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame({"$open": [1.0, 2.0, 3.0, 4.0], "$close": [1.1, 2.1, 3.1, 4.1]}, index=index)

    qlib_dir = tmp_path / "qlib"
    (qlib_dir / "calendars").mkdir(parents=True)
    (qlib_dir / "instruments").mkdir(parents=True)
    (qlib_dir / "features" / "000001sz").mkdir(parents=True)
    (qlib_dir / "calendars" / "day.txt").write_text("2026-05-28\n2026-05-29\n", encoding="utf-8")
    (qlib_dir / "instruments" / "all.txt").write_text(
        "000001sz\t2026-05-28\t2026-05-29\n",
        encoding="utf-8",
    )

    convert_to_qlib_v3.configure_paths(output_dir=str(qlib_dir))
    result = convert_to_qlib_v3._incremental_convert(convert_to_qlib_v3._prepare_qlib_price_semantics(df))

    assert result["effective_mode"] == "incremental"
    assert result["missing_instrument_count"] == 1
    assert result["missing_field_instrument_count"] == 1
    assert result["rewritten_instrument_count"] == 2
    assert (qlib_dir / "features" / "000002sz" / "open.day.bin").exists()
    instruments_text = (qlib_dir / "instruments" / "all.txt").read_text(encoding="utf-8")
    assert "000001sz\t2026-05-28\t2026-05-29" in instruments_text
    assert "000002sz\t2026-05-28\t2026-05-29" in instruments_text


def test_qlib_incremental_rewrites_existing_instrument_when_field_bins_are_missing(tmp_path):
    dates = pd.to_datetime(["2026-05-28", "2026-05-29"])
    index = pd.MultiIndex.from_product(
        [dates, ["000001sz"]],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame(
        {
            "$open": [1.0, 1.1],
            "$close": [1.0, 1.2],
            "$factor": [1.0, 1.0],
            "$change": [0.0, 0.2],
        },
        index=index,
    )
    qlib_dir = tmp_path / "qlib"
    (qlib_dir / "calendars").mkdir(parents=True)
    (qlib_dir / "instruments").mkdir(parents=True)
    (qlib_dir / "features" / "000001sz").mkdir(parents=True)
    (qlib_dir / "calendars" / "day.txt").write_text("2026-05-28\n2026-05-29\n", encoding="utf-8")
    (qlib_dir / "instruments" / "all.txt").write_text(
        "000001sz\t2026-05-28\t2026-05-29\n",
        encoding="utf-8",
    )

    convert_to_qlib_v3.configure_paths(output_dir=str(qlib_dir))
    result = convert_to_qlib_v3._incremental_convert(convert_to_qlib_v3._prepare_qlib_price_semantics(df))

    assert result["skipped"] is False
    assert result["missing_field_instrument_count"] == 1
    assert result["rewritten_instrument_count"] == 1
    assert result["valid_field_count"] >= 4
    assert "change" in result["valid_fields"]
    assert (qlib_dir / "features" / "000001sz" / "limit_buy_fallback.day.bin").exists()


def test_qlib_converter_exposes_adjusted_close_and_retains_raw_close(tmp_path):
    dates = pd.to_datetime(["2020-08-13", "2020-08-14"])
    index = pd.MultiIndex.from_product(
        [dates, ["000002sz"]],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame(
        {
            "$open": [28.0, 27.0],
            "$high": [29.0, 28.0],
            "$low": [27.0, 26.0],
            "$close": [28.59, 27.68],
            "$pre_close": [28.0, 28.59],
            "$pct_chg": [2.0, -3.18],
            "$amp": [7.14, 6.99],
            "$factor": [148.412, 153.9007],
        },
        index=index,
    )
    qlib_dir = tmp_path / "qlib"
    convert_to_qlib_v3.configure_paths(output_dir=str(qlib_dir))
    result = convert_to_qlib_v3._full_convert(convert_to_qlib_v3._prepare_qlib_price_semantics(df))

    close_start, close_values = _read_bin(qlib_dir / "features" / "000002sz" / "close.day.bin")
    pre_close_start, pre_close_values = _read_bin(qlib_dir / "features" / "000002sz" / "pre_close.day.bin")
    change_start, change_values = _read_bin(qlib_dir / "features" / "000002sz" / "change.day.bin")
    raw_start, raw_values = _read_bin(qlib_dir / "features" / "000002sz" / "raw_close.day.bin")
    assert close_start == pre_close_start == change_start == raw_start == 0
    expected_close = [28.59 * 148.412, 27.68 * 153.9007]
    expected_pre_close = [28.0 * 148.412, expected_close[0]]
    assert close_values.tolist() == pytest.approx(expected_close, rel=1e-5)
    assert pre_close_values.tolist() == pytest.approx(expected_pre_close, rel=1e-5)
    assert change_values.tolist() == pytest.approx(
        [
            (expected_close[0] - expected_pre_close[0]) / expected_pre_close[0],
            (expected_close[1] - expected_pre_close[1]) / expected_pre_close[1],
        ]
    )
    assert raw_values.tolist() == pytest.approx([28.59, 27.68])


def test_qlib_converter_keeps_raw_limit_prices_and_retains_raw_limits(tmp_path):
    dates = pd.to_datetime(["2026-01-02", "2026-01-05"])
    index = pd.MultiIndex.from_product(
        [dates, ["000001sz"]],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame(
        {
            "$open": [9.5, 10.5],
            "$high": [10.0, 10.5],
            "$low": [9.0, 9.5],
            "$close": [9.8, 10.5],
            "$pre_close": [9.0, 9.8],
            "$factor": [2.0, 2.0],
            "$up_limit": [10.0, 10.5],
            "$down_limit": [8.5, 9.0],
            "$limit_buy_open": [0.0, 1.0],
            "$limit_sell_open": [0.0, 0.0],
            "$limit_buy": [0.0, 1.0],
            "$limit_sell": [0.0, 0.0],
            "$limit_source_official": [1.0, 1.0],
        },
        index=index,
    )
    qlib_dir = tmp_path / "qlib"
    convert_to_qlib_v3.configure_paths(output_dir=str(qlib_dir))
    result = convert_to_qlib_v3._full_convert(convert_to_qlib_v3._prepare_qlib_price_semantics(df))

    up_start, up_values = _read_bin(qlib_dir / "features" / "000001sz" / "up_limit.day.bin")
    raw_up_start, raw_up_values = _read_bin(qlib_dir / "features" / "000001sz" / "raw_up_limit.day.bin")
    down_start, down_values = _read_bin(qlib_dir / "features" / "000001sz" / "down_limit.day.bin")
    raw_down_start, raw_down_values = _read_bin(qlib_dir / "features" / "000001sz" / "raw_down_limit.day.bin")
    limit_open_start, limit_open_values = _read_bin(qlib_dir / "features" / "000001sz" / "limit_buy_open.day.bin")

    open_start, open_values = _read_bin(qlib_dir / "features" / "000001sz" / "open.day.bin")
    assert up_start == raw_up_start == down_start == raw_down_start == limit_open_start == 0
    assert open_start == 0
    assert open_values.tolist() == pytest.approx([19.0, 21.0])
    assert up_values.tolist() == pytest.approx([10.0, 10.5])
    assert down_values.tolist() == pytest.approx([8.5, 9.0])
    assert raw_up_values.tolist() == pytest.approx([10.0, 10.5])
    assert raw_down_values.tolist() == pytest.approx([8.5, 9.0])
    assert limit_open_values.tolist() == pytest.approx([0.0, 1.0])


def test_qlib_limit_fields_prefer_official_limit_prices():
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2026-01-02"), "000001sz"),
            (pd.Timestamp("2026-01-05"), "000001sz"),
            (pd.Timestamp("2026-01-06"), "000001sz"),
        ],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame(
        {
            "$open": [9.5, 10.5, 9.0],
            "$high": [10.0, 11.0, 10.0],
            "$low": [9.0, 9.0, 8.0],
            "$close": [9.8, 10.5, 9.0],
            "$change": [0.0, 0.0, -0.096],
            "$up_limit": [10.0, 10.5, np.nan],
            "$down_limit": [8.5, 9.0, np.nan],
            "$turnover_rate": [1.0, 0.01, 0.01],
        },
        index=index,
    )

    convert_to_qlib_v3._ensure_limit_fields(df)

    assert df["$limit_buy"].tolist() == [0.0, 1.0, 0.0]
    assert df["$limit_sell"].tolist() == [0.0, 0.0, 0.0]
    assert df["$limit_buy_open"].tolist() == [0.0, 1.0, 0.0]
    assert df["$limit_sell_open"].tolist() == [0.0, 0.0, 0.0]
    assert df["$limit_buy_mid_oc"].tolist() == [0.0, 1.0, 0.0]
    assert df["$limit_sell_mid_oc"].tolist() == [0.0, 0.0, 0.0]
    assert df["$limit_buy_fallback"].tolist() == [0.0, 0.0, 0.0]
    assert df["$limit_sell_fallback"].tolist() == [0.0, 0.0, 1.0]
    assert df["$hit_up_limit_intraday"].tolist() == [1.0, 1.0, 0.0]
    assert df["$hit_down_limit_intraday"].tolist() == [0.0, 1.0, 0.0]
    assert df["$limit_source_official"].tolist() == [1.0, 1.0, 0.0]
    assert df["$one_price_up_limit"].tolist() == [0.0, 0.0, 0.0]
    assert df["$limit_low_liquidity"].tolist() == [0.0, 1.0, 1.0]
    assert df["$limit_buy_open_sealed"].tolist() == [0.0, 0.0, 0.0]
    assert df["$limit_sell_open_sealed"].tolist() == [0.0, 0.0, 0.0]
