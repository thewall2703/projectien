from __future__ import annotations

import json
from typing import Any

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
from backend.pipeline.deck import slide_count_for
from backend.pipeline.llm import chat_json
from backend.pipeline.prompts import review_pratham_voice, script_messages
from backend.pipeline.resolver import resolve_recipe
from backend.pipeline.style_guide import latest_style_guide
from backend.pipeline.script_flow import ScriptFlowError, load_script_topics, notes_by_page
from backend.pipeline.validator import align_script_to_topics, trim_script_to_budget, validate_script
from backend.schemas import ScriptPayload
from backend.transcripts import pick_founder_quotes
from backend.qa_extraction import rank_objections_for_pitch


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
    approved = db.query(Objection).filter(Objection.status == "approved").all()
    audience_label = ""
    if recipe_ref:
        from backend.models import Recipe

        recipe = db.query(Recipe).filter(Recipe.ref == recipe_ref).first()
        if recipe is not None:
            audience_label = recipe.audience_label or ""
    return rank_objections_for_pitch(
        approved,
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


def run(generation_id: int) -> None:
    db = SessionLocal()
    generation = db.get(Generation, generation_id)
    if generation is None:
        db.close()
        return
    try:
        resolved = resolve_recipe(
            db,
            generation.audience_cluster,
            generation.duration,
            generation.channel,
            generation.intent,
            generation.temperature,
            recipe_ref=generation.recipe_ref or None,
        )
        generation.recipe_ref = resolved.ref
        generation.module_sequence = ">".join(resolved.module_sequence)
        db.commit()

        modules = (
            db.query(Module)
            .filter(Module.id.in_(resolved.module_sequence))
            .all()
        )
        facts = db.query(LockedFact).all()
        persona_label = ""
        if generation.recipe_ref:
            recipe = db.query(Recipe).filter(Recipe.ref == generation.recipe_ref).first()
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
        plan = plan_pages(resolved.module_sequence, slide_count_for(generation.duration))
        try:
            topic_flow = load_script_topics(db, plan, resolved.module_sequence)
        except ScriptFlowError as exc:
            generation.error = str(exc)
            _set_status(db, generation, "failed")
            return
        if not founder_quotes:
            generation.error = "Approved Pratham Mittal transcript excerpts are required before generating a script"
            _set_status(db, generation, "failed")
            return

        _set_status(db, generation, "generating_script")
        messages = script_messages(
            audience_cluster=generation.audience_cluster,
            duration=generation.duration,
            channel=generation.channel,
            intent=generation.intent,
            temperature=generation.temperature,
            context_note=generation.context_note,
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

        _set_status(db, generation, "validating")
        script, violations = _validate_and_repair_budget(
            script, facts, resolved.module_sequence, resolved.word_budget, topic_flow
        )
        for _attempt in range(2):
            if not violations:
                break
            script = chat_json(
                script_messages(
                    audience_cluster=generation.audience_cluster,
                    duration=generation.duration,
                    channel=generation.channel,
                    intent=generation.intent,
                    temperature=generation.temperature,
                    context_note=generation.context_note,
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
            generation.script_json = json.dumps(script, ensure_ascii=False)
            generation.validation_report = "\n".join(violations)
            generation.error = "Script failed validation after retry"
            _set_status(db, generation, "failed")
            return

        review = review_pratham_voice(
            script,
            founder_quotes,
            style_guide=style_guide,
            duration=generation.duration,
            channel=generation.channel,
            intent=generation.intent,
            context_note=generation.context_note,
        )
        for _attempt in range(2):
            if review["passed"]:
                break
            voice_notes = review["violations"] or [
                "The draft does not sound like a Masters' Union employee using the approved founder-derived style."
            ]
            script = chat_json(
                script_messages(
                    audience_cluster=generation.audience_cluster,
                    duration=generation.duration,
                    channel=generation.channel,
                    intent=generation.intent,
                    temperature=generation.temperature,
                    context_note=generation.context_note,
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
                duration=generation.duration,
                channel=generation.channel,
                intent=generation.intent,
                context_note=generation.context_note,
            )
        generation.script_json = json.dumps(script, ensure_ascii=False)
        generation.validation_report = "\n".join(violations or review["violations"])
        if violations or not review["passed"]:
            generation.error = (
                "Script failed the employee voice and style check"
                if not violations
                else "Script failed validation after voice rewrite"
            )
            _set_status(db, generation, "failed")
            return

        _set_status(db, generation, "generating_deck")
        generation.deck_spec_json = deck_spec_from_plan(plan).model_dump_json()

        _set_status(db, generation, "rendering")
        generation.pptx_path = render_pptx(
            plan,
            generation.id,
            notes_by_page=notes_by_page(script),
            file_key=brand_deck_file_key(db),
        )

        assets = _select_assets(db, resolved.module_sequence)
        objections = _select_objections(
            db,
            generation.intent,
            audience_cluster=generation.audience_cluster,
            recipe_ref=generation.recipe_ref,
        )
        generation.asset_ids = ",".join(str(asset.id) for asset in assets)
        generation.objection_ids = ",".join(str(item.id) for item in objections)
        generation.founder_quote_ids = ",".join(str(quote.id) for quote in founder_quotes)
        generation.report_asset_ids = ",".join(
            str(item) for item in dict.fromkeys(passage["asset_id"] for passage in report_passages)
        )
        generation.report_passages_json = json.dumps(report_passages, ensure_ascii=False)
        generation.error = ""
        _set_status(db, generation, "done")
    except Exception as exc:  # noqa: BLE001
        generation.error = f"{type(exc).__name__}: {exc}"
        generation.status = "failed"
        db.commit()
    finally:
        db.close()
