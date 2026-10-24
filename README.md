# Genesis — From Intent to Action on ASI:One

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3) ![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1) ![tag:domain/automation](https://img.shields.io/badge/automation-1F8A70)

> **ASI:One Agent Challenge Entry — Genesis v2 (Orchestrator Redesign)**  
> *"Genesis hires the right specialists from Agentverse, vets them, pays them, and builds only the missing piece, all inside one ASI:One chat."*

---

## 1. Executive Summary

When a user interacts with **ASI:One**, existing agents in the **Agentverse** catalog can solve many recurring tasks. However, niche, bespoke, or newly emerging workflows frequently fail because no single agent can accomplish multi-part requests requiring specialized data, quantitative scoring, and safe real-world alerts.

**Genesis v2 acts as an intelligent Orchestrator and Broker**:
1. **Discovers Specialists**: Queries Agentverse for candidate agents across multiple phrasing angles, deduplicating candidates.
2. **Vets via Live Probes**: Conducts synthetic live ACP probes (latency, contract satisfaction, repeatability, and safety screening) to score candidates before hiring.
3. **Hires & Sets Micro-Budgets**: Selects primary and fallback specialists under a strict per-call price cap and per-run budget.
4. **Builds the Missing Glue**: Uses `asi1` to generate a strictly typed `AgentSpecV2` implementing missing data transformations or formatting.
5. **Safe Real Actions**: Integrates Telegram and Email notification actions guarded by explicit user confirmation cards and cryptographic verification codes.
6. **Managed Execution Mode**: Thin front agents deployed to Agentverse relay communication to Genesis's secure Orchestrator.
7. **Ephemeral First, Promote Later**: Delivers fast answers immediately via ephemeral execution, deploying and promoting hosted agents only for stateful, scheduled, or user-retained workflows.

---

## 2. Core Architecture & Philosophy

```
                    ASI:One User Chat
                            │
                            ▼ (Agent Chat Protocol)
                 ┌─────────────────────┐
                 │    Genesis Agent    │
                 │   (genesis/agent.py)│
                 └──────────┬──────────┘
                            │
             ┌──────────────┴──────────────┐
             ▼                             ▼
    [Existing Agent?]              [Gap Detected]
    Score >= 0.72                  Score < 0.40
             │                             │
             ▼                             ▼
     Delegate via ACP             Plan JSON AgentSpec
                                           │
                                           ▼
                                 Interactive Review Card
                                 (Approve / Edit / Cancel)
                                           │ (Approve)
                                           ▼
                                 Deployer Saga (REST)
                                 1. Create Agentverse agent
                                 2. Upload template + spec
                                 3. Start agent
                                 4. ACP self-test probe
                                           │
                                 ┌─────────┴─────────┐
                                 │                   │
                               (Pass)             (Fail)
                                 │                   │
                                 ▼                   ▼
                           ACP Handshake      Ephemeral Engine
                           & Execution        Fallback Execution
                                 │                   │
                                 └─────────┬─────────┘
                                           ▼
                                    Tool Gateway
                               (SSRF Safe / HMAC Token)
```

### Spec Interpretation vs. Code Generation
Unlike conventional generative systems that write raw Python code (which introduces remote code execution risks, syntax bugs, and dependency mismatches), Genesis follows an invariant:

> **Generated agents are data-driven spec interpreters, not synthesized code.**

- The LLM acts solely as a **Planner**, generating structured JSON that conforms to the Pydantic `AgentSpec` schema.
- The execution path is fixed and audited (`runtime/engine.py`).
- Hosted agents embed this engine and communicate with the outside world through the hardened **Tool Gateway** (`gateway/`), authenticated by HMAC-scoped, revocable tokens.
- Complete SSRF defense blocks internal RFC 1918 IPs, loopback, link-local, and cloud metadata (169.254.169.254) addresses with DNS re-resolution and redirect validation.

---

## 3. Section 12 Showcase Scenarios

Genesis includes pre-warmed, validated specifications for all three showcase scenarios:

### Scenario 1: College Mess Menu PDF Checker
- **Prompt**: *"Check this college mess menu PDF and tell me if paneer is served this week."*
- **Pipeline**: `fetch` ➔ `extract` (PDF table parser) ➔ `llm` (menu analyzer) ➔ `format`
- **Output**: Detailed day-by-day meal schedule highlighting paneer dishes.

### Scenario 2: Government Scheme Policy Diff Tracker
- **Prompt**: *"Summarize the latest changes on this government scheme page since last time."*
- **Pipeline**: `fetch` ➔ `extract` (HTML body extractor) ➔ `diff_state` (diffs against persistent SQLite state) ➔ `llm` (bulleted policy summary) ➔ `format`
- **Output**: Chronological diff highlighting changes in subsidies, eligibility, and program limits.

### Scenario 3: Mandi Tomato Price Threshold Checker
- **Prompt**: *"Find today's mandi price for tomato in my district from this URL and tell me if it is above ₹30."*
- **Pipeline**: `fetch` ➔ `extract` (HTML tables) ➔ `filter` (district & threshold match) ➔ `format`
- **Output**: District-level modal commodity prices flagged with threshold alerts.

You can execute all three demo scenarios immediately using the built-in runner:
```bash
python scripts/demo_runner.py --demo all
```

---

## 4. Quickstart & Installation

### Prerequisites
- Python 3.11, 3.12, 3.13, or 3.14
- Virtual environment (`venv`)

### 1. Clone & Install
```bash
# Clone the repository
git clone https://github.com/your-username/genesis.git
cd genesis

# Create and activate virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies (core + dev + gateway)
pip install -e '.[dev,gateway]'

# Configure environment variables
cp .env.example .env
```

### 2. Run Verification Suite
Verify that all 40 tests, strict type checks, linter rules, and platform spikes pass:
```bash
# Run pytest test suite (40 tests across all modules)
pytest

# Verify code style and imports (0 errors)
ruff check .

# Verify strict type safety (0 issues in 34 source files)
mypy --strict genesis runtime gateway

# Run Milestone 0 Spikes validation
python scripts/run_spikes.py
```

---

## 5. Running Genesis

### Local Gateway & Agent Execution
Genesis consists of two main runtime processes:

1. **Tool Gateway** (SSRF isolation & tool execution):
```bash
uvicorn gateway.main:app --host 0.0.0.0 --port 8080 --reload
```

2. **Genesis Agent** (ACP server connected to Agentverse / ASI:One):
```bash
python -m genesis.agent
```

### Environment Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `GENESIS_ENV` | `development` | Deployment environment (`development` / `production`) |
| `GENESIS_BUILDS_ENABLED` | `true` | Global kill-switch; when `false`, forces 100% ephemeral mode |
| `GENESIS_DATABASE_PATH` | `./genesis.db` | Path to persistent SQLite WAL database |
| `GENESIS_GATEWAY_URL` | `http://localhost:8080` | URL of the Tool Gateway service |
| `GENESIS_GATEWAY_SECRET` | *(secret)* | Secret key used to sign per-agent HMAC tokens |
| `GENESIS_AGENTVERSE_API_KEY` | *(optional)* | Agentverse API key for hosted agent deployments |
| `GENESIS_ASI1_API_KEY` | *(optional)* | ASI:One API key for LLM planning and intent extraction |
| `GENESIS_TAVILY_API_KEY` | *(optional)* | Tavily Search API key for web search tools |
| `GENESIS_MATCH_THRESHOLD` | `0.72` | Score threshold to delegate to existing agents |
| `GENESIS_BORDERLINE_THRESHOLD`| `0.40` | Score threshold to display carousel options |
| `GENESIS_PAYMENTS_ENABLED` | `false` | Enable FET / Stripe testnet payment rails |

---

## 6. Cloud Deployment (Render)

Genesis is designed for zero-downtime deployment on Render:

1. **Deploy Tool Gateway (`web`)**:
   - **Environment**: Docker (`Dockerfile`) or Python
   - **Build Command**: `pip install -e '.[gateway]'`
   - **Start Command**: `uvicorn gateway.main:app --host 0.0.0.0 --port $PORT`
   - **Healthcheck Path**: `/health`

2. **Deploy Genesis Agent (`worker`)**:
   - **Environment**: Python
   - **Build Command**: `pip install -e .`
   - **Start Command**: `python -m genesis.agent`
   - **Persistent Disk**: Mount persistent disk to `/data` and set `GENESIS_DATABASE_PATH=/data/genesis.db`.

---

## 7. Submission Details & Challenge Alignment

- **Innovation Lab Domain**: Automation & Autonomous Systems
- **Agentverse Registration**: Reachable via ASI:One Chat under `@genesis-agent`
- **Agent Chat Protocol (ACP)**: Fully implemented with `ChatMessage`, `ChatAcknowledgement`, `StartSessionContent`, `EndSessionContent`, and Interactive Cards.
- **Milestone 0 Evidence**: See [docs/SPIKES.md](docs/SPIKES.md) for detailed measurements and platform findings.
- **Architecture & Section 9 Edge-Case Matrix**: See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the 38 edge-case defense strategies.

---

## 8. License

Distributed under the Apache 2.0 License.
