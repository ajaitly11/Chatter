import unittest

from chatter.native_hotkey import (
    DOUBLE_TAP_PERSISTENT,
    HOLD_TO_TALK,
    _dispatch_modifier_edge,
    normalize_activation_mode,
)


class HotkeyOptionTests(unittest.TestCase):
    def test_unknown_or_missing_activation_mode_keeps_hold_default(self):
        self.assertEqual(normalize_activation_mode(None), HOLD_TO_TALK)
        self.assertEqual(normalize_activation_mode({}), HOLD_TO_TALK)
        self.assertEqual(normalize_activation_mode("double-tap-not-yet-supported"), HOLD_TO_TALK)

    def test_toggle_activation_mode_is_persistable(self):
        self.assertEqual(normalize_activation_mode(DOUBLE_TAP_PERSISTENT), DOUBLE_TAP_PERSISTENT)
        self.assertEqual(normalize_activation_mode("toggle"), DOUBLE_TAP_PERSISTENT)

    def test_repeated_modifier_state_does_not_emit_duplicate_edge(self):
        dispatched, state = _dispatch_modifier_edge(False, True)
        self.assertTrue(dispatched)
        self.assertTrue(state)

        dispatched, state = _dispatch_modifier_edge(state, True)
        self.assertFalse(dispatched)
        self.assertTrue(state)

        dispatched, state = _dispatch_modifier_edge(state, False)
        self.assertTrue(dispatched)
        self.assertFalse(state)


if __name__ == "__main__":
    unittest.main()
