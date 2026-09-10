from __future__ import annotations

import unittest

from backend.asset_map import infer_matrix_ref, infer_module_ids
from backend.sync_assets import (
    classify_link,
    extract_drive_file_id,
    extract_folder_id,
    is_downloadable_file,
    should_descend_folder,
)


class ClassifyLinkTests(unittest.TestCase):
    def test_empty_is_gap(self):
        self.assertEqual(classify_link(""), "gap")
        self.assertEqual(classify_link(None), "gap")  # type: ignore[arg-type]

    def test_figma_is_external(self):
        self.assertEqual(
            classify_link("https://www.figma.com/design/LLYTAzhCqnzO4MzL5gMDJV/Brand-Deck"),
            "external",
        )

    def test_youtube_watch_and_short_links(self):
        self.assertEqual(classify_link("https://www.youtube.com/watch?v=N5Crw6YCkSU"), "youtube")
        self.assertEqual(classify_link("https://youtu.be/VIzWHj8FrXA?si=abc"), "youtube")
        self.assertEqual(classify_link("https://www.youtube.com/shorts/abc123"), "youtube")

    def test_cdn_pdf(self):
        self.assertEqual(
            classify_link("https://files.mastersunion.link/Entrepreneurship_Report.pdf"),
            "cdn",
        )
        self.assertEqual(
            classify_link("https://prsindia.org/files/bills_acts/bills_states/haryana/2026/Bill17of2026HR.pdf"),
            "cdn",
        )

    def test_drive_file(self):
        url = "https://drive.google.com/file/d/17yPb5fyLLA18rzV9xKZiZPwqEGwB-hJV/view"
        self.assertEqual(classify_link(url), "drive_file")
        self.assertEqual(extract_drive_file_id(url), "17yPb5fyLLA18rzV9xKZiZPwqEGwB-hJV")

    def test_drive_folder(self):
        url = "https://drive.google.com/drive/u/0/folders/19cw1nnKP-DDPxAjGCgT9HKk4wrZpV1yO"
        self.assertEqual(classify_link(url), "drive_folder")
        self.assertEqual(extract_folder_id(url), "19cw1nnKP-DDPxAjGCgT9HKk4wrZpV1yO")

    def test_nested_folder_filters(self):
        self.assertTrue(is_downloadable_file("Preview.pdf", "application/pdf"))
        self.assertTrue(is_downloadable_file("Cover.png", "image/png"))
        self.assertFalse(is_downloadable_file("Notes.gdoc", "application/vnd.google-apps.document"))
        self.assertFalse(should_descend_folder("Font files"))
        self.assertTrue(should_descend_folder("Preview PDF"))
        self.assertFalse(is_downloadable_file("Inter.ttf", "font/ttf"))


class AssetMapTests(unittest.TestCase):
    def test_placement_maps_to_outcomes(self):
        self.assertEqual(infer_module_ids("report", "PGP TBM Placement Report 2025"), "M07,M13")
        self.assertEqual(infer_matrix_ref("report", "PGP TBM Placement Report 2025"), "R-01")

    def test_haryana_act(self):
        self.assertEqual(
            infer_module_ids("report", "Haryana Private Universities Act (Amendment 2026)"),
            "M02,M13",
        )

    def test_video_has_no_mapping(self):
        self.assertEqual(infer_module_ids("video", "Campus Film"), "")


if __name__ == "__main__":
    unittest.main()
