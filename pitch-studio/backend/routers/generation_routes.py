from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session, load_only

from backend.auth import get_current_user
from backend.config import settings
from backend.database import get_db
from backend.media_index import pick_recommended_media, record_video_click
from backend.models import Asset, FounderQuote, Generation, Objection, Recipe, User
from backend.pipeline.brand_deck import (
    SOURCE_PAGE_COUNT,
    BrandDeckUnavailable,
    brand_deck_file_key,
    page_image,
)
from backend.pipeline.interpret import InterpretError, interpret_brief
from backend.pipeline.resolver import is_valid_sequence, parse_sequence
from backend.pipeline.runner import run as run_generation
from backend.recipe_cache import list_recipe_options
from backend.schemas import (
    AUDIENCE_CLUSTERS,
    CHANNELS,
    DURATIONS,
    INTENTS,
    TEMPERATURES,
    AssetOut,
    AxesResponse,
    FounderQuoteOut,
    GenerationCreate,
    GenerationOut,
    InterpretRequest,
    InterpretResult,
    ObjectionOut,
    RecipeOption,
    ReportPassageOut,
    VideoClickIn,
    VideoClickOut,
)
from backend.storage import file_exists, get_url, read_file
from backend.thumbnails import ensure_thumbnail, thumbnail_key

router = APIRouter(prefix="/api", tags=["generations"], dependencies=[Depends(get_current_user)])


def _parse_ids(raw: str) -> list[int]:
    ids = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def _enrich(db: Session, generation: Generation) -> GenerationOut:
    payload = GenerationOut.model_validate(generation)
    if generation.script_json:
        try:
            payload.script = json.loads(generation.script_json)
        except json.JSONDecodeError:
            payload.script = None
    if generation.deck_spec_json:
        try:
            payload.deck_spec = json.loads(generation.deck_spec_json)
        except json.JSONDecodeError:
            payload.deck_spec = None
    asset_ids = _parse_ids(generation.asset_ids)
    objection_ids = _parse_ids(generation.objection_ids)
    quote_ids = _parse_ids(generation.founder_quote_ids)
    if asset_ids:
        assets = db.query(Asset).filter(Asset.id.in_(asset_ids)).all()
        by_id = {asset.id: asset for asset in assets}
        payload.assets = [AssetOut.model_validate(by_id[aid]) for aid in asset_ids if aid in by_id]
    if objection_ids:
        objections = db.query(Objection).filter(Objection.id.in_(objection_ids)).all()
        by_id = {item.id: item for item in objections}
        payload.objections = [
            ObjectionOut.model_validate(by_id[oid]) for oid in objection_ids if oid in by_id
        ]
    if quote_ids:
        quotes = db.query(FounderQuote).filter(FounderQuote.id.in_(quote_ids)).all()
        by_id = {item.id: item for item in quotes}
        payload.founder_quotes = [
            FounderQuoteOut.model_validate(by_id[qid]) for qid in quote_ids if qid in by_id
        ]
    if generation.report_passages_json:
        try:
            payload.report_passages = [
                ReportPassageOut.model_validate(item)
                for item in json.loads(generation.report_passages_json)
            ]
        except (json.JSONDecodeError, ValueError):
            payload.report_passages = []
    videos, pictures = pick_recommended_media(
        db,
        generation.recipe_ref,
        generation.temperature,
        duration=generation.duration,
    )
    payload.recommended_videos = videos
    payload.recommended_pictures = pictures
    return payload


def _list_item(generation: Generation) -> GenerationOut:
    return GenerationOut(
        id=generation.id,
        user_id=generation.user_id,
        audience_cluster=generation.audience_cluster,
        duration=generation.duration,
        channel=generation.channel,
        intent=generation.intent,
        temperature=generation.temperature,
        context_note=generation.context_note,
        recipe_ref=generation.recipe_ref,
        module_sequence=generation.module_sequence,
        status=generation.status,
        script_json="",
        deck_spec_json="",
        pptx_path=generation.pptx_path,
        asset_ids=generation.asset_ids,
        objection_ids=generation.objection_ids,
        founder_quote_ids=generation.founder_quote_ids,
        report_asset_ids=generation.report_asset_ids,
        report_passages_json="",
        validation_report=generation.validation_report,
        error=generation.error,
        created_at=generation.created_at,
    )


@router.get("/axes", response_model=AxesResponse)
def axes() -> AxesResponse:
    return AxesResponse(
        audience_clusters=AUDIENCE_CLUSTERS,
        durations=DURATIONS,
        channels=CHANNELS,
        intents=INTENTS,
        temperatures=TEMPERATURES,
    )


@router.get("/recipes", response_model=list[RecipeOption])
def recipe_options(db: Session = Depends(get_db)) -> list[RecipeOption]:
    return list_recipe_options(db)


@router.post("/generations/interpret", response_model=InterpretResult)
def interpret_generation(
    payload: InterpretRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InterpretResult:
    _ = user
    try:
        return interpret_brief(db, payload)
    except InterpretError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc) or "Could not interpret brief",
        ) from exc


@router.post("/generations", response_model=GenerationOut)
def create_generation(
    payload: GenerationCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GenerationOut:
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
        # Keep a matched persona for its module sequence, but do not overwrite axes
        # the user (or interpreter) already set — especially duration/channel.
        axes = {
            "audience_cluster": axes["audience_cluster"] or recipe.audience_cluster,
            "duration": axes["duration"] or recipe.duration,
            "channel": axes["channel"] or recipe.channel,
            "intent": axes["intent"] or recipe.intent,
        }
    generation = Generation(
        user_id=user.id,
        **axes,
        temperature=payload.temperature,
        context_note=payload.context_note,
        recipe_ref=recipe_ref,
        status="queued",
    )
    db.add(generation)
    db.commit()
    db.refresh(generation)
    background.add_task(run_generation, generation.id)
    return _enrich(db, generation)


@router.get("/generations", response_model=list[GenerationOut])
def list_generations(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[GenerationOut]:
    query = db.query(Generation).order_by(Generation.created_at.desc())
    if not user.is_admin:
        query = query.filter(Generation.user_id == user.id)
    rows = query.options(
        load_only(
            Generation.id,
            Generation.user_id,
            Generation.audience_cluster,
            Generation.duration,
            Generation.channel,
            Generation.intent,
            Generation.temperature,
            Generation.context_note,
            Generation.recipe_ref,
            Generation.module_sequence,
            Generation.status,
            Generation.pptx_path,
            Generation.asset_ids,
            Generation.objection_ids,
            Generation.founder_quote_ids,
            Generation.report_asset_ids,
            Generation.validation_report,
            Generation.error,
            Generation.created_at,
        )
    ).all()
    return [_list_item(item) for item in rows]


@router.get("/generations/{generation_id}", response_model=GenerationOut)
def get_generation(
    generation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GenerationOut:
    generation = db.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    if not user.is_admin and generation.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not allowed")
    return _enrich(db, generation)


@router.post("/generations/{generation_id}/video-clicks", response_model=VideoClickOut)
def create_video_click(
    generation_id: int,
    payload: VideoClickIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> VideoClickOut:
    generation = db.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    if not user.is_admin and generation.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not allowed")
    asset = db.get(Asset, payload.asset_id)
    if asset is None or asset.type != "video":
        raise HTTPException(status_code=404, detail="Video asset not found")
    source = (asset.source_url or asset.url or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="Video has no source URL")
    try:
        event, created = record_video_click(
            db,
            generation_id=generation.id,
            user_id=user.id,
            asset_id=asset.id,
            recipe_ref=generation.recipe_ref,
            displayed_rank=payload.displayed_rank,
            interaction_type=payload.interaction_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return VideoClickOut(
        id=event.id,
        generation_id=event.generation_id,
        asset_id=event.asset_id,
        recipe_ref=event.recipe_ref,
        displayed_rank=event.displayed_rank,
        click_order=event.click_order,
        interaction_type=event.interaction_type,
        created_at=event.created_at,
        created=created,
    )


@router.get("/assets/{asset_id}/file")
def download_asset_file(
    asset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _ = user
    asset = db.get(Asset, asset_id)
    if asset is None or not asset.file_key:
        raise HTTPException(status_code=404, detail="File not found")
    filename = Path(asset.file_key).name
    if settings.uses_spaces:
        url = get_url(asset.file_key)
        if url:
            return RedirectResponse(url)
    data = read_file(asset.file_key)
    media_type = asset.content_type or "application/octet-stream"
    disposition = "inline" if media_type.startswith(("video/", "image/")) else "attachment"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.get("/assets/{asset_id}/thumbnail.jpg")
def download_asset_thumbnail(
    asset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _ = user
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="File not found")
    if not (asset.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="Thumbnails apply to images")

    key = thumbnail_key(asset)
    if not file_exists(key) and not asset.file_key:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    try:
        if not file_exists(key):
            key = ensure_thumbnail(asset)
        # Stream bytes through the API. Redirecting to a Spaces signed URL often
        # fails in <img> tags even when the object is readable server-side.
        data = read_file(key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not create thumbnail") from exc

    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/brand-deck/pages/{page}.jpg")
def brand_deck_page(
    page: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Serve one brand deck page as an image, for the in-app slide preview."""
    _ = user
    if page < 1 or page > SOURCE_PAGE_COUNT:
        raise HTTPException(status_code=404, detail="Page not found")
    try:
        data = page_image(page, brand_deck_file_key(db))
    except BrandDeckUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/generations/{generation_id}/deck.pptx")
def download_deck(
    generation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    generation = db.get(Generation, generation_id)
    if generation is None or not generation.pptx_path:
        raise HTTPException(status_code=404, detail="Deck not found")
    if not user.is_admin and generation.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not allowed")
    if settings.uses_spaces:
        url = get_url(generation.pptx_path)
        if url:
            return RedirectResponse(url)
    data = read_file(generation.pptx_path)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="pitch-{generation_id}.pptx"'},
    )
