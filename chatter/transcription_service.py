"""Shared transcription core: model discovery, audio decoding, and a persistent
Model+Session so repeat calls (especially push-to-talk) don't pay a multi-second
model-load cost every time.
"""

import subprocess
import os
import sys
import threading
from pathlib import Path

import numpy as np

def _models_dir() -> Path:
    """Find external GGUFs in both source checkouts and frozen app bundles."""
    configured = os.environ.get("CHATTER_PROJECT_DIR", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser() / "models")

    source_models = Path(__file__).resolve().parent.parent / "models"
    candidates.append(source_models)

    if getattr(sys, "frozen", False):
        # The packaged app creates this as a symlink to the user's model
        # directory. Keeping the large model files outside the bundle makes
        # app upgrades small while preserving a portable app layout.
        candidates.append(Path(sys.executable).resolve().parents[1] / "Resources" / "models")

    candidates.append(Path.cwd() / "models")
    return next((path for path in candidates if path.exists()), candidates[0])


MODELS_DIR = _models_dir()
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

    def warm_up(self, model_path: str | None, backend: str) -> bool:
        """Load the model and session ahead of the first real utterance.

        Model loading is deliberately kept out of the key-down path.  A
        warm-up failure is logged and returned to the caller; the normal
        transcription path still owns the final error message so startup can
        remain non-blocking.
        """
        if not model_path:
            return False
        try:
            with self._lock:
                self._ensure_session(model_path, backend)
            return True
        except Exception:
            return False

    def backend_name(self) -> str:
        with self._lock:
            if self._model is None:
                return "unloaded"
            return str(getattr(self._model, "backend", "unknown"))

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


class StreamingTranscriptionService:
    """Persistent local streaming model/session.

    Push-to-talk uses this one ASR session for committed/tentative text and
    the final transcript. ``service`` remains available for the separate file
    transcription workflow. ``transcribe.cpp`` 0.x allows one active stream
    per model, so this class serializes all stream mutations behind one lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._session = None
        self._stream = None
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

    def warm_up(self, model_path: str | None, backend: str) -> bool:
        if not model_path:
            return False
        try:
            with self._lock:
                self._ensure_session(model_path, backend)
            return True
        except Exception:
            return False

    def start(self, model_path: str, backend: str, **stream_kwargs):
        with self._lock:
            self._ensure_session(model_path, backend)
            if self._stream is not None:
                self._stream.reset()
                self._stream = None
            self._stream = self._session.stream(**stream_kwargs)
            self._stream.__enter__()

    def feed(self, pcm: np.ndarray):
        with self._lock:
            if self._stream is None:
                return None, ""
            update = self._stream.feed(pcm)
            return update, self._stream.text().display.strip()

    def finalize(self, trailing_silence_ms: int = 240) -> str:
        with self._lock:
            if self._stream is None:
                return ""
            # A short tail lets the streaming decoder commit the final word
            # without waiting for the user to hold the hotkey after speaking.
            # This is the only intentional post-release audio padding in the
            # one-model push-to-talk path.
            if trailing_silence_ms > 0:
                self._stream.feed(
                    np.zeros(int(16_000 * trailing_silence_ms / 1000), dtype=np.float32)
                )
            self._stream.finalize()
            text = self._stream.text().display.strip()
            self._stream.__exit__(None, None, None)
            self._stream = None
            return text

    def reset(self):
        with self._lock:
            if self._stream is not None:
                self._stream.reset()
                self._stream = None

    def backend_name(self) -> str:
        with self._lock:
            if self._model is None:
                return "unloaded"
            return str(getattr(self._model, "backend", "unknown"))

    def close(self):
        if self._stream is not None:
            self._stream.__exit__(None, None, None)
            self._stream = None
        if self._session is not None:
            self._session.__exit__(None, None, None)
            self._session = None
        if self._model is not None:
            self._model.__exit__(None, None, None)
            self._model = None
        self._model_path = None
        self._backend = None


streaming_service = StreamingTranscriptionService()
