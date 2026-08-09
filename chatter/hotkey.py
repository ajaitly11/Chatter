"""Global push-to-talk: one local streaming ASR model records, transcribes,
and finalizes the utterance; the optional local language model only formats
that text before it is pasted at the cursor.
"""

import logging
import threading
import time
import traceback
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QObject, Qt, pyqtSignal

from . import config
from . import dictionary
from . import history
from . import insights
from . import paste_action
from . import sound
from .audio_capture import SAMPLE_RATE, StreamingMicRecorder
from .context import CaptureContext, current_context
from .native_hotkey import RawKeyListener
from .formatter import LiveCleanupCoordinator
from .transcription_service import streaming_service

logger = logging.getLogger("chatter.hotkey")

# Releasing the hold key lands right on top of (or a beat before) the last
# syllable, and closing the mic stream immediately on key-up doesn't leave
# the audio driver any room to hand over that last bit of audio — so the
# recording keeps running for a short grace period after key-up before the
# stream actually closes.
RELEASE_GRACE_SECONDS = 0.4

# Frames quieter than this (RMS) are treated as silence, both for trimming
# the clip's edges and for deciding whether anything was said at all. Keep
# this below the Settings microphone-test cutoff so a quiet, real voice is
    # not rejected before the streaming ASR gets a chance to recognize it.
# Device levels vary substantially (especially Bluetooth/Continuity mics).
# 0.006 rejected some real speech before the model ever saw it; keep the
# threshold low enough to accept a quiet mic, while still rejecting a truly
# silent press. The streaming model remains the authority on recognition.
_SILENCE_RMS_THRESHOLD = 0.0035
_SILENCE_FRAME_MS = 30
_SILENCE_MARGIN_MS = 200


def _trim_silence(pcm: np.ndarray, sample_rate: int) -> np.ndarray | None:
    """Chops leading/trailing near-silence off the recorded clip, and
    returns None if the *entire* clip is silence.

    Streaming ASR models can emit a confident-looking phrase on silence.
    Deciding that upfront from simple audio energy is what stops an empty
    press from pasting hallucinated text.
    """
    if pcm.size == 0:
        logger.warning("silence check: recording was empty (0 samples captured)")
        return None
    frame_len = max(1, int(sample_rate * _SILENCE_FRAME_MS / 1000))
    n_frames = len(pcm) // frame_len
    if n_frames == 0:
        return None
    frames = pcm[: n_frames * frame_len].reshape(n_frames, frame_len)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    peak_rms = float(rms.max())
    loud = np.flatnonzero(rms >= _SILENCE_RMS_THRESHOLD)
    if loud.size == 0:
        # Logged at WARNING (not the usual INFO) whenever the discarded
        # clip was long enough to plausibly contain real speech — a quick
        # tap that's genuinely silent is expected and not worth flagging,
        # but a multi-second hold failing this check is exactly the "I was
        # talking and it said no speech" symptom, and peak_rms tells you
        # whether the mic captured near-total silence (real no-speech, or a
        # stale/disconnected input device — see audio_capture.py's device
        # log line right before this) or just fell short of the threshold.
        log = logger.warning if len(pcm) / sample_rate > 2.0 else logger.info
        log("silence check: peak_rms=%.5f threshold=%.5f over %.2fs — treating as silence",
            peak_rms, _SILENCE_RMS_THRESHOLD, len(pcm) / sample_rate)
        return None
    margin_frames = max(1, int(_SILENCE_MARGIN_MS / _SILENCE_FRAME_MS))
    start = max(0, (loud[0] - margin_frames) * frame_len)
    end = min(len(pcm), (loud[-1] + margin_frames + 1) * frame_len)
    return pcm[start:end]


def _peak_frame_rms(pcm: np.ndarray, sample_rate: int) -> float:
    """Return the loudest short-time RMS level for diagnostics and gating."""
    if pcm.size == 0:
        return 0.0
    frame_len = max(1, int(sample_rate * _SILENCE_FRAME_MS / 1000))
    n_frames = len(pcm) // frame_len
    if n_frames == 0:
        return float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
    frames = pcm[: n_frames * frame_len].reshape(n_frames, frame_len)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    return float(rms.max())


class PushToTalkController(QObject):
    _key_down = pyqtSignal()
    _key_up = pyqtSignal()
    status_changed = pyqtSignal(str)
    partial_changed = pyqtSignal(str)
    result_ready = pyqtSignal(str, bool)  # (text, was_auto_pasted)
    error = pyqtSignal(str)

    def __init__(self, get_streaming_model_path, formatter):
        super().__init__()
        self._get_streaming_model_path = get_streaming_model_path
        self._formatter = formatter
        self._recorder = StreamingMicRecorder()
        self._recording = False
        # True from the moment a hold is released until _finish() (which can
        # run for several seconds during transcription/AI formatting) is
        # fully done. Without this, a quick re-press mid-processing could
        # reset shared state out from under the still-running _finish() call.
        self._processing = False
        self._listener = None

        self._backend = None
        self._raw_chunks: list[np.ndarray] = []
        self._collector_thread = None
        self._streaming_active = False
        self._streaming_error = None
        self._stream_final_text = ""
        self._stream_speech_started = False
        self._press_started_at = None
        self._first_preview_logged = False
        self._capture_context: CaptureContext | None = None
        self._live_cleanup = LiveCleanupCoordinator(formatter, self._on_live_cleanup)
        self._key_down.connect(self._on_press, Qt.ConnectionType.QueuedConnection)
        self._key_up.connect(self._on_release, Qt.ConnectionType.QueuedConnection)

    def start(self):
        keycode = config.load().get("hotkey_keycode", 60)
        self._listener = RawKeyListener(
            keycode, self._enqueue_key_down, self._enqueue_key_up, self._enqueue_listener_error,
        )
        self._listener.start()
        logger.info("push-to-talk listener started")

    def _enqueue_key_down(self):
        self._key_down.emit()

    def _enqueue_key_up(self):
        self._key_up.emit()

    def _enqueue_listener_error(self, message: str):
        self.error.emit(message)

    def stop(self):
        if self._recording:
            self._recording = False
            try:
                self._recorder.stop()
            except Exception:
                logger.exception("failed to stop microphone during shutdown")
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
            logger.info("push-to-talk listener stopped")

    def shutdown(self):
        self.stop()
        self._live_cleanup.shutdown()

    @property
    def listener_running(self) -> bool:
        """Whether the global hotkey listener has been started."""
        return self._listener is not None and self._listener.running

    def _on_press(self):
        if self._recording:
            return
        if self._processing:
            logger.info("press ignored — still finishing the previous utterance")
            self.status_changed.emit("Still finishing up…")
            return

        cfg = config.load()
        # Snapshot the destination context before Chatter shows its HUD or
        # starts any background work. The optional cleanup model sees only
        # the foreground app/window hint, never document contents.
        self._capture_context = current_context()
        context_override = cfg.get("cleanup_context_mode", "auto")
        if context_override and context_override != "auto":
            self._capture_context = self._capture_context.with_mode(context_override)
        logger.info(
            "dictation context: app=%r mode=%s window=%r",
            self._capture_context.app_name,
            self._capture_context.mode,
            self._capture_context.window_title,
        )
        self._backend = cfg.get("backend", "auto")
        self._raw_chunks = []
        self._streaming_active = False
        self._streaming_error = None
        self._stream_final_text = ""
        self._stream_speech_started = False
        self._press_started_at = time.perf_counter()
        self._first_preview_logged = False
        self._live_cleanup.reset()
        self._recording = True
        # Play before emitting the status signal. The HUD's first geometry
        # calculation can take a noticeable fraction of a second; putting
        # the cue after that made press feedback arrive late or appear to be
        # missing, while the release cue (which runs before any HUD update)
        # remained audible.
        sound.play_start()
        # Emit the visual state before opening PortAudio so a slow or broken
        # device cannot make the hotkey feel dead.
        self.status_changed.emit("Listening…")
        try:
            self._recorder.start(device=cfg.get("input_device", ""))
        except Exception as exc:
            self._recording = False
            logger.exception("couldn't open microphone")
            device_name = cfg.get("input_device", "") or "macOS system default"
            self.error.emit(f"Microphone unavailable ({device_name}): {exc}")
            return
        logger.info("recording started")
        self._collector_thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._collector_thread.start()

    def _collect_loop(self):
        try:
            streaming_model_path = self._get_streaming_model_path()
            if streaming_model_path:
                try:
                    streaming_language = config.load().get("language", "en") or None
                    stream_kwargs = {"language": streaming_language}
                    model_name = Path(streaming_model_path).name.lower()
                    import transcribe_cpp
                    if "nemotron-3.5" in model_name:
                        # The new multilingual checkpoint uses locale tags;
                        # the low-latency English default is en-US.
                        stream_kwargs["language"] = (
                            "en-US" if streaming_language == "en"
                            else (streaming_language or "auto")
                        )
                        stream_kwargs["family"] = transcribe_cpp.ParakeetStreamOptions(
                            att_context_right=3
                        )
                    elif "nemotron" in model_name or "parakeet" in model_name:
                        # The older English checkpoint was trained with a
                        # different lookahead menu; zero is its fastest valid
                        # setting.
                        stream_kwargs["language"] = streaming_language or "en"
                        stream_kwargs["family"] = transcribe_cpp.ParakeetStreamOptions(
                            att_context_right=0
                        )
                    streaming_service.start(
                        streaming_model_path, self._backend, **stream_kwargs
                    )
                    self._streaming_active = True
                    logger.info("streaming transcription started with %s", streaming_model_path)
                except Exception as exc:
                    self._streaming_error = str(exc)
                    logger.exception("streaming transcription unavailable")

            for chunk in self._recorder.chunks():
                self._raw_chunks.append(chunk)
                if self._streaming_active:
                    chunk_rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
                    if chunk_rms >= _SILENCE_RMS_THRESHOLD:
                        self._stream_speech_started = True
                    update, text = streaming_service.feed(chunk)
                    if (
                        self._stream_speech_started
                        and update is not None
                        and update.result_changed
                        and text
                        and text != self._stream_final_text
                    ):
                        text = dictionary.normalize_word_boundaries(text)
                        self._stream_final_text = text
                        if not self._first_preview_logged and self._press_started_at is not None:
                            logger.info(
                                "latency to first streaming preview: %.0fms",
                                (time.perf_counter() - self._press_started_at) * 1000,
                            )
                            self._first_preview_logged = True
                        self.partial_changed.emit(text)
                        if config.load().get("formatting_enabled", False):
                            self._live_cleanup.submit(text, context=self._capture_context)

            if self._streaming_active:
                final_text = streaming_service.finalize(trailing_silence_ms=240)
                # The frame-based silence check below is more sensitive than
                # the 200ms streaming chunk gate. If a quiet microphone has
                # real speech energy but never crosses the chunk gate, keep
                # the model's final text instead of dropping it.
                if final_text and (self._stream_speech_started or final_text.strip()):
                    final_text = dictionary.normalize_word_boundaries(final_text)
                    self._stream_final_text = final_text
                    self.partial_changed.emit(final_text)
                    if config.load().get("formatting_enabled", False):
                        self._live_cleanup.submit(final_text, context=self._capture_context)
                self._streaming_active = False
        except Exception:
            logger.exception("audio collection loop failed")
            self._streaming_active = False
            try:
                streaming_service.reset()
            except Exception:
                logger.exception("failed to reset streaming session")

    def _on_release(self):
        if not self._recording:
            return
        self._recording = False
        self._processing = True
        sound.play_stop()
        if self._press_started_at is not None:
            logger.info(
                "hotkey hold duration before release: %.0fms",
                (time.perf_counter() - self._press_started_at) * 1000,
            )
        logger.info("recording released — keeping mic open for a %.1fs grace period", RELEASE_GRACE_SECONDS)
        self.status_changed.emit("Finishing audio…")
        # Stop the mic (and thus the collector loop, via the sentinel it
        # queues) after the grace period rather than immediately, so
        # _finish's join() below waits for that trailing bit of audio
        # instead of missing it.
        threading.Timer(RELEASE_GRACE_SECONDS, self._recorder.stop).start()
        threading.Thread(target=self._finish, daemon=True).start()

    def _finish(self):
        processing_started_at = time.perf_counter()
        try:
            if self._collector_thread is not None:
                # No timeout: a truncated wait here means silently dropped
                # audio.
                self._collector_thread.join()

            raw = np.concatenate(self._raw_chunks) if self._raw_chunks else np.array([], dtype=np.float32)
            trimmed = _trim_silence(raw, SAMPLE_RATE)
            peak_rms = _peak_frame_rms(raw, SAMPLE_RATE)
            if trimmed is None:
                logger.info(
                    "recording was silence — skipping transcription: peak_rms=%.5f threshold=%.5f",
                    peak_rms, _SILENCE_RMS_THRESHOLD,
                )
                self.error.emit("No speech detected — check the selected microphone.")
                return
            logger.info(
                "%.2fs captured audio passed through the streaming ASR: peak_rms=%.5f threshold=%.5f speech_started=%s",
                len(raw) / SAMPLE_RATE, peak_rms, _SILENCE_RMS_THRESHOLD,
                self._stream_speech_started,
            )

            self.status_changed.emit("Finalizing…")
            text = self._stream_final_text.strip()
            logger.info("transcribed: %r", text)
            if not text:
                detail = self._streaming_error or (
                    "the streaming model returned no text "
                    f"(peak mic level {peak_rms:.4f})"
                )
                self.error.emit(f"No transcript available — {detail}.")
                return

            text = dictionary.apply_corrections(text)
            text = dictionary.normalize_word_boundaries(text)
            context = self._capture_context
            # Surface the accurate raw result immediately. If the optional
            # local cleanup pass is enabled, the next state tells the user
            # that the visible text is being polished rather than leaving a
            # blank UI while the local LLM runs.
            self.partial_changed.emit(text)
            cfg = config.load()
            if cfg["formatting_enabled"]:
                live_cleaned = self._live_cleanup.latest_for(text, context=context)
                if live_cleaned:
                    text = live_cleaned
                    logger.info("using parallel live cleanup result: %r", text)
                else:
                    self.status_changed.emit("Cleaning up…")
                    text = self._formatter.format_transcript(text, context=context)
                    logger.info("formatted: %r", text)
                self.partial_changed.emit(text)

            pasted = paste_action.paste(text)
            logger.info("paste_action.paste -> auto-pasted=%s", pasted)
            # Logged regardless of paste success — "even if it doesn't get
            # pasted" was the explicit ask, so a clipboard-only fallback
            # still needs to show up in the Live Dictation history.
            context_for_history = context or CaptureContext()
            history.append(
                "dictation",
                text,
                pasted=pasted,
                word_count=insights.count_words(text),
                audio_seconds=round(len(trimmed) / SAMPLE_RATE, 3),
                context_app=context_for_history.app_name,
                context_mode=context_for_history.mode,
                cleanup_applied=bool(cfg["formatting_enabled"]),
                processing_ms=round((time.perf_counter() - processing_started_at) * 1000),
            )
            self.result_ready.emit(text, pasted)
        except Exception:
            logger.exception("push-to-talk pipeline failed")
            self.error.emit(traceback.format_exc())
        finally:
            self._raw_chunks = []
            self._collector_thread = None
            self._capture_context = None
            self._processing = False
            self.status_changed.emit("Idle")

    def _on_live_cleanup(self, source: str, cleaned: str, generation: int):
        # The coordinator discards stale work, but the controller can also
        # have moved into finalization while the formatter was responding.
        # Never let a background cleanup replace the final text after paste.
        if not self._recording or self._processing:
            return
        logger.info("live cleanup preview: %r -> %r", source, cleaned)
        self.partial_changed.emit(cleaned)
