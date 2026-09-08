from __future__ import annotations

import json
from typing import Any

from backend.models import FounderQuote, LockedFact, Module
from backend.pipeline.validator import budget_range, count_script_words
from backend.schemas import AUDIENCE_CLUSTERS, CHANNELS, DURATIONS, INTENTS, TEMPERATURES
from backend.transcripts import format_founder_line


def _format_report_passages(passages: list[dict] | None) -> str:
    if not passages:
        return "(none)"
    blocks = []
    for passage in passages:
        title = passage.get("title") or "report"
        start = passage.get("start_page")
        end = passage.get("end_page")
        pages = f"p.{start}" if start == end or end is None else f"p.{start}-{end}"
        blocks.append(f"- {title} ({pages}): {passage.get('text', '').strip()}")
    return "\n".join(blocks)


def _axis_line(options, code: str) -> str:
    match = next((item for item in options if item.code == code), None)
    if not match:
        return code
    return f"{match.code} — {match.label}: {match.description}"


def _format_modules(modules: list[Module], sequence: list[str]) -> str:
    by_id = {module.id: module for module in modules}
    blocks = []
    for module_id in sequence:
        module = by_id.get(module_id)
        if not module:
            blocks.append(f"### {module_id}\n(missing module content)")
            continue
        blocks.append(
            f"### {module.id} {module.name}\n"
            f"Job: {module.job}\n"
            f"Core content: {module.core_content}\n"
            f"Flex points:\n{module.flex_points or '(none)'}"
        )
    return "\n\n".join(blocks)


def _format_facts(facts: list[LockedFact], sequence: list[str]) -> tuple[str, str]:
    locked = []
    forbidden = []
    sequence_set = set(sequence)
    for fact in facts:
        ids = {part.strip() for part in (fact.module_ids or "").split(",") if part.strip()}
        relevant = not ids or bool(ids & sequence_set)
        line = f"- {fact.fact}: {fact.value} (source: {fact.source})"
        if fact.status == "verified" and relevant:
            locked.append(line)
        elif fact.status in {"conflict", "do_not_use", "needs_source", "needs_decision"}:
            forbidden.append(line)
    return "\n".join(locked) or "(none)", "\n".join(forbidden) or "(none)"


def script_messages(
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
    founder_quotes: list[FounderQuote] | None = None,
    report_passages: list[dict] | None = None,
    corrections: list[str] | None = None,
    draft: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    locked, forbidden = _format_facts(facts, sequence)
    voice_lines = [format_founder_line(quote) for quote in (founder_quotes or [])]
    voice = "\n".join(voice_lines) or "(none)"
    reports = _format_report_passages(report_passages)
    low, high = budget_range(word_budget)
    system = (
        "You are a Masters' Union insider writing a pitch that a real person will SAY OUT LOUD "
        "to one listener. Not a brochure. Not a slide. A person talking. Return JSON only.\n\n"
        "HOW IT MUST SOUND (this matters more than anything else):\n"
        "- Write for the ear, not the page. Read every line back in your head; if a human would "
        "never say it in conversation, rewrite it.\n"
        "- Talk to 'you'. Use contractions (you're, we've, it's, don't). Mix short punchy lines with "
        "longer ones so it has a pulse. A one-line sentence is allowed and often stronger.\n"
        "- Open like a person actually opens — a real question, a sharp claim, or a concrete image. "
        "NEVER open with 'In today's...', 'In a world where...', 'Masters' Union is a...', or a definition.\n"
        "- Prove with specifics, not adjectives. Name the person, the number, the place, the company. "
        "Show it; don't label it. One concrete example beats three claims.\n"
        "- Let ideas build the way a story does: set up, then land the point. Rhetorical questions are good. "
        "A little dry confidence and humour is good. Sound convinced, not salesy.\n"
        "- BANNED words/phrases (they scream machine-written): 'in today's fast-paced/rapidly evolving', "
        "'leverage', 'delve', 'landscape', 'tapestry', 'ecosystem' (as filler), 'furthermore', 'moreover', "
        "'in conclusion', 'unlock', 'empower', 'seamless', 'robust', 'world-class', 'cutting-edge', "
        "'holistic', 'boasts', 'nestled', 'at the end of the day', 'it's worth noting', 'game-changer', "
        "'testament to'. Kill stacked three-adjective phrases and generic hype.\n"
        "- Inside a section's text: it is spoken prose. No headings, no bullet points, no numbered lists, "
        "no markdown. Just what the speaker says.\n\n"
        "WHOSE VOICE TO WRITE IN:\n"
        "- The FOUNDER VOICE block is Pratham Mittal actually speaking, transcribed from real talks. "
        "It is your primary style reference. Absorb his rhythm, plain word choice, and attitude, and write "
        "the ENTIRE script in that register — direct, specific, story-first, a bit irreverent, no corporate gloss. "
        "Match how he moves from a concrete thing to the point.\n"
        "- Borrow the VOICE, not the sentences. Only reproduce his words as a short, clearly-attributed quote "
        "to Pratham Mittal. Never present a number or claim from that block as fact — facts come from LOCKED "
        "and REPORT EVIDENCE only.\n\n"
        "HARD FACTUAL RULES:\n"
        "- Stay inside the hard word range given in the user message.\n"
        "- LOCKED facts must be used verbatim where relevant.\n"
        "- FORBIDDEN facts must never appear.\n"
        "- If you mention average CTC, the median CTC must appear in the same paragraph.\n"
        "- Never call EFMD, AACSB, BGA, BSIS or NSDC 'accreditations'. They are memberships or affiliations.\n"
        "- REPORT EVIDENCE is from official Masters' Union reports. Use at least one concrete detail from it "
        "in the relevant module (recruiter, role, programme, or named outcome) and attribute the report title "
        "naturally, the way a person would. If LOCKED and REPORT EVIDENCE conflict on a number, use LOCKED.\n"
        "- One ask only, at the end. Make it sound like a person asking, not a call-to-action button.\n"
        "- Follow the module sequence exactly. One section per module, in that order. The 'text' is the spoken "
        "words for that beat; 'heading' is a short internal label (2-4 words) only.\n"
        '- Output: {"sections":[{"module_id":"M01","heading":"...","text":"..."}], "cta":"..."}'
    )
    user = (
        f"You are pitching to this person:\n"
        f"Audience: {_axis_line(AUDIENCE_CLUSTERS, audience_cluster)}\n"
        f"Duration: {_axis_line(DURATIONS, duration)}\n"
        f"Channel: {_axis_line(CHANNELS, channel)}\n"
        f"Intent: {_axis_line(INTENTS, intent)}\n"
        f"Temperature: {_axis_line(TEMPERATURES, temperature)}\n"
        f"Context note: {context_note or '(none)'}\n"
        f"Word budget: {word_budget} words. Hard range: {low}-{high} words.\n"
        f"Module sequence: {' > '.join(sequence)}\n\n"
        f"FOUNDER VOICE — this is the exact tone, cadence, and vocabulary to write the whole pitch in "
        f"(Pratham Mittal, transcribed):\n{voice}\n\n"
        f"LOCKED — use verbatim where relevant:\n{locked}\n\n"
        f"FORBIDDEN — never state these:\n{forbidden}\n\n"
        f"REPORT EVIDENCE:\n{reports}\n\n"
        f"Modules (the beats to hit, in order — the content is raw material, rewrite it in the spoken voice):\n"
        f"{_format_modules(modules, sequence)}"
    )
    if draft:
        current = count_script_words(draft)
        user += (
            f"\n\nPrevious draft is {current} words. Rewrite THAT draft — do not start over. "
            f"Keep the same module order, the locked facts, and the spoken founder voice. "
            f"Cut or add whole sentences (never pad with filler or hype) until the total word count, "
            f"including the ask, is between {low} and {high}. It must still read like a person talking. "
            f"Return the full revised JSON.\n"
            f"{json.dumps(draft, ensure_ascii=False)}"
        )
    if corrections:
        user += "\n\nPrevious draft failed validation. Fix these issues:\n- " + "\n- ".join(corrections)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
