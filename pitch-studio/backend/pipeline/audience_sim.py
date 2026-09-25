from __future__ import annotations

import json
from typing import Any

from backend.models import ListenerTurn
from backend.pipeline.llm import chat_json
from backend.transcripts import normalize_whitespace

SIM_SYSTEM = (
    "You are a real audience member from the given persona, hearing a Masters' Union pitch "
    "ONCE, in order, at speaking pace. You are NOT raising sales objections and you are NOT "
    "answering the ask strategically. Judge only whether you can FOLLOW the talk, whether the "
    "grammar holds, and whether it sounds like a human speaking out loud.\n"
    "For each section report clarity 1-5, lost_at (quote where you lost the thread, or ''), "
    "grammar_issue (quote of a garbled/broken line, or ''), robotic_line (quote no human would "
    "say out loud, or ''). Overall: followability, sounds_human, grammar (each 1-5), and "
    "takeaway in your own words.\n"
    "Return strict JSON:\n"
    '{"sections":[{"topic_id":0,"clarity":1,"lost_at":"","grammar_issue":"","robotic_line":""}],'
    '"followability":1,"sounds_human":1,"grammar":1,"takeaway":"..."}'
)

# Familiarity only: it sets what the listener already knows (so jargon and skipped steps
# show up as confusion), not how hard they push back on claims.
_LISTENER_STANCE = {
    "X1": "You have never heard of Masters' Union and know none of its names, terms or programmes.",
    "X2": "You have heard of Masters' Union but know little beyond the name.",
    "X3": "You know Masters' Union and a few alternatives; you are comparing options.",
    "X4": "You know Masters' Union well and are close to deciding.",
    "X5": "You have already joined; you know the basics and listen for whether you could retell it.",
}


def _listener_familiarity(temperature: str) -> str:
    token = (temperature or "").strip().upper()
    return _LISTENER_STANCE.get(token, _LISTENER_STANCE["X2"])


def _format_script_for_listener(script: dict[str, Any]) -> str:
    parts: list[str] = []
    for index, section in enumerate(script.get("sections") or []):
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or section.get("topic_title") or f"Beat {index + 1}")
        topic_id = section.get("topic_id") or 0
        module_id = section.get("module_id") or ""
        text = normalize_whitespace(str(section.get("text") or ""))
        parts.append(
            f"[section {index} | topic_id={topic_id} | module_id={module_id} | {heading}]\n{text}"
        )
    cta = normalize_whitespace(str(script.get("cta") or ""))
    if cta:
        parts.append(f"[CTA]\n{cta}")
    return "\n\n".join(parts)


def _format_examples(examples: list[ListenerTurn]) -> str:
    if not examples:
        return "(none)"
    lines: list[str] = []
    for item in examples:
        lines.append(
            f"- how they talk: q={item.question_verbatim} | "
            f"reaction={item.reaction_after_answer or '(none)'} | outcome={item.outcome}"
        )
    return "\n".join(lines)


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


def normalize_listener_result(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    sections: list[dict[str, Any]] = []
    for item in raw.get("sections") or []:
        if not isinstance(item, dict):
            continue
        sections.append(
            {
                "topic_id": int(item.get("topic_id") or 0),
                "clarity": _clamp_int(item.get("clarity"), 1, 5, 3),
                "lost_at": _clamp_str(item.get("lost_at")),
                "grammar_issue": _clamp_str(item.get("grammar_issue")),
                "robotic_line": _clamp_str(item.get("robotic_line")),
            }
        )
    return {
        "sections": sections,
        "followability": _clamp_int(raw.get("followability"), 1, 5, 3),
        "sounds_human": _clamp_int(raw.get("sounds_human"), 1, 5, 3),
        "grammar": _clamp_int(raw.get("grammar"), 1, 5, 3),
        "takeaway": _clamp_str(raw.get("takeaway"), 400),
    }


def listener_passed(result: dict[str, Any]) -> bool:
    if int(result.get("followability") or 0) < 4:
        return False
    if int(result.get("sounds_human") or 0) < 4:
        return False
    if int(result.get("grammar") or 0) < 4:
        return False
    for section in result.get("sections") or []:
        if not isinstance(section, dict):
            continue
        if int(section.get("clarity") or 5) <= 2:
            return False
    return True


def listener_score(result: dict[str, Any]) -> float:
    parts = [
        int(result.get("followability") or 0) / 5.0,
        int(result.get("sounds_human") or 0) / 5.0,
        int(result.get("grammar") or 0) / 5.0,
    ]
    clarities = [
        int(section.get("clarity") or 0)
        for section in (result.get("sections") or [])
        if isinstance(section, dict)
    ]
    if clarities:
        parts.append(sum(clarities) / len(clarities) / 5.0)
    return max(0.0, min(1.0, sum(parts) / len(parts)))


def listener_notes(result: dict[str, Any], limit: int = 8) -> list[str]:
    notes: list[str] = []
    for section in result.get("sections") or []:
        if not isinstance(section, dict):
            continue
        topic = section.get("topic_id") or "a section"
        lost = section.get("lost_at") or ""
        if lost or int(section.get("clarity") or 5) <= 2:
            detail = f" at '{lost}'" if lost else ""
            notes.append(
                f"Listener lost the thread in section {topic}{detail}. "
                "Make the step from the previous idea explicit."
            )
        robotic = section.get("robotic_line") or ""
        if robotic:
            notes.append(
                f"Section {topic} sounds machine-written: '{robotic}'. "
                "Rewrite it the way a person would say it."
            )
        grammar_issue = section.get("grammar_issue") or ""
        if grammar_issue:
            notes.append(f"Section {topic} grammar: '{grammar_issue}'. Fix the sentence.")
        if len(notes) >= limit:
            return notes[:limit]
    if int(result.get("followability") or 5) < 4:
        notes.append("Overall followability is weak. Tighten the throughline between sections.")
    if int(result.get("sounds_human") or 5) < 4:
        notes.append("Overall it does not sound human enough. Prefer spoken rhythm over brochure tone.")
    if int(result.get("grammar") or 5) < 4:
        notes.append("Overall grammar feels broken. Fix garbled sentences before adding new points.")
    return notes[:limit]


def simulate_audience(
    script: dict[str, Any],
    *,
    persona_label: str,
    profile_text: str,
    listener_examples: list[ListenerTurn],
    audience_cluster: str,
    duration: str,
    channel: str,
    intent: str,
    temperature: str,
    context_note: str,
) -> dict[str, Any]:
    grounded = bool((profile_text or "").strip())
    user = (
        f"PERSONA LABEL: {persona_label or '(unspecified)'}\n"
        f"AUDIENCE CLUSTER: {audience_cluster}\n"
        f"DURATION: {duration} | CHANNEL: {channel} | INTENT: {intent}\n"
        f"WHAT YOU ALREADY KNOW: {_listener_familiarity(temperature)}\n"
        f"CONTEXT NOTE: {context_note or '(none)'}\n\n"
        f"WHO YOU ARE (attitudes only — not MU facts, not questions to ask):\n"
        f"{profile_text or '(none — ground yourself on the persona label and axes)'}\n\n"
        f"HOW REAL LISTENERS LIKE YOU TALK (examples, not a Q&A agenda):\n"
        f"{_format_examples(listener_examples)}\n\n"
        f"SPOKEN SCRIPT (hear once, in order):\n{_format_script_for_listener(script)}"
    )
    raw = chat_json(
        [
            {"role": "system", "content": SIM_SYSTEM},
            {"role": "user", "content": user},
        ],
        role="listener",
    )
    result = normalize_listener_result(raw)
    result["grounded"] = grounded
    return result


def serialize_quality_trace(
    rounds: list[dict[str, Any]],
    *,
    kept_round: int = 0,
    plan_error: str = "",
) -> str:
    return json.dumps(
        {"plan_error": plan_error or "", "rounds": rounds, "kept_round": kept_round},
        ensure_ascii=False,
    )
