"""Build a pitch deck out of pages from the Masters' Union Brand Deck.

Slides are not written from scratch any more. Every slide in a generated deck
is a real page of the brand deck PDF, chosen to match the recipe's module
sequence and trimmed to the slide budget for the duration. That keeps the deck
on brand by construction: nothing can overflow, no layout can drift, and the
design team stays the owner of what a slide looks like.

The page -> module map below is curated. Page numbers are 1-based and match the
92-page source PDF. Within each module the pages are listed **best first**: a
30-second pitch only gets each module's opening page, while a 30-minute deck
walks further down the list. That ordering is the only knob that decides what a
short deck shows, so it is worth keeping deliberate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from threading import Lock

from pptx import Presentation
from pptx.util import Inches
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.config import FILES_DIR, settings
from backend.models import Asset
from backend.pipeline.deck import (
    BRAND_SOURCE,
    SLIDE_H,
    SLIDE_W,
    DeckSlide,
    DeckSpec,
    deck_slide_from_planned,
)
from backend.storage import read_file, save_file

SOURCE_PAGE_COUNT = 92
COVER_PAGE = 1
CLOSING_PAGE = 92

# Pages are 1920x1080pt, so 72 DPI rasterises them at their native 1920x1080px
# — plenty for a 13.33in slide and far smaller than the 236MB source.
IMAGE_DPI = 72
JPEG_QUALITY = 82

PAGE_CACHE_PREFIX = "brand-deck/pages"
SOURCE_CACHE_PATH = "brand-deck/source.pdf"


class BrandDeckUnavailable(RuntimeError):
    """Raised when the brand deck PDF cannot be located."""


def brand_slide_key(page: int, occurrence: int = 1) -> str:
    """Stable identity for a brand-deck slide.

    The key is the page for the common case where a page appears once in a
    plan. When the same page is placed more than once (a deliberate repeat),
    later occurrences are suffixed so the two slides never collide on identity
    — notes, previews and edits address the occurrence, not just the page.
    """
    key = f"{BRAND_SOURCE}:p{page}"
    return key if occurrence <= 1 else f"{key}#{occurrence}"


@dataclass(frozen=True)
class BrandSlide:
    """One page of the brand deck, placed in a generated deck.

    Satisfies the :class:`~backend.pipeline.deck.PlannedSlide` interface, so it
    can be rendered or serialised without the caller knowing it is a brand
    page. ``occurrence`` is 1-based and only ever exceeds 1 when the same page
    is intentionally placed twice in one plan.
    """

    page: int
    module_id: str
    label: str
    occurrence: int = 1

    @property
    def image_url(self) -> str:
        return f"/api/brand-deck/pages/{self.page}.jpg"

    @property
    def source(self) -> str:
        return BRAND_SOURCE

    @property
    def title(self) -> str:
        return self.label

    @property
    def slide_key(self) -> str:
        return brand_slide_key(self.page, self.occurrence)


COVER = BrandSlide(COVER_PAGE, "", "Learn by Doing")
CLOSING = BrandSlide(CLOSING_PAGE, "M14", "Come and see it for yourself")

# Curated map: module id -> pages, best page first.
MODULE_PAGES: dict[str, tuple[tuple[int, str], ...]] = {
    "M01": (  # Origin — why this exists before what it is
        (6, "Hence, Masters' Union was born"),
        (2, "The origin story: why we started"),
        (4, "Medical students learn from doctors, with real patients"),
        (5, "Why should business, tech or design be any different?"),
        (3, "Education hadn't changed in decades"),
        (7, "In 2020, when everyone went online, we opened a campus"),
    ),
    "M02": (  # Institution — a real institution, not a bootcamp
        (9, "One purpose: India's first practitioner-led institution"),
        (11, "The people who joined the vision: Board of Governors"),
        (10, "One vision"),
    ),
    "M03": (  # Model — how learning works here, in one breath
        (20, "Inclass, Outclass, Immersions"),
        (19, "How students learn differently"),
        (12, "Why learn by doing?"),
    ),
    "M04": (  # Sequence — what a student actually does
        (51, "The five Outclass challenges"),
        (30, "Dropshipping Challenge: your first business"),
        (31, "₹5-50 lakh of revenue in the first semester"),
        (29, "Outclass: how students learn by building"),
        (43, "Venture Initiation Programme"),
        (36, "Food Lab Challenge: build a cloud kitchen"),
        (33, "Content Creator Challenge"),
        (39, "Makers' Lab: launch a product on Kickstarter"),
        (42, "Bloomberg Lab: trade with Wall Street tools"),
        (32, "The dropshipping brands students ran"),
        (53, "152 immersions completed in 2025"),
        (52, "Immersions: how students learn by travelling"),
        (38, "Brands built at Food Lab"),
        (41, "Builders in the Makers' Lab"),
        (34, "Creator channels: followers"),
        (35, "Creator channels: views"),
        (54, "Visiting India's top factories"),
        (56, "Students on the factory floor"),
        (37, "Inside Food Lab"),
        (40, "Inside the Makers' Lab"),
        (55, "On immersion"),
    ),
    "M05": (  # People proof — humans, not aggregates
        (17, "18 or 28, engineer or MBA: the point is to build"),
        (14, "Meet Vedang: built Zepto Cafe, now runs JustPour"),
        (13, "Meet Reyansh: MemoTag on Shark Tank India"),
        (16, "Meet Shouradeep & Upamanyu: PlaySuper, Forbes 30 Under 30"),
        (15, "Meet Rhea & Ayush: Lexi's, ₹1 Cr in year one"),
    ),
    "M06": (  # Venture proof — real companies, real revenue
        (44, "From ideas to funded ventures: 200 startups, ₹25 Cr raised"),
        (18, "Six student startups pitched on Shark Tank"),
        (45, "Eat Atlas and PlaySuper"),
        (46, "Flourish and Khet"),
        (47, "Sanyark and Zenmo"),
        (48, "Blue Brew and Cryptique"),
        (49, "Masters' Union Ventures: the funds"),
    ),
    "M07": (  # Outcome proof — the placement question, with method
        (58, "Placements: median ₹27.78 LPA, average ₹33.39 LPA"),
        (57, "Outcomes of this approach"),
        (59, "MBA career transitions"),
        (64, "Recruiters at Masters' Union"),
        (60, "MBA career transitions"),
        (61, "MBA career transitions"),
        (62, "MBA career transitions"),
        (63, "MBA career transitions"),
    ),
    "M08": (  # Faculty — what justifies the fee
        (22, "30% PhD, 30% international, 40% practitioners"),
        (25, "The practitioners: 40% of the faculty"),
        (21, "Inclass: learning from CXOs, investors and global faculty"),
        (26, "250+ CXOs and 50+ global professors on campus"),
        (23, "The academics who design the curriculum"),
        (24, "The visiting faculty"),
        (27, "Who has taught here"),
        (28, "Who has taught here"),
    ),
    "M09": (  # Campus — make it physical and specific
        (8, "In the heart of Gurugram's business hub"),
        (73, "See the campus"),
        (74, "Classrooms"),
        (81, "PwC Lab: NVIDIA-powered AI and robotics"),
        (76, "Pitching Zone"),
        (78, "Auditorium"),
        (77, "Co-working Zone"),
        (82, "Library"),
        (80, "Cafeteria"),
        (75, "Classrooms"),
        (79, "Auditorium"),
    ),
    "M10": (  # Programmes — route the listener to their own door
        (84, "Undergraduate programmes"),
        (85, "Postgraduate programmes"),
        (86, "Executive programmes"),
        (83, "Programmes at Masters' Union"),
        (87, "Immersion programmes"),
    ),
    "M11": (  # Ecosystem — the machine behind the outcomes
        (90, "Pratham joins Shark Tank India as a Shark"),
        (50, "A ₹100 crore venture fund for founders under 25"),
        (65, "In the spotlight"),
        (66, "In the spotlight"),
    ),
    "M12": (  # Culture — somewhere you would want to be
        (68, "Clubs and fraternities"),
        (67, "Student life at Masters' Union"),
        (70, "Case competitions"),
        (72, "Unmute: the cultural business festival"),
        (71, "Festivities"),
        (69, "Inside the clubs"),
    ),
    # Honest — the brand deck has no "here is what we are not" page, so the
    # closest honest answer to "are you actually a university?" is the
    # recognition record.
    "M13": (
        (88, "Youngest institution in India to get university status"),
        (89, "Acknowledged across industry and academia"),
    ),
    "M14": (  # Ask — converts
        (91, "India deserves this institution. We're building it."),
    ),
}

PAGE_LABELS: dict[int, str] = {COVER.page: COVER.label, CLOSING.page: CLOSING.label}
PAGE_MODULES: dict[int, str] = {COVER.page: COVER.module_id, CLOSING.page: CLOSING.module_id}
for _module_id, _pages in MODULE_PAGES.items():
    for _page, _label in _pages:
        PAGE_LABELS[_page] = _label
        PAGE_MODULES[_page] = _module_id


def plan_pages(sequence: list[str], slide_count: int) -> list[BrandSlide]:
    """Pick brand deck pages for ``sequence``, capped at ``slide_count`` slides.

    ``slide_count`` is a *ceiling*, not a fixed length: the cover and closing
    always bookend the deck, and the remaining slots are dealt out round-robin
    across the recipe modules (best page first). When the recipe runs out of
    pages the deck simply stops — it is never padded with off-recipe pages.
    Modules with a shallow page list drop out of the rotation and the deeper
    ones keep going until the ceiling.
    """
    budget = max(3, slide_count)
    modules = [module_id for module_id in dict.fromkeys(sequence) if MODULE_PAGES.get(module_id)]
    pools = {module_id: list(MODULE_PAGES[module_id]) for module_id in modules}
    chosen: dict[str, list[tuple[int, str]]] = {module_id: [] for module_id in modules}

    remaining = budget - 2  # cover + closing
    while remaining > 0 and any(pools.values()):
        progressed = False
        for module_id in modules:
            if remaining <= 0:
                break
            if pools[module_id]:
                chosen[module_id].append(pools[module_id].pop(0))
                remaining -= 1
                progressed = True
        if not progressed:
            break

    slides = [COVER]
    for module_id in modules:
        for page, label in sorted(chosen[module_id]):
            slides.append(BrandSlide(page, module_id, label))

    slides.append(CLOSING)
    return _assign_occurrences(slides)


def _assign_occurrences(slides: list[BrandSlide]) -> list[BrandSlide]:
    """Number repeated pages so every slide gets a unique ``slide_key``.

    Today a plan never repeats a page, so every occurrence is 1 and keys stay
    ``brand:p<page>``. The pass is here so that when a plan *does* place a page
    twice, the two slides carry distinct keys instead of silently colliding.
    """
    counts: dict[int, int] = {}
    numbered: list[BrandSlide] = []
    for slide in slides:
        counts[slide.page] = counts.get(slide.page, 0) + 1
        numbered.append(replace(slide, occurrence=counts[slide.page]))
    return numbered


def deck_spec_from_plan(plan: list[BrandSlide]) -> DeckSpec:
    return DeckSpec(slides=[deck_slide_from_planned(slide) for slide in plan])


def brand_deck_file_key(db: Session) -> str | None:
    """The stored location of the brand deck PDF, per the asset library.

    Only a cache miss needs this, and ``resolve_source`` can still fall back to
    the local copies, so a database hiccup must not stop a preview from
    serving pages it has already rendered.
    """
    try:
        asset = (
            db.query(Asset)
            .filter(Asset.type == "report", Asset.file_key != "")
            .filter(Asset.title.ilike("%brand deck%"))
            .first()
        )
    except SQLAlchemyError:
        return None
    return asset.file_key if asset else None


def _local_source_candidates() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[3]
    return [
        *sorted(FILES_DIR.glob("library/reports/brand-deck*.pdf")),
        *sorted(repo_root.glob("Brand Deck*.pdf")),
    ]


def resolve_source(file_key: str | None = None) -> Path:
    """Return a local path to the brand deck PDF, caching a Spaces copy."""
    cached = FILES_DIR / SOURCE_CACHE_PATH
    if cached.exists():
        return cached
    if file_key:
        candidate = Path(file_key)
        if candidate.is_absolute() and candidate.exists():
            return candidate
        if settings.uses_spaces:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(read_file(file_key))
            return cached
    for candidate in _local_source_candidates():
        if candidate.exists():
            return candidate
    raise BrandDeckUnavailable(
        "Brand deck PDF not found. Sync the 'Brand Deck' report asset before generating."
    )


_source_lock = Lock()
_source_cache: tuple[str, object] | None = None


def _open_source(file_key: str | None = None):
    global _source_cache
    import pymupdf

    path = str(resolve_source(file_key))
    with _source_lock:
        if _source_cache is None or _source_cache[0] != path:
            _source_cache = (path, pymupdf.open(path))
        return _source_cache[1]


def page_image(page: int, file_key: str | None = None) -> bytes:
    """A JPEG of one brand deck page, rasterised once and then cached on disk.

    Page images are derived from an immutable source, so they are cached
    locally rather than in object storage: a preview scrubs through dozens of
    pages and every one of those should be a disk read, not a network round
    trip. Losing the cache on redeploy only costs one re-render.
    """
    if page < 1 or page > SOURCE_PAGE_COUNT:
        raise ValueError(f"page {page} is outside the brand deck")
    cached = FILES_DIR / PAGE_CACHE_PREFIX / f"p{page:03d}.jpg"
    if cached.exists():
        return cached.read_bytes()
    document = _open_source(file_key)
    pixmap = document[page - 1].get_pixmap(dpi=IMAGE_DPI)
    data = pixmap.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return data


def render_pptx(
    plan: list[BrandSlide],
    generation_id: int,
    notes_by_module: dict[str, str] | None = None,
    notes_by_page: dict[int, str] | None = None,
    notes_by_slide_key: dict[str, str] | None = None,
    file_key: str | None = None,
) -> str:
    """Write the planned pages into a 16:9 PPTX, one full-bleed image per slide.

    Both brand-deck pages and concrete Stage 4 *generated* slides are embedded:
    a brand slide is rasterised from the source PDF, while a generated slide's
    JPEG is read from object storage by its ``file_key``. A generated slide that
    was never filled (an unrealised placeholder, ``file_key`` empty) has no image
    and is skipped, exactly as before.

    Speaker notes are looked up by ``slide_key`` first so a repeated brand page
    keeps a distinct note per occurrence; ``notes_by_page`` and
    ``notes_by_module`` remain as fallbacks for older callers.
    """
    presentation = Presentation()
    presentation.slide_width = Inches(SLIDE_W)
    presentation.slide_height = Inches(SLIDE_H)
    blank = presentation.slide_layouts[6]
    for item in plan:
        source = getattr(item, "source", BRAND_SOURCE)
        if source != BRAND_SOURCE:
            # A generated slide carries its rendered JPEG in storage; embed it
            # from the content-addressed file_key. An unrealised placeholder has
            # no image yet, so skip it (the deck stays the same length).
            generated_file_key = getattr(item, "file_key", "") or ""
            if not generated_file_key:
                continue
            image = read_file(generated_file_key)
        elif getattr(item, "page", None) is None:
            continue
        else:
            image = page_image(item.page, file_key)
        slide = presentation.slides.add_slide(blank)
        slide.shapes.add_picture(
            BytesIO(image),
            0,
            0,
            width=presentation.slide_width,
            height=presentation.slide_height,
        )
        note = (
            (notes_by_slide_key or {}).get(item.slide_key)
            or (notes_by_page or {}).get(item.page)
            or (notes_by_module or {}).get(item.module_id, "")
        ).strip()
        if note:
            slide.notes_slide.notes_text_frame.text = note
    buffer = BytesIO()
    presentation.save(buffer)
    return save_file(
        f"decks/{generation_id}.pptx",
        buffer.getvalue(),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
