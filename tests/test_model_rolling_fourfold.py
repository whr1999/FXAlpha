from __future__ import annotations

import pickle

import pandas as pd
import pytest

from domain.model.qlib_direct import _ensure_qlib0627_path
from scripts.model_rolling_fourfold_test import (
    _fold_portfolio_metrics,
    _fold_window_contract,
    _label_coverage,
    _portfolio_boundary_audit,
    _requested_folds,
)


def test_requested_folds_are_dynamic_four_contiguous_half_years():
    calendar = pd.bdate_range("2022-01-03", "2026-07-01")
    folds = _requested_folds({"start_date": "2022-01-01", "end_date": "2026-07-01"}, calendar)
    assert len(folds) == 4
    assert folds[0]["test_start"] == "2024-07-01"
    assert folds[-1]["test_end"] == "2026-06-30"
    assert all(row["train_start"] == "2022-01-01" for row in folds)


class _FakePosition:
    def __init__(self, stocks: list[str]):
        self._stocks = stocks

    def get_stock_list(self) -> list[str]:
        return list(self._stocks)


def test_fold_window_contract_attributes_next_day_execution_without_overlap():
    calendar = pd.DatetimeIndex(
        pd.to_datetime(["2024-07-01", "2024-07-02", "2024-12-31", "2025-01-02", "2025-01-03"])
    )
    first = _fold_window_contract(calendar, calendar, ["2024-07-01", "2024-12-31"])
    second = _fold_window_contract(calendar, calendar, ["2025-01-02", "2025-01-03"])

    assert first["signal_window"] == ["2024-07-01", "2024-12-31"]
    assert first["observed_execution_window"] == ["2024-07-02", "2025-01-02"]
    assert second["observed_execution_window"] == ["2025-01-03", "2025-01-03"]
    assert first["last_signal_executed_in_backtest"] is True
    assert second["last_signal_executed_in_backtest"] is False


def test_label_coverage_exposes_unrealized_tail_dates():
    index = pd.MultiIndex.from_product(
        [pd.to_datetime(["2026-06-23", "2026-06-24"]), ["sh.600000"]],
        names=["datetime", "instrument"],
    )
    pred = pd.Series([0.1, 0.2], index=index)
    label = pd.Series([0.05, float("nan")], index=index)

    coverage = _label_coverage(label, pred)

    assert coverage["prediction_date_count"] == 2
    assert coverage["realized_label_date_count"] == 1
    assert coverage["label_data_end"] == "2026-06-23"
    assert coverage["signal_dates_without_realized_label"] == ["2026-06-24"]


def test_fold_metrics_use_net_aliases_and_keep_explicit_gross_fields():
    _ensure_qlib0627_path()
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"])
    report = pd.DataFrame(
        {
            "return": [0.03, -0.01, 0.02],
            "cost": [0.01, 0.01, 0.01],
            "bench": [0.0, 0.0, 0.0],
            "turnover": [0.2, 0.2, 0.2],
        },
        index=dates,
    )
    contract = {
        "observed_execution_window": ["2025-01-02", "2025-01-06"],
        "signal_window": ["2024-12-31", "2025-01-03"],
    }

    metrics = _fold_portfolio_metrics(report, contract)

    assert metrics["strategy_annualized_ret"] == metrics["net_strategy_annualized_ret"]
    assert metrics["max_drawdown"] == metrics["net_max_drawdown"]
    assert metrics["gross_strategy_annualized_ret"] != metrics["net_strategy_annualized_ret"]


def test_boundary_audit_uses_account_return_identity(tmp_path):
    dates = pd.to_datetime(["2024-12-31", "2025-01-02", "2025-01-03"])
    net = pd.Series([0.0, 0.02, -0.01], index=dates)
    account = pd.Series([100.0, 102.0, 100.98], index=dates)
    report = pd.DataFrame(
        {
            "account": account,
            "return": net + 0.001,
            "cost": 0.001,
        },
        index=dates,
    )
    positions = {
        dates[0]: _FakePosition(["a", "b"]),
        dates[1]: _FakePosition(["b", "c"]),
        dates[2]: _FakePosition(["c", "d"]),
    }
    positions_path = tmp_path / "positions.pkl"
    with positions_path.open("wb") as handle:
        pickle.dump(positions, handle)
    folds = [
        {"segments": {"test": ["2024-12-31", "2024-12-31"]}},
        {"segments": {"test": ["2025-01-02", "2025-01-03"]}},
    ]

    audit = _portfolio_boundary_audit(report, positions_path, folds, initial_account=100.0)

    assert audit["passed"] is True
    assert audit["method"] == "account_pct_change_equals_return_minus_cost"
    assert audit["boundaries"][0]["continuity_residual"] == pytest.approx(0.0, abs=1.0e-12)


def test_single_fold_boundary_audit_is_not_applicable_but_passes(tmp_path):
    date = pd.Timestamp("2025-01-02")
    report = pd.DataFrame({"account": [100.0], "return": [0.0], "cost": [0.0]}, index=[date])
    positions_path = tmp_path / "positions.pkl"
    with positions_path.open("wb") as handle:
        pickle.dump({date: _FakePosition(["a"])}, handle)

    audit = _portfolio_boundary_audit(
        report,
        positions_path,
        [{"segments": {"test": ["2025-01-02", "2025-01-02"]}}],
        initial_account=100.0,
    )

    assert audit["passed"] is True
    assert audit["not_applicable"] is True
