"""Claude 多 Session 状态提示器 — 无边框透明浮窗（PyQt6）。

用法：python3 main.py
"""
import sys
import time

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                             QMenu, QToolTip, QVBoxLayout, QWidget)

import jump
from state import (STATUS_ATTENTION, STATUS_IDLE, STATUS_LABEL,
                   STATUS_WORKING, StateTracker)

COLORS = {
    STATUS_WORKING: "#4CAF50",
    STATUS_IDLE: "#9E9E9E",
    STATUS_ATTENTION: "#F44336",
}

# idle 变体（status_note）颜色
NOTE_COLORS = {
    "已中断": "#FF9800",      # 橙：用户中止
    "API 错误": "#9C27B0",    # 紫：异常停止
}

JUMP_DEBOUNCE = 2.0   # 秒：去抖窗口，防连点排队

JUMP_LOG = "/Users/kaibinwa/.claude/status/jump.log"   # 临时排查日志


def _jlog(msg):
    try:
        with open(JUMP_LOG, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


class JumpThread(QThread):
    """后台执行跳转，不阻塞 UI（跳转链路 ~0.2-1s）。"""

    def __init__(self, project):
        super().__init__()
        self.project = project
        self.ok = True
        self.msg = ""

    def run(self):
        try:
            self.ok, self.msg = jump.jump_to_project(self.project)
        except Exception as e:
            self.ok, self.msg = False, f"异常: {e!r}"
            import traceback
            _jlog("JumpThread exception:\n" + traceback.format_exc())
        _jlog(f"run done project={self.project!r} ok={self.ok} msg={self.msg!r}")


class Card(QFrame):
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self._press_pos = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(3)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("font-size:14px;")
        self.name = QLabel("")
        self.name.setStyleSheet("color:#EEE;font-weight:bold;font-size:13px;")
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("font-size:11px;font-weight:bold;")
        row.addWidget(self.dot)
        row.addWidget(self.name, 1)
        row.addWidget(self.status_label)
        layout.addLayout(row)

        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet("color:#999;font-size:11px;")
        layout.addWidget(self.msg)

    def set_state(self, s):
        color = COLORS.get(s.status, "#999")
        self.dot.setStyleSheet(f"color:{color};font-size:14px;")
        self.name.setText(s.project.split("/")[-1] if s.project and s.project != "?" else s.project)
        self.name.setToolTip(s.project)
        label = s.attention_reason if s.status == STATUS_ATTENTION else STATUS_LABEL.get(s.status, s.status)
        if s.status == STATUS_IDLE and s.status_note:
            # idle 变体：已中断 / API 错误（用对应颜色区分）
            label = s.status_note
            for note, nc in NOTE_COLORS.items():
                if note in s.status_note:
                    color = nc
                    break
        self.status_label.setText(label)
        self.status_label.setStyleSheet(f"color:{color};font-size:11px;font-weight:bold;")
        self.msg.setText(s.last_message if s.last_message else "…")
        border = f"2px solid {color}" if s.status == STATUS_ATTENTION else "1px solid #3a3a40"
        self.setStyleSheet(
            f"QFrame#card {{ background: rgba(28,28,34,0.93); border-radius:10px; border:{border}; }}"
        )

    def set_blink(self, on):
        if self.status_label.text() == "等待批准" or "等待" in self.status_label.text():
            base = "rgba(28,28,34,0.93)" if not on else "rgba(70,25,25,0.96)"
            self.setStyleSheet(
                f"QFrame#card {{ background: {base}; border-radius:10px; border:2px solid #F44336; }}"
            )

    def show_jumping(self):
        """点击后的即时反馈：状态标签切为"跳转中…"（500ms 后 _rebuild 自动恢复）。"""
        self.status_label.setText("跳转中…")
        self.status_label.setStyleSheet("color:#2196F3;font-size:11px;font-weight:bold;")

    # --- 点击跳转 / 按住拖动 ---
    def enterEvent(self, e):
        jump.prefetch()   # 预热窗口列表，点击时就不用等 code --status
        super().enterEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            if (e.position().toPoint() - self._press_pos).manhattanLength() > 8:
                self._press_pos = None   # 进入拖动，不再视为点击
                hw = self.window().windowHandle()
                if hw:
                    hw.startSystemMove()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._press_pos is not None:
            moved = (e.position().toPoint() - self._press_pos).manhattanLength()
            self._press_pos = None
            if moved < 6:
                self.clicked.emit()
        super().mouseReleaseEvent(e)


class FloatingWindow(QWidget):
    def __init__(self, tracker):
        super().__init__()
        self.tracker = tracker
        self.cards = {}

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Claude Sessions")
        self.setFixedWidth(300)

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(8, 8, 8, 8)
        self.layout.setSpacing(8)

        self.empty = QLabel("暂无活跃 Claude session\n（等 hooks 事件…）")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("color:#666;font-size:12px;padding:20px;")
        self.layout.addWidget(self.empty)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)

        self.blink_timer = QTimer(self)
        self.blink_timer.timeout.connect(self._blink)
        self.blink_timer.start(600)
        self._blink_on = False
        self._last_jump = 0.0
        self._jump_threads = []

    # --- 事件 ---
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()
        super().mousePressEvent(e)

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_action)
        menu.exec(e.globalPos())

    # --- 刷新 ---
    def refresh(self):
        self.tracker.update()
        self.tracker.tick()
        self._rebuild()

    def _rebuild(self):
        # visible_sessions 而非 sessions：滤掉 /clear 产生的 bridge session
        sessions = self.tracker.visible_sessions()
        order = {STATUS_ATTENTION: 0, STATUS_WORKING: 1, STATUS_IDLE: 2}
        sessions.sort(key=lambda s: (order.get(s.status, 3), s.project))
        visible_ids = {s.session_id for s in sessions}

        for sid, card in list(self.cards.items()):
            if sid not in visible_ids:
                self.layout.removeWidget(card)
                card.deleteLater()
                del self.cards[sid]

        for s in sessions:
            card = self.cards.get(s.session_id)
            if card is None:
                card = Card()
                card.clicked.connect(lambda sid=s.session_id: self._jump(sid))
                self.cards[s.session_id] = card
                self.layout.addWidget(card)
            card.set_state(s)

        self.empty.setVisible(not sessions)
        self.adjustSize()

    def _blink(self):
        self._blink_on = not self._blink_on
        if not self.tracker.has_attention():
            self.setWindowOpacity(1.0)
            return
        self.setWindowOpacity(0.9 if self._blink_on else 1.0)

    def _jump(self, sid):
        _jlog(f"_jump sid={sid}")
        now = time.monotonic()
        if now - self._last_jump < JUMP_DEBOUNCE:
            _jlog("debounced")
            return   # 去抖：防连点排队
        self._last_jump = now
        s = self.tracker.sessions.get(sid)
        if not s:
            _jlog("no session")
            return
        card = self.cards.get(sid)
        if card:
            card.show_jumping()
        t = JumpThread(s.project)
        t.finished.connect(self._jump_finished)
        self._jump_threads.append(t)
        t.start()
        _jlog(f"thread started project={s.project!r}")

    def _jump_finished(self):
        t = self.sender()
        _jlog(f"_jump_finished sender={t!r}")
        try:
            if t in self._jump_threads:
                self._jump_threads.remove(t)   # 释放引用；线程已结束，析构安全
        except Exception as e:
            _jlog(f"remove error: {e!r}")
        if t and not t.ok:
            QToolTip.showText(self.mapToGlobal(self.rect().center()), f"跳转失败：{t.msg}")


def main():
    app = QApplication(sys.argv)
    tracker = StateTracker()
    win = FloatingWindow(tracker)
    win.show()

    # 初始放屏幕右上角
    screen = QApplication.primaryScreen().availableGeometry()
    win.adjustSize()
    win.move(screen.right() - win.width() - 32, screen.top() + 32)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
