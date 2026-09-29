"""Engine 2 — Voice: rewrite the plan in Pratham's spoken style."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from sqlalchemy.orm import Session

from backend.master_script.verify import substitute_locked_placeholders
from backend.pipeline.llm import chat_json
from backend.pratham_passages import select_passages_for_topics
from backend.pratham_playbook import format_reference, select_reference
from backend.master_script.editorial import DECK_ONLY, STAGE_VOICE

VOICE_SYSTEM = """You write spoken pitch script in Pratham Mittal's register for Masters' Union University.
You may ONLY use facts from the supplied evidence cards (by card_id) and LOCKED lines.
Pratham references teach cadence, vocabulary, explanation and rhetorical style ONLY.
Never import an anecdote, name, figure or factual claim from those style references.
Preserve the planner's argument and distinguish reported anecdotes from verified facts.
The presenter is an employee, not Pratham. Refer to Pratham in third person when
he participates in a story; never inherit his first-person experiences.
Cards of kind approved_story are human-approved pitch inserts. Prefer their wording,
concrete details and narrative movement over generic descriptions; adapt only the
transition and length needed for the section. Keep sources in metadata, not spoken
report/transcript citations. Preserve any factual qualifications in the approved text.
For every LOCKED line you must insert the placeholder exactly as {{LOCK:<id>}} — never paraphrase a locked line.
Do not invent numbers, nouns, or claims. Do not say business school, B-school, MU, MUU, accredited, youngest, first practitioner-led, or endorsed.
If you mention an average package, the median must appear in the same or adjacent sentence.
Output JSON: {"heading":"...","text":"...","card_ids_used":[1,2],"lock_ids_used":["S1.1"],"passage_ids":[...]}
"""
VOICE_SYSTEM += "\n" + STAGE_VOICE + "\n" + DECK_ONLY

STITCH_SYSTEM = """You smooth transitions between already-written Master Script sections.
Keep every {{LOCK:...}} placeholder exactly. Do not add claims or numbers.
Return JSON: {"sections":[{"section_id":"...","text":"..."}],"cta":"..."}
"""
STITCH_SYSTEM += "\n" + STAGE_VOICE

_LOCK_RE = re.compile(r"\{\{LOCK:[^}]+\}\}")


def _passage_block(selection: dict[str, Any]) -> str:
    parts: list[str] = []
    for topic in selection.get("topics") or []:
        title = topic.get("title") or topic.get("topic_id")
        for passage in topic.get("passages") or []:
            text = str(passage.get("text") or "").strip()
            if text:
                parts.append(f"[{title}] {text}")
    return "\n\n".join(parts)


def _topics_for_section(section_plan: dict[str, Any], topic_flow: list[Any]) -> list[Any]:
    wanted: set[int] = set()
    for cue in section_plan.get("script_cues") or []:
        if isinstance(cue, dict) and cue.get("topic_id"):
            try:
                wanted.add(int(cue["topic_id"]))
            except (TypeError, ValueError):
                pass
    if not wanted:
        return list(topic_flow or [])[:2]
    return [t for t in topic_flow or [] if int(getattr(t, "topic_id", 0) or 0) in wanted]


def write_section(
    section_plan: dict[str, Any],
    *,
    cards: list[dict[str, Any]],
    locked: dict[str, str],
    style_guide: str,
    pratham_block: str,
    open_with: str = "",
    is_first: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    sid = str(section_plan.get("section_id") or "")
    lock_ids = [
        str(item.get("id") or "")
        for item in (section_plan.get("locked_slots") or [])
        if isinstance(item, dict) and item.get("id")
    ]
    allowed_locks = {lid: locked[lid] for lid in lock_ids if lid in locked}

    if dry_run:
        chunks: list[str] = []
        if is_first and open_with:
            chunks.append(open_with)
        if section_plan.get("the_one_thing"):
            chunks.append(str(section_plan["the_one_thing"]))
        for lid in lock_ids:
            if lid in allowed_locks:
                chunks.append(f"{{{{LOCK:{lid}}}}}")
        for card in cards[:2]:
            claim = str(card.get("claim") or "").strip()
            if claim:
                chunks.append(claim)
        text = " ".join(chunks).strip()
        text, _ = substitute_locked_placeholders(text, allowed_locks, fail_on_unknown=False)
        # Keep placeholders in dry-run output for the stitch/verify path to substitute later.
        text = " ".join(chunks).strip()
        return {
            "section_id": sid,
            "heading": str(section_plan.get("the_one_thing") or sid)[:120],
            "text": text,
            "card_ids_used": [int(c["id"]) for c in cards if c.get("id") is not None][:3],
            "lock_ids_used": lock_ids,
            "passage_ids": [],
        }

    card_payload = [
        {
            "id": c.get("id"),
            "kind": c.get("kind"),
            "claim": c.get("claim"),
            "figure": c.get("figure"),
            "figure_label": c.get("figure_label"),
            "checkability": c.get("checkability"),
        }
        for c in cards
    ]
    user = {
        "section": section_plan,
        "cards": card_payload,
        "locked_placeholders": [
            {"id": lid, "insert_as": f"{{{{LOCK:{lid}}}}}"} for lid in lock_ids
        ],
        "style_guide": style_guide[:4000],
        "pratham_reference": pratham_block[:6000],
        "open_with": open_with if is_first else "",
        "word_budget": section_plan.get("word_budget") or 200,
    }
    result = chat_json(
        [
            {"role": "system", "content": VOICE_SYSTEM},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        role="ms_voice",
    )
    text = str(result.get("text") or "").strip()
    # Ensure requested locks appear as placeholders.
    for lid in lock_ids:
        token = f"{{{{LOCK:{lid}}}}}"
        if token not in text and lid in allowed_locks:
            text = f"{text} {token}".strip()
    return {
        "section_id": sid,
        "heading": str(section_plan.get("heading") or section_plan.get("the_one_thing") or sid)[:160],
        "text": text,
        "card_ids_used": [
            int(x)
            for x in (result.get("card_ids_used") or [])
            if str(x).lstrip("-").isdigit()
        ],
        "lock_ids_used": [
            str(x) for x in (result.get("lock_ids_used") or lock_ids)
        ],
        "passage_ids": [
            int(x)
            for x in (result.get("passage_ids") or [])
            if str(x).lstrip("-").isdigit()
        ],
    }


def fix_section(
    draft: dict[str, Any],
    *,
    issues: list[str],
    cards: list[dict[str, Any]],
    lock_ids: list[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        text = str(draft.get("text") or "")
        # Strip banned abbreviations in dry-run.
        text = re.sub(r"(?<![A-Za-z])MUU(?![A-Za-z])", "the university", text)
        text = re.sub(r"(?<![A-Za-z])MU(?![A-Za-z])(?!\s+Ventures)", "Masters' Union University", text)
        text = re.sub(r"\bbusiness schools?\b", "university", text, flags=re.I)
        text = re.sub(r"\bb[\s\-]?schools?\b", "university", text, flags=re.I)
        draft = dict(draft)
        draft["text"] = text
        return draft

    result = chat_json(
        [
            {"role": "system", "content": VOICE_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "fix",
                        "issues": issues,
                        "draft": draft,
                        "cards": cards,
                        "lock_ids": lock_ids,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        role="ms_voice",
    )
    out = dict(draft)
    out["text"] = str(result.get("text") or draft.get("text") or "")
    out["heading"] = str(result.get("heading") or draft.get("heading") or "")
    return out


def write_sections_parallel(
    plan: dict[str, Any],
    *,
    cards_by_section: dict[str, list[dict[str, Any]]],
    locked: dict[str, str],
    style_guide: str,
    pratham_by_section: dict[str, str],
    dry_run: bool = False,
    max_workers: int = 4,
) -> list[dict[str, Any]]:
    sections = list(plan.get("sections") or [])
    results: dict[str, dict[str, Any]] = {}

    def job(index: int, section_plan: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        sid = str(section_plan.get("section_id") or "")
        return sid, write_section(
            section_plan,
            cards=cards_by_section.get(sid) or [],
            locked=locked,
            style_guide=style_guide,
            pratham_block=pratham_by_section.get(sid) or "",
            open_with=str(plan.get("open_with") or ""),
            is_first=index == 0,
            dry_run=dry_run,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(job, index, section)
            for index, section in enumerate(sections)
        ]
        for future in as_completed(futures):
            sid, row = future.result()
            results[sid] = row

    return [results[str(s.get("section_id"))] for s in sections if str(s.get("section_id")) in results]


def stitch_sections(
    drafts: list[dict[str, Any]],
    *,
    ask: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run or len(drafts) <= 1:
        return {
            "sections": drafts,
            "cta": ask,
        }
    try:
        result = chat_json(
            [
                {"role": "system", "content": STITCH_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"sections": drafts, "ask": ask},
                        ensure_ascii=False,
                    ),
                },
            ],
            role="ms_voice",
        )
    except Exception:
        return {"sections": drafts, "cta": ask}

    by_id = {
        str(item.get("section_id") or ""): item
        for item in (result.get("sections") or [])
        if isinstance(item, dict)
    }
    stitched: list[dict[str, Any]] = []
    for draft in drafts:
        sid = str(draft.get("section_id") or "")
        item = by_id.get(sid) or {}
        text = str(item.get("text") or draft.get("text") or "")
        # Preserve placeholders if the stitch model dropped them.
        for match in _LOCK_RE.finditer(str(draft.get("text") or "")):
            token = match.group(0)
            if token not in text:
                text = f"{text} {token}".strip()
        row = dict(draft)
        row["text"] = text
        stitched.append(row)
    return {
        "sections": stitched,
        "cta": str(result.get("cta") or ask),
    }


def gather_pratham_grounding(
    db: Session,
    plan: dict[str, Any],
    topic_flow: list[Any],
    *,
    persona_label: str,
    context_note: str = "",
) -> dict[str, str]:
    out: dict[str, str] = {}
    playbook = format_reference(
        select_reference(
            db,
            persona_label=persona_label,
            topics=[
                str(section.get("the_one_thing") or section.get("section_id") or "")
                for section in (plan.get("sections") or [])
            ],
            context_note=context_note,
        )
    )
    for section in plan.get("sections") or []:
        sid = str(section.get("section_id") or "")
        topics = _topics_for_section(section, topic_flow)
        try:
            selection = select_passages_for_topics(
                db,
                topics=topics or topic_flow[:1],
                persona_label=persona_label,
                context_note=context_note,
            )
            block = _passage_block(selection)
        except Exception:
            block = ""
        out[sid] = "\n\n".join(part for part in (block, playbook) if part)
    return out


def apply_locks_to_script(
    drafts: list[dict[str, Any]],
    locked: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    out: list[dict[str, Any]] = []
    for draft in drafts:
        text, errs = substitute_locked_placeholders(
            str(draft.get("text") or ""), locked, fail_on_unknown=True
        )
        errors.extend(errs)
        row = dict(draft)
        row["text"] = text
        out.append(row)
    return out, errors


def build_script_payload(
    drafts: list[dict[str, Any]],
    plan: dict[str, Any],
    topic_flow: list[Any],
    *,
    cta: str,
) -> dict[str, Any]:
    """Map Master Script sections onto vision topic_flow entries."""
    # Prefer cue topic_ids; fall back to covering topic_flow in order.
    used_topics: set[int] = set()
    sections_out: list[dict[str, Any]] = []
    plan_by_id = {
        str(s.get("section_id") or ""): s for s in (plan.get("sections") or [])
    }
    topic_list = list(topic_flow or [])

    def next_topic() -> Any | None:
        for topic in topic_list:
            tid = int(getattr(topic, "topic_id", 0) or 0)
            if tid and tid not in used_topics:
                used_topics.add(tid)
                return topic
        return topic_list[0] if topic_list else None

    for draft in drafts:
        sid = str(draft.get("section_id") or "")
        section_plan = plan_by_id.get(sid) or {}
        topic = None
        for cue in section_plan.get("script_cues") or []:
            if isinstance(cue, dict) and cue.get("topic_id"):
                try:
                    tid = int(cue["topic_id"])
                except (TypeError, ValueError):
                    continue
                for candidate in topic_list:
                    if int(getattr(candidate, "topic_id", 0) or 0) == tid:
                        topic = candidate
                        used_topics.add(tid)
                        break
            if topic is not None:
                break
        if topic is None:
            topic = next_topic()
        if topic is None:
            sections_out.append(
                {
                    "module_id": "",
                    "topic_id": 0,
                    "topic_title": sid,
                    "pages": [],
                    "slide_keys": [],
                    "heading": draft.get("heading") or sid,
                    "text": draft.get("text") or "",
                    "ms_section_id": sid,
                }
            )
            continue
        sections_out.append(
            {
                "module_id": str(getattr(topic, "module_id", "") or ""),
                "topic_id": int(getattr(topic, "topic_id", 0) or 0),
                "topic_title": str(getattr(topic, "title", "") or ""),
                "pages": list(getattr(topic, "pages", None) or []),
                "slide_keys": list(getattr(topic, "slide_keys", None) or []),
                "heading": str(draft.get("heading") or getattr(topic, "title", "") or sid),
                "text": str(draft.get("text") or ""),
                "ms_section_id": sid,
            }
        )
    return {"sections": sections_out, "cta": cta}
