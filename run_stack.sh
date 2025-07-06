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
# │ temporal dev  │ mcp_server/app.py      │
# ├───────────────┼────────────────────────┤
# │ Pane 1        │ Pane 3                 │
# │ worker/main.py│ broker_agent_client.py │
# ├───────────────┼────────────────────────┤
# │ Pane 4        │ Pane 5                 │
# │ ensemble_agent_client.py │ ticker_ui_service.py    │
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
tmux send-keys    -t $BROKER_PANE 'sleep 2 && source .venv/bin/activate && PYTHONPATH="$PWD" python agents/broker_agent_client.py' C-m
# 5. Pane 4 – ensemble agent (split Pane 3 vertically ↓)
tmux select-pane  -t $BROKER_PANE
ENS_PANE=$(tmux split-window -v -P -F "#{pane_id}")
tmux send-keys    -t $ENS_PANE 'sleep 2 && source .venv/bin/activate && PYTHONPATH="$PWD" python agents/ensemble_agent_client.py' C-m
# 6. Pane 5 – ticker UI (split Pane 4 horizontally →)
tmux select-pane  -t $ENS_PANE
UI_PANE=$(tmux split-window -h -P -F "#{pane_id}")
tmux send-keys    -t $UI_PANE 'sleep 2 && source .venv/bin/activate && PYTHONPATH="$PWD" python ticker_ui_service.py' C-m

# 10. Arrange all panes into a tiled layout for equal sizing
tmux select-layout -t $SESSION:0 tiled

# 11. Attach user to session
tmux select-pane -t $SESSION:0.0    # focus top-left pane
exec tmux attach -t $SESSION
