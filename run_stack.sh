#!/usr/bin/env bash
#
# run_stack.sh  –  spin up local Temporal + worker + MCP server in tmux
#
# Usage:
#   ./run_stack.sh                              # launches with default $1000 balance
#   ./run_stack.sh --initial-balance 250000    # launches with $250,000 balance
#
# Prereqs:
#   • tmux installed
#   • Python venv already created at .venv/ with all deps installed
#   • temporal CLI on PATH  (brew install temporal)

SESSION="crypto"
INITIAL_BALANCE="1000"  # Default balance

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --initial-balance)
      INITIAL_BALANCE="$2"
      shift 2
      ;;
    *)
      echo "Unknown option $1"
      echo "Usage: $0 [--initial-balance AMOUNT]"
      exit 1
      ;;
  esac
done

# If the session already exists, just attach
tmux has-session -t $SESSION 2>/dev/null
if [ $? -eq 0 ]; then
  echo "Session '$SESSION' already running. Attaching…"
  exec tmux attach -t $SESSION
fi

# Clear all log files for a fresh start (tabula rasa)
echo "Clearing log files for fresh start..."
if [ -d "logs" ]; then
  rm -f logs/*.log logs/*.jsonl
  echo "Cleared all log files in logs/ directory"
else
  echo "No logs directory found (will be created by agents)"
fi

# Export the initial balance as environment variable for all agents
export INITIAL_PORTFOLIO_BALANCE="$INITIAL_BALANCE"
echo "Setting initial portfolio balance to: \$${INITIAL_BALANCE}"

###############################################################################
# Pane layout
# ┌───────────────┬────────────────────────┐
# │ Pane 0        │ Pane 2                 │
# │ temporal dev  │ mcp_server/app.py      │
# ├───────────────┼────────────────────────┤
# │ Pane 1        │ Pane 3                 │
# │ worker/main.py│ broker_agent_client.py │
# ├───────────────┼────────────────────────┤
# │ Pane 4        │ Pane 5                 │
# │ execution_agent_client.py │ judge_agent_client.py  │
# ├───────────────┼────────────────────────┤
# │ Pane 6        │                        │
# │ ticker_ui_service.py │                 │
# └────────────────────────────────────────┘
###############################################################################

# 0. Create new detached session
tmux new-session  -d  -s $SESSION -n main

# 1. Pane 0 – Temporal dev server
tmux send-keys    -t $SESSION:0.0 'temporal server start-dev' C-m

# 2. Pane 1 – worker.py (split vertically ↓)
WORKER_PANE=$(tmux split-window -t $SESSION:0.0 -v -P -F "#{pane_id}")
tmux send-keys    -t $WORKER_PANE 'source .venv/bin/activate && python worker/main.py' C-m

# 3. Pane 2 – MCP server (split Pane 0 horizontally →)
tmux select-pane  -t $SESSION:0.0
MCP_PANE=$(tmux split-window -h -P -F "#{pane_id}")
tmux send-keys    -t $MCP_PANE 'source .venv/bin/activate && PYTHONPATH="$PWD" python mcp_server/app.py' C-m

# 4. Pane 3 – broker agent (split Pane 1 horizontally →)
tmux select-pane  -t $WORKER_PANE
BROKER_PANE=$(tmux split-window -h -P -F "#{pane_id}")
tmux send-keys    -t $BROKER_PANE 'sleep 3 && source .venv/bin/activate && PYTHONPATH="$PWD" python agents/broker_agent_client.py' C-m

# 5. Pane 4 – execution agent (split Pane 3 vertically ↓)
tmux select-pane  -t $BROKER_PANE
EXEC_PANE=$(tmux split-window -v -P -F "#{pane_id}")
tmux send-keys    -t $EXEC_PANE 'sleep 5 && source .venv/bin/activate && PYTHONPATH="$PWD" python agents/execution_agent_client.py' C-m

# 6. Pane 5 – judge agent (split Pane 4 horizontally →)
tmux select-pane  -t $EXEC_PANE
JUDGE_PANE=$(tmux split-window -h -P -F "#{pane_id}")
tmux send-keys    -t $JUDGE_PANE 'sleep 4 && source .venv/bin/activate && PYTHONPATH="$PWD" python agents/judge_agent_client.py' C-m

# 7. Pane 6 – ticker UI (split Pane 5 vertically ↓)
tmux select-pane  -t $JUDGE_PANE
UI_PANE=$(tmux split-window -v -P -F "#{pane_id}")
tmux send-keys    -t $UI_PANE 'sleep 3 && source .venv/bin/activate && PYTHONPATH="$PWD" python ticker_ui_service.py' C-m

# 10. Arrange all panes into a tiled layout for equal sizing
tmux select-layout -t $SESSION:0 tiled

# 11. Attach user to session
tmux select-pane -t $SESSION:0.0    # focus top-left pane
exec tmux attach -t $SESSION
