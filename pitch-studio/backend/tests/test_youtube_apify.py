from __future__ import annotations

import unittest

from backend.youtube_apify import (
    YouTubeDownloadError,
    _format_unavailable,
    _quality_attempts,
    canonical_youtube_url,
    is_youtube_url,
    parse_download_item,
    pick_subtitle_url,
    srt_to_text,
)


class YouTubeUrlTests(unittest.TestCase):
    def test_recognises_watch_and_share_links(self):
        self.assertTrue(is_youtube_url("https://www.youtube.com/watch?v=N5Crw6YCkSU"))
        self.assertTrue(is_youtube_url("https://youtu.be/VIzWHj8FrXA"))
        self.assertFalse(is_youtube_url("https://files.mastersunion.link/film.mp4"))
        self.assertFalse(is_youtube_url(""))

    def test_canonicalises_share_and_shorts_links(self):
        self.assertEqual(
            canonical_youtube_url("https://youtu.be/VIzWHj8FrXA?si=abc"),
            "https://www.youtube.com/watch?v=VIzWHj8FrXA",
        )
        self.assertEqual(
            canonical_youtube_url("https://www.youtube.com/shorts/abc123xyz01"),
            "https://www.youtube.com/watch?v=abc123xyz01",
        )


class ApifyResultTests(unittest.TestCase):
    def test_reads_highest_quality_media_url(self):
        parsed = parse_download_item(
            {
                "ok": True,
                "title": "Campus Film",
                "resolution": "3840x2160",
                "requestedQuality": "4320",
                "mediaKey": "abc.mp4",
                "mediaUrl": "https://api.apify.com/v2/key-value-stores/x/records/abc.mp4",
                "fileSizeBytes": 120000000,
            }
        )
        self.assertEqual(parsed["resolution"], "3840x2160")
        self.assertTrue(parsed["media_url"].endswith("abc.mp4"))

    def test_failed_item_raises(self):
        with self.assertRaises(YouTubeDownloadError):
            parse_download_item({"ok": False, "error": "private video"})

    def test_missing_media_url_raises(self):
        with self.assertRaises(YouTubeDownloadError):
            parse_download_item({"ok": True, "title": "x"})

    def test_keeps_subtitle_links(self):
        parsed = parse_download_item(
            {
                "ok": True,
                "mediaUrl": "https://api.apify.com/v2/key-value-stores/x/records/abc.mp4",
                "subtitles": [{"key": "en.srt", "url": "https://example.com/en.srt"}],
            }
        )
        self.assertEqual(parsed["subtitles"][0]["url"], "https://example.com/en.srt")


class CaptionParseTests(unittest.TestCase):
    def test_srt_to_text_strips_timecodes(self):
        raw = "1\n00:00:00,000 --> 00:00:01,500\nWelcome to campus.\n\n2\n00:00:01,500 --> 00:00:03,000\nThis is the walk.\n"
        self.assertEqual(srt_to_text(raw), "Welcome to campus. This is the walk.")

    def test_prefers_english_subtitle(self):
        url = pick_subtitle_url(
            [
                {"key": "hi.srt", "url": "https://example.com/hi.srt"},
                {"key": "en.srt", "url": "https://example.com/en.srt"},
            ]
        )
        self.assertEqual(url, "https://example.com/en.srt")

    def test_quality_ladder_starts_at_requested(self):
        attempts = _quality_attempts("2160")
        self.assertEqual(attempts[:3], ["2160", "1080", "720"])
        self.assertGreaterEqual(attempts.count("720"), 3)

    def test_recognises_missing_format_error(self):
        self.assertTrue(_format_unavailable("Requested format is not available. Use --list-formats"))


if __name__ == "__main__":
    unittest.main()
