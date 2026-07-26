"""Global push-to-talk: hold Right Option to record, release to transcribe,
optionally clean up with the local formatter, and paste at the cursor.
"""

import logging
import threading
import traceback

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from pynput import keyboard

from . import config
from . import paste_action
from .audio_capture import MicRecorder
from .transcription_service import service

logger = logging.getLogger("chatter.hotkey")

SILENCE_AMPLITUDE = 0.005  # below this peak amplitude, treat as "no speech"


class PushToTalkController(QObject):
    status_changed = pyqtSignal(str)
    result_ready = pyqtSignal(str, bool)  # (text, was_auto_pasted)
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
        logger.info("push-to-talk listener started")

    def stop(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
            logger.info("push-to-talk listener stopped")

    def _on_press(self, key):
        if key != keyboard.Key.alt_r or self._recording:
            return
        self._recording = True
        self._recorder.start()
        logger.info("recording started")
        self.status_changed.emit("Listening…")

    def _on_release(self, key):
        if key != keyboard.Key.alt_r or not self._recording:
            return
        self._recording = False
        pcm = self._recorder.stop()
        logger.info("recording stopped: %d samples, peak amplitude %.4f",
                     pcm.size, float(np.abs(pcm).max()) if pcm.size else 0.0)
        self.status_changed.emit("Transcribing…")
        threading.Thread(target=self._process, args=(pcm,), daemon=True).start()

    def _process(self, pcm):
        try:
            if pcm.size < 1600:  # under ~0.1s, likely an accidental tap
                logger.info("skipped: recording too short")
                self.status_changed.emit("Idle")
                return

            if np.abs(pcm).max() < SILENCE_AMPLITUDE:
                logger.warning("skipped: recording is silence (check mic permission)")
                self.error.emit(
                    "No audio detected — check that Chatter has Microphone "
                    "permission in System Settings > Privacy & Security."
                )
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
            logger.info("transcribed: %r", text)

            if cfg["formatting_enabled"] and text:
                self.status_changed.emit("Cleaning up…")
                text = self._formatter.format_transcript(text)
                logger.info("formatted: %r", text)

            if text:
                pasted = paste_action.paste(text)
                logger.info("paste_action.paste -> auto-pasted=%s", pasted)
                self.result_ready.emit(text, pasted)
            else:
                logger.warning("empty transcript, nothing to paste")
                self.error.emit("Transcription came back empty.")
        except Exception:
            logger.exception("push-to-talk pipeline failed")
            self.error.emit(traceback.format_exc())
        finally:
            self.status_changed.emit("Idle")
