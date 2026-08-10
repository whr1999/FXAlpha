from __future__ import annotations

import pytest

from domain.model.qlib_direct import _training_diagnostics
from domain.model.orchestrator import _deterministic_round_synthesis
from domain.model.scoring import (
    improvement_vs_reference,
    meaningfully_improves,
    round_research_metrics,
)


class _FakeBooster:
    best_iteration = 3

    @staticmethod
    def current_iteration() -> int:
        return 5


def test_training_diagnostics_preserves_real_curve_evidence():
    result = _training_diagnostics(
        {
            "train": {"l2": [1.0, 0.8, 0.6, 0.5, 0.4]},
            "valid": {"l2": [1.1, 0.9, 0.7, 0.72, 0.75]},
        },
        booster=_FakeBooster(),
        configured_n_estimators=100,
        early_stopping_rounds=2,
    )

    assert result["available"] is True
    assert result["metric_name"] == "l2"
    assert result["best_iteration"] == 3
    assert result["trees_built"] == 5
    assert result["early_stopped"] is True
    assert result["train_valid_gap_at_best"] == pytest.approx(0.1)
    assert result["valid_deterioration_after_best"] == pytest.approx(0.05)
    assert result["curves"]["valid"] == [1.1, 0.9, 0.7, 0.72, 0.75]
    assert any(row["iteration"] == 3 for row in result["curve_checkpoints"])


def _seed(seed: int, score: float, ann: float, ir: float, drawdown: float, best_iteration: int) -> dict:
    return {
        "seed": seed,
        "score": {"sota_score": score},
        "metrics": {
            "excess_annualized_ret_with_cost": ann,
            "excess_information_ratio_with_cost": ir,
            "max_drawdown": drawdown,
            "rank_ic": 0.03,
            "rank_icir": 0.25,
            "turnover": 0.5,
            "training_diagnostics": {
                "best_iteration": best_iteration,
                "best_iteration_ratio": best_iteration / 1000,
                "early_stopped": True,
                "train_valid_gap_at_best": 0.02,
                "valid_deterioration_after_best": 0.01,
            },
        },
    }


def test_confirmed_round_research_metrics_use_three_seed_median():
    reference = round_research_metrics(
        [_seed(42, 60, 0.20, 0.8, -0.20, 300), _seed(17, 62, 0.22, 0.9, -0.18, 320), _seed(83, 64, 0.24, 1.0, -0.16, 340)]
    )
    candidate = round_research_metrics(
        [_seed(42, 61, 0.21, 0.9, -0.18, 350), _seed(17, 64, 0.25, 1.0, -0.17, 360), _seed(83, 67, 0.28, 1.1, -0.15, 370)]
    )
    delta = improvement_vs_reference(candidate, reference)

    assert reference["research_score"] == 62
    assert candidate["research_score"] == 64
    assert delta["research_score"] == 2
    assert delta["median_abs_max_drawdown"] > 0
    assert meaningfully_improves(candidate, reference) is True
    assert candidate["training_summary"]["median_best_iteration"] == 360


def test_three_consecutive_non_improvements_checkpoint_stop():
    synthesis = _deterministic_round_synthesis(
        {
            "round_group_id": "round-3",
            "reference_round_group_id": "best-round",
            "round_metrics": {"research_score": 61.0},
            "improvement_vs_reference": {"research_score": -1.0},
            "improved_platform_best": False,
        },
        consecutive_no_improvement=3,
    )

    assert synthesis["decision"] == "checkpoint_stop"
    assert synthesis["next"] == "human_review"
    assert synthesis["llm_call_status"] == "not_called"
