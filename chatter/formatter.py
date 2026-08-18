"""Local-LLM cleanup pass: fixes punctuation/casing and strips filler words
from a raw transcript using a small Gemma model served by llama-server.
Falls back to the raw transcript (never raises) if formatting isn't
configured or the server/model fails.
"""

import logging
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import json
import re
from pathlib import Path

from . import config
from .context import CaptureContext
from . import dictionary

logger = logging.getLogger("chatter.formatter")

SYSTEM_PROMPT = (
    "You are a local transcript editor. Return only the cleaned transcript.\n"
    "Preserve every detail, list item, clause, and the speaker's wording; never "
    "summarize or invent.\n"
    "Apply only these edits:\n"
    "- fix capitalization, punctuation, and missing spaces between fused words;\n"
    "- never insert a period between letters in a word (for example, turn 't.e' "
    "back into the intended word); remove punctuation artifacts from pauses;\n"
    "- remove meaningless spoken fillers and false starts; keep words that carry "
    "meaning;\n"
    "- collapse duplicated words at a segment boundary;\n"
    "- when the speaker says wait, no, I mean, actually, or similar and restates "
    "a phrase, keep the final correction and remove the walked-back phrase;\n"
    "- do not turn a pause into a full stop when the same thought continues.\n"
    "Keep introductory phrases and transitions. If the speaker clearly dictates "
    "a list, keep the lead-in and put each item on its own line with '- ' bullets; "
    "do not turn ordinary comma-separated clauses into bullets.\n"
    "For long transcripts, return the complete transcript; never summarize, stop "
    "early, or omit the ending to fit a shorter answer.\n"
    "Examples: 'send it to Alex, I mean Sam' becomes 'Send it to Sam.'; "
    "'buy apples, no, bananas' becomes 'Buy bananas.'.\n"
    "Output only the cleaned text: no preamble, explanation, quotes, or labels."
)

_LIST_CUE = re.compile(
    r"\b(?:want to buy|need to buy|things to buy|items to buy|shopping list|"
    r"grocery list|buying|list of|items i need|things i need|to purchase|"
    r"to pick up|following items)\b",
    flags=re.IGNORECASE,
)
_LIST_PREFIX = re.compile(
    r"(?P<prefix>.*?\b(?:want to buy|need to buy|things to buy|items to buy|"
    r"shopping list|grocery list|buying|list of|items i need|things i need|"
    r"to purchase|to pick up|following items)\b)\s*:?[ \t]+(?P<items>.+)$",
    flags=re.IGNORECASE,
)
_CONTINUATION_AFTER_PERIOD = re.compile(
    r"\.\s+(?=(?:and|but|or|so|because|which|that|then|also|while)\b)",
    flags=re.IGNORECASE,
)
_LOWERCASE_DOTTED_LETTERS = re.compile(r"\b([a-z])\.\s*([a-z])\b")
_LOWERCASE_PERIOD_CONTINUATION = re.compile(r"(?<=[a-z])\s*\.\s+(?=[a-z])")


def normalize_pathological_punctuation(text: str) -> str:
    """Repair ASR/cleanup artifacts such as ``t.e`` and ``word. next``.

    These are not ordinary sentence periods. They occur when a streaming
    decoder emits punctuation between letters or when a pause is surfaced in
    the middle of a lower-case phrase. Decimal numbers and normal
    capitalized sentence boundaries are left alone.
    """
    if not text:
        return text
    for _ in range(2):
        text = _LOWERCASE_DOTTED_LETTERS.sub(r"\1\2", text)
    text = _LOWERCASE_PERIOD_CONTINUATION.sub(" ", text)
    return re.sub(r"\s+([,.;!?])", r"\1", text).strip()


def find_mtp_model_path(model_path: str, cfg: dict | None = None) -> Path | None:
    """Find the matching Gemma MTP head without guessing across model families.

    A user-supplied path always wins. Otherwise the resolver looks beside the
    target GGUF in its conventional ``MTP/`` directory and prefers BF16 heads
    for regular Gemma models and Q8 heads for a QAT target.
    """
    cfg = cfg or {}
    explicit = str(cfg.get("llama_mtp_model_path", "")).strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None

    target = Path(model_path).expanduser()
    mtp_dir = target.parent / "MTP"
    if not mtp_dir.is_dir():
        return None
    stem = target.stem
    family = re.split(r"-(?:UD|Q\d|IQ|TQ|F\d|BF\d)", stem, maxsplit=1)[0]
    is_qat = "qat" in str(target).lower()
    preferred = ("Q8_0", "BF16") if is_qat else ("BF16", "Q8_0")
    candidates = [mtp_dir / f"{family}-{quant}-MTP.gguf" for quant in preferred]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def clean_model_output(text: str) -> str:
    """Remove transport-level reasoning/labels without rewriting user text."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\|think\|>.*?<\|/think\|>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = text.strip()
    text = re.sub(r"^```(?:text|plain)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^(?:cleaned\s+transcript|transcript|output)\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def format_explicit_list(text: str, raw_text: str) -> str:
    """Give an obvious spoken shopping/list request stable item boundaries.

    The small cleanup model is good at punctuation but often collapses a
    spoken list back into a comma run. This conservative post-pass only acts
    when the raw utterance contains an explicit list/buying cue and at least
    three comma/semicolon-separated items.
    """
    if not text or "\n- " in text or "\n• " in text:
        return text
    if _LIST_CUE.search(raw_text) is None:
        return text
    # A correction marker is not a list item. Keep it for the dedicated
    # correction normalizer/model rule instead of turning "no, I mean" into
    # another bullet.
    if re.search(r"\b(?:wait|no|i mean|actually|sorry|rather)\b", raw_text, flags=re.IGNORECASE):
        return text

    candidate = re.sub(r"[.!?]+$", "", text.strip())
    match = _LIST_PREFIX.match(candidate)
    if match is None:
        return text
    prefix = match.group("prefix").strip()
    items = match.group("items").strip(" :")
    # Treat only a final conjunction as a separator; "peanut butter and jelly"
    # remains one item unless the speaker also used comma/semicolon boundaries.
    if re.search(r"[,;]", items):
        items = re.sub(
            r"\s*,?\s+and\s+(?=[^,;]+$)", ", ", items, flags=re.IGNORECASE
        )
    parts = [part.strip(" ,;.") for part in re.split(r"[,;]+", items)]
    parts = [part for part in parts if part]
    if len(parts) < 3:
        return text
    return prefix.rstrip(" :,. ") + ":\n" + "\n".join(f"- {part}" for part in parts)


def normalize_continuation_punctuation(text: str) -> str:
    """Join an obvious continuation that ASR punctuation split at a pause.

    This deliberately only repairs a period followed by a lowercase
    conjunction/continuation word. A capitalized sentence boundary is left
    to the language model, so this remains a conservative safety net rather
    than a second punctuation engine.
    """
    if not text:
        return text
    return _CONTINUATION_AFTER_PERIOD.sub(", ", text)


def deterministic_cleanup(text: str) -> str:
    """Fast, local cleanup that remains useful when the optional LLM is off."""
    cleaned = normalize_self_corrections(text or "")
    cleaned = dictionary.apply_corrections(cleaned)
    cleaned = dictionary.normalize_word_boundaries(cleaned)
    cleaned = normalize_pathological_punctuation(cleaned)
    return normalize_continuation_punctuation(cleaned)


_SELF_CORRECTION_MARKER = re.compile(
    r"(?P<before>[^.!?\n]*?)"
    r"(?P<marker>(?:\bwait\s*,?\s*)?(?:\bno\s*,?\s*)?"
    r"(?:\bi\s+mean\b|\bactually\b|\bsorry\b|\brather\b|\bno\s*,))"
    r"\s+",
    flags=re.IGNORECASE,
)

_CORRECTION_ANCHORS = re.compile(
    r"\b(?:buy|call|choose|email|get|give|have|like|meet|need|pick|prefer|"
    r"put|said|schedule|send|set|should|take|talk|tell|to|use|want|with|on|at)\b",
    flags=re.IGNORECASE,
)


def normalize_self_corrections(text: str) -> str:
    """Remove only explicit spoken walk-backs before the cleanup model.

    This is intentionally conservative. It needs a correction marker and a
    replacement phrase in the same sentence; ordinary uses of "no" and
    "actually" are left untouched. The model then handles punctuation and
    any subtler context.
    """
    for _ in range(3):
        match = _SELF_CORRECTION_MARKER.search(text)
        if match is None:
            break
        before = match.group("before").rstrip(" ,;:-")
        if not before.strip():
            break
        anchors = list(_CORRECTION_ANCHORS.finditer(before))
        if anchors:
            keep = before[: anchors[-1].end()].rstrip()
        else:
            # If the entire clause is the walked-back phrase, replace it.
            keep = before.rsplit(",", 1)[0].rstrip(" ,;:-")
        replacement = text[match.end():].lstrip(" ,;:-")
        if not replacement:
            break
        text = (keep + " " if keep else "") + replacement
    return text.strip()


class Formatter:
    def __init__(self):
        self._proc = None
        self._port = None
        self._request_lock = threading.Lock()
        self._launch_signature = None
        self._server_log_thread = None

    def _ensure_server(self) -> bool:
        cfg = config.load()
        bin_path = cfg.get("llama_server_bin")
        model_path = cfg.get("llama_model_path")
        if not bin_path or not model_path:
            return False

        port = cfg.get("llama_port", 8712)
        mtp_path = None
        if cfg.get("llama_mtp_enabled", False):
            mtp_path = find_mtp_model_path(model_path, cfg)
            if mtp_path is None:
                logger.warning("Gemma MTP enabled but no matching MTP head was found")
        signature = (str(bin_path), str(model_path), int(port), str(mtp_path or ""))
        if (
            self._proc is not None
            and self._proc.poll() is None
            and self._port == port
            and self._launch_signature == signature
        ):
            return True

        if self._proc is not None and self._proc.poll() is None:
            self._shutdown_locked()

        self._port = port
        args = [
            bin_path, "-m", model_path, "--port", str(port), "-ngl", "999", "--no-webui",
            # Every formatting call is a unique one-off prompt (never
            # repeated/incremental like a chat history), so llama-server's
            # prompt cache — built for reusing prior context — only adds
            # lookup overhead here. Measured: disabling it cut prefill
            # time roughly 6x (1887ms -> 298ms for a 33-59 token prompt).
            "--cache-ram", "0",
        ]
        if mtp_path is not None:
            args.extend([
                "--spec-type", "draft-mtp",
                "--spec-draft-model", str(mtp_path),
                "--spec-draft-n-max", str(max(1, min(8, int(cfg.get("llama_mtp_tokens", 4))))),
            ])
            logger.info("Gemma cleanup MTP enabled: draft=%s", mtp_path)
        self._launch_signature = signature
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._server_log_thread = threading.Thread(
            target=self._read_server_logs,
            args=(self._proc,),
            name="chatter-llama-server-log",
            daemon=True,
        )
        self._server_log_thread.start()
        return self._wait_ready(port)

    @staticmethod
    def _read_server_logs(proc):
        """Keep llama-server's pipe drained and expose MTP proof in chatter.log."""
        if proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if any(
                marker in line.lower()
                for marker in ("speculative", "draft acceptance", "model loaded", "server is listening", "failed")
            ):
                logger.info("llama-server: %s", line)

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
        with self._request_lock:
            self._ensure_server()

    @staticmethod
    def _matches_managed_server(command: str, port: int, model_path: str) -> bool:
        """Return true only for the llama-server Chatter configured to own.

        The cleanup model is an optional child process. If Chatter is killed
        or upgraded while it is running, the child can outlive the GUI. On the
        next launch we may reclaim that exact process, but must never kill an
        unrelated llama.cpp server that happens to be running on the machine.
        """
        command = str(command or "")
        return (
            "llama-server" in command
            and f"--port {int(port)}" in command
            and str(Path(model_path).expanduser()) in command
        )

    @classmethod
    def reap_stale_server(cls, cfg: dict | None = None) -> int:
        """Reclaim an orphaned optional cleanup server when cleanup is off.

        This is deliberately conservative: it looks only at the configured
        localhost port, verifies the full configured model path in the target
        command line, and leaves every other process alone. It fixes the
        common upgrade/crash case where a previous Chatter-owned server was
        still holding model memory even though the current setting was off.
        """
        cfg = cfg or config.load()
        if cfg.get("formatting_enabled", True):
            return 0
        model_path = str(cfg.get("llama_model_path", "")).strip()
        if not model_path:
            return 0
        try:
            port = int(cfg.get("llama_port", 8712))
        except (TypeError, ValueError):
            port = 8712

        try:
            result = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug("could not inspect stale cleanup server on port %s", port, exc_info=True)
            return 0

        reclaimed = 0
        for raw_pid in result.stdout.splitlines():
            try:
                pid = int(raw_pid.strip())
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            try:
                command_result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                command = command_result.stdout.strip()
                if not cls._matches_managed_server(command, port, model_path):
                    continue
                os.kill(pid, signal.SIGTERM)
                reclaimed += 1
                logger.info("reclaimed stale Chatter cleanup server pid=%s", pid)
            except (OSError, subprocess.SubprocessError):
                logger.debug("could not reclaim stale cleanup server pid=%s", pid, exc_info=True)
        return reclaimed

    def format_transcript(
        self, raw_text: str, context: CaptureContext | None = None
    ) -> str:
        with self._request_lock:
            try:
                fallback = deterministic_cleanup(raw_text)
                if not self._ensure_server():
                    return format_explicit_list(fallback, fallback) or fallback or raw_text

                cleanup_input = deterministic_cleanup(raw_text)
                word_count = len(cleanup_input.split())
                # A fixed 512-token ceiling silently cut off long dictation.
                # Give the editor enough room to return the full input while
                # retaining a bounded safety limit for accidental runaway
                # output.
                output_budget = max(96, min(2048, word_count * 2 + 96))
                prompt = SYSTEM_PROMPT
                if context is not None:
                    prompt += "\n\n" + context.prompt_hint()
                payload = json.dumps({
                    "messages": [
                        {"role": "system", "content": prompt + "\n" + dictionary.prompt_hint()},
                        {"role": "user", "content": cleanup_input},
                    ],
                    "temperature": 0.0,
                    "max_tokens": output_budget,
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
                timeout = 90 if word_count > 300 else 45
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read())
                choice = data["choices"][0]
                cleaned = clean_model_output(choice["message"].get("content", ""))
                if choice.get("finish_reason") == "length":
                    logger.warning(
                        "cleanup model reached max_tokens for %d-word transcript; using deterministic result",
                        word_count,
                    )
                    return format_explicit_list(fallback, fallback) or fallback or raw_text
                # Never let the optional model undo cheap, high-confidence
                # local repairs. This is especially important for the rare
                # fused token that the model repeats in its response.
                cleaned = deterministic_cleanup(cleaned)
                cleaned = format_explicit_list(cleaned, cleanup_input)
                return cleaned or fallback or raw_text
            except Exception:
                return deterministic_cleanup(raw_text) or raw_text

    def shutdown(self):
        with self._request_lock:
            self._shutdown_locked()

    def _shutdown_locked(self):
        """Stop the child while ``_request_lock`` is already held."""
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._launch_signature = None


class LiveCleanupCoordinator:
    """Debounced, non-blocking cleanup of the latest streaming preview.

    Nemotron remains the only ASR model. This coordinator optionally feeds
    the newest text to the separate local formatter on its own thread while
    the microphone and streaming decoder continue uninterrupted. Newer
    previews replace older work, so a slow formatter can never overwrite the
    HUD with stale text.
    """

    # Cleanup is a background preview, not a second live decoder. A slightly
    # longer debounce and a minimum text delta prevent long dictation from
    # queueing expensive whole-transcript rewrites on every ASR partial.
    DEBOUNCE_SECONDS = 0.75
    MIN_SUBMIT_DELTA = 28

    def __init__(self, formatter, on_result):
        self._formatter = formatter
        self._on_result = on_result
        self._condition = threading.Condition()
        self._pending = None
        self._deadline = None
        self._generation = 0
        self._latest_result = None
        self._last_submitted_text = ""
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="chatter-live-cleanup", daemon=True)
        self._thread.start()

    def reset(self):
        with self._condition:
            self._generation += 1
            self._pending = None
            self._deadline = None
            self._latest_result = None
            self._last_submitted_text = ""
            self._condition.notify_all()

    def submit(self, text: str, context: CaptureContext | None = None):
        if not text or not text.strip():
            return
        with self._condition:
            if (
                self._last_submitted_text
                and text != self._last_submitted_text
                and len(text) - len(self._last_submitted_text) < self.MIN_SUBMIT_DELTA
            ):
                return
            if text == self._last_submitted_text:
                return
            context_key = context.key if context is not None else ""
            self._pending = (text, self._generation, context, context_key)
            self._last_submitted_text = text
            self._deadline = time.monotonic() + self.DEBOUNCE_SECONDS
            self._condition.notify_all()

    def latest_for(self, text: str, context: CaptureContext | None = None) -> str | None:
        with self._condition:
            if self._latest_result is None:
                return None
            source, cleaned, generation, context_key = self._latest_result
            requested_key = context.key if context is not None else ""
            if (
                generation == self._generation
                and source == text
                and context_key == requested_key
                and cleaned.strip()
            ):
                return cleaned
            return None

    def shutdown(self):
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        self._thread.join(timeout=1)

    def _run(self):
        while True:
            with self._condition:
                while not self._stopped and self._pending is None:
                    self._condition.wait()
                if self._stopped:
                    return
                wait_for = self._deadline - time.monotonic()
                if wait_for > 0:
                    self._condition.wait(timeout=wait_for)
                    continue
                text, generation, context, context_key = self._pending
                self._pending = None
                self._deadline = None

            cleaned = self._formatter.format_transcript(text, context=context)
            if not cleaned or cleaned.strip() == text.strip():
                continue

            with self._condition:
                # A newer partial or a new recording makes this result stale.
                if self._stopped or generation != self._generation or self._pending is not None:
                    continue
                self._latest_result = (text, cleaned, generation, context_key)
            self._on_result(text, cleaned, generation)
