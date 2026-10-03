"""点击卡片 → 激活承载该 session 的 VSCode window。

跳转前先把 cwd 解析到"承载它的窗口根"（文件夹窗口 或 复合工作区），
候选来自 VSCode 的 globalStorage/storage.json 的 backupWorkspaces 字段。
这解决复合工作区问题：cwd 是工作区的子文件夹时它不是任何窗口的根，拿 cwd 去
打开只会另开一个只含该子文件夹的窗口，而不是聚焦工作区窗口。

注意 backupWorkspaces **只是候选，不代表窗口开着**：窗口关掉后条目会长期残留
（实测 2 个真实窗口 vs 16 条登记）。同一个文件夹既可能作为文件夹窗口登记、
又是某个工作区的子文件夹，两条都会命中，靠它判断死活必然出错。

解析到目标后两条路径：
1. **AXRaise（精确）**：按窗口标题 contains 匹配并置前。需要 macOS「辅助功能」权限。
   标题匹配本身就是存活验证，匹配不上即说明没开着。~0.2s。
2. **不带 flag 的 `code <目标>`**（无权限时兜底）：实测语义 = 已打开则聚焦该窗口、
   未打开则开新窗口，**任何情况下都不替换活动窗口**。所以这里不需要（也无法可靠地）
   判断窗口死活，直接调用即可。~1.2s（CLI 启动 Node）。
   绝不用 `code -r`：-r 的语义是「在最后活动的窗口打开」，目标没开着时会把活动
   窗口整个替换掉 —— 这正是本文件历史上反复出现的 bug。
   曾试过 `vscode://folder/` URL（快 ~9 倍）——但它只会在当前焦点窗口打开，
   无法跳转到其它 VSCode 窗口，已废弃。
   未命名工作区（Untitled Workspace）的配置文件不是可 `code` 打开的
   .code-workspace，跳过该候选。

多个候选时（同一文件夹既登记为文件夹窗口、又属于某个工作区）用 `code --status`
的窗口列表挑真的开着的那个。注意它**会漏报**（实测 3 个窗口只列出 2 个），
所以只当正向信号：列出来 = 确实开着；没列出来 = 不知道，按默认顺序走。
权限检测带 5 分钟缓存，避免每次点击都尝试无权限的 osascript。
同名文件夹开多个窗口时无法区分，已知局限。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import unquote, urlparse

VSCODE_PROCESS = "Code"
VSCODE_APP = "Visual Studio Code"
CODE_CLI = shutil.which("code") or "/opt/homebrew/bin/code"
STORAGE_JSON = os.path.expanduser(
    "~/Library/Application Support/Code/User/globalStorage/storage.json")

# CLI 会话（跑在 VS Code 集成终端里）跳转时，触发"聚焦终端"的快捷键。
# 需与 ~/Library/Application Support/Code/User/keybindings.json 里那条绑定一致
# （cmd+ctrl+shift+u -> workbench.action.terminal.focus），且该命令在
# settings.json 的 terminal.integrated.commandsToSkipShell 里，终端有焦点时才会被 VS Code 接住。
TERM_FOCUS_KEY = "u"
TERM_FOCUS_MODS = "command down, control down, shift down"
TERM_FOCUS_KEYCODE = 32                                # 'u' 的虚拟键码
TERM_FOCUS_FLAGS = (1 << 20) | (1 << 18) | (1 << 17)   # cmd | ctrl | shift（CGEvent 标志位）

JUMP_LOG = "/Users/kaibinwa/.claude/status/jump.log"   # 与 main.py 共用排查日志


def _jlog(msg):
    try:
        with open(JUMP_LOG, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass

_ax_available = None
_ax_checked_at = 0.0


def _has_ax_permission():
    """探测能否用 osascript 读 VSCode 窗口标题（5 分钟缓存）。

    这条链路要**两个**互相独立的 TCC 权限，缺任一个都失败，报错不同：
      -1743  本 app 没有「自动化」权限去控制 System Events
      -25211 / "not allowed assistive access"  没有「辅助功能」权限
    所以失败时必须把 stderr 记下来，否则分不清该去开哪个。
    VSCode 必然开着（session 在跑），所以失败一定是权限而不是没窗口。
    """
    global _ax_available, _ax_checked_at
    now = time.time()
    if _ax_available is not None and now - _ax_checked_at < 300:
        return _ax_available
    script = (f'tell application "System Events" to tell process "{VSCODE_PROCESS}" '
              f"to get name of every window")
    err = ""
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
        _ax_available = (r.returncode == 0)
        if not _ax_available:
            err = f" rc={r.returncode} stderr={r.stderr.strip()[:300]!r}"
    except Exception as e:
        _ax_available = False
        err = f" exc={e!r}"
    _ax_checked_at = now
    _jlog(f"ax probe -> {_ax_available}{err}")
    return _ax_available


def _uri_to_path(uri):
    """file:// URI → 本地路径；非 file（如 vscode-remote://）返回 None。"""
    try:
        p = urlparse(uri)
        if p.scheme != "file":
            return None
        return unquote(p.path).rstrip("/")
    except Exception:
        return None


def _open_roots():
    """读 storage.json 的 backupWorkspaces，返回**候选**窗口根列表。

    只是候选：条目在窗口关闭后仍长期残留，是否真的开着要靠 _is_live / AXRaise 验。

    每项：{"kind": "folder", "path": ...}
    或   {"kind": "workspace", "config": 配置文件路径, "folders": [子文件夹...],
          "name": 标题里的名字, "untitled": 是否未命名工作区}
    读不到 / 格式变了 → 返回 None（调用方退回旧逻辑）。
    """
    try:
        with open(STORAGE_JSON) as f:
            bw = json.load(f).get("backupWorkspaces")
        if bw is None:
            return None
    except Exception:
        return None
    roots = []
    for w in bw.get("folders", []):
        path = _uri_to_path(w.get("folderUri", ""))
        if path:
            roots.append({"kind": "folder", "path": path})
    for w in bw.get("workspaces", []):
        config = _uri_to_path(w.get("configURIPath", ""))
        if not config:
            continue
        try:
            with open(config) as f:
                ws = json.load(f)
        except Exception:
            continue
        base = os.path.dirname(config)
        folders = []
        for entry in ws.get("folders", []):
            p = entry.get("path")
            if p:
                folders.append(os.path.normpath(os.path.join(base, os.path.expanduser(p))))
            elif entry.get("uri"):
                p = _uri_to_path(entry["uri"])
                if p:
                    folders.append(p)
        # 未命名工作区保存在 .../Code/Workspaces/<id>/workspace.json，
        # 已保存的是用户自己的 xxx.code-workspace
        untitled = not config.endswith(".code-workspace")
        name = "Untitled" if untitled else os.path.basename(config)[:-len(".code-workspace")]
        roots.append({"kind": "workspace", "config": config, "folders": folders,
                      "name": name, "untitled": untitled})
    return roots


def _resolve_targets(cwd, roots):
    """cwd 可能属于哪些窗口？返回按根路径长度降序的候选列表。

    匹配规则：窗口根（文件夹窗口的 path / 工作区的每个子文件夹）是 cwd 本身
    或其祖先。注意 backupWorkspaces 含已关闭窗口的残留条目（backup 未清理），
    所以可能同时命中"文件夹窗口"和"包含同一文件夹的工作区"——哪个窗口真的
    开着只能靠 AXRaise / code --status 逐个验证，这里全部返回；
    同长度时文件夹排前（更精确）。
    """
    cwd = os.path.normpath(cwd)
    matches = []
    for w in roots:
        candidates = [w["path"]] if w["kind"] == "folder" else w["folders"]
        for root in candidates:
            root = os.path.normpath(root)
            if cwd == root or cwd.startswith(root + os.sep):
                matches.append((len(root), 0 if w["kind"] == "folder" else 1, w))
                break
    matches.sort(key=lambda m: (-m[0], m[1]))
    return [w for _, _, w in matches]


WINDOW_LINE = re.compile(r"window \[\d+\] \((.*)\)\s*$")


_titles_cache = None
_titles_cache_at = 0.0
_titles_lock = threading.Lock()
TITLES_TTL = 6.0        # 秒：悬停预热到点击之间够用，又不会拿到过期的窗口集


def prefetch():
    """鼠标悬停在卡片上时后台预热窗口列表，把 ~1-3s 的 Node 启动藏掉。

    非阻塞；TTL 内重复调用直接命中缓存，扫过一排卡片也只会跑一次。
    """
    if _titles_cache is not None and time.time() - _titles_cache_at < TITLES_TTL:
        return
    if _titles_lock.locked():
        return
    threading.Thread(target=_live_window_titles, daemon=True).start()


def _live_window_titles():
    """`code --status` 里的 `window [N] (标题)` 行。

    无辅助功能权限时唯一能拿到的窗口列表，但**只可当正向信号**：实测它会漏报
    （AX 报 3 个窗口时它只列 2 个），列出来的一定开着，没列出来的不代表没开。
    返回标题列表，探测失败返回 None。代价 ~1-3s，所以只在候选 >1 时才调。
    """
    global _titles_cache, _titles_cache_at
    if _titles_cache is not None and time.time() - _titles_cache_at < TITLES_TTL:
        return _titles_cache
    with _titles_lock:      # 悬停预热和点击可能同时进来，只让一个去起 Node
        if _titles_cache is not None and time.time() - _titles_cache_at < TITLES_TTL:
            return _titles_cache
        try:
            r = subprocess.run([CODE_CLI, "--status"],
                               capture_output=True, text=True, timeout=15)
        except Exception as e:
            _jlog(f"code --status 失败: {e!r}")
            return None
        if r.returncode != 0:
            _jlog(f"code --status 退出码 {r.returncode}")
            return None
        titles = [m.group(1) for m in
                  (WINDOW_LINE.search(line) for line in r.stdout.splitlines()) if m]
        _titles_cache, _titles_cache_at = titles, time.time()
        _jlog(f"live windows={titles}")
        return titles


def _title_segments(title):
    """标题按 ' — ' 拆段。VSCode 标题模板是
    `${activeEditorShort} — ${rootName} — ${profileName}`（缺省项会省略），
    所以根名是其中某一段，不是固定下标。
    """
    return [s.strip() for s in title.split(" — ") if s.strip()]


def _root_names(w):
    """窗口根在标题里应该长什么样（工作区含多语言变体）。"""
    if w["kind"] == "folder":
        return [os.path.basename(w["path"])]
    return [w["name"] + suffix
            for suffix in (" (Workspace)", "（工作区）", " (工作区)")]


def _title_has_root(title, names):
    """标题的「根名」段是否命中 names 之一。

    跳过第 0 段：那是活动编辑器名，一个恰好同名的文件会把未打开的文件夹
    误判成开着，于是 -r 又变回破坏性操作。只有一段时它就是根名本身。
    """
    segs = _title_segments(title)
    if len(segs) > 1:
        segs = segs[1:]
    for seg in segs:
        for name in names:
            if seg == name or seg.startswith(name + " ["):   # 远程窗口 "name [SSH: host]"
                return True
    return False


def _is_live(w, titles):
    return any(_title_has_root(t, _root_names(w)) for t in titles)


def _axraise(pattern):
    """把 VS Code 带到前台，并把标题 contains pattern 的窗口置前。返回 (ok, info)。

    顺序很关键：**先 `set frontmost to true`（把 app 拉到前台），再 AXRaise 目标窗口**。
    反过来做的话，VS Code 在 app 被激活时会把「上次活动的窗口」自己拉到前面，
    把刚 raise 的窗口顶掉 —— 表现就是「Dock 图标出现了，但窗口没切」。
    顺带：窗口若被最小化，先取消最小化（否则 raise 也白搭）。
    """
    esc = pattern.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "System Events"
    tell process "{VSCODE_PROCESS}"
        set frontmost to true
        try
            set w to first window whose name contains "{esc}"
        on error errMsg
            return "nowindow"
        end try
        try
            if (value of attribute "AXMinimized" of w) is true then
                set value of attribute "AXMinimized" of w to false
            end if
        end try
        try
            perform action "AXRaise" of w
        on error errMsg
            return "raisefail:" & errMsg
        end try
    end tell
end tell
return "ok"
'''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        if r.returncode == 0:
            if out == "ok":
                return True, None
            return False, out
        return False, r.stderr.strip()[:200]
    except subprocess.TimeoutExpired:
        return False, "osascript 超时"
    except Exception as e:
        return False, str(e)


def _activate():
    """把 VSCode 带到前台。"""
    try:
        subprocess.run(["osascript", "-e", f'tell application "{VSCODE_APP}" to activate'],
                       capture_output=True, text=True, timeout=5)
    except Exception:
        pass


def _code_open(target):
    """`code <target>`（不带 flag）打开/聚焦 target。返回 (ok, message)。

    不带 flag 是唯一非破坏性的选择：已打开则聚焦那个窗口，未打开则开新窗口。
    不要加 -r（会替换活动窗口），也不要加 -n（已打开时会开出重复窗口）。
    """
    if not os.path.exists(target):
        return False, "目标路径不存在"
    try:
        r = subprocess.run([CODE_CLI, target],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False, r.stderr.strip()[:200] or f"code 退出码 {r.returncode}"
        _activate()
        return True, ""
    except FileNotFoundError:
        return False, "未找到 code CLI"
    except subprocess.TimeoutExpired:
        return False, "code 超时"
    except Exception as e:
        return False, str(e)


def _try_axraise(patterns):
    """依次尝试多个标题模式，任一命中即成功。无权限直接 False。"""
    if not _has_ax_permission():
        return False
    for p in patterns:
        ok, msg = _axraise(p)
        _jlog(f"AXRaise {p!r} -> ok={ok} msg={msg!r}")
        if ok:
            return True
    return False


def _proc_cwd(pid):
    """取进程 cwd（lsof）。失败返回 None。"""
    try:
        r = subprocess.run(["lsof", "-p", str(pid), "-a", "-d", "cwd"],
                           capture_output=True, text=True, timeout=5)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        return lines[-1].split()[-1] if lines else None
    except Exception:
        return None


def _proc_is_vscode_terminal(pid):
    """进程是否跑在 VS Code 集成终端里（env 里有 TERM_PROGRAM=vscode）。"""
    try:
        r = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=5)
        return "TERM_PROGRAM=vscode" in r.stdout
    except Exception:
        return False


def _codex_cli_pids():
    """正在跑的 codex CLI 进程 pid（排除 app-server / code-mode-host / daemon）。"""
    try:
        r = subprocess.run(["ps", "-axo", "pid=,command="],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    pids = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or "/codex" not in line:
            continue
        if any(k in line for k in ("app-server", "code-mode-host", "daemon")):
            continue
        pid = line.split(None, 1)[0]
        if pid.isdigit():
            pids.append(pid)
    return pids


def _has_vscode_terminal_session(project_path):
    """项目下是否有 codex CLI 正跑在 VS Code 集成终端里。

    只用来决定"聚焦窗口后要不要顺带聚焦终端面板"：
    扩展侧会话（Codex 面板）不满足此条件，只有 CLI-in-VS-Code-terminal 才 True。
    """
    if not project_path or project_path == "?":
        return False
    proj = os.path.normpath(project_path.rstrip("/"))
    for pid in _codex_cli_pids():
        cwd = _proc_cwd(pid)
        if not cwd:
            continue
        if os.path.normpath(cwd.rstrip("/")) != proj:
            continue
        if _proc_is_vscode_terminal(pid):
            return True
    return False


def _frontmost_app_name():
    """当前前台 app 名。"""
    script = ('tell application "System Events" to get name of first application process '
              'whose frontmost is true')
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=6)
        return r.stdout.strip()
    except Exception:
        return "?"


def _frontmost_is_vscode():
    return _frontmost_app_name() == VSCODE_PROCESS


def _post_focus_term_key():
    """由本进程直接合成按键（CGEventPost），不经过 osascript。

    为什么不用 System Events 的 keystroke：那条路会被 TCC 拦成
    「osascript 不允许发送按键 (1002)」——发键被归因到 /usr/bin/osascript，
    要额外给它「辅助功能」授权；而 Claude Status 本身已有「辅助功能」，
    自己用 CGEventPost 发键就不需要任何新授权。
    （AXRaise 能工作是因为那是 System Events 代做的 AX 动作，不触发这条限制。）
    """
    try:
        import ctypes
        import ctypes.util
        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        cg.CGEventSetFlags.restype = None
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cg.CGEventPost.restype = None
    except Exception as e:
        _jlog(f"cg load failed: {e!r}")
        return False
    kCGHIDEventTap = 0
    for down in (True, False):
        ev = cg.CGEventCreateKeyboardEvent(None, TERM_FOCUS_KEYCODE, down)
        if not ev:
            return False
        cg.CGEventSetFlags(ev, TERM_FOCUS_FLAGS)
        cg.CGEventPost(kCGHIDEventTap, ev)
    return True


def _focus_vscode_terminal():
    """把 VS Code 置前后，触发"聚焦终端"（幂等：隐藏则打开、已开则聚焦）。

    安全护栏：只有确认 VS Code 在前台时才发键，避免快捷键外泄到别的应用。
    """
    if not _frontmost_is_vscode():
        try:
            subprocess.run(["osascript", "-e", f'tell application "{VSCODE_APP}" to activate'],
                           capture_output=True, text=True, timeout=5)
        except Exception:
            pass
        # 从别的 app 触发时激活有延迟，轮询等它真的到前台
        deadline = time.time() + 2.5
        while time.time() < deadline and not _frontmost_is_vscode():
            time.sleep(0.2)
    if not _frontmost_is_vscode():
        _jlog("focus terminal -> VS Code 未到前台，放弃（避免按键外泄）")
        return
    if _post_focus_term_key():
        _jlog("focus terminal -> cgevent sent")
        return
    # 兜底：System Events keystroke（需给 /usr/bin/osascript 辅助功能授权）
    script = (
        'tell application "System Events"\n'
        f'    keystroke "{TERM_FOCUS_KEY}" using {{{TERM_FOCUS_MODS}}}\n'
        '    return "sent"\n'
        'end tell'
    )
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=8)
        _jlog(f"focus terminal -> osascript fallback {r.stdout.strip()!r} {r.stderr.strip()[:200]!r}")
    except Exception as e:
        _jlog(f"focus terminal exception {e!r}")


def jump_to_project(project_path):
    """按项目路径（session cwd）跳转到对应 VSCode 窗口。返回 (ok, message)。

    若该项目的 codex 是跑在 VS Code 集成终端里的 CLI 会话，则在聚焦窗口之后
    再聚焦终端面板（否则只会停在窗口，到不了正在跑的 agent）。
    """
    if not project_path or project_path == "?":
        return False, "无项目路径"
    folder = os.path.basename(project_path.rstrip("/"))
    if not folder:
        return False, "无文件夹名"
    _jlog(f"jump_to_project path={project_path!r} folder={folder!r}")
    _jlog(f"frontmost before = {_frontmost_app_name()!r}")

    # CLI-in-VS-Code-terminal 会话：跳转末尾要顺带聚焦终端面板
    in_vscode_term = _has_vscode_terminal_session(project_path)

    # 0. 解析 cwd → 可能承载它的窗口根（文件夹窗口 / 复合工作区，可多个）
    roots = _open_roots()
    if roots is None:
        # storage.json 读不到（格式变化等）→ AXRaise，再退到实时窗口验证
        _jlog("storage.json 不可用，退回 AXRaise / 直接 code")
        if _try_axraise([folder]):
            if in_vscode_term:
                _focus_vscode_terminal()
            return True, ""
        ok, msg = _focus_without_ax([], project_path)
        if ok and in_vscode_term:
            _focus_vscode_terminal()
        return ok, msg

    matches = _resolve_targets(project_path, roots)
    _jlog(f"open_roots={len(roots)} matches={[(w['kind'], w.get('path') or w['name']) for w in matches]} vscode_term={in_vscode_term}")

    # 1. 有辅助权限：AXRaise 逐个候选验证（窗口标题是"真的开着"的唯一实时依据）
    if _has_ax_permission():
        for w in matches:
            if w["kind"] == "folder":
                patterns = [os.path.basename(w["path"])]
            else:
                # 复合工作区标题是 "<名字> (Workspace)"，不含子文件夹名
                patterns = [w["name"] + " (Workspace)",
                            w["name"] + "（工作区）",
                            w["name"] + " (工作区)"]
            if _try_axraise(patterns):
                if in_vscode_term:
                    _focus_vscode_terminal()
                return True, ""
        if _try_axraise([folder]):   # storage.json 可能滞后，cwd 名字直接试一次
            if in_vscode_term:
                _focus_vscode_terminal()
            return True, ""
        # AX 确认没有窗口开着它 → code 开新窗口
        _jlog("AX 未命中任何窗口，code 开新窗口")
        ok, msg = _code_open(project_path.rstrip("/"))
        if ok and in_vscode_term:
            _focus_vscode_terminal()
        return (True, "") if ok else (False, msg)

    # 2. 无辅助权限：不判死活，直接用不带 flag 的 code —— 已打开则聚焦，
    #    未打开则开新窗口，绝不替换活动窗口。
    ok, msg = _focus_without_ax(matches, project_path)
    if ok and in_vscode_term:
        _focus_vscode_terminal()
    return ok, msg


def _focus_without_ax(matches, project_path):
    """无辅助功能权限时的跳转。

    历史 bug：这里曾盲选 matches[0] 后 `code -r`。backupWorkspaces 的残留条目
    常让 matches[0] 是个早就关掉的文件夹窗口（该文件夹同时又是某个工作区的
    子文件夹时尤其常见），-r 于是落到未打开的路径上，把活动 workspace 替换掉。
    现在一律用不带 flag 的 code，破坏性从根上消失；候选顺序只影响"聚焦对了没"。
    """
    usable = [w for w in matches
              if not (w["kind"] == "workspace" and w["untitled"])]   # 未命名工作区无法 code 打开
    if len(usable) > 1:
        # 候选有歧义才值得花 ~1-3s 问 code --status（只作正向信号，会漏报）
        titles = _live_window_titles()
        if titles:
            live = [w for w in usable if _is_live(w, titles)]
            _jlog(f"live matches={[(w['kind'], w.get('path') or w['name']) for w in live]}")
            if live:
                usable = live
    target = None
    if usable:
        w = usable[0]
        target = w["path"] if w["kind"] == "folder" else w["config"]
    if target is None:
        target = project_path.rstrip("/")     # 无候选（或只剩未命名工作区）→ 直接开 cwd
    ok, msg = _code_open(target)
    _jlog(f"code {target!r} -> ok={ok} msg={msg!r}")
    return (True, "") if ok else (False, msg)


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "/Users/kaibinwa/Desktop/mindweave"
    ok, msg = jump_to_project(path)
    print("OK" + (f" {msg}" if msg else "") if ok else f"FAIL: {msg}")
