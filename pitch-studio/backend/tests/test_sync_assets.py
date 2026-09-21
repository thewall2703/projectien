from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.asset_map import infer_matrix_ref, infer_module_ids
from backend.models import Asset
from backend.sync_assets import (
    FOLDER_MIME,
    child_cap_for,
    classify_link,
    has_local_copy,
    sync_one,
    extract_drive_file_id,
    extract_folder_id,
    is_downloadable_file,
    list_drive_documents,
    _stream_limited,
    should_descend_folder,
    take_fair_share,
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


def fake_drive(tree: dict[str, list[dict]]):
    """Build a stub Drive service from {folder_id: [child, ...]}."""

    def list_page(_service, folder_id: str) -> list[dict]:
        return tree.get(folder_id, [])

    return list_page


def folder(node_id: str, name: str) -> dict:
    return {"id": node_id, "name": name, "mimeType": FOLDER_MIME}


def image(node_id: str, name: str) -> dict:
    return {"id": node_id, "name": name, "mimeType": "image/jpeg"}


class FolderWalkTests(unittest.TestCase):
    def test_descends_past_a_wide_top_level_folder(self):
        # 100 files at the top used to exhaust the old cap before the walk
        # ever reached `deep`, so nested files were silently dropped.
        tree = {
            "root": [folder("wide", "Wide"), folder("deep", "Deep")],
            "wide": [image(f"w{i}", f"wide-{i:03d}.jpg") for i in range(100)],
            "deep": [folder("deeper", "Deeper")],
            "deeper": [image("d1", "buried.jpg")],
        }
        with mock.patch("backend.sync_assets._list_folder_page", fake_drive(tree)):
            documents = list_drive_documents(None, "root")
        relpaths = {doc["relpath"] for doc in documents}
        self.assertEqual(len(documents), 101)
        self.assertIn("Deep/Deeper/buried.jpg", relpaths)

    def test_cap_samples_every_folder_instead_of_truncating(self):
        tree = {
            "root": [folder("a", "A"), folder("b", "B")],
            "a": [image(f"a{i}", f"a-{i:03d}.jpg") for i in range(50)],
            "b": [image(f"b{i}", f"b-{i:03d}.jpg") for i in range(50)],
        }
        with mock.patch("backend.sync_assets._list_folder_page", fake_drive(tree)):
            documents = list_drive_documents(None, "root", cap=10)
        self.assertEqual(len(documents), 10)
        self.assertEqual(sum(1 for doc in documents if doc["relpath"].startswith("A/")), 5)
        self.assertEqual(sum(1 for doc in documents if doc["relpath"].startswith("B/")), 5)

    def test_font_folders_are_still_skipped(self):
        tree = {
            "root": [folder("f", "Font files")],
            "f": [image("i1", "glyph.png")],
        }
        with mock.patch("backend.sync_assets._list_folder_page", fake_drive(tree)):
            self.assertEqual(list_drive_documents(None, "root"), [])


class FairShareTests(unittest.TestCase):
    def test_returns_everything_when_under_cap(self):
        by_folder = {"a": [{"relpath": "a/1"}], "b": [{"relpath": "b/1"}]}
        self.assertEqual(len(take_fair_share(by_folder, 10)), 2)
        self.assertEqual(len(take_fair_share(by_folder, None)), 2)

    def test_drains_small_folders_before_exhausting_the_large_one(self):
        by_folder = {
            "big": [{"relpath": f"big/{i}"} for i in range(20)],
            "small": [{"relpath": "small/0"}],
        }
        picked = take_fair_share(by_folder, 5)
        self.assertEqual(len(picked), 5)
        self.assertIn({"relpath": "small/0"}, picked)


class ChildCapTests(unittest.TestCase):
    def test_convocation_sets_stay_capped(self):
        self.assertEqual(child_cap_for("Convocation 2025"), 80)
        self.assertEqual(child_cap_for("Luminaries on Convocation"), 80)

    def test_every_other_set_syncs_whole(self):
        self.assertIsNone(child_cap_for("CXO Photos"))
        self.assertIsNone(child_cap_for("Prospectus (Drive), Prospectus (Figma)"))


class SettledStatusTests(unittest.TestCase):
    """prepare_media deletes originals on purpose; sync must not undo that."""

    def asset(self, **kwargs) -> Asset:
        defaults = {
            "type": "photo",
            "title": "Some Set — a.jpg",
            "source_url": "https://drive.google.com/file/d/abc123/view",
            "file_status": "pending",
            "file_key": "",
        }
        return Asset(**{**defaults, **kwargs})

    def test_previewed_photo_is_left_alone(self):
        asset = self.asset(file_status="preview")
        self.assertTrue(has_local_copy(asset))
        self.assertEqual(sync_one(None, asset), "skipped")

    def test_processed_video_is_left_alone(self):
        asset = self.asset(type="video", file_status="processed")
        self.assertTrue(has_local_copy(asset))
        self.assertEqual(sync_one(None, asset), "skipped")

    def test_force_does_not_refetch_settled_media(self):
        for status in ("preview", "processed"):
            with self.subTest(status=status):
                self.assertEqual(sync_one(None, self.asset(file_status=status), force=True), "skipped")

    def test_stored_without_a_key_is_still_unfinished(self):
        self.assertFalse(has_local_copy(self.asset(file_status="stored")))
        self.assertTrue(has_local_copy(self.asset(file_status="stored", file_key="library/photos/a.jpg")))


class StreamingDownloadTests(unittest.TestCase):
    def test_streams_chunks_to_disk(self):
        response = mock.Mock()
        response.headers = {"content-length": "6"}
        response.iter_content.return_value = iter([b"abc", b"", b"def"])
        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "video.mp4"
            _stream_limited(response, destination, "video.mp4", limit=10)
            self.assertEqual(destination.read_bytes(), b"abcdef")

    def test_rejects_oversized_content_length_before_download(self):
        response = mock.Mock()
        response.headers = {"content-length": "11"}
        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "video.mp4"
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                _stream_limited(response, destination, "video.mp4", limit=10)
            response.iter_content.assert_not_called()

    def test_stops_when_stream_exceeds_limit(self):
        response = mock.Mock()
        response.headers = {}
        response.iter_content.return_value = iter([b"12345", b"678901"])
        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "video.mp4"
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                _stream_limited(response, destination, "video.mp4", limit=10)


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
