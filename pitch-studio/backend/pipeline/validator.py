from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from backend.models import LockedFact

MEMBERSHIP_NAMES = ("EFMD", "AACSB", "BGA", "BSIS", "NSDC")
FORBIDDEN_STATUSES = {"conflict", "do_not_use", "needs_source", "needs_decision"}
AVERAGE_MARKER = "33.39"
MEDIAN_MARKER = "27.78"
OPENING_CONTINUATION_RE = re.compile(
    r"^\s*[\"'“‘]*(?:"
    r"then\b|and then\b|so then\b|next\b|moving on\b|finally\b|"
    r"as (?:i|we) (?:said|mentioned|noted)\b|"
    r"another (?:thing|point|reason)\b"
    r")",
    re.IGNORECASE,
)
OPENING_CAMPUS_RE = re.compile(r"\b(?:Gurugram|Cyberpark|campus)\b", re.IGNORECASE)
# Words that can never end an English sentence. Kept deliberately narrow: a false
# positive forces a rewrite and can fail the generation after retries.
DANGLING_END_WORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "nor", "if", "because", "although",
        "whether", "than", "my", "your", "our", "their", "its", "very",
        "i'm", "you're", "we're", "they're", "it's", "that's", "there's", "here's",
        "what's", "who's", "he's", "she's", "let's",
    }
)
SENTENCE_END_RE = re.compile(r"[.!?…][\"'”’)\]]*$")
MIN_SECTION_WORDS = 12
# Written-English markers in text meant to be said aloud. Thresholds stay loose because a
# hard violation that survives the correction rewrites fails the generation.
SIGNPOST_RE = re.compile(
    r"\bthe next question is\b"
    r"|\bnow let'?s\b"
    r"|\bnext,? let'?s\b"
    r"|\blet'?s (?:now )?(?:zoom|turn|move on|look at|talk about|dive|unpack)\b"
    r"|\bonce (?:that|this|the)\b[^.?!]{0,60}\b(?:is|are) (?:clear|established|in place)\b"
    r"|\bwith (?:that|this|the)\b[^.?!]{0,50}\b(?:clear|named|in place|established|covered)\b,"
    r"|\b(?:that|this|which) brings us to\b"
    r"|\bmoving on\b"
    r"|\bhaving (?:seen|covered|established)\b"
    r"|\bso here'?s that\b"
    r"|\bnow that we'?ve (?:seen|covered|looked)\b",
    re.IGNORECASE,
)
MAX_SIGNPOSTS = 2
SLASH_RE = re.compile(r"\s/\s|(?<=[A-Za-z%])/(?=[A-Za-z0-9])")
LABEL_COLON_RE = re.compile(r"(?<!\d):(?!\d)")
MAX_LABEL_COLONS = 1


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9₹.%]+", text)


_ABBREV_END_RE = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Jr|Sr|vs|etc|Inc|Ltd|St)\.$", re.IGNORECASE)


def split_spoken_sentences(text: str) -> list[str]:
    pieces = [part.strip() for part in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if part.strip()]
    sentences: list[str] = []
    buf = ""
    for piece in pieces:
        candidate = f"{buf} {piece}".strip() if buf else piece
        last = candidate.split()[-1] if candidate.split() else ""
        if _ABBREV_END_RE.search(candidate) or (candidate.endswith(".") and len(last) <= 2):
            buf = candidate
            continue
        sentences.append(candidate)
        buf = ""
    if buf:
        sentences.append(buf)
    return sentences


def paragraphize_spoken_text(text: str, sentences_per_para: int = 2) -> str:
    """Break a spoken blob into blank-line paragraphs without changing the words."""
    raw = (text or "").strip()
    if not raw:
        return ""
    if re.search(r"\n\s*\n", raw):
        parts = [re.sub(r"[ \t]+", " ", part).strip() for part in re.split(r"\n\s*\n", raw)]
        return "\n\n".join(part for part in parts if part)
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines() if line.strip()]
    if len(lines) >= 2:
        return "\n\n".join(lines)
    collapsed = re.sub(r"\s+", " ", raw)
    sentences = split_spoken_sentences(collapsed)
    if len(sentences) <= 2:
        return collapsed
    step = max(1, sentences_per_para)
    chunks = [" ".join(sentences[index : index + step]) for index in range(0, len(sentences), step)]
    return "\n\n".join(chunks)


def paragraphize_script(script: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(script)
    for section in updated.get("sections") or []:
        section["text"] = paragraphize_spoken_text(section.get("text") or "")
    if updated.get("cta"):
        updated["cta"] = re.sub(r"\s+", " ", str(updated["cta"])).strip()
    return updated


def script_text(script: dict[str, Any]) -> str:
    body = "\n".join(section.get("text", "") for section in script.get("sections", []))
    cta = (script.get("cta") or "").strip()
    if cta and cta not in body:
        return f"{body}\n{cta}".strip()
    return body.strip()


def count_script_words(script: dict[str, Any]) -> int:
    return len(_words(script_text(script)))


def budget_range(word_budget: int) -> tuple[int, int]:
    return int(word_budget * 0.85), word_budget


def _section_paragraphs(text: str) -> list[list[str]]:
    return [
        split_spoken_sentences(re.sub(r"\s+", " ", part))
        for part in re.split(r"\n\s*\n", (text or "").strip())
        if part.strip()
    ]


def trim_script_to_budget(script: dict[str, Any], word_budget: int) -> dict[str, Any]:
    """Remove whole sentences to meet the budget, without changing section structure.

    Only a section's middle sentences are removable: its first sentence carries the
    beat's setup and its last lands the point or bridges to the next beat. When whole
    sentences cannot close the gap, the script stays over budget so validation sends it
    back to the writer rather than cutting a sentence in half.
    """
    low, high = budget_range(word_budget)
    excess = count_script_words(script) - high
    if excess <= 0:
        return paragraphize_script(script)

    trimmed = deepcopy(script)
    cta = (trimmed.get("cta") or "").strip()
    parsed = {
        index: _section_paragraphs(section.get("text") or "")
        for index, section in enumerate(trimmed.get("sections") or [])
        if (section.get("text") or "").strip() and (not cta or cta not in section.get("text", ""))
    }
    slack = high - low

    while excess > 0:
        candidates: list[tuple[int, int, int, int, int]] = []
        for index, paragraphs in parsed.items():
            flat = [(p, s) for p, sentences in enumerate(paragraphs) for s in range(len(sentences))]
            section_words = sum(len(_words(sentence)) for sentences in paragraphs for sentence in sentences)
            for p, s in flat[1:-1]:
                candidates.append((section_words, len(_words(paragraphs[p][s])), index, p, s))
        fitting = [item for item in candidates if excess <= item[1] <= excess + slack]
        if fitting:
            choice = max(fitting, key=lambda item: (item[0], -item[1]))
        else:
            smaller = [item for item in candidates if 0 < item[1] < excess]
            if not smaller:
                break
            choice = max(smaller, key=lambda item: (item[0], item[1]))
        _section_words, removed, index, p, s = choice
        del parsed[index][p][s]
        parsed[index] = [sentences for sentences in parsed[index] if sentences]
        excess -= removed

    sections = trimmed.get("sections") or []
    for index, paragraphs in parsed.items():
        sections[index]["text"] = "\n\n".join(" ".join(sentences) for sentences in paragraphs)
    return paragraphize_script(trimmed)


def _section_pages(section: dict[str, Any]) -> list[int]:
    pages: list[int] = []
    for raw in section.get("pages") or []:
        try:
            page = int(raw)
        except (TypeError, ValueError):
            continue
        if page > 0:
            pages.append(page)
    return pages


def _shorten(text: str, max_words: int = 80) -> str:
    words = [part for part in text.split() if part]
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).strip()


def align_script_to_topics(
    script: dict[str, Any],
    topics: list[Any],
    modules: list[Any] | None = None,
) -> dict[str, Any]:
    """Force one section per ordered Brand Deck topic."""
    by_topic: dict[int, dict[str, Any]] = {}
    by_module: dict[str, dict[str, Any]] = {}
    for section in script.get("sections") or []:
        topic_id = int(section.get("topic_id") or 0)
        if topic_id:
            by_topic[topic_id] = section
        module_id = str(section.get("module_id") or "")
        if module_id:
            by_module[module_id] = section
    module_by_id = {item.id: item for item in (modules or []) if getattr(item, "id", None)}
    aligned: list[dict[str, Any]] = []
    for topic in topics:
        existing = by_topic.get(topic.topic_id)
        if existing is None:
            for module_id in topic.recipe_modules:
                if module_id in by_module:
                    existing = by_module[module_id]
                    break
        heading = ""
        text = ""
        if existing:
            heading = (existing.get("heading") or "").strip()
            text = (existing.get("text") or "").strip()
        heading = heading or topic.title
        if not text:
            for module_id in topic.recipe_modules:
                module = module_by_id.get(module_id)
                if module:
                    text = _shorten((module.core_content or module.job or "").strip())
                    if text:
                        break
        if not text:
            text = topic.summary or f"{topic.title}."
        if topic.recipe_modules and topic.recipe_modules[-1] == "M14" and script.get("cta") and script["cta"] not in text:
            text = f"{text} {script['cta']}".strip()
        aligned.append(
            {
                "module_id": (topic.recipe_modules[0] if topic.recipe_modules else (topic.module_ids[0] if topic.module_ids else "")),
                "topic_id": topic.topic_id,
                "topic_title": topic.title,
                "pages": list(topic.pages),
                "slide_keys": list(topic.slide_keys),
                "heading": heading,
                "text": text,
            }
        )
    return paragraphize_script({"sections": aligned, "cta": script.get("cta") or ""})


def align_script_to_recipe(
    script: dict[str, Any],
    sequence: list[str],
    modules: list[Any] | None = None,
) -> dict[str, Any]:
    """Force one section per recipe module, in order.

    The model often drops the close (M14) or reorders sections. Rebuild from the
    recipe and fill gaps from approved module copy so generation does not fail.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for section in script.get("sections") or []:
        module_id = section.get("module_id") or ""
        if module_id:
            by_id[module_id] = section
    module_by_id = {item.id: item for item in (modules or []) if getattr(item, "id", None)}
    aligned: list[dict[str, Any]] = []
    for module_id in sequence:
        existing = by_id.get(module_id)
        module = module_by_id.get(module_id)
        heading = ""
        text = ""
        if existing:
            heading = (existing.get("heading") or "").strip()
            text = (existing.get("text") or "").strip()
        if module:
            heading = heading or module.name or module_id
            if not text:
                text = _shorten((module.core_content or module.job or "").strip())
        heading = heading or module_id
        if not text:
            text = (
                f"{heading}. Name the next step and stop talking."
                if module_id == "M14"
                else f"{heading}."
            )
        if module_id == "M14" and script.get("cta") and script["cta"] not in text:
            text = f"{text} {script['cta']}".strip()
        aligned.append({"module_id": module_id, "heading": heading, "text": text})
    return paragraphize_script({"sections": aligned, "cta": script.get("cta") or ""})


def _last_word(sentence: str) -> str:
    words = sentence.replace("’", "'").split()
    return words[-1].strip("\"'“”‘’()[].,!?…;:–—-").lower() if words else ""


def _quote(text: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def sentence_integrity_violations(script: dict[str, Any], word_budget: int) -> list[str]:
    """Flag broken sentences: dangling endings, unfinished sections and stub sections."""
    violations: list[str] = []
    sections = script.get("sections") or []
    # Short scripts spread over many slides legitimately have short sections.
    min_words = min(MIN_SECTION_WORDS, max(4, word_budget // (2 * max(1, len(sections)))))
    for number, section in enumerate(sections, start=1):
        text = (section.get("text") or "").strip()
        if not text:
            continue
        label = f"Section {number} ({section.get('topic_title') or section.get('heading') or section.get('module_id') or 'untitled'})"
        if not SENTENCE_END_RE.search(text):
            violations.append(
                f"{label} ends without a complete sentence: \"{_quote(text)}\". "
                "Finish the thought or remove the fragment"
            )
        for sentence in split_spoken_sentences(re.sub(r"\s+", " ", text)):
            if _last_word(sentence) in DANGLING_END_WORDS:
                violations.append(
                    f"{label} has a sentence cut off mid-thought: \"{_quote(sentence)}\". "
                    "Complete the sentence or remove it"
                )
        if len(_words(text)) < min_words:
            violations.append(
                f"{label} is only {len(_words(text))} words: \"{_quote(text)}\". "
                "Give this beat a complete spoken point or fold it into a neighbouring beat's idea"
            )
    cta = (script.get("cta") or "").strip()
    if cta and _last_word(cta) in DANGLING_END_WORDS:
        violations.append(f"The final ask is cut off mid-thought: \"{_quote(cta)}\". Complete it")
    return violations


def spoken_english_violations(script: dict[str, Any]) -> list[str]:
    """Flag written-English habits a listener hears as a narrated document."""
    violations: list[str] = []
    texts = [
        (f"Section {number}", (section.get("text") or "").strip())
        for number, section in enumerate(script.get("sections") or [], start=1)
    ] + [("The final ask", (script.get("cta") or "").strip())]
    signposts: list[str] = []
    colons: list[str] = []
    for label, text in texts:
        if not text:
            continue
        for sentence in split_spoken_sentences(re.sub(r"\s+", " ", text)):
            if SLASH_RE.search(sentence):
                violations.append(
                    f"{label} reads out a slash: \"{_quote(sentence)}\". Say it as a spoken sentence"
                )
            if SIGNPOST_RE.search(sentence):
                signposts.append(f"{label}: \"{_quote(sentence)}\"")
            if LABEL_COLON_RE.search(sentence):
                colons.append(f"{label}: \"{_quote(sentence)}\"")
    if len(signposts) > MAX_SIGNPOSTS:
        violations.append(
            f"The script announces its own transitions {len(signposts)} times ("
            + "; ".join(signposts[:6])
            + "). Cut these and start each section on its point, the way people talk"
        )
    if len(colons) > MAX_LABEL_COLONS:
        violations.append(
            "Colons read like slide labels, not speech ("
            + "; ".join(colons[:4])
            + "). Say the connection in words"
        )
    return violations


def validate_script(
    script: dict[str, Any],
    facts: list[LockedFact],
    sequence: list[str],
    word_budget: int,
    topics: list[Any] | None = None,
) -> list[str]:
    violations: list[str] = []
    sections = script.get("sections") or []
    if topics is not None:
        expected_ids = [topic.topic_id for topic in topics]
        section_ids = [int(section.get("topic_id") or 0) for section in sections]
        if section_ids != expected_ids:
            violations.append(
                f"Section topic order {section_ids} does not match deck topics {expected_ids}"
            )
        if len(sections) != len(topics):
            violations.append(
                f"Expected one section per deck topic ({len(topics)}), got {len(sections)}"
            )
        for topic, section in zip(topics, sections):
            pages = _section_pages(section)
            if pages != list(topic.pages):
                violations.append(
                    f"Topic {topic.topic_id} pages {pages} do not match selected slides {topic.pages}"
                )
        covered = [page for section in sections for page in _section_pages(section)]
        expected = [page for topic in topics for page in topic.pages]
        if covered != expected:
            violations.append(f"Selected slide coverage {covered} does not match deck pages {expected}")
        if sections:
            opening = (sections[0].get("text") or "").strip()
            if OPENING_CONTINUATION_RE.search(opening):
                violations.append(
                    "The opening starts like a continuation. Rewrite the first sentence so it "
                    "stands alone for a listener hearing the pitch from the beginning"
                )
            first_sentence = re.split(r"(?<=[.!?])\s+", opening, maxsplit=1)[0]
            opening_modules = set(getattr(topics[0], "recipe_modules", []) or [])
            if "M09" not in opening_modules and OPENING_CAMPUS_RE.search(first_sentence):
                violations.append(
                    "The opening jumps ahead to Gurugram/campus even though the opening slides "
                    "are not the Campus module. Anchor the first sentence in the first selected "
                    "slide; save location details for the M09 beat"
                )
    else:
        section_ids = [section.get("module_id", "") for section in sections]
        if section_ids != sequence:
            violations.append(
                f"Section module order {section_ids} does not match recipe {sequence}"
            )

    if not (script.get("cta") or "").strip():
        violations.append("Script is missing one final ask")
    violations.extend(sentence_integrity_violations(script, word_budget))
    violations.extend(spoken_english_violations(script))

    full = script_text(script)
    word_count = count_script_words(script)
    low, high = budget_range(word_budget)
    if word_count < low or word_count > high:
        violations.append(
            f"Word count {word_count} is outside the {low}-{high} range "
            f"({word_budget}-word hard limit)"
        )

    for fact in facts:
        if fact.status not in FORBIDDEN_STATUSES:
            continue
        value = (fact.value or "").strip()
        if len(value) >= 8 and value in full:
            violations.append(f"Forbidden fact appears: {fact.fact} = {value}")

    for section in sections:
        text = section.get("text", "")
        if AVERAGE_MARKER in text and MEDIAN_MARKER not in text:
            violations.append(
                "Average CTC mentioned without median CTC in the same section"
            )

    sentences = re.split(r"(?<=[.!?])\s+", full)
    for sentence in sentences:
        if "accreditation" not in sentence.lower():
            continue
        if any(name.lower() in sentence.lower() for name in MEMBERSHIP_NAMES):
            violations.append(
                "Memberships must not be called accreditations in the same sentence"
            )
            break
    return violations
