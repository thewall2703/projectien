from __future__ import annotations

import json
from typing import Any

from backend.models import FounderQuote, LockedFact, Module
from backend.pipeline.resolver import MAX_SCRIPT_MINUTES, script_minutes_for_duration
from backend.pipeline.validator import budget_range, count_script_words
from backend.schemas import AUDIENCE_CLUSTERS, CHANNELS, DURATIONS, INTENTS, TEMPERATURES
from backend.transcripts import format_founder_line

PRATHAM_REFERENCE_RULES = (
    "PRATHAM BY BEAT AND PLAYBOOK — his real speech matched to this talk's beats:\n"
    "- Build each beat's delivery on its PRATHAM BY BEAT passage: his order of ideas, "
    "his examples and questions, and the way each sentence picks up the last.\n"
    "- Reuse his sentences and phrases freely (verbatim or adapted). Do not paste a whole passage.\n"
    "- Build at least one beat on a PLAYBOOK move where it fits: adapt the framework, analogy, "
    "story shape, opener, or objection handling. Adapt the move; do not paste the whole excerpt.\n"
    "- Same identity rules as FOUNDER VOICE: personal material is told in third person with attribution.\n"
    "- These are raw transcripts, not verified facts. State a number or claim from them only if LOCKED or "
    "REPORT EVIDENCE supports it; otherwise keep the move and drop the figure.\n\n"
)

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
        "30-minute session, 20-minute spoken script: write a 20-minute deck-led talk and leave the "
        "rest of the hour for dialogue, slides, and questions. Let approved assets carry evidence; "
        "do not write a 30-minute monologue or pad to fill the session."
    ),
    "T5": (
        "90-minute session, 20-minute spoken script: write a 20-minute guided talk. The remaining "
        "time is campus, assets, and Q&A — not more spoken copy. Do not write a 90-minute monologue."
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
    style_guide: str = "",
    plan: dict[str, Any] | None = None,
    pratham_reference: str = "",
) -> list[dict[str, str]]:
    locked, forbidden = _format_facts(facts, sequence)
    voice_lines = [format_founder_line(quote) for quote in (founder_quotes or [])]
    voice = "\n".join(voice_lines) or "(none)"
    reports = _format_report_passages(report_passages)
    low, high = budget_range(word_budget)
    system = (
        "You are a Masters' Union insider writing a pitch that a real person will SAY OUT LOUD "
        "to one listener. Not a brochure. Not a slide. A person talking. Return JSON only.\n\n"
        "SPOKEN ENGLISH, NOT WRITTEN ENGLISH (this matters more than anything else):\n"
        "The script is heard once, at talking speed, by someone who cannot re-read a line. Write the words "
        "a person actually says across a table, not the sentences they would write in an essay or on a slide.\n"
        "- Say it, then read it aloud in your head. If you would not say that exact sentence to a parent or "
        "student in a real conversation, rewrite it until you would.\n"
        "- Short sentences, one idea each, subject and verb up front — but CHAINED like Pratham. "
        "Most sentences pick up the one before: a link word (so, and, but, because, now), a pointer "
        "(that, this, it), or repeating the previous sentence's key word. "
        "Real example from his sessions: 'This is not playboy school. That is not this program. "
        "This program is the buffet is laid out. Now you have to get up.'\n"
        "  COLD: 'A practical model needs an institution behind it. Our purpose is practitioner-led "
        "education, with formal governance that challenges delivery. The Board of Governors on this "
        "slide is part of that structure.'\n"
        "  CHAINED: 'A model like this needs an institution behind it. And that institution has to be "
        "able to say no to us. That's what the Board of Governors on this slide is for.'\n"
        "- A list of names or bodies is said once, in one sentence, then the point — never one "
        "'They include…' sentence per item.\n"
        "- Verbs, not noun phrases: 'we built it so you practise every week', not 'the model is designed to "
        "enable repeated practice'. No stacked labels like 'practitioner-led learning outcomes'.\n"
        "- No written punctuation in the spoken text: no colons, slashes, semicolons, brackets, or "
        "'X: Y' label lines. Say the connection in words ('Here's why.', 'And that matters because…').\n"
        "- No announced transitions. Do not narrate the talk's structure: never 'the next question is', "
        "'now let's look at', 'let's zoom into', 'once that is clear', 'with X in place', 'that brings us to', "
        "'moving on'. Most sections simply start on their point, the way people move from one thing to "
        "the next in conversation. At most two or three spoken turns in the whole talk, and they must sound "
        "like speech ('So what does that look like on a Tuesday?', 'Here's the part parents ask about.').\n"
        "- No reflexive caveats. Do not undercut each point ('A pitch isn't a company, but…', 'Growth alone "
        "doesn't prove…', 'A pin on a map teaches nothing by itself'). Qualify a claim only where a LOCKED "
        "fact, its note, or a HARD FACTUAL RULE requires it. Use the 'it isn't X, it's Y' shape at most once "
        "in the whole script.\n"
        "- A name needs its story. Mention a student, founder or company only if you say who they are, "
        "what they did, and what happened. If the material gives you only a name, leave the name out.\n"
        "- Numbers the way people say them: 'six startups', 'forty percent of our faculty are practitioners'. "
        "Keep every figure exact.\n"
        "- Spell out an abbreviation the first time you say it unless every listener already uses it.\n"
        "Written → spoken, for calibration:\n"
        "  WRITTEN: 'Once that institutional foundation is clear, the next question is what practitioner-led "
        "learning is meant to produce in a student.'\n"
        "  SPOKEN: 'So what does all this actually turn you into?'\n"
        "  WRITTEN: 'External judgment matters: 6 startups pitched on Shark Tank India across Seasons 1–5.'\n"
        "  SPOKEN: 'Six of our student startups have pitched on Shark Tank India. That's a room full of "
        "investors who don't care where you studied.'\n"
        "  WRITTEN: 'The faculty mix is 40% practitioners / 30% full-time PhD / 30% visiting international.'\n"
        "  SPOKEN: 'Forty percent of the people teaching you are practitioners. Thirty percent are full-time "
        "PhDs. The other thirty percent fly in from abroad.'\n"
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
        "no markdown. Separate spoken beats with a blank line so the reader can breathe — a new "
        "paragraph every two or three sentences, or whenever the idea, example, or addressee shifts. "
        "Longer sessions (10 minutes and up) must never be one unbroken wall of text.\n\n"
        "SPEAKER IDENTITY — DO NOT CONFUSE STYLE WITH IDENTITY:\n"
        "- This script will be delivered by a Masters' Union EMPLOYEE or representative. The speaker is "
        "not Pratham Mittal and must never impersonate him.\n"
        "- The employee may say 'we', 'our', and 'at Masters\\' Union' for institutional actions and beliefs. "
        "For anything specific to the founder's life, memories, education, relationships, or achievements, "
        "refer to Pratham in the third person with attribution — never as the employee's own first-person story.\n"
        "- Never write founder-biography claims in the employee's first person: no 'I started Masters\\' Union', "
        "'when I was at Wharton', 'I met every parent', 'I lived in the hostel', or equivalent identity borrowing.\n"
        "- A third-person reference to Pratham is correct employee narration, not a voice failure.\n\n"
        "FOUNDER EXCERPTS — reuse allowed, identity is not:\n"
        "- The FOUNDER VOICE block is Pratham Mittal speaking in real talks. Excerpt sentences and phrasing "
        "may be reused, verbatim or adapted, when they fit the beat.\n"
        "- Generic lines (beliefs, philosophy, how Masters' Union works) can be said in the employee's "
        "own voice with 'we' / 'at Masters\\' Union'.\n"
        "- Anything personal to Pratham — his memories, education, decisions, relationships, achievements, "
        "first-person anecdotes — must be converted to third person and attributed "
        "('Pratham likes to say…', 'When Pratham started Masters\\' Union, he…'). Never first-person "
        "founder biography from the employee.\n"
        "- Facts and numbers from the excerpts may be used. If the same fact appears in LOCKED or REPORT "
        "EVIDENCE with a different value, LOCKED/REPORT wins. FORBIDDEN facts are never stated, even if "
        "an excerpt says them.\n"
        + (
            "- The PRATHAM STYLE AND STRUCTURE GUIDE complements FOUNDER VOICE: the guide is rules for "
            "transferable structure and register; the voice block is register examples. Treat observed devices "
            "as context-dependent, not a checklist: do not force Hindi, named parts, audience games, prizes, "
            "or Q&A mechanics into a pitch where they do not naturally fit.\n\n"
            if style_guide
            else "\n"
        )
        + (
            PRATHAM_REFERENCE_RULES
            if pratham_reference
            else ""
        )
        + "IMPACT MUST BE PROPORTIONAL TO TIME:\n"
        "- 30–60 seconds: precise, specific, factual; one point and one proof.\n"
        "- 2 minutes: highlight the listener's need and position Masters' Union as the solution.\n"
        "- 5 minutes: build a story; read the audience's likely emotional state, match it to the information "
        "or approved asset they will receive best, and create desire for Masters' Union.\n"
        "- 10 minutes: use the same audience-aware story with richer storytelling, relevant FAQs, anecdotes, "
        "and student case studies.\n"
        "- Longer sessions (20 minutes and up): write at most a 20-minute spoken script. Extra session "
        "time is dialogue, deck, campus, and Q&A — never more spoken copy or padding.\n\n"
        "HARD FACTUAL RULES:\n"
        "- Stay inside the hard word range given in the user message.\n"
        "- LOCKED facts: keep every number, name and figure exact, but say them in spoken English, never as "
        "the fact's label or slash-separated value. The university-status line is the one exception: say it "
        "word for word.\n"
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
        "'Moving on', 'As I mentioned', or anything that assumes earlier speech. The selected slides are "
        "listed in speaking order: anchor the opening in the FIRST selected slide and explain its central "
        "idea before using any later slide. Do not choose a vivid detail from a later slide as the opening.\n"
        "- BODY: Continue the same conversation. Connect naturally to the previous beat, explain only what "
        "the selected slides support, in their listed order, and land one clear point before moving forward.\n"
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
        f"Spoken script length: {script_minutes_for_duration(duration):g} minutes "
        f"(never more than {MAX_SCRIPT_MINUTES} minutes, even if the session is longer). "
        f"Word limit: {word_budget} words at 120 spoken words per minute. "
        f"Write {low}-{high} words and never exceed {high}.\n"
        f"{flow_block}\n\n"
        f"FOUNDER VOICE EXCERPTS — employee is the speaker; reuse phrasing/facts per the rules above:\n"
        f"{voice}\n\n"
        + (
            "PRATHAM-DERIVED EMPLOYEE STYLE GUIDE — use only the transferable, context-appropriate "
            "structural moves and register:\n"
            f"{style_guide}\n\n"
            if style_guide
            else ""
        )
        + (f"{pratham_reference}\n\n" if pratham_reference else "")
        + f"LOCKED — exact figures, said in spoken English:\n{locked}\n\n"
        f"FORBIDDEN — never state these:\n{forbidden}\n\n"
        f"REPORT EVIDENCE:\n{reports}"
    )
    if plan:
        beat_lines: list[str] = []
        for beat in plan.get("beats") or []:
            if not isinstance(beat, dict):
                continue
            beat_lines.append(
                f"- topic_id={beat.get('topic_id')} ({beat.get('role')}): "
                f"point={beat.get('point')}; proof={beat.get('proof')} "
                f"[{beat.get('proof_source')}]; story_device={beat.get('story_device')}; "
                f"bridge_in={beat.get('bridge_in') or '(opening)'}; "
                f"pratham_move={beat.get('pratham_move') or '(none)'}; "
                f"approx_words={beat.get('approx_words')}"
            )
        user += (
            "\n\nSTORY PLAN — follow this spine; each section's text must deliver its beat's "
            "point and use its proof. bridge_in is only a note on how the idea connects to the previous "
            "beat — do NOT open the section with it or announce the transition. Most sections just start "
            "on their point:\n"
            f"Throughline: {plan.get('throughline') or ''}\n"
            f"Listener start → end: {plan.get('listener_start') or ''} → "
            f"{plan.get('listener_end') or ''}\n"
            f"Arc: {plan.get('arc') or ''}\n"
            f"Ask: {plan.get('ask') or ''}\n"
            f"Beats:\n{chr(10).join(beat_lines) or '(none)'}"
        )
    if draft:
        current = count_script_words(draft)
        order_rule = (
            "Keep the same topic_id, pages, topic_title, beat roles, locked facts, and employee voice. "
            if topic_flow
            else "Keep the same module order, the locked facts, and the employee voice. "
        )
        user += (
            f"\n\nPrevious draft is {current} words. Rewrite THAT draft — do not start over. "
            f"{order_rule}"
            f"Cut or add whole sentences (never pad with filler or hype) until the total word count, "
            f"including the ask, is between {low} and {high}. It must still sound like a person talking, in spoken English. "
            f"Return the full revised JSON.\n"
            f"{json.dumps(draft, ensure_ascii=False)}"
        )
    if corrections:
        user += "\n\nPrevious draft failed validation. Fix these issues:\n- " + "\n- ".join(corrections)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def voice_review_messages(
    script: dict[str, Any],
    founder_quotes: list[FounderQuote],
    style_guide: str = "",
    duration: str = "",
    channel: str = "",
    intent: str = "",
    context_note: str = "",
) -> list[dict[str, str]]:
    voice = "\n".join(format_founder_line(quote) for quote in founder_quotes) or "(none)"
    system = (
        "You review a spoken pitch written for a Masters' Union EMPLOYEE or representative. "
        "Pratham Mittal is the style source, NOT the speaker. The goal is an employee who communicates "
        "with Pratham's directness, conversational rhythm, specificity, and plain language without "
        "impersonating him. Pass if the draft is mostly direct, conversational, concrete-to-point, and plain. "
        "Third-person references to Pratham are correct when discussing founder-specific material "
        "(including attributed quotes and anecdotes); NEVER fail a draft merely because it refers to him "
        "in the third person or reuses excerpt phrasing with attribution. Fail identity only when the "
        "employee claims Pratham's personal biography, memories, or actions as their own first-person "
        "experience. Fail if the draft sounds like a brochure more than a person talking. "
        "Do not fail locked institutional wording that must stay exact: university-status language, "
        "CTC figures, membership names, or other verified facts. Those can sound formal. "
        "Do not fail a heading label. Judge the spoken text only. "
        "Minor leftover formality is not enough to fail if the voice is still spoken. "
        + (
            "Use the PRATHAM-DERIVED EMPLOYEE STYLE GUIDE as contextual guidance, not a mandatory checklist. "
            "Apply only rules appropriate to the pitch duration, channel, intent, and context. The absence of "
            "Hindi, named parts, gamification, prizes, audience interaction, or a question framework is not a "
            "failure unless the supplied pitch context specifically requires that device. "
            if style_guide
            else ""
        )
        + 'Return JSON only: {"passed":true,"score":0.0,"violations":["..."]}'
    )
    user = (
        f"PITCH CONTEXT:\nDuration: {duration or '(unknown)'}\nChannel: {channel or '(unknown)'}\n"
        f"Intent: {intent or '(unknown)'}\nContext note: {context_note or '(none)'}\n\n"
        f"PRATHAM TRANSCRIPT EXCERPTS — the script may reuse their phrasing and facts; they are "
        f"not the speaker's identity:\n{voice}\n\n"
        + (
            f"PRATHAM-DERIVED EMPLOYEE STYLE GUIDE:\n{style_guide}\n\n"
            if style_guide
            else ""
        )
        + f"DRAFT SCRIPT:\n{json.dumps(script, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def review_pratham_voice(
    script: dict[str, Any],
    founder_quotes: list[FounderQuote],
    style_guide: str = "",
    duration: str = "",
    channel: str = "",
    intent: str = "",
    context_note: str = "",
) -> dict[str, Any]:
    from backend.pipeline.llm import chat_json

    if not founder_quotes:
        return {
            "passed": False,
            "score": 0.0,
            "violations": ["Approved Pratham Mittal transcript excerpts are missing"],
        }
    payload = chat_json(
        voice_review_messages(
            script,
            founder_quotes,
            style_guide=style_guide,
            duration=duration,
            channel=channel,
            intent=intent,
            context_note=context_note,
        ),
        role="voice_judge",
    )
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
        violations = ["The draft does not sound like a Masters' Union employee using the approved founder-derived style."]
    return {"passed": passed, "score": score, "violations": violations}
