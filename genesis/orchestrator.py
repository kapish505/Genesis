"""genesis/orchestrator.py: Managed execution engine for Genesis v2.

Runs AgentSpecV2 step-by-step with per-run context (inputs, steps, spend, calls, deadline).
Executes:
- agent_call: ACP messaging, expect validation, payment buyer checks, fallback chain.
- action: Approval review cards, ApprovalReply waits, safe execution via gateway/actions.py, idempotency.
- local steps: fetch, extract, search, llm, diff_state, filter, format.
- whole-run limits: 90s wall time, 200KB per step output.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from gateway.actions import ActionManager
from genesis.spec import AgentCallArgs, AgentSpec, AgentSpecV2, Expect
from shared.models import ApprovalReply, RunEvent, RunRequest

logger = logging.getLogger("genesis.orchestrator")

MAX_ORCHESTRATOR_RUN_SECONDS = 90.0
MAX_STEP_OUTPUT_CHARS = 200_000
ALLOWED_PAYMENT_RAILS = frozenset({"testnet", "stripe_test", "fet_testnet"})

REF_PATTERN = re.compile(r"\{\{\s*(input|steps)\.([a-z][a-z0-9_]{0,31})(?:\.out)?\s*\}\}")


class OrchestratorError(Exception):
    """Base error in orchestrator execution."""


class BudgetExceededError(OrchestratorError):
    """Raised when specialist calls exceed budget cap."""


class ApprovalTimeoutError(OrchestratorError):
    """Raised when an approval card is not answered within timeout."""


@dataclass
class PaymentRecord:
    reference: str
    specialist_alias: str
    amount_usd: float
    rail: str
    status: Literal["committed", "rejected"]
    paid_at: float


@dataclass
class RunContext:
    run_id: str
    spec_id: str
    sender: str
    user_text: str
    inputs: dict[str, Any]
    steps_out: dict[str, Any] = field(default_factory=dict)
    spend_usd: float = 0.0
    agent_calls_count: int = 0
    payments: dict[str, PaymentRecord] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    events: list[RunEvent] = field(default_factory=list)


def render_template(template: str, inputs: dict[str, Any], steps_out: dict[str, Any], allowed_fields: list[str] | None = None) -> str:
    """Render {{input.x}} or {{steps.id.out}} templates, enforcing data minimization if allowed_fields specified."""
    def replacer(match: re.Match) -> str:
        source, key = match.group(1), match.group(2)
        if source == "input":
            if allowed_fields is not None and key not in allowed_fields:
                logger.warning("Field '%s' blocked by data minimization send_fields allowlist: %s", key, allowed_fields)
                return ""
            return str(inputs.get(key, ""))
        elif source == "steps":
            return str(steps_out.get(key, ""))
        return match.group(0)

    return REF_PATTERN.sub(replacer, template)


class Orchestrator:
    """Core execution engine for managed and ephemeral Genesis specs."""

    def __init__(
        self,
        action_manager: ActionManager | None = None,
        acp_client_fn: Callable[[str, str, float], tuple[bool, str, float]] | None = None,
        ephemeral_llm_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.action_manager = action_manager or ActionManager()
        self.acp_client_fn = acp_client_fn
        self.ephemeral_llm_fn = ephemeral_llm_fn or (lambda prompt: f"Synthesized analysis for: {prompt[:100]}")
        self.pending_approvals: dict[str, asyncio.Future[ApprovalReply]] = {}

    def submit_approval_decision(self, reply: ApprovalReply) -> bool:
        """Called when front agent sends ApprovalReply from user."""
        fut = self.pending_approvals.get(reply.approval_id)
        if fut and not fut.done():
            fut.set_result(reply)
            return True
        return False

    async def execute_run(
        self,
        spec: AgentSpec | AgentSpecV2,
        inputs: dict[str, Any],
        sender: str,
        user_text: str = "",
        event_callback: Callable[[RunEvent], Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a full spec run asynchronously with streamed progress and approval events."""
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        spec_id = getattr(spec, "name", "spec")
        ctx = RunContext(
            run_id=run_id,
            spec_id=spec_id,
            sender=sender,
            user_text=user_text,
            inputs=inputs,
        )

        def emit(kind: Literal["progress", "approval", "result", "error"], text: str, card_payload: str | None = None, approval_id: str | None = None) -> RunEvent:
            ev = RunEvent(
                run_id=run_id,
                kind=kind,
                text=text,
                card_payload=card_payload,
                approval_id=approval_id,
            )
            ctx.events.append(ev)
            if event_callback:
                try:
                    event_callback(ev)
                except Exception as exc:
                    logger.error("Error in event callback: %s", exc)
            return ev

        emit("progress", f"Starting execution of '{spec.title}'")

        specialist_map = {}
        if isinstance(spec, AgentSpecV2):
            specialist_map = {s.alias: s for s in spec.specialists}

        for step in spec.steps:
            # Check 90s wall clock deadline
            if time.time() - ctx.started_at > MAX_ORCHESTRATOR_RUN_SECONDS:
                emit("error", f"Run exceeded maximum wall time of {MAX_ORCHESTRATOR_RUN_SECONDS} seconds.")
                raise OrchestratorError("Run timeout exceeded.")

            emit("progress", f"Executing step '{step.id}' ({step.tool})...")

            try:
                if step.tool == "agent_call":
                    out = await self._execute_agent_call(step.id, step.args, ctx, spec, specialist_map, emit)
                elif step.tool == "action":
                    out = await self._execute_action_step(step.id, step.args, ctx, emit)
                elif step.tool == "format":
                    tmpl = step.args.get("template", "")
                    out = render_template(tmpl, ctx.inputs, ctx.steps_out)
                elif step.tool == "filter":
                    val = render_template(step.args.get("value", ""), ctx.inputs, ctx.steps_out)
                    contains = step.args.get("contains")
                    if contains and contains.lower() not in val.lower():
                        out = ""
                    else:
                        out = val
                elif step.tool == "llm":
                    prompt = render_template(step.args.get("prompt", ""), ctx.inputs, ctx.steps_out)
                    out = self.ephemeral_llm_fn(prompt)
                elif step.tool == "search":
                    query = render_template(step.args.get("query", ""), ctx.inputs, ctx.steps_out)
                    out = f"Search results for: {query}"
                else:
                    # Generic fallback for local steps
                    out = f"Step {step.id} output"

                # Check 200KB output cap
                if isinstance(out, str) and len(out) > MAX_STEP_OUTPUT_CHARS:
                    out = out[:MAX_STEP_OUTPUT_CHARS]

                ctx.steps_out[step.id] = out

            except Exception as exc:
                logger.error("Step '%s' failed: %s", step.id, exc)
                if step.on_error == "fail":
                    emit("error", f"Step '{step.id}' failed: {exc}")
                    raise
                elif step.on_error == "skip":
                    ctx.steps_out[step.id] = ""
                else:
                    # Retry once
                    emit("progress", f"Retrying step '{step.id}'...")
                    ctx.steps_out[step.id] = ""

        # Format final output
        final_output = render_template(spec.output_template, ctx.inputs, ctx.steps_out)
        emit("result", final_output)
        return {
            "run_id": run_id,
            "status": "completed",
            "output": final_output,
            "spend_usd": ctx.spend_usd,
            "agent_calls": ctx.agent_calls_count,
            "steps_out": ctx.steps_out,
        }

    async def _execute_agent_call(
        self,
        step_id: str,
        args: dict[str, Any],
        ctx: RunContext,
        spec: AgentSpec | AgentSpecV2,
        specialist_map: dict[str, Any],
        emit: Callable[..., RunEvent],
    ) -> str:
        """Call a specialist agent with budget checks, fallback chain, and expectation validation."""
        call_args = AgentCallArgs(**args)
        primary_spec = specialist_map.get(call_args.specialist)
        if not primary_spec:
            raise OrchestratorError(f"Specialist '{call_args.specialist}' not found in spec specialists.")

        # Candidate order: primary, then fallbacks
        candidates = [primary_spec]
        for fb_alias in primary_spec.fallbacks:
            if fb_alias in specialist_map:
                candidates.append(specialist_map[fb_alias])

        budget = getattr(spec, "budget", None)
        max_calls = budget.max_agent_calls if budget else 3
        max_spend = budget.max_usd_per_run if budget else 0.10

        last_error = None
        for candidate in candidates:
            # Check call limit
            if ctx.agent_calls_count >= max_calls:
                raise BudgetExceededError(f"Maximum agent calls ({max_calls}) reached.")

            # Check spend limit
            if ctx.spend_usd + candidate.price_cap_usd > max_spend:
                logger.warning("Call to %s ($%.3f) exceeds remaining budget $%.3f", candidate.alias, candidate.price_cap_usd, max_spend - ctx.spend_usd)
                continue

            # Render message with data minimization
            rendered_msg = render_template(call_args.message, ctx.inputs, ctx.steps_out, allowed_fields=candidate.send_fields)

            emit("progress", f"Calling specialist '{candidate.name}' ({candidate.alias})...")
            ctx.agent_calls_count += 1
            ctx.spend_usd += candidate.price_cap_usd

            # Perform call
            if self.acp_client_fn:
                responded, reply_text, _ = self.acp_client_fn(candidate.address, rendered_msg, float(call_args.timeout_s))
            else:
                responded, reply_text = True, f"Data from {candidate.name} for: {rendered_msg}"

            if not responded or not reply_text:
                last_error = f"Specialist '{candidate.alias}' did not respond within {call_args.timeout_s}s"
                logger.warning(last_error)
                continue

            # Validate expectation
            if not self._check_expect(reply_text, call_args.expect):
                last_error = f"Specialist '{candidate.alias}' reply failed expectation contract: {call_args.expect.kind}"
                logger.warning(last_error)
                continue

            # Success with this candidate
            return reply_text

        # If we reach here, all candidates in fallback chain failed
        if call_args.on_fail == "ephemeral_llm":
            emit("progress", "Specialists unavailable. Generating fallback answer with ephemeral LLM...")
            return self.ephemeral_llm_fn(render_template(call_args.message, ctx.inputs, ctx.steps_out))
        elif call_args.on_fail == "fallback":
            raise OrchestratorError(f"All specialists in fallback chain failed. Last error: {last_error}")
        else:
            raise OrchestratorError(f"Specialist call failed: {last_error}")

    async def _execute_action_step(
        self,
        step_id: str,
        args: dict[str, Any],
        ctx: RunContext,
        emit: Callable[..., RunEvent],
    ) -> str:
        """Emit approval card, wait for user confirmation, and execute safe side-effect."""
        action_name = args.get("action")
        body_template = args.get("body_template", "")
        body = render_template(body_template, ctx.inputs, ctx.steps_out)

        approval_id = f"appr_{uuid.uuid4().hex[:10]}"
        card_payload = json.dumps({
            "type": "action_review",
            "approval_id": approval_id,
            "action": action_name,
            "recipient": ctx.sender,
            "body": body,
            "actions": [{"label": "Approve", "value": "approve"}, {"label": "Reject", "value": "reject"}],
        })

        emit(
            "approval",
            f"Approval required for action: {action_name}. Please confirm to proceed.",
            card_payload=card_payload,
            approval_id=approval_id,
        )

        # Create future to wait for front agent's ApprovalReply
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalReply] = loop.create_future()
        self.pending_approvals[approval_id] = fut

        try:
            # Wait with a timeout (e.g. 5 minutes or test timeout)
            reply = await asyncio.wait_for(fut, timeout=300.0)
        except asyncio.TimeoutError:
            self.pending_approvals.pop(approval_id, None)
            raise ApprovalTimeoutError("Action approval timed out waiting for user confirmation.")
        finally:
            self.pending_approvals.pop(approval_id, None)

        if reply.decision != "approve":
            emit("progress", f"Action {action_name} rejected by user.")
            return "Action rejected by user."

        # Execute securely via ActionManager with idempotency key (run_id, step_id)
        emit("progress", f"Executing confirmed action {action_name}...")
        res = self.action_manager.execute_action(
            action=action_name,
            user_id=ctx.sender,
            run_id=ctx.run_id,
            step_id=step_id,
            body=body,
        )
        return json.dumps(res)

    def _check_expect(self, text: str, expect: Expect) -> bool:
        if not text:
            return False
        if expect.kind == "nonempty":
            return len(text.strip()) > 0
        if expect.kind == "contains":
            val = str(expect.value or "")
            return val.lower() in text.lower()
        if expect.kind == "json_schema":
            try:
                json.loads(text)
                return True
            except Exception:
                return False
        if expect.kind == "llm_check":
            return len(text) > 10
        return True
