import hashlib
import hmac
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import JobStatus


class Ledger:
    def __init__(self, path: Path, salt: bytes):
        self.path = Path(path)
        self.salt = bytes(salt)
        self._init()

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id INTEGER PRIMARY KEY,
                    source_fingerprint TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    reason_code TEXT,
                    sink_receipt_fingerprint TEXT
                )
                """
            )

    def _fingerprint(self, source_id):
        return hmac.new(self.salt, source_id.encode(), hashlib.sha256).hexdigest()

    def start(self, source_id, attempt=1):
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO jobs(source_fingerprint, attempt_number, status, started_at) VALUES (?, ?, ?, ?)",
                (self._fingerprint(source_id), attempt, "pending", now),
            )
            return cursor.lastrowid

    def finish(self, job_id, status, reason_code, sink_receipt_fingerprint=None):
        status_value = status.value if isinstance(status, JobStatus) else str(status)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, finished_at=?, reason_code=?, sink_receipt_fingerprint=? "
                "WHERE job_id=? AND status='pending'",
                (status_value, now, reason_code, sink_receipt_fingerprint, job_id),
            )

    def recover_pending(self):
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, finished_at=?, reason_code=? WHERE status='pending'",
                (JobStatus.CANCELLED.value, datetime.now(timezone.utc).isoformat(), "recovered_pending"),
            )

    def get(self, job_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source_fingerprint, attempt_number, status, started_at, finished_at, "
                "reason_code, sink_receipt_fingerprint FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            return dict(row) if row else None
