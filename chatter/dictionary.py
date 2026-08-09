"""Personal corrections for words the ASR model consistently mishears —
accents, names, jargon. Applied as a direct substitution (fast, exact) and
also handed to the AI cleanup pass as a hint (catches cases the model
almost got right but not quite, which a regex can't).
"""

import re
from functools import lru_cache
from pathlib import Path

from . import config


# Small, conservative vocabulary for the cheap post-processing case where
# the decoder has the right letters but omitted a boundary ("goodmorning").
# A token is split only when both sides are known and the complete token is
# not known, so ordinary words such as "inside" and "another" are protected.
_COMMON_WORDS = frozenset(
    """
    a about actually add after again all also am an and another any are around as at
    audio back be because been before being better between both but by can change check
    chatter choose close come could create day did different do does done down each edit
    even every everyone first for from full get give go good great had has have he help her here him
    his hold how i if in input into is it its just keep know last learn let like local long
    look make many may me microphone more most my need new no not now of off on one only or
    other our out over parallel paste people phrase please process quick read ready real
    record right said same see sentence should show small so some speak speech start still
    system take tell text than that the their them then there these they thing think this
    through time to today together too transcribe transcription try two up use user very
    wait want was way we well were what when where which while who will with word words work
    would write yes you your morning hello world list open screen model output cleanup clean
    format formatting gpu performance space spaces headset laptop macbook app application
    fullscreen monitor settings status test result results character dictionary
    best regards professional email email notes note journal thoughts terminal twitter
    claude codex prompt greeting subject paragraph paragraphs cleanup yapping
    """.split()
)

_WORD_TOKEN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

# Only split boundaries that are unambiguous in ordinary dictation. The old
# heuristic split every unknown token whenever both halves appeared in a
# dictionary, which turned legitimate words such as "predicted" into
# "predict ed" and "noticed" into "not iced". General fused words are left
# for the cleanup model or the user's personal dictionary; a false split is
# much harder to notice and repair than a missed space.
_HIGH_CONFIDENCE_FUSED_SPLITS = {
    "goodmorning": "good morning",
    "goodnight": "good night",
    "thankyou": "thank you",
    "bestregards": "best regards",
    "rightshift": "right shift",
    "leftshift": "left shift",
    "macbookair": "MacBook Air",
    "allset": "all set",
    "alot": "a lot",
    "atleast": "at least",
    "aswell": "as well",
    "infront": "in front",
    "inthe": "in the",
    "onthe": "on the",
    "tothe": "to the",
    "forthe": "for the",
    "withthe": "with the",
}


@lru_cache(maxsize=1)
def _known_words() -> frozenset[str]:
    """Use macOS's local word list when available for arbitrary fused words."""
    words = set(_COMMON_WORDS)
    path = Path("/usr/share/dict/words")
    try:
        for line in path.read_text(errors="ignore").splitlines():
            word = line.strip().lower()
            if re.fullmatch(r"[a-z]+", word):
                words.add(word)
    except OSError:
        pass
    return frozenset(words)


def _preserve_replacement_case(token: str, replacement: str) -> str:
    if token.isupper():
        return replacement.upper()
    if token[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _split_token(token: str, *, allow_heuristic: bool = False) -> str:
    lower = token.lower()
    explicit = _HIGH_CONFIDENCE_FUSED_SPLITS.get(lower)
    if explicit is not None:
        return _preserve_replacement_case(token, explicit)
    if not allow_heuristic:
        return token

    known = _known_words()
    if len(lower) < 6 or lower in known or "'" in lower:
        return token
    candidates = []
    for index in range(2, len(lower) - 1):
        left, right = lower[:index], lower[index:]
        # Keep the opt-in heuristic conservative as well. A random valid
        # prefix is not enough evidence that the speaker intended two words.
        if left not in {"good", "thank", "right", "left", "best", "all"}:
            continue
        if left in known and right in known and min(len(left), len(right)) >= 2:
            # Prefer the split with the longest known pieces. This avoids
            # choosing tiny function words when a more natural pair exists.
            score = min(len(left), len(right)) * 10 + len(left) + len(right)
            candidates.append((score, index))
    if not candidates:
        return token
    _, index = max(candidates)
    left, right = token[:index], token[index:]
    if token[:1].isupper():
        left = left[:1].upper() + left[1:]
    return f"{left} {right.lower()}"


def normalize_word_boundaries(text: str, *, allow_heuristic: bool = False) -> str:
    """Repair only high-confidence ASR boundary omissions.

    ``allow_heuristic`` is intentionally opt-in for diagnostics or a future
    model-specific path. Normal dictation uses the explicit allowlist so
    technical terms, names, and long legitimate words are never split merely
    because a system dictionary happens to contain two shorter words.
    """
    if not text:
        return text
    return _WORD_TOKEN.sub(
        lambda match: _split_token(match.group(0), allow_heuristic=allow_heuristic),
        text,
    )


def apply_corrections(text: str) -> str:
    corrections = config.load().get("custom_dictionary", {})
    if not corrections or not text:
        return text
    for wrong, right in corrections.items():
        if not wrong or not right:
            continue
        pattern = re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE)
        text = pattern.sub(right, text)
    return text


def prompt_hint() -> str:
    corrections = config.load().get("custom_dictionary", {})
    if not corrections:
        return ""
    pairs = "; ".join(f'"{wrong}" -> "{right}"' for wrong, right in corrections.items())
    return (
        " The speaker has a personal dictionary of words the transcriber "
        f"often mishears — apply these corrections wherever the text looks "
        f"like one of these mishearings: {pairs}."
    )
