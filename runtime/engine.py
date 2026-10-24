"""Audited AgentSpec interpreter.

This module is embedded verbatim into every generated hosted agent, so it must use
the standard library only and must never import anything from ``genesis`` or
``gateway``. It is synchronous by design: Genesis runs it in a worker thread and
hosted agents call it from a synchronous ACP handler.

Security properties enforced here:

* The spec is data. Nothing in it is evaluated as Python.
* Template references are resolved against declared inputs and prior steps only.
* Step count, LLM step count, per-step and per-run deadlines, and per-step output
  size are hard caps independent of what the planner produced.
* Untrusted remote text is never interpreted as a template.
"""

from __future__ import annotations

import copy
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

MAX_STEPS = 8
MAX_LLM_STEPS = 3
MAX_STEP_SECONDS = 20.0
MAX_RUN_SECONDS = 60.0
MAX_STEP_OUTPUT = 200_000

LOCAL_TOOLS = frozenset({"diff_state", "filter", "format"})
GATEWAY_TOOLS = frozenset({"fetch", "extract", "search", "llm"})
STEP_TOOLS = LOCAL_TOOLS | GATEWAY_TOOLS

REF_PATTERN = re.compile(r"\{\{\s*(input|steps)\.([a-z][a-z0-9_]{0,31})(?:\.out)?\s*\}\}")


class SpecError(ValueError):
    """The spec cannot be interpreted (malformed references or limits exceeded)."""


class StepError(RuntimeError):
    """A step failed in a way the user should see."""

    def __init__(self, step_id: str, tool: str, message: str, retryable: bool = False) -> None:
        super().__init__(f"step '{step_id}' ({tool}) failed: {message}")
        self.step_id = step_id
        self.tool = tool
        self.retryable = retryable


def _iter_refs(value: Any) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    if isinstance(value, str):
        refs.extend((m.group(1), m.group(2)) for m in REF_PATTERN.finditer(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            refs.extend(_iter_refs(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            refs.extend(_iter_refs(item))
    return refs


def validate_spec(spec: Mapping[str, Any]) -> None:
    """Static reference and limit validation; raises SpecError on any problem."""
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        raise SpecError("spec has no steps")
    if len(steps) > MAX_STEPS:
        raise SpecError(f"spec has {len(steps)} steps; the maximum is {MAX_STEPS}")
    if (
        sum(1 for step in steps if isinstance(step, Mapping) and step.get("tool") == "llm")
        > MAX_LLM_STEPS
    ):
        raise SpecError(f"spec has more than {MAX_LLM_STEPS} llm steps")

    declared_inputs = {
        item.get("name")
        for item in spec.get("inputs") or []
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    seen: dict[str, int] = {}
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise SpecError(f"step {index} is not an object")
        step_id = step.get("id")
        tool = step.get("tool")
        if not isinstance(step_id, str) or not step_id:
            raise SpecError(f"step {index} has no id")
        if step_id in seen:
            raise SpecError(f"duplicate step id '{step_id}'")
        if tool not in STEP_TOOLS:
            raise SpecError(f"step '{step_id}' uses unsupported tool '{tool}'")
        seen[step_id] = index
        for namespace, name in _iter_refs(step.get("args") or {}):
            if namespace == "input":
                if name not in declared_inputs:
                    raise SpecError(f"step '{step_id}' references undeclared input '{name}'")
            else:
                if name not in seen or seen[name] >= index:
                    raise SpecError(
                        f"step '{step_id}' references step '{name}' that has not run yet"
                    )
    for namespace, name in _iter_refs(spec.get("output_template") or ""):
        if namespace == "input":
            if name not in declared_inputs:
                raise SpecError(f"output_template references undeclared input '{name}'")
        elif name not in seen:
            raise SpecError(f"output_template references unknown step '{name}'")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "content", "output", "results"):
            if key in value:
                return _as_text(value[key])
        try:
            import json

            return json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(item) for item in value)
    return str(value)


def resolve(value: Any, inputs: Mapping[str, Any], outputs: Mapping[str, Any]) -> Any:
    """Resolve ``{{input.x}}`` / ``{{steps.id.out}}`` templates in a spec value.

    An exact match preserves the referenced value's type; an inline match is
    string-interpolated. Text that happens to contain braces in remote content is
    never re-resolved because resolution happens before execution only.
    """
    if isinstance(value, Mapping):
        return {key: resolve(item, inputs, outputs) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [resolve(item, inputs, outputs) for item in value]
    if not isinstance(value, str):
        return value

    def _lookup(match: re.Match[str]) -> Any:
        namespace, name = match.group(1), match.group(2)
        source = inputs if namespace == "input" else outputs
        if name not in source:
            raise SpecError(f"unresolved reference '{match.group(0)}'")
        return source[name]

    exact = REF_PATTERN.fullmatch(value)
    if exact:
        return _lookup(exact)
    return REF_PATTERN.sub(lambda match: _as_text(_lookup(match)), value)


def _run_local(tool: str, args: Mapping[str, Any], state: dict[str, Any]) -> Any:
    if tool == "diff_state":
        key = str(args.get("key") or "default")
        current = _as_text(args.get("value"))
        previous = state.get(key)
        state[key] = current
        return {"changed": previous != current, "previous": previous, "current": current}
    if tool == "filter":
        value = _as_text(args.get("value"))
        contains = args.get("contains")
        if contains not in (None, ""):
            needles = [part for part in _as_text(contains).split("|") if part]
            if not any(needle.casefold() in value.casefold() for needle in needles):
                return ""
        not_contains = args.get("not_contains")
        if not_contains not in (None, ""):
            if any(
                part.casefold() in value.casefold()
                for part in _as_text(not_contains).split("|")
                if part
            ):
                return ""
        return value
    if tool == "format":
        template = _as_text(args.get("template"))
        replacements = {key: _as_text(item) for key, item in args.items() if key != "template"}
        for key, item in replacements.items():
            template = template.replace("{{" + key + "}}", item)
        return template.replace("{{value}}", _as_text(args.get("value")))
    raise SpecError(f"unsupported local tool '{tool}'")


def run_spec(
    spec: Mapping[str, Any],
    inputs: Mapping[str, Any],
    call_gateway: Callable[[str, dict[str, Any]], Any],
    state: dict[str, Any] | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Execute a spec and return ``{"output", "steps", "errors", "state"}``.

    ``call_gateway(tool, args)`` must return JSON-serialisable data or raise an
    exception exposing ``retryable`` (see ``StepError``). ``state`` is the
    persistent memory used by ``diff_state`` and is mutated in place.
    """
    validate_spec(spec)
    started = clock()
    outputs: dict[str, Any] = {}
    errors: list[str] = []
    state = state if state is not None else {}

    for step in spec["steps"]:
        step_id = str(step["id"])
        tool = str(step["tool"])
        if clock() - started > MAX_RUN_SECONDS:
            raise StepError(step_id, tool, "the run exceeded its 60 s budget")
        args = resolve(step.get("args") or {}, inputs, outputs)
        if not isinstance(args, dict):
            raise SpecError(f"step '{step_id}' args did not resolve to an object")
        policy = step.get("on_error", "retry")
        attempts = 2 if policy == "retry" else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            step_started = clock()
            try:
                if tool in LOCAL_TOOLS:
                    result = _run_local(tool, args, state)
                else:
                    result = call_gateway(tool, args)
                if clock() - step_started > MAX_STEP_SECONDS:
                    raise StepError(step_id, tool, "step exceeded its 20 s budget")
                if len(_as_text(result).encode()) > MAX_STEP_OUTPUT:
                    text = _as_text(result)
                    result = text[: MAX_STEP_OUTPUT // 4] + "\n...[truncated]"
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - surfaced as a step error below
                last_error = exc
                retryable = bool(getattr(exc, "retryable", True))
                if attempt + 1 >= attempts or not retryable:
                    break
                sleep(0.5)
        if last_error is not None:
            message = str(last_error)
            if policy == "skip":
                errors.append(f"{step_id}: {message}")
                outputs[step_id] = ""
                continue
            raise StepError(
                step_id, tool, message, retryable=bool(getattr(last_error, "retryable", False))
            )
        outputs[step_id] = result

    output = _as_text(resolve(spec.get("output_template") or "", inputs, outputs))
    if len(output.encode()) > MAX_STEP_OUTPUT:
        output = output[: MAX_STEP_OUTPUT // 4] + "\n...[truncated]"
    return {"output": output, "steps": copy.deepcopy(outputs), "errors": errors, "state": state}
