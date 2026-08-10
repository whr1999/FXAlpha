import json

from services import factor_library_audit_service as svc


def _patch_freshness(monkeypatch, *, fingerprint="fp-current", active_count=2, manifest_stale=False):
    records = [{"factor_id": f"f{i}"} for i in range(active_count)]
    monkeypatch.setattr(svc, "current_active_registry_fingerprint", lambda: (fingerprint, records))
    monkeypatch.setattr(
        svc,
        "active_values_store_summary",
        lambda: {"stale": manifest_stale, "manifest_registry_fingerprint": fingerprint},
    )


def _write_latest(path, *, audit_type="information", registry_fingerprint="fp-current", factor_count=2, candidates=None):
    payload = {
        "audit_version": svc.AUDIT_VERSION,
        "audit_type": audit_type,
        "audit_id": "fa_test",
        "audit_fingerprint": {
            "registry_fingerprint": registry_fingerprint,
            "manifest_registry_fingerprint": registry_fingerprint,
            "factor_count": factor_count,
            "status_filter": "active",
        },
        "summary": {"status": "completed", "audit_type": audit_type, "factor_count": factor_count, "active_count": factor_count},
        "factor_checks": [],
        "feature_set_recommendations": [{"name": "FS_ALL_ACTIVE", "factor_ids": ["f1"], "count": 1}],
        "cluster_representatives": [{"cluster_id": "information_001", "factor_id": "f1"}],
        "information_clusters": [],
        "redundancy_clusters": [{"cluster_id": "redundancy_001", "retire_candidates": candidates or []}],
        "actions": {"safe_to_auto_retire": False, "requires_human_confirmation": True, "retire_candidates": candidates or []},
        "artifacts": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _patch_latest_paths(tmp_path, monkeypatch):
    quality = tmp_path / "quality" / "latest.json"
    information = tmp_path / "information" / "latest.json"
    monkeypatch.setattr(svc, "LATEST_QUALITY_AUDIT_FILE", quality)
    monkeypatch.setattr(svc, "LATEST_INFORMATION_AUDIT_FILE", information)
    monkeypatch.setattr(svc, "AUDIT_REPORT_DIR", tmp_path / "reports")
    return quality, information


def test_information_status_marks_stale_when_registry_changes(tmp_path, monkeypatch):
    _, information = _patch_latest_paths(tmp_path, monkeypatch)
    _patch_freshness(monkeypatch, fingerprint="fp-current", active_count=2)
    _write_latest(information, registry_fingerprint="fp-old")

    result = svc.factor_library_audit_status(scope="information")

    assert result.ok
    assert result.outputs["summary"]["stale"] is True
    assert "active_registry_fingerprint_mismatch" in result.outputs["summary"]["stale_reason"]


def test_quality_and_information_latest_do_not_overwrite_each_other(tmp_path, monkeypatch):
    quality, information = _patch_latest_paths(tmp_path, monkeypatch)
    quality_payload = {"audit_type": "quality", "audit_id": "fa_same", "summary": {}}
    information_payload = {"audit_type": "information", "audit_id": "fa_same", "summary": {}}

    svc._save_report(quality_payload, audit_type="quality", audit_id="fa_same")
    svc._save_report(information_payload, audit_type="information", audit_id="fa_same")

    assert json.loads(quality.read_text())["audit_type"] == "quality"
    assert json.loads(information.read_text())["audit_type"] == "information"
    assert len(list((tmp_path / "reports").glob("*.json"))) == 2


def test_fresh_information_retire_plan_keeps_read_only_candidates(tmp_path, monkeypatch):
    _, information = _patch_latest_paths(tmp_path, monkeypatch)
    _patch_freshness(monkeypatch)
    _write_latest(information, candidates=["f2"])

    result = svc.factor_retire_plan()

    assert result.ok
    assert result.outputs["summary"]["stale"] is False
    assert result.outputs["actions"]["retire_candidates"] == ["f2"]
    assert result.outputs["actions"]["safe_to_auto_retire"] is False


def test_information_context_fails_closed_when_report_missing(tmp_path, monkeypatch):
    _patch_latest_paths(tmp_path, monkeypatch)
    _patch_freshness(monkeypatch)

    context = svc.factor_library_information_context()

    assert context["available"] is False
    assert context["information_families"] == []
    assert "missing" in context["reason"]


def test_information_context_can_be_advisory_after_operational_refresh_failure(tmp_path, monkeypatch):
    _, information = _patch_latest_paths(tmp_path, monkeypatch)
    _patch_freshness(monkeypatch, fingerprint="fp-current", active_count=2)
    _write_latest(information, registry_fingerprint="fp-prior")

    governance = svc.factor_library_information_context()
    advisory = svc.factor_library_information_context(allow_stale_advisory=True)

    assert governance["available"] is False
    assert advisory["available"] is True
    assert advisory["freshness"] == "stale_advisory_only"
    assert advisory["policy"] == "advisory_research_context_only_numeric_novelty_still_required"


def test_factor_library_audit_rejects_invalid_scope_and_thresholds():
    result = svc.factor_library_audit(
        scope="surprise",
        save_report=False,
        min_valid_days="bad",
        min_common_stocks=0,
        redundancy_threshold_rank_p90=1.2,
    )

    assert not result.ok
    assert "scope must be one of: quality, information, all" in result.outputs["validation_errors"]
    assert "min_valid_days must be an integer >= 0" in result.outputs["validation_errors"]
    assert "min_common_stocks must be > 0" in result.outputs["validation_errors"]


def test_audit_fingerprint_changes_with_type_and_thresholds(monkeypatch):
    _patch_freshness(monkeypatch, active_count=1)
    rows = [{"factor_id": "f1", "status": "active"}]
    kwargs = dict(
        rows=rows,
        status_filter="active",
        audit_window_start=None,
        audit_window_end=None,
        min_valid_days=120,
        min_common_stocks=300,
        redundancy_threshold_rank_p90=0.80,
        redundancy_threshold_pearson_p90=0.75,
        family_dependency_cut=0.55,
    )
    quality = svc._audit_fingerprint(audit_type="quality", **kwargs)
    information = svc._audit_fingerprint(audit_type="information", **kwargs)
    stricter = svc._audit_fingerprint(audit_type="information", **{**kwargs, "min_valid_days": 180})

    assert quality["cache_key"] != information["cache_key"]
    assert information["cache_key"] != stricter["cache_key"]


def test_information_clusters_include_singletons():
    rows = {
        "f1": {"factor_id": "f1", "name": "one", "expression": "rank(close)", "metadata": {"deep_score": 80}},
        "f2": {"factor_id": "f2", "name": "two", "expression": "rank(amount)", "metadata": {"deep_score": 81}},
    }

    clusters = svc._information_clusters(["f1", "f2"], [], rows, family_dependency_cut=0.55)

    assert sum(cluster["size"] for cluster in clusters) == 2
    assert all(cluster["size"] == 1 for cluster in clusters)
    assert {member["factor_id"] for cluster in clusters for member in cluster["members"]} == {"f1", "f2"}
    assert all(cluster["representative"]["expression"] for cluster in clusters)


def test_information_region_identity_is_stable_and_records_membership_change():
    previous = {
        "information_clusters": [
            {
                "cluster_id": "information_001",
                "region_uid": "region_existing",
                "members": [{"factor_id": "f1"}, {"factor_id": "f2"}],
            }
        ]
    }
    current = [
        {
            "cluster_id": "information_009",
            "members": [{"factor_id": "f1"}, {"factor_id": "f2"}, {"factor_id": "f3"}],
        }
    ]

    identified = svc._assign_region_identity(current, previous)

    assert identified[0]["region_uid"] == "region_existing"
    assert identified[0]["lineage_event"] == "membership_changed"
    assert identified[0]["previous_region_uids"] == ["region_existing"]
    assert identified[0]["display_index"] == 1


def test_information_region_split_creates_new_children_with_parent_lineage():
    previous = {
        "information_clusters": [
            {
                "region_uid": "region_parent",
                "members": [{"factor_id": "f1"}, {"factor_id": "f2"}],
            }
        ]
    }
    current = [
        {"cluster_id": "information_001", "members": [{"factor_id": "f1"}]},
        {"cluster_id": "information_002", "members": [{"factor_id": "f2"}]},
    ]

    identified = svc._assign_region_identity(current, previous)

    assert {item["lineage_event"] for item in identified} == {"split"}
    assert all(item["previous_region_uids"] == ["region_parent"] for item in identified)
    assert all(item["region_uid"] != "region_parent" for item in identified)


def test_feature_set_recommendations_use_family_topk_plus_singletons():
    scores = {"f1": 80, "f2": 95, "f3": 90, "f4": 88}
    rows = [
        {"factor_id": factor_id, "status": "active", "metadata": {"deep_score": score}, "created_at": f"2026-01-0{idx}"}
        for idx, (factor_id, score) in enumerate(scores.items(), start=1)
    ]
    information_clusters = [
        {"members": [{"factor_id": "f1"}, {"factor_id": "f2"}]},
        {"members": [{"factor_id": "f3"}]},
        {"members": [{"factor_id": "f4"}]},
    ]

    by_name = {item["name"]: item for item in svc._feature_sets(rows, [], information_clusters)}

    assert by_name["FAMILY_TOP1_PLUS_UNCLUSTERED8"]["factor_ids"] == ["f2", "f3", "f4"]
    assert by_name["ALL_ACTIVE"]["factor_ids"] == list(scores)


def test_factor_relation_graph_keeps_all_nodes_and_links_family_representatives():
    rows = [
        {"factor_id": "f1", "name": "one", "category": "price", "metadata": {"deep_score": 91}},
        {"factor_id": "f2", "name": "two", "category": "price", "metadata": {"deep_score": 85}},
        {"factor_id": "f3", "name": "three", "category": "flow", "metadata": {"deep_score": 89}},
    ]
    clusters = [
        {
            "cluster_id": "information_001",
            "region_uid": "region_one",
            "representative": {"factor_id": "f1", "name": "one"},
            "members": [{"factor_id": "f1"}, {"factor_id": "f2"}],
        },
        {
            "cluster_id": "information_002",
            "region_uid": "region_two",
            "representative": {"factor_id": "f3", "name": "three"},
            "members": [{"factor_id": "f3"}],
        },
    ]
    pairs = [
        {"factor_a": "f1", "factor_b": "f2", "dependency_score": 0.72, "valid_days": 200},
        {"factor_a": "f1", "factor_b": "f3", "dependency_score": 0.61, "valid_days": 200},
        {"factor_a": "f2", "factor_b": "f3", "dependency_score": 0.42, "valid_days": 200},
    ]

    graph = svc._factor_relation_graph(rows, clusters, pairs)

    assert {node["factor_id"] for node in graph["nodes"]} == {"f1", "f2", "f3"}
    assert {edge["relation_type"] for edge in graph["edges"]} == {"family_link", "representative_link"}
    assert {node["region_uid"] for node in graph["nodes"]} == {"region_one", "region_two"}
    assert all(edge["source_region_uid"] and edge["target_region_uid"] for edge in graph["edges"])
    assert graph["summary"]["node_count"] == 3
    assert graph["summary"]["representative_edge_count"] == 1
