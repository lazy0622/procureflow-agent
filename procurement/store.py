"""Local business state and an independent synthetic ERP draft ledger."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class IntentConflict(ValueError):
    """A purchase reference was reused for a different payload."""


class Store:
    """Persist jobs, stage events and synthetic ERP records in SQLite."""

    def __init__(self, path: Path) -> None:
        """Initialize the local schema without touching upstream databases."""
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    reference TEXT PRIMARY KEY, request TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, reference TEXT NOT NULL,
                    stage TEXT NOT NULL, detail TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY, reference TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Provide a bounded connection and commit or roll back atomically."""
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, reference: str, request: dict[str, Any]) -> bool:
        """Atomically admit an intent; equivalent retries reuse the same job."""
        payload = json.dumps(request, sort_keys=True, ensure_ascii=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT request FROM jobs WHERE reference=?", (reference,)
            ).fetchone()
            if existing:
                if existing["request"] != payload:
                    raise IntentConflict("Reference already belongs to another purchase intent")
                return False
            db.execute(
                "INSERT INTO jobs(reference,request,status) VALUES(?,?,?)",
                (reference, payload, "queued"),
            )
        return True

    def event(self, reference: str, stage: str, detail: dict[str, Any]) -> None:
        """Append business-level progress without provider credentials."""
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(reference,stage,detail) VALUES(?,?,?)",
                (reference, stage, json.dumps(detail, ensure_ascii=False)),
            )

    def finish(self, reference: str, status: str, result: dict[str, Any]) -> None:
        """Record a terminal or in-progress business outcome."""
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?,result=? WHERE reference=?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False),
                    reference,
                ),
            )

    def job(self, reference: str) -> dict[str, Any] | None:
        """Read a job and its ordered business events."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE reference=?", (reference,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["request"] = json.loads(result["request"])
            result["result"] = json.loads(result["result"]) if result["result"] else None
            result["events"] = [
                {**dict(event), "detail": json.loads(event["detail"])}
                for event in db.execute(
                    "SELECT * FROM events WHERE reference=? ORDER BY id", (reference,)
                )
            ]
            return result

    def create_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Simulate an ERP with a unique purchase-reference constraint."""
        reference = payload["reference"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM drafts WHERE reference=?", (reference,)).fetchone()
            if row:
                if json.loads(row["payload"]) != payload:
                    raise IntentConflict("An existing draft has different business fields")
                return {"id": row["id"], **payload, "reused": True}
            cursor = db.execute(
                "INSERT INTO drafts(reference,payload) VALUES(?,?)",
                (reference, json.dumps(payload, ensure_ascii=False)),
            )
            return {"id": cursor.lastrowid, **payload, "reused": False}

    def draft(self, reference: str) -> dict[str, Any] | None:
        """Read the actual synthetic ERP record by business reference."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM drafts WHERE reference=?", (reference,)).fetchone()
            return {"id": row["id"], **json.loads(row["payload"])} if row else None

    def interrupt_unfinished(self) -> int:
        """Mark restart-interrupted tasks explicitly; never claim automatic recovery."""
        with self.connect() as db:
            return db.execute(
                "UPDATE jobs SET status='interrupted' WHERE status IN "
                "('queued','collecting','creating_draft')"
            ).rowcount

    def requeue(self, reference: str) -> bool:
        """Atomically admit a manual retry for a stopped or failed job."""
        with self.connect() as db:
            return (
                db.execute(
                    "UPDATE jobs SET status='queued' WHERE reference=? "
                    "AND status IN ('failed','interrupted')",
                    (reference,),
                ).rowcount
                == 1
            )
