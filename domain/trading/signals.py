from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from domain.trading.prediction import latest_pred_snapshot, resolve_prediction_model_context
from domain.trading.confidence import (
    evaluate_confidence,
    is_confidence_cash_contract,
    score_boundary_evidence,
)
from domain.data_foundation.stock_metadata import instrument_to_market_code, load_stock_identity_rows
from storage.paths import MODEL_DEFAULT_TOPK, SCORES_RUNTIME_ROOT, TARGETS_RUNTIME_ROOT


def _apply_st_filter(
    score_df: pd.DataFrame,
    *,
    identity_rows: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    annotated = score_df.copy()
    annotated['market_code'] = annotated['instrument'].astype(str).map(instrument_to_market_code)
    identity = load_stock_identity_rows() if identity_rows is None else identity_rows
    identity_match_count = 0
    if not identity.empty:
        identity = identity.drop_duplicates("market_code", keep="last").set_index("market_code")
        identity_match_count = int(annotated["market_code"].isin(identity.index).sum())
        for column in ["security_name", "list_status", "st_status"]:
            if column in identity.columns:
                annotated[column] = annotated['market_code'].map(identity[column])
    if 'security_name' not in annotated.columns:
        annotated['security_name'] = ""
    security_names = annotated['security_name'].fillna('').astype(str).str.strip()
    list_status = annotated.get("list_status", pd.Series("", index=annotated.index)).fillna("").astype(str).str.upper()
    st_status = annotated.get("st_status", pd.Series("", index=annotated.index)).fillna("").astype(str).str.upper()
    st_mask = (
        list_status.eq("D")
        | st_status.isin({"ST", "DELIST"})
        | security_names.str.contains(r'^(?:\*?ST|SST)|退市', case=False, regex=True)
    )
    filtered = annotated.loc[~st_mask].copy().reset_index(drop=True)
    summary = {
        'st_filtered_count': int(st_mask.sum()),
        'st_filtered_instruments': annotated.loc[st_mask, 'instrument'].astype(str).tolist(),
        'st_filtered_names': annotated.loc[st_mask, 'security_name'].fillna('').astype(str).tolist(),
        'eligible_count_after_st_filter': int(len(filtered)),
        'identity_match_count': identity_match_count,
        'identity_match_ratio': float(identity_match_count / len(annotated)) if len(annotated) else 1.0,
        'identity_policy': 'point_in_time_trade_date' if identity_rows is not None else 'latest_identity_cache',
    }
    return filtered, summary


def _score_quality(score_df: pd.DataFrame) -> dict[str, Any]:
    if score_df.empty or "score" not in score_df.columns:
        return {"status": "empty", "record_count": int(len(score_df)), "unique_score_count": 0}
    scores = pd.to_numeric(score_df["score"], errors="coerce")
    return {
        "status": "ok",
        "record_count": int(len(score_df)),
        "non_null_score_count": int(scores.notna().sum()),
        "unique_score_count": int(scores.nunique(dropna=True)),
        "score_std": float(scores.std(skipna=True) or 0.0),
        "score_min": float(scores.min(skipna=True)) if scores.notna().any() else None,
        "score_max": float(scores.max(skipna=True)) if scores.notna().any() else None,
    }


def _assert_score_diversity(score_df: pd.DataFrame, *, topk: int) -> dict[str, Any]:
    quality = _score_quality(score_df)
    record_count = int(quality.get("record_count") or 0)
    unique_count = int(quality.get("unique_score_count") or 0)
    score_std = float(quality.get("score_std") or 0.0)
    min_unique = min(max(int(topk) * 3, 20), max(record_count // 20, 1))
    if record_count >= max(int(topk), 20) and (unique_count < min_unique or score_std <= 1e-12):
        raise RuntimeError(
            "prediction_score_degenerate: "
            f"record_count={record_count}, unique_score_count={unique_count}, "
            f"score_std={score_std:.3g}, required_unique>={min_unique}. "
            "Refuse to build recommendation because top-k would fall back to instrument ordering."
        )
    return quality


def _score_dir(model_run_id: str) -> Path:
    return SCORES_RUNTIME_ROOT / model_run_id


def _target_dir(model_run_id: str, namespace: str = "") -> Path:
    return TARGETS_RUNTIME_ROOT / model_run_id / namespace if namespace else TARGETS_RUNTIME_ROOT / model_run_id


def export_daily_score(
    *,
    model_id: str | None = None,
    model_run_id: str | None = None,
    as_of_date: str | None = None,
    topk: int | None = None,
) -> dict[str, Any]:
    model_context = resolve_prediction_model_context(model_id=model_id, model_run_id=model_run_id)
    latest_dt, latest = latest_pred_snapshot(model_context, as_of_date=as_of_date)
    if topk is not None:
        latest = latest.head(topk).copy()
        latest['rank'] = range(1, len(latest) + 1)

    latest.insert(0, 'trade_date', str(latest_dt.date()))
    latest.insert(1, 'model_id', model_context['model_id'])
    latest.insert(2, 'model_run_id', model_context['model_run_id'])

    out_dir = _score_dir(model_context['model_run_id'])
    out_dir.mkdir(parents=True, exist_ok=True)
    score_file = out_dir / f'score_{str(latest_dt.date())}.csv'
    latest.to_csv(score_file, index=False)

    meta = {
        'model_id': model_context['model_id'],
        'model_run_id': model_context['model_run_id'],
        'feature_set_id': model_context.get('feature_set_id', ''),
        'generated_at': datetime.now().isoformat(),
        'trade_date': str(latest_dt.date()),
        'score_file': str(score_file),
        'record_count': int(len(latest)),
        'topk_applied': int(topk) if topk is not None else None,
        'score_quality': _score_quality(latest),
        'source_pred_recorder': model_context['recorder_run_dir'],
        'run_context_source': model_context.get('source', ''),
    }
    meta_file = out_dir / 'latest_meta.json'
    meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
    return meta


def build_target_portfolio(
    *,
    model_id: str | None = None,
    model_run_id: str | None = None,
    topk: int = MODEL_DEFAULT_TOPK,
    weighting: str = 'equal',
    total_capital: float | None = None,
    score_meta: dict[str, Any] | None = None,
    score_df: pd.DataFrame | None = None,
    identity_rows: pd.DataFrame | None = None,
    strategy_contract_version: str = "top20_drop2_hold5_open_v1",
    confidence_policy: dict[str, Any] | None = None,
    model_confidence_evidence: dict[str, Any] | None = None,
    evidence_as_of: str = "",
    label_cutoff_date: str = "",
    output_namespace: str = "",
) -> dict[str, Any]:
    if score_meta is None:
        score_meta = export_daily_score(model_id=model_id, model_run_id=model_run_id)
    if score_df is None:
        score_df = pd.read_csv(score_meta['score_file'])

    if score_df.empty:
        raise ValueError('score file is empty')
    if topk <= 0:
        raise ValueError('topk must be positive')

    eligible_df, st_filter_summary = _apply_st_filter(score_df, identity_rows=identity_rows)
    if identity_rows is not None and float(st_filter_summary.get("identity_match_ratio") or 0.0) < 0.95:
        raise RuntimeError(
            "point_in_time_identity_coverage_below_95pct: "
            f"matched={st_filter_summary.get('identity_match_count', 0)}, "
            f"records={len(score_df)}, ratio={st_filter_summary.get('identity_match_ratio', 0.0):.4f}"
        )
    if eligible_df.empty:
        raise RuntimeError('all candidate instruments were filtered out by ST exclusion policy')
    eligible_df = eligible_df.copy()
    eligible_df["score"] = pd.to_numeric(eligible_df["score"], errors="coerce")
    eligible_df = eligible_df.dropna(subset=["score"])
    eligible_df = eligible_df.sort_values(["score", "instrument"], ascending=[False, True]).reset_index(drop=True)
    score_quality = _score_quality(eligible_df)
    if int(score_quality.get("unique_score_count") or 0) <= 1 or float(score_quality.get("score_std") or 0.0) <= 1e-12:
        _assert_score_diversity(eligible_df, topk=topk)

    confidence = None
    if is_confidence_cash_contract(strategy_contract_version):
        boundary = score_boundary_evidence(eligible_df, topk=topk)
        confidence = evaluate_confidence(
            score_quality=score_quality,
            boundary=boundary,
            topk=topk,
            model_evidence=model_confidence_evidence,
            policy=confidence_policy,
            evidence_as_of=evidence_as_of or str(score_meta.get("trade_date") or ""),
            label_cutoff_date=label_cutoff_date,
        )
        if boundary["boundary_tied"]:
            selected = eligible_df.loc[eligible_df["score"] > float(boundary["topk_boundary_score"])].copy()
        else:
            selected = eligible_df.head(topk).copy()
        selected = selected.head(int(confidence["selected_count"])).reset_index(drop=True)
    else:
        score_quality = _assert_score_diversity(eligible_df, topk=topk)
        selected = eligible_df.head(topk).copy().reset_index(drop=True)
    selected['rank'] = range(1, len(selected) + 1)

    if weighting != 'equal':
        raise NotImplementedError(f'unsupported weighting mode: {weighting}')

    target_weight = (
        float(confidence["slot_weight"])
        if confidence is not None
        else 1.0 / len(selected)
    )
    selected['target_weight'] = float(target_weight)
    if total_capital is not None:
        selected['target_value'] = selected['target_weight'] * float(total_capital)

    out_dir = _target_dir(score_meta['model_run_id'], output_namespace)
    out_dir.mkdir(parents=True, exist_ok=True)
    trade_date = str(pd.Timestamp(score_meta['trade_date']).date())
    target_file = out_dir / f'target_portfolio_{trade_date}.csv'
    selected.to_csv(target_file, index=False)

    meta = {
        'model_id': score_meta['model_id'],
        'model_run_id': score_meta['model_run_id'],
        'generated_at': datetime.now().isoformat(),
        'trade_date': trade_date,
        'topk': int(topk),
        'weighting': weighting,
        'total_capital': float(total_capital) if total_capital is not None else None,
        'target_file': str(target_file),
        'record_count': int(len(selected)),
        'source_score_file': score_meta['score_file'],
        'st_filter': st_filter_summary,
        'score_quality': score_quality,
        'confidence': confidence or {},
        'strategy_contract_version': strategy_contract_version,
        'target_stock_exposure': float(selected['target_weight'].sum()) if not selected.empty else 0.0,
        'target_cash_weight': 1.0 - (float(selected['target_weight'].sum()) if not selected.empty else 0.0),
    }
    meta_file = out_dir / 'latest_meta.json'
    meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
    return meta
