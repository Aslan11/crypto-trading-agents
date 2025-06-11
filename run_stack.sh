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
# │ worker/main.py│ feature_engineering_agent.py │
# ├───────────────┼────────────────────────┤
# │ Pane 5        │ Pane 4                 │
# │ (shell)       │ momentum_agent.py      │
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
tmux send-keys    -t $MCP_PANE 'source .venv/bin/activate && python mcp_server/app.py' C-m

# 4. Pane 3 – feature engineering agent (split Pane 1 horizontally →)
tmux select-pane  -t $WORKER_PANE
FE_PANE=$(tmux split-window -h -P -F "#{pane_id}")
tmux send-keys    -t $FE_PANE 'sleep 2 && source .venv/bin/activate && python agents/feature_engineering_agent.py' C-m

# 5. Pane 4 – momentum strategy agent (split Pane 3 vertically ↓)
tmux select-pane  -t $FE_PANE
MOM_PANE=$(tmux split-window -v -P -F "#{pane_id}")
tmux send-keys    -t $MOM_PANE 'sleep 2 && source .venv/bin/activate && python agents/strategies/momentum_agent.py' C-m

# 6. Pane 5 – blank shell (split Pane 4 horizontally ←)
tmux select-pane  -t $MOM_PANE
MAIN_PANE=$(tmux split-window -h -P -F "#{pane_id}")
tmux send-keys    -t $MAIN_PANE

# 7. Attach user to session
tmux select-pane -t $SESSION:0.0    # focus top-left pane
exec tmux attach -t $SESSION
