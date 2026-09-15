from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped[User] = relationship(back_populates="generations")


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
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    start_sec: Mapped[float] = mapped_column(Float, default=0.0)
    end_sec: Mapped[float] = mapped_column(Float, default=0.0)
    speaker: Mapped[str] = mapped_column(String(128), default="Pratham Mittal")
    text_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="approved")
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
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


class StyleTranscript(Base):
    __tablename__ = "style_transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    raw_text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="processed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class VoiceStyleGuide(Base):
    __tablename__ = "voice_style_guides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    guide_text: Mapped[str] = mapped_column(Text)
    source_transcript_ids: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
