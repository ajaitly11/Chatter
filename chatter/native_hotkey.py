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

RIGHT_OPTION_KEYCODE = 61  # kVK_RightOption


class RawKeyListener:
    def __init__(self, keycode: int, on_down, on_up):
        self._keycode = keycode
        self._on_down = on_down
        self._on_up = on_up
        self._thread = None
        self._run_loop = None

    def _callback(self, proxy, event_type, event, refcon):
        try:
            # Option (like all pure modifier keys) doesn't generate
            # keyDown/keyUp — pressing/releasing it fires flagsChanged, with
            # the keycode identifying *which* modifier key changed and the
            # Alternate flag's on/off state telling us press vs. release.
            if event_type == Quartz.kCGEventFlagsChanged:
                code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                if code == self._keycode:
                    is_down = bool(Quartz.CGEventGetFlags(event) & Quartz.kCGEventFlagMaskAlternate)
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
