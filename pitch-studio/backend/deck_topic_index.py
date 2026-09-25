"""Group Brand Deck pages into topics and apply Media Testing vision logic."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import ensure_schema
from backend.media_index import (
    ACTIVE_JOB_STATUSES,
    MAX_RECOMMENDATIONS,
    _format_axes,
    _format_feedback,
    _format_personas,
    _normalize_items,
    empty_feedback,
    parse_feedback,
    parse_recommendations,
    sha256_text,
)
from backend.models import Asset, DeckTopic, Job, utc_now
from backend.pipeline import dsai_deck
from backend.pipeline.brand_deck import (
    PAGE_LABELS,
    PAGE_MODULES,
    SOURCE_PAGE_COUNT,
    brand_deck_file_key,
    page_image,
)
from backend.pipeline.llm import chat_json, chat_text_multimodal
from backend.recipe_cache import list_recipe_options
from backend.schemas import (
    DeckTopicListOut,
    DeckTopicOut,
    DeckTopicPageOut,
    MediaAddedUsecase,
    MediaFeedback,
    MediaRecommendationItem,
    MediaRecommendations,
    MediaVerdict,
    RecipeOption,
)
from backend.transcription import normalize_visual_description

JOB_DECK_PREPARE = "deck_topics_prepare"
StageCallback = Callable[[str], None]
MAX_TOPIC_FRAMES = 3
MAX_SUMMARY_CHARS = 500

BRAND_DECK = "brand"
DECKS = (BRAND_DECK, dsai_deck.DECK_KEY)


def group_prompt(deck_name: str = "Brand Deck") -> str:
    return (
        f"You group Masters' Union {deck_name} slides into coherent topics. "
        "Pages must stay in order. Each topic is a contiguous page range. "
        "Together the topics must cover every page exactly once, with no gaps "
        "and no overlaps. Prefer natural story sections over one topic per page. "
        "Aim for 10-22 topics. module_ids are pitch module codes (M01-M14) the "
        "topic supports; leave the list empty when none fit. Return strict JSON: "
        '{"topics":[{"title":"","start_page":1,"end_page":3,"module_ids":["M01"]}]}'
    )


GROUP_PROMPT = group_prompt()


class DeckTopicError(RuntimeError):
    pass


def normalize_deck(deck: str | None) -> str:
    value = (deck or BRAND_DECK).strip().lower()
    if value not in DECKS:
        raise DeckTopicError(f"Unknown deck: {deck}")
    return value


def deck_name(deck: str) -> str:
    return dsai_deck.DECK_NAME if deck == dsai_deck.DECK_KEY else "Brand Deck"


def deck_labels(deck: str, asset: Asset | None = None) -> dict[int, str]:
    if deck == dsai_deck.DECK_KEY:
        return dsai_deck.page_labels(asset) if asset is not None else {}
    return PAGE_LABELS


def deck_modules(deck: str) -> dict[int, str]:
    return {} if deck == dsai_deck.DECK_KEY else PAGE_MODULES


def deck_page_count(deck: str, asset: Asset | None = None) -> int:
    if deck == dsai_deck.DECK_KEY:
        return dsai_deck.page_count(asset)
    return SOURCE_PAGE_COUNT


def deck_page_image(deck: str, page: int, file_key: str | None) -> bytes:
    if deck == dsai_deck.DECK_KEY:
        return dsai_deck.page_image(page, file_key)
    return page_image(page, file_key)


def deck_file_key(db: Session, deck: str, asset: Asset) -> str | None:
    if deck == dsai_deck.DECK_KEY:
        return asset.file_key or None
    return brand_deck_file_key(db)


def deck_for_asset(db: Session, asset_id: int) -> str:
    asset = db.get(Asset, asset_id) if asset_id else None
    if asset is not None and asset.type == dsai_deck.ASSET_TYPE:
        return dsai_deck.DECK_KEY
    return BRAND_DECK


def _emit_stage(on_stage: StageCallback | None, text: str) -> None:
    if on_stage is not None:
        on_stage(text)


def _ensure_schema() -> None:
    ensure_schema()


def parse_pages(raw: str) -> list[int]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    pages: list[int] = []
    for item in payload:
        try:
            page = int(item)
        except (TypeError, ValueError):
            continue
        if page > 0:
            pages.append(page)
    return pages


def page_items_for(pages: list[int], deck: str = BRAND_DECK) -> list[DeckTopicPageOut]:
    if deck == dsai_deck.DECK_KEY:
        return [
            DeckTopicPageOut(
                page=page,
                label=dsai_deck.cached_label(page),
                module_id="",
                image_url=dsai_deck.dsai_image_url(page),
            )
            for page in pages
        ]
    return [
        DeckTopicPageOut(
            page=page,
            label=PAGE_LABELS.get(page, f"Page {page}"),
            module_id=PAGE_MODULES.get(page, ""),
            image_url=f"/api/brand-deck/pages/{page}.jpg",
        )
        for page in pages
    ]


def find_deck_asset(db: Session, deck: str = BRAND_DECK) -> Asset:
    if deck == dsai_deck.DECK_KEY:
        _ensure_schema()
        asset = dsai_deck.find_dsai_asset(db)
        if asset is None:
            raise DeckTopicError("DS & AI deck has not been imported yet")
        return asset
    return find_brand_deck_asset(db)


def find_brand_deck_asset(db: Session) -> Asset:
    _ensure_schema()
    asset = (
        db.query(Asset)
        .filter(Asset.type == "report")
        .filter(Asset.title.ilike("%brand deck%"))
        .order_by(Asset.id)
        .first()
    )
    if asset is None:
        raise DeckTopicError("Brand Deck report asset was not found")
    return asset


def page_text_map(asset: Asset) -> dict[int, str]:
    if not asset.extract_json:
        return {}
    try:
        payload = json.loads(asset.extract_json)
    except json.JSONDecodeError:
        return {}
    texts: dict[int, str] = {}
    for page in payload.get("pages") or []:
        if not isinstance(page, dict):
            continue
        try:
            number = int(page.get("page") or 0)
        except (TypeError, ValueError):
            continue
        text = str(page.get("text") or "").strip()
        if number > 0 and text:
            texts[number] = text
    return texts


def build_page_catalog(asset: Asset, deck: str = BRAND_DECK) -> list[dict[str, Any]]:
    texts = page_text_map(asset)
    labels = deck_labels(deck, asset)
    modules = deck_modules(deck)
    catalog: list[dict[str, Any]] = []
    for page in range(1, deck_page_count(deck, asset) + 1):
        catalog.append(
            {
                "page": page,
                "label": labels.get(page, f"Page {page}"),
                "module_id": modules.get(page, ""),
                "text": texts.get(page, ""),
            }
        )
    return catalog


def catalog_source_hash(catalog: list[dict[str, Any]]) -> str:
    return sha256_text(json.dumps(catalog, ensure_ascii=False, sort_keys=True))


def fallback_topic_groups(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for item in catalog:
        module_id = item["module_id"] or ""
        if current is None:
            current = {
                "title": item["label"],
                "start_page": item["page"],
                "end_page": item["page"],
                "module_ids": [module_id] if module_id else [],
            }
            continue
        if module_id == (current["module_ids"][0] if current["module_ids"] else ""):
            current["end_page"] = item["page"]
            continue
        groups.append(current)
        current = {
            "title": item["label"],
            "start_page": item["page"],
            "end_page": item["page"],
            "module_ids": [module_id] if module_id else [],
        }
    if current is not None:
        groups.append(current)
    return validate_topic_groups(groups, page_count=len(catalog))


def validate_topic_groups(raw: list[dict[str, Any]], page_count: int = SOURCE_PAGE_COUNT) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item.get("start_page") or 0)
            end = int(item.get("end_page") or start)
        except (TypeError, ValueError):
            continue
        if start < 1:
            start = 1
        if end > page_count:
            end = page_count
        if end < start:
            continue
        modules = item.get("module_ids") or []
        if isinstance(modules, str):
            modules = [part.strip() for part in modules.split(",") if part.strip()]
        if not isinstance(modules, list):
            modules = []
        parsed.append(
            {
                "title": str(item.get("title") or f"Pages {start}-{end}").strip() or f"Pages {start}-{end}",
                "start_page": start,
                "end_page": end,
                "module_ids": [str(code).strip() for code in modules if str(code).strip()],
            }
        )
    if not parsed:
        raise DeckTopicError("Model did not return any topics")
    parsed.sort(key=lambda item: (item["start_page"], item["end_page"]))
    for index in range(1, len(parsed)):
        if parsed[index]["start_page"] <= parsed[index - 1]["end_page"]:
            parsed[index]["start_page"] = parsed[index - 1]["end_page"] + 1
    parsed = [item for item in parsed if item["start_page"] <= item["end_page"] <= page_count]
    if not parsed:
        raise DeckTopicError("Topics do not cover every brand deck page")
    if parsed[0]["start_page"] > 1:
        parsed[0]["start_page"] = 1
    for index in range(1, len(parsed)):
        expected = parsed[index - 1]["end_page"] + 1
        if parsed[index]["start_page"] > expected:
            parsed[index - 1]["end_page"] = parsed[index]["start_page"] - 1
        elif parsed[index]["start_page"] < expected:
            parsed[index]["start_page"] = expected
    parsed = [item for item in parsed if item["start_page"] <= item["end_page"]]
    if parsed[-1]["end_page"] < page_count:
        parsed[-1]["end_page"] = page_count
    covered: list[int] = []
    for item in parsed:
        covered.extend(range(item["start_page"], item["end_page"] + 1))
    if covered != list(range(1, page_count + 1)):
        raise DeckTopicError("Topics do not cover every brand deck page")
    return parsed


def _catalog_prompt(catalog: list[dict[str, Any]]) -> str:
    lines = []
    for item in catalog:
        text = " ".join((item["text"] or "").split())
        if len(text) > 240:
            text = text[:240].rsplit(" ", 1)[0] + "…"
        lines.append(
            f"p{item['page']}: {item['label']} [{item['module_id'] or 'none'}] {text}".rstrip()
        )
    return "\n".join(lines)


def group_brand_deck_pages(catalog: list[dict[str, Any]], name: str = "Brand Deck") -> list[dict[str, Any]]:
    payload = chat_json(
        [
            {"role": "system", "content": group_prompt(name)},
            {
                "role": "user",
                "content": (
                    f"Group these {len(catalog)} {name} pages into contiguous topics.\n\n"
                    f"{_catalog_prompt(catalog)}"
                ),
            },
        ],
        timeout=180.0,
    )
    raw = payload.get("topics") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise DeckTopicError("Model did not return a topic list")
    return validate_topic_groups(raw, page_count=len(catalog))


def representative_pages(start: int, end: int) -> list[int]:
    pages = list(range(start, end + 1))
    if len(pages) <= MAX_TOPIC_FRAMES:
        return pages
    if len(pages) == 2:
        return pages
    middle = pages[len(pages) // 2]
    chosen = [pages[0], middle, pages[-1]]
    unique: list[int] = []
    for page in chosen:
        if page not in unique:
            unique.append(page)
    return unique


def describe_topic_pages(
    start: int,
    end: int,
    catalog: list[dict[str, Any]],
    file_key: str | None,
    deck: str = BRAND_DECK,
) -> str:
    by_page = {item["page"]: item for item in catalog}
    frames: list[bytes] = []
    notes: list[str] = []
    for page in representative_pages(start, end):
        item = by_page.get(page) or {}
        notes.append(f"p{page}: {item.get('label') or PAGE_LABELS.get(page, '')}")
        try:
            frames.append(deck_page_image(deck, page, file_key))
        except Exception:
            continue
    excerpt = " | ".join(notes)
    prompt = (
        f"These are consecutive {deck_name(deck)} slides for one topic. Summarize the topic, "
        "what the slides argue, and why they belong together as communications material. "
        "Return exactly one plain-text paragraph with no Markdown. "
        f"The complete response must be at most {MAX_SUMMARY_CHARS} characters.\n\n"
        f"Slide labels: {excerpt}"
    )
    if frames:
        return normalize_visual_description(chat_text_multimodal(prompt, frames, timeout=180.0))
    fallback = " ".join(
        part
        for page in range(start, end + 1)
        for part in [
            by_page.get(page, {}).get("label", ""),
            by_page.get(page, {}).get("text", ""),
        ]
        if part
    )
    return normalize_visual_description(fallback or f"Pages {start}-{end} of the {deck_name(deck)}.")


def is_stale(row: DeckTopic) -> bool:
    if not row.indexed_vision_hash:
        return bool(row.vision and not row.recommendations_json)
    return row.vision_hash != row.indexed_vision_hash


def require_editable(row: DeckTopic) -> None:
    if row.vision_frozen:
        raise DeckTopicError("Vision is frozen. Unfreeze it before editing.")


def touch(row: DeckTopic) -> None:
    row.updated_at = utc_now()


def has_extract(row: DeckTopic) -> bool:
    return bool((row.summary or "").strip())


def derive_status(row: DeckTopic, job: Job | None = None) -> str:
    # A global prepare job must not paint every topic as processing once it is indexed.
    recommendations = getattr(row, "recommendations_json", None) or ""
    if (
        job is not None
        and job.status in ACTIVE_JOB_STATUSES
        and getattr(row, "status", "") not in {"indexed", "frozen"}
        and not recommendations
    ):
        return "processing"
    if getattr(row, "vision_frozen", False):
        return "frozen"
    if getattr(row, "status", "") == "indexed":
        return "indexed"
    if has_extract(row):
        return "ready"
    return "draft"


def build_context_index(row: DeckTopic) -> str:
    pages = parse_pages(row.pages_json)
    parts = [
        f"Topic: {row.title.strip() or 'Untitled'}",
        f"Pages: {pages[0]}-{pages[-1]}" if pages else "Pages: (none)",
    ]
    if row.module_ids.strip():
        parts.append(f"Modules: {row.module_ids}")
    if row.summary.strip():
        parts.append(f"Topic summary:\n{row.summary.strip()}")
    if row.vision.strip():
        parts.append(f"Vision:\n{row.vision.strip()}")
    return "\n\n".join(parts)


def build_topic_recommendation_messages(
    title: str,
    pages: list[int],
    summary: str,
    vision: str,
    recipe_options: list[RecipeOption],
    feedback: dict[str, Any] | None = None,
    deck: str = BRAND_DECK,
) -> list[dict[str, str]]:
    page_range = f"{pages[0]}-{pages[-1]}" if pages else "(none)"
    usage_note = (
        "These slides are only added to a pitch when the request is about data science / AI, "
        "on top of the main brand deck. Classroom exercises, quizzes and workshop activities "
        "are not pitch material: recommend no personas for them. "
        if deck == dsai_deck.DECK_KEY
        else ""
    )
    system = (
        f"You match a Masters' Union {deck_name(deck)} topic to pitch personas. "
        f"{usage_note}"
        "Base recommendations on the topic summary first. "
        "If a human vision note is present, refine the matches with it; "
        "if it is empty, still recommend from the analysis alone. "
        "Only recommend personas that should actually see these slides. "
        "Return strict JSON: {\"items\":[{\"recipe_ref\":\"\",\"temperatures\":[],"
        "\"confidence\":0.0,\"rationale\":\"\"}]}. "
        f"Use only recipe_ref values from the catalog. Rank by confidence. Max {MAX_RECOMMENDATIONS} items. "
        "Temperatures must be X1-X5 codes. Rationale is one short sentence."
    )
    user = (
        f"Topic: {title}\n"
        f"Pages: {page_range}\n\n"
        f"Axes catalog:\n{_format_axes()}\n\n"
        f"Persona catalog:\n{_format_personas(recipe_options)}\n\n"
        f"Topic summary:\n{summary.strip() or '(none)'}\n\n"
        f"Human vision:\n{vision.strip() or '(none — recommend from the analysis only)'}\n\n"
        f"Prior reviewer feedback:\n{_format_feedback(feedback or empty_feedback())}\n\n"
        f"Recommend the personas this {deck_name(deck)} topic should be shown for."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def recommend(db: Session, row: DeckTopic) -> dict[str, Any]:
    options = list_recipe_options(db)
    messages = build_topic_recommendation_messages(
        row.title,
        parse_pages(row.pages_json),
        row.summary,
        row.vision,
        options,
        parse_feedback(row.feedback_json),
        deck=getattr(row, "deck", None) or BRAND_DECK,
    )
    payload = chat_json(messages)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vision_hash": row.vision_hash,
        "items": _normalize_items(payload, options),
    }


def apply_recommendations(db: Session, row: DeckTopic) -> None:
    payload = recommend(db, row)
    row.recommendations_json = json.dumps(payload, ensure_ascii=False)
    row.indexed_vision_hash = row.vision_hash
    row.recommended_at = utc_now()
    row.status = "indexed"
    touch(row)


def latest_deck_job(db: Session, asset_id: int) -> Job | None:
    if not asset_id:
        return None
    _ensure_schema()
    return (
        db.query(Job)
        .filter(Job.job_type == JOB_DECK_PREPARE, Job.asset_id == asset_id)
        .order_by(Job.id.desc())
        .first()
    )


def active_deck_job(db: Session, asset_id: int) -> Job | None:
    return (
        db.query(Job)
        .filter(
            Job.job_type == JOB_DECK_PREPARE,
            Job.asset_id == asset_id,
            Job.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(Job.id.desc())
        .first()
    )


def enqueue_deck_prepare(db: Session, asset_id: int, *, force: bool = False) -> Job:
    _ensure_schema()
    try:
        from backend.worker import reclaim_stale_jobs

        reclaim_stale_jobs(db)
    except Exception:
        pass
    existing = active_deck_job(db, asset_id)
    if existing is not None:
        if not force:
            return existing
        existing.status = "error"
        existing.error = "Superseded by a new prepare request"
        existing.stage = "Cancelled"
        existing.finished_at = utc_now()
        db.commit()
    job = Job(
        job_type=JOB_DECK_PREPARE,
        media_id=0,
        asset_id=asset_id,
        status="queued",
        stage="Queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def serialize(row: DeckTopic, job: Job | None = None) -> DeckTopicOut:
    pages = parse_pages(row.pages_json)
    rec = parse_recommendations(row.recommendations_json)
    fb = parse_feedback(row.feedback_json)
    deck = getattr(row, "deck", None) or BRAND_DECK
    return DeckTopicOut(
        id=row.id,
        deck=deck,
        sort_order=row.sort_order,
        title=row.title,
        pages=pages,
        page_items=page_items_for(pages, deck),
        summary=row.summary,
        module_ids=row.module_ids,
        vision=row.vision,
        vision_hash=row.vision_hash,
        indexed_vision_hash=row.indexed_vision_hash,
        recommended_at=row.recommended_at,
        vision_frozen=row.vision_frozen,
        status=derive_status(row, job),
        stale=is_stale(row),
        source_hash=row.source_hash,
        job_status=job.status if job else "",
        job_stage=job.stage if job else "",
        job_error=job.error if job else "",
        job_type=job.job_type if job else "",
        created_at=row.created_at,
        updated_at=row.updated_at,
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
    )


def list_deck_topics(db: Session, deck: str = BRAND_DECK) -> DeckTopicListOut:
    _ensure_schema()
    try:
        from backend.worker import reclaim_stale_jobs

        reclaim_stale_jobs(db)
    except Exception:
        pass
    asset: Asset | None = None
    try:
        asset = find_deck_asset(db, deck)
    except DeckTopicError:
        asset = None
    rows = (
        db.query(DeckTopic)
        .filter(DeckTopic.deck == deck)
        .order_by(DeckTopic.sort_order, DeckTopic.id)
        .all()
    )
    job = latest_deck_job(db, asset.id) if asset else None
    return DeckTopicListOut(
        items=[serialize(row, job) for row in rows],
        deck=deck,
        asset_id=asset.id if asset else 0,
        asset_title=asset.title if asset else deck_name(deck),
        job_status=job.status if job else "",
        job_stage=job.stage if job else "",
        job_error=job.error if job else "",
        job_type=job.job_type if job else "",
    )


def _ensure_extract(db: Session, asset: Asset, on_stage: StageCallback | None) -> None:
    if asset.extract_status == "ready" and asset.extract_json:
        return
    if not settings.runpod_api_key or not settings.runpod_endpoint_id:
        return
    from backend.extract import extract_asset

    _emit_stage(on_stage, "Extracting brand deck text…")
    try:
        extract_asset(db, asset)
    except Exception as exc:  # noqa: BLE001
        print(f"  brand deck extract skipped: {exc}", flush=True)


def _match_existing(existing: list[DeckTopic], start: int, end: int) -> DeckTopic | None:
    pages = set(range(start, end + 1))
    best: tuple[int, DeckTopic] | None = None
    for row in existing:
        overlap = len(pages & set(parse_pages(row.pages_json)))
        if overlap == 0:
            continue
        if best is None or overlap > best[0]:
            best = (overlap, row)
    if best is None:
        return None
    overlap, row = best
    if overlap * 2 < (end - start + 1):
        return None
    return row


def prepare_deck_topics(
    db: Session,
    on_stage: StageCallback | None = None,
    force: bool = False,
    deck: str = BRAND_DECK,
) -> list[DeckTopic]:
    _ensure_schema()
    asset = find_deck_asset(db, deck)
    if deck == BRAND_DECK:
        _ensure_extract(db, asset, on_stage)
    catalog = build_page_catalog(asset, deck)
    source_hash = catalog_source_hash(catalog)
    name = deck_name(deck)
    labels = deck_labels(deck, asset)
    modules_by_page = deck_modules(deck)
    existing = (
        db.query(DeckTopic)
        .filter(DeckTopic.deck == deck)
        .order_by(DeckTopic.sort_order, DeckTopic.id)
        .all()
    )
    if existing and not force and all(row.source_hash == source_hash and row.summary.strip() for row in existing):
        missing = [row for row in existing if not row.recommendations_json and not row.vision_frozen]
        if missing:
            for index, row in enumerate(missing, start=1):
                _emit_stage(on_stage, f"Matching personas {index}/{len(missing)}…")
                try:
                    apply_recommendations(db, row)
                except Exception as exc:  # noqa: BLE001
                    print(f"  topic {row.id} recommendation skipped: {exc}", flush=True)
            db.commit()
        else:
            _emit_stage(on_stage, "Topics already indexed")
        return existing

    _emit_stage(on_stage, f"Grouping {name} pages…")
    try:
        groups = group_brand_deck_pages(catalog, name)
    except Exception as exc:  # noqa: BLE001
        print(f"  {name} grouping fallback: {exc}", flush=True)
        groups = fallback_topic_groups(catalog)

    file_key = deck_file_key(db, deck, asset)
    kept_ids: list[int] = []
    created: list[DeckTopic] = []
    for index, group in enumerate(groups):
        start = group["start_page"]
        end = group["end_page"]
        pages = list(range(start, end + 1))
        match = _match_existing(existing, start, end)
        _emit_stage(on_stage, f"Analyzing topic {index + 1}/{len(groups)}…")
        try:
            summary = describe_topic_pages(start, end, catalog, file_key, deck)
        except Exception as exc:  # noqa: BLE001
            print(f"  topic {index + 1} visual analysis fallback: {exc}", flush=True)
            summary = normalize_visual_description(
                " ".join(labels.get(page, "") for page in pages) or f"Pages {start}-{end}."
            )
        module_ids = ",".join(dict.fromkeys(group["module_ids"] or [
            modules_by_page[page] for page in pages if modules_by_page.get(page)
        ]))
        if match is None:
            match = DeckTopic(deck=deck)
            db.add(match)
        match.sort_order = index
        match.title = group["title"]
        match.pages_json = json.dumps(pages)
        match.summary = summary
        match.module_ids = module_ids
        match.source_hash = source_hash
        if match.status not in {"indexed", "frozen"}:
            match.status = "ready" if summary else "draft"
        touch(match)
        db.flush()
        if summary and not match.vision_frozen:
            _emit_stage(on_stage, f"Matching personas {index + 1}/{len(groups)}…")
            try:
                apply_recommendations(db, match)
            except Exception as exc:  # noqa: BLE001
                print(f"  topic {index + 1} recommendation skipped: {exc}", flush=True)
        kept_ids.append(match.id)
        created.append(match)

    for row in existing:
        if row.id not in kept_ids:
            db.delete(row)
    db.commit()
    for row in created:
        db.refresh(row)
    return created
