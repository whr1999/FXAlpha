from __future__ import annotations

import json

import pandas as pd

import domain.model.feature_set_builder as feature_set_builder
from domain.model.feature_set_builder import _calendar_aligned_label_frame
from domain.model.feature_sets import active_values_readiness, feature_snapshot, validate_feature_set_manifest_for_model


def test_feature_snapshot_dry_run_preserves_missing_policy_and_label_contract():
    result = feature_snapshot(
        feature_set_id="fs-dry",
        factor_ids=["f1", "f2"],
        feature_missing_strategy="qlib_processor_only",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["mode"] == "dry_run"
    assert result["request"]["feature_missing_strategy"] == "qlib_processor_only"
    assert result["request"]["label_forward_period"] == 5
    assert result["request"]["factor_holding_period_days"] == 5


def test_model_active_values_readiness_is_check_only(monkeypatch):
    monkeypatch.setattr(
        "domain.model.feature_sets.active_values_store_summary",
        lambda holding_period_days=5: {
            "stale": False,
            "registry_fingerprint": "fp-current",
            "manifest_registry_fingerprint": "fp-current",
            "active_count": 56,
            "factor_count": 56,
            "column_count": 56,
            "resolved_universe": "tradable_non_st",
            "path": "/tmp/active.parquet",
            "manifest_path": "/tmp/active.manifest.json",
            "stale_reasons": [],
        },
    )

    readiness = active_values_readiness()

    assert readiness["safe_to_freeze_feature_set"] is True
    assert readiness["required_action"] == "none"
    assert readiness["refresh_source_mode_default"] == "tail"
    assert readiness["model_computes_factor_values"] is False


def test_all_active_feature_snapshot_blocks_when_active_values_stale(monkeypatch):
    monkeypatch.setattr(
        "domain.model.feature_sets.active_values_store_summary",
        lambda holding_period_days=5: {
            "stale": True,
            "stale_reason": "registry_fingerprint_mismatch",
            "stale_reasons": ["registry_fingerprint_mismatch"],
            "registry_fingerprint": "fp-new",
            "manifest_registry_fingerprint": "fp-old",
            "active_count": 56,
            "factor_count": 54,
        },
    )
    monkeypatch.setattr(
        "domain.model.feature_sets.build_active_feature_set",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("model must not build when active values are stale")),
    )

    import pytest

    with pytest.raises(RuntimeError, match="active values are not ready"):
        feature_snapshot(feature_set_id="fs-blocked-all-active")


def test_all_active_feature_snapshot_builds_only_after_active_values_ready(monkeypatch, tmp_path):
    combined = tmp_path / "combined.parquet"
    pd.DataFrame({("feature", "f1"): [1.0], ("label", "LABEL0"): [0.1]}).to_parquet(combined)
    monkeypatch.setattr(
        "domain.model.feature_sets.active_values_store_summary",
        lambda holding_period_days=5: {
            "stale": False,
            "registry_fingerprint": "fp-current",
            "manifest_registry_fingerprint": "fp-current",
            "active_count": 1,
            "factor_count": 1,
            "column_count": 1,
            "resolved_universe": "tradable_non_st",
            "path": "/tmp/active.parquet",
            "manifest_path": "/tmp/active.manifest.json",
            "stale_reasons": [],
        },
    )
    monkeypatch.setattr(
        "domain.model.feature_sets._build_all_active_snapshot_from_active_values",
        lambda **kwargs: {
            "feature_set_id": kwargs["feature_set_id"],
            "feature_snapshot_policy_version": "qlib_feature_missing_v8_static_universe_labels",
            "feature_missing_strategy": "qlib_processor_only",
            "label_forward_period": 5,
            "factor_holding_period_days": 5,
            "label_price_mode": "qlib_calendar_adjusted_next_open_to_forward_open_from_quantgpt_adjusted_open",
            "label_source_price_field": "open",
            "label_entry_shift_days": 1,
            "label_exit_shift_days": 6,
            "label_execution_deal_price": "open",
            "label_return_mode": "next_open_to_forward_open",
            "label_uses_adjusted_price": True,
            "combined_factors_file": str(combined),
            "factor_count": 1,
            "feature_count": 1,
        },
    )

    result = feature_snapshot(feature_set_id="fs-ready-all-active")

    assert result["ok"] is True
    assert result["mode"] == "built_snapshot"
    assert result["active_values_readiness"]["safe_to_freeze_feature_set"] is True
    assert result["active_values_readiness"]["model_computes_factor_values"] is False
    assert result["validation"]["passed"] is True


def test_all_active_feature_snapshot_uses_active_values_wide_table(monkeypatch, tmp_path):
    root = tmp_path / "feature_sets"
    active_dir = tmp_path / "active_pointer"
    active_values_path = tmp_path / "active_values.parquet"
    active_values_manifest_path = tmp_path / "active_values.manifest.json"
    monkeypatch.setattr("domain.model.feature_sets.MODEL_FEATURE_SETS_ROOT", root)
    monkeypatch.setattr("domain.model.feature_sets.MODEL_ACTIVE_FEATURE_DIR", active_dir)
    monkeypatch.setattr("domain.model.feature_sets.MODEL_ACTIVE_FEATURE_FILE", active_dir / "combined_factors_df.parquet")
    monkeypatch.setattr("domain.model.feature_sets.MODEL_ACTIVE_FEATURE_MANIFEST", active_dir / "manifest.json")
    monkeypatch.setattr("domain.model.feature_sets.ACTIVE_MODEL_FEATURE_SET_FILE", tmp_path / "active_feature_set.json")
    index = pd.MultiIndex.from_tuples(
        [
            ("sh.600000", pd.Timestamp("2026-01-02")),
            ("sh.600000", pd.Timestamp("2026-01-03")),
            ("sz.000001", pd.Timestamp("2026-01-02")),
        ],
        names=["stock_code", "trade_date"],
    )
    pd.DataFrame({"expr_a": [1.0, 2.0, 3.0]}, index=index).to_parquet(active_values_path)
    active_values_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "active_adopted_factor_values_v3_static_non_st",
                "generated_at": "2026-07-04T10:00:00",
                "source_mode": "parquet",
                "resolved_universe": "tradable_non_st",
                "value_start_date": "2026-01-01",
                "value_end_date": "2026-01-10",
                "filter_non_st_before_expression": False,
                "compute_semantics_version": "test",
                "registry_fingerprint": "fp-current",
                "factor_records": [
                    {
                        "factor_id": "f-a",
                        "name": "Factor A",
                        "expression": "expr_a",
                        "data_column": "FactorA",
                        "holding_period_days": 5,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    label_index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2026-01-02"), "600000sh"),
            (pd.Timestamp("2026-01-03"), "600000sh"),
            (pd.Timestamp("2026-01-02"), "000001sz"),
        ],
        names=["datetime", "instrument"],
    )
    label = pd.DataFrame({("label", "LABEL0"): [0.1, 0.2, 0.3]}, index=label_index)
    label.columns = pd.MultiIndex.from_tuples(label.columns)
    monkeypatch.setattr(
        "domain.model.feature_sets.active_values_store_summary",
        lambda holding_period_days=5: {
            "stale": False,
            "registry_fingerprint": "fp-current",
            "manifest_registry_fingerprint": "fp-current",
            "active_count": 1,
            "factor_count": 1,
            "column_count": 1,
            "resolved_universe": "tradable_non_st",
            "path": str(active_values_path),
            "manifest_path": str(active_values_manifest_path),
            "stale_reasons": [],
        },
    )
    monkeypatch.setattr(
        "domain.model.feature_sets.current_active_registry_fingerprint",
        lambda holding_period_days=5: ("fp-current", []),
    )
    monkeypatch.setattr(
        "domain.model.feature_sets._build_label_frame",
        lambda **kwargs: label,
    )

    result = feature_snapshot(feature_set_id="fs-active-wide")

    assert result["ok"] is True
    assert result["manifest"]["feature_source"] == "active_values_wide_table"
    assert result["manifest"]["model_computes_factor_values"] is False
    assert result["manifest"]["factor_records"][0]["active_values_column"] == "expr_a"
    frozen = pd.read_parquet(result["manifest"]["combined_factors_file"])
    assert ("feature", "FactorA") in frozen.columns
    assert ("label", "LABEL0") in frozen.columns

    reused = feature_snapshot(feature_set_id="fs-active-wide-duplicate")
    assert reused["mode"] == "reused_snapshot"
    assert reused["feature_set_id"] == "fs-active-wide"
    assert len(list(root.glob("*/manifest.json"))) == 1


def test_feature_set_preflight_blocks_missing_manifest():
    from domain.model.feature_sets import model_feature_set_preflight

    result = model_feature_set_preflight("fs-does-not-exist")

    assert result["passed"] is False
    assert "feature_set_manifest_missing" in result["errors"]


def test_submit_experiment_blocks_before_round_when_feature_set_missing(tmp_path):
    from domain.model.contracts import default_r1_experiment
    from domain.model.qlib_runner import submit_experiment
    from domain.model.state_store import ModelStateStore

    result = submit_experiment(
        feature_set_id="fs-does-not-exist",
        experiment=default_r1_experiment(),
        state=ModelStateStore(runtime_root=tmp_path),
    )

    assert result["ok"] is False
    assert result["stage"] == "feature_snapshot"
    assert "feature_set_manifest_missing" in result["validation_result"]["errors"]


def test_label0_uses_next_open_to_t_plus_6_open(monkeypatch):
    dates = pd.date_range("2026-01-01", periods=8, freq="D")
    monkeypatch.setattr(
        feature_set_builder,
        "expected_trading_dates",
        lambda start_date, end_date: [str(day.date()) for day in dates],
    )
    base = pd.DataFrame(
        {
            "datetime": dates,
            "instrument": ["000001sz"] * len(dates),
            "label_price": [100.0 + idx for idx in range(len(dates))],
        }
    )

    label = _calendar_aligned_label_frame(
        base,
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
        forward_period=5,
        entry_shift_days=1,
    )

    assert label.loc[(dates[0], "000001sz"), "LABEL0"] == (106.0 / 101.0) - 1.0


def test_feature_manifest_validation_rejects_bad_label_contract():
    validation = validate_feature_set_manifest_for_model(
        {
            "feature_missing_strategy": "qlib_processor_only",
            "label_forward_period": 3,
            "factor_holding_period_days": 5,
            "label_execution_deal_price": "close",
            "factor_count": 54,
            "feature_count": 54,
        }
    )

    assert validation["passed"] is False
    assert "label_forward_period_mismatch" in validation["errors"]
    assert "label_execution_deal_price_mismatch" in validation["errors"]
    assert "feature_snapshot_policy_version_mismatch" in validation["errors"]
    assert "label_price_mode_mismatch" in validation["errors"]
    assert "combined_factors_file_missing" in validation["errors"]


def test_feature_manifest_validation_rejects_parquet_without_label0(tmp_path):
    combined = tmp_path / "combined.parquet"
    index = pd.MultiIndex.from_product(
        [pd.date_range("2026-01-01", periods=2), ["000001sz"]],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame({("feature", "f1"): [1.0, 2.0]}, index=index)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    df.to_parquet(combined)

    validation = validate_feature_set_manifest_for_model(
        {
            "feature_snapshot_policy_version": "qlib_feature_missing_v8_static_universe_labels",
            "feature_missing_strategy": "qlib_processor_only",
            "label_forward_period": 5,
            "factor_holding_period_days": 5,
            "label_price_mode": "qlib_calendar_adjusted_next_open_to_forward_open_from_quantgpt_adjusted_open",
            "label_source_price_field": "open",
            "label_entry_shift_days": 1,
            "label_exit_shift_days": 6,
            "label_execution_deal_price": "open",
            "label_return_mode": "next_open_to_forward_open",
            "label_uses_adjusted_price": True,
            "combined_factors_file": str(combined),
            "factor_count": 1,
            "feature_count": 1,
        }
    )

    assert validation["passed"] is False
    assert "label0_column_missing" in validation["errors"]


def test_explicit_factor_subset_materializes_from_source_snapshot_without_active_pointer_identity_contract(tmp_path, monkeypatch):
    root = tmp_path / "feature_sets"
    monkeypatch.setattr("domain.model.feature_sets.MODEL_FEATURE_SETS_ROOT", root)
    source_dir = root / "fs-source"
    source_dir.mkdir(parents=True)
    index = pd.MultiIndex.from_product(
        [pd.date_range("2026-01-01", periods=2), ["000001sz", "000002sz"]],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame(
        {
            ("feature", "a"): [1.0, 2.0, 3.0, 4.0],
            ("feature", "b"): [2.0, 3.0, 4.0, 5.0],
            ("label", "LABEL0"): [0.1, 0.2, 0.3, 0.4],
        },
        index=index,
    )
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    source_path = source_dir / "combined_factors_df.parquet"
    df.to_parquet(source_path)
    (source_dir / "manifest.json").write_text(
        json.dumps(
            {
                "feature_set_id": "fs-source",
                "feature_set_fingerprint": "source-fp",
                "combined_factors_file": str(source_path),
                "feature_snapshot_policy_version": "qlib_feature_missing_v8_static_universe_labels",
                "feature_missing_strategy": "qlib_processor_only",
                "label_forward_period": 5,
                "factor_holding_period_days": 5,
                "label_price_mode": "qlib_calendar_adjusted_next_open_to_forward_open_from_quantgpt_adjusted_open",
                "label_source_price_field": "open",
                "label_entry_shift_days": 1,
                "label_exit_shift_days": 6,
                "label_execution_deal_price": "open",
                "label_return_mode": "next_open_to_forward_open",
                "label_uses_adjusted_price": True,
                "factor_records": [
                    {"factor_id": "f-a", "data_column": "a"},
                    {"factor_id": "f-b", "data_column": "b"},
                ],
                "factor_ids": ["f-a", "f-b"],
                "factor_count": 2,
                "feature_count": 2,
            }
        ),
        encoding="utf-8",
    )

    result = feature_snapshot(
        feature_set_id="fs-subset",
        factor_ids=["f-a"],
        source_feature_set_id="fs-source",
        source_type="audit_recommended",
        recommendation_family="top_uncorrelated",
        audit_recommendation_id="audit-20260704-top1",
        provenance_note="unit test provenance",
        dry_run=False,
    )

    assert result["ok"] is True
    assert result["mode"] == "materialized_subset_snapshot"
    assert result["manifest"]["updates_active_feature_pointer"] is False
    assert "feature_set_fingerprint" not in result["manifest"]
    assert "source_feature_set_fingerprint" not in result["manifest"]
    assert result["manifest"]["source_manifest_signature"] == "fs-source"
    assert result["manifest"]["source_type"] == "audit_recommended"
    assert result["manifest"]["feature_set_provenance"]["recommendation_family"] == "top_uncorrelated"
    assert result["manifest"]["audit_recommendation_id"] == "audit-20260704-top1"
    assert result["provenance"]["source_type"] == "audit_recommended"
    assert result["manifest"]["factor_ids"] == ["f-a"]
    assert result["manifest"]["feature_snapshot_policy_version"] == feature_set_builder.FEATURE_SNAPSHOT_POLICY_VERSION
    assert result["manifest"]["label_entry_shift_days"] == 1
    assert result["manifest"]["label_exit_shift_days"] == 6
    assert result["manifest"]["label_return_mode"] == "next_open_to_forward_open"
    assert result["manifest"]["feature_missing_policy"] == "label_drop_feature_nan_preserved_then_qlib_processors"
    assert result["validation"]["passed"] is True
    subset = pd.read_parquet(result["manifest"]["combined_factors_file"])
    assert list(subset.columns) == [("feature", "a"), ("label", "LABEL0")]


def test_subset_default_missing_policy_preserves_feature_nan(tmp_path, monkeypatch):
    root = tmp_path / "feature_sets"
    monkeypatch.setattr("domain.model.feature_sets.MODEL_FEATURE_SETS_ROOT", root)
    source_dir = root / "fs-source-nan"
    source_dir.mkdir(parents=True)
    index = pd.MultiIndex.from_product(
        [pd.date_range("2026-01-01", periods=3), ["000001sz"]],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame(
        {
            ("feature", "margin_bal"): [1.0, None, 3.0],
            ("label", "LABEL0"): [0.1, 0.2, None],
        },
        index=index,
    )
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    source_path = source_dir / "combined_factors_df.parquet"
    df.to_parquet(source_path)
    _write_source_manifest(
        source_dir / "manifest.json",
        feature_set_id="fs-source-nan",
        source_path=source_path,
        records=[{"factor_id": "f-margin", "data_column": "margin_bal", "semantic_missing_policy_candidate": "margin_structural_zero"}],
    )

    result = feature_snapshot(
        feature_set_id="fs-subset-nan",
        factor_ids=["f-margin"],
        source_feature_set_id="fs-source-nan",
        feature_missing_strategy="qlib_processor_only",
    )

    subset = pd.read_parquet(result["manifest"]["combined_factors_file"])
    assert pd.isna(subset.loc[(pd.Timestamp("2026-01-02"), "000001sz"), ("feature", "margin_bal")])
    assert result["manifest"]["prefill_applied"] is False
    assert result["manifest"]["post_snapshot_feature_missing_summary"]["missing_cells"] == 1
    assert result["validation"]["passed"] is True


def test_subset_structural_zero_missing_policy_fills_and_audits(tmp_path, monkeypatch):
    root = tmp_path / "feature_sets"
    monkeypatch.setattr("domain.model.feature_sets.MODEL_FEATURE_SETS_ROOT", root)
    source_dir = root / "fs-source-structural"
    source_dir.mkdir(parents=True)
    index = pd.MultiIndex.from_product(
        [pd.date_range("2026-01-01", periods=3), ["000001sz"]],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame(
        {
            ("feature", "margin_bal"): [1.0, None, 3.0],
            ("label", "LABEL0"): [0.1, 0.2, None],
        },
        index=index,
    )
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    source_path = source_dir / "combined_factors_df.parquet"
    df.to_parquet(source_path)
    _write_source_manifest(
        source_dir / "manifest.json",
        feature_set_id="fs-source-structural",
        source_path=source_path,
        records=[{"factor_id": "f-margin", "data_column": "margin_bal", "semantic_missing_policy_candidate": "margin_structural_zero"}],
    )

    result = feature_snapshot(
        feature_set_id="fs-subset-structural",
        factor_ids=["f-margin"],
        source_feature_set_id="fs-source-structural",
        feature_missing_strategy="structural_zero_v2",
    )

    subset = pd.read_parquet(result["manifest"]["combined_factors_file"])
    assert subset.loc[(pd.Timestamp("2026-01-02"), "000001sz"), ("feature", "margin_bal")] == 0.0
    assert result["manifest"]["prefill_applied"] is True
    assert result["manifest"]["feature_imputation_report"][0]["filled_count"] == 1
    assert result["manifest"]["post_snapshot_feature_missing_summary"]["missing_cells"] == 0
    assert result["validation"]["passed"] is True
    assert result["validation"]["warnings"] == ["non_default_feature_missing_strategy"]


def test_subset_blocks_missing_strategy_downgrade_from_prefilled_source(tmp_path, monkeypatch):
    root = tmp_path / "feature_sets"
    monkeypatch.setattr("domain.model.feature_sets.MODEL_FEATURE_SETS_ROOT", root)
    source_dir = root / "fs-source-prefilled"
    source_dir.mkdir(parents=True)
    index = pd.MultiIndex.from_product(
        [pd.date_range("2026-01-01", periods=1), ["000001sz"]],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame({("feature", "margin_bal"): [0.0], ("label", "LABEL0"): [0.1]}, index=index)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    source_path = source_dir / "combined_factors_df.parquet"
    df.to_parquet(source_path)
    _write_source_manifest(
        source_dir / "manifest.json",
        feature_set_id="fs-source-prefilled",
        source_path=source_path,
        records=[{"factor_id": "f-margin", "data_column": "margin_bal", "semantic_missing_policy_candidate": "margin_structural_zero"}],
        feature_missing_strategy="structural_zero_v2",
        prefill_applied=True,
    )

    import pytest

    with pytest.raises(ValueError, match="different missing strategy"):
        feature_snapshot(
            feature_set_id="fs-subset-downgrade",
            factor_ids=["f-margin"],
            source_feature_set_id="fs-source-prefilled",
            feature_missing_strategy="qlib_processor_only",
        )


def _write_source_manifest(
    path,
    *,
    feature_set_id: str,
    source_path,
    records: list[dict],
    feature_missing_strategy: str = "qlib_processor_only",
    prefill_applied: bool = False,
):
    path.write_text(
        json.dumps(
            {
                "feature_set_id": feature_set_id,
                "combined_factors_file": str(source_path),
                "feature_snapshot_policy_version": "qlib_feature_missing_v8_static_universe_labels",
                "feature_missing_strategy": feature_missing_strategy,
                "prefill_applied": prefill_applied,
                "label_forward_period": 5,
                "factor_holding_period_days": 5,
                "label_price_mode": "qlib_calendar_adjusted_next_open_to_forward_open_from_quantgpt_adjusted_open",
                "label_source_price_field": "open",
                "label_entry_shift_days": 1,
                "label_exit_shift_days": 6,
                "label_execution_deal_price": "open",
                "label_return_mode": "next_open_to_forward_open",
                "label_uses_adjusted_price": True,
                "factor_records": records,
                "factor_ids": [item["factor_id"] for item in records],
                "factor_count": len(records),
                "feature_count": len(records),
            }
        ),
        encoding="utf-8",
    )
