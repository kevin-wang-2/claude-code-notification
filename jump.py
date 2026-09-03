"""点击卡片 → 激活承载该 session 的 VSCode window。

两条路径：
1. **AXRaise（精确）**：按窗口标题 contains 文件夹名匹配并置前。需要 macOS「辅助功能」权限。
2. **code -r**（无权限时默认）：`code -r <项目路径>` 让 VSCode 聚焦/复用对应项目窗口。
   ~1.2s（CLI 启动 Node），但能正确聚焦其它窗口里已打开的文件夹；
   曾试过 `vscode://folder/` URL（快 ~9 倍）——但它只会在当前焦点窗口打开，
   无法跳转到其它 VSCode 窗口，已废弃。

权限检测带 5 分钟缓存，避免每次点击都尝试无权限的 osascript。
同名文件夹开多个窗口时无法区分，已知局限。
"""
import os
import shutil
import subprocess
import time

VSCODE_PROCESS = "Code"
VSCODE_APP = "Visual Studio Code"
CODE_CLI = shutil.which("code") or "/opt/homebrew/bin/code"

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


def _axraise(folder):
    """AXRaise 精确跳转。返回 (ok, error_msg_or_None)。"""
    esc = folder.replace("\\", "\\\\").replace('"', '\\"')
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


def _code_reuse(project_path):
    """code -r 聚焦项目窗口（正确跨窗口，~1.2s）。返回 (ok, message)。"""
    if not os.path.isdir(project_path):
        return False, "项目目录不存在"
    try:
        r = subprocess.run([CODE_CLI, "-r", project_path],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False, r.stderr.strip()[:200] or f"code -r 退出码 {r.returncode}"
        _activate()
        return True, ""
    except FileNotFoundError:
        return False, "未找到 code CLI"
    except subprocess.TimeoutExpired:
        return False, "code -r 超时"
    except Exception as e:
        return False, str(e)


def jump_to_project(project_path):
    """按项目路径跳转到对应 VSCode 窗口。返回 (ok, message)。"""
    if not project_path or project_path == "?":
        return False, "无项目路径"
    folder = os.path.basename(project_path.rstrip("/"))
    if not folder:
        return False, "无文件夹名"
    _jlog(f"jump_to_project path={project_path!r} folder={folder!r}")

    # 1. 有辅助权限 → AXRaise 精确匹配
    if _has_ax_permission():
        ok, msg = _axraise(folder)
        _jlog(f"AXRaise -> ok={ok} msg={msg!r}")
        if ok:
            return True, ""
        # AXRaise 失败（窗口标题不匹配等）→ 兜底 code -r，别直接失败
        _jlog("AXRaise 失败，兜底 code -r")

    # 2. 无权限 / AXRaise 失败 → code -r（能正确跨窗口聚焦）
    ok, msg = _code_reuse(project_path)
    _jlog(f"code -r -> ok={ok} msg={msg!r}")
    if ok:
        return True, ""
    return False, msg


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "/Users/kaibinwa/Desktop/mindweave"
    ok, msg = jump_to_project(path)
    print("OK" + (f" {msg}" if msg else "") if ok else f"FAIL: {msg}")
