from __future__ import annotations

import unittest
from datetime import timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Job, utc_now
from backend import worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.engine.dispose()

    def test_claim_oldest_queued(self):
        db = self.Session()
        first = Job(job_type="media_prepare", media_id=1, asset_id=1, status="queued")
        second = Job(job_type="media_prepare", media_id=2, asset_id=2, status="queued")
        db.add_all([first, second])
        db.commit()
        claimed = worker.claim_job(db)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.media_id, 1)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(claimed.stage, "Starting")
        db.close()

    def test_process_one_success(self):
        db = self.Session()
        job = Job(job_type="media_prepare", media_id=1, asset_id=4, status="queued")
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        def fake_prepare(_db, asset_id, on_stage=None):
            self.assertEqual(asset_id, 4)
            if on_stage:
                on_stage("Downloading")
            return mock.Mock()

        with mock.patch.object(worker, "SessionLocal", self.Session):
            with mock.patch.object(worker, "prepare_media", side_effect=fake_prepare):
                self.assertTrue(worker.process_one())

        db = self.Session()
        done = db.get(Job, job_id)
        self.assertEqual(done.status, "done")
        self.assertEqual(done.stage, "Done")
        self.assertEqual(done.error, "")
        db.close()

    def test_process_qa_extract_job(self):
        db = self.Session()
        job = Job(job_type="qa_extract", media_id=9, asset_id=22, status="queued")
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        def fake_run(_db, run_id, on_stage=None):
            self.assertEqual(run_id, 22)
            if on_stage:
                on_stage("Extracting Q&A pairs")
            return mock.Mock()

        with mock.patch.object(worker, "SessionLocal", self.Session):
            with mock.patch.object(worker, "run_qa_extraction", side_effect=fake_run):
                self.assertTrue(worker.process_one())

        db = self.Session()
        done = db.get(Job, job_id)
        self.assertEqual(done.status, "done")
        db.close()

    def test_process_one_error(self):
        db = self.Session()
        job = Job(job_type="media_prepare", media_id=1, asset_id=4, status="queued")
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        with mock.patch.object(worker, "SessionLocal", self.Session):
            with mock.patch.object(worker, "prepare_media", side_effect=RuntimeError("boom")):
                self.assertTrue(worker.process_one())

        db = self.Session()
        failed = db.get(Job, job_id)
        self.assertEqual(failed.status, "error")
        self.assertIn("boom", failed.error)
        self.assertEqual(failed.stage, "Failed")
        db.close()

    def test_reclaim_stale_running(self):
        db = self.Session()
        job = Job(
            job_type="media_describe",
            media_id=5,
            asset_id=8,
            status="running",
            started_at=utc_now() - timedelta(hours=2),
        )
        db.add(job)
        db.commit()
        count = worker.reclaim_stale_jobs(db)
        self.assertEqual(count, 1)
        db.refresh(job)
        self.assertEqual(job.status, "queued")
        self.assertIsNone(job.started_at)
        db.close()

    def test_process_one_empty_queue(self):
        with mock.patch.object(worker, "SessionLocal", self.Session):
            self.assertFalse(worker.process_one())


if __name__ == "__main__":
    unittest.main()
