import tempfile
import unittest
from pathlib import Path

from chatter.formatter import (
    Formatter,
    SYSTEM_PROMPT,
    clean_model_output,
    deterministic_cleanup,
    find_mtp_model_path,
    format_explicit_list,
    normalize_pathological_punctuation,
    normalize_continuation_punctuation,
    normalize_self_corrections,
)


class FormatterTests(unittest.TestCase):
    def test_stale_server_match_is_strict(self):
        model = "/tmp/chatter-cleanup.gguf"
        command = (
            "/opt/homebrew/bin/llama-server -m /tmp/chatter-cleanup.gguf "
            "--port 8712 -ngl 999"
        )
        self.assertTrue(Formatter._matches_managed_server(command, 8712, model))
        self.assertFalse(
            Formatter._matches_managed_server(
                command.replace("8712", "9999"), 8712, model
            )
        )
        self.assertFalse(
            Formatter._matches_managed_server(
                command.replace(model, "/tmp/another-model.gguf"), 8712, model
            )
        )

    def test_cleanup_prompt_preserves_content_rules(self):
        self.assertIn("never summarize or invent", SYSTEM_PROMPT)
        self.assertIn("do not turn a pause into a full stop", SYSTEM_PROMPT)
        self.assertIn("fused words", SYSTEM_PROMPT)
        self.assertIn("I mean Sam", SYSTEM_PROMPT)

    def test_transport_wrappers_are_removed_without_rewriting_text(self):
        result = clean_model_output(
            "<think>internal reasoning</think>\nCleaned transcript: Hello, world."
        )
        self.assertEqual(result, "Hello, world.")

    def test_mtp_resolver_prefers_matching_head(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model = root / "gemma-4-E2B-it-UD-Q4_K_XL.gguf"
            mtp_dir = root / "MTP"
            mtp_dir.mkdir()
            expected = mtp_dir / "gemma-4-E2B-it-BF16-MTP.gguf"
            model.touch()
            expected.touch()
            self.assertEqual(
                find_mtp_model_path(str(model), {"llama_mtp_enabled": True}),
                expected,
            )

    def test_explicit_buying_list_gets_stable_bullets(self):
        result = format_explicit_list(
            "I want to buy apples, bananas, eggs, and milk.",
            "I want to buy apples bananas eggs and milk",
        )
        self.assertEqual(
            result,
            "I want to buy:\n- apples\n- bananas\n- eggs\n- milk",
        )

    def test_ordinary_sentence_is_not_forced_into_a_list(self):
        text = "I want to buy apples, but I also need a bag."
        self.assertEqual(format_explicit_list(text, text), text)

    def test_shopping_list_lead_in_gets_bullets(self):
        text = "Here is my shopping list: apples, bananas, eggs, and milk."
        self.assertEqual(
            format_explicit_list(text, text),
            "Here is my shopping list:\n- apples\n- bananas\n- eggs\n- milk",
        )

    def test_pause_before_continuation_is_not_kept_as_a_full_stop(self):
        self.assertEqual(
            normalize_continuation_punctuation("I was saying. and then we stopped."),
            "I was saying, and then we stopped.",
        )

    def test_pathological_periods_inside_words_are_removed(self):
        self.assertEqual(
            normalize_pathological_punctuation("Please fix t.e and word. next."),
            "Please fix te and word next.",
        )

    def test_deterministic_cleanup_applies_corrections_without_server(self):
        self.assertEqual(
            deterministic_cleanup("send it to Alex, I mean Sam goodmorning"),
            "send it to Sam good morning",
        )

    def test_explicit_self_correction_removes_walked_back_phrase(self):
        self.assertEqual(
            normalize_self_corrections(
                "I want to buy apples, no, I mean bananas and milk"
            ),
            "I want to buy bananas and milk",
        )
        self.assertEqual(
            normalize_self_corrections("send it to Alex, I mean Sam"),
            "send it to Sam",
        )

    def test_non_correction_no_is_left_alone(self):
        text = "No, I do not want to buy apples."
        self.assertEqual(normalize_self_corrections(text), text)


if __name__ == "__main__":
    unittest.main()
