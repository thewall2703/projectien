from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import hash_password, verify_password
from backend.config import settings
from backend.database import Base, get_db
from backend.models import User
from backend.routers import auth_routes


class VerifyPasswordTests(unittest.TestCase):
    def test_empty_hash_is_rejected(self):
        self.assertFalse(verify_password("anything", ""))
        self.assertFalse(verify_password("changeme", ""))

    def test_valid_hash_accepted(self):
        hashed = hash_password("secret")
        self.assertTrue(verify_password("secret", hashed))
        self.assertFalse(verify_password("wrong", hashed))


class GoogleAuthRouteTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.app = FastAPI()
        self.app.include_router(auth_routes.router)

        def override_db():
            yield self.db

        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)
        self._settings = mock.patch.multiple(
            settings,
            google_client_id="test-client-id.apps.googleusercontent.com",
            google_allowed_domain="mastersunion.org",
            admin_emails="admin@mastersunion.org",
        )
        self._settings.start()

    def tearDown(self):
        self._settings.stop()
        self.db.close()

    def test_config_exposes_client_id(self):
        response = self.client.get("/api/auth/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["google_client_id"], "test-client-id.apps.googleusercontent.com")
        self.assertEqual(body["google_allowed_domain"], "mastersunion.org")

    def test_google_login_accepts_domain_account(self):
        token_info = {
            "email": "person@mastersunion.org",
            "email_verified": True,
            "hd": "mastersunion.org",
        }
        with mock.patch(
            "backend.routers.auth_routes.id_token.verify_oauth2_token",
            return_value=token_info,
        ):
            response = self.client.post("/api/auth/google", json={"credential": "fake-token"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["email"], "person@mastersunion.org")
        self.assertFalse(body["is_admin"])
        user = self.db.query(User).filter(User.email == "person@mastersunion.org").first()
        self.assertIsNotNone(user)
        self.assertEqual(user.password_hash, "")

    def test_google_login_promotes_admin_email(self):
        token_info = {
            "email": "admin@mastersunion.org",
            "email_verified": True,
            "hd": "mastersunion.org",
        }
        with mock.patch(
            "backend.routers.auth_routes.id_token.verify_oauth2_token",
            return_value=token_info,
        ):
            response = self.client.post("/api/auth/google", json={"credential": "fake-token"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["is_admin"])

    def test_google_login_never_demotes_admin(self):
        existing = User(
            email="admin@mastersunion.org",
            password_hash=hash_password("keep"),
            is_admin=True,
        )
        self.db.add(existing)
        self.db.commit()
        with mock.patch.object(settings, "admin_emails", ""):
            token_info = {
                "email": "admin@mastersunion.org",
                "email_verified": True,
                "hd": "mastersunion.org",
            }
            with mock.patch(
                "backend.routers.auth_routes.id_token.verify_oauth2_token",
                return_value=token_info,
            ):
                response = self.client.post("/api/auth/google", json={"credential": "fake-token"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["is_admin"])
        self.db.refresh(existing)
        self.assertTrue(existing.is_admin)

    def test_google_login_rejects_when_client_id_unset(self):
        with mock.patch.object(settings, "google_client_id", ""):
            response = self.client.post("/api/auth/google", json={"credential": "fake-token"})
        self.assertEqual(response.status_code, 503)

    def test_google_login_rejects_wrong_domain(self):
        token_info = {
            "email": "person@gmail.com",
            "email_verified": True,
            "hd": "gmail.com",
        }
        with mock.patch(
            "backend.routers.auth_routes.id_token.verify_oauth2_token",
            return_value=token_info,
        ):
            response = self.client.post("/api/auth/google", json={"credential": "fake-token"})
        self.assertEqual(response.status_code, 403)

    def test_google_login_rejects_unverified_email(self):
        token_info = {
            "email": "person@mastersunion.org",
            "email_verified": False,
            "hd": "mastersunion.org",
        }
        with mock.patch(
            "backend.routers.auth_routes.id_token.verify_oauth2_token",
            return_value=token_info,
        ):
            response = self.client.post("/api/auth/google", json={"credential": "fake-token"})
        self.assertEqual(response.status_code, 403)

    def test_password_login_rejected_for_google_only_user(self):
        self.db.add(
            User(email="person@mastersunion.org", password_hash="", is_admin=False)
        )
        self.db.commit()
        response = self.client.post(
            "/api/auth/login",
            json={"email": "person@mastersunion.org", "password": "anything"},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
