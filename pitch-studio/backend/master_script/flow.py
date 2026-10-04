"""Orchestrator for master_script mode — returns ScriptPhaseResult on the vision deck spine."""

from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy.orm import Session

from backend.config import settings
from backend.master_script.budget import budgeted_generation
from backend.extract import pick_report_passages
from backend.master_script import MASTER_SCRIPT_VERSION
from backend.master_script.editor import edit_and_review, compact_authorities
from backend.master_script.evidence import cards_for_plan, fulfil_requests
from backend.master_script.planner import finalize_with_cards, plan_sections
from backend.master_script.source import (
    DEFAULT_JSON_PATH,
    load_master_script,
    locked_lines_for_sections,
    resolve_route,
)
from backend.master_script.verify import VerifyIssue, verify_script
from backend.master_script.voice import (
    apply_locks_to_script,
    build_script_payload,
    gather_pratham_grounding,
    write_sections_parallel,
)
from backend.models import Asset, Module, Recipe, MasterNoun, EvidenceCard
from backend.pipeline.deck import slide_ceiling_for
from backend.pipeline.gaps import plan_with_generated_slides
from backend.pipeline.resolver import ResolvedRecipe, resolve_recipe
from backend.pipeline.runner import (
    ScriptPhaseResult,
    ScriptTarget,
    _select_founder_quotes,
    _vision_dsai_slides,
)
from backend.pipeline.script_flow import ScriptFlowError
from backend.pipeline.style_guide import latest_style_guide
from backend.pipeline.vision_deck import (
    insert_at_section,
    load_vision_sections,
    module_sequence_from_slides,
    normalize_use_case,
    order_by_headings,
    plan_vision_pages,
)
from backend.pipeline.vision_modules_flow import build_vision_modules_plan
from backend.schemas import ScriptPayload


def _persona_label(db: Session, recipe_ref: str) -> str:
    if not recipe_ref:
        return ""
    recipe = db.query(Recipe).filter(Recipe.ref == recipe_ref).first()
    if recipe is None:
        return ""
    raw = getattr(recipe, "audience_label", "") or ""
    return raw.strip() if isinstance(raw, str) else ""


def _build_vision_spine(
    db: Session,
    target: ScriptTarget,
    resolved: ResolvedRecipe,
) -> tuple[ResolvedRecipe, list[Any], list[Any], dict[str, Any]]:
    """Mirror runner.generate_script_phase vision path for plan + topic_flow."""
    ceiling = slide_ceiling_for(target.duration)
    deck_use_case = normalize_use_case(getattr(target, "deck_use_case", "") or "")
    vision_sections = load_vision_sections(db, deck_use_case) if deck_use_case else []
    if not vision_sections:
        raise ScriptFlowError(
            "master_script mode requires a deck use case with vision rows"
        )

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
    gap_ceiling = len(plan) + max(8, ceiling // 4)
    resolved = ResolvedRecipe(
        ref=resolved.ref,
        module_sequence=module_sequence_from_slides(plan),
        word_budget=resolved.word_budget,
    )
    target.module_sequence = ">".join(resolved.module_sequence)
    db.commit()

    modules = db.query(Module).filter(Module.id.in_(resolved.module_sequence)).all()
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

    vm_plan = build_vision_modules_plan(
        db,
        plan,
        resolved.module_sequence,
        duration=target.duration,
    )
    return resolved, plan, vm_plan.topics, dict(vm_plan.trace)


@budgeted_generation
def generate_script_phase(
    db: Session,
    target: ScriptTarget,
    set_status: Callable[[str], None],
    *,
    dry_run: bool = False,
) -> ScriptPhaseResult | None:
    """Master Script three-engine script phase. Compatible with runner.run deck path."""
    if not dry_run and not DEFAULT_JSON_PATH.exists():
        target.error = (
            "Master Script source has not been imported. Run parse_master_script.py first."
        )
        set_status("failed")
        return None

    set_status("planning")
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

    persona_label = _persona_label(db, target.recipe_ref)
    style_guide = latest_style_guide(db, persona_label=persona_label)
    founder_quotes = _select_founder_quotes(
        db,
        resolved.module_sequence,
        persona_label=persona_label,
    )

    try:
        resolved, plan, topic_flow, vision_trace = _build_vision_spine(
            db, target, resolved
        )
    except ScriptFlowError as exc:
        target.error = str(exc)
        set_status("failed")
        return None

    report_passages = pick_report_passages(
        db.query(Asset).all(), resolved.module_sequence
    )

    doc = load_master_script()
    route = resolve_route(
        doc,
        audience_cluster=target.audience_cluster,
        duration=target.duration,
        channel=target.channel,
        intent=target.intent,
        persona=persona_label,
        deck_use_case=getattr(target, "deck_use_case", "") or "",
        context_note=target.context_note or "",
    )
    locked = locked_lines_for_sections(doc, route.sections)

    set_status("planning")
    beat_plan = plan_sections(
        doc,
        route,
        topic_flow=topic_flow,
        word_budget=resolved.word_budget,
        context_note=target.context_note or "",
        dry_run=dry_run,
        available_evidence=[
            {"noun": noun.canonical, "sections": json.loads(noun.sections_json or "[]"),
             "has_approved_story": any(c.kind == "approved_story" and not c.excluded
                                       for c in noun.evidence_cards)}
            for noun in db.query(MasterNoun).filter(MasterNoun.approved.is_(True)).all()
        ],
    )

    set_status("gathering_evidence")
    room = f"{route.route_id}|{target.audience_cluster}|{target.duration}"
    fulfilled = fulfil_requests(
        db,
        list(beat_plan.get("evidence_requests") or []),
        room=room,
        dry_run=dry_run,
    )
    still_open = [
        {
            "noun": row.get("noun") or "",
            "need": row.get("need") or "",
            "section_id": row.get("section_id") or "",
        }
        for row in fulfilled
        if row.get("verdict") == "insufficient"
    ]

    beat_plan = finalize_with_cards(beat_plan, fulfilled, dry_run=dry_run)
    cards_by_section = cards_for_plan(fulfilled, beat_plan)

    set_status("writing")
    pratham_by_section = gather_pratham_grounding(
        db,
        beat_plan,
        topic_flow,
        persona_label=persona_label,
        context_note=target.context_note or "",
    )
    drafts = write_sections_parallel(
        beat_plan,
        cards_by_section=cards_by_section,
        locked=locked,
        style_guide=style_guide,
        pratham_by_section=pratham_by_section,
        dry_run=dry_run,
    )
    cta = str(beat_plan.get("ask") or route.ask)
    editorial_result = {"passed": True, "reviews": [], "ledger": [], "logical_calls": 0}
    if not dry_run:
        set_status("validating")
        try:
            editorial_result = edit_and_review(
                drafts, cta=cta,
                brief={"audience": route.audience_label, "duration": target.duration,
                       "channel": target.channel, "context": target.context_note or "",
                       "word_budget": resolved.word_budget, "route_sections": route.sections},
                authorities=compact_authorities(doc.raw, set(route.sections)),
                evidence=cards_by_section,
                references=list(dict.fromkeys(pratham_by_section.values())),
                plan=beat_plan, locks=locked,
            )
        except Exception as exc:
            target.script_json = json.dumps({"sections": drafts, "cta": cta}, ensure_ascii=False)
            target.error = f"Editorial review incomplete: {exc}"
            set_status("failed")
            return None
        drafts, cta = editorial_result["sections"], editorial_result["cta"]
        target.master_script_trace_json = json.dumps({"editorial": editorial_result}, ensure_ascii=False)
        if not editorial_result["passed"]:
            target.script_json = json.dumps({"sections": drafts, "cta": cta}, ensure_ascii=False)
            target.validation_report = json.dumps(editorial_result["issues"], ensure_ascii=False)
            target.error = "Editorial review requires attention after one targeted repair"
            set_status("failed")
            return None

    requested_locks: set[str] = set()
    for section in beat_plan.get("sections") or []:
        for item in section.get("locked_slots") or []:
            if isinstance(item, dict) and item.get("id"):
                requested_locks.add(str(item["id"]))

    plan_by_section = {
        str(s.get("section_id") or ""): s for s in (beat_plan.get("sections") or [])
    }

    set_status("validating")
    validation_notes: list[str] = []
    locked_drafts, lock_errors = apply_locks_to_script(drafts, locked)
    script = build_script_payload(
        locked_drafts,
        beat_plan,
        topic_flow,
        cta=cta,
    )
    cards_for_verify: dict[str, list[dict[str, Any]]] = {}
    for section in script.get("sections") or []:
        sid = str(section.get("ms_section_id") or "")
        cards_for_verify[sid] = cards_by_section.get(sid) or []
        cards_for_verify[str(section.get("heading") or "")] = (
            cards_by_section.get(sid) or []
        )
        cards_for_verify[str(section.get("topic_id") or "")] = (
            cards_by_section.get(sid) or []
        )

    report = verify_script(
        script,
        locked={k: v for k, v in locked.items() if k in requested_locks},
        cards_by_section=cards_for_verify,
        plan_by_section=plan_by_section,
        word_budget=resolved.word_budget,
        requested_lock_ids=requested_locks,
    )
    for err in lock_errors:
        report.issues.append(VerifyIssue("lock_placeholder", err))
        report.passed = False

    validation_notes = report.messages()
    drafts = locked_drafts

    try:
        ScriptPayload.model_validate(
            {
                "sections": [
                    {k: v for k, v in section.items() if k != "ms_section_id"}
                    for section in script.get("sections") or []
                ],
                "cta": script.get("cta") or "",
            }
        )
    except Exception as exc:  # noqa: BLE001
        target.error = f"Script payload invalid: {exc}"
        target.validation_report = "\n".join(validation_notes)
        set_status("failed")
        return None

    if not report.passed:
        target.script_json = json.dumps(script, ensure_ascii=False)
        target.validation_report = "\n".join(validation_notes)
        target.error = "Master Script failed deterministic verification; draft retained for review"
        set_status("failed")
        return None

    trace = {
        "version": doc.version or MASTER_SCRIPT_VERSION,
        "models": {
            "ms_planner": settings.ms_planner_model,
            "ms_evidence": settings.ms_evidence_model,
            "ms_voice": settings.ms_voice_model,
            "ms_editor": settings.ms_editor_model,
        },
        "route": {
            "id": route.route_id,
            "audience": route.audience_label,
            "length": route.length,
            "sections": route.sections,
            "compress": route.compress,
            "skip": route.skip,
            "open_with": route.open_with,
            "ask": route.ask,
            "match_reason": route.match_reason,
        },
        "vision": vision_trace,
        "evidence_gaps": still_open,
        "fix_loops": max(0, len(editorial_result["reviews"]) - 1),
        "editorial": editorial_result,
        "sections": [
            {
                "section_id": str(s.get("section_id") or ""),
                "the_one_thing": s.get("the_one_thing"),
                "word_budget": s.get("word_budget"),
                "chosen_cards": s.get("chosen_cards") or [],
                "cards": [
                    {
                        "id": c.get("id"),
                        "claim": c.get("claim"),
                        "figure": c.get("figure"),
                        "figure_label": c.get("figure_label"),
                    }
                    for c in cards_by_section.get(str(s.get("section_id") or ""), [])
                ],
                "locked_slots": s.get("locked_slots") or [],
            }
            for s in (beat_plan.get("sections") or [])
        ],
        "voice_drafts": [
            {
                "section_id": d.get("section_id"),
                "card_ids_used": d.get("card_ids_used"),
                "lock_ids_used": d.get("lock_ids_used"),
                "passage_ids": d.get("passage_ids"),
            }
            for d in drafts
        ],
    }
    if hasattr(target, "master_script_trace_json"):
        target.master_script_trace_json = json.dumps(trace, ensure_ascii=False)

    persist_script = {
        "sections": [
            {k: v for k, v in section.items() if k != "ms_section_id"}
            for section in script.get("sections") or []
        ],
        "cta": script.get("cta") or "",
    }
    target.script_json = json.dumps(persist_script, ensure_ascii=False)
    target.validation_report = "\n".join(validation_notes) if validation_notes else "ok"
    target.error = ""

    founder_quote_ids = ",".join(str(quote.id) for quote in founder_quotes)
    report_asset_ids = ",".join(
        str(item)
        for item in dict.fromkeys(passage["asset_id"] for passage in report_passages)
    )
    target.founder_quote_ids = founder_quote_ids
    target.report_asset_ids = report_asset_ids
    target.report_passages_json = json.dumps(report_passages, ensure_ascii=False)
    db.commit()

    return ScriptPhaseResult(
        resolved=resolved,
        plan=plan,
        topic_flow=topic_flow,
        script=persist_script,
        validation_report=target.validation_report,
        founder_quote_ids=founder_quote_ids,
        report_asset_ids=report_asset_ids,
        report_passages=report_passages,
    )
