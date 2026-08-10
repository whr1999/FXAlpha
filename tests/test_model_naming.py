from domain.model.naming import (
    MODEL_DISPLAY_NAMING_VERSION,
    feature_set_display_label,
    model_display_projection,
    normalize_model_identifier,
    rolling_display_projection,
)


def test_research_display_name_uses_feature_local_time_and_round():
    result = model_display_projection(
        {
            "model_run_id": "mrun_mround_20260803_130746_example1_s42_example2",
            "feature_set_id": "fs-model-demo-set-20260803",
            "seed": 42,
            "status": "research",
        },
        round_no=6,
    )

    assert result["display_name"] == "研究 · DEMO-SET · 2026-08-03 21:07 · R6"
    assert result["display_subtitle"] == f"正式 Seed42 · {MODEL_DISPLAY_NAMING_VERSION}"


def test_round_zero_is_not_suppressed_by_naming_contract():
    result = model_display_projection(
        {
            "model_run_id": "mrun_mround_20260803_124504_example3_s42_example4",
            "feature_set_id": "fs-model-demo-set-20260803",
            "seed": 42,
            "status": "research",
        },
        round_no=0,
    )

    assert result["display_name"].endswith(" · R0")


def test_rolling_and_legacy_identifiers_share_one_display_contract():
    current = rolling_display_projection(
        {
            "campaign_id": "model_roll_20260803T134215.000000_example",
            "feature_set_id": "fs-model-demo-set-20260803",
            "status": "research",
        }
    )
    legacy = rolling_display_projection(
        {
            "campaign_id": "roll0703_20260721T003926.000000_example",
            "feature_set_id": "fs-model0703-legacy-demo-20260721",
            "status": "research",
        }
    )

    assert current["display_name"] == "ROLLING · DEMO-SET · 2026-08-03 21:42"
    assert legacy["display_name"] == "ROLLING · LEGACY-DEMO · 2026-07-21 08:39"
    assert normalize_model_identifier("roll0703_x") == "model_roll_x"
    assert feature_set_display_label("fs-model0703-legacy-demo-20260721") == "LEGACY-DEMO"


def test_production_display_uses_refit_time_not_source_rolling_time():
    result = model_display_projection(
        {
            "model_run_id": "model_prod_example_source_20260804T012216000000z0000",
            "feature_set_id": "fs-model-demo-set-20260803",
            "seed": 42,
            "status": "production",
        }
    )

    assert result["display_name"] == "生产 · DEMO-SET · 2026-08-04 09:22"
