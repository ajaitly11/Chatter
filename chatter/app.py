import logging
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon, QMenu

from . import config
from . import permissions
from .formatter import Formatter
from .hotkey import PushToTalkController
from .logging_setup import configure as configure_logging
from .main_window import MainWindow
from .overlay import Overlay
from .transcription_service import MODELS_DIR, service

# Preference order for auto-detecting a push-to-talk model when
# streaming_model_path isn't set in config: nemotron-speech-streaming
# measured faster *and* more accurate than moonshine-streaming-tiny despite
# being ~15x the file size (RTF 0.07 vs 0.15-0.28 on the same test clip).
_STREAMING_MODEL_CANDIDATES = [
    MODELS_DIR / "nemotron-speech-streaming-en-0.6b-Q8_0.gguf",
    MODELS_DIR / "moonshine-streaming-tiny-Q8_0.gguf",
]

STYLE_PATH = Path(__file__).parent / "style.qss"
logger = logging.getLogger("chatter.app")


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


def _truncate(text: str, n: int = 46) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _truncate_head(text: str, n: int = 80) -> str:
    """Keeps the *tail* of the text — for live captions, the most recent
    words are the relevant ones, not the start of a long utterance."""
    text = text.replace("\n", " ").strip()
    return text if len(text) <= n else "…" + text[-(n - 1):]


def run():
    configure_logging()
    logger.info("Chatter starting")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE_PATH.read_text())

    icon = _make_icon()
    app.setWindowIcon(icon)

    formatter = Formatter()
    window = MainWindow(formatter)
    overlay = Overlay()

    def get_streaming_model_path():
        configured = config.load().get("streaming_model_path")
        if configured:
            return configured
        for candidate in _STREAMING_MODEL_CANDIDATES:
            if candidate.exists():
                return str(candidate)
        return None

    hotkey = PushToTalkController(get_streaming_model_path, formatter)
    hotkey.status_changed.connect(window._on_hotkey_status)
    hotkey.live_text_changed.connect(lambda text: overlay.update_live_text(_truncate_head(text, 80)))

    _WORKING_STATES = {"Transcribing…", "Cleaning up…", "Still finishing up…"}

    def on_status(status: str):
        if status == "Listening…":
            overlay.show_state("listening", status)
        elif status in _WORKING_STATES:
            overlay.show_state("working", status)
        # "Idle" is handled by result_ready/error, which own the final message + hide.

    hotkey.status_changed.connect(on_status)

    def on_result(text: str, pasted: bool):
        if pasted:
            overlay.flash_and_hide("done", f"Pasted: {_truncate(text)}")
        else:
            overlay.flash_and_hide(
                "done", f"Copied to clipboard: {_truncate(text)}", delay_ms=3200
            )

    hotkey.result_ready.connect(on_result)
    hotkey.error.connect(lambda msg: overlay.flash_and_hide("error", _truncate(msg, 60), delay_ms=3200))
    hotkey.error.connect(lambda msg: logger.warning("push-to-talk error: %s", msg))

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

    # Accessibility trust is tied to *this* launching bundle's identity
    # (Chatter.app vs. a bare `python main.py` run from Terminal each get
    # their own entry) — actively request it so macOS shows the real
    # permission prompt and lists this app in System Settings, instead of
    # auto-paste silently doing nothing.
    if not permissions.is_trusted():
        logger.warning("Accessibility not trusted — requesting")
        permissions.request_trust()
        QMessageBox.information(
            window,
            "Permission needed for auto-paste",
            "Chatter needs Accessibility permission to auto-paste push-to-talk "
            "transcripts at your cursor.\n\n"
            "macOS should now show a permission prompt (or Chatter will appear "
            "in System Settings > Privacy & Security > Accessibility) — enable "
            "it there, then try push-to-talk again.\n\n"
            "Until then, transcripts are still copied to your clipboard, so "
            "Cmd+V works manually.",
        )

    if cfg.get("push_to_talk_enabled", True):
        hotkey.start()

    if cfg.get("formatting_enabled", True):
        threading.Thread(target=formatter.warm_up, daemon=True).start()

    window.show()
    sys.exit(app.exec())
