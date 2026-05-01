from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Iterator

from .schemas import (
    ExpenseFields,
    ExtractionTemplate,
    ExtractionPayload,
    PolicyReview,
    PortalState,
    SessionEvent,
    SessionSnapshot,
    WorkingMemory,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


class SessionStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    company_slug TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    current_step TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    receipt_image_path TEXT NOT NULL DEFAULT '',
                    extraction_payload TEXT NOT NULL DEFAULT '',
                    policy_payload TEXT NOT NULL DEFAULT '',
                    template_payload TEXT NOT NULL DEFAULT '',
                    memory_payload TEXT NOT NULL DEFAULT '',
                    reviewed_payload TEXT NOT NULL DEFAULT '',
                    portal_payload TEXT NOT NULL DEFAULT '',
                    error_text TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "policy_payload" not in columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN policy_payload TEXT NOT NULL DEFAULT ''"
                )
            if "company_slug" not in columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN company_slug TEXT NOT NULL DEFAULT ''"
                )
            if "template_payload" not in columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN template_payload TEXT NOT NULL DEFAULT ''"
                )
            if "memory_payload" not in columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN memory_payload TEXT NOT NULL DEFAULT ''"
                )

    def create_session(self, company_slug: str = "") -> str:
        session_id = uuid.uuid4().hex[:12]
        now = iso_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, company_slug, status, current_step, created_at, updated_at,
                    receipt_image_path, extraction_payload, policy_payload, template_payload, memory_payload, reviewed_payload, portal_payload, error_text
                ) VALUES (?, ?, ?, ?, ?, ?, '', '', '', '', '', '', ?, '')
                """,
                (
                    session_id,
                    company_slug,
                    "created",
                    "waiting_for_receipt",
                    now,
                    now,
                    PortalState(company_slug=company_slug).model_dump_json(),
                ),
            )
        self.append_event(session_id, "Session created. Ready for receipt capture.")
        return session_id

    def append_event(self, session_id: str, message: str, kind: str = "info") -> None:
        created_at = iso_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO events (session_id, created_at, kind, message) VALUES (?, ?, ?, ?)",
                (session_id, created_at, kind, message),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (created_at, session_id),
            )
        self._echo_event(created_at=created_at, session_id=session_id, kind=kind, message=message)

    def _echo_event(self, *, created_at: str, session_id: str, kind: str, message: str) -> None:
        prefix = f"[{created_at}][{kind.upper()}][{session_id}]"
        lines = [line.rstrip() for line in (message or "").splitlines() if line.strip()]
        print(prefix, file=sys.stdout, flush=True)
        for line in lines:
            print(f"  {line}", file=sys.stdout, flush=True)
        print("", file=sys.stdout, flush=True)

    def update_status(
        self,
        session_id: str,
        *,
        status: str,
        current_step: str,
        error_text: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET status = ?, current_step = ?, error_text = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (status, current_step, error_text or "", iso_now(), session_id),
            )

    def set_receipt_image(self, session_id: str, image_path: Path) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET receipt_image_path = ?, updated_at = ?, current_step = ?, status = ?
                WHERE session_id = ?
                """,
                (str(image_path), iso_now(), "receipt_uploaded", "processing_receipt", session_id),
            )

    def set_extraction(self, session_id: str, extraction: ExtractionPayload) -> None:
        reviewed = extraction.fields
        status = "needs_recapture" if extraction.retake_required else "ready_for_review"
        current_step = "retake_requested" if extraction.retake_required else "receipt_analyzed"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET extraction_payload = ?, reviewed_payload = ?, status = ?, current_step = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    extraction.model_dump_json(),
                    reviewed.model_dump_json(),
                    status,
                    current_step,
                    iso_now(),
                    session_id,
                ),
            )

    def set_policy_review(self, session_id: str, policy_review: PolicyReview) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET policy_payload = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (policy_review.model_dump_json(), iso_now(), session_id),
            )

    def save_extraction_template(self, session_id: str, extraction_template: ExtractionTemplate) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET template_payload = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (extraction_template.model_dump_json(), iso_now(), session_id),
            )

    def save_working_memory(self, session_id: str, working_memory: WorkingMemory) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET memory_payload = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (working_memory.model_dump_json(), iso_now(), session_id),
            )

    def save_review(self, session_id: str, reviewed_fields: ExpenseFields) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET reviewed_payload = ?, status = ?, current_step = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    reviewed_fields.model_dump_json(),
                    "ready_to_run",
                    "review_saved",
                    iso_now(),
                    session_id,
                ),
            )

    def update_portal_state(self, session_id: str, portal_state: PortalState) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET portal_payload = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (portal_state.model_dump_json(), iso_now(), session_id),
            )

    def get_session(self, session_id: str) -> SessionSnapshot:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)

            event_rows = connection.execute(
                "SELECT created_at, kind, message FROM events WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()

        extraction = (
            ExtractionPayload.model_validate_json(row["extraction_payload"])
            if row["extraction_payload"]
            else None
        )
        policy_review = (
            PolicyReview.model_validate_json(row["policy_payload"])
            if row["policy_payload"]
            else None
        )
        reviewed = (
            ExpenseFields.model_validate_json(row["reviewed_payload"])
            if row["reviewed_payload"]
            else ExpenseFields()
        )
        extraction_template = (
            ExtractionTemplate.model_validate_json(row["template_payload"])
            if row["template_payload"]
            else ExtractionTemplate()
        )
        working_memory = (
            WorkingMemory.model_validate_json(row["memory_payload"])
            if row["memory_payload"]
            else WorkingMemory(company_slug=row["company_slug"], receipt_image_path=row["receipt_image_path"])
        )
        portal = (
            PortalState.model_validate_json(row["portal_payload"])
            if row["portal_payload"]
            else PortalState(company_slug=row["company_slug"])
        )
        events = [
            SessionEvent(
                created_at=datetime.fromisoformat(event["created_at"]),
                kind=event["kind"],
                message=event["message"],
            )
            for event in event_rows
        ]

        return SessionSnapshot(
            session_id=row["session_id"],
            company_slug=row["company_slug"],
            status=row["status"],
            current_step=row["current_step"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            receipt_image_path=row["receipt_image_path"],
            extraction=extraction,
            policy_review=policy_review,
            extraction_template=extraction_template,
            working_memory=working_memory,
            reviewed_fields=reviewed,
            portal_state=portal,
            events=events,
            error_text=row["error_text"],
        )
