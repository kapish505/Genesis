"""Shared uAgents protocol models for Genesis orchestrator and front agents."""

from typing import Any, Literal
from uagents import Model


class RunRequest(Model):
    """Dispatched by a front agent to Genesis Orchestrator to execute a spec."""

    run_id: str
    spec_id: str
    sender: str
    user_text: str
    inputs: dict[str, Any] = {}


class RunEvent(Model):
    """Streamed by Genesis Orchestrator back to the front agent."""

    run_id: str
    kind: Literal["progress", "approval", "result", "error"]
    text: str
    card_payload: str | None = None
    approval_id: str | None = None


class ApprovalReply(Model):
    """User decision returned from front agent to Genesis Orchestrator for an action card."""

    run_id: str
    approval_id: str
    decision: Literal["approve", "reject", "edit"]
    payload: str | None = None
