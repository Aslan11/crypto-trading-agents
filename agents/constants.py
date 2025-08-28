"""Shared constants for all agents."""

# ANSI Color Codes
ORANGE = "\033[33m"
PINK = "\033[95m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
WHITE = "\033[37m"
RESET = "\033[0m"

# Exchange Configuration
EXCHANGE = "coinbaseexchange"

# Environment Variable Defaults
DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"
DEFAULT_TEMPORAL_NAMESPACE = "default"
DEFAULT_TASK_QUEUE = "mcp-tools"
DEFAULT_LOG_LEVEL = "WARNING"
DEFAULT_OPENAI_MODEL = "gpt-4o"

# Workflow IDs
BROKER_WF_ID = "broker-agent"
EXECUTION_WF_ID = "execution-agent"
JUDGE_WF_ID = "judge-agent"
LEDGER_WF_ID = "mock-ledger"

# Nudge Schedule
NUDGE_SCHEDULE_ID = "ensemble-nudge"

# Agent Names (for logging)
BROKER_AGENT = "BrokerAgent"
EXECUTION_AGENT = "ExecutionAgent"
JUDGE_AGENT = "JudgeAgent"