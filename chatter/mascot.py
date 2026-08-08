"""The Chatter blob mascot — one widget, reused at any size (large in the
Live Dictation tab, small in the HUD), matching the Claude Design mockup's
"same silhouette every time, only the pose and color change" note.

Geometry is defined once in a 100x110 design space (matching the mockup's
own container size) and scaled to whatever actual widget size is requested,
so every state's numbers below are taken directly from the mockup's CSS/SVG
values rather than re-derived.

Animation is a from-scratch approximation of the mockup's CSS keyframes
(bounce/wiggle/tilt/eye-shift) driven by a QTimer tick, in the same style as
the waveform icon this replaces (chatter/overlay.py's old `_Icon` class) —
not a literal CSS keyframe interpreter, close enough to read as the same
motion.
"""

import math

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtProperty, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QTransform
from PyQt6.QtWidgets import QWidget

from . import theme

_KAPPA = 0.5522847498
DESIGN_W = 100.0
DESIGN_H = 110.0


def _blob_path(x: float, y: float, w: float, h: float, radii: tuple[float, ...]) -> QPainterPath:
    """8-value CSS border-radius blob: (tl_h, tr_h, br_h, bl_h, tl_v, tr_v,
    br_v, bl_v), each a fraction of w (h-radii) or h (v-radii). Corners are
    cubic-bezier approximations of elliptical arcs (same kappa trick used to
    draw a rounded rect smoothly) rather than a plain rounded rectangle."""
    tl_h, tr_h, br_h, bl_h, tl_v, tr_v, br_v, bl_v = (
        radii[0] * w, radii[1] * w, radii[2] * w, radii[3] * w,
        radii[4] * h, radii[5] * h, radii[6] * h, radii[7] * h,
    )
    path = QPainterPath()
    path.moveTo(x + tl_h, y)
    path.lineTo(x + w - tr_h, y)
    path.cubicTo(x + w - tr_h + tr_h * _KAPPA, y, x + w, y + tr_v - tr_v * _KAPPA, x + w, y + tr_v)
    path.lineTo(x + w, y + h - br_v)
    path.cubicTo(x + w, y + h - br_v + br_v * _KAPPA, x + w - br_h + br_h * _KAPPA, y + h, x + w - br_h, y + h)
    path.lineTo(x + bl_h, y + h)
    path.cubicTo(x + bl_h - bl_h * _KAPPA, y + h, x, y + h - bl_v + bl_v * _KAPPA, x, y + h - bl_v)
    path.lineTo(x, y + tl_v)
    path.cubicTo(x, y + tl_v - tl_v * _KAPPA, x + tl_h - tl_h * _KAPPA, y, x + tl_h, y)
    path.closeSubpath()
    return path


# Body border-radius fractions, straight from the mockup's CSS.
_BODY_RADII = (0.44, 0.56, 0.58, 0.42, 0.48, 0.44, 0.56, 0.52)
_DONE_BODY_RADII = (0.46, 0.54, 0.50, 0.50, 0.58, 0.58, 0.42, 0.42)
_EAR_RADII = (0.5, 0.5, 0.5, 0.5, 0.6, 0.6, 0.4, 0.4)

# Per-state design-space geometry (design space is DESIGN_W x DESIGN_H).
_STATES = {
    "listening": dict(
        body_rect=(18, 46, 64, 64), body_radii=_BODY_RADII,
        ear_rect=(14, 22), ear_top=6, ear_inset=22, ear_rot=16,
        eye_style="circle", eye_r=3.2, eyes=((22, 28), (42, 28)),
        mouth=((24, 40), (32, 46), (40, 40)), mouth_w=2.4,
    ),
    "processing": dict(
        body_rect=(18, 46, 64, 64), body_radii=_BODY_RADII,
        ear_rect=(14, 22), ear_top=6, ear_inset=22, ear_rot=10,
        eye_style="circle", eye_r=3.2, eyes=((22, 28), (42, 28)),
        mouth=((26, 41), (32, 43.5), (38, 41)), mouth_w=2.4,
    ),
    "done": dict(
        # Same body/ear footprint as every other state (18,46,64,64 / a
        # 14x22 ear at top:6) — the mockup's "done" pose used a shorter,
        # wider body and shorter ears, but that made the character visibly
        # change size between states, which read as inconsistent switching
        # from listening/processing to done. The settle animation still
        # gives it a distinct, slightly squashed *feel* without changing the
        # footprint everything else shares.
        body_rect=(18, 46, 64, 64), body_radii=_DONE_BODY_RADII,
        ear_rect=(14, 22), ear_top=6, ear_inset=22, ear_rot=6,
        eye_style="arc", eyes=((18, 27, 22, 24, 26, 27), (38, 27, 42, 24, 46, 27)),
        mouth=((22, 37), (32, 46), (42, 37)), mouth_w=2.6,
    ),
    "error": dict(
        body_rect=(18, 46, 64, 64), body_radii=_BODY_RADII,
        ear_rect=(14, 22), ear_top=6, ear_inset=22, ear_rot=6,
        eye_style="circle", eye_r=3.2, eyes=((22, 28), (42, 28)),
        mouth=((26, 42), (32, 40), (38, 42)), mouth_w=2.4,
    ),
}
# Same silhouette as listening, just not bounced/wiggled — the at-rest pose.
_STATES["idle"] = _STATES["listening"]


class Mascot(QWidget):
    def __init__(self, size: int = 100, parent=None):
        super().__init__(parent)
        self._design_scale = size / DESIGN_W
        self.setFixedSize(round(size), round(size * DESIGN_H / DESIGN_W))
        self._state = "listening"
        self._phase = 0.0
        self._settle = 1.0  # done-state squash progress, animated

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30)

        self._settle_anim = QPropertyAnimation(self, b"settle")
        self._settle_anim.setDuration(600)
        self._settle_anim.setEasingCurve(QEasingCurve.Type.OutBack)

    def _get_settle(self):
        return self._settle

    def _set_settle(self, value):
        self._settle = value
        self.update()

    settle = pyqtProperty(float, _get_settle, _set_settle)

    def set_state(self, state: str):
        if state not in _STATES:
            state = "listening"
        changed = state != self._state
        self._state = state
        if changed and state == "done":
            self._settle_anim.stop()
            self._settle_anim.setStartValue(0.0)
            self._settle_anim.setEndValue(1.0)
            self._settle_anim.start()
        elif changed:
            self._settle = 1.0
        self.update()

    def _tick(self):
        self._phase += 0.045
        if self._state in ("listening", "processing"):
            self.update()

    # painting -----------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(self._design_scale, self._design_scale)

        spec = _STATES[self._state]
        bx, by, bw, bh = spec["body_rect"]
        body_color, ear_color, _dim = theme.STATE_COLORS[self._state]

        painter.save()
        cx, cy = bx + bw / 2, by + bh / 2
        if self._state == "listening":
            # Only ever lifts up from rest, never dips below it — the body
            # rect already reaches the exact bottom of the design canvas
            # (by + bh == DESIGN_H), so a downward bounce had nowhere to go
            # and clipped against the widget's own bottom edge.
            bounce = max(0.0, math.sin(self._phase)) * 5
            squash_x = 1.0 + 0.03 * math.cos(self._phase)
            squash_y = 1.0 - 0.03 * math.cos(self._phase)
            painter.translate(cx, cy - bounce)
            painter.scale(squash_x, squash_y)
            painter.translate(-cx, -cy)
        elif self._state == "processing":
            tilt = math.sin(self._phase * 0.6) * 4
            painter.translate(cx, cy)
            painter.rotate(tilt)
            painter.translate(-cx, -cy)
        elif self._state == "done":
            # doneSettle: overshoots wide/short then relaxes to a slightly
            # squashed content-looking rest pose (1.1x wide, 0.84x tall).
            t = self._settle
            sx = 1.0 + 0.1 * (1.0 - (1.0 - t) ** 2)
            sy = 1.0 - 0.16 * (1.0 - (1.0 - t) ** 2)
            painter.translate(cx, cy)
            painter.scale(sx, sy)
            painter.translate(-cx, -cy)

        painter.setPen(Qt.PenStyle.NoPen)

        # ears (drawn behind the body, matching the mockup's z-order)
        ear_w, ear_h = spec["ear_rect"]
        for side in (-1, 1):
            ex = bx + spec["ear_inset"] if side < 0 else bx + bw - spec["ear_inset"] - ear_w
            ey = by - (46 - spec["ear_top"])  # ear_top is relative to the 100x110 container
            ear_path = _blob_path(0, 0, ear_w, ear_h, _EAR_RADII)
            painter.save()
            # CSS transform-origin defaults to the element's own center for
            # these ears (only the processing-state thinkTilt animation
            # overrides it to bottom-center, applied to the whole mascot
            # instead here since that state tilts the entire body+ears unit).
            pivot_x, pivot_y = ex + ear_w / 2, ey + ear_h / 2
            painter.translate(pivot_x, pivot_y)
            angle = spec["ear_rot"] * side
            if self._state == "listening":
                angle += math.sin(self._phase + (0 if side < 0 else 0.6)) * 8
            painter.rotate(angle)
            painter.translate(-pivot_x, -pivot_y)
            painter.setBrush(ear_color)
            painter.drawPath(ear_path.translated(ex, ey))
            painter.restore()

        # body
        painter.setBrush(body_color)
        painter.drawPath(_blob_path(bx, by, bw, bh, spec["body_radii"]))

        # face (SVG viewBox 0 0 64 64 mapped onto the body rect exactly)
        face_scale = bw / 64.0
        face_dx = self._phase_shift() if self._state == "processing" else 0.0
        painter.save()
        painter.translate(bx + face_dx, by)
        painter.scale(face_scale, bh / 64.0)
        bg = QColor(theme.BG)
        if spec["eye_style"] == "circle":
            painter.setBrush(bg)
            for (ecx, ecy) in spec["eyes"]:
                painter.drawEllipse(QRectF(ecx - spec["eye_r"], ecy - spec["eye_r"], spec["eye_r"] * 2, spec["eye_r"] * 2))
        else:  # arc — closed happy eyes
            pen = QPen(bg, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for (x1, y1, cx2, cy2, x2, y2) in spec["eyes"]:
                p = QPainterPath()
                p.moveTo(x1, y1)
                p.quadTo(cx2, cy2, x2, y2)
                painter.drawPath(p)
            painter.setPen(Qt.PenStyle.NoPen)

        pen = QPen(bg, spec["mouth_w"], Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        (mx1, my1), (mcx, mcy), (mx2, my2) = spec["mouth"]
        mouth = QPainterPath()
        mouth.moveTo(mx1, my1)
        mouth.quadTo(mcx, mcy, mx2, my2)
        painter.drawPath(mouth)
        painter.restore()

        painter.restore()

    def _phase_shift(self) -> float:
        return math.sin(self._phase * 0.6) * 2
