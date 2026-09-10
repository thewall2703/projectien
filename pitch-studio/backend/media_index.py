from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from io import BytesIO
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from backend.database import ensure_schema
from backend.models import Asset, Job, MediaIndex, utc_now

_schema_ready = False


def _ensure_media_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    ensure_schema()
    _schema_ready = True
from backend.pipeline.llm import chat_json, chat_text_multimodal
from backend.recipe_cache import list_recipe_options
from backend.schemas import (
    AUDIENCE_CLUSTERS,
    CHANNELS,
    DURATIONS,
    INTENTS,
    TEMPERATURES,
    AxisOption,
    MediaAddedUsecase,
    MediaCandidate,
    MediaFeedback,
    MediaImageOut,
    MediaIndexListOut,
    MediaIndexOut,
    MediaRecommendationItem,
    MediaRecommendations,
    MediaVerdict,
    RecipeOption,
)
from backend.storage import read_file

CHILD_TITLE_SEP = " — "
MAX_IMAGES = 6
MAX_IMAGE_EDGE = 1024
MAX_RECOMMENDATIONS = 15
VALID_TEMPERATURES = {item.code for item in TEMPERATURES}
VALID_VERDICTS = {"yes", "no"}
JOB_PREPARE = "media_prepare"
JOB_DESCRIBE = "media_describe"
ACTIVE_JOB_STATUSES = ("queued", "running")
StageCallback = Callable[[str], None]

DESCRIBE_PROMPT = (
    "Describe what is happening across these images: setting, people, activity, "
    "mood, notable objects, any on-frame text. 120-200 words. Write plain prose, "
    "no bullet list, no preamble."
)


class MediaImagesUnavailable(RuntimeError):
    pass


class MediaIndexError(RuntimeError):
    pass


def sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def is_stale(row: MediaIndex) -> bool:
    if not row.indexed_vision_hash:
        return bool(row.vision and not row.recommendations_json)
    return row.vision_hash != row.indexed_vision_hash


def is_library_parent(asset: Asset) -> bool:
    return CHILD_TITLE_SEP not in (asset.title or "")


def normalize_transcript(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict) and payload.get("text"):
            return str(payload["text"])
    return text


def empty_feedback() -> dict[str, Any]:
    return {"verdicts": {}, "added": []}


def parse_feedback(raw: str) -> dict[str, Any]:
    if not raw:
        return empty_feedback()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return empty_feedback()
    if not isinstance(payload, dict):
        return empty_feedback()
    verdicts = payload.get("verdicts") or {}
    added = payload.get("added") or []
    if not isinstance(verdicts, dict):
        verdicts = {}
    if not isinstance(added, list):
        added = []
    return {"verdicts": verdicts, "added": added}


def parse_recommendations(raw: str) -> dict[str, Any]:
    if not raw:
        return {"generated_at": "", "vision_hash": "", "items": []}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"generated_at": "", "vision_hash": "", "items": []}
    if not isinstance(payload, dict):
        return {"generated_at": "", "vision_hash": "", "items": []}
    items = payload.get("items") or []
    if not isinstance(items, list):
        items = []
    return {
        "generated_at": str(payload.get("generated_at") or ""),
        "vision_hash": str(payload.get("vision_hash") or ""),
        "items": items,
    }


def parse_image_keys(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload if item]


def _downscale_jpeg(data: bytes) -> bytes:
    from PIL import Image

    image = Image.open(BytesIO(data))
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    out = BytesIO()
    image.save(out, format="JPEG", quality=82)
    return out.getvalue()


def list_image_assets(db: Session, asset: Asset) -> list[Asset]:
    if (asset.content_type or "").startswith("image/") and asset.file_status == "stored" and asset.file_key:
        return [asset]
    prefix = f"{asset.title}{CHILD_TITLE_SEP}"
    children = (
        db.query(Asset)
        .filter(Asset.type == asset.type, Asset.title.startswith(prefix))
        .order_by(Asset.title)
        .all()
    )
    return [
        child
        for child in children
        if child.file_status == "stored"
        and child.file_key
        and (child.content_type or "").startswith("image/")
    ]


def collect_image_bytes(
    db: Session,
    asset: Asset,
    max_images: int = MAX_IMAGES,
) -> tuple[list[bytes], list[str]]:
    images: list[bytes] = []
    keys: list[str] = []
    for child in list_image_assets(db, asset)[:max_images]:
        try:
            data = _downscale_jpeg(read_file(child.file_key))
        except Exception:
            continue
        images.append(data)
        keys.append(child.file_key)
    if not images:
        raise MediaImagesUnavailable(
            "No stored images found for this photo set. Sync the folder first."
        )
    return images, keys


def describe_images(images: list[bytes]) -> str:
    return chat_text_multimodal(DESCRIBE_PROMPT, images)


def _format_axes() -> str:
    groups: list[tuple[str, list[AxisOption]]] = [
        ("Audience clusters", AUDIENCE_CLUSTERS),
        ("Durations", DURATIONS),
        ("Channels", CHANNELS),
        ("Intents", INTENTS),
        ("Temperatures", TEMPERATURES),
    ]
    lines: list[str] = []
    for title, options in groups:
        lines.append(f"{title}:")
        for option in options:
            lines.append(f"- {option.code}: {option.label} — {option.description}")
    return "\n".join(lines)


def _format_personas(options: list[RecipeOption]) -> str:
    lines = []
    for option in options:
        lines.append(
            f"- {option.ref} | {option.audience_label} | "
            f"{option.audience_cluster}/{option.duration}/{option.channel}/{option.intent}"
        )
    return "\n".join(lines) or "(no personas)"


def _format_feedback(feedback: dict[str, Any]) -> str:
    verdicts = feedback.get("verdicts") or {}
    added = feedback.get("added") or []
    lines: list[str] = []
    for ref, payload in verdicts.items():
        if not isinstance(payload, dict):
            continue
        verdict = str(payload.get("verdict") or "").upper()
        note = str(payload.get("note") or "").strip()
        line = f"- Human marked {verdict} for {ref}"
        if note:
            line += f" ({note})"
        lines.append(line)
    for item in added:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("recipe_ref") or "")
        note = str(item.get("note") or "").strip()
        line = f"- Human says it ALSO applies to {ref} that you missed"
        if note:
            line += f" ({note})"
        lines.append(line)
    return "\n".join(lines) or "(none)"


def build_recommendation_messages(
    kind: str,
    transcript: str,
    visual_description: str,
    vision: str,
    recipe_options: list[RecipeOption],
    feedback: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    extracted = transcript.strip() if kind == "video" else visual_description.strip()
    extracted_label = "Transcript" if kind == "video" else "Visual description"
    system = (
        "You match Masters' Union media (a video or a photo set) to pitch personas. "
        "Combine the extracted content with the human vision note. "
        "Only recommend personas that should actually see this media. "
        "Return strict JSON: {\"items\":[{\"recipe_ref\":\"\",\"temperatures\":[],"
        "\"confidence\":0.0,\"rationale\":\"\"}]}. "
        f"Use only recipe_ref values from the catalog. Rank by confidence. Max {MAX_RECOMMENDATIONS} items. "
        "Temperatures must be X1-X5 codes. Rationale is one short sentence."
    )
    user = (
        f"Media kind: {kind}\n\n"
        f"Axes catalog:\n{_format_axes()}\n\n"
        f"Persona catalog:\n{_format_personas(recipe_options)}\n\n"
        f"{extracted_label}:\n{extracted or '(none)'}\n\n"
        f"Human vision:\n{vision.strip() or '(none)'}\n\n"
        f"Prior reviewer feedback:\n{_format_feedback(feedback or empty_feedback())}\n\n"
        "Recommend the personas this media should be shown for."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _normalize_items(payload: dict[str, Any], options: list[RecipeOption]) -> list[dict[str, Any]]:
    by_ref = {option.ref: option for option in options}
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("recipe_ref") or "").strip()
        recipe = by_ref.get(ref)
        if recipe is None or ref in seen:
            continue
        temps = item.get("temperatures") or []
        if not isinstance(temps, list):
            temps = []
        temperatures = [str(code) for code in temps if str(code) in VALID_TEMPERATURES]
        try:
            confidence = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        cleaned.append(
            {
                "recipe_ref": ref,
                "audience_cluster": recipe.audience_cluster,
                "duration": recipe.duration,
                "channel": recipe.channel,
                "intent": recipe.intent,
                "temperatures": temperatures,
                "confidence": confidence,
                "rationale": str(item.get("rationale") or "").strip(),
            }
        )
        seen.add(ref)
        if len(cleaned) >= MAX_RECOMMENDATIONS:
            break
    cleaned.sort(key=lambda item: item["confidence"], reverse=True)
    return cleaned


def recommend(db: Session, row: MediaIndex) -> dict[str, Any]:
    options = list_recipe_options(db)
    messages = build_recommendation_messages(
        row.media_kind,
        row.transcript,
        row.visual_description,
        row.vision,
        options,
        parse_feedback(row.feedback_json),
    )
    payload = chat_json(messages)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vision_hash": row.vision_hash,
        "items": _normalize_items(payload, options),
    }


def _image_outs(db: Session, asset: Asset | None) -> list[MediaImageOut]:
    if asset is None:
        return []
    return [
        MediaImageOut(
            id=child.id,
            title=child.title,
            file_status=child.file_status,
            content_type=child.content_type,
        )
        for child in list_image_assets(db, asset)
    ]


def serialize(row: MediaIndex, db: Session, job: Job | None = None) -> MediaIndexOut:
    asset = db.get(Asset, row.asset_id)
    rec = parse_recommendations(row.recommendations_json)
    fb = parse_feedback(row.feedback_json)
    if job is None:
        job = latest_job_for(db, row.id)
    return MediaIndexOut(
        id=row.id,
        asset_id=row.asset_id,
        media_kind=row.media_kind,
        transcript=row.transcript,
        visual_description=row.visual_description,
        image_keys=parse_image_keys(row.image_keys),
        vision=row.vision,
        vision_hash=row.vision_hash,
        indexed_vision_hash=row.indexed_vision_hash,
        recommended_at=row.recommended_at,
        vision_frozen=row.vision_frozen,
        status=derive_status(row, job),
        stale=is_stale(row),
        job_status=job.status if job else "",
        job_stage=job.stage if job else "",
        job_error=job.error if job else "",
        job_type=job.job_type if job else "",
        created_at=row.created_at,
        updated_at=row.updated_at,
        asset_title=asset.title if asset else "",
        asset_source_url=(asset.source_url or asset.url or "") if asset else "",
        asset_file_status=asset.file_status if asset else "",
        asset_content_type=asset.content_type if asset else "",
        context_index=build_context_index(row),
        recommendations=MediaRecommendations(
            generated_at=rec["generated_at"],
            vision_hash=rec["vision_hash"],
            items=[
                MediaRecommendationItem(**item)
                for item in rec["items"]
                if isinstance(item, dict) and item.get("recipe_ref")
            ],
        ),
        feedback=MediaFeedback(
            verdicts={
                ref: MediaVerdict(
                    verdict=str(payload.get("verdict") or ""),
                    note=str(payload.get("note") or ""),
                    by=str(payload.get("by") or ""),
                    at=str(payload.get("at") or ""),
                )
                for ref, payload in fb["verdicts"].items()
                if isinstance(payload, dict)
            },
            added=[
                MediaAddedUsecase(
                    recipe_ref=str(item.get("recipe_ref") or ""),
                    note=str(item.get("note") or ""),
                    at=str(item.get("at") or ""),
                )
                for item in fb["added"]
                if isinstance(item, dict)
            ],
        ),
        image_assets=_image_outs(db, asset),
    )


def list_media_index(db: Session) -> MediaIndexListOut:
    _ensure_media_schema()
    rows = db.query(MediaIndex).order_by(MediaIndex.id).all()
    indexed_ids = {row.asset_id for row in rows}
    assets = (
        db.query(Asset)
        .filter(Asset.type.in_(("video", "photo")))
        .order_by(Asset.type, Asset.title)
        .all()
    )
    candidates = [
        MediaCandidate(
            asset_id=asset.id,
            title=asset.title,
            type=asset.type,
            source_url=asset.source_url or asset.url or "",
            file_status=asset.file_status,
        )
        for asset in assets
        if asset.id not in indexed_ids and is_library_parent(asset)
    ]
    jobs = latest_jobs_for(db, [row.id for row in rows])
    return MediaIndexListOut(
        items=[serialize(row, db, jobs.get(row.id)) for row in rows],
        candidates=candidates,
    )


def create_media_index(db: Session, asset_id: int) -> MediaIndex:
    _ensure_media_schema()
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise MediaIndexError("Asset not found")
    if asset.type not in {"video", "photo"}:
        raise MediaIndexError("Only video and photo assets can be indexed")
    if not is_library_parent(asset):
        raise MediaIndexError("Index the photo set (folder), not an individual image")
    existing = db.query(MediaIndex).filter(MediaIndex.asset_id == asset_id).first()
    if existing is not None:
        raise MediaIndexError("Media index already exists for this asset")
    row = MediaIndex(asset_id=asset.id, media_kind=asset.type)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def touch(row: MediaIndex) -> None:
    row.updated_at = utc_now()


def require_editable(row: MediaIndex) -> None:
    if row.vision_frozen:
        raise MediaIndexError("Vision is frozen. Unfreeze it before editing.")


def build_context_index(row: MediaIndex) -> str:
    parts: list[str] = []
    extracted = row.transcript.strip() if row.media_kind == "video" else row.visual_description.strip()
    if extracted:
        label = "Transcript" if row.media_kind == "video" else "Visual description"
        parts.append(f"{label}:\n{extracted}")
    if (row.vision or "").strip():
        parts.append(f"Vision:\n{row.vision.strip()}")
    return "\n\n".join(parts)


def has_extract(row: MediaIndex) -> bool:
    if row.media_kind == "video":
        return bool(row.transcript.strip())
    return bool(row.visual_description.strip())


def derive_status(row: MediaIndex, job: Job | None = None) -> str:
    if job is not None and job.status in ACTIVE_JOB_STATUSES:
        return "processing"
    if row.status == "indexed":
        return "indexed"
    if has_extract(row):
        return "ready"
    return "draft"


def latest_job_for(db: Session, media_id: int) -> Job | None:
    if not media_id:
        return None
    _ensure_media_schema()
    return db.query(Job).filter(Job.media_id == media_id).order_by(Job.id.desc()).first()


def latest_jobs_for(db: Session, media_ids: list[int]) -> dict[int, Job]:
    if not media_ids:
        return {}
    _ensure_media_schema()
    jobs = (
        db.query(Job)
        .filter(Job.media_id.in_(media_ids))
        .order_by(Job.media_id, Job.id.desc())
        .all()
    )
    latest: dict[int, Job] = {}
    for job in jobs:
        if job.media_id not in latest:
            latest[job.media_id] = job
    return latest


def active_job_for(db: Session, media_id: int) -> Job | None:
    if not media_id:
        return None
    return (
        db.query(Job)
        .filter(Job.media_id == media_id, Job.status.in_(ACTIVE_JOB_STATUSES))
        .order_by(Job.id.desc())
        .first()
    )


def enqueue_job(db: Session, job_type: str, media_id: int, asset_id: int) -> Job:
    _ensure_media_schema()
    existing = active_job_for(db, media_id)
    if existing is not None:
        return existing
    job = Job(job_type=job_type, media_id=media_id, asset_id=asset_id, status="queued", stage="Queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _emit_stage(on_stage: StageCallback | None, text: str) -> None:
    if on_stage is not None:
        on_stage(text)


def apply_recommendations(db: Session, row: MediaIndex) -> None:
    payload = recommend(db, row)
    row.recommendations_json = json.dumps(payload, ensure_ascii=False)
    row.indexed_vision_hash = row.vision_hash
    row.recommended_at = utc_now()
    row.status = "indexed"
    touch(row)


def get_or_create_media_index(db: Session, asset_id: int) -> MediaIndex:
    _ensure_media_schema()
    existing = db.query(MediaIndex).filter(MediaIndex.asset_id == asset_id).first()
    if existing is not None:
        return existing
    return create_media_index(db, asset_id)


def prepare_media(db: Session, asset_id: int, on_stage: StageCallback | None = None) -> MediaIndex:
    from backend.sync_assets import classify_link, sync_one, sync_youtube
    from backend.thumbnails import ensure_thumbnails

    row = get_or_create_media_index(db, asset_id)
    require_editable(row)
    asset = db.get(Asset, row.asset_id)
    if asset is None:
        raise MediaIndexError("Asset not found")
    if row.media_kind == "video":
        kind = classify_link(asset.source_url)
        stored = asset.file_status == "stored" and bool(asset.file_key)
        if kind == "youtube" and not stored:
            _emit_stage(on_stage, "Downloading YouTube video…")
            transcript = sync_youtube(asset)
            if transcript:
                row.transcript = normalize_transcript(transcript)
        elif not stored:
            _emit_stage(on_stage, "Downloading video…")
            sync_one(db, asset)
        else:
            _emit_stage(on_stage, "Video already stored")
    else:
        _emit_stage(on_stage, "Syncing photo folder…")
        sync_one(db, asset)
        image_assets = list_image_assets(db, asset)
        if image_assets:
            def thumbnail_progress(done: int, total: int) -> None:
                if done == 1 or done == total or done % 10 == 0:
                    _emit_stage(on_stage, f"Creating previews… {done}/{total}")

            _, thumbnail_errors = ensure_thumbnails(image_assets, thumbnail_progress)
            for image_id, error in thumbnail_errors:
                print(f"  thumbnail failed for asset {image_id}: {error}", flush=True)
        if not row.visual_description.strip():
            _emit_stage(on_stage, "Describing images…")
            images, keys = collect_image_bytes(db, asset)
            row.visual_description = describe_images(images)
            row.image_keys = json.dumps(keys, ensure_ascii=False)
    if row.vision.strip() and has_extract(row):
        _emit_stage(on_stage, "Generating recommendations…")
        apply_recommendations(db, row)
    else:
        touch(row)
    db.commit()
    db.refresh(row)
    return row


def describe_media(db: Session, media_id: int, on_stage: StageCallback | None = None) -> MediaIndex:
    row = db.get(MediaIndex, media_id)
    if row is None:
        raise MediaIndexError("Media index not found")
    require_editable(row)
    if row.media_kind != "photo":
        raise MediaIndexError("Visual descriptions apply to photo sets")
    asset = db.get(Asset, row.asset_id)
    if asset is None:
        raise MediaIndexError("Asset not found")
    _emit_stage(on_stage, "Collecting images…")
    images, keys = collect_image_bytes(db, asset)
    _emit_stage(on_stage, "Describing images…")
    row.visual_description = describe_images(images)
    row.image_keys = json.dumps(keys, ensure_ascii=False)
    touch(row)
    db.commit()
    db.refresh(row)
    return row
