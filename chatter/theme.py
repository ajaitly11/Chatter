"""The "Warm Terracotta" palette from the Claude Design mockup
(Chatter UI Mockups.dc.html, project f2467db4-5a45-4784-868d-297b4578ea68).

The mockup defines colors in OKLCH; Qt/QSS have no oklch() support, so these
are the same colors pre-converted to sRGB hex via the standard OKLab->linear
sRGB matrices. Kept as plain hex strings (for style.qss, hand-kept in sync)
and QColor factories (for widgets that paint themselves, like mascot.py and
overlay.py, which need real QColor objects rather than stylesheet strings).
"""

from PyQt6.QtGui import QColor

BG = "#2a1b16"
SURFACE = "#3b2922"
SURFACE2 = "#49352e"
BORDER = "#9a8077"
BORDER_ALPHA = 77  # ~30% of 255
TEXT = "#f5ede7"
TEXT_DIM = "#b9aaa3"
ACTIVE = "#eb7c33"  # listening / mascot body
MASCOT_DARK = "#b75400"  # ears
DONE = "#bea333"  # gold
PROCESSING = "#db940e"  # amber
WARN = "#ef6661"

DIM_ALPHA = 46  # ~18% of 255, used for *-dim pill backgrounds


def qcolor(hex_str: str, alpha: int = 255) -> QColor:
    c = QColor(hex_str)
    c.setAlpha(alpha)
    return c


def rgba_str(c: QColor) -> str:
    """QColor -> a CSS rgba(...) string, for use in inline QSS where the
    alpha channel matters (QColor.name() silently drops it)."""
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alphaF():.2f})"


def active_dim() -> QColor:
    return qcolor(ACTIVE, DIM_ALPHA)


def done_dim() -> QColor:
    return qcolor(DONE, DIM_ALPHA)


def processing_dim() -> QColor:
    return qcolor(PROCESSING, DIM_ALPHA)


def border_color() -> QColor:
    return qcolor(BORDER, BORDER_ALPHA)


# state name -> (mascot body color, ear/dark color, dim background)
STATE_COLORS = {
    "listening": (QColor(ACTIVE), QColor(MASCOT_DARK), active_dim),
    "processing": (QColor(PROCESSING), QColor(PROCESSING), processing_dim),
    "done": (QColor(DONE), QColor(MASCOT_DARK), done_dim),
    "error": (QColor(WARN), QColor(MASCOT_DARK), lambda: qcolor(WARN, DIM_ALPHA)),
}
STATE_COLORS["idle"] = STATE_COLORS["listening"]  # same look, just not animated
