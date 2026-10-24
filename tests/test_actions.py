"""Tests for gateway/actions.py (Telegram & Email safe execution)."""

import pytest

from gateway.actions import (
    ActionDisabledError,
    ActionError,
    ActionManager,
    RateLimitExceededError,
    VerificationRequiredError,
)


def test_verification_flow():
    mgr = ActionManager(db_path=":memory:")
    user_id = "user_123"
    email = "alice@example.com"

    # Destination not verified initially
    assert mgr.get_verified_destination("email", user_id) is None

    # Generate code
    code = mgr.create_verification_code("email", user_id, email)
    assert len(code) == 6

    # Invalid code fails
    with pytest.raises(VerificationRequiredError):
        mgr.complete_verification("WRONG1")

    # Complete verification
    channel, uid, dest = mgr.complete_verification(code)
    assert channel == "email"
    assert uid == user_id
    assert dest == email

    # Now verified
    assert mgr.get_verified_destination("email", user_id) == email


def test_telegram_action_execution():
    mgr = ActionManager(db_path=":memory:")
    user_id = "user_telegram"
    chat_id = "987654321"

    # Pre-verify
    code = mgr.create_verification_code("telegram", user_id, chat_id)
    mgr.complete_verification(code)

    res = mgr.execute_action(
        action="telegram_send",
        user_id=user_id,
        run_id="run_001",
        step_id="step_notify",
        body="Price alert: BTC reached $70,000",
    )
    assert res["status"] == "sent"
    assert res["recipient"] == chat_id

    # Test idempotency: second execution returns already_executed
    res2 = mgr.execute_action(
        action="telegram_send",
        user_id=user_id,
        run_id="run_001",
        step_id="step_notify",
        body="Price alert: BTC reached $70,000",
    )
    assert res2["status"] == "already_executed"


def test_unverified_action_rejected():
    mgr = ActionManager(db_path=":memory:")
    with pytest.raises(VerificationRequiredError):
        mgr.execute_action(
            action="email_send_self",
            user_id="unverified_user",
            run_id="run_002",
            step_id="step_email",
            body="Hello",
        )


def test_body_length_cap():
    mgr = ActionManager(db_path=":memory:")
    code = mgr.create_verification_code("email", "u1", "u1@test.com")
    mgr.complete_verification(code)

    with pytest.raises(ActionError, match="exceeds maximum length"):
        mgr.execute_action(
            action="email_send_self",
            user_id="u1",
            run_id="run_003",
            step_id="step_email",
            body="x" * 801,
        )


def test_daily_rate_limit():
    mgr = ActionManager(db_path=":memory:")
    code = mgr.create_verification_code("telegram", "u_limit", "chat_limit")
    mgr.complete_verification(code)

    for i in range(10):
        res = mgr.execute_action(
            action="telegram_send",
            user_id="u_limit",
            run_id=f"run_loop_{i}",
            step_id="s1",
            body=f"Msg {i}",
        )
        assert res["status"] == "sent"

    with pytest.raises(RateLimitExceededError):
        mgr.execute_action(
            action="telegram_send",
            user_id="u_limit",
            run_id="run_loop_11",
            step_id="s1",
            body="Msg 11",
        )
