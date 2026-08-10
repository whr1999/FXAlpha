"""
factor_registry.db — Factor lifecycle management.

Tables:
  - factors: factor definitions, metrics, status
  - factor_evals: historical evaluation snapshots
"""

import sqlite3
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from storage.paths import FACTOR_REGISTRY_DB

DB_PATH = FACTOR_REGISTRY_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS factors (
    factor_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    expression   TEXT NOT NULL,
    source       TEXT DEFAULT 'quantgpt',
    status       TEXT DEFAULT 'pending' CHECK(status IN ('pending','active','retired')),
    category     TEXT DEFAULT '',
    ic_mean      REAL,
    icir         REAL,
    rank_ic      REAL,
    rank_icir    REAL,
    sharpe       REAL,
    max_drawdown REAL,
    turnover     REAL,
    universe     TEXT DEFAULT 'hs300',
    holding_period_days INTEGER DEFAULT 5,
    created_at   TEXT NOT NULL,
    last_evaluated TEXT,
    retired_at   TEXT,
    retire_reason TEXT,
    metadata     TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS factor_evals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id      TEXT NOT NULL REFERENCES factors(factor_id),
    eval_date      TEXT NOT NULL,
    eval_source    TEXT DEFAULT 'quantgpt',
    ic_mean        REAL,
    icir           REAL,
    sharpe         REAL,
    max_drawdown   REAL,
    turnover       REAL,
    rolling_3m_ic  REAL
);

CREATE INDEX IF NOT EXISTS idx_factors_status   ON factors(status);
CREATE INDEX IF NOT EXISTS idx_factors_category ON factors(category);
CREATE INDEX IF NOT EXISTS idx_factors_icir     ON factors(icir DESC);
CREATE INDEX IF NOT EXISTS idx_evals_factor     ON factor_evals(factor_id);
CREATE INDEX IF NOT EXISTS idx_evals_date       ON factor_evals(eval_date);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_expression(expression: str) -> str:
    return re.sub(r"\s+", "", str(expression or "")).lower()


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(factors)").fetchall()}
    if "holding_period_days" not in columns:
        conn.execute("ALTER TABLE factors ADD COLUMN holding_period_days INTEGER DEFAULT 5")
    if "rank_icir" not in columns:
        conn.execute("ALTER TABLE factors ADD COLUMN rank_icir REAL")
    conn.commit()
    return conn


class FactorRegistry:
    """CRUD for factor_registry.db."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH

    # ── write ──────────────────────────────────────────

    def register(
        self,
        name: str,
        expression: str,
        qlib_code: Optional[str] = None,
        source: str = "quantgpt",
        status: str = "pending",
        category: str = "",
        metrics: Optional[dict] = None,
        universe: str = "hs300",
        holding_period_days: int = 5,
        metadata: Optional[dict] = None,
    ) -> str:
        """Register a new factor, returns factor_id."""
        if status == "active":
            existing = self.get_active_by_expression(expression)
            if existing:
                return str(existing["factor_id"])

        now = datetime.now()
        factor_id = f"f_{now.strftime('%Y%m%d_%H%M%S')}_{now.microsecond // 10000:02d}"
        m = metrics or {}
        metadata = metadata or {}
        if qlib_code and "qlib_code" not in metadata:
            metadata["qlib_code"] = qlib_code
        md = json.dumps(metadata)
        for _ in range(5):
            conn = _connect(self.db_path)
            try:
                conn.execute(
                    """INSERT INTO factors
                       (factor_id, name, expression, source, status, category,
                        ic_mean, icir, rank_ic, rank_icir, sharpe, max_drawdown, turnover,
                        universe, holding_period_days, created_at, last_evaluated, metadata)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        factor_id, name, expression, source, status, category,
                        m.get("ic_mean"), m.get("icir"), m.get("rank_ic"), m.get("rank_icir"),
                        m.get("sharpe"), m.get("max_drawdown"), m.get("turnover"),
                        universe, int(holding_period_days or 5), _now(), _now() if m else None,
                        md,
                    ),
                )
                conn.commit()
                conn.close()
                return factor_id
            except Exception:
                conn.close()
                import time
                factor_id = f"f_{now.strftime('%Y%m%d_%H%M%S')}_{now.microsecond // 10000:02d}_{int(time.time() * 1e6) % 10000:04d}"
        raise RuntimeError(f"Failed to register factor after retries")

    def prepare_import(
        self,
        *,
        name: str,
        expression: str,
        source: str,
        category: str,
        metrics: Optional[dict],
        universe: str,
        holding_period_days: int,
        metadata: Optional[dict],
    ) -> dict:
        """Create one pending import row under a write lock.

        This supports a recoverable filesystem-and-registry commit when a
        caller explicitly uses the pending-import protocol. Existing callers
        continue to use ``register`` unchanged.
        """
        now = datetime.now()
        factor_id = f"f_{now.strftime('%Y%m%d_%H%M%S')}_{now.microsecond // 10000:02d}"
        target = _normalize_expression(expression)
        m = metrics or {}
        md = json.dumps(metadata or {}, ensure_ascii=False)
        conn = _connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT factor_id,expression,status FROM factors WHERE status IN ('active','pending')"
            ).fetchall()
            for row in rows:
                if _normalize_expression(row["expression"]) == target:
                    conn.rollback()
                    return {
                        "prepared": False,
                        "duplicate": True,
                        "factor_id": str(row["factor_id"]),
                        "status": str(row["status"]),
                    }
            for attempt in range(20):
                try:
                    conn.execute(
                        """INSERT INTO factors
                           (factor_id,name,expression,source,status,category,
                            ic_mean,icir,rank_ic,rank_icir,sharpe,max_drawdown,turnover,
                            universe,holding_period_days,created_at,last_evaluated,metadata)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            factor_id,
                            name,
                            expression,
                            source,
                            "pending",
                            category,
                            m.get("ic_mean"),
                            m.get("icir"),
                            m.get("rank_ic"),
                            m.get("rank_icir"),
                            m.get("sharpe"),
                            m.get("max_drawdown"),
                            m.get("turnover"),
                            universe,
                            int(holding_period_days or 5),
                            _now(),
                            _now() if m else None,
                            md,
                        ),
                    )
                    break
                except sqlite3.IntegrityError:
                    factor_id = f"f_{now.strftime('%Y%m%d_%H%M%S')}_{now.microsecond // 10000:02d}_{attempt:02d}"
            else:
                raise RuntimeError("unable_to_allocate_pending_factor_id")
            conn.commit()
            return {"prepared": True, "duplicate": False, "factor_id": factor_id, "status": "pending"}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def activate_pending_import(self, factor_id: str, *, metadata: Optional[dict] = None) -> dict:
        conn = _connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,metadata FROM factors WHERE factor_id=?", (factor_id,)).fetchone()
            if not row:
                conn.rollback()
                return {"activated": False, "error": "factor_not_found", "factor_id": factor_id}
            if str(row["status"]) == "active":
                conn.rollback()
                return {"activated": True, "idempotent": True, "factor_id": factor_id, "status": "active"}
            if str(row["status"]) != "pending":
                conn.rollback()
                return {"activated": False, "error": f"factor_not_pending:{row['status']}", "factor_id": factor_id}
            raw = row["metadata"]
            try:
                current = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:
                current = {}
            current = current if isinstance(current, dict) else {}
            current.update(metadata or {})
            conn.execute(
                "UPDATE factors SET status='active', metadata=? WHERE factor_id=? AND status='pending'",
                (json.dumps(current, ensure_ascii=False), factor_id),
            )
            conn.commit()
            return {"activated": True, "idempotent": False, "factor_id": factor_id, "status": "active"}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def abort_pending_import(self, factor_id: str) -> dict:
        conn = _connect(self.db_path)
        try:
            cursor = conn.execute("DELETE FROM factors WHERE factor_id=? AND status='pending'", (factor_id,))
            conn.commit()
            return {"aborted": cursor.rowcount > 0, "factor_id": factor_id}
        finally:
            conn.close()

    def list_pending_imports(self, *, source: str | None = None) -> list[dict]:
        if not Path(self.db_path).exists():
            return []
        conn = sqlite3.connect(f"file:{Path(self.db_path).resolve()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if source:
            rows = conn.execute(
                "SELECT * FROM factors WHERE status='pending' AND source=? ORDER BY created_at",
                (source,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM factors WHERE status='pending' ORDER BY created_at").fetchall()
        conn.close()
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.get("metadata") or "{}")
            except Exception:
                item["metadata"] = {}
            out.append(item)
        return out

    # ── read ───────────────────────────────────────────

    def get(self, factor_id: str) -> Optional[dict]:
        conn = _connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM factors WHERE factor_id=?", (factor_id,)).fetchone()
        conn.close()
        if not row:
            return None
        d = dict(row)
        if isinstance(d.get("metadata"), str):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except Exception:
                pass
        return d

    def get_active_by_expression(self, expression: str) -> Optional[dict]:
        """Return an active factor with the same normalized expression, if any."""
        target = _normalize_expression(expression)
        if not target:
            return None
        conn = _connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM factors WHERE status='active'"
        ).fetchall()
        conn.close()
        for row in rows:
            item = dict(row)
            if _normalize_expression(item.get("expression", "")) == target:
                if isinstance(item.get("metadata"), str):
                    try:
                        item["metadata"] = json.loads(item["metadata"])
                    except Exception:
                        pass
                return item
        return None

    def list_active(self, min_icir: float = 0.0, holding_period_days: int | None = None) -> list[dict]:
        conn = _connect(self.db_path)
        if holding_period_days is None:
            rows = conn.execute(
                "SELECT * FROM factors WHERE status='active' AND COALESCE(rank_icir, icir) >= ? ORDER BY COALESCE(rank_icir, icir) DESC",
                (min_icir,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM factors WHERE status='active' AND COALESCE(rank_icir, icir) >= ? AND holding_period_days = ? ORDER BY COALESCE(rank_icir, icir) DESC",
                (min_icir, int(holding_period_days)),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def list_all(
        self, status: str = "all", category: str = "all",
        min_icir: float = 0.0, sort_by: str = "icir",
        limit: int = 20, offset: int = 0,
        holding_period_days: int | None = None,
    ) -> tuple[list[dict], int]:
        conn = _connect(self.db_path)
        where = []
        params = []
        if status != "all":
            where.append("status=?")
            params.append(status)
        if category != "all":
            where.append("category=?")
            params.append(category)
        if min_icir > 0:
            where.append("COALESCE(rank_icir, icir)>=?")
            params.append(min_icir)
        if holding_period_days is not None:
            where.append("holding_period_days=?")
            params.append(int(holding_period_days))
        where_clause = ("WHERE " + " AND ".join(where)) if where else ""
        count = conn.execute(f"SELECT COUNT(*) FROM factors {where_clause}", params).fetchone()[0]
        sort_cols = {
            "icir": "COALESCE(rank_icir, icir) DESC",
            "rank_icir": "rank_icir IS NULL ASC, rank_icir DESC, rank_ic DESC, icir DESC",
            "rank_ic_mean": "rank_ic IS NULL ASC, rank_ic DESC, icir DESC",
            "sharpe": "sharpe DESC",
            "created_at": "created_at DESC",
        }
        order = sort_cols.get(sort_by, "icir DESC")
        rows = conn.execute(
            f"SELECT * FROM factors {where_clause} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows], count

    def get_by_category(self) -> dict[str, int]:
        conn = _connect(self.db_path)
        rows = conn.execute(
            "SELECT category, COUNT(*) as cnt FROM factors WHERE status='active' GROUP BY category"
        ).fetchall()
        conn.close()
        return {r["category"] or "uncategorized": r["cnt"] for r in rows}

    def audit_active_duplicates(self) -> list[dict]:
        """Find active factors that share the same normalized expression."""
        rows = self.list_active(min_icir=-1e9)
        groups: dict[str, list[dict]] = {}
        for row in rows:
            key = _normalize_expression(row.get("expression", ""))
            if not key:
                continue
            groups.setdefault(key, []).append(row)

        duplicates: list[dict] = []
        for normalized_expression, items in groups.items():
            if len(items) <= 1:
                continue
            ordered = sorted(
                items,
                key=lambda x: (
                    float(x.get("icir") or 0.0),
                    float(x.get("ic_mean") or 0.0),
                    str(x.get("created_at") or ""),
                ),
                reverse=True,
            )
            duplicates.append(
                {
                    "normalized_expression": normalized_expression,
                    "expression": ordered[0].get("expression", ""),
                    "keeper": ordered[0],
                    "duplicates": ordered[1:],
                    "count": len(ordered),
                }
            )
        duplicates.sort(key=lambda x: x["count"], reverse=True)
        return duplicates

    def retire_active_duplicates(
        self,
        *,
        dry_run: bool = True,
        reason: str = "duplicate_active_expression",
    ) -> dict:
        """Retire duplicate active factors, preserving the best row per expression."""
        groups = self.audit_active_duplicates()
        to_retire: list[dict] = []
        for group in groups:
            to_retire.extend(group.get("duplicates", []))

        if not dry_run:
            for row in to_retire:
                self.retire(str(row["factor_id"]), reason)

        return {
            "dry_run": dry_run,
            "reason": reason,
            "duplicate_groups": len(groups),
            "retire_count": len(to_retire),
            "groups": groups,
            "retired_factor_ids": [str(row["factor_id"]) for row in to_retire],
        }

    # ── update ─────────────────────────────────────────

    def update_status(self, factor_id: str, status: str, **kwargs):
        conn = _connect(self.db_path)
        conn.execute("UPDATE factors SET status=? WHERE factor_id=?", (status, factor_id))
        conn.commit()
        conn.close()

    def update_metrics(self, factor_id: str, metrics: dict):
        conn = _connect(self.db_path)
        conn.execute(
            """UPDATE factors SET
               ic_mean=?, icir=?, rank_ic=?, rank_icir=?, sharpe=?, max_drawdown=?,
               turnover=?, last_evaluated=?
               WHERE factor_id=?""",
            (
                metrics.get("ic_mean"), metrics.get("icir"), metrics.get("rank_ic"), metrics.get("rank_icir"),
                metrics.get("sharpe"), metrics.get("max_drawdown"), metrics.get("turnover"),
                _now(), factor_id,
            ),
        )
        conn.commit()
        conn.close()

    def set_qlib_code(self, factor_id: str, qlib_code: str):
        conn = _connect(self.db_path)
        row = conn.execute("SELECT metadata FROM factors WHERE factor_id=?", (factor_id,)).fetchone()
        if row:
            try:
                metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {})
            except Exception:
                metadata = {}
            metadata = metadata if isinstance(metadata, dict) else {}
            metadata["qlib_code"] = qlib_code
            conn.execute(
                "UPDATE factors SET metadata=? WHERE factor_id=?",
                (json.dumps(metadata, ensure_ascii=False), factor_id),
            )
        conn.commit()
        conn.close()

    def update_meta(self, factor_id: str, metadata: dict):
        """Update the JSON metadata blob for a factor (stores WQ, notes, etc)."""
        import json as _json
        md = _json.dumps(metadata, ensure_ascii=False)
        conn = _connect(self.db_path)
        conn.execute("UPDATE factors SET metadata=? WHERE factor_id=?", (md, factor_id))
        conn.commit()
        conn.close()

    def retire(self, factor_id: str, reason: str):
        conn = _connect(self.db_path)
        conn.execute(
            "UPDATE factors SET status='retired', retired_at=?, retire_reason=? WHERE factor_id=?",
            (_now(), reason, factor_id),
        )
        conn.commit()
        conn.close()

    # ── summary ────────────────────────────────────────

    def summary(self) -> dict:
        conn = _connect(self.db_path)
        total = conn.execute("SELECT COUNT(*) FROM factors").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM factors WHERE status='active'").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM factors WHERE status='pending'").fetchone()[0]
        retired = conn.execute("SELECT COUNT(*) FROM factors WHERE status='retired'").fetchone()[0]
        avg_icir = conn.execute(
            "SELECT AVG(icir) FROM factors WHERE status='active'"
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT holding_period_days, COUNT(*) as cnt FROM factors WHERE status='active' GROUP BY holding_period_days"
        ).fetchall()
        conn.close()
        return {
            "total": total, "active": active, "pending": pending,
            "retired": retired, "avg_icir": round(avg_icir, 4) if avg_icir else 0,
            "holding_period_counts": {str(row["holding_period_days"]): row["cnt"] for row in rows if row["holding_period_days"] is not None},
        }

    def backfill_holding_period_days(self, default: int = 5) -> int:
        conn = _connect(self.db_path)
        rows = conn.execute("SELECT factor_id, metadata, holding_period_days FROM factors").fetchall()
        updated = 0
        for row in rows:
            hp = row["holding_period_days"]
            metadata_raw = row["metadata"]
            try:
                metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else (metadata_raw or {})
            except Exception:
                metadata = {}
            metadata = metadata if isinstance(metadata, dict) else {}
            meta_hp = metadata.get("holding_period_days")
            target_hp = hp or meta_hp or default
            if metadata.get("holding_period_days") != target_hp or hp != target_hp:
                metadata["holding_period_days"] = int(target_hp)
                conn.execute(
                    "UPDATE factors SET holding_period_days=?, metadata=? WHERE factor_id=?",
                    (int(target_hp), json.dumps(metadata, ensure_ascii=False), row["factor_id"]),
                )
                updated += 1
        conn.commit()
        conn.close()
        return updated

    def backfill_active_evidence_metadata(self) -> dict:
        """Normalize active factor evidence metadata without changing factor state."""
        conn = _connect(self.db_path)
        rows = conn.execute("SELECT factor_id, metadata FROM factors WHERE status='active'").fetchall()
        updated = 0
        for row in rows:
            raw = row["metadata"]
            try:
                metadata = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:
                metadata = {}
            metadata = metadata if isinstance(metadata, dict) else {}
            before = json.dumps(metadata, ensure_ascii=False, sort_keys=True)

            deep = metadata.get("deep_validation") if isinstance(metadata.get("deep_validation"), dict) else {}
            screening = metadata.get("screening") if isinstance(metadata.get("screening"), dict) else {}
            if not isinstance(metadata.get("novelty_guard"), dict) or not metadata.get("novelty_guard"):
                novelty = screening.get("novelty_guard") or deep.get("novelty_correlation") or deep.get("novelty_guard")
                if isinstance(novelty, dict) and novelty:
                    metadata["novelty_guard"] = novelty
            if not isinstance(metadata.get("anti_overfit"), dict) or not metadata.get("anti_overfit"):
                anti = metadata.get("anti_overfit_summary") or deep.get("anti_overfit")
                if isinstance(anti, dict) and anti:
                    metadata["anti_overfit"] = anti
            if not isinstance(metadata.get("anti_overfit_summary"), dict) or not metadata.get("anti_overfit_summary"):
                anti = metadata.get("anti_overfit")
                if isinstance(anti, dict) and anti:
                    metadata["anti_overfit_summary"] = anti
            if not isinstance(metadata.get("adversarial_validation"), dict) or not metadata.get("adversarial_validation"):
                adv = metadata.get("adversarial") or deep.get("adversarial_validation")
                if isinstance(adv, dict) and adv:
                    metadata["adversarial_validation"] = adv
            if not isinstance(metadata.get("economic_thesis"), dict) or not metadata.get("economic_thesis"):
                thesis = metadata.get("thesis") or deep.get("economic_thesis")
                if isinstance(thesis, dict) and thesis:
                    metadata["economic_thesis"] = thesis
            metadata["evidence_schema_version"] = metadata.get("evidence_schema_version") or "fxalpha_evidence_v1"

            metadata.pop("metadata_incomplete", None)
            metadata.pop("metadata_incomplete_reasons", None)

            after = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
            if after != before:
                conn.execute(
                    "UPDATE factors SET metadata=? WHERE factor_id=?",
                    (json.dumps(metadata, ensure_ascii=False), row["factor_id"]),
                )
                updated += 1
        conn.commit()
        conn.close()
        return {"active_checked": len(rows), "updated": updated, "metadata_incomplete": 0}


    # ── evals ──────────────────────────────────────────

    def add_eval(self, factor_id: str, metrics: dict, eval_source: str = "quantgpt"):
        conn = _connect()
        conn.execute(
            """INSERT INTO factor_evals
               (factor_id, eval_date, eval_source, ic_mean, icir, sharpe, max_drawdown, turnover, rolling_3m_ic)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                factor_id, _now(), eval_source,
                metrics.get("ic_mean"), metrics.get("icir"), metrics.get("sharpe"),
                metrics.get("max_drawdown"), metrics.get("turnover"),
                metrics.get("rolling_3m_ic"),
            ),
        )
        conn.commit()
        conn.close()
