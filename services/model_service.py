from __future__ import annotations

import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from services._base import ServiceResult, err_result, ok_result
from services.factor_active_values_service import factor_active_values_status
from storage.model_registry import ModelRegistry
from storage.paths import MODEL_EVALUATION_MODE, PROJECT_ROOT

from domain.data_foundation.stock_metadata import load_stock_identity_map, security_name_for_instrument, stock_identity_cache_status
from domain.model.context import build_context_pack, record_mcp_context
from domain.model.contracts import (
    DEFAULT_PORTFOLIO,
    MODEL_SYSTEM_VERSION,
    is_model_system_version,
    normalize_research_baseline_overrides,
    production_contract,
    utc_now,
)
from domain.model.feature_sets import (
    active_values_readiness,
    feature_set_catalog_summary,
    feature_snapshot,
    model_feature_set_preflight,
)
from domain.model.naming import model_display_projection, rolling_display_projection
from domain.model.production_refit import production_refit_model
from domain.model.orchestrator import (
    MCP_PROMPT_PATH,
    ORCH_PROMPT_PATH,
    model_mcp_prompt,
    model_system_prompt,
    orchestrator_start,
    run_round_synthesis,
)
from domain.model.paths import (
    MODEL_ACTIVE_PRODUCTION,
    MODEL_MCP_TRACES,
    MODEL_ORCHESTRATOR_EVENTS,
    MODEL_ORCHESTRATOR_TRACES,
    MODEL_RESEARCH_STEPS,
)
from domain.model.preflight import model_preflight
from domain.model.qlib_runner import run_round, submit_experiment
from domain.model.research_confirmation import confirm_research_round
from domain.model.scoring import round_research_metrics, score_round
from domain.model.state_store import ModelStateStore, append_jsonl, read_jsonl
from domain.model.walk_forward import ROLLING_ROOT, start_production_rolling


def _decode_metadata(row: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(row.get("metadata") or "{}")
    except Exception:
        return {}


def _model_round_number_map() -> dict[str, int]:
    """Resolve session-local Round numbers without changing immutable ids."""
    mapping: dict[str, int] = {}
    for session in ModelStateStore().list_sessions(limit=500):
        payload = session.get("payload") if isinstance(session.get("payload"), dict) else {}
        for row in payload.get("completed_rounds") or []:
            if not isinstance(row, dict) or row.get("round_no") is None:
                continue
            round_no = int(row["round_no"])
            round_group_id = str(row.get("round_group_id") or "")
            if round_group_id:
                mapping[round_group_id] = round_no
            for seed_row in row.get("seed_results") or []:
                if isinstance(seed_row, dict) and seed_row.get("model_run_id"):
                    mapping[str(seed_row["model_run_id"])] = round_no
    return mapping


@lru_cache(maxsize=256)
def _feature_set_registry_projection(feature_set_id: str) -> dict[str, Any]:
    if not feature_set_id:
        return {}
    path = PROJECT_ROOT / "data" / "model" / "features" / "feature_sets" / feature_set_id / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return {
        "factor_count": payload.get("factor_count"),
        "feature_count": payload.get("feature_count"),
        "feature_snapshot_policy_version": payload.get("feature_snapshot_policy_version"),
        "feature_missing_strategy": payload.get("feature_missing_strategy"),
    }


def _model_registry_rows(status: str = "all", limit: int | None = None) -> list[dict[str, Any]]:
    rows = ModelRegistry().list_models(status)
    round_number_map = _model_round_number_map()
    out: list[dict[str, Any]] = []
    for row in rows:
        md = _decode_metadata(row)
        if not is_model_system_version(md.get("model_system_version")):
            continue
        md.setdefault("feature_set_id", row.get("feature_set_id"))
        # The relational status column is the single lifecycle authority.
        # Historical metadata may contain a pre-migration candidate/production
        # value, so never let it override the current library state.
        md["asset_status"] = row.get("status")
        md.setdefault("model_run_id", row.get("model_run_id"))
        item = dict(row)
        feature_set_projection = _feature_set_registry_projection(str(row.get("feature_set_id") or md.get("feature_set_id") or ""))
        for key in ("factor_count", "feature_count"):
            if not item.get(key) and feature_set_projection.get(key) is not None:
                item[key] = feature_set_projection.get(key)
        for key in ("feature_snapshot_policy_version", "feature_missing_strategy"):
            if md.get(key) in (None, "") and feature_set_projection.get(key) not in (None, ""):
                md[key] = feature_set_projection.get(key)
        item["asset_status"] = row.get("status")
        item["metadata"] = md
        for key in [
            "seed",
            "research_score",
            "confirmed_research_score",
            "research_confirmation",
            "rolling_score",
            "rolling_score_version",
            "rolling_gates",
            "evaluation_mode",
            "score_review_decision",
            "gate_status",
            "feature_snapshot_policy_version",
            "sample_weight_policy",
            "portfolio",
            "benchmark",
        ]:
            if item.get(key) in (None, "") and md.get(key) not in (None, ""):
                item[key] = md.get(key)
        metrics = md.get("metrics") if isinstance(md.get("metrics"), dict) else {}
        for key in [
            "annualized_ret",
            "excess_annualized_ret_with_cost",
            "excess_information_ratio_with_cost",
            "strategy_annualized_ret",
            "strategy_sharpe",
            "max_drawdown",
            "rank_ic",
            "rank_icir",
        ]:
            if item.get(key) is None and metrics.get(key) is not None:
                item[key] = metrics.get(key)
        round_group_id = str(item.get("round_group_id") or md.get("round_group_id") or "")
        round_no = round_number_map.get(str(item.get("model_run_id") or ""))
        if round_no is None and round_group_id:
            round_no = round_number_map.get(round_group_id)
        if round_no is not None:
            item["round_no"] = round_no
        item.update(model_display_projection(item, round_no=round_no))
        out.append(item)
        if limit and len(out) >= limit:
            break
    return out


def _model_formal_registry_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hide confirmation-only Seed17/83 runs from formal model projections.

    The state store and artifacts remain untouched for audit.  A research round
    is represented by its pre-declared official Seed42 run; candidate and
    production records are already aggregate/refit assets and remain visible.
    """
    projected: list[dict[str, Any]] = []
    seen_research_rounds: set[str] = set()
    for row in rows:
        metadata = _row_metadata(row)
        status = str(row.get("status") or "").lower()
        round_group_id = str(row.get("round_group_id") or metadata.get("round_group_id") or "")
        seed_value = row.get("seed") if row.get("seed") is not None else metadata.get("seed")
        try:
            seed = int(seed_value) if seed_value is not None else None
        except (TypeError, ValueError):
            seed = None
        if status == "research" and round_group_id:
            if seed is not None and seed != 42:
                continue
            if round_group_id in seen_research_rounds:
                continue
            seen_research_rounds.add(round_group_id)
            row = {**row, "official_seed": 42, "seed_role": "official"}
        projected.append(row)
    return projected


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def _model_record_is_active(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    status = str(row.get("status") or "").lower()
    if status not in {"queued", "running", "stopping"}:
        return False
    raw_ts = row.get("heartbeat_at") or row.get("updated_at") or row.get("created_at")
    if not raw_ts:
        return status == "running"
    try:
        parsed = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
        max_age_seconds = 15 * 60 if status == "queued" else 6 * 60 * 60
        return -60 <= age_seconds <= max_age_seconds
    except ValueError:
        return status == "running"


def _rolling_campaign_projection(payload: dict[str, Any], *, evidence_path: Path) -> dict[str, Any]:
    """Return the compact, GUI-safe view of one formal Rolling campaign."""
    preliminary = payload.get("preliminary") if isinstance(payload.get("preliminary"), dict) else {}
    preliminary_score = preliminary.get("score") if isinstance(preliminary.get("score"), dict) else {}
    final_score = payload.get("score") if isinstance(payload.get("score"), dict) else {}
    per_seed_scores = final_score.get("per_seed") if isinstance(final_score.get("per_seed"), dict) else {}
    seed_rows: list[dict[str, Any]] = []
    for seed_key, raw_seed in (payload.get("seed_results") or {}).items():
        seed = raw_seed if isinstance(raw_seed, dict) else {}
        fold_metrics = seed.get("fold_portfolio_metrics") if isinstance(seed.get("fold_portfolio_metrics"), dict) else {}
        fold_quality = preliminary_score.get("fold_quality") if str(seed_key) == "42" and isinstance(preliminary_score.get("fold_quality"), list) else []
        folds = []
        for index, (fold_id, metrics) in enumerate(fold_metrics.items()):
            metrics = metrics if isinstance(metrics, dict) else {}
            window = metrics.get("window_contract") if isinstance(metrics.get("window_contract"), dict) else {}
            signal_window = window.get("signal_window") if isinstance(window.get("signal_window"), list) else []
            folds.append(
                {
                    "fold_id": fold_id,
                    "signal_start": signal_window[0] if len(signal_window) > 0 else None,
                    "signal_end": signal_window[1] if len(signal_window) > 1 else None,
                    "annualized_ret": metrics.get("excess_annualized_ret_with_cost"),
                    "information_ratio": metrics.get("excess_information_ratio_with_cost"),
                    "max_drawdown": metrics.get("max_drawdown"),
                    "report_days": metrics.get("report_days"),
                    "quality_score": (fold_quality[index] or {}).get("score") if index < len(fold_quality) and isinstance(fold_quality[index], dict) else None,
                    "last_signal_executed": window.get("last_signal_executed_in_backtest"),
                }
            )
        seed_rows.append(
            {
                "seed": int(seed_key) if str(seed_key).isdigit() else seed_key,
                "status": seed.get("status"),
                "factor_count": seed.get("factor_count"),
                "completed_at": seed.get("completed_at"),
                "rolling_metrics": seed.get("rolling_metrics") or {},
                "diagnostic_score": (
                    (per_seed_scores.get(str(seed_key)) or per_seed_scores.get(seed_key) or {}).get("score")
                    if isinstance(per_seed_scores.get(str(seed_key)) or per_seed_scores.get(seed_key) or {}, dict)
                    else None
                ) if str(seed_key) != "42" else (
                    ((per_seed_scores.get(str(seed_key)) or per_seed_scores.get(seed_key) or {}).get("score"))
                    if isinstance(per_seed_scores.get(str(seed_key)) or per_seed_scores.get(seed_key) or {}, dict)
                    else preliminary_score.get("score")
                ),
                "reliability": seed.get("reliability") or {},
                "folds": folds,
                "artifacts": seed.get("artifacts") or {},
            }
        )
    seed_rows.sort(key=lambda row: (row.get("seed") != 42, row.get("seed") or 0))
    projection = {
        "campaign_id": payload.get("campaign_id") or evidence_path.parent.name,
        "status": payload.get("status"),
        "decision": payload.get("decision"),
        "ok": payload.get("ok"),
        "candidate_created": bool(payload.get("candidate_created")),
        "candidate_model_id": payload.get("candidate_model_id"),
        "source_round_group_id": payload.get("source_round_group_id"),
        "feature_set_id": payload.get("feature_set_id"),
        "portfolio": payload.get("portfolio") or {},
        "rolling_contract": payload.get("rolling_contract") or {},
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "preliminary": {
            "passed": preliminary.get("passed"),
            "score": preliminary_score.get("score"),
            "score_version": preliminary_score.get("score_version"),
            "gates": preliminary.get("gates") or {},
            "overall": preliminary_score.get("overall") or {},
            "latest_fold": preliminary_score.get("latest_fold") or {},
            "worst_fold": preliminary_score.get("worst_fold") or {},
        },
        "final": {
            "available": bool(final_score),
            "rolling_score": final_score.get("rolling_score"),
            "score_version": final_score.get("score_version"),
            "gates": final_score.get("gates") or {},
            "candidate_passed": final_score.get("candidate_passed"),
        },
        "seeds": seed_rows,
        "evidence_path": str(evidence_path),
    }
    projection.update(rolling_display_projection(projection))
    return projection


def _model_rolling_campaigns(limit: int = 10) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not ROLLING_ROOT.exists():
        return rows
    paths = sorted(ROLLING_ROOT.glob("*/campaign.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for path in paths[: max(1, int(limit))]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                rows.append(_rolling_campaign_projection(payload, evidence_path=path))
        except (OSError, ValueError, TypeError):
            continue
    return rows


def _research_record_contract_projection(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Label old research evidence without mutating the append-only journal."""
    if not isinstance(record, dict):
        return record
    projected = dict(record)
    searchable = json.dumps(
        {
            "stage": record.get("stage"),
            "decision": record.get("decision"),
            "next": record.get("next"),
            "next_action": record.get("next_action"),
            "summary": record.get("summary"),
            "evidence_refs": record.get("evidence_refs"),
            "extra": record.get("extra"),
        },
        ensure_ascii=False,
        default=str,
    ).lower()
    legacy = any(
        token in searchable
        for token in ("forward_test", "forward test", "sota_gate", "sota score", "sota threshold", "archive_below_threshold")
    )
    projected["record_era"] = "historical_pre_dual_mode" if legacy else "dual_mode_current"
    projected["current_contract"] = not legacy
    if legacy:
        projected["historical"] = True
    return projected


def _model_evaluation_score(row: dict[str, Any]) -> float:
    metadata = _row_metadata(row)
    production_evidence = str(row.get("status") or "").lower() in {"candidate", "production"} or str(
        row.get("evaluation_mode") or metadata.get("evaluation_mode") or ""
    ).lower() == "production"
    values = [
        row.get("rolling_score"), metadata.get("rolling_score"),
        row.get("research_score"), metadata.get("research_score"),
    ] if production_evidence else [
        row.get("research_score"), metadata.get("research_score"),
        row.get("confirmed_research_score"), metadata.get("confirmed_research_score"),
    ]
    values.extend([row.get("sota_score"), metadata.get("sota_score")])
    for value in values:
        score = _safe_float(value)
        if score is not None:
            return score
    return -999.0


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _row_metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    return _decode_metadata(row)


def _model_ret_candidates(row: dict[str, Any]) -> list[Path]:
    metadata = _row_metadata(row)
    artifact_refs = metadata.get("artifact_refs") if isinstance(metadata.get("artifact_refs"), dict) else {}
    artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
    candidates = [
        artifact_refs.get("ret"),
        artifact_refs.get("ret_pkl"),
        artifacts.get("ret"),
        artifacts.get("ret_pkl"),
        metadata.get("ret_pkl"),
        (Path(str(row.get("run_dir"))) / "ret.pkl") if row.get("run_dir") else None,
        (Path(str(row.get("workspace_path"))) / "ret.pkl") if row.get("workspace_path") else None,
    ]
    paths: list[Path] = []
    for item in candidates:
        if not item:
            continue
        path = Path(str(item))
        if path.exists() and path not in paths:
            paths.append(path)
    return paths


def _model_pred_candidates(row: dict[str, Any]) -> list[Path]:
    metadata = _row_metadata(row)
    artifact_refs = metadata.get("artifact_refs") if isinstance(metadata.get("artifact_refs"), dict) else {}
    artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
    candidates = [
        artifact_refs.get("pred"),
        artifact_refs.get("pred_pkl"),
        artifacts.get("pred"),
        artifacts.get("pred_pkl"),
        metadata.get("pred_pkl"),
        (Path(str(row.get("run_dir"))) / "pred.pkl") if row.get("run_dir") else None,
        (Path(str(row.get("workspace_path"))) / "pred.pkl") if row.get("workspace_path") else None,
    ]
    paths: list[Path] = []
    for item in candidates:
        if not item:
            continue
        path = Path(str(item))
        if path.exists() and path not in paths:
            paths.append(path)
    return paths


def _model_portfolio_artifacts(row: dict[str, Any]) -> dict[str, Any]:
    metadata = _row_metadata(row)
    artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
    direct = metadata.get("direct_qlib") if isinstance(metadata.get("direct_qlib"), dict) else {}
    direct_artifacts = direct.get("artifacts") if isinstance(direct.get("artifacts"), dict) else {}
    portfolio = metadata.get("resolved_portfolio_params") if isinstance(metadata.get("resolved_portfolio_params"), dict) else {}
    out: dict[str, Any] = {}
    payload = artifacts.get("portfolio") or direct_artifacts.get("portfolio") or portfolio.get("portfolio_artifacts") or {}
    # Historical registry rows used the old primary/secondary naming.  Reading
    # their primary artifact remains safe, while all new production runs write
    # the single `portfolio` artifact above.
    if not isinstance(payload, dict):
        payload = artifacts.get("primary_portfolio") or direct_artifacts.get("primary_portfolio") or portfolio.get("primary_artifacts") or {}
    if isinstance(payload, dict):
        out["portfolio"] = {
            key: value
            for key, value in payload.items()
            if value and (key.endswith("_pkl") or key.endswith("_file") or key.endswith("_dir") or key == "portfolio_analysis_dir")
        }
    return out


def _model_portfolio_positions_candidates(row: dict[str, Any]) -> list[Path]:
    artifacts = _model_portfolio_artifacts(row)
    portfolio = artifacts.get("portfolio") if isinstance(artifacts.get("portfolio"), dict) else {}
    candidates = [
        portfolio.get("positions_pkl"),
        portfolio.get("positions_file"),
        (Path(str(row.get("run_dir"))) / "portfolio_analysis" / "positions_normal_1day.pkl") if row.get("run_dir") else None,
        (Path(str(row.get("workspace_path"))) / "portfolio_analysis" / "positions_normal_1day.pkl") if row.get("workspace_path") else None,
        # Read-only fallback for historical primary artifacts.
        (Path(str(row.get("run_dir"))) / "portfolio_analysis" / "primary" / "positions_normal_1day.pkl") if row.get("run_dir") else None,
        (Path(str(row.get("workspace_path"))) / "portfolio_analysis" / "primary" / "positions_normal_1day.pkl") if row.get("workspace_path") else None,
    ]
    paths: list[Path] = []
    for item in candidates:
        if not item:
            continue
        path = Path(str(item))
        if path.exists() and path not in paths:
            paths.append(path)
    return paths


def _model_manifest_candidates(row: dict[str, Any]) -> list[Path]:
    metadata = _row_metadata(row)
    artifact_refs = metadata.get("artifact_refs") if isinstance(metadata.get("artifact_refs"), dict) else {}
    artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
    candidates = [
        artifact_refs.get("manifest"),
        artifacts.get("manifest"),
        metadata.get("manifest"),
        (Path(str(row.get("artifact_dir"))) / "manifest.json") if row.get("artifact_dir") else None,
        (Path(str(row.get("artifact_dir"))) / "direct_qlib_manifest.json") if row.get("artifact_dir") else None,
        (Path(str(row.get("run_dir"))) / "manifest.json") if row.get("run_dir") else None,
        (Path(str(row.get("run_dir"))) / "direct_qlib_manifest.json") if row.get("run_dir") else None,
        (Path(str(row.get("workspace_path"))) / "manifest.json") if row.get("workspace_path") else None,
        (Path(str(row.get("workspace_path"))) / "direct_qlib_manifest.json") if row.get("workspace_path") else None,
    ]
    paths: list[Path] = []
    for item in candidates:
        if not item:
            continue
        path = Path(str(item))
        if path.exists() and path not in paths:
            paths.append(path)
    return paths


def _read_first_json(paths: list[Path]) -> dict[str, Any]:
    for path in paths:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def _model_artifact_dir(row: dict[str, Any] | None) -> Path | None:
    if not row:
        return None
    for manifest_path in _model_manifest_candidates(row):
        if manifest_path.exists():
            return manifest_path.parent
    for path in [*_model_pred_candidates(row), *_model_ret_candidates(row)]:
        if path.exists():
            return path.parent
    for key in ("artifact_dir", "run_dir", "workspace_path"):
        raw = row.get(key)
        if raw:
            path = Path(str(raw))
            if path.exists():
                return path
    return None


def _round_with_dataset_segments(round_row: dict[str, Any]) -> dict[str, Any]:
    if round_row.get("segments") or round_row.get("dataset_segments"):
        return round_row
    for seed in round_row.get("seed_runs") or []:
        manifest = {}
        segments: Any = None
        for manifest_path in _model_manifest_candidates(seed):
            candidate_manifest = _read_first_json([manifest_path])
            candidate_segments = candidate_manifest.get("segments") or candidate_manifest.get("dataset_segments") or candidate_manifest.get("qlib_segments")
            if isinstance(candidate_segments, dict) and candidate_segments:
                manifest = candidate_manifest
                segments = candidate_segments
                break
        if not isinstance(segments, dict) or not segments:
            continue
        out = dict(round_row)
        out["segments"] = segments
        experiment = dict(out.get("experiment") or {})
        experiment.setdefault("segments", segments)
        out["experiment"] = experiment
        out["segment_source_model_run_id"] = seed.get("model_run_id")
        return out
    return round_row


def _portfolio_contract_from_manifest(row: dict[str, Any]) -> dict[str, Any]:
    manifest = _read_first_json(_model_manifest_candidates(row))
    metadata = _row_metadata(row)
    resolved = manifest.get("resolved_portfolio_params") if isinstance(manifest.get("resolved_portfolio_params"), dict) else {}
    portfolio = resolved.get("portfolio") if isinstance(resolved.get("portfolio"), dict) else {}
    if not portfolio:  # Read-only compatibility for already-persisted runs.
        portfolio = resolved.get("primary") if isinstance(resolved.get("primary"), dict) else {}
    return {
        "topk": int(portfolio.get("topk") or metadata.get("topk") or DEFAULT_PORTFOLIO["topk"]),
        "n_drop": int(portfolio.get("n_drop") or metadata.get("n_drop") or DEFAULT_PORTFOLIO["n_drop"]),
        "hold_thresh": int(portfolio.get("hold_thresh") or metadata.get("hold_thresh") or DEFAULT_PORTFOLIO["hold_thresh"]),
        "benchmark": str(resolved.get("benchmark") or metadata.get("benchmark") or "000300sh"),
        "account": float(metadata.get("account") or 100_000_000.0),
        "manifest": manifest,
    }


def _model_curve_from_ret(ret_path: Path, *, include_daily: bool, max_points: int = 260) -> dict[str, Any]:
    try:
        import pandas as pd

        ret_df = pd.read_pickle(ret_path)
    except Exception as exc:
        return {"available": False, "reason": f"failed_to_read_ret_pkl: {exc}", "ret_pkl": str(ret_path)}

    if ret_df is None or not hasattr(ret_df, "copy") or not hasattr(ret_df, "index"):
        return {"available": False, "reason": "ret_pkl_is_not_dataframe_like", "ret_pkl": str(ret_path)}
    if len(ret_df) == 0:
        return {"available": False, "reason": "ret_pkl_is_empty", "ret_pkl": str(ret_path)}

    df = ret_df.copy()
    try:
        df = df.sort_index()
    except Exception:
        pass
    if "return" not in df.columns and "account" not in df.columns:
        return {"available": False, "reason": "ret_pkl_missing_return_or_account", "ret_pkl": str(ret_path)}

    gross_daily = df["return"].fillna(0).astype(float) if "return" in df.columns else None
    cost_daily = df["cost"].fillna(0).astype(float) if "cost" in df.columns else None
    model_daily = gross_daily - cost_daily if gross_daily is not None and cost_daily is not None else gross_daily
    bench_daily = df["bench"].fillna(0).astype(float) if "bench" in df.columns else None
    if "account" in df.columns and _safe_float(df["account"].iloc[0]):
        first_account = float(df["account"].iloc[0])
        account_cum = (df["account"].astype(float) / first_account) - 1.0
    elif model_daily is not None:
        account_cum = (1.0 + model_daily).cumprod() - 1.0
    else:
        account_cum = None
    strategy_cum = (1.0 + model_daily).cumprod() - 1.0 if model_daily is not None else account_cum
    gross_strategy_cum = (1.0 + gross_daily).cumprod() - 1.0 if gross_daily is not None else strategy_cum
    bench_cum = (1.0 + bench_daily).cumprod() - 1.0 if bench_daily is not None else None
    daily_excess = model_daily - bench_daily if model_daily is not None and bench_daily is not None else None
    compounded_daily_excess = (1.0 + daily_excess).cumprod() - 1.0 if daily_excess is not None else None
    relative_cum = ((1.0 + strategy_cum) / (1.0 + bench_cum)) - 1.0 if bench_cum is not None else None
    net_value_gap = strategy_cum - bench_cum if bench_cum is not None else None
    if strategy_cum is None:
        return {"available": False, "reason": "ret_pkl_curve_unavailable", "ret_pkl": str(ret_path)}

    strategy_sharpe = None
    gross_strategy_sharpe = None
    if model_daily is not None:
        try:
            std = float(model_daily.std())
            if std > 1e-12:
                strategy_sharpe = float(model_daily.mean()) / std * (238.0 ** 0.5)
        except Exception:
            strategy_sharpe = None
    if gross_daily is not None:
        try:
            gross_std = float(gross_daily.std())
            if gross_std > 1e-12:
                gross_strategy_sharpe = float(gross_daily.mean()) / gross_std * (238.0 ** 0.5)
        except Exception:
            gross_strategy_sharpe = None

    def nav_max_drawdown(series: Any) -> float | None:
        if series is None or len(series) == 0:
            return None
        try:
            nav = 1.0 + series
            return _safe_float((nav / nav.cummax() - 1.0).min())
        except Exception:
            return None

    strategy_nav_max_drawdown = nav_max_drawdown(strategy_cum)
    gross_nav_max_drawdown = nav_max_drawdown(gross_strategy_cum)

    total = len(df)
    step = max(1, total // max(1, int(max_points or 260)))
    selected_positions = set(range(0, total, step))
    selected_positions.add(total - 1)

    curve: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    for position, (idx, row) in enumerate(df.iterrows()):
        date = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
        strategy_value = _safe_float(strategy_cum.iloc[position])
        gross_strategy_value = _safe_float(gross_strategy_cum.iloc[position]) if gross_strategy_cum is not None else strategy_value
        bench_value = _safe_float(bench_cum.iloc[position]) if bench_cum is not None else None
        account_value = _safe_float(row.get("account")) if hasattr(row, "get") else None
        cost_ratio = _safe_float(row.get("cost")) if hasattr(row, "get") else None
        total_cost = _safe_float(row.get("total_cost")) if hasattr(row, "get") else None
        record = {
            "date": date,
            "model_return": strategy_value,
            "strategy_cumulative_return": strategy_value,
            "net_strategy_cumulative_return": strategy_value,
            "gross_strategy_cumulative_return": gross_strategy_value,
            "benchmark_return": bench_value,
            "benchmark_cumulative_return": bench_value,
            "account_cumulative_return": _safe_float(account_cum.iloc[position]) if account_cum is not None else None,
            "excess_return": _safe_float(relative_cum.iloc[position]) if relative_cum is not None else None,
            "excess_cumulative_return": _safe_float(relative_cum.iloc[position]) if relative_cum is not None else None,
            "relative_cumulative_return": _safe_float(relative_cum.iloc[position]) if relative_cum is not None else None,
            "net_value_gap": _safe_float(net_value_gap.iloc[position]) if net_value_gap is not None else None,
            "compounded_daily_excess_return": _safe_float(compounded_daily_excess.iloc[position]) if compounded_daily_excess is not None else None,
            "cost_drag_cumulative": (gross_strategy_value - strategy_value) if gross_strategy_value is not None and strategy_value is not None else None,
            "daily_model_return": _safe_float(model_daily.iloc[position]) if model_daily is not None else None,
            "daily_net_return": _safe_float(model_daily.iloc[position]) if model_daily is not None else None,
            "daily_gross_return": _safe_float(gross_daily.iloc[position]) if gross_daily is not None else None,
            "daily_benchmark_return": _safe_float(row.get("bench")) if hasattr(row, "get") else None,
            "daily_excess_return": _safe_float(daily_excess.iloc[position]) if daily_excess is not None else None,
            "cost": cost_ratio,
            "cost_value": cost_ratio * account_value if cost_ratio is not None and account_value is not None else None,
            "total_cost_cumulative": total_cost,
            "turnover": _safe_float(row.get("turnover")) if hasattr(row, "get") else None,
            "account": account_value,
        }
        if include_daily:
            daily.append(record)
        if position in selected_positions:
            curve.append(record)

    return {
        "available": True,
        "ret_pkl": str(ret_path),
        "curve": curve,
        "daily": daily if include_daily else None,
        "point_count": len(curve),
        "raw_point_count": total,
        "metrics": {
            "strategy_sharpe": _safe_float(strategy_sharpe),
            "gross_strategy_sharpe": _safe_float(gross_strategy_sharpe),
            "nav_max_drawdown": strategy_nav_max_drawdown,
            "gross_nav_max_drawdown": gross_nav_max_drawdown,
            "benchmark_annualized_ret": _safe_float(bench_daily.mean() * 238.0) if bench_daily is not None else None,
            "net_cumulative_return": _safe_float(strategy_cum.iloc[-1]) if strategy_cum is not None else None,
            "gross_cumulative_return": _safe_float(gross_strategy_cum.iloc[-1]) if gross_strategy_cum is not None else None,
            "benchmark_cumulative_return": _safe_float(bench_cum.iloc[-1]) if bench_cum is not None else None,
            "relative_cumulative_return": _safe_float(relative_cum.iloc[-1]) if relative_cum is not None else None,
            "net_value_gap": _safe_float(net_value_gap.iloc[-1]) if net_value_gap is not None else None,
            "cost_drag_cumulative": _safe_float(gross_strategy_cum.iloc[-1] - strategy_cum.iloc[-1]) if gross_strategy_cum is not None and strategy_cum is not None else None,
            "qlib_annualization_factor": 238,
            "curve_return_basis": "after_cost_compounded_nav",
            "relative_return_basis": "net_strategy_nav_divided_by_benchmark_nav",
        },
        "period": {
            "start": curve[0]["date"] if curve else None,
            "end": curve[-1]["date"] if curve else None,
        },
    }


def _normalize_prediction_series(pred_obj: Any) -> Any:
    import pandas as pd

    if isinstance(pred_obj, pd.Series):
        series = pred_obj.copy()
    elif isinstance(pred_obj, pd.DataFrame):
        if "score" in pred_obj.columns:
            series = pred_obj["score"].copy()
        elif pred_obj.shape[1] == 1:
            series = pred_obj.iloc[:, 0].copy()
        else:
            numeric_cols = [col for col in pred_obj.columns if str(pred_obj[col].dtype).startswith(("float", "int"))]
            if not numeric_cols:
                raise ValueError("prediction dataframe has no numeric score column")
            series = pred_obj[numeric_cols[0]].copy()
    else:
        raise ValueError("prediction artifact is not pandas Series/DataFrame")
    if not isinstance(series.index, pd.MultiIndex):
        raise ValueError("prediction index is not MultiIndex(datetime,instrument)")
    names = list(series.index.names)
    if "datetime" not in names or "instrument" not in names:
        if len(names) >= 2:
            names[0] = "datetime"
            names[1] = "instrument"
            series.index = series.index.set_names(names)
        else:
            raise ValueError("prediction MultiIndex missing datetime/instrument")
    series = series.dropna().astype(float).sort_index()
    return series.rename("score")


def _ret_daily_by_date(daily_rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {str(row.get("date")): row for row in (daily_rows or []) if row.get("date")}


def _daily_cost_value(ret_row: dict[str, Any], account: float | None) -> float | None:
    explicit = _safe_float(ret_row.get("cost_value"))
    if explicit is not None:
        return explicit
    cost_ratio = _safe_float(ret_row.get("cost"))
    if cost_ratio is None or account is None:
        return None
    return cost_ratio * account


def _attach_trade_costs(trade_rows: list[dict[str, Any]], cost_value: float | None) -> list[dict[str, Any]]:
    if not trade_rows or cost_value is None:
        return trade_rows
    total_trade_value = sum(float(_safe_float(row.get("trade_value")) or 0.0) for row in trade_rows)
    if total_trade_value <= 0:
        return trade_rows
    for row in trade_rows:
        trade_value = float(_safe_float(row.get("trade_value")) or 0.0)
        trade_cost = float(cost_value) * trade_value / total_trade_value
        row["trade_cost"] = trade_cost
        row["trade_cost_source"] = "ret_pkl_daily_cost_proportional_allocation"
    return trade_rows


def _load_qlib_positions(positions_path: Path) -> dict[Any, Any]:
    try:
        from domain.model.qlib_direct import _ensure_qlib0627_path

        _ensure_qlib0627_path()
        import pickle

        payload = pickle.loads(positions_path.read_bytes())
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _position_entries(position_obj: Any) -> tuple[float | None, dict[str, dict[str, Any]]]:
    raw = getattr(position_obj, "position", None)
    if raw is None and isinstance(position_obj, dict):
        raw = position_obj.get("position", position_obj)
    if not isinstance(raw, dict):
        return None, {}
    account = _safe_float(raw.get("now_account_value"))
    entries: dict[str, dict[str, Any]] = {}
    for symbol, payload in raw.items():
        if symbol in {"cash", "now_account_value", "init_cash"} or not isinstance(payload, dict):
            continue
        amount = _safe_float(payload.get("amount"))
        price = _safe_float(payload.get("price"))
        weight = _safe_float(payload.get("weight"))
        if amount is None and weight is None:
            continue
        value = (amount * price) if amount is not None and price is not None else None
        entries[str(symbol)] = {
            "amount": amount,
            "price": price,
            "weight": weight,
            "value": value,
            "holding_age_days": payload.get("count_day"),
        }
    return account, entries


def _rank_score_for_day(pred: Any, dt: Any) -> tuple[dict[str, int], dict[str, float | None]]:
    if pred is None:
        return {}, {}
    try:
        day_scores = pred.xs(dt, level="datetime").sort_values(ascending=False)
    except Exception:
        return {}, {}
    ranks = {str(symbol): idx for idx, symbol in enumerate(day_scores.index.tolist(), start=1)}
    scores = {str(symbol): _safe_float(score) for symbol, score in day_scores.items()}
    return ranks, scores


def _daily_contribution_from_positions(
    prev_entries: dict[str, dict[str, Any]],
    current_entries: dict[str, dict[str, Any]],
    prev_account: float | None,
    *,
    name_cache: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not prev_entries or not prev_account:
        return out
    for symbol, prev in prev_entries.items():
        current = current_entries.get(symbol)
        if not current:
            continue
        prev_price = _safe_float(prev.get("price"))
        price = _safe_float(current.get("price"))
        amount = _safe_float(prev.get("amount"))
        if prev_price is None or price is None or amount is None:
            continue
        contribution = amount * (price - prev_price)
        out.append(
            {
                "symbol": symbol,
                "instrument": symbol,
                "security_name": security_name_for_instrument(symbol, name_cache),
                "contribution": contribution,
                "contribution_return": contribution / prev_account if prev_account else None,
                "weight": _safe_float(prev.get("weight")),
                "holding_days": prev.get("holding_age_days"),
                "price_return": (price / prev_price - 1.0) if prev_price else None,
                "max_daily_contribution": contribution,
                "min_daily_contribution": contribution,
            }
        )
    return out


def _daily_breakdown_from_positions(
    positions_path: Path,
    *,
    pred_path: Path | None,
    daily_rows: list[dict[str, Any]] | None,
    portfolio: dict[str, Any],
    max_daily_holdings: int = 30,
) -> dict[str, Any] | None:
    positions = _load_qlib_positions(positions_path)
    if not positions:
        return None
    try:
        import pandas as pd

        pred = _normalize_prediction_series(pd.read_pickle(pred_path)) if pred_path and pred_path.exists() else None
    except Exception:
        pred = None
    try:
        name_cache = load_stock_identity_map()
        cache_status = stock_identity_cache_status()
        name_source = "production_stock_identity_cache"
    except Exception as exc:
        name_cache = {}
        cache_status = {"available": False, "reason": str(exc)}
        name_source = "unavailable"

    ret_by_date = _ret_daily_by_date(daily_rows)
    max_rows = max(1, int(max_daily_holdings or 30))
    sorted_items = sorted(positions.items(), key=lambda item: item[0])
    items: list[dict[str, Any]] = []
    stock_stats: dict[str, dict[str, Any]] = {}
    contribution_stats: dict[str, dict[str, Any]] = {}
    prev_entries: dict[str, dict[str, Any]] = {}
    prev_account: float | None = None

    for dt, position_obj in sorted_items:
        date_text = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
        account, entries = _position_entries(position_obj)
        ret_row = ret_by_date.get(date_text) or {}
        account = account or _safe_float(ret_row.get("account")) or float(portfolio.get("account") or 100_000_000.0)
        ranks, scores = _rank_score_for_day(pred, dt)
        contribution_rows = _daily_contribution_from_positions(prev_entries, entries, prev_account, name_cache=name_cache)
        contribution_by_symbol = {row["symbol"]: row for row in contribution_rows}
        for row in contribution_rows:
            stat = contribution_stats.setdefault(
                row["symbol"],
                {
                    "symbol": row["symbol"],
                    "instrument": row["symbol"],
                    "security_name": row["security_name"],
                    "contribution": 0.0,
                    "holding_days": 0,
                    "max_weight": 0.0,
                    "max_daily_contribution": None,
                    "min_daily_contribution": None,
                },
            )
            value = _safe_float(row.get("contribution")) or 0.0
            stat["contribution"] += value
            stat["holding_days"] += 1
            stat["max_weight"] = max(float(stat.get("max_weight") or 0.0), float(row.get("weight") or 0.0))
            stat["max_daily_contribution"] = value if stat.get("max_daily_contribution") is None else max(float(stat["max_daily_contribution"]), value)
            stat["min_daily_contribution"] = value if stat.get("min_daily_contribution") is None else min(float(stat["min_daily_contribution"]), value)

        holding_rows: list[dict[str, Any]] = []
        for symbol, payload in sorted(entries.items(), key=lambda item: abs(float(item[1].get("weight") or 0.0)), reverse=True):
            name = security_name_for_instrument(symbol, name_cache)
            value = _safe_float(payload.get("value"))
            weight = _safe_float(payload.get("weight"))
            score = scores.get(symbol)
            stat = stock_stats.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "instrument": symbol,
                    "security_name": name,
                    "holding_days": 0,
                    "avg_weight_sum": 0.0,
                    "max_weight": 0.0,
                    "first_hold_date": date_text,
                    "last_hold_date": date_text,
                },
            )
            stat["security_name"] = stat.get("security_name") or name
            stat["holding_days"] += 1
            stat["avg_weight_sum"] += float(weight or 0.0)
            stat["max_weight"] = max(float(stat.get("max_weight") or 0.0), float(weight or 0.0))
            stat["last_hold_date"] = date_text
            contrib = contribution_by_symbol.get(symbol) or {}
            holding_rows.append(
                {
                    "rank": ranks.get(symbol),
                    "symbol": symbol,
                    "instrument": symbol,
                    "security_name": name,
                    "score": score,
                    "weight": weight,
                    "holding_age_days": payload.get("holding_age_days"),
                    "amount": _safe_float(payload.get("amount")),
                    "price": _safe_float(payload.get("price")),
                    "estimated_position_value": value,
                    "contribution": contrib.get("contribution"),
                    "contribution_return": contrib.get("contribution_return"),
                }
            )

        prev_symbols = set(prev_entries)
        current_symbols = set(entries)
        trade_rows: list[dict[str, Any]] = []
        for symbol in sorted(prev_symbols | current_symbols):
            prev = prev_entries.get(symbol, {})
            cur = entries.get(symbol, {})
            delta_amount = (_safe_float(cur.get("amount")) or 0.0) - (_safe_float(prev.get("amount")) or 0.0)
            if abs(delta_amount) <= 1e-9:
                continue
            price = _safe_float(cur.get("price")) or _safe_float(prev.get("price"))
            trade_rows.append(
                {
                    "side": "BUY" if delta_amount > 0 else "SELL",
                    "symbol": symbol,
                    "instrument": symbol,
                    "security_name": security_name_for_instrument(symbol, name_cache),
                    "rank": ranks.get(symbol),
                    "score": scores.get(symbol),
                    "amount_delta": delta_amount,
                    "weight_delta": (_safe_float(cur.get("weight")) or 0.0) - (_safe_float(prev.get("weight")) or 0.0),
                    "trade_value": abs(delta_amount) * price if price is not None else None,
                }
            )
        trade_rows = sorted(trade_rows, key=lambda row: abs(float(row.get("trade_value") or 0.0)), reverse=True)
        top_contributors = sorted(contribution_rows, key=lambda row: abs(float(row.get("contribution") or 0.0)), reverse=True)[: min(8, max_rows)]
        cost_ratio = ret_row.get("cost")
        cost_value = _daily_cost_value(ret_row, account)
        trade_rows = _attach_trade_costs(trade_rows, cost_value)
        item = {
            "date": date_text,
            "account": account,
            "daily_return": ret_row.get("daily_model_return") if ret_row.get("daily_model_return") is not None else ret_row.get("return"),
            "daily_net_return": ret_row.get("daily_net_return"),
            "daily_gross_return": ret_row.get("daily_gross_return"),
            "daily_benchmark_return": ret_row.get("daily_benchmark_return") if ret_row.get("daily_benchmark_return") is not None else ret_row.get("bench"),
            "daily_excess_return": ret_row.get("daily_excess_return"),
            "strategy_cumulative_return": ret_row.get("strategy_cumulative_return") if ret_row.get("strategy_cumulative_return") is not None else ret_row.get("model_return"),
            "gross_strategy_cumulative_return": ret_row.get("gross_strategy_cumulative_return"),
            "benchmark_cumulative_return": ret_row.get("benchmark_cumulative_return") if ret_row.get("benchmark_cumulative_return") is not None else ret_row.get("benchmark_return"),
            "excess_return": ret_row.get("excess_return"),
            "excess_cumulative_return": ret_row.get("excess_cumulative_return"),
            "relative_cumulative_return": ret_row.get("relative_cumulative_return"),
            "net_value_gap": ret_row.get("net_value_gap"),
            "cost_drag_cumulative": ret_row.get("cost_drag_cumulative"),
            "turnover": ret_row.get("turnover"),
            "cost": cost_ratio,
            "cost_value": cost_value,
            "total_cost": cost_value,
            "cost_source": "ret_pkl",
            "holdings": holding_rows[:max_rows],
            "trades": trade_rows[:max_rows],
            "top_contributors": top_contributors,
        }
        items.append(item)
        prev_entries = entries
        prev_account = account

    exposure_rows = []
    for symbol, stat in stock_stats.items():
        holding_days = int(stat.get("holding_days") or 0)
        avg_weight = float(stat.get("avg_weight_sum") or 0.0) / max(1, holding_days)
        contribution = contribution_stats.get(symbol, {})
        exposure_rows.append(
            {
                "symbol": symbol,
                "instrument": symbol,
                "security_name": stat.get("security_name") or security_name_for_instrument(symbol, name_cache),
                "holding_days": holding_days,
                "avg_weight": avg_weight,
                "max_weight": _safe_float(stat.get("max_weight")),
                "first_hold_date": stat.get("first_hold_date"),
                "last_hold_date": stat.get("last_hold_date"),
                "contribution": contribution.get("contribution"),
                "max_daily_contribution": contribution.get("max_daily_contribution"),
                "min_daily_contribution": contribution.get("min_daily_contribution"),
            }
        )
    exposure_rows = sorted(
        exposure_rows,
        key=lambda item: abs(float(item.get("contribution") or 0.0)),
        reverse=True,
    )
    return _jsonable(
        {
            "available": True,
            "source": "qlib_positions_pkl",
            "method": "qlib_position_replay_with_adjacent_day_contribution",
            "positions_pkl": str(positions_path),
            "pred_pkl": str(pred_path) if pred_path else None,
            "topk": int(portfolio.get("topk") or DEFAULT_PORTFOLIO["topk"]),
            "n_drop": int(portfolio.get("n_drop") or DEFAULT_PORTFOLIO["n_drop"]),
            "hold_thresh": int(portfolio.get("hold_thresh") or DEFAULT_PORTFOLIO["hold_thresh"]),
            "items": items,
            "by_date": {item["date"]: item for item in items},
            "dates": [item["date"] for item in items],
            "coverage": {
                "position_dates": len(items),
                "ret_dates": len(ret_by_date),
                "stock_count": len(exposure_rows),
                "daily_rows_returned": len(items),
            },
            "diagnostics": {
                "security_name_source": name_source,
                "security_name_cache": cache_status,
                "native_qlib_positions_available": True,
                "note": "holdings are read from Qlib positions; contributions approximate adjacent-day position PnL by instrument.",
            },
            "stock_exposure": exposure_rows[:50],
        }
    )


def _daily_breakdown_from_pred(
    pred_path: Path,
    *,
    daily_rows: list[dict[str, Any]] | None,
    portfolio: dict[str, Any],
    max_daily_holdings: int = 30,
    positions_path: Path | None = None,
) -> dict[str, Any]:
    if positions_path and positions_path.exists():
        from_positions = _daily_breakdown_from_positions(
            positions_path,
            pred_path=pred_path,
            daily_rows=daily_rows,
            portfolio=portfolio,
            max_daily_holdings=max_daily_holdings,
        )
        if from_positions:
            return from_positions
    try:
        import pandas as pd

        pred = _normalize_prediction_series(pd.read_pickle(pred_path))
    except Exception as exc:
        return {"available": False, "reason": f"failed_to_read_pred_pkl: {exc}", "pred_pkl": str(pred_path)}

    topk = max(1, int(portfolio.get("topk") or DEFAULT_PORTFOLIO["topk"]))
    n_drop = max(0, int(portfolio.get("n_drop") or DEFAULT_PORTFOLIO["n_drop"]))
    hold_thresh = max(1, int(portfolio.get("hold_thresh") or DEFAULT_PORTFOLIO["hold_thresh"]))
    max_rows = max(1, int(max_daily_holdings or 30))
    ret_by_date = _ret_daily_by_date(daily_rows)
    try:
        name_cache = load_stock_identity_map()
        cache_status = stock_identity_cache_status()
        name_source = "production_stock_identity_cache"
    except Exception as exc:
        name_cache = {}
        cache_status = {"available": False, "reason": str(exc)}
        name_source = "unavailable"

    holdings: dict[str, int] = {}
    stats: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    dates = list(pd.DatetimeIndex(pd.to_datetime(pred.index.get_level_values("datetime")).unique()).sort_values())
    for dt in dates:
        date_text = dt.strftime("%Y-%m-%d")
        day_scores = pred.xs(dt, level="datetime").sort_values(ascending=False)
        ranked_symbols = [str(symbol) for symbol in day_scores.index.tolist()]
        keep_universe = set(ranked_symbols[: topk + n_drop])
        target = ranked_symbols[:topk]
        previous = set(holdings)

        for symbol in list(holdings):
            holdings[symbol] += 1
            if symbol not in keep_universe or holdings[symbol] > hold_thresh:
                holdings.pop(symbol, None)
        for symbol in target:
            if len(holdings) >= topk:
                break
            holdings.setdefault(symbol, 1)

        current = set(holdings)
        sold = previous - current
        bought = current - previous
        account = _safe_float((ret_by_date.get(date_text) or {}).get("account")) or float(portfolio.get("account") or 100_000_000.0)
        per_weight = 1.0 / max(1, len(current))

        holding_rows: list[dict[str, Any]] = []
        for rank, symbol in enumerate(target, start=1):
            if symbol not in current:
                continue
            score = _safe_float(day_scores.get(symbol))
            name = security_name_for_instrument(symbol, name_cache)
            stat = stats.setdefault(symbol, {"symbol": symbol, "security_name": name, "holding_days": 0, "avg_weight_sum": 0.0, "max_weight": 0.0, "first_hold_date": date_text, "last_hold_date": date_text})
            stat["security_name"] = stat.get("security_name") or name
            stat["holding_days"] += 1
            stat["avg_weight_sum"] += per_weight
            stat["max_weight"] = max(float(stat.get("max_weight") or 0.0), per_weight)
            stat["last_hold_date"] = date_text
            holding_rows.append(
                {
                    "rank": rank,
                    "symbol": symbol,
                    "instrument": symbol,
                    "security_name": name,
                    "score": score,
                    "weight": per_weight,
                    "holding_age_days": holdings.get(symbol),
                    "estimated_position_value": account * per_weight,
                }
            )

        def trade_row(side: str, symbol: str) -> dict[str, Any]:
            rank = ranked_symbols.index(symbol) + 1 if symbol in ranked_symbols else None
            return {
                "side": side,
                "symbol": symbol,
                "instrument": symbol,
                "security_name": security_name_for_instrument(symbol, name_cache),
                "rank": rank,
                "score": _safe_float(day_scores.get(symbol)) if symbol in day_scores.index else None,
                "weight_delta": per_weight if side == "BUY" else -per_weight,
                "trade_value": account * per_weight,
            }

        trade_rows = [trade_row("SELL", symbol) for symbol in sorted(sold)] + [trade_row("BUY", symbol) for symbol in sorted(bought)]
        ret_row = ret_by_date.get(date_text) or {}
        cost_value = _daily_cost_value(ret_row, account)
        trade_rows = _attach_trade_costs(trade_rows, cost_value)
        top_contributors = [
            {
                "symbol": row["symbol"],
                "instrument": row["instrument"],
                "security_name": row["security_name"],
                "contribution": None,
                "score": row["score"],
                "rank": row["rank"],
                "note": "no_position_price_artifact; ranking proxy only",
            }
            for row in holding_rows[: min(6, len(holding_rows))]
        ]
        item = {
            "date": date_text,
            "account": account,
            "daily_return": ret_row.get("daily_model_return") if ret_row.get("daily_model_return") is not None else ret_row.get("return"),
            "daily_net_return": ret_row.get("daily_net_return"),
            "daily_gross_return": ret_row.get("daily_gross_return"),
            "daily_benchmark_return": ret_row.get("daily_benchmark_return") if ret_row.get("daily_benchmark_return") is not None else ret_row.get("bench"),
            "daily_excess_return": ret_row.get("daily_excess_return"),
            "strategy_cumulative_return": ret_row.get("strategy_cumulative_return") if ret_row.get("strategy_cumulative_return") is not None else ret_row.get("model_return"),
            "gross_strategy_cumulative_return": ret_row.get("gross_strategy_cumulative_return"),
            "benchmark_cumulative_return": ret_row.get("benchmark_cumulative_return") if ret_row.get("benchmark_cumulative_return") is not None else ret_row.get("benchmark_return"),
            "excess_return": ret_row.get("excess_return"),
            "excess_cumulative_return": ret_row.get("excess_cumulative_return"),
            "relative_cumulative_return": ret_row.get("relative_cumulative_return"),
            "net_value_gap": ret_row.get("net_value_gap"),
            "cost_drag_cumulative": ret_row.get("cost_drag_cumulative"),
            "turnover": ret_row.get("turnover"),
            "cost": ret_row.get("cost"),
            "cost_value": cost_value,
            "total_cost": cost_value,
            "cost_source": "ret_pkl",
            "holdings": holding_rows[:max_rows],
            "trades": trade_rows[:max_rows],
            "top_contributors": top_contributors,
        }
        items.append(item)

    exposure_rows = []
    for symbol, stat in stats.items():
        holding_days = int(stat.get("holding_days") or 0)
        avg_weight = float(stat.get("avg_weight_sum") or 0.0) / max(1, holding_days)
        exposure_rows.append(
            {
                "symbol": symbol,
                "instrument": symbol,
                "security_name": stat.get("security_name") or security_name_for_instrument(symbol, name_cache),
                "holding_days": holding_days,
                "avg_weight": avg_weight,
                "max_weight": _safe_float(stat.get("max_weight")),
                "first_hold_date": stat.get("first_hold_date"),
                "last_hold_date": stat.get("last_hold_date"),
                "contribution": None,
            }
        )
    exposure_rows = sorted(exposure_rows, key=lambda item: (item.get("holding_days") or 0, item.get("avg_weight") or 0.0), reverse=True)
    return _jsonable(
        {
            "available": True,
            "source": "derived_from_pred_pkl",
            "method": "topk_drop_hold_replay_from_prediction",
            "pred_pkl": str(pred_path),
            "topk": topk,
            "n_drop": n_drop,
            "hold_thresh": hold_thresh,
            "items": items,
            "by_date": {item["date"]: item for item in items},
            "dates": [item["date"] for item in items],
            "coverage": {
                "prediction_dates": len(dates),
                "ret_dates": len(ret_by_date),
                "stock_count": len(exposure_rows),
                "daily_rows_returned": len(items),
            },
            "diagnostics": {
                "security_name_source": name_source,
                "security_name_cache": cache_status,
                "native_qlib_positions_available": False,
                "note": "0703 historical runs did not persist Qlib positions; this replay uses the formal topk/drop/hold contract and pred.pkl scores.",
            },
            "stock_exposure": exposure_rows[:30],
        }
    )


def _stock_contribution_from_breakdown(breakdown: dict[str, Any]) -> dict[str, Any]:
    if not breakdown.get("available"):
        return {"available": False, "reason": breakdown.get("reason") or "daily_breakdown_unavailable"}
    rows = breakdown.get("stock_exposure") or []
    rows_with_contribution = [row for row in rows if _safe_float(row.get("contribution")) is not None]
    winners = sorted(
        [row for row in rows_with_contribution if (_safe_float(row.get("contribution")) or 0.0) > 0],
        key=lambda row: _safe_float(row.get("contribution")) or 0.0,
        reverse=True,
    )
    losers = sorted(
        [row for row in rows_with_contribution if (_safe_float(row.get("contribution")) or 0.0) < 0],
        key=lambda row: _safe_float(row.get("contribution")) or 0.0,
    )
    positive_total = sum(float(_safe_float(row.get("contribution")) or 0.0) for row in winners)
    negative_total = sum(float(_safe_float(row.get("contribution")) or 0.0) for row in losers)
    top3_positive = sum(float(_safe_float(row.get("contribution")) or 0.0) for row in winners[:3])
    return {
        "available": bool(rows),
        "method": breakdown.get("method") or "derived_holding_exposure_from_pred_replay",
        "reason": "" if rows else "no derived holdings",
        "top_winners": winners[:10] if rows_with_contribution else rows[:10],
        "top_losers": losers[:10],
        "timelines": [],
        "concentration": {
            "positive_total": positive_total if rows_with_contribution else None,
            "negative_total": negative_total if rows_with_contribution else None,
            "top3_positive_share": (top3_positive / positive_total) if positive_total > 0 else None,
            "stock_count": len(rows),
            "contribution_stock_count": len(rows_with_contribution),
        },
        "diagnostics": breakdown.get("diagnostics") or {},
    }


def _metric_pick(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _round_projection_row(round_row: dict[str, Any], index: int, active_job: dict[str, Any] | None) -> dict[str, Any]:
    seed_runs = round_row.get("seed_runs") if isinstance(round_row.get("seed_runs"), list) else []
    # Seed identity is fixed by contract; never turn a lucky seed into the
    # displayed or deployable representative of a round.
    representative_seed = next((item for item in seed_runs if int(item.get("seed") or -1) == 42), seed_runs[0] if seed_runs else {})
    metrics = representative_seed.get("metrics") or {}
    score = representative_seed.get("score") or {}
    gate = representative_seed.get("gate") or {}
    experiment = round_row.get("experiment") if isinstance(round_row.get("experiment"), dict) else {}
    research_metadata = experiment.get("research_metadata") if isinstance(experiment.get("research_metadata"), dict) else {}
    round_metrics = round_research_metrics(seed_runs)
    job_payload = (active_job or {}).get("payload") if isinstance((active_job or {}).get("payload"), dict) else {}
    best_round_group_id = str(job_payload.get("best_round_group_id") or "")
    completed_job_rounds = job_payload.get("completed_rounds") if isinstance(job_payload.get("completed_rounds"), list) else []
    latest_session_round_id = str((completed_job_rounds[-1] if completed_job_rounds else {}).get("round_group_id") or "")
    params = experiment.get("qlib_model_kwargs") or experiment.get("training_hyperparameters") or {}
    portfolio = experiment.get("portfolio") if isinstance(experiment.get("portfolio"), dict) else {}
    if not portfolio:  # Read-only compatibility for historical round evidence.
        portfolio = experiment.get("primary_portfolio") if isinstance(experiment.get("primary_portfolio"), dict) else {}
    explicit_round_no = round_row.get("round_no") if round_row.get("round_no") is not None else research_metadata.get("round_no")
    baseline_kind = str(experiment.get("baseline_kind") or "")
    is_round0 = explicit_round_no == 0 or baseline_kind == "model_orch_round0_baseline"
    round_no = 0 if is_round0 else (explicit_round_no if explicit_round_no is not None else index + 1)
    return {
        "round_no": round_no,
        "round_kind": round_row.get("round_kind") or research_metadata.get("round_kind") or ("baseline" if is_round0 else "tuning"),
        "round_label": round_row.get("round_label") or research_metadata.get("round_label") or ("Round 0 · 基准测试" if is_round0 else f"Round {round_no} · 参数研究"),
        "round_group_id": round_row.get("round_group_id"),
        "feature_set_id": round_row.get("feature_set_id"),
        "stage": round_row.get("stage"),
        "status": round_row.get("status"),
        "updated_at": round_row.get("updated_at"),
        "created_at": round_row.get("created_at"),
        "seed_set": round_row.get("seed_set") or [],
        "seed_count": len(seed_runs),
        "model_run_id": representative_seed.get("model_run_id"),
        "seed": representative_seed.get("seed"),
        "research_score": round_metrics.get("research_score"),
        "gate_status": gate.get("gate_status") or gate.get("status"),
        "registry_status": representative_seed.get("registry_status"),
        "research_confirmation": research_metadata.get("research_confirmation") or {},
        "is_active": bool(active_job and active_job.get("current_round_group_id") == round_row.get("round_group_id")),
        "is_best_session_round": bool(best_round_group_id and best_round_group_id == str(round_row.get("round_group_id") or "")),
        "is_baseline_round": is_round0,
        "is_latest_round": bool(latest_session_round_id and latest_session_round_id == str(round_row.get("round_group_id") or "")),
        "reference_round_group_id": research_metadata.get("reference_round_group_id"),
        "hypothesis": research_metadata.get("hypothesis"),
        "parameter_changes": research_metadata.get("parameter_changes") or [],
        "parameter_group": research_metadata.get("parameter_group"),
        "round_metrics": round_metrics,
        "metrics_brief": {
            "excess_annualized_ret_with_cost": round_metrics.get("median_excess_annualized_ret_with_cost"),
            "worst_excess_annualized_ret_with_cost": round_metrics.get("worst_excess_annualized_ret_with_cost"),
            "excess_information_ratio_with_cost": round_metrics.get("median_excess_information_ratio_with_cost"),
            "worst_excess_information_ratio_with_cost": round_metrics.get("worst_excess_information_ratio_with_cost"),
            "max_drawdown": (-float(round_metrics["median_abs_max_drawdown"])) if round_metrics.get("median_abs_max_drawdown") is not None else None,
            "worst_max_drawdown": (-float(round_metrics["worst_abs_max_drawdown"])) if round_metrics.get("worst_abs_max_drawdown") is not None else None,
            "worst_abs_max_drawdown": round_metrics.get("worst_abs_max_drawdown"),
            "rank_ic": round_metrics.get("median_rank_ic"),
            "rank_icir": round_metrics.get("median_rank_icir"),
            "turnover": round_metrics.get("median_turnover"),
        },
        "params_brief": {
            "learning_rate": _metric_pick(params.get("learning_rate"), params.get("lr")),
            "num_leaves": params.get("num_leaves"),
            "min_data_in_leaf": params.get("min_data_in_leaf"),
            "lambda_l1": params.get("lambda_l1"),
            "lambda_l2": params.get("lambda_l2"),
            "topk": portfolio.get("topk") or DEFAULT_PORTFOLIO["topk"],
            "n_drop": portfolio.get("n_drop") or DEFAULT_PORTFOLIO["n_drop"],
            "benchmark": experiment.get("benchmark") or "000300sh",
        },
        "seed_runs": seed_runs,
    }


def _seed_stability_projection(seed_runs: list[dict[str, Any]]) -> dict[str, Any]:
    if not seed_runs:
        return {"available": False, "verdict": "waiting_for_seed_runs"}
    ann = [
        _safe_float((row.get("metrics") or {}).get("excess_annualized_ret_with_cost") or (row.get("metrics") or {}).get("annualized_ret"))
        for row in seed_runs
    ]
    ir = [
        _safe_float((row.get("metrics") or {}).get("excess_information_ratio_with_cost") or (row.get("metrics") or {}).get("information_ratio"))
        for row in seed_runs
    ]
    scores = [_safe_float((row.get("score") or {}).get("research_score") or (row.get("score") or {}).get("sota_score")) for row in seed_runs]
    ann_values = [value for value in ann if value is not None]
    ir_values = [value for value in ir if value is not None]
    score_values = [value for value in scores if value is not None]
    positive_ratio = sum(1 for value in ann_values if value > 0) / len(ann_values) if ann_values else None
    ann_mean = sum(ann_values) / len(ann_values) if ann_values else None
    ir_mean = sum(ir_values) / len(ir_values) if ir_values else None
    return {
        "available": True,
        "verdict": "stable_enough" if positive_ratio == 1 and len(seed_runs) >= 3 else "review_required",
        "seed_count": len(seed_runs),
        "positive_seed_ratio": positive_ratio,
        "ann_mean": ann_mean,
        "ann_min": min(ann_values) if ann_values else None,
        "ann_max": max(ann_values) if ann_values else None,
        "ann_dispersion": (max(ann_values) - min(ann_values)) if len(ann_values) >= 2 else None,
        "ir_mean": ir_mean,
        "ir_min": min(ir_values) if ir_values else None,
        "ir_max": max(ir_values) if ir_values else None,
        "score_mean": sum(score_values) / len(score_values) if score_values else None,
        "score_min": min(score_values) if score_values else None,
        "score_max": max(score_values) if score_values else None,
        "seed_models": [
            {
                "seed": row.get("seed"),
                "model_run_id": row.get("model_run_id"),
                "status": row.get("status"),
                "registry_status": row.get("registry_status"),
                "research_score": (row.get("score") or {}).get("research_score") or (row.get("score") or {}).get("sota_score"),
                "gate_status": (row.get("gate") or {}).get("gate_status") or (row.get("gate") or {}).get("status"),
                "annualized_ret": (row.get("metrics") or {}).get("annualized_ret"),
                "excess_annualized_ret_with_cost": (row.get("metrics") or {}).get("excess_annualized_ret_with_cost"),
                "excess_information_ratio_with_cost": (row.get("metrics") or {}).get("excess_information_ratio_with_cost"),
                "max_drawdown": (row.get("metrics") or {}).get("max_drawdown"),
                "training_diagnostics": (row.get("metrics") or {}).get("training_diagnostics") or {},
            }
            for row in seed_runs
        ],
    }


def _stage_flow_projection(active_job: dict[str, Any] | None) -> list[dict[str, Any]]:
    current_stage = str((active_job or {}).get("stage") or "")
    current_seq = _stage_seq(current_stage)
    job_status = str((active_job or {}).get("status") or "")
    labels = [
        ("protocol_load", "protocol_load"),
        ("context_review", "context_review"),
        ("feature_snapshot", "feature_snapshot"),
        ("experiment_plan", "experiment_plan"),
        ("train_backtest_seed42", "Seed 42 训练回测"),
        ("research_score", "研究评分"),
        ("research_confirmation", "优胜轮 Seed 确认"),
        ("registry_write", "registry_write"),
        ("round_synthesis", "round_synthesis"),
        ("checkpoint_stop", "checkpoint_stop"),
    ]
    rows = []
    for key, label in labels:
        seq = _stage_seq(key)
        if not active_job:
            status = "waiting"
        elif job_status in {"failed", "cancelled", "interrupted"} and key == current_stage:
            status = "failed"
        elif seq < current_seq or job_status == "completed":
            status = "done"
        elif seq == current_seq:
            status = "running" if job_status == "running" else job_status or "waiting"
        else:
            status = "waiting"
        rows.append({"key": key, "label": label, "status": status})
    return rows


def _row_timestamp(row: dict[str, Any]) -> str:
    return str(row.get("ts") or row.get("updated_at") or row.get("created_at") or "")


def _event_execution_decision(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "")
    status = str(event.get("status") or "")
    if event_type == "checkpoint_stop_recorded":
        return "recorded_continue"
    if event_type == "checkpoint_stop":
        return "stopped"
    if event_type == "blocked":
        return "blocked"
    if status == "interrupted":
        return "stopped"
    if status == "running":
        return "recorded_continue"
    return "recorded"


def _event_execution_label(decision: str) -> str:
    return {
        "recorded_continue": "已记录但继续执行",
        "stopped": "已暂停等待人工复核",
        "blocked": "已阻断",
        "recorded": "已记录",
    }.get(decision, decision or "已记录")


def _looks_like_llm_review_signal(step: dict[str, Any]) -> bool:
    decision = str(step.get("decision") or "")
    next_stage = str(step.get("next") or "")
    stage = str(step.get("stage") or "")
    extra = step.get("extra") if isinstance(step.get("extra"), dict) else {}
    text_blob = " ".join(
        str(item or "")
        for item in [
            decision,
            next_stage,
            stage,
            step.get("summary"),
            extra.get("next_experiment_guidance"),
            extra.get("next_parameter_change_rationale"),
        ]
    ).lower()
    if decision in {"checkpoint_stop", "blocked"} or next_stage in {"human_review", "checkpoint_stop", "blocker"}:
        return True
    return any(marker in text_blob for marker in ["human_review", "checkpoint_stop", "manual review", "human review"])


def _latest_llm_review_signal(
    research_steps: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    active_session: dict[str, Any] | None = None,
    active_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_session = str((active_session or {}).get("session_id") or "")
    selected_job = str((active_job or {}).get("job_id") or "")

    def in_scope(row: dict[str, Any]) -> bool:
        if selected_session and str(row.get("session_id") or "") == selected_session:
            return True
        if selected_job and str(row.get("job_id") or "") == selected_job:
            return True
        return not selected_session and not selected_job

    scoped_steps = [row for row in research_steps if in_scope(row)]
    candidates = [row for row in scoped_steps if _looks_like_llm_review_signal(row)]
    if not candidates:
        # Do not project a previous session's checkpoint into a newly selected
        # session merely because that session has not emitted one yet.
        if selected_session or selected_job:
            return {"active": False}
        candidates = [row for row in research_steps if _looks_like_llm_review_signal(row)]
    if not candidates:
        return {"active": False}

    step = candidates[-1]
    extra = step.get("extra") if isinstance(step.get("extra"), dict) else {}
    round_group_id = str(step.get("round_group_id") or extra.get("round_group_id") or "")
    job_id = str(step.get("job_id") or "")
    session_id = str(step.get("session_id") or "")
    matching_events = [
        event for event in events
        if str(event.get("event_type") or "") in {"checkpoint_stop", "checkpoint_stop_recorded", "blocked"}
        and (not round_group_id or str(event.get("round_group_id") or "") == round_group_id)
        and (not job_id or str(event.get("job_id") or "") == job_id)
        and (not session_id or str(event.get("session_id") or "") == session_id)
    ]
    event = sorted(matching_events, key=_row_timestamp)[-1] if matching_events else {}
    llm_decision = str(step.get("decision") or extra.get("decision") or "")
    llm_next = str(step.get("next") or extra.get("next") or "")
    execution_decision = _event_execution_decision(event) if event else (
        "blocked" if llm_decision == "blocked" else "stopped" if llm_decision == "checkpoint_stop" else "recorded"
    )
    if llm_decision != "checkpoint_stop" and llm_next != "human_review" and execution_decision == "recorded_continue":
        execution_decision = "recorded"
    reason = (
        extra.get("next_experiment_guidance")
        or extra.get("next_parameter_change_rationale")
        or step.get("summary")
        or ""
    )
    return _jsonable(
        {
            "active": True,
            "severity": "danger" if execution_decision == "blocked" else "warning",
            "llm_decision": llm_decision,
            "llm_next": llm_next,
            "execution_decision": execution_decision,
            "execution_label": _event_execution_label(execution_decision),
            "round_no": step.get("round_no"),
            "round_group_id": round_group_id,
            "job_id": job_id,
            "session_id": session_id,
            "stage": step.get("stage"),
            "ts": step.get("ts"),
            "reason_summary": reason,
            "evidence_refs": step.get("evidence_refs") if isinstance(step.get("evidence_refs"), list) else [],
            "source_step": step,
            "execution_event": event,
        }
    )


def _build_gui_projection(
    *,
    rounds: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    active_job: dict[str, Any] | None,
    seed_runs: list[dict[str, Any]],
    research_steps: list[dict[str, Any]],
    context_summary: dict[str, Any],
    llm_review_signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest_first_rounds = sorted(rounds, key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    chronological = list(reversed(latest_first_rounds))
    comparison_rows = [
        _round_projection_row(round_row, index, active_job)
        for index, round_row in enumerate(chronological)
    ]
    comparison_rows = list(reversed(comparison_rows))
    latest_round = latest_first_rounds[0] if latest_first_rounds else {}
    current_round_id = (active_job or {}).get("current_round_group_id") or latest_round.get("round_group_id")
    current_round = next((row for row in rounds if row.get("round_group_id") == current_round_id), latest_round)
    current_seed_runs = current_round.get("seed_runs") if isinstance(current_round.get("seed_runs"), list) else [
        row for row in seed_runs if row.get("round_group_id") == current_round_id
    ]
    latest_research = research_steps[-1] if research_steps else {}
    latest_transition = latest_research.get("stage_transition") if isinstance(latest_research.get("stage_transition"), dict) else {}
    timeline = list(reversed(research_steps[-30:]))
    model_run_id = next((row.get("model_run_id") for row in current_seed_runs if row.get("model_run_id")), "")
    data_sources = {
        "research_current": {
            "path": "runtime/model/research_steps/current.jsonl",
            "records": len(research_steps),
            "updated_at": latest_research.get("ts"),
            "meaning": "current research narrative and decisions",
        },
        "research_journal": {
            "path": "runtime/model/research_steps/current.jsonl",
            "records": len(research_steps),
            "updated_at": latest_research.get("ts"),
            "meaning": "latest-first journal for GUI",
        },
        "session_record": {
            "path": "runtime/model/jobs.sqlite",
            "records": len(jobs),
            "updated_at": (active_job or {}).get("updated_at"),
            "meaning": "job status truth source",
        },
        "rounds": {
            "path": "runtime/model/jobs.sqlite",
            "records": len(rounds),
            "updated_at": latest_round.get("updated_at"),
            "meaning": "round and seed run truth source",
        },
    }
    return _jsonable(
        {
            "trust_state": {
                "status": "ok",
                "warnings": [],
                "truth_sources": data_sources,
            },
            "process_progress": {
                "job_id": (active_job or {}).get("job_id"),
                "status": (active_job or {}).get("status"),
                "stage": (active_job or {}).get("stage"),
                "mode": (active_job or {}).get("mode"),
                "current_round": len(chronological) if chronological else None,
                "current_round_group_id": current_round_id,
                "latest_model_run_id": model_run_id,
                "model_policy": "qlib_lgbm_canonical",
                "next_action": latest_research.get("next") or latest_transition.get("next_stage") or "",
                "stage_flow": _stage_flow_projection(active_job),
                "data_sources": data_sources,
            },
            "research_progress": {
                "latest": latest_research,
                "timeline": timeline,
                "summary": latest_research.get("summary"),
                "decision": latest_research.get("decision"),
                "next": latest_research.get("next"),
                "llm_review_signal": llm_review_signal or {"active": False},
            },
            "llm_review_signal": llm_review_signal or {"active": False},
            "research_current": {"state": latest_research, "record": latest_research},
            "candidate_rounds": {
                "flow_round_no": len(chronological) if chronological else None,
                "latest_completed_round_no": next((row.get("round_no") for row in comparison_rows if row.get("status") in {"completed", "failed"}), None),
                "current_candidate_round": next((row for row in comparison_rows if row.get("round_group_id") == current_round_id), comparison_rows[0] if comparison_rows else {}),
                "comparison_rows": comparison_rows,
            },
            "quality_gate_summary": {
                "seed_stability": _seed_stability_projection(current_seed_runs),
                "validation_gate": "production_rolling",
                "research_confirmation": (((current_round or {}).get("experiment") or {}).get("research_metadata") or {}).get("research_confirmation") or {},
            },
            "research_subject": {
                "active_feature_set": {
                    "feature_set_id": current_round.get("feature_set_id") or context_summary.get("selected_feature_set_id"),
                    "feature_set_count": context_summary.get("feature_set_count"),
                    "selected_feature_set_id": context_summary.get("selected_feature_set_id"),
                }
            },
            "latest_judgment": latest_research,
            "active_round_view": next((row for row in comparison_rows if row.get("round_group_id") == current_round_id), comparison_rows[0] if comparison_rows else {}),
            "round_evolution": comparison_rows,
            "research_timeline": timeline,
            "stop_state": {
                "active": bool((active_job or {}).get("status") in {"failed", "cancelled", "interrupted"}),
                "reasons": [str((active_job or {}).get("payload", {}).get("err") or (active_job or {}).get("status") or "")] if active_job else [],
            },
            "diagnostics": {
                "warnings": [],
                "context_summary": context_summary,
            },
        }
    )


def model_status(*, compact: bool = False) -> ServiceResult:
    try:
        state = ModelStateStore()
        rounds = [_round_with_dataset_segments(row) for row in state.list_rounds(limit=8 if compact else 20)]
        jobs = state.list_jobs(limit=8 if compact else 20)
        sessions = state.list_sessions(limit=8 if compact else 20)
        active_job = state.active_managed_job() or next((job for job in jobs if _model_record_is_active(job)), None)
        latest_job = jobs[0] if jobs else None
        active_session = next((row for row in sessions if _model_record_is_active(row)), None)
        latest_session = sessions[0] if sessions else None
        legacy_session = _legacy_session_from_job(active_job or latest_job, state=state, rounds=rounds) if not sessions else None
        if active_job and active_session is None:
            active_session = legacy_session
        latest_session = latest_session or legacy_session
        visible_sessions = sessions + ([legacy_session] if legacy_session else [])
        recent_seed_runs = state.list_seed_runs(limit=12 if compact else 30)
        rolling_campaigns = _model_rolling_campaigns(limit=3 if compact else 10)
        latest_research_steps = read_jsonl(MODEL_RESEARCH_STEPS, limit=30 if compact else 80)
        events_tail = [] if compact else list(reversed(read_jsonl(MODEL_ORCHESTRATOR_EVENTS, limit=50, include_payload=False)))
        orch_traces_tail = [] if compact else read_jsonl(MODEL_ORCHESTRATOR_TRACES, limit=100, include_payload=False)
        mcp_traces_tail = [] if compact else read_jsonl(MODEL_MCP_TRACES, limit=100, include_payload=False)
        current_context = _current_context_summary(active_job, state)
        active_values_status_result = factor_active_values_status()
        active_values_status_outputs = active_values_status_result.outputs if active_values_status_result.ok else {}
        active_values_ready = (
            active_values_readiness(summary=active_values_status_outputs)
            if active_values_status_outputs
            else active_values_readiness()
        )
        if active_values_status_outputs:
            active_values_ready = {
                **active_values_ready,
                "refresh_status": active_values_status_outputs.get("refresh_status") or active_values_ready.get("refresh_status"),
                "active_values_status": active_values_status_outputs.get("active_values_status") or active_values_ready.get("active_values_status"),
                "safe_to_freeze_feature_set": active_values_status_outputs.get("safe_to_freeze_feature_set", active_values_ready.get("safe_to_freeze_feature_set")),
                "feature_snapshot_blocked": active_values_status_outputs.get("feature_snapshot_blocked", active_values_ready.get("feature_snapshot_blocked")),
                "feature_snapshot_block_reason": active_values_status_outputs.get("feature_snapshot_block_reason") or active_values_ready.get("feature_snapshot_block_reason"),
                "active_values_job": active_values_status_outputs.get("active_values_job") or {},
                "resume_available": active_values_status_outputs.get("resume_available") or False,
                "resume_action": active_values_status_outputs.get("resume_action") or "",
            }
        llm_review_signal = _latest_llm_review_signal(
            latest_research_steps,
            events_tail,
            active_session=active_session,
            active_job=active_job,
        )
        registry_rows = _model_registry_rows("all")
        outputs = {
            "status": (
                "running"
                if active_job and active_job.get("status") == "running"
                else "ready"
                if active_values_ready.get("safe_to_freeze_feature_set")
                else "blocked"
            ),
            "active_default_mode": MODEL_EVALUATION_MODE,
            "model_system_version": MODEL_SYSTEM_VERSION,
            "contract": production_contract(),
            "rounds": rounds,
            "recent_rounds": rounds,
            "recent_seed_runs": recent_seed_runs,
            "orchestrator": {
                "active_session": active_session,
                "active_job": active_job,
                "latest_session": latest_session,
                "latest_job": latest_job,
                "jobs": jobs,
                "sessions": visible_sessions,
                "current_context_summary": current_context,
            },
            "active_session": active_session,
            "latest_session": latest_session,
            "latest_job": latest_job,
            "sessions": visible_sessions,
            "session_rounds": [
                round_row
                for round_row in rounds
                if active_session and round_row.get("round_group_id") in set(active_session.get("round_group_ids") or [])
            ],
            "session_blockers": (active_session or {}).get("blocker_history") or ([] if not (active_session or {}).get("current_blocker") else [(active_session or {}).get("current_blocker")]),
            "status_source_map": _status_source_map(
                jobs=jobs,
                sessions=visible_sessions,
                rounds=rounds,
                seed_runs=recent_seed_runs,
                active_session=active_session,
                active_job=active_job,
                research_step_count=len(latest_research_steps),
                events_count=len(events_tail),
                orch_trace_count=len(orch_traces_tail),
                mcp_trace_count=len(mcp_traces_tail),
                context_summary=current_context,
            ),
            "latest_research_steps": list(reversed(latest_research_steps[-20:])),
            "research_current": {"state": latest_research_steps[-1] if latest_research_steps else None},
            "latest_decision": latest_research_steps[-1] if latest_research_steps else {},
            "llm_review_signal": llm_review_signal,
            "live_session": {
                "session_id": (active_session or {}).get("session_id"),
                "job_id": (active_job or {}).get("job_id"),
                "status": (active_job or {}).get("status"),
                "stage": (active_job or {}).get("stage"),
                "current_round_group_id": (active_job or {}).get("current_round_group_id"),
                "latest_model_run_id": next((row.get("model_run_id") for row in recent_seed_runs if row.get("model_run_id")), ""),
            },
            "stage_flow": _stage_flow_projection(active_job),
            "gui_projection": _build_gui_projection(
                rounds=rounds,
                jobs=jobs,
                active_job=active_job,
                seed_runs=recent_seed_runs,
                research_steps=latest_research_steps,
                context_summary=current_context,
                llm_review_signal=llm_review_signal,
            ),
            "rolling_campaigns": rolling_campaigns,
            "latest_rolling_campaign": rolling_campaigns[0] if rolling_campaigns else None,
            "active_values_readiness": active_values_ready,
            "registry_summary": {
                status: sum(1 for row in registry_rows if row.get("status") == status)
                for status in ("research", "candidate", "production", "archived")
            },
            "truth_sources": {
                "jobs": "runtime/model/jobs.sqlite",
                "research_steps": "runtime/model/research_steps/current.jsonl",
                "orchestrator_events": "runtime/model/orchestrator_events/current.jsonl",
                "orchestrator_traces": "runtime/model/orchestrator_traces/current.jsonl",
                "mcp_traces": "runtime/model/mcp_traces/current.jsonl",
                "rolling_campaigns": "runtime/model/rolling/*/campaign.json",
            },
        }
        if compact:
            def compact_job(row: dict[str, Any] | None) -> dict[str, Any] | None:
                if not row:
                    return None
                payload = dict(row.get("payload") or {})
                return {
                    key: row.get(key)
                    for key in ("job_id", "status", "stage", "mode", "current_round_group_id", "created_at", "updated_at", "heartbeat_at")
                } | {
                    "payload": {
                        key: payload.get(key)
                        for key in ("session_id", "feature_set_id", "evaluation_mode", "worker_pid", "log_path", "cancel_requested", "stop_decision", "stop_next")
                        if payload.get(key) not in (None, "")
                    }
                }

            def compact_session(row: dict[str, Any] | None) -> dict[str, Any] | None:
                if not row:
                    return None
                return {
                    key: row.get(key)
                    for key in ("session_id", "status", "mode", "feature_set_id", "n_rounds_requested", "n_rounds_completed", "active_job_id", "current_stage", "round_group_ids", "model_run_ids", "updated_at")
                }

            compact_active_job = compact_job(active_job)
            compact_latest_job = compact_job(latest_job)
            compact_active_session = compact_session(active_session)
            compact_latest_session = compact_session(latest_session)
            outputs["orchestrator"] = {
                "active_session": compact_active_session,
                "active_job": compact_active_job,
                "latest_session": compact_latest_session,
                "latest_job": compact_latest_job,
                "current_context_summary": current_context,
            }
            outputs["active_session"] = compact_active_session
            outputs["latest_session"] = compact_latest_session
            outputs["latest_job"] = compact_latest_job
            outputs["sessions"] = [compact_session(row) for row in visible_sessions[:3]]
            outputs["rounds"] = []
            outputs["recent_rounds"] = []
            outputs["recent_seed_runs"] = recent_seed_runs[:3]
            outputs["latest_research_steps"] = outputs["latest_research_steps"][:5]
            projection = outputs.get("gui_projection") or {}
            candidate_rounds = projection.get("candidate_rounds") or {}
            research_progress = projection.get("research_progress") or {}
            # The GUI fetches the journal and run catalog separately.  Keep the
            # current cockpit facts here, but do not duplicate full timelines
            # and round histories in every compact status response.
            outputs["gui_projection"] = {
                key: projection.get(key)
                for key in (
                    "trust_state",
                    "process_progress",
                    "stop_state",
                    "research_subject",
                    "quality_gate_summary",
                    "latest_judgment",
                    "llm_review_signal",
                    "diagnostics",
                )
                if projection.get(key) not in (None, {}, [])
            }
            outputs["gui_projection"]["candidate_rounds"] = {
                "flow_round_no": candidate_rounds.get("flow_round_no"),
                "latest_completed_round_no": candidate_rounds.get("latest_completed_round_no"),
                "current_candidate_round": candidate_rounds.get("current_candidate_round") or {},
                "comparison_rows": [],
            }
            outputs["gui_projection"]["research_progress"] = {
                "latest": research_progress.get("latest") or {},
                "timeline": [],
                "llm_review_signal": research_progress.get("llm_review_signal") or {},
            }
            outputs["active_values_readiness"] = {
                key: value
                for key, value in active_values_ready.items()
                if key not in {"summary", "progress"}
            }
        return ok_result(outputs=outputs)
    except Exception as exc:
        return err_result(str(exc))


def model_feature_sets(limit: int | None = None, compact: bool = False) -> ServiceResult:
    try:
        return ok_result(outputs=feature_set_catalog_summary(limit=limit or 30, compact=compact))
    except Exception as exc:
        return err_result(str(exc))


def model_runs(round_group_id: str | None = None, limit: int = 50) -> ServiceResult:
    try:
        state = ModelStateStore()
        outputs = {
            "rounds": state.list_rounds(limit=limit) if not round_group_id else [state.get_round(round_group_id)],
            "seed_runs": state.list_seed_runs(round_group_id=round_group_id, limit=limit),
        }
        return ok_result(outputs=outputs)
    except Exception as exc:
        return err_result(str(exc))


def model_registry(
    status: str = "library",
    include_archived: bool = False,
    limit: int | None = None,
    compact: bool = False,
) -> ServiceResult:
    try:
        if status in {"library", "active", "visible"}:
            statuses = ["production", "candidate", "research"] + (["archived"] if include_archived else [])
            rows = []
            for item_status in statuses:
                rows.extend(_model_registry_rows(item_status))
        else:
            rows = _model_registry_rows("all" if status == "all" else status)
        rows = _model_formal_registry_rows(rows)
        rows = rows[:limit] if limit else rows
        if compact:
            compact_rows = []
            for row in rows:
                item = dict(row)
                metadata = dict(item.get("metadata") or {})
                # Full validation evidence remains available from the default
                # API and model detail routes.  It accounts for most of the
                # multi-megabyte registry payload but is not rendered in the
                # list/workbench views.
                metadata.pop("validation", None)
                item["metadata"] = metadata
                compact_rows.append(item)
            rows = compact_rows
            return ok_result(outputs={
                "items": rows,
                "count": len(rows),
                "statuses": ["research", "candidate", "production", "archived"],
                "compact": True,
            })
        return ok_result(outputs={"items": rows, "models": rows, "count": len(rows), "statuses": ["research", "candidate", "production", "archived"]})
    except Exception as exc:
        return err_result(str(exc))


def model_production() -> ServiceResult:
    try:
        rows = _model_registry_rows("production")
        active_id = ""
        try:
            active_id = str(json.loads(MODEL_ACTIVE_PRODUCTION.read_text(encoding="utf-8")).get("model_id") or "")
        except (OSError, ValueError, TypeError):
            pass
        active = next((row for row in rows if row.get("model_id") == active_id), rows[0] if rows and not active_id else {})
        return ok_result(outputs={"items": rows, "production_models": rows, "production_model": active, "active_model_id": active.get("model_id") if active else "", "count": len(rows), "multiple_production_allowed": True, "single_active_pointer": True})
    except Exception as exc:
        return err_result(str(exc))


def model_production_status() -> ServiceResult:
    """Compatibility production-status view for downstream prediction/trading.

    model supports multiple production models, but legacy downstream checks
    expect a single ``production_model`` plus ``status=ready``. The first
    production row is the default selected production model; the full list stays
    available under ``production_models``.
    """

    try:
        rows = _model_registry_rows("production")
        active_id = ""
        try:
            active_id = str(json.loads(MODEL_ACTIVE_PRODUCTION.read_text(encoding="utf-8")).get("model_id") or "")
        except (OSError, ValueError, TypeError):
            pass
        selected = next((row for row in rows if row.get("model_id") == active_id), rows[0] if rows and not active_id else {})
        return ok_result(
            outputs={
                "status": "ready" if selected else "missing",
                "production_model": selected,
                "production_models": rows,
                "items": rows,
                "count": len(rows),
                "multiple_production_allowed": True,
                "single_active_pointer": True,
                "model_system_version": MODEL_SYSTEM_VERSION,
                "source": "model",
            }
        )
    except Exception as exc:
        return err_result(str(exc))


def model_preflight_status(feature_set_id: str | None = None) -> ServiceResult:
    try:
        return ok_result(outputs=model_preflight(feature_set_id=feature_set_id))
    except Exception as exc:
        return err_result(str(exc))


def _model_row_metrics(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    metadata = _row_metadata(row)
    metrics = metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {}
    out = dict(metrics)
    for key in [
        "annualized_ret",
        "excess_annualized_ret_with_cost",
        "excess_information_ratio_with_cost",
        "strategy_annualized_ret",
        "strategy_sharpe",
        "max_drawdown",
        "rank_ic",
        "rank_icir",
    ]:
        if row.get(key) is not None:
            out[key] = row.get(key)
        elif metadata.get(key) is not None:
            out[key] = metadata.get(key)
    return out


def _model_validation_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    metadata = _row_metadata(row)
    validation = metadata.get("validation") if isinstance(metadata.get("validation"), dict) else {}
    out = dict(validation)
    for path in _model_manifest_candidates(row):
        audit_path = path.parent / "validation_audit.json"
        audit = _read_first_json([audit_path])
        if audit:
            out = {**audit, **out}
            out.setdefault("artifact_path", str(audit_path))
            break
    out.setdefault("status", metadata.get("validation_status") or "unknown")
    out.setdefault("validation_rule_version", metadata.get("validation_rule_version"))
    out.setdefault("hard_blocks", metadata.get("validation_hard_blocks") or [])
    out.setdefault("warnings", metadata.get("validation_warnings") or [])
    checks = out.get("checks") if isinstance(out.get("checks"), dict) else {}
    tradability = checks.get("tradability_exposure") if isinstance(checks.get("tradability_exposure"), dict) else {}
    style = checks.get("model_style_exposure") if isinstance(checks.get("model_style_exposure"), dict) else {}
    prediction = checks.get("prediction_artifact") if isinstance(checks.get("prediction_artifact"), dict) else {}
    if tradability:
        formal_prediction = tradability.get("prediction") if isinstance(tradability.get("prediction"), dict) else {}
        if not formal_prediction:
            summary = tradability.get("summary") if isinstance(tradability.get("summary"), dict) else {}
            pred_summary = prediction.get("summary") if isinstance(prediction.get("summary"), dict) else {}
            row_count = _safe_float(pred_summary.get("row_count"))
            st_rows = _safe_float(summary.get("st_like_prediction_rows"))
            st_ratio = (st_rows / row_count) if row_count and st_rows is not None else None
            formal_prediction = {
                "topk_avg_st_like_ratio": st_ratio,
                "top50_avg_st_like_ratio": st_ratio,
                "st_like_prediction_rows": st_rows,
                "row_count": row_count,
                "unique_instruments": summary.get("unique_instruments"),
            }
        tradability = {**tradability, "prediction": formal_prediction}
        out["tradability_exposure"] = tradability
    if style:
        out["model_style_exposure"] = style
    return _jsonable(out)


def _legacy_session_from_job(
    active_job: dict[str, Any] | None,
    *,
    state: ModelStateStore,
    rounds: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not active_job:
        return None
    payload = active_job.get("payload") if isinstance(active_job.get("payload"), dict) else {}
    job_id = str(active_job.get("job_id") or "")
    if not job_id:
        return None
    round_ids: list[str] = []
    for key in ("round_group_ids", "completed_rounds", "session_rounds"):
        value = payload.get(key)
        if isinstance(value, list):
            round_ids.extend(str(item) for item in value if item)
    if active_job.get("current_round_group_id"):
        round_ids.append(str(active_job.get("current_round_group_id")))
    if payload.get("round_group_id"):
        round_ids.append(str(payload.get("round_group_id")))
    round_ids = list(dict.fromkeys(round_ids))
    available_rounds = rounds if rounds is not None else state.list_rounds(limit=100)
    if not round_ids and payload.get("feature_set_id"):
        feature_set_id = str(payload.get("feature_set_id"))
        round_ids = [
            str(row.get("round_group_id"))
            for row in available_rounds
            if row.get("feature_set_id") == feature_set_id
        ][: int(payload.get("n_rounds") or 5)]
    seed_runs: list[dict[str, Any]] = []
    for round_id in round_ids:
        seed_runs.extend(state.list_seed_runs(round_group_id=round_id))
    current_blocker = {}
    if active_job.get("status") in {"failed", "cancelled", "interrupted"}:
        current_blocker = {
            "code": payload.get("blocker_code") or active_job.get("status"),
            "stage": active_job.get("stage"),
            "human_message": payload.get("err") or payload.get("error") or active_job.get("status"),
            "repair_action": payload.get("repair_action") or "",
            "resume_from": active_job.get("stage"),
        }
    completed = len([row for row in available_rounds if row.get("round_group_id") in set(round_ids) and row.get("status") == "completed"])
    return {
        "session_id": f"legacy_job:{job_id}",
        "status": active_job.get("status"),
        "mode": active_job.get("mode") or "legacy",
        "feature_set_id": payload.get("feature_set_id") or next((row.get("feature_set_id") for row in available_rounds if row.get("round_group_id") in set(round_ids)), ""),
        "n_rounds_requested": int(payload.get("n_rounds") or payload.get("n_rounds_requested") or len(round_ids) or 0),
        "n_rounds_completed": completed,
        "active_job_id": job_id,
        "parent_job_id": payload.get("parent_job_id") or "",
        "current_stage": active_job.get("stage"),
        "current_blocker": current_blocker,
        "round_group_ids": round_ids,
        "model_run_ids": [str(row.get("model_run_id")) for row in seed_runs if row.get("model_run_id")],
        "blocker_history": [current_blocker] if current_blocker else [],
        "payload": {"legacy_session_view": True, **payload},
        "created_at": active_job.get("created_at"),
        "updated_at": active_job.get("updated_at"),
        "legacy_session_view": True,
    }


def _status_source_map(
    *,
    jobs: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    rounds: list[dict[str, Any]],
    seed_runs: list[dict[str, Any]],
    active_session: dict[str, Any] | None,
    active_job: dict[str, Any] | None,
    research_step_count: int,
    events_count: int,
    orch_trace_count: int,
    mcp_trace_count: int,
    context_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    session_id = str((active_session or {}).get("session_id") or "")
    job_id = str((active_job or {}).get("job_id") or "")
    legacy_session_count = sum(1 for row in sessions if row.get("legacy_session_view"))
    sqlite_session_count = max(0, len(sessions) - legacy_session_count)
    return [
        {
            "key": "state_store",
            "path": "runtime/model/jobs.sqlite",
            "owner": "domain.model.state_store.ModelStateStore",
            "writes": "jobs, sessions, rounds, seed_runs",
            "gui_role": "当前 session/job、stage、round/seed 表的主状态源",
            "record_count": {
                "jobs": len(jobs),
                "sqlite_sessions": sqlite_session_count,
                "visible_sessions": len(sessions),
                "legacy_sessions": legacy_session_count,
                "rounds": len(rounds),
                "seed_runs": len(seed_runs),
            },
            "filter": {"session_id": session_id, "job_id": job_id},
            "truth_level": "primary_live_state",
            "legacy_view": bool((active_session or {}).get("legacy_session_view")),
        },
        {
            "key": "research_steps",
            "path": "runtime/model/research_steps/current.jsonl",
            "owner": "domain.model.orchestrator._research_step / service research_step",
            "writes": "operator-facing stage summaries, decisions, next actions",
            "gui_role": "研究现场时间线和中文摘要",
            "record_count": research_step_count,
            "filter": {"session_id": session_id, "job_id": job_id},
            "truth_level": "human_readable_journal",
        },
        {
            "key": "orchestrator_events",
            "path": "runtime/model/orchestrator_events/current.jsonl",
            "owner": "domain.model.orchestrator._event",
            "writes": "stage_start/stage_complete/failed/complete events",
            "gui_role": "流程事件、stage flow、实时进度",
            "record_count": events_count,
            "filter": {"session_id": session_id, "job_id": job_id},
            "truth_level": "event_log",
        },
        {
            "key": "orchestrator_traces",
            "path": "runtime/model/orchestrator_traces/current.jsonl",
            "owner": "domain.model.context.record_orch_trace",
            "writes": "DeepSeek request/result compact context and output contract",
            "gui_role": "ORCH Trace / Context、prompt 和 LLM 输出",
            "record_count": orch_trace_count,
            "filter": {"session_id": session_id, "job_id": job_id},
            "truth_level": "llm_trace",
        },
        {
            "key": "mcp_traces",
            "path": "runtime/model/mcp_traces/current.jsonl",
            "owner": "domain.model.context.record_mcp_context",
            "writes": "MCP context snapshots and operator submissions",
            "gui_role": "MCP 模式上下文、人工提交证据",
            "record_count": mcp_trace_count,
            "filter": {"session_id": session_id, "job_id": job_id},
            "truth_level": "mcp_trace",
        },
        {
            "key": "context_snapshots",
            "path": "runtime/model/context_snapshots/<context_id>.json",
            "owner": "domain.model.context.write_context_snapshot",
            "writes": "full context pack payloads",
            "gui_role": "当前 context summary，完整 payload 详情",
            "record_count": 0 if context_summary.get("generated_preview") else 1,
            "filter": {"context_id": context_summary.get("context_id")},
            "truth_level": "context_payload",
            "generated_preview": bool(context_summary.get("generated_preview")),
        },
        {
            "key": "run_artifacts",
            "path": "runtime/model/runs/<model_run_id>/",
            "owner": "domain.model.qlib_runner / qlib_direct / validation",
            "writes": "manifest, metrics, ret.pkl, pred.pkl, label.pkl, params.pkl, validation_audit",
            "gui_role": "回测曲线、每日持仓、validation、seed artifact",
            "record_count": len(seed_runs),
            "filter": {"model_run_ids": (active_session or {}).get("model_run_ids") or []},
            "truth_level": "artifact_evidence",
        },
        {
            "key": "model_registry",
            "path": "data/model/model_registry.db",
            "owner": "storage.model_registry.ModelRegistry",
            "writes": "research / candidate / production / archived assets and metadata",
            "gui_role": "模型库、生产模型选择、回测下拉",
            "record_count": {
                "research": len(_model_registry_rows("research")),
                "candidate": len(_model_registry_rows("candidate")),
                "production": len(_model_registry_rows("production")),
                "archived": len(_model_registry_rows("archived")),
            },
            "filter": {"model_system_version": MODEL_SYSTEM_VERSION},
            "truth_level": "asset_registry",
        },
        {
            "key": "active_values_feature_sets",
            "path": "data/factors/active_adopted_factor_values.* + data/model/features/feature_sets/*",
            "owner": "factor import/active values builder + domain.model.feature_sets",
            "writes": "active values readiness and immutable feature set manifests",
            "gui_role": "训练前数据、feature set catalog、LABEL0 合同",
            "record_count": "catalog",
            "filter": {"feature_set_id": (active_session or {}).get("feature_set_id")},
            "truth_level": "data_preflight",
        },
    ]


def model_backtest(
    model_id: str | None = None,
    model_run_id: str | None = None,
    rolling_campaign_id: str | None = None,
    rolling_seed: int | None = None,
    selector: str = "latest",
    include_daily: bool = False,
    max_daily_holdings: int = 30,
) -> ServiceResult:
    try:
        raw_rows = _model_registry_rows("all")
        rows = _model_formal_registry_rows(raw_rows)
        eligible = [row for row in rows if row.get("status") in {"research", "candidate", "production"}]
        research_or_candidate = [row for row in eligible if row.get("status") in {"research", "candidate"}]
        recent_models = eligible[:30]
        rolling_campaigns = _model_rolling_campaigns(limit=30)
        selected = None
        selected_campaign = None
        resolved_selector = "none"
        if rolling_campaign_id:
            selected_campaign = next((row for row in rolling_campaigns if row.get("campaign_id") == rolling_campaign_id), None)
        elif selector == "rolling" and rolling_campaigns:
            selected_campaign = rolling_campaigns[0]
        if selected_campaign:
            campaign_seeds = selected_campaign.get("seeds") or []
            # Public/formal backtests always use the pre-declared official Seed42.
            # ``rolling_seed`` remains accepted temporarily for API compatibility,
            # but cannot change the displayed or ranked model result.
            requested_rolling_seed = 42
            selected_rolling_seed = next(
                (row for row in campaign_seeds if int(row.get("seed") or 0) == requested_rolling_seed),
                None,
            )
            selected_rolling_seed = selected_rolling_seed or next(
                (row for row in campaign_seeds if int(row.get("seed") or 0) == 42),
                None,
            )
            selected_rolling_seed = selected_rolling_seed or (campaign_seeds[0] if campaign_seeds else {})
            resolved_rolling_seed = int(selected_rolling_seed.get("seed") or requested_rolling_seed)
            seed_artifacts = selected_rolling_seed.get("artifacts") if isinstance(selected_rolling_seed.get("artifacts"), dict) else {}
            rolling_seed_root = Path(str(seed_artifacts.get("result"))).parent if seed_artifacts.get("result") else None
            rolling_positions = rolling_seed_root / "stitched_portfolio_analysis" / "positions_normal_1day.pkl" if rolling_seed_root else None
            rolling_indicator = rolling_seed_root / "stitched_portfolio_analysis" / "indicator_normal_1day.pkl" if rolling_seed_root else None
            rolling_portfolio_artifacts = {
                key: str(path)
                for key, path in {
                    "positions_pkl": rolling_positions,
                    "indicator_pkl": rolling_indicator,
                    "portfolio_analysis_dir": (rolling_seed_root / "stitched_portfolio_analysis") if rolling_seed_root else None,
                }.items()
                if path is not None and path.exists()
            }
            rolling_metrics = selected_rolling_seed.get("rolling_metrics") if isinstance(selected_rolling_seed.get("rolling_metrics"), dict) else {}
            preliminary = selected_campaign.get("preliminary") if isinstance(selected_campaign.get("preliminary"), dict) else {}
            final = selected_campaign.get("final") if isinstance(selected_campaign.get("final"), dict) else {}
            portfolio = selected_campaign.get("portfolio") if isinstance(selected_campaign.get("portfolio"), dict) else {}
            selected = {
                "model_id": f"rolling:{selected_campaign.get('campaign_id')}",
                "model_run_id": selected_campaign.get("campaign_id"),
                "display_name": selected_campaign.get("display_name"),
                "display_subtitle": selected_campaign.get("display_subtitle"),
                "display_naming_version": selected_campaign.get("display_naming_version"),
                "status": selected_campaign.get("status") or "research",
                "role": "rolling_campaign",
                "seed": resolved_rolling_seed,
                "run_dir": str(rolling_seed_root) if rolling_seed_root else None,
                "evaluation_mode": "production",
                "feature_set_id": selected_campaign.get("feature_set_id"),
                "factor_count": selected_rolling_seed.get("factor_count"),
                "rolling_score": final.get("rolling_score") if final.get("available") else preliminary.get("score"),
                "rolling_gates": final.get("gates") if final.get("available") else preliminary.get("gates"),
                **rolling_metrics,
                "metadata": {
                    "model_system_version": MODEL_SYSTEM_VERSION,
                    "evaluation_mode": "production",
                    "role": "rolling_campaign",
                    "rolling_campaign_id": selected_campaign.get("campaign_id"),
                    "rolling_seed": resolved_rolling_seed,
                    "rolling_score": final.get("rolling_score") if final.get("available") else preliminary.get("score"),
                    "rolling_gates": final.get("gates") if final.get("available") else preliminary.get("gates"),
                    "topk": portfolio.get("topk"),
                    "n_drop": portfolio.get("n_drop"),
                    "hold_thresh": portfolio.get("hold_thresh"),
                    "benchmark": portfolio.get("benchmark"),
                    "artifacts": {
                        "ret_pkl": seed_artifacts.get("stitched_return"),
                        "pred_pkl": seed_artifacts.get("stitched_prediction"),
                        "portfolio": rolling_portfolio_artifacts,
                    },
                },
            }
            resolved_selector = "selected_rolling_campaign" if rolling_campaign_id else "latest_rolling_campaign"
        for row in rows:
            if selected_campaign:
                break
            if model_id and row.get("model_id") == model_id:
                selected = row
                resolved_selector = "selected_model"
                break
            if model_run_id and row.get("model_run_id") == model_run_id:
                selected = row
                resolved_selector = "selected_model_run"
                break
        if selected is None and model_run_id:
            requested_row = next((row for row in raw_rows if row.get("model_run_id") == model_run_id), None)
            requested_metadata = _row_metadata(requested_row) if requested_row else {}
            requested_round_id = str((requested_row or {}).get("round_group_id") or requested_metadata.get("round_group_id") or "")
            if requested_round_id:
                selected = next(
                    (
                        row for row in rows
                        if str(row.get("round_group_id") or _row_metadata(row).get("round_group_id") or "") == requested_round_id
                    ),
                    None,
                )
                if selected:
                    resolved_selector = "selected_research_round_official_seed42"
        if selected is None and eligible:
            production = [row for row in eligible if row.get("status") == "production"]
            if selector == "production" and production:
                selected = production[0]
                resolved_selector = "production"
            elif selector == "best" and research_or_candidate:
                selected = max(research_or_candidate, key=_model_evaluation_score)
                resolved_selector = "best_research_or_candidate"
            else:
                selected = eligible[0]
                resolved_selector = "latest_registry"
        curve_payload = {"available": False, "reason": "no eligible model found for backtest curve", "curve": [], "daily": [] if include_daily else None}
        artifacts: dict[str, Any] = {}
        daily_breakdown: dict[str, Any] = {"available": False, "reason": "selected model or pred.pkl unavailable", "items": [], "by_date": {}}
        stock_contribution: dict[str, Any] = {"available": False, "reason": "daily breakdown unavailable"}
        if selected:
            portfolio_artifacts = _model_portfolio_artifacts(selected)
            if portfolio_artifacts:
                artifacts["portfolio_artifacts"] = portfolio_artifacts
            ret_paths = _model_ret_candidates(selected)
            if ret_paths:
                curve_payload = _model_curve_from_ret(ret_paths[0], include_daily=include_daily)
                artifacts["ret_pkl"] = str(ret_paths[0])
            else:
                curve_payload = {
                    "available": False,
                    "reason": "no Qlib portfolio return artifact found",
                    "curve": [],
                    "daily": [] if include_daily else None,
                }
            pred_paths = _model_pred_candidates(selected)
            if pred_paths:
                artifacts["pred_pkl"] = str(pred_paths[0])
                position_paths = _model_portfolio_positions_candidates(selected)
                if position_paths:
                    artifacts["positions_pkl"] = str(position_paths[0])
                if include_daily:
                    daily_breakdown = _daily_breakdown_from_pred(
                        pred_paths[0],
                        daily_rows=curve_payload.get("daily") or [],
                        portfolio=_portfolio_contract_from_manifest(selected),
                        max_daily_holdings=max_daily_holdings,
                        positions_path=position_paths[0] if position_paths else None,
                    )
                    stock_contribution = _stock_contribution_from_breakdown(daily_breakdown)
            elif include_daily:
                daily_breakdown = {"available": False, "reason": "no prediction artifact found", "items": [], "by_date": {}}
        row_metrics = _model_row_metrics(selected)
        curve_metrics = curve_payload.get("metrics") if isinstance(curve_payload.get("metrics"), dict) else {}
        row_metrics = {**curve_metrics, **row_metrics}
        for curve_metric_key in [
            "strategy_sharpe",
            "gross_strategy_sharpe",
            "nav_max_drawdown",
            "gross_nav_max_drawdown",
            "benchmark_annualized_ret",
            "net_cumulative_return",
            "gross_cumulative_return",
            "benchmark_cumulative_return",
            "relative_cumulative_return",
            "net_value_gap",
            "cost_drag_cumulative",
            "qlib_annualization_factor",
            "curve_return_basis",
            "relative_return_basis",
        ]:
            if curve_metrics.get(curve_metric_key) is not None:
                row_metrics[curve_metric_key] = curve_metrics[curve_metric_key]
        validation_payload = _model_validation_payload(selected)
        return ok_result(
            outputs={
                "selector": selector,
                "selection": {
                    "requested_selector": selector,
                    "selector": resolved_selector,
                    "model_id": selected.get("model_id") if selected else None,
                    "model_run_id": selected.get("model_run_id") if selected else None,
                    "rolling_seed": selected.get("seed") if selected_campaign and selected else None,
                },
                "selected_model": selected,
                "model": selected,
                "metrics": row_metrics,
                "recent_models": recent_models,
                "rolling_campaigns": rolling_campaigns,
                "rolling_campaign": selected_campaign,
                "curve_available": bool(curve_payload.get("available")),
                "curve": curve_payload.get("curve", []),
                "daily": curve_payload.get("daily") if include_daily else None,
                "daily_breakdown": daily_breakdown if include_daily else {"available": False, "reason": "include_daily=false", "items": [], "by_date": {}},
                "stock_contribution": stock_contribution,
                "point_count": curve_payload.get("point_count", 0),
                "raw_point_count": curve_payload.get("raw_point_count", 0),
                "period": curve_payload.get("period", {"start": None, "end": None}),
                "curve_diagnostics": {key: value for key, value in curve_payload.items() if key not in {"curve", "daily"}},
                "diagnostics": {key: value for key, value in curve_payload.items() if key not in {"curve", "daily"}},
                "validation": validation_payload,
                "exposure": {
                    "available": bool(validation_payload.get("tradability_exposure") or validation_payload.get("model_style_exposure")),
                    "tradability_exposure": validation_payload.get("tradability_exposure") or {},
                    "model_style_exposure": validation_payload.get("model_style_exposure") or {},
                    "source": validation_payload.get("artifact_path") or "registry_metadata",
                    "note": "exposures are read from model validation_audit; ST uses PIT status data and style uses Qlib market/fundamental percentiles when available.",
                },
                "production_refit": (_row_metadata(selected).get("production_refit") if selected else {}) or {},
                "portfolio_contract": _portfolio_contract_from_manifest(selected) if selected else {},
                "feature_set_id": (selected or {}).get("feature_set_id") if selected else None,
                "note": "model exposes registry/artifact backtest metadata; curve replay is read from run artifacts when present.",
            },
            artifacts=artifacts,
        )
    except Exception as exc:
        return err_result(str(exc))


def model_research_current() -> ServiceResult:
    try:
        rows = read_jsonl(MODEL_RESEARCH_STEPS, limit=1)
        all_rows = read_jsonl(MODEL_RESEARCH_STEPS, limit=80)
        events = read_jsonl(MODEL_ORCHESTRATOR_EVENTS, limit=120, include_payload=False)
        current = _research_record_contract_projection(rows[-1] if rows else None)
        review = _research_record_contract_projection(_latest_llm_review_signal(all_rows, events)) or {}
        if review.get("historical"):
            review["active"] = False
        return ok_result(
            outputs={
                "current": current,
                "current_contract_available": bool(current and current.get("current_contract")),
                "historical_record_available": bool(current and current.get("historical")),
                "llm_review_signal": review,
            }
        )
    except Exception as exc:
        return err_result(str(exc))


def model_research_journal(limit: int = 80) -> ServiceResult:
    try:
        rows = read_jsonl(MODEL_RESEARCH_STEPS, limit=limit)
        events = read_jsonl(MODEL_ORCHESTRATOR_EVENTS, limit=max(int(limit), 120), include_payload=False)
        journal = [_research_record_contract_projection(row) for row in reversed(rows)]
        review = _research_record_contract_projection(_latest_llm_review_signal(rows, events)) or {}
        if review.get("historical"):
            review["active"] = False
        return ok_result(
            outputs={
                "journal": journal,
                "latest_first": True,
                "llm_review_signal": review,
            }
        )
    except Exception as exc:
        return err_result(str(exc))


def model_orchestrator_status() -> ServiceResult:
    try:
        state = ModelStateStore()
        events_tail = list(reversed(read_jsonl(MODEL_ORCHESTRATOR_EVENTS, limit=50, include_payload=False)))
        research_steps_tail = read_jsonl(MODEL_RESEARCH_STEPS, limit=80)
        orch_traces_tail = read_jsonl(MODEL_ORCHESTRATOR_TRACES, limit=100, include_payload=False)
        mcp_traces_tail = read_jsonl(MODEL_MCP_TRACES, limit=100, include_payload=False)
        jobs = state.list_jobs(limit=20)
        sessions = state.list_sessions(limit=20)
        active_job = next((job for job in jobs if _model_record_is_active(job)), None)
        latest_job = jobs[0] if jobs else None
        active_session = next((row for row in sessions if _model_record_is_active(row)), None)
        latest_session = sessions[0] if sessions else None
        rounds = [_round_with_dataset_segments(row) for row in state.list_rounds(limit=100)]
        legacy_session = _legacy_session_from_job(active_job or latest_job, state=state, rounds=rounds) if not sessions else None
        if active_job and active_session is None:
            active_session = legacy_session
        latest_session = latest_session or legacy_session
        visible_sessions = sessions + ([legacy_session] if legacy_session else [])
        current_context = _current_context_summary(active_job, state)
        session_round_ids = set((active_session or {}).get("round_group_ids") or [])
        session_rounds = [row for row in rounds if row.get("round_group_id") in session_round_ids]
        llm_review_signal = _latest_llm_review_signal(
            research_steps_tail,
            events_tail,
            active_session=active_session,
            active_job=active_job,
        )
        return ok_result(
            outputs={
                "active_session": active_session,
                "active_job": active_job,
                "latest_session": latest_session,
                "latest_job": latest_job,
                "sessions": visible_sessions,
                "jobs": jobs,
                "session_rounds": session_rounds,
                "session_blockers": (active_session or {}).get("blocker_history") or ([] if not (active_session or {}).get("current_blocker") else [(active_session or {}).get("current_blocker")]),
                "events_tail": events_tail,
                "llm_review_signal": llm_review_signal,
                "current_context_summary": current_context,
                "status_source_map": _status_source_map(
                    jobs=jobs,
                    sessions=visible_sessions,
                    rounds=rounds,
                    seed_runs=state.list_seed_runs(limit=30),
                    active_session=active_session,
                    active_job=active_job,
                    research_step_count=len(research_steps_tail),
                    events_count=len(events_tail),
                    orch_trace_count=len(orch_traces_tail),
                    mcp_trace_count=len(mcp_traces_tail),
                    context_summary=current_context,
                ),
                "truth_sources": {
                    "jobs": "runtime/model/jobs.sqlite",
                    "orchestrator_events": "runtime/model/orchestrator_events/current.jsonl",
                    "orchestrator_traces": "runtime/model/orchestrator_traces/current.jsonl",
                    "research_steps": "runtime/model/research_steps/current.jsonl",
                },
            }
        )
    except Exception as exc:
        return err_result(str(exc))


def _matches_job(row: dict[str, Any], job_id: str | None = None, run_id: str | None = None, session_id: str | None = None) -> bool:
    selected_session = str(session_id or "").strip()
    if selected_session:
        if selected_session.startswith("legacy_job:"):
            legacy_job_id = selected_session.split(":", 1)[1]
            if str(row.get("job_id") or row.get("run_id") or "") != legacy_job_id:
                return False
        elif str(row.get("session_id") or "") != selected_session:
            return False
    selected = (job_id or run_id or "").strip()
    if not selected:
        return True
    return str(row.get("job_id") or row.get("run_id") or "") == selected


def _filter_rows_for_job(rows: list[dict[str, Any]], job_id: str | None = None, run_id: str | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
    return [row for row in rows if _matches_job(row, job_id=job_id, run_id=run_id, session_id=session_id)]


def _current_context_summary(active_job: dict[str, Any] | None, state: ModelStateStore | None = None) -> dict[str, Any]:
    state = state or ModelStateStore()
    selected_job_id = str((active_job or {}).get("job_id") or "")
    selected_round_group_id = (active_job or {}).get("current_round_group_id") or None
    context_pack = None
    trace_context_id = ""
    context_source = "none"
    for path, source in ((MODEL_ORCHESTRATOR_TRACES, "orchestrator_trace"), (MODEL_MCP_TRACES, "mcp_trace")):
        # The most recent trace normally owns the current context. Read a tiny
        # tail first so GUI status does not deserialize hundreds of historical
        # multi-megabyte context packs. If an active job is not represented in
        # that tail, fall back to the previous 500-record audit window.
        fast_limit = 30 if selected_job_id else 1
        search_limits = (fast_limit, 500)
        for trace_limit in search_limits:
            for row in reversed(read_jsonl(path, limit=trace_limit, include_payload=True)):
                if selected_job_id and str(row.get("job_id") or row.get("run_id") or "") != selected_job_id:
                    continue
                candidate = row.get("context_pack") if isinstance(row.get("context_pack"), dict) else None
                if candidate:
                    context_pack = candidate
                    trace_context_id = str(row.get("context_id") or "")
                    context_source = source
                    break
            if context_pack:
                break
        if context_pack:
            break
    generated_preview = False
    if context_pack is None:
        context_pack = build_context_pack(
            stage=str((active_job or {}).get("stage") or "context_review"),
            round_group_id=selected_round_group_id,
            state=state,
        )
        generated_preview = True
    feature_catalog = context_pack.get("feature_set_catalog") or {}
    seed_runs = context_pack.get("seed_runs") or []
    research_evidence = context_pack.get("research_evidence") if isinstance(context_pack.get("research_evidence"), dict) else {}
    return {
        "job_id": (active_job or {}).get("job_id"),
        "job_status": (active_job or {}).get("status"),
        "stage": context_pack.get("stage"),
        "context_id": None if generated_preview else (trace_context_id or context_pack.get("context_id")),
        "context_source": "generated_preview" if generated_preview else context_source,
        "generated_preview": generated_preview,
        "current_round_group_id": (active_job or {}).get("current_round_group_id"),
        "feature_set_count": feature_catalog.get("count"),
        "selected_feature_set_id": (context_pack.get("lineage_context") or {}).get("selected_feature_set_id"),
        "seed_run_count": len(seed_runs),
        "admission_policy": context_pack.get("admission_policy"),
        "blocked_actions": context_pack.get("blocked_actions"),
        "truth_sources": context_pack.get("tool_evidence"),
        "planner_context_summary": {
            "recent_round_count": len(research_evidence.get("recent_rounds") or []),
            "parameter_ledger_count": len(research_evidence.get("parameter_ledger") or []),
            "cross_feature_reference_count": len(research_evidence.get("cross_feature_references") or []),
            "correction_active": bool(context_pack.get("correction")),
        } if research_evidence else {},
    }


def model_current_context(job_id: str | None = None, run_id: str | None = None) -> ServiceResult:
    try:
        state = ModelStateStore()
        jobs = state.list_jobs(limit=50)
        selected_id = (job_id or run_id or "").strip()
        job = next((row for row in jobs if row.get("job_id") == selected_id), None) if selected_id else None
        if job is None:
            job = next((row for row in jobs if row.get("status") == "running"), jobs[0] if jobs else None)
        return ok_result(outputs={"current_context_summary": _current_context_summary(job, state), "active_job": job})
    except Exception as exc:
        return err_result(str(exc))


def model_orchestrator_events(limit: int = 80, include_payload: bool = False, job_id: str | None = None, run_id: str | None = None, session_id: str | None = None) -> ServiceResult:
    try:
        rows = read_jsonl(MODEL_ORCHESTRATOR_EVENTS, limit=max(int(limit) * 5, int(limit)), include_payload=include_payload)
        rows = _filter_rows_for_job(rows, job_id=job_id, run_id=run_id, session_id=session_id)
        return ok_result(outputs={"events": list(reversed(rows[-int(limit):])), "job_id": job_id or run_id or "", "session_id": session_id or ""})
    except Exception as exc:
        return err_result(str(exc))


def _mark_legacy_trace(row: dict[str, Any]) -> dict[str, Any]:
    parsed = row.get("parsed_response") if isinstance(row.get("parsed_response"), dict) else {}
    contract = row.get("output_contract") if isinstance(row.get("output_contract"), dict) else {}
    markers = {
        str(parsed.get("planner_mode") or ""),
        str(parsed.get("llm_call_status") or ""),
        str(contract.get("planner_mode") or ""),
        str(contract.get("llm_call_status") or ""),
    }
    out = dict(row)
    legacy_reasons: list[str] = []
    if {"local_contract_planner", "context_recorded_not_called"} & markers:
        legacy_reasons.append("legacy_planner_marker")
    if row.get("event_type") == "llm_result" and row.get("stage") == "round_synthesis":
        if parsed.get("planner_mode") == "deterministic_platform_summary":
            required = {"round_metrics", "improvement_vs_reference", "improved_platform_best", "consecutive_no_improvement"}
        else:
            required = {
                "previous_parameters",
                "three_seed_results",
                "seed_dispersion",
                "score_summary",
                "gate_summary",
                "validation_summary",
                "next_parameter_change_rationale",
            }
        missing = sorted(required - set(parsed))
        if missing:
            legacy_reasons.append("legacy_round_synthesis_contract_missing:" + ",".join(missing))
    out["legacy_trace"] = bool(legacy_reasons)
    out["schema_status"] = "legacy" if legacy_reasons else out.get("schema_status", "current")
    if legacy_reasons:
        out["legacy_reasons"] = legacy_reasons
    return out


def model_orchestrator_traces(limit: int = 50, include_payload: bool = False, job_id: str | None = None, run_id: str | None = None, session_id: str | None = None) -> ServiceResult:
    try:
        rows = [_mark_legacy_trace(row) for row in read_jsonl(MODEL_ORCHESTRATOR_TRACES, limit=max(int(limit) * 5, int(limit)), include_payload=include_payload)]
        rows = _filter_rows_for_job(rows, job_id=job_id, run_id=run_id, session_id=session_id)
        return ok_result(outputs={"traces": list(reversed(rows[-int(limit):])), "job_id": job_id or run_id or "", "session_id": session_id or ""})
    except Exception as exc:
        return err_result(str(exc))


def model_mcp_traces(limit: int = 50, include_payload: bool = False, job_id: str | None = None, run_id: str | None = None, session_id: str | None = None) -> ServiceResult:
    try:
        rows = read_jsonl(MODEL_MCP_TRACES, limit=max(int(limit) * 5, int(limit)), include_payload=include_payload)
        rows = _filter_rows_for_job(rows, job_id=job_id, run_id=run_id, session_id=session_id)
        return ok_result(outputs={"traces": list(reversed(rows[-int(limit):])), "job_id": job_id or run_id or "", "session_id": session_id or ""})
    except Exception as exc:
        return err_result(str(exc))


def model_tool_context(
    stage: str = "context_review",
    round_group_id: str | None = None,
    feature_set_id: str | None = None,
    job_id: str | None = None,
    run_id: str | None = None,
) -> ServiceResult:
    try:
        context_pack = build_context_pack(stage=stage, round_group_id=round_group_id, selected_feature_set_id=feature_set_id)
        trace = record_mcp_context(
            stage,
            context_pack,
            expected_action="inspect_context",
            job_id=job_id or "",
            run_id=run_id or "",
            round_group_id=round_group_id or "",
        )
        return ok_result(outputs={"context_pack": context_pack, "trace": trace})
    except Exception as exc:
        return err_result(str(exc))


def model_tool_protocol() -> ServiceResult:
    return ok_result(
        outputs={
            "protocol": production_contract(),
            "prompts": {
                "mcp": {
                    "path": str(MCP_PROMPT_PATH),
                    "purpose": "Codex/MCP operating workflow prompt",
                    "content": model_mcp_prompt(),
                },
                "orch": {
                    "path": str(ORCH_PROMPT_PATH),
                    "purpose": "DeepSeek ORCH research-planning prompt",
                    "content": model_system_prompt(),
                },
            },
        }
    )


def model_tool_feature_snapshot(**kwargs: Any) -> ServiceResult:
    try:
        job_id = str(kwargs.pop("job_id", "") or kwargs.pop("run_id", "") or "")
        result = feature_snapshot(**kwargs)
        context_pack = build_context_pack(stage="feature_snapshot", selected_feature_set_id=kwargs.get("feature_set_id"))
        record_mcp_context(
            "feature_snapshot",
            context_pack,
            expected_action="feature_snapshot",
            submitted_payload=kwargs,
            validation_result=result,
            job_id=job_id,
        )
        return ok_result(inputs=kwargs, outputs=result)
    except Exception as exc:
        readiness: dict[str, Any] = {}
        try:
            readiness = active_values_readiness(
                factor_holding_period_days=int(kwargs.get("factor_holding_period_days") or 5)
            )
        except Exception:
            readiness = {}
        return err_result(str(exc), inputs=kwargs, outputs={"active_values_readiness": readiness})


def model_tool_session_start(feature_set_id: str | None = None, job_id: str | None = None, run_id: str | None = None) -> ServiceResult:
    try:
        state = ModelStateStore()
        resolved_job_id = job_id or run_id or f"model_mcp_session_{feature_set_id or 'context'}"
        job = state.upsert_job(resolved_job_id, status="queued", stage="context_review", mode="mcp", payload={"feature_set_id": feature_set_id})
        context_pack = build_context_pack(stage="context_review", selected_feature_set_id=feature_set_id, state=state)
        record_mcp_context("context_review", context_pack, expected_action="session_start", submitted_payload={"feature_set_id": feature_set_id}, validation_result={"ok": True}, job_id=resolved_job_id)
        return ok_result(outputs={"session": job})
    except Exception as exc:
        return err_result(str(exc))


def model_tool_submit_experiment(feature_set_id: str, experiment: dict[str, Any], job_id: str | None = None, run_id: str | None = None) -> ServiceResult:
    try:
        result = submit_experiment(feature_set_id=feature_set_id, experiment=experiment)
        round_group_id = (result.get("round_group") or {}).get("round_group_id") or ""
        context_pack = build_context_pack(stage="experiment_plan", round_group_id=round_group_id or None)
        record_mcp_context(
            "experiment_plan",
            context_pack,
            expected_action="submit_experiment",
            submitted_payload=experiment,
            validation_result=result.get("validation_result") or result,
            job_id=job_id or run_id or "",
            round_group_id=round_group_id,
        )
        return ok_result(inputs={"feature_set_id": feature_set_id, "experiment": experiment}, outputs=result) if result.get("ok") else err_result(result.get("err", "experiment_contract_failed"), inputs={"feature_set_id": feature_set_id}, outputs=result)
    except Exception as exc:
        return err_result(str(exc), inputs={"feature_set_id": feature_set_id})


def model_tool_run_round(round_group_id: str, execute_qlib: bool = False, job_id: str | None = None, run_id: str | None = None) -> ServiceResult:
    try:
        result = run_round(round_group_id=round_group_id, execute_qlib=execute_qlib)
        context_pack = build_context_pack(stage="train_backtest_seed42", round_group_id=round_group_id)
        record_mcp_context(
            "train_backtest_seed42",
            context_pack,
            expected_action="run_round",
            submitted_payload={"round_group_id": round_group_id, "execute_qlib": execute_qlib},
            validation_result={"ok": bool(result.get("ok")), "err": result.get("err"), "seed_count": len(result.get("seed_runs") or [])},
            job_id=job_id or run_id or "",
            round_group_id=round_group_id,
        )
        return ok_result(inputs={"round_group_id": round_group_id}, outputs=result) if result.get("ok") else err_result(result.get("err", "run_round_failed"), inputs={"round_group_id": round_group_id}, outputs=result)
    except Exception as exc:
        return err_result(str(exc), inputs={"round_group_id": round_group_id})


def model_tool_score_review(round_group_id: str, job_id: str | None = None, run_id: str | None = None) -> ServiceResult:
    try:
        result = score_round(round_group_id)
        context_pack = build_context_pack(stage="research_score", round_group_id=round_group_id)
        record_mcp_context(
            "research_score",
            context_pack,
            expected_action="score_review",
            submitted_payload={"round_group_id": round_group_id},
            validation_result={
                "ok": bool(result.get("ok")),
                "err": result.get("err"),
                "result_count": len(result.get("results") or []),
            },
            job_id=job_id or run_id or "",
            round_group_id=round_group_id,
        )
        return ok_result(inputs={"round_group_id": round_group_id}, outputs=result) if result.get("ok") else err_result(result.get("err", "score_review_failed"), outputs=result)
    except Exception as exc:
        return err_result(str(exc), inputs={"round_group_id": round_group_id})


def model_tool_confirm_research_round(
    round_group_id: str,
    execute_qlib: bool = False,
    write_registry: bool = True,
    job_id: str | None = None,
    run_id: str | None = None,
) -> ServiceResult:
    try:
        result = confirm_research_round(round_group_id, execute_qlib=execute_qlib, write_registry=write_registry)
        context_pack = build_context_pack(stage="research_confirmation", round_group_id=round_group_id)
        record_mcp_context(
            "research_confirmation",
            context_pack,
            expected_action="confirm_research_round",
            submitted_payload={"round_group_id": round_group_id, "execute_qlib": execute_qlib, "write_registry": write_registry},
            validation_result=result,
            job_id=job_id or run_id or "",
            round_group_id=round_group_id,
        )
        return ok_result(outputs=result) if result.get("ok") else err_result(result.get("err", "research_confirmation_failed"), outputs=result)
    except Exception as exc:
        return err_result(str(exc), inputs={"round_group_id": round_group_id})


def model_tool_start_production_rolling(
    source_round_group_id: str,
    write_registry: bool = True,
    campaign_id: str | None = None,
) -> ServiceResult:
    try:
        result = start_production_rolling(
            source_round_group_id,
            write_registry=write_registry,
            campaign_id=campaign_id,
        )
        return ok_result(inputs={"source_round_group_id": source_round_group_id}, outputs=result) if result.get("ok") else err_result(result.get("err", "production_rolling_failed"), outputs=result)
    except Exception as exc:
        return err_result(str(exc), inputs={"source_round_group_id": source_round_group_id})


def model_tool_forward_test(round_group_id: str, job_id: str | None = None, run_id: str | None = None) -> ServiceResult:
    del job_id, run_id
    return err_result("forward_test_removed_use_research_confirmation_or_production_rolling", inputs={"round_group_id": round_group_id})


def model_tool_sota_gate(round_group_id: str, job_id: str | None = None, run_id: str | None = None) -> ServiceResult:
    del job_id, run_id
    return err_result("sota_gate_removed_candidate_requires_production_rolling", inputs={"round_group_id": round_group_id})


def model_tool_round_synthesis(
    round_group_id: str,
    round_no: int = 1,
    write_registry: bool = False,
    job_id: str | None = None,
    run_id: str | None = None,
) -> ServiceResult:
    try:
        result = run_round_synthesis(
            round_group_id=round_group_id,
            round_no=round_no,
            job_id=job_id or run_id,
            write_registry=write_registry,
        )
        context_pack = build_context_pack(stage="round_synthesis", round_group_id=round_group_id)
        record_mcp_context(
            "round_synthesis",
            context_pack,
            expected_action="round_synthesis",
            submitted_payload={"round_group_id": round_group_id, "round_no": round_no, "write_registry": write_registry},
            validation_result={
                "ok": bool(result.get("ok")),
                "err": result.get("err"),
                "decision": ((result.get("round_synthesis") or {}).get("decision") if isinstance(result.get("round_synthesis"), dict) else None),
                "next": ((result.get("round_synthesis") or {}).get("next") if isinstance(result.get("round_synthesis"), dict) else None),
            },
            job_id=job_id or run_id or "",
            round_group_id=round_group_id,
        )
        return ok_result(
            inputs={"round_group_id": round_group_id, "round_no": round_no, "write_registry": write_registry},
            outputs=result,
        ) if result.get("ok") else err_result(result.get("err", "round_synthesis_failed"), inputs={"round_group_id": round_group_id}, outputs=result)
    except Exception as exc:
        return err_result(str(exc), inputs={"round_group_id": round_group_id})


def _stage_seq(stage: str) -> int:
    order = [
        "protocol_load",
        "context_review",
        "feature_snapshot",
        "experiment_plan",
        "train_backtest_seed42",
        "research_score",
        "research_confirmation",
        "rolling_preliminary",
        "rolling_confirmation",
        "rolling_score",
        "registry_write",
        "round_synthesis",
        "checkpoint_stop",
        "blocker",
    ]
    try:
        return order.index(stage) + 1
    except ValueError:
        return 0


def model_tool_research_step(
    stage: str,
    summary: str = "",
    decision: str = "",
    next: str = "",
    refs: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    round_group_id: str = "",
    model_run_id: str = "",
    feature_set_id: str = "",
) -> ServiceResult:
    try:
        payload = {
            "schema_version": "research_step_v2",
            "stage": stage,
            "stage_seq": _stage_seq(stage),
            "summary": summary,
            "decision": decision,
            "next": next,
            "stage_transition": {"next_stage": next, "reason": decision},
            "evidence_refs": refs or [],
            "refs": refs or [],
            "feature_set_id": feature_set_id,
            "round_group_id": round_group_id,
            "model_run_id": model_run_id,
            "extra": extra or {},
        }
        append_jsonl(MODEL_RESEARCH_STEPS, payload)
        return ok_result(inputs=payload, outputs=payload)
    except Exception as exc:
        return err_result(str(exc))


def model_orchestrator_start(
    evaluation_mode: str = "research",
    feature_set_id: str | None = None,
    source_round_group_id: str | None = None,
    n_rounds: int = 1,
    max_stage: str = "round_synthesis",
    run_id: str | None = None,
    session_id: str | None = None,
    parent_job_id: str | None = None,
    execute_qlib: bool = False,
    write_registry: bool = False,
    baseline_model_params: dict[str, Any] | None = None,
) -> ServiceResult:
    try:
        if evaluation_mode not in {"research", "production"}:
            return err_result(f"evaluation_mode_invalid:{evaluation_mode}")
        if evaluation_mode == "production" and not source_round_group_id:
            return err_result("source_round_group_id_required_for_production")
        if evaluation_mode == "production" and baseline_model_params:
            return err_result("baseline_model_params_research_only")
        baseline_validation = normalize_research_baseline_overrides(baseline_model_params)
        if not baseline_validation["passed"]:
            return err_result(
                "invalid_baseline_model_params",
                inputs={"baseline_model_params": baseline_model_params or {}},
                outputs={"errors": baseline_validation["errors"]},
            )
        resolved_baseline_model_params = dict(baseline_validation["normalized"])
        stamp = utc_now().replace(":", "").replace("-", "").replace("+", "_")
        job_id = run_id or f"model_orch_{stamp}"
        resolved_session_id = session_id or parent_job_id or f"msession_{stamp}"
        campaign_id = f"model_roll_{stamp}" if evaluation_mode == "production" else ""
        launch_payload = {
            "evaluation_mode": evaluation_mode,
            "feature_set_id": feature_set_id or "",
            "source_round_group_id": source_round_group_id or "",
            "n_rounds": max(0, int(n_rounds)),
            "max_stage": max_stage,
            "session_id": resolved_session_id,
            "parent_job_id": parent_job_id or "",
            "execute_qlib": bool(execute_qlib),
            "write_registry": bool(write_registry),
            "baseline_model_params": resolved_baseline_model_params,
            "campaign_id": campaign_id,
        }
        state = ModelStateStore()
        claimed, claimed_job = state.claim_managed_job(
            job_id,
            mode="orch",
            stage="queued",
            payload=launch_payload,
        )
        if not claimed:
            return ok_result(inputs=launch_payload, outputs={"status": "already_running", "active_job": claimed_job})
        log_dir = PROJECT_ROOT / "runtime" / "model" / "jobs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "model_run_orch_job.py"),
            "--evaluation-mode",
            evaluation_mode,
            "--job-id",
            job_id,
            "--session-id",
            resolved_session_id,
            "--rounds",
            str(max(0, int(n_rounds))),
            "--max-stage",
            max_stage,
        ]
        if feature_set_id:
            command.extend(["--feature-set-id", feature_set_id])
        if source_round_group_id:
            command.extend(["--source-round-group-id", source_round_group_id])
        if campaign_id:
            command.extend(["--campaign-id", campaign_id])
        if execute_qlib:
            command.append("--execute-qlib")
        if write_registry:
            command.append("--write-registry")
        if resolved_baseline_model_params:
            command.extend(["--baseline-model-params-json", json.dumps(resolved_baseline_model_params, ensure_ascii=False, sort_keys=True)])
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=str(PROJECT_ROOT),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception as exc:
            state.upsert_job(job_id, status="failed", stage="launch", mode="orch", payload={"launch_error": str(exc)})
            return err_result(f"orchestrator_launch_failed:{exc}", outputs={"job_id": job_id})
        job = state.upsert_job(
            job_id,
            status="running",
            stage="protocol_load" if evaluation_mode == "research" else "production_rolling",
            mode="orch",
            payload={"worker_pid": process.pid, "log_path": str(log_path), "launch_command": command},
        )
        return ok_result(inputs=launch_payload, outputs={"status": "accepted", "job_id": job_id, "session_id": resolved_session_id, "job": job})
    except Exception as exc:
        return err_result(str(exc))


def model_job_stop(job_id: str | None = None) -> ServiceResult:
    state = ModelStateStore()
    active = state.active_managed_job()
    if not active:
        return ok_result(outputs={"status": "idle"})
    if job_id and active.get("job_id") != job_id:
        return err_result("job_is_not_active", outputs={"active_job": active})
    stopped = state.request_job_stop(str(active.get("job_id") or ""))
    return ok_result(outputs={"status": "stopping", "job": stopped})


def model_job_resume(job_id: str) -> ServiceResult:
    state = ModelStateStore()
    source_job = state.get_job(job_id)
    if not source_job:
        return err_result("job_not_found")
    if source_job.get("status") not in {"interrupted", "failed"}:
        return err_result("job_not_resumable", outputs={"job": source_job})
    payload = dict(source_job.get("payload") or {})
    stamp = utc_now().replace(":", "").replace("-", "").replace("+", "_")
    new_job_id = f"model_orch_resume_{stamp}"
    claimed, active = state.claim_managed_job(
        new_job_id,
        mode="orch",
        stage="queued",
        payload={**payload, "resume_from_job_id": job_id, "cancel_requested": False},
    )
    if not claimed:
        return ok_result(outputs={"status": "already_running", "active_job": active})
    log_dir = PROJECT_ROOT / "runtime" / "model" / "jobs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{new_job_id}.log"
    evaluation_mode = str(payload.get("evaluation_mode") or "research")
    session_id = str(payload.get("session_id") or f"msession_{stamp}")
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "model_run_orch_job.py"),
        "--evaluation-mode", evaluation_mode,
        "--job-id", new_job_id,
        "--session-id", session_id,
        "--rounds", str(int(payload.get("n_tuning_rounds_requested") if payload.get("n_tuning_rounds_requested") is not None else (payload.get("n_rounds") or 1))),
        "--max-stage", str(payload.get("max_stage") or "round_synthesis"),
        "--resume",
    ]
    for flag, value in (
        ("--feature-set-id", payload.get("feature_set_id")),
        ("--source-round-group-id", payload.get("source_round_group_id")),
        ("--campaign-id", payload.get("campaign_id")),
    ):
        if value:
            command.extend([flag, str(value)])
    if payload.get("execute_qlib"):
        command.append("--execute-qlib")
    if payload.get("write_registry"):
        command.append("--write-registry")
    if payload.get("baseline_model_params"):
        command.extend([
            "--baseline-model-params-json",
            json.dumps(payload.get("baseline_model_params") or {}, ensure_ascii=False, sort_keys=True),
        ])
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            process = subprocess.Popen(command, cwd=str(PROJECT_ROOT), stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as exc:
        state.upsert_job(new_job_id, status="failed", stage="launch", mode="orch", payload={"launch_error": str(exc)})
        return err_result(f"orchestrator_resume_launch_failed:{exc}")
    job = state.upsert_job(new_job_id, status="running", stage="protocol_load", mode="orch", payload={"worker_pid": process.pid, "log_path": str(log_path), "launch_command": command})
    return ok_result(outputs={"status": "accepted", "job_id": new_job_id, "session_id": session_id, "job": job})


def model_promote(
    model_id: str | None = None,
    model_run_id: str | None = None,
    *,
    execute_qlib: bool = True,
    dry_run: bool = False,
    manual_override_reason: str | None = None,
) -> ServiceResult:
    try:
        result = production_refit_model(
            model_id=model_id,
            model_run_id=model_run_id,
            execute_qlib=execute_qlib,
            dry_run=dry_run,
            manual_override_reason=manual_override_reason,
        )
        inputs = {
            "model_id": model_id,
            "model_run_id": model_run_id,
            "execute_qlib": execute_qlib,
            "dry_run": dry_run,
            "manual_override_reason": manual_override_reason,
        }
        return ok_result(inputs=inputs, outputs=result) if result.get("ok") else err_result(result.get("err", "promote_failed"), inputs=inputs, outputs=result)
    except Exception as exc:
        return err_result(str(exc), inputs={"model_id": model_id, "model_run_id": model_run_id})
