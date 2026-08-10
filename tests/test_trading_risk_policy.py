from pathlib import Path

import pandas as pd
import pytest

from domain.trading import risk_policy


def _market_decision(*args, **kwargs):
    return {
        "as_of_date": "2026-08-06",
        "market_stress": True,
        "raw_stress": True,
        "cap": 0.75,
    }


def test_account_brake_only_binds_during_market_stress(monkeypatch):
    monkeypatch.setattr(risk_policy, "_market_decision", _market_decision)
    history = [
        {"trade_date": "2026-08-01", "account_value": 100.0},
        {"trade_date": "2026-08-06", "account_value": 90.0},
    ]
    decision = risk_policy.evaluate_risk_policy(
        signal_date="2026-08-06",
        model_cap=1.0,
        account_history=history,
        current_state={},
    )

    assert decision["market_cap"] == pytest.approx(0.75)
    assert decision["account"]["drawdown"] == pytest.approx(-0.10)
    assert decision["account"]["brake_active"] is True
    assert decision["account_cap"] == pytest.approx(0.50)
    assert decision["final_stock_cap"] == pytest.approx(0.50)
    assert decision["binding_layer"] == "account"


def test_target_weights_are_scaled_without_changing_selection(monkeypatch):
    monkeypatch.setattr(risk_policy, "_market_decision", _market_decision)
    target = pd.DataFrame(
        [
            {"instrument": "600000sh", "rank": 1, "target_weight": 0.50, "target_value": 50000.0},
            {"instrument": "000001sz", "rank": 2, "target_weight": 0.30, "target_value": 30000.0},
        ]
    )

    scaled, decision = risk_policy.apply_risk_policy(
        target,
        signal_date="2026-08-06",
        total_capital=100000.0,
        account_history=[{"trade_date": "2026-08-06", "account_value": 100000.0}],
        current_state={},
    )

    assert scaled["instrument"].tolist() == target["instrument"].tolist()
    assert scaled["rank"].tolist() == target["rank"].tolist()
    assert scaled["target_weight"].sum() == pytest.approx(0.75)
    assert scaled["target_value"].sum() == pytest.approx(75000.0)
    assert decision["binding_layer"] == "market"
    assert decision["scale_factor"] == pytest.approx(0.75 / 0.80)


def test_benchmark_loader_filters_future_rows(tmp_path):
    dates = pd.bdate_range("2026-01-01", periods=80)
    for name, filename in risk_policy.BENCHMARK_FILES.items():
        pd.DataFrame(
            {
                "trade_date": dates,
                "close": [100.0 + index for index in range(len(dates))],
            }
        ).to_parquet(tmp_path / filename, index=False)
    signal_date = str(dates[-5].date())

    closes = risk_policy._benchmark_close_frame(signal_date, tmp_path)

    assert closes.index.max() == dates[-5]
    assert len(closes.columns) == 3


def test_runtime_config_update_is_atomic_and_validated(tmp_path):
    path = tmp_path / "risk_policy.json"
    updated = risk_policy.update_risk_policy_config(
        {"market": {"volatility_threshold": 0.22}, "account": {"drawdown_threshold": 0.10}},
        path,
    )

    assert updated["market"]["volatility_threshold"] == pytest.approx(0.22)
    reloaded = risk_policy.load_risk_policy_config(path)
    assert reloaded["account"]["drawdown_threshold"] == pytest.approx(0.10)
    assert risk_policy.risk_policy_config_hash(reloaded) == updated["config_hash"]
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError, match="volatility_threshold_out_of_range"):
        risk_policy.update_risk_policy_config({"market": {"volatility_threshold": 2.0}}, path)


def test_history_reuses_policy_formula_without_future_data(tmp_path):
    dates = pd.bdate_range("2026-01-01", periods=90)
    for offset, filename in enumerate(risk_policy.BENCHMARK_FILES.values()):
        close = [100.0]
        for index in range(1, len(dates)):
            move = (0.014 if index % 3 else -0.02) + offset * 0.0002
            close.append(close[-1] * (1.0 + move))
        pd.DataFrame({"trade_date": dates, "close": close}).to_parquet(tmp_path / filename, index=False)
    signal_date = str(dates[-4].date())
    account_history = [
        {"trade_date": str(dates[-12].date()), "account_value": 100.0},
        {"trade_date": str(dates[-8].date()), "account_value": 96.0},
        {"trade_date": signal_date, "account_value": 91.0},
        {"trade_date": str(dates[-1].date()), "account_value": 70.0},
    ]
    recommendations = [
        {"signal_date": str(dates[-15].date()), "created_at": "2026-01-01T01:00:00", "metrics": {"target_stock_exposure": 0.8}},
        {"signal_date": signal_date, "created_at": "2026-01-01T02:00:00", "metrics": {"model_target_stock_exposure": 0.4}},
    ]

    result = risk_policy.build_risk_policy_history(
        signal_date=signal_date,
        account_history=account_history,
        recommendation_history=recommendations,
        history_days=50,
        benchmark_dir=tmp_path,
    )

    assert result["market"][-1]["date"] == signal_date
    assert result["account"][-1]["date"] == signal_date
    assert all(row["date"] <= signal_date for row in result["caps"])
    assert result["caps"][-1]["model_cap"] == pytest.approx(0.4)
    assert result["caps"][-1]["final_cap"] == pytest.approx(
        min(result["caps"][-1]["model_cap"], result["caps"][-1]["market_cap"], result["caps"][-1]["account_cap"])
    )
    assert result["method"] == "reconstructed_asof_no_lookahead"
