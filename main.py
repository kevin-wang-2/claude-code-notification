"""Claude 多 Session 状态提示器 — 无边框透明浮窗（PyQt6）。

用法：python3 main.py
"""
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                             QMenu, QVBoxLayout, QWidget)

from state import (STATUS_ATTENTION, STATUS_IDLE, STATUS_LABEL,
                   STATUS_WORKING, StateTracker)

COLORS = {
    STATUS_WORKING: "#4CAF50",
    STATUS_IDLE: "#9E9E9E",
    STATUS_ATTENTION: "#F44336",
}


class Card(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
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
        sessions = list(self.tracker.sessions.values())
        order = {STATUS_ATTENTION: 0, STATUS_WORKING: 1, STATUS_IDLE: 2}
        sessions.sort(key=lambda s: (order.get(s.status, 3), s.project))

        for sid, card in list(self.cards.items()):
            if sid not in self.tracker.sessions:
                self.layout.removeWidget(card)
                card.deleteLater()
                del self.cards[sid]

        for s in sessions:
            card = self.cards.get(s.session_id)
            if card is None:
                card = Card()
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
