from __future__ import annotations

import json
from typing import Any

from backend.pipeline.llm import chat_json
from backend.transcripts import normalize_whitespace

FLOW_SYSTEM = (
    "You are an editor judging spoken-pitch STRUCTURE only — not facts, not voice identity. "
    "Quote evidence before you score. Rubric:\n"
    "- join: does B pick up from A so a listener feels one conversation (not a new paragraph "
    "starting cold)? Score 1-5.\n"
    "- story_shape: setup → turn → point vs a list of claims. Score 1-5.\n"
    "- arc: does the talk build toward the ask, following the plan's throughline? Score 1-5.\n"
    "- naturalness: would a person say this out loud in this order? Score 1-5.\n"
    "- spoken: is this SPOKEN English or WRITTEN English read aloud? Score 1-5. 5 = sounds like a person "
    "talking across a table; 3 = a well-written essay being read out; 1 = slide copy. Written English "
    "includes long clause-stacked sentences, noun phrases instead of verbs, 'X: Y' label lines, "
    "formal connectives, and phrasing nobody says in conversation.\n"
    "- written_lines: up to 8 of the most written-sounding sentences, each with how a person would "
    "actually say it (spoken_fix).\n"
    "- cold_starts: up to 6 runs of 3+ consecutive sentences that do not connect to each other "
    "(each sentence starts cold). For each, quote the run and give a chained rewrite where most "
    "sentences pick up the one before.\n"
    "- signposting: sentences that announce the talk's own structure ('the next question is', "
    "'now let's look at', 'once that is clear', 'with X in place'). A good join does not announce itself.\n"
    "- hedging: reflexive caveats that undercut a point ('a pitch isn't a company, but…', "
    "'X alone doesn't prove…'). Only flag caveats that are not needed for accuracy.\n"
    "- name_drops: students, alumni or student ventures offered as an example with no story (who they "
    "are, what they did, what happened). Do not flag recruiter lists, membership bodies, board members, "
    "faculty names, or Pratham Mittal.\n"
    "- repetition: the same point or phrase made twice.\n"
    "- grammar: broken or garbled sentences (quote + fix).\n"
    "Return strict JSON:\n"
    '{"joins":[{"from_topic":1,"to_topic":2,"score":1,"quote":"","issue":"","suggested_bridge":""}],'
    '"sections":[{"topic_id":1,"story_shape":1,"quote":"","issue":""}],'
    '"arc":{"score":1,"issue":""},"naturalness":{"score":1,"issue":""},'
    '"spoken":{"score":1,"issue":""},'
    '"written_lines":[{"topic_id":1,"quote":"","spoken_fix":""}],'
    '"cold_starts":[{"topic_id":1,"quote":"","chained":""}],'
    '"signposting":["..."],"hedging":["..."],"name_drops":["..."],'
    '"repetition":["..."],"grammar":[{"topic_id":1,"quote":"","fix":""}]}'
)


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_str(value: Any, limit: int = 300) -> str:
    text = normalize_whitespace(str(value or ""))
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _format_script(script: dict[str, Any]) -> str:
    parts: list[str] = []
    for index, section in enumerate(script.get("sections") or []):
        if not isinstance(section, dict):
            continue
        topic_id = section.get("topic_id") or 0
        heading = section.get("heading") or section.get("topic_title") or f"Beat {index + 1}"
        text = normalize_whitespace(str(section.get("text") or ""))
        parts.append(f"[topic_id={topic_id} | {heading}]\n{text}")
    cta = normalize_whitespace(str(script.get("cta") or ""))
    if cta:
        parts.append(f"[ASK]\n{cta}")
    return "\n\n".join(parts)


def normalize_flow_result(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    joins: list[dict[str, Any]] = []
    for item in raw.get("joins") or []:
        if not isinstance(item, dict):
            continue
        joins.append(
            {
                "from_topic": int(item.get("from_topic") or 0),
                "to_topic": int(item.get("to_topic") or 0),
                "score": _clamp_int(item.get("score"), 1, 5, 3),
                "quote": _clamp_str(item.get("quote")),
                "issue": _clamp_str(item.get("issue")),
                "suggested_bridge": _clamp_str(item.get("suggested_bridge")),
            }
        )
    sections: list[dict[str, Any]] = []
    for item in raw.get("sections") or []:
        if not isinstance(item, dict):
            continue
        sections.append(
            {
                "topic_id": int(item.get("topic_id") or 0),
                "story_shape": _clamp_int(item.get("story_shape"), 1, 5, 3),
                "quote": _clamp_str(item.get("quote")),
                "issue": _clamp_str(item.get("issue")),
            }
        )
    arc_raw = raw.get("arc") if isinstance(raw.get("arc"), dict) else {}
    natural_raw = raw.get("naturalness") if isinstance(raw.get("naturalness"), dict) else {}
    spoken_raw = raw.get("spoken") if isinstance(raw.get("spoken"), dict) else {}

    def _quotes(key: str) -> list[str]:
        return [text for text in (_clamp_str(item) for item in raw.get(key) or []) if text]

    repetition = _quotes("repetition")
    written_lines: list[dict[str, Any]] = []
    for item in raw.get("written_lines") or []:
        if not isinstance(item, dict):
            continue
        quote = _clamp_str(item.get("quote"))
        if quote:
            written_lines.append(
                {
                    "topic_id": int(item.get("topic_id") or 0),
                    "quote": quote,
                    "spoken_fix": _clamp_str(item.get("spoken_fix")),
                }
            )
    cold_starts: list[dict[str, Any]] = []
    for item in raw.get("cold_starts") or []:
        if not isinstance(item, dict):
            continue
        quote = _clamp_str(item.get("quote"))
        if not quote:
            continue
        cold_starts.append(
            {
                "topic_id": int(item.get("topic_id") or 0),
                "quote": quote,
                "chained": _clamp_str(item.get("chained")),
            }
        )
        if len(cold_starts) >= 6:
            break
    grammar: list[dict[str, Any]] = []
    for item in raw.get("grammar") or []:
        if not isinstance(item, dict):
            continue
        quote = _clamp_str(item.get("quote"))
        fix = _clamp_str(item.get("fix"))
        if not quote and not fix:
            continue
        grammar.append(
            {
                "topic_id": int(item.get("topic_id") or 0),
                "quote": quote,
                "fix": fix,
            }
        )
    return {
        "joins": joins,
        "sections": sections,
        "arc": {
            "score": _clamp_int(arc_raw.get("score"), 1, 5, 3),
            "issue": _clamp_str(arc_raw.get("issue")),
        },
        "naturalness": {
            "score": _clamp_int(natural_raw.get("score"), 1, 5, 3),
            "issue": _clamp_str(natural_raw.get("issue")),
        },
        "spoken": {
            "score": _clamp_int(spoken_raw.get("score"), 1, 5, 3),
            "issue": _clamp_str(spoken_raw.get("issue")),
        },
        "written_lines": written_lines,
        "cold_starts": cold_starts,
        "signposting": _quotes("signposting"),
        "hedging": _quotes("hedging"),
        "name_drops": _quotes("name_drops"),
        "repetition": repetition,
        "grammar": grammar,
    }


def flow_passed(result: dict[str, Any]) -> bool:
    joins = result.get("joins") or []
    if joins:
        scores = [int(item.get("score") or 0) for item in joins if isinstance(item, dict)]
        if any(score < 3 for score in scores):
            return False
        if scores and (sum(scores) / len(scores)) < 3.5:
            return False
    arc = int((result.get("arc") or {}).get("score") or 0)
    natural = int((result.get("naturalness") or {}).get("score") or 0)
    if arc < 3 or natural < 3:
        return False
    if int((result.get("spoken") or {}).get("score") or 0) < 4:
        return False
    if result.get("grammar"):
        return False
    return True


def flow_score(result: dict[str, Any]) -> float:
    parts: list[float] = []
    joins = [int(item.get("score") or 0) for item in (result.get("joins") or []) if isinstance(item, dict)]
    if joins:
        parts.append(sum(joins) / len(joins) / 5.0)
    sections = [
        int(item.get("story_shape") or 0)
        for item in (result.get("sections") or [])
        if isinstance(item, dict)
    ]
    if sections:
        parts.append(sum(sections) / len(sections) / 5.0)
    arc = int((result.get("arc") or {}).get("score") or 0)
    natural = int((result.get("naturalness") or {}).get("score") or 0)
    parts.append(arc / 5.0)
    parts.append(natural / 5.0)
    # Spoken English counts double: it is the main thing a listener notices.
    spoken = int((result.get("spoken") or {}).get("score") or 0)
    parts.extend([spoken / 5.0, spoken / 5.0])
    if result.get("grammar"):
        parts.append(0.2)
    else:
        parts.append(1.0)
    if not parts:
        return 0.0
    return max(0.0, min(1.0, sum(parts) / len(parts)))


def flow_notes(result: dict[str, Any], limit: int = 8) -> list[str]:
    scored: list[tuple[int, str]] = []
    for item in result.get("joins") or []:
        if not isinstance(item, dict):
            continue
        score = int(item.get("score") or 5)
        if score >= 4:
            continue
        quote = item.get("quote") or ""
        bridge = item.get("suggested_bridge") or ""
        detail = f" ({quote})" if quote else ""
        bridge_bit = f" Bridge it, e.g.: {bridge}" if bridge else ""
        scored.append(
            (
                score,
                f"Transition from section {item.get('from_topic')} to {item.get('to_topic')} "
                f"is abrupt{detail}.{bridge_bit}".rstrip(),
            )
        )
    for item in result.get("sections") or []:
        if not isinstance(item, dict):
            continue
        shape = int(item.get("story_shape") or 5)
        if shape >= 4:
            continue
        scored.append(
            (
                shape,
                f"Section {item.get('topic_id')} reads as a list of claims. "
                "Give it a setup, a turn and a point.",
            )
        )
    arc = result.get("arc") or {}
    if int(arc.get("score") or 5) < 4 and arc.get("issue"):
        scored.append((int(arc.get("score") or 3), f"Arc: {arc.get('issue')}"))
    natural = result.get("naturalness") or {}
    if int(natural.get("score") or 5) < 4 and natural.get("issue"):
        scored.append(
            (int(natural.get("score") or 3), f"Naturalness: {natural.get('issue')}")
        )
    # One combined note that sorts first: separate repetition notes were crowded out
    # of the capped list by grammar notes, so repeats never reached the writer.
    repeats = [
        text
        for text in (normalize_whitespace(str(phrase or "")) for phrase in result.get("repetition") or [])
        if text
    ]
    if repeats:
        scored.append(
            (
                0,
                "Repetition — each of these is said more than once. Say it once, in the section "
                "where it lands best, and cut or replace the repeat: "
                + "; ".join(f"({index}) {text}" for index, text in enumerate(repeats[:10], 1)),
            )
        )
    spoken = result.get("spoken") or {}
    if int(spoken.get("score") or 5) < 4:
        scored.append(
            (
                0,
                "This reads as written English read aloud, not speech"
                + (f" ({spoken.get('issue')})" if spoken.get("issue") else "")
                + ". Rewrite every section the way a person talks: short sentences, verbs up front, "
                "no labels or colons, no announced transitions.",
            )
        )
    for key, lead in (
        ("signposting", "Announced transitions — cut these and start the section on its point"),
        ("hedging", "Reflexive caveats — drop these unless a locked fact requires them"),
        ("name_drops", "Names with no story — tell who they are and what happened, or drop the name"),
    ):
        quotes = [normalize_whitespace(str(q or "")) for q in result.get(key) or []]
        quotes = [q for q in quotes if q]
        if quotes:
            scored.append((0, f"{lead}: " + "; ".join(f"«{q}»" for q in quotes[:8])))
    for item in result.get("written_lines") or []:
        if not isinstance(item, dict) or not item.get("quote"):
            continue
        fix = item.get("spoken_fix") or ""
        scored.append(
            (
                1,
                f"Section {item.get('topic_id')} sounds written: '{item['quote']}'"
                + (f" → say it like: '{fix}'" if fix else ""),
            )
        )
    for item in result.get("cold_starts") or []:
        if not isinstance(item, dict) or not item.get("quote"):
            continue
        chained = item.get("chained") or ""
        scored.append(
            (
                1,
                f"Section {item.get('topic_id')} has cold sentence starts: '{item['quote']}'"
                + (f" → chain it like: '{chained}'" if chained else ""),
            )
        )
    for item in result.get("grammar") or []:
        if not isinstance(item, dict):
            continue
        quote = item.get("quote") or ""
        fix = item.get("fix") or ""
        if quote and fix:
            scored.append(
                (1, f"Section {item.get('topic_id')} grammar: '{quote}' → '{fix}'")
            )
        elif quote:
            scored.append((1, f"Section {item.get('topic_id')} grammar: '{quote}'"))
    scored.sort(key=lambda pair: pair[0])
    notes: list[str] = []
    for _score, note in scored:
        if note and note not in notes:
            notes.append(note)
        if len(notes) >= limit:
            break
    return notes


def check_flow(
    script: dict[str, Any],
    *,
    plan: dict[str, Any] | None,
    duration: str,
    audience_cluster: str,
    temperature: str,
    topic_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    plan_block = json.dumps(plan or {}, ensure_ascii=False)
    roles_block = json.dumps(topic_roles or [], ensure_ascii=False)
    user = (
        f"DURATION: {duration}\n"
        f"AUDIENCE: {audience_cluster}\n"
        f"TEMPERATURE: {temperature}\n"
        f"TOPIC ROLES: {roles_block}\n"
        f"STORY PLAN: {plan_block}\n\n"
        f"SPOKEN SCRIPT:\n{_format_script(script)}"
    )
    raw = chat_json(
        [{"role": "system", "content": FLOW_SYSTEM}, {"role": "user", "content": user}],
        role="flow_judge",
    )
    return normalize_flow_result(raw)
