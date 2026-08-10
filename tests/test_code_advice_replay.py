from __future__ import annotations

from scripts.replay_code_advice import replay_code_advice


RUN_ID = "fr_replay"


def _request(round_no, stage, tool_evidence, code_advice):
    round_id = f"{RUN_ID}:r{round_no:04d}"
    return {
        "trace_id": f"{round_id}:{stage}",
        "run_id": RUN_ID,
        "round_id": round_id,
        "stage": stage,
        "event_type": "llm_request",
        "ts": f"2026-07-24T00:{round_no:02d}:00",
        "payload": {
            "context_pack": {
                "tool_evidence": tool_evidence,
                "code_advice": code_advice,
            }
        },
    }


def _deep_request(
    round_no,
    candidate_id,
    deep_score,
    quick,
    anti,
    rolling,
    adversarial,
    *,
    old_action="targeted_mutation",
):
    raw = {
        "candidate_id": candidate_id,
        "factor_name": f"factor_{candidate_id}",
        "expression": f"rank(ts_mean(close,{round_no + 5})) * rank(ts_mean(volume,10))",
        "score": quick,
        "grade": "B",
        "backtest_summary": {
            "ic_mean": 0.04,
            "ic_ir": 0.6,
            "rank_ic_mean": 0.04,
            "rank_ic_ir": 0.6,
            "annual_return": 0.08,
            "sharpe": 0.5,
        },
        "novelty_score": 0.4,
        "anti_overfit_score": anti,
        "rolling_score": rolling,
        "adversarial_score": adversarial,
    }
    old_lane = {
        "candidate_id": candidate_id,
        "action": old_action,
        "deep_score": deep_score,
        "rolling_status": "ok",
        "rolling_grade": "C",
        "novelty_score": 0.4,
        "score_parts": {
            "component_scores": {
                "quick_core": quick,
                "anti_overfit": anti,
                "rolling": rolling,
                "adversarial": adversarial,
            }
        },
    }
    return _request(
        round_no,
        "deep_validation_review",
        {"deep_results": {"candidates": [raw], "evidence_refs": []}},
        {"candidate_lane_decisions": [old_lane]},
    )


def test_replay_restores_recorded_shapes_and_uses_cross_candidate_trajectory():
    score_keeper = {
        "candidate_id": "score_keeper",
        "expression": "rank(ts_mean(close,10)) * rank(ts_mean(volume,10))",
        "score": 75,
        "grade": "B",
        "rank_ic": 0.04,
        "backtest_summary": {"ic_mean": 0.04, "ic_ir": 0.8, "rank_ic_mean": 0.04},
    }
    score_reject = {
        "candidate_id": "score_reject",
        "expression": "rank(ts_mean(close,20)) * rank(ts_mean(volume,20))",
        "score": 45,
        "grade": "D",
        "rank_ic": 0.01,
        "backtest_summary": {"ic_mean": 0.01, "ic_ir": 0.4, "rank_ic_mean": 0.01},
    }
    rows = [
        _request(
            1,
            "score_review",
            {"candidate_lanes": [score_keeper, score_reject]},
            {
                "candidate_lane_decisions": [
                    {"candidate_id": "score_keeper", "action": "advance_to_novelty"},
                    {"candidate_id": "score_reject", "action": "mutate_interaction"},
                ]
            },
        ),
        _request(
            2,
            "novelty_review",
            {
                "novelty_results": {
                    "dropped": [
                        {
                            "candidate_id": "novelty_reject",
                            "expression": "rank(close)",
                            "novelty_guard": {
                                "allowed": False,
                                "reason": "low_information_gain",
                                "novelty_score": 0.0,
                                "matched_region_uid": "region_a",
                                "thresholds": {"pearson": 0.75, "rank_corr": 0.8},
                                "max_existing_pearson": 0.8,
                            },
                        }
                    ]
                }
            },
            {
                "candidate_lane_decisions": [
                    {
                        "candidate_id": "novelty_keeper",
                        "action": "advance_to_deep_validation",
                        "novelty_score": 0.4,
                    },
                    {
                        "candidate_id": "novelty_reject",
                        "action": "orthogonalize_or_switch_source",
                        "novelty_score": 0.0,
                    },
                ]
            },
        ),
        _deep_request(3, "deep_1", 78.0, 78.0, 90.0, 60.0, 80.0),
        _deep_request(4, "deep_2", 74.0, 74.0, 86.0, 52.0, 72.0),
        _deep_request(5, "deep_3", 69.0, 70.0, 80.0, 45.0, 65.0),
    ]

    report = replay_code_advice(rows, run_id=RUN_ID)

    assert report["request_count"] == 5
    assert report["keeper_contract_passed"] is True
    assert report["keeper_checks"]["score_checked"] == 1
    assert report["keeper_checks"]["novelty_checked"] == 1
    assert report["new_action_counts"].get("complete_deep_evidence", 0) == 0
    assert report["new_action_counts"]["recombine_from_best"] >= 1
    assert report["evolution_strategy_counts"]["recombine"] >= 1
    final_deep = [
        item
        for item in report["timeline"]
        if item["stage"] == "deep_validation_review"
    ][-1]
    assert final_deep["new_actions"] == ["recombine_from_best"]
    assert final_deep["trajectory_metrics"]["consecutive_declines"] == 2
    assert final_deep["recombination_candidate_ids"][0] == "deep_1"


def test_replay_defaults_to_latest_run():
    older = _request(
        1,
        "score_review",
        {"candidate_lanes": []},
        {"candidate_lane_decisions": []},
    )
    newer = {
        **older,
        "trace_id": "fr_new:r0001:score_review",
        "run_id": "fr_new",
        "round_id": "fr_new:r0001",
        "ts": "2026-07-24T01:00:00",
    }

    report = replay_code_advice([older, newer])

    assert report["run_id"] == "fr_new"
    assert report["request_count"] == 1


def test_replay_preserves_recorded_deep_gate_keeper():
    row = _deep_request(
        1,
        "deep_keeper",
        88.3,
        90.0,
        90.0,
        85.0,
        85.0,
        old_action="submit_quality_gate",
    )

    report = replay_code_advice([row], run_id=RUN_ID)

    assert report["keeper_checks"]["deep_checked"] == 1
    assert report["keeper_checks"]["deep_violations"] == []
    assert report["new_action_counts"]["submit_quality_gate"] == 1
    assert report["keeper_contract_passed"] is True
