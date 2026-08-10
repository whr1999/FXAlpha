"""
model_registry.db — Model lifecycle management for FXalpha.

Tables:
  - models: model metadata, metrics, feature-set provenance, status
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from storage.paths import MODEL_REGISTRY_DB

DB_PATH = MODEL_REGISTRY_DB
MODEL_LIBRARY_STATUSES = ("research", "candidate", "production", "archived")

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    model_id                    TEXT PRIMARY KEY,
    model_run_id                TEXT NOT NULL DEFAULT '',
    feature_set_id              TEXT NOT NULL DEFAULT '',
    feature_set_fingerprint     TEXT DEFAULT '',
    model_type                  TEXT DEFAULT '',
    model_family                TEXT DEFAULT 'lgbm',
    factor_count                INTEGER DEFAULT 0,
    factor_ids                  TEXT DEFAULT '[]',
    feature_count               INTEGER DEFAULT 0,
    ic_mean                     REAL,
    icir                        REAL,
    rank_ic                     REAL,
    rank_icir                   REAL,
    annualized_ret              REAL,
    max_drawdown                REAL,
    sharpe                      REAL,
    strategy_annualized_ret     REAL,
    strategy_sharpe             REAL,
    benchmark_annualized_ret    REAL,
    simple_excess_annualized_ret REAL,
    simple_excess_ir            REAL,
    excess_annualized_ret_with_cost REAL,
    excess_information_ratio_with_cost REAL,
    train_start                 TEXT,
    train_end                   TEXT,
    status                      TEXT DEFAULT 'research' CHECK(status IN ('research','candidate','production','archived')),
    workspace_path              TEXT DEFAULT '',
    run_dir                     TEXT DEFAULT '',
    trace_output_dir            TEXT DEFAULT '',
    resume_from_model_run_id    TEXT DEFAULT '',
    metadata                    TEXT DEFAULT '{}',
    created_at                  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_models_status ON models(status);
CREATE INDEX IF NOT EXISTS idx_models_feature_set_id ON models(feature_set_id);
CREATE INDEX IF NOT EXISTS idx_models_model_run_id ON models(model_run_id);
CREATE INDEX IF NOT EXISTS idx_models_created_at ON models(created_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate_status_schema(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(models)").fetchall()}
    if "rank_icir" not in cols:
        conn.execute("ALTER TABLE models ADD COLUMN rank_icir REAL")
    optional_cols = {
        "strategy_annualized_ret": "REAL",
        "strategy_sharpe": "REAL",
        "benchmark_annualized_ret": "REAL",
        "simple_excess_annualized_ret": "REAL",
        "simple_excess_ir": "REAL",
        "excess_annualized_ret_with_cost": "REAL",
        "excess_information_ratio_with_cost": "REAL",
    }
    for col, col_type in optional_cols.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE models ADD COLUMN {col} {col_type}")
    return conn


def _migrate_status_schema(conn: sqlite3.Connection) -> None:
    """Keep the registry on the four model-evidence lifecycle statuses."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='models'").fetchone()
    table_sql = str(row[0] if row else "")
    if (
        "research" not in table_sql
        or "candidate" not in table_sql
        or "production" not in table_sql
        or "archived" not in table_sql
        or "training" in table_sql
        or "running" in table_sql
        or "active" in table_sql
        or "failed" in table_sql
    ):
        conn.executescript(
            """
            ALTER TABLE models RENAME TO models_old;
            CREATE TABLE models (
                model_id                    TEXT PRIMARY KEY,
                model_run_id                TEXT NOT NULL DEFAULT '',
                feature_set_id              TEXT NOT NULL DEFAULT '',
                feature_set_fingerprint     TEXT DEFAULT '',
                model_type                  TEXT DEFAULT '',
                model_family                TEXT DEFAULT 'lgbm',
                factor_count                INTEGER DEFAULT 0,
                factor_ids                  TEXT DEFAULT '[]',
                feature_count               INTEGER DEFAULT 0,
                ic_mean                     REAL,
                icir                        REAL,
                rank_ic                     REAL,
                rank_icir                   REAL,
                annualized_ret              REAL,
                max_drawdown                REAL,
                sharpe                      REAL,
                strategy_annualized_ret     REAL,
                strategy_sharpe             REAL,
                benchmark_annualized_ret    REAL,
                simple_excess_annualized_ret REAL,
                simple_excess_ir            REAL,
                excess_annualized_ret_with_cost REAL,
                excess_information_ratio_with_cost REAL,
                train_start                 TEXT,
                train_end                   TEXT,
                status                      TEXT DEFAULT 'research' CHECK(status IN ('research','candidate','production','archived')),
                workspace_path              TEXT DEFAULT '',
                run_dir                     TEXT DEFAULT '',
                trace_output_dir            TEXT DEFAULT '',
                resume_from_model_run_id    TEXT DEFAULT '',
                metadata                    TEXT DEFAULT '{}',
                created_at                  TEXT NOT NULL
            );
            INSERT INTO models
            SELECT
                model_id, model_run_id, feature_set_id, feature_set_fingerprint,
                model_type, model_family, factor_count, factor_ids, feature_count,
                ic_mean, icir, rank_ic, rank_icir, annualized_ret, max_drawdown, sharpe,
                strategy_annualized_ret, strategy_sharpe, benchmark_annualized_ret,
                simple_excess_annualized_ret, simple_excess_ir,
                excess_annualized_ret_with_cost, excess_information_ratio_with_cost,
                train_start, train_end,
                CASE
                    WHEN status='production' THEN 'production'
                    WHEN status='candidate' OR status='active' OR status='research' THEN 'research'
                    ELSE 'archived'
                END AS status,
                workspace_path, run_dir, trace_output_dir, resume_from_model_run_id,
                metadata, created_at
            FROM models_old;
            DROP TABLE models_old;
            CREATE INDEX IF NOT EXISTS idx_models_status ON models(status);
            CREATE INDEX IF NOT EXISTS idx_models_feature_set_id ON models(feature_set_id);
            CREATE INDEX IF NOT EXISTS idx_models_model_run_id ON models(model_run_id);
            CREATE INDEX IF NOT EXISTS idx_models_created_at ON models(created_at DESC);
            """
        )
    conn.commit()


def _normalise_library_status(status: str | None, *, existing_status: str = "") -> str:
    normalized = str(status or "").strip().lower()
    existing = str(existing_status or "").strip().lower()
    if existing == "production" and normalized not in {"archived"}:
        return "production"
    if normalized in {"production", "candidate", "research", "archived"}:
        return normalized
    if normalized in {"active", "completed", "complete", "success", "succeeded"}:
        return "research"
    return "archived"


def _metadata_with_asset_status(metadata: Optional[dict], status: str) -> dict:
    payload = dict(metadata or {})
    payload["asset_status"] = status
    return payload


def _decode_metadata(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


class ModelRegistry:
    """CRUD for model_registry.db."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH

    def register(
        self,
        *,
        model_run_id: str = "",
        feature_set_id: str = "",
        feature_set_fingerprint: str = "",
        model_type: str = "FXAlpha model",
        model_family: str = "lgbm",
        factor_ids: Optional[list[str]] = None,
        feature_count: int = 0,
        metrics: Optional[dict] = None,
        workspace_path: str = "",
        run_dir: str = "",
        trace_output_dir: str = "",
        resume_from_model_run_id: str = "",
        train_start: str = "",
        train_end: str = "",
        status: str = "research",
        metadata: Optional[dict] = None,
    ) -> str:
        m = metrics or {}
        fids = factor_ids or []
        normalized_status = _normalise_library_status(status)
        md = json.dumps(_metadata_with_asset_status(metadata, normalized_status), ensure_ascii=False)
        conn = _connect(self.db_path)
        try:
            for _ in range(8):
                now = datetime.now()
                model_id = f"m_{now.strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:6]}"
                try:
                    conn.execute(
                        """INSERT INTO models
                           (model_id, model_run_id, feature_set_id, feature_set_fingerprint,
                            model_type, model_family, factor_count, factor_ids, feature_count,
                            ic_mean, icir, rank_ic, rank_icir, annualized_ret, max_drawdown, sharpe,
                            strategy_annualized_ret, strategy_sharpe, benchmark_annualized_ret,
                            simple_excess_annualized_ret, simple_excess_ir,
                            excess_annualized_ret_with_cost, excess_information_ratio_with_cost,
                            train_start, train_end, status, workspace_path, run_dir,
                            trace_output_dir, resume_from_model_run_id, metadata, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            model_id,
                            model_run_id,
                            feature_set_id,
                            feature_set_fingerprint,
                            model_type,
                            model_family,
                            len(fids),
                            json.dumps(fids, ensure_ascii=False),
                            feature_count,
                            m.get("ic_mean"),
                            m.get("icir"),
                            m.get("rank_ic"),
                            m.get("rank_icir"),
                            m.get("annualized_ret"),
                            m.get("max_drawdown"),
                            m.get("sharpe"),
                            m.get("strategy_annualized_ret"),
                            m.get("strategy_sharpe"),
                            m.get("benchmark_annualized_ret"),
                            m.get("simple_excess_annualized_ret"),
                            m.get("simple_excess_ir"),
                            m.get("excess_annualized_ret_with_cost", m.get("annualized_ret")),
                            m.get("excess_information_ratio_with_cost", m.get("sharpe")),
                            train_start,
                            train_end,
                            normalized_status,
                            workspace_path,
                            run_dir,
                            trace_output_dir,
                            resume_from_model_run_id,
                            md,
                            _now(),
                        ),
                    )
                    conn.commit()
                    return model_id
                except sqlite3.IntegrityError as exc:
                    if "models.model_id" not in str(exc):
                        raise
            raise RuntimeError("failed to allocate unique model_id after retries")
        finally:
            conn.close()

    def update_run_result(
        self,
        *,
        model_run_id: str,
        metrics: Optional[dict] = None,
        workspace_path: str = "",
        run_dir: str = "",
        trace_output_dir: str = "",
        train_start: str = "",
        train_end: str = "",
        feature_set_id: str = "",
        feature_set_fingerprint: str = "",
        factor_ids: Optional[list[str]] = None,
        feature_count: int = 0,
        status: str = "research",
        metadata: Optional[dict] = None,
    ) -> None:
        m = metrics or {}
        conn = _connect(self.db_path)
        row = conn.execute("SELECT metadata, status FROM models WHERE model_run_id=?", (model_run_id,)).fetchone()
        if not row:
            conn.close()
            return
        old_md = _decode_metadata(row["metadata"])
        if metadata:
            old_md.update(metadata)
        normalized_status = _normalise_library_status(status, existing_status=row["status"])
        old_md["asset_status"] = normalized_status
        conn.execute(
            """UPDATE models
               SET feature_set_id=COALESCE(NULLIF(?, ''), feature_set_id),
                   feature_set_fingerprint=COALESCE(NULLIF(?, ''), feature_set_fingerprint),
                   factor_count=COALESCE(NULLIF(?, 0), factor_count),
                   factor_ids=COALESCE(?, factor_ids),
                   feature_count=COALESCE(NULLIF(?, 0), feature_count),
                   ic_mean=COALESCE(?, ic_mean),
                   icir=COALESCE(?, icir),
                   rank_ic=COALESCE(?, rank_ic),
                   rank_icir=COALESCE(?, rank_icir),
                   annualized_ret=COALESCE(?, annualized_ret),
                   max_drawdown=COALESCE(?, max_drawdown),
                   sharpe=COALESCE(?, sharpe),
                   strategy_annualized_ret=COALESCE(?, strategy_annualized_ret),
                   strategy_sharpe=COALESCE(?, strategy_sharpe),
                   benchmark_annualized_ret=COALESCE(?, benchmark_annualized_ret),
                   simple_excess_annualized_ret=COALESCE(?, simple_excess_annualized_ret),
                   simple_excess_ir=COALESCE(?, simple_excess_ir),
                   excess_annualized_ret_with_cost=COALESCE(?, excess_annualized_ret_with_cost),
                   excess_information_ratio_with_cost=COALESCE(?, excess_information_ratio_with_cost),
                   train_start=COALESCE(NULLIF(?, ''), train_start),
                   train_end=COALESCE(NULLIF(?, ''), train_end), status=?,
                   workspace_path=COALESCE(NULLIF(?, ''), workspace_path),
                   run_dir=COALESCE(NULLIF(?, ''), run_dir),
                   trace_output_dir=COALESCE(NULLIF(?, ''), trace_output_dir),
                   metadata=?
               WHERE model_run_id=?""",
            (
                feature_set_id,
                feature_set_fingerprint,
                len(factor_ids or []),
                json.dumps(factor_ids, ensure_ascii=False) if factor_ids is not None else None,
                int(feature_count or 0),
                m.get("ic_mean"),
                m.get("icir"),
                m.get("rank_ic"),
                m.get("rank_icir"),
                m.get("annualized_ret"),
                m.get("max_drawdown"),
                m.get("sharpe"),
                m.get("strategy_annualized_ret"),
                m.get("strategy_sharpe"),
                m.get("benchmark_annualized_ret"),
                m.get("simple_excess_annualized_ret"),
                m.get("simple_excess_ir"),
                m.get("excess_annualized_ret_with_cost", m.get("annualized_ret")),
                m.get("excess_information_ratio_with_cost", m.get("sharpe")),
                train_start,
                train_end,
                normalized_status,
                workspace_path,
                run_dir,
                trace_output_dir,
                json.dumps(old_md, ensure_ascii=False),
                model_run_id,
            ),
        )
        conn.commit()
        conn.close()

    def set_running(self, model_run_id: str) -> None:
        # Running is process state, not a model-library asset status.
        return None

    def mark_failed(self, model_run_id: str) -> None:
        self._set_asset_status("model_run_id", model_run_id, "archived")

    def get(self, model_id: str) -> Optional[dict]:
        conn = _connect(self.db_path)
        row = conn.execute("SELECT * FROM models WHERE model_id=?", (model_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_production(self) -> Optional[dict]:
        conn = _connect(self.db_path)
        row = conn.execute("SELECT * FROM models WHERE status='production' ORDER BY created_at DESC LIMIT 1").fetchone()
        conn.close()
        return dict(row) if row else None

    def list_production(self) -> list[dict]:
        conn = _connect(self.db_path)
        rows = conn.execute("SELECT * FROM models WHERE status='production' ORDER BY created_at DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_latest(self) -> Optional[dict]:
        conn = _connect(self.db_path)
        row = conn.execute("SELECT * FROM models ORDER BY created_at DESC LIMIT 1").fetchone()
        conn.close()
        return dict(row) if row else None

    def list_models(self, status: str = "all") -> list[dict]:
        conn = _connect(self.db_path)
        if status == "all":
            rows = conn.execute("SELECT * FROM models ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM models WHERE status=? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def summary(self) -> dict:
        conn = _connect(self.db_path)
        total = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        candidate = conn.execute("SELECT COUNT(*) FROM models WHERE status='candidate'").fetchone()[0]
        research = conn.execute("SELECT COUNT(*) FROM models WHERE status='research'").fetchone()[0]
        production = conn.execute("SELECT COUNT(*) FROM models WHERE status='production'").fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM models WHERE status='archived'").fetchone()[0]
        conn.close()
        return {
            "total": total,
            "research": research,
            "candidate": candidate,
            "production": production,
            "archived": archived,
            "statuses": list(MODEL_LIBRARY_STATUSES),
        }

    def set_production(self, model_id: str) -> None:
        self._set_asset_status("model_id", model_id, "production")

    def archive(self, model_id: str) -> None:
        self._set_asset_status("model_id", model_id, "archived")

    def _set_asset_status(self, key: str, value: str, status: str) -> None:
        if key not in {"model_id", "model_run_id"}:
            raise ValueError(f"unsupported registry key: {key}")
        conn = _connect(self.db_path)
        row = conn.execute(f"SELECT metadata, status FROM models WHERE {key}=?", (value,)).fetchone()
        if not row:
            conn.close()
            return
        normalized_status = _normalise_library_status(status, existing_status=row["status"])
        metadata = _decode_metadata(row["metadata"])
        metadata["asset_status"] = normalized_status
        conn.execute(
            f"UPDATE models SET status=?, metadata=? WHERE {key}=?",
            (normalized_status, json.dumps(metadata, ensure_ascii=False), value),
        )
        conn.commit()
        conn.close()

    def synchronize_asset_status_metadata(self) -> dict:
        """Explicitly align metadata with the authoritative status column.

        This is intentionally not called from read paths. Operators may run it
        during a governed migration or closure pass and inspect the counts.
        """
        conn = _connect(self.db_path)
        rows = conn.execute("SELECT model_id, status, metadata FROM models").fetchall()
        changed = 0
        malformed = 0
        for row in rows:
            raw = row["metadata"] or "{}"
            metadata = _decode_metadata(raw)
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    malformed += 1
            except Exception:
                malformed += 1
            if metadata.get("asset_status") == row["status"]:
                continue
            metadata["asset_status"] = row["status"]
            conn.execute(
                "UPDATE models SET metadata=? WHERE model_id=?",
                (json.dumps(metadata, ensure_ascii=False), row["model_id"]),
            )
            changed += 1
        conn.commit()
        conn.close()
        return {"total": len(rows), "changed": changed, "malformed_metadata": malformed}
