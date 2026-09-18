from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user, hash_password
from backend.database import Base, get_db
from backend.models import ScriptTestRun, User
from backend.pipeline.runner import run
from backend.routers import script_testing_routes
from backend.script_testing import build_review_document, find_review_target, run_script_test, script_paragraphs


class ReviewDocumentTests(unittest.TestCase):
    def test_script_paragraphs_blank_line_split(self):
        text = "First paragraph here.\n\nSecond paragraph here."
        self.assertEqual(script_paragraphs(text), ["First paragraph here.", "Second paragraph here."])

    def test_stable_sentence_and_paragraph_ids(self):
        script = {
            "sections": [
                {
                    "module_id": "M01",
                    "topic_id": 3,
                    "topic_title": "Open",
                    "heading": "Open",
                    "text": "We train operators. Come sit in a class.\n\nThen decide with your eyes open.",
                }
            ],
            "cta": "Come Saturday.",
        }
        document = build_review_document(script)
        self.assertEqual(document["cta"], "Come Saturday.")
        section = document["sections"][0]
        self.assertEqual(section["index"], 0)
        self.assertEqual(section["module_id"], "M01")
        self.assertEqual(section["topic_id"], 3)
        self.assertEqual(len(section["paragraphs"]), 2)
        first = section["paragraphs"][0]
        self.assertEqual(first["id"], "section-0-paragraph-0")
        self.assertEqual(first["sentences"][0]["id"], "section-0-paragraph-0-sentence-0")
        self.assertEqual(first["sentences"][0]["text"], "We train operators.")
        self.assertEqual(first["sentences"][1]["id"], "section-0-paragraph-0-sentence-1")
        self.assertEqual(section["paragraphs"][1]["id"], "section-0-paragraph-1")
        self.assertEqual(
            section["paragraphs"][1]["sentences"][0]["text"],
            "Then decide with your eyes open.",
        )

    def test_find_review_target_rejects_unknown(self):
        document = build_review_document(
            {
                "sections": [{"heading": "A", "text": "Hello world. Next line."}],
                "cta": "",
            }
        )
        sentence_id = document["sections"][0]["paragraphs"][0]["sentences"][0]["id"]
        found = find_review_target(document, "sentence", sentence_id)
        self.assertIsNotNone(found)
        self.assertEqual(found["reference_text"], "Hello world.")
        self.assertIsNone(find_review_target(document, "sentence", "section-9-paragraph-0-sentence-0"))
        self.assertIsNone(find_review_target(document, "paragraph", sentence_id))


class ScriptPhaseShareTests(unittest.TestCase):
    def test_run_uses_generate_script_phase_then_deck(self):
        generation = SimpleNamespace(
            id=1,
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X2",
            context_note="",
            recipe_ref="A2-1",
            module_sequence="",
            status="queued",
            script_json="",
            validation_report="",
            error="",
            deck_spec_json="",
            pptx_path="",
            asset_ids="",
            objection_ids="",
            founder_quote_ids="",
            report_asset_ids="",
            report_passages_json="",
        )
        phase = SimpleNamespace(
            resolved=SimpleNamespace(module_sequence=["M01"]),
            plan=["slide"],
            script={"sections": [], "cta": ""},
        )
        db = mock.MagicMock()
        db.get.return_value = generation
        with mock.patch("backend.pipeline.runner.SessionLocal", return_value=db), mock.patch(
            "backend.pipeline.runner.generate_script_phase", return_value=phase
        ) as script_phase, mock.patch(
            "backend.pipeline.runner.deck_spec_from_plan",
            return_value=SimpleNamespace(model_dump_json=lambda: "{}"),
        ) as deck_spec, mock.patch(
            "backend.pipeline.runner.render_pptx", return_value="decks/1.pptx"
        ) as render, mock.patch(
            "backend.pipeline.runner._select_assets", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner._select_objections", return_value=[]
        ), mock.patch(
            "backend.pipeline.runner.brand_deck_file_key", return_value=""
        ), mock.patch(
            "backend.pipeline.runner.notes_by_page", return_value={}
        ):
            run(1)
        script_phase.assert_called_once()
        deck_spec.assert_called_once()
        render.assert_called_once()
        self.assertEqual(generation.status, "done")
        self.assertEqual(generation.pptx_path, "decks/1.pptx")

    def test_script_test_never_calls_deck_media_or_objections(self):
        run_row = SimpleNamespace(
            id=9,
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X2",
            context_note="",
            recipe_ref="A2-1",
            module_sequence="",
            status="queued",
            script_json="",
            review_document_json="",
            validation_report="",
            error="",
            founder_quote_ids="",
            report_asset_ids="",
            report_passages_json="",
            finished_at=None,
        )
        phase = SimpleNamespace(
            script={
                "sections": [{"heading": "Open", "text": "We train operators. Come sit in."}],
                "cta": "Come.",
            },
            validation_report="",
            founder_quote_ids="1",
            report_asset_ids="",
            report_passages=[],
        )
        db = mock.MagicMock()
        db.get.return_value = run_row
        with mock.patch("backend.script_testing.SessionLocal", return_value=db), mock.patch(
            "backend.script_testing.generate_script_phase", return_value=phase
        ) as script_phase, mock.patch(
            "backend.pipeline.runner.deck_spec_from_plan"
        ) as deck_spec, mock.patch(
            "backend.pipeline.runner.render_pptx"
        ) as render, mock.patch(
            "backend.pipeline.runner._select_assets"
        ) as assets, mock.patch(
            "backend.pipeline.runner._select_objections"
        ) as objections:
            run_script_test(9)
        script_phase.assert_called_once()
        deck_spec.assert_not_called()
        render.assert_not_called()
        assets.assert_not_called()
        objections.assert_not_called()
        self.assertEqual(run_row.status, "done")
        document = json.loads(run_row.review_document_json)
        self.assertEqual(document["sections"][0]["paragraphs"][0]["id"], "section-0-paragraph-0")
        self.assertIsNotNone(run_row.finished_at)

    def test_failed_script_generation_persists_error(self):
        run_row = SimpleNamespace(
            id=3,
            status="queued",
            error="",
            finished_at=None,
            review_document_json="",
        )
        db = mock.MagicMock()
        db.get.return_value = run_row

        def fail_phase(_db, target, set_status):
            target.error = "Boom"
            set_status("failed")
            return None

        with mock.patch("backend.script_testing.SessionLocal", return_value=db), mock.patch(
            "backend.script_testing.generate_script_phase", side_effect=fail_phase
        ):
            run_script_test(3)
        self.assertEqual(run_row.status, "failed")
        self.assertEqual(run_row.error, "Boom")
        self.assertIsNotNone(run_row.finished_at)


class ScriptTestingApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.user = User(
            email="reviewer@example.com",
            password_hash=hash_password("secret"),
            is_admin=False,
        )
        self.admin = User(
            email="admin@example.com",
            password_hash=hash_password("secret"),
            is_admin=True,
        )
        self.db.add_all([self.user, self.admin])
        self.db.commit()
        self.db.refresh(self.user)
        self.db.refresh(self.admin)

        self.app = FastAPI()
        self.app.include_router(script_testing_routes.router)

        def override_db():
            try:
                yield self.db
            finally:
                pass

        self.current = self.user

        def override_user():
            return self.current

        self.app.dependency_overrides[get_db] = override_db
        self.app.dependency_overrides[get_current_user] = override_user
        self.client = TestClient(self.app)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _done_run(self, creator: User | None = None) -> ScriptTestRun:
        script = {
            "sections": [
                {
                    "module_id": "M01",
                    "topic_id": 1,
                    "topic_title": "Open",
                    "heading": "Open",
                    "text": "We train operators. Come sit in a class.",
                }
            ],
            "cta": "Come Saturday.",
        }
        document = build_review_document(script)
        row = ScriptTestRun(
            created_by_user_id=(creator or self.user).id,
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
            review_document_json=json.dumps(document),
            validation_report="",
            error="",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def test_endpoints_require_auth_for_non_admin(self):
        self.current = self.user
        with mock.patch(
            "backend.routers.script_testing_routes.run_script_test"
        ):
            response = self.client.post(
                "/api/script-tests",
                json={
                    "audience_cluster": "A",
                    "duration": "T1",
                    "channel": "CH1",
                    "intent": "I2",
                    "temperature": "X2",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created_by_user_id"], self.user.id)
        self.assertFalse(self.user.is_admin)

    def test_shared_list_and_detail(self):
        run = self._done_run(self.admin)
        self.current = self.user
        listed = self.client.get("/api/script-tests")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 1)
        self.assertEqual(listed.json()[0]["id"], run.id)
        self.assertEqual(listed.json()[0]["created_by_email"], self.admin.email)
        detail = self.client.get(f"/api/script-tests/{run.id}")
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(body["created_by_user_id"], self.admin.id)
        self.assertIsNotNone(body["review_document"])
        self.assertEqual(body["review_document"]["sections"][0]["paragraphs"][0]["id"], "section-0-paragraph-0")

    def test_feedback_rejects_forged_targets_and_supports_multi_reviewer(self):
        run = self._done_run(self.admin)
        sentence_id = json.loads(run.review_document_json)["sections"][0]["paragraphs"][0]["sentences"][0]["id"]
        paragraph_id = json.loads(run.review_document_json)["sections"][0]["paragraphs"][0]["id"]

        self.current = self.user
        bad = self.client.post(
            f"/api/script-tests/{run.id}/feedback",
            json={"target_kind": "sentence", "target_id": "section-99-paragraph-0-sentence-0", "comment": "nope"},
        )
        self.assertEqual(bad.status_code, 404)
        kind_mismatch = self.client.post(
            f"/api/script-tests/{run.id}/feedback",
            json={"target_kind": "paragraph", "target_id": sentence_id, "comment": "wrong kind"},
        )
        self.assertEqual(kind_mismatch.status_code, 404)

        first = self.client.post(
            f"/api/script-tests/{run.id}/feedback",
            json={"target_kind": "sentence", "target_id": sentence_id, "comment": "Tighten this opener."},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["reviewer_user_id"], self.user.id)

        second = self.client.post(
            f"/api/script-tests/{run.id}/feedback",
            json={"target_kind": "sentence", "target_id": sentence_id, "comment": "Another note from me."},
        )
        self.assertEqual(second.status_code, 200)

        self.current = self.admin
        other = self.client.post(
            f"/api/script-tests/{run.id}/feedback",
            json={"target_kind": "sentence", "target_id": sentence_id, "comment": "Admin agrees."},
        )
        self.assertEqual(other.status_code, 200)
        para = self.client.post(
            f"/api/script-tests/{run.id}/feedback",
            json={"target_kind": "paragraph", "target_id": paragraph_id, "comment": "Whole paragraph feels soft."},
        )
        self.assertEqual(para.status_code, 200)

        detail = self.client.get(f"/api/script-tests/{run.id}").json()
        sentence_comments = [item for item in detail["feedback"] if item["target_id"] == sentence_id]
        self.assertEqual(len(sentence_comments), 3)
        self.assertEqual(len([item for item in detail["feedback"] if item["target_id"] == paragraph_id]), 1)

    def test_feedback_blocked_before_done(self):
        row = ScriptTestRun(
            created_by_user_id=self.user.id,
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X2",
            status="generating_script",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        response = self.client.post(
            f"/api/script-tests/{row.id}/feedback",
            json={"target_kind": "sentence", "target_id": "section-0-paragraph-0-sentence-0", "comment": "early"},
        )
        self.assertEqual(response.status_code, 409)

    def test_ratings_decimal_range_and_averages(self):
        run = self._done_run()
        self.current = self.user
        ok = self.client.put(f"/api/script-tests/{run.id}/rating", json={"rating": 7.5})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["rating"], 7.5)
        self.assertEqual(ok.json()["average_rating"], 7.5)
        self.assertEqual(ok.json()["rating_count"], 1)

        update = self.client.put(f"/api/script-tests/{run.id}/rating", json={"rating": 9.8})
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json()["rating"], 9.8)
        self.assertEqual(update.json()["rating_count"], 1)

        self.current = self.admin
        other = self.client.put(f"/api/script-tests/{run.id}/rating", json={"rating": 8})
        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.json()["rating_count"], 2)
        self.assertEqual(other.json()["average_rating"], 8.9)

        detail = self.client.get(f"/api/script-tests/{run.id}").json()
        by_user = {row["reviewer_user_id"]: row["rating"] for row in detail["ratings"]}
        self.assertEqual(by_user[self.user.id], 9.8)
        self.assertEqual(by_user[self.admin.id], 8.0)

        bad = self.client.put(f"/api/script-tests/{run.id}/rating", json={"rating": 10.1})
        self.assertEqual(bad.status_code, 422)
        negative = self.client.put(f"/api/script-tests/{run.id}/rating", json={"rating": -0.1})
        self.assertEqual(negative.status_code, 422)

        zero = self.client.put(f"/api/script-tests/{run.id}/rating", json={"rating": 0})
        self.assertEqual(zero.status_code, 200)
        ten = self.client.put(f"/api/script-tests/{run.id}/rating", json={"rating": 10})
        self.assertEqual(ten.status_code, 200)


if __name__ == "__main__":
    unittest.main()
