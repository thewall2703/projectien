"""Ask-the-library API for every logged-in user."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth import get_current_user, require_admin
from backend.database import get_db
from backend.models import User
from backend.transcript_search import ask, rebuild_index

router = APIRouter(prefix="/api", tags=["ask"], dependencies=[Depends(get_current_user)])


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class AskSourceOut(BaseModel):
    source_type: str
    source_name: str
    text: str
    score: float
    start_ms: int | None = None
    end_ms: int | None = None
    highlight_lines: list[int] = Field(default_factory=list)


class AskSegmentTargetOut(BaseModel):
    source_index: int
    line_indexes: list[int] = Field(default_factory=list)


class AskSegmentOut(BaseModel):
    text: str
    source_indexes: list[int] = Field(default_factory=list)
    quote: str = ""
    targets: list[AskSegmentTargetOut] = Field(default_factory=list)


class AskOut(BaseModel):
    answer_markdown: str
    segments: list[AskSegmentOut] = Field(default_factory=list)
    highlights: list[str]
    sources: list[AskSourceOut]


class RebuildOut(BaseModel):
    deleted: int
    written: int
    style: int
    media: int
    quote: int
    drive: int


@router.post("/ask", response_model=AskOut)
def ask_transcripts(payload: AskIn, db: Session = Depends(get_db)) -> AskOut:
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question is required")
    try:
        result = ask(db, question)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return AskOut(
        answer_markdown=str(result.get("answer_markdown") or ""),
        segments=[AskSegmentOut(**item) for item in result.get("segments") or []],
        highlights=list(result.get("highlights") or []),
        sources=[AskSourceOut(**item) for item in result.get("sources") or []],
    )


@router.post("/ask/rebuild", response_model=RebuildOut)
def rebuild_transcript_index(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> RebuildOut:
    try:
        counts = rebuild_index(db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RebuildOut(**counts)
