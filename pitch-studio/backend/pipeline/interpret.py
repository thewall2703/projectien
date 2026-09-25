from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Recipe
from backend.pipeline.llm import LLMError, chat_json
from backend.schemas import (
    AUDIENCE_CLUSTERS,
    CHANNELS,
    DURATIONS,
    INTENTS,
    TEMPERATURES,
    AxisOption,
    InterpretPersonaCandidate,
    InterpretRequest,
    InterpretResult,
)
from backend.pipeline.vision_deck import USE_CASES, USE_CASE_KEYS, normalize_use_case, use_case_for_persona

# Auto-select the highest-confidence persona when it is at least this strong.
PERSONA_AUTO_MIN_CONFIDENCE = 0.7
PERSONA_CANDIDATE_LIMIT = 2

VALID_AUDIENCE = {item.code for item in AUDIENCE_CLUSTERS}
VALID_DURATION = {item.code for item in DURATIONS}
VALID_CHANNEL = {item.code for item in CHANNELS}
VALID_INTENT = {item.code for item in INTENTS}
VALID_TEMPERATURE = {item.code for item in TEMPERATURES}
VALID_DECK_USE_CASE = set(USE_CASE_KEYS)


class InterpretError(RuntimeError):
    """Raised when the LLM response cannot be mapped to valid axes."""


def _format_catalog(title: str, options: list[AxisOption]) -> str:
    lines = [f"{title}:"]
    for option in options:
        lines.append(f"- {option.code}: {option.label} — {option.description}")
    return "\n".join(lines)


def _format_deck_use_cases() -> str:
    lines = [
        "Deck use cases (only for admissions / career-fair / parent pitches that map "
        "to the Brand Deck vision sheet; empty string for recruiters, investors, faculty, etc.):"
    ]
    for key, label in USE_CASES:
        lines.append(f"- {key}: {label}")
    lines.append("- (empty string): no vision mapping — use the legacy module recipe")
    return "\n".join(lines)


def _format_recipes(recipes: list[Recipe]) -> str:
    if not recipes:
        return "Personas / recipes: (none)"
    lines = ["Personas / recipes (ref | audience_label | audience_cluster | duration | channel | intent):"]
    for recipe in recipes:
        lines.append(
            f"- {recipe.ref} | {recipe.audience_label} | {recipe.audience_cluster} | "
            f"{recipe.duration} | {recipe.channel} | {recipe.intent}"
        )
    return "\n".join(lines)


def build_interpret_messages(
    audience_text: str,
    setting_text: str,
    goal_text: str,
    recipes: list[Recipe],
) -> list[dict[str, str]]:
    catalogs = "\n\n".join(
        [
            _format_catalog("Audience clusters", AUDIENCE_CLUSTERS),
            _format_catalog("Durations", DURATIONS),
            _format_catalog("Channels", CHANNELS),
            _format_catalog("Intents", INTENTS),
            _format_catalog("Temperatures", TEMPERATURES),
            _format_deck_use_cases(),
            _format_recipes(recipes),
        ]
    )
    system = (
        "You map plain-language pitch briefs to internal axis codes for Masters' Union Pitch Studio. "
        "Return strict JSON only with these keys: audience_cluster, duration, channel, intent, "
        "temperature, recipe_ref, deck_use_case, persona_candidates, summary, notes. "
        "Choose codes only from the catalogs provided. "
        "Duration and channel MUST follow the Setting text first. Pick the nearest existing duration "
        "code only: T0=30s, T1=2m, T2=5m, T3=10m, T4=30m, T5=90m. There is no 60-minute code — "
        "map 'about an hour' / 45-75 minutes to T5 (90 minutes), never invent a length. "
        "deck_use_case must be one of the listed keys when the audience is school students at a "
        "career fair, PG/Exec aspirants at a fair, PGP SMG, UG TBM, UG DSAI, or parents "
        "(undecided vs decided). Temperature X4 or X5 for parents leans toward parents_decided; "
        "X1–X3 for parents leans toward parents_undecided. "
        "For recruiters, investors, faculty, employees, press, government, or any audience that "
        "is not one of those seven, set deck_use_case to an empty string. "
        "persona_candidates must be an array of the 1-2 closest personas from the recipe catalog, "
        "each as {recipe_ref, confidence, rationale}. confidence is a number from 0 to 1 for how "
        "well that persona matches Who they are pitching to (audience fit only — ignore that the "
        "persona’s stored duration/channel may differ from Setting). Prefer distinct audience labels "
        "when two personas are close. If nothing is remotely close, return an empty array. "
        "Set recipe_ref to the top candidate’s ref when that match is clear; otherwise set "
        "recipe_ref to an empty string (the server may still use the scores). A matched persona must "
        "NOT override the duration/channel implied by Setting; keep those from the Setting text. "
        "summary must be one plain-English business sentence describing the pitch that will be written. "
        "notes holds leftover specifics to fold into a context note (empty string if none)."
    )
    user = (
        f"{catalogs}\n\n"
        "User brief:\n"
        f"- Who they are pitching to: {audience_text.strip() or '(not provided)'}\n"
        f"- Setting: {setting_text.strip() or '(not provided)'}\n"
        f"- What they want from it: {goal_text.strip() or '(not provided)'}\n\n"
        "Respond with JSON matching InterpretResult."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _clamp_confidence(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score != score:  # NaN
        return None
    return max(0.0, min(1.0, score))


def _normalize_persona_candidates(raw: Any, known_refs: set[str]) -> list[InterpretPersonaCandidate]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    candidates: list[InterpretPersonaCandidate] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("recipe_ref") or "").strip()
        if not ref or ref not in known_refs or ref in seen:
            continue
        confidence = _clamp_confidence(item.get("confidence"))
        if confidence is None:
            continue
        seen.add(ref)
        candidates.append(
            InterpretPersonaCandidate(
                recipe_ref=ref,
                confidence=confidence,
                rationale=str(item.get("rationale") or "").strip(),
            )
        )
        if len(candidates) >= PERSONA_CANDIDATE_LIMIT:
            break
    candidates.sort(key=lambda row: row.confidence, reverse=True)
    return candidates


def _pick_recipe_ref(recipe_ref: str, candidates: list[InterpretPersonaCandidate], known_refs: set[str]) -> str:
    if candidates:
        top = candidates[0]
        if top.confidence >= PERSONA_AUTO_MIN_CONFIDENCE:
            return top.recipe_ref
        return ""
    if recipe_ref and recipe_ref in known_refs:
        return recipe_ref
    return ""


def normalize_interpret_payload(
    raw: dict[str, Any],
    known_refs: set[str],
    persona_labels: dict[str, str] | None = None,
) -> InterpretResult:
    audience = str(raw.get("audience_cluster") or "").strip()
    duration = str(raw.get("duration") or "").strip()
    channel = str(raw.get("channel") or "").strip()
    intent = str(raw.get("intent") or "").strip()
    temperature = str(raw.get("temperature") or "").strip()
    recipe_ref = str(raw.get("recipe_ref") or "").strip()
    deck_use_case = normalize_use_case(str(raw.get("deck_use_case") or "").strip())
    summary = str(raw.get("summary") or "").strip()
    notes = str(raw.get("notes") or "").strip()

    invalid: list[str] = []
    if audience not in VALID_AUDIENCE:
        invalid.append(f"audience_cluster={audience!r}")
    if duration not in VALID_DURATION:
        invalid.append(f"duration={duration!r}")
    if channel not in VALID_CHANNEL:
        invalid.append(f"channel={channel!r}")
    if intent not in VALID_INTENT:
        invalid.append(f"intent={intent!r}")
    if temperature not in VALID_TEMPERATURE:
        invalid.append(f"temperature={temperature!r}")
    if invalid:
        raise InterpretError(f"Interpreter returned invalid axis codes: {', '.join(invalid)}")

    candidates = _normalize_persona_candidates(raw.get("persona_candidates"), known_refs)
    if recipe_ref and recipe_ref in known_refs and not any(c.recipe_ref == recipe_ref for c in candidates):
        # Preserve an explicit clear match even if the model omitted the score array.
        candidates = [
            InterpretPersonaCandidate(recipe_ref=recipe_ref, confidence=1.0, rationale=""),
            *candidates,
        ][:PERSONA_CANDIDATE_LIMIT]
    recipe_ref = _pick_recipe_ref(recipe_ref, candidates, known_refs)
    if persona_labels is not None and recipe_ref:
        deck_use_case = use_case_for_persona(
            persona_labels.get(recipe_ref, ""),
            temperature=temperature,
        )
    elif temperature in {"X4", "X5"} and deck_use_case == "parents_undecided":
        deck_use_case = "parents_decided"

    return InterpretResult(
        audience_cluster=audience,
        duration=duration,
        channel=channel,
        intent=intent,
        temperature=temperature,
        recipe_ref=recipe_ref,
        deck_use_case=deck_use_case if deck_use_case in VALID_DECK_USE_CASE else "",
        persona_candidates=candidates,
        summary=summary,
        notes=notes,
    )


def interpret_brief(db: Session, payload: InterpretRequest) -> InterpretResult:
    recipes = db.query(Recipe).order_by(Recipe.ref).all()
    messages = build_interpret_messages(
        payload.audience_text,
        payload.setting_text,
        payload.goal_text,
        recipes,
    )
    try:
        # Axis mapping is a small classification task. Keep it off the slower
        # script-writing model and disable reasoning so review appears quickly.
        raw = chat_json(
            messages,
            timeout=15.0,
            model=settings.openrouter_interpret_model,
            reasoning=False,
            max_tokens=500,
        )
    except (LLMError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise InterpretError(f"Could not interpret brief: {exc}") from exc
    if not isinstance(raw, dict):
        raise InterpretError("Interpreter did not return a JSON object")
    known_refs = {recipe.ref for recipe in recipes}
    return normalize_interpret_payload(
        raw,
        known_refs,
        {recipe.ref: recipe.audience_label or "" for recipe in recipes},
    )
