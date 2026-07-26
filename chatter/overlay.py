"""A small pill that slides up from the bottom of the screen while
push-to-talk is active — visual confirmation the hotkey was heard, mirrors
what Wispr Flow shows. Never steals focus from whatever you're typing into.
"""

from PyQt6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget

WIDTH = 280
HEIGHT = 48
BOTTOM_MARGIN = 28

STATE_COLORS = {
    "listening": "#5b8dee",
    "working": "#e0a83c",
    "done": "#4caf6d",
    "error": "#e05c5c",
}


class _Dot(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(10, 10)
        self._color = QColor(STATE_COLORS["listening"])

    def set_color(self, hex_color: str):
        self._color = QColor(hex_color)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(self._color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, 10, 10)


class Overlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.resize(WIDTH, HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 0, 18, 0)
        layout.setSpacing(10)
        self._dot = _Dot()
        layout.addWidget(self._dot)
        self._label = QLabel("Listening…")
        self._label.setStyleSheet("color: white; font-size: 13px; font-weight: 500;")
        layout.addWidget(self._label, stretch=1)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._slide_out)

        self._animation = QPropertyAnimation(self, b"pos")
        self._animation.setDuration(220)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hiding = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(30, 31, 34, 235))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect(), HEIGHT / 2, HEIGHT / 2)

    def _target_x_and_ys(self):
        screen = QApplication.primaryScreen().geometry()
        x = screen.x() + (screen.width() - WIDTH) // 2
        resting_y = screen.y() + screen.height() - HEIGHT - BOTTOM_MARGIN
        hidden_y = screen.y() + screen.height() + HEIGHT
        return x, resting_y, hidden_y

    def _animate_to(self, x: int, y: int):
        self._animation.stop()
        self._animation.setStartValue(self.pos())
        self._animation.setEndValue(QPoint(x, y))
        self._animation.start()

    def show_state(self, state: str, text: str):
        self._hiding = False
        self._dot.set_color(STATE_COLORS.get(state, STATE_COLORS["listening"]))
        self._label.setText(text)
        self._hide_timer.stop()

        x, resting_y, hidden_y = self._target_x_and_ys()
        if not self.isVisible():
            self.move(x, hidden_y)
            self.show()
        self._animate_to(x, resting_y)

    def flash_and_hide(self, state: str, text: str, delay_ms: int = 2200):
        self.show_state(state, text)
        self._hide_timer.start(delay_ms)

    def _slide_out(self):
        if self._hiding:
            return
        self._hiding = True
        x, _resting_y, hidden_y = self._target_x_and_ys()
        self._animate_to(x, hidden_y)
        QTimer.singleShot(self._animation.duration() + 20, self._finish_hide)

    def _finish_hide(self):
        if self._hiding:
            self.hide()
            self._hiding = False
