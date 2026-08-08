"""Shared transcription core: model discovery, audio decoding, and a persistent
Model+Session so repeat calls (especially push-to-talk) don't pay a multi-second
model-load cost every time.
"""

import subprocess
import threading
from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).parent.parent / "models"
CLEANUP_MODELS_DIR = MODELS_DIR / "cleanup"


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


def _field(w, name: str):
    """Words come either as live transcribe_cpp.Word objects (fresh
    transcription) or as plain dicts (re-loaded from history.py's JSONL
    log, which can't store native Word objects) — this reads either."""
    return w[name] if isinstance(w, dict) else getattr(w, name)


def words_to_srt(words) -> str:
    """One SRT cue per word, using transcribe.cpp's per-word timestamps
    (Result.words) rather than per-segment/phrase timestamps."""
    lines = []
    for i, w in enumerate(words, start=1):
        lines.append(str(i))
        start_s = _field(w, "t0_ms") / 1000
        end_s = _field(w, "t1_ms") / 1000
        lines.append(f"{format_timestamp(start_s)} --> {format_timestamp(end_s)}")
        lines.append(_field(w, "text").strip())
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

    def transcribe(self, pcm: np.ndarray, model_path: str, backend: str, **run_kwargs):
        with self._lock:
            self._ensure_session(model_path, backend)
            return self._session.run(pcm, **run_kwargs)

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
