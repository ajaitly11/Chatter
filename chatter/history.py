"""Persistent log of transcription results — dictation (push-to-talk) and
file transcriptions both, distinguished by `kind`. Append-only JSONL in the
same Application Support directory config.py already uses. Backs both the
Live Dictation tab's history (every spoken result, even ones that only got
copied to the clipboard, not pasted) and the Files tab's list of past jobs
(so they can be re-exported without re-transcribing the source file).
"""

import json
import time
from pathlib import Path

HISTORY_DIR = Path.home() / "Library" / "Application Support" / "Chatter"
HISTORY_PATH = HISTORY_DIR / "history.jsonl"


def append(kind: str, text: str, **fields) -> dict:
    entry = {"kind": kind, "text": text, "ts": time.time(), **fields}
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def clear(kind: str | None = None) -> None:
    """Erases history entries. With `kind`, only that kind is dropped and
    the other kind's entries (e.g. file transcriptions, when clearing
    dictation) are preserved by rewriting the log without them."""
    if not HISTORY_PATH.exists():
        return
    if kind is None:
        HISTORY_PATH.unlink()
        return
    remaining = [e for e in load() if e.get("kind") != kind]
    with HISTORY_PATH.open("w", encoding="utf-8") as f:
        for entry in remaining:
            f.write(json.dumps(entry) + "\n")


def load(kind: str | None = None, limit: int | None = None) -> list[dict]:
    """Newest first."""
    if not HISTORY_PATH.exists():
        return []
    entries = []
    with HISTORY_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kind is None or entry.get("kind") == kind:
                entries.append(entry)
    entries.sort(key=lambda e: e.get("ts", 0), reverse=True)
    if limit is not None:
        entries = entries[:limit]
    return entries
