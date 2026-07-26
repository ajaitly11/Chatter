"""Put text on the clipboard and simulate Cmd+V to insert it at the cursor
in whatever app is currently focused.
"""

import subprocess
import time

from pynput.keyboard import Controller, Key

_controller = Controller()


def _set_clipboard(text: str):
    proc = subprocess.Popen("pbcopy", stdin=subprocess.PIPE)
    proc.communicate(text.encode("utf-8"))


def paste(text: str):
    if not text:
        return
    _set_clipboard(text)
    time.sleep(0.05)  # let the clipboard settle before the paste keystroke fires
    with _controller.pressed(Key.cmd):
        _controller.press("v")
        _controller.release("v")
