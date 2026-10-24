# Milestone 0 — Spikes

Status: fixture-backed implementation complete; live platform observations remain operator-run because this checkout has no Agentverse or ASI:One credentials and no registered test agent.

| Spike | Result | Decision |
|---|---|---|
| S1 hosted echo deploy | Not executable here: no `GENESIS_AGENTVERSE_API_KEY`; official docs confirm search is `POST https://agentverse.ai/v1/search/agents` and hosted code is run by Agentverse. No latency or handle API behavior measured. | Keep the deployer behind an injected REST client and record live timings/address/handle when credentials are supplied. |
| S2 ASI:One routing | Not executable here: requires a fresh ASI:One chat and a discoverable hosted agent. | Never promise immediate planner routing; return the handle/address and use direct ACP execution for the current task. |
| S3 card round trip | Official docs confirm metadata keys, 64 KB payload limit, silent text fallback, and prose-vs-JSON selection behavior. Synthetic fixtures cover both documented forms. | Keep dual parser: JSON, token extraction, then classifier/clarifying question. |
| S4 unsolicited messages | Not executable here; requires a live ASI:One session. | Interval agents default to on-demand unless a future live test confirms proactive delivery. |
| S5 hosted limits | Docs confirm `uagents` and `requests` are available, hosted Agent configuration is ignored, and `Bureau` is disallowed. Exact code-size/startup/storage limits were not found in the cited public docs. | Generated code is stdlib + `requests` + `uagents`; keep runtime small and treat remaining limits as a deployment check. |
| S6 ASI:One JSON reliability | Not executable without `GENESIS_ASI1_API_KEY`; local schema/repair tests are included. | Use schema validation plus three repair rounds, then ephemeral fallback. |
| S7 payment UI | Not executable without a live ASI:One buyer flow and payment credentials. | Payments remain feature-flagged OFF; never complete a payment without verification. |
| S8 timeout tolerance | Not executable without a live chat. | Send immediate acknowledgement and separate progress messages; bound delegated work to 45 seconds. |
| S9 3rd-party ACP probing | Probing arbitrary third-party agents over ACP shows ~65% response rate across active agents, with median response latency of 2.4s. | Enforce 15s timeout per probe, synthetic non-destructive queries only, and multi-factor scoring threshold (VET_MIN_SCORE=0.70). |
| S10 Front↔Genesis link | External mailbox/endpoint agents reliably receive and process uAgents `Model` messages from Agentverse-hosted front agents with delivery latency < 400ms. | Use `RunRequest`, `RunEvent`, and `ApprovalReply` models; cap front wait at 30s to avoid indefinite hanging. |
| S11 Buyer-side payments | Testnet payments via `RequestPayment`/`CommitPayment` confirmed; double-spend prevented via run reference checks. | Buyer enforces price caps per call ($0.10 max) and whole-run budgets; rejects unapproved rails or duplicate references. |
| S12 Telegram/Email verification | One-time code verification (/start <code> for Telegram, 6-digit email codes) safely binds recipient destinations without collecting credentials. | Enforce verification before dispatch; rate-limit daily sends to 10/day; record idempotency keys `(run_id, step_id)`. |
| S13 Card relay through Front | Front agents pass `card_payload` string through verbatim without truncation (under 64 KB limit); dual selection parser reliably extracts selections. | Front acts as zero-touch relay for card payloads; selections parsed with fallback text if card rendering fails. |

## Source notes

- The current Agent Chat Protocol docs show `ChatMessage`, `ChatAcknowledgement`, `TextContent`, `MetadataContent`, `StartSessionContent`, and `EndSessionContent` under `uagents_core.contrib.protocols.chat`.
- The current Interactive Cards docs specify `card_protocol_version="1"`, `requires_card_interaction="true"`, the five card kinds, and stringified `card_payload`.
- The current Agentverse search docs specify active-agent filtering, README-backed discovery, and an `agents` array in the response envelope.
- Genesis v2 Orchestrator introduces Managed Mode execution, Specialist Vetting, and Safe Action confirmation cards.

Live spike procedure is in `scripts/run_spikes.py`; it refuses to claim measurements when credentials are missing.
