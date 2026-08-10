from __future__ import annotations

import pandas as pd
import pytest

from domain.model.reweight import FXAlphaTopBottomRankReweighter, make_sample_reweighter, validate_sample_weight_policy


def test_default_reweight_policy_is_strict():
    assert validate_sample_weight_policy("top50_smooth2_bottom50_smooth1p5_mean_norm")["passed"] is True
    bad = validate_sample_weight_policy(
        "top50_smooth2_bottom50_smooth1p5_mean_norm",
        {"top_n": 10},
    )
    assert bad["passed"] is False
    assert "top_n_mismatch" in bad["errors"][0]


def test_illegal_reweight_policy_blocks_before_qrun():
    result = validate_sample_weight_policy("experimental_weighting")
    assert result["passed"] is False
    assert result["errors"] == ["unsupported_sample_weight_policy:experimental_weighting"]


def test_sticky_reweight_policy_maps_to_default_reweighter():
    reweighter = make_sample_reweighter("sticky", {})

    assert isinstance(reweighter, FXAlphaTopBottomRankReweighter)
    index = pd.MultiIndex.from_product(
        [pd.date_range("2026-01-01", periods=1), ["000001sz", "000002sz", "000003sz"]],
        names=["datetime", "instrument"],
    )
    data = pd.DataFrame({("label", "LABEL0"): [0.2, 0.0, -0.1]}, index=index)
    data.columns = pd.MultiIndex.from_tuples(data.columns)
    weights = reweighter.reweight(data)
    assert len(weights) == 3
    assert float(weights.mean()) == pytest.approx(1.0)
