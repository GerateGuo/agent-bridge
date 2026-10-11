# TASK: agent_bridge.py —— Hermes ↔ dsh 记忆桥（单程序、无第二事实源）

> 落盘路径：`~/agent-bridge/TASK.md`　｜　写入日期：2026-09-15　｜　派活方式：`dsh --profile headless "读 ~/agent-bridge/TASK.md，严格按它完成"`
> 除本文件外，任何"顺手改进"都不要做。

## 1. 目标

写**一个**自包含程序 `~/agent-bridge/agent_bridge.py`，让 Hermes 侧的 agent 与 dsh（DeepSeek Harness）共享事实，
**事实唯一来源＝Hindsight**（本机 `http://127.0.0.1:8988`，bank 名由配置决定，默认 `default`）。

设计红线（违反即返工）：

1. **不新建任何记忆库**。程序不持有知识；`~/.dsh/AGENTS.md` 是**派生物**，删掉能重建；
   唯一的本地状态是 `~/.agent-bridge/state.json`（只存游标：最后处理过的 dsh 会话 id + 时间）。
2. **不做常驻注入**。除了 `~/.dsh/AGENTS.md`（Hindsight→dsh，dsh 首次请求时自己读），
   任何内容都不许往 Hermes 的每轮上下文里塞。
3. **程序内部不调用任何 LLM**（确定性抽取；不要在脚本里 summarise/rewrite）。
4. **不写密钥**。`~/.dsh/AGENTS.md` 只含事实与约定，禁止出现 token / key / 密码 / 人格（SOUL）内容。
5. **不删 Hindsight 数据**；只追加。

## 2. 已实测的事实（直接采信，禁止重新摸索）

### 2.1 Hindsight 数据面 API（本机环回，免鉴权；实测 200）

| 用途 | 调用 |
|---|---|
| 写入记忆 | `POST /v1/default/banks/<bank>/memories`　body: `{"items":[MemoryItem,...],"async":true}` |
| 按 tag 列记忆 | `GET /v1/default/banks/<bank>/memories/list?tags=dsh-task&tags_match=all&limit=20` |
| 语义检索 | `POST /v1/default/banks/<bank>/memories/recall`　body: `{"query":"…","tags":["…"],"tags_match":"all","max_tokens":1500}` |
| 健康 | `GET /health` → `{"status":"healthy"}` |

`bank_id` 是用户自定的字符串（可为中文），URL 里要 percent-encode（`urllib.parse.quote`）。
`MemoryItem` 字段（实测 schema）：`content`(必填, str) / `timestamp`(ISO8601 或省略) / `context`(str) /
`tags`(str[]) / `metadata`(str→str) / `document_id` / `entities` / `update_mode`。

### 2.2 dsh 会话日志（harvest 的数据源）

* 路径：`~/.dsh/sessions/<cwd-slug>/<session-id>/session.v3.jsonl.zstd`
* **zstd 压缩**，本机解压用 CLI：`zstd -dc <file>`（`/opt/homebrew/bin/zstd`；`zstandard` python 包**没装**，不要用）。
* 记录类型（实测）：`session` `permission/preset` `sandbox/mode` `turn/start` `step/start` `user/message`
  `assistant/message` `request/header` `request/context` `session/title` `step/end` `turn/end`。
* `assistant/message` → `data.message.content[*].text`（最终答复）；`data.usage` 形如
  `{"inputTokens":…,"outputTokens":…,"cacheReadTokens":…,"reasoningTokens":…}`；记录时间在顶层 `time`（毫秒 epoch）。
* **Hermes 派的任务带标记**：`user/message` 的 `content[0].text` 以 `[hermes] task dispatched by Hermes via the dsh-acp provider` 开头
  → 用这个筛"我派的活"。

### 2.3 dsh 读共享上下文的位置（sync 的输出目标）

`@deepseek-ai/dsh-agent-instructions`（ACP/web profile 里都默认启用）在会话**首次请求**时注入：
**用户级 `$DSH_HOME/AGENTS.md`（= `~/.dsh/AGENTS.md`）**，随后是从项目根到会话 cwd 的 `AGENTS.md`/`CLAUDE.md` 链。
字节预算 `maxBytes: 65536`，**更宽的文件会先被省略**。→ **不需要任何插件或 profile 改动**，写这个文件即可。

## 3. 要实现的东西

单个文件 `~/agent-bridge/agent_bridge.py`，只用标准库，**必须能在系统 `python3`（macOS 自带 3.9）下运行**
（用 `from __future__ import annotations`，不要用 3.10+ 语法）。三个子命令 + 全局 `--dry-run`：

### `harvest` —— dsh → Hindsight
1. 扫 `~/.dsh/sessions/*/*/session.v3.jsonl.zstd`，只取比游标新的文件；
2. 解压后**确定性抽取**：任务标识（`[hermes]` 标记所在 `user/message`）、最终 `assistant/message` 的文本、
   `usage` 各项、`cwd`、会话 id、时间。
3. 组装 MemoryItem：`content` = "dsh 任务结果：<最终答复>"（若答复为空则取最后一条 assistant 文本），
   `context` = `"dsh(--profile acp) 执行 Hermes 派发的任务"`，
   `tags` = `["dsh-task", "cwd:<cwd>", "session:<id>"]`，`timestamp` = 该会话最后时间（ISO8601）。
4. `POST` 到 Hindsight（`async: true`）；成功后才推进游标；单个会话解析失败只记日志、不中断。
5. **幂等**：同一 session id 不重复写。

### `sync` —— Hindsight → dsh
生成 `~/.dsh/AGENTS.md`（覆盖写，首行必须含标记 `<!-- dsh-bridge:auto-generated -->`）：
固定几个主题各打一次 `recall`（例：`dsh 的用法与配置`、`用户的工作约定与偏好`、`本机环境与路径事实`、
`最近的任务与结论`），加上最近若干条 `tags=dsh-task` 的 `list` 结果，渲染成简洁 Markdown。
**总长默认上限 12000 字符**（`--max-chars` 可调），超出时按顺序截断较旧/较次要的段落，不要截断到半句。

### `show` —— Hindsight → Hermes
`list` 最近 N 条（`--limit`，默认 10）`dsh-task` 记忆，输出每行一条的紧凑摘要（时间 + cwd + 答复首行 ≤160 字符）。

### 通用
* `--dry-run`：打印将要写入的内容/payload，**不写**（Hindsight 不 POST、`AGENTS.md` 不动、游标不推进）。
* 日志：追加到 `~/agent-bridge/bridge.log`（时间戳 + 级别 + 一句话）。
* 出错时退出码非 0，并把原因写日志与 stderr。

## 4. 硬性约束

1. 只用标准库（`urllib.request`/`json`/`subprocess`/`pathlib`/`re`/`argparse`/`datetime`）。
2. 不修改除 `~/.dsh/AGENTS.md`、`~/.agent-bridge/state.json`、`~/agent-bridge/bridge.log` 之外的任何用户文件。
3. `~/.dsh/AGENTS.md` 写入用**原子写**（临时文件 + `os.replace`），且在覆盖前把旧内容备份为
   `~/.dsh/AGENTS.md.bak.<ts>`（保留最近 3 份）。
4. 不 print 任何密钥；不把会话全文塞进记忆（只存抽取后的结论 + 用量数字）。
5. `dsh` 会话文件可能正在被写入（并发）：只处理 **mtime 早于 60 秒前**的文件。

## 5. 验收（必须逐条给出真实输出，写在文末"验证结果"一节）

1. `python3 ~/agent-bridge/agent_bridge.py harvest --dry-run`
   → 至少打印 1 条拟写入的 MemoryItem（含 tags），且 `~/.dsh/AGENTS.md` 与 state.json 未被改动（`ls -l` 前后对比）。
2. `python3 ~/agent-bridge/agent_bridge.py harvest` 然后
   `curl -s "http://127.0.0.1:8988/v1/default/banks/<bank>/memories/list?tags=dsh-task&tags_match=all&limit=5"`
   （bank 名按需 percent-encode，例如中文 bank 用 `urllib.parse.quote`）
   → 返回里能看到刚写的那条（含 `session:<id>` 痕迹）。
3. `python3 ~/agent-bridge/agent_bridge.py sync` → `~/.dsh/AGENTS.md` 存在、首行含 `dsh-bridge:auto-generated`、
   字节数 ≤ `--max-chars`；随后跑一次 `dsh --profile headless "Reply with exactly: AGENTS OK"`，
   解压该会话日志，用
   `zstd -dc <session> | grep -c "dsh-bridge:auto-generated"` → **≥1**（证明真的注入到 dsh 的首个请求）。
4. 重复跑 `harvest` 两次 → Hindsight 里 `dsh-task` 条数**不增加**（幂等）。
5. `python3 ~/agent-bridge/agent_bridge.py show --limit 5` → 紧凑打印最近 5 条。

## 6. 汇报格式（只这三样）

1. 结果：做了什么，脚本路径。
2. 我亲手验的证据：上面 5 条的命令 + 真实输出（可截断）。
3. 残留/风险/未做项：一条一句。

不要贴完整源码，不要写"步骤表格"。
