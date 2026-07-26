"""Global push-to-talk: hold Right Option to record. Audio is fed to a
streaming-capable model live (as you speak, not after you release) via
transcribe.cpp's Stream API, so the post-release wait is just the last
fraction of a second of audio, not the whole utterance. Release to finalize,
optionally clean up with the local formatter, and paste at the cursor.
"""

import logging
import threading
import traceback

from PyQt6.QtCore import QObject, pyqtSignal
from pynput import keyboard

from . import config
from . import paste_action
from .audio_capture import StreamingMicRecorder
from .transcription_service import streaming_service

logger = logging.getLogger("chatter.hotkey")


class PushToTalkController(QObject):
    status_changed = pyqtSignal(str)
    live_text_changed = pyqtSignal(str)
    result_ready = pyqtSignal(str, bool)  # (text, was_auto_pasted)
    error = pyqtSignal(str)

    def __init__(self, get_streaming_model_path, formatter):
        super().__init__()
        self._get_streaming_model_path = get_streaming_model_path
        self._formatter = formatter
        self._recorder = StreamingMicRecorder()
        self._recording = False
        self._listener = None
        self._stream = None
        self._feeder_thread = None

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

        model_path = self._get_streaming_model_path()
        if not model_path:
            self.error.emit(
                "No streaming model configured — set streaming_model_path in "
                "Chatter's config.json to a model with supports_streaming=True."
            )
            return

        try:
            cfg = config.load()
            self._stream = streaming_service.open_stream(model_path, cfg["backend"])
        except Exception:
            logger.exception("failed to open stream")
            self.error.emit("Couldn't start the streaming model — see chatter.log.")
            self._stream = None
            return

        self._recording = True
        self._recorder.start()
        logger.info("recording started (streaming)")
        self.status_changed.emit("Listening…")
        self._feeder_thread = threading.Thread(target=self._feed_loop, daemon=True)
        self._feeder_thread.start()

    def _feed_loop(self):
        try:
            for chunk in self._recorder.chunks():
                update = self._stream.feed(chunk)
                if update.result_changed:
                    text = self._stream.text()
                    live = (text.committed + text.tentative).strip()
                    if live:
                        self.live_text_changed.emit(live)
        except Exception:
            logger.exception("stream feed loop failed")

    def _on_release(self, key):
        if key != keyboard.Key.alt_r or not self._recording:
            return
        self._recording = False
        self._recorder.stop()  # queues a sentinel; feed loop drains and exits
        logger.info("recording stopped")
        self.status_changed.emit("Transcribing…")
        threading.Thread(target=self._finish, daemon=True).start()

    def _finish(self):
        stream = self._stream
        try:
            if self._feeder_thread is not None:
                self._feeder_thread.join(timeout=5)

            stream.finalize()
            result = stream.text()
            text = (result.committed + result.tentative).strip() or result.full.strip()
            logger.info("finalized: %r", text)

            if not text:
                self.error.emit("No speech detected.")
                return

            cfg = config.load()
            if cfg["formatting_enabled"]:
                self.status_changed.emit("Cleaning up…")
                text = self._formatter.format_transcript(text)
                logger.info("formatted: %r", text)

            pasted = paste_action.paste(text)
            logger.info("paste_action.paste -> auto-pasted=%s", pasted)
            self.result_ready.emit(text, pasted)
        except Exception:
            logger.exception("push-to-talk pipeline failed")
            self.error.emit(traceback.format_exc())
        finally:
            try:
                stream.reset()
            except Exception:
                pass
            self._stream = None
            self._feeder_thread = None
            self.status_changed.emit("Idle")
