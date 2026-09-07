from __future__ import annotations

import re
from typing import Any

from backend.models import LockedFact

MEMBERSHIP_NAMES = ("EFMD", "AACSB", "BGA", "BSIS", "NSDC")
FORBIDDEN_STATUSES = {"conflict", "do_not_use", "needs_source", "needs_decision"}
AVERAGE_MARKER = "33.39"
MEDIAN_MARKER = "27.78"


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9₹.%]+", text)


def script_text(script: dict[str, Any]) -> str:
    parts = [section.get("text", "") for section in script.get("sections", [])]
    parts.append(script.get("cta", ""))
    return "\n".join(parts)


def validate_script(
    script: dict[str, Any],
    facts: list[LockedFact],
    sequence: list[str],
    word_budget: int,
) -> list[str]:
    violations: list[str] = []
    sections = script.get("sections") or []
    section_ids = [section.get("module_id", "") for section in sections]
    if section_ids != sequence:
        violations.append(
            f"Section module order {section_ids} does not match recipe {sequence}"
        )

    full = script_text(script)
    word_count = len(_words(full))
    low = int(word_budget * 0.85)
    high = int(word_budget * 1.15)
    if word_count < low or word_count > high:
        violations.append(
            f"Word count {word_count} is outside budget {word_budget} (±15%, {low}-{high})"
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
