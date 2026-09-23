"""Stage 3 of the Generated Slide Engine: find the *true* gaps in a planned
Brand-Deck deck and, only where the brand deck genuinely cannot answer, plant a
typed *generated* placeholder for a later stage to fill and render.

The planner (:mod:`brand_deck`) already lays out a deck out of real brand pages.
That deck is on-brand by construction, but it can still miss an argument the
recipe, the salesperson's context note, a locked fact, or a live objection
demands — and the brand deck simply has no page for it (the canonical example is
M13's "here is what we are *not*": the deck's closest pages are the recognition
record, which answer a different question).

This module turns that observation into a small, deterministic pipeline:

1. **Detect** candidate gaps deterministically from four sources: recipe modules
   with no selected carrier slide, claims in the context note, relevant *verified*
   locked facts with no selected carrier, and the ranked objections for the
   persona/use case.
2. **RULE ZERO — prefer real brand slides, always.** Before anything is
   generated, look for an *unused* brand page that already answers the gap and
   place it (insert under the duration ceiling, else swap the weakest body page).
   Only gaps the brand deck cannot cover survive to generation.
3. **Rank + template (Haiku only).** :data:`settings.openrouter_slide_model`
   ranks the *surviving* real gaps and picks a currently-supported template.
   The model never detects gaps and never overrides RULE ZERO; if it is
   unavailable or misbehaves the deterministic order/template stand in.
4. **Plant placeholders.** Each surviving gap becomes a
   :class:`GeneratedSlidePlaceholder` (satisfying
   :class:`~backend.pipeline.deck.PlannedSlide`) *and* is woven into the script
   flow as a synthetic topic, so the existing script generator writes a spoken
   beat for it. Placeholders carry no brand page and enough claim metadata for
   Stage 4 to fill and render them. An evidence brand page may sit immediately
   after the placeholder when one exists and fits under the ceiling.

Two invariants keep the deck safe:

* **The deck grows up to the duration ceiling.** Under the ceiling a real answer
  page or generated placeholder is *inserted* (with optional evidence). At the
  ceiling a gap fill replaces the weakest planned body page, and unused evidence
  is skipped rather than pushing past the limit. The cover and closing are never
  touched, and a page that is the sole carrier of a recipe module is never
  removed or moved (that would just open a new gap).
* **Nothing changes when there is no true gap.** With no surviving gap — or at
  T0, whose generated budget is zero — the plan is returned exactly as planned.

Nothing here renders a slide; Stage 4 fills and renders the placeholders.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Sequence

from backend.config import settings
from backend.pipeline.brand_deck import (
    CLOSING_PAGE,
    COVER_PAGE,
    MODULE_PAGES,
    PAGE_LABELS,
    PAGE_MODULES,
    BrandSlide,
)
from backend.pipeline.deck import (
    BRAND_SOURCE,
    GENERATED_SOURCE,
    PlannedSlide,
    slide_ceiling_for,
    slide_count_for,
)
from backend.pipeline.slide_templates import (
    PHOTO_REQUIRED_TEMPLATE_IDS,
    SECTION_DIVIDER_TEMPLATE_ID,
    template_spec,
)
from backend.pipeline.slide_templates import (
    SUPPORTED_TEMPLATE_IDS as _ALL_TEMPLATE_IDS,
)

# The templates Stage 4 can render today. The model may only pick from these;
# the registry (:mod:`slide_templates`) is the single source of truth so a new
# template becomes selectable the moment it is registered — never registered
# but unusable.
SUPPORTED_TEMPLATE_IDS: tuple[str, ...] = tuple(_ALL_TEMPLATE_IDS)
# A safe, always-renderable, photo-free default (a section divider) used
# whenever the model is unavailable or returns an unsupported template.
DEFAULT_TEMPLATE_ID = f"{SECTION_DIVIDER_TEMPLATE_ID}-light"
# Templates with no mandatory photo — the only ones the planner may choose when
# the recipe has no approved recommended picture to draw on.
NO_PHOTO_TEMPLATE_IDS: tuple[str, ...] = tuple(
    tid for tid in SUPPORTED_TEMPLATE_IDS if tid not in PHOTO_REQUIRED_TEMPLATE_IDS
)


def allowed_template_ids(allow_photo_templates: bool) -> tuple[str, ...]:
    """The template ids the planner may choose, gated on photo availability.

    When the recipe has no approved recommended picture, photo-required
    templates (the campus photo slide) are removed from the allowed set so the
    planner never plants a placeholder that could only degrade — it picks a
    no-photo template instead.
    """
    return SUPPORTED_TEMPLATE_IDS if allow_photo_templates else NO_PHOTO_TEMPLATE_IDS

# How many *generated* slides a deck may carry, by duration. Zero at T0 (a
# 30-second pitch is left exactly as planned).
def generated_slide_budget(duration: str) -> int:
    """Max generated slides for ``duration`` — ``slide_count // 8``, 0 at T0."""
    if duration == "T0":
        return 0
    return slide_count_for(duration) // 8


# ---------------------------------------------------------------------------
# Text helpers (deterministic keyword matching)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SENTENCE_RE = re.compile(r"[.!?\n]+")
_NUMERAL_RE = re.compile(r"\d")

# Words that carry no discriminating signal for gap matching: ordinary stop
# words plus a few brand-ubiquitous tokens that appear on nearly every page and
# would otherwise make unrelated claims look "covered".
_STOPWORDS = {
    "about", "above", "after", "again", "against", "already", "also", "another",
    "because", "been", "before", "being", "between", "both", "cannot", "could",
    "does", "doing", "done", "down", "during", "each", "else", "even", "ever",
    "every", "from", "have", "having", "here", "into", "just", "keep", "keeps",
    "like", "made", "make", "many", "more", "most", "much", "must", "need",
    "nots", "only", "other", "over", "same", "should", "since", "some", "such",
    "than", "that", "their", "them", "then", "there", "these", "they", "thing",
    "this", "those", "through", "under", "until", "very", "want", "were", "what",
    "when", "where", "which", "while", "will", "with", "without", "would", "your",
    # brand-ubiquitous, non-discriminating
    "masters", "union", "mastersunion",
}


def _salient_tokens(text: str) -> frozenset[str]:
    """Lowercase content tokens (length >= 4, not stop words) of ``text``."""
    return frozenset(
        token
        for token in _TOKEN_RE.findall((text or "").lower())
        if len(token) >= 4 and token not in _STOPWORDS
    )


def _shares(a: frozenset[str], b: frozenset[str], minimum: int = 2) -> bool:
    """True when ``a`` and ``b`` overlap enough to be "the same argument".

    Short claims (one or two salient tokens) match on a single shared token so a
    terse but pointed claim is not permanently un-coverable.
    """
    shared = len(a & b)
    if len(a) <= 2:
        return shared >= 1
    return shared >= minimum


def _split_claims(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_RE.split(text or "") if part.strip()]


def _short_title(text: str, max_words: int = 6) -> str:
    words = [word for word in (text or "").split() if word]
    title = " ".join(words[:max_words]).strip(" ,;:–—-")
    return title or "Generated slide"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "gap"


def _is_brand(slide: Any) -> bool:
    return (
        getattr(slide, "source", BRAND_SOURCE) == BRAND_SOURCE
        and getattr(slide, "page", None) is not None
    )


def _split_ids(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Placeholder & result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneratedSlidePlaceholder:
    """A planned-but-not-yet-rendered generated slide.

    Satisfies :class:`~backend.pipeline.deck.PlannedSlide` (``source`` is
    ``"generated"`` and ``page`` is ``None`` — never a fake brand page) and
    carries the claim metadata Stage 4 needs to fill and render it. The
    ``slide_key`` is stable and URL-safe so notes, previews and edits can address
    it exactly like a brand page.
    """

    slide_key: str
    gap_kind: str
    claim: str
    title: str
    module_id: str = ""
    template_id: str = DEFAULT_TEMPLATE_ID
    tone: str = "light"
    subtitle: str = ""
    summary: str = ""
    recipe_modules: tuple[str, ...] = ()
    source_fact_ids: tuple[int, ...] = ()
    source_asset_ids: tuple[int, ...] = ()
    source_objection_ids: tuple[int, ...] = ()
    keywords: tuple[str, ...] = ()

    # The real brand page this placeholder displaced when it was planted. Stage
    # 4 restores exactly this page if the slide cannot be generated on brand, so
    # a failed fill degrades to the original brand slide rather than a gap.
    # ``None`` when the placeholder was *inserted* under the ceiling (no page
    # displaced) — Stage 4 then drops the slide on failure.
    replaced_page: int | None = None
    replaced_module_id: str = ""
    replaced_label: str = ""
    # Brand page that sits immediately after this placeholder as supporting
    # evidence. ``None`` when no evidence was placed (or it was skipped at the
    # ceiling / blocked as a sole carrier).
    evidence_page: int | None = None

    # -- PlannedSlide interface --------------------------------------------
    @property
    def source(self) -> str:
        return GENERATED_SOURCE

    @property
    def page(self) -> int | None:
        return None

    @property
    def image_url(self) -> str:
        # No image yet — Stage 4 renders and caches it.
        return ""

    def claim_metadata(self) -> dict[str, Any]:
        """The self-contained brief Stage 4 fills and renders from."""
        return {
            "slide_key": self.slide_key,
            "gap_kind": self.gap_kind,
            "claim": self.claim,
            "title": self.title,
            "subtitle": self.subtitle,
            "module_id": self.module_id,
            "template_id": self.template_id,
            "tone": self.tone,
            "recipe_modules": list(self.recipe_modules),
            "source_fact_ids": list(self.source_fact_ids),
            "source_asset_ids": list(self.source_asset_ids),
            "source_objection_ids": list(self.source_objection_ids),
            "keywords": list(self.keywords),
        }


@dataclass(frozen=True)
class GapCandidate:
    """One deterministically-detected candidate gap, before RULE ZERO."""

    kind: str
    key: str
    claim: str
    title: str
    keywords: frozenset[str]
    priority: tuple[int, int]
    modules: tuple[str, ...] = ()
    source_fact_ids: tuple[int, ...] = ()
    source_asset_ids: tuple[int, ...] = ()
    source_objection_ids: tuple[int, ...] = ()


@dataclass
class GapPlanResult:
    """Outcome of :func:`build_gap_plan`."""

    plan: list[PlannedSlide]
    generated: list[GeneratedSlidePlaceholder] = field(default_factory=list)
    swapped_pages: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

# Kind priority: lower fills first. Recipe-module coverage is the most
# structural, then live objections, then verified facts, then the softer
# context-note claims.
_KIND_PRIORITY = {"module": 0, "objection": 1, "fact": 2, "context": 3}


def _selected_label_tokens(plan: Sequence[Any]) -> list[frozenset[str]]:
    tokens: list[frozenset[str]] = []
    for slide in plan:
        if not _is_brand(slide):
            continue
        label = getattr(slide, "title", "") or PAGE_LABELS.get(slide.page, "")
        toks = _salient_tokens(label)
        if toks:
            tokens.append(toks)
    return tokens


def _carried_by_selection(keywords: frozenset[str], label_tokens: list[frozenset[str]]) -> bool:
    """True when a *selected* slide already argues this claim."""
    return any(_shares(keywords, tokens) for tokens in label_tokens)


def _associate_module(
    keywords: frozenset[str],
    module_info: dict[str, Any],
    sequence_order: list[str],
) -> str:
    """Best recipe module for a free-text claim, by job/name keyword overlap."""
    best = ""
    best_score = 0
    for module_id in sequence_order:
        info = module_info.get(module_id)
        if info is None:
            continue
        toks = _salient_tokens(f"{getattr(info, 'name', '')} {getattr(info, 'job', '')}")
        score = len(keywords & toks)
        if score > best_score:
            best_score = score
            best = module_id
    return best if best_score >= 1 else ""


def detect_gaps(
    plan: Sequence[Any],
    sequence_order: list[str],
    *,
    context_note: str,
    objections: Sequence[Any],
    facts: Sequence[Any],
    module_info: dict[str, Any],
) -> list[GapCandidate]:
    """Deterministically enumerate candidate gaps from all four sources."""
    sequence_set = set(sequence_order)
    carried_modules = {
        slide.module_id
        for slide in plan
        if _is_brand(slide) and getattr(slide, "module_id", "")
    }
    label_tokens = _selected_label_tokens(plan)
    candidates: list[GapCandidate] = []

    # 1) Recipe modules with no selected carrier slide.
    for index, module_id in enumerate(sequence_order):
        if module_id in carried_modules:
            continue
        info = module_info.get(module_id)
        claim = ((getattr(info, "job", "") or getattr(info, "name", "")) if info else "").strip()
        claim = claim or f"Cover the {module_id} argument"
        keywords = _salient_tokens(claim) or frozenset({module_id.lower()})
        candidates.append(
            GapCandidate(
                kind="module",
                key=f"module-{module_id.lower()}",
                claim=claim,
                title=(getattr(info, "name", "") if info else "").strip() or module_id,
                keywords=keywords,
                priority=(_KIND_PRIORITY["module"], index),
                modules=(module_id,),
            )
        )

    # 2) Ranked objections not answered by any selected slide.
    for rank, obj in enumerate(objections):
        question = (getattr(obj, "question", "") or "").strip()
        if not question:
            continue
        keywords = _salient_tokens(f"{question} {getattr(obj, 'move', '')}")
        if not keywords or _carried_by_selection(keywords, label_tokens):
            continue
        module_id = _associate_module(keywords, module_info, sequence_order)
        candidates.append(
            GapCandidate(
                kind="objection",
                key=f"objection-{getattr(obj, 'id', rank)}",
                claim=question,
                title=_short_title(question),
                keywords=keywords,
                priority=(_KIND_PRIORITY["objection"], rank),
                modules=(module_id,) if module_id else (),
                source_objection_ids=(int(getattr(obj, "id", 0)),) if getattr(obj, "id", 0) else (),
            )
        )

    # 3) Relevant *verified* locked facts with no selected carrier.
    for fact in facts:
        if (getattr(fact, "status", "") or "") != "verified":
            continue
        fact_modules = [mid for mid in _split_ids(getattr(fact, "module_ids", "")) if mid in sequence_set]
        if not fact_modules or any(mid in carried_modules for mid in fact_modules):
            continue
        name = (getattr(fact, "fact", "") or "").strip()
        value = (getattr(fact, "value", "") or "").strip()
        claim = f"{name}: {value}".strip(": ").strip()
        keywords = _salient_tokens(f"{name} {value}") or frozenset({fact_modules[0].lower()})
        candidates.append(
            GapCandidate(
                kind="fact",
                key=f"fact-{getattr(fact, 'id', name)}",
                claim=claim or name,
                title=_short_title(name or value),
                keywords=keywords,
                priority=(_KIND_PRIORITY["fact"], int(getattr(fact, "id", 0) or 0)),
                modules=tuple(fact_modules),
                source_fact_ids=(int(getattr(fact, "id", 0)),) if getattr(fact, "id", 0) else (),
            )
        )

    # 4) Claims in the context note not answered by any selected slide.
    for index, sentence in enumerate(_split_claims(context_note)):
        keywords = _salient_tokens(sentence)
        if len(keywords) < 2 or _carried_by_selection(keywords, label_tokens):
            continue
        module_id = _associate_module(keywords, module_info, sequence_order)
        candidates.append(
            GapCandidate(
                kind="context",
                key=f"context-{index}",
                claim=sentence,
                title=_short_title(sentence),
                keywords=keywords,
                priority=(_KIND_PRIORITY["context"], index),
                modules=(module_id,) if module_id else (),
            )
        )

    return _dedupe(candidates)


def _dedupe(candidates: list[GapCandidate]) -> list[GapCandidate]:
    """Drop lower-priority candidates that target an already-claimed hole."""
    kept: list[GapCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.priority):
        clash = False
        for other in kept:
            if candidate.modules and set(candidate.modules) & set(other.modules):
                clash = True
                break
            if len(candidate.keywords & other.keywords) >= 3:
                clash = True
                break
        if not clash:
            kept.append(candidate)
    return kept


# ---------------------------------------------------------------------------
# RULE ZERO — prefer a real, unused brand page
# ---------------------------------------------------------------------------


def rule_zero_page(
    candidate: GapCandidate,
    used_pages: set[int],
) -> int | None:
    """Find an *unused* brand page that already answers ``candidate``.

    A recipe-module gap is answered by that module's best unused page. Every
    other gap needs a page whose label genuinely overlaps the claim; when the
    brand deck has no such page (the M13 "what we are not" case) this returns
    ``None`` and the gap survives to generation.
    """
    if candidate.kind == "module":
        for page, _label in MODULE_PAGES.get(candidate.modules[0], ()):
            if page not in used_pages:
                return page
        return None

    search_modules = list(candidate.modules) or list(MODULE_PAGES.keys())
    best_page: int | None = None
    best_score = 0
    for module_id in search_modules:
        for page, label in MODULE_PAGES.get(module_id, ()):
            if page in used_pages:
                continue
            score = len(candidate.keywords & _salient_tokens(label))
            if score >= 2 and score > best_score:
                best_score = score
                best_page = page
    return best_page


# ---------------------------------------------------------------------------
# Meaning-based answer / evidence matching (non-module gaps)
# ---------------------------------------------------------------------------

MatchFn = Callable[[dict[str, Any]], dict[str, Any]]

_MATCH_KINDS = frozenset({"objection", "context", "fact"})

_MATCH_SYSTEM = (
    "You match pitch-deck gaps to brand-deck pages by meaning. "
    "A page is an 'answer_page' only if it directly answers the gap's "
    "claim/question on its own. An 'evidence_page' supports or backs up the "
    "answer (proof, numbers, examples) without fully answering it. "
    "Use null when none fits. Prefer pages not already in the deck for "
    "evidence. Return strict JSON: "
    '{"gaps":[{"key":"...","answer_page":null,"evidence_page":null}]}.'
)


@dataclass(frozen=True)
class AnswerMatch:
    """Pages that answer / evidence a non-module gap, or ``None`` for either."""

    answer_page: int | None
    evidence_page: int | None


def _catalogue_pages(used_pages: set[int]) -> list[dict[str, Any]]:
    """Brand pages the matcher may cite, excluding cover and closing."""
    rows: list[dict[str, Any]] = []
    for page in sorted(PAGE_LABELS):
        if page in (COVER_PAGE, CLOSING_PAGE):
            continue
        rows.append(
            {
                "page": page,
                "label": PAGE_LABELS[page],
                "module": PAGE_MODULES.get(page, ""),
                "in_deck": page in used_pages,
            }
        )
    return rows


def _coerce_match_page(value: Any) -> int | None:
    """Accept only a real catalogue body page; anything else is ignored."""
    if type(value) is not int:
        return None
    if value not in PAGE_LABELS or value in (COVER_PAGE, CLOSING_PAGE):
        return None
    return value


def _keyword_evidence_page(
    candidate: GapCandidate,
    used_pages: set[int],
) -> int | None:
    """Best unused page by keyword overlap (score >= 1) for evidence."""
    search_modules = list(candidate.modules) or list(MODULE_PAGES.keys())
    best_page: int | None = None
    best_score = 0
    for module_id in search_modules:
        for page, label in MODULE_PAGES.get(module_id, ()):
            if page in used_pages:
                continue
            score = len(candidate.keywords & _salient_tokens(label))
            if score >= 1 and score > best_score:
                best_score = score
                best_page = page
    return best_page


def _fallback_answer_matches(
    candidates: Sequence[GapCandidate],
    used_pages: set[int],
) -> dict[str, AnswerMatch]:
    """Deterministic answer/evidence when the model is unavailable."""
    matches: dict[str, AnswerMatch] = {}
    for candidate in candidates:
        answer = rule_zero_page(candidate, used_pages)
        evidence_used = set(used_pages)
        if answer is not None:
            evidence_used.add(answer)
        evidence = _keyword_evidence_page(candidate, evidence_used)
        if evidence is not None and evidence == answer:
            evidence = None
        matches[candidate.key] = AnswerMatch(answer, evidence)
    return matches


def _parse_answer_matches(
    payload: Any,
    candidates: Sequence[GapCandidate],
) -> dict[str, AnswerMatch] | None:
    """Validate a model match payload, or ``None`` when the shape is unusable."""
    if not isinstance(payload, dict):
        return None
    rows = payload.get("gaps")
    if not isinstance(rows, list):
        return None
    parsed: dict[str, AnswerMatch] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "")
        if not key:
            continue
        answer = _coerce_match_page(row.get("answer_page"))
        evidence = _coerce_match_page(row.get("evidence_page"))
        if evidence is not None and evidence == answer:
            evidence = None
        parsed[key] = AnswerMatch(answer, evidence)
    # Missing keys in an otherwise valid response stay unanswered — no
    # per-candidate keyword fallback.
    return {
        candidate.key: parsed.get(candidate.key, AnswerMatch(None, None))
        for candidate in candidates
    }


def match_answer_pages(
    candidates: Sequence[GapCandidate],
    used_pages: set[int],
    match_fn: MatchFn | None,
) -> dict[str, AnswerMatch]:
    """Match non-module gaps to answer/evidence pages by meaning.

    Only candidates with ``kind`` in {objection, context, fact} are considered;
    module gaps keep the existing keyword :func:`rule_zero_page` path. When
    ``match_fn`` is ``None``, raises, or returns a malformed payload, each
    candidate falls back to :func:`rule_zero_page` for the answer and keyword
    overlap for evidence.
    """
    eligible = [candidate for candidate in candidates if candidate.kind in _MATCH_KINDS]
    if not eligible:
        return {}
    if match_fn is None:
        return _fallback_answer_matches(eligible, used_pages)

    payload = {
        "gaps": [
            {
                "key": candidate.key,
                "kind": candidate.kind,
                "claim": candidate.claim,
                "title": candidate.title,
                "modules": list(candidate.modules),
            }
            for candidate in eligible
        ],
        "pages": _catalogue_pages(used_pages),
    }
    try:
        response = match_fn(payload)
    except Exception:  # noqa: BLE001 - any model failure -> keyword fallback
        return _fallback_answer_matches(eligible, used_pages)
    parsed = _parse_answer_matches(response, eligible)
    if parsed is None:
        return _fallback_answer_matches(eligible, used_pages)
    return parsed


def _default_model_match(payload: dict[str, Any]) -> dict[str, Any]:
    from backend.pipeline.llm import chat_json

    return chat_json(
        [
            {"role": "system", "content": _MATCH_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        timeout=45.0,
        model=settings.openrouter_slide_model,
        reasoning=False,
        max_tokens=800,
    )


# ---------------------------------------------------------------------------
# Placement helpers (insert under ceiling, else replace weakest)
# ---------------------------------------------------------------------------


def _insert_index_for_modules(
    plan: Sequence[Any],
    modules: tuple[str, ...],
) -> int:
    """Index after the last body slide of ``modules[0]``, else before the closing.

    The closing slide carries M14, so the search stops short of it: nothing is
    ever inserted after the closing.
    """
    closing_index = max(len(plan) - 1, 0)
    target = modules[0] if modules else ""
    if target:
        last: int | None = None
        for index, slide in enumerate(plan[:closing_index]):
            if getattr(slide, "module_id", "") == target:
                last = index
        if last is not None:
            return last + 1
    return closing_index


def _brand_for_page(page: int) -> BrandSlide:
    return BrandSlide(page, PAGE_MODULES.get(page, ""), PAGE_LABELS.get(page, f"Page {page}"))


def _place_real_page(
    result_plan: list[PlannedSlide],
    page: int,
    modules: tuple[str, ...],
    sequence_set: set[str],
    protected_keys: set[str],
    used_pages: set[int],
    swapped_pages: list[int],
    ceiling: int,
) -> bool:
    """Insert or replace ``page`` into the plan. Returns False if nowhere to put it."""
    real = _brand_for_page(page)
    if len(result_plan) < ceiling:
        result_plan.insert(_insert_index_for_modules(result_plan, modules), real)
    else:
        index = weakest_body_index(result_plan, sequence_set, protected_keys)
        if index is None:
            return False
        result_plan[index] = real
    used_pages.add(page)
    protected_keys.add(real.slide_key)
    swapped_pages.append(page)
    return True


def _page_index(plan: Sequence[Any], page: int) -> int | None:
    for index, slide in enumerate(plan):
        if _is_brand(slide) and slide.page == page:
            return index
    return None


def _can_move_evidence(
    plan: Sequence[Any],
    page: int,
    sequence_set: set[str],
    protected_keys: set[str],
) -> bool:
    """Whether an in-plan evidence page may be relocated after a placeholder."""
    if page in (COVER_PAGE, CLOSING_PAGE):
        return False
    index = _page_index(plan, page)
    if index is None:
        return False
    slide = plan[index]
    if getattr(slide, "slide_key", "") in protected_keys:
        return False
    module_id = getattr(slide, "module_id", "") or ""
    if module_id and module_id in sequence_set:
        carriers = sum(
            1
            for item in plan
            if _is_brand(item) and getattr(item, "module_id", "") == module_id
        )
        if carriers <= 1:
            return False
    return True


def _resolve_evidence_page(
    candidate: GapCandidate,
    matched: AnswerMatch | None,
    used_pages: set[int],
    result_plan: Sequence[Any],
    sequence_set: set[str],
    protected_keys: set[str],
) -> int | None:
    """Evidence page to try placing after a placeholder, or ``None``."""
    if candidate.kind == "module":
        evidence = _keyword_evidence_page(candidate, used_pages)
    elif matched is not None:
        evidence = matched.evidence_page
    else:
        evidence = _keyword_evidence_page(candidate, used_pages)
    if evidence is None:
        return None
    if evidence in used_pages:
        if not _can_move_evidence(result_plan, evidence, sequence_set, protected_keys):
            return None
    return evidence


# ---------------------------------------------------------------------------
# Weakest body page (the one a gap fill is allowed to replace)
# ---------------------------------------------------------------------------


def _module_page_depth(module_id: str, page: int | None) -> int:
    pages = [entry_page for entry_page, _ in MODULE_PAGES.get(module_id, ())]
    if page in pages:
        return pages.index(page)
    return len(pages)


def weakest_body_index(
    plan: Sequence[Any],
    sequence_set: set[str],
    protected_keys: set[str],
) -> int | None:
    """Index of the weakest replaceable body page, or ``None`` if there is none.

    Never the cover (first) or closing (last), never a generated placeholder or
    an already-placed gap fill, and never the *sole* carrier of a recipe module
    (removing it would just open a new gap). Among the rest, an off-recipe
    top-up page is weakest; then the most redundant recipe module's deepest,
    latest page.
    """
    count = len(plan)
    if count <= 2:
        return None
    module_counts: dict[str, int] = {}
    for index in range(1, count - 1):
        slide = plan[index]
        if _is_brand(slide):
            module_counts[slide.module_id] = module_counts.get(slide.module_id, 0) + 1

    best_index: int | None = None
    best_key: tuple[int, int, int, int] | None = None
    for index in range(1, count - 1):
        slide = plan[index]
        if not _is_brand(slide):
            continue
        if getattr(slide, "slide_key", "") in protected_keys:
            continue
        if slide.page in (COVER_PAGE, CLOSING_PAGE):
            continue
        module_id = getattr(slide, "module_id", "")
        in_recipe = bool(module_id) and module_id in sequence_set
        if in_recipe and module_counts.get(module_id, 0) <= 1:
            continue  # sole carrier of a recipe module — keep it
        tier = 1 if in_recipe else 0  # off-recipe top-up is weakest
        key = (
            tier,
            -module_counts.get(module_id, 0),
            -_module_page_depth(module_id, slide.page),
            -index,
        )
        if best_key is None or key < best_key:
            best_key = key
            best_index = index
    return best_index


# ---------------------------------------------------------------------------
# Ranking + template selection (Haiku, with a deterministic fallback)
# ---------------------------------------------------------------------------

RankFn = Callable[[dict[str, Any]], dict[str, Any]]

_RANK_SYSTEM = (
    "You rank real gaps in a pitch deck and pick a slide template for each. "
    "You do NOT invent gaps. Choose only from the supplied templates. "
    "Pick the template whose purpose matches the claim. Never pick a numbers "
    "template for a claim without figures. "
    "Keep at most `budget` gaps, most important first. "
    'Return strict JSON: {"slides":[{"key":"...","template_id":"..."}]}.'
)


def _template_tone(template_id: str) -> str:
    return "dark" if template_id.endswith("-dark") else "light"


def _template_fits(candidate: GapCandidate, template_id: str) -> bool:
    """Whether the candidate supplies the content shape a template requires."""
    spec = template_spec(template_id)
    if spec.needs_numbers:
        return candidate.kind == "fact" or bool(_NUMERAL_RE.search(candidate.claim))
    return True


def _same_tone_divider(template_id: str) -> str:
    return f"{SECTION_DIVIDER_TEMPLATE_ID}-{_template_tone(template_id)}"


def _parse_ranked(
    payload: Any,
    surviving: list[GapCandidate],
    budget: int,
    allowed: tuple[str, ...],
) -> list[tuple[GapCandidate, str]] | None:
    if not isinstance(payload, dict):
        return None
    rows = payload.get("slides") or payload.get("gaps") or []
    if not isinstance(rows, list):
        return None
    by_key = {candidate.key: candidate for candidate in surviving}
    chosen: list[tuple[GapCandidate, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "")
        candidate = by_key.get(key)
        if candidate is None or key in seen:
            continue
        template_id = str(row.get("template_id") or "")
        if template_id not in allowed:
            template_id = DEFAULT_TEMPLATE_ID
        if not _template_fits(candidate, template_id):
            template_id = _same_tone_divider(template_id)
        chosen.append((candidate, template_id))
        seen.add(key)
        if len(chosen) >= budget:
            break
    return chosen or None


def rank_and_template(
    surviving: list[GapCandidate],
    budget: int,
    rank_fn: RankFn | None,
    *,
    allow_photo_templates: bool = True,
) -> list[tuple[GapCandidate, str]]:
    """Rank surviving gaps and pick a template; deterministic when the model fails.

    The deterministic order is the detection priority and the default template;
    the model may only reorder/trim within the same set and swap in a supported
    template. Photo-required templates are offered only when the recipe has an
    approved picture to draw on. Any model error or malformed response falls
    back cleanly.
    """
    allowed = allowed_template_ids(allow_photo_templates)
    default = [(candidate, DEFAULT_TEMPLATE_ID) for candidate in surviving[:budget]]
    if not surviving or budget <= 0 or rank_fn is None:
        return default
    try:
        payload = rank_fn(
            {
                "budget": budget,
                "templates": [
                    {
                        "id": template_id,
                        "purpose": template_spec(template_id).purpose,
                    }
                    for template_id in allowed
                ],
                "gaps": [
                    {
                        "key": candidate.key,
                        "kind": candidate.kind,
                        "title": candidate.title,
                        "claim": candidate.claim,
                        "modules": list(candidate.modules),
                    }
                    for candidate in surviving
                ],
            }
        )
    except Exception:  # noqa: BLE001 - any model failure -> deterministic fallback
        return default
    parsed = _parse_ranked(payload, surviving, budget, allowed)
    return parsed if parsed is not None else default


def _default_model_rank(payload: dict[str, Any]) -> dict[str, Any]:
    from backend.pipeline.llm import chat_json

    return chat_json(
        [
            {"role": "system", "content": _RANK_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        timeout=45.0,
        model=settings.openrouter_slide_model,
        reasoning=False,
        max_tokens=600,
    )


# ---------------------------------------------------------------------------
# Placeholder assembly
# ---------------------------------------------------------------------------


def _make_placeholder(
    candidate: GapCandidate,
    template_id: str,
    existing_keys: set[str],
) -> GeneratedSlidePlaceholder:
    base = f"{GENERATED_SOURCE}:{_slug(candidate.key)}"
    key = base
    suffix = 2
    while key in existing_keys:
        key = f"{base}-{suffix}"
        suffix += 1
    existing_keys.add(key)
    module_id = candidate.modules[0] if candidate.modules else ""
    return GeneratedSlidePlaceholder(
        slide_key=key,
        gap_kind=candidate.kind,
        claim=candidate.claim,
        title=candidate.title or module_id or "Generated slide",
        module_id=module_id,
        template_id=template_id,
        tone=_template_tone(template_id),
        summary=candidate.claim,
        recipe_modules=candidate.modules,
        source_fact_ids=candidate.source_fact_ids,
        source_asset_ids=candidate.source_asset_ids,
        source_objection_ids=candidate.source_objection_ids,
        keywords=tuple(sorted(candidate.keywords)),
    )


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------


def build_gap_plan(
    plan: Sequence[PlannedSlide],
    sequence: Sequence[str],
    duration: str,
    *,
    context_note: str = "",
    objections: Sequence[Any] | None = None,
    facts: Sequence[Any] | None = None,
    modules: Sequence[Any] | None = None,
    rank_fn: RankFn | None = None,
    match_fn: MatchFn | None = None,
    allow_photo_templates: bool = True,
    ceiling: int | None = None,
) -> GapPlanResult:
    """Augment ``plan`` with brand-page placements and generated placeholders.

    Pure and database-free so it can be unit-tested directly. When there is no
    true gap — or at T0 — the original plan is returned unchanged.
    ``allow_photo_templates`` gates the photo-required templates: when the
    recipe has no approved recommended picture it is ``False`` and the planner
    only plants photo-free placeholders. ``match_fn`` defaults to ``None`` so
    pure tests stay offline (keyword answer/evidence fallback). ``ceiling``
    defaults to :func:`slide_ceiling_for` for ``duration``.
    """
    original = list(plan)
    budget = generated_slide_budget(duration)
    if budget <= 0:
        return GapPlanResult(plan=original)

    deck_ceiling = slide_ceiling_for(duration) if ceiling is None else max(3, ceiling)
    sequence_order = list(dict.fromkeys(sequence))
    sequence_set = set(sequence_order)
    module_info = {
        getattr(module, "id", None): module
        for module in (modules or [])
        if getattr(module, "id", None)
    }

    candidates = detect_gaps(
        original,
        sequence_order,
        context_note=context_note or "",
        objections=objections or [],
        facts=facts or [],
        module_info=module_info,
    )
    if not candidates:
        return GapPlanResult(plan=original)

    result_plan: list[PlannedSlide] = list(original)
    used_pages = {slide.page for slide in result_plan if _is_brand(slide)}
    protected_keys: set[str] = set()
    swapped_pages: list[int] = []
    surviving: list[GapCandidate] = []
    evidence_by_key: dict[str, int | None] = {}

    # Module gaps: RULE ZERO with insert-under-ceiling / replace-at-ceiling.
    module_candidates = [candidate for candidate in candidates if candidate.kind == "module"]
    other_candidates = [candidate for candidate in candidates if candidate.kind != "module"]

    for candidate in module_candidates:
        page = rule_zero_page(candidate, used_pages)
        if page is None:
            surviving.append(candidate)
            continue
        if not _place_real_page(
            result_plan,
            page,
            candidate.modules,
            sequence_set,
            protected_keys,
            used_pages,
            swapped_pages,
            deck_ceiling,
        ):
            surviving.append(candidate)

    # Non-module gaps: one meaning-based match call, then place or survive.
    matches = match_answer_pages(other_candidates, used_pages, match_fn)
    for candidate in other_candidates:
        matched = matches.get(candidate.key, AnswerMatch(None, None))
        answer = matched.answer_page
        if answer is not None and answer in used_pages:
            # Already covered by a page in the deck — drop silently.
            continue
        if answer is not None and answer not in used_pages:
            if _place_real_page(
                result_plan,
                answer,
                candidate.modules,
                sequence_set,
                protected_keys,
                used_pages,
                swapped_pages,
                deck_ceiling,
            ):
                continue
            # Nowhere to put the real page — fall through to generation.
        surviving.append(candidate)
        evidence_by_key[candidate.key] = matched.evidence_page

    # Generation for the gaps the brand deck cannot cover, capped by budget.
    existing_keys = {
        getattr(slide, "slide_key", "") for slide in result_plan if getattr(slide, "slide_key", "")
    }
    generated: list[GeneratedSlidePlaceholder] = []
    for candidate, template_id in rank_and_template(
        surviving, budget, rank_fn, allow_photo_templates=allow_photo_templates
    ):
        matched = AnswerMatch(None, evidence_by_key.get(candidate.key))
        evidence = _resolve_evidence_page(
            candidate,
            matched if candidate.kind != "module" else None,
            used_pages,
            result_plan,
            sequence_set,
            protected_keys,
        )
        evidence_unused = evidence is not None and evidence not in used_pages
        evidence_in_plan = evidence is not None and evidence in used_pages

        if len(result_plan) < deck_ceiling:
            insert_at = _insert_index_for_modules(result_plan, candidate.modules)
            moved: BrandSlide | None = None
            if evidence_in_plan and evidence is not None:
                old_idx = _page_index(result_plan, evidence)
                if old_idx is None:
                    evidence = None
                    evidence_in_plan = False
                else:
                    moved = result_plan.pop(old_idx)  # type: ignore[assignment]
                    if old_idx < insert_at:
                        insert_at -= 1

            placeholder = _make_placeholder(candidate, template_id, existing_keys)
            result_plan.insert(insert_at, placeholder)

            placed_evidence: int | None = None
            if evidence_unused and evidence is not None:
                if len(result_plan) < deck_ceiling:
                    evidence_slide = _brand_for_page(evidence)
                    result_plan.insert(insert_at + 1, evidence_slide)
                    used_pages.add(evidence)
                    protected_keys.add(evidence_slide.slide_key)
                    placed_evidence = evidence
                # else: placeholder fitted but unused evidence would exceed — skip
            elif moved is not None and evidence is not None:
                result_plan.insert(insert_at + 1, moved)
                protected_keys.add(getattr(moved, "slide_key", ""))
                placed_evidence = evidence

            if placed_evidence is not None:
                placeholder = replace(placeholder, evidence_page=placed_evidence)
                result_plan[insert_at] = placeholder
            protected_keys.add(placeholder.slide_key)
            generated.append(placeholder)
            continue

        # At the ceiling: replace the weakest body page; skip unused evidence.
        index = weakest_body_index(result_plan, sequence_set, protected_keys)
        if index is None:
            break
        displaced = result_plan[index]
        placeholder = replace(
            _make_placeholder(candidate, template_id, existing_keys),
            replaced_page=getattr(displaced, "page", None),
            replaced_module_id=getattr(displaced, "module_id", "") or "",
            replaced_label=getattr(displaced, "title", "")
            or getattr(displaced, "label", "")
            or "",
        )
        # If evidence is already in the plan and movable, place it after the
        # replacement slot (a MOVE does not change length). Unused evidence is
        # skipped at the ceiling.
        placed_evidence = None
        if evidence_in_plan and evidence is not None and _can_move_evidence(
            result_plan, evidence, sequence_set, protected_keys
        ):
            old_idx = _page_index(result_plan, evidence)
            if old_idx is not None and old_idx != index:
                moved = result_plan.pop(old_idx)
                if old_idx < index:
                    index -= 1
                result_plan[index] = placeholder
                result_plan.insert(index + 1, moved)
                protected_keys.add(getattr(moved, "slide_key", ""))
                placed_evidence = evidence
                placeholder = replace(placeholder, evidence_page=placed_evidence)
                result_plan[index] = placeholder
            else:
                result_plan[index] = placeholder
        else:
            result_plan[index] = placeholder
        protected_keys.add(placeholder.slide_key)
        generated.append(placeholder)

    if not generated and not swapped_pages:
        return GapPlanResult(plan=original)
    return GapPlanResult(plan=result_plan, generated=generated, swapped_pages=swapped_pages)


# ---------------------------------------------------------------------------
# Database wrapper (used by the runner)
# ---------------------------------------------------------------------------

_MISSING = object()


def _safe_all(rows: Any) -> list[Any]:
    return rows if isinstance(rows, list) else []


def _ranked_objections(db: Any, intent: str, audience_cluster: str, recipe_ref: str) -> list[Any]:
    from backend.models import Objection, Recipe
    from backend.qa_extraction import rank_objections_for_pitch

    try:
        approved = _safe_all(db.query(Objection).filter(Objection.status == "approved").all())
    except Exception:  # noqa: BLE001
        return []
    if not approved:
        return []
    audience_label = ""
    if recipe_ref:
        try:
            recipe = db.query(Recipe).filter(Recipe.ref == recipe_ref).first()
        except Exception:  # noqa: BLE001
            recipe = None
        label = getattr(recipe, "audience_label", "") if recipe is not None else ""
        if isinstance(label, str):
            audience_label = label
    return rank_objections_for_pitch(
        approved,
        audience_cluster=audience_cluster or "",
        audience_label=audience_label,
        intent=intent or "",
        limit=6,
    )


def plan_with_generated_slides(
    db: Any,
    plan: Sequence[PlannedSlide],
    resolved: Any,
    *,
    duration: str,
    context_note: str = "",
    intent: str = "",
    audience_cluster: str = "",
    recipe_ref: str = "",
    modules: Sequence[Any] | None = None,
    rank_fn: Any = _MISSING,
    match_fn: Any = _MISSING,
) -> list[PlannedSlide]:
    """Runner entry point: detect gaps and return the (possibly) augmented plan.

    Slots between :func:`~backend.pipeline.brand_deck.plan_pages` and
    :func:`~backend.pipeline.script_flow.load_script_topics`. Returns the plan
    unchanged whenever there is no true gap or the generated budget is zero.
    """
    sequence = list(getattr(resolved, "module_sequence", []) or [])
    if generated_slide_budget(duration) <= 0:
        return list(plan)

    from backend.models import LockedFact, Module

    facts = _safe_all(db.query(LockedFact).all())
    objections = _ranked_objections(db, intent, audience_cluster, recipe_ref)
    if modules is None:
        try:
            modules = _safe_all(db.query(Module).filter(Module.id.in_(sequence)).all())
        except Exception:  # noqa: BLE001
            modules = []

    resolved_rank_fn = _default_model_rank if rank_fn is _MISSING else rank_fn
    resolved_match_fn = _default_model_match if match_fn is _MISSING else match_fn
    result = build_gap_plan(
        plan,
        sequence,
        duration,
        context_note=context_note,
        objections=objections,
        facts=facts,
        modules=modules,
        rank_fn=resolved_rank_fn,
        match_fn=resolved_match_fn,
        allow_photo_templates=_recipe_has_approved_photos(db, recipe_ref),
        ceiling=slide_ceiling_for(duration),
    )
    return result.plan


def _recipe_has_approved_photos(db: Any, recipe_ref: str) -> bool:
    """True when the recipe has at least one approved recommended picture.

    Gates the photo-required templates in the planner. A DB/model hiccup is
    treated as "no approved photo" so the planner degrades to photo-free
    templates rather than planting a slide that could only fall back.
    """
    if not (recipe_ref or "").strip():
        return False
    try:
        from backend.media_index import pick_recommended_media

        _videos, pictures = pick_recommended_media(db, recipe_ref)
        return bool(pictures)
    except Exception:  # noqa: BLE001 - never block planning on media lookup
        return False
