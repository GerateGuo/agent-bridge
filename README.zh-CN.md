# agent-bridge

[English](README.md) · **简体中文**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)

一个**单文件、零依赖**的桥：让你的主 agent 与编程 agent 共用同一套事实——事实唯一来源是
[Hindsight](https://github.com/vectorize-io/hindsight)，不新增第二个记忆库。

它最初是为 **Hermes Agent ⇄ [dsh](https://github.com/deepseek-ai/deepseek-harness)（DeepSeek Harness）**
写的，但机制是通用的：读一边 agent 的会话日志，写出一份 Markdown 事实文件，另一边 agent 自己会读。

```
                 harvest（每小时）
dsh 会话日志 ───────────────────────► Hindsight（记忆 API，唯一事实源）
~/.dsh/sessions/*/session.v3.jsonl.zstd      │
                                             │  sync（每天）
              $DSH_HOME/AGENTS.md  ◄─────────┘
                      │
                      └─► 注入每个新 dsh 会话的「首次请求」
                          （dsh 自带 @deepseek-ai/dsh-agent-instructions，零插件、零 profile 改动）
```

## 要解决的问题

一台机器上两个 agent：主 agent（Hermes）派活，编程 agent（dsh）干活。它们需要同一份上下文——
你的约定、本机路径、工具配置、最近的任务结论。没有这座桥，你只能：

* 每次派活时把这些上下文重新写进任务简报；
* 或者让编程 agent 每个会话都从头摸索一遍这台机器；
* 或者「顺手同步一下记忆」，结果多出一个**第二记忆库**，慢慢就与真实情况漂移了。

## 三个命令

| 命令 | 方向 | 用途 |
|---|---|---|
| `harvest` | dsh → Hindsight | 从编程 agent 自己的会话日志里**确定性抽取**：最终答复 + token 用量 + cwd + 时间。脚本内不调用任何 LLM，重复运行幂等（游标存在 `state.json`）。 |
| `sync` | Hindsight → dsh | 把记忆渲染成紧凑的 Markdown 快照写入 `$DSH_HOME/AGENTS.md`，编程 agent 每个会话开始时会读它。原子写，旧版本保留为 `AGENTS.md.bak.<ts>`（默认 3 份）。 |
| `show` | Hindsight → 终端 | 按需打印最近回收的任务结论，每条几行——不必把编程 agent 的日志读进主 agent 的上下文。 |

`--dry-run` 写在子命令前后都行，且什么都不写（不 POST、不动文件、不推进游标）。

## 它为什么成立（一个不太显然的事实）

dsh 自带 `@deepseek-ai/dsh-agent-instructions`，在所有 profile 里默认启用。它在会话的**首次请求**时注入
用户级 `$DSH_HOME/AGENTS.md`，随后是从项目根到会话目录的 `AGENTS.md` / `CLAUDE.md` 链
（`.git` 标记项目根，字节预算 65536，超宽的文件先被省略）。

所以给编程 agent 输送共享事实**不需要任何插件、不需要改 profile**——写那个文件就够了。验证它真的被读到：

```bash
zstd -dc ~/.dsh/sessions/*/*/session.v3.jsonl.zstd | grep -c 'dsh-bridge:auto-generated'   # 期望 ≥ 1
```

## 依赖

* Python 3.9+——只用标准库，不用 `pip install`，没有构建步骤
* 本机可达的 [Hindsight](https://github.com/vectorize-io/hindsight)（默认 `http://127.0.0.1:8988`）
* dsh，会话日志在 `$DSH_HOME/sessions` 下
* `zstd` 命令行（`harvest` 用它解压会话日志）

## 快速开始

```bash
git clone https://github.com/GerateGuo/agent-bridge.git ~/agent-bridge
cd ~/agent-bridge

mkdir -p ~/.agent-bridge
cp config.example.json ~/.agent-bridge/config.json
$EDITOR ~/.agent-bridge/config.json          # 至少把 "bank" 改成你自己的 Hindsight bank id

python3 agent_bridge.py harvest --dry-run    # 先看将要写回什么
python3 agent_bridge.py sync                 # 生成编程 agent 要读的快照
python3 agent_bridge.py show --limit 10      # 最近的回收结论
```

## 配置

优先级：**环境变量 > `~/.agent-bridge/config.json` > 内置默认值**。

| `config.json` 键 | 环境变量 | 默认值 | 含义 |
|---|---|---|---|
| `bank` | `AGENT_BRIDGE_BANK` | `default` | Hindsight bank id（可为非 ASCII，会做 percent-encode） |
| `hs_base` | `AGENT_BRIDGE_HS_BASE` | `http://127.0.0.1:8988` | Hindsight 数据面地址 |
| `dsh_home` | `AGENT_BRIDGE_DSH_HOME` | `~/.dsh` | dsh 主目录（`$DSH_HOME`） |
| `sessions` | `AGENT_BRIDGE_SESSIONS` | `$DSH_HOME/sessions` | dsh 会话日志位置 |
| `agents_file` | `AGENT_BRIDGE_AGENTS_FILE` | `$DSH_HOME/AGENTS.md` | 快照写入目标 |
| `state_dir` | `AGENT_BRIDGE_STATE_DIR` | `~/.agent-bridge` | 游标（`state.json`）与日志（`bridge.log`） |
| `tag` | `AGENT_BRIDGE_TAG` | `[hermes]` | 用来识别「我派发的会话」的标记 |
| `topics` | —（仅 `config.json`） | 内置三个主题 | `[["小节标题", "recall 查询"], …]`，决定 `sync` 向 Hindsight 问什么 |
| `session_note` | `AGENT_BRIDGE_SESSION_NOTE` | 英文思维链 + 中文答复 | 固定在快照最前的一段会话约定 |
| `task_context` | `AGENT_BRIDGE_TASK_CONTEXT` | `dsh 执行上游 agent 派发的任务后的结论` | 写回记忆时附带的 `context` 字段 |

`AGENT_BRIDGE_CONFIG` 可以改变配置文件本身的位置。路径支持 `~`。

## 派活纪律（为什么标记很重要）

`harvest` 只回收**首条用户消息以标记开头**（默认 `[hermes]`）的会话——这样编程 agent 的闲聊、以及
与派活无关的会话永远不会进你的记忆。所以把派活统一走包装脚本，免得手打漏掉：

```bash
# examples/dispatch.sh
export PATH="$HOME/.npm-global/bin:$PATH"
TASK="$*"; TAG="${DSH_TAG:-[hermes]}"
cd "${HERMES_DSH_ACP_CWD:-$PWD}" && dsh --profile headless "$TAG $TASK"
```

当 Hermes 通过 `dsh-acp` provider 驱动 dsh 时，标记会自动带上（`HERMES_DSH_ACP_TAG`），
这类会话不需要包装脚本也能被回收。

## 定时任务

两个 cron 就够了（见 `examples/`）：

| 任务 | 频率 | 脚本 |
|---|---|---|
| `agent-bridge-sync` | 每天，例如 08:00 | `examples/cron-sync.sh` |
| `agent-bridge-harvest` | 每小时 | `examples/cron-harvest.sh` |

两个都是纯脚本——不调 LLM，不花 token。示例脚本里已经处理了两个真实踩过的坑：

* cron 的 `PATH` 极简（macOS 上只有 `/usr/bin:/bin`），而 `zstd` 通常在 `/opt/homebrew/bin`
  → `harvest` 会 `FileNotFoundError: 'zstd'`。所以要显式导出 `PATH`。
* 如果夜里本机没有网络：写回本地 Hindsight 是能成的，但后面的事实抽取要走上游、必然失败。
  示例 harvest 脚本会在可配置的断网窗口内安静退出，之后一次性把积压收完。

## 设计红线

1. **不新建第二记忆库。** 程序不持有知识；`AGENTS.md` 是派生物，删掉能重建。唯一的本地状态是一个游标（`state.json`）。
2. **不做常驻注入。** 没有任何内容被塞进主 agent 的每轮上下文；编程 agent 每个会话自己读一次那个文件。
3. **脚本内不调用 LLM。** 抽取是确定性解析，不是摘要重写。
4. **不写密钥。** 快照里只有事实与约定，绝不出现 token / key / 密码 / 人格（`SOUL`）内容。
5. **对 Hindsight 只追加。** 只写不删；要退役一条过时事实，是你自己主动、可回滚的 `invalidate` 操作。
6. **并发安全。** 正在被写入的会话文件会被跳过（只处理 60 秒内没再变动的文件）。

## 验收记录

macOS、dsh `0.1.5-rc.1`（2026-09-15）实测：

| # | 验收项 | 结果 |
|---|---|---|
| 1 | `--dry-run` 写在子命令前后都对 | ✅ 什么都不写（`AGENTS.md` mtime 不变） |
| 2 | 写回后能从 API 查回 | ✅ 记忆带 `session:` / `cwd:` tag |
| 3 | `sync` 后编程 agent 真的读到 | ✅ 新会话日志里 `dsh-bridge:auto-generated` 出现 1 次 |
| 4 | 幂等 | ✅ 连跑两次 `harvest`，`dsh-task` 条数不变 |
| 5 | `show --limit 5` | ✅ 每条任务一块，紧凑打印 |

## 开销说明

快照会进入**每个会话的首次请求**，所以要控制长度；`sync` 会打印最终字符数。参考值：1500 字符的快照
≈ 0.5k tokens，叠加在 dsh 自身约 7.1k tokens 的地板（harness 提示 + 工具 schema）之上；因为前缀稳定，
绝大部分按便宜的 prefix-cache 计费。可调项：`--max-chars`、`--per-section`、`--fact-limit`、`--recent-rows`。

## 相关项目

* [hindsight-dashboard](https://github.com/GerateGuo/hindsight-dashboard)——自托管 Hindsight 的单文件面板；
  它可选的 **dsh 桥**标签页就是把这个项目的快照、diff、回收记录与 cron 状态摆出来看。
* [Hindsight](https://github.com/vectorize-io/hindsight)——作为事实唯一来源的记忆系统。
* [DeepSeek Harness (dsh)](https://github.com/deepseek-ai/deepseek-harness)——它的 `AGENTS.md` 注入点让这套设计成立。

## 许可

MIT
