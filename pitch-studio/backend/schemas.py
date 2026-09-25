from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


class AxisOption(BaseModel):
    code: str
    label: str
    description: str


AUDIENCE_CLUSTERS: list[AxisOption] = [
    AxisOption(code="A", label="Demand", description="People who might enrol, and the people who decide alongside them"),
    AxisOption(code="B", label="Talent", description="People who might work here or teach here"),
    AxisOption(code="C", label="Capital", description="People who might fund the institution or its funds"),
    AxisOption(code="D", label="Commercial", description="People who might hire from, buy from, or partner with us"),
    AxisOption(code="E", label="Institutional", description="People who legitimise, regulate, accredit or rank us"),
    AxisOption(code="F", label="Public", description="People who carry the story onward — press, creators, alumni, social"),
]

DURATIONS: list[AxisOption] = [
    AxisOption(code="T0", label="30 seconds", description="The lift. One sentence plus one proof. Every employee must have this."),
    AxisOption(code="T1", label="2 minutes", description="The standard answer. The single highest-volume script in the company."),
    AxisOption(code="T2", label="5 minutes", description="The coffee conversation. Room for one full proof block."),
    AxisOption(code="T3", label="10 minutes", description="The seated pitch. Proof plus mechanism plus objection."),
    AxisOption(code="T4", label="30 minutes", description="Deck-led session. Assets carry structure; script carries voice."),
    AxisOption(code="T5", label="90 minutes", description="Campus walkthrough. The building is the asset; script is a route plus stops."),
]

CHANNELS: list[AxisOption] = [
    AxisOption(code="CH1", label="Voice only", description="Phone call. No visuals. Hardest constraint — everything must be sayable."),
    AxisOption(code="CH2", label="In person, no assets", description="Corridor, event floor, dinner table."),
    AxisOption(code="CH3", label="With deck", description="In person or video call, screen or printed deck."),
    AxisOption(code="CH4", label="Campus walkthrough", description="Physical space does the proving. Script is a route, not a monologue."),
    AxisOption(code="CH5", label="Written", description="Email, WhatsApp, LinkedIn. Read not heard — different rhythm, shorter sentences."),
    AxisOption(code="CH6", label="Stage", description="Podium, panel, convocation, conference. One-to-many, no dialogue."),
    AxisOption(code="CH7", label="Camera", description="Recorded or broadcast. Every word is permanent and quotable."),
]

INTENTS: list[AxisOption] = [
    AxisOption(code="I1", label="Inform", description="They do not know what this is."),
    AxisOption(code="I2", label="Persuade", description="They know what it is and are deciding."),
    AxisOption(code="I3", label="Defend", description="They have an objection, spoken or unspoken."),
    AxisOption(code="I4", label="Recruit", description="We want them to join — as student, employee or faculty."),
    AxisOption(code="I5", label="Transact", description="We want them to buy, partner, hire or fund."),
    AxisOption(code="I6", label="Represent", description="Formal or institutional. We are on the record."),
]

TEMPERATURES: list[AxisOption] = [
    AxisOption(code="X1", label="Cold", description="Never heard of us."),
    AxisOption(code="X2", label="Warm", description="Heard of us. Curious, possibly sceptical."),
    AxisOption(code="X3", label="Hot", description="Actively evaluating. Comparing against named alternatives."),
    AxisOption(code="X4", label="Closing", description="Ready, but has one blocker left."),
    AxisOption(code="X5", label="Post-decision", description="Already in. Now needs to be able to tell the story themselves."),
]


class AxesResponse(BaseModel):
    audience_clusters: list[AxisOption]
    durations: list[AxisOption]
    channels: list[AxisOption]
    intents: list[AxisOption]
    temperatures: list[AxisOption]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleLoginRequest(BaseModel):
    credential: str


class AuthConfigOut(BaseModel):
    google_client_id: str
    google_allowed_domain: str = ""


class UserOut(BaseModel):
    id: int
    email: str
    is_admin: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    is_admin: bool = False


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    password: str | None = None
    is_admin: bool | None = None


class ModuleIn(BaseModel):
    id: str | None = None
    name: str
    job: str = ""
    core_content: str = ""
    flex_points: str = ""
    sources: str = ""
    owner: str = ""
    sort_order: int = 0


class ModuleOut(ModuleIn):
    id: str
    edited: bool = False

    model_config = {"from_attributes": True}


class FactIn(BaseModel):
    fact: str
    value: str = ""
    source: str = ""
    status: str = "needs_source"
    note: str = ""
    module_ids: str = ""


class FactOut(FactIn):
    id: int
    edited: bool = False

    model_config = {"from_attributes": True}


class RecipeIn(BaseModel):
    ref: str
    audience_label: str = ""
    audience_cluster: str
    duration: str
    channel: str
    intent: str
    temperature: str | None = None
    module_sequence: str
    word_budget: int = 0
    priority: str = "P2"
    owner: str = ""


class RecipeOut(RecipeIn):
    id: int
    edited: bool = False

    model_config = {"from_attributes": True}


class AssetIn(BaseModel):
    type: str
    title: str
    url: str | None = None
    source_url: str = ""
    file_key: str = ""
    content_type: str = ""
    matrix_ref: str = ""
    file_status: str = "pending"
    sync_error: str = ""
    module_ids: str = ""
    audiences: str = ""
    status: str = "exists"
    notes: str = ""


class AssetOut(AssetIn):
    id: int
    edited: bool = False
    synced_at: datetime | None = None
    extract_status: str = ""
    extract_error: str = ""

    model_config = {"from_attributes": True}


class ReportPassageOut(BaseModel):
    asset_id: int
    title: str = ""
    module_ids: str = ""
    start_page: int | None = None
    end_page: int | None = None
    text: str = ""


class ObjectionIn(BaseModel):
    question: str
    who_asks: str = ""
    move: str = ""
    answer: str = ""
    status: str = "approved"
    source_name: str = ""
    source_transcript_id: int = 0
    source_candidate_id: int = 0


class ObjectionOut(ObjectionIn):
    id: int
    edited: bool = False

    model_config = {"from_attributes": True}


class QaExtractionRunOut(BaseModel):
    id: int
    style_transcript_id: int
    transcript_hash: str = ""
    status: str = "queued"
    stage: str = ""
    error: str = ""
    candidate_count: int = 0
    job_id: int = 0
    created_at: datetime | None = None
    finished_at: datetime | None = None
    transcript_name: str = ""

    model_config = {"from_attributes": True}


class QaCandidateOut(BaseModel):
    id: int
    run_id: int
    style_transcript_id: int
    fingerprint: str = ""
    match_type: str = "new"
    matched_objection_id: int = 0
    confidence: float = 0.0
    status: str = "pending"
    question_verbatim: str = ""
    answer_verbatim: str = ""
    proposed_question: str = ""
    proposed_who_asks: str = ""
    proposed_move: str = ""
    proposed_answer: str = ""
    evidence_json: str = ""
    prior_question: str = ""
    prior_who_asks: str = ""
    prior_move: str = ""
    prior_answer: str = ""
    applied_objection_id: int = 0
    review_note: str = ""
    error: str = ""
    created_at: datetime | None = None
    reviewed_at: datetime | None = None
    transcript_name: str = ""

    model_config = {"from_attributes": True}


class QaCandidateApproveIn(BaseModel):
    question: str | None = None
    who_asks: str | None = None
    move: str | None = None
    answer: str | None = None
    review_note: str = ""


class QaCandidateRejectIn(BaseModel):
    review_note: str = ""


class FounderQuoteIn(BaseModel):
    text: str
    verbatim: bool = False
    topic: str = "founder"
    module_ids: str = ""
    audiences: str = ""
    source_name: str = ""
    source_file_id: str = ""
    source_url: str = ""
    start_sec: float = 0.0
    end_sec: float = 0.0
    speaker: str = "Pratham Mittal"
    status: str = "approved"


class FounderQuoteOut(FounderQuoteIn):
    id: int
    text_hash: str = ""
    edited: bool = False
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class RecipeOption(BaseModel):
    ref: str
    audience_label: str
    audience_cluster: str
    duration: str
    channel: str
    intent: str
    module_sequence: str
    priority: str = "P2"
    word_budget: int = 0
    valid: bool = True


class GenerationCreate(BaseModel):
    audience_cluster: str = ""
    duration: str = ""
    channel: str = ""
    intent: str = ""
    temperature: str
    context_note: str = ""
    recipe_ref: str = ""


class InterpretRequest(BaseModel):
    audience_text: str = ""
    setting_text: str = ""
    goal_text: str = ""


class InterpretPersonaCandidate(BaseModel):
    recipe_ref: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class InterpretResult(BaseModel):
    audience_cluster: str
    duration: str
    channel: str
    intent: str
    temperature: str
    recipe_ref: str = ""
    persona_candidates: list[InterpretPersonaCandidate] = Field(default_factory=list)
    summary: str = ""
    notes: str = ""


class ScriptSection(BaseModel):
    module_id: str = ""
    topic_id: int = 0
    topic_title: str = ""
    pages: list[int] = []
    slide_keys: list[str] = []
    heading: str = ""
    text: str = ""


class ScriptPayload(BaseModel):
    sections: list[ScriptSection]
    cta: str = ""


class GenerationOut(BaseModel):
    id: int
    user_id: int
    user_email: str | None = None
    audience_cluster: str
    duration: str
    channel: str
    intent: str
    temperature: str
    context_note: str
    recipe_ref: str
    module_sequence: str
    status: str
    script_json: str
    deck_spec_json: str
    pptx_path: str
    asset_ids: str
    objection_ids: str
    founder_quote_ids: str = ""
    report_asset_ids: str = ""
    report_passages_json: str = ""
    validation_report: str
    error: str
    cache_key: str = ""
    cached_from_id: int | None = None
    created_at: datetime
    script: dict[str, Any] | None = None
    deck_spec: dict[str, Any] | None = None
    assets: list[AssetOut] = []
    objections: list[ObjectionOut] = []
    founder_quotes: list[FounderQuoteOut] = []
    report_passages: list[ReportPassageOut] = []
    recommended_videos: list[RecommendedMediaOut] = []
    recommended_pictures: list[RecommendedMediaOut] = []

    model_config = {"from_attributes": True}


class ScriptTestCreate(BaseModel):
    audience_cluster: str = ""
    duration: str = ""
    channel: str = ""
    intent: str = ""
    temperature: str
    context_note: str = ""
    recipe_ref: str = ""


class ScriptTestSentenceOut(BaseModel):
    id: str
    index: int
    text: str


class ScriptTestParagraphOut(BaseModel):
    id: str
    index: int
    text: str
    sentences: list[ScriptTestSentenceOut] = Field(default_factory=list)


class ScriptTestSectionOut(BaseModel):
    index: int
    module_id: str = ""
    topic_id: int = 0
    topic_title: str = ""
    heading: str = ""
    paragraphs: list[ScriptTestParagraphOut] = Field(default_factory=list)


class ScriptTestReviewDocument(BaseModel):
    sections: list[ScriptTestSectionOut] = Field(default_factory=list)
    cta: str = ""


class ScriptTestFeedbackCreate(BaseModel):
    target_kind: Literal["sentence", "paragraph"]
    target_id: str
    comment: str = Field(min_length=1)

    @field_validator("comment")
    @classmethod
    def trim_comment(cls, value: str) -> str:
        trimmed = (value or "").strip()
        if not trimmed:
            raise ValueError("comment must not be empty")
        return trimmed

    @field_validator("target_id")
    @classmethod
    def trim_target_id(cls, value: str) -> str:
        trimmed = (value or "").strip()
        if not trimmed:
            raise ValueError("target_id must not be empty")
        return trimmed


class ScriptTestFeedbackUpdate(BaseModel):
    comment: str = Field(min_length=1)

    @field_validator("comment")
    @classmethod
    def trim_comment(cls, value: str) -> str:
        trimmed = (value or "").strip()
        if not trimmed:
            raise ValueError("comment must not be empty")
        return trimmed


class ScriptTestFeedbackOut(BaseModel):
    id: int
    script_test_run_id: int
    reviewer_user_id: int
    reviewer_email: str = ""
    target_kind: str
    target_id: str
    section_index: int
    paragraph_index: int
    sentence_index: int | None = None
    reference_text: str
    comment: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ScriptTestRatingUpsert(BaseModel):
    rating: float = Field(ge=0, le=10)


class ScriptTestRatingOut(BaseModel):
    id: int
    script_test_run_id: int
    reviewer_user_id: int
    reviewer_email: str = ""
    rating: float
    created_at: datetime
    updated_at: datetime
    average_rating: float | None = None
    rating_count: int = 0

    model_config = {"from_attributes": True}


class ScriptTestRunListItem(BaseModel):
    id: int
    created_by_user_id: int
    created_by_email: str = ""
    audience_cluster: str
    duration: str
    channel: str
    intent: str
    temperature: str
    context_note: str = ""
    recipe_ref: str
    module_sequence: str = ""
    status: str
    validation_report: str = ""
    error: str = ""
    created_at: datetime
    finished_at: datetime | None = None
    average_rating: float | None = None
    rating_count: int = 0


class ScriptTestRunOut(BaseModel):
    id: int
    created_by_user_id: int
    created_by_email: str = ""
    audience_cluster: str
    duration: str
    channel: str
    intent: str
    temperature: str
    context_note: str
    recipe_ref: str
    module_sequence: str
    status: str
    script_json: str = ""
    review_document_json: str = ""
    validation_report: str
    error: str
    founder_quote_ids: str = ""
    report_asset_ids: str = ""
    report_passages_json: str = ""
    created_at: datetime
    finished_at: datetime | None = None
    script: dict[str, Any] | None = None
    review_document: ScriptTestReviewDocument | None = None
    feedback: list[ScriptTestFeedbackOut] = Field(default_factory=list)
    ratings: list[ScriptTestRatingOut] = Field(default_factory=list)
    average_rating: float | None = None
    rating_count: int = 0

    model_config = {"from_attributes": True}


class SeedCounts(BaseModel):
    modules: int
    facts: int
    recipes: int
    objections: int
    assets: int


class SyncCounts(BaseModel):
    stored: int = 0
    external: int = 0
    gap: int = 0
    error: int = 0
    skipped: int = 0


class TranscriptIngestCounts(BaseModel):
    files: int = 0
    chunks: int = 0
    kept: int = 0
    skipped: int = 0
    dropped: int = 0


class StyleTranscriptUploadIn(BaseModel):
    name: str
    text: str
    source_url: str = ""


class StyleTranscriptIndexIn(BaseModel):
    transcript_ids: list[int] = Field(min_length=1)
    persona_labels: list[str] = Field(min_length=1)


class StyleTranscriptIndexOut(BaseModel):
    transcript_ids: list[int] = Field(default_factory=list)
    persona_labels: list[str] = Field(default_factory=list)
    guides_updated: list[str] = Field(default_factory=list)
    quotes_kept: int = 0
    quotes_skipped: int = 0


class StyleTranscriptPersonasIn(BaseModel):
    persona_labels: list[str] = Field(min_length=1)


class StyleTranscriptOut(BaseModel):
    id: int
    name: str
    status: str
    created_at: datetime | None = None
    text_length: int = 0
    source_url: str = ""
    persona_labels: list[str] = Field(default_factory=list)


class StyleTranscriptIngestOut(BaseModel):
    guide_version: int
    quotes_kept: int
    quotes_skipped: int
    transcript_id: int
    persona_labels: list[str] = Field(default_factory=list)
    guides_updated: list[str] = Field(default_factory=list)


class StyleGuideIn(BaseModel):
    guide_text: str
    persona_label: str


class StyleGuideOut(BaseModel):
    version: int = 0
    guide_text: str = ""
    persona_label: str = ""
    source_transcript_ids: str = ""


class ExtractResultOut(BaseModel):
    asset_id: int
    title: str
    extract_status: str
    page_count: int = 0
    ocr_pages: int = 0
    char_count: int = 0
    chunk_count: int = 0
    extract_error: str = ""


class TranscriptIn(BaseModel):
    transcript: str


class VisionIn(BaseModel):
    vision: str


class FeedbackIn(BaseModel):
    recipe_ref: str
    verdict: str
    note: str = ""


class AddUsecaseIn(BaseModel):
    recipe_ref: str
    note: str = ""


class MediaIndexCreate(BaseModel):
    asset_id: int


class MediaImageOut(BaseModel):
    id: int
    title: str
    file_status: str = ""
    content_type: str = ""
    recommended: bool = False
    excluded: bool = False


class ExcludeImageIn(BaseModel):
    asset_id: int
    excluded: bool = True


class RecommendImageIn(BaseModel):
    asset_id: int
    recommended: bool = True


class RecommendedMediaOut(BaseModel):
    asset_id: int
    parent_asset_id: int = 0
    media_kind: str
    title: str
    source_url: str = ""
    thumbnail_url: str = ""
    preview_url: str = ""
    confidence: float = 0.0
    rationale: str = ""


class VideoClickIn(BaseModel):
    asset_id: int
    displayed_rank: int = 0
    interaction_type: str = "play"


class VideoClickOut(BaseModel):
    id: int
    generation_id: int
    asset_id: int
    recipe_ref: str = ""
    displayed_rank: int = 0
    click_order: int = 0
    interaction_type: str = "play"
    created_at: datetime | None = None
    created: bool = False


class MediaRecommendationItem(BaseModel):
    recipe_ref: str
    audience_cluster: str = ""
    duration: str = ""
    channel: str = ""
    intent: str = ""
    temperatures: list[str] = []
    confidence: float = 0.0
    rationale: str = ""


class MediaRecommendations(BaseModel):
    generated_at: str = ""
    vision_hash: str = ""
    items: list[MediaRecommendationItem] = []


class MediaVerdict(BaseModel):
    verdict: str
    note: str = ""
    by: str = ""
    at: str = ""


class MediaAddedUsecase(BaseModel):
    recipe_ref: str
    note: str = ""
    at: str = ""


class MediaFeedback(BaseModel):
    verdicts: dict[str, MediaVerdict] = {}
    added: list[MediaAddedUsecase] = []
    recommended_image_ids: list[int] = []
    excluded_image_ids: list[int] = []


class MediaIndexOut(BaseModel):
    id: int
    asset_id: int
    media_kind: str
    transcript: str = ""
    visual_description: str = ""
    image_keys: list[str] = []
    vision: str = ""
    vision_hash: str = ""
    indexed_vision_hash: str = ""
    recommended_at: datetime | None = None
    vision_frozen: bool = False
    status: str = "draft"
    stale: bool = False
    job_status: str = ""
    job_stage: str = ""
    job_error: str = ""
    job_type: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    asset_title: str = ""
    asset_source_url: str = ""
    asset_file_status: str = ""
    asset_content_type: str = ""
    context_index: str = ""
    recommendations: MediaRecommendations = MediaRecommendations()
    feedback: MediaFeedback = MediaFeedback()
    image_assets: list[MediaImageOut] = []


class MediaCandidate(BaseModel):
    asset_id: int
    title: str
    type: str
    source_url: str = ""
    file_status: str = ""


class MediaIndexListOut(BaseModel):
    items: list[MediaIndexOut]
    candidates: list[MediaCandidate]


class DeckTopicPageOut(BaseModel):
    page: int
    label: str = ""
    module_id: str = ""
    image_url: str = ""


class DeckTopicOut(BaseModel):
    id: int
    sort_order: int = 0
    title: str = ""
    pages: list[int] = []
    page_items: list[DeckTopicPageOut] = []
    summary: str = ""
    module_ids: str = ""
    vision: str = ""
    vision_hash: str = ""
    indexed_vision_hash: str = ""
    recommended_at: datetime | None = None
    vision_frozen: bool = False
    status: str = "draft"
    stale: bool = False
    source_hash: str = ""
    job_status: str = ""
    job_stage: str = ""
    job_error: str = ""
    job_type: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    context_index: str = ""
    recommendations: MediaRecommendations = MediaRecommendations()
    feedback: MediaFeedback = MediaFeedback()


class DeckTopicListOut(BaseModel):
    items: list[DeckTopicOut]
    asset_id: int = 0
    asset_title: str = ""
    job_status: str = ""
    job_stage: str = ""
    job_error: str = ""
    job_type: str = ""


class GeneratedSlideUpdate(BaseModel):
    # Text slots are strings; a bounded list slot (programme list) is an array of
    # short strings, so a value may be either.
    slot_values: dict[str, str | list[str]]


class GeneratedSlideOut(BaseModel):
    id: int
    slide_key: str
    claim_hash: str
    render_hash: str
    template_id: str
    template_version: str
    tone: str
    slot_values: dict[str, str | list[str]] = {}
    image_url: str
    status: str
    edited_by_human: bool
    source_fact_ids: str = ""
    source_asset_ids: str = ""
    created_at: datetime
    updated_at: datetime


class GeneratedSlideAttemptOut(BaseModel):
    id: int
    generation_id: int
    generated_slide_id: int | None = None
    placeholder_key: str
    attempt_number: int
    claim: str
    template_id: str
    tone: str
    outcome: str
    gate: str
    violations: list[str] = Field(default_factory=list)
    slot_values: dict[str, Any] = Field(default_factory=dict)
    render_hash: str
    image_url: str | None = None
    review_status: str
    review_note: str
    use_as_guidance: bool
    reviewer_user_id: int | None = None
    reviewed_at: datetime | None = None
    created_at: datetime


class GeneratedSlideAttemptReview(BaseModel):
    review_status: Literal["reviewed", "approved", "rejected"]
    review_note: str = ""
    use_as_guidance: bool = False

    model_config = {"extra": "forbid"}

    @field_validator("review_note")
    @classmethod
    def trim_review_note(cls, value: str) -> str:
        return (value or "").strip()
