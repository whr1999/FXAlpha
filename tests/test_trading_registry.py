from pathlib import Path
import sqlite3

from storage.trading_registry import TradingRegistry


def test_recommendation_registry_roundtrip(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")
    payload = {
        "recommendation_id": "rec-model-2026-05-19",
        "model_id": "m1",
        "model_run_id": "run1",
        "signal_date": "2026-05-19",
        "execution_date": "2026-05-20",
        "status": "pending",
        "topk": 2,
        "total_capital": 1000000,
        "score_file": "score.csv",
        "target_file": "target.csv",
        "metrics": {"score_quality": {"unique_score_count": 100, "score_std": 0.12}},
        "warnings": ["paper-only"],
    }
    orders = [
        {
            "instrument": "SZ000001",
            "action": "sell",
            "current_shares": 10000,
            "target_shares": 0,
            "delta_shares": -10000,
            "target_weight": 0,
        },
        {
            "instrument": "SH600000",
            "action": "buy",
            "current_shares": 0,
            "target_shares": 100,
            "delta_shares": 100,
            "target_weight": 0.5,
            "score": 0.8,
        },
        {
            "instrument": "SH600010",
            "action": "buy",
            "current_shares": 0,
            "target_shares": 100,
            "delta_shares": 100,
            "target_weight": 0.4,
            "score": 0.9,
        },
    ]

    registry.upsert_recommendation(payload, orders=orders)
    latest = registry.latest_recommendation()

    assert latest["recommendation_id"] == payload["recommendation_id"]
    assert latest["warnings"] == ["paper-only"]
    assert latest["metrics"]["score_quality"]["unique_score_count"] == 100
    assert registry.pending_recommendations()[0]["model_run_id"] == "run1"
    listed_orders = registry.list_orders(payload["recommendation_id"])
    assert [row["instrument"] for row in listed_orders] == ["SH600000", "SH600010", "SZ000001"]
    assert listed_orders[0]["action"] == "buy"


def test_registry_updates_execution_state(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")
    rec_id = "rec-run1-2026-05-19"
    registry.upsert_recommendation(
        {
            "recommendation_id": rec_id,
            "model_id": "m1",
            "model_run_id": "run1",
            "signal_date": "2026-05-19",
            "execution_date": "",
            "status": "pending",
        }
    )

    registry.set_execution_date(rec_id, "2026-05-20")
    registry.mark_recommendation(
        rec_id,
        status="executed",
        metrics={"daily_pnl": 12.3},
        execution_files={"ledger_file": "ledger.csv"},
    )
    registry.record_execution(
        {
            "execution_id": "exec-1",
            "recommendation_id": rec_id,
            "model_id": "m1",
            "model_run_id": "run1",
            "trade_date": "2026-05-20",
            "status": "completed",
            "metrics": {"daily_pnl": 12.3},
        }
    )

    rec = registry.get_recommendation(rec_id)
    assert rec["execution_date"] == "2026-05-20"
    assert rec["status"] == "executed"
    assert rec["metrics"]["daily_pnl"] == 12.3
    assert registry.latest_execution()["trade_date"] == "2026-05-20"
    assert registry.summary()["paper_executions"] == 1


def test_registry_does_not_supersede_unexecuted_prior_signal(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")
    for rec_id, signal_date in [("rec-old", "2026-05-19"), ("rec-new", "2026-05-20")]:
        registry.upsert_recommendation(
            {
                "recommendation_id": rec_id,
                "model_id": "m1",
                "model_run_id": "run1",
                "signal_date": signal_date,
                "execution_date": "2026-05-21",
                "status": "pending",
            }
        )

    count = registry.supersede_pending_except(
        model_run_id="run1",
        keep_recommendation_id="rec-new",
        reason="newer recommendation",
    )

    assert count == 0
    assert registry.get_recommendation("rec-old")["status"] == "pending"
    assert registry.get_recommendation("rec-new")["status"] == "pending"


def test_registry_integrity_detects_lost_superseded_intent(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-lost",
            "account_id": "paper-a",
            "model_run_id": "run-a",
            "signal_date": "2026-05-19",
            "status": "superseded",
        }
    )
    registry.record_account_snapshot(
        {
            "account_id": "paper-a",
            "model_run_id": "run-a",
            "trade_date": "2026-05-20",
            "cash": 1000,
            "stock_value": 0,
            "account_value": 1000,
        }
    )

    issues = registry.account_integrity_issues("paper-a")

    assert issues == [
        {
            "code": "unexecuted_recommendation_superseded",
            "recommendation_id": "rec-lost",
            "signal_date": "2026-05-19",
            "execution_date": "",
        }
    ]


def test_registry_integrity_accepts_superseded_sibling_when_canonical_same_day_executed(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-old",
            "account_id": "paper-a",
            "model_id": "model-a",
            "model_run_id": "run-a",
            "signal_date": "2026-05-19",
            "status": "superseded",
        }
    )
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-canonical",
            "account_id": "paper-a",
            "model_id": "model-a",
            "model_run_id": "run-a",
            "signal_date": "2026-05-19",
            "execution_date": "2026-05-20",
            "status": "pending",
        }
    )
    registry.commit_execution(
        execution={
            "execution_id": "exec-canonical",
            "account_id": "paper-a",
            "recommendation_id": "rec-canonical",
            "model_id": "model-a",
            "model_run_id": "run-a",
            "trade_date": "2026-05-20",
            "status": "completed",
        },
        recommendation_id="rec-canonical",
        recommendation_status="executed",
        snapshot={
            "account_id": "paper-a",
            "model_run_id": "run-a",
            "trade_date": "2026-05-20",
            "source_recommendation_id": "rec-canonical",
            "cash": 1000,
            "stock_value": 0,
            "account_value": 1000,
        },
    )

    assert registry.account_integrity_issues("paper-a") == []


def test_registry_records_latest_paper_account_snapshot(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")
    registry.record_account_snapshot(
        {
            "account_id": "run1",
            "model_run_id": "run1",
            "trade_date": "2026-05-20",
            "source_recommendation_id": "rec-1",
            "cash": 100.0,
            "stock_value": 900.0,
            "account_value": 1000.0,
            "positions": {"SH600000": {"amount": 100, "price": 9.0}},
            "score_hash": "score-hash",
            "target_hash": "target-hash",
            "fills_hash": "fills-hash",
            "output_files": {"ledger_file": "ledger.csv"},
        }
    )

    latest = registry.latest_account_snapshot("run1")

    assert latest["account_id"] == "run1"
    assert latest["trade_date"] == "2026-05-20"
    assert latest["positions"]["SH600000"]["amount"] == 100
    assert latest["output_files"]["ledger_file"] == "ledger.csv"
    assert registry.summary()["paper_account_snapshots"] == 1


def test_registry_lists_account_history_and_latest_accounts(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")
    for account_id, trade_date, value in [
        ("run1", "2026-05-20", 1000.0),
        ("run1", "2026-05-21", 1010.0),
        ("run2", "2026-05-21", 990.0),
    ]:
        registry.record_account_snapshot(
            {
                "account_id": account_id,
                "model_run_id": account_id,
                "trade_date": trade_date,
                "cash": value,
                "account_value": value,
                "positions": {},
            }
        )

    history = registry.list_account_snapshots("run1")
    latest_accounts = registry.list_latest_accounts()

    assert [row["trade_date"] for row in history] == ["2026-05-20", "2026-05-21"]
    assert history[0]["daily_pnl"] == 0.0
    assert history[1]["daily_pnl"] == 10.0
    assert {row["account_id"] for row in latest_accounts} == {"run1", "run2"}


def test_registry_latest_execution_can_filter_model_run(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")
    for rec_id, run_id, trade_date in [
        ("rec-run1", "run1", "2026-05-20"),
        ("rec-run2", "run2", "2026-05-21"),
    ]:
        registry.upsert_recommendation(
            {
                "recommendation_id": rec_id,
                "model_id": run_id.replace("run", "m"),
                "model_run_id": run_id,
                "signal_date": trade_date,
                "execution_date": trade_date,
                "status": "executed",
            }
        )
        registry.record_execution(
            {
                "execution_id": f"exec-{run_id}",
                "recommendation_id": rec_id,
                "model_id": run_id.replace("run", "m"),
                "model_run_id": run_id,
                "trade_date": trade_date,
                "status": "completed",
            }
        )

    assert registry.latest_execution("run1")["model_run_id"] == "run1"
    assert registry.latest_execution("run2")["model_run_id"] == "run2"
    assert registry.latest_execution()["model_run_id"] == "run2"


def test_registry_migrates_legacy_recommendations_without_losing_rows(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE recommendation_batches (
            recommendation_id TEXT PRIMARY KEY,
            model_id TEXT NOT NULL DEFAULT '',
            model_run_id TEXT NOT NULL DEFAULT '',
            signal_date TEXT NOT NULL,
            execution_date TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            topk INTEGER DEFAULT 20,
            total_capital REAL DEFAULT 0,
            score_file TEXT DEFAULT '', target_file TEXT DEFAULT '',
            decision_file TEXT DEFAULT '', order_preview_file TEXT DEFAULT '',
            recommendation_file TEXT DEFAULT '', execution_files TEXT DEFAULT '{}',
            metrics TEXT DEFAULT '{}', warnings TEXT DEFAULT '[]', error TEXT DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO recommendation_batches
        (recommendation_id, model_id, model_run_id, signal_date, created_at, updated_at)
        VALUES ('legacy-rec', 'model-a', 'run-a', '2026-07-01', 'now', 'now');
        """
    )
    conn.commit()
    conn.close()

    registry = TradingRegistry(db_path)
    migrated = registry.get_recommendation("legacy-rec")

    assert migrated["account_id"] == "run-a"
    assert migrated["n_drop"] == 2
    assert migrated["hold_thresh"] == 5
    assert migrated["deal_price"] == "open"
    assert migrated["run_kind"] == "on_time"

    registry.upsert_recommendation(
        {
            **migrated,
            "n_drop": 4,
            "hold_thresh": 8,
            "deal_price": "close",
            "strategy_contract_version": "custom-v2",
            "run_kind": "catch_up_replay",
            "data_package_id": "pkg-a",
        }
    )
    updated = registry.get_recommendation("legacy-rec")
    assert (updated["n_drop"], updated["hold_thresh"], updated["deal_price"]) == (4, 8, "close")
    assert updated["strategy_contract_version"] == "custom-v2"
    assert updated["data_package_id"] == "pkg-a"


def test_run_timestamps_cannot_finish_before_they_start(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")
    started_at = "2026-08-06T14:46:37.000000+00:00"
    clock_stepped_back = "2026-08-06T14:46:36.900000+00:00"
    registry.upsert_account({"account_id": "account-a"})

    account_run = {
        "account_run_id": "run-account-date",
        "account_id": "account-a",
        "signal_date": "2026-08-05",
        "started_at": started_at,
        "status": "running",
    }
    registry.upsert_account_run(account_run)
    registry.upsert_account_run(
        {**account_run, "status": "completed", "completed_at": clock_stepped_back}
    )
    assert registry.get_account_run("run-account-date")["completed_at"] == started_at

    fleet_run = {
        "fleet_run_id": "fleet-date",
        "target_date": "2026-08-05",
        "started_at": started_at,
        "status": "running",
    }
    registry.upsert_fleet_run(fleet_run)
    registry.upsert_fleet_run(
        {**fleet_run, "status": "completed", "completed_at": clock_stepped_back}
    )
    assert registry.get_fleet_run("fleet-date")["completed_at"] == started_at


def test_account_creation_and_deployment_are_committed_together(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")

    account_id, deployment_id = registry.upsert_account_with_deployment(
        account={"account_id": "paper-a", "status": "active"},
        deployment={
            "deployment_id": "deploy-a",
            "account_id": "paper-a",
            "model_id": "model-a",
            "model_run_id": "run-a",
            "feature_set_id": "fs-a",
            "effective_from": "2026-08-01",
        },
    )

    assert account_id == "paper-a"
    assert deployment_id == "deploy-a"
    assert registry.get_account("paper-a")["status"] == "active"
    assert registry.list_deployments("paper-a")[0]["model_run_id"] == "run-a"


def test_retiring_account_atomically_settles_pending_plans(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")
    registry.upsert_account({"account_id": "paper-a", "status": "active"})
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-a",
            "account_id": "paper-a",
            "model_run_id": "run-a",
            "signal_date": "2026-08-07",
            "status": "pending",
        }
    )

    transition = registry.transition_account_status("paper-a", "retired")

    assert transition["previous_status"] == "active"
    assert transition["retired_pending_settled"] == 1
    assert registry.get_account("paper-a")["status"] == "retired"
    recommendation = registry.get_recommendation("rec-a")
    assert recommendation["status"] == "superseded"
    assert recommendation["error"] == "account_retired_pending_cancelled"
    assert registry.summary()["pending_retired"] == 0


def test_stale_running_account_runs_reconcile_from_durable_evidence(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "execution_log.db")
    registry.upsert_account({"account_id": "paper-a", "status": "paused"})
    for run_id, signal_date in (("run-complete", "2026-08-06"), ("run-interrupted", "2026-08-07")):
        registry.upsert_account_run(
            {
                "account_run_id": run_id,
                "account_id": "paper-a",
                "signal_date": signal_date,
                "status": "running",
            }
        )
    registry.upsert_recommendation(
        {
            "recommendation_id": "rec-complete",
            "account_id": "paper-a",
            "model_run_id": "run-a",
            "signal_date": "2026-08-06",
            "status": "pending",
        }
    )
    registry.record_account_snapshot(
        {
            "account_id": "paper-a",
            "model_run_id": "run-a",
            "trade_date": "2026-08-06",
            "cash": 1000,
            "stock_value": 0,
            "account_value": 1000,
        }
    )

    actions = registry.reconcile_stale_account_runs("paper-a")

    assert {row["account_run_id"]: row["status"] for row in actions} == {
        "run-complete": "completed",
        "run-interrupted": "failed",
    }
    assert registry.get_account_run("run-complete")["current_stage"] == "recovered_completed"
    assert registry.get_account_run("run-interrupted")["error"] == "abandoned_running_attempt_without_complete_evidence"
