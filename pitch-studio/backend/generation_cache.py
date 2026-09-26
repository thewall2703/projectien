from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.config import PITCH_STUDIO_ROOT, settings
from backend.models import AppState

logger = logging.getLogger(__name__)

CONTENT_VERSION_KEY = "content_version"
BACKEND_ROOT = PITCH_STUDIO_ROOT / "backend"
SCRIPT_PIPELINE_VERSION = "11"

_deploy_version_cache: str | None = None


def normalize_context_note(note: str) -> str:
    return re.sub(r"\s+", " ", (note or "").strip()).lower()


def _hash_backend_tree() -> str:
    digest = hashlib.sha256()
    root = BACKEND_ROOT.resolve()
    paths: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        paths.append(path)
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def deploy_version() -> str:
    global _deploy_version_cache
    override = (settings.deploy_version or "").strip()
    if override:
        return override
    if _deploy_version_cache is None:
        _deploy_version_cache = _hash_backend_tree()
    return _deploy_version_cache


def reset_deploy_version_cache() -> None:
    """Test helper — clear the memoized deploy fingerprint."""
    global _deploy_version_cache
    _deploy_version_cache = None


def get_content_version(db: Session) -> int:
    row = db.get(AppState, CONTENT_VERSION_KEY)
    if row is None or not (row.value or "").strip():
        return 0
    try:
        return int(row.value)
    except ValueError:
        return 0


def bump_content_version(_db: Session | None = None) -> int:
    """Increment content_version using a fresh session. Never raises into callers.

    Uses an atomic upsert so concurrent bumps cannot lose increments via
    read-modify-write races. The unused ``_db`` arg is accepted so call sites
    can pass a session without changing their caller's transaction.
    """
    from sqlalchemy import text

    from backend.database import SessionLocal

    session = SessionLocal()
    try:
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            session.execute(
                text(
                    """
                    INSERT INTO app_state (key, value) VALUES (:key, '1')
                    ON CONFLICT(key) DO UPDATE SET
                      value = CAST(COALESCE(CAST(value AS INTEGER), 0) + 1 AS TEXT)
                    """
                ),
                {"key": CONTENT_VERSION_KEY},
            )
        else:
            session.execute(
                text(
                    """
                    INSERT INTO app_state (key, value) VALUES (:key, '1')
                    ON CONFLICT (key) DO UPDATE SET
                      value = (
                        CASE
                          WHEN app_state.value ~ '^[0-9]+$'
                          THEN app_state.value::integer
                          ELSE 0
                        END + 1
                      )::text
                    """
                ),
                {"key": CONTENT_VERSION_KEY},
            )
        session.commit()
        row = session.get(AppState, CONTENT_VERSION_KEY)
        if row is None or not (row.value or "").strip():
            return 0
        try:
            return int(row.value)
        except ValueError:
            return 0
    except Exception:
        logger.exception("Failed to bump content_version")
        try:
            session.rollback()
        except Exception:
            pass
        return 0
    finally:
        session.close()


def compute_cache_key(
    axes: dict[str, Any],
    temperature: str,
    context_note: str,
    recipe_ref: str,
    db: Session,
    *,
    deck_use_case: str = "",
) -> str:
    payload = {
        "axes": {
            "audience_cluster": axes.get("audience_cluster") or "",
            "duration": axes.get("duration") or "",
            "channel": axes.get("channel") or "",
            "intent": axes.get("intent") or "",
        },
        "temperature": temperature or "",
        "context_note": normalize_context_note(context_note),
        "recipe_ref": recipe_ref or "",
        "deck_use_case": deck_use_case or "",
        "openrouter_model": settings.openrouter_model,
        "openrouter_slide_model": settings.openrouter_slide_model,
        "script_pipeline_version": SCRIPT_PIPELINE_VERSION,
        "script_planner_model": settings.script_planner_model,
        "script_planner_reasoning_effort": settings.script_planner_reasoning_effort,
        "script_planner_verbosity": settings.script_planner_verbosity,
        "script_writer_model": settings.script_writer_model,
        "script_writer_reasoning_effort": settings.script_writer_reasoning_effort,
        "script_writer_verbosity": settings.script_writer_verbosity,
        "voice_judge_model": settings.voice_judge_model,
        "flow_judge_model": settings.flow_judge_model,
        "listener_model": settings.listener_model,
        "script_plan_enabled": bool(settings.script_plan_enabled),
        "flow_check_enabled": bool(settings.flow_check_enabled),
        "listener_enabled": bool(settings.listener_enabled),
        "pratham_passages_enabled": bool(settings.pratham_passages_enabled),
        "pratham_passages_per_topic": int(settings.pratham_passages_per_topic),
        "pratham_passage_word_cap": int(settings.pratham_passage_word_cap),
        "transcript_stories_enabled": bool(settings.transcript_stories_enabled),
        "transcript_stories_per_topic": int(settings.transcript_stories_per_topic),
        "transcript_story_word_cap": int(settings.transcript_story_word_cap),
        "deploy_version": deploy_version(),
        "content_version": get_content_version(db),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()
