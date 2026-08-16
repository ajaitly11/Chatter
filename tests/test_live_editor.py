import unittest

from chatter.live_editor import current_line_bounds


class LiveEditorTests(unittest.TestCase):
    def test_command_backspace_targets_only_the_current_line(self):
        text = "keep this\nclear this line\nkeep this too"
        start, end = current_line_bounds(10, 16)

        self.assertEqual(text[:start] + text[end:], "keep this\n\nkeep this too")

    def test_line_clear_preserves_newline_at_end_of_line(self):
        text = "first\nsecond\nthird"
        start, end = current_line_bounds(6, 7)

        self.assertEqual((start, end), (6, 12))
        self.assertEqual(text[:start] + text[end:], "first\n\nthird")

    def test_empty_block_has_no_content_to_remove(self):
        self.assertEqual(current_line_bounds(-10, 4), (0, 3))
        self.assertEqual(current_line_bounds(4, 0), (4, 4))

    def test_backspace_and_delete_share_the_same_line_bounds(self):
        self.assertEqual(current_line_bounds(8, 12), (8, 19))


if __name__ == "__main__":
    unittest.main()
