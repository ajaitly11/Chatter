import logging
import math
import shutil
import threading
import time
import traceback
from pathlib import Path

import AppKit
import numpy as np
import objc
import sounddevice as sd
from PyQt6.QtCore import (
    QEasingCurve,
    Qt,
    QPropertyAnimation,
    QThread,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import config
from . import dictionary
from . import history
from . import llama_runtime
from . import permissions
from .audio_capture import StreamingMicRecorder
from . import theme
from .mascot import Mascot
from .native_hotkey import SUPPORTED_HOTKEYS
from .transcription_service import (
    CLEANUP_MODELS_DIR,
    MODELS_DIR,
    decode_to_pcm,
    list_models,
    words_to_srt,
    service,
)

MEDIA_EXTENSIONS = {".mp3", ".wav", ".m4a", ".mp4", ".mov", ".mkv", ".flac", ".aac"}
logger = logging.getLogger("chatter.main_window")

WINDOW_W = 680
WINDOW_H = 470
MIN_WINDOW_W = 560
MIN_WINDOW_H = 420
CONTENT_H = 340  # each tab's list areas default to about this tall, but expand with the window


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
    finished = pyqtSignal(str, object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, model_path: str, backend: str, media_path: str, formatter, format_enabled: bool):
        super().__init__()
        self.model_path = model_path
        self.backend = backend
        self.media_path = media_path
        self.formatter = formatter
        self.format_enabled = format_enabled

    def run(self):
        try:
            self.progress.emit("Decoding audio…")
            pcm = decode_to_pcm(self.media_path)

            self.progress.emit("Transcribing…")
            # transcribe.cpp only populates Result.words (needed for
            # word-level SRT export) when explicitly asked — its default
            # ("auto") timestamp mode doesn't include them.
            language = config.load().get("language", "en") or None
            result = service.transcribe(
                pcm, self.model_path, self.backend, language=language, timestamps="word"
            )
            text = dictionary.apply_corrections(result.text)
            words = getattr(result, "words", None)

            if self.format_enabled and text.strip():
                self.progress.emit("Cleaning up with AI…")
                text = self.formatter.format_transcript(text)

            self.finished.emit(text, words)
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


class MainWindow(QMainWindow):
    hotkey_changed = pyqtSignal()  # push-to-talk key was changed; listener needs a restart

    def __init__(self, formatter):
        super().__init__()
        self.setWindowTitle("Chatter")
        self.resize(WINDOW_W, WINDOW_H)
        self.setMinimumSize(MIN_WINDOW_W, MIN_WINDOW_H)
        self.setAcceptDrops(True)
        _match_titlebar_to_theme(self)

        self.formatter = formatter
        self.media_path: str | None = None
        self.last_words = None
        self.worker: TranscribeWorker | None = None
        self._latest_live_preview = ""
        self._live_idle_timer = QTimer(self)
        self._live_idle_timer.setSingleShot(True)
        self._live_idle_timer.timeout.connect(lambda: self.set_live_state("idle"))

        self.tabs = QTabWidget()
        self.tabs.setTabBar(SquiggleTabBar())
        self.tabs.addTab(self._build_live_tab(), "Live Dictation")
        self.tabs.addTab(self._build_files_tab(), "Files")
        self.tabs.addTab(self._build_models_tab(), "Models")
        self.tabs.addTab(self._build_dictionary_tab(), "Dictionary")
        self.tabs.addTab(self._build_history_tab(), "History")
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        self.setCentralWidget(self.tabs)
        # The first underline can be painted before QTabBar has its final
        # geometry. Synchronize once after the window is laid out so Live
        # Dictation looks complete on first launch, not only after a tab swap.
        QTimer.singleShot(80, self.tabs.tabBar()._sync_to_current)

        self.refresh_models()
        self._reload_files_history()
        self._reload_dictation_history()

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

        self.practice_box = QTextEdit()
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
        self.live_hotkey_pill.setText(f"Hold {label} to talk")

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

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        model_row.addWidget(self.model_combo, stretch=1)
        model_row.addWidget(QLabel("Backend:"))
        self.backend_combo = QComboBox()
        self.backend_combo.addItems(["auto", "cpu", "vulkan", "metal", "cuda"])
        cfg = config.load()
        idx = self.backend_combo.findText(cfg.get("backend", "auto"))
        if idx >= 0:
            self.backend_combo.setCurrentIndex(idx)
        self.backend_combo.currentTextChanged.connect(lambda val: config.update(backend=val))
        model_row.addWidget(self.backend_combo)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_models)
        model_row.addWidget(refresh_btn)
        v.addLayout(model_row)

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
        name = QLabel(entry.get("filename", "recording"))
        name.setStyleSheet(f"color: {theme.TEXT}; font-weight: 500;")
        h.addWidget(name, stretch=1)
        h.addWidget(_pill("Done", theme.DONE, theme.rgba_str(theme.done_dim())))
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(entry.get("text", "")))
        h.addWidget(copy_btn)
        export_txt = QPushButton("Export .txt")
        export_txt.clicked.connect(lambda: self._export_history_entry(entry, "txt"))
        h.addWidget(export_txt)
        if entry.get("words"):
            export_srt = QPushButton("Export .srt")
            export_srt.clicked.connect(lambda: self._export_history_entry(entry, "srt"))
            h.addWidget(export_srt)
        return row

    def _export_history_entry(self, entry: dict, kind: str):
        default_name = Path(entry.get("filename", "transcript")).stem + f".{kind}"
        filt = "Text files (*.txt)" if kind == "txt" else "SubRip files (*.srt)"
        path, _ = QFileDialog.getSaveFileName(self, "Save", default_name, filt)
        if not path:
            return
        try:
            content = entry["text"] if kind == "txt" else words_to_srt(entry.get("words", []))
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
        heading = QLabel("Models that make sense")
        heading.setStyleSheet(f"color: {theme.TEXT}; font-size: 18px; font-weight: 700;")
        heading_col.addWidget(heading)
        subtitle = QLabel("One model for speed. One optional model for polish.")
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
                MODELS_DIR / "whisper-large-v3-turbo-Q8_0.gguf",
                MODELS_DIR / "parakeet-tdt-0.6b-v2-Q8_0.gguf",
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

    def _start_llama_runtime_download(self):
        self.llama_runtime_btn.setEnabled(False)
        self.llama_runtime_btn.setText("Starting…")
        worker = llama_runtime.LlamaServerDownloadWorker()

        def on_progress(done: int, total: int):
            if total:
                self.llama_runtime_btn.setText(f"{done * 100 // total}%")
            else:
                self.llama_runtime_btn.setText(f"{done // (1024 * 1024)} MB")

        def on_done(_path: str):
            self._refresh_llama_runtime_status()

        def on_failed(msg: str):
            self.llama_runtime_btn.setEnabled(True)
            self.llama_runtime_btn.setText("Retry")
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

        heading = QLabel("Your personal vocabulary")
        heading.setStyleSheet(f"color: {theme.TEXT}; font-size: 18px; font-weight: 700;")
        v.addWidget(heading)
        explanation = QLabel(
            "Chatter learns small, high-confidence corrections from edits after a paste. "
            "You can also add a name, phrase, or fused word here."
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
        self.dict_hint.setText(f"Chatter's learned {len(entries)} correction{'s' if len(entries) != 1 else ''} from you so far.")
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
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(10)

        heading = QLabel("History")
        heading.setStyleSheet(f"color: {theme.TEXT}; font-size: 18px; font-weight: 700;")
        v.addWidget(heading)

        row = QHBoxLayout()
        subtitle = QLabel("Your recent dictation, kept even if it never got pasted.")
        subtitle.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        row.addWidget(subtitle)
        row.addStretch(1)
        self.history_count_label = QLabel("")
        self.history_count_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        row.addWidget(self.history_count_label)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_dictation_history)
        row.addWidget(clear_btn)
        v.addLayout(row)

        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("Search your dictation…")
        self.history_search.textChanged.connect(self._reload_dictation_history)
        v.addWidget(self.history_search)

        self.dictation_list_container = QWidget()
        self.dictation_list_layout = QVBoxLayout(self.dictation_list_container)
        self.dictation_list_layout.setContentsMargins(0, 0, 0, 0)
        self.dictation_list_layout.setSpacing(7)
        self.dictation_list_layout.addStretch(1)
        v.addWidget(_scroll_list(self.dictation_list_container), stretch=1)
        return w

    def _reload_dictation_history(self):
        while self.dictation_list_layout.count() > 1:
            item = self.dictation_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        query = self.history_search.text().strip().lower() if hasattr(self, "history_search") else ""
        entries = history.load(kind="dictation", limit=100)
        if query:
            entries = [entry for entry in entries if query in entry.get("text", "").lower()]
        if hasattr(self, "history_count_label"):
            self.history_count_label.setText(f"{len(entries)} result{'s' if len(entries) != 1 else ''}")
        if not entries:
            empty = _card()
            empty_layout = QVBoxLayout(empty)
            empty_layout.setContentsMargins(16, 18, 16, 18)
            empty_label = QLabel("No dictation matches that search yet.") if query else QLabel("Your next thought will appear here.")
            empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
            empty_layout.addWidget(empty_label)
            self.dictation_list_layout.insertWidget(self.dictation_list_layout.count() - 1, empty)
            return
        for entry in entries:
            self.dictation_list_layout.insertWidget(self.dictation_list_layout.count() - 1, self._dictation_row(entry))

    def _clear_dictation_history(self):
        reply = QMessageBox.question(
            self, "Clear history", "Delete all saved dictation history? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            history.clear(kind="dictation")
            self._reload_dictation_history()

    def _dictation_row(self, entry: dict) -> QFrame:
        import datetime

        row = _card()
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 7, 10, 7)
        text = QLabel(entry.get("text", ""))
        text.setWordWrap(True)
        text.setStyleSheet(f"color: {theme.TEXT};")
        h.addWidget(text, stretch=1)
        ts = entry.get("ts")
        when = datetime.datetime.fromtimestamp(ts).strftime("%b %d, %I:%M %p") if ts else ""
        when_label = QLabel(when)
        when_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        h.addWidget(when_label)
        pasted = entry.get("pasted")
        h.addWidget(_pill("Pasted" if pasted else "Copied", theme.DONE if pasted else theme.TEXT_DIM,
                           theme.rgba_str(theme.done_dim()) if pasted else theme.SURFACE))
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(entry.get("text", "")))
        h.addWidget(copy_btn)
        return row

    # --- Settings tab ------------------------------------------------------

    def _build_settings_tab(self) -> QWidget:
        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(16)
        cfg = config.load()

        heading = QLabel("Settings")
        heading.setStyleSheet(f"color: {theme.TEXT}; font-size: 18px; font-weight: 700;")
        v.addWidget(heading)

        dictation_title = QLabel("DICTATION")
        dictation_title.setObjectName("sectionTitle")
        v.addWidget(dictation_title)

        hotkey_row = QHBoxLayout()
        hotkey_row.addWidget(QLabel("Push-to-talk key:"))
        self.hotkey_combo = QComboBox()
        current_keycode = cfg.get("hotkey_keycode", 60)
        for keycode, label, _warning in SUPPORTED_HOTKEYS:
            self.hotkey_combo.addItem(label, userData=keycode)
        idx = self.hotkey_combo.findData(current_keycode)
        if idx >= 0:
            self.hotkey_combo.setCurrentIndex(idx)
        self.hotkey_combo.currentIndexChanged.connect(self._on_hotkey_key_changed)
        hotkey_row.addWidget(self.hotkey_combo, stretch=1)
        v.addLayout(hotkey_row)

        self.hotkey_warning_label = QLabel("")
        self.hotkey_warning_label.setStyleSheet(f"color: {theme.PROCESSING}; font-size: 11px;")
        self.hotkey_warning_label.setWordWrap(True)
        self.hotkey_warning_label.hide()
        v.addWidget(self.hotkey_warning_label)
        self._update_hotkey_warning(current_keycode)

        audio_title = QLabel("AUDIO")
        audio_title.setObjectName("sectionTitle")
        v.addWidget(audio_title)

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("Microphone:"))
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
        self.mic_test_btn.clicked.connect(self._test_microphone)
        input_row.addWidget(self.mic_test_btn)
        v.addLayout(input_row)

        input_note = QLabel("Choose the microphone you will speak into. Chatter asks macOS for permission before using it; audio and transcripts stay on this Mac. Chatter will show an error if it opens a device but receives no audio.")
        input_note.setWordWrap(True)
        input_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        v.addWidget(input_note)

        language_row = QHBoxLayout()
        language_row.addWidget(QLabel("Dictation language:"))
        self.language_combo = QComboBox()
        self.language_combo.addItem("English (recommended)", userData="en")
        self.language_combo.addItem("Auto-detect", userData="")
        current_language = cfg.get("language", "en")
        language_idx = self.language_combo.findData(current_language)
        if language_idx >= 0:
            self.language_combo.setCurrentIndex(language_idx)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        language_row.addWidget(self.language_combo, stretch=1)
        v.addLayout(language_row)

        language_note = QLabel("English mode prevents short or quiet clips from being mistaken for another language. Choose Auto-detect only when you regularly dictate in multiple languages.")
        language_note.setWordWrap(True)
        language_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        v.addWidget(language_note)

        cleanup_title = QLabel("TRANSCRIPT CLEANUP")
        cleanup_title.setObjectName("sectionTitle")
        v.addWidget(cleanup_title)
        cleanup_row = QHBoxLayout()
        self.format_checkbox = QCheckBox("Clean up with local AI (parallel) ✨")
        self.format_checkbox.setChecked(cfg.get("formatting_enabled", True))
        self.format_checkbox.toggled.connect(self._on_formatting_toggled)
        cleanup_row.addWidget(self.format_checkbox)
        cleanup_row.addStretch(1)
        v.addLayout(cleanup_row)
        cleanup_note = QLabel(
            "Uses the configured small local language model to repair punctuation, "
            "fused words, pauses, and formatting. It runs on-device in the "
            "background while Nemotron keeps streaming."
        )
        cleanup_note.setWordWrap(True)
        cleanup_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        v.addWidget(cleanup_note)

        context_row = QHBoxLayout()
        context_row.addWidget(QLabel("Writing context:"))
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
        v.addLayout(context_row)
        context_note = QLabel(
            "Automatic uses only the foreground app and window title as a local hint; "
            "it never reads the page or document. Choose an override when you want a "
            "consistent style everywhere."
        )
        context_note.setWordWrap(True)
        context_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        v.addWidget(context_note)

        speed_title = QLabel("CLEANUP SPEED")
        speed_title.setObjectName("sectionTitle")
        v.addWidget(speed_title)
        speed_row = QHBoxLayout()
        self.mtp_checkbox = QCheckBox("Try Gemma multi-token prediction (experimental)")
        self.mtp_checkbox.setChecked(cfg.get("llama_mtp_enabled", False))
        self.mtp_checkbox.setEnabled(self.format_checkbox.isChecked())
        self.mtp_checkbox.toggled.connect(self._on_mtp_toggled)
        speed_row.addWidget(self.mtp_checkbox)
        speed_row.addStretch(1)
        v.addLayout(speed_row)
        speed_note = QLabel(
            "Uses a matching local Gemma MTP head when available. It can reduce "
            "decode time, but this build measured slower short cleanups on Apple "
            "Silicon, so it is off by default and never changes Nemotron ASR."
        )
        speed_note.setWordWrap(True)
        speed_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        v.addWidget(speed_note)

        permission_title = QLabel("PERMISSIONS")
        permission_title.setObjectName("sectionTitle")
        v.addWidget(permission_title)
        self.permission_status_label = QLabel()
        self.permission_status_label.setWordWrap(True)
        self.permission_status_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        v.addWidget(self.permission_status_label)
        permission_row = QHBoxLayout()
        mic_permissions_btn = QPushButton("Microphone")
        mic_permissions_btn.clicked.connect(self._request_microphone_permission)
        input_permissions_btn = QPushButton("Input Monitoring")
        input_permissions_btn.clicked.connect(permissions.open_input_monitoring_settings)
        accessibility_btn = QPushButton("Accessibility")
        accessibility_btn.clicked.connect(permissions.open_accessibility_settings)
        permission_row.addWidget(mic_permissions_btn)
        permission_row.addWidget(input_permissions_btn)
        permission_row.addWidget(accessibility_btn)
        v.addLayout(permission_row)
        self._refresh_permission_status()

        self.hotkey_status_label = QLabel("")
        self.hotkey_status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        v.addWidget(self.hotkey_status_label)

        note = QLabel("Push-to-talk uses one local streaming ASR model from start to finish. File transcription uses the separate batch model. All audio, transcripts, and cleanup stay on this Mac.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        v.addWidget(note)
        v.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

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
        self.model_combo.clear()
        models = list_models()
        if not models:
            self.model_combo.addItem("No models found in ./models")
            self.model_combo.setEnabled(False)
        else:
            self.model_combo.setEnabled(True)
            for m in models:
                self.model_combo.addItem(m.name, userData=str(m))
            whisper_idx = next((i for i, m in enumerate(models) if "whisper" in m.name.lower()), None)
            if whisper_idx is not None:
                self.model_combo.setCurrentIndex(whisper_idx)
        self.update_transcribe_enabled()

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
        has_model = self.model_combo.isEnabled() and self.model_combo.count() > 0
        self.transcribe_btn.setEnabled(bool(self.media_path) and has_model)

    def start_transcription(self):
        model_path = self.model_combo.currentData()
        if not model_path or not self.media_path:
            return

        self.transcribe_btn.setEnabled(False)
        self.open_btn.setEnabled(False)
        self.progress_bar.show()

        backend = self.backend_combo.currentText()
        self.worker = TranscribeWorker(
            model_path, backend, self.media_path,
            self.formatter, config.load().get("formatting_enabled", True),
        )
        self.worker.progress.connect(lambda msg: self.status_label.setText(msg))
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_finished(self, text: str, words):
        self.progress_bar.hide()
        self.status_label.setText("Done.")
        self.last_words = words
        self.transcribe_btn.setEnabled(True)
        self.open_btn.setEnabled(True)
        word_dicts = [{"text": w.text, "t0_ms": w.t0_ms, "t1_ms": w.t1_ms} for w in words] if words else []
        history.append("file", text, filename=Path(self.media_path).name if self.media_path else "recording", words=word_dicts)
        self._reload_files_history()

    def on_failed(self, message: str):
        self.progress_bar.hide()
        self.status_label.setText("Failed.")
        self.transcribe_btn.setEnabled(True)
        self.open_btn.setEnabled(True)
        QMessageBox.critical(self, "Transcription failed", message)
