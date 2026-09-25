from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.auth import get_current_user, require_admin
from backend.database import get_db
from backend.models import Recipe, ScriptTestFeedback, ScriptTestRating, ScriptTestRun, User, utc_now
from backend.pipeline.resolver import is_valid_sequence, parse_sequence
from backend.pipeline.vision_deck import normalize_use_case
from backend.schemas import (
    ScriptTestCreate,
    ScriptTestFeedbackCreate,
    ScriptTestFeedbackOut,
    ScriptTestFeedbackUpdate,
    ScriptTestRatingOut,
    ScriptTestRatingUpsert,
    ScriptTestReviewDocument,
    ScriptTestRunListItem,
    ScriptTestRunOut,
)
from backend.script_testing import find_review_target, run_script_test

router = APIRouter(prefix="/api/script-tests", tags=["script-tests"], dependencies=[Depends(require_admin)])


def _rating_stats(ratings: list[ScriptTestRating]) -> tuple[float | None, int]:
    if not ratings:
        return None, 0
    total = sum(float(row.rating) for row in ratings)
    count = len(ratings)
    return round(total / count, 1), count


def _feedback_out(row: ScriptTestFeedback, email: str = "") -> ScriptTestFeedbackOut:
    return ScriptTestFeedbackOut(
        id=row.id,
        script_test_run_id=row.script_test_run_id,
        reviewer_user_id=row.reviewer_user_id,
        reviewer_email=email,
        target_kind=row.target_kind,
        target_id=row.target_id,
        section_index=row.section_index,
        paragraph_index=row.paragraph_index,
        sentence_index=row.sentence_index,
        reference_text=row.reference_text,
        comment=row.comment,
        created_at=row.created_at,
    )


def _rating_out(
    row: ScriptTestRating,
    email: str = "",
    *,
    average_rating: float | None = None,
    rating_count: int = 0,
) -> ScriptTestRatingOut:
    return ScriptTestRatingOut(
        id=row.id,
        script_test_run_id=row.script_test_run_id,
        reviewer_user_id=row.reviewer_user_id,
        reviewer_email=email,
        rating=float(row.rating),
        created_at=row.created_at,
        updated_at=row.updated_at,
        average_rating=average_rating,
        rating_count=rating_count,
    )


def _list_item(run: ScriptTestRun, email: str, ratings: list[ScriptTestRating]) -> ScriptTestRunListItem:
    average, count = _rating_stats(ratings)
    return ScriptTestRunListItem(
        id=run.id,
        created_by_user_id=run.created_by_user_id,
        created_by_email=email,
        audience_cluster=run.audience_cluster,
        duration=run.duration,
        channel=run.channel,
        intent=run.intent,
        temperature=run.temperature,
        context_note=run.context_note,
        recipe_ref=run.recipe_ref,
        module_sequence=run.module_sequence,
        status=run.status,
        validation_report=run.validation_report,
        error=run.error,
        created_at=run.created_at,
        finished_at=run.finished_at,
        average_rating=average,
        rating_count=count,
    )


def _enrich(db: Session, run: ScriptTestRun) -> ScriptTestRunOut:
    creator = db.get(User, run.created_by_user_id)
    feedback_rows = (
        db.query(ScriptTestFeedback)
        .filter(ScriptTestFeedback.script_test_run_id == run.id)
        .order_by(ScriptTestFeedback.created_at.asc())
        .all()
    )
    rating_rows = (
        db.query(ScriptTestRating)
        .filter(ScriptTestRating.script_test_run_id == run.id)
        .order_by(ScriptTestRating.created_at.asc())
        .all()
    )
    user_ids = {row.reviewer_user_id for row in feedback_rows} | {
        row.reviewer_user_id for row in rating_rows
    }
    emails = {
        user.id: user.email
        for user in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}
    average, count = _rating_stats(rating_rows)

    payload = ScriptTestRunOut(
        id=run.id,
        created_by_user_id=run.created_by_user_id,
        created_by_email=creator.email if creator else "",
        audience_cluster=run.audience_cluster,
        duration=run.duration,
        channel=run.channel,
        intent=run.intent,
        temperature=run.temperature,
        context_note=run.context_note,
        recipe_ref=run.recipe_ref,
        module_sequence=run.module_sequence,
        status=run.status,
        script_json=run.script_json,
        review_document_json=run.review_document_json,
        validation_report=run.validation_report,
        error=run.error,
        founder_quote_ids=run.founder_quote_ids,
        report_asset_ids=run.report_asset_ids,
        report_passages_json=run.report_passages_json,
        created_at=run.created_at,
        finished_at=run.finished_at,
        feedback=[_feedback_out(row, emails.get(row.reviewer_user_id, "")) for row in feedback_rows],
        ratings=[
            _rating_out(row, emails.get(row.reviewer_user_id, ""), average_rating=average, rating_count=count)
            for row in rating_rows
        ],
        average_rating=average,
        rating_count=count,
    )
    if run.script_json:
        try:
            payload.script = json.loads(run.script_json)
        except json.JSONDecodeError:
            payload.script = None
    if run.review_document_json:
        try:
            payload.review_document = ScriptTestReviewDocument.model_validate(
                json.loads(run.review_document_json)
            )
        except (json.JSONDecodeError, ValueError):
            payload.review_document = None
    return payload


@router.post("", response_model=ScriptTestRunOut)
def create_script_test(
    payload: ScriptTestCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ScriptTestRunOut:
    recipe_ref = payload.recipe_ref or ""
    axes = {
        "audience_cluster": payload.audience_cluster,
        "duration": payload.duration,
        "channel": payload.channel,
        "intent": payload.intent,
    }
    if recipe_ref:
        recipe = db.query(Recipe).filter(Recipe.ref == recipe_ref).first()
        if recipe is None:
            raise HTTPException(status_code=404, detail="Recipe not found")
        if not is_valid_sequence(parse_sequence(recipe.module_sequence)):
            raise HTTPException(
                status_code=400,
                detail=f"Recipe {recipe_ref} has a non-module sequence and cannot be generated",
            )
        axes = {
            "audience_cluster": axes["audience_cluster"] or recipe.audience_cluster,
            "duration": axes["duration"] or recipe.duration,
            "channel": axes["channel"] or recipe.channel,
            "intent": axes["intent"] or recipe.intent,
        }
    run = ScriptTestRun(
        created_by_user_id=user.id,
        **axes,
        temperature=payload.temperature,
        context_note=payload.context_note,
        recipe_ref=recipe_ref,
        deck_use_case=normalize_use_case(payload.deck_use_case),
        status="queued",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    background.add_task(run_script_test, run.id)
    return _enrich(db, run)


@router.get("", response_model=list[ScriptTestRunListItem])
def list_script_tests(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ScriptTestRunListItem]:
    _ = user
    rows = db.query(ScriptTestRun).order_by(ScriptTestRun.created_at.desc()).all()
    if not rows:
        return []
    creator_ids = {row.created_by_user_id for row in rows}
    emails = {
        item.id: item.email
        for item in db.query(User).filter(User.id.in_(creator_ids)).all()
    }
    run_ids = [row.id for row in rows]
    ratings = (
        db.query(ScriptTestRating)
        .filter(ScriptTestRating.script_test_run_id.in_(run_ids))
        .all()
    )
    by_run: dict[int, list[ScriptTestRating]] = {run_id: [] for run_id in run_ids}
    for rating in ratings:
        by_run.setdefault(rating.script_test_run_id, []).append(rating)
    return [
        _list_item(row, emails.get(row.created_by_user_id, ""), by_run.get(row.id, []))
        for row in rows
    ]


@router.get("/{run_id}", response_model=ScriptTestRunOut)
def get_script_test(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ScriptTestRunOut:
    _ = user
    run = db.get(ScriptTestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Script test not found")
    return _enrich(db, run)


@router.post("/{run_id}/feedback", response_model=ScriptTestFeedbackOut)
def add_script_test_feedback(
    run_id: int,
    payload: ScriptTestFeedbackCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ScriptTestFeedbackOut:
    run = db.get(ScriptTestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Script test not found")
    if run.status != "done":
        raise HTTPException(status_code=409, detail="Script test is not ready for feedback")
    try:
        review_document = json.loads(run.review_document_json or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=404, detail="Review document not found") from exc
    target = find_review_target(review_document, payload.target_kind, payload.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Feedback target not found")
    row = ScriptTestFeedback(
        script_test_run_id=run.id,
        reviewer_user_id=user.id,
        target_kind=target["target_kind"],
        target_id=target["target_id"],
        section_index=target["section_index"],
        paragraph_index=target["paragraph_index"],
        sentence_index=target["sentence_index"],
        reference_text=target["reference_text"],
        comment=payload.comment,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _feedback_out(row, user.email)


@router.put("/{run_id}/feedback/{feedback_id}", response_model=ScriptTestFeedbackOut)
def update_script_test_feedback(
    run_id: int,
    feedback_id: int,
    payload: ScriptTestFeedbackUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ScriptTestFeedbackOut:
    run = db.get(ScriptTestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Script test not found")
    if run.status != "done":
        raise HTTPException(status_code=409, detail="Script test is not ready for feedback")
    row = db.get(ScriptTestFeedback, feedback_id)
    if row is None or row.script_test_run_id != run.id:
        raise HTTPException(status_code=404, detail="Feedback not found")
    if row.reviewer_user_id != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own feedback")
    row.comment = payload.comment
    db.commit()
    db.refresh(row)
    return _feedback_out(row, user.email)


@router.put("/{run_id}/rating", response_model=ScriptTestRatingOut)
def upsert_script_test_rating(
    run_id: int,
    payload: ScriptTestRatingUpsert,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ScriptTestRatingOut:
    run = db.get(ScriptTestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Script test not found")
    if run.status != "done":
        raise HTTPException(status_code=409, detail="Script test is not ready for rating")
    # Round to one decimal to match UI step=0.1 without floating noise.
    rating_value = round(float(payload.rating), 1)
    if rating_value < 0 or rating_value > 10:
        raise HTTPException(status_code=422, detail="Rating must be between 0 and 10")
    row = (
        db.query(ScriptTestRating)
        .filter(
            ScriptTestRating.script_test_run_id == run.id,
            ScriptTestRating.reviewer_user_id == user.id,
        )
        .first()
    )
    now = utc_now()
    if row is None:
        row = ScriptTestRating(
            script_test_run_id=run.id,
            reviewer_user_id=user.id,
            rating=rating_value,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.rating = rating_value
        row.updated_at = now
    db.commit()
    db.refresh(row)
    all_ratings = (
        db.query(ScriptTestRating)
        .filter(ScriptTestRating.script_test_run_id == run.id)
        .all()
    )
    average, count = _rating_stats(all_ratings)
    return _rating_out(row, user.email, average_rating=average, rating_count=count)
