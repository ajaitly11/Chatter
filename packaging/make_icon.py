"""Generates packaging/icon.icns from a simple QPainter-drawn glyph.
No extra image-lib dependency — reuses PyQt6 (already a project dependency)
and macOS's built-in iconutil.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

from chatter.icon_art import draw_character
from chatter import theme

OUT_DIR = Path(__file__).parent
ICONSET_DIR = OUT_DIR / "icon.iconset"
ICNS_PATH = OUT_DIR / "icon.icns"

# iconutil accepts the standard macOS iconset sizes. The @2x render of
# 512x512 supplies the 1024px source; extra 64/1024 base names make iconutil
# reject the entire set on newer macOS releases.
SIZES = [16, 32, 128, 256, 512]


def draw(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    margin = size * 0.06
    painter.setBrush(QColor(theme.SURFACE))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(
        QRectF(margin, margin, size - 2 * margin, size - 2 * margin),
        size * 0.22, size * 0.22,
    )
    painter.save()
    # Keep the bundle icon on the exact same shared geometry and scale as the
    # Dock icon. The previous smaller, lower placement made the character
    # look unrelated and visually under-filled beside other apps.
    painter.translate(size * 0.05, size * 0.005)
    draw_character(painter, size * 0.90)
    painter.restore()
    painter.end()
    return pixmap


def main():
    app = QApplication(sys.argv)
    ICONSET_DIR.mkdir(exist_ok=True)
    for stale in ICONSET_DIR.glob("*.png"):
        stale.unlink()

    for size in SIZES:
        draw(size).save(str(ICONSET_DIR / f"icon_{size}x{size}.png"))
        if size <= 512:
            draw(size * 2).save(str(ICONSET_DIR / f"icon_{size}x{size}@2x.png"))

    subprocess.run(["iconutil", "-c", "icns", str(ICONSET_DIR), "-o", str(ICNS_PATH)], check=True)
    print(f"Wrote {ICNS_PATH}")


if __name__ == "__main__":
    main()
