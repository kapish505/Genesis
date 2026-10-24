"""Durable per-agent gateway quotas.

Counters live in SQLite so a gateway restart does not reset a caller's window.
The process holds a lock around each read-modify-write; a single gateway
instance is the supported deployment, and the schema makes double-increment
impossible within that instance.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class QuotaExceeded(RuntimeError):
    pass


class Quotas:
    def __init__(self, path: str = "./gateway_quotas.db", daily_limit: int = 1000) -> None:
        self.daily_limit = daily_limit
        self.path = path
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS quota (
                    agent_id TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(agent_id, day)
                )"""
            )
            db.execute("DELETE FROM quota WHERE day < ?", (int(time.time() // 86400) - 7,))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def remaining(self, agent_id: str) -> int:
        day = int(time.time() // 86400)
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT count FROM quota WHERE agent_id=? AND day=?", (agent_id, day)
            ).fetchone()
            return max(0, self.daily_limit - int(row[0] if row else 0))

    def consume(self, agent_id: str, units: int = 1) -> int:
        """Consume ``units``; returns the remaining allowance after consumption."""
        day = int(time.time() // 86400)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO quota(agent_id, day, count) VALUES (?, ?, 0)",
                (agent_id, day),
            )
            row = db.execute(
                "SELECT count FROM quota WHERE agent_id=? AND day=?", (agent_id, day)
            ).fetchone()
            current = int(row[0]) if row else 0
            if current + units > self.daily_limit:
                raise QuotaExceeded(
                    f"daily gateway quota of {self.daily_limit} requests is exhausted for this agent"
                )
            db.execute(
                "UPDATE quota SET count=count+? WHERE agent_id=? AND day=?",
                (units, agent_id, day),
            )
            return self.daily_limit - (current + units)
