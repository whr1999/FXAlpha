from __future__ import annotations

import json
import os
import shutil
from typing import Any
from pathlib import Path
from types import SimpleNamespace
import contextlib
import io
import logging
import urllib.request
from datetime import datetime

import pandas as pd

from domain.trading.prediction import resolve_prediction_model_context
from domain.trading.recommendation import (
    build_recommendation,
    has_qlib_trade_date,
    qlib_latest_trade_date,
    resolve_pending_execution_date,
)
from domain.trading.risk_policy import (
    build_risk_policy_history,
    load_risk_policy_config,
    risk_policy_config_hash,
    update_risk_policy_config,
)
from domain.trading.execution.qlib_paper import backfill_qlib_paper_account, run_qlib_paper_execution
from domain.data_foundation.stock_metadata import load_stock_identity_map, security_name_for_instrument
from services._base import ServiceResult, err_result, ok_result
from services.prediction_service import pred_status, pred_update, target_build
from storage.paths import (
    QLIB_DATA_ROOT,
    QUANTGPT_API_URL,
    RECOMMENDATIONS_RUNTIME_ROOT,
    RUNTIME_ROOT,
    SCORES_RUNTIME_ROOT,
    TARGETS_RUNTIME_ROOT,
    TRADING_EXECUTION_LOG_DB,
    TRADING_LATEST_STATUS_FILE,
    TRADING_RISK_LATEST_FILE,
    TRADING_RISK_POLICY_CONFIG_FILE,
    MODEL_DEFAULT_TOPK,
)
from storage.trading_registry import TradingRegistry


TRADING_LOCK_DIR = RUNTIME_ROOT / "trading" / "trading_update.lock"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except Exception:
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _lock_owner(lock_dir: Path | None = None) -> dict[str, Any]:
    lock_dir = lock_dir or TRADING_LOCK_DIR
    if not lock_dir.exists():
        return {}
    owner = _read_json_file(lock_dir / "owner.json")
    owner["lock_dir"] = str(lock_dir)
    owner["alive"] = _pid_alive(owner.get("pid"))
    return owner


def _release_trading_lock(lock_dir: Path | None = None) -> None:
    lock_dir = lock_dir or TRADING_LOCK_DIR
    shutil.rmtree(lock_dir, ignore_errors=True)


def _acquire_trading_lock(lock_dir: Path | None = None) -> dict[str, Any]:
    lock_dir = lock_dir or TRADING_LOCK_DIR
    warnings: list[str] = []
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    if lock_dir.exists():
        owner = _lock_owner(lock_dir)
        if owner and owner.get("alive", True):
            return {"acquired": False, "owner": owner, "warnings": warnings}
        _release_trading_lock(lock_dir)
        warnings.append("stale_trading_lock_reclaimed")
    try:
        lock_dir.mkdir()
    except FileExistsError:
        return {"acquired": False, "owner": _lock_owner(lock_dir), "warnings": warnings}
    owner = {
        "pid": os.getpid(),
        "started_at": _now_iso(),
        "lock_dir": str(lock_dir),
        "command": "trade-daily-routine",
    }
    (lock_dir / "owner.json").write_text(json.dumps(owner, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"acquired": True, "owner": owner, "warnings": warnings}


def _production_validation_summary(production: ServiceResult) -> dict[str, Any]:
    outputs = production.outputs or {}
    model = outputs.get("production_model") or {}
    validation = outputs.get("production_validation") or {}
    return {
        "status": validation.get("status") or outputs.get("status") or ("unavailable" if not production.ok else ""),
        "hard_blocks": list(validation.get("hard_blocks") or []),
        "warnings": list(validation.get("warnings") or []),
        "artifact_path": validation.get("artifact_path") or "",
        "production_model_id": model.get("model_id") or "",
        "production_model_run_id": model.get("model_run_id") or "",
    }


def _capture_noisy_call(fn, *args, **kwargs):
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    previous_disable = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            result = fn(*args, **kwargs)
    finally:
        logging.disable(previous_disable)
    return result, stdout_buf.getvalue(), stderr_buf.getvalue()


def _execution_payload(result, recommendation: dict[str, Any], status: str, err: str = "") -> dict[str, Any]:
    return {
        'execution_id': f"exec-{recommendation.get('recommendation_id', '')}-{recommendation.get('execution_date', '')}",
        'account_id': recommendation.get('account_id', recommendation.get('model_run_id', '')),
        'recommendation_id': recommendation.get('recommendation_id', ''),
        'model_id': recommendation.get('model_id', ''),
        'model_run_id': recommendation.get('model_run_id', ''),
        'trade_date': recommendation.get('execution_date', ''),
        'status': status,
        'adapter': getattr(result, 'adapter', '') if result is not None else '',
        'output_files': getattr(result, 'output_files', {}) if result is not None else {},
        'metrics': getattr(result, 'metrics', {}) if result is not None else {},
        'diagnostics': getattr(result, 'diagnostics', {}) if result is not None else {},
        'notes': getattr(result, 'notes', []) if result is not None else [],
        'error': err,
    }


def _account_snapshot_from_execution(result, recommendation: dict[str, Any]) -> dict[str, Any] | None:
    output_files = getattr(result, "output_files", {}) if result is not None else {}
    state_path = output_files.get("staged_account_state_file") or output_files.get("account_state_file", "")
    state_file = Path(state_path) if state_path else None
    if not state_file or not state_file.exists():
        return None
    snapshot = _read_json_file(state_file)
    if not snapshot:
        return None
    snapshot.setdefault("account_id", recommendation.get("account_id", recommendation.get("model_run_id", "")))
    snapshot.setdefault("model_run_id", recommendation.get("model_run_id", ""))
    snapshot.setdefault("trade_date", recommendation.get("execution_date", ""))
    snapshot.setdefault("source_recommendation_id", recommendation.get("recommendation_id", ""))
    return snapshot


def _record_account_snapshot_from_execution(registry: TradingRegistry, result, recommendation: dict[str, Any]) -> None:
    snapshot = _account_snapshot_from_execution(result, recommendation)
    if snapshot and hasattr(registry, "record_account_snapshot"):
        registry.record_account_snapshot(snapshot)


def _registry_latest_execution(registry: TradingRegistry, model_run_id: str | None = None) -> dict[str, Any] | None:
    if model_run_id:
        try:
            return registry.latest_execution(model_run_id)
        except TypeError:
            return registry.latest_execution()
    return registry.latest_execution()


def _account_snapshot_execution_view(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    output_files = snapshot.get("output_files") or {}
    return {
        "execution_id": f"account-{snapshot.get('account_id', '')}-{snapshot.get('trade_date', '')}",
        "recommendation_id": snapshot.get("source_recommendation_id", ""),
        "model_id": "",
        "model_run_id": snapshot.get("model_run_id", snapshot.get("account_id", "")),
        "trade_date": snapshot.get("trade_date", ""),
        "status": "completed",
        "adapter": "qlib_exchange_paper_account_snapshot",
        "output_files": output_files,
        "metrics": {
            "account_id": snapshot.get("account_id", ""),
            "ending_account_value": snapshot.get("account_value"),
            "account_value": snapshot.get("account_value"),
            "cash": snapshot.get("cash"),
            "stock_value": snapshot.get("stock_value"),
            "position_count": len(snapshot.get("positions") or {}),
            "score_hash": snapshot.get("score_hash", ""),
            "target_hash": snapshot.get("target_hash", ""),
            "fills_hash": snapshot.get("fills_hash", ""),
        },
        "diagnostics": {"source": "paper_account_snapshots"},
        "notes": ["derived from latest Qlib paper account snapshot"],
    }


def _latest_paper_execution_view(latest_execution: dict[str, Any] | None, latest_account: dict[str, Any] | None) -> dict[str, Any] | None:
    account_view = _account_snapshot_execution_view(latest_account)
    if not latest_execution:
        return account_view
    if not account_view:
        return latest_execution
    try:
        account_date = pd.Timestamp(account_view.get("trade_date"))
        execution_date = pd.Timestamp(latest_execution.get("trade_date"))
    except Exception:
        return latest_execution
    return account_view if account_date > execution_date else latest_execution


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


def _score_quality_from_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    score_path = Path(path)
    if not score_path.exists():
        return {"status": "missing_score_file", "score_file": str(score_path)}
    try:
        df = pd.read_csv(score_path, usecols=lambda col: col in {"instrument", "score"})
    except Exception as exc:
        return {"status": "unreadable_score_file", "score_file": str(score_path), "error": str(exc)}
    if "score" not in df.columns or df.empty:
        return {"status": "empty_score_file", "score_file": str(score_path), "record_count": int(len(df))}
    scores = pd.to_numeric(df["score"], errors="coerce")
    instruments = df.get("instrument", pd.Series(dtype=str)).astype(str)
    return {
        "status": "ok",
        "score_file": str(score_path),
        "record_count": int(len(df)),
        "non_null_score_count": int(scores.notna().sum()),
        "unique_score_count": int(scores.nunique(dropna=True)),
        "score_std": float(scores.std(skipna=True) or 0.0),
        "score_min": float(scores.min(skipna=True)) if scores.notna().any() else None,
        "score_max": float(scores.max(skipna=True)) if scores.notna().any() else None,
        "market_counts_top50": instruments.head(50).str[-2:].str.lower().value_counts().astype(int).to_dict(),
    }


def _recommendation_score_quality(rec: dict[str, Any] | None) -> dict[str, Any]:
    if not rec:
        return {}
    metrics = rec.get("metrics") or {}
    quality = metrics.get("score_quality") if isinstance(metrics, dict) else None
    return dict(quality or _score_quality_from_file(rec.get("score_file")))


def _csv_preview(path: str | None, *, limit: int = 20) -> list[dict[str, Any]]:
    if not path:
        return []
    file_path = Path(path)
    if not file_path.exists():
        return []
    try:
        return pd.read_csv(file_path).tail(limit).to_dict("records")
    except Exception:
        return []


def _latest_execution_summary(execution: dict[str, Any] | None) -> dict[str, Any]:
    if not execution:
        return {}
    output_files = execution.get("output_files") or {}
    return {
        "execution_id": execution.get("execution_id", ""),
        "recommendation_id": execution.get("recommendation_id", ""),
        "trade_date": execution.get("trade_date", ""),
        "status": execution.get("status", ""),
        "metrics": execution.get("metrics") or {},
        "output_files": output_files,
        "ledger_rows": _csv_preview(output_files.get("ledger_file"), limit=10),
        "position_rows": _annotate_security_names(_csv_preview(output_files.get("holdings_file"), limit=50)),
        "trade_rows": _annotate_security_names(_csv_preview(output_files.get("trades_file"), limit=50)),
    }


def _score_quality_warning(rec: dict[str, Any] | None, *, topk: int | None = None) -> str:
    quality = _recommendation_score_quality(rec)
    if not quality:
        return ""
    record_count = int(quality.get("record_count") or 0)
    unique_count = int(quality.get("unique_score_count") or 0)
    score_std = float(quality.get("score_std") or 0.0)
    expected_topk = int(topk or (rec or {}).get("topk") or MODEL_DEFAULT_TOPK)
    min_unique = min(max(expected_topk * 3, 20), max(record_count // 20, 1))
    if record_count >= max(expected_topk, 20) and (unique_count < min_unique or score_std <= 1e-12):
        return (
            "prediction score degenerate for latest recommendation; "
            f"unique_score_count={unique_count}, score_std={score_std:.3g}, required_unique>={min_unique}"
        )
    return ""


def _status_warnings(
    *,
    latest_recommendation: dict[str, Any] | None,
    pending_recommendations: list[dict[str, Any]],
    latest_execution: dict[str, Any] | None,
    prediction: ServiceResult,
) -> list[str]:
    warnings: list[str] = []
    if not prediction.ok:
        warnings.append('prediction status is blocked; recommendation generation may be stale')
    if prediction.ok and (prediction.outputs or {}).get('status') == 'needs_feature_rebuild':
        warnings.append('runtime prediction feature cache must be built before generating recommendations')
    pred_context = (prediction.outputs or {}).get('run_context') if prediction.ok else None
    pred_model_run_id = (pred_context or {}).get('model_run_id')
    if latest_recommendation and pred_model_run_id and latest_recommendation.get('model_run_id') != pred_model_run_id:
        warnings.append('production model changed after latest recommendation; regenerate prediction/recommendation')
    if pending_recommendations:
        warnings.append(f"{len(pending_recommendations)} pending recommendation(s) waiting for execution")
    if latest_recommendation and latest_recommendation.get('status') == 'failed':
        warnings.append('latest recommendation failed during execution')
    if latest_execution and latest_execution.get('status') == 'failed':
        warnings.append('latest qlib paper execution failed')
    score_warning = _score_quality_warning(latest_recommendation)
    if score_warning:
        warnings.append(score_warning)
    return warnings


def _proc_cmdlines(limit: int = 200) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return rows
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            if not raw:
                continue
            cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
            if not cmd:
                continue
            stat = (entry / "stat").read_text(encoding="utf-8", errors="ignore")
            parts = stat.split()
            ppid = int(parts[3]) if len(parts) > 3 else None
            cwd = os.readlink(entry / "cwd") if (entry / "cwd").exists() else ""
            rows.append({"pid": int(entry.name), "ppid": ppid, "cwd": cwd, "cmd": cmd})
            if len(rows) >= limit:
                break
        except Exception:
            continue
    return rows


def _active_fxalpha_processes() -> dict[str, Any]:
    keywords = [
        "quantgpt",
        "cli.py factor",
        "cli.py model",
        "factor_research",
        "model_run",
        "daily_data_update",
        "download_update",
        "convert_to_qlib",
        "api_server",
    ]
    current_pid = os.getpid()
    matches = []
    for row in _proc_cmdlines():
        cmd = row.get("cmd", "")
        if row.get("pid") == current_pid:
            continue
        if any(keyword in cmd for keyword in keywords):
            matches.append(row)
    return {"count": len(matches), "matches": matches[:30]}


def _quantgpt_health() -> dict[str, Any]:
    url = f"{QUANTGPT_API_URL}/api/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        payload = json.loads(body) if body else {}
        return {"ok": True, "url": url, "payload": payload}
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def _date10(value: Any) -> str:
    return str(value or "")[:10]


def _pending_waits_for_next_trade_date(trade: ServiceResult, signal_date: str) -> bool:
    latest = ((trade.outputs or {}).get("latest_recommendation") or {}) if trade.ok else {}
    if latest.get("status") != "pending":
        return False
    if _date10(latest.get("signal_date")) != _date10(signal_date):
        return False
    warnings = "|".join(str(item) for item in (latest.get("warnings") or []))
    execution_date = _date10(latest.get("execution_date"))
    return not execution_date or "execution_date_unresolved" in warnings or "execution_date_not_available_in_qlib" in warnings


def _pending_topk_mismatches(trade: ServiceResult, expected_topk: int) -> list[dict[str, Any]]:
    rows = ((trade.outputs or {}).get("pending_recommendations") or []) if trade.ok else []
    mismatches = []
    for rec in rows:
        rec_topk = int(rec.get("topk") or 0)
        if rec_topk and rec_topk != int(expected_topk):
            mismatches.append({
                "recommendation_id": rec.get("recommendation_id"),
                "signal_date": rec.get("signal_date"),
                "execution_date": rec.get("execution_date"),
                "topk": rec_topk,
            })
    return mismatches


def trading_daily_preflight(
    *,
    model_id: str | None = None,
    model_run_id: str | None = None,
    signal_date: str | None = None,
    topk: int = MODEL_DEFAULT_TOPK,
    total_capital: float = 1_000_000.0,
) -> ServiceResult:
    inputs = {
        "model_id": model_id,
        "model_run_id": model_run_id,
        "signal_date": signal_date,
        "topk": topk,
        "total_capital": total_capital,
    }
    blockers: list[str] = []
    warnings: list[str] = []
    try:
        from services.data_foundation_service import data_status
        from services.model_service import model_production_status as model_production_status

        data = data_status()
        production = model_production_status()
        production_validation = _production_validation_summary(production)
        pred_dry = pred_update(model_id=model_id, model_run_id=model_run_id, to_date=signal_date, dry_run=True)
        trade = trading_status(model_id=model_id, model_run_id=model_run_id)
        processes = _active_fxalpha_processes()
        qgpt = _quantgpt_health()

        if not data.ok:
            blockers.append("data_status_failed")
        elif ((data.outputs or {}).get("production_health") or {}).get("status") == "blocked":
            blockers.append("data_production_audit_failed")
        if not production.ok or (production.outputs or {}).get("status") != "ready":
            blockers.append("production_model_not_ready")
            if production_validation.get("hard_blocks"):
                blockers.extend([f"production_validation:{item}" for item in production_validation["hard_blocks"]])
        if not pred_dry.ok:
            blockers.append("prediction_dry_run_failed")
        if not trade.ok:
            blockers.append("trading_status_failed")
        elif ((trade.outputs or {}).get("registry") or {}).get("failed", 0):
            blockers.append("failed_recommendation_exists")
        elif (((trade.outputs or {}).get("latest_execution") or {}).get("status") == "failed"):
            blockers.append("latest_qlib_paper_execution_failed")

        pred_freshness = ((pred_dry.outputs or {}).get("factor_freshness") or {}) if pred_dry.ok else {}
        if pred_freshness.get("status") == "feature_rebuild_required":
            warnings.append("runtime_prediction_feature_cache_required")
        score_warning = _score_quality_warning((trade.outputs or {}).get("latest_recommendation") if trade.ok else None, topk=topk)
        if score_warning:
            blockers.append("latest_recommendation_score_degenerate")
            warnings.append(score_warning)

        signal = signal_date or str((pred_dry.outputs or {}).get("target_date") or (trade.outputs or {}).get("qlib_latest") or "")
        pending_topk_mismatches = _pending_topk_mismatches(trade, topk)
        if pending_topk_mismatches:
            blockers.append(f"pending_topk_mismatch_expected_{topk}")
        waiting_for_next_trade_date = bool(signal) and _pending_waits_for_next_trade_date(trade, signal[:10])
        if waiting_for_next_trade_date:
            warnings.append("waiting_for_next_trade_date")
        commands = {
            "preflight": "python3 cli.py trade-daily-preflight",
            "prediction_update": f"python3 cli.py pred-update --to-date {signal[:10]}" if signal else "python3 cli.py pred-update",
            "daily_routine": None if waiting_for_next_trade_date else (
                f"python3 cli.py trade-daily-routine --signal-date {signal[:10]} --topk {topk} --total-capital {int(total_capital)} --skip-pred-update"
                if signal else f"python3 cli.py trade-daily-routine --topk {topk} --total-capital {int(total_capital)}"
            ),
        }
        write_set = {
            "prediction_and_trading": {
                "prediction_feature_runtime": str(RUNTIME_ROOT / "trading" / "prediction_features"),
                "scores_runtime": str(SCORES_RUNTIME_ROOT),
                "targets_runtime": str(TARGETS_RUNTIME_ROOT),
                "recommendations_runtime": str(RECOMMENDATIONS_RUNTIME_ROOT),
                "qlib_paper_runtime": str(RUNTIME_ROOT / "trading" / "paper_trading"),
                "trading_registry_db": str(TRADING_EXECUTION_LOG_DB),
                "latest_trading_status": str(TRADING_LATEST_STATUS_FILE),
            },
        }
        outputs = {
            "status": "blocked" if blockers else ("waiting" if waiting_for_next_trade_date else "go"),
            "blockers": blockers,
            "waiting_reason": "waiting_for_next_trade_date" if waiting_for_next_trade_date else "",
            "pending_topk_mismatches": pending_topk_mismatches,
            "warnings": warnings + list(trade.warnings or []),
            "data": data.to_dict(),
            "production_model": production.to_dict(),
            "production_validation_summary": production_validation,
            "prediction_dry_run": pred_dry.to_dict(),
            "trading_status": trade.to_dict(),
            "processes": processes,
            "quantgpt_health": qgpt,
            "would_write": write_set,
            "recommended_commands": commands,
        }
        return ok_result(inputs=inputs, outputs=outputs, warnings=outputs["warnings"])
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def trading_status(
    *,
    model_id: str | None = None,
    model_run_id: str | None = None,
    prediction: ServiceResult | None = None,
    compact: bool = False,
) -> ServiceResult:
    inputs = {'model_id': model_id, 'model_run_id': model_run_id}
    registry = TradingRegistry()
    prediction = prediction or pred_status(model_id=model_id, model_run_id=model_run_id)
    try:
        from services.model_service import model_production_status as model_production_status

        production = model_production_status()
        production_validation = _production_validation_summary(production)
    except Exception as exc:
        production = err_result(str(exc))
        production_validation = {
            "status": "unavailable",
            "hard_blocks": [],
            "warnings": [],
            "artifact_path": "",
            "production_model_id": "",
            "production_model_run_id": "",
        }
    account_id = model_run_id or production_validation.get("production_model_run_id") or ""
    latest_recommendation = registry.latest_recommendation()
    pending = registry.pending_recommendations(limit=20)
    latest_execution = _registry_latest_execution(registry, account_id)
    latest_account = registry.latest_account_snapshot(account_id) if account_id and hasattr(registry, "latest_account_snapshot") else (
        registry.latest_account_snapshot() if hasattr(registry, "latest_account_snapshot") else None
    )
    effective_account_id = (latest_account or {}).get("account_id") or account_id
    account_history = (
        registry.list_account_snapshots(effective_account_id, limit=2 if compact else 260)
        if effective_account_id and hasattr(registry, "list_account_snapshots")
        else []
    )
    latest_accounts = registry.list_latest_accounts(limit=10 if compact else 50) if hasattr(registry, "list_latest_accounts") else []
    latest_paper_execution = _latest_paper_execution_view(latest_execution, latest_account)
    latest_execution_summary = _latest_execution_summary(latest_paper_execution)
    latest_orders = (
        registry.list_orders(latest_recommendation['recommendation_id'], limit=80)
        if latest_recommendation and not compact
        else []
    )
    latest_orders = _annotate_security_names(latest_orders)
    risk_policy_status = _risk_policy_snapshot(registry, latest_recommendation)
    recommendation_quality = _recommendation_score_quality(latest_recommendation)
    warnings = _status_warnings(
        latest_recommendation=latest_recommendation,
        pending_recommendations=pending,
        latest_execution=latest_execution,
        prediction=prediction,
    )
    blockers: list[str] = []
    production_status = str((production.outputs or {}).get("status") or "")
    if not production.ok or production_status != "ready":
        blockers.append("production_model_not_ready")
    for item in production_validation.get("hard_blocks") or []:
        blocker = f"production_validation:{item}"
        blockers.append(blocker)
        warnings.append(blocker)
    trade_status_value = (prediction.outputs or {}).get('status') if prediction.ok else 'blocked'
    if blockers:
        trade_status_value = "blocked"
    outputs = {
        'status': trade_status_value,
        'blocked_reason': ";".join(blockers),
        'blockers': blockers,
        'qlib_latest': (prediction.outputs or {}).get('qlib_latest') if prediction.ok else '',
        'registry': registry.summary(),
        'production_model': production.to_dict(),
        'production_validation_summary': production_validation,
        'prediction': prediction.to_dict(),
        'latest_recommendation': latest_recommendation,
        'latest_recommendation_quality': recommendation_quality,
        'pending_recommendations': pending,
        'latest_execution': latest_execution,
        'latest_qlib_paper_execution': latest_paper_execution,
        'qlib_paper_account': latest_account,
        'qlib_paper_account_history': account_history,
        'qlib_paper_accounts': latest_accounts,
        'latest_execution_summary': latest_execution_summary,
        'latest_orders': latest_orders,
        'risk_policy': risk_policy_status,
        'warnings': warnings,
        'paths': {
            'recommendations_root': str(RECOMMENDATIONS_RUNTIME_ROOT),
            'qlib_paper_root': str(RUNTIME_ROOT / 'trading' / 'paper_trading'),
            'latest_status_file': str(TRADING_LATEST_STATUS_FILE),
            'risk_policy_config_file': str(TRADING_RISK_POLICY_CONFIG_FILE),
            'latest_risk_decision_file': str(TRADING_RISK_LATEST_FILE),
        },
    }
    if compact:
        production_payload = production.to_dict()
        production_outputs = dict(production_payload.get("outputs") or {})
        selected_production = dict(production_outputs.get("production_model") or {})
        selected_metadata = dict(selected_production.get("metadata") or {})
        selected_metadata.pop("validation", None)
        if selected_metadata:
            selected_production["metadata"] = selected_metadata
        outputs["production_model"] = {
            "ok": production_payload.get("ok", production.ok),
            "outputs": {
                "status": production_outputs.get("status"),
                "production_model": selected_production,
                "count": production_outputs.get("count"),
            },
            "warnings": production_payload.get("warnings") or [],
        }
        outputs["compact"] = True
    return ok_result(inputs=inputs, outputs=outputs, warnings=warnings, artifacts={'latest_status_file': str(TRADING_LATEST_STATUS_FILE)})


def _risk_policy_snapshot(
    registry: TradingRegistry | None = None,
    latest_recommendation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = load_risk_policy_config()
    latest = _read_json_file(TRADING_RISK_LATEST_FILE) if TRADING_RISK_LATEST_FILE.exists() else {}
    if not latest_recommendation:
        registry = registry or TradingRegistry()
        latest_recommendation = registry.latest_recommendation()
    recommendation_decision = ((latest_recommendation or {}).get("metrics") or {}).get("risk_policy") or {}
    if recommendation_decision:
        latest = recommendation_decision
    return {
        "status": "enforced" if config.get("enabled") and config.get("mode") == "enforced" else (
            "shadow" if config.get("enabled") else "disabled"
        ),
        "config": config,
        "config_hash": risk_policy_config_hash(config),
        "latest_decision": latest,
        "latest_recommendation_id": (latest_recommendation or {}).get("recommendation_id", ""),
        "config_file": str(TRADING_RISK_POLICY_CONFIG_FILE),
        "latest_decision_file": str(TRADING_RISK_LATEST_FILE),
    }


def trading_risk_policy_status(*, account_id: str | None = None, history_days: int = 160) -> ServiceResult:
    inputs = {"account_id": account_id or "", "history_days": int(history_days)}
    try:
        registry = TradingRegistry()
        latest_recommendation = registry.latest_recommendation(account_id=account_id) if account_id else registry.latest_recommendation()
        effective_account_id = account_id or (latest_recommendation or {}).get("account_id", "")
        latest_account = (
            registry.latest_account_snapshot(effective_account_id)
            if effective_account_id and hasattr(registry, "latest_account_snapshot")
            else None
        )
        snapshot = _risk_policy_snapshot(registry, latest_recommendation)
        signal_date = (latest_recommendation or {}).get("signal_date") or (latest_account or {}).get("trade_date") or ""
        history: dict[str, Any] = {
            "as_of_date": signal_date,
            "market": [],
            "account": [],
            "caps": [],
            "thresholds": {},
            "method": "unavailable_without_signal_date",
            "service": "services.trading_service.trading_risk_policy_status",
        }
        if signal_date:
            account_history = registry.list_account_snapshots(effective_account_id, limit=520) if effective_account_id else []
            recommendations = registry.list_recommendations(
                account_id=effective_account_id or None,
                limit=max(260, int(history_days) * 3),
            )
            history = build_risk_policy_history(
                signal_date=signal_date,
                account_history=account_history,
                recommendation_history=recommendations,
                config=snapshot["config"],
                history_days=history_days,
            )
        return ok_result(
            inputs=inputs,
            outputs={
                **snapshot,
                "account_id": effective_account_id,
                "history": history,
                "history_generated_at": _now_iso(),
            },
        )
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def trading_risk_policy_update(changes: dict[str, Any]) -> ServiceResult:
    inputs = {"changes": changes}
    try:
        previous = load_risk_policy_config()
        updated = update_risk_policy_config(changes)
        return ok_result(
            inputs=inputs,
            outputs={
                "status": "updated",
                "previous": previous,
                "config": updated,
                "config_hash": risk_policy_config_hash(updated),
                "effective_scope": "next_generated_paper_recommendation",
                "config_file": str(TRADING_RISK_POLICY_CONFIG_FILE),
            },
            warnings=["existing_pending_recommendations_keep_their_frozen_risk_decision"],
            artifacts={"risk_policy_config_file": str(TRADING_RISK_POLICY_CONFIG_FILE)},
        )
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def trading_recommend(
    *,
    model_id: str | None = None,
    model_run_id: str | None = None,
    signal_date: str | None = None,
    execution_date: str | None = None,
    account_id: str | None = None,
    topk: int = MODEL_DEFAULT_TOPK,
    total_capital: float = 1_000_000.0,
    ensure_pred_latest: bool = True,
    strategy_contract_version: str = "top20_drop2_hold5_open_v1",
    n_drop: int = 2,
    hold_thresh: int = 5,
    deal_price: str = "open",
    run_kind: str = "on_time",
    data_package_id: str = "",
    identity_rows: pd.DataFrame | None = None,
    confidence_policy: dict[str, Any] | None = None,
    include_status_snapshot: bool = True,
) -> ServiceResult:
    inputs = {
        'model_id': model_id,
        'model_run_id': model_run_id,
        'signal_date': signal_date,
        'execution_date': execution_date,
        'account_id': account_id,
        'topk': topk,
        'total_capital': total_capital,
        'ensure_pred_latest': ensure_pred_latest,
    }
    try:
        model_context = resolve_prediction_model_context(
            model_id=model_id,
            model_run_id=model_run_id,
            require_production=True,
        )
        pred_result = None
        effective_signal_date = signal_date or qlib_latest_trade_date()
        if ensure_pred_latest:
            pred_result = pred_update(
                model_id=model_context['model_id'],
                model_run_id=model_context['model_run_id'],
                to_date=effective_signal_date,
            )
            if not pred_result.ok:
                return err_result('pred update failed before recommendation', inputs=inputs, outputs={'pred_update': pred_result.to_dict()})
        recommendation = build_recommendation(
            model_id=model_context['model_id'],
            model_run_id=model_context['model_run_id'],
            signal_date=effective_signal_date,
            execution_date=execution_date,
            account_id=account_id,
            topk=topk,
            total_capital=total_capital,
            strategy_contract_version=strategy_contract_version,
            n_drop=n_drop,
            hold_thresh=hold_thresh,
            deal_price=deal_price,
            run_kind=run_kind,
            data_package_id=data_package_id,
            identity_rows=identity_rows,
            confidence_policy=confidence_policy,
        )
        outputs = {
            'status': 'pending',
            'model_context': {
                'model_id': model_context['model_id'],
                'model_run_id': model_context['model_run_id'],
                'status': model_context.get('status', ''),
            },
            'pred_update': pred_result.to_dict() if pred_result else None,
            'recommendation': recommendation,
        }
        if include_status_snapshot:
            status = trading_status(model_id=model_context['model_id'], model_run_id=model_context['model_run_id'])
            outputs['status_snapshot'] = status.outputs
        return ok_result(inputs=inputs, outputs=outputs, warnings=recommendation.get('warnings', []))
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def _resolve_executable_recommendations(
    registry: TradingRegistry,
    recommendation_id: str | None,
    account_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if recommendation_id:
        candidates = [registry.get_recommendation(recommendation_id)]
    elif account_id:
        candidates = registry.pending_recommendations(limit=50, account_id=account_id)
    else:
        candidates = registry.pending_recommendations(limit=50)
    executable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for rec in [c for c in candidates if c]:
        if rec.get("status") != "pending":
            skipped.append({
                "recommendation_id": rec.get("recommendation_id"),
                "status": rec.get("status"),
                "reason": f"recommendation_not_pending:{rec.get('status')}",
            })
            continue
        try:
            execution_date = rec.get('execution_date') or resolve_pending_execution_date(rec.get('signal_date', ''))
        except Exception as exc:
            skipped.append({
                "recommendation_id": rec.get("recommendation_id"),
                "execution_date": rec.get("execution_date") or "",
                "reason": "execution_date_invalid",
                "error": str(exc),
            })
            continue
        if execution_date and execution_date != rec.get('execution_date'):
            registry.set_execution_date(rec['recommendation_id'], execution_date)
            rec['execution_date'] = execution_date
        if not execution_date:
            skipped.append({'recommendation_id': rec.get('recommendation_id'), 'reason': 'execution_date_unavailable'})
            continue
        try:
            has_trade_date = has_qlib_trade_date(execution_date)
        except Exception as exc:
            skipped.append({
                'recommendation_id': rec.get('recommendation_id'),
                'execution_date': execution_date,
                'reason': 'execution_date_invalid',
                'error': str(exc),
            })
            continue
        if not has_trade_date:
            skipped.append({'recommendation_id': rec.get('recommendation_id'), 'execution_date': execution_date, 'reason': 'execution_date_not_in_data'})
            continue
        executable.append(rec)
    return executable, skipped


def trading_execute_pending(
    *,
    recommendation_id: str | None = None,
    account_id: str | None = None,
    total_capital: float | None = None,
    include_status_snapshot: bool = True,
) -> ServiceResult:
    registry = TradingRegistry()
    resolved_account_id = str(account_id or "")
    if not resolved_account_id and recommendation_id:
        resolved_account_id = str((registry.get_recommendation(recommendation_id) or {}).get("account_id") or "")
    inputs = {'recommendation_id': recommendation_id, 'account_id': resolved_account_id, 'total_capital': total_capital}
    if not resolved_account_id:
        return err_result(
            "paper_account_id_required",
            inputs=inputs,
            outputs={"status": "blocked", "detail": "Global pending execution is retired; execute through paper_account_day_run."},
        )
    account = registry.get_account(resolved_account_id)
    if not account or account.get("status") != "active":
        return err_result(
            "paper_account_not_active",
            inputs=inputs,
            outputs={"status": "blocked", "account": account or {}, "detail": "Paused and retired account plans cannot execute."},
        )
    executed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    executable, skipped = _resolve_executable_recommendations(registry, recommendation_id, account_id=resolved_account_id)
    for rec in executable:
        execution_result = None
        try:
            execution_result, exec_stdout, exec_stderr = _capture_noisy_call(run_qlib_paper_execution,
                SimpleNamespace(
                    version_id=rec.get('account_id') or rec['model_run_id'],
                    trade_date=rec['execution_date'],
                    score_file=Path(rec['score_file']),
                    target_file=Path(rec['target_file']),
                    initial_capital=float(total_capital or rec.get('total_capital') or 1_000_000.0),
                    extra={
                        'recommendation_id': rec['recommendation_id'],
                        'model_id': rec.get('model_id', ''),
                        'model_run_id': rec.get('model_run_id', ''),
                        'account_id': rec.get('account_id', rec.get('model_run_id', '')),
                        'signal_date': rec.get('signal_date', ''),
                        'strategy_contract_version': rec.get('strategy_contract_version', ''),
                        'recommendation_metrics': rec.get('metrics') or {},
                    },
                ),
                topk=int(rec.get("topk") or MODEL_DEFAULT_TOPK),
                n_drop=int(rec.get("n_drop") or 2),
                hold_thresh=int(rec.get("hold_thresh") or 5),
                deal_price=str(rec.get("deal_price") or "open"),
            )
            status = 'completed' if execution_result.ok else 'failed'
            payload = _execution_payload(execution_result, rec, status=status)
            payload['notes'] = list(payload.get('notes') or []) + [
                f"captured_stdout_chars={len(exec_stdout)}",
                f"captured_stderr_chars={len(exec_stderr)}",
            ]
            if execution_result.ok:
                snapshot = _account_snapshot_from_execution(execution_result, rec)
                if not snapshot:
                    raise RuntimeError("execution_snapshot_missing")
            else:
                snapshot = None
            error = '' if execution_result.ok else 'qlib paper execution returned not ok'
            registry.commit_execution(
                execution=payload,
                recommendation_id=rec['recommendation_id'],
                recommendation_status='executed' if execution_result.ok else 'failed',
                snapshot=snapshot,
                error=error,
            )
            if execution_result.ok:
                executed.append({'recommendation_id': rec['recommendation_id'], 'execution': payload})
            else:
                failed.append({
                    'recommendation_id': rec.get('recommendation_id'),
                    'error': 'qlib paper execution returned not ok',
                    'execution': payload,
                })
        except Exception as exc:
            err = str(exc)
            if execution_result is not None and execution_result.ok:
                # The canonical Qlib state may already be published.  Keep the
                # recommendation pending so the next account run can reconcile
                # the durable frozen execution instead of executing it twice.
                failed.append({'recommendation_id': rec.get('recommendation_id'), 'error': f'registry_commit_interrupted:{err}'})
                continue
            payload = _execution_payload(None, rec, status='failed', err=err)
            registry.commit_execution(
                execution=payload,
                recommendation_id=rec['recommendation_id'],
                recommendation_status='failed',
                error=err,
            )
            failed.append({'recommendation_id': rec.get('recommendation_id'), 'error': err})
    outputs = {
        'status': 'completed' if not failed else 'partial_failed',
        'executed': executed,
        'failed': failed,
        'skipped': skipped,
    }
    if include_status_snapshot:
        status = trading_status()
        outputs['status_snapshot'] = status.outputs
    if failed:
        return err_result("paper_execution_failed", inputs=inputs, outputs=outputs)
    return ok_result(inputs=inputs, outputs=outputs, warnings=[item['reason'] for item in skipped])


def trading_supersede_recommendation(
    *,
    recommendation_id: str,
    reason: str = "",
) -> ServiceResult:
    inputs = {"recommendation_id": recommendation_id, "reason": reason}
    registry = TradingRegistry()
    rec = registry.get_recommendation(recommendation_id)
    if not rec:
        return err_result("recommendation_not_found", inputs=inputs)
    if rec.get("status") != "pending":
        return err_result(f"recommendation_not_pending:{rec.get('status')}", inputs=inputs, outputs={"recommendation": rec})
    registry.mark_recommendation(
        recommendation_id,
        status="superseded",
        error=reason or "superseded by newer production policy",
    )
    status = trading_status()
    return ok_result(
        inputs=inputs,
        outputs={
            "status": "superseded",
            "recommendation_id": recommendation_id,
            "reason": reason,
            "status_snapshot": status.outputs,
        },
    )


def trading_paper_backfill(
    *,
    model_id: str | None = None,
    model_run_id: str | None = None,
    target_date: str | None = None,
    total_capital: float = 1_000_000.0,
) -> ServiceResult:
    inputs = {
        "model_id": model_id,
        "model_run_id": model_run_id,
        "target_date": target_date,
        "total_capital": total_capital,
    }
    registry = TradingRegistry()
    try:
        context = resolve_prediction_model_context(model_id=model_id, model_run_id=model_run_id)
        version_id = context["model_run_id"]
        target = target_date or qlib_latest_trade_date()
        results = backfill_qlib_paper_account(
            version_id=version_id,
            target_date=target,
            initial_capital=float(total_capital),
        )
        executed = []
        failed = []
        for result in results:
            payload = {
                "execution_id": f"mtm-{version_id}-{result.trade_date}",
                "recommendation_id": "",
                "model_id": context.get("model_id", ""),
                "model_run_id": version_id,
                "trade_date": result.trade_date,
                "status": "completed" if result.ok else "failed",
                "adapter": result.adapter,
                "output_files": result.output_files,
                "metrics": result.metrics,
                "diagnostics": result.diagnostics,
                "notes": result.notes,
                "error": "" if result.ok else str((result.diagnostics or {}).get("reason", "mark_to_market_failed")),
            }
            if result.ok:
                _record_account_snapshot_from_execution(registry, result, {"model_run_id": version_id, "execution_date": result.trade_date})
                executed.append(payload)
            else:
                failed.append(payload)
        status = trading_status(model_id=context.get("model_id"), model_run_id=version_id)
        return ok_result(
            inputs=inputs,
            outputs={
                "status": "completed" if not failed else "partial_failed",
                "model_context": context,
                "target_date": target,
                "executed": executed,
                "failed": failed,
                "status_snapshot": status.outputs,
            },
            warnings=[] if executed else ["no_account_days_to_backfill"],
        )
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def _trading_daily_routine_unlocked(
    *,
    model_id: str | None = None,
    model_run_id: str | None = None,
    signal_date: str | None = None,
    topk: int = MODEL_DEFAULT_TOPK,
    total_capital: float = 1_000_000.0,
    ensure_pred_latest: bool = True,
) -> ServiceResult:
    inputs = {
        'model_id': model_id,
        'model_run_id': model_run_id,
        'signal_date': signal_date,
        'topk': topk,
        'total_capital': total_capital,
        'ensure_pred_latest': ensure_pred_latest,
    }
    registry = TradingRegistry()
    pending_rows = registry.pending_recommendations(limit=50)
    superseded_pending: list[dict[str, Any]] = []
    if pending_rows:
        try:
            current_context = resolve_prediction_model_context(model_id=model_id, model_run_id=model_run_id)
            current_model_run_id = current_context.get("model_run_id")
            mismatched_pending_model = [
                rec for rec in pending_rows
                if current_model_run_id and rec.get("model_run_id") and rec.get("model_run_id") != current_model_run_id
            ]
            if mismatched_pending_model:
                return err_result(
                    "pending_production_model_mismatch",
                    inputs=inputs,
                    outputs={
                        "status": "blocked",
                        "blocked_reason": "pending_production_model_mismatch",
                        "mismatched_pending": [
                            {
                                "recommendation_id": rec.get("recommendation_id"),
                                "signal_date": rec.get("signal_date"),
                                "execution_date": rec.get("execution_date"),
                                "model_run_id": rec.get("model_run_id"),
                                "current_model_run_id": current_model_run_id,
                            }
                            for rec in mismatched_pending_model
                        ],
                        "execute_pending": None,
                        "recommend": None,
                        "superseded_pending": [],
                    },
                    warnings=["pending_production_model_mismatch"],
                )
        except Exception:
            pass
    mismatched_pending = []
    for rec in pending_rows:
        rec_topk = int(rec.get("topk") or 0)
        if rec_topk and rec_topk != int(topk):
            mismatched_pending.append({
                "recommendation_id": rec.get("recommendation_id"),
                "signal_date": rec.get("signal_date"),
                "execution_date": rec.get("execution_date"),
                "topk": rec_topk,
            })
    if mismatched_pending:
        reason = f"pending_topk_mismatch_expected_{topk}"
        return err_result(reason, inputs=inputs, outputs={"mismatched_pending": mismatched_pending})

    execute_result = trading_execute_pending(total_capital=total_capital)
    execute_outputs = execute_result.outputs or {}
    execute_status = execute_outputs.get("status")
    skipped_pending = list(execute_outputs.get("skipped") or [])
    if (not execute_result.ok) or execute_status not in (None, "completed"):
        warnings = list(execute_result.warnings or [])
        if superseded_pending:
            warnings.append(f"superseded_stale_pending:{len(superseded_pending)}")
        outputs = {
            "status": "blocked",
            "blocked_reason": "pending_execution_failed",
            "execute_pending": execute_result.to_dict(),
            "recommend": None,
            "superseded_pending": superseded_pending,
        }
        return err_result("pending execution failed; recommendation generation skipped", inputs=inputs, outputs=outputs, warnings=warnings)
    if skipped_pending:
        warnings = list(execute_result.warnings or [])
        if superseded_pending:
            warnings.append(f"superseded_stale_pending:{len(superseded_pending)}")
        outputs = {
            "status": "waiting",
            "blocked_reason": "waiting_for_pending_execution_date",
            "execute_pending": execute_result.to_dict(),
            "recommend": None,
            "superseded_pending": superseded_pending,
        }
        return ok_result(inputs=inputs, outputs=outputs, warnings=warnings)
    recommend_result = trading_recommend(
        model_id=model_id,
        model_run_id=model_run_id,
        signal_date=signal_date,
        topk=topk,
        total_capital=total_capital,
        ensure_pred_latest=ensure_pred_latest,
    )
    outputs = {
        'status': 'completed' if execute_result.ok and recommend_result.ok else 'partial_failed',
        'execute_pending': execute_result.to_dict(),
        'recommend': recommend_result.to_dict(),
        'superseded_pending': superseded_pending,
    }
    warnings = list(execute_result.warnings or []) + list(recommend_result.warnings or [])
    if superseded_pending:
        warnings.append(f"superseded_stale_pending:{len(superseded_pending)}")
    if not recommend_result.ok:
        return err_result('daily routine failed while generating recommendation', inputs=inputs, outputs=outputs, warnings=warnings)
    return ok_result(inputs=inputs, outputs=outputs, warnings=warnings)


def trading_daily_routine(
    *,
    model_id: str | None = None,
    model_run_id: str | None = None,
    signal_date: str | None = None,
    topk: int = MODEL_DEFAULT_TOPK,
    total_capital: float = 1_000_000.0,
    ensure_pred_latest: bool = True,
) -> ServiceResult:
    inputs = {
        'model_id': model_id,
        'model_run_id': model_run_id,
        'signal_date': signal_date,
        'topk': topk,
        'total_capital': total_capital,
        'ensure_pred_latest': ensure_pred_latest,
    }
    try:
        from services.model_service import model_production_status as model_production_status

        production = model_production_status()
        production_validation = _production_validation_summary(production)
        if not production.ok or (production.outputs or {}).get("status") != "ready":
            return err_result(
                "production_model_not_ready",
                inputs=inputs,
                outputs={
                    "status": "blocked",
                    "blocked_reason": "production_model_not_ready",
                    "production_model": production.to_dict(),
                    "production_validation_summary": production_validation,
                },
                warnings=[f"production_validation:{item}" for item in production_validation.get("hard_blocks", [])],
            )
    except Exception as exc:
        return err_result(
            "production_model_status_unavailable",
            inputs=inputs,
            outputs={"status": "blocked", "blocked_reason": "production_model_status_unavailable", "error": str(exc)},
        )
    lock = _acquire_trading_lock()
    if not lock.get("acquired"):
        return err_result(
            "trading_update_lock_active",
            inputs=inputs,
            outputs={
                "status": "blocked",
                "blocked_reason": "trading_update_lock_active",
                "lock_owner": lock.get("owner") or {},
            },
            warnings=lock.get("warnings") or [],
        )
    try:
        result = _trading_daily_routine_unlocked(
            model_id=model_id,
            model_run_id=model_run_id,
            signal_date=signal_date,
            topk=topk,
            total_capital=total_capital,
            ensure_pred_latest=ensure_pred_latest,
        )
        lock_warnings = list(lock.get("warnings") or [])
        if lock_warnings:
            result.warnings.extend(lock_warnings)
        return result
    finally:
        _release_trading_lock()


def paper_trade_run(
    *,
    model_id: str | None = None,
    model_run_id: str | None = None,
    topk: int = MODEL_DEFAULT_TOPK,
    total_capital: float = 1_000_000.0,
    ensure_pred_latest: bool = True,
) -> ServiceResult:
    inputs = {
        'model_id': model_id,
        'model_run_id': model_run_id,
        'topk': topk,
        'total_capital': total_capital,
        'ensure_pred_latest': ensure_pred_latest,
    }
    try:
        model_context = resolve_prediction_model_context(model_id=model_id, model_run_id=model_run_id)
        pred_result = None
        if ensure_pred_latest:
            pred_result = pred_update(model_id=model_context['model_id'], model_run_id=model_context['model_run_id'])
            if not pred_result.ok:
                return err_result('pred update failed before paper trade', inputs=inputs, outputs={'pred_update': pred_result.to_dict()})
        target_result = target_build(model_id=model_context['model_id'], model_run_id=model_context['model_run_id'], topk=topk, total_capital=total_capital)
        if not target_result.ok:
            return err_result('target build failed before paper trade', inputs=inputs, outputs={'target': target_result.to_dict()})
        target_meta = target_result.outputs['target']

        execution_result, exec_stdout, exec_stderr = _capture_noisy_call(run_qlib_paper_execution,
            SimpleNamespace(
                version_id=model_context['model_run_id'],
                trade_date=target_meta['trade_date'],
                score_file=Path(target_meta['source_score_file']),
                target_file=Path(target_meta['target_file']),
                initial_capital=total_capital,
                extra={'model_id': model_context['model_id'], 'model_run_id': model_context['model_run_id']},
            )
        )
        if execution_result.ok:
            _record_account_snapshot_from_execution(
                TradingRegistry(),
                execution_result,
                {"model_run_id": model_context["model_run_id"], "execution_date": execution_result.trade_date},
            )
        return ok_result(inputs=inputs, outputs={
            'status': 'completed' if execution_result.ok else 'failed',
            'model_context': {
                'model_id': model_context['model_id'],
                'model_run_id': model_context['model_run_id'],
                'status': model_context['status'],
            },
            'pred_update': pred_result.to_dict() if pred_result else None,
            'target': target_meta,
            'captured_logs': {'stdout': exec_stdout, 'stderr': exec_stderr},
            'execution': {
                'ok': execution_result.ok,
                'adapter': execution_result.adapter,
                'trade_date': execution_result.trade_date,
                'output_files': execution_result.output_files,
                'metrics': execution_result.metrics,
                'notes': execution_result.notes,
                'diagnostics': execution_result.diagnostics,
            },
        })
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)
