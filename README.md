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
┌─────────────┐         ┌──────────────────┐           ┌─────────────────┐
│ Market-Data │ ───────▶│ Feature Vector   │ ──────────▶│ Strategy Agents │
│   Streams   │         │   Builder        │           └────────┬────────┘
└─────────────┘         └──────────────────┘                    │ raw signals
        ▲                         ▲                            ▼
        │ on-chain events         │ whale alerts      ┌──────────────────┐
┌──────────────┐         ┌────────┴─────────┐          │ Ensemble / Risk │
│ On-Chain Intel│────────▶ Liquidity Agent  │──────────▶  & Treasury     │
└──────────────┘         └────────┬─────────┘          └────────┬────────┘
                                   │ intents approved           │ order basket
                                   ▼                            ▼
                            ┌───────────────┐           ┌─────────────────┐
                            │ Wallet/Custody│───────────▶ Execution Agent │
                            └───────────────┘           └─────────────────┘
```
Each block corresponds to one or more MCP tools (Temporal workflows) described below.

## Durable Tools Catalog

| Tool (Workflow)            | Purpose                                                | Typical Triggers        |
|----------------------------|--------------------------------------------------------|-------------------------|
| `subscribe_cex_stream`     | Fan-in WebSocket order-book & trade ticks from CEXs    | Startup, reconnect      |
| `subscribe_dex_blocks`     | Monitor confirmed & pending DEX swaps on-chain         | New block               |
| `subscribe_mempool_whales` | Emit alerts for large mempool txs or watch-list wallets| Pending tx              |
| `compute_feature_vector`   | Join ticks, funding, on-chain data into feature rows   | Market tick             |
| `evaluate_strategy_<name>` | Generate raw buy/sell signals for a strategy           | Feature row             |
| `ensemble_signals`         | Rank & weight raw signals into order intents           | Signal batch            |
| `pre_trade_risk_check`     | Enforce VaR, leverage, wallet exposure                 | Order intents           |
| `rebalance_portfolio`      | Route orders to reach target positions                 | Approved intents        |
| `submit_cex_order`         | Durable CEX order placement & tracking                 | Portfolio rebalance     |
| `swap_via_dex_aggregate`   | Execute DEX swaps with slippage & gas guards           | Portfolio rebalance     |
| `sign_and_send_tx`         | MPC/HSM signing & broadcast                            | Execution               |
| `watchdog_heartbeat`       | Pages if any agent stops kicking within T seconds      | Continuous              |
| `archive_trade_decision`   | Persist full vector→intent→tx history for audit        | Trade settled           |

See `docs/tools.md` for full parameter and schema definitions.

## Getting Started

### Prerequisites

| Requirement  | Version      | Notes                                        |
|--------------|--------------|----------------------------------------------|
| Python       | 3.11 or newer| Data & strategy agents                       |
| Node.js      | 20 LTS       | MCP server & tooling                         |
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
pip install -r requirements.txt

# Start MCP server (hot reload)
npm install
npm run dev
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
   curl -X POST http://localhost:8080/tools/SubscribeCEXStream \
     -H 'Content-Type: application/json' \
     -d '{"exchange": "coinbaseexchange", "symbols": ["BTC/USD"], "interval_sec": 1}'
   ```
3. `SubscribeCEXStream` records ticks to the `market_tick` signal.
4. The feature engineering agent processes those ticks via `ComputeFeatureVector`.
5. The momentum agent emits buy/sell signals using `EvaluateStrategyMomentum`.
6. The ensemble agent approves intents with `PreTradeRiskCheck` and publishes them to the `IntentBus`.
7. The mock execution agent picks up approved intents and prints simulated order fills.

If Binance is blocked in your region, pass `"exchange": "coinbaseexchange"` when starting workflows such as `SubscribeCEXStream`. Use trading pairs like `BTC/USD`. For private Coinbase endpoints, set `COINBASEEXCHANGE_API_KEY` and `COINBASEEXCHANGE_SECRET` in your environment.

`SubscribeCEXStream` automatically restarts itself via Temporal's *continue as new*
mechanism after a configurable number of cycles to prevent unbounded workflow
history growth. The default interval is one hour (3600 cycles) and can be
changed by setting the `STREAM_CONTINUE_EVERY` environment variable.

## Development Workflow
- Create a new tool under `tools/` and register it with the MCP server.
- Write a strategy agent in `agents/` that calls your tool via the MCP client SDK. Use `subscribe_vectors(symbol)` from `agents.feature_engineering_agent` to stream processed feature rows into your strategy logic.
- Unit-test determinism with `make replay` to replay recent workflows.
- Hot-reload – both MCP server and Python workers use `--watch` for instant feedback.
- Deploy – push to `main`; CI builds a Docker image and promotes to your Temporal namespace.

## Repository Layout
```
├── agents/          # Strategy & system agents (Python workers)
├── tools/           # Durable tool workflows (TypeScript + Temporal SDK)
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
