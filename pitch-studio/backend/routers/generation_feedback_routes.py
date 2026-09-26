from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.database import get_db
from backend.models import Generation, GenerationFeedback, GenerationRating, User, utc_now
from backend.schemas import (
    GenerationFeedbackOut,
    GenerationRatingOut,
    GenerationReviewOut,
    ScriptTestFeedbackCreate,
    ScriptTestFeedbackUpdate,
    ScriptTestRatingUpsert,
    ScriptTestReviewDocument,
)
from backend.script_testing import build_review_document, find_review_target

router = APIRouter(prefix="/api/generations", tags=["generation-feedback"])


def _rating_stats(ratings: list[GenerationRating]) -> tuple[float | None, int]:
    if not ratings:
        return None, 0
    total = sum(float(row.rating) for row in ratings)
    count = len(ratings)
    return round(total / count, 1), count


def _feedback_out(row: GenerationFeedback, email: str = "") -> GenerationFeedbackOut:
    return GenerationFeedbackOut(
        id=row.id,
        generation_id=row.generation_id,
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
    row: GenerationRating,
    email: str = "",
    *,
    average_rating: float | None = None,
    rating_count: int = 0,
) -> GenerationRatingOut:
    return GenerationRatingOut(
        id=row.id,
        generation_id=row.generation_id,
        reviewer_user_id=row.reviewer_user_id,
        reviewer_email=email,
        rating=float(row.rating),
        created_at=row.created_at,
        updated_at=row.updated_at,
        average_rating=average_rating,
        rating_count=rating_count,
    )


def _get_accessible_generation(
    db: Session,
    generation_id: int,
    user: User,
) -> Generation:
    generation = db.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    if not user.is_admin and generation.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not allowed")
    return generation


def _require_reviewable(generation: Generation) -> dict:
    if generation.status != "done" or not (generation.script_json or "").strip():
        raise HTTPException(status_code=409, detail="Generation is not ready for feedback")
    try:
        script = json.loads(generation.script_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=409, detail="Generation is not ready for feedback") from exc
    if not isinstance(script, dict):
        raise HTTPException(status_code=409, detail="Generation is not ready for feedback")
    return script


def _emails_for(db: Session, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    return {
        user.id: user.email
        for user in db.query(User).filter(User.id.in_(user_ids)).all()
    }


@router.get("/{generation_id}/review", response_model=GenerationReviewOut)
def get_generation_review(
    generation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GenerationReviewOut:
    generation = _get_accessible_generation(db, generation_id, user)
    script = _require_reviewable(generation)
    review_document = ScriptTestReviewDocument.model_validate(build_review_document(script))
    feedback_rows = (
        db.query(GenerationFeedback)
        .filter(GenerationFeedback.generation_id == generation.id)
        .order_by(GenerationFeedback.created_at.asc())
        .all()
    )
    rating_rows = (
        db.query(GenerationRating)
        .filter(GenerationRating.generation_id == generation.id)
        .order_by(GenerationRating.created_at.asc())
        .all()
    )
    emails = _emails_for(
        db,
        {row.reviewer_user_id for row in feedback_rows} | {row.reviewer_user_id for row in rating_rows},
    )
    average, count = _rating_stats(rating_rows)
    return GenerationReviewOut(
        review_document=review_document,
        feedback=[_feedback_out(row, emails.get(row.reviewer_user_id, "")) for row in feedback_rows],
        ratings=[
            _rating_out(row, emails.get(row.reviewer_user_id, ""), average_rating=average, rating_count=count)
            for row in rating_rows
        ],
        average_rating=average,
        rating_count=count,
    )


@router.post("/{generation_id}/feedback", response_model=GenerationFeedbackOut)
def add_generation_feedback(
    generation_id: int,
    payload: ScriptTestFeedbackCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GenerationFeedbackOut:
    generation = _get_accessible_generation(db, generation_id, user)
    script = _require_reviewable(generation)
    review_document = build_review_document(script)
    target = find_review_target(review_document, payload.target_kind, payload.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Feedback target not found")
    row = GenerationFeedback(
        generation_id=generation.id,
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


@router.put("/{generation_id}/feedback/{feedback_id}", response_model=GenerationFeedbackOut)
def update_generation_feedback(
    generation_id: int,
    feedback_id: int,
    payload: ScriptTestFeedbackUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GenerationFeedbackOut:
    generation = _get_accessible_generation(db, generation_id, user)
    _require_reviewable(generation)
    row = db.get(GenerationFeedback, feedback_id)
    if row is None or row.generation_id != generation.id:
        raise HTTPException(status_code=404, detail="Feedback not found")
    if row.reviewer_user_id != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own feedback")
    row.comment = payload.comment
    db.commit()
    db.refresh(row)
    return _feedback_out(row, user.email)


@router.put("/{generation_id}/rating", response_model=GenerationRatingOut)
def upsert_generation_rating(
    generation_id: int,
    payload: ScriptTestRatingUpsert,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GenerationRatingOut:
    generation = _get_accessible_generation(db, generation_id, user)
    _require_reviewable(generation)
    # Round to one decimal to match UI step=0.1 without floating noise.
    rating_value = round(float(payload.rating), 1)
    if rating_value < 0 or rating_value > 10:
        raise HTTPException(status_code=422, detail="Rating must be between 0 and 10")
    row = (
        db.query(GenerationRating)
        .filter(
            GenerationRating.generation_id == generation.id,
            GenerationRating.reviewer_user_id == user.id,
        )
        .first()
    )
    now = utc_now()
    if row is None:
        row = GenerationRating(
            generation_id=generation.id,
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
        db.query(GenerationRating)
        .filter(GenerationRating.generation_id == generation.id)
        .all()
    )
    average, count = _rating_stats(all_ratings)
    return _rating_out(row, user.email, average_rating=average, rating_count=count)
