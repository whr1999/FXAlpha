from __future__ import annotations

import json

import pandas as pd

import domain.model.validation as validation
from domain.model.contracts import (
    DEFAULT_PORTFOLIO,
    LIMIT_THRESHOLD,
    default_r1_experiment,
)
from domain.model.validation import audit_seed_run


def _isolate_validation_reference_data(monkeypatch, tmp_path):
    monkeypatch.setattr(validation, "PRODUCTION_RAW_HDF5", tmp_path / "missing_stock_daily.h5")
    monkeypatch.setattr(validation, "_load_qlib_style_source", lambda: (_ for _ in ()).throw(FileNotFoundError("missing Qlib style data")))
    monkeypatch.setattr(validation, "_PIT_STATUS_FRAME_CACHE", None)
    monkeypatch.setattr(validation, "_STYLE_AUDIT_FRAME_CACHE", None)


def _write_valid_seed_artifacts(run_dir, *, execute_qlib=True):
    run_dir.mkdir(parents=True)
    portfolio_dir = run_dir / "portfolio_analysis"
    portfolio_dir.mkdir(parents=True)
    experiment = default_r1_experiment({"baseline_kind": "model_validation_test"})
    manifest = {
        "model_system_version": "model",
        "model_run_id": "m0703-validation-s42",
        "round_group_id": "mr0703-validation",
        "seed": 42,
        "experiment": experiment,
        "runner": {"execute_qlib": execute_qlib, "direct_qlib_error": ""},
        "resolved_windows": {"segments": experiment["segments"]},
        "resolved_processors": experiment["qlib_processors"],
        "resolved_portfolio_params": {
            "portfolio": DEFAULT_PORTFOLIO,
            "benchmark": "000300sh",
            "deal_price": "open",
            "limit_threshold": LIMIT_THRESHOLD,
            "portfolio_artifacts": {
                "report_pkl": str(portfolio_dir / "report_normal_1day.pkl"),
                "positions_pkl": str(portfolio_dir / "positions_normal_1day.pkl"),
                "summary_file": str(portfolio_dir / "summary.json"),
            },
        },
        "artifacts": {
            "label": str(run_dir / "label.pkl"),
            "params": str(run_dir / "params.pkl"),
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    metrics = {
        "annualized_ret": 0.2,
        "excess_annualized_ret_with_cost": 0.2,
        "excess_information_ratio_with_cost": 0.8,
        "max_drawdown": -0.1,
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    dates = pd.date_range("2026-01-01", periods=25, freq="B")
    pd.DataFrame(
        {
            "account": [1_000_000 + i * 1000 for i in range(25)],
            "return": [0.002] * 25,
            "turnover": [0.2] * 25,
            "cost": [0.0002] * 25,
            "bench": [0.0005] * 25,
        },
        index=dates,
    ).to_pickle(run_dir / "ret.pkl")
    pred_index = pd.MultiIndex.from_product([dates[:3], ["000001sz", "000002sz"]], names=["datetime", "instrument"])
    pd.Series([0.1, 0.2, 0.3, 0.4, 0.5, 0.6], index=pred_index, name="score").to_pickle(run_dir / "pred.pkl")
    pd.Series([0.01, 0.02, 0.03, 0.04, 0.05, 0.06], index=pred_index, name="LABEL0").to_pickle(run_dir / "label.pkl")
    pd.Series({"seed": 42, "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm"}).to_pickle(run_dir / "params.pkl")
    pd.DataFrame({"return": [0.001], "cost": [0.0001], "bench": [0.0]}).to_pickle(portfolio_dir / "report_normal_1day.pkl")
    pd.Series({"000001sz": 0.1, "000002sz": 0.2}).to_pickle(portfolio_dir / "positions_normal_1day.pkl")
    (portfolio_dir / "summary.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    return metrics


def test_validation_accepts_direct_qlib_artifacts_with_review_warnings(tmp_path, monkeypatch):
    _isolate_validation_reference_data(monkeypatch, tmp_path)
    run_dir = tmp_path / "run"
    metrics = _write_valid_seed_artifacts(run_dir, execute_qlib=True)

    audit = audit_seed_run(
        {
            "model_run_id": "m0703-validation-s42",
            "round_group_id": "mr0703-validation",
            "seed": 42,
            "metrics": metrics,
            "artifact_dir": str(run_dir),
        }
    )

    assert audit["status"] == "review_required"
    assert audit["hard_blocks"] == []
    assert audit["checks"]["artifact_integrity"]["status"] == "clean"
    assert audit["checks"]["manifest_contract"]["status"] == "clean"
    assert audit["checks"]["portfolio_artifacts"]["status"] == "clean"
    assert "prediction_universe_below_20_instruments" in audit["checks"]["tradability_exposure"]["warnings"]
    assert (run_dir / "validation_audit.json").exists()


def test_validation_blocks_shadow_artifacts_from_production_admission(tmp_path, monkeypatch):
    _isolate_validation_reference_data(monkeypatch, tmp_path)
    run_dir = tmp_path / "run"
    metrics = _write_valid_seed_artifacts(run_dir, execute_qlib=False)

    audit = audit_seed_run(
        {
            "model_run_id": "m0703-validation-s42",
            "round_group_id": "mr0703-validation",
            "seed": 42,
            "metrics": metrics,
            "artifact_dir": str(run_dir),
        }
    )

    assert audit["status"] == "blocked"
    assert "manifest_contract" in audit["hard_blocks"]
    assert "shadow_runner_not_production_eligible" in audit["checks"]["manifest_contract"]["errors"]


def test_validation_blocks_empty_metrics_json(tmp_path, monkeypatch):
    _isolate_validation_reference_data(monkeypatch, tmp_path)
    run_dir = tmp_path / "run"
    _write_valid_seed_artifacts(run_dir, execute_qlib=True)
    (run_dir / "metrics.json").write_text("{}", encoding="utf-8")

    audit = audit_seed_run(
        {
            "model_run_id": "m0703-validation-s42",
            "round_group_id": "mr0703-validation",
            "seed": 42,
            "metrics": {},
            "artifact_dir": str(run_dir),
        }
    )

    assert audit["status"] == "blocked"
    assert "artifact_integrity" in audit["hard_blocks"]
    assert "metrics_json_empty" in audit["checks"]["artifact_integrity"]["errors"]


def test_prediction_st_exposure_uses_pit_status_by_date(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "_PIT_STATUS_FRAME_CACHE", None)
    hdf_path = tmp_path / "stock_daily.h5"
    pd.DataFrame(
        {
            "code": ["000001.SZ", "000002.SZ", "000001.SZ", "000002.SZ"],
            "kline_time": [
                pd.Timestamp("2026-01-01"),
                pd.Timestamp("2026-01-01"),
                pd.Timestamp("2026-01-02"),
                pd.Timestamp("2026-01-02"),
            ],
            "st_status": ["NORMAL", "ST", "NORMAL", "NORMAL"],
            "list_status": ["L", "L", "L", "L"],
            "SECURITY_NAME": ["平安银行", "*ST测试", "平安银行", "普通股票"],
        }
    ).to_hdf(hdf_path, key="daily")
    monkeypatch.setattr(validation, "PRODUCTION_RAW_HDF5", hdf_path)
    pred = pd.Series(
        [2.0, 1.0, 2.0, 1.0],
        index=pd.MultiIndex.from_tuples(
            [
                (pd.Timestamp("2026-01-01"), "000001sz"),
                (pd.Timestamp("2026-01-01"), "000002sz"),
                (pd.Timestamp("2026-01-02"), "000001sz"),
                (pd.Timestamp("2026-01-02"), "000002sz"),
            ],
            names=["datetime", "instrument"],
        ),
        name="score",
    )

    result = validation._prediction_st_exposure(pred)

    assert result["available"] is True
    assert result["status_match_ratio"] == 1.0
    assert result["topk_avg_st_like_ratio"] == 0.25
    assert result["top50_avg_st_like_ratio"] == 0.25
    assert result["top50_p95_st_like_ratio"] == 0.475


def test_prediction_style_exposure_uses_qlib_percentile_source(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "_STYLE_AUDIT_FRAME_CACHE", None)
    idx = pd.MultiIndex.from_product(
        [[pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")], ["000001sz", "000002sz", "000003sz", "000004sz", "000005sz"]],
        names=["datetime", "instrument"],
    )
    style_source = pd.DataFrame(
        {
            "$total_mv": [10.0, 50.0, 100.0, 500.0, 1000.0] * 2,
            "$float_mv": [8.0, 40.0, 80.0, 400.0, 800.0] * 2,
            "$roe": [5.0, 7.0, 8.0, 12.0, 20.0] * 2,
            "$eps": [1.0, 1.5, 2.0, 2.5, 3.0, 1.1, 1.6, 2.1, 2.6, 3.1],
            "$net_profit": [10.0, 15.0, 20.0, 25.0, 30.0, 11.0, 16.0, 21.0, 26.0, 31.0],
        },
        index=idx,
    )
    monkeypatch.setattr(validation, "_load_qlib_style_source", lambda: style_source)
    pred = pd.Series(
        [5.0, 4.0, 3.0, 2.0, 1.0] * 2,
        index=idx,
        name="score",
    )

    result = validation._prediction_style_exposure(pred)

    assert result["available"] is True
    assert result["top20_prediction"]["style_row_match_ratio"] == 1.0
    assert result["top20_prediction"]["avg_small_cap_ratio"] == 1 / 5
    assert result["top20_prediction"]["avg_blue_chip_ratio"] == 2 / 5
    assert result["risk_flags"]["top20_small_cap_ratio"] == 1 / 5
