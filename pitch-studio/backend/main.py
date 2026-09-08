from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from backend.config import PITCH_STUDIO_ROOT, settings
from backend.routers import admin_routes, auth_routes, generation_routes

FRONTEND_DIST = PITCH_STUDIO_ROOT / "frontend" / "dist"

app = FastAPI(title="Pitch Studio")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_routes.router)
app.include_router(admin_routes.router)
app.include_router(generation_routes.router)


@app.exception_handler(OperationalError)
def database_unavailable(_request: Request, exc: OperationalError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "Database is temporarily unreachable"},
    )


@app.on_event("startup")
def on_startup() -> None:
    # Schema/bootstrap is done once at deploy. Doing it on every reload blocks
    # the API behind DigitalOcean Postgres and leaves /api/auth/me hanging.
    return


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
