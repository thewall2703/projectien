from __future__ import annotations

import unittest
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from backend.thumbnails import ensure_thumbnail, thumbnail_jpeg, thumbnail_key


def image_bytes(size: tuple[int, int] = (2400, 1600)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "navy").save(output, "JPEG")
    return output.getvalue()


class ThumbnailTests(unittest.TestCase):
    def test_key_is_stable_per_asset(self):
        asset = SimpleNamespace(
            id=42,
            synced_at=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(thumbnail_key(asset), "thumbnails/assets/42.jpg")

    def test_thumbnail_fits_preview_bounds(self):
        result = thumbnail_jpeg(image_bytes())
        image = Image.open(BytesIO(result))
        self.assertEqual(image.format, "JPEG")
        self.assertEqual(image.size, (640, 427))

    def test_ensure_thumbnail_persists_missing_preview(self):
        asset = SimpleNamespace(
            id=8,
            synced_at=None,
            file_key="assets/8/original.jpg",
            content_type="image/jpeg",
        )
        source = image_bytes()
        with mock.patch("backend.thumbnails.file_exists", return_value=False):
            with mock.patch("backend.thumbnails.read_file", return_value=source):
                with mock.patch("backend.thumbnails.save_file") as save:
                    key = ensure_thumbnail(asset)
        self.assertEqual(key, "thumbnails/assets/8.jpg")
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], key)
        self.assertEqual(save.call_args.args[2], "image/jpeg")

    def test_ensure_thumbnail_skips_existing_preview(self):
        asset = SimpleNamespace(
            id=8,
            synced_at=None,
            file_key="assets/8/original.jpg",
            content_type="image/jpeg",
        )
        with mock.patch("backend.thumbnails.resolve_thumbnail_key", return_value="thumbnails/assets/8.jpg"):
            with mock.patch("backend.thumbnails.read_file") as read:
                self.assertEqual(ensure_thumbnail(asset), "thumbnails/assets/8.jpg")
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
