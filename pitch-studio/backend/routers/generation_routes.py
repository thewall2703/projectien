from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.config import settings
from backend.database import get_db
from backend.models import Asset, FounderQuote, Generation, Objection, User
from backend.pipeline.runner import run as run_generation
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
    ObjectionOut,
)
from backend.storage import get_url, read_file

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
    return payload


@router.get("/axes", response_model=AxesResponse)
def axes() -> AxesResponse:
    return AxesResponse(
        audience_clusters=AUDIENCE_CLUSTERS,
        durations=DURATIONS,
        channels=CHANNELS,
        intents=INTENTS,
        temperatures=TEMPERATURES,
    )


@router.post("/generations", response_model=GenerationOut)
def create_generation(
    payload: GenerationCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GenerationOut:
    generation = Generation(
        user_id=user.id,
        audience_cluster=payload.audience_cluster,
        duration=payload.duration,
        channel=payload.channel,
        intent=payload.intent,
        temperature=payload.temperature,
        context_note=payload.context_note,
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
    return [_enrich(db, item) for item in query.all()]


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
    return Response(
        content=data,
        media_type=asset.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
