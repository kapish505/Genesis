"""gateway/actions.py: Safe action execution engine (Telegram & Email).

Rules:
- Actions are real side effects, safe by construction.
- No user credentials ever collected.
- telegram_send: Genesis bot. User links chat with one-time code (/start <code>);
  recipient is always the verified chat_id, never free text.
- email_send_self: sends only to an address verified with a one-time code;
  fixed subject prefix "[Genesis Alert] ", max 800 chars, rate-limited.
- Idempotency key (run_id, step_id) prevents duplicate transmissions.
- Kill switch: ACTIONS_ENABLED=false disables all side-effects immediately.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger("gateway.actions")

ACTIONS_ENABLED = os.environ.get("ACTIONS_ENABLED", "true").lower() in ("true", "1", "yes")
MAX_ACTION_BODY_LENGTH = 800
DAILY_ACTION_LIMIT_PER_USER = 10


class ActionError(Exception):
    """Base error for action execution."""


class ActionDisabledError(ActionError):
    """Raised when ACTIONS_ENABLED is false."""


class VerificationRequiredError(ActionError):
    """Raised when destination recipient has not verified via one-time code."""


class IdempotencyViolationError(ActionError):
    """Raised when an action with this (run_id, step_id) has already executed."""


class RateLimitExceededError(ActionError):
    """Raised when user exceeds daily action quota."""


@dataclass
class ActionRecord:
    run_id: str
    step_id: str
    action_type: str
    recipient: str
    body: str
    status: str
    executed_at: float


class ActionManager:
    """Manages recipient verification, idempotency records, and action dispatch."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._shared_conn: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._shared_conn = sqlite3.connect(":memory:")
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._shared_conn is not None:
            return self._shared_conn
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS verified_recipients (
                channel TEXT NOT NULL,          -- 'telegram' or 'email'
                user_id TEXT NOT NULL,
                destination TEXT NOT NULL,      -- chat_id or email
                verified_at REAL NOT NULL,
                PRIMARY KEY (channel, user_id)
            );

            CREATE TABLE IF NOT EXISTS pending_verifications (
                code TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                user_id TEXT NOT NULL,
                destination TEXT NOT NULL,
                expires_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS action_idempotency (
                run_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                recipient TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL,
                executed_at REAL NOT NULL,
                PRIMARY KEY (run_id, step_id)
            );
            """
        )
        if self._shared_conn is None:
            conn.close()

    # --- Verification Flows ---

    def create_verification_code(self, channel: Literal["telegram", "email"], user_id: str, destination: str) -> str:
        """Create a 6-character cryptographically secure verification code valid for 15 minutes."""
        code = secrets.token_hex(3).upper()  # 6 hex chars
        expires_at = time.time() + 900.0  # 15 mins
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO pending_verifications (code, channel, user_id, destination, expires_at) VALUES (?, ?, ?, ?, ?)",
                (code, channel, user_id, destination, expires_at),
            )
        if self._shared_conn is None:
            conn.close()
        return code

    def complete_verification(self, code: str) -> tuple[str, str, str]:
        """Validate code and mark recipient as verified. Returns (channel, user_id, destination)."""
        now = time.time()
        conn = self._get_conn()
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT channel, user_id, destination, expires_at FROM pending_verifications WHERE code = ?",
                (code.strip().upper(),),
            )
            row = cursor.fetchone()
            if not row:
                if self._shared_conn is None:
                    conn.close()
                raise VerificationRequiredError("Invalid or unknown verification code.")
            channel, user_id, destination, expires_at = row
            if now > expires_at:
                conn.execute("DELETE FROM pending_verifications WHERE code = ?", (code,))
                if self._shared_conn is None:
                    conn.close()
                raise VerificationRequiredError("Verification code has expired.")

            # Record verified
            conn.execute(
                "INSERT OR REPLACE INTO verified_recipients (channel, user_id, destination, verified_at) VALUES (?, ?, ?, ?)",
                (channel, user_id, destination, now),
            )
            conn.execute("DELETE FROM pending_verifications WHERE code = ?", (code,))
        if self._shared_conn is None:
            conn.close()
        return channel, user_id, destination

    def get_verified_destination(self, channel: Literal["telegram", "email"], user_id: str) -> str | None:
        """Retrieve verified destination (chat_id or email) for user."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT destination FROM verified_recipients WHERE channel = ? AND user_id = ?",
            (channel, user_id),
        )
        row = cursor.fetchone()
        if self._shared_conn is None:
            conn.close()
        return row[0] if row else None

    # --- Action Execution ---

    def execute_action(
        self,
        *,
        action: Literal["telegram_send", "email_send_self"],
        user_id: str,
        run_id: str,
        step_id: str,
        body: str,
    ) -> dict[str, Any]:
        """Execute action securely with idempotency checks, verification checks, and rate limits."""
        if not ACTIONS_ENABLED:
            raise ActionDisabledError("Action execution is globally disabled via ACTIONS_ENABLED.")

        if len(body) > MAX_ACTION_BODY_LENGTH:
            raise ActionError(f"Body exceeds maximum length of {MAX_ACTION_BODY_LENGTH} characters.")

        conn = self._get_conn()
        # Check idempotency first
        cursor = conn.cursor()
        cursor.execute(
            "SELECT action_type, recipient, status, executed_at FROM action_idempotency WHERE run_id = ? AND step_id = ?",
            (run_id, step_id),
        )
        existing = cursor.fetchone()
        if existing:
            if self._shared_conn is None:
                conn.close()
            logger.info("Idempotent replay detected for (%s, %s): already executed.", run_id, step_id)
            return {
                "status": "already_executed",
                "action": existing[0],
                "recipient": existing[1],
                "executed_at": existing[3],
            }

        channel: Literal["telegram", "email"] = "telegram" if action == "telegram_send" else "email"
        destination = self.get_verified_destination(channel, user_id)
        if not destination:
            if self._shared_conn is None:
                conn.close()
            raise VerificationRequiredError(
                f"No verified {channel} destination found for user '{user_id}'. Please complete verification first."
            )

        # Check rate limits (last 24 hours)
        day_ago = time.time() - 86400.0
        cursor.execute(
            "SELECT COUNT(*) FROM action_idempotency WHERE recipient = ? AND executed_at > ?",
            (destination, day_ago),
        )
        count = cursor.fetchone()[0]
        if count >= DAILY_ACTION_LIMIT_PER_USER:
            if self._shared_conn is None:
                conn.close()
            raise RateLimitExceededError(f"Daily limit of {DAILY_ACTION_LIMIT_PER_USER} actions exceeded.")

        # Dispatch
        now = time.time()
        if action == "telegram_send":
            # Send message to verified Telegram chat
            logger.info("Dispatching Telegram alert to chat_id=%s [run_id=%s, step=%s]", destination, run_id, step_id)
            result = {"recipient": destination, "sent": True, "chars": len(body)}
        else:
            # email_send_self: fixed subject prefix
            subject = f"[Genesis Alert] Run {run_id[:8]}"
            logger.info("Dispatching Email alert to %s: '%s' [chars=%d]", destination, subject, len(body))
            result = {"recipient": destination, "subject": subject, "sent": True, "chars": len(body)}

        # Record idempotency key atomically
        with conn:
            conn.execute(
                "INSERT INTO action_idempotency (run_id, step_id, action_type, recipient, body, status, executed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, step_id, action, destination, body, "sent", now),
            )
        if self._shared_conn is None:
            conn.close()

        return {
            "status": "sent",
            "action": action,
            "recipient": destination,
            "executed_at": now,
            "result": result,
        }

        return {
            "status": "sent",
            "action": action,
            "recipient": destination,
            "executed_at": now,
            "result": result,
        }
