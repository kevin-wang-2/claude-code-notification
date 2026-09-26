"""Claude 多 Session 状态机 — 读 ~/.claude/status/events.jsonl，维护每个 session 的状态。

状态机（2026-08-05 实测定案）：
- PermissionRequest → needs_attention（直接信号，弹框即触发）
- AskUserQuestion   → needs_attention（立即）
- PreToolUse 后 10s+ 无 PostToolUse → needs_attention（兜底）
- PostToolUse → working（刷新）
- Stop → idle（附 last_assistant_message）
- SessionEnd → 移除
- 15 分钟无任何事件 → idle（纯兜底，防 Stop 丢失；正常思考/长回复不误判）
- 30 分钟无任何事件 → 直接删除（兜底 SessionEnd 丢失，防永久假卡）

只有 SessionStart、之后从未有过任何活动的 session 不出卡片（见 activated）：
/clear 会连开 2~3 个 session id，其中的 bridge session 只承载 /clear 这条命令
本身，十几秒后就结束，从不发 UserPromptSubmit / 工具事件。
"""
import json
import os
import time
import datetime
from pathlib import Path

EVENTS_FILE = Path.home() / ".claude" / "status" / "events.jsonl"

ATTENTION_GAP = 10.0    # PreToolUse 悬置超过此秒数 → needs_attention
ZOMBIE_TIMEOUT = 900.0  # 无任何事件超过此秒数 → idle（纯兜底，防 Stop 事件丢失；
                        # 不能设太短——Claude 思考/长回复时可能长时间不调工具）
RECENT_WINDOW = 300.0   # 只处理最近 N 秒内的事件（重读/启动回放时防死 session 复活）
SESSION_TTL = 1800.0    # 无任何事件超过此秒数 → 移除 session。
                        # 兜底：SessionEnd 若丢失（进程被 kill、hook 失败），
                        # 卡片不会永久挂着。必须 > ZOMBIE_TIMEOUT。

STATUS_WORKING = "working"
STATUS_IDLE = "idle"
STATUS_ATTENTION = "needs_attention"

STATUS_LABEL = {
    STATUS_WORKING: "工作中",
    STATUS_IDLE: "空闲",
    STATUS_ATTENTION: "需关注",
}


def tick_sessions(sessions, now=None):
    """两套 tracker（Claude hooks / Codex rollout）共用的兜底状态推进。

    - pending 悬置超过 ATTENTION_GAP → needs_attention（漏了批准事件时的兜底）
    - 无 pending 但 working 静默超过 ZOMBIE_TIMEOUT → idle（防完成事件丢失）
    - 静默超过 SESSION_TTL → 移除（防会话结束时卡片永久挂着）
    """
    now = now or time.time()
    for sid in [sid for sid, s in sessions.items()
                if now - s.last_event_ts > SESSION_TTL]:
        del sessions[sid]
    for s in sessions.values():
        if s.pending_ts is not None and now - s.pending_ts > ATTENTION_GAP:
            if s.status != STATUS_ATTENTION:
                s.status = STATUS_ATTENTION
                s.attention_reason = "等待批准/回答"
        elif s.pending_ts is None and s.status == STATUS_WORKING \
                and now - s.last_event_ts > ZOMBIE_TIMEOUT:
            s.status = STATUS_IDLE


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


def _is_recent(e, now):
    ts = _parse_ts(e.get("ts", ""), now)
    return now - ts <= RECENT_WINDOW


class SessionState:
    __slots__ = ("session_id", "project", "status", "last_message",
                 "last_event_ts", "pending_ts", "attention_reason", "tool",
                 "status_note", "activated", "agent", "title")

    def __init__(self, session_id, project):
        self.session_id = session_id
        self.project = project
        self.status = STATUS_IDLE
        self.last_message = ""
        self.last_event_ts = time.time()
        self.pending_ts = None
        self.attention_reason = ""
        self.tool = ""
        self.status_note = ""   # idle 变体说明："已中断" / "API 错误…"
        self.activated = False  # SessionStart 之后是否有过真实活动；False = 不出卡片
        self.agent = "claude"   # 事件来源："claude"（本文件）/ "codex"（codex_state.py）
        self.title = ""         # 可选显示名（Codex 的 thread_name；Claude 侧留空）


class StateTracker:
    """增量读取 events.jsonl 并驱动状态机。"""

    def __init__(self):
        self.sessions = {}   # session_id -> SessionState
        self._start_cwd = {}   # session_id -> 权威初始 cwd（来自 SessionStart）
        self._offset = 0
        self._file_size = 0
        self._load_existing()

    def _load_existing(self):
        """启动时把已有事件全部吃一遍（重建当前状态，不从头显示历史 session）。

        SessionStart 的 cwd 是权威项目路径，不受时间窗口限制（否则重启后
        老 session 会回退到不可靠的 decode）；只有"活跃性"按最近事件过滤。
        """
        if not EVENTS_FILE.exists():
            return
        try:
            with open(EVENTS_FILE) as f:
                lines = f.readlines()
                self._offset = f.tell()
                self._file_size = os.path.getsize(EVENTS_FILE)
        except Exception:
            return
        # 第一遍：收集所有 SessionStart 的 cwd（无论多久以前）
        start_cwd = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("hook_event_name") == "SessionStart" and e.get("cwd"):
                start_cwd[e.get("session_id")] = e["cwd"]
        # 缓存权威 cwd：运行期间增量事件创建 session 时也要用（decode 对含 '-' 路径不可逆）
        self._start_cwd = start_cwd
        # 第二遍：回放最近窗口内的活跃事件
        now = time.time()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not _is_recent(e, now):
                continue
            self.apply_event(e)
        # 第三遍：用 SessionStart.cwd 修正 project（decode 只作无 SessionStart 时兑底）
        for sid, s in self.sessions.items():
            cwd = start_cwd.get(sid)
            if cwd:
                s.project = cwd.rstrip("/")
        # 清理：窗口外的旧 session 不显示
        for sid in list(self.sessions.keys()):
            if now - self.sessions[sid].last_event_ts > RECENT_WINDOW:
                del self.sessions[sid]

    def _read_new_events(self):
        if not EVENTS_FILE.exists():
            return []
        try:
            size = os.path.getsize(EVENTS_FILE)
            # 文件被轮转/截断/清理（变小）→ 从头重新读；
            # 重读全文件必须套用时间窗口，否则会复活早已结束的死 session
            if size < self._file_size or size < self._offset:
                self._offset = 0
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
        now = time.time()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not _is_recent(e, now):
                continue
            events.append(e)
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
            # SessionStart 的 cwd = 初始工作目录 = 项目路径（权威）
            # 注意：transcript_path 编码（/→-）对含 '-' 的路径不可逆（如 click-in），
            # decode 只能作无 SessionStart 时的兑底
            cwd = e.get("cwd")
            if cwd:
                s.project = cwd.rstrip("/")
                self._start_cwd[sid] = s.project   # 增量 SessionStart 也更新缓存
            s.last_event_ts = ts
            return
        if name == "SessionEnd":
            self.sessions.pop(sid, None)
            return

        s = self.sessions.get(sid)
        if s is None:
            cwd = self._start_cwd.get(sid)
            if cwd:
                s = SessionState(sid, cwd)   # 权威 cwd 优先，decode 只作兑底
            else:
                s = SessionState(sid, decode_project(e.get("transcript_path", "")))
            self.sessions[sid] = s
        s.last_event_ts = ts
        # 能走到这里的都是 SessionStart/SessionEnd 之外的事件 = 真实活动，
        # bridge session 永远到不了这行
        s.activated = True

        if name == "PermissionRequest":
            s.status = STATUS_ATTENTION
            s.attention_reason = "等待批准"
            s.tool = _get(e, "tool_name", default="")
            s.status_note = ""
            return
        if name == "PreToolUse":
            s.status = STATUS_WORKING
            s.attention_reason = ""
            s.status_note = ""
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
            s.status_note = ""
            s.pending_ts = None
            return
        if name == "PostToolUseFailure":
            # 工具执行结束（无论成败），清除悬置，避免误判 needs_attention
            s.pending_ts = None
            if _get(e, "is_interrupt", default=False):
                # 用户中断了工具执行 → 停止，标记"已中断"
                s.status = STATUS_IDLE
                s.attention_reason = ""
                s.status_note = "已中断"
            else:
                # 工具报错，Claude 通常会换方法继续 → 保持工作中
                s.status = STATUS_WORKING
                s.attention_reason = ""
                s.status_note = ""
            return
        if name == "StopFailure":
            # 因 API 错误结束（rate_limit 等）→ 异常停止
            s.status = STATUS_IDLE
            s.pending_ts = None
            s.attention_reason = ""
            err = _get(e, "error", default="")
            s.status_note = f"API 错误：{err}" if err else "API 错误"
            return
        if name == "Stop":
            s.status = STATUS_IDLE
            s.attention_reason = ""
            s.status_note = ""
            s.pending_ts = None
            msg = _get(e, "last_assistant_message", default="")
            if msg:
                s.last_message = msg[:200]
            return
        if name == "Notification":
            # Claude Code 主动发系统通知 = 在等用户，绝不是工作信号。
            # 只在 working 时翻红（漏掉权限事件的兜底）；idle 时的
            # "waiting for input" 通知属正常闲置，不翻红。
            if s.status == STATUS_WORKING:
                s.status = STATUS_ATTENTION
                s.attention_reason = "等待输入"
                s.status_note = ""
            return
        if name in ("MessageDisplay", "PermissionDenied"):
            # MessageDisplay：Claude 正在流式输出文本 = 确实在干活
            #   （解决：拒绝 tool use 后无事件，状态卡在"等待批准"）
            # PermissionDenied：权限被拒后 Claude 继续处理 → 恢复工作中
            if s.status != STATUS_WORKING:
                s.status = STATUS_WORKING
                s.attention_reason = ""
                s.status_note = ""
            return
        # UserPromptSubmit 等其它事件：视为恢复活动
        if s.status == STATUS_IDLE or s.status == STATUS_ATTENTION:
            s.status = STATUS_WORKING
            s.attention_reason = ""
            s.status_note = ""

    def tick(self, now=None):
        """定时检查：长间隔 → attention；僵尸超时 → idle；彻底静默 → 移除。

        idle 以 Stop 事件为准（一轮结束）；此处超时只是最后防线，
        防止 Stop 事件丢失导致 session 永远挂着 working。
        注意不要设太短：Claude 思考中/生成长回复时不产生工具事件。

        移除以 SessionEnd 为准；SESSION_TTL 只是兜底——SessionEnd 一旦丢失
        （窗口被强杀、hook 写入失败），卡片否则会永久挂在浮窗上。

        Codex 侧（codex_state.py）语义相同，共用下面的 tick_sessions。
        """
        tick_sessions(self.sessions, now)

    def update(self):
        """增量读取并应用事件。"""
        for e in self._read_new_events():
            self.apply_event(e)

    def visible_sessions(self):
        """出卡片的 session：SessionStart 之后必须有过真实活动。

        /clear 会连开 2~3 个 session id —— 旧 session 收尾、一个只承载 /clear
        这条命令的 bridge session（transcript 里 type=bridge-session，十几秒后
        SessionEnd）、以及真正的新 session。三者都发 SessionStart，若见之即建卡，
        同一项目会瞬间冒出多张重复卡；bridge 的 SessionEnd 还常被攒着批量补发，
        期间那张假卡一直挂着。bridge 从不发 UserPromptSubmit / 工具事件，
        以"有过活动"为准即可滤掉。
        """
        return [s for s in self.sessions.values() if s.activated]

    def has_attention(self):
        return any(s.status == STATUS_ATTENTION for s in self.visible_sessions())
