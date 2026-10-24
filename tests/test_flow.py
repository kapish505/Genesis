from pathlib import Path
from typing import Any

import pytest

from genesis.builder import Builder
from genesis.config import Settings
from genesis.delegate import Relay, ReplyRouter
from genesis.deployer import Deployer
from genesis.executor import Executor, RunResult
from genesis.flow import GenesisFlow
from genesis.ledger import Ledger
from genesis.payments import PaymentConfig, SellerPayments
from genesis.session import SessionStore
from genesis.spec import AgentSpec
from tests.test_spec import sample


class MockSearch:
    def __init__(self, candidates: list[dict[str, Any]] | None = None):
        self.candidates = candidates or []
        self.configured = True

    async def search(self, query: str, limit: int = 10, **kwargs):
        return self.candidates[:limit]


class MockLLM:
    configured = True

    async def extract_intent(self, request: str):
        return None

    async def score_candidates(self, intent: Any, candidates: list[dict[str, Any]]):
        return None

    async def plan_json(self, request: str, context: str = "", error: str = ""):
        return sample().model_dump()

    async def classify_selection(
        self, text: str, state: str, options: list[str], fallback_context: str = ""
    ):
        return None


class MockAgentverse:
    async def create_agent(self, name: str):
        return {"address": f"addr-{name}", "name": name}

    async def upload_code(self, address: str, source: str):
        pass

    async def start_agent(self, address: str):
        pass

    async def stop_agent(self, address: str):
        pass

    async def delete_agent(self, address: str):
        pass

    async def search(self, query: str, limit: int = 10, **kwargs):
        return []


@pytest.fixture
async def flow_env(tmp_path: Path):
    db_file = str(tmp_path / "flow.db")
    ledger = Ledger(db_file)
    await ledger.initialize()

    settings = Settings()
    settings.discovery_timeout_s = 1
    settings.probe_timeout_s = 2
    settings.sender_runs_per_hour = 100
    settings.sender_builds_per_day = 10

    sessions = SessionStore(settings)
    llm = MockLLM()
    search = MockSearch()
    router = ReplyRouter()

    async def mock_send(target: str, msg: Any) -> None:
        pass

    relay = Relay(mock_send, router)
    builder = Builder()

    async def mock_probe(address: str, inputs: dict[str, Any]) -> str:
        return "Paneer dishes available"

    deployer = Deployer(MockAgentverse(), ledger, builder, settings, mock_probe)
    executor = Executor(settings, "gateway-secret")

    # Mock ephemeral execution
    async def mock_ephemeral(spec: AgentSpec, inputs: dict[str, Any], state=None) -> RunResult:
        return RunResult(
            text="Ephemeral execution result: Paneer found!",
            ok=True,
            mode="ephemeral",
            seconds=0.1,
            errors=[],
        )

    executor.ephemeral = mock_ephemeral  # type: ignore[method-assign]

    payments = SellerPayments(
        PaymentConfig(enabled=False, rail="stripe", recipient="", amount="5.00", currency="USD")
    )

    flow = GenesisFlow(
        settings, ledger, sessions, llm, search, relay, executor, deployer, builder, payments
    )

    yield flow, ledger, settings
    await ledger.close()


@pytest.mark.asyncio
async def test_flow_blocks_abusive_request(flow_env):
    flow, _ledger, _settings = flow_env
    reply = await flow.handle_message("user-1", "Query http://127.0.0.1:8080/admin")
    events = reply.events
    assert any(
        "private network" in event[1].get("text", "") for event in events if event[0] == "text"
    )


@pytest.mark.asyncio
async def test_flow_form_card_when_url_missing(flow_env):
    flow, _ledger, _settings = flow_env
    reply = await flow.handle_message(
        "user-2", "Check this mess menu website and tell me if paneer is served"
    )
    card_events = [e for e in reply.events if e[0] == "card"]
    assert len(card_events) > 0
    assert card_events[0][1]["kind"] == "form"


@pytest.mark.asyncio
async def test_flow_plan_review_approve_build(flow_env):
    flow, _ledger, _settings = flow_env
    # User provides full prompt with URL
    reply = await flow.handle_message(
        "user-3", "Check https://example.com/menu and tell me if paneer is served"
    )
    card_events = [e for e in reply.events if e[0] == "card"]
    assert len(card_events) > 0
    assert card_events[0][1]["kind"] == "review"

    # User approves via selection JSON
    session = flow.sessions.find("user-3", None)
    assert session is not None
    approve_msg = f'{{"sid":"{session.session_id}","action":"approve"}}'

    # Mock run on agent
    async def mock_run_on_agent(session, spec, address):
        return RunResult(
            text="Yes! Paneer Butter Masala on Wednesday.",
            ok=True,
            mode="deployed",
            seconds=0.5,
            errors=[],
        )

    flow._run_on_agent = mock_run_on_agent  # type: ignore[method-assign]

    reply2 = await flow.handle_message("user-3", approve_msg)
    texts = [e[1].get("text", "") for e in reply2.events if e[0] == "text"]
    full_text = " ".join(texts)
    assert "Approved" in full_text
    assert "The agent is live" in full_text
    assert "Paneer Butter Masala" in full_text


@pytest.mark.asyncio
async def test_flow_ephemeral_fallback_when_builds_disabled(flow_env):
    flow, _ledger, settings = flow_env
    settings.builds_enabled = False

    reply = await flow.handle_message(
        "user-4", "Check https://example.com/menu and tell me if paneer is served"
    )
    texts = [e[1].get("text", "") for e in reply.events if e[0] == "text"]
    full_text = " ".join(texts)
    assert "live builds are switched off" in full_text
    assert "Ephemeral execution result" in full_text
