from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.auth import hash_password, require_admin
from backend.config import DEFAULT_XLSX
from backend.database import get_db
from backend.deck_topic_index import (
    DeckTopicError,
    apply_recommendations as apply_deck_recommendations,
    enqueue_deck_prepare,
    find_brand_deck_asset,
    has_extract as deck_has_extract,
    is_stale as deck_is_stale,
    latest_deck_job,
    list_deck_topics,
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
    sha256_text,
    touch,
)
from backend.models import Asset, DeckTopic, FounderQuote, LockedFact, MediaIndex, Module, Objection, Recipe, User, utc_now
from backend.pipeline.llm import LLMError
from backend.recipe_cache import invalidate_recipe_cache, list_recipe_options
from backend.schemas import (
    AddUsecaseIn,
    AssetIn,
    AssetOut,
    DeckTopicListOut,
    DeckTopicOut,
    ExtractResultOut,
    FactIn,
    FactOut,
    FeedbackIn,
    FounderQuoteIn,
    FounderQuoteOut,
    MediaIndexCreate,
    MediaIndexListOut,
    MediaIndexOut,
    ModuleIn,
    ModuleOut,
    ObjectionIn,
    ObjectionOut,
    RecipeIn,
    RecipeOut,
    SeedCounts,
    SyncCounts,
    TranscriptIn,
    TranscriptIngestCounts,
    UserCreate,
    UserOut,
    UserUpdate,
    VisionIn,
)
from backend.seed import run_seed
from backend.sync_assets import run_sync, sync_one
from backend.transcripts import ingest_transcripts, quote_hash

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _apply(instance: Any, payload: dict[str, Any], mark_edited: bool = True) -> None:
    for key, value in payload.items():
        if value is not None:
            setattr(instance, key, value)
    if mark_edited and hasattr(instance, "edited"):
        instance.edited = True


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
    db.commit()
    db.refresh(item)
    return item


@router.delete("/facts/{fact_id}")
def delete_fact(fact_id: int, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(LockedFact, fact_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


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
    if item.vision.strip() and has_extract(item):
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
    if not item.vision.strip():
        raise HTTPException(status_code=400, detail="Enter a vision note before re-indexing")
    if item.media_kind == "video" and not item.transcript.strip():
        raise HTTPException(status_code=400, detail="Save a transcript before re-indexing")
    if item.media_kind == "photo" and not item.visual_description.strip():
        raise HTTPException(status_code=400, detail="Generate a visual description before re-indexing")
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


@router.post("/media-index/{media_id}/freeze", response_model=MediaIndexOut)
def freeze_media_index(media_id: int, db: Session = Depends(get_db)) -> MediaIndexOut:
    item = _get_media_index(db, media_id)
    if not item.vision.strip():
        raise HTTPException(status_code=400, detail="Enter a vision note before freezing")
    if not item.recommendations_json:
        raise HTTPException(status_code=400, detail="Re-index before freezing")
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


def _save_deck_feedback(db: Session, item: DeckTopic, payload: dict[str, Any]) -> DeckTopicOut:
    item.feedback_json = json.dumps(payload, ensure_ascii=False)
    touch_deck(item)
    db.commit()
    db.refresh(item)
    asset = find_brand_deck_asset(db)
    return serialize_deck_topic(item, latest_deck_job(db, asset.id))


@router.get("/deck-topics", response_model=DeckTopicListOut)
def list_brand_deck_topics(db: Session = Depends(get_db)) -> DeckTopicListOut:
    return list_deck_topics(db)


@router.post("/deck-topics/prepare", response_model=DeckTopicListOut)
def prepare_brand_deck_topics(
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> DeckTopicListOut:
    try:
        asset = find_brand_deck_asset(db)
        if force:
            for row in db.query(DeckTopic).all():
                row.source_hash = ""
            db.commit()
        enqueue_deck_prepare(db, asset.id)
    except DeckTopicError as exc:
        raise _deck_error(exc) from exc
    return list_deck_topics(db)


@router.get("/deck-topics/{topic_id}", response_model=DeckTopicOut)
def get_brand_deck_topic(topic_id: int, db: Session = Depends(get_db)) -> DeckTopicOut:
    item = _get_deck_topic(db, topic_id)
    try:
        asset = find_brand_deck_asset(db)
        job = latest_deck_job(db, asset.id)
    except DeckTopicError:
        job = None
    return serialize_deck_topic(item, job)


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
    if item.vision.strip() and deck_has_extract(item):
        try:
            apply_deck_recommendations(db, item)
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    else:
        touch_deck(item)
    db.commit()
    db.refresh(item)
    asset = find_brand_deck_asset(db)
    return serialize_deck_topic(item, latest_deck_job(db, asset.id))


@router.post("/deck-topics/{topic_id}/reindex", response_model=DeckTopicOut)
def reindex_deck_topic(topic_id: int, db: Session = Depends(get_db)) -> DeckTopicOut:
    item = _get_deck_topic(db, topic_id)
    try:
        require_deck_editable(item)
    except DeckTopicError as exc:
        raise _deck_error(exc) from exc
    if not item.vision.strip():
        raise HTTPException(status_code=400, detail="Enter a vision note before re-indexing")
    if not item.summary.strip():
        raise HTTPException(status_code=400, detail="Prepare the Brand Deck topics before re-indexing")
    try:
        apply_deck_recommendations(db, item)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.commit()
    db.refresh(item)
    asset = find_brand_deck_asset(db)
    return serialize_deck_topic(item, latest_deck_job(db, asset.id))


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
    if not item.vision.strip():
        raise HTTPException(status_code=400, detail="Enter a vision note before freezing")
    if not item.recommendations_json:
        raise HTTPException(status_code=400, detail="Re-index before freezing")
    if deck_is_stale(item):
        raise HTTPException(status_code=400, detail="Vision changed. Re-index before freezing")
    item.vision_frozen = True
    item.status = "frozen"
    touch_deck(item)
    db.commit()
    db.refresh(item)
    asset = find_brand_deck_asset(db)
    return serialize_deck_topic(item, latest_deck_job(db, asset.id))


@router.post("/deck-topics/{topic_id}/unfreeze", response_model=DeckTopicOut)
def unfreeze_deck_topic(topic_id: int, db: Session = Depends(get_db)) -> DeckTopicOut:
    item = _get_deck_topic(db, topic_id)
    item.vision_frozen = False
    item.status = "indexed" if item.recommendations_json else "ready"
    touch_deck(item)
    db.commit()
    db.refresh(item)
    asset = find_brand_deck_asset(db)
    return serialize_deck_topic(item, latest_deck_job(db, asset.id))
