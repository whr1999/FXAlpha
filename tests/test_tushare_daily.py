import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from domain.data_foundation import ops_common
from domain.data_foundation import tushare_daily
from domain.data_foundation.tushare_production import QLIB_RAW_FIELD_MAP, raw_chunk_to_qlib_frame, _write_trading_calendar


@pytest.fixture(autouse=True)
def _isolate_daily_status_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ops_common, "DAILY_STATUS_FILE", tmp_path / "daily_update_status.json")


def _patch_stability_ok(monkeypatch):
    monkeypatch.setattr(
        tushare_daily,
        "_wsl_stability_preflight",
        lambda: {
            "status": "ok",
            "is_wsl": True,
            "checked_paths": ["/mnt/c/Users/test/.wslconfig"],
            "gui_applications_disabled": True,
            "issues": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(tushare_daily, "_hdf_smoke_preflight", lambda: {"status": "ok", "path": "/tmp/smoke.h5"})
    monkeypatch.setattr(
        tushare_daily,
        "production_consistency_status",
        lambda **kwargs: {
            "status": "passed",
            "partial_promote_detected": False,
            "issues": [],
            "mismatches": [],
        },
    )
    monkeypatch.setattr(
        tushare_daily,
        "_cleanup_preview_summary",
        lambda: {
            "status": "ok",
            "reclaimable_bytes": 0,
            "reclaimable_human": "0 B",
            "candidate_count": 0,
            "executable_count": 0,
            "blocked_count": 0,
            "by_kind": {},
        },
    )


def _write_minimal_qlib_index_artifacts(root: Path, *, latest: str = "2026-06-02") -> None:
    (root / "calendars").mkdir(parents=True, exist_ok=True)
    (root / "calendars" / "day.txt").write_text(f"{latest}\n", encoding="utf-8")
    tushare_daily._write_json(
        root / "index_converter_meta.json",
        {
            "price_mode": "index_raw_close_identity_adjusted",
            "change_field": "pct_chg_decimal",
            "factor_field": "constant_one_when_missing",
            "calendar_latest_date": latest,
        },
    )
    for code in ["000300sh", "000905sh", "000852sh", "000001sh", "399001sz", "399006sz", "000016sh"]:
        code_dir = root / "features" / code
        code_dir.mkdir(parents=True, exist_ok=True)
        (code_dir / "close.day.bin").write_bytes(b"qlib-index-close")


def _write_test_bin(path: Path, values: list[float], *, start: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = np.hstack([[float(start)], np.array(values, dtype=np.float32)]).astype("<f4")
    with open(path, "wb") as fh:
        payload.tofile(fh)


def _read_test_bin(path: Path) -> tuple[int, list[float]]:
    payload = np.fromfile(path, dtype="<f4")
    return int(payload[0]), payload[1:].astype(float).tolist()


def _raw_daily_rows_for_qlib_patch() -> pd.DataFrame:
    rows = []
    for code, base in [("000001.SZ", 10.0), ("000300.SH", 3000.0)]:
        for offset, date in enumerate(["2026-06-30", "2026-07-01", "2026-07-02"]):
            close = base + offset
            row = {
                "trade_date": pd.Timestamp(date),
                "code": code,
                "kline_time": pd.Timestamp(date),
                "open": close - 0.2,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": 100.0 + offset,
                "amount": 1000.0 + offset,
                "pre_close": close - 1.0,
                "pct_chg": 1.0,
                "amp": 2.0,
                "backward_factor": 2.0,
                "st_status": "NORMAL",
                "up_limit": close + 1.0,
                "down_limit": close - 1.0,
                "limit_source_kind": "official",
                "list_status": "I" if code == "000300.SH" else "L",
            }
            for source_col in set(QLIB_RAW_FIELD_MAP.values()):
                row.setdefault(source_col, 1.0)
            rows.append(row)
    return pd.DataFrame(rows).set_index("trade_date")


class _AutoTargetFakePro:
    def __init__(self, *, published_dates):
        self.published_dates = set(published_dates)

    def trade_cal(self, **kwargs):
        return pd.DataFrame(
            {
                "exchange": ["SSE", "SSE", "SSE"],
                "cal_date": ["20260603", "20260604", "20260605"],
                "is_open": [1, 1, 1],
                "pretrade_date": ["20260602", "20260603", "20260604"],
            }
        )

    def daily(self, **kwargs):
        trade_date = str(kwargs.get("trade_date"))
        if trade_date not in self.published_dates:
            return pd.DataFrame(columns=["ts_code", "trade_date"])
        return pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [trade_date]})

    def daily_basic(self, **kwargs):
        return self.daily(**kwargs)


class _PartialTodayFakePro(_AutoTargetFakePro):
    def daily(self, **kwargs):
        trade_date = str(kwargs.get("trade_date"))
        count = 1 if trade_date == "20260605" else 2 if trade_date == "20260604" else 0
        return pd.DataFrame(
            {"ts_code": [f"00000{idx + 1}.SZ" for idx in range(count)], "trade_date": [trade_date] * count}
        )


def test_tushare_auto_target_uses_today_when_daily_is_published(monkeypatch):
    monkeypatch.setattr(tushare_daily, "get_tushare_client", lambda network_mode="direct": _AutoTargetFakePro(published_dates={"20260605"}))
    monkeypatch.setattr(tushare_daily, "_expected_published_stock_count", lambda: 1)

    assert tushare_daily._tushare_auto_target_date(today="20260605") == "20260605"


def test_tushare_auto_target_falls_back_to_latest_published_trade_date(monkeypatch):
    monkeypatch.setattr(tushare_daily, "get_tushare_client", lambda network_mode="direct": _AutoTargetFakePro(published_dates={"20260604"}))
    monkeypatch.setattr(tushare_daily, "_expected_published_stock_count", lambda: 1)

    assert tushare_daily._tushare_auto_target_date(today="20260605") == "20260604"


def test_tushare_auto_target_rejects_partial_current_day_even_when_basic_matches(monkeypatch):
    monkeypatch.setattr(tushare_daily, "_expected_published_stock_count", lambda: 2)

    assert tushare_daily._tushare_auto_target_date(today="20260605", client=_PartialTodayFakePro(published_dates=set())) == "20260604"


def test_daily_preflight_marks_already_current(monkeypatch):
    monkeypatch.setattr(
        tushare_daily,
        "_require_tushare_production",
        lambda: {"source": "tushare", "latest_dates": {"hdf5": "2026-06-02"}},
    )
    monkeypatch.setattr(
        tushare_daily,
        "_disk_and_memory",
        lambda: {"disk_ok": True, "mem_ok": True},
    )
    monkeypatch.setattr(tushare_daily, "_promotion_idle_state", lambda: {"blockers": [], "quantgpt_health": {}, "processes": []})
    monkeypatch.setattr(tushare_daily, "tushare_network_preflight", lambda verify_http=True: {"status": "ok"})
    _patch_stability_ok(monkeypatch)

    result = tushare_daily.data_daily_preflight("20260602", for_promotion=False)

    assert result["status"] == "go"
    assert result["already_current"] is True
    assert result["replace_from_date"] == "20260602"
    assert result["selected_target_date"] == "20260602"


def test_daily_preflight_builds_tushare_window(monkeypatch):
    monkeypatch.setattr(
        tushare_daily,
        "_require_tushare_production",
        lambda: {"source": "tushare", "latest_dates": {"hdf5": "2026-06-02"}},
    )
    monkeypatch.setattr(
        tushare_daily,
        "_disk_and_memory",
        lambda: {"disk_ok": True, "mem_ok": True},
    )
    monkeypatch.setattr(tushare_daily, "_promotion_idle_state", lambda: {"blockers": [], "quantgpt_health": {}, "processes": []})
    monkeypatch.setattr(tushare_daily, "tushare_network_preflight", lambda verify_http=True: {"status": "ok"})
    _patch_stability_ok(monkeypatch)
    monkeypatch.setattr(
        tushare_daily,
        "tushare_preflight",
        lambda **kwargs: {
            "effective_target_date": "20260605",
            "selected_target_date": "20260605",
            "padded_start_date": "20251201",
            "trade_date_count": 3,
            "code_count": 5207,
        },
    )

    result = tushare_daily.data_daily_preflight("20260605", for_promotion=False)

    assert result["status"] == "go"
    assert result["already_current"] is False
    assert result["replace_from_date"] == "20260602"
    assert result["source_rebuild"]["start_date"] == "20260602"
    assert result["source_rebuild"]["trade_date_count"] == 3


def test_daily_merge_preserves_new_limit_price_columns(tmp_path):
    production_hdf = tmp_path / "production.h5"
    delta_hdf = tmp_path / "delta.h5"
    merged_hdf = tmp_path / "merged.h5"
    production = pd.DataFrame(
        {
            "code": ["000001.SZ"],
            "kline_time": [pd.Timestamp("2026-01-02")],
            "close": [10.0],
            "adj_close": [20.0],
            "pre_close": [float("nan")],
            "adj_pre_close": [float("nan")],
            "LIST_DATE": ["20200101"],
            "list_status": ["L"],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-02")], name="trade_date"),
    )
    delta = pd.DataFrame(
        {
            "code": ["000001.SZ"],
            "kline_time": [pd.Timestamp("2026-01-05")],
            "close": [10.5],
            "adj_close": [21.0],
            "pre_close": [10.0],
            "adj_pre_close": [20.0],
            "high": [11.55],
            "low": [9.45],
            "up_limit": [11.55],
            "down_limit": [9.45],
            "stk_limit_pre_close": [10.0],
            "LIST_DATE": ["20200101"],
            "list_status": ["L"],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-05")], name="trade_date"),
    )
    production.to_hdf(production_hdf, key="/daily", mode="w", format="table")
    delta.to_hdf(delta_hdf, key="/daily", mode="w", format="table")

    tushare_daily._merge_compat_hdf(
        production_hdf=production_hdf,
        delta_hdf=delta_hdf,
        output_hdf=merged_hdf,
        replace_from_date="20260105",
    )

    merged = pd.read_hdf(merged_hdf, key="/daily")
    assert "up_limit" in merged.columns
    assert "down_limit" in merged.columns
    assert "limit_source_kind" in merged.columns
    latest = merged[pd.to_datetime(merged["kline_time"]) == pd.Timestamp("2026-01-05")].iloc[0]
    assert latest["up_limit"] == 11.55
    assert latest["down_limit"] == 9.45
    assert latest["stk_limit_pre_close"] == 10.0
    assert latest["limit_source_kind"] == "official"


def test_daily_merge_aligns_legacy_numeric_dtypes_before_append(tmp_path):
    production_hdf = tmp_path / "production_dtype.h5"
    delta_hdf = tmp_path / "delta_dtype.h5"
    merged_hdf = tmp_path / "merged_dtype.h5"
    production = pd.DataFrame(
        {
            "code": ["000001.SZ"],
            "kline_time": [pd.Timestamp("2026-01-02")],
            "open": pd.Series([10], dtype="int64"),
            "high": pd.Series([11], dtype="int64"),
            "low": pd.Series([9], dtype="int64"),
            "close": pd.Series([10], dtype="int64"),
            "volume": pd.Series([100], dtype="int64"),
            "amount": pd.Series([1000], dtype="int64"),
            "adj_close": pd.Series([20], dtype="int64"),
            "pre_close": pd.Series([float("nan")], dtype="float32"),
            "adj_pre_close": pd.Series([float("nan")], dtype="float32"),
            "LIST_DATE": ["20200101"],
            "list_status": ["L"],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-02")], name="trade_date"),
    )
    delta = pd.DataFrame(
        {
            "code": ["000001.SZ"],
            "kline_time": [pd.Timestamp("2026-01-05")],
            "open": [10.2],
            "high": [11.55],
            "low": [9.45],
            "close": [10.5],
            "volume": [120.0],
            "amount": [1200.0],
            "adj_close": [21.0],
            "pre_close": [10.0],
            "adj_pre_close": [20.0],
            "up_limit": [11.55],
            "down_limit": [9.45],
            "stk_limit_pre_close": [10.0],
            "LIST_DATE": ["20200101"],
            "list_status": ["L"],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-05")], name="trade_date"),
    )
    production.to_hdf(production_hdf, key="/daily", mode="w", format="table")
    delta.to_hdf(delta_hdf, key="/daily", mode="w", format="table")

    result = tushare_daily._merge_compat_hdf(
        production_hdf=production_hdf,
        delta_hdf=delta_hdf,
        output_hdf=merged_hdf,
        replace_from_date="20260105",
    )

    merged = pd.read_hdf(merged_hdf, key="/daily")
    assert result["schema_alignment"]["status"] == "aligned"
    assert "up_limit" in merged.columns
    assert "down_limit" in merged.columns
    assert "stk_limit_pre_close" in merged.columns
    assert str(merged["open"].dtype) == "float64"
    assert str(merged["up_limit"].dtype) == "float64"
    assert str(merged["stk_limit_pre_close"].dtype) == "float64"
    assert merged.loc[pd.to_datetime(merged["kline_time"]) == pd.Timestamp("2026-01-05"), "up_limit"].iloc[0] == 11.55


def test_daily_merge_marks_listing_day_without_limit_price_as_structural(tmp_path):
    production_hdf = tmp_path / "production.h5"
    delta_hdf = tmp_path / "delta.h5"
    merged_hdf = tmp_path / "merged.h5"
    production = pd.DataFrame(
        {
            "code": ["688033.SH"],
            "kline_time": [pd.Timestamp("2019-07-19")],
            "close": [20.0],
            "adj_close": [20.0],
            "LIST_DATE": ["20190722"],
            "list_status": ["L"],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2019-07-19")], name="trade_date"),
    )
    delta = pd.DataFrame(
        {
            "code": ["688033.SH"],
            "kline_time": [pd.Timestamp("2019-07-22")],
            "close": [21.0],
            "adj_close": [21.0],
            "up_limit": [pd.NA],
            "down_limit": [pd.NA],
            "LIST_DATE": ["20190722"],
            "list_status": ["L"],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2019-07-22")], name="trade_date"),
    )
    production.to_hdf(production_hdf, key="/daily", mode="w", format="table", min_itemsize={"code": 16, "LIST_DATE": 16, "list_status": 8})
    delta.to_hdf(delta_hdf, key="/daily", mode="w", format="table", min_itemsize={"code": 16, "LIST_DATE": 16, "list_status": 8})

    tushare_daily._merge_compat_hdf(
        production_hdf=production_hdf,
        delta_hdf=delta_hdf,
        output_hdf=merged_hdf,
        replace_from_date="20190722",
    )

    merged = pd.read_hdf(merged_hdf, key="/daily")
    latest = merged[pd.to_datetime(merged["kline_time"]) == pd.Timestamp("2019-07-22")].iloc[0]
    assert latest["limit_source_kind"] == "structural_no_limit"


def test_daily_preflight_blocks_when_tushare_network_is_not_direct(monkeypatch):
    monkeypatch.setattr(
        tushare_daily,
        "_require_tushare_production",
        lambda: {"source": "tushare", "latest_dates": {"hdf5": "2026-06-02"}},
    )
    monkeypatch.setattr(tushare_daily, "_disk_and_memory", lambda: {"disk_ok": True, "mem_ok": True})
    monkeypatch.setattr(tushare_daily, "_promotion_idle_state", lambda: {"blockers": [], "quantgpt_health": {}, "processes": []})
    _patch_stability_ok(monkeypatch)
    monkeypatch.setattr(
        tushare_daily,
        "tushare_network_preflight",
        lambda verify_http=True: {
            "status": "failed",
            "issues": ["host_tushare_route_uses_proxy_tun:8.140.225.26"],
        },
    )

    result = tushare_daily.data_daily_preflight("20260605", for_promotion=False)

    assert result["status"] == "blocked"
    assert "tushare_network_not_direct" in result["blockers"]
    assert result["network"]["issues"] == ["host_tushare_route_uses_proxy_tun:8.140.225.26"]


def test_daily_preflight_blocks_when_partial_promote_detected(monkeypatch):
    monkeypatch.setattr(
        tushare_daily,
        "_require_tushare_production",
        lambda: {"source": "tushare", "latest_dates": {"hdf5": "2026-06-02"}},
    )
    monkeypatch.setattr(tushare_daily, "_disk_and_memory", lambda: {"disk_ok": True, "mem_ok": True})
    monkeypatch.setattr(tushare_daily, "_promotion_idle_state", lambda: {"blockers": [], "quantgpt_health": {}, "processes": []})
    monkeypatch.setattr(tushare_daily, "tushare_network_preflight", lambda verify_http=True: {"status": "ok"})
    _patch_stability_ok(monkeypatch)
    monkeypatch.setattr(
        tushare_daily,
        "production_consistency_status",
        lambda **kwargs: {
            "status": "failed",
            "partial_promote_detected": True,
            "issues": ["production_registry_actual_mismatch"],
            "mismatches": [{"surface": "qlib", "expected": "2026-06-02", "actual": "2026-06-03"}],
        },
    )

    result = tushare_daily.data_daily_preflight("20260605", for_promotion=False)

    assert result["status"] == "blocked"
    assert "partial_promote_detected" in result["blockers"]
    assert result["production_consistency"]["partial_promote_detected"] is True


def test_daily_preflight_blocks_auto_target_before_tushare_calendar_when_network_fails(monkeypatch):
    monkeypatch.setattr(
        tushare_daily,
        "_require_tushare_production",
        lambda: {"source": "tushare", "latest_dates": {"hdf5": "2026-06-02"}},
    )
    monkeypatch.setattr(tushare_daily, "_disk_and_memory", lambda: {"disk_ok": True, "mem_ok": True})
    monkeypatch.setattr(tushare_daily, "_promotion_idle_state", lambda: {"blockers": [], "quantgpt_health": {}, "processes": []})
    monkeypatch.setattr(tushare_daily, "tushare_network_preflight", lambda verify_http=True: {"status": "failed"})
    _patch_stability_ok(monkeypatch)
    monkeypatch.setattr(
        tushare_daily,
        "_tushare_auto_target_date",
        lambda: (_ for _ in ()).throw(AssertionError("auto target should not run when network is blocked")),
    )

    result = tushare_daily.data_daily_preflight("auto", for_promotion=False)

    assert result["status"] == "blocked"
    assert result["target_date"] == "auto"
    assert result["selected_target_date"] is None
    assert "tushare_network_not_direct" in result["blockers"]


def test_wsl_stability_preflight_requires_headless_wslconfig(monkeypatch, tmp_path):
    config = tmp_path / ".wslconfig"
    config.write_text("[wsl2]\nguiApplications=true\n", encoding="utf-8")
    monkeypatch.setattr(tushare_daily, "_is_wsl", lambda: True)

    result = tushare_daily._wsl_stability_preflight(wslconfig_paths=[config])

    assert result["status"] == "failed"
    assert result["gui_applications_disabled"] is False
    assert "wslg_gui_applications_enabled_for_headless_data_job" in result["issues"]

    config.write_text("[wsl2]\nguiApplications=false\n", encoding="utf-8")
    result = tushare_daily._wsl_stability_preflight(wslconfig_paths=[config])

    assert result["status"] == "ok"
    assert result["gui_applications_disabled"] is True


def test_wsl_stability_preflight_accepts_bom_wslconfig(monkeypatch, tmp_path):
    config = tmp_path / ".wslconfig"
    config.write_text("\ufeff[wsl2]\nguiApplications=false\n", encoding="utf-8")
    monkeypatch.setattr(tushare_daily, "_is_wsl", lambda: True)

    result = tushare_daily._wsl_stability_preflight(wslconfig_paths=[config])

    assert result["status"] == "ok"
    assert result["gui_applications_disabled"] is True


def test_daily_preflight_blocks_when_wslg_is_enabled(monkeypatch):
    monkeypatch.setattr(
        tushare_daily,
        "_require_tushare_production",
        lambda: {"source": "tushare", "latest_dates": {"hdf5": "2026-06-02"}},
    )
    monkeypatch.setattr(tushare_daily, "_disk_and_memory", lambda: {"disk_ok": True, "mem_ok": True})
    monkeypatch.setattr(tushare_daily, "_promotion_idle_state", lambda: {"blockers": [], "quantgpt_health": {}, "processes": []})
    monkeypatch.setattr(tushare_daily, "tushare_network_preflight", lambda verify_http=True: {"status": "ok"})
    monkeypatch.setattr(
        tushare_daily,
        "production_consistency_status",
        lambda **kwargs: {"status": "passed", "partial_promote_detected": False, "issues": [], "mismatches": []},
    )
    monkeypatch.setattr(tushare_daily, "_hdf_smoke_preflight", lambda: {"status": "ok", "path": "/tmp/smoke.h5"})
    monkeypatch.setattr(tushare_daily, "_cleanup_preview_summary", lambda: {"status": "ok"})
    monkeypatch.setattr(
        tushare_daily,
        "_wsl_stability_preflight",
        lambda: {
            "status": "failed",
            "is_wsl": True,
            "checked_paths": ["/mnt/c/Users/test/.wslconfig"],
            "gui_applications_disabled": False,
            "issues": ["wslg_gui_applications_enabled_for_headless_data_job"],
            "warnings": [],
        },
    )

    result = tushare_daily.data_daily_preflight("20260605", for_promotion=False)

    assert result["status"] == "blocked"
    assert "wslg_gui_applications_enabled_for_headless_data_job" in result["blockers"]
    assert result["stability"]["gui_applications_disabled"] is False


def test_daily_preflight_blocks_when_hdf_smoke_fails(monkeypatch):
    monkeypatch.setattr(
        tushare_daily,
        "_require_tushare_production",
        lambda: {"source": "tushare", "latest_dates": {"hdf5": "2026-06-02"}},
    )
    monkeypatch.setattr(tushare_daily, "_disk_and_memory", lambda: {"disk_ok": True, "mem_ok": True})
    monkeypatch.setattr(tushare_daily, "_promotion_idle_state", lambda: {"blockers": [], "quantgpt_health": {}, "processes": []})
    monkeypatch.setattr(tushare_daily, "tushare_network_preflight", lambda verify_http=True: {"status": "ok"})
    monkeypatch.setattr(
        tushare_daily,
        "production_consistency_status",
        lambda **kwargs: {"status": "passed", "partial_promote_detected": False, "issues": [], "mismatches": []},
    )
    monkeypatch.setattr(
        tushare_daily,
        "_wsl_stability_preflight",
        lambda: {
            "status": "ok",
            "issues": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        tushare_daily,
        "_hdf_smoke_preflight",
        lambda: {"status": "failed", "issue": "hdf_smoke_failed", "error": "cannot write"},
    )
    monkeypatch.setattr(tushare_daily, "_cleanup_preview_summary", lambda: {"status": "ok"})

    result = tushare_daily.data_daily_preflight("20260605", for_promotion=False)

    assert result["status"] == "blocked"
    assert "hdf_smoke_failed" in result["blockers"]
    assert result["stability"]["hdf_smoke"]["status"] == "failed"


def test_stage_update_dry_run_reports_new_tushare_daily_package(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tushare_daily,
        "data_daily_preflight",
        lambda target_date=None, for_promotion=False: {
            "status": "go",
            "already_current": False,
            "selected_target_date": "20260605",
        },
    )
    monkeypatch.setattr(tushare_daily, "STAGING_ROOT", tmp_path)

    result = tushare_daily.data_stage_update("20260605", dry_run=True)

    assert result["status"] == "dry_run"
    assert result["package_id"].startswith("tushare-daily-")
    assert result["source_package_id"].endswith("-source")


def test_daily_routine_reports_post_promote_cleanup(monkeypatch):
    preflight = {
        "status": "go",
        "already_current": False,
        "selected_target_date": "20260605",
    }
    stage = {
        "status": "completed",
        "package_id": "tushare-daily-test-target-20260605",
    }
    promote = {"status": "promoted", "promotion_id": "promote-test"}
    cleanup = {
        "preview": {"status": "completed", "report_path": "/tmp/cleanup.json", "reclaimable_bytes": 0},
        "execute": None,
        "execute_policy": {"profile": "safe", "eligible": False},
    }
    captured = {}
    monkeypatch.setattr(tushare_daily, "data_daily_preflight", lambda target_date=None, for_promotion=False: preflight)

    def fake_stage_update(target_date=None, dry_run=False, _validated_preflight=None):
        captured["validated_preflight"] = _validated_preflight
        return stage

    monkeypatch.setattr(tushare_daily, "data_stage_update", fake_stage_update)
    monkeypatch.setattr(tushare_daily, "data_promote_staged", lambda **kwargs: promote)
    monkeypatch.setattr(tushare_daily, "_post_promote_cleanup", lambda: cleanup)
    monkeypatch.setattr(tushare_daily, "production_audit_summary", lambda **kwargs: {"status": "passed"})
    monkeypatch.setattr(tushare_daily, "record_production_audit_result", lambda result: {"status": result["status"]})

    result = tushare_daily.data_daily_routine(target_date="20260605")

    assert result["status"] == "completed"
    assert result["post_promote_audit"]["status"] == "passed"
    assert result["post_promote_cleanup_preview"]["report_path"] == "/tmp/cleanup.json"
    assert result["post_promote_cleanup_execute"] is None
    assert result["post_promote_cleanup_policy"]["profile"] == "safe"
    assert result["post_promote_cleanup_preview_report_path"] == "/tmp/cleanup.json"
    assert result["post_promote_cleanup_reclaimed_bytes"] == 0
    assert captured["validated_preflight"] is preflight


def test_memory_headroom_wait_requires_stable_samples(monkeypatch):
    reports = iter(
        [
            {"disk_ok": True, "mem_ok": False, "mem_available_bytes": 1},
            {"disk_ok": True, "mem_ok": True, "mem_available_bytes": 9},
            {"disk_ok": True, "mem_ok": True, "mem_available_bytes": 10},
        ]
    )
    monkeypatch.setattr(tushare_daily, "_disk_and_memory", lambda: next(reports))
    monkeypatch.setattr(tushare_daily.time, "sleep", lambda seconds: None)

    result = tushare_daily._wait_for_memory_headroom(timeout_seconds=60, sample_seconds=0, stable_samples=2)

    assert result["status"] == "ready"
    assert result["sample_count"] == 3
    assert result["stable_sample_count"] == 2
    assert result["resources"]["mem_available_bytes"] == 10


def test_daily_preflight_samples_resources_before_production_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tushare_daily,
        "_disk_and_memory",
        lambda: calls.append("resources") or {"disk_ok": True, "mem_ok": True},
    )

    def fail_after_resource_sample():
        calls.append("production")
        raise RuntimeError("stop_after_order_check")

    monkeypatch.setattr(tushare_daily, "_require_tushare_production", fail_after_resource_sample)

    with pytest.raises(RuntimeError, match="stop_after_order_check"):
        tushare_daily.data_daily_preflight("20260731", for_promotion=False)

    assert calls == ["resources", "production"]


def test_daily_routine_propagates_post_promote_audit_failure(monkeypatch):
    monkeypatch.setattr(
        tushare_daily,
        "data_daily_preflight",
        lambda target_date=None, for_promotion=False: {
            "status": "go",
            "already_current": False,
            "selected_target_date": "20260710",
            "replace_from_date": "20260709",
        },
    )
    monkeypatch.setattr(
        tushare_daily,
        "data_stage_update",
        lambda target_date=None, dry_run=False, _validated_preflight=None: {
            "status": "completed",
            "package_id": "daily-test",
        },
    )
    monkeypatch.setattr(tushare_daily, "data_promote_staged", lambda **kwargs: {"status": "promoted"})
    monkeypatch.setattr(
        tushare_daily,
        "_post_promote_cleanup",
        lambda: {"preview": {"reclaimable_bytes": 0}, "execute": None, "execute_policy": {}},
    )
    monkeypatch.setattr(
        tushare_daily,
        "production_audit_summary",
        lambda **kwargs: {"status": "failed", "issues": ["sample_cross_surface_mismatch"]},
    )
    monkeypatch.setattr(
        tushare_daily,
        "record_production_audit_result",
        lambda result: {"status": "failed", "issues": result["issues"]},
    )

    result = tushare_daily.data_daily_routine(target_date="20260710")

    assert result["status"] == "promoted_audit_failed"
    assert result["production_audit_gate"]["status"] == "failed"


def test_record_production_audit_failure_closes_consumer_gate(monkeypatch, tmp_path):
    current_path = tmp_path / "CURRENT_PRODUCTION_DATASET.json"
    latest_path = tmp_path / "latest_status.json"
    daily_path = tmp_path / "daily_update_status.json"
    current_path.write_text(
        '{"production_package_id":"pkg","promotion_id":"promote","latest_trade_date":"2026-07-10",'
        '"consumer_readiness":{"qlib_model_training":true,"quantgpt_factor_mining":true}}',
        encoding="utf-8",
    )
    latest_path.write_text(
        '{"status":"completed","snapshot":{"consumer_readiness":{"qlib_model_training":true}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(tushare_daily, "CURRENT_PRODUCTION_DATASET_FILE", current_path)
    monkeypatch.setattr(tushare_daily, "LATEST_STATUS_FILE", latest_path)
    monkeypatch.setattr(tushare_daily, "DAILY_STATUS_FILE", daily_path)
    monkeypatch.setattr(ops_common, "DAILY_STATUS_FILE", daily_path)
    monkeypatch.setattr(
        tushare_daily,
        "_build_snapshot",
        lambda **kwargs: {"consumer_readiness": {"qlib_model_training": True}},
    )
    monkeypatch.setattr(tushare_daily, "_qlib_index_readiness", lambda *args, **kwargs: {"status": "passed"})

    summary = tushare_daily.record_production_audit_result(
        {
            "status": "failed",
            "generated_at": "2026-07-11T04:25:39",
            "production_package_id": "pkg",
            "latest_trade_date": "2026-07-10",
            "issues": ["sample_cross_surface_mismatch"],
        }
    )

    current = tushare_daily._read_json(current_path)
    latest = tushare_daily._read_json(latest_path)
    assert summary["status"] == "failed"
    assert current["consumer_readiness_gate"] == "blocked_by_production_audit"
    assert not any(current["consumer_readiness"].values())
    assert current["artifact_readiness"]["qlib_model_training"] is True
    assert latest["status"] == "production_audit_failed"
    assert latest["consumer_readiness_gate"] == "blocked_by_production_audit"
    assert latest["snapshot"]["consumer_readiness_gate"] == "blocked_by_production_audit"


def test_record_production_audit_rolls_back_both_state_files_on_write_failure(monkeypatch, tmp_path):
    current_path = tmp_path / "CURRENT_PRODUCTION_DATASET.json"
    latest_path = tmp_path / "latest_status.json"
    daily_path = tmp_path / "daily_update_status.json"
    current_path.write_text(
        '{"production_package_id":"pkg","latest_trade_date":"2026-07-10","consumer_readiness":{}}',
        encoding="utf-8",
    )
    latest_path.write_text('{"status":"old","snapshot":{}}', encoding="utf-8")
    daily_path.write_text('{"status":"old-daily"}', encoding="utf-8")
    before_current = current_path.read_bytes()
    before_latest = latest_path.read_bytes()
    monkeypatch.setattr(tushare_daily, "CURRENT_PRODUCTION_DATASET_FILE", current_path)
    monkeypatch.setattr(tushare_daily, "LATEST_STATUS_FILE", latest_path)
    monkeypatch.setattr(tushare_daily, "DAILY_STATUS_FILE", daily_path)
    monkeypatch.setattr(ops_common, "DAILY_STATUS_FILE", daily_path)
    monkeypatch.setattr(tushare_daily, "_build_snapshot", lambda **kwargs: {})
    monkeypatch.setattr(tushare_daily, "_qlib_index_readiness", lambda *args, **kwargs: {"status": "passed"})
    original_atomic_write = tushare_daily.atomic_write_json

    def fail_latest(path, payload):
        if Path(path) == latest_path:
            raise OSError("latest_write_failed")
        return original_atomic_write(path, payload)

    monkeypatch.setattr(tushare_daily, "atomic_write_json", fail_latest)

    with pytest.raises(OSError, match="latest_write_failed"):
        tushare_daily.record_production_audit_result(
            {
                "status": "passed",
                "production_package_id": "pkg",
                "latest_trade_date": "2026-07-10",
                "issues": [],
            }
        )

    assert current_path.read_bytes() == before_current
    assert latest_path.read_bytes() == before_latest
    assert tushare_daily._read_json(daily_path)["status"] == "old-daily"


def test_post_promote_cleanup_requires_explicit_operator_approval(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_run_cleanup(*, profile, execute, write_report):
        calls.append({"profile": profile, "execute": execute, "write_report": write_report})
        if execute:
            return {
                "report_path": "/tmp/cleanup-execute.json",
                "deleted_count": 3,
                "deleted_bytes": 31 * 1024**3,
                "deleted_human": "31.0 GB",
                "errors": [],
            }
        return {
            "report_path": "/tmp/cleanup-preview.json",
            "summary": {
                "reclaimable_bytes": 31 * 1024**3,
                "reclaimable_human": "31.0 GB",
                "candidate_count": 3,
                "executable_count": 3,
                "blocked_count": 0,
            },
        }

    monkeypatch.setattr(tushare_daily, "run_cleanup", fake_run_cleanup)

    result = tushare_daily._post_promote_cleanup()

    assert result["execute_policy"]["eligible"] is False
    assert result["execute_policy"]["trigger"] == "explicit_operator_approval_required"
    assert result["execute"] is None
    assert [call["execute"] for call in calls] == [False]


def test_stage_update_dry_run_reuses_matching_existing_package(monkeypatch, tmp_path):
    preflight = {
        "status": "go",
        "already_current": False,
        "target_date": "20260605",
        "selected_target_date": "20260605",
        "effective_target_date": "20260605",
        "replace_from_date": "20260602",
    }
    monkeypatch.setattr(tushare_daily, "data_daily_preflight", lambda target_date=None, for_promotion=False: preflight)
    monkeypatch.setattr(tushare_daily, "STAGING_ROOT", tmp_path)
    root = tmp_path / "tushare-daily-existing-target-20260605"
    root.mkdir(parents=True)
    tushare_daily._write_json(
        root / "manifest.json",
        {
            "package_id": "tushare-daily-existing-target-20260605",
            "status": "failed",
            "source": "tushare",
            "package_kind": "daily_update",
            "target_date": "20260605",
            "selected_target_date": "20260605",
            "effective_target_date": "20260605",
            "replace_from_date": "20260602",
            "source_package_id": "tushare-daily-existing-target-20260605-source",
        },
    )

    result = tushare_daily.data_stage_update("20260605", dry_run=True)

    assert result["status"] == "dry_run"
    assert result["package_id"] == "tushare-daily-existing-target-20260605"
    assert result["source_package_id"] == "tushare-daily-existing-target-20260605-source"
    assert result["reused_existing_package"] is True
    assert result["existing_status"] == "failed"


def test_stage_update_dry_run_does_not_reuse_mismatched_boundary_package(monkeypatch, tmp_path):
    preflight = {
        "status": "go",
        "already_current": False,
        "target_date": "20260605",
        "selected_target_date": "20260605",
        "effective_target_date": "20260605",
        "replace_from_date": "20260602",
    }
    monkeypatch.setattr(tushare_daily, "data_daily_preflight", lambda target_date=None, for_promotion=False: preflight)
    monkeypatch.setattr(tushare_daily, "STAGING_ROOT", tmp_path)
    root = tmp_path / "tushare-daily-old-boundary-target-20260605"
    root.mkdir(parents=True)
    tushare_daily._write_json(
        root / "manifest.json",
        {
            "package_id": "tushare-daily-old-boundary-target-20260605",
            "status": "failed",
            "source": "tushare",
            "package_kind": "daily_update",
            "target_date": "20260605",
            "selected_target_date": "20260605",
            "effective_target_date": "20260605",
            "replace_from_date": "20260601",
            "source_package_id": "tushare-daily-old-boundary-target-20260605-source",
        },
    )

    result = tushare_daily.data_stage_update("20260605", dry_run=True)

    assert result["status"] == "dry_run"
    assert result["package_id"] != "tushare-daily-old-boundary-target-20260605"
    assert result["reused_existing_package"] is False


def test_stage_update_returns_completed_reused_package_without_rebuild(monkeypatch, tmp_path):
    preflight = {
        "status": "go",
        "already_current": False,
        "target_date": "20260605",
        "selected_target_date": "20260605",
        "effective_target_date": "20260605",
        "replace_from_date": "20260602",
    }
    monkeypatch.setattr(tushare_daily, "data_daily_preflight", lambda target_date=None, for_promotion=False: preflight)
    monkeypatch.setattr(tushare_daily, "STAGING_ROOT", tmp_path)
    root = tmp_path / "tushare-daily-completed-target-20260605"
    root.mkdir(parents=True)
    tushare_daily._write_json(
        root / "manifest.json",
        {
            "package_id": "tushare-daily-completed-target-20260605",
            "status": "completed",
            "source": "tushare",
            "package_kind": "daily_update",
            "target_date": "20260605",
            "selected_target_date": "20260605",
            "effective_target_date": "20260605",
            "replace_from_date": "20260602",
            "source_package_id": "tushare-daily-completed-target-20260605-source",
        },
    )

    result = tushare_daily.data_stage_update("20260605", dry_run=False)

    assert result["status"] == "completed"
    assert result["package_id"] == "tushare-daily-completed-target-20260605"
    assert result["reused_existing_package"] is True
    assert result["current_stage"] == "completed"
    assert result["stage_summary"]["current_stage"] == "completed"


def test_stage_update_reuses_existing_source_package_for_resume(monkeypatch, tmp_path):
    preflight = {
        "status": "go",
        "already_current": False,
        "target_date": "20260605",
        "selected_target_date": "20260605",
        "effective_target_date": "20260605",
        "replace_from_date": "20260602",
        "current_latest_trade_date": "20260602",
    }
    monkeypatch.setattr(tushare_daily, "data_daily_preflight", lambda target_date=None, for_promotion=False: preflight)
    monkeypatch.setattr(tushare_daily, "STAGING_ROOT", tmp_path)
    monkeypatch.setattr(tushare_daily, "_require_tushare_production", lambda: {"source": "tushare"})
    root = tmp_path / "tushare-daily-resume-target-20260605"
    root.mkdir(parents=True)
    tushare_daily._write_json(
        root / "manifest.json",
        {
            "package_id": "tushare-daily-resume-target-20260605",
            "status": "failed",
            "source": "tushare",
            "package_kind": "daily_update",
            "target_date": "20260605",
            "selected_target_date": "20260605",
            "effective_target_date": "20260605",
            "replace_from_date": "20260602",
            "source_package_id": "tushare-daily-resume-target-20260605-source",
        },
    )
    captured = {}

    def fake_rebuild(config):
        captured["package_id"] = config.package_id
        return {"status": "failed"}

    monkeypatch.setattr(tushare_daily, "tushare_full_rebuild", fake_rebuild)

    result = tushare_daily.data_stage_update("20260605", dry_run=False)

    assert captured["package_id"] == "tushare-daily-resume-target-20260605-source"
    assert result["status"] == "failed"


def test_stage_update_dry_run_marks_stale_source_rebuild_interrupted_resumable(monkeypatch, tmp_path):
    preflight = {
        "status": "go",
        "already_current": False,
        "target_date": "20260605",
        "selected_target_date": "20260605",
        "effective_target_date": "20260605",
        "replace_from_date": "20260602",
    }
    monkeypatch.setattr(tushare_daily, "data_daily_preflight", lambda target_date=None, for_promotion=False: preflight)
    monkeypatch.setattr(tushare_daily, "STAGING_ROOT", tmp_path)
    monkeypatch.setattr(tushare_daily, "_has_active_data_process", lambda: False)
    root = tmp_path / "tushare-daily-stale-target-20260605"
    source_root = tmp_path / "tushare-daily-stale-target-20260605-source"
    root.mkdir(parents=True)
    source_root.mkdir(parents=True)
    tushare_daily._write_json(
        root / "manifest.json",
        {
            "package_id": "tushare-daily-stale-target-20260605",
            "status": "stage_in_progress",
            "source": "tushare",
            "package_kind": "daily_update",
            "target_date": "20260605",
            "selected_target_date": "20260605",
            "effective_target_date": "20260605",
            "replace_from_date": "20260602",
            "source_package_id": "tushare-daily-stale-target-20260605-source",
            "current_stage": "source_rebuild",
        },
    )
    tushare_daily._write_json(
        source_root / "full_rebuild_progress.json",
        {"status": "running", "updated_at": "2000-01-01T00:00:00", "current_stage": "cyq_perf"},
    )

    result = tushare_daily.data_stage_update("20260605", dry_run=True)

    assert result["reused_existing_package"] is True
    assert result["existing_status"] == "interrupted_resumable"
    assert result["existing_interrupted_reason"] == "source_rebuild_progress_stale_no_active_process"
    assert result["source_progress_summary"]["stale"] is True


def test_stage_update_reuses_interrupted_resumable_package_for_resume(monkeypatch, tmp_path):
    preflight = {
        "status": "go",
        "already_current": False,
        "target_date": "20260605",
        "selected_target_date": "20260605",
        "effective_target_date": "20260605",
        "replace_from_date": "20260602",
        "current_latest_trade_date": "20260602",
    }
    monkeypatch.setattr(tushare_daily, "data_daily_preflight", lambda target_date=None, for_promotion=False: preflight)
    monkeypatch.setattr(tushare_daily, "STAGING_ROOT", tmp_path)
    monkeypatch.setattr(tushare_daily, "_require_tushare_production", lambda: {"source": "tushare"})
    root = tmp_path / "tushare-daily-interrupted-target-20260605"
    root.mkdir(parents=True)
    tushare_daily._write_json(
        root / "manifest.json",
        {
            "package_id": "tushare-daily-interrupted-target-20260605",
            "status": "interrupted_resumable",
            "source": "tushare",
            "package_kind": "daily_update",
            "target_date": "20260605",
            "selected_target_date": "20260605",
            "effective_target_date": "20260605",
            "replace_from_date": "20260602",
            "source_package_id": "tushare-daily-interrupted-target-20260605-source",
        },
    )
    captured = {}

    def fake_rebuild(config):
        captured["package_id"] = config.package_id
        return {"status": "failed"}

    monkeypatch.setattr(tushare_daily, "tushare_full_rebuild", fake_rebuild)

    result = tushare_daily.data_stage_update("20260605", dry_run=False)

    assert captured["package_id"] == "tushare-daily-interrupted-target-20260605-source"
    assert result["status"] == "failed"
    assert result["resume_reason"] == "interrupted_resumable"


def test_write_daily_manifest_enriches_status_with_stage_summary(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(tushare_daily, "_write_daily_status", lambda payload: captured.update(payload))

    payload = {
        "package_id": "tushare-daily-stage-target-20260605",
        "status": "stage_in_progress",
        "source": "tushare",
        "package_kind": "daily_update",
        "target_date": "20260605",
        "selected_target_date": "20260605",
        "effective_target_date": "20260605",
        "replace_from_date": "20260602",
        "source_package_id": "tushare-daily-stage-target-20260605-source",
        "package_root": str(tmp_path / "pkg"),
        "reused_existing_package": True,
        "source_rebuild": {"status": "completed"},
        "current_stage": "merge_production_hdf",
    }

    enriched = tushare_daily._write_daily_manifest(tmp_path / "pkg", payload)

    assert enriched["current_stage"] == "merge_production_hdf"
    assert enriched["stage_summary"]["current_stage"] == "merge_production_hdf"
    assert enriched["stage_summary"]["completed_stages"] == ["source_rebuild"]
    assert captured["latest_stage"]["source_package_id"] == "tushare-daily-stage-target-20260605-source"
    assert captured["latest_stage"]["reused_existing_package"] is True


def test_merge_compat_hdf_replaces_rows_from_boundary(tmp_path):
    prod = tmp_path / "prod.h5"
    delta = tmp_path / "delta.h5"
    out = tmp_path / "merged.h5"

    prod_df = pd.DataFrame(
        [
            {"trade_date": pd.Timestamp("2026-06-01"), "kline_time": pd.Timestamp("2026-06-01"), "code": "600000.SH", "close": 10.0},
            {"trade_date": pd.Timestamp("2026-06-02"), "kline_time": pd.Timestamp("2026-06-02"), "code": "600000.SH", "close": 11.0},
        ]
    ).set_index("trade_date")
    delta_df = pd.DataFrame(
        [
            {"trade_date": pd.Timestamp("2026-06-02"), "kline_time": pd.Timestamp("2026-06-02"), "code": "600000.SH", "close": 12.0},
            {"trade_date": pd.Timestamp("2026-06-03"), "kline_time": pd.Timestamp("2026-06-03"), "code": "600000.SH", "close": 13.0},
        ]
    ).set_index("trade_date")
    prod_df.to_hdf(prod, key="/daily", mode="w", format="table")
    delta_df.to_hdf(delta, key="/daily", mode="w", format="table")

    tushare_daily._merge_compat_hdf(
        production_hdf=prod,
        delta_hdf=delta,
        output_hdf=out,
        replace_from_date="20260602",
        chunk_rows=10,
    )

    merged = pd.read_hdf(out, key="/daily").reset_index()
    assert merged["close"].tolist() == [10.0, 12.0, 13.0]


def test_merge_compat_hdf_does_not_clobber_existing_output_on_failure(monkeypatch, tmp_path):
    prod = tmp_path / "prod.h5"
    delta = tmp_path / "delta.h5"
    out = tmp_path / "merged.h5"

    prod_df = pd.DataFrame(
        [{"trade_date": pd.Timestamp("2026-06-01"), "kline_time": pd.Timestamp("2026-06-01"), "code": "600000.SH", "close": 10.0}]
    ).set_index("trade_date")
    delta_df = pd.DataFrame(
        [{"trade_date": pd.Timestamp("2026-06-02"), "kline_time": pd.Timestamp("2026-06-02"), "code": "600000.SH", "close": 12.0}]
    ).set_index("trade_date")
    existing_df = pd.DataFrame(
        [{"trade_date": pd.Timestamp("2026-05-31"), "kline_time": pd.Timestamp("2026-05-31"), "code": "600000.SH", "close": 99.0}]
    ).set_index("trade_date")
    prod_df.to_hdf(prod, key="/daily", mode="w", format="table")
    delta_df.to_hdf(delta, key="/daily", mode="w", format="table")
    existing_df.to_hdf(out, key="/daily", mode="w", format="table")

    real_append = tushare_daily._append_hdf
    calls = {"count": 0}

    def flaky_append(*args, **kwargs):
        calls["count"] += 1
        real_append(*args, **kwargs)
        if calls["count"] == 1:
            raise RuntimeError("simulated_merge_interrupt")

    monkeypatch.setattr(tushare_daily, "_append_hdf", flaky_append)

    with pytest.raises(RuntimeError, match="simulated_merge_interrupt"):
        tushare_daily._merge_compat_hdf(
            production_hdf=prod,
            delta_hdf=delta,
            output_hdf=out,
            replace_from_date="20260602",
            chunk_rows=10,
        )

    existing = pd.read_hdf(out, key="/daily").reset_index()
    assert existing["close"].tolist() == [99.0]


def test_merge_compat_hdf_replaces_truncated_existing_output(tmp_path):
    prod = tmp_path / "prod.h5"
    delta = tmp_path / "delta.h5"
    out = tmp_path / "merged.h5"

    prod_df = pd.DataFrame(
        [{"trade_date": pd.Timestamp("2026-06-01"), "kline_time": pd.Timestamp("2026-06-01"), "code": "600000.SH", "close": 10.0}]
    ).set_index("trade_date")
    delta_df = pd.DataFrame(
        [{"trade_date": pd.Timestamp("2026-06-02"), "kline_time": pd.Timestamp("2026-06-02"), "code": "600000.SH", "close": 12.0}]
    ).set_index("trade_date")
    prod_df.to_hdf(prod, key="/daily", mode="w", format="table")
    delta_df.to_hdf(delta, key="/daily", mode="w", format="table")
    out.write_bytes(b"truncated hdf")

    result = tushare_daily._merge_compat_hdf(
        production_hdf=prod,
        delta_hdf=delta,
        output_hdf=out,
        replace_from_date="20260602",
        chunk_rows=10,
    )

    merged = pd.read_hdf(out, key="/daily").reset_index()
    assert merged["close"].tolist() == [10.0, 12.0]
    assert result["final_rows"] == 2
    assert not list(tmp_path.glob(".merged.h5.tmp-*"))


def test_merge_compat_hdf_preserves_preboundary_rows_for_late_sorted_codes(tmp_path):
    prod = tmp_path / "prod_index.h5"
    delta = tmp_path / "delta_index.h5"
    out = tmp_path / "merged_index.h5"

    prod_df = pd.DataFrame(
        [
            {"trade_date": pd.Timestamp("2026-06-15"), "kline_time": pd.Timestamp("2026-06-15"), "code": "000001.SH", "close": 10.0, "adj_close": 10.0, "pre_close": 9.5, "adj_pre_close": 9.5},
            {"trade_date": pd.Timestamp("2026-06-16"), "kline_time": pd.Timestamp("2026-06-16"), "code": "000001.SH", "close": 10.5, "adj_close": 10.5, "pre_close": 10.0, "adj_pre_close": 10.0},
            {"trade_date": pd.Timestamp("2026-06-15"), "kline_time": pd.Timestamp("2026-06-15"), "code": "000300.SH", "close": 20.0, "adj_close": 20.0, "pre_close": 19.5, "adj_pre_close": 19.5},
            {"trade_date": pd.Timestamp("2026-06-16"), "kline_time": pd.Timestamp("2026-06-16"), "code": "000300.SH", "close": 20.5, "adj_close": 20.5, "pre_close": 20.0, "adj_pre_close": 20.0},
        ]
    ).set_index("trade_date")
    delta_df = pd.DataFrame(
        [
            {"trade_date": pd.Timestamp("2026-06-16"), "kline_time": pd.Timestamp("2026-06-16"), "code": "000001.SH", "close": 11.0, "adj_close": 11.0, "pre_close": None, "adj_pre_close": None},
            {"trade_date": pd.Timestamp("2026-06-17"), "kline_time": pd.Timestamp("2026-06-17"), "code": "000001.SH", "close": 11.5, "adj_close": 11.5, "pre_close": 11.0, "adj_pre_close": 11.0},
            {"trade_date": pd.Timestamp("2026-06-16"), "kline_time": pd.Timestamp("2026-06-16"), "code": "000300.SH", "close": 21.0, "adj_close": 21.0, "pre_close": None, "adj_pre_close": None},
            {"trade_date": pd.Timestamp("2026-06-17"), "kline_time": pd.Timestamp("2026-06-17"), "code": "000300.SH", "close": 21.5, "adj_close": 21.5, "pre_close": 21.0, "adj_pre_close": 21.0},
        ]
    ).set_index("trade_date")
    prod_df.to_hdf(prod, key="/daily", mode="w", format="table")
    delta_df.to_hdf(delta, key="/daily", mode="w", format="table")

    result = tushare_daily._merge_compat_hdf(
        production_hdf=prod,
        delta_hdf=delta,
        output_hdf=out,
        replace_from_date="20260616",
        chunk_rows=10,
    )

    merged = pd.read_hdf(out, key="/daily").reset_index()
    merged["kline_time"] = pd.to_datetime(merged["kline_time"])
    preserved = merged[(merged["code"] == "000300.SH") & (merged["kline_time"] == pd.Timestamp("2026-06-15"))]
    boundary = merged[(merged["code"] == "000300.SH") & (merged["kline_time"] == pd.Timestamp("2026-06-16"))]

    assert result["preserved_rows"] == 2
    assert result["removed_rows"] == 2
    assert result["schema_alignment"]["status"] == "aligned"
    assert len(preserved) == 1
    assert float(boundary["pre_close"].iloc[0]) == 20.0
    assert float(boundary["adj_pre_close"].iloc[0]) == 20.0


def test_merge_compat_hdf_aligns_legacy_date_columns(tmp_path):
    prod = tmp_path / "prod_schema.h5"
    delta = tmp_path / "delta_schema.h5"
    out = tmp_path / "merged_schema.h5"

    prod_df = pd.DataFrame(
        [
            {
                "trade_date": pd.Timestamp("2026-06-01"),
                "kline_time": pd.Timestamp("2026-06-01"),
                "code": "600000.SH",
                "close": 10.0,
                "LIST_DATE": "19991110",
                "list_date": "19991110",
                "delist_date": None,
            }
        ]
    ).set_index("trade_date")
    delta_df = pd.DataFrame(
        [
            {
                "trade_date": pd.Timestamp("2026-06-02"),
                "kline_time": pd.Timestamp("2026-06-02"),
                "code": "600000.SH",
                "close": 11.0,
                "LIST_DATE": "19991110",
            }
        ]
    ).set_index("trade_date")
    prod_df.to_hdf(prod, key="/daily", mode="w", format="table", min_itemsize={"code": 16, "LIST_DATE": 16, "list_date": 16, "delist_date": 16})
    delta_df.to_hdf(delta, key="/daily", mode="w", format="table", min_itemsize={"code": 16, "LIST_DATE": 16})

    result = tushare_daily._merge_compat_hdf(
        production_hdf=prod,
        delta_hdf=delta,
        output_hdf=out,
        replace_from_date="20260602",
        chunk_rows=10,
    )

    merged = pd.read_hdf(out, key="/daily")
    assert "list_date" in merged.columns
    assert "delist_date" in merged.columns
    assert result["schema_alignment"]["delta_missing_columns_filled"] == ["delist_date"]
    assert merged.loc[merged["kline_time"] == pd.Timestamp("2026-06-02"), "list_date"].iloc[0] == "19991110"


def test_production_audit_summary_checks_hdf_quality_and_latest_alignment(monkeypatch, tmp_path):
    hdf = tmp_path / "production.h5"
    current_path = tmp_path / "CURRENT_PRODUCTION_DATASET.json"
    latest_status_path = tmp_path / "latest_status.json"
    quality_path = tmp_path / "quality_report.json"
    raw_quality_path = tmp_path / "raw_quality_report.json"
    qlib_stock_meta_path = tmp_path / "stock_converter_meta.json"
    qlib_index_meta_path = tmp_path / "index_converter_meta.json"
    frame = pd.DataFrame(
        [
            {
                "trade_date": pd.Timestamp("2026-06-01"),
                "kline_time": pd.Timestamp("2026-06-01"),
                "code": "600000.SH",
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "volume": 100.0,
                "amount": 1000.0,
                "pre_close": 10.0,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "LIST_DATE": "19991110",
                "list_date": "19991110",
                "delist_date": "",
            },
            {
                "trade_date": pd.Timestamp("2026-06-02"),
                "kline_time": pd.Timestamp("2026-06-02"),
                "code": "600000.SH",
                "open": 10.5,
                "high": 11.5,
                "low": 10.0,
                "close": 11.0,
                "volume": 120.0,
                "amount": 1300.0,
                "pre_close": 10.5,
                "up_limit": 11.55,
                "down_limit": 9.45,
                "LIST_DATE": "19991110",
                "list_date": "19991110",
                "delist_date": "",
            },
        ]
    ).set_index("trade_date")
    frame.to_hdf(hdf, key="/daily", mode="w", format="table", min_itemsize={"code": 16, "LIST_DATE": 16, "list_date": 16, "delist_date": 16})
    tushare_daily._write_json(
        current_path,
        {
            "source": "tushare",
            "production_package_id": "pkg",
            "latest_trade_date": "2026-06-02",
            "latest_dates": {"hdf5": "2026-06-02", "qlib": "2026-06-02", "quantgpt": "2026-06-02"},
        },
    )
    tushare_daily._write_json(
        latest_status_path,
        {
            "snapshot": {
                "latest_hdf5_trade_date": "2026-06-02",
                "latest_qlib_trade_date": "2026-06-02",
                "latest_quantgpt_trade_date": "2026-06-02",
            }
        },
    )
    tushare_daily._write_json(quality_path, {"passed": True})
    tushare_daily._write_json(raw_quality_path, {"passed": True})
    tushare_daily._write_json(
        qlib_stock_meta_path,
        {
            "price_mode": "adjusted_ohlc_plus_factor_for_qlib_exchange",
            "raw_price_fields_retained": True,
            "valid_field_count": 37,
        },
    )
    tushare_daily._write_json(
        qlib_index_meta_path,
        {
            "price_mode": "index_raw_close_identity_adjusted",
            "change_field": "pct_chg_decimal",
            "factor_field": "constant_one_when_missing",
        },
    )
    _write_minimal_qlib_index_artifacts(tmp_path)
    monkeypatch.setattr(tushare_daily, "PRODUCTION_RAW_HDF5", hdf)
    monkeypatch.setattr(tushare_daily, "CURRENT_PRODUCTION_DATASET_FILE", current_path)
    monkeypatch.setattr(tushare_daily, "LATEST_STATUS_FILE", latest_status_path)
    monkeypatch.setattr(tushare_daily, "PRODUCTION_QUALITY_FILE", quality_path)
    monkeypatch.setattr(tushare_daily, "PRODUCTION_RAW_QUALITY_FILE", raw_quality_path)
    monkeypatch.setattr(tushare_daily, "QLIB_STOCK_META", qlib_stock_meta_path)
    monkeypatch.setattr(tushare_daily, "QLIB_INDEX_META", qlib_index_meta_path)
    monkeypatch.setattr(
        tushare_daily,
        "production_consistency_status",
        lambda **kwargs: {"status": "passed", "partial_promote_detected": False, "issues": [], "mismatches": []},
    )
    monkeypatch.setattr(tushare_daily, "PRODUCTION_AUDIT_ROOT", tmp_path / "audits")
    monkeypatch.setattr(
        tushare_daily,
        "_build_snapshot",
        lambda **kwargs: {
            "latest_hdf5_trade_date": "2026-06-02",
            "latest_qlib_trade_date": "2026-06-02",
            "latest_quantgpt_trade_date": "2026-06-02",
            "quantgpt_stocks_on_hdf5_latest_date": 1,
        },
    )
    monkeypatch.setattr(tushare_daily, "quantgpt_contract_report", lambda *args, **kwargs: {"ok": True})
    deep_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        tushare_daily,
        "_deep_sample_quality_audit",
        lambda **kwargs: deep_calls.append(kwargs)
        or {
            "status": "passed",
            "target_date": "20260602",
            "requested_sample_count": kwargs["sample_count"],
            "sample_count": kwargs["sample_count"],
            "issues": [],
            "warnings": [],
        },
    )

    result = tushare_daily.production_audit_summary(
        replace_from_date="20260601",
        full_scan=True,
        deep_sample_count=20,
        write_report=True,
    )

    assert result["status"] == "passed"
    assert result["latest_dates_aligned"] is True
    assert result["hdf_audit"]["duplicate_code_kline_time"] == 0
    assert result["hdf_audit"]["price_sanity_issues"] == 0
    assert result["hdf_audit"]["latest_core_nulls"]["close"] == 0
    assert result["qlib_provider_audit"]["status"] == "passed"
    assert result["schema_alignment"]["LIST_DATE_present"] is True
    assert result["schema_alignment"]["list_date_present"] is True
    assert result["schema_alignment"]["delist_date_present"] is True
    assert result["deep_sample_count"] == 20
    assert deep_calls == [{"latest_date": "2026-06-02", "sample_count": 20}]
    assert result["audit_report_path"]
    assert Path(result["audit_report_path"]).exists()


def test_production_audit_summary_blocks_raw_qlib_stock_price_mode(monkeypatch, tmp_path):
    hdf = tmp_path / "production.h5"
    current_path = tmp_path / "CURRENT_PRODUCTION_DATASET.json"
    latest_status_path = tmp_path / "latest_status.json"
    quality_path = tmp_path / "quality_report.json"
    raw_quality_path = tmp_path / "raw_quality_report.json"
    qlib_stock_meta_path = tmp_path / "stock_converter_meta.json"
    qlib_index_meta_path = tmp_path / "index_converter_meta.json"
    frame = pd.DataFrame(
        [
            {
                "trade_date": pd.Timestamp("2026-06-02"),
                "kline_time": pd.Timestamp("2026-06-02"),
                "code": "600000.SH",
                "open": 10.5,
                "high": 11.5,
                "low": 10.0,
                "close": 11.0,
                "volume": 120.0,
                "amount": 1300.0,
                "pre_close": 10.5,
                "up_limit": 11.55,
                "down_limit": 9.45,
                "LIST_DATE": "19991110",
                "list_date": "19991110",
                "delist_date": "",
            },
        ]
    ).set_index("trade_date")
    frame.to_hdf(hdf, key="/daily", mode="w", format="table", min_itemsize={"code": 16, "LIST_DATE": 16, "list_date": 16, "delist_date": 16})
    tushare_daily._write_json(
        current_path,
        {
            "source": "tushare",
            "production_package_id": "pkg",
            "latest_trade_date": "2026-06-02",
            "latest_dates": {"hdf5": "2026-06-02", "qlib": "2026-06-02", "quantgpt": "2026-06-02"},
        },
    )
    tushare_daily._write_json(
        latest_status_path,
        {
            "snapshot": {
                "latest_hdf5_trade_date": "2026-06-02",
                "latest_qlib_trade_date": "2026-06-02",
                "latest_quantgpt_trade_date": "2026-06-02",
            }
        },
    )
    tushare_daily._write_json(quality_path, {"passed": True})
    tushare_daily._write_json(raw_quality_path, {"passed": True})
    tushare_daily._write_json(
        qlib_stock_meta_path,
        {
            "price_mode": "raw_close_plus_factor",
            "raw_price_fields_retained": True,
            "valid_field_count": 37,
        },
    )
    tushare_daily._write_json(
        qlib_index_meta_path,
        {
            "price_mode": "index_raw_close_identity_adjusted",
            "change_field": "pct_chg_decimal",
            "factor_field": "constant_one_when_missing",
        },
    )
    _write_minimal_qlib_index_artifacts(tmp_path)
    monkeypatch.setattr(tushare_daily, "PRODUCTION_RAW_HDF5", hdf)
    monkeypatch.setattr(tushare_daily, "CURRENT_PRODUCTION_DATASET_FILE", current_path)
    monkeypatch.setattr(tushare_daily, "LATEST_STATUS_FILE", latest_status_path)
    monkeypatch.setattr(tushare_daily, "PRODUCTION_QUALITY_FILE", quality_path)
    monkeypatch.setattr(tushare_daily, "PRODUCTION_RAW_QUALITY_FILE", raw_quality_path)
    monkeypatch.setattr(tushare_daily, "QLIB_STOCK_META", qlib_stock_meta_path)
    monkeypatch.setattr(tushare_daily, "QLIB_INDEX_META", qlib_index_meta_path)
    monkeypatch.setattr(
        tushare_daily,
        "production_consistency_status",
        lambda **kwargs: {"status": "passed", "partial_promote_detected": False, "issues": [], "mismatches": []},
    )
    monkeypatch.setattr(
        tushare_daily,
        "_build_snapshot",
        lambda **kwargs: {
            "latest_hdf5_trade_date": "2026-06-02",
            "latest_qlib_trade_date": "2026-06-02",
            "latest_quantgpt_trade_date": "2026-06-02",
            "quantgpt_stocks_on_hdf5_latest_date": 1,
        },
    )
    monkeypatch.setattr(tushare_daily, "quantgpt_contract_report", lambda *args, **kwargs: {"ok": True})

    result = tushare_daily.production_audit_summary(replace_from_date="20260601", full_scan=True)

    assert result["status"] == "failed"
    assert result["qlib_provider_audit"]["status"] == "failed"
    assert "qlib_stock_price_mode_not_adjusted_plus_factor" in result["issues"]


def test_sample_audit_codes_excludes_benchmark_indices():
    frame = pd.DataFrame(
        {
            "code": ["000001.SH", "000300.SH", "000001.SZ", "600000.SH", "399001.SZ"],
            "pre_close": [1.0, 1.0, 10.0, 11.0, 1.0],
            "LIST_DATE": ["19900101", "20020101", "19910403", "19991110", "19910403"],
        }
    )

    result = tushare_daily._sample_audit_codes(frame, sample_count=5, target_date="20260702")

    assert result == ["000001.SZ", "600000.SH"]


def _write_daily_promotion_fixture(monkeypatch, tmp_path, *, package_id: str = "pkg-daily") -> tuple[Path, Path, dict[str, Path]]:
    project_root = tmp_path
    staging_root = tmp_path / "staging"
    package_root = staging_root / package_id
    compat_root = package_root / "production_compat"
    backup_root = tmp_path / "backups"
    production = tmp_path / "production"
    paths = {
        "raw_hdf": production / "data" / "raw" / "tushare" / "stock_daily.h5",
        "raw_meta": production / "data" / "raw" / "tushare" / "metadata.json",
        "calendar": production / "data" / "raw" / "tushare" / "trade_calendar.txt",
        "calendar_meta": production / "data" / "raw" / "tushare" / "trade_calendar_meta.json",
        "quality": production / "data" / "raw" / "tushare" / "tushare_quality_report.json",
        "raw_quality": production / "data" / "raw" / "tushare" / "tushare_raw_quality_report.json",
        "qlib": production / "data" / "qlib",
        "quantgpt_stocks": production / "data" / "quantgpt" / "stocks",
        "quantgpt_benchmark": production / "data" / "quantgpt" / "benchmark",
        "current": tmp_path / "CURRENT_PRODUCTION_DATASET.json",
        "latest": tmp_path / "latest_status.json",
        "daily_status": tmp_path / "daily_update_status.json",
    }

    monkeypatch.setattr(ops_common, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(tushare_daily, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(tushare_daily, "STAGING_ROOT", staging_root)
    monkeypatch.setattr(tushare_daily, "PROMOTION_BACKUP_ROOT", backup_root)
    monkeypatch.setattr(tushare_daily, "PRODUCTION_LOCK_DIR", tmp_path / "production.lock")
    monkeypatch.setattr(tushare_daily, "PRODUCTION_RAW_HDF5", paths["raw_hdf"])
    monkeypatch.setattr(tushare_daily, "PRODUCTION_RAW_METADATA", paths["raw_meta"])
    monkeypatch.setattr(tushare_daily, "PRODUCTION_TRADING_CALENDAR_FILE", paths["calendar"])
    monkeypatch.setattr(tushare_daily, "PRODUCTION_TRADING_CALENDAR_META", paths["calendar_meta"])
    monkeypatch.setattr(tushare_daily, "PRODUCTION_QUALITY_FILE", paths["quality"])
    monkeypatch.setattr(tushare_daily, "PRODUCTION_RAW_QUALITY_FILE", paths["raw_quality"])
    monkeypatch.setattr(tushare_daily, "QLIB_DATA_ROOT", paths["qlib"])
    monkeypatch.setattr(tushare_daily, "QUANTGPT_DATA_DIR", paths["quantgpt_stocks"])
    monkeypatch.setattr(tushare_daily, "QUANTGPT_BENCHMARK_DIR", paths["quantgpt_benchmark"])
    monkeypatch.setattr(tushare_daily, "CURRENT_PRODUCTION_DATASET_FILE", paths["current"])
    monkeypatch.setattr(tushare_daily, "LATEST_STATUS_FILE", paths["latest"])
    monkeypatch.setattr(tushare_daily, "DAILY_STATUS_FILE", paths["daily_status"])
    monkeypatch.setattr(ops_common, "DAILY_STATUS_FILE", paths["daily_status"])
    monkeypatch.setattr(tushare_daily, "_promotion_idle_state", lambda: {"blockers": [], "processes": []})

    raw_root = compat_root / "raw"
    raw_root.mkdir(parents=True)
    (raw_root / "stock_daily.h5").write_bytes(b"hdf-new")
    (raw_root / "metadata.json").write_text("{}", encoding="utf-8")
    (raw_root / "trade_calendar.txt").write_text("2026-06-02\n", encoding="utf-8")
    (raw_root / "trade_calendar_meta.json").write_text("{}", encoding="utf-8")
    (compat_root / "quality_report.json").write_text(
        json.dumps(
            {
                "passed": True,
                "field_groups": {},
                "latest_code_activity": {},
                "metadata_quality": {},
                "limit_price_quality": {},
                "factor_adjusted_quality": {},
                "schema_summary": {},
            }
        ),
        encoding="utf-8",
    )
    (compat_root / "raw_quality_report.json").write_text('{"passed": true}', encoding="utf-8")
    qlib_root = compat_root / "qlib"
    _write_minimal_qlib_index_artifacts(qlib_root, latest="2026-06-02")
    tushare_daily._write_json(
        qlib_root / "stock_converter_meta.json",
        {
            "price_mode": "adjusted_ohlc_plus_factor_for_qlib_exchange",
            "raw_price_fields_retained": True,
            "valid_field_count": 69,
            "calendar_latest_date": "2026-06-02",
        },
    )
    qlib_root.joinpath("instruments").mkdir(exist_ok=True)
    (qlib_root / "instruments" / "all.txt").write_text("000001sz\t2026-06-02\t2026-06-02\n", encoding="utf-8")
    (compat_root / "quantgpt" / "stocks").mkdir(parents=True)
    (compat_root / "quantgpt" / "stocks" / "_conversion_contract.json").write_text('{"ok": true}', encoding="utf-8")
    (compat_root / "quantgpt" / "benchmark").mkdir(parents=True)
    for name in ["benchmark_hs300.parquet", "benchmark_csi500.parquet", "benchmark_csi1000.parquet"]:
        (compat_root / "quantgpt" / "benchmark" / name).write_bytes(b"parquet")
    snapshot = {
        "latest_hdf5_trade_date": "2026-06-02",
        "latest_qlib_trade_date": "2026-06-02",
        "latest_quantgpt_trade_date": "2026-06-02",
        "quantgpt_benchmark_file_count": 3,
        "quantgpt_benchmark_files": ["benchmark_csi1000.parquet", "benchmark_csi500.parquet", "benchmark_hs300.parquet"],
        "quantgpt_contract": {"ok": True},
        "consumer_readiness": {"quantgpt_factor_mining": True, "qlib_model_training": True, "qlib_paper_trading": True},
    }
    tushare_daily._write_json(
        compat_root / "production_compat_manifest.json",
        {
            "status": "completed",
            "package_id": package_id,
            "compat_root": str(compat_root),
            "snapshot": snapshot,
        },
    )
    tushare_daily._write_json(
        package_root / "manifest.json",
        {
            "status": "completed",
            "package_id": package_id,
            "effective_target_date": "20260602",
            "compat_manifest": str(compat_root / "production_compat_manifest.json"),
        },
    )
    tushare_daily._write_json(paths["current"], {"source": "tushare", "production_package_id": "old-pkg"})
    tushare_daily._write_json(paths["latest"], {"status": "old-latest"})
    tushare_daily._write_json(paths["daily_status"], {"status": "old-daily"})
    return package_root, compat_root, paths


def test_data_promote_staged_writes_journal_and_commits_after_target_verify(monkeypatch, tmp_path):
    package_root, _compat_root, paths = _write_daily_promotion_fixture(monkeypatch, tmp_path)
    paths["raw_hdf"].parent.mkdir(parents=True)
    paths["raw_hdf"].write_bytes(b"hdf-old")

    result = tushare_daily.data_promote_staged(package_id=package_root.name)

    assert result["status"] == "promoted"
    assert result["package_id"] == package_root.name
    journal = next((tmp_path / "backups").glob(f"promote-*-{package_root.name}/promote_journal.json"))
    assert result["promote_journal_path"] == str(journal)
    payload = tushare_daily._read_json(journal)
    assert payload["status"] == "committed"
    assert {item["label"] for item in payload["completed_targets"]} >= {"raw_hdf", "qlib_features", "quantgpt_stocks"}
    current = tushare_daily._read_json(paths["current"])
    assert current["production_package_id"] == package_root.name
    assert current["latest_dates"]["hdf5"] == "2026-06-02"
    assert current["consumer_readiness_gate"] == "pending_production_audit"
    assert current["artifact_readiness"]["qlib_model_training"] is True
    assert not any(current["consumer_readiness"].values())


def test_data_promote_staged_closes_consumer_gate_before_first_target_replace(monkeypatch, tmp_path):
    package_root, _compat_root, paths = _write_daily_promotion_fixture(monkeypatch, tmp_path)
    paths["raw_hdf"].parent.mkdir(parents=True)
    paths["raw_hdf"].write_bytes(b"hdf-old")
    original_replace = tushare_daily._replace_path
    observed = []

    def assert_gate_closed_before_replace(src, dest, backup_root, replaced):
        current = tushare_daily._read_json(paths["current"])
        observed.append((dest, current.get("consumer_readiness_gate")))
        assert current["consumer_readiness_gate"] == "promotion_in_progress"
        assert not any((current.get("consumer_readiness") or {}).values())
        return original_replace(src, dest, backup_root, replaced)

    monkeypatch.setattr(tushare_daily, "_replace_path", assert_gate_closed_before_replace)

    result = tushare_daily.data_promote_staged(package_id=package_root.name)

    assert result["status"] == "promoted"
    assert observed


def test_data_promote_staged_blocks_incomplete_quality_report(monkeypatch, tmp_path):
    package_root, compat_root, _paths = _write_daily_promotion_fixture(monkeypatch, tmp_path)
    (compat_root / "quality_report.json").write_text('{"passed": true}', encoding="utf-8")

    result = tushare_daily.data_promote_staged(package_id=package_root.name)

    assert result["status"] == "blocked"
    assert "production_quality_report_incomplete" in result["blockers"]


def test_data_promote_staged_reconciles_equivalent_files_after_status_crash(monkeypatch, tmp_path):
    package_root, compat_root, paths = _write_daily_promotion_fixture(monkeypatch, tmp_path)
    for _label, src, dest in tushare_daily._daily_promotion_targets(compat_root):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
    prior = tmp_path / "backups" / f"promote-20260603_000000-{package_root.name}"
    prior.mkdir(parents=True)

    result = tushare_daily.data_promote_staged(package_id=package_root.name)

    assert result["status"] == "promoted"
    assert result["reconciled_from_equivalent_files"] is True
    assert result["promotion_id"] == prior.name
    assert result["promote_journal_path"] == str(prior / "promote_journal.json")
    journal = tushare_daily._read_json(prior / "promote_journal.json")
    assert journal["status"] == "reconciled_committed"
    current = tushare_daily._read_json(paths["current"])
    assert current["production_package_id"] == package_root.name
    assert current["reconciled_from_equivalent_files"] is True


def test_data_promote_staged_rolls_back_artifacts_and_state_when_commit_journal_fails(monkeypatch, tmp_path):
    package_root, _compat_root, paths = _write_daily_promotion_fixture(monkeypatch, tmp_path)
    paths["raw_hdf"].parent.mkdir(parents=True)
    paths["raw_hdf"].write_bytes(b"hdf-old")
    original_write_journal = tushare_daily._write_promotion_journal

    def fail_committed_journal(path, payload):
        if payload.get("status") == "committed":
            raise OSError("journal_commit_failed")
        return original_write_journal(path, payload)

    monkeypatch.setattr(tushare_daily, "_write_promotion_journal", fail_committed_journal)

    with pytest.raises(OSError, match="journal_commit_failed"):
        tushare_daily.data_promote_staged(package_id=package_root.name)

    assert paths["raw_hdf"].read_bytes() == b"hdf-old"
    assert not paths["raw_meta"].exists()
    assert tushare_daily._read_json(paths["current"])["production_package_id"] == "old-pkg"
    assert tushare_daily._read_json(paths["latest"])["status"] == "old-latest"
    assert tushare_daily._read_json(paths["daily_status"])["status"] == "old-daily"


def test_recover_existing_qlib_index_requires_index_artifacts(tmp_path):
    qlib_root = tmp_path / "qlib"
    (qlib_root / "calendars").mkdir(parents=True)
    (qlib_root / "calendars" / "day.txt").write_text("2026-06-02\n", encoding="utf-8")

    missing = tushare_daily._recover_existing_qlib_step(
        qlib_root,
        name="qlib_index_convert",
        expected_latest="2026-06-02",
    )
    assert missing is None

    _write_minimal_qlib_index_artifacts(qlib_root)
    recovered = tushare_daily._recover_existing_qlib_step(
        qlib_root,
        name="qlib_index_convert",
        expected_latest="2026-06-02",
    )
    assert recovered is not None
    assert recovered["reused_existing_output"] is True


def test_daily_qlib_outputs_patch_seed_bins_without_full_rewrite(monkeypatch, tmp_path):
    seed_qlib = tmp_path / "seed_qlib"
    (seed_qlib / "calendars").mkdir(parents=True)
    (seed_qlib / "instruments").mkdir(parents=True)
    (seed_qlib / "calendars" / "day.txt").write_text("2026-06-30\n2026-07-01\n", encoding="utf-8")
    (seed_qlib / "instruments" / "all.txt").write_text("000001sz\t2026-06-30\t2026-07-01\n", encoding="utf-8")
    _write_test_bin(seed_qlib / "features" / "000001sz" / "close.day.bin", [20.0, 22.0])
    _write_test_bin(seed_qlib / "features" / "000001sz" / "pre_close.day.bin", [18.0, 20.0])
    _write_test_bin(seed_qlib / "features" / "000300sh" / "close.day.bin", [3000.0, 3001.0])

    merged_hdf = tmp_path / "merged.h5"
    _raw_daily_rows_for_qlib_patch().to_hdf(
        merged_hdf,
        key="/daily",
        mode="w",
        format="table",
        min_itemsize={"code": 16, "st_status": 16, "limit_source_kind": 24, "list_status": 8},
    )

    monkeypatch.setattr(tushare_daily, "QLIB_DATA_ROOT", seed_qlib)
    tushare_daily._QLIB_STOCK_CONVERTER_MODULE = None
    tushare_daily._QLIB_INDEX_CONVERTER_MODULE = None

    qlib_root = tmp_path / "compat" / "qlib"
    result = tushare_daily._build_daily_qlib_outputs(
        merged_hdf=merged_hdf,
        qlib_root=qlib_root,
        replace_from_date="20260701",
        expected_latest="2026-07-02",
    )

    assert result["stock_step"]["command"] == ["daily_window_patch_from_seed"]
    assert (qlib_root / "calendars" / "day.txt").read_text(encoding="utf-8").splitlines() == [
        "2026-06-30",
        "2026-07-01",
        "2026-07-02",
    ]
    close_start, close_values = _read_test_bin(qlib_root / "features" / "000001sz" / "close.day.bin")
    pre_start, pre_values = _read_test_bin(qlib_root / "features" / "000001sz" / "pre_close.day.bin")
    index_start, index_values = _read_test_bin(qlib_root / "features" / "000300sh" / "close.day.bin")
    assert close_start == pre_start == index_start == 0
    assert close_values == pytest.approx([20.0, 22.0, 24.0])
    assert pre_values == pytest.approx([18.0, 20.0, 22.0])
    assert index_values == pytest.approx([3000.0, 3001.0, 3002.0])
    stock_meta = tushare_daily._read_json(qlib_root / "stock_converter_meta.json")
    index_meta = tushare_daily._read_json(qlib_root / "index_converter_meta.json")
    assert stock_meta["effective_mode"] == "daily_window_patch"
    assert stock_meta["raw_price_fields_retained"] is True
    assert index_meta["price_mode"] == "index_raw_close_identity_adjusted"


def test_raw_chunk_to_qlib_frame_preserves_canonical_fields():
    rows = []
    for date, close in [("2026-07-01", 10.0), ("2026-07-02", 11.0)]:
        row = {
            "trade_date": pd.Timestamp(date),
            "code": "000001.SZ",
            "kline_time": pd.Timestamp(date),
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 100.0,
            "amount": 1000.0,
            "pre_close": close - 1.0,
            "pct_chg": 1.0,
            "amp": 2.0,
            "backward_factor": 2.0,
            "st_status": "NORMAL",
            "up_limit": close + 1.0,
            "down_limit": close - 1.0,
            "limit_source_kind": "official",
        }
        for source_col in set(QLIB_RAW_FIELD_MAP.values()):
            row.setdefault(source_col, 1.0)
        rows.append(row)
    out = raw_chunk_to_qlib_frame(pd.DataFrame(rows))

    assert len(out) == 2
    assert "$up_limit" in out.columns
    assert "$down_limit" in out.columns
    assert out["$close"].tolist() == pytest.approx([10.0, 11.0])


def test_validate_stage_blocks_missing_qlib_index_artifacts(tmp_path):
    compat_root = tmp_path / "production_compat"
    compat_root.mkdir()
    compat_manifest = compat_root / "production_compat_manifest.json"
    tushare_daily._write_json(
        compat_manifest,
        {
            "status": "completed",
            "snapshot": {
                "latest_hdf5_trade_date": "2026-06-02",
                "latest_qlib_trade_date": "2026-06-02",
                "latest_quantgpt_trade_date": "2026-06-02",
                "quantgpt_benchmark_file_count": 3,
                "quantgpt_contract": {"ok": True},
            },
        },
    )
    manifest = {"status": "completed", "compat_manifest": str(compat_manifest)}

    issues = tushare_daily._validate_stage_for_promotion(manifest)
    assert "qlib_index_artifacts_not_ready" in issues
    assert "qlib_index_meta_missing" in issues

    _write_minimal_qlib_index_artifacts(compat_root / "qlib")
    issues = tushare_daily._validate_stage_for_promotion(manifest)
    assert "qlib_index_artifacts_not_ready" not in issues


def test_write_trading_calendar_from_hdf(tmp_path):
    hdf = tmp_path / "stock_daily.h5"
    calendar = tmp_path / "trade_calendar.txt"
    meta = tmp_path / "trade_calendar_meta.json"
    frame = pd.DataFrame(
        [
            {"trade_date": pd.Timestamp("2026-06-02"), "kline_time": pd.Timestamp("2026-06-02"), "code": "600000.SH"},
            {"trade_date": pd.Timestamp("2026-06-02"), "kline_time": pd.Timestamp("2026-06-02"), "code": "000300.SH"},
            {"trade_date": pd.Timestamp("2026-06-03"), "kline_time": pd.Timestamp("2026-06-03"), "code": "600000.SH"},
        ]
    ).set_index("trade_date")
    frame.to_hdf(hdf, key="/daily", mode="w", format="table")

    payload = _write_trading_calendar(hdf, calendar, meta)

    assert payload["date_count"] == 2
    assert payload["first_date"] == "2026-06-02"
    assert payload["latest_date"] == "2026-06-03"
    assert calendar.read_text(encoding="utf-8").splitlines() == ["2026-06-02", "2026-06-03"]


def test_record_production_audit_persists_exact_quantgpt_coverage(tmp_path, monkeypatch):
    current_path = tmp_path / "CURRENT_PRODUCTION_DATASET.json"
    latest_status_path = tmp_path / "latest_status.json"
    daily_path = tmp_path / "daily_update_status.json"
    current_path.write_text(
        json.dumps(
            {
                "production_package_id": "pkg",
                "latest_trade_date": "2026-06-03",
                "consumer_readiness": {"quantgpt_factor_mining": True},
            }
        ),
        encoding="utf-8",
    )
    latest_status_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "snapshot": {
                    "latest_hdf5_trade_date": "2026-06-03",
                    "latest_qlib_trade_date": "2026-06-03",
                    "latest_quantgpt_trade_date": "2026-06-03",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tushare_daily, "CURRENT_PRODUCTION_DATASET_FILE", current_path)
    monkeypatch.setattr(tushare_daily, "LATEST_STATUS_FILE", latest_status_path)
    monkeypatch.setattr(tushare_daily, "DAILY_STATUS_FILE", daily_path)
    monkeypatch.setattr(ops_common, "DAILY_STATUS_FILE", daily_path)
    monkeypatch.setattr(
        tushare_daily,
        "_build_snapshot",
        lambda deep=False: {
            "latest_hdf5_trade_date": "2026-06-03",
            "latest_qlib_trade_date": "2026-06-03",
            "latest_quantgpt_trade_date": "2026-06-03",
            "quantgpt_stock_parquet_count": 2,
            "quantgpt_stocks_on_hdf5_latest_date": 2,
            "quantgpt_latest_coverage_ratio": 1.0,
        },
    )
    monkeypatch.setattr(tushare_daily, "_qlib_index_readiness", lambda *args, **kwargs: {"status": "passed"})

    tushare_daily.record_production_audit_result(
        {
            "status": "passed",
            "generated_at": "2026-06-03T10:00:00",
            "production_package_id": "pkg",
            "latest_trade_date": "2026-06-03",
            "issues": [],
        }
    )

    persisted = json.loads(latest_status_path.read_text(encoding="utf-8"))
    assert persisted["snapshot"]["quantgpt_stock_parquet_count"] == 2
    assert persisted["snapshot"]["quantgpt_latest_coverage_ratio"] == 1.0
