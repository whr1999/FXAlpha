from __future__ import annotations

import hashlib
import json
import pickle
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from .contracts import (
    DEFAULT_PORTFOLIO,
    LABEL_CONTRACT,
    LIMIT_THRESHOLD,
    MODEL_SYSTEM_VERSION,
    RoundGroup,
    experiment_signature,
    staged_seed_set,
    round_group_id_from,
    seed_policy,
    utc_now,
    validate_experiment_contract,
)
from .feature_sets import model_feature_set_preflight
from .paths import MODEL_RUNS_ROOT, QLIB0627_ROOT, ensure_model_dirs
from .state_store import ModelStateStore


DEFAULT_SEED_TIMEOUT_SECONDS = 1800


def _model_run_id(round_group_id: str, seed: int) -> str:
    digest = hashlib.sha256(f"{round_group_id}:{seed}".encode("utf-8")).hexdigest()[:8]
    return f"mrun_{round_group_id}_s{seed}_{digest}"


def _metrics_for_seed(seed: int, experiment: dict[str, Any]) -> dict[str, Any]:
    metrics_by_seed = experiment.get("metrics_by_seed") or {}
    direct = metrics_by_seed.get(str(seed)) or metrics_by_seed.get(seed)
    if isinstance(direct, dict):
        return dict(direct)
    return {
        "annualized_ret": 0.0,
        "excess_annualized_ret_with_cost": 0.0,
        "excess_information_ratio_with_cost": 0.0,
        "max_drawdown": 0.0,
        "rank_ic": 0.0,
        "rank_icir": 0.0,
        "turnover": 0.0,
    }


def _write_shadow_pickle(path: Path, payload: dict[str, Any]) -> None:
    with path.open("wb") as fh:
        pickle.dump(payload, fh)


def _seed_timeout_seconds(experiment: dict[str, Any]) -> int:
    debug = experiment.get("execution_debug") if isinstance(experiment.get("execution_debug"), dict) else {}
    raw = debug.get("seed_timeout_seconds") or os.environ.get("MODEL_SEED_TIMEOUT_SECONDS") or DEFAULT_SEED_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except Exception:
        value = DEFAULT_SEED_TIMEOUT_SECONDS
    return max(60, value)


def _tail_text(value: str, *, limit: int = 4000) -> str:
    text = value or ""
    return text[-limit:]


def _tail_file(path: Path, *, limit: int = 4000) -> str:
    """Read a bounded tail from a worker log without retaining all output in memory."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit), os.SEEK_SET)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _terminate_process_group(pid: int, *, force_after_seconds: float = 5.0) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    if force_after_seconds > 0:
        import time

        deadline = time.time() + force_after_seconds
        while time.time() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return True


def _run_direct_qlib_seed_isolated(
    *,
    feature_set_id: str,
    experiment: dict[str, Any],
    seed: int,
    run_dir: Path,
) -> dict[str, Any]:
    execution_dir = run_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    experiment_path = execution_dir / "experiment.json"
    result_path = execution_dir / "seed_worker_result.json"
    experiment_path.write_text(json.dumps(experiment, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if result_path.exists():
        result_path.unlink()
    timeout_s = _seed_timeout_seconds(experiment)
    env = os.environ.copy()
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "LIGHTGBM_NUM_THREADS"):
        env.setdefault(key, "1")
    cmd = [
        sys.executable,
        "-m",
        "domain.model.seed_worker",
        "--feature-set-id",
        feature_set_id,
        "--seed",
        str(int(seed)),
        "--run-dir",
        str(run_dir),
        "--experiment-json",
        str(experiment_path),
        "--result-json",
        str(result_path),
    ]
    stdout_path = execution_dir / "seed_worker.stdout.log"
    stderr_path = execution_dir / "seed_worker.stderr.log"
    with stdout_path.open("w", encoding="utf-8") as stdout_log, stderr_path.open("w", encoding="utf-8") as stderr_log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            text=True,
            stdout=stdout_log,
            stderr=stderr_log,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            process_group_killed = _terminate_process_group(proc.pid, force_after_seconds=0)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process_group_killed = _terminate_process_group(proc.pid, force_after_seconds=0) or process_group_killed
                proc.wait()
            payload = {
                "error": "seed_worker_timeout",
                "timeout_seconds": timeout_s,
                "stdout_tail": _tail_file(stdout_path),
                "stderr_tail": _tail_file(stderr_path),
                "process_group_killed": process_group_killed,
                "stdout_log": str(stdout_path),
                "stderr_log": str(stderr_path),
            }
            (execution_dir / "seed_worker_timeout.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return {"ok": False, **payload}
    stdout = _tail_file(stdout_path)
    stderr = _tail_file(stderr_path)
    process_group_cleaned = _terminate_process_group(proc.pid, force_after_seconds=1.0)
    result_payload: dict[str, Any] = {}
    if result_path.exists():
        try:
            result_payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            result_payload = {"ok": False, "error": f"seed_worker_result_unreadable:{exc}"}
    returncode = int(proc.returncode or 0)
    if returncode != 0:
        return {
            "ok": False,
            "error": result_payload.get("error") or f"seed_worker_failed:{returncode}",
            "returncode": returncode,
            "stdout_tail": _tail_text(stdout),
            "stderr_tail": _tail_text(stderr),
            "process_group_cleaned": process_group_cleaned,
            "result_payload": result_payload,
        }
    if not result_payload.get("ok"):
        return {
            "ok": False,
            "error": result_payload.get("error") or "seed_worker_missing_success_payload",
            "returncode": returncode,
            "stdout_tail": _tail_text(stdout),
            "stderr_tail": _tail_text(stderr),
            "process_group_cleaned": process_group_cleaned,
            "result_payload": result_payload,
        }
    result = result_payload.get("result")
    if not isinstance(result, dict):
        return {"ok": False, "error": "seed_worker_result_missing_result_dict", "result_payload": result_payload}
    result.setdefault("worker", {})
    result["worker"].update(
        {
            "isolated_subprocess": True,
            "isolated_process_group": True,
            "timeout_seconds": timeout_s,
            "returncode": returncode,
            "stdout_tail": _tail_text(stdout),
            "stderr_tail": _tail_text(stderr),
            "process_group_cleaned": process_group_cleaned,
        }
    )
    return {"ok": True, "result": result}


def submit_experiment(
    *,
    feature_set_id: str,
    experiment: dict[str, Any],
    state: ModelStateStore | None = None,
) -> dict[str, Any]:
    state = state or ModelStateStore()
    feature_preflight = model_feature_set_preflight(feature_set_id)
    if not feature_preflight.get("passed"):
        return {"ok": False, "stage": "feature_snapshot", "validation_result": feature_preflight}
    contract_result = validate_experiment_contract(experiment)
    if not contract_result["passed"]:
        return {"ok": False, "stage": "experiment_plan", "validation_result": contract_result}
    normalized = contract_result["normalized"]
    signature = experiment_signature({"feature_set_id": feature_set_id, **normalized})
    requested_round_id = str(experiment.get("round_group_id") or "")
    if requested_round_id:
        existing = state.get_round(requested_round_id)
        if existing:
            return {
                "ok": True,
                "round_group": existing,
                "validation_result": contract_result,
                "feature_set_preflight": feature_preflight,
                "reused_existing_round": True,
            }
    round_group_id = requested_round_id or round_group_id_from(feature_set_id, signature)
    if experiment.get("seed_set") is not None:
        return {
            "ok": False,
            "stage": "experiment_plan",
            "validation_result": {
                **contract_result,
                "passed": False,
                "errors": contract_result.get("errors", []) + ["seed_set_must_be_generated_by_submit_experiment"],
            },
        }
    seed_set = staged_seed_set(round_group_id, signature)
    if len(seed_set) != 3 or seed_set[0] != 42 or len(set(seed_set)) != 3:
        return {
            "ok": False,
            "stage": "experiment_plan",
            "validation_result": {
                **contract_result,
                "passed": False,
                "errors": contract_result.get("errors", []) + ["comparison_seed_set_must_be_three_unique_seeds_starting_with_42"],
            },
        }
    round_payload = RoundGroup(
        round_group_id=round_group_id,
        feature_set_id=feature_set_id,
        experiment_signature=signature,
        seed_set=seed_set,
        seed_policy=seed_policy(seed_set),
        experiment={**normalized, "feature_set_id": feature_set_id, "round_group_id": round_group_id},
        status="queued",
        stage="experiment_plan",
    ).to_dict()
    stored = state.upsert_round(round_payload)
    # Ordinary research rounds materialize only the screening seed.  The two
    # confirmation seeds are appended to the same round only for the session
    # best configuration.
    for seed in seed_set[:1]:
        state.upsert_seed_run(
            {
                "model_run_id": _model_run_id(round_group_id, int(seed)),
                "round_group_id": round_group_id,
                "seed": int(seed),
                "status": "queued",
                "metrics": {},
                "artifact_dir": str(MODEL_RUNS_ROOT / _model_run_id(round_group_id, int(seed))),
            }
        )
    return {
        "ok": True,
        "round_group": state.get_round(round_group_id),
        "validation_result": contract_result,
        "feature_set_preflight": feature_preflight,
    }


def run_round(
    *,
    round_group_id: str,
    state: ModelStateStore | None = None,
    execute_qlib: bool = False,
    seeds: list[int] | None = None,
    phase: str = "screening",
) -> dict[str, Any]:
    ensure_model_dirs()
    state = state or ModelStateStore()
    round_payload = state.get_round(round_group_id)
    if not round_payload:
        return {"ok": False, "err": "round_group_not_found", "round_group_id": round_group_id}
    if execute_qlib:
        if not QLIB0627_ROOT.exists():
            return {"ok": False, "err": "qlib0627_root_not_found", "expected_path": str(QLIB0627_ROOT)}
    experiment = dict(round_payload.get("experiment") or {})
    feature_preflight = model_feature_set_preflight(str(round_payload.get("feature_set_id") or ""))
    if not feature_preflight.get("passed"):
        return {
            "ok": False,
            "stage": "feature_snapshot",
            "round_group_id": round_group_id,
            "validation_result": feature_preflight,
        }
    planned = [int(seed) for seed in (round_payload.get("seed_set") or [])]
    requested = [int(seed) for seed in (seeds if seeds is not None else planned[:1])]
    invalid = sorted(set(requested) - set(planned))
    if invalid:
        return {"ok": False, "err": "seed_not_in_planned_panel", "invalid_seeds": invalid, "planned_seed_set": planned}
    seed_runs: list[dict[str, Any]] = []
    failed_seeds: list[dict[str, Any]] = []
    for seed in requested:
        model_run_id = _model_run_id(round_group_id, int(seed))
        run_dir = MODEL_RUNS_ROOT / model_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state.upsert_seed_run(
            {
                "model_run_id": model_run_id,
                "round_group_id": round_group_id,
                "seed": int(seed),
                "status": "running" if execute_qlib else "queued",
                "metrics": {},
                "artifact_dir": str(run_dir),
            }
        )
        if execute_qlib:
            worker_result = _run_direct_qlib_seed_isolated(
                feature_set_id=str(round_payload.get("feature_set_id") or ""),
                experiment=experiment,
                seed=int(seed),
                run_dir=run_dir,
            )
            if worker_result.get("ok") and isinstance(worker_result.get("result"), dict):
                direct_result = dict(worker_result.get("result") or {})
                metrics = dict(direct_result.get("metrics") or {})
                direct_error = "" if (direct_result.get("config_audit") or {}).get("passed") else str(
                    (direct_result.get("config_audit") or {}).get("backtest_error") or "direct_qlib_config_audit_failed"
                )
            else:
                metrics = {}
                direct_result = {
                    "error": str(worker_result.get("error") or "direct_qlib_worker_failed"),
                    "worker": {
                        key: value
                        for key, value in worker_result.items()
                        if key
                        in {
                            "timeout_seconds",
                            "stdout_tail",
                            "stderr_tail",
                            "returncode",
                            "result_payload",
                            "process_group_killed",
                            "process_group_cleaned",
                        }
                    },
                }
                direct_error = str(direct_result["error"])
                failed_seeds.append({"seed": int(seed), "model_run_id": model_run_id, "error": direct_error})
        else:
            metrics = _metrics_for_seed(int(seed), experiment)
            direct_result = {}
            direct_error = ""
        resolved_training_params = dict(experiment.get("qlib_model_kwargs") or {})
        resolved_training_params["seed"] = int(seed)
        resolved_portfolio = dict(experiment.get("portfolio") or DEFAULT_PORTFOLIO)
        resolved_processors = dict(experiment.get("qlib_processors") or {})
        direct_portfolio = dict(direct_result.get("resolved_portfolio_params") or {}) if isinstance(direct_result, dict) else {}
        direct_artifacts = dict(direct_result.get("artifacts") or {}) if isinstance(direct_result, dict) else {}
        if execute_qlib and direct_result.get("resolved_processors"):
            resolved_processors = dict(direct_result.get("resolved_processors") or resolved_processors)
        resolved_reweight = {
            "requested_sample_weight_policy": experiment.get("sample_weight_policy"),
            "effective_sample_weight_policy": experiment.get("effective_sample_weight_policy") or experiment.get("sample_weight_policy"),
            "sample_weight_kwargs": experiment.get("sample_weight_kwargs") or {},
        }
        if execute_qlib and direct_result.get("resolved_reweight_params"):
            resolved_reweight = dict(direct_result.get("resolved_reweight_params") or resolved_reweight)
        configured_segments = dict(experiment.get("segments") or {})
        direct_segments = direct_result.get("segments") if isinstance(direct_result, dict) and isinstance(direct_result.get("segments"), dict) else {}
        resolved_segments = direct_segments or configured_segments
        manifest = {
            "model_system_version": MODEL_SYSTEM_VERSION,
            "model_run_id": model_run_id,
            "round_group_id": round_group_id,
            "seed": int(seed),
            "feature_set_id": round_payload.get("feature_set_id"),
            "feature_set_preflight": feature_preflight,
            "experiment_signature": round_payload.get("experiment_signature"),
            "baseline_kind": experiment.get("baseline_kind"),
            "experiment": experiment,
            "resolved_training_params": resolved_training_params,
            "resolved_reweight_params": resolved_reweight,
            "resolved_portfolio_params": {
                "portfolio": resolved_portfolio,
                "benchmark": experiment.get("benchmark") or "000300sh",
                "deal_price": "open",
                "limit_threshold": list(LIMIT_THRESHOLD),
                "forbid_all_trade_at_limit": False,
                **{
                    key: value
                    for key, value in direct_portfolio.items()
                    if key in {"portfolio_artifacts"}
                },
            },
            "resolved_processors": resolved_processors,
            "resolved_windows": {
                "label": dict(LABEL_CONTRACT),
                "segments": resolved_segments,
                "train": resolved_segments.get("train") if isinstance(resolved_segments, dict) else None,
                "valid": resolved_segments.get("valid") if isinstance(resolved_segments, dict) else None,
                "test": resolved_segments.get("test") if isinstance(resolved_segments, dict) else None,
                "start_date": (
                    (resolved_segments.get("train") or [None, None])[0]
                    if isinstance(resolved_segments, dict) and isinstance(resolved_segments.get("train"), (list, tuple))
                    else experiment.get("start_date")
                ),
                "end_date": (
                    (resolved_segments.get("test") or [None, None])[1]
                    if isinstance(resolved_segments, dict) and isinstance(resolved_segments.get("test"), (list, tuple))
                    else experiment.get("end_date")
                ),
            },
            "config_audit": {
                "passed": not bool(direct_error),
                "shadow_runner": not bool(execute_qlib),
                "direct_qlib_adapter": bool(execute_qlib),
                "backtest_error": direct_error or None,
                "checks": [
                    "contract_guard_passed_before_run",
                    "feature_set_preflight_passed_before_run",
                    "processor_contract_recorded",
                    "portfolio_contract_recorded",
                    "no_pred_pre_shift",
                ],
            },
            "runner": {
                "main_chain": "direct_qlib0627_workflow",
                "qlib0627_root": str(QLIB0627_ROOT),
                "execute_qlib": bool(execute_qlib),
                "shadow_contract_runner": not bool(execute_qlib),
                "direct_qlib_error": direct_error,
                "note": (
                    "Direct qlib0627 adapter executed."
                    if execute_qlib and not direct_error
                    else "Direct qlib0627 adapter failed; see direct_qlib_error."
                    if execute_qlib
                    else "Shadow runner records the formal contract and accepts externally supplied metrics until full qrun execution is invoked."
                ),
            },
            "direct_qlib": direct_result,
            "artifacts": {
                "manifest": str(run_dir / "manifest.json"),
                "metrics": str(run_dir / "metrics.json"),
                "ret": str(run_dir / "ret.pkl"),
                "pred": str(run_dir / "pred.pkl"),
                **{
                    key: value
                    for key, value in direct_artifacts.items()
                    if key
                    in {
                        "label",
                        "params",
                        "model",
                        "feature_importance",
                        "training_diagnostics",
                        "portfolio",
                    }
                },
            },
            "generated_at": utc_now(),
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        (run_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        if not execute_qlib:
            _write_shadow_pickle(run_dir / "ret.pkl", {"shadow_artifact": True, "kind": "ret", "model_run_id": model_run_id, "metrics": metrics})
            _write_shadow_pickle(run_dir / "pred.pkl", {"shadow_artifact": True, "kind": "pred", "model_run_id": model_run_id, "seed": int(seed)})
            _write_shadow_pickle(run_dir / "holdings.pkl", {"shadow_artifact": True, "kind": "holdings", "model_run_id": model_run_id})
        seed_runs.append(
            state.upsert_seed_run(
                {
                    "model_run_id": model_run_id,
                    "round_group_id": round_group_id,
                    "seed": int(seed),
                    "status": "failed" if direct_error else "completed",
                    "metrics": metrics,
                    "artifact_dir": str(run_dir),
                }
            )
        )
    round_payload["status"] = "failed" if failed_seeds else "completed"
    policy = dict(round_payload.get("seed_policy") or {})
    executed = sorted({int(row.get("seed")) for row in state.list_seed_runs(round_group_id=round_group_id) if row.get("status") == "completed"})
    policy["executed_seed_set"] = executed
    policy["phase"] = phase
    round_payload["seed_policy"] = policy
    round_payload["stage"] = "research_confirmation" if phase == "confirmation" else "train_backtest_seed42"
    round_payload["updated_at"] = utc_now()
    state.upsert_round(round_payload)
    if failed_seeds:
        return {
            "ok": False,
            "err": "seed_run_failed",
            "stage": round_payload["stage"],
            "round_group": state.get_round(round_group_id),
            "seed_runs": seed_runs,
            "failed_seeds": failed_seeds,
        }
    return {"ok": True, "phase": phase, "round_group": state.get_round(round_group_id), "seed_runs": seed_runs}
