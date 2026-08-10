from __future__ import annotations

import os

import pandas as pd
import pytest

from domain.data_foundation import tushare_rebuild as rebuild
from domain.data_foundation import tushare_production


class _FakePro:
    def __init__(self) -> None:
        self._calendar = pd.DataFrame(
            {
                "cal_date": [
                    "20171226",
                    "20171227",
                    "20171228",
                    "20171229",
                    "20180102",
                    "20180103",
                    "20180104",
                    "20180105",
                ],
                "is_open": ["1"] * 8,
            }
        )
        self._stock_basic = pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "600000.SH", "920001.BJ"],
                "name": ["PingAn", "PFBank", "BJDemo"],
                "list_status": ["L", "L", "L"],
                "list_date": ["19910403", "19991110", "20200101"],
                "exchange": ["SZSE", "SSE", "BSE"],
                "market": ["Main", "Main", "BSE"],
                "delist_date": [None, None, None],
            }
        )

    def trade_cal(self, **kwargs):
        return self._calendar

    def stock_basic(self, **kwargs):
        status = kwargs.get("list_status")
        if status:
            return self._stock_basic[self._stock_basic["list_status"].eq(status)].reset_index(drop=True)
        return self._stock_basic

    def stock_st(self, **kwargs):
        return pd.DataFrame(columns=["ts_code", "name", "trade_date", "type", "type_name"])


class _ProxyInspectPro(_FakePro):
    def trade_cal(self, **kwargs):
        import os

        assert os.environ.get("HTTP_PROXY") is None
        assert os.environ.get("http_proxy") is None
        return super().trade_cal(**kwargs)


def test_tushare_preflight_uses_padded_calendar_and_limits(monkeypatch):
    monkeypatch.setattr(rebuild, "get_tushare_client", lambda **kwargs: _FakePro())

    result = rebuild.tushare_preflight(
        start_date="20180101",
        cutoff_date="20180105",
        pad_trading_days=2,
        max_trade_days=3,
        max_codes=1,
        proxy_mode="inherit",
    )

    assert result["effective_target_date"] == "20180105"
    assert result["padded_start_date"] == "20171228"
    assert result["trade_dates"] == ["20180102", "20180103", "20180104"]
    assert result["codes"] == ["000001.SZ"]


def test_tushare_preflight_excludes_bj_universe(monkeypatch):
    monkeypatch.setattr(rebuild, "get_tushare_client", lambda **kwargs: _FakePro())

    result = rebuild.tushare_preflight(
        start_date="20180101",
        cutoff_date="20180105",
        proxy_mode="inherit",
    )

    assert "920001.BJ" not in result["codes"]


def test_stock_basic_statuses_use_lp_for_market_universe():
    frame = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ"],
            "name": ["A", "B", "C"],
            "list_status": ["L", "P", "D"],
            "list_date": ["20000101", "20000101", "20000101"],
            "delist_date": [None, None, "20240101"],
        }
    )

    tradable = rebuild._tradable_stock_basic(frame)

    assert tradable["ts_code"].tolist() == ["000001.SZ", "000002.SZ"]


def test_stock_basic_tradable_universe_is_point_in_time_for_future_delist():
    frame = pd.DataFrame(
        {
            "ts_code": ["688287.SH", "000001.SZ", "000003.SZ"],
            "name": ["退市观典", "平安银行", "OldDelist"],
            "list_status": ["D", "L", "D"],
            "list_date": ["20220525", "19910403", "20000101"],
            "delist_date": ["20260610", None, "20240101"],
        }
    )

    tradable = rebuild._tradable_stock_basic(frame, as_of_date="20260602")

    assert tradable["ts_code"].tolist() == ["688287.SH", "000001.SZ"]


def test_apply_status_fields_prefers_stock_st_and_preserves_tushare_list_status():
    base = pd.DataFrame(
        {
            "code": ["000001.SZ", "000002.SZ", "000003.SZ"],
            "trade_date": pd.to_datetime(["2020-01-02", "2020-01-02", "2020-01-02"]),
            "name": ["Normal", "STDemo", "退市Demo"],
            "list_status": ["L", "L", "L"],
        }
    )
    stock_basic = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ"],
            "name": ["Normal", "STDemo", "退市Demo"],
            "list_status": ["L", "L", "L"],
            "list_date": ["20000101", "20000101", "20000101"],
        }
    )
    stock_st = pd.DataFrame(
        {
            "ts_code": ["000002.SZ"],
            "name": ["STDemo"],
            "trade_date": ["20200102"],
            "type": ["ST"],
            "type_name": ["风险警示板"],
        }
    )

    out = rebuild._apply_status_fields(base, stock_basic_df=stock_basic, stock_st_df=stock_st)

    by_code = out.set_index("code")
    assert by_code.loc["000001.SZ", "st_status"] == "NORMAL"
    assert by_code.loc["000002.SZ", "st_status"] == "ST"
    assert by_code.loc["000003.SZ", "st_status"] == "DELIST"
    assert by_code.loc["000003.SZ", "list_status"] == "L"


def test_tushare_preflight_direct_mode_clears_proxy_env(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7890")
    monkeypatch.setattr(rebuild, "get_tushare_client", lambda **kwargs: _ProxyInspectPro())

    result = rebuild.tushare_preflight(
        start_date="20180101",
        cutoff_date="20180105",
        proxy_mode="direct",
    )

    assert result["status"] == "ok"


def test_assemble_research_daily_chunk_merges_selected_fields():
    daily_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "trade_date": ["20180102", "20180103"],
            "open": [10.0, 10.5],
            "high": [10.2, 10.8],
            "low": [9.9, 10.1],
            "close": [10.1, 10.6],
            "vol": [1000, 1200],
            "amount": [2000, 2500],
        }
    )
    stock_basic_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "name": ["PingAn"],
            "list_status": ["L"],
            "list_date": ["19910403"],
        }
    )
    hfq_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "trade_date": ["20180102", "20180103"],
            "open": [20.0, 21.0],
            "high": [20.4, 21.6],
            "low": [19.8, 20.2],
            "close": [20.2, 21.2],
        }
    )
    adj_df = pd.DataFrame({"ts_code": ["000001.SZ", "000001.SZ"], "trade_date": ["20180102", "20180103"], "adj_factor": [2.0, 2.0]})
    daily_basic_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "trade_date": ["20180102", "20180103"],
            "turnover_rate": [1.0, 1.1],
            "turnover_rate_f": [0.8, 0.9],
            "pe_ttm": [8.0, 8.1],
            "pb": [1.2, 1.3],
            "ps_ttm": [2.1, 2.2],
            "dv_ttm": [0.5, 0.5],
            "total_mv": [1000, 1010],
            "circ_mv": [900, 910],
            "total_share": [500, 500],
            "float_share": [450, 450],
            "free_share": [430, 430],
        }
    )
    stk_limit_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "trade_date": ["20180102", "20180103"],
            "pre_close": [9.8, 10.3],
            "up_limit": [11.11, 11.66],
            "down_limit": [9.09, 9.54],
        }
    )
    moneyflow_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20180103"],
            "buy_sm_vol": [100],
            "sell_sm_vol": [60],
            "buy_sm_amount": [150],
            "sell_sm_amount": [90],
            "buy_lg_vol": [50],
            "sell_lg_vol": [10],
            "buy_lg_amount": [80],
            "sell_lg_amount": [20],
            "net_mf_vol": [80],
            "net_mf_amount": [120],
        }
    )
    margin_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20180103"],
            "rzmre": [10.0],
            "rzye": [20.0],
            "rqye": [3.0],
        }
    )
    cyq_perf_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20180103"],
            "cost_15pct": [9.8],
            "cost_85pct": [10.7],
            "weight_avg": [10.2],
        }
    )
    income_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "ann_date": ["20171231"],
            "end_date": ["20170930"],
            "n_income_attr_p": [100.0],
            "basic_eps": [0.6],
        }
    )
    balancesheet_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "ann_date": ["20171231"],
            "end_date": ["20170930"],
            "total_hldr_eqy_exc_min_int": [300.0],
            "total_assets": [900.0],
        }
    )
    fina_indicator_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "ann_date": ["20171231"],
            "end_date": ["20170930"],
            "eps": [0.5],
            "roe": [11.0],
            "roa": [4.5],
        }
    )
    holder_df = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "ann_date": ["20171231"],
            "end_date": ["20171231"],
            "holder_num": [12345],
        }
    )

    out = rebuild._assemble_research_daily_chunk(
        daily_df=daily_df,
        stock_basic_df=stock_basic_df,
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

    assert list(out.columns) == rebuild.RESEARCH_DAILY_FIELDS
    assert len(out) == 2
    row = out[out["trade_date"] == pd.Timestamp("2018-01-03")].iloc[0]
    assert row["name"] == "PingAn"
    assert row["st_status"] == "NORMAL"
    assert row["close"] == 10.6
    assert row["up_limit"] == 11.66
    assert row["down_limit"] == 9.54
    assert row["stk_limit_pre_close"] == 10.3
    assert row["hfq_close"] == 21.2
    assert row["volume"] == 1200
    assert row["amount"] == 2500
    assert row["net_profit"] == 100.0
    assert row["total_assets"] == 900.0
    assert row["sm_net_vol"] == 40
    assert row["lg_net_amount"] == 60
    assert row["margin_balance"] == 20.0

    normalized, _, _ = tushare_production._normalize_stock_chunk(out, {}, {})
    normalized_row = normalized[normalized["kline_time"] == pd.Timestamp("2018-01-03")].iloc[0]
    assert normalized_row["pre_close"] == 10.3
    assert normalized_row["stk_limit_pre_close"] == 10.3
    assert normalized_row["pct_chg"] == pytest.approx((10.6 - 10.3) / 10.3 * 100.0)


def test_assemble_research_daily_chunk_derives_hfq_without_pro_bar_call():
    out = rebuild._assemble_research_daily_chunk(
        daily_df=pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": ["20260102"],
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [10.5],
                "vol": [100.0],
                "amount": [1000.0],
            }
        ),
        stock_basic_df=pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "name": ["sample"],
                "list_status": ["L"],
                "list_date": ["20000101"],
            }
        ),
        hfq_df=pd.DataFrame(),
        adj_df=pd.DataFrame(
            {"ts_code": ["000001.SZ"], "trade_date": ["20260102"], "adj_factor": [2.0]}
        ),
        daily_basic_df=pd.DataFrame(),
        stk_limit_df=pd.DataFrame(),
        moneyflow_df=pd.DataFrame(),
        margin_df=pd.DataFrame(),
        cyq_perf_df=pd.DataFrame(),
        income_df=pd.DataFrame(),
        balancesheet_df=pd.DataFrame(),
        fina_indicator_df=pd.DataFrame(),
        holder_df=pd.DataFrame(),
    )

    row = out.iloc[0]
    assert row["hfq_open"] == 20.0
    assert row["hfq_high"] == 22.0
    assert row["hfq_low"] == 18.0
    assert row["hfq_close"] == 21.0


def test_official_limit_prices_drive_trade_limit_flags():
    source = pd.DataFrame(
        {
            "open": [9.5, 10.5, 9.0, 8.5],
            "high": [10.0, 11.0, 10.0, 9.0],
            "low": [9.0, 9.0, 8.0, 8.0],
            "close": [9.8, 10.5, 9.0, 8.5],
            "up_limit": [10.0, 10.5, pd.NA, 10.0],
            "down_limit": [8.5, 9.0, pd.NA, 8.5],
            "pct_chg": [0.0, 0.0, -9.6, -10.0],
        }
    )
    limit_rate = pd.Series([0.10, 0.10, 0.10, 0.10])

    (
        limit_buy,
        limit_sell,
        official,
        limit_buy_open,
        limit_sell_open,
        limit_buy_mid_oc,
        limit_sell_mid_oc,
        fallback_buy,
        fallback_sell,
        sealed_fields,
    ) = tushare_production._official_limit_flags(source, limit_rate)
    hit_up, hit_down = tushare_production._intraday_limit_hits(source)

    assert limit_buy.tolist() == [0.0, 1.0, 0.0, 0.0]
    assert limit_sell.tolist() == [0.0, 0.0, 0.0, 1.0]
    assert official.tolist() == [1.0, 1.0, 0.0, 1.0]
    assert limit_buy_open.tolist() == [0.0, 1.0, 0.0, 0.0]
    assert limit_sell_open.tolist() == [0.0, 0.0, 0.0, 1.0]
    assert limit_buy_mid_oc.tolist() == [0.0, 1.0, 0.0, 0.0]
    assert limit_sell_mid_oc.tolist() == [0.0, 0.0, 0.0, 1.0]
    assert fallback_buy.tolist() == [0.0, 0.0, 0.0, 0.0]
    assert fallback_sell.tolist() == [0.0, 0.0, 1.0, 1.0]
    assert hit_up.tolist() == [1.0, 1.0, 0.0, 0.0]
    assert hit_down.tolist() == [0.0, 1.0, 0.0, 1.0]
    assert sealed_fields["limit_buy_open_sealed"].tolist() == [0.0, 0.0, 0.0, 0.0]
    assert sealed_fields["limit_sell_open_sealed"].tolist() == [0.0, 0.0, 0.0, 0.0]


def test_sealed_limit_requires_one_price_and_low_relative_turnover():
    source = pd.DataFrame(
        {
            "open": [10.0, 10.0, 10.0, 8.0],
            "high": [10.0, 10.0, 10.0, 8.0],
            "low": [9.8, 10.0, 10.0, 8.0],
            "close": [9.9, 10.0, 10.0, 8.0],
            "up_limit": [10.0, 10.0, 10.0, 10.0],
            "down_limit": [8.0, 8.0, 8.0, 8.0],
            "pct_chg": [10.0, 10.0, 10.0, -10.0],
            "turnover_rate": [0.01, 0.20, 0.01, 0.01],
        }
    )
    limit_rate = pd.Series([0.10, 0.10, 0.10, 0.10])

    *_, sealed_fields = tushare_production._official_limit_flags(source, limit_rate)

    assert sealed_fields["one_price_up_limit"].tolist() == [0.0, 1.0, 1.0, 0.0]
    assert sealed_fields["one_price_down_limit"].tolist() == [0.0, 0.0, 0.0, 1.0]
    assert sealed_fields["limit_low_liquidity"].tolist() == [1.0, 0.0, 1.0, 1.0]
    assert sealed_fields["limit_buy_open_sealed"].tolist() == [0.0, 0.0, 1.0, 0.0]
    assert sealed_fields["limit_sell_open_sealed"].tolist() == [0.0, 0.0, 0.0, 1.0]


def test_sealed_limit_does_not_fallback_to_volume_or_amount_when_turnover_rate_missing():
    source = pd.DataFrame(
        {
            "open": [10.0, 10.0],
            "high": [10.0, 10.0],
            "low": [10.0, 10.0],
            "close": [10.0, 10.0],
            "up_limit": [10.0, 10.0],
            "down_limit": [8.0, 8.0],
            "pct_chg": [10.0, 10.0],
            "volume": [10.0, 1000.0],
            "free_share": [1000.0, 1000.0],
            "amount": [10.0, 1000.0],
            "float_mv": [100000.0, 100000.0],
        }
    )
    limit_rate = pd.Series([0.10, 0.10])

    *_, sealed_fields = tushare_production._official_limit_flags(source, limit_rate)

    assert sealed_fields["one_price_up_limit"].tolist() == [1.0, 1.0]
    assert sealed_fields["limit_turnover_ratio"].isna().all()
    assert sealed_fields["limit_low_liquidity"].tolist() == [0.0, 0.0]
    assert sealed_fields["limit_buy_open_sealed"].tolist() == [0.0, 0.0]


def test_index_compat_rows_include_empty_limit_price_columns():
    source = pd.DataFrame(
        {
            "code": ["000300.SH"],
            "trade_date": pd.to_datetime(["2026-06-26"]),
            "open": [4000.0],
            "high": [4010.0],
            "low": [3990.0],
            "close": [4005.0],
            "volume": [1000.0],
            "amount": [2000.0],
        }
    )

    out = tushare_production._normalize_index_frame(source)

    assert "up_limit" in out.columns
    assert "down_limit" in out.columns
    assert pd.isna(out.iloc[0]["up_limit"])
    assert pd.isna(out.iloc[0]["down_limit"])
    assert out.iloc[0]["limit_source_kind"] == "index"


def test_production_limit_source_kind_classifies_official_structural_and_index_rows():
    frame = pd.DataFrame(
        {
            "code": ["000001.SZ", "688033.SH", "000300.SH", "000002.SZ"],
            "kline_time": pd.to_datetime(["2026-01-02", "2019-07-22", "2026-01-02", "2026-01-02"]),
            "LIST_DATE": ["19910403", "20190722", "20050101", "19910129"],
            "list_status": ["L", "L", "I", "L"],
            "up_limit": [11.0, pd.NA, pd.NA, pd.NA],
            "down_limit": [9.0, pd.NA, pd.NA, pd.NA],
        }
    )

    result = tushare_production._limit_source_kind_for_frame(frame)

    assert result.tolist() == ["official", "structural_no_limit", "index", "missing"]


def test_resume_manifest_mismatch_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(rebuild, "STAGING_ROOT", tmp_path)
    plan = {
        "package_id": "pkg",
        "start_date": "20180101",
        "cutoff_date": "20260602",
        "effective_target_date": "20260602",
        "selected_target_date": "20260602",
        "padded_start_date": "20170710",
        "proxy_mode": "direct",
        "proxy_env": {},
        "trade_date_chunk_size": 40,
        "trade_date_count": 2,
        "trade_dates": ["20180102", "20180103"],
        "trade_dates_sha256": rebuild._stable_list_sha256(["20180102", "20180103"]),
        "code_count": 1,
        "codes": ["000001.SZ"],
        "codes_sha256": rebuild._stable_list_sha256(["000001.SZ"]),
        "network": {"status": "ok"},
    }
    package_root = rebuild._package_root("pkg")
    rebuild._ensure_layout(package_root)
    rebuild._write_manifest(package_root, plan)
    rebuild._save_progress(package_root, rebuild._initial_progress(plan))

    changed = dict(plan)
    changed["trade_date_chunk_size"] = 20

    with pytest.raises(RuntimeError, match="tushare_resume_manifest_mismatch"):
        rebuild._load_or_init_package(rebuild.TushareRebuildConfig(package_id="pkg"), changed)


def test_existing_package_without_manifest_rewrites_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(rebuild, "STAGING_ROOT", tmp_path)
    plan = {
        "package_id": "pkg",
        "start_date": "20180101",
        "cutoff_date": "20260602",
        "effective_target_date": "20260602",
        "selected_target_date": "20260602",
        "padded_start_date": "20170710",
        "proxy_mode": "direct",
        "proxy_env": {},
        "trade_date_chunk_size": 40,
        "trade_date_count": 2,
        "trade_dates": ["20180102", "20180103"],
        "trade_dates_sha256": rebuild._stable_list_sha256(["20180102", "20180103"]),
        "code_count": 1,
        "codes": ["000001.SZ"],
        "codes_sha256": rebuild._stable_list_sha256(["000001.SZ"]),
        "network": {"status": "ok"},
    }
    package_root = rebuild._package_root("pkg")
    rebuild._ensure_layout(package_root)

    _, progress = rebuild._load_or_init_package(rebuild.TushareRebuildConfig(package_id="pkg"), plan)

    assert rebuild._manifest_path(package_root).exists()
    manifest = rebuild._read_json(rebuild._manifest_path(package_root))
    assert manifest["effective_target_date"] == "20260602"
    assert progress["stages"]["daily"]["total"] == 2


def test_stage_sleep_seconds_throttles_cyq_perf():
    assert rebuild._stage_sleep_seconds("cyq_perf", 0.15) == 0.40
    assert rebuild._stage_sleep_seconds("income", 0.15) == 0.15


def test_retry_sleep_seconds_uses_long_backoff_for_rate_limit():
    error = RuntimeError("tushare_api_error:cyq_perf:doc_id=108 rate limit")
    assert rebuild._retry_sleep_seconds(stage="cyq_perf", attempt=1, base_seconds=2.0, error=error) == 75.0
    assert rebuild._retry_sleep_seconds(stage="income", attempt=1, base_seconds=2.0, error=error) == 30.0


def test_retry_sleep_seconds_uses_network_backoff_for_transient_errors():
    error = RuntimeError("ConnectionResetError(104, 'Connection reset by peer')")
    assert rebuild._retry_sleep_seconds(stage="cyq_perf", attempt=1, base_seconds=2.0, error=error) == 20.0
    assert rebuild._retry_sleep_seconds(stage="income", attempt=1, base_seconds=2.0, error=error) == 10.0


def test_stage_retry_attempts_raises_cyq_perf_budget():
    assert rebuild._stage_retry_attempts("cyq_perf", 5) == 8
    assert rebuild._stage_retry_attempts("income", 5) == 5


def test_date_windows_splits_range():
    windows = rebuild._date_windows("20200101", "20210110", window_days=365)
    assert windows == [("20200101", "20201230"), ("20201231", "20210110")]


def test_fetch_cyq_perf_windowed_combines_and_dedupes():
    class _FakeCyqPro:
        def __init__(self):
            self.calls = []

        def cyq_perf(self, **kwargs):
            self.calls.append((kwargs["start_date"], kwargs["end_date"]))
            if kwargs["start_date"] == "20200101":
                return pd.DataFrame(
                    {
                        "ts_code": ["000001.SZ", "000001.SZ"],
                        "trade_date": ["20200102", "20201230"],
                        "cost_15pct": [1.0, 2.0],
                        "cost_85pct": [3.0, 4.0],
                        "weight_avg": [2.0, 3.0],
                    }
                )
            return pd.DataFrame(
                {
                    "ts_code": ["000001.SZ", "000001.SZ"],
                    "trade_date": ["20201230", "20210105"],
                    "cost_15pct": [2.0, 5.0],
                    "cost_85pct": [4.0, 6.0],
                    "weight_avg": [3.0, 5.5],
                }
            )

    pro = _FakeCyqPro()
    out = rebuild._fetch_cyq_perf_windowed(
        pro,
        code="000001.SZ",
        start_date="20200101",
        end_date="20210110",
        fields="ts_code,trade_date,cost_15pct,cost_85pct,weight_avg",
    )

    assert pro.calls == [("20200101", "20201230"), ("20201231", "20210110")]
    assert list(out["trade_date"]) == ["20200102", "20201230", "20210105"]


def test_run_code_stage_refreshes_session_periodically(tmp_path, monkeypatch):
    monkeypatch.setattr(rebuild, "STAGING_ROOT", tmp_path)
    package_root = rebuild._package_root("pkg")
    rebuild._ensure_layout(package_root)
    progress = {"stages": {"cyq_perf": {"cursor": 0, "total": 0, "status": "pending"}}}
    refresh_calls = []

    def _fetch(code):
        return pd.DataFrame({"ts_code": [code], "trade_date": ["20250101"], "cost_15pct": [1.0], "cost_85pct": [2.0], "weight_avg": [1.5]})

    rebuild._run_code_stage(
        package_root=package_root,
        progress=progress,
        stage_name="cyq_perf",
        codes=["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ", "000005.SZ"],
        sleep_seconds=0.0,
        fetcher=_fetch,
        columns=["ts_code", "trade_date", "cost_15pct", "cost_85pct", "weight_avg"],
        refresh_every=2,
        refresh_hook=lambda: refresh_calls.append("refresh"),
    )

    assert refresh_calls == ["refresh", "refresh"]


def test_load_or_init_package_normalizes_missing_new_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(rebuild, "STAGING_ROOT", tmp_path)
    plan = {
        "package_id": "pkg",
        "start_date": "20180101",
        "cutoff_date": "20260602",
        "effective_target_date": "20260602",
        "selected_target_date": "20260602",
        "padded_start_date": "20170710",
        "proxy_mode": "direct",
        "proxy_env": {},
        "trade_date_chunk_size": 40,
        "trade_date_count": 2,
        "trade_dates": ["20180102", "20180103"],
        "trade_dates_sha256": rebuild._stable_list_sha256(["20180102", "20180103"]),
        "code_count": 1,
        "codes": ["000001.SZ"],
        "codes_sha256": rebuild._stable_list_sha256(["000001.SZ"]),
        "network": {"status": "ok"},
    }
    package_root = rebuild._package_root("pkg")
    rebuild._ensure_layout(package_root)
    rebuild._write_manifest(package_root, plan)
    rebuild._write_json(
        rebuild._progress_path(package_root),
        {
            "status": "initialized",
            "package_id": "pkg",
            "stages": {
                "stock_basic": {"cursor": 1, "total": 1, "status": "completed"},
                "daily": {"cursor": 0, "total": 2, "status": "pending"},
            },
        },
    )

    _, progress = rebuild._load_or_init_package(rebuild.TushareRebuildConfig(package_id="pkg"), plan)

    assert "raw_quality_report" in progress["stages"]
    assert progress["stages"]["stock_basic"]["status"] == "completed"


def test_raw_quality_report_ignores_pre_listing_absence(tmp_path):
    package_root = tmp_path / "pkg"
    rebuild._ensure_layout(package_root)
    stock_basic = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "name": ["A", "B"],
            "list_status": ["L", "L"],
            "list_date": ["20200102", "20200103"],
        }
    )
    rebuild._write_frame(rebuild._endpoint_dir(package_root, "stock_basic") / "all.parquet", stock_basic)

    daily_0102 = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20200102"],
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "vol": [100],
            "amount": [200],
        }
    )
    daily_0103 = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "trade_date": ["20200103", "20200103"],
            "open": [10.1, 20.0],
            "high": [10.6, 20.5],
            "low": [9.9, 19.8],
            "close": [10.3, 20.2],
            "vol": [110, 210],
            "amount": [220, 420],
        }
    )
    rebuild._write_frame(rebuild._date_file(package_root, "daily", "20200102"), daily_0102)
    rebuild._write_frame(rebuild._date_file(package_root, "daily", "20200103"), daily_0103)

    for endpoint in ["stk_limit", "daily_basic", "adj_factor", "moneyflow", "margin_detail"]:
        rebuild._write_frame(rebuild._date_file(package_root, endpoint, "20200102"), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}))
        rebuild._write_frame(
            rebuild._date_file(package_root, endpoint, "20200103"),
            pd.DataFrame({"ts_code": ["000001.SZ", "000002.SZ"], "trade_date": ["20200103", "20200103"]}),
        )

    rebuild._write_frame(
        rebuild._code_file(package_root, "pro_bar_hfq", "000001.SZ"),
        pd.DataFrame({"ts_code": ["000001.SZ", "000001.SZ"], "trade_date": ["20200102", "20200103"]}),
    )
    rebuild._write_frame(
        rebuild._code_file(package_root, "pro_bar_hfq", "000002.SZ"),
        pd.DataFrame({"ts_code": ["000002.SZ"], "trade_date": ["20200103"]}),
    )
    rebuild._write_frame(
        rebuild._code_file(package_root, "cyq_perf", "000001.SZ"),
        pd.DataFrame({"ts_code": ["000001.SZ", "000001.SZ"], "trade_date": ["20200102", "20200103"]}),
    )
    rebuild._write_frame(
        rebuild._code_file(package_root, "cyq_perf", "000002.SZ"),
        pd.DataFrame({"ts_code": ["000002.SZ"], "trade_date": ["20200103"]}),
    )

    for code in rebuild.BENCHMARK_INDEX_CODES:
        rebuild._write_frame(
            rebuild._code_file(package_root, "index_daily", code),
            pd.DataFrame({"ts_code": [code], "trade_date": ["20200103"]}),
        )

    report = rebuild._build_raw_quality_report(
        package_root,
        {"effective_target_date": "20200103", "selected_target_date": "20200103"},
        ["20200102", "20200103"],
    )

    assert report["passed"] is True
    assert report["daily"]["codes_without_rows"] == 0


def test_raw_quality_report_flags_post_listing_missing_rows(tmp_path):
    package_root = tmp_path / "pkg"
    rebuild._ensure_layout(package_root)
    stock_basic = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "name": ["A", "B"],
            "list_status": ["L", "L"],
            "list_date": ["20200102", "20200102"],
        }
    )
    rebuild._write_frame(rebuild._endpoint_dir(package_root, "stock_basic") / "all.parquet", stock_basic)
    rebuild._write_frame(
        rebuild._date_file(package_root, "daily", "20200102"),
        pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "vol": [1], "amount": [1]}),
    )
    for endpoint in ["stk_limit", "daily_basic", "adj_factor", "moneyflow", "margin_detail"]:
        rebuild._write_frame(rebuild._date_file(package_root, endpoint, "20200102"), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}))
    rebuild._write_frame(
        rebuild._code_file(package_root, "pro_bar_hfq", "000001.SZ"),
        pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}),
    )
    rebuild._write_frame(
        rebuild._code_file(package_root, "cyq_perf", "000001.SZ"),
        pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}),
    )
    for code in rebuild.BENCHMARK_INDEX_CODES:
        rebuild._write_frame(
            rebuild._code_file(package_root, "index_daily", code),
            pd.DataFrame({"ts_code": [code], "trade_date": ["20200102"]}),
        )

    report = rebuild._build_raw_quality_report(
        package_root,
        {"effective_target_date": "20200102", "selected_target_date": "20200102"},
        ["20200102"],
    )

    assert report["passed"] is False
    assert "daily_codes_without_rows:1" in report["issues"]


def test_raw_quality_report_accepts_valid_locally_derived_hfq(tmp_path):
    package_root = tmp_path / "pkg"
    rebuild._ensure_layout(package_root)
    rebuild._write_frame(
        rebuild._endpoint_dir(package_root, "stock_basic") / "all.parquet",
        pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "name": ["A"],
                "list_status": ["L"],
                "list_date": ["20200101"],
            }
        ),
    )
    daily = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20200102"],
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "vol": [100],
            "amount": [200],
        }
    )
    rebuild._write_frame(rebuild._date_file(package_root, "daily", "20200102"), daily)
    for endpoint in ["stk_limit", "daily_basic", "moneyflow", "margin_detail"]:
        rebuild._write_frame(
            rebuild._date_file(package_root, endpoint, "20200102"),
            pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}),
        )
    rebuild._write_frame(
        rebuild._date_file(package_root, "adj_factor", "20200102"),
        pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"], "adj_factor": [2.0]}),
    )
    rebuild._write_frame(
        rebuild._code_file(package_root, "cyq_perf", "000001.SZ"),
        pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}),
    )
    for code in rebuild.BENCHMARK_INDEX_CODES:
        rebuild._write_frame(
            rebuild._code_file(package_root, "index_daily", code),
            pd.DataFrame({"ts_code": [code], "trade_date": ["20200102"]}),
        )

    report = rebuild._build_raw_quality_report(
        package_root,
        {
            "effective_target_date": "20200102",
            "selected_target_date": "20200102",
            "hfq_derivation": {"mode": "local", "formula": "daily_ohlc_times_adj_factor"},
        },
        ["20200102"],
    )

    assert report["passed"] is True
    assert report["code_stage_comparisons"]["pro_bar_hfq"]["derivation"] == "daily_ohlc_times_adj_factor"
    assert report["code_stage_comparisons"]["pro_bar_hfq"]["codes_without_rows"] == 0


def test_raw_quality_report_does_not_require_delisted_status_daily_rows(tmp_path):
    package_root = tmp_path / "pkg"
    rebuild._ensure_layout(package_root)
    stock_basic = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000003.SZ"],
            "name": ["A", "Old"],
            "list_status": ["L", "D"],
            "list_date": ["20200102", "19910101"],
        }
    )
    rebuild._write_frame(rebuild._endpoint_dir(package_root, "stock_basic") / "all.parquet", stock_basic)
    rebuild._write_frame(
        rebuild._date_file(package_root, "daily", "20200102"),
        pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "vol": [1], "amount": [1]}),
    )
    for endpoint in ["stk_limit", "daily_basic", "adj_factor", "moneyflow", "margin_detail"]:
        rebuild._write_frame(rebuild._date_file(package_root, endpoint, "20200102"), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}))
    rebuild._write_frame(
        rebuild._code_file(package_root, "pro_bar_hfq", "000001.SZ"),
        pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}),
    )
    rebuild._write_frame(
        rebuild._code_file(package_root, "cyq_perf", "000001.SZ"),
        pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}),
    )
    for code in rebuild.BENCHMARK_INDEX_CODES:
        rebuild._write_frame(
            rebuild._code_file(package_root, "index_daily", code),
            pd.DataFrame({"ts_code": [code], "trade_date": ["20200102"]}),
        )

    report = rebuild._build_raw_quality_report(
        package_root,
        {"effective_target_date": "20200102", "selected_target_date": "20200102"},
        ["20200102"],
    )

    assert report["passed"] is True
    assert report["eligible_code_count"] == 1
    assert report["daily"]["codes_without_rows"] == 0


def test_raw_quality_report_warns_for_fully_suspended_missing_daily_rows(tmp_path):
    package_root = tmp_path / "pkg"
    rebuild._ensure_layout(package_root)
    stock_basic = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "name": ["A", "B"],
            "list_status": ["L", "L"],
            "list_date": ["20200102", "20200102"],
        }
    )
    rebuild._write_frame(rebuild._endpoint_dir(package_root, "stock_basic") / "all.parquet", stock_basic)
    rebuild._write_frame(
        rebuild._date_file(package_root, "daily", "20200102"),
        pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "vol": [1], "amount": [1]}),
    )
    rebuild._write_frame(
        rebuild._date_file(package_root, "suspend_d", "20200102"),
        pd.DataFrame({"ts_code": ["000002.SZ"], "trade_date": ["20200102"], "suspend_timing": [None], "suspend_type": ["S"]}),
    )
    for endpoint in ["stk_limit", "daily_basic", "adj_factor", "moneyflow", "margin_detail"]:
        rebuild._write_frame(rebuild._date_file(package_root, endpoint, "20200102"), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}))
    rebuild._write_frame(
        rebuild._code_file(package_root, "pro_bar_hfq", "000001.SZ"),
        pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}),
    )
    rebuild._write_frame(
        rebuild._code_file(package_root, "cyq_perf", "000001.SZ"),
        pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20200102"]}),
    )
    for code in rebuild.BENCHMARK_INDEX_CODES:
        rebuild._write_frame(
            rebuild._code_file(package_root, "index_daily", code),
            pd.DataFrame({"ts_code": [code], "trade_date": ["20200102"]}),
        )

    report = rebuild._build_raw_quality_report(
        package_root,
        {"effective_target_date": "20200102", "selected_target_date": "20200102"},
        ["20200102"],
    )

    assert report["passed"] is True
    assert "daily_codes_without_rows_suspended:1" in report["warnings"]
    assert report["daily"]["suspended_codes_without_rows"] == 1
    assert report["daily"]["unsuspended_codes_without_rows"] == 0


def test_write_hdf_table_allows_longer_strings_on_append(tmp_path):
    path = tmp_path / "research_daily.h5"
    first = pd.DataFrame(
        {
            "code": ["000001.SZ"],
            "trade_date": [pd.Timestamp("2020-01-01")],
            "name": ["Short"],
            "list_status": ["L"],
        }
    )
    second = pd.DataFrame(
        {
            "code": ["000002.SZ"],
            "trade_date": [pd.Timestamp("2020-01-02")],
            "name": ["MuchLongerName"],
            "list_status": ["LONGER_STATUS"],
        }
    )

    rebuild._write_hdf_table(path, first)
    rebuild._write_hdf_table(path, second)

    out = pd.read_hdf(path, key="data")
    assert list(out["name"]) == ["Short", "MuchLongerName"]


def test_assemble_chunk_normalizes_string_and_numeric_dtypes():
    out = rebuild._assemble_research_daily_chunk(
        daily_df=pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": ["20180102"],
                "open": [10.0],
                "high": [10.2],
                "low": [9.9],
                "close": [10.1],
                "vol": [1000],
                "amount": [2000],
            }
        ),
        stock_basic_df=pd.DataFrame({"ts_code": ["000001.SZ"], "name": ["PingAn"], "list_status": ["L"], "list_date": ["19910403"]}),
        hfq_df=pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20180102"], "open": [20.0], "high": [20.4], "low": [19.8], "close": [20.2]}),
        adj_df=pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20180102"], "adj_factor": [2.0]}),
        daily_basic_df=pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20180102"], "turnover_rate": [1.0], "turnover_rate_f": [0.8], "pe_ttm": [8.0], "pb": [1.2], "ps_ttm": [2.1], "dv_ttm": [0.5], "total_mv": [1000], "circ_mv": [900], "total_share": [500], "float_share": [450], "free_share": [430]}),
        moneyflow_df=pd.DataFrame(),
        margin_df=pd.DataFrame(),
        cyq_perf_df=pd.DataFrame(),
        income_df=pd.DataFrame(),
        balancesheet_df=pd.DataFrame(),
        fina_indicator_df=pd.DataFrame(),
        holder_df=pd.DataFrame(),
    )

    assert str(out["code"].dtype) == "string"
    assert str(out["name"].dtype) == "string"
    assert str(out["list_status"].dtype) == "string"
    assert str(out["st_status"].dtype) == "string"
    assert str(out["list_date"].dtype) == "string"
    assert str(out["trade_date"].dtype).startswith("datetime64")
    assert str(out["adj_factor"].dtype).startswith("float")


def test_rebuild_code_stage_hdf_reuses_fresh_consolidated(tmp_path, monkeypatch):
    monkeypatch.setattr(rebuild, "STAGING_ROOT", tmp_path)
    package_root = rebuild._package_root("pkg")
    rebuild._ensure_layout(package_root)
    endpoint_root = rebuild._endpoint_dir(package_root, "cyq_perf")
    endpoint_root.mkdir(parents=True, exist_ok=True)
    source_path = endpoint_root / "000001_SZ.parquet"
    pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20250101"], "cost_15pct": [1.0]}).to_parquet(source_path, index=False)
    consolidated = endpoint_root / "_consolidated.h5"
    pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20250101"], "cost_15pct": [1.0]}).to_hdf(
        consolidated,
        key="data",
        mode="w",
        format="table",
        data_columns=["ts_code", "trade_date"],
    )
    source_mtime = source_path.stat().st_mtime
    consolidated.touch()
    before = consolidated.stat().st_mtime

    out = rebuild._rebuild_code_stage_hdf(package_root, "cyq_perf")

    assert out == consolidated
    assert consolidated.stat().st_mtime >= before
    assert consolidated.stat().st_mtime >= source_mtime


def test_rebuild_code_stage_hdf_supports_empty_derived_stage(tmp_path):
    package_root = tmp_path / "pkg"
    rebuild._ensure_layout(package_root)

    output = rebuild._rebuild_code_stage_hdf(package_root, "pro_bar_hfq")

    assert output.exists()
    assert output.with_suffix(".receipt.json").exists()
    assert rebuild._load_hdf_trade_date_range(
        output,
        start_trade_date="20200101",
        end_trade_date="20200102",
    ).empty


def test_rebuild_code_stage_hdf_invalidates_receipt_when_source_content_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(rebuild, "STAGING_ROOT", tmp_path)
    package_root = rebuild._package_root("pkg")
    rebuild._ensure_layout(package_root)
    source_path = rebuild._endpoint_dir(package_root, "cyq_perf") / "000001_SZ.parquet"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20250101"], "cost_15pct": [1.0]}
    ).to_parquet(source_path, index=False)
    original_mtime = source_path.stat().st_mtime

    consolidated = rebuild._rebuild_code_stage_hdf(package_root, "cyq_perf")
    first_receipt = rebuild._read_json(consolidated.with_suffix(".receipt.json"))
    pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20250101"], "cost_15pct": [2.0]}
    ).to_parquet(source_path, index=False)
    source_path.touch()
    os.utime(source_path, (original_mtime, original_mtime))

    rebuilt = rebuild._rebuild_code_stage_hdf(package_root, "cyq_perf")
    second_receipt = rebuild._read_json(rebuilt.with_suffix(".receipt.json"))

    assert pd.read_hdf(rebuilt, key="data")["cost_15pct"].iloc[0] == 2.0
    assert second_receipt["input_sha256"] != first_receipt["input_sha256"]
    assert second_receipt["output_sha256"] == rebuild._file_sha256(rebuilt)


def test_prepare_effective_date_falls_back_rowwise():
    df = pd.DataFrame(
        {
            "f_ann_date": [None, "20240105"],
            "ann_date": ["20240103", "20240104"],
            "end_date": ["20231231", "20231231"],
        }
    )

    out = rebuild._prepare_effective_date(df)

    assert out["effective_date"].dt.strftime("%Y%m%d").tolist() == ["20240103", "20240105"]


def test_merge_pit_prefers_latest_end_date_for_same_effective_date():
    base = pd.DataFrame(
        {
            "code": ["000001.SZ", "000001.SZ"],
            "trade_date": pd.to_datetime(["2024-03-15", "2024-03-20"]),
        }
    )
    source = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "ann_date": ["20240310", "20240310"],
            "end_date": ["20240229", "20231231"],
            "holder_num": [200, 100],
        }
    )

    out = rebuild._merge_pit(base, source, fields=["holder_num"])

    assert out["holder_num"].tolist() == [200, 200]


def test_quality_report_allows_structural_pe_ttm_missing(tmp_path):
    monkeypatch_root = tmp_path
    package_root = monkeypatch_root / "pkg"
    package_root.mkdir(parents=True, exist_ok=True)
    rebuild._ensure_layout(package_root)
    silver_root = package_root / "silver"
    silver_root.mkdir(parents=True, exist_ok=True)
    stock_basic_root = rebuild._endpoint_dir(package_root, "stock_basic")
    stock_basic_root.mkdir(parents=True, exist_ok=True)
    daily_root = rebuild._endpoint_dir(package_root, "daily")
    daily_root.mkdir(parents=True, exist_ok=True)
    daily_basic_root = rebuild._endpoint_dir(package_root, "daily_basic")
    daily_basic_root.mkdir(parents=True, exist_ok=True)
    stk_limit_root = rebuild._endpoint_dir(package_root, "stk_limit")
    stk_limit_root.mkdir(parents=True, exist_ok=True)
    adj_factor_root = rebuild._endpoint_dir(package_root, "adj_factor")
    adj_factor_root.mkdir(parents=True, exist_ok=True)
    index_root = rebuild._endpoint_dir(package_root, "index_daily")
    index_root.mkdir(parents=True, exist_ok=True)

    target_date = "20200102"
    research_df = pd.DataFrame(
        {
            "code": pd.Series(["000001.SZ", "000002.SZ"], dtype="string"),
            "trade_date": pd.to_datetime(["2020-01-02", "2020-01-02"]),
            "name": pd.Series(["A", "B"], dtype="string"),
            "list_status": pd.Series(["L", "L"], dtype="string"),
            "st_status": pd.Series(["NORMAL", "NORMAL"], dtype="string"),
            "list_date": pd.Series(["20100101", "20100101"], dtype="string"),
            "open": [10.0, 11.0],
            "high": [10.5, 11.5],
            "low": [9.8, 10.8],
            "close": [10.2, 11.1],
            "stk_limit_pre_close": [10.0, 10.8],
            "up_limit": [11.22, 12.21],
            "down_limit": [9.18, 9.99],
            "volume": [1000.0, 1200.0],
            "amount": [2000.0, 2400.0],
            "hfq_open": [20.0, 22.0],
            "hfq_high": [21.0, 23.0],
            "hfq_low": [19.6, 21.6],
            "hfq_close": [20.4, 22.2],
            "adj_factor": [2.0, 2.0],
            "turnover_rate": [1.0, 1.2],
            "turnover_rate_f": [0.8, 1.0],
            "pe_ttm": [None, 8.0],
            "pb": [1.2, 1.3],
            "ps_ttm": [2.0, 2.1],
            "dv_ttm": [None, 0.3],
            "total_mv": [1000.0, 1100.0],
            "float_mv": [900.0, 990.0],
            "total_share": [500.0, 550.0],
            "float_share": [450.0, 495.0],
            "free_share": [430.0, 470.0],
            "eps": [1.0, 1.1],
            "net_profit": [10.0, 11.0],
            "total_equity": [100.0, 120.0],
            "total_assets": [200.0, 220.0],
            "roe": [0.1, 0.11],
            "roa": [0.05, 0.055],
            "holder_num": [1000.0, 1100.0],
            "sm_net_vol": [1.0, 2.0],
            "sm_net_amount": [3.0, 4.0],
            "lg_net_vol": [5.0, 6.0],
            "lg_net_amount": [7.0, 8.0],
            "net_mf_vol": [9.0, 10.0],
            "net_mf_amount": [11.0, 12.0],
            "cost_15pct": [13.0, 14.0],
            "cost_85pct": [15.0, 16.0],
            "weight_avg": [17.0, 18.0],
            "margin_buy_amount": [19.0, 20.0],
            "margin_balance": [21.0, 22.0],
            "short_balance": [23.0, 24.0],
        }
    )
    research_df = research_df[rebuild.RESEARCH_DAILY_FIELDS]
    research_df.to_hdf(silver_root / "research_daily.h5", key="data", mode="w", format="table")

    pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "name": ["A", "B"],
            "list_status": ["L", "L"],
            "list_date": ["20100101", "20100101"],
        }
    ).to_parquet(stock_basic_root / "all.parquet", index=False)

    pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "trade_date": [target_date, target_date],
        }
    ).to_parquet(daily_root / f"{target_date}.parquet", index=False)
    pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "trade_date": [target_date, target_date],
        }
    ).to_parquet(daily_basic_root / f"{target_date}.parquet", index=False)
    pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "trade_date": [target_date, target_date],
            "up_limit": [11.22, 12.21],
            "down_limit": [9.18, 9.99],
        }
    ).to_parquet(stk_limit_root / f"{target_date}.parquet", index=False)
    pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "trade_date": [target_date, target_date],
        }
    ).to_parquet(adj_factor_root / f"{target_date}.parquet", index=False)

    for code in rebuild.BENCHMARK_INDEX_CODES:
        pd.DataFrame({"trade_date": [target_date]}).to_parquet(rebuild._code_file(package_root, "index_daily", code), index=False)

    report = rebuild._build_quality_report(
        package_root,
        silver_root / "research_daily.h5",
        {"effective_target_date": target_date, "selected_target_date": target_date},
    )

    assert report["passed"] is True
    assert not any(issue.startswith("missing_ratio_high:pe_ttm") for issue in report["issues"])
    assert any(warning.startswith("pe_ttm_structural_missing:") for warning in report["warnings"])


def test_quality_report_warns_for_fully_suspended_missing_research_rows(tmp_path):
    package_root = tmp_path / "pkg"
    rebuild._ensure_layout(package_root)
    silver_root = package_root / "silver"
    silver_root.mkdir(parents=True, exist_ok=True)
    target_date = "20200102"

    row = {
        "code": "000001.SZ",
        "trade_date": pd.Timestamp("2020-01-02"),
        "name": "A",
        "list_status": "L",
        "st_status": "NORMAL",
        "list_date": "20100101",
    }
    for field in rebuild.RESEARCH_DAILY_FIELDS:
        row.setdefault(field, 1.0)
    research_df = pd.DataFrame([row])[rebuild.RESEARCH_DAILY_FIELDS]
    research_df.to_hdf(silver_root / "research_daily.h5", key="data", mode="w", format="table")

    rebuild._write_frame(
        rebuild._endpoint_dir(package_root, "stock_basic") / "all.parquet",
        pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "000002.SZ"],
                "name": ["A", "B"],
                "list_status": ["L", "L"],
                "list_date": ["20100101", "20100101"],
            }
        ),
    )
    rebuild._write_frame(rebuild._date_file(package_root, "daily", target_date), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [target_date]}))
    rebuild._write_frame(rebuild._date_file(package_root, "stk_limit", target_date), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [target_date], "up_limit": [1.1], "down_limit": [0.9]}))
    rebuild._write_frame(rebuild._date_file(package_root, "daily_basic", target_date), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [target_date]}))
    rebuild._write_frame(rebuild._date_file(package_root, "adj_factor", target_date), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [target_date]}))
    rebuild._write_frame(
        rebuild._date_file(package_root, "suspend_d", target_date),
        pd.DataFrame({"ts_code": ["000002.SZ"], "trade_date": [target_date], "suspend_timing": [None], "suspend_type": ["S"]}),
    )
    for code in rebuild.BENCHMARK_INDEX_CODES:
        rebuild._write_frame(rebuild._code_file(package_root, "index_daily", code), pd.DataFrame({"trade_date": [target_date]}))

    report = rebuild._build_quality_report(
        package_root,
        silver_root / "research_daily.h5",
        {"effective_target_date": target_date, "selected_target_date": target_date},
    )

    assert report["passed"] is True
    assert "research_daily_codes_without_rows_suspended:1" in report["warnings"]
    assert report["list_date_sanity"]["suspended_codes_without_rows"] == 1
    assert report["list_date_sanity"]["unsuspended_codes_without_rows"] == 0


def test_quality_report_does_not_require_delisted_status_research_rows(tmp_path):
    package_root = tmp_path / "pkg"
    rebuild._ensure_layout(package_root)
    silver_root = package_root / "silver"
    silver_root.mkdir(parents=True, exist_ok=True)
    target_date = "20200102"

    row = {
        "code": "000001.SZ",
        "trade_date": pd.Timestamp("2020-01-02"),
        "name": "A",
        "list_status": "L",
        "st_status": "NORMAL",
        "list_date": "20100101",
    }
    for field in rebuild.RESEARCH_DAILY_FIELDS:
        row.setdefault(field, 1.0)
    research_df = pd.DataFrame([row])[rebuild.RESEARCH_DAILY_FIELDS]
    research_df.to_hdf(silver_root / "research_daily.h5", key="data", mode="w", format="table")

    rebuild._write_frame(
        rebuild._endpoint_dir(package_root, "stock_basic") / "all.parquet",
        pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "000003.SZ"],
                "name": ["A", "Old"],
                "list_status": ["L", "D"],
                "list_date": ["20100101", "19910101"],
            }
        ),
    )
    rebuild._write_frame(rebuild._date_file(package_root, "daily", target_date), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [target_date]}))
    rebuild._write_frame(rebuild._date_file(package_root, "stk_limit", target_date), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [target_date], "up_limit": [1.1], "down_limit": [0.9]}))
    rebuild._write_frame(rebuild._date_file(package_root, "daily_basic", target_date), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [target_date]}))
    rebuild._write_frame(rebuild._date_file(package_root, "adj_factor", target_date), pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [target_date]}))
    for code in rebuild.BENCHMARK_INDEX_CODES:
        rebuild._write_frame(rebuild._code_file(package_root, "index_daily", code), pd.DataFrame({"trade_date": [target_date]}))

    report = rebuild._build_quality_report(
        package_root,
        silver_root / "research_daily.h5",
        {"effective_target_date": target_date, "selected_target_date": target_date},
    )

    assert report["passed"] is True
    assert report["list_date_sanity"]["codes_without_rows"] == 0
