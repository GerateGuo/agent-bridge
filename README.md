# agent-bridge

**English** · [简体中文](README.zh-CN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)

A **single-file, zero-dependency** bridge that lets your main agent and your coding agent share one
set of facts — with [Hindsight](https://github.com/vectorize-io/hindsight) as the single source of
truth and no second memory store to keep in sync.

It was built for **Hermes Agent ⇄ [dsh](https://github.com/deepseek-ai/deepseek-harness)
(DeepSeek Harness)**, but the mechanism is generic: it reads one agent's session logs and writes a
plain-Markdown fact file that the other agent reads by itself.

```
                 harvest  (hourly)
dsh session logs ─────────────────────► Hindsight   (memory API, single source of truth)
~/.dsh/sessions/*/session.v3.jsonl.zstd      │
                                             │  sync  (daily)
              $DSH_HOME/AGENTS.md  ◄─────────┘
                      │
                      └─► injected into the FIRST request of every new dsh session
                          (built-in @deepseek-ai/dsh-agent-instructions — no plugin, no profile change)
```

## The problem

Two agents, one machine: your main agent (Hermes) dispatches work, the coding agent (dsh) executes
it. Both need the same context — your conventions, machine paths, tool configuration, recent task
results. Without a bridge you either

* restate that context in every dispatch brief, or
* let the coding agent re-explore the machine from scratch every session, or
* "just sync the memory" and end up with a **second** memory store that silently drifts.

## What it does

| Command | Direction | What it is for |
|---|---|---|
| `harvest` | dsh → Hindsight | Deterministic extraction from the coding agent's own session logs: final answer + token usage + cwd + timestamp. No LLM is called inside the script, and re-running it is idempotent (a per-session cursor in `state.json`). |
| `sync` | Hindsight → dsh | Renders a compact Markdown snapshot to `$DSH_HOME/AGENTS.md`, which the coding agent reads at the start of every session. Atomic write, previous versions kept as `AGENTS.md.bak.<ts>` (3 by default). |
| `show` | Hindsight → terminal | Prints the most recent harvested task results, a few lines each — on demand, so you never have to pull the coding agent's logs into your main agent's context. |

`--dry-run` works before or after any subcommand and writes nothing (no POST, no file, no cursor
movement).

## Why it works (the one non-obvious fact)

dsh ships `@deepseek-ai/dsh-agent-instructions`, enabled by default in every profile. On a
session's **first request** it injects the user-level `$DSH_HOME/AGENTS.md`, then walks the
project chain (`AGENTS.md` / `CLAUDE.md`, `.git` marks the project root, 65536 bytes budget —
wider files are dropped first).

So pushing shared facts into the coding agent needs **zero plugins and zero profile changes** — you
just write that file. Verify it actually landed:

```bash
zstd -dc ~/.dsh/sessions/*/*/session.v3.jsonl.zstd | grep -c 'dsh-bridge:auto-generated'   # expect ≥ 1
```

## Requirements

* Python 3.9+ — standard library only, no `pip install`, no build step
* [Hindsight](https://github.com/vectorize-io/hindsight) reachable on loopback (default `http://127.0.0.1:8988`)
* dsh with session logs under `$DSH_HOME/sessions`
* `zstd` CLI (`harvest` decompresses the session logs with it)

## Quick start

```bash
git clone https://github.com/GerateGuo/agent-bridge.git ~/agent-bridge
cd ~/agent-bridge

mkdir -p ~/.agent-bridge
cp config.example.json ~/.agent-bridge/config.json
$EDITOR ~/.agent-bridge/config.json          # at least: "bank" = your Hindsight bank id

python3 agent_bridge.py harvest --dry-run    # show what would be written back
python3 agent_bridge.py sync                 # write the snapshot the coding agent reads
python3 agent_bridge.py show --limit 10      # recent harvested task results
```

## Configuration

Priority: **environment variable > `~/.agent-bridge/config.json` > built-in default**.

| `config.json` key | Environment variable | Default | Meaning |
|---|---|---|---|
| `bank` | `AGENT_BRIDGE_BANK` | `default` | Hindsight bank id (may be non-ASCII; it is percent-encoded) |
| `hs_base` | `AGENT_BRIDGE_HS_BASE` | `http://127.0.0.1:8988` | Hindsight data-plane base URL |
| `dsh_home` | `AGENT_BRIDGE_DSH_HOME` | `~/.dsh` | dsh home (`$DSH_HOME`) |
| `sessions` | `AGENT_BRIDGE_SESSIONS` | `$DSH_HOME/sessions` | Where dsh stores session logs |
| `agents_file` | `AGENT_BRIDGE_AGENTS_FILE` | `$DSH_HOME/AGENTS.md` | Snapshot target |
| `state_dir` | `AGENT_BRIDGE_STATE_DIR` | `~/.agent-bridge` | Cursor (`state.json`) and log (`bridge.log`) |
| `tag` | `AGENT_BRIDGE_TAG` | `[hermes]` | Marker that identifies *your* dispatched sessions |
| `topics` | — (`config.json` only) | three built-in topics | `[["section title", "recall query"], …]` — what `sync` asks Hindsight for |
| `session_note` | `AGENT_BRIDGE_SESSION_NOTE` | English chain-of-thought, Chinese answer | Fixed note injected at the top of every snapshot |
| `task_context` | `AGENT_BRIDGE_TASK_CONTEXT` | `dsh 执行上游 agent 派发的任务后的结论` | `context` field attached to harvested memories |

`AGENT_BRIDGE_CONFIG` moves the config file itself. Paths may use `~`.

## Dispatch discipline (why the tag matters)

`harvest` only ingests sessions whose first user message **starts with the tag**
(`[hermes]` by default) — so idle chat in a coding agent's web UI and unrelated sessions never end
up in your memory. Route dispatches through a wrapper so you cannot forget it:

```bash
# examples/dispatch.sh
export PATH="$HOME/.npm-global/bin:$PATH"
TASK="$*"; TAG="${DSH_TAG:-[hermes]}"
cd "${HERMES_DSH_ACP_CWD:-$PWD}" && dsh --profile headless "$TAG $TASK"
```

When Hermes drives dsh through the `dsh-acp` provider the tag is prepended automatically
(`HERMES_DSH_ACP_TAG`), so those sessions are harvested without any wrapper.

## Scheduling

Two cron jobs are enough (see `examples/`):

| Job | Frequency | Script |
|---|---|---|
| `agent-bridge-sync` | daily, e.g. 08:00 | `examples/cron-sync.sh` |
| `agent-bridge-harvest` | hourly | `examples/cron-harvest.sh` |

Both are plain scripts — no LLM, no token cost. Two pitfalls are already handled in the examples:

* A cron `PATH` is minimal (`/usr/bin:/bin` on macOS), while `zstd` usually lives in
  `/opt/homebrew/bin` → `harvest` dies with `FileNotFoundError: 'zstd'`. Export `PATH` explicitly.
* If your machine is offline overnight, a write-back still reaches local Hindsight but the fact
  extraction behind it needs the network and fails. The example harvest script exits quietly inside
  a configurable offline window and collects the backlog afterwards.

## Design constraints

1. **No second memory store.** The program holds no knowledge; `AGENTS.md` is a derivative you can
   delete and rebuild. The only local state is a cursor (`state.json`).
2. **No resident injection.** Nothing is pushed into the main agent's context every turn; the coding
   agent reads the file itself, once per session.
3. **No LLM inside the script.** Extraction is deterministic parsing, not summarisation.
4. **No secrets.** The snapshot holds facts and conventions only — never tokens, keys or persona
   (`SOUL`) content.
5. **Append-only towards Hindsight.** Memories are written, never deleted. Retiring a stale fact is a
   deliberate, reversible `invalidate` you run yourself.
6. **Concurrency-safe reads.** Session files still being written are skipped (only files untouched
   for 60 seconds are processed).

## Verified

macOS, dsh `0.1.5-rc.1` (2026-09-15):

| # | Check | Result |
|---|---|---|
| 1 | `--dry-run` before *and* after the subcommand | ✅ writes nothing (`AGENTS.md` mtime unchanged) |
| 2 | Write-back readable through the API | ✅ memory with `session:` / `cwd:` tags |
| 3 | `sync` really reaches the coding agent | ✅ `dsh-bridge:auto-generated` appears once in a fresh session log |
| 4 | Idempotent | ✅ `harvest` twice → `dsh-task` count unchanged |
| 5 | `show --limit 5` | ✅ compact one-block-per-task output |

## Cost note

The snapshot enters the **first request of every session**, so keep it lean; `sync` reports the
final character count. As a reference, a 1.5k-character snapshot is ~0.5k tokens on top of dsh's own
~7.1k-token floor (harness prompt + tool schemas), and because the prefix is stable it is mostly
billed at the cheap prefix-cache rate. Knobs: `--max-chars`, `--per-section`, `--fact-limit`,
`--recent-rows`.

## Related

* [hindsight-dashboard](https://github.com/GerateGuo/hindsight-dashboard) — single-file dashboard for
  self-hosted Hindsight; its optional **dsh bridge** tab surfaces this project's snapshot, diff,
  harvested tasks and cron status.
* [Hindsight](https://github.com/vectorize-io/hindsight) — the memory system used as the source of truth.
* [DeepSeek Harness (dsh)](https://github.com/deepseek-ai/deepseek-harness) — the coding agent whose
  `AGENTS.md` injection point makes the whole thing possible.

## License

MIT
