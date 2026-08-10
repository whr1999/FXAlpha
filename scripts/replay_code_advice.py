#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domain.factor_research import orchestrator


REPLAY_STAGES = {"score_review", "novelty_review", "deep_validation_review"}


def _decision_map(advice: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("candidate_id") or ""): item
        for item in ((advice or {}).get("candidate_lane_decisions") or [])
        if isinstance(item, dict) and item.get("candidate_id")
    }


def _request_rows(rows: Iterable[dict[str, Any]], run_id: str | None) -> list[dict[str, Any]]:
    requests = [
        row
        for row in rows
        if row.get("event_type") == "llm_request"
        and row.get("stage") in REPLAY_STAGES
        and (not run_id or row.get("run_id") == run_id)
    ]
    requests.sort(key=lambda row: (str(row.get("ts") or ""), str(row.get("trace_id") or "")))
    return requests


def _latest_run_id(rows: Iterable[dict[str, Any]]) -> str:
    requests = [
        row
        for row in rows
        if row.get("event_type") == "llm_request" and row.get("stage") in REPLAY_STAGES
    ]
    if not requests:
        return ""
    requests.sort(key=lambda row: (str(row.get("ts") or ""), str(row.get("trace_id") or "")))
    return str(requests[-1].get("run_id") or "")


def _score_candidates(row: dict[str, Any]) -> list[dict[str, Any]]:
    context = ((row.get("payload") or {}).get("context_pack") or {})
    evidence = context.get("tool_evidence") or {}
    candidates = evidence.get("candidate_lanes") or evidence.get("score_factor_results") or []
    return [
        {
            **candidate,
            "round_id": candidate.get("round_id") or row.get("round_id"),
        }
        for candidate in candidates
        if isinstance(candidate, dict)
    ]


def _novelty_candidates(row: dict[str, Any], old_advice: dict[str, Any]) -> list[dict[str, Any]]:
    context = ((row.get("payload") or {}).get("context_pack") or {})
    evidence = context.get("tool_evidence") or {}
    novelty_results = evidence.get("novelty_results") or {}
    raw_candidates: list[dict[str, Any]] = []
    for key in ("keepers", "dropped", "candidates"):
        raw_candidates.extend(
            candidate
            for candidate in (novelty_results.get(key) or [])
            if isinstance(candidate, dict)
        )

    by_id = {
        str(candidate.get("candidate_id") or ""): {
            **candidate,
            "round_id": candidate.get("round_id") or row.get("round_id"),
        }
        for candidate in raw_candidates
        if candidate.get("candidate_id")
    }
    # Production traces store novelty vetoes in tool_evidence.dropped, while
    # passed candidates remain in the code-advice lane list. Recreate only the
    # missing pass evidence; do not infer a pass for a rejected raw candidate.
    for candidate_id, old_lane in _decision_map(old_advice).items():
        if candidate_id in by_id:
            continue
        old_action = str(old_lane.get("action") or "")
        by_id[candidate_id] = {
            "candidate_id": candidate_id,
            "round_id": row.get("round_id"),
            "expression": str(old_lane.get("expression") or ""),
            "novelty_guard": {
                "allowed": old_action == "advance_to_deep_validation",
                "novelty_score": old_lane.get("novelty_score"),
                "reason": "reconstructed_from_recorded_code_advice",
            },
        }
    return list(by_id.values())


def _deep_candidates(row: dict[str, Any], old_advice: dict[str, Any]) -> list[dict[str, Any]]:
    context = ((row.get("payload") or {}).get("context_pack") or {})
    evidence = context.get("tool_evidence") or {}
    deep_results = evidence.get("deep_results") or {}
    old_lanes = _decision_map(old_advice)
    candidates: list[dict[str, Any]] = []
    for raw in deep_results.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        candidate_id = str(raw.get("candidate_id") or "")
        old_lane = old_lanes.get(candidate_id, {})
        components = ((old_lane.get("score_parts") or {}).get("component_scores") or {})
        quick_score = components.get("quick_core", raw.get("score"))
        anti_score = components.get("anti_overfit", raw.get("anti_overfit_score"))
        rolling_score = components.get("rolling", raw.get("rolling_score"))
        adversarial_score = components.get("adversarial", raw.get("adversarial_score"))
        candidate = {
            **raw,
            "round_id": raw.get("round_id") or row.get("round_id"),
            "quick_score": quick_score,
            "anti_overfit": {"score": anti_score},
            "rolling_validation": {
                "status": old_lane.get("rolling_status") or "ok",
                "score": rolling_score,
                "grade": old_lane.get("rolling_grade"),
                "score_policy_version": old_lane.get("rolling_policy_version") or "historical_replay",
                # The prompt trace retains the official rolling score but
                # compacts away the window list. One recorded formal window is
                # sufficient to restore the score-extraction contract.
                "summary": {"n_periods": 1},
            },
            "adversarial_validation": {"score": adversarial_score},
            "novelty_guard": {
                "allowed": True,
                "novelty_score": old_lane.get("novelty_score", raw.get("novelty_score")),
                "reason": "deep_stage_implies_novelty_passed",
            },
        }
        candidates.append(candidate)
    return candidates


def _actions(advice: dict[str, Any] | None) -> list[str]:
    return [
        str(item.get("action") or "")
        for item in ((advice or {}).get("candidate_lane_decisions") or [])
        if isinstance(item, dict)
    ]


def _strategies(advice: dict[str, Any] | None, key: str) -> list[str]:
    values: list[str] = []
    for item in ((advice or {}).get("candidate_lane_decisions") or []):
        if not isinstance(item, dict):
            continue
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("strategy")
        if value:
            values.append(str(value))
    return values


def replay_code_advice(
    rows: Iterable[dict[str, Any]],
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    materialized = list(rows)
    selected_run = run_id or _latest_run_id(materialized)
    requests = _request_rows(materialized, selected_run)
    score_history: list[dict[str, Any]] = []
    novelty_history: list[dict[str, Any]] = []
    deep_history: list[dict[str, Any]] = []
    old_actions: Counter[str] = Counter()
    new_actions: Counter[str] = Counter()
    evolution_strategies: Counter[str] = Counter()
    mutation_strategies: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    changed_by_stage: Counter[str] = Counter()
    changed_transitions: Counter[str] = Counter()
    changed_decisions = 0
    keeper_checks = {
        "score_checked": 0,
        "score_violations": [],
        "novelty_checked": 0,
        "novelty_violations": [],
        "deep_checked": 0,
        "deep_violations": [],
    }
    timeline: list[dict[str, Any]] = []

    for row in requests:
        stage = str(row.get("stage") or "")
        round_id = str(row.get("round_id") or "")
        context = ((row.get("payload") or {}).get("context_pack") or {})
        old_advice = context.get("code_advice") or {}
        old_by_id = _decision_map(old_advice)

        if stage == "score_review":
            candidates = _score_candidates(row)
            new_advice = orchestrator.quick_advice(candidates, trajectory=score_history)
            for candidate in candidates:
                score_history.append(
                    {
                        "candidate_id": candidate.get("candidate_id"),
                        "round_id": round_id,
                        "expression": candidate.get("expression"),
                        "score": candidate.get("score", candidate.get("quick_score")),
                        "grade": candidate.get("grade"),
                    }
                )
            for lane in new_advice.get("candidate_lane_decisions") or []:
                candidate_id = str(lane.get("candidate_id") or "")
                old_action = str((old_by_id.get(candidate_id) or {}).get("action") or "")
                if old_action and old_action != lane.get("action"):
                    changed_decisions += 1
                    changed_by_stage[stage] += 1
                    changed_transitions[f"{stage}:{old_action}->{lane.get('action')}"] += 1
                if str(lane.get("grade") or "").upper() in {"A", "B"}:
                    raw = next(
                        (candidate for candidate in candidates if str(candidate.get("candidate_id") or "") == candidate_id),
                        {},
                    )
                    rank_ic = raw.get("rank_ic")
                    if rank_ic is None:
                        rank_ic = (raw.get("backtest_summary") or {}).get("rank_ic_mean")
                    if rank_ic is not None and float(rank_ic) >= 0:
                        keeper_checks["score_checked"] += 1
                        if lane.get("action") != "advance_to_novelty":
                            keeper_checks["score_violations"].append(
                                {"round_id": round_id, "candidate_id": candidate_id, "action": lane.get("action")}
                            )
        elif stage == "novelty_review":
            candidates = _novelty_candidates(row, old_advice)
            new_advice = orchestrator.novelty_advice(candidates, history=novelty_history)
            novelty_history.extend(new_advice.get("candidate_lane_decisions") or [])
            for lane in new_advice.get("candidate_lane_decisions") or []:
                candidate_id = str(lane.get("candidate_id") or "")
                old_action = str((old_by_id.get(candidate_id) or {}).get("action") or "")
                if old_action and old_action != lane.get("action"):
                    changed_decisions += 1
                    changed_by_stage[stage] += 1
                    changed_transitions[f"{stage}:{old_action}->{lane.get('action')}"] += 1
                raw = next(
                    (candidate for candidate in candidates if str(candidate.get("candidate_id") or "") == candidate_id),
                    {},
                )
                if (raw.get("novelty_guard") or {}).get("allowed") is True:
                    keeper_checks["novelty_checked"] += 1
                    if lane.get("action") != "advance_to_deep_validation":
                        keeper_checks["novelty_violations"].append(
                            {"round_id": round_id, "candidate_id": candidate_id, "action": lane.get("action")}
                        )
        else:
            candidates = _deep_candidates(row, old_advice)
            new_advice = orchestrator.deep_advice(candidates, trajectory=deep_history)
            for lane in new_advice.get("candidate_lane_decisions") or []:
                deep_history.append(
                    {
                        "candidate_id": lane.get("candidate_id"),
                        "trajectory_id": lane.get("trajectory_id"),
                        "round_id": round_id,
                        "parent_candidate_id": lane.get("parent_candidate_id"),
                        "mutation_summary": lane.get("mutation_summary"),
                        "matched_region_uid": lane.get("matched_region_uid"),
                        "expression": lane.get("expression"),
                        "score": lane.get("deep_score"),
                        "rolling_score": lane.get("rolling_score"),
                        "downstream_action": lane.get("action"),
                        "grade": (lane.get("score_parts") or {}).get("official_grade"),
                    }
                )
                candidate_id = str(lane.get("candidate_id") or "")
                old_lane = old_by_id.get(candidate_id) or {}
                old_action = str(old_lane.get("action") or "")
                if old_action and old_action != lane.get("action"):
                    changed_decisions += 1
                    changed_by_stage[stage] += 1
                    changed_transitions[f"{stage}:{old_action}->{lane.get('action')}"] += 1
                if old_action == "submit_quality_gate":
                    keeper_checks["deep_checked"] += 1
                    if lane.get("action") != "submit_quality_gate":
                        keeper_checks["deep_violations"].append(
                            {"round_id": round_id, "candidate_id": candidate_id, "action": lane.get("action")}
                        )

        stage_counts[stage] += 1
        old_stage_actions = _actions(old_advice)
        new_stage_actions = _actions(new_advice)
        old_actions.update(old_stage_actions)
        new_actions.update(new_stage_actions)
        evolution_strategies.update(_strategies(new_advice, "evolution_strategy"))
        mutation_strategies.update(_strategies(new_advice, "mutation"))
        timeline.append(
            {
                "round_id": round_id,
                "stage": stage,
                "old_actions": old_stage_actions,
                "new_actions": new_stage_actions,
                "evolution_strategies": _strategies(new_advice, "evolution_strategy"),
                "mutation_strategies": _strategies(new_advice, "mutation"),
                "trajectory_metrics": new_advice.get("trajectory_metrics"),
                "recombination_candidate_ids": [
                    item.get("candidate_id")
                    for item in (new_advice.get("recombination_candidates") or [])
                ],
            }
        )

    all_violations = (
        keeper_checks["score_violations"]
        + keeper_checks["novelty_violations"]
        + keeper_checks["deep_violations"]
    )
    return {
        "run_id": selected_run,
        "request_count": len(requests),
        "stage_request_counts": dict(stage_counts),
        "candidate_decision_count": sum(new_actions.values()),
        "changed_decision_count": changed_decisions,
        "changed_decision_counts_by_stage": dict(changed_by_stage),
        "changed_action_transitions": dict(changed_transitions),
        "old_action_counts": dict(old_actions),
        "new_action_counts": dict(new_actions),
        "evolution_strategy_counts": dict(evolution_strategies),
        "mutation_strategy_counts": dict(mutation_strategies),
        "keeper_checks": keeper_checks,
        "keeper_contract_passed": not all_violations,
        "timeline": timeline,
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay code advice against recorded orchestrator LLM requests.")
    parser.add_argument(
        "--trace-file",
        default="runtime/factor_research/orchestrator_llm_traces/current.jsonl",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--timeline", action="store_true", help="Include per-round decisions in output.")
    args = parser.parse_args()

    report = replay_code_advice(_load_jsonl(Path(args.trace_file)), run_id=args.run_id)
    if not args.timeline:
        report.pop("timeline", None)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["keeper_contract_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
