from __future__ import annotations

import json
import sqlite3

import pytest

from domain.model.production_refit import production_refit_model
from domain.model.qlib_runner import run_round, submit_experiment
from domain.model.research_confirmation import confirm_research_round, register_research_screening_round
from domain.model.scoring import score_round
from domain.model.state_store import ModelStateStore
from domain.model.walk_forward import start_production_rolling
from storage.model_registry import ModelRegistry


@pytest.fixture(autouse=True)
def _feature_set_preflight_passes(monkeypatch):
    monkeypatch.setattr(
        "domain.model.qlib_runner.model_feature_set_preflight",
        lambda feature_set_id: {"passed": True, "feature_set_id": feature_set_id, "errors": [], "warnings": []},
    )


def _round(state: ModelStateStore, feature_set_id: str = "fs-reg") -> dict:
    submitted = submit_experiment(
        feature_set_id=feature_set_id,
        experiment={
            "feature_missing_strategy": "qlib_processor_only",
            "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm",
            "metrics_by_seed": {
                "42": {"excess_annualized_ret_with_cost": 0.45, "excess_information_ratio_with_cost": 1.5, "max_drawdown": -0.12, "rank_ic": 0.04, "rank_icir": 0.30},
                "17": {"excess_annualized_ret_with_cost": 0.42, "excess_information_ratio_with_cost": 1.4, "max_drawdown": -0.13, "rank_ic": 0.035, "rank_icir": 0.28},
                "83": {"excess_annualized_ret_with_cost": 0.40, "excess_information_ratio_with_cost": 1.3, "max_drawdown": -0.14, "rank_ic": 0.033, "rank_icir": 0.26},
            },
        },
        state=state,
    )
    return submitted["round_group"]


def _rolling_seed(seed: int) -> dict:
    fold = {"excess_annualized_ret_with_cost": 0.42, "excess_information_ratio_with_cost": 1.35, "max_drawdown": -0.13}
    return {
        "status": "complete",
        "seed": seed,
        "rolling_metrics": {"excess_annualized_ret_with_cost": 0.45, "excess_information_ratio_with_cost": 1.45, "max_drawdown": -0.14},
        "fold_portfolio_metrics": {f"fold_{index}": dict(fold) for index in range(4)},
        "reliability": {"predictions": True, "continuity": True, "integrity": True},
    }


def test_research_screening_is_registered_as_research_not_candidate(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    registry = ModelRegistry(tmp_path / "registry.db")
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.research_confirmation.audit_seed_run", lambda row: {"status": "clean", "hard_blocks": [], "warnings": []})
    round_row = _round(state)
    run_round(round_group_id=round_row["round_group_id"], state=state)
    score_round(round_row["round_group_id"], state=state)

    result = register_research_screening_round(round_row["round_group_id"], state=state, registry=registry)

    assert result["ok"] is True
    assert result["asset_status"] == "research"
    assert len(registry.list_models("research")) == 1
    assert registry.list_models("candidate") == []


def test_three_seed_confirmation_registers_only_official_seed42(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    registry = ModelRegistry(tmp_path / "registry.db")
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.research_confirmation.audit_seed_run", lambda row: {"status": "clean", "hard_blocks": [], "warnings": []})
    round_row = _round(state)
    run_round(round_group_id=round_row["round_group_id"], state=state)
    score_round(round_row["round_group_id"], state=state)

    result = confirm_research_round(
        round_row["round_group_id"],
        state=state,
        registry=registry,
        execute_qlib=False,
        write_registry=True,
    )

    assert result["ok"] is True
    assert {row["seed"] for row in state.list_seed_runs(round_group_id=round_row["round_group_id"])} == {42, 17, 83}
    research_models = registry.list_models("research")
    assert len(research_models) == 1
    assert json.loads(research_models[0]["metadata"])["seed"] == 42


def test_registry_status_column_and_metadata_stay_aligned(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.db")
    model_id = registry.register(
        model_run_id="alignment-run",
        feature_set_id="fs-alignment",
        status="research",
        metadata={"asset_status": "candidate", "model_system_version": "model"},
    )
    row = registry.get(model_id)
    assert row["status"] == "research"
    assert json.loads(row["metadata"])["asset_status"] == "research"

    registry.set_production(model_id)
    row = registry.get(model_id)
    assert row["status"] == "production"
    assert json.loads(row["metadata"])["asset_status"] == "production"

    registry.archive(model_id)
    row = registry.get(model_id)
    assert row["status"] == "archived"
    assert json.loads(row["metadata"])["asset_status"] == "archived"
    assert registry.summary()["research"] == 0


def test_registry_status_metadata_sync_is_explicit_and_idempotent(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.db")
    model_id = registry.register(model_run_id="legacy-run", status="research")
    conn = sqlite3.connect(registry.db_path)
    conn.execute("UPDATE models SET metadata=? WHERE model_id=?", ('{"asset_status":"candidate"}', model_id))
    conn.commit()
    conn.close()

    first = registry.synchronize_asset_status_metadata()
    second = registry.synchronize_asset_status_metadata()
    assert first["changed"] == 1
    assert second["changed"] == 0
    assert json.loads(registry.get(model_id)["metadata"])["asset_status"] == "research"


def test_only_formal_three_seed_rolling_creates_one_aggregate_candidate(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    registry = ModelRegistry(tmp_path / "registry.db")
    round_row = _round(state)
    experiment = dict(round_row["experiment"])
    experiment["research_metadata"] = {"research_confirmation": {"status": "passed"}, "confirmed_research_score": 75.0}
    state.upsert_round({**round_row, "experiment": experiment})
    monkeypatch.setattr("domain.model.walk_forward.ROLLING_ROOT", tmp_path / "rolling")
    called: list[int] = []

    def runner(seed, campaign_id, feature_set_id, source_round_group_id):
        called.append(seed)
        row = _rolling_seed(seed)
        row["rolling_metrics"]["excess_annualized_ret_with_cost"] = {42: 0.45, 17: 0.44, 83: 0.43}[seed]
        return row

    result = start_production_rolling(
        round_row["round_group_id"],
        state=state,
        registry=registry,
        seed_runner=runner,
        campaign_id="campaign-test",
    )

    assert result["ok"] is True
    assert result["status"] == "candidate"
    assert called == [42, 17, 83]
    candidates = registry.list_models("candidate")
    assert len(candidates) == 1
    metadata = json.loads(candidates[0]["metadata"])
    assert candidates[0]["excess_annualized_ret_with_cost"] == 0.45
    assert metadata["rolling_campaign_id"] == "campaign-test"
    assert metadata["source_round_group_id"] == round_row["round_group_id"]
    assert metadata["production_refit_seed"] == 42

    repeated = start_production_rolling(
        round_row["round_group_id"],
        state=state,
        registry=registry,
        seed_runner=runner,
        campaign_id="campaign-test",
    )
    assert repeated["candidate_model_id"] == result["candidate_model_id"]
    assert len(registry.list_models("candidate")) == 1

    called.clear()
    resumed = start_production_rolling(
        round_row["round_group_id"],
        state=state,
        registry=registry,
        seed_runner=runner,
        campaign_id="campaign-test",
        resume=True,
    )
    assert resumed["candidate_model_id"] == result["candidate_model_id"]
    assert called == []


def test_production_refit_rejects_nonrolling_candidate_and_uses_fixed_seed42(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    registry = ModelRegistry(tmp_path / "registry.db")
    round_row = _round(state, "fs-prod")
    nonrolling_id = registry.register(model_run_id="legacy-candidate", feature_set_id="fs-prod", status="candidate", metadata={"model_system_version": "model"})
    rejected = production_refit_model(model_id=nonrolling_id, registry=registry, dry_run=True)
    assert rejected["err"] == "formal_rolling_candidate_required"

    candidate_id = registry.register(
        model_run_id="rolling-campaign",
        feature_set_id="fs-prod",
        status="candidate",
        metadata={
            "model_system_version": "model",
            "evaluation_mode": "production",
            "rolling_campaign_id": "campaign",
            "source_round_group_id": round_row["round_group_id"],
            "feature_set_id": "fs-prod",
        },
    )
    monkeypatch.setattr("domain.model.production_refit.ModelStateStore", lambda: state, raising=False)
    # The implementation imports the class inside the function.
    monkeypatch.setattr("domain.model.state_store.ModelStateStore", lambda: state)
    preview = production_refit_model(model_id=candidate_id, registry=registry, dry_run=True)
    assert preview["ok"] is True
    assert preview["seed"] == 42
    assert preview["feature_set_id"] == "fs-prod"

    monkeypatch.setattr("domain.model.production_refit.MODEL_RUNS_ROOT", tmp_path / "production-runs")
    monkeypatch.setattr("domain.model.production_refit.MODEL_ACTIVE_PRODUCTION", tmp_path / "active-production.json")
    monkeypatch.setattr(
        "domain.model.production_refit.load_feature_set_manifest",
        lambda feature_set_id: {
            "feature_set_id": feature_set_id,
            "combined_factors_file": str(tmp_path / "combined_factors_df.parquet"),
            "latest_date": "2026-06-30",
        },
    )
    monkeypatch.setattr("domain.model.production_refit.audit_seed_run", lambda row: {"status": "clean", "hard_blocks": [], "warnings": []})
    first = production_refit_model(model_id=candidate_id, registry=registry, execute_qlib=False)
    repeated = production_refit_model(model_id=candidate_id, registry=registry, execute_qlib=False)
    assert first["ok"] is True
    assert repeated["reused_existing_production"] is True
    assert repeated["production_model_id"] == first["production_model_id"]
    assert len(registry.list_models("production")) == 1
    pointer = json.loads((tmp_path / "active-production.json").read_text(encoding="utf-8"))
    assert pointer["model_id"] == first["production_model_id"]
    manifest = json.loads((tmp_path / "production-runs" / first["production_model_run_id"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["platform_combined_factors_file"] == str(tmp_path / "combined_factors_df.parquet")
    assert manifest["latest_date"] == "2026-06-30"


def test_manual_promotion_exception_requires_reason_and_reliable_seed42_campaign(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path / "state")
    registry = ModelRegistry(tmp_path / "registry.db")
    round_row = _round(state, "fs-manual")
    rolling_root = tmp_path / "rolling"
    campaign_id = "model_roll_manual_test"
    campaign_dir = rolling_root / campaign_id
    result_path = tmp_path / "seed42-result.json"
    result_path.write_text("{}", encoding="utf-8")
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "evaluation_mode": "production",
                "status": "research",
                "decision": "stop_after_seed42",
                "candidate_created": False,
                "feature_set_id": "fs-manual",
                "source_round_group_id": round_row["round_group_id"],
                "seed_results": {
                    "42": {
                        "status": "complete",
                        "rolling_metrics": {"excess_annualized_ret_with_cost": 0.4},
                        "fold_portfolio_metrics": {f"wf{i}": {} for i in range(1, 5)},
                        "reliability": {"predictions": True, "continuity": True, "integrity": True},
                        "artifacts": {"result": str(result_path)},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("domain.model.production_refit.MODEL_ROLLING_ROOT", rolling_root)
    monkeypatch.setattr("domain.model.state_store.ModelStateStore", lambda: state)

    ordinary = production_refit_model(model_run_id=campaign_id, registry=registry, dry_run=True)
    assert ordinary["err"] == "candidate_model_not_found"

    preview = production_refit_model(
        model_run_id=campaign_id,
        registry=registry,
        dry_run=True,
        manual_override_reason="operator explicitly approved exception",
    )
    assert preview["ok"] is True
    assert preview["seed"] == 42
    assert preview["feature_set_id"] == "fs-manual"
    assert preview["manual_promotion_exception"]["gate_bypass_scope"] == "candidate_admission_only"
    assert registry.list_models("candidate") == []
