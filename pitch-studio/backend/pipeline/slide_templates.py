"""Concrete slide templates built on :mod:`slide_render`.

Each template is a factory returning a :class:`SlideManifest`. The only template
so far is the **section divider**, in light and dark variants, reconstructed
from the brand deck's real divider pages (p19, p52). Its furniture — grid,
header/footer rails, and the sweeping accent gradient — is the flattened,
text-and-photo-free SVG under ``assets/slide-templates`` (see
``section-divider-{variant}.svg``). Only the title/subtitle/branding text is
live and filled at generation time.

Known geometry, measured from the source pages at 1920x1080:

* header/footer rails at ``y=116.75`` and ``y=959.75`` (``#A3A3A3`` @ 0.5px),
* the accent gradient ``#E38330 -> #F7D344 -> #39B6D8``,
* branding "Learn by Doing" (top-left) and "mastersunion.org" (top-right),
* the section title anchored bottom-left with an optional subtitle beneath it.

The title renders in the brand **sans** (Galano Alt) with ``*emphasis*`` runs
falling to the italic **serif** token. Because the licensed display serif is
not available yet, that token resolves to a clearly-provisional fallback in one
place (:data:`slide_render.SERIF_FALLBACK_STACK`); p52's all-serif title is
expressed simply as a fully-emphasised string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from backend.pipeline.slide_render import (
    GradientLayer,
    ListSlot,
    PhotoSlot,
    Rect,
    SlideManifest,
    SvgLayer,
    TextSlot,
)

Variant = Literal["light", "dark", "blue"]

SECTION_DIVIDER_TEMPLATE_ID = "section-divider"

# Bump when a template's furniture, geometry, slots or copy conventions change,
# so every cached generated render built on the old template is invalidated.
SECTION_DIVIDER_TEMPLATE_VERSION = "1"

# Brand furniture strings shown on every divider unless overridden.
DEFAULT_BRANDING_LEFT = "Learn by Doing"
DEFAULT_BRANDING_RIGHT = "mastersunion.org"

_PALETTE = {
    "light": {
        "background": "#FFFFFF",
        "title": "#111111",
        "subtitle": "#3A3A3A",
        "branding": "#1A1A1A",
    },
    "dark": {
        "background": "#0E0E10",
        "title": "#FFFFFF",
        "subtitle": "#D8D8D8",
        "branding": "#EDEDED",
    },
}


def section_divider_manifest(variant: Variant = "light") -> SlideManifest:
    """Build the section-divider manifest for ``variant`` ("light"|"dark")."""
    if variant not in _PALETTE:
        raise ValueError(f"unknown section-divider variant: {variant!r}")
    colors = _PALETTE[variant]

    layers = (
        SvgLayer(
            name="furniture",
            asset=f"section-divider-{variant}.svg",
            rect=Rect(0, 0, 1920, 1080),
            opacity=1.0,
        ),
    )

    text_slots = (
        # Top branding rail. Fixed size, never auto-fit; overflow still rejected.
        TextSlot(
            name="branding_left",
            rect=Rect(64, 44, 520, 40),
            font="sans",
            font_size=18,
            min_font_size=18,
            weight=500,
            tracking_em=0.01,
            line_height=1.0,
            color=colors["branding"],
            align="left",
            valign="middle",
            autofit=False,
            char_budget=48,
        ),
        TextSlot(
            name="branding_right",
            rect=Rect(1336, 44, 520, 40),
            font="sans",
            font_size=18,
            min_font_size=18,
            weight=500,
            tracking_em=0.01,
            line_height=1.0,
            color=colors["branding"],
            align="right",
            valign="middle",
            autofit=False,
            char_budget=48,
        ),
        # Section title, anchored bottom-left. Sans by default; *emphasis*
        # renders italic-serif. Auto-fits down to a floor, then rejects.
        TextSlot(
            name="title",
            rect=Rect(96, 300, 1216, 232),
            font="sans",
            font_size=104,
            min_font_size=44,
            weight=700,
            tracking_em=-0.01,
            line_height=1.02,
            color=colors["title"],
            align="left",
            valign="bottom",
            char_budget=120,
            required=True,
        ),
        # Optional subtitle directly beneath the title.
        TextSlot(
            name="subtitle",
            rect=Rect(100, 548, 900, 60),
            font="sans",
            font_size=34,
            min_font_size=20,
            weight=500,
            tracking_em=0.0,
            line_height=1.12,
            color=colors["subtitle"],
            align="left",
            valign="top",
            char_budget=90,
        ),
    )

    return SlideManifest(
        template_id=f"{SECTION_DIVIDER_TEMPLATE_ID}-{variant}",
        layers=layers,
        text_slots=text_slots,
        background=colors["background"],
    )


def section_divider_values(
    title: str,
    subtitle: str = "",
    *,
    branding_left: str = DEFAULT_BRANDING_LEFT,
    branding_right: str = DEFAULT_BRANDING_RIGHT,
) -> dict[str, str]:
    """Helper to assemble the value dict a section divider expects."""
    return {
        "branding_left": branding_left,
        "branding_right": branding_right,
        "title": title,
        "subtitle": subtitle,
    }


# ---------------------------------------------------------------------------
# Stage 6 templates: stat (p58), campus photo (p74-82), programme list (p84-87)
# ---------------------------------------------------------------------------

STAT_TEMPLATE_ID = "stat"
STAT_TEMPLATE_VERSION = "1"
CAMPUS_TEMPLATE_ID = "campus-photo"
CAMPUS_TEMPLATE_VERSION = "1"
PROGRAMME_LIST_TEMPLATE_ID = "programme-list"
PROGRAMME_LIST_TEMPLATE_VERSION = "1"

# How many supporting stat rows the stat template carries beside the hero stat.
# p58 lists five figures in the right column (highest / average / median CTC,
# average international CTC, international & remote placements).
STAT_ROW_COUNT = 5

_STAT_PALETTE = {
    "dark": {
        "background": "#0E0E10",
        "title": "#FFFFFF",
        "subtitle": "#CFCFCF",
        "hero": "#FFFFFF",
        "hero_label": "#D8D8D8",
        "value": "#F2ECD6",
        "label": "#E8E8E8",
        "footnote": "#C9A24B",
        "branding": "#EDEDED",
    },
    "light": {
        "background": "#FFFFFF",
        "title": "#111111",
        "subtitle": "#3A3A3A",
        "hero": "#111111",
        "hero_label": "#3A3A3A",
        "value": "#8A6D1F",
        "label": "#222222",
        "footnote": "#8A6D1F",
        "branding": "#1A1A1A",
    },
}


def stat_manifest(variant: Variant = "dark") -> SlideManifest:
    """Build the stat-template manifest (modelled on p58, not p22).

    A hero stat bottom-left plus up to :data:`STAT_ROW_COUNT` supporting rows in
    the right column. Stat values render in the italic **serif** token (the
    brand's accent for figures); everything else is Galano sans. The furniture
    (dark/light background, grid, warm accent curves) is the flattened SVG.
    """
    if variant not in _STAT_PALETTE:
        raise ValueError(f"unknown stat variant: {variant!r}")
    c = _STAT_PALETTE[variant]
    layers = (
        SvgLayer(name="furniture", asset=f"stat-{variant}.svg", rect=Rect(0, 0, 1920, 1080), z=0),
    )
    slots: list[TextSlot] = [
        TextSlot(
            name="branding_left", rect=Rect(64, 44, 520, 40), font="sans", font_size=18,
            min_font_size=18, weight=500, tracking_em=0.01, line_height=1.0,
            color=c["branding"], align="left", valign="middle", autofit=False, char_budget=48,
        ),
        TextSlot(
            name="branding_right", rect=Rect(1336, 44, 520, 40), font="sans", font_size=18,
            min_font_size=18, weight=500, tracking_em=0.01, line_height=1.0,
            color=c["branding"], align="right", valign="middle", autofit=False, char_budget=48,
        ),
        TextSlot(
            name="title", rect=Rect(96, 74, 620, 176), font="sans", font_size=84,
            min_font_size=42, weight=700, tracking_em=-0.01, line_height=1.02,
            color=c["title"], align="left", valign="top", char_budget=48, required=True,
        ),
        TextSlot(
            name="subtitle", rect=Rect(96, 252, 470, 120), font="sans", font_size=30,
            min_font_size=18, weight=500, tracking_em=0.0, line_height=1.2,
            color=c["subtitle"], align="left", valign="top", char_budget=110,
        ),
        TextSlot(
            name="hero_value", rect=Rect(88, 700, 560, 240), font="sans", font_size=200,
            min_font_size=90, weight=800, tracking_em=-0.02, line_height=0.95,
            color=c["hero"], align="left", valign="bottom", char_budget=12, required=True,
        ),
        TextSlot(
            name="hero_label", rect=Rect(96, 946, 540, 96), font="sans", font_size=26,
            min_font_size=16, weight=600, tracking_em=0.0, line_height=1.15,
            color=c["hero_label"], align="left", valign="top", char_budget=64, required=True,
        ),
        TextSlot(
            name="footnote", rect=Rect(1240, 998, 616, 44), font="sans", font_size=18,
            min_font_size=13, weight=500, tracking_em=0.0, line_height=1.1,
            color=c["footnote"], align="right", valign="middle", char_budget=64,
        ),
    ]
    # Supporting stat rows: serif-italic value (right column) + sans label.
    row_top = 84
    row_step = 92
    for i in range(1, STAT_ROW_COUNT + 1):
        top = row_top + (i - 1) * row_step
        slots.append(
            TextSlot(
                name=f"stat{i}_value", rect=Rect(548, top, 228, 78), font="serif",
                font_size=56, min_font_size=28, weight=500, italic=True, tracking_em=0.0,
                line_height=1.0, color=c["value"], align="right", valign="middle", char_budget=12,
            )
        )
        slots.append(
            TextSlot(
                name=f"stat{i}_label", rect=Rect(792, top - 4, 270, 86), font="sans",
                font_size=25, min_font_size=15, weight=600, tracking_em=0.0, line_height=1.12,
                color=c["label"], align="left", valign="middle", char_budget=54,
            )
        )
    return SlideManifest(
        template_id=f"{STAT_TEMPLATE_ID}-{variant}",
        layers=layers,
        text_slots=tuple(slots),
        background=c["background"],
    )


def campus_photo_manifest() -> SlideManifest:
    """Build the campus photo-and-headline manifest (modelled on p74-82).

    A required full-bleed approved photo, a dark top scrim for legibility, the
    brand wave/registration furniture, an italic-serif headline and a short sans
    caption. The photo layer is filled at render time from approved media bytes
    with deterministic crop metadata (see :meth:`PhotoSlot.crop_metadata`).
    """
    layers = (
        SvgLayer(name="furniture", asset="campus-photo-dark.svg", rect=Rect(0, 0, 1920, 1080), z=25),
    )
    photos = (
        PhotoSlot(
            name="hero_photo", rect=Rect(0, 0, 1920, 1080), fit="cover",
            focus_x=0.5, focus_y=0.5, background="#0B0B0C", required=True, z=5,
        ),
    )
    gradients = (
        GradientLayer(
            name="top_scrim", rect=Rect(0, 0, 1920, 470),
            css="linear-gradient(180deg, rgba(0,0,0,0.82) 0%, rgba(0,0,0,0.34) 40%, rgba(0,0,0,0) 100%)",
            z=20,
        ),
    )
    slots = (
        TextSlot(
            name="branding_left", rect=Rect(64, 44, 520, 40), font="sans", font_size=18,
            min_font_size=18, weight=500, tracking_em=0.01, line_height=1.0,
            color="#F2F2F2", align="left", valign="middle", autofit=False, char_budget=48,
        ),
        TextSlot(
            name="branding_right", rect=Rect(1336, 44, 520, 40), font="sans", font_size=18,
            min_font_size=18, weight=500, tracking_em=0.01, line_height=1.0,
            color="#F2F2F2", align="right", valign="middle", autofit=False, char_budget=48,
        ),
        TextSlot(
            name="headline", rect=Rect(60, 84, 780, 128), font="serif", font_size=98,
            min_font_size=48, weight=400, italic=True, tracking_em=-0.005, line_height=1.0,
            color="#EDC65C", align="left", valign="middle", char_budget=32, required=True,
        ),
        TextSlot(
            name="caption", rect=Rect(1140, 92, 716, 128), font="sans", font_size=30,
            min_font_size=18, weight=500, tracking_em=0.0, line_height=1.22,
            color="#F4F4F4", align="left", valign="top", char_budget=80,
        ),
    )
    return SlideManifest(
        template_id=f"{CAMPUS_TEMPLATE_ID}-dark",
        layers=layers,
        photo_slots=photos,
        gradient_layers=gradients,
        text_slots=slots,
        background="#0B0B0C",
    )


def programme_list_manifest(variant: Variant = "light") -> SlideManifest:
    """Build the programme-list manifest (modelled on p84-87).

    A light card holds a bold title, an italic-serif programme-type line and a
    bounded ring-bulleted list; two optional tilted photo cards sit on the
    right and are only drawn when approved media is supplied (otherwise the card
    degrades cleanly to a text-only slide).
    """
    tint: Variant = "blue" if variant == "blue" else "light"
    layers = (
        SvgLayer(name="furniture", asset=f"programme-list-{tint}.svg", rect=Rect(0, 0, 1920, 1080), z=0),
    )
    photos = (
        PhotoSlot(
            name="photo_1", rect=Rect(548, 72, 452, 258), fit="cover", focus_x=0.5, focus_y=0.5,
            rotation_deg=-4.0, frame=True, frame_color="#FFFFFF", frame_width=12.0,
            shadow=True, background="#DDDDDD", z=12,
        ),
        PhotoSlot(
            name="photo_2", rect=Rect(596, 300, 452, 258), fit="cover", focus_x=0.5, focus_y=0.5,
            rotation_deg=3.5, frame=True, frame_color="#FFFFFF", frame_width=12.0,
            shadow=True, background="#DDDDDD", z=13,
        ),
    )
    slots = (
        TextSlot(
            name="branding_left", rect=Rect(64, 44, 520, 40), font="sans", font_size=18,
            min_font_size=18, weight=500, tracking_em=0.01, line_height=1.0,
            color="#1A1A1A", align="left", valign="middle", autofit=False, char_budget=48,
        ),
        TextSlot(
            name="branding_right", rect=Rect(1336, 44, 520, 40), font="sans", font_size=18,
            min_font_size=18, weight=500, tracking_em=0.01, line_height=1.0,
            color="#1A1A1A", align="right", valign="middle", autofit=False, char_budget=48,
        ),
        TextSlot(
            name="title", rect=Rect(110, 92, 330, 60), font="sans", font_size=44,
            min_font_size=26, weight=700, tracking_em=-0.005, line_height=1.0,
            color="#1A1A1A", align="left", valign="middle", char_budget=26, required=True,
        ),
        TextSlot(
            name="programme_type", rect=Rect(110, 150, 330, 52), font="serif", font_size=38,
            min_font_size=22, weight=400, italic=True, tracking_em=0.0, line_height=1.0,
            color="#333333", align="left", valign="middle", char_budget=24,
        ),
    )
    lists = (
        ListSlot(
            name="items", rect=Rect(106, 246, 336, 252), max_items=6, item_height=42,
            font="sans", font_size=24, min_font_size=15, weight=400, line_height=1.1,
            color="#1F1F1F", bullet="ring", bullet_color="#141414", bullet_size=18,
            text_indent=40, divider=True, divider_color="#DADAD6",
            char_budget_per_item=48, required=True,
        ),
    )
    return SlideManifest(
        template_id=f"{PROGRAMME_LIST_TEMPLATE_ID}-{variant}",
        layers=layers,
        photo_slots=photos,
        text_slots=slots,
        list_slots=lists,
        background="#FFFFFF",
    )


# ---------------------------------------------------------------------------
# Template registry — what Stage 4 (slot-fill / gate / render) fills against
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotBudget:
    """The *exact* per-slot copy budget a filled value may not exceed.

    ``max_words``/``max_chars`` are hard gates checked before any render: the
    slot's ``char_budget`` on the manifest is the renderer's own floor, and the
    word budget keeps the copy at section-divider scale (a headline, never a
    paragraph). They are template-owned, not model-negotiable.
    """

    max_words: int
    max_chars: int
    # Optional lower bounds (0 = no floor). The campus caption uses these to
    # keep copy in the brand's 7-11 word / 43-72 char band, not a fragment.
    min_words: int = 0
    min_chars: int = 0


@dataclass(frozen=True)
class ListBudget:
    """The exact budget for a repeated list slot: how many items and how big.

    ``max_items`` caps the row count (kept in lockstep with the manifest
    ``ListSlot.max_items``); ``max_words``/``max_chars`` bound *each* item so a
    programme-list line stays a label, never a sentence.
    """

    max_items: int
    max_words: int
    max_chars: int


@dataclass(frozen=True)
class TemplateSpec:
    """Everything Stage 4 needs to fill, gate and render one template.

    The manifest owns the *design*; this spec adds the copy contract: which
    slots the model fills, their exact budgets, the real source-page copy
    *exemplars* that govern style/shape (never content), the brand pages the
    vision gate compares the render against, and the version tags that make a
    render hash change when the template or fonts change.
    """

    template_id: str
    variant: Variant
    version: str
    tone: str
    manifest: SlideManifest
    fillable_slots: tuple[str, ...]
    required_slots: tuple[str, ...]
    budgets: dict[str, SlotBudget]
    exemplars: tuple[str, ...]
    reference_pages: tuple[int, ...]
    branding_left: str = DEFAULT_BRANDING_LEFT
    branding_right: str = DEFAULT_BRANDING_RIGHT
    # Repeated list slots (programme list): the slots the model fills with a
    # bounded list of strings, and their per-item budgets.
    fillable_lists: tuple[str, ...] = ()
    list_budgets: dict[str, ListBudget] = field(default_factory=dict)
    # Photo slots (campus, programme list). ``requires_photo`` is True only when
    # a photo slot is mandatory (campus): the planner may not choose this
    # template unless an approved photo is available, and Stage 4 degrades if it
    # ever renders without one.
    photo_slots: tuple[str, ...] = ()
    required_photo_slots: tuple[str, ...] = ()

    @property
    def requires_photo(self) -> bool:
        return bool(self.required_photo_slots)

    def fixed_values(self) -> dict[str, str]:
        """Furniture values the caller supplies, never the model (branding)."""
        return {
            "branding_left": self.branding_left,
            "branding_right": self.branding_right,
        }


# Real section-divider copy lifted from the source pages (p19, p52). These are
# *style/shape* exemplars only — they teach the model the register (a short,
# pointed question or a one-word section title with an italic-serif *emphasis*
# run), never the subject matter, which comes from the gap's own claim.
_SECTION_DIVIDER_EXEMPLARS: tuple[str, ...] = (
    "How do students *learn differently* at Masters' Union?",
    "*Immersions*",
    "How students *learn by traveling*",
)

# The brand pages the vision gate holds a divider render up against. p19 and p52
# are the real divider pages the template was reconstructed from; a third,
# module-relevant page is appended per slide at fill time.
_SECTION_DIVIDER_REFERENCE_PAGES: tuple[int, ...] = (19, 52)

_SECTION_DIVIDER_BUDGETS: dict[str, SlotBudget] = {
    # Kept in lockstep with the manifest slot ``char_budget`` floors.
    "title": SlotBudget(max_words=14, max_chars=120),
    "subtitle": SlotBudget(max_words=12, max_chars=90),
}


def _section_divider_spec(variant: Variant) -> TemplateSpec:
    manifest = section_divider_manifest(variant)
    return TemplateSpec(
        template_id=f"{SECTION_DIVIDER_TEMPLATE_ID}-{variant}",
        variant=variant,
        version=SECTION_DIVIDER_TEMPLATE_VERSION,
        tone=variant,
        manifest=manifest,
        fillable_slots=("title", "subtitle"),
        required_slots=("title",),
        budgets=dict(_SECTION_DIVIDER_BUDGETS),
        exemplars=_SECTION_DIVIDER_EXEMPLARS,
        reference_pages=_SECTION_DIVIDER_REFERENCE_PAGES,
    )


# --- Stat template (p58) ----------------------------------------------------

# Style/shape exemplars lifted from p58 — the register of the copy, never its
# subject (that comes from the gap's own facts). Serif italic *emphasis* marks
# the accent word; stat values are short, unit-bearing figures.
_STAT_EXEMPLARS: tuple[str, ...] = (
    "Placements at the *union*",
    "3.03x — Average salary increase from pre-MBA levels",
    "27.78L / Median CTC Secured",
    "Over 30+ / International & Remote Placements",
)
_STAT_REFERENCE_PAGES: tuple[int, ...] = (58, 57)

_STAT_BUDGETS: dict[str, SlotBudget] = {
    "title": SlotBudget(max_words=6, max_chars=48),
    "subtitle": SlotBudget(max_words=18, max_chars=110),
    "hero_value": SlotBudget(max_words=2, max_chars=12),
    "hero_label": SlotBudget(max_words=10, max_chars=64),
    "footnote": SlotBudget(max_words=10, max_chars=64),
}
for _i in range(1, STAT_ROW_COUNT + 1):
    _STAT_BUDGETS[f"stat{_i}_value"] = SlotBudget(max_words=3, max_chars=12)
    _STAT_BUDGETS[f"stat{_i}_label"] = SlotBudget(max_words=6, max_chars=54)

_STAT_FILLABLE: tuple[str, ...] = (
    "title",
    "subtitle",
    "hero_value",
    "hero_label",
    *tuple(
        f"stat{i}_{part}"
        for i in range(1, STAT_ROW_COUNT + 1)
        for part in ("value", "label")
    ),
    "footnote",
)


def _stat_spec(variant: Variant) -> TemplateSpec:
    return TemplateSpec(
        template_id=f"{STAT_TEMPLATE_ID}-{variant}",
        variant=variant,
        version=STAT_TEMPLATE_VERSION,
        tone="dark" if variant == "dark" else "light",
        manifest=stat_manifest(variant),
        fillable_slots=_STAT_FILLABLE,
        required_slots=("title", "hero_value", "hero_label"),
        budgets=dict(_STAT_BUDGETS),
        exemplars=_STAT_EXEMPLARS,
        reference_pages=_STAT_REFERENCE_PAGES,
    )


# --- Campus photo-and-headline template (p74-82) ----------------------------

_CAMPUS_EXEMPLARS: tuple[str, ...] = (
    "Classrooms",
    "Library",
    "PwC Lab",
    "High-energy learning spaces with digital boards & AV integration",
)
_CAMPUS_REFERENCE_PAGES: tuple[int, ...] = (74, 82)

_CAMPUS_BUDGETS: dict[str, SlotBudget] = {
    "headline": SlotBudget(max_words=3, max_chars=32),
    # The brand caption band: 7-11 words / 43-72 characters.
    "caption": SlotBudget(max_words=11, max_chars=72, min_words=7, min_chars=43),
}


def _campus_spec() -> TemplateSpec:
    return TemplateSpec(
        template_id=f"{CAMPUS_TEMPLATE_ID}-dark",
        variant="dark",
        version=CAMPUS_TEMPLATE_VERSION,
        tone="dark",
        manifest=campus_photo_manifest(),
        fillable_slots=("headline", "caption"),
        required_slots=("headline",),
        budgets=dict(_CAMPUS_BUDGETS),
        exemplars=_CAMPUS_EXEMPLARS,
        reference_pages=_CAMPUS_REFERENCE_PAGES,
        photo_slots=("hero_photo",),
        required_photo_slots=("hero_photo",),
    )


# --- Programme-list card template (p84-87) ----------------------------------

_PROGRAMME_EXEMPLARS: tuple[str, ...] = (
    "Undergraduate",
    "Postgraduate",
    "Immersions",
    "Programmes",
    "UG in **Technology & Business Management**",
    "PGP in **Applied AI & Agentic Systems**",
)
_PROGRAMME_REFERENCE_PAGES: tuple[int, ...] = (84, 85)

_PROGRAMME_BUDGETS: dict[str, SlotBudget] = {
    "title": SlotBudget(max_words=3, max_chars=26),
    "programme_type": SlotBudget(max_words=3, max_chars=24),
}
_PROGRAMME_LIST_BUDGETS: dict[str, ListBudget] = {
    "items": ListBudget(max_items=6, max_words=8, max_chars=48),
}


def _programme_list_spec(variant: Variant) -> TemplateSpec:
    return TemplateSpec(
        template_id=f"{PROGRAMME_LIST_TEMPLATE_ID}-{variant}",
        variant=variant,
        version=PROGRAMME_LIST_TEMPLATE_VERSION,
        tone="light",
        manifest=programme_list_manifest(variant),
        fillable_slots=("title", "programme_type"),
        required_slots=("title",),
        budgets=dict(_PROGRAMME_BUDGETS),
        exemplars=_PROGRAMME_EXEMPLARS,
        reference_pages=_PROGRAMME_REFERENCE_PAGES,
        fillable_lists=("items",),
        list_budgets=dict(_PROGRAMME_LIST_BUDGETS),
        photo_slots=("photo_1", "photo_2"),
    )


# Every template Stage 4 can fill/render, keyed by the id the planner emits.
# Ordered families: section dividers, stats, campus photo, programme lists.
SUPPORTED_TEMPLATE_IDS: tuple[str, ...] = (
    f"{SECTION_DIVIDER_TEMPLATE_ID}-light",
    f"{SECTION_DIVIDER_TEMPLATE_ID}-dark",
    f"{STAT_TEMPLATE_ID}-dark",
    f"{STAT_TEMPLATE_ID}-light",
    f"{CAMPUS_TEMPLATE_ID}-dark",
    f"{PROGRAMME_LIST_TEMPLATE_ID}-light",
    f"{PROGRAMME_LIST_TEMPLATE_ID}-blue",
)

# Templates that can only be planned/rendered when an approved photo exists.
PHOTO_REQUIRED_TEMPLATE_IDS: frozenset[str] = frozenset(
    {f"{CAMPUS_TEMPLATE_ID}-dark"}
)


def _spec_builder(template_id: str):
    if template_id in (
        f"{SECTION_DIVIDER_TEMPLATE_ID}-light",
        f"{SECTION_DIVIDER_TEMPLATE_ID}-dark",
    ):
        variant: Variant = "dark" if template_id.endswith("-dark") else "light"
        return lambda: _section_divider_spec(variant)
    if template_id in (f"{STAT_TEMPLATE_ID}-dark", f"{STAT_TEMPLATE_ID}-light"):
        stat_variant: Variant = "dark" if template_id.endswith("-dark") else "light"
        return lambda: _stat_spec(stat_variant)
    if template_id == f"{CAMPUS_TEMPLATE_ID}-dark":
        return _campus_spec
    if template_id in (
        f"{PROGRAMME_LIST_TEMPLATE_ID}-light",
        f"{PROGRAMME_LIST_TEMPLATE_ID}-blue",
    ):
        prog_variant: Variant = "blue" if template_id.endswith("-blue") else "light"
        return lambda: _programme_list_spec(prog_variant)
    return None


def template_spec(template_id: str) -> TemplateSpec:
    """Return the :class:`TemplateSpec` for a supported template id.

    Raises :class:`KeyError` for an unknown/unsupported id so callers fail loud
    rather than rendering an undefined template.
    """
    builder = _spec_builder(template_id)
    if builder is None:
        raise KeyError(f"unsupported template id: {template_id!r}")
    return builder()
