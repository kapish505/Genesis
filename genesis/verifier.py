"""Verification: static checks before deploy, a live probe after.

Static verification is deterministic and cheap, so it runs on every plan and
again immediately before upload. The live probe is the only thing that can
declare an agent usable, and its inputs come from the spec's own selftest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from genesis.builder import BuildArtifact
from genesis.spec import AgentSpec


class VerificationError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class StaticReport:
    ok: bool
    problems: list[str]


BANNED_PATTERNS = (
    (
        r"\b(credential|password|api key|otp|one[- ]time code|2fa|login as|sign in as)\b",
        "needs credentials or a login",
    ),
    (
        r"\b(pay|purchase|buy|checkout|transfer funds|wallet seed|private key)\b",
        "needs payments on the user's behalf",
    ),
    (
        r"\b(scrape (all|every|thousands)|mass download|crawl the entire)\b",
        "needs scraping at a scale the platform does not allow",
    ),
    (
        r"\b(bypass|circumvent|evade)(?:\s+\w+)?\s+(auth|login|captcha|detection|rate limit)\b",
        "asks to bypass platform protections",
    ),
    (r"\b(doxx|harass|stalk|spam)\b", "targets people rather than data"),
    (r"\b(exploit|malware|ransomware|keylogger|reverse shell|ddos)\b", "is abusive"),
    (
        r"\b(localhost|127\.0\.0\.1|169\.254\.169\.254|metadata service|private network|\.internal)\b",
        "targets a private network",
    ),
)

PRIVATE_HOST_PATTERN = re.compile(
    r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|\[?::1\]?$)"
)


def check_request_text(text: str) -> tuple[bool, str]:
    lowered = text.casefold()
    for pattern, reason in BANNED_PATTERNS:
        if re.search(pattern, lowered):
            return False, reason
    return True, ""


def static_verify(spec: AgentSpec, artifact: BuildArtifact | None = None) -> StaticReport:
    problems: list[str] = []
    if spec.steps and len(spec.steps) > 8:
        problems.append("more than 8 steps")
    if sum(step.tool == "llm" for step in spec.steps) > 3:
        problems.append("more than 3 llm steps")
    if any(PRIVATE_HOST_PATTERN.search(host) for host in spec.allowed_hosts):
        problems.append("allowed_hosts contains a private address")
    if spec.trigger == "interval" and not spec.interval_seconds:
        problems.append("interval agent without interval_seconds")
    if spec.notify == "telegram" and "notify" not in spec.permissions:
        problems.append("telegram notification without notify permission")
    if artifact is not None:
        if len(artifact.source.encode()) > 256 * 1024:
            problems.append("generated source is too large")
        if artifact.code_hash == "":
            problems.append("artifact has no code hash")
    return StaticReport(ok=not problems, problems=problems)


def evaluate_probe(spec: AgentSpec, reply: str) -> tuple[bool, str]:
    """Judge a probe reply against the spec's declared expectations."""
    if spec.selftest.expect_nonempty and not reply.strip():
        return False, "the agent replied with an empty message"
    lowered = reply.casefold()
    for expected in spec.selftest.expect_contains:
        if expected.casefold() not in lowered:
            return False, f"the reply did not contain the expected text '{expected}'"
    error_markers = (
        "could not complete this task",
        "needs:",
        "tool fetch failed",
        "tool extract failed",
        "traceback",
    )
    for marker in error_markers:
        if marker in lowered:
            return False, f"the reply reported a failure: {reply[:200]}"
    return True, ""


def repair_note(spec: AgentSpec, reply: str) -> str:
    """Feedback fed to the planner when the live probe fails."""
    return (
        "The deployed agent failed its self-test. "
        f"Self-test inputs: {spec.selftest.inputs!r}. Agent reply: {reply[:600]!r}. "
        "Return a corrected AgentSpec. Common causes: wrong URL shape in the fetch/extract "
        "step, an llm prompt that does not include {{steps.<id>.out}}, or expects that the "
        "page does not contain."
    )


def summarize_for_review(spec: AgentSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "title": spec.title,
        "description": spec.description,
        "steps": [{"id": step.id, "tool": step.tool} for step in spec.steps],
        "allowed_hosts": list(spec.allowed_hosts),
        "trigger": spec.trigger,
        "interval_seconds": spec.interval_seconds,
        "notify": spec.notify,
        "permissions": list(spec.permissions),
    }
