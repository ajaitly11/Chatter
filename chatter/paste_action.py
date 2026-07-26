"""Put text on the clipboard and, if Accessibility is trusted, simulate
Cmd+V to insert it at the cursor in whatever app is currently focused.
Clipboard is always set first, so manual Cmd+V works even without trust.
"""

import subprocess
import time

from pynput.keyboard import Controller, Key

from . import permissions

_controller = Controller()


def _set_clipboard(text: str):
    proc = subprocess.Popen("pbcopy", stdin=subprocess.PIPE)
    proc.communicate(text.encode("utf-8"))


def paste(text: str) -> bool:
    """Returns True if the auto-paste keystroke was sent, False if only the
    clipboard was set (e.g. Accessibility not yet trusted)."""
    if not text:
        return False
    _set_clipboard(text)

    if not permissions.is_trusted():
        return False

    time.sleep(0.05)  # let the clipboard settle before the paste keystroke fires
    with _controller.pressed(Key.cmd):
        _controller.press("v")
        _controller.release("v")
    return True
