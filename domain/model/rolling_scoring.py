from __future__ import annotations

import statistics
from typing import Any

from storage.paths import MODEL_ROLLING


ROLLING_SCORE_VERSION = "model_rolling_score_v1"


def _num(metrics: dict[str, Any], *keys: str) -> float:
    for key in keys:
        if metrics.get(key) is not None:
            return float(metrics[key])
    return 0.0


def _clip(value: float) -> float:
    return max(0.0, min(100.0, value))


def _up(value: float, floor: float, cap: float) -> float:
    return _clip((value - floor) / (cap - floor) * 100.0)


def _down(value: float, good: float, bad: float) -> float:
    return _clip((bad - value) / (bad - good) * 100.0)


def portfolio_quality(metrics: dict[str, Any]) -> dict[str, Any]:
    ir = _num(metrics, "excess_information_ratio_with_cost", "excess_information_ratio", "sharpe")
    annualized = _num(metrics, "excess_annualized_ret_with_cost", "excess_annualized_return", "annualized_ret")
    drawdown = abs(_num(metrics, "max_drawdown", "net_max_drawdown"))
    components = {
        "ir_score": round(_up(ir, 0.50, 1.50), 3),
        "return_score": round(_up(annualized, 0.10, 0.60), 3),
        "drawdown_score": round(_down(drawdown, 0.10, 0.30), 3),
    }
    score = 0.45 * components["ir_score"] + 0.35 * components["return_score"] + 0.20 * components["drawdown_score"]
    return {"score": round(_clip(score), 3), "components": components}


def score_rolling_seed(seed_result: dict[str, Any]) -> dict[str, Any]:
    folds = seed_result.get("fold_portfolio_metrics") or {}
    fold_rows = list(folds.values()) if isinstance(folds, dict) else list(folds)
    if len(fold_rows) != 4:
        return {"ok": False, "err": "rolling_score_requires_four_folds", "found": len(fold_rows)}
    overall = portfolio_quality(seed_result.get("rolling_metrics") or {})
    fold_quality = [portfolio_quality(row) for row in fold_rows]
    latest = fold_quality[-1]
    worst = min(fold_quality, key=lambda row: row["score"])
    score = 0.55 * overall["score"] + 0.25 * worst["score"] + 0.20 * latest["score"]
    return {
        "ok": True,
        "score_version": ROLLING_SCORE_VERSION,
        "score": round(score, 3),
        "overall": overall,
        "worst_fold": worst,
        "latest_fold": latest,
        "fold_quality": fold_quality,
    }


def score_rolling_campaign(seed_results: dict[int, dict[str, Any]]) -> dict[str, Any]:
    required = {42, 17, 83}
    if set(seed_results) != required:
        return {"ok": False, "err": "rolling_campaign_requires_seed_42_17_83", "found": sorted(seed_results)}
    per_seed = {seed: score_rolling_seed(result) for seed, result in seed_results.items()}
    if not all(row.get("ok") for row in per_seed.values()):
        return {"ok": False, "err": "rolling_seed_score_failed", "per_seed": per_seed}

    overall_scores = [row["overall"]["score"] for row in per_seed.values()]
    fold_medians: list[float] = []
    for fold_index in range(4):
        fold_medians.append(float(statistics.median(row["fold_quality"][fold_index]["score"] for row in per_seed.values())))
    overall = float(statistics.median(overall_scores))
    worst_fold = min(fold_medians)
    latest_fold = fold_medians[-1]
    rolling_score = round(0.55 * overall + 0.25 * worst_fold + 0.20 * latest_fold, 3)

    stitched_ir = [_num(result.get("rolling_metrics") or {}, "excess_information_ratio_with_cost", "excess_information_ratio", "sharpe") for result in seed_results.values()]
    stitched_returns = [_num(result.get("rolling_metrics") or {}, "excess_annualized_ret_with_cost", "excess_annualized_return", "annualized_ret") for result in seed_results.values()]
    stitched_drawdowns = [abs(_num(result.get("rolling_metrics") or {}, "max_drawdown", "net_max_drawdown")) for result in seed_results.values()]
    fold_irs: list[float] = []
    for fold_index in range(4):
        values = []
        for result in seed_results.values():
            rows = list((result.get("fold_portfolio_metrics") or {}).values())
            values.append(_num(rows[fold_index], "excess_information_ratio_with_cost", "excess_information_ratio", "sharpe"))
        fold_irs.append(float(statistics.median(values)))
    cfg = dict(MODEL_ROLLING)
    gates = {
        "at_least_two_positive_stitched_ir": sum(value > 0 for value in stitched_ir) >= 2,
        "ir_std_within_limit": statistics.pstdev(stitched_ir) <= float(cfg.get("max_ir_std", 0.60)),
        "return_std_within_limit": statistics.pstdev(stitched_returns) <= float(cfg.get("max_return_std", 0.20)),
        "median_drawdown_within_limit": statistics.median(stitched_drawdowns) <= float(cfg.get("max_median_drawdown", 0.30)),
        "at_least_three_positive_fold_ir": sum(value > 0 for value in fold_irs) >= 3,
        "latest_fold_ir_positive": fold_irs[-1] > 0,
    }
    threshold = float(cfg.get("candidate_score_threshold", 70.0))
    return {
        "ok": True,
        "score_version": ROLLING_SCORE_VERSION,
        "rolling_score": rolling_score,
        "components": {"overall_median": round(overall, 3), "worst_fold_median": round(worst_fold, 3), "latest_fold_median": round(latest_fold, 3)},
        "per_seed": per_seed,
        "stability": {
            "stitched_ir": stitched_ir,
            "stitched_return": stitched_returns,
            "stitched_drawdown": stitched_drawdowns,
            "fold_median_ir": fold_irs,
            "ir_std": round(float(statistics.pstdev(stitched_ir)), 6),
            "return_std": round(float(statistics.pstdev(stitched_returns)), 6),
        },
        "gates": gates,
        "candidate_threshold": threshold,
        "candidate_passed": rolling_score >= threshold and all(gates.values()),
    }
