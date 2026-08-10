from __future__ import annotations

import json
import os
from pathlib import Path
import struct

import pandas as pd

from services import data_foundation_service as svc


def test_data_benchmark_series_reads_compact_qlib_binary(tmp_path, monkeypatch):
    qlib_root = tmp_path / "qlib"
    calendar_path = qlib_root / "calendars" / "day.txt"
    feature_path = qlib_root / "features" / "000300sh" / "close.day.bin"
    calendar_path.parent.mkdir(parents=True)
    feature_path.parent.mkdir(parents=True)
    calendar_path.write_text("2026-06-16\n2026-06-17\n2026-06-18\n2026-06-19\n", encoding="utf-8")
    feature_path.write_bytes(struct.pack("<5f", 0.0, 100.0, 102.0, float("nan"), 105.0))
    monkeypatch.setattr(svc, "QLIB_DATA_ROOT", qlib_root)

    result = svc.data_benchmark_series(code="000300.SH", start="2026-06-17", end="2026-06-19")

    assert result.ok
    assert result.outputs["metadata"] == {
        "code": "000300.SH",
        "row_count": 2,
        "start": "2026-06-17",
        "end": "2026-06-19",
        "source": "qlib_binary_close",
    }
    assert result.outputs["rows"] == [
        {"date": "2026-06-17", "code": "000300.SH", "close": 102.0},
        {"date": "2026-06-19", "code": "000300.SH", "close": 105.0},
    ]


def test_production_health_blocks_matching_failed_audit():
    health = svc._production_health(
        {"production_package_id": "pkg", "latest_trade_date": "2026-07-10"},
        {
            "path": "/tmp/audit.json",
            "status": "failed",
            "production_package_id": "pkg",
            "latest_trade_date": "2026-07-10",
            "issues": ["sample_cross_surface_mismatch"],
        },
    )

    assert health["status"] == "blocked"
    assert health["reason"] == "latest_production_audit_failed"


def test_production_health_blocks_pending_post_promotion_audit():
    health = svc._production_health(
        {
            "production_package_id": "pkg",
            "latest_trade_date": "2026-07-10",
            "consumer_readiness_gate": "pending_production_audit",
            "production_audit": {"status": "pending"},
        },
        {"status": "missing", "path": None},
    )

    assert health["status"] == "blocked"
    assert health["reason"] == "production_audit_pending"


def test_data_status_preserves_full_quality_summary_from_production_report(tmp_path, monkeypatch):
    current_dataset = tmp_path / "CURRENT_PRODUCTION_DATASET.json"
    quality_report = tmp_path / "tushare_quality_report.json"
    current_dataset.write_text(
        json.dumps(
            {
                "status": "production",
                "source": "tushare",
                "canonical_read_paths": {"tushare_quality_report": str(quality_report)},
            }
        ),
        encoding="utf-8",
    )
    quality_report.write_text(
        json.dumps(
            {
                "passed": True,
                "n_rows": 5202,
                "latest_trade_date": "2026-07-17",
                "latest_code_activity": {"latest_day_stock_count": 5202, "stock_code_count": 5212},
                "field_groups": {"market_core_fields": {"max_missing_pct": 0.0}},
                "schema_summary": {"schema_version": "tushare_v1", "price_mode": "raw_with_legacy_adjusted_compat_columns"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(svc, "CURRENT_PRODUCTION_DATASET_FILE", current_dataset)
    monkeypatch.setattr(svc, "data_foundation_status", lambda: {"status": "completed", "snapshot": {}, "steps": []})
    monkeypatch.setattr(svc, "latest_staging_package", lambda: None)
    monkeypatch.setattr(svc, "production_consistency_status", lambda **kwargs: {"status": "passed", "partial_promote_detected": False})

    result = svc.data_status()

    assert result.ok
    summary = result.outputs["data_quality_summary"]
    assert summary["n_rows"] == 5202
    assert summary["schema_summary"]["schema_version"] == "tushare_v1"
    assert summary["latest_code_activity"]["latest_day_stock_count"] == 5202


def test_production_quality_report_resolves_relative_manifest_against_configured_hdf(tmp_path, monkeypatch):
    release_root = tmp_path / "release"
    mounted_data_root = tmp_path / "durable-data" / "raw" / "tushare"
    mounted_data_root.mkdir(parents=True)
    current_dataset = release_root / "runtime" / "data_foundation" / "CURRENT_PRODUCTION_DATASET.json"
    current_dataset.parent.mkdir(parents=True)
    configured_hdf = mounted_data_root / "stock_daily.h5"
    configured_hdf.touch()
    quality_report = mounted_data_root / "tushare_quality_report.json"
    quality_report.write_text(json.dumps({"passed": True, "n_rows": 42}), encoding="utf-8")
    manifest = {
        "source": "tushare",
        "canonical_read_paths": {
            "production_raw_hdf5": "data/raw/tushare/stock_daily.h5",
            "tushare_quality_report": "data/raw/tushare/tushare_quality_report.json",
        },
    }

    monkeypatch.setattr(svc, "CURRENT_PRODUCTION_DATASET_FILE", current_dataset)
    monkeypatch.setattr(svc, "PRODUCTION_RAW_HDF5", configured_hdf)
    monkeypatch.setattr(svc, "run_quality_check", lambda: (_ for _ in ()).throw(AssertionError("unexpected deep scan")))

    assert svc._production_quality_report(manifest) == {"passed": True, "n_rows": 42}


def test_data_live_status_compacts_daily_and_progress(tmp_path, monkeypatch):
    daily_file = tmp_path / "daily_update_status.json"
    current_dataset = tmp_path / "CURRENT_PRODUCTION_DATASET.json"
    staging = tmp_path / "staging" / "pkg"
    staging.mkdir(parents=True)
    daily_file.write_text(
        json.dumps(
            {
                "status": "completed",
                "package_id": "daily-pkg",
                "current_stage": "completed",
                "stage_summary": {"current_stage": "completed", "completed_stages": ["source_rebuild"]},
                "source_rebuild": {"status": "completed", "package_id": "source-pkg", "trade_date_count": 2, "code_count": 3},
                "merge_result": {"preserved_rows": 10, "removed_rows": 1, "delta_rows": 3},
                "snapshot": {"latest_hdf5_trade_date": "2026-06-17", "quantgpt_stock_parquet_count": 3},
            }
        ),
        encoding="utf-8",
    )
    current_dataset.write_text(json.dumps({"status": "production", "latest_trade_date": "2026-06-17"}), encoding="utf-8")
    (staging / "manifest.json").write_text(json.dumps({"package_id": "pkg", "status": "completed"}), encoding="utf-8")
    (staging / "full_rebuild_progress.json").write_text(
        json.dumps({"status": "completed", "package_id": "pkg", "stages": {"daily": {"cursor": 2, "total": 2, "status": "completed"}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(svc, "DAILY_STATUS_FILE", daily_file)
    monkeypatch.setattr(svc, "CURRENT_PRODUCTION_DATASET_FILE", current_dataset)
    monkeypatch.setattr(svc, "DATA_FOUNDATION_ROOT", tmp_path)
    monkeypatch.setattr(svc, "GUI_JOBS_ROOT", tmp_path / "gui_jobs")
    monkeypatch.setattr(svc, "latest_staging_package", lambda: {"package_id": "daily-pkg", "status": "completed"})

    result = svc.data_live_status()

    assert result.ok
    outputs = result.outputs
    assert outputs["daily_update"]["package_id"] == "daily-pkg"
    assert outputs["source_progress"]["stages"]["daily"]["cursor"] == 2
    assert "raw_quality" not in outputs["full_rebuild"]["manifest"]


def test_data_live_status_marks_daily_status_with_external_package_root_invalid(tmp_path, monkeypatch):
    daily_file = tmp_path / "daily_update_status.json"
    current_dataset = tmp_path / "CURRENT_PRODUCTION_DATASET.json"
    external_root = tmp_path.parent / "pytest-case" / "tushare-daily-interrupted-target-20260605"
    daily_file.write_text(
        json.dumps(
            {
                "status": "failed",
                "latest_stage": {
                    "package_id": "tushare-daily-interrupted-target-20260605",
                    "status": "failed",
                    "package_kind": "daily_update",
                    "package_root": str(external_root),
                    "current_stage": "source_rebuild",
                    "error": "source_rebuild_failed:failed",
                },
                "last_successful_promotion": {"status": "promoted", "package_id": "good-package"},
            }
        ),
        encoding="utf-8",
    )
    current_dataset.write_text(json.dumps({"status": "production", "latest_trade_date": "2026-06-26"}), encoding="utf-8")

    monkeypatch.setattr(svc, "DAILY_STATUS_FILE", daily_file)
    monkeypatch.setattr(svc, "CURRENT_PRODUCTION_DATASET_FILE", current_dataset)
    monkeypatch.setattr(svc, "DATA_FOUNDATION_ROOT", tmp_path / "runtime" / "data_foundation")
    monkeypatch.setattr(svc, "GUI_JOBS_ROOT", tmp_path / "gui_jobs")
    monkeypatch.setattr(svc, "latest_staging_package", lambda: {"package_id": "real-package", "status": "completed"})

    result = svc.data_live_status()

    assert result.ok
    daily_update = result.outputs["daily_update"]
    assert daily_update["status"] == "stale_invalid"
    assert daily_update["invalid_reason"] == "daily_status_package_root_missing"
    assert daily_update["package_id"] == "tushare-daily-interrupted-target-20260605"
    assert result.outputs["status"] == "stale_invalid"


def test_data_status_sanitizes_invalid_daily_status(tmp_path, monkeypatch):
    daily_file = tmp_path / "daily_update_status.json"
    current_dataset = tmp_path / "CURRENT_PRODUCTION_DATASET.json"
    external_root = tmp_path / "outside" / "tushare-daily-interrupted-target-20260605"
    external_root.mkdir(parents=True)
    daily_file.write_text(
        json.dumps(
            {
                "status": "failed",
                "latest_stage": {
                    "package_id": "tushare-daily-interrupted-target-20260605",
                    "status": "failed",
                    "package_root": str(external_root),
                },
            }
        ),
        encoding="utf-8",
    )
    current_dataset.write_text(json.dumps({"status": "production"}), encoding="utf-8")

    monkeypatch.setattr(svc, "DAILY_STATUS_FILE", daily_file)
    monkeypatch.setattr(svc, "CURRENT_PRODUCTION_DATASET_FILE", current_dataset)
    monkeypatch.setattr(svc, "DATA_FOUNDATION_ROOT", tmp_path / "runtime" / "data_foundation")
    monkeypatch.setattr(svc, "data_foundation_status", lambda: {"status": "completed", "snapshot": {}, "steps": []})
    monkeypatch.setattr(svc, "_production_quality_report", lambda current_dataset=None: {"passed": True})
    monkeypatch.setattr(svc, "latest_staging_package", lambda: None)
    monkeypatch.setattr(
        svc,
        "production_consistency_status",
        lambda **kwargs: {"status": "passed", "partial_promote_detected": False, "issues": [], "mismatches": []},
    )

    result = svc.data_status()

    assert result.ok
    daily_update = result.outputs["daily_update"]
    assert daily_update["status"] == "stale_invalid"
    assert daily_update["invalid_reason"] == "daily_status_package_root_outside_staging"
    assert result.outputs["partial_promote_status"]["status"] == "clear"
    assert result.outputs["latest_audit_report"]["status"] == "missing"


def test_data_update_start_returns_active_job_without_starting_thread(tmp_path, monkeypatch):
    jobs_root = tmp_path / "gui_jobs"
    jobs_root.mkdir()
    active = {
        "job_id": "data-gui-active",
        "mode": "daily",
        "status": "running",
        "pid": os.getpid(),
        "created_at": svc._now(),
    }
    (jobs_root / "data-gui-active.json").write_text(json.dumps(active), encoding="utf-8")
    monkeypatch.setattr(svc, "GUI_JOBS_ROOT", jobs_root)

    result = svc.data_update_start(mode="daily", target_date="auto", dry_run=True)

    assert result.ok
    assert result.outputs["status"] == "already_running"
    assert result.outputs["active_job"]["job_id"] == "data-gui-active"


def test_data_update_start_reconciles_stale_job_before_launch(tmp_path, monkeypatch):
    jobs_root = tmp_path / "gui_jobs"
    jobs_root.mkdir()
    stale_path = jobs_root / "data-gui-stale.json"
    stale_path.write_text(
        json.dumps(
            {
                "job_id": "data-gui-stale",
                "mode": "daily",
                "status": "running",
                "pid": 99999999,
                "created_at": "2026-06-18T10:00:00",
            }
        ),
        encoding="utf-8",
    )
    launched: list[str] = []
    monkeypatch.setattr(svc, "GUI_JOBS_ROOT", jobs_root)
    monkeypatch.setattr(svc, "_launch_data_job_worker", lambda job: launched.append(job["job_id"]) or job)

    result = svc.data_update_start(mode="daily", target_date="auto", dry_run=True)

    assert result.ok
    assert result.outputs["status"] == "started"
    assert launched == [result.outputs["job"]["job_id"]]
    assert json.loads(stale_path.read_text(encoding="utf-8"))["status"] == "interrupted"


def test_data_update_execution_requires_confirmation_without_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "GUI_JOBS_ROOT", tmp_path / "gui_jobs")
    monkeypatch.setattr(
        svc,
        "_launch_data_job_worker",
        lambda job: (_ for _ in ()).throw(AssertionError("worker must not launch")),
    )

    result = svc.data_update_start(mode="daily", target_date="auto", dry_run=False, confirm=False)

    assert not result.ok
    assert result.err == "data_update_execution_confirmation_required"


def test_data_update_start_persists_worker_launch_failure(tmp_path, monkeypatch):
    jobs_root = tmp_path / "gui_jobs"
    monkeypatch.setattr(svc, "GUI_JOBS_ROOT", jobs_root)
    monkeypatch.setattr(
        svc,
        "_launch_data_job_worker",
        lambda job: (_ for _ in ()).throw(OSError("launcher_unavailable")),
    )

    result = svc.data_update_start(mode="daily", target_date="auto", dry_run=True)

    assert not result.ok
    persisted = json.loads(next(jobs_root.glob("*.json")).read_text(encoding="utf-8"))
    assert persisted["status"] == "failed"
    assert persisted["error"] == "worker_launch_failed:launcher_unavailable"


def test_data_query_reads_single_code_and_benchmark(tmp_path, monkeypatch):
    hdf = tmp_path / "stock_daily.h5"
    df = pd.DataFrame(
        [
            {"code": "000001.SZ", "kline_time": "2026-06-16", "SECURITY_NAME": "平安银行", "list_status": "L", "st_status": "NORMAL", "open": 10.0, "high": 11.0, "low": 9.8, "close": 10.5, "up_limit": 11.55, "down_limit": 9.45, "limit_source_kind": "official", "adj_open": 9.5, "adj_close": 10.0, "backward_factor": 0.95, "PE": 5.0, "PB": 0.5, "volume": 1000},
            {"code": "000001.SZ", "kline_time": "2026-06-17", "SECURITY_NAME": "平安银行", "list_status": "L", "st_status": "NORMAL", "open": 10.5, "high": 11.5, "low": 10.2, "close": 11.0, "up_limit": 12.10, "down_limit": 9.90, "limit_source_kind": "official", "adj_open": 10.0, "adj_close": 10.5, "backward_factor": 0.96, "PE": 5.2, "PB": 0.52, "volume": 1200},
            {"code": "000300.SH", "kline_time": "2026-06-16", "SECURITY_NAME": "沪深300", "list_status": "I", "st_status": "NORMAL", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "up_limit": None, "down_limit": None, "limit_source_kind": "index", "adj_open": 100.0, "adj_close": 100.0, "backward_factor": 1.0, "PE": 0.0, "PB": 0.0, "volume": 1},
            {"code": "000300.SH", "kline_time": "2026-06-17", "SECURITY_NAME": "沪深300", "list_status": "I", "st_status": "NORMAL", "open": 101.0, "high": 103.0, "low": 100.0, "close": 102.0, "up_limit": None, "down_limit": None, "limit_source_kind": "index", "adj_open": 101.0, "adj_close": 102.0, "backward_factor": 1.0, "PE": 0.0, "PB": 0.0, "volume": 1},
        ]
    )
    df["sm_net_vol"] = [15.0, 18.0, 0.0, 0.0]
    df["lg_net_vol"] = [20.0, 25.0, 0.0, 0.0]
    df["net_mf_amount"] = [80.0, 95.0, 0.0, 0.0]
    df["margin_balance"] = [200.0, 210.0, 0.0, 0.0]
    df["short_balance"] = [30.0, 28.0, 0.0, 0.0]
    df["NET_PROFIT"] = [300.0, 320.0, 0.0, 0.0]
    df["ROE"] = [8.0, 8.2, 0.0, 0.0]
    df["cost_15pct"] = [9.5, 9.8, 0.0, 0.0]
    df["weight_avg"] = [10.1, 10.4, 0.0, 0.0]
    df.to_hdf(hdf, key="/daily", mode="w", format="table", data_columns=["code", "list_status", "st_status"])
    monkeypatch.setattr(svc, "PRODUCTION_RAW_HDF5", hdf)

    fields = svc.data_query_fields()
    result = svc.data_query(
        code="000001",
        start="2026-06-16",
        end="2026-06-17",
        fields="open,high,low,close,PE,PB,list_status,st_status",
        benchmark="000300",
        transform="index100",
    )

    assert fields.ok
    assert "st_status" in fields.outputs["columns"]
    assert "K线" not in fields.outputs["groups"]
    assert "状态" not in fields.outputs["groups"]
    assert fields.outputs["default_fields"] == ["volume", "PE", "PB"]
    assert fields.outputs["transforms"][0] == "zscore"
    assert "adj_open" in fields.outputs["groups"]["价格衍生"]
    assert "adj_close" in fields.outputs["groups"]["价格衍生"]
    assert "backward_factor" in fields.outputs["groups"]["价格衍生"]
    assert "sm_net_vol" in fields.outputs["groups"]["资金"]
    assert "lg_net_vol" in fields.outputs["groups"]["资金"]
    assert "net_mf_amount" in fields.outputs["groups"]["资金"]
    assert "short_balance" in fields.outputs["groups"]["资金"]
    assert "NET_PROFIT" in fields.outputs["groups"]["财务基本面"]
    assert "ROE" in fields.outputs["groups"]["财务基本面"]
    assert "cost_15pct" in fields.outputs["groups"]["筹码成本"]
    assert "weight_avg" in fields.outputs["groups"]["筹码成本"]
    assert "up_limit" in fields.outputs["groups"]["交易约束审计"]
    assert "down_limit" in fields.outputs["groups"]["交易约束审计"]
    assert "limit_source_kind" in fields.outputs["groups"]["交易约束审计"]
    assert result.ok
    assert result.outputs["metadata"]["code"] == "000001.SZ"
    assert result.outputs["metadata"]["latest_st_status"] == "NORMAL"
    assert len(result.outputs["rows"]) == 2
    assert any(series["field"] == "benchmark_close" for series in result.outputs["chart_series"])


def test_data_query_rejects_unknown_fields(tmp_path, monkeypatch):
    hdf = tmp_path / "stock_daily.h5"
    pd.DataFrame([{"code": "000001.SZ", "kline_time": "2026-06-17", "close": 1.0}]).to_hdf(
        hdf,
        key="/daily",
        mode="w",
        format="table",
        data_columns=["code"],
    )
    monkeypatch.setattr(svc, "PRODUCTION_RAW_HDF5", hdf)

    result = svc.data_query(code="000001.SZ", fields="close,not_a_field")

    assert not result.ok
    assert "invalid_fields:not_a_field" in result.err


def test_data_query_short_code_resolves_existing_hdf_suffix(tmp_path, monkeypatch):
    hdf = tmp_path / "stock_daily.h5"
    pd.DataFrame(
        [
            {
                "code": "999999.SH",
                "kline_time": "2026-06-17",
                "SECURITY_NAME": "其他代码",
                "list_status": "I",
                "st_status": "NORMAL",
                "open": 2.0,
                "high": 2.1,
                "low": 1.9,
                "close": 2.0,
                "PE": 0.0,
            },
            {
                "code": "000002.SH",
                "kline_time": "2026-06-17",
                "SECURITY_NAME": "测试股票",
                "list_status": "L",
                "st_status": "NORMAL",
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.0,
                "PE": 10.0,
            }
        ]
    ).to_hdf(hdf, key="/daily", mode="w", format="table", data_columns=["code"])
    monkeypatch.setattr(svc, "PRODUCTION_RAW_HDF5", hdf)

    result = svc.data_query(code="000002", fields="PE")

    assert result.ok
    assert result.outputs["metadata"]["code"] == "000002.SH"
    assert len(result.outputs["rows"]) == 1
