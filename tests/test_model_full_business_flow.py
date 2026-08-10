from __future__ import annotations

from domain.model.contracts import default_r1_experiment
from domain.model.feature_sets import feature_snapshot
from domain.model.research_confirmation import confirm_research_round
from domain.model.qlib_runner import run_round, submit_experiment
from domain.model.scoring import score_round
from domain.model.state_store import ModelStateStore
from storage.model_registry import ModelRegistry


def test_full_shadow_business_flow_from_feature_snapshot_to_registry(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    registry = ModelRegistry(tmp_path / "registry.db")
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(
        "domain.model.qlib_runner.model_feature_set_preflight",
        lambda feature_set_id: {"passed": True, "feature_set_id": feature_set_id, "errors": [], "warnings": []},
    )

    snapshot = feature_snapshot(
        feature_set_id="fs-0703-business",
        factor_ids=[f"f{i}" for i in range(54)],
        feature_missing_strategy="qlib_processor_only",
        dry_run=True,
    )
    assert snapshot["ok"] is True
    assert snapshot["request"]["feature_missing_strategy"] == "qlib_processor_only"

    experiment = default_r1_experiment()
    submitted = submit_experiment(feature_set_id="fs-0703-business", experiment=experiment, state=state)
    assert submitted["ok"] is True
    assert submitted["round_group"]["experiment"]["segments"] == {
        "train": ["2022-01-04", "2024-12-31"],
        "valid": ["2025-01-02", "2025-06-30"],
        "test": ["2025-07-01", "2026-07-01"],
    }
    round_group_id = submitted["round_group"]["round_group_id"]
    seeds = submitted["round_group"]["seed_set"]
    submitted["round_group"]["experiment"]["metrics_by_seed"] = {
        str(seeds[0]): {"annualized_ret": 0.55, "excess_annualized_ret_with_cost": 0.55, "excess_information_ratio_with_cost": 2.0, "max_drawdown": -0.11, "rank_ic": 0.03, "rank_icir": 0.25},
        str(seeds[1]): {"annualized_ret": -0.15, "excess_annualized_ret_with_cost": -0.15, "excess_information_ratio_with_cost": -0.4, "max_drawdown": -0.25, "rank_ic": -0.01, "rank_icir": -0.05},
        str(seeds[2]): {"annualized_ret": 0.62, "excess_annualized_ret_with_cost": 0.62, "excess_information_ratio_with_cost": 2.2, "max_drawdown": -0.10, "rank_ic": 0.04, "rank_icir": 0.3},
    }
    state.upsert_round(submitted["round_group"])

    run = run_round(round_group_id=round_group_id, state=state)
    assert run["ok"] is True
    assert len(run["seed_runs"]) == 1

    scored = score_round(round_group_id, state=state)
    assert scored["ok"] is True
    assert {item["decision"] for item in scored["results"]} == {"eligible_for_session_comparison"}

    confirmed = confirm_research_round(round_group_id, state=state, registry=registry, execute_qlib=False, write_registry=False)
    assert confirmed["ok"] is True
    assert len(state.list_seed_runs(round_group_id=round_group_id)) == 3
    assert confirmed["confirmation"]["status"] == "failed"
    assert registry.list_models("candidate") == []
