from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from datetime import datetime, timezone
from io import BytesIO
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from backend.database import ensure_schema
from backend.models import Asset, Job, MediaIndex, VideoClickEvent, utc_now

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
    RecommendedMediaOut,
)
from backend.storage import delete_file, file_exists, read_file

CHILD_TITLE_SEP = " — "
MAX_IMAGES = 6
MAX_IMAGE_EDGE = 1024
MAX_RECOMMENDATIONS = 15
MAX_RECOMMENDED_VIDEOS = 5  # retained for admin/tooling defaults; result pages return all videos
MAX_RECOMMENDED_PICTURES = 5
FEEDBACK_MIN_GENERATIONS = 3
FEEDBACK_MAX_BOOST = 0.35
REJECTION_PENALTY = 0.55
CLICK_ORDER_WEIGHTS = (1.0, 0.7, 0.45, 0.3, 0.2)
VALID_VIDEO_INTERACTIONS = {"play", "open_source"}
DRIVE_FILE_RE = re.compile(r"/file/d/([a-zA-Z0-9_-]+)")
DRIVE_ID_RE = re.compile(r"[?&]id=([a-zA-Z0-9_-]+)")
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
    return {"verdicts": {}, "added": [], "recommended_image_ids": [], "excluded_image_ids": []}


def parse_id_list(raw: Any) -> list[int]:
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    seen: set[int] = set()
    for value in raw:
        try:
            image_id = int(value)
        except (TypeError, ValueError):
            continue
        if image_id <= 0 or image_id in seen:
            continue
        seen.add(image_id)
        ids.append(image_id)
    return ids


def parse_excluded_image_ids(raw: Any) -> list[int]:
    return parse_id_list(raw)


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
    recommended_ids = parse_id_list(
        payload.get("recommended_image_ids") or payload.get("selected_image_ids")
    )
    return {
        "verdicts": verdicts,
        "added": added,
        "recommended_image_ids": recommended_ids,
        "excluded_image_ids": parse_id_list(payload.get("excluded_image_ids")),
    }


def recommended_image_ids_for(row: MediaIndex) -> set[int]:
    return set(parse_feedback(row.feedback_json)["recommended_image_ids"])


def excluded_image_ids_for(row: MediaIndex) -> set[int]:
    return set(parse_feedback(row.feedback_json)["excluded_image_ids"])


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


def drive_file_id(url: str) -> str:
    text = url or ""
    match = DRIVE_FILE_RE.search(text)
    if match:
        return match.group(1)
    match = DRIVE_ID_RE.search(text)
    return match.group(1) if match else ""


def drive_image_url(url: str) -> str:
    file_id = drive_file_id(url)
    if not file_id:
        return url or ""
    return f"https://drive.google.com/uc?export=view&id={file_id}"


def _recommendation_for_ref(row: MediaIndex | None, recipe_ref: str) -> dict[str, Any] | None:
    if row is None or not recipe_ref:
        return None
    feedback = parse_feedback(row.feedback_json)
    verdicts = feedback.get("verdicts") or {}
    verdict = verdicts.get(recipe_ref) if isinstance(verdicts, dict) else None
    rejected = isinstance(verdict, dict) and str(verdict.get("verdict") or "").strip().lower() == "no"
    items = parse_recommendations(row.recommendations_json).get("items") or []
    match = next(
        (
            item
            for item in items
            if isinstance(item, dict) and str(item.get("recipe_ref") or "") == recipe_ref
        ),
        None,
    )
    added = any(
        isinstance(entry, dict) and str(entry.get("recipe_ref") or "") == recipe_ref
        for entry in (feedback.get("added") or [])
    )
    if match is None and not added and not rejected:
        return None
    return {
        "recipe_ref": recipe_ref,
        "temperatures": list(match.get("temperatures") or []) if match else [],
        "confidence": float(match.get("confidence") or 0.0) if match else 0.0,
        "rationale": str(match.get("rationale") or "") if match else "",
        "added": added,
        "rejected": rejected,
    }


def _recommendation_sort_key(item: dict[str, Any], temperature: str) -> tuple[int, float]:
    temps = [str(code) for code in (item.get("temperatures") or [])]
    mismatch = 1 if temperature and temps and temperature not in temps else 0
    return (mismatch, -float(item.get("confidence") or 0.0))


def video_limit_for(duration: str) -> int | None:
    """Result pages return every linked video; duration no longer caps the list."""
    _ = duration
    return None


def _recipe_cluster(recipe_ref: str) -> str:
    token = (recipe_ref or "").strip().upper()
    return token[0] if token and token[0] in {"A", "B", "C", "D", "E", "F"} else ""


def _rejected_for_ref(row: MediaIndex | None, recipe_ref: str) -> bool:
    if row is None:
        return False
    feedback = parse_feedback(row.feedback_json)
    verdicts = feedback.get("verdicts") or {}
    verdict = verdicts.get(recipe_ref) if isinstance(verdicts, dict) else None
    return isinstance(verdict, dict) and str(verdict.get("verdict") or "").strip().lower() == "no"


def _has_exact_recipe_item(row: MediaIndex, recipe_ref: str) -> bool:
    items = parse_recommendations(row.recommendations_json).get("items") or []
    return any(
        isinstance(item, dict) and str(item.get("recipe_ref") or "") == recipe_ref for item in items
    )


def _best_scored_item(row: MediaIndex | None, recipe_ref: str) -> tuple[int, dict[str, Any]]:
    """Return (tier, item): 0 exact, 1 same cluster, 2 other usecase, 3 unscored."""
    empty = {
        "recipe_ref": recipe_ref,
        "temperatures": [],
        "confidence": 0.0,
        "rationale": "",
        "added": False,
        "rejected": False,
    }
    if row is None:
        return 3, empty
    rejected = _rejected_for_ref(row, recipe_ref)
    exact = _recommendation_for_ref(row, recipe_ref)
    if exact is not None and (
        exact.get("added")
        or float(exact.get("confidence") or 0.0) > 0
        or _has_exact_recipe_item(row, recipe_ref)
    ):
        exact["rejected"] = rejected
        return 0, exact
    items = [
        item
        for item in (parse_recommendations(row.recommendations_json).get("items") or [])
        if isinstance(item, dict) and str(item.get("recipe_ref") or "").strip()
    ]
    cluster = _recipe_cluster(recipe_ref)

    def _score(item: dict[str, Any]) -> float:
        return float(item.get("confidence") or 0.0)

    if items:
        same_cluster = [
            item
            for item in items
            if cluster and _recipe_cluster(str(item.get("recipe_ref") or "")) == cluster
        ]
        if same_cluster:
            match = max(same_cluster, key=_score)
            return 1, {
                "recipe_ref": str(match.get("recipe_ref") or ""),
                "temperatures": list(match.get("temperatures") or []),
                "confidence": _score(match),
                "rationale": str(match.get("rationale") or ""),
                "added": False,
                "rejected": rejected,
            }
        match = max(items, key=_score)
        return 2, {
            "recipe_ref": str(match.get("recipe_ref") or ""),
            "temperatures": list(match.get("temperatures") or []),
            "confidence": _score(match),
            "rationale": str(match.get("rationale") or ""),
            "added": False,
            "rejected": rejected,
        }
    empty["rejected"] = rejected
    return 3, empty


def _click_order_weight(order: int) -> float:
    if order < 1:
        return 0.0
    if order <= len(CLICK_ORDER_WEIGHTS):
        return CLICK_ORDER_WEIGHTS[order - 1]
    return CLICK_ORDER_WEIGHTS[-1] * (0.5 ** (order - len(CLICK_ORDER_WEIGHTS)))


def feedback_boosts_for_recipe(db: Session, recipe_ref: str) -> dict[int, float]:
    """Bounded global boosts from unique per-generation click order."""
    if not (recipe_ref or "").strip():
        return {}
    _ensure_media_schema()
    events = (
        db.query(VideoClickEvent)
        .filter(VideoClickEvent.recipe_ref == recipe_ref)
        .order_by(VideoClickEvent.generation_id.asc(), VideoClickEvent.click_order.asc())
        .all()
    )
    if not events:
        return {}
    generations = {event.generation_id for event in events}
    if len(generations) < FEEDBACK_MIN_GENERATIONS:
        return {}
    raw: dict[int, float] = {}
    for event in events:
        raw[event.asset_id] = raw.get(event.asset_id, 0.0) + _click_order_weight(event.click_order)
    peak = max(raw.values()) if raw else 0.0
    if peak <= 0:
        return {}
    return {asset_id: FEEDBACK_MAX_BOOST * (score / peak) for asset_id, score in raw.items()}


def _video_rank_key(
    tier: int,
    item: dict[str, Any],
    temperature: str,
    feedback_boost: float,
) -> tuple[float, int, int, float, int]:
    """Lower is better. Semantic tier dominates; feedback only reorders nearby."""
    rejected = 1 if item.get("rejected") else 0
    temp_mismatch, neg_confidence = _recommendation_sort_key(item, temperature)
    primary = float(tier) + (REJECTION_PENALTY if rejected else 0.0) - float(feedback_boost)
    return (primary, temp_mismatch, rejected, neg_confidence, -int(feedback_boost * 1000))


def _media_source_url(asset: Asset | None) -> str:
    if asset is None:
        return ""
    return (asset.source_url or asset.url or "").strip()


def _video_thumbnail_url(url: str) -> str:
    from backend.youtube_apify import VIDEO_ID_RE, is_youtube_url

    if is_youtube_url(url):
        match = VIDEO_ID_RE.search(url or "")
        if match:
            return f"https://img.youtube.com/vi/{match.group(1)}/hqdefault.jpg"
        return ""
    file_id = drive_file_id(url)
    if file_id:
        return f"https://drive.google.com/thumbnail?id={file_id}&sz=w1000"
    return ""


def _recommended_video(asset: Asset, item: dict[str, Any]) -> RecommendedMediaOut:
    source = _media_source_url(asset)
    return RecommendedMediaOut(
        asset_id=asset.id,
        parent_asset_id=asset.id,
        media_kind="video",
        title=asset.title,
        source_url=source,
        preview_url=source,
        thumbnail_url=_video_thumbnail_url(source),
        confidence=float(item.get("confidence") or 0.0),
        rationale=str(item.get("rationale") or ""),
    )


def _recommended_picture(child: Asset, parent: Asset, item: dict[str, Any]) -> RecommendedMediaOut:
    source = _media_source_url(child) or _media_source_url(parent)
    return RecommendedMediaOut(
        asset_id=child.id,
        parent_asset_id=parent.id,
        media_kind="photo",
        title=child.title,
        source_url=source,
        thumbnail_url=f"/api/assets/{child.id}/thumbnail.jpg",
        preview_url=drive_image_url(source),
        confidence=float(item.get("confidence") or 0.0),
        rationale=str(item.get("rationale") or ""),
    )


def pick_recommended_media(
    db: Session,
    recipe_ref: str,
    temperature: str = "",
    duration: str = "",
    video_limit: int | None = None,
    picture_limit: int = MAX_RECOMMENDED_PICTURES,
) -> tuple[list[RecommendedMediaOut], list[RecommendedMediaOut]]:
    _ = duration
    if not (recipe_ref or "").strip():
        return [], []
    _ensure_media_schema()
    boosts = feedback_boosts_for_recipe(db, recipe_ref)

    video_assets = db.query(Asset).filter(Asset.type == "video").order_by(Asset.id.asc()).all()
    linked_videos = [
        asset for asset in video_assets if _media_source_url(asset) and is_library_parent(asset)
    ]
    index_rows = {
        row.asset_id: row
        for row in db.query(MediaIndex).filter(MediaIndex.media_kind == "video").all()
    }
    ranked_videos: list[tuple[tuple[Any, ...], RecommendedMediaOut]] = []
    for asset in linked_videos:
        row = index_rows.get(asset.id)
        tier, item = _best_scored_item(row, recipe_ref)
        key = _video_rank_key(tier, item, temperature, boosts.get(asset.id, 0.0))
        ranked_videos.append((key, _recommended_video(asset, item)))
    ranked_videos.sort(key=lambda entry: entry[0])
    videos = [item for _key, item in ranked_videos]
    if video_limit is not None and video_limit >= 0:
        videos = videos[:video_limit]

    rows = db.query(MediaIndex).filter(MediaIndex.recommendations_json != "").all()
    asset_ids = [row.asset_id for row in rows]
    assets = {
        asset.id: asset
        for asset in (
            db.query(Asset).filter(Asset.id.in_(asset_ids)).all() if asset_ids else []
        )
    }
    photo_sets: list[tuple[tuple[int, float], Asset, dict[str, Any], set[int]]] = []
    for row in rows:
        if row.media_kind != "photo":
            continue
        asset = assets.get(row.asset_id)
        if asset is None:
            continue
        item = _recommendation_for_ref(row, recipe_ref)
        if item is None or item.get("rejected"):
            continue
        key = _recommendation_sort_key(item, temperature)
        photo_sets.append((key, asset, item, recommended_image_ids_for(row)))
    photo_sets.sort(key=lambda entry: entry[0])
    pictures: list[RecommendedMediaOut] = []
    queues = [
        deque(
            child
            for child in list_image_assets(db, asset)
            if child.id in recommended_ids
        )
        for _key, asset, _item, recommended_ids in photo_sets
    ]
    items_by_queue = [item for _key, _asset, item, _excluded in photo_sets]
    parents = [asset for _key, asset, _item, _excluded in photo_sets]
    while len(pictures) < picture_limit and any(queues):
        for index, queue in enumerate(queues):
            if not queue:
                continue
            pictures.append(_recommended_picture(queue.popleft(), parents[index], items_by_queue[index]))
            if len(pictures) >= picture_limit:
                break
    return videos, pictures


def record_video_click(
    db: Session,
    *,
    generation_id: int,
    user_id: int,
    asset_id: int,
    recipe_ref: str,
    displayed_rank: int,
    interaction_type: str,
) -> tuple[VideoClickEvent, bool]:
    """Store the first meaningful interaction with a video for a generation."""
    interaction = (interaction_type or "play").strip().lower()
    if interaction not in VALID_VIDEO_INTERACTIONS:
        raise ValueError("interaction_type must be play or open_source")
    existing = (
        db.query(VideoClickEvent)
        .filter(
            VideoClickEvent.generation_id == generation_id,
            VideoClickEvent.user_id == user_id,
            VideoClickEvent.asset_id == asset_id,
        )
        .first()
    )
    if existing is not None:
        return existing, False
    prior = (
        db.query(VideoClickEvent)
        .filter(
            VideoClickEvent.generation_id == generation_id,
            VideoClickEvent.user_id == user_id,
        )
        .count()
    )
    event = VideoClickEvent(
        generation_id=generation_id,
        user_id=user_id,
        asset_id=asset_id,
        recipe_ref=(recipe_ref or "").strip(),
        displayed_rank=max(0, int(displayed_rank or 0)),
        click_order=prior + 1,
        interaction_type=interaction,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event, True


def list_image_assets(db: Session, asset: Asset) -> list[Asset]:
    if (asset.content_type or "").startswith("image/") and asset.file_status in {"stored", "preview"}:
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
        if child.file_status in {"stored", "preview"}
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
            if child.file_key:
                key = child.file_key
            else:
                from backend.thumbnails import resolve_thumbnail_key

                key = resolve_thumbnail_key(child)
                if not key:
                    continue
            data = _downscale_jpeg(read_file(key))
        except Exception:
            continue
        images.append(data)
        keys.append(key)
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
    if kind == "video":
        extracted = (
            f"Audio transcript:\n{transcript.strip() or '(none)'}\n\n"
            f"Visual narrative and relevance:\n{visual_description.strip() or '(none)'}"
        )
        extracted_label = "Combined video analysis"
    else:
        extracted = visual_description.strip()
        extracted_label = "Visual description"
    system = (
        "You match Masters' Union media (a video or a photo set) to pitch personas. "
        "Base recommendations on the extracted analysis first. "
        "If a human vision note is present, refine the matches with it; "
        "if it is empty, still recommend from the analysis alone. "
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
        f"Human vision:\n{vision.strip() or '(none — recommend from the analysis only)'}\n\n"
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


def _image_outs(
    db: Session,
    asset: Asset | None,
    recommended_ids: set[int] | None = None,
) -> list[MediaImageOut]:
    if asset is None:
        return []
    active = recommended_ids or set()
    return [
        MediaImageOut(
            id=child.id,
            title=child.title,
            file_status=child.file_status,
            content_type=child.content_type,
            recommended=child.id in active,
            excluded=child.id not in active,
        )
        for child in list_image_assets(db, asset)
    ]


def set_image_recommended(db: Session, row: MediaIndex, image_asset_id: int, recommended: bool) -> dict[str, Any]:
    """Toggle whether an image in this photo set is selected to be recommended during generation."""
    if row.media_kind != "photo":
        raise MediaIndexError("Only photo sets can select recommended images")
    parent = db.get(Asset, row.asset_id)
    if parent is None:
        raise MediaIndexError("Photo set asset not found")
    children = list_image_assets(db, parent)
    if not any(child.id == image_asset_id for child in children):
        raise MediaIndexError("Image does not belong to this photo set")
    feedback = parse_feedback(row.feedback_json)
    current = set(feedback["recommended_image_ids"])
    if recommended:
        current.add(image_asset_id)
    else:
        current.discard(image_asset_id)
    feedback["recommended_image_ids"] = sorted(current)
    return feedback


def set_image_excluded(db: Session, row: MediaIndex, image_asset_id: int, excluded: bool) -> dict[str, Any]:
    """Legacy helper: excluding an image is equivalent to un-recommending it."""
    return set_image_recommended(db, row, image_asset_id, recommended=not excluded)


def serialize(row: MediaIndex, db: Session, job: Job | None = None) -> MediaIndexOut:
    asset = db.get(Asset, row.asset_id)
    rec = parse_recommendations(row.recommendations_json)
    fb = parse_feedback(row.feedback_json)
    recommended_ids = set(fb["recommended_image_ids"])
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
            recommended_image_ids=list(fb["recommended_image_ids"]),
            excluded_image_ids=list(fb["excluded_image_ids"]),
        ),
        image_assets=_image_outs(db, asset, recommended_ids),
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
    if row.media_kind == "video":
        if row.transcript.strip():
            parts.append(f"Audio transcript:\n{row.transcript.strip()}")
        if row.visual_description.strip():
            parts.append(f"Visual narrative and relevance:\n{row.visual_description.strip()}")
    elif row.visual_description.strip():
        parts.append(f"Visual description:\n{row.visual_description.strip()}")
    if (row.vision or "").strip():
        parts.append(f"Vision:\n{row.vision.strip()}")
    return "\n\n".join(parts)


def has_extract(row: MediaIndex) -> bool:
    if row.media_kind == "video":
        return bool(row.transcript.strip() or row.visual_description.strip())
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
    from backend.transcription import analyze_video_asset

    row = get_or_create_media_index(db, asset_id)
    require_editable(row)
    asset = db.get(Asset, row.asset_id)
    if asset is None:
        raise MediaIndexError("Asset not found")
    image_assets: list[Asset] = []
    if row.media_kind == "video":
        kind = classify_link(asset.source_url)
        stored = asset.file_status == "stored" and bool(asset.file_key)
        processed = asset.file_status == "processed" and has_extract(row)
        if processed:
            _emit_stage(on_stage, "Video already analyzed")
        elif kind == "youtube" and not stored:
            _emit_stage(on_stage, "Downloading YouTube video…")
            transcript = sync_youtube(asset)
            if transcript:
                row.transcript = normalize_transcript(transcript)
        elif not stored:
            _emit_stage(on_stage, "Downloading video…")
            sync_one(db, asset)
        else:
            _emit_stage(on_stage, "Video already stored")
        if not row.transcript.strip() or not row.visual_description.strip():
            if asset.file_status != "stored" or not asset.file_key:
                detail = asset.sync_error or "Video download did not complete"
                raise MediaIndexError(detail)
            analysis = analyze_video_asset(asset, row.transcript, on_stage)
            row.transcript = normalize_transcript(analysis.transcript)
            row.visual_description = analysis.visual_description
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
    if has_extract(row) and not row.vision_frozen:
        _emit_stage(on_stage, "Generating recommendations…")
        try:
            apply_recommendations(db, row)
        except Exception as exc:  # noqa: BLE001
            print(f"  media {getattr(row, 'id', '?')} recommendation skipped: {exc}", flush=True)
            touch(row)
    else:
        touch(row)
    if row.media_kind == "video" and has_extract(row) and asset.file_key and asset.source_url:
        _emit_stage(on_stage, "Removing processed video…")
        delete_file(asset.file_key)
        asset.file_key = ""
        asset.file_status = "processed"
        asset.url = asset.source_url
    elif row.media_kind == "photo" and row.visual_description.strip():
        from backend.thumbnails import resolve_thumbnail_key

        retained_keys: list[str] = []
        for image_asset in image_assets:
            resolved = resolve_thumbnail_key(image_asset)
            if resolved is None:
                continue
            retained_keys.append(resolved)
            if image_asset.file_key:
                delete_file(image_asset.file_key)
                image_asset.file_key = ""
            image_asset.file_status = "preview"
            image_asset.url = f"/api/assets/{image_asset.id}/thumbnail.jpg"
        row.image_keys = json.dumps(retained_keys[:MAX_IMAGES], ensure_ascii=False)
    db.commit()
    db.refresh(row)
    if row.media_kind == "video" and (row.transcript or "").strip():
        try:
            from backend.transcript_search import SOURCE_MEDIA, safe_upsert_source

            safe_upsert_source(
                db=db,
                source_type=SOURCE_MEDIA,
                source_id=str(row.id),
                source_name=(asset.title if asset else "") or f"Media {row.id}",
                text=row.transcript or "",
            )
        except Exception:  # noqa: BLE001
            print(f"  media {getattr(row, 'id', '?')} transcript index skipped", flush=True)
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
    if has_extract(row) and not row.vision_frozen:
        _emit_stage(on_stage, "Generating recommendations…")
        try:
            apply_recommendations(db, row)
        except Exception as exc:  # noqa: BLE001
            print(f"  media {getattr(row, 'id', '?')} recommendation skipped: {exc}", flush=True)
            touch(row)
    else:
        touch(row)
    db.commit()
    db.refresh(row)
    return row
