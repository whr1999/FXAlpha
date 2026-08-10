import json

from services import factor_map_service as svc
from services._base import ok_result


def _audit_payload(*, stale: bool = False):
    return {
        "audit_version": "factor_library_audit_v4",
        "audit_id": "fa_test",
        "map_id": "fm_test",
        "generated_at": "2026-07-23T12:00:00",
        "audit_fingerprint": {
            "registry_fingerprint": "registry-fp",
            "manifest_registry_fingerprint": "values-fp",
        },
        "summary": {
            "stale": stale,
            "stale_reason": "registry_changed" if stale else "",
            "current_active_count": 2,
            "factor_count": 2,
            "usable_count": 2,
            "active_pool_coverage_complete": True,
        },
        "information_clusters": [
            {
                "cluster_id": "information_001",
                "region_uid": "region_one",
                "lineage_event": "unchanged",
                "members": [{"factor_id": "f1"}],
                "representative": {"factor_id": "f1"},
            },
            {
                "cluster_id": "information_002",
                "region_uid": "region_two",
                "lineage_event": "new",
                "members": [{"factor_id": "f2"}],
                "representative": {"factor_id": "f2"},
            },
        ],
        "relation_graph": {
            "nodes": [
                {"factor_id": "f1", "region_uid": "region_one"},
                {"factor_id": "f2", "region_uid": "region_two"},
            ],
            "edges": [
                {
                    "source": "f1",
                    "target": "f2",
                    "source_region_uid": "region_one",
                    "target_region_uid": "region_two",
                    "dependency_score": 0.61,
                }
            ],
        },
    }


def test_factor_map_status_composes_regions_relations_and_archive_state(monkeypatch, tmp_path):
    marker = tmp_path / "ARCHIVED.json"
    marker.write_text(
        json.dumps({"archive_path": "/archive/experience", "archived_at": "2026-07-23", "file_count": 184}),
        encoding="utf-8",
    )
    monkeypatch.setattr(svc, "LEGACY_EXPERIENCE_MARKER", marker)
    monkeypatch.setattr(svc, "LEGACY_EXPERIENCE_MIGRATION_FILE", tmp_path / "migration.json")
    monkeypatch.setattr(svc, "RESEARCH_STEPS_FILE", tmp_path / "missing.jsonl")
    monkeypatch.setattr(svc, "RESEARCH_STEPS_HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(
        svc,
        "factor_library_audit_status",
        lambda scope="information": ok_result(outputs=_audit_payload()),
    )

    result = svc.factor_map_status(region_uid="region_two")

    assert result.ok
    assert result.outputs["available_for_research"] is True
    assert result.outputs["map_id"] == "fm_test"
    assert result.outputs["summary"]["region_count"] == 2
    assert result.outputs["summary"]["region_relation_count"] == 1
    assert result.outputs["selected_region"]["region_uid"] == "region_two"
    assert result.outputs["legacy_experience"]["status"] == "archived"


def test_factor_map_context_fails_closed_for_stale_audit(monkeypatch):
    monkeypatch.setattr(svc, "RESEARCH_STEPS_FILE", svc.Path("/missing/research_steps.jsonl"))
    monkeypatch.setattr(svc, "RESEARCH_STEPS_HISTORY_DIR", svc.Path("/missing/research_steps_history"))
    monkeypatch.setattr(
        svc,
        "factor_library_audit_status",
        lambda scope="information": ok_result(outputs=_audit_payload(stale=True)),
    )

    context = svc.factor_map_context()

    assert context["available"] is False
    assert context["reason"] == "registry_changed"
    assert context["regions"] == []


def test_region_guidance_uses_cross_round_observe_then_action_thresholds():
    novelty_observe = svc._region_guidance(
        {
            "novelty_checked": 2,
            "novelty_rejected": 2,
            "novelty_rejected_round_count": 2,
            "max_rejected_semantic_signature_count": 1,
        }
    )
    novelty_action = svc._region_guidance(
        {
            "novelty_checked": 4,
            "novelty_rejected": 3,
            "novelty_rejected_round_count": 3,
            "max_rejected_semantic_signature_count": 2,
        }
    )
    deep_observe = svc._region_guidance(
        {
            "deep_checked": 2,
            "deep_rejected": 2,
            "deep_rejected_round_count": 2,
            "max_deep_failure_category_count": 2,
        }
    )
    deep_action = svc._region_guidance(
        {
            "deep_checked": 3,
            "deep_rejected": 3,
            "deep_rejected_round_count": 3,
            "max_deep_failure_category_count": 3,
            "dominant_deep_failure_category": "rolling_stability",
        }
    )

    assert novelty_observe["action"] == "watch_novelty_crowding"
    assert novelty_action["action"] == "avoid_near_copy"
    assert deep_observe["action"] == "watch_deep_fragility"
    assert deep_action["action"] == "change_validation_mechanism"


def test_design_context_keeps_persistent_guidance_and_current_run_counts_separate(monkeypatch):
    value = {
        "available": True,
        "map_id": "fm-persistent",
        "audit_id": "fa-persistent",
        "regions": [
            {
                "region_uid": "region_crowded",
                "representative": {
                    "factor_id": "f1",
                    "name": "Crowded",
                    "expression": "rank(ts_mean(amount,10))",
                },
                "members": [{"factor_id": "f1"}],
            }
        ],
        "region_activity": {
            "region_crowded": {
                "trajectory_count": 18,
                "novelty_rejected": 16,
                "guidance": {
                    "level": "action",
                    "action": "avoid_near_copy",
                    "instruction": "停止同类参数变体。",
                    "advisory_only": True,
                },
            }
        },
    }
    monkeypatch.setattr(
        svc,
        "_research_activity",
        lambda **kwargs: (
            [],
            {
                "region_crowded": {
                    "trajectory_count": 2,
                    "round_count": 2,
                    "novelty_rejected": 2,
                    "guidance": {
                        "level": "observe",
                        "action": "watch_novelty_crowding",
                        "instruction": "本轮继续观察。",
                        "advisory_only": True,
                    },
                }
            },
            [],
        ),
    )

    context = svc.factor_map_design_context(value, run_id="run-current")
    region = context["regions"][0]

    assert region["guidance"]["action"] == "avoid_near_copy"
    assert region["current_run_trajectory"] == {
        "trajectory_count": 2,
        "round_count": 2,
        "novelty_rejected": 2,
    }
    assert "跨run正式研究引导" in context["usage"]


def test_legacy_experience_is_receipt_only_and_not_loaded_into_map(monkeypatch, tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    marker = tmp_path / "ARCHIVED.json"
    marker.write_text(
        json.dumps(
            {
                "archive_path": str(archive),
                "archived_at": "2026-07-23T14:31:40+08:00",
                "file_count": 184,
                "new_writes_disabled": True,
                "recoverable": True,
            }
        ),
        encoding="utf-8",
    )
    migration_file = tmp_path / "migration.json"
    migration_file.write_text(
        json.dumps(
            {
                "status": "completed",
                "migration_id": "fmem_frozen",
                "source_integrity_verified": True,
                "governed_annotations": [{"mapping_status": "unmapped"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(svc, "LEGACY_EXPERIENCE_MARKER", marker)
    monkeypatch.setattr(svc, "LEGACY_EXPERIENCE_MIGRATION_FILE", migration_file)

    state = svc._legacy_experience_state()

    assert not hasattr(svc, "migrate_legacy_experience_archive")
    assert state["status"] == "archived"
    assert state["migration"]["status"] == "completed"
    assert state["migration"]["effective_annotation_count"] == 1
    assert archive.is_dir()


def test_factor_map_projects_one_candidate_trajectory_across_novelty_and_deep(monkeypatch, tmp_path):
    steps = tmp_path / "research_steps.jsonl"
    trajectory_id = "ft_test_candidate"
    records = [
        {
            "ts": "2026-07-23T12:01:00",
            "run_id": "run-test",
            "round_id": "run-test:r0001",
            "stage_id": "run-test:r0001:s07",
            "stage": "novelty_review",
            "evidence_refs": [
                {
                    "type": "advice_summary",
                    "candidate_lane_decisions": [
                        {
                            "candidate_id": "c1",
                            "expression": "rank(close)",
                            "trajectory_id": trajectory_id,
                            "action": "advance_to_deep_validation",
                            "matched_existing_factor_id": "f1",
                            "matched_region_uid": "region_one",
                        }
                    ],
                }
            ],
        },
        {
            "ts": "2026-07-23T12:02:00",
            "run_id": "run-test",
            "round_id": "run-test:r0001",
            "stage_id": "run-test:r0001:s08",
            "stage": "deep_validation_review",
            "evidence_refs": [
                {
                    "type": "advice_summary",
                    "candidate_lane_decisions": [
                        {
                            "candidate_id": "c1",
                            "expression": "rank(close)",
                            "trajectory_id": trajectory_id,
                            "action": "revise_expression",
                        }
                    ],
                }
            ],
        },
    ]
    steps.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
        encoding="utf-8",
    )
    monkeypatch.setattr(svc, "RESEARCH_STEPS_FILE", steps)
    monkeypatch.setattr(svc, "RESEARCH_STEPS_HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(svc, "LEGACY_EXPERIENCE_MIGRATION_FILE", tmp_path / "migration.json")
    monkeypatch.setattr(
        svc,
        "factor_library_audit_status",
        lambda scope="information": ok_result(outputs=_audit_payload()),
    )

    result = svc.factor_map_status()

    assert result.ok
    observations = result.outputs["recent_observations"]
    assert [item["trajectory_id"] for item in observations] == [trajectory_id, trajectory_id]
    assert [item["outcome"] for item in observations] == ["novelty_passed", "deep_not_passed"]
    assert all(item["region_uid"] == "region_one" for item in observations)
    activity = result.outputs["region_activity"]["region_one"]
    assert activity["trajectory_count"] == 1
    assert activity["novelty_checked"] == 1
    assert activity["novelty_rejected"] == 0
    assert activity["deep_checked"] == 1
    assert activity["deep_rejected"] == 1
