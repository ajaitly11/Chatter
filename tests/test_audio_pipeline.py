import unittest

import numpy as np

from chatter.audio_capture import _resample_to_asr_rate
from chatter.hotkey import _trim_silence


class AudioPipelineTests(unittest.TestCase):
    def test_silence_is_rejected_before_model_inference(self):
        pcm = np.zeros(16_000, dtype=np.float32)
        self.assertIsNone(_trim_silence(pcm, 16_000))

    def test_speech_energy_is_kept_with_a_small_boundary_margin(self):
        pcm = np.zeros(16_000, dtype=np.float32)
        t = np.arange(4_800, dtype=np.float32) / 16_000
        pcm[5_600:10_400] = 0.05 * np.sin(2 * np.pi * 220 * t)

        trimmed = _trim_silence(pcm, 16_000)

        self.assertIsNotNone(trimmed)
        self.assertLess(len(trimmed), len(pcm))
        self.assertGreater(len(trimmed), 4_800)

    def test_quiet_speech_is_not_rejected_as_silence(self):
        pcm = np.zeros(16_000, dtype=np.float32)
        t = np.arange(4_800, dtype=np.float32) / 16_000
        pcm[5_600:10_400] = 0.01 * np.sin(2 * np.pi * 220 * t)

        self.assertIsNotNone(_trim_silence(pcm, 16_000))

    def test_very_quiet_device_speech_is_still_kept(self):
        pcm = np.zeros(16_000, dtype=np.float32)
        t = np.arange(4_800, dtype=np.float32) / 16_000
        pcm[5_600:10_400] = 0.005 * np.sin(2 * np.pi * 220 * t)

        self.assertIsNotNone(_trim_silence(pcm, 16_000))

    def test_native_48khz_chunk_is_resampled_to_16khz(self):
        native = np.linspace(-1.0, 1.0, 9_600, dtype=np.float32)

        pcm = _resample_to_asr_rate(native, 48_000)

        self.assertEqual(len(pcm), 3_200)
        self.assertEqual(pcm.dtype, np.float32)
        self.assertAlmostEqual(float(pcm[0]), -1.0, places=4)


if __name__ == "__main__":
    unittest.main()
