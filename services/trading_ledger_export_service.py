from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from storage.trading_registry import TradingRegistry


_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_text(value: Any) -> str:
    return escape(_INVALID_XML.sub("", str(value if value is not None else "")))


def _column_name(index: int) -> str:
    result = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cell_xml(reference: str, value: Any, style: int = 0) -> str:
    number = _finite_number(value) if not isinstance(value, bool) else None
    style_attr = f' s="{style}"' if style else ""
    if number is not None:
        return f'<c r="{reference}" t="n"{style_attr}><v>{number:.12g}</v></c>'
    return f'<c r="{reference}" t="inlineStr"{style_attr}><is><t>{_xml_text(value)}</t></is></c>'


def _worksheet_xml(
    rows: list[list[Any]],
    *,
    widths: list[float],
    percentage_columns: set[int] | None = None,
    tone_columns: set[int] | None = None,
) -> str:
    percentage_columns = percentage_columns or set()
    tone_columns = tone_columns or set()
    rendered_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(row):
            style = 1 if row_index == 1 else (3 if column_index in percentage_columns else 2 if _finite_number(value) is not None else 0)
            number = _finite_number(value)
            if row_index > 1 and column_index in tone_columns and number is not None:
                style = 6 if column_index in percentage_columns and number > 0 else 7 if column_index in percentage_columns and number < 0 else 4 if number > 0 else 5 if number < 0 else style
            cells.append(_cell_xml(f"{_column_name(column_index)}{row_index}", value, style))
        rendered_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    max_column = _column_name(max((len(row) for row in rows), default=1) - 1)
    max_row = max(len(rows), 1)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f'<cols>{columns}</cols><sheetData>{"".join(rendered_rows)}</sheetData>'
        f'<autoFilter ref="A1:{max_column}{max_row}"/>'
        '</worksheet>'
    )


def _styles_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="4">
    <font><sz val="11"/><name val="Microsoft YaHei"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Microsoft YaHei"/></font>
    <font><color rgb="FFFCA5A5"/><sz val="11"/><name val="Microsoft YaHei"/></font>
    <font><color rgb="FF22C55E"/><sz val="11"/><name val="Microsoft YaHei"/></font>
  </fonts>
  <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF172554"/><bgColor indexed="64"/></patternFill></fill></fills>
  <borders count="2"><border/><border><bottom style="thin"><color rgb="FF334155"/></bottom></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="8">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFill="1" applyFont="1"/>
    <xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="4" fontId="2" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>
    <xf numFmtId="4" fontId="3" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>
    <xf numFmtId="10" fontId="2" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>
    <xf numFmtId="10" fontId="3" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def _trade_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    path_value = str((snapshot.get("output_files") or {}).get("trades_file") or "")
    path = Path(path_value) if path_value else None
    if not path or not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error, UnicodeError):
        return []


def build_trading_ledger_xlsx(
    *,
    account_id: str,
    trade_date: str | None = None,
    registry: TradingRegistry | None = None,
) -> tuple[str, bytes]:
    registry = registry or TradingRegistry()
    account = registry.get_account(account_id)
    if not account:
        raise ValueError("paper_account_not_found")
    snapshots = registry.list_account_snapshots(account_id, limit=520)
    if not snapshots:
        raise ValueError("paper_account_ledger_empty")
    selected_date = str(trade_date or snapshots[-1].get("trade_date") or "")[:10]
    selected = next((row for row in snapshots if str(row.get("trade_date") or "")[:10] == selected_date), None)
    if not selected:
        raise ValueError("paper_account_trade_date_not_found")

    initial_capital = float(account.get("initial_capital") or 0)
    all_trades: list[dict[str, Any]] = []
    ledger_rows: list[list[Any]] = [[
        "交易日", "期初净值", "期末净值", "现金余额", "股票市值", "当日盈亏", "当日收益率",
        "累计收益额", "累计收益率", "持仓数", "成交笔数", "交易成本",
    ]]
    previous_value: float | None = None
    for snapshot in snapshots:
        account_value = float(snapshot.get("account_value") or 0)
        daily_pnl = float(snapshot.get("daily_pnl") or 0)
        opening_value = previous_value if previous_value is not None else account_value - daily_pnl
        trades = _trade_rows(snapshot)
        trade_cost = sum(float(item.get("cost") or 0) for item in trades)
        trade_date_value = str(snapshot.get("trade_date") or "")[:10]
        for item in trades:
            all_trades.append({"trade_date": trade_date_value, **item})
        cumulative_pnl = account_value - initial_capital
        cumulative_return = cumulative_pnl / initial_capital if initial_capital else 0.0
        ledger_rows.append([
            trade_date_value, opening_value, account_value, snapshot.get("cash"), snapshot.get("stock_value"),
            daily_pnl, snapshot.get("daily_return"), cumulative_pnl, cumulative_return,
            len(snapshot.get("positions") or {}), len(trades), trade_cost,
        ])
        previous_value = account_value

    trade_sheet_rows: list[list[Any]] = [["交易日", "股票代码", "股票名称", "操作", "成交数量", "成交价格", "成交金额", "交易成本", "状态"]]
    for row in all_trades:
        trade_sheet_rows.append([
            row.get("trade_date"), row.get("instrument") or row.get("symbol"), row.get("security_name") or "",
            row.get("action") or row.get("side"), row.get("filled_amount") or row.get("amount"), row.get("price"),
            row.get("trade_value"), row.get("cost"), row.get("status"),
        ])

    position_rows: list[list[Any]] = [["交易日", "股票代码", "持仓数量", "最新价格", "市值", "持有天数"]]
    for instrument, position in sorted((selected.get("positions") or {}).items()):
        position_rows.append([
            selected_date, instrument, position.get("shares") or position.get("amount"), position.get("price"),
            position.get("market_value"), position.get("count_day"),
        ])

    info_rows = [
        ["字段", "值"],
        ["账户ID", account_id],
        ["账户名称", account.get("display_name") or ""],
        ["账户状态", account.get("status") or ""],
        ["初始资金", initial_capital],
        ["账户生效日", account.get("created_at") or ""],
        ["导出查询日", selected_date],
        ["账本起止", f'{snapshots[0].get("trade_date")} — {snapshots[-1].get("trade_date")}'],
        ["账本日数量", len(snapshots)],
    ]
    sheets = [
        ("账户信息", _worksheet_xml(info_rows, widths=[20, 48])),
        ("日结账本", _worksheet_xml(ledger_rows, widths=[14, 16, 16, 16, 16, 16, 14, 16, 14, 10, 10, 14], percentage_columns={6, 8}, tone_columns={5, 6, 7, 8})),
        ("全部成交", _worksheet_xml(trade_sheet_rows, widths=[14, 16, 18, 12, 14, 14, 16, 14, 12])),
        ("所选日持仓", _worksheet_xml(position_rows, widths=[14, 16, 14, 14, 16, 12])),
    ]

    workbook_sheets = "".join(
        f'<sheet name="{_xml_text(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _) in enumerate(sheets, start=1)
    )
    workbook_rels = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{sheet_overrides}</Types>''')
        archive.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        archive.writestr("xl/workbook.xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{workbook_sheets}</sheets></workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{workbook_rels}<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''')
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, (_, worksheet) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", worksheet)
    safe_account_id = re.sub(r"[^A-Za-z0-9._-]+", "-", account_id).strip("-") or "paper-account"
    return f"fxalpha-ledger-{safe_account_id}-{selected_date}.xlsx", output.getvalue()
