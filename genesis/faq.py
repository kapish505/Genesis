"""Direct self-capability answering for Genesis.

Prevents Genesis from misreading questions about its own architecture,
circuit breakers, budget caps, ephemeral execution, and data isolation
as tasks to search or deploy external agents.
"""

from __future__ import annotations

import re

# Topic patterns
CIRCUIT_BREAKER_PATTERN = re.compile(
    r"\b(circuit breaker|fail(ing|s|ure)?|poor results?|stop(ping)? (the )?spending|spending automatically|halt spending|drift)\b",
    re.I,
)
EPHEMERAL_PATTERN = re.compile(
    r"\b(temporar(y|ily)|once|single run|without (permanently )?adding|add(ing)? (all the )?specialists to my agent list|ephemeral|keep this agent)\b",
    re.I,
)
DATA_ISOLATION_PATTERN = re.compile(
    r"\b(sensitive|api keys?|protect (my )?data|private|data privacy|leak(ing)?|send_fields|isolation|credentials?)\b",
    re.I,
)
BUDGET_PATTERN = re.compile(
    r"\b(fet budget|approve (or cap )?the costs?|budget cap|spending limits?|cost approval|before you spend|how do i approve)\b",
    re.I,
)
VETTING_PATTERN = re.compile(
    r"\b(synthetic probe|vetting|how do you vet|scoring formula|score specialists|trust score)\b",
    re.I,
)
CAPABILITIES_PATTERN = re.compile(
    r"\b(what (can you do|are your capabilities)|how does genesis work|who are you|what is genesis)\b",
    re.I,
)


def answer_meta_question(text: str) -> str | None:
    """Return a direct explanation if the text is asking about Genesis itself."""
    cleaned = text.strip()

    # 1. Circuit breakers & specialist failure
    if CIRCUIT_BREAKER_PATTERN.search(cleaned) and (
        "?" in cleaned or any(w in cleaned.lower() for w in ["what", "how", "do your", "can", "if"])
    ):
        return (
            "Yes, absolutely. Genesis has built-in circuit breakers and automated drift monitors:\n\n"
            "1. Automatic Spending Halt: If a hired specialist fails, times out (>8s), violates its contract, "
            "or returns degraded results mid-task, Genesis immediately trips the circuit breaker and halts all further spending.\n"
            "2. Protected Micropayments: Hired specialists are only paid for steps that successfully complete and "
            "pass schema verification. Unused funds from your budget cap remain completely safe.\n"
            "3. Fallback Chains: When a specialist fails, Genesis can automatically switch to a pre-vetted secondary "
            "specialist or fall back to tool gateway web extraction without runaway costs."
        )

    # 2. Ephemeral vs permanent execution
    if EPHEMERAL_PATTERN.search(cleaned):
        return (
            "Yes! Genesis operates in 'Ephemeral First' mode by default:\n\n"
            "1. In-Memory Managed Execution: Multi-agent workflows run ephemerally inside Genesis. Specialists are "
            "hired and coordinated dynamically for your single run without permanently adding them to your Agentverse agent list.\n"
            "2. Zero Clutter: Your Agentverse dashboard stays completely clean.\n"
            "3. Optional Promotion: If you want to keep the workflow permanently, simply click 'Keep this agent' on "
            "the final result card to promote it to a permanent hosted front agent."
        )

    # 3. Sensitive data isolation & API keys
    if DATA_ISOLATION_PATTERN.search(cleaned) and (
        "?" in cleaned or any(w in cleaned.lower() for w in ["how", "protect", "isolate", "safe", "want"])
    ):
        return (
            "Genesis enforces strict Sensitive Data Minimization through allowlisted `send_fields`:\n\n"
            "1. Field Allowlisting: Third-party specialists only receive parameters strictly declared in their "
            "subtask contract (e.g., public query strings or tokens). They never see unrelated session context.\n"
            "2. Credential Isolation: Sensitive values like private API keys, wallet seeds, and personal info are "
            "stripped and never forwarded to external specialists.\n"
            "3. Review Card Transparency: Before execution, Genesis displays a Review Card detailing the exact "
            "fields that will be sent to external agents for your explicit approval."
        )

    # 4. Budget cap & cost approval
    if BUDGET_PATTERN.search(cleaned):
        return (
            "Genesis puts you in full control of all spending before any specialist is hired:\n\n"
            "1. Strict Budget Caps: Every plan enforces a hard maximum FET budget cap (e.g., 0.05 FET). "
            "Genesis will never exceed this budget under any circumstances.\n"
            "2. Interactive Review Card: Before any specialists are called or micropayments occur, Genesis renders "
            "an ACP Review Card detailing the selected specialists, per-step fees, and total cap.\n"
            "3. Explicit Human Approval: Execution and spending are held until you explicitly reply APPROVE or tap the card button."
        )

    # 5. Synthetic probes & vetting
    if VETTING_PATTERN.search(cleaned):
        return (
            "Genesis evaluates candidate specialists on Agentverse using live synthetic ACP probes before hiring:\n\n"
            "Scoring Formula (0.0 to 1.0):\n"
            "• 40% Contract Adherence: Schema validation and structured field compliance.\n"
            "• 20% Repeatability: Deterministic consistency across multiple probe rounds.\n"
            "• 20% Latency: Fast execution (sub-2s responses score highest).\n"
            "• 10% Historical Reliability: Error rates recorded in the local SQLite ledger.\n"
            "• 10% Intent Fit: Semantic capability match to your specific request.\n\n"
            "Scores are cached for 24 hours with active health drift detection."
        )

    # 6. Capabilities overview
    if CAPABILITIES_PATTERN.search(cleaned):
        return (
            "Genesis is an Autonomous Specialist Broker and Orchestrator for ASI:One and Agentverse:\n\n"
            "• Discovers & Vets Specialists: Uses live synthetic probes and multi-factor scoring.\n"
            "• Managed Orchestration: Wires specialists into execution plans with sensitive data isolation.\n"
            "• Budget Protection: Enforces strict FET budget caps, human-in-the-loop approvals, and circuit breakers.\n"
            "• Ephemeral First: Runs workflows in-memory without polluting your agent list, with single-click promotion."
        )

    return None
