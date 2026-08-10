from __future__ import annotations

import json

import pandas as pd
import pytest

from domain.model import feature_set_builder
from domain.model.training_contract import QLIB_CANONICAL_PROCESSORS


def _factor_frame(values: dict[tuple[str, str], float]) -> pd.DataFrame:
    idx = pd.MultiIndex.from_tuples(
        [(pd.Timestamp(dt), inst) for dt, inst in values],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame({"value": list(values.values())}, index=idx).sort_index()
    return df


def _label_frame(label_values: dict[tuple[str, str], float | None]) -> pd.DataFrame:
    idx = pd.MultiIndex.from_tuples(
        [(pd.Timestamp(dt), inst) for dt, inst in label_values],
        names=["datetime", "instrument"],
    )
    label = pd.DataFrame({("label", "LABEL0"): list(label_values.values())}, index=idx).sort_index()
    label.columns = pd.MultiIndex.from_tuples(label.columns)
    return label


def _label_contract() -> dict[str, object]:
    return {
        "label_forward_period": 5,
        "label_price_mode": feature_set_builder.LABEL_PRICE_MODE,
        "label_source_price_field": feature_set_builder.LABEL_SOURCE_PRICE_FIELD,
        "label_entry_shift_days": feature_set_builder.LABEL_ENTRY_SHIFT_DAYS,
        "label_exit_shift_days": feature_set_builder.LABEL_ENTRY_SHIFT_DAYS + 5,
        "label_execution_deal_price": "open",
        "label_return_mode": "next_open_to_forward_open",
        "label_uses_adjusted_price": True,
    }


def test_label_frame_uses_qlib_calendar_offsets(monkeypatch):
    market = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-07"]),
            "datetime": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-07"]),
            "instrument": ["000001sz", "000001sz", "000001sz"],
            "label_price": [10.0, 11.0, 12.0],
        }
    )
    monkeypatch.setattr(
        feature_set_builder,
        "expected_trading_dates",
        lambda start, end: ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"],
    )

    label = feature_set_builder._calendar_aligned_label_frame(
        market,
        start_date="2026-01-02",
        end_date="2026-01-07",
        forward_period=2,
    )

    assert (pd.Timestamp("2026-01-02"), "000001sz") not in label.index
    assert label.loc[(pd.Timestamp("2026-01-05"), "000001sz"), "LABEL0"] == pytest.approx(12.0 / 11.0 - 1.0)


def test_label_frame_can_use_qlib_shift1_execution_offsets(monkeypatch):
    market = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]),
            "datetime": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]),
            "instrument": ["000001sz", "000001sz", "000001sz", "000001sz"],
            "label_price": [10.0, 11.0, 12.0, 14.0],
        }
    )
    monkeypatch.setattr(
        feature_set_builder,
        "expected_trading_dates",
        lambda start, end: ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"],
    )

    label = feature_set_builder._calendar_aligned_label_frame(
        market,
        start_date="2026-01-02",
        end_date="2026-01-07",
        forward_period=2,
        entry_shift_days=1,
    )

    assert label.loc[(pd.Timestamp("2026-01-02"), "000001sz"), "LABEL0"] == pytest.approx(14.0 / 11.0 - 1.0)
    assert (pd.Timestamp("2026-01-05"), "000001sz") not in label.index


def _patch_builder_paths(monkeypatch, tmp_path):
    active_dir = tmp_path / "active"
    monkeypatch.setattr(feature_set_builder, "MODEL_FEATURE_SETS_ROOT", tmp_path / "sets")
    monkeypatch.setattr(feature_set_builder, "MODEL_ACTIVE_FEATURE_DIR", active_dir)
    monkeypatch.setattr(feature_set_builder, "MODEL_ACTIVE_FEATURE_FILE", active_dir / "combined_factors_df.parquet")
    monkeypatch.setattr(feature_set_builder, "MODEL_ACTIVE_FEATURE_MANIFEST", active_dir / "manifest.json")
    monkeypatch.setattr(feature_set_builder, "ACTIVE_MODEL_FEATURE_SET_FILE", tmp_path / "active_feature_set.json")
    monkeypatch.setattr(feature_set_builder, "current_active_registry_fingerprint", lambda **kwargs: ("fp-test", []))
    monkeypatch.setattr(
        feature_set_builder,
        "active_values_store_summary",
        lambda **kwargs: {
            "stale": False,
            "path": str(tmp_path / "active_values.parquet"),
            "manifest_path": str(tmp_path / "active_values.manifest.json"),
            "registry_fingerprint": "fp-test",
            "manifest_registry_fingerprint": "fp-test",
            "resolved_universe": "tradable_non_st",
        },
    )
    monkeypatch.setattr(
        feature_set_builder,
        "load_active_values_manifest",
        lambda: {
            "schema_version": "active_adopted_factor_values_v2_calendar_bounded_ts",
            "generated_at": "2026-01-01T00:00:00",
            "resolved_universe": "tradable_non_st",
            "value_start_date": "2026-01-02",
            "value_end_date": "2026-01-05",
            "filter_non_st_before_expression": True,
            "registry_fingerprint": "fp-test",
            "audit_anchor": {"passed": True},
        },
    )


def test_feature_snapshot_preserves_feature_rows_with_missing_label(monkeypatch, tmp_path):
    index_values = {
        ("2026-01-02", "000001.SZ"): 0.01,
        ("2026-01-02", "000002.SZ"): 0.02,
        ("2026-01-05", "000001.SZ"): 0.03,
        ("2026-01-05", "000002.SZ"): None,
    }
    f1_path = tmp_path / "f1.parquet"
    f2_path = tmp_path / "f2.parquet"
    feature_values = {
        **{key: float(i) for i, key in enumerate(index_values, start=1)},
        ("2026-01-05", "000003.SZ"): 5.0,
    }
    _factor_frame(feature_values).to_parquet(f1_path)
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 10.0,
        ("2026-01-05", "000001.SZ"): 30.0,
    }).to_parquet(f2_path)

    factors = [
        {"factor_id": "f1", "name": "f1", "expression": "x", "metadata": {"data_path": str(f1_path), "data_column": "f1"}},
        {"factor_id": "f2", "name": "f2", "expression": "y", "metadata": {"data_path": str(f2_path), "data_column": "f2"}},
    ]

    class FakeRegistry:
        def list_all(self, **kwargs):
            return factors, len(factors)

    _patch_builder_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(feature_set_builder, "FactorRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(feature_set_builder, "resolve_model_end_date", lambda end_date=None: "2026-01-05")
    monkeypatch.setattr(feature_set_builder, "_build_label_frame", lambda **kwargs: _label_frame(index_values))

    manifest = feature_set_builder.build_active_feature_set(
        feature_set_id="fs-test-missing",
        start_date="2026-01-02",
        end_date="2026-01-05",
    )

    combined = pd.read_parquet(manifest["combined_factors_file"])
    assert manifest["feature_snapshot_policy_version"] == feature_set_builder.FEATURE_SNAPSHOT_POLICY_VERSION
    assert manifest["feature_missing_strategy"] == "qlib_processor_only"
    assert manifest["prefill_applied"] is False
    assert manifest["label_sample_count"] == 4
    assert manifest["label_available_sample_count"] == 3
    assert manifest["label_missing_sample_count"] == 2
    assert manifest["post_label_drop_sample_count"] == 5
    assert manifest["label_filter_policy"] == feature_set_builder.LABEL_FILTER_POLICY
    assert len(combined) == 5
    assert pd.isna(combined.loc[(pd.Timestamp("2026-01-02"), "000002.SZ"), ("feature", "f2")])
    assert pd.isna(combined.loc[(pd.Timestamp("2026-01-05"), "000003.SZ"), ("label", "LABEL0")])
    assert manifest["raw_feature_missing_summary"]["missing_cells"] == manifest["post_snapshot_feature_missing_summary"]["missing_cells"]
    assert manifest["feature_imputation_report"] == []
    assert manifest["feature_missing_summary"]["missing_cells"] == 3
    assert any(item["feature_column"] == "f2" for item in manifest["feature_coverage_report"])


def test_explicit_factor_snapshot_does_not_overwrite_active_pointer(monkeypatch, tmp_path):
    label_values = {
        ("2026-01-02", "000001.SZ"): 0.01,
        ("2026-01-02", "000002.SZ"): 0.02,
    }
    f1_path = tmp_path / "f1.parquet"
    f2_path = tmp_path / "f2.parquet"
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 1.0,
        ("2026-01-02", "000002.SZ"): 2.0,
    }).to_parquet(f1_path)
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 10.0,
        ("2026-01-02", "000002.SZ"): 20.0,
    }).to_parquet(f2_path)

    factors = [
        {"factor_id": "f1", "name": "f1", "expression": "x", "metadata": {"data_path": str(f1_path), "data_column": "f1"}},
        {"factor_id": "f2", "name": "f2", "expression": "y", "metadata": {"data_path": str(f2_path), "data_column": "f2"}},
    ]

    class FakeRegistry:
        def list_all(self, **kwargs):
            return factors, len(factors)

    _patch_builder_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(feature_set_builder, "FactorRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(feature_set_builder, "resolve_model_end_date", lambda end_date=None: "2026-01-02")
    monkeypatch.setattr(feature_set_builder, "_build_label_frame", lambda **kwargs: _label_frame(label_values))

    all_active = feature_set_builder.build_active_feature_set(
        feature_set_id="fs-all-active",
        start_date="2026-01-02",
        end_date="2026-01-02",
    )
    assert all_active["updates_active_feature_pointer"] is True
    assert feature_set_builder.MODEL_ACTIVE_FEATURE_MANIFEST.exists()
    active_before = json.loads(feature_set_builder.MODEL_ACTIVE_FEATURE_MANIFEST.read_text(encoding="utf-8"))
    pointer_before = json.loads(feature_set_builder.ACTIVE_MODEL_FEATURE_SET_FILE.read_text(encoding="utf-8"))
    assert active_before["feature_set_id"] == "fs-all-active"
    assert pointer_before["feature_set_id"] == "fs-all-active"
    assert active_before["factor_count"] == 2

    subset = feature_set_builder.build_active_feature_set(
        feature_set_id="fs-subset",
        start_date="2026-01-02",
        end_date="2026-01-02",
        factor_ids=["f1"],
    )

    assert subset["updates_active_feature_pointer"] is False
    assert subset["active_pointer_update_policy"] == "immutable_snapshot_only"
    assert subset["factor_selection_mode"] == "factor_ids"
    assert subset["factor_count"] == 1

    active_after = json.loads(feature_set_builder.MODEL_ACTIVE_FEATURE_MANIFEST.read_text(encoding="utf-8"))
    pointer_after = json.loads(feature_set_builder.ACTIVE_MODEL_FEATURE_SET_FILE.read_text(encoding="utf-8"))
    assert active_after["feature_set_id"] == "fs-all-active"
    assert pointer_after["feature_set_id"] == "fs-all-active"
    assert active_after["factor_count"] == 2


def test_non_all_active_feature_set_is_not_marked_stale_against_current_active_registry(monkeypatch, tmp_path):
    manifest = {
        "feature_set_id": "fs-subset",
        "feature_snapshot_policy_version": feature_set_builder.FEATURE_SNAPSHOT_POLICY_VERSION,
        "status_filter": "active",
        "factor_selection_mode": "factor_ids",
        "updates_active_feature_pointer": False,
        "active_pointer_update_policy": feature_set_builder.IMMUTABLE_SNAPSHOT_UPDATE_POLICY,
        "combined_factors_file": str(tmp_path / "combined.parquet"),
        "feature_file": str(tmp_path / "combined.parquet"),
        "factor_count": 1,
        "feature_count": 1,
        "factor_holding_period_days": 5,
        "feature_missing_strategy": feature_set_builder.FEATURE_MISSING_STRATEGY_DEFAULT,
        "prefill_applied": False,
        "active_factor_registry_fingerprint": "subset-fp",
        "active_values_lineage": {
            "registry_fingerprint": "subset-fp",
            "resolved_universe": "tradable_non_st",
            "audit_anchor": {"passed": True},
        },
        **_label_contract(),
    }
    monkeypatch.setattr(
        feature_set_builder,
        "active_values_store_summary",
        lambda **kwargs: {
            "active_count": 62,
            "registry_fingerprint": "current-fp",
            "manifest_registry_fingerprint": "current-fp",
            "resolved_universe": "tradable_non_st",
            "audit_anchor": {"passed": True},
        },
    )

    staleness = feature_set_builder.active_feature_snapshot_staleness(manifest)

    assert staleness["stale"] is False
    assert staleness["active_feature_contract"]["is_all_active_default"] is False
    assert "snapshot_lineage_differs_from_current_active_registry" in staleness["active_feature_contract"]["warnings"]


def test_all_active_feature_set_is_marked_stale_on_registry_mismatch(monkeypatch, tmp_path):
    manifest = {
        "feature_set_id": "fs-active",
        "feature_snapshot_policy_version": feature_set_builder.FEATURE_SNAPSHOT_POLICY_VERSION,
        "status_filter": "active",
        "factor_selection_mode": "all_active",
        "updates_active_feature_pointer": True,
        "active_pointer_update_policy": feature_set_builder.ACTIVE_POINTER_UPDATE_POLICY,
        "combined_factors_file": str(tmp_path / "combined.parquet"),
        "feature_file": str(tmp_path / "combined.parquet"),
        "factor_count": 61,
        "feature_count": 61,
        "factor_holding_period_days": 5,
        "feature_missing_strategy": feature_set_builder.FEATURE_MISSING_STRATEGY_DEFAULT,
        "prefill_applied": False,
        "active_factor_registry_fingerprint": "old-fp",
        "active_values_lineage": {
            "registry_fingerprint": "old-fp",
            "resolved_universe": "tradable_non_st",
            "audit_anchor": {"passed": True},
        },
        **_label_contract(),
    }
    monkeypatch.setattr(
        feature_set_builder,
        "active_values_store_summary",
        lambda **kwargs: {
            "active_count": 62,
            "registry_fingerprint": "current-fp",
            "manifest_registry_fingerprint": "current-fp",
            "resolved_universe": "tradable_non_st",
            "audit_anchor": {"passed": True},
        },
    )

    staleness = feature_set_builder.active_feature_snapshot_staleness(manifest)

    assert staleness["stale"] is True
    assert staleness["active_feature_contract"]["is_all_active_default"] is True
    assert "active_factor_count_mismatch" in staleness["stale_reasons"]
    assert "active_factor_registry_fingerprint_mismatch" in staleness["stale_reasons"]


def test_active_pointer_feature_set_reports_source_data_mismatch_without_stale(monkeypatch, tmp_path):
    manifest = {
        "feature_set_id": "ALL_ACTIVE",
        "feature_snapshot_policy_version": feature_set_builder.FEATURE_SNAPSHOT_POLICY_VERSION,
        "status_filter": "active",
        "factor_selection_mode": "factor_ids",
        "updates_active_feature_pointer": True,
        "active_pointer_update_policy": feature_set_builder.ACTIVE_POINTER_UPDATE_POLICY,
        "combined_factors_file": str(tmp_path / "combined.parquet"),
        "feature_file": str(tmp_path / "combined.parquet"),
        "factor_count": 62,
        "feature_count": 62,
        "factor_holding_period_days": 5,
        "feature_missing_strategy": feature_set_builder.FEATURE_MISSING_STRATEGY_DEFAULT,
        "prefill_applied": False,
        "active_factor_registry_fingerprint": "fp-old",
        "active_values_lineage": {
            "registry_fingerprint": "fp-old",
            "resolved_universe": "tradable_non_st",
            "source_data_fingerprint": "source-old",
            "audit_anchor": {"passed": True},
        },
        **_label_contract(),
    }
    monkeypatch.setattr(
        feature_set_builder,
        "active_values_store_summary",
        lambda **kwargs: {
            "active_count": 62,
            "registry_fingerprint": "fp-new",
            "manifest_registry_fingerprint": "fp-old",
            "resolved_universe": "tradable_non_st",
            "source_data_fingerprint": "source-old",
            "current_source_data_fingerprint": "source-new",
            "audit_anchor": {"passed": True},
            "source_data_mismatch": True,
            "stale": False,
        },
    )

    staleness = feature_set_builder.active_feature_snapshot_staleness(manifest)

    assert staleness["stale"] is False
    assert staleness["source_data_mismatch"] is True


def test_active_pointer_feature_set_reports_untracked_source_data_without_stale(monkeypatch, tmp_path):
    manifest = {
        "feature_set_id": "ALL_ACTIVE",
        "feature_snapshot_policy_version": feature_set_builder.FEATURE_SNAPSHOT_POLICY_VERSION,
        "status_filter": "active",
        "factor_selection_mode": "factor_ids",
        "updates_active_feature_pointer": True,
        "active_pointer_update_policy": feature_set_builder.ACTIVE_POINTER_UPDATE_POLICY,
        "combined_factors_file": str(tmp_path / "combined.parquet"),
        "feature_file": str(tmp_path / "combined.parquet"),
        "factor_count": 62,
        "feature_count": 62,
        "factor_holding_period_days": 5,
        "feature_missing_strategy": feature_set_builder.FEATURE_MISSING_STRATEGY_DEFAULT,
        "prefill_applied": False,
        "active_factor_registry_fingerprint": "fp-old",
        "active_values_lineage": {
            "registry_fingerprint": "fp-old",
            "resolved_universe": "tradable_non_st",
            "audit_anchor": {"passed": True},
        },
        **_label_contract(),
    }
    monkeypatch.setattr(
        feature_set_builder,
        "active_values_store_summary",
        lambda **kwargs: {
            "active_count": 62,
            "registry_fingerprint": "fp-new",
            "manifest_registry_fingerprint": "fp-old",
            "resolved_universe": "tradable_non_st",
            "source_data_fingerprint": "",
            "current_source_data_fingerprint": "source-current",
            "audit_anchor": {"passed": True},
            "source_data_untracked": True,
            "source_data_mismatch": False,
            "stale": False,
        },
    )

    staleness = feature_set_builder.active_feature_snapshot_staleness(manifest)

    assert staleness["stale"] is False
    assert staleness["source_data_untracked"] is True
    assert staleness["source_data_mismatch"] is False


def test_label_period_does_not_filter_factor_holding_period(monkeypatch, tmp_path):
    label_values = {
        ("2026-01-02", "000001.SZ"): 0.01,
        ("2026-01-03", "000001.SZ"): 0.02,
    }
    factor_path = tmp_path / "f1.parquet"
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 1.0,
        ("2026-01-03", "000001.SZ"): 2.0,
    }).to_parquet(factor_path)
    factors = [
        {
            "factor_id": "f1",
            "name": "f1",
            "expression": "x",
            "holding_period_days": 5,
            "metadata": {"data_path": str(factor_path), "data_column": "f1"},
        },
    ]
    calls: dict[str, list[int]] = {"registry": [], "active_values": [], "fingerprint": [], "label": []}

    class FakeRegistry:
        def list_all(self, **kwargs):
            calls["registry"].append(int(kwargs["holding_period_days"]))
            return factors, len(factors)

    _patch_builder_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(feature_set_builder, "FactorRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(feature_set_builder, "resolve_model_end_date", lambda end_date=None: "2026-01-03")
    monkeypatch.setattr(
        feature_set_builder,
        "active_values_store_summary",
        lambda **kwargs: calls["active_values"].append(int(kwargs["holding_period_days"])) or {
            "stale": False,
            "path": str(tmp_path / "active_values.parquet"),
            "manifest_path": str(tmp_path / "active_values.manifest.json"),
            "registry_fingerprint": "fp-5d",
            "manifest_registry_fingerprint": "fp-5d",
            "resolved_universe": "tradable_non_st",
        },
    )
    monkeypatch.setattr(
        feature_set_builder,
        "current_active_registry_fingerprint",
        lambda **kwargs: calls["fingerprint"].append(int(kwargs["holding_period_days"])) or ("fp-5d", []),
    )

    def fake_label_frame(**kwargs):
        calls["label"].append(int(kwargs["forward_period"]))
        return _label_frame(label_values)

    monkeypatch.setattr(feature_set_builder, "_build_label_frame", fake_label_frame)

    manifest = feature_set_builder.build_active_feature_set(
        feature_set_id="fs-label1-factor5",
        start_date="2026-01-02",
        end_date="2026-01-03",
        label_forward_period=1,
        factor_holding_period_days=5,
    )

    assert calls["label"] == [1]
    assert calls["registry"] == [5]
    assert calls["active_values"] == [5]
    assert calls["fingerprint"] == [5]
    assert manifest["label_forward_period"] == 1
    assert manifest["factor_holding_period_days"] == 5
    assert manifest["holding_period_days"] == 5
    assert manifest["factor_count"] == 1


def test_feature_snapshot_blocks_stale_active_values(monkeypatch, tmp_path):
    factor_path = tmp_path / "f1.parquet"
    _factor_frame({("2026-01-02", "000001.SZ"): 1.0}).to_parquet(factor_path)
    factors = [
        {"factor_id": "f1", "name": "f1", "expression": "x", "metadata": {"data_path": str(factor_path), "data_column": "f1"}},
    ]

    class FakeRegistry:
        def list_all(self, **kwargs):
            return factors, len(factors)

    _patch_builder_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(feature_set_builder, "FactorRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(
        feature_set_builder,
        "active_values_store_summary",
        lambda **kwargs: {
            "stale": True,
            "registry_fingerprint": "fp-new",
            "manifest_registry_fingerprint": "fp-old",
            "resolved_universe": "tradable_non_st",
        },
    )

    with pytest.raises(RuntimeError, match="active values store is stale"):
        feature_set_builder.build_active_feature_set(
            feature_set_id="fs-stale-blocked",
            start_date="2026-01-02",
            end_date="2026-01-02",
        )


def test_qlib_canonical_processors_use_cross_sectional_feature_fill_and_label_drop():
    assert QLIB_CANONICAL_PROCESSORS["infer_processors"] == [
        "ProcessInf",
        "RobustZScoreNorm(fields_group=feature, clip_outlier=True)",
        "CSZFillna(fields_group=feature)",
    ]
    assert QLIB_CANONICAL_PROCESSORS["learn_processors"] == ["DropnaLabel", "CSZScoreNorm(fields_group=label)"]


def test_label_frame_uses_quantgpt_adjusted_open_without_double_factor(monkeypatch):
    market = pd.DataFrame(
        [
            {"trade_date": "2026-01-02", "stock_code": "sz.000001", "open": 100.0, "backward_factor": 1.0},
            {"trade_date": "2026-01-05", "stock_code": "sz.000001", "open": 50.0, "backward_factor": 2.0},
            {"trade_date": "2026-01-06", "stock_code": "sz.000001", "open": 51.0, "backward_factor": 2.0},
        ]
    )
    monkeypatch.setattr(feature_set_builder, "_load_market_data", lambda **kwargs: market)
    monkeypatch.setattr(
        feature_set_builder,
        "expected_trading_dates",
        lambda start, end: ["2026-01-02", "2026-01-05", "2026-01-06"],
    )

    label = feature_set_builder._build_label_frame(
        start_date="2026-01-02",
        end_date="2026-01-06",
        forward_period=1,
    )

    first_label = label.loc[(pd.Timestamp("2026-01-02"), "000001sz"), ("label", "LABEL0")]
    assert first_label == pytest.approx(0.02)


def test_exec_label_zeroes_entry_open_limit_buy_miss(monkeypatch):
    market = pd.DataFrame(
        [
            {"trade_date": "2026-01-02", "stock_code": "sz.000001", "open": 20.0, "up_limit": 10.50, "backward_factor": 2.0},
            {"trade_date": "2026-01-05", "stock_code": "sz.000001", "open": 22.0, "up_limit": 11.00, "backward_factor": 2.0},
            {"trade_date": "2026-01-06", "stock_code": "sz.000001", "open": 24.0, "up_limit": 12.10, "backward_factor": 2.0},
        ]
    )
    monkeypatch.setattr(feature_set_builder, "_load_market_data", lambda **kwargs: market)
    monkeypatch.setattr(
        feature_set_builder,
        "expected_trading_dates",
        lambda start, end: ["2026-01-02", "2026-01-05", "2026-01-06"],
    )

    raw = feature_set_builder._build_label_frame(
        start_date="2026-01-02",
        end_date="2026-01-06",
        forward_period=1,
    )
    executable = feature_set_builder._build_label_frame(
        start_date="2026-01-02",
        end_date="2026-01-06",
        forward_period=1,
        label_mode=feature_set_builder.LABEL_MODE_EXEC_OPEN_ENTRY_LIMIT_V1,
    )

    key = (pd.Timestamp("2026-01-02"), "000001sz")
    assert raw.loc[key, ("label", "LABEL0")] == pytest.approx(24.0 / 22.0 - 1.0)
    assert executable.loc[key, ("label", "LABEL0")] == 0.0


def test_exec_label_uses_raw_open_not_adjusted_open_for_limit_check(monkeypatch):
    market = pd.DataFrame(
        [
            {"trade_date": "2026-01-02", "stock_code": "sz.000001", "open": 20.0, "up_limit": 10.50, "backward_factor": 2.0},
            {"trade_date": "2026-01-05", "stock_code": "sz.000001", "open": 21.8, "up_limit": 11.00, "backward_factor": 2.0},
            {"trade_date": "2026-01-06", "stock_code": "sz.000001", "open": 24.0, "up_limit": 12.10, "backward_factor": 2.0},
        ]
    )
    monkeypatch.setattr(feature_set_builder, "_load_market_data", lambda **kwargs: market)
    monkeypatch.setattr(
        feature_set_builder,
        "expected_trading_dates",
        lambda start, end: ["2026-01-02", "2026-01-05", "2026-01-06"],
    )

    executable = feature_set_builder._build_label_frame(
        start_date="2026-01-02",
        end_date="2026-01-06",
        forward_period=1,
        label_mode=feature_set_builder.LABEL_MODE_EXEC_OPEN_ENTRY_LIMIT_V1,
    )

    key = (pd.Timestamp("2026-01-02"), "000001sz")
    assert executable.loc[key, ("label", "LABEL0")] == pytest.approx(24.0 / 21.8 - 1.0)


def test_label_frame_does_not_apply_point_in_time_st_filter(monkeypatch):
    dates = pd.bdate_range("2026-01-01", periods=8)
    market = pd.DataFrame(
        {
            "trade_date": dates,
            "stock_code": ["sh.600000"] * len(dates),
            "open": [10.0, 11.0, 50.0, 13.0, 14.0, 15.0, 16.0, 17.0],
            "backward_factor": [1.0] * len(dates),
            "list_status": ["L"] * len(dates),
            "st_status": ["NORMAL", "NORMAL", "ST", "NORMAL", "NORMAL", "NORMAL", "NORMAL", "NORMAL"],
        }
    )
    requested_columns = set()

    def fake_load_market_data(**kwargs):
        requested_columns.update(kwargs["required_columns"])
        return market

    monkeypatch.setattr(feature_set_builder, "_load_market_data", fake_load_market_data)
    monkeypatch.setattr(
        feature_set_builder,
        "expected_trading_dates",
        lambda start, end: [str(day.date()) for day in dates],
    )

    label = feature_set_builder._build_label_frame(
        start_date="2026-01-01",
        end_date="2026-01-12",
        forward_period=2,
    )

    assert (pd.Timestamp("2026-01-05"), "600000sh") in label.index
    assert label.loc[(pd.Timestamp("2026-01-01"), "600000sh"), ("label", "LABEL0")] == pytest.approx(13.0 / 11.0 - 1.0)
    assert label.loc[(pd.Timestamp("2026-01-02"), "600000sh"), ("label", "LABEL0")] == pytest.approx(14.0 / 50.0 - 1.0)
    assert label.loc[(pd.Timestamp("2026-01-05"), "600000sh"), ("label", "LABEL0")] == pytest.approx(15.0 / 13.0 - 1.0)
    assert "st_status" not in requested_columns
    assert "list_status" not in requested_columns


def test_default_qlib_processor_only_preserves_margin_and_pe_nan(monkeypatch, tmp_path):
    label_values = {
        ("2026-01-02", "000001.SZ"): 0.01,
        ("2026-01-02", "000002.SZ"): 0.02,
        ("2026-01-02", "000003.SZ"): 0.03,
    }
    margin_path = tmp_path / "margin.parquet"
    pe_path = tmp_path / "pe.parquet"
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 5.0,
        ("2026-01-02", "000002.SZ"): float("nan"),
        ("2026-01-02", "000003.SZ"): 7.0,
    }).to_parquet(margin_path)
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 10.0,
        ("2026-01-02", "000002.SZ"): float("nan"),
        ("2026-01-02", "000003.SZ"): 20.0,
    }).to_parquet(pe_path)

    factors = [
        {
            "factor_id": "f_margin",
            "name": "MarginTradeMean60",
            "expression": "mean(margin_trade_bal, 60)",
            "metadata": {"data_path": str(margin_path), "data_column": "MarginTradeMean60"},
        },
        {
            "factor_id": "f_pe",
            "name": "PeValue",
            "expression": "rank(-pe)",
            "metadata": {"data_path": str(pe_path), "data_column": "PeValue"},
        },
    ]

    class FakeRegistry:
        def list_all(self, **kwargs):
            return factors, len(factors)

    _patch_builder_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(feature_set_builder, "FactorRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(feature_set_builder, "resolve_model_end_date", lambda end_date=None: "2026-01-02")
    monkeypatch.setattr(feature_set_builder, "_build_label_frame", lambda **kwargs: _label_frame(label_values))

    manifest = feature_set_builder.build_active_feature_set(
        feature_set_id="fs-test-default-no-semfill",
        start_date="2026-01-02",
        end_date="2026-01-02",
    )

    combined = pd.read_parquet(manifest["combined_factors_file"])
    missing_key = (pd.Timestamp("2026-01-02"), "000002.SZ")

    assert pd.isna(combined.loc[missing_key, ("feature", "MarginTradeMean60")])
    assert pd.isna(combined.loc[missing_key, ("feature", "PeValue")])
    assert manifest["feature_missing_strategy"] == "qlib_processor_only"
    assert manifest["prefill_applied"] is False
    policies = {item["feature_column"]: item for item in manifest["semantic_missing_audit_report"]}
    assert policies["MarginTradeMean60"]["semantic_policy_candidate"] == "margin_structural_zero"
    assert policies["MarginTradeMean60"]["policy"] == "qlib_processor_neutral_fill"
    assert policies["MarginTradeMean60"]["filled_count"] == 0
    assert policies["PeValue"]["semantic_policy_candidate"] == "pe_cross_sectional_floor"
    assert policies["PeValue"]["policy"] == "qlib_processor_neutral_fill"
    assert policies["PeValue"]["filled_count"] == 0
    assert manifest["raw_feature_missing_summary"]["missing_cells"] == 2
    assert manifest["post_snapshot_feature_missing_summary"]["missing_cells"] == 2


def test_explicit_structural_zero_v2_fills_margin_but_preserves_pe_nan(monkeypatch, tmp_path):
    label_values = {
        ("2026-01-02", "000001.SZ"): 0.01,
        ("2026-01-02", "000002.SZ"): 0.02,
        ("2026-01-02", "000003.SZ"): 0.03,
    }
    margin_path = tmp_path / "margin.parquet"
    pe_path = tmp_path / "pe.parquet"
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 5.0,
        ("2026-01-02", "000002.SZ"): float("nan"),
        ("2026-01-02", "000003.SZ"): 7.0,
    }).to_parquet(margin_path)
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 10.0,
        ("2026-01-02", "000002.SZ"): float("nan"),
        ("2026-01-02", "000003.SZ"): 20.0,
    }).to_parquet(pe_path)

    factors = [
        {
            "factor_id": "f_margin",
            "name": "MarginTradeMean60",
            "expression": "mean(margin_trade_bal, 60)",
            "metadata": {"data_path": str(margin_path), "data_column": "MarginTradeMean60"},
        },
        {
            "factor_id": "f_pe",
            "name": "PeValue",
            "expression": "rank(-pe)",
            "metadata": {"data_path": str(pe_path), "data_column": "PeValue"},
        },
    ]

    class FakeRegistry:
        def list_all(self, **kwargs):
            return factors, len(factors)

    _patch_builder_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(feature_set_builder, "FactorRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(feature_set_builder, "resolve_model_end_date", lambda end_date=None: "2026-01-02")
    monkeypatch.setattr(feature_set_builder, "_build_label_frame", lambda **kwargs: _label_frame(label_values))

    manifest = feature_set_builder.build_active_feature_set(
        feature_set_id="fs-test-explicit-structzero",
        start_date="2026-01-02",
        end_date="2026-01-02",
        feature_missing_strategy="structural_zero_v2",
    )

    combined = pd.read_parquet(manifest["combined_factors_file"])
    missing_key = (pd.Timestamp("2026-01-02"), "000002.SZ")

    assert combined.loc[missing_key, ("feature", "MarginTradeMean60")] == 0.0
    assert pd.isna(combined.loc[missing_key, ("feature", "PeValue")])
    assert manifest["feature_missing_strategy"] == "structural_zero_v2"
    assert manifest["prefill_applied"] is True
    policies = {item["feature_column"]: item for item in manifest["semantic_missing_audit_report"]}
    assert policies["MarginTradeMean60"]["semantic_policy_candidate"] == "margin_structural_zero"
    assert policies["MarginTradeMean60"]["policy"] == "margin_structural_zero"
    assert policies["MarginTradeMean60"]["filled_count"] == 1
    assert policies["PeValue"]["semantic_policy_candidate"] == "pe_cross_sectional_floor"
    assert policies["PeValue"]["policy"] == "qlib_processor_neutral_fill"
    assert policies["PeValue"]["filled_count"] == 0
    assert manifest["raw_feature_missing_summary"]["missing_cells"] == 2
    assert manifest["post_snapshot_feature_missing_summary"]["missing_cells"] == 1


def test_qlib_processor_only_preserves_margin_and_pe_nan_before_qlib(monkeypatch, tmp_path):
    label_values = {
        ("2026-01-02", "000001.SZ"): 0.01,
        ("2026-01-02", "000002.SZ"): 0.02,
        ("2026-01-02", "000003.SZ"): 0.03,
    }
    margin_path = tmp_path / "margin.parquet"
    pe_path = tmp_path / "pe.parquet"
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 5.0,
        ("2026-01-02", "000002.SZ"): float("nan"),
        ("2026-01-02", "000003.SZ"): 7.0,
    }).to_parquet(margin_path)
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 10.0,
        ("2026-01-02", "000002.SZ"): float("nan"),
        ("2026-01-02", "000003.SZ"): 20.0,
    }).to_parquet(pe_path)

    factors = [
        {
            "factor_id": "f_margin",
            "name": "MarginTradeMean60",
            "expression": "mean(margin_trade_bal, 60)",
            "metadata": {"data_path": str(margin_path), "data_column": "MarginTradeMean60"},
        },
        {
            "factor_id": "f_pe",
            "name": "PeValue",
            "expression": "rank(-pe)",
            "metadata": {"data_path": str(pe_path), "data_column": "PeValue"},
        },
    ]

    class FakeRegistry:
        def list_all(self, **kwargs):
            return factors, len(factors)

    _patch_builder_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(feature_set_builder, "FactorRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(feature_set_builder, "resolve_model_end_date", lambda end_date=None: "2026-01-02")
    monkeypatch.setattr(feature_set_builder, "_build_label_frame", lambda **kwargs: _label_frame(label_values))

    manifest = feature_set_builder.build_active_feature_set(
        feature_set_id="fs-test-explicit-qlib-only",
        start_date="2026-01-02",
        end_date="2026-01-02",
        feature_missing_strategy="qlib_processor_only",
    )

    combined = pd.read_parquet(manifest["combined_factors_file"])
    missing_key = (pd.Timestamp("2026-01-02"), "000002.SZ")

    assert pd.isna(combined.loc[missing_key, ("feature", "MarginTradeMean60")])
    assert pd.isna(combined.loc[missing_key, ("feature", "PeValue")])
    assert manifest["feature_missing_strategy"] == "qlib_processor_only"
    assert manifest["prefill_applied"] is False
    assert manifest["feature_imputation_report"] == []
    assert manifest["raw_feature_missing_summary"]["missing_cells"] == 2
    assert manifest["post_snapshot_feature_missing_summary"]["missing_cells"] == 2


def test_semantic_missing_fill_v1_handles_margin_zero_and_pe_floor(monkeypatch, tmp_path):
    label_values = {
        ("2026-01-02", "000001.SZ"): 0.01,
        ("2026-01-02", "000002.SZ"): 0.02,
        ("2026-01-02", "000003.SZ"): 0.03,
    }
    margin_path = tmp_path / "margin.parquet"
    pe_path = tmp_path / "pe.parquet"
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 5.0,
        ("2026-01-02", "000002.SZ"): float("nan"),
        ("2026-01-02", "000003.SZ"): 7.0,
    }).to_parquet(margin_path)
    _factor_frame({
        ("2026-01-02", "000001.SZ"): 10.0,
        ("2026-01-02", "000002.SZ"): float("nan"),
        ("2026-01-02", "000003.SZ"): 20.0,
    }).to_parquet(pe_path)

    factors = [
        {
            "factor_id": "f_margin",
            "name": "MarginTradeMean60",
            "expression": "mean(margin_trade_bal, 60)",
            "metadata": {"data_path": str(margin_path), "data_column": "MarginTradeMean60"},
        },
        {
            "factor_id": "f_pe",
            "name": "PeValue",
            "expression": "rank(-pe)",
            "metadata": {"data_path": str(pe_path), "data_column": "PeValue"},
        },
    ]

    class FakeRegistry:
        def list_all(self, **kwargs):
            return factors, len(factors)

    _patch_builder_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(feature_set_builder, "FactorRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(feature_set_builder, "resolve_model_end_date", lambda end_date=None: "2026-01-02")
    monkeypatch.setattr(feature_set_builder, "_build_label_frame", lambda **kwargs: _label_frame(label_values))

    manifest = feature_set_builder.build_active_feature_set(
        feature_set_id="fs-test-semantic-fill",
        start_date="2026-01-02",
        end_date="2026-01-02",
        feature_missing_strategy="semantic_fill_v1",
    )

    combined = pd.read_parquet(manifest["combined_factors_file"])
    missing_key = (pd.Timestamp("2026-01-02"), "000002.SZ")

    assert combined.loc[missing_key, ("feature", "MarginTradeMean60")] == 0.0
    assert combined.loc[missing_key, ("feature", "PeValue")] < 10.0
    assert manifest["feature_missing_strategy"] == "semantic_fill_v1"
    assert manifest["prefill_applied"] is True
    assert manifest["feature_special_fill_policy_version"] == "feature_semantic_missing_fill_v1"
    policies = {item["feature_column"]: item for item in manifest["feature_imputation_report"]}
    assert policies["MarginTradeMean60"]["policy"] == "margin_structural_zero"
    assert policies["MarginTradeMean60"]["filled_count"] == 1
    assert policies["PeValue"]["policy"] == "pe_cross_sectional_floor"
    assert policies["PeValue"]["filled_count"] == 1
    assert manifest["raw_feature_missing_summary"]["missing_cells"] == 2
    assert manifest["post_snapshot_feature_missing_summary"]["missing_cells"] == 0
    assert manifest["feature_missing_summary"]["missing_cells"] == 0
