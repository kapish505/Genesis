from pathlib import Path
from typing import Any

import pytest

from genesis.builder import Builder
from genesis.config import Settings
from genesis.deployer import Deployer, DeployResult
from genesis.errors import DeploymentError
from genesis.ledger import Ledger
from genesis.search import AgentverseError
from tests.test_spec import sample


class FakeAgentverseClient:
    def __init__(self, fail_at: str | None = None):
        self.fail_at = fail_at
        self.created_agents: list[str] = []
        self.deleted_agents: list[str] = []
        self.started_agents: list[str] = []
        self.uploaded_code: dict[str, str] = {}

    async def create_agent(self, name: str) -> dict[str, Any]:
        if self.fail_at == "create":
            raise AgentverseError("create failed", retryable=False)
        addr = f"agent-addr-{name}"
        self.created_agents.append(addr)
        return {"address": addr, "name": name}

    async def upload_code(self, address: str, source: str) -> None:
        if self.fail_at == "upload":
            raise AgentverseError("upload failed", retryable=False)
        self.uploaded_code[address] = source

    async def start_agent(self, address: str) -> None:
        if self.fail_at == "start":
            raise AgentverseError("start failed", retryable=False)
        self.started_agents.append(address)

    async def stop_agent(self, address: str) -> None:
        pass

    async def delete_agent(self, address: str) -> None:
        self.deleted_agents.append(address)

    async def search(self, query: str, limit: int = 10, **kwargs):
        return [{"address": f"agent-addr-{query.split()[0]}", "name": query.split()[0]}]


@pytest.fixture
async def deployer_env(tmp_path: Path):
    db_file = str(tmp_path / "deployer.db")
    ledger = Ledger(db_file)
    await ledger.initialize()

    settings = Settings()
    settings.discovery_timeout_s = 2
    settings.probe_timeout_s = 5

    yield ledger, settings
    await ledger.close()


@pytest.mark.asyncio
async def test_deployer_success_saga(deployer_env):
    ledger, settings = deployer_env
    client = FakeAgentverseClient()
    spec = sample()

    async def mock_probe(address: str, inputs: dict[str, Any]) -> str:
        return "Paneer dishes available this week"

    deployer = Deployer(client, ledger, Builder(), settings, mock_probe)
    progress_log = []

    result = await deployer.deploy(
        spec,
        session_id="s1",
        gateway_url="http://gateway",
        gateway_token="tok",
        on_progress=lambda m: progress_log.append(m),
    )

    assert isinstance(result, DeployResult)
    assert result.address == f"agent-addr-{spec.name}"
    assert result.handle == spec.name

    # Check ledger recorded agent
    agent = await ledger.get_agent(spec.name)
    # or by spec_hash
    from genesis.spec import spec_hash

    agent = await ledger.get_agent(spec_hash(spec))
    assert agent is not None
    assert agent["status"] == "live"


@pytest.mark.asyncio
async def test_deployer_compensates_on_failure(deployer_env):
    ledger, settings = deployer_env
    client = FakeAgentverseClient(fail_at="upload")
    spec = sample()

    async def mock_probe(address: str, inputs: dict[str, Any]) -> str:
        return "ok"

    deployer = Deployer(client, ledger, Builder(), settings, mock_probe)

    with pytest.raises(DeploymentError):
        await deployer.deploy(
            spec,
            session_id="s2",
            gateway_url="http://gateway",
            gateway_token="tok",
        )

    # Agent was created, then compensation deleted it
    created_addr = f"agent-addr-{spec.name}"
    assert created_addr in client.created_agents
    assert created_addr in client.deleted_agents
