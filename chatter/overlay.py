"""A small HUD docked at the physical notch (on a notched MacBook display) or
bottom-right (fallback — external monitors, older Macs) that appears the
instant push-to-talk starts: a small mascot on the left, a short
backend-rotated phrase on the right. Never steals focus from whatever you're
typing into.

Deliberately no slide-in/slide-out: it appears directly at rest with a very
quick fade (default Qt show() is a hard cut, which reads as a glitch more
than "instant" — a ~90ms fade avoids that pop without being a perceptible
animation) and fades out in place on hide, no travel distance either way.
"""

import logging

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

from . import phrases
from . import theme
from .mascot import Mascot

logger = logging.getLogger("chatter.overlay")


def _active_screen_geometry():
    """Which screen to show the HUD on: wherever the frontmost app's window
    actually is — not the mouse cursor, which may be resting on a different
    monitor than the one you're looking at/typing into (confirmed: with
    Claude.app on the built-in display but the cursor idle on an external
    monitor, the HUD was appearing on the wrong screen).

    Picks the *largest* on-screen window owned by the frontmost app, not
    specifically a "layer 0" one — inspecting Claude.app directly showed its
    main window sits at layer 1000, not 0, so requiring layer 0 meant this
    always fell back to primaryScreen for it."""
    try:
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        frontmost = workspace.frontmostApplication()
        if frontmost is not None:
            pid = frontmost.processIdentifier()
            app_name = frontmost.localizedName()
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
            )
            best = None
            best_area = 0
            for w in windows:
                if w.get("kCGWindowOwnerPID") != pid:
                    continue
                bounds = w.get("kCGWindowBounds")
                if not bounds:
                    continue
                area = bounds["Width"] * bounds["Height"]
                if area > best_area:
                    best_area = area
                    best = bounds
            if best is not None:
                center = QPoint(
                    int(best["X"] + best["Width"] / 2),
                    int(best["Y"] + best["Height"] / 2),
                )
                screen = QApplication.screenAt(center)
                logger.info(
                    "frontmost=%r pid=%s largest window bounds=%s -> screen=%s",
                    app_name, pid, dict(best), screen.name() if screen else None,
                )
                if screen:
                    return screen.geometry()
            else:
                logger.warning("frontmost=%r pid=%s had no on-screen window — falling back to primaryScreen", app_name, pid)
        else:
            logger.warning("no frontmost application found — falling back to primaryScreen")
    except Exception:
        logger.exception("couldn't determine the active app's screen")
    return QApplication.primaryScreen().geometry()


def _matching_ns_screen(qt_geometry):
    """Finds the AppKit NSScreen matching a Qt QScreen.geometry() rect, so
    the notch APIs (AppKit-only, no Qt equivalent) can be queried for the
    same physical display Qt already picked."""
    try:
        for ns in AppKit.NSScreen.screens():
            frame = ns.frame()
            if abs(frame.size.width - qt_geometry.width()) < 1 and abs(frame.size.height - qt_geometry.height()) < 1:
                return ns
    except Exception:
        logger.exception("couldn't enumerate NSScreens")
    return None


def _notch_geometry(qt_geometry):
    """(center_x, top_y, notch_width) in Qt global (top-left-origin)
    coordinates for the physical notch on the given screen, or None if that
    screen has no notch (external monitors, older Macs).

    auxiliaryTopLeftArea()/auxiliaryTopRightArea() (macOS 12+) are the two
    menu-bar-height rects flanking the notch; the gap between them is the
    notch's own span — same technique boring.notch
    (github.com/TheBoredTeam/boring.notch, sizing/matters.swift) uses for
    its closed-pill width. NSScreen frames use Cocoa's bottom-left origin,
    so the y value is flipped (using this screen's own height only —
    deliberately not touching the whole-desktop multi-monitor origin, which
    these local, per-screen offsets don't need)."""
    ns_screen = _matching_ns_screen(qt_geometry)
    if ns_screen is None:
        return None
    try:
        aux_left = ns_screen.auxiliaryTopLeftArea()
        aux_right = ns_screen.auxiliaryTopRightArea()
    except Exception:
        return None
    if aux_left is None or aux_right is None:
        return None
    frame = ns_screen.frame()
    local_notch_left = (aux_left.origin.x - frame.origin.x) + aux_left.size.width
    local_notch_right = aux_right.origin.x - frame.origin.x
    notch_width = local_notch_right - local_notch_left
    if notch_width <= 1:
        return None
    local_center_x = (local_notch_left + local_notch_right) / 2
    local_notch_bottom_cocoa = aux_left.origin.y - frame.origin.y
    local_notch_bottom_top_left = frame.size.height - local_notch_bottom_cocoa
    return (
        qt_geometry.x() + local_center_x,
        qt_geometry.y() + local_notch_bottom_top_left,
        notch_width,
    )


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
        # A panel that hides when Chatter isn't the frontmost app would
        # explain the exact symptom reported — the HUD needs to show
        # precisely while some *other* app (Claude, or anything fullscreen)
        # is frontmost, which is the opposite of when a utility panel
        # defaults to staying visible.
        ns_window.setHidesOnDeactivate_(False)
        ns_window.setCanHide_(False)
        # Confirmed via a standalone PyObjC check that Qt's frameless Tool
        # window really is an NSPanel (QNSPanel) under the hood — so it's
        # not a plain-NSWindow-vs-NSPanel gap. But that same check found
        # isFloatingPanel defaults to False and canBecomeKeyWindow defaults
        # to True — the opposite of boring.notch's reference window, which
        # explicitly sets isFloatingPanel=true (floating panels are
        # documented to float above the active application's window
        # regardless of which app owns them — exactly the other-app's-
        # fullscreen-Space case here) and canBecomeKey=false. Both are
        # settable at runtime even though Qt created the panel.
        ns_window.setFloatingPanel_(True)
        ns_window.setBecomesKeyOnlyIfNeeded_(True)
        # setFloatingPanel_ has a side effect confirmed live: it silently
        # resets the window's level to NSFloatingWindowLevel (3), clobbering
        # whatever was set before it. setLevel_ has to run AFTER it, not
        # before, or the level below is a no-op. boring.notch's `.mainMenu +
        # 3` (27) is still the target — checked live that a normal app's
        # own window (Claude.app) sits at layer 0, so 27 clears it with
        # room to spare without reaching for an unnecessarily high system
        # level (the previous NSScreenSaverWindowLevel + 1 was likely why
        # the HUD wasn't appearing over *other* apps' fullscreen Spaces at
        # all — levels that high are reserved system territory that the
        # WindowServer doesn't extend the FullScreenAuxiliary cross-Space
        # treatment to the way it does for ordinary elevated levels).
        ns_window.setLevel_(AppKit.NSMainMenuWindowLevel + 3)
        logger.info(
            "native window level set: level=%s collectionBehavior=%s hidesOnDeactivate=%s floating=%s",
            ns_window.level(), ns_window.collectionBehavior(), ns_window.hidesOnDeactivate(),
            ns_window.isFloatingPanel(),
        )
    except Exception:
        logger.exception("couldn't set native window level — HUD may not show over other apps")


def _notch_pill_path(w: float, h: float, bottom_r: float) -> QPainterPath:
    """A plain rectangle flush with the notch's own top-left/top-right
    corners (square, no curve — so there's no seam to line up against the
    notch's own corner radius) and a rounded bottom, so it reads as the
    notch itself growing a rounded tab downward. An earlier version tried
    to mirror the notch's own concave corner curve at the top (matching
    boring.notch's NotchShape.swift exactly); that added complexity without
    reading as cleaner in practice, so this starts from the simplest shape
    that still sells "expanding out of the notch" and can be refined later."""
    path = QPainterPath()
    path.moveTo(0, 0)
    path.lineTo(w, 0)
    path.lineTo(w, h - bottom_r)
    path.quadTo(w, h, w - bottom_r, h)
    path.lineTo(bottom_r, h)
    path.quadTo(0, h, 0, h - bottom_r)
    path.lineTo(0, 0)
    path.closeSubpath()
    return path

HEIGHT = 38
MIN_WIDTH = 190  # content floor — long phrases elide rather than growing past this
RIGHT_MARGIN = 26
BOTTOM_MARGIN = 30
MASCOT_SIZE = 26
BOTTOM_RADIUS = 14
# A little wider than the bare notch so it fully covers the notch's own
# rounded corners with no hairline gap at the seam.
NOTCH_WIDTH_PADDING = 8
# The physical notch casing is flat black, not the app's dark-brown theme
# background — matching it exactly (rather than theme.BG, which is a warm
# near-black but still visibly brown next to the real thing) is what sells
# the "the notch itself grew taller" illusion.
NOTCH_BLACK = "#000000"


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

        # Only the fallback (no-notch) pill gets a drop shadow, since it's
        # meant to read as a small floating HUD. In notch mode the whole
        # point is that it reads as the physical notch itself growing
        # taller — a shadow around it would break that illusion by making
        # it look like a separate panel hovering under the notch.
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(24)
        self._shadow.setOffset(0, 6)
        self._shadow.setColor(QColor(0, 0, 0, 140))
        self.setGraphicsEffect(self._shadow)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(10)
        self._mascot = Mascot(size=MASCOT_SIZE)
        layout.addWidget(self._mascot)

        self._label = QLabel("")
        self._label.setWordWrap(False)
        font = QFont()
        font.setPointSize(12)
        font.setWeight(QFont.Weight.DemiBold)
        self._label.setFont(font)
        self._label.setStyleSheet(f"color: {theme.TEXT};")
        layout.addWidget(self._label, stretch=1)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

        # Only used to reposition an already-visible pill if the target
        # screen changes mid-session (rare) — not for show/hide anymore.
        self._move_anim = QPropertyAnimation(self, b"pos")
        self._move_anim.setDuration(160)
        self._move_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._fade_anim = QPropertyAnimation(self, b"hudOpacity")
        self._fade_anim.setDuration(90)

        self._hiding = False
        self._screen_geometry = None
        self._state = None
        self._notch_mode = False

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

        w, h = self.width(), self.height()
        if self._notch_mode:
            path = _notch_pill_path(w, h, BOTTOM_RADIUS)
            # True notch black, not the terracotta surface gradient — the
            # goal is for this shape to be indistinguishable from the real
            # notch's own casing, just taller, so it reads as the notch
            # itself expanding rather than a themed panel appearing below
            # it. The mascot keeps its normal state colors (untouched here,
            # painted separately) — only this background is forced to black.
            painter.setBrush(QColor(NOTCH_BLACK))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(path)
        else:
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
            gradient = QLinearGradient(0, 0, 0, h)
            gradient.setColorAt(0.0, QColor(theme.SURFACE2))
            gradient.setColorAt(1.0, QColor(theme.BG))
            painter.setBrush(gradient)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(path)
            painter.setPen(QPen(theme.border_color(), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

    # text -----------------------------------------------------------

    def _set_label_text(self, text: str):
        metrics = QFontMetrics(self._label.font())
        available = self.width() - (MASCOT_SIZE + 16 + 10 + 16)
        self._label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(20, available)))

    # positioning --------------------------------------------------------

    def _compute_geometry(self):
        """(x, y, width, notch_mode) for the HUD's resting position — width
        varies because a notch-docked pill is sized to that notch (plus
        boring.notch's own small overlap padding), not a fixed content box."""
        if self._screen_geometry is None:
            self._screen_geometry = _active_screen_geometry()
        screen = self._screen_geometry
        notch = _notch_geometry(screen)
        if notch is not None:
            center_x, notch_bottom_y, notch_width = notch
            width = max(MIN_WIDTH, int(notch_width) + NOTCH_WIDTH_PADDING)
            return int(center_x - width / 2), int(notch_bottom_y), width, True
        # Fallback: no notch on the active screen (external monitor, older
        # Mac) — dock bottom-right instead, same spot this used before.
        width = MIN_WIDTH
        x = screen.x() + screen.width() - width - RIGHT_MARGIN
        y = screen.y() + screen.height() - HEIGHT - BOTTOM_MARGIN
        return x, y, width, False

    def _animate_to(self, x: int, y: int):
        self._move_anim.stop()
        self._move_anim.setStartValue(self.pos())
        self._move_anim.setEndValue(QPoint(x, y))
        self._move_anim.start()

    # public API -----------------------------------------------------------

    def show_state(self, state: str, detail: str = "", phrase: str | None = None):
        """`detail` is only shown verbatim for state == "error" (real,
        actionable error text). Every other state shows a short phrase —
        `phrase` if the caller passed one (so the HUD and the Live Dictation
        tab can show the *same* rotated phrase at the same moment, instead
        of each independently picking their own), otherwise one picked here
        from phrases.py."""
        self._hiding = False
        mascot_state = "listening" if state == "listening" else state
        self._mascot.set_state(mascot_state)
        self._hide_timer.stop()

        text = detail if state == "error" else (phrase if phrase is not None else phrases.pick(state))
        if state != self._state:
            self._state = state

        rx, ry, width, notch_mode = self._compute_geometry()
        self._notch_mode = notch_mode
        self._shadow.setEnabled(not notch_mode)
        self.resize(width, HEIGHT)
        if not self.isVisible():
            # Appears directly at rest — no slide from off-screen. That
            # slide was the main source of felt lag; press-to-visible should
            # be as close to instant as a fade can make it.
            self.move(rx, ry)
            self._opacity = 0.0
            self.setWindowOpacity(0.0)
            # Re-applied on every show(), not just once in __init__: macOS/Qt
            # can hand the widget a new native backing window across
            # hide/show cycles, silently reverting the elevated level set
            # earlier — which would leave the HUD at a normal window level,
            # visible only when nothing else is in front of it. Done BEFORE
            # show() (not after) so the window is never briefly ordered onto
            # screen with default (non-cross-Space) properties first.
            _make_overlay_appear_everywhere(self)
            self.show()
            logger.info("HUD shown: resting at (%d, %d) size %dx%d notch_mode=%s", rx, ry, width, HEIGHT, notch_mode)
            self._fade_anim.stop()
            self._fade_anim.setStartValue(0.0)
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.start()
        else:
            # Already up (e.g. listening -> processing mid-hold) — only
            # worth animating if the target screen actually changed.
            self._animate_to(rx, ry)
        self._set_label_text(text)

    def flash_and_hide(self, state: str, detail: str = "", delay_ms: int = 2200, phrase: str | None = None):
        self.show_state(state, detail, phrase=phrase)
        self._hide_timer.start(delay_ms)

    def _fade_out(self):
        if self._hiding:
            return
        self._hiding = True
        self._fade_anim.stop()
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()
        QTimer.singleShot(self._fade_anim.duration() + 20, self._finish_hide)

    def _finish_hide(self):
        if self._hiding:
            self.hide()
            self._hiding = False
            self._screen_geometry = None
