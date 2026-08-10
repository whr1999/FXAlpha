from __future__ import annotations

from domain.model.state_store import ModelStateStore


def test_state_store_round_and_seed_roundtrip(tmp_path):
    state = ModelStateStore(runtime_root=tmp_path)
    state.upsert_round(
        {
            "round_group_id": "mr0703_test",
            "feature_set_id": "fs-test",
            "experiment_signature": "sig",
            "seed_set": [42, 17, 83],
            "seed_policy": {"mode": "fixed_three_parallel_seeds"},
            "experiment": {"a": 1},
            "status": "queued",
            "stage": "experiment_plan",
        }
    )
    state.upsert_seed_run(
        {
            "model_run_id": "m0703_test_s42",
            "round_group_id": "mr0703_test",
            "seed": 42,
            "status": "completed",
            "metrics": {"annualized_ret": 0.1},
            "score": {"sota_score": 60},
        }
    )

    round_payload = state.get_round("mr0703_test")
    seed_run = state.get_seed_run("m0703_test_s42")
    assert round_payload["seed_set"] == [42, 17, 83]
    assert seed_run["metrics"]["annualized_ret"] == 0.1
    assert state.list_rounds()[0]["seed_runs"][0]["seed"] == 42


def test_managed_job_claim_is_global_and_payload_survives_stage_updates(tmp_path):
    state = ModelStateStore(runtime_root=tmp_path)

    claimed, first = state.claim_managed_job("job-1", mode="orch", stage="queued", payload={"session_id": "session-1"})
    second_claimed, active = state.claim_managed_job("job-2", mode="orch", stage="queued")

    assert claimed is True
    assert first["payload"]["async_job"] is True
    assert second_claimed is False
    assert active["job_id"] == "job-1"

    updated = state.upsert_job("job-1", status="running", stage="train", mode="orch", payload={"worker_pid": 123})
    assert updated["payload"]["session_id"] == "session-1"
    assert updated["payload"]["worker_pid"] == 123

    stopping = state.request_job_stop("job-1")
    assert stopping["status"] == "stopping"
    assert state.job_stop_requested("job-1") is True
