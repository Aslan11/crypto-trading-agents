# Crypto Durable Trading Agents

A 24×7 multi-agent crypto trading stack built on Temporal and Model Context Protocol (MCP). Every `@mcp.tool()` is backed by a deterministic Temporal workflow, providing exactly-once execution, automatic retries and full replay for audit and compliance.

## Table of Contents
- [Background](#background)
- [Architecture](#architecture)
- [Durable Tools Catalog](#durable-tools-catalog)
- [Getting Started](#getting-started)
- [Demo](#demo)
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
                 ┌─────┐
                 │User │
                 └─┬───┘
                   │ commands
                   ▼
           ┌──────────────────┐
           │  Broker Agent    │
           └──────┬───────────┘
                  │ start_market_stream
                  ▼
           ┌──────────────────┐
           │ Market Stream WF │
           └──────┬───────────┘
                  │ ticks
                  ▼
           ┌──────────────────┐
           │ Feature Vector   │
           └──────┬───────────┘
                  │ history
                  ▼
           ┌──────────────────┐
           │ Ensemble Agent   │<─────────────┐
           └──────┬───────────┘              │
                  │ orders                   │ nudges
                  ▼                          │
           ┌──────────────────┐              │
           │ Mock Order WF    │              │
           └──────┬───────────┘              │
                  │ fills                    │
                  ▼                          │
           ┌──────────────────┐              │
           │ Execution Ledger │              │
           └──────────────────┘              │
                                            ▼
                                    ┌──────────┐
                                    │ Nudge WF │
                                    └──────────┘
```
Each block corresponds to one or more MCP tools (Temporal workflows) described below.

## Durable Tools Catalog

| Tool (Workflow)            | Purpose                                                | Typical Triggers        |
|----------------------------|--------------------------------------------------------|-------------------------|
| `subscribe_cex_stream`   | Fan-in ticker data from centralized exchanges  | Startup, reconnect    |
| `start_market_stream`    | Begin streaming market data for selected pairs | Auto-started by broker after pair selection |
| `ComputeFeatureVector`   | Store tick history for queries                | Market tick           |
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

Required environment variables:

- `OPENAI_API_KEY` – enables the broker and ensemble agents to use OpenAI models.
- `COINBASEEXCHANGE_API_KEY` and `COINBASEEXCHANGE_SECRET` – API credentials for Coinbase Exchange.
- `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE` and `TASK_QUEUE` – Temporal connection settings (defaults are shown in `.env`).
- `MCP_PORT` – port for the MCP server (defaults to `8080`).

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
2. When prompted for trading pairs, tell the broker agent **"all of them"**.
   This instructs it to begin streaming data for every supported pair.
3. `start_market_stream` spawns a `subscribe_cex_stream` workflow that
   broadcasts each ticker to its `ComputeFeatureVector` child.
4. The ensemble agent wakes up periodically via a scheduled workflow and
   analyzes market data to decide whether to trade using `place_mock_order`.
5. Filled orders are recorded in the
   `ExecutionLedgerWorkflow`.


`subscribe_cex_stream` automatically restarts itself via Temporal's *continue as new*
mechanism after a configurable number of cycles to prevent unbounded workflow
history growth. The default interval is one hour (3600 cycles) and can be
changed by setting the `STREAM_CONTINUE_EVERY` environment variable. The workflow
also checks its current history length and continues early when it exceeds
`STREAM_HISTORY_LIMIT` (defaults to 9000 events).
`ComputeFeatureVector` behaves the same way using the `VECTOR_CONTINUE_EVERY`
and `VECTOR_HISTORY_LIMIT` environment variables.

## Repository Layout
```
├── agents/          # Broker and ensemble agents
├── tools/           # Durable workflows used as MCP tools
├── mcp_server/      # FastAPI server exposing the tools
├── worker/          # Temporal worker loading workflows
├── tests/           # Unit tests for tools and agents
├── run_stack.sh     # tmux helper to launch local stack
└── ticker_ui_service.py  # Simple websocket ticker UI
```

## Contributing
Pull requests are welcome! Please open an issue first to discuss your proposed change. Make sure to:

- Run `make lint test` and fix any CI failures.
- Keep new tools deterministic (no nondeterministic I/O inside workflows).
- Write docs – every public agent or tool needs at least minimal usage notes.

## License

This project is released under the MIT License – see `LICENSE` for details.
