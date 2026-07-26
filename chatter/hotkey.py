"""Global push-to-talk: hold Right Option to record, release to transcribe,
optionally clean up with the local formatter, and paste at the cursor.
"""

import threading
import traceback

from PyQt6.QtCore import QObject, pyqtSignal
from pynput import keyboard

from . import config
from . import paste_action
from .audio_capture import MicRecorder
from .transcription_service import service


class PushToTalkController(QObject):
    status_changed = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, get_model_path, formatter):
        super().__init__()
        self._get_model_path = get_model_path
        self._formatter = formatter
        self._recorder = MicRecorder()
        self._recording = False
        self._listener = None

    def start(self):
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.start()

    def stop(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key):
        if key != keyboard.Key.alt_r or self._recording:
            return
        self._recording = True
        self._recorder.start()
        self.status_changed.emit("Listening…")

    def _on_release(self, key):
        if key != keyboard.Key.alt_r or not self._recording:
            return
        self._recording = False
        pcm = self._recorder.stop()
        self.status_changed.emit("Transcribing…")
        threading.Thread(target=self._process, args=(pcm,), daemon=True).start()

    def _process(self, pcm):
        try:
            if pcm.size < 1600:  # under ~0.1s, likely an accidental tap
                self.status_changed.emit("Idle")
                return

            cfg = config.load()
            model_path = self._get_model_path()
            if not model_path:
                self.error.emit("No model selected — open Chatter and pick a model first.")
                self.status_changed.emit("Idle")
                return

            result = service.transcribe(pcm, model_path, cfg["backend"])
            text = result.text.strip()

            if cfg["formatting_enabled"] and text:
                self.status_changed.emit("Cleaning up…")
                text = self._formatter.format_transcript(text)

            if text:
                paste_action.paste(text)
        except Exception:
            self.error.emit(traceback.format_exc())
        finally:
            self.status_changed.emit("Idle")
