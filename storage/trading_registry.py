"""Trading recommendation and paper execution registry."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from storage.paths import MODEL_DEFAULT_TOPK, TRADING_EXECUTION_LOG_DB


DB_PATH = TRADING_EXECUTION_LOG_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS recommendation_batches (
    recommendation_id      TEXT PRIMARY KEY,
    account_id             TEXT NOT NULL DEFAULT '',
    model_id               TEXT NOT NULL DEFAULT '',
    model_run_id           TEXT NOT NULL DEFAULT '',
    signal_date            TEXT NOT NULL,
    execution_date         TEXT DEFAULT '',
    status                 TEXT NOT NULL DEFAULT 'pending'
                           CHECK(status IN ('pending','executed','failed','superseded')),
    topk                   INTEGER DEFAULT 20,
    n_drop                 INTEGER DEFAULT 2,
    hold_thresh            INTEGER DEFAULT 5,
    deal_price             TEXT DEFAULT 'open',
    strategy_contract_version TEXT DEFAULT '',
    run_kind               TEXT DEFAULT 'on_time',
    data_package_id        TEXT DEFAULT '',
    total_capital          REAL DEFAULT 0,
    score_file             TEXT DEFAULT '',
    target_file            TEXT DEFAULT '',
    decision_file          TEXT DEFAULT '',
    order_preview_file     TEXT DEFAULT '',
    recommendation_file    TEXT DEFAULT '',
    execution_files        TEXT DEFAULT '{}',
    metrics                TEXT DEFAULT '{}',
    warnings               TEXT DEFAULT '[]',
    error                  TEXT DEFAULT '',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendation_orders (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id      TEXT NOT NULL REFERENCES recommendation_batches(recommendation_id),
    signal_date            TEXT NOT NULL,
    execution_date         TEXT DEFAULT '',
    instrument             TEXT NOT NULL,
    action                 TEXT DEFAULT '',
    current_shares         INTEGER DEFAULT 0,
    target_shares          INTEGER DEFAULT 0,
    delta_shares           INTEGER DEFAULT 0,
    target_weight          REAL DEFAULT 0,
    score                  REAL,
    target_value           REAL,
    estimated_price        REAL,
    estimated_notional     REAL
);

CREATE TABLE IF NOT EXISTS paper_executions (
    execution_id           TEXT PRIMARY KEY,
    account_id             TEXT NOT NULL DEFAULT '',
    recommendation_id      TEXT NOT NULL REFERENCES recommendation_batches(recommendation_id),
    model_id               TEXT NOT NULL DEFAULT '',
    model_run_id           TEXT NOT NULL DEFAULT '',
    trade_date             TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'completed',
    adapter                TEXT DEFAULT '',
    output_files           TEXT DEFAULT '{}',
    metrics                TEXT DEFAULT '{}',
    diagnostics            TEXT DEFAULT '{}',
    notes                  TEXT DEFAULT '[]',
    error                  TEXT DEFAULT '',
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_account_snapshots (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id             TEXT NOT NULL DEFAULT '',
    model_run_id           TEXT NOT NULL DEFAULT '',
    trade_date             TEXT NOT NULL,
    source_recommendation_id TEXT DEFAULT '',
    cash                   REAL DEFAULT 0,
    stock_value            REAL DEFAULT 0,
    account_value          REAL DEFAULT 0,
    positions_json         TEXT DEFAULT '{}',
    score_hash             TEXT DEFAULT '',
    target_hash            TEXT DEFAULT '',
    fills_hash             TEXT DEFAULT '',
    output_files           TEXT DEFAULT '{}',
    risk_metrics           TEXT DEFAULT '{}',
    created_at             TEXT NOT NULL,
    UNIQUE(account_id, trade_date)
);

CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id             TEXT PRIMARY KEY,
    display_name           TEXT NOT NULL DEFAULT '',
    account_mode           TEXT NOT NULL DEFAULT 'fixed_model'
                           CHECK(account_mode IN ('fixed_model','rolling_champion')),
    initial_capital        REAL NOT NULL DEFAULT 1000000,
    strategy_contract_version TEXT NOT NULL DEFAULT 'top20_drop2_hold5_open_v1',
    topk                   INTEGER NOT NULL DEFAULT 20,
    n_drop                 INTEGER NOT NULL DEFAULT 2,
    hold_thresh            INTEGER NOT NULL DEFAULT 5,
    deal_price             TEXT NOT NULL DEFAULT 'open',
    status                 TEXT NOT NULL DEFAULT 'active'
                           CHECK(status IN ('active','paused','retired')),
    metadata               TEXT NOT NULL DEFAULT '{}',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    retired_at             TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS paper_account_model_deployments (
    deployment_id          TEXT PRIMARY KEY,
    account_id             TEXT NOT NULL REFERENCES paper_accounts(account_id),
    model_id               TEXT NOT NULL,
    model_run_id           TEXT NOT NULL,
    feature_set_id         TEXT NOT NULL DEFAULT '',
    effective_from         TEXT NOT NULL,
    effective_to           TEXT NOT NULL DEFAULT '',
    deployment_mode        TEXT NOT NULL DEFAULT 'fixed_model',
    status                 TEXT NOT NULL DEFAULT 'active'
                           CHECK(status IN ('pending','active','retired')),
    evidence               TEXT NOT NULL DEFAULT '{}',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_fleet_runs (
    fleet_run_id           TEXT PRIMARY KEY,
    target_date            TEXT NOT NULL,
    data_package_id        TEXT NOT NULL DEFAULT '',
    data_latest_date       TEXT NOT NULL DEFAULT '',
    status                 TEXT NOT NULL,
    current_stage          TEXT NOT NULL DEFAULT '',
    account_count          INTEGER NOT NULL DEFAULT 0,
    completed_count        INTEGER NOT NULL DEFAULT 0,
    failed_count           INTEGER NOT NULL DEFAULT 0,
    inputs                 TEXT NOT NULL DEFAULT '{}',
    outputs                TEXT NOT NULL DEFAULT '{}',
    error                  TEXT NOT NULL DEFAULT '',
    started_at             TEXT NOT NULL,
    completed_at           TEXT NOT NULL DEFAULT '',
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_account_runs (
    account_run_id         TEXT PRIMARY KEY,
    fleet_run_id           TEXT NOT NULL DEFAULT '',
    account_id             TEXT NOT NULL REFERENCES paper_accounts(account_id),
    signal_date            TEXT NOT NULL,
    model_id               TEXT NOT NULL DEFAULT '',
    model_run_id           TEXT NOT NULL DEFAULT '',
    strategy_contract_version TEXT NOT NULL DEFAULT '',
    config_hash            TEXT NOT NULL DEFAULT '',
    run_kind               TEXT NOT NULL DEFAULT 'on_time'
                           CHECK(run_kind IN ('on_time','catch_up_replay','manual')),
    status                 TEXT NOT NULL,
    current_stage          TEXT NOT NULL DEFAULT '',
    attempt                INTEGER NOT NULL DEFAULT 1,
    inputs                 TEXT NOT NULL DEFAULT '{}',
    outputs                TEXT NOT NULL DEFAULT '{}',
    error                  TEXT NOT NULL DEFAULT '',
    started_at             TEXT NOT NULL,
    completed_at           TEXT NOT NULL DEFAULT '',
    updated_at             TEXT NOT NULL,
    UNIQUE(account_id, signal_date, config_hash)
);

CREATE TABLE IF NOT EXISTS paper_run_events (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    fleet_run_id           TEXT NOT NULL DEFAULT '',
    account_run_id         TEXT NOT NULL DEFAULT '',
    account_id             TEXT NOT NULL DEFAULT '',
    stage                  TEXT NOT NULL,
    status                 TEXT NOT NULL,
    payload                TEXT NOT NULL DEFAULT '{}',
    created_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rec_status ON recommendation_batches(status);
CREATE INDEX IF NOT EXISTS idx_rec_model_run ON recommendation_batches(model_run_id);
CREATE INDEX IF NOT EXISTS idx_rec_signal_date ON recommendation_batches(signal_date);
CREATE INDEX IF NOT EXISTS idx_rec_execution_date ON recommendation_batches(execution_date);
CREATE INDEX IF NOT EXISTS idx_orders_rec ON recommendation_orders(recommendation_id);
CREATE INDEX IF NOT EXISTS idx_exec_rec ON paper_executions(recommendation_id);
CREATE INDEX IF NOT EXISTS idx_snap_model_run ON paper_account_snapshots(model_run_id);
CREATE INDEX IF NOT EXISTS idx_snap_trade_date ON paper_account_snapshots(trade_date);
CREATE INDEX IF NOT EXISTS idx_deploy_account_dates ON paper_account_model_deployments(account_id, effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_account_run_account_date ON paper_account_runs(account_id, signal_date);
CREATE INDEX IF NOT EXISTS idx_run_events_account_run ON paper_run_events(account_run_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply additive migrations for pre multi-account trading databases."""
    if "account_id" not in _columns(conn, "recommendation_batches"):
        conn.execute("ALTER TABLE recommendation_batches ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
    if "account_id" not in _columns(conn, "paper_executions"):
        conn.execute("ALTER TABLE paper_executions ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
    recommendation_additions = {
        "n_drop": "INTEGER DEFAULT 2",
        "hold_thresh": "INTEGER DEFAULT 5",
        "deal_price": "TEXT DEFAULT 'open'",
        "strategy_contract_version": "TEXT DEFAULT ''",
        "run_kind": "TEXT DEFAULT 'on_time'",
        "data_package_id": "TEXT DEFAULT ''",
    }
    recommendation_columns = _columns(conn, "recommendation_batches")
    for column, definition in recommendation_additions.items():
        if column not in recommendation_columns:
            conn.execute(f"ALTER TABLE recommendation_batches ADD COLUMN {column} {definition}")
    if "risk_metrics" not in _columns(conn, "paper_account_snapshots"):
        conn.execute("ALTER TABLE paper_account_snapshots ADD COLUMN risk_metrics TEXT DEFAULT '{}'")
    conn.execute("UPDATE recommendation_batches SET account_id=model_run_id WHERE account_id='' AND model_run_id<>''")
    conn.execute("UPDATE paper_executions SET account_id=model_run_id WHERE account_id='' AND model_run_id<>''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_account ON recommendation_batches(account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exec_account ON paper_executions(account_id)")
    conn.commit()


def _normalize_row(row: sqlite3.Row | dict | None) -> dict[str, Any] | None:
    if not row:
        return None
    out = dict(row)
    out["execution_files"] = _json_load(out.get("execution_files"), {})
    out["metrics"] = _json_load(out.get("metrics"), {})
    out["warnings"] = _json_load(out.get("warnings"), [])
    return out


def _normalize_account_snapshot(row: sqlite3.Row | dict | None) -> dict[str, Any] | None:
    if not row:
        return None
    out = dict(row)
    out["positions"] = _json_load(out.pop("positions_json", "{}"), {})
    out["output_files"] = _json_load(out.get("output_files"), {})
    out["risk_metrics"] = _json_load(out.get("risk_metrics"), {})
    return out


def _attach_account_history_returns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous_value: float | None = None
    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        current_value = float(item.get("account_value") or 0)
        if previous_value is None:
            item["daily_pnl"] = 0.0
            item["daily_return"] = 0.0
        else:
            item["daily_pnl"] = current_value - previous_value
            item["daily_return"] = (current_value - previous_value) / previous_value if previous_value else 0.0
        previous_value = current_value
        enriched.append(item)
    return enriched


def _insert_execution(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO paper_executions
           (execution_id, account_id, recommendation_id, model_id, model_run_id, trade_date, status,
            adapter, output_files, metrics, diagnostics, notes, error, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(payload["execution_id"]),
            payload.get("account_id", payload.get("model_run_id", "")),
            payload.get("recommendation_id", ""),
            payload.get("model_id", ""),
            payload.get("model_run_id", ""),
            payload.get("trade_date", ""),
            payload.get("status", "completed"),
            payload.get("adapter", ""),
            _json_dump(payload.get("output_files", {})),
            _json_dump(payload.get("metrics", {})),
            _json_dump(payload.get("diagnostics", {})),
            _json_dump(payload.get("notes", [])),
            payload.get("error", ""),
            payload.get("created_at") or _now(),
        ),
    )


def _upsert_snapshot(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO paper_account_snapshots
           (account_id, model_run_id, trade_date, source_recommendation_id,
            cash, stock_value, account_value, positions_json,
            score_hash, target_hash, fills_hash, output_files, risk_metrics, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(account_id, trade_date) DO UPDATE SET
            model_run_id=excluded.model_run_id,
            source_recommendation_id=excluded.source_recommendation_id,
            cash=excluded.cash,
            stock_value=excluded.stock_value,
            account_value=excluded.account_value,
            positions_json=excluded.positions_json,
            score_hash=excluded.score_hash,
            target_hash=excluded.target_hash,
            fills_hash=excluded.fills_hash,
            output_files=excluded.output_files,
            risk_metrics=excluded.risk_metrics,
            created_at=excluded.created_at""",
        (
            payload.get("account_id", ""),
            payload.get("model_run_id", payload.get("version_id", "")),
            payload.get("trade_date", payload.get("as_of_date", "")),
            payload.get("source_recommendation_id", ""),
            float(payload.get("cash") or 0),
            float(payload.get("stock_value") or 0),
            float(payload.get("account_value") or 0),
            _json_dump(payload.get("positions") or payload.get("positions_json") or {}),
            payload.get("score_hash", ""),
            payload.get("target_hash", ""),
            payload.get("fills_hash", ""),
            _json_dump(payload.get("output_files") or {}),
            _json_dump(
                payload.get("risk_metrics")
                or {
                    "execution_mode": payload.get("execution_mode", ""),
                    "target_stock_exposure": payload.get("target_stock_exposure"),
                    "target_cash_weight": payload.get("target_cash_weight"),
                    "actual_stock_exposure": payload.get("actual_stock_exposure"),
                    "actual_cash_weight": payload.get("actual_cash_weight"),
                    "exposure_gap": payload.get("exposure_gap"),
                    "execution_constraints": payload.get("execution_constraints") or [],
                }
            ),
            payload.get("created_at") or _now(),
        ),
    )


class TradingRegistry:
    """Small SQLite registry for recommendation batches and paper executions."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH

    def upsert_recommendation(self, payload: dict[str, Any], orders: list[dict[str, Any]] | None = None) -> str:
        recommendation_id = str(payload["recommendation_id"])
        account_id = str(payload.get("account_id") or payload.get("model_run_id") or "")
        now = _now()
        conn = _connect(self.db_path)
        conn.execute(
            """INSERT INTO recommendation_batches
               (recommendation_id, account_id, model_id, model_run_id, signal_date, execution_date, status,
                topk, n_drop, hold_thresh, deal_price, strategy_contract_version, run_kind, data_package_id,
                total_capital, score_file, target_file, decision_file,
                order_preview_file, recommendation_file, execution_files,
                metrics, warnings, error, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(recommendation_id) DO UPDATE SET
                account_id=excluded.account_id,
                model_id=excluded.model_id,
                model_run_id=excluded.model_run_id,
                signal_date=excluded.signal_date,
                execution_date=excluded.execution_date,
                status=excluded.status,
                topk=excluded.topk,
                n_drop=excluded.n_drop,
                hold_thresh=excluded.hold_thresh,
                deal_price=excluded.deal_price,
                strategy_contract_version=excluded.strategy_contract_version,
                run_kind=excluded.run_kind,
                data_package_id=excluded.data_package_id,
                total_capital=excluded.total_capital,
                score_file=excluded.score_file,
                target_file=excluded.target_file,
                decision_file=excluded.decision_file,
                order_preview_file=excluded.order_preview_file,
                recommendation_file=excluded.recommendation_file,
                execution_files=excluded.execution_files,
                metrics=excluded.metrics,
                warnings=excluded.warnings,
                error=excluded.error,
                updated_at=excluded.updated_at""",
            (
                recommendation_id,
                account_id,
                payload.get("model_id", ""),
                payload.get("model_run_id", ""),
                payload.get("signal_date", ""),
                payload.get("execution_date") or "",
                payload.get("status", "pending"),
                int(payload.get("topk") or MODEL_DEFAULT_TOPK),
                int(payload.get("n_drop") if payload.get("n_drop") is not None else 2),
                int(payload.get("hold_thresh") if payload.get("hold_thresh") is not None else 5),
                payload.get("deal_price", "open"),
                payload.get("strategy_contract_version", ""),
                payload.get("run_kind", "on_time"),
                payload.get("data_package_id", ""),
                float(payload.get("total_capital") or 0),
                payload.get("score_file", ""),
                payload.get("target_file", ""),
                payload.get("decision_file", ""),
                payload.get("order_preview_file", ""),
                payload.get("recommendation_file", ""),
                _json_dump(payload.get("execution_files", {})),
                _json_dump(payload.get("metrics", {})),
                _json_dump(payload.get("warnings", [])),
                payload.get("error", ""),
                payload.get("created_at") or now,
                now,
            ),
        )
        conn.execute("DELETE FROM recommendation_orders WHERE recommendation_id=?", (recommendation_id,))
        for order in orders or []:
            conn.execute(
                """INSERT INTO recommendation_orders
                   (recommendation_id, signal_date, execution_date, instrument, action,
                    current_shares, target_shares, delta_shares, target_weight, score,
                    target_value, estimated_price, estimated_notional)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    recommendation_id,
                    payload.get("signal_date", ""),
                    payload.get("execution_date") or "",
                    order.get("instrument", ""),
                    order.get("action", ""),
                    int(order.get("current_shares") or 0),
                    int(order.get("target_shares") or 0),
                    int(order.get("delta_shares") or 0),
                    float(order.get("target_weight") or 0),
                    order.get("score"),
                    order.get("target_value"),
                    order.get("estimated_price"),
                    order.get("estimated_notional"),
                ),
            )
        conn.commit()
        conn.close()
        return recommendation_id

    def mark_recommendation(self, recommendation_id: str, *, status: str, error: str = "", metrics: dict | None = None, execution_files: dict | None = None) -> None:
        conn = _connect(self.db_path)
        row = conn.execute(
            "SELECT metrics, execution_files FROM recommendation_batches WHERE recommendation_id=?",
            (recommendation_id,),
        ).fetchone()
        old_metrics = _json_load(row["metrics"], {}) if row else {}
        old_files = _json_load(row["execution_files"], {}) if row else {}
        if metrics:
            old_metrics.update(metrics)
        if execution_files:
            old_files.update(execution_files)
        conn.execute(
            """UPDATE recommendation_batches
               SET status=?, error=?, metrics=?, execution_files=?, updated_at=?
               WHERE recommendation_id=?""",
            (status, error, _json_dump(old_metrics), _json_dump(old_files), _now(), recommendation_id),
        )
        conn.commit()
        conn.close()

    def set_execution_date(self, recommendation_id: str, execution_date: str) -> None:
        conn = _connect(self.db_path)
        conn.execute(
            """UPDATE recommendation_batches
               SET execution_date=?, updated_at=?
               WHERE recommendation_id=?""",
            (execution_date, _now(), recommendation_id),
        )
        conn.execute(
            "UPDATE recommendation_orders SET execution_date=? WHERE recommendation_id=?",
            (execution_date, recommendation_id),
        )
        conn.commit()
        conn.close()

    def supersede_pending_except(
        self,
        *,
        model_run_id: str,
        account_id: str | None = None,
        keep_recommendation_id: str = "",
        reason: str = "superseded by newer recommendation",
    ) -> int:
        conn = _connect(self.db_path)
        if account_id:
            cur = conn.execute(
                """UPDATE recommendation_batches
                   SET status='superseded', error=?, updated_at=?
                   WHERE status='pending' AND account_id=? AND recommendation_id<>?
                     AND signal_date=(
                       SELECT signal_date FROM recommendation_batches WHERE recommendation_id=?
                     )""",
                (reason, _now(), account_id, keep_recommendation_id, keep_recommendation_id),
            )
        else:
            cur = conn.execute(
                """UPDATE recommendation_batches
                   SET status='superseded', error=?, updated_at=?
                   WHERE status='pending' AND model_run_id=? AND recommendation_id<>?
                     AND signal_date=(
                       SELECT signal_date FROM recommendation_batches WHERE recommendation_id=?
                     )""",
                (reason, _now(), model_run_id, keep_recommendation_id, keep_recommendation_id),
            )
        count = int(cur.rowcount or 0)
        conn.commit()
        conn.close()
        return count

    def record_execution(self, payload: dict[str, Any]) -> str:
        execution_id = str(payload["execution_id"])
        conn = _connect(self.db_path)
        _insert_execution(conn, payload)
        conn.commit()
        conn.close()
        return execution_id

    def commit_execution(
        self,
        *,
        execution: dict[str, Any],
        recommendation_id: str,
        recommendation_status: str,
        snapshot: dict[str, Any] | None = None,
        error: str = "",
    ) -> str:
        """Commit one execution, its snapshot and recommendation state atomically."""
        conn = _connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _insert_execution(conn, execution)
            if snapshot:
                _upsert_snapshot(conn, snapshot)
            current = conn.execute(
                "SELECT metrics, execution_files FROM recommendation_batches WHERE recommendation_id=?",
                (recommendation_id,),
            ).fetchone()
            metrics = _json_load(current["metrics"], {}) if current else {}
            metrics.update(execution.get("metrics") or {})
            execution_files = _json_load(current["execution_files"], {}) if current else {}
            execution_files.update(execution.get("output_files") or {})
            conn.execute(
                """UPDATE recommendation_batches
                   SET status=?, error=?, metrics=?, execution_files=?, updated_at=?
                   WHERE recommendation_id=?""",
                (
                    recommendation_status,
                    error,
                    _json_dump(metrics),
                    _json_dump(execution_files),
                    _now(),
                    recommendation_id,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return str(execution["execution_id"])

    def get_recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        conn = _connect(self.db_path)
        row = conn.execute(
            "SELECT * FROM recommendation_batches WHERE recommendation_id=?",
            (recommendation_id,),
        ).fetchone()
        conn.close()
        return _normalize_row(row)

    def latest_recommendation(self, account_id: str | None = None) -> dict[str, Any] | None:
        conn = _connect(self.db_path)
        if account_id:
            row = conn.execute(
                "SELECT * FROM recommendation_batches WHERE account_id=? ORDER BY signal_date DESC, created_at DESC LIMIT 1",
                (account_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM recommendation_batches ORDER BY signal_date DESC, created_at DESC LIMIT 1"
            ).fetchone()
        conn.close()
        return _normalize_row(row)

    def list_recommendations(self, status: str = "all", limit: int = 20, account_id: str | None = None) -> list[dict[str, Any]]:
        conn = _connect(self.db_path)
        if status == "all" and account_id:
            rows = conn.execute(
                "SELECT * FROM recommendation_batches WHERE account_id=? ORDER BY signal_date DESC, created_at DESC LIMIT ?",
                (account_id, int(limit)),
            ).fetchall()
        elif status == "all":
            rows = conn.execute(
                "SELECT * FROM recommendation_batches ORDER BY signal_date DESC, created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        elif account_id:
            rows = conn.execute(
                "SELECT * FROM recommendation_batches WHERE status=? AND account_id=? ORDER BY signal_date DESC, created_at DESC LIMIT ?",
                (status, account_id, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM recommendation_batches WHERE status=? ORDER BY signal_date DESC, created_at DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        conn.close()
        return [row for row in (_normalize_row(r) for r in rows) if row]

    def pending_recommendations(self, limit: int = 50, account_id: str | None = None) -> list[dict[str, Any]]:
        return self.list_recommendations("pending", limit=limit, account_id=account_id)

    def list_orders(self, recommendation_id: str, limit: int = 200) -> list[dict[str, Any]]:
        conn = _connect(self.db_path)
        rows = conn.execute(
            """SELECT * FROM recommendation_orders
               WHERE recommendation_id=?
               ORDER BY
                 CASE action
                   WHEN 'buy' THEN 0
                   WHEN 'hold' THEN 1
                   WHEN 'sell' THEN 2
                   ELSE 3
                 END,
                 target_weight DESC,
                 score DESC,
                 ABS(delta_shares) DESC,
                 instrument
               LIMIT ?""",
            (recommendation_id, int(limit)),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def latest_execution(self, model_run_id: str | None = None, account_id: str | None = None) -> dict[str, Any] | None:
        conn = _connect(self.db_path)
        if account_id:
            row = conn.execute(
                """SELECT * FROM paper_executions
                   WHERE account_id=? ORDER BY trade_date DESC, created_at DESC LIMIT 1""",
                (account_id,),
            ).fetchone()
        elif model_run_id:
            row = conn.execute(
                """SELECT * FROM paper_executions
                   WHERE model_run_id=?
                   ORDER BY trade_date DESC, created_at DESC LIMIT 1""",
                (model_run_id,),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM paper_executions ORDER BY created_at DESC LIMIT 1").fetchone()
        conn.close()
        if not row:
            return None
        out = dict(row)
        out["output_files"] = _json_load(out.get("output_files"), {})
        out["metrics"] = _json_load(out.get("metrics"), {})
        out["diagnostics"] = _json_load(out.get("diagnostics"), {})
        out["notes"] = _json_load(out.get("notes"), [])
        return out

    def execution_for_recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        conn = _connect(self.db_path)
        row = conn.execute(
            "SELECT * FROM paper_executions WHERE recommendation_id=? ORDER BY created_at DESC LIMIT 1",
            (recommendation_id,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        out = dict(row)
        out["output_files"] = _json_load(out.get("output_files"), {})
        out["metrics"] = _json_load(out.get("metrics"), {})
        out["diagnostics"] = _json_load(out.get("diagnostics"), {})
        out["notes"] = _json_load(out.get("notes"), [])
        return out

    def record_account_snapshot(self, payload: dict[str, Any]) -> None:
        conn = _connect(self.db_path)
        _upsert_snapshot(conn, payload)
        conn.commit()
        conn.close()

    def account_snapshot(self, account_id: str, trade_date: str) -> dict[str, Any] | None:
        conn = _connect(self.db_path)
        row = conn.execute(
            "SELECT * FROM paper_account_snapshots WHERE account_id=? AND trade_date=?",
            (account_id, trade_date),
        ).fetchone()
        conn.close()
        return _normalize_account_snapshot(row)

    def account_integrity_issues(self, account_id: str) -> list[dict[str, Any]]:
        """Return cheap ledger invariants shared by status, preflight and day runs."""
        conn = _connect(self.db_path)
        issues: list[dict[str, Any]] = []
        lost = conn.execute(
            """SELECT r.recommendation_id, r.signal_date, r.execution_date
               FROM recommendation_batches r
               WHERE r.account_id=? AND r.status='superseded'
                 AND NOT EXISTS (
                   SELECT 1 FROM paper_executions e
                   WHERE e.recommendation_id=r.recommendation_id AND e.status='completed'
                 )
                 AND NOT EXISTS (
                   SELECT 1
                   FROM recommendation_batches canonical
                   JOIN paper_executions e
                     ON e.recommendation_id=canonical.recommendation_id
                    AND e.status='completed'
                   WHERE canonical.account_id=r.account_id
                     AND canonical.signal_date=r.signal_date
                 )
                 AND EXISTS (
                   SELECT 1 FROM paper_account_snapshots s
                   WHERE s.account_id=r.account_id AND s.trade_date>r.signal_date
                 )
               ORDER BY r.signal_date""",
            (account_id,),
        ).fetchall()
        for row in lost:
            issues.append(
                {
                    "code": "unexecuted_recommendation_superseded",
                    "recommendation_id": row["recommendation_id"],
                    "signal_date": row["signal_date"],
                    "execution_date": row["execution_date"],
                }
            )
        executed_without_record = conn.execute(
            """SELECT r.recommendation_id, r.signal_date, r.execution_date
               FROM recommendation_batches r
               WHERE r.account_id=? AND r.status='executed'
                 AND NOT EXISTS (
                   SELECT 1 FROM paper_executions e
                   WHERE e.recommendation_id=r.recommendation_id AND e.status='completed'
                 )""",
            (account_id,),
        ).fetchall()
        for row in executed_without_record:
            issues.append(
                {
                    "code": "executed_recommendation_missing_execution",
                    "recommendation_id": row["recommendation_id"],
                    "signal_date": row["signal_date"],
                    "execution_date": row["execution_date"],
                }
            )
        pending_rows = conn.execute(
            "SELECT recommendation_id, signal_date FROM recommendation_batches WHERE account_id=? AND status='pending' ORDER BY signal_date",
            (account_id,),
        ).fetchall()
        if len(pending_rows) > 1:
            issues.append(
                {
                    "code": "multiple_pending_recommendations",
                    "recommendations": [dict(row) for row in pending_rows],
                }
            )
        snapshots = conn.execute(
            """SELECT trade_date, cash, stock_value, account_value
               FROM paper_account_snapshots WHERE account_id=? ORDER BY trade_date""",
            (account_id,),
        ).fetchall()
        for row in snapshots:
            gap = abs(float(row["cash"] or 0) + float(row["stock_value"] or 0) - float(row["account_value"] or 0))
            if gap > 0.01:
                issues.append({"code": "account_value_identity_mismatch", "trade_date": row["trade_date"], "gap": gap})
        conn.close()
        return issues

    def latest_account_snapshot(self, account_id: str | None = None) -> dict[str, Any] | None:
        conn = _connect(self.db_path)
        if account_id:
            row = conn.execute(
                """SELECT * FROM paper_account_snapshots
                   WHERE account_id=?
                   ORDER BY trade_date DESC, created_at DESC LIMIT 1""",
                (account_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM paper_account_snapshots ORDER BY trade_date DESC, created_at DESC LIMIT 1"
            ).fetchone()
        conn.close()
        return _normalize_account_snapshot(row)

    def list_account_snapshots(self, account_id: str, limit: int = 260) -> list[dict[str, Any]]:
        conn = _connect(self.db_path)
        rows = conn.execute(
            """SELECT * FROM paper_account_snapshots
               WHERE account_id=?
               ORDER BY trade_date DESC, created_at DESC
               LIMIT ?""",
            (account_id, int(limit)),
        ).fetchall()
        conn.close()
        normalized = [item for item in (_normalize_account_snapshot(row) for row in rows) if item]
        return _attach_account_history_returns(list(reversed(normalized)))

    def list_latest_accounts(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = _connect(self.db_path)
        rows = conn.execute(
            """SELECT s.*
               FROM paper_account_snapshots s
               JOIN (
                 SELECT account_id, MAX(trade_date) AS max_trade_date
                 FROM paper_account_snapshots
                 GROUP BY account_id
               ) latest
                 ON latest.account_id=s.account_id
                AND latest.max_trade_date=s.trade_date
               ORDER BY s.trade_date DESC, s.account_value DESC
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
        conn.close()
        return [item for item in (_normalize_account_snapshot(row) for row in rows) if item]

    def upsert_account(self, payload: dict[str, Any]) -> str:
        account_id = str(payload["account_id"])
        now = _now()
        conn = _connect(self.db_path)
        conn.execute(
            """INSERT INTO paper_accounts
               (account_id, display_name, account_mode, initial_capital,
                strategy_contract_version, topk, n_drop, hold_thresh, deal_price,
                status, metadata, created_at, updated_at, retired_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET
                display_name=excluded.display_name,
                account_mode=excluded.account_mode,
                initial_capital=excluded.initial_capital,
                strategy_contract_version=excluded.strategy_contract_version,
                topk=excluded.topk,
                n_drop=excluded.n_drop,
                hold_thresh=excluded.hold_thresh,
                deal_price=excluded.deal_price,
                status=excluded.status,
                metadata=excluded.metadata,
                updated_at=excluded.updated_at,
                retired_at=excluded.retired_at""",
            (
                account_id,
                payload.get("display_name", account_id),
                payload.get("account_mode", "fixed_model"),
                float(payload.get("initial_capital", 1_000_000.0)),
                payload.get("strategy_contract_version", "top20_drop2_hold5_open_v1"),
                int(payload.get("topk", MODEL_DEFAULT_TOPK)),
                int(payload.get("n_drop", 2)),
                int(payload.get("hold_thresh", 5)),
                payload.get("deal_price", "open"),
                payload.get("status", "active"),
                _json_dump(payload.get("metadata", {})),
                payload.get("created_at") or now,
                now,
                payload.get("retired_at", ""),
            ),
        )
        conn.commit()
        conn.close()
        return account_id

    def upsert_account_with_deployment(
        self,
        *,
        account: dict[str, Any],
        deployment: dict[str, Any],
    ) -> tuple[str, str]:
        """Create/update an account and its dated deployment in one transaction."""
        account_id = str(account["account_id"])
        deployment_id = str(deployment["deployment_id"])
        now = _now()
        conn = _connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO paper_accounts
                   (account_id, display_name, account_mode, initial_capital,
                    strategy_contract_version, topk, n_drop, hold_thresh, deal_price,
                    status, metadata, created_at, updated_at, retired_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    account_mode=excluded.account_mode,
                    initial_capital=excluded.initial_capital,
                    strategy_contract_version=excluded.strategy_contract_version,
                    topk=excluded.topk,
                    n_drop=excluded.n_drop,
                    hold_thresh=excluded.hold_thresh,
                    deal_price=excluded.deal_price,
                    status=excluded.status,
                    metadata=excluded.metadata,
                    updated_at=excluded.updated_at,
                    retired_at=excluded.retired_at""",
                (
                    account_id,
                    account.get("display_name", account_id),
                    account.get("account_mode", "fixed_model"),
                    float(account.get("initial_capital", 1_000_000.0)),
                    account.get("strategy_contract_version", "top20_drop2_hold5_open_v1"),
                    int(account.get("topk", MODEL_DEFAULT_TOPK)),
                    int(account.get("n_drop", 2)),
                    int(account.get("hold_thresh", 5)),
                    account.get("deal_price", "open"),
                    account.get("status", "active"),
                    _json_dump(account.get("metadata", {})),
                    account.get("created_at") or now,
                    now,
                    account.get("retired_at", ""),
                ),
            )
            conn.execute(
                """INSERT INTO paper_account_model_deployments
                   (deployment_id, account_id, model_id, model_run_id, feature_set_id,
                    effective_from, effective_to, deployment_mode, status, evidence,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(deployment_id) DO UPDATE SET
                    model_id=excluded.model_id,
                    model_run_id=excluded.model_run_id,
                    feature_set_id=excluded.feature_set_id,
                    effective_from=excluded.effective_from,
                    effective_to=excluded.effective_to,
                    deployment_mode=excluded.deployment_mode,
                    status=excluded.status,
                    evidence=excluded.evidence,
                    updated_at=excluded.updated_at""",
                (
                    deployment_id,
                    account_id,
                    deployment["model_id"],
                    deployment["model_run_id"],
                    deployment.get("feature_set_id", ""),
                    deployment["effective_from"],
                    deployment.get("effective_to", ""),
                    deployment.get("deployment_mode", "fixed_model"),
                    deployment.get("status", "active"),
                    _json_dump(deployment.get("evidence", {})),
                    deployment.get("created_at") or now,
                    now,
                ),
            )
            rows = conn.execute(
                """SELECT deployment_id, effective_from
                   FROM paper_account_model_deployments
                   WHERE account_id=?
                   ORDER BY effective_from, created_at, deployment_id""",
                (account_id,),
            ).fetchall()
            for index, row in enumerate(rows):
                next_row = rows[index + 1] if index + 1 < len(rows) else None
                effective_to = ""
                deployment_status = "active"
                if next_row:
                    next_start = datetime.fromisoformat(str(next_row["effective_from"])).date()
                    effective_to = str(next_start - timedelta(days=1))
                    deployment_status = "retired"
                conn.execute(
                    """UPDATE paper_account_model_deployments
                       SET effective_to=?, status=?, updated_at=? WHERE deployment_id=?""",
                    (effective_to, deployment_status, now, row["deployment_id"]),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return account_id, deployment_id

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        conn = _connect(self.db_path)
        row = conn.execute("SELECT * FROM paper_accounts WHERE account_id=?", (account_id,)).fetchone()
        conn.close()
        if not row:
            return None
        out = dict(row)
        out["metadata"] = _json_load(out.get("metadata"), {})
        return out

    def list_accounts(self, status: str = "all") -> list[dict[str, Any]]:
        conn = _connect(self.db_path)
        if status == "all":
            rows = conn.execute("SELECT * FROM paper_accounts ORDER BY account_id").fetchall()
        else:
            rows = conn.execute("SELECT * FROM paper_accounts WHERE status=? ORDER BY account_id", (status,)).fetchall()
        conn.close()
        out = []
        for row in rows:
            item = dict(row)
            item["metadata"] = _json_load(item.get("metadata"), {})
            out.append(item)
        return out

    def set_account_status(self, account_id: str, status: str) -> None:
        retired_at = _now() if status == "retired" else ""
        conn = _connect(self.db_path)
        conn.execute(
            "UPDATE paper_accounts SET status=?, retired_at=?, updated_at=? WHERE account_id=?",
            (status, retired_at, _now(), account_id),
        )
        conn.commit()
        conn.close()

    def transition_account_status(self, account_id: str, status: str) -> dict[str, Any]:
        """Atomically change lifecycle state and settle retired pending plans."""
        if status not in {"active", "paused", "retired"}:
            raise ValueError("invalid_paper_account_status")
        now = _now()
        conn = _connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status, retired_at FROM paper_accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if not existing:
                raise ValueError("paper_account_not_found")
            retired_at = (
                str(existing["retired_at"] or now)
                if status == "retired"
                else ""
            )
            conn.execute(
                "UPDATE paper_accounts SET status=?, retired_at=?, updated_at=? WHERE account_id=?",
                (status, retired_at, now, account_id),
            )
            settled = 0
            if status == "retired":
                cursor = conn.execute(
                    """UPDATE recommendation_batches
                       SET status='superseded', error='account_retired_pending_cancelled', updated_at=?
                       WHERE account_id=? AND status='pending'""",
                    (now, account_id),
                )
                settled = int(cursor.rowcount or 0)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "account_id": account_id,
            "previous_status": str(existing["status"]),
            "status": status,
            "retired_pending_settled": settled,
        }

    def reconcile_stale_account_runs(self, account_id: str | None = None) -> list[dict[str, Any]]:
        """Close abandoned running rows from durable ledger evidence without replaying trades."""
        conn = _connect(self.db_path)
        clauses = ["status='running'"]
        params: list[Any] = []
        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        rows = conn.execute(
            f"SELECT * FROM paper_account_runs WHERE {' AND '.join(clauses)} ORDER BY signal_date",
            params,
        ).fetchall()
        actions: list[dict[str, Any]] = []
        now = _now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                snapshot = conn.execute(
                    "SELECT 1 FROM paper_account_snapshots WHERE account_id=? AND trade_date=?",
                    (row["account_id"], row["signal_date"]),
                ).fetchone()
                recommendation = conn.execute(
                    """SELECT recommendation_id FROM recommendation_batches
                       WHERE account_id=? AND signal_date=? ORDER BY created_at DESC LIMIT 1""",
                    (row["account_id"], row["signal_date"]),
                ).fetchone()
                completed = bool(snapshot and recommendation)
                next_status = "completed" if completed else "failed"
                stage = "recovered_completed" if completed else "interrupted"
                error = "" if completed else "abandoned_running_attempt_without_complete_evidence"
                conn.execute(
                    """UPDATE paper_account_runs
                       SET status=?, current_stage=?, error=?, completed_at=?, updated_at=?
                       WHERE account_run_id=? AND status='running'""",
                    (next_status, stage, error, now, now, row["account_run_id"]),
                )
                payload = {
                    "reason": "stale_running_reconciliation",
                    "snapshot_present": bool(snapshot),
                    "recommendation_present": bool(recommendation),
                }
                conn.execute(
                    """INSERT INTO paper_run_events
                       (fleet_run_id, account_run_id, account_id, stage, status, payload, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        row["fleet_run_id"], row["account_run_id"], row["account_id"],
                        stage, next_status, _json_dump(payload), now,
                    ),
                )
                actions.append({
                    "account_run_id": row["account_run_id"],
                    "account_id": row["account_id"],
                    "signal_date": row["signal_date"],
                    "status": next_status,
                    **payload,
                })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return actions

    def upsert_deployment(self, payload: dict[str, Any]) -> str:
        deployment_id = str(payload["deployment_id"])
        now = _now()
        conn = _connect(self.db_path)
        conn.execute(
            """INSERT INTO paper_account_model_deployments
               (deployment_id, account_id, model_id, model_run_id, feature_set_id,
                effective_from, effective_to, deployment_mode, status, evidence,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(deployment_id) DO UPDATE SET
                account_id=excluded.account_id,
                model_id=excluded.model_id,
                model_run_id=excluded.model_run_id,
                feature_set_id=excluded.feature_set_id,
                effective_from=excluded.effective_from,
                effective_to=excluded.effective_to,
                deployment_mode=excluded.deployment_mode,
                status=excluded.status,
                evidence=excluded.evidence,
                updated_at=excluded.updated_at""",
            (
                deployment_id,
                payload["account_id"],
                payload["model_id"],
                payload["model_run_id"],
                payload.get("feature_set_id", ""),
                payload["effective_from"],
                payload.get("effective_to", ""),
                payload.get("deployment_mode", "fixed_model"),
                payload.get("status", "active"),
                _json_dump(payload.get("evidence", {})),
                payload.get("created_at") or now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        return deployment_id

    def resequence_deployments(self, account_id: str) -> None:
        """Make dated account deployments non-overlapping and auditable."""
        conn = _connect(self.db_path)
        rows = conn.execute(
            """SELECT deployment_id, effective_from
               FROM paper_account_model_deployments
               WHERE account_id=?
               ORDER BY effective_from, created_at, deployment_id""",
            (account_id,),
        ).fetchall()
        now = _now()
        for index, row in enumerate(rows):
            next_row = rows[index + 1] if index + 1 < len(rows) else None
            effective_to = ""
            status = "active"
            if next_row:
                next_start = datetime.fromisoformat(str(next_row["effective_from"])).date()
                effective_to = str(next_start - timedelta(days=1))
                status = "retired"
            conn.execute(
                """UPDATE paper_account_model_deployments
                   SET effective_to=?, status=?, updated_at=?
                   WHERE deployment_id=?""",
                (effective_to, status, now, row["deployment_id"]),
            )
        conn.commit()
        conn.close()

    def list_deployments(self, account_id: str | None = None, status: str = "all") -> list[dict[str, Any]]:
        conn = _connect(self.db_path)
        clauses: list[str] = []
        params: list[Any] = []
        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM paper_account_model_deployments{where} ORDER BY account_id, effective_from",
            params,
        ).fetchall()
        conn.close()
        out = []
        for row in rows:
            item = dict(row)
            item["evidence"] = _json_load(item.get("evidence"), {})
            out.append(item)
        return out

    def deployment_for_date(self, account_id: str, signal_date: str) -> dict[str, Any] | None:
        conn = _connect(self.db_path)
        row = conn.execute(
            """SELECT * FROM paper_account_model_deployments
               WHERE account_id=? AND status IN ('active','retired')
                 AND effective_from<=?
                 AND (effective_to='' OR effective_to>=?)
               ORDER BY effective_from DESC, created_at DESC LIMIT 1""",
            (account_id, signal_date, signal_date),
        ).fetchone()
        conn.close()
        if not row:
            return None
        out = dict(row)
        out["evidence"] = _json_load(out.get("evidence"), {})
        return out

    def upsert_fleet_run(self, payload: dict[str, Any]) -> str:
        fleet_run_id = str(payload["fleet_run_id"])
        now = _now()
        started_at = str(payload.get("started_at") or now)
        completed_at = str(payload.get("completed_at") or "")
        if completed_at and completed_at < started_at:
            completed_at = started_at
        conn = _connect(self.db_path)
        conn.execute(
            """INSERT INTO paper_fleet_runs
               (fleet_run_id, target_date, data_package_id, data_latest_date, status,
                current_stage, account_count, completed_count, failed_count,
                inputs, outputs, error, started_at, completed_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(fleet_run_id) DO UPDATE SET
                status=excluded.status, current_stage=excluded.current_stage,
                account_count=excluded.account_count, completed_count=excluded.completed_count,
                failed_count=excluded.failed_count, outputs=excluded.outputs,
                error=excluded.error,
                completed_at=CASE
                    WHEN excluded.completed_at != ''
                         AND excluded.completed_at < paper_fleet_runs.started_at
                    THEN paper_fleet_runs.started_at
                    ELSE excluded.completed_at
                END,
                updated_at=excluded.updated_at""",
            (
                fleet_run_id, payload["target_date"], payload.get("data_package_id", ""),
                payload.get("data_latest_date", ""), payload.get("status", "running"),
                payload.get("current_stage", ""), int(payload.get("account_count", 0)),
                int(payload.get("completed_count", 0)), int(payload.get("failed_count", 0)),
                _json_dump(payload.get("inputs", {})), _json_dump(payload.get("outputs", {})),
                payload.get("error", ""), started_at, completed_at, now,
            ),
        )
        conn.commit()
        conn.close()
        return fleet_run_id

    def get_fleet_run(self, fleet_run_id: str) -> dict[str, Any] | None:
        conn = _connect(self.db_path)
        row = conn.execute("SELECT * FROM paper_fleet_runs WHERE fleet_run_id=?", (fleet_run_id,)).fetchone()
        conn.close()
        if not row:
            return None
        out = dict(row)
        out["inputs"] = _json_load(out.get("inputs"), {})
        out["outputs"] = _json_load(out.get("outputs"), {})
        return out

    def upsert_account_run(self, payload: dict[str, Any]) -> str:
        account_run_id = str(payload["account_run_id"])
        now = _now()
        started_at = str(payload.get("started_at") or now)
        completed_at = str(payload.get("completed_at") or "")
        if completed_at and completed_at < started_at:
            completed_at = started_at
        conn = _connect(self.db_path)
        conn.execute(
            """INSERT INTO paper_account_runs
               (account_run_id, fleet_run_id, account_id, signal_date, model_id,
                model_run_id, strategy_contract_version, config_hash, run_kind,
                status, current_stage, attempt, inputs, outputs, error,
                started_at, completed_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_run_id) DO UPDATE SET
                status=excluded.status, current_stage=excluded.current_stage,
                attempt=excluded.attempt, outputs=excluded.outputs, error=excluded.error,
                completed_at=CASE
                    WHEN excluded.completed_at != ''
                         AND excluded.completed_at < paper_account_runs.started_at
                    THEN paper_account_runs.started_at
                    ELSE excluded.completed_at
                END,
                updated_at=excluded.updated_at""",
            (
                account_run_id, payload.get("fleet_run_id", ""), payload["account_id"],
                payload["signal_date"], payload.get("model_id", ""), payload.get("model_run_id", ""),
                payload.get("strategy_contract_version", ""), payload.get("config_hash", ""),
                payload.get("run_kind", "on_time"), payload.get("status", "running"),
                payload.get("current_stage", ""), int(payload.get("attempt", 1)),
                _json_dump(payload.get("inputs", {})), _json_dump(payload.get("outputs", {})),
                payload.get("error", ""), started_at, completed_at, now,
            ),
        )
        conn.commit()
        conn.close()
        return account_run_id

    def get_account_run(self, account_run_id: str) -> dict[str, Any] | None:
        conn = _connect(self.db_path)
        row = conn.execute("SELECT * FROM paper_account_runs WHERE account_run_id=?", (account_run_id,)).fetchone()
        conn.close()
        if not row:
            return None
        out = dict(row)
        out["inputs"] = _json_load(out.get("inputs"), {})
        out["outputs"] = _json_load(out.get("outputs"), {})
        return out

    def list_account_runs(self, account_id: str | None = None, limit: int = 260) -> list[dict[str, Any]]:
        conn = _connect(self.db_path)
        if account_id:
            rows = conn.execute(
                "SELECT * FROM paper_account_runs WHERE account_id=? ORDER BY signal_date DESC LIMIT ?",
                (account_id, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM paper_account_runs ORDER BY signal_date DESC LIMIT ?", (int(limit),)).fetchall()
        conn.close()
        out = []
        for row in rows:
            item = dict(row)
            item["inputs"] = _json_load(item.get("inputs"), {})
            item["outputs"] = _json_load(item.get("outputs"), {})
            out.append(item)
        return out

    def record_run_event(self, payload: dict[str, Any]) -> int:
        conn = _connect(self.db_path)
        cur = conn.execute(
            """INSERT INTO paper_run_events
               (fleet_run_id, account_run_id, account_id, stage, status, payload, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                payload.get("fleet_run_id", ""), payload.get("account_run_id", ""),
                payload.get("account_id", ""), payload.get("stage", ""),
                payload.get("status", ""), _json_dump(payload.get("payload", {})),
                payload.get("created_at") or _now(),
            ),
        )
        event_id = int(cur.lastrowid)
        conn.commit()
        conn.close()
        return event_id

    def summary(self) -> dict[str, Any]:
        conn = _connect(self.db_path)
        total = conn.execute("SELECT COUNT(*) FROM recommendation_batches").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM recommendation_batches WHERE status='pending'").fetchone()[0]
        pending_by_lifecycle = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                """SELECT a.status, COUNT(*) AS count
                   FROM recommendation_batches r
                   JOIN paper_accounts a ON a.account_id=r.account_id
                   WHERE r.status='pending'
                   GROUP BY a.status"""
            ).fetchall()
        }
        executed = conn.execute("SELECT COUNT(*) FROM recommendation_batches WHERE status='executed'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM recommendation_batches WHERE status='failed'").fetchone()[0]
        executions = conn.execute("SELECT COUNT(*) FROM paper_executions").fetchone()[0]
        snapshots = conn.execute("SELECT COUNT(*) FROM paper_account_snapshots").fetchone()[0]
        accounts = conn.execute("SELECT COUNT(*) FROM paper_accounts").fetchone()[0]
        active_accounts = conn.execute("SELECT COUNT(*) FROM paper_accounts WHERE status='active'").fetchone()[0]
        fleet_runs = conn.execute("SELECT COUNT(*) FROM paper_fleet_runs").fetchone()[0]
        account_runs = conn.execute("SELECT COUNT(*) FROM paper_account_runs").fetchone()[0]
        conn.close()
        return {
            "total_recommendations": int(total),
            "pending": int(pending),
            "pending_active": pending_by_lifecycle.get("active", 0),
            "pending_paused_frozen": pending_by_lifecycle.get("paused", 0),
            "pending_retired": pending_by_lifecycle.get("retired", 0),
            "executed": int(executed),
            "failed": int(failed),
            "paper_executions": int(executions),
            "paper_account_snapshots": int(snapshots),
            "paper_accounts": int(accounts),
            "active_paper_accounts": int(active_accounts),
            "paper_fleet_runs": int(fleet_runs),
            "paper_account_runs": int(account_runs),
            "db_path": str(self.db_path),
        }
