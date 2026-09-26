"""How Masters' Union makes claims — brief rules, validators, fact lint.

Source of truth for claim hygiene across scripts, slide copy, and stored facts.
Voice/personality lives elsewhere; this module is WHAT is said and HOW claims
are stated, not how they sound when spoken.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any, Iterable, Sequence

# Brief Part 4 + Section 08. Hyphen/space variants match via normalisation.
BANNED_CLAIM_PHRASES: tuple[str, ...] = (
    "world-class",
    "cutting-edge",
    "transformative",
    "learn by doing",
    "reimagining business education",
    "industry-immersive",
    "unparalleled",
    "india's most innovative",
    "beat the iims",
    "guaranteed placement",
)

# Trailing '+' / 'plus' as a rounded-up aggregate. Not C++, +91, or 2+2.
ROUNDED_AGGREGATE_RE = re.compile(
    r"(?<![A-Za-z.+])"
    r"(?:₹\s*)?"
    r"\d+(?:\.\d+)?"
    r"(?:\s*(?:Cr|crore|LPA|lakh|lakhs?|k))?"
    r"(?:"
    r"\s*\+(?!\+|\d)"
    r"(?:\s*(?:LPA|lakh|lakhs?|Cr|crore|students?|recruiters?|CXOs?|"
    r"offers?|companies|alumni|faculty|mentors?))?"
    r"|"
    r"\s+plus\s+(?:students?|recruiters?|CXOs?|offers?|companies|"
    r"alumni|LPA|lakh|lakhs?|Cr|crore|faculty|mentors?)\b"
    r")",
    re.IGNORECASE,
)

_AVERAGE_PAY_RE = re.compile(
    r"(?:"
    r"\b(?:average|avg)\b.{0,25}\b(?:ctc|salary|salaries|package|pay|placement|lpa)\b"
    r"|"
    r"\b(?:ctc|salary|package)\b.{0,25}\b(?:average|avg)\b"
    r"|"
    r"\b33\.39\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_FIGURE_RE = re.compile(r"\d|\b(?:lakhs?|lpa|crores?)\b", re.IGNORECASE)
_MEDIAN_RE = re.compile(r"\bmedian\b|\b27\.78\b", re.IGNORECASE)

_USABLE_FACT_STATUSES = frozenset({"verified"})

CLAIMS_RULES_PROMPT = (
    "HOW WE MAKE CLAIMS (from the Masters' Union communication brief):\n"
    "1. Exact figures only — never round up (₹33.39 LPA, not ₹33+ LPA; no '60Cr+', '250+'). "
    "Numbers still come only from LOCKED facts / REPORT EVIDENCE; this is how they are stated.\n"
    "2. Median beside average — if you mention average CTC/pay, the median must sit next to it "
    "in the same section/slide (e.g. average ₹33.39 LPA, median ₹27.78 LPA).\n"
    "3. Disclose one honest weakness once in longer pitches — only a limitation supported by "
    "LOCKED/REPORT (never invent one). Frame: we'd rather you knew / you should check it.\n"
    "4. Named proof over aggregates — a named person/venture with a checkable outcome beats a "
    "round total. Lead with named proof where LOCKED/REPORT/REAL STORIES give it.\n"
    "Proof ladder (strong→weak): named person > third party with nothing to gain > exact number "
    "with source > round aggregate (never).\n"
    "Banned claim phrases: world-class, cutting-edge, transformative, learn by doing, "
    "reimagining business education, industry-immersive, unparalleled, India's most innovative, "
    "beat the IIMs, guaranteed placement — and any average without its median.\n"
)

SLIDE_CLAIMS_RULES_PROMPT = (
    "HOW WE MAKE CLAIMS: exact figures only, never rounded up with '+' (₹33.39 LPA, not ₹33+ LPA); "
    "an average CTC/pay figure always has the median on the same slide; named person or venture "
    "with a checkable outcome over round aggregates. Banned: world-class, cutting-edge, "
    "transformative, learn by doing, reimagining business education, industry-immersive, "
    "unparalleled, India's most innovative, beat the IIMs, guaranteed placement. "
)

DISCLOSURE_RULE = (
    "DISCLOSURE (pitches of ~5 minutes and longer):\n"
    "Name one honest limitation once, plainly, without apology — ONLY a disclosure supported by "
    "LOCKED facts / REPORT EVIDENCE (e.g. median below average; CTC exclusions; the certification/"
    "university-status line, which must still be said word for word). Never invent a weakness. "
    "Frame it as 'we'd rather you knew now' or 'you should check it'.\n"
)

CALIBRATION_CLAIM_LINE = (
    "Average is 33.39 lakh. But look at the median, right? 27.78. "
    "We publish both, because boss, you should check it."
)


def _normalize_for_ban(text: str) -> str:
    lowered = (text or "").lower().replace("\u2019", "'")
    return re.sub(r"[^a-z0-9']+", " ", lowered).strip()


def _banned_hits(text: str) -> list[str]:
    haystack = f" {_normalize_for_ban(text)} "
    hits: list[str] = []
    for phrase in BANNED_CLAIM_PHRASES:
        needle = f" {_normalize_for_ban(phrase)} "
        if needle in haystack:
            hits.append(phrase)
    return hits


def average_without_median(text: str) -> bool:
    """True when pay-average (or 33.39) appears without median / 27.78 in the same unit."""
    blob = text or ""
    if _MEDIAN_RE.search(blob):
        return False
    return any(
        _AVERAGE_PAY_RE.search(sentence) and _FIGURE_RE.search(sentence)
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", blob)
    )


def claim_violations(text: str, *, unit_label: str) -> list[str]:
    """Human-readable claim violations for rewrite loops / slide gates."""
    blob = (text or "").strip()
    if not blob:
        return []
    violations: list[str] = []
    for phrase in _banned_hits(blob):
        violations.append(
            f'{unit_label} uses banned claim "{phrase}" — replace it with a named person, '
            "number or checkable fact"
        )
    match = ROUNDED_AGGREGATE_RE.search(blob)
    if match:
        violations.append(
            f'{unit_label} rounds a figure up ("{match.group(0).strip()}") — use the exact '
            "figure from LOCKED facts or drop it"
        )
    if average_without_median(blob):
        violations.append(
            f"{unit_label} quotes the average CTC without the median — say both: "
            "average ₹33.39 LPA, median ₹27.78 LPA"
        )
    return violations


def is_average_ctc_fact(fact: Any) -> bool:
    blob = f"{getattr(fact, 'fact', '') or ''} {getattr(fact, 'value', '') or ''}"
    return bool(_AVERAGE_PAY_RE.search(blob))


def is_median_ctc_fact(fact: Any) -> bool:
    blob = f"{getattr(fact, 'fact', '') or ''} {getattr(fact, 'value', '') or ''}"
    if re.search(r"\b27\.78\b", blob):
        return True
    name = (getattr(fact, "fact", "") or "").strip().lower()
    if "median" in name and re.search(r"\b(?:ctc|salary|package|lpa|lakh)\b", name):
        return True
    return bool(re.search(r"\bmedian\b", blob, re.IGNORECASE) and re.search(
        r"\b(?:ctc|salary|package|lpa|lakh)\b", blob, re.IGNORECASE
    ))


ROUNDED_FACT_NOTE = (
    "rounded figure: never write 'N+'; say 'more than N' or prove it with a named example"
)


def rounded_fact_note(fact: Any) -> str:
    return ROUNDED_FACT_NOTE if ROUNDED_AGGREGATE_RE.search(getattr(fact, "value", "") or "") else ""


def fact_is_usable(fact: Any) -> bool:
    return (getattr(fact, "status", "") or "").strip() in _USABLE_FACT_STATUSES


def pair_median_facts(
    selected: Sequence[Any],
    pool: Sequence[Any] | None = None,
) -> list[Any]:
    """If selected includes an average-CTC fact, ensure a usable median fact is present too."""
    result = list(selected)
    if not any(is_average_ctc_fact(fact) for fact in result):
        return result
    if any(is_median_ctc_fact(fact) for fact in result):
        return result
    search = list(pool) if pool is not None else result
    for fact in search:
        if fact_is_usable(fact) and is_median_ctc_fact(fact):
            if fact not in result:
                result.append(fact)
            break
    return result


def disclosure_applies(duration: str) -> bool:
    """True for pitches of ~5 minutes and longer (T2+)."""
    from backend.pipeline.resolver import script_minutes_for_duration

    return script_minutes_for_duration(duration) >= 5


def lint_facts(db: Any) -> list[dict[str, Any]]:
    """Flag stored facts with banned phrases, rounded aggregates, or unpaired averages."""
    from backend.models import LockedFact

    rows: list[LockedFact] = db.query(LockedFact).order_by(LockedFact.id).all()
    has_median_fact = any(fact_is_usable(row) and is_median_ctc_fact(row) for row in rows)
    problems: list[dict[str, Any]] = []
    for row in rows:
        blob = f"{row.fact or ''} {row.value or ''}".strip()
        for phrase in _banned_hits(blob):
            problems.append(
                {
                    "id": row.id,
                    "fact": row.fact,
                    "problem": f'banned claim phrase "{phrase}"',
                }
            )
        match = ROUNDED_AGGREGATE_RE.search(blob)
        if match:
            problems.append(
                {
                    "id": row.id,
                    "fact": row.fact,
                    "problem": f'rounded aggregate "{match.group(0).strip()}"',
                }
            )
        if average_without_median(blob) and not has_median_fact:
            problems.append(
                {
                    "id": row.id,
                    "fact": row.fact,
                    "problem": "average CTC/pay with no usable median fact to pair it with",
                }
            )
    return problems


def _print_lint(problems: Iterable[dict[str, Any]]) -> int:
    rows = list(problems)
    if not rows:
        print("No claim-hygiene issues in locked facts.")
        return 0
    for row in rows:
        print(f"{row['id']}\t{row['fact']}\t{row['problem']}")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("lint", help="Flag locked facts with claim-hygiene problems (no edits)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command != "lint":
        parser.print_help()
        return 2
    from backend.database import SessionLocal, ensure_schema

    ensure_schema()
    db = SessionLocal()
    try:
        return _print_lint(lint_facts(db))
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
