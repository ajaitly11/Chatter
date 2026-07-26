"""Microphone capture straight into the 16kHz mono float32 PCM transcribe.cpp
expects, delivered as a stream of small chunks so push-to-talk can feed a
streaming model in near-real-time instead of waiting for the whole recording.
"""

import queue

import sounddevice as sd

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
