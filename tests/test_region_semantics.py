from domain.factor_research.region_semantics import (
    analyze_expression,
    build_region_profile,
    semantic_signature,
)
from services.factor_map_service import _region_guidance


def test_semantic_signature_ignores_windows_scalars_and_monotonic_wrappers():
    left = "rank(ts_mean(net_mf_amount,10)) * tanh(3*rank(-ts_std(turnover_rate,5)))"
    right = "tanh(7*rank(ts_mean(net_mf_amount,20))) * rank(-ts_std(turnover_rate,12))"

    assert semantic_signature(left) == semantic_signature(right)


def test_semantic_signature_changes_when_information_relation_changes():
    baseline = "rank(ts_mean(net_mf_amount,10)) * rank(-ts_std(turnover_rate,5))"
    changed_field = "rank(ts_mean(lg_net_amount,10)) * rank(-ts_std(turnover_rate,5))"
    changed_relation = "rank(ts_corr(net_mf_amount,turnover_rate,10))"

    assert semantic_signature(baseline) != semantic_signature(changed_field)
    assert semantic_signature(baseline) != semantic_signature(changed_relation)


def test_expression_analysis_normalizes_field_aliases_and_explains_condition():
    analysis = analyze_expression(
        "where(margin_balance < ts_mean(margin_balance,60), "
        "rank((high-close)/max(high-low,0.01)), rank(-amount))"
    )

    assert analysis["available"] is True
    assert "borrow_money_bal" in analysis["fields"]
    assert "margin_balance" not in analysis["fields"]
    assert analysis["combination_form"] == "条件触发"
    assert "日内高低区间" in analysis["summary"]


def test_region_profile_uses_shared_business_structure_not_representative_name():
    region = {
        "region_uid": "region_test",
        "size": 3,
        "representative": {
            "factor_id": "f1",
            "name": "opaque_internal_name",
            "expression": "rank(ts_mean(net_mf_amount,10)) * rank(-ts_std(turnover_rate,5))",
        },
        "members": [
            {
                "factor_id": "f1",
                "expression": "rank(ts_mean(net_mf_amount,10)) * rank(-ts_std(turnover_rate,5))",
            },
            {
                "factor_id": "f2",
                "expression": "rank(ts_mean(net_mf_amount,20)) * rank(-ts_std(turnover_rate,10))",
            },
            {
                "factor_id": "f3",
                "expression": "rank(ts_mean(net_mf_amount,15)) * rank(-ts_delta(turnover_rate,10))",
            },
        ],
    }

    profile = build_region_profile(region)

    assert profile["semantic_status"] == "coherent"
    assert "主力资金净流入持续性" in profile["name"]
    assert {item["field"] for item in profile["core_fields"]} == {
        "net_mf_amount",
        "turnover_rate",
    }
    assert profile["active_factor_count"] == 3
    assert "opaque_internal_name" not in profile["name"]


def test_region_profile_is_stable_when_member_order_changes():
    members = [
        {
            "factor_id": "f2",
            "expression": "rank(-float_mv) * rank(-ts_std(pct_change,10))",
        },
        {
            "factor_id": "f1",
            "expression": "rank(-float_mv) * rank(-ts_delta(turnover_rate,10))",
        },
    ]
    base = {"region_uid": "region_stable", "size": 2, "members": members}

    left = build_region_profile(base)
    right = build_region_profile({**base, "members": list(reversed(members))})

    assert left["name"] == right["name"]
    assert left["core_fields"] == right["core_fields"]
    assert left["core_structures"] == right["core_structures"]


def test_region_guidance_requires_repeated_rounds_and_same_semantic_structure():
    insufficient = {
        "novelty_checked": 6,
        "novelty_rejected": 6,
        "novelty_rejected_round_count": 1,
        "max_rejected_semantic_signature_count": 6,
    }
    repeated = {
        **insufficient,
        "novelty_rejected_round_count": 2,
    }

    assert _region_guidance(insufficient)["action"] != "avoid_near_copy"
    guidance = _region_guidance(repeated)
    assert guidance["action"] == "avoid_near_copy"
    assert guidance["advisory_only"] is True


def test_region_guidance_does_not_treat_deep_failure_as_novelty_saturation():
    guidance = _region_guidance(
        {
            "deep_checked": 4,
            "deep_rejected": 3,
            "deep_rejected_round_count": 2,
            "dominant_deep_failure_category": "turnover",
            "max_deep_failure_category_count": 3,
        }
    )

    assert guidance["action"] == "change_validation_mechanism"
    assert "换手负担" in guidance["instruction"]
