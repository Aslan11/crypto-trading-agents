# Crypto Durable Trading Agents

A 24×7 multi-agent crypto trading stack built on Temporal and Model Context Protocol (MCP). Every `@mcp.tool()` is backed by a deterministic Temporal workflow, providing exactly-once execution, automatic retries and full replay for audit and compliance.

## Table of Contents
- [Background](#background)
- [Architecture](#architecture)
- [Durable Tools Catalog](#durable-tools-catalog)
- [Getting Started](#getting-started)
- [Demo](#demo)
- [Development Workflow](#development-workflow)
- [Repository Layout](#repository-layout)
- [Contributing](#contributing)
- [License](#license)

## Background
Crypto markets never close. Building an automated trading system therefore demands:

- **Continuous orchestration** – agents must coordinate 24×7 without downtime.
- **Deterministic audit trails** – regulators require you to replay the exact decision path for every trade.
- **Cross-venue execution** – liquidity lives on both CEXs and DEXs; routing logic has to be durable.

Temporal supplies resilient workflows while MCP gives agents a shared, tool-based contract. This repo combines them into a modular engine you can extend one agent at a time.

## Architecture
```
┌─────────────┐    ┌──────────────────┐    ┌─────────────────┐
│ Market Data │ ──▶│ Feature Vectors  │ ──▶│ Strategy Agents │
│   Streams   │    │     Store        │    └───────┬─────────┘
└─────────────┘    └──────────────────┘            │ signals
                                                   ▼
                                         ┌──────────────────┐
                                         │ Ensemble & Risk  │
                                         └────────┬─────────┘
                                                  │ intents
                                                  ▼
                                         ┌──────────────────┐
                                         │    Intent Bus    │
                                         └────────┬─────────┘
                                                  │ intents
                                                  ▼
                                         ┌──────────────────┐
                                         │ Execution Service│
                                         └────────┬─────────┘
                                                  │ fills
                                                  ▼
                                         ┌──────────────────┐
                                         │ Execution Ledger │
                                         └──────────────────┘
```
Each block corresponds to one or more MCP tools (Temporal workflows) described below.

## Durable Tools Catalog

| Tool (Workflow)            | Purpose                                                | Typical Triggers        |
|----------------------------|--------------------------------------------------------|-------------------------|
| `subscribe_cex_stream`   | Fan-in ticker data from centralized exchanges  | Startup, reconnect    |
| `ComputeFeatureVector`   | Compute rolling indicators from ticks          | Market tick           |
| `evaluate_strategy_momentum` | Log momentum signals (optional cooldown)     | Feature vector        |
| `pre_trade_risk_check`      | Validate intents against simple VaR limits     | Order intents         |
| `IntentBus`              | Broadcast approved intents to subscribers      | Approved intents      |
| `PlaceMockOrder`         | Simulate order execution and return a fill     | Portfolio rebalance   |
| `SignAndSendTx`          | Sign and broadcast an EVM transaction          | Execution             |
| `ExecutionLedgerWorkflow`| Track fills and positions in memory            | Fill events           |


## Getting Started

### Prerequisites

| Requirement  | Version      | Notes                                        |
|--------------|--------------|----------------------------------------------|
| Python       | 3.11 or newer| Data & strategy agents                       |
| Temporal CLI | 1.24+        | `brew install temporal` or use Temporal Cloud|
| Docker       | latest       | Local infra (Redis, Kafka, Postgres)         |

### Quick Setup (local dev)
```bash
# Clone and bootstrap
git clone https://github.com/your-org/durable-crypto-agents.git
cd durable-crypto-agents
make dev-up        # spins up Temporal + infra via docker-compose

# Activate Python env
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Start MCP server
python mcp_server/app.py
```
Point your agent workers at `localhost:8080` (default MCP port) and confirm health at <http://localhost:8080/healthz>.

## Demo

The quickest way to see the stack in action is to run the included `run_stack.sh` script which launches everything in a single `tmux` session.

```bash
./run_stack.sh
```
This starts the Temporal dev server, Python worker, MCP server and several sample agents. Each component runs in its own `tmux` pane so you can watch log output as orders flow through the system. Detach from the session with `Ctrl-b d` and reattach anytime by running the script again.

### Walking through the demo
1. With the tmux session running, open a new terminal window.
2. Kick off a market data workflow for Bitcoin:
   ```bash
   curl -X POST http://localhost:8080/mcp/tools/subscribe_cex_stream \
     -H 'Accept: application/json, text/event-stream' \
     -H 'Content-Type: application/json' \
     -d '{"symbols": ["BTC/USD"], "interval_sec": 1}'
  ```
3. `subscribe_cex_stream` records ticks to the `market_tick` signal.
4. The feature engineering service processes those ticks via `ComputeFeatureVector`.
5. The momentum service emits buy/sell signals using `evaluate_strategy_momentum` and continues processing while the tool runs.
6. The ensemble agent approves intents with `pre_trade_risk_check`, then
   immediately calls `place_mock_order` to record a fill in the
   `ExecutionLedgerWorkflow`.


`subscribe_cex_stream` automatically restarts itself via Temporal's *continue as new*
mechanism after a configurable number of cycles to prevent unbounded workflow
history growth. The default interval is one hour (3600 cycles) and can be
changed by setting the `STREAM_CONTINUE_EVERY` environment variable. The workflow
also checks its current history length and continues early when it exceeds
`STREAM_HISTORY_LIMIT` (defaults to 9000 events).
`ComputeFeatureVector` behaves the same way using the `VECTOR_CONTINUE_EVERY`
and `VECTOR_HISTORY_LIMIT` environment variables.

## Development Workflow
- Create a new tool under `tools/` and register it with the MCP server.
- Write a strategy agent in `agents/` that calls your tool via the MCP client SDK. Use `subscribe_vectors(symbol)` from `agents.feature_engineering_service` to stream processed feature rows into your strategy logic.
- Unit-test determinism with `make replay` to replay recent workflows.
- Hot-reload – both MCP server and Python workers use `--watch` for instant feedback.
- Deploy – push to `main`; CI builds a Docker image and promotes to your Temporal namespace.

## Repository Layout
```
├── agents/          # Strategy & system agents (Python workers)
├── tools/           # Durable tool workflows (Python + Temporal SDK)
├── scripts/         # One-off maintenance & retraining jobs
├── infra/           # docker-compose, terraform, helm charts
├── docs/            # Deep-dives, ADRs, tool schemas
└── tests/           # Pytest & Playwright test suites
```

## Contributing
Pull requests are welcome! Please open an issue first to discuss your proposed change. Make sure to:

- Run `make lint test` and fix any CI failures.
- Keep new tools deterministic (no nondeterministic I/O inside workflows).
- Write docs – every public agent or tool needs at least minimal usage notes.

## License

This project is released under the MIT License – see `LICENSE` for details.
