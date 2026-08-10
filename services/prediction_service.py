from __future__ import annotations

import json
import contextlib
import io
import logging
import threading
from datetime import datetime
import time
from typing import Any

from domain.trading.prediction import (
    ensure_factor_freshness,
    get_qlib_latest_calendar_date,
    init_qlib,
    resolve_prediction_model_context,
    update_pred_for_recorder,
    validate_pred_inputs,
)
from domain.trading.signals import build_target_portfolio, export_daily_score
from services._base import ServiceResult, err_result, ok_result
from storage.paths import LATEST_PREDICTION_STATUS_FILE, MODEL_DEFAULT_TOPK, SCORES_RUNTIME_ROOT, TARGETS_RUNTIME_ROOT

_PRED_STATUS_CACHE: dict[tuple[str | None, str | None], tuple[float, ServiceResult]] = {}
_PRED_STATUS_CACHE_SECONDS = 45.0
_PRED_STATUS_CACHE_LOCK = threading.Lock()
_PRED_STATUS_COMPUTE_LOCK = threading.Lock()


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return value


def _write_latest_status(payload: dict) -> None:
    LATEST_PREDICTION_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LATEST_PREDICTION_STATUS_FILE.write_text(
        json.dumps(_jsonable(payload), indent=2, ensure_ascii=False, default=str),
        encoding='utf-8',
    )


def pred_status_snapshot() -> ServiceResult:
    """Return the latest persisted prediction status without initializing Qlib.

    This is the safe status surface for GUI polling. Full ``pred_status`` keeps
    its live Qlib and feature validation semantics for explicit operational
    checks and preflight callers.
    """
    if not LATEST_PREDICTION_STATUS_FILE.exists():
        return err_result(
            "prediction_status_snapshot_missing",
            outputs={"status": "unknown", "source": "latest_prediction_status_file"},
            artifacts={"latest_status_file": str(LATEST_PREDICTION_STATUS_FILE)},
        )
    try:
        payload = json.loads(LATEST_PREDICTION_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return err_result(
            f"prediction_status_snapshot_unreadable:{exc}",
            outputs={"status": "unknown", "source": "latest_prediction_status_file"},
            artifacts={"latest_status_file": str(LATEST_PREDICTION_STATUS_FILE)},
        )
    outputs = dict(payload.get("outputs") or {})
    raw_status = str(outputs.get("status") or "")
    if raw_status in {"completed", "prepared"}:
        outputs["status"] = "ready"
        outputs["raw_status"] = raw_status
    elif not raw_status:
        outputs["status"] = "unknown"
    outputs["source"] = "latest_prediction_status_file"
    outputs["generated_at"] = payload.get("generated_at", "")
    return ok_result(
        inputs=payload.get("inputs") or {},
        outputs=outputs,
        artifacts={"latest_status_file": str(LATEST_PREDICTION_STATUS_FILE)},
    )




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

def _prediction_readiness(model_id: str | None = None, model_run_id: str | None = None) -> dict[str, Any]:
    try:
        model_context = resolve_prediction_model_context(model_id=model_id, model_run_id=model_run_id)
        run_context = {
            'model_id': model_context.get('model_id', ''),
            'model_run_id': model_context.get('model_run_id', ''),
            'feature_set_id': model_context.get('feature_set_id', ''),
            'status': model_context.get('status', ''),
            'source': model_context.get('source', ''),
            'recorder_run_dir': model_context.get('recorder_run_dir', ''),
            'platform_combined_factors_file': model_context.get('platform_combined_factors_file', ''),
            'platform_factor_latest_date': model_context.get('platform_factor_latest_date', ''),
        }
        ready = True
        err = ''
    except Exception as exc:
        run_context = None
        ready = False
        err = str(exc)
    return {
        'run_context': run_context,
        'score_root': {'path': str(SCORES_RUNTIME_ROOT), 'exists': SCORES_RUNTIME_ROOT.exists()},
        'target_root': {'path': str(TARGETS_RUNTIME_ROOT), 'exists': TARGETS_RUNTIME_ROOT.exists()},
        'ready_for_prediction': ready,
        'error': err,
    }


def pred_status(model_id: str | None = None, model_run_id: str | None = None) -> ServiceResult:
    cache_key = (model_id, model_run_id)
    with _PRED_STATUS_CACHE_LOCK:
        cached = _PRED_STATUS_CACHE.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] <= _PRED_STATUS_CACHE_SECONDS:
            return cached[1]

    # Qlib setup can consume many gigabytes and spawn a worker pool. Serialize
    # cache misses, then re-check the cache so concurrent status callers share
    # one computation instead of creating a cache stampede.
    with _PRED_STATUS_COMPUTE_LOCK:
        with _PRED_STATUS_CACHE_LOCK:
            cached = _PRED_STATUS_CACHE.get(cache_key)
            now = time.monotonic()
            if cached and now - cached[0] <= _PRED_STATUS_CACHE_SECONDS:
                return cached[1]
        try:
            model_context = resolve_prediction_model_context(model_id=model_id, model_run_id=model_run_id)
            _, init_stdout, init_stderr = _capture_noisy_call(init_qlib)
            qlib_latest = str(get_qlib_latest_calendar_date())
            factor_freshness = ensure_factor_freshness(model_context, qlib_latest, allow_rebuild=False)
            validation_target = None if factor_freshness.get('status') == 'feature_rebuild_required' else qlib_latest
            pred_input_validation = validate_pred_inputs(model_context, validation_target)
            outputs = {
                'status': 'ready' if factor_freshness.get('status') != 'feature_rebuild_required' else 'needs_feature_rebuild',
                'qlib_latest': qlib_latest,
                'run_context': {
                    'model_id': model_context.get('model_id', ''),
                    'model_run_id': model_context.get('model_run_id', ''),
                    'feature_set_id': model_context.get('feature_set_id', ''),
                    'status': model_context.get('status', ''),
                    'source': model_context.get('source', ''),
                    'recorder_run_dir': model_context.get('recorder_run_dir', ''),
                },
                'factor_freshness': factor_freshness,
                'pred_input_validation': pred_input_validation,
                'readiness': _prediction_readiness(model_id=model_id, model_run_id=model_run_id),
                'captured_logs': {'stdout': init_stdout, 'stderr': init_stderr},
            }
            result = ok_result(outputs=outputs, artifacts={'latest_status_file': str(LATEST_PREDICTION_STATUS_FILE)})
        except Exception as exc:
            result = err_result(str(exc), outputs={'status': 'blocked', 'readiness': _prediction_readiness(model_id=model_id, model_run_id=model_run_id)})
        with _PRED_STATUS_CACHE_LOCK:
            _PRED_STATUS_CACHE[cache_key] = (time.monotonic(), result)
        return result


def pred_update(*, model_id: str | None = None, model_run_id: str | None = None, to_date: str | None = None, from_date: str | None = None, dry_run: bool = False) -> ServiceResult:
    with _PRED_STATUS_CACHE_LOCK:
        _PRED_STATUS_CACHE.clear()
    inputs = {
        'model_id': model_id,
        'model_run_id': model_run_id,
        'to_date': to_date,
        'from_date': from_date,
        'dry_run': dry_run,
    }
    try:
        model_context = resolve_prediction_model_context(model_id=model_id, model_run_id=model_run_id)
        _, init_stdout, init_stderr = _capture_noisy_call(init_qlib)
        qlib_latest = str(get_qlib_latest_calendar_date())
        target_date = to_date or qlib_latest
        factor_freshness = ensure_factor_freshness(model_context, target_date, allow_rebuild=not dry_run)
        validation_target = None if factor_freshness.get('status') == 'feature_rebuild_required' else target_date
        pred_input_validation = validate_pred_inputs(model_context, validation_target)
        outputs = {
            'status': 'dry_run' if dry_run else 'prepared',
            'qlib_latest': qlib_latest,
            'target_date': target_date,
            'run_context': {
                'model_id': model_context.get('model_id', ''),
                'model_run_id': model_context.get('model_run_id', ''),
                'feature_set_id': model_context.get('feature_set_id', ''),
                'status': model_context.get('status', ''),
                'source': model_context.get('source', ''),
                'recorder_run_dir': model_context.get('recorder_run_dir', ''),
            },
            'factor_freshness': factor_freshness,
            'pred_input_validation': pred_input_validation,
            'captured_logs': {'stdout': init_stdout, 'stderr': init_stderr},
        }
        if dry_run:
            return ok_result(inputs=inputs, outputs=outputs, artifacts={'latest_status_file': str(LATEST_PREDICTION_STATUS_FILE)})

        result, update_stdout, update_stderr = _capture_noisy_call(update_pred_for_recorder,
            model_context['recorder_run_dir'],
            to_date=target_date,
            from_date=from_date,
            combined_factors_override=model_context.get('platform_combined_factors_file'),
        )
        outputs['status'] = 'completed'
        outputs['update'] = result
        outputs['captured_logs']['stdout'] += update_stdout
        outputs['captured_logs']['stderr'] += update_stderr
        payload = {
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'inputs': inputs,
            'outputs': outputs,
        }
        _write_latest_status(payload)
        return ok_result(inputs=inputs, outputs=outputs, artifacts={'latest_status_file': str(LATEST_PREDICTION_STATUS_FILE)})
    except Exception as exc:
        payload = {
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'inputs': inputs,
            'status': 'failed',
            'error': str(exc),
        }
        _write_latest_status(payload)
        return err_result(str(exc), inputs=inputs, outputs=payload, artifacts={'latest_status_file': str(LATEST_PREDICTION_STATUS_FILE)})


def score_export(*, model_id: str | None = None, model_run_id: str | None = None, as_of_date: str | None = None, topk: int | None = None) -> ServiceResult:
    inputs = {
        'model_id': model_id,
        'model_run_id': model_run_id,
        'as_of_date': as_of_date,
        'topk': topk,
    }
    try:
        meta, score_stdout, score_stderr = _capture_noisy_call(export_daily_score, model_id=model_id, model_run_id=model_run_id, as_of_date=as_of_date, topk=topk)
        return ok_result(inputs=inputs, outputs={'status': 'completed', 'score': meta, 'captured_logs': {'stdout': score_stdout, 'stderr': score_stderr}})
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)


def target_build(*, model_id: str | None = None, model_run_id: str | None = None, topk: int = MODEL_DEFAULT_TOPK, weighting: str = 'equal', total_capital: float | None = None) -> ServiceResult:
    inputs = {
        'model_id': model_id,
        'model_run_id': model_run_id,
        'topk': topk,
        'weighting': weighting,
        'total_capital': total_capital,
    }
    try:
        meta, target_stdout, target_stderr = _capture_noisy_call(build_target_portfolio, model_id=model_id, model_run_id=model_run_id, topk=topk, weighting=weighting, total_capital=total_capital)
        return ok_result(inputs=inputs, outputs={'status': 'completed', 'target': meta, 'captured_logs': {'stdout': target_stdout, 'stderr': target_stderr}})
    except Exception as exc:
        return err_result(str(exc), inputs=inputs)
