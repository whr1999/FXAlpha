import json

from domain.factor_research import auto_import, quality_gate
from storage.factor_registry import FactorRegistry


def _novelty_guard(**overrides):
    base = {
        "allowed": True,
        "reason": "novel_increment",
        "novelty_score": 0.6,
        "max_existing_pearson": 0.1,
        "max_existing_rank_corr": 0.2,
        "thresholds": {"pearson": 0.75, "rank_corr": 0.8},
    }
    base.update(overrides)
    return base


def _st_exposure_guard(**overrides):
    base = {
        "available": True,
        "passed": True,
        "reason": "st_exposure_passed",
        "avg_top50_ratio": 0.0,
        "p95_top50_ratio": 0.0,
        "latest_top50_ratio": 0.0,
        "sample_days": 20,
        "thresholds": {"avg_top50_ratio": 0.05, "p95_top50_ratio": 0.15},
    }
    base.update(overrides)
    return base


def _combined_guard(**overrides):
    base = {
        "allowed": True,
        "reason": "novelty_and_st_exposure_passed",
        "novelty_allowed": True,
        "st_exposure_passed": True,
    }
    base.update(overrides)
    return base


def _deep_score_parts(**overrides):
    base = {
        "component_scores": {
            "quick_core": 90.0,
            "anti_overfit": 85.0,
            "rolling": 76.0,
            "adversarial": 80.0,
        }
    }
    base.update(overrides)
    return base


def _candidate(**overrides):
    base = {
        "name": "EvidenceCandidate",
        "expression": "rank(close)",
        "score": 90,
        "quick_score": 90,
        "grade": "A",
        "screening_stage": "deep_validation",
        "holding_period_days": 5,
        "economic_thesis": {
            "name": "Intraday support",
            "market_mechanism": "Closing support can indicate short-horizon demand absorption.",
            "behavioral_or_risk_rationale": "Liquidity friction and delayed reaction.",
            "novelty_angle": "Uses a distinct support channel.",
        },
        "hypothesis": "Stocks with stronger support should outperform over the next 5 trading days.",
        "backtest_summary": {
            "ic_mean": 0.05,
            "ic_ir": 0.8,
            "rank_ic_mean": 0.04,
            "rank_ic_ir": 0.7,
            "sharpe": 1.2,
            "annual_return": 0.2,
            "turnover": 0.2,
            "max_drawdown": -0.1,
        },
        "anti_overfit": {
            "score": 85,
            "risk_flag": "normal",
            "autocorrelation": {"stock_lag1_mean": 0.2, "ic_lag1_autocorr": 0.1},
        },
        "adversarial_validation": {"score": 80, "passed_count": 4, "total_count": 4},
        "novelty_guard": _novelty_guard(),
        "st_exposure_guard": _st_exposure_guard(),
        "combined_guard": _combined_guard(),
        "rolling_validation": {
            "status": "ok",
            "score": 76.0,
            "summary": {"status": "ok", "n_windows": 3, "mean_test_ic": 0.04, "mean_test_ir": 0.7},
            "decay_analysis": {"status": "stable", "mean_decay": 0.1},
            "windows": [{"test_ic": 0.04, "test_ir": 0.7}],
        },
    }
    base.update(overrides)
    return base


def test_quality_gate_preserves_novelty_guard_from_nested_evidence(monkeypatch):
    novelty = _novelty_guard()
    candidate = _candidate(deep_validation={"novelty_correlation": novelty})

    monkeypatch.setattr(quality_gate, "assess_active_pool_novelty", lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""})
    monkeypatch.setattr(quality_gate, "_compute_deep_score", lambda candidate, quick_score: (85.0, {"official_score": 85.0}))

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2023-01-01",
        end_date="2026-04-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    adopted = report["adopted"]
    assert len(adopted) == 1
    assert adopted[0]["novelty_guard"] == novelty
    assert adopted[0]["screening"]["novelty_guard"] == novelty
    assert adopted[0]["deep_validation"]["novelty_correlation"] == novelty
    assert adopted[0]["screening"]["anti_overfit_score"] == 85


def test_quality_gate_counts_low_information_gain(monkeypatch):
    novelty = _novelty_guard(
        allowed=False,
        reason="low_information_gain",
        novelty_score=0.0,
        max_existing_pearson=0.9,
        max_existing_rank_corr=0.9,
    )
    candidate = _candidate(novelty_guard=novelty)

    monkeypatch.setattr(quality_gate, "assess_active_pool_novelty", lambda candidates, **_: {"keepers": [], "dropped": candidates, "details": [], "feedback": "low info"})
    monkeypatch.setattr(quality_gate, "_compute_deep_score", lambda candidate, quick_score: (85.0, {"official_score": 85.0}))

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2023-01-01",
        end_date="2026-04-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert report["adopted"] == []
    assert report["reason_counts"]["low_information_gain"] == 1
    assert report["rejected"][0]["screening"]["novelty_guard"] == novelty


def test_st_exposure_advisory_does_not_block_quality_or_import(monkeypatch):
    st_guard = _st_exposure_guard(
        passed=False,
        reason="st_exposure_veto:avg_top50_ratio_ge_0_05",
        avg_top50_ratio=0.08,
        p95_top50_ratio=0.18,
        mode="advisory",
        scope="counterfactual_all_market",
        label="distress_proxy_exposure",
        advisory_flag="distress_proxy_exposure",
    )
    candidate = _candidate(
        st_exposure_guard=st_guard,
        combined_guard=_combined_guard(
            reason="novelty_passed_st_exposure_advisory",
            st_exposure_passed=False,
            st_exposure_mode="advisory",
            advisory_tags=["distress_proxy_exposure"],
        ),
        risk_tags=["distress_proxy_exposure"],
    )

    monkeypatch.setattr(quality_gate, "get_live_st_exposure_guard_mode", lambda: "advisory")
    monkeypatch.setattr(auto_import, "get_live_st_exposure_guard_mode", lambda: "advisory")
    monkeypatch.setattr(quality_gate, "assess_active_pool_novelty", lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""})
    monkeypatch.setattr(
        quality_gate,
        "_compute_deep_score",
        lambda candidate, quick_score: (
            85.0,
            {
                "official_score": 85.0,
                "component_scores": {
                    "quick_core": 90.0,
                    "anti_overfit": 85.0,
                    "rolling": 76.0,
                    "adversarial": 80.0,
                },
            },
        ),
    )

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2023-01-01",
        end_date="2026-04-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert len(report["adopted"]) == 1
    adopted = report["adopted"][0]
    assert "st_exposure_veto" not in adopted["veto_reasons"]
    assert adopted["st_exposure_guard"]["advisory_flag"] == "distress_proxy_exposure"
    assert auto_import._quality_block_reason(adopted) == ""
    metadata = auto_import._compact_quality_metadata(adopted, {"ic_mean": 0.05}, None)
    assert metadata["risk_tags"] == ["distress_proxy_exposure"]
    assert metadata["st_exposure_guard"]["mode"] == "advisory"


def test_missing_economic_evidence_does_not_block_quality_gate(monkeypatch):
    candidate = _candidate(economic_thesis={}, hypothesis="")
    candidate["novelty_guard"] = _novelty_guard()

    monkeypatch.setattr(quality_gate, "assess_active_pool_novelty", lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""})
    monkeypatch.setattr(quality_gate, "_compute_deep_score", lambda candidate, quick_score: (85.0, {"official_score": 85.0}))

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2023-01-01",
        end_date="2026-04-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert len(report["adopted"]) == 1
    assert "missing_economic_thesis" not in report["adopted"][0].get("veto_reasons", [])
    assert "missing_hypothesis" not in report["adopted"][0].get("veto_reasons", [])


def test_quality_gate_accepts_run_backtest_metrics_shape(monkeypatch):
    candidate = _candidate(
        backtest_summary=None,
        backtest={
            "report_path": "/tmp/backtest_report.html",
            "metrics": {
                "ic_mean": 0.05,
                "ic_ir": 0.8,
                "rank_ic_mean": 0.04,
                "rank_ic_ir": 0.7,
                "annual_return": 0.2,
                "top_group_sharpe": 1.2,
                "max_drawdown": -0.1,
                "turnover": 0.2,
                "monotonicity_score": 1.0,
                "cagr": 0.18,
                "sharpe": 1.15,
            },
        },
    )
    candidate["novelty_guard"] = _novelty_guard()

    monkeypatch.setattr(
        quality_gate,
        "assess_active_pool_novelty",
        lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""},
    )
    monkeypatch.setattr(quality_gate, "_compute_deep_score", lambda candidate, quick_score: (85.0, {"official_score": 85.0}))

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2023-01-01",
        end_date="2026-04-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert len(report["adopted"]) == 1
    adopted = report["adopted"][0]
    assert adopted["backtest_summary"]["annual_return"] == 0.2
    assert adopted["backtest_summary"]["top_group_sharpe"] == 1.2
    assert "empty_backtest" not in adopted.get("veto_reasons", [])


def test_quality_gate_accepts_dict_shaped_quick_score(monkeypatch):
    candidate = _candidate(
        score={"score": 88.0},
        quick_score={"value": 88.0},
        novelty_guard=_novelty_guard(),
    )

    monkeypatch.setattr(
        quality_gate,
        "assess_active_pool_novelty",
        lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""},
    )
    monkeypatch.setattr(quality_gate, "_compute_deep_score", lambda candidate, quick_score: (85.0, {"official_score": 85.0}))

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2023-01-01",
        end_date="2026-04-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert len(report["adopted"]) == 1
    assert report["adopted"][0]["quick_score"] == 88.0


def test_active_metadata_backfill_marks_missing_without_changing_factor_state(tmp_path):
    db_path = tmp_path / "factor_registry.db"
    registry = FactorRegistry(db_path=db_path)
    factor_id = registry.register(
        name="Legacy",
        expression="rank(close)",
        status="active",
        metrics={"ic_mean": 0.03, "icir": 0.6},
        metadata={
            "screening": {"novelty_guard": {"allowed": True}},
            "anti_overfit_summary": {"score": 75},
        },
    )

    result = registry.backfill_active_evidence_metadata()
    row = registry.get(factor_id)

    assert result["active_checked"] == 1
    assert row["expression"] == "rank(close)"
    assert row["status"] == "active"
    metadata = row["metadata"]
    assert metadata["novelty_guard"] == {"allowed": True}
    assert metadata["anti_overfit"] == {"score": 75}
    assert "metadata_incomplete" not in metadata
    assert "metadata_incomplete_reasons" not in metadata


def test_auto_import_blocks_thin_gate_payload_without_deep_evidence():
    candidate = {
        "expression": "rank(close)",
        "gate_result": {"passed": True, "score": 90.0, "deep_score": 90.0, "holding_period_days": 5},
        "novelty_guard": _novelty_guard(),
        "st_exposure_guard": _st_exposure_guard(),
        "combined_guard": _combined_guard(),
        "holding_period_days": 5,
        "metrics": {"quick_score": 80.0, "deep_score": 90.0},
    }

    assert auto_import._quality_block_reason(candidate) == "missing_deep_validation"


def test_auto_import_blocks_deep_payload_without_official_registry_metrics():
    candidate = {
        "expression": "rank(close)",
        "gate_result": {"passed": True, "score": 90.0, "deep_score": 90.0, "holding_period_days": 5},
        "novelty_guard": _novelty_guard(),
        "st_exposure_guard": _st_exposure_guard(),
        "combined_guard": _combined_guard(),
        "holding_period_days": 5,
        "deep_validation": {"deep_score": 90.0, "score_parts": _deep_score_parts()},
        "rolling_validation": {
            "status": "ok",
            "score": 76.0,
            "summary": {"status": "ok", "n_windows": 3},
            "decay_analysis": {"status": "stable"},
            "windows": [{"test_ic": 0.04}],
        },
        "metrics": {"quick_score": 80.0, "deep_score": 90.0},
    }

    assert auto_import._quality_block_reason(candidate).startswith("missing_registry_metrics:")


def test_quality_gate_blocks_missing_rolling_validation(monkeypatch):
    candidate = _candidate()
    candidate.pop("rolling_validation")
    candidate["novelty_guard"] = _novelty_guard()

    monkeypatch.setattr(
        quality_gate,
        "assess_active_pool_novelty",
        lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""},
    )
    monkeypatch.setattr(quality_gate, "_compute_deep_score", lambda candidate, quick_score: (85.0, {"official_score": 85.0}))

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert report["adopted"] == []
    assert "missing_rolling_validation" in report["rejected"][0]["veto_reasons"]


def test_quality_gate_no_longer_blocks_c_grade_candidate(monkeypatch):
    candidate = _candidate(grade="C", score=55.0, quick_score=55.0)
    candidate["novelty_guard"] = _novelty_guard()

    monkeypatch.setattr(
        quality_gate,
        "assess_active_pool_novelty",
        lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""},
    )
    monkeypatch.setattr(quality_gate, "_compute_deep_score", lambda candidate, quick_score: (85.0, {"official_score": 85.0}))

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert len(report["adopted"]) == 1
    assert "quick_grade_below_deep_validation_threshold" not in report["adopted"][0].get("veto_reasons", [])


def test_quality_gate_blocks_skipped_rolling_but_not_unstable_rolling(monkeypatch):
    monkeypatch.setattr(
        quality_gate,
        "assess_active_pool_novelty",
        lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""},
    )
    monkeypatch.setattr(quality_gate, "_compute_deep_score", lambda candidate, quick_score: (85.0, {"official_score": 85.0}))

    skipped = _candidate(rolling_validation={"status": "skipped_short_window", "summary": {"status": "skipped_short_window"}})
    unstable = _candidate(
        rolling_validation={
            "status": "ok",
            "score": 80.0,
            "summary": {"status": "ok", "n_windows": 3},
            "decay_analysis": {"status": "unstable"},
            "windows": [{"test_ic": 0.02}],
        }
    )

    skipped_report = quality_gate.evaluate_candidate_quality(
        [skipped],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )
    unstable_report = quality_gate.evaluate_candidate_quality(
        [unstable],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert skipped_report["adopted"] == []
    assert "missing_rolling_score" in skipped_report["rejected"][0]["veto_reasons"]
    assert len(unstable_report["adopted"]) == 1


def test_quality_gate_carries_nested_rolling_validation(monkeypatch):
    rolling = {
        "status": "ok",
        "score": 81.0,
        "summary": {"status": "ok", "n_windows": 3, "mean_test_ic": 0.04, "mean_test_ir": 0.7},
        "decay_analysis": {"status": "stable"},
        "windows": [{"test_ic": 0.04}],
    }
    candidate = _candidate(rolling_validation=None, deep_validation={"rolling_validation": rolling})
    candidate["novelty_guard"] = _novelty_guard()

    monkeypatch.setattr(
        quality_gate,
        "assess_active_pool_novelty",
        lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""},
    )
    monkeypatch.setattr(quality_gate, "_compute_deep_score", lambda candidate, quick_score: (85.0, {"official_score": 85.0}))

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert len(report["adopted"]) == 1
    assert "rolling_validation" not in report["adopted"][0]["veto_reasons"]
    assert report["adopted"][0]["screening"]["novelty_guard"]["allowed"] is True


def test_quality_gate_does_not_add_low_rolling_score_hard_veto(monkeypatch):
    candidate = _candidate(
        rolling_validation={
            "status": "ok",
            "score": 5.0,
            "summary": {"status": "ok", "n_windows": 3},
            "decay_analysis": {"status": "unstable"},
            "windows": [{"test_ic": 0.001}],
        }
    )

    monkeypatch.setattr(
        quality_gate,
        "assess_active_pool_novelty",
        lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""},
    )
    monkeypatch.setattr(quality_gate, "_compute_deep_score", lambda candidate, quick_score: (85.0, {"official_score": 85.0}))

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert len(report["adopted"]) == 1
    assert not any("rolling" in reason for reason in report["adopted"][0]["veto_reasons"])


def test_auto_import_blocks_missing_rolling_validation():
    candidate = _candidate(
        gate_result={
            "passed": True,
            "score": 85.0,
            "deep_score": 85.0,
            "holding_period_days": 5,
            "ic": 0.05,
            "ir": 0.8,
            "rank_ic": 0.04,
            "rank_ir": 0.7,
            "sharpe": 1.2,
        },
        novelty_guard=_novelty_guard(),
        deep_validation={"deep_score": 85.0, "score_parts": _deep_score_parts()},
        rolling_validation=None,
    )

    assert auto_import._quality_block_reason(candidate) == "missing_rolling_validation"


def test_auto_import_ignores_c_grade_and_unstable_rolling_diagnostic():
    c_grade = _candidate(
        grade="C",
        score=55.0,
        quick_score=55.0,
        gate_result={"passed": True, "score": 85.0, "deep_score": 85.0, "holding_period_days": 5, "qgpt_grade": "C"},
        deep_validation={"deep_score": 85.0, "score_parts": _deep_score_parts()},
    )
    unstable = _candidate(
        gate_result={"passed": True, "score": 85.0, "deep_score": 85.0, "holding_period_days": 5, "qgpt_grade": "A"},
        deep_validation={"deep_score": 85.0, "score_parts": _deep_score_parts()},
        rolling_validation={
            "status": "ok",
            "score": 80.0,
            "summary": {"status": "ok", "n_windows": 3},
            "decay_analysis": {"status": "unstable"},
            "windows": [{"test_ic": 0.02}],
        },
    )

    assert auto_import._quality_block_reason(c_grade) == ""
    assert auto_import._quality_block_reason(unstable) == ""


def test_auto_import_accepts_complete_official_evidence_payload():
    candidate = _candidate(
        gate_result={
            "passed": True,
            "score": 85.0,
            "deep_score": 85.0,
            "holding_period_days": 5,
            "ic": 0.05,
            "ir": 0.8,
            "rank_ic": 0.04,
            "rank_ir": 0.7,
            "sharpe": 1.2,
        },
        novelty_guard=_novelty_guard(),
        deep_validation={"deep_score": 85.0, "score_parts": _deep_score_parts()},
    )

    assert auto_import._quality_block_reason(candidate) == ""


def test_auto_import_persists_long_only_direction_metadata():
    candidate = _candidate(
        key_metrics={"flipped": True},
        best_long_only_group_metrics={
            "selected_group_is_flipped_low_side": True,
            "annual_return": 1.23,
            "sharpe": 2.5,
            "max_drawdown": -0.12,
            "turnover": 0.4,
        },
    )

    metadata = auto_import._compact_quality_metadata(candidate, auto_import._complete_metrics(candidate), None)

    assert metadata["selected_group_is_flipped_low_side"] is True
    assert metadata["long_only_direction"] == {
        "selected_group_is_flipped_low_side": True,
        "long_only_side": "low_factor_values",
        "source": "quantgpt_backtest_best_long_only_group",
        "annual_return": 1.23,
        "sharpe": 2.5,
        "max_drawdown": -0.12,
        "turnover": 0.4,
    }


def test_quality_gate_blocks_failed_st_exposure(monkeypatch):
    candidate = _candidate(
        st_exposure_guard=_st_exposure_guard(
            passed=False,
            reason="st_exposure_veto:avg_top50_ratio_ge_0_05",
            avg_top50_ratio=0.12,
        ),
        combined_guard=_combined_guard(
            allowed=False,
            reason="st_exposure_veto:avg_top50_ratio_ge_0_05",
            st_exposure_passed=False,
        ),
    )

    monkeypatch.setattr(
        quality_gate,
        "assess_active_pool_novelty",
        lambda candidates, **_: {"keepers": [], "dropped": candidates, "details": [], "feedback": "st exposure"},
    )
    monkeypatch.setattr(quality_gate, "get_live_st_exposure_guard_mode", lambda: "hard")
    monkeypatch.setattr(quality_gate, "_compute_deep_score", lambda candidate, quick_score: (85.0, {"official_score": 85.0}))

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert report["adopted"] == []
    assert "st_exposure_veto" in report["rejected"][0]["veto_reasons"]


def test_auto_import_blocks_failed_st_exposure():
    candidate = _candidate(
        gate_result={"passed": True, "score": 85.0, "deep_score": 85.0, "holding_period_days": 5},
        deep_validation={"deep_score": 85.0, "score_parts": _deep_score_parts()},
        st_exposure_guard=_st_exposure_guard(passed=False, reason="st_exposure_veto:p95_top50_ratio_ge_0_15", mode="hard"),
        combined_guard=_combined_guard(allowed=False, reason="st_exposure_veto:p95_top50_ratio_ge_0_15"),
    )

    assert auto_import._quality_block_reason(candidate).startswith("st_exposure_blocked:")


def test_compute_deep_score_reuses_quick_score():
    candidate = _candidate(quick_score=88.0)

    deep_score, score_parts = quality_gate._compute_deep_score(candidate, quick_score=88.0)

    assert deep_score == 84.3
    assert score_parts["deep_score_policy_version"] == "deep_score_v2_55_15_20_10"
    assert score_parts["component_scores"] == {
        "quick_core": 88.0,
        "anti_overfit": 85.0,
        "rolling": 76.0,
        "adversarial": 80.0,
    }
    assert score_parts["weighted_contributions"] == {
        "quick_core": 48.4,
        "anti_overfit": 12.75,
        "rolling": 15.2,
        "adversarial": 8.0,
    }


def test_novelty_is_required_guard_but_not_numeric_deep_score_bonus():
    low_novelty = _candidate(quick_score=88.0, novelty_guard=_novelty_guard(allowed=True, novelty_score=0.1))
    high_novelty = _candidate(quick_score=88.0, novelty_guard=_novelty_guard(allowed=True, novelty_score=0.9))

    low_score, low_parts = quality_gate._compute_deep_score(low_novelty, quick_score=88.0)
    high_score, high_parts = quality_gate._compute_deep_score(high_novelty, quick_score=88.0)

    assert low_score == high_score
    assert "novelty_bonus" not in low_parts["component_scores"]
    assert "novelty_bonus" not in high_parts["weighted_contributions"]


def test_quality_gate_blocks_missing_quick_score(monkeypatch):
    candidate = _candidate(quick_score=None, score=None)

    monkeypatch.setattr(
        quality_gate,
        "assess_active_pool_novelty",
        lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""},
    )

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert report["adopted"] == []
    assert "missing_quick_score" in report["rejected"][0]["veto_reasons"]


def test_quality_gate_blocks_missing_raw_deep_scores(monkeypatch):
    candidate = _candidate(
        anti_overfit={"risk_flag": "normal"},
        adversarial_validation={"passed_count": 4, "total_count": 4},
        novelty_guard=_novelty_guard(novelty_score=None),
    )

    monkeypatch.setattr(
        quality_gate,
        "assess_active_pool_novelty",
        lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""},
    )

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    veto = report["rejected"][0]["veto_reasons"]
    assert "missing_anti_overfit_score" in veto
    assert "missing_adversarial_score" in veto
    assert "missing_novelty_score" in veto


def test_quality_gate_uses_adversarial_score_not_pass_count(monkeypatch):
    candidate = _candidate(
        adversarial_validation={"score": 55, "passed_count": 4, "total_count": 4},
        novelty_guard=_novelty_guard(),
    )

    monkeypatch.setattr(
        quality_gate,
        "assess_active_pool_novelty",
        lambda candidates, **_: {"keepers": candidates, "dropped": [], "details": [], "feedback": ""},
    )

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert report["adopted"] == []
    assert "adversarial_failed" in report["rejected"][0]["veto_reasons"]


def test_auto_import_blocks_inconsistent_gate_score():
    candidate = _candidate(
        gate_result={"passed": True, "score": 84.0, "deep_score": 85.0, "holding_period_days": 5},
        deep_validation={"deep_score": 85.0, "score_parts": _deep_score_parts()},
    )

    assert auto_import._quality_block_reason(candidate) == "inconsistent_gate_score"


def test_registry_persists_rank_icir_for_rankic_sorting(tmp_path):
    db_path = tmp_path / "factor_registry.db"
    registry = FactorRegistry(db_path=db_path)
    factor_id = registry.register(
        name="RankICIR",
        expression="rank(open)",
        status="active",
        metrics={
            "ic_mean": 0.04,
            "icir": 0.7,
            "rank_ic": 0.05,
            "rank_icir": 0.9,
            "sharpe": 1.1,
        },
        metadata={"evidence_schema_version": "fxalpha_evidence_v1"},
    )

    row = registry.get(factor_id)
    rows, count = registry.list_all(sort_by="rank_icir", limit=10)

    assert count == 1
    assert "rank_ic_mean" not in row
    assert row["rank_icir"] == 0.9
    assert "evidence_schema_version" not in row
    assert row["metadata"]["evidence_schema_version"] == "fxalpha_evidence_v1"
    assert rows[0]["factor_id"] == factor_id


def test_quality_gate_thresholds_use_rankic_when_pearson_is_weak():
    checks = quality_gate._threshold_checks(
        {
            "ic_mean": 0.005,
            "ic_ir": 0.08,
            "rank_ic_mean": 0.055,
            "rank_ic_ir": 0.65,
        },
        min_abs_ic=0.02,
        min_ir=0.3,
    )

    assert checks["ic_abs"]["passed"] is True
    assert checks["ir_abs"]["passed"] is True
    assert checks["ic_abs"]["value"] == 0.055
    assert checks["ir_abs"]["value"] == 0.65
