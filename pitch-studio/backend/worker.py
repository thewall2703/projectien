from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.database import SessionLocal, ensure_schema
from backend.media_index import JOB_DESCRIBE, JOB_PREPARE, describe_media, prepare_media
from backend.models import Job, utc_now

log = logging.getLogger("backend.worker")

POLL_SECONDS = 2
STALE_RUNNING_AFTER = timedelta(minutes=30)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _is_sqlite(db: Session) -> bool:
    return db.get_bind().dialect.name == "sqlite"


def reclaim_stale_jobs(db: Session) -> int:
    cutoff = utc_now() - STALE_RUNNING_AFTER
    stale = db.query(Job).filter(Job.status == "running").all()
    reclaimed = 0
    for job in stale:
        started = _aware(job.started_at)
        if started is None or started < cutoff:
            job.status = "queued"
            job.stage = "Requeued after worker restart"
            job.started_at = None
            job.error = ""
            reclaimed += 1
    if reclaimed:
        db.commit()
    return reclaimed


def claim_job(db: Session) -> Job | None:
    if _is_sqlite(db):
        return _claim_sqlite(db)
    return _claim_postgres(db)


def _claim_postgres(db: Session) -> Job | None:
    stmt = (
        select(Job)
        .where(Job.status == "queued")
        .order_by(Job.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = db.execute(stmt).scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.started_at = utc_now()
    job.stage = "Starting"
    db.commit()
    db.refresh(job)
    return job


def _claim_sqlite(db: Session) -> Job | None:
    candidate = db.query(Job).filter(Job.status == "queued").order_by(Job.id).first()
    if candidate is None:
        return None
    now = utc_now()
    result = db.execute(
        update(Job)
        .where(Job.id == candidate.id, Job.status == "queued")
        .values(status="running", started_at=now, stage="Starting")
    )
    db.commit()
    if result.rowcount != 1:
        return None
    db.refresh(candidate)
    return candidate


def _run_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return

        def on_stage(text: str) -> None:
            current = db.get(Job, job_id)
            if current is None:
                return
            current.stage = text
            db.commit()

        if job.job_type == JOB_PREPARE:
            prepare_media(db, job.asset_id, on_stage=on_stage)
        elif job.job_type == JOB_DESCRIBE:
            describe_media(db, job.media_id, on_stage=on_stage)
        else:
            raise RuntimeError(f"Unknown job type: {job.job_type}")

        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = "done"
        job.stage = "Done"
        job.finished_at = utc_now()
        job.error = ""
        db.commit()
    except Exception as exc:
        log.exception("Job %s failed", job_id)
        try:
            db.rollback()
        except Exception:
            pass
        try:
            failed = db.get(Job, job_id)
            if failed is not None:
                failed.status = "error"
                failed.error = str(exc)
                failed.finished_at = utc_now()
                failed.stage = "Failed"
                db.commit()
        except Exception:
            log.exception("Failed to mark job %s as error", job_id)
    finally:
        db.close()


def process_one() -> bool:
    db = SessionLocal()
    try:
        reclaim_stale_jobs(db)
        job = claim_job(db)
        job_id = job.id if job is not None else None
    finally:
        db.close()
    if job_id is None:
        return False
    _run_job(job_id)
    return True


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ensure_schema()
    log.info("Worker started")
    while True:
        try:
            did = process_one()
        except Exception:
            log.exception("Worker loop error")
            did = False
        if not did:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run_forever()
