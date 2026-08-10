"""Regression checks for the single production Top20/Drop2 entry contract."""

from __future__ import annotations

import inspect
import sys

from storage.paths import MODEL_DEFAULT_TOPK


def test_cli_trading_commands_default_to_configured_topk(monkeypatch):
    import cli

    commands = {
        "daily-ops-routine": "cmd_daily_ops_routine",
        "target-build": "cmd_target_build",
        "trade-paper": "cmd_trade_paper",
        "trade-daily-preflight": "cmd_trade_daily_preflight",
        "trade-recommend": "cmd_trade_recommend",
        "trade-daily-routine": "cmd_trade_daily_routine",
    }

    for command, handler_name in commands.items():
        captured = {}
        monkeypatch.setattr(cli, handler_name, lambda args: captured.update(topk=args.topk))
        monkeypatch.setattr(sys, "argv", ["cli.py", command])
        cli.main()
        assert captured["topk"] == MODEL_DEFAULT_TOPK


def test_production_service_defaults_follow_configured_topk():
    from domain.trading.execution.qlib_paper import run_qlib_paper_execution
    from domain.trading.recommendation import build_recommendation
    from domain.trading.signals import build_target_portfolio
    from services.daily_ops_service import daily_ops_routine
    from services.prediction_service import target_build
    from services.trading_service import (
        paper_trade_run,
        trading_daily_preflight,
        trading_daily_routine,
        trading_recommend,
    )

    functions = (
        run_qlib_paper_execution,
        build_recommendation,
        build_target_portfolio,
        daily_ops_routine,
        target_build,
        paper_trade_run,
        trading_daily_preflight,
        trading_daily_routine,
        trading_recommend,
    )
    for function in functions:
        assert inspect.signature(function).parameters["topk"].default == MODEL_DEFAULT_TOPK
