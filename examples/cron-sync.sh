#!/usr/bin/env bash
# Render Hindsight -> $DSH_HOME/AGENTS.md (the file the coding agent reads at session start).
# Cron-friendly: silent on success, prints only on failure.
set -euo pipefail

# A cron PATH is minimal (/usr/bin:/bin on macOS) while the tools you need usually live in
# /opt/homebrew/bin. Export it explicitly — otherwise nothing but python3 is found.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

BRIDGE="${AGENT_BRIDGE_PY:-$HOME/agent-bridge/agent_bridge.py}"

if ! OUT=$(python3 "$BRIDGE" sync 2>&1); then
  echo "agent_bridge sync failed:"
  echo "$OUT"
  exit 1
fi
exit 0
