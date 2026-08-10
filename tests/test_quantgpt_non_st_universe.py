import sys
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
QUANTGPT_ROOT = REPO_ROOT / "third_party" / "quantgpt"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(QUANTGPT_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANTGPT_ROOT))


def _sample_market_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=22)
    return pd.DataFrame(
        {
            "trade_date": list(dates) + list(dates),
            "stock_code": ["sh.600000"] * len(dates) + ["sz.000001"] * len(dates),
            "security_name": ["浦发银行"] * len(dates) + ["*ST测试"] + ["平安银行"] * (len(dates) - 1),
            "list_status": ["L"] * (len(dates) * 2),
            "st_status": ["NORMAL"] * len(dates) + ["ST"] + ["NORMAL"] * (len(dates) - 1),
            "close": [10.0 + idx / 10 for idx in range(len(dates))]
            + [5.0 + idx / 10 for idx in range(len(dates))],
        }
    )


def test_filter_non_st_market_data_removes_only_pit_st_rows():
    from quantgpt.market_data import filter_non_st_market_data

    filtered = filter_non_st_market_data(_sample_market_frame())

    assert len(filtered) == 43
    assert not ((filtered["trade_date"] == pd.Timestamp("2026-01-02")) & (filtered["stock_code"] == "sz.000001")).any()
    assert ((filtered["trade_date"] == pd.Timestamp("2026-01-05")) & (filtered["stock_code"] == "sz.000001")).any()


def test_filter_non_st_market_data_trusts_pit_status_over_current_name():
    from quantgpt.market_data import filter_non_st_market_data

    frame = pd.DataFrame(
        {
            "trade_date": [pd.Timestamp("2025-07-23"), pd.Timestamp("2025-07-24")],
            "stock_code": ["sz.000752", "sz.000752"],
            "security_name": ["*ST西发", "*ST西发"],
            "list_status": ["L", "L"],
            "st_status": ["NORMAL", "ST"],
            "close": [10.0, 10.1],
        }
    )

    filtered = filter_non_st_market_data(frame)

    assert len(filtered) == 1
    assert filtered.iloc[0]["trade_date"] == pd.Timestamp("2025-07-23")


def test_fixed_non_st_universe_uses_status_not_security_name(tmp_path):
    from quantgpt.market_data import fixed_non_st_stock_codes

    stock_dir = tmp_path / "stocks"
    stock_dir.mkdir()
    pd.DataFrame(
        {
            "trade_date": [pd.Timestamp("2026-06-01")],
            "stock_code": ["sz.000001"],
            "security_name": ["*ST名称滞后"],
            "list_status": ["L"],
            "st_status": ["NORMAL"],
        }
    ).to_parquet(stock_dir / "sz_000001.parquet", index=False)
    pd.DataFrame(
        {
            "trade_date": [pd.Timestamp("2026-06-01")],
            "stock_code": ["sz.000002"],
            "security_name": ["普通名称"],
            "list_status": ["L"],
            "st_status": ["ST"],
        }
    ).to_parquet(stock_dir / "sz_000002.parquet", index=False)

    codes = fixed_non_st_stock_codes("2026-06-01", str(stock_dir))

    assert "sz.000001" in codes
    assert "sz.000002" not in codes


def test_mcp_fetch_data_for_market_keeps_st_rows_for_canonical_evaluator(monkeypatch):
    from quantgpt import mcp_server

    class FakeFetcher:
        def fetch_stocks(self, stock_codes, start_date, end_date):
            return _sample_market_frame()

    monkeypatch.setattr(mcp_server, "get_universe", lambda universe, date=None: ["sh.600000", "sz.000001"])
    monkeypatch.setattr(mcp_server, "MarketDataFetcher", lambda: FakeFetcher())
    mcp_server._MARKET_DATA_CACHE.clear()

    market_df, stock_codes = mcp_server._fetch_data_for_market(
        "tradable_non_st",
        "2026-01-02",
        "2026-01-05",
    )

    assert len(market_df) == 44
    assert stock_codes == ["sh.600000", "sz.000001"]
    assert ((market_df["trade_date"] == pd.Timestamp("2026-01-02")) & (market_df["stock_code"] == "sz.000001")).any()


def test_factor_value_loader_filters_st_by_default(monkeypatch, tmp_path):
    from domain.factor_research import factor_compute
    import quantgpt.market_data as market_data

    qlib_root = tmp_path / "qlib"
    instruments_dir = qlib_root / "instruments"
    instruments_dir.mkdir(parents=True)
    (instruments_dir / "all.txt").write_text(
        "600000sh\t2020-01-01\t2026-12-31\n000001sz\t2020-01-01\t2026-12-31\n",
        encoding="utf-8",
    )

    class FakeFetcher:
        def _load_cache(self, stock_code):
            return _sample_market_frame()[_sample_market_frame()["stock_code"] == stock_code].copy()

    monkeypatch.setattr(factor_compute, "QLIB_DATA_ROOT", qlib_root)
    monkeypatch.setattr(market_data, "MarketDataFetcher", lambda: FakeFetcher())

    loaded = factor_compute._load_market_data(
        start_date="2026-01-02",
        end_date="2026-01-09",
        required_columns={"trade_date", "stock_code", "close"},
        filter_non_st=True,
    )

    assert len(loaded) == 11
    assert not ((loaded["trade_date"] == pd.Timestamp("2026-01-02")) & (loaded["stock_code"] == "sz.000001")).any()


def test_factor_value_loader_can_keep_st_rows_for_diagnostics(monkeypatch, tmp_path):
    from domain.factor_research import factor_compute
    import quantgpt.market_data as market_data

    qlib_root = tmp_path / "qlib"
    instruments_dir = qlib_root / "instruments"
    instruments_dir.mkdir(parents=True)
    (instruments_dir / "all.txt").write_text(
        "600000sh\t2020-01-01\t2026-12-31\n000001sz\t2020-01-01\t2026-12-31\n",
        encoding="utf-8",
    )

    class FakeFetcher:
        def _load_cache(self, stock_code):
            return _sample_market_frame()[_sample_market_frame()["stock_code"] == stock_code].copy()

    monkeypatch.setattr(factor_compute, "QLIB_DATA_ROOT", qlib_root)
    monkeypatch.setattr(market_data, "MarketDataFetcher", lambda: FakeFetcher())

    loaded = factor_compute._load_market_data(
        start_date="2026-01-02",
        end_date="2026-01-09",
        required_columns={"trade_date", "stock_code", "close"},
        filter_non_st=False,
    )

    assert len(loaded) == 12
    assert ((loaded["trade_date"] == pd.Timestamp("2026-01-02")) & (loaded["stock_code"] == "sz.000001")).any()


def test_compute_factor_keeps_non_st_rows_for_expression(monkeypatch):
    from domain.factor_research import factor_compute
    from quantgpt.market_data import filter_non_st_market_data

    calls = {}

    def fake_load_market_data(**kwargs):
        calls["loader_filter_non_st"] = kwargs.get("filter_non_st")
        frame = _sample_market_frame()
        if kwargs.get("filter_non_st"):
            frame = filter_non_st_market_data(frame)
        return frame

    def fake_compute_factor_from_market_df(market_df, expression):
        calls["compute_rows"] = len(market_df)
        out = market_df[["trade_date", "stock_code"]].copy()
        out["datetime"] = pd.to_datetime(out["trade_date"])
        out["instrument"] = out["stock_code"].map(factor_compute._bs_to_qlib)
        out[expression] = range(len(out))
        return out.set_index(["datetime", "instrument"])[[expression]].sort_index()

    monkeypatch.setattr(factor_compute, "_load_market_data", fake_load_market_data)
    monkeypatch.setattr(factor_compute, "_compute_factor_from_market_df", fake_compute_factor_from_market_df)

    result = factor_compute.compute_factor(
        "close",
        start_date="2026-01-02",
        end_date="2026-01-30",
        filter_non_st=True,
    )

    assert calls["loader_filter_non_st"] is False
    assert calls["compute_rows"] == 44
    assert len(result) == 42


def test_canonical_evaluator_uses_full_ts_history_but_outputs_static_non_st(monkeypatch):
    import quantgpt.factor_evaluator as evaluator

    frame = _sample_market_frame()
    monkeypatch.setattr(
        evaluator,
        "fixed_non_st_mask",
        lambda df, baseline_date=None: df["stock_code"].astype(str).eq("sh.600000"),
    )
    values = evaluator.evaluate_factor_frame(
        frame,
        "ts_mean(close, 2)",
        universe="tradable_non_st",
        output_start_date="2026-01-02",
        output_end_date="2026-01-06",
    )

    assert not (
        (values["trade_date"] == pd.Timestamp("2026-01-05"))
        & (values["stock_code"] == "sz.000001")
    ).any()
    retained = values[
        (values["trade_date"] == pd.Timestamp("2026-01-05"))
        & (values["stock_code"] == "sh.600000")
    ]["factor_value"].iloc[0]
    assert abs(retained - 10.05) < 1e-12


def test_canonical_evaluator_is_order_stable_for_ts_and_cs(monkeypatch):
    import quantgpt.factor_evaluator as evaluator

    frame = _sample_market_frame()
    shuffled = frame.sample(frac=1.0, random_state=7).reset_index(drop=True)
    expr = "rank(ts_mean(close, 3))"
    monkeypatch.setattr(
        evaluator,
        "fixed_non_st_mask",
        lambda df, baseline_date=None: pd.Series(True, index=df.index),
    )

    base = evaluator.evaluate_factor_frame(frame, expr, universe="tradable_non_st")
    other = evaluator.evaluate_factor_frame(shuffled, expr, universe="tradable_non_st")

    base = base.set_index(["stock_code", "trade_date"])["factor_value"].sort_index()
    other = other.set_index(["stock_code", "trade_date"])["factor_value"].sort_index()
    pd.testing.assert_series_equal(base, other)


def test_backtest_factor_values_match_canonical_evaluator(monkeypatch):
    from quantgpt.backtest import api_context, run_factor_backtest
    import quantgpt.factor_evaluator as evaluator

    frame = _sample_market_frame()
    expression = "rank(ts_mean(close, 2))"
    monkeypatch.setattr(
        evaluator,
        "fixed_non_st_mask",
        lambda df, baseline_date=None: pd.Series(True, index=df.index),
    )
    with api_context():
        result = run_factor_backtest(
            frame,
            expression,
            holding_period=2,
            n_groups=2,
            universe="tradable_non_st",
    )

    backtest_values = result["_factor_df"][["trade_date", "stock_code", "factor_value"]].copy()
    expected = evaluator.evaluate_factor_frame(frame, expression, universe="tradable_non_st")
    merged = backtest_values.merge(
        expected,
        on=["trade_date", "stock_code"],
        suffixes=("_backtest", "_expected"),
    )

    assert not merged.empty
    pd.testing.assert_series_equal(
        merged["factor_value_backtest"].reset_index(drop=True),
        merged["factor_value_expected"].reset_index(drop=True),
        check_names=False,
    )
