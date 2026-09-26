from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.extract import pick_report_passages
from backend.models import Asset, FounderQuote, Generation, LockedFact, Module, Objection, Recipe
from backend.pipeline.brand_deck import (
    brand_deck_file_key,
    deck_spec_from_plan,
    plan_pages,
    render_pptx,
)
from backend.pipeline.deck import slide_ceiling_for
from backend.pipeline.dsai_deck import (
    dsai_slide_budget,
    insert_before_closing,
    is_dsai_request,
    load_dsai_slides,
)
from backend.pipeline.gaps import plan_with_generated_slides
from backend.pipeline.slide_fill import realize_generated_slides
from backend.config import settings
from backend.pipeline.audience_sim import (
    listener_notes,
    listener_passed,
    listener_score,
    serialize_quality_trace,
    simulate_audience,
)
from backend.pipeline.flow_check import check_flow, flow_notes, flow_passed, flow_score
from backend.pipeline.llm import chat_json
from backend.pipeline.prompts import review_pratham_voice, script_messages
from backend.pipeline.resolver import ResolvedRecipe, resolve_recipe
from backend.pipeline.script_plan import plan_script
from backend.pipeline.style_guide import latest_style_guide
from backend.pipeline.script_flow import (
    ScriptFlowError,
    load_script_topics,
    notes_by_slide_key,
    reconcile_realized_slide_mapping,
)
from backend.pipeline.validator import align_script_to_topics, trim_script_to_budget, validate_script
from backend.pipeline.vision_deck import (
    VisionSectionPlan,
    insert_at_section,
    load_vision_sections,
    module_sequence_from_pages,
    module_sequence_from_slides,
    normalize_use_case,
    order_by_headings,
    plan_vision_pages,
)
from backend.pipeline.vision_modules_flow import (
    build_vision_modules_plan,
    check_slide_coverage,
    coverage_rewrite_note,
    normalize_generation_mode,
)
from backend.schemas import ScriptPayload
from backend.transcripts import pick_founder_quotes
from backend.qa_extraction import rank_objections_for_pitch

logger = logging.getLogger(__name__)

def _vision_dsai_slides(
    db: Session,
    recipe_ref: str,
    blank_sections: list[VisionSectionPlan],
    ceiling: int,
) -> list[Any]:
    """DS & AI pages for the sections a ug_dsai vision row leaves blank.

    Topics whose modules overlap those sections' brand modules are preferred;
    with no overlap every eligible topic is considered.
    """
    from backend.models import DeckTopic
    from backend.pipeline import dsai_deck

    asset = dsai_deck.find_dsai_asset(db)
    if asset is None:
        return []
    wanted: list[str] = []
    for section in blank_sections:
        for module_id in module_sequence_from_pages(section.section_pages):
            if module_id != "M14" and module_id not in wanted:
                wanted.append(module_id)
    topics = (
        db.query(DeckTopic)
        .filter(DeckTopic.deck == dsai_deck.DECK_KEY)
        .order_by(DeckTopic.sort_order, DeckTopic.id)
        .all()
    )
    overlapping = [
        topic
        for topic in topics
        if set(wanted) & {part.strip() for part in (topic.module_ids or "").split(",")}
    ]
    return dsai_deck.select_dsai_slides(
        overlapping or topics,
        recipe_ref=recipe_ref,
        sequence=wanted,
        budget=dsai_slide_budget(ceiling),
        labels=dsai_deck.page_labels(asset),
    )


class ScriptTarget(Protocol):
    audience_cluster: str
    duration: str
    channel: str
    intent: str
    temperature: str
    context_note: str
    recipe_ref: str
    deck_use_case: str
    module_sequence: str
    script_json: str
    validation_report: str
    error: str
    founder_quote_ids: str
    report_asset_ids: str
    report_passages_json: str


@dataclass
class ScriptPhaseResult:
    resolved: ResolvedRecipe
    plan: Any
    topic_flow: list[Any]
    script: dict[str, Any]
    validation_report: str
    founder_quote_ids: str
    report_asset_ids: str
    report_passages: list[dict[str, Any]]


def _set_status(db: Session, generation: Generation, status: str) -> None:
    generation.status = status
    db.commit()
    db.refresh(generation)


def _asset_priority(asset: Asset) -> tuple[int, int, int]:
    stored = 0 if asset.file_status == "stored" and asset.file_key else 1
    brand = 1 if "brand deck" in (asset.title or "").lower() else 0
    folder = 1 if asset.file_status == "stored" and not asset.file_key else 0
    return (brand, stored, folder)


def pick_assets(assets: list[Asset], sequence: list[str], limit: int = 8) -> list[Asset]:
    sequence_set = set(sequence)
    buckets: dict[int, list[Asset]] = {}
    for asset in assets:
        ids = [part.strip() for part in (asset.module_ids or "").split(",") if part.strip()]
        matches = [sequence.index(mid) for mid in ids if mid in sequence_set]
        if matches:
            buckets.setdefault(min(matches), []).append(asset)
    for group in buckets.values():
        group.sort(key=_asset_priority)
    selected: list[Asset] = []
    while len(selected) < limit and any(buckets.values()):
        for score in sorted(buckets):
            if buckets[score]:
                selected.append(buckets[score].pop(0))
                if len(selected) >= limit:
                    break
    if len(selected) < limit:
        extras = [
            asset
            for asset in assets
            if asset.status == "exists" and asset not in selected
        ]
        extras.sort(key=_asset_priority)
        selected.extend(extras[: limit - len(selected)])
    return selected


def _select_assets(db: Session, sequence: list[str]) -> list[Asset]:
    return pick_assets(db.query(Asset).all(), sequence)


def _select_founder_quotes(
    db: Session,
    sequence: list[str],
    *,
    persona_label: str = "",
) -> list[FounderQuote]:
    from backend.models import StyleTranscriptPersona

    quotes = db.query(FounderQuote).filter(FounderQuote.status == "approved").all()
    allowed_transcript_ids: set[int] | None = None
    label = (persona_label or "").strip()
    if label:
        rows = (
            db.query(StyleTranscriptPersona.style_transcript_id)
            .filter(StyleTranscriptPersona.persona_label == label)
            .all()
        )
        allowed_transcript_ids = {int(row[0]) for row in rows if row[0]}
    # More snippets = a richer tone reference for the spoken-voice prompt.
    return pick_founder_quotes(
        quotes,
        sequence,
        limit=10,
        persona_label=label,
        allowed_transcript_ids=allowed_transcript_ids,
    )


def _pratham_reference(
    db: Session,
    *,
    persona_label: str,
    topic_flow: list[Any],
    modules: list[Module],
    context_note: str,
) -> tuple[str, dict[str, Any]]:
    """Beat passages + stories + playbook moves matched to this script. Never fails the run."""
    empty_meta: dict[str, Any] = {
        "passage_ids": [],
        "fed_words": 0,
        "fallback_topic_ids": [],
        "empty_topic_ids": [],
        "story_ids": [],
        "story_words": 0,
        "moves": 0,
        "error": "",
    }
    if not persona_label:
        return "", empty_meta
    topics: list[str] = []
    for topic in topic_flow or []:
        payload = topic.to_prompt_dict() if hasattr(topic, "to_prompt_dict") else topic
        if isinstance(payload, dict) and payload.get("title"):
            topics.append(str(payload["title"]))
    if not topics:
        topics = [module.name for module in modules if module.name]
    try:
        from backend.pratham_passages import format_passages_for_prompt, select_passages_for_topics
        from backend.pratham_playbook import PASSAGE_LIMIT, format_reference, select_reference
        from backend.transcript_stories import format_stories_for_prompt, select_stories_for_topics

        beat_block = ""
        selection: dict[str, Any] = {
            "topics": [],
            "fed_words": 0,
            "fallback_topic_ids": [],
            "empty_topic_ids": [],
        }
        if settings.pratham_passages_enabled and topic_flow:
            selection = select_passages_for_topics(
                db,
                topics=topic_flow,
                persona_label=persona_label,
                context_note=context_note or "",
            )
            beat_block = format_passages_for_prompt(selection)
        passage_ids = [
            int(passage["id"])
            for topic in selection.get("topics") or []
            for passage in topic.get("passages") or []
            if passage.get("id") is not None
        ]

        story_block = ""
        story_selection: dict[str, Any] = {"topics": [], "fed_words": 0}
        if settings.transcript_stories_enabled and topic_flow:
            try:
                story_selection = select_stories_for_topics(db, topics=topic_flow)
                story_block = format_stories_for_prompt(story_selection)
            except Exception:  # noqa: BLE001
                logger.exception("Transcript story selection failed for %s", persona_label)
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                story_selection = {"topics": [], "fed_words": 0}
                story_block = ""
        story_ids = [
            int(story["id"])
            for topic in story_selection.get("topics") or []
            for story in topic.get("stories") or []
            if story.get("id") is not None
        ]

        # Avoid feeding the same speech twice when beat passages are present.
        selection_playbook = select_reference(
            db,
            persona_label=persona_label,
            topics=topics,
            context_note=context_note or "",
            passage_limit=0 if beat_block else PASSAGE_LIMIT,
        )
        playbook_block = format_reference(selection_playbook)
        blocks = [block for block in (beat_block, story_block, playbook_block) if block]
        meta = {
            "passage_ids": passage_ids,
            "fed_words": int(selection.get("fed_words") or 0),
            "fallback_topic_ids": list(selection.get("fallback_topic_ids") or []),
            "empty_topic_ids": list(selection.get("empty_topic_ids") or []),
            "story_ids": story_ids,
            "story_words": int(story_selection.get("fed_words") or 0),
            "moves": len(selection_playbook.get("moves") or []),
            "error": "",
        }
        return "\n\n".join(blocks), meta
    except Exception as exc:  # noqa: BLE001
        logger.exception("Pratham reference lookup failed for %s", persona_label)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return "", {**empty_meta, "error": f"{type(exc).__name__}: {exc}"}


def _script_spoken_text(script: dict[str, Any]) -> str:
    parts: list[str] = []
    for section in script.get("sections") or []:
        if isinstance(section, dict) and section.get("text"):
            parts.append(str(section["text"]))
    if script.get("cta"):
        parts.append(str(script["cta"]))
    return " ".join(parts)


def _enrich_pratham_meta(
    meta: dict[str, Any],
    *,
    script: dict[str, Any],
    reference_text: str,
) -> dict[str, Any]:
    from backend.pratham_passages import link_rate, phrase_reuse_rate

    spoken = _script_spoken_text(script)
    return {
        **meta,
        "reuse_rate": round(phrase_reuse_rate(spoken, reference_text), 4),
        "link_rate": round(link_rate(spoken), 4),
    }


def _select_objections(
    db: Session,
    intent: str,
    audience_cluster: str = "",
    recipe_ref: str = "",
) -> list[Objection]:
    # Prefer approved AMA Q&As when available
    ama_approved = (
        db.query(Objection)
        .filter(
            Objection.status == "approved",
            (Objection.source_candidate_id > 0) | (Objection.source_name != ""),
        )
        .all()
    )
    pool = ama_approved if ama_approved else db.query(Objection).filter(Objection.status == "approved").all()
    audience_label = ""
    if recipe_ref:
        from backend.models import Recipe

        recipe = db.query(Recipe).filter(Recipe.ref == recipe_ref).first()
        if recipe is not None:
            audience_label = recipe.audience_label or ""
    return rank_objections_for_pitch(
        pool,
        audience_cluster=audience_cluster,
        audience_label=audience_label,
        intent=intent,
        limit=6,
    )


def _validate_and_repair_budget(
    script: dict[str, Any],
    facts: list[LockedFact],
    sequence: list[str],
    word_budget: int,
    topics: list[Any],
) -> tuple[dict[str, Any], list[str]]:
    violations = validate_script(script, facts, sequence, word_budget, topics=topics)
    if violations and all(item.startswith("Word count ") for item in violations):
        script = trim_script_to_budget(script, word_budget)
        violations = validate_script(script, facts, sequence, word_budget, topics=topics)
    return script, violations


def _rewrite_script_with_corrections(
    *,
    target: ScriptTarget,
    modules: list[Module],
    sequence: list[str],
    facts: list[LockedFact],
    word_budget: int,
    founder_quotes: list[FounderQuote],
    report_passages: list[dict[str, Any]],
    topic_flow: list[Any],
    style_guide: str,
    corrections: list[str],
    draft: dict[str, Any],
    plan: dict[str, Any] | None = None,
    pratham_reference: str = "",
    vision_slide_briefs: str = "",
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Rewrite → budget repair (one repair pass on rule violations) → voice review.

    Returns (script, violations, review).
    """
    script = draft
    violations: list[str] = []
    for _attempt in range(2):
        script = chat_json(
            script_messages(
                audience_cluster=target.audience_cluster,
                duration=target.duration,
                channel=target.channel,
                intent=target.intent,
                temperature=target.temperature,
                context_note=target.context_note,
                modules=modules,
                sequence=sequence,
                facts=facts,
                word_budget=word_budget,
                founder_quotes=founder_quotes,
                report_passages=report_passages,
                topic_flow=topic_flow,
                corrections=violations or corrections,
                draft=script,
                style_guide=style_guide,
                plan=plan,
                pratham_reference=pratham_reference,
                vision_slide_briefs=vision_slide_briefs,
            ),
            role="script_writer",
        )
        script = align_script_to_topics(script, topic_flow, modules)
        ScriptPayload.model_validate(script)
        script, violations = _validate_and_repair_budget(
            script, facts, sequence, word_budget, topic_flow
        )
        if not violations:
            break
    if violations:
        return script, violations, {"passed": False, "violations": violations, "score": 0.0}
    review = review_pratham_voice(
        script,
        founder_quotes,
        style_guide=style_guide,
        duration=target.duration,
        channel=target.channel,
        intent=target.intent,
        context_note=target.context_note,
    )
    return script, violations, review


def _topic_roles(topic_flow: list[Any]) -> list[dict[str, Any]]:
    roles: list[dict[str, Any]] = []
    total = len(topic_flow)
    for index, topic in enumerate(topic_flow):
        payload = topic.to_prompt_dict() if hasattr(topic, "to_prompt_dict") else topic
        if not isinstance(payload, dict):
            continue
        if total <= 1:
            role = "OPENING + CLOSE"
        elif index == 0:
            role = "OPENING"
        elif index == total - 1:
            role = "CLOSE"
        else:
            role = "BODY"
        roles.append({"topic_id": payload.get("topic_id"), "role": role, "title": payload.get("title")})
    return roles


def _run_quality_loop(
    db: Session,
    target: ScriptTarget,
    *,
    script: dict[str, Any],
    modules: list[Module],
    facts: list[LockedFact],
    resolved: ResolvedRecipe,
    founder_quotes: list[FounderQuote],
    report_passages: list[dict[str, Any]],
    topic_flow: list[Any],
    style_guide: str,
    persona_label: str,
    plan: dict[str, Any] | None,
    plan_error: str,
    set_status: Callable[[str], None],
    pratham_reference: str = "",
    pratham_meta: dict[str, Any] | None = None,
    vision_slide_briefs: str = "",
    vision_modules_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flow + listener quality loop. Never fails the parent run."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from backend.audience import latest_listener_profile, listener_examples_for_persona

    flow_on = bool(settings.flow_check_enabled)
    listener_on = bool(settings.listener_enabled)
    if not flow_on and not listener_on:
        if hasattr(target, "quality_trace_json"):
            target.quality_trace_json = serialize_quality_trace(
                [],
                kept_round=0,
                plan_error=plan_error,
                pratham=_enrich_pratham_meta(
                    pratham_meta or {}, script=script, reference_text=pratham_reference
                ),
                vision_modules=vision_modules_trace,
            )
            db.commit()
        return script

    try:
        set_status("reviewing_flow")
    except Exception:
        pass

    rounds: list[dict[str, Any]] = []
    current = script
    best_script = script
    best_score: float | None = None
    best_round = 0
    previous_score: float | None = None

    def finish() -> dict[str, Any]:
        if hasattr(target, "quality_trace_json"):
            target.quality_trace_json = serialize_quality_trace(
                rounds,
                kept_round=best_round,
                plan_error=plan_error,
                pratham=_enrich_pratham_meta(
                    pratham_meta or {}, script=best_script, reference_text=pratham_reference
                ),
                vision_modules=vision_modules_trace,
            )
            db.commit()
        return best_script

    try:
        profile_text = latest_listener_profile(db, persona_label) if persona_label else ""
        examples = (
            listener_examples_for_persona(db, persona_label, limit=5) if persona_label else []
        )
    except Exception as exc:  # noqa: BLE001
        rounds.append({"round": 1, "error": f"{type(exc).__name__}: {exc}"})
        return finish()

    topic_roles = _topic_roles(topic_flow)

    def evaluate(candidate: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
        flow_result: dict[str, Any] | None = None
        listener_result: dict[str, Any] | None = None
        errors: list[str] = []

        def run_flow() -> dict[str, Any]:
            return check_flow(
                candidate,
                plan=plan,
                duration=target.duration,
                audience_cluster=target.audience_cluster,
                temperature=target.temperature,
                topic_roles=topic_roles,
            )

        def run_listener() -> dict[str, Any]:
            return simulate_audience(
                candidate,
                persona_label=persona_label,
                profile_text=profile_text,
                listener_examples=examples,
                audience_cluster=target.audience_cluster,
                duration=target.duration,
                channel=target.channel,
                intent=target.intent,
                temperature=target.temperature,
                context_note=target.context_note,
            )

        futures = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            if flow_on:
                futures[pool.submit(run_flow)] = "flow"
            if listener_on:
                futures[pool.submit(run_listener)] = "listener"
            for future in as_completed(futures):
                kind = futures[future]
                try:
                    value = future.result()
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{kind}:{type(exc).__name__}: {exc}")
                    continue
                if kind == "flow":
                    flow_result = value
                else:
                    listener_result = value
        return flow_result, listener_result, errors

    for round_index in range(3):
        try:
            flow_result, listener_result, eval_errors = evaluate(current)
        except Exception as exc:  # noqa: BLE001
            rounds.append({"round": round_index + 1, "error": f"{type(exc).__name__}: {exc}"})
            break

        scores: list[float] = []
        passed_flags: list[bool] = []
        flow_list: list[str] = []
        listener_list: list[str] = []
        if flow_result is not None:
            scores.append(flow_score(flow_result))
            passed_flags.append(flow_passed(flow_result))
            flow_list = flow_notes(flow_result, limit=10)
        if listener_result is not None:
            scores.append(listener_score(listener_result))
            passed_flags.append(listener_passed(listener_result))
            listener_list = listener_notes(listener_result, limit=8)
        notes: list[str] = []
        for pair in zip_longest(flow_list, listener_list):
            notes.extend(note for note in pair if note and note not in notes)
        notes = notes[:12]
        score = (sum(scores) / len(scores)) if scores else 0.0
        passed = bool(passed_flags) and all(passed_flags)

        round_trace: dict[str, Any] = {
            "round": round_index + 1,
            "flow": flow_result,
            "listener": listener_result,
            "score": score,
            "passed": passed,
            "notes": notes,
            "rewrite": "none",
            "errors": eval_errors,
        }
        rounds.append(round_trace)

        if best_score is None or score > best_score:
            best_script, best_score, best_round = current, score, round_index + 1

        if passed:
            break
        if previous_score is not None and score <= previous_score:
            round_trace["stopped"] = "no_score_improvement"
            break
        previous_score = score
        if round_index == 2 or not notes:
            break

        try:
            rewritten, violations, review = _rewrite_script_with_corrections(
                target=target,
                modules=modules,
                sequence=resolved.module_sequence,
                facts=facts,
                word_budget=resolved.word_budget,
                founder_quotes=founder_quotes,
                report_passages=report_passages,
                topic_flow=topic_flow,
                style_guide=style_guide,
                corrections=notes,
                draft=current,
                plan=plan,
                pratham_reference=pratham_reference,
                vision_slide_briefs=vision_slide_briefs,
            )
        except Exception as exc:  # noqa: BLE001
            round_trace["rewrite"] = "error"
            round_trace["rewrite_error"] = f"{type(exc).__name__}: {exc}"
            break

        if violations or not review.get("passed"):
            round_trace["rewrite"] = "rejected"
            round_trace["rewrite_rejected"] = {
                "violations": violations,
                "voice_passed": bool(review.get("passed")),
                "voice_violations": review.get("violations") or [],
            }
            break

        current = rewritten
        round_trace["rewrite"] = "accepted"

    return finish()


def generate_script_phase(
    db: Session,
    target: ScriptTarget,
    set_status: Callable[[str], None],
) -> ScriptPhaseResult | None:
    """Shared script creation for production generation and Script Testing."""
    resolved = resolve_recipe(
        db,
        target.audience_cluster,
        target.duration,
        target.channel,
        target.intent,
        target.temperature,
        recipe_ref=target.recipe_ref or None,
    )
    target.recipe_ref = resolved.ref
    target.module_sequence = ">".join(resolved.module_sequence)
    db.commit()

    modules = (
        db.query(Module)
        .filter(Module.id.in_(resolved.module_sequence))
        .all()
    )
    facts = db.query(LockedFact).all()
    persona_label = ""
    if target.recipe_ref:
        recipe = db.query(Recipe).filter(Recipe.ref == target.recipe_ref).first()
        if recipe is not None:
            raw_label = getattr(recipe, "audience_label", "") or ""
            if isinstance(raw_label, str):
                persona_label = raw_label.strip()
    founder_quotes = _select_founder_quotes(
        db,
        resolved.module_sequence,
        persona_label=persona_label,
    )
    style_guide = latest_style_guide(db, persona_label=persona_label)
    report_passages = pick_report_passages(db.query(Asset).all(), resolved.module_sequence)
    ceiling = slide_ceiling_for(target.duration)
    deck_use_case = normalize_use_case(getattr(target, "deck_use_case", "") or "")
    vision_sections = load_vision_sections(db, deck_use_case) if deck_use_case else []
    generation_mode = normalize_generation_mode(
        getattr(target, "generation_mode", "") or "classic"
    )
    vision_mode_trace: dict[str, Any] = {"mode": "classic"}
    if generation_mode == "vision_modules" and not vision_sections:
        generation_mode = "classic"
        vision_mode_trace = {
            "mode": "classic",
            "fallback_reason": "vision_modules requested but use case has no vision rows",
        }

    if vision_sections:
        # Vision spine: every Relevant Slides page from the sheet, authored order.
        # For ug_dsai, DS & AI pages stand in for the sections left blank.
        blank = [section for section in vision_sections if not section.pages]
        dsai_slides = (
            _vision_dsai_slides(db, target.recipe_ref, blank, ceiling)
            if deck_use_case == "ug_dsai" and blank
            else []
        )
        vision_result = plan_vision_pages(
            vision_sections,
            target.duration,
            use_case=deck_use_case,
        )
        plan = order_by_headings(
            insert_at_section(
                vision_result.slides,
                dsai_slides,
                vision_result,
                blank[0].section_order if blank else 0,
            )
        )
        # Gap fills may append (Q&A etc.) but must not replace sheet pages.
        gap_ceiling = len(plan) + max(8, ceiling // 4)
        resolved = ResolvedRecipe(
            ref=resolved.ref,
            module_sequence=module_sequence_from_slides(plan),
            word_budget=resolved.word_budget,
        )
        target.module_sequence = ">".join(resolved.module_sequence)
        db.commit()
        modules = db.query(Module).filter(Module.id.in_(resolved.module_sequence)).all()
        report_passages = pick_report_passages(db.query(Asset).all(), resolved.module_sequence)
        founder_quotes = _select_founder_quotes(
            db,
            resolved.module_sequence,
            persona_label=persona_label,
        )
    else:
        # Legacy path: module round-robin, plus optional DSAI from context text.
        dsai_slides = (
            load_dsai_slides(
                db,
                recipe_ref=target.recipe_ref,
                sequence=resolved.module_sequence,
                budget=dsai_slide_budget(ceiling),
            )
            if is_dsai_request(target.context_note)
            else []
        )
        gap_ceiling = ceiling - len(dsai_slides)
        plan = order_by_headings(plan_pages(resolved.module_sequence, gap_ceiling))

    # Stage 3: generate only for story gaps the existing deck cannot cover. The
    # sheet's "New slide logline" briefs are design-team work, not generation input.
    plan = plan_with_generated_slides(
        db,
        plan,
        resolved,
        duration=target.duration,
        context_note=target.context_note,
        intent=target.intent,
        audience_cluster=target.audience_cluster,
        recipe_ref=target.recipe_ref,
        modules=modules,
        ceiling=gap_ceiling,
    )
    if not vision_sections:
        plan = insert_before_closing(plan, dsai_slides)
    vision_slide_briefs = ""
    vision_plan_meta: dict[str, Any] | None = None
    try:
        if generation_mode == "vision_modules" and vision_sections:
            vm_plan = build_vision_modules_plan(
                db,
                plan,
                resolved.module_sequence,
                duration=target.duration,
            )
            topic_flow = vm_plan.topics
            vision_slide_briefs = vm_plan.vision_slide_briefs_block
            vision_plan_meta = vm_plan.trace
            vision_mode_trace = dict(vm_plan.trace)
        else:
            topic_flow = load_script_topics(db, plan, resolved.module_sequence)
    except ScriptFlowError as exc:
        target.error = str(exc)
        set_status("failed")
        return None
    if not founder_quotes:
        target.error = "Approved Pratham Mittal transcript excerpts are required before generating a script"
        set_status("failed")
        return None
    if generation_mode == "vision_modules" and vision_plan_meta is not None:
        from backend.pratham_playbook import format_reference, select_reference

        topic_titles = [
            str(topic.title)
            for topic in topic_flow
            if getattr(topic, "title", None)
        ]
        playbook_block = format_reference(
            select_reference(
                db,
                persona_label=persona_label,
                topics=topic_titles,
                context_note=target.context_note or "",
                passage_limit=0,
            )
        )
        blocks = [block for block in (vm_plan.pratham_by_beat, playbook_block) if block]
        pratham_reference = "\n\n".join(blocks)
        pratham_meta = {
            "passage_ids": [
                int(ref)
                for ref in (vm_plan.fed_ids.get("pratham_passage") or [])
                if str(ref).isdigit()
            ],
            "fed_words": sum(
                len(str(item.get("text") or "").split())
                for topic in topic_flow
                for brief in (getattr(topic, "slide_briefs", None) or [])
                for item in (brief.get("ranked") or [])
                if item.get("source_type") == "pratham_passage"
            ),
            "fallback_topic_ids": [],
            "empty_topic_ids": [],
            "story_ids": [
                int(ref)
                for ref in (vm_plan.fed_ids.get("transcript_story") or [])
                if str(ref).isdigit()
            ],
            "story_words": 0,
            "moves": 0,
            "error": "",
        }
    else:
        pratham_reference, pratham_meta = _pratham_reference(
            db,
            persona_label=persona_label,
            topic_flow=topic_flow,
            modules=modules,
            context_note=target.context_note,
        )

    script_plan: dict[str, Any] | None = None
    plan_error = ""
    if settings.script_plan_enabled:
        try:
            set_status("planning")
        except Exception:
            pass
        try:
            from backend.audience import latest_listener_profile

            listener_profile = (
                latest_listener_profile(db, persona_label) if persona_label else ""
            )
            script_plan = plan_script(
                audience_cluster=target.audience_cluster,
                duration=target.duration,
                channel=target.channel,
                intent=target.intent,
                temperature=target.temperature,
                context_note=target.context_note,
                modules=modules,
                sequence=resolved.module_sequence,
                facts=facts,
                word_budget=resolved.word_budget,
                founder_quotes=founder_quotes,
                report_passages=report_passages,
                topic_flow=topic_flow,
                style_guide=style_guide,
                listener_profile=listener_profile,
                pratham_reference=pratham_reference,
                vision_slide_briefs=vision_slide_briefs,
            )
            if script_plan is None:
                plan_error = "planner returned nothing usable"
        except Exception as exc:  # noqa: BLE001
            plan_error = f"{type(exc).__name__}: {exc}"
            script_plan = None
    if hasattr(target, "script_plan_json"):
        target.script_plan_json = (
            json.dumps(script_plan, ensure_ascii=False) if script_plan else ""
        )
        db.commit()

    set_status("generating_script")
    messages = script_messages(
        audience_cluster=target.audience_cluster,
        duration=target.duration,
        channel=target.channel,
        intent=target.intent,
        temperature=target.temperature,
        context_note=target.context_note,
        modules=modules,
        sequence=resolved.module_sequence,
        facts=facts,
        word_budget=resolved.word_budget,
        founder_quotes=founder_quotes,
        report_passages=report_passages,
        topic_flow=topic_flow,
        style_guide=style_guide,
        plan=script_plan,
        pratham_reference=pratham_reference,
        vision_slide_briefs=vision_slide_briefs,
    )
    script = chat_json(messages, role="script_writer")
    script = align_script_to_topics(script, topic_flow, modules)
    ScriptPayload.model_validate(script)

    narrated_for_coverage: list[tuple[int, str]] = []
    if generation_mode == "vision_modules":
        for topic in topic_flow:
            for brief in getattr(topic, "slide_briefs", None) or []:
                page = brief.get("page")
                if page is None:
                    continue
                narrated_for_coverage.append(
                    (int(page), str(brief.get("label") or f"Page {page}"))
                )
        coverage = check_slide_coverage(script, narrated_for_coverage)
        vision_mode_trace["coverage"] = coverage
    else:
        coverage = {"narrated": 0, "mentioned": 0, "missing": []}

    set_status("validating")
    script, violations = _validate_and_repair_budget(
        script, facts, resolved.module_sequence, resolved.word_budget, topic_flow
    )
    coverage_rewrite_done = False
    for _attempt in range(2):
        if violations:
            script = chat_json(
                script_messages(
                    audience_cluster=target.audience_cluster,
                    duration=target.duration,
                    channel=target.channel,
                    intent=target.intent,
                    temperature=target.temperature,
                    context_note=target.context_note,
                    modules=modules,
                    sequence=resolved.module_sequence,
                    facts=facts,
                    word_budget=resolved.word_budget,
                    founder_quotes=founder_quotes,
                    report_passages=report_passages,
                    topic_flow=topic_flow,
                    corrections=violations,
                    draft=script,
                    style_guide=style_guide,
                    plan=script_plan,
                    pratham_reference=pratham_reference,
                    vision_slide_briefs=vision_slide_briefs,
                ),
                role="script_writer",
            )
            script = align_script_to_topics(script, topic_flow, modules)
            ScriptPayload.model_validate(script)
            script, violations = _validate_and_repair_budget(
                script, facts, resolved.module_sequence, resolved.word_budget, topic_flow
            )
            continue
        if (
            generation_mode == "vision_modules"
            and narrated_for_coverage
            and not coverage_rewrite_done
        ):
            coverage = check_slide_coverage(script, narrated_for_coverage)
            vision_mode_trace["coverage"] = coverage
            coverage_note = coverage_rewrite_note(coverage)
            if coverage_note:
                coverage_rewrite_done = True
                script = chat_json(
                    script_messages(
                        audience_cluster=target.audience_cluster,
                        duration=target.duration,
                        channel=target.channel,
                        intent=target.intent,
                        temperature=target.temperature,
                        context_note=target.context_note,
                        modules=modules,
                        sequence=resolved.module_sequence,
                        facts=facts,
                        word_budget=resolved.word_budget,
                        founder_quotes=founder_quotes,
                        report_passages=report_passages,
                        topic_flow=topic_flow,
                        corrections=[coverage_note],
                        draft=script,
                        style_guide=style_guide,
                        plan=script_plan,
                        pratham_reference=pratham_reference,
                        vision_slide_briefs=vision_slide_briefs,
                    ),
                    role="script_writer",
                )
                script = align_script_to_topics(script, topic_flow, modules)
                ScriptPayload.model_validate(script)
                script, violations = _validate_and_repair_budget(
                    script, facts, resolved.module_sequence, resolved.word_budget, topic_flow
                )
                coverage = check_slide_coverage(script, narrated_for_coverage)
                vision_mode_trace["coverage"] = coverage
                if violations:
                    continue
        break
    if violations:
        target.script_json = json.dumps(script, ensure_ascii=False)
        target.validation_report = "\n".join(violations)
        target.error = "Script failed validation after retry"
        set_status("failed")
        return None

    review = review_pratham_voice(
        script,
        founder_quotes,
        style_guide=style_guide,
        duration=target.duration,
        channel=target.channel,
        intent=target.intent,
        context_note=target.context_note,
    )
    for _attempt in range(2):
        if review["passed"]:
            break
        voice_notes = review["violations"] or [
            "The draft does not sound like a Masters' Union employee using the approved founder-derived style."
        ]
        script = chat_json(
            script_messages(
                audience_cluster=target.audience_cluster,
                duration=target.duration,
                channel=target.channel,
                intent=target.intent,
                temperature=target.temperature,
                context_note=target.context_note,
                modules=modules,
                sequence=resolved.module_sequence,
                facts=facts,
                word_budget=resolved.word_budget,
                founder_quotes=founder_quotes,
                report_passages=report_passages,
                topic_flow=topic_flow,
                corrections=voice_notes,
                draft=script,
                style_guide=style_guide,
                plan=script_plan,
                pratham_reference=pratham_reference,
                vision_slide_briefs=vision_slide_briefs,
            ),
            role="script_writer",
        )
        script = align_script_to_topics(script, topic_flow, modules)
        ScriptPayload.model_validate(script)
        script, violations = _validate_and_repair_budget(
            script, facts, resolved.module_sequence, resolved.word_budget, topic_flow
        )
        if violations:
            continue
        review = review_pratham_voice(
            script,
            founder_quotes,
            style_guide=style_guide,
            duration=target.duration,
            channel=target.channel,
            intent=target.intent,
            context_note=target.context_note,
        )
    target.script_json = json.dumps(script, ensure_ascii=False)
    target.validation_report = "\n".join(violations or review["violations"])
    if violations or not review["passed"]:
        target.error = (
            "Script failed the employee voice and style check"
            if not violations
            else "Script failed validation after voice rewrite"
        )
        set_status("failed")
        return None

    if settings.flow_check_enabled or settings.listener_enabled:
        script = _run_quality_loop(
            db,
            target,
            script=script,
            modules=modules,
            facts=facts,
            resolved=resolved,
            founder_quotes=founder_quotes,
            report_passages=report_passages,
            topic_flow=topic_flow,
            style_guide=style_guide,
            persona_label=persona_label,
            plan=script_plan,
            plan_error=plan_error,
            set_status=set_status,
            pratham_reference=pratham_reference,
            pratham_meta=pratham_meta,
            vision_slide_briefs=vision_slide_briefs,
            vision_modules_trace=vision_mode_trace,
        )
        target.script_json = json.dumps(script, ensure_ascii=False)
    elif hasattr(target, "quality_trace_json"):
        target.quality_trace_json = serialize_quality_trace(
            [],
            kept_round=0,
            plan_error=plan_error,
            pratham=_enrich_pratham_meta(
                pratham_meta, script=script, reference_text=pratham_reference
            ),
            vision_modules=vision_mode_trace,
        )
        db.commit()

    founder_quote_ids = ",".join(str(quote.id) for quote in founder_quotes)
    report_asset_ids = ",".join(
        str(item) for item in dict.fromkeys(passage["asset_id"] for passage in report_passages)
    )
    target.founder_quote_ids = founder_quote_ids
    target.report_asset_ids = report_asset_ids
    target.report_passages_json = json.dumps(report_passages, ensure_ascii=False)
    target.error = ""

    return ScriptPhaseResult(
        resolved=resolved,
        plan=plan,
        topic_flow=topic_flow,
        script=script,
        validation_report=target.validation_report,
        founder_quote_ids=founder_quote_ids,
        report_asset_ids=report_asset_ids,
        report_passages=report_passages,
    )


def run(generation_id: int) -> None:
    db = SessionLocal()
    generation = db.get(Generation, generation_id)
    if generation is None:
        db.close()
        return
    try:
        def set_status(status: str) -> None:
            _set_status(db, generation, status)

        phase = generate_script_phase(db, generation, set_status)
        if phase is None:
            return

        set_status("generating_deck")
        # Stage 4: realise the generated placeholders Stage 3 planted, now that
        # the approved script exists. Each becomes a rendered, cached, persisted
        # generated slide, or degrades to the brand page it displaced. A plan
        # with no placeholders passes through untouched.
        deck_file_key = brand_deck_file_key(db)
        sections_by_topic = {
            int(section.get("topic_id") or 0): str(section.get("text") or "").strip()
            for section in phase.script.get("sections") or []
            if int(section.get("topic_id") or 0)
        }
        script_text_by_slide_key = {
            slide_key: sections_by_topic.get(topic.topic_id, "")
            for topic in phase.topic_flow
            for slide_key in topic.slide_keys
        }
        plan = realize_generated_slides(
            db,
            phase.plan,
            generation_id=generation.id,
            passages=phase.report_passages,
            script_text_by_slide_key=script_text_by_slide_key,
            brand_deck_file_key=deck_file_key,
            recipe_ref=generation.recipe_ref,
            temperature=generation.temperature,
            duration=generation.duration,
        )
        script, _topic_flow = reconcile_realized_slide_mapping(
            phase.script,
            phase.topic_flow,
            phase.plan,
            plan,
        )
        # Inserted placeholders that Stage 4 could not realise leave a None hole;
        # drop them so the deck/PPTX only contain slides that actually exist.
        plan = [slide for slide in plan if slide is not None]
        generation.script_json = json.dumps(script, ensure_ascii=False)
        generation.deck_spec_json = deck_spec_from_plan(plan).model_dump_json()

        set_status("rendering")
        generation.pptx_path = render_pptx(
            plan,
            generation.id,
            notes_by_slide_key=notes_by_slide_key(script, plan),
            file_key=deck_file_key,
        )

        assets = _select_assets(db, phase.resolved.module_sequence)
        objections = _select_objections(
            db,
            generation.intent,
            audience_cluster=generation.audience_cluster,
            recipe_ref=generation.recipe_ref,
        )
        generation.asset_ids = ",".join(str(asset.id) for asset in assets)
        generation.objection_ids = ",".join(str(item.id) for item in objections)
        generation.error = ""
        set_status("done")
    except Exception as exc:  # noqa: BLE001
        generation.error = f"{type(exc).__name__}: {exc}"
        generation.status = "failed"
        db.commit()
    finally:
        db.close()
