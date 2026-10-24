from typing import Any

import pytest

from genesis.errors import PlanningError
from genesis.planner import Planner, dry_run_note
from tests.test_spec import sample


class MockLLM:
    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.call_count = 0
        self.last_error = ""

    async def plan_json(self, request: str, context: str = "", error: str = "") -> dict[str, Any]:
        self.call_count += 1
        self.last_error = error
        if self.responses:
            return self.responses.pop(0)
        return sample().model_dump()


@pytest.mark.asyncio
async def test_planner_success_first_attempt():
    valid_spec = sample().model_dump()
    llm = MockLLM([valid_spec])
    planner = Planner(llm, max_repairs=3)
    spec = await planner.plan("Check menu")
    assert spec.name == "menu-checker"
    assert llm.call_count == 1
    assert "menu-checker" in dry_run_note(spec)


@pytest.mark.asyncio
async def test_planner_repair_loop_on_invalid_spec():
    invalid_spec = sample().model_dump()
    invalid_spec["name"] = "INVALID NAME WITH UPPERCASE"  # violates pattern
    valid_spec = sample().model_dump()

    llm = MockLLM([invalid_spec, valid_spec])
    planner = Planner(llm, max_repairs=3)
    spec = await planner.plan("Check menu")
    assert spec.name == "menu-checker"
    assert llm.call_count == 2
    assert "name" in llm.last_error


@pytest.mark.asyncio
async def test_planner_refusal_and_exhaustion():
    refusal = {"name": "unsupported-request", "description": "Needs login which is unsupported"}
    llm = MockLLM([refusal, refusal, refusal, refusal])
    planner = Planner(llm, max_repairs=2)
    with pytest.raises(PlanningError, match="could not produce a valid spec"):
        await planner.plan("Do impossible task")
    assert llm.call_count == 3  # 1 initial + 2 repairs
