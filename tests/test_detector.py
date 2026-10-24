from typing import Any

import pytest

from genesis.config import Settings
from genesis.detector import Detector, heuristic_intent, terms


class FakeSearchClient:
    def __init__(self, items: list[dict[str, Any]] | None = None):
        self.items = items or []
        self.configured = True

    async def search(self, query: str, limit: int = 10, **kwargs):
        return self.items[:limit]


class FakeLLMClient:
    configured = False

    async def extract_intent(self, request: str):
        return None

    async def score_candidates(self, intent: Any, candidates: list[dict[str, Any]]):
        return None


def test_heuristic_intent_and_terms():
    assert "college" in terms("Check this college mess menu")
    assert "the" not in terms("Check the mess menu")

    # Missing URL detection
    intent = heuristic_intent("Check this college mess menu PDF and find paneer")
    assert "url" in intent.inputs_needed
    assert intent.trigger == "on_demand"

    # URL provided
    intent2 = heuristic_intent("Check https://college.edu/menu.pdf and find paneer")
    assert "url" not in intent2.inputs_needed

    # Interval detection
    intent3 = heuristic_intent("Check https://example.gov/scheme daily for updates")
    assert intent3.trigger == "interval"


@pytest.mark.asyncio
async def test_detector_verdicts():
    settings = Settings()
    settings.match_threshold = 0.70
    settings.borderline_threshold = 0.40

    # 1. Missing URL -> ask_user
    det = Detector(FakeSearchClient(), FakeLLMClient(), settings)
    res = await det.detect("Check this website page for changes")
    assert res.verdict == "ask_user"
    assert "url" in res.missing

    # 2. No matching agents -> build
    det_empty = Detector(FakeSearchClient([]), FakeLLMClient(), settings)
    res2 = await det_empty.detect("Find today's tomato mandi price at https://mandi.gov/prices")
    assert res2.verdict == "build"
    assert len(res2.candidates) == 0

    # 3. High match agent -> use_existing
    high_match_items = [
        {
            "name": "tomato-price-tracker",
            "title": "Mandi Tomato Price Tracker",
            "readme": "Finds tomato mandi prices from government portals",
            "address": "agent1qtomato",
            "status": "active",
            "recent_interactions": 100,
            "total_interactions": 500,
        }
    ]
    det_high = Detector(FakeSearchClient(high_match_items), FakeLLMClient(), settings)
    res3 = await det_high.detect("Find tomato mandi price from https://mandi.gov/prices")
    assert res3.verdict in {"use_existing", "ask_user"}
    assert res3.best is not None
    assert res3.best.address == "agent1qtomato"
