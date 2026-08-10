from pathlib import Path

import pytest

from services._base import ok_result

daily_ops = pytest.importorskip("services.daily_ops_service")


def _data(date="2026-05-27"):
    return ok_result(outputs={"status": "completed", "snapshot": {
        "latest_hdf5_trade_date": date,
        "latest_qlib_trade_date": date,
        "latest_quantgpt_trade_date": date,
    }})


def _model():
    return ok_result(outputs={"status": "ready", "production_model": {
        "model_id": "m-1",
        "model_run_id": "run-1",
    }})


def _blocked_model():
    return ok_result(outputs={
        "status": "production_blocked",
        "production_model": {
            "model_id": "m-1",
            "model_run_id": "run-1",
        },
        "production_validation": {
            "status": "blocked",
            "hard_blocks": ["label_overlap"],
            "warnings": ["pit_data_lineage"],
            "artifact_path": "/tmp/validation_audit.json",
        },
    })


def _pred(date="2026-05-27"):
    return ok_result(outputs={"status": "ready", "qlib_latest": f"{date} 00:00:00"})


def _trade(*, pending=False, date="2026-05-27", topk=10):
    latest_recommendation = None
    pending_rows = []
    if pending:
        latest_recommendation = {
            "recommendation_id": f"rec-{date}",
            "signal_date": date,
            "execution_date": "",
            "status": "pending",
            "topk": topk,
            "warnings": ["execution_date_unresolved: next trading date is not present in qlib calendar yet"],
        }
        pending_rows = [latest_recommendation]
    return ok_result(outputs={
        "status": "ready",
        "qlib_latest": f"{date} 00:00:00",
        "registry": {"pending": len(pending_rows), "failed": 0},
        "latest_recommendation": latest_recommendation,
        "pending_recommendations": pending_rows,
        "latest_execution": {
            "status": "completed",
            "output_files": {"ledger_file": "/tmp/daily_ledger.csv"},
        },
    })


def test_daily_ops_waits_when_latest_pending_has_no_execution_date(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_ops, "LATEST_DAILY_OPS_STATUS_FILE", tmp_path / "latest_status.json")
    monkeypatch.setattr(daily_ops, "data_status", lambda: _data("2026-05-27"))
    monkeypatch.setattr(daily_ops, "model_production_status", _model)
    monkeypatch.setattr(daily_ops, "pred_status", lambda: _pred("2026-05-27"))
    monkeypatch.setattr(daily_ops, "trading_status", lambda: _trade(pending=True, date="2026-05-27"))
    monkeypatch.setattr(daily_ops, "data_daily_routine", lambda **kwargs: ok_result(outputs={"status": "completed"}))
    monkeypatch.setattr(
        daily_ops,
        "trading_daily_preflight",
        lambda **kwargs: ok_result(outputs={"status": "waiting", "waiting_reason": "waiting_for_next_trade_date", "blockers": []}),
    )

    called = {"trade": False}

    def fake_trade_routine(**kwargs):
        called["trade"] = True
        return ok_result(outputs={"status": "completed"})

    monkeypatch.setattr(daily_ops, "trading_daily_routine", fake_trade_routine)

    result = daily_ops.daily_ops_routine()

    assert result.ok
    assert result.outputs["status"] == "waiting"
    assert result.outputs["decision_status"] == "waiting"
    assert result.outputs["blocked_reason"] == "waiting_for_next_trade_date"
    assert result.outputs["trade_action"] == "wait"
    assert not called["trade"]
    assert Path(result.outputs["latest_status_file"]).exists()


def test_daily_ops_runs_data_before_trade_routine(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(daily_ops, "LATEST_DAILY_OPS_STATUS_FILE", tmp_path / "latest_status.json")
    monkeypatch.setattr(daily_ops, "data_status", lambda: _data("2026-05-27"))
    monkeypatch.setattr(daily_ops, "model_production_status", _model)
    monkeypatch.setattr(daily_ops, "pred_status", lambda: _pred("2026-05-27"))
    monkeypatch.setattr(daily_ops, "trading_status", lambda: _trade(pending=False, date="2026-05-27"))

    def fake_data_daily_routine(**kwargs):
        calls.append("data")
        return ok_result(outputs={"status": "completed"})

    def fake_trade_routine(**kwargs):
        calls.append(("trade", kwargs.get("signal_date")))
        return ok_result(outputs={"status": "completed"})

    monkeypatch.setattr(daily_ops, "data_daily_routine", fake_data_daily_routine)
    monkeypatch.setattr(daily_ops, "trading_daily_preflight", lambda **kwargs: ok_result(outputs={"status": "go", "blockers": []}))
    monkeypatch.setattr(daily_ops, "trading_daily_routine", fake_trade_routine)

    result = daily_ops.daily_ops_routine()

    assert result.ok
    assert calls == ["data", ("trade", "2026-05-27")]
    assert result.outputs["status"] == "data_updated_then_completed"


def test_daily_ops_blocks_pending_topk_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_ops, "LATEST_DAILY_OPS_STATUS_FILE", tmp_path / "latest_status.json")
    monkeypatch.setattr(daily_ops, "data_status", lambda: _data("2026-05-28"))
    monkeypatch.setattr(daily_ops, "model_production_status", _model)
    monkeypatch.setattr(daily_ops, "pred_status", lambda: _pred("2026-05-28"))
    monkeypatch.setattr(daily_ops, "trading_status", lambda: _trade(pending=True, date="2026-05-27", topk=50))
    monkeypatch.setattr(daily_ops, "data_daily_routine", lambda **kwargs: ok_result(outputs={"status": "completed"}))
    monkeypatch.setattr(
        daily_ops,
        "trading_daily_preflight",
        lambda **kwargs: ok_result(outputs={"status": "blocked", "blockers": ["pending_topk_mismatch_expected_10"]}),
    )

    result = daily_ops.daily_ops_routine(topk=10)

    assert not result.ok
    assert result.outputs["status"] == "blocked"
    assert "pending_topk_mismatch_expected_10" in result.outputs["blocked_reason"]


def test_daily_ops_dry_run_includes_trade_preflight(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_ops, "LATEST_DAILY_OPS_STATUS_FILE", tmp_path / "latest_status.json")
    monkeypatch.setattr(daily_ops, "data_status", lambda: _data("2026-05-28"))
    monkeypatch.setattr(daily_ops, "model_production_status", _model)
    monkeypatch.setattr(daily_ops, "pred_status", lambda: _pred("2026-05-28"))
    monkeypatch.setattr(daily_ops, "trading_status", lambda: _trade(pending=False, date="2026-05-28"))
    monkeypatch.setattr(daily_ops, "data_daily_routine", lambda **kwargs: ok_result(outputs={"status": "dry_run"}))
    monkeypatch.setattr(daily_ops, "trading_daily_preflight", lambda **kwargs: ok_result(outputs={"status": "go", "blockers": []}))

    called = {"trade": False}

    def fake_trade_routine(**kwargs):
        called["trade"] = True
        return ok_result(outputs={"status": "completed"})

    monkeypatch.setattr(daily_ops, "trading_daily_routine", fake_trade_routine)

    result = daily_ops.daily_ops_routine(dry_run=True)

    assert result.ok
    assert result.outputs["status"] == "dry_run"
    assert result.outputs["decision_status"] == "go"
    assert result.outputs["trade_action"] == "not_run_dry_run"
    assert result.outputs["trade_preflight"]["outputs"]["status"] == "go"
    assert not called["trade"]


def test_daily_ops_status_exposes_production_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_ops, "LATEST_DAILY_OPS_STATUS_FILE", tmp_path / "latest_status.json")
    monkeypatch.setattr(daily_ops, "data_status", lambda: _data("2026-05-28"))
    monkeypatch.setattr(daily_ops, "model_production_status", _blocked_model)
    monkeypatch.setattr(daily_ops, "_prediction_snapshot_status", lambda: _pred("2026-05-28"))
    monkeypatch.setattr(daily_ops, "trading_status", lambda prediction=None: _trade(pending=False, date="2026-05-28"))

    result = daily_ops.daily_ops_status()

    assert result.ok
    summary = result.outputs["summary"]
    assert summary["production_validation_summary"]["hard_blocks"] == ["label_overlap"]
    assert "production_validation:label_overlap" in result.warnings


def test_daily_ops_status_uses_light_prediction_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_ops, "LATEST_DAILY_OPS_STATUS_FILE", tmp_path / "latest_status.json")
    monkeypatch.setattr(daily_ops, "data_status", lambda: _data("2026-05-28"))
    monkeypatch.setattr(daily_ops, "model_production_status", _model)

    def fail_heavy_pred_status():
        raise AssertionError("daily_ops_status must not call heavy pred_status")

    monkeypatch.setattr(daily_ops, "pred_status", fail_heavy_pred_status)
    monkeypatch.setattr(daily_ops, "_prediction_snapshot_status", lambda: _pred("2026-05-28"))
    monkeypatch.setattr(daily_ops, "trading_status", lambda prediction=None: _trade(pending=False, date="2026-05-28"))

    result = daily_ops.daily_ops_status()

    assert result.ok
    assert result.outputs["summary"]["prediction_status"] == "ready"
