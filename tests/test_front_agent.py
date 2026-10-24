"""Tests for front agent messaging and shared models."""

import pytest
from shared.models import ApprovalReply, RunEvent, RunRequest


def test_shared_models_serialization():
    req = RunRequest(
        run_id="run_123",
        spec_id="spec_test",
        sender="agent1q_user",
        user_text="What is the price of FET?",
        inputs={"token": "FET"},
    )
    assert req.run_id == "run_123"
    dump = req.model_dump()
    assert dump["spec_id"] == "spec_test"
    assert dump["inputs"]["token"] == "FET"

    event = RunEvent(
        run_id="run_123",
        kind="progress",
        text="Probing specialist...",
    )
    assert event.kind == "progress"

    appr = ApprovalReply(
        run_id="run_123",
        approval_id="appr_456",
        decision="approve",
    )
    assert appr.decision == "approve"
