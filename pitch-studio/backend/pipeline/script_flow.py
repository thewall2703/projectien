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
    flow: list[ScriptTopic] = []
    current: ScriptTopic | None = None
    for slide in plan:
        topic = mapping[slide.page]
        topic_id = int(topic.id)
        if current is not None and current.topic_id == topic_id:
            current.pages.append(slide.page)
            current.labels.append(slide.label or PAGE_LABELS.get(slide.page, f"Page {slide.page}"))
            if slide.module_id and slide.module_id in sequence_set and slide.module_id not in current.recipe_modules:
                current.recipe_modules.append(slide.module_id)
            continue
        module_ids = _split_ids(getattr(topic, "module_ids", ""))
        recipe_modules = [module_id for module_id in sequence_set if module_id in module_ids]
        if slide.module_id and slide.module_id in sequence_set and slide.module_id not in recipe_modules:
            recipe_modules.append(slide.module_id)
        current = ScriptTopic(
            topic_id=topic_id,
            title=(getattr(topic, "title", "") or "").strip() or f"Topic {topic_id}",
            pages=[slide.page],
            labels=[slide.label or PAGE_LABELS.get(slide.page, f"Page {slide.page}")],
            summary=(getattr(topic, "summary", "") or "").strip(),
            vision=(getattr(topic, "vision", "") or "").strip(),
            module_ids=module_ids,
            recipe_modules=recipe_modules,
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
