from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.auth import hash_password, require_admin
from backend.config import DEFAULT_XLSX
from backend.database import get_db
from backend.extract import extract_asset
from backend.recipe_cache import invalidate_recipe_cache
from backend.models import Asset, FounderQuote, LockedFact, Module, Objection, Recipe, User
from backend.schemas import (
    AssetIn,
    AssetOut,
    ExtractResultOut,
    FactIn,
    FactOut,
    FounderQuoteIn,
    FounderQuoteOut,
    ModuleIn,
    ModuleOut,
    ObjectionIn,
    ObjectionOut,
    RecipeIn,
    RecipeOut,
    SeedCounts,
    SyncCounts,
    TranscriptIngestCounts,
    UserCreate,
    UserOut,
    UserUpdate,
)
from backend.seed import run_seed
from backend.sync_assets import run_sync
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
