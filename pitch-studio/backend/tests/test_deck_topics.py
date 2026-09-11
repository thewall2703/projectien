from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.deck_topic_index import (
    JOB_DECK_PREPARE,
    DeckTopicError,
    build_context_index,
    derive_status,
    enqueue_deck_prepare,
    fallback_topic_groups,
    is_stale,
    representative_pages,
    require_editable,
    serialize,
    validate_topic_groups,
)
from backend.pipeline.brand_deck import SOURCE_PAGE_COUNT
from backend import worker


def catalog_for(page_count: int = SOURCE_PAGE_COUNT) -> list[dict]:
    from backend.pipeline.brand_deck import PAGE_LABELS, PAGE_MODULES

    return [
        {
            "page": page,
            "label": PAGE_LABELS.get(page, f"Page {page}"),
            "module_id": PAGE_MODULES.get(page, ""),
            "text": "",
        }
        for page in range(1, page_count + 1)
    ]


class GroupingTests(unittest.TestCase):
    def test_accepts_complete_contiguous_topics(self):
        groups = validate_topic_groups(
            [
                {"title": "Open", "start_page": 1, "end_page": 10},
                {"title": "Middle", "start_page": 11, "end_page": 80},
                {"title": "Close", "start_page": 81, "end_page": 92},
            ]
        )
        self.assertEqual(groups[0]["end_page"], 10)
        self.assertEqual(groups[-1]["end_page"], 92)
        pages = []
        for group in groups:
            pages.extend(range(group["start_page"], group["end_page"] + 1))
        self.assertEqual(pages, list(range(1, 93)))

    def test_repairs_overlaps_and_gaps(self):
        groups = validate_topic_groups(
            [
                {"title": "A", "start_page": 2, "end_page": 20},
                {"title": "B", "start_page": 15, "end_page": 40},
                {"title": "C", "start_page": 50, "end_page": 80},
            ]
        )
        pages = []
        for group in groups:
            pages.extend(range(group["start_page"], group["end_page"] + 1))
        self.assertEqual(pages, list(range(1, 93)))
        self.assertEqual(len(pages), len(set(pages)))

    def test_rejects_empty_payload(self):
        with self.assertRaises(DeckTopicError):
            validate_topic_groups([])

    def test_fallback_covers_every_page_once(self):
        groups = fallback_topic_groups(catalog_for())
        pages = []
        for group in groups:
            pages.extend(range(group["start_page"], group["end_page"] + 1))
        self.assertEqual(pages, list(range(1, SOURCE_PAGE_COUNT + 1)))
        self.assertEqual(len(pages), len(set(pages)))
        self.assertGreaterEqual(len(groups), 8)

    def test_representative_pages_picks_ends_and_middle(self):
        self.assertEqual(representative_pages(1, 1), [1])
        self.assertEqual(representative_pages(10, 20), [10, 15, 20])


class LifecycleTests(unittest.TestCase):
    def test_ready_when_summary_exists(self):
        row = SimpleNamespace(status="draft", summary="Campus origin story.", vision_frozen=False)
        self.assertEqual(derive_status(row), "ready")

    def test_processing_when_job_active(self):
        row = SimpleNamespace(status="draft", summary="", vision_frozen=False)
        self.assertEqual(derive_status(row, SimpleNamespace(status="running")), "processing")

    def test_stale_when_vision_changes(self):
        row = SimpleNamespace(
            vision="show to founders",
            vision_hash="new",
            indexed_vision_hash="old",
            recommendations_json='{"items":[]}',
        )
        self.assertTrue(is_stale(row))

    def test_frozen_cannot_edit(self):
        with self.assertRaises(DeckTopicError):
            require_editable(SimpleNamespace(vision_frozen=True))

    def test_context_includes_pages_and_vision(self):
        row = SimpleNamespace(
            title="Origin",
            pages_json="[1,2,3]",
            module_ids="M01",
            summary="Why the school exists.",
            vision="Show this to parents.",
        )
        context = build_context_index(row)
        self.assertIn("Origin", context)
        self.assertIn("Pages: 1-3", context)
        self.assertIn("Why the school exists.", context)
        self.assertIn("Show this to parents.", context)

    def test_serialize_exposes_page_images(self):
        row = SimpleNamespace(
            id=4,
            sort_order=0,
            title="Origin",
            pages_json="[1,2]",
            summary="Why the school exists.",
            module_ids="M01",
            vision="",
            vision_hash="",
            indexed_vision_hash="",
            recommended_at=None,
            vision_frozen=False,
            status="ready",
            source_hash="abc",
            created_at=None,
            updated_at=None,
            recommendations_json="",
            feedback_json="",
        )
        out = serialize(row)
        self.assertEqual(out.pages, [1, 2])
        self.assertEqual(out.page_items[0].image_url, "/api/brand-deck/pages/1.jpg")
        self.assertEqual(out.status, "ready")


class EnqueueTests(unittest.TestCase):
    def test_reuses_active_job(self):
        existing = SimpleNamespace(id=3, status="queued")
        db = mock.Mock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = existing
        with mock.patch("backend.deck_topic_index._ensure_schema"):
            job = enqueue_deck_prepare(db, 9)
        self.assertIs(job, existing)
        db.add.assert_not_called()


class WorkerDeckJobTests(unittest.TestCase):
    def test_process_one_runs_deck_prepare(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from backend.database import Base
        from backend.models import Job

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = Session()
        job = Job(job_type=JOB_DECK_PREPARE, media_id=0, asset_id=12, status="queued")
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        def fake_prepare(_db, on_stage=None):
            if on_stage:
                on_stage("Grouping brand deck pages…")
            return []

        with mock.patch.object(worker, "SessionLocal", Session):
            with mock.patch.object(worker, "prepare_deck_topics", side_effect=fake_prepare) as prepare:
                self.assertTrue(worker.process_one())
        prepare.assert_called_once()
        db = Session()
        done = db.get(Job, job_id)
        self.assertEqual(done.status, "done")
        db.close()
        engine.dispose()


class PreparePersistenceTests(unittest.TestCase):
    def test_writes_validated_groups(self):
        from backend.deck_topic_index import prepare_deck_topics

        asset = SimpleNamespace(id=7, extract_json="", extract_status="", title="Brand Deck")
        db = mock.Mock()
        db.query.return_value.order_by.return_value.all.return_value = []
        added = []

        def add(row):
            row.id = 1 + len(added)
            added.append(row)

        db.add.side_effect = add
        with mock.patch("backend.deck_topic_index._ensure_schema"):
            with mock.patch("backend.deck_topic_index.find_brand_deck_asset", return_value=asset):
                with mock.patch("backend.deck_topic_index._ensure_extract"):
                    with mock.patch(
                        "backend.deck_topic_index.group_brand_deck_pages",
                        return_value=validate_topic_groups(
                            [
                                {"title": "Open", "start_page": 1, "end_page": 40, "module_ids": ["M01"]},
                                {"title": "Close", "start_page": 41, "end_page": 92, "module_ids": ["M14"]},
                            ]
                        ),
                    ):
                        with mock.patch(
                            "backend.deck_topic_index.describe_topic_pages",
                            return_value="A concise topic summary.",
                        ):
                            with mock.patch("backend.deck_topic_index.brand_deck_file_key", return_value=None):
                                with mock.patch("backend.deck_topic_index.apply_recommendations") as recommend:
                                    rows = prepare_deck_topics(db)
        self.assertEqual(recommend.call_count, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[0].pages_json)[0], 1)
        self.assertEqual(json.loads(rows[1].pages_json)[-1], 92)
        self.assertEqual(rows[0].summary, "A concise topic summary.")
        db.commit.assert_called()

    def test_skip_path_recommends_missing_personas(self):
        from backend.deck_topic_index import prepare_deck_topics

        asset = SimpleNamespace(id=7, extract_json="", extract_status="ready", title="Brand Deck")
        existing = [
            SimpleNamespace(
                id=1,
                source_hash="same",
                summary="Campus origin story.",
                recommendations_json="",
                vision_frozen=False,
            )
        ]
        db = mock.Mock()
        db.query.return_value.order_by.return_value.all.return_value = existing
        with mock.patch("backend.deck_topic_index._ensure_schema"):
            with mock.patch("backend.deck_topic_index.find_brand_deck_asset", return_value=asset):
                with mock.patch("backend.deck_topic_index._ensure_extract"):
                    with mock.patch(
                        "backend.deck_topic_index.catalog_source_hash",
                        return_value="same",
                    ):
                        with mock.patch("backend.deck_topic_index.apply_recommendations") as recommend:
                            rows = prepare_deck_topics(db)
        recommend.assert_called_once_with(db, existing[0])
        self.assertEqual(rows, existing)
        db.commit.assert_called()

    def test_recommends_from_analysis_without_vision(self):
        from backend.deck_topic_index import build_topic_recommendation_messages

        messages = build_topic_recommendation_messages(
            "Origin",
            [1, 2, 3],
            "Why the school exists.",
            "",
            [],
        )
        self.assertIn("analysis only", messages[1]["content"])
        self.assertIn("Why the school exists.", messages[1]["content"])
        self.assertIn("analysis alone", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
