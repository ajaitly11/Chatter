import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from chatter.transcription_service import (
    TranscriptionService,
    StreamingTranscriptionService,
    segments_to_srt,
    streaming_service,
    words_to_srt,
)


class _FakeSession:
    def __init__(self, error_timestamps=None, error_status=None):
        self.calls = []
        self.error_timestamps = error_timestamps
        self.error_status = error_status
        self.closed = False

    def stream(self, **kwargs):
        return _FakeStream()

    def run(self, pcm, **kwargs):
        self.calls.append(kwargs.copy())
        if kwargs.get("timestamps") == self.error_timestamps:
            message = (
                "transcribe_run: unsupported timestamp granularity (status 12)"
                if self.error_status == 12
                else "transcribe_run: unsupported language (status 10)"
            )
            error = RuntimeError(message)
            if self.error_status is not None:
                error.status = self.error_status
            raise error
        return SimpleNamespace(text="ok", words=())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


class _FakeStream:
    def __init__(self):
        self.closed = False
        self.finalized = False

    def feed(self, pcm):
        return None

    def finalize(self):
        self.finalized = True

    def text(self):
        return SimpleNamespace(display="streamed")

    def reset(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


class _FakeModel:
    def __init__(self, max_timestamp_kind):
        self.capabilities = SimpleNamespace(max_timestamp_kind=max_timestamp_kind)
        self.closed = False
        self.session_count = 0

    def session(self):
        self.session_count += 1
        return _FakeSession()

    def __exit__(self, *exc):
        self.closed = True


def _service_for_test(session, max_timestamp_kind):
    service = TranscriptionService()
    service._model = _FakeModel(max_timestamp_kind)
    service._session = session
    service._model_path = "model.gguf"
    service._backend = "cpu"
    return service


class TranscriptionServiceTests(unittest.TestCase):
    def test_run_releases_session_but_keeps_warm_model(self):
        session = _FakeSession()
        session.closed = False
        service = _service_for_test(session, "word")

        service.transcribe(
            np.zeros(1, dtype=np.float32),
            "model.gguf",
            "cpu",
        )

        self.assertTrue(session.closed)
        self.assertIsNone(service._session)
        self.assertIsNotNone(service._model)

    def test_one_shot_run_releases_model_too(self):
        session = _FakeSession()
        session.closed = False
        service = _service_for_test(session, "word")
        model = service._model

        service.transcribe(
            np.zeros(1, dtype=np.float32),
            "model.gguf",
            "cpu",
            keep_model=False,
        )

        self.assertTrue(session.closed)
        self.assertTrue(model.closed)
        self.assertIsNone(service._session)
        self.assertIsNone(service._model)

    def test_stream_finalize_releases_stream_and_session_but_keeps_model(self):
        service = StreamingTranscriptionService()
        model = _FakeModel("none")
        session = _FakeSession()
        stream = _FakeStream()
        service._model = model
        service._session = session
        service._stream = stream
        service._model_path = "model.gguf"
        service._backend = "cpu"

        self.assertEqual(service.finalize(trailing_silence_ms=0), "streamed")
        self.assertTrue(stream.closed)
        self.assertTrue(session.closed)
        self.assertIsNone(service._stream)
        self.assertIsNone(service._session)
        self.assertIs(service._model, model)

    def test_streaming_model_reuses_model_when_session_is_reopened(self):
        service = StreamingTranscriptionService()
        model = _FakeModel("none")
        service._model = model
        service._model_path = "model.gguf"
        service._backend = "cpu"

        service._ensure_session("model.gguf", "cpu")

        self.assertEqual(model.session_count, 1)
        self.assertIs(service._model, model)
        self.assertFalse(model.closed)

    def test_word_request_downgrades_to_segment_for_segment_only_model(self):
        session = _FakeSession()
        service = _service_for_test(session, "segment")

        result = service.transcribe(
            np.zeros(1, dtype=np.float32),
            "model.gguf",
            "cpu",
            language="en",
            timestamps="word",
        )

        self.assertEqual(result.text, "ok")
        self.assertEqual(session.calls, [{"language": "en", "timestamps": "segment"}])

    def test_word_request_falls_back_to_auto_for_timestampless_model(self):
        session = _FakeSession()
        service = _service_for_test(session, "none")

        service.transcribe(
            np.zeros(1, dtype=np.float32),
            "model.gguf",
            "cpu",
            timestamps="word",
        )

        self.assertEqual(session.calls, [{"timestamps": "auto"}])

    def test_status_12_retries_once_with_auto(self):
        session = _FakeSession(error_timestamps="word", error_status=12)
        service = _service_for_test(session, "token")

        result = service.transcribe(
            np.zeros(1, dtype=np.float32),
            "model.gguf",
            "cpu",
            timestamps="word",
        )

        self.assertEqual(result.text, "ok")
        self.assertEqual(
            session.calls,
            [{"timestamps": "word"}, {"timestamps": "auto"}],
        )

    def test_non_timestamp_error_is_not_retried(self):
        session = _FakeSession(error_timestamps="word", error_status=10)
        service = _service_for_test(session, "token")

        with self.assertRaisesRegex(RuntimeError, "unsupported language"):
            service.transcribe(
                np.zeros(1, dtype=np.float32),
                "model.gguf",
                "cpu",
                timestamps="word",
            )

        self.assertEqual(session.calls, [{"timestamps": "word"}])

    def test_words_to_srt_accepts_persisted_word_dicts(self):
        result = words_to_srt([
            {"text": "hello", "t0_ms": 0, "t1_ms": 420},
            {"text": "world", "t0_ms": 420, "t1_ms": 900},
        ])

        self.assertIn("00:00:00,000 --> 00:00:00,420", result)
        self.assertIn("hello", result)
        self.assertIn("world", result)

    def test_segments_to_srt_accepts_segment_timestamps(self):
        result = segments_to_srt([
            {"text": "hello world", "t0_ms": 0, "t1_ms": 900},
        ])

        self.assertIn("00:00:00,000 --> 00:00:00,900", result)
        self.assertIn("hello world", result)

    def test_word_export_rows_are_distinct_from_segment_rows(self):
        words = [
            {"text": "hello", "t0_ms": 0, "t1_ms": 300},
            {"text": "world", "t0_ms": 300, "t1_ms": 900},
        ]
        segments = [{"text": "hello world", "t0_ms": 0, "t1_ms": 900}]
        self.assertEqual(words_to_srt(words).count(" --> "), 2)
        self.assertEqual(segments_to_srt(segments).count(" --> "), 1)

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
