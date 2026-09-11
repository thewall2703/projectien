from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.media_index import (
    build_context_index,
    build_recommendation_messages,
    derive_status,
    enqueue_job,
    is_stale,
    normalize_transcript,
    prepare_media,
    recommend,
    sha256_text,
)
from backend.schemas import RecipeOption


def recipe(
    ref: str = "A1-1",
    label: str = "School student",
    cluster: str = "A",
    duration: str = "T1",
    channel: str = "CH3",
    intent: str = "I2",
) -> RecipeOption:
    return RecipeOption(
        ref=ref,
        audience_label=label,
        audience_cluster=cluster,
        duration=duration,
        channel=channel,
        intent=intent,
        module_sequence="M01>M14",
    )


class HashAndTranscriptTests(unittest.TestCase):
    def test_sha256_is_stable(self):
        self.assertEqual(sha256_text("campus walk"), sha256_text("campus walk"))
        self.assertNotEqual(sha256_text("campus walk"), sha256_text("campus walk "))

    def test_parakeet_json_uses_text_field(self):
        raw = '{"text": "Welcome to campus.", "sentences": [{"text": "Welcome to campus."}]}'
        self.assertEqual(normalize_transcript(raw), "Welcome to campus.")

    def test_plain_transcript_is_unchanged(self):
        self.assertEqual(normalize_transcript("  spoken words  "), "  spoken words  ")


class StaleFlagTests(unittest.TestCase):
    def test_stale_when_vision_changes(self):
        row = SimpleNamespace(
            vision="show to demand",
            vision_hash=sha256_text("show to demand"),
            indexed_vision_hash=sha256_text("old vision"),
            recommendations_json='{"items":[]}',
        )
        self.assertTrue(is_stale(row))
        row.indexed_vision_hash = row.vision_hash
        self.assertFalse(is_stale(row))


class RecommendationPromptTests(unittest.TestCase):
    def test_prompt_includes_axes_persona_vision_and_feedback(self):
        messages = build_recommendation_messages(
            "video",
            "This is the campus film.",
            "",
            "show this to families",
            [recipe()],
            {
                "verdicts": {"A1-1": {"verdict": "yes", "note": "right audience"}},
                "added": [{"recipe_ref": "F5-1", "note": "alumni should see it"}],
            },
        )
        user = messages[1]["content"]
        self.assertIn("Demand", user)
        self.assertIn("A1-1", user)
        self.assertIn("School student", user)
        self.assertIn("show this to families", user)
        self.assertIn("This is the campus film.", user)
        self.assertIn("Human marked YES for A1-1", user)
        self.assertIn("ALSO applies to F5-1", user)

    def test_recommends_from_analysis_without_vision(self):
        messages = build_recommendation_messages(
            "video",
            "This is the campus film.",
            "Students walk through campus.",
            "",
            [recipe()],
        )
        self.assertIn("analysis only", messages[1]["content"])
        self.assertIn("This is the campus film.", messages[1]["content"])
        self.assertIn("analysis alone", messages[0]["content"])


class RecommendValidationTests(unittest.TestCase):
    def test_drops_unknown_refs_and_fills_axes(self):
        options = [recipe()]
        row = SimpleNamespace(
            media_kind="video",
            transcript="hello campus",
            visual_description="",
            vision="show to demand",
            vision_hash="abc",
            feedback_json="",
        )

        def fake_chat(_messages):
            return {
                "items": [
                    {"recipe_ref": "A1-1", "temperatures": ["X2", "NOPE"], "confidence": 0.9, "rationale": "fit"},
                    {"recipe_ref": "NOPE", "temperatures": ["X1"], "confidence": 0.8, "rationale": "bad"},
                ]
            }

        with mock.patch("backend.media_index.list_recipe_options", return_value=options):
            with mock.patch("backend.media_index.chat_json", side_effect=fake_chat):
                result = recommend(mock.Mock(), row)
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["recipe_ref"], "A1-1")
        self.assertEqual(item["audience_cluster"], "A")
        self.assertEqual(item["duration"], "T1")
        self.assertEqual(item["temperatures"], ["X2"])
        self.assertEqual(result["vision_hash"], "abc")


class DeriveStatusTests(unittest.TestCase):
    def test_ready_when_extract_exists_and_not_indexed(self):
        row = SimpleNamespace(
            status="draft",
            media_kind="photo",
            transcript="",
            visual_description="Politicians visiting campus.",
        )
        self.assertEqual(derive_status(row), "ready")

    def test_draft_when_no_extract(self):
        row = SimpleNamespace(status="draft", media_kind="video", transcript="", visual_description="")
        self.assertEqual(derive_status(row), "draft")

    def test_indexed_stays_indexed(self):
        row = SimpleNamespace(
            status="indexed",
            media_kind="video",
            transcript="Welcome to campus.",
            visual_description="",
        )
        self.assertEqual(derive_status(row), "indexed")

    def test_processing_when_job_active(self):
        row = SimpleNamespace(
            status="draft",
            media_kind="video",
            transcript="",
            visual_description="",
        )
        self.assertEqual(derive_status(row, SimpleNamespace(status="queued")), "processing")
        self.assertEqual(derive_status(row, SimpleNamespace(status="running")), "processing")
        self.assertEqual(derive_status(row, SimpleNamespace(status="done")), "draft")


class EnqueueJobTests(unittest.TestCase):
    def test_creates_queued_job(self):
        db = mock.Mock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        with mock.patch("backend.media_index._ensure_media_schema"):
            job = enqueue_job(db, "media_prepare", 3, 9)
        db.add.assert_called()
        added = db.add.call_args[0][0]
        self.assertEqual(added.job_type, "media_prepare")
        self.assertEqual(added.status, "queued")
        self.assertEqual(added.media_id, 3)
        self.assertEqual(added.asset_id, 9)
        db.commit.assert_called()
        self.assertIs(job, added)

    def test_returns_existing_active_job(self):
        existing = SimpleNamespace(id=9, status="running", media_id=1)
        db = mock.Mock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = existing
        with mock.patch("backend.media_index._ensure_media_schema"):
            job = enqueue_job(db, "media_prepare", 1, 4)
        self.assertIs(job, existing)
        db.add.assert_not_called()


class ContextIndexTests(unittest.TestCase):
    def test_attaches_vision_to_transcript(self):
        row = SimpleNamespace(
            media_kind="video",
            transcript="Welcome to campus.",
            visual_description="",
            vision="Show this to families deciding on a school.",
        )
        context = build_context_index(row)
        self.assertIn("Audio transcript:", context)
        self.assertIn("Welcome to campus.", context)
        self.assertIn("Vision:", context)
        self.assertIn("families deciding", context)

    def test_combines_video_audio_and_visual_context(self):
        row = SimpleNamespace(
            media_kind="video",
            transcript="[No spoken audio detected in this video.]",
            visual_description="Students collaborate in a classroom and celebrate at graduation.",
            vision="",
        )
        context = build_context_index(row)
        self.assertIn("Audio transcript:", context)
        self.assertIn("Visual narrative and relevance:", context)
        self.assertIn("graduation", context)


class PrepareMediaTests(unittest.TestCase):
    def test_downloads_transcribes_and_stores_on_prepare(self):
        asset = SimpleNamespace(
            id=4,
            type="video",
            source_url="https://www.youtube.com/watch?v=abc",
            file_status="pending",
            file_key="",
        )
        row = SimpleNamespace(
            asset_id=4,
            media_kind="video",
            transcript="",
            visual_description="",
            vision="",
            vision_frozen=False,
            vision_hash="",
            recommendations_json="",
            status="draft",
        )
        db = mock.Mock()
        db.get.return_value = asset

        def sync_youtube(stored_asset):
            stored_asset.file_status = "stored"
            stored_asset.file_key = "videos/campus.mp4"
            return "Welcome to campus."

        with mock.patch("backend.media_index.get_or_create_media_index", return_value=row):
            with mock.patch("backend.sync_assets.sync_youtube", side_effect=sync_youtube) as sync:
                with mock.patch("backend.sync_assets.classify_link", return_value="youtube"):
                    with mock.patch(
                        "backend.transcription.analyze_video_asset",
                        return_value=SimpleNamespace(
                            transcript="Welcome to campus.",
                            visual_description="Students walk through campus.",
                        ),
                    ):
                        with mock.patch("backend.media_index.delete_file"):
                            with mock.patch("backend.media_index.apply_recommendations") as recommend:
                                prepared = prepare_media(db, 4)
        sync.assert_called_once_with(asset)
        recommend.assert_called_once_with(db, row)
        self.assertEqual(prepared.transcript, "Welcome to campus.")
        self.assertEqual(prepared.visual_description, "Students walk through campus.")
        db.commit.assert_called()

    def test_analyzes_already_stored_drive_video(self):
        asset = SimpleNamespace(
            id=8,
            type="video",
            source_url="https://drive.google.com/file/d/abc/view",
            file_status="stored",
            file_key="videos/campus.mp4",
            sync_error="",
        )
        row = SimpleNamespace(
            asset_id=8,
            media_kind="video",
            transcript="",
            visual_description="",
            vision="",
            vision_frozen=False,
            vision_hash="",
            recommendations_json="",
            status="draft",
        )
        db = mock.Mock()
        db.get.return_value = asset
        with mock.patch("backend.media_index.get_or_create_media_index", return_value=row):
            with mock.patch("backend.sync_assets.classify_link", return_value="drive_file"):
                with mock.patch("backend.media_index.delete_file") as delete:
                    with mock.patch(
                        "backend.transcription.analyze_video_asset",
                        return_value=SimpleNamespace(
                            transcript="Welcome to the campus.",
                            visual_description="A guided campus tour.",
                        ),
                    ) as analyze:
                        with mock.patch("backend.media_index.apply_recommendations") as recommend:
                            prepared = prepare_media(db, 8)
        analyze.assert_called_once_with(asset, "", None)
        recommend.assert_called_once_with(db, row)
        delete.assert_called_once_with("videos/campus.mp4")
        self.assertEqual(asset.file_key, "")
        self.assertEqual(asset.file_status, "processed")
        self.assertEqual(asset.url, asset.source_url)
        self.assertEqual(prepared.transcript, "Welcome to the campus.")
        self.assertEqual(prepared.visual_description, "A guided campus tour.")
        db.commit.assert_called()

    def test_processed_video_is_not_downloaded_again(self):
        asset = SimpleNamespace(
            id=9,
            type="video",
            source_url="https://drive.google.com/file/d/abc/view",
            file_status="processed",
            file_key="",
            sync_error="",
        )
        row = SimpleNamespace(
            asset_id=9,
            media_kind="video",
            transcript="Spoken words.",
            visual_description="Students work together.",
            vision="",
            vision_frozen=False,
            vision_hash="",
            recommendations_json="",
            status="draft",
        )
        db = mock.Mock()
        db.get.return_value = asset
        with mock.patch("backend.media_index.get_or_create_media_index", return_value=row):
            with mock.patch("backend.sync_assets.classify_link", return_value="drive_file"):
                with mock.patch("backend.sync_assets.sync_one") as sync:
                    with mock.patch("backend.transcription.analyze_video_asset") as analyze:
                        with mock.patch("backend.media_index.apply_recommendations") as recommend:
                            prepare_media(db, 9)
        sync.assert_not_called()
        analyze.assert_not_called()
        recommend.assert_called_once_with(db, row)

    def test_photo_original_is_removed_after_thumbnail_and_description(self):
        parent = SimpleNamespace(id=10, type="photo", title="Campus photos")
        child = SimpleNamespace(
            id=11,
            file_key="library/photos/campus.jpg",
            file_status="stored",
            content_type="image/jpeg",
            url="",
        )
        row = SimpleNamespace(
            asset_id=10,
            media_kind="photo",
            transcript="",
            visual_description="",
            image_keys="",
            vision="",
            vision_frozen=False,
            vision_hash="",
            recommendations_json="",
            status="draft",
        )
        db = mock.Mock()
        db.get.return_value = parent
        with mock.patch("backend.media_index.get_or_create_media_index", return_value=row):
            with mock.patch("backend.sync_assets.sync_one"):
                with mock.patch("backend.media_index.list_image_assets", return_value=[child]):
                    with mock.patch("backend.thumbnails.ensure_thumbnails", return_value=(1, [])):
                        with mock.patch(
                            "backend.media_index.collect_image_bytes",
                            return_value=([b"image"], ["library/photos/campus.jpg"]),
                        ):
                            with mock.patch(
                                "backend.media_index.describe_images",
                                return_value="Students gather on campus.",
                            ):
                                with mock.patch(
                                    "backend.thumbnails.thumbnail_key",
                                    return_value="thumbnails/assets/11.jpg",
                                ):
                                    with mock.patch("backend.media_index.file_exists", return_value=True):
                                        with mock.patch("backend.media_index.delete_file") as delete:
                                            with mock.patch(
                                                "backend.media_index.apply_recommendations"
                                            ) as recommend:
                                                prepare_media(db, 10)
        recommend.assert_called_once_with(db, row)
        delete.assert_called_once_with("library/photos/campus.jpg")
        self.assertEqual(child.file_key, "")
        self.assertEqual(child.file_status, "preview")
        self.assertEqual(child.url, "/api/assets/11/thumbnail.jpg")
        self.assertEqual(json.loads(row.image_keys), ["thumbnails/assets/11.jpg"])


if __name__ == "__main__":
    unittest.main()
