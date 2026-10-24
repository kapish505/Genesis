"""front_template: Lightweight front agent deployed to Agentverse.

This agent acts as the public, discoverable front for a managed-mode Genesis spec.
It runs on Agentverse (standard uAgents runtime) and communicates with the
Genesis Orchestrator via uAgents Model messages (RunRequest, RunEvent, ApprovalReply).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import requests
from uagents import Agent, Context, Model

# The orchestrator address and spec metadata are injected at deployment time
GENESIS_ADDRESS = os.environ.get("GENESIS_ADDRESS", "agent1q_genesis_orchestrator")
SPEC_ID = os.environ.get("SPEC_ID", "spec_default")
AGENT_NAME = os.environ.get("AGENT_NAME", "genesis-front")
TIMEOUT_SECONDS = int(os.environ.get("TIMEOUT_SECONDS", "30"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("genesis_front")


# Internal protocol models (embedded to keep the front agent self-contained)
class RunRequest(Model):
    run_id: str
    spec_id: str
    sender: str
    user_text: str
    inputs: dict[str, Any] = {}


class RunEvent(Model):
    run_id: str
    kind: str  # progress, approval, result, error
    text: str
    card_payload: str | None = None
    approval_id: str | None = None


class ApprovalReply(Model):
    run_id: str
    approval_id: str
    decision: str  # approve, reject, edit
    payload: str | None = None


class ChatMessage(Model):
    text: str
    card_payload: str | None = None


# Session tracking: run_id -> user sender address
active_runs: dict[str, dict[str, Any]] = {}

front_agent = Agent(
    name=AGENT_NAME,
    seed=os.environ.get("AGENT_SEED", f"{AGENT_NAME}-seed-v2"),
    port=int(os.environ.get("AGENT_PORT", "8000")),
    endpoint=[os.environ.get("AGENT_ENDPOINT", "http://127.0.0.1:8000/submit")],
)


@front_agent.on_event("startup")
async def on_startup(ctx: Context) -> None:
    logger.info("Front agent %s online at %s. Orchestrator target: %s", AGENT_NAME, ctx.address, GENESIS_ADDRESS)


@front_agent.on_message(model=ChatMessage)
async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage) -> None:
    """Handle direct chat from ASI:One user."""
    logger.info("Received chat from %s: %s", sender, msg.text[:60])
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    active_runs[run_id] = {
        "user_sender": sender,
        "started_at": time.time(),
        "status": "running",
    }

    # Acknowledge user immediately
    await ctx.send(sender, ChatMessage(text="⚡ Request accepted. Connecting to Genesis Orchestrator..."))

    # Forward to Genesis Orchestrator
    try:
        req = RunRequest(
            run_id=run_id,
            spec_id=SPEC_ID,
            sender=sender,
            user_text=msg.text,
            inputs={},
        )
        await ctx.send(GENESIS_ADDRESS, req)
    except Exception as exc:
        logger.error("Failed to forward RunRequest to Genesis: %s", exc)
        await ctx.send(
            sender,
            ChatMessage(text="⚠️ The Genesis Orchestrator is temporarily unavailable. Please try again shortly."),
        )
        active_runs.pop(run_id, None)


@front_agent.on_message(model=RunEvent)
async def handle_run_event(ctx: Context, sender: str, event: RunEvent) -> None:
    """Handle streamed progress, approval cards, results, or errors from Genesis."""
    if sender != GENESIS_ADDRESS:
        logger.warning("Rejected RunEvent from unauthorized sender: %s", sender)
        return

    run_info = active_runs.get(event.run_id)
    if not run_info:
        logger.warning("Received RunEvent for unknown run_id: %s", event.run_id)
        return

    user_sender = run_info["user_sender"]

    if event.kind == "progress":
        await ctx.send(user_sender, ChatMessage(text=f"⏳ {event.text}"))
    elif event.kind == "approval":
        # Pass approval review card through to user
        await ctx.send(
            user_sender,
            ChatMessage(
                text=event.text,
                card_payload=event.card_payload,
            ),
        )
    elif event.kind == "result":
        run_info["status"] = "completed"
        await ctx.send(
            user_sender,
            ChatMessage(
                text=event.text,
                card_payload=event.card_payload,
            ),
        )
        active_runs.pop(event.run_id, None)
    elif event.kind == "error":
        run_info["status"] = "failed"
        await ctx.send(user_sender, ChatMessage(text=f"❌ Execution failed: {event.text}"))
        active_runs.pop(event.run_id, None)


@front_agent.on_interval(period=10.0)
async def sweep_stale_runs(ctx: Context) -> None:
    """Enforce 30s timeout cap if Orchestrator never replies."""
    now = time.time()
    expired = []
    for run_id, info in active_runs.items():
        if now - info["started_at"] > TIMEOUT_SECONDS:
            expired.append(run_id)

    for run_id in expired:
        info = active_runs.pop(run_id, None)
        if info:
            logger.warning("Run %s timed out after %ds", run_id, TIMEOUT_SECONDS)
            try:
                await ctx.send(
                    info["user_sender"],
                    ChatMessage(text="⚠️ The Genesis Orchestrator is temporarily unavailable (timeout). Please try again later."),
                )
            except Exception:
                pass


if __name__ == "__main__":
    front_agent.run()
