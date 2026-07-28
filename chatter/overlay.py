"""A small HUD that slides in from the bottom-right corner of the screen
while push-to-talk is active — an animated waveform while listening, a live
caption of what's being transcribed, and a checkmark/warning glyph when
done. Never steals focus from whatever you're typing into.
"""

import logging
import math

import AppKit
import objc
import Quartz
from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    pyqtProperty,
)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QWidget

logger = logging.getLogger("chatter.overlay")


def _active_screen_geometry():
    """Which screen to show the overlay on: wherever the frontmost app's
    window actually is — not the mouse cursor, which may be resting on a
    different monitor than the one you're looking at/typing into (confirmed:
    with Claude.app on the built-in display but the cursor idle on an
    external monitor, the overlay was appearing on the wrong screen)."""
    try:
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        frontmost = workspace.frontmostApplication()
        if frontmost is not None:
            pid = frontmost.processIdentifier()
            app_name = frontmost.localizedName()
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
            )
            layer0_found = False
            for w in windows:
                if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0:
                    layer0_found = True
                    bounds = w.get("kCGWindowBounds")
                    center = QPoint(
                        int(bounds["X"] + bounds["Width"] / 2),
                        int(bounds["Y"] + bounds["Height"] / 2),
                    )
                    screen = QApplication.screenAt(center)
                    logger.info(
                        "frontmost=%r pid=%s bounds=%s -> screen=%s",
                        app_name, pid, dict(bounds), screen.name() if screen else None,
                    )
                    if screen:
                        return screen.geometry()
                    break
            if not layer0_found:
                logger.warning("frontmost=%r pid=%s had no on-screen layer-0 window — falling back to primaryScreen", app_name, pid)
        else:
            logger.warning("no frontmost application found — falling back to primaryScreen")
    except Exception:
        logger.exception("couldn't determine the active app's screen")
    return QApplication.primaryScreen().geometry()

# Qt's WindowStaysOnTopHint alone only floats above normal windows on the
# *current* Space — it does not appear over a different app's fullscreen
# Space or a different virtual desktop. That needs the underlying NSWindow's
# collectionBehavior/level set directly; Qt has no portable API for this.
_NATIVE_COLLECTION_BEHAVIOR = (
    AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
    | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
    | AppKit.NSWindowCollectionBehaviorStationary
    | AppKit.NSWindowCollectionBehaviorIgnoresCycle
)


def _make_overlay_appear_everywhere(widget):
    try:
        ns_view = objc.objc_object(c_void_p=int(widget.winId()))
        ns_window = ns_view.window()
        ns_window.setCollectionBehavior_(_NATIVE_COLLECTION_BEHAVIOR)
        ns_window.setLevel_(AppKit.NSPopUpMenuWindowLevel)
    except Exception:
        logger.exception("couldn't set native window level — overlay may not show over other apps")

HEIGHT = 56
MIN_WIDTH = 190
MAX_WIDTH = 440
RIGHT_MARGIN = 26
BOTTOM_MARGIN = 30
ICON_SIZE = 26

STATE_COLORS = {
    "listening": QColor("#5b8dee"),
    "working": QColor("#e0a83c"),
    "done": QColor("#4caf6d"),
    "error": QColor("#e0605c"),
}

_BAR_COUNT = 4


class _Icon(QWidget):
    """Animated waveform while busy; a checkmark or warning glyph at rest."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(ICON_SIZE, ICON_SIZE)
        self._color = STATE_COLORS["listening"]
        self._phase = 0.0
        self._mode = "wave"  # "wave" | "check" | "warn"
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(60)

    def set_state(self, state: str):
        self._color = STATE_COLORS.get(state, STATE_COLORS["listening"])
        self._mode = {"done": "check", "error": "warn"}.get(state, "wave")
        self.update()

    def _tick(self):
        if self._mode == "wave":
            self._phase += 0.35
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)

        if self._mode == "wave":
            bar_w = 3.4
            gap = 3.2
            total_w = _BAR_COUNT * bar_w + (_BAR_COUNT - 1) * gap
            x0 = (self.width() - total_w) / 2
            mid = self.height() / 2
            for i in range(_BAR_COUNT):
                amp = 0.35 + 0.65 * abs(math.sin(self._phase + i * 0.9))
                h = max(4.0, amp * (self.height() - 6))
                x = x0 + i * (bar_w + gap)
                painter.drawRoundedRect(QRectF(x, mid - h / 2, bar_w, h), 1.7, 1.7)
        elif self._mode == "check":
            pen = QPen(self._color, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath()
            w, h = self.width(), self.height()
            path.moveTo(w * 0.22, h * 0.55)
            path.lineTo(w * 0.42, h * 0.74)
            path.lineTo(w * 0.80, h * 0.30)
            painter.drawPath(path)
        elif self._mode == "warn":
            font = painter.font()
            font.setPointSize(15)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "!")


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
        self.resize(MIN_WIDTH, HEIGHT)
        _make_overlay_appear_everywhere(self)
        self._opacity = 1.0

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 140))
        self.setGraphicsEffect(shadow)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 20, 0)
        layout.setSpacing(12)
        self._icon = _Icon()
        layout.addWidget(self._icon)

        self._label = QLabel("Listening…")
        self._label.setWordWrap(False)
        font = QFont()
        font.setPointSize(13)
        font.setWeight(QFont.Weight.DemiBold)
        self._label.setFont(font)
        self._label.setStyleSheet("color: #f2f2f4;")
        layout.addWidget(self._label, stretch=1)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._slide_out)

        self._move_anim = QPropertyAnimation(self, b"pos")
        self._move_anim.setDuration(300)
        self._move_anim.setEasingCurve(QEasingCurve.Type.OutBack)

        self._fade_anim = QPropertyAnimation(self, b"hudOpacity")
        self._fade_anim.setDuration(220)

        self._hiding = False
        self._screen_geometry = None

    # windowOpacity as an animatable Qt property -------------------------

    def _get_opacity(self):
        return self._opacity

    def _set_opacity(self, value):
        self._opacity = value
        self.setWindowOpacity(value)

    hudOpacity = pyqtProperty(float, _get_opacity, _set_opacity)

    # painting -------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(self.rect()).adjusted(0, 0, -0.5, -0.5)
        radius = HEIGHT / 2

        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(38, 39, 43, 240))
        gradient.setColorAt(1.0, QColor(24, 25, 28, 240))
        painter.setBrush(gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, radius, radius)

        highlight = QPen(QColor(255, 255, 255, 22), 1)
        painter.setPen(highlight)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

    # sizing -----------------------------------------------------------

    def _fit_width(self, text: str) -> int:
        metrics = QFontMetrics(self._label.font())
        content_w = ICON_SIZE + 12 + 16 + 20 + metrics.horizontalAdvance(text)
        return max(MIN_WIDTH, min(MAX_WIDTH, content_w))

    # positioning --------------------------------------------------------

    def _target_x_and_ys(self, width: int):
        if self._screen_geometry is None:
            self._screen_geometry = _active_screen_geometry()
        screen = self._screen_geometry
        resting_x = screen.x() + screen.width() - width - RIGHT_MARGIN
        resting_y = screen.y() + screen.height() - HEIGHT - BOTTOM_MARGIN
        hidden_x = screen.x() + screen.width() + width
        hidden_y = screen.y() + screen.height() + HEIGHT
        return (resting_x, resting_y), (hidden_x, hidden_y)

    def _animate_to(self, x: int, y: int):
        self._move_anim.stop()
        self._move_anim.setStartValue(self.pos())
        self._move_anim.setEndValue(QPoint(x, y))
        self._move_anim.start()

    def _apply_text(self, text: str):
        self._label.setText(text)
        width = self._fit_width(text)
        current_top_right = self.x() + self.width()
        self.resize(width, HEIGHT)
        # keep the right edge anchored so the pill grows/shrinks leftward,
        # not off the edge of the screen
        if self.isVisible():
            self.move(current_top_right - width, self.y())

    # public API -----------------------------------------------------------

    def show_state(self, state: str, text: str):
        self._hiding = False
        self._icon.set_state(state)
        self._hide_timer.stop()

        width = self._fit_width(text)
        (rx, ry), (hx, hy) = self._target_x_and_ys(width)
        if not self.isVisible():
            self.resize(width, HEIGHT)
            self.move(hx, hy)
            self._opacity = 0.0
            self.setWindowOpacity(0.0)
            self.show()
            logger.info("overlay shown: resting at (%d, %d) size %dx%d", rx, ry, width, HEIGHT)
            self._fade_anim.stop()
            self._fade_anim.setStartValue(0.0)
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.start()
        self._label.setText(text)
        self.resize(width, HEIGHT)
        self._animate_to(rx, ry)

    def update_live_text(self, text: str):
        """Updates the caption in place while listening — grows the pill to
        fit, anchored to the right edge, without touching the hide timer."""
        if self.isVisible():
            self._apply_text(text)

    def flash_and_hide(self, state: str, text: str, delay_ms: int = 2200):
        self.show_state(state, text)
        self._hide_timer.start(delay_ms)

    def _slide_out(self):
        if self._hiding:
            return
        self._hiding = True
        (_rx, _ry), (hx, hy) = self._target_x_and_ys(self.width())
        self._animate_to(hx, hy)
        self._fade_anim.stop()
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()
        QTimer.singleShot(self._move_anim.duration() + 20, self._finish_hide)

    def _finish_hide(self):
        if self._hiding:
            self.hide()
            self._hiding = False
            self._screen_geometry = None
