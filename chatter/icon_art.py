"""Static mascot artwork used by the app and bundle icons.

The animated ``Mascot`` widget is intentionally not used during packaging:
creating a live QWidget just to render an icon can initialize timers and
native pasteboard services before the app exists. This small static painter
keeps the same blob, ears, eyes, and smile in a safe, reusable form.
"""

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QPainter, QPainterPath, QPen

from . import theme
from .mascot import DESIGN_W, _EAR_RADII, _STATES, _blob_path


def draw_character(
    painter: QPainter,
    size: float,
    state: str = "idle",
    monochrome: bool = False,
) -> None:
    """Paint the same 100x110 mascot silhouette used by ``Mascot``.

    The bundle icon is static, so it deliberately uses the mascot's resting
    pose. Importing the shared geometry keeps the icon and in-app character
    from slowly drifting apart as the character is refined.
    """
    scale = float(size) / DESIGN_W
    spec = _STATES.get(state, _STATES["idle"])
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(scale, scale)
    painter.setPen(Qt.PenStyle.NoPen)

    body_color, ear_color, _dim = theme.STATE_COLORS.get(
        state, theme.STATE_COLORS["idle"]
    )
    face = theme.qcolor(theme.BG)
    if monochrome:
        body_color = ear_color = theme.qcolor("#ffffff")
        face = theme.qcolor("#1c1c1c")
    bx, by, bw, bh = spec["body_rect"]
    ear_w, ear_h = spec["ear_rect"]
    painter.setBrush(ear_color)
    for side in (-1, 1):
        ex = bx + spec["ear_inset"] if side < 0 else bx + bw - spec["ear_inset"] - ear_w
        ey = by - (46 - spec["ear_top"])
        pivot_x, pivot_y = ex + ear_w / 2, ey + ear_h / 2
        painter.save()
        painter.translate(pivot_x, pivot_y)
        painter.rotate(spec["ear_rot"] * side)
        painter.translate(-pivot_x, -pivot_y)
        painter.drawPath(_blob_path(0, 0, ear_w, ear_h, _EAR_RADII).translated(ex, ey))
        painter.restore()

    painter.setBrush(body_color)
    painter.drawPath(_blob_path(0, 0, bw, bh, spec["body_radii"]).translated(bx, by))

    painter.save()
    painter.translate(bx, by)
    painter.scale(bw / 64.0, bh / 64.0)
    painter.setBrush(face)
    eye_r = spec.get("eye_r", 3.2)
    if spec.get("eye_style") == "arc":
        painter.setPen(QPen(face, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for x1, y1, cx, cy, x2, y2 in spec["eyes"]:
            eye = QPainterPath()
            eye.moveTo(x1, y1)
            eye.quadTo(cx, cy, x2, y2)
            painter.drawPath(eye)
    else:
        painter.setBrush(face)
        for ecx, ecy in spec["eyes"]:
            painter.drawEllipse(QRectF(ecx - eye_r, ecy - eye_r, eye_r * 2, eye_r * 2))

    pen = QPen(face, spec["mouth_w"], Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    smile = QPainterPath()
    (mx1, my1), (mcx, mcy), (mx2, my2) = spec["mouth"]
    smile.moveTo(mx1, my1)
    smile.quadTo(mcx, mcy, mx2, my2)
    painter.drawPath(smile)
    painter.restore()
    painter.restore()
