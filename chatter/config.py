"""Persisted user settings, stored outside the repo in Application Support."""

import json
from pathlib import Path

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "Chatter"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS = {
    "backend": "auto",
    "formatting_enabled": True,
    "llama_server_bin": "",
    "llama_model_path": "",
    "llama_port": 8712,
    "push_to_talk_enabled": True,
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
