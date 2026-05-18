from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _loads_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


@dataclass
class SessionRecord:
    id: int
    client_id: str
    project_id: str
    client_type: str
    client_name: str | None
    instagram_username: str | None
    state: str
    attachments: list[str]
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass
class AuditJobRecord:
    id: int
    session_id: int
    status: str
    analysis: dict[str, Any] | None
    image_task_id: str | None
    image_url: str | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass
class AdminRecord:
    chat_id: str
    username: str | None
    created_at: str
    updated_at: str


class SQLiteRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def init_db(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    client_type TEXT NOT NULL,
                    client_name TEXT,
                    instagram_username TEXT,
                    state TEXT NOT NULL,
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    analysis_json TEXT,
                    image_task_id TEXT,
                    image_url TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );

                CREATE TABLE IF NOT EXISTS admins (
                    chat_id TEXT PRIMARY KEY,
                    username TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def start_session(
        self,
        *,
        client_id: str,
        project_id: str,
        client_type: str,
        client_name: str | None,
        instagram_username: str | None,
        state: str,
    ) -> SessionRecord:
        existing = self.get_session_by_client_id(client_id)
        now = utcnow_iso()
        with self._connect() as conn:
            if existing:
                conn.execute(
                    """
                    UPDATE sessions
                    SET project_id = ?, client_type = ?, client_name = ?, instagram_username = ?,
                        state = ?, attachments_json = '[]', last_error = NULL, updated_at = ?
                    WHERE client_id = ?
                    """,
                    (
                        project_id,
                        client_type,
                        client_name,
                        instagram_username,
                        state,
                        now,
                        client_id,
                    ),
                )
                conn.execute("DELETE FROM audit_jobs WHERE session_id = ?", (existing.id,))
            else:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        client_id, project_id, client_type, client_name, instagram_username,
                        state, attachments_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?)
                    """,
                    (
                        client_id,
                        project_id,
                        client_type,
                        client_name,
                        instagram_username,
                        state,
                        now,
                        now,
                    ),
                )
        return self.get_session_by_client_id(client_id)  # type: ignore[return-value]

    def get_session_by_client_id(self, client_id: str) -> SessionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        return self._row_to_session(row)

    def get_session_by_id(self, session_id: int) -> SessionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return self._row_to_session(row)

    def append_attachments(
        self,
        client_id: str,
        attachment_urls: list[str],
        *,
        max_images: int,
    ) -> SessionRecord:
        session = self.get_session_by_client_id(client_id)
        if not session:
            raise KeyError(f"Session not found for client_id={client_id}")
        merged = list(session.attachments)
        for url in attachment_urls:
            if url not in merged and len(merged) < max_images:
                merged.append(url)
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET attachments_json = ?, updated_at = ? WHERE client_id = ?",
                (json.dumps(merged, ensure_ascii=True), now, client_id),
            )
        return self.get_session_by_client_id(client_id)  # type: ignore[return-value]

    def update_session_identity(
        self,
        client_id: str,
        *,
        client_name: str | None = None,
        instagram_username: str | None = None,
    ) -> SessionRecord:
        session = self.get_session_by_client_id(client_id)
        if not session:
            raise KeyError(f"Session not found for client_id={client_id}")
        next_name = (client_name or "").strip() or session.client_name
        next_username = (instagram_username or "").strip() or session.instagram_username
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET client_name = ?, instagram_username = ?, updated_at = ?
                WHERE client_id = ?
                """,
                (next_name, next_username, now, client_id),
            )
        return self.get_session_by_client_id(client_id)  # type: ignore[return-value]

    def set_session_state(
        self,
        client_id: str,
        state: str,
        *,
        last_error: str | None = None,
    ) -> SessionRecord:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET state = ?, last_error = ?, updated_at = ? WHERE client_id = ?",
                (state, last_error, now, client_id),
            )
        session = self.get_session_by_client_id(client_id)
        if not session:
            raise KeyError(f"Session not found for client_id={client_id}")
        return session

    def create_job(self, session_id: int, status: str) -> AuditJobRecord:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute("DELETE FROM audit_jobs WHERE session_id = ?", (session_id,))
            cursor = conn.execute(
                """
                INSERT INTO audit_jobs (session_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, status, now, now),
            )
            job_id = cursor.lastrowid
        return self.get_job_by_id(job_id)  # type: ignore[arg-type, return-value]

    def get_job_by_id(self, job_id: int | None) -> AuditJobRecord | None:
        if job_id is None:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM audit_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._row_to_job(row)

    def get_job_by_session_id(self, session_id: int) -> AuditJobRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM audit_jobs
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self._row_to_job(row)

    def get_job_by_image_task_id(self, image_task_id: str) -> AuditJobRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM audit_jobs WHERE image_task_id = ?",
                (image_task_id,),
            ).fetchone()
        return self._row_to_job(row)

    def save_analysis(self, job_id: int, analysis: dict[str, Any]) -> AuditJobRecord:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE audit_jobs
                SET analysis_json = ?, status = ?, error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(analysis, ensure_ascii=True), "analyzed", now, job_id),
            )
        return self.get_job_by_id(job_id)  # type: ignore[return-value]

    def set_image_task(self, job_id: int, image_task_id: str) -> AuditJobRecord:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE audit_jobs
                SET image_task_id = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (image_task_id, "image_pending", now, job_id),
            )
        return self.get_job_by_id(job_id)  # type: ignore[return-value]

    def complete_job(
        self,
        job_id: int,
        *,
        status: str,
        image_url: str | None = None,
        error: str | None = None,
    ) -> AuditJobRecord:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE audit_jobs
                SET status = ?, image_url = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, image_url, error, now, job_id),
            )
        return self.get_job_by_id(job_id)  # type: ignore[return-value]

    def add_admin(self, chat_id: str, username: str | None) -> AdminRecord:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO admins (chat_id, username, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    username = excluded.username,
                    updated_at = excluded.updated_at
                """,
                (chat_id, username, now, now),
            )
        return self.get_admin(chat_id)  # type: ignore[return-value]

    def get_admin(self, chat_id: str) -> AdminRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM admins WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return self._row_to_admin(row)

    def list_admins(self) -> list[AdminRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM admins ORDER BY created_at ASC"
            ).fetchall()
        return [self._row_to_admin(row) for row in rows if row]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _row_to_session(row: sqlite3.Row | None) -> SessionRecord | None:
        if row is None:
            return None
        return SessionRecord(
            id=row["id"],
            client_id=row["client_id"],
            project_id=row["project_id"],
            client_type=row["client_type"],
            client_name=row["client_name"],
            instagram_username=row["instagram_username"],
            state=row["state"],
            attachments=_loads_json(row["attachments_json"], []),
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row | None) -> AuditJobRecord | None:
        if row is None:
            return None
        return AuditJobRecord(
            id=row["id"],
            session_id=row["session_id"],
            status=row["status"],
            analysis=_loads_json(row["analysis_json"], None),
            image_task_id=row["image_task_id"],
            image_url=row["image_url"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_admin(row: sqlite3.Row | None) -> AdminRecord | None:
        if row is None:
            return None
        return AdminRecord(
            chat_id=row["chat_id"],
            username=row["username"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
