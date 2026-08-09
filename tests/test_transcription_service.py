import unittest
from pathlib import Path

import numpy as np

from chatter.transcription_service import streaming_service, words_to_srt


class TranscriptionServiceTests(unittest.TestCase):
    def test_words_to_srt_accepts_persisted_word_dicts(self):
        result = words_to_srt([
            {"text": "hello", "t0_ms": 0, "t1_ms": 420},
            {"text": "world", "t0_ms": 420, "t1_ms": 900},
        ])

        self.assertIn("00:00:00,000 --> 00:00:00,420", result)
        self.assertIn("hello", result)
        self.assertIn("world", result)

    @unittest.skipUnless(
        any(Path(path).exists() for path in (
            "models/nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf",
            "models/nemotron-speech-streaming-en-0.6b-Q8_0.gguf",
        )),
        "local streaming model is not available",
    )
    def test_streaming_session_accepts_audio_and_finalizes(self):
        model_path = next(path for path in (
            "models/nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf",
            "models/nemotron-speech-streaming-en-0.6b-Q8_0.gguf",
        ) if Path(path).exists())
        try:
            self.assertTrue(streaming_service.warm_up(model_path, "cpu"))
            if "3.5" in model_path:
                import transcribe_cpp
                streaming_service.start(
                    model_path,
                    "cpu",
                    language="en-US",
                    family=transcribe_cpp.ParakeetStreamOptions(att_context_right=3),
                )
            else:
                streaming_service.start(model_path, "cpu", language="en")
            update, text = streaming_service.feed(np.zeros(3_200, dtype=np.float32))
            self.assertIsNotNone(update)
            self.assertIsInstance(text, str)
            self.assertIsInstance(streaming_service.finalize(), str)
        finally:
            streaming_service.close()


if __name__ == "__main__":
    unittest.main()
