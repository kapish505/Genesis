"""Tests for genesis/orchestrator.py: Managed mode execution, fallbacks, budget, and approval cards."""

import asyncio
from datetime import datetime, timezone
import pytest

from gateway.actions import ActionManager
from genesis.orchestrator import BudgetExceededError, Orchestrator
from genesis.spec import parse_spec
from shared.models import ApprovalReply, RunEvent


def create_mock_spec_v2():
    now = datetime.now(timezone.utc).isoformat()
    raw = {
        "spec_version": "2",
        "name": "orchestrator-test",
        "title": "Test Orchestrator",
        "description": "Orchestrates testing specialists and sends notifications.",
        "use_cases": ["Testing", "Validating"],
        "limitations": ["Mock only"],
        "domain_tag": "test-analytics",
        "inputs": [{"name": "query", "kind": "text", "label": "Query"}],
        "specialists": [
            {
                "alias": "primary_spec",
                "address": "agent1q01234567890123456789primary",
                "name": "Primary Specialist",
                "readme_sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
                "vet_score": 0.95,
                "vetted_at": now,
                "price_cap_usd": 0.04,
                "send_fields": ["query"],
                "fallbacks": ["backup_spec"],
            },
            {
                "alias": "backup_spec",
                "address": "agent1q01234567890123456789backup",
                "name": "Backup Specialist",
                "readme_sha256": "123456abcdef7890abcdef1234567890abcdef1234567890abcdef1234567890",
                "vet_score": 0.88,
                "vetted_at": now,
                "price_cap_usd": 0.03,
                "send_fields": ["query"],
                "fallbacks": [],
            },
        ],
        "budget": {"max_usd_per_run": 0.10, "max_agent_calls": 2},
        "steps": [
            {
                "id": "step_call",
                "tool": "agent_call",
                "args": {
                    "specialist": "primary_spec",
                    "message": "Answer {{input.query}}",
                    "expect": {"kind": "nonempty"},
                    "timeout_s": 10,
                    "on_fail": "fallback",
                },
            },
            {
                "id": "step_alert",
                "tool": "action",
                "args": {
                    "action": "telegram_send",
                    "body_template": "Result: {{steps.step_call.out}}",
                },
            },
        ],
        "output_template": "Done: {{steps.step_call.out}}",
        "permissions": ["notify"],
        "selftest": {"inputs": {"query": "test"}, "expect_nonempty": True},
    }
    return parse_spec(raw)


@pytest.mark.asyncio
async def test_orchestrator_agent_call_and_approval():
    action_mgr = ActionManager(db_path=":memory:")
    # Pre-verify Telegram destination for user
    code = action_mgr.create_verification_code("telegram", "user_test", "123456")
    action_mgr.complete_verification(code)

    def mock_acp(address: str, message: str, timeout: float) -> tuple[bool, str, float]:
        return True, "Analysis: BTC price is $70,000", 0.5

    orchestrator = Orchestrator(action_manager=action_mgr, acp_client_fn=mock_acp)

    events: list[RunEvent] = []

    async def run_flow():
        return await orchestrator.execute_run(
            spec=create_mock_spec_v2(),
            inputs={"query": "BTC"},
            sender="user_test",
            event_callback=events.append,
        )

    task = asyncio.create_task(run_flow())

    # Wait until approval event is emitted
    for _ in range(50):
        await asyncio.sleep(0.05)
        approval_events = [e for e in events if e.kind == "approval"]
        if approval_events:
            appr = approval_events[0]
            # Submit user approval
            orchestrator.submit_approval_decision(
                ApprovalReply(
                    run_id=appr.run_id,
                    approval_id=appr.approval_id,
                    decision="approve",
                )
            )
            break

    result = await task
    assert result["status"] == "completed"
    assert "Done: Analysis: BTC price is $70,000" in result["output"]
    assert result["agent_calls"] == 1
    assert result["spend_usd"] == 0.04


@pytest.mark.asyncio
async def test_orchestrator_fallback_chain_on_primary_down():
    action_mgr = ActionManager(db_path=":memory:")

    def mock_acp_primary_fails(address: str, message: str, timeout: float) -> tuple[bool, str, float]:
        if "primary" in address:
            return False, "", 10.0  # primary down
        return True, "Backup specialist answered: ETH is $3500", 0.4

    orchestrator = Orchestrator(action_manager=action_mgr, acp_client_fn=mock_acp_primary_fails)

    # Spec without action step to test agent call fallback directly
    spec = create_mock_spec_v2()
    spec.steps = [spec.steps[0]]  # only agent_call step
    spec.output_template = "Output: {{steps.step_call.out}}"

    result = await orchestrator.execute_run(
        spec=spec,
        inputs={"query": "ETH"},
        sender="user_test",
    )

    assert result["status"] == "completed"
    assert "Backup specialist answered: ETH is $3500" in result["output"]
    assert result["agent_calls"] == 2  # primary attempted, then backup


@pytest.mark.asyncio
async def test_orchestrator_ephemeral_llm_fallback():
    action_mgr = ActionManager(db_path=":memory:")

    def mock_acp_all_fail(address: str, message: str, timeout: float) -> tuple[bool, str, float]:
        return False, "", 10.0

    orchestrator = Orchestrator(
        action_manager=action_mgr,
        acp_client_fn=mock_acp_all_fail,
        ephemeral_llm_fn=lambda p: "Synthesized direct answer",
    )

    spec = create_mock_spec_v2()
    spec.steps[0].args["on_fail"] = "ephemeral_llm"
    spec.steps = [spec.steps[0]]
    spec.output_template = "Output: {{steps.step_call.out}}"

    result = await orchestrator.execute_run(
        spec=spec,
        inputs={"query": "SOL"},
        sender="user_test",
    )

    assert result["status"] == "completed"
    assert "Synthesized direct answer" in result["output"]
