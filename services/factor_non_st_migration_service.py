from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from domain.factor_research.factor_compute import (
    audit_factor_value_coverage,
    compute_factor,
    save_factor_frame,
)
from services._base import err_result, ok_result
from services.factor_active_values_service import enqueue_active_values_refresh, factor_active_values_status
from storage.factor_registry import FactorRegistry
from storage.paths import (
    FACTOR_ACTIVE_ADOPTED_VALUES_FILE,
    FACTOR_ACTIVE_ADOPTED_VALUES_MANIFEST,
    FACTOR_ADOPTED_VALUES_FILE,
    FACTOR_DEFAULT_COST_RATE,
    FACTOR_DEFAULT_END_DATE,
    FACTOR_DEFAULT_HOLDING_PERIOD,
    FACTOR_DEFAULT_REBALANCE_ANCHOR,
    FACTOR_DEFAULT_START_DATE,
    FACTOR_PARQUET_DIR,
    FACTOR_REGISTRY_DB,
    FACTOR_VALUE_DEFAULT_END_DATE,
    FACTOR_VALUE_DEFAULT_START_DATE,
    QUANTGPT_ADOPTED_VALUES_FILE,
    RUNTIME_ROOT,
)


TARGET_UNIVERSE = "tradable_non_st"
DIAGNOSTIC_UNIVERSE = "all_market"
CONFIRM_TEXT = "RETIRE_NON_ST_FAILED"
MIGRATION_ROOT = RUNTIME_ROOT / "factor_research" / "non_st_migration"
LATEST_STATUS_FILE = MIGRATION_ROOT / "latest_status.json"
_EVAL_MARKET_CACHE: dict[tuple[str, str, str], tuple[pd.DataFrame, list[str], dict[str, Any]]] = {}
MIGRATION_REVIEW_DEEP_SCORE_FLOOR = 75.0
MIGRATION_SOFT_VETO_REASONS = {"icir_below_threshold"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _run_id() -> str:
    return "nonst_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
    except Exception:
        pass
    return value


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp.{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_metadata(row: dict) -> dict:
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    return metadata if isinstance(metadata, dict) else {}


def _data_column(row: dict, metadata: dict) -> str:
    value = str(metadata.get("data_column") or row.get("name") or row.get("factor_id") or "").strip()
    return "".join(ch if ch.isalnum() else "_" for ch in value)[:40] or str(row.get("factor_id") or "factor")[:40]


def _metrics_from_backtest(backtest_summary: dict, deep_score: float) -> dict:
    return {
        "ic_mean": backtest_summary.get("ic_mean"),
        "icir": backtest_summary.get("ic_ir"),
        "rank_ic": backtest_summary.get("rank_ic_mean"),
        "sharpe": backtest_summary.get("sharpe"),
        "max_drawdown": backtest_summary.get("max_drawdown"),
        "turnover": backtest_summary.get("turnover"),
        "deep_score": deep_score,
    }


def _migration_decision_from_gate(*, deep_score: float, veto_reasons: list[str]) -> dict:
    """Separate active-factor migration safety from new-factor admission.

    A non-ST migration is a production compatibility step for factors that are
    already active. Only hard data or governance vetoes should retire them
    automatically; softer single-factor threshold misses must be reviewed at
    the model-feature-set level before the active pool is shrunk.
    """
    veto_set = set(veto_reasons or [])
    hard_vetoes = sorted(veto_set - MIGRATION_SOFT_VETO_REASONS)
    quality_gate_passed = not veto_set and float(deep_score or 0.0) >= 80.0
    if quality_gate_passed:
        return {
            "passed": True,
            "quality_gate_passed": True,
            "migration_decision": "quality_gate_passed",
            "migration_action": "keep_active",
            "hard_veto_reasons": [],
            "review_reasons": [],
        }

    review_reasons: list[str] = []
    if float(deep_score or 0.0) < 80.0:
        review_reasons.append(f"deep_score_below_admission_threshold:{float(deep_score or 0.0):.1f}<80")
    for reason in sorted(veto_set & MIGRATION_SOFT_VETO_REASONS):
        review_reasons.append(reason)

    if not hard_vetoes:
        return {
            "passed": True,
            "quality_gate_passed": False,
            "migration_decision": "review_keep",
            "migration_action": "keep_active_review",
            "hard_veto_reasons": [],
            "review_reasons": sorted(set(review_reasons)),
        }

    return {
        "passed": False,
        "quality_gate_passed": False,
        "migration_decision": "hard_retire_candidate",
        "migration_action": "retire_candidate",
        "hard_veto_reasons": hard_vetoes,
        "review_reasons": sorted(set(review_reasons)),
    }


def _backtest_summary(result: dict) -> dict:
    keys = [
        "long_short_sharpe",
        "long_short_annual",
        "top_group_sharpe",
        "monotonicity_score",
        "spread",
        "ic_mean",
        "rank_ic_mean",
        "ic_ir",
        "rank_ic_ir",
        "ic_win_rate",
        "annual_return",
        "sharpe",
        "max_drawdown",
        "turnover",
        "wq_fitness",
        "cost_adjusted",
        "cost_rate",
        "total_cost_drag",
    ]
    return {key: result.get(key) for key in keys if key in result}


def _ensure_quantgpt_path() -> None:
    import sys

    root = Path(__file__).resolve().parents[1]
    qgpt = root / "external" / "quantgpt"
    for path in (root, qgpt):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _fetch_eval_market_data(universe: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, list[str], dict[str, Any]]:
    key = (universe, start_date, end_date)
    if key in _EVAL_MARKET_CACHE:
        frame, stock_codes, stats = _EVAL_MARKET_CACHE[key]
        return frame.copy(), list(stock_codes), dict(stats)

    _ensure_quantgpt_path()
    from quantgpt.market_data import filter_non_st_market_data
    from quantgpt.schemas import fetch_market_data

    if universe == TARGET_UNIVERSE:
        raw_df, _ = fetch_market_data(DIAGNOSTIC_UNIVERSE, start_date, end_date)
        filtered_df = filter_non_st_market_data(raw_df)
        stock_codes = sorted(filtered_df["stock_code"].dropna().astype(str).unique().tolist()) if "stock_code" in filtered_df.columns else []
        stats = {
            "base_universe": DIAGNOSTIC_UNIVERSE,
            "target_universe": TARGET_UNIVERSE,
            "mode": "pit_row_level",
            "raw_row_count": int(len(raw_df)),
            "filtered_row_count": int(len(filtered_df)),
            "st_filtered_rows": int(len(raw_df) - len(filtered_df)),
            "raw_stock_count": int(raw_df["stock_code"].nunique()) if "stock_code" in raw_df.columns else 0,
            "filtered_stock_count": int(len(stock_codes)),
            "fields": ["trade_date", "list_status", "st_status", "security_name"],
        }
        _EVAL_MARKET_CACHE[key] = (filtered_df.copy(), stock_codes, stats)
        return filtered_df, stock_codes, stats

    market_df, stock_codes = fetch_market_data(universe, start_date, end_date)
    stats = {
        "base_universe": universe,
        "target_universe": universe,
        "mode": "none",
        "raw_row_count": int(len(market_df)),
        "filtered_row_count": int(len(market_df)),
        "st_filtered_rows": 0,
        "raw_stock_count": len(stock_codes),
        "filtered_stock_count": len(stock_codes),
        "fields": [],
    }
    _EVAL_MARKET_CACHE[key] = (market_df.copy(), list(stock_codes), stats)
    return market_df, list(stock_codes), stats


def _evaluate_factor_official(
    expression: str,
    *,
    universe: str,
    start_date: str,
    end_date: str,
    holding_period: int,
) -> dict:
    _ensure_quantgpt_path()
    from domain.factor_research import quality_gate
    from quantgpt.anti_overfit import run_anti_overfit
    from quantgpt.adversarial_validator import run_adversarial_validation
    from quantgpt.backtest import api_context, run_factor_backtest
    from quantgpt.iteration import compute_local_quick_score

    market_df, stock_codes, st_filter_stats = _fetch_eval_market_data(universe, start_date, end_date)
    with api_context():
        raw_result = run_factor_backtest(
            market_df,
            expression,
            holding_period=holding_period,
            n_groups=5,
            cost_rate=FACTOR_DEFAULT_COST_RATE,
            rebalance_anchor=FACTOR_DEFAULT_REBALANCE_ANCHOR,
        )

    backtest_summary = _backtest_summary(raw_result)
    factor_df = raw_result.get("_factor_df")
    anti = run_anti_overfit(factor_df, holding_period) if factor_df is not None and len(factor_df) >= 100 else {}
    adversarial = run_adversarial_validation(factor_df, holding_period) if factor_df is not None and len(factor_df) >= 100 else {}
    quick = compute_local_quick_score(backtest_summary)
    quick_score = float(quick.get("score") or 0.0)

    novelty_guard = {
        "allowed": True,
        "novelty_score": 1.0,
        "reason": "migration_self_exempt_existing_active_factor",
        "max_existing_pearson": 0.0,
        "max_existing_rank_corr": 0.0,
        "p90_pearson": 0.0,
        "p90_rank_corr": 0.0,
        "thresholds": {"pearson": 0.75, "rank_corr": 0.80, "p90_pearson": 0.70, "p90_rank_corr": 0.75},
    }
    st_guard = {
        "passed": True,
        "reason": "pit_tradable_non_st_universe_filter",
        "avg_top50_ratio": 0.0,
        "p95_top50_ratio": 0.0,
        "top_n": 50,
    }
    candidate = {
        "expression": expression,
        "status": "success",
        "screening_stage": "deep_validation",
        "score": quick_score,
        "quick_score": quick_score,
        "grade": quick.get("grade"),
        "backtest_summary": backtest_summary,
        "anti_overfit": anti,
        "anti_overfit_summary": anti,
        "adversarial_validation": adversarial,
        "novelty_guard": novelty_guard,
        "st_exposure_guard": st_guard,
        "combined_guard": {"allowed": True, "reason": "pit_tradable_non_st_universe_filter"},
        "holding_period_days": holding_period,
    }
    quality_gate._normalize_candidate_evidence(candidate)
    deep_score, score_parts = quality_gate._compute_deep_score(candidate, quick_score=quick_score)
    threshold_checks = quality_gate._threshold_checks(backtest_summary, min_abs_ic=0.02, min_ir=0.3)
    veto_reasons = quality_gate._veto_reasons(candidate, backtest_summary)
    if not quality_gate._has_deep_validation_evidence(candidate):
        veto_reasons.append("requires_deep_validation")
    if not threshold_checks["ic_abs"]["passed"]:
        veto_reasons.append("ic_below_threshold")
    if not threshold_checks["ir_abs"]["passed"]:
        veto_reasons.append("icir_below_threshold")
    veto_reasons = sorted(set(veto_reasons))
    migration_decision = _migration_decision_from_gate(deep_score=deep_score, veto_reasons=veto_reasons)
    quality_gate_passed = bool(migration_decision["quality_gate_passed"])
    passed = bool(migration_decision["passed"])
    return {
        "status": "evaluated",
        "passed": passed,
        "quality_gate_passed": quality_gate_passed,
        "migration_decision": migration_decision["migration_decision"],
        "migration_action": migration_decision["migration_action"],
        "hard_veto_reasons": migration_decision["hard_veto_reasons"],
        "review_reasons": migration_decision["review_reasons"],
        "stock_count": len(stock_codes),
        "row_count": int(len(market_df)),
        "st_filter_stats": st_filter_stats,
        "quick_score": quick_score,
        "quick_grade": quick.get("grade"),
        "backtest_summary": backtest_summary,
        "anti_overfit": anti,
        "adversarial_validation": adversarial,
        "deep_score": deep_score,
        "score_parts": score_parts,
        "threshold_checks": threshold_checks,
        "veto_reasons": veto_reasons,
        "gate_result": {
            "passed": quality_gate_passed,
            "migration_passed": passed,
            "migration_decision": migration_decision["migration_decision"],
            "migration_action": migration_decision["migration_action"],
            "reason": "quality_gate_adopted"
            if quality_gate_passed
            else (",".join(veto_reasons) if veto_reasons else f"deep_score={deep_score}<80"),
            "review_reasons": migration_decision["review_reasons"],
            "hard_veto_reasons": migration_decision["hard_veto_reasons"],
            "deep_score": deep_score,
            "quick_score": quick_score,
            "threshold_checks": threshold_checks,
            "reference_thresholds": {
                "deep_score": 80.0,
                "min_abs_ic": 0.02,
                "min_ir": 0.3,
                "migration_review_deep_score_floor": MIGRATION_REVIEW_DEEP_SCORE_FLOOR,
                "migration_soft_veto_reasons": sorted(MIGRATION_SOFT_VETO_REASONS),
            },
        },
    }


def _factor_series_from_parquet(path: Path) -> pd.Series:
    df = pd.read_parquet(path)
    value_col = df.columns[0]
    frame = df[[value_col]].copy()
    if isinstance(frame.index, pd.MultiIndex):
        names = list(frame.index.names)
        if "datetime" in names and "instrument" in names:
            frame = frame.reset_index()
            frame["trade_date"] = pd.to_datetime(frame["datetime"]).dt.normalize()
            frame["stock_code"] = frame["instrument"].astype(str)
        elif "trade_date" in names and "stock_code" in names:
            series = frame[value_col].copy()
            series.index = series.index.set_names(["stock_code", "trade_date"])
            return series.dropna()
    if {"trade_date", "stock_code"} <= set(frame.columns):
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    elif {"datetime", "instrument"} <= set(frame.columns):
        frame["trade_date"] = pd.to_datetime(frame["datetime"]).dt.normalize()
        frame["stock_code"] = frame["instrument"].astype(str)
    else:
        raise RuntimeError(f"cannot identify factor parquet index: {path}")
    return frame.set_index(["stock_code", "trade_date"])[value_col].dropna()


def _redundancy_review(pass_items: list[dict]) -> dict:
    if len(pass_items) < 2:
        return {"dropped_factor_ids": [], "pairs": []}
    series_by_id: dict[str, pd.Series] = {}
    for item in pass_items:
        try:
            series_by_id[str(item["factor_id"])] = _factor_series_from_parquet(Path(item["staging_path"]))
        except Exception:
            continue
    pairs: list[dict] = []
    dropped: set[str] = set()
    ids = list(series_by_id)
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            joined = pd.concat([series_by_id[left], series_by_id[right]], axis=1, join="inner").dropna()
            if len(joined) < 300:
                continue
            joined.columns = ["left", "right"]
            pearson = float(joined["left"].corr(joined["right"])) if len(joined) else 0.0
            rank = float(joined["left"].rank().corr(joined["right"].rank())) if len(joined) else 0.0
            pair = {"left": left, "right": right, "pearson": pearson, "rank_corr": rank, "common_rows": int(len(joined))}
            pairs.append(pair)
            if abs(pearson) >= 0.75 or abs(rank) >= 0.80:
                left_score = float(next((x.get("deep_score") or 0.0 for x in pass_items if x["factor_id"] == left), 0.0))
                right_score = float(next((x.get("deep_score") or 0.0 for x in pass_items if x["factor_id"] == right), 0.0))
                dropped.add(right if left_score >= right_score else left)
    return {"dropped_factor_ids": sorted(dropped), "pairs": pairs[:100]}


def _plan_path(run_id: str) -> Path:
    return MIGRATION_ROOT / run_id / "plan.json"


def _latest_run_id() -> str:
    if not LATEST_STATUS_FILE.exists():
        return ""
    try:
        return str(_read_json(LATEST_STATUS_FILE).get("run_id") or "")
    except Exception:
        return ""


def factor_non_st_migration_status() -> Any:
    run_id = _latest_run_id()
    latest = _read_json(LATEST_STATUS_FILE) if LATEST_STATUS_FILE.exists() else {}
    active_values = factor_active_values_status().to_dict()
    return ok_result(
        outputs={
            "status": latest.get("status", "missing"),
            "run_id": run_id,
            "latest": latest,
            "active_values": active_values.get("outputs", {}),
        },
        artifacts={"latest_status_file": str(LATEST_STATUS_FILE)},
    )


def factor_non_st_migration_plan(
    *,
    limit: int = 0,
    offset: int = 0,
    run_id: str | None = None,
    target_universe: str = TARGET_UNIVERSE,
    holding_period_days: int = FACTOR_DEFAULT_HOLDING_PERIOD,
    selection_start_date: str = FACTOR_DEFAULT_START_DATE,
    selection_end_date: str = FACTOR_DEFAULT_END_DATE,
    value_start_date: str = FACTOR_VALUE_DEFAULT_START_DATE,
    value_end_date: str = FACTOR_VALUE_DEFAULT_END_DATE,
) -> Any:
    if target_universe != TARGET_UNIVERSE:
        return err_result("target_universe_must_be_tradable_non_st", inputs=locals())
    run_id = run_id or _run_id()
    run_dir = MIGRATION_ROOT / run_id
    staging_dir = run_dir / "staging_parquet"
    registry = FactorRegistry()
    rows = registry.list_active(min_icir=-1e9, holding_period_days=holding_period_days)
    selected = rows[int(offset or 0):]
    if int(limit or 0) > 0:
        selected = selected[: int(limit)]
    results: list[dict] = []

    for idx, row in enumerate(selected, start=1):
        full = registry.get(str(row["factor_id"])) or row
        metadata = _load_metadata(full)
        expression = str(full.get("expression") or "").strip()
        data_column = _data_column(full, metadata)
        item = {
            "factor_id": full.get("factor_id"),
            "name": full.get("name"),
            "expression": expression,
            "old_universe": full.get("universe"),
            "target_universe": target_universe,
            "data_column": data_column,
            "old_data_path": metadata.get("data_path", ""),
            "status": "planned",
            "passed": False,
            "veto_reasons": [],
        }
        try:
            factor_values = compute_factor(
                expression,
                start_date=value_start_date,
                end_date=value_end_date,
                filter_non_st=True,
            )
            coverage = audit_factor_value_coverage(factor_values, value_start_date, value_end_date)
            item["value_coverage_audit"] = coverage
            if factor_values.empty:
                item.update(status="failed", veto_reasons=["no_factor_values"], fail_reason="no_factor_values")
                results.append(item)
                continue
            staging_path = save_factor_frame(expression, data_column, factor_values, output_dir=staging_dir)
            item["staging_path"] = staging_path
            if coverage.get("passed") is not True:
                item.update(status="failed", veto_reasons=[coverage.get("reason") or "value_coverage_failed"], fail_reason="value_coverage_failed")
                results.append(item)
                continue
            eval_result = _evaluate_factor_official(
                expression,
                universe=target_universe,
                start_date=selection_start_date,
                end_date=selection_end_date,
                holding_period=holding_period_days,
            )
            item.update(eval_result)
            item["metrics"] = _metrics_from_backtest(eval_result.get("backtest_summary", {}), float(eval_result.get("deep_score") or 0.0))
        except Exception as exc:
            item.update(status="error", passed=False, veto_reasons=["migration_runtime_error"], error=str(exc)[:1000])
        results.append(item)
        _write_json(
            run_dir / "progress.json",
            {"run_id": run_id, "completed": idx, "total": len(selected), "updated_at": _now(), "results": results},
        )

    pass_items = [item for item in results if item.get("passed")]
    redundancy = _redundancy_review(pass_items)
    redundant_ids = set(redundancy.get("dropped_factor_ids") or [])
    for item in results:
        if item.get("factor_id") in redundant_ids:
            reasons = sorted(set((item.get("veto_reasons") or []) + ["redundancy_cluster_veto"]))
            item["veto_reasons"] = reasons
            item["passed"] = False
            item["gate_result"] = {**(item.get("gate_result") or {}), "passed": False, "reason": ",".join(reasons)}

    pass_items = [item for item in results if item.get("passed")]
    review_items = [item for item in pass_items if item.get("migration_decision") == "review_keep"]
    fail_items = [item for item in results if not item.get("passed")]
    st_filter_stats = next((item.get("st_filter_stats") for item in results if item.get("st_filter_stats")), {})
    backup_plan = {
        "registry_db": str(FACTOR_REGISTRY_DB),
        "active_values": [
            str(path)
            for path in dict.fromkeys((FACTOR_ACTIVE_ADOPTED_VALUES_FILE, FACTOR_ACTIVE_ADOPTED_VALUES_MANIFEST, FACTOR_ADOPTED_VALUES_FILE, QUANTGPT_ADOPTED_VALUES_FILE))
            if Path(path).exists()
        ],
        "factor_parquets": [str(item.get("old_data_path")) for item in results if item.get("old_data_path")],
    }
    payload = {
        "schema_version": "fxalpha_non_st_migration_plan_v1",
        "status": "planned",
        "run_id": run_id,
        "generated_at": _now(),
        "inputs": {
            "limit": limit,
            "offset": offset,
            "target_universe": target_universe,
            "holding_period_days": holding_period_days,
            "selection_start_date": selection_start_date,
            "selection_end_date": selection_end_date,
            "value_start_date": value_start_date,
            "value_end_date": value_end_date,
        },
        "summary": {
            "active_count": len(rows),
            "evaluated_count": len(results),
            "pass_count": len(pass_items),
            "quality_gate_pass_count": len([item for item in pass_items if item.get("quality_gate_passed")]),
            "migration_review_keep_count": len(review_items),
            "fail_count": len(fail_items),
            "retire_candidate_count": len(fail_items),
            "hard_retire_candidate_count": len(fail_items),
            "st_filter_stats": st_filter_stats,
        },
        "pass_factors": [item.get("factor_id") for item in pass_items],
        "review_keep_factors": [item.get("factor_id") for item in review_items],
        "retire_candidates": [item.get("factor_id") for item in fail_items],
        "redundancy_review": redundancy,
        "backup_plan": backup_plan,
        "results": results,
        "artifacts": {
            "run_dir": str(run_dir),
            "plan_path": str(_plan_path(run_id)),
            "staging_dir": str(staging_dir),
        },
    }
    _write_json(_plan_path(run_id), payload)
    _write_json(LATEST_STATUS_FILE, {"status": "planned", "run_id": run_id, "updated_at": _now(), "plan_path": str(_plan_path(run_id)), "summary": payload["summary"]})
    return ok_result(inputs=payload["inputs"], outputs=payload, artifacts=payload["artifacts"])


def _copy_if_exists(src: str | Path, dest_dir: Path) -> str:
    src_path = Path(src)
    if not src_path.exists():
        return ""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src_path.name
    shutil.copy2(src_path, dest)
    return str(dest)


def _update_factor_after_pass(factor_id: str, *, universe: str, metrics: dict, metadata: dict) -> None:
    registry = FactorRegistry()
    registry.update_metrics(factor_id, metrics)
    registry.update_meta(factor_id, metadata)
    conn = sqlite3.connect(str(FACTOR_REGISTRY_DB))
    try:
        conn.execute("UPDATE factors SET universe=?, last_evaluated=? WHERE factor_id=?", (universe, _now(), factor_id))
        conn.commit()
    finally:
        conn.close()


def factor_non_st_migration_execute(*, run_id: str, confirm: str, refresh_model: bool = True) -> Any:
    if confirm != CONFIRM_TEXT:
        return err_result("confirmation_required", inputs={"run_id": run_id, "confirm": confirm}, outputs={"required_confirm": CONFIRM_TEXT})
    plan_file = _plan_path(run_id)
    if not plan_file.exists():
        return err_result("migration_plan_not_found", inputs={"run_id": run_id}, artifacts={"plan_path": str(plan_file)})
    plan = _read_json(plan_file)
    if plan.get("status") not in {"planned", "executed"}:
        return err_result("migration_plan_not_executable", inputs={"run_id": run_id}, outputs={"status": plan.get("status")})
    if plan.get("status") == "executed":
        return ok_result(inputs={"run_id": run_id}, outputs=plan, warnings=["migration_plan_already_executed"], artifacts=plan.get("artifacts", {}))

    run_dir = MIGRATION_ROOT / run_id
    backup_dir = run_dir / "backups" / datetime.now().strftime("%Y%m%d_%H%M%S")
    registry = FactorRegistry()
    backup_artifacts = {
        "registry_db": _copy_if_exists(FACTOR_REGISTRY_DB, backup_dir),
        "active_values": [
            _copy_if_exists(path, backup_dir / "active_values")
            for path in dict.fromkeys((FACTOR_ACTIVE_ADOPTED_VALUES_FILE, FACTOR_ACTIVE_ADOPTED_VALUES_MANIFEST, FACTOR_ADOPTED_VALUES_FILE, QUANTGPT_ADOPTED_VALUES_FILE))
        ],
        "factor_parquets": {},
    }
    backup_artifacts["active_values"] = [path for path in backup_artifacts["active_values"] if path]

    passed, review_kept, retired, errors = [], [], [], []
    retire_reason = f"retired_non_st_migration_failed_{run_id}"
    for item in plan.get("results") or []:
        factor_id = str(item.get("factor_id") or "")
        if not factor_id:
            continue
        if item.get("passed"):
            staging_path = Path(str(item.get("staging_path") or ""))
            row = registry.get(factor_id) or {}
            metadata = _load_metadata(row)
            if not staging_path.exists():
                errors.append({"factor_id": factor_id, "error": "staging_path_missing", "staging_path": str(staging_path)})
                continue
            old_path_raw = str(metadata.get("data_path") or item.get("old_data_path") or "").strip()
            if old_path_raw:
                old_path = Path(old_path_raw)
                if old_path.exists():
                    backup_artifacts["factor_parquets"][factor_id] = _copy_if_exists(old_path, backup_dir / "factor_parquet")
                old_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = old_path.with_name(f".tmp.{run_id}.{old_path.name}")
                shutil.copy2(staging_path, tmp)
                os.replace(tmp, old_path)
                target_path = str(old_path)
            else:
                target_path = str(FACTOR_PARQUET_DIR / Path(staging_path).name)
                shutil.copy2(staging_path, target_path)
            metadata.update(
                {
                    "data_path": target_path,
                    "data_column": item.get("data_column") or metadata.get("data_column"),
                    "universe": TARGET_UNIVERSE,
                    "non_st_migration": {
                        "run_id": run_id,
                        "executed_at": _now(),
                        "old_universe": item.get("old_universe"),
                        "value_universe": TARGET_UNIVERSE,
                        "target_universe": TARGET_UNIVERSE,
                        "staging_path": str(staging_path),
                        "value_coverage_audit": item.get("value_coverage_audit"),
                        "gate_result": item.get("gate_result"),
                    },
                    "value_start_date": plan.get("inputs", {}).get("value_start_date"),
                    "value_end_date": plan.get("inputs", {}).get("value_end_date"),
                    "value_universe": TARGET_UNIVERSE,
                    "target_universe": TARGET_UNIVERSE,
                }
            )
            _update_factor_after_pass(factor_id, universe=TARGET_UNIVERSE, metrics=item.get("metrics") or {}, metadata=metadata)
            passed.append(factor_id)
            if item.get("migration_decision") == "review_keep":
                review_kept.append(factor_id)
        else:
            registry.retire(factor_id, retire_reason)
            retired.append(factor_id)

    refresh_state = {}
    if not errors:
        refresh_state = enqueue_active_values_refresh(
            holding_period_days=int(plan.get("inputs", {}).get("holding_period_days") or FACTOR_DEFAULT_HOLDING_PERIOD),
            trigger=f"non_st_migration:{run_id}",
            refresh_model=refresh_model,
        )

    plan["status"] = "executed" if not errors else "execute_errors"
    plan["executed_at"] = _now()
    plan["execution"] = {
        "passed_factor_ids": passed,
        "review_kept_factor_ids": review_kept,
        "retired_factor_ids": retired,
        "errors": errors,
        "backup_artifacts": backup_artifacts,
        "active_values_refresh": refresh_state,
    }
    _write_json(plan_file, plan)
    _write_json(LATEST_STATUS_FILE, {"status": plan["status"], "run_id": run_id, "updated_at": _now(), "plan_path": str(plan_file), "execution": plan["execution"], "summary": plan.get("summary", {})})
    if errors:
        return err_result("migration_execute_errors", inputs={"run_id": run_id}, outputs=plan, artifacts=plan.get("artifacts", {}))
    return ok_result(inputs={"run_id": run_id, "refresh_model": refresh_model}, outputs=plan, artifacts=plan.get("artifacts", {}))
