from __future__ import annotations

import json

import pandas as pd
import pytest

from domain.model.qlib_runner import run_round, submit_experiment
from domain.model.scoring import individual_score_components, score_round, seed_consistency_components
from domain.model.state_store import ModelStateStore


@pytest.fixture(autouse=True)
def _feature_set_preflight_passes(monkeypatch):
    monkeypatch.setattr(
        "domain.model.qlib_runner.model_feature_set_preflight",
        lambda feature_set_id: {"passed": True, "feature_set_id": feature_set_id, "errors": [], "warnings": []},
    )


def _submit_with_ordered_metrics(state, *, feature_set_id: str, metrics: list[dict]):
    experiment = {
        "feature_missing_strategy": "qlib_processor_only",
        "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm",
    }
    round_group = submit_experiment(feature_set_id=feature_set_id, experiment=experiment, state=state)["round_group"]
    seeds = round_group["seed_set"]
    round_group["experiment"]["metrics_by_seed"] = {str(seed): metric for seed, metric in zip(seeds, metrics)}
    state.upsert_round(round_group)
    return round_group, seeds


def test_research_score_assigns_score_to_seed42_only(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    round_group, seeds = _submit_with_ordered_metrics(
        state,
        feature_set_id="fs-test",
        metrics=[
            {"annualized_ret": 0.45, "excess_annualized_ret_with_cost": 0.45, "excess_information_ratio_with_cost": 1.6, "max_drawdown": -0.12, "rank_ic": 0.03, "rank_icir": 0.25},
            {"annualized_ret": -0.12, "excess_annualized_ret_with_cost": -0.12, "excess_information_ratio_with_cost": -0.3, "max_drawdown": -0.25, "rank_ic": -0.01, "rank_icir": -0.05},
            {"annualized_ret": 0.22, "excess_annualized_ret_with_cost": 0.22, "excess_information_ratio_with_cost": 0.95, "max_drawdown": -0.14, "rank_ic": 0.02, "rank_icir": 0.12},
        ],
    )
    run_round(round_group_id=round_group["round_group_id"], state=state)

    scored = score_round(round_group["round_group_id"], state=state)

    assert scored["ok"] is True
    assert len(scored["results"]) == 1
    assert scored["results"][0]["seed"] == 42
    assert scored["results"][0]["research_score"] == scored["research_score"]
    assert scored["results"][0]["score_review_version"] == "model_research_score_v1"
    assert scored["results"][0]["decision"] == "eligible_for_session_comparison"
    assert state.get_seed_run(scored["results"][0]["model_run_id"])["score"]["score_review_version"]


def test_score_review_hard_blocks_negative_cost_adjusted_excess_return(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    round_group, seeds = _submit_with_ordered_metrics(
        state,
        feature_set_id="fs-test",
        metrics=[
            {"annualized_ret": -0.01, "excess_annualized_ret_with_cost": -0.01, "excess_information_ratio_with_cost": -0.05, "max_drawdown": -0.12, "rank_ic": 0.08, "rank_icir": 0.6},
            {"annualized_ret": 0.25, "excess_annualized_ret_with_cost": 0.25, "excess_information_ratio_with_cost": 1.1, "max_drawdown": -0.12, "rank_ic": 0.02, "rank_icir": 0.2},
            {"annualized_ret": 0.20, "excess_annualized_ret_with_cost": 0.20, "excess_information_ratio_with_cost": 0.9, "max_drawdown": -0.13, "rank_ic": 0.02, "rank_icir": 0.15},
        ],
    )
    run_round(round_group_id=round_group["round_group_id"], state=state)

    scored = score_round(round_group["round_group_id"], state=state)

    blocked = scored["results"][0]
    assert blocked["seed"] == 42
    assert blocked["decision"] == "research_hard_flaw"
    assert "excess_annualized_ret_with_cost_below_10pct" in blocked["hard_blocks"]
    assert "excess_information_ratio_with_cost_below_0p5" in blocked["hard_blocks"]


def test_research_score_does_not_require_cross_seed_prediction_correlation(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    metrics = {
        "annualized_ret": 0.45,
        "excess_annualized_ret_with_cost": 0.45,
        "excess_information_ratio_with_cost": 1.1,
        "max_drawdown": -0.12,
        "rank_ic": 0.03,
        "rank_icir": 0.25,
        "top10_holding_overlap": 0.0,
    }
    round_group, _seeds = _submit_with_ordered_metrics(state, feature_set_id="fs-test", metrics=[metrics, metrics, metrics])
    run_round(round_group_id=round_group["round_group_id"], state=state)

    index = pd.MultiIndex.from_product(
        [pd.to_datetime(["2026-01-05", "2026-01-06"]), [f"SH{i:06d}" for i in range(20)]],
        names=["datetime", "instrument"],
    )
    prediction = pd.Series(list(range(20)) * 2, index=index, name="score", dtype="float64")
    for row in state.list_seed_runs(round_group_id=round_group["round_group_id"]):
        run_dir = tmp_path / "runs" / row["model_run_id"]
        prediction.to_pickle(run_dir / "pred.pkl")

    scored = score_round(round_group["round_group_id"], state=state)

    assert scored["ok"] is True
    assert "round_consistency" not in scored
    assert len(state.list_seed_runs(round_group_id=round_group["round_group_id"])) == 1


def test_missing_prediction_correlation_is_not_given_neutral_credit():
    components, warnings = seed_consistency_components(
        {
            42: {"annualized_ret": 0.2, "sharpe": 0.8, "top10_holding_overlap": 1.0},
            2: {"annualized_ret": 0.2, "sharpe": 0.8, "top10_holding_overlap": 1.0},
            88: {"annualized_ret": 0.2, "sharpe": 0.8, "top10_holding_overlap": 1.0},
        }
    )

    assert components["prediction_rank_corr_score"] == 0.0
    assert "missing_prediction_rank_correlation_scored_zero" in warnings
    assert "top10_holding_overlap_score" not in components


def test_turnover_is_the_only_trading_activity_score_component():
    components, warnings = individual_score_components({"turnover": 0.4})

    assert components["turnover_score"] == 100.0
    assert "cost_sensitivity_score" not in components
    assert "trading_cost_score" not in components
    assert "missing_cost_sensitivity" not in warnings
