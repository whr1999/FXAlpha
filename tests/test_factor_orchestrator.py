from __future__ import annotations

import asyncio
import json
import re
import sys
import types
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import pytest

from domain.factor_research import deepseek_client as deepseek_mod
from domain.factor_research.deepseek_client import (
    DeepSeekClientError,
    DeepSeekJSONClient,
    _deepseek_json_mode_enabled,
    _extract_json_object,
    _llm_models,
    _message_reasoning_text,
    _provider_model_name,
)
from domain.factor_research.orchestrator_context import OrchestratorContextPack, _compact_active_factor_summary
from domain.factor_research import orchestrator
from services import factor_research_service as svc


def _natural_language_summary():
    return {
        "summary": "本阶段已经完成研究判断，并形成了可供后续流程直接使用的结论。",
    }
from services._base import ok_result


def _redirect_orchestrator(monkeypatch, tmp_path):
    steps_dir = tmp_path / "research_steps"
    events_dir = tmp_path / "orchestrator_events"
    traces_dir = tmp_path / "orchestrator_llm_traces"
    monkeypatch.setattr(svc, "FACTOR_RESEARCH_STEPS_DIR", steps_dir)
    monkeypatch.setattr(svc, "FACTOR_RESEARCH_STEPS_FILE", steps_dir / "current.jsonl")
    monkeypatch.setattr(svc, "FACTOR_RESEARCH_STEPS_HISTORY_DIR", steps_dir / "history")
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_EVENTS_DIR", events_dir)
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_EVENTS_FILE", events_dir / "current.jsonl")
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR", events_dir / "history")
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_LLM_TRACES_DIR", traces_dir)
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_LLM_TRACES_FILE", traces_dir / "current.jsonl")
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_LLM_TRACES_HISTORY_DIR", traces_dir / "history")
    return steps_dir / "current.jsonl", events_dir / "current.jsonl"


def test_trajectory_metrics_match_quantgpt_style():
    metrics = orchestrator.analyze_trajectory(
        [
            {"expression": "a", "score": 40},
            {"expression": "b", "score": 55},
            {"expression": "c", "score": 50},
        ]
    )

    assert metrics["best_score"] == 55
    assert metrics["best_expression"] == "b"
    assert metrics["num_iterations"] == 3
    assert metrics["consecutive_declines"] == 1
    assert 0 < metrics["exploration_diversity"] < 1


def test_trajectory_parent_selection_excludes_downstream_rejected_candidates():
    trajectory = [
        {
            "round_id": "run:r0001",
            "candidate_id": "rejected_high",
            "expression": "rank(amount)",
            "score": 91,
            "parent_eligible": False,
        },
        {
            "round_id": "run:r0002",
            "candidate_id": "approved_parent",
            "expression": "rank(close)",
            "score": 80,
            "parent_eligible": True,
        },
        {
            "round_id": "run:r0003",
            "candidate_id": "current",
            "expression": "rank(volume)",
            "score": 60,
        },
    ]

    metrics = orchestrator.analyze_trajectory(trajectory)
    segments = orchestrator.top_trajectory_segments(trajectory)

    assert metrics["num_iterations"] == 3
    assert metrics["best_score"] == 80
    assert metrics["best_expression"] == "rank(close)"
    assert [item["candidate_id"] for item in segments] == ["approved_parent", "current"]


def test_trajectory_merge_keeps_same_candidate_expression_across_rounds():
    merged = orchestrator._merge_trajectory(
        [
            {
                "round_id": "run:r0001",
                "candidate_id": "c1",
                "expression": "rank(close)",
                "score": 70,
            },
            {
                "round_id": "run:r0002",
                "candidate_id": "c1",
                "expression": "rank(close)",
                "score": 71,
            },
        ],
        [],
    )

    assert [(item["round_id"], item["score"]) for item in merged] == [
        ("run:r0001", 70),
        ("run:r0002", 71),
    ]


def test_expression_profile_counts_all_fxalpha_fields_not_only_price_volume():
    profile = orchestrator.expression_profile(
        "rank(-ts_av_diff(borrow_money_bal,10)) * rank(-ps_ttm)"
    )

    assert profile["base_signals"] == ["borrow_money_bal", "ps_ttm"]
    assert profile["base_signal_count"] == 2


@pytest.mark.parametrize(
    ("candidate", "expected_strategy"),
    [
        ({"expression": "rank(close)", "score": 10, "grade": "D"}, "regenerate_full"),
        (
            {
                "expression": "rank(ts_mean(close,10))",
                "score": 55,
                "grade": "C",
                "key_metrics": {"ic_mean": 0.001, "ic_ir": 0.8},
            },
            "mutate_operator",
        ),
        (
            {
                "expression": "rank(ts_mean(close,10))",
                "score": 75,
                "grade": "B",
                "key_metrics": {"ic_mean": -0.02, "ic_ir": 0.8},
            },
            "mutate_signal_type",
        ),
        (
            {
                "expression": "rank(rank(rank(rank(rank(rank(rank(rank(rank(close)))))))))",
                "score": 55,
                "grade": "C",
                "key_metrics": {"ic_mean": 0.02, "ic_ir": 0.8},
            },
            "simplify",
        ),
        (
            {
                "expression": "rank(ts_mean(close,10)) * rank(ts_mean(volume,10))",
                "score": 30,
                "grade": "D",
                "key_metrics": {"ic_mean": 0.02, "ic_ir": 0.8},
            },
            "mutate_nonlinear",
        ),
        (
            {
                "expression": "ts_mean(close,10) * ts_mean(volume,10)",
                "score": 55,
                "grade": "C",
                "key_metrics": {"ic_mean": 0.02, "ic_ir": 0.2},
            },
            "mutate_normalization",
        ),
        (
            {
                "expression": "rank(ts_mean(close,10))",
                "score": 55,
                "grade": "C",
                "key_metrics": {"ic_mean": 0.02, "ic_ir": 0.8},
            },
            "mutate_interaction",
        ),
        (
            {
                "expression": "rank(ts_mean(close,10)) * rank(ts_mean(volume,10))",
                "score": 55,
                "grade": "C",
                "key_metrics": {"ic_mean": 0.02, "ic_ir": 0.8},
            },
            "mutate_window",
        ),
    ],
)
def test_mutation_diagnosis_implements_all_quantgpt_treatments(candidate, expected_strategy):
    diagnosis = orchestrator.mutation_diagnosis(candidate)

    assert diagnosis["strategy"] == expected_strategy
    assert diagnosis["strategy"] in orchestrator.MUTATION_STRATEGIES


def test_meta_strategy_exposes_all_four_evolution_strategies():
    simplify = orchestrator.meta_strategy(
        {"num_iterations": 1},
        current_score=70,
        nesting_depth=9,
    )
    exploit = orchestrator.meta_strategy(
        {
            "num_iterations": 4,
            "exploration_diversity": 0.1,
            "convergence_rate": 0.5,
            "stability_score": 0.9,
            "consecutive_declines": 0,
            "best_score": 75,
        },
        current_score=72,
    )
    recombine = orchestrator.meta_strategy(
        {
            "num_iterations": 4,
            "exploration_diversity": 0.1,
            "convergence_rate": 0.0,
            "stability_score": 0.9,
            "consecutive_declines": 3,
            "best_score": 82,
        },
        current_score=76,
    )
    explore = orchestrator.meta_strategy(
        {
            "num_iterations": 2,
            "exploration_diversity": 0.2,
            "convergence_rate": 0.0,
            "stability_score": 0.5,
            "consecutive_declines": 0,
            "best_score": 20,
        },
        current_score=18,
    )

    assert {
        simplify["strategy"],
        exploit["strategy"],
        recombine["strategy"],
        explore["strategy"],
    } == orchestrator.EVOLUTION_STRATEGIES


def test_quick_advice_uses_cross_candidate_declines_to_recombine():
    advice = orchestrator.quick_advice(
        [
            {
                "candidate_id": "current",
                "expression": "rank(ts_mean(close,10)) * rank(ts_mean(volume,10))",
                "score": 55,
                "grade": "C",
                "key_metrics": {"ic_mean": 0.02, "ic_ir": 0.8},
            }
        ],
        trajectory=[
            {"candidate_id": "p1", "round_id": "run:r0001", "expression": "rank(close)", "score": 90},
            {"candidate_id": "p2", "round_id": "run:r0002", "expression": "rank(volume)", "score": 80},
            {"candidate_id": "p3", "round_id": "run:r0003", "expression": "rank(amount)", "score": 70},
        ],
    )

    lane = advice["candidate_lane_decisions"][0]
    assert advice["trajectory_metrics"]["num_iterations"] == 4
    assert advice["trajectory_metrics"]["consecutive_declines"] == 3
    assert lane["evolution_strategy"]["strategy"] == "recombine"
    assert lane["action"] == "recombine_from_best"
    assert advice["recombination_candidates"][0]["candidate_id"] == "p1"

    compact = svc._compact_prompt_advice(advice)
    assert compact["evolution_strategy"]["strategy"] == "recombine"
    assert compact["candidate_lane_decisions"][0]["mutation_diagnosis"]["strategy"] == "mutate_window"
    assert compact["recombination_candidates"][0]["candidate_id"] == "p1"


def test_quick_advice_current_hard_failure_overrides_historical_score_gap():
    advice = orchestrator.quick_advice(
        [
            {
                "candidate_id": "current",
                "expression": "rank(ts_mean(lg_net_amount,10)) * rank(-abs(close/cost_15pct - 1))",
                "score": 14.1,
                "grade": "D",
                "key_metrics": {"ic_mean": 0.001, "ic_ir": 0.1},
            }
        ],
        trajectory=[
            {
                "candidate_id": "historical_a",
                "round_id": "run:r0001",
                "expression": "rank(-ts_std(turnover_rate,20)) * rank(ts_delta(roa,60))",
                "score": 85.4,
                "grade": "A",
            },
            {
                "candidate_id": "historical_c",
                "round_id": "run:r0001",
                "expression": "rank(ts_mean(lg_net_amount,5))",
                "score": 69.8,
                "grade": "C",
            },
            {
                "candidate_id": "historical_d",
                "round_id": "run:r0002",
                "expression": "rank(close)",
                "score": 5.1,
                "grade": "D",
            },
        ],
    )

    assert advice["candidate_lane_decisions"][0]["action"] == "explore_new_thesis"
    assert advice["evolution_strategy"] == {
        "strategy": "explore",
        "action": "explore_new_thesis",
        "reason": "all_current_candidates_require_full_regeneration",
    }
    assert advice["recombination_candidates"] == []


def test_run_candidate_trajectory_reads_persisted_score_and_deep_evidence(monkeypatch):
    steps = [
        {
            "ts": "2026-07-24T01:00:00",
            "run_id": "run1",
            "round_id": "run1:r0001",
            "stage": "score_review",
            "stage_seq": 6,
            "evidence_refs": [
                {
                    "type": "candidate_lanes",
                    "items": [
                        {
                            "candidate_id": "c1",
                            "trajectory_id": "ft-score-c1",
                            "parent_candidate_id": "run1:r0000:c4",
                            "mutation_summary": "change only confirmation window",
                            "matched_region_uid": "region_1",
                            "expression": "rank(close)",
                            "score": 71.2,
                            "grade": "B",
                        }
                    ],
                }
            ],
        },
        {
            "ts": "2026-07-24T01:03:00",
            "run_id": "run1",
            "round_id": "run1:r0001",
            "stage": "novelty_review",
            "stage_seq": 7,
            "evidence_refs": [
                {
                    "type": "advice_summary",
                    "candidate_lane_decisions": [
                        {
                            "candidate_id": "c1",
                            "expression": "rank(close)",
                            "action": "advance_to_deep_validation",
                            "reason": "formal_novelty_pass",
                        }
                    ],
                }
            ],
        },
        {
            "ts": "2026-07-24T01:05:00",
            "run_id": "run1",
            "round_id": "run1:r0001",
            "stage": "deep_validation_review",
            "stage_seq": 8,
            "evidence_refs": [
                {
                    "type": "advice_summary",
                    "candidate_lane_decisions": [
                        {
                            "candidate_id": "c1",
                            "trajectory_id": "ft-deep-c1",
                            "parent_candidate_id": "run1:r0000:c4",
                            "mutation_summary": "change only confirmation window",
                            "matched_region_uid": "region_1",
                            "expression": "rank(close)",
                            "deep_score": 76.4,
                            "rolling_score": 58.2,
                            "grade": "B",
                            "action": "targeted_mutation",
                        }
                    ],
                }
            ],
        },
    ]
    monkeypatch.setattr(
        svc,
        "_read_recent_research_steps",
        lambda limit=20, run_id=None: list(reversed(steps)),
    )

    score_trajectory = svc._orchestrator_run_candidate_trajectory(
        run_id="run1",
        stage="score_review",
    )
    deep_trajectory = svc._orchestrator_run_candidate_trajectory(
        run_id="run1",
        stage="deep_validation_review",
    )

    assert score_trajectory[0]["score"] == 71.2
    assert deep_trajectory[0]["score"] == 76.4
    assert score_trajectory[0]["round_id"] == "run1:r0001"
    assert score_trajectory[0]["deep_score"] == 76.4
    assert score_trajectory[0]["rolling_score"] == 58.2
    assert score_trajectory[0]["deep_action"] == "targeted_mutation"
    assert score_trajectory[0]["deep_parent_eligible"] is True
    assert score_trajectory[0]["parent_eligible"] is False
    assert score_trajectory[0]["downstream_action"] == "advance_to_deep_validation"
    assert deep_trajectory[0]["trajectory_id"] == "ft-deep-c1"
    assert deep_trajectory[0]["parent_candidate_id"] == "run1:r0000:c4"
    assert deep_trajectory[0]["mutation_summary"] == "change only confirmation window"
    assert deep_trajectory[0]["matched_region_uid"] == "region_1"
    assert deep_trajectory[0]["rolling_score"] == 58.2
    assert deep_trajectory[0]["downstream_action"] == "targeted_mutation"
    assert deep_trajectory[0]["parent_eligible"] is True


@pytest.mark.parametrize(
    ("deep_action", "expected_parent_eligible"),
    [
        ("submit_quality_gate", True),
        ("explore_new_thesis", False),
    ],
)
def test_score_trajectory_parent_eligibility_follows_joined_deep_terminal_action(
    monkeypatch,
    deep_action,
    expected_parent_eligible,
):
    steps = [
        {
            "ts": "2026-08-03T01:00:00",
            "run_id": "run1",
            "round_id": "run1:r0033",
            "stage": "score_review",
            "stage_seq": 6,
            "evidence_refs": [
                {
                    "type": "candidate_lanes",
                    "items": [
                        {
                            "candidate_id": "c1",
                            "expression": "rank(close)",
                            "score": 84.0,
                            "grade": "B",
                        }
                    ],
                }
            ],
        },
        {
            "ts": "2026-08-03T01:01:00",
            "run_id": "run1",
            "round_id": "run1:r0033",
            "stage": "novelty_review",
            "stage_seq": 7,
            "evidence_refs": [
                {
                    "type": "advice_summary",
                    "candidate_lane_decisions": [
                        {
                            "candidate_id": "c1",
                            "expression": "rank(close)",
                            "action": "advance_to_deep_validation",
                        }
                    ],
                }
            ],
        },
        {
            "ts": "2026-08-03T01:02:00",
            "run_id": "run1",
            "round_id": "run1:r0033",
            "stage": "deep_validation_review",
            "stage_seq": 8,
            "evidence_refs": [
                {
                    "type": "advice_summary",
                    "candidate_lane_decisions": [
                        {
                            "candidate_id": "c1",
                            "expression": "rank(close)",
                            "deep_score": 64.0,
                            "rolling_score": 34.0,
                            "grade": "D",
                            "action": deep_action,
                        }
                    ],
                }
            ],
        },
    ]
    monkeypatch.setattr(
        svc,
        "_read_recent_research_steps",
        lambda limit=20, run_id=None: list(steps),
    )

    trajectory = svc._orchestrator_run_candidate_trajectory(
        run_id="run1",
        stage="score_review",
    )

    assert trajectory[0]["deep_score"] == 64.0
    assert trajectory[0]["rolling_score"] == 34.0
    assert trajectory[0]["deep_action"] == deep_action
    assert trajectory[0]["parent_eligible"] is expected_parent_eligible
    assert bool(orchestrator.top_trajectory_segments(trajectory)) is expected_parent_eligible


def test_compact_candidate_distinguishes_missing_joined_and_full_rolling_evidence():
    missing = svc._compact_orchestrator_candidate_for_diagnosis(
        {"candidate_id": "missing", "expression": "rank(close)"}
    )
    joined = svc._compact_orchestrator_candidate_for_diagnosis(
        {
            "candidate_id": "joined",
            "expression": "rank(close)",
            "rolling_score": 49.6,
        }
    )
    available = svc._compact_orchestrator_candidate_for_diagnosis(
        {
            "candidate_id": "available",
            "expression": "rank(close)",
            "rolling_validation": {
                "score": 72.0,
                "incremental_periods": [{"period": 1}, {"period": 2}],
            },
        }
    )

    assert missing["rolling_evidence_status"] == "not_joined"
    assert "rolling_period_count" not in missing
    assert joined["rolling_evidence_status"] == "joined_summary"
    assert joined["rolling_score"] == 49.6
    assert "rolling_period_count" not in joined
    assert available["rolling_evidence_status"] == "available"
    assert available["rolling_period_count"] == 2


def test_import_event_summary_uses_canonical_model_snapshot_field():
    summary = svc._orchestrator_import_event_summary(
        import_ok=True,
        imported_count=2,
        requested_count=2,
        adopted_total=7,
        import_sync_status={
            "active_values": "queued",
            "model_snapshot": "refresh_required",
            "model_features": "wrong_legacy_value",
        },
    )

    assert "active_values=queued" in summary
    assert "model_snapshot=refresh_required" in summary
    assert "model_features" not in summary
    assert "unknown" not in summary


def test_research_step_previous_link_never_crosses_runs(monkeypatch, tmp_path):
    steps_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)

    def record(run_id: str, stage_id: str, summary: str):
        result = svc.factor_tool_record_research_step(
            stage="note",
            summary=summary,
            decision="continue",
            run_id=run_id,
            stage_id=stage_id,
            stage_transition={"next_stage": "note", "next_action": "continue"},
        ).to_dict()
        assert result["ok"] is True

    record("run-a", "run-a:r0001:s01_note", "a1")
    record("run-b", "run-b:r0001:s01_note", "b1")
    record("run-a", "run-a:r0001:s02_note", "a2")

    rows = [json.loads(line) for line in steps_file.read_text(encoding="utf-8").splitlines()]
    last = rows[-1]
    assert last["run_id"] == "run-a"
    assert last["previous_stage_id"] == "run-a:r0001:s01_note"


def test_stage_guard_uses_requested_run_transition(monkeypatch, tmp_path):
    steps_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)
    steps_file.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "schema_version": "research_step_v2",
            "ts": "2026-07-11T10:00:00+00:00",
            "run_id": "run-a",
            "round_id": "run-a:r0001",
            "stage_id": "run-a:r0001:s05_import_gate_review",
            "stage": "import_gate_review",
            "stage_transition": {"next_stage": "import_review", "next_action": "import"},
        },
        {
            "schema_version": "research_step_v2",
            "ts": "2026-07-11T10:01:00+00:00",
            "run_id": "run-b",
            "round_id": "run-b:r0001",
            "stage_id": "run-b:r0001:s03_score_review",
            "stage": "score_review",
            "stage_transition": {"next_stage": "novelty_review", "next_action": "check novelty"},
        },
    ]
    steps_file.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    allowed = {"import_gate_review", "import_review"}
    assert svc._stage_guard_result("import", allowed_stages=allowed, run_id="run-a") is None
    blocked = svc._stage_guard_result("import", allowed_stages=allowed, run_id="run-b")
    assert blocked is not None
    assert blocked.to_dict()["ok"] is False


def test_human_guidance_preserves_current_run_transition(monkeypatch, tmp_path):
    steps_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(
        svc,
        "_GUI_RUNS",
        {
            "run-guidance": {
                "run_id": "run-guidance",
                "status": "running",
                "events": [],
                "guidance_history": [],
            }
        },
    )
    monkeypatch.setattr(svc, "_persist_job", lambda job: None)
    svc.factor_tool_record_research_step(
        stage="note",
        summary="current research position",
        decision="continue score review",
        run_id="run-guidance",
        stage_id="run-guidance:r0001:s01_note",
        stage_transition={"next_stage": "score_review", "next_action": "review scores"},
    )

    result = svc.factor_research_add_guidance(
        run_id="run-guidance", message="优先检查信号方向", author="operator"
    ).to_dict()

    assert result["ok"] is True
    latest = json.loads(steps_file.read_text(encoding="utf-8").splitlines()[-1])
    assert latest["stage"] == "human_guidance"
    assert latest["stage_transition"]["next_stage"] == "score_review"
    assert latest["stage_transition"]["next_action"] == "review scores"
    assert latest["extra"]["guidance_id"].startswith("guidance_")
    assert result["outputs"]["guidance_id"] == latest["extra"]["guidance_id"]


def test_guidance_rejects_missing_completed_stopping_and_oversized_runs(monkeypatch, tmp_path):
    steps_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "_GUI_RUNS", {})

    missing = svc.factor_research_add_guidance(
        run_id="run-missing", message="不会被消费", author="operator"
    ).to_dict()
    assert missing["ok"] is False
    assert "not found" in missing["err"]

    steps_file.parent.mkdir(parents=True, exist_ok=True)
    steps_file.write_text(
        json.dumps(
            {
                "schema_version": "research_step_v2",
                "ts": "2026-07-13T19:59:00",
                "run_id": "run-stale",
                "round_id": "run-stale:r0001",
                "stage_id": "run-stale:r0001:s05_score_review",
                "stage": "score_review",
                "summary": "orphaned nonterminal step",
                "decision": "continue",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    stale = svc.factor_research_add_guidance(
        run_id="run-stale", message="没有活跃 worker", author="operator"
    ).to_dict()
    assert stale["ok"] is False
    assert "not active" in stale["err"]

    steps_file.write_text(
        json.dumps(
            {
                "schema_version": "research_step_v2",
                "ts": "2026-07-13T20:00:00",
                "run_id": "run-completed",
                "round_id": "run-completed:stop",
                "stage_id": "run-completed:stop:s99_checkpoint_stop",
                "stage": "checkpoint_stop",
                "summary": "completed",
                "decision": "done",
                "tags": ["checkpoint_stop", "operator_stop"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    completed = svc.factor_research_add_guidance(
        run_id="run-completed", message="不会有下一次判断", author="operator"
    ).to_dict()
    assert completed["ok"] is False
    assert "completed" in completed["err"]

    svc.factor_tool_record_research_step(
        stage="score_review",
        summary="running",
        decision="continue",
        run_id="run-stopping",
        stage_id="run-stopping:r0001:s05_score_review",
    )
    monkeypatch.setattr(
        svc,
        "_GUI_RUNS",
        {
            "run-stopping": {
                "run_id": "run-stopping",
                "status": "stop_requested",
                "control_action": "stop",
                "stop_requested": True,
            }
        },
    )
    stopping = svc.factor_research_add_guidance(
        run_id="run-stopping", message="停止中不应接收", author="operator"
    ).to_dict()
    assert stopping["ok"] is False
    assert "stopping" in stopping["err"]

    oversized = svc.factor_research_add_guidance(
        run_id="run-stopping",
        message="x" * (svc.FACTOR_RESEARCH_GUIDANCE_MAX_CHARS + 1),
        author="operator",
    ).to_dict()
    assert oversized["ok"] is False
    assert "exceeds 500" in oversized["err"]


def test_orchestrator_context_preserves_recent_steps_and_latest_stage_transition():
    pack = OrchestratorContextPack(
        run_id="run-ctx",
        round_id="run-ctx:r0003",
        stage="score_review",
        contract={"target_adopted": 10},
        active_context={
            "latest_stage_transition": {
                "stage": "novelty_review",
                "ts": "2026-07-02T10:00:00",
                "stage_transition": {
                    "next_stage": "deep_validation_review",
                    "next_action": "run deep validation bundle",
                    "judgment": "candidate passed novelty",
                    "why": "low active-pool correlation",
                },
            }
        },
        recent_steps=[
            {
                "ts": "2026-07-02T10:01:00",
                "stage": "score_review",
                "summary": "Quick score selected one keeper.",
                "decision": "advance_some",
                "stage_transition": {
                    "next_stage": "novelty_review",
                    "next_action": "run fxalpha_novelty_check",
                    "facts": "c1 grade B",
                    "judgment": "worth novelty",
                    "why": "quick evidence passed",
                    "history_used": "prior crowded family avoided",
                },
                "tags": ["orchestrator"],
            }
        ],
        quantgpt_summary={},
        round_events=[],
    ).to_dict()

    latest = pack["active_context"]["latest_stage_transition"]
    assert latest["next_stage"] == "deep_validation_review"
    assert latest["stage_transition"]["next_action"] == "run deep validation bundle"

    step = pack["recent_steps"][0]
    assert step["stage"] == "score_review"
    assert step["next_stage"] == "novelty_review"
    assert step["next_action"] == "run fxalpha_novelty_check"
    assert step["stage_transition"]["facts"] == "c1 grade B"
    assert step["stage_transition"]["judgment"] == "worth novelty"


def test_quick_advice_embeds_mutation_rules():
    advice = orchestrator.quick_advice(
        [
            {
                "expression": "ts_mean(ts_mean(ts_mean(ts_mean(ts_mean(close,5),5),5),5),5)",
                "score": 70,
                "grade": "B",
                "key_metrics": {"ic_mean": 0.03, "ic_ir": 0.4},
            },
            {
                "expression": "close",
                "score": 10,
                "grade": "D",
                "key_metrics": {"ic_mean": 0.0, "ic_ir": 0.0},
            },
        ]
    )

    lane_actions = [lane["action"] for lane in advice["candidate_lane_decisions"]]
    assert "advance_to_novelty" in lane_actions
    assert "explore_new_thesis" in lane_actions
    assert advice["blocked_actions"] == ["fxalpha_quality_gate", "fxalpha_import_factors"]


def test_quick_advice_only_uses_sign_flip_for_ab_keeper():
    advice = orchestrator.quick_advice(
        [
            {
                "candidate_id": "keeper",
                "expression": "rank(close)",
                "status": "success",
                "score": 76,
                "grade": "B",
                "rank_ic": -0.03,
                "backtest_summary": {"rank_ic_mean": -0.03, "ic_mean": -0.03},
            },
            {
                "candidate_id": "weak",
                "expression": "rank(amount)",
                "status": "success",
                "score": 34,
                "grade": "D",
                "rank_ic": -0.03,
                "backtest_summary": {"rank_ic_mean": -0.03, "ic_mean": -0.03},
            },
        ]
    )
    lanes = {
        item["candidate_id"]: item
        for item in advice["candidate_lane_decisions"]
    }

    assert lanes["keeper"]["action"] == "mutate_signal_direction"
    assert lanes["keeper"]["reason"] == "negative_ic_ab_keeper"
    assert lanes["weak"]["action"] == "explore_new_thesis"
    assert lanes["weak"]["reason"] == "negative_ic_without_quick_keeper"


def test_quick_advice_does_not_flip_same_ab_candidate_twice():
    advice = orchestrator.quick_advice(
        [
            {
                "candidate_id": "keeper",
                "expression": "-1 * rank(close)",
                "mutation_summary": "global_sign_flip_only",
                "status": "success",
                "score": 76,
                "grade": "B",
                "rank_ic": -0.03,
                "backtest_summary": {"rank_ic_mean": -0.03, "ic_mean": -0.03},
            }
        ]
    )

    lane = advice["candidate_lane_decisions"][0]
    assert lane["action"] == "reject_as_negative_evidence"
    assert lane["reason"] == "direction_normalization_failed"


def test_score_code_keeper_requires_final_deep_validate_payload():
    good = {
        "candidate_id": "c1",
        "status": "success",
        "screening_stage": "quick_score",
        "score": 75.0,
        "grade": "B",
        "screening_hint": {"deep_validation_required": True},
    }

    assert svc._score_candidate_code_keeper(good) is True
    assert svc._score_candidate_code_keeper({**good, "screening_stage": "quick_score_progress"}) is False
    assert svc._score_candidate_code_keeper({**good, "status": "score_error"}) is False
    assert svc._score_candidate_code_keeper({**good, "grade": "C"}) is False
    assert svc._score_candidate_code_keeper({**good, "score": 63.7, "grade": "B"}) is False
    assert svc._score_candidate_code_keeper({**good, "score": 75.0, "grade": "C"}) is False
    assert svc._score_candidate_code_keeper({**good, "screening_hint": {}, "single_factor_decision": ""}) is False
    assert svc._score_candidate_code_keeper({**good, "screening_hint": {}, "single_factor_decision": "deep_validate"}) is True


def test_score_review_direction_revision_requires_explicit_sign_only_contract():
    review = {
        "candidate_decisions": [
            {
                "candidate_id": "c1",
                "action": "revise_expression",
                "failure_class": "direction_normalization",
                "mutation_advice": {
                    "type": "mutate_signal_direction",
                    "instruction": "global_sign_flip_only",
                },
            },
            {
                "candidate_id": "c2",
                "action": "revise_expression",
                "failure_class": "near_miss_mutate",
                "mutation_advice": {
                    "type": "mutate_signal_direction",
                    "instruction": "global_sign_flip_only",
                },
            },
        ]
    }

    assert svc._score_review_direction_revision_ids(review) == {"c1"}


def test_direction_handoff_has_priority_until_expression_design_consumes_it():
    direction = {
        "from_stage": "score_review",
        "to_stage": "expression_design",
        "binding_policy": "direction_normalization_global_sign_flip_only",
        "reason": "negative signed RankIC",
        "recommended_mutation": "global_sign_flip_only",
        "parent_candidate_refs": ["c2"],
    }
    generic = svc._mechanism_level_handoff(
        from_stage="round_synthesis",
        to_stage="expression_design",
        parent_candidate_refs=["c1"],
    )

    selected = svc._select_prompt_handoff(
        stage="expression_design",
        previous_advice=[direction, generic],
        return_handoff=generic,
    )

    assert selected["binding_policy"] == "direction_normalization_global_sign_flip_only"
    assert selected["parent_candidate_refs"] == ["c2"]


def test_thesis_handoff_is_visible_only_to_thesis_stage():
    explore = {
        "from_stage": "round_synthesis",
        "to_stage": "thesis_design",
        "binding_policy": "mechanism_and_evidence_only_not_literal_expression_instruction",
        "reason": "D级候选无保留价值，探索新主题。",
        "recommended_mutation": "EXPLORE:regenerate_full",
        "parent_candidate_refs": [],
    }
    stale_expression = {
        "from_stage": "candidate_plan",
        "to_stage": "expression_design",
        "binding_policy": "mechanism_and_evidence_only_not_literal_expression_instruction",
        "reason": "旧候选需要修正。",
        "parent_candidate_refs": ["r0002:c2"],
    }

    hypothesis_handoff = svc._select_prompt_handoff(
        stage="hypothesis_design",
        previous_advice=[],
        return_handoff=explore,
    )
    expression_handoff = svc._select_prompt_handoff(
        stage="expression_design",
        previous_advice=[],
        return_handoff=explore,
    )

    assert svc._select_prompt_handoff(
        stage="thesis_design",
        previous_advice=[stale_expression],
        return_handoff=explore,
    )["to_stage"] == "thesis_design"
    assert hypothesis_handoff == {}
    assert expression_handoff == {}


def test_downstream_handoff_is_not_visible_to_an_upstream_stage():
    expression_handoff = {
        "from_stage": "candidate_plan",
        "to_stage": "expression_design",
        "binding_policy": "mechanism_and_evidence_only_not_literal_expression_instruction",
        "reason": "修正表达式。",
    }

    assert svc._handoff_targets_stage(expression_handoff, "thesis_design") is False
    assert svc._handoff_targets_stage(expression_handoff, "hypothesis_design") is False
    assert svc._handoff_targets_stage(expression_handoff, "expression_design") is True


def test_deep_research_return_is_preserved_before_round_synthesis():
    review = {
        "why": "跨候选证据支持重组已有优质信息腿。",
        "stage_transition": {
            "next_stage": "hypothesis_design",
            "reason": "重新组合两个不同变量组。",
        },
    }

    preserved = svc._deep_research_review_before_synthesis(
        review,
        {"evolution_strategy": {"strategy": "recombine"}},
    )
    forced = svc._force_code_transition(
        preserved,
        next_stage="round_synthesis",
        next_action="synthesize_deep_failures",
        reason="immediate pipeline transition",
    )

    assert preserved["stage_transition"]["next_stage"] == "hypothesis_design"
    assert forced["stage_transition"]["next_stage"] == "round_synthesis"
    handoff = svc._return_handoff_from_stage("deep_validation_review", preserved)
    assert handoff["to_stage"] == "hypothesis_design"


def test_deep_research_return_falls_back_to_code_meta_strategy():
    review = {
        "why": "模型误把立即执行阶段写成了下一轮入口。",
        "stage_transition": {"next_stage": "round_synthesis", "reason": "先总结"},
    }

    recombine = svc._deep_research_review_before_synthesis(
        review,
        {
            "candidate_lane_decisions": [
                {
                    "action": "recombine_from_best",
                    "evolution_strategy": {"strategy": "recombine"},
                }
            ]
        },
    )
    explore = svc._deep_research_review_before_synthesis(
        review,
        {"evolution_strategy": {"strategy": "explore", "action": "explore_new_thesis"}},
    )
    exploit = svc._deep_research_review_before_synthesis(
        review,
        {"evolution_strategy": {"strategy": "exploit", "action": "targeted_mutation"}},
    )

    assert recombine["stage_transition"]["next_stage"] == "hypothesis_design"
    assert explore["stage_transition"]["next_stage"] == "thesis_design"
    assert exploit["stage_transition"]["next_stage"] == "expression_design"
    assert recombine["stage_transition"]["research_resume_fallback"] is True


def test_fresh_run_first_thesis_receives_latest_completed_previous_run_research_step():
    context_pack = {
        "run_state": {"run_id": "run-new", "round_id": "run-new:r0001"},
        "recent_steps": [
            {
                "run_id": "run-new",
                "round_id": "run-new:r0001",
                "stage": "protocol_load",
                "decision": "start",
            },
            {
                "run_id": "run-old",
                "round_id": "run-old:r0012",
                "stage": "round_synthesis",
                "decision": "continue_next_round",
                "summary": "上一轮完成了四个候选的验证，只有一个候选接近深度验证门槛。",
                "stage_transition": {
                    "next_stage": "hypothesis_design",
                    "judgment": "资金流机制仍有研究价值，但滚动稳定性不足。",
                    "why": "候选 quick 与 novelty 通过，rolling 是主要短板。",
                    "history_used": ["上一轮 score 与 deep 结果"],
                },
            },
        ],
    }

    handoff = svc._latest_previous_run_research_handoff(context_pack, stage="thesis_design")

    assert handoff["binding_policy"] == "previous_run_research_continuity"
    assert handoff["previous_run_id"] == "run-old"
    assert handoff["previous_round_id"] == "run-old:r0012"
    assert handoff["to_stage"] == "hypothesis_design"
    assert "滚动稳定性不足" in handoff["judgment"]

    payload = svc._orchestrator_stage_payload(
        stage="thesis_design",
        context_pack=context_pack,
        stage_input={},
        lineage_context={},
        round_events=[],
        return_handoff={
            "from_stage": "orchestrator_interrupted",
            "to_stage": "thesis_design",
            "reason": "通用启动占位符",
        },
    )
    assert payload["context_pack"]["upstream_handoff"]["binding_policy"] == "previous_run_research_continuity"
    assert payload["context_pack"]["upstream_handoff"]["previous_run_id"] == "run-old"

    compacted_pack = svc.OrchestratorContextPack(
        run_id="run-new",
        round_id="run-new:r0001",
        stage="thesis_design",
        contract={},
        active_context={},
        recent_steps=context_pack["recent_steps"],
        quantgpt_summary={},
        round_events=[],
    ).to_dict()
    compacted_handoff = svc._latest_previous_run_research_handoff(compacted_pack, stage="thesis_design")
    assert compacted_handoff["previous_run_id"] == "run-old"
    assert compacted_handoff["previous_round_id"] == "run-old:r0012"


def test_previous_run_handoff_is_not_reinjected_after_current_run_has_research_result():
    context_pack = {
        "run_state": {"run_id": "run-new"},
        "recent_steps": [
            {
                "run_id": "run-new",
                "round_id": "run-new:r0001",
                "stage": "score_review",
                "decision": "advance_some",
            },
            {
                "run_id": "run-old",
                "round_id": "run-old:r0012",
                "stage": "round_synthesis",
                "decision": "continue_next_round",
                "summary": "上一轮总结",
            },
        ],
    }

    assert svc._latest_previous_run_research_handoff(context_pack, stage="thesis_design") == {}


def test_previous_novelty_continuity_preserves_expression_return_and_parent():
    context_pack = {
        "run_state": {"run_id": "run-new"},
        "recent_steps": [
            {
                "run_id": "run-old",
                "round_id": "run-old:r0003",
                "stage": "novelty_review",
                "decision": "orthogonalize",
                "summary": "B级候选首次拥挤，保留机制并正交化。",
                "candidate_lanes": {
                    "dropped": [
                        {"candidate_id": "c4", "novelty_score": 0.0}
                    ]
                },
                "stage_transition": {
                    "next_stage": "expression_design",
                    "reason": "保留parent，只改变造成高相关的确认关系。",
                },
            }
        ],
    }

    handoff = svc._latest_previous_run_research_handoff(
        context_pack,
        stage="thesis_design",
    )

    assert handoff["to_stage"] == "expression_design"
    assert handoff["parent_candidate_refs"] == ["r0003:c4"]
    assert handoff["recommended_mutation"] == "CONTINUE:expression_design"
    assert "不重新换题" in handoff["must_preserve"][0]


def test_fresh_run_does_not_resurrect_design_step_after_terminal_stop():
    context_pack = {
        "run_state": {"run_id": "run-new"},
        "recent_steps": [
            {
                "run_id": "run-new",
                "round_id": "run-new:interrupted",
                "stage": "blocker",
                "decision": "previous_run_interrupted",
            },
            {
                "run_id": "run-old",
                "round_id": "run-old:r0001",
                "stage": "checkpoint_stop",
                "decision": "operator_stop_completed",
            },
            {
                "run_id": "run-old",
                "round_id": "run-old:r0001",
                "stage": "expression_design",
                "decision": "propose_candidates",
                "summary": "上一 run 已完成候选表达式设计。",
                "stage_transition": {"judgment": "资金与筹码机制已形成六个待评分候选。"},
            },
        ],
    }

    assert svc._latest_previous_run_research_handoff(
        context_pack,
        stage="thesis_design",
    ) == {}


def test_fresh_run_ignores_earlier_resume_step_when_same_run_is_terminal():
    context_pack = {
        "run_state": {"run_id": "run-new"},
        "recent_steps": [
            {
                "run_id": "run-old",
                "round_id": "run-old:stop",
                "stage": "checkpoint_stop",
                "decision": "operator_stop_completed",
            },
            {
                "run_id": "run-old",
                "round_id": "run-old:r0008",
                "stage": "hypothesis_design",
                "decision": "reuse_hypothesis",
                "summary": "旧局部机制",
                "stage_transition": {
                    "next_stage": "expression_design",
                    "reason": "继续修改旧 parent",
                },
            },
        ],
    }

    assert svc._latest_previous_run_research_handoff(
        context_pack,
        stage="thesis_design",
    ) == {}


def test_pending_direction_handoff_beats_later_round_synthesis_route():
    direction = {
        "from_stage": "score_review",
        "to_stage": "expression_design",
        "binding_policy": "direction_normalization_global_sign_flip_only",
        "reason": "negative signed RankIC",
        "recommended_mutation": "global_sign_flip_only",
        "parent_candidate_refs": ["c2"],
    }
    advice = [direction]
    synthesis = {
        "round_memory": {"suggested_start_stage": "thesis_design"},
        "stage_transition": {"next_stage": "thesis_design"},
    }

    selected = svc._adopt_round_synthesis_handoff(advice, synthesis)

    assert selected["binding_policy"] == "direction_normalization_global_sign_flip_only"
    assert selected["to_stage"] == "expression_design"


def test_score_review_prompt_candidate_keeps_direction_lineage_fields():
    compact = svc._compact_orchestrator_candidate_for_diagnosis(
        {
            "candidate_id": "c2b",
            "expression": "-1 * (rank(close))",
            "parent_candidate_id": "c2",
            "mutation_summary": "global_sign_flip_only",
            "status": "success",
            "screening_stage": "quick_score",
            "grade": "A",
            "backtest_summary": {"rank_ic_mean": 0.04},
        }
    )

    assert compact["parent_candidate_id"] == "c2"
    assert compact["mutation_summary"] == "global_sign_flip_only"


def test_novelty_code_keeper_uses_official_keepers_contract():
    good = {
        "candidate_id": "c1",
        "novelty_guard": {"allowed": True},
        "combined_guard": {"allowed": True, "novelty_allowed": True},
    }

    assert svc._novelty_candidate_code_keeper(good) is True
    assert svc._novelty_candidate_code_keeper({**good, "novelty_guard": {"allowed": False}}) is False
    assert svc._novelty_candidate_code_keeper({**good, "combined_guard": {"allowed": False, "novelty_allowed": True}}) is False
    assert svc._novelty_candidate_code_keeper({**good, "combined_guard": {"allowed": True, "novelty_allowed": False}}) is False
    assert svc._novelty_candidate_code_keeper(
        {
            **good,
            "combined_guard": {},
            "st_exposure_guard": {"passed": False, "mode": "hard", "reason": "st_exposure_veto"},
        }
    ) is False
    assert svc._novelty_candidate_code_keeper(
        {
            **good,
            "combined_guard": {},
            "st_exposure_guard": {"passed": False, "mode": "advisory", "reason": "st_exposure_veto"},
        }
    ) is True


def test_code_keeper_fallback_advances_llm_omitted_candidate():
    candidates = [
        {
            "candidate_id": "c1",
            "status": "success",
            "screening_stage": "quick_score",
            "score": 88.0,
            "grade": "A",
            "screening_hint": {"deep_validation_required": True},
        },
        {
            "candidate_id": "c2",
            "status": "success",
            "screening_stage": "quick_score",
            "score": 72.0,
            "grade": "B",
            "single_factor_decision": "deep_validate",
        },
    ]
    review = {
        "candidate_decisions": [
            {"candidate_id": "c1", "action": "advance_to_novelty"},
            {"candidate_id": "c2", "action": "reject"},
        ]
    }

    selected, audit = svc._code_authoritative_allowed_candidates(
        candidates,
        review,
        allow_actions={"advance_to_novelty"},
        code_keeper=svc._score_candidate_code_keeper,
        stage_label="score_review",
    )

    assert [item["candidate_id"] for item in selected] == ["c1", "c2"]
    assert audit["code_fallback_candidate_ids"] == ["c2"]


def test_current_candidate_board_uses_current_run_history_and_requires_task_link():
    steps = [
        {
            "ts": "2026-07-03T10:03:00",
            "run_id": "run-live",
            "round_id": "run-live:r0002",
            "stage": "score_review",
            "stage_id": "run-live:r0002:s05_score_review",
            "candidate_lanes": [
                {
                    "candidate_id": "c1",
                    "expression": "rank(close)",
                    "status": "success",
                    "screening_stage": "quick_score",
                    "score": 82.5,
                    "grade": "B",
                },
                {
                    "candidate_id": "c5",
                    "expression": "rank(vwap)",
                    "status": "success",
                    "screening_stage": "quick_score",
                    "score": 77.0,
                    "grade": "B",
                },
            ],
            "evidence_refs": [
                {"tool": "score_factor", "candidate_id": "c1", "task_id": "task-score-c1"},
                {"tool": "score_factor", "candidate_id": "c2"},
                {"tool": "score_factor", "candidate_id": "c5", "task_id": "stale-score-c5"},
            ],
        },
        {
            "ts": "2026-07-03T10:02:30",
            "run_id": "run-live",
            "round_id": "run-live:r0002",
            "stage": "score_review",
            "stage_id": "run-live:r0002:s05_score_review:candidate_3_c3",
            "tags": ["tool_progress", "score_review_progress", "candidate_progress"],
            "candidate_lanes": [
                {"candidate_id": "c3", "expression": "rank(high)", "status": "running"}
            ],
            "evidence_refs": [
                {"tool": "score_factor", "candidate_id": "c3", "candidate_index": 3, "candidate_total": 3},
            ],
        },
        {
            "ts": "2026-07-03T10:02:10",
            "run_id": "run-live",
            "round_id": "run-live:r0002",
            "stage": "score_review",
            "stage_id": "run-live:r0002:s05_score_review:candidate_4_c4",
            "tags": ["tool_progress", "score_review_progress", "candidate_progress"],
            "candidate_lanes": [
                {"candidate_id": "c4", "expression": "rank(low)", "status": "running"}
            ],
            "evidence_refs": [
                {"tool": "score_factor", "candidate_id": "c4", "candidate_index": 2, "candidate_total": 3},
            ],
        },
        {
            "ts": "2026-07-03T10:02:00",
            "run_id": "run-live",
            "round_id": "run-live:r0002",
            "stage": "candidate_plan",
            "stage_id": "run-live:r0002:s04_candidate_plan",
            "candidate_decisions": [
                {"candidate_id": "c2", "expression": "rank(open)", "status": "planned_for_score"},
            ],
        },
        {
            "ts": "2026-07-03T10:01:00",
            "run_id": "run-live",
            "round_id": "run-live:r0001",
            "stage": "score_review",
            "stage_id": "run-live:r0001:s05_score_review",
            "candidate_lanes": [
                {"candidate_id": "old", "expression": "rank(amount)", "status": "success", "score": 99.0, "grade": "A"}
            ],
        },
        {
            "ts": "2026-07-03T10:00:00",
            "run_id": "previous-run",
            "round_id": "previous-run:r0009",
            "stage": "score_review",
            "stage_id": "previous-run:r0009:s05_score_review",
            "candidate_lanes": [
                {"candidate_id": "external", "expression": "rank(volume)", "status": "success", "score": 100.0, "grade": "A"}
            ],
        },
    ]
    tasks = [
        {
            "task_id": "task-score-c1",
            "task_type": "score",
            "status": "completed",
            "created_at": "2026-07-03T10:03:01",
            "result": {"score": 83.0, "grade": "B", "single_factor_decision": "deep_validate"},
        },
        {
            "task_id": "unlinked-high-score",
            "task_type": "score",
            "status": "completed",
            "created_at": "2026-07-03T10:03:02",
            "result": {"score": 99.0, "grade": "A", "expression": "rank(open)"},
        },
    ]

    board = svc._current_candidate_board(steps, tasks)

    assert board["schema_version"] == "current_candidate_board_v1"
    assert board["source"] == "research_steps_current_run"
    assert board["tool_evidence_source"] == "quantgpt_tasks_by_explicit_task_id"
    assert board["run_id"] == "run-live"
    assert board["round_id"] == "run-live:r0002"
    assert board["current_round_id"] == "run-live:r0002"
    assert board["round_count"] == 2
    assert {item["candidate_id"] for item in board["candidates"]} == {"c1", "c2", "c3", "c4", "c5", "old"}
    assert "external" not in {item["candidate_id"] for item in board["candidates"]}

    c1 = next(item for item in board["candidates"] if item["candidate_id"] == "c1")
    assert c1["quick_score"] == 83.0
    assert c1["tool_evidence"][0]["task_id"] == "task-score-c1"
    assert c1["console_scope"] == "current_run"
    assert c1["quick_grade"] == "B"
    assert c1["grade_provenance"] == "quick_score"

    c2 = next(item for item in board["candidates"] if item["candidate_id"] == "c2")
    assert c2.get("quick_score") is None
    assert c2["evidence_errors"][0]["code"] == "missing_task_link"

    c5 = next(item for item in board["candidates"] if item["candidate_id"] == "c5")
    assert c5["quick_score"] == 77.0
    assert c5.get("evidence_errors") == []
    assert c5["detached_tool_evidence"][0]["task_id"] == "stale-score-c5"

    c3 = next(item for item in board["candidates"] if item["candidate_id"] == "c3")
    assert c3.get("evidence_errors") == []
    assert c3["pending_evidence"][0]["tool"] == "score_factor"
    assert c3["stage"] == "quick_score_running"
    assert c3["status"] == "running"

    c4 = next(item for item in board["candidates"] if item["candidate_id"] == "c4")
    assert c4.get("evidence_errors") == []
    assert c4["stage"] == "quick_score_pending_result"
    assert c4["status"] == "pending_result"
    assert c4["display_status_reason"] == "non_latest_score_progress_without_task_link"
    assert board["ok"] is False


def test_current_candidate_board_ignores_hypothesis_placeholders_and_accepts_step_summarized_evidence():
    steps = [
        {
            "ts": "2026-07-03T10:04:00",
            "run_id": "run-live",
            "round_id": "run-live:r0001",
            "stage": "deep_validation_review",
            "stage_id": "run-live:r0001:s07_deep_validation_review",
            "candidate_lanes": [
                {
                    "candidate_id": "c1",
                    "expression": "rank(close)",
                    "status": "success",
                    "deep_score": 82.1,
                    "rolling_validation": {"score": 0.7},
                }
            ],
            "evidence_refs": [
                {"tool": "deep_validation", "candidate_id": "c1"},
            ],
        },
        {
            "ts": "2026-07-03T10:03:00",
            "run_id": "run-live",
            "round_id": "run-live:r0001",
            "stage": "score_review",
            "stage_id": "run-live:r0001:s06_score_review",
            "candidate_lanes": [
                {
                    "candidate_id": "c2",
                    "expression": "rank(open)",
                    "status": "invalid_expression",
                    "score": 0,
                    "grade": "D",
                }
            ],
            "evidence_refs": [
                {"tool": "score_factor", "candidate_id": "c2"},
            ],
        },
        {
            "ts": "2026-07-03T10:02:00",
            "run_id": "run-live",
            "round_id": "run-live:r0001",
            "stage": "hypothesis_design",
            "stage_id": "run-live:r0001:s03_hypothesis_design",
            "evidence_refs": [
                {
                    "type": "candidate_lanes",
                    "items": [
                        {"candidate_id": "c3", "candidate_lane": "candidate_lanes"},
                    ],
                }
            ],
            "monitoring": {
                "candidate_watch": [
                    {"candidate_id": "c4", "candidate_lane": "candidate_lanes"},
                ]
            },
        },
        {
            "ts": "2026-07-03T10:01:00",
            "run_id": "run-live",
            "round_id": "run-live:r0001",
            "stage": "expression_design",
            "stage_id": "run-live:r0001:s04_expression_design",
            "evidence_refs": [
                {
                    "type": "candidate_lanes",
                    "items": [
                        {"candidate_id": "c1", "expression": "rank(close)"},
                        {"candidate_id": "c2", "expression": "rank(open)"},
                    ],
                }
            ],
        },
    ]

    board = svc._current_candidate_board(steps, [])

    ids = {item["candidate_id"] for item in board["candidates"]}
    assert ids == {"c1", "c2"}
    assert board["ok"] is True
    assert board["errors"] == []

    c1 = next(item for item in board["candidates"] if item["candidate_id"] == "c1")
    assert c1["deep_score"] == 82.1
    assert c1["unlinked_tool_evidence"][0]["tool"] == "deep_validation"

    c2 = next(item for item in board["candidates"] if item["candidate_id"] == "c2")
    assert c2["quick_score"] == 0
    assert c2["grade"] == "D"
    assert c2["unlinked_tool_evidence"][0]["tool"] == "score_factor"


def test_current_candidate_board_expands_novelty_advice_summary():
    steps = [
        {
            "ts": "2026-07-03T10:02:00",
            "run_id": "run-live",
            "round_id": "run-live:r0001",
            "stage": "novelty_review",
            "stage_id": "run-live:r0001:s07_novelty_review",
            "evidence_refs": [
                {
                    "type": "candidate_lanes",
                    "items": [
                        {"candidate_id": "c1", "expression": "rank(close)", "status": "success", "score": 82.0, "grade": "B"},
                        {"candidate_id": "c2", "expression": "rank(open)", "status": "success", "score": 81.0, "grade": "B"},
                    ],
                },
                {
                    "type": "advice_summary",
                    "candidate_lane_decisions": [
                        {
                            "candidate_id": "c1",
                            "expression": "rank(close)",
                            "action": "advance_to_deep_validation",
                            "reason": "novelty_allowed",
                            "novelty_score": 0.37,
                            "matched_existing_factor": "active_2",
                            "combined_guard": {"allowed": True, "novelty_allowed": True},
                        },
                        {
                            "candidate_id": "c2",
                            "expression": "rank(open)",
                            "action": "orthogonalize_or_switch_source",
                            "reason": "family_crowded_p90_threshold",
                            "novelty_score": 0.0,
                            "matched_existing_factor": "active_9",
                            "combined_guard": {},
                        },
                    ],
                },
            ],
        }
    ]

    board = svc._current_candidate_board(steps, [])

    assert board["ok"] is True
    c1 = next(item for item in board["candidates"] if item["candidate_id"] == "c1")
    assert c1["novelty_score"] == 0.37
    assert c1["novelty_guard"]["allowed"] is True
    assert c1["single_factor_decision"] == "deep_validate"
    assert c1["display_status_label"] == "待深验"

    c2 = next(item for item in board["candidates"] if item["candidate_id"] == "c2")
    assert c2["novelty_score"] == 0.0
    assert c2["novelty_guard"]["allowed"] is False
    assert c2["novelty_guard"]["reason"] == "family_crowded_p90_threshold"
    assert c2["single_factor_decision"] == "reject"
    assert c2["display_status_label"] == "因子库互相关拦截"
    assert c2["final_decision"] == "reject"


def test_current_candidate_board_does_not_turn_plan_drop_into_unlinked_novelty():
    steps = [
        {
            "ts": "2026-07-03T10:01:00",
            "run_id": "run-live",
            "round_id": "run-live:r0001",
            "stage": "candidate_plan",
            "stage_id": "run-live:r0001:s05_candidate_plan",
            "candidate_lanes": [
                {
                    "candidate_id": "c1",
                    "expression": "rank(close)",
                    "candidate_lane": "candidate_plan_dropped",
                    "status": "dropped",
                }
            ],
        },
        {
            "ts": "2026-07-03T10:02:00",
            "run_id": "run-live",
            "round_id": "run-live:r0001",
            "stage": "novelty_review",
            "stage_id": "run-live:r0001:s07_novelty_review",
            "candidate_lanes": [
                {"candidate_id": "c1", "expression": "rank(close)", "status": "reviewed"}
            ],
        },
    ]

    board = svc._current_candidate_board(steps, [])
    c1 = board["candidates"][0]

    assert c1["stage"] == "candidate_plan_dropped"
    assert c1["status"] == "dropped"
    assert c1.get("quick_score") is None
    assert c1["stage_history"][-1]["screening_stage"] == "novelty_review_unlinked"


def test_current_candidate_board_preserves_candidate_plan_explanation_fields():
    steps = [
        {
            "ts": "2026-07-03T10:01:00",
            "run_id": "run-live",
            "round_id": "run-live:r0001",
            "stage": "candidate_plan",
            "stage_id": "run-live:r0001:s05_candidate_plan",
            "candidate_lanes": [
                {
                    "candidate_id": "c1",
                    "expression": "rank(close)",
                    "candidate_lane": "candidate_plan_dropped",
                    "status": "dropped",
                    "keep": False,
                    "action": "drop",
                    "reason": "same_family_over_budget",
                    "status_label": "Candidate Plan 丢弃",
                    "status_reason": "same_family_over_budget",
                }
            ],
        }
    ]

    board = svc._current_candidate_board(steps, [])
    c1 = board["candidates"][0]

    assert c1["candidate_lane"] == "candidate_plan_dropped"
    assert c1["keep"] is False
    assert c1["action"] == "drop"
    assert c1["reason"] == "same_family_over_budget"
    assert c1["status_reason"] == "same_family_over_budget"


def test_round_synthesis_keeps_bounded_llm_return_stage():
    synthesis = {
        "next_action": "start_next_round_at_expression_design",
        "stage_transition": {"next_stage": "expression_design"},
        "round_memory": {"suggested_start_stage": "thesis_design"},
    }

    out = svc._round_synthesis_resume_transition(
        synthesis,
        fallback_next_stage="expression_design",
        fallback_next_action="start_next_round_at_expression_design",
    )

    assert out["stage_transition"]["next_stage"] == "thesis_design"
    assert out["next_action"] == "start_next_round"
    assert out["stage_transition"]["resume_policy"] == "llm_bounded_upstream_return"


def test_round_synthesis_main_leg_text_does_not_override_valid_llm_stage():
    synthesis = {
        "next_action": "start_next_round_at_expression_design",
        "stage_transition": {"next_stage": "expression_design"},
        "round_memory": {
            "suggested_start_stage": "expression_design",
            "next_round_handoff": "保留 rank(ts_mean) 几何；将主腿从 lg_net_vol 换为 holder_num；避免窗口变体。",
            "negative_lessons": [],
            "avoid_patterns": ["avoid lg_net_vol as main leg"],
        },
    }

    out = svc._round_synthesis_resume_transition(
        synthesis,
        fallback_next_stage="expression_design",
        fallback_next_action="start_next_round_at_expression_design",
    )

    assert out["round_memory"]["suggested_start_stage"] == "expression_design"
    assert out["stage_transition"]["next_stage"] == "expression_design"
    assert out["next_action"] == "start_next_round_at_expression_design"
    assert out["stage_transition"]["resume_policy"] == "llm_bounded_upstream_return"


def test_round_synthesis_confirmation_leg_mutation_can_stay_at_expression_design():
    synthesis = {
        "next_action": "start_next_round_at_expression_design",
        "stage_transition": {"next_stage": "expression_design"},
        "round_memory": {
            "suggested_start_stage": "expression_design",
            "next_round_handoff": "保留 margin_buy_amount 主腿；change confirmation leg and normalization。",
            "negative_lessons": [],
            "avoid_patterns": ["avoid window-only variants"],
        },
    }

    out = svc._round_synthesis_resume_transition(
        synthesis,
        fallback_next_stage="thesis_design",
        fallback_next_action="start_next_round",
    )

    assert out["stage_transition"]["next_stage"] == "expression_design"
    assert out["next_action"] == "start_next_round_at_expression_design"
    assert out["stage_transition"]["resume_policy"] == "llm_bounded_upstream_return"


def test_expression_prompt_excludes_factor_map_context():
    compact = svc._compact_stage_active_context_for_prompt(
        stage="expression_design",
        run_state={},
        active_context={
            "factor_map_context": {
                "available": True,
                "map_id": "fm_test",
                "audit_id": "fa_test",
                "regions": [
                    {"cluster_id": "information_001", "region_uid": "region_one", "size": 1, "representative": {"factor_id": "f1", "name": "One", "expression": "rank(close)"}, "members": [{"factor_id": "f1", "name": "One", "expression": "rank(close)"}]},
                ],
            },
            "field_context": {"supported_fields": ["close"], "supported_operators": ["rank"]},
        },
    )

    assert "factor_map_context" not in compact
    assert compact["research_space"]["supported_fields"] == ["close"]
    assert "symbolic_family_representatives" not in compact


def test_operator_guidance_reaches_every_llm_stage_without_expanding_research_space():
    guidance = {
        "guidance_id": "guidance_test",
        "stage_id": "run:r0001:s98_human_guidance",
        "summary": "保留当前主信号，只调整归一化。",
        "author": "operator",
    }
    for stage in svc._ORCHESTRATOR_STAGE_BRIEFINGS:
        compact = svc._compact_stage_active_context_for_prompt(
            run_state={},
            active_context={"operator_guidance": guidance},
            stage=stage,
        )
        assert compact["operator_guidance"]["guidance_id"] == "guidance_test", stage
        assert compact["operator_guidance"]["summary"] == "保留当前主信号，只调整归一化。", stage
        if svc._ORCHESTRATOR_STAGE_CONTEXT_POLICY[stage]["research_space"] == "none":
            assert "research_space" not in compact, stage

    digest = svc._orchestrator_prompt_digest(
        {"context_pack": {"active_context": {"operator_guidance": guidance}}}
    )
    assert digest["operator_guidance"]["guidance_id"] == "guidance_test"
    assert digest["operator_guidance"]["summary"] == "保留当前主信号，只调整归一化。"


def test_context_pack_keeps_guidance_out_of_history_and_resolves_one_shot_at_call_boundary(monkeypatch):
    guidance_step = {
        "run_id": "run-guided",
        "round_id": "run-guided:r0001",
        "stage_id": "run-guided:r0001:s98_human_guidance",
        "stage": "human_guidance",
        "ts": "2026-07-13T10:00:00",
        "summary": "保留主信号，避免只改窗口。",
        "extra": {"guidance_id": "guidance_live", "author": "web_gui"},
    }
    monkeypatch.setattr(svc, "factor_tool_context", lambda **kwargs: ok_result(outputs={}))
    monkeypatch.setattr(
        svc,
        "_read_recent_research_steps",
        lambda limit=20, run_id=None: [guidance_step] if run_id == "run-guided" else [],
    )
    monkeypatch.setattr(svc, "_recent_orchestrator_anchors", lambda limit=8: [])
    monkeypatch.setattr(svc, "_recent_orchestrator_failure_feedback", lambda: {})
    monkeypatch.setattr(svc, "_fetch_quantgpt_recent_tasks", lambda **kwargs: [])
    monkeypatch.setattr(svc, "_quantgpt_task_summary", lambda tasks: {})

    pack = svc._build_orchestrator_context_pack(
        run_id="run-guided",
        round_id="run-guided:r0001",
        stage="score_review",
        contract={},
        round_events=[],
    )

    assert not pack["active_context"].get("operator_guidance")
    assert all(step.get("stage") != "human_guidance" for step in pack["recent_steps"])

    effective = svc._context_pack_with_pending_operator_guidance(pack, run_id="run-guided")
    guidance = effective["active_context"]["operator_guidance"]
    assert guidance["guidance_id"] == "guidance_live"
    assert guidance["author"] == "web_gui"
    assert guidance["summary"] == "保留主信号，避免只改窗口。"
    assert guidance["scope"] == "one_shot_next_llm_judgment"


def test_context_pack_scopes_quantgpt_summary_to_current_run(monkeypatch):
    monkeypatch.setattr(svc, "factor_tool_context", lambda **kwargs: ok_result(outputs={}))
    monkeypatch.setattr(svc, "_read_recent_research_steps", lambda limit=20, run_id=None: [])
    monkeypatch.setattr(svc, "_recent_orchestrator_anchors", lambda limit=8: [])
    monkeypatch.setattr(svc, "_recent_orchestrator_failure_feedback", lambda: {})
    monkeypatch.setattr(
        svc,
        "_fetch_quantgpt_recent_tasks",
        lambda **kwargs: [
            {"task_id": "old", "run_id": "run-old", "task_type": "score", "status": "completed"},
            {"task_id": "current", "run_id": "run-current", "task_type": "score", "status": "completed"},
        ],
    )

    pack = svc._build_orchestrator_context_pack(
        run_id="run-current",
        round_id="run-current:r0001",
        stage="thesis_design",
        contract={},
        round_events=[],
    )

    assert pack["quantgpt_summary"]["total"] == 1
    assert pack["quantgpt_summary"]["latest_task"]["task_id"] == "current"


def test_operator_guidance_is_consumed_after_first_llm_delivery_and_older_guidance_does_not_resurface(monkeypatch):
    old_guidance = {
        "run_id": "run-guided",
        "stage_id": "run-guided:r0001:s97_human_guidance",
        "stage": "human_guidance",
        "ts": "2026-07-13T09:59:00",
        "summary": "旧干预",
        "extra": {"guidance_id": "guidance_old"},
    }
    latest_guidance = {
        "run_id": "run-guided",
        "stage_id": "run-guided:r0001:s98_human_guidance",
        "stage": "human_guidance",
        "ts": "2026-07-13T10:00:00",
        "summary": "只影响下一次判断",
        "extra": {"guidance_id": "guidance_latest"},
    }
    records = [latest_guidance, old_guidance]
    monkeypatch.setattr(svc, "_read_recent_research_steps", lambda limit=20, run_id=None: list(records))

    pending = svc._latest_pending_operator_guidance("run-guided")
    assert pending["guidance_id"] == "guidance_latest"

    records.insert(
        0,
        {
            "run_id": "run-guided",
            "stage": "score_review",
            "evidence_refs": [
                {
                    "type": "operator_guidance_delivery",
                    "guidance_id": "guidance_latest",
                    "guidance_stage_id": latest_guidance["stage_id"],
                }
            ],
        },
    )
    assert svc._latest_pending_operator_guidance("run-guided") == {}
    effective = svc._context_pack_with_pending_operator_guidance(
        {"active_context": {"operator_guidance": {"guidance_id": "stale"}}},
        run_id="run-guided",
    )
    assert "operator_guidance" not in effective["active_context"]


def test_stage_call_delivers_guidance_once_then_removes_it_from_next_payload(monkeypatch):
    guidance = {
        "run_id": "run-guided",
        "round_id": "run-guided:r0001",
        "stage_id": "run-guided:r0001:s98_human_guidance",
        "stage": "human_guidance",
        "ts": "2026-07-13T10:00:00",
        "summary": "下一次判断保留主信号",
        "extra": {"guidance_id": "guidance_once"},
    }
    records = [guidance]
    payloads = []
    monkeypatch.setattr(svc, "_read_recent_research_steps", lambda limit=20, run_id=None: list(records))

    def fake_complete(**kwargs):
        payload = kwargs["payload"]
        payloads.append(payload)
        delivered = payload.get("context_pack", {}).get("active_context", {}).get("operator_guidance", {})
        if delivered:
            records.insert(
                0,
                {
                    "run_id": "run-guided",
                    "stage": kwargs["stage"],
                    "evidence_refs": [
                        {
                            "type": "operator_guidance_delivery",
                            "guidance_id": delivered["guidance_id"],
                            "guidance_stage_id": delivered["stage_id"],
                        }
                    ],
                },
            )
        return {}

    monkeypatch.setattr(svc, "_complete_orchestrator_llm_json", fake_complete)
    monkeypatch.setattr(svc, "_validate_orchestrator_stage_result", lambda *args, **kwargs: None)
    context_pack = {
        "run_state": {"run_id": "run-guided", "round_id": "run-guided:r0001", "contract": {}},
        "active_context": {},
        "recent_steps": [],
    }

    svc._complete_orchestrator_stage_json(
        client=object(),
        run_id="run-guided",
        round_id="run-guided:r0001",
        stage="score_review",
        context_pack=context_pack,
        stage_input={},
    )
    svc._complete_orchestrator_stage_json(
        client=object(),
        run_id="run-guided",
        round_id="run-guided:r0001",
        stage="novelty_review",
        context_pack=context_pack,
        stage_input={},
    )

    first = payloads[0]["context_pack"]["active_context"]["operator_guidance"]
    assert first["guidance_id"] == "guidance_once"
    assert not payloads[1]["context_pack"].get("active_context", {}).get("operator_guidance")


def test_one_shot_guidance_consumption_round_trips_through_real_research_step_journal(monkeypatch, tmp_path):
    steps_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)
    steps_file.parent.mkdir(parents=True, exist_ok=True)
    guidance = {
        "schema_version": "research_step_v2",
        "ts": "2026-07-13T10:00:00",
        "run_id": "run-journal",
        "round_id": "run-journal:r0001",
        "stage_id": "run-journal:r0001:s98_human_guidance",
        "stage": "human_guidance",
        "summary": "仅干预下一次判断",
        "decision": "recorded",
        "extra": {"guidance_id": "guidance_journal", "author": "test"},
    }
    steps_file.write_text(json.dumps(guidance, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(svc, "_charge_orchestrator_llm_budget", lambda *args, **kwargs: {})
    monkeypatch.setattr(svc, "_validate_orchestrator_stage_result", lambda *args, **kwargs: None)

    class FakeClient:
        base_url = "http://fake"
        timeout = 1
        model = "fake-model"

        def __init__(self):
            self.payloads = []

        def preferred_model(self):
            return self.model

        def model_order(self):
            return [self.model]

        def complete_json(self, **kwargs):
            self.payloads.append(kwargs["payload"])
            return {}

    client = FakeClient()
    context_pack = {
        "run_state": {"run_id": "run-journal", "round_id": "run-journal:r0001", "contract": {}},
        "active_context": {},
        "recent_steps": [],
    }
    svc._complete_orchestrator_stage_json(
        client=client,
        run_id="run-journal",
        round_id="run-journal:r0001",
        stage="score_review",
        context_pack=context_pack,
        stage_input={},
    )
    svc._complete_orchestrator_stage_json(
        client=client,
        run_id="run-journal",
        round_id="run-journal:r0001",
        stage="novelty_review",
        context_pack=context_pack,
        stage_input={},
    )

    assert client.payloads[0]["context_pack"]["active_context"]["operator_guidance"]["guidance_id"] == "guidance_journal"
    assert not client.payloads[1]["context_pack"].get("active_context", {}).get("operator_guidance")
    journal = [json.loads(line) for line in steps_file.read_text(encoding="utf-8").splitlines()]
    deliveries = [
        ref
        for step in journal
        for ref in (step.get("evidence_refs") or [])
        if ref.get("type") == "operator_guidance_delivery"
    ]
    assert len(deliveries) == 1
    assert deliveries[0]["guidance_id"] == "guidance_journal"


def test_compact_stage_history_never_replays_human_guidance():
    history = svc._compact_stage_history(
        {
            "run_state": {"run_id": "run-guided"},
            "active_context": {},
            "recent_steps": [
                {
                    "run_id": "run-guided",
                    "stage_id": "run-guided:r0001:s98_human_guidance",
                    "stage": "human_guidance",
                    "summary": "不要把本条再次喂给模型",
                    "decision": "Operator guidance recorded.",
                    "stage_transition": {"judgment": "one shot"},
                },
                {
                    "run_id": "run-guided",
                    "stage_id": "run-guided:r0001:s05_score_review",
                    "stage": "score_review",
                    "summary": "score review complete",
                    "decision": "mutate",
                    "stage_transition": {"judgment": "return expression"},
                },
            ],
        },
        stage="expression_design",
    )
    assert "不要把本条再次喂给模型" not in json.dumps(history, ensure_ascii=False)


def test_orchestrator_context_pack_preserves_run_pinned_factor_map():
    pack = OrchestratorContextPack(
        run_id="run-audit",
        round_id="run-audit:r0001",
        stage="candidate_plan",
        contract={},
        active_context={
            "factor_map_context": {
                "available": True,
                "map_id": "fm_pinned",
                "audit_id": "fa_pinned",
                "regions": [],
            },
            "operator_guidance": {"summary": "keep signal direction"},
            "orchestrator_contract": {"candidate_plan_code_precheck": {"pure_code": True}},
        },
        recent_steps=[],
        quantgpt_summary={},
        round_events=[],
    ).to_dict()

    assert pack["active_context"]["factor_map_context"]["available"] is True
    assert pack["active_context"]["factor_map_context"]["map_id"] == "fm_pinned"
    assert pack["active_context"]["factor_map_context"]["audit_id"] == "fa_pinned"
    assert pack["active_context"]["operator_guidance"]["summary"] == "keep signal direction"
    assert pack["active_context"]["orchestrator_contract"]["candidate_plan_code_precheck"]["pure_code"] is True


def test_expression_and_round_synthesis_payloads_share_exact_operator_contract():
    expression = svc._compact_stage_tool_evidence_for_prompt(
        stage="expression_design",
        stage_input={"operator_list_summary": {"supported_operators": ["ts_av_diff", "ts_std"]}},
    )
    synthesis = svc._compact_stage_tool_evidence_for_prompt(
        stage="round_synthesis",
        stage_input={"reason": "test"},
    )

    assert expression["operator_list_summary"]["operator_signatures"]["ts_av_diff"] == "ts_av_diff(x, window)"
    assert expression["operator_list_summary"]["operator_signatures"]["ts_std"] == "ts_std(x, window)"
    assert synthesis["operator_contract"]["signatures"]["ts_av_diff"] == "ts_av_diff(x, window)"
    assert "ts_stddev" not in synthesis["operator_contract"]["signatures"]


def test_expression_prompt_carries_grouped_prior_run_history_for_duplicate_avoidance():
    history = svc._prior_round_expression_history(
        {
            "expr-1": {
                "round_id": "run-a:r0001",
                "candidate_id": "c1",
                "expression": "rank(ts_delta(close, 5))",
            }
        }
    )
    expression = svc._compact_stage_tool_evidence_for_prompt(
        stage="expression_design",
        stage_input={"prior_expression_history": history},
    )

    prompt_history = expression["prior_expression_history"]
    seen = prompt_history["exact_do_not_repeat"][0]["candidates"][0]
    assert seen["candidate_id"] == "c1"
    assert seen["expression"] == "rank(ts_delta(close, 5))"
    assert "operators" not in seen
    assert "windows" not in seen
    assert prompt_history["policy"]["code_precheck_remains_authoritative"] is True


def test_expression_prompt_treats_candidate_count_as_maximum_compute_budget():
    expression = svc._compact_stage_tool_evidence_for_prompt(
        stage="expression_design",
        stage_input={
            "candidate_budget": {
                "maximum_score_candidates": 10,
                "minimum_candidates": 1,
                "must_fill": False,
                "policy": "maximum_is_compute_budget_not_output_target",
            }
        },
    )

    assert expression["candidate_budget"]["maximum_score_candidates"] == 10
    assert expression["candidate_budget"]["must_fill"] is False
    assert "requested_candidate_count" not in expression


def test_library_information_prompt_keeps_one_proven_representative_without_active_count():
    regions = [
        {
            "cluster_id": f"information_{idx:03d}",
            "region_uid": f"region_{idx}",
            "size": 2,
            "representative": {
                "factor_id": f"f{idx}",
                "name": f"factor-{idx}",
                "expression": f"rank(ts_mean(close,{idx + 1}))",
            },
            "members": [
                {"factor_id": f"f{idx}", "name": f"factor-{idx}", "expression": f"rank(ts_mean(close,{idx + 1}))"},
                {"factor_id": f"m{idx}", "name": f"member-{idx}", "expression": f"rank(ts_mean(amount,{idx + 1}))"},
            ],
        }
        for idx in range(15)
    ]
    source = {"available": True, "audit_id": "fa-ownership", "regions": regions}

    design = svc._compact_library_information_context(source, stage="expression_design", family_limit=None)
    plan = svc._compact_library_information_context(source, stage="candidate_plan", family_limit=None)

    assert len(design["regions"]) == 15
    assert len(plan["regions"]) == 15
    for region in design["regions"]:
        assert "representative" not in region
        assert "members" not in region
        assert region["representative_factor"]["expression"].startswith("rank(ts_mean(close,")
        assert "active_factor_count" not in region
        assert set(region) <= {
            "region_id",
            "region_uid",
            "name",
            "core_fields",
            "core_structures",
            "combination_form",
            "representative_factor",
            "current_run_trajectory",
            "guidance",
        }


def test_hypothesis_factor_map_filters_regions_by_current_thesis_fields():
    source = {
        "available": True,
        "map_id": "fm-related",
        "regions": [
            {
                "region_uid": "region_value",
                "size": 1,
                "representative": {
                    "factor_id": "f-value",
                    "name": "LowPs",
                    "expression": "rank(-ps_ttm)",
                    "admission_score": 81.0,
                },
            },
            {
                "region_uid": "region_flow",
                "size": 1,
                "representative": {
                    "factor_id": "f-flow",
                    "name": "MainFlow",
                    "expression": "rank(ts_mean(net_mf_amount,10))",
                    "admission_score": 79.0,
                },
            },
        ],
    }
    payload = svc._orchestrator_stage_payload(
        stage="hypothesis_design",
        context_pack={
            "run_state": {"run_id": "run-related", "contract": {"direction": "auto"}},
            "active_context": {
                "factor_map_context": source,
                "field_context": {
                    "supported_fields": ["ps_ttm", "net_mf_amount"],
                    "supported_operators": ["rank", "ts_mean"],
                },
            },
            "recent_steps": [],
        },
        lineage_context={
            "current_thesis": [
                {
                    "thesis_id": "t1",
                    "preferred_data_families": ["ps_ttm"],
                    "expected_alpha_mechanism": "低估值修复",
                }
            ]
        },
        stage_input={"field_context": {"supported_fields": ["ps_ttm"]}},
    )

    regions = payload["context_pack"]["active_context"]["factor_map_context"]["regions"]
    assert [item["region_uid"] for item in regions] == ["region_value"]
    assert regions[0]["representative_factor"]["name"] == "LowPs"
    assert "active_factor_count" not in json.dumps(regions, ensure_ascii=False)


def test_truncated_factor_map_excludes_relations_annotations_and_raw_activity():
    source = {
        "available": True,
        "regions": [
            {"cluster_id": "information_001", "region_uid": "region_one", "representative": {"factor_id": "f1", "expression": "rank(close)"}},
            {"cluster_id": "information_002", "region_uid": "region_two", "representative": {"factor_id": "f2", "expression": "rank(amount)"}},
            {"cluster_id": "information_003", "region_uid": "region_three", "representative": {"factor_id": "f3", "expression": "rank(volume)"}},
        ],
        "region_relations": [
            {"source_region_uid": "region_one", "target_region_uid": "region_two", "dependency_score": 0.3},
            {"source_region_uid": "region_one", "target_region_uid": "region_three", "dependency_score": 0.4},
        ],
        "region_activity": {
            "region_one": {"observation_count": 1},
            "region_two": {"observation_count": 2},
            "region_three": {"observation_count": 3},
        },
        "governed_annotations": [
            {"annotation_id": "a1", "region_uid": "region_one", "prompt_eligible": True},
            {"annotation_id": "a3", "region_uid": "region_three", "prompt_eligible": True},
        ],
    }

    compact = svc._compact_library_information_context(
        source,
        stage="expression_design",
        family_limit=2,
    )

    assert [item["region_uid"] for item in compact["regions"]] == [
        "region_one",
        "region_two",
    ]
    assert "region_relations" not in compact
    assert "region_activity" not in compact
    assert "governed_annotations" not in compact


def test_candidate_plan_prompt_owns_candidates_and_protected_parents_once():
    candidate = {
        "candidate_id": "c1",
        "hypothesis_id": "h1",
        "expression": "rank(ts_delta(close,6))",
        "parent_candidate_id": "r0004:c3",
        "mutation_summary": "change confirmation normalization",
    }
    payload = svc._orchestrator_stage_payload(
        stage="candidate_plan",
        context_pack={"run_state": {}, "active_context": {}, "recent_steps": []},
        lineage_context={"parent_candidates": [candidate]},
        stage_input={
            "candidates": [candidate],
            "code_precheck": [],
            "protected_parent_mutation_candidate_ids": ["c1"],
        },
    )

    context = payload["context_pack"]
    assert "candidate_drafts" not in context.get("current_round_context", {})
    assert context["tool_evidence"]["candidates"][0]["candidate_id"] == "c1"
    assert context["tool_evidence"]["protected_parent_mutation_candidate_ids"] == ["c1"]


def test_expression_lineage_preserves_all_three_theses_and_four_hypotheses():
    lineage = {
        "current_thesis": [
            {
                "thesis_id": f"t{i}",
                "economic_rationale": f"thesis {i}",
                "expected_alpha_mechanism": f"mechanism {i}",
                "preferred_data_families": [f"field_{i}"],
            }
            for i in range(1, 4)
        ],
        "current_hypothesis": [
            {
                "hypothesis_id": f"h{i}",
                "thesis_id": f"t{min(i, 3)}",
                "signal_claim": f"claim {i}",
                "expected_direction": "positive",
            }
            for i in range(1, 5)
        ],
    }

    compact = svc._compact_lineage_context_for_prompt(lineage, stage="expression_design")

    assert [item["thesis_id"] for item in compact["current_thesis"]] == ["t1", "t2", "t3"]
    assert [item["hypothesis_id"] for item in compact["current_hypothesis"]] == ["h1", "h2", "h3", "h4"]


def test_score_review_lineage_does_not_drop_later_thesis_branches():
    lineage = {
        "current_thesis": [{"thesis_id": f"t{i}"} for i in range(1, 4)],
        "current_hypothesis": [
            {"hypothesis_id": f"h{i}", "thesis_id": f"t{min(i, 3)}"}
            for i in range(1, 5)
        ],
    }

    compact = svc._compact_lineage_context_for_prompt(lineage, stage="score_review")

    assert len(compact["current_thesis"]) == 3
    assert len(compact["current_hypothesis"]) == 4


def test_active_context_family_representatives_group_window_variants():
    summary = _compact_active_factor_summary(
        {
            "active_factors": [
                {"name": "Amount5", "expression": "rank(ts_mean(amount, 5))"},
                {"name": "Amount20", "expression": "rank(ts_mean(amount, 20))"},
            ]
        }
    )

    reps = summary["family_representatives"]
    assert reps[0]["count"] == 2
    assert reps[0]["family_summary"] == "fields: amount; operators: rank, ts_mean"
    assert reps[0]["fields_used"] == ["amount"]
    assert reps[0]["operators_used"] == ["rank", "ts_mean"]
    assert reps[0]["window_lengths"] == [5, 20]
    assert len(reps[0]["representatives"]) == 2


def test_novelty_advice_blocks_crowded_family():
    advice = orchestrator.novelty_advice(
        [
            {
                "expression": "rank(close)",
                "novelty_guard": {
                    "allowed": False,
                    "max_existing_pearson": 0.82,
                    "max_existing_rank_corr": 0.65,
                    "p90_pearson": 0.76,
                    "p90_rank_corr": 0.60,
                    "thresholds": {"pearson": 0.75, "rank_corr": 0.80, "p90_pearson": 0.70, "p90_rank_corr": 0.75},
                },
            }
        ]
    )

    assert advice["action"] == "orthogonalize_or_switch_source"
    assert advice["strategy"] == "exploit"
    assert advice["candidate_lane_decisions"][0]["reason"] == "family_crowded_p90_threshold"
    assert "fxalpha_quality_gate" in advice["blocked_actions"]


def test_novelty_history_switches_repeated_rejected_family_without_blocking_keeper():
    history = [
        {
            "candidate_id": "old1",
            "action": "orthogonalize_or_switch_source",
            "matched_region_uid": "region_x",
        },
    ]
    rejected = orchestrator.novelty_advice(
        [
            {
                "candidate_id": "new_reject",
                "novelty_guard": {
                    "allowed": False,
                    "matched_region_uid": "region_x",
                },
            }
        ],
        history=history,
    )
    keeper = orchestrator.novelty_advice(
        [
            {
                "candidate_id": "new_keeper",
                "novelty_guard": {
                    "allowed": True,
                    "matched_region_uid": "region_x",
                },
            }
        ],
        history=history,
    )

    rejected_lane = rejected["candidate_lane_decisions"][0]
    keeper_lane = keeper["candidate_lane_decisions"][0]
    assert rejected_lane["action"] == "explore_new_thesis"
    assert rejected["action"] == "explore_new_thesis"
    assert rejected["strategy"] == "explore"
    assert rejected_lane["prior_family_rejections"] == 1
    assert keeper_lane["action"] == "advance_to_deep_validation"
    assert keeper_lane["repeated_same_family"] is True


def test_run_novelty_history_reads_persisted_candidate_advice(monkeypatch):
    steps = [
        {
            "ts": "2026-07-24T01:00:00",
            "run_id": "run1",
            "round_id": "run1:r0001",
            "stage": "novelty_review",
            "stage_seq": 7,
            "evidence_refs": [
                {
                    "type": "advice_summary",
                    "candidate_lane_decisions": [
                        {
                            "candidate_id": "c1",
                            "expression": "rank(close)",
                            "action": "orthogonalize_or_switch_source",
                            "reason": "active_pool_correlation_threshold",
                            "matched_region_uid": "region_x",
                        }
                    ],
                }
            ],
        }
    ]
    monkeypatch.setattr(
        svc,
        "_read_recent_research_steps",
        lambda limit=20, run_id=None: list(steps),
    )

    history = svc._orchestrator_run_novelty_history(run_id="run1")

    assert history == [
        {
            "run_id": "run1",
            "round_id": "run1:r0001",
            "candidate_id": "c1",
            "expression": "rank(close)",
            "action": "orthogonalize_or_switch_source",
            "reason": "active_pool_correlation_threshold",
            "matched_region_uid": "region_x",
            "matched_information_cluster_id": None,
            "matched_existing_factor_id": None,
            "ts": "2026-07-24T01:00:00",
        }
    ]


def test_novelty_advice_rejects_st_exposure_before_deep():
    advice = orchestrator.novelty_advice(
        [
            {
                "candidate_id": "c1",
                "expression": "rank(close)",
                "novelty_guard": {
                    "allowed": True,
                    "reason": "novel_increment",
                    "novelty_score": 0.5,
                    "thresholds": {"pearson": 0.75, "rank_corr": 0.80},
                },
                "st_exposure_guard": {
                    "available": True,
                    "passed": False,
                    "mode": "hard",
                    "reason": "st_exposure_veto:avg_top50_ratio_ge_0_05",
                    "avg_top50_ratio": 0.12,
                    "p95_top50_ratio": 0.24,
                },
                "combined_guard": {
                    "allowed": False,
                    "reason": "st_exposure_veto:avg_top50_ratio_ge_0_05",
                    "novelty_allowed": True,
                    "st_exposure_passed": False,
                },
            }
        ]
    )

    assert advice["action"] == "reject_st_exposure"
    assert advice["candidate_lane_decisions"][0]["action"] == "reject_st_exposure"
    assert "run_backtest" not in advice["allowed_actions"]


def test_novelty_advice_treats_advisory_st_exposure_as_risk_tag():
    advice = orchestrator.novelty_advice(
        [
            {
                "candidate_id": "c1",
                "expression": "rank(close)",
                "novelty_guard": {
                    "allowed": True,
                    "reason": "novel_increment",
                    "novelty_score": 0.5,
                    "thresholds": {"pearson": 0.75, "rank_corr": 0.80},
                },
                "st_exposure_guard": {
                    "available": True,
                    "passed": False,
                    "mode": "advisory",
                    "reason": "st_exposure_veto:avg_top50_ratio_ge_0_05",
                    "advisory_flag": "distress_proxy_exposure",
                },
                "combined_guard": {
                    "allowed": True,
                    "reason": "novelty_passed_st_exposure_advisory",
                    "novelty_allowed": True,
                    "st_exposure_passed": False,
                    "st_exposure_mode": "advisory",
                },
            }
        ]
    )

    assert advice["action"] == "advance_to_deep_validation"
    assert advice["candidate_lane_decisions"][0]["action"] == "advance_to_deep_validation"
    assert "run_backtest" in advice["allowed_actions"]


def test_deep_advice_treats_autocorrelation_as_diagnostic_only():
    candidate = {
        "expression": "rank(close)",
        "quick_score": 88,
        "backtest_summary": {"ic_mean": 0.03, "ic_ir": 0.4},
        "novelty_guard": {"allowed": True, "novelty_score": 0.5},
        "anti_overfit": {"score": 100, "autocorrelation": {"stock_lag1_mean": 0.98, "stock_lag5_mean": 0.84}},
        "rolling_validation": {"status": "ok", "score": 80, "summary": {"n_windows": 3}, "windows": [{"test_ic": 0.04}]},
        "adversarial_validation": {"score": 100},
        "holding_period_days": 5,
    }

    advice = orchestrator.deep_advice([candidate])

    assert advice["action"] == "submit_quality_gate"
    assert advice["allowed_actions"] == ["fxalpha_quality_gate"]
    assert "risk_flag" not in advice["candidate_lane_decisions"][0]


def test_gate_advice_is_quality_control_not_business_review():
    candidate = {
        "expression": "rank(close)",
        "quick_score": 88,
        "backtest_summary": {"ic_mean": 0.01, "ic_ir": 0.4},
        "novelty_guard": {"allowed": True, "novelty_score": 0.5},
        "anti_overfit": {"score": 100},
        "adversarial_validation": {"score": 100},
        "gate_result": {"passed": False, "reason": "ic_below_threshold", "deep_score": 85},
    }

    advice = orchestrator.gate_advice([candidate])

    assert advice["action"] == "gate_mismatch_feedback"
    assert advice["candidate_lane_decisions"][0]["reason"] == "business_rejection_should_have_been_caught_by_deep_advice"


def test_gate_advice_uses_canonical_import_action_for_adopted_candidate():
    advice = orchestrator.gate_advice(
        [
            {
                "candidate_id": "c1",
                "expression": "rank(close)",
                "gate_result": {"passed": True, "reason": "quality_gate_adopted"},
            }
        ]
    )

    assert advice["action"] == "import"
    assert advice["candidate_lane_decisions"][0]["action"] == "import"
    assert advice["allowed_actions"] == ["fxalpha_import_factors"]


def test_llm_allowed_candidates_accepts_legacy_import_action_alias():
    candidates = [{"candidate_id": "c1", "expression": "rank(close)", "gate_result": {"passed": True}}]
    result = {"candidate_decisions": [{"candidate_id": "c1", "action": "import_adopted_candidate"}]}

    selected = svc._llm_allowed_candidates(candidates, result, allow_actions={"import"})

    assert selected == candidates


def test_import_selection_requires_official_gate_passed_even_if_llm_requests_import():
    adopted = [
        {"candidate_id": "c1", "expression": "rank(close)", "gate_result": {"passed": True}},
        {"candidate_id": "c2", "expression": "rank(open)", "gate_result": {"passed": False, "reason": "quality_gate_rejected"}},
        {"candidate_id": "c3", "expression": "rank(high)"},
    ]
    gate_review = {
        "candidate_decisions": [
            {"candidate_id": "c1", "action": "import"},
            {"candidate_id": "c2", "action": "import"},
            {"candidate_id": "c3", "action": "import"},
        ]
    }

    official = svc._official_gate_import_candidates(adopted)
    selected = svc._llm_allowed_candidates(official, gate_review, allow_actions={"import"})

    assert [item["candidate_id"] for item in official] == ["c1"]
    assert [item["candidate_id"] for item in selected] == ["c1"]


def test_deep_llm_submit_cannot_bypass_code_gate_ready():
    weak_candidate = {
        "candidate_id": "c1",
        "expression": "rank(close)",
        "quick_score": 50,
        "backtest_summary": {"ic_mean": 0.001, "ic_ir": 0.1},
        "novelty_guard": {"allowed": True, "novelty_score": 0.4},
        "anti_overfit": {"score": 60, "autocorrelation": {"stock_lag1_mean": 0.98}},
        "rolling_validation": {"status": "ok", "score": 60, "summary": {"n_windows": 3}, "windows": [{"test_ic": 0.02}]},
        "adversarial_validation": {"score": 60},
    }
    advice = orchestrator.deep_advice([weak_candidate])
    code_gate_ready = [
        candidate
        for candidate, lane in zip([weak_candidate], advice["candidate_lane_decisions"])
        if lane.get("action") == "submit_quality_gate"
    ]
    llm_review = {"candidate_decisions": [{"candidate_id": "c1", "action": "submit_quality_gate"}]}

    selected = svc._llm_allowed_candidates(code_gate_ready, llm_review, allow_actions={"submit_quality_gate"})

    assert code_gate_ready == []
    assert selected == []


def test_deep_code_gate_ready_cannot_be_silently_dropped_by_llm_review():
    code_gate_ready = [
        {
            "candidate_id": "c1",
            "expression": "rank(ts_mean(net_mf_amount,5)) * rank(-pb) * rank(-ts_rank(pb,20))",
        }
    ]
    llm_review = {"candidate_decisions": [{"candidate_id": "c1", "action": "complete_deep_evidence"}]}

    selected, llm_selected = svc._code_authoritative_gate_candidates(code_gate_ready, llm_review)

    assert selected == code_gate_ready
    assert llm_selected == []


def test_deep_prompt_compaction_preserves_current_hard_evidence():
    payload = {
        "candidates": [
            {
                "candidate_id": "c2",
                "factor_name": "NetMfAmount_PbHistLow_Resonance",
                "expression": "rank(ts_mean(net_mf_amount,5)) * rank(-pb) * rank(-ts_rank(pb,20))",
                "score": 77.1,
                "grade": "B",
            }
        ],
        "evidence_refs": [
            {
                "candidate_id": "c2",
                "tool": "deep_validation",
                "quick_score": 77.1,
                "ic": 0.0361,
                "icir": 0.3924,
                "deep_score": 82.1,
                "deep_action": "submit_quality_gate",
                "anti_overfit_score": 95.2,
                "adversarial_score": 73.2,
                "novelty_score": 0.3627,
            }
        ],
    }

    compact = svc._compact_deep_result_payload(payload)
    ref = compact["evidence_refs"][0]

    assert ref["candidate_id"] == "c2"
    assert ref["tool"] == "deep_validation"
    assert ref["icir"] == 0.3924
    assert ref["deep_score"] == 82.1
    assert ref["anti_overfit_score"] == 95.2
    assert ref["adversarial_score"] == 73.2


def test_deep_advice_prompt_summary_keeps_candidate_identity():
    candidate = {
        "candidate_id": "c2",
        "expression": "rank(ts_mean(net_mf_amount,5)) * rank(-pb) * rank(-ts_rank(pb,20))",
        "quick_score": 79.1,
        "backtest_summary": {"ic_mean": 0.0361, "ic_ir": 0.3924},
        "novelty_guard": {"allowed": True, "novelty_score": 0.3627},
        "anti_overfit": {"score": 95.2},
        "rolling_validation": {"status": "ok", "score": 76.0, "summary": {"n_windows": 3}, "windows": [{"test_ic": 0.04}]},
        "adversarial_validation": {"score": 73.2},
    }

    advice = orchestrator.deep_advice([candidate])
    compact = svc._compact_prompt_advice(advice)
    lane = compact["candidate_lane_decisions"][0]

    assert lane["candidate_id"] == "c2"
    assert lane["action"] == "submit_quality_gate"
    assert lane["deep_score"] >= 80


def test_review_code_advice_keeps_candidate_identity_across_stages():
    candidate = {
        "candidate_id": "c7",
        "expression": "rank(close)",
        "score": 71,
        "grade": "B",
        "novelty_guard": {"allowed": True, "novelty_score": 0.41},
        "gate_result": {"passed": True, "reason": "quality_gate_adopted"},
    }

    for advice in (
        orchestrator.quick_advice([candidate]),
        orchestrator.novelty_advice([candidate]),
        orchestrator.gate_advice([candidate]),
    ):
        lane = svc._compact_prompt_advice(advice)["candidate_lane_decisions"][0]
        assert lane["candidate_id"] == "c7"


def test_llm_candidate_decision_compaction_preserves_action_reason_and_identity():
    compact = svc._compact_tool_evidence_leaf(
        [
            {
                "candidate_id": "c2",
                "expression": "rank(ts_mean(net_mf_amount,5)) * rank(-pb)",
                "action": "complete_deep_evidence",
                "reason": "missing anti/adversarial evidence",
                "weakest_component": "evidence_missing",
            }
        ]
    )

    lane = compact[0]
    assert lane["candidate_id"] == "c2"
    assert lane["action"] == "complete_deep_evidence"
    assert lane["reason"] == "missing anti/adversarial evidence"
    assert lane["weakest_component"] == "evidence_missing"


def test_event_prompt_compaction_preserves_deep_review_hard_evidence():
    compact = svc._compact_event_for_prompt(
        {
            "stage": "deep_validation_review",
            "summary": "deep completed",
            "decision": "submit_quality_gate",
            "stage_transition": {"next_stage": "import_gate_review", "next_action": "run_quality_gate"},
            "evidence_refs": [
                {
                    "candidate_id": "c2",
                    "tool": "deep_validation",
                    "quick_score": 77.1,
                    "deep_score": 82.1,
                    "deep_action": "submit_quality_gate",
                    "ic": 0.0361,
                    "icir": 0.3924,
                    "anti_overfit_score": 95.2,
                    "adversarial_score": 73.2,
                    "novelty_score": 0.3627,
                }
            ],
        }
    )

    ref = compact["evidence_refs"][0]
    assert ref["candidate_id"] == "c2"
    assert ref["deep_score"] == 82.1
    assert ref["deep_action"] == "submit_quality_gate"
    assert ref["anti_overfit_score"] == 95.2
    assert ref["adversarial_score"] == 73.2


def test_return_handoff_compaction_preserves_deep_review_hard_evidence():
    compact = svc._compact_return_handoff(
        {
            "from_stage": "deep_validation_review",
            "to_stage": "expression_design",
            "reason": "mutate near miss",
            "supporting_evidence_refs": [
                {
                    "candidate_id": "c2",
                    "tool": "deep_validation",
                    "quick_score": 77.1,
                    "deep_score": 82.1,
                    "deep_action": "submit_quality_gate",
                    "ic": 0.0361,
                    "icir": 0.3924,
                    "anti_overfit_score": 95.2,
                    "adversarial_score": 73.2,
                    "note": "near gate-ready candidate",
                }
            ],
        }
    )

    ref = compact["supporting_evidence_refs"][0]
    assert ref["candidate_id"] == "c2"
    assert ref["deep_score"] == 82.1
    assert ref["deep_action"] == "submit_quality_gate"
    assert ref["anti_overfit_score"] == 95.2
    assert ref["adversarial_score"] == 73.2
    assert ref["note"] == "near gate-ready candidate"


def test_return_handoff_from_stage_uses_scoped_mutation_parent_and_real_narrative():
    handoff = svc._return_handoff_from_stage(
        "deep_validation_review",
        {
            "summary": "深度验证完成，一个候选需要定向变异。",
            "judgment": "候选有快筛信号，但近期稳定性不足。",
            "why": "Quick 71.2、Deep 69.4，Rolling 48.2，暂不送质量门。",
            "candidate_decisions": [
                {"candidate_id": "c1_h1_t1_v8", "action": "mutate"},
                {"candidate_id": "c2_h2_t1_v4", "action": "deep_reject"},
            ],
            "stage_transition": {
                "next_stage": "expression_design",
                "reason": "保留经济机制并重新设计确认关系。",
            },
        },
        evidence_refs=[
            {
                "candidate_id": "c1_h1_t1_v8",
                "tool": "deep_validation",
                "quick_score": 71.2,
                "deep_score": 69.4,
                "deep_action": "targeted_mutation",
            },
            {
                "candidate_id": "c2_h2_t1_v4",
                "tool": "deep_validation",
                "deep_score": 51.0,
                "deep_action": "deep_reject",
            },
        ],
        round_id="fr_test:r0050",
    )

    compact = svc._compact_return_handoff(handoff)
    assert compact["parent_candidate_refs"] == ["r0050:c1_h1_t1_v8"]
    assert compact["reason"] == "保留经济机制并重新设计确认关系。"
    assert compact["summary"] == "深度验证完成，一个候选需要定向变异。"
    assert compact["judgment"] == "候选有快筛信号，但近期稳定性不足。"
    assert compact["why"] == "Quick 71.2、Deep 69.4，Rolling 48.2，暂不送质量门。"
    assert compact["supporting_evidence_refs"][0]["deep_action"] == "targeted_mutation"
    assert "c2_h2_t1_v4" not in compact["parent_candidate_refs"]


def test_exploit_handoff_keeps_only_top_two_scored_parents():
    decisions = [
        {"candidate_id": f"c{i}", "action": "revise_expression"}
        for i in range(1, 5)
    ]
    handoff = svc._return_handoff_from_stage(
        "score_review",
        {
            "candidate_decisions": decisions,
            "stage_transition": {
                "next_stage": "expression_design",
                "reason": "保留最好的少量 parent 做定向变异。",
            },
        },
        evidence_refs=[
            {"candidate_id": f"c{i}", "tool": "score_factor"}
            for i in range(1, 5)
        ],
        round_id="fr_test:r0001",
        code_advice={
            "evolution_strategy": {"strategy": "EXPLOIT", "action": "targeted_mutation"},
            "candidate_lane_decisions": [
                {"candidate_id": "c1", "action": "mutate_nonlinear", "score": 42.1},
                {"candidate_id": "c2", "action": "mutate_nonlinear", "score": 42.8},
                {"candidate_id": "c3", "action": "adjust_window_or_signal_frequency", "score": 54.2},
                {"candidate_id": "c4", "action": "adjust_window_or_signal_frequency", "score": 55.1},
            ],
        },
    )

    compact = svc._compact_return_handoff(handoff)
    assert compact["binding_policy"] == "targeted_parent_mutation"
    assert compact["parent_candidate_refs"] == ["r0001:c3", "r0001:c4"]


def test_return_handoff_from_stage_projects_recombine_code_advice():
    handoff = svc._return_handoff_from_stage(
        "deep_validation_review",
        {
            "summary": "当前 parent 连续下降，改为跨候选重组。",
            "judgment": "不再继续局部微调。",
            "why": "跨候选轨迹显示两个历史候选具有互补证据。",
            "candidate_decisions": [{"candidate_id": "c7", "action": "recombine_from_best"}],
            "stage_transition": {"next_stage": "expression_design", "reason": "执行重组。"},
        },
        evidence_refs=[{"candidate_id": "c7", "tool": "deep_validation"}],
        round_id="fr_test:r0012",
        code_advice={
            "evolution_strategy": {"strategy": "RECOMBINE", "action": "recombine"},
            "recombination_candidates": [{"candidate_id": "r0009:c2"}, {"candidate_id": "r0010:c4"}],
            "candidate_lane_decisions": [
                {
                    "candidate_id": "c7",
                    "action": "recombine_from_best",
                    "evolution_strategy": {"strategy": "RECOMBINE"},
                    "mutation": {"strategy": "mutate_interaction"},
                }
            ],
        },
    )

    compact = svc._compact_return_handoff(handoff)
    assert compact["recommended_mutation"] == "RECOMBINE:mutate_interaction"
    assert compact["parent_candidate_refs"] == [
        "r0012:c7",
        "r0009:c2",
        "r0010:c4",
    ]
    assert "不得继续微调当前 parent" in compact["must_change"][0]


def test_recombine_does_not_mislabel_unscoped_historical_parent_as_current_round():
    handoff = svc._return_handoff_from_stage(
        "deep_validation_review",
        {
            "candidate_decisions": [{"candidate_id": "c7", "action": "recombine_from_best"}],
            "stage_transition": {"next_stage": "expression_design"},
        },
        evidence_refs=[{"candidate_id": "c7", "tool": "deep_validation"}],
        round_id="fr_test:r0012",
        code_advice={
            "evolution_strategy": {"strategy": "RECOMBINE", "action": "recombine"},
            "recombination_candidates": [{"candidate_id": "c4"}],
        },
    )

    compact = svc._compact_return_handoff(handoff)
    assert compact["parent_candidate_refs"] == ["r0012:c7", "c4"]
    assert "r0012:c4" not in compact["parent_candidate_refs"]


def test_return_handoff_from_stage_projects_explore_and_clears_parent_refs():
    handoff = svc._return_handoff_from_stage(
        "novelty_review",
        {
            "summary": "同一关系连续被新颖性否决。",
            "judgment": "应探索新的经济主线。",
            "why": "窗口变化没有改变信息关系。",
            "candidate_decisions": [{"candidate_id": "c2", "action": "return_thesis"}],
            "stage_transition": {"next_stage": "thesis_design", "reason": "返回主线设计。"},
        },
        evidence_refs=[{"candidate_id": "c2", "tool": "novelty_check"}],
        round_id="fr_test:r0013",
        code_advice={
            "evolution_strategy": {"strategy": "EXPLORE", "action": "regenerate_full"},
            "candidate_lane_decisions": [
                {
                    "candidate_id": "c2",
                    "action": "return_thesis_design",
                    "evolution_strategy": {"strategy": "EXPLORE"},
                    "mutation": {"strategy": "regenerate_full"},
                }
            ],
        },
    )

    compact = svc._compact_return_handoff(handoff)
    assert compact["recommended_mutation"] == "EXPLORE:regenerate_full"
    assert compact.get("parent_candidate_refs") in (None, [])
    assert "放弃当前弱机制族" in compact["must_change"][0]


def test_first_novelty_orthogonalization_preserves_quick_keeper_parent():
    handoff = svc._return_handoff_from_stage(
        "novelty_review",
        {
            "summary": "B级候选因与active库相关而未通过新颖性。",
            "judgment": "保留经济机制，只正交化确认关系。",
            "why": "Quick为72.4，正式novelty相关性超过阈值。",
            "candidate_decisions": [
                {
                    "candidate_id": "c3",
                    "action": "orthogonalize_expression",
                    "preserve": "保留主信息腿和已获支持的经济方向。",
                    "change": "只替换造成相关性过高的确认关系。",
                    "avoid": "不得只改窗口或复制原表达式。",
                }
            ],
            "stage_transition": {
                "next_stage": "expression_design",
                "reason": "返回表达式设计执行一次定向正交化。",
            },
        },
        evidence_refs=[
            {
                "candidate_id": "c3",
                "action": "orthogonalize_expression",
                "quick_score": 72.4,
            }
        ],
        round_id="fr_test:r0004",
        code_advice={
            "action": "orthogonalize_or_switch_source",
            # Preserve compatibility with historical traces that incorrectly
            # labeled the top-level novelty strategy as explore.
            "strategy": "explore",
            "candidate_lane_decisions": [
                {
                    "candidate_id": "c3",
                    "action": "orthogonalize_or_switch_source",
                    "reason": "family_crowded_p90_threshold",
                }
            ],
        },
    )

    compact = svc._compact_return_handoff(handoff)
    assert compact["binding_policy"] == "targeted_parent_mutation"
    assert compact["recommended_mutation"] == "EXPLOIT:orthogonalize_or_switch_source"
    assert compact["parent_candidate_refs"] == ["r0004:c3"]
    assert "保留主信息腿" in compact["must_preserve"][0]
    assert "只替换" in compact["must_change"][0]
    assert "不得只改窗口" in compact["must_avoid"][0]


def test_normal_research_history_filters_stale_blockers():
    history = svc._compact_stage_history(
        {
            "active_context": {},
            "recent_steps": [
                {
                    "stage": "blocker",
                    "summary": "Orchestrator stopped.",
                    "decision": "score_tool_system_error:1",
                    "stage_transition": {"next_stage": "blocker", "next_action": "fix_runtime"},
                    "tags": ["orchestrator", "blocker"],
                },
                {
                    "stage": "score_review",
                    "summary": "Quick score 完成。",
                    "decision": "reject_batch",
                    "stage_transition": {
                        "next_stage": "round_synthesis",
                        "next_action": "synthesize_score_failures",
                        "judgment": "A/B=0/5",
                        "why": "weak quick evidence",
                    },
                    "tags": ["orchestrator", "score_review"],
                },
            ],
        },
        stage="thesis_design",
        round_events=[],
    )

    stages = [item["stage"] for item in history["short_term_history"]["stage_relevant_steps"]]
    assert "blocker" not in stages
    assert "score_review" in stages


def test_candidate_plan_code_precheck_flags_mutually_exclusive_cost_product():
    checks = svc._candidate_plan_code_precheck(
        [
            {
                "candidate_id": "c4",
                "expression": "rank(where(close > cost_85pct, -ts_rank(pct_change,5), 0)) * rank(where(close < cost_15pct, ts_rank(pct_change,5), 0))",
            },
            {
                "candidate_id": "c2",
                "expression": "rank(where(close > cost_85pct, -ts_rank(pct_change,5), 0) + where(close < cost_15pct, ts_rank(pct_change,5), 0))",
            },
        ]
    )

    assert [item["candidate_id"] for item in checks] == ["c4"]
    assert checks[0]["fatal"] is True
    assert "mutually_exclusive_cost_branches_multiplied" in checks[0]["warnings"]


def test_candidate_plan_code_precheck_flags_ambiguous_centered_leg_product():
    checks = svc._candidate_plan_code_precheck(
        [
            {
                "candidate_id": "c1",
                "expression": "zscore(ts_rank(ts_sum(lg_net_amount,5),20)) * zscore(rank(-cost_15pct))",
            },
            {
                "candidate_id": "c2",
                "expression": "rank(ts_mean(lg_net_amount,10)) * rank(-cost_15pct)",
            },
        ]
    )

    assert [item["candidate_id"] for item in checks] == ["c1"]
    assert checks[0]["fatal"] is False
    assert any(
        warning.startswith("ambiguous_centered_leg_product:")
        for warning in checks[0]["warnings"]
    )


def test_candidate_plan_code_precheck_catches_production_low_leg_direction_inversion():
    hypotheses = [
        {
            "hypothesis_id": "h1",
            "candidate_variable_groups": [
                {"fields": ["ps_ttm"], "direction": "negative"},
                {"fields": ["net_mf_amount"], "direction": "positive"},
            ],
        },
        {
            "hypothesis_id": "h2",
            "candidate_variable_groups": [
                {"fields": ["borrow_money_bal"], "direction": "negative"},
                {"fields": ["amount"], "direction": "negative"},
            ],
        },
    ]
    checks = svc._candidate_plan_code_precheck(
        [
            {
                "candidate_id": "c1",
                "hypothesis_id": "h1",
                "expression": "rank(-ps_ttm) * rank(ts_mean(net_mf_amount, 10))",
            },
            {
                "candidate_id": "c3",
                "hypothesis_id": "h2",
                "expression": "rank(ts_delta(borrow_money_bal, 10)) * rank(-ts_rank(amount, 20))",
            },
        ],
        hypotheses=hypotheses,
    )

    assert [item["candidate_id"] for item in checks] == ["c3"]
    assert checks[0]["fatal"] is False
    assert checks[0]["instruction"] == "revise_expression_before_score"
    assert checks[0]["warnings"] == [
        "definite_hypothesis_direction_mismatch:"
        "borrow_money_bal:expected_negative:leg_positive"
    ]


def test_candidate_plan_direction_mismatch_cannot_be_scored_even_if_llm_keeps_it():
    candidates = [
        {
            "candidate_id": "c3",
            "hypothesis_id": "h2",
            "expression": "rank(ts_delta(borrow_money_bal, 10)) * rank(-ts_rank(amount, 20))",
        }
    ]
    checks = [
        {
            "candidate_id": "c3",
            "fatal": False,
            "warnings": [
                "definite_hypothesis_direction_mismatch:"
                "borrow_money_bal:expected_negative:leg_positive"
            ],
        }
    ]
    result = {
        "candidate_lanes": [
            {"candidate_id": "c3", "action": "score", "keep": True, "reason": "模型误判方向一致"}
        ]
    }

    svc._enforce_conservative_candidate_plan_lanes(
        result,
        {"candidates": candidates, "code_precheck": checks},
    )

    assert result["candidate_lanes"][0]["action"] == "revise_expression"
    assert result["candidate_lanes"][0]["keep"] is False
    assert svc._candidate_plan_score_candidates(candidates, checks, result) == []


def test_candidate_plan_code_precheck_blocks_wrong_operator_arity_before_score():
    checks = svc._candidate_plan_code_precheck(
        [{"candidate_id": "c1", "expression": "rank(ts_av_diff(close, 5, 20))"}]
    )

    assert checks[0]["candidate_id"] == "c1"
    assert checks[0]["fatal"] is True
    assert any(item.startswith("expression_parser_error:") for item in checks[0]["warnings"])


def test_candidate_plan_code_precheck_blocks_exact_prior_round_expression():
    expression = "rank(ts_delta(close, 5))"
    normalized = svc._normalize_symbolic_expression(expression)
    checks = svc._candidate_plan_code_precheck(
        [{"candidate_id": "c2", "expression": expression}],
        prior_round_expression_refs={
            normalized: {
                "round_id": "run:r0001",
                "candidate_id": "c7",
                "stage_id": "run:r0001:s04_expression_design",
                "expression": "rank(ts_delta(close,5))",
            }
        },
    )

    assert checks[0]["fatal"] is True
    assert any(item.startswith("exact_prior_round_expression:run:r0001:c7") for item in checks[0]["warnings"])
    assert checks[0]["matched_prior_round"]["round_id"] == "run:r0001"


def test_candidate_plan_code_precheck_marks_parameter_only_batch_variant():
    checks = svc._candidate_plan_code_precheck(
        [
            {"candidate_id": "c1", "expression": "rank(-ts_corr(close, turnover_rate, 5))"},
            {"candidate_id": "c2", "expression": "rank(-ts_corr(close, turnover_rate, 10))"},
            {"candidate_id": "c3", "expression": "zscore(-ts_corr(close, turnover_rate, 10))"},
        ]
    )

    assert [item["candidate_id"] for item in checks] == ["c2"]
    assert checks[0]["fatal"] is False
    assert checks[0]["matched_candidate_ids"] == ["c1"]
    assert checks[0]["warnings"] == ["batch_parameter_only_variant:c1"]
    assert checks[0]["instruction"] == "skip_batch_duplicate_unless_evidenced_parent_time_scale_experiment"


def test_parameter_agnostic_key_preserves_digits_inside_field_names():
    high = svc._parameter_agnostic_expression_key("rank(ts_delta(cost_85pct, 5))")
    low = svc._parameter_agnostic_expression_key("rank(ts_delta(cost_15pct, 10))")

    assert high != low
    assert "cost_85pct" in high
    assert "cost_15pct" in low


def test_prior_round_expression_refs_reads_only_earlier_expression_events(monkeypatch):
    records = [
        {
            "run_id": "run",
            "round_id": "run:r0003",
            "stage": "expression_design",
            "candidate_lanes": [{"candidate_id": "current", "expression": "rank(open)"}],
        },
        {
            "run_id": "run",
            "round_id": "run:r0002",
            "stage_id": "run:r0002:s04_expression_design",
            "stage": "expression_design",
            "candidate_lanes": [{"candidate_id": "old", "expression": "rank(ts_mean(close, 5))"}],
        },
        {
            "run_id": "run",
            "round_id": "run:r0001",
            "stage": "score_review",
            "candidate_lanes": [{"candidate_id": "not_expression_stage", "expression": "rank(low)"}],
        },
    ]
    monkeypatch.setattr(svc, "_read_recent_journal_records", lambda **kwargs: (records, {}))

    refs = svc._prior_round_expression_refs("run", "run:r0003")

    assert svc._normalize_symbolic_expression("rank(ts_mean(close,5))") in refs
    assert svc._normalize_symbolic_expression("rank(open)") not in refs
    assert svc._normalize_symbolic_expression("rank(low)") not in refs


def test_candidate_plan_code_precheck_summary_is_llm_ready():
    checks = [
        {
            "candidate_id": "c1",
            "fatal": True,
            "warnings": ["exact_active_expression"],
            "instruction": "drop_candidate",
            "expression": "rank(close)",
        },
    ]

    summary = svc._candidate_plan_code_precheck_summary(checks)

    assert summary["scope"] == "pre_score_schema_and_obvious_expression_error_triage"
    assert summary["policy"] == "pure_code_error_guard_candidate_plan_semantic_budget_final_numeric_novelty_required"
    assert summary["fatal_candidate_ids"] == ["c1"]
    assert summary["soft_candidate_ids"] == []
    assert summary["reason_counts"]["exact_active_expression"] == 1
    assert "symbolic_family" not in summary["items"][0]


def test_candidate_plan_precheck_does_not_guess_active_family_similarity():
    active_summary = _compact_active_factor_summary(
        {
            "active_factor_count": 2,
            "active_factors": [
                {"name": "Amount5", "expression": "rank(ts_mean(amount, 5))"},
                {"name": "Amount20", "expression": "rank(ts_mean(amount, 20))"},
            ],
        }
    )

    checks = svc._candidate_plan_code_precheck(
        [{"candidate_id": "c1", "expression": "rank(ts_mean(amount, 60))"}],
        active_factor_summary=active_summary,
    )

    assert checks == []


def test_candidate_plan_precheck_lanes_preserve_code_block_and_score_states():
    candidates = [
        {"candidate_id": "c1", "expression": "rank(foo)"},
        {"candidate_id": "c2", "expression": "rank(close)"},
        {"candidate_id": "c3", "expression": "rank(open)"},
    ]
    checks = [
        {
            "candidate_id": "c1",
            "fatal": True,
            "warnings": ["unsupported_fields:foo"],
            "instruction": "drop_candidate",
            "symbolic_novelty": {"family_key": "foo|rank"},
        },
    ]

    lanes = svc._candidate_plan_precheck_candidate_lanes(candidates, checks)

    by_id = {item["candidate_id"]: item for item in lanes}
    assert by_id["c1"]["candidate_lane"] == "precheck_blocked"
    assert by_id["c1"]["status"] == "blocked"
    assert by_id["c1"]["precheck_warnings"] == ["unsupported_fields:foo"]
    assert by_id["c2"]["candidate_lane"] == "planned_for_score"
    assert by_id["c3"]["candidate_lane"] == "planned_for_score"
    assert by_id["c3"]["status"] == "planned_for_score"

    nonfatal_lanes = svc._candidate_plan_precheck_candidate_lanes(
        [{"candidate_id": "c2", "expression": "rank(close)"}],
        [],
    )
    assert nonfatal_lanes[0]["candidate_lane"] == "planned_for_score"
    assert nonfatal_lanes[0]["status"] == "planned_for_score"


def test_candidate_plan_contract_requires_lane_coverage():
    result = {
        **_natural_language_summary(),
        "stage": "candidate_plan",
        "decision": "run_batch",
        "judgment": "保留一个候选进入评分。",
        "why": "当前候选覆盖不完整，仍需由契约检查发现缺失项。",
        "history_used": [],
        "candidate_lanes": [{"candidate_id": "c1", "lane": "primary", "keep": True, "reason": "ok"}],
        "next_action": "validate_and_score",
        "stage_transition": {"next_stage": "score_review", "reason": "候选计划完成后进入评分复核。"},
        "confidence": 0.8,
    }

    with pytest.raises(DeepSeekClientError, match="candidate_lane_missing_candidate_ids"):
        svc._validate_orchestrator_stage_result(
            "candidate_plan",
            result,
            stage_input={
                "candidates": [
                    {"candidate_id": "c1", "expression": "rank(close)"},
                    {"candidate_id": "c2", "expression": "rank(open)"},
                ]
            },
        )


def test_stage_contract_requires_model_authored_natural_language_summary():
    result = {
        "stage": "candidate_plan",
        "decision": "run_batch",
        "judgment": "进入评分",
        "why": "候选具备研究价值。",
        "history_used": [],
        "candidate_lanes": [{"candidate_id": "c1", "action": "score", "keep": True, "reason": "合法"}],
        "next_action": "validate_and_score",
        "stage_transition": {"next_stage": "score_review", "reason": "继续"},
        "confidence": 0.8,
    }

    with pytest.raises(DeepSeekClientError, match="missing_required_fields:summary"):
        svc._validate_orchestrator_stage_result("candidate_plan", result)


def test_stage_contract_warns_without_blocking_on_internal_paths_in_natural_language_fields():
    result = {
        **_natural_language_summary(),
        "stage": "candidate_plan",
        "decision": "run_batch",
        "judgment": "进入评分",
        "why": "候选具备研究价值。",
        "history_used": [],
        "candidate_lanes": [{"candidate_id": "c1", "action": "score", "keep": True, "reason": "合法"}],
        "next_action": "validate_and_score",
        "stage_transition": {"next_stage": "score_review", "reason": "继续"},
        "confidence": 0.8,
    }
    result["why"] = "请参考 tool_evidence.score_factor_results 后继续。"

    svc._validate_orchestrator_stage_result("candidate_plan", result)

    assert "natural_language_fields_contain_internal_paths:tool_evidence." in result["_orchestrator_validation_warnings"]


def test_stage_contract_does_not_retry_only_for_history_used_prose_style():
    result = {
        **_natural_language_summary(),
        "stage": "candidate_plan",
        "decision": "run_batch",
        "judgment": "进入评分",
        "why": "候选具备研究价值。",
        "history_used": ["tool_evidence.score_factor_results"],
        "candidate_lanes": [{"candidate_id": "c1", "action": "score", "keep": True, "reason": "合法"}],
        "next_action": "validate_and_score",
        "stage_transition": {"next_stage": "score_review", "reason": "继续"},
        "confidence": 0.8,
    }

    svc._validate_orchestrator_stage_result("candidate_plan", result)

    assert "natural_language_fields_contain_internal_paths:tool_evidence." in result["_orchestrator_validation_warnings"]


def test_stage_contract_normalizes_history_used_string_without_retry():
    result = {
        **_natural_language_summary(),
        "stage": "candidate_plan",
        "decision": "run_batch",
        "judgment": "进入评分",
        "why": "候选具备研究价值。",
        "history_used": "上一轮最佳候选仍有改进价值。",
        "candidate_lanes": [{"candidate_id": "c1", "action": "score", "keep": True, "reason": "合法"}],
        "next_action": "validate_and_score",
        "stage_transition": {"next_stage": "score_review", "reason": "继续"},
        "confidence": 0.8,
    }

    svc._validate_orchestrator_stage_result("candidate_plan", result)

    assert result["history_used"] == ["上一轮最佳候选仍有改进价值。"]


def test_candidate_plan_empty_lanes_are_contract_error():
    result = {
        **_natural_language_summary(),
        "stage": "candidate_plan",
        "decision": "run_batch",
        "judgment": "当前计划没有可执行候选。",
        "why": "候选列表为空，因此应触发候选计划契约错误。",
        "history_used": [],
        "candidate_lanes": [],
        "next_action": "validate_and_score",
        "stage_transition": {"next_stage": "score_review", "reason": "计划原本准备进入评分复核。"},
        "confidence": 0.8,
    }

    with pytest.raises(DeepSeekClientError, match="candidate_lanes_required"):
        svc._validate_orchestrator_stage_result("candidate_plan", result)


def test_candidate_plan_grouped_lanes_are_normalized_to_contract_shape():
    result = {
        **_natural_language_summary(),
        "stage": "candidate_plan",
        "decision": "keep_and_plan",
        "judgment": "保留有代表性的候选。",
        "why": "这些变体需要先归一为逐候选计划，再进入评分。",
        "history_used": [],
        "candidate_lanes": {
            "planned_for_score": ["c1", "c2"],
            "candidate_plan_dropped": ["c3"],
        },
        "next_action": "compress_batch",
        "stage_transition": "expression_design",
        "confidence": 0.8,
    }

    svc._validate_orchestrator_stage_result(
        "candidate_plan",
        result,
        stage_input={
            "candidates": [
                {"candidate_id": "c1", "expression": "rank(close)"},
                {"candidate_id": "c2", "expression": "rank(open)"},
                {"candidate_id": "c3", "expression": "rank(high)"},
            ]
        },
    )

    assert isinstance(result["candidate_lanes"], list)
    assert {item["candidate_id"] for item in result["candidate_lanes"]} == {"c1", "c2", "c3"}
    assert [item["candidate_id"] for item in result["candidate_lanes"] if item["keep"]] == ["c1", "c2", "c3"]
    assert next(item for item in result["candidate_lanes"] if item["candidate_id"] == "c3")["action"] == "score"
    assert result["stage_transition"]["next_stage"] == "score_review"
    assert result["next_action"] == "validate_and_score"
    assert "candidate_lanes_grouped_dict_normalized_to_list" in result["_orchestrator_validation_warnings"]


def test_orchestrator_round_budget_zero_means_unlimited():
    inputs = {"n_rounds": 0, "target_adopted": 10}

    assert svc._round_budget_limit(inputs) is None
    assert svc._round_should_continue(round_no=1, inputs=inputs, adopted_total=0) is True
    assert svc._round_stop_reason(round_no=999, inputs=inputs, adopted_total=0) == "checkpoint_stop"
    assert svc._round_should_continue(round_no=999, inputs=inputs, adopted_total=10) is False
    assert svc._round_stop_reason(round_no=999, inputs=inputs, adopted_total=10) == "target_reached"


def test_round_synthesis_string_stage_transition_is_normalized_when_allowed():
    result = {
        **_natural_language_summary(),
        "stage": "round_synthesis",
        "decision": "round_failed",
        "judgment": "本轮深度验证未通过。",
        "why": "现有证据表明需要返回表达式设计进行变异。",
        "history_used": [],
        "round_memory": "try expression mutation",
        "next_action": "continue_research",
        "stage_transition": "expression_design",
        "confidence": 0.8,
    }

    svc._validate_orchestrator_stage_result("round_synthesis", result)

    assert result["stage_transition"]["next_stage"] == "expression_design"
    assert result["next_action"] == "start_next_round_at_expression_design"
    assert "stage_transition_string_normalized_to_object" in result["_orchestrator_validation_warnings"]


def test_round_synthesis_string_stage_transition_still_blocks_when_not_allowed():
    result = {
        **_natural_language_summary(),
        "stage": "round_synthesis",
        "decision": "round_failed",
        "judgment": "deep failed",
        "why": "needs mutation",
        "history_used": [],
        "round_memory": "try expression mutation",
        "next_action": "continue_research",
        "stage_transition": "import_review",
        "confidence": 0.8,
    }

    with pytest.raises(DeepSeekClientError, match="next_stage_not_allowed"):
        svc._validate_orchestrator_stage_result("round_synthesis", result)


def test_candidate_plan_prompt_compaction_preserves_full_candidate_coverage():
    candidates = [
        {"candidate_id": f"c{i}", "expression": f"rank(close_{i})"}
        for i in range(1, 11)
    ]

    compact = svc._compact_stage_tool_evidence_for_prompt(
        stage="candidate_plan",
        stage_input={"candidates": candidates, "code_precheck": []},
    )

    compact_candidates = compact["candidates"]
    assert len(compact_candidates) == 10
    assert [item["candidate_id"] for item in compact_candidates] == [f"c{i}" for i in range(1, 11)]


def test_candidate_plan_prompt_omits_future_stage_null_metric_placeholders():
    candidates = [
        {
            "candidate_id": "c1",
            "factor_name_hint": "CleanDesignCandidate",
            "expression": "rank(close)",
            "mechanism_summary": "价格强度候选草案。",
            "expected_direction": "positive",
            "status": None,
            "score": None,
            "grade": None,
            "reject_reasons": [],
            "deep_score": None,
            "deep_action": None,
            "novelty_allowed": None,
            "anti_overfit_score": None,
        }
    ]

    payload = svc._orchestrator_stage_payload(
        stage="candidate_plan",
        context_pack={},
        lineage_context={"parent_candidates": candidates},
        stage_input={"candidates": candidates, "code_precheck": []},
    )

    compact_candidate = payload["context_pack"]["tool_evidence"]["candidates"][0]
    raw = json.dumps(payload, ensure_ascii=False)
    assert compact_candidate == {
        "candidate_id": "c1",
        "factor_name": "CleanDesignCandidate",
        "expression": "rank(close)",
        "expected_direction": "positive",
        "mechanism_summary": "价格强度候选草案。",
    }
    assert '"score": null' not in raw
    assert '"deep_score": null' not in raw
    assert '"novelty_allowed": null' not in raw


def test_stage_payload_carries_operator_direction_and_run_contract_to_llm():
    payload = svc._orchestrator_stage_payload(
        stage="hypothesis_design",
        context_pack={
            "run_state": {
                "contract": {
                    "direction": "研究盈利预期修复，避开低估值慢变量",
                    "universe": "tradable_non_st",
                    "selection_start_date": "2022-01-01",
                    "selection_end_date": "2025-06-30",
                    "holding_period": 5,
                    "top_frac": 0.15,
                    "cost_rate": 0.0015,
                    "target_adopted": 8,
                    "n_candidates": 6,
                    "n_rounds": 12,
                }
            },
            "active_context": {},
        },
        stage_input={},
    )

    active = payload["context_pack"]["active_context"]
    assert active["operator_research_direction"]["mode"] == "operator_constrained"
    assert "binding research scope" in active["operator_research_direction"]["instruction"]
    assert active["operator_research_direction"]["value"] == "研究盈利预期修复，避开低估值慢变量"
    assert "direction" not in active["research_contract"]
    assert active["research_contract"]["top_frac"] == 0.15
    assert active["research_contract"]["cost_rate"] == 0.0015
    assert active["research_contract"]["n_candidates"] == 6


def test_stage_payload_marks_auto_direction_as_autonomous():
    payload = svc._orchestrator_stage_payload(
        stage="thesis_design",
        context_pack={"run_state": {"contract": {"direction": "auto"}}, "active_context": {}},
        stage_input={},
    )

    direction = payload["context_pack"]["active_context"]["operator_research_direction"]
    assert direction["mode"] == "autonomous_topic_selection"
    assert direction["value"] == "auto"


def test_review_stage_prompt_omits_future_stage_null_metric_placeholders():
    payload = svc._orchestrator_stage_payload(
        stage="score_review",
        context_pack={},
        lineage_context={},
        stage_input={
            "candidate_lanes": [
                {
                    "candidate_id": "c1",
                    "expression": "rank(close)",
                    "score": 72.0,
                    "grade": "B",
                    "deep_score": None,
                    "novelty_allowed": None,
                }
            ],
            "validate_results": [{"candidate_id": "c1", "status": "success", "validation": ""}],
            "score_factor_results": [
                {
                    "candidate_id": "c1",
                    "expression": "rank(close)",
                    "score": 72.0,
                    "grade": "B",
                    "deep_score": None,
                    "novelty_allowed": None,
                    "reject_reasons": [],
                }
            ],
            "code_advice": {
                "action": "advance_to_novelty",
                "candidate_lane_decisions": [
                    {
                        "candidate_id": "c1",
                        "action": "advance_to_novelty",
                        "reason": "quick keeper",
                        "weakest_component": None,
                        "deep_score": None,
                        "novelty_score": None,
                    }
                ],
            },
        },
    )

    raw = json.dumps(payload["context_pack"]["tool_evidence"], ensure_ascii=False)
    assert '"deep_score": null' not in raw
    assert '"novelty_allowed": null' not in raw
    assert '"weakest_component": null' not in raw
    assert '"validation": ""' not in raw


def test_novelty_prompt_preserves_similarity_evidence_without_null_candidate_schema():
    novelty_payload = {
        "keepers": [
            {
                "candidate_id": "c1",
                "expression": "rank(close)",
                "score": 82.0,
                "grade": "B",
                "novelty_guard": {
                    "allowed": True,
                    "reason": "novel_increment",
                    "novelty_score": 0.31,
                    "matched_existing_factor": "active_7",
                    "p90_rank_corr": 0.66,
                },
                "combined_guard": {"allowed": True, "reason": "novelty_and_st_exposure_passed"},
            }
        ],
        "details": [
            {
                "candidate_id": None,
                "factor_name": "",
                "expression": "rank(close)",
                "reason": "novel_increment",
                "matched_existing_factor": "active_7",
                "max_existing_pearson": 0.42,
                "p90_rank_corr": 0.66,
            }
        ],
    }

    compact = svc._compact_novelty_result_payload(novelty_payload)
    raw = json.dumps(compact, ensure_ascii=False)
    assert '"candidate_id": null' not in raw
    assert '"factor_name": ""' not in raw
    assert "matched_existing_factor" in raw
    assert "p90_rank_corr" in raw
    assert compact["keepers"][0]["novelty_guard"]["allowed"] is True


def test_candidate_plan_prompt_compaction_preserves_code_precheck_evidence():
    checks = [
        {
            "candidate_id": "c1",
            "fatal": True,
            "warnings": ["unsupported_fields:foo"],
            "instruction": "drop_candidate",
            "expression": "rank(foo)",
        }
    ]

    compact = svc._compact_stage_tool_evidence_for_prompt(
        stage="candidate_plan",
        stage_input={"candidates": [{"candidate_id": "c1", "expression": "rank(foo)"}], "code_precheck": checks},
    )

    assert compact["code_precheck"][0]["candidate_id"] == "c1"
    assert compact["code_precheck"][0]["fatal"] is True
    assert compact["code_precheck"][0]["warnings"] == ["unsupported_fields:foo"]


def test_round_synthesis_compaction_carries_code_precheck_to_next_llm():
    checks = [
        {
            "candidate_id": "c1",
            "fatal": True,
            "warnings": ["batch_duplicate_expression:c0"],
            "instruction": "drop_candidate",
            "expression": "rank(close)",
        }
    ]

    compact = svc._compact_round_synthesis_stage_input(
        {
            "reason": "candidate_plan_kept_none",
            "failed_candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
            "code_precheck": checks,
        }
    )

    assert compact["code_precheck_summary"]["fatal_candidate_ids"] == ["c1"]
    assert compact["code_precheck_summary"]["reason_counts"]["batch_duplicate_expression"] == 1
    assert compact["code_precheck"][0]["warnings"] == ["batch_duplicate_expression:c0"]


def test_candidate_plan_retry_shrink_preserves_full_candidate_coverage():
    candidates = [
        {"candidate_id": f"c{i}", "expression": f"rank(close_{i})"}
        for i in range(1, 11)
    ]
    payload = svc._orchestrator_stage_payload(
        stage="candidate_plan",
        context_pack={},
        stage_input={"candidates": candidates, "code_precheck": []},
        lineage_context=None,
        round_events=None,
        return_handoff=None,
    )

    shrunk = svc._shrink_orchestrator_stage_payload_for_retry(payload, stage="candidate_plan")
    compact_candidates = ((shrunk.get("context_pack") or {}).get("tool_evidence") or {}).get("candidates") or []

    assert len(compact_candidates) == 10
    assert [item["candidate_id"] for item in compact_candidates] == [f"c{i}" for i in range(1, 11)]
    assert shrunk["retry_contract"]["required_candidate_ids"] == [f"c{i}" for i in range(1, 11)]
    assert shrunk["retry_contract"]["candidate_lanes_shape"] == "array_of_objects_covering_every_required_candidate_id"
    assert "不能返回 {\"planned_for_score\"" in shrunk["retry_contract"]["instruction"]


def test_score_review_retry_shrink_preserves_full_scored_batch():
    scored = [
        {
            "candidate_id": f"c{i}",
            "expression": f"rank(close_{i})",
            "status": "success",
            "score": 70 + i,
            "grade": "B",
            "validation": "OK",
        }
        for i in range(1, 11)
    ]
    payload = svc._orchestrator_stage_payload(
        stage="score_review",
        context_pack={},
        stage_input={
            "candidate_lanes": scored,
            "validate_results": [{"candidate_id": item["candidate_id"], "status": "success", "validation": "OK"} for item in scored],
            "score_factor_results": scored,
            "trajectory_metrics": {"grade_distribution": {"B": 10}},
            "code_advice": {},
        },
        lineage_context=None,
        round_events=None,
        return_handoff=None,
    )

    shrunk = svc._shrink_orchestrator_stage_payload_for_retry(payload, stage="score_review")
    evidence = ((shrunk.get("context_pack") or {}).get("tool_evidence") or {})

    assert [item["candidate_id"] for item in evidence["score_factor_results"]] == [f"c{i}" for i in range(1, 11)]
    assert [item["candidate_id"] for item in evidence["candidate_lanes"]] == [f"c{i}" for i in range(1, 11)]
    assert shrunk["retry_contract"]["required_candidate_ids"] == [f"c{i}" for i in range(1, 11)]
    assert "不得省略任何已评分候选" in shrunk["retry_contract"]["instruction"]
    assert "绝对不能复制 history_context、tool_evidence" in shrunk["retry_contract"]["instruction"]


def test_round_synthesis_retry_preserves_every_completed_candidate():
    failed = [
        {
            "candidate_id": f"c{i}",
            "expression": f"rank(close_{i})",
            "status": "success",
            "score": 40 + i,
            "grade": "D" if i < 4 else "C",
        }
        for i in range(1, 5)
    ]
    payload = svc._orchestrator_stage_payload(
        stage="round_synthesis",
        context_pack={},
        stage_input={
            "authoritative_outcome": {"reason": "four candidates completed quick score"},
            "failed_candidates": failed,
            "tool_evidence_summary": [
                {"candidate_id": item["candidate_id"], "tool": "score_factor", "score": item["score"], "grade": item["grade"]}
                for item in failed
            ],
        },
        lineage_context=None,
        round_events=None,
        return_handoff=None,
    )

    shrunk = svc._shrink_orchestrator_stage_payload_for_retry(
        payload,
        stage="round_synthesis",
        correction_reason="round_synthesis:history_used_must_be_natural_chinese",
    )
    evidence = ((shrunk.get("context_pack") or {}).get("tool_evidence") or {})

    assert [item["candidate_id"] for item in evidence["failed_candidates"]] == ["c1", "c2", "c3", "c4"]
    assert [item["candidate_id"] for item in evidence["tool_evidence_summary"]] == ["c1", "c2", "c3", "c4"]
    assert shrunk["retry_contract"]["required_candidate_ids"] == ["c1", "c2", "c3", "c4"]
    assert "不得把其中任何候选写成未提交、未评分或不存在" in shrunk["retry_contract"]["instruction"]


def test_round_synthesis_retry_accepts_novelty_lane_dict():
    novelty_payload = {
        "keepers": [],
        "dropped": [
            {
                "candidate_id": "c4",
                "expression": "rank(-ts_rank(amount,20)) * rank(-ts_delta(borrow_money_bal,20))",
                "novelty_score": 0.0,
                "reason": "family_crowded_p90_threshold",
            }
        ],
    }
    payload = {
        "context_pack": {
            "tool_evidence": {
                "failed_candidates": novelty_payload,
                "tool_evidence_summary": novelty_payload,
            }
        },
        "output_contract": {},
    }

    shrunk = svc._shrink_orchestrator_stage_payload_for_retry(
        payload,
        stage="round_synthesis",
        correction_reason="round_synthesis:history_used_must_be_natural_chinese",
    )
    evidence = shrunk["context_pack"]["tool_evidence"]

    assert [item["candidate_id"] for item in evidence["failed_candidates"]] == ["c4"]
    assert [item["candidate_id"] for item in evidence["tool_evidence_summary"]] == ["c4"]
    assert shrunk["retry_contract"]["required_candidate_ids"] == ["c4"]


def test_expression_retry_contract_requires_unique_candidate_or_explicit_block():
    payload = {
        "task": "fxalpha_orchestrator_stage",
        "stage": "expression_design",
        "context_pack": {
            "tool_evidence": {
                "prior_expression_history": {
                    "exact_do_not_repeat": [
                        {
                            "round_id": "fr_test:r0001",
                            "candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
                        }
                    ]
                }
            }
        },
        "output_contract": {
            "required_fields": svc._ORCHESTRATOR_STAGE_REQUIRED["expression_design"],
            "allowed_next_stages": sorted(svc._ORCHESTRATOR_ALLOWED_NEXT_STAGES["expression_design"]),
        },
    }

    shrunk = svc._shrink_orchestrator_stage_payload_for_retry(
        payload,
        stage="expression_design",
        correction_reason="expression_design:all_candidates_exact_prior_round:c1=rank(close)@r0001",
    )

    instruction = shrunk["retry_contract"]["instruction"]
    assert "decision=blocked" in instruction
    assert "candidates=[]" in instruction
    assert "stage_transition.next_stage=blocker_review" in instruction
    assert "绝不能返回 propose_candidates 配空 candidates" in instruction


def test_compact_candidate_plan_context_does_not_replay_history_locator_paths():
    compact = svc._compact_llm_stage_result_for_prompt(
        {
            "stage": "candidate_plan",
            "decision": "run_batch",
            "judgment": "候选可以进入快筛。",
            "why": "预检查没有发现致命问题。",
            "history_used": ["history_context.short_term_history.stage_relevant_steps[0]"],
            "next_action": "validate_and_score",
            "stage_transition": {"next_stage": "score_review", "reason": "进入快筛。"},
        }
    )

    assert compact["judgment"] == "候选可以进入快筛。"
    assert "history_used" not in compact


def test_force_code_transition_overrides_llm_projection_path():
    result = {
        "next_action": "import_factor",
        "stage_transition": {"next_stage": "import_review", "next_action": "import_factor", "reason": "llm wants import"},
    }

    guarded = svc._force_code_transition(
        result,
        next_stage="round_synthesis",
        next_action="synthesize_gate_feedback",
        reason="code blocked import",
    )

    assert guarded["next_action"] == "synthesize_gate_feedback"
    assert guarded["stage_transition"]["next_stage"] == "round_synthesis"
    assert guarded["stage_transition"]["code_authoritative"] is True


def test_score_candidate_with_mcp_reads_outputs_payload(monkeypatch):
    class _FakeMcp:
        received = {}

        @staticmethod
        def validate_expression(expression, mode="local"):
            return "OK: expression is valid"

        @staticmethod
        async def score_factor(**kwargs):
            _FakeMcp.received = dict(kwargs)
            return json.dumps(
                {
                    "ok": True,
                    "outputs": {
                        "status": "success",
                        "score": 68.2,
                        "grade": "B",
                        "backtest_summary": {"ic_mean": 0.031, "ic_ir": 0.34},
                    },
                },
                ensure_ascii=False,
            )

    monkeypatch.setattr(svc, "_orchestrator_mcp_server", lambda: _FakeMcp)

    scored = svc._score_candidate_with_mcp(
        {"candidate_id": "c1", "expression": "rank(close)"},
        contract={
            "universe": "all_market",
            "selection_start_date": "2022-01-01",
            "selection_end_date": "2025-06-30",
            "holding_period": 5,
            "top_frac": 0.17,
            "cost_rate": 0.0015,
            "rebalance_anchor": "2022-01-07",
        },
    )

    assert scored["score"] == 68.2
    assert scored["grade"] == "B"
    assert scored["backtest_summary"]["ic_ir"] == 0.34
    assert scored["validation"].startswith("OK")
    assert _FakeMcp.received["top_frac"] == 0.17
    assert _FakeMcp.received["cost_rate"] == 0.0015
    assert _FakeMcp.received["rebalance_anchor"] == "2022-01-07"


def test_invalid_expression_has_no_synthetic_score_or_grade(monkeypatch):
    class _FakeMcp:
        @staticmethod
        def validate_expression(expression, mode="local"):
            return "ERROR: ts_av_diff requires exactly 2 arguments"

    monkeypatch.setattr(svc, "_orchestrator_mcp_server", lambda: _FakeMcp)
    scored = svc._score_candidate_with_mcp(
        {"candidate_id": "c1", "expression": "ts_av_diff(close,5,20)"},
        contract={},
    )

    assert scored["status"] == "invalid_expression"
    assert scored["score"] is None
    assert scored["quick_score"] is None
    assert scored["grade"] is None


def test_score_candidate_with_mcp_isolated_keeps_batch_alive_on_tool_error(monkeypatch):
    monkeypatch.setenv("FXALPHA_ORCH_DISABLE_TOOL_ISOLATION", "1")

    class _FakeMcp:
        @staticmethod
        def validate_expression(expression, mode="local"):
            return "OK: expression is valid"

        @staticmethod
        async def score_factor(**kwargs):
            raise RuntimeError("synthetic score failure")

    monkeypatch.setattr(svc, "_orchestrator_mcp_server", lambda: _FakeMcp)

    scored = svc._score_candidate_with_mcp_isolated(
        {"candidate_id": "c5", "expression": "rank(close)"},
        contract={"universe": "all_market", "selection_start_date": "2022-01-01", "selection_end_date": "2025-06-30", "holding_period": 5},
    )

    assert scored["candidate_id"] == "c5"
    assert scored["status"] == "score_error"
    assert scored["score"] == 0
    assert scored["grade"] == "D"
    assert scored["reject_reasons"] == ["score_runtime_error"]


def test_run_async_tool_times_out_instead_of_hanging():
    async def slow_tool():
        await asyncio.sleep(0.05)
        return {"ok": True}

    with pytest.raises(TimeoutError, match="orchestrator_tool_timeout_after"):
        svc._run_async_tool(slow_tool(), timeout_s=0.001)


def test_deep_validate_candidate_with_mcp_reads_outputs_payload(monkeypatch):
    class _FakeMcp:
        @staticmethod
        async def run_backtest(**kwargs):
            return json.dumps(
                {
                    "ok": True,
                    "outputs": {
                        "backtest_summary": {
                            "ic_mean": 0.0256,
                            "ic_ir": 0.2374,
                            "rank_ic_mean": 0.052,
                            "rank_ic_ir": 0.401,
                            "annual_return": 0.11,
                            "sharpe": 0.41,
                            "max_drawdown": -0.28,
                            "turnover": 0.22,
                        },
                        "metrics": {"total_return": 1.23},
                        "report_path": "/tmp/report.html",
                    },
                },
                ensure_ascii=False,
            )

        @staticmethod
        async def run_anti_overfit(**kwargs):
            return json.dumps(
                {
                    "ok": True,
                    "outputs": {
                        "score": 84.5,
                        "autocorrelation": {"stock_lag1_mean": 0.98, "stock_lag5_mean": 0.84},
                        "summary": {"passed": False},
                    },
                },
                ensure_ascii=False,
            )

        @staticmethod
        async def run_rolling_validation(**kwargs):
            return json.dumps(
                {
                    "ok": True,
                    "outputs": {
                        "status": "ok",
                        "score": 72.5,
                        "summary": {"status": "ok", "n_windows": 3},
                        "windows": [{"test_ic": 0.04, "test_ir": 0.6}],
                    },
                },
                ensure_ascii=False,
            )

        @staticmethod
        async def run_adversarial_validation(**kwargs):
            return json.dumps(
                {
                    "ok": True,
                    "outputs": {
                        "score": 76.5,
                        "summary": {"temporal_shuffle": 1.09},
                    },
                },
                ensure_ascii=False,
            )

    monkeypatch.setattr(svc, "_orchestrator_mcp_server", lambda: _FakeMcp)

    enriched = svc._deep_validate_candidate_with_mcp(
        {"candidate_id": "c1", "expression": "rank(close)", "score": 64.3, "grade": "B"},
        contract={"universe": "all_market", "selection_start_date": "2022-01-01", "selection_end_date": "2025-06-30", "holding_period": 5, "benchmark": "hs300", "n_groups": 5},
    )

    assert enriched["backtest_summary"]["ic_mean"] == 0.0256
    assert enriched["backtest_summary"]["ic_ir"] == 0.2374
    assert enriched["anti_overfit"]["score"] == 84.5
    assert enriched["anti_overfit"]["autocorrelation"]["stock_lag1_mean"] == 0.98
    assert enriched["rolling_validation"]["score"] == 72.5
    assert enriched["adversarial_validation"]["score"] == 76.5
    compact = svc._compact_orchestrator_candidate_for_diagnosis(enriched)
    assert compact["ic"] == 0.0256
    assert compact["icir"] == 0.2374
    assert compact["anti_overfit_score"] == 84.5
    assert "risk_flag" not in compact
    assert compact["adversarial_score"] == 76.5


def test_deep_validate_candidate_with_mcp_uses_public_bundle_with_idempotency(monkeypatch):
    class _FakeMcp:
        received = {}

        @staticmethod
        async def fxalpha_run_deep_validation_bundle(**kwargs):
            _FakeMcp.received = dict(kwargs)
            return {
                "component_sequence": [
                    "run_backtest",
                    "run_anti_overfit",
                    "run_rolling_validation",
                    "run_adversarial_validation",
                ],
                "backtest_summary": {"ic_mean": 0.031, "ic_ir": 0.42},
                "metrics": {"total_return": 1.1},
                "report_path": "/tmp/bundle.html",
                "anti_overfit": {"score": 82.0},
                "rolling_validation": {"status": "ok", "score": 77.0},
                "adversarial_validation": {"score": 74.0},
            }

        @staticmethod
        async def run_backtest(**kwargs):
            raise AssertionError("bundle path should not call public run_backtest")

        @staticmethod
        async def run_anti_overfit(**kwargs):
            raise AssertionError("bundle path should not call public run_anti_overfit")

        @staticmethod
        async def run_rolling_validation(**kwargs):
            raise AssertionError("bundle path should not call public run_rolling_validation")

        @staticmethod
        async def run_adversarial_validation(**kwargs):
            raise AssertionError("bundle path should not call public run_adversarial_validation")

    monkeypatch.setattr(svc, "_orchestrator_mcp_server", lambda: _FakeMcp)

    enriched = svc._deep_validate_candidate_with_mcp(
        {"candidate_id": "c1", "expression": "rank(close)", "score": 64.3, "grade": "B"},
        contract={
            "universe": "all_market",
            "selection_start_date": "2022-01-01",
            "selection_end_date": "2025-06-30",
            "holding_period": 5,
            "top_frac": 0.16,
            "cost_rate": 0.0012,
            "rebalance_anchor": "2022-01-06",
        },
    )

    assert enriched["deep_validation_bundle"]["public_gateway"] is True
    assert enriched["deep_validation_bundle"]["component_sequence"] == [
        "run_backtest",
        "run_anti_overfit",
        "run_rolling_validation",
        "run_adversarial_validation",
    ]
    assert enriched["backtest_summary"]["ic_mean"] == 0.031
    assert enriched["rolling_validation"]["score"] == 77.0
    assert _FakeMcp.received["idempotency_key"].startswith("fxalpha-orch-v1:")
    assert _FakeMcp.received["top_frac"] == 0.16
    assert _FakeMcp.received["cost_rate"] == 0.0012
    assert _FakeMcp.received["rebalance_anchor"] == "2022-01-06"
    assert enriched["orchestrator_tool_intent"]["tool"] == "deep_validation"


def test_orchestrator_tool_intent_is_stable_per_run_round_candidate_and_tool():
    contract = {
        "run_id": "run-a",
        "round_id": "run-a:r0002",
        "universe": "tradable_non_st",
        "selection_start_date": "start",
        "selection_end_date": "end",
        "holding_period": 5,
        "benchmark": "hs300",
        "top_frac": 0.2,
        "cost_rate": 0.003,
        "neutralize_cap": True,
        "neutralize_industry": False,
    }
    candidate = {"candidate_id": "c1", "expression": "rank(close)"}
    first = svc._orchestrator_tool_intent(tool="score_factor", candidate=candidate, contract=contract)
    second = svc._orchestrator_tool_intent(tool="score_factor", candidate=candidate, contract=dict(contract))
    deep = svc._orchestrator_tool_intent(tool="deep_validation", candidate=candidate, contract=contract)
    changed_cost = svc._orchestrator_tool_intent(
        tool="score_factor",
        candidate=candidate,
        contract={**contract, "cost_rate": 0.001},
    )

    assert first == second
    assert first["task_id"] != deep["task_id"]
    assert first["task_id"] != changed_cost["task_id"]
    assert first["policy"] == "completed_task_may_recover_failed_task_must_rerun"






def test_preflight_is_read_only_and_only_returns_stale_preview(monkeypatch):
    monkeypatch.setattr(svc, "factor_research_runtime_defaults", lambda: {"qgpt_url": "http://qgpt"})
    monkeypatch.setattr(svc, "_factor_readiness", lambda *args, **kwargs: {"quantgpt_api": {"reachable": True}})
    monkeypatch.setattr(svc, "_orchestrator_stale_preview", lambda: {"stale": True, "reason": "stale_heartbeat"})
    monkeypatch.setattr(svc, "_mark_stale_orchestrator_run_interrupted", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("read path wrote state")))

    result = svc.factor_research_preflight().to_dict()

    assert result["ok"] is True
    assert result["outputs"]["stale_or_interrupted"] == {"stale": True, "reason": "stale_heartbeat"}


def test_interrupted_handoff_uses_recovery_checkpoint_without_forcing_new_thesis(monkeypatch, tmp_path):
    events_file = tmp_path / "events.jsonl"
    checkpoint = {
        "type": "orchestrator_recovery_checkpoint",
        "round_id": "run-recover:r0003",
        "stage": "score_factor",
        "thesis": {"theses": [{"name": "keep"}]},
        "hypothesis": {"hypotheses": [{"name": "keep-h"}]},
        "candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
        "planned_candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
        "candidate_plan": {"candidate_lanes": [{"candidate_id": "c1", "keep": True}]},
        "candidate_precheck": [],
    }
    events_file.write_text(json.dumps({"run_id": "run-recover", "evidence_refs": [checkpoint]}) + "\n", encoding="utf-8")
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_EVENTS_FILE", events_file)
    interrupted = {
        "run_id": "run-recover",
        "previous_stage": "score_review",
        "previous_stage_id": "run-recover:r0003:s05_score_review",
        "evidence_refs": [
            {
                "type": "orchestrator_interrupted",
                "interrupted_run_id": "run-recover",
                "last_stage": "score_review",
                "last_stage_transition": {"next_stage": "novelty_review"},
            }
        ],
    }

    handoff = svc._orchestrator_interrupted_handoff(interrupted)

    assert handoff["to_stage"] == "expression_design"
    assert handoff["recommended_mutation"] == "replay_existing_candidates_without_llm_redesign"
    assert handoff["recovery_checkpoint"]["planned_candidates"][0]["candidate_id"] == "c1"


def test_retryable_llm_blocker_can_resume_only_with_durable_checkpoint(monkeypatch, tmp_path):
    events_file = tmp_path / "events.jsonl"
    checkpoint = {
        "type": "orchestrator_recovery_checkpoint",
        "round_id": "run-json:r0004",
        "thesis": {"theses": [{"thesis_id": "t1"}]},
        "hypothesis": {"hypotheses": [{"hypothesis_id": "h1"}]},
        "candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
        "planned_candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
        "candidate_plan": {"candidate_lanes": [{"candidate_id": "c1", "keep": True}]},
        "candidate_precheck": [],
    }
    events_file.write_text(
        "\n".join(
            [
                json.dumps({"run_id": "run-json", "evidence_refs": [checkpoint]}),
                json.dumps(
                    {
                        "run_id": "run-json",
                        "stage": "blocker",
                        "event_type": "blocker",
                        "decision": "阻塞原因：llm_response_not_valid_json:Expecting ',' delimiter",
                        "previous_stage": "score_review",
                        "previous_stage_id": "run-json:r0004:s05_score_review",
                        "evidence_refs": [{"type": "DeepSeekClientError", "error": "llm_response_not_valid_json:Expecting ',' delimiter"}],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_EVENTS_FILE", events_file)

    blocker = svc._latest_orchestrator_recoverable_llm_blocker("run-json")
    handoff = svc._orchestrator_interrupted_handoff(blocker)

    assert blocker["stage"] == "blocker"
    assert handoff["from_stage"] == "orchestrator_recovery"
    assert handoff["to_stage"] == "expression_design"
    assert handoff["recovery_checkpoint"]["round_id"] == "run-json:r0004"
    assert svc._latest_orchestrator_recoverable_llm_blocker("missing") == {}


def test_run_view_projects_existing_process_log_and_tool_intent(monkeypatch, tmp_path):
    steps_dir = tmp_path / "research_steps"
    steps_file = steps_dir / "current.jsonl"
    history_dir = steps_dir / "history"
    steps_dir.mkdir()
    history_dir.mkdir()
    step = {
        "schema_version": "research_step_v2",
        "ts": "2026-07-11T12:00:00",
        "run_id": "run-view",
        "round_id": "run-view:r0001",
        "stage": "score_review",
        "evidence_refs": [
            {
                "type": "orchestrator_tool_intent",
                "tool": "score_factor",
                "candidate_id": "c1",
                "task_id": "task-1",
                "idempotency_key": "key-1",
            }
        ],
    }
    steps_file.write_text(json.dumps(step) + "\n", encoding="utf-8")
    monkeypatch.setattr(svc, "FACTOR_RESEARCH_STEPS_FILE", steps_file)
    monkeypatch.setattr(svc, "FACTOR_RESEARCH_STEPS_HISTORY_DIR", history_dir)
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_LLM_TRACES_FILE", tmp_path / "traces.jsonl")
    monkeypatch.setattr(svc, "_fetch_quantgpt_tasks_by_ids", lambda task_ids, limit=1000: [{"task_id": "task-1", "status": "running", "task_type": "score"}])

    result = svc.factor_research_run_view(run_id="run-view").to_dict()

    outputs = result["outputs"]
    assert outputs["source_roles"]["research_steps"] == "authoritative_process_log"
    assert outputs["tool_intents"][0]["task_id"] == "task-1"
    assert outputs["quantgpt_tasks"][0]["task_id"] == "task-1"


def test_progress_events_remain_in_orch_journal_but_not_research_steps(monkeypatch, tmp_path):
    steps_file, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    base = {
        "run_id": "run-progress",
        "round_id": "run-progress:r0001",
        "stage_seq": 3,
        "previous_stage": "candidate_plan",
        "previous_stage_id": "run-progress:r0001:s02_candidate_plan",
        "stage": "score_review",
        "summary": "score update",
        "decision": "continue",
        "stage_transition": {"next_stage": "score_review", "next_action": "continue"},
        "event_type": "checkpoint",
        "checkpoint": "score_review",
    }
    progress = svc._write_orchestrator_event(
        {**base, "stage_id": "run-progress:r0001:s03_score_progress", "tags": ["tool_progress", "candidate_progress"]}
    )
    semantic = svc._write_orchestrator_event(
        {**base, "stage_id": "run-progress:r0001:s04_score_review", "tags": ["llm_result"]}
    )

    assert progress["sync_status"] == "event_only_progress"
    assert semantic["sync_status"] == "synced_to_research_step"
    assert len(events_file.read_text(encoding="utf-8").splitlines()) == 2
    steps = [json.loads(line) for line in steps_file.read_text(encoding="utf-8").splitlines()]
    assert [step["stage_id"] for step in steps] == ["run-progress:r0001:s04_score_review"]


def test_orchestrator_current_journals_are_bounded_and_history_is_complete(monkeypatch, tmp_path):
    _, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_EVENTS_MAX_LINES", 2)
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_EVENTS_MAX_BYTES", 1024 * 1024)
    for seq in range(3):
        svc._write_orchestrator_event(
            {
                "run_id": "run-bounded",
                "round_id": "run-bounded:r0001",
                "stage_seq": seq,
                "stage_id": f"run-bounded:r0001:s{seq:02d}_review",
                "stage": "score_review",
                "summary": f"semantic-{seq}",
                "decision": "continue",
                "stage_transition": {"next_stage": "score_review", "next_action": "continue"},
                "tags": ["llm_result"],
            }
        )

    current = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    history = list((events_file.parent / "history").glob("*.jsonl"))
    history_rows = [json.loads(line) for line in history[0].read_text(encoding="utf-8").splitlines()]
    assert [row["stage_seq"] for row in current] == [1, 2]
    assert [row["stage_seq"] for row in history_rows] == [0, 1, 2]


def test_run_scoped_journal_reader_finds_records_after_current_cache_rollover(monkeypatch, tmp_path):
    _, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    history_dir = events_file.parent / "history"
    history_dir.mkdir(parents=True)
    events_file.write_text(json.dumps({"run_id": "new-run", "stage": "score_review"}) + "\n", encoding="utf-8")
    history_file = history_dir / "2026-07-01.jsonl"
    history_file.write_text(
        "\n".join(
            [
                json.dumps({"run_id": "old-run", "stage": "thesis_design", "summary": "old"}),
                json.dumps({"run_id": "new-run", "stage": "score_review", "summary": "new"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = svc.factor_research_orchestrator_events(run_id="old-run", limit=10).to_dict()

    assert result["outputs"]["count"] == 1
    assert result["outputs"]["events"][0]["summary"] == "old"
    assert result["outputs"]["history_files_scanned"] >= 1


def test_stale_quantgpt_task_indicator_is_read_only():
    indicator = svc._quantgpt_stale_task_summary(
        [
            {
                "task_id": "stale-1",
                "task_type": "backtest",
                "status": "running",
                "created_at": "2000-01-01T00:00:00",
                "updated_at": "2000-01-01T00:00:00",
            }
        ],
        stale_seconds=60,
    )

    assert indicator["stale_count"] == 1
    assert indicator["tasks"][0]["task_id"] == "stale-1"
    assert indicator["action"] == "inspect_or_retry_explicitly; no_status_mutation_on_read"

    state_summary = svc._quantgpt_summary_for_research_state(
        {"running_count": 1, "running_tasks": [{"task_id": "stale-1", "status": "running"}]},
        indicator,
    )
    assert state_summary["observed_running_count"] == 1
    assert state_summary["stale_running_count"] == 1
    assert state_summary["running_count"] == 0
    assert state_summary["running_tasks"] == []


def test_stale_quantgpt_task_indicator_treats_naive_db_timestamp_as_utc():
    # QuantGPT persists SQLite DateTime columns as UTC without an offset.
    # On a UTC+8 host, treating this value as local would immediately mark a
    # newly-created task stale.
    utc_now_without_offset = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    indicator = svc._quantgpt_stale_task_summary(
        [
            {
                "task_id": "fresh-utc-task",
                "task_type": "score",
                "status": "running",
                "created_at": utc_now_without_offset,
                "updated_at": utc_now_without_offset,
            }
        ],
        stale_seconds=60,
    )

    assert indicator["stale_count"] == 0


def test_process_recovery_replays_existing_candidates_without_design_llm(monkeypatch):
    class _Client:
        def __init__(self, **kwargs):
            pass

        def available(self):
            return True

        def preferred_model(self):
            return "test-model"

    checkpoint = {
        "round_id": "run-resume:r0003",
        "thesis": {"theses": [{"name": "existing-thesis"}]},
        "hypothesis": {"hypotheses": [{"name": "existing-hypothesis"}]},
        "candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
        "planned_candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
        "candidate_plan": {"candidate_lanes": [{"candidate_id": "c1", "keep": True}]},
        "candidate_precheck": [],
    }
    llm_calls = []
    scored = []

    def event(**kwargs):
        return {"stage": kwargs["stage"], "stage_id": f"{kwargs['round_id']}:{kwargs['stage']}"}

    def record(**kwargs):
        stage = kwargs["stage"]
        if stage == "score_review":
            result = {
                "decision": "return_expression_design",
                "judgment": "score below keeper",
                "why": "synthetic",
                "candidate_decisions": [{"candidate_id": "c1", "action": "return_expression_design"}],
                "code_advice_alignment": {"items": []},
                "next_action": "return_expression_design",
                "stage_transition": {"next_stage": "expression_design", "next_action": "return_expression_design"},
            }
        else:
            result = {
                "decision": "checkpoint_stop",
                "judgment": "synthetic",
                "why": "synthetic",
                "round_memory": {},
                "next_action": "stop",
                "stage_transition": {"next_stage": "checkpoint_stop", "next_action": "stop"},
            }
        return {"stage": stage, "stage_id": f"{kwargs['round_id']}:{stage}", "llm_result": result}

    def complete(**kwargs):
        llm_calls.append(kwargs["stage"])
        if kwargs["stage"] == "score_review":
            return {
                "decision": "return_expression_design",
                "judgment": "score below keeper",
                "why": "synthetic",
                "candidate_decisions": [{"candidate_id": "c1", "action": "return_expression_design"}],
                "code_advice_alignment": {"items": []},
                "next_action": "return_expression_design",
                "stage_transition": {"next_stage": "expression_design", "next_action": "return_expression_design"},
            }
        return {
            "decision": "checkpoint_stop",
            "judgment": "synthetic",
            "why": "synthetic",
            "round_memory": {},
            "next_action": "stop",
            "stage_transition": {"next_stage": "checkpoint_stop", "next_action": "stop"},
        }

    monkeypatch.setattr(svc, "DeepSeekJSONClient", _Client)
    monkeypatch.setattr(svc, "_orchestrator_set_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc, "_orchestrator_stage_event", event)
    monkeypatch.setattr(svc, "_record_llm_stage_event", record)
    monkeypatch.setattr(svc, "_complete_orchestrator_stage_json", complete)
    monkeypatch.setattr(svc, "_build_orchestrator_context_pack", lambda **kwargs: {"active_context": {"active_factor_summary": {}}})
    monkeypatch.setattr(svc, "factor_library_audit", lambda **kwargs: svc.ok_result(outputs={}))
    monkeypatch.setattr(svc, "factor_map_context", lambda **kwargs: {"available": True, "map_id": "fm_test", "audit_id": "fa_test"})
    monkeypatch.setattr(svc, "_candidate_plan_code_precheck", lambda *args, **kwargs: [])
    monkeypatch.setattr(svc, "_score_candidate_with_mcp_isolated", lambda candidate, contract: scored.append(dict(candidate)) or {**candidate, "status": "success", "score": 30.0, "grade": "D", "screening_stage": "quick_score"})

    svc._run_orchestrator_job(
        "run-resume",
        {"n_rounds": 3, "target_adopted": 1, "n_candidates": 1, "llm_timeout_s": 10},
        {
            "interrupted_handoff": {
                "to_stage": "expression_design",
                "reason": "worker interrupted",
                "recovery_checkpoint": checkpoint,
            },
        },
    )

    assert [item["candidate_id"] for item in scored] == ["c1"]
    assert "expression_design" not in llm_calls
    assert "candidate_plan" not in llm_calls
    assert "score_review" in llm_calls


def test_deep_validate_candidate_with_mcp_isolated_keeps_round_alive_on_tool_error(monkeypatch):
    monkeypatch.setenv("FXALPHA_ORCH_DISABLE_TOOL_ISOLATION", "1")

    class _FakeMcp:
        @staticmethod
        async def run_backtest(**kwargs):
            raise RuntimeError("synthetic deep failure")

        @staticmethod
        async def run_anti_overfit(**kwargs):
            return json.dumps({"ok": True, "outputs": {"score": 80}}, ensure_ascii=False)

        @staticmethod
        async def run_adversarial_validation(**kwargs):
            return json.dumps({"ok": True, "outputs": {"score": 80}}, ensure_ascii=False)

    monkeypatch.setattr(svc, "_orchestrator_mcp_server", lambda: _FakeMcp)

    enriched = svc._deep_validate_candidate_with_mcp_isolated(
        {"candidate_id": "c1", "expression": "rank(close)", "score": 64.3, "grade": "B"},
        contract={"universe": "all_market", "selection_start_date": "2022-01-01", "selection_end_date": "2025-06-30", "holding_period": 5},
    )

    assert enriched["status"] == "deep_validation_error"
    assert enriched["deep_score"] == 0
    assert enriched["reject_reasons"] == ["deep_validation_runtime_error"]


def test_deep_validation_replays_incomplete_evidence_once(monkeypatch):
    calls = []
    incomplete = {
        "candidate_id": "c1",
        "expression": "rank(close)",
        "quick_score": 75,
        "backtest_summary": {"ic_mean": 0.03, "ic_ir": 0.4},
        "novelty_guard": {"allowed": True, "novelty_score": 0.3},
        "anti_overfit": {"score": 90},
        "rolling_validation": {
            "status": "ok",
            "score": 70,
            "summary": {"n_windows": 1},
            "windows": [{"test_ic": 0.03}],
        },
    }
    complete = {**incomplete, "adversarial_validation": {"score": 75}}

    def fake_deep(candidate, *, contract):
        calls.append(candidate["candidate_id"])
        return incomplete if len(calls) == 1 else complete

    monkeypatch.setattr(svc, "_deep_validate_candidate_with_mcp_isolated", fake_deep)

    result = svc._deep_validate_candidate_with_evidence_retry(
        {"candidate_id": "c1", "expression": "rank(close)"},
        contract={},
    )

    assert calls == ["c1", "c1"]
    assert result["adversarial_validation"]["score"] == 75


def test_orchestrator_event_syncs_light_research_step(monkeypatch, tmp_path):
    steps_file, events_file = _redirect_orchestrator(monkeypatch, tmp_path)

    svc._write_orchestrator_event(
        {
            "run_id": "run-orch",
            "round_id": "run-orch:r0001",
            "stage_seq": 2,
            "stage_id": "run-orch:r0001:s02_score_review",
            "previous_stage": "candidate_plan",
            "previous_stage_id": "run-orch:r0001:s01_candidate_plan",
            "stage": "score_review",
            "summary": "Quick advice complete.",
            "decision": "Advance one candidate.",
            "priority": "normal",
            "stage_transition": {
                "next_stage": "novelty_review",
                "next_action": "Run novelty for candidate a.",
                "research_strategy": "normal_process_to_novelty",
                "facts": "best_score=70",
                "judgment": "candidate merits novelty",
                "why": "quick score passed",
                "history_used": "recent failures avoided",
            },
            "event_type": "advice",
            "checkpoint": "score_review",
            "candidate_lanes": [{"candidate_id": "a"}],
            "trajectory_metrics": {"best_score": 70},
            "advice": {"action": "advance_to_novelty"},
            "allowed_actions": ["fxalpha_novelty_check"],
            "blocked_actions": ["fxalpha_quality_gate"],
        },
        sync_research_step=True,
    )

    event = json.loads(events_file.read_text(encoding="utf-8").splitlines()[-1])
    step = json.loads(steps_file.read_text(encoding="utf-8").splitlines()[-1])
    assert event["schema_version"] == "orchestrator_event_v1"
    assert event["candidate_lanes"] == [{"candidate_id": "a"}]
    assert event["sync_status"] == "synced_to_research_step"
    assert step["schema_version"] == "research_step_v2"
    assert step["stage"] == "score_review"
    assert "candidate_lanes" not in step
    assert "advice" not in step
    assert step["tags"][0] == "orchestrator"


def test_llm_request_progress_step_updates_gui_chain(monkeypatch, tmp_path):
    steps_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)

    svc._write_research_step(
        {
            "schema_version": "research_step_v2",
            "ts": "2026-06-14T10:00:00",
            "run_id": "run-orch",
            "round_id": "run-orch:r0001",
            "stage_seq": 5,
            "stage_id": "run-orch:r0001:s05_score_review",
            "previous_stage": "candidate_plan",
            "previous_stage_id": "run-orch:r0001:s04_candidate_plan",
            "stage": "score_review",
            "summary": "开始快筛",
            "decision": "等待 score_factor",
            "stage_transition": {"next_stage": "score_review", "next_action": "validate_and_score_in_progress"},
            "tags": ["orchestrator", "tool_progress", "score_review_progress"],
        }
    )

    svc._write_orchestrator_llm_request_step(
        run_id="run-orch",
        round_id="run-orch:r0001",
        stage="score_review",
        checkpoint="score_review",
        trace_id="run-orch:r0001:score_review:abcd1234",
        payload_chars=12345,
        llm_model="deepseek-v4-flash",
        prompt_digest={
            "stage_briefing": "你现在处于 quick score review 阶段。",
            "history_used": ["deep_validation_review / mutate / 返回 expression_design"],
            "facts": "recent_rounds=6 | review_anchors=3 | score_factor_results=4",
            "handoff_reason": "上一轮 deep_score 不足",
            "knowledge_titles": ["ps_ttm path near miss"],
            "tool_summary": ["score_factor_results=4"],
            "operator_guidance": {
                "guidance_id": "guidance_abcd",
                "stage_id": "run-orch:r0001:s98_human_guidance",
                "summary": "保留当前主信号，只调整归一化。",
                "author": "operator",
            },
        },
    )

    step = json.loads(steps_file.read_text(encoding="utf-8").splitlines()[-1])
    assert step["stage"] == "score_review"
    assert step["decision"] == "进入 LLM review 阶段，等待 DeepSeek v4 返回 JSON 决策。"
    assert step["stage_transition"]["next_action"] == "llm_review_in_progress"
    assert "等待 DeepSeek v4 返回 score_review 阶段 JSON 研究判断" in step["stage_transition"]["judgment"]
    assert "deep_validation_review / mutate" in step["stage_transition"]["history_used"]
    assert "上下文摘要（非模型判断）" in step["stage_transition"]["facts"]
    assert "score_factor_results=4" in step["stage_transition"]["facts"]
    assert "你现在处于 quick score review 阶段。" in step["stage_transition"]["research_strategy"]
    assert "上一轮 deep_score 不足" in step["stage_transition"]["research_strategy"]
    assert step["monitoring"]["event_type"] == "llm_request"
    assert step["llm_trace_id"] == "run-orch:r0001:score_review:abcd1234"
    assert step["evidence_refs"][1]["type"] == "context_pack_digest"
    assert step["evidence_refs"][1]["knowledge_titles"] == ["ps_ttm path near miss"]
    delivery = step["evidence_refs"][2]
    assert delivery["type"] == "operator_guidance_delivery"
    assert delivery["guidance_id"] == "guidance_abcd"
    assert delivery["trace_id"] == "run-orch:r0001:score_review:abcd1234"
    assert delivery["delivered_to_stage"] == "score_review"
    assert step["monitoring"]["operator_guidance"]["guidance_id"] == "guidance_abcd"
    assert "已收到人工干预" in step["summary"]


def test_compact_stage_history_prefers_substantive_steps_over_request_noise():
    history = svc._compact_stage_history(
        {
            "active_context": {},
            "recent_steps": [
                {
                    "stage": "score_review",
                    "summary": "DeepSeek v4 已收到 score_review 阶段证据，正在生成研究判断。",
                    "decision": "进入 LLM review 阶段，等待 DeepSeek v4 返回 JSON 决策。",
                    "tags": ["orchestrator", "deepseek_v4", "llm_request_progress", "score_review"],
                    "monitoring": {"event_type": "llm_request"},
                    "stage_transition": {"next_stage": "score_review", "next_action": "llm_review_in_progress"},
                },
                {
                    "stage": "score_review",
                    "summary": "Quick score 完成。",
                    "decision": "advance_some",
                    "tags": ["orchestrator", "deepseek_v4", "score_review", "llm_review"],
                    "stage_transition": {
                        "next_stage": "novelty_review",
                        "next_action": "run_novelty",
                        "judgment": "B级候选可进入 novelty",
                        "why": "IC/ICIR 达标",
                    },
                },
            ],
        },
        stage="score_review",
        round_events=[],
    )

    recent = history["short_term_history"]["stage_relevant_steps"]
    assert recent[0]["decision"] == "advance_some"
    assert all(item["decision"] != "进入 LLM review 阶段，等待 DeepSeek v4 返回 JSON 决策。" for item in recent[:1])


def test_compact_stage_history_reads_compact_recent_steps_with_top_level_fields():
    history = svc._compact_stage_history(
        {
            "active_context": {},
            "recent_steps": [
                {
                    "ts": "2026-06-14T10:10:00",
                    "stage": "score_review",
                    "summary": "Quick score 完成。",
                    "decision": "advance_some",
                    "next_stage": "novelty_review",
                    "next_action": "run_novelty",
                    "judgment": "B级候选可进入 novelty",
                    "why": "IC/ICIR 达标",
                    "history_used": "deep_validation_review: mutate",
                    "tags": ["orchestrator", "deepseek_v4", "score_review", "llm_review"],
                }
            ],
        },
        stage="hypothesis_design",
        round_events=[],
    )

    recent = history["short_term_history"]["stage_relevant_steps"][0]
    anchor = history["short_term_history"]["review_anchors"][0]
    assert recent["next_stage"] == "novelty_review"
    assert recent["judgment"] == "B级候选可进入 novelty"
    assert "history_used" not in recent
    assert anchor["judgment"] == "B级候选可进入 novelty"


def test_compact_stage_history_prefers_current_run_before_cross_run():
    history = svc._compact_stage_history(
        {
            "run_state": {"run_id": "run-current"},
            "active_context": {},
            "recent_steps": [
                {
                    "run_id": "run-other",
                    "stage": "deep_validation_review",
                    "summary": "其他 run 的 deep。",
                    "decision": "mutate",
                    "stage_transition": {"next_stage": "expression_design", "next_action": "return_expression_design"},
                },
                {
                    "run_id": "run-current",
                    "stage": "score_review",
                    "summary": "当前 run 的 score。",
                    "decision": "advance_some",
                    "stage_transition": {"next_stage": "novelty_review", "next_action": "run_novelty"},
                },
            ],
        },
        stage="score_review",
        round_events=[],
    )

    recent = history["short_term_history"]["stage_relevant_steps"]
    assert recent[0]["summary"] == "当前 run 的 score。"


def test_compact_stage_history_tightens_review_context_limits():
    history = svc._compact_stage_history(
        {
            "run_state": {"run_id": "run-current"},
            "active_context": {},
            "recent_steps": [
                {
                    "run_id": "run-current" if idx < 6 else "run-other",
                    "stage_id": f"s{idx}",
                    "stage": "deep_validation_review" if idx % 2 == 0 else "score_review",
                    "summary": f"summary-{idx}",
                    "decision": f"decision-{idx}",
                    "stage_transition": {
                        "next_stage": "expression_design",
                        "next_action": "return_expression_design",
                        "judgment": f"judgment-{idx}",
                        "why": f"why-{idx}",
                    },
                    "tags": ["orchestrator", "deepseek_v4", "llm_review"],
                }
                for idx in range(8)
            ],
        },
        stage="score_review",
        round_events=[{"stage": "score_review", "summary": f"round-{idx}", "decision": "advance_some"} for idx in range(6)],
    )

    assert len(history["short_term_history"]["stage_relevant_steps"]) <= 5
    assert len(history["short_term_history"]["review_anchors"]) <= 3
    assert len(history["short_term_history"]["recent_same_round_events"]) <= 4


def test_recent_failure_feedback_counts_only_negative_fields(monkeypatch, tmp_path):
    _, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    events_file.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "run_id": "run-a",
            "round_id": "run-a:r0001",
            "stage": "deep_validation_review",
            "summary": "deep near miss",
            "candidate_lanes": [
                {
                    "candidate_id": "c1",
                    "expression": "rank(-ts_delta(ps_ttm,5)/max(ps_ttm,1)) * rank(-ts_std(close,5))",
                    "grade": "B",
                }
            ],
            "evidence_refs": [
                {
                    "candidate_id": "c1",
                    "deep_score": 79.6,
                    "quick_score": 69.7,
                    "risk_flag": "watch",
                }
            ],
            "advice": {"candidate_lane_decisions": []},
        },
        {
            "run_id": "run-a",
            "round_id": "run-a:r0002",
            "stage": "score_review",
            "summary": "weak quick candidate",
            "candidate_lanes": [
                {
                    "candidate_id": "c2",
                    "expression": "rank(ts_delta(net_mf_amount,5)) * rank(-float_mv)",
                    "grade": "D",
                }
            ],
            "evidence_refs": [
                {
                    "candidate_id": "c2",
                    "score": 29.1,
                    "grade": "D",
                }
            ],
            "advice": {"candidate_lane_decisions": []},
        },
    ]
    events_file.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n", encoding="utf-8")

    feedback = svc._recent_orchestrator_failure_feedback(limit_candidates=5, limit_fields=8)

    weak_fields = {item["field"] for item in feedback["weak_fields"]}
    assert "net_mf_amount" in weak_fields
    assert "ps_ttm" not in weak_fields


def test_history_step_transition_merges_top_level_fields_into_partial_transition():
    transition = svc._history_step_transition(
        {
            "stage": "hypothesis_design",
            "stage_transition": {"next_stage": "expression_design"},
            "judgment": "保留新的假设",
            "why": "旧 handoff 已经消费",
            "history_used": "score_review: mutate",
        }
    )

    assert transition["next_stage"] == "expression_design"
    assert transition["judgment"] == "保留新的假设"
    assert transition["why"] == "旧 handoff 已经消费"
    assert transition["history_used"] == "score_review: mutate"


def test_stage_payload_keeps_upstream_handoff_visible_for_review_stage():
    payload = svc._orchestrator_stage_payload(
        stage="deep_validation_review",
        context_pack={
            "run_state": {"run_id": "run1", "round_id": "run1:r0001", "contract": {"target_adopted": 1, "holding_period": 5}},
            "active_context": {"active_factor_summary": {}, "field_context": {}},
            "recent_steps": [],
            "protocol": {},
        },
        stage_input={"deep_results": {}},
        lineage_context={
            "previous_review_advice": [
                {
                    "from_stage": "score_review",
                    "to_stage": "expression_design",
                    "reason": "quick_score 不足",
                    "recommended_mutation": "return_expression_design",
                }
            ]
        },
        round_events=[],
        return_handoff={
            "from_stage": "novelty_review",
            "to_stage": "deep_validation_review",
            "reason": "novelty 已通过",
            "recommended_mutation": "run_deep_validation",
        },
    )

    assert payload["context_pack"]["upstream_handoff"]["from_stage"] == "novelty_review"
    assert payload["context_pack"]["upstream_handoff"]["to_stage"] == "deep_validation_review"


def test_stage_payload_does_not_leak_downstream_advice_into_thesis():
    payload = svc._orchestrator_stage_payload(
        stage="thesis_design",
        context_pack={
            "run_state": {"run_id": "run1", "round_id": "run1:r0001", "contract": {"target_adopted": 1, "holding_period": 5}},
            "active_context": {"active_factor_summary": {}, "field_context": {}},
            "recent_steps": [],
            "protocol": {},
        },
        stage_input={"blocked_or_failed_reasons": {}, "available_field_families": ["ps_ttm", "close"], "target_constraints": {"need_active_factors": 1}},
        lineage_context={
            "previous_review_advice": [
                {"from_stage": "score_review", "to_stage": "expression_design", "reason": "ICIR 不足", "recommended_mutation": "return_expression_design"},
                {"from_stage": "score_review", "to_stage": "expression_design", "reason": "ICIR 不足", "recommended_mutation": "return_expression_design"},
            ]
        },
        round_events=[],
    )

    assert "lineage_context" not in payload["context_pack"]
    assert "handoff" not in payload["context_pack"].get("current_round_context", {})
    assert not payload["context_pack"].get("upstream_handoff")
    assert "latest_round_handoff" not in payload["context_pack"].get("history_context", {}).get("short_term_history", {})


def test_expression_design_uses_only_expression_targeted_handoff():
    payload = svc._orchestrator_stage_payload(
        stage="expression_design",
        context_pack={
            "run_state": {"run_id": "run1", "round_id": "run1:r0001", "contract": {"target_adopted": 1, "holding_period": 5}},
            "active_context": {"active_factor_summary": {}, "field_context": {}},
            "recent_steps": [],
            "protocol": {},
        },
        stage_input={"hypotheses": [{"hypothesis_id": "h1", "signal_claim": "repair"}]},
        lineage_context={
            "previous_review_advice": [
                {"from_stage": "candidate_plan", "to_stage": "hypothesis_design", "reason": "先重写 hypothesis", "recommended_mutation": "redesign_hypothesis"},
                {"from_stage": "deep_validation_review", "to_stage": "expression_design", "reason": "保留机制但重写表达式", "recommended_mutation": "return_expression_design"},
            ],
            "return_reason_from_downstream": {
                "from_stage": "candidate_plan",
                "to_stage": "hypothesis_design",
                "reason": "旧的 hypothesis 已失败",
            },
        },
        round_events=[],
        return_handoff={"from_stage": "candidate_plan", "to_stage": "hypothesis_design", "reason": "旧的 hypothesis 已失败"},
    )

    assert "lineage_context" not in payload["context_pack"]
    assert "handoff" not in payload["context_pack"].get("current_round_context", {})
    handoff = payload["context_pack"]["upstream_handoff"]
    assert handoff["to_stage"] == "expression_design"
    assert "表达关系与确认结构" in handoff["reason"]
    assert "hypothesis" not in handoff["reason"]


def test_hypothesis_stage_tool_evidence_keeps_field_context_without_duplicate_failure_digest():
    compact = svc._compact_stage_tool_evidence_for_prompt(
        stage="hypothesis_design",
        stage_input={
            "selected_theses": [{"thesis_id": "t1", "economic_rationale": "x"}],
            "field_context": {
                "supported_fields": ["ps_ttm", "close", "float_mv", "borrow_money_bal"],
                "blocked_fields": ["raw_open"],
                "aliases": {"ps": "ps_ttm", "cap": "total_mv"},
            },
            "operator_constraints": "Use supported operators only.",
            "known_failure_modes": {"weak_fields": [{"field": "roe", "recent_failure_count": 16}]},
        },
    )

    assert "field_context" not in compact
    assert compact["field_requirements"]["blocked_fields"] == ["raw_open"]
    assert compact["field_requirements"]["aliases"]["ps"] == "ps_ttm"
    assert "known_failure_modes" not in compact


def test_thesis_stage_tool_evidence_does_not_duplicate_history_failure_feedback():
    compact = svc._compact_stage_tool_evidence_for_prompt(
        stage="thesis_design",
        stage_input={
            "available_field_families": ["close", "lg_net_vol"],
            "known_failure_modes": {
                "weak_fields": [{"field": "lg_net_vol", "recent_failure_count": 3}],
                "weak_candidates": [
                    {"candidate_id": "c2", "score": 9.8, "grade": "D", "fields": ["lg_net_vol"]}
                ],
                "policy": ["Do not reuse weak_fields as the main thesis source."],
            },
        },
    )

    assert "known_failure_modes" not in compact


def test_lineage_prompt_normalizes_string_mutation_plan():
    compact = svc._compact_lineage_context_for_prompt(
        {
            "current_hypothesis": [
                {
                    "hypothesis_id": "h1",
                    "signal_claim": "盈利改善",
                    "risk_notes": "避免过拟合",
                    "mutation_plan_if_fail": "加入成交量萎缩过滤",
                }
            ]
        },
        stage="expression_design",
    )

    hypothesis = compact["current_hypothesis"][0]
    assert hypothesis["risk_notes"] == ["避免过拟合"]
    assert hypothesis["mutation_plan_if_fail"] == ["加入成交量萎缩过滤"]


def test_runtime_view_marks_pre_boot_orchestrator_step_as_orphaned(monkeypatch):
    monkeypatch.setattr(svc, "FACTOR_API_BOOT_TS", svc.datetime.fromisoformat("2026-06-14T02:30:00"))

    assert svc._runtime_view_looks_orphaned(
        {
            "run_id": "run-orch",
            "updated_at": "2026-06-14T02:27:28",
            "latest_step": {
                "ts": "2026-06-14T02:27:28",
                "stage": "round_synthesis",
                "tags": ["orchestrator", "deepseek_v4", "llm_request_progress", "round_synthesis"],
            },
        }
    )


def test_runtime_view_pre_boot_step_beats_recent_activity(monkeypatch):
    monkeypatch.setattr(svc, "FACTOR_API_BOOT_TS", svc.datetime.fromisoformat("2026-06-14T02:30:00"))
    monkeypatch.setattr(svc, "_orchestrator_run_has_recent_activity", lambda run_id: True)

    assert svc._runtime_view_looks_orphaned(
        {
            "run_id": "run-orch",
            "updated_at": "2026-06-14T02:27:28",
            "latest_step": {
                "ts": "2026-06-14T02:27:28",
                "stage": "round_synthesis",
                "tags": ["orchestrator", "deepseek_v4", "llm_request_progress", "round_synthesis"],
            },
        }
    )


def test_factor_research_start_orchestrator_starts_background_runner(monkeypatch, tmp_path):
    steps_file, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "factor_research_status", lambda: ok_result(outputs={"registry_summary": {}}))
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    captured = {}

    class FakeThread:
        name = "fake-orchestrator-thread"

    def fake_start(run_id, inputs, contract):
        captured["run_id"] = run_id
        captured["inputs"] = inputs
        captured["contract"] = contract
        svc._write_orchestrator_event(
            {
                "run_id": run_id,
                "round_id": f"{run_id}:r0001",
                "stage_seq": 1,
                "stage_id": f"{run_id}:r0001:s01_protocol_load",
                "stage": "protocol_load",
                "summary": "background started",
                "decision": "continue",
                "stage_transition": {"next_stage": "pre_batch_decision", "next_action": "call_deepseek"},
                "event_type": "checkpoint",
                "tags": ["orchestrator", "protocol_load"],
            }
        )
        return FakeThread()

    monkeypatch.setattr(svc, "_start_orchestrator_background", fake_start)

    result = svc.factor_research_start(
        orchestration_mode="orchestrator",
        llm_model="deepseek-v4-flash",
        direction="研究盈利预期修复",
        n_candidates=2,
        n_rounds=1,
        target_adopted=1,
        top_frac=0.18,
        cost_rate=0.0018,
        rebalance_anchor="2022-01-10",
    )

    assert result.ok
    assert result.outputs["status"] == "running"
    assert captured["inputs"]["orchestration_mode"] == "orchestrator"
    assert captured["inputs"]["llm_model"] == "deepseek-v4-flash"
    assert captured["inputs"]["evaluation_mode"] == "production"
    assert captured["inputs"]["profile_version"] == "production_v1"
    assert captured["inputs"]["evaluation_profile_snapshot"]["factor"]["selection_end_date"] == "2026-06-30"
    assert captured["inputs"]["direction"] == "研究盈利预期修复"
    assert captured["inputs"]["top_frac"] == 0.18
    assert captured["inputs"]["cost_rate"] == 0.0018
    assert captured["inputs"]["rebalance_anchor"] == "2022-01-10"
    assert captured["contract"]["target_adopted"] == 1
    assert captured["contract"]["direction"] == "研究盈利预期修复"
    assert captured["contract"]["top_frac"] == 0.18
    assert captured["contract"]["cost_rate"] == 0.0018
    assert captured["contract"]["rebalance_anchor"] == "2022-01-10"
    assert captured["contract"]["n_candidates"] == 2
    assert captured["contract"]["n_rounds"] == 1
    assert captured["contract"]["llm_model"] == "deepseek-v4-flash"
    assert captured["contract"]["llm_model_selection"]["scope"] == "run_pinned_primary"
    assert captured["contract"]["evaluation_mode"] == "production"
    assert captured["contract"]["evidence_class"] == "discovery_conditioned_rolling"
    assert captured["contract"]["evaluation_contract_hash"]
    launch_spec = svc._latest_orchestrator_launch_spec(captured["run_id"])
    assert launch_spec["inputs"]["direction"] == "研究盈利预期修复"
    assert launch_spec["inputs"]["top_frac"] == 0.18
    assert launch_spec["research_contract"]["cost_rate"] == 0.0018
    assert events_file.exists()
    assert steps_file.exists()
    event = json.loads(events_file.read_text(encoding="utf-8").splitlines()[-1])
    step = json.loads(steps_file.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event_type"] == "checkpoint"
    assert "codex_task" not in event
    assert step["stage"] == "protocol_load"
    assert "codex_task" not in step


def test_factor_research_start_defaults_to_orchestrator(monkeypatch, tmp_path):
    _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    monkeypatch.setattr(svc, "factor_research_status", lambda: ok_result(outputs={"registry_summary": {}}))

    class FakeThread:
        name = "fake-default-orchestrator-thread"
        ident = 123

    monkeypatch.setattr(svc, "_start_orchestrator_background", lambda *args, **kwargs: FakeThread())

    result = svc.factor_research_start(n_candidates=1, n_rounds=1, target_adopted=1)

    assert result.ok
    assert result.outputs["status"] == "running"
    assert result.outputs["evaluation_mode"] == "production"
    assert result.outputs["evidence_class"] == "discovery_conditioned_rolling"
    assert result.inputs["orchestration_mode"] == "orchestrator"
    assert result.inputs["llm_model"] in svc.FACTOR_ORCHESTRATOR_LLM_MODELS


def test_factor_research_start_rejects_unknown_orchestrator_llm_model():
    result = svc.factor_research_start(llm_model="deepseek-unknown")

    assert not result.ok
    assert result.err == "invalid_orchestrator_llm_model"
    assert result.outputs["status"] == "blocked"
    assert result.outputs["allowed_models"] == ["deepseek-v4-pro", "deepseek-v4-flash"]


def test_orchestrator_llm_client_uses_run_pinned_primary_model():
    client = svc._orchestrator_llm_client(
        {"llm_model": "deepseek-v4-flash", "llm_timeout_s": 9999}
    )

    assert client.preferred_model() == "deepseek-v4-flash"
    assert client.timeout == svc.FACTOR_ORCHESTRATOR_LLM_TIMEOUT_MAX


def test_factor_research_config_defaults_updates_allowlisted_fields(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
paths:
  external_root: "/safe/external"
factor_research:
  default_orchestration_mode: "orchestrator"
  default_target_adopted: 10
  default_n_candidates: 10
  default_qgpt_url: "http://127.0.0.1:8003"
llm:
  quant_research:
    api_key: "sk-test-secret"
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(svc, "CONFIG_FILE", config_file)
    monkeypatch.setattr(svc, "load_live_config", lambda: svc.yaml.safe_load(config_file.read_text(encoding="utf-8")))

    result = svc.factor_research_update_config_defaults(
        {
            "target_adopted": 12,
            "n_candidates": 8,
            "holding_period": 5,
            "benchmark": "hs300",
            "top_frac": 0.2,
            "cost_rate": 0.003,
            "qgpt_url": "http://127.0.0.1:8003",
        }
    )

    parsed = svc.yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert result.ok
    assert parsed["factor_research"]["default_orchestration_mode"] == "orchestrator"
    assert parsed["factor_research"]["default_target_adopted"] == 12
    assert parsed["factor_research"]["default_n_candidates"] == 8
    assert parsed["llm"]["quant_research"]["api_key"] == "sk-test-secret"
    assert result.outputs["runtime_defaults"]["target_adopted"] == 12
    assert "api_key" not in json.dumps(result.outputs, ensure_ascii=False).lower()
    assert list(tmp_path.glob("config.yaml.bak.*"))


def test_factor_research_config_defaults_rejects_forbidden_and_unknown_fields(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("factor_research:\n  default_orchestration_mode: orchestrator\n", encoding="utf-8")
    monkeypatch.setattr(svc, "CONFIG_FILE", config_file)

    result = svc.factor_research_update_config_defaults(
        {
            "default_orchestration_mode": "codex_mcp",
            "llm": {"api_key": "secret"},
            "unknown_setting": "x",
        }
    )

    assert not result.ok
    assert result.err == "invalid_factor_config_default_fields"
    assert "default_orchestration_mode" in result.outputs["forbidden_fields"]
    assert "unknown_setting" in result.outputs["unknown_fields"]
    assert config_file.read_text(encoding="utf-8") == "factor_research:\n  default_orchestration_mode: orchestrator\n"


def test_factor_research_runtime_defaults_read_live_config(monkeypatch):
    monkeypatch.setattr(
        svc,
        "load_live_config",
        lambda: {
            "factor_research": {
                "default_qgpt_url": "http://127.0.0.1:8999",
                "default_universe": "zz500",
                "default_start_date": "2021-01-01",
                "default_end_date": "2024-12-31",
                "default_holding_period": 10,
                "default_benchmark": "zz500",
                "default_target_adopted": 7,
                "default_n_candidates": 6,
                "default_n_rounds": 0,
                "default_top_frac": 0.15,
                "default_cost_rate": 0.002,
                "default_orchestration_mode": "orchestrator",
            }
        },
    )

    defaults = svc.factor_research_runtime_defaults()

    assert defaults["qgpt_url"] == "http://127.0.0.1:8999"
    assert defaults["universe"] == "zz500"
    assert defaults["evaluation_mode"] == "production"
    assert defaults["selection_start_date"] == "2022-01-01"
    assert defaults["selection_end_date"] == "2026-06-30"
    assert defaults["holding_period"] == 10
    assert defaults["default_orchestration_mode"] == "orchestrator"
    assert defaults["llm_model"] in svc.FACTOR_ORCHESTRATOR_LLM_MODELS
    assert defaults["llm_model_options"] == ["deepseek-v4-pro", "deepseek-v4-flash"]


def test_factor_research_preflight_blocks_when_quantgpt_unhealthy(monkeypatch):
    monkeypatch.setattr(svc, "_factor_readiness", lambda qgpt_url, allow_quantgpt_restart=False: {"quantgpt_api": {"reachable": False, "url": qgpt_url}})
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    monkeypatch.setattr(svc, "_latest_orchestrator_event_from_file", lambda: {})

    result = svc.factor_research_preflight(qgpt_url="http://127.0.0.1:8003")

    assert result.ok
    assert result.outputs["qgpt_ok"] is False
    assert result.outputs["can_start"] is False
    assert "quantgpt_api_unreachable" in result.outputs["blocking_errors"]
    assert result.outputs["doctor_hint"]


def test_factor_research_preflight_reports_active_orchestrator(monkeypatch):
    monkeypatch.setattr(svc, "_factor_readiness", lambda qgpt_url, allow_quantgpt_restart=False: {"quantgpt_api": {"reachable": True, "url": qgpt_url}})
    monkeypatch.setattr(
        svc,
        "_GUI_RUNS",
        {
            "run-active": {
                "run_id": "run-active",
                "status": "running",
                "stage": "score_review",
                "started_at": "2026-06-17T10:00:00",
                "finished_at": None,
                "inputs": {"orchestration_mode": "orchestrator"},
                "summary": {},
                "latest_event": {"ts": "2026-06-17T10:00:10"},
                "events": deque([{"ts": "2026-06-17T10:00:10", "event": "score_review"}], maxlen=10),
            }
        },
    )
    monkeypatch.setattr(svc, "_orchestrator_thread_alive", lambda run_id: run_id == "run-active")
    monkeypatch.setattr(svc, "_latest_orchestrator_event_from_file", lambda: {})

    result = svc.factor_research_preflight()

    assert result.ok
    assert result.outputs["qgpt_ok"] is True
    assert result.outputs["can_start"] is False
    assert result.outputs["has_active_orchestrator_run"] is True
    assert result.outputs["active_orchestrator_run"]["run_id"] == "run-active"


def test_orchestrator_import_summary_accepts_count_based_payload():
    candidates = [{"candidate_id": "c8"}, {"candidate_id": "c9"}]

    count, items = svc._orchestrator_imported_count_and_items(
        {"imported": 1, "details": [{"factor_id": "f1", "name": "Imported"}]},
        candidates,
    )

    assert count == 1
    assert items == [{"factor_id": "f1", "name": "Imported"}]


def test_orchestrator_import_summary_accepts_legacy_list_payload():
    count, items = svc._orchestrator_imported_count_and_items({"imported": [{"factor_id": "f1"}]})

    assert count == 1
    assert items == [{"factor_id": "f1"}]


def test_orchestrator_import_summary_falls_back_to_details():
    count, items = svc._orchestrator_imported_count_and_items(
        {"details": [{"factor_id": "f1"}, {"status": "skipped_duplicate_active"}]}
    )

    assert count == 1
    assert items == [{"factor_id": "f1"}]


def test_isolated_import_runner_accepts_child_success_payload(monkeypatch):
    def fake_run(args, **kwargs):
        assert "capture_output" not in kwargs
        assert kwargs.get("stdout") is not None
        assert kwargs.get("stderr") is not None
        Path(args[-1]).write_text(
            json.dumps({"ok": True, "result": {"imported": 1, "details": [{"factor_id": "f1"}], "errors": []}}),
            encoding="utf-8",
        )
        return svc.subprocess.CompletedProcess(args=args, returncode=0, stdout="child ok", stderr="")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)

    result = svc._run_import_factors_isolated(
        candidates=[{"expression": "close", "gate_result": {"passed": True}}],
        universe="all_market",
        start_date="2022-01-01",
        end_date="2026-05-31",
        selection_start_date="2022-01-01",
        selection_end_date="2025-06-30",
    )

    assert result["imported"] == 1
    assert result["details"] == [{"factor_id": "f1"}]


def test_isolated_import_runner_reports_timeout_without_import(monkeypatch):
    def fake_run(args, **kwargs):
        raise svc.subprocess.TimeoutExpired(args, kwargs["timeout"], output="stdout tail", stderr="stderr tail")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)

    result = svc._run_import_factors_isolated(
        candidates=[{"expression": "close"}],
        universe="all_market",
        start_date="2022-01-01",
        end_date="2026-05-31",
        selection_start_date="2022-01-01",
        selection_end_date="2025-06-30",
        timeout_s=60,
    )

    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert result["errors"] == ["isolated_import_timeout_after_60s"]
    assert result["details"][0]["status"] == "isolated_import_timeout"


def test_codex_mcp_required_flow_uses_contract_date_fields(monkeypatch, tmp_path):
    steps_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    monkeypatch.setattr(svc, "factor_research_status", lambda: ok_result(outputs={"registry_summary": {}}))

    result = svc.factor_research_start(
        evaluation_mode="research",
        orchestration_mode="codex_mcp",
        n_candidates=1,
        n_rounds=1,
        target_adopted=1,
    )

    assert result.ok
    flow = result.outputs["codex_mcp"]["required_mcp_flow"]
    flow_text = json.dumps(flow, ensure_ascii=False)
    assert not re.search(r"\d{4}-\d{2}-\d{2}", flow_text)
    assert "selection_start_date" in flow_text
    assert "selection_end_date" in flow_text
    assert "value_start_date" in flow_text
    assert "value_end_date" in flow_text
    assert "run_rolling_validation" in flow_text
    assert "required deep-validation evidence" in flow_text
    assert "fxalpha_code_advice(checkpoint=candidate_plan)" in flow_text
    assert "fxalpha_code_advice(checkpoint=score_review)" in flow_text

    step = json.loads(steps_file.read_text(encoding="utf-8").splitlines()[-1])
    assert step["extra"]["research_contract"]["evaluation_mode"] == "research"
    assert step["extra"]["research_contract"]["selection_start_date"] == "2022-01-01"
    assert step["extra"]["research_contract"]["selection_end_date"] == "2024-12-31"


def test_codex_mcp_start_blocks_active_orchestrator_without_clearing_live_journals(monkeypatch, tmp_path):
    steps_file, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    traces_file = svc.FACTOR_ORCHESTRATOR_LLM_TRACES_FILE
    for path, value in (
        (steps_file, "active-step\n"),
        (events_file, "active-event\n"),
        (traces_file, "active-trace\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    monkeypatch.setattr(svc, "factor_research_status", lambda: ok_result(outputs={"registry_summary": {}}))
    monkeypatch.setattr(
        svc,
        "_GUI_RUNS",
        {
            "run-existing": {
                "run_id": "run-existing",
                "status": "running",
                "stage": "score_review",
                "started_at": "2026-07-26T10:00:00",
                "finished_at": None,
                "inputs": {"orchestration_mode": "orchestrator"},
                "summary": {},
                "latest_event": {"ts": "2026-07-26T10:01:00"},
                "events": deque([{"ts": "2026-07-26T10:01:00", "event": "score_review"}], maxlen=10),
                "guidance_history": [],
                "latest_result": None,
            }
        },
    )
    monkeypatch.setattr(svc, "_orchestrator_thread_alive", lambda run_id: run_id == "run-existing")

    result = svc.factor_research_start(
        orchestration_mode="codex_mcp",
        n_candidates=1,
        n_rounds=1,
        target_adopted=1,
    )

    assert not result.ok
    assert result.err == "orchestrator_run_already_active"
    assert result.outputs["active_run_id"] == "run-existing"
    assert steps_file.read_text(encoding="utf-8") == "active-step\n"
    assert events_file.read_text(encoding="utf-8") == "active-event\n"
    assert traces_file.read_text(encoding="utf-8") == "active-trace\n"


def test_codex_mcp_start_never_clears_orchestrator_event_or_trace_caches(monkeypatch, tmp_path):
    steps_file, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    traces_file = svc.FACTOR_ORCHESTRATOR_LLM_TRACES_FILE
    events_file.parent.mkdir(parents=True, exist_ok=True)
    traces_file.parent.mkdir(parents=True, exist_ok=True)
    events_file.write_text("preserved-event\n", encoding="utf-8")
    traces_file.write_text("preserved-trace\n", encoding="utf-8")
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    monkeypatch.setattr(svc, "factor_research_status", lambda: ok_result(outputs={"registry_summary": {}}))

    result = svc.factor_research_start(
        orchestration_mode="codex_mcp",
        n_candidates=1,
        n_rounds=1,
        target_adopted=1,
    )

    assert result.ok
    assert result.outputs["status"] == "waiting_codex_mcp"
    assert "protocol_load" in steps_file.read_text(encoding="utf-8")
    assert events_file.read_text(encoding="utf-8") == "preserved-event\n"
    assert traces_file.read_text(encoding="utf-8") == "preserved-trace\n"


def test_factor_tool_code_advice_reuses_shared_quick_and_candidate_plan_logic(monkeypatch):
    monkeypatch.setattr(
        svc,
        "factor_tool_context",
        lambda **kwargs: ok_result(outputs={"active_factor_summary": {"active_factors": []}}),
    )
    monkeypatch.setattr(svc, "_prior_round_expression_refs", lambda run_id, round_id: {})

    plan = svc.factor_tool_code_advice(
        checkpoint="candidate_plan",
        candidates=[
            {"candidate_id": "c1", "expression": "rank(close)"},
            {"candidate_id": "c2", "expression": "rank(close)"},
        ],
        run_id="run-test",
        round_id="run-test:r0001",
    )
    score = svc.factor_tool_code_advice(
        checkpoint="score_review",
        candidates=[
            {
                "candidate_id": "c1",
                "expression": "rank(close)",
                "score": 72,
                "grade": "B",
                "key_metrics": {"ic_mean": 0.03, "ic_ir": 0.7},
            }
        ],
    )

    assert plan.ok
    assert plan.outputs["fatal_precheck_is_code_owned"] is True
    plan_advice = plan.outputs["advice"]
    assert plan_advice["blocked_count"] == 1
    assert plan_advice["candidate_lane_decisions"][1]["candidate_lane"] == "precheck_blocked"
    assert score.ok
    assert score.outputs["shared_logic_source"] == "domain.factor_research.orchestrator"
    assert score.outputs["advice"] == orchestrator.quick_advice(
        [
            {
                "candidate_id": "c1",
                "expression": "rank(close)",
                "score": 72,
                "grade": "B",
                "key_metrics": {"ic_mean": 0.03, "ic_ir": 0.7},
            }
        ],
        trajectory=[],
    )


def test_fxalpha_context_contract_marks_rolling_validation_required():
    contract = svc._fxalpha_must_read_contract()
    raw = json.dumps(contract, ensure_ascii=False)

    assert "run_rolling_validation as required" in raw
    assert "rolling-validation, economic_thesis" not in raw
    assert "archived/manual diagnostic" not in raw


def test_list_operators_exposes_current_local_field_schema():
    from quantgpt import mcp_server as quantgpt_mcp

    text = quantgpt_mcp.list_operators()
    schema = json.loads(quantgpt_mcp._current_data_schema_doc())

    for field in ("net_mf_amount", "cost_85pct", "free_share", "short_balance"):
        assert field in text
    assert '"market_cap": "total_mv"' in text
    assert '"blocked_fields"' in text
    assert "backward_factor" not in schema["available_fields"]
    assert "backward_factor" in schema["blocked_fields"]


def test_operator_catalog_parser_and_candidate_plan_are_fully_consistent():
    from quantgpt import expression_parser as expression_mod
    from quantgpt import mcp_server as quantgpt_mcp

    documented_raw = set(
        re.findall(
            r"^-\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            quantgpt_mcp.list_operators(),
            flags=re.MULTILINE,
        )
    )
    documented_canonical = {
        expression_mod.ExpressionParser._OPERATOR_ALIASES.get(
            name,
            expression_mod.ExpressionParser._OPERATOR_ALIASES.get(name.lower(), name.lower()),
        )
        for name in documented_raw
    }
    parser_local_catalog = (
        set(expression_mod._WQ_OPERATORS)
        - set(expression_mod._WQ_REMOTE_ONLY_OPS)
    ) | set(expression_mod._LOCAL_ONLY_OPERATORS)

    assert documented_canonical == parser_local_catalog
    assert documented_canonical <= svc._FACTOR_EXPRESSION_FUNCTIONS
    assert svc._FACTOR_EXPRESSION_FUNCTIONS - documented_canonical == {
        "and",
        "delay",
        "mean",
        "or",
    }


def test_candidate_plan_code_precheck_accepts_catalogued_atr_operator():
    checks = svc._candidate_plan_code_precheck(
        [
            {
                "candidate_id": "atr_candidate",
                "expression": "rank(-atr(14) / max(close, 0.01)) * rank(-pb)",
            }
        ]
    )

    assert checks == []


def test_quantgpt_mcp_exposes_shared_code_advice_tool(monkeypatch):
    from quantgpt import mcp_server as quantgpt_mcp

    monkeypatch.setattr(
        quantgpt_mcp,
        "_fxalpha_factor_service",
        lambda: types.SimpleNamespace(
            factor_tool_code_advice=lambda **kwargs: ok_result(
                outputs={"checkpoint": kwargs["checkpoint"], "candidate_count": len(kwargs["candidates"])}
            )
        ),
    )

    payload = json.loads(
        quantgpt_mcp.fxalpha_code_advice(
            checkpoint="score_review",
            candidates=[{"candidate_id": "c1", "expression": "rank(close)"}],
        )
    )

    assert payload["ok"] is True
    assert payload["outputs"] == {"checkpoint": "score_review", "candidate_count": 1}


def test_factor_research_start_orchestrator_reuses_existing_active_run(monkeypatch, tmp_path):
    _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "factor_research_status", lambda: ok_result(outputs={"registry_summary": {}}))
    monkeypatch.setattr(
        svc,
        "_GUI_RUNS",
        {
            "run-existing": {
                "run_id": "run-existing",
                "status": "running",
                "stage": "score_review",
                "started_at": "2026-06-14T04:30:00",
                "finished_at": None,
                "inputs": {"orchestration_mode": "orchestrator"},
                "summary": {"thread": "fxalpha-orchestrator-run-existing"},
                "latest_event": {"ts": "2026-06-14T04:31:00", "event": "score_review"},
                "events": deque([{"ts": "2026-06-14T04:31:00", "event": "score_review"}], maxlen=10),
                "guidance_history": [],
                "latest_result": None,
            }
        },
    )
    monkeypatch.setattr(svc, "_orchestrator_thread_alive", lambda run_id: run_id == "run-existing")
    monkeypatch.setattr(svc, "_start_orchestrator_background", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not start second orchestrator")))

    result = svc.factor_research_start(orchestration_mode="orchestrator", n_candidates=2, n_rounds=3, target_adopted=2)

    assert result.ok
    assert result.outputs["status"] == "running"
    assert result.outputs["run_id"] == "run-existing"
    assert result.outputs["deduplicated"] is True
    assert "orchestrator_run_already_active" in result.warnings


def test_round_synthesis_stop_reason_uses_round_budget_label():
    result = svc._normalize_llm_stage_result(
        "round_synthesis",
        {"decision": "continue_next_round", "stage_transition": {}},
        default_next_stage="checkpoint_stop",
        default_next_action="stop_run",
        stop_reason="round_budget_reached",
    )

    assert result["decision"] == "round_budget_reached"
    assert result["next_action"] == "stop_run"
    assert result["stage_transition"]["next_stage"] == "checkpoint_stop"


def test_round_synthesis_does_not_stop_on_research_stagnation_when_continuing():
    result = svc._normalize_llm_stage_result(
        "round_synthesis",
        {
            "decision": "blocker",
            "next_action": "block_for_human",
            "why": "several rounds failed to find a keeper",
            "stage_transition": {"next_stage": "blocker_review", "reason": "research stagnation"},
        },
        default_next_stage="thesis_design",
        default_next_action="start_next_round",
    )

    assert result["decision"] == "continue_next_round"
    assert result["next_action"] == "start_next_round"
    assert result["stage_transition"]["next_stage"] == "thesis_design"


def test_precise_return_handoff_preserves_non_thesis_resume_targets():
    score_review = {
        "decision": "mutate",
        "next_action": "return_hypothesis_design",
        "why": "quick score has signal but normalization policy is unstable",
        "stage_transition": {"next_stage": "hypothesis_design", "reason": "rewrite signal direction and normalization"},
    }

    handoff = svc._return_handoff_from_stage("score_review", score_review)
    next_stage, next_action = svc._round_synthesis_defaults(
        return_handoff=handoff,
        round_no=1,
        inputs={"n_rounds": 3, "target_adopted": 2},
        adopted_total=0,
    )
    normalized = svc._normalize_llm_stage_result(
        "round_synthesis",
        {
            "decision": "continue_next_round",
            "next_action": "start_next_round_at_hypothesis_design",
            "stage_transition": {"next_stage": "hypothesis_design", "reason": "continue from hypothesis"},
        },
        default_next_stage=next_stage,
        default_next_action=next_action,
    )

    assert handoff["to_stage"] == "hypothesis_design"
    assert next_stage == "hypothesis_design"
    assert next_action == "start_next_round_at_hypothesis_design"
    assert normalized["stage_transition"]["next_stage"] == "hypothesis_design"


def test_round_synthesis_cannot_stop_run_while_code_budget_remains():
    result = svc._round_synthesis_resume_transition(
        {
            "decision": "stop_target_reached",
            "next_action": "stop_run",
            "round_memory": {
                "suggested_start_stage": "checkpoint_stop",
                "promising_parents": ["r0002:c6"],
            },
            "stage_transition": {
                "next_stage": "checkpoint_stop",
                "reason": "the current parent has no further value",
            },
        },
        fallback_next_stage="expression_design",
        fallback_next_action="start_next_round_at_expression_design",
    )

    assert result["decision"] == "continue_next_round"
    assert result["next_action"] == "start_next_round"
    assert result["stage_transition"]["next_stage"] == "thesis_design"
    assert result["round_memory"]["suggested_start_stage"] == "thesis_design"
    assert result["round_memory"]["promising_parents"] == []


def test_round_synthesis_obeys_code_owned_stop():
    result = svc._round_synthesis_resume_transition(
        {
            "decision": "continue_next_round",
            "next_action": "start_next_round",
            "round_memory": {"suggested_start_stage": "thesis_design"},
            "stage_transition": {
                "next_stage": "thesis_design",
                "reason": "model wants another round",
            },
        },
        fallback_next_stage="checkpoint_stop",
        fallback_next_action="stop_run",
    )

    assert result["decision"] == "round_budget_reached"
    assert result["next_action"] == "stop_run"
    assert result["stage_transition"]["next_stage"] == "checkpoint_stop"
    assert result["stage_transition"]["resume_policy"] == "code_owned_run_stop"


def test_round_entry_handoff_targets_exactly_one_stage():
    handoff = {"to_stage": "hypothesis_design", "reason": "rewrite roles"}

    assert svc._handoff_targets_stage(handoff, "thesis_design") is False
    assert svc._handoff_targets_stage(handoff, "hypothesis_design") is True
    assert svc._handoff_targets_stage(handoff, "expression_design") is False


def test_import_gate_deep_return_maps_to_safe_expression_resume():
    gate_review = {
        "decision": "complete_evidence",
        "next_action": "return_deep_validation",
        "why": "gate reports missing deep evidence",
        "stage_transition": {"next_stage": "deep_validation_review", "reason": "complete missing deep refs"},
    }

    handoff = svc._return_handoff_from_stage("import_gate_review", gate_review)
    outcome = svc._authoritative_outcome_from_llm(
        from_stage="import_gate_review",
        result=gate_review,
        fallback_next_stage="expression_design",
        fallback_next_action="start_next_round_at_expression_design",
    )
    next_stage, next_action = svc._round_synthesis_defaults(
        return_handoff=handoff,
        round_no=1,
        inputs={"n_rounds": 3, "target_adopted": 2},
        adopted_total=0,
    )

    assert outcome["required_next_stage"] == "deep_validation_review"
    assert handoff["to_stage"] == "deep_validation_review"
    assert next_stage == "expression_design"
    assert next_action == "start_next_round_at_expression_design"


def test_context_budget_compresses_round_synthesis_payload():
    huge_events = [
        {
            "stage": "deep_validation_review",
            "summary": "s" * 5000,
            "decision": "mutate",
            "stage_transition": {"next_stage": "expression_design", "why": "w" * 3000},
            "candidate_lanes": [{"candidate_id": f"c{i}", "expression": "rank(close)" * 200}],
        }
        for i in range(30)
    ]
    payload = svc._apply_orchestrator_context_budget(
        {
            "task": "round_synthesis",
            "stage_briefing": "总结 round",
            "context_pack": {
                "history_context": {
                    "short_term_history": {
                        "stage_relevant_steps": huge_events,
                        "review_anchors": huge_events,
                        "negative_precedents": {"weak_candidates": [{"expression": "rank(close)" * 100, "score": 10} for _ in range(20)]},
                    },
                },
                "current_round_context": {"handoff": {"to_stage": "expression_design", "reason": "r" * 1000}},
                "tool_evidence": {"deep": huge_events, "failed_candidates": [{"candidate_id": f"c{i}", "expression": "rank(close)" * 200} for i in range(20)]},
            },
            "output_contract": {"required_fields": ["decision", "stage_transition"], "schema_example": {"x": "y" * 1000}},
        },
        stage="round_synthesis",
    )

    budget = payload["_context_budget"]
    assert budget["compressed"] is True
    assert budget["after_chars"] < budget["before_chars"]
    assert budget["after_chars"] <= budget["max_payload_chars"]
    visible_payload = svc._llm_visible_payload(payload)
    assert "context_budget" not in visible_payload
    assert "_context_budget" not in visible_payload


def test_stage_event_progress_keeps_heartbeat_in_event_journal(monkeypatch, tmp_path):
    steps_file, events_file = _redirect_orchestrator(monkeypatch, tmp_path)

    svc._orchestrator_stage_event(
        run_id="run-orch",
        round_id="run-orch:r0001",
        stage_seq=3,
        stage="score_review",
        previous_stage="candidate_plan",
        previous_stage_id="run-orch:r0001:s02_candidate_plan",
        summary="score tool running",
        decision="wait for score",
        next_stage="score_review",
        next_action="validate_and_score_in_progress",
        event_type="checkpoint",
        evidence_refs=[{"tool": "score_factor", "candidate_id": "c1", "score": 66}],
        tags=["tool_progress"],
        candidate_lanes=[{"candidate_id": "c1", "expression": "rank(close)", "score": 66}],
    )

    event = json.loads(events_file.read_text(encoding="utf-8").splitlines()[-1])
    assert event["heartbeat_status"] == "alive"
    assert event["thread_id"]
    assert event["sync_status"] == "event_only_progress"
    assert not steps_file.exists()


def test_orchestrator_candidate_progress_events_do_not_create_research_steps(monkeypatch, tmp_path):
    _, events_file = _redirect_orchestrator(monkeypatch, tmp_path)

    for idx in (1, 2):
        svc._orchestrator_stage_event(
            run_id="run-orch",
            round_id="run-orch:r0001",
            stage_seq=5,
            stage="score_review",
            previous_stage="score_review",
            previous_stage_id="run-orch:r0001:s05_score_review",
            summary=f"score progress {idx}/2",
            decision=f"score candidate {idx}",
            next_stage="score_review",
            next_action="validate_and_score_in_progress",
            event_type="checkpoint",
            evidence_refs=[{"tool": "score_factor", "candidate_index": idx, "candidate_id": f"c{idx}"}],
            tags=["tool_progress", "score_review_progress", "candidate_progress"],
            stage_id_suffix=f"candidate_{idx}_c{idx}",
            candidate_lanes=[{"candidate_id": f"c{idx}", "expression": f"rank(close/{idx})"}],
        )

    steps = svc._read_recent_research_steps(limit=4)
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]

    assert steps == []
    assert len(events) == 2
    assert all(event["sync_status"] == "event_only_progress" for event in events)
    assert {event["evidence_refs"][0]["candidate_index"] for event in events} == {1, 2}


def test_read_recent_research_steps_keeps_legacy_candidate_progress_with_shared_stage_id(monkeypatch, tmp_path):
    steps_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)
    steps_file.parent.mkdir(parents=True, exist_ok=True)
    shared_stage_id = "run-orch:r0001:s05_score_review"
    steps_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "stage_id": shared_stage_id,
                        "stage": "score_review",
                        "ts": "2026-06-14T10:00:00",
                        "tags": ["candidate_progress"],
                        "evidence_refs": [{"tool": "score_factor", "candidate_index": 1, "candidate_id": "c1"}],
                    }
                ),
                json.dumps(
                    {
                        "stage_id": shared_stage_id,
                        "stage": "score_review",
                        "ts": "2026-06-14T10:01:00",
                        "tags": ["candidate_progress"],
                        "evidence_refs": [{"tool": "score_factor", "candidate_index": 2, "candidate_id": "c2"}],
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    steps = svc._read_recent_research_steps(limit=4)

    assert [step["evidence_refs"][0]["candidate_index"] for step in steps] == [2, 1]


def test_stale_orchestrator_run_is_marked_interrupted(monkeypatch, tmp_path):
    steps_file, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "FACTOR_API_BOOT_TS", datetime(2026, 6, 14, 12, 0, 0))
    svc._write_orchestrator_event(
        {
            "ts": "2026-06-14T11:55:00",
            "run_id": "run-stale",
            "round_id": "run-stale:r0001",
            "stage_seq": 5,
            "stage_id": "run-stale:r0001:s05_score_review",
            "stage": "score_review",
            "summary": "running before API restart",
            "decision": "running",
            "stage_transition": {"next_stage": "score_review", "next_action": "llm_review_in_progress", "llm_trace_id": "trace-score"},
            "event_type": "llm_request",
            "llm_trace_id": "trace-score",
            "candidate_lanes": [{"candidate_id": "c1", "expression": "rank(close)", "score": 66}],
            "evidence_refs": [{"tool": "score_factor", "candidate_id": "c1", "score": 66}],
            "tags": ["orchestrator", "deepseek_v4"],
        }
    )

    interrupted = svc._mark_stale_orchestrator_run_interrupted(stale_seconds=3600)

    assert interrupted["stage"] == "blocker"
    assert "orchestrator_interrupted" in interrupted["tags"]
    interrupted_ref = interrupted["evidence_refs"][0]
    assert interrupted_ref["interrupted_run_id"] == "run-stale"
    assert interrupted_ref["interrupt_reason"] == "interrupted_by_api_restart"
    assert interrupted_ref["legacy_interrupt_reason"] == "api_boot_mismatch"
    assert interrupted_ref["last_llm_trace_id"] == "trace-score"
    assert interrupted_ref["last_candidate_lanes"][0]["candidate_id"] == "c1"
    assert interrupted_ref["last_evidence_refs"][0]["tool"] == "score_factor"
    step = json.loads(steps_file.read_text(encoding="utf-8").splitlines()[-1])
    assert step["stage"] == "blocker"
    assert step["monitoring"]["heartbeat_status"] == "interrupted"
    assert events_file.exists()


def test_completed_run_worker_exit_is_not_reclassified_as_interrupted(monkeypatch, tmp_path):
    _, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "FACTOR_API_BOOT_TS", datetime(2026, 6, 14, 12, 0, 0))
    terminal = {
        "ts": "2026-06-14T11:55:00",
        "run_id": "run-complete",
        "round_id": "run-complete:stop",
        "stage_seq": 99,
        "stage_id": "run-complete:stop:checkpoint_stop",
        "stage": "checkpoint_stop",
        "summary": "target reached",
        "decision": "stop_target_reached",
        "stage_transition": {"next_stage": "checkpoint_stop", "next_action": "idle"},
        "event_type": "checkpoint",
        "tags": ["orchestrator", "checkpoint_stop"],
    }
    svc._write_orchestrator_event(terminal)
    svc._write_orchestrator_event(
        {
            "ts": "2026-06-14T11:55:01",
            "run_id": "run-complete",
            "round_id": "run-complete:control",
            "stage_seq": 0,
            "stage_id": "run-complete:control:worker_exited",
            "stage": "orchestrator_worker",
            "summary": "worker exited",
            "decision": "worker_exited",
            "stage_transition": {"next_stage": "checkpoint_stop", "next_action": "idle"},
            "event_type": "orchestrator_worker",
            "tags": ["orchestrator", "orchestrator_worker"],
        }
    )

    candidate = svc._latest_orchestrator_interruption_candidate()
    preview = svc._orchestrator_stale_preview(stale_seconds=60)
    interrupted = svc._mark_stale_orchestrator_run_interrupted(stale_seconds=60)

    assert candidate["stage_id"] == terminal["stage_id"]
    assert preview["stale"] is False
    assert preview["latest_event"]["stage"] == "checkpoint_stop"
    assert interrupted == {}
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    assert all(event.get("stage") != "blocker" for event in events)


def test_stale_orchestrator_marker_skips_active_in_process_job(monkeypatch, tmp_path):
    steps_file, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "FACTOR_API_BOOT_TS", datetime(2026, 6, 14, 12, 0, 0))
    monkeypatch.setattr(
        svc,
        "_GUI_RUNS",
        {
            "run-live": {
                "run_id": "run-live",
                "status": "running",
                "stage": "score_review",
                "started_at": "2026-06-14T11:50:00",
                "finished_at": None,
                "inputs": {"orchestration_mode": "orchestrator"},
                "summary": {},
                "latest_event": {"ts": "2026-06-14T11:55:00"},
                "events": deque([{"ts": "2026-06-14T11:55:00", "event": "score_review"}], maxlen=10),
            }
        },
    )
    monkeypatch.setattr(svc, "_orchestrator_thread_alive", lambda run_id: run_id == "run-live")
    svc._write_orchestrator_event(
        {
            "ts": "2026-06-14T11:55:00",
            "run_id": "run-live",
            "round_id": "run-live:r0001",
            "stage_seq": 5,
            "stage_id": "run-live:r0001:s05_score_review",
            "stage": "score_review",
            "summary": "long score is still running in this API process",
            "decision": "running",
            "stage_transition": {"next_stage": "score_review", "next_action": "validate_and_score_in_progress"},
            "event_type": "tool_call",
            "tags": ["orchestrator"],
        }
    )

    preview = svc._orchestrator_stale_preview(stale_seconds=60)
    interrupted = svc._mark_stale_orchestrator_run_interrupted(stale_seconds=60)

    assert preview["stale"] is False
    assert preview["active_in_current_process"] is True
    assert interrupted == {}
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    assert all(event.get("stage") != "blocker" for event in events)
    assert not steps_file.exists() or all(
        json.loads(line).get("stage") != "blocker"
        for line in steps_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def test_orphaned_llm_request_trace_is_marked_interrupted(monkeypatch, tmp_path):
    steps_file, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "FACTOR_API_BOOT_TS", datetime(2026, 6, 14, 12, 0, 0))
    svc._write_orchestrator_event(
        {
            "ts": "2026-06-14T11:50:00",
            "run_id": "run-orphan",
            "round_id": "run-orphan:r0001",
            "stage_seq": 3,
            "stage_id": "run-orphan:r0001:s03_hypothesis_design",
            "stage": "hypothesis_design",
            "summary": "hypothesis done",
            "decision": "advance",
            "stage_transition": {"next_stage": "expression_design", "next_action": "advance_to_expression_design"},
            "event_type": "llm_result",
            "tags": ["orchestrator", "deepseek_v4"],
        }
    )
    svc._write_orchestrator_llm_trace(
        {
            "ts": "2026-06-14T11:55:00",
            "trace_id": "run-orphan:r0001:expression_design:trace1234",
            "run_id": "run-orphan",
            "round_id": "run-orphan:r0001",
            "stage": "expression_design",
            "checkpoint": "expression_design",
            "event_type": "llm_request",
            "llm_model": "deepseek-v4-flash",
        }
    )
    svc._write_orchestrator_llm_request_step(
        run_id="run-orphan",
        round_id="run-orphan:r0001",
        stage="expression_design",
        checkpoint="expression_design",
        trace_id="run-orphan:r0001:expression_design:trace1234",
        payload_chars=123,
        llm_model="deepseek-v4-flash",
        prompt_digest={"allowed_next_stages": ["candidate_plan"]},
    )

    preview = svc._orchestrator_stale_preview(stale_seconds=3600)
    interrupted = svc._mark_stale_orchestrator_run_interrupted(stale_seconds=3600)

    assert preview["stale"] is True
    assert preview["latest_event"]["stage"] == "expression_design"
    assert interrupted["stage"] == "blocker"
    interrupted_ref = interrupted["evidence_refs"][0]
    assert interrupted_ref["interrupted_run_id"] == "run-orphan"
    assert interrupted_ref["last_stage"] == "expression_design"
    assert interrupted_ref["last_llm_trace_id"] == "run-orphan:r0001:expression_design:trace1234"
    assert interrupted_ref["last_evidence_refs"][0]["type"] == "orphaned_llm_request"
    step = json.loads(steps_file.read_text(encoding="utf-8").splitlines()[-1])
    assert step["stage"] == "blocker"
    assert events_file.exists()


def test_orchestrator_interruption_marker_is_idempotent(monkeypatch, tmp_path):
    steps_file, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "FACTOR_API_BOOT_TS", datetime(2026, 6, 14, 12, 0, 0))
    svc._write_orchestrator_event(
        {
            "ts": "2026-06-14T11:55:00",
            "run_id": "run-idempotent",
            "round_id": "run-idempotent:r0001",
            "stage_seq": 5,
            "stage_id": "run-idempotent:r0001:s05_score_review",
            "stage": "score_review",
            "summary": "running before API restart",
            "decision": "running",
            "stage_transition": {"next_stage": "score_review", "next_action": "llm_review_in_progress"},
            "event_type": "llm_request",
            "tags": ["orchestrator", "deepseek_v4"],
        }
    )

    first = svc._mark_stale_orchestrator_run_interrupted(stale_seconds=3600)
    second = svc._mark_stale_orchestrator_run_interrupted(stale_seconds=3600)

    assert first["stage"] == "blocker"
    assert second["stage"] == "blocker"
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    steps = [json.loads(line) for line in steps_file.read_text(encoding="utf-8").splitlines()]
    assert sum(1 for event in events if event.get("stage") == "blocker") == 1
    assert sum(1 for step in steps if step.get("stage") == "blocker") == 1


def test_factor_research_status_reports_orphaned_step_without_writing_state(monkeypatch, tmp_path):
    steps_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "FACTOR_API_BOOT_TS", datetime(2026, 6, 14, 12, 0, 0))
    monkeypatch.setattr(svc, "_factor_readiness", lambda qgpt_url, **kwargs: {"quantgpt_api": {"reachable": True}})
    monkeypatch.setattr(svc, "_api_18081_owner_status", lambda: {})
    monkeypatch.setattr(svc, "factor_research_runtime_defaults", lambda: {"holding_period": 5})
    monkeypatch.setattr(svc, "factor_active_values_status", lambda holding_period_days=5: ok_result(outputs={}))
    monkeypatch.setattr(svc, "_fetch_quantgpt_recent_tasks", lambda **kwargs: [])
    monkeypatch.setattr(svc, "_code_advice_alignment_summary", lambda: {})
    monkeypatch.setattr(svc, "_orchestrator_run_has_recent_activity", lambda run_id, **kwargs: False)

    class _Registry:
        def summary(self):
            return {}

    monkeypatch.setattr(svc, "FactorRegistry", _Registry)
    svc._write_research_step(
        {
            "schema_version": "research_step_v2",
            "ts": "2026-06-14T11:55:00",
            "run_id": "run-status",
            "round_id": "run-status:r0001",
            "stage_seq": 2,
            "stage_id": "run-status:r0001:req_thesis_trace1234",
            "previous_stage": "protocol_load",
            "previous_stage_id": "run-status:r0001:s01_protocol_load",
            "stage": "thesis_design",
            "summary": "waiting for llm",
            "decision": "llm_request_in_progress",
            "evidence_refs": [],
            "tags": ["orchestrator", "deepseek_v4", "llm_request_progress", "thesis_design"],
            "stage_transition": {"next_stage": "thesis_design", "next_action": "llm_review_in_progress"},
        }
    )

    result = svc.factor_research_status().to_dict()

    assert result["outputs"]["status"] == "research_blocked"
    assert result["outputs"]["runtime_view"]["status"] == "research_blocked"
    assert result["outputs"]["runtime_view"]["current_action"] == "已中断，需重启"
    persisted_steps = [json.loads(line) for line in steps_file.read_text(encoding="utf-8").splitlines()]
    assert len(persisted_steps) == 1
    assert persisted_steps[0]["stage"] == "thesis_design"


def test_start_orchestrator_passes_interrupted_handoff_to_runner(monkeypatch, tmp_path):
    _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "FACTOR_API_BOOT_TS", datetime(2026, 6, 14, 12, 0, 0))
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    monkeypatch.setattr(svc, "factor_research_status", lambda: ok_result(outputs={"registry_summary": {}}))
    captured = {}

    svc._write_orchestrator_event(
        {
            "ts": "2026-06-14T11:50:00",
            "run_id": "run-stale",
            "round_id": "run-stale:r0001",
            "stage_seq": 8,
            "stage_id": "run-stale:r0001:s08_deep_validation_review",
            "stage": "deep_validation_review",
            "summary": "lost before API restart",
            "decision": "running",
            "stage_transition": {"next_stage": "deep_validation_review", "next_action": "llm_review_in_progress", "llm_trace_id": "trace-deep"},
            "event_type": "llm_request",
            "llm_trace_id": "trace-deep",
            "candidate_lanes": [{"candidate_id": "c9", "expression": "rank(amount)", "deep_score": 79}],
            "evidence_refs": [{"tool": "deep_validation", "candidate_id": "c9", "deep_score": 79}],
            "tags": ["orchestrator", "deepseek_v4"],
        }
    )

    class FakeThread:
        name = "fake-orchestrator-thread"
        ident = 123

    def fake_start(run_id, inputs, contract):
        captured["contract"] = contract
        return FakeThread()

    monkeypatch.setattr(svc, "_start_orchestrator_background", fake_start)

    result = svc.factor_research_start(orchestration_mode="orchestrator", n_candidates=1, n_rounds=1, target_adopted=1)

    assert result.ok
    handoff = captured["contract"]["interrupted_handoff"]
    assert handoff["from_stage"] == "orchestrator_interrupted"
    assert "deep_validation_review" in handoff["reason"]
    assert "run-stale:r0001:s08_deep_validation_review" in handoff["must_preserve"]
    assert "trace-deep" in handoff["must_preserve"]
    assert handoff["last_stage_transition"]["next_stage"] == "deep_validation_review"
    assert handoff["last_candidate_lanes"][0]["candidate_id"] == "c9"
    assert handoff["last_evidence_refs"][0]["tool"] == "deep_validation"


def test_start_orchestrator_can_resume_the_same_interrupted_run(monkeypatch, tmp_path):
    _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    monkeypatch.setattr(svc, "factor_research_status", lambda: ok_result(outputs={"registry_summary": {}}))
    checkpoint = {
        "type": "orchestrator_recovery_checkpoint",
        "round_id": "run-resume-api:r0001",
        "candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
        "planned_candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
        "candidate_plan": {"candidate_lanes": [{"candidate_id": "c1", "keep": True}]},
        "candidate_precheck": [],
    }
    svc._write_orchestrator_event(
        {
            "ts": "2026-06-14T11:50:00",
            "run_id": "run-resume-api",
            "round_id": "run-resume-api:r0001",
            "stage_seq": 5,
            "stage_id": "run-resume-api:r0001:s05_score_review",
            "stage": "score_review",
            "summary": "interrupted score worker",
            "decision": "running",
            "stage_transition": {"next_stage": "score_review", "next_action": "validate_and_score_in_progress"},
            "event_type": "checkpoint",
            "evidence_refs": [checkpoint],
            "tags": ["orchestrator", "tool_progress"],
        }
    )
    interrupted = svc._mark_stale_orchestrator_run_interrupted(run_id="run-resume-api", stale_seconds=0)
    assert interrupted["stage"] == "blocker"
    captured = {}

    class FakeThread:
        name = "fake-resume-thread"
        ident = 12

    def fake_start(run_id, inputs, contract):
        captured.update({"run_id": run_id, "inputs": inputs, "contract": contract})
        return FakeThread()

    monkeypatch.setattr(svc, "_start_orchestrator_background", fake_start)

    result = svc.factor_research_start(
        orchestration_mode="orchestrator",
        n_candidates=1,
        n_rounds=1,
        target_adopted=1,
        resume_run_id="run-resume-api",
    )

    assert result.ok
    assert result.outputs["run_id"] == "run-resume-api"
    assert captured["run_id"] == "run-resume-api"
    assert captured["contract"]["interrupted_handoff"]["recovery_checkpoint"]["planned_candidates"][0]["candidate_id"] == "c1"


def test_fresh_run_does_not_inherit_stale_recovery_round(monkeypatch, tmp_path):
    _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    monkeypatch.setattr(svc, "factor_research_status", lambda: ok_result(outputs={"registry_summary": {}}))
    checkpoint = {
        "type": "orchestrator_recovery_checkpoint",
        "round_id": "run-old:r0027",
        "candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
        "planned_candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
        "candidate_plan": {"candidate_lanes": [{"candidate_id": "c1", "keep": True}]},
        "candidate_precheck": [],
    }
    svc._write_orchestrator_event({
        "ts": "2026-06-14T11:50:00",
        "run_id": "run-old",
        "round_id": "run-old:r0027",
        "stage_seq": 5,
        "stage_id": "run-old:r0027:s05_score_review",
        "stage": "score_review",
        "summary": "interrupted score worker",
        "decision": "running",
        "stage_transition": {"next_stage": "score_review", "next_action": "validate_and_score_in_progress"},
        "event_type": "checkpoint",
        "evidence_refs": [checkpoint],
        "tags": ["orchestrator", "tool_progress"],
    })
    captured = {}

    class FakeThread:
        name = "fake-fresh-thread"
        ident = 13

    def fake_start(run_id, inputs, contract):
        captured["contract"] = contract
        return FakeThread()
    monkeypatch.setattr(svc, "_start_orchestrator_background", fake_start)

    result = svc.factor_research_start(
        orchestration_mode="orchestrator",
        n_candidates=1,
        n_rounds=5,
        target_adopted=10,
    )

    assert result.ok
    handoff = captured["contract"]["interrupted_handoff"]
    assert handoff["to_stage"] == "thesis_design"
    assert "recovery_checkpoint" not in handoff


def test_background_orchestrator_uses_detached_worker(monkeypatch, tmp_path):
    _, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    captured = {}
    config_file = tmp_path / "production-config.yaml"
    monkeypatch.setenv("FXALPHA_CONFIG_FILE", str(config_file))

    def fake_run(command, **kwargs):
        captured["command"] = command
        return types.SimpleNamespace(returncode=0, stdout="Running as unit", stderr="")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)

    worker = svc._start_orchestrator_background("run-live", {"n_rounds": 1}, {"contract_source": "orchestrator"})

    assert captured["command"][0:2] == ["systemd-run", "--user"]
    assert f"--setenv=FXALPHA_CONFIG_FILE={config_file}" in captured["command"]
    assert "orchestrator_worker.py" in " ".join(captured["command"])
    assert captured["command"][captured["command"].index("--run-id") + 1] == "run-live"
    assert captured["command"][captured["command"].index("--worker-unit") + 1].endswith(".service")
    assert worker.name == "fxalpha-orchestrator-run-live"
    assert worker.mode == "systemd_transient"
    event = json.loads(events_file.read_text(encoding="utf-8").splitlines()[-1])
    assert event["worker_action"] == "launch_requested"


def test_record_orchestrator_event_validates_and_syncs(monkeypatch, tmp_path):
    steps_file, events_file = _redirect_orchestrator(monkeypatch, tmp_path)

    result = svc.factor_tool_record_orchestrator_event(
        event={
            "run_id": "run-orch",
            "round_id": "run-orch:r0002",
            "stage_seq": 3,
            "stage_id": "run-orch:r0002:s03_novelty_review",
            "stage": "novelty_review",
            "summary": "Novelty advice complete.",
            "decision": "Advance keeper to deep validation.",
            "stage_transition": {
                "next_stage": "deep_validation_review",
                "next_action": "Run deep validation for keeper.",
            },
            "event_type": "advice",
            "checkpoint": "novelty_review",
            "candidate_lanes": [{"candidate_id": "keeper"}],
        }
    )

    assert result.ok
    assert events_file.exists()
    assert steps_file.exists()
    assert result.outputs["recorded"]["sync_status"] == "synced_to_research_step"

    bad = svc.factor_tool_record_orchestrator_event(event={"run_id": "run-orch"})
    assert not bad.ok
    assert "stage_transition.next_stage" in bad.inputs["missing_fields"]


def test_deepseek_json_extractor_accepts_fenced_json():
    payload = _extract_json_object('```json\n{"candidates":[{"expression":"rank(close)"}]}\n```')

    assert payload["candidates"][0]["expression"] == "rank(close)"


def test_pause_is_acknowledged_before_worker_reaches_safe_checkpoint(monkeypatch, tmp_path):
    _, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    run_id = "run-pause-control"
    monkeypatch.setattr(
        svc,
        "_GUI_RUNS",
        {
            run_id: {
                "run_id": run_id,
                "status": "running",
                "stage": "score_review",
                "started_at": "2026-07-13T10:00:00",
                "inputs": {"orchestration_mode": "orchestrator"},
                "events": deque(maxlen=20),
            }
        },
    )
    monkeypatch.setattr(svc, "_orchestrator_thread_alive", lambda candidate: candidate == run_id)

    result = svc.factor_research_pause(run_id=run_id).to_dict()

    assert result["ok"] is True
    assert result["outputs"]["accepted"] is True
    assert result["outputs"]["actual_state"] == "pause_requested"
    assert result["outputs"]["requested_state"] == "paused"
    assert "stopped" not in result["outputs"]
    assert svc._GUI_RUNS[run_id]["control_action"] == "pause"
    event = json.loads(events_file.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event_type"] == "operator_control"
    assert event["control_action"] == "pause"


def test_orchestrator_control_journal_is_visible_to_worker(monkeypatch, tmp_path):
    _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    svc._write_orchestrator_control_request(run_id="run-control-journal", action="pause")

    with pytest.raises(svc.OrchestratorStopRequested) as raised:
        svc._raise_if_orchestrator_stop_requested("run-control-journal")

    assert raised.value.action == "pause"
    assert raised.value.request_id.startswith("ctl_")


def test_api_restart_can_reconstruct_and_control_detached_worker(monkeypatch, tmp_path):
    _redirect_orchestrator(monkeypatch, tmp_path)
    run_id = "run-detached-after-api-restart"
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    svc._write_orchestrator_launch_event(
        run_id=run_id,
        inputs={"orchestration_mode": "orchestrator", "n_rounds": 2},
        contract={"target_adopted": 1},
    )
    svc._write_orchestrator_worker_event(
        run_id=run_id,
        action="started",
        unit="fxalpha-factor-orch-test.service",
        pid=999,
        mode="systemd_transient",
    )
    monkeypatch.setattr(svc, "_orchestrator_thread_alive", lambda candidate: candidate == run_id)

    external = svc._active_external_orchestrator_job()
    result = svc.factor_research_pause(run_id=run_id).to_dict()

    assert external["run_id"] == run_id
    assert external["summary"]["worker_unit"] == "fxalpha-factor-orch-test.service"
    assert result["outputs"]["actual_state"] == "pause_requested"
    assert svc._latest_orchestrator_control_request(run_id)["control_action"] == "pause"


def test_resume_reuses_durable_launch_inputs_and_same_run(monkeypatch, tmp_path):
    _redirect_orchestrator(monkeypatch, tmp_path)
    run_id = "run-durable-resume"
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    monkeypatch.setattr(svc, "factor_research_status", lambda: ok_result(outputs={"registry_summary": {}}))
    monkeypatch.setattr(svc, "_factor_readiness", lambda *args, **kwargs: {"quantgpt_api": {"reachable": True}})
    launch_inputs = {
        "orchestration_mode": "orchestrator",
        "direction": "auto",
        "universe": "tradable_non_st",
        "n_candidates": 4,
        "n_rounds": 7,
        "target_adopted": 2,
    }
    svc._write_orchestrator_launch_event(run_id=run_id, inputs=launch_inputs, contract={"target_adopted": 2})
    svc._write_orchestrator_event(
        {
            "run_id": run_id,
            "round_id": f"{run_id}:stop",
            "stage_seq": 99,
            "stage_id": f"{run_id}:stop:s99_checkpoint_stop",
            "stage": "checkpoint_stop",
            "summary": "paused",
            "decision": "operator_pause_completed",
            "stage_transition": {"next_stage": "checkpoint_stop", "next_action": "idle"},
            "event_type": "checkpoint",
            "tags": ["checkpoint_stop", "operator_pause"],
        }
    )
    captured = {}

    class FakeThread:
        name = f"fxalpha-orchestrator-{run_id}"
        ident = 55

    def fake_start(actual_run_id, inputs, contract):
        captured.update({"run_id": actual_run_id, "inputs": inputs, "contract": contract})
        return FakeThread()

    monkeypatch.setattr(svc, "_start_orchestrator_background", fake_start)

    result = svc.factor_research_resume(run_id).to_dict()

    assert result["ok"] is True
    assert result["outputs"]["run_id"] == run_id
    assert captured["run_id"] == run_id
    assert captured["inputs"]["n_candidates"] == 4
    assert captured["inputs"]["n_rounds"] == 7
    assert captured["contract"]["recovery_attempt"] is True


def test_control_state_exposes_only_valid_actions_for_operator_pause(monkeypatch, tmp_path):
    steps_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(svc, "_GUI_RUNS", {})
    monkeypatch.setattr(svc, "_factor_readiness", lambda *args, **kwargs: {"quantgpt_api": {"reachable": True}})
    steps_file.parent.mkdir(parents=True, exist_ok=True)
    steps_file.write_text(
        json.dumps(
            {
                "run_id": "run-paused-state",
                "round_id": "run-paused-state:stop",
                "stage_id": "run-paused-state:stop:s99_checkpoint_stop",
                "stage": "checkpoint_stop",
                "tags": ["checkpoint_stop", "operator_pause"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = svc.factor_research_control_state().to_dict()

    assert result["outputs"]["state"] == "paused"
    assert result["outputs"]["run_id"] == "run-paused-state"
    assert result["outputs"]["allowed_actions"] == ["resume", "stop", "guidance"]


def test_live_console_prefers_new_active_worker_over_previous_completed_research_step():
    selected = svc._live_console_run_id(
        {"run_id": "run-new", "status": "running", "stage": "orchestrator_background_started"},
        [{"run_id": "run-old", "stage": "checkpoint_stop"}],
    )
    assert selected == "run-new"
    assert svc._live_console_run_id({}, [{"run_id": "run-old"}]) == "run-old"


def test_orchestrator_publishes_startup_progress_before_information_audit():
    source = Path(svc.__file__).read_text(encoding="utf-8")
    start = source.index("def _run_orchestrator_job(run_id: str, inputs: dict, contract: dict) -> None:")
    body = source[start : source.index("def ", start + 10)]
    progress_position = body.index('event={"event": "information_audit_refresh_started"}')
    audit_position = body.index("audit_result = factor_library_audit(")
    assert progress_position < audit_position
    assert 'next_action="refresh_factor_map_context"' in body
    assert "include_feature_sets=True" in body


def test_context_pack_is_compact_and_excludes_secrets():
    pack = OrchestratorContextPack(
        run_id="run1",
        round_id="run1:r0001",
        stage="candidate_plan",
        contract={"target_adopted": 1},
        active_context={
            "config": {"default_universe": "all_market"},
            "field_context": {"supported_fields": ["close", "amount"]},
            "active_factor_summary": {"active_factor_count": 2},
        },
        recent_steps=[{"stage": "score_review", "summary": "x", "extra": {"large": "ignored"}}],
        quantgpt_summary={"score": {"completed": 2}},
        round_events=[{"stage": "score_review", "summary": "s", "advice": {"action": "mutate"}}],
    ).to_dict()

    raw = json.dumps(pack, ensure_ascii=False)
    assert "api_key" not in raw
    assert "ignored" not in raw


def test_context_pack_preserves_nested_transition_and_active_pool_crowding():
    active_factors = [
        {
            "name": f"f{i}",
            "hypothesis": "price-volume crowding",
            "expression": f"rank(ts_mean(close, {i + 2}) / amount)",
        }
        for i in range(8)
    ]
    pack = OrchestratorContextPack(
        run_id="run1",
        round_id="run1:r0001",
        stage="candidate_plan",
        contract={"target_adopted": 1},
        active_context={
            "config": {"default_universe": "all_market"},
            "field_context": {"supported_fields": ["close", "amount"]},
            "active_factor_summary": {"active_factor_count": 8, "active_factors": active_factors},
            "latest_stage_transition": {
                "latest_stage": "score_review",
                "latest_step_ts": "2026-06-14T10:00:00",
                "stage_transition": {
                    "next_stage": "novelty_review",
                    "next_action": "检查同 horizon active pool 拥挤度",
                    "why": "score batch has keepers",
                },
            },
        },
        recent_steps=[],
        quantgpt_summary={},
        round_events=[],
    ).to_dict()

    latest = pack["active_context"]["latest_stage_transition"]
    assert latest["latest_stage"] == "score_review"
    assert latest["next_stage"] == "novelty_review"
    assert latest["stage_transition"]["next_action"] == "检查同 horizon active pool 拥挤度"

    crowding = pack["active_context"]["active_factor_summary"]["crowding_map"]
    assert crowding["expression_count"] == 8
    assert len(crowding["expressions"]) == 8
    assert crowding["field_usage_counts"]["close"] == 8
    assert crowding["field_usage_counts"]["amount"] == 8


def test_orchestrator_prompt_contract_does_not_mix_mcp_mode_contract():
    pack = OrchestratorContextPack(
        run_id="run1",
        round_id="run1:r0001",
        stage="thesis_design",
        contract={"target_adopted": 1, "contract_source": "orchestrator"},
        active_context={
            "must_read_contract": {
                "production_boundary": ["Use only Codex direct native QuantGPT MCP orchestration."],
                "import_rules": ["run_rolling_validation as required deep-validation evidence for import candidates"],
            },
            "config": {"default_universe": "all_market"},
            "field_context": {"supported_fields": ["close", "amount"]},
            "active_factor_summary": {"active_factor_count": 0},
        },
        recent_steps=[],
        quantgpt_summary={},
        round_events=[],
    ).to_dict()
    payload = svc._orchestrator_stage_payload(
        stage="thesis_design",
        context_pack=pack,
        stage_input={"blocked_or_failed_reasons": {}, "available_field_families": ["close"], "target_constraints": {"need_active_factors": 1}},
        lineage_context={},
        round_events=[],
    )
    raw = json.dumps(payload, ensure_ascii=False)

    assert payload["task"] == "fxalpha_orchestrator_stage"
    assert payload["stage"] == "thesis_design"
    assert "system_contract" not in payload
    assert "Codex direct native QuantGPT MCP" not in raw
    assert "mcp_native_tools_missing" not in raw
    assert "diagnostic_only" not in raw
    assert "selection_window" not in raw


def test_deep_advice_requires_rolling_validation_for_orchestrator_gate():
    candidate = {
        "expression": "rank(close)",
        "quick_score": 90,
        "backtest_summary": {"ic_mean": 0.035, "ic_ir": 0.45, "rank_ic_mean": 0.06, "rank_ic_ir": 0.55},
        "novelty_guard": {"allowed": True, "novelty_score": 0.5},
        "anti_overfit": {"score": 92, "risk_flag": "low"},
        "adversarial_validation": {"score": 88},
        "holding_period_days": 5,
    }

    advice = orchestrator.deep_advice([candidate])

    assert advice["action"] == "complete_deep_evidence"
    assert "run_rolling_validation" in advice["allowed_actions"]
    assert "rolling" in json.dumps(advice, ensure_ascii=False)


def test_deep_advice_does_not_call_passing_rolling_component_weak():
    candidate = {
        "candidate_id": "c-near",
        "expression": "rank(close)",
        "quick_score": 77.5,
        "backtest_summary": {"ic_mean": 0.04, "ic_ir": 0.5, "rank_ic_mean": 0.04, "rank_ic_ir": 0.5},
        "novelty_guard": {"allowed": True, "novelty_score": 0.3},
        "anti_overfit": {"score": 93.8},
        "rolling_validation": {
            "status": "ok",
            "score": 73.0,
            "grade": "B",
            "summary": {"n_windows": 1},
            "windows": [{"test_ic": 0.04}],
        },
        "adversarial_validation": {"score": 76.4},
    }

    lane = orchestrator.deep_advice([candidate])["candidate_lane_decisions"][0]

    assert lane["deep_score"] == 78.9
    assert lane["gap_to_gate"] == 1.1
    assert lane["reason"] == "deep_score_lt_80_lowest_component_rolling"
    assert lane["lowest_component"] == "rolling"
    assert lane["lowest_component_reference_status"] == "grade_b_or_better"
    assert "weak_rolling" not in json.dumps(lane, ensure_ascii=False)
    compact_lane = svc._compact_prompt_advice({"candidate_lane_decisions": [lane]})["candidate_lane_decisions"][0]
    assert compact_lane["gap_to_gate"] == 1.1
    assert compact_lane["rolling_grade"] == "B"
    assert compact_lane["score_parts"]["weighted_contributions"]["rolling"] == 14.6


def _plateau_deep_candidate(
    *,
    candidate_id: str,
    round_id: str,
    parent_candidate_id: str | None,
    window: int,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "round_id": round_id,
        "parent_candidate_id": parent_candidate_id,
        "mutation_summary": "directed window mutation" if parent_candidate_id else None,
        "matched_region_uid": "region_plateau",
        "expression": (
            f"rank(ts_delta(borrow_money_bal,{window}))"
            " * rank(-ts_std(amp,5))"
            " * rank(ts_mean(lg_net_amount,10))"
        ),
        "quick_score": 78.0,
        "backtest_summary": {
            "ic_mean": 0.04,
            "ic_ir": 0.5,
            "rank_ic_mean": 0.04,
            "rank_ic_ir": 0.5,
        },
        "novelty_guard": {
            "allowed": True,
            "novelty_score": 0.4,
            "matched_region_uid": "region_plateau",
        },
        "anti_overfit": {"score": 90.0},
        "rolling_validation": {
            "status": "ok",
            "score": 59.0,
            "grade": "C",
            "summary": {"n_windows": 3},
            "windows": [{"test_ic": 0.03}],
        },
        "adversarial_validation": {"score": 78.0},
    }


def test_deep_advice_recombines_when_parent_lineage_stops_improving():
    current = _plateau_deep_candidate(
        candidate_id="c2",
        round_id="run:r0002",
        parent_candidate_id="run:r0001:c1",
        window=15,
    )
    current_score = orchestrator.deep_advice([current])["candidate_lane_decisions"][0]["deep_score"]
    advice = orchestrator.deep_advice(
        [current],
        trajectory=[
            {
                "round_id": "run:r0001",
                "candidate_id": "c1",
                "expression": (
                    "rank(ts_delta(borrow_money_bal,20))"
                    " * rank(-ts_std(amp,5))"
                    " * rank(ts_mean(lg_net_amount,10))"
                ),
                "score": current_score + 0.2,
                "rolling_score": 59.2,
                "downstream_action": "targeted_mutation",
                "parent_eligible": True,
            }
        ],
    )

    lane = advice["candidate_lane_decisions"][0]
    assert lane["action"] == "recombine_from_best"
    assert lane["evolution_strategy"]["strategy"] == "recombine"
    assert lane["trajectory_progress"] == {
        "scope": "parent_lineage",
        "attempts": 2,
        "failed_attempts": 2,
        "deep_gain": -0.2,
        "rolling_gain": -0.2,
        "meaningful_gain": False,
    }
    assert advice["recombination_candidates"][0]["candidate_id"] == "c1"


def test_deep_advice_explores_after_recombination_also_stalls():
    current = _plateau_deep_candidate(
        candidate_id="c3",
        round_id="run:r0003",
        parent_candidate_id="run:r0002:c2",
        window=12,
    )
    current_score = orchestrator.deep_advice([current])["candidate_lane_decisions"][0]["deep_score"]
    trajectory = [
        {
            "round_id": "run:r0001",
            "candidate_id": "c1",
            "expression": _plateau_deep_candidate(
                candidate_id="c1",
                round_id="run:r0001",
                parent_candidate_id=None,
                window=20,
            )["expression"],
            "score": current_score + 0.3,
            "rolling_score": 59.3,
            "downstream_action": "targeted_mutation",
            "parent_eligible": True,
        },
        {
            "round_id": "run:r0002",
            "candidate_id": "c2",
            "parent_candidate_id": "run:r0001:c1",
            "expression": _plateau_deep_candidate(
                candidate_id="c2",
                round_id="run:r0002",
                parent_candidate_id="run:r0001:c1",
                window=15,
            )["expression"],
            "score": current_score + 0.1,
            "rolling_score": 59.1,
            "downstream_action": "recombine_from_best",
            "parent_eligible": False,
        },
    ]

    lane = orchestrator.deep_advice(
        [current],
        trajectory=trajectory,
    )["candidate_lane_decisions"][0]

    assert lane["action"] == "explore_new_thesis"
    assert lane["evolution_strategy"] == {
        "strategy": "explore",
        "action": "explore_new_thesis",
        "reason": "parent_lineage_stalled_after_recombine",
    }
    assert lane["trajectory_progress"]["attempts"] == 3


def test_orchestrator_event_and_trace_redact_secret_like_values(monkeypatch, tmp_path):
    _, events_file = _redirect_orchestrator(monkeypatch, tmp_path)
    trace_dir = tmp_path / "orchestrator_llm_traces"
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_LLM_TRACES_DIR", trace_dir)
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_LLM_TRACES_FILE", trace_dir / "current.jsonl")
    monkeypatch.setattr(svc, "FACTOR_ORCHESTRATOR_LLM_TRACES_HISTORY_DIR", trace_dir / "history")
    secret = "sk-testsecret123"
    client_secret = "client-secret-value-123456"
    cookie_secret = "sessionid=abc123secretcookie"
    proxy_url = "https://user:password@example.com/path"

    event = svc._write_orchestrator_event(
        {
            "run_id": "run-secret",
            "round_id": "run-secret:r0001",
            "stage_seq": 1,
            "stage_id": "run-secret:r0001:s01_protocol_load",
            "stage": "protocol_load",
            "summary": "contains secret",
            "decision": "continue",
            "stage_transition": {"next_stage": "thesis_design", "next_action": "continue"},
            "evidence_refs": [
                {
                    "note": f"provider key {secret}",
                    "authorization": "Bearer abcdefghijklmnop",
                    "cookie": cookie_secret,
                    "proxy": proxy_url,
                }
            ],
        }
    )
    trace = svc._write_orchestrator_llm_trace(
        {
            "trace_id": "trace-secret",
            "run_id": "run-secret",
            "round_id": "run-secret:r0001",
            "stage": "thesis_design",
            "event_type": "llm_request",
            "payload": {"api_key": secret, "client_secret": client_secret, "text": f"inline {secret}", "proxy": proxy_url},
            "user_prompt": f"inline {secret}",
        }
    )

    event_text = events_file.read_text(encoding="utf-8")
    trace_text = (trace_dir / "current.jsonl").read_text(encoding="utf-8")
    for sensitive in (secret, client_secret, cookie_secret, "user:password", "Bearer abcdefghijklmnop"):
        assert sensitive not in event_text
        assert sensitive not in trace_text
    assert event["redaction_status"]["redacted"] is True
    assert trace["redaction_status"]["redacted"] is True


def test_orchestrator_redaction_preserves_factor_semantics_but_redacts_real_tokens():
    original = {
        "field_tokens": ["amount", "close"],
        "operator_tokens": ["rank", "ts_mean"],
        "window_tokens": [5, 20],
        "fields_used": ["amount"],
        "operators_used": ["rank"],
        "window_lengths": [10],
        "access_token": "real-auth-token-value",
        "api_key": "sk-testsecret123",
    }

    redacted, count = svc._redact_orchestrator_payload(original)

    assert redacted["field_tokens"] == ["amount", "close"]
    assert redacted["operator_tokens"] == ["rank", "ts_mean"]
    assert redacted["window_tokens"] == [5, 20]
    assert redacted["fields_used"] == ["amount"]
    assert redacted["operators_used"] == ["rank"]
    assert redacted["window_lengths"] == [10]
    assert redacted["access_token"] == "***REDACTED_SECRET***"
    assert redacted["api_key"] == "***REDACTED_SECRET***"
    assert count == 2
    assert original["access_token"] == "real-auth-token-value"


def test_active_factor_semantics_are_derived_before_expression_preview_truncation():
    expression = "rank(ts_mean(amount, 20) / close) + " + "rank(close) + " * 40
    compact = svc._compact_active_factor_for_context(
        {"name": "LongFactor", "expression": expression},
        supported_fields={"amount", "close"},
    )
    summary = _compact_active_factor_summary({"active_factor_count": 1, "active_factors": [compact]})
    factor = summary["active_factors"][0]

    assert compact["expression"] == expression.strip()
    assert compact["fields_used"] == ["amount", "close"]
    assert factor["expression_complete"] is False
    assert factor["fields_used"] == ["amount", "close"]
    assert all(field not in {"a", "amoun"} for field in factor["fields_used"])


def test_read_recent_research_steps_uses_history_tail(monkeypatch, tmp_path):
    current_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)
    history_dir = svc.FACTOR_RESEARCH_STEPS_HISTORY_DIR
    history_dir.mkdir(parents=True, exist_ok=True)
    current_file.parent.mkdir(parents=True, exist_ok=True)
    current_file.write_text(
        "\n".join(
            [
                json.dumps({"stage_id": "cur-1", "stage": "score_review", "ts": "2026-06-14T10:00:00"}),
                json.dumps({"stage_id": "cur-2", "stage": "novelty_review", "ts": "2026-06-14T10:01:00"}),
            ]
        ),
        encoding="utf-8",
    )
    (history_dir / "20260613.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"stage_id": "hist-1", "stage": "deep_validation_review", "ts": "2026-06-13T10:00:00"}),
                json.dumps({"stage_id": "hist-2", "stage": "round_synthesis", "ts": "2026-06-13T10:01:00"}),
            ]
        ),
        encoding="utf-8",
    )

    steps = svc._read_recent_research_steps(limit=4)

    assert [step["stage_id"] for step in steps] == ["cur-2", "cur-1", "hist-2", "hist-1"]


















def test_context_pack_routes_only_factor_map_to_design_stages(monkeypatch):
    monkeypatch.setattr(svc, "factor_tool_context", lambda **kwargs: ok_result(outputs={}))
    monkeypatch.setattr(svc, "_read_recent_research_steps", lambda limit=20, run_id=None: [])
    monkeypatch.setattr(svc, "_recent_orchestrator_anchors", lambda limit=8: [])
    monkeypatch.setattr(svc, "_recent_orchestrator_failure_feedback", lambda: {})
    monkeypatch.setattr(svc, "_fetch_quantgpt_recent_tasks", lambda **kwargs: [])
    monkeypatch.setattr(svc, "_quantgpt_task_summary", lambda tasks: {})
    deep_review = svc._build_orchestrator_context_pack(
        run_id="run-current",
        round_id="run-current:r0002",
        stage="deep_validation_review",
        contract={},
        round_events=[],
    )
    assert deep_review["active_context"]["factor_map_context"] == {}

    design = svc._build_orchestrator_context_pack(
        run_id="run-current",
        round_id="run-current:r0003",
        stage="expression_design",
        contract={
            "research_direction": "资金流正交确认",
            "factor_map_context": {"available": True, "map_id": "fm_test", "regions": []},
        },
        round_events=[],
    )
    assert design["active_context"]["factor_map_context"]["map_id"] == "fm_test"

    candidate_plan = svc._build_orchestrator_context_pack(
        run_id="run-current",
        round_id="run-current:r0003",
        stage="candidate_plan",
        contract={},
        round_events=[],
    )
    assert candidate_plan["active_context"]["factor_map_context"] == {}

    novelty = svc._build_orchestrator_context_pack(
        run_id="run-current",
        round_id="run-current:r0003",
        stage="novelty_review",
        contract={},
        round_events=[],
    )
    assert novelty["active_context"]["factor_map_context"] == {}

    synthesis = svc._build_orchestrator_context_pack(
        run_id="run-current",
        round_id="run-current:r0003",
        stage="round_synthesis",
        contract={},
        round_events=[],
    )
    assert synthesis["active_context"]["factor_map_context"] == {}

    gate = svc._build_orchestrator_context_pack(
        run_id="run-current",
        round_id="run-current:r0003",
        stage="import_gate_review",
        contract={},
        round_events=[],
    )


def test_expression_design_filters_exact_prior_round_candidates_without_exposing_private_map():
    prior_expression = "rank(ts_mean(net_mf_amount,10))"
    prior_refs = {
        svc._normalize_symbolic_expression(prior_expression): {
            "round_id": "run-x:r0001",
            "candidate_id": "old-c1",
            "expression": prior_expression,
        }
    }
    stage_input = {
        "operator_list_summary": {"supported_operators": ["rank", "ts_mean"]},
        "prior_expression_history": svc._prior_round_expression_history(prior_refs),
        "_private_prior_expression_refs": prior_refs,
    }
    compact = svc._compact_stage_tool_evidence_for_prompt(
        stage="expression_design",
        stage_input=stage_input,
    )
    assert "_private_prior_expression_refs" not in json.dumps(compact, ensure_ascii=False)
    assert compact["prior_expression_history"]["full_history_digest"]["expression_count"] == 1

    result = {
        "candidates": [
            {"candidate_id": "c1", "expression": prior_expression},
            {"candidate_id": "c2", "expression": "rank(ts_mean(lg_net_amount,10))"},
        ]
    }
    svc._filter_expression_design_exact_repeats(
        result,
        stage_input=stage_input,
        require_one_unique=True,
    )
    assert [item["candidate_id"] for item in result["candidates"]] == ["c2"]
    assert result["_orchestrator_validation_warnings"] == ["exact_prior_round_candidates_removed:c1"]






































def test_score_review_prompt_compaction_keeps_signal_and_drops_heavy_blobs():
    compact = svc._compact_stage_tool_evidence_for_prompt(
        stage="score_review",
        stage_input={
            "candidate_lanes": [
                {
                    "candidate_id": "c1",
                    "expression": "rank(close)",
                    "score": 68.7,
                    "grade": "B",
                    "validation": "OK",
                    "backtest_summary": {
                        "ic_mean": 0.031,
                        "ic_ir": 0.355,
                        "rank_ic_mean": 0.069,
                        "rank_ic_ir": 0.560,
                        "annual_return": 0.089,
                        "sharpe": 0.326,
                        "max_drawdown": -0.328,
                        "turnover": 0.185,
                    },
                    "best_long_only_group_metrics": {"group_returns": {"0": {"annual_return": 0.1}}},
                }
            ],
            "validate_results": [{"candidate_id": "c1", "validation": "OK", "status": "success"}],
            "score_factor_results": [
                {
                    "candidate_id": "c1",
                    "expression": "rank(close)",
                    "score": 68.7,
                    "grade": "B",
                    "validation": "OK",
                    "backtest_summary": {
                        "ic_mean": 0.031,
                        "ic_ir": 0.355,
                        "rank_ic_mean": 0.069,
                        "rank_ic_ir": 0.560,
                        "annual_return": 0.089,
                        "sharpe": 0.326,
                        "max_drawdown": -0.328,
                        "turnover": 0.185,
                    },
                    "best_long_only_group_metrics": {"group_returns": {"0": {"annual_return": 0.1}}},
                }
            ],
            "trajectory_metrics": {"best_score": 68.7},
            "code_advice": {"action": "advance_to_novelty", "candidate_lane_decisions": [{"candidate_id": "c1", "action": "advance_to_novelty"}]},
        },
    )

    row = compact["score_factor_results"][0]
    assert row["score"] == 68.7
    assert row["grade"] == "B"
    assert row["ic"] == 0.031
    assert row["icir"] == 0.355
    assert "best_long_only_group_metrics" not in json.dumps(compact, ensure_ascii=False)


def test_return_handoff_is_compacted_for_prompt():
    lineage = svc._stage_lineage_context(
        previous_review_advice=[
            {
                "from_stage": "deep_validation_review",
                "to_stage": "expression_design",
                "reason": "deep score below threshold",
                "must_preserve": ["keep dv_ttm thesis"],
                "must_avoid": ["where branch"],
                "recommended_mutation": "add tanh",
                "supporting_evidence_refs": [
                    {
                        "candidate_id": "c1",
                        "tool": "deep_validation",
                        "task_id": "task-1",
                        "deep_score": 76.5,
                        "ic": 0.034,
                        "icir": 0.356,
                        "risk_flag": "watch",
                        "raw_blob": {"ignored": True},
                    }
                ],
            }
        ]
    )

    handoff = lineage["previous_review_advice"][0]
    assert handoff["from_stage"] == "deep_validation_review"
    assert handoff["binding_policy"] == "mechanism_and_evidence_only_not_literal_expression_instruction"
    assert handoff["recommended_mutation"] == "target_stage_reassesses_mechanism_from_evidence"
    assert "tanh" not in handoff["recommended_mutation"]
    assert handoff["supporting_evidence_refs"][0]["deep_score"] == 76.5
    assert "raw_blob" not in json.dumps(handoff, ensure_ascii=False)


def test_candidate_plan_payload_compacts_active_and_current_round_context():
    payload = svc._orchestrator_stage_payload(
        stage="candidate_plan",
        context_pack={
            "run_state": {
                "run_id": "run-x",
                "round_id": "run-x:r0002",
                "adopted_so_far": 1,
                "contract": {
                    "target_adopted": 2,
                    "holding_period": 5,
                    "universe": "all_market",
                    "benchmark": "hs300",
                    "selection_start_date": "2023-01-01",
                    "selection_end_date": "2025-12-31",
                },
            },
            "protocol": {"gate_standard": {"deep_score_min": 80, "min_abs_ic": 0.02, "min_abs_icir": 0.3}},
                "active_context": {
                    "factor_map_context": {
                        "available": True,
                        "map_id": "fm_test",
                        "audit_id": "fa_test",
                        "regions": [{
                            "cluster_id": "information_001",
                            "region_uid": "region_one",
                            "size": 1,
                            "representative": {"factor_id": "factor_0", "name": "Factor0", "expression": "rank(close)"},
                            "members": [{"factor_id": "factor_0", "name": "Factor0", "expression": "rank(close)"}],
                        }],
                    },
                    "active_factor_summary": {
                    "active_factor_count": 5,
                    "active_factors": [
                        {
                            "name": f"Factor{i}",
                            "factor_id": f"factor_{i}",
                            "hypothesis": "h" * 120,
                            "expression": f"rank(close) + rank(amount) * {i}",
                            "fields": ["close", "amount"],
                            "raw_blob": {"large": True},
                        }
                        for i in range(5)
                    ],
                    "registry_summary": {"total": 123},
                },
                "field_context": {
                    "supported_fields": ["close", "amount", "ps_ttm"],
                    "blocked_fields": ["revenue", "cash_flow", "profit"],
                    "aliases": {"market_cap": "total_mv", "pe_ratio": "pe", "extra": "ignored"},
                    "coverage_summary": {"huge": "x" * 2000},
                },
            },
            "recent_steps": [
                {
                    "stage_id": "step-score",
                    "stage": "score_review",
                    "summary": "recent score",
                    "decision": "advance_some",
                    "stage_transition": {"next_stage": "novelty_review", "judgment": "score ok", "why": "recent"},
                },
                {
                    "stage_id": "step-deep",
                    "stage": "deep_validation_review",
                    "summary": "recent deep",
                    "decision": "mutate",
                    "stage_transition": {"next_stage": "expression_design", "judgment": "deep miss", "why": "need mutation"},
                },
            ],
            "round_events": [],
        },
        lineage_context={
            "current_thesis": [
                {
                    "thesis_id": "t1",
                    "economic_rationale": "r" * 200,
                    "expected_alpha_mechanism": "m" * 200,
                    "preferred_data_families": ["ps_ttm", "close"],
                    "avoid_patterns": ["where"],
                    "priority": "high",
                    "unused_blob": {"big": True},
                }
            ],
            "current_hypothesis": [
                {
                    "hypothesis_id": "h1",
                    "thesis_id": "t1",
                    "signal_claim": "s" * 240,
                    "expected_direction": "positive",
                    "candidate_variable_groups": [{"role": "main_signal", "fields": ["ps_ttm"]}],
                    "window_policy": "w" * 200,
                    "normalization_policy": "n" * 200,
                    "risk_notes": ["risk-a", "risk-b"],
                    "mutation_plan_if_fail": ["mut-a"],
                    "raw_blob": {"ignored": True},
                }
            ],
            "parent_candidates": [
                {
                    "candidate_id": "c1",
                    "hypothesis_id": "h1",
                    "expression": "rank(ps_ttm)" * 30,
                    "expected_direction": "positive",
                    "mechanism_summary": "mech" * 40,
                    "complexity_intent": "simple",
                    "factor_name_hint": "PSTTM",
                    "best_long_only_group_metrics": {"group_returns": {"0": {"annual_return": 0.2}}},
                }
            ],
        },
        stage_input={"candidate_lanes": [{"candidate_id": "c1", "expression": "rank(close)", "score": 61.0, "grade": "B"}]},
        round_events=[
            {"stage": "score_review", "summary": "recent score", "decision": "advance_some"},
            {"stage": "deep_validation_review", "summary": "recent deep", "decision": "mutate"},
        ],
    )

    active = payload["context_pack"]["active_context"]
    current = payload["context_pack"]["current_round_context"]
    history = payload["context_pack"].get("history_context", {})
    raw = json.dumps(payload, ensure_ascii=False)

    assert "active_factor_samples" not in active
    assert "factor_map_context" not in active
    assert "symbolic_crowding_map" not in active
    assert "registry_summary" not in json.dumps(active, ensure_ascii=False)
    assert "coverage_summary" not in json.dumps(active, ensure_ascii=False)
    assert active["research_space"]["field_constraints"]["aliases"] == {"market_cap": "total_mv", "pe_ratio": "pe"}
    assert current["thesis"][0]["priority"] == "high"
    assert len(current["hypotheses"][0]["signal_claim"]) <= 160
    assert "candidate_drafts" not in current
    assert history == {}
    assert "current_round_context" in raw
    assert "factor_map_context" not in raw
    assert "library_information_context" not in raw
    assert "symbolic_family_representatives" not in raw
    assert "active_factor_samples" not in raw
    assert "usage_policy" not in raw
    assert "context_budget" not in svc._llm_visible_payload(payload)
    assert "_context_budget" not in svc._llm_visible_payload(payload)
    assert "system_contract" not in raw
    assert "best_long_only_group_metrics" not in raw


def test_review_stage_history_budget_is_tighter():
    payload = svc._orchestrator_stage_payload(
        stage="score_review",
        context_pack={
            "run_state": {"run_id": "run-x", "round_id": "run-x:r0002", "contract": {"target_adopted": 2, "holding_period": 5}},
            "protocol": {"gate_standard": {"deep_score_min": 80, "min_abs_ic": 0.02, "min_abs_icir": 0.3}},
            "active_context": {
                "active_factor_summary": {"active_factor_count": 5, "active_factors": [{"name": f"f{i}", "hypothesis": "h" * 100, "expression": "rank(close)" * 20} for i in range(5)]},
                "field_context": {"supported_fields": [f"field_{i}" for i in range(80)], "blocked_fields": [f"blocked_{i}" for i in range(20)]},
                "recent_orchestrator_anchors": [{"stage": "deep_validation_review", "candidate_id": f"c{i}", "expression": "rank(close)"} for i in range(6)],
                "recent_orchestrator_failure_feedback": {"weak_fields": [{"field": f"w{i}", "recent_failure_count": i} for i in range(8)]},
            },
            "recent_steps": [
                {"stage_id": f"step-{i}", "stage": "score_review" if i % 2 == 0 else "deep_validation_review", "summary": "s" * 80, "decision": "advance", "stage_transition": {"judgment": "j" * 80, "why": "w" * 80}}
                for i in range(14)
            ],
            "round_events": [{"stage": "score_review", "summary": f"summary-{i}", "decision": "advance"} for i in range(10)],
        },
        lineage_context={
            "current_thesis": [{"thesis_id": f"t{i}", "economic_rationale": "r", "expected_alpha_mechanism": "m"} for i in range(5)],
            "current_hypothesis": [{"hypothesis_id": f"h{i}", "signal_claim": "s"} for i in range(6)],
            "parent_candidates": [{"candidate_id": f"c{i}", "expression": "rank(close)"} for i in range(6)],
        },
        stage_input={"candidate_lanes": [{"candidate_id": "c1", "expression": "rank(close)", "score": 61.0, "grade": "B"}]},
        round_events=[{"stage": "score_review", "summary": f"round-{i}", "decision": "advance"} for i in range(10)],
    )

    history = payload["context_pack"].get("history_context", {})
    active = payload["context_pack"]["active_context"]
    current = payload["context_pack"]["current_round_context"]
    assert history == {}
    assert active["operator_research_direction"]["mode"] == "autonomous_topic_selection"
    assert active["research_contract"] == {"holding_period": 5, "target_adopted": 2}
    assert set(active) == {"operator_research_direction", "research_contract"}
    assert "usage_policy" not in json.dumps(history, ensure_ascii=False)
    assert len(current["thesis"]) <= 3
    assert len(current["hypotheses"]) <= 4
    assert "candidate_drafts" not in current


def test_model_visible_history_keeps_only_fallback_handoff():
    raw_history = {
        "short_term_history": {
            "latest_round_handoff": {"stage": "round_synthesis", "next_stage": "expression_design"},
            "stage_relevant_steps": [{"stage": "deep_validation_review"}],
            "positive_precedents": [{"candidate_id": "c1"}],
            "negative_precedents": {"deep_near_misses": [{"candidate_id": "c1"}]},
            "review_anchors": [{"stage": "deep_validation_review"}],
            "recent_same_round_events": [{"stage": "deep_validation_review"}],
        },
    }

    fallback_visible = svc._model_visible_stage_history(
        raw_history,
        has_upstream_handoff=False,
    )
    short_term = fallback_visible["short_term_history"]
    assert set(short_term) == {"latest_round_handoff"}

    handoff_visible = svc._model_visible_stage_history(
        raw_history,
        has_upstream_handoff=True,
    )
    assert "short_term_history" not in handoff_visible
    assert handoff_visible == {}


def test_model_visible_history_keeps_recent_round_facts_for_design_with_handoff():
    raw_history = {
        "short_term_history": {
            "latest_round_handoff": {"stage": "round_synthesis", "next_stage": "expression_design"},
            "recent_completed_rounds": [
                {
                    "round_id": "fr_test:r0003",
                    "decision": "continue_next_round",
                    "summary": "上一轮因重复关系未通过新颖性检查。",
                    "why": "正式 novelty 证据显示信息关系重复。",
                    "next_stage": "hypothesis_design",
                },
                {
                    "round_id": "fr_test:r0002",
                    "decision": "continue_next_round",
                    "summary": "深验显示近期稳定性不足。",
                    "next_stage": "expression_design",
                },
            ],
            "positive_precedents": [{"candidate_id": "c1"}],
            "negative_precedents": {"deep_near_misses": [{"candidate_id": "c1"}]},
        },
    }

    visible = svc._model_visible_stage_history(
        raw_history,
        has_upstream_handoff=True,
        stage="expression_design",
    )

    short_term = visible["short_term_history"]
    assert set(short_term) == {"recent_completed_rounds"}
    assert [item["round_id"] for item in short_term["recent_completed_rounds"]] == [
        "fr_test:r0003",
        "fr_test:r0002",
    ]
    assert "positive_precedents" not in json.dumps(visible, ensure_ascii=False)
    assert "negative_precedents" not in json.dumps(visible, ensure_ascii=False)


def test_model_visible_history_hides_recent_round_facts_from_review_stages():
    raw_history = {
        "short_term_history": {
            "recent_completed_rounds": [{"round_id": "fr_test:r0003", "summary": "历史事实"}],
        },
    }

    visible = svc._model_visible_stage_history(
        raw_history,
        has_upstream_handoff=True,
        stage="score_review",
    )

    assert visible == {}




def test_orchestrator_candidate_limit_allows_ten_candidate_batches():
    assert svc._orchestrator_candidate_limit(10) == 10
    assert svc._orchestrator_candidate_limit(50) == 10
    assert svc._orchestrator_candidate_limit(None) == 3


def test_candidate_plan_payload_hides_stale_return_handoff():
    payload = svc._orchestrator_stage_payload(
        stage="candidate_plan",
        context_pack={
            "run_state": {"run_id": "run-x", "round_id": "run-x:r0002", "contract": {"target_adopted": 2, "holding_period": 5}},
            "protocol": {"gate_standard": {"deep_score_min": 80, "min_abs_ic": 0.02, "min_abs_icir": 0.3}},
            "active_context": {"active_factor_summary": {"active_factor_count": 2}, "field_context": {"supported_fields": ["close", "ps_ttm"]}},
            "recent_steps": [],
            "round_events": [],
        },
        lineage_context={
            "current_thesis": [{"thesis_id": "t1", "economic_rationale": "ps_ttm repair", "expected_alpha_mechanism": "delta x low vol"}],
            "current_hypothesis": [{"hypothesis_id": "h1", "thesis_id": "t1", "signal_claim": "repair"}],
            "parent_candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
            "previous_review_advice": [
                {
                    "from_stage": "score_review",
                    "to_stage": "thesis_design",
                    "reason": "last round failed",
                    "recommended_mutation": "return_thesis_design",
                }
            ],
            "return_reason_from_downstream": {
                "from_stage": "score_review",
                "to_stage": "thesis_design",
                "reason": "last round failed",
            },
        },
        stage_input={"candidates": [{"candidate_id": "c1", "expression": "rank(close)"}]},
        round_events=[],
        return_handoff={"from_stage": "score_review", "to_stage": "thesis_design", "reason": "last round failed"},
    )

    assert "lineage_context" not in payload["context_pack"]
    assert "handoff" not in payload["context_pack"]["current_round_context"]
    assert payload["context_pack"].get("upstream_handoff", {}) == {}


def test_expression_design_payload_keeps_return_handoff():
    payload = svc._orchestrator_stage_payload(
        stage="expression_design",
        context_pack={
            "run_state": {"run_id": "run-x", "round_id": "run-x:r0002", "contract": {"target_adopted": 2, "holding_period": 5}},
            "protocol": {"gate_standard": {"deep_score_min": 80, "min_abs_ic": 0.02, "min_abs_icir": 0.3}},
            "active_context": {"active_factor_summary": {"active_factor_count": 2}, "field_context": {"supported_fields": ["close", "ps_ttm"]}},
            "recent_steps": [],
            "round_events": [],
        },
        lineage_context={
            "current_thesis": [{"thesis_id": "t1", "economic_rationale": "ps_ttm repair", "expected_alpha_mechanism": "delta x low vol"}],
            "current_hypothesis": [{"hypothesis_id": "h1", "thesis_id": "t1", "signal_claim": "repair"}],
            "previous_review_advice": [
                {
                    "from_stage": "deep_validation_review",
                    "to_stage": "expression_design",
                    "reason": "mutate low-vol leg",
                    "recommended_mutation": "return_expression_design",
                }
            ],
        },
        stage_input={"hypotheses": [{"hypothesis_id": "h1", "signal_claim": "repair"}]},
        round_events=[],
        return_handoff={"from_stage": "deep_validation_review", "to_stage": "expression_design", "reason": "mutate low-vol leg"},
    )

    assert "lineage_context" not in payload["context_pack"]
    assert "handoff" not in payload["context_pack"]["current_round_context"]
    assert payload["context_pack"]["upstream_handoff"]["to_stage"] == "expression_design"


def test_llm_transition_uses_existing_stage_outputs_directly():
    transition = svc._llm_transition(
        {
            "summary": "候选设计已经完成。",
            "decision": "propose_candidates",
            "judgment": "候选 c1 可以进入规划检查。",
            "why": "这个候选保留了可检验的收盘价截面排序关系。",
            "history_used": ["此前的研究结论支持保持表达式简洁。"],
            "facts": "候选 c1 的表达式为 rank(close)，机制保持简洁。",
            "candidates": [
                {
                    "candidate_id": "c1",
                    "expression": "rank(close)",
                    "mechanism_summary": "close rank",
                }
            ],
            "stage_transition": {"next_stage": "candidate_plan", "reason": "进入计划"},
        },
        default_next_stage="candidate_plan",
        default_next_action="validate_and_score",
    )

    assert "c1" in transition["facts"]
    assert "rank(close)" in transition["facts"]
    assert transition["judgment"] == "候选 c1 可以进入规划检查。"
    assert transition["why"] == "这个候选保留了可检验的收盘价截面排序关系。"
    assert transition["reason"] == "进入计划"


def test_deepseek_model_order_preserves_explicit_flash_and_configured_fallback():
    assert _llm_models("deepseek-v4-flash")[0] == "deepseek-v4-flash"
    client = DeepSeekJSONClient(model="deepseek-v4-flash")
    assert client.preferred_model() == "deepseek-v4-flash"
    fallback_client = DeepSeekJSONClient(model="")
    assert fallback_client.preferred_model() == deepseek_mod.LLM_CROSS_REVIEW_MODEL


def test_deepseek_provider_model_maps_v4_to_pro():
    assert _provider_model_name("deepseek-v4") == "deepseek-v4-pro"
    assert _provider_model_name("deepseek-v4-flash") == "deepseek-v4-flash"


def test_llm_transition_uses_runtime_model_when_available():
    transition = svc._llm_transition(
        {
            **_natural_language_summary(),
            "decision": "advance_some",
            "judgment": "进入 novelty",
            "why": "B级通过",
            "_orchestrator_llm_model": "deepseek-v4",
        },
        default_next_stage="novelty_review",
        default_next_action="run_novelty",
    )

    assert transition["llm_model"] == "deepseek-v4"


def test_candidate_progress_brief_prefers_id_name_expression():
    brief = svc._candidate_progress_brief(
        {
            "candidate_id": "c1",
            "factor_name": "MyFactor",
            "expression": "rank(close) * rank(volume)",
        }
    )

    assert "c1" in brief
    assert "MyFactor" in brief
    assert "rank(close)" in brief


def test_ensure_factor_naming_repairs_raw_field_name():
    candidate, naming = orchestrator.ensure_factor_naming(
        {
            "candidate_id": "c1",
            "factor_name": "Amount",
            "expression": "rank(ts_mean((cost_85pct-cost_15pct)/close,5)) * rank(-ts_mean(amount/free_share,5))",
            "metadata": {"factor_name": "Amount"},
        }
    )

    assert naming["factor_name_status"] == "repaired"
    assert naming["factor_name_repair_reason"] == "raw_field_name"
    assert candidate["factor_name"] != "Amount"
    assert "CostSpread" in candidate["factor_name"]
    assert candidate["metadata"]["factor_name"] == candidate["factor_name"]


def test_canonical_factor_name_repairs_raw_field_window_name():
    from domain.factor_research.auto_import import canonical_factor_name, classify_factor_expression

    expression = "rank(-ts_mean(cost_85pct / cost_15pct, 5)) * rank(-ts_mean(amount, 5))"
    category = classify_factor_expression(expression)

    name, status = canonical_factor_name(expression, category, proposed_name="AmountMean5")

    assert status == "repaired"
    assert name != "AmountMean5"
    assert "CostRatio" in name
    assert name.startswith("CostRatioMean5")


def test_factor_registry_list_is_read_only(monkeypatch):
    class _Registry:
        def backfill_holding_period_days(self):
            raise AssertionError("factor_registry_list must not backfill holding_period_days")

        def backfill_active_evidence_metadata(self):
            raise AssertionError("factor_registry_list must not backfill evidence metadata")

        def list_all(self, **kwargs):
            return [], 0

        def summary(self):
            return {"active": 0}

    monkeypatch.setattr(svc, "FactorRegistry", _Registry)

    result = svc.factor_registry_list()

    assert result.ok
    assert result.outputs["total"] == 0


def test_factor_atomic_text_write_leaves_no_temp_file(tmp_path):
    target = tmp_path / "state.json"

    svc._atomic_write_text(
        target,
        json.dumps({"status": "ok", "nested": {"count": 1}}, ensure_ascii=False),
    )

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "ok", "nested": {"count": 1}}
    assert not list(tmp_path.glob("*.tmp.*"))


def test_research_step_current_snapshot_write_is_atomic_jsonl(tmp_path, monkeypatch):
    current = tmp_path / "current.jsonl"
    history_dir = tmp_path / "history"
    monkeypatch.setattr(svc, "FACTOR_RESEARCH_STEPS_DIR", tmp_path)
    monkeypatch.setattr(svc, "FACTOR_RESEARCH_STEPS_FILE", current)
    monkeypatch.setattr(svc, "FACTOR_RESEARCH_STEPS_HISTORY_DIR", history_dir)

    svc._write_research_step({"ts": "2026-06-15T00:00:00", "stage": "score_review", "summary": "a"})
    svc._write_research_step({"ts": "2026-06-15T00:00:01", "stage": "novelty_review", "summary": "b"})

    lines = current.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["stage"] for line in lines] == ["score_review", "novelty_review"]
    assert (history_dir / "2026-06-15.jsonl").exists()
    assert not list(tmp_path.glob("*.tmp.*"))


def test_factor_tool_import_enqueues_active_values_refresh_after_import(monkeypatch):
    calls = []

    monkeypatch.setattr(
        svc,
        "_latest_stage_transition",
        lambda: ({"stage": "import_gate_review"}, {"next_stage": "import_review"}),
    )
    monkeypatch.setattr(
        svc,
        "_run_import_factors_isolated",
        lambda **kwargs: {"imported": 1, "skipped": 0, "errors": [], "details": [{"factor_id": "f1"}]},
    )

    def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return {"status": "queued", "registry_fingerprint": "abc"}

    monkeypatch.setattr(svc, "enqueue_active_values_refresh", fake_enqueue)

    result = svc.factor_tool_import(
        candidates=[{"expression": "rank(close)"}],
        universe="all_market",
    )

    assert result.ok
    assert calls == [{"holding_period_days": svc.FACTOR_DEFAULT_HOLDING_PERIOD, "trigger": "fxalpha_import_factors", "refresh_model": True}]
    assert result.outputs["active_values_refresh_required"] is True
    assert result.outputs["active_values_refresh"]["status"] == "queued"
    assert result.outputs["registry_imported"] is True
    assert result.outputs["active_values_refresh_status"] == "queued"
    assert result.outputs["model_feature_snapshot_status"] == "refresh_required"
    assert result.outputs["model_feature_snapshot_trigger"] == "model_side"
    assert result.outputs["model_feature_refresh_status"] == "refresh_required"
    assert result.outputs["import_sync_status"] == {
        "registry_imported": True,
        "active_values": "queued",
        "model_snapshot": "refresh_required",
        "trigger_owner": "model_side",
    }


def test_orchestrator_worker_hands_active_values_refresh_to_long_lived_api(monkeypatch):
    requests = []

    monkeypatch.setenv("FXALPHA_ORCHESTRATOR_WORKER", "1")
    monkeypatch.setattr(
        svc,
        "_latest_stage_transition",
        lambda: ({"stage": "import_gate_review"}, {"next_stage": "import_review"}),
    )
    monkeypatch.setattr(
        svc,
        "_run_import_factors_isolated",
        lambda **kwargs: {"imported": 1, "skipped": 0, "errors": [], "details": [{"factor_id": "f1"}]},
    )
    monkeypatch.setattr(
        svc,
        "enqueue_active_values_refresh",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("short-lived worker must not own refresh thread")),
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "outputs": {
                        "status": "queued",
                        "registry_fingerprint": "abc",
                        "model_refresh_required": True,
                    },
                }
            ).encode()

    class FakeOpener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return FakeResponse()

    monkeypatch.setattr(svc.urllib.request, "build_opener", lambda *args: FakeOpener())

    result = svc.factor_tool_import(
        candidates=[{"expression": "rank(close)"}],
        universe="all_market",
    )

    assert result.ok
    assert result.outputs["active_values_refresh"]["durable_owner"] == "fxalpha-api-18081"
    assert result.outputs["active_values_refresh_status"] == "queued"
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == "http://127.0.0.1:18081/factor/active-values/refresh"
    assert timeout == 20
    assert json.loads(request.data)["source_mode"] == "tail"


def test_import_gate_llm_factor_name_is_applied_to_import_payload():
    candidates = [
        {
            "candidate_id": "c2",
            "factor_name": "AmountMean5",
            "expression": "rank(-ts_mean(cost_85pct / cost_15pct, 5)) * rank(-ts_mean(amount, 5))",
            "metadata": {"factor_name": "AmountMean5"},
        }
    ]
    gate_review = {
        "candidate_decisions": [
            {"candidate_id": "c2", "action": "import", "factor_name": "CostRatioNarrowLowAmount"}
        ]
    }

    enriched = svc._apply_import_gate_factor_names(candidates, gate_review)

    assert enriched[0]["factor_name"] == "CostRatioNarrowLowAmount"
    assert enriched[0]["llm_factor_name"] == "CostRatioNarrowLowAmount"
    assert enriched[0]["previous_factor_name"] == "AmountMean5"
    assert enriched[0]["metadata"]["factor_name_source"] == "import_gate_review_llm"


def test_runtime_view_downgrades_orphaned_orchestrator_to_blocked():
    runtime = {
        "run_id": "run1",
        "round_id": "run1:r0001",
        "stage_id": "run1:r0001:s05_score_review",
        "status": "research_active",
        "current_phase": "Score Review",
        "current_action": "validate_and_score_in_progress",
        "updated_at": "2026-06-15T10:00:00",
        "latest_decision": "running score",
        "latest_step": {"stage": "score_review", "tags": ["orchestrator"]},
        "stage_transition": {"next_action": "validate_and_score_in_progress"},
    }
    pipeline = {
        "overall_status": "research_blocked",
        "message": "orchestrator_orphaned_after_service_restart_or_stale_quantgpt_task",
    }

    downgraded = svc._runtime_view_with_authoritative_liveness(runtime, pipeline)

    assert downgraded["status"] == "research_blocked"
    assert downgraded["current_phase"] == "Blocked"
    assert downgraded["heartbeat_status"] == "interrupted"
    assert downgraded["last_visible_stage"]["stage_id"] == "run1:r0001:s05_score_review"


def test_start_api_18081_owner_check_allows_empty_port(monkeypatch):
    from scripts import start_fxalpha_api_18081 as launcher

    monkeypatch.setattr(launcher, "port_owner", lambda port=18081: {})

    launcher.validate_single_owner()


def test_start_api_18081_owner_check_rejects_systemd_owner(monkeypatch):
    from scripts import start_fxalpha_api_18081 as launcher

    monkeypatch.setattr(
        launcher,
        "port_owner",
        lambda port=18081: {"pid": "123", "cmd": "python3 <repo-root>/scripts/start_fxalpha_api_18081.py", "unit": "fxalpha-api-18081.service"},
    )

    with pytest.raises(SystemExit, match="fxalpha_api_port_owned_by_systemd"):
        launcher.validate_single_owner()


def test_start_api_18081_owner_check_reports_orphan_direct_launcher(monkeypatch):
    from scripts import start_fxalpha_api_18081 as launcher

    monkeypatch.setattr(
        launcher,
        "port_owner",
        lambda port=18081: {"pid": "456", "cmd": "python3 <repo-root>/scripts/start_fxalpha_api_18081.py", "unit": ""},
    )

    with pytest.raises(SystemExit, match="fxalpha_api_port_owned_by_orphan_direct_launcher"):
        launcher.validate_single_owner()


def test_candidate_plan_return_is_detected_as_upstream_reroute():
    result = {
        "decision": "return_hypothesis",
        "next_action": "return_to_hypothesis_design",
        "stage_transition": {
            "next_stage": "hypothesis_design",
            "reason": "need more diversity",
        },
    }

    assert svc._candidate_plan_requests_upstream_return(result) is True


def test_candidate_plan_run_batch_is_not_detected_as_upstream_reroute():
    result = {
        "decision": "run_batch",
        "next_action": "validate_and_score",
        "stage_transition": {
            "next_stage": "score_review",
            "reason": "ready to score",
        },
    }

    assert svc._candidate_plan_requests_upstream_return(result) is False


def test_candidate_plan_model_cannot_fake_code_fatal_precheck_block():
    result = {
        **_natural_language_summary(),
        "decision": "run_batch",
        "judgment": "候选表达式存在明确方向问题。",
        "why": "模型发现方向问题，但代码预检查没有给出 fatal 结论。",
        "history_used": [],
        "candidate_lanes": [
            {
                "candidate_id": "c1",
                "action": "precheck_blocked",
                "keep": False,
                "reason": "方向与假设相反。",
            }
        ],
        "next_action": "validate_and_score",
        "stage_transition": {
            "next_stage": "score_review",
            "reason": "其余候选继续评分。",
        },
        "confidence": 0.8,
    }

    with pytest.raises(DeepSeekClientError, match="precheck_blocked_requires_code_fatal:c1"):
        svc._validate_orchestrator_stage_result(
            "candidate_plan",
            result,
            stage_input={
                "candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
                "code_precheck": [],
            },
        )


def test_candidate_plan_normalizes_semantic_block_when_batch_returns_expression():
    result = {
        **_natural_language_summary(),
        "decision": "revise_expression",
        "judgment": "候选表达式存在明确方向问题。",
        "why": "整批返回表达式设计修正。",
        "history_used": [],
        "candidate_lanes": [
            {
                "candidate_id": "c1",
                "action": "precheck_blocked",
                "keep": False,
                "reason": "方向与假设相反。",
            }
        ],
        "next_action": "return_expression_design",
        "stage_transition": {
            "next_stage": "expression_design",
            "reason": "修正方向语义。",
        },
        "confidence": 0.8,
    }

    svc._validate_orchestrator_stage_result(
        "candidate_plan",
        result,
        stage_input={
            "candidates": [{"candidate_id": "c1", "expression": "rank(close)"}],
            "code_precheck": [],
        },
    )

    assert result["candidate_lanes"][0]["action"] == "score"
    assert result["candidate_lanes"][0]["keep"] is True
    assert result["stage_transition"]["next_stage"] == "expression_design"
    assert any(
        item.startswith("candidate_plan_fake_code_blocks_failed_open_to_score:c1")
        for item in result["_orchestrator_validation_warnings"]
    )


def test_candidate_plan_replays_20260724_false_batch_return_into_quick():
    candidates = [
        {"candidate_id": "c1", "expression": "rank(ts_rank(lg_net_amount, 10)) * rank(cost_15pct)"},
        {
            "candidate_id": "c2",
            "expression": "rank(-ts_delta(borrow_money_bal, 10)) * rank(-ts_delta(turnover_rate, 10))",
        },
        {
            "candidate_id": "c3",
            "expression": "rank(ts_delta(net_asset_ps, 10)) * rank(-ts_std(pct_change, 20))",
        },
        {
            "candidate_id": "c4",
            "expression": "rank(-ts_delta(borrow_money_bal, 10)) * rank(-ts_rank(amount, 10))",
        },
    ]
    checks = svc._candidate_plan_code_precheck(candidates)
    assert checks == []
    result = {
        **_natural_language_summary(),
        "decision": "revise_expression",
        "judgment": "模型错误地声称 c1 命中代码预检查，其余候选可以评分。",
        "why": "历史回执引用了输入中不存在的 ambiguous_centered_leg_product。",
        "history_used": [],
        "candidate_lanes": [
            {
                "candidate_id": "c1",
                "action": "precheck_blocked",
                "keep": False,
                "reason": "错误引用不存在的 code_precheck warning。",
            },
            *[
                {
                    "candidate_id": candidate_id,
                    "action": "score",
                    "keep": True,
                    "reason": "送快筛。",
                }
                for candidate_id in ("c2", "c3", "c4")
            ],
        ],
        "next_action": "return_expression_design",
        "stage_transition": {
            "next_stage": "expression_design",
            "reason": "历史逻辑把一个伪告警升级成整批返回。",
        },
        "confidence": 0.95,
    }

    svc._validate_orchestrator_stage_result(
        "candidate_plan",
        result,
        stage_input={"candidates": candidates, "code_precheck": checks},
    )
    selected = svc._candidate_plan_score_candidates(candidates, checks, result)
    guarded = svc._enforce_candidate_plan_score_transition(
        result,
        score_candidate_count=len(selected),
    )

    assert [item["candidate_id"] for item in selected] == ["c1", "c2", "c3", "c4"]
    assert guarded["next_action"] == "validate_and_score_candidates"
    assert guarded["stage_transition"]["next_stage"] == "score_review"


def test_candidate_plan_routes_ambiguous_centered_leg_to_revision_without_blocking_siblings():
    result = {
        **_natural_language_summary(),
        "decision": "run_batch",
        "judgment": "候选表达式方向一致。",
        "why": "模型声称两个确认条件可以直接相乘。",
        "history_used": [],
        "candidate_lanes": [
            {
                "candidate_id": "c1",
                "action": "score",
                "keep": True,
                "reason": "送快筛。",
            },
            {
                "candidate_id": "c2",
                "action": "score",
                "keep": True,
                "reason": "送快筛。",
            },
        ],
        "next_action": "validate_and_score",
        "stage_transition": {
            "next_stage": "score_review",
            "reason": "进入快筛。",
        },
        "confidence": 0.8,
    }

    candidates = [
        {
            "candidate_id": "c1",
            "expression": "zscore(rank(close)) * zscore(rank(-cost_15pct))",
        },
        {"candidate_id": "c2", "expression": "rank(close) + rank(-cost_15pct)"},
    ]
    checks = [
        {
            "candidate_id": "c1",
            "fatal": False,
            "warnings": [
                "ambiguous_centered_leg_product:"
                "zscore_or_ts_zscore_multiplication_rewards_both_double_high_and_double_low"
            ],
        },
        {"candidate_id": "c2", "fatal": False, "warnings": []},
    ]

    svc._validate_orchestrator_stage_result(
        "candidate_plan",
        result,
        stage_input={"candidates": candidates, "code_precheck": checks},
    )
    selected = svc._candidate_plan_score_candidates(candidates, checks, result)
    guarded = svc._enforce_candidate_plan_score_transition(
        result,
        score_candidate_count=len(selected),
    )

    lane_map = {item["candidate_id"]: item for item in result["candidate_lanes"]}
    assert lane_map["c1"]["action"] == "revise_expression"
    assert lane_map["c1"]["keep"] is False
    assert lane_map["c2"]["action"] == "score"
    assert lane_map["c2"]["keep"] is True
    assert [item["candidate_id"] for item in selected] == ["c2"]
    assert guarded["stage_transition"]["next_stage"] == "score_review"
    assert guarded["next_action"] == "validate_and_score_candidates"


def test_candidate_plan_batch_semantic_return_does_not_hold_back_scoreable_lanes():
    result = {
        "decision": "revise_expression",
        "next_action": "return_expression_design",
        "stage_transition": {
            "next_stage": "expression_design",
            "reason": "候选方向语义矛盾，整批返回修正。",
        },
    }

    guarded = svc._enforce_candidate_plan_score_transition(
        result,
        score_candidate_count=3,
    )

    assert guarded["next_action"] == "validate_and_score_candidates"
    assert guarded["stage_transition"]["next_stage"] == "score_review"


def test_review_stage_derives_code_advice_alignment_after_llm_without_requiring_self_report():
    stage_input = {
        "code_advice": {
            "candidate_lane_decisions": [
                {"candidate_id": "c1", "action": "mutate_interaction"},
                {"candidate_id": "c2", "action": "advance_to_novelty"},
            ]
        }
    }
    base_result = {
        **_natural_language_summary(),
        "decision": "advance_some",
        "judgment": "候选 c2 可以前进，候选 c1 需要变异。",
        "why": "正式证据和代码建议共同支持这一候选级取舍。",
        "history_used": [],
        "candidate_decisions": [
            {"candidate_id": "c1", "action": "revise_expression", "reason": "single_signal"},
            {"candidate_id": "c2", "action": "advance_to_novelty", "reason": "quick_grade_ab"},
        ],
        "next_action": "run_novelty",
        "stage_transition": {"next_stage": "novelty_review", "reason": "保留候选已经通过快筛，因此进入新颖性复核。"},
        "confidence": 0.8,
    }

    missing_alignment = dict(base_result)
    svc._validate_orchestrator_stage_result("score_review", missing_alignment, stage_input=stage_input)

    alignment = missing_alignment["code_advice_alignment"]
    assert alignment["source"] == "deterministic_post_llm_audit"
    assert alignment["items"][0]["candidate_id"] == "c1"
    assert alignment["items"][0]["alignment"] == "refine"
    assert alignment["items"][1]["candidate_id"] == "c2"
    assert alignment["items"][1]["alignment"] == "follow"

    aligned = {
        **base_result,
        "code_advice_alignment": {
            "overall": "refine",
            "items": [
                {
                    "candidate_id": "c1",
                    "code_action": "mutate_interaction",
                    "llm_action": "revise_expression",
                    "alignment": "refine",
                    "reason": "same mutation direction with clearer return target",
                },
                {
                    "candidate_id": "c2",
                    "code_action": "advance_to_novelty",
                    "llm_action": "advance_to_novelty",
                    "alignment": "follow",
                    "reason": "same action",
                },
            ],
        },
    }

    svc._validate_orchestrator_stage_result("score_review", aligned, stage_input=stage_input)
    assert aligned["code_advice_alignment"]["source"] == "deterministic_post_llm_audit"
    assert aligned["code_advice_alignment"]["items"][0]["reason"] != "same mutation direction with clearer return target"


def test_review_stage_maps_zero_based_idx_advice_to_candidate_ids():
    stage_input = {
        "code_advice": {
            "candidate_lane_decisions": [
                {"idx": 0, "action": "orthogonalize_or_switch_source"},
            ]
        }
    }
    result = {
        **_natural_language_summary(),
        "decision": "return_thesis",
        "judgment": "该候选没有通过新颖性复核。",
        "why": "正式的新颖性否决证据优先于模型的主观判断。",
        "history_used": [],
        "candidate_decisions": [
            {"candidate_id": "c1", "action": "return_thesis", "reason": "novelty veto"},
        ],
        "code_advice_alignment": {
            "overall": "follow",
            "items": [
                {
                    "candidate_id": "c1",
                    "code_action": "orthogonalize_or_switch_source",
                    "llm_action": "return_thesis",
                    "alignment": "follow",
                    "reason": "switch source requires returning to thesis design",
                }
            ],
        },
        "next_action": "return_thesis_design",
        "stage_transition": {"next_stage": "thesis_design", "reason": "现有机制受到新颖性否决，需要返回主线设计更换信息来源。"},
        "confidence": 0.9,
    }

    svc._validate_orchestrator_stage_result("novelty_review", result, stage_input=stage_input)


def test_deepseek_json_mode_defaults_off_for_deepseek(monkeypatch):
    monkeypatch.delenv("FXALPHA_DEEPSEEK_JSON_MODE", raising=False)

    assert _deepseek_json_mode_enabled() is False


def test_dict_candidate_lanes_are_normalized_for_prompt_projection_and_events():
    payload = {
        "keepers": [{"candidate_id": "k1", "expression": "rank(close)", "score": 68.0, "grade": "B"}],
        "dropped": [{"candidate_id": "d1", "expression": "rank(open)", "score": 41.0, "grade": "C"}],
    }

    compact_prompt = svc._compact_stage_tool_evidence_for_prompt(
        stage="score_review",
        stage_input={
            "candidate_lanes": payload,
            "validate_results": [],
            "score_factor_results": [],
        },
    )
    event = {
        "run_id": "run1",
        "round_id": "run1:r0001",
        "stage_id": "s1",
        "stage": "novelty_review",
        "summary": "Novelty done",
        "decision": "advance",
        "candidate_lanes": payload,
        "stage_transition": {"next_stage": "deep_validation_review"},
    }
    monitoring = svc._orchestrator_projection_monitoring(event)
    refs = svc._orchestrator_projection_evidence_refs(event)
    compact_event = svc._compact_orchestrator_event(event)

    assert [item["candidate_id"] for item in compact_prompt["candidate_lanes"]] == ["k1", "d1"]
    assert {item["candidate_id"] for item in monitoring["candidate_watch"]} == {"k1", "d1"}
    assert refs[-1]["type"] == "candidate_lanes"
    assert refs[-1]["lane_counts"] == {"keepers": 1, "dropped": 1}
    assert compact_event["candidate_lane_count"] == 2
    assert compact_event["candidate_lane_counts"] == {"keepers": 1, "dropped": 1}


def test_message_reasoning_text_reads_model_dump_reasoning():
    class FakeMessage:
        def model_dump(self):
            return {"content": "", "reasoning_content": "先分析，再输出。"}

    assert _message_reasoning_text(FakeMessage()) == "先分析，再输出。"


def test_deepseek_complete_json_repairs_reasoning_only_response(monkeypatch):
    calls = []

    class FakeMessage:
        def __init__(self, *, content="", reasoning=""):
            self.content = content
            self.reasoning_content = reasoning

        def model_dump(self):
            return {
                "content": self.content,
                "reasoning_content": self.reasoning_content,
            }

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            message=FakeMessage(
                                content="",
                                reasoning="需要输出 thesis_design 的严格 JSON，先给出两个 thesis。",
                            )
                        )
                    ]
                )
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=FakeMessage(
                            content=json.dumps(
                                {
                                    "decision": "propose_theses",
                                    "judgment": "ok",
                                    "why": "ok",
                                    "history_used": [],
                                    "theses": [{"thesis_id": "t1", "economic_rationale": "x"}],
                                    "next_action": "advance_to_hypothesis_design",
                                    "stage_transition": {"next_stage": "hypothesis_design", "reason": "ok"},
                                    "confidence": 0.7,
                                },
                                ensure_ascii=False,
                            )
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))

    client = DeepSeekJSONClient(model="deepseek-v4", api_key="test-key", base_url="https://api.deepseek.com/v1", timeout=3)
    result = client.complete_json(
        system="system",
        payload={
            "task": "thesis_design",
            "stage_briefing": "提出 thesis",
            "output_contract": {"json_only": True},
        },
        max_tokens=400,
    )

    assert result["decision"] == "propose_theses"
    assert result["_orchestrator_llm_repaired_from_reasoning"] is True
    assert len(calls) == 2
    assert calls[0]["model"] == "deepseek-v4-pro"
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "response_format" not in calls[0]
    assert "内部分析摘录" in calls[1]["messages"][1]["content"]


def test_deepseek_transport_error_retries_original_messages(monkeypatch):
    calls = []

    class FakeMessage:
        content = json.dumps({"decision": "ok", "stage_transition": {"next_stage": "hypothesis_design"}}, ensure_ascii=False)
        reasoning_content = ""

        def model_dump(self):
            return {"content": self.content, "reasoning_content": ""}

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TimeoutError("request timed out")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=FakeMessage())])

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))

    payload = {"task": "thesis_design", "context_pack": {"history_context": {"short_term_history": {"stage_relevant_steps": ["x"]}}}}
    client = DeepSeekJSONClient(model="deepseek-v4-flash", api_key="test-key", base_url="https://api.deepseek.com", timeout=3)
    result = client.complete_json(system="system prompt", payload=payload, max_tokens=200)

    assert result["decision"] == "ok"
    assert len(calls) == 2
    assert calls[0]["messages"] == calls[1]["messages"]
    assert "上一轮返回" not in calls[1]["messages"][1]["content"]
    assert calls[1]["model"] == "deepseek-v4-flash"


def test_complete_stage_json_retries_with_shrunk_payload(monkeypatch):
    calls = []

    def fake_complete(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise DeepSeekClientError("llm_response_not_valid_json:Expecting ',' delimiter", raw_preview='{"bad": ')
        return {
            **_natural_language_summary(),
            "stage": "expression_design",
            "decision": "propose_candidates",
            "judgment": "候选表达式已经完成设计。",
            "why": "候选结构合法且能够进入后续规划检查。",
            "history_used": [],
            "candidates": [
                {
                    "candidate_id": "c1",
                    "hypothesis_id": "h1",
                    "expression": "rank(close)",
                    "expected_direction": "positive",
                    "mechanism_summary": "simple",
                    "complexity_intent": "simple",
                    "factor_name_hint": "CloseRank",
                }
            ],
            "blocked_reason": None,
            "next_action": "validate_and_score",
            "stage_transition": {"next_stage": "candidate_plan", "reason": "表达式设计完成后需要进行候选规划检查。"},
            "confidence": 0.8,
        }

    monkeypatch.setattr(svc, "_complete_orchestrator_llm_json", fake_complete)

    result = svc._complete_orchestrator_stage_json(
        client=object(),
        run_id="run1",
        round_id="run1:r0001",
        stage="expression_design",
        context_pack={
            "run_state": {"run_id": "run1", "round_id": "run1:r0001", "contract": {"target_adopted": 1, "holding_period": 5}},
            "active_context": {"active_factor_summary": {"active_factor_count": 2}, "field_context": {"supported_fields": ["close", "dv_ttm"]}},
            "recent_steps": [{"stage": "score_review", "summary": "s"} for _ in range(8)],
        },
        stage_input={
            "hypotheses": [{"hypothesis_id": "h1", "signal_claim": "x"}],
            "operator_list_summary": ["rank", "ts_delta"],
            "field_context": {"supported_fields": ["close", "dv_ttm"]},
            "expression_rules": "strict",
            "complexity_limits": {"max_nested_depth_soft": 8},
        },
        lineage_context={
            "current_thesis": [{"thesis_id": "t1", "economic_rationale": "a", "expected_alpha_mechanism": "b"} for _ in range(3)],
            "current_hypothesis": [{"hypothesis_id": "h1", "signal_claim": "s"} for _ in range(4)],
            "parent_candidates": [{"candidate_id": "c1", "expression": "rank(close)"} for _ in range(5)],
        },
        round_events=[{"stage": "score_review", "summary": "x"} for _ in range(6)],
    )

    assert result["decision"] == "propose_candidates"
    assert len(calls) == 2
    assert calls[1]["checkpoint"] == "expression_design_repair"


def test_complete_stage_json_retries_on_next_stage_contract_violation(monkeypatch):
    calls = []

    def fake_complete(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {
                **_natural_language_summary(),
                "stage": "thesis_design",
                "decision": "propose_theses",
                "judgment": "模型尝试跳过假设设计阶段。",
                "why": "该流转不符合当前阶段允许的状态机路径。",
                "history_used": [],
                "theses": [{"thesis_id": "t1", "economic_rationale": "a", "expected_alpha_mechanism": "b", "preferred_data_families": ["close"], "avoid_patterns": [], "priority": "high"}],
                "next_action": "advance_to_expression_design",
                "stage_transition": {"next_stage": "expression_design", "reason": "模型错误地要求直接进入表达式设计。"},
                "confidence": 0.2,
            }
        return {
            **_natural_language_summary(),
            "stage": "thesis_design",
            "decision": "propose_theses",
            "judgment": "研究主线可以进入假设设计。",
            "why": "主线具备明确经济机制和可用字段依据。",
            "history_used": [],
            "theses": [{"thesis_id": "t1", "economic_rationale": "a", "expected_alpha_mechanism": "b", "preferred_data_families": ["close"], "avoid_patterns": [], "priority": "high"}],
            "next_action": "advance_to_hypothesis_design",
            "stage_transition": {"next_stage": "hypothesis_design", "reason": "研究主线明确，下一步需要形成可检验假设。"},
            "confidence": 0.8,
        }

    monkeypatch.setattr(svc, "_complete_orchestrator_llm_json", fake_complete)

    result = svc._complete_orchestrator_stage_json(
        client=object(),
        run_id="run1",
        round_id="run1:r0001",
        stage="thesis_design",
        context_pack={
            "run_state": {"run_id": "run1", "round_id": "run1:r0001", "contract": {"target_adopted": 1, "holding_period": 5}},
            "protocol": {"gate_standard": {"deep_score_min": 80, "min_abs_ic": 0.02, "min_abs_icir": 0.3}},
            "active_context": {"active_factor_summary": {"active_factor_count": 2}, "field_context": {"supported_fields": ["close", "dv_ttm"]}},
            "recent_steps": [{"stage": "round_synthesis", "summary": "s"} for _ in range(8)],
        },
        stage_input={"blocked_or_failed_reasons": {}, "available_field_families": ["close", "dv_ttm"], "target_constraints": {"need_active_factors": 1}},
        lineage_context={},
        round_events=[{"stage": "round_synthesis", "summary": "x"} for _ in range(6)],
    )

    assert result["stage_transition"]["next_stage"] == "hypothesis_design"
    assert len(calls) == 2
    assert calls[1]["checkpoint"] == "thesis_design_repair"
    assert "retry_contract" in calls[1]["payload"]


def test_score_review_retry_keeps_sufficient_output_budget(monkeypatch):
    calls = []

    def fake_complete(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise DeepSeekClientError("llm_response_not_valid_json:Expecting ',' delimiter", raw_preview='{"candidate_decisions":')
        return {
            **_natural_language_summary(),
            "decision": "advance_some",
            "judgment": "候选 c1 已经通过快筛。",
            "why": "该候选取得 B 级快筛结果，可以继续接受新颖性检查。",
            "history_used": [],
            "candidate_decisions": [{"candidate_id": "c1", "action": "advance_to_novelty", "reason": "B grade"}],
            "code_advice_alignment": {"overall": "follow", "items": [{"candidate_id": "c1", "code_action": "advance_to_novelty", "llm_action": "advance_to_novelty", "alignment": "follow", "reason": "same"}]},
            "next_action": "run_novelty",
            "stage_transition": {"next_stage": "novelty_review", "reason": "快筛保留候选需要继续接受新颖性复核。"},
            "confidence": 0.9,
        }

    monkeypatch.setattr(svc, "_complete_orchestrator_llm_json", fake_complete)
    svc._complete_orchestrator_stage_json(
        client=object(), run_id="run1", round_id="run1:r0001", stage="score_review",
        context_pack={},
        stage_input={"candidate_lanes": [{"candidate_id": "c1"}], "validate_results": [], "score_factor_results": [{"candidate_id": "c1", "status": "success", "score": 72, "grade": "B"}], "code_advice": {}},
        lineage_context=None, round_events=None, max_tokens=3600,
    )

    assert len(calls) == 2
    assert calls[1]["checkpoint"] == "score_review_repair"
    assert calls[1]["max_tokens"] == 3600


def test_llm_request_progress_step_updates_gui_chain_clean(monkeypatch, tmp_path):
    steps_file, _ = _redirect_orchestrator(monkeypatch, tmp_path)

    svc._write_research_step(
        {
            "schema_version": "research_step_v2",
            "ts": "2026-06-14T10:00:00",
            "run_id": "run-orch",
            "round_id": "run-orch:r0001",
            "stage_seq": 5,
            "stage_id": "run-orch:r0001:s05_score_review",
            "previous_stage": "candidate_plan",
            "previous_stage_id": "run-orch:r0001:s04_candidate_plan",
            "stage": "score_review",
            "summary": "开始快筛",
            "decision": "等待 score_factor",
            "stage_transition": {"next_stage": "score_review", "next_action": "validate_and_score_in_progress"},
            "tags": ["orchestrator", "tool_progress", "score_review_progress"],
        }
    )

    svc._write_orchestrator_llm_request_step(
        run_id="run-orch",
        round_id="run-orch:r0001",
        stage="score_review",
        checkpoint="score_review",
        trace_id="run-orch:r0001:score_review:abcd1234",
        payload_chars=12345,
        llm_model="deepseek-v4",
        prompt_digest={
            "stage_briefing": "你现在处于 quick score review 阶段。",
            "history_used": ["deep_validation_review / mutate / 返回 expression_design"],
            "facts": "recent_rounds=6 | review_anchors=3 | score_factor_results=4",
            "handoff_reason": "上一轮 deep_score 不足",
            "tool_summary": ["score_factor_results=4"],
        },
    )

    step = json.loads(steps_file.read_text(encoding="utf-8").splitlines()[-1])
    assert step["stage"] == "score_review"
    assert step["decision"] == "进入 LLM review 阶段，等待 DeepSeek v4 返回 JSON 决策。"
    assert step["stage_transition"]["next_action"] == "llm_review_in_progress"
    assert "等待 DeepSeek v4 返回 score_review 阶段 JSON 研究判断" in step["stage_transition"]["judgment"]
    assert "deep_validation_review / mutate" in step["stage_transition"]["history_used"]
    assert "上下文摘要（非模型判断）" in step["stage_transition"]["facts"]
    assert "score_factor_results=4" in step["stage_transition"]["facts"]
    assert "你现在处于 quick score review 阶段。" in step["stage_transition"]["research_strategy"]
    assert step["monitoring"]["event_type"] == "llm_request"
    assert step["llm_trace_id"] == "run-orch:r0001:score_review:abcd1234"
    assert step["evidence_refs"][1]["type"] == "context_pack_digest"


def test_compact_stage_history_prefers_substantive_steps_over_request_noise_clean():
    history = svc._compact_stage_history(
        {
            "active_context": {},
            "recent_steps": [
                {
                    "stage": "score_review",
                    "summary": "DeepSeek v4 已收到 score_review 阶段证据，正在生成研究判断。",
                    "decision": "进入 LLM review 阶段，等待 DeepSeek v4 返回 JSON 决策。",
                    "tags": ["orchestrator", "deepseek_v4", "llm_request_progress", "score_review"],
                    "monitoring": {"event_type": "llm_request"},
                    "stage_transition": {"next_stage": "score_review", "next_action": "llm_review_in_progress"},
                },
                {
                    "stage": "score_review",
                    "summary": "Quick score 完成。",
                    "decision": "advance_some",
                    "tags": ["orchestrator", "deepseek_v4", "score_review", "llm_review"],
                    "stage_transition": {
                        "next_stage": "novelty_review",
                        "next_action": "run_novelty",
                        "judgment": "B级候选可进入 novelty",
                        "why": "IC/ICIR 达标",
                    },
                },
            ],
        },
        stage="score_review",
        round_events=[],
    )

    recent = history["short_term_history"]["stage_relevant_steps"]
    assert recent[0]["decision"] == "advance_some"
    assert all(item["decision"] != "进入 LLM review 阶段，等待 DeepSeek v4 返回 JSON 决策。" for item in recent[:1])


def test_orchestrator_prompt_contract_uses_clean_chinese_stage_briefing():
    assert "量化因子研究员" in svc._ORCHESTRATOR_RESEARCH_SYSTEM
    assert svc._ORCHESTRATOR_STAGE_BRIEFINGS["score_review"].startswith("你现在处于 score_review 阶段")
    assert "证据优先级" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["score_review"]
    assert "code_advice_alignment" not in svc._ORCHESTRATOR_STAGE_BRIEFINGS["score_review"]
    for stage in ("thesis_design", "hypothesis_design"):
        briefing = svc._ORCHESTRATOR_STAGE_BRIEFINGS[stage]
        assert "factor_map_context" in briefing
        assert "最近" in briefing
        assert "数量" in briefing
    expression = svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]
    assert "factor_map_context" not in expression
    assert "EXPLORE" in expression
    assert "RECOMBINE" in expression
    assert "SIMPLIFY" in expression
    assert "exact_do_not_repeat" in expression
    assert "完全相同必须删除" in expression
    assert "不要求填满" in expression
    novelty = svc._ORCHESTRATOR_STAGE_BRIEFINGS["novelty_review"]
    assert "orthogonalize_or_switch_source 不是 explore_new_thesis" in novelty
    assert "本次再次被拒绝" in novelty
    assert "不再消耗第三次同族尝试" in novelty
    assert "hypothesis_design" in svc._ORCHESTRATOR_ALLOWED_NEXT_STAGES["novelty_review"]
    assert "无法明确判断的合法候选默认 score" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["candidate_plan"]
    assert "code_precheck fatal" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["candidate_plan"]
    assert "code_advice_alignment" not in svc._ORCHESTRATOR_STAGE_REQUIRED["score_review"]
    assert "code_advice_alignment" not in svc._ORCHESTRATOR_STAGE_SCHEMAS["score_review"]
    for schema in svc._ORCHESTRATOR_STAGE_SCHEMAS.values():
        assert re.search(r"[\u4e00-\u9fff]", schema["summary"])
        assert re.search(r"[\u4e00-\u9fff]", schema["judgment"])
        assert re.search(r"[\u4e00-\u9fff]", schema["why"])
        assert schema["history_used"] == []
        assert re.search(r"[\u4e00-\u9fff]", schema["stage_transition"]["reason"])


def test_orchestrator_system_prompt_preserves_core_contract_with_information_audit_source():
    assert "只使用本次实际提供的字段" in svc._ORCHESTRATOR_RESEARCH_SYSTEM
    assert "不要编造字段、表达式、分数" in svc._ORCHESTRATOR_RESEARCH_SYSTEM
    assert "factor_map_context" not in svc._ORCHESTRATOR_RESEARCH_SYSTEM
    assert "candidate_decisions" not in svc._ORCHESTRATOR_RESEARCH_SYSTEM
    assert "code_advice_alignment" not in svc._ORCHESTRATOR_RESEARCH_SYSTEM
    assert "active 因子库已经覆盖的经济关系" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["thesis_design"]
    assert "guidance.action=avoid_near_copy" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["thesis_design"]
    assert "主字段、角色和关系" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["hypothesis_design"]
    assert "direction 只用 positive 或 negative" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["hypothesis_design"]
    assert "factor_map_context" not in svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]
    assert "逐一比较" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]
    assert "低值看多通常需要反向 rank" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]
    assert "rank(ts_rank(x,w)) 的高值明确代表 x 处于自身历史高位" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]
    assert "目标经济场景必须使每条腿都取高值" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]
    assert "每条腿做方向检查" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]
    assert "where 必须解释条件和两个分支" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]
    candidate_plan = svc._ORCHESTRATOR_STAGE_BRIEFINGS["candidate_plan"]
    assert "最终因子高值是否真的对应 expected_direction" in candidate_plan
    assert "rank(ts_rank(x,w)) 奖励历史高位，不是历史低位" in candidate_plan
    assert "rank(-ts_delta(x,10)/ts_mean(x,60)) 的高值表示 x 下降更多" in candidate_plan


def test_prompt_research_space_matches_alpha_precheck_field_contract():
    from quantgpt.data_schema import AVAILABLE_FIELDS, BLOCKED_FIELDS, FIELD_ALIASES

    alpha_blocked = {"up_limit", "down_limit", "backward_factor"}
    supported = sorted(set(AVAILABLE_FIELDS) - set(BLOCKED_FIELDS) - alpha_blocked)
    context = {
        "supported_fields": supported,
        "blocked_fields": {**BLOCKED_FIELDS, **{field: "alpha blocked" for field in alpha_blocked}},
        "aliases": FIELD_ALIASES,
        "field_descriptions": {field: AVAILABLE_FIELDS[field] for field in supported},
    }

    assert "up_limit" not in context["supported_fields"]
    assert "down_limit" not in context["supported_fields"]
    assert "backward_factor" not in context["supported_fields"]
    assert {"up_limit", "down_limit", "backward_factor"} <= set(context["blocked_fields"])

    research_space = svc._candidate_context_research_space_for_stage(context, stage="hypothesis_design")
    assert set(research_space["field_constraints"]["field_descriptions"]) == set(
        research_space["supported_fields"]
    )


def test_expression_prompt_exposes_complete_operator_palette_and_signatures():
    palette = svc._orchestrator_supported_operator_palette()
    summary = svc._compact_operator_list_summary_for_prompt(
        {"supported_operators": palette}
    )

    assert len(palette) == 40
    assert summary["supported_operators"] == palette
    assert set(summary["operator_signatures"]) == set(palette)


def test_each_llm_stage_receives_only_its_declared_active_context_contract():
    active_context = {
        "factor_map_context": {
            "available": True,
            "map_id": "fm_current",
            "audit_id": "fa_current",
            "regions": [
                {"cluster_id": "information_001", "region_uid": "region_one", "size": 1, "representative": {"factor_id": "f1", "name": "AmountFlow", "expression": "rank(ts_mean(amount, 5))"}, "members": [{"factor_id": "f1", "name": "AmountFlow", "expression": "rank(ts_mean(amount, 5))"}]},
            ],
        },
        "field_context": {
            "supported_fields": ["amount", "pct_change"],
            "blocked_fields": ["security_name"],
        },
    }
    for stage in svc._ORCHESTRATOR_STAGE_BRIEFINGS:
        compact = svc._compact_stage_active_context_for_prompt(
            run_state={},
            active_context=active_context,
            stage=stage,
        )
        policy = svc._ORCHESTRATOR_STAGE_CONTEXT_POLICY[stage]
        if policy["research_space"] == "none" and not policy.get("factor_map"):
            assert compact == {}, stage
            continue
        if policy["research_space"] != "none":
            assert compact["research_space"]["supported_fields"] == ["amount", "pct_change"], stage
            if policy["research_space"] == "full":
                assert compact["research_space"]["supported_operators"], stage
            else:
                assert "supported_operators" not in compact["research_space"], stage
        if policy.get("factor_map"):
            if stage == "round_synthesis":
                assert "factor_map_context" not in compact, stage
            else:
                assert compact["factor_map_context"]["map_id"] == "fm_current", stage
                assert compact["factor_map_context"]["audit_id"] == "fa_current", stage
        else:
            assert "factor_map_context" not in compact, stage
        assert "active_factor_samples" not in compact, stage
        assert "symbolic_family_representatives" not in compact, stage


def test_design_stage_payload_preserves_thesis_hypothesis_and_diversity_semantics():
    context_pack = {
        "run_state": {"run_id": "run1", "round_id": "run1:r0001"},
        "active_context": {
            "active_factor_summary": {"active_factor_count": 0, "active_factors": []},
            "field_context": {"supported_fields": ["amount", "close"]},
        },
        "recent_steps": [],
    }
    thesis = {
        "thesis_id": "t1",
        "economic_rationale": "资金流持续性",
        "expected_alpha_mechanism": "慢变量资金流被价格逐步吸收",
        "preferred_data_families": ["moneyflow"],
        "avoid_patterns": ["window-only variants"],
        "priority": "high",
    }
    hypothesis = {
        "hypothesis_id": "h1",
        "thesis_id": "t1",
        "signal_claim": "净流入持续且价格确认较弱时存在后续收益",
        "expected_direction": "positive",
        "candidate_variable_groups": [{"role": "main", "fields": ["amount"]}],
        "window_policy": "main 10-20, confirmation 5",
        "normalization_policy": "cross-sectional rank",
        "risk_notes": ["amount unit"],
        "mutation_plan_if_fail": ["change confirmation leg"],
    }

    hypothesis_payload = svc._orchestrator_stage_payload(
        stage="hypothesis_design",
        context_pack=context_pack,
        stage_input={"selected_theses": [thesis], "field_context": context_pack["active_context"]["field_context"]},
        lineage_context={"current_thesis": [thesis]},
    )
    expression_payload = svc._orchestrator_stage_payload(
        stage="expression_design",
        context_pack=context_pack,
        stage_input={
            "hypotheses": [hypothesis],
            "operator_list_summary": ["rank", "ts_mean"],
            "field_context": context_pack["active_context"]["field_context"],
            "diversity_budget": {"same_expression_family_soft_max": 3},
        },
        lineage_context={"current_thesis": [thesis], "current_hypothesis": [hypothesis]},
    )

    selected = hypothesis_payload["context_pack"]["current_round_context"]["thesis"][0]
    direct_hypothesis = expression_payload["context_pack"]["current_round_context"]["hypotheses"][0]
    assert selected["economic_rationale"] == thesis["economic_rationale"]
    assert selected["expected_alpha_mechanism"] == thesis["expected_alpha_mechanism"]
    assert direct_hypothesis["signal_claim"] == hypothesis["signal_claim"]
    assert direct_hypothesis["normalization_policy"] == hypothesis["normalization_policy"]
    assert expression_payload["context_pack"]["tool_evidence"]["diversity_budget"]["same_expression_family_soft_max"] == 3
    assert "selected_theses" not in hypothesis_payload["context_pack"].get("tool_evidence", {})
    assert "hypotheses" not in expression_payload["context_pack"]["tool_evidence"]


def test_downstream_payload_keeps_candidate_plan_and_stage_specific_evidence():
    context_pack = {"run_state": {}, "active_context": {}, "recent_steps": []}
    candidate_plan = {
        "stage": "candidate_plan",
        "decision": "run_batch",
        "judgment": "score all nonfatal candidates",
        "why": "fatal-only policy",
        "history_used": [],
        "candidate_lanes": [{"candidate_id": "c1", "lane": "primary", "keep": True, "reason": "nonfatal"}],
        "next_action": "validate_and_score",
        "stage_transition": {"next_stage": "score_review", "reason": "run"},
        "confidence": 0.9,
    }
    lineage = svc._stage_lineage_context(
        thesis_result={"theses": [{"thesis_id": "t1", "economic_rationale": "r"}]},
        hypothesis_result={"hypotheses": [{"hypothesis_id": "h1", "thesis_id": "t1", "signal_claim": "s"}]},
        expression_result={"candidates": [{"candidate_id": "c1", "hypothesis_id": "h1", "expression": "rank(close)"}]},
        candidate_plan_result=candidate_plan,
    )
    score_payload = svc._orchestrator_stage_payload(
        stage="score_review",
        context_pack=context_pack,
        stage_input={"score_factor_results": [{"candidate_id": "c1", "expression": "rank(close)", "score": 0, "grade": "D"}]},
        lineage_context=lineage,
    )
    deep_payload = svc._orchestrator_stage_payload(
        stage="deep_validation_review",
        context_pack=context_pack,
        stage_input={
            "deep_results": {
                "candidates": [{"candidate_id": "c1", "backtest_summary": {"ic_mean": 0.03}, "rolling_validation": {"score": 75}}],
                "missing_evidence": [{"candidate_id": "c1", "components": ["anti_overfit"]}],
                "system_errors": [{"candidate_id": "c2", "status": "deep_validation_error", "error": "timeout"}],
            }
        },
        lineage_context=lineage,
    )

    assert score_payload["context_pack"]["current_round_context"]["candidate_plan"]["candidate_lanes"][0]["candidate_id"] == "c1"
    assert score_payload["context_pack"]["tool_evidence"]["score_factor_results"][0]["score"] == 0
    deep_results = deep_payload["context_pack"]["tool_evidence"]["deep_results"]
    assert deep_results["missing_evidence"][0]["candidate_id"] == "c1"
    assert deep_results["system_errors"][0]["status"] == "deep_validation_error"


def test_score_review_payload_preserves_all_ten_candidate_results_and_code_advice():
    candidates = [
        {
            "candidate_id": f"c{idx}",
            "expression": f"rank(ts_mean(close,{idx}))",
            "score": 70 + idx / 10,
            "grade": "B",
            "status": "success",
        }
        for idx in range(1, 11)
    ]
    advice = {
        "action": "advance_to_novelty",
        "candidate_lane_decisions": [
            {"candidate_id": f"c{idx}", "action": "advance_to_novelty", "reason": "quick_grade_ab"}
            for idx in range(1, 11)
        ],
    }

    payload = svc._orchestrator_stage_payload(
        stage="score_review",
        context_pack={"run_state": {}, "active_context": {}, "recent_steps": []},
        stage_input={
            "candidate_lanes": candidates,
            "validate_results": [{"candidate_id": item["candidate_id"], "status": "valid"} for item in candidates],
            "score_factor_results": candidates,
            "code_advice": advice,
        },
        lineage_context={},
    )
    context = payload["context_pack"]

    assert [item["candidate_id"] for item in context["tool_evidence"]["score_factor_results"]] == [f"c{idx}" for idx in range(1, 11)]
    assert len(context["tool_evidence"]["candidate_lanes"]) == 10
    assert len(context["tool_evidence"]["validate_results"]) == 10
    assert [item["candidate_id"] for item in context["code_advice"]["candidate_lane_decisions"]] == [f"c{idx}" for idx in range(1, 11)]


def test_import_and_round_payloads_expose_real_gate_import_and_code_advice():
    context_pack = {"run_state": {}, "active_context": {}, "recent_steps": []}
    gate_review = {
        "stage": "import_gate_review",
        "decision": "adopted",
        "judgment": "gate passed",
        "why": "official adopted",
        "history_used": [],
        "candidate_decisions": [{"candidate_id": "c1", "action": "import", "reason": "adopted"}],
        "next_action": "import_factor",
        "stage_transition": {"next_stage": "import_review", "reason": "adopted"},
        "confidence": 0.9,
    }
    import_payload = svc._orchestrator_stage_payload(
        stage="import_review",
        context_pack=context_pack,
        stage_input={
            "gate_review_summary": gate_review,
            "import_results": {"imported": 1, "details": [{"candidate_id": "c1", "factor_id": "f1", "status": "imported"}]},
            "registry_summary": {"active": 65},
            "import_sync_status": {"active_values": "fresh"},
            "code_advice": {"import_summary": {"imported_count": 1, "failed_count": 0}, "warnings": []},
        },
    )
    round_payload = svc._orchestrator_stage_payload(
        stage="round_synthesis",
        context_pack=context_pack,
        stage_input={
            "authoritative_outcome": {
                "from_stage": "deep_validation_review",
                "required_next_stage": "expression_design",
                "required_next_action": "start_next_round_at_expression_design",
            }
        },
    )

    import_context = import_payload["context_pack"]
    assert import_context["tool_evidence"]["gate_review_summary"]["decision"] == "adopted"
    assert import_context["tool_evidence"]["import_results"]["imported"] == 1
    assert import_context["code_advice"]["import_summary"]["imported_count"] == 1
    assert round_payload["context_pack"]["code_advice"]["next_round_suggestions"]


def test_candidate_plan_invalid_skip_evidence_fails_open_to_score():
    candidates = [
        {"candidate_id": "c1", "expression": "rank(amount)"},
        {"candidate_id": "c2", "expression": "rank(close)"},
        {"candidate_id": "c3", "expression": "rank(security_name)"},
    ]
    checks = [
        {"candidate_id": "c3", "fatal": True, "warnings": ["non_numeric_meta_fields:security_name"]},
    ]
    result = {
        "candidate_lanes": [
            {"candidate_id": "c1", "action": "skip_batch_duplicate", "keep": False, "reason": "similar"},
            {"candidate_id": "c2", "action": "skip_library_near_copy", "keep": False, "reason": "similar", "matched_cluster_id": "missing", "matched_factor_ids": ["f1"]},
            {"candidate_id": "c3", "action": "score", "keep": True, "reason": "score"},
        ]
    }
    svc._enforce_conservative_candidate_plan_lanes(
        result,
        {"candidates": candidates, "code_precheck": checks, "library_information_context": {"available": False}},
    )

    by_id = {item["candidate_id"]: item for item in result["candidate_lanes"]}
    assert by_id["c1"]["action"] == "score"
    assert by_id["c2"]["action"] == "score"
    assert by_id["c3"]["action"] == "precheck_blocked"
    selected = svc._candidate_plan_score_candidates(candidates, checks, result)
    assert [item["candidate_id"] for item in selected] == ["c1", "c2"]


def test_candidate_plan_evidenced_batch_skip_reduces_score_batch():
    candidates = [
        {"candidate_id": "c1", "expression": "rank(ts_delta(close,5))"},
        {"candidate_id": "c2", "expression": "rank(ts_delta(close,10))"},
    ]
    result = {"candidate_lanes": [
        {"candidate_id": "c1", "action": "score", "keep": True, "reason": "simpler representative"},
        {"candidate_id": "c2", "action": "skip_batch_duplicate", "keep": False, "reason": "same mechanism and only window differs", "matched_candidate_ids": ["c1"]},
    ]}
    svc._enforce_conservative_candidate_plan_lanes(result, {"candidates": candidates, "code_precheck": [], "library_information_context": {"available": False}})

    selected = svc._candidate_plan_score_candidates(candidates, [], result)
    projected = svc._candidate_plan_result_lanes(candidates, [], result)

    assert [item["candidate_id"] for item in selected] == ["c1"]
    assert {item["candidate_id"]: item["status_label"] for item in projected}["c2"] == "表达式预检拦截"
    assert next(item for item in projected if item["candidate_id"] == "c2").get("grade") is None


def test_candidate_plan_forces_non_parent_parameter_only_variant_out_of_score_batch():
    candidates = [
        {"candidate_id": "c1", "expression": "rank(ts_delta(close,5))"},
        {"candidate_id": "c2", "expression": "rank(ts_delta(close,10))"},
    ]
    checks = svc._candidate_plan_code_precheck(candidates)
    result = {"candidate_lanes": [
        {"candidate_id": "c1", "action": "score", "keep": True, "reason": "representative"},
        {"candidate_id": "c2", "action": "score", "keep": True, "reason": "try another window"},
    ]}

    svc._enforce_conservative_candidate_plan_lanes(
        result,
        {"candidates": candidates, "code_precheck": checks, "library_information_context": {"available": False}},
    )

    by_id = {item["candidate_id"]: item for item in result["candidate_lanes"]}
    assert by_id["c1"]["action"] == "score"
    assert by_id["c2"]["action"] == "skip_batch_duplicate"
    assert by_id["c2"]["matched_candidate_ids"] == ["c1"]
    assert [item["candidate_id"] for item in svc._candidate_plan_score_candidates(candidates, checks, result)] == ["c1"]


def test_candidate_plan_skip_fails_open_when_field_or_operator_channel_changes():
    candidates = [
        {"candidate_id": "c1", "expression": "tanh(3*rank(-ts_corr(close,amount,20))) * rank(-cost_85pct)"},
        {"candidate_id": "c2", "expression": "rank(-ts_corr(close,amount,20)) * rank(-cost_85pct)"},
        {"candidate_id": "c3", "expression": "rank(-ts_corr(close,amount,20)) * rank(-low)"},
    ]
    result = {"candidate_lanes": [
        {"candidate_id": "c1", "action": "score", "keep": True, "reason": "representative"},
        {"candidate_id": "c2", "action": "skip_batch_duplicate", "keep": False, "reason": "normalization differs", "matched_candidate_ids": ["c1"]},
        {"candidate_id": "c3", "action": "skip_batch_duplicate", "keep": False, "reason": "confirmation differs", "matched_candidate_ids": ["c2"]},
    ]}

    svc._enforce_conservative_candidate_plan_lanes(
        result,
        {"candidates": candidates, "code_precheck": [], "library_information_context": {"available": False}},
    )

    by_id = {item["candidate_id"]: item for item in result["candidate_lanes"]}
    assert by_id["c2"]["action"] == "score"
    assert by_id["c3"]["action"] == "score"


def test_candidate_plan_explicit_parent_mutation_is_forced_to_score():
    candidates = [{
        "candidate_id": "c1",
        "expression": "rank(ts_delta(close,6))",
        "parent_candidate_id": "r0004:c3",
        "mutation_summary": "preserve main leg and replace confirmation normalization",
    }]
    result = {"candidate_lanes": [{
        "candidate_id": "c1",
        "action": "skip_library_near_copy",
        "keep": False,
        "reason": "near existing representative",
        "matched_cluster_id": "information_001",
        "matched_factor_ids": ["f1"],
    }]}
    stage_input = {
        "candidates": candidates,
        "code_precheck": [],
        "protected_parent_mutation_candidate_ids": svc._protected_parent_mutation_candidate_ids(
            candidates,
            prior_round_expression_refs={
                svc._normalize_symbolic_expression("rank(ts_delta(close,5))"): {
                    "round_id": "run:r0004",
                    "candidate_id": "c3",
                }
            },
            allowed_parent_refs=["r0004:c3"],
        ),
        "library_information_context": {
            "available": True,
            "information_families": [{"cluster_id": "information_001", "members": [{"factor_id": "f1"}]}],
        },
    }

    svc._enforce_conservative_candidate_plan_lanes(result, stage_input)

    assert result["candidate_lanes"][0]["action"] == "score"
    assert result["candidate_lanes"][0]["keep"] is True
    assert [item["candidate_id"] for item in svc._candidate_plan_score_candidates(candidates, [], result)] == ["c1"]


def test_unchanged_parent_expression_is_not_protected_as_mutation():
    candidate = {
        "candidate_id": "c1",
        "expression": "rank(ts_delta(close,5))",
        "parent_candidate_id": "r0004:c3",
        "mutation_summary": "re-run unchanged parent to test random variation",
    }
    normalized = svc._normalize_symbolic_expression(candidate["expression"])

    protected = svc._protected_parent_mutation_candidate_ids(
        [candidate],
        prior_round_expression_refs={normalized: {"round_id": "run:r0004", "candidate_id": "c3"}},
        allowed_parent_refs=["r0004:c3"],
    )

    assert protected == []


def test_same_batch_self_declared_parent_is_not_protected():
    candidates = [
        {
            "candidate_id": "c1",
            "expression": "rank(-ps_ttm) * rank(ts_mean(net_mf_amount,10))",
        },
        {
            "candidate_id": "c2",
            "expression": "rank(-ps_ttm) * rank(ts_mean(net_mf_amount,20))",
            "parent_candidate_id": "c1",
            "mutation_summary": "只把窗口从10改成20",
        },
    ]

    protected = svc._protected_parent_mutation_candidate_ids(
        candidates,
        prior_round_expression_refs={},
        allowed_parent_refs=[],
    )

    assert protected == []


def test_review_stage_tool_evidence_omits_full_operator_contract():
    score = svc._compact_stage_tool_evidence_for_prompt(stage="score_review", stage_input={})
    novelty = svc._compact_stage_tool_evidence_for_prompt(stage="novelty_review", stage_input={})
    deep = svc._compact_stage_tool_evidence_for_prompt(stage="deep_validation_review", stage_input={})

    assert "operator_contract" not in score
    assert "operator_contract" not in novelty
    assert "operator_contract" not in deep


def test_round_synthesis_handoff_adopts_round_memory_for_next_round():
    advice = []
    synthesis = {
        "stage": "round_synthesis",
        "why": "fallback why",
        "next_action": "start_next_round",
        "stage_transition": {"next_stage": "expression_design", "reason": "mutate parent"},
        "round_memory": {
            "next_round_handoff": "preserve: r0004:c3; change: confirmation leg; avoid: window-only variants; suggested_start_stage: expression_design",
            "avoid_patterns": ["margin_buy_amount + low turnover only changing windows"],
            "promising_parents": ["r0004:c3 -> keep main leg, replace confirmation leg"],
        },
    }
    event = {
        "stage_transition": {"next_stage": "expression_design"},
        "evidence_refs": [{"tool": "round_synthesis"}],
    }

    handoff = svc._adopt_round_synthesis_handoff(advice, synthesis, event)

    assert handoff["from_stage"] == "round_synthesis"
    assert handoff["to_stage"] == "expression_design"
    assert handoff["binding_policy"] == "mechanism_and_evidence_only_not_literal_expression_instruction"
    assert handoff["reason"].startswith("preserve: r0004:c3")
    assert any("不得复制 parent 表达式" in item for item in handoff["must_change"])
    assert handoff["parent_candidate_refs"] == ["r0004:c3"]
    assert all("margin_buy_amount" not in value for value in handoff["must_avoid"])
    assert advice == [handoff]


def test_round_synthesis_replaces_prior_code_strategy_handoff_with_llm_judgment():
    advice = []
    deep_handoff = svc._return_handoff_from_stage(
        "deep_validation_review",
        {
            "summary": "c20深验接近门槛，只需修复滚动稳定性。",
            "judgment": "保留主机制并执行一次定向变异。",
            "why": "deep_score为79.6，rolling为最低分项。",
            "history_used": [],
            "candidate_decisions": [
                {
                    "candidate_id": "c20",
                    "action": "targeted_mutation",
                    "preserve": "保留ts_mean(lg_net_amount,40)、-ts_delta(turnover_rate,20)、乘法和rank。",
                    "change": "只调整一个窗口或平滑turnover_rate下降腿。",
                    "avoid": "不得更换字段、组合结构或加入其他hypothesis。",
                }
            ],
            "stage_transition": {
                "next_stage": "expression_design",
                "reason": "返回表达式设计修复rolling。",
            },
        },
        evidence_refs=[{"candidate_id": "c20", "action": "targeted_mutation"}],
        round_id="fr_test:r0008",
        code_advice={
            "evolution_strategy": {"strategy": "EXPLOIT", "action": "targeted_mutation"},
            "candidate_lane_decisions": [
                {
                    "candidate_id": "c20",
                    "action": "targeted_mutation",
                    "evolution_strategy": {"strategy": "EXPLOIT"},
                    "mutation_diagnosis": {"strategy": "mutate_window"},
                }
            ],
        },
    )
    advice.append(deep_handoff)

    adopted = svc._adopt_round_synthesis_handoff(
        advice,
        {
            "stage": "round_synthesis",
            "summary": "c20只差0.4分。",
            "judgment": "继续定向修复。",
            "why": "rolling仍是唯一短板。",
            "history_used": [],
            "stage_transition": {
                "next_stage": "expression_design",
                "reason": "继续表达式定向修复。",
            },
        },
        {"stage_transition": {"next_stage": "expression_design"}},
    )

    assert adopted["binding_policy"] == "mechanism_and_evidence_only_not_literal_expression_instruction"
    assert adopted["recommended_mutation"] == "target_stage_reassesses_mechanism_from_evidence"
    assert adopted["parent_candidate_refs"] == []
    assert adopted["reason"] == "继续表达式定向修复。"
    assert all("ts_mean(lg_net_amount,40)" not in item for item in adopted["must_preserve"])


def test_round_synthesis_can_keep_parent_even_when_prior_code_strategy_was_explore():
    advice = [
        {
            **svc._mechanism_level_handoff(
                from_stage="score_review",
                to_stage="thesis_design",
                parent_candidate_refs=[],
            ),
            "recommended_mutation": "EXPLORE:regenerate_full",
            "must_change": ["放弃当前弱机制族，重新选择经济主线。"],
        }
    ]

    adopted = svc._adopt_round_synthesis_handoff(
        advice,
        {
            "stage": "round_synthesis",
            "stage_transition": {
                "next_stage": "thesis_design",
                "reason": "当前候选无parent价值。",
            },
            "round_memory": {
                "suggested_start_stage": "thesis_design",
                "promising_parents": ["c2", "c3"],
            },
        },
        {
            "round_id": "run:r0004",
            "stage_transition": {"next_stage": "thesis_design"},
        },
    )

    assert adopted["recommended_mutation"] == "target_stage_reassesses_mechanism_from_evidence"
    assert adopted["parent_candidate_refs"] == ["r0004:c2", "r0004:c3"]
    assert adopted["binding_policy"] == "mechanism_and_evidence_only_not_literal_expression_instruction"


def test_expression_prompt_promotes_referenced_parent_before_budget_truncation():
    compact = svc._compact_current_round_context_for_prompt(
        {
            "parent_candidates": [
                {"candidate_id": "c17", "expression": "rank(close)"},
                {"candidate_id": "c18", "expression": "rank(open)"},
                {"candidate_id": "c19", "expression": "rank(high)"},
                {
                    "candidate_id": "c20",
                    "expression": "rank(ts_mean(lg_net_amount,40)) * rank(-ts_delta(turnover_rate,20))",
                },
            ]
        },
        stage="expression_design",
        preferred_parent_refs=["r0008:c20"],
    )

    drafts = compact["candidate_drafts"]
    assert drafts[0]["candidate_id"] == "c20"
    assert [item["candidate_id"] for item in drafts] == ["c20", "c17", "c18"]


def test_hypothesis_alignment_rejects_thesis_id_with_unrelated_fields():
    result = {
        "hypotheses": [
            {
                "hypothesis_id": "h2",
                "thesis_id": "t2",
                "candidate_variable_groups": [
                    {"variables": ["lg_net_amount", "turnover_rate"]}
                ],
            }
        ]
    }
    stage_input = {
        "selected_theses": [
            {
                "thesis_id": "t2",
                "preferred_data_families": ["net_asset_ps", "pct_change"],
            }
        ]
    }

    with pytest.raises(DeepSeekClientError, match="thesis_semantic_alignment_failed"):
        svc._validate_hypothesis_thesis_alignment(result, stage_input)


def test_hypothesis_alignment_reads_schema_fields_key():
    result = {
        "hypotheses": [
            {
                "hypothesis_id": "h2",
                "thesis_id": "t2",
                "candidate_variable_groups": [
                    {"fields": ["lg_net_amount", "turnover_rate"]}
                ],
            }
        ]
    }
    stage_input = {
        "selected_theses": [
            {
                "thesis_id": "t2",
                "preferred_data_families": ["net_asset_ps", "pct_change"],
            }
        ]
    }

    with pytest.raises(DeepSeekClientError, match="thesis_semantic_alignment_failed"):
        svc._validate_hypothesis_thesis_alignment(result, stage_input)


def test_hypothesis_alignment_requires_machine_usable_group_direction():
    result = {
        "hypotheses": [
            {
                "hypothesis_id": "h1",
                "thesis_id": "t1",
                "candidate_variable_groups": [
                    {"role": "main_signal", "fields": ["ps_ttm"]}
                ],
            }
        ]
    }
    stage_input = {
        "selected_theses": [
            {"thesis_id": "t1", "preferred_data_families": ["ps_ttm"]}
        ]
    }

    with pytest.raises(
        DeepSeekClientError,
        match="direction_must_be_positive_or_negative",
    ):
        svc._validate_hypothesis_thesis_alignment(result, stage_input)


def test_targeted_expression_rejects_unrelated_candidates():
    result = {
        "decision": "propose_candidates",
        "candidates": [
            {
                "candidate_id": "c21",
                "parent_candidate_id": "c20",
                "expression": "rank(ts_mean(lg_net_amount,30)) * rank(-ts_delta(turnover_rate,20))",
            },
            {
                "candidate_id": "c23",
                "parent_candidate_id": None,
                "expression": "rank(ts_delta(net_asset_ps,20)) * rank(-ts_std(pct_change,20))",
            },
        ],
    }

    with pytest.raises(DeepSeekClientError, match="requires_referenced_parent:c23"):
        svc._validate_targeted_expression_parent_contract(
            result,
            {"_private_targeted_parent_refs": ["r0008:c20"]},
        )
