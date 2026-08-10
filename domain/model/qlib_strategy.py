from __future__ import annotations

import copy
from typing import Iterable

import numpy as np
import pandas as pd
from qlib.backtest.decision import Order, OrderDir, TradeDecisionWO
from qlib.backtest.position import Position
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy


MISSING_SCORE_SELL_PRIORITY_VALUE = -1.0e30


def rank_scores_with_instrument_tiebreak(
    scores: pd.Series,
    *,
    ascending: bool = False,
) -> pd.Index:
    """Rank scores deterministically, using instrument text as the tie-break."""

    numeric = pd.to_numeric(scores, errors="coerce")
    frame = pd.DataFrame(
        {
            "instrument": numeric.index.map(str),
            "score": numeric.to_numpy(),
        }
    )
    ranked = frame.sort_values(
        ["score", "instrument"],
        ascending=[ascending, True],
        kind="mergesort",
        na_position="last",
    )
    return pd.Index(ranked["instrument"].tolist())


def rank_current_holdings_with_missing_score_priority(
    pred_score: pd.Series,
    current_stock_list: Iterable[str],
    *,
    missing_score_value: float = MISSING_SCORE_SELL_PRIORITY_VALUE,
) -> pd.Index:
    """Rank held stocks, treating missing scores as explicit sell candidates."""

    current_scores = pd.to_numeric(pred_score.reindex(list(current_stock_list)), errors="coerce")
    return rank_scores_with_instrument_tiebreak(current_scores.fillna(float(missing_score_value)))


def tradable_buy_scores(pred_score: pd.Series) -> pd.Series:
    """Scores eligible for new buys; missing-score rows are never buy candidates."""

    return pd.to_numeric(pred_score, errors="coerce").dropna()


class FXAlphaTopkDropoutStrategy(TopkDropoutStrategy):
    """Qlib TopK strategy with explicit liquidation priority for missing scores.

    FXAlpha's model feature universe is the fixed baseline universe recorded by
    the adopted factor-value store; it is not filtered point-in-time during
    training or prediction feature construction.  Live ST buy filtering is a
    separate trading-signal rule.  This strategy treats any missing score as a
    sell-priority condition and never admits it as a new buy candidate.  Order
    tradability, limit-up and limit-down checks remain owned by Qlib Exchange.
    """

    def __init__(
        self,
        *,
        missing_score_sell_priority_value: float = MISSING_SCORE_SELL_PRIORITY_VALUE,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.missing_score_sell_priority_value = float(missing_score_sell_priority_value)

    def generate_trade_decision(self, execute_result=None):  # type: ignore[override]
        trade_step = self.trade_calendar.get_trade_step()
        trade_start_time, trade_end_time = self.trade_calendar.get_step_time(trade_step)
        pred_start_time, pred_end_time = self.trade_calendar.get_step_time(trade_step, shift=1)
        pred_score = self.signal.get_signal(start_time=pred_start_time, end_time=pred_end_time)
        if isinstance(pred_score, pd.DataFrame):
            pred_score = pred_score.iloc[:, 0]
        if pred_score is None:
            return TradeDecisionWO([], self)

        pred_score = pd.to_numeric(pred_score, errors="coerce")
        buy_score = tradable_buy_scores(pred_score)

        if self.only_tradable:
            def get_first_n(li, n, reverse=False):
                cur_n = 0
                res = []
                for si in reversed(li) if reverse else li:
                    if self.trade_exchange.is_stock_tradable(
                        stock_id=si, start_time=trade_start_time, end_time=trade_end_time
                    ):
                        res.append(si)
                        cur_n += 1
                        if cur_n >= n:
                            break
                return res[::-1] if reverse else res

            def get_last_n(li, n):
                return get_first_n(li, n, reverse=True)

            def filter_stock(li):
                return [
                    si
                    for si in li
                    if self.trade_exchange.is_stock_tradable(
                        stock_id=si, start_time=trade_start_time, end_time=trade_end_time
                    )
                ]
        else:
            def get_first_n(li, n):
                return list(li)[:n]

            def get_last_n(li, n):
                return list(li)[-n:]

            def filter_stock(li):
                return li

        current_temp: Position = copy.deepcopy(self.trade_position)
        sell_order_list = []
        buy_order_list = []
        cash = current_temp.get_cash()
        current_stock_list = current_temp.get_stock_list()
        last = rank_current_holdings_with_missing_score_priority(
            pred_score,
            current_stock_list,
            missing_score_value=self.missing_score_sell_priority_value,
        )
        if self.method_buy == "top":
            today = get_first_n(
                rank_scores_with_instrument_tiebreak(buy_score[~buy_score.index.isin(last)]),
                self.n_drop + self.topk - len(last),
            )
        elif self.method_buy == "random":
            topk_candi = get_first_n(rank_scores_with_instrument_tiebreak(buy_score), self.topk)
            candi = list(filter(lambda x: x not in last, topk_candi))
            n = self.n_drop + self.topk - len(last)
            try:
                today = np.random.choice(candi, n, replace=False)
            except ValueError:
                today = candi
        else:
            raise NotImplementedError("This type of input is not supported")

        comb_scores = buy_score.reindex(last.union(pd.Index(today))).fillna(self.missing_score_sell_priority_value)
        comb = rank_scores_with_instrument_tiebreak(comb_scores)

        if self.method_sell == "bottom":
            sell = last[last.isin(get_last_n(comb, self.n_drop))]
        elif self.method_sell == "random":
            candi = filter_stock(last)
            try:
                sell = pd.Index(np.random.choice(candi, self.n_drop, replace=False) if len(last) else [])
            except ValueError:
                sell = candi
        else:
            raise NotImplementedError("This type of input is not supported")

        buy = today[: len(sell) + self.topk - len(last)]
        for code in current_stock_list:
            if not self.trade_exchange.is_stock_tradable(
                stock_id=code,
                start_time=trade_start_time,
                end_time=trade_end_time,
                direction=None if self.forbid_all_trade_at_limit else OrderDir.SELL,
            ):
                continue
            if code in sell:
                time_per_step = self.trade_calendar.get_freq()
                if current_temp.get_stock_count(code, bar=time_per_step) < self.hold_thresh:
                    continue
                sell_amount = current_temp.get_stock_amount(code=code)
                sell_order = Order(
                    stock_id=code,
                    amount=sell_amount,
                    start_time=trade_start_time,
                    end_time=trade_end_time,
                    direction=Order.SELL,
                )
                if self.trade_exchange.check_order(sell_order):
                    sell_order_list.append(sell_order)
                    trade_val, trade_cost, _trade_price = self.trade_exchange.deal_order(
                        sell_order, position=current_temp
                    )
                    cash += trade_val - trade_cost

        value = cash * self.risk_degree / len(buy) if len(buy) > 0 else 0
        for code in buy:
            if not self.trade_exchange.is_stock_tradable(
                stock_id=code,
                start_time=trade_start_time,
                end_time=trade_end_time,
                direction=None if self.forbid_all_trade_at_limit else OrderDir.BUY,
            ):
                continue
            buy_price = self.trade_exchange.get_deal_price(
                stock_id=code, start_time=trade_start_time, end_time=trade_end_time, direction=OrderDir.BUY
            )
            buy_amount = value / buy_price
            factor = self.trade_exchange.get_factor(stock_id=code, start_time=trade_start_time, end_time=trade_end_time)
            buy_amount = self.trade_exchange.round_amount_by_trade_unit(buy_amount, factor)
            buy_order = Order(
                stock_id=code,
                amount=buy_amount,
                start_time=trade_start_time,
                end_time=trade_end_time,
                direction=Order.BUY,
            )
            buy_order_list.append(buy_order)
        return TradeDecisionWO(sell_order_list + buy_order_list, self)
