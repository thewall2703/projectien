"""Stage 4 of the Generated Slide Engine: fill, gate, render, cache and persist
the typed placeholders Stage 3 planted — or, if a slide cannot be produced on
brand, degrade cleanly to the real brand page it displaced.

Stage 3 (:mod:`backend.pipeline.gaps`) turns a true gap the brand deck cannot
answer into a :class:`~backend.pipeline.gaps.GeneratedSlidePlaceholder`: a typed
brief (claim, module, template, tone, the source fact/passage ids) that carries
no image yet. This stage realises each placeholder into either

* a concrete :class:`GeneratedSlideInstance` backed by a rendered, cached JPEG
  and a persisted :class:`~backend.models.GeneratedSlide` row, or
* the original :class:`~backend.pipeline.brand_deck.BrandSlide` it displaced,
  when the slide cannot be produced on brand.

The pipeline, in order, is:

1. **Shared claim cache.** A deterministic, content-based claim hash keys a
   cache shared across users. On a hit *with* valid stored pixels, the existing
   slide is reused — no model, no vision, no render.
2. **Slot fill (Haiku).** On a miss, ``settings.openrouter_slide_model`` writes
   typed slot copy for the fixed template. The model never designs; the layout,
   furniture and colours are the template's. Its output is post-processed for
   deck conventions (ampersands, no terminal full stop).
3. **Gates, cheapest first, strict pass/fail:**
   a. schema + the template's *exact* per-slot word/character budgets;
   b. number/factual provenance — every numeral must trace to a locked fact or
      report passage, or the slide is rejected;
   c. copy register — unmistakable officialese is caught here rather than
      paying for a render and a vision call to discover it;
   d. DOM overflow, via the real slide renderer;
   e. an Opus multimodal vision review against three actual brand pages.
4. **Persist.** On pass, a deterministic render hash content-addresses the JPEG
   in Spaces (Stage 2 helper) and a shared :class:`~backend.models.GeneratedSlide`
   row is upserted race-safely.

A rejection is only worth retrying when rewriting the slot copy could fix it.
The vision review therefore classifies each violation as ``copy`` or
``furniture``: copy failures retry (three attempts total), while a furniture
failure — wrong typography, a missing lockup, off-brand accent art — degrades
immediately, because the copy model is explicitly forbidden from designing and
so can never repair it. A template that fails that way is also skipped for the
rest of the run instead of being rediscovered on every later placeholder.
Degrading always restores the displaced brand page: never a shortened deck,
never a skipped page. Everything external (LLM, renderer, storage, brand-page
fetch) is injectable so the whole flow is unit-testable without a browser or
network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy.exc import IntegrityError

from backend.config import settings
from backend.generated_slides import (
    compute_claim_hash,
    compute_render_hash,
    generated_slide_key,
    save_generated_slide_attempt_image,
    save_generated_slide_image,
)
from backend.models import GeneratedSlide, GeneratedSlideAttempt
from backend.pipeline.claims import SLIDE_CLAIMS_RULES_PROMPT, claim_violations, pair_median_facts
from backend.pipeline.brand_deck import (
    COVER_PAGE,
    MODULE_PAGES,
    PAGE_LABELS,
    BrandSlide,
    page_image,
)
from backend.pipeline.deck import GENERATED_SOURCE, PlannedSlide
from backend.pipeline.gaps import GeneratedSlidePlaceholder
from backend.pipeline.slide_render import (
    FONT_BUNDLE_VERSION,
    BrowserUnavailable,
    SlideOverflowError,
    SlideRenderError,
    get_renderer,
)
from backend.pipeline.slide_templates import TemplateSpec, template_spec
from backend.storage import file_exists, read_file


@dataclass(frozen=True)
class ApprovedPhoto:
    """One approved photo available to fill a template's photo slot.

    Sourced only from the recipe's approved *recommended pictures* (never an
    arbitrary URL): ``asset_id`` and ``key`` identify the stored image and feed
    the render hash, and ``data`` is the embedded (downscaled) JPEG bytes.
    """

    asset_id: int
    key: str
    data: bytes


FillFn = Callable[[dict[str, Any]], Any]
VisionFn = Callable[[bytes, list[bytes], dict[str, Any]], Any]
BrandPageFn = Callable[[int], bytes]
# Given a placeholder + its resolved spec, return the approved photos (in
# preference order) the template may use. Empty when the recipe has none.
PhotoSourceFn = Callable[[GeneratedSlidePlaceholder, TemplateSpec], Sequence[ApprovedPhoto]]

_MISSING = object()

# One placeholder gets at most this many fill attempts before it degrades to the
# brand page it displaced. Furniture failures exit immediately because copy
# cannot repair them.
DEFAULT_MAX_ATTEMPTS = 3

# Reference brand pages a vision review holds a render up against.
_VISION_REFERENCE_COUNT = 3
_GUIDANCE_NOTE_LIMIT = 5
_GUIDANCE_CHAR_LIMIT = 1500
_GUIDANCE_NOTE_CHAR_LIMIT = 500


# ---------------------------------------------------------------------------
# Concrete generated slide (satisfies PlannedSlide)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneratedSlideInstance:
    """A rendered, persisted generated slide placed in a deck.

    Satisfies :class:`~backend.pipeline.deck.PlannedSlide`, so the deck spec and
    the PPTX renderer treat it exactly like a brand page. ``image_url`` points at
    the authenticated generated-slide route (keyed by the stable ``slide_key``),
    and ``file_key`` lets :func:`~backend.pipeline.brand_deck.render_pptx` embed
    the stored JPEG straight from object storage.
    """

    slide_key: str
    template_id: str
    template_version: str
    tone: str
    title: str
    module_id: str
    file_key: str
    generated_slide_id: int = 0
    claim_hash: str = ""
    render_hash: str = ""

    @property
    def source(self) -> str:
        return GENERATED_SOURCE

    @property
    def page(self) -> int | None:
        return None

    @property
    def image_url(self) -> str:
        return f"/api/generated-slides/{self.slide_key}.jpg"


# ---------------------------------------------------------------------------
# Text helpers, deck conventions and number provenance
# ---------------------------------------------------------------------------

# Matches a numeral with optional thousands separators / decimals; range and
# percent glue (``-`` ``%`` ``₹``) sit outside the class, so "₹5-50 lakh" yields
# the two supportable tokens 5 and 50, and "27.78%" yields 27.78.
_NUMERAL_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_AND_RE = re.compile(r"\s+and\s+", re.IGNORECASE)
_STRONG_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_EMPHASIS_RE = re.compile(r"\*(.+?)\*", re.S)

# Conservative officialese that does not occur in the terse, direct brand
# exemplars. These are deliberately phrase-level patterns rather than broad
# vocabulary bans: ordinary words such as "approval" remain valid on their own.
_REGISTER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bas per\b", re.I), "'as per'"),
    (re.compile(r"\bpursuant to\b", re.I), "'pursuant to'"),
    (re.compile(r"\bin order to\b", re.I), "'in order to'"),
    (re.compile(r"\bwith respect to\b", re.I), "'with respect to'"),
    (re.compile(r"\bfollowing\b[^.!?]{0,80}\bapproval\b", re.I), "'following … approval'"),
    (re.compile(r"\bfor the purpose of\b", re.I), "'for the purpose of'"),
    (re.compile(r"\bin this regard\b", re.I), "'in this regard'"),
    (re.compile(r"\bit (?:is|should be) (?:important to )?noted that\b", re.I), "'it is/should be noted that'"),
    (re.compile(r"\b(?:is|are|was|were) hereby\b", re.I), "passive 'hereby' construction"),
)


def _numerals(text: str) -> set[str]:
    return {match.group(0).replace(",", "") for match in _NUMERAL_RE.finditer(text or "")}


def _plain(text: str) -> str:
    """Drop the ``**strong**``/``*emphasis*`` markup for display/labels."""
    text = _STRONG_RE.sub(lambda m: m.group(1), text or "")
    return _EMPHASIS_RE.sub(lambda m: m.group(1), text).strip()


def apply_deck_conventions(value: str) -> str:
    """Normalise one slot value to deck copy conventions.

    The deck writes an ampersand between two things and never ends a slide line
    with a full stop. Applied after the model and before every gate, so budgets
    and provenance see exactly the copy that will render.
    """
    text = (value or "").strip()
    if not text:
        return ""
    text = _AND_RE.sub(" & ", text)
    if text.endswith(".") and not text.endswith("..."):
        text = text[:-1].rstrip()
    return text


def allowed_numerals(
    source_facts: Sequence[Any],
    source_passages: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Every numeral the locked facts and report passages can support."""
    allowed: set[str] = set()
    for fact in source_facts:
        allowed |= _numerals(getattr(fact, "value", "") or "")
        allowed |= _numerals(getattr(fact, "fact", "") or "")
    for passage in source_passages:
        allowed |= _numerals(str(passage.get("text") or ""))
    return allowed


# ---------------------------------------------------------------------------
# Gates (pure)
# ---------------------------------------------------------------------------


def _list_items(values: Mapping[str, Any], name: str) -> list[str]:
    raw = values.get(name)
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw if str(item).strip()]


def gate_schema(spec: TemplateSpec, values: Mapping[str, Any]) -> list[str]:
    violations = [
        f"slot '{name}' is required and must not be empty"
        for name in spec.required_slots
        if not str(values.get(name) or "").strip()
    ]
    for name in spec.fillable_lists:
        try:
            required = spec.manifest.list_slot(name).required
        except KeyError:
            required = False
        if required and not _list_items(values, name):
            violations.append(f"list '{name}' is required and must have at least one item")
    return violations


def slot_budget_violations(spec: TemplateSpec, name: str, value: Any) -> list[str]:
    """Budget violations for one scalar slot (empty when it fits or is unbounded)."""
    budget = spec.budgets.get(name)
    if budget is None:
        return []
    value = str(value or "")
    if not value:
        # A required slot being empty is a schema violation, not a budget
        # one; an empty optional slot has nothing to bound.
        if budget.min_chars and name in spec.required_slots:
            return [f"slot '{name}' is empty, needs at least {budget.min_chars} characters"]
        return []
    plain = _plain(value)
    chars = len(plain)
    words = len(plain.split())
    violations: list[str] = []
    if chars > budget.max_chars:
        violations.append(
            f"slot '{name}' is {chars} characters, over the {budget.max_chars} limit"
        )
    if words > budget.max_words:
        violations.append(
            f"slot '{name}' is {words} words, over the {budget.max_words} limit"
        )
    if budget.min_chars and chars < budget.min_chars:
        violations.append(
            f"slot '{name}' is {chars} characters, under the {budget.min_chars} minimum"
        )
    if budget.min_words and words < budget.min_words:
        violations.append(
            f"slot '{name}' is {words} words, under the {budget.min_words} minimum"
        )
    return violations


def gate_budgets(spec: TemplateSpec, values: Mapping[str, Any]) -> list[str]:
    violations: list[str] = []
    for name in spec.fillable_slots:
        violations.extend(slot_budget_violations(spec, name, values.get(name)))
    for name in spec.fillable_lists:
        items = _list_items(values, name)
        budget = spec.list_budgets.get(name)
        if budget is None:
            continue
        if len(items) > budget.max_items:
            violations.append(
                f"list '{name}' has {len(items)} items, over the {budget.max_items} limit"
            )
        for index, item in enumerate(items):
            plain = _plain(item)
            if len(plain) > budget.max_chars:
                violations.append(
                    f"list '{name}' item {index + 1} is {len(plain)} characters, over the {budget.max_chars} limit"
                )
            if len(plain.split()) > budget.max_words:
                violations.append(
                    f"list '{name}' item {index + 1} is {len(plain.split())} words, over the {budget.max_words} limit"
                )
    return violations


def gate_number_provenance(
    spec: TemplateSpec,
    values: Mapping[str, Any],
    allowed: set[str],
) -> list[str]:
    violations: list[str] = []
    for name in spec.fillable_slots:
        for numeral in sorted(_numerals(str(values.get(name) or ""))):
            if numeral not in allowed:
                violations.append(
                    f"slot '{name}' uses the number '{numeral}', which is not supported by any "
                    "locked fact or report passage"
                )
    for name in spec.fillable_lists:
        for item in _list_items(values, name):
            for numeral in sorted(_numerals(item)):
                if numeral not in allowed:
                    violations.append(
                        f"list '{name}' uses the number '{numeral}', which is not supported by any "
                        "locked fact or report passage"
                    )
    return violations


def gate_claim_hygiene(spec: TemplateSpec, values: Mapping[str, Any]) -> list[str]:
    """Reject banned claim phrases, rounded aggregates, and unpaired averages per slide."""
    parts: list[str] = []
    for name in spec.fillable_slots:
        parts.append(str(values.get(name) or ""))
    for name in spec.fillable_lists:
        parts.extend(_list_items(values, name))
    return claim_violations("\n".join(parts), unit_label="This slide")


def gate_copy_register(spec: TemplateSpec, values: Mapping[str, Any]) -> list[str]:
    """Reject unmistakable officialese before render and vision.

    The real brand exemplars are fragments, direct questions, compact labels
    and active descriptions. Phrase-level bureaucratic scaffolding is therefore
    both off-register and wasted copy inside these tightly bounded slots.
    """
    violations: list[str] = []
    entries = [
        (f"slot '{name}'", str(values.get(name) or ""))
        for name in spec.fillable_slots
    ]
    entries.extend(
        (f"list '{name}' item {index + 1}", item)
        for name in spec.fillable_lists
        for index, item in enumerate(_list_items(values, name))
    )
    for location, text in entries:
        plain = _plain(text)
        for pattern, label in _REGISTER_PATTERNS:
            if pattern.search(plain):
                violations.append(
                    f"{location} uses bureaucratic phrasing {label}; rewrite as short, direct brand copy"
                )
    if spec.template_id.startswith("section-divider-"):
        title = str(values.get("title") or "")
        if not _EMPHASIS_RE.search(title):
            violations.append(
                "slot 'title' must wrap one meaningful 1-4 word phrase in "
                "*single asterisks* for the divider's italic-serif accent"
            )
    return violations


# ---------------------------------------------------------------------------
# Default model / vision hooks (network) — injectable for tests
# ---------------------------------------------------------------------------

_FILL_SYSTEM = (
    "You write the on-slide copy for ONE pitch-deck slide from a fixed, "
    "pre-designed template. You never design: the layout, furniture, colours, "
    "type and any photos are already set. Fill each provided text slot (and any "
    "list_slots, as a JSON array of short strings) so the slide expresses the "
    "given claim in the brand's voice. Match the register and shape of the "
    "style_exemplars, never their subject. Use ONLY numbers that appear "
    "verbatim in locked_facts or report_passages; never invent a number. "
    "Treat approved_review_guidance as binding lessons from human reviewers "
    "for this template; apply the lessons without copying unrelated subject matter. "
    "Write short, direct, confident fragments or sentences like the exemplars. "
    "Avoid officialese, passive-bureaucratic scaffolding and hedging filler: "
    "never use 'as per', 'pursuant to', 'in order to', 'with respect to', "
    "'following ... approval', 'for the purpose of', 'in this regard', "
    "'it is/should be noted that', or passive 'hereby' constructions. "
    "Follow the conventions: write '&' not 'and', and no trailing full stop. "
    "You may wrap a short italic-serif accent in *single asterisks* and bold a "
    "subject in **double asterisks**, sparingly, like the exemplars. Respect "
    "every slot's min/max words and chars and each list's max_items. "
    "On-slide copy stays spec-sheet: short declarative fragments, exact numbers — "
    "no slang, no personality asides. "
    + SLIDE_CLAIMS_RULES_PROMPT
    + 'Return strict JSON mapping each slot/list name to its value, e.g. {"title":"...",'
    '"items":["...","..."]}. Do not add keys and do not fill photo content.'
)


def _default_fill_fn(payload: dict[str, Any]) -> dict[str, Any]:
    from backend.pipeline.llm import chat_json

    return chat_json(
        [
            {"role": "system", "content": _FILL_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        timeout=45.0,
        model=settings.openrouter_slide_model,
        reasoning=False,
        max_tokens=500,
    )


_VISION_PROMPT = (
    "Image 1 is a NEWLY GENERATED pitch-deck slide. The remaining images are "
    "real pages from the same brand's deck. Judge only image 1. It passes when "
    "it is visually on brand and consistent with the brand pages — the same "
    "typographic scale and restraint, layout discipline, colour and furniture — "
    "and its copy reads like real brand copy of this kind (a short, pointed "
    "line, a clean stat, a tight caption or a bounded list — not a paragraph). "
    "It fails if it looks generated, cramped, off-brand, mis-scaled, a photo is "
    "distorted or mis-cropped, or the copy is awkward or off-register. Classify "
    "every violation as 'copy' when rewriting only the supplied slot text could "
    "fix it (wording, tone, register, length or cramped copy), or 'furniture' "
    "when it requires changing the fixed template (typography, type scale/weight, "
    "logo or lockup, ribbon/accent art, palette, layout geometry or other brand "
    "furniture). Return STRICT JSON only: "
    '{"passed": true|false, "violations": [{"category":"copy|furniture","detail":"..."}]}. '
    "Context: "
)


def _normalise_vision_violations(raw: Any) -> list[dict[str, str]]:
    """Accept the typed contract and legacy string violations defensively."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    violations: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, Mapping):
            category = str(item.get("category") or "copy").strip().lower()
            if category not in {"copy", "furniture"}:
                category = "copy"
            detail = str(item.get("detail") or item.get("message") or "").strip()
        else:
            category = "copy"
            detail = str(item).strip()
        if detail:
            violations.append({"category": category, "detail": detail})
    return violations


def _default_vision_fn(
    jpeg: bytes,
    brand_pages: list[bytes],
    context: dict[str, Any],
) -> dict[str, Any]:
    from backend.pipeline.llm import _extract_json, chat_text_multimodal

    text = chat_text_multimodal(
        _VISION_PROMPT + json.dumps(context, ensure_ascii=False),
        [jpeg, *brand_pages],
        timeout=90.0,
    )
    data = _extract_json(text)
    return {
        "passed": bool(data.get("passed")),
        "violations": _normalise_vision_violations(data.get("violations")),
    }


# ---------------------------------------------------------------------------
# Photo sourcing (approved recommended pictures only) — injectable for tests
# ---------------------------------------------------------------------------


def _no_photos(placeholder: GeneratedSlidePlaceholder, spec: TemplateSpec) -> list[ApprovedPhoto]:
    return []


def make_recommended_photo_fn(
    db: Any,
    recipe_ref: str,
    *,
    temperature: str = "",
    duration: str = "",
) -> PhotoSourceFn:
    """A :data:`PhotoSourceFn` backed by the recipe's approved recommended pictures.

    Photos come *only* from :func:`~backend.media_index.pick_recommended_media`
    (the human-reviewed, non-rejected recommendations for this recipe) — never
    an arbitrary URL — and are embedded from stored bytes, so a render never
    depends on a browser network fetch. Returns at most as many photos as the
    template has slots, in recommendation order.
    """

    def photo_fn(
        placeholder: GeneratedSlidePlaceholder, spec: TemplateSpec
    ) -> list[ApprovedPhoto]:
        if not spec.photo_slots or not (recipe_ref or "").strip():
            return []
        try:
            from backend.media_index import _downscale_jpeg, pick_recommended_media
            from backend.models import Asset
            from backend.thumbnails import thumbnail_key

            _videos, pictures = pick_recommended_media(
                db, recipe_ref, temperature or "", duration or ""
            )
        except Exception:  # noqa: BLE001 - a media lookup failure means "no photo"
            return []
        photos: list[ApprovedPhoto] = []
        seen: set[str] = set()
        for picture in pictures:
            child = db.get(Asset, picture.asset_id)
            if child is None:
                continue
            key = child.file_key or thumbnail_key(child)
            if not key or key in seen:
                continue
            try:
                data = _downscale_jpeg(read_file(key))
            except Exception:  # noqa: BLE001 - skip an unreadable image
                continue
            photos.append(ApprovedPhoto(asset_id=int(picture.asset_id), key=key, data=data))
            seen.add(key)
            if len(photos) >= len(spec.photo_slots):
                break
        return photos

    return photo_fn


def make_asset_photo_fn(db: Any, asset_ids: Sequence[int]) -> PhotoSourceFn:
    """A :data:`PhotoSourceFn` backed by specific approved image asset ids.

    Used when re-rendering an already-generated slide (an admin copy edit): the
    photo is re-embedded from the same stored image assets the slide was first
    built from, in id order, so a text tweak keeps the exact same photo. Only
    image assets are used; non-image ids (e.g. report passages) are ignored.
    """

    def photo_fn(
        placeholder: GeneratedSlidePlaceholder | None, spec: TemplateSpec
    ) -> list[ApprovedPhoto]:
        if not spec.photo_slots or not asset_ids:
            return []
        from backend.media_index import _downscale_jpeg
        from backend.models import Asset
        from backend.thumbnails import thumbnail_key

        photos: list[ApprovedPhoto] = []
        seen: set[str] = set()
        for raw_id in asset_ids:
            try:
                asset = db.get(Asset, int(raw_id))
            except Exception:  # noqa: BLE001
                asset = None
            if asset is None:
                continue
            is_image = (getattr(asset, "type", "") or "").lower() in {
                "picture",
                "image",
                "photo",
            } or (getattr(asset, "content_type", "") or "").startswith("image/")
            if not is_image:
                continue
            key = asset.file_key or thumbnail_key(asset)
            if not key or key in seen:
                continue
            try:
                data = _downscale_jpeg(read_file(key))
            except Exception:  # noqa: BLE001 - skip an unreadable image
                continue
            photos.append(ApprovedPhoto(asset_id=int(raw_id), key=key, data=data))
            seen.add(key)
            if len(photos) >= len(spec.photo_slots):
                break
        return photos

    return photo_fn


def _prepare_photos(
    spec: TemplateSpec,
    placeholder: GeneratedSlidePlaceholder | None,
    photo_fn: PhotoSourceFn,
) -> tuple[dict[str, bytes], list[dict[str, Any]], list[int]]:
    """Assign approved photos to the template's photo slots.

    Returns ``(photos_by_slot, hash_descriptors, asset_ids)``. The descriptors
    carry the asset id/key and the slot's deterministic crop metadata, so the
    render hash changes when the chosen asset or its crop changes. Optional
    slots with no available photo are simply left unfilled.
    """
    if not spec.photo_slots:
        return {}, [], []
    approved = list(photo_fn(placeholder, spec) or [])
    photos_by_slot: dict[str, bytes] = {}
    descriptors: list[dict[str, Any]] = []
    asset_ids: list[int] = []
    for slot_name, photo in zip(spec.photo_slots, approved):
        if not photo.data:
            continue
        photo_slot = spec.manifest.photo_slot(slot_name)
        photos_by_slot[slot_name] = photo.data
        descriptors.append(
            {
                "slot": slot_name,
                "asset_id": int(photo.asset_id),
                "key": photo.key,
                "crop": photo_slot.crop_metadata(),
            }
        )
        asset_ids.append(int(photo.asset_id))
    return photos_by_slot, descriptors, asset_ids


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec_or_none(template_id: str) -> TemplateSpec | None:
    try:
        return template_spec(template_id)
    except KeyError:
        return None


def _load_facts(db: Any) -> list[Any]:
    from backend.models import LockedFact

    try:
        rows = db.query(LockedFact).all()
    except Exception:  # noqa: BLE001 - a DB hiccup must not crash generation
        return []
    return rows if isinstance(rows, list) else list(rows or [])


def _placeholder_modules(placeholder: GeneratedSlidePlaceholder) -> set[str]:
    modules = {mid for mid in placeholder.recipe_modules if mid}
    if placeholder.module_id:
        modules.add(placeholder.module_id)
    return modules


def relevant_passages(
    passages: Sequence[Mapping[str, Any]],
    modules: set[str],
) -> list[Mapping[str, Any]]:
    """Report passages that bear on this slide's module(s).

    A passage overlaps when its ``module_ids`` intersect the slide's modules;
    untagged passages are kept because they carry no counter-signal. With no
    module context every passage is in scope.
    """
    if not modules:
        return list(passages)
    kept: list[Mapping[str, Any]] = []
    for passage in passages:
        ids = {part.strip() for part in str(passage.get("module_ids") or "").split(",") if part.strip()}
        if not ids or ids & modules:
            kept.append(passage)
    return kept


def _reference_pages(
    spec: TemplateSpec,
    placeholder: GeneratedSlidePlaceholder,
    brand_page_fn: BrandPageFn,
) -> list[bytes]:
    pages: list[int] = list(spec.reference_pages)
    for module_id in (*placeholder.recipe_modules, placeholder.module_id):
        options = MODULE_PAGES.get(module_id or "")
        if options:
            candidate = options[0][0]
            if candidate not in pages:
                pages.append(candidate)
                break
    if COVER_PAGE not in pages:
        pages.append(COVER_PAGE)
    pages = pages[:_VISION_REFERENCE_COUNT]

    images: list[bytes] = []
    for page in pages:
        try:
            images.append(brand_page_fn(page))
        except Exception:  # noqa: BLE001 - a missing reference must not block the gate
            continue
    return images


def _fill_payload(
    spec: TemplateSpec,
    placeholder: GeneratedSlidePlaceholder,
    script_text: str,
    source_facts: Sequence[Any],
    source_passages: Sequence[Mapping[str, Any]],
    corrections: Sequence[str],
    review_guidance: Sequence[str] = (),
    previous_values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "template_id": spec.template_id,
        "tone": spec.tone,
        "claim": placeholder.claim,
        "title_hint": placeholder.title,
        "subtitle_hint": placeholder.subtitle,
        "approved_script_excerpt": script_text.strip(),
        "slots": [
            {
                "name": name,
                "required": name in spec.required_slots,
                "max_words": spec.budgets[name].max_words if name in spec.budgets else None,
                "max_chars": spec.budgets[name].max_chars if name in spec.budgets else None,
                "min_words": spec.budgets[name].min_words if name in spec.budgets else 0,
                "min_chars": spec.budgets[name].min_chars if name in spec.budgets else 0,
            }
            for name in spec.fillable_slots
        ],
        "list_slots": [
            {
                "name": name,
                "max_items": spec.list_budgets[name].max_items if name in spec.list_budgets else None,
                "max_words_per_item": spec.list_budgets[name].max_words if name in spec.list_budgets else None,
                "max_chars_per_item": spec.list_budgets[name].max_chars if name in spec.list_budgets else None,
                "note": "A JSON array of short strings; bold the subject with **double asterisks**.",
            }
            for name in spec.fillable_lists
        ],
        "style_exemplars": list(spec.exemplars),
        "locked_facts": [
            {
                "fact": getattr(fact, "fact", "") or "",
                "value": getattr(fact, "value", "") or "",
            }
            for fact in source_facts
        ],
        "report_passages": [str(passage.get("text") or "")[:400] for passage in source_passages],
        "conventions": [
            "Infer the slide's subject and wording only from approved_script_excerpt.",
            "Use style_exemplars only for tone, length and structure; never copy their subject matter.",
            "Write '&' not 'and'.",
            "No trailing full stop.",
            "Use short, direct, confident brand language; prefer fragments, compact labels and active descriptions.",
            "Avoid officialese, passive-bureaucratic scaffolding and hedging filler.",
            "Never use 'as per', 'pursuant to', 'in order to', 'with respect to', 'following ... approval', 'for the purpose of', 'in this regard', 'it is/should be noted that', or passive 'hereby' constructions.",
            "Only use numbers that appear in locked_facts or report_passages.",
            "If you state an average CTC/pay figure, put the median on the same slide.",
            "Never round figures up with '+' / 'plus'; never use banned claim adjectives.",
            "Do not design; only write the slot copy.",
            "max_chars counts visible characters (spaces included, emphasis asterisks excluded); aim a few characters under every limit.",
            "When previous_values is present, fix only what corrections names and keep every other slot exactly as it was.",
            *(
                [
                    "The section-divider title MUST wrap one meaningful 1-4 word phrase in *single asterisks*; the contrast between bold sans and italic serif is required brand furniture."
                ]
                if spec.template_id.startswith("section-divider-")
                else []
            ),
        ],
        "previous_values": dict(previous_values or {}),
        "corrections": list(corrections),
        "approved_review_guidance": list(review_guidance),
    }


def _repair_budgets(
    spec: TemplateSpec,
    values: Mapping[str, Any],
    previous_values: Mapping[str, Any],
    *,
    final_attempt: bool,
) -> dict[str, Any]:
    """Stop a retry from trading one over-budget slot for another.

    A slot that now overflows falls back to its previous value when that one
    fitted. On the final attempt a droppable supporting line that still
    overflows is blanked, so a long footnote cannot sink an otherwise good slide.
    """
    repaired = dict(values)
    for name in spec.fillable_slots:
        if not slot_budget_violations(spec, name, repaired.get(name)):
            continue
        prior = previous_values.get(name)
        if prior and not slot_budget_violations(spec, name, prior):
            repaired[name] = prior
            continue
        budget = spec.budgets.get(name)
        if final_attempt and budget is not None and budget.droppable and name not in spec.required_slots:
            repaired[name] = ""
    if spec.template_id.startswith("section-divider-"):
        title = _plain(str(repaired.get("title") or ""))
        # The source deck uses either a two-line question (p19) or a short
        # title plus subtitle (p52). A subtitle beneath a question/long title
        # collides with the second headline line, so select one composition.
        if "?" in title or len(title) > 32:
            repaired["subtitle"] = ""
    return repaired


def _resolve_values(spec: TemplateSpec, raw: Any) -> dict[str, Any]:
    """Restrict the model output to the fillable slots/lists and apply conventions."""
    payload = raw if isinstance(raw, Mapping) else {}
    values: dict[str, Any] = {}
    for name in spec.fillable_slots:
        candidate = payload.get(name)
        values[name] = apply_deck_conventions("" if candidate is None else str(candidate))
    for name in spec.fillable_lists:
        candidate = payload.get(name)
        budget = spec.list_budgets.get(name)
        max_items = budget.max_items if budget else None
        items: list[str] = []
        if isinstance(candidate, (list, tuple)):
            for entry in candidate:
                cleaned = apply_deck_conventions(str(entry))
                if cleaned:
                    items.append(cleaned)
        elif isinstance(candidate, str) and candidate.strip():
            cleaned = apply_deck_conventions(candidate)
            if cleaned:
                items.append(cleaned)
        values[name] = items[:max_items] if max_items is not None else items
    return values


def _run_vision(
    vision_fn: VisionFn,
    jpeg: bytes,
    brand_pages: list[bytes],
    spec: TemplateSpec,
    placeholder: GeneratedSlidePlaceholder,
) -> dict[str, Any]:
    context = {
        "template_id": spec.template_id,
        "tone": spec.tone,
        "claim": placeholder.claim,
        "title_hint": placeholder.title,
    }
    try:
        review = vision_fn(jpeg, brand_pages, context)
    except Exception as exc:  # noqa: BLE001 - a failed review is a failed gate, retryable
        return {
            "passed": False,
            "violations": [{"category": "copy", "detail": f"Vision review failed: {exc}"}],
        }
    if not isinstance(review, Mapping):
        return {
            "passed": False,
            "violations": [{"category": "copy", "detail": "Vision review returned no verdict"}],
        }
    return {
        "passed": bool(review.get("passed")),
        "violations": _normalise_vision_violations(review.get("violations")),
    }


def _instance_from_row(
    row: GeneratedSlide,
    placeholder: GeneratedSlidePlaceholder | None = None,
) -> GeneratedSlideInstance:
    try:
        values = json.loads(row.slot_values_json or "{}")
    except json.JSONDecodeError:
        values = {}
    title = _plain(
        values.get("title")
        or values.get("headline")
        or (placeholder.title if placeholder else "")
        or ""
    )
    module_id = placeholder.module_id if placeholder else ""
    return GeneratedSlideInstance(
        slide_key=row.slide_key,
        template_id=row.template_id,
        template_version=row.template_version,
        tone=row.tone,
        title=title,
        module_id=module_id,
        file_key=row.file_key,
        generated_slide_id=row.id,
        claim_hash=row.claim_hash,
        render_hash=row.render_hash,
    )


def _restore_brand_slide(placeholder: GeneratedSlidePlaceholder) -> PlannedSlide | None:
    """Put back the brand page the placeholder displaced, or drop the slide.

    When the placeholder replaced a weakest body page, restoring that page keeps
    the deck the same length. When the placeholder was *inserted* under the
    ceiling (``replaced_page`` is ``None``), there is nothing to restore — return
    ``None`` so the runner drops the hole from the final deck.
    """
    if placeholder.replaced_page is None:
        return None
    label = placeholder.replaced_label or PAGE_LABELS.get(
        placeholder.replaced_page, f"Page {placeholder.replaced_page}"
    )
    return BrandSlide(placeholder.replaced_page, placeholder.replaced_module_id, label)


def _lookup_cache(db: Any, claim_hash: str) -> GeneratedSlide | None:
    """Return a cached, still-usable slide for ``claim_hash`` or ``None``.

    A hit only counts when its pixels are still in storage: a row whose object
    was lost is treated as a miss so the slide is regenerated rather than a dead
    image URL served.
    """
    row = (
        db.query(GeneratedSlide)
        .filter(
            GeneratedSlide.claim_hash == claim_hash,
            GeneratedSlide.status == "ready",
        )
        .order_by(GeneratedSlide.id.desc())
        .first()
    )
    if row is None or not row.file_key:
        return None
    try:
        if not file_exists(row.file_key):
            return None
    except Exception:  # noqa: BLE001 - storage flakiness is treated as a miss
        return None
    return row


def _approved_guidance(db: Any, template_id: str) -> list[str]:
    """Return bounded, deduplicated, explicitly approved notes for a template."""
    rows = (
        db.query(GeneratedSlideAttempt)
        .filter(
            GeneratedSlideAttempt.template_id == template_id,
            GeneratedSlideAttempt.review_status == "approved",
            GeneratedSlideAttempt.use_as_guidance.is_(True),
        )
        .order_by(
            GeneratedSlideAttempt.reviewed_at.desc(),
            GeneratedSlideAttempt.id.desc(),
        )
        .limit(_GUIDANCE_NOTE_LIMIT * 4)
        .all()
    )
    notes: list[str] = []
    seen: set[str] = set()
    used_chars = 0
    for row in rows:
        note = " ".join((row.review_note or "").split())[:_GUIDANCE_NOTE_CHAR_LIMIT].strip()
        normalized = note.casefold()
        if not note or normalized in seen:
            continue
        remaining = _GUIDANCE_CHAR_LIMIT - used_chars
        if remaining <= 0:
            break
        note = note[:remaining].rstrip()
        if not note:
            break
        notes.append(note)
        seen.add(normalized)
        used_chars += len(note)
        if len(notes) >= _GUIDANCE_NOTE_LIMIT:
            break
    return notes


def _archive_attempt(
    db: Any,
    generation_id: int | None,
    placeholder: GeneratedSlidePlaceholder,
    *,
    attempt_number: int,
    outcome: str,
    gate: str,
    violations: Sequence[str] = (),
    slot_values: Mapping[str, Any] | None = None,
    render_hash: str = "",
    file_key: str = "",
    generated_slide_id: int | None = None,
) -> GeneratedSlideAttempt | None:
    """Append one immutable pipeline event when this run has a generation."""
    if not generation_id:
        return None
    row = GeneratedSlideAttempt(
        generation_id=generation_id,
        generated_slide_id=generated_slide_id,
        placeholder_key=placeholder.slide_key,
        attempt_number=attempt_number,
        claim=placeholder.claim,
        template_id=placeholder.template_id,
        tone=placeholder.tone,
        outcome=outcome,
        gate=gate,
        violations_json=json.dumps(list(violations), ensure_ascii=False),
        slot_values_json=json.dumps(dict(slot_values or {}), ensure_ascii=False),
        render_hash=render_hash,
        file_key=file_key,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _persist_generated_slide(
    db: Any,
    *,
    spec: TemplateSpec,
    claim_hash: str,
    render_hash: str,
    slot_values: Mapping[str, Any],
    file_key: str,
    source_facts: Sequence[Any],
    source_passages: Sequence[Mapping[str, Any]],
    photo_asset_ids: Sequence[int] = (),
) -> GeneratedSlide:
    """Upsert the shared :class:`GeneratedSlide` row race-safely.

    The cache is shared across users, so two requests can render the same
    content-addressed slide at once. ``render_hash`` (and the ``slide_key``
    derived from it) are unique, so a concurrent insert raises
    :class:`IntegrityError`; that is caught and the row the other writer created
    is returned instead, so both callers converge on one shared slide.
    """
    existing = (
        db.query(GeneratedSlide).filter(GeneratedSlide.render_hash == render_hash).first()
    )
    if existing is not None:
        return existing

    slide_key = generated_slide_key(render_hash)
    row = GeneratedSlide(
        slide_key=slide_key,
        claim_hash=claim_hash,
        render_hash=render_hash,
        template_id=spec.template_id,
        template_version=spec.version,
        tone=spec.tone,
        slot_values_json=json.dumps(dict(slot_values), ensure_ascii=False),
        file_key=file_key,
        status="ready",
        source_fact_ids=",".join(
            str(getattr(fact, "id", 0) or 0) for fact in source_facts if getattr(fact, "id", 0)
        ),
        source_asset_ids=",".join(
            dict.fromkeys(
                str(asset_id)
                for asset_id in (
                    *(passage.get("asset_id") for passage in source_passages),
                    *photo_asset_ids,
                )
                if asset_id
            )
        ),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = (
            db.query(GeneratedSlide).filter(GeneratedSlide.render_hash == render_hash).first()
            or db.query(GeneratedSlide).filter(GeneratedSlide.slide_key == slide_key).first()
        )
        if row is None:
            raise
        return row
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Realise one placeholder
# ---------------------------------------------------------------------------


def _realize_one(
    db: Any,
    placeholder: GeneratedSlidePlaceholder,
    facts_by_id: Mapping[int, Any],
    passages: Sequence[Mapping[str, Any]],
    script_text: str,
    *,
    fill_fn: FillFn,
    vision_fn: VisionFn,
    renderer: Any,
    brand_page_fn: BrandPageFn,
    photo_fn: PhotoSourceFn,
    max_attempts: int,
    generation_id: int | None,
    furniture_rejected_templates: set[str],
) -> PlannedSlide | None:
    spec = _spec_or_none(placeholder.template_id)
    if spec is None:
        # The planner should only emit supported templates, but never render an
        # undefined one — fall back to the brand page it displaced.
        _archive_attempt(
            db, generation_id, placeholder,
            attempt_number=0, outcome="unknown_template", gate="template",
            violations=[f"Unknown generated slide template: {placeholder.template_id}"],
        )
        _archive_attempt(
            db, generation_id, placeholder,
            attempt_number=1, outcome="final_degradation", gate="final",
            violations=["Unknown template; restored displaced brand page"],
        )
        return _restore_brand_slide(placeholder)

    if spec.template_id in furniture_rejected_templates:
        reason = (
            f"Template '{spec.template_id}' was suppressed for this run after an earlier "
            "furniture rejection"
        )
        _archive_attempt(
            db, generation_id, placeholder,
            attempt_number=0, outcome="template_suppressed", gate="furniture",
            violations=[reason],
        )
        _archive_attempt(
            db, generation_id, placeholder,
            attempt_number=1, outcome="final_degradation", gate="furniture",
            violations=["Suppressed template; restored displaced brand page"],
        )
        return _restore_brand_slide(placeholder)

    # Source approved photos (recommended pictures only). A template that
    # *requires* a photo but has none available degrades to the brand page it
    # displaced rather than rendering an empty photo layer.
    photos_by_slot, photo_descriptors, photo_asset_ids = _prepare_photos(
        spec, placeholder, photo_fn
    )
    if any(name not in photos_by_slot for name in spec.required_photo_slots):
        missing = [name for name in spec.required_photo_slots if name not in photos_by_slot]
        _archive_attempt(
            db, generation_id, placeholder,
            attempt_number=0, outcome="missing_photo", gate="photo",
            violations=[f"Required approved photo is missing for slot '{name}'" for name in missing],
        )
        _archive_attempt(
            db, generation_id, placeholder,
            attempt_number=1, outcome="final_degradation", gate="final",
            violations=["Required approved photo unavailable; restored displaced brand page"],
        )
        return _restore_brand_slide(placeholder)

    source_facts = [
        facts_by_id[fid] for fid in placeholder.source_fact_ids if fid in facts_by_id
    ]
    source_facts = pair_median_facts(source_facts, pool=list(facts_by_id.values()))
    source_passages = relevant_passages(passages, _placeholder_modules(placeholder))
    claim_hash = compute_claim_hash(
        claim=placeholder.claim,
        module_id=placeholder.module_id,
        template_id=placeholder.template_id,
        tone=placeholder.tone,
        source_facts=source_facts,
        source_passages=source_passages,
    )

    # 1) Shared claim cache — no model/vision/render when valid pixels exist.
    cached = _lookup_cache(db, claim_hash)
    if cached is not None:
        try:
            cached_values = json.loads(cached.slot_values_json or "{}")
        except (json.JSONDecodeError, TypeError):
            cached_values = {}
        _archive_attempt(
            db, generation_id, placeholder,
            attempt_number=0, outcome="cache_hit", gate="cache",
            slot_values=cached_values,
            render_hash=cached.render_hash, file_key=cached.file_key,
            generated_slide_id=cached.id,
        )
        return _instance_from_row(cached, placeholder)

    # 2) Miss: fill + gate, up to the configured attempts, then degrade.
    allowed = allowed_numerals(source_facts, source_passages)
    if placeholder.gap_kind == "objection":
        # Restating the listener's own question quotes their figure; it asserts nothing.
        allowed |= _numerals(placeholder.claim)
    brand_pages = _reference_pages(spec, placeholder, brand_page_fn)
    corrections: list[str] = []
    previous_values: dict[str, Any] = {}
    review_guidance = _approved_guidance(db, spec.template_id)
    attempt_count = max(1, max_attempts)
    for attempt_number in range(1, attempt_count + 1):
        try:
            raw = fill_fn(
                _fill_payload(
                    spec,
                    placeholder,
                    script_text,
                    source_facts,
                    source_passages,
                    corrections,
                    review_guidance,
                    previous_values,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a failed fill is a retryable attempt
            corrections = [f"The copy generator failed ({exc}); return valid slot JSON."]
            _archive_attempt(
                db, generation_id, placeholder,
                attempt_number=attempt_number, outcome="fill_error", gate="fill",
                violations=corrections,
            )
            continue
        values = _repair_budgets(
            spec,
            _resolve_values(spec, raw),
            previous_values,
            final_attempt=attempt_number == attempt_count,
        )
        if (
            placeholder.claim.rstrip().endswith("?")
            and values.get("title")
            and not _plain(str(values["title"])).endswith("?")
        ):
            values["title"] = f"{str(values['title']).rstrip()}?"
        previous_values = values

        # Gate a: schema + exact word/character budgets.
        violations = gate_schema(spec, values) + gate_budgets(spec, values)
        if violations:
            corrections = violations
            _archive_attempt(
                db, generation_id, placeholder,
                attempt_number=attempt_number, outcome="schema_budget",
                gate="schema_budget", violations=violations, slot_values=values,
            )
            continue

        # Gate b: number/factual provenance.
        violations = gate_number_provenance(spec, values, allowed)
        if violations:
            corrections = violations
            _archive_attempt(
                db, generation_id, placeholder,
                attempt_number=attempt_number, outcome="provenance",
                gate="provenance", violations=violations, slot_values=values,
            )
            continue

        # Gate b2: claim hygiene (banned phrases, rounded aggregates, unpaired averages).
        violations = gate_claim_hygiene(spec, values)
        if violations:
            corrections = violations
            _archive_attempt(
                db, generation_id, placeholder,
                attempt_number=attempt_number, outcome="claim_hygiene",
                gate="claim_hygiene", violations=violations, slot_values=values,
            )
            continue

        # Gate c: deterministic copy register (still cheap, before rendering).
        violations = gate_copy_register(spec, values)
        if violations:
            corrections = violations
            _archive_attempt(
                db, generation_id, placeholder,
                attempt_number=attempt_number, outcome="copy_register",
                gate="copy_register", violations=violations, slot_values=values,
            )
            continue

        # Gate d: DOM overflow, via the real renderer (with approved photos).
        render_values = {**spec.fixed_values(), **values}
        try:
            render_result = renderer.render(
                spec.manifest, render_values, photos=photos_by_slot or None
            )
        except SlideOverflowError as exc:
            corrections = [
                "The copy overflows its box even at the minimum size "
                f"({', '.join(exc.slots)}); make it shorter."
            ]
            _archive_attempt(
                db, generation_id, placeholder,
                attempt_number=attempt_number, outcome="overflow", gate="render",
                violations=corrections, slot_values=values,
            )
            continue
        except (BrowserUnavailable, SlideRenderError) as exc:
            # A renderer that cannot draw at all will not be fixed by rewriting
            # copy — degrade to the brand page rather than spin.
            _archive_attempt(
                db, generation_id, placeholder,
                attempt_number=attempt_number, outcome="render_error", gate="render",
                violations=[f"{type(exc).__name__}: {exc}"], slot_values=values,
            )
            _archive_attempt(
                db, generation_id, placeholder,
                attempt_number=attempt_count + 1, outcome="final_degradation", gate="final",
                violations=["Slide rendering failed; restored displaced brand page"],
            )
            return _restore_brand_slide(placeholder)

        render_hash = compute_render_hash(
            template_id=spec.template_id,
            template_version=spec.version,
            slot_values=values,
            font_bundle_version=FONT_BUNDLE_VERSION,
            photo={"photos": photo_descriptors} if photo_descriptors else None,
        )
        attempt_file_key = ""
        if generation_id:
            try:
                attempt_file_key = save_generated_slide_attempt_image(
                    generation_id,
                    placeholder.slide_key,
                    attempt_number,
                    render_hash,
                    render_result.jpeg,
                )
            except Exception as exc:  # noqa: BLE001 - preserve an auditable degradation
                _archive_attempt(
                    db, generation_id, placeholder,
                    attempt_number=attempt_number, outcome="render_error", gate="storage",
                    violations=[f"{type(exc).__name__}: {exc}"], slot_values=values,
                    render_hash=render_hash,
                )
                _archive_attempt(
                    db, generation_id, placeholder,
                    attempt_number=attempt_count + 1, outcome="final_degradation", gate="final",
                    violations=["Rendered attempt could not be archived; restored displaced brand page"],
                )
                return _restore_brand_slide(placeholder)

        # Gate e: Opus multimodal vision review vs three brand pages.
        review = _run_vision(vision_fn, render_result.jpeg, brand_pages, spec, placeholder)
        # Furniture is invariant for a rendered image, but the multimodal
        # classifier is stochastic. A single false furniture verdict currently
        # suppresses the template for the whole run, so require the same image
        # to receive a second furniture verdict before taking that irreversible
        # path. A passing or copy-only confirmation follows the normal flow.
        if not review["passed"] and any(
            violation["category"] == "furniture"
            for violation in review["violations"]
        ):
            review = _run_vision(
                vision_fn, render_result.jpeg, brand_pages, spec, placeholder
            )
        if not review["passed"]:
            typed_violations = review["violations"]
            furniture = [
                violation["detail"]
                for violation in typed_violations
                if violation["category"] == "furniture"
            ]
            copy_violations = [
                violation["detail"]
                for violation in typed_violations
                if violation["category"] == "copy"
            ]
            if furniture:
                furniture_rejected_templates.add(spec.template_id)
                all_details = [violation["detail"] for violation in typed_violations]
                _archive_attempt(
                    db, generation_id, placeholder,
                    attempt_number=attempt_number, outcome="vision_furniture", gate="vision",
                    violations=all_details, slot_values=values,
                    render_hash=render_hash, file_key=attempt_file_key,
                )
                _archive_attempt(
                    db, generation_id, placeholder,
                    attempt_number=attempt_number + 1,
                    outcome="final_degradation", gate="furniture",
                    violations=[
                        "Template furniture cannot be repaired by rewriting copy; "
                        "restored displaced brand page"
                    ],
                )
                return _restore_brand_slide(placeholder)

            corrections = copy_violations or ["The slide is not visually on brand."]
            _archive_attempt(
                db, generation_id, placeholder,
                attempt_number=attempt_number, outcome="vision", gate="vision",
                violations=corrections, slot_values=values,
                render_hash=render_hash, file_key=attempt_file_key,
            )
            continue

        # Pass — content-address the pixels, store, and upsert the shared row.
        try:
            file_key = save_generated_slide_image(render_hash, render_result.jpeg)
            row = _persist_generated_slide(
                db,
                spec=spec,
                claim_hash=claim_hash,
                render_hash=render_hash,
                slot_values=values,
                file_key=file_key,
                source_facts=source_facts,
                source_passages=source_passages,
                photo_asset_ids=photo_asset_ids,
            )
        except Exception as exc:  # noqa: BLE001 - retain attempt and degrade cleanly
            db.rollback()
            _archive_attempt(
                db, generation_id, placeholder,
                attempt_number=attempt_number, outcome="render_error", gate="persistence",
                violations=[f"{type(exc).__name__}: {exc}"], slot_values=values,
                render_hash=render_hash, file_key=attempt_file_key,
            )
            _archive_attempt(
                db, generation_id, placeholder,
                attempt_number=attempt_count + 1, outcome="final_degradation", gate="final",
                violations=["Accepted render could not be persisted; restored displaced brand page"],
            )
            return _restore_brand_slide(placeholder)
        _archive_attempt(
            db, generation_id, placeholder,
            attempt_number=attempt_number, outcome="success", gate="accepted",
            slot_values=values, render_hash=render_hash,
            file_key=attempt_file_key, generated_slide_id=row.id,
        )
        return _instance_from_row(row, placeholder)

    _archive_attempt(
        db, generation_id, placeholder,
        attempt_number=attempt_count + 1, outcome="final_degradation", gate="final",
        violations=corrections or ["All generated slide attempts failed"],
    )
    return _restore_brand_slide(placeholder)


# ---------------------------------------------------------------------------
# Runner entry point
# ---------------------------------------------------------------------------


def realize_generated_slides(
    db: Any,
    plan: Sequence[PlannedSlide],
    *,
    facts: Sequence[Any] | None = None,
    passages: Sequence[Mapping[str, Any]] | None = None,
    script_text_by_slide_key: Mapping[str, str] | None = None,
    brand_deck_file_key: str | None = None,
    recipe_ref: str = "",
    temperature: str = "",
    duration: str = "",
    generation_id: int | None = None,
    fill_fn: Any = _MISSING,
    vision_fn: Any = _MISSING,
    renderer: Any = _MISSING,
    brand_page_fn: Any = _MISSING,
    photo_fn: Any = _MISSING,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[PlannedSlide | None]:
    """Realise every generated placeholder in ``plan``.

    Runs between the approved script and the DeckSpec/PPTX render. Each
    :class:`~backend.pipeline.gaps.GeneratedSlidePlaceholder` becomes either a
    concrete :class:`GeneratedSlideInstance` (rendered + persisted, or reused
    from the shared cache), the brand page it displaced, or ``None`` when an
    inserted placeholder (no displaced page) cannot be realised. Positional
    alignment with ``plan`` is preserved so the list may contain ``None``
    holes. A plan with no placeholders is returned unchanged and touches
    nothing (no model, no DB, no storage), so existing gap-free generations are
    byte-for-byte unaffected.
    """
    result: list[PlannedSlide | None] = list(plan)
    placeholder_indexes = [
        index
        for index, slide in enumerate(result)
        if isinstance(slide, GeneratedSlidePlaceholder)
    ]
    if not placeholder_indexes:
        return result

    resolved_fill = _default_fill_fn if fill_fn is _MISSING else fill_fn
    resolved_vision = _default_vision_fn if vision_fn is _MISSING else vision_fn
    resolved_renderer = get_renderer() if renderer is _MISSING else renderer
    if brand_page_fn is _MISSING:
        def resolved_brand_page_fn(page: int) -> bytes:
            return page_image(page, brand_deck_file_key)
    else:
        resolved_brand_page_fn = brand_page_fn
    if photo_fn is _MISSING:
        resolved_photo_fn = make_recommended_photo_fn(
            db, recipe_ref, temperature=temperature, duration=duration
        )
    else:
        resolved_photo_fn = photo_fn if photo_fn is not None else _no_photos

    all_facts = list(facts) if facts is not None else _load_facts(db)
    facts_by_id = {
        int(getattr(fact, "id", 0) or 0): fact
        for fact in all_facts
        if getattr(fact, "id", 0)
    }
    all_passages = list(passages) if passages is not None else []
    script_texts = script_text_by_slide_key or {}
    furniture_rejected_templates: set[str] = set()

    for index in placeholder_indexes:
        result[index] = _realize_one(
            db,
            result[index],
            facts_by_id,
            all_passages,
            script_texts.get(result[index].slide_key, ""),
            fill_fn=resolved_fill,
            vision_fn=resolved_vision,
            renderer=resolved_renderer,
            brand_page_fn=resolved_brand_page_fn,
            photo_fn=resolved_photo_fn,
            max_attempts=max_attempts,
            generation_id=generation_id,
            furniture_rejected_templates=furniture_rejected_templates,
        )
    return result
