"""SQLite 持久层：浏览器会话 + 导出任务。无账户体系，sid 是随机 cookie。

表里不存任何项目内容/评论/附件 —— 只有任务进度、产物路径与加密后的
OAuth token（任务终态即清列）。这是「不保留项目内容」承诺的落地。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    oauth_state   TEXT,
    asana_gid     TEXT,
    asana_name    TEXT,
    asana_email   TEXT,
    token_enc     TEXT,
    refresh_enc   TEXT,
    token_expires_at TEXT,
    job_id        TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    status          TEXT NOT NULL,
    options_json    TEXT NOT NULL,
    projects_json   TEXT NOT NULL,
    total_tasks     INTEGER NOT NULL DEFAULT 0,
    processed_tasks INTEGER NOT NULL DEFAULT 0,
    token_enc       TEXT,
    refresh_enc     TEXT,
    token_expires_at TEXT,
    error           TEXT,
    zip_path        TEXT,
    download_token  TEXT,
    expires_at      TEXT,
    stats_json      TEXT,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id);
"""

JOB_PENDING = "pending"
JOB_PROCESSING = "processing"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
TERMINAL_STATUSES = {JOB_COMPLETED, JOB_FAILED}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class Store:
    """sqlite3 默认禁止跨线程使用；导出跑在后台线程，这里用一把锁串行化。"""

    def __init__(self, db_path: Path | str) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------- sessions ----------

    def create_session(self) -> dict[str, Any]:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)",
                (sid, iso(utcnow())),
            )
            self._conn.commit()
        return {"id": sid}

    def get_session(self, sid: str | None) -> dict[str, Any] | None:
        if not sid:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        return dict(row) if row else None

    def update_session(self, sid: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*fields.values(), sid))
            self._conn.commit()

    def delete_session(self, sid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            self._conn.commit()

    def purge_stale_sessions(self, older_than: datetime) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE created_at < ?", (iso(older_than),))
            self._conn.commit()
            return cur.rowcount

    # ---------- jobs ----------

    def create_job(
        self,
        session_id: str,
        projects: list[dict[str, Any]],
        options: dict[str, Any],
        token_enc: str | None,
        refresh_enc: str | None,
        token_expires_at: str | None,
    ) -> dict[str, Any]:
        job_id = secrets.token_urlsafe(12)
        now = iso(utcnow())
        with self._lock:
            # 一次性服务：一个会话同时只保留一个任务，旧任务先清场
            self._conn.execute(
                "DELETE FROM jobs WHERE session_id = ? AND status IN (?, ?)",
                (session_id, JOB_PENDING, JOB_PROCESSING),
            )
            self._conn.execute(
                """INSERT INTO jobs
                   (id, session_id, status, options_json, projects_json,
                    token_enc, refresh_enc, token_expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    session_id,
                    JOB_PENDING,
                    json.dumps(options, ensure_ascii=False),
                    json.dumps(projects, ensure_ascii=False),
                    token_enc,
                    refresh_enc,
                    token_expires_at,
                    now,
                ),
            )
            self._conn.execute("UPDATE sessions SET job_id = ? WHERE id = ?", (job_id, session_id))
            self._conn.commit()
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_session_active_job(self, sid: str | None) -> dict[str, Any] | None:
        if not sid:
            return None
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM jobs WHERE session_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (sid,),
            ).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
            self._conn.commit()

    def update_job_project(self, job_id: str, project_gid: str, attrs: dict[str, Any]) -> None:
        """更新单个项目批次的进度（对应 bridge 的 updateBatch）。"""
        with self._lock:
            row = self._conn.execute("SELECT projects_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return
            projects = json.loads(row["projects_json"])
            for p in projects:
                if p["gid"] == project_gid:
                    p.update(attrs)
                    break
            total = sum(p.get("total", 0) for p in projects)
            processed = sum(p.get("processed", 0) for p in projects)
            self._conn.execute(
                "UPDATE jobs SET projects_json = ?, total_tasks = ?, processed_tasks = ? WHERE id = ?",
                (json.dumps(projects, ensure_ascii=False), total, processed, job_id),
            )
            self._conn.commit()

    def wipe_job_tokens(self, job_id: str) -> None:
        """任务终态后立即抹掉 token 密文（隐私承诺）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET token_enc = NULL, refresh_enc = NULL, token_expires_at = NULL WHERE id = ?",
                (job_id,),
            )
            self._conn.commit()

    def jobs_with_live_tokens(self) -> Iterable[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs WHERE token_enc IS NOT NULL").fetchall()
        for row in rows:
            yield dict(row)

    def expired_completed_jobs(self, now: datetime) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM jobs
                   WHERE status = ? AND expires_at IS NOT NULL AND expires_at < ?""",
                (JOB_COMPLETED, iso(now)),
            ).fetchall()
        return [dict(r) for r in rows]

    def stale_jobs(self, older_than: datetime) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs WHERE created_at < ?", (iso(older_than),)).fetchall()
        return [dict(r) for r in rows]

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def expiry_from(now: datetime, ttl_hours: float) -> datetime:
    return now + timedelta(hours=ttl_hours)
