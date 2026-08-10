from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from domain.factor_research.region_semantics import build_region_profile, semantic_signature
from services._base import ok_result
from services.factor_library_audit_service import factor_library_audit_status
from storage.paths import QUANTGPT_RESEARCH_NOTES_DIR, RUNTIME_ROOT


FACTOR_MAP_SCHEMA_VERSION = "factor_map_v3"
LEGACY_EXPERIENCE_MARKER = Path(QUANTGPT_RESEARCH_NOTES_DIR) / "experience" / "ARCHIVED.json"
FACTOR_MAP_RUNTIME_DIR = Path(RUNTIME_ROOT) / "factor_map"
LEGACY_EXPERIENCE_MIGRATION_FILE = FACTOR_MAP_RUNTIME_DIR / "legacy_experience_migration_v1.json"
RESEARCH_STEPS_DIR = Path(RUNTIME_ROOT) / "factor_research" / "research_steps"
RESEARCH_STEPS_FILE = RESEARCH_STEPS_DIR / "current.jsonl"
RESEARCH_STEPS_HISTORY_DIR = RESEARCH_STEPS_DIR / "history"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _legacy_experience_state() -> dict[str, Any]:
    payload = _read_json(LEGACY_EXPERIENCE_MARKER)
    if not payload:
        return {"status": "not_archived", "marker": str(LEGACY_EXPERIENCE_MARKER)}
    migration = _read_json(LEGACY_EXPERIENCE_MIGRATION_FILE)
    return {
        "status": "archived",
        "marker": str(LEGACY_EXPERIENCE_MARKER),
        "archive_path": payload.get("archive_path"),
        "archived_at": payload.get("archived_at"),
        "file_count": payload.get("file_count"),
        "byte_count": payload.get("byte_count"),
        "new_writes_disabled": bool(payload.get("new_writes_disabled")),
        "recoverable": bool(payload.get("recoverable")),
        "migration": {
            "status": migration.get("status") or "not_generated",
            "migration_id": migration.get("migration_id"),
            "generated_at": migration.get("generated_at"),
            "source_integrity_verified": bool(migration.get("source_integrity_verified")),
            "effective_annotation_count": len(migration.get("governed_annotations") or []),
            "unmapped_annotation_count": sum(
                str(item.get("mapping_status") or "") != "mapped"
                for item in (migration.get("governed_annotations") or [])
                if isinstance(item, dict)
            ),
            "artifact": str(LEGACY_EXPERIENCE_MIGRATION_FILE),
        },
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _region_relations(relation_graph: dict[str, Any]) -> list[dict[str, Any]]:
    strongest: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in relation_graph.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        source_region = str(edge.get("source_region_uid") or "").strip()
        target_region = str(edge.get("target_region_uid") or "").strip()
        if (
            not source_region
            or not target_region
            or source_region == target_region
            or "unclustered" in {source_region, target_region}
        ):
            continue
        key = tuple(sorted((source_region, target_region)))
        current = strongest.get(key)
        score = float(edge.get("dependency_score") or 0.0)
        if current is None or score > float(current.get("dependency_score") or 0.0):
            strongest[key] = {
                "source_region_uid": key[0],
                "target_region_uid": key[1],
                "dependency_score": edge.get("dependency_score"),
                "p90_abs_rank_corr": edge.get("p90_abs_rank_corr"),
                "p90_abs_pearson": edge.get("p90_abs_pearson"),
                "evidence_factor_ids": [edge.get("source"), edge.get("target")],
                "relation_type": "strongest_measured_cross_region_link",
            }
    return sorted(
        strongest.values(),
        key=lambda item: float(item.get("dependency_score") or 0.0),
        reverse=True,
    )


def _trajectory_id(run_id: str, round_id: str, candidate_id: str, expression: str) -> str:
    material = "|".join(
        str(value or "").strip()
        for value in (run_id, round_id, candidate_id, expression)
    )
    return f"ft_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def _research_step_paths(limit_files: int = 5) -> list[Path]:
    paths = [RESEARCH_STEPS_FILE] if RESEARCH_STEPS_FILE.is_file() else []
    if RESEARCH_STEPS_HISTORY_DIR.is_dir():
        history = sorted(
            RESEARCH_STEPS_HISTORY_DIR.glob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        paths.extend(history[: max(0, int(limit_files))])
    return paths


def _candidate_items(step: dict[str, Any]) -> list[dict[str, Any]]:
    lane_items: list[dict[str, Any]] = []
    advice_items: list[dict[str, Any]] = []
    for ref in step.get("evidence_refs") or []:
        if not isinstance(ref, dict):
            continue
        if str(ref.get("type") or "") == "candidate_lanes":
            lane_items.extend(item for item in (ref.get("items") or []) if isinstance(item, dict))
        if str(ref.get("type") or "") == "advice_summary":
            advice_items.extend(
                item for item in (ref.get("candidate_lane_decisions") or [])
                if isinstance(item, dict)
            )
    return advice_items or lane_items


def _stage_outcome(stage: str, item: dict[str, Any]) -> str:
    action = str(item.get("action") or item.get("status") or "").strip()
    if stage == "novelty_review":
        if action == "advance_to_deep_validation":
            return "novelty_passed"
        if action in {"orthogonalize_or_switch_source", "reject_st_exposure", "keep_best_drop_variants"}:
            return "novelty_blocked"
    if stage == "deep_validation_review":
        if action in {"submit_quality_gate", "advance_to_import_gate"}:
            return "deep_passed"
        if action:
            return "deep_not_passed"
    if stage == "import_gate_review":
        return "gate_passed" if action == "import" else "gate_not_passed"
    if stage == "import_review":
        adopted = item.get("adopted")
        if adopted is True or action in {"import", "import_success", "adopted"}:
            return "imported"
        if action or adopted is False:
            return "import_not_completed"
    return action or "observed"


def _deep_failure_category(reason: str) -> str:
    text = str(reason or "").lower()
    categories = (
        ("rolling_stability", ("rolling", "滚动", "stability", "稳定")),
        ("cost_after_return", ("cost", "成本")),
        ("turnover", ("turnover", "换手")),
        ("group_monotonicity", ("monotonic", "单调", "group")),
        ("adversarial", ("adversarial", "对抗")),
        ("overfit", ("overfit", "过拟合")),
        ("st_exposure", ("st_exposure", "st暴露", "st exposure")),
        ("missing_evidence", ("missing", "evidence", "证据不完整")),
    )
    for category, markers in categories:
        if any(marker in text for marker in markers):
            return category
    return "other"


def _region_guidance(activity: dict[str, Any]) -> dict[str, Any]:
    novelty_checked = int(activity.get("novelty_checked") or 0)
    novelty_rejected = int(activity.get("novelty_rejected") or 0)
    deep_checked = int(activity.get("deep_checked") or 0)
    deep_rejected = int(activity.get("deep_rejected") or 0)
    imported = int(activity.get("imported_near_region") or 0)
    novelty_round_count = int(activity.get("novelty_rejected_round_count") or 0)
    deep_round_count = int(activity.get("deep_rejected_round_count") or 0)
    repeated_signature_count = int(activity.get("max_rejected_semantic_signature_count") or 0)
    common_deep_failure_count = int(activity.get("max_deep_failure_category_count") or 0)
    novelty_rate = novelty_rejected / novelty_checked if novelty_checked else 0.0
    deep_rate = deep_rejected / deep_checked if deep_checked else 0.0

    novelty_action = (
        novelty_checked >= 3
        and novelty_rejected >= 3
        and novelty_rate >= 0.75
        and novelty_round_count >= 2
        and repeated_signature_count >= 2
    )
    deep_action = (
        deep_checked >= 3
        and deep_rejected >= 3
        and deep_rate >= 2 / 3
        and deep_round_count >= 2
        and common_deep_failure_count >= 3
    )
    novelty_watch = (
        novelty_checked >= 2
        and novelty_rejected >= 2
        and novelty_rate >= 0.75
        and novelty_round_count >= 2
    )
    deep_watch = (
        deep_checked >= 2
        and deep_rejected >= 2
        and deep_round_count >= 2
        and common_deep_failure_count >= 2
    )

    if novelty_action and imported:
        return {
            "level": "action",
            "action": "expanded_and_crowded",
            "instruction": (
                "该区域附近本轮已经产生新入库因子，同时相同语义结构的新颖性拒绝持续偏高。"
                "停止同类参数变体；如继续研究，必须更换主要信息来源或组合机制。"
            ),
            "advisory_only": True,
        }
    if novelty_action:
        return {
            "level": "action",
            "action": "avoid_near_copy",
            "instruction": (
                "该区域相同字段、方向和核心结构的新颖性拒绝持续偏高。"
                "停止同类参数变体；如继续研究，必须更换主要信息来源、交互方式或确认机制。"
            ),
            "advisory_only": True,
        }
    if deep_action:
        failure = str(activity.get("dominant_deep_failure_category") or "深度验证")
        failure_label = {
            "rolling_stability": "滚动稳定性",
            "cost_after_return": "成本后收益",
            "turnover": "换手负担",
            "group_monotonicity": "分组单调性",
            "adversarial": "对抗检验",
            "overfit": "过拟合风险",
            "st_exposure": "ST 暴露",
            "missing_evidence": "证据完整性",
            "other": "深度验证的同一类弱项",
        }.get(failure, failure)
        return {
            "level": "action",
            "action": "change_validation_mechanism",
            "instruction": (
                f"该区域的新颖候选反复卡在{failure_label}。"
                "保留机制前必须改变相关确认关系或风险约束，不能只调整窗口、常数或外层包装。"
            ),
            "advisory_only": True,
        }
    if novelty_watch:
        return {
            "level": "observe",
            "action": "watch_novelty_crowding",
            "instruction": "该区域本轮新颖性拒绝偏多，避免增加相同结构的窗口或常数变体。",
            "advisory_only": True,
        }
    if deep_watch:
        return {
            "level": "observe",
            "action": "watch_deep_fragility",
            "instruction": "该区域已有多个新颖候选未通过深度验证，后续应优先回应具体失败组件。",
            "advisory_only": True,
        }
    if imported:
        return {
            "level": "observe",
            "action": "recent_expansion",
            "instruction": "该区域附近本轮已经产生新入库因子，后续应重点避免与新增因子形成近似结构。",
            "advisory_only": True,
        }
    return {
        "level": "insufficient_evidence",
        "action": "none",
        "instruction": "",
        "advisory_only": True,
    }


def _research_activity(
    *,
    regions: list[dict[str, Any]],
    run_id: str = "",
    max_steps: int = 1600,
    max_observations: int = 240,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    factor_to_region: dict[str, str] = {}
    for region in regions:
        region_uid = str(region.get("region_uid") or "").strip()
        if not region_uid:
            continue
        members = list(region.get("members") or [])
        if isinstance(region.get("representative"), dict):
            members.append(region["representative"])
        for member in members:
            if not isinstance(member, dict):
                continue
            factor_id = str(member.get("factor_id") or "").strip()
            if factor_id:
                factor_to_region[factor_id] = region_uid

    stage_names = {
        "novelty_review",
        "deep_validation_review",
        "import_gate_review",
        "import_review",
    }
    steps: list[dict[str, Any]] = []
    for path in _research_step_paths():
        steps.extend(_read_jsonl(path))
    steps = sorted(
        [
            step for step in steps
            if str(step.get("stage") or "") in stage_names
            and ":req_" not in str(step.get("stage_id") or "")
            and (not run_id or str(step.get("run_id") or "") == str(run_id))
        ],
        key=lambda step: str(step.get("ts") or ""),
    )[-max(1, int(max_steps)) :]

    observations_by_event: dict[str, dict[str, Any]] = {}
    region_by_trajectory: dict[str, str] = {}
    for step in steps:
        run_id = str(step.get("run_id") or "")
        round_id = str(step.get("round_id") or "")
        stage = str(step.get("stage") or "")
        for item in _candidate_items(step):
            candidate_id = str(item.get("candidate_id") or item.get("idx") or "").strip()
            expression = str(item.get("expression") or "").strip()
            if not candidate_id and not expression:
                continue
            trajectory_id = str(item.get("trajectory_id") or "").strip() or _trajectory_id(
                run_id, round_id, candidate_id, expression
            )
            matched_factor_id = str(
                item.get("matched_existing_factor_id")
                or item.get("matched_existing_factor")
                or ""
            ).strip()
            region_uid = str(item.get("matched_region_uid") or "").strip()
            if not region_uid and matched_factor_id and not matched_factor_id.startswith("session:"):
                region_uid = factor_to_region.get(matched_factor_id, "")
            if region_uid:
                region_by_trajectory[trajectory_id] = region_uid
            event_id = hashlib.sha256(
                f"{step.get('stage_id')}|{trajectory_id}|{stage}".encode("utf-8")
            ).hexdigest()[:20]
            observation = observations_by_event.setdefault(
                event_id,
                {
                    "observation_id": f"fmo_{event_id}",
                    "trajectory_id": trajectory_id,
                    "ts": step.get("ts"),
                    "run_id": run_id,
                    "round_id": round_id,
                    "stage": stage,
                    "candidate_id": candidate_id,
                    "expression": expression[:420],
                    "outcome": _stage_outcome(stage, item),
                    "reason": str(item.get("reason") or item.get("deep_reason") or "")[:240],
                    "matched_factor_id": matched_factor_id or None,
                    "region_uid": region_uid or None,
                    "semantic_signature": semantic_signature(expression),
                    "source": "research_steps",
                    "advisory_only": True,
                    "not_gate": True,
                },
            )
            if region_uid:
                observation["region_uid"] = region_uid
            for key in (
                "score",
                "grade",
                "quick_score",
                "novelty_score",
                "deep_score",
                "rolling_score",
                "adopted",
            ):
                if item.get(key) not in (None, ""):
                    observation[key] = item.get(key)

    observations = sorted(
        observations_by_event.values(),
        key=lambda item: str(item.get("ts") or ""),
    )
    for observation in observations:
        if not observation.get("region_uid"):
            observation["region_uid"] = region_by_trajectory.get(
                str(observation.get("trajectory_id") or "")
            )
    observations = observations[-max(1, int(max_observations)) :]

    trajectories: dict[str, dict[str, Any]] = {}
    unmapped: list[dict[str, Any]] = []
    for observation in observations:
        region_uid = str(observation.get("region_uid") or "").strip()
        if not region_uid:
            unmapped.append(
                {
                    **observation,
                    "mapping_reason": (
                        "session_reference_has_no_stable_region"
                        if str(observation.get("matched_factor_id") or "").startswith("session:")
                        else "no_explicit_active_factor_region_anchor"
                    ),
                }
            )
            continue
        trajectory_id = str(observation.get("trajectory_id") or "")
        trajectory = trajectories.setdefault(
            trajectory_id,
            {
                "trajectory_id": trajectory_id,
                "region_uid": region_uid,
                "run_id": observation.get("run_id"),
                "round_id": observation.get("round_id"),
                "candidate_id": observation.get("candidate_id"),
                "expression": observation.get("expression"),
                "semantic_signature": observation.get("semantic_signature"),
                "novelty_checked": False,
                "novelty_rejected": False,
                "deep_checked": False,
                "deep_rejected": False,
                "gate_checked": False,
                "imported": False,
                "deep_failure_category": None,
            },
        )
        trajectory["region_uid"] = trajectory.get("region_uid") or region_uid
        trajectory["semantic_signature"] = (
            trajectory.get("semantic_signature") or observation.get("semantic_signature")
        )
        outcome = str(observation.get("outcome") or "")
        stage = str(observation.get("stage") or "")
        if stage == "novelty_review":
            trajectory["novelty_checked"] = True
            trajectory["novelty_rejected"] = outcome == "novelty_blocked"
        elif stage == "deep_validation_review":
            trajectory["deep_checked"] = True
            trajectory["deep_rejected"] = outcome == "deep_not_passed"
            if trajectory["deep_rejected"]:
                trajectory["deep_failure_category"] = _deep_failure_category(
                    str(observation.get("reason") or "")
                )
        elif stage == "import_gate_review":
            trajectory["gate_checked"] = True
        elif stage == "import_review" and outcome == "imported":
            trajectory["imported"] = True

    region_activity: dict[str, Any] = {}
    for trajectory in trajectories.values():
        region_uid = str(trajectory.get("region_uid") or "").strip()
        if not region_uid:
            continue
        activity = region_activity.setdefault(
            region_uid,
            {
                "region_uid": region_uid,
                "trajectory_count": 0,
                "round_ids": set(),
                "novelty_checked": 0,
                "novelty_rejected": 0,
                "novelty_rejected_rounds": set(),
                "deep_checked": 0,
                "deep_rejected": 0,
                "deep_rejected_rounds": set(),
                "gate_checked": 0,
                "imported_near_region": 0,
                "rejected_semantic_signatures": {},
                "deep_failure_categories": {},
            },
        )
        activity["trajectory_count"] += 1
        round_id_value = str(trajectory.get("round_id") or "")
        if round_id_value:
            activity["round_ids"].add(round_id_value)
        if trajectory.get("novelty_checked"):
            activity["novelty_checked"] += 1
        if trajectory.get("novelty_rejected"):
            activity["novelty_rejected"] += 1
            if round_id_value:
                activity["novelty_rejected_rounds"].add(round_id_value)
            signature = str(trajectory.get("semantic_signature") or "")
            if signature:
                signatures = activity["rejected_semantic_signatures"]
                signatures[signature] = int(signatures.get(signature) or 0) + 1
        if trajectory.get("deep_checked"):
            activity["deep_checked"] += 1
        if trajectory.get("deep_rejected"):
            activity["deep_rejected"] += 1
            if round_id_value:
                activity["deep_rejected_rounds"].add(round_id_value)
            category = str(trajectory.get("deep_failure_category") or "other")
            categories = activity["deep_failure_categories"]
            categories[category] = int(categories.get(category) or 0) + 1
        if trajectory.get("gate_checked"):
            activity["gate_checked"] += 1
        if trajectory.get("imported"):
            activity["imported_near_region"] += 1

    for activity in region_activity.values():
        activity["round_count"] = len(activity.pop("round_ids", set()))
        activity["novelty_rejected_round_count"] = len(
            activity.pop("novelty_rejected_rounds", set())
        )
        activity["deep_rejected_round_count"] = len(
            activity.pop("deep_rejected_rounds", set())
        )
        signatures = activity.pop("rejected_semantic_signatures", {})
        activity["max_rejected_semantic_signature_count"] = max(
            [int(value or 0) for value in signatures.values()] or [0]
        )
        categories = activity.pop("deep_failure_categories", {})
        if categories:
            dominant_category, dominant_count = max(
                categories.items(), key=lambda item: int(item[1] or 0)
            )
        else:
            dominant_category, dominant_count = None, 0
        activity["dominant_deep_failure_category"] = dominant_category
        activity["max_deep_failure_category_count"] = dominant_count
        checked = int(activity.get("novelty_checked") or 0)
        rejected = int(activity.get("novelty_rejected") or 0)
        activity["novelty_rejection_rate"] = round(rejected / checked, 4) if checked else None
        deep_checked = int(activity.get("deep_checked") or 0)
        deep_rejected = int(activity.get("deep_rejected") or 0)
        activity["deep_rejection_rate"] = (
            round(deep_rejected / deep_checked, 4) if deep_checked else None
        )
        activity["guidance"] = _region_guidance(activity)
    return observations, region_activity, unmapped[-40:]


def factor_map_design_context(
    value: dict[str, Any] | None,
    *,
    run_id: str = "",
) -> dict[str, Any]:
    """Project persistent region guidance plus a current-run trajectory overlay.

    The pinned cluster membership never changes inside a run.  Cross-run
    guidance comes from the already-computed map activity, while only the
    deduplicated current-run counters are refreshed at an LLM design-stage
    boundary.  This keeps the map's memory without mixing historical counts
    into the current-run receipt.
    """

    value = value if isinstance(value, dict) else {}
    if not value.get("available"):
        return {
            "available": False,
            "schema_version": FACTOR_MAP_SCHEMA_VERSION,
            "map_id": value.get("map_id"),
            "audit_id": value.get("audit_id"),
            "reason": value.get("reason") or "factor_map_unavailable",
            "regions": [],
        }
    regions = [item for item in (value.get("regions") or []) if isinstance(item, dict)]
    persistent_activity = (
        value.get("region_activity")
        if isinstance(value.get("region_activity"), dict)
        else {}
    )
    _, activity, _ = _research_activity(regions=regions, run_id=run_id)
    prompt_regions: list[dict[str, Any]] = []
    for idx, region in enumerate(regions, start=1):
        profile = build_region_profile(region)
        region_uid = str(region.get("region_uid") or "")
        run_activity = activity.get(region_uid) or {}
        persisted = persistent_activity.get(region_uid) or {}
        current_guidance = (
            run_activity.get("guidance")
            if isinstance(run_activity.get("guidance"), dict)
            else {}
        )
        persistent_guidance = (
            persisted.get("guidance")
            if isinstance(persisted.get("guidance"), dict)
            else {}
        )
        guidance_rank = {"insufficient_evidence": 0, "observe": 1, "action": 2}
        guidance = max(
            [current_guidance, persistent_guidance],
            key=lambda item: guidance_rank.get(str(item.get("level") or ""), 0),
        )
        representative = (
            region.get("representative")
            if isinstance(region.get("representative"), dict)
            else {}
        )
        prompt_regions.append(
            {
                "region_id": f"R{idx:02d}",
                "region_uid": region_uid,
                "name": profile.get("name"),
                "core_fields": profile.get("core_fields") or [],
                "core_structures": profile.get("core_structures") or [],
                "combination_form": profile.get("combination_form"),
                # The full audit and GUI retain active_factor_count.  It is
                # intentionally absent from the model projection because the
                # LLM repeatedly treated low library coverage as alpha
                # evidence despite explicit instructions not to do so.
                "representative_factor": {
                    "factor_id": representative.get("factor_id"),
                    "name": representative.get("name"),
                    "expression": representative.get("expression"),
                    "admission_score": representative.get("admission_score"),
                    "score_source": representative.get("score_source"),
                },
                "current_run_trajectory": {
                    key: run_activity.get(key)
                    for key in (
                        "trajectory_count",
                        "round_count",
                        "novelty_checked",
                        "novelty_rejected",
                        "deep_checked",
                        "deep_rejected",
                        "imported_near_region",
                    )
                    if run_activity.get(key) not in (None, 0, "")
                },
                "guidance": guidance or {
                    "level": "insufficient_evidence",
                    "action": "none",
                    "instruction": "",
                    "advisory_only": True,
                },
            }
        )
    return {
        "available": True,
        "schema_version": FACTOR_MAP_SCHEMA_VERSION,
        "map_id": value.get("map_id"),
        "audit_id": value.get("audit_id"),
        "scope": "active_library_occupied_information_regions_only",
        "run_id": run_id or None,
        "regions": prompt_regions,
        "usage": (
            "因子地图说明active库已覆盖的经济关系、跨run正式研究引导及本run轨迹。"
            "它不是研究机会排名、候选级novelty预审或质量门；"
            "共享字段不等于重复，正式候选结论以工具证据为准。"
        ),
    }


def _map_outputs(audit: dict[str, Any], *, region_uid: str = "") -> dict[str, Any]:
    summary = dict(audit.get("summary") or {})
    regions = [
        {
            **dict(item),
            "semantic_profile": build_region_profile(item),
        }
        for item in (audit.get("information_clusters") or [])
        if isinstance(item, dict)
    ]
    selected_region = next(
        (item for item in regions if str(item.get("region_uid") or "") == str(region_uid or "")),
        None,
    )
    relation_graph = dict(audit.get("relation_graph") or {})
    stale = bool(summary.get("stale", True))
    status = "missing" if not audit.get("audit_id") else "stale" if stale else "fresh"
    region_relations = _region_relations(relation_graph)
    recent_observations, region_activity, unmapped_activity = _research_activity(
        regions=regions
    )
    return {
        "schema_version": FACTOR_MAP_SCHEMA_VERSION,
        "map_id": audit.get("map_id"),
        "audit_id": audit.get("audit_id"),
        "generated_at": audit.get("generated_at"),
        "status": status,
        "available_for_research": status == "fresh" and bool(regions),
        "audit": {
            "audit_version": audit.get("audit_version"),
            "registry_fingerprint": (audit.get("audit_fingerprint") or {}).get("registry_fingerprint"),
            "values_manifest_fingerprint": (audit.get("audit_fingerprint") or {}).get("manifest_registry_fingerprint"),
            "fresh": status == "fresh",
            "stale_reason": summary.get("stale_reason") or "",
            "active_factor_count": summary.get("current_active_count") or summary.get("active_count"),
            "audited_factor_count": summary.get("factor_count"),
            "usable_factor_count": summary.get("usable_count"),
            "active_pool_coverage_complete": bool(summary.get("active_pool_coverage_complete")),
        },
        "summary": {
            "region_count": len(regions),
            "factor_node_count": len(relation_graph.get("nodes") or []),
            "factor_relation_count": len(relation_graph.get("edges") or []),
            "region_relation_count": len(region_relations),
            "recent_observation_count": len(recent_observations),
            "active_region_count": len(region_activity),
            "governed_annotation_count": 0,
            "prompt_eligible_annotation_count": 0,
            "unmapped_evidence_count": len(unmapped_activity),
            "lineage_event_counts": {
                event: sum(str(item.get("lineage_event") or "") == event for item in regions)
                for event in ("unchanged", "membership_changed", "new", "split", "merged", "reclustered")
            },
        },
        "regions": regions,
        "region_relations": region_relations,
        "relation_graph": relation_graph,
        "top_correlated_pairs": audit.get("top_correlated_pairs") or [],
        "region_activity": region_activity,
        "selected_region": selected_region,
        "selected_region_activity": region_activity.get(str(region_uid or "")),
        "recent_observations": recent_observations,
        "governed_annotations": [],
        "unmapped_evidence": unmapped_activity,
        "legacy_experience": _legacy_experience_state(),
        "policy": {
            "region_source": "factor_value_information_audit_only",
            "research_activity_status": "research_steps_projection_enabled",
            "annotations_status": "legacy_archive_receipt_only_not_loaded",
            "numeric_novelty_still_required": True,
            "unmapped_evidence_enters_prompt": False,
            "map_context_is_advisory_only": True,
            "gate_or_score_effect": False,
        },
    }


def factor_map_status(*, region_uid: str = ""):
    """Return the unified, read-only factor-map view from the latest audit."""

    result = factor_library_audit_status(scope="information")
    audit = result.outputs if isinstance(result.outputs, dict) else {}
    artifacts = dict(result.artifacts or {})
    if LEGACY_EXPERIENCE_MIGRATION_FILE.is_file():
        artifacts["legacy_experience_migration"] = str(LEGACY_EXPERIENCE_MIGRATION_FILE)
    return ok_result(
        inputs={"region_uid": region_uid or None},
        outputs=_map_outputs(audit, region_uid=region_uid),
        artifacts=artifacts,
        warnings=result.warnings,
    )


def factor_map_context() -> dict[str, Any]:
    """Return the single fail-closed factor-map context for research design."""

    result = factor_map_status()
    outputs = result.outputs if isinstance(result.outputs, dict) else {}
    if not outputs.get("available_for_research"):
        return {
            "available": False,
            "schema_version": FACTOR_MAP_SCHEMA_VERSION,
            "map_id": outputs.get("map_id"),
            "audit_id": outputs.get("audit_id"),
            "reason": (outputs.get("audit") or {}).get("stale_reason") or outputs.get("status") or "factor_map_unavailable",
            "regions": [],
            "region_relations": [],
        }
    return {
        "available": True,
        "schema_version": FACTOR_MAP_SCHEMA_VERSION,
        "map_id": outputs.get("map_id"),
        "audit_id": outputs.get("audit_id"),
        "generated_at": outputs.get("generated_at"),
        "audit": outputs.get("audit") or {},
        "regions": outputs.get("regions") or [],
        "region_relations": outputs.get("region_relations") or [],
        "region_activity": outputs.get("region_activity") or {},
        "recent_observations": outputs.get("recent_observations") or [],
        "governed_annotations": [
            item
            for item in (outputs.get("governed_annotations") or [])
            if isinstance(item, dict) and bool(item.get("prompt_eligible"))
        ],
        "policy": outputs.get("policy") or {},
    }
