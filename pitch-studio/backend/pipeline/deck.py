from __future__ import annotations

from io import BytesIO
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt
from pydantic import BaseModel, Field

from backend.storage import save_file

INK = RGBColor(0x11, 0x11, 0x11)
PAPER = RGBColor(0xFF, 0xFF, 0xFF)
ACCENT = RGBColor(0x1F, 0x3A, 0x93)
MUTED = RGBColor(0x5C, 0x5C, 0x5C)

SLIDE_COUNTS = {
    "T0": 6,
    "T1": 6,
    "T2": 10,
    "T3": 14,
    "T4": 22,
    "T5": 18,
}

LAYOUTS = {"title", "section", "stat_pair", "bullets", "profile", "quote", "inventory", "cta"}


class DeckStat(BaseModel):
    label: str = ""
    value: str = ""


class DeckSlide(BaseModel):
    layout: str = "bullets"
    title: str = ""
    subtitle: str | None = None
    bullets: list[str] | None = None
    stats: list[DeckStat] | None = None
    quote: str | None = None
    attribution: str | None = None
    module_id: str = ""


class DeckSpec(BaseModel):
    slides: list[DeckSlide] = Field(default_factory=list)


def slide_count_for(duration: str) -> int:
    return SLIDE_COUNTS.get(duration, 10)


def validate_deck_order(spec: DeckSpec, sequence: list[str]) -> None:
    seen = [slide.module_id for slide in spec.slides if slide.module_id]
    filtered = [module_id for module_id in seen if module_id in sequence]
    last_index = -1
    for module_id in filtered:
        index = sequence.index(module_id)
        if index < last_index:
            raise ValueError("Deck module order does not follow the script sequence")
        last_index = index


def _set_run(run, text: str, size: int, bold: bool = False, color=INK, font: str = "Georgia") -> None:
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run.font.name = font


def _fill_slide(slide, color=PAPER) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def _add_accent_bar(slide) -> None:
    shape = slide.shapes.add_shape(1, Inches(0), Inches(0), Inches(13.333), Inches(0.12))
    shape.fill.solid()
    shape.fill.fore_color.rgb = ACCENT
    shape.line.fill.background()


def _add_text(slide, left, top, width, height, text, size, bold=False, color=INK, font="Georgia", align=PP_ALIGN.LEFT):
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    _set_run(run, text, size, bold, color, font)
    return box


def _render_title(slide, item: DeckSlide) -> None:
    _add_text(slide, Inches(0.8), Inches(2.2), Inches(11.7), Inches(1.6), item.title or "Masters' Union", 40, True)
    if item.subtitle:
        _add_text(slide, Inches(0.8), Inches(4.0), Inches(11.7), Inches(1.2), item.subtitle, 20, False, MUTED, "Helvetica Neue")


def _render_section(slide, item: DeckSlide) -> None:
    _add_text(slide, Inches(0.8), Inches(2.8), Inches(11.7), Inches(1.4), item.title or item.module_id, 36, True, ACCENT)
    if item.subtitle:
        _add_text(slide, Inches(0.8), Inches(4.3), Inches(11.7), Inches(1.0), item.subtitle, 18, False, MUTED, "Helvetica Neue")


def _render_bullets(slide, item: DeckSlide) -> None:
    _add_text(slide, Inches(0.8), Inches(0.5), Inches(11.7), Inches(1.0), item.title or "Key points", 28, True)
    bullets = (item.bullets or [])[:6]
    box = slide.shapes.add_textbox(Inches(0.9), Inches(1.7), Inches(11.5), Inches(5.2))
    tf = box.text_frame
    tf.word_wrap = True
    if not bullets:
        p = tf.paragraphs[0]
        run = p.add_run()
        _set_run(run, item.subtitle or "", 16, False, INK, "Helvetica Neue")
        return
    for index, bullet in enumerate(bullets):
        p = tf.paragraphs[0] if index == 0 else tf.add_paragraph()
        p.level = 0
        p.space_after = Pt(10)
        run = p.add_run()
        _set_run(run, f"•  {bullet}", 18, False, INK, "Helvetica Neue")


def _render_stat_pair(slide, item: DeckSlide) -> None:
    _add_text(slide, Inches(0.8), Inches(0.5), Inches(11.7), Inches(1.0), item.title or "Outcomes", 28, True)
    stats = (item.stats or [])[:4] or [DeckStat(label="Fact", value="—")]
    width = 5.4 if len(stats) <= 2 else 2.6
    for index, stat in enumerate(stats):
        left = 0.8 + (index % 4) * (width + 0.3)
        _add_text(slide, Inches(left), Inches(2.2), Inches(width), Inches(1.2), stat.value, 32, True, ACCENT)
        _add_text(slide, Inches(left), Inches(3.5), Inches(width), Inches(1.0), stat.label, 14, False, MUTED, "Helvetica Neue")


def _render_profile(slide, item: DeckSlide) -> None:
    _add_text(slide, Inches(0.8), Inches(0.5), Inches(11.7), Inches(0.8), item.title or "People", 28, True)
    _add_text(slide, Inches(0.8), Inches(1.6), Inches(11.7), Inches(0.6), item.subtitle or "", 16, False, ACCENT, "Helvetica Neue")
    bullets = item.bullets or []
    box = slide.shapes.add_textbox(Inches(0.9), Inches(2.4), Inches(11.5), Inches(4.4))
    tf = box.text_frame
    tf.word_wrap = True
    for index, bullet in enumerate(bullets[:6]):
        p = tf.paragraphs[0] if index == 0 else tf.add_paragraph()
        run = p.add_run()
        _set_run(run, f"•  {bullet}", 18, False, INK, "Helvetica Neue")


def _render_quote(slide, item: DeckSlide) -> None:
    _add_text(slide, Inches(1.2), Inches(2.0), Inches(10.8), Inches(2.6), f"“{item.quote or item.title}”", 26, True)
    if item.attribution:
        _add_text(slide, Inches(1.2), Inches(4.8), Inches(10.8), Inches(0.6), item.attribution, 16, False, MUTED, "Helvetica Neue")


def _render_inventory(slide, item: DeckSlide) -> None:
    _render_bullets(slide, item)


def _render_cta(slide, item: DeckSlide) -> None:
    _add_text(slide, Inches(0.8), Inches(2.4), Inches(11.7), Inches(1.2), item.title or "Next step", 34, True, ACCENT)
    if item.subtitle:
        _add_text(slide, Inches(0.8), Inches(3.8), Inches(11.7), Inches(1.2), item.subtitle, 18, False, INK, "Helvetica Neue")


RENDERERS = {
    "title": _render_title,
    "section": _render_section,
    "stat_pair": _render_stat_pair,
    "bullets": _render_bullets,
    "profile": _render_profile,
    "quote": _render_quote,
    "inventory": _render_inventory,
    "cta": _render_cta,
}


def spec_from_dict(payload: dict[str, Any]) -> DeckSpec:
    return DeckSpec.model_validate(payload)


def render_pptx(spec: DeckSpec, generation_id: int, sequence: list[str]) -> str:
    validate_deck_order(spec, sequence)
    presentation = Presentation()
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(7.5)
    blank = presentation.slide_layouts[6]
    slides = spec.slides or [DeckSlide(layout="title", title="Masters' Union", module_id=sequence[0] if sequence else "")]
    for item in slides:
        slide = presentation.slides.add_slide(blank)
        _fill_slide(slide)
        _add_accent_bar(slide)
        layout = item.layout if item.layout in LAYOUTS else "bullets"
        RENDERERS[layout](slide, item)
    buffer = BytesIO()
    presentation.save(buffer)
    key = f"decks/{generation_id}.pptx"
    return save_file(key, buffer.getvalue(), "application/vnd.openxmlformats-officedocument.presentationml.presentation")
