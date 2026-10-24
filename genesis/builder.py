"""Build a hosted agent from an AgentSpec.

The LLM never contributes code. The builder renders a fixed template, inlines
the audited engine, substitutes JSON-escaped configuration, and then *proves*
the result is acceptable before it can be uploaded:

* every marker is replaced (no unresolved placeholders);
* the module parses and compiles;
* every top-level import is on the hosted allowlist;
* the source and its SHA-256 are recorded for verification and idempotent updates.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from genesis.errors import GenesisError
from genesis.spec import AgentSpec, canonical_json
from runtime.agent_template import AGENT_TEMPLATE

MARKERS = (
    "__SPEC_JSON__",
    "__GATEWAY_URL__",
    "__GATEWAY_TOKEN__",
    "__AGENT_ID__",
    "__AGENT_NAME__",
    "__NOTIFY_CHAT_ID__",
    "__AGENT_TITLE__",
    "__ENGINE_SOURCE__",
    "__INTERVAL_HANDLER__",
)

ALLOWED_IMPORTS = frozenset(
    {
        # standard library
        "__future__",
        "ast",
        "base64",
        "collections",
        "copy",
        "datetime",
        "email",
        "enum",
        "functools",
        "hashlib",
        "html",
        "io",
        "json",
        "logging",
        "math",
        "os",
        "random",
        "re",
        "string",
        "sys",
        "textwrap",
        "time",
        "typing",
        "unicodedata",
        "urllib",
        "uuid",
        # hosted allowlist
        "requests",
        "uagents",
        "uagents_core",
    }
)

MAX_SOURCE_BYTES = 256 * 1024

INTERVAL_TEMPLATE = """@agent.on_interval(period=float(__INTERVAL_SECONDS__))
async def scheduled_run(ctx: Context):
    if not NOTIFY_CHAT_ID:
        ctx.logger.warning("scheduled run skipped: no notification target configured")
        return
    try:
        inputs = __SELFTEST_INPUTS__
        text = execute(ctx, inputs)
        _notify(ctx, text)
    except Exception as exc:
        ctx.logger.exception("scheduled run failed")
        _notify(ctx, "Scheduled run failed: " + str(exc)[:300])
"""


class BuildError(GenesisError):
    user_message = "I could not assemble a reliable agent from that plan."


@dataclass(frozen=True)
class BuildArtifact:
    source: str
    code_hash: str
    readme: str

    @property
    def size(self) -> int:
        return len(self.source.encode())


def _strip_future_import(source: str) -> str:
    return re.sub(r"^from __future__ import annotations\s*$", "", source, count=1, flags=re.M)


class Builder:
    def __init__(self, template: str | None = None, engine_path: Path | None = None) -> None:
        root = Path(__file__).resolve().parent.parent
        self.template = template if template is not None else AGENT_TEMPLATE
        self.engine_path = engine_path or root / "runtime" / "engine.py"

    # ---------------------------------------------------------------- render

    def render(
        self,
        spec: AgentSpec,
        gateway_url: str,
        gateway_token: str,
        *,
        notify_chat_id: str = "",
    ) -> str:
        engine_source = _strip_future_import(self.engine_path.read_text())
        name = spec.name
        interval = ""
        if spec.trigger == "interval" and spec.interval_seconds:
            interval = INTERVAL_TEMPLATE.replace(
                "__INTERVAL_SECONDS__", str(spec.interval_seconds)
            ).replace("__SELFTEST_INPUTS__", repr(dict(spec.selftest.inputs)))
        replacements = {
            "__SPEC_JSON__": repr(canonical_json(spec)),
            "__GATEWAY_URL__": repr(gateway_url.rstrip("/")),
            "__GATEWAY_TOKEN__": repr(gateway_token),
            "__AGENT_ID__": repr(name),
            "__AGENT_NAME__": repr(name),
            "__AGENT_TITLE__": spec.title.replace('"', "'").replace("\n", " "),
            "__NOTIFY_CHAT_ID__": repr(notify_chat_id or ""),
            "__INTERVAL_HANDLER__": interval,
            "__ENGINE_SOURCE__": engine_source,
        }
        source = self.template
        # Engine last: it must not be scanned for the other markers (it contains none,
        # but ordering keeps behaviour obvious if the engine ever grows examples).
        for marker in (
            "__SPEC_JSON__",
            "__GATEWAY_URL__",
            "__GATEWAY_TOKEN__",
            "__AGENT_ID__",
            "__AGENT_NAME__",
            "__AGENT_TITLE__",
            "__NOTIFY_CHAT_ID__",
            "__INTERVAL_HANDLER__",
        ):
            source = source.replace(marker, replacements[marker])
        source = source.replace("__ENGINE_SOURCE__", replacements["__ENGINE_SOURCE__"])
        remaining = [marker for marker in MARKERS if marker in source]
        if remaining:
            raise BuildError(f"unresolved template markers: {remaining}")
        self._validate_source(source)
        return source

    # -------------------------------------------------------------- validate

    @staticmethod
    def _validate_source(source: str) -> None:
        size = len(source.encode())
        if size > MAX_SOURCE_BYTES:
            raise BuildError(f"generated source is {size} bytes; the limit is {MAX_SOURCE_BYTES}")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise BuildError(f"generated agent does not parse: {exc.msg}") from exc
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
        disallowed = imports - ALLOWED_IMPORTS
        if disallowed:
            raise BuildError(f"generated agent imports disallowed modules: {sorted(disallowed)}")
        try:
            compile(source, "<generated-agent>", "exec")
        except SyntaxError as exc:  # pragma: no cover - ast.parse already covers this
            raise BuildError(f"generated agent does not compile: {exc.msg}") from exc
        if "from genesis" in source or "import genesis" in source:
            raise BuildError("generated agent must not import genesis")

    # ----------------------------------------------------------------- build

    def build(
        self,
        spec: AgentSpec,
        gateway_url: str,
        gateway_token: str,
        *,
        notify_chat_id: str = "",
        address: str = "",
        handle: str = "",
    ) -> BuildArtifact:
        source = self.render(spec, gateway_url, gateway_token, notify_chat_id=notify_chat_id)
        return BuildArtifact(
            source=source,
            code_hash=hashlib.sha256(source.encode()).hexdigest(),
            readme=self.readme(spec, address=address, handle=handle),
        )

    def build_files(
        self,
        spec: AgentSpec,
        out_dir: Path,
        gateway_url: str,
        gateway_token: str,
        **kwargs: Any,
    ) -> tuple[Path, Path]:
        artifact = self.build(spec, gateway_url, gateway_token, **kwargs)
        out_dir.mkdir(parents=True, exist_ok=True)
        agent_path = out_dir / "agent.py"
        readme_path = out_dir / "README.md"
        agent_path.write_text(artifact.source)
        readme_path.write_text(artifact.readme)
        return agent_path, readme_path

    # ---------------------------------------------------------------- readme

    @staticmethod
    def readme(spec: AgentSpec, *, address: str = "", handle: str = "") -> str:
        tools = ", ".join(spec.tools)
        inputs = spec.inputs
        input_rows = (
            "\n".join(
                f"| `{item.name}` | {item.kind} | {item.label} | {'yes' if item.required else 'no'} |"
                for item in inputs
            )
            or "| _none_ | - | This agent takes free text or needs no input. | - |"
        )
        schedule = "on demand"
        if spec.trigger == "interval" and spec.interval_seconds:
            schedule = f"every {spec.interval_seconds // 60} minutes"
        reach = []
        if handle:
            reach.append(f"- ASI:One handle: `@{handle}`")
        if address:
            reach.append(f"- Agent address: `{address}`")
        reach_block = "\n".join(reach) if reach else "- Ask for it by description in ASI:One."
        selftest = json.dumps(spec.selftest.inputs, ensure_ascii=False, indent=2)
        return f"""# {spec.title}

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3) ![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1) ![tag:domain/{spec.domain_tag}](https://img.shields.io/badge/{spec.domain_tag}-1F8A70)

{spec.description}

This agent was planned and deployed by Genesis and runs the audited Genesis spec engine. It executes a fixed workflow; it does not generate code or browse arbitrary sites.

## Use cases

{chr(10).join(f"- {item}" for item in spec.use_cases)}

## Inputs

| Name | Type | Description | Required |
|---|---|---|---|
{input_rows}

## Output

The agent replies in chat with the result formatted by:

```
{spec.output_template}
```

## How it works

| Step | Tool | Purpose |
|---|---|---|
{chr(10).join(f"| `{step.id}` | `{step.tool}` | {_purpose(step)} |" for step in spec.steps)}

Tools used: {tools}. Schedule: **{schedule}**. Network access is limited to: {", ".join(spec.allowed_hosts) or "none"}.

## How to reach it

{reach_block}

## Example request

```json
{selftest}
```

## Limitations

{chr(10).join(f"- {item}" for item in spec.limitations)}

## Safety

- Only the hostnames listed above can be fetched, and only through Genesis's SSRF-protected Tool Gateway.
- Fetched content is treated as untrusted data, never as instructions.
- The agent holds a scoped, revocable gateway token; it never sees platform or model API keys.
"""


def _purpose(step: Any) -> str:
    args = step.args
    if step.tool == "fetch":
        return f"fetch `{args.get('url', '')}`"
    if step.tool == "extract":
        return f"extract text from `{args.get('url') or 'provided content'}`"
    if step.tool == "search":
        return f"search for `{args.get('query', '')}`"
    if step.tool == "llm":
        prompt = str(args.get("prompt") or args.get("input") or "")
        return f"analyse: {prompt[:80].replace('|', '/').strip()}"
    if step.tool == "filter":
        return "keep only matching content"
    if step.tool == "diff_state":
        return "compare with the previous run"
    if step.tool == "format":
        return "format the final reply"
    return str(step.tool)
