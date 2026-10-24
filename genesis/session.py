"""Session state for one conversation.

Sessions are keyed by (sender, ASI:One external session id when present). They
carry the pending card interaction so a reply can be tied back to the exact card
that produced it, which is what makes stale-card detection possible.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from genesis.config import Settings

SessionState = Literal[
    "new",
    "detecting",
    "needs_input",
    "awaiting_choice",
    "awaiting_approval",
    "building",
    "running",
    "complete",
    "cancelled",
    "failed",
]

INPUT_STATES: frozenset[str] = frozenset({"needs_input", "awaiting_choice", "awaiting_approval"})

CARD_ACTIONS: dict[str, tuple[str, ...]] = {
    "form": ("submit", "cancel"),
    "carousel": ("use_existing", "build_new", "cancel"),
    "review": ("approve", "edit", "cancel"),
    "detail": ("run_again", "delete_agent", "cancel"),
}


@dataclass
class Session:
    sender: str
    external_id: str | None = None
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: SessionState = "new"
    data: dict[str, Any] = field(default_factory=dict)
    pending_action: str | None = None
    pending_actions: tuple[str, ...] = ()
    pending_since: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()

    def set_state(self, state: SessionState) -> None:
        self.state = state
        self.touch()

    def open_card(self, kind: str) -> None:
        self.pending_action = kind
        self.pending_actions = CARD_ACTIONS.get(kind, ())
        self.pending_since = time.time()
        self.touch()

    def close_card(self) -> None:
        self.pending_action = None
        self.pending_actions = ()
        self.pending_since = 0.0
        self.touch()

    def expired(self, settings: Settings, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        if now - self.created_at > settings.session_total_ttl_s:
            return True
        if self.state in INPUT_STATES and now - self.pending_since > settings.session_input_ttl_s:
            return True
        return False

    def card_stale(self, settings: Settings, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (
            self.state in INPUT_STATES and now - self.pending_since > settings.session_input_ttl_s
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "sender": self.sender,
            "external_id": self.external_id,
            "state": self.state,
            "data": self.data,
            "pending_action": self.pending_action,
            "pending_actions": list(self.pending_actions),
            "pending_since": self.pending_since,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Session:
        return cls(
            sender=str(row["sender"]),
            external_id=row.get("external_id"),
            session_id=str(row["session_id"]),
            state=row.get("state") or "new",
            data=row.get("data") or {},
            pending_action=row.get("pending_action"),
            pending_actions=tuple(row.get("pending_actions") or ()),
            pending_since=float(row.get("pending_since") or 0.0),
            created_at=float(row.get("created_at") or 0.0),
            updated_at=float(row.get("updated_at") or 0.0),
        )


class SessionStore:
    """In-memory index over sessions persisted in the ledger.

    The store owns no eviction policy beyond TTL: a restart rebuilds from SQLite,
    which is what lets 'approve' still work after a redeploy.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sessions: dict[tuple[str, str | None], Session] = {}
        self._by_id: dict[str, Session] = {}

    def get_or_create(self, sender: str, external_id: str | None = None) -> Session:
        key = (sender, external_id)
        existing = self._sessions.get(key)
        if existing is not None:
            if existing.expired(self.settings):
                self.forget(existing)
            else:
                existing.touch()
                return existing
        session = Session(sender=sender, external_id=external_id)
        self._sessions[key] = session
        self._by_id[session.session_id] = session
        return session

    def hydrate(self, session: Session) -> Session:
        key = (session.sender, session.external_id)
        session.expired(self.settings)  # keep timestamps comparable
        self._sessions[key] = session
        self._by_id[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._by_id.get(session_id)

    def find(self, sender: str, external_id: str | None = None) -> Session | None:
        return self._sessions.get((sender, external_id))

    def forget(self, session: Session) -> None:
        self._sessions.pop((session.sender, session.external_id), None)
        self._by_id.pop(session.session_id, None)

    def purge_expired(self) -> list[Session]:
        expired = [item for item in self._by_id.values() if item.expired(self.settings)]
        for item in expired:
            self.forget(item)
        return expired
