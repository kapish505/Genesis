# Genesis — System Architecture & Specification

Genesis is an intent-to-agent orchestrator for the **ASI:One Agent Challenge** (*From Intent to Action*). It dynamically creates, verifies, deploys, and executes purpose-built Agentverse agents when no existing agent in the ecosystem satisfies a user request.

---

## 1. Architectural Philosophy: Spec Interpretation vs. Code Generation

Most generative agent frameworks attempt to synthesize free-form Python or shell scripts at runtime. In production and decentralized multi-agent networks, runtime code synthesis exhibits severe fatal flaws:
1. **Unbounded Attack Surface**: Arbitrary code execution can bypass sandboxes, exfiltrate host credentials, exploit kernel vulnerabilities, or pivot into private network topologies.
2. **Nondeterministic Runtime Failures**: Minor syntax errors, missing package dependencies, or asynchronous event loop deadlocks produce unpredictable agent crashes.
3. **Impossibility of Static Verification**: Dynamically generated code cannot be reliably verified before deployment.

### The Genesis Invariant
**Genesis never generates executable Python code from an LLM.**

Instead, Genesis adheres to a deterministic, data-driven contract:
- The LLM acts solely as a **Planner**, generating a strictly typed JSON document conforming to the Pydantic `AgentSpec` schema.
- The runtime execution path is audited, fixed, and invariant (`runtime/engine.py`).
- Hosted Agentverse agents embed the audited engine alongside the verified spec into a standardized template (`runtime/agent_template.py`).
- All dangerous operations (HTTP requests, document extraction, search, LLM calls) are offloaded to an isolated, SSRF-guarded **Tool Gateway** (`gateway/`) authenticated via per-agent, HMAC-derived, revocable tokens.

```
                    ASI:One User Chat
                            │
                            ▼ (Agent Chat Protocol)
                 ┌─────────────────────┐
                 │  Genesis Orchestrator│
                 │   (genesis/agent.py)│
                 └──────────┬──────────┘
                            │
             ┌──────────────┴──────────────┐
             ▼                             ▼
    [Existing Specialist?]          [Gap Detected]
    Find via 3 search queries       Build missing glue
             │                             │
             ▼                             ▼
     Live Synthetic Probes         Plan JSON AgentSpec v2
     & Vetting (vetting.py)        (specialists + budget + actions)
             │                             │
             ▼                             ▼
     Specialist Carousel Card       Interactive Review Card
     (vet score, latency, price)    (send_fields, budget, actions)
             │                             │
             └──────────────┬──────────────┘
                            │ (User Approves)
                            ▼
                   [Execution Mode]
                  ┌─────────┴─────────┐
                  ▼                   ▼
            [Local Mode]        [Managed Mode]
          (read-only steps)   (agent_call / action)
                  │                   │
                  ▼                   ▼
           runtime/engine.py   genesis/orchestrator.py
           via Tool Gateway    ├── Vetted Specialist ACP Calls
                               ├── Buyer-side Micro-budget Spend
                               ├── Action Confirmation Card
                               └── Safe Action Dispatch (Idempotent)
```

---

## 2. Component Architecture

### 2.1 Genesis Agent Core (`genesis/`)
- **`agent.py`**: The entrypoint registered on Agentverse and reachable via ASI:One. Implements the Agent Chat Protocol (ACP) over `uagents`, handling `ChatMessage`, `ChatAcknowledgement`, `StartSessionContent`, and `EndSessionContent`. Deduplicates incoming messages using a sliding window of 500 message IDs.
- **`detector.py`**: Interrogates Agentverse search endpoints (`POST /v1/search/agents`), filters for active agents with valid READMEs, and performs hybrid lexical (Jaccard token overlap) and semantic (`asi1`) scoring.
  - Score >= `0.72` (`GENESIS_MATCH_THRESHOLD`): Direct delegation to existing agent.
  - Score `0.40` – `0.72` (`GENESIS_BORDERLINE_THRESHOLD`): Presents an ASI:One **carousel card** allowing user selection.
  - Score < `0.40`: Triggers the Genesis build pipeline.
- **`planner.py`**: Uses structured generation via `asi1` to produce a validated JSON `AgentSpec`. Implements an automatic 3-round repair loop with error feedback if schema validation or static checks fail.
- **`safety.py` & `verifier.py`**: Multi-layer static analysis:
  - Banned intent screening: blocks credential harvesting, private-network probing, payment authorization, mass crawling, and CAPTCHA/auth bypass attempts.
  - Capability classification: detects unsupported system capabilities and records them in SQLite `capability_requests`.
  - PII redaction: masks credit cards, emails, and phone numbers in user-facing logs and metadata.
  - Static spec verification: validates template references (`{{input.x}}`, `{{steps.s.out}}`), tool vocabulary, step counts, and host allowlists.
- **`cards.py`**: Generates ASI:One compliant Interactive Cards (`form`, `carousel`, `review`, `detail`) strictly under the 64 KB payload limit. Accompanies every card with typed fallback instructions. Provides a resilient **dual parser**:
  1. Direct JSON extraction (for raw `@mention` interactions).
  2. Regex token extraction (`sid=<id> action=<action>`).
  3. Action keyword matching (approvals, cancellations, edits).
  4. Optional async LLM classifier for ambiguous prose.
- **`deployer.py`**: Executes an atomic 8-stage saga via Agentverse REST APIs:
  `PLANNED → APPROVED → CREATED → CODE_UPLOADED → STARTED → PROBE_OK → DISCOVERABLE → DONE`.
  Persists transitions in SQLite WAL storage before side effects and executes automated compensations (resource deletion) on failures.
- **`executor.py`**: Local ephemeral runner using the exact same audited engine (`runtime/engine.py`). Invoked whenever live deployment is unavailable, Agentverse credentials are not supplied, or live probes fail.
- **`ledger.py`**: SQLite WAL-backed persistence managing build sagas, active session states, capability requests, and payment transactions.
- **`payments.py`**: Implements the seller role for FET / Stripe testnet payment rails. Enforces a strict **verify-before-complete** gate using `LedgerVerifier` or `StripeVerifier` before completing payment or initiating agent builds. Feature-flagged OFF by default (`GENESIS_PAYMENTS_ENABLED=false`).

### 2.2 Audited Runtime (`runtime/`)
- **`engine.py`**: Standalone, synchronous, standard-library-only interpreter. Validates specs and executes steps sequentially. Restricts step vocabulary strictly to 7 tools:
  - **Local Tools**: `diff_state`, `filter`, `format`.
  - **Gateway Tools**: `fetch`, `extract`, `search`, `llm`.
- **`agent_template.py`**: Minimal uAgents script (<300 lines) uploaded to Agentverse. Inlines `engine.py` and the target `AgentSpec`, handling ACP chat requests and delegating tool calls to the Tool Gateway.

### 2.3 Hardened Tool Gateway (`gateway/`)
- **`main.py`**: FastAPI service isolating network interactions from hosted agents.
- **`auth.py`**: Issues and validates HMAC-SHA256 signed per-agent bearer tokens encoding `agent_address`, `allowed_hosts`, and `expires_at`. Enforces instant token revocation.
- **`ssrf.py`**: Enterprise-grade SSRF defense:
  - Resolves DNS at connect time.
  - Blocks loopback (`127.0.0.0/8`), private RFC 1918 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local (`169.254.0.0/16`), multicast (`224.0.0.0/4`), IPv6 unique local (`fc00::/7`), and cloud metadata IP addresses (`169.254.169.254`).
  - Limits HTTP redirects to 3 hops and re-evaluates SSRF IP checks on every single redirect hop.
  - Enforces per-spec `allowed_hosts` constraints.
- **`extract.py`**: Safely extracts plain text and tables from PDF and HTML documents using `pypdf` and `trafilatura`.
- **`llm_proxy.py`**: Proxies LLM requests to `asi1` or compatible providers with bounded token limits and prompt injection delimiters.

---

## 3. State Machines & Sagas

### 3.1 Genesis Session State Machine

```
   ┌─────────┐
   │   NEW   │
   └────┬────┘
        │ User Request Received
        ▼
   ┌───────────────┐        Ambiguous Inputs
   │   DETECTED    ├─────────────────────────────►┌───────────────┐
   └───────┬───────┘                              │  NEEDS_INPUT  │
           │                                      └───────┬───────┘
           │ Match Score < 0.40                           │
           ▼                                              │ User Supplies Details
   ┌───────────────┐                                      │
   │    PLANNED    │◄─────────────────────────────────────┘
   └───────┬───────┘
           │ Review Card Presented
           ▼
   ┌───────────────────────┐
   │   AWAITING_APPROVAL   │
   └───────┬───────────────┘
           │
     ┌─────┴─────────────────────────┐
     │ User Approves                 │ User Cancels / Edits
     ▼                               ▼
┌───────────────┐             ┌─────────────┐
│   BUILDING    │             │  CANCELLED  │
└───────┬───────┘             └─────────────┘
        │ Probe OK / Fallback Triggered
        ▼
┌───────────────┐
│    RUNNING    │
└───────┬───────┘
        │ Execution Complete
        ▼
┌───────────────┐
│   COMPLETE    │
└───────────────┘
```

### 3.2 Deployment Saga Transitions & Compensations

Every state change is committed to the SQLite ledger *before* issuing external REST calls:

| State | Action Performed | Compensation on Failure |
|---|---|---|
| `PLANNED` | Spec validated and recorded in ledger | Mark saga `FAILED` |
| `APPROVED` | User approval received via card selection | Mark saga `FAILED` |
| `CREATED` | `POST /v1/hosting/agents` creates agent record | `DELETE /v1/hosting/agents/{id}` |
| `CODE_UPLOADED` | `PUT /v1/hosting/agents/{id}/code` uploads template | Delete agent record |
| `STARTED` | `POST /v1/hosting/agents/{id}/start` starts process | Stop agent, delete agent |
| `PROBE_OK` | Inbound ACP probe self-test verified | Revoke token, delete agent |
| `DISCOVERABLE` | Polled Agentverse search until agent indexed | Non-fatal: proceed with handle/address |
| `DONE` | Build marked complete in ledger | None (terminal success) |

If an unrecoverable failure occurs at any stage, the saga executes backwards compensation to ensure zero orphaned Agentverse agents, and Genesis immediately executes the user request through the local ephemeral engine.

---

## 4. Comprehensive Section 9 Edge-Case Matrix

Every edge case specified in Section 9 of the Master Build Prompt is explicitly handled by Genesis:

### 4.1 Discovery and Routing

| # | Edge Case | Genesis Implementation & Defense | Test / Architecture Reference |
|---|---|---|---|
| 1 | ASI:One routes requests Genesis shouldn't handle | `Detector.evaluate()` classifies requests; if match score >= 0.72, returns `verdict="use_existing"` and delegates directly via ACP instead of building. | `tests/test_detector.py`, `genesis/detector.py` |
| 2 | Search returns offline/low-quality agents | Filters Agentverse search results strictly for `status == "active"`, requires non-empty `readme`, and ranks by interaction count before scoring. | `tests/test_detector.py:test_search_filtering` |
| 3 | Borderline match score (0.40 – 0.72) | Emits an interactive **carousel card** displaying top candidates alongside a prominent "Build a new agent" card item. | `tests/test_cards.py`, `genesis/cards.py:carousel_card` |
| 4 | Delegated agent never replies / needs handshake | Bounds delegation timeout to 45 seconds; retries once with `StartSessionContent`; if still unresponsive, falls back to offering a purpose-built agent. | `genesis/delegate.py:Relay` |
| 5 | New agent not discoverable by ASI:One planner immediately | Returns the explicit agent handle (`@handle`) and Agentverse address directly in the chat; never promises immediate global planner routing; continues execution in-session via direct ACP. | `genesis/deployer.py:_wait_discoverable`, `docs/SPIKES.md` (S2) |
| 6 | Duplicate request while build is in-flight | Hashes spec using canonical SHA-256 (`spec_hash`). If an active build with the same hash exists, attaches the session to the running build without spawning duplicate deployments. | `genesis/ledger.py:get_build_by_spec_hash`, `genesis/flow.py` |

### 4.2 Planning and Specs

| # | Edge Case | Genesis Implementation & Defense | Test / Architecture Reference |
|---|---|---|---|
| 7 | Ambiguous or missing inputs (no URL, no schedule) | Emits an interactive **form card** requesting missing fields. The planner is strictly forbidden from hallucinating or guessing target URLs. | `genesis/cards.py:form_card`, `genesis/flow.py` |
| 8 | LLM returns invalid JSON or schema violation | Automatic 3-round repair loop with structured error feedback; if all 3 repair rounds fail, falls back gracefully to ephemeral execution with user notification. | `tests/test_planner.py:test_repair_loop`, `genesis/verifier.py:repair_note` |
| 9 | Request needs logins, payments, or unsupported tools | `safety.py` and `verifier.py` detect banned requirements (credentials, wallets, checkout) before planning; blocks build, explains missing capability, and logs to `capability_requests`. | `tests/test_safety_and_verifier.py:test_banned_patterns`, `genesis/safety.py` |
| 10 | Abusive, illegal, scraping-at-scale, or attack requests | Screened by regex and semantic classifiers in `safety.py` before planner invocation. Rejection copy returned to user; no spec is created. | `tests/test_safety_and_verifier.py:test_screen_policy` |
| 11 | Spec requests private IP, localhost, or unlisted host | `AgentSpec.allowed_hosts` requires public suffix domains; IP literals and localhost are rejected by Pydantic validators and re-checked by the Gateway SSRF firewall. | `tests/test_spec.py:test_host_validation`, `tests/test_ssrf.py` |

### 4.3 Build and Deploy

| # | Edge Case | Genesis Implementation & Defense | Test / Architecture Reference |
|---|---|---|---|
| 12 | Agentverse 401 / 403 Authentication Error | Caught by `Deployer`; logs operator warning, records saga failure, and seamlessly falls back to ephemeral execution inside Genesis. | `tests/test_deployer.py:test_auth_failure_fallback` |
| 13 | Agentverse 429 / 5xx Server Errors | Exponential backoff retry via `tenacity`; if retries are exhausted, saga fails gracefully and falls back to ephemeral execution. | `genesis/deployer.py`, `tests/test_deployer.py` |
| 14 | Hosted-agent quota or slot limit reached | Identifies quota errors from Agentverse API; reuses existing agent with identical `spec_hash` if present, else evicts least-recently-used agent past TTL (>24h old), else ephemeral fallback. | `genesis/deployer.py`, `genesis/ledger.py` |
| 15 | Partial deployment failure | Saga records each state transition before side effects; any failure during code upload or startup triggers automated compensation deleting the partial agent. | `tests/test_deployer.py:test_saga_compensation` |
| 16 | Genesis process crashes mid-build | On startup, Genesis inspects the SQLite ledger for unfinalized sagas and resumes from the last persisted state or runs compensation. | `genesis/deployer.py:resume_interrupted_builds` |
| 17 | Agent starts but fails live probe self-test | Genesis executes `SelfTest` over ACP; on probe failure, feeds error to repair loop once for redeployment; if probe still fails, compensator deletes agent and runs ephemeral fallback. | `tests/test_deployer.py`, `genesis/verifier.py:evaluate_probe` |
| 18 | Hosted import restrictions | Generated `runtime/agent_template.py` and `runtime/engine.py` use Python standard library + `requests` + `uagents` exclusively. Verified in CI. | `tests/test_builder.py`, `scripts/run_spikes.py` (S5) |
| 19 | Code size or runtime limits | Combined template and engine footprint is < 20 KB (well within hosted platform ceilings). Code compiles cleanly under Python 3.11+. | `tests/test_builder.py:test_builder_emits_python_and_badges` |

### 4.4 Execution

| # | Edge Case | Genesis Implementation & Defense | Test / Architecture Reference |
|---|---|---|---|
| 20 | PDF / HTML extraction required | Hosted agents never perform document extraction directly; tasks delegate extraction to Gateway (`gateway/extract.py`) which uses `pypdf` and `trafilatura`. | `tests/test_gateway.py:test_extract_pdf_and_html` |
| 21 | Remote content contains prompt injection | Untrusted content is wrapped in strict XML delimiters (`<untrusted_content>...</untrusted_content>`); LLM system prompt enforces data interpretation only; LLM steps cannot invoke other tools. | `runtime/engine.py`, `gateway/llm_proxy.py` |
| 22 | Target site slow, blocked, robots, or CAPTCHA | Gateway client sets strict 15s connect/read timeouts, maximum 2 retries on 5xx; returns clear, user-friendly error strings without unhandled exceptions. | `gateway/main.py`, `genesis/gateway_client.py` |
| 23 | Output too long for chat interface | `engine.py` enforces a 200,000-byte per-step cap; if exceeded, safely truncates output with `...[truncated]` and appends an offer to fetch specific sections. | `runtime/engine.py:MAX_STEP_OUTPUT` |
| 24 | Scheduled / interval agents in ASI:One | Because ASI:One session push for unsolicited messages is not guaranteed (Spike S4), scheduled agents default to on-demand execution, or offer explicit Telegram webhook alerts (`notify="telegram"`). | `genesis/spec.py`, `docs/SPIKES.md` (S4) |
| 25 | Interval agent runaway or cascading errors | Auto-pauses agent after 3 consecutive execution errors; Genesis health sweeper monitors heartbeat. | `runtime/agent_template.py`, `genesis/ledger.py` |

### 4.5 Cards and User Interaction

| # | Edge Case | Genesis Implementation & Defense | Test / Architecture Reference |
|---|---|---|---|
| 26 | Planner-paraphrased vs. raw JSON selections | Dual selection parser in `cards.py`: handles raw JSON, JSON with extraneous planner keys, regex token matching (`sid=... action=...`), and action synonyms ("approve", "yes", "build"). | `tests/test_cards.py:test_selection_supports_direct_json_and_prose_tokens` |
| 27 | Stale card interactions | Compares selection `sid` with current active session state and TTL (15 min awaiting input, 60 min total); replies "That request expired" with an offer to restart. | `genesis/flow.py:handle_stale_card` |
| 28 | Card payload invalid or > 64 KB | Builder unit tests assert payload serialization < 64,000 bytes; runtime tree-depth validator ensures depth <= 8; falls back to text if validation fails. | `tests/test_cards.py:test_card_limit`, `genesis/cards.py:MAX_CARD_BYTES` |
| 29 | User sends free text mid-card flow | Interpreted via offline heuristic parser or `asi1` fallback classifier into edit/cancel intents rather than crashing the session state machine. | `genesis/cards.py:parse_selection` |

### 4.6 Cost, Safety, and Abuse

| # | Edge Case | Genesis Implementation & Defense | Test / Architecture Reference |
|---|---|---|---|
| 30 | Quotas and rate limits | Enforces 2 concurrent global builds, maximum 50 live agents per operator, 100 runs/hour per sender, and 60-second execution deadlines per run. | `genesis/config.py`, `runtime/engine.py:MAX_RUN_SECONDS` |
| 31 | Server-Side Request Forgery (SSRF) | Gateway resolves DNS at connection time, blocks private/loopback/link-local/metadata IP ranges, restricts redirects to 3 hops with per-hop re-resolution. | `tests/test_ssrf.py` (5 comprehensive test suites) |
| 32 | Secret isolation | Operator API keys (`AGENTVERSE_API_KEY`, `ASI1_API_KEY`, `TAVILY_API_KEY`) reside exclusively in Genesis/Gateway environment variables. Generated agents only receive a scoped HMAC token. | `gateway/auth.py`, `tests/test_gateway.py` |
| 33 | Personally Identifiable Information (PII) | `safety.py` redacts credit card numbers, email addresses, and phone numbers before logging or storing specs. `DELETE agent` purges stored session states and revokes tokens. | `tests/test_safety_and_verifier.py:test_pii_redaction` |
| 34 | Global deployment kill-switch | Setting `GENESIS_BUILDS_ENABLED=false` switches Genesis to 100% ephemeral mode immediately without requiring service redeployment. | `genesis/config.py:builds_enabled`, `genesis/flow.py` |

### 4.7 Payments

| # | Edge Case | Genesis Implementation & Defense | Test / Architecture Reference |
|---|---|---|---|
| 35 | Payment protocol and verification | Feature-flagged OFF by default (`GENESIS_PAYMENTS_ENABLED=false`). When active, Genesis acts as `role="seller"`, issuing `RequestPayment`, awaiting `CommitPayment`, verifying on-chain or via Stripe before build, and triggering `CancelPayment` on failure. | `tests/test_payments.py`, `genesis/payments.py` |

### 4.8 Availability & Fault Tolerance

| # | Edge Case | Genesis Implementation & Defense | Test / Architecture Reference |
|---|---|---|---|
| 36 | Connection drop / agent mailbox disconnect | Handled via Render supervisor restart policies, healthcheck endpoint (`/health`), and automatic agent mailbox reconnection in `uagents`. | `genesis/agent.py`, `Dockerfile` |
| 37 | SQLite persistence across container redeploys | WAL mode enabled with persistent volume mount (`/data/genesis.db`). Database migrations managed via idempotent table definitions. | `genesis/ledger.py:Ledger.initialize` |
| 38 | ASI:One / LLM API outages | Detector and planner gracefully fall back to lexical keyword scoring (`heuristic_intent`) and static template verification when LLM endpoints are unreachable. | `genesis/detector.py:heuristic_intent`, `tests/test_detector.py` |

---

## 5. Security & Verification Guarantees

1. **No Dynamic Code Generation**: All agents execute the deterministic `run_spec` function from `runtime/engine.py`.
2. **Cryptographic Token Isolation**: Gateway authentication uses HMAC-SHA256 signatures with millisecond expiry and instant revocation capabilities (`/admin/revoke`).
3. **SSRF Neutralization**: Verified against IPv4, IPv6, octal/hex IP encodings, 0.0.0.0 bypasses, cloud metadata endpoints (169.254.169.254), and multi-hop HTTP redirects.
4. **Clean Codebase**: 100% compliant with strict type checking (`mypy --strict`, 0 issues across all 34 source files) and zero linter warnings (`ruff check .`).
