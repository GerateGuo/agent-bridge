#!/usr/bin/env python3
"""agent_bridge.py —— Hermes ↔ dsh 记忆桥（设计见 TASK.md）。

单一程序，三个动作，事实唯一来源＝Hindsight：

  harvest   dsh → Hindsight    把上游 agent 派发的 dsh 任务结论确定性地写回记忆库
  sync      Hindsight → dsh    把记忆渲染成 $DSH_HOME/AGENTS.md（dsh 首次请求自动读取）
  show      Hindsight → 终端    打印最近的任务结论（紧凑，几行）

设计红线：不新建记忆库（只保留一个游标）、不做常驻注入、脚本内不调用任何 LLM、
不写密钥、不删 Hindsight 数据。全部实现只用标准库，兼容系统 python3（3.9）。

配置优先级：环境变量 > ~/.agent-bridge/config.json > 内置默认值
（可配置项见 config.example.json 与 README）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()

# ── 配置：环境变量 > config.json > 默认值 ──────────────────────────────────
CONFIG_FILE = Path(os.environ.get("AGENT_BRIDGE_CONFIG") or (HOME / ".agent-bridge" / "config.json")).expanduser()


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_CONFIG = _load_config()


def cfg(key: str, env: str, default):
    """环境变量 > 配置文件 > 默认值。"""
    value = os.environ.get(env)
    if value:
        return value
    value = _CONFIG.get(key)
    return value if value not in (None, "") else default


HS_BASE = cfg("hs_base", "AGENT_BRIDGE_HS_BASE", "http://127.0.0.1:8988")
BANK = cfg("bank", "AGENT_BRIDGE_BANK", "default")
DSH_HOME = Path(cfg("dsh_home", "AGENT_BRIDGE_DSH_HOME", HOME / ".dsh")).expanduser()
SESSIONS = Path(cfg("sessions", "AGENT_BRIDGE_SESSIONS", DSH_HOME / "sessions")).expanduser()
AGENTS_FILE = Path(cfg("agents_file", "AGENT_BRIDGE_AGENTS_FILE", DSH_HOME / "AGENTS.md")).expanduser()
STATE_DIR = Path(cfg("state_dir", "AGENT_BRIDGE_STATE_DIR", HOME / ".agent-bridge")).expanduser()
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "bridge.log"

HERMES_TAG = cfg("tag", "AGENT_BRIDGE_TAG", "[hermes]")
MARKER = "<!-- dsh-bridge:auto-generated -->"
SETTLE_SECONDS = 60          # 只处理 60 秒没再被写入的会话文件
BACKUP_KEEP = 3

_DEFAULT_TOPICS = (
    ("用户的工作约定与偏好", "用户的偏好、约定、工作方式与决策习惯"),
    ("本机环境与路径事实", "这台机器的环境事实、路径、服务端口、工具位置"),
    ("dsh 的用法与配置", "dsh DeepSeek Harness 的用法、配置、ACP 与 headless 模式"),
)
_config_topics = _CONFIG.get("topics")
TOPICS = (tuple(tuple(pair) for pair in _config_topics)
          if isinstance(_config_topics, list) and _config_topics else _DEFAULT_TOPICS)

# 渲染瘦身：Hindsight 常把同一件事中英各存一份，英文抽取版对 dsh 价值低；同义改写又躲得过
# 「整行文本去重」，所以补一道相似度去重。这些值都可以用 sync 的 CLI 参数临时覆盖。
FACT_LIMIT = 150           # 每条事实保留的字符上限
PER_SECTION = 4            # 每个主题最多保留条数
RECENT_ROWS = 3            # 「最近 dsh 任务」保留条数
RECENT_LIMIT = 130         # 任务结论每条字符上限
SIMILARITY = 0.7           # 两条事实相似度超过它即视为重复
SESSION_NOTE = cfg("session_note", "AGENT_BRIDGE_SESSION_NOTE",
                   "思考过程使用英文思维链（reasoning in English），最终答复仍用中文。")


# ── 基础设施 ────────────────────────────────────────────────────────────────

def log(level: str, message: str) -> None:
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {level:5} {message}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if level in ("ERROR", "WARN"):
        print(line, file=sys.stderr)
    elif level == "INFO" and os.environ.get("BRIDGE_VERBOSE"):
        print(line)


def http_json(method: str, path: str, body=None, timeout: float = 30.0):
    """对本机 Hindsight 发一个 JSON 请求，返回解析后的 dict（失败抛 RuntimeError）。"""
    url = HS_BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Hindsight {method} {path} -> HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001 — 网络层任何异常都该显式失败
        raise RuntimeError(f"Hindsight {method} {path} 失败: {exc}") from exc


def bank_path(suffix: str) -> str:
    return f"/v1/default/banks/{urllib.parse.quote(BANK)}{suffix}"


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
            return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def zstd_decompress(path: Path):
    out = subprocess.run(["zstd", "-dc", str(path)], capture_output=True)
    if out.returncode != 0:
        log("ERROR", f"zstd 解压失败 {path.name}: {out.stderr.decode('utf-8', 'replace')[:160]}")
        return None
    return out.stdout.decode("utf-8", "replace")


def slug_to_cwd(slug: str) -> str:
    body = slug.strip("-")
    return "/" + body.replace("-", "/") if body else slug


# ── harvest：dsh → Hindsight ────────────────────────────────────────────────

def _text_parts(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text") or "") for c in content if isinstance(c, dict)).strip()
    return ""


def _last_user_request(prompt: str) -> str:
    """从 Hermes 塞进来的整段转录里取出最后一条 User 请求（确定性正则，不调模型）。"""
    match = re.search(r"User:\s*\n(.+?)(?:\n\s*\nContinue the conversation|\Z)", prompt, re.S)
    text = (match.group(1) if match else prompt).strip()
    return re.sub(r"\s+", " ", text)[:200]


def extract_session(path: Path):
    """把一个 dsh 会话文件抽成一条待写入的记忆；不是 Hermes 派的任务就返回 None。"""
    raw = zstd_decompress(path)
    if not raw:
        return None
    session_id = path.parent.name
    cwd = slug_to_cwd(path.parent.parent.name)
    tagged = False
    prompt = ""
    answer = ""
    title = ""
    usage = None
    last_ms = None

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        kind = record.get("type")
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        if isinstance(data.get("cwd"), str) and data["cwd"]:
            cwd = data["cwd"]
        if isinstance(data.get("usage"), dict):
            usage = data["usage"]
        if isinstance(record.get("time"), (int, float)):
            last_ms = record["time"]
        if kind == "user/message" and not tagged:
            text = _text_parts(data.get("content"))
            if text.startswith(HERMES_TAG):
                tagged = True
                prompt = text
        elif kind == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            text = _text_parts(message.get("content"))
            if text:
                answer = text
        elif kind == "session/title" and isinstance(data.get("title"), str):
            title = data["title"]

    if not tagged:
        return None
    if not answer:
        answer = title
    if not answer:
        log("WARN", f"{session_id}: 带标记但没有可用的答复文本，跳过")
        return None

    when = dt.datetime.fromtimestamp(last_ms / 1000) if last_ms else dt.datetime.now()
    usage_bits = ""
    if usage:
        usage_bits = (f"（输入 {usage.get('inputTokens', 0)}、缓存命中 {usage.get('cacheReadTokens', 0)}、"
                      f"输出 {usage.get('outputTokens', 0)}）")
    return {
        "session_id": session_id,
        "cwd": cwd,
        "answer": answer.strip(),
        "request": _last_user_request(prompt) if prompt else "",
        "usage": usage_bits,
        "timestamp": when.isoformat(timespec="seconds"),
        "title": title,
    }


def memory_item(record: dict) -> dict:
    content = f"dsh 任务结果（{record['cwd']}）：{record['answer']}"
    if record["request"]:
        content += f"\n（Hermes 派发的任务：{record['request']}）"
    return {
        "content": content[:4000],
        "context": cfg("task_context", "AGENT_BRIDGE_TASK_CONTEXT",
                       "dsh 执行上游 agent 派发的任务后的结论"),
        "timestamp": record["timestamp"],
        "tags": ["dsh-task", f"cwd:{record['cwd']}", f"session:{record['session_id']}"],
        "metadata": {"source": "agent_bridge.py", "profile": "acp"},
    }


def cmd_harvest(args) -> int:
    state = load_state()
    processed = state.setdefault("processed", {})
    if not SESSIONS.is_dir():
        log("ERROR", f"找不到 dsh 会话目录 {SESSIONS}")
        return 2

    candidates = []
    now = time.time()
    for path in SESSIONS.glob("*/*/session*.jsonl.zstd"):
        try:
            if now - path.stat().st_mtime < SETTLE_SECONDS:
                continue
        except OSError:
            continue
        if path.parent.name in processed:
            continue
        candidates.append(path)
    candidates.sort(key=lambda p: p.stat().st_mtime)
    log("INFO", f"harvest: 候选会话 {len(candidates)} 个（已处理 {len(processed)} 个）")

    items, new_ids = [], []
    for path in candidates:
        record = extract_session(path)
        if record is None:
            processed[path.parent.name] = "skipped"      # 不是 Hermes 派的任务，别反复解压
            continue
        items.append((record, memory_item(record)))
        new_ids.append(record["session_id"])

    if not items:
        print("没有新的 Hermes 派发任务需要写回。")
        if not args.dry_run:
            save_state(state)
        return 0

    print(f"发现 {len(items)} 条待写回：")
    for record, item in items:
        print(f"  · {record['timestamp']}  cwd={record['cwd']}  {item['content'][:110]}…")

    if args.dry_run:
        print("\n--- dry-run：以下 payload 不会真的发送 ---")
        print(json.dumps({"items": [i for _, i in items], "async": True}, ensure_ascii=False, indent=1)[:2000])
        return 0

    ok = 0
    for record, item in items:
        try:
            http_json("POST", bank_path("/memories"), {"items": [item], "async": True})
            processed[record["session_id"]] = record["timestamp"]
            ok += 1
        except RuntimeError as exc:
            log("ERROR", f"写回失败 {record['session_id']}: {exc}")
    save_state(state)
    print(f"写回成功 {ok}/{len(items)} 条到 Hindsight（bank {BANK}）。")
    log("INFO", f"harvest 完成：{ok}/{len(items)}")
    return 0 if ok == len(items) else 1


# ── sync：Hindsight → dsh（~/.dsh/AGENTS.md）────────────────────────────────

def recall(query: str, max_tokens: int = 1200, tags=None) -> list:
    body = {"query": query, "max_tokens": max_tokens}
    if tags:
        body["tags"] = tags
        body["tags_match"] = "all"
    result = http_json("POST", bank_path("/memories/recall"), body, timeout=60.0)
    out = []
    for item in (result.get("results") or result.get("items") or []):
        if isinstance(item, dict) and item.get("text"):
            out.append(str(item["text"]).strip())
    return out


def list_memories(tags=None, limit: int = 10) -> list:
    """按 tag 列记忆。tags 是数组参数，必须重复传（`?tags=a&tags=b`），不能逗号拼接。"""
    params = [f"limit={int(limit)}"]
    if tags:
        params += ["tags=" + urllib.parse.quote(tag) for tag in tags]
        params.append("tags_match=all_strict")
    result = http_json("GET", bank_path("/memories/list") + "?" + "&".join(params))
    items = result.get("items") or result.get("memories") or []
    items = [i for i in items if isinstance(i, dict)]
    if tags:
        wanted = set(tags)
        strict = [i for i in items
                  if wanted & {t for t in (i.get("tags") or []) if isinstance(t, str)}]
        if strict:
            items = strict
        elif items:
            log("WARN", f"tag 过滤未命中({','.join(tags)})，返回的 {len(items)} 条不含该 tag")
    return items


def _first_line(text: str, limit: int = 160) -> str:
    """去掉 Hindsight 自己加的元数据尾巴（`| When: … | Involving: … | 原因`），压成一行。"""
    line = re.sub(r"\s*\|\s*(When|Where|Involving|Why)\s*:.*$", "", text.strip(), flags=re.S)
    line = re.sub(r"\s+", " ", line).strip()
    return line[:limit] + ("…" if len(line) > limit else "")


_CJK = re.compile(r"[\u4e00-\u9fff]")


def _mostly_ascii(text: str) -> bool:
    """Hindsight 的英文抽取版：ASCII 字母远超汉字。技术条目里路径/参数本身就多，
    所以不能只比字母数——阈值放宽到「3×汉字数 + 30」。"""
    letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return letters > 3 * len(_CJK.findall(text)) + 30


def _too_similar(text: str, kept: list, ratio: float = SIMILARITY) -> bool:
    """同义改写去重：整行相等比不出来，用相似度兜住（比对全串——只比前 N 字符会被日期前缀骗过）。"""
    return any(difflib.SequenceMatcher(None, text, prev).ratio() > ratio for prev in kept)


def _grams(text: str, n: int = 2) -> set:
    """去标点后的二元字组，用来衡量中文两条事实的覆盖程度。"""
    body = re.sub(r"[\s\W_]+", "", text)
    return {body[i:i + n] for i in range(len(body) - n + 1)}


def _covered(text: str, kept: list, cover: float = 0.7) -> bool:
    """短条目的说法基本已被某个更长条目包含 ⇒ 同一件事的另一次抽取，砍掉。"""
    cur = _grams(text)
    if len(cur) < 8:
        return False
    for prev in kept:
        if len(text) > len(prev):
            continue
        bag = _grams(prev)
        if bag and len(cur & bag) / len(cur) >= cover:
            return True
    return False


def render_snapshot(recent: list, recall_sections, max_chars: int,
                    per_section: int = PER_SECTION, fact_limit: int = FACT_LIMIT,
                    recent_rows: int = RECENT_ROWS, recent_limit: int = RECENT_LIMIT) -> str:
    head = [
        MARKER,
        "# 共享事实（本文件由 agent_bridge.py 从 Hindsight 自动生成，请勿手改）",
        "",
        f"生成时间：{dt.datetime.now().isoformat(timespec='seconds')}　来源：Hindsight bank {BANK}",
        "用途：dsh（DeepSeek Harness）在会话开始时可读到这些事实，从而不必重新摸索本机环境与约定。",
        "",
        "## dsh 的会话约定",
        f"- {SESSION_NOTE}",
        "",
    ]
    body, seen = [], set()
    for topic, facts in recall_sections:
        kept = []
        for fact in facts:
            clean = _first_line(fact, fact_limit)
            if len(clean) < 12 or _mostly_ascii(clean):
                continue
            if clean in seen or _too_similar(clean, kept) or _covered(clean, kept):
                continue
            seen.add(clean)
            kept.append(clean)
            if len(kept) >= per_section:
                break
        if not kept:
            continue
        body.append(f"## {topic}")
        body += [f"- {fact}" for fact in kept]
        body.append("")
    if recent:
        rows = []
        for item in recent[:recent_rows]:
            stamp = str(item.get("date") or item.get("timestamp") or "")[:16]
            tags = item.get("tags") or []
            cwd = next((t[4:] for t in tags if isinstance(t, str) and t.startswith("cwd:")), "")
            line = _first_line(str(item.get("text") or ""), recent_limit)
            if not line or _mostly_ascii(line):
                continue
            rows.append(f"- [{stamp}]{' ' + cwd if cwd else ''} {line}")
        if rows:
            body.append("## 最近由 Hermes 派发的 dsh 任务")
            body += rows
            body.append("")
    text = "\n".join(head + body).rstrip() + "\n"
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n\n（已达大小上限，较旧的段落被省略）\n"
    return text


def cmd_sync(args) -> int:
    recent = list_memories(tags=["dsh-task"], limit=args.recent)
    sections = []
    for topic, query in TOPICS:
        try:
            sections.append((topic, recall(query, max_tokens=args.tokens_per_topic)))
        except RuntimeError as exc:
            log("WARN", f"recall 失败（{topic}）：{exc}")
            sections.append((topic, []))
    snapshot = render_snapshot(recent, sections, args.max_chars,
                               per_section=args.per_section, fact_limit=args.fact_limit,
                               recent_rows=args.recent_rows)

    if args.dry_run:
        print(f"--- dry-run：将要写入 {AGENTS_FILE}（{len(snapshot)} 字符）---")
        print(snapshot[:2500])
        return 0

    AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if AGENTS_FILE.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        backup = AGENTS_FILE.with_name(f"AGENTS.md.bak.{stamp}")
        backup.write_text(AGENTS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        backups = sorted(AGENTS_FILE.parent.glob("AGENTS.md.bak.*"))
        for old in backups[:-BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
    tmp = AGENTS_FILE.with_suffix(".tmp")
    tmp.write_text(snapshot, encoding="utf-8")
    os.replace(tmp, AGENTS_FILE)
    print(f"已写入 {AGENTS_FILE}（{len(snapshot)} 字符，{snapshot.count(chr(10) + '- ')} 条事实）。")
    log("INFO", f"sync 完成：{len(snapshot)} 字符")
    return 0


# ── show：Hindsight → Hermes ────────────────────────────────────────────────

def cmd_show(args) -> int:
    items = list_memories(tags=["dsh-task"], limit=args.limit)
    if not items:
        print("Hindsight 里还没有 dsh-task 记忆。")
        return 0
    print(f"最近 {len(items)} 条 dsh 任务结论（bank {BANK}）：")
    for item in items:
        stamp = str(item.get("date") or item.get("timestamp") or "")[:16]
        tags = [t for t in (item.get("tags") or []) if isinstance(t, str)]
        cwd = next((t[4:] for t in tags if t.startswith("cwd:")), "")
        print(f"  [{stamp}] {cwd}")
        print(f"      {_first_line(str(item.get('text') or ''), 220)}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    # --dry-run 允许写在子命令之前：先摘出来再解析，否则 argparse 会直接报错退出
    _dry_early = "--dry-run" in raw_argv
    _clean_argv = [token for token in raw_argv if token != "--dry-run"]

    parser = argparse.ArgumentParser(description="Hermes ↔ dsh 记忆桥（Hindsight 为唯一事实源）")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="只打印，不写任何东西")

    p_harvest = sub.add_parser("harvest", parents=[common], help="把 Hermes 派发的 dsh 任务结论写回 Hindsight")
    p_harvest.set_defaults(func=cmd_harvest)

    p_sync = sub.add_parser("sync", parents=[common], help="把 Hindsight 渲染成 ~/.dsh/AGENTS.md")
    p_sync.add_argument("--max-chars", type=int, default=12000)
    p_sync.add_argument("--per-section", type=int, default=PER_SECTION, help="每个主题最多保留条数")
    p_sync.add_argument("--fact-limit", type=int, default=FACT_LIMIT, help="每条事实的字符上限")
    p_sync.add_argument("--recent-rows", type=int, default=RECENT_ROWS, help="最近任务保留条数")
    p_sync.add_argument("--recent", type=int, default=8, help="最近任务查询条数")
    p_sync.add_argument("--tokens-per-topic", type=int, default=1200)
    p_sync.set_defaults(func=cmd_sync)

    p_show = sub.add_parser("show", parents=[common], help="打印最近的任务结论")
    p_show.add_argument("--limit", type=int, default=10)
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args(_clean_argv)
    if _dry_early:
        args.dry_run = True
    try:
        return args.func(args)
    except RuntimeError as exc:
        log("ERROR", str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
