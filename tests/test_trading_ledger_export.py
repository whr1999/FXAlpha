from __future__ import annotations

import io
import zipfile
from pathlib import Path

from services.trading_ledger_export_service import build_trading_ledger_xlsx
from storage.trading_registry import TradingRegistry


def test_trading_ledger_export_is_real_xlsx_with_complete_account_sheets(tmp_path: Path):
    registry = TradingRegistry(tmp_path / "trading.db")
    registry.upsert_account(
        {
            "account_id": "paper-active50",
            "display_name": "ACTIVE50 账户",
            "initial_capital": 1_000_000,
            "status": "active",
        }
    )
    trades_file = tmp_path / "trades.csv"
    trades_file.write_text(
        "instrument,action,filled_amount,price,trade_value,cost,status\n"
        "301526.SZ,buy,100,38.69,3869,5.00,filled\n",
        encoding="utf-8",
    )
    registry.record_account_snapshot(
        {
            "account_id": "paper-active50",
            "trade_date": "2026-08-06",
            "cash": 600_000,
            "stock_value": 400_000,
            "account_value": 1_000_000,
            "positions": {},
        }
    )
    registry.record_account_snapshot(
        {
            "account_id": "paper-active50",
            "trade_date": "2026-08-07",
            "cash": 596_126,
            "stock_value": 423_874,
            "account_value": 1_020_000,
            "positions": {"301526.SZ": {"amount": 100, "price": 38.69, "market_value": 3869, "count_day": 1}},
            "output_files": {"trades_file": str(trades_file)},
        }
    )

    filename, body = build_trading_ledger_xlsx(
        account_id="paper-active50",
        trade_date="2026-08-07",
        registry=registry,
    )

    assert filename == "fxalpha-ledger-paper-active50-2026-08-07.xlsx"
    assert body.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(body)) as workbook:
        names = set(workbook.namelist())
        assert "xl/workbook.xml" in names
        assert "xl/styles.xml" in names
        assert {f"xl/worksheets/sheet{index}.xml" for index in range(1, 5)} <= names
        workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
        assert all(name in workbook_xml for name in ("账户信息", "日结账本", "全部成交", "所选日持仓"))
        ledger_xml = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
        trades_xml = workbook.read("xl/worksheets/sheet3.xml").decode("utf-8")
        positions_xml = workbook.read("xl/worksheets/sheet4.xml").decode("utf-8")
        assert "2026-08-06" in ledger_xml and "2026-08-07" in ledger_xml
        assert "301526.SZ" in trades_xml and "3869" in trades_xml
        assert "301526.SZ" in positions_xml and "count_day" not in positions_xml
