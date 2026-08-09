import unittest
from datetime import datetime, timedelta

from chatter.insights import count_words, summarize


class InsightsTests(unittest.TestCase):
    def test_count_words_ignores_punctuation(self):
        self.assertEqual(count_words("Hello, Chatter — this is local."), 5)

    def test_summary_uses_local_history_fields(self):
        now = datetime(2026, 8, 9, 12, 0)
        entries = [
            {
                "kind": "dictation",
                "text": "one two three",
                "ts": now.timestamp(),
                "audio_seconds": 3,
                "context_app": "Claude",
                "context_mode": "coding",
                "cleanup_applied": True,
                "pasted": True,
                "processing_ms": 900,
            },
            {
                "kind": "dictation",
                "text": "four five",
                "ts": (now - timedelta(days=1)).timestamp(),
                "audio_seconds": 2,
                "context_app": "Claude",
                "context_mode": "coding",
                "cleanup_applied": False,
                "pasted": False,
                "processing_ms": 500,
            },
            {"kind": "file", "text": "not part of dictation insights", "ts": now.timestamp()},
        ]

        summary = summarize(entries, dictionary_entries=4, days=7, now=now)

        self.assertEqual(summary.total_words, 5)
        self.assertEqual(summary.dictations, 2)
        self.assertEqual(summary.average_wpm, 60)
        self.assertEqual(summary.words_today, 3)
        self.assertEqual(summary.sessions_today, 1)
        self.assertEqual(summary.active_days, 2)
        self.assertEqual(summary.current_streak, 2)
        self.assertEqual(summary.longest_streak, 2)
        self.assertEqual(summary.cleanup_sessions, 1)
        self.assertEqual(summary.dictionary_entries, 4)
        self.assertEqual(summary.pasted_count, 1)
        self.assertEqual(summary.average_processing_ms, 700)
        self.assertEqual(summary.contexts, (("Claude", 2),))


if __name__ == "__main__":
    unittest.main()
