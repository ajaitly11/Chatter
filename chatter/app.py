import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon, QMenu

from . import config
from .formatter import Formatter
from .hotkey import PushToTalkController
from .main_window import MainWindow
from .transcription_service import service

STYLE_PATH = Path(__file__).parent / "style.qss"


def _make_icon() -> QIcon:
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#5b8dee"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(4, 4, size - 8, size - 8, 16, 16)
    painter.setPen(QColor("white"))
    font = painter.font()
    font.setPointSize(28)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "C")
    painter.end()
    return QIcon(pixmap)


def run():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE_PATH.read_text())

    icon = _make_icon()
    app.setWindowIcon(icon)

    formatter = Formatter()
    window = MainWindow(formatter)

    def get_model_path():
        return window.model_combo.currentData()

    hotkey = PushToTalkController(get_model_path, formatter)
    hotkey.status_changed.connect(window._on_hotkey_status)
    hotkey.error.connect(lambda msg: QMessageBox.warning(window, "Push-to-talk error", msg))

    tray = QSystemTrayIcon(icon)
    tray.setToolTip("Chatter")
    menu = QMenu()

    open_action = QAction("Open Chatter")
    open_action.triggered.connect(lambda: (window.show(), window.raise_(), window.activateWindow()))
    menu.addAction(open_action)

    ptt_action = QAction("Push-to-talk enabled")
    ptt_action.setCheckable(True)
    cfg = config.load()
    ptt_action.setChecked(cfg.get("push_to_talk_enabled", True))

    def toggle_ptt(checked):
        config.update(push_to_talk_enabled=checked)
        if checked:
            hotkey.start()
        else:
            hotkey.stop()

    ptt_action.toggled.connect(toggle_ptt)
    menu.addAction(ptt_action)

    menu.addSeparator()
    quit_action = QAction("Quit Chatter")

    def do_quit():
        hotkey.stop()
        formatter.shutdown()
        service.close()
        app.quit()

    quit_action.triggered.connect(do_quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()

    if cfg.get("push_to_talk_enabled", True):
        hotkey.start()

    window.show()
    sys.exit(app.exec())
