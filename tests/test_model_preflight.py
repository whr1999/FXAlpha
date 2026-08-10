from __future__ import annotations

import pandas as pd

from domain.model.preflight import model_preflight


def test_preflight_blocks_all_active_when_active_values_stale(monkeypatch):
    monkeypatch.setattr(
        "domain.model.preflight.current_active_registry_fingerprint",
        lambda holding_period_days=5: ("fp-new", [{"factor_id": "f1"}, {"factor_id": "f2"}]),
    )
    monkeypatch.setattr(
        "domain.model.preflight.active_values_readiness",
        lambda factor_holding_period_days=5: {
            "active_values_status": "stale",
            "safe_to_freeze_feature_set": False,
            "requested_registry_fingerprint": "fp-old",
            "built_registry_fingerprint": "fp-old",
            "manifest_registry_fingerprint": "fp-old",
            "feature_snapshot_block_reason": "active values stale because registry changed from fp-old to fp-new",
            "model_snapshot_refresh_required": True,
        },
    )
    monkeypatch.setattr(
        "domain.model.preflight._qlib_data_availability",
        lambda: {"passed": True, "missing": []},
    )

    result = model_preflight(all_active=True)

    assert result["passed"] is False
    assert result["stage"] == "feature_snapshot_preflight"
    assert result["active_factor_count"] == 2
    assert result["fingerprint_match"] is False
    assert result["safe_to_freeze_feature_set"] is False
    assert result["stale_reason"] == "active values stale because registry changed from fp-old to fp-new"
    assert result["blocker"]["category"] == "external_data_blocker"
    assert result["blocker"]["resume_from"] == "feature_snapshot_preflight"


def test_preflight_passes_ready_all_active_before_orch(monkeypatch):
    monkeypatch.setattr(
        "domain.model.preflight.current_active_registry_fingerprint",
        lambda holding_period_days=5: ("fp-ok", [{"factor_id": "f1"}]),
    )
    monkeypatch.setattr(
        "domain.model.preflight.active_values_readiness",
        lambda factor_holding_period_days=5: {
            "active_values_status": "ready",
            "safe_to_freeze_feature_set": True,
            "requested_registry_fingerprint": "fp-ok",
            "built_registry_fingerprint": "fp-ok",
            "manifest_registry_fingerprint": "fp-ok",
        },
    )
    monkeypatch.setattr(
        "domain.model.preflight._qlib_data_availability",
        lambda: {"passed": True, "missing": []},
    )

    result = model_preflight(all_active=True)

    assert result["passed"] is True
    assert result["fingerprint_match"] is True
    assert result["safe_to_freeze_feature_set"] is True
    assert result["errors"] == []


def test_preflight_blocks_feature_set_missing_label0_column(tmp_path, monkeypatch):
    feature_dir = tmp_path / "feature_sets" / "fs-bad-label"
    feature_dir.mkdir(parents=True)
    combined = feature_dir / "combined_factors_df.parquet"
    index = pd.MultiIndex.from_product(
        [pd.date_range("2026-01-01", periods=2), ["000001sz"]],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame({("feature", "f1"): [1.0, 2.0]}, index=index)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    df.to_parquet(combined)
    manifest = {
        "feature_set_id": "fs-bad-label",
        "feature_snapshot_policy_version": "qlib_feature_missing_v8_static_universe_labels",
        "feature_missing_strategy": "qlib_processor_only",
        "factor_selection_mode": "explicit_factor_ids_from_source_snapshot",
        "combined_factors_file": str(combined),
        "factor_count": 1,
        "feature_count": 1,
        "label_forward_period": 5,
        "factor_holding_period_days": 5,
        "label_price_mode": "qlib_calendar_adjusted_next_open_to_forward_open_from_quantgpt_adjusted_open",
        "label_source_price_field": "open",
        "label_entry_shift_days": 1,
        "label_exit_shift_days": 6,
        "label_execution_deal_price": "open",
        "label_return_mode": "next_open_to_forward_open",
        "label_uses_adjusted_price": True,
    }
    monkeypatch.setattr("domain.model.preflight.current_active_registry_fingerprint", lambda holding_period_days=5: ("fp-ok", []))
    monkeypatch.setattr("domain.model.preflight.active_values_readiness", lambda factor_holding_period_days=5: {"safe_to_freeze_feature_set": True, "manifest_registry_fingerprint": "fp-ok"})
    monkeypatch.setattr("domain.model.preflight._qlib_data_availability", lambda: {"passed": True, "missing": []})
    monkeypatch.setattr("domain.model.preflight.load_feature_set_manifest", lambda feature_set_id: manifest)

    result = model_preflight(feature_set_id="fs-bad-label", all_active=False)

    assert result["passed"] is False
    assert "label0_contract_failed" in result["errors"]
    assert result["label0_contract"]["label_column_present"] is False
