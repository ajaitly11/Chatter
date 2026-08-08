"""Insert text at the cursor in whatever app is currently focused, by typing
it directly via simulated keystrokes. Deliberately never touches the system
clipboard — the user routinely has something else on it (a link, etc.) they
mean to paste right after dictating, and clobbering that was the whole
complaint. The only time the clipboard is used is the fallback below, when
Accessibility isn't trusted yet and simulated keystrokes aren't possible at
all — that's still better than losing the transcript outright.
"""

import subprocess

from pynput.keyboard import Controller

from . import permissions

_controller = Controller()


def _set_clipboard(text: str):
    proc = subprocess.Popen("pbcopy", stdin=subprocess.PIPE)
    proc.communicate(text.encode("utf-8"))


def paste(text: str) -> bool:
    """Returns True if the text was typed at the cursor, False if it was only
    copied to the clipboard (Accessibility not yet trusted, so simulated
    keystrokes aren't available either)."""
    if not text:
        return False

    if not permissions.is_trusted():
        _set_clipboard(text)
        return False

    _controller.type(text)
    return True
