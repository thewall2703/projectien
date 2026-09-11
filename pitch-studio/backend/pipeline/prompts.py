from __future__ import annotations

import json
from typing import Any

from backend.models import FounderQuote, LockedFact, Module
from backend.pipeline.validator import budget_range, count_script_words
from backend.schemas import AUDIENCE_CLUSTERS, CHANNELS, DURATIONS, INTENTS, TEMPERATURES
from backend.transcripts import format_founder_line

UNIVERSITY_STATUS_LINE = (
    "Masters' Union is becoming a university, following approval from the Government of Haryana."
)


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


_DURATION_FRAMEWORK = {
    "T0": (
        "30–60 seconds: Be precise, specific, and factual. Make one clear point, "
        "support it with one memorable proof, and ask for one next step. Do not attempt a full story."
    ),
    "T1": (
        "2 minutes: Establish the listener's need or tension, then position Masters' Union as the "
        "specific solution. Use only the strongest proof needed to make that connection credible."
    ),
    "T2": (
        "5 minutes: Build a story. Infer the audience's likely emotional state from the audience, "
        "intent, temperature, channel, and context; meet them there without naming or diagnosing it. "
        "Choose the information, example, or approved asset they will be most receptive to, and use "
        "it to create genuine desire for Masters' Union."
    ),
    "T3": (
        "10 minutes: Use the same audience-aware story arc as the 5-minute pitch, with room for richer "
        "storytelling. Address the most relevant FAQs or objection, and use concrete anecdotes or "
        "student case studies. Depth must come from specificity, not repetition."
    ),
    "T4": (
        "30 minutes: Extend the 10-minute audience-aware story into a deck-led conversation. Let the "
        "approved assets carry evidence and structure; connect them with anecdotes, relevant FAQs, "
        "and pauses for dialogue. Do not turn the script into a continuous lecture."
    ),
    "T5": (
        "90 minutes: Treat this as a guided campus experience, not a 90-minute monologue. Read the "
        "audience's emotional state, use each physical stop or approved asset as proof, tell relevant "
        "student stories, and answer FAQs as they naturally arise."
    ),
}


def duration_framework(duration: str) -> str:
    return _DURATION_FRAMEWORK.get(duration, _DURATION_FRAMEWORK["T2"])


def _format_topic_flow(topic_flow: list[Any], modules: list[Module], sequence: list[str]) -> str:
    by_id = {module.id: module for module in modules}
    blocks = []
    for index, topic in enumerate(topic_flow, start=1):
        if len(topic_flow) == 1:
            role = "OPENING + CLOSE"
        else:
            role = "OPENING" if index == 1 else ("CLOSE" if index == len(topic_flow) else "BODY")
        payload = topic.to_prompt_dict() if hasattr(topic, "to_prompt_dict") else topic
        labels = ", ".join(str(label) for label in (payload.get("labels") or []) if label)
        recipe_ids = payload.get("recipe_modules") or []
        module_blocks = []
        for module_id in recipe_ids:
            module = by_id.get(module_id)
            if not module:
                continue
            module_blocks.append(
                f"Recipe {module.id} {module.name}: {module.job}\n"
                f"Core content: {module.core_content}\n"
                f"Flex points: {module.flex_points or '(none)'}"
            )
        blocks.append(
            f"### Beat {index}: {payload.get('title')} ({payload.get('page_range')})\n"
            f"Role: {role}\n"
            f"topic_id: {payload.get('topic_id')}\n"
            f"Selected slides: {labels or '(unlabeled)'}\n"
            f"Topic summary: {payload.get('summary') or '(none)'}\n"
            f"Topic vision: {payload.get('vision') or '(none)'}\n"
            f"{chr(10).join(module_blocks) or 'No overlapping recipe module — stay with the slides.'}"
        )
    used: set[str] = set()
    for topic in topic_flow:
        payload = topic.to_prompt_dict() if hasattr(topic, "to_prompt_dict") else topic
        used.update(str(item) for item in (payload.get("recipe_modules") or []))
    leftovers = [module_id for module_id in sequence if module_id not in used]
    if leftovers:
        blocks.append(
            "Recipe material not tied to a selected topic. Weave it into the most relevant existing "
            f"beat only: {', '.join(leftovers)}"
        )
    return "\n\n".join(blocks)


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
        value = UNIVERSITY_STATUS_LINE if fact.fact.strip().lower() == "university status" else fact.value
        line = f"- {fact.fact}: {value} (source: {fact.source})"
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
    topic_flow: list[Any] | None = None,
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
        "IMPACT MUST BE PROPORTIONAL TO TIME:\n"
        "- 30–60 seconds: precise, specific, factual; one point and one proof.\n"
        "- 2 minutes: highlight the listener's need and position Masters' Union as the solution.\n"
        "- 5 minutes: build a story; read the audience's likely emotional state, match it to the information "
        "or approved asset they will receive best, and create desire for Masters' Union.\n"
        "- 10 minutes: use the same audience-aware story with richer storytelling, relevant FAQs, anecdotes, "
        "and student case studies.\n"
        "- Longer sessions extend the 10-minute approach through dialogue, assets, and physical experience — "
        "never through padding or repetition.\n\n"
        "HARD FACTUAL RULES:\n"
        "- Stay inside the hard word range given in the user message.\n"
        "- LOCKED facts must be used verbatim where relevant.\n"
        "- FORBIDDEN facts must never appear.\n"
        "- If you mention average CTC, the median CTC must appear in the same paragraph.\n"
        "- Never call EFMD, AACSB, BGA, BSIS or NSDC 'accreditations'. They are memberships or affiliations.\n"
        "- REPORT EVIDENCE is from official Masters' Union reports. Use at least one concrete detail from it "
        "in the relevant module (recruiter, role, programme, or named outcome) and attribute the report title "
        "naturally, the way a person would. If LOCKED and REPORT EVIDENCE conflict on a number, use LOCKED.\n"
        "- One ask only, at the end. Make it sound like a person asking, not a call-to-action button.\n\n"
        "BEAT ROLES:\n"
        "- OPENING: This is the first thing the listener hears. Start independently with a direct question, "
        "sharp claim, or concrete image. Never begin with a continuation such as 'Then', 'Next', "
        "'Moving on', 'As I mentioned', or anything that assumes earlier speech.\n"
        "- BODY: Continue the same conversation. Connect naturally to the previous beat, explain only what "
        "the selected slides support, and land one clear point before moving forward.\n"
        "- CLOSE: Add no new argument or evidence. Briefly land the core point, make exactly one specific "
        "ask, and stop. Do not end with a slogan, summary list, or multiple options.\n"
        + (
            "- Follow the DECK TOPIC FLOW exactly. One section per topic, in that order. Never invent, "
            "merge, or reorder topics. Extra recipe information may be woven into the most relevant "
            "existing topic only. The 'text' is the spoken words for that beat; 'heading' is a short "
            "internal label (2-4 words) only.\n"
            '- Output: {"sections":[{"topic_id":1,"topic_title":"...","pages":[1,2],"module_id":"M01",'
            '"heading":"...","text":"..."}], "cta":"..."}'
            if topic_flow
            else
            "- Follow the module sequence exactly. One section per module, in that order. The 'text' is the spoken "
            "words for that beat; 'heading' is a short internal label (2-4 words) only.\n"
            '- Output: {"sections":[{"module_id":"M01","heading":"...","text":"..."}], "cta":"..."}'
        )
    )
    flow_block = (
        f"DECK TOPIC FLOW — this is the governing spoken order. Stay inside it:\n"
        f"{_format_topic_flow(topic_flow, modules, sequence)}\n\n"
        f"Recipe module sequence for supporting material only: {' > '.join(sequence)}\n"
        if topic_flow
        else
        f"Module sequence: {' > '.join(sequence)}\n\n"
        f"Modules (the beats to hit, in order — the content is raw material, rewrite it in the spoken voice):\n"
        f"{_format_modules(modules, sequence)}"
    )
    user = (
        f"You are pitching to this person:\n"
        f"Audience: {_axis_line(AUDIENCE_CLUSTERS, audience_cluster)}\n"
        f"Duration: {_axis_line(DURATIONS, duration)}\n"
        f"Channel: {_axis_line(CHANNELS, channel)}\n"
        f"Intent: {_axis_line(INTENTS, intent)}\n"
        f"Temperature: {_axis_line(TEMPERATURES, temperature)}\n"
        f"Context note: {context_note or '(none)'}\n"
        f"Duration strategy — follow this as the governing narrative brief:\n{duration_framework(duration)}\n"
        f"Word limit: {word_budget} words at 120 spoken words per minute. "
        f"Write {low}-{high} words and never exceed {high}.\n"
        f"{flow_block}\n\n"
        f"FOUNDER VOICE — this is the exact tone, cadence, and vocabulary to write the whole pitch in "
        f"(Pratham Mittal, transcribed). Write like him talking, not like a brochure:\n{voice}\n\n"
        f"LOCKED — use verbatim where relevant:\n{locked}\n\n"
        f"FORBIDDEN — never state these:\n{forbidden}\n\n"
        f"REPORT EVIDENCE:\n{reports}"
    )
    if draft:
        current = count_script_words(draft)
        order_rule = (
            "Keep the same topic_id, pages, topic_title, beat roles, locked facts, and spoken founder voice. "
            if topic_flow
            else "Keep the same module order, the locked facts, and the spoken founder voice. "
        )
        user += (
            f"\n\nPrevious draft is {current} words. Rewrite THAT draft — do not start over. "
            f"{order_rule}"
            f"Cut or add whole sentences (never pad with filler or hype) until the total word count, "
            f"including the ask, is between {low} and {high}. It must still read like a person talking. "
            f"Return the full revised JSON.\n"
            f"{json.dumps(draft, ensure_ascii=False)}"
        )
    if corrections:
        user += "\n\nPrevious draft failed validation. Fix these issues:\n- " + "\n- ".join(corrections)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def voice_review_messages(script: dict[str, Any], founder_quotes: list[FounderQuote]) -> list[dict[str, str]]:
    voice = "\n".join(format_founder_line(quote) for quote in founder_quotes) or "(none)"
    system = (
        "You review a spoken pitch for whether it sounds like Pratham Mittal talking. "
        "Use only the transcript excerpts as the style reference. "
        "Pass if the draft is mostly direct, conversational, concrete-to-point, and plain. "
        "Fail if it copies transcript sentences, treats transcript anecdotes as unsourced facts, "
        "or sounds like a brochure more than a person talking. "
        "Do not fail locked institutional wording that must stay exact: university-status language, "
        "CTC figures, membership names, or other verified facts. Those can sound formal. "
        "Do not fail a heading label. Judge the spoken text only. "
        "Minor leftover formality is not enough to fail if the voice is still spoken. "
        'Return JSON only: {"passed":true,"score":0.0,"violations":["..."]}'
    )
    user = (
        f"PRATHAM TRANSCRIPT EXCERPTS:\n{voice}\n\n"
        f"DRAFT SCRIPT:\n{json.dumps(script, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def review_pratham_voice(script: dict[str, Any], founder_quotes: list[FounderQuote]) -> dict[str, Any]:
    from backend.pipeline.llm import chat_json

    if not founder_quotes:
        return {
            "passed": False,
            "score": 0.0,
            "violations": ["Approved Pratham Mittal transcript excerpts are missing"],
        }
    payload = chat_json(voice_review_messages(script, founder_quotes))
    violations = [
        str(item).strip()
        for item in (payload.get("violations") or [])
        if str(item).strip()
    ]
    try:
        score = float(payload.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    passed = bool(payload.get("passed")) and score >= 0.7
    if not passed and not violations:
        violations = ["The draft does not sound like Pratham Mittal speaking."]
    return {"passed": passed, "score": score, "violations": violations}
