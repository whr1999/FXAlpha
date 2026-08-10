from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_ACTIVE_FACTOR_CONTEXT_LIMIT = 24
_FACTOR_OPERATOR_TOKENS = {
    "abs",
    "and",
    "boll_lower",
    "boll_mid",
    "boll_upper",
    "clip",
    "decay_linear",
    "ema",
    "exp",
    "group_rank",
    "group_zscore",
    "indneutralize",
    "log",
    "macd",
    "max",
    "min",
    "or",
    "power",
    "product",
    "rank",
    "rsi",
    "scale",
    "sign",
    "sign_power",
    "sigmoid",
    "sma",
    "sqrt",
    "tanh",
    "trade_when",
    "ts_argmax",
    "ts_argmin",
    "ts_av_diff",
    "ts_corr",
    "ts_cov",
    "ts_delta",
    "ts_max",
    "ts_mean",
    "ts_min",
    "ts_rank",
    "ts_shift",
    "ts_std",
    "ts_sum",
    "ts_zscore",
    "where",
    "wma",
    "zscore",
}


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _expression_tokens(expression: Any) -> list[str]:
    tokens = []
    for token in re.findall(r"\b[a-zA-Z_][A-Za-z0-9_]*\b", str(expression or "").lower()):
        if token in _FACTOR_OPERATOR_TOKENS or token.startswith("adv"):
            continue
        tokens.append(token)
    return sorted(set(tokens))[:12]


def _expression_family_signature(expression: Any) -> tuple[str, list[str], list[str], list[int]]:
    text = str(expression or "").lower()
    fields = _expression_tokens(text)
    operators = sorted(
        set(
            token
            for token in re.findall(r"\b[a-z_][a-z0-9_]*\b", text)
            if token in _FACTOR_OPERATOR_TOKENS or token.startswith("ts_") or token.startswith("group_")
        )
    )[:10]
    windows = sorted({int(match) for match in re.findall(r"\bts_[a-z_]+\s*\([^)]*,\s*(\d+)", text)})[:8]
    family_key = "|".join(
        [
            ",".join(fields[:8]) or "no_fields",
            ",".join(operators[:8]) or "no_ops",
        ]
    )
    return family_key, fields, operators, windows


def _expression_view(expression: Any, *, limit: int = 320) -> dict[str, Any]:
    """Return an honest prompt view without implying a clipped formula is complete."""

    text = re.sub(r"\s+", " ", str(expression or "")).strip()
    if not text:
        return {}
    if len(text) <= limit:
        return {"expression": text, "expression_complete": True}
    return {
        "expression_preview": text[: max(0, limit - 1)].rstrip() + "…",
        "expression_complete": False,
    }


def _semantic_signature(item: dict[str, Any]) -> tuple[str, list[str], list[str], list[int]]:
    expression = str(item.get("expression") or "")
    _, parsed_fields, parsed_operators, parsed_windows = _expression_family_signature(expression)
    fields = sorted(
        {
            str(value).lower()
            for value in (item.get("fields_used") or item.get("field_tokens") or parsed_fields)
            if str(value).strip()
        }
    )[:12]
    operators = sorted(
        {
            str(value).lower()
            for value in (item.get("operators_used") or item.get("operator_tokens") or parsed_operators)
            if str(value).strip()
        }
    )[:10]
    windows = sorted(
        {
            int(value)
            for value in (item.get("window_lengths") or item.get("window_tokens") or parsed_windows)
            if str(value).isdigit()
        }
    )[:12]
    family_key = "|".join(
        [
            ",".join(fields[:8]) or "no_fields",
            ",".join(operators[:8]) or "no_ops",
        ]
    )
    return family_key, fields, operators, windows


def _compact_step(step: dict[str, Any]) -> dict[str, Any]:
    transition = step.get("stage_transition") if isinstance(step.get("stage_transition"), dict) else {}
    compact_transition = {
        "next_stage": transition.get("next_stage"),
        "next_action": _clip(transition.get("next_action"), 180),
        "judgment": _clip(transition.get("judgment"), 180),
        "why": _clip(transition.get("why"), 180),
        "history_used": transition.get("history_used", [])[:5] if isinstance(transition.get("history_used"), list) else _clip(transition.get("history_used"), 180),
        "facts": _clip(transition.get("facts"), 220),
    }
    return {
        "run_id": step.get("run_id"),
        "round_id": step.get("round_id"),
        "ts": step.get("ts"),
        "stage": step.get("stage"),
        "summary": _clip(step.get("summary"), 260),
        "decision": _clip(step.get("decision"), 180),
        "next_stage": compact_transition.get("next_stage"),
        "next_action": compact_transition.get("next_action"),
        "judgment": compact_transition.get("judgment"),
        "why": compact_transition.get("why"),
        "history_used": compact_transition.get("history_used"),
        "stage_transition": compact_transition,
        "tags": (step.get("tags") or [])[:6],
    }


def _compact_active_factor_summary(summary: dict[str, Any]) -> dict[str, Any]:
    factors: list[dict[str, Any]] = []
    crowding_entries: list[dict[str, Any]] = []
    token_counts: dict[str, int] = {}
    active_items = [item for item in (summary.get("active_factors") or []) if isinstance(item, dict)]
    try:
        active_factor_count = int(summary.get("active_factor_count") or len(active_items))
    except Exception:
        active_factor_count = len(active_items)
    family_groups: dict[str, dict[str, Any]] = {}
    for item in active_items:
        expression = str(item.get("expression") or "").strip()
        if not expression:
            continue
        family_key, fields, operators, windows = _semantic_signature(item)
        group = family_groups.setdefault(
            family_key,
            {
                "count": 0,
                "family_summary": (
                    f"fields: {', '.join(fields) or 'none'}; "
                    f"operators: {', '.join(operators) or 'none'}"
                ),
                "fields_used": fields,
                "operators_used": operators,
                "window_lengths": windows,
                "representatives": [],
            },
        )
        group["count"] += 1
        group["window_lengths"] = sorted(set(group.get("window_lengths") or []) | set(windows))[:12]
        if len(group["representatives"]) < 2:
            representative = {
                "name": _clip(item.get("name"), 80),
                "hypothesis": _clip(item.get("hypothesis"), 120),
                **_expression_view(expression, limit=240),
            }
            group["representatives"].append(representative)

    # Select one factor per family before taking a second member from any
    # family.  This preserves active-library breadth instead of spending the
    # whole prompt budget on window variants of the first few families.
    family_members: dict[str, list[dict[str, Any]]] = {}
    for item in active_items:
        expression = str(item.get("expression") or "").strip()
        if not expression:
            continue
        family_key, fields, operators, windows = _semantic_signature(item)
        family_members.setdefault(family_key, []).append(
            {
                "name": _clip(item.get("name"), 80),
                "hypothesis": _clip(item.get("hypothesis"), 140),
                "fields_used": fields,
                "operators_used": operators,
                "window_lengths": windows,
                **_expression_view(expression),
            }
        )
    ordered_families = sorted(
        family_members,
        key=lambda key: (-len(family_members[key]), key),
    )
    depth = 0
    while len(factors) < _ACTIVE_FACTOR_CONTEXT_LIMIT:
        added = False
        for family_key in ordered_families:
            members = family_members[family_key]
            if depth >= len(members):
                continue
            factor = members[depth]
            factors.append(factor)
            for field in factor.get("fields_used") or []:
                token_counts[field] = token_counts.get(field, 0) + 1
            crowding_entries.append(factor)
            added = True
            if len(factors) >= _ACTIVE_FACTOR_CONTEXT_LIMIT:
                break
        if not added:
            break
        depth += 1
    family_representatives = sorted(
        family_groups.values(),
        key=lambda item: (-int(item.get("count") or 0), str(item.get("family_summary") or "")),
    )[:12]
    return {
        "registry_summary": summary.get("registry_summary", {}),
        "active_factor_count": summary.get("active_factor_count"),
        "included_active_factor_count": len(factors),
        "active_factors_complete": len(factors) == active_factor_count,
        "coverage_note": (
            "active_factors is complete"
            if len(factors) == active_factor_count
            else "active_factors is a compact sample; use family_representatives/crowding_map for active-library crowding context"
        ),
        "active_factors": factors,
        "family_representatives": family_representatives,
        "crowding_map": {
            "expression_count": len(active_items),
            "included_expression_count": len(crowding_entries),
            "truncated": len(active_items) > _ACTIVE_FACTOR_CONTEXT_LIMIT,
            "field_usage_counts": dict(sorted(token_counts.items(), key=lambda item: (-item[1], item[0]))[:24]),
            "expressions": crowding_entries,
            "family_representatives": family_representatives,
        },
    }


def _compact_latest_stage_transition(raw: dict[str, Any]) -> dict[str, Any]:
    latest = raw if isinstance(raw, dict) else {}
    transition = latest.get("stage_transition") if isinstance(latest.get("stage_transition"), dict) else latest
    compact_transition = {
        "next_stage": transition.get("next_stage"),
        "next_action": _clip(transition.get("next_action"), 220),
        "judgment": _clip(transition.get("judgment"), 180),
        "why": _clip(transition.get("why"), 180),
    }
    return {
        "latest_stage": latest.get("latest_stage") or latest.get("stage"),
        "latest_step_ts": latest.get("latest_step_ts") or latest.get("ts") or latest.get("created_at"),
        "next_stage": compact_transition.get("next_stage"),
        "next_action": compact_transition.get("next_action"),
        "stage_transition": compact_transition,
    }


def _compact_field_context(field_context: dict[str, Any]) -> dict[str, Any]:
    blocked = field_context.get("blocked_fields") or {}
    return {
        "supported_fields": field_context.get("supported_fields", []),
        "supported_field_count": field_context.get("supported_field_count"),
        "aliases": field_context.get("aliases", {}),
        "coverage_summary": field_context.get("coverage_summary", {}),
        "blocked_fields": list(blocked.keys())[:24],
        "neutralization_status": field_context.get("neutralization_status", {}),
    }


def _compact_quantgpt_summary(summary: dict[str, Any]) -> dict[str, Any]:
    latest = summary.get("latest_task") or {}
    result = latest.get("result") or {}
    return {
        "total": summary.get("total"),
        "by_type": summary.get("by_type", {}),
        "running_count": summary.get("running_count"),
        "latest_task": {
            "task_id": latest.get("task_id"),
            "status": latest.get("status"),
            "task_type": latest.get("task_type"),
            "expression": _clip(latest.get("expression"), 220),
            "score": result.get("score"),
            "grade": result.get("grade"),
            "reject_reasons": result.get("reject_reasons", [])[:5],
            "key_metrics": result.get("key_metrics", {}),
        },
    }


def _compact_event_advice(advice: Any) -> dict[str, Any]:
    if not isinstance(advice, dict):
        return {}
    lanes = []
    for item in (advice.get("candidate_lane_decisions") or [])[:3]:
        if not isinstance(item, dict):
            continue
        lanes.append(
            {
                "candidate_id": item.get("candidate_id") or item.get("idx"),
                "action": item.get("action"),
                "reason": _clip(item.get("reason"), 120),
                "evolution_strategy": item.get("evolution_strategy"),
                "mutation": item.get("mutation"),
                "score": item.get("score"),
                "grade": item.get("grade"),
                "quick_score": item.get("quick_score"),
                "deep_score": item.get("deep_score"),
                "deep_action": item.get("deep_action"),
                "deep_reason": _clip(item.get("deep_reason"), 120),
                "anti_overfit_score": item.get("anti_overfit_score"),
                "adversarial_score": item.get("adversarial_score"),
                "novelty_score": item.get("novelty_score"),
                "ic": item.get("ic"),
                "icir": item.get("icir"),
                "weakest_component": item.get("weakest_component"),
                "expression": _clip(item.get("expression"), 160),
            }
        )
    return {
        "action": advice.get("action"),
        "strategy": advice.get("strategy"),
        "evolution_strategy": advice.get("evolution_strategy"),
        "trajectory_metrics": advice.get("trajectory_metrics"),
        "recombination_candidates": (advice.get("recombination_candidates") or [])[:5],
        "candidate_lane_decisions": lanes,
    }


@dataclass(frozen=True)
class OrchestratorContextPack:
    run_id: str
    round_id: str
    stage: str
    contract: dict[str, Any]
    active_context: dict[str, Any]
    recent_steps: list[dict[str, Any]]
    quantgpt_summary: dict[str, Any]
    round_events: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        active = self.active_context or {}
        return {
            "protocol": {
                "mode": "orchestrator",
                "contract_source": "domain/factor_research/ORCHESTRATOR_README.md",
                "rules": [
                    "Return strict JSON only.",
                    "DeepSeek proposes candidates and diagnoses; Orchestrator code controls tools, gate, import, and state.",
                    "Do not invent unsupported fields; use active_context.field_context.supported_fields.",
                    "Do not add temporal_shuffle as a hard gate.",
                    "Use selection window for evidence and value window only for import output.",
                    "Orchestrator is an explicit background mode and does not require Codex foreground MCP supervision.",
                ],
                "gate_standard": {
                    "deep_score_min": 80,
                    "min_abs_ic": 0.02,
                    "min_abs_icir": 0.3,
                    "novelty_required": True,
                    "autocorrelation": "diagnostic_only_not_a_hard_veto",
                },
                "evidence_chain": {
                    "required": [
                        "score_factor",
                        "fxalpha_novelty_check",
                        "run_backtest",
                        "run_anti_overfit",
                        "run_rolling_validation",
                        "run_adversarial_validation",
                        "fxalpha_quality_gate",
                        "fxalpha_import_factors",
                    ],
                    "diagnostic_only": [],
                },
            },
            "run_state": {
                "run_id": self.run_id,
                "round_id": self.round_id,
                "stage": self.stage,
                "contract": self.contract,
            },
            "active_context": {
                "config": active.get("config", {}),
                "active_factor_summary": _compact_active_factor_summary(active.get("active_factor_summary", {})),
                "field_context": _compact_field_context(active.get("field_context", {})),
                # These are already run-pinned/compacted by the service.  Do
                # not silently drop them while constructing the prompt pack.
                "factor_map_context": active.get("factor_map_context", {}),
                "operator_guidance": active.get("operator_guidance", {}),
                "orchestrator_contract": active.get("orchestrator_contract", {}),
                "latest_stage_transition": _compact_latest_stage_transition(active.get("latest_stage_transition", {})),
                "recent_failure_digest": active.get("recent_failure_digest", []),
                "recent_orchestrator_anchors": active.get("recent_orchestrator_anchors", [])[:5],
                "recent_orchestrator_failure_feedback": active.get("recent_orchestrator_failure_feedback", {}),
            },
            "recent_steps": [_compact_step(step) for step in (self.recent_steps or [])[:10]],
            "quantgpt_summary": _compact_quantgpt_summary(self.quantgpt_summary or {}),
            "round_events": [
                {
                    "stage": event.get("stage"),
                    "summary": _clip(event.get("summary"), 360),
                    "decision": _clip(event.get("decision"), 240),
                    "event_type": event.get("event_type"),
                    "checkpoint": event.get("checkpoint"),
                    "advice": _compact_event_advice(event.get("advice")),
                }
                for event in (self.round_events or [])[-12:]
            ],
        }
