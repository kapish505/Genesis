"""SQLite persistence for sessions, builds, agents, and quotas.

One aiosqlite connection is shared per process and guarded by an asyncio lock.
WAL mode is enabled so the sweeper and the request path can read concurrently.
Every saga transition is committed before the corresponding side effect runs,
which is what makes crash resumption possible.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

ACTIVE_BUILD_STATES = (
    "PLANNED",
    "APPROVED",
    "CREATED",
    "CODE_UPLOADED",
    "STARTED",
    "PROBE_OK",
    "DISCOVERABLE",
)
TERMINAL_BUILD_STATES = ("DONE", "FAILED", "COMPENSATED")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Ledger:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        schema = (Path(__file__).parent / "db" / "schema.sql").read_text()
        await self._db.executescript(schema)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Ledger.initialize() has not run")
        return self._db

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[aiosqlite.Connection]:
        db = self._connection()
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # ---------------------------------------------------------------- dedupe

    async def mark_message(self, msg_id: str, window: int) -> bool:
        """Return False when this msg_id was already handled."""
        db = self._connection()
        async with self._write():
            cursor = await db.execute(
                "INSERT OR IGNORE INTO message_ids(msg_id, seen_at) VALUES (?, ?)",
                (msg_id, time.time()),
            )
            inserted = cursor.rowcount == 1
            await db.execute(
                "DELETE FROM message_ids WHERE msg_id IN "
                "(SELECT msg_id FROM message_ids ORDER BY seen_at DESC LIMIT -1 OFFSET ?)",
                (window,),
            )
        return inserted

    # --------------------------------------------------------------- sessions

    async def save_session(self, row: dict[str, Any]) -> None:
        db = self._connection()
        async with self._write():
            await db.execute(
                """INSERT INTO sessions(session_id, sender_address, external_session_id, state,
                    data_json, pending_action, pending_actions_json, pending_since, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET state=excluded.state,
                    data_json=excluded.data_json, pending_action=excluded.pending_action,
                    pending_actions_json=excluded.pending_actions_json,
                    pending_since=excluded.pending_since, updated_at=excluded.updated_at""",
                (
                    row["session_id"],
                    row["sender"],
                    row.get("external_id"),
                    row["state"],
                    json.dumps(row.get("data") or {}, separators=(",", ":"), default=str),
                    row.get("pending_action"),
                    json.dumps(list(row.get("pending_actions") or [])),
                    float(row.get("pending_since") or 0.0),
                    float(row["created_at"]),
                    float(row["updated_at"]),
                ),
            )

    async def load_session(self, sender: str, external_id: str | None) -> dict[str, Any] | None:
        db = self._connection()
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE sender_address=? AND external_session_id IS ?",
            (sender, external_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["data"] = json.loads(data.get("data_json") or "{}")
        except json.JSONDecodeError:
            data["data"] = {}
        try:
            data["pending_actions"] = json.loads(data.get("pending_actions_json") or "[]")
        except json.JSONDecodeError:
            data["pending_actions"] = []
        data["sender"] = data["sender_address"]
        data["external_id"] = data["external_session_id"]
        return data

    async def purge_sessions(self, cutoff: float) -> int:
        db = self._connection()
        async with self._write():
            cursor = await db.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        return cursor.rowcount

    # ----------------------------------------------------------------- specs

    async def save_spec(self, digest: str, spec: dict[str, Any]) -> None:
        db = self._connection()
        async with self._write():
            await db.execute(
                "INSERT OR REPLACE INTO specs(spec_hash, spec_json, created_at) VALUES (?, ?, ?)",
                (digest, json.dumps(spec, sort_keys=True), now_iso()),
            )

    async def load_spec(self, digest: str) -> dict[str, Any] | None:
        db = self._connection()
        cursor = await db.execute("SELECT spec_json FROM specs WHERE spec_hash=?", (digest,))
        row = await cursor.fetchone()
        return json.loads(row["spec_json"]) if row else None

    # ---------------------------------------------------------------- builds

    async def create_build(self, build_id: str, digest: str, session_id: str) -> None:
        db = self._connection()
        async with self._write():
            await db.execute(
                """INSERT INTO builds(build_id, spec_hash, session_id, state, created_at, updated_at)
                   VALUES (?, ?, ?, 'PLANNED', ?, ?)""",
                (build_id, digest, session_id, now_iso(), now_iso()),
            )
            await db.execute(
                "INSERT INTO build_events(build_id, state, created_at) VALUES (?, 'PLANNED', ?)",
                (build_id, now_iso()),
            )

    async def transition_build(
        self,
        build_id: str,
        state: str,
        *,
        address: str | None = None,
        handle: str | None = None,
        code_hash: str | None = None,
        failure: tuple[str, str] | None = None,
        detail: str = "",
    ) -> None:
        db = self._connection()
        async with self._write():
            await db.execute(
                """UPDATE builds SET state=?, agent_address=COALESCE(?, agent_address),
                    handle=COALESCE(?, handle), code_hash=COALESCE(?, code_hash),
                    failure_stage=COALESCE(?, failure_stage),
                    failure_reason=COALESCE(?, failure_reason),
                    attempts=attempts + 1, updated_at=?
                   WHERE build_id=?""",
                (
                    state,
                    address,
                    handle,
                    code_hash,
                    failure[0] if failure else None,
                    failure[1] if failure else None,
                    now_iso(),
                    build_id,
                ),
            )
            await db.execute(
                "INSERT INTO build_events(build_id, state, detail, created_at) VALUES (?, ?, ?, ?)",
                (build_id, state, detail or (failure[1] if failure else None), now_iso()),
            )

    async def get_build(self, build_id: str) -> dict[str, Any] | None:
        db = self._connection()
        cursor = await db.execute("SELECT * FROM builds WHERE build_id=?", (build_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def build_for_spec(self, digest: str) -> dict[str, Any] | None:
        db = self._connection()
        cursor = await db.execute(
            "SELECT * FROM builds WHERE spec_hash=? ORDER BY updated_at DESC LIMIT 1", (digest,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def active_build_for_spec(self, digest: str) -> dict[str, Any] | None:
        db = self._connection()
        placeholders = ",".join("?" for _ in ACTIVE_BUILD_STATES)
        cursor = await db.execute(
            f"SELECT * FROM builds WHERE spec_hash=? AND state IN ({placeholders}) "
            "ORDER BY updated_at DESC LIMIT 1",
            (digest, *ACTIVE_BUILD_STATES),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def resumable_builds(self, older_than_iso: str) -> list[dict[str, Any]]:
        db = self._connection()
        placeholders = ",".join("?" for _ in ACTIVE_BUILD_STATES)
        cursor = await db.execute(
            f"SELECT * FROM builds WHERE state IN ({placeholders}) AND updated_at < ? "
            "ORDER BY updated_at ASC",
            (*ACTIVE_BUILD_STATES, older_than_iso),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def event_count(self, build_id: str) -> int:
        db = self._connection()
        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM build_events WHERE build_id=?", (build_id,)
        )
        row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    # ---------------------------------------------------------------- agents

    async def upsert_agent(
        self,
        digest: str,
        address: str,
        title: str,
        status: str,
        handle: str | None = None,
        token_id: str | None = None,
    ) -> None:
        db = self._connection()
        async with self._write():
            await db.execute(
                """INSERT INTO agents(spec_hash, agent_address, handle, title, status, gateway_token_id,
                    created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(spec_hash) DO UPDATE SET agent_address=excluded.agent_address,
                    handle=COALESCE(excluded.handle, agents.handle), status=excluded.status,
                    gateway_token_id=COALESCE(excluded.gateway_token_id, agents.gateway_token_id),
                    updated_at=excluded.updated_at""",
                (digest, address, handle, title, status, token_id, now_iso(), now_iso()),
            )

    async def get_agent(self, digest: str) -> dict[str, Any] | None:
        db = self._connection()
        cursor = await db.execute("SELECT * FROM agents WHERE spec_hash=?", (digest,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_agents(self, status: str | None = None) -> list[dict[str, Any]]:
        db = self._connection()
        if status:
            cursor = await db.execute(
                "SELECT * FROM agents WHERE status=? ORDER BY created_at DESC", (status,)
            )
        else:
            cursor = await db.execute("SELECT * FROM agents ORDER BY created_at DESC")
        return [dict(row) for row in await cursor.fetchall()]

    async def count_live_agents(self) -> int:
        db = self._connection()
        cursor = await db.execute("SELECT COUNT(*) AS n FROM agents WHERE status='live'")
        row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    async def touch_agent(self, digest: str) -> None:
        db = self._connection()
        async with self._write():
            await db.execute(
                "UPDATE agents SET last_used_at=?, updated_at=? WHERE spec_hash=?",
                (now_iso(), now_iso(), digest),
            )

    async def set_agent_status(self, digest: str, status: str) -> None:
        db = self._connection()
        async with self._write():
            await db.execute(
                "UPDATE agents SET status=?, updated_at=? WHERE spec_hash=?",
                (status, now_iso(), digest),
            )

    async def set_runtime_state(
        self, digest: str, state: dict[str, Any], failures: int | None = None
    ) -> None:
        db = self._connection()
        async with self._write():
            if failures is None:
                await db.execute(
                    "UPDATE agents SET runtime_state_json=?, updated_at=? WHERE spec_hash=?",
                    (json.dumps(state, default=str), now_iso(), digest),
                )
            else:
                await db.execute(
                    "UPDATE agents SET runtime_state_json=?, consecutive_failures=?, updated_at=? "
                    "WHERE spec_hash=?",
                    (json.dumps(state, default=str), failures, now_iso(), digest),
                )

    async def due_interval_agents(self, now_ts: float) -> list[dict[str, Any]]:
        agents = await self.list_agents("live")
        due: list[dict[str, Any]] = []
        for agent in agents:
            try:
                state = json.loads(agent.get("runtime_state_json") or "{}")
            except json.JSONDecodeError:
                state = {}
            next_run = float(state.get("next_run_at") or 0)
            if next_run and next_run <= now_ts:
                agent["runtime_state"] = state
                due.append(agent)
        return due

    async def delete_agent(self, digest: str) -> None:
        db = self._connection()
        async with self._write():
            await db.execute("DELETE FROM agents WHERE spec_hash=?", (digest,))

    async def lru_agents(self, keep_hours: int = 24) -> list[dict[str, Any]]:
        """Oldest live agents that have not been used recently (eviction candidates)."""
        cutoff = datetime.fromtimestamp(time.time() - keep_hours * 3600, UTC).isoformat()
        db = self._connection()
        cursor = await db.execute(
            "SELECT * FROM agents WHERE status='live' AND COALESCE(last_used_at, created_at) < ? "
            "ORDER BY COALESCE(last_used_at, created_at) ASC",
            (cutoff,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ----------------------------------------------------------------- usage

    async def consume_usage(
        self, sender: str, bucket: str, limit: int, window_s: float, cost: int = 1
    ) -> tuple[bool, int]:
        """Atomic fixed-window counter. Returns (allowed, remaining_after)."""
        db = self._connection()
        now = time.time()
        async with self._write():
            cursor = await db.execute(
                "SELECT window_start, count FROM usage WHERE sender_address=? AND bucket=?",
                (sender, bucket),
            )
            row = await cursor.fetchone()
            if row is None or now - float(row["window_start"]) >= window_s:
                await db.execute(
                    "INSERT INTO usage(sender_address, bucket, window_start, count) VALUES (?,?,?,?) "
                    "ON CONFLICT(sender_address, bucket) DO UPDATE SET "
                    "window_start=excluded.window_start, count=excluded.count",
                    (sender, bucket, now, cost),
                )
                return True, max(0, limit - cost)
            current = int(row["count"])
            if current + cost > limit:
                return False, 0
            await db.execute(
                "UPDATE usage SET count=count+? WHERE sender_address=? AND bucket=?",
                (cost, sender, bucket),
            )
            return True, max(0, limit - (current + cost))

    async def usage_count(self, sender: str, bucket: str, window_s: float) -> int:
        db = self._connection()
        cursor = await db.execute(
            "SELECT window_start, count FROM usage WHERE sender_address=? AND bucket=?",
            (sender, bucket),
        )
        row = await cursor.fetchone()
        if row is None or time.time() - float(row["window_start"]) >= window_s:
            return 0
        return int(row["count"])

    # ------------------------------------------------------------ capability

    async def capability_request(self, sender: str, request: str, reason: str) -> None:
        db = self._connection()
        async with self._write():
            await db.execute(
                "INSERT INTO capability_requests(sender_address, request_text, reason, created_at) "
                "VALUES(?,?,?,?)",
                (sender, request[:2000], reason[:500], now_iso()),
            )

    # --------------------------------------------------------------- payments

    async def save_payment(
        self, reference: str, session_id: str, status: str, **fields: Any
    ) -> None:
        db = self._connection()
        async with self._write():
            await db.execute(
                """INSERT INTO payments(reference, session_id, status, rail, transaction_id, amount, currency, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(reference) DO UPDATE SET status=excluded.status,
                    rail=COALESCE(excluded.rail, payments.rail),
                    transaction_id=COALESCE(excluded.transaction_id, payments.transaction_id),
                    amount=COALESCE(excluded.amount, payments.amount),
                    currency=COALESCE(excluded.currency, payments.currency),
                    updated_at=excluded.updated_at""",
                (
                    reference,
                    session_id,
                    status,
                    fields.get("rail"),
                    fields.get("transaction_id"),
                    fields.get("amount"),
                    fields.get("currency"),
                    now_iso(),
                ),
            )

    async def get_payment(self, reference: str) -> dict[str, Any] | None:
        db = self._connection()
        cursor = await db.execute("SELECT * FROM payments WHERE reference=?", (reference,))
        row = await cursor.fetchone()
        return dict(row) if row else None
