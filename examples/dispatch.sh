#!/usr/bin/env bash
# Dispatch a coding task so that `harvest` can pick the result up later.
#
# Why: harvest only ingests sessions whose first message starts with the tag ([hermes] by default),
# which keeps unrelated chat out of memory. Typing the tag by hand is easy to forget — route every
# dispatch through this wrapper instead.
#
#   ./dispatch.sh "fix the failing test in src/foo.py"          # foreground, headless
#   DSH_BG=1 ./dispatch.sh "long job ..."                       # background, output to a log
#   HERMES_DSH_ACP_CWD=~/proj ./dispatch.sh "..."               # set the session working directory
#
# Exit code: 0 = the coding agent reported completion, 1 = not completed, other = startup error.
set -euo pipefail

export PATH="$HOME/.npm-global/bin:$PATH"      # wherever pnpm/npm put dsh
TAG="${DSH_TAG:-[hermes]}"

if [ "$#" -eq 0 ]; then
  echo "usage: $(basename "$0") \"<task>\"   (add DSH_BG=1 to run in the background)" >&2
  exit 64
fi

PROMPT="$TAG $*"
WORKDIR="${HERMES_DSH_ACP_CWD:-$PWD}"

if [ -n "${DSH_BG:-}" ]; then
  LOG_DIR="$HOME/.agent-bridge"
  mkdir -p "$LOG_DIR"
  LOG="$LOG_DIR/dispatch-$(date +%Y%m%d-%H%M%S).log"
  ( cd "$WORKDIR" && dsh --profile headless "$PROMPT" ) >"$LOG" 2>&1 &
  echo "dispatched in background, pid=$!, log: $LOG"
else
  cd "$WORKDIR"
  dsh --profile headless "$PROMPT"
fi
