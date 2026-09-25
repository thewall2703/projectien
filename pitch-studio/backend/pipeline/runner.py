from __future__ import annotations

import json
from dataclasses import dataclass
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
from backend.pipeline.gaps import plan_with_generated_slides, vision_candidates_from_instructions
from backend.pipeline.slide_fill import realize_generated_slides
from backend.pipeline.llm import chat_json
from backend.pipeline.prompts import review_pratham_voice, script_messages
from backend.pipeline.resolver import ResolvedRecipe, resolve_recipe
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
    plan_vision_pages,
)
from backend.schemas import ScriptPayload
from backend.transcripts import pick_founder_quotes
from backend.qa_extraction import rank_objections_for_pitch


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

    vision_instructions: list[Any] = []
    if vision_sections:
        # Vision spine: sheet pages in authored order, trimmed to the ceiling.
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
            ceiling=ceiling - len(dsai_slides),
        )
        plan = insert_at_section(
            vision_result.slides,
            dsai_slides,
            vision_result,
            blank[0].section_order if blank else 0,
        )
        gap_ceiling = ceiling
        vision_instructions = list(vision_result.instructions)
        # Script / validator follow the modules the deck actually carries.
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
        plan = plan_pages(resolved.module_sequence, gap_ceiling)

    # Stage 3: vision loglines first (when mapped), then heuristic gaps.
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
        vision_candidates=vision_candidates_from_instructions(vision_instructions),
    )
    if not vision_sections:
        plan = insert_before_closing(plan, dsai_slides)
    try:
        topic_flow = load_script_topics(db, plan, resolved.module_sequence)
    except ScriptFlowError as exc:
        target.error = str(exc)
        set_status("failed")
        return None
    if not founder_quotes:
        target.error = "Approved Pratham Mittal transcript excerpts are required before generating a script"
        set_status("failed")
        return None

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
    )
    script = chat_json(messages)
    script = align_script_to_topics(script, topic_flow, modules)
    ScriptPayload.model_validate(script)

    set_status("validating")
    script, violations = _validate_and_repair_budget(
        script, facts, resolved.module_sequence, resolved.word_budget, topic_flow
    )
    for _attempt in range(2):
        if not violations:
            break
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
            )
        )
        script = align_script_to_topics(script, topic_flow, modules)
        ScriptPayload.model_validate(script)
        script, violations = _validate_and_repair_budget(
            script, facts, resolved.module_sequence, resolved.word_budget, topic_flow
        )
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
            )
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
