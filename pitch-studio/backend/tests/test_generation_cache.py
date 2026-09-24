from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.config import settings
from backend.database import Base, get_db
from backend.generation_cache import (
    bump_content_version,
    compute_cache_key,
    deploy_version,
    get_content_version,
    reset_deploy_version_cache,
)
from backend.models import Generation, User
from backend.routers import generation_routes


class GenerationCacheTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.user_a = User(email="a@example.com", password_hash="x", is_admin=False)
        self.user_b = User(email="b@example.com", password_hash="x", is_admin=False)
        self.db.add_all([self.user_a, self.user_b])
        self.db.commit()
        self.db.refresh(self.user_a)
        self.db.refresh(self.user_b)

        self.app = FastAPI()
        self.app.include_router(generation_routes.router)

        def override_db():
            yield self.db

        self.current = self.user_a

        def override_user():
            return self.current

        self.app.dependency_overrides[get_db] = override_db
        self.app.dependency_overrides[get_current_user] = override_user
        self.client = TestClient(self.app)

        self._session_local = mock.patch(
            "backend.database.SessionLocal",
            self.Session,
        )
        self._session_local.start()
        self._deploy = mock.patch.object(settings, "deploy_version", "test-deploy-v1")
        self._deploy.start()
        reset_deploy_version_cache()

        self.run_calls: list[int] = []

        def fake_run(generation_id: int) -> None:
            self.run_calls.append(generation_id)

        self._run = mock.patch(
            "backend.routers.generation_routes.run_generation",
            side_effect=fake_run,
        )
        self._run.start()

        self._media = mock.patch(
            "backend.routers.generation_routes.pick_recommended_media",
            return_value=([], []),
        )
        self._media.start()

    def tearDown(self):
        self._media.stop()
        self._run.stop()
        self._deploy.stop()
        self._session_local.stop()
        reset_deploy_version_cache()
        self.db.close()

    def _axes_payload(self, **overrides):
        body = {
            "audience_cluster": "A",
            "duration": "T1",
            "channel": "CH1",
            "intent": "I2",
            "temperature": "X2",
            "context_note": "  Hello   World  ",
            "recipe_ref": "",
        }
        body.update(overrides)
        return body

    def _seed_done(self, user: User, **overrides) -> Generation:
        axes = {
            "audience_cluster": "A",
            "duration": "T1",
            "channel": "CH1",
            "intent": "I2",
        }
        context_note = overrides.pop("context_note", "Hello World")
        temperature = overrides.pop("temperature", "X2")
        recipe_ref = overrides.pop("recipe_ref", "")
        cache_key = compute_cache_key(axes, temperature, context_note, recipe_ref, self.db)
        row = Generation(
            user_id=user.id,
            **axes,
            temperature=temperature,
            context_note=context_note,
            recipe_ref=recipe_ref,
            module_sequence="M01>M14",
            status=overrides.pop("status", "done"),
            script_json='{"sections":[],"cta":"Go"}',
            deck_spec_json='{"slides":[]}',
            pptx_path="decks/cached.pptx",
            asset_ids="1,2",
            objection_ids="3",
            founder_quote_ids="4",
            report_asset_ids="5",
            report_passages_json="[]",
            validation_report="ok",
            cache_key=cache_key,
            **overrides,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def test_cache_hit_copies_for_second_user_without_background_run(self):
        source = self._seed_done(self.user_a)
        self.current = self.user_b
        response = self.client.post("/api/generations", json=self._axes_payload())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["cached_from_id"], source.id)
        self.assertEqual(body["script"]["cta"], "Go")
        self.assertEqual(body["pptx_path"], "decks/cached.pptx")
        self.assertEqual(body["user_id"], self.user_b.id)
        self.assertEqual(self.run_calls, [])

    def test_failed_generation_not_reused(self):
        self._seed_done(self.user_a, status="failed")
        self.current = self.user_b
        response = self.client.post("/api/generations", json=self._axes_payload())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertIsNone(body.get("cached_from_id"))
        self.assertEqual(len(self.run_calls), 1)

    def test_content_version_bump_causes_miss(self):
        self._seed_done(self.user_a)
        bump_content_version()
        self.current = self.user_b
        response = self.client.post("/api/generations", json=self._axes_payload())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertIsNone(body.get("cached_from_id"))
        self.assertEqual(len(self.run_calls), 1)

    def test_deploy_version_change_causes_miss(self):
        self._seed_done(self.user_a)
        with mock.patch.object(settings, "deploy_version", "test-deploy-v2"):
            reset_deploy_version_cache()
            self.current = self.user_b
            response = self.client.post("/api/generations", json=self._axes_payload())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(len(self.run_calls), 1)

    def test_list_includes_user_email(self):
        self._seed_done(self.user_a)
        self.current = self.user_a
        self.user_a.is_admin = True
        self.db.commit()
        response = self.client.get("/api/generations")
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(rows[0]["user_email"], "a@example.com")

    def test_content_version_starts_at_zero_and_bumps(self):
        self.assertEqual(get_content_version(self.db), 0)
        self.assertEqual(bump_content_version(), 1)
        self.assertEqual(get_content_version(self.db), 1)
        self.assertEqual(deploy_version(), "test-deploy-v1")


if __name__ == "__main__":
    unittest.main()
