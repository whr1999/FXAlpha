import os
import sqlite3
import json
from datetime import datetime, timedelta

from domain.platform_ops import cleanup_executor as cleanup
from domain.platform_ops.cleanup_policy import CleanupCategory, cleanup_categories
from storage.paths import MODEL_RUNTIME_ROOT


def _touch_file(path, content="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _package(root, name, days_old):
    path = root / name
    _touch_file(path / "payload.txt")
    ts = (datetime.now() - timedelta(days=days_old)).timestamp()
    os.utime(path, (ts, ts))
    os.utime(path / "payload.txt", (ts, ts))
    return path


def _only_data_foundation(monkeypatch, tmp_path):
    data_root = tmp_path / "runtime" / "data_foundation"
    monkeypatch.setattr(cleanup, "DATA_FOUNDATION_ROOT", data_root)
    monkeypatch.setattr(cleanup, "CURRENT_PRODUCTION_DATASET_FILE", data_root / "CURRENT_PRODUCTION_DATASET.json")
    monkeypatch.setattr(cleanup, "MODEL_FEATURE_SETS_ROOT", tmp_path / "data" / "model" / "features" / "feature_sets")
    monkeypatch.setattr(cleanup, "cleanup_categories", lambda: [])
    monkeypatch.setattr(cleanup, "_collect_cache_dirs", lambda extra: [])
    monkeypatch.setattr(cleanup, "_active_feature_protection_map", lambda: {})
    monkeypatch.setattr(cleanup, "_model_registry_feature_refs", lambda: {})
    monkeypatch.setattr(cleanup, "_model_runtime_feature_refs", lambda: {})
    monkeypatch.setattr(cleanup, "_collect_runtime_test_tmp", lambda profile, now, days: [])
    monkeypatch.setattr(cleanup, "_collect_runtime_task_tmp", lambda profile, now, days: [])
    return data_root


def _only_categories(monkeypatch, tmp_path, categories):
    monkeypatch.setattr(cleanup, "DATA_FOUNDATION_ROOT", tmp_path / "runtime" / "data_foundation")
    monkeypatch.setattr(cleanup, "CURRENT_PRODUCTION_DATASET_FILE", tmp_path / "runtime" / "data_foundation" / "CURRENT_PRODUCTION_DATASET.json")
    monkeypatch.setattr(cleanup, "MODEL_FEATURE_SETS_ROOT", tmp_path / "data" / "model" / "features" / "feature_sets")
    monkeypatch.setattr(cleanup, "cleanup_categories", lambda: categories)
    monkeypatch.setattr(cleanup, "_collect_cache_dirs", lambda extra: [])
    monkeypatch.setattr(cleanup, "_collect_data_foundation_dirs", lambda profile, now, keep_extra: [])
    monkeypatch.setattr(cleanup, "_collect_data_foundation_misc_backups", lambda profile, now, days: [])
    monkeypatch.setattr(cleanup, "_active_feature_protection_map", lambda: {})
    monkeypatch.setattr(cleanup, "_model_registry_feature_refs", lambda: {})
    monkeypatch.setattr(cleanup, "_model_runtime_feature_refs", lambda: {})
    monkeypatch.setattr(cleanup, "_collect_runtime_test_tmp", lambda profile, now, days: [])
    monkeypatch.setattr(cleanup, "_collect_runtime_task_tmp", lambda profile, now, days: [])


def test_model_cleanup_categories_use_model_runtime_root():
    model_categories = {
        category.name: category
        for category in cleanup_categories()
        if category.name.startswith("model_") and category.name != "model_feature_sets"
    }

    assert model_categories
    assert all(category.root.is_relative_to(MODEL_RUNTIME_ROOT) for category in model_categories.values())


def test_data_foundation_cleanup_keeps_current_and_recent_packages(monkeypatch, tmp_path):
    data_root = _only_data_foundation(monkeypatch, tmp_path)
    staging = data_root / "staging"
    backups = data_root / "production_backups"
    _package(staging, "stage-current", 1)
    _package(staging, "stage-recent-1", 2)
    _package(staging, "stage-recent-2", 3)
    _package(staging, "stage-old", 5)
    _package(backups, "backup-current", 1)
    _package(backups, "backup-recent-1", 2)
    _package(backups, "backup-recent-2", 3)
    _package(backups, "backup-old", 5)
    _touch_file(
        data_root / "CURRENT_PRODUCTION_DATASET.json",
        '{"production_package_id":"stage-current","promotion_id":"backup-current"}',
    )

    candidates = cleanup.build_cleanup_candidates(profile="safe")
    by_name = {item.path.split("/")[-1]: item for item in candidates}

    assert by_name["stage-current"].blocked_reason == "current_production_staging_package"
    assert by_name["backup-current"].blocked_reason == "current_production_backup"
    assert by_name["stage-recent-1"].blocked_reason == "retained_recent_2_data_foundation_staging"
    assert by_name["backup-recent-1"].blocked_reason == "retained_recent_2_data_foundation_production_backups"
    assert by_name["stage-recent-2"].blocked_reason == "retained_recent_2_data_foundation_staging"
    assert by_name["backup-recent-2"].blocked_reason == "retained_recent_2_data_foundation_production_backups"
    assert by_name["stage-old"].blocked_reason == "retained_by_7_day_retention"
    assert by_name["backup-old"].blocked_reason == "retained_by_7_day_retention"


def test_data_foundation_cleanup_blocks_all_when_lock_exists(monkeypatch, tmp_path):
    data_root = _only_data_foundation(monkeypatch, tmp_path)
    _package(data_root / "staging", "stage-old", 5)
    _package(data_root / "production_backups", "backup-old", 5)
    _touch_file(
        data_root / "CURRENT_PRODUCTION_DATASET.json",
        '{"production_package_id":"stage-current","promotion_id":"backup-current"}',
    )
    (data_root / "update.lock").mkdir(parents=True)

    candidates = cleanup.build_cleanup_candidates(profile="safe")

    assert candidates
    assert all(item.executable is False for item in candidates)
    assert {item.blocked_reason for item in candidates} == {"update_lock_exists"}


def test_data_foundation_cleanup_blocks_all_when_current_refs_missing(monkeypatch, tmp_path):
    data_root = _only_data_foundation(monkeypatch, tmp_path)
    _package(data_root / "staging", "stage-old", 5)
    _package(data_root / "production_backups", "backup-old", 5)

    candidates = cleanup.build_cleanup_candidates(profile="safe")

    assert candidates
    assert all(item.executable is False for item in candidates)
    assert {item.blocked_reason for item in candidates} == {
        "missing_current_production_package_and_current_promotion_backup"
    }


def test_pickle_cache_safe_uses_one_day_regenerable_files_only(monkeypatch, tmp_path):
    root = tmp_path / "pickle_cache"
    _touch_file(root / "old.pkl")
    _touch_file(root / "old.zip")
    _touch_file(root / "old.txt")
    _touch_file(root / "fresh.pkl")
    old_ts = (datetime.now() - timedelta(days=2)).timestamp()
    fresh_ts = (datetime.now() - timedelta(hours=2)).timestamp()
    for path in [root / "old.pkl", root / "old.zip", root / "old.txt"]:
        os.utime(path, (old_ts, old_ts))
    os.utime(root / "fresh.pkl", (fresh_ts, fresh_ts))
    _only_categories(
        monkeypatch,
        tmp_path,
        [
            CleanupCategory(
                name="pickle_cache",
                root=root,
                retention_key="pickle_cache",
                risk="medium",
                profiles=("safe",),
                description="cache",
            )
        ],
    )

    candidates = cleanup.build_cleanup_candidates(profile="safe")
    names = {item.path.split("/")[-1] for item in candidates if item.executable}

    assert names == {"old.pkl", "old.zip"}
    assert cleanup.DEFAULT_RETENTION_DAYS["pickle_cache"] == 1


def test_reset_backups_safe_retains_latest_one(monkeypatch, tmp_path):
    root = tmp_path / "runtime" / "reset_backups"
    latest = _package(root, "latest-even-old", 30)
    old_a = _package(root, "old-a", 31)
    old_b = _package(root, "old-b", 32)
    now_ts = datetime.now().timestamp()
    os.utime(latest, (now_ts, now_ts))
    _only_categories(
        monkeypatch,
        tmp_path,
        [
            CleanupCategory(
                name="reset_backups",
                root=root,
                retention_key="reset_backups",
                risk="medium",
                profiles=("safe",),
                description="reset",
            )
        ],
    )

    candidates = cleanup.build_cleanup_candidates(profile="safe")
    names = {item.path.split("/")[-1] for item in candidates if item.executable}

    assert names == {old_a.name, old_b.name}
    assert latest.name not in names
    assert cleanup.DEFAULT_RETENTION_DAYS["reset_backups"] == 7


def test_data_foundation_fresh_package_is_blocked(monkeypatch, tmp_path):
    data_root = _only_data_foundation(monkeypatch, tmp_path)
    _package(data_root / "staging", "stage-current", 5)
    _package(data_root / "staging", "stage-recent-1", 4)
    _package(data_root / "staging", "stage-recent-2", 3)
    fresh = _package(data_root / "staging", "stage-fresh", 0)
    _touch_file(
        data_root / "CURRENT_PRODUCTION_DATASET.json",
        '{"production_package_id":"stage-current","promotion_id":"backup-current"}',
    )
    ts = (datetime.now() - timedelta(hours=2)).timestamp()
    os.utime(fresh, (ts, ts))

    candidates = cleanup.build_cleanup_candidates(profile="safe")
    by_name = {item.path.split("/")[-1]: item for item in candidates}

    assert by_name["stage-fresh"].executable is False
    assert by_name["stage-fresh"].blocked_reason == "fresh_within_24h"


def test_data_foundation_misc_backups_safe_collects_old_repair_backups(monkeypatch, tmp_path):
    data_root = _only_data_foundation(monkeypatch, tmp_path)
    old = _package(data_root / "backups", "repair-old", 4)
    fresh = _package(data_root / "backups", "repair-fresh", 0)
    _touch_file(
        data_root / "CURRENT_PRODUCTION_DATASET.json",
        '{"production_package_id":"stage-current","promotion_id":"backup-current"}',
    )

    candidates = cleanup.build_cleanup_candidates(profile="safe")
    by_name = {item.path.split("/")[-1]: item for item in candidates}

    assert by_name[old.name].kind == "data_foundation_misc_backups"
    assert by_name[old.name].executable is True
    assert by_name[fresh.name].executable is False
    assert by_name[fresh.name].blocked_reason == "fresh_within_24h"


def test_model_feature_sets_safe_never_collects_feature_sets(monkeypatch, tmp_path):
    root = tmp_path / "data" / "model" / "features" / "feature_sets"
    active = _package(root, "fs-active", 20)
    old = _package(root, "fs-old", 30)
    for idx in range(5):
        _package(root, f"fs-recent-{idx}", idx + 1)
    fresh = _package(root, "fs-fresh", 0)
    active_file = tmp_path / "runtime" / "model" / "active_feature_set.json"
    _touch_file(active_file, '{"feature_set_id":"fs-active"}')
    monkeypatch.setattr(cleanup, "ACTIVE_MODEL_FEATURE_SET_FILE", active_file)
    _only_categories(monkeypatch, tmp_path, [])
    monkeypatch.setattr(cleanup, "MODEL_FEATURE_SETS_ROOT", root)
    monkeypatch.setattr(cleanup, "_active_feature_protection_map", lambda: {active: "active_model_feature_snapshot"})

    candidates = cleanup.build_cleanup_candidates(profile="safe")
    assert not [item for item in candidates if item.kind == "model_feature_sets"]


def test_model_registry_protects_archived_rows(monkeypatch, tmp_path):
    db = tmp_path / "model_registry.db"
    feature_root = tmp_path / "data" / "model" / "features" / "feature_sets"
    monkeypatch.setattr(cleanup, "MODEL_REGISTRY_DB", db)
    monkeypatch.setattr(cleanup, "MODEL_FEATURE_SETS_ROOT", feature_root)
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE models (
            feature_set_id TEXT,
            status TEXT,
            workspace_path TEXT,
            run_dir TEXT,
            metadata TEXT,
            created_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO models VALUES (?,?,?,?,?,?)",
        [
            (
                "fs-archived-newest",
                "archived",
                str(tmp_path / "ws-archived"),
                str(tmp_path / "run-archived"),
                "{}",
                "2026-06-25T07:20:19+00:00",
            ),
            (
                "fs-candidate-visible",
                "candidate",
                str(tmp_path / "ws-candidate"),
                str(tmp_path / "run-candidate"),
                "{}",
                "2026-06-25T03:02:30+00:00",
            ),
            (
                "fs-production",
                "production",
                str(tmp_path / "ws-production"),
                str(tmp_path / "run-production"),
                "{}",
                "2026-06-24T03:02:30+00:00",
            ),
        ],
    )
    conn.commit()
    conn.close()

    feature_refs = cleanup._model_registry_feature_refs()

    assert feature_refs[feature_root / "fs-archived-newest"] == "model_registry_archived"
    assert feature_refs[feature_root / "fs-candidate-visible"] == "model_registry_candidate"
    assert feature_refs[feature_root / "fs-production"] == "model_registry_production"


def test_model_cleanup_reference_maps_ignore_latest_status_snapshot(monkeypatch, tmp_path):
    feature_root = tmp_path / "data" / "model" / "features" / "feature_sets"
    active_pointer = tmp_path / "runtime" / "model" / "active_feature_set.json"
    active_manifest = tmp_path / "data" / "model" / "features" / "active" / "manifest.json"
    run_root = tmp_path / "runtime" / "model" / "runs"
    orch_events = tmp_path / "runtime" / "model" / "orchestrator_events" / "current.jsonl"

    monkeypatch.setattr(cleanup, "MODEL_FEATURE_SETS_ROOT", feature_root)
    monkeypatch.setattr(cleanup, "ACTIVE_MODEL_FEATURE_SET_FILE", active_pointer)
    monkeypatch.setattr(cleanup, "MODEL_ACTIVE_FEATURE_MANIFEST", active_manifest)
    monkeypatch.setattr(cleanup, "MODEL_RUNS_ROOT", run_root)
    monkeypatch.setattr(cleanup, "RUNTIME_ROOT", tmp_path / "runtime")
    monkeypatch.setattr(cleanup, "MODEL_RUNTIME_ROOT", tmp_path / "runtime" / "model")

    _touch_file(active_pointer, json.dumps({"feature_set_id": "fs-active"}))
    _touch_file(active_manifest, json.dumps({"feature_set_id": "fs-active-manifest", "combined_factors_file": str(feature_root / "fs-active-manifest" / "combined_factors_df.parquet")}))
    _touch_file(
        run_root / "run-recent" / "manifest.json",
        json.dumps({"feature_set_id": "fs-run-recent"}),
    )
    _touch_file(
        run_root / "run-orch" / "manifest.json",
        json.dumps({"feature_set_id": "fs-run-orch"}),
    )
    _touch_file(tmp_path / "runtime" / "model" / "latest_status.json", json.dumps({"feature_set_id": "fs-status-only", "workspace_path": str(tmp_path / "ws-status-only")}))
    _touch_file(orch_events, json.dumps({"event_type": "tool_result", "run_id": "orch", "model_run_id": "run-orch"}) + "\n")

    feature_refs = cleanup._model_runtime_feature_refs()

    assert feature_root / "fs-status-only" not in feature_refs
    assert feature_refs[feature_root / "fs-active"] == "active_model_feature_snapshot"
    assert feature_refs[feature_root / "fs-active-manifest"] == "active_model_feature_manifest"
    assert feature_refs[feature_root / "fs-run-recent"] == "model_run_manifest_recent"
    assert feature_refs[feature_root / "fs-run-orch"] == "model_orchestrator_latest_run"


def test_trading_prediction_features_safe_retains_latest_and_cleans_old(monkeypatch, tmp_path):
    root = tmp_path / "runtime" / "trading" / "prediction_features"
    latest = _package(root, "pred-latest", 20)
    old = _package(root, "pred-old", 30)
    now_ts = datetime.now().timestamp()
    os.utime(latest, (now_ts, now_ts))
    _only_categories(
        monkeypatch,
        tmp_path,
        [
            CleanupCategory(
                name="trading_prediction_features",
                root=root,
                retention_key="trading_prediction_features",
                risk="medium",
                profiles=("safe",),
                description="prediction features",
            )
        ],
    )

    candidates = cleanup.build_cleanup_candidates(profile="safe")
    by_name = {item.path.split("/")[-1]: item for item in candidates}

    assert by_name[old.name].kind == "trading_prediction_features"
    assert by_name[old.name].executable is True
    assert latest.name not in by_name
    assert cleanup.DEFAULT_RETENTION_DAYS["trading_prediction_features"] == 30


def test_factor_value_repair_safe_retains_latest_and_cleans_old(monkeypatch, tmp_path):
    root = tmp_path / "runtime" / "factor_research" / "value_repair"
    latest = _package(root, "repair-latest", 10)
    old = _package(root, "repair-old", 11)
    now_ts = datetime.now().timestamp()
    os.utime(latest, (now_ts, now_ts))
    _only_categories(
        monkeypatch,
        tmp_path,
        [
            CleanupCategory(
                name="factor_value_repair",
                root=root,
                retention_key="factor_value_repair",
                risk="medium",
                profiles=("safe",),
                description="repair",
            )
        ],
    )

    candidates = cleanup.build_cleanup_candidates(profile="safe")
    names = {item.path.split("/")[-1] for item in candidates if item.executable}

    assert names == {old.name}
    assert latest.name not in names
    assert cleanup.DEFAULT_RETENTION_DAYS["factor_value_repair"] == 2
    assert cleanup.DEFAULT_RETENTION_DAYS["factor_value_repair_keep_latest"] == 1


def test_model_quarantine_safe_retains_latest_and_cleans_old(monkeypatch, tmp_path):
    root = tmp_path / "runtime" / "model" / "quarantine"
    latest = _package(root, "quarantine-latest", 10)
    old = _package(root, "quarantine-old", 11)
    now_ts = datetime.now().timestamp()
    os.utime(latest, (now_ts, now_ts))
    _only_categories(
        monkeypatch,
        tmp_path,
        [
            CleanupCategory(
                name="model_quarantine",
                root=root,
                retention_key="model_quarantine",
                risk="medium",
                profiles=("safe",),
                description="quarantine",
            )
        ],
    )

    candidates = cleanup.build_cleanup_candidates(profile="safe")
    names = {item.path.split("/")[-1] for item in candidates if item.executable}

    assert names == {old.name}
    assert latest.name not in names
    assert cleanup.DEFAULT_RETENTION_DAYS["model_quarantine"] == 2
    assert cleanup.DEFAULT_RETENTION_DAYS["model_quarantine_keep_latest"] == 1


def test_factor_research_history_files_safe_does_not_touch_current_streams(monkeypatch, tmp_path):
    trace_root = tmp_path / "runtime" / "factor_research" / "orchestrator_llm_traces"
    event_root = tmp_path / "runtime" / "factor_research" / "orchestrator_events"
    old_trace = trace_root / "history" / "old.jsonl"
    fresh_trace = trace_root / "history" / "fresh.jsonl"
    current_trace = trace_root / "current.jsonl"
    old_event = event_root / "history" / "old.jsonl"
    current_event = event_root / "current.jsonl"
    for path in [old_trace, fresh_trace, current_trace, old_event, current_event]:
        _touch_file(path)
    old_ts = (datetime.now() - timedelta(days=31)).timestamp()
    fresh_ts = (datetime.now() - timedelta(hours=4)).timestamp()
    for path in [old_trace, old_event]:
        os.utime(path, (old_ts, old_ts))
    for path in [fresh_trace, current_trace, current_event]:
        os.utime(path, (fresh_ts, fresh_ts))
    _only_categories(
        monkeypatch,
        tmp_path,
        [
            CleanupCategory(
                name="factor_research_trace_history",
                root=trace_root / "history",
                retention_key="factor_research_trace_history",
                risk="low",
                profiles=("safe",),
                description="trace history",
            ),
            CleanupCategory(
                name="factor_research_event_history",
                root=event_root / "history",
                retention_key="factor_research_event_history",
                risk="low",
                profiles=("safe",),
                description="event history",
            ),
        ],
    )

    candidates = cleanup.build_cleanup_candidates(profile="safe")
    names = {item.path.split("/")[-1] for item in candidates if item.executable}
    paths = {item.path for item in candidates}

    assert names == {"old.jsonl"}
    assert str(current_trace) not in paths
    assert str(current_event) not in paths
    assert cleanup.DEFAULT_RETENTION_DAYS["factor_research_trace_history"] == 30
    assert cleanup.DEFAULT_RETENTION_DAYS["factor_research_event_history"] == 30


def test_retired_factor_values_protects_active_manifest_paths(monkeypatch, tmp_path):
    parquet_root = tmp_path / "data" / "factors" / "parquet"
    active_path = parquet_root / "factor_active_collision.parquet"
    retired_path = parquet_root / "factor_retired_only.parquet"
    _touch_file(active_path)
    _touch_file(retired_path)
    manifest = tmp_path / "data" / "factors" / "active_adopted_factor_values.manifest.json"
    _touch_file(
        manifest,
        json.dumps({"factor_records": [{"factor_id": "f_active", "data_path": str(active_path)}]}),
    )
    registry_db = tmp_path / "data" / "factors" / "factor_registry.db"
    registry_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(registry_db)
    conn.execute("CREATE TABLE factors (factor_id TEXT, status TEXT, metadata TEXT)")
    conn.execute(
        "INSERT INTO factors VALUES (?,?,?)",
        ("f_retired_collision", "retired", json.dumps({"data_path": str(active_path)})),
    )
    conn.execute(
        "INSERT INTO factors VALUES (?,?,?)",
        ("f_retired_only", "retired", json.dumps({"data_path": str(retired_path)})),
    )
    conn.commit()
    conn.close()
    _only_categories(
        monkeypatch,
        tmp_path,
        [
            CleanupCategory(
                name="retired_factor_values",
                root=parquet_root,
                retention_key="retired_factor_values",
                risk="medium",
                profiles=("safe",),
                description="retired",
            )
        ],
    )
    monkeypatch.setattr(cleanup, "FACTOR_PARQUET_DIR", parquet_root)
    monkeypatch.setattr(cleanup, "FACTOR_REGISTRY_DB", registry_db)
    monkeypatch.setattr(cleanup, "FACTOR_ACTIVE_ADOPTED_VALUES_MANIFEST", manifest)

    candidates = cleanup.build_cleanup_candidates(profile="safe")
    by_name = {item.path.split("/")[-1]: item for item in candidates}

    assert by_name[active_path.name].executable is False
    assert by_name[active_path.name].blocked_reason == "active_manifest_factor_value"
    assert by_name[retired_path.name].executable is True


def test_retention_days_json_parser_accepts_valid_and_rejects_invalid():
    from mcp_servers.platform_server import _parse_retention_days_json

    parsed, error = _parse_retention_days_json('{"pickle_cache": 1, "logs": "3"}', profile="safe", execute=False)
    assert parsed == {"pickle_cache": 1, "logs": 3}
    assert error is None

    parsed, error = _parse_retention_days_json("{bad json", profile="safe", execute=False)
    assert parsed is None
    assert error is not None
    assert error.err == "invalid_retention_days_json"


def test_runtime_test_tmp_collects_broken_pytest_current_symlink(monkeypatch, tmp_path):
    runtime_root = tmp_path / "runtime"
    session_root = runtime_root / "test-tmp" / "pytest-of-roy"
    session_root.mkdir(parents=True)
    current = session_root / "pytest-current"
    current.symlink_to(session_root / "pytest-99")
    monkeypatch.setattr(cleanup, "RUNTIME_ROOT", runtime_root)

    candidates = cleanup._collect_runtime_test_tmp(profile="safe", now=datetime.now(), days=1)

    assert len(candidates) == 1
    assert candidates[0].path == str(current)
    assert candidates[0].executable is True
