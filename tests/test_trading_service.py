import pytest

from services._base import ok_result

trading_service = pytest.importorskip("services.trading_service")


def _allow_production_model(monkeypatch):
    monkeypatch.setattr(
        "services.model_service.model_production_status",
        lambda: ok_result(outputs={"status": "ready", "production_model": {"model_id": "m-1", "model_run_id": "run-1"}}),
    )


def test_daily_routine_executes_pending_before_recommend(monkeypatch):
    calls = []
    _allow_production_model(monkeypatch)

    class FakeRegistry:
        def pending_recommendations(self, limit=50):
            return []

    def fake_execute_pending(**kwargs):
        calls.append("execute")
        return ok_result(outputs={"status": "completed"})

    def fake_recommend(**kwargs):
        calls.append("recommend")
        return ok_result(outputs={"status": "pending", "recommendation": {"recommendation_id": "rec-1"}})

    monkeypatch.setattr(trading_service, "TradingRegistry", FakeRegistry)
    monkeypatch.setattr(trading_service, "trading_execute_pending", fake_execute_pending)
    monkeypatch.setattr(trading_service, "trading_recommend", fake_recommend)

    result = trading_service.trading_daily_routine(topk=10, total_capital=1000)

    assert result.ok
    assert calls == ["execute", "recommend"]
    assert result.outputs["status"] == "completed"


def test_daily_routine_waits_when_pending_execution_is_skipped(monkeypatch):
    calls = []
    _allow_production_model(monkeypatch)

    class FakeRegistry:
        def pending_recommendations(self, limit=50):
            return []

    def fake_execute_pending(**kwargs):
        calls.append("execute")
        return ok_result(
            outputs={
                "status": "completed",
                "skipped": [{"recommendation_id": "rec-1", "reason": "execution_date_unavailable"}],
            },
            warnings=["execution_date_unavailable"],
        )

    def fake_recommend(**kwargs):
        calls.append("recommend")
        return ok_result(outputs={"status": "pending"})

    monkeypatch.setattr(trading_service, "TradingRegistry", FakeRegistry)
    monkeypatch.setattr(trading_service, "trading_execute_pending", fake_execute_pending)
    monkeypatch.setattr(trading_service, "trading_recommend", fake_recommend)

    result = trading_service.trading_daily_routine(topk=10, total_capital=1000)

    assert result.ok
    assert calls == ["execute"]
    assert result.outputs["status"] == "waiting"
    assert result.outputs["blocked_reason"] == "waiting_for_pending_execution_date"
    assert result.outputs["recommend"] is None


def test_daily_routine_blocks_when_pending_execution_fails(monkeypatch):
    calls = []
    _allow_production_model(monkeypatch)

    class FakeRegistry:
        def pending_recommendations(self, limit=50):
            return []

    def fake_execute_pending(**kwargs):
        calls.append("execute")
        return ok_result(
            outputs={
                "status": "partial_failed",
                "failed": [{"recommendation_id": "rec-1", "error": "qlib paper failed"}],
                "skipped": [],
            }
        )

    def fake_recommend(**kwargs):
        calls.append("recommend")
        return ok_result(outputs={"status": "pending"})

    monkeypatch.setattr(trading_service, "TradingRegistry", FakeRegistry)
    monkeypatch.setattr(trading_service, "trading_execute_pending", fake_execute_pending)
    monkeypatch.setattr(trading_service, "trading_recommend", fake_recommend)

    result = trading_service.trading_daily_routine(topk=10, total_capital=1000)

    assert not result.ok
    assert calls == ["execute"]
    assert result.outputs["status"] == "blocked"
    assert result.outputs["blocked_reason"] == "pending_execution_failed"
    assert result.outputs["recommend"] is None


def test_daily_routine_blocks_when_pending_model_mismatches_current_model(monkeypatch):
    calls = []
    _allow_production_model(monkeypatch)

    class FakeRegistry:
        def pending_recommendations(self, limit=50):
            return [
                {
                    "recommendation_id": "rec-old",
                    "status": "pending",
                    "signal_date": "2026-05-19",
                    "execution_date": "2026-05-20",
                    "model_run_id": "run-old",
                    "topk": 10,
                }
            ]

        def mark_recommendation(self, *args, **kwargs):
            raise AssertionError("routine must not auto-supersede mismatched pending recommendations")

    def fake_execute_pending(**kwargs):
        calls.append("execute")
        return ok_result(outputs={"status": "completed"})

    def fake_recommend(**kwargs):
        calls.append("recommend")
        return ok_result(outputs={"status": "pending"})

    monkeypatch.setattr(trading_service, "TradingRegistry", FakeRegistry)
    monkeypatch.setattr(trading_service, "resolve_prediction_model_context", lambda **kwargs: {"model_run_id": "run-new"})
    monkeypatch.setattr(trading_service, "trading_execute_pending", fake_execute_pending)
    monkeypatch.setattr(trading_service, "trading_recommend", fake_recommend)

    result = trading_service.trading_daily_routine(topk=10, total_capital=1000)

    assert not result.ok
    assert result.err == "pending_production_model_mismatch"
    assert result.outputs["status"] == "blocked"
    assert result.outputs["blocked_reason"] == "pending_production_model_mismatch"
    assert result.outputs["mismatched_pending"][0]["recommendation_id"] == "rec-old"
    assert calls == []


def test_daily_routine_blocks_when_trading_lock_active(monkeypatch, tmp_path):
    _allow_production_model(monkeypatch)
    lock_dir = tmp_path / "trading_update.lock"
    lock_dir.mkdir()
    (lock_dir / "owner.json").write_text('{"pid": 1, "started_at": "now"}', encoding="utf-8")
    monkeypatch.setattr(trading_service, "TRADING_LOCK_DIR", lock_dir)
    monkeypatch.setattr(trading_service, "_pid_alive", lambda pid: True)

    result = trading_service.trading_daily_routine(topk=10, total_capital=1000)

    assert not result.ok
    assert result.err == "trading_update_lock_active"
    assert result.outputs["status"] == "blocked"
    assert result.outputs["blocked_reason"] == "trading_update_lock_active"


def test_daily_routine_blocks_when_production_validation_blocks(monkeypatch):
    monkeypatch.setattr(
        "services.model_service.model_production_status",
        lambda: ok_result(outputs={
            "status": "production_blocked",
            "production_model": {"model_id": "m-1", "model_run_id": "run-1"},
            "production_validation": {
                "status": "blocked",
                "hard_blocks": ["label_overlap"],
                "warnings": [],
                "artifact_path": "/tmp/validation_audit.json",
            },
        }),
    )

    result = trading_service.trading_daily_routine(topk=10, total_capital=1000)

    assert not result.ok
    assert result.err == "production_model_not_ready"
    assert result.outputs["status"] == "blocked"
    assert result.outputs["production_validation_summary"]["hard_blocks"] == ["label_overlap"]


def test_daily_routine_reclaims_stale_trading_lock(monkeypatch, tmp_path):
    lock_dir = tmp_path / "trading_update.lock"
    lock_dir.mkdir()
    (lock_dir / "owner.json").write_text('{"pid": 999999999, "started_at": "old"}', encoding="utf-8")
    calls = []
    _allow_production_model(monkeypatch)

    class FakeRegistry:
        def pending_recommendations(self, limit=50):
            return []

    def fake_execute_pending(**kwargs):
        calls.append("execute")
        return ok_result(outputs={"status": "completed"})

    def fake_recommend(**kwargs):
        calls.append("recommend")
        return ok_result(outputs={"status": "pending", "recommendation": {"recommendation_id": "rec-1"}})

    monkeypatch.setattr(trading_service, "TRADING_LOCK_DIR", lock_dir)
    monkeypatch.setattr(trading_service, "TradingRegistry", FakeRegistry)
    monkeypatch.setattr(trading_service, "trading_execute_pending", fake_execute_pending)
    monkeypatch.setattr(trading_service, "trading_recommend", fake_recommend)

    result = trading_service.trading_daily_routine(topk=10, total_capital=1000)

    assert result.ok
    assert calls == ["execute", "recommend"]
    assert "stale_trading_lock_reclaimed" in result.warnings
    assert not lock_dir.exists()


def test_execute_pending_skips_non_pending_recommendation(monkeypatch):
    class FakeRegistry:
        def get_account(self, account_id):
            return {"account_id": account_id, "status": "active"}

        def get_recommendation(self, recommendation_id):
            return {
                "recommendation_id": recommendation_id,
                "account_id": "paper-a",
                "status": "executed",
                "signal_date": "2026-05-19",
                "execution_date": "2026-05-20",
            }

        def pending_recommendations(self, limit=50):
            return []

        def latest_recommendation(self):
            return None

        def latest_execution(self):
            return None

        def summary(self):
            return {}

    monkeypatch.setattr(trading_service, "TradingRegistry", FakeRegistry)
    monkeypatch.setattr(trading_service, "trading_status", lambda **kwargs: ok_result(outputs={}))
    result = trading_service.trading_execute_pending(recommendation_id="rec-1")

    assert result.ok
    assert result.outputs["executed"] == []
    assert result.outputs["skipped"][0]["reason"] == "recommendation_not_pending:executed"


def test_execute_pending_rejects_global_account_scan(monkeypatch):
    class FakeRegistry:
        def get_recommendation(self, recommendation_id):
            return None

    monkeypatch.setattr(trading_service, "TradingRegistry", FakeRegistry)

    result = trading_service.trading_execute_pending()

    assert not result.ok
    assert result.err == "paper_account_id_required"
    assert result.outputs["status"] == "blocked"


def test_execute_pending_skips_invalid_execution_date(monkeypatch):
    class FakeRegistry:
        def get_account(self, account_id):
            return {"account_id": account_id, "status": "active"}

        def pending_recommendations(self, limit=50, account_id=None):
            return [
                {
                    "recommendation_id": "rec-1",
                    "account_id": account_id,
                    "status": "pending",
                    "signal_date": "2026-05-19",
                    "execution_date": "not-a-date",
                }
            ]

        def latest_recommendation(self):
            return None

        def latest_execution(self):
            return None

        def summary(self):
            return {}

    monkeypatch.setattr(trading_service, "TradingRegistry", FakeRegistry)
    monkeypatch.setattr(trading_service, "has_qlib_trade_date", lambda value: (_ for _ in ()).throw(ValueError("bad date")))
    monkeypatch.setattr(trading_service, "trading_status", lambda **kwargs: ok_result(outputs={}))

    result = trading_service.trading_execute_pending(account_id="paper-a")

    assert result.ok
    assert result.outputs["executed"] == []
    assert result.outputs["skipped"][0]["reason"] == "execution_date_invalid"


def test_execute_pending_marks_not_ok_qlib_paper_result_as_failed(monkeypatch):
    from domain.trading.execution.base import ExecutionResult

    rec = {
        "recommendation_id": "rec-1",
        "status": "pending",
        "model_id": "m-1",
        "model_run_id": "run-1",
        "signal_date": "2026-05-19",
        "execution_date": "2026-05-20",
        "score_file": "/tmp/score.csv",
        "target_file": "/tmp/target.csv",
        "total_capital": 1000,
    }
    calls = {"marked": [], "executions": []}

    class FakeRegistry:
        def get_account(self, account_id):
            return {"account_id": account_id, "status": "active"}

        def pending_recommendations(self, limit=50, account_id=None):
            return [rec]

        def commit_execution(self, *, execution, recommendation_id, recommendation_status, snapshot=None, error=""):
            calls["executions"].append(execution)
            calls["marked"].append({"recommendation_id": recommendation_id, "status": recommendation_status, "error": error})

    def fake_run_qlib_paper_execution(inputs, **kwargs):
        return ExecutionResult(
            ok=False,
            adapter="qlib_exchange_paper",
            version_id=inputs.version_id,
            trade_date=inputs.trade_date,
            metrics={"trade_count": 0},
            diagnostics={"reason": "simulated_failure"},
        )

    monkeypatch.setattr(trading_service, "TradingRegistry", FakeRegistry)
    monkeypatch.setattr(trading_service, "has_qlib_trade_date", lambda value: True)
    monkeypatch.setattr(trading_service, "trading_status", lambda **kwargs: ok_result(outputs={}))
    monkeypatch.setattr(trading_service, "run_qlib_paper_execution", fake_run_qlib_paper_execution)

    result = trading_service.trading_execute_pending(account_id="paper-a")

    assert not result.ok
    assert result.err == "paper_execution_failed"
    assert result.outputs["status"] == "partial_failed"
    assert result.outputs["executed"] == []
    assert result.outputs["failed"][0]["recommendation_id"] == "rec-1"
    assert calls["marked"] == [{"recommendation_id": "rec-1", "status": "failed", "error": "qlib paper execution returned not ok"}]
    assert calls["executions"][0]["status"] == "failed"


def test_latest_execution_summary_reads_ledger_positions_and_trades(tmp_path):
    ledger = tmp_path / "daily_ledger.csv"
    positions = tmp_path / "positions.csv"
    trades = tmp_path / "trades.csv"
    ledger.write_text("trade_date,ending_account_value,daily_pnl\n2026-05-20,1001000,1000\n", encoding="utf-8")
    positions.write_text("instrument,shares,price\nSH600000,100,10\n", encoding="utf-8")
    trades.write_text("instrument,direction,price,shares\nSH600000,buy,10,100\n", encoding="utf-8")

    summary = trading_service._latest_execution_summary(
        {
            "execution_id": "exec-1",
            "recommendation_id": "rec-1",
            "trade_date": "2026-05-20",
            "status": "completed",
            "metrics": {"trade_count": 1},
            "output_files": {
                "ledger_file": str(ledger),
                "holdings_file": str(positions),
                "trades_file": str(trades),
            },
        }
    )

    assert summary["ledger_rows"][0]["daily_pnl"] == 1000
    assert summary["position_rows"][0]["instrument"] == "SH600000"
    assert summary["trade_rows"][0]["direction"] == "buy"


def test_status_warning_detects_model_switch():
    prediction = ok_result(outputs={"run_context": {"model_run_id": "run-new"}})

    warnings = trading_service._status_warnings(
        latest_recommendation={"model_run_id": "run-old", "status": "pending"},
        pending_recommendations=[{"recommendation_id": "rec-1"}],
        latest_execution=None,
        prediction=prediction,
    )

    assert any("production model changed" in item for item in warnings)
    assert any("pending recommendation" in item for item in warnings)


def test_status_warning_detects_degenerate_recommendation_score():
    prediction = ok_result(outputs={"run_context": {"model_run_id": "run-1"}})

    warnings = trading_service._status_warnings(
        latest_recommendation={
            "model_run_id": "run-1",
            "status": "pending",
            "topk": 10,
            "metrics": {
                "score_quality": {
                    "record_count": 3478,
                    "unique_score_count": 1,
                    "score_std": 0.0,
                }
            },
        },
        pending_recommendations=[],
        latest_execution=None,
        prediction=prediction,
    )

    assert any("prediction score degenerate" in item for item in warnings)


def test_trading_status_exposes_production_validation_blocker(monkeypatch):
    class FakeRegistry:
        def latest_recommendation(self):
            return None

        def pending_recommendations(self, limit=20):
            return []

        def latest_execution(self):
            return None

        def summary(self):
            return {"pending": 0, "failed": 0}

    monkeypatch.setattr(trading_service, "TradingRegistry", FakeRegistry)
    monkeypatch.setattr(
        "services.model_service.model_production_status",
        lambda: ok_result(outputs={
            "status": "production_blocked",
            "production_model": {"model_id": "m-1", "model_run_id": "run-1"},
            "production_validation": {
                "status": "blocked",
                "hard_blocks": ["label_overlap"],
                "warnings": [],
                "artifact_path": "/tmp/validation_audit.json",
            },
        }),
    )

    result = trading_service.trading_status(
        prediction=ok_result(outputs={"status": "ready", "qlib_latest": "2026-05-28 00:00:00"})
    )

    assert result.ok
    assert result.outputs["status"] == "blocked"
    assert "production_model_not_ready" in result.outputs["blockers"]
    assert "production_validation:label_overlap" in result.outputs["blockers"]
    assert result.outputs["production_validation_summary"]["hard_blocks"] == ["label_overlap"]


def test_trading_status_derives_latest_paper_execution_from_account_snapshot(monkeypatch):
    snapshot = {
        "account_id": "run-1",
        "model_run_id": "run-1",
        "trade_date": "2026-05-22",
        "account_value": 1010.0,
        "cash": 100.0,
        "stock_value": 910.0,
        "positions": {"SH600000": {"amount": 100}},
        "output_files": {"ledger_file": "/tmp/ledger.csv"},
    }

    class FakeRegistry:
        def latest_recommendation(self):
            return None

        def pending_recommendations(self, limit=20):
            return []

        def latest_execution(self, *args):
            return None

        def latest_account_snapshot(self, account_id=None):
            return snapshot

        def list_account_snapshots(self, account_id, limit=260):
            return [snapshot]

        def list_latest_accounts(self, limit=50):
            return [snapshot]

        def summary(self):
            return {"pending": 0, "failed": 0, "paper_account_snapshots": 1}

    monkeypatch.setattr(trading_service, "TradingRegistry", FakeRegistry)
    monkeypatch.setattr(
        "services.model_service.model_production_status",
        lambda: ok_result(outputs={"status": "ready", "production_model": {"model_id": "m-1", "model_run_id": "run-1"}}),
    )

    result = trading_service.trading_status(
        model_run_id="run-1",
        prediction=ok_result(outputs={"status": "ready", "qlib_latest": "2026-05-22 00:00:00"}),
    )

    assert result.ok
    latest = result.outputs["latest_qlib_paper_execution"]
    assert latest["trade_date"] == "2026-05-22"
    assert latest["metrics"]["account_value"] == 1010.0
    assert result.outputs["qlib_paper_account_history"][0]["trade_date"] == "2026-05-22"


def test_preflight_waits_for_same_day_pending_recommendation(monkeypatch):
    rec = {
        "recommendation_id": "rec-1",
        "signal_date": "2026-05-28",
        "execution_date": "",
        "status": "pending",
        "topk": 10,
        "warnings": ["execution_date_unresolved: next trading date is not present in qlib calendar yet"],
    }

    monkeypatch.setattr("services.data_foundation_service.data_status", lambda: ok_result(outputs={"status": "completed"}))
    monkeypatch.setattr("services.model_service.model_production_status", lambda: ok_result(outputs={"status": "ready"}))
    monkeypatch.setattr(trading_service, "pred_update", lambda **kwargs: ok_result(outputs={"target_date": "2026-05-28"}))
    monkeypatch.setattr(
        trading_service,
        "trading_status",
        lambda **kwargs: ok_result(outputs={
            "qlib_latest": "2026-05-28 00:00:00",
            "registry": {"failed": 0},
            "latest_recommendation": rec,
            "pending_recommendations": [rec],
            "latest_execution": {"status": "completed"},
        }, warnings=["1 pending recommendation(s) waiting for execution"]),
    )
    monkeypatch.setattr(trading_service, "_active_fxalpha_processes", lambda: {"count": 0, "matches": []})
    monkeypatch.setattr(trading_service, "_quantgpt_health", lambda: {"ok": True, "payload": {"active_tasks": 0}})

    result = trading_service.trading_daily_preflight(topk=10)

    assert result.ok
    assert result.outputs["status"] == "waiting"
    assert result.outputs["waiting_reason"] == "waiting_for_next_trade_date"
    assert result.outputs["recommended_commands"]["daily_routine"] is None


def test_preflight_blocks_pending_topk_mismatch(monkeypatch):
    rec = {
        "recommendation_id": "rec-1",
        "signal_date": "2026-05-27",
        "execution_date": "2026-05-28",
        "status": "pending",
        "topk": 50,
    }

    monkeypatch.setattr("services.data_foundation_service.data_status", lambda: ok_result(outputs={"status": "completed"}))
    monkeypatch.setattr("services.model_service.model_production_status", lambda: ok_result(outputs={"status": "ready"}))
    monkeypatch.setattr(trading_service, "pred_update", lambda **kwargs: ok_result(outputs={"target_date": "2026-05-28"}))
    monkeypatch.setattr(
        trading_service,
        "trading_status",
        lambda **kwargs: ok_result(outputs={
            "qlib_latest": "2026-05-28 00:00:00",
            "registry": {"failed": 0},
            "latest_recommendation": rec,
            "pending_recommendations": [rec],
            "latest_execution": {"status": "completed"},
        }),
    )
    monkeypatch.setattr(trading_service, "_active_fxalpha_processes", lambda: {"count": 0, "matches": []})
    monkeypatch.setattr(trading_service, "_quantgpt_health", lambda: {"ok": True, "payload": {"active_tasks": 0}})

    result = trading_service.trading_daily_preflight(topk=10)

    assert result.ok
    assert result.outputs["status"] == "blocked"
    assert "pending_topk_mismatch_expected_10" in result.outputs["blockers"]
    assert result.outputs["pending_topk_mismatches"][0]["topk"] == 50


def test_preflight_blocks_failed_data_production_audit(monkeypatch):
    monkeypatch.setattr(
        "services.data_foundation_service.data_status",
        lambda: ok_result(outputs={"status": "completed", "production_health": {"status": "blocked"}}),
    )
    monkeypatch.setattr("services.model_service.model_production_status", lambda: ok_result(outputs={"status": "ready"}))
    monkeypatch.setattr(trading_service, "pred_update", lambda **kwargs: ok_result(outputs={"target_date": "2026-07-10"}))
    monkeypatch.setattr(
        trading_service,
        "trading_status",
        lambda **kwargs: ok_result(
            outputs={
                "qlib_latest": "2026-07-10 00:00:00",
                "registry": {"failed": 0},
                "latest_recommendation": None,
                "pending_recommendations": [],
                "latest_execution": None,
            }
        ),
    )
    monkeypatch.setattr(trading_service, "_active_fxalpha_processes", lambda: {"count": 0, "matches": []})
    monkeypatch.setattr(trading_service, "_quantgpt_health", lambda: {"ok": True, "payload": {"active_tasks": 0}})

    result = trading_service.trading_daily_preflight(topk=10)

    assert result.ok
    assert result.outputs["status"] == "blocked"
    assert "data_production_audit_failed" in result.outputs["blockers"]


def test_preflight_exposes_production_validation_blocker(monkeypatch):
    monkeypatch.setattr("services.data_foundation_service.data_status", lambda: ok_result(outputs={"status": "completed"}))
    monkeypatch.setattr(
        "services.model_service.model_production_status",
        lambda: ok_result(outputs={
            "status": "production_blocked",
            "production_model": {"model_id": "m-1", "model_run_id": "run-1"},
            "production_validation": {
                "status": "blocked",
                "hard_blocks": ["label_overlap"],
                "warnings": ["pit_data_lineage"],
                "artifact_path": "/tmp/validation_audit.json",
            },
        }),
    )
    monkeypatch.setattr(trading_service, "pred_update", lambda **kwargs: ok_result(outputs={"target_date": "2026-05-28"}))
    monkeypatch.setattr(
        trading_service,
        "trading_status",
        lambda **kwargs: ok_result(outputs={
            "qlib_latest": "2026-05-28 00:00:00",
            "registry": {"failed": 0},
            "latest_recommendation": None,
            "pending_recommendations": [],
            "latest_execution": None,
        }),
    )
    monkeypatch.setattr(trading_service, "_active_fxalpha_processes", lambda: {"count": 0, "matches": []})
    monkeypatch.setattr(trading_service, "_quantgpt_health", lambda: {"ok": True, "payload": {"active_tasks": 0}})

    result = trading_service.trading_daily_preflight(topk=10)

    assert result.ok
    assert result.outputs["status"] == "blocked"
    assert "production_model_not_ready" in result.outputs["blockers"]
    assert "production_validation:label_overlap" in result.outputs["blockers"]
    assert result.outputs["production_validation_summary"] == {
        "status": "blocked",
        "hard_blocks": ["label_overlap"],
        "warnings": ["pit_data_lineage"],
        "artifact_path": "/tmp/validation_audit.json",
        "production_model_id": "m-1",
        "production_model_run_id": "run-1",
    }
