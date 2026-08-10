from __future__ import annotations

import json

import pytest

from domain.factor_research.deepseek_client import DeepSeekClientError
from domain.model.contracts import default_r1_experiment
from domain.model.orchestrator import _normalize_llm_experiment, orchestrator_start, run_round_synthesis
from domain.model.qlib_runner import run_round, submit_experiment
from domain.model.scoring import score_round
from domain.model.state_store import ModelStateStore, read_jsonl


@pytest.fixture(autouse=True)
def _feature_set_preflight_passes(monkeypatch):
    def passed(feature_set_id):
        return {"passed": True, "feature_set_id": feature_set_id, "errors": [], "warnings": []}

    monkeypatch.setattr("domain.model.orchestrator.model_feature_set_preflight", passed)
    monkeypatch.setattr(
        "domain.model.orchestrator.model_preflight",
        lambda feature_set_id=None: {
            "passed": True,
            "stage": "feature_snapshot_preflight",
            "feature_set_id": feature_set_id,
            "active_values_status": "ready",
            "fingerprint_match": True,
            "safe_to_freeze_feature_set": True,
            "errors": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr("domain.model.qlib_runner.model_feature_set_preflight", passed)


class FakeDeepSeekClient:
    def __init__(self):
        self.calls = []

    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        self.calls.append({"system": system, "payload": payload, "temperature": temperature, "max_tokens": max_tokens})
        round_no = int(payload["round_no"])
        if payload.get("stage") == "round_synthesis":
            return {
                "stage": "round_synthesis",
                "decision": "continue",
                "summary": f"fake deepseek synthesis round {round_no}",
                "next": "experiment_plan",
                "round_group_id": payload["round_group_id"],
                "previous_parameters": payload.get("previous_parameters") or {},
                "three_seed_results": payload.get("three_seed_results") or [],
                "seed_dispersion": payload.get("seed_dispersion") or {},
                "score_summary": payload.get("score_summary") or [],
                "gate_summary": payload.get("gate_summary") or [],
                "validation_summary": {"status": "fake"},
                "parameter_lessons": ["fake lesson"],
                "next_experiment_guidance": "continue constrained parameter search",
                "next_parameter_change_rationale": ["fake next rationale"],
                "evidence_refs": ["fake"],
                "_orchestrator_llm_model": "fake-deepseek",
                "_orchestrator_llm_provider_model": "fake-deepseek-provider",
            }
        params = dict((payload.get("tuning_state") or {}).get("best_parameters") or {})
        parameter = "lambda_l1" if round_no == 1 else "lambda_l2"
        old_value = params.get(parameter)
        new_value = float(old_value) + (5 if round_no == 1 else 10)
        return {
            "stage": "experiment_plan",
            "decision": "submit_experiment",
            "next_move": "converge" if round_no == 2 else "explore",
            "evidence_interpretation": "fake stable three-seed evidence",
            "hypothesis": "fake controlled regularization hypothesis",
            "parameter_changes": [
                {"parameter": parameter, "from": old_value, "to": new_value, "reason": "fake evidence-based change"}
            ],
            "risks_to_watch": ["fake risk"],
            "_orchestrator_llm_model": "fake-deepseek",
            "_orchestrator_llm_provider_model": "fake-deepseek-provider",
        }


class FailingDeepSeekClient:
    def complete_json(self, **kwargs):
        raise DeepSeekClientError("llm_api_key_missing", category="provider_config_error")


class NoImprovementDeepSeekClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        self.calls.append({"system": system, "payload": payload, "temperature": temperature, "max_tokens": max_tokens})
        round_no = int(payload["round_no"])
        params = dict((payload.get("tuning_state") or {}).get("best_parameters") or {})
        return {
            "stage": "experiment_plan",
            "decision": "submit_experiment",
            "evidence_interpretation": "same three-seed evidence without meaningful improvement",
            "next_move": "regularize",
            "hypothesis": f"test regularization level {round_no}",
            "parameter_changes": [
                {
                    "parameter": "lambda_l1",
                    "from": params["lambda_l1"],
                    "to": float(params["lambda_l1"]) + 5 * round_no,
                    "reason": "bounded distinct regularization test",
                }
            ],
            "risks_to_watch": ["no improvement"],
        }


def test_round0_uses_defaults_without_calling_deepseek(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    client = FakeDeepSeekClient()
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-round0",
        n_rounds=0,
        max_stage="experiment_plan",
        run_id="orch0703-round0-only",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=client,
    )

    assert result["ok"] is True
    assert client.calls == []
    assert result["session"]["n_rounds_requested"] == 1
    assert result["completed_rounds"][0]["round_no"] == 0
    round_payload = state.get_round(result["completed_rounds"][0]["round_group_id"])
    assert round_payload["experiment"]["baseline_kind"] == "model_orch_round0_baseline"


def test_round0_uses_operator_configured_qlib_baseline(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    client = FakeDeepSeekClient()
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-custom-round0",
        n_rounds=0,
        max_stage="experiment_plan",
        run_id="orch0703-custom-round0",
        baseline_model_params={"learning_rate": 0.03, "num_leaves": 64, "max_depth": 7},
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=client,
    )

    assert result["ok"] is True
    round_payload = state.get_round(result["completed_rounds"][0]["round_group_id"])
    experiment = round_payload["experiment"]
    assert experiment["baseline_kind"] == "model_orch_round0_custom_baseline"
    assert experiment["qlib_model_kwargs"]["learning_rate"] == 0.03
    assert experiment["qlib_model_kwargs"]["lr"] == 0.03
    assert experiment["qlib_model_kwargs"]["num_leaves"] == 64
    assert experiment["research_metadata"]["baseline_parameter_source"] == "operator_override"


def test_llm_experiment_fixed_contract_fields_are_injected_not_required():
    params = dict(default_r1_experiment()["qlib_model_kwargs"])
    normalized, warnings = _normalize_llm_experiment(
        {
            "baseline_kind": "llm_research_only",
            "feature_missing_strategy": "qlib_processor_only",
            "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm",
            "sample_weight_kwargs": {
                "top_n": 50,
                "top_max": 2.0,
                "bottom_n": 50,
                "bottom_max": 1.5,
                "normalize_mean": True,
            },
            "qlib_model_kwargs": params,
            "training_hyperparameters": params,
        }
    )

    assert "fixed_contract_field_ignored:sample_weight_policy" in warnings
    assert "fixed_contract_field_ignored:sample_weight_kwargs" in warnings
    assert normalized["portfolio"]["topk"] == 20
    assert normalized["benchmark"] == "000300sh"
    assert normalized["deal_price"] == "open"
    assert normalized["forbid_all_trade_at_limit"] is False


def test_llm_experiment_ignores_fixed_contract_field_drift():
    params = dict(default_r1_experiment()["qlib_model_kwargs"])
    normalized, warnings = _normalize_llm_experiment(
        {
            "baseline_kind": "llm_research_only",
            "feature_missing_strategy": "qlib_processor_only",
            "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm",
            "sample_weight_kwargs": {},
            "qlib_model_kwargs": params,
            "training_hyperparameters": params,
            "portfolio": "top999/drop9/hold99",
            "benchmark": "000905sh",
            "deal_price": "close",
            "forbid_all_trade_at_limit": True,
        }
    )

    assert "fixed_contract_field_ignored:portfolio" in warnings
    assert "fixed_contract_field_ignored:benchmark" in warnings
    assert normalized["portfolio"]["topk"] == 20
    assert normalized["benchmark"] == "000300sh"
    assert normalized["deal_price"] == "open"
    assert normalized["forbid_all_trade_at_limit"] is False


def test_llm_experiment_ignores_fixed_model_kwargs():
    params = dict(default_r1_experiment()["qlib_model_kwargs"])
    params.update(
        {
            "loss": "regression_l2",
            "objective": "regression_l2",
            "seed": 99,
            "feature_fraction_seed": 99,
            "bagging_seed": 99,
            "data_random_seed": 99,
            "drop_seed": 99,
            "bin_construct_sample_cnt": 200_000,
            "bagging_fraction": 0.8,
            "learning_rate": 0.07,
            "lr": 0.07,
        }
    )
    normalized, warnings = _normalize_llm_experiment(
        {
            "baseline_kind": "llm_research_only",
            "feature_missing_strategy": "qlib_processor_only",
            "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm",
            "sample_weight_kwargs": {},
            "qlib_model_kwargs": params,
            "training_hyperparameters": params,
        }
    )

    assert "fixed_model_kwarg_ignored:qlib_model_kwargs.loss" in warnings
    assert "fixed_model_kwarg_ignored:qlib_model_kwargs.objective" in warnings
    assert "fixed_model_kwarg_ignored:qlib_model_kwargs.seed" in warnings
    assert "fixed_model_kwarg_ignored:qlib_model_kwargs.feature_fraction_seed" in warnings
    assert "fixed_model_kwarg_ignored:qlib_model_kwargs.bin_construct_sample_cnt" in warnings
    assert "fixed_model_kwarg_ignored:qlib_model_kwargs.bagging_fraction" in warnings
    assert normalized["qlib_model_kwargs"]["loss"] == "mse"
    assert normalized["qlib_model_kwargs"]["seed"] == 42
    assert normalized["qlib_model_kwargs"]["feature_fraction_seed"] == 42
    assert normalized["qlib_model_kwargs"]["bagging_seed"] == 42
    assert normalized["qlib_model_kwargs"]["data_random_seed"] == 42
    assert normalized["qlib_model_kwargs"]["drop_seed"] == 42
    assert normalized["qlib_model_kwargs"]["bin_construct_sample_cnt"] == 5_000_000
    assert normalized["qlib_model_kwargs"]["bagging_fraction"] == 0.9
    assert normalized["qlib_model_kwargs"]["bagging_freq"] == 1
    assert normalized["qlib_model_kwargs"]["learning_rate"] == 0.07


class CheckpointDeepSeekClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "round_synthesis":
            result["decision"] = "checkpoint_stop"
            result["next"] = "human_review"
        return result


class ExperimentPlanCheckpointClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "experiment_plan":
            result["next_move"] = "checkpoint_stop"
        return result


class RepeatingParameterPlanClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "experiment_plan" and int(payload["round_no"]) >= 2:
            result["next_move"] = "converge"
            result["hypothesis"] = "fake repeated core params"
            result["parameter_changes"] = [{"parameter": "lambda_l1", "from": 20, "to": 25, "reason": "repeat round one"}]
        return result


class DuplicateThenDiversePlanClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "experiment_plan" and int(payload["round_no"]) == 2:
            if not payload.get("correction"):
                result["hypothesis"] = "fake first duplicate response"
                result["parameter_changes"] = [{"parameter": "lambda_l1", "from": 20, "to": 25, "reason": "duplicate round one"}]
            else:
                result["hypothesis"] = "fake retry diverse response"
                result["parameter_changes"] = [{"parameter": "lambda_l2", "from": 50, "to": 65, "reason": "distinct retry"}]
        return result


class HistoricalDuplicateThenDiversePlanClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "experiment_plan" and int(payload["round_no"]) == 1:
            if not (payload.get("context_pack") or {}).get("correction"):
                params = dict(default_r1_experiment()["qlib_model_kwargs"])
                result["summary"] = "fake historical duplicate response"
                result["changed_knobs"] = []
                result["experiment_json"] = default_r1_experiment(
                    {
                        "baseline_kind": "historical_duplicate_first_attempt",
                        "qlib_model_kwargs": params,
                        "training_hyperparameters": params,
                    }
                )
            else:
                params = dict(default_r1_experiment()["qlib_model_kwargs"])
                params.update({"learning_rate": 0.035, "lr": 0.035, "num_leaves": 80, "lambda_l2": 5.0})
                result["summary"] = "fake retry diverse from historical ledger"
                result["changed_knobs"] = ["learning_rate", "num_leaves", "lambda_l2"]
                result["experiment_json"] = default_r1_experiment(
                    {
                        "baseline_kind": "retry_diverse_from_historical_ledger",
                        "qlib_model_kwargs": params,
                        "training_hyperparameters": params,
                    }
                )
        return result


class SimplifiedLGBMPlanClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        if payload.get("stage") == "round_synthesis":
            return super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        self.calls.append({"system": system, "payload": payload, "temperature": temperature, "max_tokens": max_tokens})
        params = dict(default_r1_experiment()["qlib_model_kwargs"])
        params.update({"learning_rate": 0.045, "lr": 0.045, "lambda_l1": 0.5})
        return {
            "hypothesis": "Test the calibrated baseline under the current evidence.",
            "evidence_interpretation": "No completed current-snapshot round is available yet.",
            "next_move": "explore",
            "parameter_changes": [],
            "risks_to_watch": ["baseline may be underfit"],
            "lgbm_parameters": {
                "learning_rate": params["learning_rate"],
                "num_leaves": params["num_leaves"],
                "max_depth": params["max_depth"],
                "min_data_in_leaf": params["min_data_in_leaf"],
                "feature_fraction": params["feature_fraction"],
                "lambda_l1": params["lambda_l1"],
                "lambda_l2": params["lambda_l2"],
                "n_estimators": params["n_estimators"],
                "early_stopping_rounds": params["early_stopping_rounds"],
            },
        }


class BadSynthesisTransitionDeepSeekClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "round_synthesis":
            result["decision"] = "blocked"
            result["next"] = "experiment_plan"
        return result


class MissingHypothesisDeepSeekClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "experiment_plan":
            result.pop("hypothesis", None)
        return result


class MissingRisksDeepSeekClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "experiment_plan":
            result.pop("risks_to_watch", None)
        return result


class FeatureSetRotatePlanClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "experiment_plan":
            result["next_move"] = "feature_set_rotate"
        return result


class DifferentFeatureSetPlanClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "experiment_plan":
            result["feature_set_id"] = "fs-different"
        return result


class AllActiveAliasPlanClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "experiment_plan":
            result["feature_set_id"] = "64_factor_all_active"
        return result


class FeatureSetRotateSynthesisClient(FakeDeepSeekClient):
    def complete_json(self, *, system: str, payload: dict, temperature: float = 0.12, max_tokens: int = 2600) -> dict:
        result = super().complete_json(system=system, payload=payload, temperature=temperature, max_tokens=max_tokens)
        if payload.get("stage") == "round_synthesis":
            result["next_experiment_guidance"] = {"next_move": "feature_set_rotate", "next_feature_set_id": "fs-curated"}
        return result


def test_model_orchestrator_runs_shadow_multiround_with_events_and_traces(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    client = FakeDeepSeekClient()
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-orch-test",
        n_rounds=2,
        run_id="orch0703-test",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=client,
    )

    assert result["ok"] is True
    assert len(client.calls) == 2
    first_plan_payload = client.calls[0]["payload"]
    assert "feature_set_id" not in first_plan_payload
    assert set(first_plan_payload) == {"task", "stage", "round_no", "tuning_state", "round_roles", "completed_rounds"}
    assert first_plan_payload["round_roles"]["best_round_group_id"] == result["completed_rounds"][0]["round_group_id"]
    assert "context_pack" not in first_plan_payload
    assert result["registry_target"] == "shadow"
    assert result["job"]["status"] == "completed"
    assert [row["round_no"] for row in result["completed_rounds"]] == [0, 1, 2]
    assert result["session"]["n_rounds_requested"] == 3
    assert result["completed_rounds"][0]["round_label"] == "Round 0 · 基准测试"
    assert len(state.list_jobs()) == 1
    counts = {row["round_group_id"]: len(state.list_seed_runs(round_group_id=row["round_group_id"])) for row in result["completed_rounds"]}
    assert counts[result["session"]["payload"]["best_round_group_id"]] == 3
    assert all(count in {1, 3} for count in counts.values())

    events = read_jsonl(tmp_path / "events.jsonl", limit=100)
    assert {row["event_type"] for row in events} >= {"start", "stage_start", "stage_complete", "complete"}
    assert any(row["stage"] == "research_score" for row in events)
    assert any(row["stage"] == "research_confirmation" for row in events)

    traces = read_jsonl(tmp_path / "traces.jsonl", limit=100)
    assert [row["event_type"] for row in traces].count("llm_request") == 2
    assert [row["event_type"] for row in traces].count("llm_result") >= 2
    plan_requests = [row for row in traces if row["event_type"] == "llm_request" and row.get("stage") == "experiment_plan"]
    assert all("feature_set_catalog" not in (row.get("context_pack") or {}) for row in plan_requests)
    request_contracts = [row.get("output_contract") or {} for row in traces if row["event_type"] == "llm_request"]
    assert all(row.get("llm_call_status") == "call_required" for row in request_contracts)
    assert all(row.get("planner_mode") == "deepseek" for row in request_contracts)
    result_summaries = [row.get("result_summary") or {} for row in traces if row["event_type"] == "llm_result" and row.get("stage") == "experiment_plan" and row.get("round_no") != 0]
    assert all(row.get("planner_mode") == "deepseek" for row in result_summaries)
    assert all(row.get("llm_call_status") == "called" for row in result_summaries)
    synthesis_results = [row for row in traces if row["event_type"] == "llm_result" and row.get("stage") == "round_synthesis"]
    assert len(synthesis_results) == 3
    round0_synthesis = next(row for row in synthesis_results if row.get("round_no") == 0)
    assert (round0_synthesis.get("result_summary") or {}).get("planner_mode") == "deterministic_platform_summary"
    assert (round0_synthesis.get("result_summary") or {}).get("llm_call_status") == "not_called"

    steps = read_jsonl(tmp_path / "research.jsonl", limit=100)
    assert any(row["stage"] == "round_synthesis" for row in steps)


def test_orchestrator_stops_after_three_consecutive_non_improving_rounds(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    client = NoImprovementDeepSeekClient()
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")
    baseline_metrics = {
        "42": {"annualized_ret": 0.30, "excess_annualized_ret_with_cost": 0.30, "excess_information_ratio_with_cost": 1.10, "max_drawdown": -0.13, "rank_ic": 0.022, "rank_icir": 0.16},
        "17": {"annualized_ret": 0.18, "excess_annualized_ret_with_cost": 0.18, "excess_information_ratio_with_cost": 0.75, "max_drawdown": -0.16, "rank_ic": 0.016, "rank_icir": 0.10},
        "83": {"annualized_ret": -0.08, "excess_annualized_ret_with_cost": -0.08, "excess_information_ratio_with_cost": -0.15, "max_drawdown": -0.24, "rank_ic": -0.004, "rank_icir": -0.02},
    }
    monkeypatch.setattr("domain.model.orchestrator._round_metrics_for_shadow", lambda _round_no: baseline_metrics)

    result = orchestrator_start(
        feature_set_id="fs-no-improve",
        n_rounds=4,
        run_id="orch0703-no-improve",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=client,
    )

    assert result["ok"] is True
    assert result["job"]["status"] == "interrupted"
    assert result["job"]["stage"] == "checkpoint_stop"
    assert len(result["completed_rounds"]) == 4
    assert result["session"]["payload"]["consecutive_no_improvement"] == 3
    assert result["session"]["payload"]["checkpoint_stop_policy"] == "three_consecutive_non_improving_rounds"
    assert result["session"]["payload"]["best_round_group_id"] == result["completed_rounds"][0]["round_group_id"]
    assert len(client.calls) == 3
    events = read_jsonl(tmp_path / "events.jsonl", limit=200)
    assert any(row["event_type"] == "checkpoint_stop" for row in events)


def test_fixed_feature_set_session_rejects_llm_feature_set_rotation_plan(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-fixed",
        n_rounds=1,
        run_id="orch0703-fs-rotate-block",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=FeatureSetRotatePlanClient(),
    )

    assert result["ok"] is False
    assert "llm_next_move_invalid:feature_set_rotate" in result["err"]
    assert [row["experiment"].get("baseline_kind") for row in state.list_rounds()] == ["model_orch_round0_baseline"]


def test_experiment_plan_rejects_checkpoint_stop_next_move(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-orch-test",
        n_rounds=1,
        run_id="orch0703-plan-checkpoint-block",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=ExperimentPlanCheckpointClient(),
    )

    assert result["ok"] is False
    assert "llm_next_move_invalid:checkpoint_stop" in result["err"]
    assert [row["experiment"].get("baseline_kind") for row in state.list_rounds()] == ["model_orch_round0_baseline"]


def test_orchestrator_rejects_repeated_core_lgbm_parameters(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-orch-test",
        n_rounds=2,
        run_id="orch0703-repeat-core-block",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=RepeatingParameterPlanClient(),
    )

    assert result["ok"] is False
    assert "llm_duplicate_core_experiment_parameters:matches_round_1" in result["err"]
    assert len(result["completed_rounds"]) == 2
    traces = read_jsonl(tmp_path / "traces.jsonl", limit=100)
    round2_request = [
        row for row in traces
        if row["event_type"] == "llm_request" and row.get("stage") == "experiment_plan" and row.get("round_no") == 2
    ][0]
    payload = (round2_request.get("output_contract") or {}).get("llm_payload") or {}
    assert "prior_experiment_history" not in payload
    assert "context_pack" not in payload
    assert (round2_request.get("output_contract") or {}).get("private_duplicate_history_count") >= 1


def test_orchestrator_retries_duplicate_core_lgbm_parameters_with_deepseek_feedback(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    client = DuplicateThenDiversePlanClient()
    result = orchestrator_start(
        feature_set_id="fs-orch-test",
        n_rounds=2,
        run_id="orch0703-repeat-core-retry",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=client,
    )

    assert result["ok"] is True
    assert len(result["completed_rounds"]) == 3
    assert any(call["payload"].get("correction") for call in client.calls)
    traces = read_jsonl(tmp_path / "traces.jsonl", limit=100)
    retry_requests = [
        row
        for row in traces
        if row["event_type"] == "llm_request"
        and row.get("stage") == "experiment_plan"
        and row.get("round_no") == 2
        and ((row.get("output_contract") or {}).get("llm_payload") or {}).get("correction")
    ]
    assert retry_requests
    correction = ((retry_requests[0].get("output_contract") or {}).get("llm_payload") or {}).get("correction") or {}
    assert "完全相同" in correction["message"]
    assert "至少" not in correction["message"]
    round2 = [
        row
        for row in state.list_rounds()
        if row["experiment"].get("baseline_kind") == "deepseek_lgbm_experiment_v2"
    ][0]
    params = round2["experiment"]["qlib_model_kwargs"]
    assert params["lambda_l2"] == 65


def test_orchestrator_does_not_send_or_block_on_cross_session_history(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    seeded = submit_experiment(
        feature_set_id="fs-orch-test",
        experiment=default_r1_experiment({"baseline_kind": "historical_seeded_round"}),
        state=state,
    )
    assert seeded["ok"] is True

    client = FakeDeepSeekClient()
    result = orchestrator_start(
        feature_set_id="fs-orch-test",
        n_rounds=1,
        run_id="orch0703-history-ledger",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=client,
    )

    assert result["ok"] is True
    assert not any(call["payload"].get("correction") for call in client.calls)
    first_payload = client.calls[0]["payload"]
    assert "prior_experiment_history" not in first_payload
    assert "context_pack" not in first_payload
    latest_round = next(row for row in state.list_rounds(limit=10) if row["experiment"].get("baseline_kind") == "deepseek_lgbm_experiment_v2")
    params = latest_round["experiment"]["qlib_model_kwargs"]
    assert latest_round["experiment"]["baseline_kind"] == "deepseek_lgbm_experiment_v2"
    assert params["lambda_l1"] == 25


def test_orchestrator_uses_other_feature_snapshots_as_reference_not_duplicate_blockers(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    old_feature_set_id = "fs-model-family-top2-20260709_120000"
    current_feature_set_id = "fs-model-active-20260710_120000"
    seeded = submit_experiment(
        feature_set_id=old_feature_set_id,
        experiment=default_r1_experiment({"baseline_kind": "old_snapshot_baseline"}),
        state=state,
    )
    assert seeded["ok"] is True

    client = FakeDeepSeekClient()
    result = orchestrator_start(
        feature_set_id=current_feature_set_id,
        n_rounds=1,
        run_id="orch0703-cross-snapshot-reference",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=client,
    )

    assert result["ok"] is True
    plan_payload = [call["payload"] for call in client.calls if call["payload"].get("stage") == "experiment_plan"][0]
    assert "context_pack" not in plan_payload
    assert len(plan_payload["completed_rounds"]) == 1
    assert "feature_set_id" not in plan_payload
    assert not any(call["payload"].get("correction") for call in client.calls)


def test_orchestrator_rejects_legacy_full_parameter_plan(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    client = SimplifiedLGBMPlanClient()
    result = orchestrator_start(
        feature_set_id="fs-orch-compact-context",
        n_rounds=1,
        run_id="orch0703-compact-context",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=client,
    )

    assert result["ok"] is False
    assert "llm_output_unknown_fields:" in result["err"] or "llm_stage_mismatch:" in result["err"]
    plan_payload = [call["payload"] for call in client.calls if call["payload"].get("stage") == "experiment_plan"][0]
    assert set(plan_payload) == {"task", "stage", "round_no", "tuning_state", "round_roles", "completed_rounds"}
    assert len(state.list_rounds()) == 1


def test_fixed_feature_set_session_rejects_different_llm_feature_set_id(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-fixed",
        n_rounds=1,
        run_id="orch0703-fs-id-block",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=DifferentFeatureSetPlanClient(),
    )

    assert result["ok"] is False
    assert "llm_output_unknown_fields:feature_set_id" in result["err"]
    assert [row["experiment"].get("baseline_kind") for row in state.list_rounds()] == ["model_orch_round0_baseline"]


def test_fixed_session_rejects_unrequested_feature_set_alias_field(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")
    result = orchestrator_start(
        feature_set_id="fs-model-active-20260709_230632",
        n_rounds=1,
        run_id="orch0703-fs-alias-ok",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=AllActiveAliasPlanClient(),
    )

    assert result["ok"] is False
    assert "llm_output_unknown_fields:feature_set_id" in result["err"]


def test_fixed_feature_set_session_does_not_call_synthesis_client(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-fixed",
        n_rounds=2,
        run_id="orch0703-fs-synthesis-guidance",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=FeatureSetRotateSynthesisClient(),
    )

    assert result["ok"] is True
    assert len(result["completed_rounds"]) == 3
    assert len([call for call in result["completed_rounds"] if call.get("round_synthesis_decision")]) == 3
    traces = read_jsonl(tmp_path / "traces.jsonl", limit=100)
    synthesis_results = [row for row in traces if row["event_type"] == "llm_result" and row.get("stage") == "round_synthesis"]
    assert all((row.get("parsed_response") or {}).get("planner_mode") == "deterministic_platform_summary" for row in synthesis_results)


def test_orchestrator_blocks_on_feature_snapshot_preflight_before_deepseek(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    client = FakeDeepSeekClient()
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")
    monkeypatch.setattr(
        "domain.model.orchestrator.model_preflight",
        lambda feature_set_id=None: {
            "passed": False,
            "stage": "feature_snapshot_preflight",
            "feature_set_id": feature_set_id,
            "errors": ["active_values_registry_fingerprint_mismatch"],
            "warnings": [],
            "blocker": {
                "code": "active_values_registry_fingerprint_mismatch",
                "category": "external_data_blocker",
                "stage": "feature_snapshot_preflight",
                "human_message": "active values stale because registry changed from fp-old to fp-new",
                "repair_action": "refresh_active_values_from_parquet_then_freeze_feature_snapshot",
                "resume_from": "feature_snapshot_preflight",
                "affected_round": "",
            },
        },
    )

    result = orchestrator_start(
        feature_set_id="fs-orch-test",
        n_rounds=1,
        run_id="orch0703-preflight-block",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=client,
    )

    assert result["ok"] is False
    assert result["err"] == "feature_snapshot_preflight_failed"
    assert client.calls == []
    job = state.get_job("orch0703-preflight-block")
    assert job["status"] == "failed"
    assert job["stage"] == "blocker"
    session = state.get_session(result["session_id"])
    assert session["status"] == "failed"
    assert session["current_blocker"]["category"] == "external_data_blocker"
    assert state.list_rounds() == []


def test_model_orchestrator_blocks_when_deepseek_fails_without_fallback(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-orch-test",
        n_rounds=1,
        run_id="orch0703-fail",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=FailingDeepSeekClient(),
    )

    assert result["ok"] is False
    assert "llm_api_key_missing" in result["err"]
    assert [row["round_no"] for row in result["completed_rounds"]] == [0]
    assert state.get_job("orch0703-fail")["status"] == "failed"
    assert state.get_job("orch0703-fail")["stage"] == "blocker"
    assert [row["experiment"].get("baseline_kind") for row in state.list_rounds()] == ["model_orch_round0_baseline"]
    events = read_jsonl(tmp_path / "events.jsonl", limit=100)
    assert events[-1]["event_type"] == "failed"
    steps = read_jsonl(tmp_path / "research.jsonl", limit=100)
    assert steps[-1]["stage"] == "blocker"
    traces = read_jsonl(tmp_path / "traces.jsonl", limit=100)
    failed_trace = traces[-1]
    assert failed_trace["event_type"] == "llm_result"
    assert failed_trace["result_summary"]["llm_call_status"] == "failed"
    assert failed_trace["parsed_response"]["summary"] == "DeepSeek experiment_plan failed; no fallback planner executed."


def test_model_orchestrator_rejects_parameter_plan_without_hypothesis(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-orch-test",
        n_rounds=1,
        run_id="orch0703-missing-hypothesis",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=MissingHypothesisDeepSeekClient(),
    )

    assert result["ok"] is False
    assert "llm_hypothesis_missing" in result["err"]
    assert state.get_job("orch0703-missing-hypothesis")["stage"] == "blocker"


def test_model_orchestrator_accepts_plan_with_missing_noncritical_risks(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-orch-test",
        n_rounds=1,
        run_id="orch0703-missing-risks",
        state=state,
        execute_qlib=False,
        write_registry=False,
        max_stage="experiment_plan",
        client=MissingRisksDeepSeekClient(),
    )

    assert result["ok"] is True
    traces = read_jsonl(tmp_path / "traces.jsonl", limit=100)
    plan_trace = next(row for row in traces if row["event_type"] == "llm_result" and row.get("stage") == "experiment_plan" and row.get("round_no") == 1)
    assert plan_trace["parsed_response"]["risks_to_watch"] == []
    assert "risks_to_watch_missing" not in plan_trace["parsed_response"]["schema_warnings"]


def test_model_orchestrator_checkpoint_round_is_not_queued(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    client = FakeDeepSeekClient()
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-orch-test",
        n_rounds=1,
        run_id="orch0703-checkpoint",
        state=state,
        execute_qlib=False,
        write_registry=False,
        max_stage="experiment_plan",
        client=client,
    )

    assert result["ok"] is True
    round_payload = state.get_round(result["completed_rounds"][0]["round_group_id"])
    assert round_payload["status"] == "interrupted"
    assert round_payload["stage"] == "experiment_plan"


def test_model_orchestrator_does_not_call_deepseek_for_round_synthesis(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    client = CheckpointDeepSeekClient()
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")

    result = orchestrator_start(
        feature_set_id="fs-orch-test",
        n_rounds=3,
        run_id="orch0703-synthesis-checkpoint",
        state=state,
        execute_qlib=False,
        write_registry=False,
        client=client,
    )

    assert result["ok"] is True
    assert result["job"]["status"] == "completed"
    assert len(result["completed_rounds"]) == 4
    assert len(client.calls) == 3
    assert all(call["payload"]["stage"] == "experiment_plan" for call in client.calls)


def test_standalone_round_synthesis_uses_completed_round_and_shadow_registry(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    client = FakeDeepSeekClient()
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")
    experiment = {
        "feature_missing_strategy": "qlib_processor_only",
        "sample_weight_policy": "top50_smooth2_bottom50_smooth1p5_mean_norm",
        "metrics_by_seed": {
            "42": {"annualized_ret": -0.01, "excess_annualized_ret_with_cost": -0.01, "excess_information_ratio_with_cost": -0.1, "max_drawdown": -0.1, "rank_ic": 0.03},
            "17": {"annualized_ret": -0.02, "excess_annualized_ret_with_cost": -0.02, "excess_information_ratio_with_cost": -0.2, "max_drawdown": -0.1, "rank_ic": 0.03},
            "83": {"annualized_ret": -0.03, "excess_annualized_ret_with_cost": -0.03, "excess_information_ratio_with_cost": -0.3, "max_drawdown": -0.1, "rank_ic": 0.03},
        },
    }
    submitted = submit_experiment(feature_set_id="fs-synth", experiment=experiment, state=state)
    round_group_id = submitted["round_group"]["round_group_id"]
    run_round(round_group_id=round_group_id, state=state)
    score_round(round_group_id, state=state)

    result = run_round_synthesis(
        round_group_id=round_group_id,
        round_no=1,
        job_id="synth-job",
        write_registry=False,
        state=state,
        client=client,
    )

    assert result["ok"] is True
    assert result["registry_target"] == "shadow"
    assert result["job"]["status"] == "completed"
    assert result["round_synthesis"]["stage"] == "round_synthesis"
    assert len(client.calls) == 0
    assert all(item["asset_status"] in {"research", "archived"} for item in result["gate_summary"])
    assert not (tmp_path / "orchestrator_shadow_registry.db").exists()
    traces = read_jsonl(tmp_path / "traces.jsonl", limit=20)
    assert [row["event_type"] for row in traces] == ["llm_result"]
    steps = read_jsonl(tmp_path / "research.jsonl", limit=20)
    assert steps[-1]["stage"] == "round_synthesis"


def test_round_synthesis_ignores_llm_transition_clients(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    client = BadSynthesisTransitionDeepSeekClient()
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")
    submitted = submit_experiment(feature_set_id="fs-bad-transition", experiment=default_r1_experiment(), state=state)
    round_group_id = submitted["round_group"]["round_group_id"]
    run_round(round_group_id=round_group_id, state=state)
    score_round(round_group_id, state=state)

    result = run_round_synthesis(
        round_group_id=round_group_id,
        round_no=1,
        job_id="bad-transition-job",
        write_registry=False,
        state=state,
        client=client,
    )

    assert result["ok"] is True
    assert client.calls == []
    assert state.get_job("bad-transition-job")["stage"] == "round_synthesis"


def test_standalone_round_synthesis_is_deterministic_even_with_checkpoint_client(tmp_path, monkeypatch):
    state = ModelStateStore(runtime_root=tmp_path)
    client = CheckpointDeepSeekClient()
    monkeypatch.setattr("domain.model.qlib_runner.MODEL_RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_ORCHESTRATOR_EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr("domain.model.orchestrator.MODEL_RESEARCH_STEPS", tmp_path / "research.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_ORCHESTRATOR_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr("domain.model.context.MODEL_CONTEXT_SNAPSHOTS", tmp_path / "context")
    submitted = submit_experiment(feature_set_id="fs-checkpoint-synth", experiment=default_r1_experiment(), state=state)
    round_group_id = submitted["round_group"]["round_group_id"]
    run_round(round_group_id=round_group_id, state=state)
    score_round(round_group_id, state=state)

    result = run_round_synthesis(
        round_group_id=round_group_id,
        round_no=1,
        job_id="checkpoint-synth-job",
        write_registry=False,
        state=state,
        client=client,
    )

    assert result["ok"] is True
    assert client.calls == []
    job = state.get_job("checkpoint-synth-job")
    assert job["status"] == "completed"
    assert job["stage"] == "round_synthesis"


def test_read_jsonl_compacts_payload_by_default(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"payload": {"large": True}, "context_pack": {"large": True}}) + "\n", encoding="utf-8")

    compact = read_jsonl(path, include_payload=False)[0]

    assert compact["payload"] == {"omitted": True}
    assert compact["context_pack"] == {"omitted": True}


def test_read_jsonl_reads_only_tail_and_preserves_utf8(tmp_path):
    path = tmp_path / "large-tail.jsonl"
    rows = [{"index": index, "message": f"研究轮次-{index}"} for index in range(2500)]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    tail = read_jsonl(path, limit=3)

    assert tail == rows[-3:]
