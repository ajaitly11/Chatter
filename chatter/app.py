import logging
import sys
import threading
import webbrowser
from pathlib import Path

import AppKit
from PyQt6.QtCore import QLockFile, QRectF, Qt, QTimer
from PyQt6.QtGui import QAction, QActionGroup, QColor, QIcon, QKeySequence, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon
import sounddevice as sd

from . import config
from . import history
from . import insights
from . import permissions
from . import theme
from .correction_watcher import CorrectionWatcher
from .formatter import Formatter
from .hotkey import PushToTalkController
from .logging_setup import configure as configure_logging
from .main_window import MainWindow
from .native_hotkey import (
    ACTIVATION_MODE_OPTIONS,
    HOLD_TO_TALK,
    normalize_activation_mode,
)
from .icon_art import draw_character
from .onboarding import OnboardingWindow
from .overlay import Overlay
from .tray_popover import TrayPopover
from .transcription_service import MODELS_DIR, service, streaming_service
from .update_checker import UpdateChecker

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
    # A previous build can leave the optional cleanup server behind if the
    # GUI was force-quit or updated while formatting was enabled. Do this
    # before the window is shown so a disabled cleanup setting really means
    # only the streaming ASR model is resident.
    if not config.load().get("formatting_enabled", True):
        Formatter.reap_stale_server()
    window = MainWindow(formatter)
    update_checker = UpdateChecker(window)
    app.setApplicationVersion(update_checker.current_version)
    window.set_installed_version(update_checker.current_version)
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

    # macOS can update TCC while the onboarding or System Settings window is
    # open. Polling lets the listener come alive immediately after the user
    # enables the final switch; no quit/relaunch or second setup ceremony is
    # required.
    permission_poller = QTimer()
    permission_poller.setInterval(1500)

    def recover_hotkey_when_ready():
        window._refresh_permission_status()
        window._refresh_setup_banner()
        if permissions_ready() and not config.load().get("onboarding_complete", False):
            config.update(onboarding_complete=True, onboarding_dismissed=False)
        if (
            config.load().get("push_to_talk_enabled", True)
            and permissions_ready()
            and not hotkey.listener_running
        ):
            logger.info("all Chatter permissions are ready; starting push-to-talk listener")
            hotkey.start()

    permission_poller.timeout.connect(recover_hotkey_when_ready)
    permission_poller.start()

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

    # Keep the most recent committed dictation available to the native Edit
    # menu without changing the window or the transcript storage model. The
    # history fallback also covers results recorded before this process began.
    last_transcript = {"value": ""}
    recent_history = history.load(kind="dictation", limit=1)
    if recent_history:
        last_transcript["value"] = str(recent_history[0].get("text", ""))

    def on_result(text: str, pasted: bool):
        last_transcript["value"] = text or ""
        flash_hud("done", phrase="Done!")
        window.set_live_state("done", label="Done!")
        window._reload_dictation_history()
        window._reload_insights()
        if pasted:
            correction_watcher.watch_after_paste()

    hotkey.result_ready.connect(on_result)

    def on_correction_learned(wrong: str, right: str):
        window._reload_dictionary_table()
        window._reload_insights()

    correction_watcher.correction_learned.connect(on_correction_learned)
    hotkey.error.connect(lambda msg: flash_hud("error", _truncate(msg, 60), delay_ms=3200))
    hotkey.error.connect(lambda msg: window.set_live_state("error", label=_truncate(msg, 60)))
    hotkey.error.connect(lambda msg: logger.warning("push-to-talk error: %s", msg))

    tray_icon = _make_tray_icon()
    tray = QSystemTrayIcon(tray_icon)
    tray.setToolTip("Chatter")
    menu = QMenu()
    menu.setObjectName("chatterTrayMenu")
    # QSystemTrayIcon uses a native menu on macOS. Keep an explicit palette
    # here as well as the app-wide QSS so the compact insights surface uses
    # Chatter's terracotta/orange language wherever Qt allows styling it.
    menu.setStyleSheet(
        f"""
        QMenu#chatterTrayMenu {{
            background-color: {theme.SURFACE};
            color: {theme.TEXT};
            border: 1px solid {theme.BORDER};
            border-radius: 10px;
            padding: 7px;
            font-family: \"Avenir Next\";
            font-size: 13px;
        }}
        QMenu#chatterTrayMenu::item {{
            background-color: transparent;
            border-radius: 6px;
            padding: 8px 30px 8px 11px;
        }}
        QMenu#chatterTrayMenu::item:selected {{
            background-color: {theme.SURFACE2};
            color: {theme.TEXT};
        }}
        QMenu#chatterTrayMenu::item:disabled {{
            color: {theme.TEXT_DIM};
        }}
        QMenu#chatterTrayMenu::separator {{
            background-color: {theme.BORDER};
            height: 1px;
            margin: 6px 7px;
        }}
        """
    )

    snapshot_words_action = QAction("Today · 0 words", menu)
    snapshot_words_action.setEnabled(False)
    snapshot_sessions_action = QAction("0 dictations · stored locally", menu)
    snapshot_sessions_action.setEnabled(False)
    menu.addAction(snapshot_words_action)
    menu.addAction(snapshot_sessions_action)
    menu.addSeparator()

    def present_window(tab=None):
        """Bring Chatter back to the user's current desktop and frontmost."""
        if tab is not None:
            window.tabs.setCurrentWidget(tab)

        # ``show()`` alone can leave a minimized QMainWindow minimized on
        # macOS. Clear that state explicitly, then ask both Qt and AppKit to
        # make the window visible and frontmost. This is shared by every
        # menu-bar route so Open Chatter, Settings, and Insights behave the
        # same way.
        state = window.windowState()
        if state & Qt.WindowState.WindowMinimized:
            window.setWindowState(state & ~Qt.WindowState.WindowMinimized)
        window.showNormal()
        window.show()
        window.raise_()
        window.activateWindow()
        try:
            AppKit.NSApp().activateIgnoringOtherApps_(True)
        except Exception:
            logger.exception("couldn't activate Chatter through AppKit")
        # AppKit may process the activation after the Qt event returns. A
        # second pass on the next event-loop turn closes that small race.
        QTimer.singleShot(0, lambda: (window.raise_(), window.activateWindow()))

    open_action = QAction("Open Chatter")
    open_action.triggered.connect(present_window)
    menu.addAction(open_action)

    settings_action = QAction("Settings…")
    settings_action.setShortcut("Ctrl+,")
    settings_action.setMenuRole(QAction.MenuRole.PreferencesRole)

    def open_settings():
        present_window(window.settings_tab)

    settings_action.triggered.connect(open_settings)

    def open_insights():
        present_window(window.insights_tab)

    insights_action = QAction("See more insights")
    insights_action.triggered.connect(open_insights)
    menu.addAction(insights_action)
    menu.addAction(settings_action)

    def refresh_permissions():
        """Refresh TCC state and start the listener when setup is complete."""
        window._refresh_permission_status()
        window._refresh_setup_banner()
        restart_hotkey_listener()
        refresh_menu_state()

    def run_onboarding():
        # A deliberate setup session gets one bounded TCC recovery attempt.
        # If macOS still reports a stale state after that relaunch, the
        # dialog offers a non-blocking exit instead of sending the user back
        # through the same button forever.
        config.update(
            onboarding_dismissed=False,
            onboarding_permission_restart_attempted=False,
        )
        onboarding = OnboardingWindow(window)
        onboarding.exec()
        if onboarding.permissions_ready:
            config.update(
                onboarding_complete=True,
                onboarding_dismissed=False,
                onboarding_permission_restart_attempted=False,
            )
        elif onboarding.dismissed:
            config.update(
                onboarding_dismissed=True,
                onboarding_permission_restart_attempted=False,
            )
        refresh_permissions()

    window.setup_requested.connect(run_onboarding)

    finish_setup_action = QAction("Finish setup…")
    finish_setup_action.triggered.connect(run_onboarding)

    permission_status_action = QAction()
    permission_status_action.setEnabled(False)

    check_permissions_action = QAction("Refresh permissions")
    check_permissions_action.triggered.connect(refresh_permissions)

    # QSystemTrayIcon's context menu is an NSMenu on macOS. That native menu
    # deliberately ignores the app stylesheet, so the status item uses a
    # themed Qt popover instead. Keep this indirection because the refresh
    # callbacks are defined before the popover itself is constructed.
    tray_popover_ref = {"value": None}

    def set_menu_bar_icon(visible: bool):
        config.update(menu_bar_icon_enabled=visible)
        tray.setVisible(visible)
        window.menu_bar_icon_checkbox.blockSignals(True)
        window.menu_bar_icon_checkbox.setChecked(visible)
        window.menu_bar_icon_checkbox.blockSignals(False)
        popover = tray_popover_ref.get("value")
        if popover is not None:
            popover.set_menu_bar_checked(visible)

    menu_bar_icon_action = QAction("Show Chatter in menu bar", menu)
    menu_bar_icon_action.setCheckable(True)
    menu_bar_icon_action.toggled.connect(set_menu_bar_icon)
    window.menu_bar_icon_visibility_changed.connect(set_menu_bar_icon)

    ptt_action = QAction("Push-to-talk enabled")
    ptt_action.setCheckable(True)
    cfg = config.load()
    ptt_action.setChecked(cfg.get("push_to_talk_enabled", True))

    def toggle_ptt(checked):
        config.update(push_to_talk_enabled=checked)
        window.ptt_enabled_checkbox.blockSignals(True)
        window.ptt_enabled_checkbox.setChecked(checked)
        window.ptt_enabled_checkbox.blockSignals(False)
        if checked and permissions_ready():
            hotkey.start()
        else:
            hotkey.stop()

    ptt_action.toggled.connect(toggle_ptt)

    dictation_menu = QMenu("Dictation", menu)
    dictation_menu.addAction(ptt_action)

    activation_menu = QMenu("Activation", dictation_menu)
    activation_group = QActionGroup(activation_menu)
    activation_group.setExclusive(True)
    activation_actions = []
    for mode, label in ACTIVATION_MODE_OPTIONS:
        action = QAction(label, activation_menu)
        action.setCheckable(True)
        action.setData(mode)

        def select_activation(_checked, activation_mode=mode):
            config.update(hotkey_activation_mode=activation_mode)
            window._update_live_hotkey_pill()
            window._update_activation_note()
            index = window.activation_combo.findData(activation_mode)
            if index >= 0:
                window.activation_combo.blockSignals(True)
                window.activation_combo.setCurrentIndex(index)
                window.activation_combo.blockSignals(False)

        action.triggered.connect(select_activation)
        activation_group.addAction(action)
        activation_menu.addAction(action)
        activation_actions.append(action)
    dictation_menu.addMenu(activation_menu)

    cleanup_action = QAction("Clean up with local AI")
    cleanup_action.setCheckable(True)
    cleanup_action.setChecked(cfg.get("formatting_enabled", True))

    def toggle_cleanup(checked):
        window.format_checkbox.blockSignals(True)
        window.format_checkbox.setChecked(checked)
        window.format_checkbox.blockSignals(False)
        window._on_formatting_toggled(checked)

    cleanup_action.toggled.connect(toggle_cleanup)

    writing_menu = QMenu("Writing", menu)
    writing_menu.addAction(cleanup_action)

    context_menu = QMenu("Writing context", writing_menu)
    context_group = QActionGroup(context_menu)
    context_group.setExclusive(True)
    context_options = [
        ("Automatic (foreground app)", "auto"),
        ("Neutral dictation", "general"),
        ("Professional email", "email"),
        ("Notes / journal", "notes"),
        ("Coding / AI prompt", "coding"),
        ("Social / chat", "social"),
    ]
    context_actions = []
    for label, value in context_options:
        action = QAction(label, context_menu)
        action.setCheckable(True)
        action.setData(value)

        def select_context(_checked, mode=value):
            config.update(cleanup_context_mode=mode)
            index = window.context_combo.findData(mode)
            if index >= 0:
                window.context_combo.blockSignals(True)
                window.context_combo.setCurrentIndex(index)
                window.context_combo.blockSignals(False)

        action.triggered.connect(select_context)
        context_group.addAction(action)
        context_menu.addAction(action)
        context_actions.append(action)
    writing_menu.addMenu(context_menu)

    microphone_menu = QMenu("Microphone", dictation_menu)
    microphone_group = QActionGroup(microphone_menu)
    microphone_group.setExclusive(True)
    microphone_actions = []
    try:
        input_devices = [
            str(info.get("name", "")).strip()
            for info in sd.query_devices()
            if info.get("max_input_channels", 0) > 0 and str(info.get("name", "")).strip()
        ]
    except Exception:
        logger.exception("couldn't enumerate menu-bar microphone devices")
        input_devices = []
    # Preserve order while avoiding duplicate CoreAudio aliases.
    input_devices = list(dict.fromkeys(input_devices))
    for label, value in [("System default", "")] + [(name, name) for name in input_devices]:
        action = QAction(label, microphone_menu)
        action.setCheckable(True)
        action.setData(value)

        def select_microphone(_checked, device=value):
            config.update(input_device=device)
            index = window.input_device_combo.findData(device)
            if index >= 0:
                window.input_device_combo.blockSignals(True)
                window.input_device_combo.setCurrentIndex(index)
                window.input_device_combo.blockSignals(False)

        action.triggered.connect(select_microphone)
        microphone_group.addAction(action)
        microphone_menu.addAction(action)
        microphone_actions.append(action)
    dictation_menu.addMenu(microphone_menu)

    # The status-item menu is intentionally a concise glance surface. The
    # complete Dictation/Writing/Permissions menus remain in the native app
    # menu, so the two Mac menu locations no longer duplicate one another.
    latest_update_url = {"value": ""}
    update_action = QAction("Download update…", menu)
    update_action.setVisible(False)
    update_action.triggered.connect(
        lambda: webbrowser.open(latest_update_url["value"] or "https://github.com/ajaitly11/Chatter/releases/latest")
    )
    menu.addAction(update_action)
    menu.addSeparator()
    menu.addAction(menu_bar_icon_action)

    def refresh_menu_state():
        cfg_now = config.load()
        mic = "ready" if permissions.is_microphone_authorized() else "needs permission"
        input_state = "ready" if permissions.input_monitoring_available() else "needs permission"
        access = "ready" if permissions.is_trusted() else "needs permission"
        summary = insights.summarize(history.load(kind="dictation"), days=1)
        snapshot_words_action.setText(f"Today · {summary.words_today:,} words")
        snapshot_sessions_action.setText(
            f"{summary.sessions_today} dictation{'s' if summary.sessions_today != 1 else ''} · stored locally"
        )
        permission_status_action.setText(
            f"Microphone {mic} · Input Monitoring {input_state} · Accessibility {access}"
        )
        ptt_action.blockSignals(True)
        ptt_action.setChecked(cfg_now.get("push_to_talk_enabled", True))
        ptt_action.blockSignals(False)
        activation_mode = normalize_activation_mode(
            cfg_now.get("hotkey_activation_mode", HOLD_TO_TALK)
        )
        for action in activation_actions:
            action.blockSignals(True)
            action.setChecked(action.data() == activation_mode)
            action.blockSignals(False)
        window.ptt_enabled_checkbox.blockSignals(True)
        window.ptt_enabled_checkbox.setChecked(cfg_now.get("push_to_talk_enabled", True))
        window.ptt_enabled_checkbox.blockSignals(False)
        cleanup_action.blockSignals(True)
        cleanup_action.setChecked(cfg_now.get("formatting_enabled", True))
        cleanup_action.blockSignals(False)
        current_context = cfg_now.get("cleanup_context_mode", "auto")
        for action in context_actions:
            action.blockSignals(True)
            action.setChecked(action.data() == current_context)
            action.blockSignals(False)
        current_device = cfg_now.get("input_device", "")
        for action in microphone_actions:
            action.blockSignals(True)
            action.setChecked(action.data() == current_device)
            action.blockSignals(False)
        menu_bar_icon_action.blockSignals(True)
        menu_bar_icon_action.setChecked(cfg_now.get("menu_bar_icon_enabled", True))
        menu_bar_icon_action.blockSignals(False)
        popover = tray_popover_ref.get("value")
        if popover is not None:
            popover.update_snapshot(summary.words_today, summary.sessions_today)
            popover.set_menu_bar_checked(cfg_now.get("menu_bar_icon_enabled", True))

    menu.aboutToShow.connect(refresh_menu_state)
    refresh_menu_state()

    def notify_update(version: str, release_url: str, dmg_url: str):
        latest_update_url["value"] = dmg_url or release_url
        update_action.setText(f"Download Chatter {version}…")
        update_action.setVisible(True)
        popover = tray_popover_ref.get("value")
        if popover is not None:
            popover.set_update_available(version)
        window.set_update_status(f"Chatter {version} is ready. Download it from the release page.")
        if config.load().get("notified_update_version", "") == version:
            return
        config.update(notified_update_version=version)
        try:
            from Foundation import NSUserNotification, NSUserNotificationCenter

            notification = NSUserNotification.alloc().init()
            notification.setTitle_("Chatter update available")
            notification.setInformativeText_(
                f"Chatter {version} is ready. Open Chatter or its menu-bar icon to download it."
            )
            notification.setSoundName_("NSUserNotificationDefaultSoundName")
            NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(notification)
        except Exception:
            logger.exception("couldn't deliver update notification")

    def update_is_current(version: str):
        window.set_update_status(f"You’re up to date · Chatter {version}")

    def update_check_failed(message: str):
        logger.info("update check unavailable: %s", message)
        window.set_update_status("Update check unavailable right now. Try again later.", error=True)

    update_checker.available.connect(notify_update)
    update_checker.current.connect(update_is_current)
    update_checker.failed.connect(update_check_failed)
    window.update_check_requested.connect(update_checker.check)
    update_timer = QTimer(window)
    update_timer.setInterval(6 * 60 * 60 * 1000)
    update_timer.timeout.connect(update_checker.check)
    update_timer.start()
    QTimer.singleShot(2500, update_checker.check)

    menu.addSeparator()
    quit_action = QAction("Quit Chatter")
    runtime_closed = {"value": False}

    def close_runtime():
        """Release native model/server resources for every quit path."""
        if runtime_closed["value"]:
            return
        runtime_closed["value"] = True
        hotkey.shutdown()
        formatter.shutdown()
        service.close()
        streaming_service.close()

    app.aboutToQuit.connect(close_runtime)

    def do_quit():
        app.quit()

    quit_action.triggered.connect(do_quit)
    menu.addSeparator()
    menu.addAction(quit_action)

    tray_popover = TrayPopover(
        tray_icon,
        on_open=lambda: open_action.trigger(),
        on_insights=lambda: insights_action.trigger(),
        on_settings=lambda: settings_action.trigger(),
        on_update=lambda: webbrowser.open(
            latest_update_url["value"] or "https://github.com/ajaitly11/Chatter/releases/latest"
        ),
        on_toggle_menu_bar=set_menu_bar_icon,
        on_quit=do_quit,
    )
    tray_popover_ref["value"] = tray_popover

    def show_tray_popover(_reason):
        refresh_menu_state()
        tray_popover.show_at_cursor()

    tray.activated.connect(show_tray_popover)
    # Populate the first snapshot after the popover exists. The earlier call
    # keeps all native action state ready for the first menu-bar click.
    refresh_menu_state()

    # Expose the important commands in the native macOS application menu too.
    # The status-item menu remains the compact background control surface;
    # native menus keep one non-duplicated home for each app command family:
    # File, Edit, Dictation, Permissions, View, Window, Help.
    native_menu = window.menuBar()
    native_menu.setNativeMenuBar(True)
    chatter_menu = native_menu.addMenu("Chatter")
    # Keep the application menu focused on app-level commands. Window and
    # destination navigation live in their standard native menus below; the
    # same actions appearing in several native menus made the menu bar noisy
    # and, on macOS, could produce duplicate Preferences/Open entries.
    chatter_menu.addAction(settings_action)
    chatter_menu.addSeparator()
    chatter_menu.addAction(quit_action)
    chatter_menu.aboutToShow.connect(refresh_menu_state)

    file_menu = native_menu.addMenu("File")
    open_media_action = QAction("Open audio or video…", window)

    def open_media():
        present_window(window.tabs.widget(1))
        window.open_file()

    open_media_action.triggered.connect(open_media)
    file_menu.addAction(open_media_action)

    edit_menu = native_menu.addMenu("Edit")

    def focused_edit_call(method):
        widget = app.focusWidget()
        callback = getattr(widget, method, None) if widget is not None else None
        if callable(callback):
            callback()
            return

        # A saved transcript is immutable, so Copy is the only Edit command
        # with a safe no-focus fallback. Do not silently map Cut, Paste, Undo,
        # Redo, or Select All onto history and risk changing the wrong target.
        if method == "copy" and last_transcript["value"].strip():
            app.clipboard().setText(last_transcript["value"])

    for title, method, shortcut in (
        ("Undo", "undo", QKeySequence.StandardKey.Undo),
        ("Redo", "redo", QKeySequence.StandardKey.Redo),
        ("Cut", "cut", QKeySequence.StandardKey.Cut),
        ("Copy", "copy", QKeySequence.StandardKey.Copy),
        ("Paste", "paste", QKeySequence.StandardKey.Paste),
        ("Select All", "selectAll", QKeySequence.StandardKey.SelectAll),
    ):
        action = QAction(title, window)
        action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(lambda _checked=False, name=method: focused_edit_call(name))
        edit_menu.addAction(action)

    clear_line_action = QAction("Delete Current Line", window)
    # PyQt6 swaps Ctrl/Meta on macOS by default, so the "Ctrl+" spelling is
    # what actually displays and matches the physical Command key here — see
    # LiveDictationTextEdit._is_line_clear_event for the same discovery.
    clear_line_action.setShortcuts([
        QKeySequence("Ctrl+Backspace"),
        QKeySequence("Ctrl+Del"),
    ])
    clear_line_action.triggered.connect(
        lambda _checked=False: focused_edit_call("clear_current_line")
    )
    edit_menu.addSeparator()
    edit_menu.addAction(clear_line_action)

    native_dictation_menu = native_menu.addMenu("Dictation")
    native_dictation_menu.addAction(ptt_action)
    native_dictation_menu.addAction(activation_menu.menuAction())
    native_dictation_menu.addAction(microphone_menu.menuAction())

    native_permissions_menu = native_menu.addMenu("Permissions")
    native_permissions_menu.addAction(finish_setup_action)
    native_permissions_menu.addSeparator()
    native_permissions_menu.addAction(permission_status_action)
    native_permissions_menu.addAction(check_permissions_action)

    view_menu = native_menu.addMenu("View")
    view_actions = []
    for index, label in enumerate(("Live Dictation", "Transcribed Files", "Dictionary", "History", "Insights")):
        view_action = QAction(label, window)
        view_action.setCheckable(True)
        view_action.triggered.connect(lambda _checked=False, tab_index=index: window.tabs.setCurrentIndex(tab_index))
        view_menu.addAction(view_action)
        view_actions.append(view_action)
    view_menu.addSeparator()
    refresh_insights_action = QAction("Refresh insights", window)
    refresh_insights_action.triggered.connect(window._reload_insights)
    view_menu.addAction(refresh_insights_action)

    def refresh_view_menu():
        current_index = window.tabs.currentIndex()
        for index, action in enumerate(view_actions):
            action.blockSignals(True)
            action.setChecked(index == current_index)
            action.blockSignals(False)

    view_menu.aboutToShow.connect(refresh_view_menu)
    window.tabs.currentChanged.connect(lambda _index: refresh_view_menu())

    window_menu = native_menu.addMenu("Window")
    minimize_action = QAction("Minimize", window)
    minimize_action.setShortcut(QKeySequence("Meta+M"))
    minimize_action.triggered.connect(window.showMinimized)
    window_menu.addAction(minimize_action)

    help_menu = native_menu.addMenu("Help")
    model_guide_action = QAction("Model guide…", window)
    model_guide_action.triggered.connect(window._open_advanced_settings)
    help_menu.addAction(model_guide_action)
    about_action = QAction("About Chatter", window)
    about_action.triggered.connect(lambda: QMessageBox.about(window, "About Chatter", "Local voice, with a little character."))
    help_menu.addAction(about_action)

    logger.info(
        "menu bar status item: available=%s",
        QSystemTrayIcon.isSystemTrayAvailable(),
    )
    tray.show()
    tray.setVisible(config.load().get("menu_bar_icon_enabled", True))

    # Accessibility trust is tied to *this* launching bundle's identity
    # (Chatter.app vs. a bare `python main.py` run from Terminal each get
    # their own entry) — the onboarding flow's second step actively
    # requests it so macOS shows the real permission prompt and lists this
    # app in System Settings, instead of auto-paste silently doing nothing.
    # Privacy settings can be revoked or can take a restart to refresh. That
    # must disable only the affected feature, not lock the user out of the
    # app. Setup is shown automatically once, then resumed from the menu bar.
    needs_onboarding = (
        not cfg.get("onboarding_complete", False)
        and not cfg.get("onboarding_dismissed", False)
    )
    if needs_onboarding:
        # Register this frozen bundle with macOS before the onboarding dialog
        # opens. This makes the permission entry appear as Chatter and lets
        # the dialog explain the next step.
        if not permissions.is_microphone_authorized():
            permissions.request_microphone_access()
        onboarding = OnboardingWindow(window)
        onboarding.exec()
        if onboarding.permissions_ready:
            config.update(
                onboarding_complete=True,
                onboarding_dismissed=False,
                onboarding_permission_restart_attempted=False,
            )
        elif onboarding.dismissed:
            config.update(
                onboarding_dismissed=True,
                onboarding_permission_restart_attempted=False,
            )
        window._refresh_permission_status()
        cfg = config.load()

    if permissions_ready() and not cfg.get("onboarding_complete", False):
        config.update(onboarding_complete=True, onboarding_dismissed=False)
        cfg = config.load()

    logger.info(
        "permission preflight: microphone=%s input_monitoring_preflight=%s "
        "input_monitoring_available=%s accessibility=%s",
        permissions.is_microphone_authorized(),
        permissions.is_input_monitoring_trusted(),
        permissions.input_monitoring_available(),
        permissions.is_trusted(),
    )

    if cfg.get("push_to_talk_enabled", True) and permissions_ready():
        hotkey.start()
    elif cfg.get("push_to_talk_enabled", True):
        logger.warning(
            "push-to-talk listener not started: mic=%s input_monitoring=%s accessibility=%s",
            permissions.is_microphone_authorized(),
            permissions.input_monitoring_available(),
            permissions.is_trusted(),
        )

    # Do not eagerly load native models at app startup. A single Nemotron
    # checkpoint uses roughly 850 MB of macOS physical footprint on Metal even
    # before the first utterance. The hotkey collector already loads the live
    # model before it consumes its queued audio, so this defers that cost until
    # the feature is actually used without changing model, backend, or output.
    # The optional formatter follows the same lazy path in format_transcript().
    logger.info("native model warm-up deferred until first use")

    window.show()
    sys.exit(app.exec())
