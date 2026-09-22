"""Layered HTML slide renderer.

This is the render substrate for *generated* slides (see the Generated Slide
Engine plan). A slide is described by a :class:`SlideManifest`: one or more
flattened SVG *furniture* layers (grid, rails, accent strokes — no live text,
no photos) plus a list of typed *slots* the caller fills at generation time.
Nothing about the visual design is decided here; the manifest owns the design
and the caller only supplies content.

Rendering is deliberately close to the rest of the deck, which is a run of
images: the manifest is turned into absolutely-positioned HTML, screenshotted
by a warm headless Chromium at exactly 1920x1080, and returned as a JPEG that
downstream stages cache and drop into the plan like any other page.

Two properties make this safe to build higher layers (templates, caching,
fact/vision gates) on top of:

* **Auto-fit with a floor.** Each text slot binary-searches its font size down
  until the copy stops overflowing its box. If it still overflows at the slot's
  ``min_font_size`` the render is *rejected* (:class:`SlideOverflowError`)
  rather than silently shrinking the type into something off-brand. Overflow is
  never rendered.
* **Determinism.** Fonts are embedded as ``@font-face`` data URIs so a render
  never depends on system fonts; animations/carets are disabled; the viewport
  and device scale factor are pinned. The same manifest and values produce the
  same pixels.

The browser is a warm singleton confined to its own thread, so it can be called
from sync or async code without tripping Playwright's "sync API inside asyncio"
guard, and it is cheap to reuse across many renders. Test hooks
(:func:`shutdown_renderer`, :meth:`SlideRenderer.close`) make lifecycle explicit
so suites can start/stop it or skip it when no browser is installed.
"""

from __future__ import annotations

import base64
import html
import json
import queue
import re
import threading
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

# ---------------------------------------------------------------------------
# Paths & font tokens
# ---------------------------------------------------------------------------

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
SLIDE_TEMPLATES_DIR = ASSETS_DIR / "slide-templates"

SLIDE_WIDTH = 1920
SLIDE_HEIGHT = 1080

# Version tag for the bundled font faces (see ``_GALANO_FACES`` and the serif
# token). It feeds the generated-slide render hash so that swapping in the
# licensed display serif — or otherwise changing the embedded faces — naturally
# invalidates every cached render without any manual cache bust. Bump this
# whenever ``_GALANO_FACES`` or :data:`SERIF_FALLBACK_STACK` changes.
FONT_BUNDLE_VERSION = "galano-alt-1+serif-fallback-1"

# The brand sans, bundled into the image (backend/assets/fonts). Filenames look
# like ``GalanoGrotesqueAltSemiBold.otf`` / ``...SemiBoldItalic.otf``.
SANS_FAMILY = "Galano Grotesque Alt"

# TEMPORARY serif fallback. The brand's real display serif is not available yet
# (see plan: "We do not have the real serif yet"). Everything that wants the
# serif references the ``"serif"`` font token, which resolves here, so when the
# licensed face is bundled this single constant is the only thing to change.
# Keep it isolated and obviously provisional.
SERIF_FALLBACK_FAMILY = "PT Serif"  # nominal @font-face family name
SERIF_FALLBACK_STACK = (
    f"'{SERIF_FALLBACK_FAMILY}', 'Georgia', 'Times New Roman', 'Noto Serif', serif"
)

FontToken = Literal["sans", "serif"]

FONT_STACKS: dict[str, str] = {
    "sans": f"'{SANS_FAMILY}', 'Helvetica Neue', Arial, sans-serif",
    "serif": SERIF_FALLBACK_STACK,
}

# Which bundled Galano Alt weights to embed, and the CSS ``font-weight`` /
# ``font-style`` each maps to. Only these are embedded so the generated HTML
# stays a sensible size; extend as templates need more weights.
_GALANO_FACES: tuple[tuple[str, int, str], ...] = (
    ("GalanoGrotesqueAltLight.otf", 300, "normal"),
    ("GalanoGrotesqueAltLightItalic.otf", 300, "italic"),
    ("GalanoGrotesqueAltRegular.otf", 400, "normal"),
    ("GalanoGrotesqueAltItalic.otf", 400, "italic"),
    ("GalanoGrotesqueAltMedium.otf", 500, "normal"),
    ("GalanoGrotesqueAltMediumItalic.otf", 500, "italic"),
    ("GalanoGrotesqueAltSemiBold.otf", 600, "normal"),
    ("GalanoGrotesqueAltSemiBoldItalic.otf", 600, "italic"),
    ("GalanoGrotesqueAltBold.otf", 700, "normal"),
    ("GalanoGrotesqueAltBoldItalic.otf", 700, "italic"),
    ("GalanoGrotesqueAltBlack.otf", 900, "normal"),
    ("GalanoGrotesqueAltBlackItalic.otf", 900, "italic"),
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SlideRenderError(RuntimeError):
    """A slide could not be rendered for a non-content reason."""


class SlideOverflowError(SlideRenderError):
    """Copy does not fit a slot even at its minimum font size.

    Raised instead of shrinking type below the floor or clipping text. Carries
    the offending slot names so callers can revise copy or drop the slide.
    """

    def __init__(self, slots: Sequence[str], detail: str = "") -> None:
        self.slots = list(slots)
        joined = ", ".join(self.slots)
        msg = f"copy overflows slot(s) even at minimum size: {joined}"
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


class BrowserUnavailable(SlideRenderError):
    """Playwright or its Chromium build is not installed in this environment."""


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    """A box in slide pixels (top-left origin)."""

    x: float
    y: float
    width: float
    height: float

    def style(self) -> str:
        return (
            f"left:{_px(self.x)};top:{_px(self.y)};"
            f"width:{_px(self.width)};height:{_px(self.height)};"
        )


VAlign = Literal["top", "middle", "bottom"]
HAlign = Literal["left", "center", "right"]


@dataclass(frozen=True)
class SvgLayer:
    """One flattened SVG furniture layer, drawn full-box behind the slots.

    ``asset`` is either a filename resolved under ``assets/slide-templates`` or
    a raw ``<svg>...</svg>`` string. Layers must not carry live text or photos —
    only static, on-brand vector furniture.
    """

    name: str
    asset: str
    rect: Rect = field(default=Rect(0, 0, SLIDE_WIDTH, SLIDE_HEIGHT))
    opacity: float = 1.0
    # Paint order across every visual layer (svg / photo / gradient); higher is
    # nearer the viewer. Text and list slots are always drawn above the layers.
    z: int = 0


# A conservative allow-list for gradient CSS: only ``linear-gradient`` /
# ``radial-gradient`` built from rgb/rgba/hex stops, spaces, commas, %, deg and
# the ``at``/``to`` keywords. Furniture-authored (never user copy), but
# validated anyway so a malformed value fails loudly instead of leaking markup.
_GRADIENT_RE = re.compile(
    r"^(?:linear|radial)-gradient\([#0-9a-zA-Z.,%\s()\-]+\)$"
)


@dataclass(frozen=True)
class GradientLayer:
    """A pure-CSS gradient overlay — the brand scrims that keep text legible.

    No asset file: the ``css`` is a validated ``linear-``/``radial-gradient``
    string painted full-box behind (or over) the photo. Used for the dark top
    scrim on the campus template and the tint washes on the stat template.
    """

    name: str
    rect: Rect
    css: str
    opacity: float = 1.0
    z: int = 0

    def __post_init__(self) -> None:
        if not _GRADIENT_RE.match(self.css.strip()):
            raise SlideRenderError(f"unsafe/invalid gradient css: {self.css!r}")


@dataclass(frozen=True)
class PhotoSlot:
    """A raster photo placed in a box, filled at render time from image bytes.

    The photo never ships in the manifest (no base64 in the template): the
    caller passes approved image bytes for the slot's ``name`` and they are
    embedded as a data URI. ``fit`` + ``focus_x``/``focus_y`` give a
    deterministic cover crop (same asset + same box -> same pixels), and the
    crop metadata feeds the render hash. A polaroid ``frame`` (white border,
    slight ``rotation_deg``, drop ``shadow``) reproduces the programme-list
    photo cards.
    """

    name: str
    rect: Rect
    fit: Literal["cover", "contain"] = "cover"
    focus_x: float = 0.5
    focus_y: float = 0.5
    rotation_deg: float = 0.0
    radius: float = 0.0
    frame: bool = False
    frame_color: str = "#FFFFFF"
    frame_width: float = 0.0
    shadow: bool = False
    background: str = "#000000"
    required: bool = False
    z: int = 10

    def crop_metadata(self) -> dict[str, Any]:
        """The deterministic crop descriptor that feeds the render hash."""
        return {
            "fit": self.fit,
            "focus_x": round(float(self.focus_x), 4),
            "focus_y": round(float(self.focus_y), 4),
            "rect": [self.rect.x, self.rect.y, self.rect.width, self.rect.height],
            "rotation_deg": round(float(self.rotation_deg), 4),
        }


@dataclass(frozen=True)
class ListSlot:
    """A bounded, repeated list of one-line items with ring bullets.

    Reproduces the programme-list card: up to ``max_items`` rows, each a bullet
    plus a single line of copy (``**strong**`` bolds the subject), with a hair
    divider between rows. Each provided item is rendered as its own measured
    text box, so the overflow/auto-fit gate applies per item exactly like a
    normal slot.
    """

    name: str
    rect: Rect
    max_items: int
    item_height: float
    font: FontToken = "sans"
    font_size: float = 28.0
    min_font_size: float = 18.0
    weight: int = 400
    tracking_em: float = 0.0
    line_height: float = 1.1
    color: str = "#111111"
    bullet: Literal["ring", "dot", "none"] = "ring"
    bullet_color: str = "#111111"
    bullet_size: float = 22.0
    text_indent: float = 44.0
    divider: bool = True
    divider_color: str = "#D8D8D8"
    char_budget_per_item: int | None = None
    required: bool = False


@dataclass(frozen=True)
class TextSlot:
    """A typed text box the caller fills.

    The size is the *maximum*; auto-fit shrinks toward ``min_font_size`` to make
    the copy fit, and rejects the render if it cannot. ``tracking_em`` is in em
    so letter-spacing scales with the fitted size (per-size tracking).
    Inline ``*emphasis*`` in a value renders as an italic serif run — the
    brand's italic-serif accent — using the ``serif`` token.
    """

    name: str
    rect: Rect
    font: FontToken = "sans"
    font_size: float = 96.0
    min_font_size: float = 40.0
    weight: int = 700
    italic: bool = False
    tracking_em: float = 0.0
    line_height: float = 1.05
    color: str = "#111111"
    align: HAlign = "left"
    valign: VAlign = "bottom"
    char_budget: int | None = None
    uppercase: bool = False
    # When False the slot renders at exactly ``font_size`` and overflow is still
    # detected (and rejected) but not auto-fitted. Useful for fixed furniture
    # labels such as header/footer branding.
    autofit: bool = True
    required: bool = False

    def resolved_min(self) -> float:
        return min(self.min_font_size, self.font_size)


@dataclass(frozen=True)
class SlideManifest:
    """A complete, typed description of one renderable slide template."""

    template_id: str
    layers: tuple[SvgLayer, ...] = ()
    text_slots: tuple[TextSlot, ...] = ()
    photo_slots: tuple[PhotoSlot, ...] = ()
    gradient_layers: tuple[GradientLayer, ...] = ()
    list_slots: tuple[ListSlot, ...] = ()
    width: int = SLIDE_WIDTH
    height: int = SLIDE_HEIGHT
    background: str = "#FFFFFF"

    def slot(self, name: str) -> TextSlot:
        for s in self.text_slots:
            if s.name == name:
                return s
        raise KeyError(name)

    def photo_slot(self, name: str) -> PhotoSlot:
        for p in self.photo_slots:
            if p.name == name:
                return p
        raise KeyError(name)

    def list_slot(self, name: str) -> ListSlot:
        for l in self.list_slots:
            if l.name == name:
                return l
        raise KeyError(name)

    @property
    def required_photo_slots(self) -> tuple[PhotoSlot, ...]:
        return tuple(p for p in self.photo_slots if p.required)

    # --- (de)serialisation --------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "width": self.width,
            "height": self.height,
            "background": self.background,
            "layers": [
                {
                    "name": l.name,
                    "asset": l.asset,
                    "rect": [l.rect.x, l.rect.y, l.rect.width, l.rect.height],
                    "opacity": l.opacity,
                    "z": l.z,
                }
                for l in self.layers
            ],
            "gradient_layers": [
                {
                    "name": g.name,
                    "rect": [g.rect.x, g.rect.y, g.rect.width, g.rect.height],
                    "css": g.css,
                    "opacity": g.opacity,
                    "z": g.z,
                }
                for g in self.gradient_layers
            ],
            "photo_slots": [
                {
                    "name": p.name,
                    "rect": [p.rect.x, p.rect.y, p.rect.width, p.rect.height],
                    "fit": p.fit,
                    "focus_x": p.focus_x,
                    "focus_y": p.focus_y,
                    "rotation_deg": p.rotation_deg,
                    "radius": p.radius,
                    "frame": p.frame,
                    "frame_color": p.frame_color,
                    "frame_width": p.frame_width,
                    "shadow": p.shadow,
                    "background": p.background,
                    "required": p.required,
                    "z": p.z,
                }
                for p in self.photo_slots
            ],
            "list_slots": [
                {
                    "name": ls.name,
                    "rect": [ls.rect.x, ls.rect.y, ls.rect.width, ls.rect.height],
                    "max_items": ls.max_items,
                    "item_height": ls.item_height,
                    "font": ls.font,
                    "font_size": ls.font_size,
                    "min_font_size": ls.min_font_size,
                    "weight": ls.weight,
                    "tracking_em": ls.tracking_em,
                    "line_height": ls.line_height,
                    "color": ls.color,
                    "bullet": ls.bullet,
                    "bullet_color": ls.bullet_color,
                    "bullet_size": ls.bullet_size,
                    "text_indent": ls.text_indent,
                    "divider": ls.divider,
                    "divider_color": ls.divider_color,
                    "char_budget_per_item": ls.char_budget_per_item,
                    "required": ls.required,
                }
                for ls in self.list_slots
            ],
            "text_slots": [
                {
                    "name": s.name,
                    "rect": [s.rect.x, s.rect.y, s.rect.width, s.rect.height],
                    "font": s.font,
                    "font_size": s.font_size,
                    "min_font_size": s.min_font_size,
                    "weight": s.weight,
                    "italic": s.italic,
                    "tracking_em": s.tracking_em,
                    "line_height": s.line_height,
                    "color": s.color,
                    "align": s.align,
                    "valign": s.valign,
                    "char_budget": s.char_budget,
                    "uppercase": s.uppercase,
                    "autofit": s.autofit,
                    "required": s.required,
                }
                for s in self.text_slots
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SlideManifest":
        def rect(v: Sequence[float]) -> Rect:
            return Rect(float(v[0]), float(v[1]), float(v[2]), float(v[3]))

        layers = tuple(
            SvgLayer(
                name=l["name"],
                asset=l["asset"],
                rect=rect(l.get("rect", [0, 0, SLIDE_WIDTH, SLIDE_HEIGHT])),
                opacity=float(l.get("opacity", 1.0)),
                z=int(l.get("z", 0)),
            )
            for l in payload.get("layers", [])
        )
        gradient_layers = tuple(
            GradientLayer(
                name=g["name"],
                rect=rect(g["rect"]),
                css=g["css"],
                opacity=float(g.get("opacity", 1.0)),
                z=int(g.get("z", 0)),
            )
            for g in payload.get("gradient_layers", [])
        )
        photo_slots = tuple(
            PhotoSlot(
                name=p["name"],
                rect=rect(p["rect"]),
                fit=p.get("fit", "cover"),
                focus_x=float(p.get("focus_x", 0.5)),
                focus_y=float(p.get("focus_y", 0.5)),
                rotation_deg=float(p.get("rotation_deg", 0.0)),
                radius=float(p.get("radius", 0.0)),
                frame=bool(p.get("frame", False)),
                frame_color=p.get("frame_color", "#FFFFFF"),
                frame_width=float(p.get("frame_width", 0.0)),
                shadow=bool(p.get("shadow", False)),
                background=p.get("background", "#000000"),
                required=bool(p.get("required", False)),
                z=int(p.get("z", 10)),
            )
            for p in payload.get("photo_slots", [])
        )
        list_slots = tuple(
            ListSlot(
                name=ls["name"],
                rect=rect(ls["rect"]),
                max_items=int(ls["max_items"]),
                item_height=float(ls["item_height"]),
                font=ls.get("font", "sans"),
                font_size=float(ls.get("font_size", 28.0)),
                min_font_size=float(ls.get("min_font_size", 18.0)),
                weight=int(ls.get("weight", 400)),
                tracking_em=float(ls.get("tracking_em", 0.0)),
                line_height=float(ls.get("line_height", 1.1)),
                color=ls.get("color", "#111111"),
                bullet=ls.get("bullet", "ring"),
                bullet_color=ls.get("bullet_color", "#111111"),
                bullet_size=float(ls.get("bullet_size", 22.0)),
                text_indent=float(ls.get("text_indent", 44.0)),
                divider=bool(ls.get("divider", True)),
                divider_color=ls.get("divider_color", "#D8D8D8"),
                char_budget_per_item=ls.get("char_budget_per_item"),
                required=bool(ls.get("required", False)),
            )
            for ls in payload.get("list_slots", [])
        )
        slots = tuple(
            TextSlot(
                name=s["name"],
                rect=rect(s["rect"]),
                font=s.get("font", "sans"),
                font_size=float(s.get("font_size", 96.0)),
                min_font_size=float(s.get("min_font_size", 40.0)),
                weight=int(s.get("weight", 700)),
                italic=bool(s.get("italic", False)),
                tracking_em=float(s.get("tracking_em", 0.0)),
                line_height=float(s.get("line_height", 1.05)),
                color=s.get("color", "#111111"),
                align=s.get("align", "left"),
                valign=s.get("valign", "bottom"),
                char_budget=s.get("char_budget"),
                uppercase=bool(s.get("uppercase", False)),
                autofit=bool(s.get("autofit", True)),
                required=bool(s.get("required", False)),
            )
            for s in payload.get("text_slots", [])
        )
        return cls(
            template_id=payload["template_id"],
            layers=layers,
            text_slots=slots,
            photo_slots=photo_slots,
            gradient_layers=gradient_layers,
            list_slots=list_slots,
            width=int(payload.get("width", SLIDE_WIDTH)),
            height=int(payload.get("height", SLIDE_HEIGHT)),
            background=payload.get("background", "#FFFFFF"),
        )


# ---------------------------------------------------------------------------
# HTML building (pure, no browser)
# ---------------------------------------------------------------------------


def _px(v: float) -> str:
    # Trim trailing zeros for stable, readable output.
    return f"{v:.4f}".rstrip("0").rstrip(".") + "px"


@lru_cache(maxsize=1)
def _font_face_css() -> str:
    """Build ``@font-face`` rules with the bundled Galano Alt embedded as data
    URIs, so rendering never depends on system-installed fonts."""
    rules: list[str] = []
    for filename, weight, style in _GALANO_FACES:
        path = FONTS_DIR / filename
        if not path.exists():
            continue
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        rules.append(
            "@font-face{"
            f"font-family:'{SANS_FAMILY}';"
            f"font-weight:{weight};font-style:{style};font-display:block;"
            f"src:url(data:font/otf;base64,{data}) format('opentype');"
            "}"
        )
    return "".join(rules)


def _svg_data_uri(layer: SvgLayer) -> str:
    text = layer.asset
    if not text.lstrip().startswith("<"):
        path = SLIDE_TEMPLATES_DIR / layer.asset
        if not path.exists():
            raise SlideRenderError(f"furniture asset not found: {layer.asset}")
        text = path.read_text(encoding="utf-8")
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


# Inline markup. ``**strong**`` is a bold *sans* run (e.g. the emphasised
# subject in a programme-list item); ``*emphasis*`` is the brand's italic-serif
# accent. Strong is parsed first so the double-asterisk form is never mistaken
# for two single-asterisk runs.
_STRONG_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_EMPHASIS_RE = re.compile(r"\*(.+?)\*", re.S)


def _render_inline(value: str) -> str:
    """Escape ``value`` and turn its markup into styled runs.

    ``**strong**`` becomes a bold sans run and ``*emphasis*`` becomes an
    italic-serif run. Escaping happens first, so the markup can never be used to
    inject HTML: only the literal asterisk delimiters are interpreted, and the
    substituted spans contain no asterisks of their own.
    """
    escaped = html.escape(value, quote=False)
    escaped = _STRONG_RE.sub(
        lambda m: f'<span class="strong">{m.group(1)}</span>', escaped
    )
    return _EMPHASIS_RE.sub(
        lambda m: f'<span class="emph">{m.group(1)}</span>', escaped
    )


_VALIGN_CSS = {"top": "flex-start", "middle": "center", "bottom": "flex-end"}
_HALIGN_CSS = {"left": "flex-start", "center": "center", "right": "flex-end"}
_TEXTALIGN_CSS = {"left": "left", "center": "center", "right": "right"}

# Text/list copy is always painted above every visual layer (svg/photo/gradient),
# whose ``z`` values are template furniture in a much lower band.
_TEXT_Z = 200
_BULLET_Z = 150
_DIVIDER_Z = 140

PhotoData = "bytes | bytearray | str"


def _strip_markup(value: str) -> str:
    """Drop ``**strong**``/``*emphasis*`` markup, leaving the plain copy."""
    text = _STRONG_RE.sub(lambda m: m.group(1), value or "")
    return _EMPHASIS_RE.sub(lambda m: m.group(1), text).strip()


def _photo_data_uri(data: "bytes | bytearray | str") -> str:
    """Embed approved image *bytes* (or pass through a ``data:`` URI).

    Only local/stored bytes or an existing data URI are accepted — never a
    remote URL — so a render never depends on a browser network fetch.
    """
    if isinstance(data, str):
        s = data.strip()
        if s.startswith("data:image/"):
            return s
        raise SlideRenderError("photo value must be image bytes or a data:image URI")
    if not isinstance(data, (bytes, bytearray)):
        raise SlideRenderError("photo value must be image bytes or a data:image URI")
    blob = bytes(data)
    if not blob:
        raise SlideRenderError("photo value is empty")
    mime = "image/png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    encoded = base64.b64encode(blob).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _photo_layer_html(slot: PhotoSlot, uri: str) -> str:
    transform = f"transform:rotate({slot.rotation_deg}deg);" if slot.rotation_deg else ""
    shadow = "box-shadow:0 18px 44px rgba(0,0,0,0.30);" if slot.shadow else ""
    frame = (
        f"background:{slot.frame_color};padding:{_px(slot.frame_width)};"
        if slot.frame and slot.frame_width
        else ""
    )
    radius = f"border-radius:{_px(slot.radius)};" if slot.radius else ""
    inner_radius = radius if not frame else ""
    return (
        f'<div class="layer photo" style="{slot.rect.style()}z-index:{slot.z};'
        f'{transform}{shadow}{frame}{radius}">'
        f'<div class="photo-clip" style="width:100%;height:100%;overflow:hidden;'
        f'background:{slot.background};{inner_radius}">'
        f'<img src="{uri}" style="width:100%;height:100%;display:block;'
        f'object-fit:{slot.fit};'
        f'object-position:{slot.focus_x * 100:.2f}% {slot.focus_y * 100:.2f}%;">'
        f"</div></div>"
    )


def _text_slot_html(
    *,
    name: str,
    rect: Rect,
    content: str,
    font: str,
    weight: int,
    italic: bool,
    tracking_em: float,
    line_height: float,
    color: str,
    align: HAlign,
    valign: VAlign,
    uppercase: bool,
    autofit: bool,
    font_size: float,
    min_size: float,
    z: int = _TEXT_Z,
) -> str:
    stack = FONT_STACKS.get(font, FONT_STACKS["sans"])
    style = (
        f"{rect.style()}z-index:{z};"
        f"justify-content:{_HALIGN_CSS[align]};"
        f"align-items:{_VALIGN_CSS[valign]};"
    )
    content_style = (
        f"font-family:{stack};"
        f"font-weight:{weight};"
        f"font-style:{'italic' if italic else 'normal'};"
        f"letter-spacing:{tracking_em}em;"
        f"line-height:{line_height};"
        f"color:{color};"
        f"text-align:{_TEXTALIGN_CSS[align]};"
        f"{'text-transform:uppercase;' if uppercase else ''}"
    )
    return (
        f'<div class="slot" data-slot="{html.escape(name)}" '
        f'data-autofit="{1 if autofit else 0}" '
        f'data-max="{font_size}" data-min="{min_size}" '
        f'style="{style}">'
        f'<div class="content" style="{content_style}">{content}</div>'
        f"</div>"
    )


def _list_slot_html(ls: ListSlot, items: Sequence[str]) -> list[str]:
    """Expand a list slot into bullet/divider furniture plus measured rows."""
    parts: list[str] = []
    drawn = [item for item in items if (item or "").strip()][: ls.max_items]
    for index, raw in enumerate(drawn):
        top = ls.rect.y + index * ls.item_height
        row = Rect(ls.rect.x, top, ls.rect.width, ls.item_height)
        # Ring/dot bullet, vertically centred in the row.
        if ls.bullet != "none":
            b = ls.bullet_size
            by = top + (ls.item_height - b) / 2
            if ls.bullet == "ring":
                bullet_style = (
                    f"border:{max(2.0, b * 0.16):.2f}px solid {ls.bullet_color};"
                    "background:transparent;"
                )
            else:
                bullet_style = f"background:{ls.bullet_color};"
            parts.append(
                f'<div class="bullet" style="left:{_px(ls.rect.x)};top:{_px(by)};'
                f"width:{_px(b)};height:{_px(b)};border-radius:50%;z-index:{_BULLET_Z};"
                f'{bullet_style}"></div>'
            )
        text_rect = Rect(
            ls.rect.x + ls.text_indent,
            top,
            ls.rect.width - ls.text_indent,
            ls.item_height,
        )
        parts.append(
            _text_slot_html(
                name=f"{ls.name}#{index}",
                rect=text_rect,
                content=_render_inline(raw),
                font=ls.font,
                weight=ls.weight,
                italic=False,
                tracking_em=ls.tracking_em,
                line_height=ls.line_height,
                color=ls.color,
                align="left",
                valign="middle",
                uppercase=False,
                autofit=True,
                font_size=ls.font_size,
                min_size=min(ls.min_font_size, ls.font_size),
            )
        )
        if ls.divider and index < len(drawn) - 1:
            dy = top + ls.item_height
            parts.append(
                f'<div class="divider" style="left:{_px(ls.rect.x + ls.text_indent)};'
                f"top:{_px(dy)};width:{_px(ls.rect.width - ls.text_indent)};height:1px;"
                f'background:{ls.divider_color};z-index:{_DIVIDER_Z};"></div>'
            )
    return parts


def build_slide_html(
    manifest: SlideManifest,
    values: Mapping[str, Any],
    *,
    photos: Mapping[str, "bytes | bytearray | str"] | None = None,
    embed_fonts: bool = True,
) -> str:
    """Render ``manifest`` + ``values`` (+ ``photos``) to a self-contained page.

    Pure and deterministic — no browser involved — so HTML/escaping/overflow
    logic can be unit-tested without Chromium. ``embed_fonts=False`` omits the
    (large) font data URIs for readable test snapshots. ``photos`` maps a photo
    slot name to approved image *bytes* (or a ``data:`` URI); a required photo
    slot with no bytes raises :class:`SlideRenderError` so the caller can
    degrade to a no-photo template.
    """
    photos = photos or {}

    missing_required = [
        s.name
        for s in manifest.text_slots
        if s.required and not str(values.get(s.name) or "").strip()
    ]
    missing_required += [
        ls.name
        for ls in manifest.list_slots
        if ls.required and not [i for i in (values.get(ls.name) or []) if str(i).strip()]
    ]
    missing_photos = [
        p.name for p in manifest.photo_slots if p.required and not photos.get(p.name)
    ]
    if missing_photos:
        raise SlideRenderError(
            f"missing required photo slot(s): {', '.join(missing_photos)}"
        )
    if missing_required:
        raise SlideRenderError(f"missing required slot(s): {', '.join(missing_required)}")

    over_budget: list[str] = []
    for s in manifest.text_slots:
        raw = str(values.get(s.name, "") or "")
        if s.char_budget is not None and len(raw) > s.char_budget:
            over_budget.append(f"{s.name}({len(raw)}>{s.char_budget})")
    for ls in manifest.list_slots:
        if ls.char_budget_per_item is None:
            continue
        for index, item in enumerate((values.get(ls.name) or [])[: ls.max_items]):
            plain = _strip_markup(str(item))
            if len(plain) > ls.char_budget_per_item:
                over_budget.append(
                    f"{ls.name}#{index}({len(plain)}>{ls.char_budget_per_item})"
                )
    if over_budget:
        raise SlideOverflowError(
            [b.split("(")[0] for b in over_budget],
            detail="over character budget: " + ", ".join(over_budget),
        )

    # Visual layers (svg furniture, photos, gradients) share one paint order,
    # sorted by ``z`` then declaration order; copy is always painted above them.
    ordered: list[tuple[int, int, str]] = []
    seq = 0
    for layer in manifest.layers:
        uri = _svg_data_uri(layer)
        ordered.append(
            (
                layer.z,
                seq,
                f'<div class="layer" style="{layer.rect.style()}z-index:{layer.z};'
                f'opacity:{layer.opacity}"><img src="{uri}" alt=""></div>',
            )
        )
        seq += 1
    for photo in manifest.photo_slots:
        data = photos.get(photo.name)
        if not data:
            continue  # optional photo, absent -> not drawn
        ordered.append((photo.z, seq, _photo_layer_html(photo, _photo_data_uri(data))))
        seq += 1
    for grad in manifest.gradient_layers:
        ordered.append(
            (
                grad.z,
                seq,
                f'<div class="layer" style="{grad.rect.style()}z-index:{grad.z};'
                f'opacity:{grad.opacity};background:{grad.css};"></div>',
            )
        )
        seq += 1
    ordered.sort(key=lambda item: (item[0], item[1]))
    layer_html = [html_str for _z, _seq, html_str in ordered]

    slot_html: list[str] = []
    for s in manifest.text_slots:
        raw = str(values.get(s.name, "") or "")
        if not raw.strip():
            continue  # optional, empty -> not drawn
        slot_html.append(
            _text_slot_html(
                name=s.name,
                rect=s.rect,
                content=_render_inline(raw),
                font=s.font,
                weight=s.weight,
                italic=s.italic,
                tracking_em=s.tracking_em,
                line_height=s.line_height,
                color=s.color,
                align=s.align,
                valign=s.valign,
                uppercase=s.uppercase,
                autofit=s.autofit,
                font_size=s.font_size,
                min_size=s.resolved_min(),
            )
        )
    for ls in manifest.list_slots:
        items = values.get(ls.name) or []
        if isinstance(items, str):  # tolerate a single string
            items = [items]
        slot_html.extend(_list_slot_html(ls, list(items)))

    fonts_css = _font_face_css() if embed_fonts else ""
    emph_stack = FONT_STACKS["serif"]

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{fonts_css}
*{{margin:0;padding:0;box-sizing:border-box;}}
html,body{{width:{manifest.width}px;height:{manifest.height}px;background:{manifest.background};}}
#slide{{position:relative;width:{manifest.width}px;height:{manifest.height}px;overflow:hidden;background:{manifest.background};}}
.layer{{position:absolute;}}
.layer img{{width:100%;height:100%;display:block;}}
.photo{{position:absolute;}}
.bullet{{position:absolute;box-sizing:border-box;}}
.divider{{position:absolute;}}
.slot{{position:absolute;display:flex;overflow:hidden;}}
.slot .content{{width:100%;overflow-wrap:break-word;}}
.content .emph{{font-family:{emph_stack};font-style:italic;font-weight:inherit;}}
.content .strong{{font-weight:700;}}
</style></head>
<body><div id="slide">
{''.join(layer_html)}
{''.join(slot_html)}
</div></body></html>"""


# The in-page auto-fit: for every autofit slot binary-search the largest font
# size in [min,max] that fits both height and width; non-autofit slots are only
# measured. Returns the fitted size and whether it still overflows at min.
_AUTOFIT_JS = r"""
() => {
  const results = {};
  const slots = document.querySelectorAll('#slide .slot');
  // Measure the content's *natural* box, not the flex container: with
  // vertical alignment (align-items) the container's own scrollHeight can
  // round a few pixels past the box even when the copy clearly fits.
  const fits = (slot) => {
    const c = slot.querySelector('.content');
    return c.scrollHeight <= slot.clientHeight + 0.5
        && c.scrollWidth <= slot.clientWidth + 0.5;
  };
  for (const slot of slots) {
    const name = slot.getAttribute('data-slot');
    const content = slot.querySelector('.content');
    const max = parseFloat(slot.getAttribute('data-max'));
    const min = parseFloat(slot.getAttribute('data-min'));
    const autofit = slot.getAttribute('data-autofit') === '1';
    let size = max;
    if (autofit && max > min) {
      let lo = min, hi = max;
      content.style.fontSize = hi + 'px';
      if (!fits(slot)) {
        // Binary search for the largest fitting integer-ish size.
        for (let i = 0; i < 24 && hi - lo > 0.25; i++) {
          const mid = (lo + hi) / 2;
          content.style.fontSize = mid + 'px';
          if (fits(slot)) { lo = mid; } else { hi = mid; }
        }
        size = lo;
        content.style.fontSize = size + 'px';
      }
    } else {
      content.style.fontSize = size + 'px';
    }
    const overflow = !fits(slot);
    results[name] = { size: Math.round(size * 100) / 100, overflow, min, max };
  }
  return results;
}
"""


@dataclass(frozen=True)
class RenderResult:
    """Outcome of a successful render."""

    jpeg: bytes
    template_id: str
    fitted_sizes: dict[str, float]
    width: int = SLIDE_WIDTH
    height: int = SLIDE_HEIGHT


# ---------------------------------------------------------------------------
# Warm browser (thread-confined sync Playwright)
# ---------------------------------------------------------------------------


class SlideRenderer:
    """A reusable, warm headless-Chromium renderer.

    The browser lives on a dedicated thread and processes render jobs off a
    queue, so a single warmed instance is safe to share and can be driven from
    both sync and async callers. Start lazily on first render; call
    :meth:`close` (or :func:`shutdown_renderer`) to tear down cleanly.
    """

    def __init__(self, *, jpeg_quality: int = 92, headless: bool = True) -> None:
        self.jpeg_quality = jpeg_quality
        self.headless = headless
        self._thread: threading.Thread | None = None
        self._jobs: "queue.Queue[tuple]" = queue.Queue()
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._lock = threading.Lock()
        self._closed = False

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                if self._start_error is not None:
                    raise self._start_error
                return
            self._closed = False
            self._ready.clear()
            self._start_error = None
            self._thread = threading.Thread(
                target=self._run, name="slide-renderer", daemon=True
            )
            self._thread.start()
        self._ready.wait()
        if self._start_error is not None:
            # Surface the startup failure and reset so a later call can retry.
            err = self._start_error
            self._join_thread()
            raise err

    def close(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._closed = True
        self._jobs.put(("stop", None, None))
        thread.join(timeout=30)
        with self._lock:
            self._thread = None
            self._ready.clear()

    def _join_thread(self) -> None:
        thread = self._thread
        self._thread = None
        if thread is not None:
            self._jobs.put(("stop", None, None))
            thread.join(timeout=10)

    # -- rendering ----------------------------------------------------------
    def render(
        self,
        manifest: SlideManifest,
        values: Mapping[str, Any],
        *,
        photos: Mapping[str, "bytes | bytearray | str"] | None = None,
    ) -> RenderResult:
        """Render one slide. Raises :class:`SlideOverflowError` if copy cannot
        fit, :class:`BrowserUnavailable` if Chromium is missing."""
        html_doc = build_slide_html(manifest, values, photos=photos, embed_fonts=True)
        fitted = self.render_html(
            html_doc, width=manifest.width, height=manifest.height
        )
        return RenderResult(
            jpeg=fitted[0],
            template_id=manifest.template_id,
            fitted_sizes=fitted[1],
            width=manifest.width,
            height=manifest.height,
        )

    def render_html(
        self, html_doc: str, *, width: int = SLIDE_WIDTH, height: int = SLIDE_HEIGHT
    ) -> tuple[bytes, dict[str, float]]:
        """Render a prepared HTML document to (jpeg_bytes, fitted_sizes)."""
        self.start()
        result: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)
        self._jobs.put(("render", (html_doc, width, height), result))
        kind, payload = result.get()
        if kind == "ok":
            return payload
        raise payload  # an exception instance

    # -- worker thread ------------------------------------------------------
    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - import guard
            self._start_error = BrowserUnavailable(
                "playwright is not installed; run `pip install playwright` and "
                "`playwright install chromium`"
            )
            self._start_error.__cause__ = exc
            self._ready.set()
            return

        playwright = browser = None
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(
                headless=self.headless,
                args=["--force-color-profile=srgb", "--disable-lcd-text"],
            )
        except Exception as exc:
            self._start_error = BrowserUnavailable(
                "could not launch Chromium; run `playwright install chromium`"
            )
            self._start_error.__cause__ = exc
            self._ready.set()
            if browser is not None:
                browser.close()
            if playwright is not None:
                playwright.stop()
            return

        self._ready.set()
        try:
            while True:
                op, args, out = self._jobs.get()
                if op == "stop":
                    break
                if op != "render":
                    continue
                try:
                    out.put(("ok", self._render_once(browser, *args)))
                except BaseException as exc:  # return to caller thread
                    out.put(("err", exc))
        finally:
            try:
                browser.close()
            finally:
                playwright.stop()

    def _render_once(
        self, browser: Any, html_doc: str, width: int, height: int
    ) -> tuple[bytes, dict[str, float]]:
        context = browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=1,
        )
        try:
            page = context.new_page()
            page.set_content(html_doc, wait_until="load")
            # Ensure embedded fonts are parsed before measuring/screenshotting.
            page.evaluate("async () => { await document.fonts.ready; }")
            fitted_raw = page.evaluate(_AUTOFIT_JS)
            overflow = [name for name, info in fitted_raw.items() if info.get("overflow")]
            if overflow:
                raise SlideOverflowError(sorted(overflow))
            jpeg = page.screenshot(
                type="jpeg",
                quality=self.jpeg_quality,
                clip={"x": 0, "y": 0, "width": width, "height": height},
                animations="disabled",
            )
            fitted = {name: float(info["size"]) for name, info in fitted_raw.items()}
            return jpeg, fitted
        finally:
            context.close()


# ---------------------------------------------------------------------------
# Module-level warm singleton + test hooks
# ---------------------------------------------------------------------------

_RENDERER: SlideRenderer | None = None
_RENDERER_LOCK = threading.Lock()


def get_renderer() -> SlideRenderer:
    """Return the process-wide warm renderer, starting it on first use."""
    global _RENDERER
    with _RENDERER_LOCK:
        if _RENDERER is None:
            _RENDERER = SlideRenderer()
    _RENDERER.start()
    return _RENDERER


def shutdown_renderer() -> None:
    """Tear down the warm renderer if one is running (test/lifecycle hook)."""
    global _RENDERER
    with _RENDERER_LOCK:
        renderer = _RENDERER
        _RENDERER = None
    if renderer is not None:
        renderer.close()


def render_slide(
    manifest: SlideManifest,
    values: Mapping[str, Any],
    *,
    photos: Mapping[str, "bytes | bytearray | str"] | None = None,
) -> RenderResult:
    """Convenience: render one slide on the shared warm renderer."""
    return get_renderer().render(manifest, values, photos=photos)


def playwright_available() -> bool:
    """True when Playwright and a launchable Chromium are present.

    Used by tests to skip *integration* render cases (only) with a clear reason
    while pure manifest/HTML/overflow tests keep running everywhere.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return True
    except Exception:
        return False
