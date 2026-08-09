"""Insert text at the cursor in one paste operation.

Per-character simulated typing made long dictations visibly arrive like a
typewriter and could leave a prompt half-inserted. Chatter now preserves the
user's existing pasteboard, pastes the complete transcript with Cmd+V, then
restores the previous pasteboard shortly after the target app has consumed it.
"""

import threading
import subprocess

import AppKit
from pynput.keyboard import Controller
from pynput.keyboard import Key

from . import permissions

_controller = Controller()
_RESTORE_DELAY_SECONDS = 0.65


def _set_clipboard(text: str):
    proc = subprocess.Popen("pbcopy", stdin=subprocess.PIPE)
    proc.communicate(text.encode("utf-8"))


def _snapshot_pasteboard():
    board = AppKit.NSPasteboard.generalPasteboard()
    snapshot = []
    for item in board.pasteboardItems() or []:
        values = {}
        for type_name in item.types() or []:
            data = item.dataForType_(type_name)
            if data is not None:
                try:
                    values[str(type_name)] = bytes(data)
                except Exception:
                    continue
        if values:
            snapshot.append(values)
    return snapshot


def _restore_pasteboard(snapshot):
    try:
        board = AppKit.NSPasteboard.generalPasteboard()
        board.clearContents()
        items = []
        for values in snapshot:
            item = AppKit.NSPasteboardItem.alloc().init()
            for type_name, data in values.items():
                item.setData_forType_(data, type_name)
            items.append(item)
        if items:
            board.writeObjects_(items)
    except Exception:
        # Restoring the clipboard is a courtesy; never turn a successful
        # dictation paste into an error if another app owns the pasteboard.
        return


def paste(text: str) -> bool:
    """Return True when the complete text was sent to the focused app."""
    if not text:
        return False

    if not permissions.is_trusted():
        _set_clipboard(text)
        return False

    snapshot = _snapshot_pasteboard()
    board = AppKit.NSPasteboard.generalPasteboard()
    board.clearContents()
    board.setString_forType_(text, AppKit.NSPasteboardTypeString)
    _controller.press(Key.cmd)
    _controller.press("v")
    _controller.release("v")
    _controller.release(Key.cmd)
    threading.Timer(
        _RESTORE_DELAY_SECONDS,
        _restore_pasteboard,
        args=(snapshot,),
    ).start()
    return True
