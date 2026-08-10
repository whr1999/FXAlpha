import sys
from pathlib import Path

import numpy as np
import pandas as pd


QUANTGPT_ROOT = Path(__file__).resolve().parents[1] / "third_party" / "quantgpt"
if str(QUANTGPT_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANTGPT_ROOT))

from quantgpt.rolling_validator import RollingValidator, run_rolling_validation


def _factor_frame(start="2022-01-03", end="2026-06-30", stocks=12, reverse=False):
    dates = pd.bdate_range(start, end)
    rows = []
    prices = np.full(stocks, 100.0)
    for date_index, date in enumerate(dates):
        common = np.sin(date_index / 17) * 0.0002
        for stock_index in range(stocks):
            signal = (stock_index - (stocks - 1) / 2) / stocks
            factor = -signal if reverse else signal
            daily_return = signal * 0.0015 + common
            prices[stock_index] *= 1 + daily_return
            rows.append(
                {
                    "trade_date": date,
                    "stock_code": f"S{stock_index:03d}",
                    "factor_value": factor,
                    "daily_ret": daily_return,
                    "close": prices[stock_index],
                }
            )
    return pd.DataFrame(rows)


def test_rolling_v2_uses_five_incremental_periods_and_trailing_views():
    result = run_rolling_validation(_factor_frame(), holding_period=5)

    assert result["schema_version"] == "rolling_validation_v2"
    assert result["score_policy_version"] == "rolling_ic_recency_robust_v1"
    assert result["status"] == "ok"
    assert [p["period_id"] for p in result["incremental_periods"]] == [
        "P1_0_6m", "P2_6_12m", "P3_12_24m", "P4_24_36m", "P5_36_48m"
    ]
    assert set(result["trailing_horizons"]) == {"6m", "12m", "24m", "36m", "48m"}
    assert abs(sum(result["effective_weights"].values()) - 1.0) < 1e-5


def test_rolling_v2_rejects_daily_ret_as_label_source():
    frame = _factor_frame().drop(columns=["close"])
    result = run_rolling_validation(frame, holding_period=5)

    assert result["status"] == "label_contract_error"
    assert "daily_ret_is_not_a_valid" in result["summary"]["reason"]
    assert result["score"] == 0


def test_rolling_v2_reports_insufficient_history_below_24_months():
    result = run_rolling_validation(_factor_frame("2025-01-02", "2026-06-30"), holding_period=5)

    assert result["status"] == "insufficient_history"
    assert result["summary"]["n_periods"] == 0


def test_rolling_v2_preserves_direction_and_never_uses_absolute_ic():
    positive = run_rolling_validation(_factor_frame(), holding_period=5)
    negative = run_rolling_validation(_factor_frame(reverse=True), holding_period=5)

    assert positive["weighted_ic"] > 0
    assert positive["score"] > 90
    assert negative["weighted_ic"] < 0
    assert negative["score"] == 0
    assert "negative_incremental_period" in negative["risk_flags"]


def test_rolling_v2_formula_matches_documented_policy():
    result = run_rolling_validation(_factor_frame(), holding_period=5)
    periods = [p for p in result["incremental_periods"] if p["status"] == "ok"]
    weighted = sum(p["effective_weight"] * p["rank_ic"] for p in periods)
    weighted_std = np.sqrt(
        sum(p["effective_weight"] * (p["rank_ic"] - weighted) ** 2 for p in periods)
    )
    robust = weighted - 0.25 * weighted_std
    expected = float(np.clip(robust / 0.08 * 100, 0, 100))

    assert abs(result["weighted_ic"] - weighted) < 2e-6
    assert abs(result["robust_ic"] - robust) < 2e-6
    assert abs(result["score"] - round(expected, 1)) < 0.11


def test_rolling_v2_exposes_calendar_label_dates():
    result = run_rolling_validation(_factor_frame(), holding_period=5)

    assert result["data_as_of_date"] == "2026-06-30"
    assert result["last_evaluable_signal_date"] < result["data_as_of_date"]
    assert result["data_contract"]["label"] == "calendar_date_T_plus_5_close_return"


def test_legacy_window_kwargs_are_ignored_not_reinterpreted():
    validator = RollingValidator(
        _factor_frame(),
        holding_period=5,
        train_months=18,
        valid_months=6,
        test_months=6,
        step_months=6,
    )
    result = validator.run()

    assert result.status == "ok"
    assert len(result.periods) == 5
