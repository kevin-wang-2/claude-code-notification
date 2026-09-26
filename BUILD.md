# 打包 / 安装 / 权限：操作流程

目标：把源码打成 `.app`、签名、装到 `/Applications`，并让「辅助功能」等 TCC 授权**跨重建不失效**。

> 结论先讲：**日常开发不用打包**（`./venv/bin/python main.py` 就行）；只有"改完想装到 `/Applications` 里长期用"时才走这里。装到 `/Applications` 的那份是 **PyInstaller 冻结版**，源码改了**必须重建**才会生效。

## 两个 `Claude Status.app` 别搞混

| 位置 | 是什么 | 用途 |
|---|---|---|
| 仓库根 `Claude Status.app` | 手写的 bash shim（`Contents/MacOS/launcher` → `cd .. && ./venv/bin/python main.py`） | 开发期用（跟着源码走，改完直接生效）；**它不是签名身份的那份** |
| `dist/Claude Status.app` → `/Applications/Claude Status.app` | PyInstaller 冻结 + 自签名证书签名 | 正式长期用的那份；有独立 bundle 身份、`LSUIElement`（不进 Dock/Cmd+Tab） |

## 前置条件

- venv 与 PyInstaller 已就位：`./venv/bin/pyinstaller --version` → `6.21.0`
- **代码签名证书在 login.keychain**（名字 `Claude Status Signing`，自签名 + Code Signing 用途）：
  ```bash
  security find-certificate -c "Claude Status Signing" | head -3   # 有输出=证书在
  ```
  ⚠️ `security find-identity -v -p codesigning` 显示 **0 valid identities 是正常的**（自签名没设信任，codesign 不校验信任链，照样能签）。别被它误导去"修证书"。
- 私钥必须还在同一个钥匙串。若真丢了（`codesign -s "Claude Status Signing"` 报 `no identity found`），用「钥匙串访问 → 证书助理 → 创建证书」重建：名字**必须仍是 `Claude Status Signing`**、类型=代码签名、自签名。**换了证书 DR 就变，辅助功能授权要重新给一次。**

## 1. 构建

```bash
cd ~/Desktop/claude-notification
./venv/bin/pyinstaller "Claude Status.spec" --noconfirm
```

- spec 里已固定：`CODESIGN_IDENTITY='Claude Status Signing'`、`BUNDLE_ID='com.kaibinwa.claude-status'`、`entitlements.plist`（关库验证，见下）、`LSUIElement=True`、`console=False`。
- `main.py` 的 `import` 会被自动跟踪 → 新增模块（如 `codex_state.py`）**不用改 spec**。
- 产物：`dist/Claude Status.app`。
- 常见警告可忽略：`upx=True` 但本机没装 upx。

## 2. 签名（构建日志里最后一步）

PyInstaller 会自动执行（等价命令，出错时手动补跑）：

```bash
/usr/bin/codesign -s "Claude Status Signing" --force --all-architectures \
  --timestamp --options=runtime --entitlements entitlements.plist --deep \
  "dist/Claude Status.app"
```

- 报 **`The timestamp service is not available`** = Apple 时间戳服务超时（走代理时偶发），**重跑这条命令一次**通常就好；仍不行就**去掉 `--timestamp`**（自签名 + 本地自用，DR 里不含时间戳，不影响授权稳定性）。
- 验证：
  ```bash
  codesign -dv --verbose=2 "dist/Claude Status.app" | grep -E 'Identifier|Authority'
  codesign -d -r- "dist/Claude Status.app"     # 记下这行 designated requirement
  codesign --verify --deep --strict "dist/Claude Status.app" && echo OK
  ```
  期望：`Identifier=com.kaibinwa.claude-status`、`Authority=Claude Status Signing`。

## 3. 安装到 /Applications

```bash
# 先退出正在运行的实例（右键窗口 → 退出；或 pkill）
pkill -f "Claude Status.app/Contents/MacOS/Claude Status"

# 备份旧的（保留一份，出问题能换回来）
mv "/Applications/Claude Status.app" "/Applications/Claude Status.app.bak-$(date +%Y%m%d)"

# 用 ditto 复制（保留签名/权限；别用 cp -R）
ditto "dist/Claude Status.app" "/Applications/Claude Status.app"
chmod -R a+rX "/Applications/Claude Status.app"     # 保持正常可读权限
```

**⚠️ 安装后必须核对 DR 没变**（变了=辅助功能授权会静默失效）：

```bash
codesign -d -r- "/Applications/Claude Status.app"
# 期望与第 2 步记下的同一行：
# designated => identifier "com.kaibinwa.claude-status" and certificate leaf = H"4123bb7f8d7e16edd28d954955582415952a4d25"
```

`/Applications` 属 `root:admin` 且组可写，当前用户在 admin 组 → **不需要 sudo**。

## 4. 权限（TCC）

装好后双击 `/Applications/Claude Status.app`。它需要两项：

| 权限 | 干什么用 | 给谁 |
|---|---|---|
| 辅助功能（Accessibility） | `jump.py` 用 AXRaise 按窗口标题置前（精确跳转） | `Claude Status` |
| 自动化 → System Events | 上面那条 osascript 链路 | `Claude Status` |

因为签名身份（DR）跨重建不变，**第一次授权后，之后重建+重装都不用再点**。首次或换证书后：

- 系统设置 → 隐私与安全性 → **辅助功能** → `+` 添加 `/Applications/Claude Status.app` 并勾选（它 `LSUIElement`、没有 Dock 图标，列表里可能只在"已允许"里出现）。
- **自动化**：首次点击卡片时会弹「"Claude Status"想控制"System Events"」→ 允许。
- 重置（排查用）：`tccutil reset Accessibility com.kaibinwa.claude-status`。
- 症状对照：`-1743` = 自动化没给；`-25211` / `not allowed assistive access` / `-1728` = 辅助功能没给（`jump.log` 里会记）。

**不需要**给"桌面文件夹"权限——打包版装在 `/Applications`，只读 `~/.claude/status/*`、`~/.codex/*`、`~/Library/Application Support/Code/...`，都不受 TCC 保护。（把源码放 `~/Desktop` 且用 venv 直跑时才需要桌面权限，那是终端身份的授权，跟 app 无关。）

## 5. 启动 / 自启

- 启动：`open -a "Claude Status"`（或 Finder 双击）。
- 自启（可选）：系统设置 → 通用 → 登录项，添加 `/Applications/Claude Status.app`；或写一个 `RunAtLoad` 的 LaunchAgent。
- 退出：右键浮窗 → 退出。

## 6. 改了代码之后怎么办

- **快点看效果**：`./venv/bin/python main.py`（不打包；但此时 TCC 身份是终端/Python，不是 `Claude Status`，跳转可能退回 `code` 兜底路径）。
- **要长期可用的那份**：重跑 1→2→3（构建 / 签名 / 安装），核对 DR，然后启动。

## 排障速查

| 现象 | 原因 / 处理 |
|---|---|
| 构建最后 `Error while signing the bundle ... timestamp service is not available` | 重跑 codesign 一次；仍失败去掉 `--timestamp` |
| `codesign ... no identity found` | 证书/私钥不在 login.keychain → 重建证书（会换 DR，要重新授权） |
| 启动即崩、`dlopen` 报 `different Team IDs` | 签名时没带 `entitlements.plist`（`disable-library-validation`） |
| 列表里辅助功能勾还在但跳转失效 / `-1728` | 签名身份变了（ad-hoc 签名每次重建都变身份）→ 重新勾选，并确认用证书签名 |
| 窗口起不来、报 `Operation not permitted: .../venv/pyvenv.cfg` | 你是从 `~/Desktop` 里直接跑 app bundle（桌面 TCC）；装到 `/Applications` 或从终端跑源码即可 |
| 不进 Dock / Cmd+Tab，但窗口正常 | 预期行为（`LSUIElement=True`） |
