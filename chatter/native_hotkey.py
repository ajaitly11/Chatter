"""Direct Quartz CGEventTap listener for a single key's down/up events.

pynput's keyboard.Listener converts every observed key event to an NSEvent
internally (`NSEvent.eventWithCGEvent_`). For most keys that's harmless, but
for Caps Lock specifically it triggers macOS's text-input-source machinery,
which asserts it's running on a specific dispatch queue — and pynput's
listener runs on its own background thread, not that queue. The assertion
fails and the OS kills the whole process (EXC_BREAKPOINT/SIGTRAP), which
can't be caught with a Python try/except since it's a native-level abort,
not a Python exception.

We only care about one physical key's raw keycode, so there's no need for
pynput's higher-level (and crash-prone) character/Key translation at all —
reading CGEventGetIntegerValueField(kCGKeyboardEventKeycode) directly avoids
that code path entirely.
"""

import logging
import threading

import Quartz

logger = logging.getLogger("chatter.native_hotkey")

# macOS virtual keycodes for the modifier keys push-to-talk can bind to, and
# each one's flagsChanged bit (see RawKeyListener docstring on _callback).
# (keycode, human-readable label, warning-or-None)
SUPPORTED_HOTKEYS = [
    (60, "Right Shift", None),
    (61, "Right Option (⌥)", None),
    (62, "Right Control", None),
    (54, "Right Command (⌘)", None),
    (56, "Left Shift", None),
    (58, "Left Option (⌥)", None),
    (59, "Left Control", None),
    (55, "Left Command (⌘)", None),
    (
        57,
        "Caps Lock",
        "Caps Lock also toggles its normal on/off state as a side effect — "
        "anything you type elsewhere right after may come out capitalized "
        "unexpectedly.",
    ),
]

_FLAG_MASK_BY_KEYCODE = {
    60: Quartz.kCGEventFlagMaskShift,       # Right Shift
    61: Quartz.kCGEventFlagMaskAlternate,   # Right Option
    62: Quartz.kCGEventFlagMaskControl,     # Right Control
    54: Quartz.kCGEventFlagMaskCommand,     # Right Command
    56: Quartz.kCGEventFlagMaskShift,       # Left Shift
    58: Quartz.kCGEventFlagMaskAlternate,   # Left Option
    59: Quartz.kCGEventFlagMaskControl,     # Left Control
    55: Quartz.kCGEventFlagMaskCommand,     # Left Command
    57: Quartz.kCGEventFlagMaskAlphaShift,  # Caps Lock
}


class RawKeyListener:
    def __init__(self, keycode: int, on_down, on_up):
        self._keycode = keycode
        self._flag_mask = _FLAG_MASK_BY_KEYCODE[keycode]
        self._on_down = on_down
        self._on_up = on_up
        self._thread = None
        self._run_loop = None

    def _callback(self, proxy, event_type, event, refcon):
        try:
            if event_type == Quartz.kCGEventFlagsChanged:
                code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                if code == self._keycode:
                    is_down = bool(Quartz.CGEventGetFlags(event) & self._flag_mask)
                    (self._on_down if is_down else self._on_up)()
        except Exception:
            logger.exception("hotkey tap callback failed")
        return event

    def _run(self):
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            self._callback,
            None,
        )
        if tap is None:
            logger.warning(
                "couldn't create event tap — grant Input Monitoring permission "
                "in System Settings > Privacy & Security"
            )
            return

        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._run_loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._run_loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        logger.info("raw hotkey tap running for keycode %d", self._keycode)
        Quartz.CFRunLoopRun()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._run_loop is not None:
            Quartz.CFRunLoopStop(self._run_loop)
            self._run_loop = None
