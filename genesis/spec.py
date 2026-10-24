"""AgentSpec: the validated, versioned contract between planner and runtime.

Supports both AgentSpec (v1 local engine) and AgentSpecV2 (v2 orchestrator & managed mode).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from runtime.engine import MAX_STEPS, SpecError, validate_spec

ToolName = Literal[
    "fetch",
    "extract",
    "search",
    "llm",
    "diff_state",
    "filter",
    "format",
    "agent_call",
    "action",
]

RESERVED_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".localhost",
    ".localdomain",
    ".home.arpa",
    ".test",
    ".invalid",
    ".example",
)

TOOL_ARGS: dict[str, frozenset[str]] = {
    "fetch": frozenset({"url", "max_bytes", "headers"}),
    "extract": frozenset({"url", "content", "format", "query", "max_chars"}),
    "search": frozenset({"query", "max_results"}),
    "llm": frozenset({"prompt", "system", "input", "max_tokens", "temperature"}),
    "diff_state": frozenset({"key", "value"}),
    "filter": frozenset({"value", "contains", "not_contains"}),
    "format": frozenset({"template", "value", "title"}),
    "agent_call": frozenset({"specialist", "message", "expect", "timeout_s", "on_fail"}),
    "action": frozenset({"action", "body_template"}),
}

TOOL_PERMISSIONS: dict[str, str] = {
    "fetch": "network_read",
    "extract": "network_read",
    "search": "network_read",
    "llm": "llm",
    "action": "notify",
}


class InputField(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    kind: Literal["text", "url", "number", "email", "select"]
    label: str = Field(min_length=1, max_length=80)
    required: bool = True
    options: list[str] | None = None

    @model_validator(mode="after")
    def _options_match_kind(self) -> InputField:
        if self.kind == "select" and not self.options:
            raise ValueError("select inputs need options")
        if self.kind != "select" and self.options:
            raise ValueError("only select inputs may declare options")
        return self


class Expect(BaseModel):
    kind: Literal["nonempty", "contains", "json_schema", "llm_check"]
    value: str | dict[str, Any] | None = None


class SpecialistRef(BaseModel):
    alias: str = Field(pattern=r"^[a-z][a-z0-9_]{1,23}$")
    address: str = Field(pattern=r"^agent1q[a-z0-9]{20,80}$")
    name: str
    readme_sha256: str
    vet_score: float = Field(ge=0, le=1)
    vetted_at: datetime
    price_cap_usd: float = Field(ge=0, le=1.0)
    send_fields: list[str] = Field(max_length=6)
    fallbacks: list[str] = []


class AgentCallArgs(BaseModel):
    specialist: str
    message: str = Field(max_length=1500)
    expect: Expect
    timeout_s: int = Field(default=30, ge=5, le=60)
    on_fail: Literal["fallback", "ephemeral_llm", "fail"] = "fallback"


class ActionArgs(BaseModel):
    action: Literal["telegram_send", "email_send_self"]
    body_template: str = Field(max_length=800)


class Budget(BaseModel):
    max_usd_per_run: float = Field(default=0.10, ge=0, le=0.5)
    max_agent_calls: int = Field(default=3, ge=0, le=4)


class Step(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")
    tool: ToolName
    args: dict[str, Any]
    on_error: Literal["fail", "skip", "retry"] = "retry"

    @model_validator(mode="after")
    def _arg_names(self) -> Step:
        allowed = TOOL_ARGS.get(self.tool, frozenset())
        unknown = set(self.args) - allowed
        if unknown:
            raise ValueError(f"tool '{self.tool}' does not accept {sorted(unknown)}")
        if self.tool in ("fetch", "extract"):
            if not self.args.get("url") and not (
                self.tool == "extract" and self.args.get("content")
            ):
                raise ValueError(f"tool '{self.tool}' requires either url or content")
        if self.tool == "search" and not self.args.get("query"):
            raise ValueError("search requires a query")
        if self.tool == "llm" and not (self.args.get("prompt") or self.args.get("input")):
            raise ValueError("llm requires a prompt or input")
        if self.tool == "filter" and "value" not in self.args:
            raise ValueError("filter requires a value")
        if self.tool == "format" and not self.args.get("template"):
            raise ValueError("format requires a template")
        if self.tool == "diff_state" and "value" not in self.args:
            raise ValueError("diff_state requires a value")
        if self.tool == "agent_call":
            AgentCallArgs(**self.args)
        if self.tool == "action":
            ActionArgs(**self.args)
        return self


class SelfTest(BaseModel):
    inputs: dict[str, str]
    expect_nonempty: bool = True
    expect_contains: list[str] = Field(default_factory=list, max_length=4)


class AgentSpec(BaseModel):
    spec_version: Literal["1"] = "1"
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,47}$")
    title: str = Field(min_length=3, max_length=60)
    description: str = Field(min_length=20, max_length=400)
    use_cases: list[str] = Field(min_length=2, max_length=3)
    limitations: list[str] = Field(min_length=1, max_length=4)
    domain_tag: str = Field(pattern=r"^[a-z][a-z0-9-]{1,23}$")
    inputs: list[InputField] = Field(default_factory=list, max_length=6)
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)
    output_template: str = Field(min_length=1, max_length=2000)
    trigger: Literal["on_demand", "interval"] = "on_demand"
    interval_seconds: int | None = Field(default=None, ge=300, le=86400)
    notify: Literal["chat", "telegram", "none"] = "chat"
    allowed_hosts: list[str] = Field(default_factory=list, max_length=8)
    permissions: list[Literal["network_read", "llm", "storage", "notify"]] = Field(
        default_factory=list
    )
    selftest: SelfTest
    mode: Literal["local", "managed"] = "local"

    @field_validator("allowed_hosts")
    @classmethod
    def _public_hostnames(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for host in value:
            host = host.strip().casefold().rstrip(".")
            if not host or "*" in host or "/" in host or ":" in host:
                raise ValueError(
                    "allowed_hosts must be exact hostnames without wildcards, paths or ports"
                )
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("allowed_hosts must not contain IP literals")
            if "." not in host:
                raise ValueError(f"allowed_hosts entry '{host}' is not a public hostname")
            if host == "localhost" or host.endswith(RESERVED_HOST_SUFFIXES):
                raise ValueError(f"allowed_hosts entry '{host}' is local or reserved")
            if host not in cleaned:
                cleaned.append(host)
        return cleaned

    @model_validator(mode="after")
    def _consistent(self) -> AgentSpec:
        if self.trigger == "interval" and self.interval_seconds is None:
            raise ValueError("interval_seconds is required when trigger is interval")
        if self.trigger == "on_demand" and self.interval_seconds is not None:
            raise ValueError("interval_seconds is only valid when trigger is interval")
        if self.notify == "telegram" and "notify" not in self.permissions:
            raise ValueError("telegram notifications require the notify permission")
        tools = {step.tool for step in self.steps}
        for tool, permission in TOOL_PERMISSIONS.items():
            if tool in tools and permission not in self.permissions:
                raise ValueError(f"tool '{tool}' requires the {permission} permission")
        if "fetch" in tools and not self.allowed_hosts:
            raise ValueError("fetch steps need at least one entry in allowed_hosts")
        if "search" in tools and "llm" not in tools and "format" not in tools:
            raise ValueError("search results need an llm or format step to be useful")
        is_managed = any(step.tool in ("agent_call", "action", "code") for step in self.steps)
        if not is_managed:
            try:
                validate_spec(self.as_spec_dict())
            except SpecError as exc:
                raise ValueError(str(exc)) from exc
        declared = {item.name for item in self.inputs}
        for key in self.selftest.inputs:
            if key not in declared:
                raise ValueError(f"selftest input '{key}' is not declared")
        missing = {item.name for item in self.inputs if item.required} - set(self.selftest.inputs)
        if missing:
            raise ValueError(f"selftest is missing required inputs {sorted(missing)}")
        return self

    def as_spec_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def tools(self) -> list[str]:
        return sorted({step.tool for step in self.steps})


class AgentSpecV2(AgentSpec):
    """v2 Orchestrator Specification extending AgentSpec with specialists, budget, and managed mode."""

    spec_version: Literal["2"] = "2"
    specialists: list[SpecialistRef] = Field(default_factory=list, max_length=4)
    budget: Budget = Field(default_factory=Budget)
    mode: Literal["local", "managed"] = "local"

    @model_validator(mode="after")
    def derive_and_check(self) -> AgentSpecV2:
        managed = any(s.tool in ("agent_call", "action", "code") for s in self.steps)
        self.mode = "managed" if managed else "local"

        aliases = {s.alias for s in self.specialists}
        addresses = {s.address for s in self.specialists}
        if len(addresses) != len(self.specialists):
            raise ValueError("duplicate specialist address declared in specialists list")

        agent_call_count = 0
        action_count = 0
        for s in self.steps:
            if s.tool == "agent_call":
                agent_call_count += 1
                a = AgentCallArgs(**s.args)
                if a.specialist not in aliases:
                    raise ValueError(f"agent_call step '{s.id}' references unknown specialist '{a.specialist}'")
            if s.tool == "action":
                action_count += 1
                ActionArgs(**s.args)

        if agent_call_count > 4:
            raise ValueError(f"exceeded maximum of 4 agent_call steps (got {agent_call_count})")
        if action_count > 2:
            raise ValueError(f"exceeded maximum of 2 action steps (got {action_count})")

        # actions only after all non-action steps (no side effect before analysis succeeds)
        tools = [s.tool for s in self.steps]
        if "action" in tools:
            first_action = tools.index("action")
            if not all(t == "action" for t in tools[first_action:]):
                raise ValueError("action steps must only appear after all non-action steps")

        return self


def canonical_json(spec: AgentSpec | AgentSpecV2) -> str:
    return json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def spec_hash(spec: AgentSpec | AgentSpecV2) -> str:
    return hashlib.sha256(canonical_json(spec).encode()).hexdigest()


class PlanError(ValueError):
    """The planner produced something that is not a valid AgentSpec."""


def parse_spec(value: str | dict[str, Any]) -> AgentSpec | AgentSpecV2:
    try:
        data: Any = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise PlanError(f"response was not JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise PlanError("response must be a JSON object")

    spec_version = str(data.get("spec_version", "1"))
    try:
        if spec_version == "2":
            return AgentSpecV2.model_validate(data)
        return AgentSpec.model_validate(data)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc


SPEC_JSON_SCHEMA: dict[str, Any] = AgentSpec.model_json_schema()
SPEC_V2_JSON_SCHEMA: dict[str, Any] = AgentSpecV2.model_json_schema()
