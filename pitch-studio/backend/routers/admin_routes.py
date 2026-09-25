from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from backend.auth import hash_password, require_admin
from backend.config import DEFAULT_XLSX
from backend.database import get_db
from backend.generated_slides import compute_render_hash, save_generated_slide_image
from backend.deck_topic_index import (
    DeckTopicError,
    apply_recommendations as apply_deck_recommendations,
    enqueue_deck_prepare,
    find_deck_asset,
    has_extract as deck_has_extract,
    is_stale as deck_is_stale,
    latest_deck_job,
    list_deck_topics,
    normalize_deck,
    require_editable as require_deck_editable,
    serialize as serialize_deck_topic,
    touch as touch_deck,
)
from backend.extract import extract_asset
from backend.media_index import (
    JOB_DESCRIBE,
    JOB_PREPARE,
    MediaIndexError,
    VALID_VERDICTS,
    create_media_index,
    enqueue_job,
    get_or_create_media_index,
    is_stale,
    list_media_index,
    normalize_transcript,
    parse_feedback,
    apply_recommendations,
    has_extract,
    require_editable,
    serialize,
    set_image_excluded,
    set_image_recommended,
    sha256_text,
    touch,
)
from backend.models import (
    Asset,
    DeckTopic,
    FounderQuote,
    GeneratedSlide,
    GeneratedSlideAttempt,
    ListenerTurn,
    LockedFact,
    MediaIndex,
    Module,
    Objection,
    QaCandidate,
    QaExtractionRun,
    Recipe,
    StyleTranscript,
    User,
    utc_now,
)
from backend.pipeline.llm import LLMError
from backend.pipeline.slide_fill import (
    _prepare_photos,
    apply_deck_conventions,
    gate_budgets,
    gate_schema,
    make_asset_photo_fn,
)
from backend.pipeline.slide_render import FONT_BUNDLE_VERSION, SlideRenderError, render_slide
from backend.pipeline.slide_templates import template_spec
from backend.pipeline.style_guide import (
    DuplicateStyleTranscriptError,
    StyleTranscriptPersonaError,
    index_style_transcript,
    latest_style_guide_row,
    personas_for_transcripts,
    save_style_guide,
    store_style_transcript,
    update_style_transcript_personas,
)
from backend.qa_extraction import (
    QaExtractionError,
    approve_candidate,
    enqueue_qa_extraction,
    reject_candidate,
)
from backend.audience import (
    build_listener_profile,
    calibrate_simulator,
    extract_listener_turns,
    latest_listener_profile_row,
    list_listener_turns,
)
from backend.recipe_cache import invalidate_recipe_cache, list_recipe_options
from backend.schemas import (
    AddUsecaseIn,
    AssetIn,
    AssetOut,
    AudienceCalibrateOut,
    DeckTopicListOut,
    DeckTopicOut,
    ExcludeImageIn,
    RecommendImageIn,
    ExtractResultOut,
    FactIn,
    FactOut,
    FeedbackIn,
    FounderQuoteIn,
    FounderQuoteOut,
    GeneratedSlideAttemptOut,
    GeneratedSlideAttemptReview,
    GeneratedSlideOut,
    GeneratedSlideUpdate,
    ListenerExtractOut,
    ListenerProfileOut,
    ListenerTurnOut,
    MediaIndexCreate,
    MediaIndexListOut,
    MediaIndexOut,
    ModuleIn,
    ModuleOut,
    ObjectionIn,
    ObjectionOut,
    QaCandidateApproveIn,
    QaCandidateOut,
    QaCandidateRejectIn,
    QaExtractionRunOut,
    RecipeIn,
    RecipeOut,
    SeedCounts,
    StyleGuideIn,
    StyleGuideOut,
    StyleTranscriptIndexIn,
    StyleTranscriptIndexOut,
    StyleTranscriptOut,
    StyleTranscriptPersonasIn,
    StyleTranscriptUploadIn,
    SyncCounts,
    TranscriptIn,
    TranscriptIngestCounts,
    UserCreate,
    UserOut,
    UserUpdate,
    VisionIn,
)
from backend.seed import run_seed
from backend.storage import delete_file, read_file
from backend.sync_assets import run_sync, sync_one
from backend.transcripts import ingest_transcripts, quote_hash

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _apply(instance: Any, payload: dict[str, Any], mark_edited: bool = True) -> None:
    for key, value in payload.items():
        if value is not None:
            setattr(instance, key, value)
    if mark_edited and hasattr(instance, "edited"):
        instance.edited = True


def _generated_slide_out(item: GeneratedSlide) -> GeneratedSlideOut:
    try:
        slots = json.loads(item.slot_values_json or "{}")
    except json.JSONDecodeError:
        slots = {}
    if not isinstance(slots, dict):
        slots = {}

    def _coerce(value: Any) -> str | list[str]:
        # Preserve list slots (programme list) as arrays; everything else is a
        # display string.
        if isinstance(value, (list, tuple)):
            return [str(entry) for entry in value]
        return str(value)

    return GeneratedSlideOut(
        id=item.id,
        slide_key=item.slide_key,
        claim_hash=item.claim_hash,
        render_hash=item.render_hash,
        template_id=item.template_id,
        template_version=item.template_version,
        tone=item.tone,
        slot_values={str(key): _coerce(value) for key, value in slots.items()},
        image_url=f"/api/generated-slides/{item.slide_key}.jpg",
        status=item.status,
        edited_by_human=item.edited_by_human,
        source_fact_ids=item.source_fact_ids,
        source_asset_ids=item.source_asset_ids,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(entry) for entry in parsed]


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _generated_slide_attempt_out(item: GeneratedSlideAttempt) -> GeneratedSlideAttemptOut:
    return GeneratedSlideAttemptOut(
        id=item.id,
        generation_id=item.generation_id,
        generated_slide_id=item.generated_slide_id,
        placeholder_key=item.placeholder_key,
        attempt_number=item.attempt_number,
        claim=item.claim,
        template_id=item.template_id,
        tone=item.tone,
        outcome=item.outcome,
        gate=item.gate,
        violations=_json_list(item.violations_json),
        slot_values=_json_object(item.slot_values_json),
        render_hash=item.render_hash,
        image_url=(
            f"/api/admin/generated-slide-attempts/{item.id}/image"
            if item.file_key
            else None
        ),
        review_status=item.review_status,
        review_note=item.review_note,
        use_as_guidance=item.use_as_guidance,
        reviewer_user_id=item.reviewer_user_id,
        reviewed_at=item.reviewed_at,
        created_at=item.created_at,
    )


def _invalidate_fact_slides(db: Session, fact_id: int) -> None:
    """Invalidate cached slides that cite a changed locked fact.

    Machine output is removed from the cache immediately. Human-edited output
    remains previewable but is marked for review so their work is never silently
    overwritten.
    """
    token = str(fact_id)
    for slide in db.query(GeneratedSlide).all():
        cited = {part.strip() for part in (slide.source_fact_ids or "").split(",")}
        if token not in cited:
            continue
        slide.status = "review" if slide.edited_by_human else "invalidated"
        slide.updated_at = utc_now()


@router.get("/modules", response_model=list[ModuleOut])
def list_modules(db: Session = Depends(get_db)) -> list[Module]:
    return db.query(Module).order_by(Module.sort_order, Module.id).all()


@router.post("/modules", response_model=ModuleOut)
def create_module(payload: ModuleIn, db: Session = Depends(get_db)) -> Module:
    if not payload.id:
        raise HTTPException(status_code=400, detail="Module id is required")
    if db.get(Module, payload.id):
        raise HTTPException(status_code=409, detail="Module already exists")
    item = Module(**payload.model_dump(), edited=True)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/modules/{module_id}", response_model=ModuleOut)
def update_module(module_id: str, payload: ModuleIn, db: Session = Depends(get_db)) -> Module:
    item = db.get(Module, module_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Module not found")
    data = payload.model_dump(exclude={"id"})
    _apply(item, data)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/modules/{module_id}")
def delete_module(module_id: str, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(Module, module_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Module not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.get("/facts", response_model=list[FactOut])
def list_facts(db: Session = Depends(get_db)) -> list[LockedFact]:
    return db.query(LockedFact).order_by(LockedFact.id).all()


@router.post("/facts", response_model=FactOut)
def create_fact(payload: FactIn, db: Session = Depends(get_db)) -> LockedFact:
    item = LockedFact(**payload.model_dump(), edited=True)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/facts/{fact_id}", response_model=FactOut)
def update_fact(fact_id: int, payload: FactIn, db: Session = Depends(get_db)) -> LockedFact:
    item = db.get(LockedFact, fact_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    _apply(item, payload.model_dump())
    _invalidate_fact_slides(db, fact_id)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/facts/{fact_id}")
def delete_fact(fact_id: int, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(LockedFact, fact_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    _invalidate_fact_slides(db, fact_id)
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.get("/generated-slides", response_model=list[GeneratedSlideOut])
def list_generated_slides(db: Session = Depends(get_db)) -> list[GeneratedSlideOut]:
    rows = db.query(GeneratedSlide).order_by(GeneratedSlide.updated_at.desc()).all()
    return [_generated_slide_out(item) for item in rows]


@router.put("/generated-slides/{slide_id}", response_model=GeneratedSlideOut)
def update_generated_slide(
    slide_id: int,
    payload: GeneratedSlideUpdate,
    db: Session = Depends(get_db),
) -> GeneratedSlideOut:
    item = db.get(GeneratedSlide, slide_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Generated slide not found")
    try:
        spec = template_spec(item.template_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        stored = json.loads(item.slot_values_json or "{}")
    except json.JSONDecodeError:
        stored = {}

    # Text slots come from the payload (falling back to the stored copy);
    # bounded list slots (programme list) are preserved from what was stored, or
    # updated when the editor sends an array / newline-delimited block.
    slots = {
        name: str(payload.slot_values.get(name, stored.get(name)) or "").strip()
        for name in spec.fillable_slots
    }
    list_values: dict[str, Any] = {}
    for name in spec.fillable_lists:
        provided = payload.slot_values.get(name, stored.get(name))
        if isinstance(provided, str):
            provided = [line for line in provided.splitlines() if line.strip()]
        list_values[name] = [
            apply_deck_conventions(str(entry))
            for entry in (provided or [])
            if str(entry).strip()
        ]
    persisted_values = {**slots, **list_values}

    violations = gate_schema(spec, persisted_values) + gate_budgets(spec, persisted_values)
    if violations:
        raise HTTPException(status_code=400, detail=violations)

    # Re-embed the slide's original approved photo(s) from the assets it was
    # built from, so a copy edit keeps the same picture. A photo-required
    # template whose asset is gone must be regenerated from the pipeline.
    asset_ids = [int(x) for x in (item.source_asset_ids or "").split(",") if x.strip().isdigit()]
    photos_by_slot, photo_descriptors, _asset_ids = _prepare_photos(
        spec, None, make_asset_photo_fn(db, asset_ids)
    )
    missing_photos = [n for n in spec.required_photo_slots if n not in photos_by_slot]
    if missing_photos:
        raise HTTPException(
            status_code=400,
            detail=(
                "This slide's approved photo is no longer available; "
                "regenerate it from the pipeline rather than editing copy."
            ),
        )

    values = {**spec.fixed_values(), **persisted_values}
    try:
        rendered = render_slide(spec.manifest, values, photos=photos_by_slot or None)
    except SlideRenderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    render_hash = compute_render_hash(
        template_id=spec.template_id,
        template_version=spec.version,
        slot_values=persisted_values,
        font_bundle_version=FONT_BUNDLE_VERSION,
        photo={"photos": photo_descriptors} if photo_descriptors else None,
    )
    collision = (
        db.query(GeneratedSlide)
        .filter(
            GeneratedSlide.render_hash == render_hash,
            GeneratedSlide.id != item.id,
        )
        .first()
    )
    if collision is not None:
        raise HTTPException(
            status_code=409,
            detail="An identical generated slide already exists",
        )

    old_file_key = item.file_key
    item.file_key = save_generated_slide_image(render_hash, rendered.jpeg)
    item.render_hash = render_hash
    item.template_version = spec.version
    item.slot_values_json = json.dumps(persisted_values, ensure_ascii=False)
    item.edited_by_human = True
    item.status = "ready"
    item.updated_at = utc_now()
    db.commit()
    db.refresh(item)
    if old_file_key and old_file_key != item.file_key:
        try:
            delete_file(old_file_key)
        except Exception:
            pass
    return _generated_slide_out(item)


@router.delete("/generated-slides/{slide_id}")
def delete_generated_slide(
    slide_id: int,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    item = db.get(GeneratedSlide, slide_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Generated slide not found")
    file_key = item.file_key
    db.delete(item)
    db.commit()
    if file_key:
        try:
            delete_file(file_key)
        except Exception:
            pass
    return {"ok": True}


@router.get(
    "/generated-slide-attempts",
    response_model=list[GeneratedSlideAttemptOut],
)
def list_generated_slide_attempts(
    review_status: str | None = Query(None),
    outcome: str | None = Query(None),
    gate: str | None = Query(None),
    template_id: str | None = Query(None),
    generation_id: int | None = Query(None),
    placeholder_key: str | None = Query(None),
    use_as_guidance: bool | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[GeneratedSlideAttemptOut]:
    query = db.query(GeneratedSlideAttempt)
    if review_status:
        query = query.filter(GeneratedSlideAttempt.review_status == review_status)
    if outcome:
        query = query.filter(GeneratedSlideAttempt.outcome == outcome)
    if gate:
        query = query.filter(GeneratedSlideAttempt.gate == gate)
    if template_id:
        query = query.filter(GeneratedSlideAttempt.template_id == template_id)
    if generation_id is not None:
        query = query.filter(GeneratedSlideAttempt.generation_id == generation_id)
    if placeholder_key:
        query = query.filter(GeneratedSlideAttempt.placeholder_key == placeholder_key)
    if use_as_guidance is not None:
        query = query.filter(
            GeneratedSlideAttempt.use_as_guidance.is_(use_as_guidance)
        )
    rows = (
        query.order_by(
            GeneratedSlideAttempt.created_at.desc(),
            GeneratedSlideAttempt.id.desc(),
        )
        .limit(limit)
        .all()
    )
    return [_generated_slide_attempt_out(item) for item in rows]


@router.get("/generated-slide-attempts/{attempt_id}/image")
def generated_slide_attempt_image(
    attempt_id: int,
    db: Session = Depends(get_db),
) -> Response:
    item = db.get(GeneratedSlideAttempt, attempt_id)
    if item is None or not item.file_key:
        raise HTTPException(status_code=404, detail="Attempt image not found")
    try:
        data = read_file(item.file_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Attempt image not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not read attempt image") from exc
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.put(
    "/generated-slide-attempts/{attempt_id}/review",
    response_model=GeneratedSlideAttemptOut,
)
def review_generated_slide_attempt(
    attempt_id: int,
    payload: GeneratedSlideAttemptReview,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> GeneratedSlideAttemptOut:
    item = db.get(GeneratedSlideAttempt, attempt_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Generated slide attempt not found")
    if payload.use_as_guidance and not payload.review_note:
        raise HTTPException(
            status_code=400,
            detail="A review note is required when approving guidance",
        )
    if payload.use_as_guidance and payload.review_status != "approved":
        raise HTTPException(
            status_code=400,
            detail="Guidance must have approved review status",
        )
    item.review_status = payload.review_status
    item.review_note = payload.review_note
    item.use_as_guidance = payload.use_as_guidance
    item.reviewer_user_id = user.id
    item.reviewed_at = utc_now()
    db.commit()
    db.refresh(item)
    return _generated_slide_attempt_out(item)


@router.get("/recipes", response_model=list[RecipeOut])
def list_recipes(db: Session = Depends(get_db)) -> list[Recipe]:
    return db.query(Recipe).order_by(Recipe.ref).all()


@router.post("/recipes", response_model=RecipeOut)
def create_recipe(payload: RecipeIn, db: Session = Depends(get_db)) -> Recipe:
    if db.query(Recipe).filter(Recipe.ref == payload.ref).first():
        raise HTTPException(status_code=409, detail="Recipe ref already exists")
    item = Recipe(**payload.model_dump(), edited=True)
    db.add(item)
    db.commit()
    db.refresh(item)
    invalidate_recipe_cache()
    return item


@router.put("/recipes/{recipe_id}", response_model=RecipeOut)
def update_recipe(recipe_id: int, payload: RecipeIn, db: Session = Depends(get_db)) -> Recipe:
    item = db.get(Recipe, recipe_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    _apply(item, payload.model_dump())
    db.commit()
    db.refresh(item)
    invalidate_recipe_cache()
    return item


@router.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: int, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(Recipe, recipe_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    db.delete(item)
    db.commit()
    invalidate_recipe_cache()
    return {"ok": True}


@router.get("/assets", response_model=list[AssetOut])
def list_assets(db: Session = Depends(get_db)) -> list[Asset]:
    return db.query(Asset).order_by(Asset.type, Asset.title).all()


@router.post("/assets", response_model=AssetOut)
def create_asset(payload: AssetIn, db: Session = Depends(get_db)) -> Asset:
    item = Asset(**payload.model_dump(), edited=True)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/assets/{asset_id}", response_model=AssetOut)
def update_asset(asset_id: int, payload: AssetIn, db: Session = Depends(get_db)) -> Asset:
    item = db.get(Asset, asset_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    _apply(item, payload.model_dump())
    db.commit()
    db.refresh(item)
    return item


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: int, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(Asset, asset_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.get("/objections", response_model=list[ObjectionOut])
def list_objections(db: Session = Depends(get_db)) -> list[Objection]:
    return db.query(Objection).order_by(Objection.id).all()


@router.post("/objections", response_model=ObjectionOut)
def create_objection(payload: ObjectionIn, db: Session = Depends(get_db)) -> Objection:
    item = Objection(**payload.model_dump(), edited=True)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/objections/{objection_id}", response_model=ObjectionOut)
def update_objection(objection_id: int, payload: ObjectionIn, db: Session = Depends(get_db)) -> Objection:
    item = db.get(Objection, objection_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Objection not found")
    _apply(item, payload.model_dump())
    db.commit()
    db.refresh(item)
    return item


@router.delete("/objections/{objection_id}")
def delete_objection(objection_id: int, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(Objection, objection_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Objection not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return db.query(User).order_by(User.id).all()


@router.post("/users", response_model=UserOut)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    email = payload.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already exists")
    item = User(email=email, password_hash=hash_password(payload.password), is_admin=payload.is_admin)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, db: Session = Depends(get_db)) -> User:
    item = db.get(User, user_id)
    if item is None:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.email is not None:
        item.email = payload.email.lower().strip()
    if payload.is_admin is not None:
        item.is_admin = payload.is_admin
    if payload.password and len(payload.password) >= 6:
        item.password_hash = hash_password(payload.password)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(User, user_id)
    if item is None:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.post("/reseed", response_model=SeedCounts)
def reseed(db: Session = Depends(get_db)) -> SeedCounts:
    path = Path(DEFAULT_XLSX)
    counts = run_seed(path, db=db)
    invalidate_recipe_cache()
    return counts


@router.get("/founder-quotes", response_model=list[FounderQuoteOut])
def list_founder_quotes(db: Session = Depends(get_db)) -> list[FounderQuote]:
    return db.query(FounderQuote).order_by(FounderQuote.id.desc()).all()


@router.post("/founder-quotes", response_model=FounderQuoteOut)
def create_founder_quote(payload: FounderQuoteIn, db: Session = Depends(get_db)) -> FounderQuote:
    digest = quote_hash(payload.source_file_id, payload.text)
    if db.query(FounderQuote).filter(FounderQuote.text_hash == digest).first():
        raise HTTPException(status_code=409, detail="Quote already exists")
    item = FounderQuote(**payload.model_dump(), text_hash=digest, edited=True)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/founder-quotes/{quote_id}", response_model=FounderQuoteOut)
def update_founder_quote(
    quote_id: int,
    payload: FounderQuoteIn,
    db: Session = Depends(get_db),
) -> FounderQuote:
    item = db.get(FounderQuote, quote_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Founder quote not found")
    _apply(item, payload.model_dump())
    item.text_hash = quote_hash(item.source_file_id, item.text)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/founder-quotes/{quote_id}")
def delete_founder_quote(quote_id: int, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(FounderQuote, quote_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Founder quote not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.get("/style-transcripts", response_model=list[StyleTranscriptOut])
def list_style_transcripts(db: Session = Depends(get_db)) -> list[StyleTranscriptOut]:
    rows = db.query(StyleTranscript).order_by(StyleTranscript.id.desc()).all()
    personas = personas_for_transcripts(db, [row.id for row in rows])
    return [
        StyleTranscriptOut(
            id=row.id,
            name=row.name,
            status=row.status,
            created_at=row.created_at,
            text_length=len(row.raw_text or ""),
            source_url=row.source_url or "",
            persona_labels=personas.get(row.id, []),
        )
        for row in rows
    ]


@router.post("/style-transcripts", response_model=StyleTranscriptOut)
def create_style_transcript(
    payload: StyleTranscriptUploadIn,
    db: Session = Depends(get_db),
) -> StyleTranscriptOut:
    try:
        row = store_style_transcript(db, payload.name, payload.text, payload.source_url)
    except DuplicateStyleTranscriptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StyleTranscriptOut(
        id=row.id,
        name=row.name,
        status=row.status,
        created_at=row.created_at,
        text_length=len(row.raw_text or ""),
        source_url=row.source_url or "",
        persona_labels=[],
    )


@router.post("/style-transcripts/index", response_model=StyleTranscriptIndexOut)
def index_stored_style_transcripts(
    payload: StyleTranscriptIndexIn,
    db: Session = Depends(get_db),
) -> StyleTranscriptIndexOut:
    transcript_ids = list(dict.fromkeys(payload.transcript_ids))
    rows = db.query(StyleTranscript).filter(StyleTranscript.id.in_(transcript_ids)).all()
    by_id = {row.id: row for row in rows}
    missing = [transcript_id for transcript_id in transcript_ids if transcript_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"Transcript(s) not found: {', '.join(map(str, missing))}")
    already_indexed = [by_id[transcript_id].name for transcript_id in transcript_ids if by_id[transcript_id].status == "processed"]
    if already_indexed:
        raise HTTPException(
            status_code=409,
            detail="Already indexed: " + ", ".join(already_indexed),
        )

    guides_updated: list[str] = []
    quotes_kept = 0
    quotes_skipped = 0
    try:
        for transcript_id in transcript_ids:
            result = index_style_transcript(
                db,
                transcript_id,
                persona_labels=payload.persona_labels,
            )
            quotes_kept += int(result["quotes_kept"])
            quotes_skipped += int(result["quotes_skipped"])
            for label in result["guides_updated"]:
                if label not in guides_updated:
                    guides_updated.append(label)
    except StyleTranscriptPersonaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return StyleTranscriptIndexOut(
        transcript_ids=transcript_ids,
        persona_labels=payload.persona_labels,
        guides_updated=guides_updated,
        quotes_kept=quotes_kept,
        quotes_skipped=quotes_skipped,
    )


@router.put("/style-transcripts/{transcript_id}/personas", response_model=StyleTranscriptOut)
def set_style_transcript_personas(
    transcript_id: int,
    payload: StyleTranscriptPersonasIn,
    db: Session = Depends(get_db),
) -> StyleTranscriptOut:
    try:
        result = update_style_transcript_personas(
            db,
            transcript_id,
            persona_labels=payload.persona_labels,
        )
    except StyleTranscriptPersonaError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except LLMError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    row = db.get(StyleTranscript, transcript_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Style transcript not found")
    return StyleTranscriptOut(
        id=row.id,
        name=row.name,
        status=row.status,
        created_at=row.created_at,
        text_length=len(row.raw_text or ""),
        source_url=row.source_url or "",
        persona_labels=result["persona_labels"],
    )


@router.get("/style-guide", response_model=StyleGuideOut)
def get_style_guide(
    persona_label: str = Query(""),
    db: Session = Depends(get_db),
) -> StyleGuideOut:
    label = (persona_label or "").strip()
    if not label:
        return StyleGuideOut(version=0, guide_text="", persona_label="", source_transcript_ids="")
    row = latest_style_guide_row(db, persona_label=label)
    if row is None or not isinstance(getattr(row, "guide_text", None), str):
        return StyleGuideOut(version=0, guide_text="", persona_label=label, source_transcript_ids="")
    return StyleGuideOut(
        version=row.version,
        guide_text=row.guide_text,
        persona_label=row.persona_label or label,
        source_transcript_ids=row.source_transcript_ids or "",
    )


@router.put("/style-guide", response_model=StyleGuideOut)
def update_style_guide(payload: StyleGuideIn, db: Session = Depends(get_db)) -> StyleGuideOut:
    try:
        item = save_style_guide(db, payload.guide_text, payload.persona_label)
    except StyleTranscriptPersonaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StyleGuideOut(
        version=item.version,
        guide_text=item.guide_text,
        persona_label=item.persona_label or "",
        source_transcript_ids=item.source_transcript_ids or "",
    )


def _serialize_listener_turn(row: ListenerTurn, persona_labels: list[str] | None = None) -> ListenerTurnOut:
    return ListenerTurnOut(
        id=row.id,
        style_transcript_id=row.style_transcript_id,
        self_description=row.self_description or "",
        concern=row.concern or "",
        question_verbatim=row.question_verbatim or "",
        reaction_after_answer=row.reaction_after_answer or "",
        outcome=row.outcome or "unclear",
        answer_summary=row.answer_summary or "",
        confidence=float(row.confidence or 0.0),
        created_at=row.created_at,
        persona_labels=list(persona_labels or []),
    )


@router.post(
    "/style-transcripts/{transcript_id}/extract-listeners",
    response_model=ListenerExtractOut,
)
def extract_listeners_for_transcript(
    transcript_id: int,
    db: Session = Depends(get_db),
) -> ListenerExtractOut:
    transcript = db.get(StyleTranscript, transcript_id)
    if transcript is None:
        raise HTTPException(status_code=404, detail="Style transcript not found")
    try:
        turns = extract_listener_turns(db, transcript_id)
    except (LLMError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    persona_map = personas_for_transcripts(db, [transcript_id])
    labels = persona_map.get(transcript_id, [])
    return ListenerExtractOut(
        style_transcript_id=transcript_id,
        turn_count=len(turns),
        turns=[_serialize_listener_turn(row, labels) for row in turns],
    )


@router.get("/listener-turns", response_model=list[ListenerTurnOut])
def get_listener_turns(
    transcript_id: int | None = Query(None),
    persona_label: str = Query(""),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[ListenerTurnOut]:
    rows = list_listener_turns(
        db,
        style_transcript_id=transcript_id,
        persona_label=persona_label,
        limit=limit,
    )
    transcript_ids = sorted({row.style_transcript_id for row in rows})
    persona_map = personas_for_transcripts(db, transcript_ids)
    return [
        _serialize_listener_turn(row, persona_map.get(row.style_transcript_id, []))
        for row in rows
    ]


@router.get("/listener-profile", response_model=ListenerProfileOut)
def get_listener_profile(
    persona_label: str = Query(""),
    db: Session = Depends(get_db),
) -> ListenerProfileOut:
    label = (persona_label or "").strip()
    if not label:
        return ListenerProfileOut()
    row = latest_listener_profile_row(db, label)
    if row is None:
        return ListenerProfileOut(persona_label=label)
    return ListenerProfileOut(
        version=row.version,
        persona_label=row.persona_label or label,
        profile_text=row.profile_text or "",
        source_transcript_ids=row.source_transcript_ids or "",
    )


@router.post("/listener-profile/rebuild", response_model=ListenerProfileOut)
def rebuild_listener_profile(
    persona_label: str = Query(...),
    db: Session = Depends(get_db),
) -> ListenerProfileOut:
    label = (persona_label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="persona_label is required")
    try:
        row = build_listener_profile(db, label)
    except (LLMError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ListenerProfileOut(
        version=row.version,
        persona_label=row.persona_label or label,
        profile_text=row.profile_text or "",
        source_transcript_ids=row.source_transcript_ids or "",
    )


@router.post("/audience-sim/calibrate", response_model=AudienceCalibrateOut)
def run_audience_calibration(
    persona_label: str = Query(...),
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
) -> AudienceCalibrateOut:
    label = (persona_label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="persona_label is required")
    try:
        result = calibrate_simulator(db, label, limit=limit)
    except (LLMError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AudienceCalibrateOut(**result)


def _transcript_names(db: Session, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = db.query(StyleTranscript).filter(StyleTranscript.id.in_(ids)).all()
    return {row.id: row.name for row in rows}


def _serialize_run(run: QaExtractionRun, transcript_name: str = "") -> QaExtractionRunOut:
    return QaExtractionRunOut(
        id=run.id,
        style_transcript_id=run.style_transcript_id,
        transcript_hash=run.transcript_hash,
        status=run.status,
        stage=run.stage,
        error=run.error,
        candidate_count=run.candidate_count,
        job_id=run.job_id,
        created_at=run.created_at,
        finished_at=run.finished_at,
        transcript_name=transcript_name,
    )


def _serialize_candidate(candidate: QaCandidate, transcript_name: str = "") -> QaCandidateOut:
    return QaCandidateOut(
        id=candidate.id,
        run_id=candidate.run_id,
        style_transcript_id=candidate.style_transcript_id,
        fingerprint=candidate.fingerprint,
        match_type=candidate.match_type,
        matched_objection_id=candidate.matched_objection_id,
        confidence=candidate.confidence,
        status=candidate.status,
        question_verbatim=candidate.question_verbatim,
        answer_verbatim=candidate.answer_verbatim,
        proposed_question=candidate.proposed_question,
        proposed_who_asks=candidate.proposed_who_asks,
        proposed_move=candidate.proposed_move,
        proposed_answer=candidate.proposed_answer,
        evidence_json=candidate.evidence_json,
        prior_question=candidate.prior_question,
        prior_who_asks=candidate.prior_who_asks,
        prior_move=candidate.prior_move,
        prior_answer=candidate.prior_answer,
        applied_objection_id=candidate.applied_objection_id,
        review_note=candidate.review_note,
        error=candidate.error,
        created_at=candidate.created_at,
        reviewed_at=candidate.reviewed_at,
        transcript_name=transcript_name,
    )


@router.post("/style-transcripts/{transcript_id}/extract-qa", response_model=QaExtractionRunOut)
def start_qa_extraction(
    transcript_id: int,
    force: bool = Query(False),
    db: Session = Depends(get_db),
) -> QaExtractionRunOut:
    try:
        run, _job = enqueue_qa_extraction(db, transcript_id, force=force)
    except QaExtractionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    names = _transcript_names(db, {run.style_transcript_id})
    return _serialize_run(run, names.get(run.style_transcript_id, ""))


@router.get("/qa-extractions", response_model=list[QaExtractionRunOut])
def list_qa_extractions(
    transcript_id: int | None = Query(None),
    db: Session = Depends(get_db),
) -> list[QaExtractionRunOut]:
    query = db.query(QaExtractionRun)
    if transcript_id is not None:
        query = query.filter(QaExtractionRun.style_transcript_id == transcript_id)
    rows = query.order_by(QaExtractionRun.id.desc()).limit(100).all()
    names = _transcript_names(db, {row.style_transcript_id for row in rows})
    return [_serialize_run(row, names.get(row.style_transcript_id, "")) for row in rows]


@router.get("/qa-extractions/{run_id}", response_model=QaExtractionRunOut)
def get_qa_extraction(run_id: int, db: Session = Depends(get_db)) -> QaExtractionRunOut:
    run = db.get(QaExtractionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Extraction run not found")
    names = _transcript_names(db, {run.style_transcript_id})
    return _serialize_run(run, names.get(run.style_transcript_id, ""))


@router.get("/qa-candidates", response_model=list[QaCandidateOut])
def list_qa_candidates(
    status: str | None = Query(None),
    run_id: int | None = Query(None),
    transcript_id: int | None = Query(None),
    db: Session = Depends(get_db),
) -> list[QaCandidateOut]:
    query = db.query(QaCandidate)
    if status:
        query = query.filter(QaCandidate.status == status)
    if run_id is not None:
        query = query.filter(QaCandidate.run_id == run_id)
    if transcript_id is not None:
        query = query.filter(QaCandidate.style_transcript_id == transcript_id)
    rows = query.order_by(QaCandidate.id.desc()).limit(300).all()
    names = _transcript_names(db, {row.style_transcript_id for row in rows})
    return [_serialize_candidate(row, names.get(row.style_transcript_id, "")) for row in rows]


@router.get("/qa-candidates/{candidate_id}", response_model=QaCandidateOut)
def get_qa_candidate(candidate_id: int, db: Session = Depends(get_db)) -> QaCandidateOut:
    candidate = db.get(QaCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    names = _transcript_names(db, {candidate.style_transcript_id})
    return _serialize_candidate(candidate, names.get(candidate.style_transcript_id, ""))


@router.post("/qa-candidates/{candidate_id}/approve", response_model=QaCandidateOut)
def approve_qa_candidate(
    candidate_id: int,
    payload: QaCandidateApproveIn,
    db: Session = Depends(get_db),
) -> QaCandidateOut:
    try:
        candidate = approve_candidate(
            db,
            candidate_id,
            question=payload.question,
            who_asks=payload.who_asks,
            move=payload.move,
            answer=payload.answer,
            review_note=payload.review_note,
        )
    except QaExtractionError as exc:
        detail = str(exc)
        code = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=code, detail=detail) from exc
    names = _transcript_names(db, {candidate.style_transcript_id})
    return _serialize_candidate(candidate, names.get(candidate.style_transcript_id, ""))


@router.post("/qa-candidates/{candidate_id}/reject", response_model=QaCandidateOut)
def reject_qa_candidate(
    candidate_id: int,
    payload: QaCandidateRejectIn,
    db: Session = Depends(get_db),
) -> QaCandidateOut:
    try:
        candidate = reject_candidate(db, candidate_id, review_note=payload.review_note)
    except QaExtractionError as exc:
        detail = str(exc)
        code = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=code, detail=detail) from exc
    names = _transcript_names(db, {candidate.style_transcript_id})
    return _serialize_candidate(candidate, names.get(candidate.style_transcript_id, ""))


@router.post("/ingest-transcripts", response_model=TranscriptIngestCounts)
def ingest_founder_transcripts(
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> TranscriptIngestCounts:
    raw = ingest_transcripts(force=force, db=db)
    return TranscriptIngestCounts(
        files=raw.get("files", 0),
        chunks=raw.get("chunks", 0),
        kept=raw.get("kept", 0),
        skipped=raw.get("skipped", 0),
        dropped=raw.get("dropped", 0),
    )


@router.post("/sync-assets", response_model=SyncCounts)
def sync_library_assets(
    types: str = Query(default="report"),
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> SyncCounts:
    raw = run_sync(
        types=[part.strip() for part in types.split(",") if part.strip()],
        force=force,
        db=db,
    )
    return SyncCounts(
        stored=raw.get("stored", 0),
        external=raw.get("external", 0),
        gap=raw.get("gap", 0),
        error=raw.get("error", 0),
        skipped=raw.get("skipped", 0),
    )


@router.post("/extract-assets/{asset_id}", response_model=ExtractResultOut)
def extract_library_asset(
    asset_id: int,
    max_pages: int = Query(default=0),
    ocr: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> ExtractResultOut:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    extract_asset(db, asset, max_pages=max_pages or None, ocr=ocr)
    payload: dict[str, Any] = {}
    if asset.extract_json:
        try:
            payload = json.loads(asset.extract_json)
        except json.JSONDecodeError:
            payload = {}
    return ExtractResultOut(
        asset_id=asset.id,
        title=asset.title,
        extract_status=asset.extract_status,
        page_count=int(payload.get("page_count") or 0),
        ocr_pages=int(payload.get("ocr_pages") or 0),
        char_count=int(payload.get("char_count") or 0),
        chunk_count=len(payload.get("chunks") or []),
        extract_error=asset.extract_error,
    )


def _media_error(exc: MediaIndexError) -> HTTPException:
    message = str(exc)
    lowered = message.lower()
    if "not found" in lowered:
        return HTTPException(status_code=404, detail=message)
    if "already exists" in lowered or "frozen" in lowered:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=400, detail=message)


def _get_media_index(db: Session, media_id: int) -> MediaIndex:
    item = db.get(MediaIndex, media_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Media index not found")
    return item


def _save_feedback(db: Session, item: MediaIndex, payload: dict[str, Any]) -> MediaIndexOut:
    item.feedback_json = json.dumps(payload, ensure_ascii=False)
    touch(item)
    db.commit()
    db.refresh(item)
    return serialize(item, db)


@router.post("/assets/{asset_id}/sync", response_model=AssetOut)
def sync_library_asset(
    asset_id: int,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Asset:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    sync_one(db, asset, force=force)
    db.commit()
    db.refresh(asset)
    return asset


@router.get("/media-index", response_model=MediaIndexListOut)
def list_media_indexes(db: Session = Depends(get_db)) -> MediaIndexListOut:
    return list_media_index(db)


@router.post("/media-index/prepare", response_model=MediaIndexOut)
def prepare_media_index(payload: MediaIndexCreate, db: Session = Depends(get_db)) -> MediaIndexOut:
    try:
        item = get_or_create_media_index(db, payload.asset_id)
        require_editable(item)
        job = enqueue_job(db, JOB_PREPARE, item.id, payload.asset_id)
    except MediaIndexError as exc:
        raise _media_error(exc) from exc
    return serialize(item, db, job)


@router.post("/media-index", response_model=MediaIndexOut)
def create_media_index_row(payload: MediaIndexCreate, db: Session = Depends(get_db)) -> MediaIndexOut:
    try:
        item = create_media_index(db, payload.asset_id)
    except MediaIndexError as exc:
        raise _media_error(exc) from exc
    return serialize(item, db)


@router.get("/media-index/{media_id}", response_model=MediaIndexOut)
def get_media_index_row(media_id: int, db: Session = Depends(get_db)) -> MediaIndexOut:
    return serialize(_get_media_index(db, media_id), db)


@router.put("/media-index/{media_id}/transcript", response_model=MediaIndexOut)
def save_media_transcript(
    media_id: int,
    payload: TranscriptIn,
    db: Session = Depends(get_db),
) -> MediaIndexOut:
    item = _get_media_index(db, media_id)
    try:
        require_editable(item)
    except MediaIndexError as exc:
        raise _media_error(exc) from exc
    if item.media_kind != "video":
        raise HTTPException(status_code=400, detail="Transcripts apply to video assets")
    item.transcript = normalize_transcript(payload.transcript)
    touch(item)
    db.commit()
    db.refresh(item)
    try:
        from backend.models import Asset
        from backend.transcript_search import SOURCE_MEDIA, safe_upsert_source

        asset = db.get(Asset, item.asset_id)
        safe_upsert_source(
            db=db,
            source_type=SOURCE_MEDIA,
            source_id=str(item.id),
            source_name=(asset.title if asset else "") or f"Media {item.id}",
            text=item.transcript or "",
        )
    except Exception:
        pass
    return serialize(item, db)


@router.post("/media-index/{media_id}/describe", response_model=MediaIndexOut)
def describe_media_images(media_id: int, db: Session = Depends(get_db)) -> MediaIndexOut:
    item = _get_media_index(db, media_id)
    try:
        require_editable(item)
    except MediaIndexError as exc:
        raise _media_error(exc) from exc
    if item.media_kind != "photo":
        raise HTTPException(status_code=400, detail="Visual descriptions apply to photo sets")
    job = enqueue_job(db, JOB_DESCRIBE, item.id, item.asset_id)
    return serialize(item, db, job)


@router.put("/media-index/{media_id}/vision", response_model=MediaIndexOut)
def save_media_vision(
    media_id: int,
    payload: VisionIn,
    db: Session = Depends(get_db),
) -> MediaIndexOut:
    item = _get_media_index(db, media_id)
    try:
        require_editable(item)
    except MediaIndexError as exc:
        raise _media_error(exc) from exc
    item.vision = payload.vision
    item.vision_hash = sha256_text(payload.vision)
    if item.status == "frozen":
        item.status = "indexed"
    if has_extract(item):
        try:
            apply_recommendations(db, item)
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    else:
        touch(item)
    db.commit()
    db.refresh(item)
    return serialize(item, db)


@router.post("/media-index/{media_id}/reindex", response_model=MediaIndexOut)
def reindex_media(media_id: int, db: Session = Depends(get_db)) -> MediaIndexOut:
    item = _get_media_index(db, media_id)
    try:
        require_editable(item)
    except MediaIndexError as exc:
        raise _media_error(exc) from exc
    if not has_extract(item):
        raise HTTPException(status_code=400, detail="Prepare the media before re-indexing")
    try:
        apply_recommendations(db, item)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.commit()
    db.refresh(item)
    return serialize(item, db)


@router.post("/media-index/{media_id}/feedback", response_model=MediaIndexOut)
def save_media_feedback(
    media_id: int,
    payload: FeedbackIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> MediaIndexOut:
    item = _get_media_index(db, media_id)
    verdict = payload.verdict.strip().lower()
    if verdict not in VALID_VERDICTS:
        raise HTTPException(status_code=400, detail="Verdict must be yes or no")
    feedback = parse_feedback(item.feedback_json)
    feedback["verdicts"][payload.recipe_ref] = {
        "verdict": verdict,
        "note": payload.note,
        "by": user.email,
        "at": utc_now().isoformat(),
    }
    return _save_feedback(db, item, feedback)


@router.post("/media-index/{media_id}/add-usecase", response_model=MediaIndexOut)
def add_media_usecase(
    media_id: int,
    payload: AddUsecaseIn,
    db: Session = Depends(get_db),
) -> MediaIndexOut:
    item = _get_media_index(db, media_id)
    refs = {option.ref for option in list_recipe_options(db)}
    if payload.recipe_ref not in refs:
        raise HTTPException(status_code=400, detail="Unknown recipe_ref")
    feedback = parse_feedback(item.feedback_json)
    already = any(entry.get("recipe_ref") == payload.recipe_ref for entry in feedback["added"])
    if not already:
        feedback["added"].append(
            {
                "recipe_ref": payload.recipe_ref,
                "note": payload.note,
                "at": utc_now().isoformat(),
            }
        )
    return _save_feedback(db, item, feedback)


@router.post("/media-index/{media_id}/exclude-image", response_model=MediaIndexOut)
def exclude_media_image(
    media_id: int,
    payload: ExcludeImageIn,
    db: Session = Depends(get_db),
) -> MediaIndexOut:
    item = _get_media_index(db, media_id)
    try:
        feedback = set_image_excluded(db, item, payload.asset_id, payload.excluded)
    except MediaIndexError as exc:
        raise _media_error(exc) from exc
    return _save_feedback(db, item, feedback)


@router.post("/media-index/{media_id}/recommend-image", response_model=MediaIndexOut)
def recommend_media_image(
    media_id: int,
    payload: RecommendImageIn,
    db: Session = Depends(get_db),
) -> MediaIndexOut:
    item = _get_media_index(db, media_id)
    try:
        feedback = set_image_recommended(db, item, payload.asset_id, payload.recommended)
    except MediaIndexError as exc:
        raise _media_error(exc) from exc
    return _save_feedback(db, item, feedback)


@router.post("/media-index/{media_id}/freeze", response_model=MediaIndexOut)
def freeze_media_index(media_id: int, db: Session = Depends(get_db)) -> MediaIndexOut:
    item = _get_media_index(db, media_id)
    if not item.recommendations_json:
        raise HTTPException(status_code=400, detail="Generate recommendations before freezing")
    if is_stale(item):
        raise HTTPException(status_code=400, detail="Vision changed. Re-index before freezing")
    item.vision_frozen = True
    item.status = "frozen"
    touch(item)
    db.commit()
    db.refresh(item)
    return serialize(item, db)


@router.post("/media-index/{media_id}/unfreeze", response_model=MediaIndexOut)
def unfreeze_media_index(media_id: int, db: Session = Depends(get_db)) -> MediaIndexOut:
    item = _get_media_index(db, media_id)
    item.vision_frozen = False
    item.status = "indexed" if item.recommendations_json else "draft"
    touch(item)
    db.commit()
    db.refresh(item)
    return serialize(item, db)


def _deck_error(exc: DeckTopicError) -> HTTPException:
    message = str(exc)
    lowered = message.lower()
    if "not found" in lowered:
        return HTTPException(status_code=404, detail=message)
    if "frozen" in lowered:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=400, detail=message)


def _get_deck_topic(db: Session, topic_id: int) -> DeckTopic:
    item = db.get(DeckTopic, topic_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Deck topic not found")
    return item


def _serialize_topic(db: Session, item: DeckTopic) -> DeckTopicOut:
    try:
        asset = find_deck_asset(db, item.deck or "brand")
        job = latest_deck_job(db, asset.id)
    except DeckTopicError:
        job = None
    return serialize_deck_topic(item, job)


def _save_deck_feedback(db: Session, item: DeckTopic, payload: dict[str, Any]) -> DeckTopicOut:
    item.feedback_json = json.dumps(payload, ensure_ascii=False)
    touch_deck(item)
    db.commit()
    db.refresh(item)
    return _serialize_topic(db, item)


@router.get("/deck-topics", response_model=DeckTopicListOut)
def list_brand_deck_topics(
    deck: str = Query(default="brand"),
    db: Session = Depends(get_db),
) -> DeckTopicListOut:
    try:
        return list_deck_topics(db, normalize_deck(deck))
    except DeckTopicError as exc:
        raise _deck_error(exc) from exc


@router.post("/deck-topics/prepare", response_model=DeckTopicListOut)
def prepare_brand_deck_topics(
    force: bool = Query(default=False),
    deck: str = Query(default="brand"),
    db: Session = Depends(get_db),
) -> DeckTopicListOut:
    try:
        deck = normalize_deck(deck)
        asset = find_deck_asset(db, deck)
        if force:
            for row in db.query(DeckTopic).filter(DeckTopic.deck == deck).all():
                row.source_hash = ""
            db.commit()
        enqueue_deck_prepare(db, asset.id, force=force)
    except DeckTopicError as exc:
        raise _deck_error(exc) from exc
    return list_deck_topics(db, deck)


@router.get("/deck-topics/{topic_id}", response_model=DeckTopicOut)
def get_brand_deck_topic(topic_id: int, db: Session = Depends(get_db)) -> DeckTopicOut:
    return _serialize_topic(db, _get_deck_topic(db, topic_id))


@router.put("/deck-topics/{topic_id}/vision", response_model=DeckTopicOut)
def save_deck_topic_vision(
    topic_id: int,
    payload: VisionIn,
    db: Session = Depends(get_db),
) -> DeckTopicOut:
    item = _get_deck_topic(db, topic_id)
    try:
        require_deck_editable(item)
    except DeckTopicError as exc:
        raise _deck_error(exc) from exc
    item.vision = payload.vision
    item.vision_hash = sha256_text(payload.vision)
    if item.status == "frozen":
        item.status = "indexed"
    if deck_has_extract(item):
        try:
            apply_deck_recommendations(db, item)
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    else:
        touch_deck(item)
    db.commit()
    db.refresh(item)
    return _serialize_topic(db, item)


@router.post("/deck-topics/{topic_id}/reindex", response_model=DeckTopicOut)
def reindex_deck_topic(topic_id: int, db: Session = Depends(get_db)) -> DeckTopicOut:
    item = _get_deck_topic(db, topic_id)
    try:
        require_deck_editable(item)
    except DeckTopicError as exc:
        raise _deck_error(exc) from exc
    if not item.summary.strip():
        raise HTTPException(status_code=400, detail="Prepare the Brand Deck topics before re-indexing")
    try:
        apply_deck_recommendations(db, item)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.commit()
    db.refresh(item)
    return _serialize_topic(db, item)


@router.post("/deck-topics/{topic_id}/feedback", response_model=DeckTopicOut)
def save_deck_topic_feedback(
    topic_id: int,
    payload: FeedbackIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> DeckTopicOut:
    item = _get_deck_topic(db, topic_id)
    verdict = payload.verdict.strip().lower()
    if verdict not in VALID_VERDICTS:
        raise HTTPException(status_code=400, detail="Verdict must be yes or no")
    feedback = parse_feedback(item.feedback_json)
    feedback["verdicts"][payload.recipe_ref] = {
        "verdict": verdict,
        "note": payload.note,
        "by": user.email,
        "at": utc_now().isoformat(),
    }
    return _save_deck_feedback(db, item, feedback)


@router.post("/deck-topics/{topic_id}/add-usecase", response_model=DeckTopicOut)
def add_deck_topic_usecase(
    topic_id: int,
    payload: AddUsecaseIn,
    db: Session = Depends(get_db),
) -> DeckTopicOut:
    item = _get_deck_topic(db, topic_id)
    refs = {option.ref for option in list_recipe_options(db)}
    if payload.recipe_ref not in refs:
        raise HTTPException(status_code=400, detail="Unknown recipe_ref")
    feedback = parse_feedback(item.feedback_json)
    already = any(entry.get("recipe_ref") == payload.recipe_ref for entry in feedback["added"])
    if not already:
        feedback["added"].append(
            {
                "recipe_ref": payload.recipe_ref,
                "note": payload.note,
                "at": utc_now().isoformat(),
            }
        )
    return _save_deck_feedback(db, item, feedback)


@router.post("/deck-topics/{topic_id}/freeze", response_model=DeckTopicOut)
def freeze_deck_topic(topic_id: int, db: Session = Depends(get_db)) -> DeckTopicOut:
    item = _get_deck_topic(db, topic_id)
    if not item.recommendations_json:
        raise HTTPException(status_code=400, detail="Generate recommendations before freezing")
    if deck_is_stale(item):
        raise HTTPException(status_code=400, detail="Vision changed. Re-index before freezing")
    item.vision_frozen = True
    item.status = "frozen"
    touch_deck(item)
    db.commit()
    db.refresh(item)
    return _serialize_topic(db, item)


@router.post("/deck-topics/{topic_id}/unfreeze", response_model=DeckTopicOut)
def unfreeze_deck_topic(topic_id: int, db: Session = Depends(get_db)) -> DeckTopicOut:
    item = _get_deck_topic(db, topic_id)
    item.vision_frozen = False
    item.status = "indexed" if item.recommendations_json else "ready"
    touch_deck(item)
    db.commit()
    db.refresh(item)
    return _serialize_topic(db, item)
