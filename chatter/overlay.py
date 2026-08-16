"""A small HUD docked at the physical notch (on a notched MacBook display) or
bottom-right (fallback — external monitors, older Macs) that appears the
instant push-to-talk starts: a small mascot on the left, a literal state or
live transcript preview on the right. Never steals focus from whatever you're
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
from Foundation import NSNotificationCenter, NSOperationQueue
from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    pyqtProperty,
)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PyQt6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QLabel, QWidget

from . import phrases
from . import theme
from .mascot import Mascot

logger = logging.getLogger("chatter.overlay")


def _frontmost_app_screen_geometry():
    """Return the screen containing the frontmost app's largest window."""
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
                logger.warning(
                    "frontmost=%r pid=%s had no on-screen window",
                    app_name, pid,
                )
        else:
            logger.warning("no frontmost application found")
    except Exception:
        logger.exception("couldn't determine the active app's screen")
    return None


def _built_in_screen_geometry():
    for screen in QApplication.screens():
        if _notch_geometry(screen.geometry()) is not None:
            return screen.geometry()
    return None


def _active_screen_geometry(display_mode="notch"):
    """Choose the HUD's display.

    The primary HUD is anchored to the physical MacBook notch, including
    while another app is fullscreen on an external monitor. A second HUD can
    use ``display_mode="active"`` to mirror the same status onto the display
    containing the frontmost app, so the feedback is still visible when the
    user is looking only at an attached monitor.

    The frontmost app's *largest* on-screen window is used rather than the
    mouse cursor, which may be resting on a different monitor. This also
    handles normal windows and fullscreen windows consistently.
    """
    if display_mode == "active":
        active = _frontmost_app_screen_geometry()
        if active is not None:
            logger.info("using active app display for HUD: %s", active)
            return active

    built_in = _built_in_screen_geometry()
    if built_in is not None:
        logger.info("using built-in notched display for HUD: %s", built_in)
        return built_in

    active = _frontmost_app_screen_geometry()
    if active is not None:
        logger.info("using active app display for HUD fallback: %s", active)
        return active

    primary = QApplication.primaryScreen()
    return primary.geometry() if primary is not None else None


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
    AppKit.NSWindowCollectionBehaviorCanJoinAllApplications
    | AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
    | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
    | AppKit.NSWindowCollectionBehaviorStationary
    | AppKit.NSWindowCollectionBehaviorIgnoresCycle
)

# boring.notch's own reference level (github.com/TheBoredTeam/boring.notch)
# — this is not a private number. Confirmed live via CGWindowListCopyWindowInfo
# that the actual boringNotch app, installed and running on this Mac, sits its
# own window at this exact layer. Two apps both targeting the same level race
# on every orderFrontRegardless() call; whichever reasserts more recently
# wins, so a persistent competing app doesn't just flicker over the HUD
# occasionally — it can win every race indefinitely from the moment it starts
# reasserting more often (e.g. right after the user switches apps a lot,
# such as while using a screenshot tool). See _hud_window_level below.
_BASE_HUD_LEVEL = AppKit.NSMainMenuWindowLevel + 3
# Genuinely reserved system chrome (Dock, Spotlight, Control Center, screen
# savers) lives at 1000+ (see the 'Claude' overlay's own screenshot-panel
# window observed at 1000, and Control Center at a few levels above this).
# Only contend with other ordinary elevated windows below that, matching the
# existing reasoning for not reaching into system territory the WindowServer
# won't extend FullScreenAuxiliary cross-Space treatment to.
_MAX_CONTENDED_LEVEL = 200


def _hud_window_level():
    """The level to show the HUD at: one above whatever else is currently
    claiming our base level (or higher, up to _MAX_CONTENDED_LEVEL), so a
    persistent competing notch-style overlay (boringNotch or similar) can't
    permanently win the front-ordering race. Re-scanned on every show() —
    see the call site — so a competitor that starts after Chatter does still
    gets cleared on the next hold-to-talk."""
    try:
        own_pid = AppKit.NSRunningApplication.currentApplication().processIdentifier()
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        level = _BASE_HUD_LEVEL
        for w in windows:
            if w.get("kCGWindowOwnerPID") == own_pid:
                continue
            layer = w.get("kCGWindowLayer")
            if layer is None:
                continue
            if _BASE_HUD_LEVEL <= layer < _MAX_CONTENDED_LEVEL and layer >= level:
                level = layer + 1
        return level
    except Exception:
        logger.exception("couldn't scan on-screen window layers for HUD level")
        return _BASE_HUD_LEVEL


def _make_overlay_appear_everywhere(widget):
    try:
        ns_view = objc.objc_object(c_void_p=int(widget.winId()))
        ns_window = ns_view.window()

        # Mirror boring.notch's native window recipe. Use the small,
        # intentional non-activating panel mask instead of preserving Qt's
        # Tool/HUD bits. The latter can make WindowServer treat this as a
        # regular application window after a screen recorder or fullscreen
        # Space changes ownership of the active surface.
        style_mask = (
            getattr(AppKit, "NSWindowStyleMaskBorderless", 0)
            | getattr(AppKit, "NSWindowStyleMaskNonactivatingPanel", 0)
            | getattr(AppKit, "NSWindowStyleMaskUtilityWindow", 0)
            | getattr(AppKit, "NSWindowStyleMaskHUDWindow", 0)
        )
        ns_window.setStyleMask_(style_mask)
        ns_window.setOpaque_(False)
        ns_window.setHasShadow_(False)
        ns_window.setBackgroundColor_(AppKit.NSColor.clearColor())
        ns_window.setIgnoresMouseEvents_(True)
        ns_window.setHidesOnDeactivate_(False)
        ns_window.setCanHide_(False)
        ns_window.setWorksWhenModal_(True)
        ns_window.setReleasedWhenClosed_(False)
        ns_window.setCollectionBehavior_(_NATIVE_COLLECTION_BEHAVIOR)
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
        ns_window.setBecomesKeyOnlyIfNeeded_(False)
        # AppKit can normalize the style mask when a Qt-created panel is
        # promoted to a floating panel. Reapply the boring.notch-compatible
        # HUD mask after that promotion so a screen/app transition cannot
        # silently turn this back into an ordinary utility window.
        ns_window.setStyleMask_(style_mask)
        if hasattr(AppKit, "NSWindowAnimationBehaviorNone"):
            ns_window.setAnimationBehavior_(AppKit.NSWindowAnimationBehaviorNone)
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
        # treatment to the way it does for ordinary elevated levels). Not a
        # flat 27 though — see _hud_window_level: a real notch-style utility
        # can and does sit at that exact shared level too.
        ns_window.setLevel_(_hud_window_level())
        logger.info(
            "native HUD panel configured: windowNumber=%s styleMask=%s level=%s collectionBehavior=%s hidesOnDeactivate=%s floating=%s",
            ns_window.windowNumber(), ns_window.styleMask(),
            ns_window.level(), ns_window.collectionBehavior(), ns_window.hidesOnDeactivate(),
            ns_window.isFloatingPanel(),
        )
    except Exception:
        logger.exception("couldn't set native window level — HUD may not show over other apps")


def _order_overlay_front(widget):
    """Order the native panel without activating Chatter.

    Qt's ``show()`` is normally enough above ordinary windows, but a
    full-screen Space can keep a deactivated auxiliary panel behind the
    full-screen surface even when its collection behavior is correct. The
    AppKit call is deliberately made after show() on every new ordering.
    """
    try:
        ns_view = objc.objc_object(c_void_p=int(widget.winId()))
        ns_window = ns_view.window()
        # windowNumber/level logged at debug so a drift between what
        # _make_overlay_appear_everywhere last configured and what's
        # actually being ordered here (e.g. after Qt silently swaps in a
        # fresh native window) is visible in the log without spamming info
        # level on every 280ms reassert tick.
        logger.debug(
            "ordering HUD front: windowNumber=%s level=%s floating=%s",
            ns_window.windowNumber(), ns_window.level(), ns_window.isFloatingPanel(),
        )
        ns_window.orderFrontRegardless()
    except Exception:
        logger.exception("couldn't order HUD panel above the active Space")


def _notch_pill_path(w: float, h: float, bottom_r: float) -> QPainterPath:
    """A plain rectangle flush with the notch's own top-left/top-right
    corners (square, no curve — so there's no seam to line up against the
    notch's own corner radius) and a rounded bottom, so it reads as the
    notch itself growing a rounded tab downward. An earlier version tried
    to mirror the notch's own concave corner curve at the top (matching
    boring.notch's NotchShape.swift exactly); that added complexity without
    reading as cleaner in practice, so this starts from the simplest shape
    that still sells "expanding out of the notch" and can be refined later."""
    bottom_r = min(bottom_r, h / 2)
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
# Keep one calm, predictable surface for the whole interaction. This is the
# largest width the previous content-driven HUD reached, so existing copy has
# room without making the window resize on every partial ASR update.
HUD_WIDTH = 520
RIGHT_MARGIN = 26
BOTTOM_MARGIN = 30
MASCOT_SIZE = 26
BOTTOM_RADIUS = 14
# The physical notch casing is flat black, not the app's dark-brown theme
# background — matching it exactly (rather than theme.BG, which is a warm
# near-black but still visibly brown next to the real thing) is what sells
# the "the notch itself grew taller" illusion.
NOTCH_BLACK = "#000000"


class Overlay(QWidget):
    def __init__(self, display_mode="notch"):
        super().__init__()
        self._display_mode = display_mode
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.resize(HUD_WIDTH, HEIGHT)
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

        # The label owns the full surface so its text can be geometrically
        # centered. The mascot is a small listening cue layered on the left;
        # it never participates in the text layout and therefore cannot make
        # status messages appear visually off-center.
        self._label = QLabel(self)
        self._label.setWordWrap(False)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = QFont()
        font.setPointSize(12)
        font.setWeight(QFont.Weight.DemiBold)
        self._label.setFont(font)
        # The application-wide QWidget rule supplies a terracotta fill to
        # child widgets unless they opt out. Without this explicit
        # transparency the text looks like it has its own orange card laid
        # over the black notch.
        self._label.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        self._label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._mascot = Mascot(size=MASCOT_SIZE, parent=self)
        self._mascot.setStyleSheet("background: transparent;")
        self._label.raise_()
        self._mascot.raise_()

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

        # App activation, screen recording, and fullscreen transitions can
        # leave a deactivated auxiliary panel behind the newly frontmost
        # surface even though its collection behavior is still correct.
        # While the HUD is visible, quietly re-order only the native panel;
        # this does not steal focus and avoids rebuilding/animating the Qt
        # widget on every tick.
        self._front_reassert_timer = QTimer(self)
        self._front_reassert_timer.setInterval(280)
        self._front_reassert_timer.timeout.connect(self._reassert_front)

        # App switching and display/fullscreen changes are asynchronous
        # WindowServer events. A periodic re-order handles the steady state,
        # but the native notifications let us repair the panel immediately
        # when a new app, Space, recorder, or display configuration appears.
        # This mirrors boring.notch's screen-configuration observer instead
        # of relying on a user toggling fullscreen to force AppKit to repaint.
        self._native_observers = []
        self._register_native_environment_observers()

        # Only used to reposition an already-visible pill if the target
        # screen changes mid-session (rare) — not for show/hide anymore.
        self._move_anim = QPropertyAnimation(self, b"pos")
        self._move_anim.setDuration(160)
        self._move_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # Grow from the physical notch's width into the full HUD surface.
        self._expand_anim = QPropertyAnimation(self, b"geometry")
        self._expand_anim.setDuration(300)
        self._expand_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._fade_anim = QPropertyAnimation(self, b"hudOpacity")
        self._fade_anim.setDuration(90)

        self._hiding = False
        self._screen_geometry = None
        self._physical_notch_width = 0
        self._physical_notch_height = 0
        self._state = None
        self._notch_mode = False
        self._display_text = ""
        self._position_children()

    def _register_native_environment_observers(self):
        def observe(center, name):
            try:
                token = center.addObserverForName_object_queue_usingBlock_(
                    name,
                    None,
                    NSOperationQueue.mainQueue(),
                    self._native_environment_changed,
                )
                self._native_observers.append((center, token))
            except Exception:
                logger.exception("couldn't observe native HUD environment event: %s", name)

        app_center = NSNotificationCenter.defaultCenter()
        observe(
            app_center,
            getattr(
                AppKit,
                "NSApplicationDidChangeScreenParametersNotification",
                "NSApplicationDidChangeScreenParametersNotification",
            ),
        )
        observe(
            app_center,
            getattr(AppKit, "NSWindowDidChangeScreenNotification", "NSWindowDidChangeScreenNotification"),
        )

        workspace_center = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
        for name in (
            getattr(AppKit, "NSWorkspaceDidActivateApplicationNotification", "NSWorkspaceDidActivateApplicationNotification"),
            getattr(AppKit, "NSWorkspaceDidLaunchApplicationNotification", "NSWorkspaceDidLaunchApplicationNotification"),
            getattr(AppKit, "NSWorkspaceDidTerminateApplicationNotification", "NSWorkspaceDidTerminateApplicationNotification"),
        ):
            observe(workspace_center, name)

    def _native_environment_changed(self, _notification):
        """Repair the HUD after AppKit changes the active app, Space, or screen."""
        self._screen_geometry = None
        if not self.isVisible() or self._hiding:
            return
        if self._display_mode == "active" and not self.targets_external_display():
            self.dismiss()
            return
        self._refresh_presentation()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_children()
        if getattr(self, "_display_text", ""):
            self._set_label_text(self._display_text)

    def _position_children(self):
        if not hasattr(self, "_label"):
            return
        # The window includes the menu-bar/notch plane above the content. Do
        # not center status text in that black strip; keep the character and
        # text in the part that grows below the physical notch.
        content_top = self._physical_notch_height if self._notch_mode else 0
        content_height = max(0, self.height() - content_top)
        self._label.setGeometry(0, content_top, self.width(), content_height)
        mascot_y = content_top + max(0, (content_height - self._mascot.height()) // 2)
        self._mascot.setGeometry(16, mascot_y, self._mascot.width(), self._mascot.height())

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
            # The external-display mirror is the same HUD, not a themed app
            # card. Keeping both surfaces true black makes the character and
            # live text feel like one notch-native object everywhere.
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
            painter.setBrush(QColor(NOTCH_BLACK))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(path)

    # text -----------------------------------------------------------

    def _set_label_text(self, text: str):
        metrics = QFontMetrics(self._label.font())
        # Reserve a little space for the mascot while keeping the status
        # centered in the fixed surface. The same layout remains stable as
        # the character changes pose between listening, processing, done,
        # and error.
        mascot_reserve = MASCOT_SIZE + 28 if self._mascot.isVisible() else 0
        available = self.width() - 32 - mascot_reserve
        # The old right-elision left users staring at the beginning of a
        # long sentence forever. A live HUD should privilege the newest
        # words, so the visible slice follows the speaker as the sentence
        # grows. The full text remains in the Live Dictation tab and in
        # history, while the HUD stays compact enough to read at a glance.
        self._label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideLeft, max(20, available)))

    # positioning --------------------------------------------------------

    def _compute_geometry(self):
        """(x, y, fixed width, notch_mode) for the HUD's resting position."""
        if self._screen_geometry is None:
            self._screen_geometry = _active_screen_geometry(self._display_mode)
        screen = self._screen_geometry
        notch = _notch_geometry(screen)
        if notch is not None:
            center_x, notch_bottom_y, notch_width = notch
            self._physical_notch_width = max(1, int(round(notch_width)))
            self._physical_notch_height = max(1, int(round(notch_bottom_y - screen.y())))
            # Cover the menu-bar plane as well as the expanded content. A
            # single top-level black surface prevents the old two-rectangle
            # seam between macOS's menu bar and Chatter's extension.
            return int(center_x - HUD_WIDTH / 2), int(screen.y()), HUD_WIDTH, True
        self._physical_notch_width = 0
        self._physical_notch_height = 0
        # Fallback: no notch on the active screen (external monitor, older
        # Mac) — dock bottom-right instead, same spot this used before.
        x = screen.x() + screen.width() - HUD_WIDTH - RIGHT_MARGIN
        y = screen.y() + screen.height() - HEIGHT - BOTTOM_MARGIN
        return x, y, HUD_WIDTH, False

    def targets_external_display(self):
        """Whether the active-app mirror belongs on an external display."""
        if self._display_mode != "active":
            return False
        geometry = _active_screen_geometry("active")
        return geometry is not None and _notch_geometry(geometry) is None

    def dismiss(self):
        """Hide immediately and forget the previous display target."""
        self._hide_timer.stop()
        self._front_reassert_timer.stop()
        self._fade_anim.stop()
        self.hide()
        self._hiding = False
        self._screen_geometry = None

    def _refresh_presentation(self):
        """Recalculate screen placement and restore the native panel ordering."""
        if not self.isVisible() or self._hiding:
            return
        old_geometry = self.geometry()
        rx, ry, width, notch_mode = self._compute_geometry()
        self._notch_mode = notch_mode
        self._shadow.setEnabled(notch_mode)
        target_height = self._physical_notch_height + HEIGHT if notch_mode else HEIGHT
        target = QRectF(rx, ry, width, target_height).toRect()
        self._position_children()
        if old_geometry.size() != target.size():
            self.setGeometry(target)
        else:
            self._animate_to(rx, ry)
        _make_overlay_appear_everywhere(self)
        _order_overlay_front(self)

    def _animate_to(self, x: int, y: int):
        self._move_anim.stop()
        self._move_anim.setStartValue(self.pos())
        self._move_anim.setEndValue(QPoint(x, y))
        self._move_anim.start()

    def _reassert_front(self):
        if self.isVisible() and not self._hiding:
            # Reconfigure, not just reorder: Qt can silently swap in a new
            # native backing window mid-session (display/Space
            # reconfiguration, a screen-recording tool changing the screen
            # graph, another app's fullscreen transition) that reverts to
            # Qt's default level/collectionBehavior. Only calling
            # _order_overlay_front here would keep re-ordering that
            # wrong-configured window without ever fixing its level, so the
            # HUD would silently lose its above-everything treatment until
            # the next full hide/show cycle. Both calls are cheap AppKit
            # property sets — no Qt widget rebuild/animation — so doing this
            # every 280ms is not the "rebuilding/animating" this timer was
            # originally designed to avoid.
            _make_overlay_appear_everywhere(self)
            _order_overlay_front(self)

    # public API -----------------------------------------------------------

    def show_state(self, state: str, detail: str = "", phrase: str | None = None):
        """`detail` is only shown verbatim for state == "error" (real,
        actionable error text). Every other state shows `phrase` when the
        caller provides it, allowing literal statuses and live transcript
        previews to share the same HUD surface."""
        self._hiding = False
        # A new press can arrive while the previous Done/Error fade is still
        # running. Stop that old animation and restore full opacity before
        # reusing the already-visible panel; otherwise the HUD can be fully
        # configured and ordered in front while remaining visually invisible.
        self._fade_anim.stop()
        if self.isVisible():
            self._opacity = 1.0
            self.setWindowOpacity(1.0)
        mascot_state = "listening" if state == "listening" else state
        self._mascot.set_state(mascot_state)
        self._hide_timer.stop()

        text = detail if state == "error" else (phrase if phrase is not None else phrases.pick(state))
        self._display_text = text
        if state != self._state:
            self._state = state

        # The character stays present for the entire interaction, including
        # processing, done, and error. That gives every status one consistent
        # visual anchor while the fixed black surface keeps text centered.
        self._mascot.setVisible(state in {"listening", "processing", "done", "error"})
        rx, ry, width, notch_mode = self._compute_geometry()
        self._notch_mode = notch_mode
        self._shadow.setEnabled(not notch_mode)
        # Geometry is set below; notch mode begins at the real notch width and
        # expands into this final width.
        if not self.isVisible():
            # Appears directly at rest — no slide from off-screen. That
            # slide was the main source of felt lag; press-to-visible should
            # be as close to instant as a fade can make it.
            if notch_mode and self._physical_notch_width > 0:
                start_width = self._physical_notch_width
                start_x = int(rx + (width - start_width) / 2)
                self.setGeometry(start_x, ry, start_width, self._physical_notch_height)
            else:
                self.setGeometry(rx, ry, width, HEIGHT)
            self._opacity = 0.0
            self.setWindowOpacity(0.0)
            # Re-applied on every show(), not just once in __init__: macOS/Qt
            # can hand the widget a new native backing window across
            # hide/show cycles, silently reverting the elevated level set
            # earlier — which would leave the HUD at a normal window level,
            # visible only when nothing else is in front of it. Called BEFORE
            # show() so the window is never briefly ordered onto screen with
            # default (non-cross-Space) properties first, AND again
            # immediately AFTER show() in case show() itself is what swaps in
            # the new native window (the before-call would then have
            # configured a window that's already been discarded, leaving the
            # real one at Qt's defaults). The second call is effectively free
            # since it runs before _order_overlay_front, not on a timer.
            _make_overlay_appear_everywhere(self)
            self.show()
            _make_overlay_appear_everywhere(self)
            _order_overlay_front(self)
            self._front_reassert_timer.start()
            target_height = self._physical_notch_height + HEIGHT if notch_mode else HEIGHT
            logger.info("HUD shown: resting at (%d, %d) target size %dx%d notch_mode=%s", rx, ry, width, target_height, notch_mode)
            self._fade_anim.stop()
            self._fade_anim.setStartValue(0.0)
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.start()
            if notch_mode and self._physical_notch_width > 0:
                self._expand_anim.stop()
                self._expand_anim.setStartValue(self.geometry())
                self._expand_anim.setEndValue(
                    QRectF(rx, ry, width, self._physical_notch_height + HEIGHT).toRect()
                )
                self._expand_anim.start()
        else:
            # Already up (e.g. listening -> processing mid-hold). A screen
            # recorder or app/fullscreen transition may have put the panel
            # behind the current Space without hiding the Qt widget, so
            # refresh the native presentation before moving it.
            _make_overlay_appear_everywhere(self)
            _order_overlay_front(self)
            self._front_reassert_timer.start()
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
            self._front_reassert_timer.stop()
            self.hide()
            self._hiding = False
            self._screen_geometry = None
