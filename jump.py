"""点击卡片 → 激活承载该 session 的 VSCode window。

跳转前先把 cwd 解析到"当前真正打开的窗口根"（文件夹窗口 或 复合工作区），
数据源是 VSCode 的 globalStorage/storage.json 的 backupWorkspaces 字段
（实时反映打开的窗口，用于崩溃恢复登记）。这解决复合工作区 bug：
cwd 是工作区的子文件夹时它不是任何窗口的根，直接 `code -r <cwd>` 会把
当前活动窗口的工作区整个替换掉（-r 语义 = 在最后活动窗口里打开）。

解析到目标后两条路径：
1. **AXRaise（精确）**：按窗口标题 contains 匹配并置前。需要 macOS「辅助功能」权限。
2. **code -r**（无权限时兜底）：只对"确认已打开的根"使用 —— 此时 -r 会聚焦
   已有窗口而不是替换。~1.2s（CLI 启动 Node）。
   曾试过 `vscode://folder/` URL（快 ~9 倍）——但它只会在当前焦点窗口打开，
   无法跳转到其它 VSCode 窗口，已废弃。
   未命名工作区（Untitled Workspace）没有可 `code -r` 的 .code-workspace
   文件，只能走 AXRaise；无权限时明确报错而不是破坏性兜底。

cwd 不属于任何已打开窗口时用 `code -n` 开新窗口（绝不替换别人的窗口）。
权限检测带 5 分钟缓存，避免每次点击都尝试无权限的 osascript。
同名文件夹开多个窗口时无法区分，已知局限。
"""
import json
import os
import shutil
import subprocess
import time
from urllib.parse import unquote, urlparse

VSCODE_PROCESS = "Code"
VSCODE_APP = "Visual Studio Code"
CODE_CLI = shutil.which("code") or "/opt/homebrew/bin/code"
STORAGE_JSON = os.path.expanduser(
    "~/Library/Application Support/Code/User/globalStorage/storage.json")

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
    """探测辅助功能权限（5 分钟缓存）。VSCode 必然开着（session 在跑）。"""
    global _ax_available, _ax_checked_at
    now = time.time()
    if _ax_available is not None and now - _ax_checked_at < 300:
        return _ax_available
    script = (f'tell application "System Events" to tell process "{VSCODE_PROCESS}" '
              f"to get name of every window")
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
        _ax_available = (r.returncode == 0)
    except Exception:
        _ax_available = False
    _ax_checked_at = now
    _jlog(f"ax probe -> {_ax_available}")
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
    """读 storage.json 的 backupWorkspaces（实时反映打开窗口），返回窗口根列表。

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
    开着只能靠 AXRaise 逐个验证，这里全部返回；同长度时文件夹排前（更精确）。
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


def _axraise(pattern):
    """AXRaise 按窗口标题 contains 匹配置前。返回 (ok, error_msg_or_None)。"""
    esc = pattern.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "System Events"
    tell process "{VSCODE_PROCESS}"
        set targetWindow to first window whose name contains "{esc}"
        perform action "AXRaise" of targetWindow
    end tell
end tell
tell application "{VSCODE_APP}" to activate
'''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return True, None
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


def _code_open(target, flag="-r"):
    """code CLI 打开/聚焦 target。返回 (ok, message)。

    -r 只能用于"确认已打开的根"（此时聚焦已有窗口）；
    对未打开的路径用 -n 开新窗口，避免替换别人窗口的内容。
    """
    if not os.path.exists(target):
        return False, "目标路径不存在"
    try:
        r = subprocess.run([CODE_CLI, flag, target],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False, r.stderr.strip()[:200] or f"code {flag} 退出码 {r.returncode}"
        _activate()
        return True, ""
    except FileNotFoundError:
        return False, "未找到 code CLI"
    except subprocess.TimeoutExpired:
        return False, f"code {flag} 超时"
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


def jump_to_project(project_path):
    """按项目路径（session cwd）跳转到对应 VSCode 窗口。返回 (ok, message)。"""
    if not project_path or project_path == "?":
        return False, "无项目路径"
    folder = os.path.basename(project_path.rstrip("/"))
    if not folder:
        return False, "无文件夹名"
    _jlog(f"jump_to_project path={project_path!r} folder={folder!r}")

    # 0. 解析 cwd → 可能承载它的窗口根（文件夹窗口 / 复合工作区，可多个）
    roots = _open_roots()
    if roots is None:
        # storage.json 读不到（格式变化等）→ 退回旧逻辑：AXRaise → code -r
        _jlog("storage.json 不可用，退回旧逻辑")
        if _try_axraise([folder]):
            return True, ""
        ok, msg = _code_open(project_path.rstrip("/"))
        _jlog(f"legacy code -r -> ok={ok} msg={msg!r}")
        return (True, "") if ok else (False, msg)

    matches = _resolve_targets(project_path, roots)
    _jlog(f"open_roots={len(roots)} matches={[(w['kind'], w.get('path') or w['name']) for w in matches]}")

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
                return True, ""
        if _try_axraise([folder]):   # storage.json 可能滞后，cwd 名字直接试一次
            return True, ""
        # AX 确认没有窗口开着它 → 开新窗口（-r 会替换活动窗口，不可用）
        _jlog("AX 未命中任何窗口，code -n 开新窗口")
        ok, msg = _code_open(project_path.rstrip("/"), flag="-n")
        return (True, "") if ok else (False, msg)

    # 2. 无辅助权限：只能盲选，破坏性动作一律不做
    #    code -r 的语义 =「在最后活动的窗口打开」——目标未打开时会整个替换
    #    活动窗口的内容，所以只对"大概率开着的根"使用
    if any(w["kind"] == "workspace" and w["untitled"] for w in matches):
        # 命中未命名工作区：没有可 -r 的 .code-workspace 文件；
        # 赌 code -r 文件夹/其它候选可能把工作区窗口替换掉，明确拒绝
        return False, "该 session 在未命名工作区中，需授权「辅助功能」才能跳转"
    if matches:
        w = matches[0]
        target = w["path"] if w["kind"] == "folder" else w["config"]
        ok, msg = _code_open(target)
        _jlog(f"code -r {w['kind']} -> ok={ok} msg={msg!r}")
        return (True, "") if ok else (False, msg)
    # 确认没有窗口和它相关 → 开新窗口
    _jlog("无匹配窗口，code -n 开新窗口")
    ok, msg = _code_open(project_path.rstrip("/"), flag="-n")
    return (True, "") if ok else (False, msg)


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "/Users/kaibinwa/Desktop/mindweave"
    ok, msg = jump_to_project(path)
    print("OK" + (f" {msg}" if msg else "") if ok else f"FAIL: {msg}")
