from pathlib import Path

import pandas as pd
import pytest

qlib_paper = pytest.importorskip("domain.trading.execution.qlib_paper")
from domain.trading.execution.base import ExecutionInput


class FakeExchange:
    def __init__(self, *args, **kwargs):
        self.prices = {"SH600000": 10.0, "SZ000001": 20.0, "SH600010": 5.0}

    def is_stock_tradable(self, *args, **kwargs):
        return True

    def get_deal_price(self, stock_id, *args, **kwargs):
        return self.prices[stock_id]

    def get_factor(self, *args, **kwargs):
        return None

    def round_amount_by_trade_unit(self, amount, *args, **kwargs):
        return int(float(amount) // 100) * 100

    def check_order(self, order):
        return True

    def deal_order(self, order, position=None, **kwargs):
        price = self.get_deal_price(order.stock_id)
        order.deal_amount = self.round_amount_by_trade_unit(order.amount)
        trade_val = order.deal_amount * price
        cost = 0.0
        if position is not None and trade_val > 0:
            position.update_order(order, trade_val, cost, price)
        return trade_val, cost, price


class FakeD:
    @staticmethod
    def features(instruments, fields, start_time=None, end_time=None, freq="day", disk_cache=False):
        rows = []
        for instrument in instruments:
            rows.append({"datetime": pd.Timestamp(start_time), "instrument": instrument, "$close": {"SH600000": 11.0, "SZ000001": 21.0, "SH600010": 6.0}.get(instrument, 1.0)})
        return pd.DataFrame(rows).set_index(["datetime", "instrument"])

    @staticmethod
    def calendar(freq="day"):
        return pd.to_datetime(["2026-05-20", "2026-05-21", "2026-05-22"])


def test_topk_dropout_plan_sells_bottom_and_buys_replacement():
    scores = pd.Series({"SH600000": 0.2, "SZ000001": 0.9, "SH600010": 0.8})

    plan = qlib_paper.build_topk_dropout_plan(
        pred_score=scores,
        current_stock_list=["SH600000", "SZ000001"],
        topk=2,
        n_drop=1,
    )

    assert plan.sell == ["SH600000"]
    assert plan.buy == ["SH600010"]


def test_topk_dropout_plan_is_stable_when_scores_tie():
    scores_a = pd.Series([0.5, 0.5, 0.5], index=["SZ000003", "SZ000001", "SZ000002"])
    scores_b = scores_a.reindex(["SZ000002", "SZ000003", "SZ000001"])

    plan_a = qlib_paper.build_topk_dropout_plan(pred_score=scores_a, current_stock_list=[], topk=2, n_drop=0)
    plan_b = qlib_paper.build_topk_dropout_plan(pred_score=scores_b, current_stock_list=[], topk=2, n_drop=0)

    assert plan_a.buy == ["SZ000001", "SZ000002"]
    assert plan_b.buy == plan_a.buy


def test_qlib_paper_execution_writes_account_state_and_fills(monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_paper, "PAPER_TRADING_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(qlib_paper, "Exchange", FakeExchange)
    monkeypatch.setattr(qlib_paper, "D", FakeD)
    monkeypatch.setattr(qlib_paper, "init_qlib", lambda: None)
    score_file = tmp_path / "score.csv"
    target_file = tmp_path / "target.csv"
    pd.DataFrame(
        [
            {"instrument": "SH600000", "score": 0.2},
            {"instrument": "SZ000001", "score": 0.9},
        ]
    ).to_csv(score_file, index=False)
    pd.DataFrame([{"instrument": "SZ000001", "target_weight": 1.0}]).to_csv(target_file, index=False)

    result = qlib_paper.run_qlib_paper_execution(
        ExecutionInput(
            version_id="run1",
            trade_date="2026-05-20",
            score_file=score_file,
            target_file=target_file,
            initial_capital=100000.0,
            extra={"recommendation_id": "rec-1"},
        ),
        topk=1,
        n_drop=1,
        hold_thresh=0,
    )

    assert result.ok
    assert result.adapter == "qlib_exchange_paper"
    assert Path(result.output_files["account_state_file"]).exists()
    assert Path(result.output_files["event_log_file"]).exists()
    fills = pd.read_csv(result.output_files["fills_file"])
    ledger = pd.read_csv(result.output_files["ledger_file"])
    assert fills.iloc[0]["action"] == "buy"
    assert fills.iloc[0]["filled_amount"] > 0
    assert ledger.iloc[0]["buy_value"] > 0
    assert ledger.iloc[0]["turnover"] == pytest.approx(ledger.iloc[0]["buy_value"])
    assert "trading_cost" in ledger.columns
    assert Path(result.output_files["staged_account_state_file"]).exists()
    state = qlib_paper._load_account_state("run1", 100000.0)
    assert state["positions"]["SZ000001"]["amount"] > 0
    event = Path(result.output_files["event_log_file"]).read_text(encoding="utf-8")
    assert '"event": "rebalance_execution"' in event


def test_confidence_cash_execution_uses_target_weights_and_preserves_cash(monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_paper, "PAPER_TRADING_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(qlib_paper, "Exchange", FakeExchange)
    monkeypatch.setattr(qlib_paper, "D", FakeD)
    monkeypatch.setattr(qlib_paper, "init_qlib", lambda: None)
    score_file = tmp_path / "score-confidence.csv"
    target_file = tmp_path / "target-confidence.csv"
    pd.DataFrame(
        [
            {"instrument": "SH600000", "score": 0.9},
            {"instrument": "SZ000001", "score": 0.8},
        ]
    ).to_csv(score_file, index=False)
    pd.DataFrame(
        [
            {"instrument": "SH600000", "target_weight": 0.25},
            {"instrument": "SZ000001", "target_weight": 0.25},
        ]
    ).to_csv(target_file, index=False)

    result = qlib_paper.run_qlib_paper_execution(
        ExecutionInput(
            version_id="confidence-account",
            trade_date="2026-05-20",
            score_file=score_file,
            target_file=target_file,
            initial_capital=100000.0,
            extra={
                "recommendation_id": "rec-confidence",
                "strategy_contract_version": "confidence_cash_top20_drop2_hold5_open_v1",
            },
        ),
        topk=20,
        n_drop=2,
        hold_thresh=0,
    )

    assert result.ok
    assert result.metrics["execution_mode"] == "target_weight_v2"
    assert result.metrics["target_stock_exposure"] == pytest.approx(0.5)
    state = qlib_paper._load_account_state("confidence-account", 100000.0)
    assert state["cash"] > 45000
    assert state["target_cash_weight"] == pytest.approx(0.5)
    assert state["actual_cash_weight"] > 0.45
    assert set(state["positions"]) == {"SH600000", "SZ000001"}


def test_target_weight_rebalance_partially_sells_excess_position(monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_paper, "PAPER_TRADING_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(qlib_paper, "Exchange", FakeExchange)
    monkeypatch.setattr(qlib_paper, "D", FakeD)
    monkeypatch.setattr(qlib_paper, "init_qlib", lambda: None)
    state_dir = tmp_path / "confidence-sell" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "account_state.json").write_text(
        '{"account_id":"confidence-sell","version_id":"confidence-sell","as_of_date":"2026-05-19","cash":0,"initial_capital":10000,"account_value":10000,"stock_value":10000,"positions":{"SH600000":{"amount":1000,"price":10,"count_day":6}}}',
        encoding="utf-8",
    )
    score_file = tmp_path / "score-sell.csv"
    target_file = tmp_path / "target-sell.csv"
    pd.DataFrame([{"instrument": "SH600000", "score": 0.9}]).to_csv(score_file, index=False)
    pd.DataFrame([{"instrument": "SH600000", "target_weight": 0.5}]).to_csv(target_file, index=False)

    result = qlib_paper.run_qlib_paper_execution(
        ExecutionInput(
            version_id="confidence-sell",
            trade_date="2026-05-20",
            score_file=score_file,
            target_file=target_file,
            initial_capital=10000.0,
            extra={
                "recommendation_id": "rec-confidence-sell",
                "strategy_contract_version": "confidence_cash_top20_drop2_hold5_open_v1",
            },
        ),
        topk=20,
        n_drop=2,
        hold_thresh=5,
    )

    fills = pd.read_csv(result.output_files["fills_file"])
    assert result.ok
    assert fills.iloc[0]["action"] == "sell"
    assert fills.iloc[0]["filled_amount"] == pytest.approx(500)
    state = qlib_paper._load_account_state("confidence-sell", 10000.0)
    assert state["positions"]["SH600000"]["amount"] == pytest.approx(500)
    assert state["cash"] == pytest.approx(5000)


def test_enforced_market_risk_cap_uses_target_weights_and_overrides_hold5(monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_paper, "PAPER_TRADING_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(qlib_paper, "Exchange", FakeExchange)
    monkeypatch.setattr(qlib_paper, "D", FakeD)
    monkeypatch.setattr(qlib_paper, "init_qlib", lambda: None)
    state_dir = tmp_path / "risk-overlay" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "account_state.json").write_text(
        '{"account_id":"risk-overlay","version_id":"risk-overlay","as_of_date":"2026-05-19","cash":0,"initial_capital":10000,"account_value":10000,"stock_value":10000,"positions":{"SH600000":{"amount":1000,"price":10,"count_day":1}}}',
        encoding="utf-8",
    )
    score_file = tmp_path / "score-risk.csv"
    target_file = tmp_path / "target-risk.csv"
    pd.DataFrame([{"instrument": "SH600000", "score": 0.9}]).to_csv(score_file, index=False)
    pd.DataFrame([{"instrument": "SH600000", "target_weight": 0.5}]).to_csv(target_file, index=False)

    result = qlib_paper.run_qlib_paper_execution(
        ExecutionInput(
            version_id="risk-overlay",
            trade_date="2026-05-20",
            score_file=score_file,
            target_file=target_file,
            initial_capital=10000.0,
            extra={
                "recommendation_id": "rec-risk-overlay",
                "strategy_contract_version": "top20_drop2_hold5_open_v1",
                "recommendation_metrics": {
                    "risk_policy": {
                        "enabled": True,
                        "enforced": True,
                        "model_cap": 1.0,
                        "final_stock_cap": 0.5,
                    }
                },
            },
        ),
        topk=20,
        n_drop=2,
        hold_thresh=5,
    )

    fills = pd.read_csv(result.output_files["fills_file"])
    assert result.ok
    assert result.metrics["execution_mode"] == "risk_target_weight_v1"
    assert fills.iloc[0]["action"] == "sell"
    assert fills.iloc[0]["filled_amount"] == pytest.approx(500)
    assert result.diagnostics["plan"]["risk_reduction_overrides_hold_thresh"] is True


def test_locked_old_positions_consume_gross_budget_and_block_new_buys(monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_paper, "PAPER_TRADING_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(qlib_paper, "Exchange", FakeExchange)
    monkeypatch.setattr(qlib_paper, "D", FakeD)
    monkeypatch.setattr(qlib_paper, "init_qlib", lambda: None)
    state_dir = tmp_path / "confidence-budget" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "account_state.json").write_text(
        '{"account_id":"confidence-budget","version_id":"confidence-budget","as_of_date":"2026-05-19","cash":10000,"initial_capital":20000,"account_value":20000,"stock_value":10000,"positions":{"SH600000":{"amount":1000,"price":10,"count_day":1}}}',
        encoding="utf-8",
    )
    score_file = tmp_path / "score-budget.csv"
    target_file = tmp_path / "target-budget.csv"
    pd.DataFrame([{"instrument": "SZ000001", "score": 0.9}]).to_csv(score_file, index=False)
    pd.DataFrame([{"instrument": "SZ000001", "target_weight": 0.5}]).to_csv(target_file, index=False)

    result = qlib_paper.run_qlib_paper_execution(
        ExecutionInput(
            version_id="confidence-budget",
            trade_date="2026-05-20",
            score_file=score_file,
            target_file=target_file,
            initial_capital=20000.0,
            extra={
                "recommendation_id": "rec-confidence-budget",
                "strategy_contract_version": "confidence_cash_top20_drop2_hold5_open_v2",
            },
        ),
        topk=20,
        n_drop=2,
        hold_thresh=5,
    )

    assert result.ok
    assert result.metrics["buy_count"] == 0
    assert result.metrics["actual_stock_exposure"] <= 0.55
    reasons = [row["reason"] for row in result.diagnostics["plan"]["constraints"]]
    assert "hold_thresh" in reasons
    assert "gross_exposure_budget_exhausted" in reasons
    state = qlib_paper._load_account_state("confidence-budget", 20000.0)
    assert set(state["positions"]) == {"SH600000"}


def test_qlib_paper_backfill_marks_existing_account_to_market(monkeypatch, tmp_path):
    monkeypatch.setattr(qlib_paper, "PAPER_TRADING_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(qlib_paper, "D", FakeD)
    monkeypatch.setattr(qlib_paper, "init_qlib", lambda: None)
    state_dir = tmp_path / "run1" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "account_state.json").write_text(
        """{"account_id":"run1","version_id":"run1","as_of_date":"2026-05-20","cash":1000,"initial_capital":1000,"account_value":2000,"stock_value":1000,"positions":{"SH600000":{"amount":100,"price":10,"count_day":1}}}""",
        encoding="utf-8",
    )

    results = qlib_paper.backfill_qlib_paper_account(
        version_id="run1",
        target_date="2026-05-22",
        initial_capital=1000.0,
    )

    assert [item.trade_date for item in results] == ["2026-05-21", "2026-05-22"]
    assert all(item.ok for item in results)
    state = qlib_paper._load_account_state("run1", 1000.0)
    assert state["as_of_date"] == "2026-05-22"
    assert state["account_value"] == 2100.0
    assert state["actual_stock_exposure"] == pytest.approx(1100.0 / 2100.0)
    assert state["actual_cash_weight"] == pytest.approx(1000.0 / 2100.0)
    ledger = pd.read_csv(results[-1].output_files["ledger_file"])
    assert ledger.iloc[0]["trading_cost"] == 0
    assert ledger.iloc[0]["turnover"] == 0
    assert "mark_to_market" in (tmp_path / "run1" / "paper_account_events.jsonl").read_text(encoding="utf-8")
