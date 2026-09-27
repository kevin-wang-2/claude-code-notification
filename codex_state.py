"""Codex session 状态机 — 增量读 ~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl。

与 state.py（Claude Code：hook 写 events.jsonl）并列，输出同一种 SessionState，
由 main.py 合并渲染。Codex 没有 Claude 那种 hooks，但它把一条 turn 的每个事件
都落进 rollout jsonl，所以直接 tail 这个文件就能还原状态。

实测格式（codex 0.155.0-alpha.16.3 / VS Code 扩展，2026-09-26）：
  {"timestamp":"2026-09-26T01:57:37.600Z","type":"session_meta","payload":{...}}
  type: session_meta / event_msg / response_item / turn_context /
        token_usage_record / world_state / compacted
  event_msg.payload.type: task_started / task_complete / turn_aborted /
        user_message / agent_message / agent_reasoning / token_count /
        item_completed / patch_apply_end / web_search_end / thread_settings_applied
  response_item.payload.type: message / reasoning / custom_tool_call /
        custom_tool_call_output / function_call / function_call_output

要点：
- **两条数据路径**：① `~/.codex/status/events.jsonl`（hooks 事件，配好并信任
  `~/.codex/hooks.json` 后由 hook 脚本实时写入）——事件驱动，**优先**；
  ② `~/.codex/sessions/**/rollout-*.jsonl`（每条 turn 的全量事件）——兜底，
  未信任 hooks 的会话、或早期历史会话靠它。两者共用同一份 sessions，顺序上
  hooks 后应用（更权威）。
- 目录与文件名用**本地时间**（rollout-2026-09-26T09-56-29-…），行内 timestamp 是
  UTC（…Z）→ 一律用行内 timestamp 算时间，文件名只用来找文件；hook 事件文件
  则和 Claude 侧一致用本地时间。
- 一条 rollout 里可能出现多个 session_meta（resume/rollover），事件按 payload.
  thread_id 归属；文件级兜底用"最后一次见到的 session_meta"。
- session_meta 里 **id = 本线程 id，session_id 在子线程里是父线程 id**（实测
  guardian_review 子线程：id=子线程、session_id=父线程）→ 一律用 id。
  thread_source != "user" / source 是 dict 的线程是内部审查子线程，不出卡片。
- 没有"等待批准"的显式事件（VS Code 侧 approvals_reviewer=auto_review）：
  用与 Claude 相同的兜底启发式 —— 工具调用发出后 ATTENTION_GAP 秒没有输出
  → needs_attention。代价是长命令（npm test 之类）也会误报，与 Claude 侧一致。

状态映射（2026-09-26 实测）：
| 信号 | 状态 |
| task_started / user_message / agent_message / agent_reasoning | working |
| token_count / patch_apply_end / web_search_end / thread_settings_applied | working（心跳） |
| response_item: reasoning / *_tool_call | working（工具调用记 pending） |
| response_item: *_tool_call_output / item_completed(CommandExecution/FileChange) | working（清 pending） |
| task_complete | idle（附 last_agent_message 前 200 字） |
| turn_aborted | idle，status_note="已中断" |
| 工具调用 pending > 10s（无 hook 的会话）/ 90s（有 hook 的会话） | needs_attention（兜底） |
| 工作中静默 > 3 分钟 | idle（关掉窗口后不再长时间装绿） |
| 静默 > 10 分钟 | 删卡 |
| 右键“删除此会话” | 立即摘卡，但**下次再有事件会自动回来**（见 state.Dismissed） |

hook 路径另有两条**直接信号**（比轮询准）：`PermissionRequest` → 🔴 等待批准；
`SessionEnd` → 立即下卡（不用等 TTL 兜底）。

死 session 清理：Codex **没有"会话结束"事件**（只有 turn 级的 task_complete /
turn_aborted），所以只能靠超时。阈值比 Claude 侧紧（Claude 靠 SessionEnd 立即删卡，
30 分钟只是兜底）：用户习惯"一个任务开一个会话、开完就关"，30 分钟会把已放弃的
卡片挂太久。三个值都在本文件顶部 CODEX_* 常量里。
"""
import datetime
import glob
import json
import os
import time
from pathlib import Path

from state import (ATTENTION_GAP, STATUS_ATTENTION, STATUS_IDLE,
                   STATUS_WORKING, Dismissed, SessionState, tick_sessions)

CODEX_DIR = Path.home() / ".codex"
SESSIONS_DIR = CODEX_DIR / "sessions"        # sessions/<Y>/<M>/<D>/rollout-*.jsonl
INDEX_FILE = CODEX_DIR / "session_index.jsonl"   # {"id":..,"thread_name":..,"updated_at":..}

# Codex 也有一套 Claude Code 风格的 hooks（hooks.json，事件同名）。配好后事件会
# 被 hook 脚本实时写到这里 —— 这是**事件驱动**的路径，优先于轮询 rollout。
# 没信任/没配 hook 的会话仍然靠 rollout 兜底（两条都会跑，hooks 最后应用=更权威）。
HOOK_EVENTS_FILE = CODEX_DIR / "status" / "events.jsonl"
INTERNAL_AGENT_TYPES = {"subagent", "guardian", "review", "auto_review",
                        "guardian_review", "guardian_v2"}
# 内部工作会话（不出卡片）：
#  - memory 会话：cwd=~/.codex/memories，由 memories_1.sqlite 的 jobs 驱动，
#    **不写 rollout、不进 threads 表**，只在 hook 里露头（实测 2026-09-27）。
#  - 其它 cwd 落在 CODEX_HOME 下的 worker 同理。
INTERNAL_CWD_PREFIXES = (str(CODEX_DIR) + os.sep,)

# 死 session 清理阈值（Codex 无结束事件，只能超时清）——
# 比 Claude 侧（ZOMBIE 15min / TTL 30min）紧，见模块 docstring。
CODEX_ACTIVE_WINDOW = 600.0    # 启动回放/重读时，只认最近这么久内的活动（与删卡阈值同量级）
CODEX_ZOMBIE_TIMEOUT = 180.0   # 工作中静默 → 空闲（3 分钟）
CODEX_SESSION_TTL = 600.0      # 静默 → 删卡（10 分钟）
# 有 hook 事件覆盖的会话："等待批准"有 PermissionRequest 这个**直接信号**，所以
# 悬置兜底可以放松得多（否则 `clocksleep` / npm test 这类长工具会反复误报红）。
# 没被 hook 覆盖的会话（CLI 直跑、未信任）仍用调用方的默认值（10s）。
CODEX_HOOK_PENDING_GAP = 90.0

# event_msg.payload.type 里表示"正在干活"的
WORKING_EVENTS = {
    "task_started", "user_message", "agent_message", "agent_reasoning",
    "token_count", "patch_apply_end", "web_search_end", "thread_settings_applied",
}
# item_completed.item.type 里表示"工具/步骤结束"的（清 pending）
TOOL_ITEM_TYPES = {"CommandExecution", "FileChange", "Reasoning"}


def _parse_ts(ts_str, fallback):
    """行内 timestamp（ISO8601，UTC 'Z'）→ epoch 秒。"""
    try:
        return datetime.datetime.fromisoformat(
            str(ts_str).replace("Z", "+00:00")).timestamp()
    except Exception:
        return fallback


def _parse_local_ts(ts_str, fallback):
    """hook 事件文件的 ts 是本地无时区 ISO 串（和 Claude 侧一致）。"""
    try:
        return datetime.datetime.fromisoformat(str(ts_str)).timestamp()
    except Exception:
        return fallback


def _is_internal_hook(agent_type):
    """hook 记录是不是内部子线程（guardian/审查）发的。

    未知值一律当用户线程（宁多一张卡，也别把真会话滤没）。
    """
    if not agent_type:
        return False
    a = str(agent_type).strip().lower()
    return a in INTERNAL_AGENT_TYPES or "subagent" in a or "guardian" in a


def _is_internal_cwd(cwd):
    """cwd 在 CODEX_HOME 下 = Codex 自己的内部工作目录（memory 等），不出卡。"""
    if not cwd:
        return False
    c = str(cwd).rstrip("/")
    return any(c == p.rstrip("/") or c.startswith(p) for p in INTERNAL_CWD_PREFIXES)


def load_thread_names():
    """~/.codex/session_index.jsonl → {thread_id: thread_name}，让卡片标题更好认。"""
    names = {}
    try:
        with open(INDEX_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                tid = e.get("id")
                if tid and e.get("thread_name"):
                    names[tid] = e["thread_name"]
    except Exception:
        pass
    return names


def _path_from_uri(uri):
    """file:///Users/... → 本地路径（codex 的 item.cwd 是 URI 形式）。"""
    if not isinstance(uri, str):
        return None
    if uri.startswith("file://"):
        from urllib.parse import unquote, urlparse
        p = urlparse(uri)
        if p.scheme != "file":
            return None
        return unquote(p.path).rstrip("/") or None
    return None


class _Tail:
    """一个 rollout 文件的读取游标。"""

    __slots__ = ("offset", "buf", "sid")

    def __init__(self):
        self.offset = 0     # 已消费到的字节偏移
        self.buf = ""       # 半个 JSON 行的残留（文件正在被追加写）
        self.sid = None     # 本文件最近一次 session_meta 的 id（事件无 thread_id 时的兜底）


class CodexTracker:
    """增量读 rollout 文件并驱动与 Claude 侧同款的状态机。"""

    def __init__(self, dismissals=None):
        self.sessions = {}          # session_id -> SessionState
        self._tails = {}            # path -> _Tail
        self._ignored = set()       # 内部子线程（guardian_review 等）的 thread_id，不出卡片
        self.dismissals = dismissals or Dismissed()
        self._thread_names = load_thread_names()
        self._names_mtime = self._index_mtime()
        self._hook_offset = 0       # hook 事件文件读取游标
        self._hook_size = 0
        self._load_existing()

    # ---------- 文件发现 ----------
    @staticmethod
    def _index_mtime():
        try:
            return os.path.getmtime(INDEX_FILE)
        except OSError:
            return None

    def _candidate_files(self):
        """今/昨两天的 rollout 文件（目录按本地日期分；跨零点的 session 在昨天目录）。"""
        out = []
        today = datetime.date.today()
        for d in (today, today - datetime.timedelta(days=1)):
            pat = SESSIONS_DIR / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / "rollout-*.jsonl"
            out.extend(glob.glob(str(pat)))
        return out

    def _maybe_reload_names(self):
        m = self._index_mtime()
        if m and m != self._names_mtime:
            self._thread_names = load_thread_names()
            self._names_mtime = m
            for sid, s in self.sessions.items():
                s.title = self._thread_names.get(sid, s.title)

    # ---------- 初始回放 ----------
    def _load_existing(self):
        """启动时把近期文件的已有内容吃一遍，重建当前状态。

        只回放最近窗口内的事件（否则重启后老 session 会全体复活）；session_meta /
        turn_context 里的 cwd 不受窗口限制 —— 它们是"这个 session 属于哪个项目"
        的权威来源。CODEX_SESSION_TTL 之外的旧文件直接跳过（连兜底移除都过了，
        不可能出卡片）。
        """
        now = time.time()
        for path in self._candidate_files():
            try:
                if now - os.path.getmtime(path) > CODEX_SESSION_TTL:
                    continue
            except OSError:
                continue
            tail = self._tails.setdefault(path, _Tail())
            self._consume(path, tail, now)
        # hook 事件文件（若已配并信任 hooks）先读进来，再跑一次 rollout，两边共用
        # 同一份 sessions；顺序上 hooks 后应用 = 更权威（见 update()）
        self._apply_hook_records(self._read_hook_events(), now)
        for sid in [sid for sid, s in self.sessions.items()
                    if now - s.last_event_ts > CODEX_ACTIVE_WINDOW]:
            del self.sessions[sid]

    # ---------- 增量读 ----------
    def update(self):
        now = time.time()
        self._maybe_reload_names()
        for path in self._candidate_files():
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            tail = self._tails.get(path)
            if tail is None:
                if now - mtime > CODEX_SESSION_TTL:
                    continue
                tail = self._tails[path] = _Tail()
            self._consume(path, tail, now)
        # 后应用 hook 事件：配了 hooks 的会话以它为准（rollout 只当兜底）
        self._apply_hook_records(self._read_hook_events(), now)

    def _consume(self, path, tail, now):
        try:
            size = os.path.getsize(path)
            if size < tail.offset:      # 被截断/重建 → 从头
                tail.offset = 0
                tail.buf = ""
            if size == tail.offset:
                return
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(tail.offset)
                chunk = f.read()
                tail.offset = f.tell()
        except Exception:
            return
        data = tail.buf + chunk
        lines = data.split("\n")
        tail.buf = lines.pop()          # 末尾可能是不完整的一行，留到下次
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            self.apply_record(e, tail, now)

    # ---------- 事件 → 状态 ----------
    def _ensure(self, sid, cwd, ts):
        s = self.sessions.get(sid)
        if s is None:
            s = SessionState(sid, (cwd or "?").rstrip("/"))
            s.agent = "codex"
            s.title = self._thread_names.get(sid, "")
            s.last_event_ts = ts
            self.sessions[sid] = s
        elif cwd:
            s.project = cwd.rstrip("/")
        return s

    def apply_record(self, e, tail, now):
        typ = e.get("type")
        p = e.get("payload")
        if not isinstance(p, dict):
            p = {}
        ts = _parse_ts(e.get("timestamp"), now)

        if typ == "session_meta":
            # id = 本线程自己的 id；session_id 在子线程里是**父线程**的 id
            # （guardian_review 子线程实测：id=子线程, session_id=父线程）
            sid = p.get("id") or p.get("session_id")
            if not sid:
                return
            tail.sid = sid
            # 内部子线程（approval/guardian 审查）不是用户 session，不出卡片：
            # 父线程此刻本来就处于 working/等批准，再挂一张"?"卡是噪音
            if p.get("thread_source") not in (None, "user") \
                    or isinstance(p.get("source"), dict):
                self._ignored.add(sid)
                self.sessions.pop(sid, None)
                return
            cwd = p.get("cwd") or (p.get("runtime_workspace_roots") or [None])[0]
            if _is_internal_cwd(cwd):
                self._ignored.add(sid)      # 内部工作目录（~/.codex/**）
                self.sessions.pop(sid, None)
                return
            self._ensure(sid, cwd, ts)
            return

        sid = p.get("thread_id") or p.get("session_id") or tail.sid
        if not sid or sid in self._ignored:
            return

        # 补 cwd：turn_context / CommandExecution 里也带（比 session_meta 更新）
        cwd = p.get("cwd")
        if not cwd and p.get("item"):
            cwd = _path_from_uri((p.get("item") or {}).get("cwd"))
        if _is_internal_cwd(cwd):
            self._ignored.add(sid)
            self.sessions.pop(sid, None)
            return
        if typ == "turn_context" and cwd:
            self._ensure(sid, cwd, ts)
            return

        s = self._ensure(sid, cwd, ts)
        if _is_internal_cwd(s.project):     # 已建过卡的内部会话（cwd 后到）
            self._ignored.add(sid)
            self.sessions.pop(sid, None)
            return

        # 窗口外的事件：只用来补 cwd/标题，不参与状态（否则重启后死 session 复活）
        if now - ts > CODEX_ACTIVE_WINDOW:
            return

        if typ == "event_msg":
            self._apply_event_msg(s, p, ts)
        elif typ == "response_item":
            self._apply_response_item(s, p)

    def _apply_event_msg(self, s, p, ts):
        name = p.get("type")
        s.last_event_ts = ts
        s.activated = True

        if name == "task_complete":
            s.status = STATUS_IDLE
            s.attention_reason = ""
            s.status_note = ""
            s.pending_ts = None
            msg = p.get("last_agent_message") or ""
            if msg:
                s.last_message = msg[:200]
            return
        if name == "turn_aborted":
            s.status = STATUS_IDLE
            s.attention_reason = ""
            s.status_note = "已中断"
            s.pending_ts = None
            return
        if name == "agent_message":
            s.status = STATUS_WORKING
            s.attention_reason = ""
            s.status_note = ""
            msg = p.get("message") or ""
            if msg:
                s.last_message = msg[:200]
            return
        if name == "item_completed":
            item = p.get("item") or {}
            if item.get("type") in TOOL_ITEM_TYPES:
                # 工具/步骤收尾 → 清 pending，并从"等待批准"回落到工作中
                s.pending_ts = None
                s.status = STATUS_WORKING
                s.attention_reason = ""
                s.status_note = ""
            elif s.status == STATUS_IDLE:
                s.status = STATUS_WORKING
                s.status_note = ""
            return
        if name in WORKING_EVENTS:
            # token_count 等心跳：只在非 attention 时回落到 working，
            # 免得刚翻红的"等待批准"被心跳洗掉
            if s.status != STATUS_ATTENTION:
                s.status = STATUS_WORKING
                s.attention_reason = ""
                s.status_note = ""
            return
        # 兜底：未知 event_msg 也当作活动
        if s.status == STATUS_IDLE or s.status == STATUS_ATTENTION:
            s.status = STATUS_WORKING
            s.attention_reason = ""
            s.status_note = ""

    def _apply_response_item(self, s, p):
        ptype = p.get("type")
        if ptype in ("custom_tool_call", "function_call"):
            s.status = STATUS_WORKING
            s.attention_reason = ""
            s.status_note = ""
            s.pending_ts = time.time()      # 工具调用发出 → 等输出
            return
        if ptype in ("custom_tool_call_output", "function_call_output"):
            # 工具出结果 = 工具执行完毕（也可能是刚批准并跑完）→ 无条件回工作中
            s.pending_ts = None
            s.status = STATUS_WORKING
            s.attention_reason = ""
            s.status_note = ""
            return
        if ptype == "reasoning":
            if s.status != STATUS_ATTENTION:
                s.status = STATUS_WORKING
                s.status_note = ""
            return
        # message / compaction 等：视为活动
        if s.status == STATUS_IDLE or s.status == STATUS_ATTENTION:
            s.status = STATUS_WORKING
            s.attention_reason = ""
            s.status_note = ""

    # ---------- hook 事件（事件驱动路径） ----------
    def _read_hook_events(self):
        if not HOOK_EVENTS_FILE.exists():
            return []
        try:
            size = os.path.getsize(HOOK_EVENTS_FILE)
            if size < self._hook_size or size < self._hook_offset:
                self._hook_offset = 0      # 被轮转/清理（hook_logger 会按 session 裁剪）
            if size == self._hook_size:
                return []
            with open(HOOK_EVENTS_FILE, encoding="utf-8", errors="replace") as f:
                f.seek(self._hook_offset)
                lines = f.readlines()
                self._hook_offset = f.tell()
                self._hook_size = size
        except Exception:
            return []
        now = time.time()
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = _parse_local_ts(e.get("ts"), now)
            if now - ts > CODEX_ACTIVE_WINDOW:
                continue
            e["_ts"] = ts
            out.append(e)
        return out

    def _apply_hook_records(self, records, now):
        for e in records:
            self.apply_hook_record(e, now)

    def apply_hook_record(self, e, now=None):
        """hook 事件 → 状态。事件名/语义与 Claude 侧对齐（SessionStart/PreToolUse/
        PermissionRequest/PostToolUse/Stop/SessionEnd/Interrupt）。

        比轮询 rollout 多两个直接信号：**PermissionRequest = 等待批准**（不用再靠
        10s 悬置猜）、**SessionEnd = 立即下卡**（不用等 10 分钟 TTL）。
        """
        name = e.get("hook_event_name")
        sid = e.get("session_id")
        now = now or time.time()
        if not name or not sid:
            return
        ts = e.get("_ts") or now
        if _is_internal_hook(e.get("agent_type")) or _is_internal_cwd(e.get("cwd")):
            self._ignored.add(sid)
            self.sessions.pop(sid, None)
            return
        if sid in self._ignored:
            return

        s = self._ensure(sid, e.get("cwd"), ts)
        s.last_event_ts = ts
        s.hook_seen = True
        s.pending_gap = CODEX_HOOK_PENDING_GAP

        if name == "SessionStart":
            # 与 Claude 侧同口径：SessionStart 之后有真实活动才出卡
            s.activated = False
            return
        if name == "SessionEnd":
            self.sessions.pop(sid, None)
            return

        s.activated = True
        if name == "PermissionRequest":
            s.status = STATUS_ATTENTION
            s.attention_reason = "等待批准"
            s.tool = e.get("tool_name") or s.tool
            s.status_note = ""
            s.pending_ts = None
            return
        if name == "Interrupt":
            s.status = STATUS_IDLE
            s.attention_reason = ""
            s.status_note = "已中断"
            s.pending_ts = None
            return
        if name == "Stop":
            s.status = STATUS_IDLE
            s.attention_reason = ""
            s.status_note = ""
            s.pending_ts = None
            msg = e.get("last_assistant_message") or e.get("message") or ""
            if msg:
                s.last_message = str(msg)[:200]
            return
        if name == "PreToolUse":
            s.status = STATUS_WORKING
            s.attention_reason = ""
            s.status_note = ""
            s.tool = e.get("tool_name") or s.tool
            s.pending_ts = ts
            return
        if name == "PostToolUse":
            s.status = STATUS_WORKING
            s.attention_reason = ""
            s.status_note = ""
            s.pending_ts = None
            return
        # UserPromptSubmit / PreCompact / PostCompact 及其它：视为活动
        s.status = STATUS_WORKING
        s.attention_reason = ""
        s.status_note = ""
        s.pending_ts = None

    # ---------- 对外 ----------
    def tick(self, now=None):
        """Codex 专用阈值（比 Claude 紧）：见文件顶部 CODEX_* 常量。

        hook 覆盖的会话的悬置阈值由 SessionState.pending_gap 单独给（90s）。
        """
        tick_sessions(self.sessions, now,
                      zombie_timeout=CODEX_ZOMBIE_TIMEOUT,
                      session_ttl=CODEX_SESSION_TTL)

    def visible_sessions(self):
        """出卡片的 session；同样滤掉右键“删除”（静音到下次活动）。"""
        return [s for s in self.sessions.values()
                if s.activated
                and not self.dismissals.is_dismissed(s.session_id, s.last_event_ts)]

    def dismiss(self, sid):
        """右键删除：记阈值 + 立即摘卡；同一线程再有事件自动重新出卡。"""
        s = self.sessions.get(sid)
        if s is None:
            return False
        self.dismissals.add(sid, max(s.last_event_ts, time.time()))
        self.sessions.pop(sid, None)
        return True

    def has_attention(self):
        return any(s.status == STATUS_ATTENTION for s in self.visible_sessions())


if __name__ == "__main__":
    # 自检：python codex_state.py  → 打印当前 Codex session 状态
    t = CodexTracker()
    t.update()
    t.tick()
    rows = sorted(t.visible_sessions(), key=lambda s: -s.last_event_ts)
    print(f"codex sessions: {len(rows)}")
    for s in rows:
        age = int(time.time() - s.last_event_ts)
        print(f"  [{s.status:15s}] {s.project}  sid={s.session_id[:13]} "
              f"age={age}s tool={s.tool!r} note={s.status_note!r}")
        if s.last_message:
            print(f"       msg: {s.last_message[:80]}")
