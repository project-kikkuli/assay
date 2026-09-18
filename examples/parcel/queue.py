"""A small SQLite-backed leased job queue.

Claims use ``BEGIN IMMEDIATE`` so selection and fencing-token assignment are
one serialized operation across queue instances. The composite index supports
status/expiry filtering, but the OR predicate and global id ordering may require
a scan or a temporary sort. Inspect EXPLAIN QUERY PLAN and measured VM work;
do not assume logarithmic claim cost. A snapshot is O(n) and intentionally
returns every row.

The fencing token is required because an old worker can reconnect with the
same owner name after its lease has expired.  ``unsafe_fencing=True`` is a
deliberate fault-injection switch: it removes only the token check, allowing
that stale worker to complete a re-leased job.  This queue does not provide
exactly-once external effects and is not production-ready.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True)
class Lease:
    """The immutable facts a worker needs to process and complete a job."""

    job_id: int
    key: str
    payload: str
    owner: str
    token: int
    lease_until: int


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty text value")
    return value


def _payload(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("payload must be a text value")
    return value


class Queue:
    """A persistent queue whose leases are coordinated by SQLite locking."""

    def __init__(self, path: str, *, unsafe_fencing: bool = False):
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError("path must be a non-empty string")
        if not isinstance(unsafe_fencing, bool):
            raise ValueError("unsafe_fencing must be a boolean")
        self._connection = sqlite3.connect(path, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('queued', 'leased', 'done')),
                owner TEXT,
                token INTEGER NOT NULL DEFAULT 0,
                lease_until INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS jobs_claim_order
                ON jobs(status, lease_until, id);
            """
        )
        self._unsafe_fencing = unsafe_fencing
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("queue is closed")

    def enqueue(self, key: str, payload: str) -> int:
        """Insert a job, or return its id when the same key/payload exists."""
        self._ensure_open()
        key = _text(key, "key")
        payload = _payload(payload)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT id, payload FROM jobs WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                if row["payload"] != payload:
                    raise ValueError("key already exists with a different payload")
                self._connection.commit()
                return int(row["id"])
            cursor = self._connection.execute(
                "INSERT INTO jobs(key, payload, status) VALUES (?, ?, 'queued')",
                (key, payload),
            )
            self._connection.commit()
            return int(cursor.lastrowid)
        except Exception:
            self._connection.rollback()
            raise

    def claim(self, owner: str, now: int, lease_for: int = 10) -> Lease | None:
        """Atomically take the oldest available job and advance its token."""
        self._ensure_open()
        owner = _text(owner, "owner")
        now = _integer(now, "now")
        lease_for = _integer(lease_for, "lease_for")
        if lease_for <= 0:
            raise ValueError("lease_for must be positive")
        lease_until = now + lease_for
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                """
                SELECT id, key, payload, token FROM jobs
                WHERE status = 'queued' OR (status = 'leased' AND lease_until <= ?)
                ORDER BY id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                self._connection.commit()
                return None
            new_token = int(row["token"]) + 1
            self._connection.execute(
                """
                UPDATE jobs
                SET status = 'leased', owner = ?, token = ?, lease_until = ?
                WHERE id = ?
                """,
                (owner, new_token, lease_until, row["id"]),
            )
            self._connection.commit()
            return Lease(
                int(row["id"]), row["key"], row["payload"], owner, new_token, lease_until
            )
        except Exception:
            self._connection.rollback()
            raise

    def complete(self, job_id: int, owner: str, token: int, now: int) -> bool:
        """Complete only a live lease owned by ``owner`` and, normally, ``token``."""
        self._ensure_open()
        job_id = _integer(job_id, "job_id")
        owner = _text(owner, "owner")
        token = _integer(token, "token")
        now = _integer(now, "now")
        token_clause = "" if self._unsafe_fencing else " AND token = ?"
        parameters: tuple[object, ...] = (owner, now, job_id)
        if not self._unsafe_fencing:
            parameters = (owner, token, now, job_id)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                """
                UPDATE jobs SET status = 'done', owner = NULL, lease_until = 0
                WHERE owner = ?""" + token_clause + " AND status = 'leased' AND lease_until > ? AND id = ?",
                parameters,
            )
            self._connection.commit()
            return cursor.rowcount == 1
        except Exception:
            self._connection.rollback()
            raise

    def snapshot(self) -> list[dict]:
        """Return JSON-compatible rows in insertion order."""
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT id, key, payload, status, owner, token, lease_until FROM jobs ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> Queue:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()
