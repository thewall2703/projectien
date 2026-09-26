from __future__ import annotations

from typing import Any

from backend.models import FounderQuote, LockedFact, Module
from backend.pipeline.llm import chat_json
from backend.pipeline.prompts import (
    _axis_line,
    _format_facts,
    _format_report_passages,
    _format_topic_flow,
    duration_framework,
)
from backend.pipeline.resolver import MAX_SCRIPT_MINUTES, script_minutes_for_duration
from backend.schemas import AUDIENCE_CLUSTERS, CHANNELS, DURATIONS, INTENTS, TEMPERATURES
from backend.transcripts import format_founder_line, normalize_whitespace

PLAN_SYSTEM = (
    "You plan a spoken Masters' Union pitch for an EMPLOYEE speaker (never Pratham). "
    "Build one throughline and one beat per deck topic, in topic-flow order. "
    "Each beat needs a point, a proof (from LOCKED, REPORT, FOUNDER excerpts, or NONE), "
    "a story device, a bridge_in, a pratham_move, and an approx word count. "
    "The talk will be SPOKEN, so plan for the ear: one clear idea per beat, said simply. "
    "bridge_in is a short note on the logical link from the previous beat, not a line to say; leave it "
    "empty when the link is obvious, and never plan announced transitions ('the next question is…'). "
    "Plan a named student or company as proof only if the material says what they did and what happened; "
    "otherwise use the fact without the name. Do not plan a caveat for every point. "
    "Reuse founder excerpt phrasing when useful; convert Pratham-personal material to third person. "
    "Never plan a FORBIDDEN fact as proof, even if an excerpt states it. If an excerpt and "
    "LOCKED/REPORT disagree on a fact, plan the LOCKED/REPORT value — unless a SELECTED SLIDE "
    "label for that beat states a conflicting figure, in which case plan the slide's figure "
    "(proof_source LOCKED is still fine when the slide and LOCKED agree). "
    "When a PRATHAM PLAYBOOK is given, build at least one beat's story_device on a playbook move "
    "(name the move in that beat's point or proof) where it fits the listener; never plan an "
    "unverified number from the playbook or passages as proof. "
    "For every beat that has a PRATHAM BY BEAT passage, plan how the beat borrows from it "
    "(his example, his question, his framing) in pratham_move; use \"\" if none fits. "
    "Return strict JSON only:\n"
    '{"throughline":"...","listener_start":"...","listener_end":"...","arc":"...",'
    '"beats":[{"topic_id":1,"role":"OPENING|BODY|CLOSE","point":"...","proof":"...",'
    '"proof_source":"LOCKED|REPORT|FOUNDER|NONE","story_device":"anecdote|question|contrast|example|none",'
    '"bridge_in":"...","pratham_move":"...","approx_words":80}],"ask":"..."}'
)

_PROOF_SOURCES = frozenset({"LOCKED", "REPORT", "FOUNDER", "NONE"})
_STORY_DEVICES = frozenset({"anecdote", "question", "contrast", "example", "none"})

def _clamp_str(value: Any, limit: int = 400) -> str:
    text = normalize_whitespace(str(value or ""))
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _topic_ids(topic_flow: list[Any]) -> list[int]:
    ids: list[int] = []
    for topic in topic_flow:
        payload = topic.to_prompt_dict() if hasattr(topic, "to_prompt_dict") else topic
        if not isinstance(payload, dict):
            continue
        try:
            topic_id = int(payload.get("topic_id") or 0)
        except (TypeError, ValueError):
            topic_id = 0
        if topic_id:
            ids.append(topic_id)
    return ids


def _role_for_index(index: int, total: int) -> str:
    if total <= 1:
        return "OPENING + CLOSE"
    if index == 0:
        return "OPENING"
    if index == total - 1:
        return "CLOSE"
    return "BODY"


def _placeholder_beat(topic_id: int, role: str, approx_words: int) -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "role": role,
        "point": "",
        "proof": "",
        "proof_source": "NONE",
        "story_device": "none",
        "bridge_in": "" if role in {"OPENING", "OPENING + CLOSE"} else "",
        "pratham_move": "",
        "approx_words": approx_words,
    }


def normalize_script_plan(
    raw: Any,
    *,
    topic_flow: list[Any],
    word_budget: int,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    topic_ids = _topic_ids(topic_flow)
    if not topic_ids:
        return None

    by_id: dict[int, dict[str, Any]] = {}
    for item in raw.get("beats") or []:
        if not isinstance(item, dict):
            continue
        try:
            topic_id = int(item.get("topic_id") or 0)
        except (TypeError, ValueError):
            continue
        if topic_id not in topic_ids or topic_id in by_id:
            continue
        proof_source = str(item.get("proof_source") or "NONE").strip().upper()
        if proof_source not in _PROOF_SOURCES:
            proof_source = "NONE"
        device = str(item.get("story_device") or "none").strip().lower()
        if device not in _STORY_DEVICES:
            device = "none"
        try:
            approx = int(item.get("approx_words") or 0)
        except (TypeError, ValueError):
            approx = 0
        by_id[topic_id] = {
            "topic_id": topic_id,
            "role": "",
            "point": _clamp_str(item.get("point"), 240),
            "proof": _clamp_str(item.get("proof"), 240),
            "proof_source": proof_source,
            "story_device": device,
            "bridge_in": _clamp_str(item.get("bridge_in"), 240),
            "pratham_move": _clamp_str(item.get("pratham_move"), 240),
            "approx_words": max(0, approx),
        }

    total = len(topic_ids)
    default_words = max(12, word_budget // total) if total else 12
    beats: list[dict[str, Any]] = []
    for index, topic_id in enumerate(topic_ids):
        role = _role_for_index(index, total)
        beat = by_id.get(topic_id) or _placeholder_beat(topic_id, role, default_words)
        beat["role"] = role
        if role in {"OPENING", "OPENING + CLOSE"}:
            beat["bridge_in"] = ""
        beats.append(beat)

    raw_sum = sum(max(0, int(beat.get("approx_words") or 0)) for beat in beats)
    budget = max(12 * total, int(word_budget or 0))
    if raw_sum <= 0:
        base, rem = divmod(budget, total)
        for index, beat in enumerate(beats):
            beat["approx_words"] = max(12, base + (1 if index < rem else 0))
    else:
        scaled = [max(12, int(round(budget * (beat["approx_words"] / raw_sum)))) for beat in beats]
        drift = budget - sum(scaled)
        cursor = 0
        while drift != 0 and beats:
            step = 1 if drift > 0 else -1
            idx = cursor % len(scaled)
            if step < 0 and scaled[idx] <= 12:
                cursor += 1
                if cursor > len(scaled) * 3:
                    break
                continue
            scaled[idx] += step
            drift -= step
            cursor += 1
        for beat, words in zip(beats, scaled):
            beat["approx_words"] = words

    return {
        "throughline": _clamp_str(raw.get("throughline"), 400),
        "listener_start": _clamp_str(raw.get("listener_start"), 240),
        "listener_end": _clamp_str(raw.get("listener_end"), 240),
        "arc": _clamp_str(raw.get("arc"), 400),
        "beats": beats,
        "ask": _clamp_str(raw.get("ask"), 240),
    }


def plan_script(
    *,
    audience_cluster: str,
    duration: str,
    channel: str,
    intent: str,
    temperature: str,
    context_note: str,
    modules: list[Module],
    sequence: list[str],
    facts: list[LockedFact],
    word_budget: int,
    founder_quotes: list[FounderQuote],
    report_passages: list[dict[str, Any]],
    topic_flow: list[Any],
    style_guide: str = "",
    listener_profile: str = "",
    pratham_reference: str = "",
) -> dict[str, Any] | None:
    locked, forbidden = _format_facts(facts, sequence)
    voice = "\n".join(format_founder_line(quote) for quote in founder_quotes) or "(none)"
    reports = _format_report_passages(report_passages)
    user = (
        f"Audience: {_axis_line(AUDIENCE_CLUSTERS, audience_cluster)}\n"
        f"Duration: {_axis_line(DURATIONS, duration)}\n"
        f"Channel: {_axis_line(CHANNELS, channel)}\n"
        f"Intent: {_axis_line(INTENTS, intent)}\n"
        f"Temperature: {_axis_line(TEMPERATURES, temperature)}\n"
        f"Context note: {context_note or '(none)'}\n"
        f"Duration strategy:\n{duration_framework(duration)}\n"
        f"Spoken script length: {script_minutes_for_duration(duration):g} minutes "
        f"(cap {MAX_SCRIPT_MINUTES}). Word budget: {word_budget}.\n\n"
        f"DECK TOPIC FLOW:\n{_format_topic_flow(topic_flow, modules, sequence)}\n\n"
        f"LISTENER PERSONA PROFILE (attitudes only):\n{listener_profile or '(none)'}\n\n"
        + (
            f"STYLE GUIDE:\n{style_guide}\n\n"
            if style_guide
            else ""
        )
        + f"FOUNDER VOICE EXCERPTS:\n{voice}\n\n"
        + (f"{pratham_reference}\n\n" if pratham_reference else "")
        + f"LOCKED:\n{locked}\n\n"
        f"FORBIDDEN:\n{forbidden}\n\n"
        f"REPORT EVIDENCE:\n{reports}\n\n"
        "Plan one beat per topic_id above, in that order. approx_words must sum near the word budget."
    )
    raw = chat_json(
        [{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": user}],
        role="script_planner",
    )
    return normalize_script_plan(raw, topic_flow=topic_flow, word_budget=word_budget)
