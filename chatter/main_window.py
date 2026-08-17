import logging
import math
import shutil
import threading
import time
import traceback
from datetime import timedelta
from pathlib import Path

import AppKit
import numpy as np
import objc
import sounddevice as sd
from PyQt6.QtCore import (
    QEvent,
    QEasingCurve,
    QLineF,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QPropertyAnimation,
    QThread,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QKeyEvent,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton as _QtPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import config
from . import dictionary
from . import history
from . import insights
from . import llama_runtime
from .live_editor import current_line_bounds
from . import permissions
from .audio_capture import StreamingMicRecorder
from . import theme
from .mascot import Mascot
from .native_hotkey import (
    ACTIVATION_MODE_OPTIONS,
    DOUBLE_TAP_PERSISTENT,
    HOLD_TO_TALK,
    SUPPORTED_HOTKEYS,
    normalize_activation_mode,
)
from .transcription_service import (
    CLEANUP_MODELS_DIR,
    MODELS_DIR,
    decode_to_pcm,
    list_models,
    segments_to_srt,
    words_to_srt,
    service,
)

MEDIA_EXTENSIONS = {".mp3", ".wav", ".m4a", ".mp4", ".mov", ".mkv", ".flac", ".aac"}
logger = logging.getLogger("chatter.main_window")

WINDOW_W = 760
WINDOW_H = 520
MIN_WINDOW_W = 680
MIN_WINDOW_H = 460
CONTENT_H = 340  # each tab's list areas default to about this tall, but expand with the window


class AnimatedButton(_QtPushButton):
    """A normal Qt button with a restrained, visible click sheen.

    The animation is paint-only: it does not resize the widget, steal focus,
    or schedule work on the audio/transcription path. Keeping the effect here
    means every button in the app, including dialog buttons, gets the same
    small premium interaction cue.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shine_position = -1.0
        self._shine_animation = QPropertyAnimation(self, b"shinePosition", self)
        self._shine_animation.setDuration(260)
        self._shine_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.pressed.connect(self._start_shine)
        self._shine_animation.finished.connect(self._finish_shine)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _start_shine(self):
        if not self.isEnabled():
            return
        self._shine_animation.stop()
        self._shine_animation.setStartValue(-0.35)
        self._shine_animation.setEndValue(1.35)
        self._shine_animation.start()

    def _finish_shine(self):
        self._shine_position = -1.0
        self.update()

    def _get_shine_position(self):
        return self._shine_position

    def _set_shine_position(self, value):
        self._shine_position = float(value)
        self.update()

    shinePosition = pyqtProperty(float, _get_shine_position, _set_shine_position)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isEnabled() or self._shine_position < -0.2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)
        painter.setClipPath(clip)
        center = self._shine_position * (self.width() + 56) - 28
        gradient = QLinearGradient(center - 28, 0, center + 28, 0)
        gradient.setColorAt(0.0, QColor(255, 255, 255, 0))
        gradient.setColorAt(0.5, QColor(255, 255, 255, 42))
        gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRect(self.rect())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        outline = QColor(theme.ACTIVE)
        outline.setAlpha(150)
        painter.setPen(QPen(outline, 1.2))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)


# Keep the existing construction sites concise while upgrading every button.
QPushButton = AnimatedButton


class ToggleSwitch(QCheckBox):
    """A warm, compact switch that reads like a native preference control."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._knob_position = 1.0 if self.isChecked() else 0.0
        self._knob_animation = QPropertyAnimation(self, b"knobPosition", self)
        self._knob_animation.setDuration(180)
        self._knob_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate_knob)
        self.setMinimumHeight(28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _get_knob_position(self):
        return self._knob_position

    def _set_knob_position(self, value):
        self._knob_position = float(value)
        self.update()

    knobPosition = pyqtProperty(float, _get_knob_position, _set_knob_position)

    def _animate_knob(self, checked: bool):
        self._knob_animation.stop()
        self._knob_animation.setStartValue(self._knob_position)
        self._knob_animation.setEndValue(1.0 if checked else 0.0)
        self._knob_animation.start()

    def sizeHint(self):
        text_width = self.fontMetrics().horizontalAdvance(self.text())
        return QSize(max(72, text_width + 62), 30)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(0, (self.height() - 20) / 2, 38, 20)
        if self.isEnabled():
            track_color = QColor(theme.ACTIVE if self.isChecked() else theme.SURFACE2)
            knob_color = QColor(theme.TEXT)
            text_color = QColor(theme.TEXT)
        else:
            track_color = QColor(theme.SURFACE2)
            knob_color = QColor(theme.TEXT_DIM)
            text_color = QColor(theme.TEXT_DIM)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, 10, 10)
        knob_x = 3 + self._knob_position * 18
        painter.setBrush(knob_color)
        painter.drawEllipse(QRectF(knob_x, track.y() + 3, 14, 14))
        painter.setPen(text_color)
        painter.drawText(
            QRectF(52, 0, max(0, self.width() - 52), self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self.text(),
        )


def _match_titlebar_to_theme(window: QWidget):
    """Makes the native titlebar strip (where the traffic-light buttons
    live) render in the app's own background color instead of the system's
    default gray, so it reads as part of the app rather than bolted-on OS
    chrome. Qt has no cross-platform API for this — same
    objc.objc_object(winId()) trick overlay.py uses for window level."""
    try:
        ns_view = objc.objc_object(c_void_p=int(window.winId()))
        ns_window = ns_view.window()
        ns_window.setTitlebarAppearsTransparent_(True)
        bg = QColor(theme.BG)
        ns_window.setBackgroundColor_(
            AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                bg.redF(), bg.greenF(), bg.blueF(), 1.0
            )
        )
    except Exception:
        logger.exception("couldn't match the titlebar color to the theme")

def _pill(text: str, color: str, bg: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(
        f"color: {color}; background: {bg}; border-radius: 999px; "
        f"padding: 3px 10px; font-size: 11px; font-weight: 600;"
    )
    return label


class TranscribeWorker(QThread):
    finished = pyqtSignal(str, object, object, object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, model_path: str, backend: str, media_path: str, formatter, format_enabled: bool, timestamp_kind: str):
        super().__init__()
        self.model_path = model_path
        self.backend = backend
        self.media_path = media_path
        self.formatter = formatter
        self.format_enabled = format_enabled
        self.timestamp_kind = timestamp_kind

    def run(self):
        try:
            self.progress.emit("Decoding audio…")
            pcm = decode_to_pcm(self.media_path)

            self.progress.emit("Transcribing…")
            # The service requests the finest timing the selected model can
            # provide, then safely downgrades to segment timing when needed.
            language = config.load().get("language", "en") or None
            result = service.transcribe(
                pcm, self.model_path, self.backend, keep_model=False,
                language=language, timestamps=self.timestamp_kind,
            )
            text = dictionary.apply_corrections(result.text)
            words = getattr(result, "words", None)
            segments = getattr(result, "segments", None)
            tokens = getattr(result, "tokens", None)

            if self.format_enabled and text.strip():
                self.progress.emit("Cleaning up with AI…")
                text = self.formatter.format_transcript(text)

            self.finished.emit(text, words, segments, tokens)
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


class MicTestWorker(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, device):
        super().__init__()
        self.device = device

    def run(self):
        recorder = StreamingMicRecorder()
        try:
            recorder.start(device=self.device)
            time.sleep(0.8)
            recorder.stop()
            chunks = list(recorder.chunks())
            pcm = np.concatenate(chunks) if chunks else np.array([], dtype=np.float32)
            rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))) if pcm.size else 0.0
            if rms < 0.005:
                self.finished.emit(False, f"Very little audio detected (RMS {rms:.4f}). Check the selected device and macOS microphone permission.")
            else:
                self.finished.emit(True, f"Microphone looks good (RMS {rms:.4f}).")
        except Exception as exc:
            try:
                recorder.stop()
            except Exception:
                pass
            self.finished.emit(False, f"Microphone test failed: {exc}")


def _card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    return frame


def _scroll_list(inner: QWidget, min_height: int = CONTENT_H - 90) -> QScrollArea:
    """A scrollable list that grows with the window instead of clipping at a
    fixed height — callers add it to their layout with stretch=1."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setMinimumHeight(min_height)
    area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(inner)
    return area


def _squiggle_path(
    width: float,
    y: float,
    amplitude: float = 2.6,
    period: float = 11.0,
    phase: float = 0.0,
) -> QPainterPath:
    """A repeating wave — chained quadratic Beziers alternating the control
    point above/below the baseline — matching the mockup's hand-drawn
    squiggle underline (its SVG paths are literally this same
    Q-then-repeat-T pattern) rather than a plain straight line."""
    path = QPainterPath()
    path.moveTo(0, y)
    x = 0.0
    while x < width - 0.01:
        end_x = min(x + period, width)
        cx = (x + end_x) / 2
        cy = y - amplitude * math.sin((cx / period) * math.pi + phase)
        path.quadTo(cx, cy, end_x, y)
        x = end_x
    return path


_SQUIGGLE_MARGIN = 16


class SquiggleTabBar(QTabBar):
    """Draws the active tab's underline as the mockup's wavy squiggle
    instead of a plain straight line, sliding smoothly to the newly
    selected tab instead of snapping (disappearing/reappearing) between
    them."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._underline_x = 0.0
        self._underline_w = 0.0
        self._underline_y = 0.0
        self._wave_phase = 0.0
        self._anim_x = QPropertyAnimation(self, b"underlineX")
        self._anim_w = QPropertyAnimation(self, b"underlineWidth")
        self._anim_phase = QPropertyAnimation(self, b"wavePhase")
        for anim in (self._anim_x, self._anim_w, self._anim_phase):
            anim.setDuration(220)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.currentChanged.connect(self._animate_to)

    def _get_underline_x(self):
        return self._underline_x

    def _set_underline_x(self, value):
        self._underline_x = value
        self.update()

    underlineX = pyqtProperty(float, _get_underline_x, _set_underline_x)

    def _get_underline_w(self):
        return self._underline_w

    def _set_underline_w(self, value):
        self._underline_w = value
        self.update()

    underlineWidth = pyqtProperty(float, _get_underline_w, _set_underline_w)

    def _get_wave_phase(self):
        return self._wave_phase

    def _set_wave_phase(self, value):
        self._wave_phase = value
        self.update()

    wavePhase = pyqtProperty(float, _get_wave_phase, _set_wave_phase)

    def _target(self, index: int):
        rect = self.tabRect(index)
        return rect.x() + _SQUIGGLE_MARGIN, rect.width() - 2 * _SQUIGGLE_MARGIN, rect.bottom() - 4

    def _sync_to_current(self):
        """Install a complete initial underline after QTabBar lays out tabs.

        On startup ``currentChanged`` can fire before tab geometry exists.
        Waiting one event-loop turn avoids the half-width/top-left squiggle
        that otherwise appears for the first frame of the window.
        """
        index = self.currentIndex()
        if index < 0:
            return
        target_x, target_w, target_y = self._target(index)
        if target_w <= 0:
            return
        for anim in (self._anim_x, self._anim_w, self._anim_phase):
            anim.stop()
        self._underline_x = float(target_x)
        self._underline_w = float(target_w)
        self._underline_y = float(target_y)
        self._wave_phase = 0.0
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_to_current)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._sync_to_current)

    def _animate_to(self, index: int):
        if index < 0:
            return
        target_x, target_w, target_y = self._target(index)
        if target_w <= 0:
            return
        self._underline_y = target_y
        self._anim_x.stop()
        self._anim_x.setStartValue(self._underline_x)
        self._anim_x.setEndValue(float(target_x))
        self._anim_x.start()
        self._anim_w.stop()
        self._anim_w.setStartValue(self._underline_w)
        self._anim_w.setEndValue(float(target_w))
        self._anim_w.start()
        self._anim_phase.stop()
        self._anim_phase.setStartValue(self._wave_phase)
        self._anim_phase.setEndValue(self._wave_phase + math.tau)
        self._anim_phase.start()

    def paintEvent(self, event):
        super().paintEvent(event)
        idx = self.currentIndex()
        if idx < 0:
            return
        if self._underline_w <= 0:
            # First paint after the tabs exist — jump straight to position
            # instead of animating in from the top-left corner.
            target_x, target_w, target_y = self._target(idx)
            if target_w <= 0:
                return
            self._underline_x, self._underline_w, self._underline_y = target_x, target_w, target_y
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(theme.ACTIVE), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.translate(self._underline_x, self._underline_y)
        painter.drawPath(_squiggle_path(self._underline_w, 0, phase=self._wave_phase))


class _ModelSlotPanel(QWidget):
    """One model 'slot' — the active transcription model, or the active
    text-cleanup model. Deliberately not a browsable/searchable catalog: an
    in-app list of every match read as confusing (direct feedback), and
    Hugging Face's search API doesn't give good multi-word results through
    a simple passthrough query (e.g. searching "Qwen" surfaced only one
    hit despite many existing). So instead: what's active right now, a
    button to import a .gguf file you've already downloaded yourself, and
    a link to where to find one."""

    def __init__(self, description: str, dest_dir: Path, config_key: str,
                 browse_url: str, browse_label: str, on_change=None,
                 runtime_widget: QWidget | None = None,
                 requires_streaming: bool = False,
                 fallback_paths: tuple[Path, ...] = ()):
        super().__init__()
        self.dest_dir = dest_dir
        self.config_key = config_key
        self.on_change = on_change
        self.requires_streaming = requires_streaming
        self.fallback_paths = fallback_paths

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(12)

        if runtime_widget is not None:
            v.addWidget(runtime_widget)

        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {theme.TEXT_DIM};")
        v.addWidget(desc)

        card = _card()
        h = QHBoxLayout(card)
        h.setContentsMargins(14, 12, 14, 12)
        info = QVBoxLayout()
        title = QLabel("ACTIVE MODEL")
        title.setObjectName("sectionTitle")
        info.addWidget(title)
        self.active_label = QLabel()
        self.active_label.setWordWrap(True)
        self.active_label.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
        info.addWidget(self.active_label)
        h.addLayout(info, stretch=1)
        import_btn = QPushButton("Import .gguf file…")
        import_btn.clicked.connect(self._import_file)
        h.addWidget(import_btn)
        v.addWidget(card)

        link = QLabel(f'Find models on Hugging Face: <a href="{browse_url}">{browse_label}</a>')
        link.setOpenExternalLinks(True)
        link.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        v.addWidget(link)
        v.addStretch(1)

        self.refresh()

    def refresh(self):
        current = config.load().get(self.config_key, "")
        if current and Path(current).exists():
            self.active_label.setText(Path(current).name)
            return
        fallback = next((path for path in self.fallback_paths if path.exists()), None)
        if fallback is not None:
            self.active_label.setText(f"Auto: {fallback.name}")
        else:
            self.active_label.setText("None selected yet — import a file below")

    def _import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a .gguf model file", "", "GGUF models (*.gguf)")
        if not path:
            return
        try:
            if self.requires_streaming:
                import transcribe_cpp
                probe = transcribe_cpp.Model(path, backend="cpu")
                try:
                    if not probe.capabilities.supports_streaming:
                        raise ValueError(
                            "This model does not support live streaming. Choose a streaming ASR model."
                        )
                finally:
                    probe.close()
            self.dest_dir.mkdir(parents=True, exist_ok=True)
            dest = self.dest_dir / Path(path).name
            if Path(path).resolve() != dest.resolve():
                shutil.copy2(path, dest)
            config.update(**{self.config_key: str(dest)})
        except Exception:
            logger.exception("model import failed")
            QMessageBox.critical(self, "Import failed", traceback.format_exc())
            return
        self.refresh()
        if self.on_change:
            self.on_change()


class _ModelGuideDialog(QDialog):
    """A short, in-app model chooser instead of asking people to read a file."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose a local model")
        self.setMinimumSize(520, 420)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 20)
        v.setSpacing(12)

        eyebrow = QLabel("A SIMPLE START")
        eyebrow.setObjectName("sectionTitle")
        v.addWidget(eyebrow)
        title = QLabel("Choose the job, not a catalog.")
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 20px; font-weight: 700;")
        v.addWidget(title)
        intro = QLabel(
            "Chatter keeps live dictation fast with one streaming ASR model. "
            "A second, small local model is optional and only polishes the text."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_DIM};")
        v.addWidget(intro)

        steps = (
            ("1", "Start with live dictation", "Use Nemotron 3.5 streaming for push-to-talk. It is the model that produces the live preview and the final raw transcript."),
            ("2", "Add cleanup only if you want it", "Choose a small 2B–4B instruct GGUF for punctuation, corrections, and context-aware formatting. It runs locally after the stream."),
            ("3", "Match it to your Mac", "8 GB: keep cleanup off. 16 GB: Nemotron + a small cleanup model. 24 GB or more: you can try a larger cleanup model, but measure latency."),
        )
        for number, heading, body in steps:
            card = _card()
            h = QHBoxLayout(card)
            h.setContentsMargins(12, 10, 12, 10)
            badge = _pill(number, theme.ACTIVE, theme.rgba_str(theme.active_dim()))
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedWidth(24)
            h.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)
            text_col = QVBoxLayout()
            step_title = QLabel(heading)
            step_title.setStyleSheet(f"color: {theme.TEXT}; font-weight: 650;")
            text_col.addWidget(step_title)
            step_body = QLabel(body)
            step_body.setWordWrap(True)
            step_body.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
            text_col.addWidget(step_body)
            h.addLayout(text_col, stretch=1)
            v.addWidget(card)

        links = QLabel(
            'Download models from <a href="https://huggingface.co/models?search=gguf">Hugging Face</a> · '
            '<a href="https://github.com/ggerganov/llama.cpp/releases">llama.cpp runtime</a>'
        )
        links.setOpenExternalLinks(True)
        links.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        v.addWidget(links)
        v.addStretch(1)

        close_btn = QPushButton("Done")
        close_btn.setObjectName("primary")
        close_btn.clicked.connect(self.accept)
        v.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)


class _InsightBarChart(QWidget):
    """A compact daily-volume chart with a readable scale and grid."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._values: list[tuple[object, int]] = []
        self.setMinimumHeight(168)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, values):
        self._values = list(values)
        self.update()

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left_axis = 42
        right_margin = 10
        top = 12
        baseline = self.height() - 30
        chart_height = max(20, baseline - top)
        plot_width = max(1, self.width() - left_axis - right_margin)
        values = self._values
        max_value = max((value for _day, value in values), default=0)

        if not values or not any(value for _day, value in values):
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.drawText(QRectF(0, 34, self.width(), 24), Qt.AlignmentFlag.AlignCenter, "No dictation in this range yet")
            return

        painter.setFont(self.font())
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        for fraction in (0.0, 0.5, 1.0):
            y = baseline - chart_height * fraction
            painter.setPen(QPen(QColor(theme.BORDER), 1, Qt.PenStyle.DotLine))
            painter.drawLine(QLineF(left_axis, y, self.width() - right_margin, y))
            painter.setPen(QColor(theme.TEXT_DIM))
            label = f"{round(max_value * fraction):,}"
            painter.drawText(QRectF(0, y - 9, left_axis - 8, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)

        step = plot_width / max(len(values), 1)
        bar_width = max(4.0, min(24.0, step * 0.62))
        for index, (day, value) in enumerate(values):
            x = left_axis + index * step + (step - bar_width) / 2
            bar_height = (value / max_value * chart_height) if max_value else 3
            color = QColor(theme.ACTIVE if value else theme.SURFACE2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(x, baseline - bar_height, bar_width, bar_height), 4, 4)

            painter.setPen(QColor(theme.TEXT_DIM))
            label = day.strftime("%a") if len(values) <= 7 else day.strftime("%d")
            painter.drawText(
                QRectF(x - 4, baseline + 7, bar_width + 8, 18),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )


class _ActivityCalendar(QWidget):
    """A local speaking heatmap with readable date numbers and hover detail."""

    _DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._details: list[tuple[object, int, int, str | None]] = []
        self._cells: list[tuple[QRectF, object, int, int, str | None]] = []
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    def set_values(self, details):
        self._details = list(details)
        self._cells = []
        self.update()

    @staticmethod
    def _fill(color: str, alpha: int) -> QColor:
        result = QColor(color)
        result.setAlpha(alpha)
        return result

    def _tooltip(self, day, words: int, sessions: int, app: str | None) -> str:
        if not words and not sessions:
            return f"{day.strftime('%A, %B %-d')}\nNo dictation"
        destination = app or "No named destination"
        return (
            f"{day.strftime('%A, %B %-d')}\n"
            f"{words:,} words · {sessions} dictation{'s' if sessions != 1 else ''}\n"
            f"Top destination: {destination}"
        )

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._cells = []
        if not self._details:
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Your speaking calendar will appear here.")
            return

        detail_map = {day: (words, sessions, app) for day, words, sessions, app in self._details}
        first = self._details[0][0]
        last = self._details[-1][0]
        start = first - timedelta(days=first.weekday())
        end = last + timedelta(days=6 - last.weekday())
        row_count = ((end - start).days + 1) // 7
        left = 38.0
        right = 8.0
        top = 24.0
        bottom = 28.0
        gap = 6.0
        cell_width = max(22.0, (self.width() - left - right - gap * 6) / 7)
        cell_height = max(22.0, min(34.0, (self.height() - top - bottom - gap * (row_count - 1)) / row_count))
        max_words = max((words for _day, words, _sessions, _app in self._details), default=0)

        painter.setFont(self.font())
        painter.setPen(QColor(theme.TEXT_DIM))
        for column, day_name in enumerate(self._DAY_NAMES):
            x = left + column * (cell_width + gap)
            painter.drawText(QRectF(x, 0, cell_width, 18), Qt.AlignmentFlag.AlignCenter, day_name)

        day = start
        while day <= end:
            row_index = (day - start).days // 7
            column = day.weekday()
            x = left + column * (cell_width + gap)
            y = top + row_index * (cell_height + gap)
            rect = QRectF(x, y, cell_width, cell_height)
            words, sessions, app = detail_map.get(day, (0, 0, None))
            self._cells.append((rect, day, words, sessions, app))
            intensity = 0 if not words or not max_words else min(1.0, words / max_words)
            fill_alpha = 24 if not words else 48 + round(190 * (intensity ** 0.62))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._fill(theme.ACTIVE, fill_alpha))
            painter.drawRoundedRect(rect, 7, 7)
            painter.setPen(QColor(theme.TEXT if words or day in detail_map else theme.TEXT_DIM))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(day.day))
            day += timedelta(days=1)

        painter.setPen(QColor(theme.TEXT_DIM))
        painter.drawText(QRectF(left, self.height() - 20, 80, 18), Qt.AlignmentFlag.AlignLeft, "Less")
        for index, alpha in enumerate((24, 86, 150, 230)):
            x = left + 36 + index * 24
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._fill(theme.ACTIVE, alpha))
            painter.drawRoundedRect(QRectF(x, self.height() - 19, 16, 16), 4, 4)
        painter.setPen(QColor(theme.TEXT_DIM))
        painter.drawText(QRectF(left + 138, self.height() - 20, 80, 18), Qt.AlignmentFlag.AlignLeft, "More")

    def mouseMoveEvent(self, event):
        point = event.position()
        for rect, day, words, sessions, app in self._cells:
            if rect.contains(point):
                QToolTip.showText(event.globalPosition().toPoint(), self._tooltip(day, words, sessions, app), self)
                return
        QToolTip.hideText()

    def leaveEvent(self, event):
        del event
        QToolTip.hideText()


class _PaceGauge(QWidget):
    """A small speedometer-style gauge with clear pacing guidance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wpm: int | None = None
        self.setMinimumHeight(112)

    def set_value(self, wpm: int | None):
        self._wpm = wpm
        self.update()

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center_x = self.width() / 2
        radius = max(34.0, min(self.width() / 2 - 34.0, 92.0))
        arc_rect = QRectF(center_x - radius, 8, radius * 2, radius * 2)
        track_pen = QPen(QColor(theme.SURFACE2), 10, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.drawArc(arc_rect, 180 * 16, -180 * 16)

        if self._wpm is not None:
            progress = max(0.0, min(float(self._wpm) / 200.0, 1.0))
            painter.setPen(QPen(QColor(theme.PROCESSING), 10, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(arc_rect, 180 * 16, round(-180 * 16 * progress))
            angle = math.pi * (1.0 - progress)
            marker_x = center_x - radius * math.cos(angle)
            marker_y = 8 + radius + radius * math.sin(angle)
            painter.setBrush(QColor(theme.TEXT))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(marker_x, marker_y), 5, 5)

        label_y = arc_rect.bottom() - 2
        painter.setPen(QColor(theme.TEXT_DIM))
        painter.drawText(QRectF(0, label_y, 86, 20), Qt.AlignmentFlag.AlignLeft, "Slow")
        painter.drawText(QRectF(center_x - 72, label_y, 144, 20), Qt.AlignmentFlag.AlignCenter, "Conversational")
        painter.drawText(QRectF(self.width() - 86, label_y, 86, 20), Qt.AlignmentFlag.AlignRight, "Fast")


class _ContextDonut(QWidget):
    """A visual distribution of the foreground apps receiving dictation."""

    _COLORS = (theme.ACTIVE, theme.DONE, theme.PROCESSING, theme.MASCOT_DARK, theme.BORDER)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._contexts: tuple[tuple[str, int], ...] = ()
        self.setFixedSize(112, 112)

    def set_values(self, contexts: tuple[tuple[str, int], ...]):
        self._contexts = contexts
        self.update()

    @classmethod
    def color_for(cls, index: int) -> str:
        return cls._COLORS[index % len(cls._COLORS)]

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(10, 10, 92, 92)
        total = sum(count for _label, count in self._contexts)
        if not total:
            painter.setPen(QPen(QColor(theme.SURFACE2), 12))
            painter.drawArc(rect, 0, 360 * 16)
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.drawText(QRectF(0, 46, self.width(), 20), Qt.AlignmentFlag.AlignCenter, "No data")
            return

        start = 90 * 16
        for index, (_label, count) in enumerate(self._contexts):
            span = max(1, round(360 * 16 * count / total))
            painter.setPen(QPen(QColor(self.color_for(index)), 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(rect, start, -span)
            start -= span
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(QRectF(0, 45, self.width(), 20), Qt.AlignmentFlag.AlignCenter, f"{total}")
        painter.setPen(QColor(theme.TEXT_DIM))
        painter.drawText(QRectF(0, 63, self.width(), 15), Qt.AlignmentFlag.AlignCenter, "sessions")


class InsightsPanel(QWidget):
    """A calm, local snapshot of how the user speaks through Chatter.

    The layout follows a simple story: what happened, how it felt, where it
    went, and whether the local pipeline helped. It deliberately avoids
    inventing an "unclassified" destination when macOS did not provide a
    named foreground app.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._metric_values: dict[str, QLabel] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title = QLabel("Insights")
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 20px; font-weight: 700;")
        title_col.addWidget(title)
        subtitle = QLabel("A private view of your local voice workflow — nothing here leaves this Mac.")
        subtitle.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        subtitle.setWordWrap(True)
        title_col.addWidget(subtitle)
        header.addLayout(title_col, stretch=1)
        self.range_combo = QComboBox()
        self.range_combo.addItem("This week", userData=7)
        self.range_combo.addItem("Last 30 days", userData=30)
        self.range_combo.addItem("All time", userData=None)
        self.range_combo.setCurrentIndex(1)
        self.range_combo.setMinimumWidth(132)
        self.range_combo.currentIndexChanged.connect(self.refresh)
        header.addWidget(self.range_combo, alignment=Qt.AlignmentFlag.AlignTop)
        refresh_button = QPushButton("Refresh")
        refresh_button.setMinimumWidth(82)
        refresh_button.clicked.connect(self.refresh)
        header.addWidget(refresh_button, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        # Keep the page fluid when the window enters macOS full screen. A
        # fixed 1200 px cap leaves the rest of the full-screen viewport
        # showing the scroll area's dark backing, which looks like a black
        # border around Chatter. The cards already provide their own spacing,
        # so letting the page fill the viewport is both safer and cleaner.
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 4)
        body.setSpacing(10)

        hero = _card()
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(16, 14, 16, 14)
        hero_copy = QVBoxLayout()
        eyebrow = QLabel("YOUR VOICE, LOCALLY")
        eyebrow.setObjectName("sectionTitle")
        eyebrow.setStyleSheet(f"color: {theme.ACTIVE}; font-size: 10px; font-weight: 700;")
        hero_copy.addWidget(eyebrow)
        self._metric_values["words"] = QLabel("0")
        self._metric_values["words"].setStyleSheet(f"color: {theme.TEXT}; font-size: 30px; font-weight: 700;")
        hero_copy.addWidget(self._metric_values["words"])
        hero_label = QLabel("words shaped by Chatter")
        hero_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        hero_copy.addWidget(hero_label)
        hero_layout.addLayout(hero_copy)
        hero_layout.addSpacing(20)
        self.hero_story = QLabel()
        self.hero_story.setWordWrap(True)
        self.hero_story.setMinimumWidth(180)
        self.hero_story.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; line-height: 1.3;")
        hero_layout.addWidget(self.hero_story, stretch=1)
        mascot = Mascot(size=54)
        mascot.set_state("idle")
        hero_layout.addWidget(mascot, alignment=Qt.AlignmentFlag.AlignVCenter)
        body.addWidget(hero)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(10)
        metrics.setVerticalSpacing(10)
        for index, (key, label, accent) in enumerate(
            (
                ("dictations", "DICTATIONS", theme.PROCESSING),
                ("today", "WORDS TODAY", theme.MASCOT_DARK),
                ("dictionary", "WORDS LEARNED", theme.ACTIVE),
            )
        ):
            card, value = self._metric_card(label, accent)
            metrics.addWidget(card, 0, index)
            self._metric_values[key] = value
        for column in range(3):
            metrics.setColumnStretch(column, 1)
        body.addLayout(metrics)

        details = QGridLayout()
        details.setHorizontalSpacing(10)
        details.setVerticalSpacing(10)

        pace_card = _card()
        pace_layout = QVBoxLayout(pace_card)
        pace_layout.setContentsMargins(14, 12, 14, 12)
        pace_header = QHBoxLayout()
        pace_title = QLabel("Speaking pace")
        pace_title.setStyleSheet(f"color: {theme.TEXT}; font-size: 16px; font-weight: 700;")
        pace_header.addWidget(pace_title)
        pace_header.addStretch(1)
        self._metric_values["pace"] = QLabel("—")
        self._metric_values["pace"].setStyleSheet(f"color: {theme.DONE}; font-size: 16px; font-weight: 700;")
        pace_header.addWidget(self._metric_values["pace"])
        pace_layout.addLayout(pace_header)
        self.pace_note = QLabel("A simple signal, not a score: where your average pace sits.")
        self.pace_note.setWordWrap(True)
        self.pace_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        pace_layout.addWidget(self.pace_note)
        self.pace_gauge = _PaceGauge()
        pace_layout.addWidget(self.pace_gauge)
        details.addWidget(pace_card, 0, 0)

        consistency_card = _card()
        consistency_layout = QVBoxLayout(consistency_card)
        consistency_layout.setContentsMargins(14, 12, 14, 12)
        consistency_title = QLabel("Your rhythm")
        consistency_title.setStyleSheet(f"color: {theme.TEXT}; font-size: 16px; font-weight: 700;")
        consistency_layout.addWidget(consistency_title)
        rhythm_note = QLabel("Small, repeatable moments matter more than a perfect streak.")
        rhythm_note.setWordWrap(True)
        rhythm_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        consistency_layout.addWidget(rhythm_note)
        self.consistency_values = {}
        for key, label in (("active", "Active days"), ("current", "Current streak"), ("longest", "Longest streak"), ("average", "Words per dictation")):
            row = QHBoxLayout()
            name = QLabel(label)
            name.setMinimumWidth(0)
            name.setStyleSheet(f"color: {theme.TEXT_DIM};")
            value = QLabel("0")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value.setStyleSheet(f"color: {theme.TEXT}; font-weight: 700;")
            row.addWidget(name, stretch=1)
            row.addWidget(value)
            consistency_layout.addLayout(row)
            self.consistency_values[key] = value
        details.addWidget(consistency_card, 0, 1)
        details.setColumnStretch(0, 1)
        details.setColumnStretch(1, 1)
        body.addLayout(details)

        self.empty_hint = _card()
        empty_layout = QHBoxLayout(self.empty_hint)
        empty_layout.setContentsMargins(14, 10, 14, 10)
        empty_text = QLabel("Your first dictation will turn this into a useful personal snapshot. Insights are derived from local history only.")
        empty_text.setWordWrap(True)
        empty_text.setStyleSheet(f"color: {theme.TEXT_DIM};")
        empty_layout.addWidget(empty_text)
        body.addWidget(self.empty_hint)

        rhythm_card = _card()
        rhythm_layout = QVBoxLayout(rhythm_card)
        rhythm_layout.setContentsMargins(14, 12, 14, 12)
        rhythm_header = QHBoxLayout()
        rhythm_title = QLabel("Speaking calendar")
        rhythm_title.setStyleSheet(f"color: {theme.TEXT}; font-size: 16px; font-weight: 700;")
        rhythm_header.addWidget(rhythm_title)
        rhythm_header.addStretch(1)
        rhythm_note = QLabel("Hover a day for details")
        rhythm_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        rhythm_header.addWidget(rhythm_note)
        rhythm_layout.addLayout(rhythm_header)
        self.daily_chart = _ActivityCalendar()
        rhythm_layout.addWidget(self.daily_chart)
        body.addWidget(rhythm_card)

        context_card = _card()
        context_layout = QVBoxLayout(context_card)
        context_layout.setContentsMargins(14, 12, 14, 12)
        context_title = QLabel("Dictations by destination")
        context_title.setStyleSheet(f"color: {theme.TEXT}; font-size: 16px; font-weight: 700;")
        context_layout.addWidget(context_title)
        context_note = QLabel("Named foreground apps only. Each slice is a local count of dictation sessions.")
        context_note.setWordWrap(True)
        context_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        context_layout.addWidget(context_note)
        context_body = QHBoxLayout()
        self.context_donut = _ContextDonut()
        context_body.addWidget(self.context_donut, alignment=Qt.AlignmentFlag.AlignTop)
        self.context_rows = QVBoxLayout()
        self.context_rows.setSpacing(8)
        context_body.addLayout(self.context_rows, stretch=1)
        context_layout.addLayout(context_body)
        body.addWidget(context_card)

        scroll.setWidget(content)
        outer.addWidget(scroll, stretch=1)
        self.refresh()

    @staticmethod
    def _metric_card(label: str, accent: str) -> tuple[QFrame, QLabel]:
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 11, 14, 11)
        eyebrow = QLabel(label)
        eyebrow.setObjectName("sectionTitle")
        eyebrow.setStyleSheet(f"color: {accent}; font-size: 10px; font-weight: 700; letter-spacing: 0.5px;")
        layout.addWidget(eyebrow)
        value = QLabel("0")
        value.setStyleSheet(f"color: {theme.TEXT}; font-size: 22px; font-weight: 700;")
        layout.addWidget(value)
        return card, value

    def _set_context_rows(self, contexts: tuple[tuple[str, int], ...]):
        while self.context_rows.count():
            item = self.context_rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not contexts:
            empty = QLabel("Named app destinations will appear after your next dictation.")
            empty.setStyleSheet(f"color: {theme.TEXT_DIM};")
            self.context_donut.set_values(())
            self.context_rows.addWidget(empty)
            return
        self.context_donut.set_values(contexts)
        for index, (label, count) in enumerate(contexts):
            row_widget = QWidget()
            row = QVBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(0)
            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)
            swatch = QFrame()
            swatch.setFixedSize(10, 10)
            swatch.setStyleSheet(
                f"background: {self.context_donut.color_for(index)}; border-radius: 5px;"
            )
            top.addWidget(swatch)
            top.addSpacing(7)
            name = QLabel(label)
            name.setMinimumWidth(0)
            name.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
            count_label = QLabel(f"{count} session{'s' if count != 1 else ''}")
            count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            count_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
            top.addWidget(name, stretch=1)
            top.addWidget(count_label)
            row.addLayout(top)
            self.context_rows.addWidget(row_widget)

    def refresh(self):
        days = self.range_combo.currentData()
        summary = insights.summarize(
            history.load(kind="dictation"),
            dictionary_entries=len(config.load().get("custom_dictionary", {})),
            days=days,
        )
        self._metric_values["words"].setText(f"{summary.total_words:,}")
        self._metric_values["dictations"].setText(f"{summary.dictations:,}")
        self._metric_values["pace"].setText(f"{summary.average_wpm:,} WPM" if summary.average_wpm else "—")
        self._metric_values["today"].setText(f"{summary.words_today:,}")
        self._metric_values["dictionary"].setText(str(summary.dictionary_entries))
        self.pace_gauge.set_value(summary.average_wpm)
        self.pace_note.setText(
            f"Based on {summary.pace_sessions} sample{'s' if summary.pace_sessions != 1 else ''} with audio duration."
            if summary.pace_sessions else "Speak for a few seconds to get a pace sample."
        )
        self.empty_hint.setVisible(summary.dictations == 0)
        self.daily_chart.set_values(summary.daily_details)
        self._set_context_rows(summary.contexts)

        apps = [label for label, _count in summary.contexts]
        if not summary.dictations:
            self.hero_story.setText("Your first local dictation will create a private snapshot of your rhythm, pace, and writing destinations.")
        elif apps:
            app_text = ", ".join(apps[:3])
            self.hero_story.setText(f"You have shaped {summary.dictations:,} dictation{'s' if summary.dictations != 1 else ''} across {app_text}. Your local history is becoming a map of how you work.")
        else:
            self.hero_story.setText(f"You have shaped {summary.dictations:,} dictation{'s' if summary.dictations != 1 else ''}. Chatter is keeping the story local while you build your rhythm.")

        self.consistency_values["active"].setText(str(summary.active_days))
        self.consistency_values["current"].setText(f"{summary.current_streak} day{'s' if summary.current_streak != 1 else ''}")
        self.consistency_values["longest"].setText(f"{summary.longest_streak} day{'s' if summary.longest_streak != 1 else ''}")
        self.consistency_values["average"].setText(f"{summary.average_words:,}")



class LiveDictationTextEdit(QTextEdit):
    """Text editor with the macOS whole-current-line delete shortcut."""

    # PyQt6 on macOS swaps Ctrl and Meta by default (so cross-platform
    # "Ctrl+X" code reads as Cmd+X here) — a physical Command press shows up
    # in event.modifiers() as Qt.KeyboardModifier.ControlModifier, not
    # MetaModifier. Checked live with a raw QKeyEvent probe: Cmd+Backspace
    # arrives as modifiers()=ControlModifier, nativeModifiers() bit 1<<20
    # set (AppKit's real, unswapped NSEventModifierFlagCommand). Reading the
    # native flags directly sidesteps the swap entirely instead of chasing
    # which Qt enum value means what on which build. Bits 17-20 are AppKit's
    # device-independent Shift/Control/Option/Command flags; masking to just
    # those four and requiring exactly Command ignores the low-order
    # device-dependent bits (which physical key, numpad, etc.) that vary
    # per keypress and aren't meaningful here.
    _NATIVE_SHIFT = 1 << 17
    _NATIVE_CONTROL = 1 << 18
    _NATIVE_OPTION = 1 << 19
    _NATIVE_COMMAND = 1 << 20
    _NATIVE_MODIFIER_MASK = _NATIVE_SHIFT | _NATIVE_CONTROL | _NATIVE_OPTION | _NATIVE_COMMAND

    def clear_current_line(self):
        cursor = self.textCursor()
        block = cursor.block()
        start, end = current_line_bounds(block.position(), block.length())
        cursor.beginEditBlock()
        cursor.setPosition(start, QTextCursor.MoveMode.MoveAnchor)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.endEditBlock()
        self.setTextCursor(cursor)

    @staticmethod
    def _is_line_clear_event(event: QKeyEvent) -> bool:
        if event.key() not in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            return False
        native_modifiers = int(event.nativeModifiers() or 0) & LiveDictationTextEdit._NATIVE_MODIFIER_MASK
        return native_modifiers == LiveDictationTextEdit._NATIVE_COMMAND

    def event(self, event):
        # Qt's shortcut resolver sends ShortcutOverride before keyPressEvent.
        # Claim the two macOS line-delete combinations here so QTextEdit or a
        # parent shortcut cannot consume them first.
        if (
            event.type() == QEvent.Type.ShortcutOverride
            and isinstance(event, QKeyEvent)
            and self._is_line_clear_event(event)
        ):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent):
        if self._is_line_clear_event(event):
            self.clear_current_line()
            event.accept()
            return
        super().keyPressEvent(event)


class MainWindow(QMainWindow):
    hotkey_changed = pyqtSignal()  # push-to-talk key was changed; listener needs a restart
    setup_requested = pyqtSignal()
    menu_bar_icon_visibility_changed = pyqtSignal(bool)
    update_check_requested = pyqtSignal(bool)

    def __init__(self, formatter):
        super().__init__()
        self.setWindowTitle("Chatter")
        self.resize(WINDOW_W, WINDOW_H)
        self.setMinimumSize(MIN_WINDOW_W, MIN_WINDOW_H)
        self.setAcceptDrops(True)
        _match_titlebar_to_theme(self)

        self.formatter = formatter
        self._advanced_mtp_checkbox = None
        self.media_path: str | None = None
        self.file_model_path: str | None = None
        self.worker: TranscribeWorker | None = None
        self._latest_live_preview = ""
        self._live_idle_timer = QTimer(self)
        self._live_idle_timer.setSingleShot(True)
        self._live_idle_timer.timeout.connect(lambda: self.set_live_state("idle"))
        self._has_shown = False
        self._tab_fade_animation = None

        self.tabs = QTabWidget()
        self.tabs.setTabBar(SquiggleTabBar())
        self.tabs.addTab(self._build_live_tab(), "Live Dictation")
        self.tabs.addTab(self._build_files_tab(), "Transcribed Files")
        self.tabs.addTab(self._build_dictionary_tab(), "Dictionary")
        self.tabs.addTab(self._build_history_tab(), "History")
        self.insights_tab = InsightsPanel()
        self.tabs.addTab(self.insights_tab, "Insights")
        self.settings_tab = self._build_settings_tab()
        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.currentChanged.connect(self._animate_tab_content)
        self.setCentralWidget(self.tabs)
        # The first underline can be painted before QTabBar has its final
        # geometry. Synchronize once after the window is laid out so Live
        # Dictation looks complete on first launch, not only after a tab swap.
        QTimer.singleShot(80, self.tabs.tabBar()._sync_to_current)

        self.refresh_models()
        self._reload_files_history()
        self._reload_dictation_history()

    def showEvent(self, event):
        super().showEvent(event)
        if self._has_shown:
            return
        self._has_shown = True
        self.setWindowOpacity(0.0)
        self._window_fade_animation = QPropertyAnimation(self, b"windowOpacity", self)
        self._window_fade_animation.setDuration(180)
        self._window_fade_animation.setStartValue(0.0)
        self._window_fade_animation.setEndValue(1.0)
        self._window_fade_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._window_fade_animation.start()

    def _animate_tab_content(self, index: int):
        if index < 0:
            return
        widget = self.tabs.widget(index)
        if widget is None:
            return
        effect = widget.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
        effect.setOpacity(0.72)
        self._tab_fade_animation = QPropertyAnimation(effect, b"opacity", self)
        self._tab_fade_animation.setDuration(150)
        self._tab_fade_animation.setStartValue(0.72)
        self._tab_fade_animation.setEndValue(1.0)
        self._tab_fade_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._tab_fade_animation.start()

    def eventFilter(self, watched, event):
        # Keep the macOS whole-line delete shortcut at the window boundary as
        # well as inside QTextEdit. On some Qt/macOS combinations the native
        # Command key is resolved by the parent shortcut dispatcher before
        # QTextEdit receives keyPressEvent; handling it here prevents that
        # platform-specific path from silently falling through to Backspace.
        if (
            watched is getattr(self, "practice_box", None)
            and isinstance(event, QKeyEvent)
            and event.type() in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress)
            and LiveDictationTextEdit._is_line_clear_event(event)
        ):
            event.accept()
            if event.type() == QEvent.Type.KeyPress:
                self.practice_box.clear_current_line()
            return True
        return super().eventFilter(watched, event)

    def _reload_insights(self):
        if hasattr(self, "insights_tab"):
            self.insights_tab.refresh()

    # --- Live Dictation tab ---------------------------------------------

    def _build_live_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(20, 16, 20, 20)
        v.setSpacing(10)

        mascot_row = QHBoxLayout()
        mascot_row.addStretch(1)
        self.live_mascot = Mascot(size=64)
        self.live_mascot.set_state("idle")
        mascot_row.addWidget(self.live_mascot)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self.live_title = QLabel("Ready when you are")
        self.live_title.setStyleSheet(f"font-size: 17px; font-weight: 700; color: {theme.TEXT};")
        title_col.addWidget(self.live_title)
        self.live_hotkey_pill = QLabel()
        self.live_hotkey_pill.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        title_col.addWidget(self.live_hotkey_pill)
        mascot_row.addLayout(title_col)
        mascot_row.addStretch(1)
        v.addLayout(mascot_row)
        self._update_live_hotkey_pill()

        self.setup_card = _card()
        setup_row = QHBoxLayout(self.setup_card)
        setup_row.setContentsMargins(12, 9, 12, 9)
        setup_copy = QVBoxLayout()
        self.setup_title = QLabel("Push-to-talk needs one quick setup")
        self.setup_title.setStyleSheet(f"color: {theme.TEXT}; font-weight: 700;")
        setup_copy.addWidget(self.setup_title)
        self.setup_message = QLabel()
        self.setup_message.setWordWrap(True)
        self.setup_message.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        setup_copy.addWidget(self.setup_message)
        setup_row.addLayout(setup_copy, stretch=1)
        setup_button = QPushButton("Finish setup")
        setup_button.setObjectName("primary")
        setup_button.clicked.connect(self.setup_requested.emit)
        setup_row.addWidget(setup_button, alignment=Qt.AlignmentFlag.AlignVCenter)
        v.addWidget(self.setup_card)
        self._refresh_setup_banner()

        # A real place to try dictation on the app itself, first-run or
        # any time — click in, hold the hotkey, and watch the words land
        # right here. paste_action.py types via simulated keystrokes at
        # whatever's focused system-wide, so this needs no special-casing:
        # it works exactly like any other text field would.
        practice_row = QHBoxLayout()
        practice_label = QLabel("TRY IT")
        practice_label.setObjectName("sectionTitle")
        practice_row.addWidget(practice_label)
        practice_row.addStretch(1)
        clear_practice_btn = QPushButton("Clear")
        clear_practice_btn.clicked.connect(lambda: self.practice_box.clear())
        practice_row.addWidget(clear_practice_btn)
        v.addLayout(practice_row)

        self.practice_box = LiveDictationTextEdit()
        self.practice_box.installEventFilter(self)
        self.practice_box.setPlaceholderText(
            "Click here, then hold your push-to-talk key and start talking — "
            "your words will show up right in this box."
        )
        self.practice_box.setMinimumHeight(140)
        v.addWidget(self.practice_box, stretch=1)

        v.addStretch(1)
        return w

    def _update_live_hotkey_pill(self):
        keycode = config.load().get("hotkey_keycode", 60)
        label = next((l for kc, l, _w in SUPPORTED_HOTKEYS if kc == keycode), "your hotkey")
        mode = normalize_activation_mode(config.load().get("hotkey_activation_mode", HOLD_TO_TALK))
        if mode == DOUBLE_TAP_PERSISTENT:
            self.live_hotkey_pill.setText(f"Double-tap {label} to start/stop hands-free")
        else:
            self.live_hotkey_pill.setText(f"Hold {label} to talk")

    def _refresh_setup_banner(self):
        missing = []
        if not permissions.is_microphone_authorized():
            missing.append("Microphone")
        if not permissions.input_monitoring_available():
            missing.append("Input Monitoring")
        if not permissions.is_trusted():
            missing.append("Accessibility")
        if missing:
            self.setup_message.setText(
                f"Chatter is ready, but the global shortcut is paused until macOS grants: {', '.join(missing)}. "
                "Your audio and transcripts remain on this Mac."
            )
            self.setup_card.show()
        else:
            self.setup_card.hide()

    def set_live_state(self, state: str, label: str | None = None):
        """Mirrors the HUD's state on the Live Dictation tab's mascot, so
        the main window (if open) shows the same listening/processing/done
        story, not just a status string. Both the HUD and this tab use the
        literal state text; the mascot provides the character while the
        transcript preview provides the immediate feedback."""
        self._live_idle_timer.stop()
        self.live_mascot.set_state(state)
        if state == "idle":
            self.live_title.setText("Ready when you are")
            self.live_hotkey_pill.show()
        else:
            self.live_title.setText(label or state.capitalize())
            self.live_hotkey_pill.hide()
            if state in ("done", "error"):
                self._live_idle_timer.start(2500)

    def set_live_preview(self, text: str):
        """Retain the latest streaming draft for diagnostics/history.

        The HUD is the unobtrusive live surface; the practice box is reserved
        for the committed result so tentative text cannot be pasted twice or
        overwrite a user's existing draft.
        """
        self._latest_live_preview = text or ""

    # --- Files tab -------------------------------------------------------

    def _build_files_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(10)

        heading = QLabel("Transcribed files")
        heading.setStyleSheet(f"color: {theme.TEXT}; font-size: 20px; font-weight: 700;")
        v.addWidget(heading)
        subtitle = QLabel(
            "Choose an audio or video file. Chatter uses your configured local model automatically, "
            "and keeps recent file work here."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        v.addWidget(subtitle)

        timing_row = QHBoxLayout()
        timing_label = QLabel("Subtitle timing")
        timing_label.setMinimumWidth(115)
        timing_row.addWidget(timing_label)
        self.file_timestamp_combo = QComboBox()
        self.file_timestamp_combo.addItem("Word level (recommended)", userData="word")
        self.file_timestamp_combo.addItem("Phrase level (most compatible)", userData="segment")
        timestamp_mode = config.load().get("file_timestamp_mode", "word")
        timing_index = self.file_timestamp_combo.findData(timestamp_mode)
        if timing_index < 0:
            timing_index = 0
        self.file_timestamp_combo.setCurrentIndex(timing_index)
        self.file_timestamp_combo.currentIndexChanged.connect(self._on_file_timestamp_mode_changed)
        timing_row.addWidget(self.file_timestamp_combo, stretch=1)
        v.addLayout(timing_row)
        self.file_timestamp_note = QLabel()
        self.file_timestamp_note.setWordWrap(True)
        self.file_timestamp_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        v.addWidget(self.file_timestamp_note)

        self.drop_zone = QLabel("Drop files, or click to browse")
        self.drop_zone.setObjectName("dropZone")
        self.drop_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_zone.setFixedHeight(64)
        v.addWidget(self.drop_zone)

        row = QHBoxLayout()
        self.open_btn = QPushButton("Open audio / video…")
        self.open_btn.clicked.connect(self.open_file)
        self.file_label = QLabel("No file selected")
        self.file_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.transcribe_btn = QPushButton("Transcribe")
        self.transcribe_btn.setObjectName("primary")
        self.transcribe_btn.clicked.connect(self.start_transcription)
        self.transcribe_btn.setEnabled(False)
        row.addWidget(self.open_btn)
        row.addWidget(self.file_label, stretch=1)
        row.addWidget(self.transcribe_btn)
        v.addLayout(row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        v.addWidget(self.progress_bar)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        v.addWidget(self.status_label)

        self.files_list_container = QWidget()
        self.files_list_layout = QVBoxLayout(self.files_list_container)
        self.files_list_layout.setContentsMargins(0, 0, 0, 0)
        self.files_list_layout.setSpacing(7)
        self.files_list_layout.addStretch(1)
        v.addWidget(_scroll_list(self.files_list_container, 130), stretch=1)

        return w

    def _reload_files_history(self):
        while self.files_list_layout.count() > 1:
            item = self.files_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        entries = history.load(kind="file", limit=50)
        for entry in entries:
            self.files_list_layout.insertWidget(self.files_list_layout.count() - 1, self._file_row(entry))

    def _file_row(self, entry: dict) -> QFrame:
        row = _card()
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 7, 10, 7)
        name_col = QVBoxLayout()
        name_col.setContentsMargins(0, 0, 0, 0)
        name_col.setSpacing(2)
        name = QLabel(entry.get("filename", "recording"))
        name.setStyleSheet(f"color: {theme.TEXT}; font-weight: 500;")
        name_col.addWidget(name)
        model_name = str(entry.get("model_name", "")).strip()
        timing_kind = str(entry.get("actual_timestamp_mode", "")).strip().lower()
        if model_name:
            timing_label = {
                "word": "word timing",
                "phrase": "phrase timing",
                "none": "no timing",
            }.get(timing_kind, "timing recorded")
            model_note = QLabel(f"{Path(model_name).stem} · {timing_label}")
            model_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 10px;")
            model_note.setToolTip(
                "The local model used for this file transcription: "
                f"{model_name}. Requested: {entry.get('requested_timestamp_mode', 'automatic')}."
            )
            name_col.addWidget(model_note)
        h.addLayout(name_col, stretch=1)
        done_pill = _pill("Done", theme.DONE, theme.rgba_str(theme.done_dim()))
        h.addWidget(done_pill)
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(entry.get("text", "")))
        h.addWidget(copy_btn)
        export_menu_button = QPushButton("Export…")
        export_menu = QMenu(export_menu_button)
        export_menu.addAction("Plain text (.txt)", lambda: self._export_history_entry(entry, "txt"))
        word_rows = self._word_timestamps(entry)
        word_srt = export_menu.addAction(
            "Word-level subtitles (.srt)",
            lambda: self._export_history_entry(entry, "word_srt"),
        )
        word_srt.setEnabled(bool(word_rows))
        word_srt.setToolTip("Available when the selected file model returns word timestamps")
        word_vtt = export_menu.addAction(
            "Word-level subtitles (.vtt)",
            lambda: self._export_history_entry(entry, "word_vtt"),
        )
        word_vtt.setEnabled(bool(word_rows))
        word_vtt.setToolTip("Available when the selected file model returns word timestamps")
        phrase_rows = self._timed_rows(entry, "segments")
        if phrase_rows:
            export_menu.addSeparator()
            export_menu.addAction(
                "Phrase-level subtitles (.srt)",
                lambda: self._export_history_entry(entry, "phrase_srt"),
            )
            export_menu.addAction(
                "Phrase-level subtitles (.vtt)",
                lambda: self._export_history_entry(entry, "phrase_vtt"),
            )
        export_menu_button.setMenu(export_menu)
        h.addWidget(export_menu_button)
        return row

    @staticmethod
    def _word_value(word, name: str):
        return word.get(name) if isinstance(word, dict) else getattr(word, name, None)

    @classmethod
    def _timed_rows(cls, entry: dict, kind: str) -> list[dict]:
        rows = []
        for row in entry.get(kind) or []:
            text = cls._word_value(row, "text")
            start = cls._word_value(row, "t0_ms")
            end = cls._word_value(row, "t1_ms")
            if text is None or start is None or end is None:
                continue
            try:
                rows.append({"text": str(text), "t0_ms": float(start), "t1_ms": float(end)})
            except (TypeError, ValueError):
                continue
        return rows

    @classmethod
    def _has_word_timestamps(cls, entry: dict) -> bool:
        return bool(cls._word_timestamps(entry))

    @classmethod
    def _word_timestamps(cls, entry: dict) -> list[dict]:
        """Return true word rows, grouping token rows when needed.

        Some transcribe.cpp models expose token timing but do not materialize
        ``Result.words``. Grouping by the native ``word_index`` preserves the
        model's timing instead of pretending that a phrase timestamp is a word
        timestamp. Models that expose only segments correctly return no rows.
        """
        word_rows = cls._timed_rows(entry, "words")
        if word_rows:
            return word_rows
        grouped: dict[int, dict] = {}
        order: list[int] = []
        for token in entry.get("tokens") or []:
            text = cls._word_value(token, "text")
            start = cls._word_value(token, "t0_ms")
            end = cls._word_value(token, "t1_ms")
            word_index = cls._word_value(token, "word_index")
            if text is None or start is None or end is None or word_index is None:
                continue
            try:
                word_index = int(word_index)
                if word_index < 0:
                    continue
                start = float(start)
                end = float(end)
            except (TypeError, ValueError):
                continue
            if word_index not in grouped:
                grouped[word_index] = {"text": "", "t0_ms": start, "t1_ms": end}
                order.append(word_index)
            row = grouped[word_index]
            row["text"] += str(text)
            row["t0_ms"] = min(row["t0_ms"], start)
            row["t1_ms"] = max(row["t1_ms"], end)
        return [row for index in order if (row := grouped[index]).get("text", "").strip()]

    @classmethod
    def _has_timestamps(cls, entry: dict) -> bool:
        return bool(cls._word_timestamps(entry) or cls._timed_rows(entry, "segments"))

    @classmethod
    def _words_to_vtt(cls, words) -> str:
        lines = ["WEBVTT", ""]
        for line in words_to_srt(words).splitlines():
            # SRT uses a comma for milliseconds; WebVTT uses a period. Only
            # timestamp lines are changed so transcript punctuation survives.
            lines.append(line.replace(",", ".", 2) if "-->" in line else line)
        return "\n".join(lines) + "\n"

    @classmethod
    def _segments_to_vtt(cls, segments) -> str:
        lines = ["WEBVTT", ""]
        for line in segments_to_srt(segments).splitlines():
            lines.append(line.replace(",", ".", 2) if "-->" in line else line)
        return "\n".join(lines) + "\n"

    def _export_history_entry(self, entry: dict, kind: str):
        if kind not in {"txt", "word_srt", "word_vtt", "phrase_srt", "phrase_vtt"}:
            return
        word_rows = self._word_timestamps(entry)
        segment_rows = self._timed_rows(entry, "segments")
        if kind in {"word_srt", "word_vtt"} and not word_rows:
            QMessageBox.information(
                self,
                "Word timestamps unavailable",
                "The selected file model returned phrase timestamps only. Choose a word-timestamp-capable model in Advanced settings to export word-level subtitles.",
            )
            return
        if kind in {"phrase_srt", "phrase_vtt"} and not segment_rows:
            QMessageBox.information(
                self,
                "Timestamps unavailable",
                "This transcription did not include timestamps, so a timestamped export is not available.",
            )
            return
        extension = ".vtt" if kind.endswith("vtt") else ".srt" if kind.endswith("srt") else ".txt"
        default_name = Path(entry.get("filename", "transcript")).stem + extension
        filters = {
            "txt": "Plain text files (*.txt)",
            "word_srt": "SubRip subtitles (*.srt)",
            "word_vtt": "WebVTT subtitles (*.vtt)",
            "phrase_srt": "SubRip subtitles (*.srt)",
            "phrase_vtt": "WebVTT subtitles (*.vtt)",
        }
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export {kind.upper()} transcript", default_name, filters[kind]
        )
        if not path:
            return
        try:
            if kind == "txt":
                content = str(entry.get("text", ""))
            elif kind == "word_srt":
                content = words_to_srt(word_rows)
            elif kind == "phrase_srt":
                content = segments_to_srt(segment_rows)
            elif kind == "word_vtt":
                content = self._words_to_vtt(word_rows)
            else:
                content = self._segments_to_vtt(segment_rows)
            Path(path).write_text(content, encoding="utf-8")
            logger.info("exported .%s to %s", kind, path)
        except Exception:
            logger.exception("history export failed")
            QMessageBox.critical(self, "Export failed", traceback.format_exc())

    # --- drag & drop ---------------------------------------------------

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            path = Path(event.mimeData().urls()[0].toLocalFile())
            if path.suffix.lower() in MEDIA_EXTENSIONS:
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event):
        path = Path(event.mimeData().urls()[0].toLocalFile())
        self._set_media_path(str(path))
        event.acceptProposedAction()

    # --- Models tab --------------------------------------------------------

    def _build_models_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(12)

        heading_row = QHBoxLayout()
        heading_col = QVBoxLayout()
        heading = QLabel("Local models")
        heading.setStyleSheet(f"color: {theme.TEXT}; font-size: 18px; font-weight: 700;")
        heading_col.addWidget(heading)
        subtitle = QLabel("One model for live dictation. One optional model for polish.")
        subtitle.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        heading_col.addWidget(subtitle)
        heading_row.addLayout(heading_col, stretch=1)
        guide_btn = QPushButton("How to choose")
        guide_btn.clicked.connect(lambda: _ModelGuideDialog(self).exec())
        heading_row.addWidget(guide_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        v.addLayout(heading_row)

        sub_tabs = QTabWidget()
        sub_tabs.setTabBar(SquiggleTabBar())

        self.transcription_panel = _ModelSlotPanel(
            description="Batch model used for file transcription. Push-to-talk uses the Live preview model only, then optionally formats it locally.",
            dest_dir=MODELS_DIR,
            config_key="whisper_model_path",
            browse_url="https://huggingface.co/models?search=transcribe.cpp",
            browse_label="browse transcribe.cpp models",
            on_change=self.refresh_models,
            fallback_paths=(
                MODELS_DIR / "parakeet-tdt-0.6b-v2-Q8_0.gguf",
                MODELS_DIR / "whisper-large-v3-turbo-Q8_0.gguf",
            ),
        )
        sub_tabs.addTab(self.transcription_panel, "File transcription")

        self.streaming_panel = _ModelSlotPanel(
            description="The single local ASR model used for push-to-talk: it shows live text and finalizes the same transcript on release.",
            dest_dir=MODELS_DIR,
            config_key="streaming_model_path",
            browse_url="https://huggingface.co/models?search=streaming+asr+gguf",
            browse_label="browse streaming ASR models",
            requires_streaming=True,
            fallback_paths=(
                MODELS_DIR / "nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf",
                MODELS_DIR / "nemotron-speech-streaming-en-0.6b-Q8_0.gguf",
                MODELS_DIR / "moonshine-streaming-tiny-Q8_0.gguf",
            ),
        )
        sub_tabs.addTab(self.streaming_panel, "Live dictation")

        self.cleanup_panel = _ModelSlotPanel(
            description='A small instruction-tuned chat model for the "Clean up with AI" pass.',
            dest_dir=CLEANUP_MODELS_DIR,
            config_key="llama_model_path",
            browse_url="https://huggingface.co/models?search=instruct+gguf&sort=downloads",
            browse_label="browse instruct GGUF chat models",
            runtime_widget=self._build_llama_runtime_card(),
        )
        sub_tabs.addTab(self.cleanup_panel, "AI cleanup")

        v.addWidget(sub_tabs)
        return w

    def _build_llama_runtime_card(self) -> QFrame:
        """Text cleanup also needs the llama-server binary itself (a
        separate native process this app doesn't bundle) — shown above the
        model list since a downloaded model alone doesn't do anything
        without it."""
        card = _card()
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        title = QLabel("llama-server runtime")
        title.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
        v.addWidget(title)
        row = QHBoxLayout()
        self.llama_runtime_status = QLabel("")
        self.llama_runtime_status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        self.llama_runtime_status.setWordWrap(True)
        row.addWidget(self.llama_runtime_status, stretch=1)
        self.llama_runtime_btn = QPushButton("Download")
        self.llama_runtime_btn.clicked.connect(self._start_llama_runtime_download)
        row.addWidget(self.llama_runtime_btn)
        v.addLayout(row)
        self._refresh_llama_runtime_status()
        return card

    def _refresh_llama_runtime_status(self):
        found = llama_runtime.find_installed()
        try:
            if found:
                self.llama_runtime_status.setText(f"Found: {found}")
                self.llama_runtime_btn.setText("Re-download")
            else:
                self.llama_runtime_status.setText(
                    "Not found — needed to run any text-cleanup model. Downloads the official "
                    "prebuilt binary from llama.cpp's GitHub releases (macOS only)."
                )
                self.llama_runtime_btn.setText("Download")
            self.llama_runtime_btn.setEnabled(True)
        except RuntimeError:
            # The advanced dialog can be closed while a background download is
            # still finishing. Its widgets may already be gone; the download
            # itself is safe to complete and the next open will re-read state.
            return

    def _start_llama_runtime_download(self):
        self.llama_runtime_btn.setEnabled(False)
        self.llama_runtime_btn.setText("Starting…")
        worker = llama_runtime.LlamaServerDownloadWorker()

        def on_progress(done: int, total: int):
            try:
                if total:
                    self.llama_runtime_btn.setText(f"{done * 100 // total}%")
                else:
                    self.llama_runtime_btn.setText(f"{done // (1024 * 1024)} MB")
            except RuntimeError:
                return

        def on_done(_path: str):
            self._refresh_llama_runtime_status()

        def on_failed(msg: str):
            try:
                self.llama_runtime_btn.setEnabled(True)
                self.llama_runtime_btn.setText("Retry")
            except RuntimeError:
                pass
            logger.warning("llama-server download failed: %s", msg)

        worker.progress.connect(on_progress)
        worker.finished_ok.connect(on_done)
        worker.failed.connect(on_failed)
        self._llama_runtime_worker = worker  # keep a reference alive
        worker.start()

    # --- Dictionary tab --------------------------------------------------

    def _build_dictionary_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(10)

        heading = QLabel("Your growing vocabulary")
        heading.setStyleSheet(f"color: {theme.TEXT}; font-size: 20px; font-weight: 700;")
        v.addWidget(heading)
        explanation = QLabel(
            "Teach Chatter names, jargon, and phrases that matter to you. "
            "Corrections stay on this Mac and are applied before the optional cleanup pass."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        v.addWidget(explanation)

        self.dict_hint = QLabel()
        self.dict_hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        self.dict_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.dict_hint)

        self.dict_table = QTableWidget(0, 2)
        self.dict_table.setHorizontalHeaderLabels(["Sounds like", "Correct to"])
        self.dict_table.verticalHeader().setVisible(False)
        self.dict_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.dict_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.dict_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.dict_table.setMinimumHeight(180)
        self._reload_dictionary_table()
        v.addWidget(self.dict_table, stretch=1)

        add_row = QHBoxLayout()
        self.dict_wrong_input = QLineEdit()
        self.dict_wrong_input.setPlaceholderText("Sounds like…")
        self.dict_right_input = QLineEdit()
        self.dict_right_input.setPlaceholderText("Correct to…")
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_dictionary_entry)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove_dictionary_entry)
        add_row.addWidget(self.dict_wrong_input, stretch=1)
        add_row.addWidget(self.dict_right_input, stretch=1)
        add_row.addWidget(add_btn)
        add_row.addWidget(remove_btn)
        v.addLayout(add_row)
        v.addStretch(1)
        return w

    def _reload_dictionary_table(self):
        entries = config.load().get("custom_dictionary", {})
        self.dict_hint.setText(
            f"{len(entries)} correction{'s' if len(entries) != 1 else ''} remembered so far · "
            "edits after a paste can teach Chatter automatically."
        )
        self.dict_table.setRowCount(0)
        for wrong, right in entries.items():
            row = self.dict_table.rowCount()
            self.dict_table.insertRow(row)
            self.dict_table.setItem(row, 0, QTableWidgetItem(wrong))
            self.dict_table.setItem(row, 1, QTableWidgetItem(right))

    def _add_dictionary_entry(self):
        wrong = self.dict_wrong_input.text().strip()
        right = self.dict_right_input.text().strip()
        if not wrong or not right:
            return
        corrections = dict(config.load().get("custom_dictionary", {}))
        corrections[wrong] = right
        config.update(custom_dictionary=corrections)
        self.dict_wrong_input.clear()
        self.dict_right_input.clear()
        self._reload_dictionary_table()

    def _remove_dictionary_entry(self):
        rows = sorted({idx.row() for idx in self.dict_table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        corrections = dict(config.load().get("custom_dictionary", {}))
        for row in rows:
            item = self.dict_table.item(row, 0)
            if item:
                corrections.pop(item.text(), None)
        config.update(custom_dictionary=corrections)
        self._reload_dictionary_table()

    # --- History tab (push-to-talk dictation log) -------------------------

    def _build_history_tab(self) -> QWidget:
        content = QWidget()
        # This page must expand with the window; otherwise full-screen mode
        # shows a centered 1200 px island with a dark surround.
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        v = QVBoxLayout(content)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(10)

        heading_row = QHBoxLayout()
        heading_col = QVBoxLayout()
        heading = QLabel("History")
        heading.setStyleSheet(f"color: {theme.TEXT}; font-size: 20px; font-weight: 700;")
        heading_col.addWidget(heading)
        subtitle = QLabel("A private timeline of what Chatter heard and delivered.")
        subtitle.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        heading_col.addWidget(subtitle)
        heading_row.addLayout(heading_col, stretch=1)
        self.history_count_label = QLabel("")
        self.history_count_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        heading_row.addWidget(self.history_count_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.clear_history_btn = QPushButton("Clear all…")
        self.clear_history_btn.setObjectName("secondary")
        self.clear_history_btn.clicked.connect(self._clear_dictation_history)
        heading_row.addWidget(self.clear_history_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        v.addLayout(heading_row)

        search_card = _card()
        search_layout = QVBoxLayout(search_card)
        search_layout.setContentsMargins(10, 8, 10, 8)
        search_header = QHBoxLayout()
        search_label = QLabel("SEARCH YOUR DICTATION")
        search_label.setObjectName("sectionTitle")
        search_header.addWidget(search_label)
        search_header.addStretch(1)
        self.history_filter_toggle = QPushButton("☷")
        self.history_filter_toggle.setObjectName("secondary")
        self.history_filter_toggle.setCheckable(True)
        self.history_filter_toggle.setFixedSize(34, 28)
        self.history_filter_toggle.setToolTip("Show filters")
        self.history_filter_toggle.setAccessibleName("Show history filters")
        search_header.addWidget(self.history_filter_toggle)
        search_layout.addLayout(search_header)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("Search words, apps, or phrases…")
        self.history_search.textChanged.connect(self._reload_dictation_history)
        search_layout.addWidget(self.history_search)
        self.history_filter_panel = QWidget()
        filters = QHBoxLayout()
        self.history_filter_panel.setLayout(filters)
        self.history_filter_panel.setVisible(False)
        filters.setSpacing(8)
        app_label = QLabel("App")
        app_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        filters.addWidget(app_label)
        self.history_app_filter = QComboBox()
        self.history_app_filter.addItem("All apps", userData="")
        self.history_app_filter.setMinimumWidth(150)
        self.history_app_filter.currentIndexChanged.connect(self._reload_dictation_history)
        filters.addWidget(self.history_app_filter)
        date_label = QLabel("Date")
        date_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        filters.addWidget(date_label)
        self.history_date_filter = QComboBox()
        self.history_date_filter.addItem("All time", userData=None)
        self.history_date_filter.addItem("Last 30 days", userData=30)
        self.history_date_filter.addItem("Last 7 days", userData=7)
        self.history_date_filter.currentIndexChanged.connect(self._reload_dictation_history)
        filters.addWidget(self.history_date_filter)
        filters.addStretch(1)
        search_layout.addWidget(self.history_filter_panel)
        self.history_filter_toggle.toggled.connect(self.history_filter_panel.setVisible)
        v.addWidget(search_card)

        self.dictation_list_container = QWidget()
        self.dictation_list_layout = QVBoxLayout(self.dictation_list_container)
        self.dictation_list_layout.setContentsMargins(0, 0, 0, 0)
        self.dictation_list_layout.setSpacing(7)
        self.dictation_list_layout.addStretch(1)
        v.addWidget(_scroll_list(self.dictation_list_container, min_height=180), stretch=1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _reload_dictation_history(self):
        while self.dictation_list_layout.count() > 1:
            item = self.dictation_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        import datetime

        query = self.history_search.text().strip().lower() if hasattr(self, "history_search") else ""
        all_entries = history.load(kind="dictation")
        selected_app = self.history_app_filter.currentData() if hasattr(self, "history_app_filter") else ""
        selected_days = self.history_date_filter.currentData() if hasattr(self, "history_date_filter") else None
        cutoff = datetime.datetime.now().timestamp() - selected_days * 86400 if selected_days else None
        if query:
            entries = [
                entry for entry in all_entries
                if query in entry.get("text", "").lower()
                or query in str(entry.get("context_app", "")).lower()
            ]
        else:
            entries = list(all_entries)
        if selected_app:
            entries = [entry for entry in entries if str(entry.get("context_app", "")).strip() == selected_app]
        if cutoff is not None:
            entries = [entry for entry in entries if float(entry.get("ts", 0) or 0) >= cutoff]
        if hasattr(self, "history_app_filter"):
            current_app = selected_app
            apps = sorted({str(entry.get("context_app", "")).strip() for entry in all_entries if str(entry.get("context_app", "")).strip()}, key=str.casefold)
            self.history_app_filter.blockSignals(True)
            self.history_app_filter.clear()
            self.history_app_filter.addItem("All apps", userData="")
            for app_name in apps:
                self.history_app_filter.addItem(app_name, userData=app_name)
            restored = self.history_app_filter.findData(current_app)
            self.history_app_filter.setCurrentIndex(restored if restored >= 0 else 0)
            self.history_app_filter.blockSignals(False)
        visible_entries = entries[:100]
        if hasattr(self, "history_count_label"):
            suffix = " · showing latest 100" if len(entries) > 100 else ""
            self.history_count_label.setText(f"{len(entries)} result{'s' if len(entries) != 1 else ''}{suffix}")
        if hasattr(self, "clear_history_btn"):
            self.clear_history_btn.setEnabled(bool(all_entries))
        if not visible_entries:
            empty = _card()
            empty_layout = QVBoxLayout(empty)
            empty_layout.setContentsMargins(16, 18, 16, 18)
            empty_label = QLabel("No dictation matches that search yet.") if query else QLabel("Your next thought will appear here.")
            empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
            empty_layout.addWidget(empty_label)
            self.dictation_list_layout.insertWidget(self.dictation_list_layout.count() - 1, empty)
            return
        for entry in visible_entries:
            self.dictation_list_layout.insertWidget(self.dictation_list_layout.count() - 1, self._dictation_row(entry))

    def _clear_dictation_history(self):
        reply = QMessageBox.question(
            self, "Clear history", "Delete all saved dictation history? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            history.clear(kind="dictation")
            self._reload_dictation_history()
            if hasattr(self, "insights_tab"):
                self.insights_tab.refresh()

    def _dictation_row(self, entry: dict) -> QFrame:
        import datetime

        row = _card()
        v = QVBoxLayout(row)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(8)
        text = QLabel(entry.get("text", ""))
        text.setWordWrap(True)
        text.setMinimumWidth(0)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.setStyleSheet(f"color: {theme.TEXT};")
        v.addWidget(text)

        meta = QHBoxLayout()
        ts = entry.get("ts")
        when = datetime.datetime.fromtimestamp(ts).strftime("%b %d, %I:%M %p") if ts else ""
        when_label = QLabel(when)
        when_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        meta.addWidget(when_label)
        context_app = str(entry.get("context_app", "")).strip()
        if context_app:
            meta.addWidget(_pill(context_app, theme.TEXT_DIM, theme.SURFACE))
        meta.addStretch(1)
        copy_btn = QPushButton("Copy")
        copy_btn.setObjectName("secondary")
        copy_btn.setText("⧉")
        copy_btn.setFixedSize(34, 28)
        copy_btn.setToolTip("Copy transcript")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(entry.get("text", "")))
        meta.addWidget(copy_btn)
        v.addLayout(meta)
        return row

    # --- Settings tab ------------------------------------------------------

    def _build_settings_tab(self) -> QWidget:
        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(12)
        cfg = config.load()

        header = QHBoxLayout()
        heading_col = QVBoxLayout()
        heading = QLabel("Settings")
        heading.setStyleSheet(f"color: {theme.TEXT}; font-size: 20px; font-weight: 700;")
        heading_col.addWidget(heading)
        intro = QLabel("The few controls you need every day. Everything stays on this Mac.")
        intro.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        intro.setWordWrap(True)
        heading_col.addWidget(intro)
        header.addLayout(heading_col, stretch=1)
        advanced_btn = QPushButton("Advanced settings…")
        advanced_btn.setObjectName("secondary")
        advanced_btn.clicked.connect(self._open_advanced_settings)
        header.addWidget(advanced_btn, alignment=Qt.AlignmentFlag.AlignTop)
        v.addLayout(header)

        dictation_card = _card()
        dictation_layout = QVBoxLayout(dictation_card)
        dictation_layout.setContentsMargins(14, 12, 14, 12)
        dictation_title = QLabel("Dictation")
        dictation_title.setStyleSheet(f"color: {theme.TEXT}; font-size: 16px; font-weight: 700;")
        dictation_layout.addWidget(dictation_title)
        dictation_note = QLabel("Choose how Chatter starts listening and which language it expects.")
        dictation_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        dictation_layout.addWidget(dictation_note)

        enabled_row = QHBoxLayout()
        self.ptt_enabled_checkbox = ToggleSwitch("Push-to-talk enabled")
        self.ptt_enabled_checkbox.setChecked(cfg.get("push_to_talk_enabled", True))
        self.ptt_enabled_checkbox.toggled.connect(self._on_push_to_talk_toggled)
        enabled_row.addWidget(self.ptt_enabled_checkbox)
        enabled_row.addStretch(1)
        dictation_layout.addLayout(enabled_row)

        activation_row = QHBoxLayout()
        activation_label = QLabel("Activation")
        activation_label.setMinimumWidth(105)
        activation_row.addWidget(activation_label)
        self.activation_combo = QComboBox()
        for mode, label in ACTIVATION_MODE_OPTIONS:
            self.activation_combo.addItem(label, userData=mode)
        activation_index = self.activation_combo.findData(
            normalize_activation_mode(cfg.get("hotkey_activation_mode", HOLD_TO_TALK))
        )
        if activation_index >= 0:
            self.activation_combo.setCurrentIndex(activation_index)
        self.activation_combo.currentIndexChanged.connect(self._on_activation_mode_changed)
        activation_row.addWidget(self.activation_combo, stretch=1)
        dictation_layout.addLayout(activation_row)
        self.activation_note = QLabel()
        self.activation_note.setWordWrap(True)
        self.activation_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        dictation_layout.addWidget(self.activation_note)
        self._update_activation_note()

        hotkey_row = QHBoxLayout()
        hotkey_label = QLabel("Hotkey")
        hotkey_label.setMinimumWidth(105)
        hotkey_row.addWidget(hotkey_label)
        self.hotkey_combo = QComboBox()
        current_keycode = cfg.get("hotkey_keycode", 60)
        for keycode, label, _warning in SUPPORTED_HOTKEYS:
            self.hotkey_combo.addItem(label, userData=keycode)
        idx = self.hotkey_combo.findData(current_keycode)
        if idx >= 0:
            self.hotkey_combo.setCurrentIndex(idx)
        self.hotkey_combo.currentIndexChanged.connect(self._on_hotkey_key_changed)
        hotkey_row.addWidget(self.hotkey_combo, stretch=1)
        dictation_layout.addLayout(hotkey_row)

        self.hotkey_warning_label = QLabel("")
        self.hotkey_warning_label.setStyleSheet(f"color: {theme.PROCESSING}; font-size: 11px;")
        self.hotkey_warning_label.setWordWrap(True)
        self.hotkey_warning_label.hide()
        dictation_layout.addWidget(self.hotkey_warning_label)
        self._update_hotkey_warning(current_keycode)

        language_row = QHBoxLayout()
        language_label = QLabel("Language")
        language_label.setMinimumWidth(105)
        language_row.addWidget(language_label)
        self.language_combo = QComboBox()
        self.language_combo.addItem("English (recommended)", userData="en")
        self.language_combo.addItem("Auto-detect", userData="")
        current_language = cfg.get("language", "en")
        language_idx = self.language_combo.findData(current_language)
        if language_idx >= 0:
            self.language_combo.setCurrentIndex(language_idx)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        language_row.addWidget(self.language_combo, stretch=1)
        dictation_layout.addLayout(language_row)
        v.addWidget(dictation_card)

        audio_card = _card()
        audio_layout = QVBoxLayout(audio_card)
        audio_layout.setContentsMargins(14, 12, 14, 12)
        audio_title = QLabel("Audio")
        audio_title.setStyleSheet(f"color: {theme.TEXT}; font-size: 16px; font-weight: 700;")
        audio_layout.addWidget(audio_title)

        input_row = QHBoxLayout()
        input_label = QLabel("Microphone")
        input_label.setMinimumWidth(105)
        input_row.addWidget(input_label)
        self.input_device_combo = QComboBox()
        self.input_device_combo.addItem("System default", userData="")
        try:
            for device_index, device in enumerate(sd.query_devices()):
                if device.get("max_input_channels", 0) > 0:
                    device_name = device.get("name", f"Input {device_index}")
                    self.input_device_combo.addItem(device_name, userData=device_name)
        except Exception:
            logger.exception("couldn't enumerate microphone devices")
        current_device = cfg.get("input_device", "")
        if isinstance(current_device, int):
            # Migrate the pre-existing index-based preference to a stable
            # device name so reconnecting a headset cannot silently point at
            # a different PortAudio index.
            try:
                current_device = sd.query_devices(current_device).get("name", "")
            except Exception:
                current_device = ""
        idx = self.input_device_combo.findData(current_device)
        if idx >= 0:
            self.input_device_combo.setCurrentIndex(idx)
        self.input_device_combo.currentIndexChanged.connect(self._on_input_device_changed)
        input_row.addWidget(self.input_device_combo, stretch=1)
        self.mic_test_btn = QPushButton("Test")
        self.mic_test_btn.setObjectName("secondary")
        self.mic_test_btn.clicked.connect(self._test_microphone)
        input_row.addWidget(self.mic_test_btn)
        audio_layout.addLayout(input_row)

        input_note = QLabel("System default follows the microphone selected in macOS. Chatter asks permission before use and keeps audio on this Mac.")
        input_note.setWordWrap(True)
        input_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        audio_layout.addWidget(input_note)
        v.addWidget(audio_card)

        app_card = _card()
        app_layout = QVBoxLayout(app_card)
        app_layout.setContentsMargins(14, 12, 14, 12)
        app_title = QLabel("Chatter")
        app_title.setStyleSheet(f"color: {theme.TEXT}; font-size: 16px; font-weight: 700;")
        app_layout.addWidget(app_title)

        menu_bar_row = QHBoxLayout()
        self.menu_bar_icon_checkbox = ToggleSwitch("Show Chatter in the menu bar")
        self.menu_bar_icon_checkbox.setChecked(cfg.get("menu_bar_icon_enabled", True))
        self.menu_bar_icon_checkbox.toggled.connect(self._on_menu_bar_icon_toggled)
        menu_bar_row.addWidget(self.menu_bar_icon_checkbox)
        menu_bar_row.addStretch(1)
        app_layout.addLayout(menu_bar_row)
        menu_bar_note = QLabel(
            "The menu-bar icon shows a small local activity summary. You can hide it here and turn it back on from this Settings tab."
        )
        menu_bar_note.setWordWrap(True)
        menu_bar_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        app_layout.addWidget(menu_bar_note)

        update_row = QHBoxLayout()
        self.updates_checkbox = ToggleSwitch("Check for Chatter updates")
        self.updates_checkbox.setChecked(cfg.get("updates_enabled", True))
        self.updates_checkbox.toggled.connect(self._on_updates_toggled)
        update_row.addWidget(self.updates_checkbox)
        update_row.addStretch(1)
        self.version_label = QLabel("Version —")
        self.version_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        update_row.addWidget(self.version_label)
        self.check_updates_btn = QPushButton("Check now")
        self.check_updates_btn.setObjectName("secondary")
        self.check_updates_btn.clicked.connect(lambda: self.update_check_requested.emit(True))
        update_row.addWidget(self.check_updates_btn)
        app_layout.addLayout(update_row)
        self.update_status_label = QLabel("Checks use only the public Chatter release page.")
        self.update_status_label.setWordWrap(True)
        self.update_status_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        app_layout.addWidget(self.update_status_label)
        v.addWidget(app_card)

        writing_card = _card()
        writing_layout = QVBoxLayout(writing_card)
        writing_layout.setContentsMargins(14, 12, 14, 12)
        cleanup_title = QLabel("Writing")
        cleanup_title.setStyleSheet(f"color: {theme.TEXT}; font-size: 16px; font-weight: 700;")
        writing_layout.addWidget(cleanup_title)
        cleanup_intro = QLabel("Make the final text easier to paste into the app you are using.")
        cleanup_intro.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        writing_layout.addWidget(cleanup_intro)
        cleanup_row = QHBoxLayout()
        self.format_checkbox = ToggleSwitch("Clean up with local AI")
        self.format_checkbox.setChecked(cfg.get("formatting_enabled", True))
        self.format_checkbox.toggled.connect(self._on_formatting_toggled)
        cleanup_row.addWidget(self.format_checkbox)
        cleanup_row.addStretch(1)
        writing_layout.addLayout(cleanup_row)
        cleanup_note = QLabel(
            "Uses a small local model after Nemotron finishes. It never blocks live transcription."
        )
        cleanup_note.setWordWrap(True)
        cleanup_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        writing_layout.addWidget(cleanup_note)

        context_row = QHBoxLayout()
        context_label = QLabel("Writing context")
        context_label.setMinimumWidth(105)
        context_row.addWidget(context_label)
        self.context_combo = QComboBox()
        context_options = [
            ("Automatic (foreground app)", "auto"),
            ("Neutral dictation", "general"),
            ("Professional email", "email"),
            ("Notes / journal", "notes"),
            ("Coding / AI prompt", "coding"),
            ("Social / chat", "social"),
        ]
        for label, value in context_options:
            self.context_combo.addItem(label, userData=value)
        context_idx = self.context_combo.findData(cfg.get("cleanup_context_mode", "auto"))
        if context_idx >= 0:
            self.context_combo.setCurrentIndex(context_idx)
        self.context_combo.currentIndexChanged.connect(self._on_context_changed)
        context_row.addWidget(self.context_combo, stretch=1)
        writing_layout.addLayout(context_row)
        context_note = QLabel(
            "Automatic uses only the foreground app name as a local hint; it never reads the page or document."
        )
        context_note.setWordWrap(True)
        context_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        writing_layout.addWidget(context_note)
        v.addWidget(writing_card)

        # Retain this control for the advanced dialog, but do not expose an
        # experimental decoder switch in the everyday settings surface.
        self.mtp_checkbox = QCheckBox("Try Gemma multi-token prediction (experimental)")
        self.mtp_checkbox.setChecked(cfg.get("llama_mtp_enabled", False))
        self.mtp_checkbox.setEnabled(self.format_checkbox.isChecked())
        self.mtp_checkbox.toggled.connect(self._on_mtp_toggled)
        self.mtp_checkbox.hide()

        permission_card = _card()
        permission_layout = QVBoxLayout(permission_card)
        permission_layout.setContentsMargins(14, 12, 14, 12)
        permission_title = QLabel("Permissions")
        permission_title.setStyleSheet(f"color: {theme.TEXT}; font-size: 16px; font-weight: 700;")
        permission_layout.addWidget(permission_title)
        self.permission_status_label = QLabel()
        self.permission_status_label.setWordWrap(True)
        self.permission_status_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        permission_layout.addWidget(self.permission_status_label)
        permission_row = QHBoxLayout()
        mic_permissions_btn = QPushButton("Microphone")
        mic_permissions_btn.setObjectName("secondary")
        mic_permissions_btn.clicked.connect(self._request_microphone_permission)
        input_permissions_btn = QPushButton("Input Monitoring")
        input_permissions_btn.setObjectName("secondary")
        input_permissions_btn.clicked.connect(permissions.open_input_monitoring_settings)
        accessibility_btn = QPushButton("Accessibility")
        accessibility_btn.setObjectName("secondary")
        accessibility_btn.clicked.connect(permissions.open_accessibility_settings)
        permission_row.addWidget(mic_permissions_btn)
        permission_row.addWidget(input_permissions_btn)
        permission_row.addWidget(accessibility_btn)
        permission_layout.addLayout(permission_row)
        self._refresh_permission_status()

        self.hotkey_status_label = QLabel("")
        self.hotkey_status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        permission_layout.addWidget(self.hotkey_status_label)

        note = QLabel("Chatter uses one local streaming model for push-to-talk. All audio, transcripts, and cleanup stay on this Mac.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        permission_layout.addWidget(note)
        v.addWidget(permission_card)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        scroll.setWidget(content)
        return scroll

    def _open_advanced_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Advanced settings")
        dialog.setMinimumSize(700, 560)

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(10)

        title = QLabel("Advanced settings")
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 20px; font-weight: 700;")
        outer.addWidget(title)
        intro = QLabel(
            "These controls are optional. Chatter already has a recommended local setup; "
            "change this only when you want to choose model files or tune performance."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        outer.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 4)
        content_layout.setSpacing(12)
        content_layout.addWidget(self._build_models_tab())

        performance_card = _card()
        performance_layout = QVBoxLayout(performance_card)
        performance_layout.setContentsMargins(14, 12, 14, 12)
        performance_title = QLabel("EXPERIMENTAL PERFORMANCE")
        performance_title.setObjectName("sectionTitle")
        performance_layout.addWidget(performance_title)
        self._advanced_mtp_checkbox = QCheckBox(
            "Try Gemma multi-token prediction for local AI cleanup"
        )
        self._advanced_mtp_checkbox.setChecked(config.load().get("llama_mtp_enabled", False))
        self._advanced_mtp_checkbox.setEnabled(self.format_checkbox.isChecked())
        self._advanced_mtp_checkbox.toggled.connect(self._on_mtp_toggled)
        performance_layout.addWidget(self._advanced_mtp_checkbox)
        performance_note = QLabel(
            "This affects only the optional cleanup model. It never changes the live "
            "Nemotron transcription path and may be slower on some Macs."
        )
        performance_note.setWordWrap(True)
        performance_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        performance_layout.addWidget(performance_note)
        content_layout.addWidget(performance_card)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, stretch=1)

        close_btn = QPushButton("Done")
        close_btn.setObjectName("primary")
        close_btn.clicked.connect(dialog.accept)
        outer.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

        try:
            dialog.exec()
        finally:
            self._advanced_mtp_checkbox = None

    def _update_hotkey_warning(self, keycode: int):
        warning = next((w for kc, _l, w in SUPPORTED_HOTKEYS if kc == keycode), None)
        if warning:
            self.hotkey_warning_label.setText(f"⚠ {warning}")
            self.hotkey_warning_label.show()
        else:
            self.hotkey_warning_label.hide()

    def _on_hotkey_key_changed(self, _index: int):
        keycode = self.hotkey_combo.currentData()
        config.update(hotkey_keycode=keycode)
        self._update_hotkey_warning(keycode)
        self._update_live_hotkey_pill()
        self.hotkey_changed.emit()

    def _on_activation_mode_changed(self, _index: int):
        config.update(
            hotkey_activation_mode=normalize_activation_mode(
                self.activation_combo.currentData()
            )
        )
        self._update_live_hotkey_pill()
        self._update_activation_note()

    def _update_activation_note(self):
        mode = normalize_activation_mode(
            config.load().get("hotkey_activation_mode", HOLD_TO_TALK)
        )
        if mode == DOUBLE_TAP_PERSISTENT:
            self.activation_note.setText(
                "Hands-free mode is explicit: double-tap your hotkey to start listening, "
                "then double-tap the same hotkey again to stop. Releasing the key alone does not stop it."
            )
        else:
            self.activation_note.setText(
                "Hold the hotkey while speaking. Releasing it stops the current dictation."
            )

    def _on_push_to_talk_toggled(self, checked: bool):
        config.update(push_to_talk_enabled=checked)
        self.hotkey_changed.emit()

    def _on_menu_bar_icon_toggled(self, checked: bool):
        config.update(menu_bar_icon_enabled=checked)
        self.menu_bar_icon_visibility_changed.emit(checked)

    def _on_updates_toggled(self, checked: bool):
        config.update(updates_enabled=checked)
        if checked:
            self.update_check_requested.emit(False)
        else:
            self.update_status_label.setText("Update checks are off. You can turn them back on anytime.")

    def set_update_status(self, message: str, *, error: bool = False):
        if not hasattr(self, "update_status_label"):
            return
        self.update_status_label.setText(message)
        self.update_status_label.setStyleSheet(
            f"color: {theme.PROCESSING if error else theme.TEXT_DIM}; font-size: 11px;"
        )

    def set_installed_version(self, version: str):
        if hasattr(self, "version_label"):
            self.version_label.setText(f"Version {version}")

    def _on_input_device_changed(self, _index: int):
        config.update(input_device=self.input_device_combo.currentData())

    def _on_language_changed(self, _index: int):
        config.update(language=self.language_combo.currentData())

    def _on_context_changed(self, _index: int):
        config.update(cleanup_context_mode=self.context_combo.currentData())

    def _test_microphone(self):
        self.mic_test_btn.setEnabled(False)
        self.mic_test_btn.setText("Listening…")
        self._mic_test_worker = MicTestWorker(self.input_device_combo.currentData())

        def finished(ok: bool, message: str):
            self.mic_test_btn.setEnabled(True)
            self.mic_test_btn.setText("Test")
            self.permission_status_label.setText(message)
            self.permission_status_label.setStyleSheet(
                f"color: {theme.DONE if ok else theme.PROCESSING}; font-size: 11px;"
            )

        self._mic_test_worker.finished.connect(finished)
        self._mic_test_worker.start()

    def _refresh_permission_status(self):
        microphone_state = "Granted" if permissions.is_microphone_authorized() else "Needs permission"
        input_state = "Granted" if permissions.input_monitoring_available() else "Needs permission"
        accessibility_state = "Granted" if permissions.is_trusted() else "Needs permission"
        self.permission_status_label.setText(
            f"Microphone: {microphone_state}. "
            f"Input Monitoring: {input_state}. Accessibility: {accessibility_state}. "
            "All microphone audio and transcript processing stays on this Mac."
        )

    def _request_microphone_permission(self):
        if not permissions.is_microphone_authorized():
            permissions.request_microphone_access()
            QTimer.singleShot(400, permissions.open_microphone_settings)
        else:
            permissions.open_microphone_settings()

    def _on_hotkey_status(self, status: str):
        if status == "Idle":
            self.hotkey_status_label.setText("")
        else:
            self.hotkey_status_label.setText(f"⇧ {status}")

    def _on_formatting_toggled(self, checked: bool):
        config.update(formatting_enabled=checked)
        if hasattr(self, "mtp_checkbox"):
            self.mtp_checkbox.setEnabled(checked)
        if self._advanced_mtp_checkbox is not None:
            self._advanced_mtp_checkbox.setEnabled(checked)
        if checked:
            threading.Thread(target=self.formatter.warm_up, daemon=True).start()

    def _on_mtp_toggled(self, checked: bool):
        config.update(llama_mtp_enabled=checked)

        def restart_formatter():
            self.formatter.shutdown()
            if checked and config.load().get("formatting_enabled", False):
                self.formatter.warm_up()

        threading.Thread(target=restart_formatter, daemon=True).start()

    # --- file transcription actions ---------------------------------------

    def refresh_models(self):
        models = list_models()
        configured = config.load().get("whisper_model_path", "")
        configured_path = Path(configured).expanduser() if configured else None
        if configured_path and configured_path.exists():
            self.file_model_path = str(configured_path)
        else:
            timestamp_mode = config.load().get("file_timestamp_mode", "word")
            preferred_names = ("parakeet", "nemotron", "whisper") if timestamp_mode == "word" else ("whisper", "parakeet", "nemotron")
            preferred = next(
                (m for name in preferred_names for m in models if name in m.name.lower()),
                None,
            )
            selected = preferred or (models[0] if models else None)
            self.file_model_path = str(selected) if selected else None
        self._update_file_timestamp_note()
        self.update_transcribe_enabled()

    def _on_file_timestamp_mode_changed(self, _index: int):
        mode = self.file_timestamp_combo.currentData()
        config.update(file_timestamp_mode=mode)
        self.refresh_models()

    def _update_file_timestamp_note(self):
        mode = config.load().get("file_timestamp_mode", "word")
        model_name = Path(self.file_model_path).name if self.file_model_path else "No file model selected"
        if mode == "word":
            if "whisper" in model_name.lower():
                self.file_timestamp_note.setText(
                    f"Current model: {model_name}. Whisper returns phrase timings here, so word-level "
                    "exports stay unavailable for this transcription. Choose Parakeet TDT in Models "
                    "for one timestamp per spoken word."
                )
            else:
                self.file_timestamp_note.setText(
                    f"Current model: {model_name}. Word-level exports create one timestamp per spoken word. "
                    "The model selected here is the model used for this file transcription."
                )
        else:
            self.file_timestamp_note.setText(
                f"Phrase-level exports use larger subtitle cues. Current model: {model_name}. "
                "Whisper large-v3 Turbo is a strong compatible choice."
            )

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose audio or video file", "",
            "Media files (*.mp3 *.wav *.m4a *.mp4 *.mov *.mkv *.flac *.aac);;All files (*)",
        )
        if path:
            self._set_media_path(path)

    def _set_media_path(self, path: str):
        self.media_path = path
        self.file_label.setText(Path(path).name)
        self.file_label.setStyleSheet(f"color: {theme.TEXT};")
        self.update_transcribe_enabled()

    def update_transcribe_enabled(self):
        has_model = bool(self.file_model_path)
        self.transcribe_btn.setEnabled(bool(self.media_path) and has_model)

    def start_transcription(self):
        model_path = self.file_model_path
        if not model_path or not self.media_path:
            return

        self.transcribe_btn.setEnabled(False)
        self.open_btn.setEnabled(False)
        self.progress_bar.show()

        backend = config.load().get("backend", "auto") or "auto"
        self.worker = TranscribeWorker(
            model_path, backend, self.media_path,
            self.formatter, config.load().get("formatting_enabled", True),
            config.load().get("file_timestamp_mode", "word"),
        )
        self.worker.progress.connect(lambda msg: self.status_label.setText(msg))
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_finished(self, text: str, words, segments, tokens):
        self.progress_bar.hide()
        self.status_label.setText("Done.")
        self.transcribe_btn.setEnabled(True)
        self.open_btn.setEnabled(True)
        word_dicts = []
        for word in words or []:
            word_text = self._word_value(word, "text")
            start_ms = self._word_value(word, "t0_ms")
            end_ms = self._word_value(word, "t1_ms")
            if word_text is None or start_ms is None or end_ms is None:
                continue
            try:
                word_dicts.append({
                    "text": str(word_text),
                    "t0_ms": float(start_ms),
                    "t1_ms": float(end_ms),
                })
            except (TypeError, ValueError):
                continue
        segment_dicts = []
        for segment in segments or []:
            segment_text = self._word_value(segment, "text")
            start_ms = self._word_value(segment, "t0_ms")
            end_ms = self._word_value(segment, "t1_ms")
            if segment_text is None or start_ms is None or end_ms is None:
                continue
            try:
                segment_dicts.append({
                    "text": str(segment_text),
                    "t0_ms": float(start_ms),
                    "t1_ms": float(end_ms),
                })
            except (TypeError, ValueError):
                continue
        token_dicts = []
        for token in tokens or []:
            token_text = self._word_value(token, "text")
            start_ms = self._word_value(token, "t0_ms")
            end_ms = self._word_value(token, "t1_ms")
            word_index = self._word_value(token, "word_index")
            if token_text is None or start_ms is None or end_ms is None or word_index is None:
                continue
            try:
                token_dicts.append({
                    "text": str(token_text),
                    "t0_ms": float(start_ms),
                    "t1_ms": float(end_ms),
                    "word_index": int(word_index),
                })
            except (TypeError, ValueError):
                continue
        requested_timestamp_mode = config.load().get("file_timestamp_mode", "word")
        actual_timestamp_mode = (
            "word" if word_dicts or token_dicts else
            "phrase" if segment_dicts else
            "none"
        )
        history.append(
            "file",
            text,
            filename=Path(self.media_path).name if self.media_path else "recording",
            words=word_dicts,
            segments=segment_dicts,
            tokens=token_dicts,
            model_name=Path(self.file_model_path).name if self.file_model_path else "",
            requested_timestamp_mode=requested_timestamp_mode,
            actual_timestamp_mode=actual_timestamp_mode,
        )
        self._reload_files_history()

    def on_failed(self, message: str):
        self.progress_bar.hide()
        self.status_label.setText("Failed.")
        self.transcribe_btn.setEnabled(True)
        self.open_btn.setEnabled(True)
        QMessageBox.critical(self, "Transcription failed", message)
