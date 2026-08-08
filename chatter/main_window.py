import logging
import shutil
import threading
import traceback
from pathlib import Path

import AppKit
import objc
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
            result = service.transcribe(pcm, self.model_path, self.backend, timestamps="word")
            text = dictionary.apply_corrections(result.text)
            words = getattr(result, "words", None)

            if self.format_enabled and text.strip():
                self.progress.emit("Cleaning up with AI…")
                text = self.formatter.format_transcript(text)

            self.finished.emit(text, words)
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


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


def _squiggle_path(width: float, y: float, amplitude: float = 2.6, period: float = 11.0) -> QPainterPath:
    """A repeating wave — chained quadratic Beziers alternating the control
    point above/below the baseline — matching the mockup's hand-drawn
    squiggle underline (its SVG paths are literally this same
    Q-then-repeat-T pattern) rather than a plain straight line."""
    path = QPainterPath()
    path.moveTo(0, y)
    x = 0.0
    up = True
    while x < width - 0.01:
        end_x = min(x + period, width)
        cx = (x + end_x) / 2
        cy = y - amplitude if up else y + amplitude
        path.quadTo(cx, cy, end_x, y)
        x = end_x
        up = not up
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
        self._anim_x = QPropertyAnimation(self, b"underlineX")
        self._anim_w = QPropertyAnimation(self, b"underlineWidth")
        for anim in (self._anim_x, self._anim_w):
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

    def _target(self, index: int):
        rect = self.tabRect(index)
        return rect.x() + _SQUIGGLE_MARGIN, rect.width() - 2 * _SQUIGGLE_MARGIN, rect.bottom() - 4

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
        painter.drawPath(_squiggle_path(self._underline_w, 0))


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
                 runtime_widget: QWidget | None = None):
        super().__init__()
        self.dest_dir = dest_dir
        self.config_key = config_key
        self.on_change = on_change

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
        self.active_label.setText(Path(current).name if current else "None selected yet — import a file below")

    def _import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a .gguf model file", "", "GGUF models (*.gguf)")
        if not path:
            return
        try:
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
        story, not just a status string. Unlike the HUD (which shows a
        short rotated phrase — "I'm here", "all ears" — meant to feel
        light in a small space), this tab is the primary surface and shows
        `label` verbatim: the literal status text ("Listening…",
        "Transcribing…") rather than flavor text, so it's unambiguous."""
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
        self.format_checkbox = QCheckBox("Clean up with AI ✨")
        self.format_checkbox.setChecked(cfg.get("formatting_enabled", True))
        self.format_checkbox.toggled.connect(self._on_formatting_toggled)
        self.transcribe_btn = QPushButton("Transcribe")
        self.transcribe_btn.setObjectName("primary")
        self.transcribe_btn.clicked.connect(self.start_transcription)
        self.transcribe_btn.setEnabled(False)
        row.addWidget(self.open_btn)
        row.addWidget(self.file_label, stretch=1)
        row.addWidget(self.format_checkbox)
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
        v.setContentsMargins(0, 0, 0, 0)
        sub_tabs = QTabWidget()
        sub_tabs.setTabBar(SquiggleTabBar())

        self.transcription_panel = _ModelSlotPanel(
            description="The Whisper/transcribe.cpp model used for push-to-talk and file transcription.",
            dest_dir=MODELS_DIR,
            config_key="whisper_model_path",
            browse_url="https://huggingface.co/models?search=transcribe.cpp",
            browse_label="browse transcribe.cpp models",
            on_change=self.refresh_models,
        )
        sub_tabs.addTab(self.transcription_panel, "Transcription")

        self.cleanup_panel = _ModelSlotPanel(
            description='A small instruction-tuned chat model for the "Clean up with AI" pass.',
            dest_dir=CLEANUP_MODELS_DIR,
            config_key="llama_model_path",
            browse_url="https://huggingface.co/models?search=instruct+gguf&sort=downloads",
            browse_label="browse instruct GGUF chat models",
            runtime_widget=self._build_llama_runtime_card(),
        )
        sub_tabs.addTab(self.cleanup_panel, "Text Cleanup")

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

        row = QHBoxLayout()
        row.addWidget(QLabel("Every push-to-talk result, kept even if it never got pasted."))
        row.addStretch(1)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_dictation_history)
        row.addWidget(clear_btn)
        v.addLayout(row)

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
        for entry in history.load(kind="dictation", limit=100):
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
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(16)
        cfg = config.load()

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

        self.hotkey_status_label = QLabel("")
        self.hotkey_status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        v.addWidget(self.hotkey_status_label)

        note = QLabel("Push-to-talk's final pass and file transcription both use the model picked in the Files tab; formatting is toggled per-file there too.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        v.addWidget(note)
        v.addStretch(1)
        return w

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

    def _on_hotkey_status(self, status: str):
        if status == "Idle":
            self.hotkey_status_label.setText("")
        else:
            self.hotkey_status_label.setText(f"⇧ {status}")

    def _on_formatting_toggled(self, checked: bool):
        config.update(formatting_enabled=checked)
        if checked:
            threading.Thread(target=self.formatter.warm_up, daemon=True).start()

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
            self.formatter, self.format_checkbox.isChecked(),
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

