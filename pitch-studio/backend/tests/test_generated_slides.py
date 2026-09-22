from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from backend.generated_slides import (
    generated_slide_file_key,
    save_generated_slide_image,
)


class GeneratedSlideStorageTests(unittest.TestCase):
    def test_file_key_is_content_addressed(self):
        digest = hashlib.sha256(b"slide").hexdigest()
        self.assertEqual(
            generated_slide_file_key(digest),
            f"generated-slides/{digest}.jpg",
        )

    def test_file_key_rejects_non_sha256_values(self):
        for value in ("", "../slide", "ABC", "f" * 63, "g" * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                generated_slide_file_key(value)

    @patch("backend.generated_slides.save_file")
    def test_save_uses_private_storage_abstraction(self, save_file):
        digest = hashlib.sha256(b"render").hexdigest()
        save_file.return_value = f"generated-slides/{digest}.jpg"

        result = save_generated_slide_image(digest, b"jpeg")

        self.assertEqual(result, f"generated-slides/{digest}.jpg")
        save_file.assert_called_once_with(
            f"generated-slides/{digest}.jpg",
            b"jpeg",
            "image/jpeg",
        )

    def test_save_rejects_empty_image(self):
        digest = hashlib.sha256(b"render").hexdigest()
        with self.assertRaises(ValueError):
            save_generated_slide_image(digest, b"")


if __name__ == "__main__":
    unittest.main()
