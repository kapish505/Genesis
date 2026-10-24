"""Spec planning with a bounded repair loop.

The model is asked for an AgentSpec as JSON. Every response goes through the
same Pydantic model the runtime uses, and each rejection is fed back with the
validation error. After ``max_repairs`` the caller gets a ``PlanningError`` and
the flow falls back to ephemeral execution.

The spec model itself lives in ``genesis.spec``; this module only orchestrates
the conversation with the model.
"""

from __future__ import annotations

import logging
from typing import Any

from genesis.errors import PlanningError
from genesis.llm import AsiOneClient
from genesis.spec import AgentSpec, PlanError, canonical_json, parse_spec, spec_hash

logger = logging.getLogger(__name__)

__all__ = ["Planner", "AgentSpec", "parse_spec", "canonical_json", "spec_hash", "PlanError"]

REFUSAL_NAME = "unsupported-request"


class Planner:
    def __init__(self, llm: AsiOneClient, max_repairs: int = 3) -> None:
        self.llm = llm
        self.max_repairs = max(0, max_repairs)

    async def plan(self, request: str, context: str = "") -> AgentSpec:
        last_error = ""
        for attempt in range(self.max_repairs + 1):
            data = await self._ask(request, context, last_error)
            try:
                spec = self._from_response(data)
            except PlanError as exc:
                last_error = str(exc)
                logger.info("planner attempt %d rejected: %s", attempt + 1, last_error[:300])
                continue
            return spec
        raise PlanningError(
            f"could not produce a valid spec after {self.max_repairs + 1} attempts: {last_error[:300]}"
        )

    async def _ask(self, request: str, context: str, error: str) -> dict[str, Any]:
        try:
            return await self.llm.plan_json(request, context, error)
        except Exception as exc:  # noqa: BLE001 - LLMError and transport errors alike
            logger.warning("planner call failed: %s", exc)
            raise PlanningError(f"the planner model is unavailable: {exc}") from exc

    @staticmethod
    def _from_response(data: dict[str, Any]) -> AgentSpec:
        if not isinstance(data, dict):
            raise PlanError("planner did not return an object")
        if data.get("name") == REFUSAL_NAME:
            description = str(data.get("description") or "").strip()
            raise PlanError(
                description or "the planner judged this request impossible with the available tools"
            )
        return parse_spec(data)


def dry_run_note(spec: AgentSpec) -> str:
    """A compact description used in logs and review text."""
    tools = ", ".join(spec.tools)
    return f"{spec.name}: {len(spec.steps)} steps ({tools}), hosts {spec.allowed_hosts or 'none'}"
