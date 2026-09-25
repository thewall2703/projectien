"""Deck planning from the Deck - Vision Mapping sheet.

When a generation has a recognised ``deck_use_case``, the brand-deck spine is
the sheet's section × use-case page lists (authored order), trimmed to the
duration ceiling. Module round-robin in ``brand_deck.plan_pages`` remains the
fallback for unmapped audiences (recruiters, investors, etc.).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Sequence

from backend.pipeline.brand_deck import (
    CLOSING,
    CLOSING_PAGE,
    COVER,
    COVER_PAGE,
    MODULE_PAGES,
    PAGE_LABELS,
    PAGE_MODULES,
    BrandSlide,
    _assign_occurrences,
)
from backend.pipeline.deck import slide_ceiling_for

VISION_SHEET_NAME = " Deck - Vision Mapping"

# Stable keys used in the DB, Interpret catalog, and Generate wizard.
USE_CASES: tuple[tuple[str, str], ...] = (
    (
        "school_fair",
        "Introducing MUU at a career fair/ career counselling seminar to school students that know nothing about us",
    ),
    (
        "pg_exec_fair",
        "Introducing MUU at a career fair/ career counselling seminar to PG/ Exec aspirants (TBM, HROS, CMT, OPM, EBAP)",
    ),
    ("pgp_smg", "PGP SMG aspirant"),
    ("ug_tbm", "UG TBM aspirant"),
    ("ug_dsai", "UG DSAI aspirant"),
    ("parents_undecided", "Parents, undecided"),
    ("parents_decided", "Parents, decided"),
)

USE_CASE_KEYS = frozenset(key for key, _label in USE_CASES)
USE_CASE_LABELS = {key: label for key, label in USE_CASES}

_PAGE_TOKEN_RE = re.compile(
    r"(?P<a>\d+)\s*[-–—]\s*(?P<b>\d+)|(?P<single>\d+)",
)
# Any "slide 8", "slides 3-8", "slides 25, 26" mention in a logline.
_SLIDE_REF_RE = re.compile(
    r"\bslides?\s+(?P<pages>\d+(?:\s*[-–—]\s*\d+)?(?:\s*(?:,|&|and)\s*\d+(?:\s*[-–—]\s*\d+)?)*)",
    re.IGNORECASE,
)
_ADD_SLIDES_PLURAL_RE = re.compile(r"\badd\s+slides\b", re.IGNORECASE)
# Notes the generator cannot act on: pure visual direction, media swaps, curation.
_DESIGN_ONLY_RE = re.compile(
    r"\b("
    r"visual\s+language|different\s+image|reads\s+like\s+a\s+section\s+divider"
    r"|warmer|more\s+lively|video|film|condense"
    r")\b",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*[-•*]\s*")
_NA_RE = re.compile(r"^(n/?a|na|-|—|–|\.)$", re.IGNORECASE)


def _normalize_use_case_text(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text.replace("\u2018", "'").replace("\u2019", "'")


_USE_CASE_ALIASES: dict[str, str] = {}
for _key, _label in USE_CASES:
    _USE_CASE_ALIASES[_normalize_use_case_text(_label)] = _key
    _USE_CASE_ALIASES[_key] = _key

# Extra fuzzy aliases for Interpret / sheet drift.
_USE_CASE_ALIASES.update(
    {
        _normalize_use_case_text("school students"): "school_fair",
        _normalize_use_case_text("school student"): "school_fair",
        _normalize_use_case_text("pg/exec aspirants"): "pg_exec_fair",
        _normalize_use_case_text("pg exec aspirants"): "pg_exec_fair",
        _normalize_use_case_text("pgp smg"): "pgp_smg",
        _normalize_use_case_text("ug tbm"): "ug_tbm",
        _normalize_use_case_text("ug dsai"): "ug_dsai",
        _normalize_use_case_text("parents undecided"): "parents_undecided",
        _normalize_use_case_text("parents decided"): "parents_decided",
    }
)


def normalize_use_case(value: str | None) -> str:
    """Map a free-text or key use case to a stable key, or ``""`` if unknown."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw in USE_CASE_KEYS:
        return raw
    key = _USE_CASE_ALIASES.get(_normalize_use_case_text(raw), "")
    if key:
        return key
    # Longest alias contained in the text wins ("Parents, decided — X5 ...").
    needle = _normalize_use_case_text(raw)
    matches = [alias for alias in _USE_CASE_ALIASES if len(alias) >= 5 and alias in needle]
    if not matches:
        return ""
    return _USE_CASE_ALIASES[max(matches, key=len)]


def slides_included_to_text(value: Any) -> str:
    """Recover a page-range string from an Excel cell (dates or plain text).

    Excel often coerces ``1-11`` / ``9-11`` into dates when the cell format is
    ``d-m``. A datetime whose year looks like a spreadsheet auto-date is turned
    back into ``f"{day}-{month}"``.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return f"{value.day}-{value.month}"
    text = str(value).strip()
    if not text or _NA_RE.match(text):
        return ""
    return text


def parse_page_list(raw: Any) -> list[int]:
    """Parse an ordered page list from a vision-mapping cell.

    Accepts strings like ``"19-23, 25, 26, 49, 28"``, bare integers, Excel
    datetimes for ranges, and ``"-"`` / ``N/A`` as empty. Order is preserved;
    duplicate pages within one cell are kept only on first occurrence.
    """
    if raw is None:
        return []
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        page = int(raw)
        return [page] if page > 0 else []
    text = slides_included_to_text(raw)
    if not text:
        return []
    pages: list[int] = []
    seen: set[int] = set()
    for match in _PAGE_TOKEN_RE.finditer(text):
        if match.group("single"):
            chunk = [int(match.group("single"))]
        else:
            start = int(match.group("a"))
            end = int(match.group("b"))
            if start > end:
                start, end = end, start
            chunk = list(range(start, end + 1))
        for page in chunk:
            if page > 0 and page not in seen:
                seen.add(page)
                pages.append(page)
    return pages


def needs_more_flag(raw: Any) -> bool:
    text = "" if raw is None else str(raw).strip().lower()
    return text.startswith("y")


def is_empty_logline(raw: Any) -> bool:
    text = "" if raw is None else str(raw).strip()
    return not text or bool(_NA_RE.match(text))


# ---------------------------------------------------------------------------
# Logline → instructions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisionInstruction:
    action: str  # add | replace | design_only
    brief: str
    target_page: int | None = None
    section: str = ""
    section_order: int = 0
    source: str = "rules"  # rules | llm

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "brief": self.brief,
            "target_page": self.target_page,
            "section": self.section,
            "section_order": self.section_order,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any], **defaults: Any) -> VisionInstruction | None:
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"add", "replace", "design_only"}:
            return None
        target = payload.get("target_page")
        try:
            target_page = int(target) if target not in (None, "") else None
        except (TypeError, ValueError):
            target_page = None
        if action == "replace" and not target_page:
            return None
        return cls(
            action=action,
            brief=str(payload.get("brief") or "").strip(),
            target_page=target_page,
            section=str(payload.get("section") or defaults.get("section") or ""),
            section_order=int(payload.get("section_order") or defaults.get("section_order") or 0),
            source=str(payload.get("source") or defaults.get("source") or "rules"),
        )


def _logline_groups(logline: str) -> list[tuple[str, list[str]]]:
    """Group logline lines into ``(head, bullets)``.

    ``- item`` lines attach to the preceding head; so do plain lines that follow
    a head ending in ``:`` (a header without bullet markers), unless they name
    their own slide or are themselves a header.
    """
    groups: list[tuple[str, list[str]]] = []
    for raw in str(logline).splitlines():
        is_bullet = bool(_BULLET_RE.match(raw))
        text = _BULLET_RE.sub("", raw).strip()
        if not text or _NA_RE.match(text):
            continue
        if groups and is_bullet:
            groups[-1][1].append(text)
            continue
        if (
            groups
            and groups[-1][0].endswith(":")
            and not text.endswith(":")
            and not _SLIDE_REF_RE.search(text)
        ):
            groups[-1][1].append(text)
            continue
        groups.append((text, []))
    return groups


def _slide_ref_pages(text: str) -> list[int]:
    pages: list[int] = []
    for match in _SLIDE_REF_RE.finditer(text):
        for page in parse_page_list(match.group("pages")):
            if page not in pages:
                pages.append(page)
    return pages


def _group_brief(head: str, bullets: Sequence[str]) -> str:
    head = head.rstrip(":").strip()
    return f"{head}: {'; '.join(bullets)}" if bullets else head


def _classify_group(head: str, bullets: Sequence[str]) -> list[VisionInstruction]:
    brief = _group_brief(head, bullets)
    design_head = bool(_DESIGN_ONLY_RE.search(head))
    pages = _slide_ref_pages(head)
    if pages:
        action = "design_only" if design_head else "replace"
        return [VisionInstruction(action=action, brief=brief, target_page=page) for page in pages]
    if bullets and _ADD_SLIDES_PLURAL_RE.search(head):
        prefix = head.rstrip(":").strip()
        return [
            VisionInstruction(
                action="design_only" if _DESIGN_ONLY_RE.search(bullet) else "add",
                brief=f"{prefix}: {bullet}",
            )
            for bullet in bullets
        ]
    return [VisionInstruction(action="design_only" if design_head else "add", brief=brief)]


def classify_logline(logline: str) -> list[VisionInstruction]:
    """Deterministic classification of a vision-mapping logline.

    Each head line and its bullets form one instruction: a head naming slides
    (``Redo slide 55``, ``Slide 67: ...``, ``slides 25, 26``) replaces those
    pages; anything else is one new slide whose brief is the head plus bullets.
    ``add slides on:`` heads fan out into one slide per bullet. Bullets that
    name their own slide become separate instructions. Visual, media and
    curation notes (visual language, different image, video, condense) are
    ``design_only`` and ignored by the planner.
    """
    if is_empty_logline(logline):
        return []
    instructions: list[VisionInstruction] = []
    for head, bullets in _logline_groups(logline):
        own = [bullet for bullet in bullets if _SLIDE_REF_RE.search(bullet)]
        rest = [bullet for bullet in bullets if bullet not in own]
        instructions.extend(_classify_group(head, rest))
        for bullet in own:
            instructions.extend(_classify_group(bullet, []))
    return _dedupe_instructions(instructions)


def _dedupe_instructions(items: Sequence[VisionInstruction]) -> list[VisionInstruction]:
    seen: set[tuple[str, int | None, str]] = set()
    result: list[VisionInstruction] = []
    for item in items:
        key = (item.action, item.target_page, item.brief[:120].lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def classify_logline_with_llm(logline: str) -> list[VisionInstruction]:
    """Optional LLM pass; falls back to :func:`classify_logline` on failure."""
    deterministic = classify_logline(logline)
    if is_empty_logline(logline):
        return deterministic
    try:
        from backend.config import settings
        from backend.pipeline.llm import chat_json

        if not settings.openrouter_api_key:
            return deterministic
        raw = chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "You turn a deck design logline into structured slide instructions. "
                        "Return strict JSON: "
                        '{"instructions":[{"action":"add|replace|design_only","target_page":null|int,"brief":""}]}. '
                        "action=replace when an existing brand slide must be redone (needs target_page). "
                        "action=add when a new slide is requested. "
                        "action=design_only for visual/layout-only notes the engine should ignore "
                        "(different image, visual language, section-divider look). "
                        "Split multi-bullet loglines into one instruction each."
                    ),
                },
                {"role": "user", "content": logline},
            ],
            timeout=20.0,
            model=settings.openrouter_interpret_model,
            reasoning=False,
            max_tokens=800,
        )
    except Exception:  # noqa: BLE001
        return deterministic
    if not isinstance(raw, dict):
        return deterministic
    parsed: list[VisionInstruction] = []
    for item in raw.get("instructions") or []:
        if isinstance(item, dict):
            instruction = VisionInstruction.from_dict(item, source="llm")
            if instruction is not None:
                parsed.append(instruction)
    return _dedupe_instructions(parsed) if parsed else deterministic


def parse_instructions_json(
    raw: str,
    *,
    section: str = "",
    section_order: int = 0,
) -> list[VisionInstruction]:
    if not (raw or "").strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    result: list[VisionInstruction] = []
    for item in payload:
        if isinstance(item, dict):
            instruction = VisionInstruction.from_dict(
                item, section=section, section_order=section_order
            )
            if instruction is not None:
                result.append(instruction)
    return result


def instructions_to_json(items: Sequence[VisionInstruction]) -> str:
    return json.dumps([item.to_dict() for item in items], ensure_ascii=False)


# ---------------------------------------------------------------------------
# Sheet row parsing (seed)
# ---------------------------------------------------------------------------


@dataclass
class VisionSheetRow:
    section: str
    section_order: int
    use_case: str
    pages: list[int]
    section_pages: list[int]
    needs_more: bool
    logline: str
    design_status: str
    slide_status: str


def parse_vision_sheet_rows(rows: Sequence[Sequence[Any]]) -> list[VisionSheetRow]:
    """Parse workbook rows from the vision-mapping sheet into structured rows.

    Forward-fills merged ``Titles / Section`` and ``Slides Included`` cells.
    Section order is the order distinct section titles first appear.
    """
    if not rows:
        return []
    body = list(rows[1:]) if rows else []
    last_section = ""
    last_section_pages_raw: Any = ""
    section_order_by_title: dict[str, int] = {}
    next_order = 0
    parsed: list[VisionSheetRow] = []

    for row in body:
        cells = list(row) + [None] * max(0, 9 - len(row))
        section_raw = cells[1]
        section_pages_raw = cells[2]
        use_case_raw = cells[3]
        relevant_raw = cells[4]
        needs_raw = cells[5]
        logline_raw = cells[6]
        design_raw = cells[7]
        status_raw = cells[8]

        section = str(section_raw).strip().strip('"') if section_raw not in (None, "") else ""
        if section:
            last_section = section
        else:
            section = last_section
        if not section:
            continue

        if section_pages_raw not in (None, ""):
            last_section_pages_raw = section_pages_raw
        section_pages = parse_page_list(last_section_pages_raw)
        use_case = normalize_use_case(str(use_case_raw or ""))
        if not use_case:
            continue
        if section not in section_order_by_title:
            section_order_by_title[section] = next_order
            next_order += 1

        logline = "" if is_empty_logline(logline_raw) else str(logline_raw).strip()
        parsed.append(
            VisionSheetRow(
                section=section,
                section_order=section_order_by_title[section],
                use_case=use_case,
                pages=parse_page_list(relevant_raw),
                section_pages=section_pages,
                needs_more=needs_more_flag(needs_raw),
                logline=logline,
                design_status="" if design_raw is None else str(design_raw).strip(),
                slide_status="" if status_raw is None else str(status_raw).strip(),
            )
        )
    return parsed


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class VisionSectionPlan:
    section: str
    section_order: int
    pages: list[int] = field(default_factory=list)
    instructions: list[VisionInstruction] = field(default_factory=list)
    # Every brand page the sheet lists for the section, for any use case.
    section_pages: list[int] = field(default_factory=list)


@dataclass
class VisionPlanResult:
    slides: list[BrandSlide]
    module_sequence: list[str]
    instructions: list[VisionInstruction]
    use_case: str
    sections: list[VisionSectionPlan] = field(default_factory=list)


def _page_rank(page: int) -> tuple[int, int]:
    """Lower is better. Uses MODULE_PAGES best-first order; unranked last."""
    module_id = PAGE_MODULES.get(page, "")
    entries = MODULE_PAGES.get(module_id) or ()
    for index, (candidate, _label) in enumerate(entries):
        if candidate == page:
            return (0, index)
    return (1, page)


def _trim_section_pages(pages: list[int], keep: int) -> list[int]:
    if keep <= 0 or not pages:
        return []
    if keep >= len(pages):
        return list(pages)
    ranked = sorted(pages, key=_page_rank)
    keep_set = set(ranked[:keep])
    return [page for page in pages if page in keep_set]


def _allocate_slots(sizes: list[int], budget: int) -> list[int]:
    """Split ``budget`` across sections in proportion to size (D'Hondt).

    Every non-empty section gets one slot first; when the budget cannot cover
    that, the largest sections (earliest on ties) win. No section is given
    more slots than it has pages.
    """
    allot = [0] * len(sizes)
    nonempty = [index for index, size in enumerate(sizes) if size > 0]
    if budget <= 0 or not nonempty:
        return allot
    if budget < len(nonempty):
        for index in sorted(nonempty, key=lambda i: (-sizes[i], i))[:budget]:
            allot[index] = 1
        return allot
    for index in nonempty:
        allot[index] = 1
    for _ in range(budget - len(nonempty)):
        open_sections = [index for index in nonempty if allot[index] < sizes[index]]
        if not open_sections:
            break
        best = max(open_sections, key=lambda i: (sizes[i] / (allot[i] + 1), -i))
        allot[best] += 1
    return allot


def module_sequence_from_pages(pages: Sequence[int]) -> list[str]:
    sequence: list[str] = []
    seen: set[str] = set()
    for page in pages:
        module_id = PAGE_MODULES.get(page, "")
        if module_id and module_id not in seen:
            seen.add(module_id)
            sequence.append(module_id)
    if "M14" not in seen:
        sequence.append("M14")
    return sequence


def module_sequence_from_slides(slides: Sequence[Any]) -> list[str]:
    """Module order carried by a deck's body slides, closing with M14."""
    sequence: list[str] = []
    for slide in list(slides)[1:-1]:
        module_id = getattr(slide, "module_id", "") or ""
        if module_id and module_id not in sequence:
            sequence.append(module_id)
    if "M14" not in sequence:
        sequence.append("M14")
    return sequence


def plan_vision_pages(
    sections: Sequence[VisionSectionPlan],
    duration: str,
    *,
    use_case: str = "",
    ceiling: int | None = None,
) -> VisionPlanResult:
    """Build a brand-deck plan from vision-mapping sections.

    Concatenates each section's pages in authored order, drops duplicate pages
    on first occurrence, bookends with cover/closing, then trims to the duration
    ceiling while keeping every non-empty section represented.
    """
    budget = slide_ceiling_for(duration) if ceiling is None else max(3, ceiling)
    # Body budget excludes cover + closing.
    body_budget = max(1, budget - 2)

    # Dedupe across sections (first occurrence wins).
    # Cover and closing are bookends, never body slots.
    seen_pages: set[int] = {COVER_PAGE, CLOSING_PAGE}
    cleaned: list[VisionSectionPlan] = []
    for section in sections:
        pages: list[int] = []
        for page in section.pages:
            if page not in seen_pages:
                seen_pages.add(page)
                pages.append(page)
        cleaned.append(replace(section, pages=pages, instructions=list(section.instructions)))

    sizes = [len(item.pages) for item in cleaned]
    total_body = sum(sizes)
    if total_body <= body_budget:
        kept = [list(item.pages) for item in cleaned]
    else:
        allotments = _allocate_slots(sizes, body_budget)
        kept = [
            _trim_section_pages(item.pages, allotments[index])
            for index, item in enumerate(cleaned)
        ]

    slides: list[BrandSlide] = [
        replace(COVER, section=cleaned[0].section if cleaned else ""),
    ]
    body_pages: list[int] = []
    for section, pages in zip(cleaned, kept):
        for page in pages:
            body_pages.append(page)
            slides.append(
                BrandSlide(
                    page=page,
                    module_id=PAGE_MODULES.get(page, ""),
                    label=PAGE_LABELS.get(page, f"Page {page}"),
                    section=section.section,
                )
            )
    slides.append(replace(CLOSING, section=cleaned[-1].section if cleaned else ""))
    slides = _assign_occurrences(slides)

    instructions: list[VisionInstruction] = []
    for section in cleaned:
        for item in section.instructions:
            if item.action == "design_only":
                continue
            instructions.append(
                replace(
                    item,
                    section=section.section,
                    section_order=section.section_order,
                )
            )

    return VisionPlanResult(
        slides=slides,
        module_sequence=module_sequence_from_pages(body_pages),
        instructions=instructions,
        use_case=use_case,
        sections=[replace(item, pages=kept[index]) for index, item in enumerate(cleaned)],
    )


def sections_from_db_rows(rows: Sequence[Any]) -> list[VisionSectionPlan]:
    """Group DB ``DeckVisionRow`` objects for one use case into section plans."""
    by_order: dict[int, VisionSectionPlan] = {}
    for row in sorted(rows, key=lambda item: (int(getattr(item, "section_order", 0) or 0), int(getattr(item, "id", 0) or 0))):
        order = int(getattr(row, "section_order", 0) or 0)
        section = str(getattr(row, "section", "") or "")
        page_list = _json_page_list(getattr(row, "pages_json", ""))
        instructions = parse_instructions_json(
            getattr(row, "instructions_json", "") or "",
            section=section,
            section_order=order,
        )
        # LLM classifications are cached at seed time; rule-based ones are
        # recomputed so classifier fixes apply without a reseed.
        if not any(item.source == "llm" for item in instructions):
            instructions = [
                replace(item, section=section, section_order=order)
                for item in classify_logline(getattr(row, "logline", "") or "")
            ]
        existing = by_order.get(order)
        if existing is None:
            by_order[order] = VisionSectionPlan(
                section=section,
                section_order=order,
                pages=page_list,
                instructions=instructions,
                section_pages=_json_page_list(getattr(row, "section_pages_json", "")),
            )
        else:
            # Same section_order should be unique per use case; merge defensively.
            for page in page_list:
                if page not in existing.pages:
                    existing.pages.append(page)
            existing.instructions.extend(instructions)
    return [by_order[key] for key in sorted(by_order)]


def _json_page_list(raw: Any) -> list[int]:
    try:
        pages = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(pages, list):
        return []
    return [int(page) for page in pages if isinstance(page, int) or str(page).isdigit()]


def empty_sections(result: VisionPlanResult) -> list[VisionSectionPlan]:
    """Sections the use case leaves blank (no brand pages mapped)."""
    return [section for section in result.sections if not section.pages]


def insert_at_section(
    plan: Sequence[Any],
    slides: Sequence[Any],
    result: VisionPlanResult,
    section_order: int,
) -> list[Any]:
    """Insert ``slides`` where section ``section_order`` sits in the deck.

    That is after the last slide of any earlier section, else right after the
    cover; never after the closing.
    """
    if not slides:
        return list(plan)
    earlier = {
        section.section for section in result.sections if section.section_order < section_order
    }
    closing_index = max(len(plan) - 1, 1)
    index = 1
    for position in range(1, closing_index):
        if getattr(plan[position], "section", "") in earlier:
            index = position + 1
    return [*plan[:index], *slides, *plan[index:]]


def load_vision_sections(db: Any, use_case: str) -> list[VisionSectionPlan]:
    key = normalize_use_case(use_case)
    if not key:
        return []
    from backend.models import DeckVisionRow

    rows = (
        db.query(DeckVisionRow)
        .filter(DeckVisionRow.use_case == key)
        .order_by(DeckVisionRow.section_order, DeckVisionRow.id)
        .all()
    )
    return sections_from_db_rows(rows)
