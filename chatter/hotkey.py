"""Global push-to-talk: hold the configured key (default Right Shift) to
record, release to transcribe with the batch model (Whisper by default) and
paste at the cursor.

Deliberately single-pass. An earlier version also streamed the recording
live through a second, streaming-capable model (Nemotron) to drive a
word-by-word live caption. That doubled GPU load for the entire time the key
was held and added visible lag before the pasted text arrived, in service of
a live caption that wasn't even what got pasted — the accurate output always
came from a second, separate Whisper pass on release regardless. Recording
is now just buffered raw audio; on release, the whole clip is decoded once,
after a short trailing grace period so the last syllable isn't clipped by
the mic stream closing right on key-up.
"""

import logging
import threading
import traceback
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from . import config
from . import dictionary
from . import history
from . import paste_action
from . import sound
from .audio_capture import SAMPLE_RATE, StreamingMicRecorder
from .native_hotkey import RawKeyListener
from .transcription_service import service

logger = logging.getLogger("chatter.hotkey")

# Releasing the hold key lands right on top of (or a beat before) the last
# syllable, and closing the mic stream immediately on key-up doesn't leave
# the audio driver any room to hand over that last bit of audio — so the
# recording keeps running for a short grace period after key-up before the
# stream actually closes.
RELEASE_GRACE_SECONDS = 0.4

# Frames quieter than this (RMS) are treated as silence, both for trimming
# the clip's edges and for deciding whether anything was said at all.
_SILENCE_RMS_THRESHOLD = 0.012
_SILENCE_FRAME_MS = 30
_SILENCE_MARGIN_MS = 200


def _trim_silence(pcm: np.ndarray, sample_rate: int) -> np.ndarray | None:
    """Chops leading/trailing near-silence off the recorded clip, and
    returns None if the *entire* clip is silence.

    Whisper is trained on YouTube captions and has a well-documented
    tendency to hallucinate a confident, generic sign-off phrase ("Thank
    you.", "Thanks for watching!") when fed silence — it does not reliably
    self-report "no speech" the way its own no-speech threshold implies.
    Deciding that upfront from simple audio energy, rather than trusting
    Whisper's output for a clip we already know was silent, is what
    actually stops an empty press from pasting hallucinated text.
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


class PushToTalkController(QObject):
    status_changed = pyqtSignal(str)
    result_ready = pyqtSignal(str, bool)  # (text, was_auto_pasted)
    error = pyqtSignal(str)

    def __init__(self, get_batch_model_path, formatter):
        super().__init__()
        self._get_batch_model_path = get_batch_model_path
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

    def start(self):
        keycode = config.load().get("hotkey_keycode", 60)
        self._listener = RawKeyListener(keycode, self._on_press, self._on_release)
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

        self._backend = config.load()["backend"]
        self._raw_chunks = []
        self._recording = True
        self._recorder.start()
        sound.play_start()
        logger.info("recording started")
        self.status_changed.emit("Listening…")
        self._collector_thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._collector_thread.start()

    def _collect_loop(self):
        try:
            for chunk in self._recorder.chunks():
                self._raw_chunks.append(chunk)
        except Exception:
            logger.exception("audio collection loop failed")

    def _on_release(self):
        if not self._recording:
            return
        self._recording = False
        self._processing = True
        sound.play_stop()
        logger.info("recording released — keeping mic open for a %.1fs grace period", RELEASE_GRACE_SECONDS)
        self.status_changed.emit("Transcribing…")
        # Stop the mic (and thus the collector loop, via the sentinel it
        # queues) after the grace period rather than immediately, so
        # _finish's join() below waits for that trailing bit of audio
        # instead of missing it.
        threading.Timer(RELEASE_GRACE_SECONDS, self._recorder.stop).start()
        threading.Thread(target=self._finish, daemon=True).start()

    def _transcribe(self, pcm: np.ndarray) -> str:
        model_path = self._get_batch_model_path()
        if not model_path:
            self.error.emit(
                "No transcription model configured — set whisper_model_path in "
                "Chatter's config.json."
            )
            return ""
        run_kwargs = {}
        if "whisper" in Path(model_path).name.lower():
            import transcribe_cpp
            # Isolated few-second clips — no reason to condition decoding on
            # tokens from anything but this clip itself.
            run_kwargs["family"] = transcribe_cpp.WhisperRunOptions(condition_on_prev_tokens=False)
        result = service.transcribe(pcm, model_path, self._backend, **run_kwargs)
        return result.text.strip()

    def _finish(self):
        try:
            if self._collector_thread is not None:
                # No timeout: a truncated wait here means silently dropped
                # audio.
                self._collector_thread.join()

            raw = np.concatenate(self._raw_chunks) if self._raw_chunks else np.array([], dtype=np.float32)
            trimmed = _trim_silence(raw, SAMPLE_RATE)
            if trimmed is None:
                logger.info("recording was silence — skipping transcription")
                self.error.emit("No speech detected.")
                return
            logger.info(
                "%.2fs raw -> %.2fs after silence trim",
                len(raw) / SAMPLE_RATE, len(trimmed) / SAMPLE_RATE,
            )

            text = self._transcribe(trimmed)
            logger.info("transcribed: %r", text)
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
            # Logged regardless of paste success — "even if it doesn't get
            # pasted" was the explicit ask, so a clipboard-only fallback
            # still needs to show up in the Live Dictation history.
            history.append("dictation", text, pasted=pasted)
            self.result_ready.emit(text, pasted)
        except Exception:
            logger.exception("push-to-talk pipeline failed")
            self.error.emit(traceback.format_exc())
        finally:
            self._raw_chunks = []
            self._collector_thread = None
            self._processing = False
            self.status_changed.emit("Idle")
