"""Genesis uAgent entrypoint.

Responsibilities, in order:

1. register the ACP chat protocol and (when enabled) the seller-side payment
   protocol with the correct role;
2. acknowledge every inbound ChatMessage *before* doing any work, then dedupe;
3. route agent-to-agent replies into the relay router, and user messages into
   the flow;
4. send progress while long work runs so the chat never goes quiet;
5. run the background sweeper (interrupted builds, session expiry, interval
   agents) on an interval.

All dependencies are constructed in ``build_flow`` so tests can assemble the
same flow with fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, cast

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CommitPayment,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

from genesis.builder import Builder
from genesis.config import Settings, get_settings
from genesis.delegate import Relay, ReplyRouter
from genesis.deployer import Deployer
from genesis.executor import Executor
from genesis.flow import FlowReply, GenesisFlow
from genesis.gateway_client import SyncGateway
from genesis.ledger import Ledger
from genesis.llm import AsiOneClient
from genesis.payments import LedgerVerifier, PaymentConfig, SellerPayments, StripeVerifier
from genesis.search import AgentverseClient
from genesis.session import SessionStore

logger = logging.getLogger("genesis.agent")


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


# --------------------------------------------------------------------- wiring


async def _probe(address: str, inputs: dict[str, Any]) -> str:
    """Live probe over ACP — bound to the running agent's relay in main()."""
    raise RuntimeError("probe called before the agent was initialised")


async def build_flow(settings: Settings, *, relay: Relay, probe: Any) -> tuple[GenesisFlow, Any]:
    ledger = Ledger(settings.database_path)
    await ledger.initialize()
    sessions = SessionStore(settings)

    llm = AsiOneClient(
        settings.asi1_api_key,
        settings.asi1_base_url,
        settings.asi1_model,
        settings.asi1_fast_model,
    )
    search = AgentverseClient(settings.agentverse_base_url, settings.agentverse_api_key)
    executor = Executor(settings, settings.gateway_secret)
    builder = Builder()
    deployer = Deployer(search, ledger, builder, settings, probe)

    if settings.payments_enabled:
        verifier: Any
        if settings.payment_rail == "stripe":
            verifier = StripeVerifier(settings.stripe_secret_key)
        else:
            verifier = LedgerVerifier(ledger)
        payments = SellerPayments(
            PaymentConfig(
                enabled=True,
                rail=settings.payment_rail,
                recipient=settings.payment_recipient,
                amount=settings.payment_amount,
                currency=settings.payment_currency,
            ),
            ledger,
            verifier,
        )
    else:
        payments = SellerPayments(PaymentConfig(enabled=False), ledger, None)

    flow = GenesisFlow(
        settings, ledger, sessions, llm, search, relay, executor, deployer, builder, payments
    )
    return flow, ledger


# ------------------------------------------------------------------ rendering


def render(reply: FlowReply) -> list[ChatMessage]:
    """Convert flow events into ACP ChatMessages (text, card, end, payment)."""
    messages: list[ChatMessage] = []
    for kind, payload in reply.events:
        if kind == "text":
            messages.append(ChatMessage(content=[TextContent(type="text", text=payload["text"])]))
        elif kind == "card":
            messages.append(
                ChatMessage(
                    content=[
                        TextContent(type="text", text=payload.get("text") or "Choose an option."),
                        MetadataContent(type="metadata", metadata=payload["metadata"]),
                    ]
                )
            )
        elif kind == "end":
            messages.append(ChatMessage(content=[EndSessionContent(type="end-session")]))
    return messages


# ---------------------------------------------------------------------- agent


class GenesisRuntime:
    """Mutable holder for the flow so module-level handlers stay thin."""

    def __init__(self) -> None:
        self.flow: GenesisFlow | None = None
        self.ledger: Ledger | None = None
        self.router = ReplyRouter()
        self.settings: Settings = get_settings()
        self._relay: Relay | None = None

    async def probe(self, address: str, inputs: dict[str, Any]) -> str:
        relay = self.relay
        return await relay.ask(
            address, json.dumps(inputs, ensure_ascii=False), timeout_s=self.settings.probe_timeout_s
        )

    @property
    def relay(self) -> Relay:
        if self._relay is None:
            raise RuntimeError("relay not initialised")
        return self._relay

    @relay.setter
    def relay(self, value: Relay) -> None:
        self._relay = value


def create_agent(settings: Settings, runtime: GenesisRuntime) -> tuple[Agent, Protocol]:
    agent_kwargs: dict[str, Any] = {
        "name": settings.agent_name,
        "mailbox": settings.agent_mailbox,
        "handle": settings.agent_handle or None,
        "publish_agent_details": True,
        "readme_path": "README.md",
    }
    if settings.agent_seed:
        agent_kwargs["seed"] = settings.agent_seed
    if settings.agent_endpoint:
        agent_kwargs["endpoint"] = settings.agent_endpoint
    agent = Agent(**agent_kwargs)
    agent = cast(Agent, agent)
    chat = Protocol(spec=chat_protocol_spec)

    async def send_reply(ctx: Context, sender: str, reply: FlowReply) -> None:
        for message in render(reply):
            await ctx.send(sender, message)

    async def run_flow(ctx: Context, sender: str, text: str, external_id: str | None) -> None:
        assert runtime.flow is not None
        reply = await runtime.flow.handle_message(sender, text, external_id)
        await send_reply(ctx, sender, reply)

    @chat.on_message(ChatMessage)
    async def handle_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
        # First side effect, always: acknowledge.
        await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))
        if runtime.ledger is not None:
            fresh = await runtime.ledger.mark_message(
                str(msg.msg_id), runtime.settings.dedupe_window
            )
            if not fresh:
                return
        text = "".join(item.text for item in msg.content if isinstance(item, TextContent)).strip()
        external_id = None
        if runtime.router.deliver(sender, text):
            return
        await run_flow(ctx, sender, text, external_id)

    @chat.on_message(ChatAcknowledgement)
    async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
        return

    agent.include(chat, publish_manifest=True)

    if settings.payments_enabled:
        payment = Protocol(spec=payment_protocol_spec, role="seller")

        @payment.on_message(CommitPayment)
        async def handle_commit(ctx: Context, sender: str, msg: CommitPayment) -> None:
            assert runtime.flow is not None
            reference = msg.reference or ""
            reply = FlowReply()
            await runtime.flow.on_payment_commit(
                sender, reference, msg.transaction_id, msg.funds, reply
            )
            await send_reply(ctx, sender, reply)

        @payment.on_message(RejectPayment)
        async def handle_reject(ctx: Context, sender: str, msg: RejectPayment) -> None:
            assert runtime.flow is not None
            if msg.reason:
                logger.info("buyer rejected payment: %s", msg.reason)
            await runtime.flow.payments.on_reject(msg.reference or "", msg.reason or "")
            await send_reply(
                ctx, sender, FlowReply().text("Payment rejected, so nothing was deployed.").end()
            )

        @payment.on_message(RequestPayment)
        async def handle_request_payment(ctx: Context, sender: str, msg: RequestPayment) -> None:
            return

        agent.include(payment, publish_manifest=True)

    @agent.on_interval(period=60.0)
    async def sweeper(ctx: Context) -> None:
        await sweep(ctx, runtime)

    return agent, chat


async def sweep(ctx: Context, runtime: GenesisRuntime) -> None:
    if runtime.flow is None or runtime.ledger is None:
        return
    settings = runtime.settings
    try:
        touched = await runtime.flow.deployer.sweep()
        if touched:
            logger.info("sweeper compensated %d interrupted builds", len(touched))
    except Exception:  # noqa: BLE001 - a sweeper must never crash the agent
        logger.exception("build sweep failed")
    try:
        cutoff = time.time() - settings.session_total_ttl_s
        await runtime.ledger.purge_sessions(cutoff)
        runtime.flow.sessions.purge_expired()
    except Exception:  # noqa: BLE001
        logger.exception("session purge failed")
    try:
        for agent_row in await runtime.ledger.due_interval_agents(time.time()):
            spec_data = await runtime.ledger.load_spec(agent_row["spec_hash"])
            if not spec_data:
                continue
            from genesis.spec import parse_spec

            spec = parse_spec(spec_data)
            state = agent_row.get("runtime_state", {})
            notify_chat_id = str(state.get("notify_chat_id") or "")
            if spec.notify == "telegram" and notify_chat_id:
                try:
                    from gateway.auth import make_token

                    token = make_token(
                        settings.gateway_secret,
                        spec.name,
                        tools=sorted(
                            set(spec.tools) & {"fetch", "extract", "search", "llm", "notify"}
                        ),
                        hosts=spec.allowed_hosts,
                    )
                    gateway = SyncGateway(
                        settings.gateway_url, spec.name, token, hosts=spec.allowed_hosts
                    )
                    try:
                        result = await asyncio.to_thread(
                            runtime.flow.executor.run_ephemeral_sync,
                            spec,
                            dict(spec.selftest.inputs),
                            state,
                        )
                    finally:
                        gateway.close()
                    await _notify(ctx, runtime, notify_chat_id, str(result["output"])[:3000])
                    failures = 0
                    state["last_error"] = ""
                except Exception as exc:  # noqa: BLE001
                    failures = int(agent_row.get("consecutive_failures") or 0) + 1
                    state["last_error"] = str(exc)[:300]
                    logger.warning("interval run failed for %s: %s", spec.name, exc)
                    await _notify(
                        ctx, runtime, notify_chat_id, f"Scheduled run failed: {str(exc)[:300]}"
                    )
                    if failures >= settings.interval_max_failures:
                        await runtime.ledger.set_agent_status(agent_row["spec_hash"], "paused")
            state["next_run_at"] = time.time() + (spec.interval_seconds or 3600)
            await runtime.ledger.set_runtime_state(
                agent_row["spec_hash"], state, failures if "failures" in locals() else None
            )
    except Exception:  # noqa: BLE001
        logger.exception("interval sweep failed")


async def _notify(ctx: Context, runtime: GenesisRuntime, chat_id: str, text: str) -> None:
    import httpx

    token = _gateway_token_for_notify(runtime.settings)
    if not token:
        return
    payload = {"agent_id": "genesis-scheduler", "payload": {"chat_id": chat_id, "text": text}}
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{runtime.settings.gateway_url.rstrip('/')}/notify",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )


def _gateway_token_for_notify(settings: Settings) -> str:
    from gateway.auth import make_token

    return make_token(
        settings.gateway_secret, "genesis-scheduler", tools=["notify"], hosts=[], ttl_s=86400
    )


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    runtime = GenesisRuntime()
    agent, _chat = create_agent(settings, runtime)

    @agent.on_event("startup")
    async def startup(ctx: Context) -> None:
        router = ReplyRouter()

        async def send(address: str, message: ChatMessage) -> None:
            await ctx.send(address, message)

        runtime.relay = Relay(send, router, default_timeout_s=settings.delegate_timeout_s)
        runtime.router = router
        flow, ledger = await build_flow(settings, relay=runtime.relay, probe=runtime.probe)
        runtime.flow, runtime.ledger = flow, ledger
        logger.info("genesis started as %s", agent.address)

    @agent.on_event("shutdown")
    async def shutdown(ctx: Context) -> None:
        if runtime.ledger is not None:
            await runtime.ledger.close()

    agent.run()


if __name__ == "__main__":
    main()
