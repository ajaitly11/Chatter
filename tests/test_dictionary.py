import unittest

from chatter.dictionary import normalize_word_boundaries


class DictionaryTests(unittest.TestCase):
    def test_fused_common_words_are_split(self):
        self.assertEqual(
            normalize_word_boundaries("goodmorning everyone"),
            "good morning everyone",
        )

    def test_known_words_are_not_over_split(self):
        self.assertEqual(normalize_word_boundaries("inside another"), "inside another")

    def test_legitimate_unknown_or_technical_words_are_not_split(self):
        self.assertEqual(
            normalize_word_boundaries("predicted noticed cleaned longword"),
            "predicted noticed cleaned longword",
        )

    def test_high_confidence_fused_phrases_still_split(self):
        self.assertEqual(
            normalize_word_boundaries("goodmorning thankyou rightshift"),
            "good morning thank you right shift",
        )


if __name__ == "__main__":
    unittest.main()
