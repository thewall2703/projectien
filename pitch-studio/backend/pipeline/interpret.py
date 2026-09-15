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
    InterpretRequest,
    InterpretResult,
)

VALID_AUDIENCE = {item.code for item in AUDIENCE_CLUSTERS}
VALID_DURATION = {item.code for item in DURATIONS}
VALID_CHANNEL = {item.code for item in CHANNELS}
VALID_INTENT = {item.code for item in INTENTS}
VALID_TEMPERATURE = {item.code for item in TEMPERATURES}


class InterpretError(RuntimeError):
    """Raised when the LLM response cannot be mapped to valid axes."""


def _format_catalog(title: str, options: list[AxisOption]) -> str:
    lines = [f"{title}:"]
    for option in options:
        lines.append(f"- {option.code}: {option.label} — {option.description}")
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
            _format_recipes(recipes),
        ]
    )
    system = (
        "You map plain-language pitch briefs to internal axis codes for Masters' Union Pitch Studio. "
        "Return strict JSON only with these keys: audience_cluster, duration, channel, intent, "
        "temperature, recipe_ref, summary, notes. "
        "Choose codes only from the catalogs provided. "
        "Duration and channel MUST follow the Setting text first. Pick the nearest existing duration "
        "code only: T0=30s, T1=2m, T2=5m, T3=10m, T4=30m, T5=90m. There is no 60-minute code — "
        "map 'about an hour' / 45-75 minutes to T5 (90 minutes), never invent a length. "
        "Set recipe_ref only when the audience clearly matches that persona. A matched persona must "
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


def normalize_interpret_payload(raw: dict[str, Any], known_refs: set[str]) -> InterpretResult:
    audience = str(raw.get("audience_cluster") or "").strip()
    duration = str(raw.get("duration") or "").strip()
    channel = str(raw.get("channel") or "").strip()
    intent = str(raw.get("intent") or "").strip()
    temperature = str(raw.get("temperature") or "").strip()
    recipe_ref = str(raw.get("recipe_ref") or "").strip()
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

    if recipe_ref and recipe_ref not in known_refs:
        recipe_ref = ""

    return InterpretResult(
        audience_cluster=audience,
        duration=duration,
        channel=channel,
        intent=intent,
        temperature=temperature,
        recipe_ref=recipe_ref,
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
    return normalize_interpret_payload(raw, known_refs)
