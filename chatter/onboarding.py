"""First-run permission flow — three steps (mic, accessibility, done),
replacing the old QMessageBox prompt with the mockup's card design. Shown
once, gated by config.py's onboarding_complete flag.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from . import permissions
from . import theme
from .audio_capture import StreamingMicRecorder

_STEPS = [
    dict(
        icon="🎙",
        title="Hear you out",
        body="Chatter needs mic access to turn your voice into text. Nothing ever leaves your Mac.",
        button="Allow microphone",
    ),
    dict(
        icon="⌨",
        title="One more thing",
        body="Accessibility access lets Chatter type where your cursor is.",
        button="Open System Settings",
    ),
    dict(
        icon="✓",
        title="You're all set",
        body="Hold your hotkey, speak, let go. Chatter takes it from there.",
        button="Start dictating",
    ),
]


class OnboardingWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to Chatter")
        self.setFixedSize(260, 340)
        self._step = 0

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

        self._render_step()

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
            try:
                recorder = StreamingMicRecorder()
                recorder.start()
                recorder.stop()
            except Exception:
                pass
            self._advance()
        elif self._step == 1:
            if not permissions.is_trusted():
                permissions.request_trust()
                permissions.open_accessibility_settings()
            self._advance()
        else:
            self.accept()

    def _advance(self):
        self._step = min(self._step + 1, len(_STEPS) - 1)
        self._render_step()
