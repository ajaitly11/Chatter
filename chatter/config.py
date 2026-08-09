"""Persisted user settings, stored outside the repo in Application Support."""

import json
from pathlib import Path

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "Chatter"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS = {
    "backend": "auto",
    "formatting_enabled": True,
    # How the optional cleanup model should understand the destination. Auto
    # uses the foreground app/window title; the other values are explicit
    # overrides for users who prefer a stable writing mode.
    "cleanup_context_mode": "auto",
    "llama_server_bin": "",
    "llama_model_path": "",
    "llama_port": 8712,
    # Optional llama.cpp speculative decoding for the cleanup model. This is
    # deliberately opt-in: on Apple Silicon it can be slower for short,
    # latency-sensitive cleanup requests depending on the llama.cpp build.
    "llama_mtp_enabled": False,
    "llama_mtp_model_path": "",
    "llama_mtp_tokens": 4,
    "push_to_talk_enabled": True,
    # macOS virtual keycode for the push-to-talk hold key. Defaults to Right
    # Shift (60); see chatter/native_hotkey.py for other supported keycodes.
    "hotkey_keycode": 60,
    # Optional batch model for file transcription. Push-to-talk uses the
    # separate streaming_model_path and does not run a second ASR pass.
    "whisper_model_path": "",
    # Push-to-talk model: one streaming-capable local ASR session from press
    # through finalization.
    "streaming_model_path": "",
    # Optional PortAudio input device name. Empty means the system default.
    # Older integer indexes are still accepted for migration.
    "input_device": "",
    # Explicit language prevents the ASR model from choosing a spurious
    # language on short or quiet English clips. Empty means auto-detect.
    "language": "en",
    # Personal corrections for words the ASR model consistently mishears
    # (accents, names, jargon) — {"mis-heard term": "correct term"}.
    "custom_dictionary": {},
    # Whether the first-run permission flow (chatter/onboarding.py) has
    # already been shown.
    "onboarding_complete": False,
}


def load() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def save(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def update(**changes) -> dict:
    config = load()
    config.update(changes)
    save(config)
    return config
