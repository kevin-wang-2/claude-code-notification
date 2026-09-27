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
from codex_state import (CODEX_HOOK_PENDING_GAP, CODEX_SESSION_TTL,
                         CODEX_ZOMBIE_TIMEOUT, CodexTracker)      # noqa: E402

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

        # Claude 侧阈值仍是 zombie=900s / ttl=1800s：10 分钟不该删（Codex 侧才删）
        s.pending_ts = None
        s.status = st.STATUS_WORKING
        s.last_event_ts = time.time()
        t.tick(time.time() + CODEX_SESSION_TTL + 5)
        check("Claude: 10min 静默仍保留（30min 才删）", "S1" in t.sessions, True)

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

    # Codex 专用阈值：工作中静默 3 分钟 → 空闲；静默 10 分钟 → 删卡
    s.pending_ts = None
    s.status = st.STATUS_WORKING
    s.last_event_ts = now
    t.tick(now + CODEX_ZOMBIE_TIMEOUT + 5)
    check("codex: 3min 静默 → idle", s.status, st.STATUS_IDLE)
    t.tick(now + CODEX_SESSION_TTL - 5)
    check("codex: 未到 10min 不删卡", "S1" in t.sessions, True)
    t.tick(now + CODEX_SESSION_TTL + 5)
    check("codex: 10min 静默 → 删卡", "S1" in t.sessions, False)

    # 删卡后同一线程再有事件 → 卡片自动回来（tail 偏移不断，不会重读旧事件）
    t.apply_record(cdx("event_msg", {"type": "task_started", "thread_id": "S1"}), tail, now + 700)
    check("codex: 重新活跃 → 卡片回来", "S1" in t.sessions, True)
    s = t.sessions["S1"]

    # 内部工作目录（~/.codex/**，如 memory 会话）不出卡片
    mem = os.path.expanduser("~/.codex/memories")
    t.apply_record(cdx("session_meta", {"id": "M2", "session_id": "M2", "cwd": mem,
                                        "thread_source": "user", "source": "vscode"}), tail, now)
    check("rollout: ~/.codex/memories 不出卡", "M2" in t.sessions, False)

    t.apply_record(cdx("session_meta", {"id": "G1", "session_id": "S1", "cwd": "/p/proj",
                                        "thread_source": "guardian_review",
                                        "source": {"subagent": {"other": "guardian"}}}), tail, now)
    t.apply_record(cdx("event_msg", {"type": "task_started", "thread_id": "G1"}), tail, now)
    check("guardian 子线程：不出卡片", "G1" in t.sessions, False)

    t.tick(now + 3600)
    check("TTL → 移除", "S1" in t.sessions, False)


def test_dismiss():
    """右键"删除会话"：立刻摘卡、落盘、再有活动自动回来。"""
    print("右键删除（静音到下次活动）")
    import shutil
    from state import Dismissed, StateTracker

    tmpd = tempfile.mkdtemp()
    dpath = os.path.join(tmpd, "dismissed.json")
    d = Dismissed(dpath)

    # --- Claude 侧 ---
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    now = time.time()
    def ev(name, sid, ts=None, **kw):
        r = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts or now)),
             "hook_event_name": name, "session_id": sid}
        r.update(kw)
        return r

    for r in [ev("SessionStart", "S1", cwd="/p/demo"),
              ev("PreToolUse", "S1", tool_name="Bash"),
              ev("PostToolUse", "S1"),
              ev("Stop", "S1", last_assistant_message="done")]:
        tmp.write(json.dumps(r) + "\n")
    tmp.close()

    orig = st.EVENTS_FILE
    st.EVENTS_FILE = Path(tmp.name)
    try:
        t = StateTracker(d)
        check("删除前有卡", len(t.visible_sessions()), 1)
        check("dismiss 返回 True", t.dismiss("S1"), True)
        check("删除后无卡", t.visible_sessions(), [])
        check("落盘", os.path.exists(dpath), True)
        check("重新加载仍被静音", Dismissed(dpath).is_dismissed("S1", now), True)

        t.apply_event(ev("PreToolUse", "S1", ts=now + 5).copy())
        check("有新事件 → 卡片自动回来", len(t.visible_sessions()), 1)
        check("删除不存在的 session", t.dismiss("nope"), False)
    finally:
        st.EVENTS_FILE = orig
        os.unlink(tmp.name)

    # --- Codex 侧 ---
    # 用 __new__ 造空 tracker：直接构造会去扫真实的 ~/.codex/sessions
    t2 = CodexTracker.__new__(CodexTracker)
    t2.sessions, t2._tails, t2._ignored = {}, {}, set()
    t2._thread_names, t2._names_mtime = {}, None
    t2.dismissals = d
    tail = _Tail()
    t2.apply_record(cdx("session_meta", {"id": "C1", "session_id": "C1", "cwd": "/p/proj",
                                          "thread_source": "user", "source": "vscode"}), tail, now)
    t2.apply_record(cdx("event_msg", {"type": "task_started", "thread_id": "C1"}), tail, now)
    check("codex 删除前有卡", len(t2.visible_sessions()), 1)
    check("codex dismiss", t2.dismiss("C1"), True)
    check("codex 删除后无卡", t2.visible_sessions(), [])
    t2.apply_record(cdx("event_msg", {"type": "task_started", "thread_id": "C1"},
                        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 5))), tail, now)
    check("codex 有新事件 → 卡回来", len(t2.visible_sessions()), 1)

    shutil.rmtree(tmpd, ignore_errors=True)


def test_codex_hooks():
    """hook 事件路径：权限请求→等待批准、SessionEnd→下卡、子线程忽略。"""
    print("Codex hooks 事件路径")
    t = CodexTracker.__new__(CodexTracker)
    t.sessions, t._tails, t._ignored = {}, {}, set()
    t._thread_names, t._names_mtime = {}, None
    t.dismissals = st.Dismissed(os.path.join(tempfile.mkdtemp(), "d.json"))
    now = time.time()

    def hk(name, sid="H1", ts=None, **kw):
        r = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts or now)),
             "agent": "codex", "hook_event_name": name, "session_id": sid,
             "_ts": ts or now}
        r.update(kw)
        return r

    t.apply_hook_record(hk("SessionStart", cwd="/p/demo"))
    check("hook: SessionStart 不出卡", t.visible_sessions(), [])
    t.apply_hook_record(hk("UserPromptSubmit"))
    check("hook: UserPromptSubmit → working", t.sessions["H1"].status, st.STATUS_WORKING)
    t.apply_hook_record(hk("PreToolUse", tool_name="exec"))
    check("hook: PreToolUse 记 pending", t.sessions["H1"].pending_ts is not None, True)
    # hook 覆盖的会话：悬置兜底放松到 90s（否则 clocksleep / npm test 这类长工具误报红）
    t.tick(now + 30)
    check("hook: 悬置 30s 不翻红", t.sessions["H1"].status, st.STATUS_WORKING)
    t.tick(now + CODEX_HOOK_PENDING_GAP + 5)
    check("hook: 悬置 >90s 才翻红", t.sessions["H1"].status, st.STATUS_ATTENTION)
    t.apply_hook_record(hk("PreToolUse", tool_name="exec"))   # 重置为工作中继续下一步
    t.sessions["H1"].pending_ts = now
    t.apply_hook_record(hk("PermissionRequest", tool_name="apply_patch"))
    check("hook: PermissionRequest → attention", t.sessions["H1"].status, st.STATUS_ATTENTION)
    check("hook: 原因", t.sessions["H1"].attention_reason, "等待批准")
    t.apply_hook_record(hk("PostToolUse"))
    check("hook: PostToolUse → working", t.sessions["H1"].status, st.STATUS_WORKING)
    t.apply_hook_record(hk("Stop", last_assistant_message="搞定了"))
    check("hook: Stop → idle", t.sessions["H1"].status, st.STATUS_IDLE)
    check("hook: Stop 带最后一句", t.sessions["H1"].last_message, "搞定了")
    t.apply_hook_record(hk("SessionEnd"))
    check("hook: SessionEnd → 下卡", "H1" in t.sessions, False)

    # 内部子线程（guardian）的 hook 事件不建卡
    t.apply_hook_record(hk("UserPromptSubmit", sid="G1", agent_type="guardian_review"))
    check("hook: guardian 子线程不建卡", "G1" in t.sessions, False)
    # 内部工作目录（~/.codex/**）—— memory 会话实测只会在 hook 里露头
    mem = os.path.expanduser("~/.codex/memories")
    t.apply_hook_record(hk("UserPromptSubmit", sid="M1", cwd=mem))
    check("hook: ~/.codex/memories 不出卡", "M1" in t.sessions, False)
    t.apply_hook_record(hk("PreToolUse", sid="M1", cwd=mem, tool_name="exec"))
    check("hook: 内部会话后续事件也不出卡", "M1" in t.sessions, False)
    t.apply_hook_record(hk("SessionEnd", sid="M1", cwd=mem))
    check("hook: 内部会话 SessionEnd 也不报错", "M1" in t.sessions, False)

    # 未知 agent_type 当用户线程（宁可多一张卡）
    t.apply_hook_record(hk("UserPromptSubmit", sid="H2", agent_type="something_new"))
    check("hook: 未知 agent_type 仍建卡", "H2" in t.sessions, True)
    # 被忽略的子线程 id 后续事件也不建卡
    t.apply_hook_record(hk("PreToolUse", sid="G1"))
    check("hook: 已忽略 id 不复活", "G1" in t.sessions, False)


if __name__ == "__main__":
    test_claude()
    test_codex()
    test_dismiss()
    test_codex_hooks()
    print("FAILED" if FAILS else "ALL OK")
    sys.exit(1 if FAILS else 0)
