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
| Stop | ⚪ 空闲（显示最后一句） |
| SessionEnd | 移除卡片 |
| 15 分钟无事件 | ⚪ 空闲（纯兜底，防 Stop 丢失；思考/长回复不误判） |

## 切换到正式版 hook（可选）

研究版 hook 会落 `tool_input`/`tool_response`（可能含文件内容）。切到正式版：

```bash
cp ~/Desktop/claude-notification/hook_logger.py ~/.claude/status/hook_logger.py
```

（settings.json 里 command 已指向 `~/.claude/status/hook_logger.py`，换文件即可。）
