"""Gap detection: turn a request into an intent, search, and score candidates.

The detector is deliberately conservative. It returns ``use_existing`` only
when the best agent clears ``match_threshold``; the 0.4–0.72 band becomes a
user choice, and anything below triggers a build. If the intent needs an input
the user did not provide (notably a URL), the verdict is ``ask_user`` before any
search result is trusted.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, cast

from genesis.config import Settings
from genesis.llm import AsiOneClient

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
STOPWORDS = frozenset(
    {
        "the",
        "this",
        "that",
        "with",
        "from",
        "have",
        "tell",
        "please",
        "check",
        "find",
        "give",
        "what",
        "when",
        "which",
        "there",
        "their",
        "about",
        "into",
        "using",
        "need",
        "want",
        "make",
        "then",
        "than",
        "them",
        "they",
        "your",
        "will",
        "would",
        "should",
        "could",
    }
)


@dataclass(frozen=True)
class Intent:
    goal: str
    output: str = ""
    trigger: str = "on_demand"
    inputs_needed: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    address: str
    name: str
    readme: str
    score: float
    reason: str
    handle: str = ""
    status: str = "active"
    total_interactions: int = 0


@dataclass
class Detection:
    verdict: str  # use_existing | ask_user | build
    intent: Intent
    candidates: list[Candidate] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def terms(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9][a-z0-9_+-]{2,}", text.casefold())
        if word not in STOPWORDS
    }


def heuristic_intent(request: str) -> Intent:
    words = sorted(terms(request))
    goal = " ".join(request.split())[:160]
    queries = [" ".join(words[:6]) or request[:80]]
    if len(words) > 6:
        queries.append(" ".join(words[3:9]))
    interval = bool(
        re.search(r"\b(daily|every day|hourly|weekly|each morning|every morning)\b", request, re.I)
    )
    missing: list[str] = []
    if re.search(
        r"\b(url|link|page|website|pdf|document|site)\b", request, re.I
    ) and not URL_PATTERN.search(request):
        missing.append("url")
    return Intent(
        goal=goal,
        output=goal,
        trigger="interval" if interval else "on_demand",
        inputs_needed=tuple(missing),
        queries=tuple(queries),
    )


def overlap_score(request: str, item: dict[str, Any]) -> float:
    request_terms = terms(request)
    if not request_terms:
        return 0.0
    text = (
        f"{item.get('name', '')} {item.get('description', '')} {str(item.get('readme', ''))[:2000]}"
    )
    overlap = len(request_terms & terms(text))
    base = overlap / max(1, min(8, len(request_terms)))
    interactions = int(item.get("total_interactions") or 0)
    activity = min(0.1, interactions / 1000)
    return round(min(1.0, base * 0.9 + activity), 3)


class Detector:
    def __init__(self, search: Any, llm: AsiOneClient, settings: Settings) -> None:
        self.search = search
        self.llm = llm
        self.settings = settings

    async def intent(self, request: str) -> Intent:
        if not self.llm.configured:
            return heuristic_intent(request)
        try:
            data = await self.llm.extract_intent(request)
        except Exception as exc:  # noqa: BLE001 - heuristic fallback is intentional
            logger.warning("intent extraction failed, using heuristic: %s", exc)
            return heuristic_intent(request)
        if not isinstance(data, dict):
            return heuristic_intent(request)
        queries = [str(item) for item in data.get("queries") or [] if str(item).strip()][:3]
        inputs = [str(item) for item in data.get("inputs_needed") or [] if str(item).strip()][:4]
        trigger = str(data.get("trigger") or "on_demand")
        fallback = heuristic_intent(request)
        return Intent(
            goal=str(data.get("goal") or fallback.goal)[:200],
            output=str(data.get("output") or fallback.output)[:300],
            trigger=trigger if trigger in {"on_demand", "interval"} else "on_demand",
            inputs_needed=tuple(inputs) if inputs else fallback.inputs_needed,
            queries=tuple(queries) or fallback.queries,
        )

    async def _search_all(self, queries: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        if not getattr(self.search, "configured", True):
            return {}
        results: dict[str, dict[str, Any]] = {}

        async def _one(query: str) -> list[dict[str, Any]]:
            try:
                return cast(
                    list[dict[str, Any]],
                    await self.search.search(query, limit=10, state=["active"], has_readme=True),
                )
            except Exception as exc:  # noqa: BLE001 - a failed phrasing should not kill detection
                logger.warning("Agentverse search failed for %r: %s", query, exc)
                return []

        for batch in await asyncio.gather(*(_one(query) for query in queries or ("",))):
            for item in batch:
                address = str(item.get("address") or "")
                if not address:
                    continue
                if str(item.get("status") or "active") != "active":
                    continue
                if not item.get("readme"):
                    continue
                existing = results.get(address)
                if existing is None or int(item.get("total_interactions") or 0) > int(
                    existing.get("total_interactions") or 0
                ):
                    results[address] = item
        return results

    async def detect(self, request: str) -> Detection:
        intent = await self.intent(request)
        results = await self._search_all(intent.queries)
        items = sorted(
            results.values(),
            key=lambda item: (
                -int(item.get("total_interactions") or 0),
                -int(item.get("recent_interactions") or 0),
            ),
        )[:12]

        scores: dict[str, tuple[float, str]] = {}
        if items and self.llm.configured:
            try:
                for item in await self.llm.score_candidates(request, items):
                    address = item.get("address", "")
                    if address:
                        scores[address] = (float(item["score"]), str(item.get("reason", "")))
            except Exception as exc:  # noqa: BLE001 - fall back to lexical scoring
                logger.warning("candidate scoring failed, using overlap: %s", exc)
        if not scores:
            scores = {
                str(item["address"]): (
                    overlap_score(request, item),
                    f"{overlap_score(request, item):.2f} lexical match",
                )
                for item in items
            }

        candidates = []
        for item in items:
            address = str(item["address"])
            score, reason = scores.get(address, (0.0, "not scored"))
            candidates.append(
                Candidate(
                    address=address,
                    name=str(item.get("name") or "Unnamed agent"),
                    readme=str(item.get("readme") or ""),
                    score=score,
                    reason=reason,
                    handle=str(item.get("handle") or ""),
                    status=str(item.get("status") or "active"),
                    total_interactions=int(item.get("total_interactions") or 0),
                )
            )
        candidates.sort(key=lambda candidate: (-candidate.score, -candidate.total_interactions))
        candidates = [candidate for candidate in candidates if candidate.score >= 0.05][:6]

        missing = list(intent.inputs_needed)
        if missing:
            verdict = "ask_user"
        elif candidates and candidates[0].score >= self.settings.match_threshold:
            verdict = "use_existing"
        elif candidates and candidates[0].score >= self.settings.borderline_threshold:
            verdict = "ask_user"
        else:
            verdict = "build"
        return Detection(verdict=verdict, intent=intent, candidates=candidates, missing=missing)
