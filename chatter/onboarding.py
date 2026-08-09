"""First-run permission flow — microphone, Input Monitoring, Accessibility,
and done,
replacing the old QMessageBox prompt with the mockup's card design. Shown
once, gated by config.py's onboarding_complete flag.
"""

import shlex
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from . import config
from . import permissions
from . import theme
from .audio_capture import StreamingMicRecorder

_STEPS = [
    dict(
        icon="🎙",
        title="Hear you out",
        body="Chatter needs access to your chosen microphone to turn your voice into text. Audio is processed locally and never leaves your Mac.",
        button="Allow microphone",
    ),
    dict(
        icon="↗",
        title="Type where you are",
        body="Accessibility access lets Chatter insert the final text where your cursor is. It does not send your text anywhere.",
        button="Open Accessibility",
    ),
    dict(
        icon="⌨",
        title="Listen for your hotkey",
        body="Input Monitoring lets Chatter notice your push-to-talk key anywhere on your Mac. Chatter only watches the configured hotkey.",
        button="Open Input Monitoring",
    ),
    dict(
        icon="✓",
        title="You're all set",
        body="Hold your hotkey, speak, let go. One local streaming model shows a preview and finalizes the same transcript on release.",
        button="Start dictating",
    ),
]


class OnboardingWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to Chatter")
        self.setFixedSize(320, 420)
        self._step = self._first_incomplete_step()
        self._mic_ready = permissions.is_microphone_authorized()
        if self._step == 0:
            self._mic_ready = False
        self._settings_opened = False
        self._restart_required = False
        self._dismissed = False
        self._permission_timer = QTimer(self)
        self._permission_timer.setInterval(600)
        self._permission_timer.timeout.connect(self._refresh_permission_step)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(12)

        dots_row = QHBoxLayout()
        dots_row.addStretch(1)
        self._dots = []
        for _ in _STEPS:
            dot = QLabel()
            dot.setFixedSize(16, 5)
            self._dots.append(dot)
            dots_row.addWidget(dot)
        dots_row.addStretch(1)
        v.addLayout(dots_row)

        icon_row = QHBoxLayout()
        icon_row.addStretch(1)
        self._icon = QLabel()
        self._icon.setFixedSize(52, 52)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_font = self._icon.font()
        icon_font.setPointSize(20)
        self._icon.setFont(icon_font)
        icon_row.addWidget(self._icon)
        icon_row.addStretch(1)
        v.addLayout(icon_row)

        self._title = QLabel()
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {theme.TEXT};")
        v.addWidget(self._title)

        self._body = QLabel()
        self._body.setWordWrap(True)
        self._body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        v.addWidget(self._body, stretch=1)

        self._button = QPushButton()
        self._button.setObjectName("primary")
        self._button.clicked.connect(self._on_button)
        v.addWidget(self._button)

        later_button = QPushButton("Continue to Chatter")
        later_button.setObjectName("secondary")
        later_button.setToolTip("You can finish permissions later from the Chatter menu bar icon.")
        later_button.clicked.connect(self._continue_without_setup)
        v.addWidget(later_button)

        self._render_step()

    @staticmethod
    def _first_incomplete_step() -> int:
        """Open onboarding at the first permission macOS still needs."""
        if not permissions.is_microphone_authorized():
            return 0
        if not permissions.is_trusted():
            return 1
        if not permissions.input_monitoring_available():
            return 2
        return 3

    @staticmethod
    def _bundle_path() -> str | None:
        executable = Path(sys.executable).resolve()
        parts = executable.parts
        for index, part in enumerate(parts):
            if part.endswith(".app") and index + 3 < len(parts):
                return str(Path(*parts[: index + 1]))
        return None

    def _restart_and_recheck(self):
        """Relaunch the bundle when TCC has not refreshed in this process."""
        bundle = self._bundle_path()
        if not bundle:
            self._body.setText(
                _STEPS[self._step]["body"]
                + "\n\nPlease quit and reopen Chatter from Applications after enabling this permission."
            )
            self._button.setText("Check again")
            self._restart_required = False
            return

        self._button.setEnabled(False)
        self._body.setText(
            _STEPS[self._step]["body"]
            + "\n\nRestarting Chatter once so macOS can apply the permission to this bundle…"
        )
        quoted_bundle = shlex.quote(bundle)
        # Let the current process release its instance lock before the new
        # bundle is opened. The new process calculates the first incomplete
        # step again, so it resumes here when the permission is recognized.
        subprocess.Popen(
            ["/bin/sh", "-c", f"sleep 1; open -a {quoted_bundle}"],
            start_new_session=True,
        )
        QTimer.singleShot(100, QApplication.instance().quit)

    def _render_step(self):
        step = _STEPS[self._step]
        for i, dot in enumerate(self._dots):
            color = theme.ACTIVE if i == self._step else theme.BORDER
            dot.setStyleSheet(f"background: {color}; border-radius: 2px;")
        self._icon.setText(step["icon"])
        self._icon.setStyleSheet(
            f"background: {theme.rgba_str(theme.active_dim())}; border: 1px solid {theme.ACTIVE}; "
            f"border-radius: 26px; color: {theme.ACTIVE};"
        )
        self._title.setText(step["title"])
        self._body.setText(step["body"])
        self._button.setText(step["button"])

    def _on_button(self):
        if self._step == 0:
            # Opening (and immediately closing) a mic input stream is what
            # actually triggers macOS's mic-permission prompt — doing it
            # here means the prompt appears right when we've just explained
            # why, rather than silently the first time push-to-talk is held.
            mic_status = permissions.microphone_authorization_status()
            if mic_status == "not_determined":
                permissions.request_microphone_access()
                # This is a native prompt, not the System Settings pane. If
                # the user denies it, the next click must still be able to
                # open Microphone settings and recover without restarting.
                self._settings_opened = False
                self._permission_timer.start()
                self._body.setText(
                    _STEPS[self._step]["body"]
                    + "\n\nAllow Chatter in the macOS prompt, then click this button again."
                )
                self._button.setText("I allowed it — continue")
                return
            if mic_status != "authorized":
                if not self._settings_opened:
                    permissions.open_microphone_settings()
                    self._settings_opened = True
                    self._permission_timer.start()
                self._body.setText(
                    _STEPS[self._step]["body"]
                    + "\n\nChatter is not allowed to use the microphone. Enable Chatter in Microphone settings, then click again."
                )
                self._button.setText("I enabled it — continue")
                return
            try:
                recorder = StreamingMicRecorder()
                recorder.start(device=config.load().get("input_device", ""))
                recorder.stop()
                self._mic_ready = True
                self._advance()
            except Exception as exc:
                self._body.setText(
                    _STEPS[self._step]["body"]
                    + f"\n\nChatter could not open that device yet: {exc}"
                )
        elif self._step == 1:
            if permissions.is_trusted():
                self._advance()
            elif self._restart_required:
                self._restart_and_recheck()
            elif not self._settings_opened:
                permissions.request_trust()
                permissions.open_accessibility_settings()
                self._settings_opened = True
                self._permission_timer.start()
                self._body.setText(
                    _STEPS[self._step]["body"]
                    + "\n\nTurn on the Chatter switch in Accessibility, then return here. The button will update when macOS sees the change."
                )
                self._button.setText("Check Accessibility")
            else:
                # Do an active TCC refresh on the user's explicit check.
                # AXIsProcessTrusted can remain stale until its prompting
                # variant is called after returning from System Settings.
                if permissions.request_trust():
                    self._permission_timer.stop()
                    self._settings_opened = False
                    self._advance()
                    return
                self._body.setText(
                    _STEPS[self._step]["body"]
                    + "\n\nmacOS has not refreshed this permission in the current process. Click below once to restart Chatter and check the toggle again."
                )
                self._button.setText("Restart Chatter and check")
                self._restart_required = True
        elif self._step == 2:
            if permissions.input_monitoring_available():
                self._advance()
            elif self._restart_required:
                self._restart_and_recheck()
            elif not self._settings_opened:
                permissions.request_input_monitoring()
                permissions.open_input_monitoring_settings()
                self._settings_opened = True
                self._permission_timer.start()
                self._body.setText(
                    _STEPS[self._step]["body"]
                    + "\n\nTurn on Chatter if it appears, then return here. The button will update when macOS sees the change. "
                    "If it is missing, relaunch Chatter from Applications after finishing Accessibility."
                )
                self._button.setText("Check Input Monitoring")
            else:
                self._body.setText(
                    _STEPS[self._step]["body"]
                    + "\n\nmacOS has not refreshed this permission in the current process. Click below once to restart Chatter and check the toggle again."
                )
                self._button.setText("Restart Chatter and check")
                self._restart_required = True
        else:
            self.accept()

    def _continue_without_setup(self):
        """Leave setup without making permissions a launch-blocking trap."""
        self._dismissed = True
        config.update(onboarding_dismissed=True)
        self.accept()

    def reject(self):
        # Treat the window close button like the explicit secondary action.
        # A first-run dialog should never force a user to rediscover a stale
        # permission state before they can reach the main app.
        self._dismissed = True
        config.update(onboarding_dismissed=True)
        super().reject()

    def _refresh_permission_step(self):
        """Poll while System Settings is open so the user gets confirmation
        without repeatedly reopening the same pane."""
        if self._step == 0 and permissions.is_microphone_authorized():
            self._permission_timer.stop()
            self._settings_opened = False
            self._body.setText(_STEPS[self._step]["body"] + "\n\nMicrophone access is ready.")
            self._button.setText("Continue")
        elif self._step == 1 and permissions.is_trusted():
            self._permission_timer.stop()
            self._settings_opened = False
            self._body.setText(_STEPS[self._step]["body"] + "\n\nAccessibility is ready.")
            self._button.setText("Continue")
        elif self._step == 2 and permissions.input_monitoring_available():
            self._permission_timer.stop()
            self._settings_opened = False
            self._body.setText(_STEPS[self._step]["body"] + "\n\nInput Monitoring is ready.")
            self._button.setText("Continue")

    @property
    def permissions_ready(self) -> bool:
        return (
            self._mic_ready
            and permissions.is_microphone_authorized()
            and permissions.input_monitoring_available()
            and permissions.is_trusted()
        )

    @property
    def dismissed(self) -> bool:
        return self._dismissed

    def _advance(self):
        self._permission_timer.stop()
        self._settings_opened = False
        self._restart_required = False
        self._step = min(self._step + 1, len(_STEPS) - 1)
        self._render_step()
