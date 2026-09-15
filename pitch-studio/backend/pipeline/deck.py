"""Deck shape and slide budget.

A deck is a run of brand deck pages (see ``brand_deck.py``), so a slide carries
a page reference and a label rather than any content of its own.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

SLIDE_W = 13.333
SLIDE_H = 7.5

# Slides available per duration axis: T0 30s, T1 2m, T2 5m, T3 10m, T4 30m,
# T5 90m. T5 used to sit below T4 on the assumption that a campus walk
# replaces the deck; a 90-minute seated pitch still needs a full-length deck.
SLIDE_COUNTS = {
    "T0": 8,
    "T1": 12,
    "T2": 16,
    "T3": 22,
    "T4": 30,
    "T5": 50,
}


def slide_count_for(duration: str) -> int:
    return SLIDE_COUNTS.get(duration, 16)


class DeckSlide(BaseModel):
    layout: str = "image"
    page: int = 0
    title: str = ""
    module_id: str = ""
    image_url: str = ""


class DeckSpec(BaseModel):
    slides: list[DeckSlide] = Field(default_factory=list)


def spec_from_dict(payload: dict[str, Any]) -> DeckSpec:
    return DeckSpec.model_validate(payload)
