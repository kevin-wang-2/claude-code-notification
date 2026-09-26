# Claude / Codex 多 Session 状态提示器

无边框透明浮窗，实时显示所有 coding session 的状态：工作中 / 空闲 / 需要批准。

**两个来源**：Claude Code（hook → events.jsonl）和 Codex（rollout jsonl）。
Codex 的卡片带一个青色 `Codex` 小标签，Claude 的不带（保持原外观）。

## 结构

- `main.py` — PyQt6 入口 + 浮窗 UI（无边框、置顶、不抢焦点、可拖动、右键退出）
- `state.py` — Claude 状态机：增量读 `~/.claude/status/events.jsonl`，维护每个 session 的状态
- `codex_state.py` — Codex 状态机：增量读 `~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl`
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
        ↓ 500ms 轮询 ──┐
state.py 状态机 ───────┼→ main.py 浮窗
codex_state.py 状态机 ─┘
        ↑ 500ms 轮询
~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl（codex 每次 turn 的全量事件）
```

两套状态机输出同一种 `SessionState`（`agent` 字段区分来源），主窗口合并排序渲染。

## 自检

```bash
./venv/bin/python codex_state.py       # 打印当前 Codex session 状态（实时）
./venv/bin/python tests/selftest.py    # 两套状态机离线自测（不依赖真实活动）
```

## Claude 状态机（2026-08-05 实测）

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

点卡片 → 激活承载该 session 的 VSCode 窗口（两个来源共用：jump.py 只看 session 的 cwd）。
先读 VSCode
`globalStorage/storage.json` 的 `backupWorkspaces`，把 session cwd 解析到窗口根
——**普通文件夹窗口**或**复合工作区**（cwd 是工作区子文件夹时窗口标题不含它的
名字）。`backupWorkspaces` 只是**候选**：窗口关掉后条目长期残留（实测 2 个真实
窗口 vs 16 条登记），不能用来判断窗口死活。有辅助功能权限时用 AXRaise 按标题
逐个验证并置前（~0.2s，标题是唯一可靠的实时依据）。

无 AX 权限时一律用**不带 flag 的 `code <目标>`**：已打开则聚焦那个窗口，未打开
则开新窗口，任何情况下都不替换活动窗口。**绝不用 `code -r`**——它的语义是「在
最后活动窗口打开」，目标没开着时会把活动窗口整个替换掉；曾经盲选残留条目再 -r，
于是点开某些 chip 会把当前 workspace 覆盖掉。多个候选歧义时用 `code --status`
的窗口列表挑活着的那个，但它**会漏报**（3 个窗口只列 2 个），只当正向信号；
这一步 ~1-3s，在鼠标悬停卡片时就预热好（`jump.prefetch()`）。

## Codex 状态机（2026-09-26 实测，codex 0.155.0-alpha.16.3 / VS Code 扩展）

Codex 没有 hooks，但一条 turn 的每个事件都落在 rollout jsonl 里，直接 tail 即可：

- 目录/文件名按**本地日期/时间**分（`sessions/2026/09/26/rollout-2026-09-26T09-56-29-<id>.jsonl`），
  行内 `timestamp` 是 UTC —— 时间一律取行内 timestamp。
- `session_meta` 里的 **`id` 才是本线程 id，`session_id` 在子线程里是父线程 id**
  （实测 guardian_review 子线程），所以按 `id` 建卡片。
- `thread_source != "user"`（或 `source` 是 dict 的 subagent）＝内部审查子线程，不出卡片。
- 事件按 `payload.thread_id` 归属；`event_msg` 承载状态，`response_item` 承载工具调用。

| 信号 | 状态 |
|---|---|
| `event_msg`: task_started / user_message / agent_message / agent_reasoning | 🟢 工作中 |
| `event_msg`: token_count / patch_apply_end / web_search_end / thread_settings_applied | 🟢 工作中（心跳） |
| `response_item`: reasoning / `*_tool_call` | 🟢 工作中（工具调用记 pending） |
| `response_item`: `*_tool_call_output` / `item_completed`(CommandExecution/FileChange/Reasoning) | 🟢 工作中（清 pending） |
| `event_msg.task_complete` | ⚪ 空闲（附 `last_agent_message` 前 200 字） |
| `event_msg.turn_aborted` | ⚪ 空闲，标「已中断」 |
| 工具调用 pending > 10s | 🔴 等待批准/回答（兜底，与 Claude 侧同款启发式） |
| 30 分钟无事件 | 移除卡片（兜底窗口关闭/进程被杀） |

已知取舍：Codex 侧**没有**「等待批准」的显式事件（VS Code 里 approvals_reviewer=auto_review，
批准过程是另开的 guardian_review 子线程），所以沿用 Claude 的 10s 悬置兜底 ——
跑长命令（`npm test` 几十秒）时也会短暂翻红，属已知误报。

自检：`./venv/bin/python codex_state.py` 打印当前 Codex session 状态。

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
