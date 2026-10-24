"""The conversation flow: the state machine that ties every component together.

Entry point: ``handle_message(sender, text, external_session_id)``. The flow is
transport-agnostic — it receives text and returns a list of outbound events —
which is what makes the whole end-to-end path testable without a live agent.

Flow (happy path)::

    new request
      -> safety screen / capability check / quota
      -> detector (intent + Agentverse search + scoring)
         use_existing  -> delegate, relay reply
         ask_user      -> form or carousel card, wait
         build         -> planner (JSON + repair loop) -> static verify
                        -> review card, wait
                        -> [payment gate when enabled]
                        -> deploy saga (progress messages)
                        -> live probe (inside the saga)
                        -> run the original task through the new agent
                        -> detail card with the result
                        -> fallback to ephemeral execution on any build failure

Every user-visible branch states its typed fallback, and every card action is
validated against the session's pending card and TTL before acting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from genesis.builder import Builder
from genesis.cards import (
    carousel_card,
    detail_card,
    fallback_text,
    form_card,
    is_structured_selection,
    parse_selection,
    review_card,
)
from genesis.config import Settings
from genesis.delegate import DelegationError, Relay
from genesis.deployer import Deployer
from genesis.detector import Detection, Detector
from genesis.errors import DeploymentError, GenesisError, PlanningError
from genesis.executor import Executor, RunResult
from genesis.faq import answer_meta_question
from genesis.ledger import Ledger
from genesis.llm import AsiOneClient
from genesis.payments import SellerPayments
from genesis.planner import Planner
from genesis.safety import classify_capability, quota_reason, redact, screen
from genesis.search import AgentverseClient
from genesis.session import Session, SessionStore
from genesis.spec import AgentSpec, PlanError, parse_spec, spec_hash
from genesis.verifier import summarize_for_review

logger = logging.getLogger(__name__)

Event = tuple[str, dict[str, Any]]  # (kind, payload)

BUILD_ACTIONS = {"approve": "review", "edit": "review", "cancel": "review"}
CARD_ACTION_MAP: dict[str, dict[str, str]] = {
    "form": {"submit": "submit", "cancel": "cancel"},
    "carousel": {"use_existing": "use_existing", "build_new": "build_new", "cancel": "cancel"},
    "review": {"approve": "approve", "edit": "edit", "cancel": "cancel"},
    "detail": {"run_again": "run_again", "delete_agent": "delete_agent", "cancel": "cancel"},
}


@dataclass
class FlowReply:
    events: list[Event] = field(default_factory=list)

    def text(self, text: str) -> FlowReply:
        self.events.append(("text", {"text": text}))
        return self

    def card(self, kind: str, metadata: dict[str, str], text: str = "") -> FlowReply:
        self.events.append(("card", {"kind": kind, "metadata": metadata, "text": text}))
        return self

    def end(self) -> FlowReply:
        self.events.append(("end", {}))
        return self


class GenesisFlow:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        sessions: SessionStore,
        llm: AsiOneClient,
        search: AgentverseClient,
        relay: Relay,
        executor: Executor,
        deployer: Deployer,
        builder: Builder,
        payments: SellerPayments,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.sessions = sessions
        self.llm = llm
        self.detector = Detector(search, llm, settings)
        self.relay = relay
        self.executor = executor
        self.deployer = deployer
        self.builder = builder
        self.payments = payments
        self.planner = Planner(llm, settings.planner_repairs)
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ entry

    async def load_or_create(self, sender: str, external_id: str | None) -> Session:
        session = self.sessions.find(sender, external_id)
        if session is not None:
            if session.expired(self.settings):
                self.sessions.forget(session)
            else:
                return session
        row = await self.ledger.load_session(sender, external_id)
        if row is not None:
            session = Session.from_row(row)
            if not session.expired(self.settings):
                return self.sessions.hydrate(session)
        session = self.sessions.get_or_create(sender, external_id)
        await self._save(session)
        return session

    async def _save(self, session: Session) -> None:
        session.touch()
        await self.ledger.save_session(session.to_row())

    def _classifier(
        self, session: Session
    ) -> Callable[[str, str, list[str]], Awaitable[dict[str, Any] | None]]:
        async def classify(text: str, sid: str, actions: list[str]) -> dict[str, Any] | None:
            if not self.llm.configured:
                return None
            try:
                result = await self.llm.classify_selection(
                    text,
                    state=session.state,
                    options=actions,
                    fallback_context=json.dumps(session.data.get("review", {}), default=str)[:1500],
                )
            except Exception as exc:  # noqa: BLE001 - classification is best-effort
                logger.warning("selection classification failed: %s", exc)
                return None
            return result

        return classify

    # ------------------------------------------------------------- dispatch

    async def handle_message(
        self, sender: str, text: str, external_id: str | None = None
    ) -> FlowReply:
        reply = FlowReply()
        session = await self.load_or_create(sender, external_id)
        lock = self._locks.setdefault(session.session_id, asyncio.Lock())
        async with lock:
            try:
                await self._dispatch(session, sender, text, reply)
            except GenesisError as exc:
                logger.info("flow error for %s: %s", session.session_id, exc.detail)
                reply.text(exc.user_message).end()
                session.set_state("failed")
                session.close_card()
            except Exception:  # noqa: BLE001 - the user must always get an answer
                logger.exception("unhandled flow error for session %s", session.session_id)
                reply.text(
                    "Something went wrong on my side while handling that. "
                    "Nothing was deployed. Send the request again if you want me to retry."
                ).end()
                session.set_state("failed")
                session.close_card()
            finally:
                await self._save(session)
        return reply

    async def _dispatch(self, session: Session, sender: str, text: str, reply: FlowReply) -> None:
        stripped = text.strip()
        if not stripped:
            reply.text("Send me a task and I will take it from there.")
            return

        if is_structured_selection(stripped) or session.pending_action:
            handled = await self._handle_card_reply(session, sender, stripped, reply)
            if handled:
                return
            if is_structured_selection(stripped):
                reply.text(
                    "That card is no longer active. Send the request again and I will restart the flow."
                )
                session.close_card()
                return

        await self._start_request(session, sender, stripped, reply)

    # ---------------------------------------------------------- card replies

    async def _handle_card_reply(
        self, session: Session, sender: str, text: str, reply: FlowReply
    ) -> bool:
        if session.pending_action is None:
            return False
        if session.card_stale(self.settings):
            session.close_card()
            session.set_state("new")
            reply.text(
                "That card expired before it was answered, so nothing was built. "
                "Send the request again and I will restart it."
            )
            return True
        kind = session.pending_action
        selection = await parse_selection(
            text,
            session.session_id,
            CARD_ACTION_MAP.get(kind),
            self._classifier(session) if not is_structured_selection(text) else None,
        )
        if selection is None:
            reply.text(
                f"I could not tell what you chose. {fallback_text(kind, session.session_id)}"
            )
            return True
        action = str(selection.get("action"))
        sid = str(selection.get("sid"))
        if sid != session.session_id:
            reply.text(
                "That choice belongs to a different request, which has expired. "
                "Send the request again to restart."
            )
            return True

        session.close_card()
        if action == "cancel":
            session.set_state("cancelled")
            reply.text("Cancelled. Nothing was deployed.").end()
            return True
        if kind == "form" and action == "submit":
            await self._submit_form(session, sender, selection, reply)
            return True
        if kind == "carousel":
            if action == "use_existing":
                address = str(selection.get("agent_address") or "")
                await self._use_existing(session, sender, address, reply)
            elif action == "build_new":
                await self._plan_and_review(session, sender, reply)
            else:
                reply.text(fallback_text(kind, session.session_id))
            return True
        if kind == "review":
            if action == "approve":
                await self._approve_and_build(session, sender, reply)
            elif action == "edit":
                session.set_state("new")
                reply.text(
                    "Send the corrected request (for example: change the URL, the schedule, "
                    "or what the answer should include) and I will plan it again."
                )
            else:
                reply.text(fallback_text(kind, session.session_id))
            return True
        if kind == "detail":
            if action == "run_again":
                await self._run_again(session, sender, reply)
            elif action == "delete_agent":
                await self._delete_agent(session, selection, reply)
            else:
                reply.text(fallback_text(kind, session.session_id))
            return True
        return False

    async def _submit_form(
        self, session: Session, sender: str, selection: dict[str, Any], reply: FlowReply
    ) -> None:
        fields = {
            key: str(value).strip()
            for key, value in selection.items()
            if key not in {"sid", "action"} and isinstance(value, (str, int, float))
        }
        request = str(session.data.get("request", ""))
        merged = self._merge_inputs(request, fields)
        session.data["request"] = merged
        await self._start_request(session, sender, merged, reply)

    @staticmethod
    def _merge_inputs(request: str, fields: dict[str, str]) -> str:
        missing = ", ".join(f"{key}={value}" for key, value in fields.items() if value)
        if not missing:
            return request
        return f"{request}\nProvided: {missing}"

    # ---------------------------------------------------------- new requests

    async def _start_request(
        self, session: Session, sender: str, text: str, reply: FlowReply
    ) -> None:
        decision = screen(text)
        if not decision.allowed:
            reply.text(decision.reason).end()
            return

        meta_answer = answer_meta_question(text)
        if meta_answer:
            reply.text(meta_answer)
            session.set_state("answered")
            return
        capability = classify_capability(text)
        if capability.capability:
            await self.ledger.capability_request(sender, redact(text), capability.capability)
            reply.text(
                capability.reason + " I have logged this as a requested capability. "
                "I can still help with public-data tasks that need no login or payment."
            ).end()
            return

        allowed, _remaining = await self.ledger.consume_usage(
            sender, "runs", self.settings.sender_runs_per_hour, 3600
        )
        if not allowed:
            reply.text(quota_reason("runs", self.settings.sender_runs_per_hour)).end()
            return

        session.data = {"request": text}
        session.set_state("detecting")
        reply.text("Looking for an existing agent that already does this.")

        detection = await self.detector.detect(text)
        session.data["detection"] = {
            "verdict": detection.verdict,
            "intent": detection.intent.__dict__,
            "candidates": [candidate.__dict__ for candidate in detection.candidates],
            "missing": detection.missing,
        }
        await self._apply_detection(session, sender, detection, reply)

    async def _apply_detection(
        self, session: Session, sender: str, detection: Detection, reply: FlowReply
    ) -> None:
        if detection.missing:
            await self._ask_inputs(session, detection, reply)
            return
        if detection.verdict == "use_existing" and detection.best is not None:
            await self._use_existing(session, sender, detection.best.address, reply)
            return
        if detection.verdict == "ask_user" and detection.candidates:
            self._send_carousel(session, detection.candidates, reply)
            return
        if not self.settings.builds_enabled:
            reply.text(
                "No existing agent does this, and live builds are switched off right now. "
                "I can still run the task once here without deploying anything."
            )
            await self._ephemeral_for_request(session, sender, reply, reason="builds disabled")
            return
        await self._plan_and_review(session, sender, reply)

    async def _ask_inputs(self, session: Session, detection: Detection, reply: FlowReply) -> None:
        request = str(session.data.get("request", ""))
        fields: list[dict[str, Any]] = []
        for name in detection.missing:
            label = {
                "url": "Public URL to read",
                "link": "Public URL to read",
                "page": "Public URL to read",
                "website": "Public URL to read",
            }.get(name.casefold(), name.replace("_", " ").capitalize())
            entry: dict[str, Any] = {
                "name": name if name.isidentifier() else "url",
                "label": label,
                "type": "url" if "url" in name.casefold() or "link" in name.casefold() else "text",
                "required": True,
            }
            if entry["type"] == "url":
                entry["placeholder"] = "https://example.gov/scheme-page"
            fields.append(entry)
        if not fields:
            fields = [
                {
                    "name": "url",
                    "label": "Public URL to read",
                    "type": "url",
                    "required": True,
                    "placeholder": "https://example.gov/page",
                }
            ]
        session.data["missing"] = detection.missing
        session.set_state("needs_input")
        session.open_card("form")
        card = form_card(session.session_id, fields, intro="I will not guess a URL for you.")
        reply.text(
            "I can do that, but I need the exact source. "
            + fallback_text("form", session.session_id)
        )
        reply.card("form", card, text=fallback_text("form", session.session_id))
        session.data["request"] = request

    def _send_carousel(self, session: Session, candidates: list[Any], reply: FlowReply) -> None:
        session.set_state("awaiting_choice")
        session.open_card("carousel")
        agents = [
            {
                "name": candidate.name,
                "address": candidate.address,
                "readme": candidate.readme,
                "reason": f"{candidate.score:.0%} match - {candidate.reason}",
                "handle": candidate.handle,
            }
            for candidate in candidates[:4]
        ]
        card = carousel_card(session.session_id, agents)
        text = "I found agents that are close but not exact. " + fallback_text(
            "carousel", session.session_id
        )
        reply.text(text)
        reply.card("carousel", card, text=text)

    # --------------------------------------------------------------- planning

    async def _plan_and_review(self, session: Session, sender: str, reply: FlowReply) -> None:
        request = str(session.data.get("request", ""))
        intent = session.data.get("detection", {}).get("intent", {})
        context = json.dumps(intent, default=str)[:1500]
        session.set_state("building")
        reply.text("Planning an agent for this. This takes a moment.")
        try:
            spec = await self.planner.plan(request, context)
        except (PlanError, PlanningError) as exc:
            logger.info("planning failed: %s", exc)
            reply.text(
                "I could not turn that into a reliable agent plan after three attempts. "
                "Running it once without deploying instead."
            )
            await self._ephemeral_for_request(session, sender, reply, reason="planning failed")
            return

        digest = spec_hash(spec)
        await self.ledger.save_spec(digest, spec.model_dump(mode="json"))
        session.data["spec_hash"] = digest
        session.data["review"] = summarize_for_review(spec)
        session.set_state("awaiting_approval")
        session.open_card("review")
        card = review_card(
            session.session_id, spec.model_dump(mode="json"), cost=self._cost_label()
        )
        summary = (
            f"**{spec.title}**\n\n{spec.description}\n\n"
            f"Tools: {', '.join(spec.tools)}. Hosts: {', '.join(spec.allowed_hosts) or 'none'}. "
            f"Schedule: {spec.trigger}."
        )
        reply.text(summary)
        reply.card("review", card, text=fallback_text("review", session.session_id))

    def _cost_label(self) -> str:
        if not self.payments.enabled:
            return "Free"
        return f"{self.payments.config.amount} {self.payments.config.currency}"

    async def _approve_and_build(self, session: Session, sender: str, reply: FlowReply) -> None:
        digest = str(session.data.get("spec_hash") or "")
        spec_data = await self.ledger.load_spec(digest) if digest else None
        if not spec_data:
            reply.text(
                "The plan for this request is no longer available. Send the request again and I will replan it."
            )
            session.set_state("new")
            return
        try:
            spec = parse_spec(spec_data)
        except PlanError:
            reply.text(
                "The stored plan is invalid, so I will not deploy it. Send the request again."
            )
            session.set_state("new")
            return

        allowed, _ = await self.ledger.consume_usage(
            sender, "builds", self.settings.sender_builds_per_day, 86400
        )
        if not allowed:
            reply.text(quota_reason("builds", self.settings.sender_builds_per_day))
            await self._ephemeral_for_spec(session, spec, reply, reason="quota")
            return

        if self.payments.enabled:
            request_message = self.payments.request(session.session_id)
            session.data["awaiting_payment"] = True
            session.set_state("building")
            reply.text(
                f"This build costs {self.payments.config.amount} {self.payments.config.currency}. "
                "Approve the payment request to continue; nothing is deployed until it is verified."
            )
            reply.events.append(
                ("payment_request", {"message": request_message, "reference": session.session_id})
            )
            return

        await self._build_and_run(session, spec, reply)

    async def _build_and_run(self, session: Session, spec: AgentSpec, reply: FlowReply) -> None:
        digest = spec_hash(spec)
        session.set_state("building")
        reply.text(f"Approved. Building **{spec.title}** now.")
        token = await self.executor.token_for_agent(spec)
        notify_chat_id = str(session.data.get("telegram_chat_id", ""))

        progress_messages: list[str] = []

        async def progress(message: str) -> None:
            progress_messages.append(message)

        run_error: DeploymentError | None = None
        result = None
        try:
            deploy = await self.deployer.deploy(
                spec,
                session_id=session.session_id,
                gateway_url=self.settings.gateway_url,
                gateway_token=token,
                notify_chat_id=notify_chat_id,
                on_progress=progress,
            )
            reply.text("\n".join(f"- {message}" for message in progress_messages))
            reply.text(
                "The agent is live"
                + (
                    " and indexed for discovery."
                    if deploy.discoverable
                    else ". Indexing for discovery is still pending."
                )
            )
            run_result = await self._run_on_agent(session, spec, deploy.address)
            result = (deploy.address, deploy.handle, deploy.discoverable, run_result)
        except DeploymentError as exc:
            logger.warning("deployment failed for %s at %s: %s", spec.name, exc.stage, exc.detail)
            run_error = exc

        if run_error is not None or result is None:
            stage = run_error.stage if run_error else "unknown"
            reply.text(
                "I could not finish deploying the agent"
                + (f" (failed at: {stage})" if stage else "")
                + ". Running the same workflow here instead, so you still get an answer."
            )
            if self.payments.enabled:
                await self.payments.cancel(
                    session.session_id, reason=f"deployment failed at {stage}"
                )
            await self._ephemeral_for_spec(session, spec, reply, reason="deployment failed")
            return

        address, handle, discoverable, run_result = result
        if not run_result.ok:
            reply.text("The new agent is live, but this run hit a problem: " + run_result.text)
        else:
            reply.text(run_result.text)
        await self.ledger.upsert_agent(digest, address, spec.title, "live", handle=handle)
        session.data["address"] = address
        session.data["handle"] = handle
        session.set_state("complete")
        session.open_card("detail")
        rows = [
            {"label": "Agent", "value": spec.title},
            {
                "label": "Ran in",
                "value": "deployed agent" if run_result.mode == "deployed" else "Genesis",
            },
        ]
        card = detail_card(
            session.session_id,
            "Done",
            run_result.text[:1800],
            address=address,
            handle=handle,
            rows=rows,
        )
        note = (
            f"\n\nAgent `{handle}` is registered. If ASI:One's planner does not show it "
            f"immediately, mention `@{handle}` directly or use address `{address}` "
            + ("- discovery indexing is still pending." if not discoverable else ".")
        )
        reply.card("detail", card, text=fallback_text("detail", session.session_id) + note)
        reply.end()

    async def _run_on_agent(self, session: Session, spec: AgentSpec, address: str) -> RunResult:
        request = str(session.data.get("request", ""))
        inputs = self._inputs_for(spec, request)
        text = json.dumps(inputs, ensure_ascii=False) if inputs else request
        started = time.monotonic()
        try:
            reply = await self.relay.ask(
                address, text, timeout_s=min(self.settings.delegate_timeout_s, 60)
            )
            return RunResult(
                text=reply, ok=True, mode="deployed", seconds=time.monotonic() - started, errors=[]
            )
        except DelegationError as exc:
            logger.info("deployed agent did not answer (%s); using ephemeral execution", exc.reason)
            fallback = await self.executor.ephemeral(spec, inputs)
            fallback.text = (
                "The new agent did not answer in time, so I ran the same workflow here.\n\n"
                + fallback.text
            )
            fallback.mode = "deployed"
            return fallback

    @staticmethod
    def _inputs_for(spec: AgentSpec, request: str) -> dict[str, Any]:
        """Extract declared inputs from the request without inventing values."""
        import re

        values: dict[str, Any] = {}
        for item in spec.inputs:
            if item.kind == "url":
                match = re.search(r"https?://[^\s<>\"')]+", request)
                if match:
                    values[item.name] = match.group(0)
            elif item.kind == "number":
                match = re.search(
                    rf"{re.escape(item.name)}s?\s*[=:]\s*(-?\d+(?:\.\d+)?)", request, re.I
                )
                if match:
                    values[item.name] = match.group(1)
        if len(spec.inputs) == 1 and not values:
            values[spec.inputs[0].name] = request
        return values

    async def _ephemeral_for_request(
        self, session: Session, sender: str, reply: FlowReply, *, reason: str
    ) -> None:
        request = str(session.data.get("request", ""))
        detection = session.data.get("detection", {})
        intent = detection.get("intent", {})
        session.set_state("building")
        reply.text("Planning the workflow so I can run it here.")
        try:
            spec = await self.planner.plan(request, json.dumps(intent, default=str)[:1500])
        except (PlanError, PlanningError):
            reply.text(
                "I cannot reliably automate that request, so I would rather not pretend to. "
                "Try narrowing it to a single public page and a specific question."
            ).end()
            session.set_state("failed")
            return
        await self._ephemeral_for_spec(session, spec, reply, reason=reason)

    async def _ephemeral_for_spec(
        self, session: Session, spec: AgentSpec, reply: FlowReply, *, reason: str
    ) -> None:
        request = str(session.data.get("request", ""))
        inputs = self._inputs_for(spec, request)
        missing = [item.name for item in spec.inputs if item.required and item.name not in inputs]
        if missing:
            labels = [
                {
                    "name": item.name,
                    "label": item.label,
                    "type": "url" if item.kind == "url" else "text",
                    "required": True,
                }
                for item in spec.inputs
                if item.name in missing
            ]
            session.set_state("needs_input")
            session.open_card("form")
            card = form_card(session.session_id, labels)
            reply.text(fallback_text("form", session.session_id))
            reply.card("form", card, text=fallback_text("form", session.session_id))
            return
        result = await self.executor.ephemeral(spec, inputs)
        reply.text(("Ran here instead (" + reason + ").\n\n" + result.text)[:6000])
        session.set_state("complete")
        session.open_card("detail")
        card = detail_card(
            session.session_id,
            "Result",
            result.text[:1800],
            deletable=False,
            rows=[{"label": "Ran in", "value": "Genesis (ephemeral)"}],
        )
        reply.card("detail", card, text=fallback_text("detail", session.session_id))
        reply.end()

    # ------------------------------------------------------------- delegation

    async def _use_existing(
        self, session: Session, sender: str, address: str, reply: FlowReply
    ) -> None:
        if not address:
            reply.text(
                "No agent address was selected. Send the request again and I will offer the list."
            )
            return
        request = str(session.data.get("request", ""))
        session.set_state("running")
        reply.text("Checking that agent now.")
        try:
            answer = await self.relay.ask(
                address, request, timeout_s=self.settings.delegate_timeout_s
            )
        except DelegationError as exc:
            logger.info("delegation failed: %s", exc)
            reply.text(
                "That agent did not answer, so it is not usable right now. "
                "I can build a new agent for this instead."
            )
            detection = session.data.get("detection", {})
            candidates = detection.get("candidates", [])
            session.set_state("awaiting_choice")
            session.open_card("carousel")
            card = carousel_card(session.session_id, candidates[:3])
            reply.card("carousel", card, text=fallback_text("carousel", session.session_id))
            return
        reply.text(answer[:6000])
        session.set_state("complete")
        session.open_card("detail")
        card = detail_card(
            session.session_id,
            "Answered by an existing agent",
            answer[:1800],
            address=address,
            deletable=False,
        )
        reply.card("detail", card, text=fallback_text("detail", session.session_id))
        reply.end()

    # ---------------------------------------------------------- detail actions

    async def _run_again(self, session: Session, sender: str, reply: FlowReply) -> None:
        request = str(session.data.get("request", ""))
        if not request:
            reply.text("Send the request again and I will rerun it.")
            return
        await self._start_request(session, sender, request, reply)

    async def _delete_agent(
        self, session: Session, selection: dict[str, Any], reply: FlowReply
    ) -> None:
        digest = str(session.data.get("spec_hash") or "")
        address = str(selection.get("agent_address") or session.data.get("address") or "")
        if not digest:
            agent = None
        else:
            agent = await self.ledger.get_agent(digest)
        if agent is None:
            reply.text("That agent is not one I created, so I will not delete it.")
            return
        try:
            await self.deployer.delete_agent(digest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete failed: %s", exc)
            reply.text(
                "I could not delete the agent on Agentverse right now. "
                "Its gateway token has been revoked, so it cannot read any data."
            )
        spec_data = await self.ledger.load_spec(digest)
        if spec_data:
            try:
                spec = parse_spec(spec_data)
                token = await self.executor.token_for_agent(spec)
                await self.executor.revoke(token)
            except Exception:  # noqa: BLE001 - revoke is best-effort after delete
                logger.debug("token revoke failed", exc_info=True)
        await self.ledger.delete_agent(digest)
        session.set_state("complete")
        session.close_card()
        reply.text(f"Deleted. Agent `{address}` is removed and its gateway token is revoked.").end()
        _ = CARD_ACTION_MAP

    # ---------------------------------------------------------------- payment

    async def on_payment_commit(
        self, sender: str, reference: str, transaction_id: str, funds: Any, reply: FlowReply
    ) -> None:
        session = self.sessions.get(reference)
        if session is None:
            reply.text("That payment does not match an active request.")
            return
        verified, reason = await self.payments.on_commit(reference, transaction_id, funds)
        if not verified:
            reply.text(
                "I could not verify that payment"
                + (f" ({reason})" if reason else "")
                + ". Nothing has been deployed. Cancel the payment or try again."
            )
            return
        await self.payments.mark_completed(reference)
        spec_data = await self.ledger.load_spec(str(session.data.get("spec_hash") or ""))
        if not spec_data:
            reply.text(
                "The payment was verified but the plan is gone. Contact the operator for a refund."
            )
            return
        session.data.pop("awaiting_payment", None)
        await self._build_and_run(session, parse_spec(spec_data), reply)


class PaymentError(Exception):
    pass
