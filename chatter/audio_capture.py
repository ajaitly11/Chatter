"""Microphone capture straight into the 16kHz mono float32 PCM transcribe.cpp
expects, delivered as a stream of small chunks so push-to-talk can feed a
streaming model in near-real-time instead of waiting for the whole recording.
"""

import logging
import queue

import numpy as np
import sounddevice as sd

from . import permissions

logger = logging.getLogger("chatter.audio_capture")

SAMPLE_RATE = 16000
# Keep chunks short enough for responsive streaming updates. The model still
# does its own decode buffering; this is the audio-to-engine handoff size.
CHUNK_MS = 200


def _resample_to_asr_rate(chunk: np.ndarray, capture_rate: float) -> np.ndarray:
    """Convert one native-rate mono chunk to transcribe.cpp's 16 kHz PCM."""
    if abs(capture_rate - SAMPLE_RATE) <= 1 or chunk.size == 0:
        return chunk.astype(np.float32, copy=False)
    target_length = max(1, int(round(len(chunk) * SAMPLE_RATE / capture_rate)))
    source_x = np.linspace(0.0, 1.0, num=len(chunk), endpoint=False)
    target_x = np.linspace(0.0, 1.0, num=target_length, endpoint=False)
    return np.interp(target_x, source_x, chunk).astype(np.float32)


class StreamingMicRecorder:
    def __init__(self, chunk_ms: int = CHUNK_MS, device=None):
        self._chunk_ms = chunk_ms
        self._blocksize = int(SAMPLE_RATE * chunk_ms / 1000)
        self._capture_rate = SAMPLE_RATE
        self._device = device
        self._queue: queue.Queue = queue.Queue()
        self._stream = None

    def start(self, device=None):
        self._queue = queue.Queue()
        microphone_status = permissions.microphone_authorization_status()
        logger.info("macOS microphone authorization: %s", microphone_status)
        if microphone_status != "authorized":
            raise PermissionError(
                "Chatter does not have microphone access "
                f"(macOS status: {microphone_status}). Enable Chatter in "
                "System Settings > Privacy & Security > Microphone."
            )
        selected_device = self._device if device is None else device
        portaudio_device = selected_device if selected_device not in ("", None) else None
        candidates = [portaudio_device]
        if portaudio_device is not None:
            # A named Continuity/Bluetooth device can disappear between the
            # Settings list and the key press. Try the macOS default before
            # reporting a hard failure, which gives the user a useful
            # fallback without silently changing their saved preference.
            candidates.append(None)

        last_error = None
        for candidate in candidates:
            try:
                info = sd.query_devices(candidate, "input")
                native_rate = float(info.get("default_samplerate") or SAMPLE_RATE)
                if native_rate <= 0:
                    native_rate = SAMPLE_RATE
                self._capture_rate = native_rate
                self._blocksize = max(1, int(round(native_rate * self._chunk_ms / 1000)))
                self._stream = sd.InputStream(
                    # CoreAudio devices commonly expose 48 kHz as their
                    # native rate. Opening at that rate avoids PortAudio's
                    # intermittent -9986 internal error; _on_audio converts
                    # the small chunks back to the 16 kHz ASR format.
                    samplerate=native_rate,
                    channels=1,
                    dtype="float32",
                    blocksize=self._blocksize,
                    device=candidate,
                    callback=self._on_audio,
                )
                self._stream.start()
                if candidate != portaudio_device:
                    logger.warning(
                        "selected microphone %r could not be opened; using macOS system default",
                        portaudio_device,
                    )
                break
            except Exception as exc:
                last_error = exc
                self._stream = None
                logger.warning("couldn't open microphone candidate %r: %s", candidate, exc)
        else:
            raise last_error

        # Cheap diagnostic for the "recording was marked as silence but I was
        # definitely talking" case — if the device it actually opened isn't
        # the one you'd expect (e.g. a stale/virtual device after a rapid
        # start/stop cycle), this is the first thing to check in the log.
        try:
            dev = sd.query_devices(self._stream.device)
            logger.info(
                "mic stream opened: device=%r capture_samplerate=%s asr_samplerate=%s blocksize=%s",
                dev.get("name"), self._capture_rate, SAMPLE_RATE, self._blocksize,
            )
        except Exception:
            logger.exception("couldn't query opened input device name")

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            logger.warning("mic callback status: %s", status)
        chunk = indata[:, 0].copy()
        self._queue.put(_resample_to_asr_rate(chunk, self._capture_rate))

    def stop(self):
        """Stops capturing and signals chunks() to end once the queue drains."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._queue.put(None)  # sentinel

    def chunks(self):
        """Yields captured chunks in order; ends after stop()'s sentinel is drained."""
        while True:
            chunk = self._queue.get()
            if chunk is None:
                return
            yield chunk
