import unittest

from chatter.context import CaptureContext, classify_mode


class ContextTests(unittest.TestCase):
    def test_email_context_uses_app_or_window_title(self):
        self.assertEqual(classify_mode("Google Chrome", "com.google.Chrome", "Inbox - Gmail"), "email")

    def test_notes_context(self):
        self.assertEqual(classify_mode("Notes", "com.apple.Notes", "Journal"), "notes")

    def test_coding_context(self):
        self.assertEqual(classify_mode("Claude", "com.anthropic.claudefordesktop", "New chat"), "coding")

    def test_context_hint_does_not_claim_to_read_document_content(self):
        hint = CaptureContext(app_name="Safari", window_title="X", mode="social").prompt_hint()
        self.assertIn("social", hint)
        self.assertNotIn("document body", hint)


if __name__ == "__main__":
    unittest.main()
