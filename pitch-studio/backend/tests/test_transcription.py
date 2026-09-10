from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from backend.transcription import MAX_VISUAL_DESCRIPTION_CHARS, max_volume_db, normalize_visual_description


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


class VisualDescriptionTests(unittest.TestCase):
    def test_removes_markdown_and_collapses_to_paragraph(self):
        source = "### What happens\n\n- Students build **robots**.\n- The film presents campus life."
        self.assertEqual(
            normalize_visual_description(source),
            "Students build robots. The film presents campus life.",
        )

    def test_limits_description_to_500_characters(self):
        result = normalize_visual_description("campus activity " * 100)
        self.assertLessEqual(len(result), MAX_VISUAL_DESCRIPTION_CHARS)


if __name__ == "__main__":
    unittest.main()
