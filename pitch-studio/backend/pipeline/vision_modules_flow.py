"""Vision-modules script orchestration: quotas, ranking, topics, coverage.

Used only when ``Generation.generation_mode == "vision_modules"``. Classic mode
never imports the allocation/ranking path for its own topic building.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from backend.config import settings
from backend.deck_topic_index import parse_pages
from backend.models import (
    DeckTopic,
    LockedFact,
    PrathamPassage,
    TranscriptStory,
    VisionModuleContent,
)
from backend.pipeline.brand_deck import (
    CLOSING_PAGE,
    COVER_PAGE,
    PAGE_LABELS,
    BrandSlide,
)
from backend.pipeline.deck import BRAND_SOURCE, GENERATED_SOURCE, slide_ceiling_for
from backend.pipeline.script_flow import ScriptFlowError, ScriptTopic, _split_ids
from backend.transcript_search import (
    EmbedFn,
    cosine_similarity,
    default_embed_texts,
    parse_embedding,
)
from backend.transcripts import normalize_whitespace
from backend.vision_modules import (
    VISION_MODULES,
    STRENGTH_PASSING,
    STRENGTH_STRONG,
    vision_module_for_page,
    vm_title,
)

SOURCE_WEIGHTS = {
    "pratham_passage": 0.15,
    "locked_fact": 0.10,
    "report_passage": 0.05,
    "transcript_story": 0.0,
}
STRENGTH_WEIGHTS = {
    STRENGTH_STRONG: 1.0,
    STRENGTH_PASSING: 0.55,
}
NEAR_DUP_COSINE = 0.75
PAGE_HINT_BONUS = 0.08
COSINE_RANK_WEIGHT = 0.6

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "with",
        "by", "from", "is", "are", "was", "were", "be", "as", "our", "we", "you",
        "your", "this", "that", "these", "those", "into", "over", "under", "page",
        "slide", "masters", "union", "mu",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'’-]{1,}", re.IGNORECASE)


@dataclass
class RankedContent:
    source_type: str
    source_ref: str
    text: str
    strength: str
    score: float
    page_hints: list[int] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    row_id: int = 0


@dataclass
class SlideBrief:
    page: int
    slide_key: str
    label: str
    vm_id: str
    brief: str
    narrated: bool
    ranked: list[RankedContent] = field(default_factory=list)


@dataclass
class VisionModulesPlan:
    topics: list[ScriptTopic]
    slide_briefs: list[SlideBrief]
    narrated_pages: list[int]
    quotas: dict[str, int]
    cap: int
    total_body: int
    exceeded_cap: bool
    fed_ids: dict[str, list[str]]
    pratham_by_beat: str
    vision_slide_briefs_block: str
    trace: dict[str, Any]


def normalize_generation_mode(value: str) -> str:
    text = (value or "").strip().lower()
    return text if text in {"classic", "vision_modules"} else "classic"


def allocate_quotas(module_sizes: dict[str, int], cap: int) -> dict[str, int]:
    """Per-VM narrated-slide quotas. Never trim the deck — only speech coverage."""
    modules = {vm_id: int(n) for vm_id, n in module_sizes.items() if int(n) > 0}
    if not modules:
        return {}
    total = sum(modules.values())
    if total <= cap:
        return dict(modules)
    if len(modules) > cap:
        return {vm_id: 1 for vm_id in modules}

    exact = {vm_id: n * cap / total for vm_id, n in modules.items()}
    quotas = {
        vm_id: max(1, min(n, math.ceil(exact[vm_id])))
        for vm_id, n in modules.items()
    }
    while sum(quotas.values()) > cap:
        candidates = [vm_id for vm_id, quota in quotas.items() if quota > 1]
        if not candidates:
            break
        candidates.sort(key=lambda vm_id: (-(quotas[vm_id] - exact[vm_id]), -exact[vm_id]))
        quotas[candidates[0]] -= 1
    return quotas


def _is_body_brand(slide: Any) -> bool:
    if getattr(slide, "source", BRAND_SOURCE) == GENERATED_SOURCE:
        return False
    page = getattr(slide, "page", None)
    if page is None:
        return False
    try:
        page_num = int(page)
    except (TypeError, ValueError):
        return False
    return page_num not in {COVER_PAGE, CLOSING_PAGE}


def _parse_hints(raw: str) -> list[int]:
    hints: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            page = int(part)
        except ValueError:
            continue
        if page not in hints:
            hints.append(page)
    return hints


def load_vm_content(db: Session, vm_id: str) -> list[RankedContent]:
    rows = db.query(VisionModuleContent).filter(VisionModuleContent.vm_id == vm_id).all()
    return [
        RankedContent(
            source_type=row.source_type,
            source_ref=row.source_ref,
            text=row.text or "",
            strength=row.strength or STRENGTH_PASSING,
            score=float(row.score or 0.0),
            page_hints=_parse_hints(row.page_hints or ""),
            embedding=parse_embedding(row.embedding_json or ""),
            row_id=int(row.id),
        )
        for row in rows
    ]


def filter_live_content(db: Session, items: list[RankedContent]) -> list[RankedContent]:
    """Drop index rows whose source is no longer usable; refresh locked-fact text."""
    def ids_for(source_type: str) -> set[int]:
        return {
            int(item.source_ref)
            for item in items
            if item.source_type == source_type and str(item.source_ref).isdigit()
        }

    fact_ids = ids_for("locked_fact")
    passage_ids = ids_for("pratham_passage")
    story_ids = ids_for("transcript_story")
    facts = (
        {row.id: row for row in db.query(LockedFact).filter(LockedFact.id.in_(fact_ids)).all()}
        if fact_ids
        else {}
    )
    live_passages = (
        {
            row.id
            for row in db.query(PrathamPassage.id)
            .filter(PrathamPassage.id.in_(passage_ids), PrathamPassage.usable.is_(True))
            .all()
        }
        if passage_ids
        else set()
    )
    live_stories = (
        {
            row.id
            for row in db.query(TranscriptStory.id)
            .filter(TranscriptStory.id.in_(story_ids), TranscriptStory.usable.is_(True))
            .all()
        }
        if story_ids
        else set()
    )
    kept: list[RankedContent] = []
    for item in items:
        ref = int(item.source_ref) if str(item.source_ref).isdigit() else None
        if item.source_type == "locked_fact":
            fact = facts.get(ref) if ref is not None else None
            if fact is None or (fact.status or "") != "verified":
                continue
            item.text = normalize_whitespace(f"{fact.fact}: {fact.value}. {fact.note or ''}")
        elif item.source_type == "pratham_passage":
            if ref not in live_passages:
                continue
        elif item.source_type == "transcript_story":
            if ref not in live_stories:
                continue
        kept.append(item)
    return kept


def score_slide_support(
    slide_vector: list[float],
    content: list[RankedContent],
    page: int,
) -> float:
    if not content:
        return 0.0
    best = 0.0
    for item in content:
        cos = (
            cosine_similarity(slide_vector, item.embedding)
            if slide_vector and item.embedding
            else 0.0
        )
        weight = SOURCE_WEIGHTS.get(item.source_type, 0.0)
        hint = PAGE_HINT_BONUS if page in item.page_hints else 0.0
        best = max(best, cos + weight + hint)
    return best


def select_narrated_pages(
    pages: list[int],
    quota: int,
    *,
    slide_vectors: dict[int, list[float]],
    content: list[RankedContent],
) -> list[int]:
    if quota >= len(pages):
        return list(pages)
    scored = [
        (score_slide_support(slide_vectors.get(page) or [], content, page), page)
        for page in pages
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = {page for _score, page in scored[: max(0, int(quota))]}
    return [page for page in pages if page in chosen]


def _rank_score(item: RankedContent, slide_vector: list[float], page: int) -> float:
    strength = STRENGTH_WEIGHTS.get(item.strength, STRENGTH_WEIGHTS[STRENGTH_PASSING])
    cos = (
        cosine_similarity(slide_vector, item.embedding)
        if slide_vector and item.embedding
        else 0.0
    )
    source = SOURCE_WEIGHTS.get(item.source_type, 0.0)
    hint = PAGE_HINT_BONUS if page in item.page_hints else 0.0
    return strength + COSINE_RANK_WEIGHT * cos + source + hint


def _is_pratham(item: RankedContent) -> bool:
    return item.source_type == "pratham_passage"


def filter_near_duplicates(ranked: list[RankedContent]) -> list[RankedContent]:
    kept: list[RankedContent] = []
    for item in ranked:
        drop = False
        replace_at: int | None = None
        for index, other in enumerate(kept):
            if not item.embedding or not other.embedding:
                continue
            if cosine_similarity(item.embedding, other.embedding) < NEAR_DUP_COSINE:
                continue
            prefer_item = item.score > other.score or (
                abs(item.score - other.score) < 1e-9
                and _is_pratham(item)
                and not _is_pratham(other)
            )
            if prefer_item:
                replace_at = index
            else:
                drop = True
            break
        if drop:
            continue
        if replace_at is not None:
            kept[replace_at] = item
        else:
            kept.append(item)
    return kept


def rank_content_for_slide(
    content: list[RankedContent],
    *,
    slide_vector: list[float],
    page: int,
    top_k: int | None = None,
) -> list[RankedContent]:
    top_k = settings.vision_modules_top_k if top_k is None else top_k
    scored: list[RankedContent] = []
    for item in content:
        scored.append(
            RankedContent(
                source_type=item.source_type,
                source_ref=item.source_ref,
                text=item.text,
                strength=item.strength,
                score=_rank_score(item, slide_vector, page),
                page_hints=list(item.page_hints),
                embedding=list(item.embedding),
                row_id=item.row_id,
            )
        )
    scored.sort(key=lambda row: (-row.score, 0 if _is_pratham(row) else 1, row.source_ref))
    return filter_near_duplicates(scored)[: max(1, int(top_k))]


def apply_word_cap(
    briefs: list[SlideBrief],
    *,
    word_cap: int | None = None,
) -> list[SlideBrief]:
    word_cap = settings.vision_modules_word_cap if word_cap is None else word_cap
    word_cap = max(0, int(word_cap))
    pool: list[tuple[float, int, int, RankedContent]] = []
    for b_index, brief in enumerate(briefs):
        if not brief.narrated:
            continue
        for c_index, item in enumerate(brief.ranked):
            pool.append((item.score, b_index, c_index, item))
    pool.sort(key=lambda row: (-row[0], row[1], row[2]))
    kept_keys: set[tuple[int, int]] = set()
    used = 0
    for _score, b_index, c_index, item in pool:
        words = len((item.text or "").split())
        if used and used + words > word_cap:
            continue
        kept_keys.add((b_index, c_index))
        used += words
    for b_index, brief in enumerate(briefs):
        if not brief.narrated:
            continue
        brief.ranked = [
            item
            for c_index, item in enumerate(brief.ranked)
            if (b_index, c_index) in kept_keys
        ]
    return briefs


def _source_tag(source_type: str) -> str:
    return {
        "pratham_passage": "[PRATHAM]",
        "locked_fact": "[LOCKED]",
        "report_passage": "[REPORT]",
        "transcript_story": "[STORY — unverified]",
    }.get(source_type, f"[{source_type}]")


def format_vision_slide_briefs(topics: list[ScriptTopic]) -> str:
    lines = [
        "SLIDES TO NARRATE (in order) — each needs one sentence about what's on it:",
    ]
    for topic in topics:
        narrated = list(getattr(topic, "narrated_pages", None) or [])
        briefs = list(getattr(topic, "slide_briefs", None) or [])
        if not narrated and not briefs:
            continue
        lines.append(f"### {topic.title}")
        by_page = {
            int(item.get("page")): item
            for item in briefs
            if item.get("page") is not None
        }
        for page in narrated:
            item = by_page.get(page) or {}
            label = item.get("label") or PAGE_LABELS.get(page, f"Page {page}")
            lines.append(f"- [p{page}] {label}")
            for content in item.get("ranked") or []:
                tag = _source_tag(str(content.get("source_type") or ""))
                text = normalize_whitespace(str(content.get("text") or ""))
                if text:
                    lines.append(f"  {tag} {text}")
        shown = list(getattr(topic, "shown_not_narrated", None) or [])
        if shown:
            lines.append("Shown, not narrated: " + ", ".join(f"p{page}" for page in shown))
    return "\n".join(lines)


def format_pratham_by_beat_from_briefs(topics: list[ScriptTopic]) -> str:
    blocks: list[str] = []
    for topic in topics:
        passages: list[str] = []
        for item in getattr(topic, "slide_briefs", None) or []:
            for content in item.get("ranked") or []:
                if content.get("source_type") != "pratham_passage":
                    continue
                text = normalize_whitespace(str(content.get("text") or ""))
                if text and text not in passages:
                    passages.append(text)
        if not passages:
            continue
        header = (
            f"[topic_id={topic.topic_id} | {topic.title} | "
            f"{', '.join(topic.module_ids) or '—'}]"
        )
        blocks.append("\n".join([header, *passages]))
    if not blocks:
        return ""
    return (
        "PRATHAM BY BEAT — his real speech about each beat's module "
        "(verbatim transcript, not verified facts):\n"
        + "\n\n".join(blocks)
    )


def salient_tokens(label: str) -> set[str]:
    tokens: set[str] = set()
    for match in _TOKEN_RE.findall(label or ""):
        token = match.lower().replace("’", "'")
        if token in _STOPWORDS or len(token) < 3:
            continue
        tokens.add(token)
    return tokens


def check_slide_coverage(
    script: dict[str, Any],
    narrated: list[tuple[int, str]],
) -> dict[str, Any]:
    sections = script.get("sections") or []
    full_text = " ".join(
        str(section.get("text") or "")
        for section in sections
        if isinstance(section, dict)
    ).lower()
    if script.get("cta"):
        full_text += " " + str(script["cta"]).lower()

    mentioned: list[int] = []
    missing: list[int] = []
    for page, label in narrated:
        tokens = salient_tokens(label)
        if not tokens:
            mentioned.append(page)
            continue
        section_text = ""
        for section in sections:
            if not isinstance(section, dict):
                continue
            if page in (section.get("pages") or []):
                section_text = str(section.get("text") or "").lower()
                break
        haystack = section_text or full_text
        if any(token in haystack for token in tokens):
            mentioned.append(page)
        else:
            missing.append(page)
    return {
        "narrated": len(narrated),
        "mentioned": len(mentioned),
        "missing": missing,
    }


def coverage_rewrite_note(coverage: dict[str, Any]) -> str | None:
    missing = coverage.get("missing") or []
    if not missing:
        return None
    pages = ", ".join(f"p{page}" for page in missing)
    return (
        "Please add one clear spoken sentence about each of these narrated slides "
        f"(name the person, venture, number, or thing shown): {pages}."
    )


def _ranked_dicts(items: list[RankedContent]) -> list[dict[str, Any]]:
    return [
        {
            "source_type": item.source_type,
            "source_ref": item.source_ref,
            "text": item.text,
            "strength": item.strength,
            "score": item.score,
            "row_id": item.row_id,
        }
        for item in items
    ]


def _bookend_topic(slide: Any, *, vm_id: str, sequence_set: list[str]) -> ScriptTopic:
    page = int(slide.page)
    label = slide.label or PAGE_LABELS.get(page, f"Page {page}")
    recipe = []
    modules = []
    if slide.module_id:
        modules = [slide.module_id]
        if slide.module_id in sequence_set:
            recipe = [slide.module_id]
    return ScriptTopic(
        topic_id=0,
        title=label,
        pages=[page],
        slide_keys=[slide.slide_key],
        labels=[label],
        module_ids=modules,
        recipe_modules=recipe,
        section=label,
        narrated_pages=[page],
        narrated_slide_keys=[slide.slide_key],
        shown_not_narrated=[],
        slide_briefs=[
            {
                "page": page,
                "slide_key": slide.slide_key,
                "label": label,
                "vm_id": vm_id,
                "brief": label,
                "ranked": [],
            }
        ],
    )


def _generated_topic(slide: Any, sequence_set: list[str]) -> ScriptTopic:
    module_id = getattr(slide, "module_id", "") or ""
    recipe_modules = list(getattr(slide, "recipe_modules", ()) or ())
    if not recipe_modules and module_id:
        recipe_modules = [module_id]
    recipe_modules = [mid for mid in recipe_modules if mid in sequence_set]
    title = getattr(slide, "title", "") or "Generated slide"
    summary = (getattr(slide, "summary", "") or getattr(slide, "claim", "") or "").strip()
    return ScriptTopic(
        topic_id=0,
        title=title,
        pages=[],
        slide_keys=[getattr(slide, "slide_key", "")],
        labels=[title],
        summary=summary,
        vision=(getattr(slide, "vision", "") or "").strip(),
        module_ids=list(
            getattr(slide, "recipe_modules", ()) or ([module_id] if module_id else [])
        ),
        recipe_modules=recipe_modules,
        section="Q&A" if (getattr(slide, "gap_kind", "") or "") == "objection" else "",
        narrated_pages=[],
        narrated_slide_keys=[getattr(slide, "slide_key", "")],
        shown_not_narrated=[],
        slide_briefs=[],
        _source_topic_id=-1,
        _slide_module_id=module_id,
        _generated=True,
    )


def build_vision_modules_plan(
    db: Session,
    plan: list[BrandSlide],
    sequence: list[str],
    *,
    duration: str,
    embed_fn: EmbedFn | None = None,
    top_k: int | None = None,
    word_cap: int | None = None,
) -> VisionModulesPlan:
    if not plan:
        raise ScriptFlowError("The Brand Deck plan is empty")

    body_slides = [slide for slide in plan if _is_body_brand(slide)]
    by_vm: dict[str, list[Any]] = {}
    for slide in body_slides:
        vm_id = vision_module_for_page(int(slide.page))
        if vm_id:
            by_vm.setdefault(vm_id, []).append(slide)

    ordered_vms = [vm_id for vm_id, *_rest in VISION_MODULES if vm_id in by_vm]
    sizes = {vm_id: len(by_vm[vm_id]) for vm_id in ordered_vms}
    cap = slide_ceiling_for(duration)
    total_body = sum(sizes.values())
    quotas = allocate_quotas(sizes, cap)
    exceeded = sum(quotas.values()) > cap

    embed = embed_fn or default_embed_texts
    deck_topics = db.query(DeckTopic).order_by(DeckTopic.sort_order, DeckTopic.id).all()
    page_to_topic: dict[int, Any] = {}
    for topic_row in deck_topics:
        for page_num in parse_pages(getattr(topic_row, "pages_json", "") or "[]"):
            page_to_topic.setdefault(page_num, topic_row)

    def page_brief_text(page: int) -> str:
        bits: list[str] = [PAGE_LABELS.get(page, f"Page {page}")]
        topic_row = page_to_topic.get(page)
        if topic_row is not None:
            for attr in ("summary", "vision", "title"):
                value = normalize_whitespace(getattr(topic_row, attr, "") or "")
                if value and value not in bits:
                    bits.append(value)
        return " ".join(bits)

    page_texts: dict[int, str] = {}
    for slide in body_slides:
        page = int(slide.page)
        label = slide.label or PAGE_LABELS.get(page, f"Page {page}")
        vm_id = vision_module_for_page(page) or ""
        page_texts[page] = f"{label}. {vm_title(vm_id)}. {page_brief_text(page)}".strip()
    pages_ordered = list(page_texts.keys())
    vectors_list = embed([page_texts[page] for page in pages_ordered]) if pages_ordered else []
    slide_vectors = {
        page: (vectors_list[index] if index < len(vectors_list) else [])
        for index, page in enumerate(pages_ordered)
    }

    content_by_vm = {
        vm_id: filter_live_content(db, load_vm_content(db, vm_id)) for vm_id in ordered_vms
    }
    narrated_by_vm: dict[str, list[int]] = {}
    for vm_id in ordered_vms:
        pages = [int(slide.page) for slide in by_vm[vm_id]]
        narrated_by_vm[vm_id] = select_narrated_pages(
            pages,
            quotas.get(vm_id, 0),
            slide_vectors=slide_vectors,
            content=content_by_vm.get(vm_id) or [],
        )

    sequence_set = list(dict.fromkeys(sequence))
    all_briefs: list[SlideBrief] = []
    for vm_id in ordered_vms:
        for slide in by_vm[vm_id]:
            page = int(slide.page)
            label = slide.label or PAGE_LABELS.get(page, f"Page {page}")
            is_narrated = page in set(narrated_by_vm.get(vm_id) or [])
            ranked: list[RankedContent] = []
            if is_narrated:
                ranked = rank_content_for_slide(
                    content_by_vm.get(vm_id) or [],
                    slide_vector=slide_vectors.get(page) or [],
                    page=page,
                    top_k=top_k,
                )
            all_briefs.append(
                SlideBrief(
                    page=page,
                    slide_key=slide.slide_key,
                    label=label,
                    vm_id=vm_id,
                    brief=page_texts.get(page, label),
                    narrated=is_narrated,
                    ranked=ranked,
                )
            )
    apply_word_cap(all_briefs, word_cap=word_cap)
    brief_by_page = {brief.page: brief for brief in all_briefs}

    topics: list[ScriptTopic] = []
    seen_vms: set[str] = set()
    for slide in plan:
        if getattr(slide, "source", BRAND_SOURCE) == GENERATED_SOURCE:
            topics.append(_generated_topic(slide, sequence_set))
            continue
        page = getattr(slide, "page", None)
        if page == COVER_PAGE:
            if not any(t.pages == [COVER_PAGE] for t in topics):
                topics.append(_bookend_topic(slide, vm_id="VM01", sequence_set=sequence_set))
            continue
        if page == CLOSING_PAGE:
            if not any(t.pages == [CLOSING_PAGE] for t in topics):
                topics.append(_bookend_topic(slide, vm_id="VM15", sequence_set=sequence_set))
            continue
        if not _is_body_brand(slide):
            continue
        vm_id = vision_module_for_page(int(slide.page))
        if not vm_id or vm_id in seen_vms:
            continue
        seen_vms.add(vm_id)
        slides = by_vm[vm_id]
        pages = [int(s.page) for s in slides]
        narrated = list(narrated_by_vm.get(vm_id) or [])
        shown = [p for p in pages if p not in set(narrated)]
        labels = [
            s.label or PAGE_LABELS.get(int(s.page), f"Page {s.page}") for s in slides
        ]
        recipe_modules: list[str] = []
        module_ids: list[str] = []
        summary_parts: list[str] = []
        vision_parts: list[str] = []
        for s in slides:
            if s.module_id and s.module_id not in module_ids:
                module_ids.append(s.module_id)
            if s.module_id and s.module_id in sequence_set and s.module_id not in recipe_modules:
                recipe_modules.append(s.module_id)
            topic_row = page_to_topic.get(int(s.page))
            if topic_row is None:
                continue
            for mid in _split_ids(getattr(topic_row, "module_ids", "") or ""):
                if mid not in module_ids:
                    module_ids.append(mid)
            for attr, bucket in (("summary", summary_parts), ("vision", vision_parts)):
                extra = (getattr(topic_row, attr, "") or "").strip()
                if extra and extra not in bucket:
                    bucket.append(extra)
        slide_brief_dicts = []
        for page_num in narrated:
            live = brief_by_page.get(page_num)
            if live is None:
                continue
            slide_brief_dicts.append(
                {
                    "page": page_num,
                    "slide_key": live.slide_key,
                    "label": live.label,
                    "vm_id": vm_id,
                    "brief": live.brief,
                    "ranked": _ranked_dicts(live.ranked),
                }
            )
        topics.append(
            ScriptTopic(
                topic_id=0,
                title=vm_title(vm_id),
                pages=pages,
                slide_keys=[s.slide_key for s in slides],
                labels=labels,
                summary=" ".join(summary_parts),
                vision=" ".join(vision_parts),
                module_ids=module_ids,
                recipe_modules=recipe_modules,
                section=vm_title(vm_id),
                narrated_pages=list(narrated),
                narrated_slide_keys=[
                    s.slide_key for s in slides if int(s.page) in set(narrated)
                ],
                shown_not_narrated=shown,
                slide_briefs=slide_brief_dicts,
            )
        )

    for index, topic in enumerate(topics, start=1):
        topic.topic_id = index

    narrated_pages = [
        brief.page for brief in all_briefs if brief.narrated
    ]
    fed_ids: dict[str, list[str]] = {
        "pratham_passage": [],
        "locked_fact": [],
        "report_passage": [],
        "transcript_story": [],
    }
    for brief in all_briefs:
        if not brief.narrated:
            continue
        for item in brief.ranked:
            bucket = fed_ids.setdefault(item.source_type, [])
            if item.source_ref not in bucket:
                bucket.append(item.source_ref)

    block = format_vision_slide_briefs(topics)
    pratham_block = format_pratham_by_beat_from_briefs(topics)
    trace = {
        "mode": "vision_modules",
        "cap": cap,
        "total": total_body,
        "quotas": quotas,
        "narrated_pages": narrated_pages,
        "exceeded_cap": exceeded,
        "fed_content_ids": fed_ids,
    }
    return VisionModulesPlan(
        topics=topics,
        slide_briefs=all_briefs,
        narrated_pages=narrated_pages,
        quotas=quotas,
        cap=cap,
        total_body=total_body,
        exceeded_cap=exceeded,
        fed_ids=fed_ids,
        pratham_by_beat=pratham_block,
        vision_slide_briefs_block=block,
        trace=trace,
    )
