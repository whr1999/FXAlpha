import pandas as pd

from domain.model.qlib_strategy import (
    rank_current_holdings_with_missing_score_priority,
    rank_scores_with_instrument_tiebreak,
    tradable_buy_scores,
)


def test_strategy_prioritizes_scoreless_holdings_for_sell_side():
    pred_score = pd.Series(
        {
            "000001sz": 0.9,
            "000002sz": 0.1,
            "000004sz": 0.8,
        }
    )

    ranked = rank_current_holdings_with_missing_score_priority(
        pred_score,
        ["000001sz", "000002sz", "000003sz"],
    )

    assert ranked.tolist() == ["000001sz", "000002sz", "000003sz"]
    assert ranked[-1] == "000003sz"
    assert "000003sz" not in tradable_buy_scores(pred_score.reindex(["000003sz", "000004sz"])).index


def test_score_ties_use_instrument_as_order_independent_tiebreak():
    scores_a = pd.Series(
        [0.5, 0.9, 0.9, 0.5],
        index=["000004sz", "000003sz", "000001sz", "000002sz"],
    )
    scores_b = scores_a.reindex(["000002sz", "000001sz", "000004sz", "000003sz"])

    expected = ["000001sz", "000003sz", "000002sz", "000004sz"]
    assert rank_scores_with_instrument_tiebreak(scores_a).tolist() == expected
    assert rank_scores_with_instrument_tiebreak(scores_b).tolist() == expected
