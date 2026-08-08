"""Short rotating phrases for the HUD's caption label — one picked at random
each time push-to-talk enters a new state, so the HUD doesn't say the exact
same word every single time. Errors show the real error text instead (see
overlay.py) since that's actionable information, not flavor text.
"""

import random

PHRASES = {
    "listening": ["listening…", "go ahead", "I'm here", "all ears"],
    "processing": ["cleaning that up…", "thinking…", "one sec", "almost there"],
    "done": ["pasted!", "done", "got it", "all set"],
}


def pick(state: str) -> str:
    options = PHRASES.get(state)
    if not options:
        return ""
    return random.choice(options)
