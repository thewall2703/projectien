"""Map a planned Brand Deck to ordered spoken topics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from backend.deck_topic_index import parse_pages
from backend.models import DeckTopic
from backend.pipeline.brand_deck import BrandSlide, PAGE_LABELS


class ScriptFlowError(RuntimeError):
    pass


@dataclass
class ScriptTopic:
    topic_id: int
    title: str
    pages: list[int] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    summary: str = ""
    vision: str = ""
    module_ids: list[str] = field(default_factory=list)
    recipe_modules: list[str] = field(default_factory=list)
    _source_topic_id: int = field(default=0, repr=False)
    _slide_module_id: str = field(default="", repr=False)

    def to_prompt_dict(self) -> dict[str, Any]:
        page_range = f"p{self.pages[0]}" if len(self.pages) == 1 else f"p{self.pages[0]}–{self.pages[-1]}"
        return {
            "topic_id": self.topic_id,
            "title": self.title,
            "pages": self.pages,
            "page_range": page_range,
            "labels": self.labels,
            "summary": self.summary,
            "vision": self.vision,
            "module_ids": self.module_ids,
            "recipe_modules": self.recipe_modules,
        }


def _split_ids(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


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
    missing = [slide.page for slide in plan if slide.page not in mapping]
    if missing:
        raise ScriptFlowError(
            "Selected Brand Deck pages are missing from the topic index: "
            + ", ".join(f"p{page}" for page in missing)
        )

    sequence_set = list(dict.fromkeys(sequence))
    selected_modules_by_topic: dict[int, set[str]] = {}
    for slide in plan:
        source_topic_id = int(mapping[slide.page].id)
        selected_modules_by_topic.setdefault(source_topic_id, set()).add(slide.module_id)
    flow: list[ScriptTopic] = []
    current: ScriptTopic | None = None
    for slide in plan:
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
