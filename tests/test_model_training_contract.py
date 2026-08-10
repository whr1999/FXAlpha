from __future__ import annotations

from domain.model.contracts import (
    default_r1_experiment,
    normalize_research_baseline_overrides,
    production_contract,
    validate_experiment_contract,
)
from domain.model.qlib_direct import _model_kwargs
from domain.model.training_contract import model_training_contract


CALIBRATED_R1_KIND = "model_fxalpha_calibrated_lgbm_highcap_fast_stochastic_top20_drop2_hold5"


def test_research_baseline_override_contract_exposes_defaults_and_validates_relations():
    contract = production_contract()["research_baseline_overrides"]
    assert contract["defaults"]["learning_rate"] == 0.04
    assert contract["defaults"]["num_leaves"] == 96
    assert contract["fixed"]["seed_policy"] == "seed42_screening_then_seed17_83_confirmation"

    valid = normalize_research_baseline_overrides({"learning_rate": "0.03", "num_leaves": "64"})
    assert valid == {
        "passed": True,
        "errors": [],
        "normalized": {"learning_rate": 0.03, "lr": 0.03, "num_leaves": 64},
    }

    qlib_anchor = normalize_research_baseline_overrides(
        {"learning_rate": 0.2, "feature_fraction": 0.8879, "lambda_l2": 580.9768}
    )
    assert qlib_anchor["passed"] is True

    invalid = normalize_research_baseline_overrides({"num_leaves": 256, "max_depth": 4})
    assert invalid["passed"] is False
    assert "baseline_model_param_relation:num_leaves_exceeds_depth_capacity" in invalid["errors"]


def test_training_contract_keeps_processor_reweight_and_portfolio_defaults():
    contract = model_training_contract()

    assert contract["processor_chain"]["infer_processors"][1]["class"] == "RobustZScoreNorm"
    assert contract["processor_chain"]["infer_processors"][1]["kwargs"]["clip_outlier"] is True
    assert contract["processor_chain"]["infer_processors"][2]["class"] == "CSZFillna"
    assert contract["default_sample_weight_policy"] == "top50_smooth2_bottom50_smooth1p5_mean_norm"
    assert contract["portfolio"]["topk"] == 20
    assert contract["portfolio"]["n_drop"] == 2
    assert contract["formal_backtest"]["deal_price"] == "open"
    assert contract["r1_baseline_kind"] == CALIBRATED_R1_KIND
    assert contract["r1_default_source"] == "fxalpha_default_parameter_study_20260718"
    assert contract["r1_default_lgbm_params"]["learning_rate"] == 0.04
    assert contract["r1_default_lgbm_params"]["num_leaves"] == 96
    assert contract["qlib_official_alpha158_lgbm_params"]["learning_rate"] == 0.2
    assert contract["qlib_official_alpha158_lgbm_params"]["num_leaves"] == 210
    assert contract["qlib_contract"]["model_module"] == "domain.model.reweight"
    assert contract["qlib_contract"]["strategy_module"] == "domain.model.qlib_strategy"


def test_default_r1_experiment_is_submit_ready():
    experiment = default_r1_experiment()
    result = validate_experiment_contract(experiment)

    assert result["passed"] is True
    assert result["normalized"]["segments"] == {
        "train": ["2022-01-04", "2024-12-31"],
        "valid": ["2025-01-02", "2025-06-30"],
        "test": ["2025-07-01", "2026-07-01"],
    }
    assert result["normalized"]["baseline_kind"] == CALIBRATED_R1_KIND
    assert result["normalized"]["qlib_model_kwargs"]["learning_rate"] == 0.04
    assert result["normalized"]["qlib_model_kwargs"]["lr"] == 0.04
    assert result["normalized"]["qlib_model_kwargs"]["num_leaves"] == 96
    assert result["normalized"]["qlib_model_kwargs"]["max_depth"] == 8
    assert result["normalized"]["qlib_model_kwargs"]["min_data_in_leaf"] == 10
    assert result["normalized"]["qlib_model_kwargs"]["lambda_l1"] == 20
    assert result["normalized"]["qlib_model_kwargs"]["lambda_l2"] == 50
    assert result["normalized"]["qlib_model_kwargs"]["feature_fraction"] == 0.9
    assert result["normalized"]["qlib_model_kwargs"]["bagging_fraction"] == 0.9
    assert result["normalized"]["qlib_model_kwargs"]["bagging_freq"] == 1
    assert result["normalized"]["qlib_model_kwargs"]["n_estimators"] == 2400
    assert result["normalized"]["qlib_model_kwargs"]["early_stopping_rounds"] == 100
    assert result["normalized"]["qlib_model_kwargs"]["bin_construct_sample_cnt"] == 5_000_000
    assert result["normalized"]["qlib_model_kwargs"]["seed"] == 42
    assert result["normalized"]["qlib_model_kwargs"]["feature_fraction_seed"] == 42
    assert result["normalized"]["qlib_model_kwargs"]["bagging_seed"] == 42
    assert result["normalized"]["qlib_model_kwargs"]["data_random_seed"] == 42
    assert result["normalized"]["qlib_model_kwargs"]["drop_seed"] == 42
    assert result["normalized"]["portfolio"]["topk"] == 20
    assert result["normalized"]["effective_sample_weight_policy"] == "top50_smooth2_bottom50_smooth1p5_mean_norm"


def test_explicit_operator_segments_override_is_allowed():
    segments = {
        "train": ["2022-01-04", "2024-06-30"],
        "valid": ["2024-07-01", "2024-12-31"],
        "test": ["2025-01-01", "2025-12-31"],
    }

    result = validate_experiment_contract(default_r1_experiment({"segments": segments}))

    assert result["passed"] is True
    assert result["normalized"]["segments"] == segments


def test_string_false_forbid_all_trade_at_limit_is_submit_ready():
    experiment = default_r1_experiment({"forbid_all_trade_at_limit": "false"})
    result = validate_experiment_contract(experiment)

    assert result["passed"] is True
    assert result["normalized"]["forbid_all_trade_at_limit"] is False


def test_string_portfolio_contract_is_submit_ready():
    experiment = default_r1_experiment(
        {
            "portfolio": "top20/drop2/hold5",
        }
    )
    result = validate_experiment_contract(experiment)

    assert result["passed"] is True
    assert result["normalized"]["portfolio"]["topk"] == 20
    assert result["normalized"]["portfolio"]["n_drop"] == 2


def test_orch_parameter_tuning_is_submit_ready_under_same_contract():
    tuned = {
        **default_r1_experiment()["qlib_model_kwargs"],
        "learning_rate": 0.1,
        "lr": 0.1,
        "max_depth": 7,
        "num_leaves": 150,
        "min_data_in_leaf": 30,
    }
    experiment = default_r1_experiment(
        {
            "qlib_model_kwargs": tuned,
            "training_hyperparameters": tuned,
        }
    )

    result = validate_experiment_contract(experiment)

    assert result["passed"] is True
    assert result["normalized"]["qlib_model_kwargs"]["learning_rate"] == 0.1
    assert result["normalized"]["qlib_model_kwargs"]["num_leaves"] == 150


def test_strict_r1_baseline_still_blocks_parameter_drift_when_requested():
    tuned = {
        **default_r1_experiment()["qlib_model_kwargs"],
        "learning_rate": 0.1,
        "lr": 0.1,
    }
    experiment = default_r1_experiment(
        {
            "strict_r1_baseline": True,
            "qlib_model_kwargs": tuned,
            "training_hyperparameters": tuned,
        }
    )

    result = validate_experiment_contract(experiment)

    assert result["passed"] is False
    assert any(item.startswith("r1_model_param_mismatch:learning_rate") for item in result["errors"])


def test_illegal_experiment_blocks_before_qrun():
    result = validate_experiment_contract(
        {
            "feature_missing_strategy": "magic_fill",
            "sample_weight_policy": "freeform",
            "pre_shift_pred": True,
            "deal_price": "close",
        }
    )

    assert result["passed"] is False
    assert "unsupported_feature_missing_strategy:magic_fill" in result["errors"]
    assert "unsupported_sample_weight_policy:freeform" in result["errors"]
    assert "pred_pkl_must_not_be_pre_shifted" in result["errors"]
    assert "backtest_deal_price_must_be_open" in result["errors"]


def test_contract_blocks_portfolio_processor_and_seed_policy_drift():
    experiment = default_r1_experiment(
        {
            "portfolio": {"topk": 10, "n_drop": 2, "hold_thresh": 5, "deal_price": "open"},
            "qlib_processors": {"infer_processors": [{"class": "ProcessInf"}]},
            "seed_policy": {"mode": "best_seed", "use_ensemble": True},
        }
    )
    result = validate_experiment_contract(experiment)

    assert result["passed"] is False
    assert any(item.startswith("portfolio_topk_mismatch") for item in result["errors"])
    assert any(item.startswith("processor_policy_drift") for item in result["errors"])
    assert "seed_policy_ensemble_forbidden" in result["errors"]
    assert "seed_policy_mode_forbidden:best_seed" in result["errors"]


def test_nested_sample_weight_is_lifted_out_of_model_kwargs():
    experiment = default_r1_experiment()
    experiment.pop("sample_weight_policy", None)
    experiment.pop("sample_weight_kwargs", None)
    experiment["qlib_model_kwargs"] = {
        **experiment["qlib_model_kwargs"],
        "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm",
        "sample_weight_kwargs": {
            "top_n": 50,
            "top_max": 2.0,
            "bottom_n": 50,
            "bottom_max": 1.5,
            "normalize_mean": True,
        },
    }
    experiment["training_hyperparameters"] = dict(experiment["qlib_model_kwargs"])

    result = validate_experiment_contract(experiment)

    assert result["passed"] is True
    assert result["normalized"]["sample_weight_policy"] == "top50_smooth2_bottom50_smooth1p5_mean_norm"
    assert "sample_weight_policy" not in result["normalized"]["qlib_model_kwargs"]
    assert "sample_weight_kwargs" not in result["normalized"]["training_hyperparameters"]


def test_sample_weight_conflict_blocks_before_submit():
    experiment = default_r1_experiment(
        {
            "sample_weight_policy": "none",
            "qlib_model_kwargs": {
                **default_r1_experiment()["qlib_model_kwargs"],
                "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm",
            },
        }
    )

    result = validate_experiment_contract(experiment)

    assert result["passed"] is False
    assert any(item.startswith("sample_weight_policy_conflict") for item in result["errors"])


def test_non_default_sample_weight_policy_blocks_before_submit():
    experiment = default_r1_experiment({"sample_weight_policy": "sticky", "sample_weight_kwargs": {}})

    result = validate_experiment_contract(experiment)

    assert result["passed"] is False
    assert "sample_weight_policy_fixed_contract:sticky!=expected:top50_smooth2_bottom50_smooth1p5_mean_norm" in result["errors"]


def test_none_sample_weight_policy_blocks_before_submit():
    experiment = default_r1_experiment({"sample_weight_policy": "none", "sample_weight_kwargs": {}})

    result = validate_experiment_contract(experiment)

    assert result["passed"] is False
    assert "sample_weight_policy_fixed_contract:none!=expected:top50_smooth2_bottom50_smooth1p5_mean_norm" in result["errors"]


def test_direct_qlib_execution_forces_single_thread_resources():
    experiment = default_r1_experiment()
    experiment["qlib_model_kwargs"] = {**experiment["qlib_model_kwargs"], "n_jobs": 8, "num_threads": 8}

    params = _model_kwargs(experiment, seed=42)

    assert params["n_jobs"] == 1
    assert params["num_threads"] == 1
    assert params["seed"] == 42
    assert params["deterministic"] is True
    assert params["force_col_wise"] is True


def test_default_execution_uses_the_comparison_panel_seed():
    params = _model_kwargs(default_r1_experiment(), seed=17)

    assert params["seed"] == 17
    assert params["feature_fraction_seed"] == 17
    assert params["bagging_seed"] == 17
    assert params["data_random_seed"] == 17
    assert params["drop_seed"] == 17
    assert params["bin_construct_sample_cnt"] == 5_000_000
