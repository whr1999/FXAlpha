from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .contracts import utc_now
from .paths import MODEL_JOBS_DB, ensure_model_dirs


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    mode TEXT DEFAULT '',
    current_round_group_id TEXT DEFAULT '',
    payload_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    heartbeat_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    mode TEXT DEFAULT '',
    feature_set_id TEXT DEFAULT '',
    n_rounds_requested INTEGER DEFAULT 0,
    n_rounds_completed INTEGER DEFAULT 0,
    active_job_id TEXT DEFAULT '',
    parent_job_id TEXT DEFAULT '',
    current_stage TEXT DEFAULT '',
    current_blocker_json TEXT DEFAULT '{}',
    round_group_ids_json TEXT DEFAULT '[]',
    model_run_ids_json TEXT DEFAULT '[]',
    blocker_history_json TEXT DEFAULT '[]',
    payload_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    round_group_id TEXT PRIMARY KEY,
    feature_set_id TEXT NOT NULL,
    experiment_signature TEXT NOT NULL,
    seed_set_json TEXT NOT NULL,
    seed_policy_json TEXT NOT NULL,
    experiment_json TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seed_runs (
    model_run_id TEXT PRIMARY KEY,
    round_group_id TEXT NOT NULL,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL,
    metrics_json TEXT DEFAULT '{}',
    score_json TEXT DEFAULT '{}',
    validation_json TEXT DEFAULT '{}',
    forward_json TEXT DEFAULT '{}',
    gate_json TEXT DEFAULT '{}',
    registry_status TEXT DEFAULT '',
    registry_model_id TEXT DEFAULT '',
    artifact_dir TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_seed_runs_round ON seed_runs(round_group_id);
CREATE INDEX IF NOT EXISTS idx_model_seed_runs_updated ON seed_runs(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_sessions_updated ON sessions(updated_at DESC);
"""


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return default


class ModelStateStore:
    def __init__(self, db_path: Path | None = None, runtime_root: Path | None = None):
        paths = ensure_model_dirs(runtime_root)
        self.runtime_root = paths["runtime_root"]
        self.db_path = db_path or (self.runtime_root / "jobs.sqlite" if runtime_root else MODEL_JOBS_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        self._migrate(conn)
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(seed_runs)").fetchall()}
        if "forward_json" not in cols:
            conn.execute("ALTER TABLE seed_runs ADD COLUMN forward_json TEXT DEFAULT '{}'")
        if "validation_json" not in cols:
            conn.execute("ALTER TABLE seed_runs ADD COLUMN validation_json TEXT DEFAULT '{}'")

    def _init(self) -> None:
        conn = self._connect()
        conn.commit()
        conn.close()

    def upsert_job(self, job_id: str, *, status: str, stage: str, mode: str = "", current_round_group_id: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        now = utc_now()
        conn = self._connect()
        existing = conn.execute("SELECT created_at,payload_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        merged_payload = _loads(existing["payload_json"], {}) if existing else {}
        merged_payload.update(payload or {})
        conn.execute(
            """INSERT INTO jobs(job_id,status,stage,mode,current_round_group_id,payload_json,created_at,updated_at,heartbeat_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 status=excluded.status, stage=excluded.stage, mode=excluded.mode,
                 current_round_group_id=excluded.current_round_group_id,
                 payload_json=excluded.payload_json, updated_at=excluded.updated_at,
                 heartbeat_at=excluded.heartbeat_at""",
            (
                job_id,
                status,
                stage,
                mode,
                current_round_group_id,
                json.dumps(merged_payload, ensure_ascii=False),
                existing["created_at"] if existing else now,
                now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        return self.get_job(job_id) or {}

    def active_managed_job(self) -> dict[str, Any] | None:
        """Return the one API-managed model job that still owns the global slot."""

        for job in self.list_jobs(limit=100):
            if job.get("status") not in {"queued", "running", "stopping"}:
                continue
            if bool((job.get("payload") or {}).get("async_job")):
                return job
        return None

    def claim_managed_job(
        self,
        job_id: str,
        *,
        mode: str,
        stage: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Atomically claim the single model-training slot.

        Legacy rows are deliberately ignored: only jobs created by the async
        launcher carry ``async_job=true`` and participate in this lock.
        """

        now = utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running','stopping') ORDER BY updated_at DESC"
            ).fetchall()
            for row in rows:
                row_payload = _loads(row["payload_json"], {})
                if bool(row_payload.get("async_job")):
                    conn.rollback()
                    data = dict(row)
                    data["payload"] = row_payload
                    data.pop("payload_json", None)
                    return False, data
            claim_payload = dict(payload or {})
            claim_payload["async_job"] = True
            conn.execute(
                """INSERT INTO jobs(job_id,status,stage,mode,current_round_group_id,payload_json,created_at,updated_at,heartbeat_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    "queued",
                    stage,
                    mode,
                    "",
                    json.dumps(claim_payload, ensure_ascii=False),
                    now,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True, self.get_job(job_id) or {}

    def request_job_stop(self, job_id: str) -> dict[str, Any] | None:
        job = self.get_job(job_id)
        if not job:
            return None
        if job.get("status") not in {"queued", "running", "stopping"}:
            return job
        return self.upsert_job(
            job_id,
            status="stopping",
            stage=str(job.get("stage") or "stopping"),
            mode=str(job.get("mode") or "orch"),
            current_round_group_id=str(job.get("current_round_group_id") or ""),
            payload={"cancel_requested": True, "cancel_requested_at": utc_now()},
        )

    def job_stop_requested(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return bool(job and (job.get("payload") or {}).get("cancel_requested"))

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        conn.close()
        if not row:
            return None
        data = dict(row)
        data["payload"] = _loads(data.pop("payload_json", "{}"), {})
        return data

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        conn.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["payload"] = _loads(data.pop("payload_json", "{}"), {})
            out.append(data)
        return out

    def upsert_session(self, session_payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        session_id = session_payload["session_id"]
        conn = self._connect()
        existing = conn.execute("SELECT created_at FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        conn.execute(
            """INSERT INTO sessions(
                 session_id,status,mode,feature_set_id,n_rounds_requested,n_rounds_completed,
                 active_job_id,parent_job_id,current_stage,current_blocker_json,
                 round_group_ids_json,model_run_ids_json,blocker_history_json,payload_json,created_at,updated_at
               )
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 status=excluded.status,
                 mode=excluded.mode,
                 feature_set_id=excluded.feature_set_id,
                 n_rounds_requested=excluded.n_rounds_requested,
                 n_rounds_completed=excluded.n_rounds_completed,
                 active_job_id=excluded.active_job_id,
                 parent_job_id=excluded.parent_job_id,
                 current_stage=excluded.current_stage,
                 current_blocker_json=excluded.current_blocker_json,
                 round_group_ids_json=excluded.round_group_ids_json,
                 model_run_ids_json=excluded.model_run_ids_json,
                 blocker_history_json=excluded.blocker_history_json,
                 payload_json=excluded.payload_json,
                 updated_at=excluded.updated_at""",
            (
                session_id,
                session_payload.get("status", "running"),
                session_payload.get("mode", "orch"),
                session_payload.get("feature_set_id", ""),
                int(session_payload.get("n_rounds_requested") or 0),
                int(session_payload.get("n_rounds_completed") or 0),
                session_payload.get("active_job_id", ""),
                session_payload.get("parent_job_id", ""),
                session_payload.get("current_stage", ""),
                json.dumps(session_payload.get("current_blocker") or {}, ensure_ascii=False),
                json.dumps(session_payload.get("round_group_ids") or [], ensure_ascii=False),
                json.dumps(session_payload.get("model_run_ids") or [], ensure_ascii=False),
                json.dumps(session_payload.get("blocker_history") or [], ensure_ascii=False),
                json.dumps(session_payload.get("payload") or {}, ensure_ascii=False),
                existing["created_at"] if existing else session_payload.get("created_at") or now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        return self.get_session(session_id) or {}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        conn.close()
        if not row:
            return None
        return self._session_row_to_dict(row)

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        conn.close()
        return [self._session_row_to_dict(row) for row in rows]

    def upsert_round(self, round_payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        created = round_payload.get("created_at") or now
        updated = round_payload.get("updated_at") or now
        conn = self._connect()
        conn.execute(
            """INSERT INTO rounds(round_group_id,feature_set_id,experiment_signature,seed_set_json,seed_policy_json,experiment_json,status,stage,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(round_group_id) DO UPDATE SET
                 feature_set_id=excluded.feature_set_id,
                 experiment_signature=excluded.experiment_signature,
                 seed_set_json=excluded.seed_set_json,
                 seed_policy_json=excluded.seed_policy_json,
                 experiment_json=excluded.experiment_json,
                 status=excluded.status,
                 stage=excluded.stage,
                 updated_at=excluded.updated_at""",
            (
                round_payload["round_group_id"],
                round_payload.get("feature_set_id", ""),
                round_payload.get("experiment_signature", ""),
                json.dumps(round_payload.get("seed_set") or [], ensure_ascii=False),
                json.dumps(round_payload.get("seed_policy") or {}, ensure_ascii=False),
                json.dumps(round_payload.get("experiment") or {}, ensure_ascii=False),
                round_payload.get("status", "queued"),
                round_payload.get("stage", "experiment_plan"),
                created,
                updated,
            ),
        )
        conn.commit()
        conn.close()
        return self.get_round(round_payload["round_group_id"]) or {}

    def get_round(self, round_group_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM rounds WHERE round_group_id=?", (round_group_id,)).fetchone()
        conn.close()
        if not row:
            return None
        data = dict(row)
        data["seed_set"] = _loads(data.pop("seed_set_json", "[]"), [])
        data["seed_policy"] = _loads(data.pop("seed_policy_json", "{}"), {})
        data["experiment"] = _loads(data.pop("experiment_json", "{}"), {})
        return data

    def list_rounds(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM rounds ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        conn.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["seed_set"] = _loads(data.pop("seed_set_json", "[]"), [])
            data["seed_policy"] = _loads(data.pop("seed_policy_json", "{}"), {})
            data["experiment"] = _loads(data.pop("experiment_json", "{}"), {})
            data["seed_runs"] = self.list_seed_runs(round_group_id=data["round_group_id"])
            out.append(data)
        return out

    def upsert_seed_run(self, seed_payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        created = seed_payload.get("created_at") or now
        updated = seed_payload.get("updated_at") or now
        conn = self._connect()
        conn.execute(
            """INSERT INTO seed_runs(model_run_id,round_group_id,seed,status,metrics_json,score_json,validation_json,forward_json,gate_json,registry_status,registry_model_id,artifact_dir,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(model_run_id) DO UPDATE SET
                 status=excluded.status,
                 metrics_json=excluded.metrics_json,
                 score_json=excluded.score_json,
                 validation_json=excluded.validation_json,
                 forward_json=excluded.forward_json,
                 gate_json=excluded.gate_json,
                 registry_status=excluded.registry_status,
                 registry_model_id=excluded.registry_model_id,
                 artifact_dir=excluded.artifact_dir,
                 updated_at=excluded.updated_at""",
            (
                seed_payload["model_run_id"],
                seed_payload["round_group_id"],
                int(seed_payload["seed"]),
                seed_payload.get("status", "queued"),
                json.dumps(seed_payload.get("metrics") or {}, ensure_ascii=False),
                json.dumps(seed_payload.get("score") or {}, ensure_ascii=False),
                json.dumps(seed_payload.get("validation") or {}, ensure_ascii=False),
                json.dumps(seed_payload.get("forward") or {}, ensure_ascii=False),
                json.dumps(seed_payload.get("gate") or {}, ensure_ascii=False),
                seed_payload.get("registry_status", ""),
                seed_payload.get("registry_model_id", ""),
                seed_payload.get("artifact_dir", ""),
                created,
                updated,
            ),
        )
        conn.commit()
        conn.close()
        return self.get_seed_run(seed_payload["model_run_id"]) or {}

    def get_seed_run(self, model_run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM seed_runs WHERE model_run_id=?", (model_run_id,)).fetchone()
        conn.close()
        if not row:
            return None
        return self._seed_row_to_dict(row)

    def list_seed_runs(self, round_group_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        conn = self._connect()
        if round_group_id:
            rows = conn.execute(
                "SELECT * FROM seed_runs WHERE round_group_id=? ORDER BY seed ASC",
                (round_group_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM seed_runs ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        conn.close()
        return [self._seed_row_to_dict(row) for row in rows]

    @staticmethod
    def _seed_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["metrics"] = _loads(data.pop("metrics_json", "{}"), {})
        data["score"] = _loads(data.pop("score_json", "{}"), {})
        data["validation"] = _loads(data.pop("validation_json", "{}"), {})
        data["forward"] = _loads(data.pop("forward_json", "{}"), {})
        data["gate"] = _loads(data.pop("gate_json", "{}"), {})
        return data

    @staticmethod
    def _session_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["current_blocker"] = _loads(data.pop("current_blocker_json", "{}"), {})
        data["round_group_ids"] = _loads(data.pop("round_group_ids_json", "[]"), [])
        data["model_run_ids"] = _loads(data.pop("model_run_ids_json", "[]"), [])
        data["blocker_history"] = _loads(data.pop("blocker_history_json", "[]"), [])
        data["payload"] = _loads(data.pop("payload_json", "{}"), {})
        return data


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": utc_now(), **payload}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path, *, limit: int = 100, include_payload: bool = True) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    selected_limit = max(int(limit), 0)
    if selected_limit == 0:
        return []
    # Runtime traces can be hundreds of megabytes. A status request only needs
    # their tail, so avoid decoding and splitting the entire append-only file.
    # Work in bytes until the final complete lines are selected to preserve
    # UTF-8 characters that may straddle a read-block boundary.
    chunks: list[bytes] = []
    newline_count = 0
    with path.open("rb") as fh:
        fh.seek(0, 2)
        remaining = fh.tell()
        while remaining > 0 and newline_count <= selected_limit:
            read_size = min(64 * 1024, remaining)
            remaining -= read_size
            fh.seek(remaining)
            chunk = fh.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    raw_lines = b"".join(reversed(chunks)).splitlines()[-selected_limit:]
    lines = [line.decode("utf-8") for line in raw_lines]
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not include_payload:
            for key in ("context_pack", "system_prompt", "stage_briefing", "submitted_payload", "payload"):
                if key in row:
                    row[key] = {"omitted": True}
            output_contract = row.get("output_contract")
            if isinstance(output_contract, dict) and "llm_payload" in output_contract:
                output_contract["llm_payload"] = {"omitted": True}
            parsed_response = row.get("parsed_response")
            if isinstance(parsed_response, dict) and "experiment_json" in parsed_response:
                parsed_response["experiment_json"] = {"omitted": True}
        rows.append(row)
    return rows
