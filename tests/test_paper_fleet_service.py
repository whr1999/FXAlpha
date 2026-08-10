from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from services._base import ok_result
from storage.trading_registry import TradingRegistry


fleet = pytest.importorskip("services.paper_fleet_service")


@pytest.fixture(autouse=True)
def _isolate_operation_lock(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(fleet, "PAPER_OPERATION_LOCK_FILE", tmp_path / "paper-operation.lock")


def _account(account_id: str = "paper-prod-a") -> dict:
    return {
        "account_id": account_id,
        "display_name": account_id,
        "account_mode": "fixed_model",
        "initial_capital": 1_000_000,
        "strategy_contract_version": "top20_drop2_hold5_open_v1",
        "topk": 20,
        "n_drop": 2,
        "hold_thresh": 5,
        "deal_price": "open",
        "status": "active",
        "metadata": {"inception_date": "2026-07-01"},
    }


def _deployment(account_id: str = "paper-prod-a", *, model_id: str = "model-a", model_run_id: str = "run-a") -> dict:
    return {
        "deployment_id": f"deploy-{account_id}-{model_run_id}-2026-07-01",
        "account_id": account_id,
        "model_id": model_id,
        "model_run_id": model_run_id,
        "feature_set_id": "fs-a",
        "effective_from": "2026-07-01",
        "deployment_mode": "fixed_model",
        "status": "active",
    }


def _commit_mock_execution(registry: TradingRegistry, rec: dict, *, account_id: str, total_capital: float) -> None:
    snapshot = {
        "account_id": account_id,
        "model_run_id": rec["model_run_id"],
        "trade_date": rec["execution_date"],
        "source_recommendation_id": rec["recommendation_id"],
        "cash": total_capital,
        "stock_value": 0,
        "account_value": total_capital,
    }
    registry.commit_execution(
        execution={
            "execution_id": f"exec-{rec['recommendation_id']}-{rec['execution_date']}",
            "account_id": account_id,
            "recommendation_id": rec["recommendation_id"],
            "model_id": rec.get("model_id", ""),
            "model_run_id": rec["model_run_id"],
            "trade_date": rec["execution_date"],
            "status": "completed",
        },
        recommendation_id=rec["recommendation_id"],
        recommendation_status="executed",
        snapshot=snapshot,
    )


def test_registry_isolates_accounts_recommendations_and_deployments(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    models = {
        "paper-prod-a": ("model-a", "run-a"),
        "paper-prod-b": ("model-b", "run-b"),
    }
    for account_id, (model_id, model_run_id) in models.items():
        registry.upsert_account(_account(account_id))
        registry.upsert_deployment(_deployment(account_id, model_id=model_id, model_run_id=model_run_id))
        registry.upsert_recommendation(
            {
                "recommendation_id": f"rec-{account_id}",
                "account_id": account_id,
                "model_id": model_id,
                "model_run_id": model_run_id,
                "signal_date": "2026-07-01",
                "execution_date": "2026-07-02",
                "status": "pending",
                "topk": 20,
                "total_capital": 1_000_000,
            }
        )

    assert len(registry.list_accounts("active")) == 2
    assert registry.deployment_for_date("paper-prod-a", "2026-07-02")["model_run_id"] == "run-a"
    assert registry.deployment_for_date("paper-prod-b", "2026-07-02")["model_run_id"] == "run-b"
    assert [row["recommendation_id"] for row in registry.pending_recommendations(account_id="paper-prod-a")] == ["rec-paper-prod-a"]
    assert [row["recommendation_id"] for row in registry.pending_recommendations(account_id="paper-prod-b")] == ["rec-paper-prod-b"]


def test_account_day_resolves_blank_pending_execution_date_before_due_filter(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    account = _account()
    deployment = _deployment()
    registry.upsert_account(account)
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-2026-07-01",
            "account_id": account["account_id"],
            "model_id": deployment["model_id"],
            "model_run_id": deployment["model_run_id"],
            "signal_date": "2026-07-01",
            "execution_date": "",
            "status": "pending",
            "topk": 20,
            "total_capital": 1_000_000,
        }
    )
    monkeypatch.setattr(fleet, "resolve_pending_execution_date", lambda signal_date: "2026-07-02")
    executed = []

    def execute_pending(*, recommendation_id, account_id, total_capital, include_status_snapshot=True):
        rec = registry.get_recommendation(recommendation_id)
        assert rec["execution_date"] == "2026-07-02"
        executed.append(recommendation_id)
        _commit_mock_execution(registry, rec, account_id=account_id, total_capital=total_capital)
        return ok_result(outputs={"status": "completed", "executed": [{"recommendation_id": recommendation_id}], "failed": [], "skipped": []})

    def recommend(**kwargs):
        rec = {
            "recommendation_id": "rec-2026-07-02",
            "account_id": kwargs["account_id"],
            "model_id": kwargs["model_id"],
            "model_run_id": kwargs["model_run_id"],
            "signal_date": kwargs["signal_date"],
            "execution_date": "",
            "status": "pending",
            "topk": kwargs["topk"],
            "total_capital": kwargs["total_capital"],
        }
        registry.upsert_recommendation(rec)
        return ok_result(outputs={"status": "pending", "recommendation": rec})

    monkeypatch.setattr(fleet, "trading_execute_pending", execute_pending)
    monkeypatch.setattr(fleet, "trading_recommend", recommend)
    monkeypatch.setattr(
        fleet,
        "_initialize_or_mark_account",
        lambda *args, **kwargs: pytest.fail("resolved due recommendation must execute before mark-to-market"),
    )

    result = fleet.paper_account_day_run(
        registry=registry,
        account=account,
        deployment=deployment,
        signal_date="2026-07-02",
        data={"data_package_id": "pkg-a", "qlib_latest": "2026-07-02"},
        identity_rows=pd.DataFrame(),
    )

    assert result.ok
    assert executed == ["rec-2026-07-01"]
    assert registry.get_recommendation("rec-2026-07-01")["status"] == "executed"
    assert registry.latest_account_snapshot(account["account_id"])["trade_date"] == "2026-07-02"
    assert registry.latest_recommendation(account["account_id"])["signal_date"] == "2026-07-02"


def test_account_day_resumes_after_execution_was_published_before_process_died(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    account = _account()
    deployment = _deployment()
    registry.upsert_account(account)
    registry.upsert_deployment(deployment)
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-old-variant",
            "account_id": account["account_id"],
            "model_id": deployment["model_id"],
            "model_run_id": deployment["model_run_id"],
            "signal_date": "2026-07-01",
            "status": "superseded",
        }
    )
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-canonical",
            "account_id": account["account_id"],
            "model_id": deployment["model_id"],
            "model_run_id": deployment["model_run_id"],
            "signal_date": "2026-07-01",
            "execution_date": "2026-07-02",
            "status": "pending",
        }
    )
    canonical = registry.get_recommendation("rec-canonical")
    _commit_mock_execution(registry, canonical, account_id=account["account_id"], total_capital=1_000_000)
    config_hash = fleet._config_hash(account, deployment)
    account_run_id = fleet._account_run_id(account["account_id"], "2026-07-02", config_hash)
    registry.upsert_account_run(
        {
            "account_run_id": account_run_id,
            "fleet_run_id": "fleet-interrupted",
            "account_id": account["account_id"],
            "signal_date": "2026-07-02",
            "model_id": deployment["model_id"],
            "model_run_id": deployment["model_run_id"],
            "strategy_contract_version": account["strategy_contract_version"],
            "config_hash": config_hash,
            "run_kind": "on_time",
            "status": "running",
            "current_stage": "generate_recommendation",
            "attempt": 1,
        }
    )

    monkeypatch.setattr(
        fleet,
        "trading_execute_pending",
        lambda **kwargs: pytest.fail("published execution must not run twice"),
    )

    def recommend(**kwargs):
        recommendation = {
            "recommendation_id": "rec-2026-07-02",
            "account_id": kwargs["account_id"],
            "model_id": kwargs["model_id"],
            "model_run_id": kwargs["model_run_id"],
            "signal_date": kwargs["signal_date"],
            "execution_date": "",
            "status": "pending",
            "topk": kwargs["topk"],
            "total_capital": kwargs["total_capital"],
        }
        registry.upsert_recommendation(recommendation)
        return ok_result(outputs={"status": "pending", "recommendation": recommendation})

    monkeypatch.setattr(fleet, "trading_recommend", recommend)

    result = fleet.paper_account_day_run(
        registry=registry,
        account=account,
        deployment=deployment,
        signal_date="2026-07-02",
        data={"data_package_id": "pkg-a", "qlib_latest": "2026-07-02"},
        identity_rows=pd.DataFrame(),
        fleet_run_id="fleet-interrupted",
        run_kind="on_time",
    )

    assert result.ok
    assert result.outputs["execution"]["status"] == "already_marked"
    assert registry.get_account_run(account_run_id)["status"] == "completed"
    assert registry.get_account_run(account_run_id)["attempt"] == 2
    assert len(registry.list_account_snapshots(account["account_id"])) == 1
    assert registry.latest_recommendation(account["account_id"])["signal_date"] == "2026-07-02"


def test_prediction_window_reuses_matching_persisted_prediction(monkeypatch):
    monkeypatch.setattr(
        fleet,
        "pred_status_snapshot",
        lambda: ok_result(
            outputs={
                "status": "ready",
                "run_context": {"model_id": "model-a", "model_run_id": "run-a"},
                "factor_freshness": {"factor_latest_date": "2026-07-02"},
                "update": {"updated_end": "2026-07-02"},
            }
        ),
    )
    monkeypatch.setattr(
        fleet.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("current prediction must not spawn a heavy worker"),
    )

    result = fleet._isolated_prediction_update(
        model_id="model-a",
        model_run_id="run-a",
        from_date="2026-07-02",
        to_date="2026-07-02",
    )

    assert result.ok
    assert result.outputs["status"] == "already_current"
    assert result.outputs["covered_end"] == "2026-07-02"


def test_reconcile_published_state_finishes_interrupted_registry_commit(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    account = _account()
    deployment = _deployment()
    registry.upsert_account(account)
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-recover",
            "account_id": account["account_id"],
            "model_id": deployment["model_id"],
            "model_run_id": deployment["model_run_id"],
            "signal_date": "2026-07-01",
            "execution_date": "2026-07-02",
            "status": "pending",
        }
    )
    root = tmp_path / "paper"
    execution_dir = root / account["account_id"] / "executions" / "2026-07-02_rec-recover"
    execution_dir.mkdir(parents=True)
    meta_file = execution_dir / "execution_meta.json"
    state_file = root / account["account_id"] / "state" / "account_state.json"
    state_file.parent.mkdir(parents=True)
    output_files = {
        "execution_meta_file": str(meta_file),
        "account_state_file": str(state_file),
    }
    meta_file.write_text(
        json.dumps(
            {
                "adapter": "qlib_exchange_paper",
                "trade_date": "2026-07-02",
                "recommendation_id": "rec-recover",
                "metrics": {"trade_count": 1},
                "output_files": output_files,
            }
        ),
        encoding="utf-8",
    )
    state_file.write_text(
        json.dumps(
            {
                "account_id": account["account_id"],
                "as_of_date": "2026-07-02",
                "cash": 900000,
                "stock_value": 100000,
                "account_value": 1000000,
                "positions": {},
                "source_recommendation_id": "rec-recover",
                "output_files": output_files,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(fleet, "PAPER_TRADING_RUNTIME_ROOT", root)

    actions = fleet._reconcile_published_account_state(
        registry,
        account=account,
        deployment=deployment,
    )

    assert actions == ["execution_commit_recovered:rec-recover"]
    assert registry.get_recommendation("rec-recover")["status"] == "executed"
    assert registry.execution_for_recommendation("rec-recover")["status"] == "completed"
    assert registry.account_snapshot(account["account_id"], "2026-07-02")["account_value"] == 1000000


def test_status_blocks_when_blank_pending_execution_date_is_older_than_data(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    registry.upsert_account(_account())
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-overdue",
            "account_id": "paper-prod-a",
            "model_id": "model-a",
            "model_run_id": "run-a",
            "signal_date": "2026-07-01",
            "execution_date": "",
            "status": "pending",
            "topk": 20,
            "total_capital": 1_000_000,
        }
    )
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)
    monkeypatch.setattr(
        fleet,
        "_data_context",
        lambda: (ok_result(), {"qlib_latest": "2026-07-02", "production_health": "ready"}),
    )
    monkeypatch.setattr(fleet, "load_stock_identity_map", lambda: {})
    monkeypatch.setattr(fleet, "FLEET_LATEST_STATUS_FILE", tmp_path / "missing-latest-status.json")

    result = fleet.paper_fleet_status()

    assert result.ok
    assert result.outputs["status"] == "blocked"
    assert result.outputs["blocked_accounts"] == [
        {"account_id": "paper-prod-a", "reasons": ["pending_execution_date_unresolved:2026-07-01"]}
    ]


def test_status_exposes_run_summaries_without_nested_execution_payloads(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    registry.upsert_account(_account())
    registry.upsert_account_run(
        {
            "account_run_id": "paper-run-a",
            "account_id": "paper-prod-a",
            "signal_date": "2026-07-01",
            "status": "completed",
            "current_stage": "completed",
            "outputs": {"execution": {"large": ["unused"] * 100}},
        }
    )
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)
    monkeypatch.setattr(
        fleet,
        "_data_context",
        lambda: (ok_result(), {"qlib_latest": "2026-07-01", "production_health": "ready"}),
    )
    monkeypatch.setattr(fleet, "load_stock_identity_map", lambda: {})
    monkeypatch.setattr(fleet, "FLEET_LATEST_STATUS_FILE", tmp_path / "missing-latest-status.json")

    result = fleet.paper_fleet_status()

    recent = result.outputs["accounts"][0]["recent_runs"][0]
    assert recent["account_run_id"] == "paper-run-a"
    assert recent["status"] == "completed"
    assert "outputs" not in recent


def test_fleet_preflight_reports_already_current_when_no_date_needs_work(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    registry.upsert_account(_account())
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)
    monkeypatch.setattr(
        fleet,
        "_data_context",
        lambda: (ok_result(), {"qlib_latest": "2026-07-01", "production_health": "ready"}),
    )
    monkeypatch.setattr(
        fleet,
        "paper_replay_plan",
        lambda **kwargs: ok_result(outputs={"plan": {"trade_date_count": 0, "trade_dates": []}}),
    )

    result = fleet.paper_fleet_preflight()

    assert result.ok
    assert result.outputs["status"] == "already_current"
    assert result.outputs["pending_trade_date_count"] == 0


def test_replay_run_processes_each_trade_date_and_is_idempotent(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    registry.upsert_account(_account())
    registry.upsert_deployment(_deployment())
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)

    dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
    plan = {
        "account_id": "paper-prod-a",
        "from_date": dates[0],
        "to_date": dates[-1],
        "trade_dates": dates,
        "trade_date_count": len(dates),
        "deployments": [
            {"signal_date": value, "deployment_id": "deploy", "model_id": "model-a", "model_run_id": "run-a"}
            for value in dates
        ],
        "data": {
            "hdf5_latest": dates[-1],
            "qlib_latest": dates[-1],
            "quantgpt_latest": dates[-1],
            "data_package_id": "pkg-a",
            "production_health": "ready",
        },
        "replay_basis": "latest_promoted_restated_asof_capped",
        "requires_confirmation": False,
        "blockers": [],
    }
    monkeypatch.setattr(fleet, "paper_replay_plan", lambda **kwargs: ok_result(outputs={"status": "ready", "plan": plan}))
    prediction_calls = []
    monkeypatch.setattr(
        fleet,
        "_isolated_prediction_update",
        lambda **kwargs: prediction_calls.append(kwargs) or ok_result(outputs={"status": "completed"}),
    )
    monkeypatch.setattr(
        fleet,
        "_replay_score_quality",
        lambda **kwargs: {"status": "passed", "dates": [], "blockers": []},
    )
    monkeypatch.setattr(
        fleet,
        "load_stock_identity_rows_for_window",
        lambda start, end: pd.DataFrame({"trade_date": pd.to_datetime(dates)}),
    )

    def initialize(registry, *, account, deployment, signal_date):
        registry.record_account_snapshot(
            {
                "account_id": account["account_id"],
                "model_run_id": deployment["model_run_id"],
                "trade_date": signal_date,
                "cash": account["initial_capital"],
                "stock_value": 0,
                "account_value": account["initial_capital"],
            }
        )
        return {"status": "marked"}

    monkeypatch.setattr(fleet, "_initialize_or_mark_account", initialize)

    next_dates = {"2026-07-01": "2026-07-02", "2026-07-02": "2026-07-03", "2026-07-03": ""}

    def recommend(**kwargs):
        signal_date = kwargs["signal_date"]
        rec = {
            "recommendation_id": f"rec-{signal_date}",
            "account_id": kwargs["account_id"],
            "model_id": kwargs["model_id"],
            "model_run_id": kwargs["model_run_id"],
            "signal_date": signal_date,
            "execution_date": next_dates[signal_date],
            "status": "pending",
            "topk": kwargs["topk"],
            "total_capital": kwargs["total_capital"],
        }
        registry.upsert_recommendation(rec)
        registry.supersede_pending_except(
            model_run_id=kwargs["model_run_id"],
            account_id=kwargs["account_id"],
            keep_recommendation_id=rec["recommendation_id"],
        )
        return ok_result(outputs={"status": "pending", "recommendation": rec})

    monkeypatch.setattr(fleet, "trading_recommend", recommend)

    def execute_pending(*, recommendation_id, account_id, total_capital, include_status_snapshot=True):
        rec = registry.get_recommendation(recommendation_id)
        _commit_mock_execution(registry, rec, account_id=account_id, total_capital=total_capital)
        return ok_result(outputs={"status": "completed", "executed": [{"recommendation_id": recommendation_id}], "failed": []})

    monkeypatch.setattr(fleet, "trading_execute_pending", execute_pending)

    first = fleet.paper_replay_run(account_id="paper-prod-a", to_date=dates[-1])
    assert first.ok
    assert [row["signal_date"] for row in first.outputs["runs"]] == dates
    assert len(registry.list_account_runs("paper-prod-a")) == 3
    assert len(registry.list_account_snapshots("paper-prod-a")) == 3
    assert [row["signal_date"] for row in registry.pending_recommendations(account_id="paper-prod-a")] == [dates[-1]]
    assert prediction_calls == [{"model_id": "model-a", "model_run_id": "run-a", "from_date": dates[0], "to_date": dates[-1]}]

    second = fleet.paper_replay_run(account_id="paper-prod-a", to_date=dates[-1])
    assert second.ok
    assert all(row["status"] == "already_completed" for row in second.outputs["runs"])
    assert len(registry.list_account_snapshots("paper-prod-a")) == 3


def test_replay_requires_confirmation_for_long_gap(monkeypatch):
    monkeypatch.setattr(
        fleet,
        "paper_replay_plan",
        lambda **kwargs: ok_result(
            outputs={
                "status": "ready",
                "plan": {"trade_dates": [f"2026-07-{day:02d}" for day in range(1, 8)], "requires_confirmation": True},
            }
        ),
    )
    result = fleet.paper_replay_run(account_id="paper-prod-a")
    assert not result.ok
    assert result.err == "long_replay_confirmation_required"


def test_data_context_reads_current_production_package(monkeypatch):
    monkeypatch.setattr(
        fleet,
        "data_status",
        lambda: ok_result(
            outputs={
                "snapshot": {
                    "latest_hdf5_trade_date": "2026-08-03",
                    "latest_qlib_trade_date": "2026-08-03",
                    "latest_quantgpt_trade_date": "2026-08-03",
                },
                "current_production_dataset": {"production_package_id": "pkg-prod-a"},
                "production_health": {"status": "ready"},
            }
        ),
    )

    result, context = fleet._data_context()

    assert result.ok
    assert context["data_package_id"] == "pkg-prod-a"
    assert context["production_health"] == "ready"


def test_replay_stops_before_account_writes_when_score_quality_is_blocked(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    registry.upsert_account(_account())
    registry.upsert_deployment(_deployment())
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)
    plan = {
        "account_id": "paper-prod-a",
        "trade_dates": ["2026-07-01"],
        "deployments": [{"signal_date": "2026-07-01", "model_id": "model-a", "model_run_id": "run-a"}],
        "data": {"hdf5_latest": "2026-07-01", "qlib_latest": "2026-07-01", "quantgpt_latest": "2026-07-01", "production_health": "ready"},
        "requires_confirmation": False,
    }
    monkeypatch.setattr(fleet, "paper_replay_plan", lambda **kwargs: ok_result(outputs={"plan": plan}))
    monkeypatch.setattr(fleet, "_isolated_prediction_update", lambda **kwargs: ok_result(outputs={"status": "completed"}))
    monkeypatch.setattr(
        fleet,
        "_replay_score_quality",
        lambda **kwargs: {"status": "blocked", "dates": [], "blockers": ["score_quality:2026-07-01"]},
    )

    result = fleet.paper_replay_run(account_id="paper-prod-a")

    assert not result.ok
    assert result.err == "replay_score_quality_blocked"
    assert registry.list_account_runs("paper-prod-a") == []
    assert registry.list_account_snapshots("paper-prod-a") == []


def test_fleet_status_is_snapshot_only_and_separates_archived_accounts(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    registry.upsert_account(_account())
    registry.upsert_deployment(_deployment())
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-paper-prod-a",
            "account_id": "paper-prod-a",
            "model_id": "model-a",
            "model_run_id": "run-a",
            "signal_date": "2026-07-01",
            "status": "pending",
            "topk": 20,
            "total_capital": 1_000_000,
        },
        orders=[
            {
                "instrument": "000001sz",
                "action": "buy",
                "target_shares": 100,
                "delta_shares": 100,
                "target_weight": 0.05,
                "target_value": 50_000,
            }
        ],
    )
    registry.upsert_account(_account("paper-prod-paused"))
    registry.set_account_status("paper-prod-paused", "paused")
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)
    monkeypatch.setattr(
        fleet,
        "_data_context",
        lambda: (ok_result(), {"qlib_latest": "2026-07-01", "production_health": "ready"}),
    )
    monkeypatch.setattr(
        fleet,
        "paper_replay_plan",
        lambda **kwargs: pytest.fail("status must not generate a replay plan"),
    )
    monkeypatch.setattr(fleet, "load_stock_identity_map", lambda: {"000001sz": "平安银行"})
    monkeypatch.setattr(fleet, "FLEET_LATEST_STATUS_FILE", tmp_path / "missing-latest-status.json")

    result = fleet.paper_fleet_status()

    assert result.ok
    assert result.outputs["status"] == "ready"
    assert result.outputs["status_mode"] == "snapshot_only"
    assert [row["account_id"] for row in result.outputs["accounts"]] == ["paper-prod-a"]
    assert [row["account_id"] for row in result.outputs["archived_accounts"]] == ["paper-prod-paused"]
    assert result.outputs["accounts"][0]["gap_summary"]["status"] == "needs_plan"
    assert result.outputs["accounts"][0]["latest_orders"] == [
        {
            "id": 1,
            "recommendation_id": "rec-paper-prod-a",
            "signal_date": "2026-07-01",
            "execution_date": "",
            "instrument": "000001sz",
            "action": "buy",
            "current_shares": 0,
            "target_shares": 100,
            "delta_shares": 100,
            "target_weight": 0.05,
            "score": None,
            "target_value": 50_000.0,
            "estimated_price": None,
            "estimated_notional": None,
            "security_name": "平安银行",
        }
    ]
    assert result.outputs["accounts"][0]["security_names"]["000001sz"] == "平安银行"


def test_fleet_run_isolates_one_account_failure(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    for account_id in ("paper-prod-a", "paper-prod-b"):
        registry.upsert_account(_account(account_id))
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)
    monkeypatch.setattr(fleet, "FLEET_LATEST_STATUS_FILE", tmp_path / "latest.json")
    monkeypatch.setattr(
        fleet,
        "paper_fleet_preflight",
        lambda **kwargs: ok_result(
            outputs={
                "status": "go",
                "target_date": "2026-07-01",
                "data": {"data_package_id": "pkg-a", "qlib_latest": "2026-07-01"},
                "accounts": [_account("paper-prod-a"), _account("paper-prod-b")],
                "plans": [
                    ok_result(outputs={"plan": {"account_id": account_id, "trade_dates": ["2026-07-01"]}}).to_dict()
                    for account_id in ("paper-prod-a", "paper-prod-b")
                ],
            }
        ),
    )
    monkeypatch.setattr(
        fleet,
        "_run_account_plan",
        lambda **kwargs: ok_result(outputs={"status": "completed"})
        if kwargs["account"]["account_id"] == "paper-prod-a"
        else fleet.err_result("account-b-failed"),
    )
    monkeypatch.setattr(
        fleet,
        "paper_replay_run",
        lambda **kwargs: pytest.fail("fleet must call the shared account-day path, not replay"),
    )

    result = fleet.paper_fleet_run(target_date="2026-07-01")

    assert not result.ok
    assert result.err == "paper_fleet_partial_failed"
    assert result.outputs["completed_count"] == 1
    assert result.outputs["failed_count"] == 1
    saved = registry.get_fleet_run(result.outputs["fleet_run_id"])
    assert saved["status"] == "partial_failed"


def test_replay_resumes_failed_date_without_repeating_completed_days(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    registry.upsert_account(_account())
    registry.upsert_deployment(_deployment())
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)
    dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
    plan = {
        "account_id": "paper-prod-a",
        "trade_dates": dates,
        "deployments": [
            {"signal_date": value, "model_id": "model-a", "model_run_id": "run-a"}
            for value in dates
        ],
        "data": {
            "hdf5_latest": dates[-1],
            "qlib_latest": dates[-1],
            "quantgpt_latest": dates[-1],
            "production_health": "ready",
        },
        "requires_confirmation": False,
    }
    monkeypatch.setattr(fleet, "paper_replay_plan", lambda **kwargs: ok_result(outputs={"plan": plan}))
    monkeypatch.setattr(fleet, "_isolated_prediction_update", lambda **kwargs: ok_result(outputs={"status": "completed"}))
    monkeypatch.setattr(
        fleet,
        "_replay_score_quality",
        lambda **kwargs: {"status": "passed", "dates": [], "blockers": []},
    )
    monkeypatch.setattr(
        fleet,
        "load_stock_identity_rows_for_window",
        lambda start, end: pd.DataFrame({"trade_date": pd.to_datetime(dates)}),
    )

    def initialize(registry, *, account, deployment, signal_date):
        latest = registry.latest_account_snapshot(account["account_id"])
        if latest and latest["trade_date"] == signal_date:
            return {"status": "already_marked"}
        registry.record_account_snapshot(
            {
                "account_id": account["account_id"],
                "model_run_id": deployment["model_run_id"],
                "trade_date": signal_date,
                "cash": account["initial_capital"],
                "account_value": account["initial_capital"],
            }
        )
        return {"status": "marked"}

    monkeypatch.setattr(fleet, "_initialize_or_mark_account", initialize)
    next_dates = {"2026-07-01": "2026-07-02", "2026-07-02": "2026-07-03", "2026-07-03": ""}
    failed_once = {"value": False}

    def recommend(**kwargs):
        signal_date = kwargs["signal_date"]
        if signal_date == "2026-07-02" and not failed_once["value"]:
            failed_once["value"] = True
            return fleet.err_result("transient-recommendation-failure")
        rec = {
            "recommendation_id": f"rec-{signal_date}",
            "account_id": kwargs["account_id"],
            "model_id": kwargs["model_id"],
            "model_run_id": kwargs["model_run_id"],
            "signal_date": signal_date,
            "execution_date": next_dates[signal_date],
            "status": "pending",
            "topk": kwargs["topk"],
            "total_capital": kwargs["total_capital"],
        }
        registry.upsert_recommendation(rec)
        return ok_result(outputs={"recommendation": rec})

    monkeypatch.setattr(fleet, "trading_recommend", recommend)

    def execute_pending(*, recommendation_id, account_id, total_capital, include_status_snapshot=True):
        rec = registry.get_recommendation(recommendation_id)
        _commit_mock_execution(registry, rec, account_id=account_id, total_capital=total_capital)
        return ok_result(outputs={"executed": [{"recommendation_id": recommendation_id}], "failed": []})

    monkeypatch.setattr(fleet, "trading_execute_pending", execute_pending)

    first = fleet.paper_replay_run(account_id="paper-prod-a")
    assert not first.ok
    assert first.outputs["failed_date"] == "2026-07-02"
    assert registry.get_account_run(fleet._account_run_id("paper-prod-a", "2026-07-01", fleet._config_hash(_account(), _deployment())))["status"] == "completed"

    second = fleet.paper_replay_run(account_id="paper-prod-a")
    assert second.ok
    assert second.outputs["runs"][0]["status"] == "already_completed"
    resumed = registry.get_account_run(fleet._account_run_id("paper-prod-a", "2026-07-02", fleet._config_hash(_account(), _deployment())))
    assert resumed["status"] == "completed"
    assert resumed["attempt"] == 2
    assert [row["trade_date"] for row in registry.list_account_snapshots("paper-prod-a")] == dates


def test_rolling_champion_model_switch_preserves_inception_and_closes_deployment_boundary(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)

    def context(*, model_id=None, model_run_id=None, require_production=True):
        return {
            "model_id": model_id,
            "model_run_id": model_run_id,
            "feature_set_id": f"fs-{model_id}",
            "status": "production",
        }

    monkeypatch.setattr(fleet, "resolve_prediction_model_context", context)
    first = fleet.paper_account_create(
        account_id="paper-prod-switch",
        account_mode="rolling_champion",
        model_id="model-a",
        model_run_id="run-a",
        effective_from="2026-07-01",
        metadata={"owner": "desk-a"},
    )
    second = fleet.paper_account_create(
        account_id="paper-prod-switch",
        account_mode="rolling_champion",
        model_id="model-b",
        model_run_id="run-b",
        effective_from="2026-07-15",
        metadata={"note": "promoted successor"},
    )

    assert first.ok and second.ok
    account = registry.get_account("paper-prod-switch")
    assert account["metadata"]["inception_date"] == "2026-07-01"
    assert account["metadata"]["owner"] == "desk-a"
    assert account["metadata"]["note"] == "promoted successor"
    deployments = registry.list_deployments("paper-prod-switch")
    assert [(row["model_run_id"], row["effective_from"], row["effective_to"], row["status"]) for row in deployments] == [
        ("run-a", "2026-07-01", "2026-07-14", "retired"),
        ("run-b", "2026-07-15", "", "active"),
    ]
    assert registry.deployment_for_date("paper-prod-switch", "2026-07-14")["model_run_id"] == "run-a"
    assert registry.deployment_for_date("paper-prod-switch", "2026-07-15")["model_run_id"] == "run-b"


def test_fixed_model_account_rejects_model_switch_and_requires_new_account(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)
    monkeypatch.setattr(
        fleet,
        "resolve_prediction_model_context",
        lambda *, model_id=None, model_run_id=None, require_production=True: {
            "model_id": model_id,
            "model_run_id": model_run_id,
            "feature_set_id": f"fs-{model_id}",
            "status": "production",
        },
    )

    assert fleet.paper_account_create(
        account_id="paper-prod-fixed",
        model_id="model-a",
        model_run_id="run-a",
        effective_from="2026-07-01",
    ).ok
    switched = fleet.paper_account_create(
        account_id="paper-prod-fixed",
        model_id="model-b",
        model_run_id="run-b",
        effective_from="2026-07-15",
    )

    assert not switched.ok
    assert switched.err == "fixed_model_account_already_bound_create_new_account"
    assert switched.outputs["binding_contract"] == fleet.FIXED_MODEL_ACCOUNT_CONTRACT
    assert switched.outputs["required_action"] == "create_a_new_account_for_the_new_model"
    assert [row["model_run_id"] for row in registry.list_deployments("paper-prod-fixed")] == ["run-a"]


def test_manual_promotion_model_generates_account_name_and_source_tags(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)
    monkeypatch.setattr(
        fleet,
        "resolve_prediction_model_context",
        lambda *, model_id=None, model_run_id=None, require_production=True: {
            "model_id": "model-manual",
            "model_run_id": "model_prod_example_20260803T134215_0000",
            "feature_set_id": "fs-model-demo-set-20260803",
            "status": "production",
            "registry_row": {
                "model_id": "model-manual",
                "model_run_id": "model_prod_example_20260803T134215_0000",
                "feature_set_id": "fs-model-demo-set-20260803",
                "status": "production",
                "metadata": {
                    "manual_promotion_exception": {
                        "policy": "model_manual_promotion_exception_v1",
                        "source_campaign_status": "research",
                    },
                },
            },
        },
    )

    result = fleet.paper_account_create(
        account_id="paper-prod-manual",
        model_run_id="model_prod_example_20260803T134215_0000",
        display_name="Confidence Cash V2",
        effective_from="2026-07-01",
    )

    assert result.ok
    assert result.outputs["account"]["display_name"] == "手工晋升 · DEMO-SET · 2026-08-03 21:42"
    assert result.outputs["model_binding"]["tags"] == ["手工晋升", "研究来源"]
    assert result.warnings == ["requested_display_name_ignored_model_name_is_authoritative"]


def test_account_rejects_invalid_or_retroactive_deployment_boundary(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)
    monkeypatch.setattr(
        fleet,
        "resolve_prediction_model_context",
        lambda *, model_id=None, model_run_id=None, require_production=True: {
            "model_id": model_id,
            "model_run_id": model_run_id,
            "feature_set_id": "fs-a",
            "status": "production",
        },
    )
    invalid = fleet.paper_account_create(
        account_id="paper-prod-history",
        model_id="model-a",
        model_run_id="run-a",
        effective_from="20260701",
    )
    assert not invalid.ok
    assert "invalid_effective_from" in invalid.err

    assert fleet.paper_account_create(
        account_id="paper-prod-history",
        model_id="model-a",
        model_run_id="run-a",
        effective_from="2026-07-01",
    ).ok
    registry.upsert_account_run(
        {
            "account_run_id": "completed-2026-07-10",
            "account_id": "paper-prod-history",
            "signal_date": "2026-07-10",
            "status": "completed",
        }
    )
    retroactive = fleet.paper_account_create(
        account_id="paper-prod-history",
        model_id="model-b",
        model_run_id="run-b",
        effective_from="2026-07-05",
    )
    assert not retroactive.ok
    assert retroactive.err == "deployment_history_immutable_after_completed_run"
    assert len(registry.list_deployments("paper-prod-history")) == 1


def test_account_cannot_reactivate_with_integrity_error(monkeypatch, tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    account = _account("paper-corrupt")
    account["status"] = "paused"
    registry.upsert_account(account)
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-lost",
            "account_id": account["account_id"],
            "model_run_id": "run-a",
            "signal_date": "2026-07-01",
            "status": "superseded",
        }
    )
    registry.record_account_snapshot(
        {
            "account_id": account["account_id"],
            "model_run_id": "run-a",
            "trade_date": "2026-07-02",
            "cash": 1_000_000,
            "account_value": 1_000_000,
        }
    )
    monkeypatch.setattr(fleet, "TradingRegistry", lambda: registry)

    result = fleet.paper_account_set_status(account_id=account["account_id"], status="active")

    assert not result.ok
    assert result.err == "paper_account_integrity_blocked"
    assert registry.get_account(account["account_id"])["status"] == "paused"


def test_fleet_and_replay_share_cross_process_write_lock():
    assert fleet._paper_operation_lock_status()["status"] == "idle"
    with fleet._paper_operation_lock():
        assert fleet._paper_operation_lock_status()["status"] == "held"
        replay = fleet.paper_replay_run(account_id="paper-prod-a")
        fleet_run = fleet.paper_fleet_run()

    assert not replay.ok and replay.err == "paper_operation_in_progress"
    assert not fleet_run.ok and fleet_run.err == "paper_operation_in_progress"
    assert replay.outputs["lock_file"] == str(fleet.PAPER_OPERATION_LOCK_FILE)
    assert fleet._paper_operation_lock_status()["status"] == "idle"


def test_paper_account_market_context_adds_names_and_daily_trades(tmp_path: Path):
    trades_file = tmp_path / "fills.csv"
    pd.DataFrame(
        [
            {
                "instrument": "000001sz",
                "action": "buy",
                "filled_amount": 100,
                "price": 12.5,
                "trade_value": 1250,
                "cost": 3.1,
                "status": "filled",
            }
        ]
    ).to_csv(trades_file, index=False)
    context = fleet._paper_account_market_context(
        [
            {
                "trade_date": "2026-07-01",
                "positions": {"000001sz": {"market_value": 1250}},
                "output_files": {"trades_file": str(trades_file)},
            }
        ],
        {"000001sz": "平安银行"},
    )

    assert context["security_names"]["000001sz"] == "平安银行"
    assert context["daily_trades"]["2026-07-01"] == [
        {
            "instrument": "000001sz",
            "security_name": "平安银行",
            "action": "buy",
            "filled_amount": 100,
            "price": 12.5,
            "trade_value": 1250,
            "cost": 3.1,
            "status": "filled",
        }
    ]
