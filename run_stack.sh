#!/usr/bin/env bash
#
# run_stack.sh  –  spin up local Temporal + worker + MCP server in tmux
#
# Usage:
#   ./run_stack.sh           # launches or attaches to the "crypto" session
#
# Prereqs:
#   • tmux installed
#   • Python venv already created at .venv/ with all deps installed
#   • temporal CLI on PATH  (brew install temporal)

SESSION="crypto"

# If the session already exists, just attach
tmux has-session -t $SESSION 2>/dev/null
if [ $? -eq 0 ]; then
  echo "Session '$SESSION' already running. Attaching…"
  exec tmux attach -t $SESSION
fi

###############################################################################
# Pane layout
# ┌───────────────┬────────────────────────┐
# │ Pane 0        │ Pane 2                 │
# │ temporal dev  │ MCP server             │
# ├───────────────┼────────────────────────┤
# │ Pane 1        │ Pane 3                 │
# │ worker.py     │ curl smoke-test        │
# └───────────────┴────────────────────────┘
###############################################################################

# 0. Create new detached session
tmux new-session  -d  -s $SESSION -n main

# 1. Pane 0 – Temporal dev server
tmux send-keys    -t $SESSION:0.0 'temporal server start-dev' C-m

# 2. Pane 1 – worker.py (split vertically ↓)
tmux split-window -t $SESSION:0.0 -v
tmux send-keys    -t $SESSION:0.1 'source .venv/bin/activate && python worker.py' C-m

# 3. Pane 2 – MCP server (split Pane 0 horizontally →)
tmux select-pane  -t $SESSION:0.0
tmux split-window -h
tmux send-keys    -t $SESSION:0.2 'source .venv/bin/activate && python -m mcp_server.app' C-m

# 4. Pane 3 – smoke-test curl (split Pane 1 horizontally →)
tmux select-pane  -t $SESSION:0.1
tmux split-window -h
tmux send-keys    -t $SESSION:0.3 '
sleep 8  # wait for services
echo -e "\n=== MCP health ==="
curl -s http://localhost:8080/healthz
echo -e "\n=== Launch test workflow ==="
curl -s -X POST http://localhost:8080/tools/SubscribeCEXStream \
  -H "Content-Type: application/json" \
  -d '"'"'{"exchange":"binance","symbols":["BTC/USDT"],"interval_sec":5,"max_cycles":2}'"'"' | jq
' C-m

# 5. Attach user to session
tmux select-pane -t $SESSION:0.0    # focus top-left pane
exec tmux attach -t $SESSION
