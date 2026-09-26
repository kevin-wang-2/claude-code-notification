"""两套状态机的离线自测（不依赖真实 Claude/Codex 活动）。

跑法：cd ~/Desktop/claude-notification && ./venv/bin/python tests/selftest.py

cover：
- Claude（state.py）：SessionStart/bridge session、PermissionRequest、PostToolUse、
  Stop、PreToolUse 悬置兜底、SESSION_TTL 移除、agent/title 新字段默认值。
- Codex（codex_state.py）：session_meta(id vs session_id)、task_started、
  工具调用悬置兜底、工具出结果回落、agent_message、task_complete、turn_aborted、
  guardian_review 子线程不出卡片、SESSION_TTL 移除。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import state as st                                    # noqa: E402
from codex_state import CodexTracker                  # noqa: E402

FAILS = 0


def check(label, got, want):
    global FAILS
    ok = got == want
    if not ok:
        FAILS += 1
    print(("  ok   " if ok else "  FAIL ") + f"{label}: {got!r}" + ("" if ok else f" (want {want!r})"))


# ---------------- Claude 侧 ----------------
def test_claude():
    print("Claude state.py")
    def ev(name, sid, ts=None, **kw):
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts or time.time())),
             "hook_event_name": name, "session_id": sid}
        d.update(kw)
        return d

    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    rows = [
        ev("SessionStart", "S1", cwd="/Users/kaibinwa/Desktop/demo",
           transcript_path="/Users/kaibinwa/.claude/projects/-Users-kaibinwa-Desktop-demo/S1.jsonl"),
        ev("SessionStart", "S2", cwd="/Users/kaibinwa/Desktop/bridge"),   # 只 SessionStart
        ev("PreToolUse", "S1", tool_name="Bash"),
        ev("PostToolUse", "S1"),
        ev("Stop", "S1", last_assistant_message="改好了，跑了测试"),
        ev("SessionEnd", "S2"),
    ]
    for r in rows:
        tmp.write(json.dumps(r) + "\n")
    tmp.close()

    orig_events = st.EVENTS_FILE
    st.EVENTS_FILE = Path(tmp.name)          # 只覆盖本进程
    try:
        t = st.StateTracker()
        vis = t.visible_sessions()
        check("visible count（bridge session 不出卡片）", len(vis), 1)
        s = vis[0]
        check("session_id", s.session_id, "S1")
        check("project（SessionStart.cwd 权威）", s.project, "/Users/kaibinwa/Desktop/demo")
        check("status（Stop → idle）", s.status, st.STATUS_IDLE)
        check("last_message", s.last_message, "改好了，跑了测试")
        check("agent 默认值", s.agent, "claude")
        check("title 默认空", s.title, "")

        t.apply_event(ev("PermissionRequest", "S1", tool_name="Bash"))
        check("PermissionRequest → attention", s.status, st.STATUS_ATTENTION)
        check("attention_reason", s.attention_reason, "等待批准")

        t.apply_event(ev("PostToolUse", "S1"))
        check("PostToolUse → working", s.status, st.STATUS_WORKING)

        s.pending_ts = time.time() - 11
        t.tick()
        check("悬置兜底 → attention", s.status, st.STATUS_ATTENTION)

        s.last_event_ts = time.time() - st.SESSION_TTL - 1
        t.tick()
        check("TTL → 移除", "S1" in t.sessions, False)
    finally:
        st.EVENTS_FILE = orig_events
        os.unlink(tmp.name)


# ---------------- Codex 侧 ----------------
class _Tail:
    sid = None


def cdx(typ, payload, ts=None):
    ts = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"timestamp": ts, "type": typ, "payload": payload}


def test_codex():
    print("Codex codex_state.py")
    # 不开真实文件扫描，直接构造空 tracker 后喂事件
    t = CodexTracker.__new__(CodexTracker)
    t.sessions, t._tails, t._ignored = {}, {}, set()
    t._thread_names, t._names_mtime = {}, None
    tail = _Tail()
    now = time.time()

    t.apply_record(cdx("session_meta", {"id": "S1", "session_id": "S1", "cwd": "/p/proj",
                                        "thread_source": "user", "source": "vscode"}), tail, now)
    s = t.sessions["S1"]
    check("meta: project", s.project, "/p/proj")
    check("meta: agent", s.agent, "codex")
    check("meta: activated", s.activated, False)
    check("meta: visible", t.visible_sessions(), [])

    t.apply_record(cdx("event_msg", {"type": "task_started", "thread_id": "S1"}), tail, now)
    check("started: status", s.status, st.STATUS_WORKING)
    check("started: activated", s.activated, True)

    t.apply_record(cdx("response_item", {"type": "custom_tool_call", "name": "exec",
                                         "thread_id": "S1"}), tail, now)
    check("tool: pending", s.pending_ts is not None, True)
    t.tick(now + 11)
    check("tool: attention", s.status, st.STATUS_ATTENTION)

    t.apply_record(cdx("response_item", {"type": "custom_tool_call_output",
                                         "thread_id": "S1"}), tail, now)
    check("output: status", s.status, st.STATUS_WORKING)
    check("output: pending", s.pending_ts, None)

    t.apply_record(cdx("event_msg", {"type": "agent_message", "message": "正在实现表格",
                                     "thread_id": "S1"}), tail, now)
    check("agent_message", s.last_message, "正在实现表格")

    t.apply_record(cdx("event_msg", {"type": "task_complete",
                                     "last_agent_message": "做完了，改了 3 个文件",
                                     "thread_id": "S1"}), tail, now)
    check("complete: status", s.status, st.STATUS_IDLE)
    check("complete: msg", s.last_message, "做完了，改了 3 个文件")

    t.apply_record(cdx("event_msg", {"type": "turn_aborted", "reason": "interrupted",
                                     "thread_id": "S1"}), tail, now)
    check("aborted: note", s.status_note, "已中断")

    t.apply_record(cdx("session_meta", {"id": "G1", "session_id": "S1", "cwd": "/p/proj",
                                        "thread_source": "guardian_review",
                                        "source": {"subagent": {"other": "guardian"}}}), tail, now)
    t.apply_record(cdx("event_msg", {"type": "task_started", "thread_id": "G1"}), tail, now)
    check("guardian 子线程：不出卡片", "G1" in t.sessions, False)

    t.tick(now + 3600)
    check("TTL → 移除", "S1" in t.sessions, False)


if __name__ == "__main__":
    test_claude()
    test_codex()
    print("FAILED" if FAILS else "ALL OK")
    sys.exit(1 if FAILS else 0)
