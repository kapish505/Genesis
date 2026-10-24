"""Deployment saga for hosted agents.

Every transition is persisted *before* the side effect it guards, and every step
is idempotent on restart:

* ``PLANNED``  — build row created
* ``APPROVED`` — the user approved; no side effects yet
* ``CREATED``  — Agentverse agent exists (address recorded before upload)
* ``CODE_UPLOADED`` — code accepted (code hash recorded)
* ``STARTED``  — process started
* ``PROBE_OK`` — the live self-test replied correctly
* ``DISCOVERABLE`` / ``DONE`` — indexing confirmed (or timed out and reported)
* ``FAILED`` / ``COMPENSATED`` — terminal failure, half-built agent removed

A sweeper resumes builds that were interrupted (crash or restart) and compensates
those that cannot be resumed. All retries are bounded and only cover
429/5xx/timeouts.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from genesis.builder import Builder
from genesis.config import Settings
from genesis.errors import DeploymentError
from genesis.ledger import Ledger
from genesis.search import AgentverseClient, AgentverseError
from genesis.spec import AgentSpec, spec_hash
from genesis.verifier import VerificationError, evaluate_probe, static_verify

logger = logging.getLogger(__name__)

Probe = Callable[[str, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class DeployResult:
    address: str
    handle: str
    discoverable: bool
    build_id: str


class Deployer:
    def __init__(
        self,
        client: AgentverseClient,
        ledger: Ledger,
        builder: Builder,
        settings: Settings,
        probe: Probe,
    ) -> None:
        self.client = client
        self.ledger = ledger
        self.builder = builder
        self.settings = settings
        self.probe = probe

    # ------------------------------------------------------------------ saga

    async def deploy(
        self,
        spec: AgentSpec,
        *,
        session_id: str,
        gateway_url: str,
        gateway_token: str,
        notify_chat_id: str = "",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> DeployResult:
        digest = spec_hash(spec)

        async def progress(message: str) -> None:
            if on_progress is not None:
                try:
                    await on_progress(message)
                except Exception:  # noqa: BLE001 - progress must never fail a build
                    logger.debug("progress callback failed", exc_info=True)

        agent = await self.ledger.get_agent(digest)
        if agent and agent["status"] == "live":
            await self.ledger.touch_agent(digest)
            return DeployResult(agent["agent_address"], agent.get("handle") or "", True, "reused")

        active = await self.ledger.active_build_for_spec(digest)
        if active is not None:
            # Edge case 6: attach to the running build instead of starting a second one.
            await progress("A build for this exact agent is already running; joining it.")
            return await self._await_active(active["build_id"], digest)

        await self.ledger.save_spec(digest, spec.model_dump(mode="json"))
        build_id = uuid.uuid4().hex
        await self.ledger.create_build(build_id, digest, session_id)
        return await self._run(build_id, spec, gateway_url, gateway_token, notify_chat_id, progress)

    async def _await_active(
        self, build_id: str, digest: str, timeout_s: float = 600.0
    ) -> DeployResult:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            row = await self.ledger.get_build(build_id)
            if row is None:
                break
            if row["state"] in {"DONE", "DISCOVERABLE"} and row.get("agent_address"):
                return DeployResult(
                    row["agent_address"],
                    row.get("handle") or "",
                    row["state"] == "DISCOVERABLE",
                    build_id,
                )
            if row["state"] in {"FAILED", "COMPENSATED"}:
                raise DeploymentError(
                    row.get("failure_reason") or "the concurrent build failed",
                    stage=row.get("failure_stage") or "deploy",
                )
            await asyncio.sleep(2)
        agent = await self.ledger.get_agent(digest)
        if agent and agent["status"] == "live":
            return DeployResult(agent["agent_address"], agent.get("handle") or "", True, build_id)
        raise DeploymentError("the concurrent build did not finish in time", stage="join")

    async def _run(
        self,
        build_id: str,
        spec: AgentSpec,
        gateway_url: str,
        gateway_token: str,
        notify_chat_id: str,
        progress: Callable[[str], Awaitable[None]],
        *,
        repair_note: str = "",
        attempt: int = 1,
    ) -> DeployResult:
        digest = spec_hash(spec)
        address: str | None = None
        try:
            if repair_note and attempt > 1:
                await progress(f"Repairing the agent after a failed self-test (attempt {attempt}).")
            row = await self.ledger.get_build(build_id)
            state = row["state"] if row else "PLANNED"
            address = row.get("agent_address") if row else None

            await progress("Creating the hosted agent on Agentverse.")
            if state in {"PLANNED", "APPROVED"} or not address:
                created = await self._retry(lambda: self.client.create_agent(spec.name), "create")
                address = str(created.get("address") or "")
                handle = str(created.get("name") or spec.name)
                if not address:
                    raise DeploymentError(
                        "Agentverse did not return an agent address", stage="create"
                    )
                await self.ledger.transition_build(
                    build_id, "CREATED", address=address, handle=handle
                )
                await self.ledger.upsert_agent(
                    digest, address, spec.title, "building", handle=handle
                )
            else:
                await self.ledger.transition_build(build_id, "CREATED", address=address)

            artifact = self.builder.build(
                spec,
                gateway_url,
                gateway_token,
                notify_chat_id=notify_chat_id,
                address=address,
                handle=spec.name,
            )
            report = static_verify(spec, artifact)
            if not report.ok:
                raise VerificationError("static", "; ".join(report.problems))

            await progress("Uploading the generated agent code.")
            await self._retry(lambda: self.client.upload_code(address, artifact.source), "upload")
            await self.ledger.transition_build(
                build_id, "CODE_UPLOADED", address=address, code_hash=artifact.code_hash
            )

            await progress("Starting the agent.")
            await self._retry(lambda: self.client.start_agent(address), "start")
            await self.ledger.transition_build(build_id, "STARTED", address=address)

            await progress("Running the agent's self-test.")
            reply = await self._probe_with_timeout(address, spec)
            ok, reason = evaluate_probe(spec, reply)
            if not ok:
                raise VerificationError("probe", reason)
            await self.ledger.transition_build(
                build_id, "PROBE_OK", address=address, detail=reply[:500]
            )

            await progress("Checking that the agent is indexed for discovery.")
            discoverable = await self._wait_discoverable(spec, address)
            await self.ledger.transition_build(
                build_id, "DISCOVERABLE" if discoverable else "DONE", address=address
            )
            await self.ledger.upsert_agent(digest, address, spec.title, "live", handle=spec.name)
            if spec.trigger == "interval" and spec.interval_seconds:
                await self.ledger.set_runtime_state(
                    digest,
                    {"next_run_at": time.time() + spec.interval_seconds, "last_error": ""},
                    0,
                )
            return DeployResult(address, spec.name, discoverable, build_id)
        except VerificationError as exc:
            await self.ledger.transition_build(build_id, "FAILED", failure=(exc.stage, exc.message))
            await progress(
                "The deployed agent failed verification; retrying the plan once."
                if attempt == 1
                else "The agent failed verification twice; I will answer without deploying."
            )
            raise DeploymentError(exc.message, stage=exc.stage) from exc
        except Exception as exc:
            retryable = bool(getattr(exc, "retryable", False))
            stage = _stage_from_state(
                (await self.ledger.get_build(build_id) or {}).get("state", "")
            )
            await self.ledger.transition_build(build_id, "FAILED", failure=(stage, str(exc)))
            if address:
                await progress("Cleaning up the partially created agent.")
                await self._compensate(build_id, address)
            raise DeploymentError(str(exc), stage=stage, retryable=retryable) from exc

    async def _compensate(self, build_id: str, address: str) -> None:
        for action in (self.client.stop_agent, self.client.delete_agent):
            try:
                await action(address)
            except Exception as exc:  # noqa: BLE001 - compensation is best-effort
                logger.warning("compensation step failed for %s: %s", address, exc)
        await self.ledger.transition_build(build_id, "COMPENSATED", address=address)
        row = await self.ledger.get_build(build_id)
        if row:
            await self.ledger.set_agent_status(row["spec_hash"], "deleted")

    async def _retry(self, action: Callable[[], Awaitable[Any]], stage: str) -> Any:
        last: Exception | None = None
        for attempt in range(3):
            try:
                return await action()
            except AgentverseError as exc:
                last = exc
                if not exc.retryable:
                    raise
                await asyncio.sleep(2**attempt)
        raise DeploymentError(str(last), stage=stage, retryable=True)

    async def _probe_with_timeout(self, address: str, spec: AgentSpec) -> str:
        try:
            return await asyncio.wait_for(
                self.probe(address, dict(spec.selftest.inputs)),
                timeout=self.settings.probe_timeout_s,
            )
        except TimeoutError as exc:
            raise VerificationError(
                "probe", "the agent did not reply before the probe timeout"
            ) from exc

    async def _wait_discoverable(self, spec: AgentSpec, address: str = "") -> bool:
        deadline = time.monotonic() + max(0, self.settings.discovery_timeout_s)
        words = " ".join(spec.title.split()[:6])
        while time.monotonic() < deadline:
            try:
                results = await self.client.search(words, limit=10)
            except AgentverseError as exc:
                logger.warning("discovery poll failed: %s", exc)
                results = []
            if any(
                str(item.get("address", "")) == address or str(item.get("name", "")) == spec.name
                for item in results
            ):
                return True
            await asyncio.sleep(5)
        return False

    async def repair(
        self,
        spec: AgentSpec,
        address: str,
        *,
        gateway_url: str,
        gateway_token: str,
        notify_chat_id: str = "",
    ) -> tuple[bool, str]:
        """Redeploy repaired code to an existing agent (edge case 17)."""
        artifact = self.builder.build(
            spec, gateway_url, gateway_token, notify_chat_id=notify_chat_id, address=address
        )
        report = static_verify(spec, artifact)
        if not report.ok:
            return False, "; ".join(report.problems)
        try:
            await self.client.upload_code(address, artifact.source)
            await self.client.start_agent(address)
        except AgentverseError as exc:
            return False, str(exc)
        try:
            reply = await self._probe_with_timeout(address, spec)
        except VerificationError as exc:
            return False, exc.message
        ok, reason = evaluate_probe(spec, reply)
        return ok, reason or reply

    # --------------------------------------------------------------- sweeper

    async def sweep(self) -> list[str]:
        """Resume or compensate builds interrupted by a crash. Returns build ids touched."""
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=self.settings.build_resume_after_s)
        ).isoformat()
        touched: list[str] = []
        for row in await self.ledger.resumable_builds(cutoff):
            build_id = row["build_id"]
            digest = row["spec_hash"]
            spec_data = await self.ledger.load_spec(digest)
            address = row.get("agent_address")
            if address and row["state"] in {"CREATED", "CODE_UPLOADED", "STARTED", "PROBE_OK"}:
                # The agent exists but the build never finished: compensate to avoid
                # orphans, then let the next request rebuild cleanly.
                await self._compensate(build_id, address)
                touched.append(build_id)
                continue
            if address:
                await self._compensate(build_id, address)
            else:
                await self.ledger.transition_build(
                    build_id,
                    "FAILED",
                    failure=("resume", "interrupted before an agent was created"),
                )
            touched.append(build_id)
            if spec_data and row["state"] in {"STARTED", "PROBE_OK"}:
                logger.info("compensated interrupted build %s for spec %s", build_id, digest[:12])
        return touched

    async def delete_agent(self, spec_hash_value: str) -> bool:
        agent = await self.ledger.get_agent(spec_hash_value)
        if not agent:
            return False
        address = agent["agent_address"]
        try:
            await self.client.stop_agent(address)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stop failed for %s: %s", address, exc)
        try:
            await self.client.delete_agent(address)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete failed for %s: %s", address, exc)
            raise
        await self.ledger.set_agent_status(spec_hash_value, "deleted")
        return True


def _stage_from_state(state: str) -> str:
    return {
        "PLANNED": "create",
        "APPROVED": "create",
        "CREATED": "upload",
        "CODE_UPLOADED": "start",
        "STARTED": "probe",
        "PROBE_OK": "discovery",
    }.get(state, "deploy")
