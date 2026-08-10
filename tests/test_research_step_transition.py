from __future__ import annotations

from services import factor_research_service as svc


def _redirect_research_steps(monkeypatch, tmp_path):
    steps_dir = tmp_path / "research_steps"
    steps_file = steps_dir / "current.jsonl"
    history_dir = steps_dir / "history"
    monkeypatch.setattr(svc, "FACTOR_RESEARCH_STEPS_DIR", steps_dir)
    monkeypatch.setattr(svc, "FACTOR_RESEARCH_STEPS_FILE", steps_file)
    monkeypatch.setattr(svc, "FACTOR_RESEARCH_STEPS_HISTORY_DIR", history_dir)
    return steps_file


def test_record_research_step_preserves_new_stage_and_transition(monkeypatch, tmp_path):
    _redirect_research_steps(monkeypatch, tmp_path)

    result = svc.factor_tool_record_research_step(
        stage="novelty_review",
        summary="Novelty checked one keeper.",
        decision="Continue to deep validation.",
        next_action="Run deep validation.",
        run_id="run-test",
        round_id="run-test:r0001",
        stage_seq=2,
        previous_stage="score_review",
        previous_stage_id="run-test:r0001:s01_score_review",
        stage_transition={
            "facts": "candidate novelty was clean",
            "judgment": "candidate merits deep validation",
            "next_stage": "deep_validation_review",
            "next_action": "run_backtest and robustness checks",
            "research_strategy": "normal process flow to deep validation",
            "why": "novelty is clean and quick score is strong",
            "history_used": "recent crowded lanes were avoided",
        },
    )

    assert result.ok
    recorded = result.outputs["recorded"]
    assert recorded["schema_version"] == "research_step_v2"
    assert recorded["run_id"] == "run-test"
    assert recorded["round_id"] == "run-test:r0001"
    assert recorded["stage_seq"] == 2
    assert recorded["stage_id"] == "run-test:r0001:s02_novelty_review"
    assert recorded["previous_stage"] == "score_review"
    assert recorded["previous_stage_id"] == "run-test:r0001:s01_score_review"
    assert recorded["stage"] == "novelty_review"
    assert "next" not in recorded
    transition = recorded["stage_transition"]
    assert transition["research_strategy"] == "normal process flow to deep validation"
    assert "stage_transition" not in recorded.get("extra", {})
    assert result.artifacts["research_steps_history_dir"].endswith("history")


def test_record_research_step_rejects_missing_required_transition(monkeypatch, tmp_path):
    _redirect_research_steps(monkeypatch, tmp_path)

    result = svc.factor_tool_record_research_step(
        stage="score_review",
        summary="Legacy score review.",
        decision="Continue.",
        next_action="Run novelty.",
        extra={"quick_screened": 3},
    )

    assert not result.ok
    assert "research_step_v2 decision-stage schema violation" in result.err
    assert "stage_transition.next_stage" in result.inputs["missing_fields"]


def test_record_research_step_rejects_forbidden_control_fields_in_extra(monkeypatch, tmp_path):
    _redirect_research_steps(monkeypatch, tmp_path)

    result = svc.factor_tool_record_research_step(
        stage="score_review",
        summary="Legacy client sent transition in extra.",
        decision="Continue.",
        next_action="Run novelty.",
        extra={
            "quick_screened": 3,
            "stage_transition": {
                "facts": "two candidates",
                "judgment": "continue",
                "next_stage": "novelty_review",
                "next_action": "run novelty",
                "research_strategy": "normal process flow",
                "why": "score passed",
                "history_used": "recent steps",
            },
        },
    )

    assert not result.ok
    assert "extra contains forbidden control fields" in result.err
    assert "stage_transition" in result.inputs["forbidden_extra_keys"]


def test_record_research_step_cleans_deprecated_extra_and_writes_history(monkeypatch, tmp_path):
    steps_file = _redirect_research_steps(monkeypatch, tmp_path)

    result = svc.factor_tool_record_research_step(
        stage="score_review",
        summary="Scored batch.",
        decision="Keep one candidate.",
        run_id="run-test",
        round_id="run-test:r0002",
        stage_transition={
            "facts": "best score was 82; raw metrics remain in task store",
            "judgment": "candidate is worth novelty",
            "next_stage": "novelty_review",
            "next_action": "run novelty",
            "research_strategy": "normal process flow",
            "why": "quick score passed",
            "history_used": "used prior crowded-family note",
        },
        evidence_refs=[{"tool": "score_factor", "task_id": "task-1", "note": "raw metrics in task store"}],
        tags=["quick_screen"],
        extra={"metrics": {"ic": 0.1}, "score_summary": {"score": 82}, "automation_id": "a1"},
    )

    assert result.ok
    recorded = result.outputs["recorded"]
    assert recorded["evidence_refs"][0]["task_id"] == "task-1"
    assert recorded["tags"] == ["quick_screen"]
    assert "metrics" not in recorded.get("extra", {})
    assert "score_summary" not in recorded.get("extra", {})
    assert recorded["extra"]["automation_id"] == "a1"
    assert set(recorded["extra_removed_keys"]) == {"metrics", "score_summary"}
    assert steps_file.exists()
    history_files = list((tmp_path / "research_steps" / "history").glob("*.jsonl"))
    assert history_files
    assert "research_step_v2" in history_files[0].read_text(encoding="utf-8")


def test_record_research_step_keeps_required_schema_fields_present(monkeypatch, tmp_path):
    _redirect_research_steps(monkeypatch, tmp_path)

    result = svc.factor_tool_record_research_step(
        stage="protocol_load",
        summary="Loaded protocol and context.",
        decision="Prepare the first thesis batch.",
        run_id="run-test",
        round_id="run-test:r0001",
        stage_seq=1,
        previous_stage="",
        previous_stage_id="",
        stage_transition={
            "next_stage": "pre_batch_decision",
            "next_action": "design first candidate batch",
            "research_strategy": "",
            "facts": "",
            "judgment": "",
            "why": "",
            "history_used": "",
        },
    )

    assert result.ok
    recorded = result.outputs["recorded"]
    assert recorded["previous_stage"] == ""
    assert recorded["previous_stage_id"] == ""
    assert recorded["evidence_refs"] == []
    assert recorded["tags"] == []
    assert recorded["stage_transition"] == {
        "next_stage": "pre_batch_decision",
        "next_action": "design first candidate batch",
        "research_strategy": "",
        "facts": "",
        "judgment": "",
        "why": "",
        "history_used": "",
        "reason": "",
    }


def test_record_research_step_persists_candidate_lanes_for_mcp_gui_projection(monkeypatch, tmp_path):
    _redirect_research_steps(monkeypatch, tmp_path)

    result = svc.factor_tool_record_research_step(
        stage="score_review",
        summary="One candidate passed quick score.",
        decision="Run novelty.",
        run_id="run-test",
        round_id="run-test:r0001",
        stage_transition={
            "facts": "c1 scored 72.8",
            "judgment": "c1 merits novelty review",
            "next_stage": "novelty_review",
            "next_action": "fxalpha_novelty_check",
            "research_strategy": "normal validation flow",
            "why": "quick score passed",
            "history_used": "none",
        },
        candidate_lanes=[
            {
                "candidate_id": "c1",
                "expression": "rank(close)",
                "quick_score": 72.8,
                "grade": "B",
                "quality_decision": "deep_validate",
            }
        ],
        candidate_decisions=[{"candidate_id": "c1", "action": "advance_to_novelty", "reason": "quick_grade_b"}],
    )

    assert result.ok
    recorded = result.outputs["recorded"]
    assert recorded["candidate_lanes"][0]["candidate_id"] == "c1"
    assert recorded["candidate_lanes"][0]["quick_score"] == 72.8
    assert recorded["candidate_decisions"][0]["action"] == "advance_to_novelty"


def test_stage_guard_blocks_when_transition_missing(monkeypatch, tmp_path):
    _redirect_research_steps(monkeypatch, tmp_path)

    result = svc._stage_guard_result(
        "fxalpha_novelty_check",
        allowed_stages={"score_review", "candidate_decision", "novelty_review"},
    )

    assert result is not None
    assert not result.ok
    assert "missing required research step stage_transition" in result.err


def test_stage_guard_allows_matching_transition(monkeypatch, tmp_path):
    _redirect_research_steps(monkeypatch, tmp_path)
    svc.factor_tool_record_research_step(
        stage="score_review",
        summary="Score review complete.",
        decision="Two candidates should enter novelty.",
        next_action="Run novelty.",
        stage_transition={
            "facts": "two B candidates",
            "judgment": "continue normal validation",
            "next_stage": "novelty_review",
            "next_action": "run fxalpha_novelty_check",
            "research_strategy": "normal process flow to novelty check",
            "why": "quick score selected candidates",
            "history_used": "avoided last crowded expression family",
        },
    )

    result = svc._stage_guard_result(
        "fxalpha_novelty_check",
        allowed_stages={"score_review", "candidate_decision", "novelty_review"},
    )

    assert result is None
