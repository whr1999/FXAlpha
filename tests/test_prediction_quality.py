from pathlib import Path

import json

import pandas as pd
import pytest

prediction = pytest.importorskip("domain.trading.prediction")
signals = pytest.importorskip("domain.trading.signals")


def test_short_runtime_feature_cache_is_rejected(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(prediction, "PREDICTION_FEATURE_RUNTIME_ROOT", tmp_path)
    model_run_id = "run-quality"
    cache_dir = tmp_path / model_run_id
    cache_dir.mkdir(parents=True)

    idx = pd.MultiIndex.from_product(
        [[pd.Timestamp("2026-05-28")], ["000001sz", "600000sh"]],
        names=["datetime", "instrument"],
    )
    feature_file = cache_dir / "combined_factors_2026-05-28.parquet"
    pd.DataFrame({("feature", "alpha_1"): [1.0, 2.0]}, index=idx).to_parquet(feature_file)
    (cache_dir / "manifest_2026-05-28.json").write_text(
        """
        {
          "model_run_id": "run-quality",
          "target_date": "2026-05-28",
          "latest_date": "2026-05-28",
          "combined_factors_file": "%s"
        }
        """ % feature_file,
        encoding="utf-8",
    )

    assert prediction._cached_prediction_feature_file({"model_run_id": model_run_id}, "2026-05-28") is None


def test_processed_feature_quality_detects_all_zero_features():
    frame = pd.DataFrame({"alpha_1": [0.0, 0.0, 0.0], "alpha_2": [0.0, 0.0, 0.0]})

    quality = prediction._prepared_feature_quality(frame)

    assert quality["feature_count"] == 2
    assert quality["zero_var_feature_count"] == 2
    assert quality["all_nan_feature_count"] == 0


def test_direct_production_artifacts_are_valid_prediction_inputs(tmp_path: Path):
    run_dir = tmp_path / "direct-production"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps({"runner": {"main_chain": "direct_qlib0627_workflow"}}),
        encoding="utf-8",
    )
    (run_dir / "model.pkl").write_bytes(b"model")
    (run_dir / "params.pkl").write_bytes(b"params")
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-06-30"), "000001sz")],
        names=["datetime", "instrument"],
    )
    pd.DataFrame({"score": [0.1]}, index=index).to_pickle(run_dir / "pred.pkl")

    validation = prediction.validate_pred_inputs({"recorder_run_dir": str(run_dir)}, None)
    loaded = prediction.load_pred_dataframe(run_dir)

    assert validation["required_artifacts_ok"] is True
    assert validation["artifact_layout"] == "direct_qlib"
    assert loaded.iloc[0]["score"] == pytest.approx(0.1)


def test_prediction_feature_rebuild_preserves_static_factor_universe(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(prediction, "PREDICTION_FEATURE_RUNTIME_ROOT", tmp_path / "runtime")
    historical_path = tmp_path / "factor.parquet"
    historical_index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-01-02"), "000001sz")],
        names=["datetime", "instrument"],
    )
    pd.DataFrame({"alpha": [1.0]}, index=historical_index).to_parquet(historical_path)

    monkeypatch.setattr(
        prediction,
        "_model_factor_records",
        lambda _context: [
            {
                "factor_id": "f-static",
                "data_column": "alpha",
                "data_path": str(historical_path),
                "expression": "rank(close)",
            }
        ],
    )
    requested_columns = set()

    def fake_load_market_data(**kwargs):
        requested_columns.update(kwargs["required_columns"])
        return pd.DataFrame(
            {
                "trade_date": [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-05")],
                "stock_code": ["sz.000001", "sz.000002"],
                "close": [10.0, 20.0],
            }
        )

    computed_index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2026-01-05"), "000001sz"),
            (pd.Timestamp("2026-01-05"), "000002sz"),
        ],
        names=["datetime", "instrument"],
    )
    monkeypatch.setattr(prediction, "_load_market_data", fake_load_market_data)
    monkeypatch.setattr(
        prediction,
        "_compute_factor_from_market_df",
        lambda _market, _expression: pd.DataFrame({"rank(close)": [0.5, 1.0]}, index=computed_index),
    )
    monkeypatch.setattr(prediction, "_prediction_warmup_start_date", lambda *_args, **_kwargs: "2026-01-01")

    manifest = prediction.build_prediction_feature_cache(
        {"model_id": "model-static", "model_run_id": "run-static"},
        target_date="2026-01-05",
        start_date="2026-01-02",
    )

    rebuilt = pd.read_parquet(manifest["combined_factors_file"])
    target_rows = rebuilt.xs(pd.Timestamp("2026-01-05"), level="datetime")
    assert set(target_rows.index) == {"000001sz", "000002sz"}
    assert "st_status" not in requested_columns
    assert "list_status" not in requested_columns
    assert manifest["feature_universe_policy"] == "adopted_factor_static_universe_no_point_in_time_st_post_filter"


def test_build_target_portfolio_rejects_constant_scores(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(signals, "TARGETS_RUNTIME_ROOT", tmp_path / "targets")
    monkeypatch.setattr(signals, "load_stock_identity_rows", lambda: pd.DataFrame())

    score_file = tmp_path / "score.csv"
    score_df = pd.DataFrame(
        {
            "instrument": [f"000{i:03d}sz" for i in range(1, 51)],
            "score": [0.0036315516039809] * 50,
        }
    )
    score_df.to_csv(score_file, index=False)
    score_meta = {
        "model_id": "m-quality",
        "model_run_id": "run-quality",
        "trade_date": "2026-05-28",
        "score_file": str(score_file),
    }

    with pytest.raises(RuntimeError, match="prediction_score_degenerate"):
        signals.build_target_portfolio(score_meta=score_meta, score_df=score_df, topk=10, total_capital=1_000_000)


def test_build_target_portfolio_records_score_quality(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(signals, "TARGETS_RUNTIME_ROOT", tmp_path / "targets")
    monkeypatch.setattr(signals, "load_stock_identity_rows", lambda: pd.DataFrame())

    score_file = tmp_path / "score.csv"
    score_df = pd.DataFrame(
        {
            "instrument": [f"000{i:03d}sz" for i in range(1, 51)],
            "score": list(range(50, 0, -1)),
        }
    )
    score_df.to_csv(score_file, index=False)
    score_meta = {
        "model_id": "m-quality",
        "model_run_id": "run-quality",
        "trade_date": "2026-05-28",
        "score_file": str(score_file),
    }

    meta = signals.build_target_portfolio(score_meta=score_meta, score_df=score_df, topk=10, total_capital=1_000_000)

    assert meta["score_quality"]["unique_score_count"] == 50
    assert meta["score_quality"]["score_std"] > 0


def test_confidence_cash_contract_keeps_only_strict_boundary_names_and_cash(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(signals, "TARGETS_RUNTIME_ROOT", tmp_path / "targets")
    monkeypatch.setattr(signals, "load_stock_identity_rows", lambda: pd.DataFrame())
    instruments = [f"000{i:03d}sz" for i in range(1, 51)]
    scores = [float(100 - i) for i in range(7)] + [50.0] * 26 + [float(30 - i) for i in range(17)]
    score_df = pd.DataFrame({"instrument": instruments, "score": scores})
    score_file = tmp_path / "score-confidence.csv"
    score_df.to_csv(score_file, index=False)
    score_meta = {
        "model_id": "m-confidence",
        "model_run_id": "run-confidence",
        "trade_date": "2026-06-30",
        "score_file": str(score_file),
    }

    meta = signals.build_target_portfolio(
        score_meta=score_meta,
        score_df=score_df,
        topk=20,
        total_capital=1_000_000,
        strategy_contract_version="confidence_cash_top20_drop2_hold5_open_v1",
        model_confidence_evidence={"status": "available", "trees_built": 1},
    )

    target = pd.read_csv(meta["target_file"])
    assert len(target) == 7
    assert target["target_weight"].tolist() == pytest.approx([0.025] * 7)
    assert meta["target_stock_exposure"] == pytest.approx(0.175)
    assert meta["target_cash_weight"] == pytest.approx(0.825)
    assert meta["confidence"]["confidence_state"] == "weak"
    assert meta["confidence"]["selection_confidence"]["equal_to_boundary"] == 26
    assert meta["confidence"]["model_confidence"]["evidence"]["trees_built"] == 1
