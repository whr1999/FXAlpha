"""
WQ BRAIN Submitter ? read active factors and submit them to QuantGPT/WQ BRAIN,
then persist submission results back into factor metadata.
"""

import logging
import os
import time
from datetime import datetime

import requests

from domain.factor_research.wq_expression import generate_wq_expression, normalize_wq_expression
from storage.factor_registry import FactorRegistry
from storage.paths import FACTOR_DEFAULT_UNIVERSE

logger = logging.getLogger(__name__)
_HTTP = requests.Session()
_HTTP.trust_env = False
_QGPT_URL = os.environ.get("QGPT_URL", "http://localhost:8003")


def _factor_metadata(factor: dict) -> dict:
    md = factor.get("metadata") or {}
    return md if isinstance(md, dict) else {}


def _persist_wq_result(factor_id: str, result: dict) -> None:
    reg = FactorRegistry()
    factor = reg.get(factor_id)
    if not factor:
        logger.warning("[wq] factor not found when persisting result: %s", factor_id)
        return
    metadata = _factor_metadata(factor).copy()
    metadata.setdefault("wq", {})
    metadata["wq"].update({
        "last_status": result.get("status"),
        "last_error": result.get("error"),
        "updated_at": datetime.now().isoformat(),
    })
    if result.get("status") == "completed":
        metadata["wq"].update({
            "alpha_id": result.get("alpha_id"),
            "rating": result.get("rating"),
            "submitted_at": datetime.now().isoformat(),
            "metrics": result.get("metrics") or {},
        })
    reg.update_meta(factor_id, metadata)


def _ensure_wq_expression(factor: dict) -> str | None:
    metadata = _factor_metadata(factor)
    wq_expression = metadata.get("wq_expression")
    if wq_expression:
        normalized = normalize_wq_expression(str(wq_expression))
        if normalized != str(wq_expression):
            reg = FactorRegistry()
            reg.update_meta(
                factor.get("factor_id", ""),
                {
                    **metadata,
                    "wq_expression": normalized,
                },
            )
        return normalized
    expression = factor.get("expression") or factor.get("name") or ""
    if not expression:
        return None
    try:
        generated = generate_wq_expression(str(expression))
    except Exception as exc:
        logger.warning("[wq] failed to generate WQ expression for %s: %s", factor.get("factor_id"), exc)
        return None
    if not generated:
        return None
    reg = FactorRegistry()
    reg.update_meta(
        factor.get("factor_id", ""),
        {
            **metadata,
            "wq_expression": generated,
        },
    )
    return generated


def submit_single_factor(
    expression: str,
    name: str = "",
    universe: str = FACTOR_DEFAULT_UNIVERSE,
    account: str = "primary",
    timeout: int = 600,
) -> dict:
    if not expression or not isinstance(expression, str):
        return {
            "status": "failed",
            "error": "missing_wq_expression",
            "alpha_id": None,
            "rating": None,
            "metrics": None,
        }
    url = f"{_QGPT_URL}/api/v1/wq-brain/submit"
    tag = f"fx-pipeline-{name}" if name else "fx-pipeline-auto"
    universe_map = {
        "hs300": "TOP500",
        "csi500": "TOP1000",
        "csi1000": "TOP2000",
        "csi2000": "TOP3000",
        "all_market": "TOP3000",
        "tradable_non_st": "TOP3000",
        "all_market_non_st": "TOP3000",
        "all": "TOP3000",
    }
    wq_universe = universe_map.get(universe, "TOP3000")
    payload = {
        "expression": expression,
        "tag": tag,
        "universe": wq_universe,
        "account": account,
    }

    try:
        resp = _HTTP.post(url, json=payload, timeout=30)
        if resp.status_code == 202:
            task_id = resp.json().get("task_id", "")
        else:
            return {"status": "failed", "error": f"API error {resp.status_code}: {resp.text[:200]}", "alpha_id": None, "rating": None, "metrics": None}
    except Exception as e:
        return {"status": "failed", "error": str(e), "alpha_id": None, "rating": None, "metrics": None}

    start = time.time()
    while time.time() - start < timeout:
        try:
            r = _HTTP.get(f"{_QGPT_URL}/api/v1/tasks/{task_id}", timeout=10)
            data = r.json()
            status = data.get("status", "")
            if status == "completed":
                result = data.get("result", {})
                alpha_id = result.get("alpha_id")
                interpretation = result.get("interpretation", {})
                rating = interpretation.get("rating", "?")
                metrics = {
                    "wq_sharpe": result.get("wq_sharpe"),
                    "wq_fitness": result.get("wq_fitness"),
                    "wq_returns": result.get("wq_returns"),
                    "wq_turnover": result.get("wq_turnover"),
                }
                return {"status": "completed", "alpha_id": alpha_id, "rating": rating, "metrics": metrics, "error": None}
            elif status == "failed":
                return {"status": "failed", "error": data.get("error", "WQ simulation failed"), "alpha_id": None, "rating": None, "metrics": None}
            time.sleep(5)
        except Exception:
            time.sleep(5)

    return {"status": "timeout", "error": "WQ BRAIN did not respond in time", "alpha_id": None, "rating": None, "metrics": None}


def submit_and_record_factor(
    factor_id: str,
    expression: str,
    name: str = "",
    universe: str = FACTOR_DEFAULT_UNIVERSE,
    account: str = "primary",
    timeout: int = 600,
) -> dict:
    result = submit_single_factor(expression, name=name, universe=universe, account=account, timeout=timeout)
    try:
        _persist_wq_result(factor_id, result)
    except Exception as exc:
        logger.warning("[wq] failed to persist WQ result for %s: %s", factor_id, exc)
    return result


def submit_all_active(universe: str = FACTOR_DEFAULT_UNIVERSE, min_icir: float = 0.3) -> dict:
    reg = FactorRegistry()
    active = reg.list_active(min_icir=min_icir)

    to_submit = []
    for f in active:
        if f.get("status") != "active":
            continue
        md = _factor_metadata(f)
        wq = md.get("wq") or {}
        if wq.get("alpha_id"):
            continue
        expr = md.get("wq_expression") or _ensure_wq_expression(f)
        if not expr:
            continue
        to_submit.append(f)

    if not to_submit:
        return {"submitted": 0, "skipped": len(active), "results": []}

    results = []
    for f in to_submit:
        md = _factor_metadata(f)
        expr = md.get("wq_expression") or _ensure_wq_expression(f) or ""
        name = f.get("name", "")
        factor_id = f.get("factor_id", "")
        logger.info(f"[wq] Submitting {name}: {expr[:50]}")
        result = submit_and_record_factor(factor_id, expr, name=name, universe=universe)
        if result["status"] == "completed":
            logger.info(f"[wq] ? {name}: alpha_id={result.get('alpha_id')} rating={result.get('rating')}")
        elif result["status"] == "failed":
            logger.warning(f"[wq] ? {name}: {result.get('error')}")
        else:
            logger.warning(f"[wq] ? {name}: timeout")
        results.append({"factor_id": factor_id, "factor_name": name, "expression": expr[:40], **result})

    return {
        "submitted": len(results),
        "skipped": len(active) - len(to_submit),
        "results": results,
        "missing_wq_expression": sum(
            1
            for f in active
            if f.get("status") == "active"
            and not (_factor_metadata(f).get("wq") or {}).get("alpha_id")
            and not _factor_metadata(f).get("wq_expression")
        ),
    }


def check_submission_status() -> dict:
    reg = FactorRegistry()
    active = reg.list_active()
    submitted = []
    for f in active:
        md = _factor_metadata(f)
        wq = md.get("wq") or {}
        if wq.get("alpha_id"):
            submitted.append({
                "factor_id": f.get("factor_id"),
                "name": f.get("name"),
                "alpha_id": wq.get("alpha_id"),
                "rating": wq.get("rating", "?"),
                "wq_sharpe": (wq.get("metrics") or {}).get("wq_sharpe"),
                "wq_fitness": (wq.get("metrics") or {}).get("wq_fitness"),
                "date": wq.get("submitted_at", ""),
            })
    return {"total_submitted": len(submitted), "factors": submitted}
