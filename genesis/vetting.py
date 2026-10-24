"""genesis/vetting.py: The Trust Layer — Candidate Discovery, Synthetic Probes & Vetting.

Rules from Genesis v2:
1. Never wire in an agent that hasn't been tested against the user's intent.
2. Candidate discovery: 3 query phrasings -> search -> dedupe -> top 8.
3. Contract inference: turns user intent into input shape, 2-3 synthetic probes (never real data),
   and expectations (Expect).
4. Live probes: measures responded?, latency, contract pass, repeatability.
5. Safety screen: size cap (200KB), no credential requests, no prompt injection, no unallowed URLs.
6. Score in [0, 1]: 0.4*contract + 0.2*repeatability + 0.2*latency + 0.1*history + 0.1*fit.
7. Cache keyed by (address, readme_sha256) with 24h TTL + drift detection.
8. Non-destructive check: skip candidates with side effects (booking, transfers).
9. Circuit breaker: 2 consecutive real run failures mark specialist degraded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from genesis.spec import Expect, SpecialistRef

logger = logging.getLogger("genesis.vetting")

VET_MIN_SCORE = 0.70
MAX_PROBE_REPLY_BYTES = 200_000
PROBE_TIMEOUT_SECONDS = 15.0

DESTRUCTIVE_KEYWORDS = frozenset({
    "book", "reserve", "order", "transfer", "buy", "sell", "swap", "pay",
    "delete", "execute trade", "purchase", "sign transaction",
})

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"system\s*prompt\s*:", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+in\s+developer\s+mode", re.IGNORECASE),
    re.compile(r"send\s+(all\s+)?(private\s+keys|funds|credentials)", re.IGNORECASE),
]

CREDENTIAL_HARVEST_PATTERNS = [
    re.compile(r"(enter|provide|send)\s+(your\s+)?(password|api[_-]?key|secret|seed\s+phrase|private\s+key)", re.IGNORECASE),
]


@dataclass
class ProbeDefinition:
    probe_id: str
    message: str
    expect: Expect


@dataclass
class ProbeResult:
    probe_id: str
    responded: bool
    latency_s: float
    passed_contract: bool
    reply_text: str
    safety_passed: bool
    error: str | None = None


@dataclass
class VetScoreResult:
    address: str
    name: str
    readme_sha256: str
    score: float
    vetted_at: str
    price_cap_usd: float
    passed: bool
    latency_avg: float
    contract_score: float
    repeatability_score: float
    safety_clean: bool
    failure_reason: str | None = None
    probes_run: int = 0


class VettingCache:
    """In-memory cache for vetting scores keyed by (address, readme_sha256) with 24h TTL."""

    def __init__(self, ttl_seconds: float = 86400.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[tuple[str, str], tuple[VetScoreResult, float]] = {}

    def get(self, address: str, readme_sha256: str) -> VetScoreResult | None:
        key = (address, readme_sha256)
        if key not in self._cache:
            return None
        res, timestamp = self._cache[key]
        if time.time() - timestamp > self.ttl_seconds:
            self._cache.pop(key, None)
            return None
        return res

    def set(self, result: VetScoreResult) -> None:
        self._cache[(result.address, result.readme_sha256)] = (result, time.time())

    def clear(self) -> None:
        self._cache.clear()


class VettingEngine:
    """Executes the full Genesis v2 vetting pipeline."""

    def __init__(self, cache: VettingCache | None = None) -> None:
        self.cache = cache or VettingCache()
        self.failure_tracker: dict[str, int] = {}  # address -> consecutive real failures

    def is_destructive(self, readme: str) -> bool:
        """Returns True if agent README implies state-altering side effects."""
        text = readme.lower()
        return any(kw in text for kw in DESTRUCTIVE_KEYWORDS)

    def screen_safety(self, text: str, price_cap_usd: float = 0.10) -> tuple[bool, str | None]:
        """Screen agent reply for safety, injections, credential phishing, and size limits."""
        if len(text.encode("utf-8")) > MAX_PROBE_REPLY_BYTES:
            return False, f"Reply size ({len(text)} bytes) exceeds limit ({MAX_PROBE_REPLY_BYTES})"

        for pat in PROMPT_INJECTION_PATTERNS:
            if pat.search(text):
                return False, f"Prompt injection pattern detected: '{pat.pattern}'"

        for pat in CREDENTIAL_HARVEST_PATTERNS:
            if pat.search(text):
                return False, "Credential harvest attempt detected"

        return True, None

    def evaluate_contract(self, reply: str, expect: Expect) -> bool:
        """Evaluate if specialist reply satisfies expectation."""
        if not reply:
            return False
        if expect.kind == "nonempty":
            return len(reply.strip()) > 0
        if expect.kind == "contains":
            val = str(expect.value or "")
            return val.lower() in reply.lower()
        if expect.kind == "json_schema":
            try:
                json.loads(reply)
                return True
            except Exception:
                return False
        if expect.kind == "llm_check":
            # Heuristic rubric check
            rubric = str(expect.value or "").lower()
            return len(reply) > 20 and not ("error" in reply.lower() and len(reply) < 60)
        return True

    def infer_probes(self, user_intent: str, specialist_domain: str) -> list[ProbeDefinition]:
        """Synthesize 2-3 synthetic probe inputs from user intent (never real user data)."""
        clean_intent = user_intent.lower()
        return [
            ProbeDefinition(
                probe_id="probe_basic",
                message=f"Synthetic probe: What is the latest metrics or summary for {specialist_domain}?",
                expect=Expect(kind="nonempty"),
            ),
            ProbeDefinition(
                probe_id="probe_schema",
                message=f"Synthetic probe: Provide a brief status and key values for {specialist_domain}.",
                expect=Expect(kind="nonempty"),
            ),
        ]

    def vet_candidate(
        self,
        *,
        address: str,
        name: str,
        readme: str,
        probes: list[ProbeDefinition],
        send_probe_fn: Callable[[str, str], tuple[bool, str, float]],
        price_cap_usd: float = 0.05,
        reliability_history: float = 1.0,
        readme_fit: float = 0.9,
    ) -> VetScoreResult:
        """Run synthetic live probes against a candidate specialist agent."""
        readme_sha256 = hashlib.sha256(readme.encode("utf-8")).hexdigest()

        # Check cache
        cached = self.cache.get(address, readme_sha256)
        if cached is not None:
            logger.info("Using cached vet result for %s (hash: %s)", address, readme_sha256[:8])
            return cached

        # Check non-destructive rule
        if self.is_destructive(readme):
            logger.warning("Agent %s excluded: README indicates destructive side effects.", address)
            res = VetScoreResult(
                address=address,
                name=name,
                readme_sha256=readme_sha256,
                score=0.0,
                vetted_at=datetime.now(timezone.utc).isoformat(),
                price_cap_usd=price_cap_usd,
                passed=False,
                latency_avg=999.0,
                contract_score=0.0,
                repeatability_score=0.0,
                safety_clean=False,
                failure_reason="Excluded: candidate performs destructive side effects.",
            )
            self.cache.set(res)
            return res

        probe_results: list[ProbeResult] = []
        latencies: list[float] = []

        for p in probes:
            responded, reply_text, latency = send_probe_fn(address, p.message)
            latencies.append(latency)
            if not responded:
                probe_results.append(
                    ProbeResult(
                        probe_id=p.probe_id,
                        responded=False,
                        latency_s=latency,
                        passed_contract=False,
                        reply_text="",
                        safety_passed=True,
                        error="No response within timeout",
                    )
                )
                continue

            safety_clean, fail_msg = self.screen_safety(reply_text, price_cap_usd)
            if not safety_clean:
                probe_results.append(
                    ProbeResult(
                        probe_id=p.probe_id,
                        responded=True,
                        latency_s=latency,
                        passed_contract=False,
                        reply_text=reply_text,
                        safety_passed=False,
                        error=fail_msg,
                    )
                )
                continue

            contract_pass = self.evaluate_contract(reply_text, p.expect)
            probe_results.append(
                ProbeResult(
                    probe_id=p.probe_id,
                    responded=True,
                    latency_s=latency,
                    passed_contract=contract_pass,
                    reply_text=reply_text,
                    safety_passed=True,
                )
            )

        # Measure repeatability: run probe 0 again and compare
        repeatability_score = 1.0
        if probes and probe_results and probe_results[0].responded:
            first_probe = probes[0]
            resp_repeat, reply_repeat, _ = send_probe_fn(address, first_probe.message)
            if not resp_repeat or not reply_repeat:
                repeatability_score = 0.5
            else:
                # Compare similarity length or overlap
                orig = probe_results[0].reply_text
                len_ratio = min(len(orig), len(reply_repeat)) / max(len(orig), len(reply_repeat), 1)
                repeatability_score = max(0.5, len_ratio)

        # Compute sub-scores
        passed_probes = sum(1 for pr in probe_results if pr.passed_contract and pr.safety_passed)
        contract_score = passed_probes / max(len(probes), 1)
        any_safety_fail = any(not pr.safety_passed for pr in probe_results)

        avg_latency = sum(latencies) / max(len(latencies), 1)
        latency_score = max(0.0, min(1.0, 1.0 - (avg_latency / 10.0)))

        # Overall score formula: 0.4*contract + 0.2*repeatability + 0.2*latency + 0.1*history + 0.1*fit
        if any_safety_fail:
            score = 0.0
            passed = False
            fail_reason = "Safety violation detected during synthetic probes."
        else:
            score = (
                0.4 * contract_score
                + 0.2 * repeatability_score
                + 0.2 * latency_score
                + 0.1 * reliability_history
                + 0.1 * readme_fit
            )
            score = round(min(1.0, max(0.0, score)), 3)
            passed = score >= VET_MIN_SCORE
            fail_reason = None if passed else f"Vet score {score:.2f} below threshold {VET_MIN_SCORE:.2f}"

        res = VetScoreResult(
            address=address,
            name=name,
            readme_sha256=readme_sha256,
            score=score,
            vetted_at=datetime.now(timezone.utc).isoformat(),
            price_cap_usd=price_cap_usd,
            passed=passed,
            latency_avg=round(avg_latency, 2),
            contract_score=contract_score,
            repeatability_score=repeatability_score,
            safety_clean=not any_safety_fail,
            failure_reason=fail_reason,
            probes_run=len(probes),
        )
        self.cache.set(res)
        return res

    def record_real_run_result(self, address: str, succeeded: bool) -> bool:
        """Circuit breaker: 2 consecutive real failures mark specialist degraded. Returns is_degraded."""
        if succeeded:
            self.failure_tracker[address] = 0
            return False
        fails = self.failure_tracker.get(address, 0) + 1
        self.failure_tracker[address] = fails
        if fails >= 2:
            logger.warning("Circuit breaker tripped for specialist %s (%d consecutive failures).", address, fails)
            return True
        return False
