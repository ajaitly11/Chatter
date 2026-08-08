"""Microphone capture straight into the 16kHz mono float32 PCM transcribe.cpp
expects, delivered as a stream of small chunks so push-to-talk can feed a
streaming model in near-real-time instead of waiting for the whole recording.
"""

import logging
import queue

import sounddevice as sd

logger = logging.getLogger("chatter.audio_capture")

SAMPLE_RATE = 16000
# Larger chunks amortize per-feed()-call overhead much better — measured
# ~0.36 real-time-factor at 100ms chunks vs. ~0.16 at 500ms on this model.
CHUNK_MS = 500


class StreamingMicRecorder:
    def __init__(self, chunk_ms: int = CHUNK_MS):
        self._blocksize = int(SAMPLE_RATE * chunk_ms / 1000)
        self._queue: queue.Queue = queue.Queue()
        self._stream = None

    def start(self):
        self._queue = queue.Queue()
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=self._blocksize,
            callback=self._on_audio,
        )
        self._stream.start()
        # Cheap diagnostic for the "recording was marked as silence but I was
        # definitely talking" case — if the device it actually opened isn't
        # the one you'd expect (e.g. a stale/virtual device after a rapid
        # start/stop cycle), this is the first thing to check in the log.
        try:
            dev = sd.query_devices(self._stream.device)
            logger.info("mic stream opened: device=%r samplerate=%s", dev.get("name"), self._stream.samplerate)
        except Exception:
            logger.exception("couldn't query opened input device name")

    def _on_audio(self, indata, frames, time_info, status):
        self._queue.put(indata[:, 0].copy())

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
