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
    def __init__(self, keycode: int, on_down, on_up, on_error=None):
        self._keycode = keycode
        self._flag_mask = _FLAG_MASK_BY_KEYCODE[keycode]
        self._on_down = on_down
        self._on_up = on_up
        self._on_error = on_error
        self._thread = None
        self._run_loop = None
        self._tap = None
        self._monitor_thread = None
        self._monitor_stop = threading.Event()
        self._callback_count = 0
        self._hotkey_count = 0
        self._disabled_event_count = 0

    def _callback(self, proxy, event_type, event, refcon):
        # CGEventTap callbacks have a very small timeout. Do not log, query
        # AppKit, or do any other blocking work here: macOS disables the tap
        # when this callback is slow, which makes a global hotkey appear to
        # work only while Chatter is frontmost.
        try:
            disabled_events = {
                getattr(Quartz, "kCGEventTapDisabledByTimeout", -1),
                getattr(Quartz, "kCGEventTapDisabledByUserInput", -2),
            }
            if event_type in disabled_events and self._tap is not None:
                self._disabled_event_count += 1
                Quartz.CGEventTapEnable(self._tap, True)
                return event
            if event_type != Quartz.kCGEventFlagsChanged:
                return event
            self._callback_count += 1
            code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            if code == self._keycode:
                self._hotkey_count += 1
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
            if self._on_error is not None:
                self._on_error("Hotkey listener unavailable — grant Input Monitoring permission.")
            return

        self._tap = tap
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._run_loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._run_loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)

        # Keep diagnostics off the event-tap callback. This monitor is only
        # for distinguishing "the OS delivered no cross-app events" from
        # "the event arrived but downstream Qt handling failed".
        self._monitor_stop.clear()

        def _monitor():
            while not self._monitor_stop.wait(5.0):
                try:
                    logger.info(
                        "hotkey tap health: enabled=%s callbacks=%d hotkeys=%d disabled_events=%d",
                        bool(Quartz.CGEventTapIsEnabled(tap)),
                        self._callback_count,
                        self._hotkey_count,
                        self._disabled_event_count,
                    )
                except Exception:
                    logger.exception("hotkey tap health check failed")

        self._monitor_thread = threading.Thread(target=_monitor, daemon=True)
        self._monitor_thread.start()
        logger.info("raw hotkey tap running for keycode %d", self._keycode)
        Quartz.CFRunLoopRun()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self):
        self._monitor_stop.set()
        if self._run_loop is not None:
            Quartz.CFRunLoopStop(self._run_loop)
            self._run_loop = None
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.5)
        if self._monitor_thread is not None and self._monitor_thread is not threading.current_thread():
            self._monitor_thread.join(timeout=0.5)
        self._thread = None
        self._monitor_thread = None
        self._tap = None
