from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

from services import prediction_service


def _clear_status_cache() -> None:
    with prediction_service._PRED_STATUS_CACHE_LOCK:
        prediction_service._PRED_STATUS_CACHE.clear()


def test_prediction_status_snapshot_does_not_initialize_qlib(monkeypatch, tmp_path):
    status_file = tmp_path / "latest_status.json"
    status_file.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-06T07:00:00",
                "inputs": {"model_run_id": "run-1"},
                "outputs": {"status": "completed", "qlib_latest": "2026-08-05"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(prediction_service, "LATEST_PREDICTION_STATUS_FILE", status_file)
    monkeypatch.setattr(
        prediction_service,
        "init_qlib",
        lambda: (_ for _ in ()).throw(AssertionError("snapshot status must not initialize Qlib")),
    )

    result = prediction_service.pred_status_snapshot()

    assert result.ok
    assert result.outputs["status"] == "ready"
    assert result.outputs["raw_status"] == "completed"
    assert result.outputs["source"] == "latest_prediction_status_file"
    assert result.outputs["generated_at"] == "2026-08-06T07:00:00"


def test_prediction_status_serializes_concurrent_cache_misses(monkeypatch):
    _clear_status_cache()
    calls = {"validation": 0}
    model_context = {
        "model_id": "model-1",
        "model_run_id": "run-1",
        "feature_set_id": "features-1",
        "status": "production",
        "source": "test",
        "recorder_run_dir": "/tmp/run-1",
    }
    monkeypatch.setattr(prediction_service, "resolve_prediction_model_context", lambda **_kwargs: dict(model_context))
    monkeypatch.setattr(prediction_service, "init_qlib", lambda: None)
    monkeypatch.setattr(prediction_service, "get_qlib_latest_calendar_date", lambda: "2026-08-05")
    monkeypatch.setattr(
        prediction_service,
        "ensure_factor_freshness",
        lambda *_args, **_kwargs: {"status": "fresh"},
    )

    def fake_validate(*_args, **_kwargs):
        calls["validation"] += 1
        time.sleep(0.05)
        return {"required_artifacts_ok": True}

    monkeypatch.setattr(prediction_service, "validate_pred_inputs", fake_validate)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: prediction_service.pred_status(), range(2)))

    assert all(result.ok for result in results)
    assert calls["validation"] == 1
    _clear_status_cache()
