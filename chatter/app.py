import logging
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QLockFile, QRectF, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

from . import config
from . import permissions
from . import theme
from .correction_watcher import CorrectionWatcher
from .formatter import Formatter
from .hotkey import PushToTalkController
from .logging_setup import configure as configure_logging
from .main_window import MainWindow
from .icon_art import draw_character
from .onboarding import OnboardingWindow
from .overlay import Overlay
from .transcription_service import MODELS_DIR, service, streaming_service

_STREAMING_MODEL_CANDIDATES = [
    MODELS_DIR / "nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf",
    MODELS_DIR / "nemotron-speech-streaming-en-0.6b-Q8_0.gguf",
    MODELS_DIR / "moonshine-streaming-tiny-Q8_0.gguf",
]

STYLE_PATH = Path(__file__).parent / "style.qss"
logger = logging.getLogger("chatter.app")


def _make_icon() -> QIcon:
    icon = QIcon()
    # Supplying several native sizes keeps Dock/Launchpad rendering crisp
    # instead of asking a single 64px pixmap to serve every scale factor.
    for size in (16, 32, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(theme.SURFACE))
        painter.setPen(Qt.PenStyle.NoPen)
        margin = size * 0.06
        painter.drawRoundedRect(
            QRectF(margin, margin, size - 2 * margin, size - 2 * margin),
            size * 0.22, size * 0.22,
        )
        painter.save()
        # The shared design canvas includes a little headroom above the ears
        # but no dead space below the body. Paint it nearly edge-to-edge and
        # lift it slightly so the visual mass is centered in Dock/Launchpad.
        character_size = size * 0.90
        painter.translate(size * 0.05, size * 0.005)
        draw_character(painter, character_size)
        painter.restore()
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _make_tray_icon() -> QIcon:
    """A crisp white mascot for the macOS menu bar status item."""
    icon = QIcon()
    for size in (16, 32, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        character_size = size * 0.90
        painter.translate(size * 0.05, size * 0.005)
        draw_character(painter, character_size, monochrome=True)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _truncate(text: str, n: int = 46) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def run():
    configure_logging()
    logger.info("Chatter starting")

    # A previous checkout can remain alive after the project is moved. A
    # shared lock in Application Support prevents two copies from both
    # consuming the same global hotkey and competing for the microphone.
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(config.CONFIG_DIR / "instance.lock"))
    instance_lock.setStaleLockTime(30_000)
    if not instance_lock.tryLock(100):
        logger.warning("another Chatter instance is already running; exiting")
        return

    app = QApplication(sys.argv)
    # Keep Qt's application identity aligned with the bundle identity. This
    # controls window/settings labels inside Qt; the packaged Mach-O
    # executable (built separately) is what makes macOS privacy panes name
    # the process Chatter instead of the interpreter.
    app.setApplicationName("Chatter")
    app.setApplicationDisplayName("Chatter")
    app.setOrganizationName("Chatter")
    app.setOrganizationDomain("chatter.local")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE_PATH.read_text())

    icon = _make_icon()
    app.setWindowIcon(icon)

    formatter = Formatter()
    window = MainWindow(formatter)
    overlay = Overlay()
    # Keep the notch as the stable MacBook anchor, but mirror the HUD on the
    # display containing the active app so it remains visible while the user
    # is looking at an attached monitor.
    active_display_overlay = Overlay(display_mode="active")
    correction_watcher = CorrectionWatcher()

    def get_streaming_model_path():
        configured = config.load().get("streaming_model_path")
        if configured and Path(configured).exists():
            return configured
        for candidate in _STREAMING_MODEL_CANDIDATES:
            if candidate.exists():
                return str(candidate)
        return None

    hotkey = PushToTalkController(get_streaming_model_path, formatter)
    hotkey.status_changed.connect(window._on_hotkey_status)

    def permissions_ready():
        return (
            permissions.is_microphone_authorized()
            and permissions.input_monitoring_available()
            and permissions.is_trusted()
        )

    def restart_hotkey_listener():
        hotkey.stop()
        if config.load().get("push_to_talk_enabled", True) and permissions_ready():
            hotkey.start()

    window.hotkey_changed.connect(restart_hotkey_listener)

    _PROCESSING_STATES = {"Finishing audio…", "Finalizing…", "Transcribing…", "Cleaning up…", "Still finishing up…"}
    current_status = {"value": "Idle"}

    def show_hud(state: str, detail: str = "", phrase: str | None = None):
        overlay.show_state(state, detail, phrase=phrase)
        if active_display_overlay.targets_external_display():
            active_display_overlay.show_state(state, detail, phrase=phrase)
        else:
            active_display_overlay.dismiss()

    def flash_hud(state: str, detail: str = "", delay_ms: int = 2200, phrase: str | None = None):
        overlay.flash_and_hide(state, detail, delay_ms=delay_ms, phrase=phrase)
        if active_display_overlay.targets_external_display():
            active_display_overlay.flash_and_hide(state, detail, delay_ms=delay_ms, phrase=phrase)
        else:
            active_display_overlay.dismiss()

    def on_status(status: str):
        current_status["value"] = status
        # No system notifications anywhere in this flow, by explicit
        # request: a notification banner is fixed by macOS to the top-right
        # corner and auto-dismisses after a few seconds, so it can neither
        # sit at the notch nor persist for the length of a long hold. The
        # HUD (already fixed to sit above other apps' windows, including
        # fullscreen ones — see overlay.py) is the only feedback surface for
        # the whole press/hold/release cycle now.
        # Both surfaces receive the same literal state. The mascot provides
        # the personality; the text stays unambiguous about what the system
        # is doing, especially while the streaming model finalizes and local
        # cleanup runs.
        if status == "Listening…":
            show_hud("listening", phrase=status)
            window.set_live_state("listening", label=status)
        elif status in _PROCESSING_STATES:
            show_hud("processing", phrase=status)
            window.set_live_state("processing", label=status)
        # "Idle" is handled by result_ready/error, which own the final message + hide.

    hotkey.status_changed.connect(on_status)

    def on_partial(text: str):
        if not text:
            return
        state = "listening" if current_status["value"] == "Listening…" else "processing"
        # Pass the complete streaming text through. The HUD itself keeps the
        # newest visible words in its compact viewport, while the Live
        # Dictation tab retains the complete draft for inspection/editing.
        show_hud(state, phrase=text)
        window.set_live_preview(text)

    hotkey.partial_changed.connect(on_partial)

    def on_result(text: str, pasted: bool):
        flash_hud("done", phrase="Done!")
        window.set_live_state("done", label="Done!")
        window._reload_dictation_history()
        if pasted:
            correction_watcher.watch_after_paste()

    hotkey.result_ready.connect(on_result)

    def on_correction_learned(wrong: str, right: str):
        window._reload_dictionary_table()

    correction_watcher.correction_learned.connect(on_correction_learned)
    hotkey.error.connect(lambda msg: flash_hud("error", _truncate(msg, 60), delay_ms=3200))
    hotkey.error.connect(lambda msg: window.set_live_state("error", label=_truncate(msg, 60)))
    hotkey.error.connect(lambda msg: logger.warning("push-to-talk error: %s", msg))

    tray = QSystemTrayIcon(_make_tray_icon())
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
        if checked and permissions_ready():
            hotkey.start()
        else:
            hotkey.stop()

    ptt_action.toggled.connect(toggle_ptt)
    menu.addAction(ptt_action)

    menu.addSeparator()
    quit_action = QAction("Quit Chatter")

    def do_quit():
        hotkey.shutdown()
        formatter.shutdown()
        service.close()
        streaming_service.close()
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
    needs_onboarding = (
        not cfg.get("onboarding_complete", False)
        or not permissions.is_microphone_authorized()
        or not permissions.is_trusted()
        or not permissions.is_input_monitoring_trusted()
    )
    if needs_onboarding:
        # Register this frozen bundle with macOS before the onboarding dialog
        # opens. This makes the permission entry appear as Chatter (rather
        # than leaving the user with the old Python 3 entry from the former
        # thin-wrapper build) and lets the dialog explain the next step.
        if not permissions.is_microphone_authorized():
            permissions.request_microphone_access()
        onboarding = OnboardingWindow(window)
        onboarding.exec()
        if onboarding.permissions_ready:
            config.update(onboarding_complete=True)
        window._refresh_permission_status()
        cfg = config.load()

    if cfg.get("push_to_talk_enabled", True) and permissions_ready():
        hotkey.start()
    elif cfg.get("push_to_talk_enabled", True):
        logger.warning("push-to-talk listener not started: Chatter permissions are incomplete")

    def warm_up_models():
        backend = config.load().get("backend", "auto")
        streaming_path = get_streaming_model_path()
        logger.info("streaming model warm-up starting: path=%s backend=%s", streaming_path, backend)
        if streaming_path:
            try:
                ready = streaming_service.warm_up(streaming_path, backend)
                logger.info(
                    "streaming model warm-up: ready=%s path=%s backend=%s",
                    ready, streaming_path, streaming_service.backend_name(),
                )
            except Exception:
                logger.exception("streaming model warm-up failed: path=%s", streaming_path)
        else:
            logger.warning("streaming model warm-up skipped: no local streaming model found")

    logger.info("starting background streaming model warm-up")
    threading.Thread(target=warm_up_models, daemon=True).start()

    if cfg.get("formatting_enabled", True):
        threading.Thread(target=formatter.warm_up, daemon=True).start()

    window.show()
    sys.exit(app.exec())
