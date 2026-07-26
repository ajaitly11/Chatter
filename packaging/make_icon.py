"""Generates packaging/icon.icns from a simple QPainter-drawn glyph.
No extra image-lib dependency — reuses PyQt6 (already a project dependency)
and macOS's built-in iconutil.
"""

import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

OUT_DIR = Path(__file__).parent
ICONSET_DIR = OUT_DIR / "icon.iconset"
ICNS_PATH = OUT_DIR / "icon.icns"

SIZES = [16, 32, 64, 128, 256, 512, 1024]


def draw(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    margin = size * 0.06
    painter.setBrush(QColor("#5b8dee"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(
        int(margin), int(margin), int(size - 2 * margin), int(size - 2 * margin),
        size * 0.22, size * 0.22,
    )
    painter.setPen(QColor("white"))
    font = painter.font()
    font.setPointSizeF(size * 0.5)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "C")
    painter.end()
    return pixmap


def main():
    app = QApplication(sys.argv)
    ICONSET_DIR.mkdir(exist_ok=True)

    for size in SIZES:
        draw(size).save(str(ICONSET_DIR / f"icon_{size}x{size}.png"))
        if size <= 512:
            draw(size * 2).save(str(ICONSET_DIR / f"icon_{size}x{size}@2x.png"))

    subprocess.run(["iconutil", "-c", "icns", str(ICONSET_DIR), "-o", str(ICNS_PATH)], check=True)
    print(f"Wrote {ICNS_PATH}")


if __name__ == "__main__":
    main()
