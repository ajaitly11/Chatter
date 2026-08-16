"""The themed status-item popover.

macOS renders QSystemTrayIcon context menus with the system NSMenu theme and
ignores most Qt stylesheet rules. Chatter's status item is a small Insights
surface rather than a conventional command menu, so use a lightweight Qt
popup for that surface and keep the full command families in the native app
menu.
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCursor, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme


class TrayPopover(QFrame):
    """A small, fully themed popover anchored to the menu-bar click."""

    def __init__(
        self,
        icon: QIcon,
        *,
        on_open: Callable[[], None],
        on_insights: Callable[[], None],
        on_settings: Callable[[], None],
        on_update: Callable[[], None],
        on_toggle_menu_bar: Callable[[bool], None],
        on_quit: Callable[[], None],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("chatterTrayPopover")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Popup
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(310)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 10)
        shadow.setColor(theme.qcolor("#000000", 150))
        self.setGraphicsEffect(shadow)

        self.setStyleSheet(
            f"""
            QFrame#chatterTrayPopover {{
                background-color: {theme.SURFACE};
                color: {theme.TEXT};
                border: 1px solid {theme.BORDER};
                border-radius: 14px;
            }}
            QLabel {{
                background: transparent;
                color: {theme.TEXT};
            }}
            QLabel#trayEyebrow {{
                color: {theme.ACTIVE};
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1px;
            }}
            QLabel#trayMetric {{
                color: {theme.TEXT_DIM};
                font-size: 13px;
                font-weight: 600;
            }}
            QLabel#trayUpdate {{
                color: {theme.ACTIVE};
                font-size: 11px;
                font-weight: 700;
            }}
            QPushButton#trayAction {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 8px;
                color: {theme.TEXT};
                font-size: 14px;
                padding: 8px 10px;
                text-align: left;
            }}
            QPushButton#trayAction:hover {{
                background: {theme.SURFACE2};
                border-color: {theme.BORDER};
            }}
            QPushButton#trayAction:pressed {{
                background: {theme.ACTIVE};
                color: {theme.BG};
            }}
            QPushButton#trayUpdateAction {{
                background: {theme.ACTIVE};
                border: 1px solid {theme.ACTIVE};
                border-radius: 8px;
                color: {theme.BG};
                font-size: 13px;
                font-weight: 700;
                padding: 8px 10px;
                text-align: left;
            }}
            QPushButton#trayUpdateAction:hover {{
                background: {theme.PROCESSING};
                border-color: {theme.PROCESSING};
            }}
            QCheckBox#trayCheck {{
                spacing: 8px;
                color: {theme.TEXT};
                font-size: 13px;
                font-weight: 600;
                padding: 5px 3px;
            }}
            QCheckBox#trayCheck::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 8px;
                border: 1px solid {theme.BORDER};
                background: {theme.BG};
            }}
            QCheckBox#trayCheck::indicator:checked {{
                border-color: {theme.ACTIVE};
                background: {theme.ACTIVE};
            }}
            QFrame#trayRule {{
                background: {theme.BORDER};
                min-height: 1px;
                max-height: 1px;
            }}
            """
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 15, 16, 14)
        outer.setSpacing(7)

        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 2)
        mark = QLabel()
        mark.setPixmap(icon.pixmap(22, 22))
        mark.setFixedSize(22, 22)
        heading.addWidget(mark)
        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(0)
        title = QLabel("Chatter")
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 15px; font-weight: 700;")
        title_col.addWidget(title)
        eyebrow = QLabel("LOCAL VOICE SNAPSHOT")
        eyebrow.setObjectName("trayEyebrow")
        title_col.addWidget(eyebrow)
        heading.addLayout(title_col, stretch=1)
        outer.addLayout(heading)

        self.words_label = QLabel("Today · 0 words")
        self.words_label.setObjectName("trayMetric")
        self.sessions_label = QLabel("0 dictations · stored locally")
        self.sessions_label.setObjectName("trayMetric")
        outer.addWidget(self.words_label)
        outer.addWidget(self.sessions_label)

        outer.addWidget(self._rule())
        outer.addWidget(self._action("Open Chatter", on_open))
        outer.addWidget(self._action("See more insights", on_insights))
        outer.addWidget(self._action("Settings…", on_settings))

        self.update_button = self._action("Download update…", on_update, update=True)
        self.update_button.hide()
        outer.addWidget(self.update_button)

        outer.addWidget(self._rule())
        self.menu_bar_checkbox = QCheckBox("Show Chatter in menu bar")
        self.menu_bar_checkbox.setObjectName("trayCheck")
        self.menu_bar_checkbox.toggled.connect(on_toggle_menu_bar)
        outer.addWidget(self.menu_bar_checkbox)
        outer.addWidget(self._action("Quit Chatter", on_quit))

        self.adjustSize()

    @staticmethod
    def _rule() -> QFrame:
        rule = QFrame()
        rule.setObjectName("trayRule")
        rule.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return rule

    @staticmethod
    def _action(text: str, callback: Callable[[], None], *, update: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("trayUpdateAction" if update else "trayAction")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    def update_snapshot(self, words_today: int, sessions_today: int):
        self.words_label.setText(f"Today · {words_today:,} words")
        suffix = "dictation" if sessions_today == 1 else "dictations"
        self.sessions_label.setText(f"{sessions_today} {suffix} · stored locally")

    def set_menu_bar_checked(self, checked: bool):
        self.menu_bar_checkbox.blockSignals(True)
        self.menu_bar_checkbox.setChecked(checked)
        self.menu_bar_checkbox.blockSignals(False)

    def set_update_available(self, version: str, visible: bool = True):
        self.update_button.setText(f"Download Chatter {version}…")
        self.update_button.setVisible(visible)
        self.adjustSize()

    def show_at_cursor(self):
        self.adjustSize()
        cursor = QCursor.pos()
        screen = QApplication.screenAt(cursor) or QApplication.primaryScreen()
        if screen is None:
            self.move(cursor.x() - self.width() // 2, cursor.y() + 8)
        else:
            bounds = screen.availableGeometry()
            x = max(bounds.left() + 8, min(cursor.x() - self.width() // 2, bounds.right() - self.width() - 8))
            y = cursor.y() + 8
            if y + self.height() > bounds.bottom() - 8:
                y = cursor.y() - self.height() - 8
            y = max(bounds.top() + 8, y)
            self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

