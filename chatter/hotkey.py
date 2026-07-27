"""Global push-to-talk: hold Right Option to record. Audio is fed to a
streaming-capable model live (as you speak, not after you release) via
transcribe.cpp's Stream API. Per-chunk cost grows with how much audio a
stream has accumulated, so long holds periodically finalize and reopen a
fresh stream (a new "segment") to keep processing bounded — segment texts
are joined together for the final result. Release finalizes the last
segment, optionally cleans up with the local formatter, and pastes at the
cursor. The feeder thread is always drained in full before finalizing —
never abandoned on a timeout — so no audio is silently dropped.
"""

import logging
import re
import threading
import traceback

from PyQt6.QtCore import QObject, pyqtSignal

from . import config
from . import dictionary
from . import paste_action
from . import sound
from .audio_capture import SAMPLE_RATE, StreamingMicRecorder
from .native_hotkey import RIGHT_OPTION_KEYCODE, RawKeyListener
from .transcription_service import streaming_service

logger = logging.getLogger("chatter.hotkey")

SEGMENT_SAMPLES = SAMPLE_RATE * 10  # reopen the stream every ~10s of audio
_WORD_RE = re.compile(r"[a-z0-9']+")


def _join_segments(segments: list[str], max_overlap_words: int = 8) -> str:
    """Joins segment texts, trimming an exact duplicated run of words where
    one segment repeats the tail of the previous one — a byproduct of
    finalizing/reopening the stream mid-utterance. Only catches exact
    (case/punctuation-insensitive) repeats; anything fuzzier is left for the
    AI cleanup pass, which can catch it semantically.
    """
    if not segments:
        return ""
    joined = segments[0].strip()
    for seg in segments[1:]:
        prev_words = _WORD_RE.findall(joined.lower())
        seg_words_raw = seg.split()
        seg_words_norm = _WORD_RE.findall(seg.lower())
        limit = min(max_overlap_words, len(prev_words), len(seg_words_norm))
        trimmed = seg
        for k in range(limit, 0, -1):
            if prev_words[-k:] == seg_words_norm[:k]:
                trimmed = " ".join(seg_words_raw[k:])
                break
        joined = f"{joined.strip()} {trimmed.strip()}".strip()
    return joined


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
        # True from the moment a hold is released until _finish() (which
        # touches self._segments / self._current_stream, possibly for
        # several seconds during AI formatting) is fully done. Without this,
        # a quick re-press mid-formatting could reset that shared state out
        # from under the still-running _finish() call — surfaced as AI
        # cleanup only ever returning the most recent short utterance.
        self._processing = False
        self._listener = None

        self._model_path = None
        self._backend = None
        self._current_stream = None
        self._segment_samples = 0
        self._segments: list[str] = []
        self._feeder_thread = None

    def start(self):
        self._listener = RawKeyListener(RIGHT_OPTION_KEYCODE, self._on_press, self._on_release)
        self._listener.start()
        logger.info("push-to-talk listener started")

    def stop(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
            logger.info("push-to-talk listener stopped")

    def _on_press(self):
        if self._recording:
            return
        if self._processing:
            logger.info("press ignored — still finishing the previous utterance")
            self.status_changed.emit("Still finishing up…")
            return

        model_path = self._get_streaming_model_path()
        if not model_path:
            self.error.emit(
                "No streaming model configured — set streaming_model_path in "
                "Chatter's config.json to a model with supports_streaming=True."
            )
            return

        cfg = config.load()
        try:
            self._model_path = model_path
            self._backend = cfg["backend"]
            self._current_stream = streaming_service.open_stream(model_path, self._backend)
        except Exception:
            logger.exception("failed to open stream")
            self.error.emit("Couldn't start the streaming model — see chatter.log.")
            self._current_stream = None
            return

        self._segments = []
        self._segment_samples = 0
        self._recording = True
        self._recorder.start()
        sound.play_start()
        logger.info("recording started (streaming)")
        self.status_changed.emit("Listening…")
        self._feeder_thread = threading.Thread(target=self._feed_loop, daemon=True)
        self._feeder_thread.start()

    def _live_preview(self, current_text: str) -> str:
        return _join_segments([*self._segments, current_text])

    def _feed_loop(self):
        try:
            for chunk in self._recorder.chunks():
                update = self._current_stream.feed(chunk)
                self._segment_samples += len(chunk)

                if update.result_changed:
                    current = self._current_stream.text().full.strip()
                    live = self._live_preview(current)
                    if live:
                        self.live_text_changed.emit(live)

                if self._segment_samples >= SEGMENT_SAMPLES:
                    self._roll_over_segment()
        except Exception:
            logger.exception("stream feed loop failed")

    def _roll_over_segment(self):
        """Finalizes the current stream and opens a fresh one, so a long
        hold doesn't keep growing one stream's context indefinitely."""
        text = self._finalize_current_stream()
        if text:
            self._segments.append(text)
        logger.info("segment rolled over: %r (%d total)", text, len(self._segments))
        self._current_stream = streaming_service.open_stream(self._model_path, self._backend)
        self._segment_samples = 0

    def _finalize_current_stream(self) -> str:
        stream = self._current_stream
        stream.finalize()
        # `full` is the raw model hypothesis and, empirically, the reliable
        # one here — after finalize() `tentative` gets wiped to empty while
        # `committed` can be left stuck on stale text, so `committed +
        # tentative` silently drops real content that only survives in `full`.
        text = stream.text().full.strip()
        try:
            stream.reset()
        except Exception:
            pass
        return text

    def _on_release(self):
        if not self._recording:
            return
        self._recording = False
        self._processing = True
        self._recorder.stop()  # queues a sentinel; feed loop drains and exits
        sound.play_stop()
        logger.info("recording stopped")
        self.status_changed.emit("Transcribing…")
        threading.Thread(target=self._finish, daemon=True).start()

    def _finish(self):
        try:
            if self._feeder_thread is not None:
                # No timeout: a truncated wait here means silently dropped
                # audio. Segmenting during _feed_loop keeps this bounded to
                # roughly one segment's worth of processing time.
                self._feeder_thread.join()

            final_segment = self._finalize_current_stream()
            if final_segment:
                self._segments.append(final_segment)
            text = _join_segments(self._segments)
            logger.info("finalized: %r", text)

            if not text:
                self.error.emit("No speech detected.")
                return

            text = dictionary.apply_corrections(text)
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
            self._current_stream = None
            self._feeder_thread = None
            self._segments = []
            self._processing = False
            self.status_changed.emit("Idle")
