from __future__ import annotations

import pandas as pd
import pytest

from services import model_service as svc
from domain.model.state_store import ModelStateStore


@pytest.fixture(autouse=True)
def _isolate_model_trace_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")
    monkeypatch.setattr("domain.model.context.MODEL_MCP_TRACES", tmp_path / "mcp.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "orch.jsonl")


def test_service_context_and_protocol_are_available(tmp_path, monkeypatch):
    protocol = svc.model_tool_protocol()
    context = svc.model_tool_context()

    assert protocol.ok is True
    assert protocol.outputs["protocol"]["model_system_version"] == "model"
    assert "MCP Operating Prompt" in protocol.outputs["prompts"]["mcp"]["content"]
    assert "Research Planner System Prompt" in protocol.outputs["prompts"]["orch"]["content"]
    assert context.ok is True
    assert context.outputs["context_pack"]["model_system_version"] == "model"
    assert "best_seed_selection" in context.outputs["context_pack"]["blocked_actions"]


def test_model_mcp_registers_only_current_model_tools():
    from mcp_servers.model_server import mcp

    names = {tool.name for tool in mcp._tool_manager.list_tools()}

    assert "fxalpha_model_context" in names
    assert "fxalpha_record_model_step" in names
    assert not any(name.startswith("rdagent_model") for name in names)


def test_legacy_http_model_routes_are_thin_model_aliases():
    from api_server import MODEL_GET_ALIASES, MODEL_POST_ALIASES

    aliases = {**MODEL_GET_ALIASES, **MODEL_POST_ALIASES}

    assert aliases["/model0703/status"] == "/model/status"
    assert aliases["/model/tools/run"] == "/model/tools/run-round"
    assert aliases["/model0703/tools/run-round"] == "/model/tools/run-round"
    assert all(target.startswith("/model/") for target in aliases.values())


def test_service_submit_run_score_flow(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.ModelStateStore", lambda: state)
    monkeypatch.setattr("domain.model.scoring.ModelStateStore", lambda: state)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(
        "domain.model.qlib_runner.model_feature_set_preflight",
        lambda feature_set_id: {"passed": True, "feature_set_id": feature_set_id, "errors": [], "warnings": []},
    )
    submit = svc.model_tool_submit_experiment(
        feature_set_id="fs-service",
        experiment={
            "feature_missing_strategy": "qlib_processor_only",
            "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm",
        },
    )

    assert submit.ok is True
    round_group_id = submit.outputs["round_group"]["round_group_id"]
    seeds = submit.outputs["round_group"]["seed_set"]
    round_group = submit.outputs["round_group"]
    round_group["experiment"]["metrics_by_seed"] = {
        str(seeds[0]): {"annualized_ret": 0.3, "excess_annualized_ret_with_cost": 0.3, "excess_information_ratio_with_cost": 1.1, "max_drawdown": -0.1},
        str(seeds[1]): {"annualized_ret": 0.2, "excess_annualized_ret_with_cost": 0.2, "excess_information_ratio_with_cost": 0.8, "max_drawdown": -0.12},
        str(seeds[2]): {"annualized_ret": -0.1, "excess_annualized_ret_with_cost": -0.1, "excess_information_ratio_with_cost": -0.2, "max_drawdown": -0.2},
    }
    state.upsert_round(round_group)
    assert svc.model_tool_run_round(round_group_id).ok is True
    scored = svc.model_tool_score_review(round_group_id)
    assert scored.ok is True
    assert len(scored.outputs["results"]) == 1
    assert scored.outputs["results"][0]["seed"] == 42


def test_research_step_is_v2_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "MODEL_RESEARCH_STEPS", tmp_path / "steps.jsonl")
    result = svc.model_tool_research_step(
        stage="research_score",
        summary="本轮 Seed 42 已完成研究评分。",
        decision="进入会话内比较。",
        next="round_synthesis",
        refs=["runtime/model/runs/example"],
        feature_set_id="fs-test",
        round_group_id="mr0703-test",
        model_run_id="m0703-test",
    )

    assert result.ok is True
    assert result.outputs["schema_version"] == "research_step_v2"
    assert result.outputs["stage_seq"] > 0
    assert result.outputs["evidence_refs"] == ["runtime/model/runs/example"]


def test_backtest_replays_ret_pkl_for_selected_seed_model(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "m0703-test-s42"
    run_dir.mkdir(parents=True)
    ret = pd.DataFrame(
        {
            "return": [0.01, -0.005, 0.02],
            "bench": [0.002, 0.001, -0.003],
            "cost": [0.0005, 0.0004, 0.0006],
            "turnover": [0.2, 0.18, 0.22],
            "account": [1_000_000.0, 1_004_500.0, 1_024_590.0],
        },
        index=pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]),
    )
    ret.to_pickle(run_dir / "ret.pkl")
    pred_index = pd.MultiIndex.from_product(
        [pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]), [f"SH60{i:04d}" for i in range(12)]],
        names=["datetime", "instrument"],
    )
    pred = pd.Series(range(len(pred_index)), index=pred_index, dtype="float64").rename("score")
    pred.to_pickle(run_dir / "pred.pkl")
    portfolio_dir = run_dir / "portfolio_analysis"
    portfolio_dir.mkdir(parents=True)
    (portfolio_dir / "positions_normal_1day.pkl").write_bytes(b"placeholder")
    row = {
        "model_id": "m-reg-0703",
        "model_run_id": "m0703-test-s42",
        "status": "candidate",
        "created_at": "2026-07-04T00:00:00Z",
        "run_dir": str(run_dir),
        "workspace_path": str(run_dir),
        "metadata": {
            "model_system_version": "model",
            "seed": 42,
            "sota_score": 61.2,
            "artifacts": {
                "portfolio": {
                    "positions_pkl": str(portfolio_dir / "positions_normal_1day.pkl"),
                    "portfolio_analysis_dir": str(portfolio_dir),
                }
            },
        },
    }
    monkeypatch.setattr(svc, "_model_registry_rows", lambda status="all", limit=None: [row])

    result = svc.model_backtest(model_run_id="m0703-test-s42", include_daily=True)

    assert result.ok is True
    assert result.outputs["selected_model"]["model_run_id"] == "m0703-test-s42"
    assert result.outputs["curve_available"] is True
    assert result.outputs["point_count"] == 3
    assert len(result.outputs["daily"]) == 3
    curve = result.outputs["curve"]
    assert curve[0]["daily_gross_return"] == pytest.approx(0.01)
    assert curve[0]["daily_net_return"] == pytest.approx(0.0095)
    assert curve[0]["daily_model_return"] == pytest.approx(0.0095)
    assert curve[-1]["model_return"] == pytest.approx(((1.0095 * 0.9946 * 1.0194) - 1.0))
    assert curve[-1]["gross_strategy_cumulative_return"] == pytest.approx(((1.01 * 0.995 * 1.02) - 1.0))
    assert curve[-1]["excess_cumulative_return"] == pytest.approx(
        (1.0 + curve[-1]["strategy_cumulative_return"])
        / (1.0 + curve[-1]["benchmark_cumulative_return"])
        - 1.0
    )
    assert result.outputs["metrics"]["curve_return_basis"] == "after_cost_compounded_nav"
    assert result.outputs["metrics"]["relative_return_basis"] == "net_strategy_nav_divided_by_benchmark_nav"
    assert result.outputs["metrics"]["qlib_annualization_factor"] == 238
    assert result.outputs["daily_breakdown"]["available"] is True
    assert result.outputs["daily_breakdown"]["items"][-1]["holdings"]
    first_day = result.outputs["daily_breakdown"]["items"][0]
    assert first_day["daily_return"] == pytest.approx(0.0095)
    assert first_day["daily_gross_return"] == pytest.approx(0.01)
    assert first_day["daily_excess_return"] == pytest.approx(0.0075)
    assert first_day["cost"] == 0.0005
    assert first_day["cost_value"] == 500.0
    assert first_day["trades"]
    assert sum(row.get("trade_cost") or 0.0 for row in first_day["trades"]) == pytest.approx(first_day["cost_value"])
    assert "by_date" in result.outputs["daily_breakdown"]
    assert result.outputs["stock_contribution"]["method"] == "topk_drop_hold_replay_from_prediction"
    assert result.outputs["period"] == {"start": "2026-01-02", "end": "2026-01-06"}
    assert result.artifacts["ret_pkl"] == str(run_dir / "ret.pkl")
    assert result.artifacts["pred_pkl"] == str(run_dir / "pred.pkl")
    assert result.artifacts["portfolio_artifacts"]["portfolio"]["positions_pkl"] == str(portfolio_dir / "positions_normal_1day.pkl")


def test_backtest_reads_existing_validation_exposure_without_on_demand_generation(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "m0703-test-exposure-s42"
    run_dir.mkdir(parents=True)
    dates = pd.to_datetime(["2026-01-02", "2026-01-05"])
    instruments = [f"{code:06d}sz" for code in range(1, 13)]
    pd.DataFrame(
        {
            "return": [0.01, 0.02],
            "bench": [0.001, 0.002],
            "cost": [0.0005, 0.0006],
            "turnover": [0.2, 0.22],
            "account": [1_000_000.0, 1_020_000.0],
        },
        index=dates,
    ).to_pickle(run_dir / "ret.pkl")
    pred_index = pd.MultiIndex.from_product([dates, instruments], names=["datetime", "instrument"])
    pred = pd.Series(range(len(pred_index), 0, -1), index=pred_index, dtype="float64").rename("score")
    pred.to_pickle(run_dir / "pred.pkl")
    (run_dir / "metrics.json").write_text('{"excess_annualized_ret_with_cost": 0.2}', encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        '{"model_system_version":"model","runner":{"execute_qlib":true},"experiment":{"sample_weight_policy":"top50_smooth2_bottom50_smooth1p5_mean_norm"}}',
        encoding="utf-8",
    )
    (run_dir / "validation_audit.json").write_text(
        """
        {
          "status": "review_required",
          "checks": {
            "tradability_exposure": {
              "status": "review_required",
              "prediction": {
                "topk_avg_st_like_ratio": 0.25,
                "top50_avg_st_like_ratio": 0.15,
                "row_count": 24
              }
            },
            "model_style_exposure": {
              "status": "warning",
              "small_cap": {"top10": 0.4, "top50": 0.3}
            }
          }
        }
        """,
        encoding="utf-8",
    )
    row = {
        "model_id": "m-reg-0703-exposure",
        "model_run_id": "m0703-test-exposure-s42",
        "status": "candidate",
        "created_at": "2026-07-04T00:00:00Z",
        "run_dir": str(run_dir),
        "workspace_path": str(run_dir),
        "metadata": {"model_system_version": "model", "seed": 42, "sota_score": 61.2},
    }
    monkeypatch.setattr(svc, "_model_registry_rows", lambda status="all", limit=None: [row])

    result = svc.model_backtest(model_run_id="m0703-test-exposure-s42", include_daily=False)

    assert result.ok is True
    assert (run_dir / "validation_audit.json").exists()
    assert result.outputs["exposure"]["available"] is True
    prediction = result.outputs["validation"]["tradability_exposure"]["prediction"]
    assert prediction["topk_avg_st_like_ratio"] > 0
    assert "generated_on_demand" not in result.outputs["validation"]


def test_registry_and_production_use_items_as_primary_field(monkeypatch):
    row = {
        "model_id": "m-reg-0703",
        "model_run_id": "m0703-test-s42",
        "status": "candidate",
        "created_at": "2026-07-04T00:00:00Z",
        "metadata": {"model_system_version": "model", "seed": 42, "sota_score": 61.2},
    }
    prod = {**row, "status": "production", "model_id": "m-prod-0703"}
    monkeypatch.setattr(svc, "_model_registry_rows", lambda status="all", limit=None: [prod] if status == "production" else [row, prod])

    registry = svc.model_registry()
    production = svc.model_production()
    archived = {
        **row,
        "status": "archived",
        "model_id": "m-arch-0703",
        "model_run_id": "m0703-arch-s17",
        "created_at": "2026-07-05T00:00:00Z",
    }
    monkeypatch.setattr(
        svc,
        "_model_registry_rows",
        lambda status="all", limit=None: [prod] if status == "production" else [archived, row, prod],
    )
    backtest = svc.model_backtest(include_daily=False)

    assert registry.ok is True
    assert registry.outputs["items"] == registry.outputs["models"]
    assert registry.outputs["items"][0]["model_run_id"] == "m0703-test-s42"
    assert production.ok is True
    assert production.outputs["items"] == production.outputs["production_models"]
    assert production.outputs["multiple_production_allowed"] is True
    assert backtest.ok is True
    assert [item["status"] for item in backtest.outputs["recent_models"][:2]] == ["candidate", "production"]
    assert all(item["status"] != "archived" for item in backtest.outputs["recent_models"])
    assert backtest.outputs["selection"]["model_run_id"] == "m0703-test-s42"


def test_backtest_selection_includes_research_and_candidate(monkeypatch):
    research = {
        "model_id": "m-research",
        "model_run_id": "run-research",
        "status": "research",
        "created_at": "2026-07-19T00:00:00Z",
        "metadata": {"model_system_version": "model", "confirmed_research_score": 91.5},
    }
    candidate = {
        "model_id": "m-candidate",
        "model_run_id": "run-candidate",
        "status": "candidate",
        "created_at": "2026-07-18T00:00:00Z",
        "metadata": {"model_system_version": "model", "rolling_score": 72.0},
    }
    monkeypatch.setattr(svc, "_model_registry_rows", lambda status="all", limit=None: [research, candidate])

    result = svc.model_backtest(selector="best", include_daily=False)

    assert result.ok is True
    assert {item["status"] for item in result.outputs["recent_models"]} == {"research", "candidate"}
    assert result.outputs["selection"]["selector"] == "best_research_or_candidate"
    assert result.outputs["selection"]["model_run_id"] == "run-research"


def test_backtest_can_select_formal_rolling_campaign(tmp_path, monkeypatch):
    stitched_ret = tmp_path / "stitched_ret.pkl"
    stitched_ret_seed17 = tmp_path / "stitched_ret_seed17.pkl"
    pd.DataFrame(
        {
            "return": [0.01, -0.002, 0.015],
            "bench": [0.001, 0.0, 0.002],
            "cost": [0.0001, 0.0001, 0.0001],
        },
        index=pd.to_datetime(["2024-07-02", "2024-07-03", "2024-07-04"]),
    ).to_pickle(stitched_ret)
    pd.DataFrame(
        {
            "return": [0.02, -0.001, 0.01],
            "bench": [0.001, 0.0, 0.002],
            "cost": [0.0001, 0.0001, 0.0001],
        },
        index=pd.to_datetime(["2024-07-02", "2024-07-03", "2024-07-04"]),
    ).to_pickle(stitched_ret_seed17)
    campaign = {
        "campaign_id": "roll-test",
        "status": "research",
        "decision": "stop_after_seed42",
        "feature_set_id": "fs-33",
        "portfolio": {"topk": 20, "n_drop": 2, "hold_thresh": 5, "benchmark": "000300sh"},
        "preliminary": {"score": 56.73, "gates": {"latest_fold_ir_positive": False}},
        "final": {"available": False},
        "seeds": [
            {
                "seed": 42,
                "factor_count": 33,
                "rolling_metrics": {
                    "excess_annualized_ret_with_cost": 0.6039,
                    "excess_information_ratio_with_cost": 2.413,
                    "max_drawdown": -0.1746,
                },
                "artifacts": {"stitched_return": str(stitched_ret)},
                "folds": [],
            },
            {
                "seed": 17,
                "factor_count": 33,
                "rolling_metrics": {
                    "excess_annualized_ret_with_cost": 0.4321,
                    "excess_information_ratio_with_cost": 1.765,
                    "max_drawdown": -0.201,
                },
                "artifacts": {"stitched_return": str(stitched_ret_seed17)},
                "folds": [],
            },
        ],
    }
    monkeypatch.setattr(svc, "_model_registry_rows", lambda status="all", limit=None: [])
    monkeypatch.setattr(svc, "_model_rolling_campaigns", lambda limit=30: [campaign])

    result = svc.model_backtest(rolling_campaign_id="roll-test", rolling_seed=42, include_daily=False)

    assert result.ok is True
    assert result.outputs["selection"]["selector"] == "selected_rolling_campaign"
    assert result.outputs["selection"]["model_run_id"] == "roll-test"
    assert result.outputs["selection"]["rolling_seed"] == 42
    assert result.outputs["selected_model"]["role"] == "rolling_campaign"
    assert result.outputs["selected_model"]["seed"] == 42
    assert result.outputs["rolling_campaign"]["decision"] == "stop_after_seed42"
    assert result.outputs["curve_available"] is True

    seed17_result = svc.model_backtest(rolling_campaign_id="roll-test", rolling_seed=17, include_daily=False)

    assert seed17_result.ok is True
    assert seed17_result.outputs["selection"]["rolling_seed"] == 42
    assert seed17_result.outputs["selected_model"]["seed"] == 42
    assert seed17_result.outputs["selected_model"]["excess_annualized_ret_with_cost"] == 0.6039
    assert seed17_result.artifacts["ret_pkl"] == str(stitched_ret)


def test_registry_and_backtest_hide_research_audit_seeds(monkeypatch):
    seed42 = {
        "model_id": "formal-42",
        "model_run_id": "round-a-s42",
        "round_group_id": "round-a",
        "seed": 42,
        "status": "research",
        "research_score": 75.0,
        "metadata": {"model_system_version": "model", "round_group_id": "round-a", "seed": 42, "research_score": 75.0},
    }
    seed17 = {
        **seed42,
        "model_id": "audit-17",
        "model_run_id": "round-a-s17",
        "seed": 17,
        "research_score": 99.0,
        "metadata": {"model_system_version": "model", "round_group_id": "round-a", "seed": 17, "research_score": 99.0},
    }
    monkeypatch.setattr(svc, "_model_registry_rows", lambda status="all", limit=None: [seed17, seed42])

    registry = svc.model_registry()
    backtest = svc.model_backtest(model_run_id="round-a-s17", include_daily=False)

    assert [row["model_run_id"] for row in registry.outputs["items"]] == ["round-a-s42"]
    assert backtest.outputs["selection"]["selector"] == "selected_research_round_official_seed42"
    assert backtest.outputs["selection"]["model_run_id"] == "round-a-s42"


def test_status_exposes_gui_projection_from_state(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    state.upsert_job(
        "job-0703",
        status="running",
        stage="score_review",
        mode="orch",
        current_round_group_id="mr0703-test",
        payload={
            "best_round_group_id": "mr0703-test",
            "consecutive_no_improvement": 0,
            "completed_rounds": [{"round_group_id": "mr0703-test"}],
        },
    )
    state.upsert_round(
        {
            "round_group_id": "mr0703-test",
            "feature_set_id": "fs-test",
            "experiment_signature": "sig",
            "seed_set": [42, 17, 83],
            "seed_policy": {"mode": "fixed_three_parallel_seeds"},
            "experiment": {
                "qlib_model_kwargs": {"learning_rate": 0.04, "num_leaves": 31},
                "research_metadata": {
                    "round_no": 1,
                    "round_kind": "tuning",
                    "hypothesis": "test hypothesis",
                    "reference_round_group_id": "mr0703-baseline",
                    "parameter_changes": [{"parameter": "num_leaves", "from": 63, "to": 31, "reason": "test"}],
                },
            },
            "status": "running",
            "stage": "score_review",
        }
    )
    state.upsert_seed_run(
        {
            "model_run_id": "m0703-test-s42",
            "round_group_id": "mr0703-test",
            "seed": 42,
            "status": "completed",
            "metrics": {
                "excess_annualized_ret_with_cost": 0.2,
                "excess_information_ratio_with_cost": 0.8,
                "training_diagnostics": {"available": True, "best_iteration": 320, "early_stopped": True},
            },
            "score": {"sota_score": 61.2},
        }
    )
    monkeypatch.setattr(svc, "ModelStateStore", lambda: state)
    monkeypatch.setattr(svc, "factor_active_values_status", lambda: type("R", (), {"ok": True, "outputs": {}})())
    monkeypatch.setattr(svc, "active_values_readiness", lambda: {"safe_to_freeze_feature_set": True})
    monkeypatch.setattr(svc, "_model_registry_rows", lambda status="all", limit=None: [])
    monkeypatch.setattr(svc, "read_jsonl", lambda path, limit=100, include_payload=True: [])

    result = svc.model_status()

    assert result.ok is True
    projection = result.outputs["gui_projection"]
    assert projection["process_progress"]["job_id"] == "job-0703"
    assert projection["process_progress"]["stage"] == "score_review"
    assert projection["candidate_rounds"]["comparison_rows"][0]["round_group_id"] == "mr0703-test"
    assert projection["candidate_rounds"]["comparison_rows"][0]["is_best_session_round"] is True
    assert projection["candidate_rounds"]["comparison_rows"][0]["hypothesis"] == "test hypothesis"
    assert projection["candidate_rounds"]["comparison_rows"][0]["research_score"] == 61.2
    assert projection["quality_gate_summary"]["seed_stability"]["seed_models"][0]["model_run_id"] == "m0703-test-s42"
    assert projection["quality_gate_summary"]["seed_stability"]["seed_models"][0]["training_diagnostics"]["best_iteration"] == 320
    source_map = result.outputs["status_source_map"]
    keys = {row["key"] for row in source_map}
    assert {"state_store", "research_steps", "orchestrator_events", "orchestrator_traces", "mcp_traces", "model_registry"} <= keys
    state_source = next(row for row in source_map if row["key"] == "state_store")
    assert state_source["record_count"]["jobs"] == 1
    assert state_source["filter"]["job_id"] == "job-0703"


def test_status_keeps_finished_legacy_session_historical_not_active(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    state.upsert_job(
        "job-legacy-0703",
        status="failed",
        stage="feature_snapshot",
        mode="orch",
        payload={"feature_set_id": "fs-legacy", "n_rounds": 5, "err": "active values stale"},
    )
    monkeypatch.setattr(svc, "ModelStateStore", lambda: state)
    monkeypatch.setattr(svc, "factor_active_values_status", lambda: type("R", (), {"ok": True, "outputs": {}})())
    monkeypatch.setattr(svc, "active_values_readiness", lambda: {"safe_to_freeze_feature_set": False})
    monkeypatch.setattr(svc, "_model_registry_rows", lambda status="all", limit=None: [])
    monkeypatch.setattr(svc, "read_jsonl", lambda path, limit=100, include_payload=True: [])

    result = svc.model_status()

    assert result.ok is True
    assert result.outputs["orchestrator"]["active_job"] is None
    assert result.outputs["active_session"] is None
    assert result.outputs["latest_session"]["legacy_session_view"] is True
    assert result.outputs["latest_session"]["session_id"] == "legacy_job:job-legacy-0703"
    assert result.outputs["session_blockers"] == []


def test_status_projects_formal_rolling_campaign_for_gui(tmp_path, monkeypatch):
    rolling_root = tmp_path / "rolling"
    campaign_dir = rolling_root / "roll-test"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.json").write_text(
        """
        {
          "campaign_id": "roll-test",
          "ok": true,
          "status": "research",
          "decision": "stop_after_seed42",
          "feature_set_id": "fs-33",
          "preliminary": {
            "passed": false,
            "gates": {"latest_fold_ir_positive": false},
            "score": {"score": 56.73, "fold_quality": [{"score": 88.0}]}
          },
          "seed_results": {
            "42": {
              "status": "complete",
              "factor_count": 33,
              "rolling_metrics": {"excess_annualized_ret_with_cost": 0.6},
              "fold_portfolio_metrics": {
                "wf1": {
                  "excess_annualized_ret_with_cost": 0.4,
                  "excess_information_ratio_with_cost": 1.2,
                  "max_drawdown": -0.1,
                  "window_contract": {"signal_window": ["2024-07-01", "2024-12-31"]}
                }
              }
            }
          }
        }
        """,
        encoding="utf-8",
    )
    state = ModelStateStore(runtime_root=tmp_path / "state")
    monkeypatch.setattr(svc, "ROLLING_ROOT", rolling_root)
    monkeypatch.setattr(svc, "ModelStateStore", lambda: state)
    monkeypatch.setattr(svc, "factor_active_values_status", lambda: type("R", (), {"ok": True, "outputs": {}})())
    monkeypatch.setattr(svc, "active_values_readiness", lambda: {"safe_to_freeze_feature_set": True})
    monkeypatch.setattr(svc, "_model_registry_rows", lambda status="all", limit=None: [])
    monkeypatch.setattr(svc, "read_jsonl", lambda path, limit=100, include_payload=True: [])

    result = svc.model_status()

    assert result.ok is True
    campaign = result.outputs["latest_rolling_campaign"]
    assert campaign["campaign_id"] == "roll-test"
    assert campaign["preliminary"]["score"] == 56.73
    assert campaign["seeds"][0]["folds"][0]["signal_start"] == "2024-07-01"


def test_research_current_marks_pre_dual_mode_record_as_historical(monkeypatch):
    legacy = {
        "stage": "round_synthesis",
        "summary": "seed passed SOTA threshold then failed forward_test",
        "decision": "continue",
    }
    monkeypatch.setattr(svc, "read_jsonl", lambda path, limit=100, include_payload=True: [legacy])
    monkeypatch.setattr(svc, "_latest_llm_review_signal", lambda rows, events: {**legacy, "active": True})

    result = svc.model_research_current()

    assert result.ok is True
    assert result.outputs["current"]["record_era"] == "historical_pre_dual_mode"
    assert result.outputs["current"]["current_contract"] is False
    assert result.outputs["current_contract_available"] is False
    assert result.outputs["historical_record_available"] is True
    assert result.outputs["llm_review_signal"]["active"] is False


def test_llm_review_signal_does_not_leak_from_another_selected_session():
    old_checkpoint = {
        "session_id": "old-session",
        "job_id": "old-job",
        "decision": "checkpoint_stop",
        "next": "human_review",
        "stage": "round_synthesis",
        "summary": "old checkpoint",
    }
    current_step = {
        "session_id": "new-session",
        "job_id": "new-job",
        "decision": "continue",
        "next": "experiment_plan",
        "stage": "round_synthesis",
        "summary": "new session has not stopped",
    }

    result = svc._latest_llm_review_signal(
        [old_checkpoint, current_step],
        [],
        active_session={"session_id": "new-session"},
        active_job={"job_id": "new-job"},
    )

    assert result == {"active": False}
