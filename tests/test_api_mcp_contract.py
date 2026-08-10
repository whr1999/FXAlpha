from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import api_server
from mcp_servers import model_server, platform_server
from services._base import ok_result
from storage.paths import FACTOR_VALUE_DEFAULT_END_DATE


def _post_json(path: str, payload: dict) -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_server.APIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get_json_twice(path: str) -> tuple[dict, dict, str | None]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_server.APIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}{path}"
        with urllib.request.urlopen(url, timeout=5) as first_response:
            first = json.loads(first_response.read().decode("utf-8"))
        with urllib.request.urlopen(url, timeout=5) as second_response:
            second = json.loads(second_response.read().decode("utf-8"))
            cache_state = second_response.headers.get("X-FXAlpha-Cache")
        return first, second, cache_state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get_json(path: str) -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_server.APIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_platform_and_model_mcp_dump_return_structured_service_result():
    result = ok_result(inputs={"input": 1}, outputs={"value": 2})

    for payload in (platform_server._dump(result), model_server._dump(result)):
        assert isinstance(payload, dict)
        assert set(payload) == {
            "ok",
            "err",
            "inputs",
            "outputs",
            "artifacts",
            "warnings",
            "generated_at",
        }


def test_model_confirmation_error_uses_service_result_contract():
    payload = model_server.fxalpha_model_confirm_research_round("")

    assert isinstance(payload, dict)
    assert payload["ok"] is False
    assert payload["err"] == "round_group_not_found"
    assert "error" not in payload


def test_pipeline_api_default_end_date_comes_from_config(monkeypatch):
    captured: dict = {}

    def fake_pipeline_run(**kwargs):
        captured.update(kwargs)
        return ok_result(outputs={"status": "captured"})

    monkeypatch.setattr(api_server, "pipeline_run", fake_pipeline_run)

    payload = _post_json("/pipeline/run", {})

    assert payload["ok"] is True
    assert captured["end_date"] == FACTOR_VALUE_DEFAULT_END_DATE


def test_status_get_uses_short_lived_read_only_response_cache(monkeypatch):
    calls = {"count": 0}

    def fake_data_status():
        calls["count"] += 1
        return ok_result(outputs={"status": "ready", "call": calls["count"]})

    monkeypatch.setattr(api_server, "data_status", fake_data_status)
    with api_server._GET_CACHE_LOCK:
        api_server._GET_RESPONSE_CACHE.clear()

    first, second, cache_state = _get_json_twice("/data/status")

    assert first == second
    assert calls["count"] == 1
    assert cache_state == "HIT"


def test_maintenance_status_api_is_snapshot_first_and_deep_is_explicit(monkeypatch):
    calls: list[bool] = []

    def fake_maintenance_status(*, include_disk_audit=True):
        calls.append(bool(include_disk_audit))
        return ok_result(
            outputs={
                "status": "ready",
                "disk_audit_mode": "deep" if include_disk_audit else "snapshot_only",
            }
        )

    monkeypatch.setattr(api_server, "maintenance_status", fake_maintenance_status)
    with api_server._GET_CACHE_LOCK:
        api_server._GET_RESPONSE_CACHE.clear()

    snapshot = _get_json("/maintenance/status")
    deep = _get_json("/maintenance/status?deep=true")

    assert snapshot["outputs"]["disk_audit_mode"] == "snapshot_only"
    assert deep["outputs"]["disk_audit_mode"] == "deep"
    assert calls == [False, True]


def test_data_daily_preflight_runs_in_isolated_configured_python(monkeypatch):
    expected = ok_result(outputs={"status": "go", "target_date": "2026-08-07"}).to_dict()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps(expected), stderr="")

    monkeypatch.setattr(api_server.subprocess, "run", fake_run)
    monkeypatch.setattr(api_server.time, "monotonic", lambda: 100.0)
    with api_server._DATA_PREFLIGHT_LOCK:
        api_server._DATA_PREFLIGHT_CACHE.clear()

    payload, status = api_server._isolated_data_daily_preflight("2026-08-07")

    assert status == 200
    assert payload == expected
    assert captured["command"][0] == api_server.sys.executable
    assert captured["command"][-2:] == ["--target-date", "2026-08-07"]
    assert captured["kwargs"]["cwd"] == api_server.PROJECT_ROOT


def test_data_daily_preflight_subprocess_failure_is_structured(monkeypatch):
    monkeypatch.setattr(
        api_server.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="not-json", stderr="boom"),
    )
    with api_server._DATA_PREFLIGHT_LOCK:
        api_server._DATA_PREFLIGHT_CACHE.clear()

    payload, status = api_server._isolated_data_daily_preflight("auto")

    assert status == 500
    assert payload["ok"] is False
    assert payload["err"] == "data_daily_preflight_invalid_output"
    assert payload["outputs"]["stderr_tail"] == "boom"


def test_prediction_and_trading_status_routes_use_light_snapshot(monkeypatch):
    snapshot = ok_result(outputs={"status": "ready", "source": "latest_prediction_status_file"})
    captured: dict = {}

    def fail_heavy_pred_status(*_args, **_kwargs):
        raise AssertionError("GUI status routes must not call heavy pred_status")

    def fake_trading_status(**kwargs):
        captured.update(kwargs)
        return ok_result(outputs={"status": "ready", "prediction": kwargs["prediction"].to_dict()})

    monkeypatch.setattr(api_server, "pred_status", fail_heavy_pred_status)
    monkeypatch.setattr(api_server, "pred_status_snapshot", lambda: snapshot)
    monkeypatch.setattr(api_server, "trading_status", fake_trading_status)
    with api_server._GET_CACHE_LOCK:
        api_server._GET_RESPONSE_CACHE.clear()

    prediction_payload = _get_json("/pred/status")
    trading_payload = _get_json("/trade/status")

    assert prediction_payload["outputs"]["source"] == "latest_prediction_status_file"
    assert captured["prediction"] is snapshot
    assert trading_payload["outputs"]["prediction"]["outputs"]["status"] == "ready"


def test_factor_orchestrator_control_api_routes_are_distinct(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        api_server,
        "factor_research_control_state",
        lambda: ok_result(outputs={"state": "paused", "allowed_actions": ["resume", "stop"]}),
    )
    monkeypatch.setattr(
        api_server,
        "factor_research_pause",
        lambda run_id=None, reason="": calls.append(("pause", str(run_id))) or ok_result(outputs={"actual_state": "pause_requested"}),
    )
    monkeypatch.setattr(
        api_server,
        "factor_research_resume",
        lambda run_id: calls.append(("resume", str(run_id))) or ok_result(outputs={"actual_state": "running"}),
    )
    monkeypatch.setattr(
        api_server,
        "factor_research_stop",
        lambda run_id=None, reason="": calls.append(("stop", str(run_id))) or ok_result(outputs={"actual_state": "stop_requested"}),
    )

    assert _get_json("/factor/research/control")["outputs"]["state"] == "paused"
    assert _post_json("/factor/research/pause", {"run_id": "run-1"})["outputs"]["actual_state"] == "pause_requested"
    assert _post_json("/factor/research/resume", {"run_id": "run-1"})["outputs"]["actual_state"] == "running"
    assert _post_json("/factor/research/stop", {"run_id": "run-1"})["outputs"]["actual_state"] == "stop_requested"
    assert calls == [("pause", "run-1"), ("resume", "run-1"), ("stop", "run-1")]




def test_factor_map_api_and_platform_mcp_share_service_contract(monkeypatch):
    def fake_factor_map_status(*, region_uid=""):
        return ok_result(outputs={"map_id": "fm-test", "region_uid": region_uid, "status": "fresh"})

    monkeypatch.setattr(api_server, "factor_map_status", fake_factor_map_status)
    monkeypatch.setattr(platform_server, "factor_map_status", fake_factor_map_status)

    api_payload = _get_json("/factor/map?region_uid=region-one")
    mcp_payload = platform_server.fxalpha_factor_map_status("region-one")

    assert api_payload["outputs"] == mcp_payload["outputs"]
    assert api_payload["outputs"]["region_uid"] == "region-one"


def test_factor_map_refresh_reuses_information_audit_queue(monkeypatch):
    captured = {}

    def fake_enqueue(**kwargs):
        captured.update(kwargs)
        return ok_result(outputs={"status": "queued"})

    monkeypatch.setattr(api_server, "enqueue_factor_library_audit", fake_enqueue)

    payload = _post_json("/factor/map/refresh", {})

    assert payload["ok"] is True
    assert captured == {
        "scope": "information",
        "status_filter": "active",
        "save_report": True,
        "include_feature_sets": True,
    }
