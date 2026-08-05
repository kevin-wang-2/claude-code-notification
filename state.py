"""Claude 多 Session 状态机 — 读 ~/.claude/status/events.jsonl，维护每个 session 的状态。

状态机（2026-08-05 实测定案）：
- PermissionRequest → needs_attention（直接信号，弹框即触发）
- AskUserQuestion   → needs_attention（立即）
- PreToolUse 后 10s+ 无 PostToolUse → needs_attention（兜底）
- PostToolUse → working（刷新）
- Stop → idle（附 last_assistant_message）
- SessionEnd → 移除
- 60s 无任何事件 → idle（兜底）
"""
import json
import os
import time
import datetime
from pathlib import Path

EVENTS_FILE = Path.home() / ".claude" / "status" / "events.jsonl"

ATTENTION_GAP = 10.0   # PreToolUse 悬置超过此秒数 → needs_attention
IDLE_TIMEOUT = 60.0    # 无事件超过此秒数 → idle

STATUS_WORKING = "working"
STATUS_IDLE = "idle"
STATUS_ATTENTION = "needs_attention"

STATUS_LABEL = {
    STATUS_WORKING: "工作中",
    STATUS_IDLE: "空闲",
    STATUS_ATTENTION: "需关注",
}


def decode_project(transcript_path):
    """~/.claude/projects/<编码路径>/<session-id>.jsonl → 初始项目路径。

    编码规则：去掉前导 '/'，'/' → '-'（实测已验证可反解）。
    """
    try:
        parts = transcript_path.split("/")
        idx = parts.index("projects")
        encoded = parts[idx + 1]
        return "/" + encoded.lstrip("-").replace("-", "/")
    except Exception:
        return "?"


def _parse_ts(ts_str, fallback):
    try:
        return datetime.datetime.fromisoformat(ts_str).timestamp()
    except Exception:
        return fallback


def _get(record, *keys, default=None):
    """兼容新旧两种 hook 日志格式：顶层字段 或 extra 里。"""
    for k in keys:
        if record.get(k) is not None:
            return record[k]
        extra = record.get("extra") or {}
        if extra.get(k) is not None:
            return extra[k]
    return default


class SessionState:
    __slots__ = ("session_id", "project", "status", "last_message",
                 "last_event_ts", "pending_ts", "attention_reason", "tool")

    def __init__(self, session_id, project):
        self.session_id = session_id
        self.project = project
        self.status = STATUS_IDLE
        self.last_message = ""
        self.last_event_ts = time.time()
        self.pending_ts = None
        self.attention_reason = ""
        self.tool = ""


class StateTracker:
    """增量读取 events.jsonl 并驱动状态机。"""

    def __init__(self):
        self.sessions = {}   # session_id -> SessionState
        self._offset = 0
        self._file_size = 0
        self._load_existing()

    def _load_existing(self):
        """启动时把已有事件全部吃一遍（重建当前状态，不从头显示历史 session）。"""
        if not EVENTS_FILE.exists():
            return
        try:
            with open(EVENTS_FILE) as f:
                lines = f.readlines()
                self._offset = f.tell()
                self._file_size = os.path.getsize(EVENTS_FILE)
        except Exception:
            return
        # 只保留最近 ~2 分钟内的 session 相关事件，避免展示早已关闭的 session
        now = time.time()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = _parse_ts(e.get("ts", ""), now)
            if now - ts > 300:      # >5 分钟前的 session 直接忽略
                continue
            self.apply_event(e)
        # 清理：5 分钟前的旧 session 不显示
        for sid in list(self.sessions.keys()):
            if now - self.sessions[sid].last_event_ts > 300:
                del self.sessions[sid]

    def _read_new_events(self):
        if not EVENTS_FILE.exists():
            return []
        try:
            size = os.path.getsize(EVENTS_FILE)
            if size == self._file_size:
                return []
            with open(EVENTS_FILE) as f:
                f.seek(self._offset)
                lines = f.readlines()
                self._offset = f.tell()
                self._file_size = size
        except Exception:
            return []
        events = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                pass
        return events

    def apply_event(self, e):
        name = e.get("hook_event_name")
        sid = e.get("session_id")
        if not sid or not name:
            return
        ts = _parse_ts(e.get("ts", ""), time.time())

        if name == "SessionStart":
            s = self.sessions.get(sid)
            if s is None:
                s = SessionState(sid, decode_project(e.get("transcript_path", "")))
                self.sessions[sid] = s
            s.last_event_ts = ts
            return
        if name == "SessionEnd":
            self.sessions.pop(sid, None)
            return

        s = self.sessions.get(sid)
        if s is None:
            s = SessionState(sid, decode_project(e.get("transcript_path", "")))
            self.sessions[sid] = s
        s.last_event_ts = ts

        if name == "PermissionRequest":
            s.status = STATUS_ATTENTION
            s.attention_reason = "等待批准"
            s.tool = _get(e, "tool_name", default="")
            return
        if name == "PreToolUse":
            s.status = STATUS_WORKING
            s.attention_reason = ""
            s.tool = _get(e, "tool_name", default="")
            if s.tool == "AskUserQuestion":
                s.status = STATUS_ATTENTION
                s.attention_reason = "等待回答"
                s.pending_ts = None
            else:
                s.pending_ts = ts
            return
        if name == "PostToolUse":
            s.status = STATUS_WORKING
            s.attention_reason = ""
            s.pending_ts = None
            return
        if name == "Stop":
            s.status = STATUS_IDLE
            s.attention_reason = ""
            s.pending_ts = None
            msg = _get(e, "last_assistant_message", default="")
            if msg:
                s.last_message = msg[:200]
            return
        # UserPromptSubmit 等其它事件：视为恢复活动
        if s.status == STATUS_IDLE or s.status == STATUS_ATTENTION:
            s.status = STATUS_WORKING
            s.attention_reason = ""

    def tick(self, now=None):
        """定时检查：长间隔 → attention；超时 → idle。"""
        now = now or time.time()
        for s in self.sessions.values():
            if s.pending_ts is not None and now - s.pending_ts > ATTENTION_GAP:
                if s.status != STATUS_ATTENTION:
                    s.status = STATUS_ATTENTION
                    s.attention_reason = "等待批准/回答"
            elif s.pending_ts is None and s.status == STATUS_WORKING \
                    and now - s.last_event_ts > IDLE_TIMEOUT:
                s.status = STATUS_IDLE

    def update(self):
        """增量读取并应用事件。"""
        for e in self._read_new_events():
            self.apply_event(e)

    def has_attention(self):
        return any(s.status == STATUS_ATTENTION for s in self.sessions.values())
