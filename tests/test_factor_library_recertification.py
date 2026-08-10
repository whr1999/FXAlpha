from domain.factor_research.library_recertification import classify_lifecycle, official_quality_pass


def _result(*, quick=85.0, deep=86.0, ic=0.04, icir=0.5, status="success", flipped=False):
    return {
        "status": status,
        "quick_score": quick,
        "deep_score": deep,
        "direction_review": flipped,
        "backtest_summary": {"rank_ic_mean": ic, "rank_ic_ir": icir},
    }


def test_official_quality_pass_uses_current_gate_thresholds():
    assert official_quality_pass(_result()) is True
    assert official_quality_pass(_result(ic=0.019)) is False
    assert official_quality_pass(_result(icir=0.29)) is False
    assert official_quality_pass(_result(quick=69.9)) is False
    assert official_quality_pass(_result(deep=79.9)) is False


def test_retired_factor_that_recovers_is_restore_candidate():
    advice = classify_lifecycle(_result(), {"status": "retired", "metadata": {"retired_reason": "old_low_score"}})
    assert advice["advice"] == "restore_candidate"


def test_existing_active_factor_uses_79_5_retention_tolerance():
    retained = classify_lifecycle(_result(deep=79.5), {"status": "active", "metadata": {}})
    review = classify_lifecycle(_result(deep=79.4), {"status": "active", "metadata": {}})
    assert retained["advice"] == "keep_active"
    assert retained["reason"] == "passes_active_retention_tolerance_79_5"
    assert review["advice"] == "active_review"


def test_retired_factor_does_not_use_active_retention_tolerance():
    advice = classify_lifecycle(_result(deep=79.9), {"status": "retired", "metadata": {}})
    assert advice["advice"] == "keep_retired"


def test_prior_st_retirement_requires_policy_review_even_if_score_recovers():
    advice = classify_lifecycle(
        _result(),
        {"status": "retired", "retire_reason": "full_history_st_exposure", "metadata": {}},
    )
    assert advice["advice"] == "policy_review"


def test_direction_is_sent_to_expression_review_not_auto_restored():
    advice = classify_lifecycle(_result(flipped=True), {"status": "retired", "metadata": {}})
    assert advice["advice"] == "direction_review"


def test_weak_flipped_factor_is_not_promoted_to_expression_review():
    advice = classify_lifecycle(
        _result(quick=29.5, deep=34.7, ic=-0.005, icir=-0.1, flipped=True),
        {"status": "retired", "metadata": {}},
    )
    assert advice["advice"] == "keep_retired"


def test_materially_weak_active_factor_is_exit_candidate():
    advice = classify_lifecycle(
        _result(quick=50.0, deep=60.0, ic=0.005, icir=0.1),
        {"status": "active", "metadata": {}},
    )
    assert advice["advice"] == "exit_candidate"
