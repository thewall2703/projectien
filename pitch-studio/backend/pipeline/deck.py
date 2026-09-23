"""Deck shape, slide budget, and the stable slide-identity abstraction.

A deck is a run of slides. Today every slide is a page of the Masters' Union
brand deck (see ``brand_deck.py``), so a slide carries a page reference and a
label rather than any content of its own. Later stages will add *generated*
slides that are not brand-deck pages, so a slide is described here by a small
identity abstraction — :class:`PlannedSlide` — rather than by a bare page
number.

Every planned slide has a ``slide_key``: a string that is stable and unique
*within a plan*. The key, not the 1-based brand-deck ``page``, is the thing to
key notes, previews and edits on, because the same brand page can legitimately
appear more than once in a single plan and a page number alone cannot tell the
two occurrences apart.
"""

from __future__ import annotations

from math import floor
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from backend.pipeline.resolver import DURATION_MINUTES

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

# Soft speaking pace used to raise the ceiling for long pitches (T4/T5) so the
# deck can grow with real answers and evidence without padding off-recipe pages.
SLIDES_PER_MINUTE = 1.7

# A slide either comes from the brand deck or, in later stages, is generated.
BRAND_SOURCE = "brand"
GENERATED_SOURCE = "generated"

SlideSource = Literal["brand", "generated"]


def slide_count_for(duration: str) -> int:
    return SLIDE_COUNTS.get(duration, 16)


def slide_ceiling_for(duration: str) -> int:
    """Hard upper bound on deck length for ``duration``.

    At least the classic :data:`SLIDE_COUNTS` budget, and for longer pitches
    also ``floor(SLIDES_PER_MINUTE * minutes)`` so a 30-minute deck can grow
    with real brand answers and evidence instead of swapping them away.
    """
    minutes = DURATION_MINUTES.get(duration, DURATION_MINUTES.get("T2", 5))
    return max(SLIDE_COUNTS.get(duration, 16), floor(SLIDES_PER_MINUTE * minutes))


@runtime_checkable
class PlannedSlide(Protocol):
    """The stable identity of one slide in a planned deck.

    Both brand-deck pages (``source == "brand"``) and future generated slides
    (``source == "generated"``) satisfy this interface. Anything that produces
    a deck spec or renders a PPTX can work against ``PlannedSlide`` and stay
    agnostic to where the slide came from.
    """

    @property
    def slide_key(self) -> str:
        """Stable, unique-within-a-plan identity for the slide."""

    @property
    def source(self) -> str:
        """``"brand"`` or ``"generated"``."""

    @property
    def page(self) -> int | None:
        """1-based brand-deck page, or ``None`` for a generated slide."""

    @property
    def module_id(self) -> str:
        ...

    @property
    def title(self) -> str:
        ...

    @property
    def image_url(self) -> str:
        ...


class DeckSlide(BaseModel):
    """The serialised, API-facing shape of one planned slide.

    ``source``/``kind`` and ``slide_key`` are additive: older decks that were
    stored without them validate fine and fall back to the brand-deck defaults,
    so existing generated decks keep working unchanged.
    """

    layout: str = "image"
    source: str = BRAND_SOURCE
    kind: str = "image"
    slide_key: str = ""
    page: int = 0
    title: str = ""
    module_id: str = ""
    image_url: str = ""


class DeckSpec(BaseModel):
    slides: list[DeckSlide] = Field(default_factory=list)


def deck_slide_from_planned(slide: PlannedSlide) -> DeckSlide:
    """Serialise any :class:`PlannedSlide` into a :class:`DeckSlide`."""
    source = getattr(slide, "source", BRAND_SOURCE) or BRAND_SOURCE
    return DeckSlide(
        layout="image",
        source=source,
        kind="image",
        slide_key=getattr(slide, "slide_key", "") or "",
        page=getattr(slide, "page", 0) or 0,
        title=getattr(slide, "title", "") or "",
        module_id=getattr(slide, "module_id", "") or "",
        image_url=getattr(slide, "image_url", "") or "",
    )


def spec_from_dict(payload: dict[str, Any]) -> DeckSpec:
    return DeckSpec.model_validate(payload)
