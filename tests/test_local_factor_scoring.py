from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUANTGPT_ROOT = PROJECT_ROOT / "third_party" / "quantgpt"
if str(QUANTGPT_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANTGPT_ROOT))

from quantgpt.iteration import _grade_from_score, compute_local_quick_score


def test_local_quick_score_ignores_long_short_and_wq_diagnostics():
    summary = {
        "ic_mean": 0.04,
        "ic_ir": 0.5,
        "rank_ic_mean": 0.08,
        "rank_ic_ir": 0.75,
        "annual_return": 0.18,
        "sharpe": 0.65,
        "max_drawdown": -0.10,
        "turnover": 0.20,
        "long_short_sharpe": 9.9,
        "long_short_annual": 9.9,
        "spread": 9.9,
        "wq_fitness": 9.9,
        "data_days": 999,
    }

    baseline = compute_local_quick_score(summary)
    changed = compute_local_quick_score(
        {
            **summary,
            "long_short_sharpe": -9.9,
            "long_short_annual": -9.9,
            "spread": -9.9,
            "wq_fitness": -9.9,
            "data_days": 10,
        }
    )

    assert baseline["score"] == 100.0
    assert changed["score"] == 100.0
    assert baseline["component_scores"] == changed["component_scores"]


def test_local_quick_score_grades_and_component_math():
    result = compute_local_quick_score(
        {
            "ic_mean": 0.02,
            "ic_ir": 0.25,
            "rank_ic_mean": 0.04,
            "rank_ic_ir": 0.375,
            "annual_return": 0.09,
            "sharpe": 0.325,
            "max_drawdown": -0.275,
            "turnover": 0.20,
        }
    )

    assert result["component_scores"]["ic_mean"] == 50.0
    assert result["component_scores"]["ic_ir"] == 50.0
    assert result["component_scores"]["rank_ic_mean"] == 50.0
    assert result["component_scores"]["rank_ic_ir"] == 50.0
    assert result["component_scores"]["annual_return"] == 50.0
    assert result["component_scores"]["sharpe"] == 50.0
    assert result["component_scores"]["max_drawdown"] == 41.7
    assert result["component_scores"]["turnover"] == 100.0
    assert result["score"] == 51.7
    assert result["grade"] == "D"


def test_local_quick_score_grade_boundaries():
    assert _grade_from_score(100.0) == "A"
    assert _grade_from_score(85.0) == "A"
    assert _grade_from_score(84.9) == "B"
    assert _grade_from_score(70.0) == "B"
    assert _grade_from_score(69.9) == "C"
    assert _grade_from_score(55.0) == "C"
    assert _grade_from_score(54.9) == "D"


def test_local_quick_score_zeroes_deep_drawdown():
    result = compute_local_quick_score(
        {
            "ic_mean": 0.0,
            "ic_ir": 0.0,
            "rank_ic_mean": 0.0,
            "rank_ic_ir": 0.0,
            "annual_return": 0.0,
            "sharpe": 0.0,
            "max_drawdown": -0.40,
            "turnover": 0.20,
        }
    )

    assert result["component_scores"]["max_drawdown"] == 0.0


def test_local_quick_score_caps_negative_long_only_metrics():
    result = compute_local_quick_score(
        {
            "ic_mean": 0.05,
            "ic_ir": 0.6,
            "rank_ic_mean": 0.08,
            "rank_ic_ir": 0.8,
            "annual_return": -0.01,
            "sharpe": 0.9,
            "max_drawdown": -0.08,
            "turnover": 0.20,
        }
    )

    assert result["capped"] is True
    assert result["cap_reason"] == "negative_annual_return"
    assert result["score"] == 59.9
    assert result["grade"] == "C"


def test_local_quick_score_does_not_fallback_to_long_short_metrics():
    result = compute_local_quick_score(
        {
            "ic_mean": 0.05,
            "ic_ir": 0.6,
            "rank_ic_mean": 0.08,
            "rank_ic_ir": 0.8,
            "annual_return": None,
            "sharpe": None,
            "long_short_annual": 0.8,
            "long_short_sharpe": 3.0,
            "max_drawdown": -0.08,
            "turnover": 0.20,
        }
    )

    assert result["component_scores"]["annual_return"] == 0.0
    assert result["component_scores"]["sharpe"] == 0.0
    assert result["score"] < 80.0
