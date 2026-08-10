import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from domain.data_foundation import convert_to_quantgpt, ops_common, quality_check, tushare_daily


BENCHMARKS = ["000300.SH", "000905.SH", "000852.SH"]


def _quantgpt_seed_row(code: str, day: str, close: float) -> dict:
    row = {column: 1.0 for column in convert_to_quantgpt.REQUIRED_COLUMNS}
    row.update(
        {
            "trade_date": pd.Timestamp(day),
            "stock_code": code,
            "security_name": "sample",
            "list_status": "I" if code in BENCHMARKS else "L",
            "st_status": "NORMAL",
            "list_date": "2000-01-01",
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "pre_close": close,
            "vwap": close,
            "volume": 100.0,
            "amount": close * 10.0,
            "cost_15pct": close * 0.9,
            "cost_85pct": close * 1.1,
            "weight_avg": close,
            "backward_factor": 1.0,
        }
    )
    return row


def _base_row(code: str = "600000.SH", day: str = "2026-04-07") -> dict:
    row = {}
    for group in quality_check.FIELD_GROUPS.values():
        for field in group["fields"]:
            row[field] = 1.0
    is_index = code in BENCHMARKS
    row.update(
        {
            "code": code,
            "kline_time": day,
            "SECURITY_NAME": "sample",
            "MARKET_CODE": code,
            "LIST_DATE": "2000-01-01",
            "list_status": "I" if is_index else "L",
            "st_status": "NORMAL",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "up_limit": None if is_index else 11.0,
            "down_limit": None if is_index else 9.0,
            "limit_source_kind": "index" if is_index else "official",
            "pre_close": 9.5,
            "volume": 100.0,
            "amount": 1000.0,
            "backward_factor": 1.0,
            "adj_open": 10.0,
            "adj_high": 11.0,
            "adj_low": 9.0,
            "adj_close": 10.0,
            "adj_pre_close": 9.5,
            "adj_pct_chg": (10.0 - 9.5) / 9.5 * 100,
            "adj_amp": (11.0 - 9.0) / 9.5 * 100,
            "pct_chg": (10.0 - 9.5) / 9.5 * 100,
            "amp": (11.0 - 9.0) / 9.5 * 100,
        }
    )
    return row


def _write_daily(h5_path: Path, rows: list[dict]) -> None:
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["kline_time"])
    df = df.set_index("trade_date")
    df.to_hdf(h5_path, key="/daily", mode="w")


def test_quality_check_fails_when_expected_field_missing_inside_present_group(tmp_path):
    h5_path = tmp_path / "stock_daily.h5"
    row = _base_row()
    row.pop("ROE")
    rows = [row, *[_base_row(code=code) for code in BENCHMARKS]]
    for item in rows:
        item.pop("ROE", None)
    _write_daily(h5_path, rows)

    result = quality_check.check(h5_path)

    assert result["passed"] is False
    group = result["field_groups"]["valuation_and_fundamental_fields"]
    assert "ROE" in group["missing_fields"]
    assert any("valuation_and_fundamental_fields missing fields: ROE" in item for item in result["issues"])


def test_quality_check_fails_when_adjusted_fields_break_factor_consistency(tmp_path):
    h5_path = tmp_path / "stock_daily.h5"
    row = _base_row()
    row["adj_close"] = 99.0
    _write_daily(h5_path, [row, *[_base_row(code=code) for code in BENCHMARKS]])

    result = quality_check.check(h5_path)

    assert result["passed"] is False
    assert result["factor_adjusted_quality"]["present"] is True
    assert result["factor_adjusted_quality"]["issues"]


def test_quality_check_demotes_tushare_v1_legacy_adjusted_and_structural_pe(tmp_path):
    h5_path = tmp_path / "stock_daily.h5"
    row = _base_row()
    row["PE"] = None
    row["adj_close"] = 99.0
    rows = [row, *[_base_row(code=code) for code in BENCHMARKS]]
    _write_daily(h5_path, rows)
    (tmp_path / "metadata.json").write_text(
        """
{
  "schema_version": "tushare_v1",
  "price_mode": "raw_with_legacy_adjusted_compat_columns",
  "effective_target_date": 20260605
}
""".strip(),
        encoding="utf-8",
    )

    result = quality_check.check(h5_path)

    assert result["passed"] is True
    assert any(warning.startswith("pe_ttm_structural_missing:") for warning in result["warnings"])
    assert any(warning.startswith("tushare_legacy_adjusted_compat:adj_close") for warning in result["warnings"])


def test_quality_check_accepts_tushare_v1_legacy_adjusted_formula(tmp_path):
    h5_path = tmp_path / "stock_daily.h5"
    row = _base_row()
    row["open"] = 13.35
    row["high"] = 13.93
    row["low"] = 13.32
    row["close"] = 13.70
    row["pre_close"] = 13.33
    row["backward_factor"] = 106.309
    row["adj_open"] = round(row["open"] * row["backward_factor"], 2)
    row["adj_high"] = round(row["high"] * row["backward_factor"], 2)
    row["adj_low"] = round(row["low"] * row["backward_factor"], 2)
    row["adj_close"] = round(row["close"] * row["backward_factor"], 2)
    row["adj_pre_close"] = round(row["pre_close"] * row["backward_factor"], 2)
    _write_daily(h5_path, [row, *[_base_row(code=code) for code in BENCHMARKS]])
    (tmp_path / "metadata.json").write_text(
        """
{
  "schema_version": "tushare_v1",
  "price_mode": "raw_with_legacy_adjusted_compat_columns",
  "adjusted_price_mode": "legacy_raw_times_backward_factor"
}
""".strip(),
        encoding="utf-8",
    )

    result = quality_check.check(h5_path)

    assert result["passed"] is True
    assert result["factor_adjusted_quality"]["mode"] == "legacy_raw_times_backward_factor"
    assert not result["factor_adjusted_quality"]["issues"]


def test_quality_check_ignores_deleted_limit_fields(tmp_path):
    h5_path = tmp_path / "stock_daily.h5"
    rows = [_base_row(code="600000.SH"), *[_base_row(code=code) for code in BENCHMARKS]]
    _write_daily(h5_path, rows)
    (tmp_path / "metadata.json").write_text(
        """
{
  "schema_version": "tushare_v1",
  "price_mode": "raw_with_legacy_adjusted_compat_columns"
}
""".strip(),
        encoding="utf-8",
    )

    result = quality_check.check(h5_path)

    assert result["passed"] is True
    assert "market_limit_fields" not in result["field_groups"]
    assert "high_limited" not in result["field_stats"]
    assert "low_limited" not in result["field_stats"]


def test_quality_check_includes_schema_summary(tmp_path):
    h5_path = tmp_path / "stock_daily.h5"
    _write_daily(h5_path, [_base_row(code="600000.SH"), *[_base_row(code=code) for code in BENCHMARKS]])
    (tmp_path / "metadata.json").write_text(
        """
{
  "schema_version": "v2",
  "price_mode": "raw_plus_factor",
  "cache_mode": "remote_only",
  "effective_target_date": 20260529,
  "historical_limit_source_untrusted": true
}
""".strip(),
        encoding="utf-8",
    )

    result = quality_check.check(h5_path)

    assert result["passed"] is True
    assert result["schema_summary"]["schema_version"] == "v2"
    assert result["schema_summary"]["price_mode"] == "raw_plus_factor"
    assert result["schema_summary"]["historical_limit_source_untrusted"] is True


def test_quality_check_demotes_historical_window_metadata_mismatch_to_warning(tmp_path):
    h5_path = tmp_path / "stock_daily.h5"
    _write_daily(h5_path, [_base_row(code="600000.SH"), *[_base_row(code=code) for code in BENCHMARKS]])
    (tmp_path / "metadata.json").write_text(
        """
{
  "last_update_quality": {
    "historical_window": true,
    "expected_stock_count": 2,
    "processed_code_count": 1,
    "latest_day_stock_count": 1,
    "latest_day_missing_codes": ["000001.SZ"]
  }
}
""".strip(),
        encoding="utf-8",
    )

    result = quality_check.check(h5_path)

    assert result["passed"] is True
    assert any("processed_code_count 1 != expected_stock_count 2" in item for item in result["warnings"])
    assert any("historical-window raw build snapshots" in item for item in result["warnings"])


def test_quality_check_fails_when_required_benchmark_missing(tmp_path):
    h5_path = tmp_path / "stock_daily.h5"
    _write_daily(h5_path, [_base_row(code="600000.SH"), _base_row(code="000300.SH"), _base_row(code="000905.SH")])

    result = quality_check.check(h5_path)

    assert result["passed"] is False
    assert any("benchmark index missing: 000852.SH" in item for item in result["issues"])


def test_daily_compat_quality_checks_refresh_window(tmp_path):
    h5_path = tmp_path / "stock_daily.h5"
    rows = []
    for day in ["2026-06-01", "2026-06-02"]:
        rows.append(_base_row(code="600000.SH", day=day))
        rows.extend(_base_row(code=code, day=day) for code in BENCHMARKS)
    _write_daily(h5_path, rows)

    result = quality_check.check(h5_path, profile="daily_compat", replace_from_date="20260602")

    assert result["passed"] is True
    assert result["profile"] == "daily_compat"
    assert result["latest_trade_date"] == "2026-06-02"
    assert result["scanned_rows"] == 4
    assert result["duplicate_keys"] == 0


def test_daily_compat_quality_blocks_duplicate_and_price_range_errors(tmp_path):
    h5_path = tmp_path / "stock_daily.h5"
    bad = _base_row(code="600000.SH", day="2026-06-02")
    bad["close"] = 12.0
    bad["high"] = 11.0
    rows = [bad, dict(bad), *[_base_row(code=code, day="2026-06-02") for code in BENCHMARKS]]
    _write_daily(h5_path, rows)

    result = quality_check.check(h5_path, profile="daily_compat", replace_from_date="20260602")

    assert result["passed"] is False
    assert any("duplicate code/kline_time" in item for item in result["issues"])
    assert any("close_outside_range" in item for item in result["issues"])


def test_quantgpt_incremental_creates_missing_new_stock_parquet(tmp_path):
    h5_path = tmp_path / "stock_daily.h5"
    out_dir = tmp_path / "stocks"
    benchmark_dir = tmp_path / "benchmark"
    out_dir.mkdir()
    pd.DataFrame(
        [_quantgpt_seed_row("sh.600000", "2026-01-01", 9.5)]
    ).to_parquet(out_dir / "sh_600000.parquet", index=False)
    convert_to_quantgpt._write_contract(out_dir)

    df = pd.DataFrame(
        [
            {"trade_date": "2026-01-02", "code": "600000.SH", "kline_time": "2026-01-02", "adj_close": 10.0, "close": 10.0, "adj_open": 10.0, "adj_high": 11.0, "adj_low": 9.0, "adj_pre_close": 9.5, "adj_pct_chg": 1.0, "adj_amp": 2.0, "volume": 100.0, "amount": 100.0, "backward_factor": 1.0, "cost_15pct": 9.0, "cost_85pct": 11.0, "weight_avg": 10.0},
            {"trade_date": "2026-01-02", "code": "000002.SZ", "kline_time": "2026-01-02", "adj_close": 20.0, "close": 20.0, "adj_open": 20.0, "adj_high": 21.0, "adj_low": 19.0, "adj_pre_close": 19.0, "adj_pct_chg": 1.0, "adj_amp": 2.0, "volume": 100.0, "amount": 200.0, "backward_factor": 1.0, "cost_15pct": 18.0, "cost_85pct": 22.0, "weight_avg": 20.0},
        ]
    )
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date")
    df.to_hdf(h5_path, key="/daily", mode="w")

    result = convert_to_quantgpt.convert_incremental(h5_path, out_dir, benchmark_dir)

    assert result["status"] == "completed"
    assert result["mode"] == "incremental"
    assert result["stale_codes_updated"] == 1
    assert result["new_codes_created"] == 1
    assert result["codes_updated"] == 2
    assert result["price_mode"] == "adjusted_from_adj_fields_with_adjusted_vwap_and_chip_cost"
    assert (out_dir / "sz_000002.parquet").exists()
    exported = pd.read_parquet(out_dir / "sz_000002.parquet")
    assert "high_limited" not in exported.columns
    assert "low_limited" not in exported.columns


def test_quantgpt_conversion_materializes_adjusted_vwap_and_chip_costs():
    frame = pd.DataFrame(
        {
            "kline_time": ["2026-01-02"],
            "adj_open": [20.0],
            "adj_high": [22.0],
            "adj_low": [18.0],
            "adj_close": [21.0],
            "adj_pre_close": [19.0],
            "adj_pct_chg": [10.5263],
            "adj_amp": [21.0526],
            "volume": [100.0],
            "amount": [1000.0],
            "cost_15pct": [9.0],
            "cost_85pct": [11.0],
            "weight_avg": [10.0],
            "backward_factor": [2.0],
        }
    )

    out = convert_to_quantgpt._to_quantgpt_frame(frame, "600000.SH")
    row = out.iloc[0]

    assert row["open"] == 20.0
    assert row["vwap"] == 200.0
    assert row["cost_15pct"] == 18.0
    assert row["cost_85pct"] == 22.0
    assert row["weight_avg"] == 20.0


def test_quantgpt_incremental_from_delta_rewrites_only_replace_window(tmp_path):
    delta_h5 = tmp_path / "delta.h5"
    seed_stocks = tmp_path / "seed_stocks"
    seed_benchmark = tmp_path / "seed_benchmark"
    out_dir = tmp_path / "out_stocks"
    benchmark_dir = tmp_path / "out_benchmark"
    seed_stocks.mkdir()
    seed_benchmark.mkdir()

    pd.DataFrame(
        [
            _quantgpt_seed_row("sh.600000", "2026-01-01", 9.5),
            _quantgpt_seed_row("sh.600000", "2026-01-02", 9.8),
        ]
    ).to_parquet(seed_stocks / "sh_600000.parquet", index=False)
    convert_to_quantgpt._write_contract(seed_stocks)
    pd.DataFrame(
        [
            _quantgpt_seed_row("sh.000300", "2026-01-01", 100.0),
            _quantgpt_seed_row("sh.000300", "2026-01-02", 101.0),
        ]
    ).to_parquet(seed_benchmark / "benchmark_hs300.parquet", index=False)

    delta = pd.DataFrame(
        [
            {"trade_date": "2026-01-02", "code": "600000.SH", "kline_time": "2026-01-02", "adj_close": 10.0, "close": 10.0, "adj_open": 10.0, "adj_high": 11.0, "adj_low": 9.0, "adj_pre_close": 9.5, "adj_pct_chg": 1.0, "adj_amp": 2.0, "volume": 100.0, "amount": 100.0, "backward_factor": 1.0, "cost_15pct": 9.0, "cost_85pct": 11.0, "weight_avg": 10.0, "list_status": "L", "st_status": "NORMAL"},
            {"trade_date": "2026-01-03", "code": "600000.SH", "kline_time": "2026-01-03", "adj_close": 10.5, "close": 10.5, "adj_open": 10.2, "adj_high": 11.2, "adj_low": 9.8, "adj_pre_close": 10.0, "adj_pct_chg": 5.0, "adj_amp": 14.0, "volume": 100.0, "amount": 105.0, "backward_factor": 1.0, "cost_15pct": 9.5, "cost_85pct": 11.5, "weight_avg": 10.5, "list_status": "L", "st_status": "NORMAL"},
            {"trade_date": "2026-01-02", "code": "000300.SH", "kline_time": "2026-01-02", "adj_close": 102.0, "close": 102.0, "adj_open": 101.0, "adj_high": 103.0, "adj_low": 100.0, "adj_pre_close": 100.0, "adj_pct_chg": 2.0, "adj_amp": 3.0, "volume": 100.0, "amount": 1020.0, "backward_factor": 1.0, "list_status": "I", "st_status": "NORMAL"},
            {"trade_date": "2026-01-03", "code": "000300.SH", "kline_time": "2026-01-03", "adj_close": 103.0, "close": 103.0, "adj_open": 102.0, "adj_high": 104.0, "adj_low": 101.0, "adj_pre_close": 102.0, "adj_pct_chg": 1.0, "adj_amp": 2.9, "volume": 100.0, "amount": 1030.0, "backward_factor": 1.0, "list_status": "I", "st_status": "NORMAL"},
        ]
    )
    delta["trade_date"] = pd.to_datetime(delta["trade_date"])
    delta = delta.set_index("trade_date")
    delta.to_hdf(delta_h5, key="/daily", mode="w")

    result = convert_to_quantgpt.convert_incremental_from_delta(
        delta_h5,
        out_dir,
        benchmark_dir,
        replace_from_date="20260102",
        seed_output_dir=seed_stocks,
        seed_benchmark_dir=seed_benchmark,
    )

    assert result["status"] == "completed"
    assert result["mode"] == "incremental_from_delta"
    stock = pd.read_parquet(out_dir / "sh_600000.parquet")
    benchmark = pd.read_parquet(benchmark_dir / "benchmark_hs300.parquet")
    assert stock["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert stock["close"].tolist() == [9.5, 10.0, 10.5]
    assert "vwap" in stock.columns
    assert "cost_15pct" in stock.columns
    assert benchmark["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert benchmark["close"].tolist() == [100.0, 102.0, 103.0]


def test_quantgpt_incremental_clears_replace_window_for_code_absent_from_delta(tmp_path):
    delta_h5 = tmp_path / "delta.h5"
    seed_stocks = tmp_path / "seed_stocks"
    seed_benchmark = tmp_path / "seed_benchmark"
    out_dir = tmp_path / "out_stocks"
    benchmark_dir = tmp_path / "out_benchmark"
    seed_stocks.mkdir()
    seed_benchmark.mkdir()
    for quantgpt_code in ["sh.600000", "sz.000002"]:
        pd.DataFrame(
            [
                _quantgpt_seed_row(quantgpt_code, "2026-01-01", 9.5),
                _quantgpt_seed_row(quantgpt_code, "2026-01-02", 9.8),
            ]
        ).to_parquet(seed_stocks / f"{quantgpt_code.replace('.', '_')}.parquet", index=False)
    convert_to_quantgpt._write_contract(seed_stocks)
    delta = pd.DataFrame(
        [
            {
                "trade_date": "2026-01-02",
                "code": "600000.SH",
                "kline_time": "2026-01-02",
                "adj_close": 10.0,
                "close": 10.0,
                "adj_open": 10.0,
                "adj_high": 11.0,
                "adj_low": 9.0,
                "adj_pre_close": 9.5,
                "adj_pct_chg": 1.0,
                "adj_amp": 2.0,
                "volume": 100.0,
                "amount": 100.0,
                "backward_factor": 1.0,
                "cost_15pct": 9.0,
                "cost_85pct": 11.0,
                "weight_avg": 10.0,
                "list_status": "L",
                "st_status": "NORMAL",
            }
        ]
    )
    delta["trade_date"] = pd.to_datetime(delta["trade_date"])
    delta.set_index("trade_date").to_hdf(delta_h5, key="/daily", mode="w")

    result = convert_to_quantgpt.convert_incremental_from_delta(
        delta_h5,
        out_dir,
        benchmark_dir,
        replace_from_date="20260102",
        seed_output_dir=seed_stocks,
        seed_benchmark_dir=seed_benchmark,
    )

    absent_code = pd.read_parquet(out_dir / "sz_000002.parquet")
    assert result["status"] == "completed"
    assert result["stock_window_truncate"]["files_rewritten"] == 2
    assert absent_code["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-01"]


def test_quantgpt_incremental_reports_failed_code_instead_of_false_success(tmp_path, monkeypatch):
    delta_h5 = tmp_path / "delta.h5"
    seed_stocks = tmp_path / "seed_stocks"
    seed_benchmark = tmp_path / "seed_benchmark"
    seed_stocks.mkdir()
    seed_benchmark.mkdir()
    pd.DataFrame([_quantgpt_seed_row("sh.600000", "2026-01-01", 9.5)]).to_parquet(
        seed_stocks / "sh_600000.parquet", index=False
    )
    convert_to_quantgpt._write_contract(seed_stocks)
    delta = pd.DataFrame(
        [{"trade_date": pd.Timestamp("2026-01-02"), "code": "600000.SH", "kline_time": "2026-01-02"}]
    ).set_index("trade_date")
    delta.to_hdf(delta_h5, key="/daily", mode="w")
    monkeypatch.setattr(
        convert_to_quantgpt,
        "_to_quantgpt_frame",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("write_failed")),
    )

    result = convert_to_quantgpt.convert_incremental_from_delta(
        delta_h5,
        tmp_path / "out_stocks",
        tmp_path / "out_benchmark",
        replace_from_date="20260102",
        seed_output_dir=seed_stocks,
        seed_benchmark_dir=seed_benchmark,
    )

    assert result["status"] == "failed"
    assert result["errors"] == 1
    assert result["failed_codes"][0]["code"] == "600000.SH"


def test_qlib_patch_clears_missing_and_explicit_nan_values(tmp_path):
    path = tmp_path / "close.day.bin"
    tushare_daily._write_qlib_bin_file(path, np.array([1.0, 2.0, 3.0]), 0)
    calendar = {"2026-01-01": 0, "2026-01-02": 1, "2026-01-03": 2}

    changed = tushare_daily._patch_qlib_bin_file(
        path,
        calendar,
        pd.Series([float("nan")], index=[pd.Timestamp("2026-01-02")]),
        clear_from_iso="2026-01-02",
        clear_to_iso="2026-01-03",
    )

    payload = np.fromfile(path, dtype="<f4")
    assert changed is True
    assert payload[0] == 0
    assert payload[1] == 1.0
    assert np.isnan(payload[2:]).all()


def test_file_equivalence_hashes_contents_even_when_size_and_mtime_match(tmp_path):
    left = tmp_path / "left.bin"
    right = tmp_path / "right.bin"
    left.write_bytes(b"a" * 1024)
    right.write_bytes(b"b" * 1024)
    timestamp = 1_700_000_000
    os.utime(left, (timestamp, timestamp))
    os.utime(right, (timestamp, timestamp))

    assert ops_common._file_equivalent(left, right) is False


def test_data_job_lock_rejects_second_owner(tmp_path):
    lock_dir = tmp_path / "data_job.lock"
    ops_common._acquire_lock(lock_dir, owner={"mode": "first"})
    try:
        with pytest.raises(RuntimeError, match="lock exists"):
            ops_common._acquire_lock(lock_dir, owner={"mode": "second"})
    finally:
        ops_common._release_lock(lock_dir)


def test_gui_data_foundation_defaults_to_staged_daily_routine_and_schema_summary():
    app_js = Path(__file__).resolve().parents[1] / "gui" / "app.js"
    index_html = Path(__file__).resolve().parents[1] / "gui" / "index.html"
    styles_css = Path(__file__).resolve().parents[1] / "gui" / "styles.css"
    source = app_js.read_text(encoding="utf-8")
    html = index_html.read_text(encoding="utf-8")
    css = styles_css.read_text(encoding="utf-8")

    assert 'postJson("/data/refresh"' not in source
    assert 'postJson("/data/update/start"' in source
    assert "DATA_LIVE_REFRESH_INTERVAL_MS = 30 * 1000" in source
    assert "dataLiveIsRunning() ? 3000 : 15000" not in source
    assert "data-live-cockpit" in source
    assert "计数未知" in source
    assert "renderDataCompositeChart" in source
    assert "DATA_RIGHT_AXIS_FIELDS" in source
    assert "right-axis" in source
    assert 'label: "状态字段"' not in source
    assert "list_status / st_status 已进入生产库" not in source
    assert 'label: "覆盖率"' in source
    assert 'label: "质量门"' in source
    assert "setupDatePickerButtons" in source
    assert "data-side-legend" in source
    assert "data-candle-hit" in source
    assert "data-candle-body" in source
    assert 'id="data-foundation-live-panel" hidden' in html
    assert 'id="data-foundation-query-panel" hidden' in html
    assert 'value="000001"' in html
    assert 'class="date-input-shell"' in html
    assert '<option value="zscore">Z-score</option>' in html
    assert 'id="data-query-submit"' in html
    assert 'class="data-query-fields-shell"' in html
    assert 'src="/gui/app.js?v=' in html
    assert ".data-foundation-nav" in css
    assert ".data-foundation-nav {\n  display: flex" in css
    assert "position: sticky" in css
    assert "top: 12px" in css
    assert "width: max-content" in css
    assert "margin: 0 0 18px" in css
    assert "flex: 0 0 auto" in css
    assert "justify-content: flex-start" in css
    assert "text-align: center" in css
    assert "floating-x-scrollbar" in css
    assert "FLOATING_X_SCROLL_SELECTOR" in source
    assert "queueFloatingXScrollbarRefresh" in source
    assert "dataProgressTone" in source
    assert "生产接续流程" in source
    assert "data-live-raw-preview" in css
    assert "最近事件与原始状态" in source
    assert "data-query-summary-strip" in source
    assert "DATA_QUERY_DEFAULT_WINDOW_DAYS = 180" in source
    assert "DATA_QUERY_MAX_CHART_ROWS = 160" in source
    assert "renderDataQueryFieldBrief" not in source
    assert ".data-query-actions-bar" in css
    assert ".data-candle.up .data-candle-body" in css
    assert ".data-candle.down .data-candle-body" in css
    assert ".data-candle.up rect" not in css
    assert ".data-candle.down rect" not in css
    assert ".data-query-fields-shell" in css
    assert ".data-query-fields-title" in css
    assert ".date-picker-glyph" in css
    assert ".data-query-selection-board" in css
    assert ".data-query-select-pane" in css
    assert ".data-query-transfer-rail" in css
    assert ".data-query-transfer-button" in css
    assert ".data-query-transfer-row" in css
    assert ".data-query-field-group-toggle" in css
    assert "data-query-group" in source
    assert "dataQueryExpandedGroups" in source
    assert ".data-quality-hero > div" in css
    assert ".data-quality-hero .status-chip" in css
    assert "width: auto" in css
    assert ".data-query-summary-strip" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert "align-items: start" in css
    assert "align-self: start" in css
    assert "height: 390px" in css
    assert "data-query-identity" in source
    assert "data-query-status" in source
    assert "fieldGroupMap" in source
    assert "data-query-transfer-candidate" in source
    assert "dataLiveCurrentCard" in source
    assert "data-live-current-card" in source
    assert "data-progress-grid" in source
    assert "无 total，仅显示状态" in source
    assert 'dataLiveGauge("生产最新日"' not in source
    assert ".data-live-current-card" in css
    assert "grid-template-columns: repeat(2, minmax(220px, 1fr))" in css
    assert ".data-progress-grid" in css
    assert ".data-progress-card" in css
    assert "data-quality-hero" in source
    assert "data-quality-fact-grid" in source
    assert "data-quality-meter-grid" in source
    assert "data-quality-alert-list" in source
    assert ".data-quality-grid" in css
    assert ".data-quality-meter" in css
    assert "日更动作状态" not in source
    assert "最近动作原始输出" not in source
    assert 'id="data-live-control-form"' in html
    assert 'id="data-live-preflight"' in html
    assert 'id="start-data-daily-live"' in html
    assert 'id="start-data-full-rebuild-live"' in html
    assert "异步干跑日更" not in html
    assert "全量重建干跑" not in html
    assert 'id="data-form"' not in html
    assert 'id="data-stage-dry-run"' not in html
    assert 'id="check-amazingdata"' not in html
    assert "dataLivePreflightResult" in source
    assert "renderDataLivePreflight" in source
    assert "data-live-control-panel" in css
    assert ".data-live-refresh-zone .refresh-action" in css
    assert "flex: 1 1 510px" in css
    assert "color-scheme: dark" in css
    assert ".date-input-shell:focus-within" in css
    assert "font-variant-numeric: tabular-nums" in css
    assert "data-preflight-card" in css
    assert "grid-template-columns: repeat(auto-fit, minmax(158px, 1fr))" in css
    assert "padding: 8px 10px" in css
    assert 'postJson("/data/daily-preflight"' in source
    assert 'postJson("/data/stage-update"' not in source
    assert 'factor_adjusted_quality' in source
    assert 'schema_summary' in source


def test_tushare_daily_source_does_not_import_legacy_amazingdata_controller():
    root = Path(__file__).resolve().parents[1]
    for relative in [
        "domain/data_foundation/tushare_daily.py",
        "domain/data_foundation/tushare_production.py",
    ]:
        source = (root / relative).read_text(encoding="utf-8")
        assert "legacy.amazingdata_daily_controller" not in source


def test_current_docs_do_not_advertise_amazingdata_production_paths():
    root = Path(__file__).resolve().parents[1]
    current_docs = [
        "docs/DATA_FOUNDATION_DATASET_REGISTRY_CURRENT.md",
        "docs/PROJECT_STRUCTURE_CURRENT.md",
        "docs/DATA_TRADING_OPERATION_CONTRACT_CURRENT.md",
        "docs/DATA_FOUNDATION_TUSHARE_REBUILD_RUNBOOK_CURRENT.md",
        "docs/DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md",
    ]
    forbidden = [
        "production: `<repo-root>/data/raw/amazingdata/",
        "current production dataset pointer still targets the AmazingData",
        "production data assets still write to `data/raw/amazingdata/`",
    ]
    for relative in current_docs:
        text = (root / relative).read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern not in text
