"""Intent policy, quotas, abuse limits, and log redaction.

Two stages:

* ``screen`` runs before anything else and blocks abusive or private-network
  requests outright.
* ``classify_capability`` distinguishes "needs a capability Genesis does not
  have" (credentials, payments on the user's behalf, logged-in sites) from a
  normal build. Those are refused with an explanation and recorded so operators
  can see demand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from genesis.verifier import check_request_text

MAX_REQUEST_CHARS = 4000

CREDENTIAL_PATTERN = re.compile(
    r"\b(log ?in to|sign ?in to|use my (account|password|credentials)|enter the password|"
    r"authenticate as me|my bank|my email inbox|send (an )?email|post (this )?on my behalf)\b",
    re.I,
)
PAYMENT_PATTERN = re.compile(
    r"\b(pay (the )?bill|make a payment|buy (me|this|that)|place (an )?order|check ?out|"
    r"transfer money|use my card|purchase)\b",
    re.I,
)
PII_PATTERNS = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "<card>"),
    (re.compile(r"(?<!\d)(?:\+?\d[\d\s()-]{7,}\d)(?!\d)"), "<number>"),
)


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    reason: str = ""
    capability: str = ""

    @property
    def blocked_for_capability(self) -> bool:
        return self.allowed and bool(self.capability)


def screen(text: str) -> SafetyDecision:
    if not text.strip():
        return SafetyDecision(False, "Send me a task and I will take it from there.")
    if len(text) > MAX_REQUEST_CHARS:
        return SafetyDecision(
            False,
            f"That request is too long ({len(text)} characters). Keep it under {MAX_REQUEST_CHARS}.",
        )
    ok, reason = check_request_text(text)
    if not ok:
        return SafetyDecision(
            False,
            "I cannot take that one: it " + reason + ". "
            "I only work with public data through ordinary web requests.",
        )
    return SafetyDecision(True)


def classify_capability(text: str) -> SafetyDecision:
    if CREDENTIAL_PATTERN.search(text):
        return SafetyDecision(
            True,
            capability=(
                "This needs a login or actions on your account. Genesis has no way to hold your "
                "credentials or act as you, so I cannot build that."
            ),
        )
    if PAYMENT_PATTERN.search(text):
        return SafetyDecision(
            True,
            capability=(
                "This needs a purchase or payment on your behalf. Genesis cannot hold payment "
                "details or make purchases, so I cannot build that."
            ),
        )
    return SafetyDecision(True)


def redact(text: str) -> str:
    """Redact emails, phone-like numbers and card-like digit runs for logs."""
    for pattern, replacement in PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def quota_reason(bucket: str, limit: int) -> str:
    units = {
        "builds": ("per day", "agents built today"),
        "runs": ("per hour", "task runs this hour"),
    }
    window, noun = units.get(bucket, ("", "requests"))
    return f"You have used all {limit} {noun} {window}. Try again later."
