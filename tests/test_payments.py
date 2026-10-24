from pathlib import Path

import pytest
from uagents_core.contrib.protocols.payment import Funds

from genesis.ledger import Ledger
from genesis.payments import PaymentConfig, SellerPayments


class FakePaymentVerifier:
    def __init__(self, succeeds: bool = True):
        self.succeeds = succeeds

    async def verify(self, reference: str, transaction_id: str, funds: Funds) -> tuple[bool, str]:
        return self.succeeds, "" if self.succeeds else "verification failed"


@pytest.fixture
async def payment_env(tmp_path: Path):
    db_file = str(tmp_path / "payments.db")
    ledger = Ledger(db_file)
    await ledger.initialize()

    config = PaymentConfig(
        enabled=True,
        rail="stripe",
        recipient="seller_account",
        amount="5.00",
        currency="USD",
        deadline_s=300,
    )

    verifier = FakePaymentVerifier(succeeds=True)
    payments = SellerPayments(config, ledger=ledger, verifier=verifier)

    yield payments, ledger, config
    await ledger.close()


@pytest.mark.asyncio
async def test_seller_payments_full_lifecycle(payment_env):
    payments, ledger, config = payment_env
    ref = "sid-payment-1"

    assert payments.enabled is True

    # 1. Request Payment
    req_msg = payments.request(ref)
    assert req_msg.reference == ref
    assert req_msg.accepted_funds[0].amount == config.amount
    assert req_msg.accepted_funds[0].currency == config.currency

    # 2. Commit Payment
    funds = Funds(amount=config.amount, currency=config.currency, payment_method=config.rail)
    verified, reason = await payments.on_commit(ref, "tx_123456", funds)
    assert verified is True
    assert reason == ""

    # Check status is verified
    rec = await ledger.get_payment(ref)
    assert rec is not None
    assert rec["status"] == "verified"
    assert rec["transaction_id"] == "tx_123456"

    # 3. Complete Payment
    assert payments.can_complete(ref) is True
    await payments.mark_completed(ref)

    rec = await ledger.get_payment(ref)
    assert rec is not None
    assert rec["status"] == "completed"


@pytest.mark.asyncio
async def test_seller_payments_reject_and_cancel(payment_env):
    payments, ledger, config = payment_env
    ref = "sid-payment-2"

    payments.request(ref)
    await payments.on_reject(ref, reason="User declined")

    rec = await ledger.get_payment(ref)
    assert rec is not None
    assert rec["status"] == "rejected"

    # Cancel payment (after verification)
    ref3 = "sid-payment-3"
    payments.request(ref3)
    funds = Funds(amount=config.amount, currency=config.currency, payment_method=config.rail)
    await payments.on_commit(ref3, "tx_cancel", funds)
    await payments.cancel(ref3, reason="Build error")
    rec3 = await ledger.get_payment(ref3)
    assert rec3 is not None
    assert rec3["status"] == "cancelled"
