from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from backend.transcription import max_volume_db


class VolumeDetectionTests(unittest.TestCase):
    def test_reads_max_volume(self):
        result = mock.Mock(stderr="[Parsed_volumedetect_0] max_volume: -12.4 dB")
        with mock.patch("backend.transcription.subprocess.run", return_value=result):
            self.assertEqual(max_volume_db(Path("audio.mp3")), -12.4)

    def test_reads_digital_silence(self):
        result = mock.Mock(stderr="[Parsed_volumedetect_0] max_volume: -inf dB")
        with mock.patch("backend.transcription.subprocess.run", return_value=result):
            self.assertEqual(max_volume_db(Path("audio.mp3")), float("-inf"))

    def test_returns_none_when_volume_is_unavailable(self):
        result = mock.Mock(stderr="ffmpeg failed")
        with mock.patch("backend.transcription.subprocess.run", return_value=result):
            self.assertIsNone(max_volume_db(Path("audio.mp3")))


if __name__ == "__main__":
    unittest.main()
