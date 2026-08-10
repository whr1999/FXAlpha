from __future__ import annotations

import json
import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from services._base import ServiceResult, err_result, ok_result
from services.data_foundation_service import data_daily_routine, data_status
from services.model_service import model_production_status as model_production_status
from services.prediction_service import pred_status, pred_status_snapshot, pred_update
from services.trading_service import trading_daily_preflight, trading_daily_routine, trading_status
from domain.data_foundation.stock_metadata import load_stock_identity_map, security_name_for_instrument
from storage.paths import LATEST_DAILY_OPS_STATUS_FILE, MODEL_DEFAULT_TOPK


def _read_latest() -> dict[str, Any]:
    if not LATEST_DAILY_OPS_STATUS_FILE.exists():
        return {}
    try:
        return json.loads(LATEST_DAILY_OPS_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "unreadable", "error": str(exc), "path": str(LATEST_DAILY_OPS_STATUS_FILE)}


def _prediction_snapshot_status() -> ServiceResult:
    """Fast read-only prediction status for GUI status refreshes.

    The full pred_status() path can initialize Qlib and inspect feature
    freshness, which is appropriate for CLI/preflight gates but too heavy for a
    cockpit refresh. The daily-ops status page should explain the latest known
    state without accidentally doing production work.
    """
    return pred_status_snapshot()


def _csv_preview(path: str | None, *, limit: int = 20, tail: bool = False) -> list[dict[str, Any]]:
    if not path:
        return []
    csv_path = Path(path)
    if not csv_path.exists() or not csv_path.is_file():
        return []
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return []
    rows = rows[-limit:] if tail else rows[:limit]
    return [dict(row) for row in rows]


def _annotate_security_names(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows
    try:
        name_map = load_stock_identity_map()
    except Exception:
        name_map = {}
    annotated: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        instrument = item.get("instrument") or item.get("symbol")
        if instrument and not item.get("security_name"):
            item["security_name"] = security_name_for_instrument(str(instrument), name_map)
        annotated.append(item)
    return annotated


def _position_preview(path: str | None, ledger_path: str | None, *, limit: int = 30) -> list[dict[str, Any]]:
    rows = _annotate_security_names(_csv_preview(path, limit=limit))
    ledger_rows = _csv_preview(ledger_path, limit=1, tail=True)
    account_value = None
    if ledger_rows:
        try:
            account_value = float(ledger_rows[-1].get("ending_account_value") or 0)
        except Exception:
            account_value = None
    for row in rows:
        if "price" not in row and "last_price" in row:
            row["price"] = row.get("last_price")
        if account_value:
            try:
                row["weight"] = float(row.get("market_value") or 0) / account_value
            except Exception:
                row["weight"] = ""
    return rows


def _target_preview(recommendation: dict[str, Any], *, limit: int = 30) -> list[dict[str, Any]]:
    """Preview the latest target portfolio, not the currently executed holdings."""
    target_path = (
        recommendation.get("decision_file")
        or recommendation.get("portfolio_decision_file")
        or recommendation.get("target_file")
    )
    return _annotate_security_names(_csv_preview(target_path, limit=limit))


def _write_latest(payload: dict[str, Any]) -> None:
    LATEST_DAILY_OPS_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LATEST_DAILY_OPS_STATUS_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def daily_ops_status() -> ServiceResult:
    """Read-only status for the GUI cockpit.

    This intentionally does not run data update, prediction repair, or trading.
    It combines the last daily-ops checkpoint with current service states so the
    GUI can explain whether the next action is wait, investigate, dry-run, or
    manually execute from the advanced panel.
    """
    latest = _read_latest()
    data = data_status()
    model = model_production_status()
    pred = _prediction_snapshot_status()
    trade = trading_status(prediction=pred)
    outputs = {
        "status": latest.get("status") or "empty",
        "latest": latest,
        "latest_status_file": str(LATEST_DAILY_OPS_STATUS_FILE),
        "data": data.to_dict(),
        "model": model.to_dict(),
        "prediction": pred.to_dict(),
        "trade": trade.to_dict(),
        "summary": {
            "data_latest_dates": _dates(data),
            "production_model_id": _production_model_id(model, "model_id"),
            "production_model_run_id": _production_model_id(model, "model_run_id"),
            "production_validation_summary": _production_validation_summary(model, trade),
            "prediction_status": (pred.outputs or {}).get("status") if pred.ok else "blocked",
            "trade_status": (trade.outputs or {}).get("status") if trade.ok else "blocked",
            "pending_count": _pending_count(trade),
            "latest_recommendation": _latest_recommendation(trade),
            "latest_recommendation_quality": ((trade.outputs or {}).get("latest_recommendation_quality") or {}) if trade.ok else {},
            "latest_execution": _latest_execution(trade),
            "qlib_paper_account": ((trade.outputs or {}).get("qlib_paper_account") or {}) if trade.ok else {},
            "qlib_paper_account_history": ((trade.outputs or {}).get("qlib_paper_account_history") or []) if trade.ok else [],
            "qlib_paper_accounts": ((trade.outputs or {}).get("qlib_paper_accounts") or []) if trade.ok else [],
            "target_rows": _target_preview(_latest_recommendation(trade), limit=30),
            "paper_ledger_path": _ledger_path(trade),
            "ledger_rows": _csv_preview(_ledger_path(trade), limit=8, tail=True),
            "position_rows": _position_preview(((_latest_execution(trade).get("output_files") or {}).get("holdings_file")), _ledger_path(trade), limit=30),
            "trade_rows": _annotate_security_names(
                _csv_preview(((_latest_execution(trade).get("output_files") or {}).get("trades_file")), limit=30)
            ),
        },
    }
    warnings: list[str] = []
    if not latest:
        warnings.append("daily_ops_latest_status_missing")
    for name, result in [("data", data), ("model", model), ("prediction", pred), ("trade", trade)]:
        if not result.ok:
            warnings.append(f"{name}_status_unavailable:{result.err}")
    validation = _production_validation_summary(model, trade)
    for item in validation.get("hard_blocks") or []:
        warnings.append(f"production_validation:{item}")
    return ok_result(outputs=outputs, warnings=warnings, artifacts={"latest_status_file": str(LATEST_DAILY_OPS_STATUS_FILE)})


def _write_checkpoint(stage: str, commands_run: list[str], inputs: dict[str, Any], **extra: Any) -> None:
    payload = {
        "status": "running",
        "stage": stage,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "inputs": inputs,
        "commands_run": list(commands_run),
    }
    payload.update(extra)
    _write_latest(payload)


def _dates(data_result: ServiceResult) -> dict[str, Any]:
    snap = (data_result.outputs or {}).get("snapshot") or {}
    return {
        "hdf5": snap.get("latest_hdf5_trade_date"),
        "qlib": snap.get("latest_qlib_trade_date"),
        "quantgpt": snap.get("latest_quantgpt_trade_date"),
    }


def _date10(value: Any) -> str:
    text = str(value or "")
    return text[:10]


def _qlib_latest(data_result: ServiceResult, pred_result: ServiceResult, trade_result: ServiceResult) -> str:
    pred_latest = (pred_result.outputs or {}).get("qlib_latest") if pred_result.ok else ""
    trade_latest = (trade_result.outputs or {}).get("qlib_latest") if trade_result.ok else ""
    return _date10(pred_latest or trade_latest or _dates(data_result).get("qlib"))


def _production_model_id(model_result: ServiceResult, key: str) -> str:
    return str(((model_result.outputs or {}).get("production_model") or {}).get(key) or "")


def _production_validation_summary(model_result: ServiceResult, trade_result: ServiceResult | None = None) -> dict[str, Any]:
    if trade_result and trade_result.ok:
        summary = (trade_result.outputs or {}).get("production_validation_summary")
        if isinstance(summary, dict) and summary:
            return dict(summary)
    outputs = model_result.outputs or {}
    model = outputs.get("production_model") or {}
    validation = outputs.get("production_validation") or {}
    return {
        "status": validation.get("status") or outputs.get("status") or ("unavailable" if not model_result.ok else ""),
        "hard_blocks": list(validation.get("hard_blocks") or []),
        "warnings": list(validation.get("warnings") or []),
        "artifact_path": validation.get("artifact_path") or "",
        "production_model_id": model.get("model_id") or "",
        "production_model_run_id": model.get("model_run_id") or "",
    }


def _latest_recommendation(trade_result: ServiceResult) -> dict[str, Any]:
    return ((trade_result.outputs or {}).get("latest_recommendation") or {}) if trade_result.ok else {}


def _latest_execution(trade_result: ServiceResult) -> dict[str, Any]:
    return ((trade_result.outputs or {}).get("latest_execution") or {}) if trade_result.ok else {}


def _pending(trade_result: ServiceResult) -> list[dict[str, Any]]:
    return list(((trade_result.outputs or {}).get("pending_recommendations") or []) if trade_result.ok else [])


def _pending_summary(trade_result: ServiceResult) -> list[dict[str, Any]]:
    rows = []
    for rec in _pending(trade_result)[:20]:
        rows.append(
            {
                "recommendation_id": rec.get("recommendation_id"),
                "signal_date": rec.get("signal_date"),
                "execution_date": rec.get("execution_date"),
                "status": rec.get("status"),
                "topk": rec.get("topk"),
                "warnings": rec.get("warnings") or [],
            }
        )
    return rows


def _pending_count(trade_result: ServiceResult) -> int:
    registry = ((trade_result.outputs or {}).get("registry") or {}) if trade_result.ok else {}
    return int(registry.get("pending", len(_pending(trade_result))) or 0)


def _ledger_path(trade_result: ServiceResult) -> str:
    execution = _latest_execution(trade_result)
    return str((execution.get("output_files") or {}).get("ledger_file") or "")


def _data_quality_summary(data_result: ServiceResult) -> dict[str, Any]:
    return dict((data_result.outputs or {}).get("data_quality_summary") or {})


def _preflight_status(trade_preflight: ServiceResult | None) -> str:
    if not trade_preflight:
        return ""
    if not trade_preflight.ok:
        return "blocked"
    return str((trade_preflight.outputs or {}).get("status") or "blocked")


def _preflight_blockers(trade_preflight: ServiceResult | None) -> list[str]:
    if not trade_preflight:
        return []
    outputs = trade_preflight.outputs or {}
    blockers = outputs.get("blockers") or []
    if blockers:
        return [str(item) for item in blockers]
    if not trade_preflight.ok and trade_preflight.err:
        return [trade_preflight.err]
    return []


def _preflight_waiting_reason(trade_preflight: ServiceResult | None) -> str:
    if not trade_preflight:
        return ""
    return str((trade_preflight.outputs or {}).get("waiting_reason") or "")


def _repairable_prediction(pred_result: ServiceResult, trade_preflight: ServiceResult) -> bool:
    pre_outputs = trade_preflight.outputs or {}
    blockers = set(pre_outputs.get("blockers") or [])
    warnings = set(pre_outputs.get("warnings") or [])
    pred_status_value = (pred_result.outputs or {}).get("status") if pred_result.ok else "blocked"
    return (
        pred_status_value in {"needs_feature_rebuild", "blocked"}
        or "prediction_dry_run_failed" in blockers
        or "runtime_prediction_feature_cache_required" in warnings
    )


def _status_payload(
    *,
    status: str,
    data_before: ServiceResult,
    data_after: ServiceResult,
    model: ServiceResult,
    pred: ServiceResult,
    trade: ServiceResult,
    data_update: ServiceResult | None,
    trade_preflight: ServiceResult | None,
    execution_result: str,
    blocked_reason: str,
    commands_run: list[str],
    repaired_prediction: bool = False,
    trade_routine: ServiceResult | None = None,
    decision_status: str = "",
    waiting_reason: str = "",
    blockers: list[str] | None = None,
    trade_action: str = "",
) -> dict[str, Any]:
    latest_rec = _latest_recommendation(trade)
    latest_exec = _latest_execution(trade)
    blockers = blockers or []
    return {
        "status": status,
        "decision_status": decision_status or status,
        "waiting_reason": waiting_reason,
        "blockers": blockers,
        "trade_action": trade_action,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_update_result": ((data_update.outputs or {}).get("status") if data_update else ""),
        "data_update_reason": (data_update.err if data_update and not data_update.ok else ""),
        "data_latest_date_before": _dates(data_before),
        "data_latest_date_after": _dates(data_after),
        "data_quality_summary": _data_quality_summary(data_after),
        "qlib_latest": _qlib_latest(data_after, pred, trade),
        "production_model_id": _production_model_id(model, "model_id"),
        "production_model_run_id": _production_model_id(model, "model_run_id"),
        "prediction_status": (pred.outputs or {}).get("status") if pred.ok else "blocked",
        "latest_recommendation_id": latest_rec.get("recommendation_id"),
        "latest_recommendation_signal_date": latest_rec.get("signal_date"),
        "latest_recommendation_execution_date": latest_rec.get("execution_date"),
        "pending_count": _pending_count(trade),
        "pending_summary": _pending_summary(trade),
        "latest_qlib_paper_execution_status": latest_exec.get("status"),
        "execution_result": execution_result,
        "paper_ledger_path": _ledger_path(trade),
        "blocked_reason": blocked_reason,
        "commands_run": commands_run,
        "repaired_prediction": repaired_prediction,
        "data_update": data_update.to_dict() if data_update else None,
        "trade_preflight": trade_preflight.to_dict() if trade_preflight else None,
        "trade_routine": trade_routine.to_dict() if trade_routine else None,
        "latest_status_file": str(LATEST_DAILY_OPS_STATUS_FILE),
    }


def _data_update_ok(result: ServiceResult) -> bool:
    if result.ok:
        return True
    outputs = result.outputs or {}
    promote = outputs.get("promote") or {}
    return outputs.get("status") in {"already_current"} or (outputs.get("status") == "partial" and promote.get("status") == "already_promoted")


def daily_ops_routine(
    *,
    target_date: str | None = "auto",
    timeout_minutes: int = 180,
    topk: int = MODEL_DEFAULT_TOPK,
    total_capital: float = 1_000_000.0,
    dry_run: bool = False,
) -> ServiceResult:
    commands_run: list[str] = []
    inputs = {
        "target_date": target_date,
        "timeout_minutes": timeout_minutes,
        "topk": topk,
        "total_capital": total_capital,
        "dry_run": dry_run,
    }

    data_before = data_status()
    commands_run.append("python3 cli.py data-status")
    model = model_production_status()
    commands_run.append("python3 cli.py model-production")
    pred = pred_status()
    commands_run.append("python3 cli.py pred-status")
    trade = trading_status()
    commands_run.append("python3 cli.py trade-status")
    _write_checkpoint(
        "initial_status_collected",
        commands_run,
        inputs,
        data_latest_date_before=_dates(data_before),
        production_model_id=_production_model_id(model, "model_id"),
        production_model_run_id=_production_model_id(model, "model_run_id"),
        prediction_status=(pred.outputs or {}).get("status") if pred.ok else "blocked",
        pending_count=_pending_count(trade),
    )

    data_update = data_daily_routine(target_date=target_date, wait_idle=True, timeout_minutes=timeout_minutes, dry_run=dry_run)
    commands_run.append(f"python3 cli.py data-daily-routine --target-date {target_date or 'auto'} --timeout-minutes {timeout_minutes}")
    data_after = data_status()
    commands_run.append("python3 cli.py data-status")
    _write_checkpoint(
        "data_update_completed",
        commands_run,
        inputs,
        data_update=data_update.to_dict(),
        data_latest_date_after=_dates(data_after),
    )

    if not _data_update_ok(data_update):
        payload = _status_payload(
            status="blocked",
            data_before=data_before,
            data_after=data_after,
            model=model,
            pred=pred,
            trade=trade,
            data_update=data_update,
            trade_preflight=None,
            execution_result="not_run",
            blocked_reason=f"data_update_failed:{data_update.err or (data_update.outputs or {}).get('status')}",
            commands_run=commands_run,
            decision_status="blocked",
            blockers=[f"data_update_failed:{data_update.err or (data_update.outputs or {}).get('status')}"],
            trade_action="not_run",
        )
        _write_latest(payload)
        return err_result(payload["blocked_reason"], inputs=inputs, outputs=payload, artifacts={"latest_status_file": str(LATEST_DAILY_OPS_STATUS_FILE)})

    pred = pred_status()
    commands_run.append("python3 cli.py pred-status")
    trade = trading_status()
    commands_run.append("python3 cli.py trade-status")
    trade_preflight = trading_daily_preflight(topk=topk, total_capital=total_capital)
    commands_run.append(f"python3 cli.py trade-daily-preflight --topk {topk} --total-capital {int(total_capital)}")

    repaired_prediction = False
    qlib_latest = _qlib_latest(data_after, pred, trade)
    if _repairable_prediction(pred, trade_preflight) and model.ok:
        repair = pred_update(to_date=qlib_latest)
        repaired_prediction = True
        commands_run.append(f"python3 cli.py pred-update --to-date {qlib_latest}")
        _write_checkpoint(
            "prediction_repair_attempted",
            commands_run,
            inputs,
            prediction_repair=repair.to_dict(),
            qlib_latest=qlib_latest,
        )
        if not repair.ok:
            payload = _status_payload(
                status="blocked",
                data_before=data_before,
                data_after=data_after,
                model=model,
                pred=pred,
                trade=trade,
                data_update=data_update,
                trade_preflight=trade_preflight,
                execution_result="not_run",
                blocked_reason=f"prediction_repair_failed:{repair.err}",
                commands_run=commands_run,
                repaired_prediction=True,
            )
            payload["prediction_repair"] = repair.to_dict()
            _write_latest(payload)
            return err_result(payload["blocked_reason"], inputs=inputs, outputs=payload, artifacts={"latest_status_file": str(LATEST_DAILY_OPS_STATUS_FILE)})
        pred = pred_status()
        commands_run.append("python3 cli.py pred-status")
        trade = trading_status()
        commands_run.append("python3 cli.py trade-status")
        trade_preflight = trading_daily_preflight(topk=topk, total_capital=total_capital)
        commands_run.append(f"python3 cli.py trade-daily-preflight --topk {topk} --total-capital {int(total_capital)}")

    decision = _preflight_status(trade_preflight)
    waiting_reason = _preflight_waiting_reason(trade_preflight)
    blockers = _preflight_blockers(trade_preflight)

    if dry_run:
        payload = _status_payload(
            status="dry_run",
            data_before=data_before,
            data_after=data_after,
            model=model,
            pred=pred,
            trade=trade,
            data_update=data_update,
            trade_preflight=trade_preflight,
            execution_result="not_run",
            blocked_reason="dry_run",
            commands_run=commands_run,
            repaired_prediction=repaired_prediction,
            decision_status=decision,
            waiting_reason=waiting_reason,
            blockers=blockers,
            trade_action="not_run_dry_run",
        )
        _write_latest(payload)
        return ok_result(inputs=inputs, outputs=payload, artifacts={"latest_status_file": str(LATEST_DAILY_OPS_STATUS_FILE)})

    if decision == "waiting":
        payload = _status_payload(
            status="waiting",
            data_before=data_before,
            data_after=data_after,
            model=model,
            pred=pred,
            trade=trade,
            data_update=data_update,
            trade_preflight=trade_preflight,
            execution_result="not_run",
            blocked_reason=waiting_reason or "waiting",
            commands_run=commands_run,
            repaired_prediction=repaired_prediction,
            decision_status="waiting",
            waiting_reason=waiting_reason,
            blockers=[],
            trade_action="wait",
        )
        _write_latest(payload)
        return ok_result(inputs=inputs, outputs=payload, warnings=[waiting_reason or "waiting"], artifacts={"latest_status_file": str(LATEST_DAILY_OPS_STATUS_FILE)})

    if decision != "go":
        blockers = blockers or [trade_preflight.err if trade_preflight and trade_preflight.err else "trade_preflight_not_go"]
        payload = _status_payload(
            status="blocked",
            data_before=data_before,
            data_after=data_after,
            model=model,
            pred=pred,
            trade=trade,
            data_update=data_update,
            trade_preflight=trade_preflight,
            execution_result="not_run",
            blocked_reason=";".join(str(item) for item in blockers),
            commands_run=commands_run,
            repaired_prediction=repaired_prediction,
            decision_status="blocked",
            blockers=blockers,
            trade_action="not_run",
        )
        _write_latest(payload)
        return err_result(payload["blocked_reason"], inputs=inputs, outputs=payload, artifacts={"latest_status_file": str(LATEST_DAILY_OPS_STATUS_FILE)})

    qlib_latest = _qlib_latest(data_after, pred, trade)
    trade_routine = trading_daily_routine(signal_date=qlib_latest, topk=topk, total_capital=total_capital, ensure_pred_latest=False)
    commands_run.append(f"python3 cli.py trade-daily-routine --signal-date {qlib_latest} --topk {topk} --total-capital {int(total_capital)} --skip-pred-update")
    _write_checkpoint(
        "trade_routine_completed",
        commands_run,
        inputs,
        trade_routine=trade_routine.to_dict(),
        qlib_latest=qlib_latest,
    )
    trade_after = trading_status()
    commands_run.append("python3 cli.py trade-status")
    status = "completed" if trade_routine.ok else "blocked"
    if repaired_prediction and trade_routine.ok:
        status = "repaired_then_completed"
    elif trade_routine.ok:
        status = "data_updated_then_completed"
    payload = _status_payload(
        status=status,
        data_before=data_before,
        data_after=data_after,
        model=model,
        pred=pred,
        trade=trade_after,
        data_update=data_update,
        trade_preflight=trade_preflight,
        execution_result=(trade_routine.outputs or {}).get("status", "completed" if trade_routine.ok else "failed"),
        blocked_reason="" if trade_routine.ok else trade_routine.err,
        commands_run=commands_run,
        repaired_prediction=repaired_prediction,
        trade_routine=trade_routine,
        decision_status="completed" if trade_routine.ok else "blocked",
        blockers=[] if trade_routine.ok else [trade_routine.err or "trade_daily_routine_failed"],
        trade_action="executed_daily_routine" if trade_routine.ok else "failed_daily_routine",
    )
    _write_latest(payload)
    if not trade_routine.ok:
        return err_result(trade_routine.err or "trade_daily_routine_failed", inputs=inputs, outputs=payload, artifacts={"latest_status_file": str(LATEST_DAILY_OPS_STATUS_FILE)})
    return ok_result(inputs=inputs, outputs=payload, artifacts={"latest_status_file": str(LATEST_DAILY_OPS_STATUS_FILE)})
