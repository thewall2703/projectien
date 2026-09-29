"""Load the versioned Master Script JSON and resolve audience/duration routes.

Matcher policy (best-effort):
1. Match ``deck_use_case`` codes and persona/context keywords to a routing row.
2. If duration is very short (T0–T3), force the Cutdown length even when a Lean
   audience route is chosen — shorten by dropping whole sections in Master
   Script order.
3. Fall back to the Standard pitch route when nothing matches.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.config import DATA_DIR
from backend.master_script import MASTER_SCRIPT_VERSION

MASTER_SCRIPT_DIR = DATA_DIR / "master_script"
DEFAULT_JSON_PATH = MASTER_SCRIPT_DIR / f"master_script.{MASTER_SCRIPT_VERSION}.json"

# Canonical section order used when dropping for time.
SECTION_ORDER = [
    "1",
    "2",
    "3",
    "4A",
    "4B",
    "4C",
    "4D",
    "4E",
    "4F",
    "5",
    "6",
    "7",
]

# Duration → length profile. Matches the Master Script's Full / Standard / Cutdown.
DURATION_LENGTH = {
    "T0": "cutdown",
    "T1": "cutdown",
    "T2": "cutdown",
    "T3": "cutdown",
    "T4": "standard",
    "T5": "full",
}

CUTDOWN_KEEP = {"1", "3", "5", "7"}
CUTDOWN_KEEP_SCEPTICAL = {"1", "2", "3", "5", "7"}

# Built-in routing rows used when the JSON is missing or incomplete.
_BUILTIN_ROUTES: list[dict[str, Any]] = [
    {
        "id": "school_students",
        "audience": "School students, Class 11 to 12 (career fairs, counselling seminars)",
        "seeking": "Who are you, why do I care, what would I actually do every day",
        "lean": ["1", "3", "4A", "4C", "4D", "6", "7"],
        "compress": ["2", "5"],
        "skip": ["4E", "4F"],
        "open_with": (
            "Doctors learn in hospitals. Where do you think people learn business?"
        ),
        "ask": "Come and stand in it: open day on [date], register at [link]",
        "keywords": [
            "school",
            "class 11",
            "class 12",
            "career fair",
            "school_fair",
        ],
    },
    {
        "id": "ug_students",
        "audience": "Prospective UG students, warm or hot (info sessions, campus visits)",
        "seeking": "Will I get in, what happens in term one, is this only for founders",
        "lean": ["1", "3", "4A", "4B", "4C", "4D", "5", "6", "7"],
        "compress": ["2"],
        "skip": [],
        "open_with": (
            "In your first term here you will run a store with real money. "
            "Let me show you what that looks like."
        ),
        "ask": "Apply by [date]; a current student will call you this week",
        "keywords": ["ug", "tbm", "dsai", "ug_tbm", "ug_dsai", "undergraduate"],
    },
    {
        "id": "parents_undecided",
        "audience": "Parents of UG aspirants, undecided",
        "seeking": "Is this real, or ₹40 lakh for a bootcamp; what could go wrong",
        "lean": ["2", "4A", "4B", "5", "7"],
        "compress": ["3", "6"],
        "skip": ["4D"],
        "open_with": (
            "I will start with what this institution is, and then I will show you "
            "the numbers with the method attached."
        ),
        "ask": "A campus visit on [date]. Bring the questions you did not ask today",
        "keywords": ["parents_undecided", "parent", "undecided"],
    },
    {
        "id": "parents_decided",
        "audience": "Parents, decided or committed (offer stage, family scrutiny)",
        "seeking": "The words to defend the choice to relatives",
        "lean": ["1", "2", "5", "7"],
        "compress": ["3", "4A", "4B", "4C", "4D", "4E", "4F", "6"],
        "skip": [],
        "open_with": (
            "You have decided. My job today is to give you three sentences you "
            "can say at dinner."
        ),
        "ask": "Join the parents' group at [link]; the first-term calendar arrives on [date]",
        "keywords": ["parents_decided", "decided", "committed", "offer"],
    },
    {
        "id": "pg_students",
        "audience": "Prospective PG students, 21 to 29, including working professionals",
        "seeking": "Better than the MBA I am re-attempting; will I switch function",
        "lean": ["3", "4B", "4C", "4E", "5", "7"],
        "compress": ["1", "2", "6"],
        "skip": [],
        "open_with": (
            "Learn business by running a P&L, and finish on an audited placement "
            "record. Both halves matter."
        ),
        "ask": "Apply by [date]. Admissions will call within 48 hours",
        "keywords": [
            "pg",
            "pgp",
            "mba",
            "working professional",
            "pg_exec_fair",
            "pgp_smg",
        ],
    },
    {
        "id": "executives",
        "audience": "Executives 30 to 45 and family business promoters",
        "seeking": "Worth my weekends; peers; applied to my own business",
        "lean": ["2", "4B", "4F", "7"],
        "compress": ["1", "3"],
        "skip": ["4C", "4D", "6"],
        "open_with": (
            "Nobody in this room needs a first job. So this is about who you "
            "would give up a Saturday for."
        ),
        "ask": "A call with a current cohort member on [date]",
        "keywords": ["executive", "family business", "promoter", "owners"],
    },
    {
        "id": "investors",
        "audience": "Financiers: investors, lenders, MU Ventures co-investors",
        "seeking": "Does it scale, is it defensible, is the deal flow real",
        "lean": ["2", "4B", "4E", "5", "7"],
        "compress": [],
        "skip": ["6"],
        "open_with": (
            "Every number I show you today has its method beside it. Anything "
            "without one, I will not show you."
        ),
        "ask": "Data room access by [date]; second meeting with Finance on [date]",
        "keywords": ["investor", "lender", "ventures", "financier", "i3"],
    },
    {
        "id": "press_adversarial",
        "audience": "Press, adversarial",
        "seeking": "What are you hiding",
        "lean": ["5", "2", "7"],
        "compress": [],
        "skip": ["6"],
        "open_with": (
            "Ask me the question directly and I will answer it directly. I will "
            "start with the placement methodology."
        ),
        "ask": "The audited report and a named spokesperson by [time]",
        "keywords": ["adversarial", "press", "sceptical", "skeptical", "i6"],
    },
    {
        "id": "press_friendly",
        "audience": "Press, friendly or feature",
        "seeking": "A story, not an institution",
        "lean": ["3", "4C", "1", "5", "7"],
        "compress": ["2"],
        "skip": ["6"],
        "open_with": (
            "An 18-year-old from this campus pitched on Shark Tank India while "
            "enrolled. Start there."
        ),
        "ask": "A campus day with two students, on [date]",
        "keywords": ["press", "media", "feature", "journalist"],
    },
    {
        "id": "standard_pitch",
        "audience": "Standard pitch",
        "seeking": "A complete case in a short slot",
        "lean": list(SECTION_ORDER),
        "compress": [],
        "skip": [],
        "open_with": (
            "Masters' Union University is a practitioner-led university in "
            "Gurugram, where students learn business and technology by building "
            "real businesses."
        ),
        "ask": "The next step is yours: apply, visit, or take the data room.",
        "keywords": ["standard", "default", "career fair"],
    },
]


@dataclass
class RoutePlan:
    route_id: str
    audience_label: str
    length: str
    sections: list[str]
    compress: list[str]
    skip: list[str]
    open_with: str
    ask: str
    seeking: str = ""
    match_reason: str = ""


@dataclass
class MasterScriptDoc:
    version: str
    sections: dict[str, dict[str, Any]]
    routing: list[dict[str, Any]]
    never_say: list[str]
    numbers_you_may_say: str
    objection_bank: list[dict[str, Any]]
    delivery_notes: str
    open_items: list[str]
    raw: dict[str, Any] = field(default_factory=dict)

    def section(self, section_id: str) -> dict[str, Any] | None:
        return self.sections.get(section_id)


def master_script_path() -> Path:
    return DEFAULT_JSON_PATH


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def load_master_script(path: Path | None = None) -> MasterScriptDoc:
    target = path or DEFAULT_JSON_PATH
    if not target.exists():
        return _builtin_doc()
    raw = json.loads(target.read_text(encoding="utf-8"))
    sections: dict[str, dict[str, Any]] = {}
    for item in raw.get("sections") or []:
        sid = str(item.get("id") or "").strip()
        if sid:
            sections[sid] = item
    routing = list(raw.get("routing") or raw.get("audience_routes") or [])
    if not routing:
        routing = list(_BUILTIN_ROUTES)
    return MasterScriptDoc(
        version=str(raw.get("version") or MASTER_SCRIPT_VERSION),
        sections=sections,
        routing=routing,
        never_say=list(raw.get("never_say") or []),
        numbers_you_may_say=str(raw.get("numbers_you_may_say") or ""),
        objection_bank=list(raw.get("objection_bank") or []),
        delivery_notes=str(raw.get("delivery_notes") or ""),
        open_items=[str(x) for x in (raw.get("open_items") or [])],
        raw=raw,
    )


def _builtin_doc() -> MasterScriptDoc:
    sections = {
        sid: {
            "id": sid,
            "title": sid,
            "slides": [],
            "the_one_thing": "",
            "premises": [],
            "script_cues": [],
            "locked": [],
            "blocked": [],
            "modulate": "",
            "expect": [],
        }
        for sid in SECTION_ORDER
    }
    # Minimal slide ranges from the Master Script.
    ranges = {
        "1": list(range(1, 9)),
        "2": list(range(9, 12)),
        "3": list(range(12, 19)),
        "4A": list(range(19, 22)),
        "4B": list(range(22, 29)),
        "4C": list(range(29, 37)),
        "4D": list(range(37, 43)),
        "4E": list(range(43, 51)),
        "4F": list(range(51, 55)),
        "5": list(range(55, 65)),
        "6": list(range(65, 81)),
        "7": list(range(81, 91)),
    }
    for sid, slides in ranges.items():
        sections[sid]["slides"] = slides
    return MasterScriptDoc(
        version=MASTER_SCRIPT_VERSION,
        sections=sections,
        routing=list(_BUILTIN_ROUTES),
        never_say=[],
        numbers_you_may_say="",
        objection_bank=[],
        delivery_notes="",
        open_items=[],
        raw={},
    )


def _haystack(
    audience_cluster: str,
    intent: str,
    channel: str,
    persona: str,
    deck_use_case: str,
    context_note: str,
) -> str:
    return " ".join(
        part
        for part in (
            audience_cluster,
            intent,
            channel,
            persona,
            deck_use_case,
            context_note,
        )
        if part
    ).lower()


def _score_route(route: dict[str, Any], haystack: str, deck_use_case: str) -> int:
    score = 0
    route_id = str(route.get("id") or "").lower()
    if deck_use_case and deck_use_case.lower() == route_id:
        score += 50
    for keyword in route.get("keywords") or []:
        token = str(keyword).lower().strip()
        if not token:
            continue
        if token == deck_use_case.lower():
            score += 40
        elif token in haystack:
            score += 10 if len(token) > 3 else 4
    audience = str(route.get("audience") or "").lower()
    if audience and audience in haystack:
        score += 8
    return score


def match_route(
    doc: MasterScriptDoc,
    *,
    audience_cluster: str = "",
    intent: str = "",
    channel: str = "",
    persona: str = "",
    deck_use_case: str = "",
    context_note: str = "",
) -> dict[str, Any]:
    haystack = _haystack(
        audience_cluster, intent, channel, persona, deck_use_case, context_note
    )
    routes = doc.routing or _BUILTIN_ROUTES
    best: dict[str, Any] | None = None
    best_score = 0
    for route in routes:
        score = _score_route(route, haystack, deck_use_case)
        if score > best_score:
            best = route
            best_score = score
    if best is None or best_score <= 0:
        for route in routes:
            if str(route.get("id") or "") == "standard_pitch":
                return {**route, "_match_reason": "fallback:standard_pitch"}
        return {**_BUILTIN_ROUTES[-1], "_match_reason": "fallback:builtin_standard"}
    return {**best, "_match_reason": f"score:{best_score}"}


def _apply_length(sections: list[str], length: str, sceptical: bool) -> list[str]:
    selected = set(sections)
    if length == "full":
        return [sid for sid in SECTION_ORDER if sid in selected]
    if length == "standard":
        return [sid for sid in SECTION_ORDER if sid in selected]
    keep = CUTDOWN_KEEP_SCEPTICAL if sceptical else CUTDOWN_KEEP
    return [sid for sid in SECTION_ORDER if sid in keep]


def resolve_route(
    doc: MasterScriptDoc,
    *,
    audience_cluster: str = "",
    duration: str = "T2",
    channel: str = "",
    intent: str = "",
    persona: str = "",
    deck_use_case: str = "",
    context_note: str = "",
) -> RoutePlan:
    route = match_route(
        doc,
        audience_cluster=audience_cluster,
        intent=intent,
        channel=channel,
        persona=persona,
        deck_use_case=deck_use_case,
        context_note=context_note,
    )
    length = DURATION_LENGTH.get(duration, "standard")
    lean = [str(x) for x in (route.get("lean") or [])]
    compress = [str(x) for x in (route.get("compress") or [])]
    skip = [str(x) for x in (route.get("skip") or [])]

    # Start from lean ∪ compress (compress still present, just short).
    selected = [sid for sid in SECTION_ORDER if sid in set(lean) | set(compress)]
    selected = [sid for sid in selected if sid not in set(skip)]
    if not selected:
        selected = list(SECTION_ORDER)

    sceptical = any(
        token in _haystack(audience_cluster, intent, channel, persona, deck_use_case, context_note)
        for token in ("sceptic", "skeptic", "adversarial", "regulator", "accredit")
    )
    if length == "cutdown":
        keep = CUTDOWN_KEEP_SCEPTICAL if sceptical else CUTDOWN_KEEP
        selected = [sid for sid in SECTION_ORDER if sid in keep]
        # Drop compress/skip still apply on top of cutdown when route forbids a section.
        selected = [sid for sid in selected if sid not in set(skip)]
    elif length == "full":
        selected = [sid for sid in SECTION_ORDER if sid not in set(skip)]
    else:
        # standard: lean + compress, minus skip
        pass

    # V2 audience routes may deliberately bring outcomes ahead of the model.
    route_order = route.get("section_order") or SECTION_ORDER
    selected = [sid for sid in dict.fromkeys([*route_order, *SECTION_ORDER]) if sid in selected]

    # Final drop-whole-sections safety: if duration is tiny, keep only first N
    # cutdown sections in Master Script order.
    if duration in {"T0", "T1"} and len(selected) > 3:
        selected = selected[:3]

    return RoutePlan(
        route_id=str(route.get("id") or "standard_pitch"),
        audience_label=str(route.get("audience") or "Standard pitch"),
        length=length,
        sections=selected,
        compress=[sid for sid in compress if sid in selected],
        skip=skip,
        open_with=str(route.get("open_with") or ""),
        ask=str(route.get("ask") or ""),
        seeking=str(route.get("seeking") or ""),
        match_reason=str(route.get("_match_reason") or ""),
    )


def locked_lines_for_sections(
    doc: MasterScriptDoc, section_ids: list[str]
) -> dict[str, str]:
    """Return lock_id → verbatim text for the given sections."""
    out: dict[str, str] = {}
    for sid in section_ids:
        section = doc.section(sid) or {}
        for index, item in enumerate(section.get("locked") or [], start=1):
            if isinstance(item, dict):
                if item.get("pending_signoff"):
                    continue
                lock_id = str(item.get("id") or f"S{sid}.{index}")
                text = str(item.get("text") or "").strip()
            else:
                lock_id = f"S{sid}.{index}"
                text = str(item).strip()
            if text:
                out[lock_id] = text
    return out
