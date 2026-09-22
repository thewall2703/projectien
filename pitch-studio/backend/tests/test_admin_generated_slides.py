from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import GeneratedSlide
from backend.routers.admin_routes import (
    _invalidate_fact_slides,
    delete_generated_slide,
    update_generated_slide,
)
from backend.schemas import GeneratedSlideUpdate


class GeneratedSlideAdminTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

    def tearDown(self):
        self.db.close()

    def add_slide(self, *, edited: bool = False, fact_ids: str = "7") -> GeneratedSlide:
        suffix = "human" if edited else "machine"
        row = GeneratedSlide(
            slide_key=f"generated-{suffix}",
            claim_hash=hashlib.sha256(f"claim-{suffix}".encode()).hexdigest(),
            render_hash=hashlib.sha256(f"render-{suffix}".encode()).hexdigest(),
            template_id="section-divider-light",
            template_version="1",
            tone="light",
            slot_values_json='{"title":"Original","subtitle":""}',
            file_key=f"generated-slides/{suffix}.jpg",
            status="ready",
            edited_by_human=edited,
            source_fact_ids=fact_ids,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def test_fact_change_invalidates_machine_output_and_flags_human_output(self):
        machine = self.add_slide()
        human = self.add_slide(edited=True)

        _invalidate_fact_slides(self.db, 7)
        self.db.commit()

        self.assertEqual(self.db.get(GeneratedSlide, machine.id).status, "invalidated")
        self.assertEqual(self.db.get(GeneratedSlide, human.id).status, "review")

    def test_fact_token_matching_does_not_invalidate_partial_id(self):
        row = self.add_slide(fact_ids="17,70")
        _invalidate_fact_slides(self.db, 7)
        self.db.commit()
        self.assertEqual(self.db.get(GeneratedSlide, row.id).status, "ready")

    @patch("backend.routers.admin_routes.delete_file")
    @patch("backend.routers.admin_routes.save_generated_slide_image")
    @patch("backend.routers.admin_routes.render_slide")
    def test_edit_rerenders_and_marks_human_without_changing_stable_key(
        self,
        render_slide,
        save_image,
        delete_file,
    ):
        row = self.add_slide()
        original_key = row.slide_key
        render_slide.return_value = SimpleNamespace(jpeg=b"jpeg")
        save_image.return_value = "generated-slides/new.jpg"

        result = update_generated_slide(
            row.id,
            GeneratedSlideUpdate(
                slot_values={"title": "New direction", "subtitle": "What changed"},
            ),
            self.db,
        )

        self.assertEqual(result.slide_key, original_key)
        self.assertTrue(result.edited_by_human)
        self.assertEqual(result.slot_values["title"], "New direction")
        save_image.assert_called_once()
        delete_file.assert_called_once_with("generated-slides/machine.jpg")

    def add_programme_slide(self) -> GeneratedSlide:
        row = GeneratedSlide(
            slide_key="generated-prog",
            claim_hash=hashlib.sha256(b"prog").hexdigest(),
            render_hash=hashlib.sha256(b"prog-render").hexdigest(),
            template_id="programme-list-light",
            template_version="1",
            tone="light",
            slot_values_json=json.dumps(
                {
                    "title": "Undergraduate",
                    "programme_type": "Programmes",
                    "items": ["UG in **Technology**", "UG in **Marketing**"],
                }
            ),
            file_key="generated-slides/prog.jpg",
            status="ready",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def add_campus_slide(self) -> GeneratedSlide:
        row = GeneratedSlide(
            slide_key="generated-campus",
            claim_hash=hashlib.sha256(b"campus").hexdigest(),
            render_hash=hashlib.sha256(b"campus-render").hexdigest(),
            template_id="campus-photo-dark",
            template_version="1",
            tone="dark",
            slot_values_json=json.dumps(
                {"headline": "Classrooms", "caption": "High-energy learning spaces with AV & boards"}
            ),
            file_key="generated-slides/campus.jpg",
            status="ready",
            source_asset_ids="101",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    @patch("backend.routers.admin_routes.delete_file")
    @patch("backend.routers.admin_routes.save_generated_slide_image")
    @patch("backend.routers.admin_routes.render_slide")
    def test_programme_list_edit_preserves_and_updates_the_bounded_list(
        self, render_slide, save_image, delete_file
    ):
        row = self.add_programme_slide()
        render_slide.return_value = SimpleNamespace(jpeg=b"jpeg")
        save_image.return_value = "generated-slides/prog2.jpg"

        result = update_generated_slide(
            row.id,
            GeneratedSlideUpdate(
                slot_values={
                    "title": "Postgraduate",
                    "programme_type": "Programmes",
                    "items": ["PG in **Applied AI**", "PG in **Finance**", "PG in **Design**"],
                }
            ),
            self.db,
        )

        self.assertEqual(result.slot_values["title"], "Postgraduate")
        self.assertEqual(
            result.slot_values["items"],
            ["PG in **Applied AI**", "PG in **Finance**", "PG in **Design**"],
        )
        # The list survives the round-trip in storage as an array.
        stored = json.loads(self.db.get(GeneratedSlide, row.id).slot_values_json)
        self.assertIsInstance(stored["items"], list)
        self.assertEqual(len(stored["items"]), 3)

    @patch("backend.routers.admin_routes.delete_file")
    @patch("backend.routers.admin_routes.save_generated_slide_image")
    @patch("backend.routers.admin_routes.render_slide")
    @patch("backend.routers.admin_routes._prepare_photos")
    def test_campus_edit_reembeds_the_original_photo(
        self, prepare_photos, render_slide, save_image, delete_file
    ):
        row = self.add_campus_slide()
        prepare_photos.return_value = (
            {"hero_photo": b"img"},
            [{"slot": "hero_photo", "asset_id": 101, "key": "k", "crop": {"fit": "cover"}}],
            [101],
        )
        render_slide.return_value = SimpleNamespace(jpeg=b"jpeg")
        save_image.return_value = "generated-slides/campus2.jpg"

        result = update_generated_slide(
            row.id,
            GeneratedSlideUpdate(
                slot_values={
                    "headline": "Library",
                    "caption": "Quiet focused study spaces open around the clock",
                }
            ),
            self.db,
        )

        self.assertEqual(result.slot_values["headline"], "Library")
        # The photo bytes were passed to the renderer for re-embed.
        _, kwargs = render_slide.call_args
        self.assertIn("hero_photo", kwargs["photos"])
        save_image.assert_called_once()

    @patch("backend.routers.admin_routes.render_slide")
    @patch("backend.routers.admin_routes._prepare_photos")
    def test_campus_edit_without_recoverable_photo_is_rejected(
        self, prepare_photos, render_slide
    ):
        row = self.add_campus_slide()
        prepare_photos.return_value = ({}, [], [])  # asset gone

        with self.assertRaises(HTTPException) as ctx:
            update_generated_slide(
                row.id,
                GeneratedSlideUpdate(
                    slot_values={
                        "headline": "Library",
                        "caption": "Quiet focused study spaces open around the clock",
                    }
                ),
                self.db,
            )
        self.assertEqual(ctx.exception.status_code, 400)
        render_slide.assert_not_called()

    @patch("backend.routers.admin_routes.delete_file")
    def test_delete_removes_row_and_backing_image(self, delete_file):
        row = self.add_slide()
        self.assertEqual(delete_generated_slide(row.id, self.db), {"ok": True})
        self.assertIsNone(self.db.get(GeneratedSlide, row.id))
        delete_file.assert_called_once_with("generated-slides/machine.jpg")


if __name__ == "__main__":
    unittest.main()
