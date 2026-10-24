"""Stdlib-only interpreter embedded into generated Agentverse agents."""

import re


def _resolve(value, inputs, outputs):
    if not isinstance(value, str):
        if isinstance(value, dict):
            return {key: _resolve(item, inputs, outputs) for key, item in value.items()}
        if isinstance(value, list):
            return [_resolve(item, inputs, outputs) for item in value]
        return value
    exact = re.fullmatch(r"[{][{](input|steps)[.]([a-zA-Z0-9_]+)(?:[.]out)?[}][}]", value)
    if exact:
        return (
            inputs.get(exact.group(2), "")
            if exact.group(1) == "input"
            else outputs.get(exact.group(2), "")
        )
    for match in re.finditer(r"[{][{](input|steps)[.]([a-zA-Z0-9_]+)(?:[.]out)?[}][}]", value):
        replacement = (
            inputs.get(match.group(2), "")
            if match.group(1) == "input"
            else outputs.get(match.group(2), "")
        )
        value = value.replace(match.group(0), str(replacement))
    return value


def hosted_execute(spec, inputs, call_gateway):
    outputs = {}
    state = {}
    for step in spec["steps"][:8]:
        args = _resolve(step.get("args", {}), inputs, outputs)
        tool = step["tool"]
        if tool == "diff_state":
            key = str(args.get("key", "default"))
            result = {
                "changed": state.get(key) != args.get("value"),
                "previous": state.get(key),
                "current": args.get("value"),
            }
            state[key] = args.get("value")
        elif tool == "filter":
            result = args.get("value", "")
            needle = args.get("contains")
            if needle is not None and str(needle).casefold() not in str(result).casefold():
                result = ""
        elif tool == "format":
            result = str(args.get("template", "{{value}}")).replace(
                "{{value}}", str(args.get("value", ""))
            )
        else:
            result = call_gateway(tool, args)
        outputs[step["id"]] = result
    return str(_resolve(spec.get("output_template", "{{steps.last.out}}"), inputs, outputs))[
        :200000
    ]
