from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from domain.factor_research.active_values_store import active_values_store_summary, current_active_registry_fingerprint
from domain.runtime_memory import release_process_memory
from services._base import err_result, ok_result
from storage.factor_registry import FactorRegistry
from storage.paths import FACTOR_ACTIVE_ADOPTED_VALUES_FILE, FACTOR_ADOPTED_VALUES_FILE, FACTOR_PARQUET_DIR, RUNTIME_ROOT


AUDIT_VERSION = "factor_library_audit_v4"
FACTOR_MAP_SCHEMA_VERSION = "factor_map_v1"
AUDIT_RUNTIME_DIR = RUNTIME_ROOT / "factor_audit"
AUDIT_REPORT_DIR = RUNTIME_ROOT / "reports" / "factor_audit"
QUALITY_AUDIT_DIR = AUDIT_RUNTIME_DIR / "quality"
INFORMATION_AUDIT_DIR = AUDIT_RUNTIME_DIR / "information"
LATEST_QUALITY_AUDIT_FILE = QUALITY_AUDIT_DIR / "latest.json"
LATEST_INFORMATION_AUDIT_FILE = INFORMATION_AUDIT_DIR / "latest.json"
LATEST_RUN_STATUS_FILE = AUDIT_RUNTIME_DIR / "run_status.json"

_RUN_LOCK = threading.Lock()
_RUN_THREAD: threading.Thread | None = None
_RUN_STATE: dict[str, Any] = {
    "status": "idle",
    "scope": "",
    "last_error": "",
    "last_requested_at": "",
    "last_started_at": "",
    "last_finished_at": "",
    "latest_quality_path": str(LATEST_QUALITY_AUDIT_FILE),
    "latest_information_path": str(LATEST_INFORMATION_AUDIT_FILE),
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _parse_metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            return json.loads(metadata)
        except Exception:
            return {}
    return metadata if isinstance(metadata, dict) else {}


def _nested_get(payload: dict[str, Any], *path: str) -> Any:
    cur: Any = payload
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _active_rows(status_filter: str) -> list[dict[str, Any]]:
    registry = FactorRegistry()
    if status_filter == "active":
        return registry.list_active(min_icir=-1e9)
    rows, _ = registry.list_all(status=status_filter or "all", limit=10000, min_icir=-1e9)
    return rows


def _active_values_mtime() -> float:
    if FACTOR_ACTIVE_ADOPTED_VALUES_FILE.exists():
        return FACTOR_ACTIVE_ADOPTED_VALUES_FILE.stat().st_mtime
    if FACTOR_ADOPTED_VALUES_FILE.exists():
        return FACTOR_ADOPTED_VALUES_FILE.stat().st_mtime
    return 0.0


def _audit_fingerprint(
    *,
    rows: list[dict[str, Any]],
    audit_type: str,
    status_filter: str,
    audit_window_start: str | None,
    audit_window_end: str | None,
    min_valid_days: int,
    min_common_stocks: int,
    redundancy_threshold_rank_p90: float,
    redundancy_threshold_pearson_p90: float,
    family_dependency_cut: float,
) -> dict[str, Any]:
    registry_fingerprint, active_records = current_active_registry_fingerprint()
    active_values_summary = active_values_store_summary()
    payload = {
        "audit_version": AUDIT_VERSION,
        "audit_type": audit_type,
        "status_filter": status_filter,
        "factor_ids": sorted(str(row.get("factor_id")) for row in rows),
        "factor_count": len(rows),
        "active_count": len([row for row in rows if row.get("status") == "active"]),
        "current_active_count": len(active_records),
        "registry_fingerprint": registry_fingerprint,
        "manifest_registry_fingerprint": active_values_summary.get("manifest_registry_fingerprint"),
        "active_values_stale": bool(active_values_summary.get("stale")),
        "active_values_mtime": _active_values_mtime(),
        "audit_window_start": audit_window_start,
        "audit_window_end": audit_window_end,
        "min_valid_days": min_valid_days,
        "min_common_stocks": min_common_stocks,
        "redundancy_threshold_rank_p90": redundancy_threshold_rank_p90,
        "redundancy_threshold_pearson_p90": redundancy_threshold_pearson_p90,
        "family_dependency_cut": family_dependency_cut,
    }
    cache_key = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    return {**payload, "cache_key": cache_key}


def _admission_score(row: dict[str, Any], metadata: dict[str, Any] | None = None) -> tuple[float | None, str]:
    metadata = metadata or _parse_metadata(row)
    candidates = [
        (_nested_get(metadata, "metrics", "deep_score"), "metadata.metrics.deep_score"),
        (_nested_get(metadata, "gate_result", "deep_score"), "metadata.gate_result.deep_score"),
        (metadata.get("deep_score"), "metadata.deep_score"),
        (metadata.get("quality_score"), "metadata.quality_score"),
        (_nested_get(metadata, "metrics", "score"), "metadata.metrics.score"),
        (metadata.get("score"), "metadata.score"),
        (_nested_get(metadata, "metrics", "quick_score"), "metadata.metrics.quick_score"),
        (metadata.get("quick_score"), "metadata.quick_score"),
    ]
    for value, source in candidates:
        try:
            if value is not None:
                return float(value), source
        except Exception:
            continue
    return None, "missing"


def _representative_score(row: dict[str, Any]) -> tuple[float, str, str]:
    score, source = _admission_score(row)
    if score is not None:
        return score, source, str(row.get("created_at") or "")
    return -1.0, "missing_score_created_at_fallback", str(row.get("created_at") or "")


def _factor_column_candidates(row: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    candidates = [
        metadata.get("data_column"),
        row.get("expression"),
        metadata.get("wq_expression"),
        metadata.get("expression"),
        metadata.get("factor_expression"),
        row.get("name"),
        row.get("factor_id"),
    ]
    seen: set[str] = set()
    out: list[str] = []
    for item in candidates:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _adopted_value_columns() -> set[str]:
    values_file = FACTOR_ACTIVE_ADOPTED_VALUES_FILE if FACTOR_ACTIVE_ADOPTED_VALUES_FILE.exists() else FACTOR_ADOPTED_VALUES_FILE
    if not values_file.exists():
        return set()
    try:
        import pyarrow.parquet as pq

        return set(pq.ParquetFile(values_file).schema.names)
    except Exception:
        return set(pd.read_parquet(values_file).columns)


def _individual_factor_exists(metadata: dict[str, Any]) -> bool:
    data_path = metadata.get("data_path")
    if data_path and Path(data_path).exists():
        return True
    data_column = metadata.get("data_column")
    return bool(data_column and any(FACTOR_PARQUET_DIR.glob(f"*{data_column}*.parquet")))


def _validate_audit_inputs(
    *,
    scope: str,
    min_valid_days: int,
    min_common_stocks: int,
    redundancy_threshold_rank_p90: float,
    redundancy_threshold_pearson_p90: float,
    family_dependency_cut: float,
) -> list[str]:
    errors: list[str] = []
    if scope not in {"quality", "information", "all"}:
        errors.append("scope must be one of: quality, information, all")

    try:
        valid_days = int(min_valid_days)
        if valid_days < 0:
            errors.append("min_valid_days must be >= 0")
    except (TypeError, ValueError):
        errors.append("min_valid_days must be an integer >= 0")

    try:
        common_stocks = int(min_common_stocks)
        if common_stocks <= 0:
            errors.append("min_common_stocks must be > 0")
    except (TypeError, ValueError):
        errors.append("min_common_stocks must be an integer > 0")

    threshold_values = {
        "redundancy_threshold_rank_p90": redundancy_threshold_rank_p90,
        "redundancy_threshold_pearson_p90": redundancy_threshold_pearson_p90,
        "family_dependency_cut": family_dependency_cut,
    }
    for name, value in threshold_values.items():
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            errors.append(f"{name} must be a number between 0 and 1")
            continue
        if not math.isfinite(numeric) or not 0 <= numeric <= 1:
            errors.append(f"{name} must be between 0 and 1")
    return errors


def _latest_path(audit_type: str) -> Path:
    return LATEST_QUALITY_AUDIT_FILE if audit_type == "quality" else LATEST_INFORMATION_AUDIT_FILE


def _load_latest_audit(audit_type: str) -> dict[str, Any] | None:
    path = _latest_path(audit_type)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _freshness_payload(latest: dict[str, Any] | None, *, audit_type: str | None = None) -> dict[str, Any]:
    try:
        current_fingerprint, active_records = current_active_registry_fingerprint()
        active_values_summary = active_values_store_summary()
    except Exception as exc:
        return {
            "stale": True,
            "stale_reason": f"freshness_check_failed:{exc}",
            "current_active_count": None,
            "current_registry_fingerprint": "",
            "active_values_stale": True,
        }
    if not latest:
        return {
            "stale": True,
            "stale_reason": f"latest_{audit_type or 'audit'}_audit_missing",
            "current_active_count": len(active_records),
            "current_registry_fingerprint": current_fingerprint,
            "active_values_stale": bool(active_values_summary.get("stale")),
        }
    summary = latest.get("summary") or {}
    audit_fp = latest.get("audit_fingerprint") or {}
    reasons: list[str] = []
    if str(latest.get("audit_version") or "") != AUDIT_VERSION:
        reasons.append("audit_version_mismatch")
    if audit_type and str(latest.get("audit_type") or "") != audit_type:
        reasons.append("audit_type_mismatch")
    if not audit_fp:
        reasons.append("audit_fingerprint_missing")
    if str(audit_fp.get("registry_fingerprint") or "") != str(current_fingerprint):
        reasons.append("active_registry_fingerprint_mismatch")
    if int(summary.get("factor_count") or -1) != int(audit_fp.get("factor_count") or -2):
        reasons.append("summary_fingerprint_factor_count_mismatch")
    if str(audit_fp.get("status_filter") or "active") == "active" and int(summary.get("factor_count") or -1) != int(len(active_records)):
        reasons.append("active_factor_count_mismatch")
    manifest_fp = str(active_values_summary.get("manifest_registry_fingerprint") or "")
    if str(audit_fp.get("manifest_registry_fingerprint") or "") != manifest_fp:
        reasons.append("active_values_manifest_fingerprint_mismatch")
    return {
        "stale": bool(reasons),
        "stale_reason": ",".join(reasons),
        "current_active_count": len(active_records),
        "current_registry_fingerprint": current_fingerprint,
        "active_values_stale": bool(active_values_summary.get("stale")),
        "active_values_stale_reason": str(active_values_summary.get("stale_reason") or ""),
        "manifest_registry_fingerprint": manifest_fp,
    }


def _with_freshness(payload: dict[str, Any], *, audit_type: str | None = None) -> dict[str, Any]:
    out = json.loads(json.dumps(_jsonable(payload), ensure_ascii=False))
    summary = dict(out.get("summary") or {})
    freshness = _freshness_payload(out, audit_type=audit_type)
    summary.update(freshness)
    out["summary"] = summary
    return out


def _information_governance_ready(latest: dict[str, Any] | None) -> tuple[bool, dict[str, Any], dict[str, Any] | None]:
    if not latest:
        return False, _freshness_payload(None, audit_type="information"), None
    payload = _with_freshness(latest, audit_type="information")
    summary = payload.get("summary") or {}
    ready = bool(not summary.get("stale") and payload.get("audit_type") == "information")
    return ready, summary, payload


def _write_run_state(state: dict[str, Any]) -> None:
    AUDIT_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_jsonable(state), ensure_ascii=False, indent=2)
    tmp_path = LATEST_RUN_STATUS_FILE.with_name(f"{LATEST_RUN_STATUS_FILE.name}.tmp.{threading.get_ident()}")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(LATEST_RUN_STATUS_FILE)


def _load_run_state() -> dict[str, Any]:
    if LATEST_RUN_STATUS_FILE.exists():
        try:
            disk = json.loads(LATEST_RUN_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            disk = {}
    else:
        disk = {}
    with _RUN_LOCK:
        running = bool(_RUN_THREAD and _RUN_THREAD.is_alive())
        state = {**_RUN_STATE, **disk}
        if running:
            state.update({k: v for k, v in _RUN_STATE.items() if v})
            state["status"] = "running"
        state["_thread_alive"] = running
        return state


def _set_run_state(**updates: Any) -> dict[str, Any]:
    with _RUN_LOCK:
        _RUN_STATE.update(updates)
        state = dict(_RUN_STATE)
    _write_run_state(state)
    return state


def _match_columns(rows: list[dict[str, Any]], available_columns: set[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in rows:
        metadata = _parse_metadata(row)
        for candidate in _factor_column_candidates(row, metadata):
            if candidate in available_columns:
                mapping[str(row.get("factor_id"))] = candidate
                break
    return mapping


def _factor_checks(rows: list[dict[str, Any]], values: pd.DataFrame | None, column_map: dict[str, str]) -> list[dict[str, Any]]:
    checks = []
    total_rows = int(len(values)) if values is not None else 0
    for row in rows:
        metadata = _parse_metadata(row)
        factor_id = str(row.get("factor_id"))
        column = column_map.get(factor_id)
        score, score_source = _admission_score(row, metadata)
        issues: list[str] = []
        coverage: dict[str, Any] = {}
        health = "ok"
        recommendation = "keep"
        if not column:
            issues.append("factor_values_column_missing")
            health = "bad"
            recommendation = "watch"
            coverage["individual_parquet_exists"] = _individual_factor_exists(metadata)
        elif values is None:
            coverage = {"status": "not_computed_in_quality_audit", "column_present": True}
        else:
            series = values[column].replace([np.inf, -np.inf], np.nan)
            non_null = int(series.notna().sum())
            coverage_ratio = non_null / max(total_rows, 1)
            nunique = int(series.dropna().nunique())
            coverage = {
                "non_null": non_null,
                "total": total_rows,
                "coverage_ratio": coverage_ratio,
                "nunique": nunique,
            }
            if non_null == 0:
                issues.append("all_null")
                health = "bad"
                recommendation = "watch"
            elif nunique <= 1:
                issues.append("constant_or_near_constant")
                health = "bad"
                recommendation = "watch"
            elif coverage_ratio < 0.30:
                issues.append("low_coverage")
                health = "watch"
                recommendation = "watch"
        checks.append(
            {
                "factor_id": factor_id,
                "name": row.get("name"),
                "expression": row.get("expression"),
                "category": row.get("category"),
                "status": row.get("status"),
                "holding_period_days": row.get("holding_period_days"),
                "column": column,
                "health": health,
                "data_coverage": coverage,
                "admission_score": score,
                "admission_score_source": score_source,
                "metrics": {
                    "ic_mean": row.get("ic_mean"),
                    "icir": row.get("icir"),
                    "rank_ic": row.get("rank_ic"),
                    "rank_icir": _nested_get(metadata, "metrics", "rank_icir") or _nested_get(metadata, "backtest_summary", "rank_ic_ir"),
                    "sharpe": row.get("sharpe"),
                    "annual_return": _nested_get(metadata, "metrics", "annual_return") or _nested_get(metadata, "backtest_summary", "annual_return"),
                    "max_drawdown": row.get("max_drawdown"),
                    "turnover": row.get("turnover"),
                },
                "issues": issues,
                "recommendation": recommendation,
                "reason": ";".join(issues) if issues else "healthy",
            }
        )
    return checks


def _pairwise_stats(
    values: pd.DataFrame,
    factor_info: list[dict[str, Any]],
    *,
    min_common_stocks: int,
    audit_window_start: str | None,
    audit_window_end: str | None,
) -> list[dict[str, Any]]:
    if values.empty or len(factor_info) < 2:
        return []
    frame = values.copy().replace([np.inf, -np.inf], np.nan)
    if audit_window_start or audit_window_end:
        dates = pd.to_datetime(frame.index.get_level_values("trade_date"))
        mask = np.ones(len(frame), dtype=bool)
        if audit_window_start:
            mask &= dates >= pd.Timestamp(audit_window_start)
        if audit_window_end:
            mask &= dates <= pd.Timestamp(audit_window_end)
        frame = frame.loc[mask]
    factor_ids = [item["factor_id"] for item in factor_info]
    columns = [item["column"] for item in factor_info]
    pair_values: dict[tuple[str, str], dict[str, list[float]]] = {
        tuple(sorted((a, b))): {"pearson": [], "rank": [], "common": []}
        for i, a in enumerate(factor_ids)
        for b in factor_ids[i + 1 :]
    }
    for _, sub in frame.groupby(level="trade_date", sort=True):
        if len(sub) < min_common_stocks:
            continue
        pearson = sub.corr(min_periods=min_common_stocks).abs()
        rank_corr = sub.rank(pct=True).corr(min_periods=min_common_stocks).abs()
        counts = sub.notna().astype("int16").T.dot(sub.notna().astype("int16"))
        for i, col_a in enumerate(columns):
            for j in range(i + 1, len(columns)):
                col_b = columns[j]
                common = int(counts.loc[col_a, col_b])
                if common < min_common_stocks:
                    continue
                key = tuple(sorted((factor_ids[i], factor_ids[j])))
                p = pearson.loc[col_a, col_b]
                r = rank_corr.loc[col_a, col_b]
                if pd.notna(p):
                    pair_values[key]["pearson"].append(float(p))
                if pd.notna(r):
                    pair_values[key]["rank"].append(float(r))
                pair_values[key]["common"].append(float(common))
    names = {item["factor_id"]: item.get("name") for item in factor_info}
    out = []
    for (a, b), vals in pair_values.items():
        pearson_vals = np.array(vals["pearson"], dtype=float)
        rank_vals = np.array(vals["rank"], dtype=float)
        common_vals = np.array(vals["common"], dtype=float)
        valid_days = int(max(len(pearson_vals), len(rank_vals)))
        if valid_days == 0:
            continue

        def stat(arr: np.ndarray, name: str) -> float | None:
            if arr.size == 0:
                return None
            if name == "mean":
                return float(np.nanmean(arr))
            if name == "p90":
                return float(np.nanpercentile(arr, 90))
            if name == "max":
                return float(np.nanmax(arr))
            return None

        metrics = {
            "mean_abs_pearson": stat(pearson_vals, "mean"),
            "p90_abs_pearson": stat(pearson_vals, "p90"),
            "max_abs_pearson": stat(pearson_vals, "max"),
            "mean_abs_rank_corr": stat(rank_vals, "mean"),
            "p90_abs_rank_corr": stat(rank_vals, "p90"),
            "max_abs_rank_corr": stat(rank_vals, "max"),
        }
        dependency_inputs = {
            key: metrics.get(key)
            for key in [
                "p90_abs_rank_corr",
                "p90_abs_pearson",
                "mean_abs_rank_corr",
                "mean_abs_pearson",
            ]
        }
        dependency = max(value or 0.0 for value in dependency_inputs.values())
        out.append(
            {
                "factor_a": a,
                "factor_b": b,
                "factor_id_a": a,
                "factor_id_b": b,
                "name_a": names.get(a),
                "name_b": names.get(b),
                **metrics,
                "dependency_score": dependency,
                "dependency_score_basis": dependency_inputs,
                "valid_days": valid_days,
                "avg_common_stocks": float(np.nanmean(common_vals)) if common_vals.size else None,
            }
        )
    return sorted(out, key=lambda item: item.get("dependency_score") or 0.0, reverse=True)


def _connected_components(nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    graph: dict[str, set[str]] = {node: set() for node in nodes}
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)
    seen = set()
    comps = []
    for node in nodes:
        if node in seen:
            continue
        seen.add(node)
        queue = deque([node])
        comp = []
        while queue:
            cur = queue.popleft()
            comp.append(cur)
            for nxt in graph.get(cur, set()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        if len(comp) > 1:
            comps.append(sorted(comp))
    return comps


def _representative(members: list[str], rows_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ranked = []
    for factor_id in members:
        score, source, created_at = _representative_score(rows_by_id[factor_id])
        ranked.append((score, created_at, factor_id, source))
    ranked.sort(reverse=True)
    score, _, factor_id, source = ranked[0]
    return {
        "factor_id": factor_id,
        "name": rows_by_id[factor_id].get("name"),
        "expression": rows_by_id[factor_id].get("expression"),
        "admission_score": score if score >= 0 else None,
        "score_source": source,
    }


def _cluster_member(factor_id: str, rows_by_id: dict[str, dict[str, Any]], *, recommendation: str | None = None) -> dict[str, Any]:
    row = rows_by_id[factor_id]
    member = {
        "factor_id": factor_id,
        "name": row.get("name"),
        "expression": row.get("expression"),
    }
    if recommendation:
        member["recommendation"] = recommendation
    return member


def _redundancy_clusters(factor_ids: list[str], pair_stats: list[dict[str, Any]], rows_by_id: dict[str, dict[str, Any]], *, rank_p90: float, pearson_p90: float) -> list[dict[str, Any]]:
    edges = [
        (pair["factor_a"], pair["factor_b"])
        for pair in pair_stats
        if (pair.get("p90_abs_pearson") or 0) >= pearson_p90
        or (pair.get("p90_abs_rank_corr") or 0) >= rank_p90
        or (pair.get("mean_abs_pearson") or 0) >= pearson_p90
        or (pair.get("mean_abs_rank_corr") or 0) >= rank_p90
    ]
    pair_lookup = {tuple(sorted((p["factor_a"], p["factor_b"]))): p for p in pair_stats}
    clusters = []
    for idx, members in enumerate(_connected_components(factor_ids, edges), start=1):
        rep = _representative(members, rows_by_id)
        pairs = [pair_lookup[tuple(sorted((a, b)))] for i, a in enumerate(members) for b in members[i + 1 :] if tuple(sorted((a, b))) in pair_lookup]
        clusters.append(
            {
                "cluster_id": f"redundancy_{idx:03d}",
                "size": len(members),
                "representative": rep,
                "members": [_cluster_member(mid, rows_by_id, recommendation="keep" if mid == rep["factor_id"] else "watch") for mid in members],
                "max_p90_abs_pearson": max((p.get("p90_abs_pearson") or 0) for p in pairs) if pairs else None,
                "max_p90_abs_rank_corr": max((p.get("p90_abs_rank_corr") or 0) for p in pairs) if pairs else None,
                "retire_candidates": [mid for mid in members if mid != rep["factor_id"]],
                "reason": "high_factor_value_correlation_cluster",
            }
        )
    return clusters


def _information_clusters(factor_ids: list[str], pair_stats: list[dict[str, Any]], rows_by_id: dict[str, dict[str, Any]], *, family_dependency_cut: float) -> list[dict[str, Any]]:
    if not factor_ids:
        return []
    if len(factor_ids) == 1:
        only = factor_ids[0]
        return [{
            "cluster_id": "information_001",
            "size": 1,
            "representative": _representative([only], rows_by_id),
            "members": [_cluster_member(only, rows_by_id)],
            "max_dependency_score": None,
            "mean_dependency_score": None,
            "reason": "singleton_information_family",
        }]
    pair_dep = {tuple(sorted((p["factor_a"], p["factor_b"]))): float(p.get("dependency_score") or 0.0) for p in pair_stats}
    try:
        from scipy.cluster.hierarchy import fcluster, linkage
        from scipy.spatial.distance import squareform

        n = len(factor_ids)
        dist = np.zeros((n, n), dtype=float)
        for i, a in enumerate(factor_ids):
            for j, b in enumerate(factor_ids):
                if i != j:
                    dist[i, j] = math.sqrt(max(0.0, 1.0 - pair_dep.get(tuple(sorted((a, b))), 0.0)))
        labels = fcluster(linkage(squareform(dist, checks=False), method="complete"), t=math.sqrt(max(0.0, 1.0 - family_dependency_cut)), criterion="distance")
        grouped: dict[int, list[str]] = defaultdict(list)
        for factor_id, label in zip(factor_ids, labels):
            grouped[int(label)].append(factor_id)
        members_list = [sorted(members) for members in grouped.values()]
    except Exception:
        multi_members = _connected_components(factor_ids, [(a, b) for (a, b), dep in pair_dep.items() if dep >= family_dependency_cut])
        clustered = {factor_id for members in multi_members for factor_id in members}
        members_list = [*multi_members, *[[factor_id] for factor_id in factor_ids if factor_id not in clustered]]
    members_list = sorted(members_list, key=lambda members: (-len(members), members))
    pair_lookup = {tuple(sorted((p["factor_a"], p["factor_b"]))): p for p in pair_stats}
    clusters = []
    for idx, members in enumerate(members_list, start=1):
        rep = _representative(members, rows_by_id)
        pairs = [pair_lookup[tuple(sorted((a, b)))] for i, a in enumerate(members) for b in members[i + 1 :] if tuple(sorted((a, b))) in pair_lookup]
        deps = [p.get("dependency_score") or 0 for p in pairs]
        clusters.append(
            {
                "cluster_id": f"information_{idx:03d}",
                "size": len(members),
                "representative": rep,
                "members": [_cluster_member(mid, rows_by_id) for mid in members],
                "max_dependency_score": max(deps) if deps else None,
                "mean_dependency_score": float(np.mean(deps)) if deps else None,
                "reason": "shared_information_family_for_feature_set_design" if len(members) > 1 else "singleton_information_family",
            }
        )
    return clusters


def _cluster_member_ids(cluster: dict[str, Any]) -> list[str]:
    return sorted(
        str(member.get("factor_id"))
        for member in (cluster.get("members") or [])
        if isinstance(member, dict) and str(member.get("factor_id") or "").strip()
    )


def _derived_region_uid(member_ids: list[str]) -> str:
    payload = "|".join(sorted(str(item) for item in member_ids if str(item).strip()))
    return f"region_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def _previous_region_uid(cluster: dict[str, Any]) -> str:
    return str(cluster.get("region_uid") or "").strip() or _derived_region_uid(_cluster_member_ids(cluster))


def _assign_region_identity(
    information_clusters: list[dict[str, Any]],
    previous_information: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Attach stable ids without changing factor-value cluster membership."""

    previous_clusters = [
        item
        for item in ((previous_information or {}).get("information_clusters") or [])
        if isinstance(item, dict) and _cluster_member_ids(item)
    ]
    previous_sets = [set(_cluster_member_ids(item)) for item in previous_clusters]
    current_sets = [set(_cluster_member_ids(item)) for item in information_clusters]
    previous_to_current: dict[int, list[int]] = defaultdict(list)
    current_to_previous: dict[int, list[int]] = defaultdict(list)
    for previous_idx, previous_members in enumerate(previous_sets):
        for current_idx, current_members in enumerate(current_sets):
            if previous_members & current_members:
                previous_to_current[previous_idx].append(current_idx)
                current_to_previous[current_idx].append(previous_idx)

    identified: list[dict[str, Any]] = []
    for current_idx, cluster in enumerate(information_clusters):
        current_members = current_sets[current_idx]
        overlapping_previous = current_to_previous.get(current_idx, [])
        exact_previous = next(
            (idx for idx in overlapping_previous if previous_sets[idx] == current_members),
            None,
        )
        inherited_previous: int | None = exact_previous
        lineage_event = "unchanged" if exact_previous is not None else "new"

        if inherited_previous is None and len(overlapping_previous) == 1:
            previous_idx = overlapping_previous[0]
            previous_members = previous_sets[previous_idx]
            union_size = len(previous_members | current_members)
            jaccard = len(previous_members & current_members) / union_size if union_size else 0.0
            if len(previous_to_current.get(previous_idx, [])) == 1 and jaccard >= 0.5:
                inherited_previous = previous_idx
                lineage_event = "membership_changed"

        parent_region_uids = [
            _previous_region_uid(previous_clusters[idx])
            for idx in overlapping_previous
        ]
        if inherited_previous is not None:
            region_uid = _previous_region_uid(previous_clusters[inherited_previous])
        else:
            region_uid = _derived_region_uid(list(current_members))
            if len(overlapping_previous) > 1:
                lineage_event = "merged"
            elif len(overlapping_previous) == 1 and len(previous_to_current.get(overlapping_previous[0], [])) > 1:
                lineage_event = "split"
            elif overlapping_previous:
                lineage_event = "reclustered"

        identified.append(
            {
                **cluster,
                "region_uid": region_uid,
                "display_index": current_idx + 1,
                "previous_region_uids": list(dict.fromkeys(parent_region_uids)),
                "lineage_event": lineage_event,
                "member_fingerprint": hashlib.sha256(
                    "|".join(sorted(current_members)).encode("utf-8")
                ).hexdigest()[:16],
            }
        )
    return identified


def _factor_relation_graph(
    rows: list[dict[str, Any]],
    information_clusters: list[dict[str, Any]],
    pair_stats: list[dict[str, Any]],
    *,
    representative_degree: int = 2,
) -> dict[str, Any]:
    """Build one compact whole-library graph from the information audit.

    Every usable factor is retained as a node.  Edges stay readable by keeping
    the measured representative-to-member link inside each information family
    plus each representative's strongest cross-family neighbours.  This is a
    display projection of the already-computed pair matrix, not a second
    clustering or correlation implementation.
    """

    rows_by_id = {str(row.get("factor_id")): row for row in rows if row.get("factor_id")}
    cluster_by_factor: dict[str, str] = {}
    region_by_factor: dict[str, str] = {}
    representative_ids: set[str] = set()
    cluster_summaries: list[dict[str, Any]] = []
    for cluster in information_clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        region_uid = str(cluster.get("region_uid") or "")
        representative = cluster.get("representative") or {}
        representative_id = str(representative.get("factor_id") or "")
        if representative_id:
            representative_ids.add(representative_id)
        members = [
            str(member.get("factor_id"))
            for member in (cluster.get("members") or [])
            if member.get("factor_id")
        ]
        for factor_id in members:
            cluster_by_factor[factor_id] = cluster_id
            region_by_factor[factor_id] = region_uid
        cluster_summaries.append(
            {
                "cluster_id": cluster_id,
                "region_uid": region_uid,
                "size": len(members),
                "representative_factor_id": representative_id,
                "representative_name": representative.get("name"),
                "max_dependency_score": cluster.get("max_dependency_score"),
                "mean_dependency_score": cluster.get("mean_dependency_score"),
            }
        )

    nodes: list[dict[str, Any]] = []
    for factor_id, row in rows_by_id.items():
        score, score_source, _ = _representative_score(row)
        nodes.append(
            {
                "factor_id": factor_id,
                "name": row.get("name") or factor_id,
                "category": row.get("category") or "",
                "cluster_id": cluster_by_factor.get(factor_id, "unclustered"),
                "region_uid": region_by_factor.get(factor_id, "unclustered"),
                "is_representative": factor_id in representative_ids,
                "admission_score": score if score >= 0 else None,
                "score_source": score_source,
            }
        )

    pair_lookup = {
        tuple(sorted((str(pair.get("factor_a") or ""), str(pair.get("factor_b") or "")))): pair
        for pair in pair_stats
        if pair.get("factor_a") and pair.get("factor_b")
    }
    selected_edges: dict[tuple[str, str], dict[str, Any]] = {}

    def add_edge(source: str, target: str, relation_type: str) -> None:
        if not source or not target or source == target:
            return
        key = tuple(sorted((source, target)))
        pair = pair_lookup.get(key)
        if not pair:
            return
        current = selected_edges.get(key)
        edge = {
            "source": source,
            "target": target,
            "name_source": rows_by_id.get(source, {}).get("name") or source,
            "name_target": rows_by_id.get(target, {}).get("name") or target,
            "dependency_score": pair.get("dependency_score"),
            "p90_abs_rank_corr": pair.get("p90_abs_rank_corr"),
            "p90_abs_pearson": pair.get("p90_abs_pearson"),
            "valid_days": pair.get("valid_days"),
            "relation_type": relation_type,
            "source_region_uid": region_by_factor.get(source, "unclustered"),
            "target_region_uid": region_by_factor.get(target, "unclustered"),
        }
        # Representative links are the higher-level relationship and should
        # win if a pair is selected through more than one projection rule.
        if current is None or relation_type == "representative_link":
            selected_edges[key] = edge

    for cluster in information_clusters:
        representative_id = str((cluster.get("representative") or {}).get("factor_id") or "")
        for member in cluster.get("members") or []:
            member_id = str(member.get("factor_id") or "")
            if member_id and member_id != representative_id:
                add_edge(representative_id, member_id, "family_link")

    representative_pairs = [
        pair
        for pair in pair_stats
        if str(pair.get("factor_a") or "") in representative_ids
        and str(pair.get("factor_b") or "") in representative_ids
        and cluster_by_factor.get(str(pair.get("factor_a") or ""))
        != cluster_by_factor.get(str(pair.get("factor_b") or ""))
    ]
    representative_pairs.sort(key=lambda item: float(item.get("dependency_score") or 0.0), reverse=True)
    per_representative: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in representative_pairs:
        per_representative[str(pair.get("factor_a"))].append(pair)
        per_representative[str(pair.get("factor_b"))].append(pair)
    for representative_id in representative_ids:
        for pair in per_representative.get(representative_id, [])[: max(1, int(representative_degree))]:
            add_edge(str(pair.get("factor_a") or ""), str(pair.get("factor_b") or ""), "representative_link")

    edges = sorted(
        selected_edges.values(),
        key=lambda item: (
            item.get("relation_type") != "representative_link",
            -float(item.get("dependency_score") or 0.0),
        ),
    )
    return {
        "schema_version": "factor_relation_graph_v1",
        "nodes": nodes,
        "edges": edges,
        "clusters": cluster_summaries,
        "summary": {
            "node_count": len(nodes),
            "cluster_count": len(information_clusters),
            "available_pair_count": len(pair_stats),
            "display_edge_count": len(edges),
            "family_edge_count": sum(edge.get("relation_type") == "family_link" for edge in edges),
            "representative_edge_count": sum(edge.get("relation_type") == "representative_link" for edge in edges),
            "edge_policy": "family_representative_links_plus_top2_cross_family_representative_links",
        },
    }


def _rank_factor_ids(factor_ids: list[str], rows_by_id: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(factor_ids, key=lambda fid: _representative_score(rows_by_id[fid])[0], reverse=True)


def _unique_factor_ids(factor_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for factor_id in factor_ids:
        if factor_id not in seen:
            seen.add(factor_id)
            out.append(factor_id)
    return out


def _feature_set_eligible(row: dict[str, Any], *, min_admission_score: float = 80.0) -> bool:
    metadata = _parse_metadata(row)
    score, _ = _admission_score(row, metadata)
    if score is None or score < min_admission_score:
        return False
    gate = metadata.get("gate_result") if isinstance(metadata.get("gate_result"), dict) else {}
    if gate.get("passed") is False:
        return False
    return True


def _feature_set_item(
    *,
    name: str,
    factor_ids: list[str],
    all_ids: list[str],
    rationale: str,
    use_case: str,
    family_coverage_count: int,
) -> dict[str, Any]:
    unique_ids = _unique_factor_ids(factor_ids)
    all_set = set(all_ids)
    unique_set = set(unique_ids)
    all_active_names = {"ALL_ACTIVE", "FS_ALL_ACTIVE"}
    degenerate = len(unique_ids) == len(all_ids) and unique_set == all_set
    compression_ratio = 1.0 - (len(unique_ids) / max(len(all_ids), 1))
    return {
        "name": name,
        "factor_ids": unique_ids,
        "count": len(unique_ids),
        "compression_ratio": compression_ratio,
        "family_coverage_count": family_coverage_count,
        "degenerate": degenerate,
        "degenerate_reason": "same_as_all_active" if degenerate and name not in all_active_names else "",
        "use_case": use_case,
        "rationale": rationale,
    }


def _feature_sets(rows: list[dict[str, Any]], redundancy_clusters: list[dict[str, Any]], information_clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active_ids = [str(row.get("factor_id")) for row in rows]
    rows_by_id = {str(row.get("factor_id")): row for row in rows}
    eligible_ids = {str(row.get("factor_id")) for row in rows if _feature_set_eligible(row)}

    multi_factor_clusters = [cluster for cluster in information_clusters if len(cluster.get("members") or []) > 1]
    family_member_sets = [
        [m["factor_id"] for m in cluster.get("members", []) if m.get("factor_id") in rows_by_id and m.get("factor_id") in eligible_ids]
        for cluster in multi_factor_clusters
    ]
    family_members = {fid for members in family_member_sets for fid in members}
    unclustered = _rank_factor_ids([fid for fid in active_ids if fid in eligible_ids and fid not in family_members], rows_by_id)

    top1_by_family: list[str] = []
    top2_by_family: list[str] = []
    top3_by_family: list[str] = []
    for cluster in multi_factor_clusters:
        members = [m["factor_id"] for m in cluster.get("members", []) if m.get("factor_id") in rows_by_id and m.get("factor_id") in eligible_ids]
        ranked = _rank_factor_ids(members, rows_by_id)
        if ranked:
            top1_by_family.append(ranked[0])
            top2_by_family.extend(ranked[: min(2, len(ranked))])
            top3_by_family.extend(ranked[: min(3, len(ranked))])

    eligible_rows = [row for row in rows if str(row.get("factor_id")) in eligible_ids]
    experimental = [str(row.get("factor_id")) for row in sorted(eligible_rows, key=lambda row: _representative_score(row)[0], reverse=True)[: min(12, len(eligible_rows))]]
    return [
        _feature_set_item(
            name="ALL_ACTIVE",
            factor_ids=active_ids,
            all_ids=active_ids,
            family_coverage_count=len(multi_factor_clusters),
            use_case="Tree models with internal feature selection.",
            rationale="Use every active factor as the broad baseline.",
        ),
        _feature_set_item(
            name="FAMILY_TOP1_PLUS_UNCLUSTERED8",
            factor_ids=[*top1_by_family, *unclustered[:8]],
            all_ids=active_ids,
            family_coverage_count=len(top1_by_family),
            use_case="Low-dimensional diversified baseline.",
            rationale="Use one admission-score representative per information family plus top unclustered factors.",
        ),
        _feature_set_item(
            name="FAMILY_TOP2_PLUS_UNCLUSTERED8",
            factor_ids=[*top2_by_family, *unclustered[:8]],
            all_ids=active_ids,
            family_coverage_count=len(top1_by_family),
            use_case="Balance factor quality and information diversity.",
            rationale="Use up to two admission-score leaders per information family plus top unclustered factors.",
        ),
        _feature_set_item(
            name="FAMILY_TOP3_PLUS_UNCLUSTERED8",
            factor_ids=[*top3_by_family, *unclustered[:8]],
            all_ids=active_ids,
            family_coverage_count=len(top1_by_family),
            use_case="Wide family-aware model feature set.",
            rationale="Use up to three admission-score leaders per information family plus top unclustered factors.",
        ),
        _feature_set_item(
            name="QUALITY_TOP12",
            factor_ids=experimental,
            all_ids=active_ids,
            family_coverage_count=0,
            use_case="Fast model sweep and sanity check.",
            rationale="Use the top 12 factors by admission score regardless of family membership.",
        ),
    ]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_jsonable(payload), ensure_ascii=False, indent=2)
    tmp = path.with_name(f"{path.name}.tmp.{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _save_report(payload: dict[str, Any], *, audit_type: str, audit_id: str) -> dict[str, str]:
    latest_path = _latest_path(audit_type)
    report_path = AUDIT_REPORT_DIR / f"{audit_id}_{audit_type}.json"
    _atomic_write_json(latest_path, payload)
    _atomic_write_json(report_path, payload)
    return {"latest_path": str(latest_path), "report_path": str(report_path)}


def _quality_report(
    *,
    audit_id: str,
    fingerprint: dict[str, Any],
    rows: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    duplicate_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    bad = [item for item in checks if item.get("health") == "bad"]
    watch = [item for item in checks if item.get("health") == "watch"]
    return {
        "audit_version": AUDIT_VERSION,
        "audit_type": "quality",
        "audit_id": audit_id,
        "cache_key": fingerprint.get("cache_key"),
        "audit_fingerprint": fingerprint,
        "generated_at": _now(),
        "summary": {
            "status": "completed",
            "audit_type": "quality",
            "active_count": len([row for row in rows if row.get("status") == "active"]),
            "factor_count": len(rows),
            "usable_count": len([item for item in checks if item.get("health") != "bad" and item.get("column")]),
            "data_issue_count": len(bad),
            "watch_count": len(watch),
            "duplicate_expression_group_count": len(duplicate_groups),
            "duplicate_expression_factor_count": sum(max(0, int(item.get("count") or 0) - 1) for item in duplicate_groups),
            "feature_ready": bool(checks) and not bad,
            "stale": False,
            "stale_reason": "",
            "current_active_count": fingerprint.get("current_active_count"),
            "active_values_store_status": "partial" if bad else "complete",
            "evidence_level": "current_quality_evidence",
        },
        "factor_checks": checks,
        "duplicate_expression_groups": duplicate_groups,
        "artifacts": {},
    }


def _information_report(
    *,
    audit_id: str,
    fingerprint: dict[str, Any],
    rows: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    values: pd.DataFrame | None,
    include_feature_sets: bool,
    min_valid_days: int,
    min_common_stocks: int,
    audit_window_start: str | None,
    audit_window_end: str | None,
    redundancy_threshold_rank_p90: float,
    redundancy_threshold_pearson_p90: float,
    family_dependency_cut: float,
    previous_information: dict[str, Any] | None = None,
) -> dict[str, Any]:
    usable_checks = [item for item in checks if item.get("health") != "bad" and item.get("column")]
    usable_ids = [str(item["factor_id"]) for item in usable_checks]
    rows_by_id = {str(row.get("factor_id")): row for row in rows}
    usable_rows = [rows_by_id[factor_id] for factor_id in usable_ids if factor_id in rows_by_id]
    pair_stats: list[dict[str, Any]] = []
    redundancy_clusters: list[dict[str, Any]] = []
    information_clusters: list[dict[str, Any]] = []
    if values is not None and usable_checks:
        factor_info = [{"factor_id": item["factor_id"], "name": item["name"], "column": item["column"]} for item in usable_checks]
        source_columns = [item["column"] for item in factor_info]
        audit_values = values[source_columns].copy()
        audit_values.columns = [item["factor_id"] for item in factor_info]
        pair_factor_info = [{**item, "column": item["factor_id"]} for item in factor_info]
        pair_stats = _pairwise_stats(
            audit_values,
            pair_factor_info,
            min_common_stocks=min_common_stocks,
            audit_window_start=audit_window_start,
            audit_window_end=audit_window_end,
        )
        cluster_pairs = [pair for pair in pair_stats if int(pair.get("valid_days") or 0) >= min_valid_days]
        redundancy_clusters = _redundancy_clusters(
            usable_ids,
            cluster_pairs,
            rows_by_id,
            rank_p90=redundancy_threshold_rank_p90,
            pearson_p90=redundancy_threshold_pearson_p90,
        )
        information_clusters = _information_clusters(
            usable_ids,
            cluster_pairs,
            rows_by_id,
            family_dependency_cut=family_dependency_cut,
        )
    information_clusters = _assign_region_identity(information_clusters, previous_information)
    feature_sets = _feature_sets(usable_rows, redundancy_clusters, information_clusters) if include_feature_sets else []
    relation_graph = _factor_relation_graph(usable_rows, information_clusters, pair_stats)
    retire_candidates = sorted({fid for cluster in redundancy_clusters for fid in cluster.get("retire_candidates", [])})
    clustered_ids = {str(member.get("factor_id")) for cluster in information_clusters for member in cluster.get("members", []) if member.get("factor_id")}
    generated_at = _now()
    map_id = f"fm_{generated_at.replace('-', '').replace(':', '').replace('T', '_')}_{str(fingerprint.get('registry_fingerprint') or '')[:8]}"
    return {
        "audit_version": AUDIT_VERSION,
        "map_schema_version": FACTOR_MAP_SCHEMA_VERSION,
        "audit_type": "information",
        "audit_id": audit_id,
        "map_id": map_id,
        "cache_key": fingerprint.get("cache_key"),
        "audit_fingerprint": fingerprint,
        "generated_at": generated_at,
        "summary": {
            "status": "completed",
            "audit_type": "information",
            "active_count": len([row for row in rows if row.get("status") == "active"]),
            "factor_count": len(rows),
            "usable_count": len(usable_checks),
            "excluded_count": len(rows) - len(usable_checks),
            "top_correlated_pair_count": len(pair_stats),
            "redundancy_cluster_count": len(redundancy_clusters),
            "information_cluster_count": len(information_clusters),
            "information_clustered_factor_count": len(clustered_ids),
            "information_coverage_complete": len(clustered_ids) == len(usable_ids),
            "active_pool_coverage_complete": len(usable_ids) == len(rows),
            "stale": False,
            "stale_reason": "",
            "current_active_count": fingerprint.get("current_active_count"),
            "active_values_store_status": "partial" if len(usable_checks) != len(rows) else "complete",
            "evidence_level": "current_information_evidence",
            "map_id": map_id,
            "map_schema_version": FACTOR_MAP_SCHEMA_VERSION,
        },
        "eligibility": {
            "usable_factor_ids": usable_ids,
            "excluded_factors": [
                {
                    "factor_id": item.get("factor_id"),
                    "name": item.get("name"),
                    "expression": (rows_by_id.get(str(item.get("factor_id"))) or {}).get("expression"),
                    "health": item.get("health"),
                    "issues": item.get("issues") or [],
                }
                for item in checks
                if item.get("health") == "bad" or not item.get("column")
            ],
        },
        "top_correlated_pairs": pair_stats[:50],
        "redundancy_clusters": redundancy_clusters,
        "information_clusters": information_clusters,
        "cluster_representatives": [
            {
                "cluster_id": cluster["cluster_id"],
                "region_uid": cluster.get("region_uid"),
                **cluster["representative"],
            }
            for cluster in information_clusters
        ],
        "feature_set_recommendations": feature_sets,
        "relation_graph": relation_graph,
        "actions": {"safe_to_auto_retire": False, "requires_human_confirmation": True, "retire_candidates": retire_candidates},
        "artifacts": {},
    }


def _combined_audit_view(quality: dict[str, Any] | None, information: dict[str, Any] | None) -> dict[str, Any]:
    quality_summary = (quality or {}).get("summary") or {}
    information_summary = (information or {}).get("summary") or {}
    stale = bool(quality_summary.get("stale", True) or information_summary.get("stale", True))
    reasons = [str(item) for item in [quality_summary.get("stale_reason"), information_summary.get("stale_reason")] if item]
    return {
        "scope": "all",
        "audit_type": "all",
        "audit_version": AUDIT_VERSION,
        "audit_id": (information or quality or {}).get("audit_id"),
        "map_id": (information or {}).get("map_id"),
        "map_schema_version": (information or {}).get("map_schema_version"),
        "generated_at": max(str((quality or {}).get("generated_at") or ""), str((information or {}).get("generated_at") or "")),
        "summary": {
            **quality_summary,
            "scope": "all",
            "audit_type": "all",
            "status": "completed" if quality and information else "partial" if quality or information else "missing",
            "stale": stale,
            "stale_reason": ",".join(dict.fromkeys(reasons)),
            "top_correlated_pair_count": information_summary.get("top_correlated_pair_count", 0),
            "redundancy_cluster_count": information_summary.get("redundancy_cluster_count", 0),
            "information_cluster_count": information_summary.get("information_cluster_count", 0),
            "information_coverage_complete": information_summary.get("information_coverage_complete", False),
        },
        "quality": quality or {},
        "information": information or {},
        "factor_checks": (quality or {}).get("factor_checks") or [],
        "duplicate_expression_groups": (quality or {}).get("duplicate_expression_groups") or [],
        "top_correlated_pairs": (information or {}).get("top_correlated_pairs") or [],
        "redundancy_clusters": (information or {}).get("redundancy_clusters") or [],
        "information_clusters": (information or {}).get("information_clusters") or [],
        "cluster_representatives": (information or {}).get("cluster_representatives") or [],
        "feature_set_recommendations": (information or {}).get("feature_set_recommendations") or [],
        "relation_graph": (information or {}).get("relation_graph") or {},
        "actions": (information or {}).get("actions") or {"safe_to_auto_retire": False, "requires_human_confirmation": True, "retire_candidates": []},
        "artifacts": {"quality": (quality or {}).get("artifacts") or {}, "information": (information or {}).get("artifacts") or {}},
    }


def factor_library_audit(
    *,
    scope: str = "all",
    status_filter: str = "active",
    save_report: bool = True,
    include_feature_sets: bool = True,
    audit_window_start: str | None = None,
    audit_window_end: str | None = None,
    min_valid_days: int = 120,
    min_common_stocks: int = 300,
    redundancy_threshold_rank_p90: float = 0.80,
    redundancy_threshold_pearson_p90: float = 0.75,
    family_dependency_cut: float = 0.55,
) -> Any:
    inputs = locals().copy()
    validation_errors = _validate_audit_inputs(
        scope=scope,
        min_valid_days=min_valid_days,
        min_common_stocks=min_common_stocks,
        redundancy_threshold_rank_p90=redundancy_threshold_rank_p90,
        redundancy_threshold_pearson_p90=redundancy_threshold_pearson_p90,
        family_dependency_cut=family_dependency_cut,
    )
    if validation_errors:
        return err_result("invalid_factor_audit_inputs", inputs=inputs, outputs={"validation_errors": validation_errors})
    min_valid_days = int(min_valid_days)
    min_common_stocks = int(min_common_stocks)
    redundancy_threshold_rank_p90 = float(redundancy_threshold_rank_p90)
    redundancy_threshold_pearson_p90 = float(redundancy_threshold_pearson_p90)
    family_dependency_cut = float(family_dependency_cut)
    try:
        rows = _active_rows(status_filter)
        available_columns = _adopted_value_columns()
        column_map = _match_columns(rows, available_columns)
        values_file = FACTOR_ACTIVE_ADOPTED_VALUES_FILE if FACTOR_ACTIVE_ADOPTED_VALUES_FILE.exists() else FACTOR_ADOPTED_VALUES_FILE
        values = pd.read_parquet(values_file) if values_file.exists() else None
        checks = _factor_checks(rows, values, column_map)
        duplicate_groups = FactorRegistry().audit_active_duplicates() if status_filter == "active" else []
        base_kwargs = {
            "rows": rows,
            "status_filter": status_filter,
            "audit_window_start": audit_window_start,
            "audit_window_end": audit_window_end,
            "min_valid_days": min_valid_days,
            "min_common_stocks": min_common_stocks,
            "redundancy_threshold_rank_p90": redundancy_threshold_rank_p90,
            "redundancy_threshold_pearson_p90": redundancy_threshold_pearson_p90,
            "family_dependency_cut": family_dependency_cut,
        }
        quality_fp = _audit_fingerprint(audit_type="quality", **base_kwargs)
        information_fp = _audit_fingerprint(audit_type="information", **base_kwargs)
        audit_id = f"fa_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(quality_fp.get('registry_fingerprint') or '')[:8]}"
        previous_information = _load_latest_audit("information") if scope in {"information", "all"} else None
        quality = _quality_report(
            audit_id=audit_id,
            fingerprint=quality_fp,
            rows=rows,
            checks=checks,
            duplicate_groups=duplicate_groups,
        ) if scope in {"quality", "all"} else None
        information = _information_report(
            audit_id=audit_id,
            fingerprint=information_fp,
            rows=rows,
            checks=checks,
            values=values,
            include_feature_sets=include_feature_sets,
            min_valid_days=min_valid_days,
            min_common_stocks=min_common_stocks,
            audit_window_start=audit_window_start,
            audit_window_end=audit_window_end,
            redundancy_threshold_rank_p90=redundancy_threshold_rank_p90,
            redundancy_threshold_pearson_p90=redundancy_threshold_pearson_p90,
            family_dependency_cut=family_dependency_cut,
            previous_information=previous_information,
        ) if scope in {"information", "all"} else None
        artifacts: dict[str, Any] = {}
        if save_report and quality:
            quality_artifacts = _save_report(quality, audit_type="quality", audit_id=audit_id)
            quality["artifacts"] = quality_artifacts
            artifacts["quality"] = quality_artifacts
        if save_report and information:
            information_artifacts = _save_report(information, audit_type="information", audit_id=audit_id)
            information["artifacts"] = information_artifacts
            artifacts["information"] = information_artifacts
        outputs = quality if scope == "quality" else information if scope == "information" else _combined_audit_view(quality, information)
        outputs = dict(outputs or {})
        outputs["memory_release"] = release_process_memory("factor_library_audit_completed")
        return ok_result(inputs=inputs, outputs=_jsonable(outputs), artifacts=artifacts)
    except Exception as exc:
        return err_result("factor_library_audit_failed", inputs=inputs, outputs={"detail": str(exc)})


def factor_library_audit_status(scope: str = "all", compact: bool = False) -> Any:
    if scope not in {"quality", "information", "all"}:
        return err_result("invalid_factor_audit_scope", inputs={"scope": scope}, outputs={"allowed_scopes": ["quality", "information", "all"]})
    quality = _load_latest_audit("quality") if scope in {"quality", "all"} else None
    information = _load_latest_audit("information") if scope in {"information", "all"} else None
    if quality:
        quality = _with_freshness(quality, audit_type="quality")
    if information:
        information = _with_freshness(information, audit_type="information")
    if scope == "quality":
        outputs = quality or {"audit_type": "quality", "summary": {"status": "missing", **_freshness_payload(None, audit_type="quality")}, "factor_checks": [], "artifacts": {}}
    elif scope == "information":
        outputs = information or {"audit_type": "information", "summary": {"status": "missing", **_freshness_payload(None, audit_type="information")}, "top_correlated_pairs": [], "redundancy_clusters": [], "information_clusters": [], "feature_set_recommendations": [], "relation_graph": {}, "actions": {"safe_to_auto_retire": False, "requires_human_confirmation": True, "retire_candidates": []}, "artifacts": {}}
    else:
        outputs = _combined_audit_view(quality, information)
    if compact:
        factor_checks = [
            row
            for row in (outputs.get("factor_checks") or [])
            if row.get("issues") or row.get("health") in {"bad", "watch"}
        ]
        outputs = {
            key: outputs.get(key)
            for key in (
                "scope",
                "audit_type",
                "audit_version",
                "audit_id",
                "map_id",
                "map_schema_version",
                "generated_at",
                "summary",
                "feature_set_recommendations",
                "actions",
            )
        }
        outputs["factor_checks"] = factor_checks
        outputs["compact"] = True
    return ok_result(outputs=outputs, artifacts=outputs.get("artifacts", {}))


def factor_library_information_context(*, allow_stale_advisory: bool = False) -> dict[str, Any]:
    """Return a compact information map for a newly started research run.

    Normal governance callers require a current audit.  Factor research may
    explicitly use the last map as advisory context after a refresh failure;
    numeric novelty and import gates never consume this fallback.
    """
    latest = _load_latest_audit("information")
    ready, summary, payload = _information_governance_ready(latest)
    if not ready and allow_stale_advisory and payload:
        return {
            "available": True,
            "source": "factor_library_information_audit_advisory_snapshot",
            "audit_id": payload.get("audit_id"),
            "map_id": payload.get("map_id"),
            "map_schema_version": payload.get("map_schema_version"),
            "generated_at": payload.get("generated_at"),
            "registry_fingerprint": (payload.get("audit_fingerprint") or {}).get("registry_fingerprint"),
            "factor_count": summary.get("factor_count"),
            "usable_count": summary.get("usable_count"),
            "active_pool_coverage_complete": bool(summary.get("active_pool_coverage_complete")),
            "excluded_factors": ((payload.get("eligibility") or {}).get("excluded_factors") or []),
            "information_families": payload.get("information_clusters") or [],
            "redundancy_clusters": payload.get("redundancy_clusters") or [],
            "relation_graph": payload.get("relation_graph") or {},
            "freshness": "stale_advisory_only",
            "reason": summary.get("stale_reason"),
            "policy": "advisory_research_context_only_numeric_novelty_still_required",
        }
    if not ready or not payload:
        return {
            "available": False,
            "audit_id": (latest or {}).get("audit_id"),
            "map_id": (latest or {}).get("map_id"),
            "reason": summary.get("stale_reason") or "information_audit_missing",
            "summary": summary,
            "information_families": [],
            "redundancy_clusters": [],
            "relation_graph": {},
        }
    return {
        "available": True,
        "source": "factor_library_information_audit",
        "audit_id": payload.get("audit_id"),
        "map_id": payload.get("map_id"),
        "map_schema_version": payload.get("map_schema_version"),
        "generated_at": payload.get("generated_at"),
        "registry_fingerprint": (payload.get("audit_fingerprint") or {}).get("registry_fingerprint"),
        "factor_count": summary.get("factor_count"),
        "usable_count": summary.get("usable_count"),
        "active_pool_coverage_complete": bool(summary.get("active_pool_coverage_complete")),
        "excluded_factors": ((payload.get("eligibility") or {}).get("excluded_factors") or []),
        "information_families": payload.get("information_clusters") or [],
        "redundancy_clusters": payload.get("redundancy_clusters") or [],
        "relation_graph": payload.get("relation_graph") or {},
        "freshness": "fresh",
        "policy": "semantic_research_budget_context_only_numeric_novelty_still_required",
    }


def factor_feature_set_recommendations() -> Any:
    latest = _load_latest_audit("information")
    ready, summary, payload = _information_governance_ready(latest)
    if not latest:
        return ok_result(outputs={"summary": {**summary, "evidence_level": "insufficient_for_current_governance"}, "feature_set_recommendations": [], "cluster_representatives": [], "note": "Run a fresh information audit before requesting feature-set recommendations."}, warnings=["latest_information_audit_missing"])
    if not ready:
        summary = {**summary, "evidence_level": "insufficient_for_current_governance"}
    return ok_result(outputs={"summary": summary, "feature_set_recommendations": (payload or latest).get("feature_set_recommendations", []), "cluster_representatives": (payload or latest).get("cluster_representatives", [])}, artifacts=latest.get("artifacts", {}))


def factor_retire_plan() -> Any:
    latest = _load_latest_audit("information")
    ready, summary, payload = _information_governance_ready(latest)
    if not ready:
        actions = {"safe_to_auto_retire": False, "requires_human_confirmation": True, "retire_candidates": [], "blocked_reason": "information_audit_missing_or_stale"}
        return ok_result(outputs={"summary": {**summary, "evidence_level": "insufficient_for_current_governance"}, "actions": actions, "redundancy_clusters": [], "note": "Run a fresh information audit before using retire candidates."}, artifacts=(latest or {}).get("artifacts", {}), warnings=["information_audit_not_ready"])
    return ok_result(outputs={"summary": summary, "actions": payload.get("actions", {}), "redundancy_clusters": payload.get("redundancy_clusters", []), "note": "This is a read-only retire plan. It does not modify factor_registry.db."}, artifacts=latest.get("artifacts", {}))


def _run_audit_background(kwargs: dict[str, Any]) -> None:
    _set_run_state(
        status="running",
        scope=str(kwargs.get("scope") or "all"),
        last_error="",
        last_started_at=_now(),
        latest_quality_path=str(LATEST_QUALITY_AUDIT_FILE),
        latest_information_path=str(LATEST_INFORMATION_AUDIT_FILE),
    )
    try:
        result = factor_library_audit(**kwargs)
        payload = result.to_dict()
        _set_run_state(
            status="completed" if result.ok else "failed",
            last_finished_at=_now(),
            last_error="" if result.ok else str(payload.get("err") or "factor_library_audit_failed"),
            latest_quality_path=str(LATEST_QUALITY_AUDIT_FILE),
            latest_information_path=str(LATEST_INFORMATION_AUDIT_FILE),
            result_summary=(payload.get("outputs") or {}).get("summary", {}),
        )
    except Exception as exc:
        _set_run_state(status="failed", last_finished_at=_now(), last_error=str(exc))


def enqueue_factor_library_audit(**kwargs: Any) -> Any:
    kwargs = {**kwargs, "save_report": True}
    with _RUN_LOCK:
        global _RUN_THREAD
        running = bool(_RUN_THREAD and _RUN_THREAD.is_alive())
        if running:
            state = dict(_RUN_STATE)
            _write_run_state(state)
            return ok_result(
                outputs={**state, "request_accepted": False, "request_reason": "factor_library_audit_already_running"},
                artifacts={"state_file": str(LATEST_RUN_STATUS_FILE)},
                warnings=["factor_library_audit_already_running"],
            )
        _RUN_STATE.update(
            {
                "status": "queued",
                "scope": str(kwargs.get("scope") or "all"),
                "last_error": "",
                "last_requested_at": _now(),
                "last_started_at": "",
                "last_finished_at": "",
                "latest_quality_path": str(LATEST_QUALITY_AUDIT_FILE),
                "latest_information_path": str(LATEST_INFORMATION_AUDIT_FILE),
                "request_accepted": True,
                "request_reason": "",
                "result_summary": {},
            }
        )
        _RUN_THREAD = threading.Thread(
            target=_run_audit_background,
            kwargs={"kwargs": kwargs},
            name="fxalpha-factor-library-audit",
            daemon=True,
        )
        _RUN_THREAD.start()
        state = dict(_RUN_STATE)
    _write_run_state(state)
    return ok_result(outputs=state, artifacts={"state_file": str(LATEST_RUN_STATUS_FILE)})


def factor_library_audit_run_status() -> Any:
    state = _load_run_state()
    return ok_result(outputs=state, artifacts={"state_file": str(LATEST_RUN_STATUS_FILE)})
