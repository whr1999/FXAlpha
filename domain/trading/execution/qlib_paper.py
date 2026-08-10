from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from storage.paths import MODEL_DEFAULT_TOPK, PAPER_TRADING_RUNTIME_ROOT

from qlib.backtest.decision import Order, OrderDir
from qlib.backtest.exchange import Exchange
from qlib.backtest.position import Position
from qlib.data import D

from domain.model.qlib_strategy import (
    MISSING_SCORE_SELL_PRIORITY_VALUE,
    rank_current_holdings_with_missing_score_priority,
    rank_scores_with_instrument_tiebreak,
    tradable_buy_scores,
)
from domain.model.training_contract import (
    QLIB_REQUIRED_DEAL_PRICE,
    QLIB_REQUIRED_HOLD_THRESH,
    QLIB_REQUIRED_LIMIT_THRESHOLD,
)
from domain.trading.execution.base import ExecutionInput, ExecutionResult
from domain.trading.confidence import is_confidence_cash_contract
from domain.trading.prediction import init_qlib


ADAPTER_NAME = "qlib_exchange_paper"
MARK_TO_MARKET_ADAPTER_NAME = "qlib_exchange_paper_mark_to_market"
DEFAULT_OPEN_COST = 0.0015
DEFAULT_CLOSE_COST = 0.0025
DEFAULT_MIN_COST = 5.0


@dataclass
class PaperOrderPlan:
    sell: list[str]
    buy: list[str]
    ranked_current: list[str]
    buy_candidates: list[str]


def _date10(value: Any) -> str:
    return str(pd.Timestamp(value).date())


def _execution_dir(version_id: str, trade_date: str, recommendation_id: str = "") -> Path:
    suffix = recommendation_id or f"manual-{trade_date}"
    clean = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in suffix)
    return PAPER_TRADING_RUNTIME_ROOT / version_id / "executions" / f"{trade_date}_{clean}"


def _state_dir(version_id: str) -> Path:
    return PAPER_TRADING_RUNTIME_ROOT / version_id / "state"


def _state_file(version_id: str) -> Path:
    return _state_dir(version_id) / "account_state.json"


def _event_log_file(version_id: str) -> Path:
    return PAPER_TRADING_RUNTIME_ROOT / version_id / "paper_account_events.jsonl"


def _sha256_file(path: Path | str | None) -> str:
    if not path:
        return ""
    file_path = Path(path)
    if not file_path.exists():
        return ""
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


def _append_event(version_id: str, payload: dict[str, Any]) -> None:
    event_file = _event_log_file(version_id)
    event_file.parent.mkdir(parents=True, exist_ok=True)
    record = dict(payload)
    record.setdefault("version_id", version_id)
    record.setdefault("logged_at", datetime.now().isoformat(timespec="seconds"))
    with event_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _load_account_state(version_id: str, initial_capital: float) -> dict[str, Any]:
    file_path = _state_file(version_id)
    if file_path.exists():
        return json.loads(file_path.read_text(encoding="utf-8"))
    return {
        "account_id": version_id,
        "version_id": version_id,
        "as_of_date": None,
        "cash": float(initial_capital),
        "initial_capital": float(initial_capital),
        "account_value": float(initial_capital),
        "stock_value": 0.0,
        "positions": {},
        "source": "fresh_qlib_paper_start",
    }


def _state_to_position(state: dict[str, Any], initial_capital: float) -> Position:
    position_dict: dict[str, Any] = {"cash": float(state.get("cash", initial_capital) or 0)}
    for instrument, payload in (state.get("positions") or {}).items():
        if not isinstance(payload, dict):
            continue
        amount = float(payload.get("amount", payload.get("shares", 0)) or 0)
        if amount <= 0:
            continue
        position_dict[str(instrument)] = {
            "amount": amount,
            "price": float(payload.get("price", 0) or 0),
            "count_day": float(payload.get("count_day", payload.get("hold_days", 0)) or 0),
        }
    return Position(cash=float(position_dict.pop("cash", 0.0)), position_dict=position_dict) if position_dict else Position(cash=float(state.get("cash", initial_capital) or 0))


def _position_to_state(
    *,
    position: Position,
    version_id: str,
    trade_date: str,
    initial_capital: float,
    previous_state: dict[str, Any],
    recommendation_id: str,
    score_hash: str,
    target_hash: str,
    fills_hash: str,
    output_files: dict[str, str],
) -> dict[str, Any]:
    positions: dict[str, dict[str, Any]] = {}
    stock_value = 0.0
    for instrument in sorted(position.get_stock_list()):
        amount = float(position.get_stock_amount(instrument))
        price = float(position.get_stock_price(instrument) or 0)
        market_value = amount * price
        stock_value += market_value
        positions[instrument] = {
            "amount": amount,
            "shares": amount,
            "price": price,
            "market_value": market_value,
            "count_day": float(position.get_stock_count(instrument, "day")),
        }
    cash = float(position.get_cash())
    account_value = cash + stock_value
    return {
        "account_id": previous_state.get("account_id") or version_id,
        "version_id": version_id,
        "as_of_date": trade_date,
        "cash": cash,
        "initial_capital": float(previous_state.get("initial_capital", initial_capital) or initial_capital),
        "stock_value": stock_value,
        "account_value": account_value,
        "positions": positions,
        "source": ADAPTER_NAME,
        "source_recommendation_id": recommendation_id,
        "score_hash": score_hash,
        "target_hash": target_hash,
        "fills_hash": fills_hash,
        "output_files": output_files,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _load_score_series(score_file: Path) -> pd.Series:
    score_df = pd.read_csv(score_file)
    if score_df.empty or "instrument" not in score_df.columns or "score" not in score_df.columns:
        raise ValueError(f"invalid score file for qlib paper execution: {score_file}")
    score_df["instrument"] = score_df["instrument"].astype(str)
    score_df["score"] = pd.to_numeric(score_df["score"], errors="coerce")
    score_df = score_df.drop_duplicates("instrument", keep="first")
    return score_df.set_index("instrument")["score"]


def _load_target_frame(target_file: Path) -> pd.DataFrame:
    if not target_file.exists():
        raise ValueError(f"target file missing for target-weight execution: {target_file}")
    target = pd.read_csv(target_file)
    if "instrument" not in target.columns or "target_weight" not in target.columns:
        raise ValueError(f"invalid target file for target-weight execution: {target_file}")
    target = target.copy()
    target["instrument"] = target["instrument"].astype(str)
    target["target_weight"] = pd.to_numeric(target["target_weight"], errors="coerce")
    if target["target_weight"].isna().any() or (target["target_weight"] < 0).any():
        raise ValueError("target_weight_invalid: weights must be finite and non-negative")
    if target["instrument"].duplicated().any():
        raise ValueError("target_instrument_duplicate")
    total_weight = float(target["target_weight"].sum())
    if total_weight > 1.0 + 1e-9:
        raise ValueError(f"target_weight_sum_above_one:{total_weight:.12f}")
    return target.sort_values(["target_weight", "instrument"], ascending=[False, True]).reset_index(drop=True)


def _target_weight_rebalance(
    *,
    exchange: Exchange,
    position: Position,
    target: pd.DataFrame,
    trade_date: str,
    topk: int,
    n_drop: int,
    hold_thresh: int,
    execution_version: str = "target_weight_v2",
    risk_cap_reduction: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    start_ts = pd.Timestamp(trade_date)
    original_stock_list = list(position.get_stock_list())
    target_map = {
        str(row["instrument"]): float(row["target_weight"])
        for _, row in target.iterrows()
        if float(row["target_weight"]) > 0
    }
    all_codes = sorted(set(original_stock_list) | set(target_map))
    prices: dict[str, float] = {}
    for instrument in all_codes:
        direction = OrderDir.BUY if instrument in target_map else OrderDir.SELL
        price = exchange.get_deal_price(instrument, start_time=start_ts, end_time=start_ts, direction=direction)
        if price is None or pd.isna(price) or float(price) <= 0:
            price = position.get_stock_price(instrument) if instrument in position.get_stock_list() else 0
        if price is not None and float(price) > 0:
            prices[instrument] = float(price)
    pretrade_value = float(position.get_cash()) + sum(
        float(position.get_stock_amount(instrument)) * float(prices.get(instrument, position.get_stock_price(instrument) or 0))
        for instrument in original_stock_list
    )
    target_amounts: dict[str, float] = {}
    for instrument, target_weight in target_map.items():
        price = prices.get(instrument, 0.0)
        if price <= 0:
            target_amounts[instrument] = 0.0
            continue
        factor = exchange.get_factor(instrument, start_time=start_ts, end_time=start_ts)
        target_amounts[instrument] = float(
            exchange.round_amount_by_trade_unit(
                target_weight * pretrade_value / price,
                factor,
                stock_id=instrument,
                start_time=start_ts,
                end_time=start_ts,
            )
        )

    order_rows: list[dict[str, Any]] = []
    fill_rows: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []

    # A market/account risk-cap reduction overrides strategic n_drop/hold5, but never
    # exchange tradability, lot size, price-limit or cash constraints.
    for instrument in sorted(original_stock_list):
        current_amount = float(position.get_stock_amount(instrument))
        target_amount = float(target_amounts.get(instrument, 0.0))
        sell_amount = max(current_amount - target_amount, 0.0)
        if sell_amount <= 0:
            continue
        hold_count = float(position.get_stock_count(instrument, "day"))
        blocked_reason = "hold_thresh" if hold_count < int(hold_thresh) and not risk_cap_reduction else ""
        order_rows.append(
            {
                "trade_date": trade_date,
                "instrument": instrument,
                "action": "sell",
                "planned_amount": sell_amount,
                "current_amount": current_amount,
                "target_amount": target_amount,
                "target_weight": float(target_map.get(instrument, 0.0)),
                "hold_count": hold_count,
                "blocked_reason": blocked_reason,
                "risk_reduction_override": True,
                "risk_cap_reduction": bool(risk_cap_reduction),
            }
        )
        if blocked_reason:
            constraints.append({"instrument": instrument, "action": "sell", "reason": blocked_reason})
            continue
        if not exchange.is_stock_tradable(instrument, start_time=start_ts, end_time=start_ts, direction=OrderDir.SELL):
            fill_rows.append({"instrument": instrument, "action": "sell", "requested_amount": sell_amount, "filled_amount": 0.0, "status": "skipped", "reason": "not_tradable"})
            constraints.append({"instrument": instrument, "action": "sell", "reason": "not_tradable"})
            continue
        order = Order(stock_id=instrument, amount=sell_amount, start_time=start_ts, end_time=start_ts, direction=Order.SELL)
        fill_rows.append(_deal_order(exchange, position, order, "sell"))

    target_stock_value = float(sum(target_map.values())) * pretrade_value
    stock_value_after_sells = sum(
        float(position.get_stock_amount(instrument))
        * float(prices.get(instrument, position.get_stock_price(instrument) or 0))
        for instrument in position.get_stock_list()
    )
    gross_budget_remaining = max(target_stock_value - stock_value_after_sells, 0.0)
    new_targets = [instrument for instrument in target_map if instrument not in original_stock_list]
    new_buy_limit = max(int(n_drop) + int(topk) - len(original_stock_list), 0)
    allowed_new = set(new_targets[:new_buy_limit])
    for instrument in target_map:
        current_amount = float(position.get_stock_amount(instrument)) if instrument in position.get_stock_list() else 0.0
        target_amount = float(target_amounts.get(instrument, 0.0))
        buy_amount = max(target_amount - current_amount, 0.0)
        if buy_amount <= 0:
            continue
        if gross_budget_remaining <= 0:
            constraints.append({"instrument": instrument, "action": "buy", "reason": "gross_exposure_budget_exhausted"})
            continue
        if instrument not in original_stock_list and instrument not in allowed_new:
            constraints.append({"instrument": instrument, "action": "buy", "reason": "n_drop_ramp_limit"})
            continue
        price = float(prices.get(instrument, 0.0))
        order_rows.append(
            {
                "trade_date": trade_date,
                "instrument": instrument,
                "action": "buy",
                "planned_amount": buy_amount,
                "planned_value": buy_amount * price,
                "current_amount": current_amount,
                "target_amount": target_amount,
                "target_weight": float(target_map[instrument]),
                "blocked_reason": "",
            }
        )
        if not exchange.is_stock_tradable(instrument, start_time=start_ts, end_time=start_ts, direction=OrderDir.BUY):
            fill_rows.append({"instrument": instrument, "action": "buy", "requested_amount": buy_amount, "filled_amount": 0.0, "status": "skipped", "reason": "not_tradable"})
            constraints.append({"instrument": instrument, "action": "buy", "reason": "not_tradable"})
            continue
        if price <= 0:
            fill_rows.append({"instrument": instrument, "action": "buy", "requested_amount": buy_amount, "filled_amount": 0.0, "status": "skipped", "reason": "missing_deal_price"})
            constraints.append({"instrument": instrument, "action": "buy", "reason": "missing_deal_price"})
            continue
        factor = exchange.get_factor(instrument, start_time=start_ts, end_time=start_ts)
        affordable = exchange.round_amount_by_trade_unit(
            float(position.get_cash()) / (price * (1.0 + DEFAULT_OPEN_COST)),
            factor,
            stock_id=instrument,
            start_time=start_ts,
            end_time=start_ts,
        )
        budget_amount = exchange.round_amount_by_trade_unit(
            gross_budget_remaining / price,
            factor,
            stock_id=instrument,
            start_time=start_ts,
            end_time=start_ts,
        )
        requested = min(float(buy_amount), float(affordable), float(budget_amount))
        if requested <= 0:
            reason = "gross_exposure_budget_rounding" if gross_budget_remaining < price * 100 else "insufficient_cash"
            constraints.append({"instrument": instrument, "action": "buy", "reason": reason})
            continue
        order = Order(stock_id=instrument, amount=requested, start_time=start_ts, end_time=start_ts, direction=Order.BUY)
        fill = _deal_order(exchange, position, order, "buy")
        fill_rows.append(fill)
        gross_budget_remaining = max(gross_budget_remaining - float(fill.get("trade_value") or 0.0), 0.0)

    return order_rows, fill_rows, {
        "mode": execution_version,
        "pretrade_account_value": pretrade_value,
        "target_stock_exposure": float(sum(target_map.values())),
        "target_cash_weight": 1.0 - float(sum(target_map.values())),
        "target_amounts": target_amounts,
        "new_buy_limit": new_buy_limit,
        "target_stock_value": target_stock_value,
        "stock_value_after_sells": stock_value_after_sells,
        "gross_budget_remaining": gross_budget_remaining,
        "constraints": constraints,
        "risk_reduction_overrides_n_drop": True,
        "risk_reduction_overrides_hold_thresh": bool(risk_cap_reduction),
    }


def build_topk_dropout_plan(
    *,
    pred_score: pd.Series,
    current_stock_list: list[str],
    topk: int,
    n_drop: int,
) -> PaperOrderPlan:
    last = rank_current_holdings_with_missing_score_priority(
        pred_score,
        current_stock_list,
        missing_score_value=MISSING_SCORE_SELL_PRIORITY_VALUE,
    )
    buy_score = tradable_buy_scores(pred_score)
    today_count = max(int(n_drop) + int(topk) - len(last), 0)
    today = list(rank_scores_with_instrument_tiebreak(buy_score[~buy_score.index.isin(last)])[:today_count])
    comb_scores = buy_score.reindex(last.union(pd.Index(today))).fillna(MISSING_SCORE_SELL_PRIORITY_VALUE)
    comb = rank_scores_with_instrument_tiebreak(comb_scores)
    sell = list(last[last.isin(comb[-int(n_drop) :])]) if int(n_drop) > 0 else []
    buy_count = max(len(sell) + int(topk) - len(last), 0)
    buy = today[:buy_count]
    return PaperOrderPlan(sell=sell, buy=buy, ranked_current=list(last), buy_candidates=today)


def _build_exchange(*, trade_date: str, codes: list[str], deal_price: str) -> Exchange:
    limit_threshold = list(QLIB_REQUIRED_LIMIT_THRESHOLD) if str(deal_price).strip().lstrip("$") == "open" else None
    return Exchange(
        freq="day",
        start_time=trade_date,
        end_time=trade_date,
        codes=sorted(set(codes)) or "all",
        deal_price=deal_price,
        open_cost=DEFAULT_OPEN_COST,
        close_cost=DEFAULT_CLOSE_COST,
        min_cost=DEFAULT_MIN_COST,
        limit_threshold=limit_threshold,
    )


def _deal_order(exchange: Exchange, position: Position, order: Order, action: str) -> dict[str, Any]:
    before_cash = float(position.get_cash())
    trade_val, trade_cost, trade_price = exchange.deal_order(order, position=position)
    after_cash = float(position.get_cash())
    status = "filled" if float(trade_val or 0) > 0 and float(order.deal_amount or 0) > 0 else "skipped"
    return {
        "instrument": order.stock_id,
        "action": action,
        "requested_amount": float(order.amount),
        "filled_amount": float(order.deal_amount or 0),
        "price": float(trade_price) if pd.notna(trade_price) else None,
        "trade_value": float(trade_val or 0),
        "cost": float(trade_cost or 0),
        "cash_before": before_cash,
        "cash_after": after_cash,
        "status": status,
    }


def _close_price_map(trade_date: str, instruments: list[str]) -> dict[str, float]:
    if not instruments:
        return {}
    frame = D.features(
        sorted(set(instruments)),
        ["$close"],
        start_time=trade_date,
        end_time=trade_date,
        freq="day",
        disk_cache=False,
    )
    if frame.empty:
        return {}
    df = frame.reset_index()
    df["instrument"] = df["instrument"].astype(str)
    return {
        str(row["instrument"]): float(row["$close"])
        for _, row in df.iterrows()
        if pd.notna(row.get("$close")) and float(row["$close"]) > 0
    }


def _mark_positions_to_close(position: Position, trade_date: str) -> None:
    prices = _close_price_map(trade_date, position.get_stock_list())
    for instrument, price in prices.items():
        if instrument in position.get_stock_list():
            position.update_stock_price(instrument, float(price))


def _positions_frame(position: Position, trade_date: str) -> pd.DataFrame:
    rows = []
    for instrument in sorted(position.get_stock_list()):
        amount = float(position.get_stock_amount(instrument))
        price = float(position.get_stock_price(instrument) or 0)
        rows.append({
            "trade_date": trade_date,
            "instrument": instrument,
            "amount": amount,
            "shares": amount,
            "price": price,
            "market_value": amount * price,
            "count_day": float(position.get_stock_count(instrument, "day")),
        })
    return pd.DataFrame(rows)


def _ledger_row(
    *,
    trade_date: str,
    previous_state: dict[str, Any],
    account_state: dict[str, Any],
    fills: pd.DataFrame,
) -> dict[str, Any]:
    previous_value = float(previous_state.get("account_value") or previous_state.get("initial_capital") or account_state.get("initial_capital") or 0)
    ending_value = float(account_state.get("account_value") or 0)
    daily_pnl = ending_value - previous_value
    filled = fills.loc[fills.get("status") == "filled"] if not fills.empty else fills
    buy_rows = filled.loc[filled.get("action") == "buy"] if not filled.empty else filled
    sell_rows = filled.loc[filled.get("action") == "sell"] if not filled.empty else filled
    buy_value = float(pd.to_numeric(buy_rows.get("trade_value"), errors="coerce").fillna(0).sum()) if not buy_rows.empty else 0.0
    sell_value = float(pd.to_numeric(sell_rows.get("trade_value"), errors="coerce").fillna(0).sum()) if not sell_rows.empty else 0.0
    trading_cost = float(pd.to_numeric(filled.get("cost"), errors="coerce").fillna(0).sum()) if not filled.empty else 0.0
    return {
        "trade_date": trade_date,
        "starting_account_value": previous_value,
        "ending_account_value": ending_value,
        "daily_pnl": daily_pnl,
        "daily_return": daily_pnl / previous_value if previous_value else 0.0,
        "cash": float(account_state.get("cash") or 0),
        "stock_value": float(account_state.get("stock_value") or 0),
        "trade_count": int((fills.get("status") == "filled").sum()) if not fills.empty else 0,
        "buy_count": int(((fills.get("action") == "buy") & (fills.get("status") == "filled")).sum()) if not fills.empty else 0,
        "sell_count": int(((fills.get("action") == "sell") & (fills.get("status") == "filled")).sum()) if not fills.empty else 0,
        "buy_value": buy_value,
        "sell_value": sell_value,
        "turnover": buy_value + sell_value,
        "trading_cost": trading_cost,
    }


def run_qlib_paper_execution(
    inputs: ExecutionInput,
    *,
    topk: int = MODEL_DEFAULT_TOPK,
    n_drop: int = 2,
    hold_thresh: int = QLIB_REQUIRED_HOLD_THRESH,
    deal_price: str = QLIB_REQUIRED_DEAL_PRICE,
) -> ExecutionResult:
    version_id = inputs.version_id
    trade_date = _date10(inputs.trade_date)
    recommendation_id = str(inputs.extra.get("recommendation_id") or "")
    previous_state = _load_account_state(version_id, inputs.initial_capital)
    if previous_state.get("as_of_date") == trade_date:
        return ExecutionResult(
            ok=False,
            adapter=ADAPTER_NAME,
            version_id=version_id,
            trade_date=trade_date,
            diagnostics={"reason": "account_already_executed_trade_date", "state_file": str(_state_file(version_id))},
            notes=["qlib paper execution refused to replay an already applied account date"],
        )

    init_qlib()
    score_file = Path(inputs.score_file)
    target_file = Path(inputs.target_file)
    out_dir = _execution_dir(version_id, trade_date, recommendation_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    frozen_score_file = out_dir / "scores.csv"
    frozen_target_file = out_dir / "target.csv"
    shutil.copy2(score_file, frozen_score_file)
    if target_file.exists():
        shutil.copy2(target_file, frozen_target_file)

    pred_score = _load_score_series(frozen_score_file)
    position = _state_to_position(previous_state, inputs.initial_capital)
    current_stock_list = position.get_stock_list()
    strategy_contract_version = str(inputs.extra.get("strategy_contract_version") or "")
    recommendation_metrics = inputs.extra.get("recommendation_metrics") or {}
    risk_policy_decision = recommendation_metrics.get("risk_policy") or {}
    risk_policy_enforced = bool(risk_policy_decision.get("enforced"))
    target_weight_mode = is_confidence_cash_contract(strategy_contract_version) or risk_policy_enforced
    if target_weight_mode:
        target = _load_target_frame(frozen_target_file)
        involved_codes = sorted(set(current_stock_list) | set(target["instrument"].astype(str)))
        exchange = _build_exchange(trade_date=trade_date, codes=involved_codes, deal_price=deal_price)
        confidence_metrics = recommendation_metrics.get("confidence") or {}
        execution_version = str(
            confidence_metrics.get("execution_version")
            or ("risk_target_weight_v1" if risk_policy_enforced else "target_weight_v2")
        )
        risk_cap_reduction = bool(
            risk_policy_enforced
            and float(risk_policy_decision.get("final_stock_cap") or 0.0)
            < float(risk_policy_decision.get("model_cap") or 0.0) - 1e-12
        )
        order_rows, fill_rows, plan_diagnostics = _target_weight_rebalance(
            exchange=exchange,
            position=position,
            target=target,
            trade_date=trade_date,
            topk=topk,
            n_drop=n_drop,
            hold_thresh=hold_thresh,
            execution_version=execution_version,
            risk_cap_reduction=risk_cap_reduction,
        )
    else:
        plan = build_topk_dropout_plan(
            pred_score=pred_score,
            current_stock_list=current_stock_list,
            topk=topk,
            n_drop=n_drop,
        )
        involved_codes = sorted(set(current_stock_list) | set(plan.sell) | set(plan.buy) | set(pred_score.index[: max(topk + n_drop, topk)]))
        exchange = _build_exchange(trade_date=trade_date, codes=involved_codes, deal_price=deal_price)

        order_rows = []
        fill_rows = []
        start_ts = pd.Timestamp(trade_date)
        for instrument in plan.sell:
            hold_count = float(position.get_stock_count(instrument, "day")) if instrument in position.get_stock_list() else 0.0
            amount = float(position.get_stock_amount(instrument)) if instrument in position.get_stock_list() else 0.0
            order_rows.append({
                "trade_date": trade_date,
                "instrument": instrument,
                "action": "sell",
                "planned_amount": amount,
                "hold_count": hold_count,
                "blocked_reason": "hold_thresh" if hold_count < int(hold_thresh) else "",
            })
            if amount <= 0 or hold_count < int(hold_thresh):
                continue
            if not exchange.is_stock_tradable(instrument, start_time=start_ts, end_time=start_ts, direction=OrderDir.SELL):
                fill_rows.append({"instrument": instrument, "action": "sell", "requested_amount": amount, "filled_amount": 0.0, "status": "skipped", "reason": "not_tradable"})
                continue
            order = Order(stock_id=instrument, amount=amount, start_time=start_ts, end_time=start_ts, direction=Order.SELL)
            fill_rows.append(_deal_order(exchange, position, order, "sell"))

        cash_for_buys = float(position.get_cash())
        buy_value = cash_for_buys / len(plan.buy) if plan.buy else 0.0
        for instrument in plan.buy:
            order_rows.append({
                "trade_date": trade_date,
                "instrument": instrument,
                "action": "buy",
                "planned_value": buy_value,
                "blocked_reason": "",
            })
            if buy_value <= 0:
                continue
            if not exchange.is_stock_tradable(instrument, start_time=start_ts, end_time=start_ts, direction=OrderDir.BUY):
                fill_rows.append({"instrument": instrument, "action": "buy", "requested_amount": 0.0, "filled_amount": 0.0, "status": "skipped", "reason": "not_tradable"})
                continue
            price = exchange.get_deal_price(instrument, start_time=start_ts, end_time=start_ts, direction=OrderDir.BUY)
            if price is None or pd.isna(price) or float(price) <= 0:
                fill_rows.append({"instrument": instrument, "action": "buy", "requested_amount": 0.0, "filled_amount": 0.0, "status": "skipped", "reason": "missing_deal_price"})
                continue
            factor = exchange.get_factor(instrument, start_time=start_ts, end_time=start_ts)
            amount = exchange.round_amount_by_trade_unit(float(buy_value) / float(price), factor, stock_id=instrument, start_time=start_ts, end_time=start_ts)
            order = Order(stock_id=instrument, amount=amount, start_time=start_ts, end_time=start_ts, direction=Order.BUY)
            fill_rows.append(_deal_order(exchange, position, order, "buy"))
        plan_diagnostics = {
            "mode": "legacy_topk_dropout",
            "sell": plan.sell,
            "buy": plan.buy,
            "ranked_current": plan.ranked_current,
            "buy_candidates": plan.buy_candidates,
        }

    position.add_count_all("day")
    _mark_positions_to_close(position, trade_date)

    orders_df = pd.DataFrame(order_rows)
    fills_df = pd.DataFrame(fill_rows)
    positions_df = _positions_frame(position, trade_date)
    orders_file = out_dir / "orders.csv"
    fills_file = out_dir / "fills.csv"
    positions_file = out_dir / "positions.csv"
    ledger_file = out_dir / "ledger.csv"
    account_state_file = _state_file(version_id)
    execution_meta_file = out_dir / "execution_meta.json"
    orders_df.to_csv(orders_file, index=False)
    fills_df.to_csv(fills_file, index=False)
    positions_df.to_csv(positions_file, index=False)

    output_files = {
        "score_file": str(frozen_score_file),
        "target_file": str(frozen_target_file),
        "orders_file": str(orders_file),
        "trades_file": str(fills_file),
        "fills_file": str(fills_file),
        "holdings_file": str(positions_file),
        "positions_file": str(positions_file),
        "ledger_file": str(ledger_file),
        "account_state_file": str(account_state_file),
        "staged_account_state_file": str(out_dir / "account_state.json"),
        "event_log_file": str(_event_log_file(version_id)),
        "execution_meta_file": str(execution_meta_file),
    }
    score_hash = _sha256_file(frozen_score_file)
    target_hash = _sha256_file(frozen_target_file)
    fills_hash = _sha256_file(fills_file)
    account_state = _position_to_state(
        position=position,
        version_id=version_id,
        trade_date=trade_date,
        initial_capital=inputs.initial_capital,
        previous_state=previous_state,
        recommendation_id=recommendation_id,
        score_hash=score_hash,
        target_hash=target_hash,
        fills_hash=fills_hash,
        output_files=output_files,
    )
    actual_stock_exposure = (
        float(account_state.get("stock_value") or 0) / float(account_state.get("account_value") or 0)
        if float(account_state.get("account_value") or 0) > 0
        else 0.0
    )
    target_stock_exposure = float(plan_diagnostics.get("target_stock_exposure", 1.0 if not target_weight_mode else 0.0))
    account_state.update(
        {
            "execution_mode": plan_diagnostics.get("mode", "legacy_topk_dropout"),
            "target_stock_exposure": target_stock_exposure,
            "target_cash_weight": float(plan_diagnostics.get("target_cash_weight", 1.0 - target_stock_exposure)),
            "actual_stock_exposure": actual_stock_exposure,
            "actual_cash_weight": 1.0 - actual_stock_exposure,
            "exposure_gap": actual_stock_exposure - target_stock_exposure,
            "execution_constraints": plan_diagnostics.get("constraints", []),
            "risk_metrics": {
                "execution_mode": plan_diagnostics.get("mode", "legacy_topk_dropout"),
                "target_stock_exposure": target_stock_exposure,
                "target_cash_weight": float(plan_diagnostics.get("target_cash_weight", 1.0 - target_stock_exposure)),
                "actual_stock_exposure": actual_stock_exposure,
                "actual_cash_weight": 1.0 - actual_stock_exposure,
                "exposure_gap": actual_stock_exposure - target_stock_exposure,
                "execution_constraints": plan_diagnostics.get("constraints", []),
                "policy": risk_policy_decision,
            },
        }
    )
    ledger_row = _ledger_row(
        trade_date=trade_date,
        previous_state=previous_state,
        account_state=account_state,
        fills=fills_df,
    )
    pd.DataFrame([ledger_row]).to_csv(ledger_file, index=False)
    _write_json(out_dir / "account_state.json", account_state)

    metrics = {
        **ledger_row,
        "account_id": account_state["account_id"],
        "position_count": int(len(account_state.get("positions") or {})),
        "score_hash": score_hash,
        "target_hash": target_hash,
        "fills_hash": fills_hash,
        "topk": int(topk),
        "n_drop": int(n_drop),
        "hold_thresh": int(hold_thresh),
        "deal_price": deal_price,
        "execution_mode": plan_diagnostics.get("mode", "legacy_topk_dropout"),
        "target_stock_exposure": target_stock_exposure,
        "target_cash_weight": float(plan_diagnostics.get("target_cash_weight", 1.0 - target_stock_exposure)),
        "actual_stock_exposure": actual_stock_exposure,
        "actual_cash_weight": 1.0 - actual_stock_exposure,
        "exposure_gap": actual_stock_exposure - target_stock_exposure,
        "constraint_count": len(plan_diagnostics.get("constraints", [])),
    }
    diagnostics = {
        "plan": plan_diagnostics,
        "previous_state_as_of_date": previous_state.get("as_of_date"),
        "output_dir": str(out_dir),
    }
    _write_json(
        execution_meta_file,
        {
            "adapter": ADAPTER_NAME,
            "version_id": version_id,
            "trade_date": trade_date,
            "recommendation_id": recommendation_id,
            "metrics": metrics,
            "diagnostics": diagnostics,
            "output_files": output_files,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    _append_event(
        version_id,
        {
            "event": "rebalance_execution",
            "adapter": ADAPTER_NAME,
            "trade_date": trade_date,
            "recommendation_id": recommendation_id,
            "metrics": metrics,
            "output_files": output_files,
            "diagnostics": diagnostics,
        },
    )
    # Publish the canonical account state last.  All frozen evidence needed to
    # reconcile a process crash is durable before the account can advance.
    _write_json(account_state_file, account_state)
    return ExecutionResult(
        ok=True,
        adapter=ADAPTER_NAME,
        version_id=version_id,
        trade_date=trade_date,
        output_files=output_files,
        metrics=metrics,
        notes=["executed via qlib Exchange + Position paper account"],
        diagnostics=diagnostics,
    )


def run_qlib_paper_mark_to_market(
    *,
    version_id: str,
    trade_date: str,
    initial_capital: float,
) -> ExecutionResult:
    trade_date = _date10(trade_date)
    previous_state = _load_account_state(version_id, initial_capital)
    if previous_state.get("as_of_date") == trade_date:
        return ExecutionResult(
            ok=False,
            adapter=MARK_TO_MARKET_ADAPTER_NAME,
            version_id=version_id,
            trade_date=trade_date,
            diagnostics={"reason": "account_already_marked_trade_date", "state_file": str(_state_file(version_id))},
        )
    init_qlib()
    out_dir = _execution_dir(version_id, trade_date, f"mtm-{trade_date}")
    out_dir.mkdir(parents=True, exist_ok=True)
    position = _state_to_position(previous_state, initial_capital)
    position.add_count_all("day")
    _mark_positions_to_close(position, trade_date)

    fills_df = pd.DataFrame(columns=["instrument", "action", "requested_amount", "filled_amount", "status"])
    positions_df = _positions_frame(position, trade_date)
    fills_file = out_dir / "fills.csv"
    positions_file = out_dir / "positions.csv"
    ledger_file = out_dir / "ledger.csv"
    account_state_file = _state_file(version_id)
    execution_meta_file = out_dir / "execution_meta.json"
    fills_df.to_csv(fills_file, index=False)
    positions_df.to_csv(positions_file, index=False)
    output_files = {
        "trades_file": str(fills_file),
        "fills_file": str(fills_file),
        "holdings_file": str(positions_file),
        "positions_file": str(positions_file),
        "ledger_file": str(ledger_file),
        "account_state_file": str(account_state_file),
        "staged_account_state_file": str(out_dir / "account_state.json"),
        "event_log_file": str(_event_log_file(version_id)),
        "execution_meta_file": str(execution_meta_file),
    }
    fills_hash = _sha256_file(fills_file)
    account_state = _position_to_state(
        position=position,
        version_id=version_id,
        trade_date=trade_date,
        initial_capital=initial_capital,
        previous_state=previous_state,
        recommendation_id="",
        score_hash="",
        target_hash="",
        fills_hash=fills_hash,
        output_files=output_files,
    )
    account_state["source"] = MARK_TO_MARKET_ADAPTER_NAME
    account_value = float(account_state.get("account_value") or 0)
    actual_stock_exposure = (
        float(account_state.get("stock_value") or 0) / account_value if account_value > 0 else 0.0
    )
    account_state.update(
        {
            "execution_mode": "mark_to_market",
            "target_stock_exposure": previous_state.get("target_stock_exposure"),
            "target_cash_weight": previous_state.get("target_cash_weight"),
            "actual_stock_exposure": actual_stock_exposure,
            "actual_cash_weight": 1.0 - actual_stock_exposure,
            "exposure_gap": (
                actual_stock_exposure - float(previous_state["target_stock_exposure"])
                if previous_state.get("target_stock_exposure") is not None
                else None
            ),
            "execution_constraints": list(previous_state.get("execution_constraints") or []),
        }
    )
    ledger_row = _ledger_row(
        trade_date=trade_date,
        previous_state=previous_state,
        account_state=account_state,
        fills=fills_df,
    )
    pd.DataFrame([ledger_row]).to_csv(ledger_file, index=False)
    _write_json(out_dir / "account_state.json", account_state)
    metrics = {
        **ledger_row,
        "account_id": account_state["account_id"],
        "position_count": int(len(account_state.get("positions") or {})),
        "fills_hash": fills_hash,
        "mark_to_market": True,
    }
    diagnostics = {
        "previous_state_as_of_date": previous_state.get("as_of_date"),
        "output_dir": str(out_dir),
    }
    _write_json(
        execution_meta_file,
        {
            "adapter": MARK_TO_MARKET_ADAPTER_NAME,
            "version_id": version_id,
            "trade_date": trade_date,
            "metrics": metrics,
            "diagnostics": diagnostics,
            "output_files": output_files,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    _append_event(
        version_id,
        {
            "event": "mark_to_market",
            "adapter": MARK_TO_MARKET_ADAPTER_NAME,
            "trade_date": trade_date,
            "metrics": metrics,
            "output_files": output_files,
            "diagnostics": diagnostics,
        },
    )
    _write_json(account_state_file, account_state)
    return ExecutionResult(
        ok=True,
        adapter=MARK_TO_MARKET_ADAPTER_NAME,
        version_id=version_id,
        trade_date=trade_date,
        output_files=output_files,
        metrics=metrics,
        notes=["marked qlib paper account to market without rebalance"],
        diagnostics=diagnostics,
    )


def backfill_qlib_paper_account(
    *,
    version_id: str,
    target_date: str,
    initial_capital: float,
) -> list[ExecutionResult]:
    init_qlib()
    state = _load_account_state(version_id, initial_capital)
    start = state.get("as_of_date")
    if not start:
        return []
    calendar = pd.Index(pd.to_datetime(D.calendar(freq="day")).normalize())
    start_ts = pd.Timestamp(start).normalize()
    target_ts = pd.Timestamp(target_date).normalize()
    dates = calendar[(calendar > start_ts) & (calendar <= target_ts)]
    results: list[ExecutionResult] = []
    for dt in dates:
        results.append(
            run_qlib_paper_mark_to_market(
                version_id=version_id,
                trade_date=_date10(dt),
                initial_capital=initial_capital,
            )
        )
    return results
