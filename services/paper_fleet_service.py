from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from services._base import ServiceResult, err_result, ok_result
from services.data_foundation_service import data_status
from services.prediction_service import pred_status_snapshot
from services.trading_service import (
    _record_account_snapshot_from_execution,
    trading_execute_pending,
    trading_recommend,
)
from domain.model.naming import model_display_projection
from domain.trading.execution.qlib_paper import run_qlib_paper_mark_to_market
from domain.trading.recommendation import resolve_pending_execution_date
from domain.trading.prediction import get_qlib_latest_calendar_date, init_qlib, resolve_prediction_model_context
from domain.trading.prediction import load_pred_dataframe
from domain.trading.signals import _apply_st_filter, _assert_score_diversity, _score_quality
from domain.trading.confidence import (
    evaluate_confidence,
    is_confidence_cash_contract,
    load_model_confidence_evidence,
    normalize_confidence_policy,
    score_boundary_evidence,
)
from domain.data_foundation.stock_metadata import (
    load_stock_identity_map,
    load_stock_identity_rows_for_window,
    security_name_for_instrument,
)
from qlib.data import D
from storage.paths import (
    MODEL_DEFAULT_TOPK,
    PAPER_TRADING_RUNTIME_ROOT,
    PRODUCTION_RAW_HDF5,
    PROJECT_ROOT,
    TRADING_RUNTIME_ROOT,
)
from storage.model_registry import ModelRegistry
from storage.trading_registry import TradingRegistry


FLEET_RUNTIME_ROOT = TRADING_RUNTIME_ROOT / "fleet"
FLEET_LATEST_STATUS_FILE = FLEET_RUNTIME_ROOT / "latest_status.json"
PAPER_OPERATION_LOCK_FILE = FLEET_RUNTIME_ROOT / "paper_operation.lock"
DEFAULT_STRATEGY_CONTRACT = "top20_drop2_hold5_open_v1"
DEFAULT_HOLD_THRESH = 5
ACCOUNT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,127}$")
FIXED_MODEL_ACCOUNT_CONTRACT = "one_fixed_account_one_model_v1"
_SCORE_QUALITY_CACHE: dict[str, dict[str, Any]] = {}


def _prediction_snapshot_covers_window(
    *,
    model_id: str,
    model_run_id: str,
    from_date: str,
    to_date: str,
) -> ServiceResult | None:
    snapshot = pred_status_snapshot()
    if not snapshot.ok:
        return None
    outputs = snapshot.outputs or {}
    context = outputs.get("run_context") or {}
    update = outputs.get("update") or {}
    covered_end = str(
        update.get("updated_end")
        or update.get("pred_latest")
        or outputs.get("target_date")
        or ""
    )[:10]
    factor_date = str((outputs.get("factor_freshness") or {}).get("factor_latest_date") or "")[:10]
    if (
        str(context.get("model_id") or "") == model_id
        and str(context.get("model_run_id") or "") == model_run_id
        and covered_end >= to_date
        and (not factor_date or factor_date >= to_date)
    ):
        return ok_result(
            inputs={
                "model_id": model_id,
                "model_run_id": model_run_id,
                "from_date": from_date,
                "to_date": to_date,
            },
            outputs={
                "status": "already_current",
                "source": "latest_prediction_status_file",
                "covered_end": covered_end,
                "factor_latest_date": factor_date,
            },
            artifacts=snapshot.artifacts,
        )
    return None


def _isolated_prediction_update(
    *,
    model_id: str,
    model_run_id: str,
    from_date: str,
    to_date: str,
) -> ServiceResult:
    current = _prediction_snapshot_covers_window(
        model_id=model_id,
        model_run_id=model_run_id,
        from_date=from_date,
        to_date=to_date,
    )
    if current is not None:
        return current

    command = [
        sys.executable,
        str(PROJECT_ROOT / "cli.py"),
        "pred-update",
        "--model-id",
        model_id,
        "--model-run-id",
        model_run_id,
        "--from-date",
        from_date,
        "--to-date",
        to_date,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=3 * 60 * 60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return err_result(
            "isolated_prediction_update_failed",
            inputs={"command": command},
            outputs={"error": str(exc)},
        )

    payload = None
    stdout = completed.stdout or ""
    candidates = [0] + [index + 1 for index, char in enumerate(stdout) if char == "\n"]
    for index in reversed(candidates):
        candidate = stdout[index:].strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break
    if payload is None:
        return err_result(
            "isolated_prediction_update_invalid_output",
            inputs={"command": command},
            outputs={
                "returncode": completed.returncode,
                "stdout_tail": stdout[-4000:],
                "stderr_tail": (completed.stderr or "")[-4000:],
            },
        )
    return ServiceResult(
        ok=bool(payload.get("ok")) and completed.returncode == 0,
        err=str(payload.get("err") or ("isolated_prediction_update_process_exit_nonzero" if completed.returncode else "")),
        inputs=payload.get("inputs") or {},
        outputs=payload.get("outputs") or {},
        artifacts=payload.get("artifacts") or {},
        warnings=payload.get("warnings") or [],
    )


def _paper_account_market_context(
    history: list[dict[str, Any]],
    name_map: dict[str, str],
) -> dict[str, Any]:
    security_names: dict[str, str] = {}
    daily_trades: dict[str, list[dict[str, Any]]] = {}
    for snapshot in history:
        trade_date = str(snapshot.get("trade_date") or "")[:10]
        for instrument in (snapshot.get("positions") or {}):
            instrument_text = str(instrument)
            security_names[instrument_text] = security_name_for_instrument(instrument_text, name_map)
        trade_path_raw = str((snapshot.get("output_files") or {}).get("trades_file") or "")
        trade_path = Path(trade_path_raw) if trade_path_raw else None
        if not trade_date or not trade_path or not trade_path.is_file():
            continue
        try:
            frame = pd.read_csv(trade_path)
        except Exception:
            continue
        rows: list[dict[str, Any]] = []
        for raw in frame.to_dict("records"):
            instrument = str(raw.get("instrument") or raw.get("symbol") or "")
            if not instrument:
                continue
            security_name = security_name_for_instrument(instrument, name_map)
            security_names[instrument] = security_names.get(instrument) or security_name
            rows.append(
                {
                    "instrument": instrument,
                    "security_name": security_name,
                    "action": str(raw.get("action") or raw.get("side") or ""),
                    "filled_amount": raw.get("filled_amount") or raw.get("amount"),
                    "price": raw.get("price"),
                    "trade_value": raw.get("trade_value"),
                    "cost": raw.get("cost"),
                    "status": str(raw.get("status") or ""),
                }
            )
        daily_trades[trade_date] = sorted(
            rows,
            key=lambda row: abs(float(row.get("trade_value") or 0)),
            reverse=True,
        )
    return {"security_names": security_names, "daily_trades": daily_trades}


def _paper_recommendation_orders(
    registry: TradingRegistry,
    recommendation: dict[str, Any] | None,
    name_map: dict[str, str],
) -> list[dict[str, Any]]:
    recommendation_id = str((recommendation or {}).get("recommendation_id") or "")
    if not recommendation_id:
        return []
    rows: list[dict[str, Any]] = []
    for raw in registry.list_orders(recommendation_id, limit=80):
        row = dict(raw)
        instrument = str(row.get("instrument") or "")
        row["security_name"] = security_name_for_instrument(instrument, name_map) if instrument else ""
        rows.append(row)
    return rows


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _model_binding_projection(context: dict[str, Any]) -> dict[str, Any]:
    row = dict(context.get("registry_row") or {})
    row.setdefault("model_id", context.get("model_id", ""))
    row.setdefault("model_run_id", context.get("model_run_id", ""))
    row.setdefault("feature_set_id", context.get("feature_set_id", ""))
    row.setdefault("status", context.get("status", ""))
    metadata = _json_mapping(row.get("metadata"))
    row["metadata"] = metadata
    display = model_display_projection(row)
    manual_exception = _json_mapping(metadata.get("manual_promotion_exception"))
    manual_promotion = bool(manual_exception)
    source_stage = str(
        metadata.get("source_campaign_status")
        or manual_exception.get("source_campaign_status")
        or ""
    ).lower()
    tags: list[str] = []
    if manual_promotion:
        tags.append("手工晋升")
    if source_stage == "research":
        tags.append("研究来源")
    promotion_mode = "manual_promotion" if manual_promotion else (
        "formal_production" if str(row.get("status") or "").lower() == "production" else "research"
    )
    promotion_label = {
        "manual_promotion": "手工晋升",
        "formal_production": "正式晋升",
        "research": "研究模型",
    }[promotion_mode]
    display_name = display["display_name"]
    if manual_promotion:
        display_name = " · ".join(
            ["手工晋升", display["display_feature_set"], display["display_timestamp"]]
        )
    return {
        "contract_version": FIXED_MODEL_ACCOUNT_CONTRACT,
        "model_id": str(row.get("model_id") or ""),
        "model_run_id": str(row.get("model_run_id") or ""),
        "feature_set_id": str(row.get("feature_set_id") or ""),
        "model_status": str(row.get("status") or ""),
        "display_name": display_name,
        "model_display_name": display["display_name"],
        "display_feature_set": display["display_feature_set"],
        "display_timestamp": display["display_timestamp"],
        "promotion_mode": promotion_mode,
        "promotion_label": promotion_label,
        "source_stage": source_stage,
        "source_stage_label": "研究来源" if source_stage == "research" else "",
        "tags": tags,
    }


def _account_with_model_binding(
    account: dict[str, Any],
    deployments: list[dict[str, Any]],
    model_registry: ModelRegistry,
) -> dict[str, Any]:
    ordered = sorted(
        deployments,
        key=lambda row: (str(row.get("effective_from") or ""), str(row.get("created_at") or "")),
        reverse=True,
    )
    deployment = next((row for row in ordered if row.get("status") == "active"), ordered[0] if ordered else {})
    stored = _json_mapping((account.get("metadata") or {}).get("model_binding"))
    binding = stored
    model_id = str(deployment.get("model_id") or stored.get("model_id") or "")
    if model_id:
        model_row = model_registry.get(model_id)
        if model_row:
            binding = _model_binding_projection({"registry_row": model_row})
    projected = dict(account)
    projected["model_binding"] = binding
    projected["model_binding_contract"] = (
        FIXED_MODEL_ACCOUNT_CONTRACT if account.get("account_mode") == "fixed_model" else "dated_deployment_v1"
    )
    if account.get("account_mode") == "fixed_model" and binding.get("display_name"):
        projected["display_name"] = binding["display_name"]
    return projected


@contextmanager
def _paper_operation_lock():
    PAPER_OPERATION_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = PAPER_OPERATION_LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        holder = handle.read().strip()
        handle.close()
        raise BlockingIOError(holder or "paper operation lock is held")
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "acquired_at": _now()}))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "released_at": _now()}))
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _paper_operation_lock_status() -> dict[str, Any]:
    if not PAPER_OPERATION_LOCK_FILE.exists():
        return {"status": "idle", "active": False, "lock_file": str(PAPER_OPERATION_LOCK_FILE)}
    handle = PAPER_OPERATION_LOCK_FILE.open("r+", encoding="utf-8")
    try:
        content = handle.read().strip()
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "status": "held",
                "active": True,
                "lock_file": str(PAPER_OPERATION_LOCK_FILE),
                "holder": content,
            }
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {
            "status": "idle",
            "active": False,
            "lock_file": str(PAPER_OPERATION_LOCK_FILE),
            "last_holder": content,
        }
    finally:
        handle.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date10(value: Any) -> str:
    return str(pd.Timestamp(value).date())


def _validated_date10(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"invalid_{field}:expected_YYYY-MM-DD")
    parsed = pd.Timestamp(text)
    if pd.isna(parsed):
        raise ValueError(f"invalid_{field}:expected_YYYY-MM-DD")
    return str(parsed.date())


def _write_latest(payload: dict[str, Any]) -> None:
    FLEET_LATEST_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    FLEET_LATEST_STATUS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _calendar_dates(start_date: str, end_date: str) -> list[str]:
    init_qlib()
    calendar = pd.DatetimeIndex(pd.to_datetime(D.calendar(freq="day"))).normalize()
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    return [_date10(value) for value in calendar[(calendar >= start) & (calendar <= end)]]


def _data_context() -> tuple[ServiceResult, dict[str, Any]]:
    result = data_status()
    outputs = result.outputs or {}
    snapshot = outputs.get("snapshot") or {}
    current = outputs.get("current_production_dataset") or outputs.get("current_dataset") or {}
    context = {
        "hdf5_latest": _date10(snapshot.get("latest_hdf5_trade_date")) if snapshot.get("latest_hdf5_trade_date") else "",
        "qlib_latest": _date10(snapshot.get("latest_qlib_trade_date")) if snapshot.get("latest_qlib_trade_date") else "",
        "quantgpt_latest": _date10(snapshot.get("latest_quantgpt_trade_date")) if snapshot.get("latest_quantgpt_trade_date") else "",
        "data_package_id": str(
            current.get("production_package_id")
            or current.get("package_id")
            or outputs.get("production_package_id")
            or ""
        ),
        "production_health": (outputs.get("production_health") or {}).get("status", ""),
    }
    return result, context


def _config_hash(account: dict[str, Any], deployment: dict[str, Any]) -> str:
    metadata = account.get("metadata") or {}
    payload = {
        "account_id": account["account_id"],
        "model_run_id": deployment["model_run_id"],
        "strategy_contract_version": account["strategy_contract_version"],
        "initial_capital": float(account["initial_capital"]),
        "topk": int(account["topk"]),
        "n_drop": int(account["n_drop"]),
        "hold_thresh": int(account["hold_thresh"]),
        "deal_price": account["deal_price"],
        "confidence_policy": (
            normalize_confidence_policy(metadata.get("confidence_policy"))
            if is_confidence_cash_contract(account.get("strategy_contract_version"))
            else {}
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _replay_score_quality(
    *,
    account: dict[str, Any],
    deployments: dict[str, dict[str, Any]],
    dates: list[str],
) -> dict[str, Any]:
    """Validate every replay day's score cross-section before any account write."""
    contexts: dict[str, dict[str, Any]] = {}
    cache_inputs: list[dict[str, Any]] = []
    for model_run_id in sorted({deployments[value]["model_run_id"] for value in dates}):
        sample = next(row for row in deployments.values() if row["model_run_id"] == model_run_id)
        context = resolve_prediction_model_context(
            model_id=sample["model_id"],
            model_run_id=model_run_id,
            require_production=True,
        )
        contexts[model_run_id] = context
        pred_path = Path(context["recorder_run_dir"]) / "pred.pkl"
        stat = pred_path.stat()
        cache_inputs.append(
            {"model_run_id": model_run_id, "pred_size": stat.st_size, "pred_mtime_ns": stat.st_mtime_ns}
        )
    raw_stat = PRODUCTION_RAW_HDF5.stat()
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "account_id": account["account_id"],
                "topk": int(account["topk"]),
                "dates": dates,
                "deployments": [(value, deployments[value]["model_run_id"]) for value in dates],
                "models": cache_inputs,
                "raw_hdf_size": raw_stat.st_size,
                "raw_hdf_mtime_ns": raw_stat.st_mtime_ns,
                "strategy_contract_version": account.get("strategy_contract_version", ""),
                "confidence_policy": (account.get("metadata") or {}).get("confidence_policy", {}),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if cache_key in _SCORE_QUALITY_CACHE:
        return copy.deepcopy(_SCORE_QUALITY_CACHE[cache_key])

    frames: dict[str, pd.DataFrame] = {}
    identity_history = load_stock_identity_rows_for_window(min(dates), max(dates))
    rows: list[dict[str, Any]] = []
    blockers: list[str] = []
    for signal_date in dates:
        deployment = deployments[signal_date]
        model_run_id = deployment["model_run_id"]
        if model_run_id not in frames:
            context = contexts[model_run_id]
            frames[model_run_id] = load_pred_dataframe(context["recorder_run_dir"])
        frame = frames[model_run_id]
        target = pd.Timestamp(signal_date).normalize()
        available = pd.to_datetime(frame.index.get_level_values("datetime")).normalize()
        if target not in set(available):
            blockers.append(f"prediction_missing:{signal_date}")
            rows.append({"signal_date": signal_date, "status": "missing"})
            continue
        daily = frame.loc[available == target].copy()
        score_col = "score" if "score" in daily.columns else daily.columns[0]
        daily = daily.reset_index().rename(columns={score_col: "score"})
        identity_rows = identity_history.loc[identity_history["trade_date"] == target]
        daily, st_filter = _apply_st_filter(daily[["instrument", "score"]], identity_rows=identity_rows)
        if float(st_filter.get("identity_match_ratio") or 0.0) < 0.95:
            blockers.append(
                f"point_in_time_identity_coverage:{signal_date}:"
                f"{float(st_filter.get('identity_match_ratio') or 0.0):.4f}"
            )
            rows.append(
                {
                    "signal_date": signal_date,
                    "status": "blocked",
                    "error": "point_in_time_identity_coverage_below_95pct",
                    "st_filter": st_filter,
                }
            )
            continue
        daily = daily.sort_values(["score", "instrument"], ascending=[False, True]).reset_index(drop=True)
        boundary = score_boundary_evidence(daily, topk=int(account["topk"]))
        try:
            quality = _score_quality(daily)
            if int(quality.get("unique_score_count") or 0) <= 1 or float(quality.get("score_std") or 0.0) <= 1e-12:
                _assert_score_diversity(daily, topk=int(account["topk"]))
            if is_confidence_cash_contract(account.get("strategy_contract_version")):
                confidence = evaluate_confidence(
                    score_quality=quality,
                    boundary=boundary,
                    topk=int(account["topk"]),
                    model_evidence=load_model_confidence_evidence(contexts[model_run_id]),
                    policy=(account.get("metadata") or {}).get("confidence_policy"),
                    evidence_as_of=signal_date,
                )
                rows.append(
                    {
                        "signal_date": signal_date,
                        "status": confidence["confidence_state"],
                        **quality,
                        **boundary,
                        "confidence": confidence,
                        "st_filter": st_filter,
                    }
                )
            else:
                quality = _assert_score_diversity(daily, topk=int(account["topk"]))
                rows.append({"signal_date": signal_date, "status": "passed", **quality, **boundary, "st_filter": st_filter})
        except Exception as exc:
            quality = _score_quality(daily)
            blockers.append(f"score_quality:{signal_date}:{exc}")
            rows.append(
                {
                    "signal_date": signal_date,
                    **quality,
                    "status": "blocked",
                    "error": str(exc),
                    **boundary,
                    "st_filter": st_filter,
                }
            )
    result = {"status": "blocked" if blockers else "passed", "dates": rows, "blockers": blockers}
    if len(_SCORE_QUALITY_CACHE) >= 32:
        _SCORE_QUALITY_CACHE.pop(next(iter(_SCORE_QUALITY_CACHE)))
    _SCORE_QUALITY_CACHE[cache_key] = copy.deepcopy(result)
    return result


def _account_run_id(account_id: str, signal_date: str, config_hash: str) -> str:
    return f"paper-run-{account_id}-{signal_date}-{config_hash}"


def _fleet_run_id(target_date: str, data_package_id: str, fleet_config: list[dict[str, Any]] | None = None) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "target_date": target_date,
                "data_package_id": data_package_id,
                "fleet_config": fleet_config or [],
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"paper-fleet-{target_date}-{digest}"


def paper_account_create(
    *,
    account_id: str,
    model_id: str | None = None,
    model_run_id: str | None = None,
    display_name: str | None = None,
    account_mode: str = "fixed_model",
    initial_capital: float = 1_000_000.0,
    effective_from: str,
    strategy_contract_version: str = DEFAULT_STRATEGY_CONTRACT,
    topk: int = MODEL_DEFAULT_TOPK,
    n_drop: int = 2,
    hold_thresh: int = DEFAULT_HOLD_THRESH,
    deal_price: str = "open",
    metadata: dict[str, Any] | None = None,
) -> ServiceResult:
    inputs = {
        "account_id": account_id,
        "model_id": model_id,
        "model_run_id": model_run_id,
        "account_mode": account_mode,
        "initial_capital": initial_capital,
        "effective_from": effective_from,
        "strategy_contract_version": strategy_contract_version,
        "topk": topk,
        "n_drop": n_drop,
        "hold_thresh": hold_thresh,
        "deal_price": deal_price,
    }
    if not ACCOUNT_ID_RE.fullmatch(account_id):
        return err_result("invalid_account_id", inputs=inputs)
    if account_mode not in {"fixed_model", "rolling_champion"}:
        return err_result("invalid_account_mode", inputs=inputs)
    if int(topk) <= 0 or float(initial_capital) <= 0:
        return err_result("invalid_strategy_parameters", inputs=inputs)
    try:
        effective_date = _validated_date10(effective_from, field="effective_from")
        context = resolve_prediction_model_context(
            model_id=model_id,
            model_run_id=model_run_id,
            require_production=True,
        )
        registry = TradingRegistry()
        existing = registry.get_account(account_id)
        if existing and existing.get("status") == "retired":
            return err_result(
                "retired_account_id_immutable_create_new_account",
                inputs=inputs,
                outputs={"account": existing},
            )
        if existing and registry.latest_account_snapshot(account_id):
            immutable = {
                "initial_capital": float(initial_capital),
                "account_mode": account_mode,
                "strategy_contract_version": strategy_contract_version,
                "topk": int(topk),
                "n_drop": int(n_drop),
                "hold_thresh": int(hold_thresh),
                "deal_price": deal_price,
            }
            mismatches = {
                key: {"existing": existing.get(key), "requested": value}
                for key, value in immutable.items()
                if str(existing.get(key)) != str(value)
            }
            if mismatches:
                return err_result(
                    "account_contract_immutable_after_first_snapshot",
                    inputs=inputs,
                    outputs={"mismatches": mismatches},
                )
        deployments = registry.list_deployments(account_id)
        boundary_conflicts = [
            row for row in deployments
            if row.get("effective_from") == effective_date and row.get("model_run_id") != context["model_run_id"]
        ]
        if boundary_conflicts:
            return err_result(
                "deployment_boundary_conflict",
                inputs=inputs,
                outputs={"effective_from": effective_date, "conflicts": boundary_conflicts},
            )
        completed_after_boundary = [
            row for row in registry.list_account_runs(account_id, limit=10000)
            if row.get("status") == "completed" and row.get("signal_date", "") >= effective_date
        ]
        existing_at_boundary = registry.deployment_for_date(account_id, effective_date)
        if (
            completed_after_boundary
            and existing_at_boundary
            and existing_at_boundary.get("model_run_id") != context["model_run_id"]
        ):
            return err_result(
                "deployment_history_immutable_after_completed_run",
                inputs=inputs,
                outputs={"effective_from": effective_date, "completed_runs": completed_after_boundary[:20]},
            )
        if account_mode == "fixed_model":
            different_bindings = [
                row for row in deployments if row.get("model_run_id") != context["model_run_id"]
            ]
            if different_bindings:
                return err_result(
                    "fixed_model_account_already_bound_create_new_account",
                    inputs=inputs,
                    outputs={
                        "account_id": account_id,
                        "binding_contract": FIXED_MODEL_ACCOUNT_CONTRACT,
                        "existing_model_run_ids": sorted(
                            {str(row.get("model_run_id") or "") for row in deployments}
                        ),
                        "requested_model_run_id": context["model_run_id"],
                        "required_action": "create_a_new_account_for_the_new_model",
                    },
                )
        model_binding = _model_binding_projection(context)
        existing_metadata = dict((existing or {}).get("metadata") or {})
        merged_metadata = {**existing_metadata, **(metadata or {})}
        merged_metadata.setdefault("inception_date", effective_date)
        merged_metadata["model_binding"] = model_binding
        merged_metadata["model_binding_contract"] = (
            FIXED_MODEL_ACCOUNT_CONTRACT if account_mode == "fixed_model" else "dated_deployment_v1"
        )
        if is_confidence_cash_contract(strategy_contract_version):
            merged_metadata["confidence_policy"] = normalize_confidence_policy(
                merged_metadata.get("confidence_policy")
            )
        account = {
            "account_id": account_id,
            "display_name": model_binding["display_name"] if account_mode == "fixed_model" else (display_name or account_id),
            "account_mode": account_mode,
            "initial_capital": float(initial_capital),
            "strategy_contract_version": strategy_contract_version,
            "topk": int(topk),
            "n_drop": int(n_drop),
            "hold_thresh": int(hold_thresh),
            "deal_price": deal_price,
            # Account creation/upsert must not implicitly resume a paused or
            # retired lifecycle. Status changes use paper_account_set_status.
            "status": str((existing or {}).get("status") or "active"),
            "metadata": merged_metadata,
        }
        deployment_id = f"deploy-{account_id}-{context['model_run_id']}-{effective_date}"
        deployment = {
                "deployment_id": deployment_id,
                "account_id": account_id,
                "model_id": context["model_id"],
                "model_run_id": context["model_run_id"],
                "feature_set_id": context.get("feature_set_id", ""),
                "effective_from": effective_date,
                "deployment_mode": account_mode,
                "status": "active",
                "evidence": {"source": "paper_account_create", "model_status": context.get("status", "")},
            }
        registry.upsert_account_with_deployment(account=account, deployment=deployment)
        return ok_result(
            inputs=inputs,
            outputs={
                "status": "created" if not existing else "updated",
                "account": registry.get_account(account_id),
                "deployment": registry.deployment_for_date(account_id, effective_date),
                "model_binding": model_binding,
                "binding_contract": merged_metadata["model_binding_contract"],
            },
            warnings=(["requested_display_name_ignored_model_name_is_authoritative"] if display_name and account_mode == "fixed_model" else []),
        )
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def paper_account_set_status(*, account_id: str, status: str) -> ServiceResult:
    inputs = {"account_id": account_id, "status": status}
    if status not in {"active", "paused", "retired"}:
        return err_result("invalid_paper_account_status", inputs=inputs)
    registry = TradingRegistry()
    account = registry.get_account(account_id)
    if not account:
        return err_result("paper_account_not_found", inputs=inputs)
    reconciled_runs = registry.reconcile_stale_account_runs(account_id)
    if status == "active":
        issues = registry.account_integrity_issues(account_id)
        if issues:
            return err_result(
                "paper_account_integrity_blocked",
                inputs=inputs,
                outputs={"integrity_issues": issues, "reconciled_runs": reconciled_runs},
            )
    transition = registry.transition_account_status(account_id, status)
    return ok_result(
        inputs=inputs,
        outputs={
            "status": "updated",
            "account": registry.get_account(account_id),
            "transition": transition,
            "reconciled_runs": reconciled_runs,
        },
    )


def paper_replay_plan(
    *,
    account_id: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> ServiceResult:
    inputs = {"account_id": account_id, "from_date": from_date, "to_date": to_date}
    try:
        registry = TradingRegistry()
        account = registry.get_account(account_id)
        if not account:
            return err_result("paper_account_not_found", inputs=inputs)
        integrity_issues = registry.account_integrity_issues(account_id)
        data_result, data = _data_context()
        if not data_result.ok:
            return err_result("data_status_failed", inputs=inputs, outputs={"data": data_result.to_dict()})
        target = _date10(to_date or data.get("qlib_latest") or get_qlib_latest_calendar_date())
        if data.get("qlib_latest") and pd.Timestamp(target) > pd.Timestamp(data["qlib_latest"]):
            return err_result(
                "target_date_after_qlib_latest",
                inputs=inputs,
                outputs={"target_date": target, "qlib_latest": data["qlib_latest"]},
            )
        runs = registry.list_account_runs(account_id, limit=10000)
        completed_dates = {row["signal_date"] for row in runs if row.get("status") == "completed"}
        inception = str((account.get("metadata") or {}).get("inception_date") or "")
        if from_date:
            start = _date10(from_date)
        elif completed_dates:
            init_qlib()
            calendar = pd.DatetimeIndex(pd.to_datetime(D.calendar(freq="day"))).normalize()
            latest_completed = pd.Timestamp(max(completed_dates)).normalize()
            future = calendar[calendar > latest_completed]
            start = _date10(future[0]) if len(future) else target
        elif inception:
            start = _date10(inception)
        else:
            deployments = registry.list_deployments(account_id)
            if not deployments:
                return err_result("paper_account_deployment_missing", inputs=inputs)
            start = _date10(deployments[0]["effective_from"])
        dates = _calendar_dates(start, target) if pd.Timestamp(start) <= pd.Timestamp(target) else []
        pending_dates = [value for value in dates if value not in completed_dates]
        deployment_rows = []
        blockers: list[str] = [
            f"account_integrity:{item['code']}:{item.get('signal_date') or item.get('trade_date') or ''}"
            for item in integrity_issues
        ]
        for value in pending_dates:
            deployment = registry.deployment_for_date(account_id, value)
            if not deployment:
                blockers.append(f"deployment_missing:{value}")
                continue
            deployment_rows.append(
                {
                    "signal_date": value,
                    "deployment_id": deployment["deployment_id"],
                    "model_id": deployment["model_id"],
                    "model_run_id": deployment["model_run_id"],
                }
            )
        latest_snapshot = registry.latest_account_snapshot(account_id)
        pending = registry.pending_recommendations(limit=20, account_id=account_id)
        score_quality: dict[str, Any] = {"status": "not_checked", "dates": [], "blockers": []}
        score_warnings: list[str] = []
        if pending_dates and not blockers:
            score_quality = _replay_score_quality(
                account=account,
                deployments={row["signal_date"]: row for row in deployment_rows},
                dates=pending_dates,
            )
            for item in score_quality.get("blockers") or []:
                if str(item).startswith("prediction_missing:"):
                    score_warnings.append(str(item))
                else:
                    blockers.append(str(item))
        plan = {
            "account_id": account_id,
            "from_date": start,
            "to_date": target,
            "trade_dates": pending_dates,
            "trade_date_count": len(pending_dates),
            "completed_dates_skipped": sorted(set(dates) & completed_dates),
            "deployments": deployment_rows,
            "starting_snapshot_date": (latest_snapshot or {}).get("trade_date", ""),
            "starting_pending": pending,
            "data": data,
            "replay_basis": "latest_promoted_restated_asof_capped",
            "automatic": len(pending_dates) <= 5,
            "requires_confirmation": len(pending_dates) > 5,
            "score_quality": score_quality,
            "integrity_issues": integrity_issues,
            "warnings": score_warnings,
            "blockers": blockers,
        }
        if blockers:
            return err_result("paper_replay_plan_blocked", inputs=inputs, outputs=plan)
        return ok_result(inputs=inputs, outputs={"status": "ready", "plan": plan})
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def _checkpoint(
    registry: TradingRegistry,
    *,
    account_run: dict[str, Any],
    stage: str,
    status: str = "running",
    payload: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    row = dict(account_run)
    row.update({"current_stage": stage, "status": status, "error": error, "outputs": payload or {}})
    if status == "completed":
        row["completed_at"] = _now()
    registry.upsert_account_run(row)
    registry.record_run_event(
        {
            "fleet_run_id": row.get("fleet_run_id", ""),
            "account_run_id": row["account_run_id"],
            "account_id": row["account_id"],
            "stage": stage,
            "status": status,
            "payload": payload or {},
        }
    )


def _initialize_or_mark_account(
    registry: TradingRegistry,
    *,
    account: dict[str, Any],
    deployment: dict[str, Any],
    signal_date: str,
) -> dict[str, Any]:
    snapshot = registry.latest_account_snapshot(account["account_id"])
    if snapshot and snapshot.get("trade_date") == signal_date:
        return {"status": "already_marked", "snapshot": snapshot}
    result = run_qlib_paper_mark_to_market(
        version_id=account["account_id"],
        trade_date=signal_date,
        initial_capital=float(account["initial_capital"]),
    )
    if not result.ok:
        raise RuntimeError(str((result.diagnostics or {}).get("reason") or "mark_to_market_failed"))
    _record_account_snapshot_from_execution(
        registry,
        result,
        {
            "account_id": account["account_id"],
            "model_run_id": deployment["model_run_id"],
            "execution_date": signal_date,
            "recommendation_id": "",
        },
    )
    return {"status": "marked", "metrics": result.metrics, "output_files": result.output_files}


def _reconcile_published_account_state(
    registry: TradingRegistry,
    *,
    account: dict[str, Any],
    deployment: dict[str, Any],
) -> list[str]:
    """Finish a DB commit when a durable Qlib account state is ahead of SQLite."""
    state_file = PAPER_TRADING_RUNTIME_ROOT / account["account_id"] / "state" / "account_state.json"
    if not state_file.exists():
        return []
    state = json.loads(state_file.read_text(encoding="utf-8"))
    trade_date = str(state.get("as_of_date") or state.get("trade_date") or "")[:10]
    if not trade_date:
        return []
    state.setdefault("account_id", account["account_id"])
    state.setdefault("model_run_id", deployment["model_run_id"])
    state.setdefault("trade_date", trade_date)
    recommendation_id = str(state.get("source_recommendation_id") or "")
    snapshot = registry.account_snapshot(account["account_id"], trade_date)
    actions: list[str] = []
    if recommendation_id and not registry.execution_for_recommendation(recommendation_id):
        recommendation = registry.get_recommendation(recommendation_id)
        meta_path = Path(str((state.get("output_files") or {}).get("execution_meta_file") or ""))
        if not recommendation or not meta_path.exists():
            raise RuntimeError(f"published_state_recovery_evidence_missing:{trade_date}:{recommendation_id}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if str(meta.get("trade_date") or "")[:10] != trade_date or str(meta.get("recommendation_id") or "") != recommendation_id:
            raise RuntimeError(f"published_state_recovery_evidence_mismatch:{trade_date}:{recommendation_id}")
        execution = {
            "execution_id": f"exec-{recommendation_id}-{trade_date}",
            "account_id": account["account_id"],
            "recommendation_id": recommendation_id,
            "model_id": recommendation.get("model_id", ""),
            "model_run_id": recommendation.get("model_run_id", ""),
            "trade_date": trade_date,
            "status": "completed",
            "adapter": meta.get("adapter", ""),
            "output_files": meta.get("output_files") or state.get("output_files") or {},
            "metrics": meta.get("metrics") or {},
            "diagnostics": {**(meta.get("diagnostics") or {}), "recovered_from_published_state": True},
            "notes": ["recovered durable Qlib execution after interrupted registry commit"],
        }
        registry.commit_execution(
            execution=execution,
            recommendation_id=recommendation_id,
            recommendation_status="executed",
            snapshot=state,
        )
        actions.append(f"execution_commit_recovered:{recommendation_id}")
    elif not snapshot:
        registry.record_account_snapshot(state)
        actions.append(f"snapshot_recovered:{trade_date}")
    return actions


def _account_day_audit(
    registry: TradingRegistry,
    *,
    account_id: str,
    signal_date: str,
) -> None:
    snapshot = registry.account_snapshot(account_id, signal_date)
    if not snapshot:
        raise RuntimeError("post_run_audit_snapshot_missing")
    identity_gap = abs(
        float(snapshot.get("cash") or 0)
        + float(snapshot.get("stock_value") or 0)
        - float(snapshot.get("account_value") or 0)
    )
    if identity_gap > 0.01:
        raise RuntimeError(f"post_run_audit_account_value_identity_mismatch:{identity_gap:.6f}")
    latest_rec = registry.latest_recommendation(account_id)
    if not latest_rec or latest_rec.get("signal_date") != signal_date:
        raise RuntimeError("post_run_audit_latest_recommendation_mismatch")
    issues = registry.account_integrity_issues(account_id)
    if issues:
        raise RuntimeError(f"post_run_audit_integrity_failed:{json.dumps(issues, ensure_ascii=False)}")


def paper_account_day_run(
    *,
    registry: TradingRegistry,
    account: dict[str, Any],
    deployment: dict[str, Any],
    signal_date: str,
    data: dict[str, Any],
    identity_rows: pd.DataFrame,
    fleet_run_id: str = "",
    run_kind: str = "on_time",
    replay_basis: str = "",
) -> ServiceResult:
    """Run exactly one paper account for exactly one signal date.

    This is the only function that advances an account ledger. Fleet runs and
    historical replays only decide which accounts and dates should call it.
    """
    inputs = {
        "account_id": account.get("account_id", ""),
        "signal_date": signal_date,
        "model_run_id": deployment.get("model_run_id", ""),
        "fleet_run_id": fleet_run_id,
        "run_kind": run_kind,
    }
    if run_kind not in {"on_time", "catch_up_replay", "manual"}:
        return err_result("invalid_paper_run_kind", inputs=inputs)
    if account.get("status") != "active":
        return err_result("paper_account_not_active", inputs=inputs, outputs={"account": account})

    account_id = str(account["account_id"])
    recovery_actions = _reconcile_published_account_state(
        registry,
        account=account,
        deployment=deployment,
    )
    integrity_issues = registry.account_integrity_issues(account_id)
    if integrity_issues:
        return err_result(
            "paper_account_integrity_blocked",
            inputs=inputs,
            outputs={"integrity_issues": integrity_issues, "recovery_actions": recovery_actions},
        )
    config_hash = _config_hash(account, deployment)
    account_run_id = _account_run_id(account_id, signal_date, config_hash)
    existing_run = registry.get_account_run(account_run_id)
    if existing_run and existing_run.get("status") == "completed":
        return ok_result(
            inputs=inputs,
            outputs={
                "signal_date": signal_date,
                "status": "already_completed",
                "account_run_id": account_run_id,
            },
        )

    account_run = {
        "account_run_id": account_run_id,
        "fleet_run_id": fleet_run_id,
        "account_id": account_id,
        "signal_date": signal_date,
        "model_id": deployment["model_id"],
        "model_run_id": deployment["model_run_id"],
        "strategy_contract_version": account["strategy_contract_version"],
        "config_hash": config_hash,
        "run_kind": run_kind,
        "status": "running",
        "current_stage": "started",
        "attempt": int((existing_run or {}).get("attempt", 0)) + 1,
        "inputs": {
            "data_package_id": data.get("data_package_id", ""),
            "data_latest_date": data.get("qlib_latest", ""),
            "as_of_date": signal_date,
            "replay_basis": replay_basis,
        },
    }
    registry.upsert_account_run(account_run)
    try:
        _checkpoint(registry, account_run=account_run, stage="execute_pending")
        pending_before = registry.pending_recommendations(limit=20, account_id=account_id)
        due_pending: list[dict[str, Any]] = []
        unresolved_prior: list[str] = []
        for pending_row in pending_before:
            row = dict(pending_row)
            execution_date = str(row.get("execution_date") or "")
            if not execution_date:
                execution_date = str(resolve_pending_execution_date(str(row.get("signal_date") or "")) or "")
                if execution_date:
                    registry.set_execution_date(str(row["recommendation_id"]), execution_date)
                    row["execution_date"] = execution_date
            if execution_date and pd.Timestamp(execution_date) <= pd.Timestamp(signal_date):
                due_pending.append(row)
            elif not execution_date and pd.Timestamp(str(row.get("signal_date") or signal_date)) < pd.Timestamp(signal_date):
                unresolved_prior.append(str(row.get("recommendation_id") or "unknown"))
        if unresolved_prior:
            raise RuntimeError(f"pending_execution_date_unresolved:{','.join(unresolved_prior)}")
        due_pending.sort(key=lambda row: (str(row.get("execution_date") or ""), str(row.get("signal_date") or "")))
        if due_pending:
            executions: list[dict[str, Any]] = []
            for due_row in due_pending:
                execution = trading_execute_pending(
                    recommendation_id=due_row["recommendation_id"],
                    account_id=account_id,
                    total_capital=float(account["initial_capital"]),
                    include_status_snapshot=False,
                )
                execution_payload = execution.to_dict()
                failed = ((execution.outputs or {}).get("failed") or []) if execution.ok else [execution.err]
                skipped = ((execution.outputs or {}).get("skipped") or []) if execution.ok else []
                executed = ((execution.outputs or {}).get("executed") or []) if execution.ok else []
                if failed or skipped or not executed:
                    raise RuntimeError(f"pending_execution_failed:failed={failed}:skipped={skipped}")
                executions.append(execution_payload)
            execution_payload = executions[-1] if len(executions) == 1 else {
                "ok": True,
                "err": "",
                "outputs": {"status": "completed", "executions": executions},
                "warnings": [],
            }
        else:
            execution_payload = _initialize_or_mark_account(
                registry,
                account=account,
                deployment=deployment,
                signal_date=signal_date,
            )

        _checkpoint(
            registry,
            account_run=account_run,
            stage="generate_recommendation",
            payload={"execution": execution_payload},
        )
        recommendation = trading_recommend(
            account_id=account_id,
            model_id=deployment["model_id"],
            model_run_id=deployment["model_run_id"],
            signal_date=signal_date,
            topk=int(account["topk"]),
            total_capital=float(account["initial_capital"]),
            ensure_pred_latest=False,
            strategy_contract_version=account["strategy_contract_version"],
            n_drop=int(account["n_drop"]),
            hold_thresh=int(account["hold_thresh"]),
            deal_price=str(account["deal_price"]),
            run_kind=run_kind,
            data_package_id=data.get("data_package_id", ""),
            identity_rows=identity_rows,
            confidence_policy=(account.get("metadata") or {}).get("confidence_policy"),
            include_status_snapshot=False,
        )
        if not recommendation.ok:
            raise RuntimeError(f"recommendation_failed:{recommendation.err}")
        output = {
            "execution": execution_payload,
            "recommendation": recommendation.to_dict(),
            "snapshot": registry.latest_account_snapshot(account_id),
        }
        _checkpoint(registry, account_run=account_run, stage="post_run_audit", payload=output)
        _account_day_audit(registry, account_id=account_id, signal_date=signal_date)
        _checkpoint(registry, account_run=account_run, stage="completed", status="completed", payload=output)
        return ok_result(
            inputs=inputs,
            outputs={
                "signal_date": signal_date,
                "status": "completed",
                "account_run_id": account_run_id,
                **output,
            },
        )
    except Exception as exc:
        _checkpoint(registry, account_run=account_run, stage="failed", status="failed", error=str(exc))
        return err_result(
            "paper_account_day_failed",
            inputs=inputs,
            outputs={
                "signal_date": signal_date,
                "account_run_id": account_run_id,
                "error": str(exc),
            },
        )


def _run_account_plan(
    *,
    registry: TradingRegistry,
    account: dict[str, Any],
    plan: dict[str, Any],
    fleet_run_id: str = "",
    on_time_date: str = "",
) -> ServiceResult:
    """Prepare predictions once, then dispatch each date to the day engine."""
    dates = list(plan.get("trade_dates") or [])
    inputs = {
        "account_id": account.get("account_id", ""),
        "from_date": dates[0] if dates else None,
        "to_date": dates[-1] if dates else None,
        "fleet_run_id": fleet_run_id,
    }
    if not dates:
        return ok_result(inputs=inputs, outputs={"status": "already_current", "runs": []})
    data = plan.get("data") or {}
    if data.get("production_health") not in {"ready", ""}:
        return err_result("data_production_health_not_ready", inputs=inputs, outputs={"plan": plan})
    if len({data.get("hdf5_latest"), data.get("qlib_latest"), data.get("quantgpt_latest")} - {""}) > 1:
        return err_result("production_data_latest_dates_misaligned", inputs=inputs, outputs={"data": data})

    deployments = {row["signal_date"]: row for row in plan.get("deployments") or []}
    missing_deployments = [value for value in dates if value not in deployments]
    if missing_deployments:
        return err_result(
            "deployment_missing_during_paper_run",
            inputs=inputs,
            outputs={"missing_dates": missing_deployments},
        )
    model_ranges: dict[str, dict[str, Any]] = {}
    for signal_date in dates:
        deployment = deployments[signal_date]
        entry = model_ranges.setdefault(
            deployment["model_run_id"],
            {"model_id": deployment["model_id"], "start": signal_date, "end": signal_date},
        )
        entry["start"] = min(entry["start"], signal_date)
        entry["end"] = max(entry["end"], signal_date)
    prediction_updates: list[dict[str, Any]] = []
    for model_run_id, window in model_ranges.items():
        update = _isolated_prediction_update(
            model_id=window["model_id"],
            model_run_id=model_run_id,
            from_date=window["start"],
            to_date=window["end"],
        )
        prediction_updates.append(update.to_dict())
        if not update.ok:
            return err_result(
                "paper_prediction_update_failed",
                inputs=inputs,
                outputs={"prediction_updates": prediction_updates},
            )

    score_quality = _replay_score_quality(account=account, deployments=deployments, dates=dates)
    if score_quality["status"] != "passed":
        return err_result(
            "paper_score_quality_blocked",
            inputs=inputs,
            outputs={"prediction_updates": prediction_updates, "score_quality": score_quality},
        )

    identity_history = load_stock_identity_rows_for_window(min(dates), max(dates))
    run_results: list[dict[str, Any]] = []
    for signal_date in dates:
        identity_rows = identity_history.loc[
            identity_history["trade_date"] == pd.Timestamp(signal_date).normalize()
        ]
        run_kind = "on_time" if on_time_date and signal_date == on_time_date else "catch_up_replay"
        day = paper_account_day_run(
            registry=registry,
            account=account,
            deployment=deployments[signal_date],
            signal_date=signal_date,
            data=data,
            identity_rows=identity_rows,
            fleet_run_id=fleet_run_id,
            run_kind=run_kind,
            replay_basis=str(plan.get("replay_basis") or ""),
        )
        if not day.ok:
            return err_result(
                "paper_account_plan_failed",
                inputs=inputs,
                outputs={
                    "failed_date": signal_date,
                    "error": (day.outputs or {}).get("error") or day.err,
                    "runs": run_results,
                    "day_run": day.to_dict(),
                    "prediction_updates": prediction_updates,
                    "score_quality": score_quality,
                },
            )
        run_results.append(day.outputs or {})
    return ok_result(
        inputs=inputs,
        outputs={
            "status": "completed",
            "runs": run_results,
            "prediction_updates": prediction_updates,
            "score_quality": score_quality,
            "latest_snapshot": registry.latest_account_snapshot(account["account_id"]),
            "latest_recommendation": registry.latest_recommendation(account["account_id"]),
        },
    )


def paper_replay_run(
    *,
    account_id: str,
    from_date: str | None = None,
    to_date: str | None = None,
    confirm_long_replay: bool = False,
    fleet_run_id: str = "",
    _lock_acquired: bool = False,
) -> ServiceResult:
    if not _lock_acquired:
        try:
            with _paper_operation_lock():
                return paper_replay_run(
                    account_id=account_id,
                    from_date=from_date,
                    to_date=to_date,
                    confirm_long_replay=confirm_long_replay,
                    fleet_run_id=fleet_run_id,
                    _lock_acquired=True,
                )
        except BlockingIOError as exc:
            return err_result(
                "paper_operation_in_progress",
                inputs={"account_id": account_id, "from_date": from_date, "to_date": to_date},
                outputs={"lock_file": str(PAPER_OPERATION_LOCK_FILE), "holder": str(exc)},
            )
    inputs = {
        "account_id": account_id,
        "from_date": from_date,
        "to_date": to_date,
        "confirm_long_replay": confirm_long_replay,
        "fleet_run_id": fleet_run_id,
    }
    plan_result = paper_replay_plan(account_id=account_id, from_date=from_date, to_date=to_date)
    if not plan_result.ok:
        return err_result(plan_result.err or "paper_replay_plan_failed", inputs=inputs, outputs=plan_result.to_dict())
    plan = (plan_result.outputs or {}).get("plan") or {}
    if plan.get("requires_confirmation") and not confirm_long_replay:
        return err_result("long_replay_confirmation_required", inputs=inputs, outputs={"plan": plan})
    dates = list(plan.get("trade_dates") or [])
    if not dates:
        return ok_result(inputs=inputs, outputs={"status": "already_current", "plan": plan, "runs": []})
    registry = TradingRegistry()
    account = registry.get_account(account_id)
    if not account or account.get("status") != "active":
        return err_result("paper_account_not_active", inputs=inputs, outputs={"account": account})
    run = _run_account_plan(registry=registry, account=account, plan=plan, fleet_run_id=fleet_run_id)
    if not run.ok:
        error_map = {
            "paper_prediction_update_failed": "replay_prediction_update_failed",
            "paper_score_quality_blocked": "replay_score_quality_blocked",
            "paper_account_plan_failed": "paper_replay_failed",
        }
        return err_result(
            error_map.get(run.err, run.err or "paper_replay_failed"),
            inputs=inputs,
            outputs={"plan": plan, **(run.outputs or {})},
        )
    return ok_result(
        inputs=inputs,
        outputs={"plan": plan, **(run.outputs or {}), "account": registry.get_account(account_id)},
    )


def paper_fleet_preflight(*, target_date: str | None = None) -> ServiceResult:
    inputs = {"target_date": target_date}
    registry = TradingRegistry()
    data_result, data = _data_context()
    global_blockers: list[str] = []
    if not data_result.ok:
        global_blockers.append("data_status_failed")
    if data.get("production_health") not in {"ready", ""}:
        global_blockers.append("data_production_health_not_ready")
    dates = {
        value
        for value in (data.get("hdf5_latest"), data.get("qlib_latest"), data.get("quantgpt_latest"))
        if value
    }
    if len(dates) > 1:
        global_blockers.append("production_data_latest_dates_misaligned")
    accounts = registry.list_accounts("active")
    if not accounts:
        global_blockers.append("no_active_paper_accounts")
    target = _date10(target_date or data.get("qlib_latest") or get_qlib_latest_calendar_date())
    plans = []
    runnable_accounts: list[dict[str, Any]] = []
    account_blockers: list[dict[str, Any]] = []
    for account in accounts:
        plan = paper_replay_plan(account_id=account["account_id"], to_date=target)
        plans.append(plan.to_dict())
        if not plan.ok:
            account_blockers.append(
                {
                    "account_id": account["account_id"],
                    "error": str(plan.err or "paper_replay_plan_blocked"),
                    "plan": plan.to_dict(),
                }
            )
        else:
            runnable_accounts.append(account)
    blockers = list(global_blockers)
    if not runnable_accounts and accounts:
        blockers.append("no_runnable_paper_accounts")
    pending_trade_dates = sum(
        int((((row.get("outputs") or {}).get("plan") or {}).get("trade_date_count") or 0))
        for row in plans
        if row.get("ok")
    )
    account_warnings = [
        f"account_plan_blocked:{item['account_id']}:{item['error']}" for item in account_blockers
    ]
    status = "blocked" if blockers else "go" if pending_trade_dates else "already_current"
    return ok_result(
        inputs=inputs,
        outputs={
            "status": status,
            "target_date": target,
            "data": data,
            "accounts": accounts,
            "runnable_accounts": runnable_accounts,
            "account_blockers": account_blockers,
            "plans": plans,
            "pending_trade_date_count": pending_trade_dates,
            "blockers": blockers,
        },
        warnings=blockers + account_warnings,
    )


def _paper_fleet_run_locked(*, target_date: str | None = None, confirm_long_replay: bool = False) -> ServiceResult:
    inputs = {"target_date": target_date, "confirm_long_replay": confirm_long_replay}
    registry = TradingRegistry()
    reconciled_runs = registry.reconcile_stale_account_runs()
    preflight = paper_fleet_preflight(target_date=target_date)
    outputs = preflight.outputs or {}
    if outputs.get("status") == "already_current":
        payload = {
            "status": "already_current",
            "preflight": preflight.to_dict(),
            "reconciled_runs": reconciled_runs,
            "generated_at": _now(),
        }
        _write_latest(payload)
        return ok_result(inputs=inputs, outputs=payload)
    if outputs.get("status") != "go":
        payload = {"status": "blocked", "preflight": preflight.to_dict(), "generated_at": _now()}
        _write_latest(payload)
        return err_result("paper_fleet_preflight_blocked", inputs=inputs, outputs=payload)
    target = outputs["target_date"]
    data = outputs.get("data") or {}
    all_accounts = outputs.get("accounts") or []
    accounts = outputs.get("runnable_accounts") or all_accounts
    fleet_config = [
        {
            "account_id": account.get("account_id", ""),
            "strategy_contract_version": account.get("strategy_contract_version", ""),
            "updated_at": account.get("updated_at", ""),
        }
        for account in all_accounts
    ]
    fleet_run_id = _fleet_run_id(target, data.get("data_package_id", ""), fleet_config)
    existing = registry.get_fleet_run(fleet_run_id)
    if existing and existing.get("status") == "completed":
        return ok_result(
            inputs=inputs,
            outputs={"status": "already_completed", "fleet_run": existing, "reconciled_runs": reconciled_runs},
        )
    account_blockers = outputs.get("account_blockers") or []
    fleet_row = {
        "fleet_run_id": fleet_run_id,
        "target_date": target,
        "data_package_id": data.get("data_package_id", ""),
        "data_latest_date": data.get("qlib_latest", ""),
        "status": "running",
        "current_stage": "run_accounts",
        "account_count": len(all_accounts),
        "inputs": inputs,
    }
    registry.upsert_fleet_run(fleet_row)
    results = [
        {
            "ok": False,
            "err": "paper_replay_plan_blocked",
            "outputs": item,
        }
        for item in account_blockers
    ]
    completed = 0
    failed = len(account_blockers)
    plans_by_account: dict[str, dict[str, Any]] = {}
    for serialized in outputs.get("plans") or []:
        plan_outputs = serialized.get("outputs") or {}
        plan = plan_outputs.get("plan") or plan_outputs
        if serialized.get("ok") and plan.get("account_id"):
            plans_by_account[str(plan["account_id"])] = plan
    for account in accounts:
        plan = plans_by_account.get(str(account["account_id"]))
        if not plan:
            result = err_result(
                "paper_fleet_plan_missing",
                outputs={"account_id": account["account_id"], "target_date": target},
            )
        elif plan.get("requires_confirmation") and not confirm_long_replay:
            result = err_result(
                "long_replay_confirmation_required",
                outputs={"account_id": account["account_id"], "plan": plan},
            )
        else:
            result = _run_account_plan(
                registry=registry,
                account=account,
                plan=plan,
                fleet_run_id=fleet_run_id,
                on_time_date=target,
            )
        results.append(result.to_dict())
        if result.ok:
            completed += 1
        else:
            failed += 1
    status = "completed" if failed == 0 else "partial_failed"
    fleet_row.update(
        {
            "status": status,
            "current_stage": "completed" if failed == 0 else "partial_failed",
            "completed_count": completed,
            "failed_count": failed,
            "outputs": {"accounts": results},
            "completed_at": _now(),
            "error": "" if failed == 0 else f"{failed} account(s) failed",
        }
    )
    registry.upsert_fleet_run(fleet_row)
    payload = {
        "status": status,
        "fleet_run_id": fleet_run_id,
        "target_date": target,
        "completed_count": completed,
        "failed_count": failed,
        "accounts": results,
        "generated_at": _now(),
    }
    _write_latest(payload)
    if failed:
        return err_result("paper_fleet_partial_failed", inputs=inputs, outputs=payload)
    return ok_result(inputs=inputs, outputs=payload, artifacts={"latest_status_file": str(FLEET_LATEST_STATUS_FILE)})


def paper_fleet_run(*, target_date: str | None = None, confirm_long_replay: bool = False) -> ServiceResult:
    try:
        with _paper_operation_lock():
            return _paper_fleet_run_locked(
                target_date=target_date,
                confirm_long_replay=confirm_long_replay,
            )
    except BlockingIOError as exc:
        return err_result(
            "paper_operation_in_progress",
            inputs={"target_date": target_date, "confirm_long_replay": confirm_long_replay},
            outputs={"lock_file": str(PAPER_OPERATION_LOCK_FILE), "holder": str(exc)},
        )


def paper_fleet_status(*, compact: bool = False) -> ServiceResult:
    registry = TradingRegistry()
    model_registry = ModelRegistry()
    data_result, data = _data_context()
    try:
        security_name_map = load_stock_identity_map()
    except Exception:
        security_name_map = {}
    active_accounts = []
    paused_accounts = []
    retired_accounts = []
    archived_accounts = []
    for account in registry.list_accounts():
        deployments = registry.list_deployments(account["account_id"])
        account = _account_with_model_binding(account, deployments, model_registry)
        latest_snapshot = registry.latest_account_snapshot(account["account_id"])
        latest_recommendation = registry.latest_recommendation(account["account_id"])
        runs = registry.list_account_runs(account["account_id"], limit=30 if compact else 260)
        runs = sorted(
            runs,
            key=lambda row: (str(row.get("signal_date") or ""), str(row.get("updated_at") or "")),
            reverse=True,
        )
        run_summaries = [
            {
                key: row.get(key)
                for key in (
                    "account_run_id",
                    "fleet_run_id",
                    "account_id",
                    "signal_date",
                    "run_kind",
                    "status",
                    "current_stage",
                    "attempt",
                    "error",
                    "started_at",
                    "completed_at",
                    "updated_at",
                )
            }
            for row in runs
        ]
        pending = registry.pending_recommendations(limit=20, account_id=account["account_id"])
        pending_summaries = [
            {
                key: row.get(key)
                for key in (
                    "recommendation_id",
                    "account_id",
                    "signal_date",
                    "execution_date",
                    "status",
                    "updated_at",
                )
            }
            for row in pending
        ]
        integrity_issues = registry.account_integrity_issues(account["account_id"])
        if account.get("status") != "active":
            lifecycle = str(account.get("status") or "")
            management_item = {
                **account,
                "deployments": deployments,
                "latest_snapshot": latest_snapshot,
                "latest_recommendation": latest_recommendation,
                "latest_run": run_summaries[0] if run_summaries else {},
                "recent_runs": run_summaries[:5 if compact else 30],
                "pending_recommendations": pending_summaries,
                "pending_mode": "frozen" if lifecycle == "paused" else "settled",
                "integrity_issues": integrity_issues,
            }
            archived_accounts.append(management_item)
            if lifecycle == "paused":
                paused_accounts.append(management_item)
            else:
                retired_accounts.append(management_item)
            continue
        completed_dates = [row.get("signal_date", "") for row in runs if row.get("status") == "completed"]
        latest_completed_date = max(completed_dates) if completed_dates else ""
        target_date = str(data.get("qlib_latest") or "")
        gap_state = "unknown"
        if target_date:
            gap_state = "current" if latest_completed_date >= target_date else "needs_plan"
        raw_account_history = registry.list_account_snapshots(account["account_id"], limit=90 if compact else 260)
        market_context = {} if compact else _paper_account_market_context(raw_account_history, security_name_map)
        account_history = []
        for snapshot in raw_account_history:
            risk = snapshot.get("risk_metrics") or {}
            positions = {} if compact else {
                instrument: {
                    key: position.get(key)
                    for key in ("shares", "amount", "price", "market_value", "count_day")
                }
                for instrument, position in (snapshot.get("positions") or {}).items()
            }
            account_history.append(
                {
                    key: snapshot.get(key)
                    for key in (
                        "trade_date",
                        "cash",
                        "stock_value",
                        "account_value",
                        "daily_pnl",
                        "daily_return",
                    )
                }
                | {
                    "positions": positions,
                    "risk_metrics": {
                        key: risk.get(key)
                        for key in (
                            "execution_mode",
                            "target_stock_exposure",
                            "target_cash_weight",
                            "actual_stock_exposure",
                            "actual_cash_weight",
                            "exposure_gap",
                        )
                    },
                }
            )
        latest_orders = [] if compact else _paper_recommendation_orders(registry, latest_recommendation, security_name_map)
        for order in latest_orders:
            instrument = str(order.get("instrument") or "")
            if instrument:
                market_context["security_names"][instrument] = str(
                    order.get("security_name") or market_context["security_names"].get(instrument) or ""
                )
        item = {
            **account,
            "deployments": deployments,
            "latest_snapshot": latest_snapshot,
            "account_history": account_history,
            **market_context,
            "latest_recommendation": latest_recommendation,
            "latest_orders": latest_orders,
            "pending_recommendations": pending_summaries,
            "integrity_issues": integrity_issues,
            "recent_runs": run_summaries[:5 if compact else 30],
            "gap_summary": {
                "status": gap_state,
                "latest_completed_date": latest_completed_date,
                "target_date": target_date,
                "exact_plan_required": gap_state != "current",
            },
        }
        active_accounts.append(item)
    latest = {}
    if FLEET_LATEST_STATUS_FILE.exists():
        try:
            latest = json.loads(FLEET_LATEST_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            latest = {"status": "unreadable", "error": str(exc)}
    blocked_accounts = []
    for item in active_accounts:
        recent = item.get("recent_runs") or []
        reasons = []
        reasons.extend(
            f"account_integrity:{row.get('code', 'unknown')}:{row.get('signal_date') or row.get('trade_date') or ''}"
            for row in item.get("integrity_issues") or []
        )
        if recent and recent[0].get("status") == "failed":
            reasons.append(f"latest_account_run_failed:{recent[0].get('signal_date', '')}")
        target_date = str(data.get("qlib_latest") or "")
        for pending_row in item.get("pending_recommendations") or []:
            signal_date = str(pending_row.get("signal_date") or "")
            execution_date = str(pending_row.get("execution_date") or "")
            if target_date and execution_date and pd.Timestamp(execution_date) <= pd.Timestamp(target_date):
                reasons.append(f"pending_execution_overdue:{execution_date}")
            elif target_date and signal_date and not execution_date and pd.Timestamp(signal_date) < pd.Timestamp(target_date):
                reasons.append(f"pending_execution_date_unresolved:{signal_date}")
        if reasons:
            blocked_accounts.append({"account_id": item["account_id"], "reasons": reasons})
    runnable_count = max(len(active_accounts) - len(blocked_accounts), 0)
    if not data_result.ok or (blocked_accounts and runnable_count == 0):
        fleet_status = "blocked"
    elif blocked_accounts:
        fleet_status = "degraded"
    else:
        fleet_status = "ready"
    return ok_result(
        outputs={
            "status": fleet_status,
            "status_mode": "snapshot_only",
            "data": data,
            "registry": registry.summary(),
            "account_count": len(active_accounts) + len(archived_accounts),
            "active_account_count": len(active_accounts),
            "paused_account_count": len(paused_accounts),
            "retired_account_count": len(retired_accounts),
            "archived_account_count": len(archived_accounts),
            "accounts": active_accounts,
            "active_accounts": active_accounts,
            "paused_accounts": paused_accounts,
            "retired_accounts": retired_accounts,
            "manageable_accounts": active_accounts + paused_accounts,
            "archived_accounts": archived_accounts,
            "latest_fleet_run": latest,
            "blocked_accounts": blocked_accounts,
            "compact": compact,
            "operation_lock": _paper_operation_lock_status(),
            "paths": {"latest_status_file": str(FLEET_LATEST_STATUS_FILE)},
        },
        warnings=([] if data_result.ok else [data_result.err or "data_status_failed"])
        + [f"account_blocked:{item['account_id']}:{'|'.join(item['reasons'])}" for item in blocked_accounts],
        artifacts={"latest_status_file": str(FLEET_LATEST_STATUS_FILE)},
    )
