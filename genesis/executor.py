"""Task execution: on the deployed agent, or ephemerally inside Genesis.

Both paths run the *same* audited interpreter, so the fallback behaves exactly
like the agent that would have been built. Ephemeral runs use a short-lived,
narrowly scoped gateway token minted for the run.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from genesis.config import Settings
from genesis.errors import GatewayCallError
from genesis.gateway_client import SyncGateway
from genesis.spec import AgentSpec
from runtime.engine import StepError, run_spec


@dataclass
class RunResult:
    text: str
    ok: bool
    mode: str  # ephemeral | deployed
    seconds: float
    errors: list[str]


def _tools_for(spec: AgentSpec) -> list[str]:
    tools = {"fetch", "extract", "search", "llm", "notify"} & set(spec.tools)
    return sorted(tools)


class Executor:
    def __init__(self, settings: Settings, gateway_secret: str) -> None:
        self.settings = settings
        self.gateway_secret = gateway_secret

    def ephemeral_token(self, spec: AgentSpec, ttl_s: int = 600) -> tuple[str, str]:
        from gateway.auth import make_token

        agent_id = f"ephemeral-{spec.name}"
        token = make_token(
            self.gateway_secret,
            agent_id,
            tools=_tools_for(spec),
            hosts=spec.allowed_hosts,
            ttl_s=ttl_s,
        )
        return agent_id, token

    def run_ephemeral_sync(
        self, spec: AgentSpec, inputs: dict[str, Any], state: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        agent_id, token = self.ephemeral_token(spec)
        gateway = SyncGateway(
            self.settings.gateway_url,
            agent_id,
            token,
            hosts=spec.allowed_hosts,
            timeout_s=self.settings.gateway_timeout_s,
        )
        try:
            return run_spec(spec.model_dump(), inputs, gateway.call, state)
        finally:
            gateway.close()

    async def ephemeral(
        self, spec: AgentSpec, inputs: dict[str, Any], state: dict[str, Any] | None = None
    ) -> RunResult:
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.run_ephemeral_sync, spec, inputs, state), timeout=75
            )
        except TimeoutError:
            return RunResult(
                text="That took too long to finish. Try a narrower request or a single page.",
                ok=False,
                mode="ephemeral",
                seconds=time.monotonic() - started,
                errors=["run timed out"],
            )
        except StepError as exc:
            return RunResult(
                text=_friendly_step_error(exc),
                ok=False,
                mode="ephemeral",
                seconds=time.monotonic() - started,
                errors=[str(exc)],
            )
        except GatewayCallError as exc:
            return RunResult(
                text="The tool service is unavailable right now, so I could not run the task.",
                ok=False,
                mode="ephemeral",
                seconds=time.monotonic() - started,
                errors=[str(exc)],
            )
        return RunResult(
            text=str(result["output"]),
            ok=True,
            mode="ephemeral",
            seconds=time.monotonic() - started,
            errors=list(result.get("errors") or []),
        )

    async def token_for_agent(self, spec: AgentSpec, ttl_s: int = 90 * 86400) -> str:
        from gateway.auth import make_token

        return make_token(
            self.gateway_secret,
            spec.name,
            tools=_tools_for(spec),
            hosts=spec.allowed_hosts,
            ttl_s=ttl_s,
        )

    async def revoke(self, token: str) -> bool:
        import httpx

        admin = getattr(self.settings, "gateway_admin_secret", "")
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.settings.gateway_url.rstrip('/')}/admin/revoke",
                json={"token": token},
                headers={"X-Genesis-Admin": admin},
            )
            return response.status_code == 200

    @staticmethod
    def inputs_from_text(spec: AgentSpec, text: str) -> dict[str, Any]:
        """Map free text onto declared inputs without guessing URLs."""
        declared = [item.name for item in spec.inputs]
        if not declared:
            return {}
        if len(declared) == 1:
            return {declared[0]: text.strip()}
        return {}


def _friendly_step_error(exc: StepError) -> str:
    tool = exc.tool
    if tool == "fetch":
        return "I could not fetch that page. Check that the URL is public and reachable."
    if tool == "extract":
        return "I fetched the document but could not read usable text from it (it may be scanned or empty)."
    if tool == "search":
        return "Web search is unavailable right now, so I could not complete this task."
    if tool == "llm":
        return "The analysis step failed (the language service returned an error)."
    return f"The task failed at step '{exc.step_id}'."
