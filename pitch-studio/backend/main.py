from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.auth import bootstrap_admin
from backend.config import DEFAULT_XLSX, PITCH_STUDIO_ROOT
from backend.database import Base, SessionLocal, engine, ensure_schema
from backend.models import Module
from backend.routers import admin_routes, auth_routes, generation_routes
from backend.seed import run_seed

FRONTEND_DIST = PITCH_STUDIO_ROOT / "frontend" / "dist"

app = FastAPI(title="Pitch Studio")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_routes.router)
app.include_router(admin_routes.router)
app.include_router(generation_routes.router)


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    db = SessionLocal()
    try:
        bootstrap_admin(db)
        if db.query(Module).count() == 0 and DEFAULT_XLSX.exists():
            run_seed(DEFAULT_XLSX, db=db)
    finally:
        db.close()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
