#!/usr/bin/env python3
"""Claude Code hook logger — 正式版：只落元数据，不落工具内容。

替代研究版后，events.jsonl 不再包含 tool_input / tool_response
（可能含文件内容），只保留状态机需要的字段：
  ts / hook_event_name / session_id / cwd / transcript_path
  + tool_name / permission_mode（PreToolUse 等）
  + is_interrupt（PostToolUseFailure：用户中断工具执行）
  + error（StopFailure：API 错误类型）
  + last_assistant_message（Stop，截断 200 字）
  + source（SessionStart）

并发模型：轮转 / 追加 / 清理三步全部在同一把文件锁内完成。
  （2026-09-08 修复：原先追加是裸写、只有 prune 上锁，于是
   "A 追加一行" 与 "B 正在 prune 的读-截断-回写" 会相互覆盖——
   B 的快照早于 L 就把 L 抹掉了。/clear 时 3~4 个 hook 进程在几秒内
   密集触发，最容易丢 SessionEnd，卡片就永远删不掉。）

超过 MAX_SIZE 自动轮转：内容搬到 events.jsonl.1，原文件就地清空
（保持 inode，不用 os.replace，避免等锁的进程写进已被改名的旧文件）。

安装：在 ~/.claude/settings.json 的 hooks 里把 command 指向本文件即可。
"""
import sys
import json
import datetime
import os
import fcntl

MAX_SIZE = 20 * 1024 * 1024   # 20MB 轮转阈值
OUT = os.path.expanduser("~/.claude/status/events.jsonl")


def _rotate_locked(f):
    """已持锁：超阈值则把内容搬到 .1，原文件就地清空。必须在追加之前调用。"""
    f.seek(0, os.SEEK_END)
    if f.tell() <= MAX_SIZE:
        return
    f.seek(0)
    data = f.read()
    try:
        with open(OUT + ".1", "w") as old:
            old.write(data)
    except Exception:
        pass
    f.seek(0)
    f.truncate()


def _prune_locked(f, sid):
    """已持锁：删除该 session 的历史行，只保留最后 1 条（刚写的 SessionStart/SessionEnd）。

    SessionStart 必须保留（浮窗靠它拿 cwd）；SessionEnd 必须保留（浮窗靠它移除卡片）。
    """
    if not sid:
        return
    f.seek(0)
    other = []
    sid_lines = []
    for line in f.readlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("session_id") == sid:
            sid_lines.append(line + "\n")
        else:
            other.append(line + "\n")
    if not sid_lines:
        return
    f.seek(0)
    f.truncate()
    # 文件以 a+ 打开（O_APPEND），truncate 后 EOF=0，写入自动落回开头
    f.writelines(other + sid_lines[-1:])


raw = sys.stdin.read()
try:
    data = json.loads(raw)
except Exception:
    sys.exit(0)

name = data.get("hook_event_name", "")
record = {
    "ts": datetime.datetime.now().isoformat(timespec="seconds"),
    "hook_event_name": name,
    "session_id": data.get("session_id"),
    "cwd": data.get("cwd"),
    "transcript_path": data.get("transcript_path"),
}

# 元数据白名单（绝不落 tool_input / tool_response）
if data.get("tool_name"):
    record["tool_name"] = data["tool_name"]
if data.get("permission_mode"):
    record["permission_mode"] = data["permission_mode"]
if name == "PostToolUseFailure" and "is_interrupt" in data:
    record["is_interrupt"] = bool(data["is_interrupt"])
if name == "StopFailure" and data.get("error"):
    record["error"] = data["error"]
if name == "SessionStart" and data.get("source"):
    record["source"] = data["source"]
if name == "Notification" and data.get("message"):
    record["message"] = str(data["message"])[:200]
if name == "Stop":
    msg = data.get("last_assistant_message", "")
    if msg:
        record["last_assistant_message"] = msg[:200]

# session 生命周期闭环：新一轮开始 / 会话结束 → 清理该 session 的历史
# （上一轮事件对状态机已无意义，只留刚写的这条）
prune_sid = record.get("session_id") if name in ("SessionStart", "SessionEnd") else None

try:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            _rotate_locked(f)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if prune_sid:
                f.flush()
                _prune_locked(f, prune_sid)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
except Exception:
    pass
