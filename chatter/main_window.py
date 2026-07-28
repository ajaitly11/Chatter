import threading
import traceback
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
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
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import config
from . import dictionary
from .native_hotkey import SUPPORTED_HOTKEYS
from .transcription_service import (
    decode_to_pcm,
    list_models,
    segments_to_srt,
    service,
)

MEDIA_EXTENSIONS = {".mp3", ".wav", ".m4a", ".mp4", ".mov", ".mkv", ".flac", ".aac"}


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
            result = service.transcribe(pcm, self.model_path, self.backend)
            text = dictionary.apply_corrections(result.text)
            segments = getattr(result, "segments", None)

            if self.format_enabled and text.strip():
                self.progress.emit("Cleaning up with AI…")
                text = self.formatter.format_transcript(text)

            self.finished.emit(text, segments)
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


def _card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    return frame


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionTitle")
    return label


class MainWindow(QMainWindow):
    hotkey_changed = pyqtSignal()  # push-to-talk key was changed; listener needs a restart

    def __init__(self, formatter):
        super().__init__()
        self.setWindowTitle("Chatter")
        self.resize(820, 740)
        self.setAcceptDrops(True)

        self.formatter = formatter
        self.media_path: str | None = None
        self.last_segments = None
        self.worker: TranscribeWorker | None = None

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        layout.addWidget(self._build_file_card())
        layout.addWidget(self._build_config_card())
        layout.addWidget(self._build_dictionary_card())
        layout.addWidget(self._build_output_card(), stretch=1)
        layout.addLayout(self._build_export_row())

        self.setCentralWidget(root)
        self.refresh_models()

    # --- UI construction ---------------------------------------------------

    def _build_file_card(self) -> QFrame:
        card = _card()
        v = QVBoxLayout(card)
        v.addWidget(_section_title("File"))

        self.drop_zone = QLabel("Drag an audio or video file here, or click Open")
        self.drop_zone.setObjectName("dropZone")
        self.drop_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.drop_zone)

        row = QHBoxLayout()
        self.open_btn = QPushButton("Open audio / video…")
        self.open_btn.clicked.connect(self.open_file)
        self.file_label = QLabel("No file selected")
        self.file_label.setStyleSheet("color: #9a9ba1;")
        row.addWidget(self.open_btn)
        row.addWidget(self.file_label, stretch=1)
        v.addLayout(row)
        return card

    def _build_config_card(self) -> QFrame:
        card = _card()
        v = QVBoxLayout(card)
        v.addWidget(_section_title("Model"))

        row = QHBoxLayout()
        row.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        row.addWidget(self.model_combo, stretch=1)

        row.addWidget(QLabel("Backend:"))
        self.backend_combo = QComboBox()
        self.backend_combo.addItems(["auto", "cpu", "vulkan", "metal", "cuda"])
        cfg = config.load()
        idx = self.backend_combo.findText(cfg.get("backend", "auto"))
        if idx >= 0:
            self.backend_combo.setCurrentIndex(idx)
        self.backend_combo.currentTextChanged.connect(
            lambda val: config.update(backend=val)
        )
        row.addWidget(self.backend_combo)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_models)
        row.addWidget(refresh_btn)
        v.addLayout(row)

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
        self.hotkey_warning_label.setStyleSheet("color: #e0a83c; font-size: 11px;")
        self.hotkey_warning_label.setWordWrap(True)
        self.hotkey_warning_label.hide()
        v.addWidget(self.hotkey_warning_label)
        self._update_hotkey_warning(current_keycode)

        bottom_row = QHBoxLayout()
        self.format_checkbox = QCheckBox("Clean up with AI ✨")
        self.format_checkbox.setChecked(cfg.get("formatting_enabled", True))
        self.format_checkbox.toggled.connect(self._on_formatting_toggled)
        bottom_row.addWidget(self.format_checkbox)

        self.hotkey_status_label = QLabel("")
        self.hotkey_status_label.setStyleSheet("color: #9a9ba1;")
        bottom_row.addWidget(self.hotkey_status_label)
        bottom_row.addStretch(1)

        self.transcribe_btn = QPushButton("Transcribe")
        self.transcribe_btn.setObjectName("primary")
        self.transcribe_btn.clicked.connect(self.start_transcription)
        self.transcribe_btn.setEnabled(False)
        bottom_row.addWidget(self.transcribe_btn)
        v.addLayout(bottom_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        v.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #9a9ba1;")
        v.addWidget(self.status_label)
        return card

    def _build_dictionary_card(self) -> QFrame:
        card = _card()
        v = QVBoxLayout(card)
        v.addWidget(_section_title("Custom Dictionary"))

        hint = QLabel('Teach Chatter words it keeps mishearing — e.g. "clawed" → "Claude".')
        hint.setStyleSheet("color: #9a9ba1; font-size: 11px;")
        v.addWidget(hint)

        self.dict_table = QTableWidget(0, 2)
        self.dict_table.setHorizontalHeaderLabels(["Sounds like", "Correct to"])
        self.dict_table.verticalHeader().setVisible(False)
        self.dict_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.dict_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.dict_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.dict_table.setMaximumHeight(130)
        self._reload_dictionary_table()
        v.addWidget(self.dict_table)

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
        return card

    def _reload_dictionary_table(self):
        self.dict_table.setRowCount(0)
        for wrong, right in config.load().get("custom_dictionary", {}).items():
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

    def _build_output_card(self) -> QFrame:
        card = _card()
        v = QVBoxLayout(card)
        v.addWidget(_section_title("Transcript"))
        self.output = QTextEdit()
        self.output.setPlaceholderText("Transcript will appear here…")
        v.addWidget(self.output)
        return card

    def _build_export_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.export_txt_btn = QPushButton("Export .txt")
        self.export_txt_btn.clicked.connect(self.export_txt)
        self.export_txt_btn.setEnabled(False)
        self.export_srt_btn = QPushButton("Export .srt")
        self.export_srt_btn.clicked.connect(self.export_srt)
        self.export_srt_btn.setEnabled(False)
        row.addWidget(self.export_txt_btn)
        row.addWidget(self.export_srt_btn)
        row.addStretch(1)
        return row

    # --- drag & drop ---------------------------------------------------

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            path = Path(event.mimeData().urls()[0].toLocalFile())
            if path.suffix.lower() in MEDIA_EXTENSIONS:
                self.drop_zone.setObjectName("dropZoneActive")
                self.drop_zone.style().unpolish(self.drop_zone)
                self.drop_zone.style().polish(self.drop_zone)
                event.acceptProposedAction()
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self.drop_zone.setObjectName("dropZone")
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)

    def dropEvent(self, event):
        self.drop_zone.setObjectName("dropZone")
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)
        path = Path(event.mimeData().urls()[0].toLocalFile())
        self._set_media_path(str(path))
        event.acceptProposedAction()

    # --- hotkey status ---------------------------------------------------

    def _on_hotkey_status(self, status: str):
        if status == "Idle":
            self.hotkey_status_label.setText("")
        else:
            self.hotkey_status_label.setText(f"⇧ {status}")

    # --- actions ---------------------------------------------------

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
        self.hotkey_changed.emit()

    def _on_formatting_toggled(self, checked: bool):
        config.update(formatting_enabled=checked)
        if checked:
            # Without this, the *first* real use after flipping the toggle
            # mid-session pays the full ~5-10s llama-server cold-start cost
            # — warm_up() at launch only covers formatting already being on
            # when the app started.
            threading.Thread(target=self.formatter.warm_up, daemon=True).start()

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
        self.update_transcribe_enabled()

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose audio or video file",
            "",
            "Media files (*.mp3 *.wav *.m4a *.mp4 *.mov *.mkv *.flac *.aac);;All files (*)",
        )
        if path:
            self._set_media_path(path)

    def _set_media_path(self, path: str):
        self.media_path = path
        self.file_label.setText(Path(path).name)
        self.file_label.setStyleSheet("color: #e8e8ea;")
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
        self.export_txt_btn.setEnabled(False)
        self.export_srt_btn.setEnabled(False)
        self.output.clear()
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

    def on_finished(self, text: str, segments):
        self.progress_bar.hide()
        self.status_label.setText("Done.")
        self.output.setPlainText(text)
        self.last_segments = segments
        self.export_txt_btn.setEnabled(True)
        self.export_srt_btn.setEnabled(bool(segments))
        self.transcribe_btn.setEnabled(True)
        self.open_btn.setEnabled(True)

    def on_failed(self, message: str):
        self.progress_bar.hide()
        self.status_label.setText("Failed.")
        self.transcribe_btn.setEnabled(True)
        self.open_btn.setEnabled(True)
        QMessageBox.critical(self, "Transcription failed", message)

    def export_txt(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save transcript", "transcript.txt", "Text files (*.txt)")
        if path:
            Path(path).write_text(self.output.toPlainText(), encoding="utf-8")

    def export_srt(self):
        if not self.last_segments:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save subtitles", "transcript.srt", "SubRip files (*.srt)")
        if path:
            Path(path).write_text(segments_to_srt(self.last_segments), encoding="utf-8")
