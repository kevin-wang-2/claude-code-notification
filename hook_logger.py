#!/usr/bin/env python3
"""Claude Code hook logger — 正式版：只落元数据，不落工具内容。

替代研究版 hook_logger.py 后，events.jsonl 不再包含 tool_input / tool_response
（可能含文件内容），只保留状态机需要的字段：
  ts / hook_event_name / session_id / cwd / transcript_path
  + tool_name / permission_mode（PreToolUse 等）
  + last_assistant_message（Stop，截断 200 字）
  + source（SessionStart）

安装：在 ~/.claude/settings.json 的 hooks 里把 command 指向本文件即可。
"""
import sys
import json
import datetime
import os

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
if name == "SessionStart" and data.get("source"):
    record["source"] = data["source"]
if name == "Stop":
    msg = data.get("last_assistant_message", "")
    if msg:
        record["last_assistant_message"] = msg[:200]

out = os.path.expanduser("~/.claude/status/events.jsonl")
try:
    with open(out, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
except Exception:
    pass
