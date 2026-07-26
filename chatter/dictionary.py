"""Personal corrections for words the ASR model consistently mishears —
accents, names, jargon. Applied as a direct substitution (fast, exact) and
also handed to the AI cleanup pass as a hint (catches cases the model
almost got right but not quite, which a regex can't).
"""

import re

from . import config


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
