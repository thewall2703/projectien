from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.media_index import (
    build_context_index,
    build_recommendation_messages,
    derive_status,
    drive_file_id,
    drive_image_url,
    enqueue_job,
    feedback_boosts_for_recipe,
    is_stale,
    normalize_transcript,
    pick_recommended_media,
    prepare_media,
    recommend,
    record_video_click,
    sha256_text,
)
from backend.models import Asset, MediaIndex, VideoClickEvent
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


def _rec_row(
    asset_id: int,
    kind: str,
    items: list[dict],
    verdicts: dict | None = None,
    added: list | None = None,
):
    return SimpleNamespace(
        asset_id=asset_id,
        media_kind=kind,
        recommendations_json=json.dumps({"items": items}),
        feedback_json=json.dumps({"verdicts": verdicts or {}, "added": added or []}),
    )


def _asset(asset_id: int, title: str, source: str, asset_type: str = "video"):
    return SimpleNamespace(
        id=asset_id,
        title=title,
        source_url=source,
        url="",
        type=asset_type,
        content_type="video/mp4" if asset_type == "video" else "image/jpeg",
        file_status="stored",
    )


def _query_router(side_effects: dict):
    """Route SQLAlchemy-style mock queries by model class."""

    def query(model):
        chain = mock.Mock()
        result = side_effects.get(model, [])
        if callable(result):
            result = result()
        chain.filter.return_value = chain
        chain.order_by.return_value = chain
        chain.all.return_value = result
        chain.first.return_value = result[0] if result else None
        chain.count.return_value = len(result) if isinstance(result, list) else 0
        return chain

    db = mock.Mock()
    db.query.side_effect = query
    return db


class RecommendedMediaPickTests(unittest.TestCase):
    def test_drive_image_url_uses_file_id(self):
        self.assertEqual(drive_file_id("https://drive.google.com/file/d/abc123/view"), "abc123")
        self.assertEqual(
            drive_image_url("https://drive.google.com/file/d/abc123/view"),
            "https://drive.google.com/uc?export=view&id=abc123",
        )

    def test_returns_all_linked_videos_ranked_by_fit(self):
        rows = [
            _rec_row(1, "video", [{"recipe_ref": "A1-1", "temperatures": ["X4"], "confidence": 0.99, "rationale": "hot"}]),
            _rec_row(2, "video", [{"recipe_ref": "A1-1", "temperatures": ["X2"], "confidence": 0.4, "rationale": "fit"}]),
            _rec_row(3, "video", [{"recipe_ref": "B2-1", "temperatures": ["X2"], "confidence": 0.9, "rationale": "other"}]),
        ]
        assets = [
            _asset(1, "Hot film", "https://youtu.be/one"),
            _asset(2, "Fit film", "https://youtu.be/two"),
            _asset(3, "Other film", "https://youtu.be/three"),
            _asset(4, "Unscored film", "https://youtu.be/four"),
            _asset(5, "No link", ""),
        ]
        db = _query_router(
            {
                VideoClickEvent: [],
                Asset: assets,
                MediaIndex: rows,
            }
        )
        with mock.patch("backend.media_index._ensure_media_schema"):
            videos, pictures = pick_recommended_media(db, "A1-1", "X2")
        self.assertEqual([item.asset_id for item in videos], [2, 1, 3, 4])
        self.assertEqual(pictures, [])

    def test_rejected_videos_stay_visible_but_rank_lower(self):
        rows = [
            _rec_row(
                1,
                "video",
                [{"recipe_ref": "A1-1", "temperatures": ["X2"], "confidence": 0.9, "rationale": "nope"}],
                verdicts={"A1-1": {"verdict": "no"}},
            ),
            _rec_row(2, "video", [], added=[{"recipe_ref": "A1-1", "note": "missed"}]),
        ]
        assets = [_asset(1, "Rejected", "https://youtu.be/a"), _asset(2, "Added", "https://youtu.be/b")]
        db = _query_router({VideoClickEvent: [], Asset: assets, MediaIndex: rows})
        with mock.patch("backend.media_index._ensure_media_schema"):
            videos, _pictures = pick_recommended_media(db, "A1-1", "X2")
        self.assertEqual([item.asset_id for item in videos], [2, 1])

    def test_expands_photo_sets_round_robin_and_caps(self):
        rows = [
            _rec_row(10, "photo", [{"recipe_ref": "A1-1", "temperatures": ["X2"], "confidence": 0.8, "rationale": "a"}]),
            _rec_row(20, "photo", [{"recipe_ref": "A1-1", "temperatures": ["X2"], "confidence": 0.7, "rationale": "b"}]),
        ]
        parents = [
            _asset(10, "Campus", "https://drive.google.com/file/d/p1/view", "photo"),
            _asset(20, "Studio", "https://drive.google.com/file/d/p2/view", "photo"),
        ]
        children = {
            10: [
                _asset(11, "Campus — 1", "https://drive.google.com/file/d/c1/view", "photo"),
                _asset(12, "Campus — 2", "https://drive.google.com/file/d/c2/view", "photo"),
            ],
            20: [
                _asset(21, "Studio — 1", "https://drive.google.com/file/d/s1/view", "photo"),
                _asset(22, "Studio — 2", "https://drive.google.com/file/d/s2/view", "photo"),
                _asset(23, "Studio — 3", "https://drive.google.com/file/d/s3/view", "photo"),
                _asset(24, "Studio — 4", "https://drive.google.com/file/d/s4/view", "photo"),
                _asset(25, "Studio — 5", "https://drive.google.com/file/d/s5/view", "photo"),
            ],
        }

        def asset_query():
            # First Asset query is videos (empty); later photo parents via MediaIndex ids.
            return parents

        call_assets = {"n": 0}

        def query(model):
            chain = mock.Mock()
            chain.filter.return_value = chain
            chain.order_by.return_value = chain
            if model is VideoClickEvent:
                chain.all.return_value = []
            elif model is Asset:
                call_assets["n"] += 1
                # video pass first, then photo parents
                chain.all.return_value = [] if call_assets["n"] == 1 else parents
            else:
                chain.all.return_value = rows
            return chain

        db = mock.Mock()
        db.query.side_effect = query
        with mock.patch("backend.media_index._ensure_media_schema"):
            with mock.patch(
                "backend.media_index.list_image_assets",
                side_effect=lambda _db, asset: children[asset.id],
            ):
                videos, pictures = pick_recommended_media(db, "A1-1", "X2")
        self.assertEqual(videos, [])
        self.assertEqual(len(pictures), 5)
        self.assertEqual([item.asset_id for item in pictures], [11, 21, 12, 22, 23])
        self.assertTrue(pictures[0].preview_url.endswith("id=c1"))
        self.assertEqual(pictures[0].thumbnail_url, "/api/assets/11/thumbnail.jpg")

    def test_short_and_long_sessions_return_full_catalog(self):
        rows = [
            _rec_row(1, "video", [{"recipe_ref": "A1-1", "temperatures": ["X2"], "confidence": 0.9, "rationale": "exact"}]),
            _rec_row(2, "video", [{"recipe_ref": "B2-1", "temperatures": ["X2"], "confidence": 0.8, "rationale": "other"}]),
        ]
        assets = [
            _asset(1, "Exact film", "https://youtu.be/one"),
            _asset(2, "Other film", "https://youtu.be/two"),
            _asset(3, "Unscored", "https://youtu.be/three"),
        ]
        db = _query_router({VideoClickEvent: [], Asset: assets, MediaIndex: rows})
        with mock.patch("backend.media_index._ensure_media_schema"):
            short, _ = pick_recommended_media(db, "A1-1", "X2", duration="T1")
            long, _ = pick_recommended_media(db, "A1-1", "X2", duration="T5")
        self.assertEqual([item.asset_id for item in short], [1, 2, 3])
        self.assertEqual([item.asset_id for item in long], [1, 2, 3])

    def test_feedback_boost_requires_three_generations(self):
        events = [
            SimpleNamespace(generation_id=1, asset_id=9, click_order=1),
            SimpleNamespace(generation_id=2, asset_id=9, click_order=1),
        ]
        db = _query_router({VideoClickEvent: events})
        with mock.patch("backend.media_index._ensure_media_schema"):
            self.assertEqual(feedback_boosts_for_recipe(db, "A1-1"), {})
        events.append(SimpleNamespace(generation_id=3, asset_id=9, click_order=1))
        with mock.patch("backend.media_index._ensure_media_schema"):
            boosts = feedback_boosts_for_recipe(db, "A1-1")
        self.assertAlmostEqual(boosts[9], 0.35)

    def test_bounded_feedback_cannot_outrank_exact_match(self):
        rows = [
            _rec_row(1, "video", [{"recipe_ref": "A1-1", "temperatures": ["X2"], "confidence": 0.9, "rationale": "exact"}]),
            _rec_row(2, "video", [{"recipe_ref": "B2-1", "temperatures": ["X2"], "confidence": 0.8, "rationale": "other"}]),
        ]
        assets = [
            _asset(1, "Exact film", "https://youtu.be/one"),
            _asset(2, "Popular other", "https://youtu.be/two"),
        ]
        events = [
            SimpleNamespace(generation_id=1, asset_id=2, click_order=1),
            SimpleNamespace(generation_id=2, asset_id=2, click_order=1),
            SimpleNamespace(generation_id=3, asset_id=2, click_order=1),
        ]
        db = _query_router({VideoClickEvent: events, Asset: assets, MediaIndex: rows})
        with mock.patch("backend.media_index._ensure_media_schema"):
            videos, _ = pick_recommended_media(db, "A1-1", "X2")
        self.assertEqual(videos[0].asset_id, 1)
        self.assertEqual(videos[1].asset_id, 2)


class VideoClickRecordTests(unittest.TestCase):
    def test_assigns_sequential_click_order_and_is_idempotent(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from backend.database import Base
        from backend.models import VideoClickEvent

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine, tables=[VideoClickEvent.__table__])
        Session = sessionmaker(bind=engine)
        db = Session()
        try:
            first, created = record_video_click(
                db,
                generation_id=10,
                user_id=7,
                asset_id=3,
                recipe_ref="A1-1",
                displayed_rank=2,
                interaction_type="play",
            )
            self.assertTrue(created)
            self.assertEqual(first.click_order, 1)
            second, created_again = record_video_click(
                db,
                generation_id=10,
                user_id=7,
                asset_id=3,
                recipe_ref="A1-1",
                displayed_rank=2,
                interaction_type="open_source",
            )
            self.assertFalse(created_again)
            self.assertEqual(second.id, first.id)
            self.assertEqual(second.click_order, 1)
            other, created_other = record_video_click(
                db,
                generation_id=10,
                user_id=7,
                asset_id=4,
                recipe_ref="A1-1",
                displayed_rank=1,
                interaction_type="play",
            )
            self.assertTrue(created_other)
            self.assertEqual(other.click_order, 2)
            with self.assertRaises(ValueError):
                record_video_click(
                    db,
                    generation_id=10,
                    user_id=7,
                    asset_id=5,
                    recipe_ref="A1-1",
                    displayed_rank=3,
                    interaction_type="hover",
                )
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
