"""Seller-side payment protocol.

State machine, keyed by ``reference`` (the Genesis session id):

    requested -> committed -> verified -> completed
        |            |           |
        +-> rejected +-> expired +-> cancelled (refund path)

Hard rule: ``CompletePayment`` is sent only after a rail-specific verifier
confirms the transaction against the rail's own records. The verifier is an
interface so a live deployment can plug in Stripe or an on-chain lookup; when a
verifier is not configured, payments are treated as disabled and the flow never
reaches this code.

The module is transport-free on purpose: ``genesis.flow`` sends the protocol
messages, and this class decides whether sending them is legal.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from uagents_core.contrib.protocols.payment import Funds, RequestPayment

from genesis.ledger import Ledger

logger = logging.getLogger(__name__)


class PaymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaymentConfig:
    enabled: bool = False
    rail: str = "fet_direct"
    recipient: str = ""
    amount: str = "0.05"
    currency: str = "FET"
    deadline_s: int = 600
    min_amount: float = 0.0


class PaymentVerifier(Protocol):
    async def verify(self, reference: str, transaction_id: str, funds: Funds) -> tuple[bool, str]:
        """Return (verified, reason). Must check the rail, not the message."""
        ...


class LedgerVerifier:
    """Verifier for rails whose settlement is recorded out of band.

    A row in the ``payments`` table with status ``settled`` and a matching
    reference, transaction id and amount is the only thing it accepts. This is
    what an operator uses with a testnet faucet transaction: the operator (or a
    reconciliation job) writes the settled row after checking the chain, and
    Genesis then completes.

    It deliberately does **not** trust ``CommitPayment`` alone.
    """

    def __init__(self, ledger: Ledger, min_amount: float = 0.0) -> None:
        self.ledger = ledger
        self.min_amount = min_amount

    async def verify(self, reference: str, transaction_id: str, funds: Funds) -> tuple[bool, str]:
        row = await self.ledger.get_payment(reference)
        if row is None:
            return False, "no payment record for this reference"
        if row.get("status") != "settled":
            return False, f"payment is {row.get('status')!r}, not settled"
        if row.get("transaction_id") and row["transaction_id"] != transaction_id:
            return False, "transaction id does not match the settled payment"
        try:
            amount = float(funds.amount)
        except (TypeError, ValueError):
            return False, "the committed amount is not a number"
        if amount < self.min_amount:
            return False, f"amount {amount} is below the required {self.min_amount}"
        if len(row.get("transaction_id") or "") < 16:
            return False, "settled transaction id is too short to be real"
        return True, ""


class StripeVerifier:
    """Verifies a Stripe Checkout session in ``complete`` state and paid."""

    def __init__(self, secret_key: str, base_url: str = "https://api.stripe.com") -> None:
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")

    async def verify(self, reference: str, transaction_id: str, funds: Funds) -> tuple[bool, str]:
        import httpx

        if not self.secret_key:
            return False, "stripe is not configured"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    f"{self.base_url}/v1/checkout/sessions/{transaction_id}",
                    headers={"Authorization": f"Bearer {self.secret_key}"},
                )
        except httpx.HTTPError as exc:
            return False, f"stripe lookup failed: {type(exc).__name__}"
        if response.status_code != 200:
            return False, f"stripe returned HTTP {response.status_code}"
        data = response.json()
        if data.get("payment_status") != "paid":
            return False, f"stripe session is {data.get('payment_status')!r}"
        metadata = data.get("metadata") or {}
        if metadata.get("reference") not in (None, "", reference):
            return False, "stripe session reference does not match"
        try:
            paid = float(data.get("amount_total", 0)) / 100
        except (TypeError, ValueError):
            return False, "stripe amount is not a number"
        try:
            expected = float(funds.amount)
        except (TypeError, ValueError):
            return False, "committed amount is not a number"
        if paid + 1e-9 < expected:
            return False, f"paid amount {paid} is below the committed {expected}"
        return True, ""


@dataclass
class PaymentSession:
    reference: str
    status: str = "requested"
    rail: str = "fet_direct"
    transaction_id: str | None = None
    created_at: float = field(default_factory=time.time)
    attempts: int = 0


class SellerPayments:
    def __init__(
        self,
        config: PaymentConfig,
        ledger: Ledger | None = None,
        verifier: PaymentVerifier | None = None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.verifier = verifier
        self._sessions: dict[str, PaymentSession] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.verifier is not None and self.config.recipient)

    # ------------------------------------------------------------------ seller

    def request(self, reference: str) -> RequestPayment:
        """Build the RequestPayment to send to the buyer."""
        session = PaymentSession(reference=reference, rail=self.config.rail)
        self._sessions[reference] = session
        return RequestPayment(
            accepted_funds=[
                Funds(
                    amount=self.config.amount,
                    currency=self.config.currency,
                    payment_method=self.config.rail,
                )
            ],
            recipient=self.config.recipient,
            deadline_seconds=self.config.deadline_s,
            reference=reference,
            description="Genesis agent build",
        )

    async def on_commit(
        self, reference: str, transaction_id: str, funds: Funds
    ) -> tuple[bool, str]:
        session = self._sessions.get(reference)
        if session is None:
            return False, "unknown payment reference"
        if session.status not in {"requested", "committed"}:
            return False, f"payment is already {session.status}"
        session.attempts += 1
        session.transaction_id = transaction_id
        session.status = "committed"
        if self.verifier is None:
            return False, "no payment verifier is configured"
        verified, reason = await self.verifier.verify(reference, transaction_id, funds)
        if not verified:
            logger.info("payment verification failed for %s: %s", reference, reason)
            if session.attempts >= 3:
                session.status = "rejected"
            return False, reason
        session.status = "verified"
        if self.ledger is not None:
            await self.ledger.save_payment(
                reference,
                "",
                "verified",
                rail=self.config.rail,
                transaction_id=transaction_id,
                amount=funds.amount,
                currency=funds.currency,
            )
        return True, ""

    async def on_reject(self, reference: str, reason: str = "") -> None:
        session = self._sessions.get(reference)
        if session is not None:
            session.status = "rejected"
        if self.ledger is not None:
            await self.ledger.save_payment(reference, "", "rejected", rail=self.config.rail)

    def can_complete(self, reference: str) -> bool:
        session = self._sessions.get(reference)
        return bool(session and session.status == "verified")

    async def mark_completed(self, reference: str) -> None:
        session = self._sessions.get(reference)
        if session is None or session.status != "verified":
            raise PaymentError("refusing to complete an unverified payment")
        session.status = "completed"
        if self.ledger is not None:
            await self.ledger.save_payment(reference, "", "completed", rail=self.config.rail)

    async def cancel(self, reference: str, reason: str = "build failed") -> None:
        session = self._sessions.get(reference)
        if session is None:
            return
        if session.status == "verified":
            session.status = "cancelled"
            if self.ledger is not None:
                await self.ledger.save_payment(
                    reference,
                    "",
                    "cancelled",
                    rail=self.config.rail,
                    transaction_id=session.transaction_id,
                )
            logger.warning(
                "payment %s was verified then cancelled (%s); a refund is required out of band",
                reference,
                reason,
            )

    def expired(self, reference: str, now: float | None = None) -> bool:
        session = self._sessions.get(reference)
        if session is None:
            return True
        current = now if now is not None else time.time()
        return current - session.created_at > self.config.deadline_s

    def status(self, reference: str) -> dict[str, Any]:
        session = self._sessions.get(reference)
        if session is None:
            return {"status": "unknown"}
        return {
            "status": session.status,
            "rail": session.rail,
            "transaction_id": session.transaction_id,
            "attempts": session.attempts,
        }
