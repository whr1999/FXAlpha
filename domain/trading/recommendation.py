from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from qlib.data import D

from domain.trading.confidence import (
    is_confidence_cash_contract,
    load_model_confidence_evidence,
    normalize_confidence_policy,
)
from domain.trading.prediction import get_qlib_latest_calendar_date, init_qlib, resolve_prediction_model_context
from domain.trading.signals import build_target_portfolio, export_daily_score
from domain.trading.risk_policy import (
    apply_risk_policy,
    load_risk_policy_config,
    risk_policy_config_hash,
    write_latest_risk_decision,
)
from domain.data_foundation.stock_metadata import (
    load_stock_identity_map,
    load_stock_identity_rows_for_window,
    security_name_for_instrument,
)
from storage.paths import MODEL_DEFAULT_TOPK, PAPER_TRADING_RUNTIME_ROOT, RECOMMENDATIONS_RUNTIME_ROOT, TRADING_LATEST_STATUS_FILE
from storage.trading_registry import TradingRegistry


LOT_SIZE = 100
PAPER_ACCOUNT_ROOT = PAPER_TRADING_RUNTIME_ROOT


def _date_str(value: Any) -> str:
    return str(pd.Timestamp(value).date())


def qlib_latest_trade_date() -> str:
    init_qlib()
    return _date_str(get_qlib_latest_calendar_date())


def next_trading_date(signal_date: str) -> str | None:
    init_qlib()
    calendar = pd.Index(pd.to_datetime(D.calendar(freq="day")).normalize())
    current = pd.Timestamp(signal_date).normalize()
    future = calendar[calendar > current]
    if len(future) == 0:
        return None
    return _date_str(future[0])


def has_qlib_trade_date(trade_date: str | None) -> bool:
    if not trade_date:
        return False
    init_qlib()
    target = pd.Timestamp(trade_date).normalize()
    calendar = pd.Index(pd.to_datetime(D.calendar(freq="day")).normalize())
    return bool((calendar == target).any())


def resolve_pending_execution_date(signal_date: str, execution_date: str | None = None) -> str | None:
    if execution_date:
        return _date_str(execution_date)
    return next_trading_date(signal_date)


def _recommendation_dir(model_run_id: str, account_id: str | None = None) -> Path:
    return RECOMMENDATIONS_RUNTIME_ROOT / (account_id or model_run_id)


def _state_file(account_id: str) -> Path:
    return PAPER_ACCOUNT_ROOT / account_id / "state" / "account_state.json"


def load_current_position_state(account_id: str, initial_capital: float) -> dict[str, Any]:
    state_file = _state_file(account_id)
    if state_file.exists():
        return json.loads(state_file.read_text(encoding="utf-8"))
    return {
        "account_id": account_id,
        "version_id": account_id,
        "as_of_date": None,
        "cash": float(initial_capital),
        "initial_capital": float(initial_capital),
        "account_value": float(initial_capital),
        "stock_value": 0.0,
        "positions": {},
        "source": "fresh_recommendation_start",
    }


def _estimated_price_map(trade_date: str, instruments: list[str]) -> dict[str, float]:
    if not instruments:
        return {}
    init_qlib()
    day = _date_str(trade_date)
    frame = D.features(
        sorted(set(instruments)),
        ["$close"],
        start_time=day,
        end_time=day,
        freq="day",
        disk_cache=False,
    )
    if frame.empty:
        return {}
    df = frame.reset_index()
    price_col = "$close" if "$close" in df.columns else df.columns[-1]
    df["instrument"] = df["instrument"].astype(str)
    return {
        str(row["instrument"]): float(row[price_col])
        for _, row in df.iterrows()
        if pd.notna(row[price_col]) and float(row[price_col]) > 0
    }


def _target_shares(target_weight: float, capital: float, price: float) -> int:
    if price <= 0:
        return 0
    raw = int(float(target_weight) * float(capital) / float(price))
    return max((raw // LOT_SIZE) * LOT_SIZE, 0)


def _action(delta_shares: int, current_shares: int, target_shares: int) -> str:
    if delta_shares > 0:
        return "buy"
    if delta_shares < 0:
        return "sell"
    if current_shares > 0 or target_shares > 0:
        return "hold"
    return "ignore"


def _market_counts(instruments: pd.Series) -> dict[str, int]:
    suffixes = instruments.astype(str).str[-2:].str.lower()
    return {str(k): int(v) for k, v in suffixes.value_counts().sort_index().items()}


def build_order_preview(
    *,
    signal_date: str,
    execution_date: str | None,
    target_df: pd.DataFrame,
    score_df: pd.DataFrame,
    current_state: dict[str, Any],
    total_capital: float,
) -> pd.DataFrame:
    positions = current_state.get("positions", {}) or {}
    current_instruments = set(str(item) for item in positions.keys())
    target = target_df.copy()
    target["instrument"] = target["instrument"].astype(str)
    target_instruments = set(target["instrument"])
    all_instruments = sorted(target_instruments | current_instruments)
    prices = _estimated_price_map(signal_date, all_instruments)
    score_map = {}
    if not score_df.empty and "instrument" in score_df.columns and "score" in score_df.columns:
        score_map = dict(zip(score_df["instrument"].astype(str), score_df["score"].astype(float)))
    target_map = {str(row["instrument"]): row.to_dict() for _, row in target.iterrows()}
    try:
        name_map = load_stock_identity_map()
    except Exception:
        name_map = {}
    rows = []
    for instrument in all_instruments:
        payload = target_map.get(instrument, {})
        current_payload = positions.get(instrument, {}) or {}
        current_shares = int(float(current_payload.get("amount", 0) or 0))
        target_weight = float(payload.get("target_weight", payload.get("weight", 0)) or 0)
        price = float(prices.get(instrument, current_payload.get("price", 0) or 0) or 0)
        target_shares = _target_shares(target_weight, total_capital, price)
        delta = int(target_shares - current_shares)
        rows.append({
            "signal_date": _date_str(signal_date),
            "execution_date": execution_date or "",
            "instrument": instrument,
            "security_name": security_name_for_instrument(instrument, name_map),
            "action": _action(delta, current_shares, target_shares),
            "current_shares": current_shares,
            "target_shares": target_shares,
            "delta_shares": delta,
            "target_weight": target_weight,
            "score": score_map.get(instrument),
            "target_value": target_weight * float(total_capital),
            "estimated_price": price if price > 0 else None,
            "estimated_notional": abs(delta) * price if price > 0 else None,
        })
    if not rows:
        return pd.DataFrame(columns=[
            "signal_date", "execution_date", "instrument", "security_name", "action",
            "current_shares", "target_shares", "delta_shares", "target_weight",
            "score", "target_value", "estimated_price", "estimated_notional",
        ])
    result = pd.DataFrame(rows)
    result = result.sort_values(["action", "target_weight", "instrument"], ascending=[True, False, True]).reset_index(drop=True)
    return result


def build_recommendation(
    *,
    model_id: str | None = None,
    model_run_id: str | None = None,
    signal_date: str | None = None,
    execution_date: str | None = None,
    account_id: str | None = None,
    topk: int = MODEL_DEFAULT_TOPK,
    total_capital: float = 1_000_000.0,
    strategy_contract_version: str = "top20_drop2_hold5_open_v1",
    n_drop: int = 2,
    hold_thresh: int = 5,
    deal_price: str = "open",
    run_kind: str = "on_time",
    data_package_id: str = "",
    identity_rows: pd.DataFrame | None = None,
    confidence_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_context = resolve_prediction_model_context(model_id=model_id, model_run_id=model_run_id, require_production=False)
    resolved_account_id = str(account_id or model_context["model_run_id"])
    signal_date = _date_str(signal_date or qlib_latest_trade_date())
    execution_date = resolve_pending_execution_date(signal_date, execution_date)
    resolved_confidence_policy = (
        normalize_confidence_policy(confidence_policy)
        if is_confidence_cash_contract(strategy_contract_version)
        else {}
    )
    model_confidence_evidence = (
        load_model_confidence_evidence(model_context)
        if is_confidence_cash_contract(strategy_contract_version)
        else {}
    )
    risk_policy_config = load_risk_policy_config()
    contract_hash = hashlib.sha256(
        json.dumps(
            {
                "account_id": resolved_account_id,
                "model_run_id": model_context["model_run_id"],
                "strategy_contract_version": strategy_contract_version,
                "topk": int(topk),
                "n_drop": int(n_drop),
                "hold_thresh": int(hold_thresh),
                "deal_price": deal_price,
                "total_capital": float(total_capital),
                "confidence_policy": resolved_confidence_policy,
                "risk_policy_hash": risk_policy_config_hash(risk_policy_config),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    recommendation_id = f"rec-{resolved_account_id}-{model_context['model_run_id']}-{signal_date}-{contract_hash}"
    out_dir = _recommendation_dir(model_context["model_run_id"], resolved_account_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    score_meta = export_daily_score(
        model_id=model_context["model_id"],
        model_run_id=model_context["model_run_id"],
        as_of_date=signal_date,
    )
    score_df = pd.read_csv(score_meta["score_file"])
    if identity_rows is None:
        identity_rows = load_stock_identity_rows_for_window(signal_date, signal_date)
    target_meta = build_target_portfolio(
        model_id=model_context["model_id"],
        model_run_id=model_context["model_run_id"],
        topk=topk,
        weighting="equal",
        total_capital=total_capital,
        score_meta=score_meta,
        score_df=score_df,
        identity_rows=identity_rows,
        strategy_contract_version=strategy_contract_version,
        confidence_policy=resolved_confidence_policy,
        model_confidence_evidence=model_confidence_evidence,
        evidence_as_of=signal_date,
        output_namespace=resolved_account_id,
    )
    target_df = pd.read_csv(target_meta["target_file"])
    current_state = load_current_position_state(resolved_account_id, total_capital)
    registry = TradingRegistry()
    account_history = registry.list_account_snapshots(resolved_account_id, limit=260)
    target_df, risk_decision = apply_risk_policy(
        target_df,
        signal_date=signal_date,
        total_capital=total_capital,
        account_history=account_history,
        current_state=current_state,
        config=risk_policy_config,
    )
    orders_df = build_order_preview(
        signal_date=signal_date,
        execution_date=execution_date,
        target_df=target_df,
        score_df=score_df,
        current_state=current_state,
        total_capital=total_capital,
    )

    decision_file = out_dir / f"portfolio_decision_{signal_date}.csv"
    order_preview_file = out_dir / f"orders_preview_{signal_date}.csv"
    recommendation_file = out_dir / f"recommendation_{signal_date}.json"
    risk_decision_file = out_dir / f"risk_decision_{signal_date}.json"
    target_df.to_csv(decision_file, index=False)
    orders_df.to_csv(order_preview_file, index=False)
    risk_decision_file.write_text(
        json.dumps(risk_decision, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    write_latest_risk_decision(
        {
            **risk_decision,
            "account_id": resolved_account_id,
            "model_id": model_context["model_id"],
            "model_run_id": model_context["model_run_id"],
            "risk_decision_file": str(risk_decision_file),
        }
    )

    warnings: list[str] = []
    if not execution_date:
        warnings.append("execution_date_unresolved: next trading date is not present in qlib calendar yet")
    if not has_qlib_trade_date(execution_date):
        warnings.append("execution_date_not_available_in_qlib: recommendation remains pending")
    confidence = target_meta.get("confidence") or {}
    if confidence.get("confidence_state") in {"weak", "no_trade"}:
        warnings.extend(str(item) for item in confidence.get("reasons") or [])

    payload = {
        "recommendation_id": recommendation_id,
        "account_id": resolved_account_id,
        "status": "pending",
        "model_id": model_context["model_id"],
        "model_run_id": model_context["model_run_id"],
        "model_status": model_context.get("status", ""),
        "feature_set_id": model_context.get("feature_set_id", ""),
        "signal_date": signal_date,
        "execution_date": execution_date or "",
        "topk": int(topk),
        "n_drop": int(n_drop),
        "hold_thresh": int(hold_thresh),
        "deal_price": deal_price,
        "total_capital": float(total_capital),
        "score_file": score_meta["score_file"],
        "target_file": str(decision_file),
        "decision_file": str(decision_file),
        "order_preview_file": str(order_preview_file),
        "recommendation_file": str(recommendation_file),
        "risk_decision_file": str(risk_decision_file),
        "current_state_source": current_state.get("source", ""),
        "current_state_as_of_date": current_state.get("as_of_date"),
        "record_count": int(len(target_df)),
        "order_preview_count": int(len(orders_df)),
        "buy_count": int((orders_df["action"] == "buy").sum()) if not orders_df.empty else 0,
        "sell_count": int((orders_df["action"] == "sell").sum()) if not orders_df.empty else 0,
        "hold_count": int((orders_df["action"] == "hold").sum()) if not orders_df.empty else 0,
        "metrics": {
            "score_quality": target_meta.get("score_quality", {}),
            "confidence": confidence,
            "target_stock_exposure": float(risk_decision.get("final_stock_cap") or 0.0),
            "target_cash_weight": float(risk_decision.get("final_cash_weight") or 0.0),
            "model_target_stock_exposure": float(target_meta.get("target_stock_exposure") or 0.0),
            "risk_policy": risk_decision,
            "target_market_counts": _market_counts(target_df["instrument"]) if "instrument" in target_df.columns else {},
            "order_action_counts": orders_df["action"].value_counts().astype(int).to_dict() if not orders_df.empty else {},
        },
        "warnings": warnings,
        "strategy_contract_version": strategy_contract_version,
        "contract_hash": contract_hash,
        "confidence_policy": resolved_confidence_policy,
        "risk_policy": risk_policy_config,
        "run_kind": run_kind,
        "data_package_id": data_package_id,
        "st_filter": target_meta.get("st_filter", {}),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    recommendation_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    registry.upsert_recommendation(payload, orders=orders_df.to_dict("records"))
    registry.supersede_pending_except(
        model_run_id=model_context["model_run_id"],
        account_id=resolved_account_id,
        keep_recommendation_id=recommendation_id,
        reason="superseded by newer recommendation for the same model run",
    )
    return payload


def write_latest_trading_status(payload: dict[str, Any]) -> None:
    TRADING_LATEST_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRADING_LATEST_STATUS_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
