"""Offline unit checks for helpers used by the UI pipeline."""

from __future__ import annotations

import tempfile
import subprocess
import threading
import unittest
from pathlib import Path

from drive_auth import _finish_partial, _truncate_partial, extract_file_id
from transcription_pipeline import (
    extract_audio_from_video,
    format_timestamp,
    job_complete,
    parse_sheet,
    slugify,
    validate_outputs,
    write_srt,
    write_vtt,
)


class _Sentence:
    def __init__(self, text: str, start: float, end: float):
        self.text = text
        self.start = start
        self.end = end
        self.tokens = []


class HelperTests(unittest.TestCase):
    def test_extract_file_id(self):
        self.assertEqual(
            extract_file_id("https://drive.google.com/file/d/abc123XYZ_-/view?usp=sharing"),
            "abc123XYZ_-",
        )
        self.assertEqual(
            extract_file_id("https://drive.google.com/open?id=abc123XYZ_-"),
            "abc123XYZ_-",
        )
        self.assertEqual(extract_file_id("abc123XYZ_-abcdefghi"), "abc123XYZ_-abcdefghi")
        self.assertIsNone(extract_file_id("not-a-link"))

    def test_oversized_partial_is_truncated_to_expected_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial = root / "video.mp4.part"
            destination = root / "video.mp4"
            partial.write_bytes(b"abcdefghij")  # 10 bytes, file is only 6
            _truncate_partial(partial, 6)
            self.assertEqual(partial.stat().st_size, 6)
            _finish_partial(partial, destination, expected_size=6)
            self.assertTrue(destination.exists())
            self.assertEqual(destination.read_bytes(), b"abcdef")
            self.assertFalse(partial.exists())

    def test_public_helpers_exist(self):
        from drive_auth import get_public_file_metadata, open_public_drive_stream

        self.assertTrue(callable(get_public_file_metadata))
        self.assertTrue(callable(open_public_drive_stream))

    def test_slugify(self):
        self.assertEqual(slugify("My Cool Video!.mp4"), "my-cool-video")
        self.assertTrue(slugify("@@@").startswith("video") or slugify("@@@") == "video")

    def test_timestamps(self):
        self.assertEqual(format_timestamp(3661.5), "01:01:01,500")
        self.assertEqual(format_timestamp(3661.5, vtt=True), "01:01:01.500")

    def test_parse_sheet_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "links.csv"
            path.write_text(
                "notes,link\n"
                "a,https://drive.google.com/file/d/FILEONE123456789012/view\n"
                "b,https://drive.google.com/file/d/FILETWO123456789012/view\n",
                encoding="utf-8",
            )
            urls = parse_sheet(path)
            self.assertEqual(len(urls), 2)

    def test_output_bundle_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            name = "demo"
            sentences = [_Sentence("Hello world.", 0.0, 1.2)]
            (out / f"{name}.txt").write_text("Hello world.\n", encoding="utf-8")
            write_srt(sentences, out / f"{name}.srt")
            write_vtt(sentences, out / f"{name}.vtt")
            (out / f"{name}.json").write_text('{"text":"Hello world."}\n', encoding="utf-8")
            validate_outputs(out, name)
            self.assertTrue(job_complete(out, name))

    def test_seekable_video_audio_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "sample.mp4"
            audio = root / "sample.opus"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x240:d=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=1000:duration=1",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(video),
                ],
                check=True,
            )
            extract_audio_from_video(video, audio, threading.Event())
            self.assertTrue(audio.exists())
            self.assertGreater(audio.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
