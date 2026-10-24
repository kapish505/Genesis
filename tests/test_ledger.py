from pathlib import Path

import pytest

from genesis.ledger import Ledger


@pytest.mark.asyncio
async def test_ledger_lifecycle_and_spec_storage(tmp_path: Path):
    db_file = str(tmp_path / "test.db")
    ledger = Ledger(db_file)
    await ledger.initialize()

    # Spec storage
    spec_data = {"name": "test-agent", "steps": []}
    await ledger.save_spec("hash123", spec_data)
    loaded = await ledger.load_spec("hash123")
    assert loaded == spec_data
    assert await ledger.load_spec("nonexistent") is None

    # Message deduplication
    assert await ledger.mark_message("msg-1", 10) is True
    assert await ledger.mark_message("msg-1", 10) is False
    assert await ledger.mark_message("msg-2", 10) is True

    await ledger.close()


@pytest.mark.asyncio
async def test_ledger_agent_and_build_transitions(tmp_path: Path):
    db_file = str(tmp_path / "test.db")
    ledger = Ledger(db_file)
    await ledger.initialize()

    # Upsert and get agent
    await ledger.upsert_agent("hash1", "agent1_addr", "Test Agent 1", "live", handle="test-1")
    agent = await ledger.get_agent("hash1")
    assert agent is not None
    assert agent["agent_address"] == "agent1_addr"
    assert agent["title"] == "Test Agent 1"
    assert agent["status"] == "live"

    # Set status and runtime state
    await ledger.set_agent_status("hash1", "paused")
    agent = await ledger.get_agent("hash1")
    assert agent is not None
    assert agent["status"] == "paused"

    await ledger.set_runtime_state("hash1", {"next_run_at": 1000.0}, failures=0)
    agent = await ledger.get_agent("hash1")
    assert agent is not None
    import json

    assert json.loads(agent["runtime_state_json"])["next_run_at"] == 1000.0

    # Build transitions
    build_id = "build-1"
    await ledger.create_build(build_id, "hash1", session_id="s1")
    build = await ledger.get_build(build_id)
    assert build is not None
    assert build["state"] == "PLANNED"

    await ledger.transition_build(build_id, "CREATED", address="agent1_addr")
    build = await ledger.get_build(build_id)
    assert build is not None
    assert build["state"] == "CREATED"
    assert build["agent_address"] == "agent1_addr"

    await ledger.transition_build(build_id, "DONE")
    build = await ledger.get_build(build_id)
    assert build is not None
    assert build["state"] == "DONE"

    # Delete agent
    await ledger.delete_agent("hash1")
    assert await ledger.get_agent("hash1") is None

    await ledger.close()


@pytest.mark.asyncio
async def test_ledger_sessions_usage_and_capabilities(tmp_path: Path):
    db_file = str(tmp_path / "test.db")
    ledger = Ledger(db_file)
    await ledger.initialize()

    # Session persistence
    from genesis.session import Session

    session = Session(
        sender="user-addr-1",
        external_id="ext-1",
        session_id="sid-1",
        state="detecting",
        data={"request": "test"},
    )
    await ledger.save_session(session.to_row())
    loaded = await ledger.load_session("user-addr-1", "ext-1")
    assert loaded is not None
    assert loaded["session_id"] == "sid-1"
    assert loaded["state"] == "detecting"

    # Usage tracking
    allowed, remaining = await ledger.consume_usage("user-addr-1", "runs", limit=2, window_s=60)
    assert allowed is True
    assert remaining == 1

    allowed, remaining = await ledger.consume_usage("user-addr-1", "runs", limit=2, window_s=60)
    assert allowed is True
    assert remaining == 0

    allowed, remaining = await ledger.consume_usage("user-addr-1", "runs", limit=2, window_s=60)
    assert allowed is False
    assert remaining == 0

    # Capability request logging
    await ledger.capability_request("user-addr-1", "login to my bank", "credential requirement")

    await ledger.close()
