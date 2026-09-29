"""Deterministic verifier for Master Script Voice output."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Never-say patterns. Careful with false positives:
# - bare MU/MUU is banned, but "MU Ventures" and "Masters' Union" are allowed
# - "accredited" / "accreditation" banned except inside locked-line placeholders
#   or when negating ("not accredited", "I'm not going to call them accreditations")
# - average requires median in the same or adjacent sentence

_BUSINESS_SCHOOL = re.compile(
    r"\b(?:business\s+schools?|b[\s\-]?schools?)\b",
    re.IGNORECASE,
)
_MU_ABBREV = re.compile(r"(?<![A-Za-z])(?:MU|MUU)(?![A-Za-z])")
_MU_VENTURES_OK = re.compile(r"\bMU\s+Ventures\b", re.IGNORECASE)
_YOUNGEST = re.compile(
    r"\byoungest\b.*\b(?:institution|university)\b|\bin\s+four\s+and\s+a\s+half\s+years\b",
    re.IGNORECASE,
)
_FIRST_PRACTITIONER = re.compile(
    r"\b(?:india'?s\s+)?first\s+practitioner[\s\-]?led\s+university\b",
    re.IGNORECASE,
)
_ACCREDITED = re.compile(r"\baccredit(?:ed|ation|ations)?\b", re.IGNORECASE)
_ACCREDITED_NEGATION = re.compile(
    r"(?:\bnot\s+accredit(?:ed|ation|ations)?\b|"
    r"\b(?:don'?t|do\s+not|never|won'?t|will\s+not|not\s+going\s+to)\b"
    r".{0,40}\baccredit(?:ed|ation|ations)?\b|"
    r"\baccredit(?:ed|ation|ations)?\b.{0,40}\b(?:not|never)\b)",
    re.IGNORECASE,
)
_ENDORSED = re.compile(r"\bendorsed\b", re.IGNORECASE)
_AVERAGE = re.compile(r"\baverage\b", re.IGNORECASE)
_MEDIAN = re.compile(r"\bmedian\b", re.IGNORECASE)

_LOCK_PLACEHOLDER = re.compile(r"\{\{LOCK:([^}]+)\}\}")

# Number forms: digits, lakh/crore, percent, K, x multipliers, spelled small ints.
_NUMBER_TOKEN = re.compile(
    r"(?<![A-Za-z])(?:"
    r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|"  # 2,500 / 33.39 style with commas
    r"\d+(?:\.\d+)?|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|"
    r"thousand|lakh|crore)(?:[\s\-](?:one|two|three|four|five|six|seven|"
    r"eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|"
    r"seventy|eighty|ninety|hundred|thousand|lakh|crore))*"
    r")(?:\s*(?:lakh|crore|percent|%|k|x)\b)?",
    re.IGNORECASE,
)

_SPELLED_SKIP = {
    "a",
    "an",
    "the",
    "one",  # "one thing" / "one sentence" — still checked if surrounded by figure context
}


@dataclass
class VerifyIssue:
    code: str
    message: str
    section_id: str = ""
    quote: str = ""


@dataclass
class VerifyReport:
    passed: bool
    issues: list[VerifyIssue] = field(default_factory=list)

    def messages(self) -> list[str]:
        return [issue.message for issue in self.issues]


def substitute_locked_placeholders(
    text: str,
    locked: dict[str, str],
    *,
    fail_on_unknown: bool = True,
) -> tuple[str, list[str]]:
    """Replace ``{{LOCK:id}}`` with verbatim locked text. Returns (text, errors)."""
    errors: list[str] = []

    def repl(match: re.Match[str]) -> str:
        lock_id = match.group(1).strip()
        if lock_id not in locked:
            if fail_on_unknown:
                errors.append(f"Unknown locked placeholder {{{{LOCK:{lock_id}}}}}")
            return match.group(0)
        return locked[lock_id]

    return _LOCK_PLACEHOLDER.sub(repl, text), errors


def remaining_placeholders(text: str) -> list[str]:
    return [match.group(1).strip() for match in _LOCK_PLACEHOLDER.finditer(text or "")]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [part.strip() for part in parts if part.strip()]


def check_never_say(
    text: str,
    *,
    section_id: str = "",
    locked_spans: list[str] | None = None,
) -> list[VerifyIssue]:
    """Flag never-say violations. Locked verbatim spans are exempt from accredited."""
    issues: list[VerifyIssue] = []
    body = text or ""
    locked_spans = locked_spans or []

    # Strip locked spans before some checks so their internal wording doesn't trip rules.
    scrubbed = body
    for span in locked_spans:
        if span and span in scrubbed:
            scrubbed = scrubbed.replace(span, " ")

    if _BUSINESS_SCHOOL.search(scrubbed):
        issues.append(
            VerifyIssue(
                "never_say",
                "Never say 'business school' / 'B-school'.",
                section_id=section_id,
            )
        )

    # MU/MUU: allow MU Ventures by masking it first.
    mu_probe = _MU_VENTURES_OK.sub(" ", scrubbed)
    if _MU_ABBREV.search(mu_probe):
        issues.append(
            VerifyIssue(
                "never_say",
                "Never say MU/MUU; use Masters' Union University (or 'the university').",
                section_id=section_id,
            )
        )

    if _YOUNGEST.search(scrubbed):
        issues.append(
            VerifyIssue(
                "never_say",
                "Never claim youngest / four-and-a-half-years university status.",
                section_id=section_id,
            )
        )

    if _FIRST_PRACTITIONER.search(scrubbed):
        issues.append(
            VerifyIssue(
                "never_say",
                "Never say 'first practitioner-led university'.",
                section_id=section_id,
            )
        )

    for match in _ACCREDITED.finditer(scrubbed):
        start = max(0, match.start() - 60)
        end = min(len(scrubbed), match.end() + 60)
        window = scrubbed[start:end]
        if _ACCREDITED_NEGATION.search(window):
            continue
        issues.append(
            VerifyIssue(
                "never_say",
                "Never say 'accredited' for memberships (EFMD/AACSB/BGA/BSIS/NSDC).",
                section_id=section_id,
                quote=match.group(0),
            )
        )
        break

    if _ENDORSED.search(scrubbed):
        issues.append(
            VerifyIssue(
                "never_say",
                "Never say 'endorsed' for a ministerial visit.",
                section_id=section_id,
            )
        )

    # Average without median in same or adjacent sentence.
    sentences = _sentences(scrubbed)
    for index, sentence in enumerate(sentences):
        if not _AVERAGE.search(sentence):
            continue
        window = " ".join(
            sentences[max(0, index - 1) : min(len(sentences), index + 2)]
        )
        if not _MEDIAN.search(window):
            issues.append(
                VerifyIssue(
                    "never_say",
                    "Average package must be accompanied by the median in the same or adjacent sentence.",
                    section_id=section_id,
                    quote=sentence[:160],
                )
            )
            break

    return issues


def _normalize_number(token: str) -> str:
    text = token.lower().strip().replace(",", "")
    text = re.sub(r"\s+", " ", text)
    return text


def extract_numbers(text: str) -> list[str]:
    found: list[str] = []
    for match in _NUMBER_TOKEN.finditer(text or ""):
        token = match.group(0).strip()
        if not token:
            continue
        # Skip lone connector words without digits.
        if token.lower() in {"one", "a", "an"} and not re.search(r"\d", token):
            # Keep "one hundred", "two and a half thousand" etc. via multi-word matches.
            if " " not in token and "-" not in token:
                continue
        found.append(token)
    return found


def check_number_traceability(
    text: str,
    *,
    allowed_texts: list[str],
    section_id: str = "",
) -> list[VerifyIssue]:
    """Every number in text must appear in a card, locked line, or plan text."""
    issues: list[VerifyIssue] = []
    allow_blob = " ".join(_normalize_number(t) for t in allowed_texts)
    for token in extract_numbers(text):
        norm = _normalize_number(token)
        if not norm:
            continue
        # Single small spelled numbers used as grammar ("one sentence") — skip if
        # no digit and length is a lone small word.
        if norm in {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"}:
            continue
        if norm not in allow_blob and not any(
            norm in _normalize_number(chunk) for chunk in allowed_texts
        ):
            issues.append(
                VerifyIssue(
                    "untraceable_number",
                    f"Number '{token}' is not traceable to a card, locked line, or plan.",
                    section_id=section_id,
                    quote=token,
                )
            )
    return issues


def check_locked_integrity(
    text: str,
    locked: dict[str, str],
) -> list[VerifyIssue]:
    issues: list[VerifyIssue] = []
    leftovers = remaining_placeholders(text)
    for lock_id in leftovers:
        issues.append(
            VerifyIssue(
                "lock_placeholder",
                f"Unresolved locked placeholder {{{{LOCK:{lock_id}}}}}",
            )
        )
    for lock_id, verbatim in locked.items():
        if not verbatim:
            continue
        if verbatim not in (text or ""):
            # Soft: only fail if a placeholder was supposed to insert it. Presence
            # after substitution is required when the plan requested the lock.
            pass
        # Detect near-miss alterations: same start but different body.
        # If the lock text appears with a small edit nearby, flag it.
        compact = re.sub(r"\s+", " ", verbatim).strip()
        body_compact = re.sub(r"\s+", " ", text or "")
        if compact and compact not in body_compact:
            # Only flag when the lock id was referenced in the draft path — caller
            # passes only locks that were requested.
            issues.append(
                VerifyIssue(
                    "lock_altered",
                    f"Locked line {lock_id} missing or altered after substitution.",
                    quote=verbatim[:120],
                )
            )
    return issues


def verify_script(
    script: dict[str, Any],
    *,
    locked: dict[str, str],
    cards_by_section: dict[str, list[dict[str, Any]]],
    plan_by_section: dict[str, dict[str, Any]],
    word_budget: int = 0,
    requested_lock_ids: set[str] | None = None,
) -> VerifyReport:
    issues: list[VerifyIssue] = []
    requested = requested_lock_ids if requested_lock_ids is not None else set(locked)
    total_words = 0

    for section in script.get("sections") or []:
        text = str(section.get("text") or "")
        section_key = str(
            section.get("ms_section_id")
            or section.get("heading")
            or section.get("topic_id")
            or ""
        )
        total_words += len(text.split())

        leftovers = remaining_placeholders(text)
        for lock_id in leftovers:
            issues.append(
                VerifyIssue(
                    "lock_placeholder",
                    f"Unresolved locked placeholder {{{{LOCK:{lock_id}}}}}",
                    section_id=section_key,
                )
            )

        locked_spans = [
            locked[lid]
            for lid in requested
            if lid in locked and locked[lid] and locked[lid] in text
        ]
        issues.extend(
            check_never_say(text, section_id=section_key, locked_spans=locked_spans)
        )

        allowed: list[str] = []
        for lid in requested:
            if lid in locked:
                allowed.append(locked[lid])
        for card in cards_by_section.get(section_key) or []:
            allowed.append(str(card.get("claim") or ""))
            allowed.append(str(card.get("figure") or ""))
            allowed.append(str(card.get("figure_label") or ""))
        plan = plan_by_section.get(section_key) or {}
        allowed.append(str(plan.get("the_one_thing") or ""))
        for premise in plan.get("premises") or []:
            allowed.append(str(premise))
        for cue in plan.get("script_cues") or []:
            if isinstance(cue, dict):
                allowed.append(str(cue.get("cue") or ""))
            else:
                allowed.append(str(cue))
        # Numbers from cards/locks/plan — check the spoken text excluding locked spans
        # so locked verbatim numbers are auto-allowed via allowed list.
        issues.extend(
            check_number_traceability(
                text, allowed_texts=allowed, section_id=section_key
            )
        )

        for lid in requested:
            if lid not in locked:
                continue
            verbatim = locked[lid]
            if verbatim and verbatim not in text:
                # Only require locks that belong to this section.
                if lid.upper().startswith(f"S{section_key}".upper()) or any(
                    lid == str(item.get("id") or "")
                    for item in (plan.get("locked_slots") or [])
                    if isinstance(item, dict)
                ):
                    issues.append(
                        VerifyIssue(
                            "lock_altered",
                            f"Locked line {lid} missing or altered.",
                            section_id=section_key,
                            quote=verbatim[:120],
                        )
                    )

    if word_budget and total_words > int(word_budget * 1.15):
        issues.append(
            VerifyIssue(
                "word_budget",
                f"Script is {total_words} words vs budget {word_budget}.",
            )
        )

    return VerifyReport(passed=not issues, issues=issues)
