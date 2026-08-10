from __future__ import annotations

import hashlib
import json
import logging
import re as _re
from typing import Any
from typing import Callable

from domain.factor_research.dedup import assess_active_pool_novelty
from storage.paths import FACTOR_DEFAULT_HOLDING_PERIOD, get_live_st_exposure_guard_mode

logger = logging.getLogger(__name__)


def evaluate_candidate_quality(
    candidates: list[dict],
    *,
    start_date: str,
    end_date: str,
    min_abs_ic: float,
    min_ir: float,
    extra_existing_candidates: list[dict] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    stage: str = "round",
    round_no: int | None = None,
    family: str | None = None,
    trusted_novelty_evidence: bool = False,
) -> dict:
    """Run the post-validation asset/import gate once and return a report.

    QuantGPT's score_factor is only a quick screen. This gate is for candidates
    that have completed full/deep validation and are being considered as
    FXAlpha factor-library assets.
    """
    if not candidates:
        return _empty_quality_report()

    dedup_keepers, dedup_dropped, dedup_feedback = _dedup_round(candidates)
    reuse_novelty, reuse_reason = _can_reuse_novelty_evidence(
        dedup_keepers,
        start_date=start_date,
        end_date=end_date,
        extra_existing_candidates=extra_existing_candidates,
        requested=trusted_novelty_evidence,
    )
    if reuse_novelty:
        novelty_keepers: list[dict] = []
        novelty_dropped: list[dict] = []
        for candidate in dedup_keepers:
            novelty = _extract_novelty_guard(candidate)
            combined = candidate.get("combined_guard") if isinstance(candidate.get("combined_guard"), dict) else {}
            if novelty and novelty.get("allowed") is True and combined.get("allowed", True) is True:
                novelty_keepers.append(candidate)
            else:
                novelty_dropped.append(candidate)
        novelty_result = {
            "keepers": novelty_keepers,
            "dropped": novelty_dropped,
            "details": [],
            "feedback": "",
            "reused_trusted_evidence": True,
            "reuse_reason": reuse_reason,
        }
    else:
        novelty_result = assess_active_pool_novelty(
            dedup_keepers,
            start_date=start_date,
            end_date=end_date,
            extra_existing_candidates=extra_existing_candidates,
        )
        novelty_result["reused_trusted_evidence"] = False
        novelty_result["reuse_reason"] = reuse_reason
    novelty_keepers = novelty_result.get("keepers", dedup_keepers)
    novelty_dropped = novelty_result.get("dropped", [])
    novelty_feedback = novelty_result.get("feedback", "")

    gate_adopted, gate_rejected = apply_gate(novelty_keepers + novelty_dropped, min_abs_ic, min_ir)
    adopted: list[dict] = []
    rejected: list[dict] = []

    for candidate in gate_adopted:
        _normalize_candidate_evidence(candidate)
        screening = _attach_screening_evidence(candidate, screen_candidate(candidate))
        candidate["screening"] = screening
        if screening.get("allowed", True):
            adopted.append(candidate)
        else:
            rejected.append(_blocked_candidate(candidate, screening))

    for candidate in gate_rejected:
        _normalize_candidate_evidence(candidate)
        screening = _attach_screening_evidence(candidate, screen_candidate(candidate))
        candidate["screening"] = screening
        rejected.append(_blocked_candidate(candidate, screening))

    feedback_parts = [
        dedup_feedback,
        novelty_feedback,
        _screening_feedback(rejected) if rejected else "",
    ]
    feedback = " ".join(part for part in feedback_parts if part).strip()
    reasons = _reason_counts(rejected)

    report = {
        "adopted": adopted,
        "rejected": rejected,
        "screened_out": [],
        "dedup_dropped": dedup_dropped,
        "dedup_feedback": dedup_feedback,
        "novelty": novelty_result,
        "novelty_feedback": novelty_feedback,
        "feedback": feedback,
        "reason_counts": reasons,
        "counts": {
            "input": len(candidates),
            "gate_adopted": len(gate_adopted),
            "gate_rejected": len(gate_rejected),
            "diagnostic_screened_out": 0,
            "dedup_keepers": len(dedup_keepers),
            "dedup_dropped": len(dedup_dropped),
            "novelty_keepers": len(novelty_keepers),
            "novelty_dropped": len(novelty_dropped),
            "adopted": len(adopted),
            "screened_out": 0,
        },
    }

    _emit_quality_event(progress_callback, stage, round_no, family, report)
    return report


_NOVELTY_EVIDENCE_SCHEMA_VERSION = "factor_novelty_evidence_v1"


def attach_novelty_evidence(
    result: dict,
    *,
    start_date: str,
    end_date: str,
    extra_existing_candidates: list[dict] | None = None,
    pearson_threshold: float = 0.75,
    rank_threshold: float = 0.80,
    p90_pearson_threshold: float | None = None,
    p90_rank_threshold: float | None = None,
) -> dict:
    """Attest novelty outputs so a later final gate can safely reuse them.

    The attestation is deliberately local to the candidate payload.  It is not
    another state store and it does not decide import eligibility by itself;
    ``evaluate_candidate_quality`` revalidates the surrounding context before
    accepting it.
    """
    if not isinstance(result, dict):
        return result
    base = _novelty_evidence_base(
        start_date=start_date,
        end_date=end_date,
        candidates=_novelty_result_candidates(result),
        extra_existing_candidates=extra_existing_candidates,
        pearson_threshold=pearson_threshold,
        rank_threshold=rank_threshold,
        p90_pearson_threshold=p90_pearson_threshold,
        p90_rank_threshold=p90_rank_threshold,
    )
    for bucket in ("keepers", "dropped"):
        for candidate in result.get(bucket) or []:
            if not isinstance(candidate, dict):
                continue
            candidate["novelty_evidence"] = {
                **base,
                "candidate_fingerprint": _novelty_candidate_fingerprint(candidate),
            }
    result["evidence_context"] = {
        key: value
        for key, value in base.items()
        if key not in {"candidate_batch_fingerprint"}
    }
    return result


def _can_reuse_novelty_evidence(
    candidates: list[dict],
    *,
    start_date: str,
    end_date: str,
    extra_existing_candidates: list[dict] | None,
    requested: bool,
) -> tuple[bool, str]:
    if not requested:
        return False, "reuse_not_requested"
    if not candidates:
        return False, "no_gate_candidates"
    expected = _novelty_evidence_base(
        start_date=start_date,
        end_date=end_date,
        candidates=candidates,
        extra_existing_candidates=extra_existing_candidates,
    )
    for candidate in candidates:
        evidence = candidate.get("novelty_evidence")
        if not isinstance(evidence, dict):
            return False, "novelty_evidence_missing"
        if evidence.get("schema_version") != _NOVELTY_EVIDENCE_SCHEMA_VERSION:
            return False, "novelty_evidence_schema_mismatch"
        for key in (
            "selection_start_date",
            "selection_end_date",
            "active_pool_fingerprint",
            "extra_existing_fingerprint",
            "thresholds",
        ):
            if evidence.get(key) != expected.get(key):
                return False, f"novelty_evidence_{key}_mismatch"
        if evidence.get("candidate_fingerprint") != _novelty_candidate_fingerprint(candidate):
            return False, "novelty_evidence_candidate_mismatch"
        novelty = _extract_novelty_guard(candidate)
        if not novelty or novelty.get("allowed") is not True:
            return False, "novelty_evidence_not_allowed"
        combined = candidate.get("combined_guard") if isinstance(candidate.get("combined_guard"), dict) else {}
        if combined.get("allowed", True) is not True:
            return False, "combined_guard_not_allowed"
    return True, "same_candidate_window_active_pool_and_thresholds"


def _novelty_evidence_base(
    *,
    start_date: str,
    end_date: str,
    candidates: list[dict],
    extra_existing_candidates: list[dict] | None,
    pearson_threshold: float = 0.75,
    rank_threshold: float = 0.80,
    p90_pearson_threshold: float | None = None,
    p90_rank_threshold: float | None = None,
) -> dict:
    from domain.factor_research.active_values_store import current_active_registry_fingerprint

    holding_period = _novelty_holding_period(candidates)
    active_pool_fingerprint, _ = current_active_registry_fingerprint(
        holding_period_days=holding_period,
    )
    p90_pearson_threshold = (
        p90_pearson_threshold
        if p90_pearson_threshold is not None
        else max(0.0, float(pearson_threshold) - 0.05)
    )
    p90_rank_threshold = (
        p90_rank_threshold
        if p90_rank_threshold is not None
        else max(0.0, float(rank_threshold) - 0.05)
    )
    return {
        "schema_version": _NOVELTY_EVIDENCE_SCHEMA_VERSION,
        "selection_start_date": str(start_date or ""),
        "selection_end_date": str(end_date or ""),
        "holding_period_days": holding_period,
        "active_pool_fingerprint": active_pool_fingerprint,
        "extra_existing_fingerprint": _novelty_candidate_set_fingerprint(extra_existing_candidates or []),
        "candidate_batch_fingerprint": _novelty_candidate_set_fingerprint(candidates),
        "thresholds": {
            "pearson": float(pearson_threshold),
            "rank_corr": float(rank_threshold),
            "p90_pearson": float(p90_pearson_threshold),
            "p90_rank_corr": float(p90_rank_threshold),
        },
    }


def _novelty_result_candidates(result: dict) -> list[dict]:
    return [
        candidate
        for bucket in ("keepers", "dropped")
        for candidate in (result.get(bucket) or [])
        if isinstance(candidate, dict)
    ]


def _novelty_holding_period(candidates: list[dict]) -> int | None:
    for candidate in candidates or []:
        for value in (
            candidate.get("holding_period_days"),
            candidate.get("holding_period"),
            (candidate.get("params") or {}).get("holding_period") if isinstance(candidate.get("params"), dict) else None,
        ):
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
    return FACTOR_DEFAULT_HOLDING_PERIOD


def _novelty_candidate_fingerprint(candidate: dict) -> str:
    payload = {
        "candidate_id": str(candidate.get("candidate_id") or candidate.get("id") or ""),
        "expression": _re.sub(r"\s+", "", str(candidate.get("expression") or "")).lower(),
        "holding_period_days": _novelty_holding_period([candidate]),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def _novelty_candidate_set_fingerprint(candidates: list[dict]) -> str:
    rows = sorted(
        _novelty_candidate_fingerprint(candidate)
        for candidate in candidates or []
        if isinstance(candidate, dict)
    )
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:20]


def build_quality_feedback(*parts: str | None) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()


def low_information_dominates(report: dict) -> bool:
    counts = report.get("reason_counts", {}) or {}
    low_info = int(counts.get("low_information_gain", 0) or 0)
    adopted = len(report.get("adopted", []) or [])
    return low_info > 0 and low_info >= max(1, adopted)


def _blocked_candidate(candidate: dict, screening: dict) -> dict:
    blocked = dict(candidate)
    blocked["screening"] = screening
    blocked["screening_reason"] = screening.get("reason")
    return blocked


def screen_candidate(candidate: dict) -> dict:
    _normalize_candidate_evidence(candidate)
    persistence = _extract_persistence(candidate)
    novelty = _extract_novelty_guard(candidate)
    st_guard = _extract_st_exposure_guard(candidate)
    combined_guard = _extract_combined_guard(candidate)
    gate = candidate.get("gate_result", {}) or {}
    stock_lag1 = _safe_abs(persistence.get("stock_lag1_mean"))
    stock_lag5 = _safe_abs(persistence.get("stock_lag5_mean"))
    ic_lag1 = _safe_abs(persistence.get("ic_lag1_autocorr"))
    status = candidate.get("status")

    if gate and gate.get("passed") is False:
        reject_reasons = candidate.get("reject_reasons") or candidate.get("veto_reasons") or []
        if "requires_deep_validation" in reject_reasons or gate.get("reason") == "requires_deep_validation_before_import_gate":
            return {
                "allowed": False,
                "decision": "deep_validate",
                "reason": "requires_deep_validation",
                "summary": (
                    "Candidate came from quick score only. It must complete full backtest, "
                    "anti-overfit, adversarial validation, and novelty check before import."
                ),
                "next_round_hint": (
                    "Do not discard this solely because it failed the import gate. If quick-score evidence is promising, "
                    "run the official deep-validation tool chain and then retry the quality gate."
                ),
                "stock_lag1_mean": stock_lag1,
                "stock_lag5_mean": stock_lag5,
                "ic_lag1_autocorr": ic_lag1,
                "gate_result": gate,
            }
        if "novelty_correlation_veto" in reject_reasons:
            return {
                "allowed": False,
                "decision": "reject",
                "reason": "low_information_gain",
                "summary": (
                    "Candidate is too similar to the active factor pool and is vetoed before import. "
                    "Treat this as direction-level feedback for the next research round."
                ),
                "next_round_hint": "Switch mechanism or signal source; avoid small parameter tweaks on the matched active factor family.",
                "stock_lag1_mean": stock_lag1,
                "stock_lag5_mean": stock_lag5,
                "ic_lag1_autocorr": ic_lag1,
                "gate_result": gate,
                "novelty_guard": novelty,
            }
        if "st_exposure_veto" in reject_reasons or "missing_st_exposure_guard" in reject_reasons:
            return {
                "allowed": False,
                "decision": "reject",
                "reason": "st_exposure_veto",
                "summary": "Candidate passed earlier evidence but failed the ST exposure guard before import.",
                "next_round_hint": "Redesign the signal so the long-only top50 is not concentrated in ST or delisting-risk names.",
                "stock_lag1_mean": stock_lag1,
                "stock_lag5_mean": stock_lag5,
                "ic_lag1_autocorr": ic_lag1,
                "gate_result": gate,
                "novelty_guard": novelty,
                "st_exposure_guard": st_guard,
                "combined_guard": combined_guard,
            }
        if "holding_period_mismatch" in reject_reasons:
            return {
                "allowed": False,
                "decision": "reject",
                "reason": "holding_period_mismatch",
                "summary": "Candidate holding period does not match the target factor pool.",
                "next_round_hint": "Keep factor generation, novelty, and import within the same holding-period pool.",
                "stock_lag1_mean": stock_lag1,
                "stock_lag5_mean": stock_lag5,
                "ic_lag1_autocorr": ic_lag1,
                "gate_result": gate,
            }
        if "adversarial_failed" in reject_reasons:
            return {
                "allowed": False,
                "decision": "reject",
                "reason": "adversarial_failed",
                "summary": "Candidate failed adversarial validation and is not robust enough for import.",
                "next_round_hint": "Change signal geometry or source so the factor degrades more cleanly under destructive tests.",
                "stock_lag1_mean": stock_lag1,
                "stock_lag5_mean": stock_lag5,
                "ic_lag1_autocorr": ic_lag1,
                "gate_result": gate,
            }
        if "anti_overfit_failed" in reject_reasons:
            return {
                "allowed": False,
                "decision": "reject",
                "reason": "anti_overfit_failed",
                "summary": "Candidate failed anti-overfit checks and is vetoed before import.",
                "next_round_hint": "Use diagnose_factor and four-step analysis to redesign the mechanism before retesting.",
                "stock_lag1_mean": stock_lag1,
                "stock_lag5_mean": stock_lag5,
                "ic_lag1_autocorr": ic_lag1,
                "gate_result": gate,
            }
        reason = gate.get("reason") or ",".join(candidate.get("reject_reasons") or []) or "quality_gate_failed"
        return {
            "allowed": False,
            "decision": "reject",
            "reason": "quality_gate_failed",
            "summary": f"Candidate failed the final quality gate: {reason}.",
            "next_round_hint": "Use this candidate only as negative evidence; switch mechanism or fix the specific failing component before retesting.",
            "stock_lag1_mean": stock_lag1,
            "stock_lag5_mean": stock_lag5,
            "ic_lag1_autocorr": ic_lag1,
            "gate_result": gate,
        }

    if status == "duplicate":
        return {
            "allowed": False,
            "decision": "reject",
            "reason": "duplicate_candidate",
            "summary": "Candidate duplicates an existing factor path and should not enter the platform library.",
            "next_round_hint": "Switch operator family instead of tuning only signs or window sizes; avoid generating a numerical duplicate.",
            "stock_lag1_mean": stock_lag1,
            "stock_lag5_mean": stock_lag5,
            "ic_lag1_autocorr": ic_lag1,
        }

    if not novelty:
        return {
            "allowed": False,
            "decision": "reject",
            "reason": "missing_novelty_guard",
            "summary": "Novelty evidence is missing. Re-run fxalpha_novelty_check before import gate.",
            "next_round_hint": "Call fxalpha_novelty_check after deep validation and pass its original candidate objects into the import gate.",
            "stock_lag1_mean": stock_lag1,
            "stock_lag5_mean": stock_lag5,
            "ic_lag1_autocorr": ic_lag1,
            "novelty_guard": novelty,
        }

    if novelty and not novelty.get("allowed", True):
        matched = novelty.get("matched_existing_factor")
        pearson = novelty.get("max_existing_pearson")
        rank_corr = novelty.get("max_existing_rank_corr")
        return {
            "allowed": False,
            "decision": "reject",
            "reason": "low_information_gain",
            "summary": (
                "Candidate is too similar to the existing active factor pool and adds insufficient new information. "
                f"Closest active factor={matched or 'unknown'}, pearson={pearson}, rank_corr={rank_corr}."
            ),
            "next_round_hint": (
                "Switch to a materially different mechanism. Avoid minor parameter tweaks on the same volatility/volume/"
                "price chain, avoid the closest active factor's main inputs, and search for a signal source with "
                "lower Pearson and rank correlation to the active pool."
            ),
            "stock_lag1_mean": stock_lag1,
            "stock_lag5_mean": stock_lag5,
            "ic_lag1_autocorr": ic_lag1,
            "novelty_guard": novelty,
        }

    st_mode = _candidate_st_exposure_mode(candidate, st_guard)
    if not st_guard and st_mode == "hard":
        return {
            "allowed": False,
            "decision": "reject",
            "reason": "missing_st_exposure_guard",
            "summary": "ST exposure evidence is missing. Re-run fxalpha_novelty_check before import gate.",
            "next_round_hint": "Use the fxalpha_novelty_check novelty and distress_proxy_exposure output object when submitting the candidate to quality gate.",
            "stock_lag1_mean": stock_lag1,
            "stock_lag5_mean": stock_lag5,
            "ic_lag1_autocorr": ic_lag1,
            "novelty_guard": novelty,
            "st_exposure_guard": st_guard,
            "combined_guard": combined_guard,
        }

    if st_guard and st_guard.get("passed") is not True and st_mode == "hard":
        return {
            "allowed": False,
            "decision": "reject",
            "reason": "st_exposure_veto",
            "summary": (
                "Candidate's long-only top50 portfolio is too concentrated in ST or delisting-risk names. "
                f"avg_top50_ratio={st_guard.get('avg_top50_ratio')}, p95_top50_ratio={st_guard.get('p95_top50_ratio')}."
            ),
            "next_round_hint": "Redesign the mechanism or conditioning logic to remove ST/delisting concentration before deep validation.",
            "stock_lag1_mean": stock_lag1,
            "stock_lag5_mean": stock_lag5,
            "ic_lag1_autocorr": ic_lag1,
            "novelty_guard": novelty,
            "st_exposure_guard": st_guard,
            "combined_guard": combined_guard,
        }

    return {
        "allowed": True,
        "decision": "allow_import_check",
        "reason": "passed_quality_gate",
        "summary": "Candidate passed the final quality gate and may proceed to import.",
        "next_round_hint": "",
        "stock_lag1_mean": stock_lag1,
        "stock_lag5_mean": stock_lag5,
        "ic_lag1_autocorr": ic_lag1,
        "novelty_guard": novelty,
        "st_exposure_guard": st_guard,
        "combined_guard": combined_guard,
        "risk_tags": list(candidate.get("risk_tags") or []),
    }


def apply_gate(candidates: list[dict], min_abs_ic: float = 0.02, min_ir: float = 0.3) -> tuple[list[dict], list[dict]]:
    """Final import gate: official deep score + novelty + robustness + holding period."""
    adopted, rejected = [], []

    for candidate in candidates:
        _normalize_candidate_evidence(candidate)
        bs = _extract_backtest_summary(candidate)
        grade = _candidate_qgpt_grade(candidate)
        quick_score = _extract_quick_score(candidate)
        reject_reasons = list(candidate.get("reject_reasons") or [])
        rank_ic = abs(_safe_float(bs.get("rank_ic_mean", bs.get("ic_mean")), 0.0))
        rank_ir = abs(_safe_float(bs.get("rank_ic_ir", bs.get("ic_ir", bs.get("icir"))), 0.0))
        ic = rank_ic
        ir = rank_ir
        sharpe = bs.get("sharpe", 0) or 0
        deep_score, score_parts = _compute_deep_score(candidate, quick_score=quick_score)
        veto_reasons = _veto_reasons(candidate, bs)
        holding_period_days = _extract_holding_period_days(candidate)
        adversarial = _extract_adversarial(candidate)
        rolling = _extract_rolling_validation(candidate)
        novelty = _extract_novelty_guard(candidate)
        st_guard = _extract_st_exposure_guard(candidate)
        combined_guard = _extract_combined_guard(candidate)
        threshold_checks = _threshold_checks(bs, min_abs_ic=min_abs_ic, min_ir=min_ir)

        if not _has_deep_validation_evidence(candidate):
            veto_reasons.append("requires_deep_validation")
        if not threshold_checks["ic_abs"]["passed"]:
            veto_reasons.append("ic_below_threshold")
        if not threshold_checks["ir_abs"]["passed"]:
            veto_reasons.append("icir_below_threshold")
        candidate["deep_score"] = deep_score
        candidate["veto_reasons"] = sorted(set(veto_reasons))
        candidate["holding_period_days"] = holding_period_days
        candidate["quick_score"] = quick_score
        candidate["deep_validation"] = {
            "deep_score": deep_score,
            "score_parts": score_parts,
            "veto_reasons": candidate["veto_reasons"],
            "novelty_correlation": novelty,
            "st_exposure_guard": st_guard,
            "combined_guard": combined_guard,
            "anti_overfit": _extract_anti_overfit(candidate),
            "rolling_validation": rolling,
            "adversarial_validation": adversarial,
            "persistence_diagnostic": _extract_persistence(candidate),
            "threshold_checks": threshold_checks,
            "holding_period_days": holding_period_days,
            "next_round_feedback": _next_round_feedback(candidate),
        }

        passed = not candidate["veto_reasons"] and deep_score >= 80.0
        if passed:
            candidate["gate_result"] = {
                "passed": True,
                "deep_score": deep_score,
                "quick_score": quick_score,
                "ic": round(ic, 4),
                "ir": round(ir, 3),
                "rank_ic": round(rank_ic, 4),
                "rank_ir": round(rank_ir, 3),
                "sharpe": round(sharpe, 3),
                "qgpt_grade": grade,
                "score": deep_score,
                "reason": "quality_gate_adopted",
                "holding_period_days": holding_period_days,
                "reference_thresholds": {
                    "deep_score": 80.0,
                    "holding_period_days": FACTOR_DEFAULT_HOLDING_PERIOD,
                    "min_abs_ic": min_abs_ic,
                    "min_ir": min_ir,
                },
                "threshold_checks": threshold_checks,
            }
            adopted.append(candidate)
        else:
            reason = ",".join(candidate["veto_reasons"]) if candidate["veto_reasons"] else f"deep_score={deep_score:.1f}<80"
            candidate["gate_result"] = {
                "passed": False,
                "reason": reason,
                "deep_score": deep_score,
                "quick_score": quick_score,
                "ic": round(ic, 4),
                "ir": round(ir, 3),
                "rank_ic": round(rank_ic, 4),
                "rank_ir": round(rank_ir, 3),
                "qgpt_grade": grade,
                "score": deep_score,
                "sharpe": round(sharpe, 3),
                "holding_period_days": holding_period_days,
                "reference_thresholds": {
                    "deep_score": 80.0,
                    "holding_period_days": FACTOR_DEFAULT_HOLDING_PERIOD,
                    "min_abs_ic": min_abs_ic,
                    "min_ir": min_ir,
                },
                "threshold_checks": threshold_checks,
            }
            candidate.setdefault("reject_reasons", []).extend(candidate["veto_reasons"])
            rejected.append(candidate)
    return adopted, rejected



def _candidate_qgpt_grade(candidate: dict) -> str:
    """Return the quick-screen QuantGPT grade for audit metadata only.

    This does not change quality-gate pass/fail rules. Heartbeat/MCP callers often
    attach the original score_factor payload under score_result instead of copying
    grade to the top level; without this, gate_result.qgpt_grade can fall back to
    D even when quick screening actually rated the candidate A/B.
    """
    for value in (
        candidate.get("grade"),
        (candidate.get("score_result") or {}).get("grade") if isinstance(candidate.get("score_result"), dict) else None,
        (candidate.get("scoring") or {}).get("grade") if isinstance(candidate.get("scoring"), dict) else None,
    ):
        text = str(value or "").strip().upper()
        if text in {"A", "B", "C", "D"}:
            return text
    quick_score = _extract_quick_score(candidate)
    if quick_score is not None:
        return _grade_from_score(float(quick_score))
    return "D"


def _threshold_checks(backtest_summary: dict, *, min_abs_ic: float, min_ir: float) -> dict:
    ic = abs(_safe_float(backtest_summary.get("rank_ic_mean", backtest_summary.get("ic_mean")), 0.0))
    ir = abs(_safe_float(backtest_summary.get("rank_ic_ir", backtest_summary.get("ic_ir", backtest_summary.get("icir"))), 0.0))
    return {
        "ic_abs": {
            "value": round(ic, 6),
            "threshold": float(min_abs_ic),
            "passed": ic >= float(min_abs_ic),
        },
        "ir_abs": {
            "value": round(ir, 6),
            "threshold": float(min_ir),
            "passed": ir >= float(min_ir),
        },
    }

def _grade_from_score(score: float) -> str:
    if score >= 85.0:
        return "A"
    if score >= 70.0:
        return "B"
    if score >= 55.0:
        return "C"
    return "D"


def _apply_long_only_cap(score: float, grade: str, backtest_summary: dict) -> tuple[float, str, bool, str | None]:
    annual_return = _safe_float(backtest_summary.get("annual_return"), default=float("nan"))
    sharpe = _safe_float(
        backtest_summary.get("sharpe", backtest_summary.get("top_group_sharpe")),
        default=float("nan"),
    )
    if annual_return == annual_return and annual_return < 0:
        return min(score, 59.9), "C", True, "negative_annual_return"
    if sharpe == sharpe and sharpe < 0:
        return min(score, 59.9), "C", True, "negative_sharpe"
    return score, grade, False, None


def _extract_adversarial_score(candidate: dict) -> float | None:
    adversarial = _extract_adversarial(candidate)
    if not isinstance(adversarial, dict) or not adversarial:
        return None
    try:
        return max(0.0, min(100.0, float(adversarial.get("score"))))
    except Exception:
        return None


def _extract_novelty_score(candidate: dict) -> float | None:
    novelty = _extract_novelty_guard(candidate)
    if not isinstance(novelty, dict) or not novelty:
        return None
    try:
        return max(0.0, min(1.0, float(novelty.get("novelty_score"))))
    except Exception:
        return None


def _compute_deep_score(candidate: dict, *, quick_score: float | None) -> tuple[float, dict]:
    """Deep score v2; novelty remains an admission guard, not score points."""
    bs = _extract_backtest_summary(candidate)
    anti_score = _extract_anti_overfit_score(candidate)
    rolling_score = _extract_rolling_score(candidate)
    adversarial_score = _extract_adversarial_score(candidate)
    novelty_score = _extract_novelty_score(candidate)
    missing_components = _missing_deep_score_components(candidate)
    weighted_contributions = {
        "quick_core": round((quick_score or 0.0) * 0.55, 2),
        "anti_overfit": round((anti_score or 0.0) * 0.15, 2),
        "rolling": round((rolling_score or 0.0) * 0.20, 2),
        "adversarial": round((adversarial_score or 0.0) * 0.10, 2),
    }
    if missing_components:
        raw_score = 0.0
    else:
        raw_score = sum(weighted_contributions.values())
    grade = _grade_from_score(raw_score)
    capped_score, grade, capped, cap_reason = _apply_long_only_cap(raw_score, grade, bs)
    deep_score = round(capped_score, 1)
    return deep_score, {
        "official_score": deep_score,
        "official_grade": grade,
        "deep_score_policy_version": "deep_score_v2_55_15_20_10",
        "quick_score": round(quick_score, 1) if quick_score is not None else None,
        "anti_overfit_score": round(anti_score, 1) if anti_score is not None else None,
        "rolling_score": round(rolling_score, 1) if rolling_score is not None else None,
        "adversarial_score": round(adversarial_score, 1) if adversarial_score is not None else None,
        "novelty_score": round(novelty_score, 4) if novelty_score is not None else None,
        "component_scores": {
            "quick_core": round(quick_score, 1) if quick_score is not None else None,
            "anti_overfit": round(anti_score, 1) if anti_score is not None else None,
            "rolling": round(rolling_score, 1) if rolling_score is not None else None,
            "adversarial": round(adversarial_score, 1) if adversarial_score is not None else None,
        },
        "component_weights": {
            "quick_core": 0.55,
            "anti_overfit": 0.15,
            "rolling": 0.20,
            "adversarial": 0.10,
        },
        "weighted_contributions": weighted_contributions,
        "capped": capped,
        "cap_reason": cap_reason,
        "missing_components": missing_components,
    }

def _extract_anti_overfit_score(candidate: dict) -> float | None:
    anti = _extract_anti_overfit(candidate)
    if not isinstance(anti, dict) or not anti:
        return None
    try:
        return max(0.0, min(100.0, float(anti.get("score"))))
    except Exception:
        return None


def _veto_reasons(candidate: dict, backtest_summary: dict) -> list[str]:
    reasons: list[str] = []
    expression = str(candidate.get("expression") or "").strip()
    status = str(candidate.get("status") or "").lower()
    if not expression:
        reasons.append("empty_expression")
    if status in {"invalid_field", "score_error", "invalid_runtime", "error"}:
        reasons.append(status)
    if not backtest_summary:
        reasons.append("empty_backtest")
    if _extract_quick_score(candidate) is None:
        reasons.append("missing_quick_score")
    if candidate.get("lookahead_bias") is True:
        reasons.append("lookahead_bias")
    novelty = _extract_novelty_guard(candidate)
    if not novelty:
        reasons.append("missing_novelty_guard")
    elif _extract_novelty_score(candidate) is None:
        reasons.append("missing_novelty_score")
    pearson = _safe_float(novelty.get("max_existing_pearson"), 0.0)
    rank_corr = _safe_float(novelty.get("max_existing_rank_corr"), 0.0)
    p90_pearson = _safe_float(novelty.get("p90_pearson"), 0.0)
    p90_rank_corr = _safe_float(novelty.get("p90_rank_corr"), 0.0)
    thresholds = novelty.get("thresholds") or {}
    pearson_threshold = _safe_float(thresholds.get("pearson"), 0.75)
    rank_threshold = _safe_float(thresholds.get("rank_corr"), 0.80)
    p90_pearson_threshold = _safe_float(thresholds.get("p90_pearson"), max(0.0, pearson_threshold - 0.05))
    p90_rank_threshold = _safe_float(thresholds.get("p90_rank_corr"), max(0.0, rank_threshold - 0.05))
    if (
        novelty.get("allowed") is False
        or pearson >= pearson_threshold
        or rank_corr >= rank_threshold
        or p90_pearson >= p90_pearson_threshold
        or p90_rank_corr >= p90_rank_threshold
    ):
        reasons.append("novelty_correlation_veto")
    st_guard = _extract_st_exposure_guard(candidate)
    st_mode = _candidate_st_exposure_mode(candidate, st_guard)
    if not st_guard and st_mode == "hard":
        reasons.append("missing_st_exposure_guard")
    elif st_guard and st_guard.get("passed") is not True and st_mode == "hard":
        reasons.append("st_exposure_veto")
    combined_guard = _extract_combined_guard(candidate)
    if (
        st_mode == "hard"
        and combined_guard
        and combined_guard.get("allowed") is False
        and str(combined_guard.get("reason") or "").startswith("st_exposure")
    ):
        reasons.append("st_exposure_veto")
    anti = _extract_anti_overfit(candidate)
    if not anti:
        reasons.append("missing_anti_overfit")
    elif _extract_anti_overfit_score(candidate) is None:
        reasons.append("missing_anti_overfit_score")
    if anti and _anti_overfit_failed(candidate):
        reasons.append("anti_overfit_failed")
    rolling = _extract_rolling_validation(candidate)
    if not rolling:
        reasons.append("missing_rolling_validation")
    elif _extract_rolling_score(candidate) is None:
        reasons.append("missing_rolling_score")
    adversarial = _extract_adversarial(candidate)
    if not adversarial:
        reasons.append("missing_adversarial_validation")
    elif _extract_adversarial_score(candidate) is None:
        reasons.append("missing_adversarial_score")
    elif not _adversarial_passed(candidate):
        reasons.append("adversarial_failed")
    if _extract_holding_period_days(candidate) <= 0:
        reasons.append("holding_period_mismatch")
    if any(_is_bad_number(backtest_summary.get(k)) for k in ("ic_mean", "ic_ir", "rank_ic_mean", "rank_ic_ir")):
        reasons.append("data_abnormal")
    return reasons


def _anti_overfit_failed(candidate: dict) -> bool:
    anti = _extract_anti_overfit(candidate)
    if not isinstance(anti, dict) or not anti:
        return False
    recommendation = str(anti.get("recommendation") or "").lower()
    if "不推荐" in recommendation or "reject" in recommendation or "fail" in recommendation:
        return True
    score = _safe_float(anti.get("score"), 60.0)
    return score < 50.0


def _extract_anti_overfit(candidate: dict) -> dict:
    deep_validation = candidate.get("deep_validation") if isinstance(candidate.get("deep_validation"), dict) else {}
    for value in (
        candidate.get("anti_overfit"),
        candidate.get("anti_overfit_summary"),
        deep_validation.get("anti_overfit"),
        deep_validation.get("anti_overfit_summary"),
        (candidate.get("backtest") or {}).get("anti_overfit"),
        (candidate.get("backtest_result") or {}).get("anti_overfit"),
        (candidate.get("result") or {}).get("anti_overfit"),
    ):
        if isinstance(value, dict) and value and value.get("status") != "not_run_in_quick_score":
            return value
    return {}


def _extract_rolling_validation(candidate: dict) -> dict:
    """Read formal rolling-validation evidence from supported candidate shapes."""
    for value in (
        candidate.get("rolling_validation"),
        candidate.get("rolling_validation_summary"),
        (candidate.get("deep_validation") or {}).get("rolling_validation") if isinstance(candidate.get("deep_validation"), dict) else None,
        (candidate.get("backtest") or {}).get("rolling_validation") if isinstance(candidate.get("backtest"), dict) else None,
        (candidate.get("backtest_result") or {}).get("rolling_validation") if isinstance(candidate.get("backtest_result"), dict) else None,
        (candidate.get("result") or {}).get("rolling_validation") if isinstance(candidate.get("result"), dict) else None,
    ):
        if isinstance(value, dict) and value:
            return value
    return {}


def _extract_rolling_score(candidate: dict) -> float | None:
    rolling = _extract_rolling_validation(candidate)
    if not isinstance(rolling, dict) or not rolling:
        return None
    status = str(rolling.get("status") or rolling.get("summary", {}).get("status") or "").lower()
    if status in {
        "insufficient_data",
        "insufficient_history",
        "label_contract_error",
        "contract_error",
        "skipped_short_window",
        "skipped",
        "skip",
        "not_run",
        "not_run_in_quick_score",
        "insufficient",
        "missing_factor_cache",
        "error",
    }:
        return None
    summary = rolling.get("summary") or {}
    periods = rolling.get("incremental_periods") or rolling.get("windows") or []
    n_windows = summary.get("n_periods", summary.get("n_windows", len(periods) if isinstance(periods, list) else 0))
    try:
        n_windows = int(n_windows or 0)
    except Exception:
        n_windows = 0
    if n_windows <= 0:
        return None
    try:
        return max(0.0, min(100.0, float(rolling.get("score"))))
    except Exception:
        return None


def _extract_novelty_guard(candidate: dict) -> dict:
    for value in (
        candidate.get("novelty_guard"),
        candidate.get("novelty_correlation"),
        (candidate.get("screening") or {}).get("novelty_guard") if isinstance(candidate.get("screening"), dict) else None,
        (candidate.get("deep_validation") or {}).get("novelty_correlation") if isinstance(candidate.get("deep_validation"), dict) else None,
        (candidate.get("deep_validation") or {}).get("novelty_guard") if isinstance(candidate.get("deep_validation"), dict) else None,
        (candidate.get("metadata") or {}).get("novelty_guard") if isinstance(candidate.get("metadata"), dict) else None,
    ):
        if isinstance(value, dict) and value:
            return value
    return {}


def _extract_st_exposure_guard(candidate: dict) -> dict:
    for value in (
        candidate.get("st_exposure_guard"),
        (candidate.get("screening") or {}).get("st_exposure_guard") if isinstance(candidate.get("screening"), dict) else None,
        (candidate.get("deep_validation") or {}).get("st_exposure_guard") if isinstance(candidate.get("deep_validation"), dict) else None,
        (candidate.get("metadata") or {}).get("st_exposure_guard") if isinstance(candidate.get("metadata"), dict) else None,
    ):
        if isinstance(value, dict) and value:
            return value
    return {}


def _candidate_st_exposure_mode(candidate: dict, st_guard: dict | None = None) -> str:
    guard = st_guard if isinstance(st_guard, dict) else _extract_st_exposure_guard(candidate)
    mode = str((guard or {}).get("mode") or "").strip().lower()
    if mode in {"advisory", "diagnostic", "tag", "tag_only", "label"}:
        return "advisory"
    if mode in {"hard", "strict", "block", "blocking"}:
        return "hard"
    return get_live_st_exposure_guard_mode()


def _extract_combined_guard(candidate: dict) -> dict:
    for value in (
        candidate.get("combined_guard"),
        (candidate.get("screening") or {}).get("combined_guard") if isinstance(candidate.get("screening"), dict) else None,
        (candidate.get("deep_validation") or {}).get("combined_guard") if isinstance(candidate.get("deep_validation"), dict) else None,
        (candidate.get("metadata") or {}).get("combined_guard") if isinstance(candidate.get("metadata"), dict) else None,
    ):
        if isinstance(value, dict) and value:
            return value
    return {}


def _normalize_candidate_evidence(candidate: dict) -> None:
    novelty = _extract_novelty_guard(candidate)
    if novelty and (not isinstance(candidate.get("novelty_guard"), dict) or not candidate.get("novelty_guard")):
        candidate["novelty_guard"] = novelty
    st_guard = _extract_st_exposure_guard(candidate)
    if st_guard and (not isinstance(candidate.get("st_exposure_guard"), dict) or not candidate.get("st_exposure_guard")):
        candidate["st_exposure_guard"] = st_guard
    combined_guard = _extract_combined_guard(candidate)
    if combined_guard and (not isinstance(candidate.get("combined_guard"), dict) or not candidate.get("combined_guard")):
        candidate["combined_guard"] = combined_guard
    deep_validation = candidate.get("deep_validation")
    if isinstance(deep_validation, dict) and novelty and not deep_validation.get("novelty_correlation"):
        deep_validation["novelty_correlation"] = novelty
    if isinstance(deep_validation, dict) and st_guard and not deep_validation.get("st_exposure_guard"):
        deep_validation["st_exposure_guard"] = st_guard
    if isinstance(deep_validation, dict) and combined_guard and not deep_validation.get("combined_guard"):
        deep_validation["combined_guard"] = combined_guard
    rolling = _extract_rolling_validation(candidate)
    if rolling and (not isinstance(candidate.get("rolling_validation"), dict) or not candidate.get("rolling_validation")):
        candidate["rolling_validation"] = rolling
    if isinstance(deep_validation, dict) and rolling and not deep_validation.get("rolling_validation"):
        deep_validation["rolling_validation"] = rolling
    backtest_summary = _extract_backtest_summary(candidate)
    if backtest_summary and (not isinstance(candidate.get("backtest_summary"), dict) or not candidate.get("backtest_summary")):
        candidate["backtest_summary"] = backtest_summary


def _extract_overfit_monitor(candidate: dict) -> dict:
    anti = _extract_anti_overfit(candidate)
    persistence = _extract_persistence(candidate)
    adversarial = _extract_adversarial(candidate)
    temporal_shuffle = {}
    if isinstance(adversarial, dict):
        for key in ("temporal_shuffle", "Temporal Shuffle", "temporal_shuffle_test"):
            value = adversarial.get(key)
            if isinstance(value, dict):
                temporal_shuffle = value
                break
        tests = adversarial.get("tests")
        if not temporal_shuffle and isinstance(tests, dict):
            for key, value in tests.items():
                if "temporal" in str(key).lower() and "shuffle" in str(key).lower() and isinstance(value, dict):
                    temporal_shuffle = value
                    break
    return {
        "anti_overfit_score": _extract_anti_overfit_score(candidate),
        "stock_lag1_mean": _safe_abs(persistence.get("stock_lag1_mean")),
        "stock_lag5_mean": _safe_abs(persistence.get("stock_lag5_mean")),
        "ic_lag1_autocorr": _safe_abs(persistence.get("ic_lag1_autocorr")),
        "temporal_shuffle": temporal_shuffle,
        "adversarial_score": adversarial.get("score") if isinstance(adversarial, dict) else None,
        "adversarial_passed_count": adversarial.get("passed_count") if isinstance(adversarial, dict) else None,
    }


def _extract_temporal_shuffle_ratio(candidate: dict) -> float | None:
    adversarial = _extract_adversarial(candidate)
    if not isinstance(adversarial, dict) or not adversarial:
        return None
    direct_keys = ("temporal_shuffle", "Temporal Shuffle", "temporal_shuffle_test")
    for key in direct_keys:
        details = adversarial.get(key)
        if isinstance(details, dict):
            ratio = _safe_float(details.get("ratio"), default=float("nan"))
            if ratio == ratio:
                return ratio
    tests = adversarial.get("tests")
    if isinstance(tests, list):
        for item in tests:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").lower()
            if "temporal" not in name or "shuffle" not in name:
                continue
            details = item.get("details")
            if not isinstance(details, dict):
                continue
            ratio = _safe_float(details.get("ratio"), default=float("nan"))
            if ratio == ratio:
                return ratio
    if isinstance(tests, dict):
        for key, details in tests.items():
            name = str(key or "").lower()
            if "temporal" not in name or "shuffle" not in name or not isinstance(details, dict):
                continue
            ratio = _safe_float(details.get("ratio"), default=float("nan"))
            if ratio == ratio:
                return ratio
    return None


def _expression_smoothness_profile(expression: str) -> dict[str, int]:
    text = str(expression or "").lower()
    return {
        "smooth_ops": sum(
            text.count(token)
            for token in ("ts_mean(", "ts_rank(", "ts_zscore(", "decay_linear(", "ema(", "sma(", "wma(")
        ),
        "branch_ops": text.count("where("),
        "nonlinear_ops": sum(text.count(token) for token in ("tanh(", "sigmoid(", "sign_power(")),
        "mul_ops": text.count("*"),
    }


def _temporal_shuffle_smoothness_feedback(candidate: dict) -> str:
    ratio = _extract_temporal_shuffle_ratio(candidate)
    if ratio is None or _adversarial_passed(candidate):
        return ""
    persistence = _extract_persistence(candidate)
    profile = _expression_smoothness_profile(str(candidate.get("expression") or ""))
    stock_lag1 = _safe_abs(persistence.get("stock_lag1_mean"))
    ic_lag1 = _safe_abs(persistence.get("ic_lag1_autocorr"))
    if (
        stock_lag1 < 0.35
        and ic_lag1 < 0.60
        and profile["smooth_ops"] < 3
        and profile["branch_ops"] < 2
        and profile["nonlinear_ops"] < 2
    ):
        return ""
    return (
        "Temporal shuffle remains the weak point in destructive validation; if this keeps repeating across rounds, "
        "consider simplifying one mechanism leg, removing one smoothing layer, and preferring faster event confirmations "
        "over stacked slow gates."
    )


def _attach_screening_evidence(candidate: dict, screening: dict) -> dict:
    screening = dict(screening or {})
    novelty = _extract_novelty_guard(candidate)
    if novelty:
        candidate["novelty_guard"] = novelty
        screening.setdefault("novelty_guard", novelty)
    monitor = _extract_overfit_monitor(candidate)
    for key, value in monitor.items():
        if value not in (None, {}, ""):
            screening.setdefault(key, value)
    deep_validation = candidate.get("deep_validation")
    if isinstance(deep_validation, dict):
        if novelty:
            deep_validation.setdefault("novelty_correlation", novelty)
        deep_validation.setdefault("overfit_monitor", {k: v for k, v in monitor.items() if v not in (None, {}, "")})
    return screening


def _extract_adversarial(candidate: dict) -> dict:
    for value in (
        candidate.get("adversarial_validation"),
        candidate.get("adversarial"),
        (candidate.get("backtest") or {}).get("adversarial_validation"),
        (candidate.get("backtest_result") or {}).get("adversarial_validation"),
        (candidate.get("result") or {}).get("adversarial_validation"),
    ):
        if isinstance(value, dict) and value:
            return value
    return {}


def _extract_persistence(candidate: dict) -> dict:
    for value in (
        candidate.get("persistence_diagnostic"),
        candidate.get("autocorrelation"),
        (candidate.get("anti_overfit") or {}).get("autocorrelation"),
        (candidate.get("anti_overfit_result") or {}).get("autocorrelation"),
        (candidate.get("backtest") or {}).get("anti_overfit", {}).get("autocorrelation"),
        (candidate.get("backtest_result") or {}).get("anti_overfit", {}).get("autocorrelation"),
    ):
        if isinstance(value, dict) and value:
            return value
    return {}


def _extract_holding_period_days(candidate: dict) -> int:
    sources = [
        candidate.get("holding_period_days"),
        candidate.get("holding_period"),
        (candidate.get("params") or {}).get("holding_period") if isinstance(candidate.get("params"), dict) else None,
        (candidate.get("metadata") or {}).get("holding_period_days") if isinstance(candidate.get("metadata"), dict) else None,
    ]
    for value in sources:
        try:
            if value is not None:
                return int(value)
        except Exception:
            continue
    return int(FACTOR_DEFAULT_HOLDING_PERIOD)


def _adversarial_passed(candidate: dict) -> bool:
    adv = _extract_adversarial(candidate)
    if not adv:
        return False
    score = _safe_float(adv.get("score"), 0.0)
    return score >= 60.0


def _missing_deep_score_components(candidate: dict) -> list[str]:
    """Return only the four numeric Deep Score v2 components."""
    missing: list[str] = []
    if _extract_quick_score(candidate) is None:
        missing.append("quick_score")
    if _extract_anti_overfit_score(candidate) is None:
        missing.append("anti_overfit_score")
    if not _extract_rolling_validation(candidate):
        missing.append("rolling_validation")
    elif _extract_rolling_score(candidate) is None:
        missing.append("rolling_score")
    if not _extract_adversarial(candidate):
        missing.append("adversarial_validation")
    elif _extract_adversarial_score(candidate) is None:
        missing.append("adversarial_score")
    return missing


def _missing_deep_components(candidate: dict) -> list[str]:
    """Return all required admission evidence, including non-score novelty."""
    missing = _missing_deep_score_components(candidate)
    if not _extract_novelty_guard(candidate):
        missing.append("novelty_guard")
    elif _extract_novelty_score(candidate) is None:
        missing.append("novelty_score")
    return missing


def missing_deep_components(candidate: dict) -> list[str]:
    """Public shared completeness contract for ORCH adapters and the gate."""
    return _missing_deep_components(candidate)


def _next_round_feedback(candidate: dict) -> str:
    gate = candidate.get("gate_result") or {}
    novelty = _extract_novelty_guard(candidate)
    parts = []
    if novelty.get("allowed") is False:
        parts.append(
            "Novelty veto: switch mechanism or signal source; avoid minor variants of the matched active factor."
        )
    if not _adversarial_passed(candidate):
        parts.append("Adversarial validation is weak: change signal geometry so destructive tests break the candidate more clearly.")
    temporal_feedback = _temporal_shuffle_smoothness_feedback(candidate)
    if temporal_feedback:
        parts.append(temporal_feedback)
    if gate.get("deep_score", candidate.get("deep_score", 0)) < 80:
        parts.append("Deep score is below the import threshold; improve weak components rather than forcing import.")
    return " ".join(parts)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _extract_quick_score(candidate: dict) -> float | None:
    screening = candidate.get("screening") if isinstance(candidate.get("screening"), dict) else {}
    metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), dict) else {}
    gate_result = candidate.get("gate_result") if isinstance(candidate.get("gate_result"), dict) else {}
    for value in (
        candidate.get("quick_score"),
        candidate.get("score"),
        screening.get("score"),
        screening.get("quick_score"),
        metrics.get("quick_score"),
        gate_result.get("quick_score"),
    ):
        if isinstance(value, dict):
            for key in ("score", "quick_score", "value", "numeric_value"):
                nested = _safe_float(value.get(key), default=float("nan"))
                if nested == nested:
                    return nested
            continue
        scalar = _safe_float(value, default=float("nan"))
        if scalar == scalar:
            return scalar
    return None


def _is_bad_number(value: Any) -> bool:
    try:
        v = float(value)
        return v != v or v in {float("inf"), float("-inf")}
    except Exception:
        return False


def _dedup_round(adopted: list[dict]) -> tuple[list[dict], list[dict], str]:
    if len(adopted) <= 1:
        return adopted, [], ""
    groups: dict[str, list[int]] = {}
    for idx, candidate in enumerate(adopted):
        groups.setdefault(_strip_params(candidate.get("expression", "")), []).append(idx)

    keepers: list[dict] = []
    dropped_idxs: set[int] = set()
    for idxs in groups.values():
        if len(idxs) == 1:
            keepers.append(adopted[idxs[0]])
            continue
        best_idx = max(idxs, key=lambda i: abs(adopted[i].get("gate_result", {}).get("ic", 0) or 0))
        keepers.append(adopted[best_idx])
        dropped_idxs.update(i for i in idxs if i != best_idx)

    feedback = ""
    if dropped_idxs:
        feedback = (
            f"Dropped {len(dropped_idxs)} structurally identical factors. "
            "Explore materially different operator chains instead of window-only variants."
        )
    return keepers, [{"idx": idx} for idx in dropped_idxs], feedback


def _screening_feedback(screened_out: list[dict]) -> str:
    reasons = [(item.get("screening", {}) or {}).get("reason") or "quality_guard" for item in screened_out[:3]]
    unique_reasons = ", ".join(sorted(set(reasons))) if reasons else "quality_guard"
    if "low_information_gain" in reasons:
        return (
            "Last round produced candidates that were too similar to the active factor pool. "
            "Change mechanism entirely and avoid small operator or window tweaks on the same signal chain."
        )
    return f"Last round produced gate-passed candidates blocked by {unique_reasons}. Change operator family and reduce signal persistence."


def _strip_params(expr: str) -> str:
    text = _re.sub(r"\b\d+\b", "", expr or "")
    text = _re.sub(r"\s+", "", text)
    text = _re.sub(r",,+", ",", text)
    text = _re.sub(r"\(,+", "(", text)
    return _re.sub(r",+\)", ")", text)


def _has_deep_validation_evidence(candidate: dict) -> bool:
    def _complete() -> bool:
        return bool(
            _extract_anti_overfit(candidate)
            and _extract_adversarial(candidate)
            and _extract_rolling_score(candidate) is not None
        )

    if str(candidate.get("screening_stage") or "") == "deep_validation":
        return _complete()
    if str(candidate.get("source_tool") or "") == "run_backtest":
        return _complete()
    if _extract_backtest_summary(candidate):
        return _complete()
    if candidate.get("anti_overfit") or candidate.get("adversarial_validation"):
        return _complete()
    nested_anti = ((candidate.get("backtest") or {}).get("anti_overfit") or {})
    nested_adv = ((candidate.get("backtest") or {}).get("adversarial_validation") or {})
    nested_rolling = ((candidate.get("backtest") or {}).get("rolling_validation") or {})
    if nested_anti and nested_adv and nested_rolling and _extract_rolling_score(candidate) is not None:
        return True
    anti_summary = candidate.get("anti_overfit_summary") or {}
    adv_summary = candidate.get("adversarial_validation") or {}
    return bool(
        isinstance(anti_summary, dict) and anti_summary and anti_summary.get("status") != "not_run_in_quick_score"
        and isinstance(adv_summary, dict) and adv_summary
        and _extract_rolling_score(candidate) is not None
    )


def _extract_backtest_summary(candidate: dict) -> dict:
    """Accept both direct gate candidates and raw QuantGPT tool payloads."""
    for value in (
        (candidate.get("backtest") or {}).get("backtest_summary"),
        (candidate.get("backtest") or {}).get("summary"),
        (candidate.get("backtest_result") or {}).get("backtest_summary"),
        (candidate.get("result") or {}).get("backtest_summary"),
        candidate.get("backtest_summary"),
        (candidate.get("metrics") or {}).get("backtest_summary") if isinstance(candidate.get("metrics"), dict) else None,
    ):
        if isinstance(value, dict) and value:
            return value
    nested_metrics = (candidate.get("backtest") or {}).get("metrics")
    if isinstance(nested_metrics, dict) and nested_metrics:
        derived = {
            "ic_mean": nested_metrics.get("ic_mean"),
            "ic_ir": nested_metrics.get("ic_ir"),
            "rank_ic_mean": nested_metrics.get("rank_ic_mean"),
            "rank_ic_ir": nested_metrics.get("rank_ic_ir"),
            "sharpe": nested_metrics.get("sharpe", nested_metrics.get("top_group_sharpe")),
            "top_group_sharpe": nested_metrics.get("top_group_sharpe", nested_metrics.get("sharpe")),
            "annual_return": nested_metrics.get("annual_return", nested_metrics.get("cagr")),
            "turnover": nested_metrics.get("turnover"),
            "max_drawdown": nested_metrics.get("max_drawdown"),
            "monotonicity_score": nested_metrics.get("monotonicity_score"),
            "data_days": nested_metrics.get("data_days"),
        }
        if any(value is not None for value in derived.values()):
            return {key: value for key, value in derived.items() if value is not None}
    return {}


def _safe_abs(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return round(abs(float(value)), 4)
    except Exception:
        return None


def _reason_counts(screened_out: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in screened_out:
        reason = ((item.get("screening") or {}).get("reason")) or "quality_guard"
        counts[str(reason)] = counts.get(str(reason), 0) + 1
    return counts


def _emit_quality_event(
    progress_callback: Callable[[dict], None] | None,
    stage: str,
    round_no: int | None,
    family: str | None,
    report: dict,
) -> None:
    if not progress_callback:
        return
    payload = {
        "event": "quality_gate_completed",
        "stage": stage,
        "round": round_no,
        "family": family,
        "counts": report.get("counts", {}),
        "reason_counts": report.get("reason_counts", {}),
        "feedback": report.get("feedback", ""),
    }
    try:
        progress_callback(payload)
    except Exception:
        logger.debug("[quality_gate] progress callback failed", exc_info=True)


def _empty_quality_report() -> dict:
    return {
        "adopted": [],
        "rejected": [],
        "screened_out": [],
        "dedup_dropped": [],
        "dedup_feedback": "",
        "novelty": {"keepers": [], "dropped": [], "details": [], "feedback": ""},
        "novelty_feedback": "",
        "feedback": "",
        "reason_counts": {},
        "counts": {
            "input": 0,
            "gate_adopted": 0,
            "gate_rejected": 0,
            "dedup_keepers": 0,
            "dedup_dropped": 0,
            "novelty_keepers": 0,
            "novelty_dropped": 0,
            "adopted": 0,
            "screened_out": 0,
        },
    }
