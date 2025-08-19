# LLM as Judge Agent System

This system implements an "LLM as Judge" pattern that evaluates execution agent performance and dynamically updates system prompts to improve trading performance.

## Architecture Overview

### Core Components

1. **JudgeAgentWorkflow** (`agents/workflows.py`)
   - Maintains evaluation history and prompt versions
   - Tracks performance trends and change effectiveness
   - Provides queries for system monitoring

2. **JudgeAgent** (`agents/judge_agent_client.py`) 
   - Runs periodic performance evaluations
   - Analyzes trading decisions using LLM
   - Implements prompt updates based on performance


3. **PerformanceAnalyzer** (`tools/performance_analysis.py`)
   - Calculates comprehensive trading metrics
   - Generates performance reports and grades
   - Analyzes risk and consistency patterns

## Usage

### Running the Judge Agent

```bash
# Run the judge agent (evaluates every 4 hours by default)
python agents/judge_agent_client.py

# Set verbose mode for detailed reports
JUDGE_VERBOSE=true python agents/judge_agent_client.py
```

### MCP Tools for Evaluation

The system exposes several MCP tools for manual evaluation and monitoring:

```python
# Trigger manual evaluation
await mcp_session.call_tool("trigger_performance_evaluation", {
    "window_days": 7,
    "force": True  # Override cooldown
})

# Get recent evaluations
evaluations = await mcp_session.call_tool("get_judge_evaluations", {
    "limit": 10
})

# Get transaction history
transactions = await mcp_session.call_tool("get_transaction_history", {
    "since_ts": 1234567890,
    "limit": 100
})

# Get performance metrics
metrics = await mcp_session.call_tool("get_performance_metrics", {
    "window_days": 30
})

# Get prompt version history
history = await mcp_session.call_tool("get_prompt_history", {
    "limit": 5
})
```

### Environment Variables

```bash
# Judge agent settings
JUDGE_VERBOSE=true                    # Enable detailed output
JUDGE_WF_ID=judge-agent              # Judge workflow ID
LEDGER_WF_ID=mock-ledger             # Ledger workflow ID

# Evaluation timing
JUDGE_COOLDOWN_HOURS=4               # Hours between evaluations
EVALUATION_WINDOW_DAYS=7             # Days to analyze per evaluation

# Model settings
OPENAI_MODEL=gpt-4o                  # Model for analysis
OPENAI_API_KEY=your_api_key         # OpenAI API key

# Temporal settings
TEMPORAL_ADDRESS=localhost:7233      # Temporal server
TEMPORAL_NAMESPACE=default           # Temporal namespace
TASK_QUEUE=mcp-tools                # Task queue name
```

## Evaluation Criteria

The judge agent evaluates performance across four dimensions:

### 1. Returns (30% weight)
- Total and annualized returns
- Risk-adjusted performance
- Comparison to benchmarks

### 2. Risk Management (25% weight)
- Maximum drawdown control
- Position concentration limits
- Cash management
- Safety rule adherence

### 3. Decision Quality (25% weight)
- Trade timing and rationale
- Position sizing consistency
- Market analysis accuracy
- Rule following

### 4. Consistency (20% weight)
- Return volatility
- Decision pattern stability
- Error frequency

## Prompt Update Triggers

The system automatically updates prompts based on:

### Emergency Conservative Mode
- **Trigger**: Overall score < 40 or high drawdown
- **Action**: Switch to conservative risk parameters
- **Changes**: Reduced position sizes, increased cash reserves

### Risk Reduction Mode  
- **Trigger**: Max drawdown > 20%
- **Action**: Enhanced risk management controls
- **Changes**: Stricter position limits, better safety checks

### Decision Improvement Mode
- **Trigger**: Decision quality score < 40
- **Action**: Enhanced decision framework
- **Changes**: Better analysis requirements, performance monitoring

### Increased Aggressiveness Mode
- **Trigger**: Very low drawdown + poor returns + good overall score
- **Action**: More aggressive parameters
- **Changes**: Higher position limits, reduced cash requirements

## Monitoring and Safety

### Performance Monitoring
- Continuous tracking of prompt effectiveness
- A/B testing capabilities
- Automatic rollback on poor performance

### Safety Mechanisms
- Minimum performance thresholds before updates
- Change frequency limits
- Human oversight capabilities
- Complete audit trail

### Logging and Alerts
- Detailed evaluation logs
- Performance trend alerts
- Prompt change notifications
- Error tracking and reporting

## File Structure

```
agents/
├── judge_agent_client.py          # Main judge agent implementation
├── workflows.py                   # JudgeAgentWorkflow + enhancements
└── context_manager.py             # Context management (existing)

tools/
└── performance_analysis.py        # Performance calculation and analysis

mcp_server/
└── app.py                        # Enhanced with judge tools

tests/
└── test_judge_agent.py           # Comprehensive test suite
```

## Example Evaluation Output

```
[JudgeAgent] Starting evaluation cycle
[JudgeAgent] Collecting performance data...
[JudgeAgent] Generating evaluation report...
[JudgeAgent] Evaluation completed
[JudgeAgent] Overall Score: 72.3/100
[JudgeAgent] Component Scores:
[JudgeAgent]   returns: 75.2
[JudgeAgent]   risk_management: 68.1
[JudgeAgent]   decision_quality: 74.5
[JudgeAgent]   consistency: 71.8
[JudgeAgent] Prompt update recommended: risk_reduction
[JudgeAgent] Implemented prompt update: Risk Reduction Update
[JudgeAgent] Reason: High drawdown (18.5%) requires enhanced risk management
[JudgeAgent] - Enhanced drawdown protection
[JudgeAgent] - More conservative position sizing
[JudgeAgent] - Stricter risk management rules
```

This system creates a self-improving execution agent that adapts its strategy based on performance feedback while maintaining comprehensive safety and oversight mechanisms.