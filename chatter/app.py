import logging
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

from . import config
from . import permissions
from . import phrases
from . import theme
from .correction_watcher import CorrectionWatcher
from .formatter import Formatter
from .hotkey import PushToTalkController
from .logging_setup import configure as configure_logging
from .main_window import MainWindow
from .onboarding import OnboardingWindow
from .overlay import Overlay
from .transcription_service import MODELS_DIR, service

# Push-to-talk transcribes with one of these (see hotkey.py) — Whisper
# first, since the user would rather wait than get a less accurate result.
_BATCH_MODEL_CANDIDATES = [
    MODELS_DIR / "whisper-large-v3-turbo-Q8_0.gguf",
]

STYLE_PATH = Path(__file__).parent / "style.qss"
logger = logging.getLogger("chatter.app")


def _make_icon() -> QIcon:
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(theme.ACTIVE))
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
    correction_watcher = CorrectionWatcher()

    def get_batch_model_path():
        configured = config.load().get("whisper_model_path")
        if configured:
            return configured
        for candidate in _BATCH_MODEL_CANDIDATES:
            if candidate.exists():
                return str(candidate)
        return None

    hotkey = PushToTalkController(get_batch_model_path, formatter)
    hotkey.status_changed.connect(window._on_hotkey_status)

    def restart_hotkey_listener():
        if config.load().get("push_to_talk_enabled", True):
            hotkey.stop()
            hotkey.start()

    window.hotkey_changed.connect(restart_hotkey_listener)

    _PROCESSING_STATES = {"Transcribing…", "Cleaning up…", "Still finishing up…"}

    def on_status(status: str):
        # No system notifications anywhere in this flow, by explicit
        # request: a notification banner is fixed by macOS to the top-right
        # corner and auto-dismisses after a few seconds, so it can neither
        # sit at the notch nor persist for the length of a long hold. The
        # HUD (already fixed to sit above other apps' windows, including
        # fullscreen ones — see overlay.py) is the only feedback surface for
        # the whole press/hold/release cycle now.
        # Picked once and passed to both surfaces — each used to call
        # phrases.pick() independently, so the HUD and the Live Dictation
        # tab could (and did) show two different rotated phrases at once.
        # The HUD gets a short, rotated, vibe-y phrase (phrases.pick); the
        # Live Dictation tab gets the literal status text instead — it's
        # the bigger, primary surface, so it should say plainly what's
        # happening ("Transcribing…", "Cleaning up…") rather than cycle
        # through cute flavor text that isn't always obvious.
        if status == "Listening…":
            overlay.show_state("listening", phrase=phrases.pick("listening"))
            window.set_live_state("listening", label=status)
        elif status in _PROCESSING_STATES:
            overlay.show_state("processing", phrase=phrases.pick("processing"))
            window.set_live_state("processing", label=status)
        # "Idle" is handled by result_ready/error, which own the final message + hide.

    hotkey.status_changed.connect(on_status)

    def on_result(text: str, pasted: bool):
        overlay.flash_and_hide("done", phrase=phrases.pick("done"))
        window.set_live_state("done", label="Done!")
        window._reload_dictation_history()
        if pasted:
            correction_watcher.watch_after_paste()

    hotkey.result_ready.connect(on_result)

    def on_correction_learned(wrong: str, right: str):
        window._reload_dictionary_table()

    correction_watcher.correction_learned.connect(on_correction_learned)
    hotkey.error.connect(lambda msg: overlay.flash_and_hide("error", _truncate(msg, 60), delay_ms=3200))
    hotkey.error.connect(lambda msg: window.set_live_state("error", label=_truncate(msg, 60)))
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
    # their own entry) — the onboarding flow's second step actively
    # requests it so macOS shows the real permission prompt and lists this
    # app in System Settings, instead of auto-paste silently doing nothing.
    if not cfg.get("onboarding_complete", False):
        OnboardingWindow(window).exec()
        config.update(onboarding_complete=True)
        cfg = config.load()

    if cfg.get("push_to_talk_enabled", True):
        hotkey.start()

    if cfg.get("formatting_enabled", True):
        threading.Thread(target=formatter.warm_up, daemon=True).start()

    window.show()
    sys.exit(app.exec())
