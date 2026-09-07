from __future__ import annotations

from backend.models import FounderQuote, LockedFact, Module
from backend.schemas import AUDIENCE_CLUSTERS, CHANNELS, DURATIONS, INTENTS, TEMPERATURES
from backend.transcripts import format_founder_line


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
    corrections: list[str] | None = None,
) -> list[dict[str, str]]:
    locked, forbidden = _format_facts(facts, sequence)
    voice_lines = [format_founder_line(quote) for quote in (founder_quotes or [])]
    voice = "\n".join(voice_lines) or "(none)"
    system = (
        "You write Masters' Union pitch scripts from approved modules. "
        "Return JSON only.\n\n"
        "Rules:\n"
        "- Stay within the word budget ±10%.\n"
        "- LOCKED facts must be used verbatim where relevant.\n"
        "- FORBIDDEN facts must never appear.\n"
        "- If you mention average CTC, the median CTC must appear in the same paragraph.\n"
        "- Never call EFMD, AACSB, BGA, BSIS or NSDC 'accreditations'. They are memberships or affiliations.\n"
        "- FOUNDER VOICE is for tone, vision and student narrative only. "
        "You MAY quote briefly with attribution to Pratham Mittal. "
        "NEVER introduce any number, statistic, or claim from that block as fact — "
        "all numbers must come from LOCKED facts.\n"
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
        f"Word budget: {word_budget}\n"
        f"Module sequence: {' > '.join(sequence)}\n\n"
        f"LOCKED — use verbatim where relevant:\n{locked}\n\n"
        f"FORBIDDEN — never state these:\n{forbidden}\n\n"
        f"FOUNDER VOICE (Pratham Mittal):\n{voice}\n\n"
        f"Modules:\n{_format_modules(modules, sequence)}"
    )
    if corrections:
        user += "\n\nPrevious draft failed validation. Fix these issues:\n- " + "\n- ".join(corrections)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def deck_messages(script: dict, duration: str, slide_count: int) -> list[dict[str, str]]:
    system = (
        "Convert a validated pitch script into a slide deck specification. "
        "Return JSON only. Never invent numbers that are not in the script.\n"
        f"Target {slide_count} slides. Max 25 words per slide except bullets "
        "(at most 6 bullets, 12 words each).\n"
        "Layouts: title, section, stat_pair, bullets, profile, quote, inventory, cta.\n"
        "Keep slide module_id values in the same order as the script sections.\n"
        'Output: {"slides":[{"layout":"title","title":"...","subtitle":"...","module_id":"M01",'
        '"bullets":[],"stats":[{"label":"...","value":"..."}],"quote":"","attribution":""}]}'
    )
    user = f"Duration code: {duration}\nScript JSON:\n{script}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
