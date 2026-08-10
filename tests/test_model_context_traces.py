from __future__ import annotations

import json

from domain.model.context import build_context_pack, record_mcp_context, record_orch_trace
from domain.model.orchestrator import model_mcp_prompt, model_system_prompt
from domain.model.state_store import ModelStateStore, append_jsonl
from services import model_service as svc


def test_context_traces_include_payload_and_blocked_actions(tmp_path, monkeypatch):
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")
    monkeypatch.setattr("domain.model.context.MODEL_MCP_TRACES", tmp_path / "mcp.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "orch.jsonl")

    context_pack = build_context_pack(stage="experiment_plan")
    record_mcp_context("experiment_plan", context_pack, expected_action="submit_experiment")
    record_orch_trace(
        "experiment_plan",
        system_prompt="sys",
        stage_briefing="brief",
        context_pack=context_pack,
        output_contract={"type": "experiment"},
    )

    mcp_line = json.loads((tmp_path / "mcp.jsonl").read_text(encoding="utf-8").splitlines()[0])
    orch_line = json.loads((tmp_path / "orch.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert mcp_line["mode"] == "mcp"
    assert mcp_line["context_id"]
    assert set(mcp_line["context_pack"]) == {"context_version", "stage", "research_evidence", "correction"}
    assert set(mcp_line["context_pack"]["research_evidence"]) == {"recent_rounds", "parameter_ledger", "cross_feature_references"}
    assert orch_line["mode"] == "orch"
    assert orch_line["context_id"]
    assert orch_line["context_pack"]["stage"] == "experiment_plan"


def test_context_pack_records_explicit_feature_set_before_round_exists():
    context_pack = build_context_pack(stage="experiment_plan", selected_feature_set_id="fs-selected")

    assert context_pack["stage"] == "experiment_plan"
    assert "feature_set_id" not in json.dumps(context_pack)
    assert set(context_pack["research_evidence"]) == {"recent_rounds", "parameter_ledger", "cross_feature_references"}


def test_model_prompt_names_seed_and_gate_contracts():
    prompt = model_system_prompt()

    assert "Research Planner System Prompt" in prompt
    assert "research_goal" in prompt
    assert "operator_guidance" in prompt
    assert "Flash" in prompt
    assert "learning_rate" in prompt
    assert "early_stopping_rounds" in prompt
    assert "tuning_state.best_round_group_id" in prompt
    assert "completed_rounds" in prompt
    assert "training_diagnostics" in prompt
    assert "next_move" in prompt
    assert "evidence_interpretation" in prompt
    assert "parameter_changes" in prompt
    assert "不负责决定是否进入生产 Rolling" in prompt
    assert "不要输出停止决定" in prompt
    assert "不要新增字段" in prompt
    assert "完整参数" in prompt
    assert '"stop_reason"' not in prompt
    assert '"decision": "checkpoint_stop"' not in prompt


def test_model_mcp_prompt_is_operational_not_research_planner():
    prompt = model_mcp_prompt()

    assert "MCP Operating Prompt" in prompt
    assert "research" in prompt
    assert "production" in prompt
    assert "confirm_research_round" in prompt
    assert "start_production_rolling" in prompt
    assert "Top20/Drop2/Hold5" in prompt


def test_context_pack_compacts_llm_evidence_window(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.context.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    state.upsert_round(
        {
            "round_group_id": "mr-test",
            "feature_set_id": "fs-test",
            "experiment_signature": "sig",
            "seed_set": [42, 17, 83],
            "seed_policy": {"mode": "fixed_three_parallel_seeds"},
            "experiment": {
                "baseline_kind": "baseline",
                "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm",
                "qlib_model_kwargs": {"learning_rate": 0.2, "num_leaves": 210},
            },
            "status": "completed",
            "stage": "round_synthesis",
        }
    )
    state.upsert_seed_run(
        {
            "model_run_id": "m-test-s42",
            "round_group_id": "mr-test",
            "seed": 42,
            "status": "completed",
            "metrics": {"excess_annualized_ret_with_cost": 0.12, "excess_information_ratio_with_cost": 0.8},
            "score": {"sota_score": 61.23456, "decision": "send_to_sota_gate", "score_review_version": "v"},
            "gate": {"gate_status": "pass", "asset_status": "candidate"},
            "artifact_dir": "/tmp/artifact",
        }
    )
    append_jsonl(
        tmp_path / "research.jsonl",
        {
            "stage": "round_synthesis",
            "job_id": "job-old",
            "round_no": 1,
            "round_group_id": "mr-old",
            "feature_set_id": "fs-old",
            "decision": "continue",
            "next": "experiment_plan",
            "summary": "old summary",
            "evidence_refs": ["old"],
            "extra": {"llm_usage": {"prompt_tokens": 999999}, "next_experiment_guidance": "old guidance"},
        },
    )
    append_jsonl(
        tmp_path / "research.jsonl",
        {
            "stage": "round_synthesis",
            "job_id": "job-new",
            "round_no": 2,
            "round_group_id": "mr-test",
            "feature_set_id": "fs-test",
            "decision": "continue",
            "next": "experiment_plan",
            "summary": "new summary",
            "evidence_refs": ["new"],
            "extra": {"llm_usage": {"prompt_tokens": 999999}, "next_experiment_guidance": "new guidance"},
        },
    )

    context_pack = build_context_pack(stage="experiment_plan", round_group_id="mr-test", state=state)
    encoded = json.dumps(context_pack, ensure_ascii=False)
    evidence = context_pack["research_evidence"]

    assert "llm_usage" not in encoded
    assert len(evidence["recent_rounds"]) == 1
    assert evidence["recent_rounds"][0]["recency"] == "latest"
    assert evidence["recent_rounds"][0]["lesson"]["next_experiment_guidance"] == "new guidance"
    assert evidence["recent_rounds"][0]["seed42_result"]["research_score"] == 61.235
    assert "seed_results" not in evidence["recent_rounds"][0]


def test_service_traces_are_latest_first_filterable_and_compact(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr(svc, "MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")
    context_pack = build_context_pack(stage="experiment_plan")
    record_orch_trace(
        "experiment_plan",
        system_prompt="sys",
        stage_briefing="brief",
        context_pack=context_pack,
        job_id="job-old",
        output_contract={"llm_payload": {"large": True}},
    )
    record_orch_trace(
        "experiment_plan",
        system_prompt="sys",
        stage_briefing="brief",
        context_pack=context_pack,
        job_id="job-new",
        parsed_response={"decision": "submit_experiment", "experiment_json": {"large": True}, "planner_mode": "deepseek"},
    )

    all_traces = svc.model_orchestrator_traces(limit=5, include_payload=False)
    filtered = svc.model_orchestrator_traces(limit=5, include_payload=False, job_id="job-new")

    assert all_traces.ok is True
    assert all_traces.outputs["traces"][0]["job_id"] == "job-new"
    assert filtered.outputs["traces"][0]["job_id"] == "job-new"
    assert filtered.outputs["traces"][0]["parsed_response"]["experiment_json"] == {"omitted": True}


def test_service_marks_old_round_synthesis_schema_as_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")
    context_pack = build_context_pack(stage="round_synthesis")
    record_orch_trace(
        "round_synthesis",
        system_prompt="sys",
        stage_briefing="brief",
        context_pack=context_pack,
        job_id="job-old",
        parsed_response={
            "stage": "round_synthesis",
            "decision": "continue",
            "next": "experiment_plan",
            "summary": "old schema",
            "score_summary": [],
            "gate_summary": [],
            "validation_summary": {},
        },
    )

    traces = svc.model_orchestrator_traces(limit=5, include_payload=False)

    assert traces.ok is True
    row = traces.outputs["traces"][0]
    assert row["legacy_trace"] is True
    assert row["schema_status"] == "legacy"
    assert any(reason.startswith("legacy_round_synthesis_contract_missing") for reason in row["legacy_reasons"])


def test_mcp_traces_are_job_filterable(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "MODEL_MCP_TRACES", tmp_path / "mcp.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_MCP_TRACES", tmp_path / "mcp.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = svc.model_tool_context(stage="context_review", job_id="mcp-job-1")
    filtered = svc.model_mcp_traces(limit=5, include_payload=False, job_id="mcp-job-1")

    assert result.ok is True
    assert filtered.ok is True
    assert len(filtered.outputs["traces"]) == 1
    assert filtered.outputs["traces"][0]["job_id"] == "mcp-job-1"


def test_mcp_context_tool_accepts_explicit_feature_set(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "MODEL_MCP_TRACES", tmp_path / "mcp.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_MCP_TRACES", tmp_path / "mcp.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = svc.model_tool_context(stage="experiment_plan", feature_set_id="fs-mcp-selected", job_id="mcp-job-2")

    assert result.ok is True
    context_pack = result.outputs["context_pack"]
    assert context_pack["stage"] == "experiment_plan"
    assert "feature_set_id" not in json.dumps(context_pack)



def test_current_context_uses_latest_real_trace_context_id(tmp_path, monkeypatch):
    from domain.model.state_store import ModelStateStore

    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr(svc, "ModelStateStore", lambda: state)
    monkeypatch.setattr(svc, "MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr(svc, "MODEL_MCP_TRACES", tmp_path / "mcp.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")
    state.upsert_job("ctx-job", status="completed", stage="experiment_plan", mode="orch")
    context_pack = build_context_pack(stage="experiment_plan", state=state)
    trace = record_orch_trace(
        "experiment_plan",
        system_prompt="sys",
        stage_briefing="brief",
        context_pack=context_pack,
        job_id="ctx-job",
    )

    first = svc.model_current_context(job_id="ctx-job")
    second = svc.model_current_context(job_id="ctx-job")

    assert first.ok is True
    assert first.outputs["current_context_summary"]["context_id"] == trace["context_id"]
    assert second.outputs["current_context_summary"]["context_id"] == trace["context_id"]
    assert first.outputs["current_context_summary"]["generated_preview"] is False
