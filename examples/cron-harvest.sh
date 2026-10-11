#!/usr/bin/env bash
# Hourly: pull dispatched task results out of the coding agent's session logs into Hindsight.
# Pure script — no LLM, no token cost. Silent when there is nothing new.
set -euo pipefail

# See cron-sync.sh for why PATH is set explicitly.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
command -v zstd >/dev/null 2>&1 || { echo "agent-bridge harvest: zstd not found (PATH problem), skipped"; exit 1; }

# Offline window guard: if this machine has no network between 01:00 and 07:00, harvesting would
# write to local Hindsight fine but the fact extraction behind it would fail on every item. Exit
# quietly and collect the backlog after the window. Set the window to match your own schedule
# (OFFLINE_FROM/OFFLINE_TO are HHMM, single hour prefixes) or delete this block.
OFFLINE_FROM="${OFFLINE_FROM:-01}"
OFFLINE_TO="${OFFLINE_TO:-07}"
NOW_HHMM="${AGENT_BRIDGE_NOW:-$(date +%H%M)}"          # AGENT_BRIDGE_NOW is for testing only
HOUR=$((10#${NOW_HHMM:0:2}))
if [ "$HOUR" -ge "$((10#$OFFLINE_FROM))" ] && [ "$HOUR" -lt "$((10#$OFFLINE_TO))" ]; then
  printf '%s offline window (%s), skipping harvest, backlog will be collected later\n' \
    "$(date '+%F %T')" "$NOW_HHMM" >> "$HOME/.agent-bridge/bridge.log"
  exit 0
fi

BRIDGE="${AGENT_BRIDGE_PY:-$HOME/agent-bridge/agent_bridge.py}"
OUT=$(BRIDGE_VERBOSE= python3 "$BRIDGE" harvest 2>&1) || {
  echo "agent_bridge harvest failed:"
  echo "$OUT"
  exit 1
}
case "$OUT" in
  *"没有新的"*|*"nothing new"*) exit 0 ;;   # nothing new -> silent
  *) echo "$OUT" ;;                        # wrote something -> one summary line
esac
