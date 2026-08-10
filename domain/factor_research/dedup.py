"""
Factor deduplication gate — pairwise cross-sectional factor correlation.

Pattern: RD-Agent's deduplicate_new_factors (QlibFactorRunner).
Computes per-date cross-sectional Pearson correlation between new candidate
factors and existing active factors. Rejects candidates too correlated
with any existing factor.
"""

import logging
import random
import re

import numpy as np
import pandas as pd

from domain.factor_research.st_exposure_guard import (
    candidate_flipped_low_side,
    combined_novelty_st_guard,
    evaluate_st_exposure_from_factor_values,
    st_exposure_unavailable,
)
from storage.paths import get_live_st_exposure_guard_mode

logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────

_DEFAULT_CORR_THRESHOLD = 0.75   # max allowable Pearson r with any existing factor
_SAMPLE_STOCKS = 300             # number of stocks to sample for correlation check
_SAMPLE_DATES = 80               # number of trading days to sample (≈4 months)
_ST_ADVISORY_TAG = "distress_proxy_exposure"

# ── Public API ──────────────────────────────────────────


def deduplicate_new_factors(
    new_expressions: list[tuple[str, str]],  # [(expr, label), ...]
    existing_expressions: list[tuple[str, str]],  # [(expr, label_or_id), ...]
    market_df: pd.DataFrame,
    threshold: float = _DEFAULT_CORR_THRESHOLD,
    sample_stocks: int = _SAMPLE_STOCKS,
    sample_dates: int = _SAMPLE_DATES,
) -> dict:
    """Check new factors against existing ones via cross-sectional value correlation.

    Args:
        new_expressions: List of (expression, label) for new candidate factors.
        existing_expressions: List of (expression, label) for existing active factors.
        market_df: Per-stock-format DataFrame (columns: trade_date, stock_code, open,
            high, low, close, volume, amount, pe, pb, roe, ...).
        threshold: Max allowable Pearson r (default 0.95). A new factor with
            max correlation > threshold to ANY existing factor is flagged.
        sample_stocks: Number of stocks to randomly sample (for speed).
        sample_dates: Number of most recent trading days to sample.

    Returns:
        {
            "results": [
                {
                    "expression": ...,
                    "label": ...,
                    "status": "unique" | "duplicate",
                    "max_corr_with_existing": float,
                    "matched_existing_factor": str | None,  # label of most similar factor
                    "all_correlations": {existing_label: corr, ...}
                },
                ...
            ],
            "unique_count": int,
            "duplicate_count": int,
        }
    """
    if not new_expressions:
        return {"results": [], "unique_count": 0, "duplicate_count": 0}
    if not existing_expressions:
        # No existing factors to check against — all are unique
        return {
            "results": [
                {"expression": e, "label": l, "status": "unique",
                 "max_corr_with_existing": 0.0, "matched_existing_factor": None,
                 "all_correlations": {}}
                for e, l in new_expressions
            ],
            "unique_count": len(new_expressions),
            "duplicate_count": 0,
        }

    # Sample data for speed
    sampled_df = _sample_market_data(market_df, sample_stocks, sample_dates)

    # Compute factor values for ALL expressions (new + existing)
    all_expr = []
    all_expr.extend(existing_expressions)
    all_expr.extend(new_expressions)

    n_existing = len(existing_expressions)
    factor_values_df = _compute_factor_values_batch(sampled_df, all_expr)
    if factor_values_df is None or factor_values_df.empty:
        logger.warning("[dedup] Factor value computation failed — failing closed")
        return {
            "results": [
                {"expression": e, "label": l, "status": "duplicate",
                 "max_corr_with_existing": 1.0, "matched_existing_factor": "dedup_unavailable",
                 "all_correlations": {}}
                for e, l in new_expressions
            ],
            "unique_count": 0,
            "duplicate_count": len(new_expressions),
        }

    # RD-Agent pattern: group by date, compute pairwise Pearson correlations
    # factor_values_df has MultiIndex (trade_date, stock_code), columns = [label_0 ... label_N]
    pair_corrs = _compute_pairwise_correlations(factor_values_df, n_existing)
    # pair_corrs[i][j] = correlation between existing[i] and new[j] (mean across dates)

    # Classify each new factor
    results = []
    for j, (expr, label) in enumerate(new_expressions):
        corrs_with_existing = {existing_expressions[i][1]: pair_corrs[i][j]
                               for i in range(n_existing)}
        max_corr = max(corrs_with_existing.values()) if corrs_with_existing else 0.0
        best_match = max(corrs_with_existing, key=corrs_with_existing.get) if corrs_with_existing else None

        if max_corr > threshold:
            status = "duplicate"
        else:
            status = "unique"

        results.append({
            "expression": expr,
            "label": label,
            "status": status,
            "max_corr_with_existing": round(max_corr, 4),
            "matched_existing_factor": best_match,
            "all_correlations": {k: round(v, 4) for k, v in corrs_with_existing.items()},
        })

    unique_count = sum(1 for r in results if r["status"] == "unique")
    dupe_count = sum(1 for r in results if r["status"] == "duplicate")

    return {"results": results, "unique_count": unique_count, "duplicate_count": dupe_count}


def dedup_filter(
    new_expressions: list[tuple[str, str]],
    existing_expressions: list[tuple[str, str]],
    market_df: pd.DataFrame,
    threshold: float = _DEFAULT_CORR_THRESHOLD,
) -> list[str]:
    """Convenience: return only unique expressions.

    Returns list of (expr, label) tuples that passed the dedup check.
    """
    result = deduplicate_new_factors(new_expressions, existing_expressions, market_df, threshold)
    kept = [r for r in result["results"] if r["status"] == "unique"]
    return kept


def assess_active_pool_novelty(
    candidates: list[dict],
    *,
    start_date: str,
    end_date: str,
    extra_existing_candidates: list[dict] | None = None,
    information_cluster_by_factor_id: dict[str, str] | None = None,
    information_region_by_factor_id: dict[str, str] | None = None,
    factor_map_id: str = "",
    factor_map_audit_id: str = "",
    pearson_threshold: float = 0.75,
    rank_threshold: float = 0.80,
    p90_pearson_threshold: float | None = None,
    p90_rank_threshold: float | None = None,
) -> dict:
    """Assess whether candidates add enough information beyond the active factor pool.

    This follows the RD-Agent vector-mode spirit: compare actual factor values,
    not guessed semantic families. A candidate is vetoed when it is too similar
    to an existing active factor in Pearson or Rank correlation.
    """
    from storage.factor_registry import FactorRegistry
    from storage.paths import QLIB_DATA_ROOT, QUANTGPT_CODE_ROOT

    p90_pearson_threshold = p90_pearson_threshold if p90_pearson_threshold is not None else max(0.0, pearson_threshold - 0.05)
    p90_rank_threshold = p90_rank_threshold if p90_rank_threshold is not None else max(0.0, rank_threshold - 0.05)

    if not candidates:
        return {"keepers": [], "dropped": [], "details": [], "feedback": ""}

    # Batch de-duplication should keep the strongest quick-screen survivor when
    # near-neighbor candidates compete for the same information.
    candidates = sorted(
        candidates,
        key=lambda candidate: (
            _candidate_quick_score(candidate),
            str(candidate.get("candidate_id") or candidate.get("id") or ""),
        ),
        reverse=True,
    )

    registry = FactorRegistry()
    target_holding_period = None
    for candidate in candidates:
        for value in (
            candidate.get("holding_period_days"),
            candidate.get("holding_period"),
            (candidate.get("params") or {}).get("holding_period") if isinstance(candidate.get("params"), dict) else None,
        ):
            try:
                if value is not None:
                    target_holding_period = int(value)
                    break
            except Exception:
                continue
        if target_holding_period is not None:
            break
    existing = registry.list_active(holding_period_days=target_holding_period)
    cluster_by_factor_id = information_cluster_by_factor_id if isinstance(information_cluster_by_factor_id, dict) else {}
    region_by_factor_id = information_region_by_factor_id if isinstance(information_region_by_factor_id, dict) else {}
    existing_exprs: list[tuple[str, str]] = []
    reference_metadata: dict[str, dict] = {}
    existing_norm_to_label: dict[str, str] = {}
    for idx, item in enumerate(existing):
        expr = item.get("expression") or item.get("name")
        if expr:
            label = f"active_{idx}"
            factor_id = str(item.get("factor_id") or "").strip()
            existing_exprs.append((expr, label))
            existing_norm_to_label.setdefault(_normalize_expression(expr), label)
            reference_metadata[label] = {
                "source": "active_registry",
                "factor_id": factor_id or label,
                "name": item.get("name") or factor_id or label,
                "expression": expr,
                "information_cluster_id": cluster_by_factor_id.get(factor_id),
                "region_uid": region_by_factor_id.get(factor_id),
                "factor_map_id": factor_map_id or None,
                "factor_map_audit_id": factor_map_audit_id or None,
            }
    for idx, item in enumerate(extra_existing_candidates or []):
        expr = item.get("expression")
        if expr:
            label = f"session_{idx}"
            candidate_id = str(item.get("candidate_id") or item.get("id") or idx).strip()
            existing_exprs.append((expr, label))
            existing_norm_to_label.setdefault(_normalize_expression(expr), label)
            reference_metadata[label] = {
                "source": "session_candidate",
                "factor_id": f"session:{candidate_id}",
                "name": item.get("factor_name") or item.get("name") or candidate_id,
                "expression": expr,
                "information_cluster_id": item.get("information_cluster_id") or item.get("matched_information_cluster_id"),
                "region_uid": item.get("region_uid") or item.get("matched_region_uid"),
                "factor_map_id": item.get("factor_map_id") or factor_map_id or None,
                "factor_map_audit_id": item.get("factor_map_audit_id") or factor_map_audit_id or None,
            }

    pre_dropped: list[dict] = []
    pre_details: list[dict] = []
    unique_candidates: list[dict] = []
    seen_candidate_exprs: dict[str, str] = {}
    for idx, candidate in enumerate(candidates):
        expr = candidate.get("expression", "")
        norm = _normalize_expression(expr)
        matched = seen_candidate_exprs.get(norm)
        if norm in existing_norm_to_label:
            matched = existing_norm_to_label[norm]
        if matched:
            guard = _novelty_guard(
                False,
                "low_information_gain",
                1.0,
                1.0,
                matched,
                p90_pearson=1.0,
                p90_rank_corr=1.0,
                max_pearson=1.0,
                max_rank_corr=1.0,
                pearson_threshold=pearson_threshold,
                rank_threshold=rank_threshold,
                p90_pearson_threshold=p90_pearson_threshold,
                p90_rank_threshold=p90_rank_threshold,
                matched_metadata=reference_metadata.get(matched),
            )
            candidate["novelty_guard"] = guard
            pre_dropped.append(candidate)
            pre_details.append(_novelty_detail(candidate, guard))
            continue
        candidate_label = f"candidate_{len(unique_candidates)}"
        candidate_id = str(candidate.get("candidate_id") or candidate.get("id") or idx).strip()
        seen_candidate_exprs[norm] = candidate_label
        reference_metadata[candidate_label] = {
            "source": "batch_candidate",
            "factor_id": f"session:{candidate_id}",
            "name": candidate.get("factor_name") or candidate.get("name") or candidate_id,
            "expression": expr,
            "information_cluster_id": candidate.get("information_cluster_id") or candidate.get("matched_information_cluster_id"),
            "region_uid": candidate.get("region_uid") or candidate.get("matched_region_uid"),
            "factor_map_id": candidate.get("factor_map_id") or factor_map_id or None,
            "factor_map_audit_id": candidate.get("factor_map_audit_id") or factor_map_audit_id or None,
        }
        unique_candidates.append(candidate)

    candidates = unique_candidates
    if not existing_exprs:
        for candidate in candidates:
            candidate["novelty_guard"] = _novelty_guard(
                True,
                "no_existing_active_pool",
                0.0,
                0.0,
                None,
                pearson_threshold=pearson_threshold,
                rank_threshold=rank_threshold,
                p90_pearson_threshold=p90_pearson_threshold,
                p90_rank_threshold=p90_rank_threshold,
            )
        keepers, st_dropped, st_details, st_feedback = _apply_st_exposure_gate_from_storage(
            candidates,
            start_date=start_date,
            end_date=end_date,
        )
        dropped = pre_dropped + st_dropped
        details = pre_details + st_details
        return {
            "keepers": keepers,
            "dropped": dropped,
            "details": details,
            "feedback": _combine_feedback(_build_low_info_feedback(pre_dropped), st_feedback),
        }
    if not candidates:
        return {
            "keepers": [],
            "dropped": pre_dropped,
            "details": pre_details,
            "feedback": _build_low_info_feedback(pre_dropped),
        }

    all_exprs = existing_exprs + [
        (c.get("expression", ""), f"candidate_{idx}")
        for idx, c in enumerate(candidates)
    ]

    try:
        import sys
        if str(QUANTGPT_CODE_ROOT) not in sys.path:
            sys.path.insert(0, str(QUANTGPT_CODE_ROOT))
        from quantgpt.market_data import MarketDataFetcher

        mf = MarketDataFetcher()
        inst_path = QLIB_DATA_ROOT / "instruments" / "all.txt"
        raw_instruments = inst_path.read_text().strip().splitlines() if inst_path.exists() else []
        # Qlib instrument rows are usually "code<TAB>start<TAB>end"; novelty only
        # needs the code. Treating the whole row as a code silently disables the
        # factor-value correlation check.
        instruments = [line.split()[0] for line in raw_instruments if line.strip()]
        novelty_instruments = list(instruments)
        if len(novelty_instruments) > 500:
            rng = random.Random(42)
            novelty_instruments = rng.sample(novelty_instruments, 500)
        frames = _load_cached_market_frames(mf, novelty_instruments, start_date, end_date, min_rows=5)
        if not frames:
            return _novelty_unavailable_result(
                candidates,
                pre_dropped=pre_dropped,
                pre_details=pre_details,
                reason="novelty_market_data_unavailable",
                pearson_threshold=pearson_threshold,
                rank_threshold=rank_threshold,
                p90_pearson_threshold=p90_pearson_threshold,
                p90_rank_threshold=p90_rank_threshold,
            )
        market_df = pd.concat(frames, ignore_index=True)
        sample_dates = max(_SAMPLE_DATES, _max_timeseries_lookback(all_exprs) + 40)
        market_df = _sample_market_data(
            market_df,
            n_stocks=_SAMPLE_STOCKS,
            n_dates=sample_dates,
        )
    except Exception as exc:
        logger.warning("[dedup] novelty market load failed: %s", exc)
        return _novelty_unavailable_result(
            candidates,
            pre_dropped=pre_dropped,
            pre_details=pre_details,
            reason="novelty_market_load_failed",
            pearson_threshold=pearson_threshold,
            rank_threshold=rank_threshold,
            p90_pearson_threshold=p90_pearson_threshold,
            p90_rank_threshold=p90_rank_threshold,
        )

    factor_df = _compute_factor_values_batch(market_df, all_exprs)
    if factor_df is None or factor_df.empty:
        return _novelty_unavailable_result(
            candidates,
            pre_dropped=pre_dropped,
            pre_details=pre_details,
            reason="novelty_factor_values_unavailable",
            pearson_threshold=pearson_threshold,
            rank_threshold=rank_threshold,
            p90_pearson_threshold=p90_pearson_threshold,
            p90_rank_threshold=p90_rank_threshold,
        )

    n_existing = len(existing_exprs)
    n_new = len(candidates)
    labels = list(factor_df.columns)
    existing_labels_present = [label for _expr, label in existing_exprs if label in labels]

    keepers: list[dict] = []
    dropped: list[dict] = list(pre_dropped)
    details: list[dict] = list(pre_details)
    accepted_candidate_indices: list[int] = []
    for j, candidate in enumerate(candidates):
        new_label = f"candidate_{j}"
        if new_label not in labels:
            guard = _novelty_guard(
                False,
                "novelty_candidate_values_unavailable",
                0.0,
                0.0,
                "novelty_unavailable",
                pearson_threshold=pearson_threshold,
                rank_threshold=rank_threshold,
                p90_pearson_threshold=p90_pearson_threshold,
                p90_rank_threshold=p90_rank_threshold,
            )
            candidate["novelty_guard"] = guard
            dropped.append(candidate)
            details.append(_novelty_detail(candidate, guard))
            continue
        reference_labels = existing_labels_present + [f"candidate_{idx}" for idx in accepted_candidate_indices]
        if not reference_labels:
            guard = _novelty_guard(
                False,
                "novelty_reference_values_unavailable",
                0.0,
                0.0,
                "novelty_unavailable",
                pearson_threshold=pearson_threshold,
                rank_threshold=rank_threshold,
                p90_pearson_threshold=p90_pearson_threshold,
                p90_rank_threshold=p90_rank_threshold,
            )
            candidate["novelty_guard"] = guard
            dropped.append(candidate)
            details.append(_novelty_detail(candidate, guard))
            continue
        best_stats = _empty_corr_stats()
        matched = None
        for ref_label in reference_labels:
            stats = _daily_correlation_stats(factor_df, ref_label, new_label)
            if _stats_strength(stats) > _stats_strength(best_stats):
                best_stats = stats
                matched = ref_label

        max_pearson = best_stats["mean_pearson"]
        max_rank = best_stats["mean_rank_corr"]
        p90_pearson = best_stats["p90_pearson"]
        p90_rank_corr = best_stats["p90_rank_corr"]
        too_similar = (
            max_pearson >= pearson_threshold
            or max_rank >= rank_threshold
            or p90_pearson >= p90_pearson_threshold
            or p90_rank_corr >= p90_rank_threshold
        )
        guard = _novelty_guard(
            not too_similar,
            "low_information_gain" if too_similar else "novel_increment",
            max_pearson,
            max_rank,
            matched,
            p90_pearson=p90_pearson,
            p90_rank_corr=p90_rank_corr,
            max_pearson=best_stats["max_pearson"],
            max_rank_corr=best_stats["max_rank_corr"],
            pearson_threshold=pearson_threshold,
            rank_threshold=rank_threshold,
            p90_pearson_threshold=p90_pearson_threshold,
            p90_rank_threshold=p90_rank_threshold,
            matched_metadata=reference_metadata.get(matched),
        )
        candidate["novelty_guard"] = guard
        if too_similar:
            dropped.append(candidate)
            details.append(_novelty_detail(candidate, guard))
        else:
            keepers.append(candidate)
            accepted_candidate_indices.append(j)

    novelty_feedback = _build_low_info_feedback(dropped)
    keepers, st_dropped, st_details, st_feedback = _apply_st_exposure_gate(
        keepers,
        market_fetcher=mf if "mf" in locals() else None,
        instruments=instruments if "instruments" in locals() else [],
        start_date=start_date,
        end_date=end_date,
    )
    dropped.extend(st_dropped)
    details.extend(st_details)
    feedback = _combine_feedback(novelty_feedback, st_feedback)
    return {"keepers": keepers, "dropped": dropped, "details": details, "feedback": feedback}


def _normalize_expression(expression: str) -> str:
    return re.sub(r"\s+", "", str(expression or "")).lower()


def _candidate_quick_score(candidate: dict) -> float:
    sources = (
        candidate.get("quick_score"),
        candidate.get("score"),
        candidate.get("qgpt_score"),
        (candidate.get("metrics") or {}).get("quick_score") if isinstance(candidate.get("metrics"), dict) else None,
        (candidate.get("screening") or {}).get("quick_score") if isinstance(candidate.get("screening"), dict) else None,
        (candidate.get("gate_result") or {}).get("quick_score") if isinstance(candidate.get("gate_result"), dict) else None,
    )
    for value in sources:
        try:
            if value is not None:
                return float(value)
        except Exception:
            continue
    return float("-inf")


def _max_timeseries_lookback(expressions: list[tuple[str, str]]) -> int:
    """Best-effort lookback estimate for sampling enough warm history."""
    max_window = 0
    for expr, _label in expressions:
        for match in re.finditer(r"ts_[a-zA-Z_]+\([^,]+,\s*(\d+)", str(expr or "")):
            try:
                max_window = max(max_window, int(match.group(1)))
            except Exception:
                continue
    return max_window


def _novelty_unavailable_result(
    candidates: list[dict],
    *,
    pre_dropped: list[dict],
    pre_details: list[dict],
    reason: str,
    pearson_threshold: float,
    rank_threshold: float,
    p90_pearson_threshold: float | None,
    p90_rank_threshold: float | None,
) -> dict:
    dropped = list(pre_dropped)
    details = list(pre_details)
    for candidate in candidates:
        guard = _novelty_guard(
            False,
            reason,
            0.0,
            0.0,
            "novelty_unavailable",
            pearson_threshold=pearson_threshold,
            rank_threshold=rank_threshold,
            p90_pearson_threshold=p90_pearson_threshold,
            p90_rank_threshold=p90_rank_threshold,
        )
        candidate["novelty_guard"] = guard
        dropped.append(candidate)
        details.append(_novelty_detail(candidate, guard))
    feedback = (
        f"Novelty factor-value correlation could not be computed ({reason}). "
        "Fail closed: do not import or treat candidates as novel until factor values are available."
    )
    low_info_feedback = _build_low_info_feedback(pre_dropped)
    if low_info_feedback:
        feedback = f"{low_info_feedback} {feedback}"
    return {"keepers": [], "dropped": dropped, "details": details, "feedback": feedback}


def _novelty_guard(
    allowed: bool,
    reason: str,
    pearson: float,
    rank_corr: float,
    matched: str | None,
    *,
    p90_pearson: float = 0.0,
    p90_rank_corr: float = 0.0,
    max_pearson: float = 0.0,
    max_rank_corr: float = 0.0,
    pearson_threshold: float = 0.75,
    rank_threshold: float = 0.80,
    p90_pearson_threshold: float | None = None,
    p90_rank_threshold: float | None = None,
    matched_metadata: dict | None = None,
) -> dict:
    p90_pearson_threshold = p90_pearson_threshold if p90_pearson_threshold is not None else max(0.0, pearson_threshold - 0.05)
    p90_rank_threshold = p90_rank_threshold if p90_rank_threshold is not None else max(0.0, rank_threshold - 0.05)
    metadata = matched_metadata if isinstance(matched_metadata, dict) else {}
    matched_factor_id = str(metadata.get("factor_id") or matched or "").strip() or None
    result = {
        "allowed": allowed,
        "reason": reason,
        "max_existing_pearson": round(float(pearson), 4),
        "max_existing_rank_corr": round(float(rank_corr), 4),
        "p90_pearson": round(float(p90_pearson), 4),
        "p90_rank_corr": round(float(p90_rank_corr), 4),
        "max_pearson": round(float(max_pearson), 4),
        "max_rank_corr": round(float(max_rank_corr), 4),
        "novelty_score": round(
            0.0 if not allowed else max(
                0.0,
                1.0 - max(
                    abs(float(pearson)),
                    abs(float(rank_corr)),
                    abs(float(p90_pearson)),
                    abs(float(p90_rank_corr)),
                ),
            ),
            4,
        ),
        # Compatibility field retained for existing consumers.  It now carries
        # the real registry factor_id (or a stable session:<candidate_id>)
        # instead of the internal dataframe label such as active_3.
        "matched_existing_factor": matched_factor_id,
        "matched_existing_factor_id": matched_factor_id,
        "matched_reference_source": metadata.get("source"),
        "matched_existing_factor_name": metadata.get("name"),
        "matched_existing_expression_summary": str(metadata.get("expression") or "")[:220] or None,
        "matched_information_cluster_id": metadata.get("information_cluster_id"),
        "matched_region_uid": metadata.get("region_uid"),
        "factor_map_id": metadata.get("factor_map_id"),
        "factor_map_audit_id": metadata.get("factor_map_audit_id"),
        "thresholds": {
            "pearson": pearson_threshold,
            "rank_corr": rank_threshold,
            "p90_pearson": p90_pearson_threshold,
            "p90_rank_corr": p90_rank_threshold,
        },
    }
    return {key: value for key, value in result.items() if value is not None}


def _novelty_detail(candidate: dict, guard: dict) -> dict:
    return {
        "candidate_id": candidate.get("candidate_id") or candidate.get("id"),
        "expression": candidate.get("expression", "")[:80],
        "reason": guard.get("reason") or "low_information_gain",
        "matched_existing_factor": guard.get("matched_existing_factor"),
        "matched_existing_factor_id": guard.get("matched_existing_factor_id"),
        "matched_reference_source": guard.get("matched_reference_source"),
        "matched_existing_factor_name": guard.get("matched_existing_factor_name"),
        "matched_existing_expression_summary": guard.get("matched_existing_expression_summary"),
        "matched_information_cluster_id": guard.get("matched_information_cluster_id"),
        "matched_region_uid": guard.get("matched_region_uid"),
        "factor_map_id": guard.get("factor_map_id"),
        "factor_map_audit_id": guard.get("factor_map_audit_id"),
        "max_existing_pearson": guard.get("max_existing_pearson"),
        "max_existing_rank_corr": guard.get("max_existing_rank_corr"),
        "p90_pearson": guard.get("p90_pearson"),
        "p90_rank_corr": guard.get("p90_rank_corr"),
        "max_pearson": guard.get("max_pearson"),
        "max_rank_corr": guard.get("max_rank_corr"),
        "thresholds": guard.get("thresholds", {}),
    }


def _load_cached_market_frames(market_fetcher, instruments: list[str], start_date: str, end_date: str, *, min_rows: int) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    for qcode in instruments:
        qcode = str(qcode)
        if qcode.endswith("sh"):
            bs = qcode[:6].upper() + ".SH"
        elif qcode.endswith("sz"):
            bs = qcode[:6].upper() + ".SZ"
        else:
            continue
        df = market_fetcher._load_cache(bs)
        if df is not None and len(df) >= min_rows:
            df = df.copy()
            if "trade_date" in df.columns:
                df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)]
            if len(df) >= min_rows:
                frames.append(df)
    return frames


def _apply_st_exposure_gate(
    keepers: list[dict],
    *,
    market_fetcher,
    instruments: list[str],
    start_date: str,
    end_date: str,
) -> tuple[list[dict], list[dict], list[dict], str]:
    if not keepers:
        return [], [], [], ""
    mode = get_live_st_exposure_guard_mode()
    if market_fetcher is None or not instruments:
        dropped: list[dict] = []
        details: list[dict] = []
        for candidate in keepers:
            guard = _decorate_st_exposure_guard(
                st_exposure_unavailable("st_exposure_market_data_unavailable"),
                mode=mode,
            )
            _attach_st_exposure_guard(candidate, guard)
            candidate["combined_guard"] = combined_novelty_st_guard(candidate)
            if _st_exposure_blocks(guard):
                dropped.append(candidate)
            details.append(_combined_detail(candidate))
        return ([] if dropped else keepers), dropped, details, _build_st_feedback(dropped)

    try:
        st_frames = _load_cached_market_frames(market_fetcher, instruments, start_date, end_date, min_rows=5)
        if not st_frames:
            raise ValueError("st_exposure_market_data_unavailable")
        st_market_df = pd.concat(st_frames, ignore_index=True)
        expressions = [(candidate.get("expression", ""), f"candidate_{idx}") for idx, candidate in enumerate(keepers)]
        st_factor_df = _compute_factor_values_batch(st_market_df, expressions)
    except Exception as exc:
        logger.warning("[dedup] ST exposure guard failed to compute factor values: %s", exc)
        st_factor_df = None

    passed: list[dict] = []
    dropped = []
    details = []
    for idx, candidate in enumerate(keepers):
        label = f"candidate_{idx}"
        if st_factor_df is None or label not in getattr(st_factor_df, "columns", []):
            guard = st_exposure_unavailable("st_exposure_factor_values_unavailable")
        else:
            guard = evaluate_st_exposure_from_factor_values(
                st_factor_df[label],
                flipped_low_side=candidate_flipped_low_side(candidate),
            )
        guard = _decorate_st_exposure_guard(guard, mode=mode)
        _attach_st_exposure_guard(candidate, guard)
        candidate["combined_guard"] = combined_novelty_st_guard(candidate)
        details.append(_combined_detail(candidate))
        if guard.get("passed") is True or not _st_exposure_blocks(guard):
            passed.append(candidate)
        else:
            dropped.append(candidate)
    return passed, dropped, details, _build_st_feedback(dropped)


def _apply_st_exposure_gate_from_storage(
    keepers: list[dict],
    *,
    start_date: str,
    end_date: str,
) -> tuple[list[dict], list[dict], list[dict], str]:
    try:
        import sys
        from storage.paths import QLIB_DATA_ROOT, QUANTGPT_CODE_ROOT

        if str(QUANTGPT_CODE_ROOT) not in sys.path:
            sys.path.insert(0, str(QUANTGPT_CODE_ROOT))
        from quantgpt.market_data import MarketDataFetcher

        inst_path = QLIB_DATA_ROOT / "instruments" / "all.txt"
        raw_instruments = inst_path.read_text().strip().splitlines() if inst_path.exists() else []
        instruments = [line.split()[0] for line in raw_instruments if line.strip()]
        return _apply_st_exposure_gate(
            keepers,
            market_fetcher=MarketDataFetcher(),
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as exc:
        logger.warning("[dedup] ST exposure storage load failed: %s", exc)
        dropped: list[dict] = []
        details: list[dict] = []
        for candidate in keepers:
            mode = get_live_st_exposure_guard_mode()
            guard = _decorate_st_exposure_guard(
                st_exposure_unavailable("st_exposure_market_data_unavailable"),
                mode=mode,
            )
            _attach_st_exposure_guard(candidate, guard)
            candidate["combined_guard"] = combined_novelty_st_guard(candidate)
            if _st_exposure_blocks(guard):
                dropped.append(candidate)
            details.append(_combined_detail(candidate))
        return ([] if dropped else keepers), dropped, details, _build_st_feedback(dropped)


def _combined_detail(candidate: dict) -> dict:
    novelty = candidate.get("novelty_guard") if isinstance(candidate.get("novelty_guard"), dict) else {}
    st_guard = candidate.get("st_exposure_guard") if isinstance(candidate.get("st_exposure_guard"), dict) else {}
    combined = candidate.get("combined_guard") if isinstance(candidate.get("combined_guard"), dict) else {}
    detail = _novelty_detail(candidate, novelty)
    detail.update(
        {
            "combined_allowed": combined.get("allowed"),
            "combined_reason": combined.get("reason"),
            "st_exposure_guard": st_guard,
        }
    )
    return detail


def _decorate_st_exposure_guard(guard: dict, *, mode: str) -> dict:
    decorated = dict(guard or {})
    normalized_mode = "advisory" if str(mode).strip().lower() == "advisory" else "hard"
    decorated.setdefault("scope", "counterfactual_all_market")
    decorated.setdefault("label", _ST_ADVISORY_TAG)
    decorated["mode"] = normalized_mode
    decorated["hard_veto"] = bool(normalized_mode == "hard" and decorated.get("passed") is not True)
    if normalized_mode == "advisory" and decorated.get("passed") is not True:
        decorated["advisory_flag"] = _ST_ADVISORY_TAG
    return decorated


def _attach_st_exposure_guard(candidate: dict, guard: dict) -> None:
    candidate["st_exposure_guard"] = guard
    if guard.get("advisory_flag"):
        tags = list(candidate.get("risk_tags") or [])
        if _ST_ADVISORY_TAG not in tags:
            tags.append(_ST_ADVISORY_TAG)
        candidate["risk_tags"] = tags


def _st_exposure_blocks(guard: dict) -> bool:
    return guard.get("passed") is not True and str(guard.get("mode") or "hard").strip().lower() != "advisory"


def _build_st_feedback(dropped: list[dict]) -> str:
    if not dropped:
        return ""
    parts = []
    for candidate in dropped[:5]:
        guard = candidate.get("st_exposure_guard") or {}
        parts.append(
            f"{candidate.get('candidate_id') or candidate.get('expression', '')[:32]} "
            f"avg={guard.get('avg_top50_ratio')} p95={guard.get('p95_top50_ratio')} reason={guard.get('reason')}"
        )
    return (
        f"{len(dropped)} candidates passed novelty but were vetoed by hard-mode distress_proxy_exposure. "
        "Do not run deep validation for hard-mode vetoes; in advisory mode this diagnostic is a risk tag only. "
        + "; ".join(parts)
    )


def _combine_feedback(*parts: str | None) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()


def _build_low_info_feedback(dropped: list[dict]) -> str:
    if not dropped:
        return ""
    reasons = {
        str((item.get("novelty_guard") or {}).get("reason") or "")
        for item in dropped
    }
    unavailable = sorted(reason for reason in reasons if "unavailable" in reason or "failed" in reason)
    if unavailable:
        return (
            f"{len(dropped)} candidates were vetoed because novelty factor-value correlation was not computable "
            f"({', '.join(unavailable)}). Fail closed: do not import or treat candidates as novel until factor values are available."
        )
    return (
        f"{len(dropped)} candidates were vetoed by the active-pool factor-value correlation / novelty gate. "
        "Do not reuse the same cross-sectional value pattern; change the signal source, conditioning logic, operator geometry, or economic mechanism materially. "
        "This is not a broad family ban."
    )


def _empty_corr_stats() -> dict[str, float]:
    return {
        "mean_pearson": 0.0,
        "mean_rank_corr": 0.0,
        "p90_pearson": 0.0,
        "p90_rank_corr": 0.0,
        "max_pearson": 0.0,
        "max_rank_corr": 0.0,
    }


def _stats_strength(stats: dict[str, float]) -> float:
    return max(
        abs(float(stats.get("mean_pearson", 0.0))),
        abs(float(stats.get("mean_rank_corr", 0.0))),
        abs(float(stats.get("p90_pearson", 0.0))),
        abs(float(stats.get("p90_rank_corr", 0.0))),
    )


def _daily_correlation_stats(factor_values: pd.DataFrame, ref_label: str, new_label: str) -> dict[str, float]:
    pearsons = []
    ranks = []
    for date in factor_values.index.get_level_values("trade_date").unique():
        try:
            day = factor_values.xs(date, level="trade_date")
        except Exception:
            continue
        if len(day) < 10 or ref_label not in day.columns or new_label not in day.columns:
            continue
        ref_col = day[ref_label]
        new_col = day[new_label]
        if isinstance(ref_col, pd.DataFrame):
            ref_col = ref_col.iloc[:, 0]
        if isinstance(new_col, pd.DataFrame):
            new_col = new_col.iloc[:, 0]
        ref_arr = ref_col.to_numpy(dtype=float)
        new_arr = new_col.to_numpy(dtype=float)
        pearson = _safe_pearson(ref_arr, new_arr)
        if pearson is not None:
            pearsons.append(pearson)
        rank_corr = _safe_rank_corr(ref_arr, new_arr)
        if rank_corr is not None:
            ranks.append(rank_corr)
    abs_pearsons = np.abs(np.asarray(pearsons, dtype=float)) if pearsons else np.asarray([], dtype=float)
    abs_ranks = np.abs(np.asarray(ranks, dtype=float)) if ranks else np.asarray([], dtype=float)
    return {
        "mean_pearson": float(np.mean(abs_pearsons)) if len(abs_pearsons) else 0.0,
        "mean_rank_corr": float(np.mean(abs_ranks)) if len(abs_ranks) else 0.0,
        "p90_pearson": float(np.quantile(abs_pearsons, 0.90)) if len(abs_pearsons) else 0.0,
        "p90_rank_corr": float(np.quantile(abs_ranks, 0.90)) if len(abs_ranks) else 0.0,
        "max_pearson": float(np.max(abs_pearsons)) if len(abs_pearsons) else 0.0,
        "max_rank_corr": float(np.max(abs_ranks)) if len(abs_ranks) else 0.0,
    }


# ── Internal helpers ────────────────────────────────────


def _sample_market_data(
    df: pd.DataFrame,
    n_stocks: int = _SAMPLE_STOCKS,
    n_dates: int = _SAMPLE_DATES,
) -> pd.DataFrame:
    """Sample a subset of stocks and most recent dates for speed."""
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])

    # Get unique stocks
    stocks = df["stock_code"].unique()
    if len(stocks) > n_stocks:
        rng = random.Random(42)
        sampled_stocks = set(rng.sample(list(stocks), n_stocks))
        df = df[df["stock_code"].isin(sampled_stocks)]

    # Get most recent dates
    dates = sorted(df["trade_date"].unique())
    if len(dates) > n_dates:
        cutoff = dates[-n_dates]
        df = df[df["trade_date"] >= cutoff]

    # Sort for time-series operators
    df = df.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)

    return df


def _compute_factor_values_batch(
    df: pd.DataFrame,
    expressions: list[tuple[str, str]],
) -> pd.DataFrame | None:
    """Compute factor values for all expressions on sampled data.

    Critical: DataFrame must be sorted by (stock_code, trade_date) so that
    time-series operators (ts_shift, ts_mean, etc.) work correctly per-stock.

    Returns DataFrame with:
        Index: MultiIndex (trade_date, stock_code)
        Columns: expression labels (in the order of expressions param)
        Values: factor scores (z-scored cross-sectionally per date)
    """
    from quantgpt.factor_evaluator import evaluate_factor_series

    # Ensure proper sorting for per-stock time-series ops
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)

    # Sanity: each stock should have enough rows for time-series operators.
    # Cap the minimum by the actual sampled window; short novelty windows used
    # to require 60 rows even when the window had fewer trading days, which
    # filtered out every stock and made factor-value novelty unavailable.
    stock_counts = df.groupby("stock_code").size()
    available_dates = int(df["trade_date"].nunique())
    min_days = min(60, max(30, available_dates // 2), available_dates)
    valid_stocks = stock_counts[stock_counts >= min_days].index
    if len(valid_stocks) < len(stock_counts):
        df = df[df["stock_code"].isin(valid_stocks)]
        logger.info(f"[dedup] Excluded {len(stock_counts) - len(valid_stocks)} stocks with <{min_days} rows")
    if df.empty:
        logger.warning("[dedup] No stocks remain after minimum history filter")
        return None

    # Drop columns that expression_parser might choke on
    # Keep only our standard columns + the 31 field columns
    base_cols = {"trade_date", "stock_code", "open", "high", "low", "close",
                 "volume", "amount", "pct_change", "pre_close", "pe", "pb",
                 "ps", "roe", "roa", "eps", "total_mv", "float_mv",
                 "turnover_rate", "net_profit", "tot_equity", "total_assets",
                 "net_asset_ps", "tot_share", "float_a_share", "holder_num",
                 "amp", "borrow_money_bal",
                 "purch_borrow_money", "sec_lending_bal", "margin_trade_bal"}
    extra_cols = [c for c in df.columns if c not in base_cols]
    for c in extra_cols:
        if c not in ("security_name", "list_date"):
            continue
        df = df.drop(columns=[c], errors="ignore")

    dfs = []
    for expr, label in expressions:
        try:
            values = evaluate_factor_series(df, expr, universe="tradable_non_st")
            if values is None or len(values) == 0 or values.notna().sum() == 0:
                logger.warning(f"[dedup] Expression '{label}' returned no valid values")
                continue
            result = pd.DataFrame({
                "trade_date": df["trade_date"].values,
                "stock_code": df["stock_code"].values,
                "factor_value": values.values,
                "label": label,
            })
            dfs.append(result)
        except Exception as e:
            logger.warning(f"[dedup] Failed to compute '{label}': {e}")
            continue

    if not dfs:
        return None

    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.dropna(subset=["factor_value"])

    if len(combined) == 0:
        return None

    # Z-score per date per label (normalize factor values cross-sectionally)
    # Group by date + label because each row is one stock-date-label
    combined["zscore"] = combined.groupby(["trade_date", "label"], group_keys=False)["factor_value"].transform(
        lambda x: (x - x.mean()) / (x.std() + 1e-10)
    )

    # Pivot: index=(date, stock), columns=label, values=zscore
    # Use pivot_table with explicit column order matching the input expressions
    labels_order = [label for _, label in expressions]
    combined["idx"] = combined["trade_date"].astype(str) + "_XX_" + combined["stock_code"]
    pivot = combined.pivot_table(
        index="idx",
        columns="label",
        values="zscore",
        aggfunc="first",
    )

    # Reorder columns to match input order (pivot_table may sort alphabetically)
    existing_cols = [c for c in labels_order if c in pivot.columns]
    pivot = pivot[existing_cols]

    # Split idx back into multi-index
    pivot = pivot.reset_index()
    parts = pivot["idx"].str.split("_XX_", n=1, expand=True)
    pivot["trade_date"] = pd.to_datetime(parts[0])
    pivot["stock_code"] = parts[1]
    pivot = pivot.set_index(["trade_date", "stock_code"])
    pivot = pivot.drop(columns=["idx"])

    return pivot


def _compute_pairwise_correlations(
    factor_values: pd.DataFrame,
    n_existing: int,
) -> np.ndarray:
    """Compute mean cross-sectional Pearson r between existing and new factors.

    RD-Agent pattern: for each date, compute pairwise correlations between
    existing factor columns and new factor columns, then mean across dates.

    factor_values columns are in input order: [existing_0 ... existing_N-1, new_0 ... new_M-1]

    Returns:
        Array shape (n_existing, n_new) — mean Pearson r across all dates.
    """
    if factor_values is None or len(factor_values) == 0:
        return np.zeros((n_existing, 0))

    n_new = len(factor_values.columns) - n_existing
    if n_new <= 0:
        return np.zeros((n_existing, 0))

    existing_labels = list(factor_values.columns[:n_existing])
    new_labels = list(factor_values.columns[n_existing:])

    # Get unique dates
    dates = factor_values.index.get_level_values("trade_date").unique()
    n_dates = len(dates)
    if n_dates < 5:
        logger.warning(f"[dedup] Only {n_dates} dates for correlation — too few")
        return np.zeros((n_existing, n_new))

    # Accumulate correlation matrix across dates
    corr_sum = np.zeros((n_existing, n_new))
    valid_days = 0

    for date in dates:
        day_data = factor_values.xs(date, level="trade_date")

        # Skip dates with too few stocks
        if len(day_data) < 10:
            continue

        existing_vals = []
        for i in range(n_existing):
            col = existing_labels[i]
            if col not in day_data.columns:
                existing_vals.append(np.full(len(day_data), np.nan))
            else:
                # Handle duplicate column names (take first)
                col_data = day_data[col]
                if isinstance(col_data, pd.DataFrame):
                    col_data = col_data.iloc[:, 0]
                existing_vals.append(col_data.values.astype(float))

        new_vals = []
        for j in range(n_new):
            col = new_labels[j]
            if col not in day_data.columns:
                new_vals.append(np.full(len(day_data), np.nan))
            else:
                col_data = day_data[col]
                if isinstance(col_data, pd.DataFrame):
                    col_data = col_data.iloc[:, 0]
                new_vals.append(col_data.values.astype(float))

        # Pairwise Pearson correlation
        day_valid = False
        for i in range(n_existing):
            ei = existing_vals[i]
            valid_i = ~np.isnan(ei)
            for j in range(n_new):
                nj = new_vals[j]
                valid_j = ~np.isnan(nj)
                both_valid = valid_i & valid_j
                if both_valid.sum() < 10:
                    continue
                ei_clean = ei[both_valid]
                nj_clean = nj[both_valid]
                if np.std(ei_clean) < 1e-10 or np.std(nj_clean) < 1e-10:
                    continue
                corr = np.corrcoef(ei_clean, nj_clean)[0, 1]
                if not np.isnan(corr):
                    corr_sum[i, j] += corr
                    day_valid = True

        if day_valid:
            valid_days += 1

    if valid_days == 0:
        logger.warning("[dedup] No valid dates for correlation computation")
        return np.zeros((n_existing, n_new))

    # Mean across valid dates
    mean_corr = corr_sum / valid_days
    return mean_corr


def _safe_pearson(a: np.ndarray, b: np.ndarray) -> float | None:
    mask = ~(np.isnan(a) | np.isnan(b))
    if mask.sum() < 10:
        return None
    aa = a[mask]
    bb = b[mask]
    if np.std(aa) < 1e-10 or np.std(bb) < 1e-10:
        return None
    corr = np.corrcoef(aa, bb)[0, 1]
    return None if np.isnan(corr) else float(corr)


def _safe_rank_corr(a: np.ndarray, b: np.ndarray) -> float | None:
    mask = ~(np.isnan(a) | np.isnan(b))
    if mask.sum() < 10:
        return None
    aa = pd.Series(a[mask]).rank(method="average").to_numpy(dtype=float)
    bb = pd.Series(b[mask]).rank(method="average").to_numpy(dtype=float)
    if np.std(aa) < 1e-10 or np.std(bb) < 1e-10:
        return None
    corr = np.corrcoef(aa, bb)[0, 1]
    return None if np.isnan(corr) else float(corr)
