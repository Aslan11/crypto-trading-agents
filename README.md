# Crypto Durable Trading Agents

A 24×7 multi-agent crypto trading stack built on Temporal and Model Context Protocol (MCP) with integrated LLM as Judge performance optimization. Every `@mcp.tool()` is backed by a deterministic Temporal workflow, providing exactly-once execution, automatic retries and full replay for audit and compliance.

## ✨ Key Features

- **🤖 Multi-Agent Architecture**: Broker, execution, and judge agents working in coordination
- **📊 LLM as Judge System**: Autonomous performance evaluation and prompt optimization
- **🔄 Dynamic Prompt Management**: Adaptive system prompts based on trading performance
- **📈 Comprehensive Analytics**: Real-time performance metrics, risk analysis, and transaction history
- **🛡️ Risk Management**: Intelligent position sizing, drawdown protection, and portfolio monitoring
- **⚡ Durable Execution**: Built on Temporal workflows for fault tolerance and auditability

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

The system consists of three main agents working together:

### 🏆 Single Interface Design
The **Broker Agent** serves as the sole user interface, providing access to all system functionality including trading, performance analysis, and evaluation triggering.

```
                 ┌─────┐
                 │User │
                 └─┬───┘
                   │ commands & queries
                   ▼
           ┌──────────────────┐
           │  Broker Agent    │◄─── Single User Interface
           │  - Trading       │     • Market data & portfolio
           │  - Analytics     │     • Performance evaluation
           │  - Evaluation    │     • Transaction history
           └──────┬───────────┘     • Risk metrics
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
           ┌──────────────────┐     ┌──────────────────┐
           │ Execution Agent  │◄────┤  Judge Agent     │
           │ - Dynamic Prompts│     │ - LLM as Judge   │
           │ - Risk Management│     │ - Performance    │
           │ - Order Execution│     │   Evaluation     │
           └──────┬───────────┘     │ - Prompt Updates │
                  │ orders          └─────────┬────────┘
                  ▼                           │ evaluations
           ┌──────────────────┐               │
           │ Mock Order WF    │               │
           └──────┬───────────┘               │
                  │ fills                     │
                  ▼                           ▼
           ┌──────────────────┐     ┌──────────────────┐
           │ Execution Ledger │────►│ Performance      │
           │ - Transactions   │     │ Analytics        │
           │ - P&L Tracking   │     │ - Sharpe Ratio   │
           │ - Risk Metrics   │     │ - Drawdown       │
           └──────────────────┘     │ - Decision Qual. │
                  ▲                 └──────────────────┘
                  │ nudges
                  │
           ┌──────────────────┐
           │ Scheduled Nudges │
           └──────────────────┘
```

### 🔄 LLM as Judge Workflow
The judge agent continuously monitors performance and automatically optimizes the execution agent:

1. **Performance Monitoring**: Evaluates trading performance every 30 minutes
2. **Decision Analysis**: Uses GPT-4o to analyze decision quality and timing
3. **Prompt Optimization**: Automatically updates execution agent prompts based on performance
4. **Risk Management**: Switches to conservative mode during poor performance periods

Each block corresponds to one or more MCP tools (Temporal workflows) described below.

## Durable Tools Catalog

### Core Trading Tools

| Tool (Workflow)            | Purpose                                                | Typical Triggers        |
|----------------------------|--------------------------------------------------------|-------------------------|
| `subscribe_cex_stream`   | Fan-in ticker data from centralized exchanges  | Startup, reconnect    |
| `start_market_stream`    | Begin streaming market data for selected pairs | Auto-started by broker after pair selection |
| `ComputeFeatureVector`   | Store tick history for queries                | Market tick           |
| `PlaceMockOrder`         | Simulate order execution and return a fill     | Portfolio rebalance   |
| `ExecutionLedgerWorkflow`| Track fills, positions, and transaction history | Fill events           |

### Performance & Analytics Tools

| Tool (Workflow)            | Purpose                                                | Typical Triggers        |
|----------------------------|--------------------------------------------------------|-------------------------|
| `get_transaction_history`  | Retrieve filtered transaction history               | User queries, evaluations |
| `get_performance_metrics`  | Calculate returns, Sharpe ratio, win rates         | Performance analysis    |
| `get_risk_metrics`         | Analyze position concentration and leverage         | Risk monitoring         |
| `get_portfolio_status`     | Current cash, positions, and P&L                   | User queries            |

### Judge Agent Tools

| Tool (Workflow)            | Purpose                                                | Typical Triggers        |
|----------------------------|--------------------------------------------------------|-------------------------|
| `trigger_performance_evaluation` | Force immediate performance evaluation        | User request, poor performance |
| `get_judge_evaluations`    | Retrieve recent performance evaluations            | User queries            |
| `get_prompt_history`       | View prompt evolution and version history           | System monitoring       |
| `JudgeAgentWorkflow`       | Manage evaluation state and prompt versions        | Judge agent lifecycle   |


## Getting Started

### Prerequisites

| Requirement  | Version      | Notes                                        |
|--------------|--------------|----------------------------------------------|
| Python       | 3.11 or newer| Data & strategy agents                       |
| Temporal CLI | 1.24+        | `brew install temporal` or use Temporal Cloud|
| tmux         | latest       | Required for `run_stack.sh` start script     |

Required environment variables:

- `OPENAI_API_KEY` – enables the broker and execution agents to use OpenAI models.
- `COINBASEEXCHANGE_API_KEY` and `COINBASEEXCHANGE_SECRET` – API credentials for Coinbase Exchange.
- `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE` and `TASK_QUEUE` – Temporal connection settings (defaults are shown in `.env`).
- `MCP_PORT` – port for the MCP server (defaults to `8080`).

### Quick Setup (local dev)
```bash
# Clone and bootstrap
git clone https://github.com/your-org/durable-crypto-agents.git
cd durable-crypto-agents

# Activate Python env
python -m venv .venv && source .venv/bin/activate
pip install uv
uv sync

# Launch the full stack
./run_stack.sh
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
4. The execution agent wakes up periodically via a scheduled workflow and
   analyzes market data to decide whether to trade using `place_mock_order`.
5. Filled orders are recorded in the `ExecutionLedgerWorkflow`.
6. The judge agent monitors performance autonomously and can be queried through the broker:
   - **"How is the system performing?"** - Triggers evaluation and shows metrics
   - **"What's the transaction history?"** - Shows recent trades and fills
   - **"Evaluate performance"** - Forces immediate performance analysis

### 🤖 Interacting with the System
The broker agent serves as your single interface. Try these commands:

**Trading Commands:**
- `"Start trading BTC/USD and ETH/USD"`
- `"What's my portfolio status?"`
- `"Show me recent transactions"`

**Performance Analysis:**
- `"How is the system performing?"`
- `"Trigger a performance evaluation"`
- `"Show me the latest evaluation results"`
- `"What are the current risk metrics?"`

**System Insights:**
- `"Show prompt evolution history"`
- `"What performance trends do you see?"`


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
├── agents/                    # Multi-agent system components
│   ├── broker_agent_client.py    # Single user interface agent
│   ├── execution_agent_client.py # Trading decision agent
│   ├── judge_agent_client.py     # LLM as Judge performance optimizer
│   ├── workflows.py               # Temporal workflow definitions
│   ├── context_manager.py         # Intelligent conversation management
│   └── prompt_manager.py          # Dynamic prompt system
├── tools/                     # Durable workflows used as MCP tools
│   ├── performance_analysis.py   # Performance metrics and analysis
│   ├── market_data.py            # Market data streaming
│   ├── execution.py              # Order execution
│   └── ...
├── mcp_server/               # FastAPI server exposing the tools
├── worker/                   # Temporal worker loading workflows
├── tests/                    # Unit tests for tools and agents
├── run_stack.sh             # tmux helper to launch local stack
└── ticker_ui_service.py     # Simple websocket ticker UI
```

### Key Components

- **`broker_agent_client.py`**: Main user interface providing access to all system functionality
- **`execution_agent_client.py`**: Autonomous trading agent with dynamic prompt loading
- **`judge_agent_client.py`**: Performance evaluator using LLM as Judge pattern
- **`workflows.py`**: Temporal workflows for state management and coordination
- **`context_manager.py`**: Token-based conversation management with summarization
- **`prompt_manager.py`**: Modular prompt templates with versioning and A/B testing
- **`performance_analysis.py`**: Comprehensive trading performance analysis tools

## 🧠 LLM as Judge System

The system implements a sophisticated "LLM as Judge" pattern for continuous self-improvement:

### Performance Evaluation Framework
- **Multi-dimensional Analysis**: Evaluates returns, risk management, decision quality, and consistency
- **Automated Scoring**: Combines quantitative metrics with LLM-based decision analysis
- **Trend Detection**: Identifies performance patterns and degradation early

### Dynamic Prompt Optimization
- **Template System**: Modular prompt components for different trading scenarios
- **Automatic Updates**: Switches between conservative, standard, and aggressive modes based on performance
- **Version Tracking**: Full history of prompt changes with performance attribution

### Intelligent Triggers
- **Poor Performance**: Automatic intervention when scores drop below thresholds
- **High Drawdown**: Emergency conservative mode activation
- **Overly Conservative**: Increased risk-taking when performance is too safe

### Example Evaluation Cycle
```python
# Triggered every 30 minutes or on-demand via broker agent
1. Collect transaction history and portfolio metrics
2. Calculate quantitative performance (Sharpe, drawdown, win rate)
3. Use GPT-4o to analyze decision quality and timing
4. Generate weighted overall score across four dimensions
5. Determine if prompt updates are needed
6. Implement changes and track effectiveness
```

## Contributing
Pull requests are welcome! Please open an issue first to discuss your proposed change. Make sure to:

- Run `make lint test` and fix any CI failures.
- Keep new tools deterministic (no nondeterministic I/O inside workflows).
- Write docs – every public agent or tool needs at least minimal usage notes.

## License

This project is released under the MIT License – see `LICENSE` for details.
