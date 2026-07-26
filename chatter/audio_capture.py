"""Microphone capture straight into the 16kHz mono float32 PCM transcribe.cpp expects."""

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000


class MicRecorder:
    def __init__(self):
        self._stream = None
        self._chunks: list[np.ndarray] = []

    def start(self):
        self._chunks = []
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self._on_audio,
        )
        self._stream.start()

    def _on_audio(self, indata, frames, time_info, status):
        self._chunks.append(indata[:, 0].copy())

    def stop(self) -> np.ndarray:
        if self._stream is None:
            return np.array([], dtype=np.float32)
        self._stream.stop()
        self._stream.close()
        self._stream = None
        if not self._chunks:
            return np.array([], dtype=np.float32)
        return np.concatenate(self._chunks)
