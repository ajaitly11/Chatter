"""Shared transcription core: model discovery, audio decoding, and a persistent
Model+Session so repeat calls (especially push-to-talk) don't pay a multi-second
model-load cost every time.
"""

import subprocess
import threading
from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).parent.parent / "models"


def list_models() -> list[Path]:
    if not MODELS_DIR.exists():
        return []
    return sorted(MODELS_DIR.glob("*.gguf"))


def decode_to_pcm(input_path: str) -> np.ndarray:
    """ffmpeg -> 16kHz mono float32 PCM, the format transcribe.cpp expects."""
    cmd = [
        "ffmpeg",
        "-i", input_path,
        "-f", "f32le",
        "-ac", "1",
        "-ar", "16000",
        "-loglevel", "error",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr.decode(errors='ignore')}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def format_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments) -> str:
    lines = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}")
        lines.append(seg.text.strip())
        lines.append("")
    return "\n".join(lines)


class TranscriptionService:
    """Lazily loads one Model + Session and reuses it for every call.

    transcribe.cpp serializes one run per session, so a lock guards session.run()
    against overlapping calls from the file-open flow and push-to-talk.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._session = None
        self._model_path = None
        self._backend = None

    def _ensure_session(self, model_path: str, backend: str):
        if self._session is not None and self._model_path == model_path and self._backend == backend:
            return
        self.close()
        import transcribe_cpp

        self._model = transcribe_cpp.Model(model_path, backend=backend)
        self._model.__enter__()
        self._session = self._model.session()
        self._session.__enter__()
        self._model_path = model_path
        self._backend = backend

    def transcribe(self, pcm: np.ndarray, model_path: str, backend: str):
        with self._lock:
            self._ensure_session(model_path, backend)
            return self._session.run(pcm)

    def open_stream(self, model_path: str, backend: str):
        """Returns a live Stream for incremental feed()/text()/finalize() calls.
        Only the lock-guarded session setup happens here — feed() calls happen
        outside the lock over the recording's lifetime, so this is meant for a
        single dedicated-purpose service instance (see `streaming_service`
        below), not one shared with concurrent batch transcribe() calls.
        """
        with self._lock:
            self._ensure_session(model_path, backend)
            return self._session.stream()

    def close(self):
        if self._session is not None:
            self._session.__exit__(None, None, None)
            self._session = None
        if self._model is not None:
            self._model.__exit__(None, None, None)
            self._model = None
        self._model_path = None
        self._backend = None


service = TranscriptionService()
# Separate persistent Model+Session dedicated to push-to-talk streaming, since
# it typically uses a different (streaming-capable) model than file transcription.
streaming_service = TranscriptionService()
