from __future__ import annotations

from domain.model.rolling_scoring import portfolio_quality, score_rolling_campaign, score_rolling_seed


def _seed_result(*, latest_ir: float = 1.3, overall_ir: float = 1.4) -> dict:
    folds = {}
    for index in range(4):
        folds[f"fold_{index}"] = {
            "excess_annualized_ret_with_cost": 0.42,
            "excess_information_ratio_with_cost": latest_ir if index == 3 else 1.35,
            "max_drawdown": -0.13,
        }
    return {
        "rolling_metrics": {
            "excess_annualized_ret_with_cost": 0.45,
            "excess_information_ratio_with_cost": overall_ir,
            "max_drawdown": -0.14,
        },
        "fold_portfolio_metrics": folds,
    }


def test_portfolio_quality_uses_ir_return_drawdown_45_35_20():
    result = portfolio_quality({
        "excess_annualized_ret_with_cost": 0.35,
        "excess_information_ratio_with_cost": 1.0,
        "max_drawdown": -0.20,
    })
    assert result["components"] == {"ir_score": 50.0, "return_score": 50.0, "drawdown_score": 50.0}
    assert result["score"] == 50.0


def test_seed42_preliminary_score_uses_overall_worst_latest():
    result = score_rolling_seed(_seed_result())
    assert result["ok"] is True
    expected = round(0.55 * result["overall"]["score"] + 0.25 * result["worst_fold"]["score"] + 0.20 * result["latest_fold"]["score"], 3)
    assert result["score"] == expected


def test_formal_rolling_score_requires_all_seeds_and_latest_positive():
    passed = score_rolling_campaign({42: _seed_result(), 17: _seed_result(overall_ir=1.3), 83: _seed_result(overall_ir=1.2)})
    assert passed["ok"] is True
    assert passed["candidate_passed"] is True
    assert passed["gates"]["latest_fold_ir_positive"] is True

    failed = score_rolling_campaign({42: _seed_result(latest_ir=-0.2), 17: _seed_result(latest_ir=-0.1), 83: _seed_result(latest_ir=-0.3)})
    assert failed["ok"] is True
    assert failed["candidate_passed"] is False
    assert failed["gates"]["latest_fold_ir_positive"] is False
