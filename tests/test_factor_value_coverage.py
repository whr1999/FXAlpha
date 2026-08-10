from __future__ import annotations

import pandas as pd

from domain.factor_research import auto_import, factor_compute


def _calendar(tmp_path, dates: list[str]) -> None:
    calendar_dir = tmp_path / "qlib" / "calendars"
    calendar_dir.mkdir(parents=True)
    (calendar_dir / "day.txt").write_text("\n".join(dates) + "\n", encoding="utf-8")


def _factor_frame(dates: list[str], n_instruments: int = 301) -> pd.DataFrame:
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp(day), f"{idx:06d}sh")
            for day in dates
            for idx in range(n_instruments)
        ],
        names=["datetime", "instrument"],
    )
    return pd.DataFrame({"value": 1.0}, index=index)


def test_factor_value_coverage_blocks_missing_trading_day(monkeypatch, tmp_path):
    _calendar(tmp_path, ["2026-04-03", "2026-04-07"])
    monkeypatch.setattr(factor_compute, "QLIB_DATA_ROOT", tmp_path / "qlib")

    audit = factor_compute.audit_factor_value_coverage(
        _factor_frame(["2026-04-03"]),
        "2026-04-03",
        "2026-04-07",
    )

    assert audit["passed"] is False
    assert audit["reason"] == "missing_trading_days"
    assert audit["missing_dates"] == ["2026-04-07"]


def test_factor_value_coverage_allows_partial_stock_gaps_above_threshold(monkeypatch, tmp_path):
    _calendar(tmp_path, ["2026-04-03", "2026-04-07"])
    monkeypatch.setattr(factor_compute, "QLIB_DATA_ROOT", tmp_path / "qlib")

    audit = factor_compute.audit_factor_value_coverage(
        _factor_frame(["2026-04-03", "2026-04-07"], n_instruments=301),
        "2026-04-03",
        "2026-04-07",
    )

    assert audit["passed"] is True
    assert audit["min_daily_valid"] == 301


def test_auto_import_returns_skipped_value_coverage(monkeypatch, tmp_path):
    _calendar(tmp_path, ["2026-04-03", "2026-04-07"])
    monkeypatch.setattr(factor_compute, "QLIB_DATA_ROOT", tmp_path / "qlib")
    monkeypatch.setattr(factor_compute, "compute_factor", lambda *args, **kwargs: _factor_frame(["2026-04-03"]))
    monkeypatch.setattr(factor_compute, "save_factor_frame", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("save should not be called")))

    class Registry:
        def get_active_by_expression(self, expression):
            return None

        def register(self, **kwargs):
            raise AssertionError("register should not be called")

    import storage.factor_registry as factor_registry

    monkeypatch.setattr(factor_registry, "FactorRegistry", Registry)
    result = auto_import.import_factors(
        [{"expression": "rank(close)", "gate_result": {"holding_period_days": 5}}],
        start_date="2026-04-03",
        end_date="2026-04-07",
        force_import=True,
        submit_wq=False,
    )

    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert result["details"][0]["status"] == "skipped_value_coverage"
    assert result["details"][0]["missing_dates"] == ["2026-04-07"]


def test_save_factor_frame_does_not_sync_wide_store_by_default(monkeypatch, tmp_path):
    df = _factor_frame(["2026-04-03"], n_instruments=3)

    def fail_sync(*args, **kwargs):
        raise AssertionError("wide store sync should be deferred outside import save")

    monkeypatch.setattr(factor_compute, "sync_adopted_factor_values", fail_sync)

    path = factor_compute.save_factor_frame("rank(close)", "QGF_Test", df, output_dir=tmp_path)

    assert path is not None
    assert path.endswith("factor_QGF_Test.parquet")


def test_auto_import_safe_column_avoids_active_data_column_collision():
    used = {"QGF_Amount_00"}

    safe_name = auto_import._unique_safe_factor_column(
        "QGF_Amount",
        "rank(ts_mean((cost_85pct-cost_15pct)/close,5)) * rank(-ts_mean(amount/free_share,5))",
        0,
        used,
    )

    assert safe_name != "QGF_Amount_00"
    assert safe_name in used
    assert len(safe_name) <= 40


def test_auto_import_reads_active_data_columns_from_registry_metadata():
    class Registry:
        def list_active(self, min_icir=0.0):
            return [
                {"metadata": {"data_column": "QGF_Amount_00"}},
                {"metadata": '{"data_column": "QGF_Other_00"}'},
                {"metadata": "not-json"},
            ]

    assert auto_import._active_data_columns(Registry()) == {"QGF_Amount_00", "QGF_Other_00"}


def test_factor_name_generator_avoids_raw_field_name_for_composite_amount_signal():
    expression = "rank(ts_mean((cost_85pct-cost_15pct)/close,5)) * rank(-ts_mean(amount/free_share,5))"

    category_info = auto_import.classify_factor_expression(expression)
    name, status = auto_import.canonical_factor_name(expression, category_info, proposed_name="Amount")

    assert status == "repaired"
    assert name != "Amount"
    assert "CostSpread" in name
    assert "AmountFloat" in name
    assert auto_import.factor_name_quality_reason(name) == ""
