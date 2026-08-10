from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

import pandas as pd

from .contracts import SCORE_REVIEW_VERSION, utc_now
from .state_store import ModelStateStore


def _num(metrics: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if metrics.get(key) is not None:
            try:
                return float(metrics[key])
            except Exception:
                return default
    return default


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _scale_up(value: float, floor: float, cap: float) -> float:
    return _clamp((value - floor) / (cap - floor) * 100.0)


def _scale_down(value: float, good: float, bad: float) -> float:
    return _clamp((bad - value) / (bad - good) * 100.0)


def individual_score_components(metrics: dict[str, Any]) -> tuple[dict[str, float], list[str]]:
    ann = _num(metrics, "excess_annualized_ret_with_cost", "annualized_ret")
    ir = _num(metrics, "excess_information_ratio_with_cost", "sharpe")
    dd = abs(_num(metrics, "max_drawdown"))
    rank_ic = _num(metrics, "rank_ic", "ic_mean")
    rank_icir = _num(metrics, "rank_icir", "icir")
    return_score = _scale_up(ann, 0.10, 0.60)
    ir_score = _scale_up(ir, 0.50, 1.50)
    drawdown_score = _scale_down(dd, 0.10, 0.30)
    rank_ic_score = _scale_up(rank_ic, 0.02, 0.05)
    rank_icir_score = _scale_up(rank_icir, 0.20, 0.50)
    rank_signal_score = 0.5 * rank_ic_score + 0.5 * rank_icir_score

    warnings: list[str] = []

    turnover_value = metrics.get("turnover")
    if turnover_value is None:
        turnover_score = 50.0
        warnings.append("missing_turnover")
    else:
        turnover_score = _scale_down(abs(_num(metrics, "turnover")), 0.40, 1.00)

    return {
        "return_score": round(return_score, 3),
        "ir_score": round(ir_score, 3),
        "drawdown_score": round(drawdown_score, 3),
        "rank_ic_score": round(rank_ic_score, 3),
        "rank_icir_score": round(rank_icir_score, 3),
        "rank_signal_score": round(rank_signal_score, 3),
        "turnover_score": round(turnover_score, 3),
    }, warnings


def individual_performance_score(metrics: dict[str, Any]) -> float:
    components, _warnings = individual_score_components(metrics)
    score = (
        0.40 * components["ir_score"]
        + 0.30 * components["return_score"]
        + 0.20 * components["drawdown_score"]
        + 0.10 * components["rank_signal_score"]
    )
    return round(_clamp(score), 3)


def performance_hard_blocks(metrics: dict[str, Any]) -> list[str]:
    blocks: list[str] = []
    ann = _num(metrics, "excess_annualized_ret_with_cost", "annualized_ret")
    ir = _num(metrics, "excess_information_ratio_with_cost", "sharpe")
    if ann < 0.10:
        blocks.append("excess_annualized_ret_with_cost_below_10pct")
    if ir < 0.50:
        blocks.append("excess_information_ratio_with_cost_below_0p5")
    return blocks


def _prediction_series(artifact_dir: str) -> pd.Series:
    """Load the formal Qlib prediction artifact as daily cross-sectional scores."""
    path = Path(artifact_dir) / "pred.pkl"
    pred = pd.read_pickle(path)
    if isinstance(pred, pd.DataFrame):
        if "score" in pred.columns:
            pred = pred["score"]
        else:
            numeric = pred.select_dtypes(include="number")
            if numeric.shape[1] != 1:
                raise ValueError("pred.pkl has no unambiguous score column")
            pred = numeric.iloc[:, 0]
    if not isinstance(pred, pd.Series):
        raise ValueError("pred.pkl is not a score series")
    if not isinstance(pred.index, pd.MultiIndex) or not {"datetime", "instrument"}.issubset(pred.index.names):
        raise ValueError("pred.pkl index must include datetime and instrument")
    series = pd.to_numeric(pred, errors="coerce").rename("score").dropna().sort_index()
    if series.empty:
        raise ValueError("pred.pkl has no numeric scores")
    return series


def _daily_rank_correlation(left: pd.Series, right: pd.Series) -> float | None:
    joined = pd.concat([left.rename("left"), right.rename("right")], axis=1).dropna()
    if joined.empty:
        return None
    daily: list[float] = []
    for _date, group in joined.groupby(level="datetime", sort=True):
        # A cross-sectional rank correlation with fewer than 20 securities is
        # not comparable to the RankIC evidence used by the same workflow.
        if len(group) < 20:
            continue
        value = group["left"].corr(group["right"], method="spearman")
        if pd.notna(value):
            daily.append(float(value))
    return float(statistics.mean(daily)) if daily else None


def _write_metrics_artifact(artifact_dir: str, metrics: dict[str, Any]) -> None:
    if not artifact_dir:
        return
    path = Path(artifact_dir) / "metrics.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _attach_prediction_rank_correlations(
    seed_runs: list[dict[str, Any]], *, state: ModelStateStore
) -> list[dict[str, Any]]:
    """Compute each seed's mean daily Spearman correlation to the other seeds.

    The comparison belongs here, after all three pred.pkl artifacts exist.  The
    result is stored in both the seed metrics record and metrics.json so the
    score review is reproducible from the training artifacts.
    """
    predictions: dict[str, pd.Series] = {}
    errors: dict[str, str] = {}
    for row in seed_runs:
        model_run_id = str(row["model_run_id"])
        try:
            predictions[model_run_id] = _prediction_series(str(row.get("artifact_dir") or ""))
        except Exception as exc:
            errors[model_run_id] = str(exc)

    correlations: dict[str, list[float]] = {str(row["model_run_id"]): [] for row in seed_runs}
    available_ids = sorted(predictions)
    for index, left_id in enumerate(available_ids):
        for right_id in available_ids[index + 1 :]:
            value = _daily_rank_correlation(predictions[left_id], predictions[right_id])
            if value is not None:
                correlations[left_id].append(value)
                correlations[right_id].append(value)

    updated_runs: list[dict[str, Any]] = []
    for row in seed_runs:
        model_run_id = str(row["model_run_id"])
        metrics = dict(row.get("metrics") or {})
        # Removed from the score contract in v3; do not retain stale values in
        # newly reviewed run metrics or their reproducibility artifact.
        metrics.pop("top10_holding_overlap", None)
        values = correlations[model_run_id]
        if values:
            metrics["prediction_rank_correlation"] = round(float(statistics.mean(values)), 6)
            metrics["prediction_rank_correlation_pair_count"] = len(values)
            metrics["prediction_rank_correlation_status"] = "computed"
            metrics.pop("prediction_rank_correlation_error", None)
        else:
            metrics.pop("prediction_rank_correlation", None)
            metrics["prediction_rank_correlation_pair_count"] = 0
            metrics["prediction_rank_correlation_status"] = "unavailable"
            metrics["prediction_rank_correlation_error"] = errors.get(model_run_id, "no_comparable_prediction_pairs")
        _write_metrics_artifact(str(row.get("artifact_dir") or ""), metrics)
        updated_runs.append(state.upsert_seed_run({**row, "metrics": metrics}))
    return updated_runs


def seed_consistency_components(all_metrics: dict[int, dict[str, Any]]) -> tuple[dict[str, float], list[str]]:
    if not all_metrics:
        return {
            "return_dispersion_score": 0.0,
            "ir_dispersion_score": 0.0,
            "prediction_rank_corr_score": 0.0,
            "return_std": 0.0,
            "ir_std": 0.0,
        }, ["missing_seed_metrics"]
    ann_values = [_num(metrics, "excess_annualized_ret_with_cost", "annualized_ret") for metrics in all_metrics.values()]
    ir_values = [_num(metrics, "excess_information_ratio_with_cost", "sharpe") for metrics in all_metrics.values()]
    ann_dispersion = statistics.pstdev(ann_values) if len(ann_values) > 1 else 0.0
    ir_dispersion = statistics.pstdev(ir_values) if len(ir_values) > 1 else 0.0
    return_dispersion_score = _scale_down(ann_dispersion, 0.10, 0.30)
    ir_dispersion_score = _scale_down(ir_dispersion, 0.30, 0.90)
    warnings: list[str] = []
    pred_corr_values = [
        _num(metrics, "prediction_rank_correlation", default=None)
        for metrics in all_metrics.values()
        if metrics.get("prediction_rank_correlation") is not None
    ]
    if pred_corr_values:
        prediction_rank_corr_score = _scale_up(statistics.mean(pred_corr_values), 0.30, 0.70)
    else:
        prediction_rank_corr_score = 0.0
        warnings.append("missing_prediction_rank_correlation_scored_zero")
    return {
        "return_dispersion_score": round(return_dispersion_score, 3),
        "ir_dispersion_score": round(ir_dispersion_score, 3),
        "prediction_rank_corr_score": round(prediction_rank_corr_score, 3),
        "return_std": round(ann_dispersion, 6),
        "ir_std": round(ir_dispersion, 6),
    }, warnings


def seed_consistency_score(all_metrics: dict[int, dict[str, Any]]) -> float:
    components, _warnings = seed_consistency_components(all_metrics)
    score = (
        0.40 * components["return_dispersion_score"]
        + 0.35 * components["ir_dispersion_score"]
        + 0.25 * components["prediction_rank_corr_score"]
    )
    return round(_clamp(score), 3)


def _median(values: list[float]) -> float | None:
    return round(float(statistics.median(values)), 6) if values else None


def _dispersion(values: list[float]) -> float | None:
    return round(float(max(values) - min(values)), 6) if len(values) >= 2 else (0.0 if values else None)


def training_diagnostics_summary(seed_runs: list[dict[str, Any]]) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    for row in seed_runs:
        diagnostic = (row.get("metrics") or {}).get("training_diagnostics")
        if isinstance(diagnostic, dict) and diagnostic and diagnostic.get("available") is not False:
            diagnostics.append(dict(diagnostic))
    best_iterations = [int(row["best_iteration"]) for row in diagnostics if row.get("best_iteration") is not None]
    ratios = [float(row["best_iteration_ratio"]) for row in diagnostics if row.get("best_iteration_ratio") is not None]
    gaps = [float(row["train_valid_gap_at_best"]) for row in diagnostics if row.get("train_valid_gap_at_best") is not None]
    deteriorations = [
        float(row["valid_deterioration_after_best"])
        for row in diagnostics
        if row.get("valid_deterioration_after_best") is not None
    ]
    return {
        "available_seed_count": len(diagnostics),
        "early_stopped_seed_count": sum(1 for row in diagnostics if row.get("early_stopped") is True),
        "median_best_iteration": _median([float(value) for value in best_iterations]),
        "min_best_iteration": min(best_iterations) if best_iterations else None,
        "max_best_iteration": max(best_iterations) if best_iterations else None,
        "best_iteration_dispersion": _dispersion([float(value) for value in best_iterations]),
        "median_best_iteration_ratio": _median(ratios),
        "median_train_valid_gap_at_best": _median(gaps),
        "median_valid_deterioration_after_best": _median(deteriorations),
    }


def round_research_metrics(seed_runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a parameter round without letting one lucky seed represent it."""
    def metric_values(*keys: str) -> list[float]:
        values: list[float] = []
        for row in seed_runs:
            metrics = dict(row.get("metrics") or {})
            for key in keys:
                if metrics.get(key) is not None:
                    try:
                        values.append(float(metrics[key]))
                    except (TypeError, ValueError):
                        pass
                    break
        return values

    research_scores = [
        float((row.get("score") or {}).get("research_score", (row.get("score") or {}).get("sota_score")))
        for row in seed_runs
        if (row.get("score") or {}).get("research_score", (row.get("score") or {}).get("sota_score")) is not None
    ]
    ann = metric_values("excess_annualized_ret_with_cost", "annualized_ret")
    ir = metric_values("excess_information_ratio_with_cost", "information_ratio", "sharpe")
    drawdown = [abs(value) for value in metric_values("max_drawdown")]
    rank_ic = metric_values("rank_ic", "ic_mean")
    rank_icir = metric_values("rank_icir", "icir")
    turnover = metric_values("turnover")
    return {
        "seed_count": len(seed_runs),
        "research_score": _median(research_scores),
        "worst_research_score": min(research_scores) if research_scores else None,
        "research_score_dispersion": _dispersion(research_scores),
        "median_excess_annualized_ret_with_cost": _median(ann),
        "worst_excess_annualized_ret_with_cost": min(ann) if ann else None,
        "annualized_return_dispersion": _dispersion(ann),
        "median_excess_information_ratio_with_cost": _median(ir),
        "worst_excess_information_ratio_with_cost": min(ir) if ir else None,
        "information_ratio_dispersion": _dispersion(ir),
        "median_abs_max_drawdown": _median(drawdown),
        "worst_abs_max_drawdown": max(drawdown) if drawdown else None,
        "median_rank_ic": _median(rank_ic),
        "median_rank_icir": _median(rank_icir),
        "median_turnover": _median(turnover),
        "training_summary": training_diagnostics_summary(seed_runs),
    }


def improvement_vs_reference(candidate: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """Return direction-normalized deltas; positive always means better."""
    def delta(key: str, *, lower_is_better: bool = False) -> float | None:
        left, right = candidate.get(key), reference.get(key)
        if left is None or right is None:
            return None
        value = float(right) - float(left) if lower_is_better else float(left) - float(right)
        return round(value, 6)

    return {
        "research_score": delta("research_score"),
        "worst_research_score": delta("worst_research_score"),
        "research_score_dispersion": delta("research_score_dispersion", lower_is_better=True),
        "median_excess_annualized_ret_with_cost": delta("median_excess_annualized_ret_with_cost"),
        "worst_excess_annualized_ret_with_cost": delta("worst_excess_annualized_ret_with_cost"),
        "annualized_return_dispersion": delta("annualized_return_dispersion", lower_is_better=True),
        "median_excess_information_ratio_with_cost": delta("median_excess_information_ratio_with_cost"),
        "worst_excess_information_ratio_with_cost": delta("worst_excess_information_ratio_with_cost"),
        "information_ratio_dispersion": delta("information_ratio_dispersion", lower_is_better=True),
        "median_abs_max_drawdown": delta("median_abs_max_drawdown", lower_is_better=True),
        "worst_abs_max_drawdown": delta("worst_abs_max_drawdown", lower_is_better=True),
        "median_rank_ic": delta("median_rank_ic"),
        "median_rank_icir": delta("median_rank_icir"),
        "median_turnover": delta("median_turnover", lower_is_better=True),
    }


def meaningfully_improves(candidate: dict[str, Any], reference: dict[str, Any], *, min_delta: float = 1.0) -> bool:
    candidate_score = candidate.get("research_score")
    reference_score = reference.get("research_score")
    if candidate_score is None or reference_score is None:
        return False
    return float(candidate_score) >= float(reference_score) + float(min_delta)


def score_research_screening(round_group_id: str, *, state: ModelStateStore | None = None) -> dict[str, Any]:
    state = state or ModelStateStore()
    seed_runs = state.list_seed_runs(round_group_id=round_group_id)
    screening = [row for row in seed_runs if int(row.get("seed") or -1) == 42 and row.get("status") == "completed"]
    if len(screening) != 1:
        return {"ok": False, "err": "screening_seed42_run_required", "found": len(screening)}
    row = screening[0]
    metrics = dict(row.get("metrics") or {})
    components, warnings = individual_score_components(metrics)
    score = individual_performance_score(metrics)
    hard_blocks = performance_hard_blocks(metrics)
    payload = {
            "score_review_version": SCORE_REVIEW_VERSION,
            "model_run_id": row["model_run_id"],
            "round_group_id": round_group_id,
            "seed": 42,
            "phase": "screening",
            "research_score": score,
            "hard_blocks": hard_blocks,
            "component_scores": components,
            "warnings": sorted(set(warnings)),
            "formula": {
                "research_score": "IR 40% + excess return 30% + drawdown 20% + RankIC/RankICIR 10%",
            },
            "decision": "eligible_for_session_comparison" if not hard_blocks else "research_hard_flaw",
            "generated_at": utc_now(),
    }
    state.upsert_seed_run({**row, "score": payload})
    round_payload = state.get_round(round_group_id) or {}
    if round_payload:
        experiment = dict(round_payload.get("experiment") or {})
        metadata = dict(experiment.get("research_metadata") or {})
        metadata["research_score"] = score
        metadata["research_score_phase"] = "screening"
        experiment["research_metadata"] = metadata
        round_payload["experiment"] = experiment
        round_payload["stage"] = "research_score"
        round_payload["updated_at"] = utc_now()
        state.upsert_round(round_payload)
    return {
        "ok": True,
        "round_group_id": round_group_id,
        "score_review_version": SCORE_REVIEW_VERSION,
        "phase": "screening",
        "research_score": score,
        "results": [payload],
    }


def score_round(round_group_id: str, *, state: ModelStateStore | None = None, threshold: float | None = None) -> dict[str, Any]:
    """Compatibility name for the research screening score."""
    del threshold
    return score_research_screening(round_group_id, state=state)
