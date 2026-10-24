"""Genesis Local Chat Tester — Test Genesis backend directly without browser or mailbox latency."""

from __future__ import annotations

import asyncio
import sys
import time

from genesis.agent import GenesisRuntime
from genesis.builder import Builder
from genesis.config import get_settings
from genesis.delegate import Relay, ReplyRouter
from genesis.deployer import Deployer
from genesis.executor import Executor
from genesis.flow import GenesisFlow
from genesis.ledger import Ledger
from genesis.llm import AsiOneClient
from genesis.payments import PaymentConfig, SellerPayments
from genesis.search import AgentverseClient
from genesis.session import SessionStore


async def run_local_chat(prompt: str) -> None:
    settings = get_settings()
    settings.asi1_model = "asi1-mini"  # Low-latency model for instant verification
    ledger = Ledger(":memory:")
    await ledger.initialize()
    sessions = SessionStore(settings)

    llm = AsiOneClient(
        settings.asi1_api_key,
        settings.asi1_base_url,
        settings.asi1_model,
        settings.asi1_fast_model,
    )
    search = AgentverseClient(settings.agentverse_base_url, settings.agentverse_api_key)
    router = ReplyRouter()

    async def mock_send(target: str, msg: any) -> None:
        pass

    relay = Relay(mock_send, router)
    builder = Builder()
    deployer = Deployer(search, ledger, builder, settings, None)
    executor = Executor(settings, settings.gateway_secret)
    payments = SellerPayments(PaymentConfig(enabled=False), ledger, None)

    flow = GenesisFlow(
        settings, ledger, sessions, llm, search, relay, executor, deployer, builder, payments
    )

    print(f"\n=======================================================")
    print(f"PROMPT: {prompt}")
    print(f"=======================================================")
    start_time = time.perf_counter()
    reply = await flow.handle_message("local_dev_user", prompt)
    duration = time.perf_counter() - start_time

    print(f"\n[OK] Backend processed successfully in {duration:.2f} seconds!")
    print(f"Total events produced: {len(reply.events)}\n")

    for i, (kind, payload) in enumerate(reply.events, 1):
        if kind == "text":
            print(f"[{i}] TEXT RESPONSE:")
            print(f"    {payload.get('text')}\n")
        elif kind == "card":
            print(f"[{i}] INTERACTIVE CARD (kind: {payload.get('kind')}):")
            print(f"    Text: {payload.get('text')}")
            print(f"    Metadata Keys: {list(payload.get('metadata', {}).keys())}\n")
        elif kind == "end":
            print(f"[{i}] END SESSION EVENT\n")


if __name__ == "__main__":
    test_prompt = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "Track FET market volume and send me a Telegram alert if risk spikes above moderate. Budget cap: 0.05 FET."
    )
    asyncio.run(run_local_chat(test_prompt))
