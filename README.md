# Claude 多 Session 状态提示器（骨架）

无边框透明浮窗，实时显示所有 Claude Code session 的状态：工作中 / 空闲 / 需要批准。

## 结构

- `main.py` — PyQt6 入口 + 浮窗 UI（无边框、置顶、不抢焦点、可拖动、右键退出）
- `state.py` — 状态机：增量读 `~/.claude/status/events.jsonl`，维护每个 session 的状态
- `hook_logger.py` — 正式版 hook 脚本（只落元数据，不落工具内容）

## 运行

系统默认 `python3`（homebrew 3.14）的 pyexpat 和 macOS 自带 libexpat 版本对不上，导致 `pip` 用不了，且 PyPI 上 PyQt6 暂无 3.14 wheel。改用 3.13 的 venv：

```bash
cd ~/Desktop/claude-notification
python3.13 -m venv venv        # 首次运行需要
./venv/bin/pip install PyQt6   # 首次运行需要
./venv/bin/python main.py
```

## 数据流

```
Claude Code hooks（~/.claude/settings.json 配置）
        ↓ 追加
~/.claude/status/events.jsonl
        ↓ 500ms 轮询
state.py 状态机 → main.py 浮窗
```

## 状态机（2026-08-05 实测）

| 信号 | 状态 |
|---|---|
| PermissionRequest | 🔴 等待批准（直接信号） |
| AskUserQuestion | 🔴 等待回答 |
| PreToolUse 后 10s+ 无 PostToolUse | 🔴 等待批准/回答（兜底） |
| PostToolUse | 🟢 工作中 |
| Notification（working 时） | 🔴 等待输入（漏权限事件的兜底；idle 时忽略） |
| Stop | ⚪ 空闲（显示最后一句） |
| SessionEnd | 移除卡片 |
| 15 分钟无事件 | ⚪ 空闲（纯兜底，防 Stop 丢失；思考/长回复不误判） |

## 点击跳转（jump.py）

点卡片 → 激活承载该 session 的 VSCode 窗口。先读 VSCode
`globalStorage/storage.json` 的 `backupWorkspaces`（实时反映打开的窗口），把
session cwd 解析到真正的窗口根——**普通文件夹窗口**或**复合工作区**（cwd 是
工作区子文件夹时窗口标题不含它的名字）。候选逐个用 AXRaise 按标题验证
（需辅助功能权限，backupWorkspaces 有已关窗口的残留条目，标题是唯一实时依据）。

无 AX 权限的兜底原则：`code -r` 语义是「在最后活动窗口打开」，目标未打开时会
**整个替换活动窗口的内容**——所以只对确认过的根使用；未命名工作区（无
.code-workspace 文件可 -r）直接报错提示授权；确认没开的路径用 `code -n` 开新窗口。

## 架构说明：为什么是 hooks 而不是 Remote Control

Remote Control（2025）是让手机/网页接管本机 session 的功能：本地 Claude Code
出站连到 Anthropic 服务器中转，不开本地端口、无本地 API，第三方程序无法借它
枚举/订阅 session 状态。官方也没有本地状态订阅接口（无 HTTP/WS endpoint；
cross-session socket 只能发消息不能订阅）。hooks + jsonl 仍是正解：零网络依赖、
毫秒级、能拿到 PermissionRequest/cwd。详见 code.claude.com/docs/en/remote-control.md。

## 切换到正式版 hook（可选）

研究版 hook 会落 `tool_input`/`tool_response`（可能含文件内容）。切到正式版：

```bash
cp ~/Desktop/claude-notification/hook_logger.py ~/.claude/status/hook_logger.py
```

（settings.json 里 command 已指向 `~/.claude/status/hook_logger.py`，换文件即可。）
