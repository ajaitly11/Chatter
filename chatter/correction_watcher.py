"""Watches the text field you pasted into for a correction, so fixing a
mis-transcribed word teaches Chatter instead of requiring you to open the
Custom Dictionary table yourself. Uses the same Accessibility trust already
granted for paste simulation — no new permission needed, and nothing here
ever looks at any app except the one specific field we just pasted into.
"""

import difflib
import logging
import re

import ApplicationServices as AS
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import config

logger = logging.getLogger("chatter.correction_watcher")

POLL_INTERVAL_MS = 1000
QUIET_POLLS_BEFORE_SETTLED = 2  # ~2s of no further edits before treating a change as "final"
WATCH_DURATION_MS = 60_000
_WORD_RE = re.compile(r"[A-Za-z0-9']+")


def _get_focused_value():
    system_wide = AS.AXUIElementCreateSystemWide()
    err, focused = AS.AXUIElementCopyAttributeValue(system_wide, AS.kAXFocusedUIElementAttribute, None)
    if err != 0 or focused is None:
        return None, None
    err2, value = AS.AXUIElementCopyAttributeValue(focused, AS.kAXValueAttribute, None)
    if err2 != 0 or value is None:
        return focused, None
    return focused, str(value)


def _single_word_correction(old_text: str, new_text: str):
    """Returns (wrong, right) only if the edit looks like exactly one word
    swapped for another — not a rewrite, not new text appended/typed
    elsewhere. Conservative on purpose: a false "correction" would teach
    Chatter the wrong thing.
    """
    if old_text == new_text:
        return None
    old_words = _WORD_RE.findall(old_text)
    new_words = _WORD_RE.findall(new_text)
    matcher = difflib.SequenceMatcher(a=[w.lower() for w in old_words], b=[w.lower() for w in new_words])
    opcodes = matcher.get_opcodes()
    replacements = [op for op in opcodes if op[0] == "replace"]
    other_changes = [op for op in opcodes if op[0] in ("insert", "delete")]
    if len(replacements) != 1 or other_changes:
        return None
    _, i1, i2, j1, j2 = replacements[0]
    if i2 - i1 != 1 or j2 - j1 != 1:
        return None
    wrong, right = old_words[i1], new_words[j1]
    return (wrong, right) if wrong.lower() != right.lower() else None


class CorrectionWatcher(QObject):
    correction_learned = pyqtSignal(str, str)  # (wrong, right)

    def __init__(self):
        super().__init__()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._focused_element = None
        # `_baseline_text` only advances once an edit has *settled* (see
        # _poll) — never on every poll — so we diff against the last known-
        # final state, not against an intermediate, still-being-typed one.
        self._baseline_text = None
        self._last_seen_text = None
        self._quiet_polls = 0
        self._elapsed_ms = 0

    def watch_after_paste(self):
        """Call right after a successful paste — captures whatever field
        currently has focus (the one we just pasted into) as the baseline.
        """
        self._timer.stop()
        focused, value = _get_focused_value()
        if focused is None or value is None:
            logger.info("couldn't read the focused field after paste — skipping correction watch")
            return
        self._focused_element = focused
        self._baseline_text = value
        self._last_seen_text = value
        self._quiet_polls = 0
        self._elapsed_ms = 0
        self._timer.start(POLL_INTERVAL_MS)
        logger.info("watching pasted field for corrections (%ds window)", WATCH_DURATION_MS // 1000)

    def _poll(self):
        self._elapsed_ms += POLL_INTERVAL_MS
        if self._elapsed_ms >= WATCH_DURATION_MS:
            self._timer.stop()
            return

        try:
            err, value = AS.AXUIElementCopyAttributeValue(self._focused_element, AS.kAXValueAttribute, None)
        except Exception:
            logger.exception("correction watch poll failed")
            self._timer.stop()
            return
        if err != 0 or value is None:
            return

        if value != self._last_seen_text:
            # Still changing (mid-keystroke) — reset the quiet counter and
            # wait for it to settle instead of reacting to every keystroke.
            self._last_seen_text = value
            self._quiet_polls = 0
            return

        if value == self._baseline_text:
            return  # settled, but identical to baseline — nothing to learn

        self._quiet_polls += 1
        if self._quiet_polls < QUIET_POLLS_BEFORE_SETTLED:
            return

        correction = _single_word_correction(self._baseline_text, value)
        self._baseline_text = value  # this settled state is the new reference point
        if correction:
            wrong, right = correction
            corrections = dict(config.load().get("custom_dictionary", {}))
            corrections[wrong] = right
            config.update(custom_dictionary=corrections)
            logger.info("learned correction: %r -> %r", wrong, right)
            self.correction_learned.emit(wrong, right)
