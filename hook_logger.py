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

超过 MAX_SIZE 自动轮转：events.jsonl → events.jsonl.1（保留最近一份）。

安装：在 ~/.claude/settings.json 的 hooks 里把 command 指向本文件即可。
"""
import sys
import json
import datetime
import os
import fcntl

MAX_SIZE = 20 * 1024 * 1024   # 20MB 轮转阈值
OUT = os.path.expanduser("~/.claude/status/events.jsonl")


def _prune_sid(sid):
    """删除文件中该 session 的历史行，只保留最后 1 条（刚写的 SessionStart/SessionEnd）。

    SessionStart 必须保留（浮窗靠它拿 cwd）；SessionEnd 必须保留（浮窗靠它移除卡片）。
    文件锁防并行 hook 进程竞态。
    """
    if not sid or not os.path.exists(OUT):
        return
    try:
        with open(OUT, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            lines = f.readlines()
            other = []
            sid_lines = []
            for line in lines:
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
            if sid_lines:
                f.seek(0)
                f.truncate()
                f.writelines(other + sid_lines[-1:])
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass


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

out = OUT
try:
    # 轮转：超过阈值 → 旧文件改名保留一份，开新文件
    if os.path.exists(out) and os.path.getsize(out) > MAX_SIZE:
        os.replace(out, out + ".1")
    with open(out, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
except Exception:
    pass

# session 生命周期闭环：新一轮开始 / 会话结束 → 清理该 session 的历史
# （上一轮事件对状态机已无意义，只留刚写的这条）
if name in ("SessionStart", "SessionEnd"):
    _prune_sid(record.get("session_id"))
