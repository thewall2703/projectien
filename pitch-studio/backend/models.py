from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect as sa_inspect,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    generations: Mapped[list[Generation]] = relationship(back_populates="user")
    script_test_runs: Mapped[list[ScriptTestRun]] = relationship(back_populates="created_by")


class Module(Base):
    __tablename__ = "modules"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    job: Mapped[str] = mapped_column(Text, default="")
    core_content: Mapped[str] = mapped_column(Text, default="")
    flex_points: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[str] = mapped_column(String(500), default="")
    owner: Mapped[str] = mapped_column(String(255), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)


class LockedFact(Base):
    __tablename__ = "locked_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fact: Mapped[str] = mapped_column(String(255))
    value: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(32), default="needs_source")
    note: Mapped[str] = mapped_column(Text, default="")
    module_ids: Mapped[str] = mapped_column(String(255), default="")
    edited: Mapped[bool] = mapped_column(Boolean, default=False)


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    audience_label: Mapped[str] = mapped_column(String(255), default="")
    audience_cluster: Mapped[str] = mapped_column(String(16), default="")
    duration: Mapped[str] = mapped_column(String(8), default="")
    channel: Mapped[str] = mapped_column(String(16), default="")
    intent: Mapped[str] = mapped_column(String(8), default="")
    temperature: Mapped[str | None] = mapped_column(String(8), nullable=True)
    module_sequence: Mapped[str] = mapped_column(String(500), default="")
    word_budget: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[str] = mapped_column(String(8), default="P2")
    owner: Mapped[str] = mapped_column(String(255), default="")
    edited: Mapped[bool] = mapped_column(Boolean, default=False)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    file_key: Mapped[str] = mapped_column(String(500), default="")
    content_type: Mapped[str] = mapped_column(String(128), default="")
    matrix_ref: Mapped[str] = mapped_column(String(16), default="")
    file_status: Mapped[str] = mapped_column(String(16), default="pending")
    sync_error: Mapped[str] = mapped_column(Text, default="")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    module_ids: Mapped[str] = mapped_column(String(255), default="")
    audiences: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="exists")
    notes: Mapped[str] = mapped_column(Text, default="")
    extract_status: Mapped[str] = mapped_column(String(16), default="")
    extract_error: Mapped[str] = mapped_column(Text, default="")
    extract_json: Mapped[str] = mapped_column(Text, default="")
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)


class Objection(Base):
    __tablename__ = "objections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    who_asks: Mapped[str] = mapped_column(String(255), default="")
    move: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="approved")
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    source_name: Mapped[str] = mapped_column(String(300), default="")
    source_transcript_id: Mapped[int] = mapped_column(Integer, default=0)
    source_candidate_id: Mapped[int] = mapped_column(Integer, default=0)


class Generation(Base):
    __tablename__ = "generations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    audience_cluster: Mapped[str] = mapped_column(String(16))
    duration: Mapped[str] = mapped_column(String(8))
    channel: Mapped[str] = mapped_column(String(16))
    intent: Mapped[str] = mapped_column(String(8))
    temperature: Mapped[str] = mapped_column(String(8))
    context_note: Mapped[str] = mapped_column(Text, default="")
    recipe_ref: Mapped[str] = mapped_column(String(32), default="")
    deck_use_case: Mapped[str] = mapped_column(String(64), default="")
    module_sequence: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(32), default="queued")
    script_json: Mapped[str] = mapped_column(Text, default="")
    deck_spec_json: Mapped[str] = mapped_column(Text, default="")
    pptx_path: Mapped[str] = mapped_column(String(500), default="")
    asset_ids: Mapped[str] = mapped_column(String(255), default="")
    objection_ids: Mapped[str] = mapped_column(String(255), default="")
    founder_quote_ids: Mapped[str] = mapped_column(String(255), default="")
    report_asset_ids: Mapped[str] = mapped_column(String(255), default="")
    report_passages_json: Mapped[str] = mapped_column(Text, default="")
    validation_report: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    script_plan_json: Mapped[str] = mapped_column(Text, default="")
    quality_trace_json: Mapped[str] = mapped_column(Text, default="")
    cache_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    cached_from_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped[User] = relationship(back_populates="generations")
    feedback: Mapped[list[GenerationFeedback]] = relationship(back_populates="generation")
    ratings: Mapped[list[GenerationRating]] = relationship(back_populates="generation")


class GenerationFeedback(Base):
    __tablename__ = "generation_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_id: Mapped[int] = mapped_column(ForeignKey("generations.id"), index=True)
    reviewer_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    target_kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(128), index=True)
    section_index: Mapped[int] = mapped_column(Integer, default=0)
    paragraph_index: Mapped[int] = mapped_column(Integer, default=0)
    sentence_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reference_text: Mapped[str] = mapped_column(Text, default="")
    comment: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    generation: Mapped[Generation] = relationship(back_populates="feedback")
    reviewer: Mapped[User] = relationship()


class GenerationRating(Base):
    __tablename__ = "generation_ratings"
    __table_args__ = (
        UniqueConstraint(
            "generation_id",
            "reviewer_user_id",
            name="uq_generation_rating_generation_reviewer",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_id: Mapped[int] = mapped_column(ForeignKey("generations.id"), index=True)
    reviewer_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    rating: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    generation: Mapped[Generation] = relationship(back_populates="ratings")
    reviewer: Mapped[User] = relationship()


class DeckVisionRow(Base):
    """One section × use-case row from the Deck - Vision Mapping sheet."""

    __tablename__ = "deck_vision_rows"
    __table_args__ = (
        UniqueConstraint("use_case", "section_order", name="uq_deck_vision_use_case_section"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section: Mapped[str] = mapped_column(String(255), default="")
    section_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    use_case: Mapped[str] = mapped_column(String(64), index=True)
    pages_json: Mapped[str] = mapped_column(Text, default="[]")
    section_pages_json: Mapped[str] = mapped_column(Text, default="[]")
    needs_more: Mapped[bool] = mapped_column(Boolean, default=False)
    logline: Mapped[str] = mapped_column(Text, default="")
    instructions_json: Mapped[str] = mapped_column(Text, default="[]")
    design_status: Mapped[str] = mapped_column(String(255), default="")
    slide_status: Mapped[str] = mapped_column(String(255), default="")


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class GeneratedSlide(Base):
    """A shared, content-addressed slide rendered from a brand template."""

    __tablename__ = "generated_slides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slide_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    claim_hash: Mapped[str] = mapped_column(String(64), index=True)
    render_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    template_id: Mapped[str] = mapped_column(String(64), index=True)
    template_version: Mapped[str] = mapped_column(String(32), default="1")
    tone: Mapped[str] = mapped_column(String(16), default="light")
    slot_values_json: Mapped[str] = mapped_column(Text, default="{}")
    file_key: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(24), default="ready", index=True)
    edited_by_human: Mapped[bool] = mapped_column(Boolean, default=False)
    source_fact_ids: Mapped[str] = mapped_column(String(1000), default="")
    source_asset_ids: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GeneratedSlideAttempt(Base):
    """Append-only generation audit; review fields may be annotated later."""

    __tablename__ = "generated_slide_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_id: Mapped[int] = mapped_column(ForeignKey("generations.id"), index=True)
    generated_slide_id: Mapped[int | None] = mapped_column(
        ForeignKey("generated_slides.id"), nullable=True, index=True
    )
    placeholder_key: Mapped[str] = mapped_column(String(255), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=0)
    claim: Mapped[str] = mapped_column(Text, default="")
    template_id: Mapped[str] = mapped_column(String(64), index=True)
    tone: Mapped[str] = mapped_column(String(16), default="light")
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    gate: Mapped[str] = mapped_column(String(32), default="")
    violations_json: Mapped[str] = mapped_column(Text, default="[]")
    slot_values_json: Mapped[str] = mapped_column(Text, default="{}")
    render_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    file_key: Mapped[str] = mapped_column(String(500), default="")
    review_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    use_as_guidance: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reviewer_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


_ATTEMPT_REVIEW_FIELDS = {
    "review_status",
    "review_note",
    "use_as_guidance",
    "reviewer_user_id",
    "reviewed_at",
}


@event.listens_for(GeneratedSlideAttempt, "before_update")
def _protect_generated_slide_attempt_audit_fields(_mapper, _connection, target) -> None:
    changed = {
        attribute.key
        for attribute in sa_inspect(target).attrs
        if attribute.history.has_changes()
    }
    immutable_changes = changed - _ATTEMPT_REVIEW_FIELDS
    if immutable_changes:
        names = ", ".join(sorted(immutable_changes))
        raise ValueError(f"Generated slide attempt audit fields are immutable: {names}")


@event.listens_for(GeneratedSlideAttempt, "before_delete")
def _prevent_generated_slide_attempt_delete(_mapper, _connection, _target) -> None:
    raise ValueError("Generated slide attempt audit records cannot be deleted")


class ScriptTestRun(Base):
    __tablename__ = "script_test_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    audience_cluster: Mapped[str] = mapped_column(String(16))
    duration: Mapped[str] = mapped_column(String(8))
    channel: Mapped[str] = mapped_column(String(16))
    intent: Mapped[str] = mapped_column(String(8))
    temperature: Mapped[str] = mapped_column(String(8))
    context_note: Mapped[str] = mapped_column(Text, default="")
    recipe_ref: Mapped[str] = mapped_column(String(32), default="")
    deck_use_case: Mapped[str] = mapped_column(String(64), default="")
    module_sequence: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(32), default="queued")
    script_json: Mapped[str] = mapped_column(Text, default="")
    review_document_json: Mapped[str] = mapped_column(Text, default="")
    validation_report: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    founder_quote_ids: Mapped[str] = mapped_column(String(255), default="")
    report_asset_ids: Mapped[str] = mapped_column(String(255), default="")
    report_passages_json: Mapped[str] = mapped_column(Text, default="")
    script_plan_json: Mapped[str] = mapped_column(Text, default="")
    quality_trace_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[User] = relationship(back_populates="script_test_runs")
    feedback: Mapped[list[ScriptTestFeedback]] = relationship(back_populates="run")
    ratings: Mapped[list[ScriptTestRating]] = relationship(back_populates="run")


class ScriptTestFeedback(Base):
    __tablename__ = "script_test_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    script_test_run_id: Mapped[int] = mapped_column(ForeignKey("script_test_runs.id"), index=True)
    reviewer_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    target_kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(128), index=True)
    section_index: Mapped[int] = mapped_column(Integer, default=0)
    paragraph_index: Mapped[int] = mapped_column(Integer, default=0)
    sentence_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reference_text: Mapped[str] = mapped_column(Text, default="")
    comment: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    run: Mapped[ScriptTestRun] = relationship(back_populates="feedback")
    reviewer: Mapped[User] = relationship()


class ScriptTestRating(Base):
    __tablename__ = "script_test_ratings"
    __table_args__ = (
        UniqueConstraint(
            "script_test_run_id",
            "reviewer_user_id",
            name="uq_script_test_rating_run_reviewer",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    script_test_run_id: Mapped[int] = mapped_column(ForeignKey("script_test_runs.id"), index=True)
    reviewer_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    rating: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    run: Mapped[ScriptTestRun] = relationship(back_populates="ratings")
    reviewer: Mapped[User] = relationship()


class FounderQuote(Base):
    __tablename__ = "founder_quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    verbatim: Mapped[bool] = mapped_column(Boolean, default=False)
    topic: Mapped[str] = mapped_column(String(32), default="founder")
    module_ids: Mapped[str] = mapped_column(String(255), default="")
    audiences: Mapped[str] = mapped_column(String(255), default="")
    source_name: Mapped[str] = mapped_column(String(255), default="")
    source_file_id: Mapped[str] = mapped_column(String(128), default="")
    source_style_transcript_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    start_sec: Mapped[float] = mapped_column(Float, default=0.0)
    end_sec: Mapped[float] = mapped_column(Float, default=0.0)
    speaker: Mapped[str] = mapped_column(String(128), default="Pratham Mittal")
    text_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="approved")
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PrathamMove(Base):
    """A reusable delivery move (framework, analogy, story, ...) from Pratham's real speech."""

    __tablename__ = "pratham_moves"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    style_transcript_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="framework")
    label: Mapped[str] = mapped_column(String(200), default="")
    excerpt: Mapped[str] = mapped_column(Text)
    use_when: Mapped[str] = mapped_column(Text, default="")
    personal: Mapped[bool] = mapped_column(Boolean, default=False)
    module_ids: Mapped[str] = mapped_column(String(255), default="")
    source_name: Mapped[str] = mapped_column(String(300), default="")
    embedding_json: Mapped[str] = mapped_column(Text, default="")
    text_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="approved")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PrathamPassage(Base):
    """A contiguous Pratham-only passage tagged to modules for beat-level retrieval."""

    __tablename__ = "pratham_passages"
    __table_args__ = (
        UniqueConstraint("style_transcript_id", "passage_index", name="uq_pratham_passage_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    style_transcript_id: Mapped[int] = mapped_column(Integer, index=True)
    passage_index: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    module_ids: Mapped[str] = mapped_column(String(255), default="")
    module_strengths_json: Mapped[str] = mapped_column(Text, default="{}")
    audience: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[str] = mapped_column(String(400), default="")
    usable: Mapped[bool] = mapped_column(Boolean, default=True)
    source_name: Mapped[str] = mapped_column(String(300), default="")
    embedding_json: Mapped[str] = mapped_column(Text, default="")
    text_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MediaIndex(Base):
    __tablename__ = "media_index"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    media_kind: Mapped[str] = mapped_column(String(16))
    transcript: Mapped[str] = mapped_column(Text, default="")
    visual_description: Mapped[str] = mapped_column(Text, default="")
    image_keys: Mapped[str] = mapped_column(Text, default="")
    vision: Mapped[str] = mapped_column(Text, default="")
    vision_hash: Mapped[str] = mapped_column(String(64), default="")
    indexed_vision_hash: Mapped[str] = mapped_column(String(64), default="")
    recommendations_json: Mapped[str] = mapped_column(Text, default="")
    recommended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    feedback_json: Mapped[str] = mapped_column(Text, default="")
    vision_frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DeckTopic(Base):
    __tablename__ = "deck_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deck: Mapped[str] = mapped_column(String(16), default="brand", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    pages_json: Mapped[str] = mapped_column(Text, default="[]")
    summary: Mapped[str] = mapped_column(Text, default="")
    module_ids: Mapped[str] = mapped_column(String(255), default="")
    vision: Mapped[str] = mapped_column(Text, default="")
    vision_hash: Mapped[str] = mapped_column(String(64), default="")
    indexed_vision_hash: Mapped[str] = mapped_column(String(64), default="")
    recommendations_json: Mapped[str] = mapped_column(Text, default="")
    recommended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    feedback_json: Mapped[str] = mapped_column(Text, default="")
    vision_frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    source_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(32), index=True)
    media_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    asset_id: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(255), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VideoClickEvent(Base):
    __tablename__ = "video_click_events"
    __table_args__ = (
        UniqueConstraint(
            "generation_id",
            "user_id",
            "asset_id",
            name="uq_video_click_generation_user_asset",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    asset_id: Mapped[int] = mapped_column(Integer, index=True)
    recipe_ref: Mapped[str] = mapped_column(String(32), default="", index=True)
    displayed_rank: Mapped[int] = mapped_column(Integer, default=0)
    click_order: Mapped[int] = mapped_column(Integer, default=0)
    interaction_type: Mapped[str] = mapped_column(String(32), default="play")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class StyleTranscript(Base):
    __tablename__ = "style_transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    raw_text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[str] = mapped_column(String(16), default="processed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class StyleTranscriptPersona(Base):
    __tablename__ = "style_transcript_personas"
    __table_args__ = (
        UniqueConstraint(
            "style_transcript_id",
            "persona_label",
            name="uq_style_transcript_persona",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    style_transcript_id: Mapped[int] = mapped_column(Integer, index=True)
    persona_label: Mapped[str] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class VoiceStyleGuide(Base):
    __tablename__ = "voice_style_guides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    persona_label: Mapped[str] = mapped_column(String(255), default="", index=True)
    guide_text: Mapped[str] = mapped_column(Text)
    source_transcript_ids: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ListenerTurn(Base):
    """One audience-member turn extracted from an AMA style transcript.

    Never used as factual evidence for scripts — attitudes and calibration only.
    """

    __tablename__ = "listener_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    style_transcript_id: Mapped[int] = mapped_column(Integer, index=True)
    self_description: Mapped[str] = mapped_column(Text, default="")
    concern: Mapped[str] = mapped_column(Text, default="")
    question_verbatim: Mapped[str] = mapped_column(Text, default="")
    reaction_after_answer: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(16), default="unclear", index=True)
    answer_summary: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ListenerProfile(Base):
    """Versioned per-persona aggregate of real listener attitudes."""

    __tablename__ = "listener_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    persona_label: Mapped[str] = mapped_column(String(255), default="", index=True)
    profile_text: Mapped[str] = mapped_column(Text)
    source_transcript_ids: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class QaExtractionRun(Base):
    __tablename__ = "qa_extraction_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    style_transcript_id: Mapped[int] = mapped_column(Integer, index=True)
    transcript_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(255), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    job_id: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QaCandidate(Base):
    __tablename__ = "qa_candidates"
    __table_args__ = (
        UniqueConstraint("style_transcript_id", "fingerprint", name="uq_qa_candidate_transcript_fingerprint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    style_transcript_id: Mapped[int] = mapped_column(Integer, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    match_type: Mapped[str] = mapped_column(String(16), default="new")
    matched_objection_id: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    question_verbatim: Mapped[str] = mapped_column(Text, default="")
    answer_verbatim: Mapped[str] = mapped_column(Text, default="")
    proposed_question: Mapped[str] = mapped_column(Text, default="")
    proposed_who_asks: Mapped[str] = mapped_column(String(255), default="")
    proposed_move: Mapped[str] = mapped_column(Text, default="")
    proposed_answer: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="")
    prior_question: Mapped[str] = mapped_column(Text, default="")
    prior_who_asks: Mapped[str] = mapped_column(String(255), default="")
    prior_move: Mapped[str] = mapped_column(Text, default="")
    prior_answer: Mapped[str] = mapped_column(Text, default="")
    applied_objection_id: Mapped[int] = mapped_column(Integer, default=0)
    review_note: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TranscriptChunk(Base):
    """One embedded passage in the Ask-the-library semantic index.

    Sources are style transcripts, media STT, founder quotes, and Drive file
    bundles. Objections and AMA Q&A candidates are never indexed here.
    """

    __tablename__ = "transcript_chunks"
    __table_args__ = (
        UniqueConstraint(
            "source_type",
            "source_id",
            "chunk_index",
            name="uq_transcript_chunk_source_index",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(16), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_name: Mapped[str] = mapped_column(String(500), default="")
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    embedding_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AskAnswerCache(Base):
    """Cached Ask-the-library responses, keyed by question + index fingerprint."""

    __tablename__ = "ask_answer_cache"
    __table_args__ = (
        UniqueConstraint(
            "question_hash",
            "index_fingerprint",
            name="uq_ask_answer_question_index",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_hash: Mapped[str] = mapped_column(String(64), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    index_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    response_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
