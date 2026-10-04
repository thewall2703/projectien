"""Engine 1 — Planner grounded in the Master Script."""

from __future__ import annotations
from backend.master_script.budget import compact_global_rules

import json
from typing import Any

from backend.master_script.source import MasterScriptDoc, RoutePlan
from backend.pipeline.llm import chat_json
from backend.master_script.editorial import DECK_ONLY

PLANNER_SYSTEM = """You are the Master Script Planner for Masters' Union University pitches.
You decide WHAT to say and WHY — never how. Follow the Master Script section briefs exactly.
Output JSON only.

For each section on the route, produce:
- section_id
- the_one_thing (from the Master Script)
- premises (list)
- locked_slots: list of {id, reason} for LOCKED lines that must appear (use the given lock ids)
- script_cues: list of {slides, cue, topic_id?, slide_keys?} mapped to the deck topic when possible
- Each script_cue and evidence_request must include source_quote, copied verbatim from its own section.
- word_budget (int)
- evidence_requests: list of {noun, need, section_id} — proof the Evidence engine must fetch
- compress (bool) when the route marks the section as compressed

Rules:
- Do not invent numbers or claims.
- Follow required_audience_coverage even when the approved story library has no entry:
  examples already in the Master Script are available as source beats. The library is
  additional evidence, not a whitelist of permitted source examples.
- Evidence requests name a noun and the specific proof needed (e.g. "PlaySuper: labelled raised figure").
- Prefer fewer, sharper evidence requests over exhaustive ones.
- Prefer relevant subjects in available_evidence, especially those with an approved
  story. Use their exact canonical noun in evidence requests; do not invent coverage.
- For facilities and programmes, request concrete student usefulness: what students can
  do, how the resource enables it, and why it matters. Named student success stories
  and measured outcomes are optional enrichment, not required evidence formats.
- source_text is the authoritative section, including BLOCKED items and delivery rules.
- Pending sign-off passages cannot be used as approved facts. Do not request their locks.
"""
PLANNER_SYSTEM += "\n" + DECK_ONLY


def _has_section_anchor(row: dict[str, Any], brief: dict[str, Any]) -> bool:
    quote = " ".join(str(row.get("source_quote") or "").split())
    if len(quote) < 20:
        return False
    source = " ".join(str(brief.get("source_text") or "").split())
    if quote in source:
        return True
    return any(quote in " ".join(str(p).split()) for p in brief.get("premises") or [])


def _section_brief(doc: MasterScriptDoc, section_id: str) -> dict[str, Any]:
    section = doc.section(section_id) or {"id": section_id}
    locked = []
    for index, item in enumerate(section.get("locked") or [], start=1):
        if isinstance(item, dict):
            if item.get("pending_signoff"):
                continue
            locked.append(
                {
                    "id": str(item.get("id") or f"S{section_id}.{index}"),
                    "text": str(item.get("text") or ""),
                    "pending_signoff": bool(item.get("pending_signoff")),
                }
            )
        else:
            locked.append({"id": f"S{section_id}.{index}", "text": str(item), "pending_signoff": False})
    return {
        "id": section_id,
        "title": section.get("title") or section_id,
        "slides": section.get("slides") or [],
        "the_one_thing": section.get("the_one_thing") or "",
        "premises": section.get("premises") or [],
        "script_cues": section.get("script_cues") or [],
        "locked": locked,
        "blocked": section.get("blocked") or [],
        "modulate": section.get("modulate") or "",
        "expect": section.get("expect") or [],
        "source_text": section.get("source_text") or "",
    }


def _topic_map(topic_flow: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for topic in topic_flow or []:
        pages = list(getattr(topic, "pages", None) or [])
        rows.append(
            {
                "topic_id": int(getattr(topic, "topic_id", 0) or 0),
                "title": str(getattr(topic, "title", "") or ""),
                "pages": pages,
                "slide_keys": list(getattr(topic, "slide_keys", None) or []),
                "module_id": str(getattr(topic, "module_id", "") or ""),
            }
        )
    return rows


def _map_cues_to_topics(
    cues: list[Any],
    slides: list[int],
    topic_flow: list[Any],
) -> list[dict[str, Any]]:
    page_to_topic: dict[int, Any] = {}
    for topic in topic_flow or []:
        for page in getattr(topic, "pages", None) or []:
            try:
                page_to_topic[int(page)] = topic
            except (TypeError, ValueError):
                continue
    mapped: list[dict[str, Any]] = []
    for cue in cues or []:
        if isinstance(cue, dict):
            row = dict(cue)
        else:
            row = {"cue": str(cue), "slides": ""}
        cue_slides: list[int] = []
        raw_slides = row.get("slides") or slides[:1]
        if isinstance(raw_slides, list):
            for item in raw_slides:
                try:
                    cue_slides.append(int(item))
                except (TypeError, ValueError):
                    pass
        elif isinstance(raw_slides, str):
            for part in re_split_pages(raw_slides):
                cue_slides.append(part)
        topic = None
        for page in cue_slides or slides[:1]:
            topic = page_to_topic.get(int(page))
            if topic is not None:
                break
        if topic is not None:
            row["topic_id"] = int(getattr(topic, "topic_id", 0) or 0)
            row["slide_keys"] = list(getattr(topic, "slide_keys", None) or [])
            row["topic_title"] = str(getattr(topic, "title", "") or "")
            row["module_id"] = str(getattr(topic, "module_id", "") or "")
        mapped.append(row)
    return mapped


def re_split_pages(raw: str) -> list[int]:
    import re

    pages: list[int] = []
    for match in re.finditer(r"\d+", raw or ""):
        pages.append(int(match.group(0)))
    return pages


def _fallback_plan(
    doc: MasterScriptDoc,
    route: RoutePlan,
    topic_flow: list[Any],
    word_budget: int,
) -> dict[str, Any]:
    per = max(40, int(word_budget / max(1, len(route.sections))))
    sections: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    for sid in route.sections:
        brief = _section_brief(doc, sid)
        compress = sid in route.compress
        budget = max(30, per // 2) if compress else per
        cues = _map_cues_to_topics(brief.get("script_cues") or [], brief.get("slides") or [], topic_flow)
        locked_slots = [
            {"id": item["id"], "reason": "Master Script LOCKED line"}
            for item in brief.get("locked") or []
            if item.get("text")
        ]
        # Heuristic evidence asks from the one thing / premises nouns later filled by coverage.
        for premise in (brief.get("premises") or [])[:2]:
            requests.append(
                {
                    "noun": "",
                    "need": str(premise)[:200],
                    "section_id": sid,
                }
            )
        sections.append(
            {
                "section_id": sid,
                "heading": brief.get("title") or sid,
                "the_one_thing": brief.get("the_one_thing") or "",
                "premises": brief.get("premises") or [],
                "locked_slots": locked_slots,
                "script_cues": cues,
                "word_budget": budget,
                "compress": compress,
                "evidence_requests": [r for r in requests if r["section_id"] == sid],
                "chosen_cards": [],
            }
        )
    return {
        "open_with": route.open_with,
        "ask": route.ask,
        "sections": sections,
        "evidence_requests": requests,
    }


def plan_sections(
    doc: MasterScriptDoc,
    route: RoutePlan,
    *,
    topic_flow: list[Any],
    word_budget: int,
    context_note: str = "",
    available_evidence: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    briefs = [_section_brief(doc, sid) for sid in route.sections]
    if dry_run:
        return _fallback_plan(doc, route, topic_flow, word_budget)

    payload = {
        "required_audience_coverage": next(
            (r.get("required_coverage", {}) for r in doc.routing if r.get("id") == route.route_id), {}
        ),
        "route": {
            "id": route.route_id,
            "audience": route.audience_label,
            "length": route.length,
            "sections": route.sections,
            "compress": route.compress,
            "skip": route.skip,
            "open_with": route.open_with,
            "ask": route.ask,
            "seeking": route.seeking,
        },
        "word_budget": word_budget,
        "context_note": context_note,
        "available_evidence": available_evidence or [],
        "section_briefs": briefs,
        "deck_topics": _topic_map(topic_flow),
        "global_rules": compact_global_rules(doc.raw),
    }
    try:
        result = chat_json(
            [
                {"role": "system", "content": PLANNER_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        "Build the beat plan and evidence requests for this room.\n"
                        + json.dumps(payload, ensure_ascii=False)
                    ),
                },
            ],
            role="ms_planner",
        )
    except Exception:
        return _fallback_plan(doc, route, topic_flow, word_budget)

    sections_out: list[dict[str, Any]] = []
    all_requests: list[dict[str, Any]] = []
    by_id = {
        str(item.get("section_id") or ""): item
        for item in (result.get("sections") or [])
        if isinstance(item, dict)
    }
    for sid in route.sections:
        brief = _section_brief(doc, sid)
        item = by_id.get(sid) or {}
        anchored_cues = [c for c in item.get("script_cues") or []
                         if isinstance(c, dict) and _has_section_anchor(c, brief)]
        cues = _map_cues_to_topics(
            anchored_cues or brief.get("script_cues") or [],
            brief.get("slides") or [],
            topic_flow,
        )
        locked_slots = item.get("locked_slots")
        if not isinstance(locked_slots, list) or not locked_slots:
            locked_slots = [
                {"id": lock["id"], "reason": "Master Script LOCKED line"}
                for lock in brief.get("locked") or []
                if lock.get("text")
            ]
        allowed_lock_ids = {lock["id"] for lock in brief.get("locked") or []}
        locked_slots = [lock for lock in locked_slots if isinstance(lock, dict) and lock.get("id") in allowed_lock_ids]
        requests = []
        for req in item.get("evidence_requests") or []:
            if not isinstance(req, dict) or not _has_section_anchor(req, brief):
                continue
            row = {
                "noun": str(req.get("noun") or "").strip(),
                "need": str(req.get("need") or "").strip(),
                "section_id": sid,
                "source_quote": req["source_quote"],
            }
            if row["need"] or row["noun"]:
                requests.append(row)
                all_requests.append(row)
        sections_out.append(
            {
                "section_id": sid,
                "heading": brief.get("title") or sid,
                "the_one_thing": str(brief.get("the_one_thing") or ""),
                "premises": list(brief.get("premises") or []),
                "locked_slots": locked_slots,
                "script_cues": cues,
                "word_budget": int(item.get("word_budget") or (word_budget // max(1, len(route.sections)))),
                "compress": bool(item.get("compress") or sid in route.compress),
                "evidence_requests": requests,
                "chosen_cards": [],
            }
        )
    # Top-level requests must meet the same section-source boundary.
    for req in result.get("evidence_requests") or []:
        if isinstance(req, dict) and req not in all_requests:
            sid = str(req.get("section_id") or "")
            if sid not in route.sections or not _has_section_anchor(req, _section_brief(doc, sid)):
                continue
            all_requests.append(
                {
                    "noun": str(req.get("noun") or "").strip(),
                    "need": str(req.get("need") or "").strip(),
                    "section_id": str(req.get("section_id") or ""),
                    "source_quote": req["source_quote"],
                }
            )
    return {
        "open_with": route.open_with,
        "ask": route.ask,
        "sections": sections_out,
        "evidence_requests": all_requests,
    }


FINALIZE_SYSTEM = """You are the Master Script Planner finalising evidence choices.
Given the beat plan and returned evidence cards, pick the cards to use per section.
Output JSON: {"sections":[{"section_id":"...","chosen_cards":[{"card_id":123,"reason":"..."}],"drop_requests":["..."]}]}
Only cite card_ids that appear in the supplied cards. Prefer checkable, labelled figures.
For a storytelling need, prefer a relevant approved_story over a collection of generic facts.
"""


def finalize_with_cards(
    plan: dict[str, Any],
    fulfilled: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Second planner pass: attach chosen card ids to each section."""
    cards_by_section: dict[str, list[dict[str, Any]]] = {}
    for row in fulfilled:
        sid = str(row.get("section_id") or "")
        for card in row.get("cards") or []:
            cards_by_section.setdefault(sid, []).append(card)

    if dry_run:
        for section in plan.get("sections") or []:
            sid = str(section.get("section_id") or "")
            chosen = []
            for card in cards_by_section.get(sid) or []:
                if card.get("verdict") == "use":
                    chosen.append(
                        {
                            "card_id": card.get("id"),
                            "reason": "deterministic top card",
                        }
                    )
            section["chosen_cards"] = chosen[:3]
        return plan

    try:
        result = chat_json(
            [
                {"role": "system", "content": FINALIZE_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"plan_sections": plan.get("sections") or [], "fulfilled": fulfilled},
                        ensure_ascii=False,
                    ),
                },
            ],
            role="ms_planner",
        )
    except Exception:
        return finalize_with_cards(plan, fulfilled, dry_run=True)

    by_id = {
        str(item.get("section_id") or ""): item
        for item in (result.get("sections") or [])
        if isinstance(item, dict)
    }
    valid_ids = {
        int(card.get("id"))
        for row in fulfilled
        for card in (row.get("cards") or [])
        if str(card.get("id") or "").isdigit() or isinstance(card.get("id"), int)
    }
    for section in plan.get("sections") or []:
        sid = str(section.get("section_id") or "")
        chosen = []
        for item in (by_id.get(sid) or {}).get("chosen_cards") or []:
            if not isinstance(item, dict):
                continue
            try:
                card_id = int(item.get("card_id"))
            except (TypeError, ValueError):
                continue
            if card_id not in valid_ids:
                continue
            chosen.append({"card_id": card_id, "reason": str(item.get("reason") or "")})
        section["chosen_cards"] = chosen
    return plan
