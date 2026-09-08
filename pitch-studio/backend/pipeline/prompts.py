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
        "You write Masters' Union pitch scripts from approved modules. "
        "Return JSON only.\n\n"
        "Rules:\n"
        "- Stay inside the hard word range given in the user message.\n"
        "- LOCKED facts must be used verbatim where relevant.\n"
        "- FORBIDDEN facts must never appear.\n"
        "- If you mention average CTC, the median CTC must appear in the same paragraph.\n"
        "- Never call EFMD, AACSB, BGA, BSIS or NSDC 'accreditations'. They are memberships or affiliations.\n"
        "- FOUNDER VOICE is for tone, vision and student narrative only. "
        "You MAY quote briefly with attribution to Pratham Mittal. "
        "NEVER introduce any number, statistic, or claim from that block as fact — "
        "all numbers must come from LOCKED facts or REPORT EVIDENCE.\n"
        "- REPORT EVIDENCE is extracted from official Masters' Union reports. "
        "Use at least one concrete detail from it in the relevant module "
        "(recruiter, role, programme color, or named outcome) and attribute the report title. "
        "If a number appears in both LOCKED and REPORT EVIDENCE and they conflict, use LOCKED.\n"
        "- One CTA only, at the end.\n"
        "- Follow the module sequence exactly. One section per module, in that order.\n"
        '- Output: {"sections":[{"module_id":"M01","heading":"...","text":"..."}], "cta":"..."}'
    )
    user = (
        f"Audience: {_axis_line(AUDIENCE_CLUSTERS, audience_cluster)}\n"
        f"Duration: {_axis_line(DURATIONS, duration)}\n"
        f"Channel: {_axis_line(CHANNELS, channel)}\n"
        f"Intent: {_axis_line(INTENTS, intent)}\n"
        f"Temperature: {_axis_line(TEMPERATURES, temperature)}\n"
        f"Context note: {context_note or '(none)'}\n"
        f"Word budget: {word_budget} words. Hard range: {low}-{high} words.\n"
        f"Module sequence: {' > '.join(sequence)}\n\n"
        f"LOCKED — use verbatim where relevant:\n{locked}\n\n"
        f"FORBIDDEN — never state these:\n{forbidden}\n\n"
        f"FOUNDER VOICE (Pratham Mittal):\n{voice}\n\n"
        f"REPORT EVIDENCE:\n{reports}\n\n"
        f"Modules:\n{_format_modules(modules, sequence)}"
    )
    if draft:
        current = count_script_words(draft)
        user += (
            f"\n\nPrevious draft is {current} words. Rewrite THAT draft — do not start over. "
            f"Keep the same module order and locked facts. Cut or add sentences until the "
            f"total word count, including the CTA, is between {low} and {high}. "
            f"Return the full revised JSON.\n"
            f"{json.dumps(draft, ensure_ascii=False)}"
        )
    if corrections:
        user += "\n\nPrevious draft failed validation. Fix these issues:\n- " + "\n- ".join(corrections)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def deck_messages(script: dict, duration: str, slide_count: int) -> list[dict[str, str]]:
    system = (
        "Convert a validated pitch script into a dense slide deck specification. "
        "Return JSON only. Never invent numbers that are not in the script.\n"
        f"Target about {slide_count} slides. Sparse decks are a failure.\n"
        "Each module MUST produce: one section divider, then at least two content slides "
        "(bullets and/or stat_pair). Do not stop at a title plus a divider.\n"
        "Bullet slides: 4-6 bullets. Each bullet is one full argument from the script "
        "(18-28 words), not a two-word label.\n"
        "Put every number, rupee figure, percentage, and named outcome on a stat_pair slide.\n"
        "If the script quotes someone, add a quote slide with attribution.\n"
        "Layouts: title, agenda, section, stat_pair, bullets, profile, quote, inventory, cta.\n"
        "Structure: slide 1 = title, slide 2 = agenda, then module blocks, end with cta.\n"
        "Keep slide module_id values in the same order as the script sections.\n"
        'Output: {"slides":[{"layout":"title","title":"...","subtitle":"...","module_id":"M01",'
        '"bullets":[],"stats":[{"label":"...","value":"..."}],"quote":"","attribution":""}]}'
    )
    user = f"Duration code: {duration}\nScript JSON:\n{script}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
