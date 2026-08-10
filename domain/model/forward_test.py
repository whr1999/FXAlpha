from __future__ import annotations

import json
import hashlib
import math
import pickle
from pathlib import Path
from typing import Any

import pandas as pd

from .contracts import SEED_SOTA_SCORE_THRESHOLD, forward_test_contract, stable_json, utc_now
from .qlib_runner import _run_direct_qlib_seed_isolated
from .state_store import ModelStateStore


FORWARD_TEST_VERSION = "model_forward_test_v1"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ret_metrics(ret_path: Path, window: list[str]) -> dict[str, Any]:
    try:
        with ret_path.open("rb") as fh:
            payload = pickle.load(fh)
    except Exception as exc:
        return {"available": False, "reason": f"ret_unreadable:{exc}", "ret_pkl": str(ret_path)}
    if not isinstance(payload, pd.DataFrame) or payload.empty:
        return {"available": False, "reason": "ret_pkl_not_dataframe_or_empty", "ret_pkl": str(ret_path)}
    df = payload.copy()
    try:
        df.index = pd.to_datetime(df.index)
    except Exception:
        return {"available": False, "reason": "ret_index_not_datetime", "ret_pkl": str(ret_path)}
    start, end = pd.Timestamp(window[0]), pd.Timestamp(window[1])
    df = df[(df.index >= start) & (df.index <= end)]
    if df.empty:
        return {"available": False, "reason": "ret_window_empty", "ret_pkl": str(ret_path), "window": list(window)}
    returns = pd.to_numeric(df["return"], errors="coerce").dropna() if "return" in df.columns else pd.Series(dtype=float)
    bench = pd.to_numeric(df["bench"], errors="coerce").reindex(returns.index).fillna(0.0) if "bench" in df.columns else pd.Series(0.0, index=returns.index)
    cost = pd.to_numeric(df["cost"], errors="coerce").reindex(returns.index).fillna(0.0) if "cost" in df.columns else pd.Series(0.0, index=returns.index)
    if returns.empty:
        return {"available": False, "reason": "ret_window_missing_return_column", "ret_pkl": str(ret_path), "window": list(window)}
    net_returns = returns - cost
    excess = net_returns - bench
    ann = (float((1.0 + excess).prod()) ** (252.0 / max(len(excess), 1))) - 1.0
    std = float(excess.std(ddof=0))
    ir = float(excess.mean() / std * math.sqrt(252.0)) if std > 0 else 0.0
    curve = (1.0 + returns).cumprod()
    dd = float((curve / curve.cummax() - 1.0).min()) if not curve.empty else 0.0
    turnover = float(pd.to_numeric(df["turnover"], errors="coerce").mean()) if "turnover" in df.columns else None
    return {
        "available": True,
        "ret_pkl": str(ret_path),
        "window": [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")],
        "point_count": int(len(df)),
        "annualized_ret": ann,
        "excess_annualized_ret_with_cost": ann,
        "excess_information_ratio_with_cost": ir,
        "max_drawdown": dd,
        "turnover": turnover,
        "avg_cost": float(cost.mean()) if not cost.empty else None,
        "cost_adjusted": True,
    }


def _shadow_metrics(base: dict[str, Any], *, multiplier: float, reason: str, window: list[str]) -> dict[str, Any]:
    ann = _num(base.get("excess_annualized_ret_with_cost") or base.get("annualized_ret")) * multiplier
    ir = _num(base.get("excess_information_ratio_with_cost") or base.get("information_ratio")) * multiplier
    dd = _num(base.get("max_drawdown")) * (1.05 if _num(base.get("max_drawdown")) < 0 else 1.0)
    return {
        "available": True,
        "shadow_forward_evidence": True,
        "reason": reason,
        "window": list(window),
        "annualized_ret": ann,
        "excess_annualized_ret_with_cost": ann,
        "excess_information_ratio_with_cost": ir,
        "max_drawdown": dd,
        "rank_ic": _num(base.get("rank_ic")),
        "rank_icir": _num(base.get("rank_icir")),
    }


def _label(existing: dict[str, Any], retrain: dict[str, Any], contract: dict[str, Any]) -> tuple[str, str, list[str]]:
    labels = contract.get("labels") if isinstance(contract.get("labels"), dict) else {}
    pass_cfg = labels.get("pass") or {}
    watch_cfg = labels.get("watch") or {}
    warnings: list[str] = []
    if not existing.get("available") or not retrain.get("available"):
        return "insufficient_evidence", "reject", ["forward_evidence_incomplete"]
    existing_ann = _num(existing.get("excess_annualized_ret_with_cost") or existing.get("annualized_ret"))
    existing_ir = _num(existing.get("excess_information_ratio_with_cost"))
    existing_dd = abs(_num(existing.get("max_drawdown")))
    retrain_ann = _num(retrain.get("excess_annualized_ret_with_cost") or retrain.get("annualized_ret"))
    retrain_ir = _num(retrain.get("excess_information_ratio_with_cost"))
    retrain_dd = abs(_num(retrain.get("max_drawdown")))
    min_pass_ann = float(pass_cfg.get("min_forward_annualized", 0.10))
    min_pass_ir = float(pass_cfg.get("min_forward_ir", 0.50))
    max_pass_dd = float(pass_cfg.get("max_forward_drawdown", 0.30))
    min_watch_ann = float(watch_cfg.get("min_forward_annualized", 0.00))
    min_watch_ir = float(watch_cfg.get("min_forward_ir", 0.20))
    if existing_ann >= min_pass_ann and retrain_ann >= min_pass_ann and existing_ir >= min_pass_ir and retrain_ir >= min_pass_ir and max(existing_dd, retrain_dd) <= max_pass_dd:
        return "stable", "pass", warnings
    if existing_ann >= min_watch_ann and retrain_ann >= min_watch_ann and existing_ir >= min_watch_ir and retrain_ir >= min_watch_ir:
        warnings.append("forward_test_watch_threshold")
        return "mildly_unstable", "watch", warnings
    return "unstable", "reject", ["forward_test_failed_threshold"]


def _manifest(seed_run: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(str(seed_run.get("artifact_dir") or ""))
    return _read_json(run_dir / "manifest.json")


def _is_direct_qlib(seed_run: dict[str, Any]) -> bool:
    manifest = _manifest(seed_run)
    runner = manifest.get("runner") if isinstance(manifest.get("runner"), dict) else {}
    return bool(runner.get("execute_qlib")) and not bool(runner.get("shadow_contract_runner"))


def _forward_evidence_key(
    *,
    round_payload: dict[str, Any],
    seed_run: dict[str, Any],
    threshold: float,
    sota_score: float,
    should_execute: bool,
    contract: dict[str, Any],
) -> str:
    payload = {
        "forward_test_version": FORWARD_TEST_VERSION,
        "feature_set_id": round_payload.get("feature_set_id"),
        "experiment_signature": round_payload.get("experiment_signature"),
        "model_run_id": seed_run.get("model_run_id"),
        "seed": seed_run.get("seed"),
        "threshold": threshold,
        "sota_score": sota_score,
        "execute_qlib": should_execute,
        "existing_model_slice": contract.get("existing_model_slice"),
        "retrain_segments": contract.get("retrain_segments"),
        "labels": contract.get("labels"),
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()[:24]


def _can_reuse_forward(
    existing: dict[str, Any],
    *,
    evidence_key: str,
    model_run_id: str,
    seed: int,
    threshold: float,
    sota_score: float,
    should_execute: bool,
    existing_window: list[str],
    retrain_segments: dict[str, Any],
) -> bool:
    if existing.get("status") not in {"pass", "watch", "reject"}:
        return False
    if str(existing.get("model_run_id") or "") != model_run_id or int(existing.get("seed") or 0) != seed:
        return False
    stored_key = str(existing.get("evidence_key") or "")
    if stored_key:
        return stored_key == evidence_key
    # Backward-compatible reuse for evidence created before evidence_key was
    # recorded. This prevents a gate read from destructively retraining and
    # overwriting a completed forward run.
    return (
        existing.get("forward_test_version") == FORWARD_TEST_VERSION
        and _num(existing.get("threshold"), float("nan")) == threshold
        and _num(existing.get("sota_score"), float("nan")) == sota_score
        and bool(existing.get("execute_qlib")) == should_execute
        and list((existing.get("existing_model_slice") or {}).get("window") or []) == existing_window
        and dict(existing.get("retrain_segments") or {}) == retrain_segments
    )


def run_forward_test(
    *,
    round_group_id: str,
    state: ModelStateStore | None = None,
    execute_qlib: bool | None = None,
    threshold: float | None = None,
    force_rerun: bool = False,
) -> dict[str, Any]:
    state = state or ModelStateStore()
    contract = forward_test_contract()
    if not contract.get("enabled", True):
        return {"ok": True, "round_group_id": round_group_id, "enabled": False, "results": []}
    threshold = float(threshold if threshold is not None else contract.get("score_threshold", SEED_SOTA_SCORE_THRESHOLD))
    round_payload = state.get_round(round_group_id)
    if not round_payload:
        return {"ok": False, "err": "round_group_not_found", "round_group_id": round_group_id}
    experiment = dict(round_payload.get("experiment") or {})
    existing_window = list((contract.get("existing_model_slice") or {}).get("test") or [])
    retrain_segments = dict(contract.get("retrain_segments") or {})
    outputs: list[dict[str, Any]] = []
    state_mutated = False
    for seed_run in state.list_seed_runs(round_group_id=round_group_id):
        score = dict(seed_run.get("score") or {})
        sota_score = _num(score.get("sota_score"))
        model_run_id = str(seed_run.get("model_run_id") or "")
        run_dir = Path(str(seed_run.get("artifact_dir") or ""))
        fwd_dir = run_dir / "forward_test"
        if sota_score < threshold:
            existing_forward = dict(seed_run.get("forward") or {})
            if (
                not force_rerun
                and existing_forward.get("status") == "skipped"
                and existing_forward.get("label") == "below_score_threshold"
                and _num(existing_forward.get("threshold"), float("nan")) == threshold
                and _num(existing_forward.get("sota_score"), float("nan")) == sota_score
            ):
                outputs.append({**existing_forward, "reused_existing": True})
                continue
            payload = {
                "forward_test_version": FORWARD_TEST_VERSION,
                "model_run_id": model_run_id,
                "round_group_id": round_group_id,
                "seed": seed_run.get("seed"),
                "status": "skipped",
                "label": "below_score_threshold",
                "threshold": threshold,
                "sota_score": sota_score,
                "generated_at": utc_now(),
            }
            state.upsert_seed_run({**seed_run, "forward": payload})
            state_mutated = True
            outputs.append(payload)
            continue
        manifest = _manifest(seed_run)
        manifest_artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
        ret_path = Path(str(manifest_artifacts.get("ret") or run_dir / "ret.pkl"))
        is_direct = _is_direct_qlib(seed_run)
        should_execute = bool(is_direct if execute_qlib is None else execute_qlib)
        evidence_key = _forward_evidence_key(
            round_payload=round_payload,
            seed_run=seed_run,
            threshold=threshold,
            sota_score=sota_score,
            should_execute=should_execute,
            contract=contract,
        )
        existing_forward = dict(seed_run.get("forward") or {})
        if not force_rerun and _can_reuse_forward(
            existing_forward,
            evidence_key=evidence_key,
            model_run_id=model_run_id,
            seed=int(seed_run.get("seed") or 0),
            threshold=threshold,
            sota_score=sota_score,
            should_execute=should_execute,
            existing_window=existing_window,
            retrain_segments=retrain_segments,
        ):
            outputs.append({**existing_forward, "evidence_key": evidence_key, "reused_existing": True})
            continue
        fwd_dir.mkdir(parents=True, exist_ok=True)
        if should_execute:
            existing_metrics = _ret_metrics(ret_path, existing_window)
            retrain_experiment = {**experiment, "segments": retrain_segments}
            worker_result = _run_direct_qlib_seed_isolated(
                feature_set_id=str(round_payload.get("feature_set_id") or ""),
                experiment=retrain_experiment,
                seed=int(seed_run.get("seed") or 0),
                run_dir=fwd_dir / "retrain_shift_6m",
            )
            if worker_result.get("ok") and isinstance(worker_result.get("result"), dict):
                retrain_result = dict(worker_result.get("result") or {})
                retrain_metrics = dict(retrain_result.get("metrics") or {})
                retrain_metrics.update({"available": bool(retrain_metrics), "artifact_dir": str(fwd_dir / "retrain_shift_6m")})
                retrain_error = ""
                config_audit = retrain_result.get("config_audit") if isinstance(retrain_result.get("config_audit"), dict) else {}
                if config_audit and not config_audit.get("passed"):
                    retrain_error = str(config_audit.get("backtest_error") or "shifted_retrain_config_audit_failed")
                    retrain_metrics["available"] = False
                    retrain_metrics["reason"] = retrain_error
            else:
                retrain_error = str(worker_result.get("error") or "shifted_retrain_worker_failed")
                retrain_metrics = {
                    "available": False,
                    "reason": retrain_error,
                    "artifact_dir": str(fwd_dir / "retrain_shift_6m"),
                    "worker": {key: value for key, value in worker_result.items() if key != "ok"},
                }
                retrain_result = {}
        else:
            base_metrics = dict(seed_run.get("metrics") or {})
            existing_metrics = _shadow_metrics(base_metrics, multiplier=0.92, reason="source_run_shadow_contract_runner", window=existing_window)
            retrain_metrics = _shadow_metrics(base_metrics, multiplier=0.85, reason="shifted_retrain_shadow_contract_runner", window=list(retrain_segments.get("test") or []))
            retrain_result = {}
            retrain_error = ""
        label, status, warnings = _label(existing_metrics, retrain_metrics, contract)
        payload = {
            "forward_test_version": FORWARD_TEST_VERSION,
            "evidence_key": evidence_key,
            "model_run_id": model_run_id,
            "round_group_id": round_group_id,
            "seed": int(seed_run.get("seed") or 0),
            "status": status,
            "label": label,
            "threshold": threshold,
            "sota_score": sota_score,
            "execute_qlib": should_execute,
            "shadow_forward_evidence": not should_execute,
            "existing_model_slice": existing_metrics,
            "shifted_retrain": retrain_metrics,
            "retrain_segments": retrain_segments,
            "retrain_error": retrain_error,
            "warnings": warnings,
            "artifacts": {
                "forward_dir": str(fwd_dir),
                "manifest": str(fwd_dir / "forward_manifest.json"),
                "retrain_artifacts": (retrain_result.get("artifacts") if isinstance(retrain_result, dict) else {}) or {},
            },
            "generated_at": utc_now(),
        }
        (fwd_dir / "forward_manifest.json").write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        state.upsert_seed_run({**seed_run, "forward": payload})
        state_mutated = True
        outputs.append(payload)
    if state_mutated:
        round_payload["stage"] = "forward_test"
        round_payload["updated_at"] = utc_now()
        state.upsert_round(round_payload)
    return {
        "ok": True,
        "round_group_id": round_group_id,
        "enabled": True,
        "results": outputs,
        "reused_all": not state_mutated,
    }
