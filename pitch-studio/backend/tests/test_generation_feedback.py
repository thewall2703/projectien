from __future__ import annotations

import json
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user, hash_password
from backend.database import Base, get_db
from backend.models import Generation, User
from backend.routers import generation_feedback_routes
from backend.script_testing import build_review_document


class GenerationFeedbackApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.owner = User(
            email="owner@example.com",
            password_hash=hash_password("secret"),
            is_admin=False,
        )
        self.other = User(
            email="other@example.com",
            password_hash=hash_password("secret"),
            is_admin=False,
        )
        self.admin = User(
            email="admin@example.com",
            password_hash=hash_password("secret"),
            is_admin=True,
        )
        self.db.add_all([self.owner, self.other, self.admin])
        self.db.commit()
        self.db.refresh(self.owner)
        self.db.refresh(self.other)
        self.db.refresh(self.admin)

        self.app = FastAPI()
        self.app.include_router(generation_feedback_routes.router)

        def override_db():
            try:
                yield self.db
            finally:
                pass

        self.current = self.owner

        def override_user():
            return self.current

        self.app.dependency_overrides[get_db] = override_db
        self.app.dependency_overrides[get_current_user] = override_user
        self.client = TestClient(self.app)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _done_generation(self, owner: User | None = None) -> Generation:
        script = {
            "sections": [
                {
                    "module_id": "M01",
                    "topic_id": 1,
                    "topic_title": "Open",
                    "heading": "Open",
                    "text": "We train operators. Come sit in a class.\n\nThen decide with your eyes open.",
                }
            ],
            "cta": "Come Saturday.",
        }
        row = Generation(
            user_id=(owner or self.owner).id,
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X2",
            context_note="",
            recipe_ref="A2-1",
            module_sequence="M01>M14",
            status="done",
            script_json=json.dumps(script),
            validation_report="",
            error="",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _targets(self, generation: Generation) -> tuple[str, str]:
        document = build_review_document(json.loads(generation.script_json))
        paragraph = document["sections"][0]["paragraphs"][0]
        return paragraph["sentences"][0]["id"], paragraph["id"]

    def test_owner_can_read_review_and_add_sentence_and_paragraph_feedback(self):
        generation = self._done_generation()
        sentence_id, paragraph_id = self._targets(generation)
        self.current = self.owner

        review = self.client.get(f"/api/generations/{generation.id}/review")
        self.assertEqual(review.status_code, 200)
        body = review.json()
        self.assertEqual(body["review_document"]["sections"][0]["paragraphs"][0]["id"], "section-0-paragraph-0")
        self.assertEqual(body["feedback"], [])
        self.assertIsNone(body["average_rating"])
        self.assertEqual(body["rating_count"], 0)

        sentence = self.client.post(
            f"/api/generations/{generation.id}/feedback",
            json={"target_kind": "sentence", "target_id": sentence_id, "comment": "Tighten this opener."},
        )
        self.assertEqual(sentence.status_code, 200)
        self.assertEqual(sentence.json()["reviewer_user_id"], self.owner.id)
        self.assertEqual(sentence.json()["generation_id"], generation.id)
        self.assertEqual(sentence.json()["target_kind"], "sentence")

        paragraph = self.client.post(
            f"/api/generations/{generation.id}/feedback",
            json={"target_kind": "paragraph", "target_id": paragraph_id, "comment": "Whole paragraph feels soft."},
        )
        self.assertEqual(paragraph.status_code, 200)
        self.assertEqual(paragraph.json()["target_kind"], "paragraph")

        refreshed = self.client.get(f"/api/generations/{generation.id}/review").json()
        self.assertEqual(len(refreshed["feedback"]), 2)
        self.assertEqual(refreshed["feedback"][0]["reviewer_email"], self.owner.email)

    def test_admin_can_review_someone_elses_generation(self):
        generation = self._done_generation(self.owner)
        sentence_id, _ = self._targets(generation)
        self.current = self.admin
        review = self.client.get(f"/api/generations/{generation.id}/review")
        self.assertEqual(review.status_code, 200)
        created = self.client.post(
            f"/api/generations/{generation.id}/feedback",
            json={"target_kind": "sentence", "target_id": sentence_id, "comment": "Admin note."},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["reviewer_user_id"], self.admin.id)

    def test_other_non_admin_is_refused(self):
        generation = self._done_generation(self.owner)
        self.current = self.other
        review = self.client.get(f"/api/generations/{generation.id}/review")
        self.assertEqual(review.status_code, 403)
        missing = self.client.get("/api/generations/99999/review")
        self.assertEqual(missing.status_code, 404)

    def test_not_done_generation_returns_409(self):
        row = Generation(
            user_id=self.owner.id,
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X2",
            status="generating_script",
            script_json="",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        self.current = self.owner
        response = self.client.get(f"/api/generations/{row.id}/review")
        self.assertEqual(response.status_code, 409)
        feedback = self.client.post(
            f"/api/generations/{row.id}/feedback",
            json={"target_kind": "sentence", "target_id": "section-0-paragraph-0-sentence-0", "comment": "early"},
        )
        self.assertEqual(feedback.status_code, 409)

    def test_forged_target_returns_404(self):
        generation = self._done_generation()
        sentence_id, _ = self._targets(generation)
        self.current = self.owner
        bad = self.client.post(
            f"/api/generations/{generation.id}/feedback",
            json={"target_kind": "sentence", "target_id": "section-99-paragraph-0-sentence-0", "comment": "nope"},
        )
        self.assertEqual(bad.status_code, 404)
        kind_mismatch = self.client.post(
            f"/api/generations/{generation.id}/feedback",
            json={"target_kind": "paragraph", "target_id": sentence_id, "comment": "wrong kind"},
        )
        self.assertEqual(kind_mismatch.status_code, 404)

    def test_only_author_can_edit_feedback(self):
        generation = self._done_generation()
        sentence_id, _ = self._targets(generation)
        self.current = self.owner
        created = self.client.post(
            f"/api/generations/{generation.id}/feedback",
            json={"target_kind": "sentence", "target_id": sentence_id, "comment": "Original wording"},
        )
        self.assertEqual(created.status_code, 200)
        feedback_id = created.json()["id"]

        updated = self.client.put(
            f"/api/generations/{generation.id}/feedback/{feedback_id}",
            json={"comment": "  Improved wording  "},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["comment"], "Improved wording")

        self.current = self.admin
        forbidden = self.client.put(
            f"/api/generations/{generation.id}/feedback/{feedback_id}",
            json={"comment": "Admin overwrite"},
        )
        self.assertEqual(forbidden.status_code, 403)

        self.current = self.owner
        empty = self.client.put(
            f"/api/generations/{generation.id}/feedback/{feedback_id}",
            json={"comment": "   "},
        )
        self.assertEqual(empty.status_code, 422)

    def test_feedback_from_another_generation_returns_404(self):
        first = self._done_generation()
        second = self._done_generation()
        sentence_id, _ = self._targets(first)
        self.current = self.owner
        created = self.client.post(
            f"/api/generations/{first.id}/feedback",
            json={"target_kind": "sentence", "target_id": sentence_id, "comment": "On first"},
        )
        self.assertEqual(created.status_code, 200)
        feedback_id = created.json()["id"]
        missing = self.client.put(
            f"/api/generations/{second.id}/feedback/{feedback_id}",
            json={"comment": "Wrong generation"},
        )
        self.assertEqual(missing.status_code, 404)

    def test_ratings_bounds_rounding_upsert_and_average(self):
        generation = self._done_generation()
        self.current = self.owner
        ok = self.client.put(f"/api/generations/{generation.id}/rating", json={"rating": 7.56})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["rating"], 7.6)
        self.assertEqual(ok.json()["average_rating"], 7.6)
        self.assertEqual(ok.json()["rating_count"], 1)
        self.assertEqual(ok.json()["generation_id"], generation.id)

        update = self.client.put(f"/api/generations/{generation.id}/rating", json={"rating": 9.8})
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json()["rating"], 9.8)
        self.assertEqual(update.json()["rating_count"], 1)

        self.current = self.admin
        other = self.client.put(f"/api/generations/{generation.id}/rating", json={"rating": 8})
        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.json()["rating_count"], 2)
        self.assertEqual(other.json()["average_rating"], 8.9)

        review = self.client.get(f"/api/generations/{generation.id}/review").json()
        by_user = {row["reviewer_user_id"]: row["rating"] for row in review["ratings"]}
        self.assertEqual(by_user[self.owner.id], 9.8)
        self.assertEqual(by_user[self.admin.id], 8.0)
        self.assertEqual(review["average_rating"], 8.9)
        self.assertEqual(review["rating_count"], 2)

        bad = self.client.put(f"/api/generations/{generation.id}/rating", json={"rating": 10.1})
        self.assertEqual(bad.status_code, 422)
        negative = self.client.put(f"/api/generations/{generation.id}/rating", json={"rating": -0.1})
        self.assertEqual(negative.status_code, 422)

        zero = self.client.put(f"/api/generations/{generation.id}/rating", json={"rating": 0})
        self.assertEqual(zero.status_code, 200)
        ten = self.client.put(f"/api/generations/{generation.id}/rating", json={"rating": 10})
        self.assertEqual(ten.status_code, 200)


if __name__ == "__main__":
    unittest.main()
