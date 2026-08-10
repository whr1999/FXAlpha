from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "experiments" / "model_rank_sandbox" / "run_isolated_rank_screen.py"
SPEC = importlib.util.spec_from_file_location("isolated_rank_screen", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _index() -> pd.MultiIndex:
    return pd.MultiIndex.from_product(
        [pd.to_datetime(["2026-01-02", "2026-01-05"]), [f"s{i:03d}" for i in range(120)]],
        names=["datetime", "instrument"],
    )


def test_candidate_matrix_is_predeclared_and_complete():
    rows = MODULE.candidate_matrix()
    assert len(rows) == 12
    assert len({row.candidate_id for row in rows}) == 12
    assert {row.window for row in rows} == {"expanding", "recent36m"}
    assert {row.objective for row in rows} == {"ridge", "regression", "huber", "rank_xendcg"}


def test_rank_objective_uses_query_scale_regularization():
    params = MODULE._common_lgb_params("rank_xendcg", smoke=True)
    assert params["lambda_l1"] == 0.0
    assert params["lambda_l2"] == 1.0
    assert params["min_data_in_leaf"] == 50


def test_relevance_label_targets_top20_top50_and_top100():
    index = _index()
    label = pd.Series(np.tile(np.arange(120, dtype="float64"), 2), index=index)
    relevance = MODULE._relevance_label(label)
    for _dt, group in relevance.groupby(level="datetime"):
        assert int((group == 4).sum()) == 20
        assert int((group == 3).sum()) == 30
        assert int((group == 2).sum()) == 50
        assert int((group == 1).sum()) == 0
        assert int((group == 0).sum()) == 20


def test_group_sizes_match_daily_queries():
    sizes = MODULE._group_sizes(_index())
    assert sizes.tolist() == [120, 120]


def test_real_snapshot_calendar_is_normalized_and_sorted():
    if not MODULE.DEFAULT_FEATURE_SET_ID:
        pytest.skip("set FXALPHA_SANDBOX_FEATURE_SET_ID to run the private snapshot check")
    manifest = MODULE.SNAPSHOT_ROOT / MODULE.DEFAULT_FEATURE_SET_ID / "manifest.json"
    if not manifest.exists():
        pytest.skip("production feature snapshot is not part of the public source repository")
    parquet_path, _manifest = MODULE._snapshot(MODULE.DEFAULT_FEATURE_SET_ID)
    dates = MODULE._calendar(parquet_path)
    assert dates.is_monotonic_increasing
    assert len(dates) > 1000
    assert all(timestamp == timestamp.normalize() for timestamp in dates)


def test_current_weights_are_daily_mean_normalized():
    index = _index()
    label = pd.Series(np.tile(np.linspace(-2.0, 2.0, 120), 2), index=index)
    weights = pd.Series(MODULE._current_weights(label), index=index)
    means = weights.groupby(level="datetime").mean()
    assert np.allclose(means.to_numpy(), 1.0)
    assert float(weights.max()) > 1.0


def test_sandbox_output_cannot_escape(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    monkeypatch.setattr(MODULE, "SANDBOX_ROOT", allowed)
    out = MODULE._safe_output_dir("run-1")
    assert out.parent == allowed.resolve()
    try:
        MODULE._safe_output_dir("../escape")
    except ValueError as exc:
        assert "escaped" in str(exc)
    else:
        raise AssertionError("sandbox path escape was accepted")
