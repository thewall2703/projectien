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
    body = "\n".join(section.get("text", "") for section in script.get("sections", []))
    cta = (script.get("cta") or "").strip()
    if cta and cta not in body:
        return f"{body}\n{cta}".strip()
    return body.strip()


def count_script_words(script: dict[str, Any]) -> int:
    return len(_words(script_text(script)))


def budget_range(word_budget: int) -> tuple[int, int]:
    return int(word_budget * 0.85), int(word_budget * 1.15)


def _shorten(text: str, max_words: int = 80) -> str:
    words = [part for part in text.split() if part]
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).strip()


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
    return {"sections": aligned, "cta": script.get("cta") or ""}


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
    word_count = count_script_words(script)
    low, high = budget_range(word_budget)
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
