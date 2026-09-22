from __future__ import annotations

import hashlib
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.database import Base, get_db
from backend.models import GeneratedSlide, GeneratedSlideAttempt, User, utc_now
from backend.routers import admin_routes
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
        self.admin = User(
            email="admin@example.com",
            password_hash="unused",
            is_admin=True,
        )
        self.member = User(
            email="member@example.com",
            password_hash="unused",
            is_admin=False,
        )
        self.db.add_all([self.admin, self.member])
        self.db.commit()
        self.db.refresh(self.admin)
        self.db.refresh(self.member)

        self.app = FastAPI()
        self.app.include_router(admin_routes.router)

        def override_db():
            yield self.db

        self.current_user = self.admin

        def override_user():
            return self.current_user

        self.app.dependency_overrides[get_db] = override_db
        self.app.dependency_overrides[get_current_user] = override_user
        self.client = TestClient(self.app)

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

    def add_attempt(
        self,
        *,
        generation_id: int = 10,
        outcome: str = "rendered",
        gate: str = "passed",
        review_status: str = "pending",
        file_key: str = "generated-slide-attempts/attempt.jpg",
        created_offset: int = 0,
    ) -> GeneratedSlideAttempt:
        row = GeneratedSlideAttempt(
            generation_id=generation_id,
            generated_slide_id=None,
            placeholder_key=f"hero-{generation_id}-{created_offset}",
            attempt_number=created_offset + 1,
            claim="Students learn by building real companies.",
            template_id="section-divider-light",
            tone="light",
            outcome=outcome,
            gate=gate,
            violations_json='["Title exceeds budget"]' if gate != "passed" else "[]",
            slot_values_json='{"title":"Learn by doing","subtitle":"From day one"}',
            render_hash=f"render-{generation_id}-{created_offset}",
            file_key=file_key,
            review_status=review_status,
            created_at=utc_now() + timedelta(seconds=created_offset),
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
    @patch("backend.routers.admin_routes._prepare_photos")
    def test_programme_list_edit_preserves_and_updates_the_bounded_list(
        self, prepare_photos, render_slide, save_image, delete_file
    ):
        row = self.add_programme_slide()
        prepare_photos.return_value = (
            {"photo_1": b"img-1", "photo_2": b"img-2"},
            [
                {"slot": "photo_1", "asset_id": 101, "key": "a", "crop": {"fit": "cover"}},
                {"slot": "photo_2", "asset_id": 102, "key": "b", "crop": {"fit": "cover"}},
            ],
            [101, 102],
        )
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
        _, kwargs = render_slide.call_args
        self.assertEqual(set(kwargs["photos"]), {"photo_1", "photo_2"})

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

    def test_attempt_api_requires_admin_and_lists_filtered_newest_first(self):
        older = self.add_attempt(outcome="rejected", gate="schema", created_offset=0)
        newer = self.add_attempt(outcome="rendered", gate="passed", created_offset=2)
        newest = self.add_attempt(outcome="rendered", gate="passed", created_offset=3)

        self.current_user = self.member
        denied = self.client.get("/api/admin/generated-slide-attempts")
        self.assertEqual(denied.status_code, 403)

        self.current_user = self.admin
        response = self.client.get(
            "/api/admin/generated-slide-attempts",
            params={"outcome": "rendered", "review_status": "pending"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([entry["id"] for entry in body], [newest.id, newer.id])
        self.assertEqual(body[0]["slot_values"]["title"], "Learn by doing")
        self.assertEqual(body[0]["violations"], [])
        self.assertNotIn(older.id, [entry["id"] for entry in body])

    @patch("backend.routers.admin_routes.read_file", return_value=b"jpeg-bytes")
    def test_attempt_image_reads_private_file(self, read_file):
        attempt = self.add_attempt()
        response = self.client.get(
            f"/api/admin/generated-slide-attempts/{attempt.id}/image"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"jpeg-bytes")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        read_file.assert_called_once_with(attempt.file_key)

    def test_attempt_review_sets_only_review_metadata(self):
        attempt = self.add_attempt()
        original_claim = attempt.claim
        response = self.client.put(
            f"/api/admin/generated-slide-attempts/{attempt.id}/review",
            json={
                "review_status": "approved",
                "review_note": "Keep the concrete, outcome-led headline.",
                "use_as_guidance": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["reviewer_user_id"], self.admin.id)
        self.assertIsNotNone(body["reviewed_at"])
        stored = self.db.get(GeneratedSlideAttempt, attempt.id)
        self.assertEqual(stored.claim, original_claim)
        self.assertEqual(stored.review_status, "approved")
        self.assertTrue(stored.use_as_guidance)

        immutable = self.client.put(
            f"/api/admin/generated-slide-attempts/{attempt.id}/review",
            json={
                "review_status": "reviewed",
                "review_note": "",
                "use_as_guidance": False,
                "claim": "Mutated",
            },
        )
        self.assertEqual(immutable.status_code, 422)
        self.assertEqual(self.db.get(GeneratedSlideAttempt, attempt.id).claim, original_claim)

    def test_guidance_review_requires_nonempty_note(self):
        attempt = self.add_attempt()
        response = self.client.put(
            f"/api/admin/generated-slide-attempts/{attempt.id}/review",
            json={
                "review_status": "approved",
                "review_note": "   ",
                "use_as_guidance": True,
            },
        )
        self.assertEqual(response.status_code, 400)
        stored = self.db.get(GeneratedSlideAttempt, attempt.id)
        self.assertEqual(stored.review_status, "pending")
        self.assertIsNone(stored.reviewer_user_id)
        self.assertIsNone(stored.reviewed_at)


if __name__ == "__main__":
    unittest.main()
