"""Map a planned Brand Deck to ordered spoken topics."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from backend.deck_topic_index import parse_pages
from backend.models import DeckTopic
from backend.pipeline.brand_deck import BrandSlide, PAGE_LABELS
from backend.pipeline.deck import BRAND_SOURCE, GENERATED_SOURCE


class ScriptFlowError(RuntimeError):
    pass


@dataclass
class ScriptTopic:
    topic_id: int
    title: str
    pages: list[int] = field(default_factory=list)
    slide_keys: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    summary: str = ""
    vision: str = ""
    module_ids: list[str] = field(default_factory=list)
    recipe_modules: list[str] = field(default_factory=list)
    _source_topic_id: int = field(default=0, repr=False)
    _slide_module_id: str = field(default="", repr=False)

    def to_prompt_dict(self) -> dict[str, Any]:
        if not self.pages:
            page_range = ""  # a generated slide has no brand page
        elif len(self.pages) == 1:
            page_range = f"p{self.pages[0]}"
        else:
            page_range = f"p{self.pages[0]}–{self.pages[-1]}"
        return {
            "topic_id": self.topic_id,
            "title": self.title,
            "pages": self.pages,
            "slide_keys": self.slide_keys,
            "page_range": page_range,
            "labels": self.labels,
            "summary": self.summary,
            "vision": self.vision,
            "module_ids": self.module_ids,
            "recipe_modules": self.recipe_modules,
        }


def _split_ids(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _is_generated(slide: Any) -> bool:
    """A Stage 3 generated placeholder — no brand page, ``source`` generated."""
    return getattr(slide, "source", BRAND_SOURCE) == GENERATED_SOURCE or getattr(slide, "page", 0) is None


def _generated_topic(slide: Any, topic_id: int, sequence_set: list[str]) -> ScriptTopic:
    """Build a spoken beat straight from a generated placeholder's claim metadata.

    A generated slide carries no brand page of its own, so it never touches the
    page-based topic index: its beat is synthesised from the placeholder itself.
    ``pages`` starts empty and may later gain the placeholder's evidence page when
    that brand slide immediately follows it in the plan.
    """
    module_id = getattr(slide, "module_id", "") or ""
    recipe_modules = list(getattr(slide, "recipe_modules", ()) or ())
    if not recipe_modules and module_id:
        recipe_modules = [module_id]
    recipe_modules = [mid for mid in recipe_modules if mid in sequence_set]
    title = getattr(slide, "title", "") or "Generated slide"
    summary = (getattr(slide, "summary", "") or getattr(slide, "claim", "") or "").strip()
    return ScriptTopic(
        topic_id=topic_id,
        title=title,
        pages=[],
        slide_keys=[getattr(slide, "slide_key", "")],
        labels=[title],
        summary=summary,
        vision=(getattr(slide, "vision", "") or "").strip(),
        module_ids=list(getattr(slide, "recipe_modules", ()) or ([module_id] if module_id else [])),
        recipe_modules=recipe_modules,
        # A unique negative source id guarantees a generated slide is always its
        # own beat and never collapses into an adjacent brand topic.
        _source_topic_id=-topic_id,
        _slide_module_id=module_id,
    )


def _page_map(topics: list[Any]) -> dict[int, Any]:
    mapping: dict[int, Any] = {}
    for topic in topics:
        for page in parse_pages(getattr(topic, "pages_json", "") or "[]"):
            mapping[page] = topic
    return mapping


def build_script_topics(
    plan: list[BrandSlide],
    topics: list[Any],
    sequence: list[str],
) -> list[ScriptTopic]:
    if not topics:
        raise ScriptFlowError("Prepare Brand Deck topics before generating a pitch")
    if not plan:
        raise ScriptFlowError("The Brand Deck plan is empty")
    mapping = _page_map(topics)
    missing = [slide.page for slide in plan if not _is_generated(slide) and slide.page not in mapping]
    if missing:
        raise ScriptFlowError(
            "Selected Brand Deck pages are missing from the topic index: "
            + ", ".join(f"p{page}" for page in missing)
        )

    sequence_set = list(dict.fromkeys(sequence))
    selected_modules_by_topic: dict[int, set[str]] = {}
    for slide in plan:
        if _is_generated(slide):
            continue
        source_topic_id = int(mapping[slide.page].id)
        selected_modules_by_topic.setdefault(source_topic_id, set()).add(slide.module_id)
    flow: list[ScriptTopic] = []
    current: ScriptTopic | None = None
    pending_evidence_page: int | None = None
    for slide in plan:
        if _is_generated(slide):
            current = _generated_topic(slide, len(flow) + 1, sequence_set)
            flow.append(current)
            pending_evidence_page = getattr(slide, "evidence_page", None)
            continue
        # Evidence brand page that Stage 3 planted immediately after a generated
        # placeholder stays in that generated beat — never starts a new topic.
        if (
            current is not None
            and pending_evidence_page is not None
            and getattr(slide, "page", None) == pending_evidence_page
        ):
            current.pages.append(slide.page)
            current.slide_keys.append(slide.slide_key)
            current.labels.append(slide.label or PAGE_LABELS.get(slide.page, f"Page {slide.page}"))
            if (
                slide.module_id
                and slide.module_id in sequence_set
                and slide.module_id not in current.recipe_modules
            ):
                current.recipe_modules.append(slide.module_id)
            pending_evidence_page = None
            continue
        pending_evidence_page = None
        topic = mapping[slide.page]
        source_topic_id = int(topic.id)
        # Deck-topic rows are intentionally broad visual chapters and can span
        # several recipe modules (for example Origin may include both M01 and
        # the M09 Gurugram page). Do not turn those different arguments into
        # one spoken beat: that lets a later, vivid slide hijack the opening.
        # Collapse only adjacent pages that share both chapter and module.
        if (
            current is not None
            and current._source_topic_id == source_topic_id
            and current._slide_module_id == slide.module_id
        ):
            current.pages.append(slide.page)
            current.slide_keys.append(slide.slide_key)
            current.labels.append(slide.label or PAGE_LABELS.get(slide.page, f"Page {slide.page}"))
            if slide.module_id and slide.module_id in sequence_set and slide.module_id not in current.recipe_modules:
                current.recipe_modules.append(slide.module_id)
            continue
        module_ids = _split_ids(getattr(topic, "module_ids", ""))
        # The source DeckTopic may span several modules. Feeding all of them
        # into every split beat leaks later arguments into earlier slides
        # (notably M09 campus copy into the cover/origin opening).
        recipe_modules = [slide.module_id] if slide.module_id in sequence_set else []
        source_title = (getattr(topic, "title", "") or "").strip()
        label = slide.label or PAGE_LABELS.get(slide.page, f"Page {slide.page}")
        # Once a broad source chapter is split, give each spoken beat a title
        # that describes its actual slides instead of repeating a misleading
        # chapter title such as "Origin" over the Gurugram beat.
        beat_title = (
            label
            if len(selected_modules_by_topic.get(source_topic_id, set())) > 1
            else source_title or f"Topic {source_topic_id}"
        )
        current = ScriptTopic(
            # This is a beat id, not the database DeckTopic id. Splitting a
            # broad visual chapter must still produce unique section ids.
            topic_id=len(flow) + 1,
            title=beat_title,
            pages=[slide.page],
            slide_keys=[slide.slide_key],
            labels=[label],
            summary=(getattr(topic, "summary", "") or "").strip(),
            vision=(getattr(topic, "vision", "") or "").strip(),
            module_ids=module_ids,
            recipe_modules=recipe_modules,
            _source_topic_id=source_topic_id,
            _slide_module_id=slide.module_id,
        )
        flow.append(current)
    return flow


def load_script_topics(db: Session, plan: list[BrandSlide], sequence: list[str]) -> list[ScriptTopic]:
    rows = db.query(DeckTopic).order_by(DeckTopic.sort_order, DeckTopic.id).all()
    return build_script_topics(plan, rows, sequence)


def reconcile_realized_slide_mapping(
    script: dict[str, Any],
    topics: list[ScriptTopic],
    planned: list[Any],
    realized: list[Any],
) -> tuple[dict[str, Any], list[ScriptTopic]]:
    """Replace Stage 3 slide references with their Stage 4 identities.

    Topic beats are built before generated placeholders are realised. Stage 4
    preserves plan order but replaces each placeholder with either a
    content-addressed generated slide, its displaced brand page, or ``None``
    when an *inserted* placeholder could not be realised (no page to restore).
    Reconcile by plan position, while verifying the original keys, so duplicate
    pages and generated keys cannot accidentally attach a beat to another slide.
    Dropped (``None``) entries are removed from the topic's ``slide_keys`` /
    ``pages``; section text is kept even when no slides remain.
    """
    if len(planned) != len(realized):
        raise ScriptFlowError(
            "Cannot reconcile script topics: the realised deck changed slide count"
        )

    for planned_slide, realized_slide in zip(planned, realized):
        if realized_slide is None:
            if not _is_generated(planned_slide):
                raise ScriptFlowError(
                    "Cannot reconcile script topics: a non-generated slide was dropped"
                )
            continue
        if not (getattr(planned_slide, "slide_key", "") and getattr(realized_slide, "slide_key", "")):
            raise ScriptFlowError(
                "Cannot reconcile script topics: every planned slide needs a stable key"
            )

    planned_keys = [str(getattr(slide, "slide_key", "") or "") for slide in planned]
    if not all(planned_keys):
        raise ScriptFlowError(
            "Cannot reconcile script topics: every planned slide needs a stable key"
        )

    references = [key for topic in topics for key in topic.slide_keys]
    if references != planned_keys:
        raise ScriptFlowError(
            "Cannot reconcile script topics: topic slide order no longer matches the plan"
        )

    reconciled_topics = deepcopy(topics)
    position = 0
    for topic in reconciled_topics:
        count = len(topic.slide_keys)
        final_slides = [slide for slide in realized[position : position + count] if slide is not None]
        topic.slide_keys = [
            str(getattr(slide, "slide_key", "") or "") for slide in final_slides
        ]
        topic.pages = [
            int(page)
            for slide in final_slides
            if (page := getattr(slide, "page", None)) is not None and int(page) > 0
        ]
        position += count

    updated_script = deepcopy(script)
    topics_by_id = {topic.topic_id: topic for topic in reconciled_topics}
    for section in updated_script.get("sections") or []:
        try:
            topic_id = int(section.get("topic_id") or 0)
        except (TypeError, ValueError):
            continue
        topic = topics_by_id.get(topic_id)
        if topic is None:
            continue
        section["slide_keys"] = list(topic.slide_keys)
        section["pages"] = list(topic.pages)

    return updated_script, reconciled_topics


def notes_by_page(script: dict[str, Any]) -> dict[int, str]:
    notes: dict[int, str] = {}
    for section in script.get("sections") or []:
        text = str(section.get("text") or "").strip()
        if not text:
            continue
        for raw in section.get("pages") or []:
            try:
                page = int(raw)
            except (TypeError, ValueError):
                continue
            if page > 0:
                notes[page] = text
    return notes


def notes_by_slide_key(script: dict[str, Any], plan: list[Any]) -> dict[str, str]:
    """Map each planned slide's stable key to its spoken note.

    Reconciled sections carry final slide keys and therefore support generated
    slides directly. Legacy sections that only reference pages still work:
    each page occurrence is consumed in plan order so repeated brand pages land
    on distinct keys instead of overwriting one shared page entry.
    """
    plan_keys = {
        str(getattr(slide, "slide_key", "") or "")
        for slide in plan
        if getattr(slide, "slide_key", "")
    }
    occurrences: dict[int, list[str]] = {}
    for slide in plan:
        page = getattr(slide, "page", None)
        if page:
            occurrences.setdefault(int(page), []).append(slide.slide_key)

    cursor: dict[int, int] = {}
    notes: dict[str, str] = {}
    for section in script.get("sections") or []:
        text = str(section.get("text") or "").strip()
        if not text:
            continue
        section_keys = [
            str(key)
            for key in section.get("slide_keys") or []
            if str(key) in plan_keys
        ]
        if section_keys:
            for key in section_keys:
                notes[key] = text
            continue
        for raw in section.get("pages") or []:
            try:
                page = int(raw)
            except (TypeError, ValueError):
                continue
            keys = occurrences.get(page)
            if not keys:
                continue
            index = cursor.get(page, 0)
            # Once every occurrence has a note, extra references fall on the
            # last occurrence rather than being dropped.
            key = keys[index] if index < len(keys) else keys[-1]
            notes[key] = text
            if index < len(keys):
                cursor[page] = index + 1
    return notes
