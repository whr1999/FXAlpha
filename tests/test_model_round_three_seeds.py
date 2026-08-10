from __future__ import annotations

import json

import pytest

from domain.model.qlib_runner import run_round, submit_experiment
from domain.model.state_store import ModelStateStore


@pytest.fixture(autouse=True)
def _feature_set_preflight_passes(monkeypatch):
    monkeypatch.setattr(
        "domain.model.qlib_runner.model_feature_set_preflight",
        lambda feature_set_id: {"passed": True, "feature_set_id": feature_set_id, "errors": [], "warnings": []},
    )


def _experiment():
    return {
        "feature_missing_strategy": "qlib_processor_only",
        "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm",
        "metrics_by_seed": {
            "42": {"annualized_ret": 0.2, "excess_information_ratio_with_cost": 1.0, "max_drawdown": -0.1},
            "17": {"annualized_ret": 0.1, "excess_information_ratio_with_cost": 0.5, "max_drawdown": -0.12},
            "83": {"annualized_ret": -0.1, "excess_information_ratio_with_cost": -0.4, "max_drawdown": -0.2},
        },
    }


def test_submit_experiment_persists_cross_round_comparison_panel(tmp_path):
    state = ModelStateStore(runtime_root=tmp_path)
    result = submit_experiment(feature_set_id="fs-test", experiment=_experiment(), state=state)

    assert result["ok"] is True
    round_group = result["round_group"]
    assert round_group["seed_set"] == [42, 17, 83]
    assert round_group["seed_policy"]["mode"] == "staged_screening_then_confirmation"
    assert round_group["seed_policy"]["executed_seed_set"] == [42]
    assert round_group["seed_policy"]["cross_round_stable"] is True

    rerun = submit_experiment(
        feature_set_id="fs-test",
        experiment={**_experiment(), "round_group_id": round_group["round_group_id"]},
        state=state,
    )
    assert rerun["round_group"]["seed_set"] == round_group["seed_set"]

    other_round = submit_experiment(
        feature_set_id="fs-test",
        experiment={**_experiment(), "qlib_model_kwargs": {"learning_rate": 0.07}},
        state=state,
    )
    assert other_round["round_group"]["seed_set"] == round_group["seed_set"]


def test_submit_experiment_rejects_external_seed_set_for_new_round(tmp_path):
    state = ModelStateStore(runtime_root=tmp_path)

    result = submit_experiment(feature_set_id="fs-test", experiment={**_experiment(), "seed_set": [42, 17, 83]}, state=state)

    assert result["ok"] is False
    assert "seed_set_must_be_generated_by_submit_experiment" in result["validation_result"]["errors"]


def test_run_round_creates_only_seed42_screening_run(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    result = submit_experiment(feature_set_id="fs-test", experiment=_experiment(), state=state)
    round_group_id = result["round_group"]["round_group_id"]

    run_result = run_round(round_group_id=round_group_id, state=state)

    assert run_result["ok"] is True
    seed_runs = state.list_seed_runs(round_group_id=round_group_id)
    assert len(seed_runs) == 1
    assert {row["status"] for row in seed_runs} == {"completed"}
    assert len({row["model_run_id"] for row in seed_runs}) == 1
    assert all(row["artifact_dir"] for row in seed_runs)


def test_run_round_manifest_records_contract_params_for_each_seed(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    result = submit_experiment(
        feature_set_id="fs-test",
        experiment=_experiment(),
        state=state,
    )
    round_group_id = result["round_group"]["round_group_id"]

    run_result = run_round(round_group_id=round_group_id, state=state)

    assert run_result["ok"] is True
    for row in state.list_seed_runs(round_group_id=round_group_id):
        manifest = json.loads((tmp_path / "runs" / row["model_run_id"] / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["seed"] == row["seed"]
        assert manifest["resolved_reweight_params"]["requested_sample_weight_policy"] == "top50_smooth2_bottom50_smooth1p5_mean_norm"
        assert manifest["resolved_reweight_params"]["effective_sample_weight_policy"] == "top50_smooth2_bottom50_smooth1p5_mean_norm"
        assert manifest["resolved_portfolio_params"]["portfolio"]["topk"] == 20
        assert manifest["resolved_processors"]["infer_processors"][1]["class"] == "RobustZScoreNorm"
        assert manifest["resolved_windows"]["segments"] == manifest["experiment"]["segments"]
        assert manifest["resolved_windows"]["train"] == manifest["experiment"]["segments"]["train"]
        assert manifest["feature_set_preflight"]["passed"] is True
        assert "feature_set_preflight_passed_before_run" in manifest["config_audit"]["checks"]
        metrics = json.loads((tmp_path / "runs" / row["model_run_id"] / "metrics.json").read_text(encoding="utf-8"))
        assert metrics


def test_execute_qlib_seed_worker_failure_marks_seed_and_round_failed(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.qlib_runner.QLIB0627_ROOT", tmp_path)

    def failed_worker(**kwargs):
        return {
            "ok": False,
            "error": "seed_worker_timeout",
            "timeout_seconds": 60,
            "stderr_tail": "timeout",
        }

    monkeypatch.setattr("domain.model.qlib_runner._run_direct_qlib_seed_isolated", failed_worker)
    result = submit_experiment(feature_set_id="fs-test", experiment=_experiment(), state=state)
    round_group_id = result["round_group"]["round_group_id"]

    run_result = run_round(round_group_id=round_group_id, state=state, execute_qlib=True)

    assert run_result["ok"] is False
    assert run_result["err"] == "seed_run_failed"
    assert state.get_round(round_group_id)["status"] == "failed"
    seed_runs = state.list_seed_runs(round_group_id=round_group_id)
    assert len(seed_runs) == 1
    assert {row["status"] for row in seed_runs} == {"failed"}
    for row in seed_runs:
        manifest = json.loads((tmp_path / "runs" / row["model_run_id"] / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["config_audit"]["passed"] is False
        assert manifest["runner"]["direct_qlib_error"] == "seed_worker_timeout"
