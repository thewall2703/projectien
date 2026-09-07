from __future__ import annotations

import json

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import Asset, FounderQuote, Generation, LockedFact, Module, Objection
from backend.pipeline.deck import render_pptx, slide_count_for, spec_from_dict
from backend.pipeline.llm import chat_json
from backend.pipeline.prompts import deck_messages, script_messages
from backend.pipeline.resolver import resolve_recipe
from backend.pipeline.validator import validate_script
from backend.schemas import ScriptPayload
from backend.transcripts import pick_founder_quotes


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


def _select_founder_quotes(db: Session, sequence: list[str]) -> list[FounderQuote]:
    quotes = db.query(FounderQuote).filter(FounderQuote.status == "approved").all()
    return pick_founder_quotes(quotes, sequence)


def _select_objections(db: Session, intent: str) -> list[Objection]:
    approved = db.query(Objection).filter(Objection.status == "approved").all()
    if intent == "I3":
        return approved[:6]
    return approved[:6]


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
        founder_quotes = _select_founder_quotes(db, resolved.module_sequence)

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
        )
        script = chat_json(messages)
        ScriptPayload.model_validate(script)

        _set_status(db, generation, "validating")
        violations = validate_script(script, facts, resolved.module_sequence, resolved.word_budget)
        if violations:
            retry_messages = script_messages(
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
                corrections=violations,
            )
            script = chat_json(retry_messages)
            ScriptPayload.model_validate(script)
            violations = validate_script(script, facts, resolved.module_sequence, resolved.word_budget)
        generation.script_json = json.dumps(script, ensure_ascii=False)
        generation.validation_report = "\n".join(violations)
        if violations:
            generation.error = "Script failed validation after retry"
            _set_status(db, generation, "failed")
            return

        _set_status(db, generation, "generating_deck")
        deck_payload = chat_json(
            deck_messages(script, generation.duration, slide_count_for(generation.duration))
        )
        spec = spec_from_dict(deck_payload)
        generation.deck_spec_json = spec.model_dump_json()

        _set_status(db, generation, "rendering")
        generation.pptx_path = render_pptx(spec, generation.id, resolved.module_sequence)

        assets = _select_assets(db, resolved.module_sequence)
        objections = _select_objections(db, generation.intent)
        generation.asset_ids = ",".join(str(asset.id) for asset in assets)
        generation.objection_ids = ",".join(str(item.id) for item in objections)
        generation.founder_quote_ids = ",".join(str(quote.id) for quote in founder_quotes)
        generation.error = ""
        _set_status(db, generation, "done")
    except Exception as exc:  # noqa: BLE001
        generation.error = f"{type(exc).__name__}: {exc}"
        generation.status = "failed"
        db.commit()
    finally:
        db.close()
