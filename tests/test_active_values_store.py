from __future__ import annotations

import json

import pandas as pd
import pytest

from domain.factor_research import active_values_store as store
from domain.factor_research import active_values_tail_refresh as tail_refresh
from domain.model import feature_set_builder
from services import factor_active_values_service as active_values_service


@pytest.fixture(autouse=True)
def _active_values_jobs_db_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(active_values_service, "ACTIVE_VALUES_REFRESH_JOBS_DB", tmp_path / "jobs.sqlite")
    monkeypatch.setattr(store, "get_live_factor_value_default_start_date", lambda: "2026-01-02")
    monkeypatch.setattr(store, "get_live_factor_value_default_end_date", lambda: "2026-01-03")


def _factor_parquet(path, column: str, value: float = 1.0):
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2026-01-02"), "600000sh"),
            (pd.Timestamp("2026-01-03"), "600000sh"),
            (pd.Timestamp("2026-01-02"), "000001sz"),
        ],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame({("feature", column): [value, value + 1, value + 2]}, index=index)
    df.to_parquet(path)


class _Registry:
    def __init__(self, rows):
        self.rows = rows

    def list_active(self, min_icir=-1e9, holding_period_days=None):
        return list(self.rows)


def _row(tmp_path, factor_id: str, expression: str, data_column: str):
    path = tmp_path / f"{factor_id}.parquet"
    _factor_parquet(path, data_column)
    return {
        "factor_id": factor_id,
        "name": factor_id,
        "expression": expression,
        "holding_period_days": 5,
        "metadata": {"data_path": str(path), "data_column": data_column},
    }


def test_active_values_store_builds_active_only_wide_store_and_manifest(tmp_path):
    rows = [
        _row(tmp_path, "f1", "rank(close)", "QGF_CloseRank_00"),
        _row(tmp_path, "f2", "rank(amount)", "QGF_AmountRank_00"),
    ]
    output = tmp_path / "active.parquet"
    manifest_path = tmp_path / "active.manifest.json"
    manifest = store.build_active_values_store(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry(rows),
        sync_quantgpt=True,
        run_id="test",
        source_mode="parquet",
        end_date="2026-01-03",
    )

    wide = pd.read_parquet(output)
    assert manifest["factor_count"] == 2
    assert manifest["column_count"] == 2
    assert manifest["resolved_universe"] == "tradable_non_st"
    assert manifest["filter_non_st_before_expression"] is False
    assert manifest["compute_semantics_version"] == store.FACTOR_COMPUTE_SEMANTICS_VERSION
    assert manifest["quantgpt_path"] == str(output)
    assert manifest["quantgpt_sync_mode"] == "shared_canonical_path"
    assert list(wide.columns) == ["rank(close)", "rank(amount)"]
    assert wide.index.names == ["stock_code", "trade_date"]
    assert manifest_path.exists()


def test_active_values_store_trims_per_factor_tail_to_resolved_value_window(tmp_path):
    row = _row(tmp_path, "f1", "rank(close)", "QGF_CloseRank_00")
    path = row["metadata"]["data_path"]
    frame = pd.read_parquet(path)
    future = frame.iloc[[0]].copy()
    future.index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-01-04"), "600000sh")],
        names=["datetime", "instrument"],
    )
    pd.concat([frame, future]).sort_index().to_parquet(path)

    output = tmp_path / "active.parquet"
    manifest = store.build_active_values_store(
        output_path=output,
        manifest_path=tmp_path / "active.manifest.json",
        registry=_Registry([row]),
        sync_quantgpt=False,
        run_id="test-window-trim",
        source_mode="parquet",
        start_date="2026-01-02",
        end_date="2026-01-03",
    )

    wide = pd.read_parquet(output)
    assert wide.index.get_level_values("trade_date").max() == pd.Timestamp("2026-01-03")
    assert manifest["actual_end_date"] == "2026-01-03"
    assert manifest["output_window_enforced"] is True


def test_compute_semantics_version_includes_missing_policy():
    from domain.factor_research import factor_compute

    factor_compute._ensure_quantgpt_import_path()
    from quantgpt.expression_parser import SEMANTIC_MISSING_POLICY_VERSION
    from quantgpt.factor_evaluator import FACTOR_COMPUTE_SEMANTICS_VERSION as quantgpt_semantics

    assert SEMANTIC_MISSING_POLICY_VERSION in factor_compute.FACTOR_COMPUTE_SEMANTICS_VERSION
    assert factor_compute.FACTOR_COMPUTE_SEMANTICS_VERSION == quantgpt_semantics
    assert store.FACTOR_COMPUTE_SEMANTICS_VERSION == quantgpt_semantics


def test_active_values_store_summary_marks_stale_when_fingerprint_changes(tmp_path):
    row = _row(tmp_path, "f1", "rank(close)", "QGF_CloseRank_00")
    output = tmp_path / "active.parquet"
    manifest_path = tmp_path / "active.manifest.json"
    store.build_active_values_store(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry([row]),
        sync_quantgpt=False,
        run_id="test",
        source_mode="parquet",
        end_date="2026-01-03",
    )

    changed = dict(row)
    changed["expression"] = "rank(open)"
    summary = store.active_values_store_summary(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry([changed]),
    )

    assert summary["exists"] is True
    assert summary["stale"] is True
    assert summary["active_values_status"] == "stale"
    assert summary["safe_to_freeze_feature_set"] is False
    assert summary["model_snapshot_refresh_required"] is True
    assert summary["built_registry_fingerprint"]
    assert summary["requested_registry_fingerprint"] == summary["current_registry_fingerprint"]
    assert summary["current_registry_fingerprint"] != summary["built_registry_fingerprint"]
    assert "active values stale because registry changed from" in summary["stale_message"]


def test_active_values_store_summary_reports_source_data_change_without_stale(tmp_path, monkeypatch):
    row = _row(tmp_path, "f1", "rank(close)", "QGF_CloseRank_00")
    output = tmp_path / "active.parquet"
    manifest_path = tmp_path / "active.manifest.json"

    monkeypatch.setattr(
        store,
        "quantgpt_stock_cache_signature",
        lambda: {
            "path": str(tmp_path / "stocks"),
            "exists": True,
            "file_count": 1,
            "total_size": 10,
            "max_mtime_ns": 100,
            "fingerprint": "source-old",
        },
    )
    store.build_active_values_store(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry([row]),
        sync_quantgpt=False,
        run_id="test",
        source_mode="parquet",
        end_date="2026-01-03",
    )

    monkeypatch.setattr(
        store,
        "quantgpt_stock_cache_signature",
        lambda: {
            "path": str(tmp_path / "stocks"),
            "exists": True,
            "file_count": 1,
            "total_size": 11,
            "max_mtime_ns": 200,
            "fingerprint": "source-new",
        },
    )
    summary = store.active_values_store_summary(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry([row]),
    )

    assert summary["exists"] is True
    assert summary["source_data_fingerprint"] == "source-old"
    assert summary["current_source_data_fingerprint"] == "source-new"
    assert summary["source_data_mismatch"] is True
    assert summary["stale"] is False


def test_active_values_store_summary_reports_untracked_source_data_without_stale(tmp_path, monkeypatch):
    row = _row(tmp_path, "f1", "rank(close)", "QGF_CloseRank_00")
    output = tmp_path / "active.parquet"
    manifest_path = tmp_path / "active.manifest.json"

    monkeypatch.setattr(
        store,
        "quantgpt_stock_cache_signature",
        lambda: {
            "path": str(tmp_path / "stocks"),
            "exists": True,
            "file_count": 1,
            "total_size": 10,
            "max_mtime_ns": 100,
            "fingerprint": "source-current",
        },
    )
    store.build_active_values_store(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry([row]),
        sync_quantgpt=False,
        run_id="test",
        source_mode="parquet",
        end_date="2026-01-03",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("source_data_fingerprint", None)
    manifest.pop("source_data_signature", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    summary = store.active_values_store_summary(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry([row]),
    )

    assert summary["source_data_fingerprint"] == ""
    assert summary["current_source_data_fingerprint"] == "source-current"
    assert summary["source_data_untracked"] is True
    assert summary["source_data_mismatch"] is False
    assert summary["stale"] is False


def test_active_values_store_summary_marks_stale_when_parquet_missing(tmp_path):
    row = _row(tmp_path, "f1", "rank(close)", "QGF_CloseRank_00")
    output = tmp_path / "active.parquet"
    manifest_path = tmp_path / "active.manifest.json"
    store.build_active_values_store(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry([row]),
        sync_quantgpt=False,
        run_id="test",
        source_mode="parquet",
        end_date="2026-01-03",
    )
    output.unlink()

    summary = store.active_values_store_summary(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry([row]),
    )

    assert summary["exists"] is False
    assert summary["manifest_exists"] is True
    assert summary["stale"] is True


def test_active_values_store_summary_marks_stale_when_actual_parquet_end_is_short(tmp_path, monkeypatch):
    row = _row(tmp_path, "f1", "rank(close)", "QGF_CloseRank_00")
    output = tmp_path / "active.parquet"
    manifest_path = tmp_path / "active.manifest.json"
    store.build_active_values_store(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry([row]),
        sync_quantgpt=False,
        run_id="test",
        source_mode="parquet",
        end_date="2026-01-03",
    )
    monkeypatch.setattr(store, "get_live_factor_value_default_end_date", lambda: "2026-01-04")

    summary = store.active_values_store_summary(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry([row]),
    )

    assert summary["stale"] is True
    assert "active_values_actual_end_date_short" in summary["stale_reasons"]
    assert summary["actual_end_date"] == "2026-01-03"
    assert "run source_mode=tail" in summary["stale_message"]


def test_active_values_store_blocks_duplicate_active_data_column(tmp_path):
    rows = [
        _row(tmp_path, "f1", "rank(close)", "QGF_Dupe_00"),
        _row(tmp_path, "f2", "rank(amount)", "QGF_Dupe_00"),
    ]

    try:
        store.build_active_values_store(
            output_path=tmp_path / "active.parquet",
            manifest_path=tmp_path / "active.manifest.json",
            registry=_Registry(rows),
            sync_quantgpt=False,
            run_id="test",
            source_mode="parquet",
            end_date="2026-01-03",
        )
    except RuntimeError as exc:
        assert "duplicate_data_columns" in str(exc)
    else:
        raise AssertionError("duplicate active data_column should block active-only rebuild")


def test_active_values_store_computes_with_full_history_before_output_filter(tmp_path, monkeypatch):
    row = _row(tmp_path, "f1", "rank(close)", "QGF_CloseRank_00")
    output = tmp_path / "active.parquet"
    manifest_path = tmp_path / "active.manifest.json"
    calls = {}

    market = pd.DataFrame(
        {
            "trade_date": [pd.Timestamp("2026-05-29"), pd.Timestamp("2026-05-29")],
            "stock_code": ["sh.600000", "sz.000001"],
            "close": [10.0, 20.0],
        }
    )

    def fake_load_market_data(**kwargs):
        calls["filter_non_st"] = kwargs.get("filter_non_st")
        calls["start_date"] = kwargs.get("start_date")
        calls["end_date"] = kwargs.get("end_date")
        return market.copy()

    def fake_compute_factor_from_market_df(market_df, expression):
        calls["compute_rows"] = len(market_df)
        out = market_df[["trade_date", "stock_code"]].copy()
        out["datetime"] = pd.to_datetime(out["trade_date"])
        out["instrument"] = out["stock_code"].str.split(".").str[1] + out["stock_code"].str.split(".").str[0]
        out[expression[:40]] = [0.25, 0.75]
        return out.set_index(["datetime", "instrument"])[[expression[:40]]].sort_index()

    monkeypatch.setattr(store, "_required_market_columns", lambda expressions: {"trade_date", "stock_code", "close"})
    monkeypatch.setattr(store, "_load_market_data", fake_load_market_data)
    monkeypatch.setattr(store, "_compute_factor_from_market_df", fake_compute_factor_from_market_df)

    manifest = store.build_active_values_store(
        output_path=output,
        manifest_path=manifest_path,
        registry=_Registry([row]),
        sync_quantgpt=False,
        run_id="test",
        start_date="2026-05-29",
        end_date="2026-05-29",
        universe="tradable_non_st",
        source_mode="compute",
    )

    wide = pd.read_parquet(output)
    factor_df = pd.read_parquet(row["metadata"]["data_path"])
    assert calls["filter_non_st"] is False
    assert calls["compute_rows"] == 2
    assert manifest["source_mode"] == "compute"
    assert manifest["resolved_universe"] == "tradable_non_st"
    assert manifest["value_start_date"] == "2026-05-29"
    assert manifest["value_end_date"] == "2026-05-29"
    assert manifest["filter_non_st_before_expression"] is False
    assert manifest["compute_semantics_version"] == store.FACTOR_COMPUTE_SEMANTICS_VERSION
    assert list(wide.columns) == ["rank(close)"]
    assert factor_df.columns.tolist() == [("feature", "QGF_CloseRank_00")]


def test_active_values_store_records_anchor_when_name_matches_but_column_is_expression(tmp_path, monkeypatch):
    row = _row(tmp_path, "f1", "rank(close)", "QGF_CloseRank_00")
    row["name"] = "AnchorFactorName"

    monkeypatch.setattr(
        store,
        "ACTIVE_VALUE_AUDIT_ANCHOR",
        {
            "factor_expression": "AnchorFactorName",
            "trade_date": "2026-01-02",
            "stock_code": "sh.600000",
            "round_digits": 6,
        },
    )

    manifest = store.build_active_values_store(
        output_path=tmp_path / "active.parquet",
        manifest_path=tmp_path / "active.manifest.json",
        registry=_Registry([row]),
        sync_quantgpt=False,
        run_id="test",
        source_mode="parquet",
        end_date="2026-01-03",
    )

    assert manifest["audit_anchor"]["matched_expression"] == "rank(close)"
    assert manifest["audit_anchor"]["stored_value"] == 1.0
    assert manifest["audit_anchor"]["passed"] is True


def test_active_values_service_state_write_is_atomic_json(tmp_path, monkeypatch):
    state_file = tmp_path / "latest_status.json"
    monkeypatch.setattr(active_values_service, "ACTIVE_VALUES_REFRESH_STATUS_FILE", state_file)

    active_values_service._write_state({"status": "running", "active_values_refresh_required": True})
    active_values_service._write_state({"status": "completed", "active_values_refresh_required": False})

    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "status": "completed",
        "active_values_refresh_required": False,
    }
    assert not list(tmp_path.glob("*.tmp.*"))


def test_active_values_refresh_marks_model_refresh_required_without_freezing_snapshot(tmp_path, monkeypatch):
    state_file = tmp_path / "latest_status.json"
    monkeypatch.setattr(active_values_service, "ACTIVE_VALUES_REFRESH_STATUS_FILE", state_file)
    monkeypatch.setattr(
        active_values_service,
        "build_active_values_store",
        lambda holding_period_days=None, source_mode="parquet": {
            "registry_fingerprint": "fp-current",
            "factor_count": 2,
            "source_mode": source_mode,
        },
    )
    active_values_service._run_refresh(
        holding_period_days=5,
        trigger="test",
        refresh_model=True,
        registry_fingerprint="fp-current",
        source_mode="parquet",
    )

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert state["source_mode"] == "parquet"
    assert state["model_refresh"]["status"] == "refresh_required"
    assert state["model_refresh"]["reason"] == "active_values_refreshed_without_freezing_model_feature_set"
    assert state["model_refresh_required"] is True
    assert state["model_snapshot_refresh_required"] is True
    assert state["active_values_refresh_required"] is False


def test_active_values_refresh_uses_parquet_by_default(tmp_path, monkeypatch):
    state_file = tmp_path / "latest_status.json"
    calls = []
    monkeypatch.setattr(active_values_service, "ACTIVE_VALUES_REFRESH_STATUS_FILE", state_file)
    monkeypatch.setattr(
        active_values_service,
        "current_active_registry_fingerprint",
        lambda holding_period_days=None: ("fp-current", []),
    )

    def fake_build(holding_period_days=None, source_mode="parquet"):
        calls.append(source_mode)
        return {"registry_fingerprint": "fp-current", "factor_count": 2}

    monkeypatch.setattr(active_values_service, "build_active_values_store", fake_build)

    active_values_service._run_refresh(
        holding_period_days=5,
        trigger="test",
        refresh_model=False,
        registry_fingerprint="fp-current",
        source_mode="parquet",
    )

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert calls == ["parquet"]
    assert state["status"] == "completed"
    assert state["source_mode"] == "parquet"


def test_active_values_refresh_supports_tail_source_mode(tmp_path, monkeypatch):
    state_file = tmp_path / "latest_status.json"
    calls = []
    monkeypatch.setattr(active_values_service, "ACTIVE_VALUES_REFRESH_STATUS_FILE", state_file)
    monkeypatch.setattr(
        active_values_service,
        "current_active_registry_fingerprint",
        lambda holding_period_days=None: ("fp-tail", []),
    )

    def fake_tail(holding_period_days=None, run_id=None):
        calls.append({"holding_period_days": holding_period_days, "run_id": run_id})
        return {
            "status": "completed",
            "active_values_manifest": {
                "registry_fingerprint": "fp-tail",
                "factor_count": 2,
                "source_mode": "parquet",
                "actual_end_date": "2026-01-03",
            },
        }

    monkeypatch.setattr(active_values_service, "refresh_active_values_tail", fake_tail)

    active_values_service._run_refresh(
        holding_period_days=5,
        trigger="test",
        refresh_model=False,
        registry_fingerprint="fp-tail",
        source_mode="tail",
    )

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert calls[0]["holding_period_days"] == 5
    assert state["status"] == "completed"
    assert state["source_mode"] == "tail"
    assert state["registry_fingerprint"] == "fp-tail"


def test_tail_refresh_rebuilds_wide_store_when_registry_changed_without_date_tail(tmp_path, monkeypatch):
    target = pd.Timestamp("2026-01-03")
    factor_path = tmp_path / "factor.parquet"
    monkeypatch.setattr(tail_refresh, "FACTOR_ACTIVE_ADOPTED_VALUES_FILE", tmp_path / "active.parquet")
    monkeypatch.setattr(
        tail_refresh,
        "resolve_active_values_lineage",
        lambda start_date=None, end_date=None: {"value_start_date": "2026-01-02", "value_end_date": "2026-01-03"},
    )
    monkeypatch.setattr(
        tail_refresh,
        "parquet_index_date_bounds",
        lambda path: (pd.Timestamp("2026-01-02"), target, 3, 2),
    )
    monkeypatch.setattr(
        tail_refresh,
        "current_active_registry_fingerprint",
        lambda holding_period_days=None, end_date=None: (
            "fp-new",
            [{"factor_id": "f-new", "expression": "rank(close)", "data_path": str(factor_path), "data_column": "QGF_New"}],
        ),
    )
    monkeypatch.setattr(tail_refresh, "calendar_dates", lambda start_date, end_date: [target])
    monkeypatch.setattr(
        tail_refresh,
        "load_active_values_manifest",
        lambda: {"registry_fingerprint": "fp-old", "factor_count": 0},
    )
    builds = []

    def fake_build(**kwargs):
        builds.append(kwargs)
        return {"registry_fingerprint": "fp-new", "factor_count": 1}

    monkeypatch.setattr(tail_refresh, "build_active_values_store", fake_build)

    result = tail_refresh.refresh_active_values_tail(end_date="2026-01-03", run_id="test")

    assert result["status"] == "already_current_rebuilt"
    assert result["refresh_factor_count"] == 0
    assert len(builds) == 1
    assert builds[0]["source_mode"] == "parquet"


def test_active_values_status_does_not_misreport_restart_after_completed_manifest(tmp_path, monkeypatch):
    state_file = tmp_path / "latest_status.json"
    monkeypatch.setattr(active_values_service, "ACTIVE_VALUES_REFRESH_STATUS_FILE", state_file)
    state_file.write_text(
        json.dumps(
            {
                "status": "running",
                "requested_registry_fingerprint": "fp-current",
                "active_values_refresh_required": True,
                "model_refresh_required": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        active_values_service,
        "active_values_store_summary",
        lambda holding_period_days=None: {
            "stale": False,
            "manifest_registry_fingerprint": "fp-current",
            "factor_count": 2,
            "column_count": 2,
            "last_error": "",
        },
    )
    monkeypatch.setattr(
        feature_set_builder,
        "active_feature_snapshot_staleness",
        lambda holding_period_days=5: {"stale": False, "stale_reason": ""},
    )

    result = active_values_service.factor_active_values_status()

    assert result.ok
    assert result.outputs["refresh_status"] == "completed_after_restart"
    assert result.outputs["active_values_refresh_required"] is False
    assert result.outputs["model_snapshot_refresh_marked"] is True
    assert result.outputs["model_snapshot_currently_stale"] is False
    assert result.outputs["model_snapshot_refresh_required"] is False
    assert result.outputs["model_snapshot_trigger"] == "model_side"


def test_active_values_status_reports_current_model_snapshot_stale(tmp_path, monkeypatch):
    state_file = tmp_path / "latest_status.json"
    monkeypatch.setattr(active_values_service, "ACTIVE_VALUES_REFRESH_STATUS_FILE", state_file)
    state_file.write_text(
        json.dumps(
            {
                "status": "completed",
                "active_values_refresh_required": False,
                "model_snapshot_refresh_required": True,
                "model_refresh_required": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        active_values_service,
        "active_values_store_summary",
        lambda holding_period_days=None: {
            "stale": False,
            "manifest_registry_fingerprint": "fp-current",
            "factor_count": 2,
            "column_count": 2,
            "last_error": "",
        },
    )
    monkeypatch.setattr(
        feature_set_builder,
        "active_feature_snapshot_staleness",
        lambda holding_period_days=5: {
            "stale": True,
            "stale_reason": "active_values_lineage_mismatch",
            "required_action": "fxalpha_model_feature_snapshot",
        },
    )

    result = active_values_service.factor_active_values_status()

    assert result.ok
    assert result.outputs["active_values_refresh_required"] is False
    assert result.outputs["model_snapshot_refresh_marked"] is True
    assert result.outputs["model_snapshot_currently_stale"] is True
    assert result.outputs["model_snapshot_refresh_required"] is True
    assert result.outputs["model_refresh_required"] is True
    assert result.outputs["model_snapshot_staleness"]["required_action"] == "fxalpha_model_feature_snapshot"


def test_active_values_refresh_rebuilds_once_if_registry_changes_during_build(tmp_path, monkeypatch):
    state_file = tmp_path / "latest_status.json"
    calls = []
    monkeypatch.setattr(active_values_service, "ACTIVE_VALUES_REFRESH_STATUS_FILE", state_file)

    def fake_build(holding_period_days=None, source_mode="parquet"):
        calls.append({"holding_period_days": holding_period_days, "source_mode": source_mode})
        fingerprint = "fp-old" if len(calls) == 1 else "fp-new"
        return {"registry_fingerprint": fingerprint, "factor_count": 2}

    monkeypatch.setattr(active_values_service, "build_active_values_store", fake_build)
    monkeypatch.setattr(
        active_values_service,
        "current_active_registry_fingerprint",
        lambda holding_period_days=None: ("fp-new", []),
    )

    active_values_service._run_refresh(
        holding_period_days=5,
        trigger="test",
        refresh_model=False,
        registry_fingerprint="fp-old",
        source_mode="parquet",
    )

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(calls) == 2
    assert all(call["source_mode"] == "parquet" for call in calls)
    assert state["status"] == "completed"
    assert state["registry_fingerprint"] == "fp-new"


def test_active_values_refresh_dry_run_does_not_queue_or_write_state(tmp_path, monkeypatch):
    state_file = tmp_path / "latest_status.json"
    monkeypatch.setattr(active_values_service, "ACTIVE_VALUES_REFRESH_STATUS_FILE", state_file)
    monkeypatch.setattr(
        active_values_service,
        "current_active_registry_fingerprint",
        lambda holding_period_days=None: ("fp-current", []),
    )
    monkeypatch.setattr(
        active_values_service,
        "active_values_store_summary",
        lambda holding_period_days=None: {
            "stale": True,
            "manifest_registry_fingerprint": "fp-old",
            "resolved_universe": "tradable_non_st",
        },
    )

    state = active_values_service.enqueue_active_values_refresh(
        holding_period_days=5,
        trigger="test",
        refresh_model=True,
        dry_run=True,
    )

    assert state["status"] == "dry_run"
    assert state["would_queue"] is True
    assert state["requested_registry_fingerprint"] == "fp-current"
    assert state["model_snapshot_refresh_required"] is True
    assert state["model_snapshot_trigger"] == "model_side"
    assert state["source_mode"] == "tail"
    assert not state_file.exists()
