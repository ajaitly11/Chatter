"""Local-LLM cleanup pass: fixes punctuation/casing and strips filler words
from a raw transcript using a small Gemma model served by llama-server.
Falls back to the raw transcript (never raises) if formatting isn't
configured or the server/model fails.
"""

import subprocess
import time
import urllib.error
import urllib.request
import json

from . import config
from . import dictionary

SYSTEM_PROMPT = (
    "You clean up raw speech-to-text transcripts. Fix punctuation, capitalization, "
    "and remove filler words (um, uh, like, you know) and false starts. The "
    "transcript may be stitched together from multiple segments and contain a "
    "short run of duplicated or repeated words right where two segments meet — "
    "collapse those into a single occurrence. Keep the speaker's wording and "
    "meaning otherwise unchanged. Output only the cleaned text, nothing else — "
    "no preamble, no quotes, no commentary."
)


class Formatter:
    def __init__(self):
        self._proc = None
        self._port = None

    def _ensure_server(self) -> bool:
        cfg = config.load()
        bin_path = cfg.get("llama_server_bin")
        model_path = cfg.get("llama_model_path")
        if not bin_path or not model_path:
            return False

        port = cfg.get("llama_port", 8712)
        if self._proc is not None and self._proc.poll() is None and self._port == port:
            return True

        self._port = port
        self._proc = subprocess.Popen(
            [bin_path, "-m", model_path, "--port", str(port), "-ngl", "999", "--no-webui"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self._wait_ready(port)

    def _wait_ready(self, port: int, timeout: float = 60.0) -> bool:
        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{port}/health"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if resp.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                pass
            if self._proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def warm_up(self):
        """Spawns llama-server ahead of time so the first real transcription
        doesn't pay the ~5-10s model-load cost."""
        self._ensure_server()

    def format_transcript(self, raw_text: str) -> str:
        try:
            if not self._ensure_server():
                return raw_text

            payload = json.dumps({
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT + dictionary.prompt_hint()},
                    {"role": "user", "content": raw_text},
                ],
                "temperature": 0.2,
                "max_tokens": max(256, len(raw_text.split()) * 3),
                # Gemma's chat template defaults to emitting a chain-of-thought
                # "reasoning_content" block before the real answer — for a
                # quick cleanup pass that just burns the token budget on
                # thinking and leaves nothing for the actual output.
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode("utf-8")

            req = urllib.request.Request(
                f"http://127.0.0.1:{self._port}/v1/chat/completions",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            cleaned = data["choices"][0]["message"]["content"].strip()
            return cleaned or raw_text
        except Exception:
            return raw_text

    def shutdown(self):
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
