from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from services._base import ServiceResult, err_result, ok_result
from services.data_foundation_service import data_status
from services.factor_research_service import factor_research_run, factor_research_status
from services.model_service import model_status as model_status
from storage.factor_registry import FactorRegistry
from storage.paths import (
    FACTOR_DEFAULT_COST_RATE,
    FACTOR_DEFAULT_END_DATE,
    FACTOR_DEFAULT_REBALANCE_ANCHOR,
    FACTOR_DEFAULT_START_DATE,
    FACTOR_DEFAULT_TOP_FRAC,
    FACTOR_DEFAULT_UNIVERSE,
    FACTOR_DEFAULT_UNIVERSE_DATE,
    FACTOR_VALUE_DEFAULT_END_DATE,
    LATEST_PIPELINE_STATUS_FILE,
    PIPELINE_DEFAULT_FACTOR_SESSIONS,
    PIPELINE_DEFAULT_FACTOR_TARGET,
    PIPELINE_DEFAULT_MODEL_FAMILY,
)



_AUTO_PIPELINE_DIRECTIONS = [
    "Mine A-share value and quality factors around low valuation, earnings quality, and price-volume triggered re-rating.",
    "Mine A-share momentum and trend factors around information diffusion, trend continuation, and price-volume confirmation.",
    "Mine A-share reversal factors around overreaction repair, short-term mean reversion, and volume shock unwinds.",
    "Mine A-share low-volatility and risk-preference factors around volatility regime shifts, defensive preference, and turnover-based crowding.",
    "Mine A-share price-volume factors around VWAP deviation, abnormal volume, and turnover imbalance.",
    "Mine A-share size and liquidity factors around small-cap premium, liquidity pressure, and turnover segmentation.",
]


def _resolve_session_direction(direction: str, session_idx: int) -> str:
    if (direction or '').strip().lower() != 'auto':
        return direction
    return _AUTO_PIPELINE_DIRECTIONS[(session_idx - 1) % len(_AUTO_PIPELINE_DIRECTIONS)]


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return value


def _write_latest_status(payload: dict) -> None:
    LATEST_PIPELINE_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LATEST_PIPELINE_STATUS_FILE.write_text(
        json.dumps(_jsonable(payload), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def pipeline_run(
    *,
    end_date: str | None = FACTOR_VALUE_DEFAULT_END_DATE,
    skip_download: bool = True,
    direction: str = "auto",
    universe: str = FACTOR_DEFAULT_UNIVERSE,
    n_candidates: int = 12,
    n_rounds: int = 6,
    target_adopted: int = PIPELINE_DEFAULT_FACTOR_TARGET,
    factor_sessions: int = PIPELINE_DEFAULT_FACTOR_SESSIONS,
    qgpt_url: str = "http://127.0.0.1:8003",
    mcp_url: str | None = None,
    max_agent_steps: int = 60,
    start_date: str = FACTOR_DEFAULT_START_DATE,
    factor_end_date: str = FACTOR_DEFAULT_END_DATE,
    holding_period: int = 5,
    benchmark: str = "hs300",
    top_frac: float = FACTOR_DEFAULT_TOP_FRAC,
    cost_rate: float = FACTOR_DEFAULT_COST_RATE,
    rebalance_anchor: str | None = FACTOR_DEFAULT_REBALANCE_ANCHOR,
    universe_date: str | None = FACTOR_DEFAULT_UNIVERSE_DATE,
    seed_count: int = 3,
    seed_max_concurrent: int = 3,
    max_direction_attempts: int = 3,
    max_stagnation_rounds: int = 3,
    model_family: str = PIPELINE_DEFAULT_MODEL_FAMILY,
    model_loop_n: int | None = 1,
    model_step_n: int | None = None,
    seed_batch_rounds: int = 0,
    seed_batch_max_candidates: int = 0,
    dry_run: bool = False,
) -> ServiceResult:
    inputs = {
        "end_date": end_date,
        "skip_download": skip_download,
        "direction": direction,
        "universe": universe,
        "n_candidates": n_candidates,
        "n_rounds": n_rounds,
        "target_adopted": target_adopted,
        "factor_sessions": factor_sessions,
        "qgpt_url": qgpt_url,
        "mcp_url": mcp_url,
        "max_agent_steps": max(4, int(max_agent_steps or 60)),
        "start_date": start_date,
        "factor_end_date": factor_end_date,
        "holding_period": holding_period,
        "benchmark": benchmark,
        "top_frac": top_frac,
        "cost_rate": cost_rate,
        "rebalance_anchor": rebalance_anchor,
        "universe_date": universe_date,
        "seed_count": seed_count,
        "seed_max_concurrent": seed_max_concurrent,
        "max_direction_attempts": max_direction_attempts,
        "max_stagnation_rounds": max_stagnation_rounds,
        "model_family": model_family,
        "model_loop_n": model_loop_n,
        "model_step_n": model_step_n,
        "seed_batch_rounds": seed_batch_rounds,
        "seed_batch_max_candidates": seed_batch_max_candidates,
        "dry_run": dry_run,
    }
    registry = FactorRegistry()
    before_summary = registry.summary()

    if dry_run:
        return ok_result(
            inputs=inputs,
            outputs={
                "status": "dry_run",
                "data": data_status().to_dict(),
                "factor": factor_research_status().to_dict(),
                "model": model_status().to_dict(),
                "factor_registry_before": before_summary,
            },
            artifacts={"latest_status_file": str(LATEST_PIPELINE_STATUS_FILE)},
        )

    data_result = data_status()
    data_quality = ((data_result.outputs or {}).get("data_quality_summary") or {}) if data_result.ok else {}
    production_health = ((data_result.outputs or {}).get("production_health") or {}) if data_result.ok else {}
    if not data_result.ok or data_quality.get("passed") is not True or production_health.get("status") == "blocked":
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "inputs": inputs,
            "status": "data_failed",
            "data": data_result.to_dict(),
            "factor_registry_before": before_summary,
        }
        _write_latest_status(payload)
        return err_result(
            "pipeline data quality failed; run data-daily-routine first",
            inputs=inputs,
            outputs=payload,
            artifacts={"latest_status_file": str(LATEST_PIPELINE_STATUS_FILE)},
        )

    factor_runs = []
    imported_total = 0
    adopted_total = 0
    active_before = before_summary.get("active", 0)
    last_factor_result = None

    for session_idx in range(1, factor_sessions + 1):
        remaining = max(target_adopted - imported_total, 1)
        session_direction = _resolve_session_direction(direction, session_idx)
        factor_result = factor_research_run(
            direction=session_direction,
            universe=universe,
            n_candidates=n_candidates,
            n_rounds=n_rounds,
            target_adopted=remaining,
            qgpt_url=qgpt_url,
            mcp_url=mcp_url,
            max_agent_steps=max(4, int(max_agent_steps or 60)),
            start_date=start_date,
            end_date=factor_end_date,
            holding_period=holding_period,
            benchmark=benchmark,
            top_frac=top_frac,
            cost_rate=cost_rate,
            rebalance_anchor=rebalance_anchor,
            universe_date=universe_date,
            seed_count=seed_count,
            seed_max_concurrent=seed_max_concurrent,
            max_direction_attempts=max_direction_attempts,
            max_stagnation_rounds=max_stagnation_rounds,
            dry_run=False,
            submit_wq=False,
        )
        factor_runs.append({"session": session_idx, **factor_result.to_dict()})
        last_factor_result = factor_result
        if not factor_result.ok:
            break
        outputs = factor_result.to_dict().get("outputs", {})
        result_blob = outputs.get("result", {}) if isinstance(outputs, dict) else {}
        summary = result_blob.get("summary", {}) if isinstance(result_blob, dict) else {}
        imported_total += int(summary.get("imported", 0) or 0)
        adopted_total += int(summary.get("import_ready", summary.get("adopted", 0)) or 0)
        active_now = registry.summary().get("active", 0)
        if imported_total >= target_adopted or (active_now - active_before) >= target_adopted:
            break

    after_factor_summary = registry.summary()
    new_active = after_factor_summary.get("active", 0) - active_before

    seed_runs = []
    if new_active < target_adopted and seed_batch_rounds > 0:
        seed_runs.append(
            {
                "status": "ignored",
                "reason": "legacy local seed mining is no longer part of production; QuantGPT MCP owns factor research",
                "requested_seed_batch_rounds": seed_batch_rounds,
                "requested_seed_batch_max_candidates": seed_batch_max_candidates,
            }
        )

    if last_factor_result is None or not last_factor_result.ok or new_active < target_adopted:
        status = "factor_target_not_met"
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "inputs": inputs,
            "status": status,
            "data": data_result.to_dict(),
            "factor_runs": factor_runs,
            "seed_runs": seed_runs,
            "factor_registry_before": before_summary,
            "factor_registry_after": after_factor_summary,
            "factor_target": target_adopted,
            "factor_imported_total": imported_total,
            "factor_new_active": new_active,
        }
        _write_latest_status(payload)
        return err_result(
            f"pipeline factor target not met: imported={imported_total}, new_active={new_active}, target={target_adopted}",
            inputs=inputs,
            outputs=payload,
            artifacts={"latest_status_file": str(LATEST_PIPELINE_STATUS_FILE)},
        )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "inputs": inputs,
        "status": "model_mcp_handoff_required",
        "data": data_result.to_dict(),
        "factor_runs": factor_runs,
        "seed_runs": seed_runs,
        "factor_registry_before": before_summary,
        "factor_registry_after": after_factor_summary,
        "factor_target": target_adopted,
        "factor_imported_total": imported_total,
        "factor_new_active": new_active,
        "model": {
            "status": "not_started",
            "reason": "archived one-shot model training has been removed; start model research through fxalpha-model MCP tools.",
            "recommended_flow": [
                "fxalpha_model_context",
                "fxalpha_model_protocol",
                "fxalpha_model_feature_snapshot",
                "fxalpha_model_session_start",
                "fxalpha_model_submit_experiment",
                "fxalpha_model_run_round",
                "fxalpha_model_score_review",
                "fxalpha_model_confirm_research_round",
                "fxalpha_model_round_synthesis",
                "fxalpha_model_start_production_rolling",
                "fxalpha_model_promote",
            ],
        },
    }
    _write_latest_status(payload)
    return ok_result(inputs=inputs, outputs=payload, artifacts={"latest_status_file": str(LATEST_PIPELINE_STATUS_FILE)})


def pipeline_status() -> ServiceResult:
    latest = {}
    if LATEST_PIPELINE_STATUS_FILE.exists():
        latest = json.loads(LATEST_PIPELINE_STATUS_FILE.read_text(encoding="utf-8"))
    return ok_result(
        outputs={
            "status": latest.get("status", "idle"),
            "latest": latest,
            "data": data_status().to_dict(),
            "factor": factor_research_status().to_dict(),
            "model": model_status().to_dict(),
        },
        artifacts={"latest_status_file": str(LATEST_PIPELINE_STATUS_FILE)},
    )
