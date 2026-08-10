from __future__ import annotations

import json
from datetime import datetime, timedelta

from domain.model.production_refit import _dynamic_refit_segments
from domain.model.state_store import ModelStateStore
from services import model_service
from storage.model_registry import ModelRegistry


def test_orchestrator_start_returns_immediately_and_rejects_second_job(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path / "state")

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(model_service, "ModelStateStore", lambda: state)
    monkeypatch.setattr(model_service, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(model_service.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    first = model_service.model_orchestrator_start(feature_set_id="fs-test", run_id="job-a")
    second = model_service.model_orchestrator_start(feature_set_id="fs-test", run_id="job-b")

    assert first.ok is True
    assert first.outputs["status"] == "accepted"
    assert first.outputs["job"]["payload"]["worker_pid"] == 4321
    assert second.ok is True
    assert second.outputs["status"] == "already_running"
    assert second.outputs["active_job"]["job_id"] == "job-a"


def test_orchestrator_start_persists_research_baseline_overrides_in_worker_command(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path / "state")
    captured = {}

    class FakeProcess:
        pid = 4322

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(model_service, "ModelStateStore", lambda: state)
    monkeypatch.setattr(model_service, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(model_service.subprocess, "Popen", fake_popen)

    result = model_service.model_orchestrator_start(
        feature_set_id="fs-custom",
        run_id="job-custom",
        baseline_model_params={"learning_rate": 0.03, "num_leaves": 64},
    )

    assert result.ok is True
    command = captured["command"]
    params = json.loads(command[command.index("--baseline-model-params-json") + 1])
    assert params == {"learning_rate": 0.03, "lr": 0.03, "num_leaves": 64}
    assert result.inputs["baseline_model_params"] == params


def test_production_orchestrator_rejects_research_baseline_overrides():
    result = model_service.model_orchestrator_start(
        evaluation_mode="production",
        source_round_group_id="round-confirmed",
        baseline_model_params={"learning_rate": 0.03},
    )

    assert result.ok is False
    assert result.err == "baseline_model_params_research_only"


def test_dynamic_refit_segments_follow_latest_snapshot_trading_day(tmp_path, monkeypatch):
    calendar_path = tmp_path / "day.txt"
    start = datetime(2024, 1, 1)
    dates = [start + timedelta(days=index) for index in range(1000)]
    weekdays = [value for value in dates if value.weekday() < 5]
    calendar_path.write_text("\n".join(value.strftime("%Y-%m-%d") for value in weekdays), encoding="utf-8")
    latest = weekdays[-10].strftime("%Y-%m-%d")
    monkeypatch.setattr("domain.model.production_refit.QLIB_CALENDAR_FILE", calendar_path)
    monkeypatch.setattr("domain.model.production_refit.load_feature_set_manifest", lambda feature_set_id: {"actual_end_date": latest})

    segments = _dynamic_refit_segments(
        "fs-dynamic",
        {
            "segments": {
                "train": ["2023-01-03", "2025-12-31"],
                "valid": ["2026-01-02", "2026-06-30"],
                "test": ["2026-01-02", "2026-07-01"],
            }
        },
    )

    assert segments["valid"][1] == latest
    assert segments["test"] == segments["valid"]
    assert segments["train"][1] < segments["valid"][0]


def test_registry_update_fills_existing_lineage_columns(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.db")
    model_id = registry.register(model_run_id="lineage-run", status="research")

    registry.update_run_result(
        model_run_id="lineage-run",
        feature_set_id="fs-lineage",
        feature_set_fingerprint="fp-lineage",
        factor_ids=["f1", "f2"],
        feature_count=2,
        train_start="2022-01-01",
        train_end="2024-12-31",
    )

    row = registry.get(model_id)
    assert row["feature_set_id"] == "fs-lineage"
    assert row["feature_set_fingerprint"] == "fp-lineage"
    assert row["factor_count"] == 2
    assert row["feature_count"] == 2
    assert row["train_start"] == "2022-01-01"
    assert row["train_end"] == "2024-12-31"
